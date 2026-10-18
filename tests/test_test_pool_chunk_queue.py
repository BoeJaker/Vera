"""A gate's test run is a queue of chunks every free runner pulls from.

2026-10-05: 7 of the last 25 gates ran as ONE shard - a gate that started
while others held the runners took the one runner free at its start and ran
the whole tier there (8-12 min) while runners freed up beside it. Now the tier
is split into chunks and each free runner takes the next one until none are
left. This drives the real run_pooled against a fake host container and three
fake nodes, all busy at first.
"""
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPT = r'''
import asyncio, importlib.util, json, sys, time, types
root = sys.argv[1]
REAL_SLEEP = asyncio.sleep

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m); return m

for n in ("Vera", "Vera.vera", "Vera.vera.evolve", "Vera.vera.workers"):
    sys.modules[n] = types.ModuleType(n)

class FakeRedis:
    def __init__(self): self.kv = {}
    async def get(self, k): return self.kv.get(k)
    async def set(self, k, v, nx=False, ex=None):
        if nx and k in self.kv: return None
        self.kv[k] = v; return True
    async def delete(self, *ks):
        for k in ks: self.kv.pop(k, None)
    async def eval(self, script, n, key, token):
        if self.kv.get(key) == token: del self.kv[key]
    async def hgetall(self, k): return {}
    async def hset(self, *a, **k): pass

orch = types.ModuleType("Vera.vera.capability_orchestration")
orch.REDIS = FakeRedis()
orch.capability = lambda *a, **k: (lambda f: f)
async def emit_event(e): pass
orch.emit_event = emit_event
sys.modules["Vera.vera.capability_orchestration"] = orch
core = load("Vera.vera.evolve.test_runner_core", root + "/vera/evolve/test_runner_core.py")
nc = load("Vera.vera.workers.node_compute_core", root + "/vera/workers/node_compute_core.py")
ut = load("ut_core", root + "/vera/evolve/evolve_unittest_core.py")

FILES = [f"test_{i:03d}.py" for i in range(48)]
ev = types.ModuleType("evolve_capabilities")
async def wt(branch): return "/wt"
ev._branch_worktree = wt
ev._pstep = lambda rec, stage, ok, detail: rec.setdefault("steps", []).append(detail)
async def noop(*a, **k): pass
ev._save_pipeline = noop; ev._audit = noop; ev._record_unittest_run = noop
ev.DEV_IMAGE = "img"; ev._ut_parse = ut.parse_pytest_output
ev._CRITICAL_TIER_LEASE_S = 600; ev._CRITICAL_TIER_RELEASE_LUA = ""
sys.modules["evolve_capabilities"] = ev

import os as _os
_os.path.isdir = lambda p: True
_os.listdir = lambda p: FILES
trc = load("trc", root + "/vera/evolve/test_runner_capabilities.py")

def junit(files, fail=()):
    cases = "".join(f'<testcase classname="tests.{f[:-3]}" name="t" time="0.1">'
                    + ('<failure message="x"/>' if f in fail else "") + "</testcase>" for f in files)
    return ("out\n__VERA_JUNIT__\n<testsuites><testsuite tests=\"%d\" failures=\"%d\">%s</testsuite></testsuites>\n__VERA_RC=%d__"
            % (len(files), len([f for f in files if f in fail]), cases, 1 if any(f in fail for f in files) else 0))

ran = []          # (runner, file)
node_free_at = time.time() + 0.3          # every node busy for the first 0.3 s
calls = {"node_fail_once": True}

async def host_shard(wtree, files, markers, timeout, **kw):
    seen_host_kw.update(kw)
    await REAL_SLEEP(0.15)
    ran.extend(("host", f) for f in files)
    out = junit(files)
    return {"parsed": ut.parse_pytest_output(out), "out": out}
seen_host_kw = {}
real_host_shard = trc._host_shard
trc._host_shard = host_shard
async def archive(w): return b"tgz"
trc._archive = archive

free_nodes = ["n1", "n2", "n3"]
taken = set()
async def node_slots(k):
    if time.time() < node_free_at:
        return []
    return [n for n in free_nodes if n not in taken][:k]
trc._node_slots = node_slots

async def node_compute(fn, inputs, **kw):
    n = next(x for x in free_nodes if x not in taken)
    taken.add(n)
    try:
        await REAL_SLEEP(0.15)
        assert "archive" in inputs.get("_blob_refs", {}), inputs.keys()
        if calls["node_fail_once"]:
            calls["node_fail_once"] = False
            raise RuntimeError(f"{n} stopped answering")
        ran.extend((n, f) for f in inputs["files"])
        return {"out": junit(inputs["files"]), "_node": n}
    finally:
        taken.discard(n)
orch.NODE_COMPUTE = node_compute
orch.NODE_PLAN = object()

async def cfg(): return core.merge_pool_config({"enabled": True, "chunks": 12, "min_files": 1})
trc._pool_config = cfg
_sleep = asyncio.sleep
async def fast(s): await _sleep(min(s, 0.02))
trc.asyncio.sleep = fast

async def main():
    rec = {"id": "p1"}
    res = await trc.run_pooled(rec, "b", force=True)
    files = [f for _, f in ran]
    assert sorted(files) == sorted(f"tests/{f}" for f in FILES), "every file exactly once"
    runners = {r for r, _ in ran}
    assert {"host", "n1", "n2", "n3"} <= runners, runners
    assert res["passed"] == len(FILES) and res["ok"], res["summary"]
    assert any(s.get("requeued") for s in res["pool"]["shards"]), "a failed chunk is requeued"
    assert seen_host_kw.get("parallel") == 3, seen_host_kw     # host_parallel default

    # a host chunk runs as `parallel` containers side by side, one combined result
    started = []
    ev._ut_sanitize = lambda paths, markers, extra: (paths.split(), "")
    ev._ut_inner = lambda tokens: " ".join(tokens)
    ev._ut_argv = lambda image, w, inner: ["docker", inner]
    async def sh(argv, timeout=0):
        started.append(time.time())
        files = argv[1].split()
        await REAL_SLEEP(0.1)
        return {"out": junit(files, fail=("tests/test_007.py",)), "err": ""}
    ev._sh = sh
    nine = [f"tests/test_{i:03d}.py" for i in range(9)]
    r = await real_host_shard("/wt", nine, "critical", 60, parallel=3,
                              durations={"tests/test_000.py": 5.0})
    assert len(started) == 3 and max(started) - min(started) < 0.08, "3 containers side by side"
    p = r["parsed"]
    assert (p["total"], p["failed"], p["rc"]) == (9, 1, 1), p
    assert r["out"].count("__VERA_RC=") == 1
    started.clear()
    r1 = await real_host_shard("/wt", nine[:2], "critical", 60, parallel=1)
    assert len(started) == 1 and r1["parsed"]["total"] == 2

    # evolve.unittest.run through the pool: no pipeline record is written,
    # the run is still recorded under the caller's pipeline id
    ran.clear()
    recorded = []
    async def record(parsed, **kw): recorded.append(kw)
    ev._record_unittest_run = record
    saved = []
    async def save(rec): saved.append(rec)
    ev._save_pipeline = save
    res2 = await trc.run_for_unittest("b", markers="critical", timeout=60, pipeline_id="pX")
    assert res2 is not None and res2["passed"] == len(FILES), res2 and res2.get("summary")
    assert saved == [], "no pipeline record touched"
    assert recorded and recorded[-1]["pipeline_id"] == "pX", recorded
    print("ok", sorted(runners), len(res["pool"]["shards"]))
asyncio.run(main())
'''


def test_chunks_spread_over_every_runner_as_they_free_up():
    r = subprocess.run([sys.executable, "-c", SCRIPT, ROOT], capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0 and r.stdout.strip().startswith("ok"), (r.stdout + r.stderr)[-3000:]


def test_pool_config_has_chunks():
    sys.path.insert(0, ROOT)
    from vera.evolve import test_runner_core as core
    assert core.merge_pool_config({})["chunks"] == 12
    assert core.merge_pool_config({"chunks": 500})["chunks"] == 64


def test_placement_counts_runners_host_first():
    """17 Oct 2026: vera.log gets one line per gate saying where its chunks ran."""
    import importlib.util as _u
    _sp = _u.spec_from_file_location("trc_placement", os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "vera", "evolve", "test_runner_core.py"))
    _trc = _u.module_from_spec(_sp)
    _sp.loader.exec_module(_trc)
    shards = [{"runner": "Ollama-C"}, {"runner": "host"}, {"runner": "Ollama"}, {"runner": "host"},
              {"runner": "node", "error": "x", "requeued": True}]
    assert _trc.placement(shards) == "host 2, Ollama 1, Ollama-C 1 (1 requeued)"
    assert _trc.placement([]) == "no chunks"
