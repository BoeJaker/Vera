"""Exact, fail-closed Workflow IR boundary for plain native DAG execution."""

from __future__ import annotations

import json
from typing import Any

from Vera.vera.execution.workflow_ir import export_native_dag, import_native_dag


SCHEMA = "vera.dag-workflow-execution/v1"


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
