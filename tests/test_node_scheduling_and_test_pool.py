"""Node scheduling (node_compute_core.admit/plan) and the gate test pool
(test_runner_core).

Scheduling: a job goes to one node, and only to a node whose measured free CPU
and memory, less what the node keeps for its own LLM/NLP services and what the
host already sent it, cover the job. The test pool: a gate's pytest run split
into shards over the host's container and the nodes, merged into one result
that must equal what one run would have said.
"""
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile

import pytest

from vera.workers import node_compute_core as nc
from vera.workers import worker_placement_core as wp
from vera.evolve import test_runner_core as tr
from vera.evolve import evolve_unittest_core as ut

pytestmark = pytest.mark.critical

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = nc.merge_config({})
FULL = {"numpy": True, "torch": True, "sklearn": True, "testenv": True}


def _w(host, *, cpu_pct=10.0, ncpu=12, mem=40.0, status="idle", libs=FULL, hid=None):
    return {"role": "node-worker", "host": host, "host_id": hid or f"id-{host}",
            "status": status, "classes": json.dumps(["general", "cpu_compute"]),
            "libs": json.dumps(libs), "cpu_pct": str(cpu_pct), "ncpu": str(ncpu),
            "mem_avail_gb": str(mem), "ram_total_gb": "84", "metrics_at": "1000"}


# ── admission ─────────────────────────────────────────────────────────────────
def test_a_node_is_admitted_only_with_room_beyond_its_reserve():
    # 12 cores at 10% busy = 10.8 free; reserve 2; worldview needs 4
    ok, why, _ = nc.admit("worldview.train", _w("a"), CFG)
    assert ok, why
    # two LLM runners holding ~11 cores: no room for a 4-core job
    ok, why, _ = nc.admit("worldview.train", _w("a", cpu_pct=85), CFG)
    assert not ok and "CPU" in why
    # memory below need + reserve
    ok, why, _ = nc.admit("worldview.train", _w("a", mem=12), CFG)
    assert not ok and "memory" in why
    # a busy worker runs one task at a time
    ok, why, _ = nc.admit("ml.train", _w("a", status="running:nlp.ner"), CFG)
    assert not ok and "busy" in why
    # what this host already sent counts before the node's metrics show it
    ok, _, _ = nc.admit("worldview.train", _w("a", cpu_pct=40), CFG, {"cpu": 4.0, "mem_gb": 6})
    assert not ok
    # stale metrics are not trusted
    ok, why, _ = nc.admit("ml.train", _w("a"), CFG, now=1000 + nc.METRICS_STALE_S + 5)
    assert not ok and "metrics" in why


def test_plan_picks_the_node_with_most_room_and_skips_the_excluded():
    ws = [_w("busy", cpu_pct=70), _w("roomy", cpu_pct=5), _w("mid", cpu_pct=40)]
    p = nc.plan("worldview.train", ws, CFG)
    assert p["pick"]["host"] == "roomy"
    p = nc.plan("worldview.train", ws, CFG, exclude=["id-roomy"])
    assert p["pick"]["host"] == "mid"
    # a node a job was just sent to is taken, even with room left: shards
    # planned side by side spread over the nodes (2026-10-04)
    p = nc.plan("pytest.shard", ws, CFG, reserved={"id-roomy": {"cpu": 1.5, "mem_gb": 3, "jobs": 1}})
    assert p["pick"]["host"] == "mid"
    p = nc.plan("worldview.train", [_w("busy", cpu_pct=95)], CFG)
    assert p["pick"] is None and "busy" in p["why"]
    # a worker on older code reports no host id: never targeted
    old = dict(_w("old"), host_id="")
    assert nc.plan("ml.train", [old], CFG)["pick"] is None


def test_a_node_reads_its_own_queue_only_with_cpu_compute():
    s = wp.streams_to_read(is_worker=True, classes=["nlp", "cpu_compute"], host_id="h1")
    assert nc.node_stream("h1") in s and nc.NODE_COMPUTE_STREAM in s
    assert nc.node_stream("h1") not in wp.streams_to_read(is_worker=True, classes=["nlp"],
                                                          host_id="h1")
    assert nc.node_stream("h1") not in wp.streams_to_read(is_worker=False)


def test_a_node_checks_only_the_libraries_it_can_import():
    # `testenv` is a venv on disk, checked by the shard itself: judging it as a
    # library refused every gate shard on every node (2026-10-04)
    assert nc.node_missing("pytest.shard", {"numpy": True}) == []
    assert nc.node_missing("worldview.train", {"numpy": True, "torch": False}) == ["torch"]


