import asyncio
import importlib

import pytest

from vera.execution.run_journal import MemoryRunJournal
from vera.execution.run_projection import ShadowRunRegistry
from vera.execution.schedule_run_projection import (
    ScheduledRunProjection,
    finish_scheduled_run,
    start_scheduled_run,
)


pytestmark = pytest.mark.critical


def _registry():
    return ShadowRunRegistry(max_runs=20, journal=MemoryRunJournal())


@pytest.mark.parametrize(("outcome", "status"), [
    ({}, "completed"),
    ({"cancelled": True}, "cancelled"),
    ({"error_type": "native_failed"}, "failed"),
])
def test_scheduled_occurrence_projects_only_content_free_identity(outcome, status):
    registry = _registry()
    projection = start_scheduled_run(
        source="research_iteration", native_id="iteration-1",
        schedule_id="schedule-1", occurrence=3, registry=registry)
    finish_scheduled_run(projection, **outcome)

    observed = registry.get(projection.run.id)
    assert observed["run"]["status"] == status
    assert observed["run"]["workflow_id"] == "schedule:schedule-1"
    assert observed["run"]["task_id"] == "occurrence:3"
    assert "query" not in str(observed).lower()
    assert "prompt" not in str(observed).lower()
    assert "result" not in str(observed).lower()


def test_projection_failure_is_isolated():
    class BrokenRegistry:
        def record(self, run, event):
            raise RuntimeError("journal unavailable")

    projection = ScheduledRunProjection(
        source="calendar_action", native_id="action-1",
        schedule_id="schedule-1", registry=BrokenRegistry())
    projection.finish(error_type="native_failed")


@pytest.mark.parametrize(("native_status", "expected"), [
    ("done", "completed"),
    ("failed", "failed"),
])
def test_calendar_system_action_wrapper_preserves_native_status(
        monkeypatch, native_status, expected):
    from vera.calendar import longterm_scheduler as scheduler

    runtime_projection = importlib.import_module(
        "Vera.vera.execution.schedule_run_projection")
    registry = _registry()
    monkeypatch.setattr(runtime_projection, "SHADOW_RUNS", registry)

    action = {
        "id": "action-1", "title": "private title", "side": "system",
        "status": "scheduled", "when": "2026-09-01T09:00:00Z",
        "trigger": {},
    }

    async def native(_action, _cfg):
        return None

    async def get_action(_aid):
        return {**action, "status": native_status}

    monkeypatch.setattr(scheduler, "_run_system_action_native", native)
    monkeypatch.setattr(scheduler, "_get_action", get_action)
    asyncio.run(scheduler._run_system_action(action, {}))

    root = registry.list()[0]
    assert root["status"] == expected
    observed = registry.get(root["id"])
    assert "private title" not in str(observed)


def test_calendar_system_action_wrapper_preserves_cancellation(monkeypatch):
    from vera.calendar import longterm_scheduler as scheduler

    runtime_projection = importlib.import_module(
        "Vera.vera.execution.schedule_run_projection")
    registry = _registry()
    monkeypatch.setattr(runtime_projection, "SHADOW_RUNS", registry)
    action = {
        "id": "action-2", "title": "private title", "side": "system",
        "status": "scheduled", "when": "2026-09-01T09:00:00Z",
        "trigger": {},
    }

    async def native(_action, _cfg):
        raise asyncio.CancelledError()

    monkeypatch.setattr(scheduler, "_run_system_action_native", native)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler._run_system_action(action, {}))
    assert registry.list()[0]["status"] == "cancelled"


def test_calendar_wrapper_binds_the_due_path_selected_by_native_tick(monkeypatch):
    from vera.calendar import longterm_scheduler as scheduler
    from vera.execution.workflow_schedule import calendar_action_schedule

    runtime_projection = importlib.import_module(
        "Vera.vera.execution.schedule_run_projection")
    registry = _registry()
    monkeypatch.setattr(runtime_projection, "SHADOW_RUNS", registry)
    action = {
        "id": "action-both", "title": "private", "side": "system",
        "status": "scheduled", "when": "2026-09-01T09:00:00Z",
        "trigger": {"cap": "health.check"},
    }

    async def native(_action, _cfg):
        return None

    async def get_action(_aid):
        return {**action, "status": "done"}

    monkeypatch.setattr(scheduler, "_run_system_action_native", native)
    monkeypatch.setattr(scheduler, "_get_action", get_action)
    asyncio.run(scheduler._run_system_action(action, {}, due_kind="time"))

    observed = registry.get(registry.list()[0]["id"])
    expected = calendar_action_schedule(action, due_kind="time")["schedule_id"]
    assert observed["run"]["workflow_id"] == f"schedule:{expected}"
