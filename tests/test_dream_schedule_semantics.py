import asyncio
from datetime import datetime, timezone

import pytest


pytestmark = pytest.mark.critical


class _FrozenDateTime(datetime):
    instant = datetime(2026, 10, 26, 8, 30, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.instant if tz is None else cls.instant.astimezone(tz)


def test_trigger_due_uses_timezone_window_and_elapsed_recurrence(monkeypatch):
    from vera.dream import dream_capabilities as dream

    async def last_run(_name):
        return "2026-10-26T07:00:00Z"

    monkeypatch.setattr(dream, "datetime", _FrozenDateTime)
    monkeypatch.setattr(dream, "_last_run_ts", last_run)
    trigger = {
        "name": "briefing", "enabled": True, "timezone": "Europe/London",
        "hours_start": 8, "hours_end": 9, "min_idle_minutes": 0,
        "min_interval_minutes": 60, "sensors": [],
    }

    assert asyncio.run(dream._trigger_due(trigger, idle_min=0)) is True
    trigger["hours_start"] = 9
    trigger["hours_end"] = 10
    assert asyncio.run(dream._trigger_due(trigger, idle_min=0)) is False


def test_invalid_timezone_fails_closed_without_sensor_or_cycle_work(monkeypatch):
    from vera.dream import dream_capabilities as dream

    calls = []

    async def last_run(_name):
        calls.append("last_run")
        return ""

    monkeypatch.setattr(dream, "_last_run_ts", last_run)
    trigger = {
        "name": "broken", "enabled": True, "timezone": "Not/AZone",
        "hours_start": 0, "hours_end": 24, "min_idle_minutes": 0,
        "min_interval_minutes": 60, "sensors": ["must-not-run"],
    }

    assert asyncio.run(dream._trigger_due(trigger, idle_min=0)) is False
    assert calls == []


def test_timeline_reports_local_slots_from_same_schedule_contract(monkeypatch):
    from vera.dream import dream_capabilities as dream

    trigger = {
        "name": "briefing", "label": "Briefing", "enabled": True,
        "timezone": "Europe/London", "hours_start": 8, "hours_end": 9,
        "min_idle_minutes": 0, "min_interval_minutes": 60,
    }

    async def triggers():
        return [trigger]

    async def idle():
        return 120.0

    async def last_run(_name):
        return ""

    monkeypatch.setattr(dream, "datetime", _FrozenDateTime)
    monkeypatch.setattr(dream, "_list_triggers", triggers)
    monkeypatch.setattr(dream, "_idle_minutes", idle)
    monkeypatch.setattr(dream, "_last_run_ts", last_run)

    result = asyncio.run(dream.dream_timeline(hours_ahead=2))
    item = result["triggers"][0]
    assert item["timezone"] == "Europe/London"
    assert item["windows"][0]["hour"] == 8
    assert item["windows"][0]["in_window"] is True
    assert item["windows"][0]["instant"] == "2026-10-26T08:30:00Z"
    assert item["earliest_slot"]["offset_h"] == 0


def test_trigger_upsert_rejects_invalid_schedule_before_save(monkeypatch):
    from vera.dream import dream_capabilities as dream

    saved = []

    async def get_trigger(_name):
        return None

    async def save_trigger(value):
        saved.append(value)

    monkeypatch.setattr(dream, "_get_trigger", get_trigger)
    monkeypatch.setattr(dream, "_save_trigger", save_trigger)
    result = asyncio.run(dream.dream_trigger_upsert(
        name="broken", timezone_name="Not/AZone",
    ))

    assert result["ok"] is False
    assert "invalid schedule" in result["error"]
    assert saved == []
