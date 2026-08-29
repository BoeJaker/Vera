import asyncio
import json
import sys

import pytest

from vera.execution.dbos_mapping import (
    DBOS_PACKAGE, DBOSDurabilityMapping, DBOSMappingGap, DBOSStepMapping,
    compile_dbos_mapping)
from vera.execution.durability_fixture import build_durability_fixture


pytestmark = pytest.mark.critical


def test_mapping_is_canonical_pinned_and_bound_to_lib15_without_importing_dbos():
    before = set(sys.modules)
    first = compile_dbos_mapping()
    second = compile_dbos_mapping()
    assert first == second
    assert first.mapping_id == second.mapping_id
    assert first.package == DBOS_PACKAGE == "dbos==2.30.0"
    fixture = build_durability_fixture()
    assert first.fixture_id == fixture.fixture_id
    assert first.workflow_content_hash == fixture.workflow["content_hash"]
    assert set(sys.modules) - before == set()
    assert "dbos" not in sys.modules
    assert first.to_dict()["executes"] is False
    assert first.to_dict()["imports_runtime"] is False


def test_mapping_uses_documented_constructs_and_preserves_step_contracts():
    mapping = compile_dbos_mapping()
    steps = {item.step_id: item for item in mapping.steps}
    assert mapping.workflow_construct == "DBOS.workflow"
    assert mapping.workflow_id_source == "Run.id|SetWorkflowID"
    assert steps["durable_wait"].construct == "DBOS.sleep"
    assert steps["durable_wait"].input_bindings == ("wake_at",)
    retry = dict(steps["retryable_step"].options)
    assert retry == {
        "interval_seconds": 1,
        "max_attempts": 3,
        "preemptible": True,
        "retries_allowed": True,
        "timeout_seconds": 30,
    }
    effect = steps["idempotent_effect"]
    assert effect.input_bindings == ("request_id",)
    assert effect.output_binding == "receipt"
    assert dict(effect.options) == {"preemptible": False}


def test_status_and_event_mapping_do_not_hide_timeout_or_synthetic_observations():
    mapping = compile_dbos_mapping()
    statuses = dict(mapping.status_mapping)
    events = dict(mapping.event_sources)
    assert statuses["CANCELLED"] == "cancelled|timed_out"
    assert events["run.timed_out"] == "WorkflowStatus.CANCELLED|timeout_deadline"
    assert events["step.retrying"] == "adapter_observer"
    assert events["effect.receipt.recorded"] == "verified_step_result_artifact"


def test_mapping_is_explicitly_not_ready_until_all_live_semantics_are_proven():
    mapping = compile_dbos_mapping()
    assert not mapping.ready_for_execution
    codes = {item.code for item in mapping.gaps}
    assert codes == {
        "absolute_wake_translation",
        "timeout_cancel_ambiguity",
        "compatible_version_plan_missing",
        "effect_receipt_atomicity_unproven",
        "run_event_projection_unproven",
        "large_result_boundary_unmapped",
        "cancellation_boundary_unproven",
    }
    assert all(item.blocking for item in mapping.gaps)


def test_mapping_refuses_a_different_step_shape_instead_of_guessing():
    fixture = build_durability_fixture()
    workflow = fixture.workflow
    workflow["steps"][-1]["id"] = "finish"
    from vera.execution.durability_fixture import DurabilityFixture, DurabilityScenario
    boundaries = tuple(
        boundary for step in workflow["steps"]
        for boundary in (f"before:{step['id']}", f"after:{step['id']}"))
    scenarios = []
    for scenario in fixture.scenarios:
        boundary = scenario.crash_boundary.replace("finalize", "finish")
        resume = scenario.resume_after.replace("finalize", "finish")
        scenario_id = scenario.scenario_id.replace("finalize", "finish")
        scenarios.append(DurabilityScenario(
            scenario_id, scenario.kind, scenario.terminal_status,
            scenario.expected_events, crash_boundary=boundary, resume_after=resume,
            attempts=scenario.attempts, effect_receipts=scenario.effect_receipts,
            version_outcome=scenario.version_outcome))
    changed = DurabilityFixture(
        definition_revision=fixture.definition_revision,
        implementation_revision=fixture.implementation_revision,
        workflow_json=json.dumps(workflow),
        crash_boundaries=boundaries,
        scenarios=tuple(scenarios),
        requirements=fixture.requirements)
    with pytest.raises(ValueError, match="canonical LIB-15 step set"):
        compile_dbos_mapping(changed)


def test_mapping_records_reject_duplicate_and_unbounded_content():
    step = DBOSStepMapping("step", "DBOS.step")
    gap = DBOSMappingGap("gap", "steps.step", "explicit gap")
    values = dict(
        fixture_id="fixture", workflow_content_hash="sha256:" + "a" * 64,
        definition_revision="def", implementation_revision="impl",
        package=DBOS_PACKAGE, workflow_construct="DBOS.workflow",
        workflow_id_source="Run.id", application_version_source="impl",
        status_mapping=(("SUCCESS", "completed"),),
        event_sources=(("run.completed", "WorkflowStatus.SUCCESS"),), gaps=(gap,))
    with pytest.raises(ValueError, match="step mappings must be unique"):
        DBOSDurabilityMapping(steps=(step, step), **values)
    with pytest.raises(ValueError, match="bounded non-empty"):
        DBOSMappingGap("gap", "path", "x" * 1001)


def test_capability_returns_the_same_nonexecuting_manifest():
    from vera import capability_orchestration as orchestration
    result = asyncio.run(
        orchestration.cap_workflow_durability_dbos_mapping.__wrapped__())
    assert result == compile_dbos_mapping().to_dict()
    assert result["executes"] is False
    assert result["imports_runtime"] is False
    assert result["ready_for_execution"] is False
