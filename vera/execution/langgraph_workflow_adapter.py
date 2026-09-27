"""Loss-aware Workflow IR compiler for an injected LangGraph runtime.

This module never imports LangGraph and does not register an execution path.
It creates a content-addressed plan for the exact Workflow IR subset whose
semantics Vera can currently prove, plus an injected seam used by conformance
tests and a future separately gated operational bridge.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from typing import Any, Awaitable, Callable, Mapping

from .workflow_ir import (
    WorkflowIRValidationError,
    export_native_dag,
    normalize_workflow,
)


PLAN_SCHEMA = "vera.langgraph-workflow-plan/v1"
RESULT_SCHEMA = "vera.langgraph-workflow-result/v1"
START = "__start__"
Runner = Callable[[Mapping[str, Any], Mapping[str, Any]],
                  Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _gap(path: str, code: str, detail: str) -> dict[str, str]:
    return {"path": path, "code": code, "detail": detail}


def compile_langgraph_workflow(workflow: Mapping[str, Any]) -> dict[str, Any]:
    """Compile the proven task/parallel/condition subset without executing it."""
    if not isinstance(workflow, Mapping):
        return {"ok": False, "adapter": "langgraph", "available": True,
                "gaps": [_gap("workflow", "invalid_workflow",
                              "workflow must be an object")],
                "executes": False}
    try:
        normalized = normalize_workflow(dict(workflow))
    except (TypeError, ValueError, WorkflowIRValidationError) as exc:
        return {"ok": False, "adapter": "langgraph", "available": True,
                "gaps": [_gap("workflow", "invalid_workflow", str(exc))],
                "executes": False}

    # Native DAG export is the existing loss oracle for precisely the subset
    # this first compiler claims. Never silently reinterpret richer contracts.
    compatibility = export_native_dag(normalized, allow_lossy=False)
    if not compatibility["ok"]:
        return {"ok": False, "adapter": "langgraph", "available": True,
                "content_hash": normalized["content_hash"],
                "gaps": list(compatibility["gaps"]), "executes": False}

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    predecessors = [START]
    entrypoints: list[str] = []

    def task_node(step: Mapping[str, Any]) -> dict[str, Any]:
        node = {"id": step["id"], "kind": "task", "task": step["task"],
                "output": step.get("output", "")}
        when = step.get("when")
        if when:
            node["guard"] = {"kind": "state_truthy", "key": when["key"]}
        return node

    for step in normalized["steps"]:
        current = ([task_node(step)] if step["type"] == "task"
                   else [task_node(branch) for branch in step["branches"]])
        ids = [node["id"] for node in current]
        if predecessors == [START]:
            entrypoints.extend(ids)
        for source in predecessors:
            for target in ids:
                edges.append({"from": source, "to": target})
        nodes.extend(current)
        predecessors = ids

    plan_body = {
        "schema": PLAN_SCHEMA,
        "adapter": "langgraph",
        "workflow_hash": normalized["content_hash"],
        "ir_version": normalized["ir_version"],
        "entrypoints": entrypoints,
        "terminal_nodes": ([] if predecessors == [START] else predecessors),
        "nodes": nodes,
        "edges": edges,
        "execution_authority": "injected_langgraph_runner",
    }
    return {"ok": True, "adapter": "langgraph", "available": True,
            "plan": {"plan_id": _identity("lgplan_", plan_body), **plan_body},
            "content_hash": normalized["content_hash"], "gaps": [],
            "executes": False}


class LangGraphWorkflowRuntimeAdapter:
    """Validate an injected runner against an exact compiled-plan identity."""

    def __init__(self, runner: Runner) -> None:
        if not callable(runner):
            raise TypeError("runner must be callable")
        self._runner = runner

    async def run(self, workflow: Mapping[str, Any],
                  input_state: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(input_state, Mapping):
            raise TypeError("input_state must be a mapping")
        try:
            encoded = _canonical(input_state)
        except (TypeError, ValueError) as exc:
            raise ValueError("input_state must be canonical JSON") from exc
        if len(encoded.encode("utf-8")) > 1_048_576:
            raise ValueError("input_state exceeds the one MiB adapter boundary")

        compiled = compile_langgraph_workflow(workflow)
        if not compiled["ok"]:
            return {**compiled, "status": "blocked"}
        plan = compiled["plan"]
        response = self._runner(copy.deepcopy(plan), copy.deepcopy(dict(input_state)))
        if inspect.isawaitable(response):
            response = await response
        if not isinstance(response, Mapping):
            raise ValueError("LangGraph runner response must be a mapping")
        expected = {"plan_id": plan["plan_id"],
                    "workflow_hash": plan["workflow_hash"],
                    "runtime_id": "langgraph"}
        for key, value in expected.items():
            if response.get(key) != value:
                raise ValueError(f"LangGraph runner {key} does not match compiled plan")
        status = response.get("status")
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("LangGraph runner returned an unsupported terminal status")
        if status == "succeeded":
            if not isinstance(response.get("result_state"), Mapping):
                raise ValueError("successful LangGraph result requires result_state")
            if response.get("error_code"):
                raise ValueError("successful LangGraph result cannot contain error_code")
            try:
                result_encoded = _canonical(response["result_state"])
            except (TypeError, ValueError) as exc:
                raise ValueError("LangGraph result_state must be canonical JSON") from exc
            if len(result_encoded.encode("utf-8")) > 1_048_576:
                raise ValueError("LangGraph result_state exceeds the one MiB adapter boundary")
        elif response.get("result_state") is not None:
            raise ValueError("unsuccessful LangGraph result cannot contain result_state")
        error_code = response.get("error_code", "")
        if status == "failed" and (not isinstance(error_code, str)
                                    or not error_code.strip() or len(error_code) > 256):
            raise ValueError("failed LangGraph result requires a bounded error_code")
        if error_code and not isinstance(error_code, str):
            raise ValueError("LangGraph error_code must be a string")

        return {
            "schema": RESULT_SCHEMA,
            "runtime_id": "langgraph",
            "plan_id": plan["plan_id"],
            "workflow_hash": plan["workflow_hash"],
            "status": status,
            "result_state": (copy.deepcopy(dict(response["result_state"]))
                             if status == "succeeded" else None),
            "error_code": error_code,
            "runtime_execution": {
                "adapter": "langgraph", "plan_id": plan["plan_id"],
                "workflow_hash": plan["workflow_hash"],
                "execution_authority": "injected_langgraph_runner",
            },
        }
