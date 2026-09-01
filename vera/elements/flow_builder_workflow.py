"""Loss-aware, non-executing Flow Builder ↔ Workflow IR conversion."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping

from ..execution.workflow_ir import (
    IR_VERSION,
    WorkflowIRValidationError,
    normalize_workflow,
)


GRAPH_EXTENSION = "vera.flow_builder.graph"


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not canonical JSON: {exc}") from exc


def _copy(value: Any) -> Any:
    return json.loads(_canonical(value))


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _gap(path: str, code: str, detail: str, *, blocking: bool = False) -> dict[str, Any]:
    return {"path": path, "code": code, "detail": detail, "blocking": blocking}


def _text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _validate_graph(graph: Any) -> dict[str, Any]:
    if not isinstance(graph, Mapping):
        raise ValueError("graph must be an object")
    if set(graph) - {"nodes", "meta"}:
        raise ValueError("graph has unsupported top-level fields")
    nodes = graph.get("nodes")
    meta = graph.get("meta", {})
    if not isinstance(nodes, list):
        raise ValueError("graph.nodes must be an array")
    if not isinstance(meta, Mapping):
        raise ValueError("graph.meta must be an object")
    result = _copy({"nodes": nodes, "meta": meta})
    seen: set[str] = set()
    for index, node in enumerate(result["nodes"]):
        if not isinstance(node, dict):
            raise ValueError(f"graph.nodes[{index}] must be an object")
        node_id = _text(f"graph.nodes[{index}].id", node.get("id"))
        _text(f"graph.nodes[{index}].type", node.get("type"))
        if node_id in seen:
            raise ValueError(f"duplicate graph node id: {node_id}")
        seen.add(node_id)
    return result


def _graph_native_gaps(graph: Mapping[str, Any]) -> list[dict[str, Any]]:
    gaps = [
        _gap(f"meta.{key}", "native_extension",
             "metadata is preserved but not portable")
        for key in sorted(set(graph["meta"]) - {"name", "description"})
    ]
    for index, node in enumerate(graph["nodes"]):
        for key in sorted(set(node) - {"id", "type", "out", "params"}):
            gaps.append(_gap(f"nodes[{index}].{key}", "native_extension",
                             "node field is exactly preserved but not portable"))
    return gaps


def semantic_diff(before: Any, after: Any) -> list[dict[str, str]]:
    """Return stable changed paths and opaque hashes, never document values."""
    before = _copy(before)
    after = _copy(after)
    changes: list[dict[str, str]] = []

    def walk(left: Any, right: Any, path: str) -> None:
        if type(left) is not type(right):
            changes.append({"path": path or "$", "kind": "type_changed",
                            "before_hash": _hash(left), "after_hash": _hash(right)})
        elif isinstance(left, dict):
            for key in sorted(set(left) | set(right)):
                child = f"{path}.{key}" if path else key
                if key not in left:
                    changes.append({"path": child, "kind": "added",
                                    "before_hash": "", "after_hash": _hash(right[key])})
                elif key not in right:
                    changes.append({"path": child, "kind": "removed",
                                    "before_hash": _hash(left[key]), "after_hash": ""})
                else:
                    walk(left[key], right[key], child)
        elif isinstance(left, list):
            for index in range(max(len(left), len(right))):
                child = f"{path}[{index}]" if path else f"[{index}]"
                if index >= len(left):
                    changes.append({"path": child, "kind": "added",
                                    "before_hash": "", "after_hash": _hash(right[index])})
                elif index >= len(right):
                    changes.append({"path": child, "kind": "removed",
                                    "before_hash": _hash(left[index]), "after_hash": ""})
                else:
                    walk(left[index], right[index], child)
        elif left != right:
            changes.append({"path": path or "$", "kind": "value_changed",
                            "before_hash": _hash(left), "after_hash": _hash(right)})

    walk(before, after, "")
    return changes


def graph_to_workflow_ir(graph: Any) -> dict[str, Any]:
    """Convert a graph while preserving its exact native representation."""
    try:
        graph = _validate_graph(graph)
    except ValueError as exc:
        return {"ok": False, "classification": "invalid", "workflow": None,
                "gaps": [_gap("graph", "invalid_graph", str(exc), blocking=True)],
                "semantic_diff": [], "executes": False}

    gaps = _graph_native_gaps(graph)
    meta = graph["meta"]
    workflow: dict[str, Any] = {
        "ir_version": IR_VERSION,
        "steps": [],
        "extensions": {GRAPH_EXTENSION: graph},
    }
    for key in ("name", "description"):
        if key in meta:
            if not isinstance(meta[key], str):
                return {"ok": False, "classification": "invalid", "workflow": None,
                        "gaps": [_gap(f"meta.{key}", "invalid_metadata",
                                      f"{key} must be a string", blocking=True)],
                        "semantic_diff": [], "executes": False}
            workflow[key] = meta[key]

    for index, node in enumerate(graph["nodes"]):
        path = f"nodes[{index}]"
        step: dict[str, Any] = {
            "id": node["id"], "type": "task", "task": node["type"],
        }
        if "out" in node:
            if not isinstance(node["out"], str):
                return {"ok": False, "classification": "invalid", "workflow": None,
                        "gaps": [_gap(f"{path}.out", "invalid_output",
                                      "out must be a string", blocking=True)],
                        "semantic_diff": [], "executes": False}
            if node["out"]:
                step["output"] = node["out"]
        params = node.get("params", {})
        if not isinstance(params, Mapping):
            return {"ok": False, "classification": "invalid", "workflow": None,
                    "gaps": [_gap(f"{path}.params", "invalid_bindings",
                                  "params must be an object", blocking=True)],
                    "semantic_diff": [], "executes": False}
        bindings: dict[str, Any] = {}
        for name, param in params.items():
            binding_path = f"{path}.params.{name}"
            if not isinstance(name, str) or not name or not isinstance(param, Mapping) \
                    or set(param) != {"source", "value"}:
                return {"ok": False, "classification": "invalid", "workflow": None,
                        "gaps": [_gap(binding_path, "invalid_binding",
                                      "binding must contain only source and value",
                                      blocking=True)], "semantic_diff": [], "executes": False}
            source = param.get("source")
            if source == "state":
                if not isinstance(param.get("value"), str) or not param["value"]:
                    return {"ok": False, "classification": "invalid", "workflow": None,
                            "gaps": [_gap(binding_path, "invalid_state_reference",
                                          "state value must be a non-empty string",
                                          blocking=True)], "semantic_diff": [], "executes": False}
                bindings[name] = {"kind": "state", "value": param["value"]}
            elif source == "value":
                bindings[name] = {"kind": "literal", "value": param.get("value")}
            else:
                return {"ok": False, "classification": "unsupported", "workflow": None,
                        "gaps": [_gap(binding_path, "unsupported_binding_source",
                                      "binding source is not portable", blocking=True)],
                        "semantic_diff": [], "executes": False}
        if bindings:
            step["bindings"] = bindings
        workflow["steps"].append(step)

    try:
        workflow = normalize_workflow(workflow)
    except WorkflowIRValidationError as exc:
        return {"ok": False, "classification": "invalid", "workflow": None,
                "gaps": [_gap("workflow", "invalid_workflow", str(exc), blocking=True)],
                "semantic_diff": [], "executes": False}
    classification = "portable" if not gaps else "native_extensions"
    return {"ok": True, "classification": classification,
            "workflow": workflow, "gaps": gaps, "semantic_diff": [],
            "executes": False}


def workflow_ir_to_graph(workflow: Any) -> dict[str, Any]:
    """Read IR exactly, refusing any conversion that would silently lose data."""
    try:
        normalized = normalize_workflow(workflow)
    except (TypeError, WorkflowIRValidationError) as exc:
        return {"ok": False, "classification": "invalid", "graph": None,
                "gaps": [_gap("workflow", "invalid_workflow", str(exc), blocking=True)],
                "semantic_diff": [], "executes": False}
    preserved = (normalized.get("extensions") or {}).get(GRAPH_EXTENSION)
    if preserved is not None:
        try:
            graph = _validate_graph(preserved)
        except ValueError as exc:
            return {"ok": False, "classification": "invalid", "graph": None,
                    "gaps": [_gap(f"extensions.{GRAPH_EXTENSION}", "invalid_extension",
                                  str(exc), blocking=True)],
                    "semantic_diff": [], "executes": False}
        projected = graph_to_workflow_ir(graph)
        if not projected["ok"]:
            return {"ok": False, "classification": "invalid", "graph": None,
                    "content_hash": normalized["content_hash"],
                    "gaps": projected["gaps"], "semantic_diff": [],
                    "executes": False}
        expected = dict(projected["workflow"])
        actual = dict(normalized)
        expected.pop("content_hash", None)
        actual.pop("content_hash", None)
        differences = semantic_diff(expected, actual)
        if differences:
            return {"ok": False, "classification": "inconsistent", "graph": None,
                    "content_hash": normalized["content_hash"],
                    "gaps": [_gap("workflow", "extension_ir_mismatch",
                                  "preserved graph and Workflow IR semantics disagree",
                                  blocking=True)],
                    "semantic_diff": differences, "executes": False}
        gaps = _graph_native_gaps(graph)
        return {"ok": True,
                "classification": "portable" if not gaps else "native_extensions",
                "graph": graph, "content_hash": normalized["content_hash"],
                "gaps": gaps, "semantic_diff": differences,
                "executes": False}

    gaps: list[dict[str, Any]] = []
    allowed_top = {"ir_version", "name", "description", "steps", "content_hash"}
    for key in sorted(set(normalized) - allowed_top):
        gaps.append(_gap(key, "unsupported_contract",
                         "workflow contract cannot be represented by the generic graph",
                         blocking=True))
    graph = {"nodes": [], "meta": {}}
    for key in ("name", "description"):
        if key in normalized:
            graph["meta"][key] = normalized[key]
    for index, step in enumerate(normalized["steps"]):
        path = f"steps[{index}]"
        if step.get("type") != "task":
            gaps.append(_gap(path, "unsupported_structure",
                             "only task steps are representable", blocking=True))
            continue
        allowed_step = {"id", "type", "task", "output", "bindings"}
        for key in sorted(set(step) - allowed_step):
            gaps.append(_gap(f"{path}.{key}", "unsupported_contract",
                             "task contract cannot be represented", blocking=True))
        node: dict[str, Any] = {"id": step["id"], "type": step["task"], "params": {}}
        if step.get("output"):
            node["out"] = step["output"]
        for name, ref in (step.get("bindings") or {}).items():
            if ref["kind"] == "state":
                node["params"][name] = {"source": "state", "value": ref["value"]}
            elif ref["kind"] == "literal":
                node["params"][name] = {"source": "value", "value": ref["value"]}
            else:
                gaps.append(_gap(f"{path}.bindings.{name}", "unsupported_reference",
                                 "reference kind cannot be represented", blocking=True))
        graph["nodes"].append(node)
    if gaps:
        return {"ok": False, "classification": "unsupported", "graph": None,
                "content_hash": normalized["content_hash"], "gaps": gaps,
                "semantic_diff": [], "executes": False}
    graph = _validate_graph(graph)
    return {"ok": True, "classification": "portable", "graph": graph,
            "content_hash": normalized["content_hash"], "gaps": [],
            "semantic_diff": [], "executes": False}


def analyze_flow_builder_graph(graph: Any) -> dict[str, Any]:
    converted = graph_to_workflow_ir(graph)
    return {key: value for key, value in converted.items() if key != "workflow"}
