"""Runtime-neutral Workflow IR and loss-aware adapters for Vera's native DAG.

This module only describes workflows.  It deliberately has no execution entry
point: native engines remain authoritative until a later, separately gated
integration chooses an adapter and authorizes its effects.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any


IR_VERSION = "1.0"
_STEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class WorkflowIRValidationError(ValueError):
    """The supplied IR is structurally invalid."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def workflow_hash(workflow: dict[str, Any]) -> str:
    """Return a stable content hash, excluding any previously attached hash."""
    material = copy.deepcopy(workflow)
    material.pop("content_hash", None)
    return "sha256:" + hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()


def normalize_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    """Validate and return a detached canonical-order-independent IR value."""
    if not isinstance(workflow, dict):
        raise WorkflowIRValidationError("workflow must be an object")
    if workflow.get("ir_version") != IR_VERSION:
        raise WorkflowIRValidationError(f"unsupported ir_version: {workflow.get('ir_version')!r}")
    allowed_workflow = {"ir_version", "name", "description", "inputs", "outputs",
                        "steps", "extensions", "content_hash"}
    unknown_workflow = sorted(set(workflow) - allowed_workflow)
    if unknown_workflow:
        raise WorkflowIRValidationError(
            "unknown workflow fields: " + ", ".join(unknown_workflow))
    for key in ("name", "description"):
        if key in workflow and not isinstance(workflow[key], str):
            raise WorkflowIRValidationError(f"{key} must be a string")
    for key in ("inputs", "outputs", "extensions"):
        if key in workflow and not isinstance(workflow[key], dict):
            raise WorkflowIRValidationError(f"{key} must be an object")
    steps = workflow.get("steps")
    if not isinstance(steps, list):
        raise WorkflowIRValidationError("steps must be an array")
    result = copy.deepcopy(workflow)
    seen: set[str] = set()

    def check_step(step: Any, path: str) -> None:
        if not isinstance(step, dict):
            raise WorkflowIRValidationError(f"{path} must be an object")
        step_id = step.get("id")
        if not isinstance(step_id, str) or not _STEP_ID.fullmatch(step_id):
            raise WorkflowIRValidationError(f"{path}.id is invalid")
        if step_id in seen:
            raise WorkflowIRValidationError(f"duplicate step id: {step_id}")
        seen.add(step_id)
        kind = step.get("type")
        if kind == "task":
            unknown = sorted(set(step) - {"id", "type", "task", "output", "when", "extensions"})
            if unknown:
                raise WorkflowIRValidationError(
                    f"{path} has unknown fields: {', '.join(unknown)}")
            if not isinstance(step.get("task"), str) or not step["task"].strip():
                raise WorkflowIRValidationError(f"{path}.task is required")
            if "output" in step and not isinstance(step["output"], str):
                raise WorkflowIRValidationError(f"{path}.output must be a string")
            if "extensions" in step and not isinstance(step["extensions"], dict):
                raise WorkflowIRValidationError(f"{path}.extensions must be an object")
            when = step.get("when")
            if when is not None and (not isinstance(when, dict)
                                     or when.get("kind") != "state_truthy"
                                     or not isinstance(when.get("key"), str)
                                     or not when["key"]):
                raise WorkflowIRValidationError(f"{path}.when is unsupported")
        elif kind == "parallel":
            unknown = sorted(set(step) - {"id", "type", "branches", "extensions"})
            if unknown:
                raise WorkflowIRValidationError(
                    f"{path} has unknown fields: {', '.join(unknown)}")
            if "extensions" in step and not isinstance(step["extensions"], dict):
                raise WorkflowIRValidationError(f"{path}.extensions must be an object")
            branches = step.get("branches")
            if not isinstance(branches, list) or not branches:
                raise WorkflowIRValidationError(f"{path}.branches must be non-empty")
            for index, branch in enumerate(branches):
                check_step(branch, f"{path}.branches[{index}]")
                if branch.get("type") != "task":
                    raise WorkflowIRValidationError(
                        f"{path}.branches[{index}] must be a task in IR {IR_VERSION}")
        else:
            raise WorkflowIRValidationError(f"{path}.type is unsupported: {kind!r}")

    for index, step in enumerate(steps):
        check_step(step, f"steps[{index}]")
    result["content_hash"] = workflow_hash(result)
    return result


def _gap(path: str, code: str, detail: str, *, blocking: bool = True) -> dict[str, Any]:
    return {"path": path, "code": code, "detail": detail, "blocking": blocking}


