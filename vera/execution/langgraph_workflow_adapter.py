"""Loss-aware Workflow IR compiler for an injected LangGraph runtime."""

from __future__ import annotations

from typing import Any, Mapping

from .workflow_runtime_plan import (
    InjectedWorkflowRuntimeAdapter,
    Runner,
    START,
    compile_workflow_plan,
)

PLAN_SCHEMA = "vera.langgraph-workflow-plan/v1"
RESULT_SCHEMA = "vera.langgraph-workflow-result/v1"


def compile_langgraph_workflow(workflow: Mapping[str, Any]) -> dict[str, Any]:
    return compile_workflow_plan(workflow, adapter="langgraph", plan_schema=PLAN_SCHEMA,
                                 plan_id_prefix="lgplan_")


class LangGraphWorkflowRuntimeAdapter(InjectedWorkflowRuntimeAdapter):
    def __init__(self, runner: Runner) -> None:
        super().__init__(runner, adapter="langgraph", display_name="LangGraph",
                         plan_schema=PLAN_SCHEMA, result_schema=RESULT_SCHEMA,
                         plan_id_prefix="lgplan_")
