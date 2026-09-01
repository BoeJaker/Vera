from pathlib import Path

import pytest

from vera.execution.workflow_schedule import research_iteration_schedule
from vera.execution.workflow_schedule_lifecycle import research_iteration_lifecycle
from vera.execution.workflow_trigger import (
    research_iteration_schedule_decision,
    research_iteration_workflow_trigger,
    validate_workflow_trigger,
)


pytestmark = pytest.mark.critical


def _iteration(**overrides):
    value = {
        "id": "iter-1", "target_type": "project", "target_id": "project-1",
        "seed_query": "private research question", "mode": "single",
        "output_mode": "report", "interval_secs": 300, "status": "running",
    }
    value.update(overrides)
    return value


def test_research_schedule_reflects_native_effective_interval():
    schedule = research_iteration_schedule(_iteration(interval_secs=0))
    assert schedule["kind"] == "interval_after_completion"
    assert schedule["timezone"] == "UTC"
    assert schedule["window"] == {"start_hour": 0, "end_hour": 24}
    assert schedule["recurrence"]["interval_seconds"] == 10
    assert schedule["executes"] is False


def test_research_lifecycle_projects_only_records_that_still_exist():
    active = research_iteration_lifecycle(_iteration())
    paused = research_iteration_lifecycle(_iteration(status="paused"))
    assert active["state"] == "active"
    assert paused["state"] == "paused"
    assert "private research question" not in str(active)
    with pytest.raises(ValueError, match="not safely projectable"):
        research_iteration_lifecycle(_iteration(status="stopped"))


def test_research_trigger_and_decision_are_content_safe_observations():
    iteration = _iteration()
    event = research_iteration_workflow_trigger(
        iteration, observed_at="2026-09-01T12:05:01Z",
        previous_run="2026-09-01T12:00:00Z")
    decision = research_iteration_schedule_decision(
        iteration, observed_at="2026-09-01T12:05:01Z",
        previous_run="2026-09-01T12:00:00Z",
        trigger_id=event["trigger_id"])
    assert validate_workflow_trigger(event) == event
    assert event["source"]["kind"] == "research.iteration"
    assert event["schedule"]["kind"] == "completion_interval"
    assert event["authority"]["execution"] == "native"
    assert event["executes"] is False
    assert "private research question" not in str(event)
    assert decision["classification"] == "misfire"
    assert decision["disposition"] == "due_once"
    assert decision["replay_effects"] is False


def test_initial_research_occurrence_is_due_once_and_identity_is_stable():
    event = research_iteration_workflow_trigger(
        _iteration(), observed_at="2026-09-01T12:00:00Z")
    repeated = research_iteration_workflow_trigger(
        _iteration(), observed_at="2026-09-01T12:01:00Z")
    decision = research_iteration_schedule_decision(
        _iteration(), observed_at="2026-09-01T12:00:00Z",
        trigger_id=event["trigger_id"])
    assert event["trigger_id"] == repeated["trigger_id"]
    assert decision["classification"] == "initial"
    assert decision["disposition"] == "due_once"


def test_native_loop_emits_projection_only_after_query_generation():
    source = (Path(__file__).parents[1] / "vera" / "research" /
              "researcher_api.py").read_text(encoding="utf-8")
    generation = source.index("next_q = await _iter_next_query")
    projection = source.index("_schedule_iteration_workflow_trigger(it, tm)")
    native_job = source.index("job = ResearchJob(", projection)
    assert generation < projection < native_job
    assert "projection failures must never delay or suppress" in source
    assert "_iter_evidence_tasks.add(task)" in source
