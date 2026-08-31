import asyncio

import pytest

from vera.execution.workflow_trigger import (
    EVENT_TYPE,
    SCHEMA,
    build_workflow_trigger,
    calendar_action_schedule_decision,
    calendar_action_workflow_trigger,
    dream_schedule_decision,
    dream_schedule_workflow_trigger,
    validate_workflow_trigger,
)


pytestmark = pytest.mark.critical
NOW = "2026-08-31T12:00:00+00:00"


def test_trigger_identity_is_deterministic_across_observation_time():
    common = {
        "source_kind": "calendar.action", "source_id": "action-1",
        "source_revision": "sha256:definition", "target_ref": "sched.action:action-1",
        "schedule_kind": "time", "occurrence_key": "one-shot",
        "timezone_name": "UTC", "scheduled_for": "2026-09-01T09:00:00+00:00",
        "native_owner": "vera.calendar.longterm_scheduler",
    }

    first = build_workflow_trigger(observed_at=NOW, **common)
    later = build_workflow_trigger(
        observed_at="2026-08-31T12:05:00+00:00", **common,
    )

    assert first["schema"] == SCHEMA
    assert first["type"] == EVENT_TYPE
    assert first["trigger_id"] == later["trigger_id"]
    assert first["idempotency_key"] == first["trigger_id"]
    assert first["authority"] == {
        "scheduler": "vera.calendar.longterm_scheduler",
        "execution": "native", "projection": "workflow_trigger",
    }
    assert first["policy"] == {
        "duplicates": "receipt_observed", "misfire": "decision_sidecar",
        "catch_up": "max_one_no_effect_replay",
    }
    assert first["executes"] is False


def test_trigger_validation_fails_closed_on_identity_timezone_and_extra_fields():
    event = build_workflow_trigger(
        source_kind="dream.trigger", source_id="briefing",
        source_revision="sha256:definition", target_ref="dream.trigger:briefing",
        schedule_kind="idle_interval", occurrence_key="initial",
        observed_at=NOW, native_owner="vera.dream.scheduler",
    )

    forged = {**event, "trigger_id": "sha256:forged"}
    with pytest.raises(ValueError, match="identity"):
        validate_workflow_trigger(forged)
    changed_schedule = {
        **event,
        "schedule": {**event["schedule"], "timezone": "Europe/London"},
    }
    with pytest.raises(ValueError, match="identity"):
        validate_workflow_trigger(changed_schedule)
    with pytest.raises(ValueError, match="timezone offset"):
        build_workflow_trigger(
            source_kind="dream.trigger", source_id="briefing",
            source_revision="sha256:definition", target_ref="dream.trigger:briefing",
            schedule_kind="idle_interval", occurrence_key="initial",
            observed_at="2026-08-31T12:00:00", native_owner="vera.dream.scheduler",
        )
    with pytest.raises(ValueError, match="fields"):
        validate_workflow_trigger({**event, "private_payload": "no"})
    with pytest.raises(ValueError, match="scheduled_for is required"):
        build_workflow_trigger(
            source_kind="calendar.action", source_id="action-1",
            source_revision="sha256:definition", target_ref="sched.action:action-1",
            schedule_kind="time", occurrence_key="one-shot", observed_at=NOW,
            native_owner="vera.calendar.longterm_scheduler",
        )
    missing_schedule_time = {
        **build_workflow_trigger(
            source_kind="calendar.action", source_id="action-1",
            source_revision="sha256:definition", target_ref="sched.action:action-1",
            schedule_kind="time", occurrence_key="one-shot", observed_at=NOW,
            scheduled_for="2026-09-01T09:00:00+00:00",
            native_owner="vera.calendar.longterm_scheduler",
        ),
    }
    missing_schedule_time["schedule"] = {
        **missing_schedule_time["schedule"], "scheduled_for": "",
    }
    with pytest.raises(ValueError, match="scheduled_for is required"):
        validate_workflow_trigger(missing_schedule_time)
    unexpected_schedule_time = {
        **event,
        "schedule": {
            **event["schedule"],
            "scheduled_for": "2026-09-01T09:00:00+00:00",
        },
    }
    with pytest.raises(ValueError, match="scheduled_for is only valid"):
        validate_workflow_trigger(unexpected_schedule_time)


