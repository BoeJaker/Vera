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
    assert event["ui"]["session_id"] == "chat-1"
    assert event["extra"]["catalog_recovery"]["attempted"] is False
    assert [item["type"] for item in event["extra"]["lifecycle"]] == [
        "run.started", "run.progress", "run.completed"]
    assert event["extra"]["lifecycle"][1]["payload"]["progress"] == 1.0
    assert event["extra"]["children"][0]["artifacts"][0]["uri"].endswith("/result")
    assert event["extra"]["failure_count"] == 0


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


def test_harness_overlay_renders_run_metadata_and_child_actions():
    from pathlib import Path

    root = Path(__file__).parents[1]
    overlay = (root / "vera" / "activity_overlay.js").read_text(encoding="utf-8")
    harness = (root / "vera" / "capability_orchestration.html").read_text(
        encoding="utf-8")
    assert "run: '◇'" in overlay
    assert "x.trace_id" in overlay
    assert "x.authoritative === false" in overlay
    assert "child.events" in overlay
    assert "Open full Run timeline" in overlay
    assert '/ui/elements/activity_overlay.js' in harness


def test_narrator_probe_kit_observes_runs_without_self_narration():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "vera" / "dream" /
              "dream_capabilities.py").read_text(encoding="utf-8")
    assert '"activity.timeline"' in source
    assert '"run.shadow.graph"' in source
    assert 'args["kinds"] = "run,dream_cycle,dream,loop_live,program,project,goal,artifact"' in source
    assert '_essential += [("activity.timeline", {"scope": "all", "limit": 24})' in source
    assert '("run.shadow.graph", {"limit": 24})' in source


def test_activity_surfaces_current_narrator_intent_as_nonhistorical_metadata():
    from pathlib import Path

    root = Path(__file__).parents[1]
    activity = (root / "vera" / "activity" /
                "activity_capabilities.py").read_text(encoding="utf-8")
    overlay = (root / "vera" / "activity_overlay.js").read_text(encoding="utf-8")
    assert '"vera:system:narrator:intent"' in activity
    assert '"current_intent": current_intent' in activity
    assert "intent.focus" in overlay
    assert "intent.evidence" in overlay
    assert "current as of" in overlay
    timeline = (root / "vera" / "activity_timeline_element.js").read_text(
        encoding="utf-8")
    assert "Narrator context · current intent is not historical" in timeline
    assert "intent.focus" in timeline


def test_activity_run_cards_link_to_chat_and_memory_with_shared_filters():
    from pathlib import Path

    root = Path(__file__).parents[1]
    timeline = (root / "vera" / "activity_timeline_element.js").read_text(
        encoding="utf-8")
    chat = (root / "vera" / "chat" / "chat_panel.html").read_text(encoding="utf-8")
    memory = (root / "vera" / "fabric" / "memory_graph_panel.html").read_text(
        encoding="utf-8")
    assert "/chat_panel?session_id=" in timeline
    assert "/memgraph/panel?run_id=" in timeline
    assert 'id="statusFilter"' in timeline
    assert 'id="windowFilter"' in timeline
    assert "this._windowMinutes * 60000" in timeline
    assert "linkedSession=new URLSearchParams" in chat
    assert "get('run_id')" in memory
    assert "selectNode('run:' + linkedRun)" in memory


def test_run_recovery_ui_is_verified_and_observational_only():
    from pathlib import Path

    root = Path(__file__).parents[1]
    activity = (root / "vera" / "activity" /
                "activity_capabilities.py").read_text(encoding="utf-8")
    timeline = (root / "vera" / "activity_timeline_element.js").read_text(
        encoding="utf-8")
    overlay = (root / "vera" / "activity_overlay.js").read_text(encoding="utf-8")
    assert '"reconciliation": reconciliation' in activity
    assert '"read_only": True' in activity
    assert '"retry_count": len(retry_events)' in activity
    assert "Recovery &amp; reconciliation · observation only" in timeline
    assert "this UI cannot execute or resume the native run" in timeline
    assert "projections recovered" in timeline
    assert "quarantined" in timeline
    assert "durable catalog recovery not enabled for this process" in timeline
    assert "journal verified" in overlay
    assert "catalogRecovery.recovered" in overlay
    assert "catalogRecovery.quarantined" in overlay


