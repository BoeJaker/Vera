"""Runtime-neutral Workflow IR and loss-aware adapters for Vera's native DAG.

This module only describes workflows.  It deliberately has no execution entry
point: native engines remain authoritative until a later, separately gated
integration chooses an adapter and authorizes its effects.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any


IR_VERSION = "1.0"
_STEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REF_KINDS = {"state", "secret", "artifact", "record", "literal"}
_EFFECT_KINDS = {"filesystem", "network", "database", "process", "model",
                 "device", "notification", "external_service"}


class WorkflowIRValidationError(ValueError):
    """The supplied IR is structurally invalid."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def workflow_hash(workflow: dict[str, Any]) -> str:
    """Return a stable content hash, excluding any previously attached hash."""
    material = copy.deepcopy(workflow)
    material.pop("content_hash", None)
    try:
        encoded = _canonical(material).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkflowIRValidationError(f"workflow is not canonical JSON: {exc}") from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def normalize_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    """Validate and return a detached canonical-order-independent IR value."""
    if not isinstance(workflow, dict):
        raise WorkflowIRValidationError("workflow must be an object")
    if workflow.get("ir_version") != IR_VERSION:
        raise WorkflowIRValidationError(f"unsupported ir_version: {workflow.get('ir_version')!r}")
    allowed_workflow = {"ir_version", "name", "description", "inputs", "outputs",
                        "steps", "extensions", "schedule", "resources", "providers",
                        "content_hash"}
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
    for collection in ("inputs", "outputs"):
        for port_name, port in (workflow.get(collection) or {}).items():
            path = f"{collection}.{port_name}"
            if not isinstance(port_name, str) or not port_name:
                raise WorkflowIRValidationError(f"{collection} keys must be non-empty strings")
            if not isinstance(port, dict) or set(port) - {"schema", "required", "description"}:
                raise WorkflowIRValidationError(f"{path} has unsupported fields")
            if not isinstance(port.get("schema"), dict):
                raise WorkflowIRValidationError(f"{path}.schema must be an object")
            if "required" in port and not isinstance(port["required"], bool):
                raise WorkflowIRValidationError(f"{path}.required must be boolean")
            if "description" in port and not isinstance(port["description"], str):
                raise WorkflowIRValidationError(f"{path}.description must be a string")
    schedule = workflow.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, dict) or set(schedule) - {
                "kind", "expression", "seconds", "event", "timezone", "owner"}:
            raise WorkflowIRValidationError("schedule has unsupported fields")
        kind = schedule.get("kind")
        if kind not in {"cron", "interval", "event"}:
            raise WorkflowIRValidationError("schedule.kind is unsupported")
        required = {"cron": "expression", "interval": "seconds", "event": "event"}[kind]
        if required not in schedule:
            raise WorkflowIRValidationError(f"schedule.{required} is required for {kind}")
        if kind == "interval" and (not isinstance(schedule["seconds"], (int, float))
                                   or isinstance(schedule["seconds"], bool)
                                   or not math.isfinite(schedule["seconds"])
                                   or schedule["seconds"] <= 0):
            raise WorkflowIRValidationError("schedule.seconds must be positive")
        for key in ("expression", "event", "timezone", "owner"):
            if key in schedule and not isinstance(schedule[key], str):
                raise WorkflowIRValidationError(f"schedule.{key} must be a string")
        if schedule.get("owner", "runtime") not in {"runtime", "external"}:
            raise WorkflowIRValidationError("schedule.owner is unsupported")
    resources = workflow.get("resources")
    if resources is not None:
        if not isinstance(resources, dict) or set(resources) - {
                "cpu", "memory_mb", "accelerator", "max_concurrency"}:
            raise WorkflowIRValidationError("resources has unsupported fields")
        for key in ("cpu", "memory_mb", "max_concurrency"):
            if key in resources and (not isinstance(resources[key], (int, float))
                                     or isinstance(resources[key], bool)
                                     or not math.isfinite(resources[key])
                                     or resources[key] <= 0):
                raise WorkflowIRValidationError(f"resources.{key} must be positive")
        if "accelerator" in resources and not isinstance(resources["accelerator"], str):
            raise WorkflowIRValidationError("resources.accelerator must be a string")
    providers = workflow.get("providers")
    if providers is not None:
        if not isinstance(providers, list) or any(
                not isinstance(provider, str) or not provider for provider in providers):
            raise WorkflowIRValidationError("providers must be non-empty opaque strings")
        if len(set(providers)) != len(providers):
            raise WorkflowIRValidationError("providers must not contain duplicates")
    steps = workflow.get("steps")
    if not isinstance(steps, list):
        raise WorkflowIRValidationError("steps must be an array")
    result = copy.deepcopy(workflow)
    seen: set[str] = set()

    def check_ref(ref: Any, path: str) -> None:
        if not isinstance(ref, dict):
            raise WorkflowIRValidationError(f"{path} must be a reference object")
        unknown = sorted(set(ref) - {"kind", "value", "provider", "revision"})
        if unknown:
            raise WorkflowIRValidationError(f"{path} has unknown fields: {', '.join(unknown)}")
        kind = ref.get("kind")
        if kind not in _REF_KINDS:
            raise WorkflowIRValidationError(f"{path}.kind is unsupported: {kind!r}")
        if "value" not in ref:
            raise WorkflowIRValidationError(f"{path}.value is required")
        if kind != "literal" and (not isinstance(ref["value"], str) or not ref["value"]):
            raise WorkflowIRValidationError(f"{path}.value must be a non-empty opaque string")
        if kind == "secret" and not ref["value"].startswith("secret://"):
            raise WorkflowIRValidationError(f"{path}.value must use an opaque secret:// reference")
        for key in ("provider", "revision"):
            if key in ref and not isinstance(ref[key], str):
                raise WorkflowIRValidationError(f"{path}.{key} must be a string")

    def check_contracts(step: dict[str, Any], path: str) -> None:
        bindings = step.get("bindings")
        if bindings is not None:
            if not isinstance(bindings, dict):
                raise WorkflowIRValidationError(f"{path}.bindings must be an object")
            for name, ref in bindings.items():
                if not isinstance(name, str) or not name:
                    raise WorkflowIRValidationError(f"{path}.bindings keys must be non-empty strings")
                check_ref(ref, f"{path}.bindings.{name}")
        retry = step.get("retry")
        if retry is not None:
            if not isinstance(retry, dict) or set(retry) - {"max_attempts", "backoff_seconds", "owner"}:
                raise WorkflowIRValidationError(f"{path}.retry has unsupported fields")
            if not isinstance(retry.get("max_attempts"), int) or isinstance(retry.get("max_attempts"), bool) or retry["max_attempts"] < 1:
                raise WorkflowIRValidationError(f"{path}.retry.max_attempts must be a positive integer")
            if "backoff_seconds" in retry and (not isinstance(retry["backoff_seconds"], (int, float))
                                                or isinstance(retry["backoff_seconds"], bool)
                                                or not math.isfinite(retry["backoff_seconds"])
                                                or retry["backoff_seconds"] < 0):
                raise WorkflowIRValidationError(f"{path}.retry.backoff_seconds must be non-negative")
            if retry.get("owner", "runtime") not in {"runtime", "task"}:
                raise WorkflowIRValidationError(f"{path}.retry.owner is unsupported")
        timeout = step.get("timeout")
        if timeout is not None:
            if not isinstance(timeout, dict) or set(timeout) - {"seconds", "owner"}:
                raise WorkflowIRValidationError(f"{path}.timeout has unsupported fields")
            if (not isinstance(timeout.get("seconds"), (int, float))
                    or isinstance(timeout.get("seconds"), bool)
                    or not math.isfinite(timeout["seconds"]) or timeout["seconds"] <= 0):
                raise WorkflowIRValidationError(f"{path}.timeout.seconds must be positive")
            if timeout.get("owner", "runtime") not in {"runtime", "task"}:
                raise WorkflowIRValidationError(f"{path}.timeout.owner is unsupported")
        idempotency = step.get("idempotency")
        if idempotency is not None:
            if not isinstance(idempotency, dict) or set(idempotency) - {"key", "owner"}:
                raise WorkflowIRValidationError(f"{path}.idempotency has unsupported fields")
            check_ref(idempotency.get("key"), f"{path}.idempotency.key")
            if idempotency.get("owner", "runtime") not in {"runtime", "task"}:
                raise WorkflowIRValidationError(f"{path}.idempotency.owner is unsupported")
        effects = step.get("effects")
        if effects is not None:
            if not isinstance(effects, list):
                raise WorkflowIRValidationError(f"{path}.effects must be an array")
            for index, effect in enumerate(effects):
                effect_path = f"{path}.effects[{index}]"
                if not isinstance(effect, dict) or set(effect) - {"kind", "target", "mode"}:
                    raise WorkflowIRValidationError(f"{effect_path} has unsupported fields")
                if effect.get("kind") not in _EFFECT_KINDS:
                    raise WorkflowIRValidationError(f"{effect_path}.kind is unsupported")
                for key in ("target", "mode"):
                    if key in effect and not isinstance(effect[key], str):
                        raise WorkflowIRValidationError(f"{effect_path}.{key} must be a string")

    def check_step(step: Any, path: str) -> None:
        if not isinstance(step, dict):
            raise WorkflowIRValidationError(f"{path} must be an object")
        step_id = step.get("id")
        if not isinstance(step_id, str) or not _STEP_ID.fullmatch(step_id):
            raise WorkflowIRValidationError(f"{path}.id is invalid")
        if step_id in seen:
            raise WorkflowIRValidationError(f"duplicate step id: {step_id}")
        seen.add(step_id)
        if "extensions" in step and not isinstance(step["extensions"], dict):
            raise WorkflowIRValidationError(f"{path}.extensions must be an object")
        kind = step.get("type")
        if kind == "task":
            unknown = sorted(set(step) - {"id", "type", "task", "output", "when", "extensions",
                                               "bindings", "retry", "timeout", "idempotency", "effects",
                                               "approval", "compensation"})
            if unknown:
                raise WorkflowIRValidationError(
                    f"{path} has unknown fields: {', '.join(unknown)}")
            if not isinstance(step.get("task"), str) or not step["task"].strip():
                raise WorkflowIRValidationError(f"{path}.task is required")
            if "output" in step and not isinstance(step["output"], str):
                raise WorkflowIRValidationError(f"{path}.output must be a string")
            if "extensions" in step and not isinstance(step["extensions"], dict):
                raise WorkflowIRValidationError(f"{path}.extensions must be an object")
            check_contracts(step, path)
            approval = step.get("approval")
            if approval is not None:
                if not isinstance(approval, dict) or set(approval) - {
                        "required", "policy", "timeout_seconds"}:
                    raise WorkflowIRValidationError(f"{path}.approval has unsupported fields")
                if approval.get("required") is not True:
                    raise WorkflowIRValidationError(f"{path}.approval.required must be true")
                if "policy" in approval and not isinstance(approval["policy"], str):
                    raise WorkflowIRValidationError(f"{path}.approval.policy must be a string")
                if "timeout_seconds" in approval and (
                        not isinstance(approval["timeout_seconds"], (int, float))
                        or isinstance(approval["timeout_seconds"], bool)
                        or not math.isfinite(approval["timeout_seconds"])
                        or approval["timeout_seconds"] <= 0):
                    raise WorkflowIRValidationError(
                        f"{path}.approval.timeout_seconds must be positive")
            compensation = step.get("compensation")
            if compensation is not None:
                if not isinstance(compensation, dict) or set(compensation) - {"task", "on"}:
                    raise WorkflowIRValidationError(f"{path}.compensation has unsupported fields")
                if not isinstance(compensation.get("task"), str) or not compensation["task"]:
                    raise WorkflowIRValidationError(f"{path}.compensation.task is required")
                triggers = compensation.get("on", ["failure"])
                if (not isinstance(triggers, list) or not triggers
                        or any(trigger not in {"failure", "cancel", "timeout"} for trigger in triggers)):
                    raise WorkflowIRValidationError(f"{path}.compensation.on is unsupported")
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
        elif kind == "subworkflow":
            unknown = sorted(set(step) - {"id", "type", "workflow", "bindings", "output",
                                               "extensions"})
            if unknown:
                raise WorkflowIRValidationError(
                    f"{path} has unknown fields: {', '.join(unknown)}")
            check_ref(step.get("workflow"), f"{path}.workflow")
            if step["workflow"]["kind"] not in {"artifact", "record"}:
                raise WorkflowIRValidationError(
                    f"{path}.workflow must be an artifact or record reference")
            bindings = step.get("bindings", {})
            if not isinstance(bindings, dict):
                raise WorkflowIRValidationError(f"{path}.bindings must be an object")
            for name, ref in bindings.items():
                if not isinstance(name, str) or not name:
                    raise WorkflowIRValidationError(
                        f"{path}.bindings keys must be non-empty strings")
                check_ref(ref, f"{path}.bindings.{name}")
            if "output" in step and not isinstance(step["output"], str):
                raise WorkflowIRValidationError(f"{path}.output must be a string")
            if "extensions" in step and not isinstance(step["extensions"], dict):
                raise WorkflowIRValidationError(f"{path}.extensions must be an object")
        elif kind == "choice":
            unknown = sorted(set(step) - {"id", "type", "cases", "default", "extensions"})
            if unknown:
                raise WorkflowIRValidationError(
                    f"{path} has unknown fields: {', '.join(unknown)}")
            cases = step.get("cases")
            if not isinstance(cases, list) or not cases:
                raise WorkflowIRValidationError(f"{path}.cases must be non-empty")
            for case_index, case in enumerate(cases):
                case_path = f"{path}.cases[{case_index}]"
                if not isinstance(case, dict) or set(case) != {"when", "steps"}:
                    raise WorkflowIRValidationError(f"{case_path} requires when and steps")
                check_ref(case["when"], case_path + ".when")
                if not isinstance(case["steps"], list) or not case["steps"]:
                    raise WorkflowIRValidationError(f"{case_path}.steps must be non-empty")
                for index, child in enumerate(case["steps"]):
                    check_step(child, f"{case_path}.steps[{index}]")
            default = step.get("default", [])
            if not isinstance(default, list):
                raise WorkflowIRValidationError(f"{path}.default must be an array")
            for index, child in enumerate(default):
                check_step(child, f"{path}.default[{index}]")
        elif kind == "map":
            unknown = sorted(set(step) - {"id", "type", "items", "body", "output",
                                               "max_concurrency", "extensions"})
            if unknown:
                raise WorkflowIRValidationError(
                    f"{path} has unknown fields: {', '.join(unknown)}")
            check_ref(step.get("items"), f"{path}.items")
            body = step.get("body")
            if not isinstance(body, list) or not body:
                raise WorkflowIRValidationError(f"{path}.body must be non-empty")
            for index, child in enumerate(body):
                check_step(child, f"{path}.body[{index}]")
            if "output" in step and not isinstance(step["output"], str):
                raise WorkflowIRValidationError(f"{path}.output must be a string")
            concurrency = step.get("max_concurrency")
            if concurrency is not None and (not isinstance(concurrency, int)
                                            or isinstance(concurrency, bool) or concurrency < 1):
                raise WorkflowIRValidationError(f"{path}.max_concurrency must be positive")
        elif kind == "reduce":
            unknown = sorted(set(step) - {"id", "type", "items", "initial", "reducer",
                                               "output", "extensions"})
            if unknown:
                raise WorkflowIRValidationError(
                    f"{path} has unknown fields: {', '.join(unknown)}")
            check_ref(step.get("items"), f"{path}.items")
            if "initial" in step:
                check_ref(step["initial"], f"{path}.initial")
            check_step(step.get("reducer"), f"{path}.reducer")
            if step["reducer"].get("type") != "task":
                raise WorkflowIRValidationError(f"{path}.reducer must be a task")
            if "output" in step and not isinstance(step["output"], str):
                raise WorkflowIRValidationError(f"{path}.output must be a string")
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
    for field in ("inputs", "outputs", "schedule", "resources", "providers"):
        if normalized.get(field):
            gaps.append(_gap(field, "unsupported_contract",
                             f"typed workflow {field} are not represented by a native DAG array"))
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
        for field, detail in (
            ("bindings", "explicit typed bindings are not enforced by the native state injector"),
            ("retry", "retry ownership is not represented by a native DAG node"),
            ("timeout", "timeout ownership is not represented by a native DAG node"),
            ("idempotency", "idempotency ownership is not represented by a native DAG node"),
            ("effects", "declared effects are not represented or authorized by a native DAG node"),
            ("approval", "HITL approval is not represented or consumed by a native DAG node"),
            ("compensation", "compensation is not represented or invoked by a native DAG node"),
        ):
            if field in step:
                gaps.append(_gap(path + "." + field, "unsupported_contract", detail))
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
        elif step["type"] == "task":
            dag.append(task(step, path))
        else:
            gaps.append(_gap(path, "unsupported_structure",
                             f"{step['type']} has no native DAG representation"))
    blocking = [gap for gap in gaps if gap["blocking"]]
    return {"ok": not blocking or allow_lossy,
            "dag": dag if not blocking or allow_lossy else None,
            "gaps": gaps, "lossy": bool(blocking), "executes": False,
            "content_hash": normalized["content_hash"]}