def test_serial_slot_names():
    assert nc.serial_name("Training") == "training"
    assert nc.serial_name("") == "" and nc.serial_name("a b") == "" and nc.serial_name("x" * 65) == ""
    assert nc.serial_key("training") == "vera:compute:serial:training"


_SERIAL_SCRIPT = r'''
import asyncio, importlib.util, sys, time, types
root = sys.argv[1]

class FakeRedis:
    def __init__(self): self.kv = {}
    async def set(self, k, v, nx=False, ex=None):
        if nx and k in self.kv: return None
        self.kv[k] = v.encode() if isinstance(v, str) else v; return True
    async def get(self, k): return self.kv.get(k)
    async def expire(self, k, s): return True
    async def eval(self, script, n, key, token):
        if self.kv.get(key) == token.encode(): del self.kv[key]; return 1
        return 0

orch = types.ModuleType("Vera.vera.capability_orchestration")
orch.REDIS = FakeRedis()
orch.capability = lambda *a, **k: (lambda f: f)
async def emit_event(e): pass
orch.emit_event = emit_event
for name in ("Vera", "Vera.vera", "Vera.vera.workers"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["Vera.vera.capability_orchestration"] = orch
sys.modules["Vera"].vera = sys.modules["Vera.vera"]
sys.modules["Vera.vera"].capability_orchestration = orch
spec = importlib.util.spec_from_file_location("Vera.vera.workers.node_compute_core", root + "/vera/workers/node_compute_core.py")
core = importlib.util.module_from_spec(spec); spec.loader.exec_module(core)
sys.modules["Vera.vera.workers.node_compute_core"] = core
sys.modules["Vera.vera.workers"].node_compute_core = core
spec = importlib.util.spec_from_file_location("ncc", root + "/vera/workers/node_compute_capabilities.py")
ncc = importlib.util.module_from_spec(spec); spec.loader.exec_module(ncc)

order = []
async def fake_inner(fn, inputs, **kw):
    order.append(("start", inputs["n"])); await asyncio.sleep(0.3); order.append(("end", inputs["n"]))
    return {"n": inputs["n"]}
ncc._run_on_node = fake_inner
async def cfg(): return core.merge_config({"max_wait_s": 30})
ncc._config = cfg
_sleep = asyncio.sleep
async def fast_sleep(s): await _sleep(min(s, 0.05))
ncc.asyncio.sleep = fast_sleep

async def main():
    res = await asyncio.gather(ncc.run_on_node("ml.train", {"n": 1}, serial="training"),
                               ncc.run_on_node("ml.train", {"n": 2}, serial="training"))
    assert [r["n"] for r in res] == [1, 2]
    # one at a time: the second starts only after the first ends
    assert order[1][0] == "end", order
    assert core.serial_key("training") not in orch.REDIS.kv     # released
    # a slot held elsewhere past max_wait_s: refused, naming the holder
    orch.REDIS.kv[core.serial_key("training")] = b"other-host"
    try:
        await ncc.run_on_node("ml.train", {"n": 3}, serial="training", max_wait_s=0)
        raise SystemExit("not refused")
    except ncc.NodeComputeError as e:
        assert "other-host" in str(e), e
    print("ok")
asyncio.run(main())
'''