def test_artifact_ui_distinguishes_reference_metadata_from_verification():
    from pathlib import Path

    root = Path(__file__).parents[1]
    activity = (root / "vera" / "activity" /
                "activity_capabilities.py").read_text(encoding="utf-8")
    timeline = (root / "vera" / "activity_timeline_element.js").read_text(
        encoding="utf-8")
    overlay = (root / "vera" / "activity_overlay.js").read_text(encoding="utf-8")
    assert '"availability": "unchecked"' in activity
    assert '"content_verified": False' in activity
    assert '"artifact_refs": artifact_refs' in activity
    assert "content not verified" in timeline
    assert "provenance " in timeline
    assert "partial output" in overlay


def test_policy_ui_never_claims_authoritative_control():
    from pathlib import Path

    root = Path(__file__).parents[1]
    activity = (root / "vera" / "activity" /
                "activity_capabilities.py").read_text(encoding="utf-8")
    timeline = (root / "vera" / "activity_timeline_element.js").read_text(
        encoding="utf-8")
    overlay = (root / "vera" / "activity_overlay.js").read_text(encoding="utf-8")
    assert '"authoritative_control_available": False' in activity
    assert '"reason_redacted": True' in activity
    payload_keys = activity.split("_RUN_EVENT_PAYLOAD_KEYS = {", 1)[1].split("}", 1)[0]
    assert '"reason"' not in payload_keys
    assert "Policy &amp; control evidence · read only" in timeline
    assert "No authoritative approve, reject, cancel or retry action" in timeline
    assert "approval pending" in overlay


def test_run_ui_links_projection_identity_to_native_dag_authority():
    from pathlib import Path

    root = Path(__file__).parents[1]
    activity = (root / "vera" / "activity" /
                "activity_capabilities.py").read_text(encoding="utf-8")
    timeline = (root / "vera" / "activity_timeline_element.js").read_text(
        encoding="utf-8")
    overlay = (root / "vera" / "activity_overlay.js").read_text(encoding="utf-8")
    assert '"authority": "native_dag"' in activity
    assert '"projection": "run_protocol_shadow"' in activity
    assert '"native_url": "/workshop/panel"' in activity
    assert "Execution and control remain authoritative there" in timeline
    assert "Native DAG workshop" in timeline
    assert "Open native DAG workshop" in overlay


def test_telemetry_ui_reports_local_correlation_without_claiming_export():
    from pathlib import Path

    root = Path(__file__).parents[1]
    activity = (root / "vera" / "activity" /
                "activity_capabilities.py").read_text(encoding="utf-8")
    timeline = (root / "vera" / "activity_timeline_element.js").read_text(
        encoding="utf-8")
    overlay = (root / "vera" / "activity_overlay.js").read_text(encoding="utf-8")
    assert '"exporter": export_state["state"]' in activity
    assert '"exported": export_state["last_status"] == "accepted"' in activity
    assert '"content_redacted": True' in activity
    assert "Portable telemetry readiness" in timeline
    assert "export_accepted" in timeline
    assert "export_failed" in timeline
    assert "not exported" in overlay


def test_activity_surfaces_are_keyboard_and_narrow_screen_accessible():
    from pathlib import Path

    root = Path(__file__).parents[1]
    timeline = (root / "vera" / "activity_timeline_element.js").read_text(
        encoding="utf-8")
    overlay = (root / "vera" / "activity_overlay.js").read_text(encoding="utf-8")
    assert "@media (max-width:700px)" in timeline
    assert "@media (prefers-reduced-motion:reduce)" in timeline
    assert 'role="feed"' in timeline
    assert 'role="status" aria-live="polite"' in timeline
    assert 'aria-pressed="' in timeline
    assert "@media (max-width:640px)" in overlay
    assert "aria-controls" in overlay
    assert "aria-hidden" in overlay
    assert "ev.currentTarget.click()" in overlay


