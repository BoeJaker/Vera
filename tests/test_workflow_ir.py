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
        "id": "s0", "type": "task", "task": "alpha", "mystery": {"max": 3},
    }]}
    with pytest.raises(WorkflowIRValidationError, match="unknown fields: mystery"):
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


def test_typed_references_are_validated_but_never_resolved():
    workflow = {"ir_version": "1.0", "steps": [{
        "id": "s0", "type": "task", "task": "storage.write",
        "bindings": {
            "payload": {"kind": "artifact", "value": "sha256:opaque", "provider": "s3"},
            "credential": {"kind": "secret", "value": "secret://storage/key"},
            "count": {"kind": "literal", "value": 3},
        },
    }]}
    normalized = normalize_workflow(workflow)
    assert normalized["steps"][0]["bindings"] == workflow["steps"][0]["bindings"]
    exported = export_native_dag(normalized)
    assert exported["ok"] is False
    assert exported["dag"] is None
    assert exported["gaps"][0]["path"] == "steps[0].bindings"


@pytest.mark.parametrize("reference", [
    {"kind": "secret", "value": ""},
    {"kind": "secret", "value": "plaintext-not-a-reference"},
    {"kind": "unknown", "value": "x"},
    {"kind": "record"},
    {"kind": "state", "value": 4},
])
def test_invalid_references_fail_before_adapter_output(reference):
    workflow = {"ir_version": "1.0", "steps": [{
        "id": "s0", "type": "task", "task": "alpha", "bindings": {"x": reference}}]}
    with pytest.raises(WorkflowIRValidationError):
        normalize_workflow(workflow)


def test_execution_contracts_are_typed_and_block_native_export():
    workflow = {"ir_version": "1.0", "steps": [{
        "id": "s0", "type": "task", "task": "external.call",
        "retry": {"max_attempts": 3, "backoff_seconds": 0.5, "owner": "runtime"},
        "timeout": {"seconds": 10, "owner": "task"},
        "idempotency": {"key": {"kind": "state", "value": "request_id"}, "owner": "runtime"},
        "effects": [{"kind": "network", "target": "api.example", "mode": "write"}],
    }]}
    normalized = normalize_workflow(workflow)
    result = export_native_dag(normalized)
    assert result["ok"] is False
    assert result["dag"] is None
    assert [gap["path"] for gap in result["gaps"]] == [
        "steps[0].retry", "steps[0].timeout", "steps[0].idempotency", "steps[0].effects"]


def test_workflow_ports_hold_explicit_schemas_and_affect_hash():
    workflow = {"ir_version": "1.0",
                "inputs": {"query": {"schema": {"type": "string"}, "required": True}},
                "outputs": {"report": {"schema": {"type": "object"}}},
                "steps": [{"id": "s0", "type": "task", "task": "report.create"}]}
    normalized = normalize_workflow(workflow)
    changed = copy.deepcopy(workflow)
    changed["inputs"]["query"]["required"] = False
    assert normalized["content_hash"] != normalize_workflow(changed)["content_hash"]
    exported = export_native_dag(normalized)
    assert exported["ok"] is False
    assert [gap["path"] for gap in exported["gaps"]] == ["inputs", "outputs"]


def test_invalid_port_descriptor_is_rejected():
    workflow = {"ir_version": "1.0", "inputs": {"query": {"type": "string"}}, "steps": []}
    with pytest.raises(WorkflowIRValidationError, match="inputs.query has unsupported fields"):
        normalize_workflow(workflow)


@pytest.mark.parametrize("field,value", [("backoff_seconds", float("nan")),
                                          ("backoff_seconds", float("inf"))])
def test_retry_rejects_non_finite_numbers(field, value):
    workflow = {"ir_version": "1.0", "steps": [{
        "id": "s0", "type": "task", "task": "alpha",
        "retry": {"max_attempts": 2, field: value}}]}
    with pytest.raises(WorkflowIRValidationError, match="must be non-negative"):
        normalize_workflow(workflow)
