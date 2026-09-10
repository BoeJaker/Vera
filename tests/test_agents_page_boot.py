"""The Agents page's cap must load with the app and read the five stores
through the registry and the evolve module's own readers.

Runs in-container, where Vera.vera resolves to THIS checkout; skips on a
host venv. No Redis is needed: every reader is stubbed.
"""
import asyncio
import os

import pytest

try:
    from Vera.vera import capability_orchestration as ORCH
    from Vera.vera.evolve import evolve_capabilities as EV
    from Vera.vera.evolve import agents_capabilities as AG
except Exception:                                    # pragma: no cover
    ORCH = EV = AG = None

_HERE_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
_SAME_TREE = bool(AG is not None and
                  os.path.realpath(getattr(AG, "__file__", "")).startswith(_HERE_ROOT))
pytestmark = pytest.mark.skipif(not _SAME_TREE, reason="app module not importable from THIS checkout here")


def run(coro):
    return asyncio.run(coro)


def test_the_cap_is_registered():
    assert "evolve.agents.rows" in ORCH.CAPABILITY_REGISTRY


def _stub_all(monkeypatch, calls):
    import sys
    monkeypatch.setitem(sys.modules, "evolve_capabilities", EV)

    async def _call(name, **kw):
        calls.append((name, kw))
        return {
            "ide.claude_sessions.watch": {"ok": True, "sessions": [
                {"claude_session_id": "s-live", "title": "Live one", "state": "live", "age_s": 30, "turns": 3,
                 "last_ts": "2026-09-10T12:00:00Z", "claims": [], "pipelines": []},
                {"claude_session_id": "s-idle", "title": "Old one", "state": "untracked", "age_s": 99999, "turns": 1,
                 "last_ts": "2026-09-01T12:00:00Z", "claims": [], "pipelines": []}],
                "summary": {"total": 2, "by_state": {"live": 1, "untracked": 1}},
                "policy": {"stalled_after_s": 2700}},
            "board.items": {"ok": True, "provider": "file", "items": [
                {"id": "it-1", "title": "Do x", "lane": "in_progress", "agent": "codex", "session": "codex-run",
                 "branch": "feat/x", "labels": [], "updated_at": "2026-09-10T11:00:00Z", "comment_count": 2, "plan": "p"},
                {"id": "it-2", "title": "Did y", "lane": "done", "agent": "claude", "session": "s-idle",
                 "branch": "", "labels": ["ui"], "updated_at": "2026-09-02T11:00:00Z", "comment_count": 0, "plan": "p"}]},
            "capacity.status": {"ok": True, "seats": [], "ollama": {"enabled": True, "gpu_cap": 1},
                                "summary": {"seats": 0, "available": 0, "cooling": 0}},
        }.get(name, {})
    monkeypatch.setattr(EV, "_call", _call)

    def stub(name, value):
        async def _f(*a, **kw):
            calls.append((name, kw))
            return value
        monkeypatch.setattr(EV, name, _f)
    stub("evolve_pipeline_list", {"pipelines": [{"id": "p-1", "branch": "feat/x", "decision": "pending", "live": True,
                                                 "created_at": "2026-09-10T11:30:00Z", "session_id": "codex-run",
                                                 "controller": "codex"}]})
    stub("evolve_sandbox_list", {"sandboxes": [{"name": "vera-dev-feat-x", "branch": "feat/x", "running": True,
                                                "session_id": "codex-run", "owner": "codex", "last_activity": "2026-09-10T11:40:00Z"}]})
    stub("evolve_improve_list", {"sessions": [{"id": "imp-1", "status": "running", "profile": "planning", "rounds_done": 1,
                                               "max_rounds": 3, "started_at": "2026-09-10T11:50:00Z"}]})
    stub("evolve_editq_list", {"queue": []})


