"""An orphaned gate is not re-run when its branch has a NEWER pipeline in any state (4 Oct
2026: orphans of branches already re-gated, promoted and live were re-run by the resume)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from vera.evolve import evolve_capabilities as evolve
from vera.evolve import gate_resume_core as G

pytestmark = pytest.mark.critical
NOW = datetime(2026, 10, 4, 19, 0, tzinfo=timezone.utc)


def _iso(m):
    return (NOW - timedelta(minutes=m)).isoformat()


def test_newer_pipeline_any_state():
    rec = {"id": "old", "branch": "fix/a", "created_at": _iso(60)}
    rows = [{"id": "old", "branch": "fix/a", "created_at": _iso(60)},
            {"id": "mid", "branch": "fix/a", "created_at": _iso(30), "gate_passed": True},
            {"id": "new", "branch": "fix/a", "created_at": _iso(10), "gate_passed": None},
            {"id": "other", "branch": "fix/b", "created_at": _iso(5)},
            {"id": "older", "branch": "fix/a", "created_at": _iso(90)}]
    assert G.newer_pipeline(rec, rows)["id"] == "new"
    assert G.newer_pipeline({"id": "new", "branch": "fix/a", "created_at": _iso(10)}, rows) is None


class FakeRedis:
    def __init__(self):
        self.kv, self.lists = {}, {}

    async def get(self, key):
        return self.kv.get(key)

    async def delete(self, key):
        return 1 if self.kv.pop(key, None) is not None else 0

    async def lrange(self, key, a, b):
        return self.lists.get(key, [])[a:b + 1]


def _orphan(pid, branch, m):
    return {"id": pid, "adopted": True, "status": "adopted", "decision": "pending", "branch": branch,
            "gate_passed": None, "current": "critical tier queued behind another gate in this process",
            "to": "bleeding-edge", "created_at": _iso(m),
            "steps": [{"stage": "gate", "ok": True}, {"stage": "critical-queue", "ok": True}]}


def test_resume_skips_an_orphan_whose_branch_was_regated(monkeypatch):
    redis = FakeRedis()
    orphan = _orphan("orph", "fix/url-guard", 120)          # cut off by a restart
    lone = _orphan("lone", "fix/lone", 100)                  # cut off, never re-adopted
    rows = [{"id": "orph", "adopted": True, "gate_passed": None, "branch": "fix/url-guard", "created_at": _iso(120)},
            {"id": "redo", "adopted": True, "gate_passed": True, "branch": "fix/url-guard", "created_at": _iso(60)},
            {"id": "lone", "adopted": True, "gate_passed": None, "branch": "fix/lone", "created_at": _iso(100)}]
    redis.lists[evolve.KEY_PIPELINES] = [json.dumps(x) for x in rows]
    redis.kv[evolve.KEY_PIPELINE + "orph"] = json.dumps(orphan).encode()
    redis.kv[evolve.KEY_PIPELINE + "lone"] = json.dumps(lone).encode()
    ran, saved = [], {}

    async def crit(rec, branch):
        ran.append(rec["id"])
        return True

    async def save(rec):
        saved[rec["id"]] = json.loads(json.dumps(rec))

    async def nothing(*a, **k):
        return None

    async def git(*args, **kw):
        return {"ok": True, "out": "abc a commit\n"}           # both still ahead (not merged)

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
    assert ran == ["lone"]
    assert "superseded by newer pipeline redo (passed)" in saved["orph"]["steps"][-1]["detail"]
    assert not G.interrupted(saved["orph"], NOW)               # not picked up next restart either
