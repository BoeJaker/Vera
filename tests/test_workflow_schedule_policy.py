from datetime import datetime, timezone

import pytest

from vera.execution.workflow_schedule import (
    build_workflow_schedule,
    dream_trigger_schedule,
)
from vera.execution.workflow_schedule_policy import (
    build_schedule_policy,
    calendar_action_policy,
    classify_schedule_occurrence,
    dream_trigger_policy,
    validate_schedule_decision,
    validate_schedule_policy,
)


pytestmark = pytest.mark.critical


def test_calendar_late_one_shot_is_explicit_fire_once_without_effect_replay():
    schedule = build_workflow_schedule(
        kind="one_shot", scheduled_for="2026-08-31T09:00:00Z")
    policy = calendar_action_policy({}, due_kind="time")
    value = classify_schedule_occurrence(
        schedule, policy, evaluated_at="2026-08-31T12:00:00Z",
        trigger_id="sha256:trigger-1")

    assert policy["mode"] == "fire_once"
    assert policy["max_catch_up"] == 1
    assert value["classification"] == "misfire"
    assert value["disposition"] == "due_once"
    assert value["missed_occurrences"] == 1
    assert value["replay_effects"] is False
    assert value["executes"] is False


def test_dream_downtime_is_coalesced_to_one_occurrence():
    schedule = dream_trigger_schedule({
        "timezone": "UTC", "hours_start": 0, "hours_end": 24,
        "min_interval_minutes": 60,
    })
    policy = dream_trigger_policy({})
    value = classify_schedule_occurrence(
        schedule, policy, last_completed_at="2026-08-31T08:00:00Z",
        evaluated_at="2026-08-31T12:30:00Z")

    assert value["classification"] == "misfire"
    assert value["disposition"] == "coalesced_once"
    assert value["missed_occurrences"] == 4
    assert value["max_catch_up"] == 1


def test_not_due_and_within_grace_boundaries_are_deterministic():
    schedule = build_workflow_schedule(
        kind="one_shot", scheduled_for="2026-08-31T09:00:00Z")
    policy = build_schedule_policy(mode="fire_once", grace_seconds=300)

    waiting = classify_schedule_occurrence(
        schedule, policy, evaluated_at="2026-08-31T08:59:59Z")
    boundary = classify_schedule_occurrence(
        schedule, policy, evaluated_at="2026-08-31T09:05:00Z")
    late = classify_schedule_occurrence(
        schedule, policy, evaluated_at="2026-08-31T09:05:01Z")

    assert (waiting["classification"], waiting["disposition"]) == ("not_due", "wait")
    assert boundary["classification"] == "within_grace"
    assert late["classification"] == "misfire"


def test_skip_policy_classifies_without_executing_or_replaying():
    schedule = build_workflow_schedule(
        kind="one_shot", scheduled_for="2026-08-31T09:00:00Z")
    policy = build_schedule_policy(mode="skip", grace_seconds=60)
    value = classify_schedule_occurrence(
        schedule, policy, evaluated_at="2026-08-31T09:02:00Z")

    assert value["disposition"] == "skip"
    assert value["max_catch_up"] == 0
    assert value["replay_effects"] is False
    assert value["executes"] is False


def test_condition_policy_defers_to_native_condition_authority():
    schedule = build_workflow_schedule(kind="condition")
    policy = calendar_action_policy({}, due_kind="condition")
    value = classify_schedule_occurrence(
        schedule, policy, evaluated_at=datetime(2026, 8, 31, tzinfo=timezone.utc))

    assert value["classification"] == "condition"
    assert value["disposition"] == "native_condition"
    assert value["due_at"] == ""


def test_policy_validation_rejects_tamper_and_naive_times():
    policy = build_schedule_policy(mode="coalesce_once", grace_seconds=0)
    with pytest.raises(ValueError, match="identity"):
        validate_schedule_policy({**policy, "grace_seconds": 1})
    schedule = build_workflow_schedule(
        kind="one_shot", scheduled_for="2026-08-31T09:00:00Z")
    with pytest.raises(ValueError, match="timezone offset"):
        classify_schedule_occurrence(
            schedule, policy, evaluated_at="2026-08-31T09:00:00")
    decision = classify_schedule_occurrence(
        schedule, policy, evaluated_at="2026-08-31T09:00:00Z")
    with pytest.raises(ValueError, match="identity"):
        validate_schedule_decision({**decision, "missed_occurrences": 99})