def test_the_table_reads_the_five_stores_in_one_call(monkeypatch):
    calls = []
    _stub_all(monkeypatch, calls)
    out = run(AG.cap_evolve_agents_rows())
    assert out["mode"] == "sessions"
    assert {"sessions", "count", "total", "summary", "swarm", "capacity", "watch", "any_live", "lanes", "agents",
            "plans", "repos", "projects", "board_provider"} <= set(out)
    ids = [s["id"] for s in out["sessions"]]
    assert ids == ["s-live", "imp-1", "codex-run", "s-idle"], "the live band, newest activity first; idle last"
    assert out["total"] == 4
    codex = next(s for s in out["sessions"] if s["id"] == "codex-run")
    assert codex["agent"] == "codex" and codex["state"] == "live" and codex["items_n"] == 1 and codex["pipelines_n"] == 1
    assert codex["sandboxes"][0]["name"] == "vera-dev-feat-x" and codex["title"] == "Do x"
    assert out["any_live"] is True
    assert out["swarm"]["loops"] == 1 and out["swarm"]["pipelines_live"] == 1 and out["swarm"]["dispatched"] == 1
    assert out["swarm"]["containers"] == 1 and out["swarm"]["sessions"] == 3
    assert out["capacity"]["ollama"]["gpu_cap"] == 1 and out["watch"]["policy"]["stalled_after_s"] == 2700
    assert out["lanes"] == ["done", "in_progress"] and out["plans"] == ["p"] and out["board_provider"] == "file"
    assert dict(calls)["ide.claude_sessions.watch"] == {"max_sessions": AG.WATCH_N}
    assert dict(calls)["evolve_sandbox_list"] == {"detail": False}, "no git probes for a poll"

    live = next(s for s in out["sessions"] if s["id"] == "s-live")
    assert live["state"] == "live"
    calls.clear()
    both = run(AG.cap_evolve_agents_rows(mode="both"))
    assert both["mode"] == "both" and len(both["sessions"]) == 4 and both["items_total"] == 2 and both["total"] == 4
    assert all(s["items"] == [] for s in both["sessions"]), "the items ride once, in `items`; the page joins them"
    assert next(s for s in both["sessions"] if s["id"] == "codex-run")["items_n"] == 1, "the counts stay"
    assert len(calls) == 7, "one read of each store for the page's one poll"
    calls.clear()
    items = run(AG.cap_evolve_agents_rows(mode="items", hide_done="true"))
    assert items["mode"] == "items" and [i["id"] for i in items["items"]] == ["it-1"] and items["total"] == 2
    assert items["items"][0]["session_state"] == "live", "an item names its session's state"
    lean = run(AG.cap_evolve_agents_rows(state="idle", limit=1))
    assert [s["id"] for s in lean["sessions"]] == ["s-idle"] and lean["count"] == 1


def test_a_failing_reader_costs_its_column_not_the_page(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "evolve_capabilities", EV)

    async def _call(name, **kw):
        if name == "board.items":
            return {"items": [{"id": "i", "title": "t", "lane": "ready", "session": "s", "agent": "claude",
                               "updated_at": "2026-09-10T00:00:00Z"}]}
        return {"error": "gone"}

    async def boom(*a, **kw):
        raise RuntimeError("docker is away")

    async def empty(*a, **kw):
        return {}
    monkeypatch.setattr(EV, "_call", _call)
    monkeypatch.setattr(EV, "evolve_pipeline_list", empty)
    monkeypatch.setattr(EV, "evolve_sandbox_list", boom)
    monkeypatch.setattr(EV, "evolve_improve_list", empty)
    monkeypatch.setattr(EV, "evolve_editq_list", boom)
    out = run(AG.cap_evolve_agents_rows())
    assert [s["id"] for s in out["sessions"]] == ["s"] and out["sessions"][0]["state"] == "working"
    assert out["capacity"] == {"seats": [], "ollama": {}, "summary": {}} and out["watch"]["policy"] == {}
