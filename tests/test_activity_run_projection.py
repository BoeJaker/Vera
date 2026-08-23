import asyncio

import pytest

from vera.execution.run_projection import DagRunObserver, ShadowRunRegistry
from vera.execution.run_protocol import Run, RunStatus


pytestmark = pytest.mark.critical


async def _ignore(_event):
    return None


def _registry_with_completed_dag():
    registry = ShadowRunRegistry()
    parent = Run(id="run-1", kind="vera.dag", trace_id="chat-1",
                 session_id="chat-1", workflow_id="chat-1")
    registry.record(parent, parent.transition(RunStatus.RUNNING,
                                              event_type="run.started"))
    observer = DagRunObserver(parent=parent, graph=[["example.cap", "answer"]],
                              emit=_ignore, registry=registry)
    asyncio.run(observer.node_started((0,), "example.cap"))
    asyncio.run(observer.node_finished((0,), "example.cap", {"answer": 42}))
    registry.record(parent, parent.transition(RunStatus.COMPLETED,
                                              event_type="run.completed"))
    return registry


def test_activity_projects_root_run_with_inline_child_feedback(monkeypatch):
    from Vera.vera.activity import activity_capabilities as activity
    from Vera.vera.execution import run_projection

    monkeypatch.setattr(run_projection, "SHADOW_RUNS", _registry_with_completed_dag())
    events = asyncio.run(activity._run_events())

    assert len(events) == 1
    event = events[0]
    assert event["kind"] == "run"
    assert event["status"] == "completed"
    assert event["ref"] == "run-1"
    assert event["summary"] == "1/1 child nodes finished"
    assert event["extra"]["authoritative"] is False
    assert event["extra"]["storage"] == "process_local_memory"
    assert event["extra"]["children"][0]["status"] == "completed"


def test_activity_run_and_chat_scopes_filter_run_projections(monkeypatch):
    from Vera.vera.activity import activity_capabilities as activity
    from Vera.vera.execution import run_projection

    monkeypatch.setattr(run_projection, "SHADOW_RUNS", _registry_with_completed_dag())
    by_run = asyncio.run(activity.cap_activity_timeline.__wrapped__(scope="run:run-1"))
    by_chat = asyncio.run(activity.cap_activity_timeline.__wrapped__(scope="chat:chat-1"))
    missing = asyncio.run(activity.cap_activity_timeline.__wrapped__(scope="run:missing"))

    assert [event["ref"] for event in by_run["events"]] == ["run-1"]
    assert any(event["ref"] == "run-1" for event in by_chat["events"])
    assert missing["events"] == []


def test_activity_timeline_script_renders_run_children_and_ephemeral_warning():
    from pathlib import Path

    script = (Path(__file__).parents[1] / "vera" / "activity_timeline_element.js").read_text()
    assert "child node" in script
    assert "non-authoritative" in script
    assert "ephemeral view" in script
    assert "data-scope=\"run:" in script