def import_native_dag(dag: Any, *, name: str = "", allow_lossy: bool = False) -> dict[str, Any]:
    """Project a native DAG array into IR, reporting every semantic gap."""
    if not isinstance(dag, list):
        return {"ok": False, "workflow": None,
                "gaps": [_gap("dag", "invalid_dag", "native DAG must be an array")]}
    gaps: list[dict[str, Any]] = []

    def task(node: Any, path: str, step_id: str) -> dict[str, Any] | None:
        if not isinstance(node, list) or len(node) < 2 or not isinstance(node[0], str):
            gaps.append(_gap(path, "invalid_node", "expected [capability, output, ...]"))
            return None
        if len(node) > 5:
            gaps.append(_gap(path, "extra_node_fields", "fields after output_map are not portable"))
        result: dict[str, Any] = {"id": step_id, "type": "task", "task": node[0]}
        if node[1] not in (None, ""):
            if not isinstance(node[1], str):
                gaps.append(_gap(path + "[1]", "invalid_output", "output key must be a string"))
            else:
                result["output"] = node[1]
        condition = node[2] if len(node) > 2 else None
        if isinstance(condition, str) and condition.startswith("CONDITION:") and condition[10:]:
            result["when"] = {"kind": "state_truthy", "key": condition[10:]}
        elif condition is not None:
            gaps.append(_gap(path + "[2]", "unsupported_condition",
                             "only CONDITION:<state-key> is portable"))
        for index, key in ((3, "input_map"), (4, "output_map")):
            if len(node) > index and node[index] not in (None, {}):
                if isinstance(node[index], dict):
                    result.setdefault("extensions", {})[f"vera.native.{key}"] = copy.deepcopy(node[index])
                    gaps.append(_gap(path + f"[{index}]", "native_extension",
                                     f"{key} is stored but not interpreted by the core runner",
                                     blocking=False))
                else:
                    gaps.append(_gap(path + f"[{index}]", f"invalid_{key}", f"{key} must be an object"))
        return result

    steps: list[dict[str, Any]] = []
    for index, node in enumerate(dag):
        path = f"dag[{index}]"
        if isinstance(node, list) and node and isinstance(node[0], list):
            branches = []
            for branch_index, branch in enumerate(node):
                converted = task(branch, f"{path}[{branch_index}]", f"s{index}.b{branch_index}")
                if converted is not None:
                    branches.append(converted)
            if branches:
                steps.append({"id": f"s{index}", "type": "parallel", "branches": branches})
        else:
            converted = task(node, path, f"s{index}")
            if converted is not None:
                steps.append(converted)
    blocking = [gap for gap in gaps if gap["blocking"]]
    workflow = normalize_workflow({"ir_version": IR_VERSION, "name": name, "steps": steps})
    return {"ok": not blocking or allow_lossy,
            "workflow": workflow if not blocking or allow_lossy else None,
            "gaps": gaps, "lossy": bool(blocking), "executes": False}


def export_native_dag(workflow: Any, *, allow_lossy: bool = False) -> dict[str, Any]:
    """Convert supported IR to a native DAG array without executing it."""
    try:
        normalized = normalize_workflow(workflow)
    except WorkflowIRValidationError as exc:
        return {"ok": False, "dag": None,
                "gaps": [_gap("workflow", "invalid_workflow", str(exc))], "executes": False}
    gaps: list[dict[str, Any]] = []
    for key in sorted((normalized.get("extensions") or {})):
        gaps.append(_gap("extensions." + key, "unsupported_extension",
                         "workflow extension has no native DAG representation"))

    def task(step: dict[str, Any], path: str) -> list[Any]:
        node: list[Any] = [step["task"], step.get("output")]
        when = step.get("when")
        extensions = step.get("extensions") or {}
        input_map = extensions.get("vera.native.input_map")
        output_map = extensions.get("vera.native.output_map")
        if when is not None or input_map is not None or output_map is not None:
            node.append(f"CONDITION:{when['key']}" if when else None)
        if input_map is not None or output_map is not None:
            node.append(copy.deepcopy(input_map) if input_map is not None else None)
            gaps.append(_gap(path + ".extensions.vera.native.input_map", "native_extension",
                             "input_map is preserved but not interpreted by the core runner",
                             blocking=False))
        if output_map is not None:
            node.append(copy.deepcopy(output_map))
            gaps.append(_gap(path + ".extensions.vera.native.output_map", "native_extension",
                             "output_map is preserved but not interpreted by the core runner",
                             blocking=False))
        unknown = sorted(key for key in extensions if key not in {
            "vera.native.input_map", "vera.native.output_map"})
        for key in unknown:
            gaps.append(_gap(path + ".extensions." + key, "unsupported_extension",
                             "extension has no native DAG representation"))
        return node

    dag: list[Any] = []
    for index, step in enumerate(normalized["steps"]):
        path = f"steps[{index}]"
        for key in sorted((step.get("extensions") or {})) if step["type"] == "parallel" else ():
            gaps.append(_gap(path + ".extensions." + key, "unsupported_extension",
                             "parallel extension has no native DAG representation"))
        if step["type"] == "parallel":
            dag.append([task(branch, f"{path}.branches[{branch_index}]")
                        for branch_index, branch in enumerate(step["branches"])])
        else:
            dag.append(task(step, path))
    blocking = [gap for gap in gaps if gap["blocking"]]
    return {"ok": not blocking or allow_lossy,
            "dag": dag if not blocking or allow_lossy else None,
            "gaps": gaps, "lossy": bool(blocking), "executes": False,
            "content_hash": normalized["content_hash"]}
