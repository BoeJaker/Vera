"""Loss-aware Workflow IR compiler for an injected Temporal runtime."""

from __future__ import annotations

from typing import Any, Mapping

from .workflow_runtime_plan import InjectedWorkflowRuntimeAdapter, Runner, compile_workflow_plan

PLAN_SCHEMA = "vera.temporal-workflow-plan/v1"
RESULT_SCHEMA = "vera.temporal-workflow-result/v1"


def compile_temporal_workflow(workflow: Mapping[str, Any]) -> dict[str, Any]:
    return compile_workflow_plan(workflow, adapter="temporal", plan_schema=PLAN_SCHEMA,
                                 plan_id_prefix="tmplan_")


class TemporalWorkflowRuntimeAdapter(InjectedWorkflowRuntimeAdapter):
    def __init__(self, runner: Runner) -> None:
        super().__init__(runner, adapter="temporal", display_name="Temporal",
                         plan_schema=PLAN_SCHEMA, result_schema=RESULT_SCHEMA,
                         plan_id_prefix="tmplan_")
