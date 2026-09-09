import asyncio

import pytest

from vera.execution.run_projection import ShadowRunRegistry
from vera.operator import operator_run_projection as projection


pytestmark = pytest.mark.critical


def _registry():
    return ShadowRunRegistry(max_runs=20)


def test_operator_projection_is_content_free_and_binds_screenshot_artifact():
    registry = _registry()
    observer = projection.OperatorRunProjection(
        "op-run-1", session_id="session-1", target="panel", registry=registry)
    observer.step({
        "i": 1, "phase": "act", "action": "click",
        "args": {"text": "private input"}, "thought": "private reasoning",
        "seen": "private page content",
        "screenshot": "/operator/artifact?path=operator/session-1/shot.png",
    })
    observer.finish("done")

    value = observer.to_dict()
    assert value["execution_authority"] == "native_operator"
    assert value["run"]["status"] == "completed"
    assert value["run"]["session_id"] == "session-1"
    assert value["run"]["progress"] == 1.0
    child = value["children"][0]
    assert child["status"] == "completed"
    assert child["artifacts"][0]["uri"].startswith("/operator/artifact?path=")
    serialized = str(value)
    assert "private input" not in serialized
    assert "private reasoning" not in serialized
    assert "private page content" not in serialized


@pytest.mark.parametrize(("reason", "status"), [
    ("cancelled", "cancelled"),
    ("time_budget", "timed_out"),
    ("max_steps", "failed"),
    ("blocked", "failed"),
])
def test_native_stop_reason_maps_to_honest_terminal_status(reason, status):
    observer = projection.OperatorRunProjection("op-run-stop", registry=_registry())
    observer.finish(reason)

    assert observer.to_dict()["run"]["status"] == status


def test_failed_step_is_child_failure_without_stopping_native_projection():
    observer = projection.OperatorRunProjection("op-run-step", registry=_registry())
    observer.step({"i": 1, "phase": "blocked", "action": "click"})
    observer.finish("blocked")

    value = observer.to_dict()
    assert value["children"][0]["status"] == "failed"
    assert value["children"][0]["error"]["code"] == "operator_step_failed"
    assert value["run"]["status"] == "failed"


def test_observe_bridges_legacy_event_sequence_and_drops_active_state():
    registry = _registry()
    projection.drop("op-run-events")
    projection.observe("op-run-events", {
        "type": "operator.run", "stage": "start", "session_id": "session-2",
        "target": "url", "goal": "must not be projected",
    }, registry=registry)
    projection.observe("op-run-events", {
        "type": "operator.step", "i": 1, "phase": "done", "action": "done",
    })
    projection.observe("op-run-events", {
        "type": "operator.run", "stage": "done", "reason": "done",
    })

    assert registry.get("op-run-events")["run"]["status"] == "completed"
    assert "op-run-events" not in projection._ACTIVE


def test_legacy_recorder_treats_projection_as_non_authoritative(monkeypatch):
    from vera.operator import operator_web_capabilities as caps

    calls = []
    monkeypatch.setattr(caps, "emit_event", lambda event: asyncio.sleep(0))
    monkeypatch.setattr(caps._orch, "REDIS", None)

    def broken(run_id, event):
        calls.append((run_id, event["type"]))
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr(caps._run_projection, "observe", broken)
    asyncio.run(caps._op_record("op-run-safe", {"type": "operator.run", "stage": "start"}))

    assert calls == [("op-run-safe", "operator.run")]


def test_invalid_or_duplicate_step_identity_fails_closed():
    observer = projection.OperatorRunProjection("op-run-invalid", registry=_registry())
    with pytest.raises(ValueError, match="positive integer"):
        observer.step({"i": "bad", "phase": "act"})
    observer.step({"i": 1, "phase": "act"})
    with pytest.raises(ValueError, match="already projected"):
        observer.step({"i": 1, "phase": "act"})
