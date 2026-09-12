"""Mission control's cap must load with the app and read its stores in one
call. Runs in-container (Vera.vera resolves to THIS checkout); skips on a
host venv. Every reader is stubbed - no Redis needed.
"""
import asyncio
import os

import pytest

try:
    from Vera.vera import capability_orchestration as ORCH
    from Vera.vera.evolve import evolve_capabilities as EV
    from Vera.vera.evolve import task_history_capabilities as TH
    from Vera.vera.evolve import mission_capabilities as MI
except Exception:                                    # pragma: no cover
    ORCH = EV = TH = MI = None

_HERE_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
_SAME_TREE = bool(MI is not None and
                  os.path.realpath(getattr(MI, "__file__", "")).startswith(_HERE_ROOT))
pytestmark = pytest.mark.skipif(not _SAME_TREE, reason="app module not importable from THIS checkout here")


def run(coro):
    return asyncio.run(coro)


def test_the_cap_is_registered():
    assert "evolve.mission.events" in ORCH.CAPABILITY_REGISTRY


def _stub(monkeypatch, calls):
    import sys
    monkeypatch.setitem(sys.modules, "evolve_capabilities", EV)
    monkeypatch.setitem(sys.modules, "task_history_capabilities", TH)

    def stub(mod, name, value):
        async def _f(*a, **kw):
            calls.append((name, kw))
            return value
        monkeypatch.setattr(mod, name, _f)
    stub(EV, "evolve_audit_list", {"audit": [
        {"ts": "2026-09-10T10:00:00Z", "action": "pipeline.promote", "summary": "MERGED x", "ok": True, "id": "p1",
         "kind": "code", "branch": "feat/x", "by": {"host": "LLM"}},
        {"ts": "2026-09-10T09:00:00Z", "action": "sandbox.exec", "summary": "ls", "ok": True, "by": {"host": "LLM"}}]})
    stub(EV, "evolve_errors_list", {"items": [{"id": "e1", "state": "new", "source": "sandbox", "title": "boom", "count": 1,
                                                "last_seen": "2026-09-10T08:00:00Z"}], "counts": {"new": 1}})
    stub(EV, "evolve_unittest_history", {"runs": [{"ts": "2026-09-10T07:00:00Z", "branch": "feat/x", "pipeline_id": "p1",
                                                    "ok": False, "passed": 10, "failed": 1, "total": 11, "summary": "10 passed, 1 failed"}]})
    stub(EV, "evolve_pipeline_list", {"pipelines": [{"id": "p1", "decision": "pending", "gate_passed": True, "live": False},
                                                     {"id": "p2", "decision": "pending", "gate_passed": None, "live": True}]})

    async def _call(name, **kw):
        calls.append((name, kw))
        return {"items": [{"lane": "in_progress"}, {"lane": "done"}]} if name == "board.items" else {}
    monkeypatch.setattr(EV, "_call", _call)
    stub(EV, "autonomous_status", {"ok": True, "engaged": True, "reason": "closed loop", "since": "2026-09-10T00:00:00Z"})
    stub(TH, "cap_evolve_work_live", {"census": {}, "suite": {}, "improve": None, "run": {}, "any_live": False})
    stub(EV, "evolve_improve_list", {"sessions": [{"id": "s1", "status": "running", "profile": "planning", "phase": "evaluate",
                                                   "rounds_done": 1, "max_rounds": 4},
                                                  {"id": "s0", "status": "done", "rounds_done": 4, "max_rounds": 4}]})
    stub(EV, "evolve_editq_list", {"queue": [{"id": "a1", "status": "queued", "model": "gpt-oss:20b"},
                                             {"id": "a2", "status": "running", "model": "gpt-oss:20b", "instance": "cpu"},
                                             {"id": "a0", "status": "done"}]})
    stub(EV, "evolve_activity", {"buckets": [{"hour": "2026-09-10T09", "pass": 2, "fail": 1, "edits": 0}]})


def test_the_table_and_the_strip_come_from_one_call(monkeypatch):
    calls = []
    _stub(monkeypatch, calls)
    out = run(MI.cap_evolve_mission_events())
    assert {"events", "count", "total", "summary", "families", "live", "autonomous", "counts", "any_live", "fleet", "activity"} <= set(out)
    # the page's infographics ride the same call: the fleet and the activity chart
    assert [l["id"] for l in out["fleet"]["loops"]] == ["s1"] and out["fleet"]["loops"][0]["pct"] == 25
    assert [e["id"] for e in out["fleet"]["editors"]] == ["a2", "a1"], "running first, done not at all"
    assert out["fleet"]["count"] == 3 and out["activity"][0]["pass"] == 2
    assert [e["kind"] for e in out["events"]] == ["error", "action", "action", "gate"], "open errors first, then newest"
    assert out["total"] == 4 and out["summary"]["problems"] == 2
    assert out["counts"]["needs_promotion"] == 1 and out["counts"]["live_pipelines"] == 1 and out["counts"]["active_items"] == 1
    assert out["counts"]["errors"] == {"new": 1} and out["counts"]["last_gate"]["ok"] is False
    assert out["autonomous"]["engaged"] is True and out["live"]["any_live"] is False
    assert out["any_live"] is True, "a live pipeline is live"
    assert out["families"] == ["errors", "pipeline", "sandbox", "unittest"]
    names = [c[0] for c in calls]
    assert names.count("board.items") == 1 and names.count("cap_evolve_work_live") == 1 and len(names) == 10
    calls.clear()
    lean = run(MI.cap_evolve_mission_events(kind="action", hide_exec="true", limit=5))
    assert [e["action"] for e in lean["events"]] == ["pipeline.promote"] and lean["count"] == 1 and lean["total"] == 4


def test_a_failing_reader_costs_its_rows_not_the_page(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "evolve_capabilities", EV)
    monkeypatch.setitem(sys.modules, "task_history_capabilities", None)

    async def boom(*a, **kw):
        raise RuntimeError("redis is away")

    async def ok(*a, **kw):
        return {"audit": [{"ts": "2026-09-10T10:00:00Z", "action": "config.set", "summary": "x", "ok": True}]}

    async def empty(*a, **kw):
        return {}
    monkeypatch.setattr(EV, "evolve_audit_list", ok)
    monkeypatch.setattr(EV, "evolve_errors_list", boom)
    monkeypatch.setattr(EV, "evolve_unittest_history", boom)
    monkeypatch.setattr(EV, "evolve_pipeline_list", empty)
    monkeypatch.setattr(EV, "_call", empty)
    monkeypatch.setattr(EV, "autonomous_status", boom)
    monkeypatch.setattr(EV, "evolve_improve_list", boom)
    monkeypatch.setattr(EV, "evolve_editq_list", empty)
    monkeypatch.setattr(EV, "evolve_activity", boom)
    out = run(MI.cap_evolve_mission_events())
    assert [e["action"] for e in out["events"]] == ["config.set"]
    assert out["live"] == {} and out["autonomous"] == {} and out["counts"]["errors"] == {} and out["any_live"] is False
    assert out["fleet"] == {"loops": [], "editors": [], "count": 0} and out["activity"] == []
