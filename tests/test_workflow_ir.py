import copy

import pytest

from vera.execution.workflow_ir import (
    WorkflowIRValidationError,
    export_native_dag,
    import_native_dag,
    normalize_workflow,
    workflow_hash,
)


pytestmark = pytest.mark.critical


def test_native_dag_round_trip_is_stable_and_non_executing():
    dag = [
        ["alpha.read", "read"],
        [["beta.left", "left"], ["beta.right", "right"]],
        ["gamma.write", "saved", "CONDITION:approved"],
    ]
    imported = import_native_dag(dag, name="portable")
    assert imported["ok"] is True
    assert imported["executes"] is False
    assert imported["gaps"] == []
    assert export_native_dag(imported["workflow"])["dag"] == dag


def test_hash_is_stable_across_key_order_and_attached_hash():
    left = {"ir_version": "1.0", "name": "x", "steps": [
        {"id": "s0", "type": "task", "task": "alpha", "output": "value"}]}
    right = {"steps": [{"task": "alpha", "output": "value", "type": "task", "id": "s0"}],
             "name": "x", "ir_version": "1.0"}
    normalized = normalize_workflow(left)
    assert normalized["content_hash"] == workflow_hash(right)
    assert workflow_hash(normalized) == normalized["content_hash"]


def test_import_refuses_callable_condition_without_invoking_it():
    invoked = []

    def condition(_state):
        invoked.append(True)
        return True

    result = import_native_dag([["alpha", "value", condition]])
    assert result["ok"] is False
    assert result["workflow"] is None
    assert result["gaps"][0]["code"] == "unsupported_condition"
    assert invoked == []


def test_lossy_import_requires_explicit_opt_in_and_remains_descriptive():
    dag = [["alpha", "value", object()]]
    refused = import_native_dag(dag)
    allowed = import_native_dag(dag, allow_lossy=True)
    assert refused["workflow"] is None
    assert refused["ok"] is False
    assert allowed["ok"] is True
    assert allowed["lossy"] is True
    assert allowed["executes"] is False


def test_native_maps_round_trip_as_non_blocking_extensions():
    dag = [["alpha", "value", None, {"source": "input"}, {"value": "target"}]]
    imported = import_native_dag(dag)
    assert imported["ok"] is True
    assert {gap["code"] for gap in imported["gaps"]} == {"native_extension"}
    exported = export_native_dag(imported["workflow"])
    assert exported["ok"] is True
    assert exported["dag"] == dag


def test_export_refuses_unknown_extension_before_returning_dag():
    workflow = {"ir_version": "1.0", "steps": [{
        "id": "s0", "type": "task", "task": "alpha",
        "extensions": {"langgraph.interrupt": {"before": True}},
    }]}
    result = export_native_dag(workflow)
    assert result["ok"] is False
    assert result["dag"] is None
    assert result["gaps"][0]["code"] == "unsupported_extension"


def test_validation_rejects_duplicate_ids_and_does_not_mutate_input():
    workflow = {"ir_version": "1.0", "steps": [
        {"id": "same", "type": "task", "task": "alpha"},
        {"id": "same", "type": "task", "task": "beta"},
    ]}
    original = copy.deepcopy(workflow)
    with pytest.raises(WorkflowIRValidationError, match="duplicate step id"):
        normalize_workflow(workflow)
    assert workflow == original


def test_validation_rejects_unknown_fields_instead_of_silently_dropping_them():
    workflow = {"ir_version": "1.0", "steps": [{
        "id": "s0", "type": "task", "task": "alpha", "retry": {"max": 3},
    }]}
    with pytest.raises(WorkflowIRValidationError, match="unknown fields: retry"):
        normalize_workflow(workflow)


def test_export_reports_workflow_and_parallel_extensions_as_blocking_gaps():
    workflow = {"ir_version": "1.0", "extensions": {"temporal.schedule": "daily"},
                "steps": [{"id": "p0", "type": "parallel",
                           "extensions": {"langgraph.join": "all"},
                           "branches": [{"id": "b0", "type": "task", "task": "alpha"}]}]}
    result = export_native_dag(workflow)
    assert result["ok"] is False
    assert result["dag"] is None
    assert [gap["path"] for gap in result["gaps"]] == [
        "extensions.temporal.schedule", "steps[0].extensions.langgraph.join"]
