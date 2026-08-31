"""Read-only Workflow IR projection for persisted Vera DAG definitions."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from Vera.vera.execution.workflow_ir import import_native_dag
from Vera.vera.execution.dag_workflow_execution import prepare_plain_dag_execution


SCHEMA = "vera.stored-dag-workflow-inspection/v1"
_NATIVE_MODES = ("plain", "supervised", "monitored", "streamed", "stepwise")


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def inspect_stored_dag_workflow(
    record: dict[str, Any], *, registered_aliases: Iterable[str] = (),
) -> dict[str, Any]:
    """Project a detached stored record without mutating or executing it."""
    if not isinstance(record, dict):
        raise TypeError("record must be an object")
    dag_id = record.get("id")
    name = record.get("name")
    dag = record.get("dag")
    initial_state = record.get("initial_state")
    if not isinstance(dag_id, str) or not dag_id:
        raise ValueError("record.id must be a non-empty string")
    if not isinstance(name, str) or not name:
        raise ValueError("record.name must be a non-empty string")
    if not isinstance(dag, list):
        raise ValueError("record.dag must be an array")
    if not isinstance(initial_state, dict):
        raise ValueError("record.initial_state must be an object")

    definition_hash = _canonical_hash({"dag": dag, "initial_state": initial_state})
    stored_hash = record.get("content_hash") or ""
    imported = import_native_dag(dag, name=name, allow_lossy=False)
    prepared = prepare_plain_dag_execution(dag)
    aliases = sorted({alias for alias in registered_aliases
                      if isinstance(alias, str) and alias})
    gaps = list(imported.get("gaps") or [])
    converged_modes = {"plain", "monitored", "streamed"}
    mode_status = {
        mode: {
            "native_authoritative": (
                mode not in converged_modes or not prepared["authoritative"]
            ),
            "workflow_ir_authoritative": (
                mode in converged_modes and prepared["authoritative"]
            ),
            "parity_gate": (
                prepared["mode"]
                if mode in converged_modes else "pending"
            ),
        }
        for mode in _NATIVE_MODES
    }
    return {
        "schema": SCHEMA,
        "ok": bool(imported.get("ok")),
        "executes": False,
        "mutates": False,
        "record": {
            "id": dag_id,
            "name": name,
            "archived": bool(record.get("archived", False)),
            "updated_at": str(record.get("updated_at") or ""),
            "definition_hash": definition_hash,
            "stored_content_hash": stored_hash,
            "stored_hash_status": (
                "missing" if not stored_hash
                else "match" if stored_hash == definition_hash
                else "mismatch"
            ),
        },
        "aliases": aliases,
        "registered": bool(aliases),
        "workflow": imported.get("workflow"),
        "workflow_hash": ((imported.get("workflow") or {}).get("content_hash")
                          if imported.get("workflow") else ""),
        "gaps": gaps,
        "lossy": bool(imported.get("lossy")),
        "execution_modes": mode_status,
    }
