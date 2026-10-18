"""4 Oct 2026: a release restarted prod while gates were queued "behind another gate in this
process" - nothing would ever run them - and /health only knew about the census, so probe
loops and gates in flight were invisible. Gates now resume after a restart; /health's
`activity` lists loops, gates and declared tests/probes."""
import asyncio
import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from vera.evolve import evolve_capabilities as evolve
from vera.evolve import gate_resume_core as G
from vera import activity_health_core as A

pytestmark = pytest.mark.critical
ROOT = pathlib.Path(__file__).resolve().parents[1]
CO = (ROOT / "vera" / "capability_orchestration.py").read_text(encoding="utf-8")
EV = (ROOT / "vera" / "evolve" / "evolve_capabilities.py").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)


def _rec(pid, current, minutes_ago=10, gate_passed=None, last="critical-queue", ok_steps=True):
    return {"id": pid, "adopted": True, "status": "adopted", "decision": "pending", "branch": f"fix/{pid}",
            "gate_passed": gate_passed, "current": current, "to": "bleeding-edge",
            "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
            "steps": [{"stage": "gate", "ok": ok_steps, "detail": "compile"},
                      {"stage": last, "ok": True, "detail": "x"}]}


# ── gate_resume_core ──────────────────────────────────────────────────────────

def test_interrupted_finds_queued_and_running_gates_only():
    assert G.interrupted(_rec("a", "critical tier queued behind another gate in this process"), NOW)
    assert G.interrupted(_rec("b", "running critical tier", last="adopt"), NOW)
    assert G.interrupted(_rec("c", "gate resumed after restart", last="gate-resume"), NOW)
    assert not G.interrupted(_rec("d", "running critical tier", gate_passed=True), NOW)
    assert not G.interrupted(_rec("e", "adopted an existing hand-authored branch", last="gate"), NOW)
    assert not G.interrupted(_rec("f", "running critical tier", minutes_ago=60 * 30), NOW)
    r = _rec("g", "running critical tier"); r["decision"] = "promoted"
    assert not G.interrupted(r, NOW)


def test_compile_ok_and_lease_and_order():
    assert G.compile_ok(_rec("a", "x")["steps"]) is True
    assert G.compile_ok(_rec("a", "x", ok_steps=False)["steps"]) is False
    assert G.orphaned_lease(b"abc123:fix/x", ["abc123"]) is True
    assert G.orphaned_lease("zzz:fix/y", ["abc123"]) is False and G.orphaned_lease(None, ["a"]) is False
    order = G.resume_order([_rec("new", "x", 1), _rec("old", "x", 30)])
    assert [r["id"] for r in order] == ["old", "new"]


# ── the gate says what it is doing, while it does it ─────────────────────────

