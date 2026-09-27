"""Shared, non-operational Workflow IR plan and injected-runner boundary."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from typing import Any, Awaitable, Callable, Mapping

from .workflow_ir import WorkflowIRValidationError, export_native_dag, normalize_workflow

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


def compile_workflow_plan(workflow: Mapping[str, Any], *, adapter: str,
                          plan_schema: str, plan_id_prefix: str) -> dict[str, Any]:
    """Compile the common proven task/parallel/condition subset."""
    if not isinstance(workflow, Mapping):
        return {"ok": False, "adapter": adapter, "available": True,
                "gaps": [_gap("workflow", "invalid_workflow",
                              "workflow must be an object")], "executes": False}
    try:
        normalized = normalize_workflow(dict(workflow))
    except (TypeError, ValueError, WorkflowIRValidationError) as exc:
        return {"ok": False, "adapter": adapter, "available": True,
                "gaps": [_gap("workflow", "invalid_workflow", str(exc))],
                "executes": False}
    compatibility = export_native_dag(normalized, allow_lossy=False)
    if not compatibility["ok"]:
        return {"ok": False, "adapter": adapter, "available": True,
                "content_hash": normalized["content_hash"],
                "gaps": list(compatibility["gaps"]), "executes": False}

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    predecessors = [START]
    entrypoints: list[str] = []

    def task_node(step: Mapping[str, Any]) -> dict[str, Any]:
        node = {"id": step["id"], "kind": "task", "task": step["task"],
                "output": step.get("output", "")}
        if step.get("when"):
            node["guard"] = {"kind": "state_truthy", "key": step["when"]["key"]}
        return node

    for step in normalized["steps"]:
        current = ([task_node(step)] if step["type"] == "task"
                   else [task_node(branch) for branch in step["branches"]])
        ids = [node["id"] for node in current]
        if predecessors == [START]:
            entrypoints.extend(ids)
        edges.extend({"from": source, "to": target}
                     for source in predecessors for target in ids)
        nodes.extend(current)
        predecessors = ids

    authority = f"injected_{adapter}_runner"
    body = {"schema": plan_schema, "adapter": adapter,
            "workflow_hash": normalized["content_hash"],
            "ir_version": normalized["ir_version"], "entrypoints": entrypoints,
            "terminal_nodes": ([] if predecessors == [START] else predecessors),
            "nodes": nodes, "edges": edges, "execution_authority": authority}
    return {"ok": True, "adapter": adapter, "available": True,
            "plan": {"plan_id": _identity(plan_id_prefix, body), **body},
            "content_hash": normalized["content_hash"], "gaps": [],
            "executes": False}


class InjectedWorkflowRuntimeAdapter:
    """Validate an injected external runner without selecting or registering it."""

    def __init__(self, runner: Runner, *, adapter: str, display_name: str,
                 plan_schema: str, result_schema: str, plan_id_prefix: str) -> None:
        if not callable(runner):
            raise TypeError("runner must be callable")
        self._runner, self.adapter, self.display_name = runner, adapter, display_name
        self.plan_schema, self.result_schema = plan_schema, result_schema
        self.plan_id_prefix = plan_id_prefix

    def compile(self, workflow: Mapping[str, Any]) -> dict[str, Any]:
        return compile_workflow_plan(workflow, adapter=self.adapter,
                                     plan_schema=self.plan_schema,
                                     plan_id_prefix=self.plan_id_prefix)

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
        compiled = self.compile(workflow)
        if not compiled["ok"]:
            return {**compiled, "status": "blocked"}
        plan = compiled["plan"]
        response = self._runner(copy.deepcopy(plan), copy.deepcopy(dict(input_state)))
        if inspect.isawaitable(response):
            response = await response
        if not isinstance(response, Mapping):
            raise ValueError(f"{self.display_name} runner response must be a mapping")
        expected = {"plan_id": plan["plan_id"], "workflow_hash": plan["workflow_hash"],
                    "runtime_id": self.adapter}
        for key, value in expected.items():
            if response.get(key) != value:
                raise ValueError(f"{self.display_name} runner {key} does not match compiled plan")
        status = response.get("status")
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError(f"{self.display_name} runner returned an unsupported terminal status")
        if status == "succeeded":
            if not isinstance(response.get("result_state"), Mapping):
                raise ValueError(f"successful {self.display_name} result requires result_state")
            if response.get("error_code"):
                raise ValueError(f"successful {self.display_name} result cannot contain error_code")
            try:
                result_encoded = _canonical(response["result_state"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{self.display_name} result_state must be canonical JSON") from exc
            if len(result_encoded.encode("utf-8")) > 1_048_576:
                raise ValueError(f"{self.display_name} result_state exceeds the one MiB adapter boundary")
        elif response.get("result_state") is not None:
            raise ValueError(f"unsuccessful {self.display_name} result cannot contain result_state")
        error_code = response.get("error_code", "")
        if status == "failed" and (not isinstance(error_code, str)
                                    or not error_code.strip() or len(error_code) > 256):
            raise ValueError(f"failed {self.display_name} result requires a bounded error_code")
        if error_code and not isinstance(error_code, str):
            raise ValueError(f"{self.display_name} error_code must be a string")
        return {"schema": self.result_schema, "runtime_id": self.adapter,
                "plan_id": plan["plan_id"], "workflow_hash": plan["workflow_hash"],
                "status": status,
                "result_state": (copy.deepcopy(dict(response["result_state"]))
                                 if status == "succeeded" else None),
                "error_code": error_code,
                "runtime_execution": {"adapter": self.adapter,
                    "plan_id": plan["plan_id"], "workflow_hash": plan["workflow_hash"],
                    "execution_authority": f"injected_{self.adapter}_runner"}}
