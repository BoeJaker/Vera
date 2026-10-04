"""4 Oct 2026: four releases in 80 minutes each threw away every gate in flight, and the restart
resume re-ran 20 gates one at a time. A release now waits for RUNNING gates while new gates
wait for the release (the drain key), and cut-off gates resume in parallel."""
import asyncio
import importlib.util
import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from vera.evolve import evolve_capabilities as evolve

pytestmark = pytest.mark.critical
ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rc_gates", ROOT / "vera" / "evolve" / "release_core.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)
REL = (ROOT / "vera" / "evolve" / "release_capabilities.py").read_text(encoding="utf-8")
CLEAR = sorted(R.CLEAR_STATES)[0]


# ── release_core ─────────────────────────────────────────────────────────────

def test_running_gates_hold_a_release_and_force_overrides():
    c = {"busy": False, "state": CLEAR, "gates": [{"id": "p1", "branch": "fix/a", "state": "running"}]}
    ok, why = R.may_release("finish", c)
    assert ok is False and "1 gate(s) running (fix/a)" in why and "force" in why
    assert R.waiting_on_gates(why) is True
    assert R.may_release("force", c)[0] is True
    assert R.may_release("finish", {"busy": False, "state": CLEAR, "gates": []})[0] is True


def test_loops_and_census_reasons_are_not_gate_waits():
    assert R.waiting_on_gates(R.may_release("finish", {"busy": False, "state": CLEAR, "loops": ["x"]})[1]) is False
    assert R.waiting_on_gates(R.may_release("finish", {"busy": True, "state": "running", "goal": "g"})[1]) is False


def test_census_summary_reports_only_running_gates():
    body = REL[REL.index("async def census_summary()"):]
    body = body[:body.index("\n\n\nasync def ")]
    assert '"_GATES_ACTIVE"' in body and 'if g.get("state") == "running"]' in body
    tick = REL[REL.index("async def run_tick()"):]
    assert "await _drain(core.waiting_on_gates(act.get(\"why\")))" in tick
    assert "await _drain(False)" in tick


# ── a gate waits for a draining release ──────────────────────────────────────

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


def test_a_gate_does_not_start_while_a_release_drains(monkeypatch):
    redis, started = FakeRedis(), []
    redis.kv[R.DRAIN_KEY] = b"1"

    async def unittest_run(branch, paths, markers, timeout, pipeline_id):
        started.append(branch)
        return {"ok": True, "summary": "PASS"}

    async def save(_rec):
        return None

    monkeypatch.setattr(evolve, "evolve_unittest_run", unittest_run)
    monkeypatch.setattr(evolve, "_save_pipeline", save)
    monkeypatch.setattr(evolve, "_redis", lambda: redis)
    monkeypatch.setattr(evolve, "_CRITICAL_TIER_POLL_S", 0.01)
    monkeypatch.setattr(evolve, "_critical_tier_lock", None)
    rec = {"id": "p9"}

    async def run():
        task = asyncio.ensure_future(evolve._critical_tier_queued(rec, "fix/p9"))
        await asyncio.sleep(0.08)
        assert not started, "must not start while the release drains"
        assert evolve._GATES_ACTIVE["p9"]["state"] == "queued"
        del redis.kv[R.DRAIN_KEY]                       # the release went out
        return await task

    assert asyncio.run(run())["ok"] and started == ["fix/p9"]
    assert any("a release is waiting" in s["detail"] for s in rec["steps"])


# ── resume runs in parallel ───────────────────────────────────────────────────

NOW = datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc)


def _rec(pid, minutes_ago):
    return {"id": pid, "adopted": True, "status": "adopted", "decision": "pending", "branch": f"fix/{pid}",
            "gate_passed": None, "current": "critical tier queued behind another gate in this process",
            "to": "bleeding-edge", "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
            "steps": [{"stage": "gate", "ok": True}, {"stage": "critical-queue", "ok": True}]}


def test_resume_runs_gates_side_by_side_up_to_the_limit(monkeypatch):
    redis = FakeRedis()
    recs = {f"g{i}": _rec(f"g{i}", 10 + i) for i in range(5)}
    redis.lists[evolve.KEY_PIPELINES] = [json.dumps({"id": k, "adopted": True, "gate_passed": None}) for k in recs]
    for k, v in recs.items():
        redis.kv[evolve.KEY_PIPELINE + k] = json.dumps(v).encode()
    live, peak = [0], [0]

    async def crit(rec, branch):
        live[0] += 1
        peak[0] = max(peak[0], live[0])
        await asyncio.sleep(0.05)
        live[0] -= 1
        return True

    async def nothing(*a, **k):
        return None

    async def git(*args, **kw):
        return {"ok": True, "out": "abc a commit\n"}

    async def root(_repo):
        return "/repo"

    monkeypatch.setattr(evolve, "_redis", lambda: redis)
    monkeypatch.setattr(evolve, "_critical_gate", crit)
    monkeypatch.setattr(evolve, "_save_pipeline", nothing)
    monkeypatch.setattr(evolve, "_audit", nothing)
    monkeypatch.setattr(evolve, "emit_event", nothing)
    monkeypatch.setattr(evolve, "_git", git)
    monkeypatch.setattr(evolve, "_resolve_repo_root", root)
    monkeypatch.setattr(evolve, "_GATE_RESUME_DELAY_S", 0)
    monkeypatch.setattr(evolve, "_GATE_RESUME_PARALLEL", 3)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    import datetime as _dtmod
    monkeypatch.setattr(_dtmod, "datetime", _Clock)
    asyncio.run(evolve._resume_interrupted_gates())
    assert peak[0] == 3, "resumed gates run side by side, capped at the limit"