class FakeRedis:
    def __init__(self):
        self.kv, self.lists = {}, {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value.encode() if isinstance(value, str) else value
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def delete(self, key):
        return 1 if self.kv.pop(key, None) is not None else 0

    async def eval(self, _s, _n, key, token):
        if self.kv.get(key) == token.encode():
            del self.kv[key]
            return 1
        return 0

    async def lrange(self, key, a, b):
        return self.lists.get(key, [])[a:b + 1]


def test_gates_active_shows_queued_then_running_then_clears(monkeypatch):
    redis, seen = FakeRedis(), []

    async def unittest_run(branch, paths, markers, timeout, pipeline_id):
        seen.append(dict(evolve._GATES_ACTIVE[pipeline_id]))
        return {"ok": True, "summary": "PASS"}

    saved = []

    async def save(rec):
        saved.append(rec.get("current"))

    monkeypatch.setattr(evolve, "evolve_unittest_run", unittest_run)
    monkeypatch.setattr(evolve, "_save_pipeline", save)
    monkeypatch.setattr(evolve, "_redis", lambda: redis)
    monkeypatch.setattr(evolve, "_critical_tier_lock", None)
    assert asyncio.run(evolve._critical_tier_queued({"id": "p1"}, "fix/p1"))["ok"]
    assert seen == [{"id": "p1", "branch": "fix/p1", "state": "running", "since": seen[0]["since"]}]
    assert evolve._GATES_ACTIVE == {}
    assert "running critical tier" in saved          # on the record even when not queued


# ── a restart's orphans run again ─────────────────────────────────────────────

def test_resume_runs_the_cut_off_gates_oldest_first_and_frees_their_lease(monkeypatch):
    redis = FakeRedis()
    recs = {"old": _rec("old", "critical tier queued behind another gate in this process", 40),
            "new": _rec("new", "running critical tier", 5, last="adopt"),
            "done": _rec("done", "running critical tier", 20, gate_passed=True),
            "ancient": _rec("ancient", "running critical tier", 60 * 30)}
    redis.lists[evolve.KEY_PIPELINES] = [json.dumps({"id": k, "adopted": True, "gate_passed": v["gate_passed"]})
                                         for k, v in recs.items()]
    for k, v in recs.items():
        redis.kv[evolve.KEY_PIPELINE + k] = json.dumps(v).encode()
    redis.kv[evolve._CRITICAL_TIER_LEASE_KEY] = b"old:fix/old"     # held by a dead gate
    ran, saved = [], {}

    async def crit(rec, branch):
        ran.append(rec["id"])
        assert evolve._CRITICAL_TIER_LEASE_KEY not in redis.kv, "the orphan's lease is freed first"
        return rec["id"] != "new"                                    # 'new' fails its tests

    async def save(rec):
        saved[rec["id"]] = json.loads(json.dumps(rec))

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(evolve, "_redis", lambda: redis)
    monkeypatch.setattr(evolve, "_critical_gate", crit)
    monkeypatch.setattr(evolve, "_save_pipeline", save)
    monkeypatch.setattr(evolve, "_audit", nothing)
    monkeypatch.setattr(evolve, "emit_event", nothing)
    monkeypatch.setattr(evolve, "_GATE_RESUME_DELAY_S", 0)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    import datetime as _dtmod
    monkeypatch.setattr(_dtmod, "datetime", _Clock)
    asyncio.run(evolve._resume_interrupted_gates())
    assert ran == ["old", "new"]
    assert saved["old"]["gate_passed"] is True and saved["new"]["gate_passed"] is False
    assert any(s["stage"] == "gate-resume" for s in saved["old"]["steps"])
    assert saved["old"]["current"] == "gate finished after a restart"


def test_a_live_lease_from_another_gate_is_left_alone(monkeypatch):
    redis = FakeRedis()
    r = _rec("mine", "critical tier queued behind another gate in this process", 5)
    redis.lists[evolve.KEY_PIPELINES] = [json.dumps({"id": "mine", "adopted": True, "gate_passed": None})]
    redis.kv[evolve.KEY_PIPELINE + "mine"] = json.dumps(r).encode()
    redis.kv[evolve._CRITICAL_TIER_LEASE_KEY] = b"someone-else:fix/x"

    async def crit(rec, branch):
        return True

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(evolve, "_redis", lambda: redis)
    monkeypatch.setattr(evolve, "_critical_gate", crit)
    monkeypatch.setattr(evolve, "_save_pipeline", nothing)
    monkeypatch.setattr(evolve, "_audit", nothing)
    monkeypatch.setattr(evolve, "emit_event", nothing)
    monkeypatch.setattr(evolve, "_GATE_RESUME_DELAY_S", 0)
    asyncio.run(evolve._resume_interrupted_gates())
    assert redis.kv[evolve._CRITICAL_TIER_LEASE_KEY] == b"someone-else:fix/x"


def test_adopt_and_startup_are_wired():
    adopt = EV[EV.index("async def evolve_pipeline_adopt("):]
    assert "critical_ok = await _critical_gate(rec, branch)" in adopt
    assert "_resume_interrupted_gates())" in EV[EV.index("async def _startup():"):]


# ── /health activity ──────────────────────────────────────────────────────────

def test_summarise():
    idle = A.summarise([], [], [])
    assert idle == {"busy": False, "summary": "idle", "loops": [], "gates": [], "declared": []}
    s = A.summarise([{"session_id": "s1", "goal": "timer"}],
                    [{"id": "p1", "branch": "fix/a", "state": "running"}, {"id": "p2", "branch": "fix/b", "state": "queued"}],
                    [{"id": "batch1b", "kind": "probe", "label": "batch1b fix checks", "by": "claude:0c68dd8c"}])
    assert s["busy"] is True
    assert s["summary"] == ("probe batch1b fix checks (claude:0c68dd8c) · 1 agent loop · "
                            "gate running: fix/a · 1 gate queued")


def test_declare_bounds():
    assert A.clamp_ttl(10) == 60 and A.clamp_ttl(10 ** 9) == A.MAX_TTL_S and A.clamp_ttl("x") == A.DEFAULT_TTL_S
    assert A.norm_kind("Probe") == "probe" and A.norm_kind("weird") == "other"


def test_health_and_caps_are_wired():
    assert '"activity":await _health_activity(),' in CO
    assert '@capability("sys.activity.declare"' in CO and '@capability("sys.activity.clear"' in CO
    h = CO[CO.index("async def _health_activity()"):]
    assert '"_AGENT_LOOP_TASKS"' in h[:1500] and '"_GATES_ACTIVE"' in h[:2000]


def test_a_pooled_gate_cut_off_while_its_chunks_waited_is_resumed():
    # 18 Oct 2026: the pool's own notes are the last step while chunks wait; a restart then stranded the pipeline
    rec = _rec("p", "test runners all busy - waiting for one", last="test-pool")
    assert G.interrupted(rec, NOW)
    rec2 = _rec("q", "1079 test files in 12 chunks over the host and the nodes as they free up", last="test-pool")
    assert G.interrupted(rec2, NOW)
