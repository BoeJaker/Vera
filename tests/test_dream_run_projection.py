import asyncio
import importlib

import pytest

from vera.execution.dream_run_projection import (
    DreamRunProjection,
    configure_dream_run,
    enter_dream_run_boundary,
    exit_dream_run_boundary,
    finish_dream_stage,
    start_dream_stage,
)
from vera.execution.run_journal import MemoryRunJournal
from vera.execution.run_projection import ShadowRunRegistry


pytestmark = pytest.mark.critical


def _registry():
    return ShadowRunRegistry(max_runs=20, journal=MemoryRunJournal())


def test_cycle_and_repeated_stages_project_content_free_parent_child_runs():
    registry = _registry()
    projection, token = enter_dream_run_boundary(
        cycle_id="cycle-1", registry=registry)
    configure_dream_run(
        stages=["dream.stage.gather", "dream.stage.investigate"],
        workflow_plan={"content_hash": "sha256:workflow"},
        schedule_id="dream-schedule-1",
    )

    first = start_dream_stage(
        stage="dream.stage.investigate", index=1, iteration=1)
    finish_dream_stage(first, ok=True)
    second = start_dream_stage(
        stage="dream.stage.investigate", index=1, iteration=2)
    finish_dream_stage(second, ok=False)
    exit_dream_run_boundary(
        projection, token, result={"early_exit": True})

    observed = registry.get(projection.parent.id)
    assert observed["run"]["status"] == "completed"
    assert observed["run"]["workflow_id"] == "dream-workflow:sha256:workflow"
    assert observed["journal"]["ok"] is True
    assert len(observed["children"]) == 2
    assert [child["status"] for child in observed["children"]] == [
        "completed", "failed"]
    assert [child["task_id"] for child in observed["children"]] == [
        "stage:1", "stage:2"]
    assert observed["run"]["events"][-1]["payload"] == {
        "early_exit": True, "observed_stages": 2}
    schedule_events = [event for event in observed["run"]["events"]
                       if event["type"] == "run.schedule.bound"]
    assert schedule_events[0]["payload"] == {
        "native_owner": "vera.dream.scheduler",
        "schedule_id": "dream-schedule-1",
    }
    payloads = [event["payload"] for child in observed["children"]
                for event in child["events"]]
    assert not any("result" in payload or "state" in payload or "prompt" in payload
                   for payload in payloads)


def test_parent_cancellation_closes_active_stage_and_context_resets():
    registry = _registry()
    projection, token = enter_dream_run_boundary(
        cycle_id="cycle-cancel", registry=registry)
    handle = start_dream_stage(stage="dream.stage.execute", index=3, iteration=None)
    exit_dream_run_boundary(projection, token, cancelled=True)

    observed = registry.get(projection.parent.id)
    assert observed["run"]["status"] == "cancelled"
    assert observed["children"][0]["status"] == "cancelled"
    # The old handle is inert after the boundary is gone.
    finish_dream_stage(handle, ok=True)
    assert registry.get(projection.parent.id)["children"][0]["status"] == "cancelled"


def test_projection_failures_never_escape_into_native_execution():
    class BrokenRegistry:
        def record(self, run, event):
            raise RuntimeError("journal unavailable")

    projection = DreamRunProjection(cycle_id="isolated", registry=BrokenRegistry())
    projection.configure(stages=["dream.stage.gather"])
    handle = projection.stage_started(
        stage="dream.stage.gather", index=0, iteration=None)
    projection.stage_finished(handle, ok=False)
    projection.finish(error_type="RuntimeError")


def test_run_cycle_wrapper_preserves_result_and_terminal_semantics(monkeypatch):
    import vera.dream.dream_capabilities as dream
    registry = _registry()
    runtime_projection = importlib.import_module(
        dream.configure_dream_run.__module__)
    monkeypatch.setattr(
        dream, "enter_dream_run_boundary",
        lambda *, cycle_id: runtime_projection.enter_dream_run_boundary(
            cycle_id=cycle_id, registry=registry))

    async def native(trig, force=False, seed=None, preview_only=False):
        dream.configure_dream_run(stages=["dream.stage.gather"])
        handle = dream.start_dream_stage(
            stage="dream.stage.gather", index=0, iteration=None)
        dream.finish_dream_stage(handle, ok=True)
        return {"cycle_id": seed["cycle_id"], "early_exit": False}

    monkeypatch.setattr(dream, "_run_cycle_native", native)
    result = asyncio.run(dream._run_cycle({"name": "test"}))

    assert result["cycle_id"]
    runs = registry.list()
    assert len(runs) == 2
    root = next(item for item in runs if not item["parent_run_id"])
    assert root["status"] == "completed"
    observed = registry.get(root["id"])
    assert observed["run"]["trace_id"] == result["cycle_id"]
    assert observed["children"][0]["status"] == "completed"


@pytest.mark.parametrize("kind", ["cancelled", "failed"])
def test_run_cycle_wrapper_closes_cancel_and_failure(monkeypatch, kind):
    import vera.dream.dream_capabilities as dream
    registry = _registry()
    runtime_projection = importlib.import_module(
        dream.configure_dream_run.__module__)
    monkeypatch.setattr(
        dream, "enter_dream_run_boundary",
        lambda *, cycle_id: runtime_projection.enter_dream_run_boundary(
            cycle_id=cycle_id, registry=registry))

    async def native(*args, **kwargs):
        if kind == "cancelled":
            raise asyncio.CancelledError()
        raise RuntimeError("native failure")

    monkeypatch.setattr(dream, "_run_cycle_native", native)
    with pytest.raises(asyncio.CancelledError if kind == "cancelled" else RuntimeError):
        asyncio.run(dream._run_cycle({"name": "test"}))

    root = next(item for item in registry.list() if not item["parent_run_id"])
    assert root["status"] == kind
