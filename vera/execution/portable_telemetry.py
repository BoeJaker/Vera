"""Deterministic, content-redacted Run projections for portable tracing.

The output is deliberately SDK-neutral: exporters can translate these documents
to OTLP without making the Run protocol depend on an observability library or a
network connection.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import json
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlsplit

from .run_protocol import Run, RunStatus


SCHEMA = "vera.run-portable-spans/v1"
MAX_EVENTS_PER_SPAN = 200
_ERROR_STATUSES = {RunStatus.FAILED.value, RunStatus.TIMED_OUT.value}
_OK_STATUSES = {RunStatus.COMPLETED.value, RunStatus.SKIPPED.value}
_SAFE_EVENT_FIELDS = {
    "attempt", "next_attempt", "retry_owner", "progress", "completed_nodes",
    "total_nodes", "capability", "action", "status",
}
_SYMBOL = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$")


@dataclass(frozen=True)
class ExporterConfig:
    enabled: bool
    endpoint: str = ""
    protocol: str = "http/json"
    timeout_seconds: float = 10.0
    max_request_bytes: int = 1_048_576
    headers: tuple[tuple[str, str], ...] = ()
    reason: str = "not_configured"


@dataclass(frozen=True)
class QueueConfig:
    enabled: bool
    max_queue: int = 64
    batch_size: int = 8
    flush_seconds: float = 0.1
    dedupe_limit: int = 1024
    retry_max: int = 2
    retry_backoff_seconds: float = 0.1
    reason: str = "auto_export_disabled"


_EXPORT_STATS: dict[str, Any] = {
    "attempts": 0, "accepted": 0, "failed": 0, "last_status": "never",
    "last_error_type": "", "last_duration_ms": None, "last_span_count": 0,
}
_EXPORT_STATS_LOCK = threading.Lock()


def _digest(value: Any, length: int) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:length]


def _run_dict(value: Run | Mapping[str, Any]) -> dict[str, Any]:
    return value.to_dict() if isinstance(value, Run) else dict(value)


def _symbol(value: Any, fallback: str = "redacted") -> str:
    candidate = str(value or "")
    return candidate if _SYMBOL.fullmatch(candidate) else fallback


def _bounded_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(float(value), high))
    except (TypeError, ValueError):
        return default


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def _parse_headers(value: str) -> tuple[tuple[str, str], ...]:
    parsed = []
    for item in (value or "").split(","):
        name, separator, raw_value = item.partition("=")
        name = name.strip().lower()
        if not separator or not _HEADER_NAME.fullmatch(name):
            continue
        if name in {"content-type", "content-length", "host"}:
            continue
        parsed.append((name, unquote(raw_value.strip())))
        if len(parsed) >= 32:
            break
    return tuple(parsed)


def exporter_config(environ: Mapping[str, str] | None = None) -> ExporterConfig:
    """Resolve explicit OTLP/HTTP JSON configuration; no endpoint means offline."""
    env = environ if environ is not None else os.environ
    trace_endpoint = str(env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or "").strip()
    base_endpoint = str(env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
    endpoint = trace_endpoint or (base_endpoint.rstrip("/") + "/v1/traces"
                                  if base_endpoint else "")
    protocol = str(env.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") or
                   env.get("OTEL_EXPORTER_OTLP_PROTOCOL") or "http/json").strip().lower()
    if not endpoint:
        return ExporterConfig(enabled=False)
    parts = urlsplit(endpoint)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or
            parts.username or parts.password or parts.fragment):
        return ExporterConfig(enabled=False, endpoint="", protocol=protocol,
                              reason="invalid_endpoint")
    if protocol != "http/json":
        return ExporterConfig(enabled=False, endpoint=endpoint, protocol=protocol,
                              reason="unsupported_protocol")
    timeout_ms = env.get("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT") or env.get(
        "OTEL_EXPORTER_OTLP_TIMEOUT") or "10000"
    headers = env.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS") or env.get(
        "OTEL_EXPORTER_OTLP_HEADERS") or ""
    return ExporterConfig(
        enabled=True, endpoint=endpoint, protocol=protocol,
        timeout_seconds=_bounded_float(timeout_ms, 10000.0, 100.0, 10000.0) / 1000.0,
        max_request_bytes=_bounded_int(env.get("VERA_OTLP_MAX_REQUEST_BYTES"),
                                       1_048_576, 1024, 4_194_304),
        headers=_parse_headers(headers), reason="configured",
    )


def queue_config(environ: Mapping[str, str] | None = None) -> QueueConfig:
    env = environ if environ is not None else os.environ
    enabled_value = str(env.get("VERA_OTLP_AUTO_EXPORT") or "").strip().lower()
    exporter = exporter_config(env)
    if enabled_value not in {"1", "true", "yes", "on"}:
        return QueueConfig(enabled=False)
    if not exporter.enabled:
        return QueueConfig(enabled=False, reason=exporter.reason)
    return QueueConfig(
        enabled=True,
        max_queue=_bounded_int(env.get("VERA_OTLP_QUEUE_SIZE"), 64, 1, 256),
        batch_size=_bounded_int(env.get("VERA_OTLP_BATCH_SIZE"), 8, 1, 32),
        flush_seconds=_bounded_float(env.get("VERA_OTLP_FLUSH_MS"),
                                     100.0, 0.0, 1000.0) / 1000.0,
        dedupe_limit=_bounded_int(env.get("VERA_OTLP_DEDUPE_SIZE"), 1024, 16, 4096),
        retry_max=_bounded_int(env.get("VERA_OTLP_RETRY_MAX"), 2, 0, 3),
        retry_backoff_seconds=_bounded_float(env.get("VERA_OTLP_RETRY_BACKOFF_MS"),
                                             100.0, 0.0, 1000.0) / 1000.0,
        reason="configured",
    )


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


def _artifact_projection(artifact: Mapping[str, Any], timestamp: str = "") -> dict[str, Any]:
    uri = str(artifact.get("uri") or "")
    checksum = str(artifact.get("checksum") or "")
    return {
        "name": "vera.run.artifact",
        "timestamp": timestamp,
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
    raw_events = list(run.get("events") or [])
    raw_artifacts = list(run.get("artifacts") or [])
    selected_events = raw_events[:MAX_EVENTS_PER_SPAN]
    remaining = max(0, MAX_EVENTS_PER_SPAN - len(selected_events))
    selected_artifacts = raw_artifacts[:remaining]
    events = [_event_projection(item) for item in selected_events]
    artifact_time = str(run.get("ended_at") or run.get("started_at") or
                        run.get("created_at") or "")
    events.extend(_artifact_projection(item, artifact_time) for item in selected_artifacts)
    attributes: dict[str, Any] = {
        "openinference.span.kind": "TOOL" if parent_id else "CHAIN",
        "vera.run.protocol": str(run.get("protocol") or "vera.run.v1"),
        "vera.run.kind": kind,
        "vera.run.status": status,
        "vera.run.attempt": int(run.get("attempt") or 1),
        "vera.run.artifact_count": len(run.get("artifacts") or []),
        "vera.run.event_count": len(raw_events),
        "vera.run.events_truncated": (
            len(raw_events) + len(raw_artifacts) > len(events)),
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


def _unix_nanos(value: str) -> str:
    if not value:
        return "0"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return str(int(parsed.timestamp() * 1_000_000_000))
    except (TypeError, ValueError, OverflowError):
        return "0"


def _any_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _otlp_attributes(attributes: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"key": str(key), "value": _any_value(value)}
            for key, value in sorted(attributes.items())]


def to_otlp_json(trace: Mapping[str, Any]) -> dict[str, Any]:
    """Translate a portable trace into an OTLP/HTTP JSON ExportTrace request."""
    spans = []
    for span in list(trace.get("spans") or []):
        item = {
            "traceId": str(span.get("trace_id") or ""),
            "spanId": str(span.get("span_id") or ""),
            "name": str(span.get("name") or "run"),
            "kind": "SPAN_KIND_INTERNAL",
            "startTimeUnixNano": _unix_nanos(str(span.get("start_time") or "")),
            "endTimeUnixNano": _unix_nanos(str(span.get("end_time") or "")),
            "attributes": _otlp_attributes(dict(span.get("attributes") or {})),
            "events": [{
                "timeUnixNano": _unix_nanos(str(event.get("timestamp") or "")),
                "name": str(event.get("name") or "run.event"),
                "attributes": _otlp_attributes(dict(event.get("attributes") or {})),
            } for event in list(span.get("events") or [])],
            "status": {"code": "STATUS_CODE_" +
                       str((span.get("status") or {}).get("code") or "UNSET")},
        }
        if span.get("parent_span_id"):
            item["parentSpanId"] = str(span["parent_span_id"])
        spans.append(item)
    return {
        "resourceSpans": [{
            "resource": {"attributes": _otlp_attributes({
                "service.name": "vera", "vera.telemetry.schema": SCHEMA,
            })},
            "scopeSpans": [{
                "scope": {"name": "vera.execution", "version": "1"},
                "spans": spans,
            }],
        }],
    }


async def _http_json_sender(endpoint: str, body: bytes, headers: Mapping[str, str],
                            timeout_seconds: float) -> tuple[int, bytes]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
        response = await client.post(endpoint, content=body, headers=dict(headers))
        return int(response.status_code), response.content[:65_536]


def _transport_response(value: Any) -> tuple[int, int]:
    status_code, response_body = (value if isinstance(value, tuple) else (value, b""))
    rejected = 0
    if int(status_code) == 200 and response_body:
        try:
            decoded = json.loads(response_body)
            partial = decoded.get("partialSuccess") or decoded.get("partial_success") or {}
            rejected = max(0, int(partial.get("rejectedSpans") or
                                  partial.get("rejected_spans") or 0))
        except (TypeError, ValueError, json.JSONDecodeError):
            # A malformed success body is not proof the collector accepted data.
            return int(status_code), -1
    return int(status_code), rejected


def exporter_status(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    config = exporter_config(environ)
    with _EXPORT_STATS_LOCK:
        stats = dict(_EXPORT_STATS)
    queue = globals().get("TELEMETRY_QUEUE")
    return {
        "state": "configured" if config.enabled else config.reason,
        "enabled": config.enabled,
        "protocol": config.protocol,
        "endpoint_scheme": urlsplit(config.endpoint).scheme if config.endpoint else "",
        "endpoint_host_ref": _digest(urlsplit(config.endpoint).hostname, 12)
        if config.endpoint else "",
        "timeout_ms": int(config.timeout_seconds * 1000),
        "max_request_bytes": config.max_request_bytes,
        "header_count": len(config.headers),
        "content_redacted": True,
        "queue": queue.status() if queue is not None else {
            "enabled": False, "reason": "not_initialized"},
        **stats,
    }


def _export_validation_error(trace: Mapping[str, Any]) -> str:
    if trace.get("schema") != SCHEMA or trace.get("content_redacted") is not True:
        return "untrusted_projection"
    spans = trace.get("spans")
    if not isinstance(spans, list) or not 1 <= len(spans) <= 200:
        return "invalid_span_batch"
    for span in spans:
        if not isinstance(span, Mapping) or not span.get("end_time"):
            return "incomplete_span"
        attributes = span.get("attributes")
        events = span.get("events")
        if not isinstance(attributes, Mapping) or not isinstance(events, list):
            return "invalid_span_shape"
        if len(events) > MAX_EVENTS_PER_SPAN:
            return "invalid_span_shape"
        if any(not (str(key).startswith("vera.") or key in {
                "openinference.span.kind", "error.type"}) for key in attributes):
            return "untrusted_projection"
        for event in events:
            event_attributes = event.get("attributes") if isinstance(event, Mapping) else None
            if not isinstance(event_attributes, Mapping) or any(
                    not str(key).startswith("vera.") for key in event_attributes):
                return "untrusted_projection"
    return ""


async def export_trace(trace: Mapping[str, Any], *, sender=None,
                       environ: Mapping[str, str] | None = None,
                       record_stats: bool = True) -> dict[str, Any]:
    """Best-effort bounded export. Transport failures are data, never exceptions."""
    config = exporter_config(environ)
    span_count = len(trace.get("spans") or [])
    if not config.enabled:
        return {"ok": False, "exported": False, "reason": config.reason,
                "span_count": span_count, "content_redacted": True}
    validation_error = _export_validation_error(trace)
    if validation_error:
        return {"ok": False, "exported": False, "reason": validation_error,
                "span_count": span_count, "content_redacted": True}
    preparation_started = time.perf_counter()
    request = to_otlp_json(trace)
    body = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    preparation_ms = round((time.perf_counter() - preparation_started) * 1000, 3)
    if len(body) > config.max_request_bytes:
        return {"ok": False, "exported": False, "reason": "request_too_large",
                "span_count": span_count, "request_bytes": len(body),
                "max_request_bytes": config.max_request_bytes,
                "preparation_ms": preparation_ms,
                "content_redacted": True}
    transport = sender or _http_json_sender
    headers = {**dict(config.headers), "content-type": "application/json"}
    if record_stats:
        with _EXPORT_STATS_LOCK:
            _EXPORT_STATS["attempts"] += 1
    started = time.perf_counter()
    try:
        response = await transport(config.endpoint, body, headers,
                                   config.timeout_seconds)
        status_code, rejected_spans = _transport_response(response)
        accepted = status_code == 200 and rejected_spans == 0
        partial = status_code == 200 and rejected_spans > 0
        exported = accepted or (partial and rejected_spans < span_count)
        if record_stats:
            with _EXPORT_STATS_LOCK:
                _EXPORT_STATS["accepted" if accepted else "failed"] += 1
                _EXPORT_STATS["last_status"] = (
                    "accepted" if accepted else ("partial" if partial else "rejected"))
                _EXPORT_STATS["last_error_type"] = (
                    "" if accepted else ("partial_success" if partial else
                                          ("invalid_response" if rejected_spans < 0
                                           else "http_status")))
        reason = ("accepted" if accepted else
                  ("partial_success" if partial else
                   ("invalid_collector_response" if rejected_spans < 0
                    else "collector_rejected")))
        return {"ok": accepted, "exported": exported, "reason": reason,
                "status_code": status_code, "rejected_spans": max(0, rejected_spans),
                "span_count": span_count,
                "request_bytes": len(body), "preparation_ms": preparation_ms,
                "content_redacted": True}
    except Exception as exc:
        if record_stats:
            with _EXPORT_STATS_LOCK:
                _EXPORT_STATS["failed"] += 1
                _EXPORT_STATS["last_status"] = "failed"
                _EXPORT_STATS["last_error_type"] = type(exc).__name__
        return {"ok": False, "exported": False, "reason": "transport_failure",
                "error_type": type(exc).__name__, "span_count": span_count,
                "request_bytes": len(body), "preparation_ms": preparation_ms,
                "content_redacted": True}
    finally:
        if record_stats:
            with _EXPORT_STATS_LOCK:
                _EXPORT_STATS["last_duration_ms"] = round(
                    (time.perf_counter() - started) * 1000, 3)
                _EXPORT_STATS["last_span_count"] = span_count


def merge_traces(traces: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Combine already-redacted traces into one bounded OTLP export unit."""
    spans = []
    for trace in traces:
        spans.extend(list(trace.get("spans") or []))
    return {
        "schema": SCHEMA, "authoritative": False, "offline": True,
        "exported": False, "content_redacted": True,
        "span_count": len(spans), "truncated": False, "spans": spans,
    }


