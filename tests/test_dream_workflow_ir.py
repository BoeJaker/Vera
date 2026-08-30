import copy

import pytest

from vera.dream.dream_workflow_ir import (
    DEFAULT_GENERIC_DREAM_PIPELINE,
    DREAM_EXECUTION_OWNER,
    DreamWorkflowContractError,
    compile_generic_dream_workflow,
    materialize_generic_dream_stage_plan,
)


pytestmark = pytest.mark.critical


def test_generic_pipeline_round_trips_through_authoritative_ir_plan():
    workflow = compile_generic_dream_workflow(DEFAULT_GENERIC_DREAM_PIPELINE)
    plan = materialize_generic_dream_stage_plan(workflow)

    assert plan["stages"] == list(DEFAULT_GENERIC_DREAM_PIPELINE)
    assert plan["execution_owner"] == DREAM_EXECUTION_OWNER
    assert plan["content_hash"] == workflow["content_hash"]
    assert plan["executes"] is False
    assert workflow["extensions"]["vera"]["native_semantics"] == [
        "cancellation", "early_exit", "hitl", "journaling", "artifacts",
        "progress", "cycle_persistence",
    ]


def test_filtered_preview_plan_preserves_stage_identity_and_order():
    stages = ["dream.stage.gather", "dream.stage.themes", "dream.stage.plan"]
    workflow = compile_generic_dream_workflow(stages)
    plan = materialize_generic_dream_stage_plan(workflow)

    assert plan["stages"] == stages


@pytest.mark.parametrize("stages", [
    ["dream.stage.plan", "dream.stage.gather"],
    ["dream.stage.gather", "dream.stage.gather"],
    ["dream.stage.gather", "dream.stage.third_party"],
])
def test_compiler_rejects_non_generic_or_ambiguous_stage_plans(stages):
    with pytest.raises(DreamWorkflowContractError):
        compile_generic_dream_workflow(stages)


def test_empty_filtered_plan_preserves_legacy_no_op_cycle_semantics():
    workflow = compile_generic_dream_workflow([])

    assert materialize_generic_dream_stage_plan(workflow)["stages"] == []


def test_materializer_rejects_task_provenance_tampering():
    workflow = compile_generic_dream_workflow(DEFAULT_GENERIC_DREAM_PIPELINE)
    tampered = copy.deepcopy(workflow)
    tampered["steps"][0]["extensions"]["vera"]["dream_stage"] = "dream.stage.plan"
    tampered.pop("content_hash")

    with pytest.raises(DreamWorkflowContractError, match="provenance"):
        materialize_generic_dream_stage_plan(tampered)


def test_materializer_rejects_another_execution_owner():
    workflow = compile_generic_dream_workflow(DEFAULT_GENERIC_DREAM_PIPELINE)
    tampered = copy.deepcopy(workflow)
    tampered["extensions"]["vera"]["execution_owner"] = "portable.core"
    tampered.pop("content_hash")

    with pytest.raises(DreamWorkflowContractError, match="execution owner"):
        materialize_generic_dream_stage_plan(tampered)