def test_run_activity_evidence_omits_content_and_error_messages():
    from Vera.vera.activity import activity_capabilities as activity

    evidence = activity._run_evidence({
        "id": "child", "kind": "vera.dag.node", "status": "failed",
        "events": [{"id": "ev", "sequence": 1, "type": "run.failed",
                    "status": "failed", "occurred_at": "now",
                    "payload": {"capability": "code.author", "result": "SECRET",
                                "prompt": "PRIVATE", "error_type": "ValueError"}}],
        "artifacts": [{"id": "a", "kind": "partial", "uri": "run://child/result",
                       "checksum": "sha256:abc", "inline_data": "SECRET"}],
        "error": {"code": "failed", "message": "SECRET", "retryable": False,
                  "details": {"prompt": "PRIVATE"}},
    })

    rendered = repr(evidence)
    assert "SECRET" not in rendered
    assert "PRIVATE" not in rendered
    assert evidence["events"][0]["payload"] == {
        "capability": "code.author", "error_type": "ValueError"}
    assert evidence["error"] == {"code": "failed", "retryable": False}


def test_run_graph_nodes_offer_bidirectional_evidence_navigation():
    from pathlib import Path

    root = Path(__file__).parents[1]
    activity = (root / "vera" / "activity_timeline_element.js").read_text()
    chat = (root / "vera" / "chat" / "chat_panel.html").read_text()
    memory = (root / "vera" / "fabric" / "memory_graph_panel.html").read_text()
    assert "Execution evidence" in activity
    assert "child.events" in activity
    assert "Activity evidence" in chat
    assert "openRelatedUi('/activity/panel#" in memory
    assert "r.non_authoritative ? ''" in memory


def test_activity_run_waterfall_is_measured_and_labels_unknown_time():
    from pathlib import Path

    timeline = (Path(__file__).parents[1] / "vera" /
                "activity_timeline_element.js").read_text(encoding="utf-8")
    assert "function runWaterfall(extra)" in timeline
    assert "Observed performance waterfall" in timeline
    assert "Unattributed orchestration gap" in timeline
    assert "wf.unattributed" in timeline
    assert "span.child.attempt > 1" in timeline
    assert "['failed','timed_out','cancelled']" in timeline
    # Do not manufacture provider/queue/post-processing attribution from the
    # coarse Run lifecycle timestamps currently available.
    assert "provider gap" not in timeline.lower()
    assert "post-processing gap" not in timeline.lower()


def test_failure_triage_includes_failed_children_of_completed_parents(monkeypatch):
    from Vera.vera.activity import activity_capabilities as activity
    from Vera.vera.execution import run_projection

    registry = ShadowRunRegistry()
    parent = Run(id="completed-parent", kind="vera.dag", session_id="chat-1")
    registry.record(parent, parent.transition(RunStatus.RUNNING, event_type="run.started"))
    observer = DagRunObserver(parent=parent, graph=[["broken.cap", "out"]],
                              emit=_ignore, registry=registry)
    asyncio.run(observer.node_started((0,), "broken.cap"))
    asyncio.run(observer.node_finished((0,), "broken.cap", None, "private error"))
    registry.record(parent, parent.transition(RunStatus.COMPLETED,
                                              event_type="run.completed"))
    monkeypatch.setattr(run_projection, "SHADOW_RUNS", registry)

    event = asyncio.run(activity._run_events())[0]
    assert event["status"] == "completed"
    assert event["extra"]["failure_count"] == 1
    assert event["extra"]["failed_capabilities"] == ["broken.cap"]
    assert "private error" not in repr(event)


def test_failure_triage_ui_is_read_only_and_can_inspect_child():
    from pathlib import Path

    timeline = (Path(__file__).parents[1] / "vera" /
                "activity_timeline_element.js").read_text(encoding="utf-8")
    assert "this._failureOnly" in timeline
    assert "failed_capabilities" in timeline
    assert "read-only triage · no automatic retry" in timeline
    assert "including completed parents" in timeline
    assert "data-open=\"' + childLink" in timeline
