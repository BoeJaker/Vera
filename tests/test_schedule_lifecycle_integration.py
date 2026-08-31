import asyncio

import pytest


pytestmark = pytest.mark.critical


def test_calendar_pause_resume_cancel_preserve_record_and_terminal_state(monkeypatch):
    from vera.calendar import longterm_scheduler as scheduler

    stored = {
        "id": "action-1", "title": "Action", "side": "user",
        "status": "scheduled", "when": "2026-09-01T09:00:00Z",
        "trigger": {},
    }
    emitted = []

    async def get_action(_id):
        return dict(stored)

    async def save_action(value):
        stored.clear()
        stored.update(value)
        return dict(stored)

    async def emit(value):
        emitted.append(value)

    monkeypatch.setattr(scheduler, "_get_action", get_action)
    monkeypatch.setattr(scheduler, "_save_action", save_action)
    monkeypatch.setattr(scheduler, "emit_event", emit)

    paused = asyncio.run(scheduler.cap_sched_action_pause(id="action-1"))
    resumed = asyncio.run(scheduler.cap_sched_action_resume(id="action-1"))
    cancelled = asyncio.run(scheduler.cap_sched_action_cancel(id="action-1"))
    refused = asyncio.run(scheduler.cap_sched_action_resume(id="action-1"))

    assert paused["lifecycle"]["state"] == "paused"
    assert resumed["lifecycle"]["state"] == "active"
    assert cancelled["lifecycle"]["state"] == "cancelled"
    assert stored["id"] == "action-1"
    assert stored["status"] == "cancelled"
    assert refused["ok"] is False
    assert "cannot transition" in refused["error"]
    assert [event["transition"] for event in emitted] == [
        "active_to_paused", "paused_to_active", "active_to_cancelled",
    ]


def test_paused_calendar_action_is_not_evaluated_or_fired(monkeypatch):
    from vera.calendar import longterm_scheduler as scheduler

    async def get_config():
        return dict(scheduler.DEFAULT_CONFIG)

    async def list_actions():
        return [{
            "id": "action-1", "title": "Action", "side": "user",
            "status": "paused", "when": "2020-01-01T00:00:00Z",
            "trigger": {},
        }]

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("paused action reached an effect path")

    monkeypatch.setattr(scheduler, "_get_config", get_config)
    monkeypatch.setattr(scheduler, "_list_actions", list_actions)
    monkeypatch.setattr(scheduler, "_notify_user", must_not_run)
    monkeypatch.setattr(scheduler, "_emit_workflow_trigger", must_not_run)

    assert asyncio.run(scheduler._evaluate_once()) == {
        "evaluated": 1, "fired": [],
    }


def test_calendar_terminal_and_inflight_actions_cannot_be_resurrected(monkeypatch):
    from vera.calendar import longterm_scheduler as scheduler

    stored = {"id": "action-1", "status": "done"}

    async def get_action(_id):
        return dict(stored)

    monkeypatch.setattr(scheduler, "_get_action", get_action)
    completed = asyncio.run(scheduler.cap_sched_action_resume(id="action-1"))
    stored["status"] = "running"
    running = asyncio.run(scheduler.cap_sched_action_cancel(id="action-1"))
    stored["status"] = "awaiting_reply"
    awaiting = asyncio.run(scheduler.cap_sched_action_pause(id="action-1"))

    assert completed["ok"] is False
    assert "cannot transition" in completed["error"]
    assert running["ok"] is False and "while action is running" in running["error"]
    assert awaiting["ok"] is False and "awaiting_reply" in awaiting["error"]


def test_dream_pause_resume_cancel_are_persisted_and_gate_due_work(monkeypatch):
    from vera.dream import dream_capabilities as dream

    stored = {
        "name": "briefing", "enabled": True, "lifecycle_state": "active",
        "timezone": "UTC", "hours_start": 0, "hours_end": 24,
        "min_idle_minutes": 0, "min_interval_minutes": 60, "sensors": [],
    }
    emitted = []

    async def get_trigger(_name):
        return dict(stored)

    async def save_trigger(value):
        stored.clear()
        stored.update(value)

    async def emit(value):
        emitted.append(value)

    monkeypatch.setattr(dream, "_get_trigger", get_trigger)
    monkeypatch.setattr(dream, "_save_trigger", save_trigger)
    monkeypatch.setattr(dream, "emit_event", emit)

    paused = asyncio.run(dream.dream_trigger_pause(name="briefing"))
    assert paused["lifecycle"]["state"] == "paused"
    assert asyncio.run(dream._trigger_due(dict(stored), idle_min=999)) is False
    resumed = asyncio.run(dream.dream_trigger_resume(name="briefing"))
    cancelled = asyncio.run(dream.dream_trigger_cancel(name="briefing"))
    refused = asyncio.run(dream.dream_trigger_resume(name="briefing"))

    assert resumed["lifecycle"]["state"] == "active"
    assert cancelled["lifecycle"]["state"] == "cancelled"
    assert stored["enabled"] is False
    assert stored["lifecycle_state"] == "cancelled"
    assert refused["ok"] is False
    assert [event["transition"] for event in emitted] == [
        "active_to_paused", "paused_to_active", "active_to_cancelled",
    ]


def test_lifecycle_event_failure_does_not_undo_native_pause(monkeypatch):
    from vera.dream import dream_capabilities as dream

    stored = {"name": "briefing", "enabled": True, "lifecycle_state": "active"}

    async def get_trigger(_name):
        return dict(stored)

    async def save_trigger(value):
        stored.clear()
        stored.update(value)

    async def broken_emit(_value):
        raise RuntimeError("event stream unavailable")

    monkeypatch.setattr(dream, "_get_trigger", get_trigger)
    monkeypatch.setattr(dream, "_save_trigger", save_trigger)
    monkeypatch.setattr(dream, "emit_event", broken_emit)

    result = asyncio.run(dream.dream_trigger_pause(name="briefing"))

    assert result["ok"] is True
    assert stored["enabled"] is False
    assert stored["lifecycle_state"] == "paused"


def test_legacy_toggle_uses_lifecycle_and_upsert_cannot_revive_cancelled(monkeypatch):
    from vera.dream import dream_capabilities as dream

    stored = {
        "name": "briefing", "enabled": True, "lifecycle_state": "active",
        "timezone": "UTC", "hours_start": 0, "hours_end": 24,
        "min_interval_minutes": 60,
    }

    async def get_trigger(_name):
        return dict(stored)

    async def save_trigger(value):
        stored.clear()
        stored.update(value)

    async def emit(_value):
        return None

    monkeypatch.setattr(dream, "_get_trigger", get_trigger)
    monkeypatch.setattr(dream, "_save_trigger", save_trigger)
    monkeypatch.setattr(dream, "emit_event", emit)

    asyncio.run(dream.dream_trigger_toggle(name="briefing", enabled=False))
    assert stored["lifecycle_state"] == "paused"
    asyncio.run(dream.dream_trigger_toggle(name="briefing", enabled=True))
    assert stored["lifecycle_state"] == "active"
    asyncio.run(dream.dream_trigger_cancel(name="briefing"))

    preserved = asyncio.run(dream.dream_trigger_upsert(
        name="briefing", label="Updated", enabled=False))
    refused = asyncio.run(dream.dream_trigger_upsert(
        name="briefing", enabled=True))

    assert preserved["ok"] is True
    assert preserved["trigger"]["lifecycle"]["state"] == "cancelled"
    assert stored["lifecycle_state"] == "cancelled"
    assert refused["ok"] is False
    assert "cannot be resumed" in refused["error"]