def test_a_serial_slot_runs_one_job_at_a_time_cluster_wide():
    import subprocess as sp
    r = sp.run([sys.executable, "-c", _SERIAL_SCRIPT, ROOT], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip().endswith("ok"), (r.stdout + r.stderr)[-3000:]


def test_every_function_has_a_resource_need():
    assert set(nc.FUNCTIONS) <= set(nc.NEEDS)


# ── the test pool ─────────────────────────────────────────────────────────────
def test_lock_drops_the_gpu_stack_and_editable_installs():
    freeze = "numpy==1.26.4\npytest==9.1.1\nnvidia-cublas-cu12==12.1\ntorch==2.7.1\n-e git+x#egg=y\nfoo @ file:///x\n"
    lines = tr.lock_lines(freeze)
    assert lines == ["numpy==1.26.4", "pytest==9.1.1"]
    assert tr.lock_sha(lines, "3.11") != tr.lock_sha(lines, "3.12")
    cmd = tr.testenv_build_cmd(lines, "abc123")
    assert "--no-deps" in cmd and "while read -r L" in cmd and "VERA_TESTENV_BUILT" in cmd


def test_parse_testenv_output():
    built = 'VERA_TESTENV_BUILT\n{"sha": "abc", "failed": " playwright==1.0", "python": "3.11.2"}\n'
    st = tr.parse_testenv_output(built)
    assert st["ok"] and st["state"] == "built" and st["failed"] == ["playwright==1.0"]
    assert tr.parse_testenv_output('VERA_TESTENV_CURRENT\n{"sha": "abc", "failed": ""}')["state"] == "current"
    bad = tr.parse_testenv_output("VERA_TESTENV_FAILED venv")
    assert not bad["ok"] and bad["error"] == "venv"


def test_shards_cover_every_file_once_and_balance():
    files = [f"tests/test_{i}.py" for i in range(40)]
    dur = {f: (100.0 if i == 0 else 1.0 + i % 5) for i, f in enumerate(files)}
    shards = tr.shard(files, dur, 4)
    flat = [f for s in shards for f in s]
    assert sorted(flat) == sorted(files) and len(flat) == len(set(flat))
    loads = sorted(sum(dur[f] for f in s) for s in shards)
    assert loads[-1] == 100.0          # the long file alone
    assert loads[0] > 30               # the rest spread evenly
    assert tr.shard(files[:2], {}, 5) and len(tr.shard(files[:2], {}, 5)) == 2


def test_durations_from_junit():
    xml = ('<testsuites><testsuite tests="3">'
           '<testcase classname="tests.test_a" name="x" time="1.5"/>'
           '<testcase classname="tests.test_a.TestK" name="y" time="0.5"/>'
           '<testcase classname="test_b" name="z" time="2"/></testsuite></testsuites>')
    assert tr.file_durations(xml) == {"tests/test_a.py": 2.0, "tests/test_b.py": 2.0}


def test_merge_sums_and_ignores_a_shard_with_no_critical_tests():
    a = {"passed": 10, "failed": 0, "errors": 0, "skipped": 1, "total": 11, "rc": 0}
    b = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "total": 0, "rc": 5}
    m = tr.merge([a, b])
    assert m["ok"] and m["total"] == 11 and m["rc"] == 0
    c = {"passed": 1, "failed": 1, "errors": 0, "skipped": 0, "total": 2, "rc": 1,
         "failure_details": [{"node_id": "t::f"}]}
    m = tr.merge([a, b, c])
    assert not m["ok"] and m["failed"] == 1 and m["failure_details"] == [{"node_id": "t::f"}]


def test_a_node_shard_runs_the_files_and_reports_in_the_gate_format(tmp_path):
    # a stand-in test venv: this interpreter (it has pytest)
    env_dir = tmp_path / "testenv"
    (env_dir / "bin").mkdir(parents=True)
    py = env_dir / "bin" / "python"
    py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
    (env_dir / tr.TESTENV_MARKER).write_text(json.dumps({"sha": "s1", "python": "x"}))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, body in (("tests/test_one.py", "def test_ok():\n    assert True\n\n"
                                                  "def test_bad():\n    assert 1 == 2\n"),
                           ("tests/test_two.py", "def test_ok2():\n    pass\n")):
            data = body.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    script = (
        "import json, sys\n"
        "from vera.evolve import test_runner_core as tr\n"
        "inputs = json.load(open(sys.argv[1]))\n"
        "inputs['archive'] = open(sys.argv[2], 'rb').read()\n"
        "print(json.dumps(tr.node_run_shard(inputs, lambda d: None, lambda: False)))\n")
    (tmp_path / "in.json").write_text(json.dumps({"files": ["tests/test_one.py", "tests/test_two.py"],
                                                   "markers": "", "testenv_sha": "s1", "timeout": 60}))
    (tmp_path / "a.tgz").write_bytes(buf.getvalue())
    r = subprocess.run([sys.executable, "-c", script, str(tmp_path / "in.json"), str(tmp_path / "a.tgz")],
                       cwd=ROOT, capture_output=True, text=True, timeout=120,
                       env={**os.environ, "PYTHONPATH": ROOT, "VERA_TESTENV_DIR": str(env_dir)})
    assert r.returncode == 0, r.stderr[-2000:]
    res = json.loads(r.stdout.strip().splitlines()[-1])
    p = ut.parse_pytest_output(res["out"])
    assert (p["passed"], p["failed"], p["total"]) == (2, 1, 3), res["out"][-1500:]
    assert any("test_bad" in d["node_id"] for d in p["failure_details"])
    # a stale venv refuses rather than running with the wrong packages
    (tmp_path / "in.json").write_text(json.dumps({"files": ["tests/test_one.py"], "testenv_sha": "other"}))
    r = subprocess.run([sys.executable, "-c", script, str(tmp_path / "in.json"), str(tmp_path / "a.tgz")],
                       cwd=ROOT, capture_output=True, text=True, timeout=120,
                       env={**os.environ, "PYTHONPATH": ROOT, "VERA_TESTENV_DIR": str(env_dir)})
    assert r.returncode != 0 and "test venv" in r.stderr
