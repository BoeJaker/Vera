"""Several compute jobs per node worker (ml-workshop-compute MW-7, owner
2026-10-11): a worker ran one task at a time and admission refused a node
with a job in flight, so a live 8-trial sweep ran in three waves (one trial
per node) while ~9 cores per node sat idle. A worker now runs compute jobs
side by side up to its slots; free CPU / memory still admit each job, and a
job counts on top of the node's metrics only until they show its load."""
import os

import pytest

from vera.workers import node_compute_core as nc

pytestmark = pytest.mark.critical

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = nc.merge_config({})
NOW = 10_000.0


def _w(**kw):
    w = {"host": "n1", "host_id": "id-n1", "status": "idle", "libs": '{"numpy": true}',
         "cpu_pct": "10", "ncpu": "12", "mem_avail_gb": "40", "ram_total_gb": "84", "metrics_at": str(NOW)}
    w.update({k: str(v) for k, v in kw.items()})
    return w


def test_a_slot_worker_takes_jobs_until_its_slots_or_cpu_run_out():
    w = _w(compute_slots=10, compute_running=3)
    ok, why, _ = nc.admit("ml.train", w, CFG, {"jobs": 3, "cpu": 3, "mem_gb": 3, "sent": []}, now=NOW)
    assert ok, why
    ok, why, _ = nc.admit("ml.train", _w(compute_slots=4, compute_running=4), CFG, {"jobs": 4}, now=NOW)
    assert not ok and "slots in use" in why
    # running a non-compute task does not block a compute job any more
    ok, why, _ = nc.admit("ml.train", _w(compute_slots=10, status="running:nlp.ner"), CFG, {}, now=NOW)
    assert ok, why
    assert not nc.admit("ml.train", _w(compute_slots=10, status="starting"), CFG, {}, now=NOW)[0]
    # free CPU still decides: 10.8 free - 2 reserve - 8 just sent < 1
    sent = [[NOW - 1, 1.0, 1.0]] * 8
    ok, why, _ = nc.admit("ml.train", _w(compute_slots=10), CFG,
                          {"jobs": 8, "cpu": 8, "mem_gb": 8, "sent": sent}, now=NOW)
    assert not ok and "CPU" in why


def test_an_older_worker_keeps_one_job_at_a_time():
    ok, why, _ = nc.admit("ml.train", _w(), CFG, {"jobs": 1, "cpu": 1, "mem_gb": 1}, now=NOW)
    assert not ok and "just sent" in why
    assert not nc.admit("ml.train", _w(status="running:x"), CFG, {}, now=NOW)[0]


def test_a_job_counts_on_top_only_until_the_metrics_show_it():
    res = {"cpu": 5.0, "mem_gb": 5.0, "sent": [[NOW - 60, 4.0, 4.0], [NOW - 5, 1.0, 1.0]]}
    assert nc.reserved_now(res, NOW) == (1.0, 1.0)              # the old one is in the metrics
    assert nc.reserved_now({"cpu": 2.0, "mem_gb": 3.0}, NOW) == (2.0, 3.0)   # no send times: all of it


def test_only_compute_jobs_run_detached_and_slots_leave_room():
    assert nc.run_detached("compute.run", True, 10)
    assert not nc.run_detached("compute.run", True, 1)
    assert not nc.run_detached("compute.run", False, 10)       # the host
    assert not nc.run_detached("nlp.ner", True, 10)
    assert nc.compute_slots_default(12) == 10 and nc.compute_slots_default(2) == 1


def test_the_worker_and_dispatcher_are_wired():
    orch = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    assert "_compute_core.run_detached(" in orch and '"compute_slots": _compute_slots' in orch
    assert '"compute_running": str(w.get("compute_running", 0))' in orch
    caps = open(os.path.join(ROOT, "vera", "workers", "node_compute_capabilities.py"), encoding="utf-8").read()
    assert "_reserve(hid, fn, -1, _entry)" in caps
    ml = open(os.path.join(ROOT, "vera", "machine learning", "ml_training.py"), encoding="utf-8").read()
    assert '"trained_on": job.get("trained_on", "")' in ml