def test_calendar_adapter_matches_native_naive_utc_and_redacts_action_content():
    event = calendar_action_workflow_trigger({
        "id": "action-1", "title": "private title", "goal": "private goal",
        "instructions": "private instructions", "side": "system",
        "profile": "planning", "when": "2026-09-01T09:00:00",
        "trigger": {},
    }, observed_at=NOW, due_kind="time")

    assert event["source"]["kind"] == "calendar.action"
    assert event["schedule"] == {
        "kind": "time", "timezone": "UTC",
        "scheduled_for": "2026-09-01T09:00:00Z",
    }
    assert "private title" not in str(event)
    assert "private goal" not in str(event)
    assert "private instructions" not in str(event)


def test_dream_adapter_uses_prior_run_as_recurrence_identity_and_redacts_prompt():
    trigger = {
        "name": "briefing", "prompt": "private prompt", "description": "private",
        "hours_start": 5, "hours_end": 9, "min_idle_minutes": 20,
        "min_interval_minutes": 720, "sensors": ["sensor.news"],
        "pipeline": ["stage.gather", "stage.deliver"],
    }
    first = dream_schedule_workflow_trigger(
        trigger, observed_at=NOW, previous_run="",
    )
    retry = dream_schedule_workflow_trigger(
        trigger, observed_at="2026-08-31T12:01:00+00:00", previous_run="",
    )
    next_run = dream_schedule_workflow_trigger(
        trigger, observed_at="2026-09-01T12:00:00+00:00", previous_run=NOW,
    )

    assert first["trigger_id"] == retry["trigger_id"]
    assert first["trigger_id"] != next_run["trigger_id"]
    assert first["occurrence"]["key"] == "initial"
    assert "private prompt" not in str(first)
    assert "description" not in str(first)


def test_dream_adapter_projects_configured_iana_timezone():
    event = dream_schedule_workflow_trigger({
        "name": "briefing", "timezone": "Europe/London",
        "hours_start": 8, "hours_end": 10, "min_interval_minutes": 60,
    }, observed_at=NOW)

    assert event["schedule"]["timezone"] == "Europe/London"


def test_adapter_decisions_correlate_to_trigger_without_private_content():
    action = {
        "id": "action-1", "title": "private", "goal": "private goal",
        "when": "2026-08-31T09:00:00Z", "side": "system",
    }
    trigger = calendar_action_workflow_trigger(
        action, observed_at=NOW, due_kind="time")
    decision = calendar_action_schedule_decision(
        action, observed_at=NOW, due_kind="time",
        trigger_id=trigger["trigger_id"])

    assert decision["trigger_id"] == trigger["trigger_id"]
    assert decision["classification"] == "misfire"
    assert decision["disposition"] == "due_once"
    assert "private" not in str(decision)

    dream_trigger = {
        "name": "briefing", "prompt": "private prompt", "timezone": "UTC",
        "hours_start": 0, "hours_end": 24, "min_interval_minutes": 60,
    }
    dream_event = dream_schedule_workflow_trigger(
        dream_trigger, observed_at=NOW, previous_run="2026-08-31T08:00:00Z")
    dream_decision = dream_schedule_decision(
        dream_trigger, observed_at=NOW, previous_run="2026-08-31T08:00:00Z",
        trigger_id=dream_event["trigger_id"])
    assert dream_decision["disposition"] == "coalesced_once"
    assert dream_decision["missed_occurrences"] == 4
    assert "private prompt" not in str(dream_decision)


def test_calendar_native_fire_survives_projection_and_receipt_failure(monkeypatch):
    from vera.calendar import longterm_scheduler as scheduler

    calls = []
    action = {
        "id": "action-1", "title": "Action", "side": "user",
        "status": "scheduled", "when": "2020-01-01T00:00:00+00:00",
        "trigger": {}, "comms_channel": "test",
    }

    async def get_config():
        return dict(scheduler.DEFAULT_CONFIG)

    async def list_actions():
        return [dict(action)]

    async def save_action(value):
        calls.append(("saved", value["id"]))
        return value

    async def notify(_action, _channel):
        calls.append(("notified", _action["id"]))
        return True

    async def broken_emit(event):
        calls.append(("projected", event["schedule"]["kind"]))
        raise RuntimeError("event bus unavailable")

    monkeypatch.setattr(scheduler, "_get_config", get_config)
    monkeypatch.setattr(scheduler, "_list_actions", list_actions)
    monkeypatch.setattr(scheduler, "_save_action", save_action)
    monkeypatch.setattr(scheduler, "_notify_user", notify)
    monkeypatch.setattr(scheduler, "emit_event", broken_emit)
    def broken_receipt(_event):
        raise RuntimeError("receipt store unavailable")

    monkeypatch.setattr(scheduler, "_record_workflow_trigger", broken_receipt)
    result = asyncio.run(scheduler._evaluate_once())

    assert result == {"evaluated": 1, "fired": ["action-1"]}
    assert calls[:2] == [("projected", "time"), ("notified", "action-1")]
    assert ("saved", "action-1") in calls


