import asyncio
import sys

import pytest

from vera.execution.dbos_mapping import compile_dbos_mapping
from vera.execution.durability_fixture import build_durability_fixture
from vera.execution.temporal_mapping import (
    DBOSGapDisposition, TEMPORAL_PACKAGE, TemporalDecisionRequirement, TemporalDurabilityMapping,
    TemporalMappingGap, TemporalStepMapping, compile_temporal_mapping)


pytestmark = pytest.mark.critical


def test_manifest_is_stable_pinned_and_import_free():
    before = {name for name in sys.modules
              if name == "temporalio" or name.startswith("temporalio.")}
    first = compile_temporal_mapping()
    assert first == compile_temporal_mapping()
    assert first.package == TEMPORAL_PACKAGE == "temporalio==1.32.0"
    assert first.mapping_id.startswith("temporalmap_")
    after = {name for name in sys.modules
             if name == "temporalio" or name.startswith("temporalio.")}
    assert after == before == set()
    assert first.to_dict()["executes"] is False
    assert first.to_dict()["imports_runtime"] is False


def test_manifest_binds_exact_fixture_and_dbos_comparison():
    fixture = build_durability_fixture()
    dbos = compile_dbos_mapping(fixture)
    mapping = compile_temporal_mapping(fixture, dbos)
    assert mapping.fixture_id == fixture.fixture_id
    assert mapping.workflow_content_hash == fixture.workflow["content_hash"]
    assert mapping.dbos_mapping_id == dbos.mapping_id
    other = compile_dbos_mapping()
    object.__setattr__(other, "fixture_id", "fixture_mismatch")
    with pytest.raises(ValueError, match="same LIB-15 fixture"):
        compile_temporal_mapping(fixture, other)


def test_documented_constructs_preserve_fixture_contracts():
    mapping = compile_temporal_mapping()
    steps = {item.step_id: item for item in mapping.steps}
    assert mapping.workflow_construct == "workflow.defn|workflow.run"
    assert steps["durable_wait"].construct == "workflow.sleep"
    assert steps["durable_wait"].input_bindings == ("wake_at",)
    assert dict(steps["retryable_step"].options) == {
        "initial_interval_seconds": 1,
        "maximum_attempts": 3,
        "start_to_close_timeout_seconds": 30,
    }
    effect = steps["idempotent_effect"]
    assert effect.input_bindings == ("request_id",)
    assert effect.output_binding == "receipt"
    assert "workflow.patched" in mapping.versioning_sources


def test_statuses_keep_timeout_distinct_and_events_disclose_projection():
    mapping = compile_temporal_mapping()
    assert dict(mapping.status_mapping)["TIMED_OUT"] == "timed_out"
    events = dict(mapping.event_sources)
    assert events["run.timed_out"] == "WorkflowExecutionTimedOut"
    assert "verified_artifact" in events["effect.receipt.recorded"]


def test_decision_gate_defers_without_live_dbos_failure_evidence():
    result = compile_temporal_mapping().to_dict()
    assert result["recommendation"] == "defer"
    assert result["decision_ready"] is False
    assert result["live_pilot_approved"] is False
    assert result["ready_for_execution"] is False
    requirements = {item["requirement"]: item
                    for item in result["decision_requirements"]}
    assert set(requirements) == {
        "cross_service_workers", "long_lived_history",
        "versioned_worker_routing", "signals_updates", "child_workflows",
        "schedules", "retention_visibility", "in_flight_migration",
    }
    assert all(item["disposition"] == "requires_live_evidence"
               for item in requirements.values())
    assert all("not" in item["dbos_evidence"].lower()
               for item in requirements.values())


def test_every_dbos_blocker_remains_visible_to_the_decision():
    result = compile_temporal_mapping().to_dict()
    dbos_codes = {gap.code for gap in compile_dbos_mapping().gaps}
    assert dbos_codes == {
        "absolute_wake_translation", "timeout_cancel_ambiguity",
        "compatible_version_plan_missing", "effect_receipt_atomicity_unproven",
        "run_event_projection_unproven", "large_result_boundary_unmapped",
        "cancellation_boundary_unproven",
    }
    dispositions = {item["gap_code"]: item
                    for item in result["dbos_gap_dispositions"]}
    assert set(dispositions) == dbos_codes
    assert all(item["disposition"] == "requires_live_evidence"
               for item in dispositions.values())
    assert result["dbos_mapping_id"] == compile_dbos_mapping().mapping_id


def test_records_reject_duplicates_and_unknown_dispositions():
    step = TemporalStepMapping("step", "workflow.execute_activity")
    gap = TemporalMappingGap("gap", "steps.step", "explicit gap")
    requirement = TemporalDecisionRequirement(
        "requirement", "candidate", "evidence absent")
    disposition = DBOSGapDisposition("dbos_gap", "candidate relation")
    values = dict(
        fixture_id="fixture", workflow_content_hash="sha256:" + "a" * 64,
        definition_revision="def", implementation_revision="impl",
        dbos_mapping_id="dbosmap_" + "b" * 64, package=TEMPORAL_PACKAGE,
        workflow_construct="workflow.defn", workflow_id_source="Run.id",
        versioning_sources=("workflow.patched",),
        status_mapping=(("COMPLETED", "completed"),),
        event_sources=(("run.completed", "WorkflowExecutionCompleted"),),
        gaps=(gap,), decision_requirements=(requirement,),
        dbos_gap_dispositions=(disposition,))
    with pytest.raises(ValueError, match="step mappings must be unique"):
        TemporalDurabilityMapping(steps=(step, step), **values)
    with pytest.raises(ValueError, match="unsupported decision disposition"):
        TemporalDecisionRequirement("requirement", "candidate", "evidence", "yes")


def test_capability_returns_same_nonexecuting_decision_manifest():
    from vera import capability_orchestration as orchestration
    result = asyncio.run(
        orchestration.cap_workflow_durability_temporal_paper.__wrapped__())
    assert result == compile_temporal_mapping().to_dict()
    assert result["live_pilot_approved"] is False
