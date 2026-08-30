"""Workflow IR contract for Dream's built-in generic pipeline.

This module is intentionally an adapter, not another executor.  Dream retains
ownership of stage invocation, cancellation, HITL, journals, artifacts and
cycle persistence; the normalized Workflow IR value is authoritative for the
ordered stage plan on the migrated path.
"""

from __future__ import annotations

from typing import Any, Iterable

from Vera.vera.execution.workflow_ir import normalize_workflow


DREAM_WORKFLOW_SCHEMA = "vera.dream.workflow/v1"
DREAM_EXECUTION_OWNER = "dream.native-stage-runner"
DEFAULT_GENERIC_DREAM_PIPELINE = (
    "dream.stage.gather",
    "dream.stage.themes",
    "dream.stage.plan",
    "dream.stage.execute",
    "dream.stage.synthesize",
    "dream.stage.deliver",
)


class DreamWorkflowContractError(ValueError):
    """The workflow cannot be executed by the narrow Dream adapter."""


def _validated_stages(stages: Iterable[str]) -> list[str]:
    result = list(stages)
    if len(result) != len(set(result)):
        raise DreamWorkflowContractError("generic Dream workflow stages must be unique")
    unknown = [stage for stage in result if stage not in DEFAULT_GENERIC_DREAM_PIPELINE]
    if unknown:
        raise DreamWorkflowContractError(
            "generic Dream workflow contains unsupported stages: " + ", ".join(unknown))
    positions = [DEFAULT_GENERIC_DREAM_PIPELINE.index(stage) for stage in result]
    if positions != sorted(positions):
        raise DreamWorkflowContractError(
            "generic Dream workflow must preserve the built-in stage order")
    return result


def compile_generic_dream_workflow(
    stages: Iterable[str],
) -> dict[str, Any]:
    """Compile the migrated built-in pipeline into normalized Workflow IR."""
    stage_plan = _validated_stages(stages)
    workflow = {
        "ir_version": "1.0",
        "name": "dream.generic",
        "description": "Built-in generic Dream cycle stage plan.",
        "steps": [
            {
                "id": f"dream-stage-{index:02d}",
                "type": "task",
                "task": stage,
                "output": f"stage_{index:02d}",
                "extensions": {
                    "vera": {
                        "dream_stage": stage,
                        "execution_owner": DREAM_EXECUTION_OWNER,
                    }
                },
            }
            for index, stage in enumerate(stage_plan, start=1)
        ],
        "extensions": {
            "vera": {
                "schema": DREAM_WORKFLOW_SCHEMA,
                "execution_owner": DREAM_EXECUTION_OWNER,
                "source": "dream.builtin-generic",
                "native_semantics": [
                    "cancellation",
                    "early_exit",
                    "hitl",
                    "journaling",
                    "artifacts",
                    "progress",
                    "cycle_persistence",
                ],
            }
        },
    }
    return normalize_workflow(workflow)


def materialize_generic_dream_stage_plan(workflow: dict[str, Any]) -> dict[str, Any]:
    """Validate an IR value and return the only stage plan Dream may execute.

    No permissive fallback exists: malformed provenance, non-task control flow,
    altered task/extension identity, or an unsupported stage fails closed.
    """
    normalized = normalize_workflow(workflow)
    vera = ((normalized.get("extensions") or {}).get("vera") or {})
    if vera.get("schema") != DREAM_WORKFLOW_SCHEMA:
        raise DreamWorkflowContractError("workflow is not a Dream workflow contract")
    if vera.get("execution_owner") != DREAM_EXECUTION_OWNER:
        raise DreamWorkflowContractError("workflow has an unsupported execution owner")
    if vera.get("source") != "dream.builtin-generic":
        raise DreamWorkflowContractError("workflow source is not the built-in generic pipeline")

    stages: list[str] = []
    step_ids: list[str] = []
    for step in normalized["steps"]:
        if step.get("type") != "task":
            raise DreamWorkflowContractError("generic Dream adapter accepts task steps only")
        stage = step.get("task")
        step_vera = ((step.get("extensions") or {}).get("vera") or {})
        if step_vera.get("dream_stage") != stage:
            raise DreamWorkflowContractError("Dream stage provenance does not match task identity")
        if step_vera.get("execution_owner") != DREAM_EXECUTION_OWNER:
            raise DreamWorkflowContractError("Dream step has an unsupported execution owner")
        stages.append(stage)
        step_ids.append(step["id"])

    return {
        "schema": DREAM_WORKFLOW_SCHEMA,
        "ir_version": normalized["ir_version"],
        "content_hash": normalized["content_hash"],
        "execution_owner": DREAM_EXECUTION_OWNER,
        "stages": _validated_stages(stages),
        "step_ids": step_ids,
        "executes": False,
    }
