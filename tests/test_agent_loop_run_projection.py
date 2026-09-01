import json

import pytest

from vera.execution.agent_loop_run_projection import (
    _ACTIVE,
    AgentLoopRunProjection,
    bind_agent_loop_projection,
    finish_agent_loop_projection,
    observe_agent_loop_event,
    reset_agent_loop_projection,
    start_agent_loop_projection,
)
from vera.execution.run_journal import MemoryRunJournal
from vera.execution.run_projection import ShadowRunRegistry
from vera.execution.run_protocol import RunStatus


pytestmark = pytest.mark.critical


def _registry():
    return ShadowRunRegistry(max_runs=30, journal=MemoryRunJournal())


def test_agent_loop_and_tools_project_to_parent_child_runs_without_content():
    registry = _registry()
    projection = AgentLoopRunProjection(
        session_id="session-a", engine="v7", profile="coding", registry=registry)
    projection.observe({
        "type": "agent_loop_v6.toolkit", "session_id": "session-a",
        "toolkit": ["code.author", "ide.fs.read"], "goal": "PRIVATE GOAL"})
    projection.observe({
        "type": "agent_loop_v5.tool_call", "session_id": "session-a",
        "step_id": 2, "cycle": 4, "tool": "code.author",
        "args": {"token": "PRIVATE TOKEN"}, "thought": "PRIVATE THOUGHT"})
    projection.observe({
        "type": "agent_loop_v5.tool_done", "session_id": "session-a",
        "step_id": 2, "cycle": 4, "tool": "code.author", "ok": True,
        "elapsed_ms": 123, "preview": "PRIVATE RESULT"})
    projection.finish()

    root = registry.get(projection.run_id)
    assert root["run"]["kind"] == "vera.agent_loop"
    assert root["run"]["status"] == "completed"
    assert len(root["children"]) == 1
    child = root["children"][0]
    assert child["kind"] == "vera.agent_loop.tool"
    assert child["status"] == "completed"
    assert child["task_id"] == "step:2"
    assert child["events"][-1]["payload"] == {
        "capability": "code.author", "step_id": 2, "cycle": 4,
        "elapsed_ms": 123, "ok": True}
    encoded = json.dumps(registry.journal.export(projection.run_id), sort_keys=True)
    for secret in ("PRIVATE GOAL", "PRIVATE TOKEN", "PRIVATE THOUGHT", "PRIVATE RESULT"):
        assert secret not in encoded


def test_failure_approval_and_parent_terminal_cleanup_are_explicit():
    registry = _registry()
    projection = AgentLoopRunProjection(session_id="session-b", registry=registry)
    projection.observe({"type": "agent_loop_v3.hitl_request"})
    assert projection.parent.status == RunStatus.APPROVAL_PENDING
    projection.observe({"type": "agent_loop_v3.hitl_resolved"})
    assert projection.parent.status == RunStatus.RUNNING
    projection.observe({"type": "agent_loop_v5.tool_call", "step_id": 1,
                        "cycle": 1, "tool": "web.fetch", "args": {"url": "secret"}})
    projection.observe({"type": "agent_loop_v5.tool_done", "step_id": 1,
                        "cycle": 1, "tool": "web.fetch", "ok": False,
                        "error": "PRIVATE ERROR"})
    projection.observe({"type": "agent_loop_v5.tool_call", "step_id": 2,
                        "cycle": 1, "tool": "prose.author"})
    projection.finish(error_type="RuntimeError")

    root = registry.get(projection.run_id)
    assert root["run"]["status"] == "failed"
    assert root["run"]["error"]["code"] == "RuntimeError"
    assert sorted(child["status"] for child in root["children"]) == ["cancelled", "failed"]
    encoded = json.dumps(root, sort_keys=True)
    assert "PRIVATE ERROR" not in encoded
    assert '"url": "secret"' not in encoded
    assert "parent_terminal_without_tool_done" in encoded


def test_duplicate_calls_are_correlated_fifo_and_unmatched_done_is_ignored():
    registry = _registry()
    projection = AgentLoopRunProjection(session_id="session-c", registry=registry)
    call = {"type": "agent_loop_v5.tool_call", "step_id": 1,
            "cycle": 2, "tool": "ide.fs.read"}
    done = {"type": "agent_loop_v5.tool_done", "step_id": 1,
            "cycle": 2, "tool": "ide.fs.read", "ok": True}
    projection.observe(call); projection.observe(call)
    projection.observe(done); projection.observe(done); projection.observe(done)
    projection.finish()
    children = registry.get(projection.run_id)["children"]
    assert len(children) == 2
    assert all(child["status"] == "completed" for child in children)


def test_global_observer_is_session_scoped_immutable_and_supersedes_safely():
    registry = _registry()
    sid = "projection-supersession-test"
    _ACTIVE.pop(sid, None)
    first = start_agent_loop_projection(session_id=sid, engine="v6", registry=registry)
    second = start_agent_loop_projection(session_id=sid, engine="v7", registry=registry)
    assert first.parent.status == RunStatus.CANCELLED
    native = {"type": "agent_loop_v5.tool_call", "session_id": sid,
              "step_id": 3, "cycle": 1, "tool": "system.ping",
              "args": {"secret": "unchanged"}}
    before = json.loads(json.dumps(native))
    observe_agent_loop_event(native)
    observe_agent_loop_event({**native, "session_id": "another-session"})
    assert native == before
    assert len(second.children) == 1
    finish_agent_loop_projection(second, result={"ok": True, "final": "PRIVATE"})
    assert second.parent.status == RunStatus.COMPLETED
    assert sid not in _ACTIVE


def test_context_identity_prevents_superseded_session_event_contamination():
    registry = _registry()
    sid = "projection-context-test"
    _ACTIVE.pop(sid, None)
    first = start_agent_loop_projection(session_id=sid, engine="v6", registry=registry)
    token = bind_agent_loop_projection(first)
    try:
        second = start_agent_loop_projection(session_id=sid, engine="v7", registry=registry)
        observe_agent_loop_event({"type": "agent_loop_v5.tool_call",
                                  "session_id": sid, "tool": "old.tool"})
        assert not first.children
        assert not second.children
    finally:
        reset_agent_loop_projection(token)
        finish_agent_loop_projection(second)


def test_projection_failures_never_escape_to_native_event_transport():
    class BrokenRegistry:
        def record(self, run, event):
            raise RuntimeError("registry unavailable")

    projection = AgentLoopRunProjection(session_id="broken-registry", registry=BrokenRegistry())
    projection.observe({"type": "agent_loop_v5.tool_call", "tool": "system.ping"})
    projection.observe({"type": "agent_loop_v5.tool_done", "tool": "system.ping",
                        "ok": True})
    projection.finish()


def test_only_content_free_native_fields_are_named_by_the_projection_source():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "vera" / "execution" /
              "agent_loop_run_projection.py").read_text(encoding="utf-8")
    for forbidden in ('event.get("goal")', 'event.get("args")',
                      'event.get("preview")', 'event.get("thought")',
                      'event.get("result")'):
        assert forbidden not in source
