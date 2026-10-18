"""Work a node reserves for itself (P0d, 18 Oct 2026, agreed with vera-6e for the crawl worker class).

A class-stream task (ingress.fetch.batch on vera:tasks:cls:crawl) is taken by whichever idle worker reads it, so the
host cannot reserve for it as it does for a job it sends (node_compute._reserve). The worker writes its own
reservation into vera:node_compute:extra_res:<host_id>; admission adds it on top; a field past its `until` is
ignored and pruned, so a killed worker holds nothing.
"""
import json
import os

import pytest

from vera.workers import node_compute_core as core

pytestmark = pytest.mark.critical


def _w(**kw):
    w = {"host": "cpu-246", "host_id": "h246", "status": "idle", "ncpu": 12, "cpu_pct": 0.0, "cpu_scope": "pinned",
         "cpu_pct_avg": 50.0, "ram_total_gb": 64, "mem_avail_gb": 20.0, "metrics_at": 1000.0, "compute_slots": 4}
    w.update(kw)
    return w


CFG = {"reserve_cpu": 2.0, "reserve_mem_gb": 8.0}


def test_an_entry_is_the_caps_needs_until_its_timeout():
    core.NEEDS.setdefault("test.selfres", {"cpu": 0.25, "mem_gb": 0.25})
    e = json.loads(core.extra_entry("test.selfres", 300, now=1000.0))
    assert e == {"cpu": 0.25, "mem_gb": 0.25, "until": 1300.0, "cap": "test.selfres"}


def test_live_fields_sum_and_expired_or_broken_ones_are_named_for_pruning():
    f = {b"t1": json.dumps({"cpu": 0.25, "mem_gb": 0.25, "until": 2000, "cap": "ingress.fetch.batch"}),
         "t2": json.dumps({"cpu": 0.25, "mem_gb": 0.5, "until": 2000, "cap": "ingress.fetch.batch"}),
         "old": json.dumps({"cpu": 4, "mem_gb": 4, "until": 900}),
         "junk": "not json"}
    x, dead = core.extra_from(f, now=1000.0)
    assert x == {"cpu": 0.5, "mem_gb": 0.75, "n": 2, "caps": ["ingress.fetch.batch"]}
    assert sorted(dead) == ["junk", "old"]
    assert core.extra_from(None, 0) == ({"cpu": 0.0, "mem_gb": 0.0, "n": 0, "caps": []}, [])


def test_admission_counts_them_on_top_and_says_so():
    w = _w()                                      # 6 cores free, 2 kept spare -> 4 left: one 4-core chunk fits
    assert core.admit("pytest.shard", w, CFG, {}, now=1000.0)[0] is True
    extra = {"h246": {"cpu": 0.5, "mem_gb": 0.5, "n": 2, "caps": ["ingress.fetch.batch"]}}
    res = core.with_extra({}, extra)["h246"]
    ok, why, _ = core.admit("pytest.shard", w, CFG, res, now=1000.0)
    assert ok is False and "ingress.fetch.batch 2 task(s) holding 0.50 cores / 0.50 GB" in why, why


def test_with_extra_leaves_the_hosts_own_reservations_alone():
    mine = {"h246": {"cpu": 4.0, "mem_gb": 6.0, "jobs": 1.0, "sent": [[1.0, 4.0, 6.0]]}}
    out = core.with_extra(mine, {"h246": {"cpu": 0.25, "mem_gb": 0.25, "n": 1, "caps": []}, "h247": {"n": 0}})
    assert out["h246"]["cpu"] == 4.0 and out["h246"]["extra_cpu"] == 0.25
    assert "h247" not in out, "a node with nothing reserved adds no row"
    assert "extra_cpu" not in mine["h246"], "the host's own map is not changed"


def test_the_planner_reads_the_hashes_best_effort():
    src = open(os.path.join(os.path.dirname(__file__), "..", "vera", "workers", "node_compute_capabilities.py"),
               encoding="utf-8").read()
    body = src[src.index("async def _extra_reserved("):src.index("async def node_plan(")]
    assert "core.KEY_EXTRA_RES + hid" in body and "hdel" in body and "except Exception" in body
    plan = src[src.index("async def node_plan("):]
    plan = plan[:plan.index("\n\n\n")]
    assert "core.with_extra(_RESERVED, await _extra_reserved(ws))" in plan
