import pytest

from vera.execution.workflow_schedule_lifecycle import (
    build_schedule_lifecycle,
    calendar_action_lifecycle,
    dream_trigger_lifecycle,
    transition_schedule_lifecycle,
    validate_schedule_lifecycle_event,
    validate_schedule_lifecycle,
)


pytestmark = pytest.mark.critical


def _active(source_kind="dream.trigger"):
    return build_schedule_lifecycle(
        source_kind=source_kind, source_id="briefing",
        definition_revision="sha256:definition", state="active",
        native_owner="vera.dream.scheduler",
    )


def test_pause_resume_and_cancel_are_closed_non_executing_transitions():
    paused = transition_schedule_lifecycle(
        _active(), requested_state="paused",
        observed_at="2026-08-31T12:00:00Z")
    resumed = transition_schedule_lifecycle(
        paused["lifecycle"], requested_state="active",
        observed_at="2026-08-31T12:01:00Z")
    cancelled = transition_schedule_lifecycle(
        resumed["lifecycle"], requested_state="cancelled",
        observed_at="2026-08-31T12:02:00Z")

    assert paused["transition"] == "active_to_paused"
    assert resumed["transition"] == "paused_to_active"
    assert cancelled["transition"] == "active_to_cancelled"
    assert cancelled["preserves_record"] is True
    assert cancelled["executes"] is False


def test_cancelled_is_terminal_but_idempotent_cancel_is_allowed():
    cancelled = transition_schedule_lifecycle(
        _active(), requested_state="cancelled",
        observed_at="2026-08-31T12:00:00Z")
    repeated = transition_schedule_lifecycle(
        cancelled["lifecycle"], requested_state="cancelled",
        observed_at="2026-08-31T12:01:00Z")

    assert repeated["transition"] == "no_change"
    with pytest.raises(ValueError, match="cannot transition"):
        transition_schedule_lifecycle(
            cancelled["lifecycle"], requested_state="active",
            observed_at="2026-08-31T12:02:00Z")


def test_lifecycle_validation_rejects_tamper_unknown_state_and_naive_time():
    value = _active()
    with pytest.raises(ValueError, match="identity"):
        validate_schedule_lifecycle({**value, "state": "paused"})
    with pytest.raises(ValueError, match="unsupported"):
        build_schedule_lifecycle(
            source_kind="dream.trigger", source_id="briefing",
            definition_revision="sha256:definition", state="deleted",
            native_owner="vera.dream.scheduler")
    with pytest.raises(ValueError, match="timezone offset"):
        transition_schedule_lifecycle(
            value, requested_state="paused", observed_at="2026-08-31T12:00:00")
    event = transition_schedule_lifecycle(
        value, requested_state="paused", observed_at="2026-08-31T12:00:00Z")
    with pytest.raises(ValueError, match="transition"):
        validate_schedule_lifecycle_event({**event, "state": "active"})


def test_lifecycle_identity_is_content_safe_and_stable():
    first = _active()
    second = _active()

    assert first == second
    assert "prompt" not in str(first)
    assert first["preserves_record"] is True


def test_native_adapters_map_status_without_exposing_content():
    calendar = calendar_action_lifecycle({
        "id": "action-1", "status": "paused", "title": "private title",
        "goal": "private goal", "when": "2026-09-01T09:00:00Z",
    })
    dream = dream_trigger_lifecycle({
        "name": "briefing", "enabled": False, "prompt": "private prompt",
    })

    assert calendar["state"] == "paused"
    assert dream["state"] == "paused"
    assert "private title" not in str(calendar)
    assert "private goal" not in str(calendar)
    assert "private prompt" not in str(dream)

    assert calendar_action_lifecycle({
        "id": "done-1", "status": "done",
    })["state"] == "completed"
    assert calendar_action_lifecycle({
        "id": "failed-1", "status": "failed",
    })["state"] == "failed"
