"""Gate resume keeps the newest pipeline per branch and skips branches already merged
(4 Oct 2026: the first live resume picked up 18 gates - the same branch re-adopted two or
three times, and branches already on bleeding-edge). /health's activity also lists loops
started through /mcp/call."""
import asyncio
import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from vera.evolve import evolve_capabilities as evolve
from vera.evolve import gate_resume_core as G

pytestmark = pytest.mark.critical
ROOT = pathlib.Path(__file__).resolve().parents[1]
CO = (ROOT / "vera" / "capability_orchestration.py").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 4, 17, 0, tzinfo=timezone.utc)


def _rec(pid, branch, minutes_ago):
    return {"id": pid, "adopted": True, "status": "adopted", "decision": "pending", "branch": branch,
            "gate_passed": None, "current": "critical tier queued behind another gate in this process",
            "to": "bleeding-edge", "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
            "steps": [{"stage": "gate", "ok": True}, {"stage": "critical-queue", "ok": True}]}


def test_latest_per_branch():
    recs = [_rec("ui1", "feat/ui", 90), _rec("ui3", "feat/ui", 10), _rec("ui2", "feat/ui", 40),
            _rec("x", "fix/x", 50)]
    keep, sup = G.latest_per_branch(recs)
    assert [r["id"] for r in keep] == ["x", "ui3"]               # oldest first
    assert sorted((o["id"], n) for o, n in sup) == [("ui1", "ui3"), ("ui2", "ui3")]


class FakeRedis:
    def __init__(self):
        self.kv, self.lists = {}, {}

    async def get(self, key):
        return self.kv.get(key)

    async def delete(self, key):
        return 1 if self.kv.pop(key, None) is not None else 0

    async def lrange(self, key, a, b):
        return self.lists.get(key, [])[a:b + 1]


def test_resume_runs_only_the_newest_unmerged(monkeypatch):
    redis = FakeRedis()
    recs = {"ui1": _rec("ui1", "feat/ui", 90), "ui3": _rec("ui3", "feat/ui", 10),
            "merged": _rec("merged", "feat/done", 30), "x": _rec("x", "fix/x", 50)}
    redis.lists[evolve.KEY_PIPELINES] = [json.dumps({"id": k, "adopted": True, "gate_passed": None}) for k in recs]
    for k, v in recs.items():
        redis.kv[evolve.KEY_PIPELINE + k] = json.dumps(v).encode()
    ran, saved = [], {}

    async def crit(rec, branch):
        ran.append(rec["id"])
        return True

    async def save(rec):
        saved[rec["id"]] = json.loads(json.dumps(rec))

    async def nothing(*a, **k):
        return None

    async def git(*args, **kw):
        rng = args[-1]
        return {"ok": True, "out": "" if rng.endswith("..feat/done") else "abc123 a commit\n"}

    async def root(_repo):
        return "/repo"

    monkeypatch.setattr(evolve, "_redis", lambda: redis)
    monkeypatch.setattr(evolve, "_critical_gate", crit)
    monkeypatch.setattr(evolve, "_save_pipeline", save)
    monkeypatch.setattr(evolve, "_audit", nothing)
    monkeypatch.setattr(evolve, "emit_event", nothing)
    monkeypatch.setattr(evolve, "_git", git)
    monkeypatch.setattr(evolve, "_resolve_repo_root", root)
    monkeypatch.setattr(evolve, "_GATE_RESUME_DELAY_S", 0)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    import datetime as _dtmod
    monkeypatch.setattr(_dtmod, "datetime", _Clock)
    asyncio.run(evolve._resume_interrupted_gates())
    assert ran == ["x", "ui3"]
    assert saved["ui1"]["steps"][-1]["stage"] == "gate-resume-skipped" and "ui3" in saved["ui1"]["current"]
    assert saved["merged"]["current"] == "gate not resumed - branch already merged"
    assert saved["ui3"]["gate_passed"] is True
    # skipped pipelines are not picked up by the next restart
    assert not G.interrupted(saved["ui1"], NOW) and not G.interrupted(saved["merged"], NOW)


def test_health_activity_lists_mcp_started_loops():
    h = CO[CO.index("async def _health_activity()"):]
    h = h[:h.index("\n\n\nasync def ")]
    assert '_live = getattr(dm, "_live_loop_session_ids", None)' in h
    assert "ids += [s for s in (await _live())[:20] if s not in ids]" in h
