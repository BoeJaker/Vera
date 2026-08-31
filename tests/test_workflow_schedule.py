from datetime import datetime, timezone

import pytest

from vera.execution.workflow_schedule import (
    build_workflow_schedule,
    calendar_action_schedule,
    dream_trigger_schedule,
    recurrence_due,
    validate_workflow_schedule,
    within_schedule_window,
)


pytestmark = pytest.mark.critical


def test_calendar_one_shot_is_canonical_utc_and_non_executing():
    value = calendar_action_schedule(
        {"when": "2026-10-25T09:00:00+01:00"}, due_kind="time")

    assert value["kind"] == "one_shot"
    assert value["timezone"] == "UTC"
    assert value["scheduled_for"] == "2026-10-25T08:00:00Z"
    assert value["window"] is None
    assert value["recurrence"] is None
    assert value["executes"] is False


def test_dream_schedule_has_explicit_timezone_window_and_completion_interval():
    value = dream_trigger_schedule({
        "timezone": "Europe/London", "hours_start": 22, "hours_end": 6,
        "min_interval_minutes": 90,
    })

    assert value["timezone"] == "Europe/London"
    assert value["window"] == {"start_hour": 22, "end_hour": 6}
    assert value["recurrence"] == {
        "kind": "interval", "anchor": "last_completed", "interval_seconds": 5400,
    }


def test_local_window_is_dst_aware_for_same_utc_hour():
    schedule = dream_trigger_schedule({
        "timezone": "Europe/London", "hours_start": 8, "hours_end": 9,
        "min_interval_minutes": 60,
    })

    # 08:30 UTC is 09:30 BST before the fall-back, then 08:30 GMT afterwards.
    before_fallback = datetime(2026, 10, 24, 8, 30, tzinfo=timezone.utc)
    after_fallback = datetime(2026, 10, 26, 8, 30, tzinfo=timezone.utc)
    assert within_schedule_window(schedule, before_fallback) is False
    assert within_schedule_window(schedule, after_fallback) is True


def test_overnight_and_full_day_windows_are_deterministic():
    overnight = dream_trigger_schedule({
        "timezone": "UTC", "hours_start": 22, "hours_end": 6,
        "min_interval_minutes": 60,
    })
    full_day = dream_trigger_schedule({
        "timezone": "UTC", "hours_start": 0, "hours_end": 24,
        "min_interval_minutes": 60,
    })

    assert within_schedule_window(
        overnight, datetime(2026, 1, 1, 23, tzinfo=timezone.utc)) is True
    assert within_schedule_window(
        overnight, datetime(2026, 1, 1, 12, tzinfo=timezone.utc)) is False
    assert within_schedule_window(
        full_day, datetime(2026, 1, 1, 12, tzinfo=timezone.utc)) is True


def test_recurrence_uses_elapsed_instants_not_local_wall_clock():
    schedule = dream_trigger_schedule({
        "timezone": "Europe/London", "hours_start": 0, "hours_end": 24,
        "min_interval_minutes": 120,
    })
    instant = datetime(2026, 10, 25, 2, 30, tzinfo=timezone.utc)

    assert recurrence_due(
        schedule, last_completed_at="2026-10-25T01:00:00Z", instant=instant) is False
    assert recurrence_due(
        schedule, last_completed_at="2026-10-25T00:30:00Z", instant=instant) is True


def test_schedule_validation_fails_closed_on_tamper_and_invalid_zone():
    value = build_workflow_schedule(
        kind="one_shot", scheduled_for="2026-09-01T09:00:00+00:00")
    with pytest.raises(ValueError, match="identity"):
        validate_workflow_schedule({**value, "timezone": "Europe/London"})
    with pytest.raises(ValueError, match="IANA"):
        dream_trigger_schedule({
            "timezone": "Not/AZone", "hours_start": 0, "hours_end": 24,
            "min_interval_minutes": 60,
        })
    with pytest.raises(ValueError, match="end_hour"):
        dream_trigger_schedule({
            "timezone": "UTC", "hours_start": 0, "hours_end": 25,
            "min_interval_minutes": 60,
        })
