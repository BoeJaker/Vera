"""Deterministic evaluator for the frozen W1-06 portable telemetry gate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .portable_telemetry import (
    PortableTelemetryQueue,
    export_trace,
    exporter_status,
    project_run_trace,
    to_otlp_json,
)
from .run_protocol import ArtifactRef, Run, RunError, RunStatus


CORPUS_SCHEMA = "vera.run-telemetry-corpus/v1"
REPORT_SCHEMA = "vera.run-telemetry-report/v1"
REQUIRED_GATES = frozenset({
    "otlp_shape", "correlation", "terminal_semantics", "redaction",
    "failure_isolation", "bounded_work", "default_off",
})
OPERATIONS = frozenset({
    "golden_otlp", "nested_lineage", "terminal_outcomes", "redaction",
    "failure_isolation", "bounded_work", "default_off",
})
_FIXED_START = "2026-01-01T00:00:00Z"
_FIXED_MIDDLE = "2026-01-01T00:00:01Z"
_FIXED_END = "2026-01-01T00:00:02Z"
_CONFIGURED_ENV = {
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.invalid",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
}
_STAT_KEYS = (
    "attempts", "accepted", "failed", "last_status", "last_error_type",
    "last_duration_ms", "last_span_count",
)


def load_telemetry_corpus(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("corpus_not_object")
    return value


def validate_telemetry_corpus(corpus: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if corpus.get("schema") != CORPUS_SCHEMA:
        errors.append("schema_invalid")
    if corpus.get("policy") != {
            "executes_capabilities": False, "uses_network": False,
            "uses_external_secrets": False, "measures_wall_clock": False}:
        errors.append("policy_invalid")
    declared = corpus.get("required_gates")
    if not isinstance(declared, list) or set(declared) != REQUIRED_GATES:
        errors.append("required_gates_invalid")
    cases = corpus.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("cases_invalid")
        cases = []
    ids: set[str] = set()
    covered: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"cases[{index}]_invalid")
            continue
        case_id = case.get("id")
        gate = case.get("gate")
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            errors.append(f"cases[{index}].id_invalid")
        else:
            ids.add(case_id)
        if gate not in REQUIRED_GATES:
            errors.append(f"cases[{index}].gate_invalid")
        else:
            covered.add(gate)
        if case.get("operation") not in OPERATIONS:
            errors.append(f"cases[{index}].operation_invalid")
        if not isinstance(case.get("expected"), dict):
            errors.append(f"cases[{index}].expected_invalid")
    missing = sorted(REQUIRED_GATES - covered)
    if missing:
        errors.append("gate_coverage_missing:" + ",".join(missing))
    return {"valid": not errors, "errors": errors, "case_count": len(cases),
            "gate_coverage": sorted(covered), "missing_gates": missing}


def _completed(run_id: str, *, kind: str = "vera.dag", trace_id: str = "") -> Run:
    run = Run(id=run_id, kind=kind, trace_id=trace_id)
    run.transition(RunStatus.RUNNING, event_type="run.started",
                   occurred_at=_FIXED_START)
    run.transition(RunStatus.COMPLETED, event_type="run.completed",
                   occurred_at=_FIXED_END)
    return run


def _attributes(items: list[dict[str, Any]]) -> dict[str, Any]:
    values = {}
    for item in items:
        encoded = item["value"]
        values[item["key"]] = next(iter(encoded.values()))
    return values


def _golden_otlp() -> dict[str, Any]:
    envelope = to_otlp_json(project_run_trace(
        _completed("root-fixture", trace_id="trace-fixture")))
    span = envelope["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    attrs = _attributes(span["attributes"])
    return {
        "trace_id": span["traceId"], "span_id": span["spanId"],
        "parent_span_id": span.get("parentSpanId", ""), "name": span["name"],
        "kind": span["kind"], "start_nanos": span["startTimeUnixNano"],
        "end_nanos": span["endTimeUnixNano"], "status": span["status"]["code"],
        "openinference_kind": attrs["openinference.span.kind"],
        "event_names": [event["name"] for event in span["events"]],
    }


def _nested_fixture() -> tuple[Run, Run]:
    root = _completed("root-fixture", trace_id="trace-fixture")
    child = Run(id="child-fixture", kind="vera.dag.node",
                parent_run_id=root.id, trace_id=root.trace_id)
    child.transition(RunStatus.RUNNING, event_type="run.started",
                     occurred_at=_FIXED_START)
    child.error = RunError(code="tool_failed", message="secret-exception",
                           retryable=True, details={"token": "secret-token"})
    child.transition(RunStatus.RETRYING, event_type="run.retrying",
                     occurred_at=_FIXED_MIDDLE,
                     payload={"next_attempt": 2, "error": "secret-retry"})
    child.transition(RunStatus.RUNNING, event_type="run.retry.started",
                     occurred_at=_FIXED_MIDDLE, payload={"attempt": 2})
    child.artifacts.append(ArtifactRef(
        id="secret-artifact", kind="capability.output",
        uri="s3://secret-bucket/secret-result", checksum="sha256:secret-checksum",
        media_type="application/json", size_bytes=42))
    child.transition(RunStatus.FAILED, event_type="run.failed",
                     occurred_at=_FIXED_END)
    return root, child


def _nested_lineage() -> dict[str, Any]:
    root, child = _nested_fixture()
    spans = project_run_trace(root, [child])["spans"]
    parent, tool = spans
    retry = next(event for event in tool["events"] if event["name"] == "run.retrying")
    artifact = next(event for event in tool["events"]
                    if event["name"] == "vera.run.artifact")
    return {
        "span_count": len(spans), "same_trace": parent["trace_id"] == tool["trace_id"],
        "child_parent_matches": tool["parent_span_id"] == parent["span_id"],
        "root_kind": parent["attributes"]["openinference.span.kind"],
        "child_kind": tool["attributes"]["openinference.span.kind"],
        "child_status": tool["status"]["code"],
        "error_type": tool["attributes"]["error.type"],
        "retry_attempt": retry["attributes"]["vera.run.event.next_attempt"],
        "artifact_scheme": artifact["attributes"]["vera.artifact.uri_scheme"],
        "checksum_algorithm": artifact["attributes"]["vera.artifact.checksum_algorithm"],
    }


def _terminal_run(run_id: str, status: RunStatus) -> Run:
    run = Run(id=run_id, kind="run")
    run.transition(RunStatus.RUNNING, occurred_at=_FIXED_START)
    run.transition(status, occurred_at=_FIXED_END)
    return run


def _terminal_outcomes() -> dict[str, Any]:
    retry = Run(id="retry", kind="run")
    retry.transition(RunStatus.RUNNING, occurred_at=_FIXED_START)
    retry.transition(RunStatus.RETRYING, event_type="run.retrying",
                     occurred_at=_FIXED_MIDDLE, payload={"next_attempt": 2})
    retry.transition(RunStatus.RUNNING, occurred_at=_FIXED_MIDDLE,
                     payload={"attempt": 2})
    retry.transition(RunStatus.FAILED, occurred_at=_FIXED_END)
    return {
        "completed": project_run_trace(_terminal_run("ok", RunStatus.COMPLETED))
        ["spans"][0]["status"]["code"],
        "cancelled": project_run_trace(_terminal_run("cancel", RunStatus.CANCELLED))
        ["spans"][0]["status"]["code"],
        "timed_out": project_run_trace(_terminal_run("timeout", RunStatus.TIMED_OUT))
        ["spans"][0]["status"]["code"],
        "failed": project_run_trace(retry)["spans"][0]["status"]["code"],
        "retry_event": any(event["name"] == "run.retrying"
                           for event in project_run_trace(retry)["spans"][0]["events"]),
    }


def _redaction() -> dict[str, Any]:
    root, child = _nested_fixture()
    root.events[0].payload["prompt"] = "secret-prompt"
    portable = project_run_trace(root, [child])
    portable_text = json.dumps(portable, sort_keys=True)
    otlp_text = json.dumps(to_otlp_json(portable), sort_keys=True)
    forbidden = ["secret-prompt", "secret-exception", "secret-token", "secret-retry",
                 "secret-bucket", "secret-result", "secret-checksum"]
    return {
        "portable_clean": not any(item in portable_text for item in forbidden),
        "otlp_clean": not any(item in otlp_text for item in forbidden),
        "forbidden_matches": sum(item in portable_text or item in otlp_text
                                 for item in forbidden),
        "content_redacted": portable["content_redacted"] is True,
    }


def _stats_snapshot() -> tuple[Any, ...]:
    status = exporter_status()
    return tuple(status[key] for key in _STAT_KEYS)


async def _failure_isolation() -> dict[str, Any]:
    calls = 0

    async def sender(*args):
        nonlocal calls
        calls += 1
        raise TimeoutError("secret-transport-message")

    before = _stats_snapshot()
    result = await export_trace(project_run_trace(_completed("failure")), sender=sender,
                                environ=_CONFIGURED_ENV, record_stats=False)
    after = _stats_snapshot()
    return {
        "reason": result["reason"], "error_type": result["error_type"],
        "exported": result["exported"], "sender_calls": calls,
        "stats_unchanged": before == after,
        "message_absent": "secret-transport-message" not in json.dumps(result),
    }


async def _bounded_work() -> dict[str, Any]:
    calls = 0

    async def sender(*args):
        nonlocal calls
        calls += 1
        return 200

    root = Run(id="bounded-root", kind="run")
    root.transition(RunStatus.RUNNING, occurred_at=_FIXED_START)
    for index in range(400):
        root.record_event("run.progress", occurred_at=_FIXED_MIDDLE,
                          payload={"completed_nodes": index})
    root.transition(RunStatus.COMPLETED, occurred_at=_FIXED_END)
    children = [Run(id=f"child-{index}", kind="tool", parent_run_id=root.id,
                    status=RunStatus.COMPLETED, started_at=_FIXED_START,
                    ended_at=_FIXED_END) for index in range(300)]
    trace = project_run_trace(root, children, limit=200)
    result = await export_trace(
        trace, sender=sender,
        environ={**_CONFIGURED_ENV, "VERA_OTLP_MAX_REQUEST_BYTES": "1024"},
        record_stats=False)
    root_span = trace["spans"][0]
    return {
        "span_count": trace["span_count"], "trace_truncated": trace["truncated"],
        "root_events": len(root_span["events"]),
        "events_truncated": root_span["attributes"]["vera.run.events_truncated"],
        "reason": result["reason"], "sender_calls": calls,
        "has_preparation_measurement": isinstance(result.get("preparation_ms"), float),
    }


async def _default_off() -> dict[str, Any]:
    calls = 0

    async def sender(*args):
        nonlocal calls
        calls += 1
        return 200

    before = _stats_snapshot()
    result = await export_trace(project_run_trace(_completed("offline")), sender=sender,
                                environ={}, record_stats=False)
    queue = PortableTelemetryQueue(sender=sender, environ={}).status()
    return {
        "reason": result["reason"], "exported": result["exported"],
        "sender_calls": calls, "queue_enabled": queue["enabled"],
        "queue_reason": queue["reason"], "stats_unchanged": before == _stats_snapshot(),
    }


async def _evaluate_operation(operation: str) -> dict[str, Any]:
    if operation == "golden_otlp":
        return _golden_otlp()
    if operation == "nested_lineage":
        return _nested_lineage()
    if operation == "terminal_outcomes":
        return _terminal_outcomes()
    if operation == "redaction":
        return _redaction()
    if operation == "failure_isolation":
        return await _failure_isolation()
    if operation == "bounded_work":
        return await _bounded_work()
    if operation == "default_off":
        return await _default_off()
    raise ValueError("unsupported_operation")


async def evaluate_telemetry_corpus(corpus: dict[str, Any]) -> dict[str, Any]:
    validation = validate_telemetry_corpus(corpus)
    if not validation["valid"]:
        return {"schema": REPORT_SCHEMA, "ok": False, **validation,
                "executes_capabilities": False, "uses_network": False,
                "uses_external_secrets": False, "measures_wall_clock": False,
                "cases": []}
    results = []
    for case in corpus["cases"]:
        observation = await _evaluate_operation(case["operation"])
        results.append({
            "id": case["id"], "gate": case["gate"],
            "passed": observation == case["expected"],
            "reason_codes": [] if observation == case["expected"]
            else ["observation_mismatch"],
            "observation": observation,
        })
    passed = sum(item["passed"] for item in results)
    report = {
        "schema": REPORT_SCHEMA, "ok": passed == len(results),
        "revision": corpus.get("revision"), "total": len(results),
        "passed": passed, "failed": len(results) - passed,
        "gate_coverage": validation["gate_coverage"],
        "missing_gates": validation["missing_gates"],
        "executes_capabilities": False, "uses_network": False,
        "uses_external_secrets": False, "measures_wall_clock": False,
        "cases": results,
    }
    serialized = json.dumps(report, sort_keys=True)
    forbidden = ["secret-", "collector.invalid", "ignore instructions"]
    report["content_free"] = not any(value in serialized for value in forbidden)
    report["ok"] = report["ok"] and report["content_free"]
    return report
