import copy

import pytest

from vera.execution.workflow_ir import (
    IR_VERSION,
    WorkflowIRValidationError,
    adapter_profiles,
    analyze_adapter,
    export_native_dag,
    import_native_dag,
    migrate_workflow,
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


def test_structural_ir_validates_nested_ids_and_stays_descriptive():
    workflow = {"ir_version": "1.0", "steps": [
        {"id": "sub", "type": "subworkflow",
         "workflow": {"kind": "artifact", "value": "sha256:child"},
         "bindings": {"query": {"kind": "state", "value": "query"}}, "output": "child"},
        {"id": "choose", "type": "choice", "cases": [{
            "when": {"kind": "state", "value": "approved"},
            "steps": [{"id": "approved_task", "type": "task", "task": "publish"}],
        }], "default": [{"id": "fallback", "type": "task", "task": "draft"}]},
        {"id": "map", "type": "map", "items": {"kind": "state", "value": "records"},
         "max_concurrency": 4, "body": [{"id": "map_task", "type": "task", "task": "enrich"}]},
        {"id": "reduce", "type": "reduce", "items": {"kind": "state", "value": "enriched"},
         "initial": {"kind": "literal", "value": {}},
         "reducer": {"id": "reduce_task", "type": "task", "task": "merge"}, "output": "report"},
    ]}
    normalized = normalize_workflow(workflow)
    assert normalized["content_hash"].startswith("sha256:")
    exported = export_native_dag(normalized)
    assert exported["ok"] is False
    assert exported["dag"] is None
    assert [gap["code"] for gap in exported["gaps"]] == ["unsupported_structure"] * 4
    lossy = export_native_dag(normalized, allow_lossy=True)
    assert lossy["ok"] is True
    assert lossy["dag"] == []
    assert lossy["executes"] is False


def test_structural_ir_rejects_duplicate_nested_ids():
    workflow = {"ir_version": "1.0", "steps": [{
        "id": "choice", "type": "choice", "cases": [{
            "when": {"kind": "literal", "value": True},
            "steps": [{"id": "duplicate", "type": "task", "task": "alpha"}],
        }], "default": [{"id": "duplicate", "type": "task", "task": "beta"}],
    }]}
    with pytest.raises(WorkflowIRValidationError, match="duplicate step id"):
        normalize_workflow(workflow)


def test_subworkflow_requires_an_opaque_workflow_reference():
    workflow = {"ir_version": "1.0", "steps": [{
        "id": "sub", "type": "subworkflow",
        "workflow": {"kind": "literal", "value": {"steps": []}},
    }]}
    with pytest.raises(WorkflowIRValidationError, match="artifact or record"):
        normalize_workflow(workflow)


def test_non_json_literal_fails_as_ir_validation_not_encoder_error():
    workflow = {"ir_version": "1.0", "steps": [{
        "id": "map", "type": "map", "items": {"kind": "literal", "value": object()},
        "body": [{"id": "task", "type": "task", "task": "alpha"}],
    }]}
    with pytest.raises(WorkflowIRValidationError, match="not canonical JSON"):
        normalize_workflow(workflow)


def test_operational_contracts_are_validated_and_block_native_export():
    workflow = {"ir_version": "1.0",
                "schedule": {"kind": "cron", "expression": "0 2 * * *",
                             "timezone": "Europe/London", "owner": "external"},
                "resources": {"cpu": 2, "memory_mb": 1024, "accelerator": "cuda",
                              "max_concurrency": 3},
                "providers": ["ollama:local", "langgraph:remote"],
                "steps": [{"id": "publish", "type": "task", "task": "publish.run",
                           "approval": {"required": True, "policy": "release",
                                        "timeout_seconds": 3600},
                           "compensation": {"task": "publish.rollback",
                                            "on": ["failure", "cancel"]}}]}
    normalized = normalize_workflow(workflow)
    result = export_native_dag(normalized)
    assert result["ok"] is False
    assert result["dag"] is None
    assert [gap["path"] for gap in result["gaps"]] == [
        "schedule", "resources", "providers",
        "steps[0].approval", "steps[0].compensation"]
    assert result["executes"] is False


@pytest.mark.parametrize("schedule", [
    {"kind": "cron"},
    {"kind": "interval", "seconds": 0},
    {"kind": "event", "event": "topic", "owner": "vera-guessed"},
])
def test_invalid_schedule_contracts_are_rejected(schedule):
    with pytest.raises(WorkflowIRValidationError):
        normalize_workflow({"ir_version": "1.0", "schedule": schedule, "steps": []})


def test_duplicate_provider_requirements_are_rejected():
    with pytest.raises(WorkflowIRValidationError, match="duplicates"):
        normalize_workflow({"ir_version": "1.0", "providers": ["onnx", "onnx"], "steps": []})


def test_approval_cannot_be_present_but_optional():
    workflow = {"ir_version": "1.0", "steps": [{
        "id": "write", "type": "task", "task": "external.write",
        "approval": {"required": False}}]}
    with pytest.raises(WorkflowIRValidationError, match="required must be true"):
        normalize_workflow(workflow)


def test_current_version_migration_is_hash_stable_and_non_executing():
    workflow = {"steps": [{"task": "alpha", "type": "task", "id": "s0"}],
                "ir_version": "1.0"}
    result = migrate_workflow(workflow)
    assert result["ok"] is True
    assert result["version_changed"] is False
    assert result["migrations"] == []
    assert result["workflow"]["content_hash"] == normalize_workflow(workflow)["content_hash"]
    assert result["executes"] is False


def test_migration_refuses_unknown_source_and_target_versions():
    source = migrate_workflow({"ir_version": "0.9", "steps": []})
    target = migrate_workflow({"ir_version": "1.0", "steps": []}, target_version="2.0")
    assert source["error"] == "unsupported_source_version"
    assert target["error"] == "unsupported_target_version"
    assert source["supported_versions"] == target["supported_versions"] == ["1.0"]


def test_adapter_profiles_distinguish_offline_compilers_from_uninstalled_runtimes():
    profiles = adapter_profiles()
    assert profiles["executes"] is False
    assert profiles["profiles"]["portable.core"]["available"] is True
    assert profiles["profiles"]["portable.core"]["executable"] is False
    assert profiles["profiles"]["langgraph"]["available"] is True
    assert profiles["profiles"]["langgraph"]["executable"] is False
    assert profiles["profiles"]["langgraph"]["supports"] == [
        "tasks", "parallel", "conditions"]
    assert profiles["profiles"]["temporal"]["supports"] == []


def test_langgraph_adapter_analysis_uses_offline_compiler_without_runtime_import():
    workflow = {"ir_version": "1.0", "steps": [{
        "id": "s0", "type": "task", "task": "alpha"}]}
    result = analyze_adapter(workflow, adapter="langgraph")
    assert result["ok"] is True
    assert result["available"] is True
    assert result["gaps"] == []
    assert result["executes"] is False


def test_native_adapter_analysis_reuses_loss_aware_gap_contract():
    workflow = {"ir_version": "1.0", "schedule": {
        "kind": "interval", "seconds": 60}, "steps": []}
    result = analyze_adapter(workflow, adapter="vera.native_dag")
    assert result["ok"] is False
    assert result["available"] is True
    assert result["gaps"][0]["path"] == "schedule"


def test_unknown_adapter_profile_is_explicit():
    result = analyze_adapter({"ir_version": "1.0", "steps": []}, adapter="openclaw")
    assert result["ok"] is False
    assert result["gaps"][0]["code"] == "unknown_adapter"


def test_variables_bounded_loop_and_join_are_hash_stable_and_non_executing():
    workflow = {
        "ir_version": IR_VERSION,
        "name": "bounded convergence",
        "variables": {
            "cursor": {
                "schema": {"type": "integer"},
                "initial": {"kind": "literal", "value": 0},
                "mutable": True,
            },
        },
        "steps": [
            {
                "id": "iterate",
                "type": "loop",
                "condition": {"kind": "state", "value": "continue"},
                "max_iterations": 10,
                "body": [{"id": "advance", "type": "task", "task": "cursor.advance"}],
                "output": "iterations",
            },
            {
                "id": "gather",
                "type": "join",
                "inputs": [
                    {"kind": "state", "value": "left"},
                    {"kind": "state", "value": "right"},
                ],
                "strategy": "quorum",
                "quorum": 2,
                "output": "joined",
            },
        ],
    }

    normalized = normalize_workflow(workflow)
    reordered = {"steps": workflow["steps"], "variables": workflow["variables"],
                 "name": workflow["name"], "ir_version": IR_VERSION}
    assert normalize_workflow(reordered)["content_hash"] == normalized["content_hash"]
    portable = analyze_adapter(workflow, adapter="portable.core")
    assert portable == {
        "ok": True, "adapter": "portable.core", "available": True,
        "content_hash": normalized["content_hash"], "gaps": [], "executes": False,
    }
    features = adapter_profiles()["profiles"]["portable.core"]["supports"]
    assert {"variables", "loop", "join"}.issubset(features)

    native = export_native_dag(workflow)
    assert native["ok"] is False and native["dag"] is None
    assert {(gap["path"], gap["code"]) for gap in native["gaps"]} >= {
        ("variables", "unsupported_contract"),
        ("steps[0]", "unsupported_structure"),
        ("steps[1]", "unsupported_structure"),
    }


@pytest.mark.parametrize("step,error", [
    ({"id": "loop", "type": "loop",
      "condition": {"kind": "state", "value": "again"},
      "body": [{"id": "body", "type": "task", "task": "x"}]},
     "max_iterations must be a positive integer"),
    ({"id": "join", "type": "join",
      "inputs": [{"kind": "state", "value": "one"}],
      "strategy": "quorum", "quorum": 2},
     "quorum must be between 1 and the input count"),
    ({"id": "join", "type": "join",
      "inputs": [{"kind": "state", "value": "one"}],
      "strategy": "all", "quorum": 1},
     "quorum is only valid for quorum strategy"),
    ({"id": "join", "type": "join",
      "inputs": [{"kind": "state", "value": "same"},
                 {"kind": "state", "value": "same"}]},
     "inputs must not contain duplicates"),
])
def test_loop_and_join_fail_closed_before_adapter_output(step, error):
    with pytest.raises(WorkflowIRValidationError, match=error):
        normalize_workflow({"ir_version": IR_VERSION, "steps": [step]})


def test_workflow_variables_are_typed_references_not_resolved_values():
    workflow = {
        "ir_version": IR_VERSION,
        "variables": {
            "credential": {
                "schema": {"type": "string"},
                "initial": {"kind": "secret", "value": "plain-text"},
            },
        },
        "steps": [],
    }
    with pytest.raises(WorkflowIRValidationError, match="opaque secret:// reference"):
        normalize_workflow(workflow)