class PortableTelemetryQueue:
    """One-process, non-blocking, bounded queue for terminal root Runs."""

    def __init__(self, *, sender=None, environ: Mapping[str, str] | None = None,
                 retry_jitter=None) -> None:
        self.sender = sender
        self.environ = environ
        self.retry_jitter = retry_jitter or (lambda: random.uniform(0.8, 1.2))
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._closing = False
        self._stats = {
            "offered": 0, "enqueued": 0, "deduplicated": 0,
            "dropped": 0, "exported_traces": 0, "failed_traces": 0,
            "exported_batches": 0, "failed_batches": 0,
            "retries": 0,
            "last_result": "never",
        }

    def _config(self) -> QueueConfig:
        return queue_config(self.environ)

    def status(self) -> dict[str, Any]:
        config = self._config()
        return {
            "enabled": config.enabled, "reason": config.reason,
            "max_queue": config.max_queue, "batch_size": config.batch_size,
            "flush_ms": int(config.flush_seconds * 1000),
            "retry_max": config.retry_max,
            "queue_depth": self._queue.qsize() if self._queue else 0,
            "worker_running": bool(self._worker and not self._worker.done()),
            "closing": self._closing, **dict(self._stats),
        }

    def offer_run(self, run: Run, children: Iterable[Run] = ()) -> bool:
        config = self._config()
        if (not config.enabled or self._closing or run.parent_run_id or
                run.status not in {RunStatus.COMPLETED, RunStatus.FAILED,
                                   RunStatus.CANCELLED, RunStatus.TIMED_OUT,
                                   RunStatus.SKIPPED}):
            return False
        return self.offer(project_run_trace(run, children), key=run.id)

    def offer(self, trace: Mapping[str, Any], *, key: str) -> bool:
        config = self._config()
        if not config.enabled or self._closing:
            return False
        self._stats["offered"] += 1
        if key in self._seen:
            self._stats["deduplicated"] += 1
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._stats["dropped"] += 1
            self._stats["last_result"] = "no_running_loop"
            return False
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=config.max_queue)
        try:
            self._queue.put_nowait(dict(trace))
        except asyncio.QueueFull:
            self._stats["dropped"] += 1
            self._stats["last_result"] = "queue_full"
            return False
        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > config.dedupe_limit:
            self._seen.popitem(last=False)
        self._stats["enqueued"] += 1
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._run(), name="vera-otlp-export")
        return True

    async def _send_with_retry(self, trace: Mapping[str, Any]) -> dict[str, Any]:
        config = self._config()
        retryable_statuses = {429, 502, 503, 504}
        for attempt in range(config.retry_max + 1):
            result = await export_trace(trace, sender=self.sender, environ=self.environ)
            retryable = (result.get("reason") == "transport_failure" or
                         result.get("status_code") in retryable_statuses)
            if result.get("ok") or not retryable or attempt >= config.retry_max:
                return result
            self._stats["retries"] += 1
            if config.retry_backoff_seconds:
                await asyncio.sleep(config.retry_backoff_seconds * (2 ** attempt) *
                                    float(self.retry_jitter()))
        return result

    async def _export_batch(self, traces: list[dict[str, Any]]) -> None:
        result = await self._send_with_retry(merge_traces(traces))
        results = [result]
        if result.get("reason") == "request_too_large" and len(traces) > 1:
            results = [await self._send_with_retry(trace) for trace in traces]
        if len(results) == 1:
            successful = len(traces) if result.get("ok") else 0
            failed = len(traces) - successful
        else:
            successful = sum(1 for item in results if item.get("ok"))
            failed = len(traces) - successful
        self._stats["exported_traces"] += successful
        self._stats["failed_traces"] += failed
        if failed:
            self._stats["failed_batches"] += 1
            self._stats["last_result"] = str(results[-1].get("reason") or "failed")
        else:
            self._stats["exported_batches"] += 1
            self._stats["last_result"] = "accepted"

    async def _run(self) -> None:
        assert self._queue is not None
        carry = None
        try:
            while True:
                first = carry if carry is not None else await self._queue.get()
                carry = None
                batch = [first]
                config = self._config()
                if config.flush_seconds:
                    await asyncio.sleep(config.flush_seconds)
                span_count = len(first.get("spans") or [])
                while len(batch) < config.batch_size:
                    try:
                        candidate = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    candidate_spans = len(candidate.get("spans") or [])
                    if span_count + candidate_spans > 200:
                        carry = candidate
                        break
                    batch.append(candidate)
                    span_count += candidate_spans
                try:
                    await self._export_batch(batch)
                except Exception as exc:
                    self._stats["failed_traces"] += len(batch)
                    self._stats["failed_batches"] += 1
                    self._stats["last_result"] = type(exc).__name__
                finally:
                    for _ in batch:
                        self._queue.task_done()
                if carry is not None:
                    # The carried item was removed from the queue and remains unfinished.
                    continue
        except asyncio.CancelledError:
            if carry is not None:
                self._queue.task_done()
            raise

    async def close(self, *, flush: bool = True, timeout_seconds: float = 2.0) -> None:
        self._closing = True
        if self._queue and flush:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=max(0.01, timeout_seconds))
            except asyncio.TimeoutError:
                self._stats["last_result"] = "shutdown_timeout"
        if self._worker and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass


TELEMETRY_QUEUE = PortableTelemetryQueue()
