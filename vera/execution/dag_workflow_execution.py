"""Exact, fail-closed Workflow IR boundary for plain native DAG execution."""

from __future__ import annotations

import json
import os
from typing import Any

from Vera.vera.execution.workflow_ir import (
    WorkflowIRValidationError,
    export_native_dag,
    import_native_dag,
    normalize_workflow,
)


SCHEMA = "vera.dag-workflow-execution/v1"


def native_bridge_enabled() -> bool:
    """Return whether Workflow IR may own the native definition boundary."""
    return str(os.getenv("VERA_WORKFLOW_IR_NATIVE_BRIDGE", "1")).strip().lower() \
        not in {"0", "false", "no", "off"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def prepare_plain_dag_execution(graph: list) -> dict[str, Any]:
    """Return an exact IR-materialized graph or an explicit native fallback.

    The caller may execute ``graph`` in either outcome. ``authoritative`` is
    true only when normalization and export preserve the native definition
    byte-for-byte under canonical JSON comparison.
    """
    if not isinstance(graph, list):
        raise TypeError("graph must be an array")
    if not native_bridge_enabled():
        return _fallback(graph, "bridge_disabled")
    try:
        imported = import_native_dag(graph, name="dag.run", allow_lossy=False)
    except (TypeError, ValueError) as exc:
        return _fallback(graph, "non_canonical_native_graph", str(exc))
    if not imported.get("ok") or not imported.get("workflow"):
        return _fallback(graph, "workflow_import_blocked", gaps=imported.get("gaps") or [])

    exported = export_native_dag(imported["workflow"], allow_lossy=False)
    if not exported.get("ok") or exported.get("dag") is None:
        return _fallback(graph, "workflow_export_blocked", gaps=exported.get("gaps") or [])
    try:
        exact = _canonical(graph) == _canonical(exported["dag"])
    except (TypeError, ValueError) as exc:
        return _fallback(graph, "non_canonical_native_graph", str(exc))
    if not exact:
        return _fallback(graph, "round_trip_mismatch",
                         gaps=(imported.get("gaps") or []) + (exported.get("gaps") or []))

    workflow = imported["workflow"]
    return {
        "schema": SCHEMA,
        "authoritative": True,
        "mode": "workflow_ir_materialized",
        "graph": exported["dag"],
        "workflow_hash": workflow["content_hash"],
        "ir_version": workflow["ir_version"],
        "gaps": (imported.get("gaps") or []) + (exported.get("gaps") or []),
        "executes": False,
    }


def prepare_workflow_ir_native_execution(workflow: dict[str, Any]) -> dict[str, Any]:
    """Compile the exact supported IR subset for the existing native DAG engine.

    This function only prepares execution. It never invokes a capability and
    therefore cannot become a second retry, cancellation, or effects owner.
    Unsupported or lossy definitions are returned as blocked evidence with no
    executable graph.
    """
    if not native_bridge_enabled():
        return {
            "schema": SCHEMA, "ok": False, "authoritative": False,
            "mode": "native_compatibility", "reason": "bridge_disabled",
            "graph": None, "workflow_hash": "", "ir_version": "",
            "gaps": [], "executes": False,
        }
    try:
        normalized = normalize_workflow(workflow)
    except (TypeError, ValueError, WorkflowIRValidationError) as exc:
        return {
            "schema": SCHEMA, "ok": False, "authoritative": False,
            "mode": "workflow_ir_blocked", "reason": "invalid_workflow",
            "detail": str(exc), "graph": None, "workflow_hash": "",
            "ir_version": "", "gaps": [], "executes": False,
        }
    exported = export_native_dag(normalized, allow_lossy=False)
    if not exported.get("ok") or exported.get("dag") is None:
        return {
            "schema": SCHEMA, "ok": False, "authoritative": False,
            "mode": "workflow_ir_blocked",
            "reason": "unsupported_workflow_semantics", "graph": None,
            "workflow_hash": normalized["content_hash"],
            "ir_version": normalized["ir_version"],
            "gaps": list(exported.get("gaps") or []), "executes": False,
        }
    return {
        "schema": SCHEMA, "ok": True, "authoritative": True,
        "mode": "workflow_ir_native_bridge", "graph": exported["dag"],
        "workflow_hash": normalized["content_hash"],
        "ir_version": normalized["ir_version"],
        "gaps": list(exported.get("gaps") or []), "executes": False,
    }


def prepare_dag_execution(graph: list, *, supervised: bool = False) -> dict[str, Any]:
    """Prepare a DAG for execution without changing supervised semantics."""
    if not isinstance(graph, list):
        raise TypeError("graph must be an array")
    prepared = prepare_plain_dag_execution(graph)
    prepared["control_mode"] = (
        "native_supervised" if supervised else "native_plain"
    )
    return prepared


def workflow_execution_metadata(prepared: dict[str, Any]) -> dict[str, Any]:
    """Return the stable, graph-free provenance exposed by DAG entry points."""
    metadata = {
        "schema": prepared["schema"],
        "authoritative": bool(prepared.get("authoritative")),
        "mode": prepared.get("mode", "native_compatibility"),
        "workflow_hash": prepared.get("workflow_hash", ""),
        "ir_version": prepared.get("ir_version", ""),
        "control_mode": prepared.get("control_mode", "native_plain"),
        "gaps": list(prepared.get("gaps") or []),
    }
    if prepared.get("reason"):
        metadata["reason"] = prepared["reason"]
    return metadata


def prepare_streamed_dag_execution(
    graph: list, *, include_workflow_ir: bool = False,
) -> dict[str, Any]:
    """Prepare one complete streamed graph without executing or emitting it."""
    prepared = prepare_dag_execution(graph)
    return {
        "graph": prepared["graph"],
        "workflow_ir": (
            workflow_execution_metadata(prepared)
            if include_workflow_ir else None
        ),
    }


def prepare_stepwise_dag_action(
    cap_name: str, out_key: str, *, include_workflow_ir: bool = False,
) -> dict[str, Any]:
    """Prepare one adaptive action without claiming an upfront workflow."""
    if not isinstance(cap_name, str) or not cap_name:
        raise ValueError("cap_name must be a non-empty string")
    if not isinstance(out_key, str) or not out_key:
        raise ValueError("out_key must be a non-empty string")
    graph = [[cap_name, out_key]]
    prepared = prepare_dag_execution(graph)
    prepared["control_mode"] = "native_stepwise"
    materialized = prepared["graph"][0]
    return {
        "cap": materialized[0],
        "out_key": materialized[1],
        "workflow_ir": (
            workflow_execution_metadata(prepared)
            if include_workflow_ir else None
        ),
    }


def _fallback(graph: list, reason: str, detail: str = "", gaps: list | None = None) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "authoritative": False,
        "mode": "native_compatibility",
        "reason": reason,
        "detail": detail,
        "graph": graph,
        "workflow_hash": "",
        "ir_version": "",
        "gaps": list(gaps or []),
        "executes": False,
    }
