"""Deterministic, content-redacted Run projections for portable tracing.

The output is deliberately SDK-neutral: exporters can translate these documents
to OTLP without making the Run protocol depend on an observability library or a
network connection.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .run_protocol import Run, RunStatus


SCHEMA = "vera.run-portable-spans/v1"
_ERROR_STATUSES = {RunStatus.FAILED.value, RunStatus.TIMED_OUT.value}
_OK_STATUSES = {RunStatus.COMPLETED.value, RunStatus.SKIPPED.value}
_SAFE_EVENT_FIELDS = {
    "attempt", "next_attempt", "retry_owner", "progress", "completed_nodes",
    "total_nodes", "capability", "action", "status",
}
_SYMBOL = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")


def _digest(value: Any, length: int) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:length]


def _run_dict(value: Run | Mapping[str, Any]) -> dict[str, Any]:
    return value.to_dict() if isinstance(value, Run) else dict(value)


def _symbol(value: Any, fallback: str = "redacted") -> str:
    candidate = str(value or "")
    return candidate if _SYMBOL.fullmatch(candidate) else fallback


def _span_status(status: str) -> dict[str, str]:
    if status in _OK_STATUSES:
        return {"code": "OK"}
    if status in _ERROR_STATUSES:
        return {"code": "ERROR"}
    # Cancellation and non-terminal states are outcomes, not application errors.
    return {"code": "UNSET"}


def _event_projection(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    safe_payload = {
        key: (_symbol(value) if isinstance(value, str) else value)
        for key, value in dict(payload or {}).items()
        if key in _SAFE_EVENT_FIELDS and isinstance(value, (bool, int, float, str))
    }
    return {
        "name": _symbol(event.get("type"), "run.event"),
        "timestamp": str(event.get("occurred_at") or ""),
        "attributes": {
            "vera.run.event.sequence": int(event.get("sequence") or 0),
            "vera.run.event.status": _symbol(event.get("status")),
            **{f"vera.run.event.{key}": value for key, value in safe_payload.items()},
        },
    }


def _artifact_projection(artifact: Mapping[str, Any]) -> dict[str, Any]:
    uri = str(artifact.get("uri") or "")
    checksum = str(artifact.get("checksum") or "")
    return {
        "name": "vera.run.artifact",
        "timestamp": "",
        "attributes": {
            "vera.artifact.ref": _digest(artifact.get("id"), 16),
            "vera.artifact.kind": _symbol(artifact.get("kind")),
            "vera.artifact.uri_scheme": urlsplit(uri).scheme,
            "vera.artifact.media_type": _symbol(artifact.get("media_type")),
            "vera.artifact.size_bytes": int(artifact.get("size_bytes") or 0),
            "vera.artifact.checksum_algorithm": checksum.partition(":")[0],
        },
    }


def project_run_span(value: Run | Mapping[str, Any], *, root_run_id: str = "") -> dict[str, Any]:
    """Project one Run into an offline, content-free OTel/OpenInference span."""
    run = _run_dict(value)
    run_id = str(run.get("id") or "")
    parent_id = str(run.get("parent_run_id") or "")
    trace_seed = str(run.get("trace_id") or root_run_id or run_id)
    kind = _symbol(run.get("kind"), "run")
    status = _symbol(run.get("status"), RunStatus.CREATED.value)
    error = dict(run.get("error") or {})
    events = [_event_projection(item) for item in list(run.get("events") or [])]
    events.extend(_artifact_projection(item) for item in list(run.get("artifacts") or []))
    attributes: dict[str, Any] = {
        "openinference.span.kind": "TOOL" if parent_id else "CHAIN",
        "vera.run.protocol": str(run.get("protocol") or "vera.run.v1"),
        "vera.run.kind": kind,
        "vera.run.status": status,
        "vera.run.attempt": int(run.get("attempt") or 1),
        "vera.run.artifact_count": len(run.get("artifacts") or []),
        "vera.run.content_redacted": True,
    }
    if error:
        attributes["error.type"] = _symbol(error.get("code"), "run_error")
        attributes["vera.run.error.retryable"] = bool(error.get("retryable"))
    if run.get("progress") is not None:
        attributes["vera.run.progress"] = float(run["progress"])
    return {
        "name": kind,
        "trace_id": _digest(trace_seed, 32),
        "span_id": _digest(run_id, 16),
        "parent_span_id": _digest(parent_id, 16) if parent_id else "",
        "span_kind": "INTERNAL",
        "start_time": str(run.get("started_at") or run.get("created_at") or ""),
        "end_time": str(run.get("ended_at") or ""),
        "status": _span_status(status),
        "attributes": attributes,
        "events": events,
        "links": [],
    }


def project_run_trace(root: Run | Mapping[str, Any],
                      children: Iterable[Run | Mapping[str, Any]] = (), *,
                      limit: int = 200) -> dict[str, Any]:
    """Return a bounded portable trace document with parent-before-child spans."""
    bounded = max(1, min(int(limit), 200))
    root_dict = _run_dict(root)
    all_children = [_run_dict(item) for item in children]
    child_dicts = all_children[:bounded - 1]
    spans = [project_run_span(root_dict, root_run_id=str(root_dict.get("id") or ""))]
    spans.extend(project_run_span(item, root_run_id=str(root_dict.get("id") or ""))
                 for item in child_dicts)
    return {
        "schema": SCHEMA,
        "authoritative": False,
        "offline": True,
        "exported": False,
        "content_redacted": True,
        "span_count": len(spans),
        "truncated": len(all_children) > len(child_dicts),
        "spans": spans,
    }
