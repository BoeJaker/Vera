"""The release hold sees loops started through /mcp/call too (4 Oct 2026: a probe loop
started that way had no runner task, so the hold, which read only _AGENT_LOOP_TASKS,
could not see it)."""
import asyncio
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REL = (ROOT / "vera" / "evolve" / "release_capabilities.py").read_text(encoding="utf-8")

try:
    from vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None
needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")


class FakeRedis:
    def __init__(self, runs, index):
        self.runs, self.index = runs, index

    async def zrevrange(self, key, a, b):
        return [s.encode() for s in self.index] if key == "vera:loop:history:index" else []

    async def hgetall(self, key):
        sid = key.rsplit(":", 1)[-1]
        return {k.encode(): v.encode() for k, v in self.runs.get(sid, {}).items()}

    async def zscore(self, key, sid):
        return None


@needs_app
def test_live_loop_session_ids_are_running_and_not_stale(monkeypatch):
    runs = {"live1": {"status": "running"}, "stale1": {"status": "running"},
            "done1": {"status": "done"}, "gone1": {}}
    r = FakeRedis(runs, ["live1", "stale1", "done1", "gone1"])

    async def stale(_r, sid, run):
        return sid == "stale1"

    monkeypatch.setattr(M, "_redis", lambda: r)
    monkeypatch.setattr(M, "_loop_run_is_stale", stale)
    assert asyncio.run(M._live_loop_session_ids()) == ["live1"]
    monkeypatch.setattr(M, "_redis", lambda: None)
    assert asyncio.run(M._live_loop_session_ids()) == []


def test_census_summary_adds_mcp_started_runs():
    body = REL[REL.index("async def census_summary()"):]
    body = body[:body.index("\n\n\nasync def ")]
    assert 'ids = list(core.live_loop_ids(getattr(dm, "_AGENT_LOOP_TASKS", None) or {}))' in body
    assert '_live = getattr(dm, "_live_loop_session_ids", None)' in body
    assert "for sid in await _live():" in body
    assert 's["loops"] = ids' in body


@needs_app
def test_census_summary_reports_both_kinds(monkeypatch):
    from vera.evolve import release_capabilities as RC

    class T:
        def done(self):
            return False

    async def live():
        return ["mcp-run-1", "ui-run-1"]

    fake = types.SimpleNamespace(_AGENT_LOOP_TASKS={"ui-run-1": T()}, _live_loop_session_ids=live)
    monkeypatch.setitem(sys.modules, "dag_workshop_capabilities", fake)
    monkeypatch.delitem(sys.modules, "census_capabilities", raising=False)
    monkeypatch.delitem(sys.modules, "schedule_capabilities", raising=False)
    monkeypatch.delitem(sys.modules, "Vera.vera.evolve.schedule_capabilities", raising=False)
    s = asyncio.run(RC.census_summary())
    assert s["loops"] == ["ui-run-1", "mcp-run-1"]