def test_calendar_emits_correlated_receipt_and_schedule_decision(
        monkeypatch, tmp_path):
    from vera.calendar import longterm_scheduler as scheduler
    from vera.execution import workflow_trigger_receipts as receipts

    action = {
        "id": "action-1", "title": "private title", "side": "user",
        "status": "scheduled", "when": "2020-01-01T00:00:00Z",
        "trigger": {}, "comms_channel": "test",
    }
    emitted = []

    async def get_config():
        return dict(scheduler.DEFAULT_CONFIG)

    async def list_actions():
        return [dict(action)]

    async def save_action(value):
        return value

    async def notify(_action, _channel):
        return True

    async def emit(event):
        emitted.append(event)

    ledger = receipts.WorkflowTriggerReceiptLedger(tmp_path / "receipts.sqlite3")
    monkeypatch.setattr(scheduler, "_get_config", get_config)
    monkeypatch.setattr(scheduler, "_list_actions", list_actions)
    monkeypatch.setattr(scheduler, "_save_action", save_action)
    monkeypatch.setattr(scheduler, "_notify_user", notify)
    monkeypatch.setattr(scheduler, "emit_event", emit)
    monkeypatch.setattr(scheduler, "_record_workflow_trigger", ledger.record)

    result = asyncio.run(scheduler._evaluate_once())

    assert result["fired"] == ["action-1"]
    assert [item["type"] for item in emitted] == [
        EVENT_TYPE, "workflow.trigger.receipt.recorded",
        "workflow.schedule.decision",
    ]
    assert emitted[2]["trigger_id"] == emitted[0]["trigger_id"]
    assert emitted[2]["disposition"] == "due_once"
    assert "private title" not in str(emitted[2])


def test_dream_projection_emits_same_contract_and_is_failure_isolated(
        monkeypatch, tmp_path):
    from vera.dream import dream_capabilities as dream
    from vera.execution import workflow_trigger_receipts as receipts

    emitted = []

    async def last_run(_name):
        return "2026-08-30T12:00:00+00:00"

    async def emit(event):
        emitted.append(event)

    monkeypatch.setattr(dream, "_last_run_ts", last_run)
    monkeypatch.setattr(dream, "emit_event", emit)
    ledger = receipts.WorkflowTriggerReceiptLedger(tmp_path / "receipts.sqlite3")
    monkeypatch.setattr(dream, "_record_dream_workflow_trigger", ledger.record)
    asyncio.run(dream._emit_dream_workflow_trigger({
        "name": "briefing", "hours_start": 5, "hours_end": 9,
        "min_idle_minutes": 20, "min_interval_minutes": 720,
        "sensors": [], "pipeline": ["stage.gather"],
    }))

    assert emitted[0]["type"] == EVENT_TYPE
    assert emitted[0]["schema"] == SCHEMA
    assert emitted[0]["source"]["kind"] == "dream.trigger"
    assert emitted[1]["type"] == "workflow.trigger.receipt.recorded"
    assert emitted[1]["classification"] == "first_seen"
    assert emitted[2]["type"] == "workflow.schedule.decision"
    assert emitted[2]["trigger_id"] == emitted[0]["trigger_id"]
    assert emitted[2]["disposition"] == "coalesced_once"
    assert emitted[2]["executes"] is False

    async def broken_emit(_event):
        raise RuntimeError("event bus unavailable")

    monkeypatch.setattr(dream, "emit_event", broken_emit)
    asyncio.run(dream._emit_dream_workflow_trigger({"name": "briefing"}))


def test_dream_scheduler_emits_projection_before_native_cycle_creation():
    from vera.dream import dream_capabilities as dream

    source = open(dream.__file__, encoding="utf-8").read()
    projection = source.index("await _emit_dream_workflow_trigger(trig)")
    native_cycle = source.index(
        "_CYCLE_TASK = asyncio.create_task(_run_cycle(trig))", projection,
    )
    assert projection < native_cycle
