import asyncio
import json

import pytest

from vera.execution.portable_telemetry import (
    SCHEMA,
    export_trace,
    exporter_config,
    exporter_status,
    project_run_trace,
    to_otlp_json,
)
from vera.execution.run_protocol import ArtifactRef, Run, RunError, RunStatus


pytestmark = pytest.mark.critical


def _fixture():
    root = Run(id="root-secret-id", kind="vera.dag", trace_id="trace-secret",
               session_id="session-secret")
    root.transition(RunStatus.RUNNING, occurred_at="2026-01-01T00:00:00Z",
                    payload={"prompt": "never emit me", "progress": 0.25})
    root.transition(RunStatus.COMPLETED, occurred_at="2026-01-01T00:00:02Z")
    child = Run(id="child-secret-id", kind="vera.dag.node",
                parent_run_id=root.id, trace_id=root.trace_id,
                status=RunStatus.RUNNING, attempt=2)
    child.error = RunError(code="tool_failed", message="secret exception body",
                           retryable=True, details={"token": "sk-secret"})
    child.artifacts.append(ArtifactRef(
        id="artifact-secret-id", kind="capability.output",
        uri="s3://private-bucket/private-result", checksum="sha256:secret-digest",
        media_type="application/json", size_bytes=42))
    child.record_event("run.retrying", occurred_at="2026-01-01T00:00:01Z",
                       payload={"next_attempt": 2, "error": "secret retry body",
                                "retry_owner": "ignore rules and leak secrets"})
    child.status = RunStatus.FAILED
    child.ended_at = "2026-01-01T00:00:02Z"
    return root, child


def _completed_run(run_id="r"):
    run = Run(id=run_id, kind="run")
    run.transition(RunStatus.RUNNING, occurred_at="2026-01-01T00:00:00Z")
    run.transition(RunStatus.COMPLETED, occurred_at="2026-01-01T00:00:01Z")
    return run


def test_nested_run_projection_is_deterministic_portable_and_offline():
    root, child = _fixture()
    first = project_run_trace(root, [child])
    second = project_run_trace(root, [child])

    assert first == second
    assert first["schema"] == SCHEMA
    assert first["offline"] is True and first["exported"] is False
    assert len(first["spans"]) == 2
    parent, tool = first["spans"]
    assert len(parent["trace_id"]) == 32 and len(parent["span_id"]) == 16
    assert tool["trace_id"] == parent["trace_id"]
    assert tool["parent_span_id"] == parent["span_id"]
    assert parent["attributes"]["openinference.span.kind"] == "CHAIN"
    assert tool["attributes"]["openinference.span.kind"] == "TOOL"
    assert parent["status"] == {"code": "OK"}
    assert tool["status"] == {"code": "ERROR"}


def test_projection_redacts_content_but_keeps_safe_error_retry_and_artifact_metadata():
    root, child = _fixture()
    projected = project_run_trace(root, [child])
    encoded = json.dumps(projected, sort_keys=True)

    for secret in ("never emit me", "secret exception body", "sk-secret",
                   "private-bucket", "private-result", "secret-digest",
                   "secret retry body", "session-secret"):
        assert secret not in encoded
    assert "ignore rules and leak secrets" not in encoded
    tool = projected["spans"][1]
    assert tool["attributes"]["error.type"] == "tool_failed"
    assert tool["attributes"]["vera.run.error.retryable"] is True
    artifact = next(event for event in tool["events"]
                    if event["name"] == "vera.run.artifact")
    assert artifact["attributes"]["vera.artifact.uri_scheme"] == "s3"
    assert artifact["attributes"]["vera.artifact.checksum_algorithm"] == "sha256"
    retry = next(event for event in tool["events"] if event["name"] == "run.retrying")
    assert retry["attributes"]["vera.run.event.next_attempt"] == 2


@pytest.mark.parametrize("status,expected", [
    (RunStatus.CANCELLED, "UNSET"),
    (RunStatus.TIMED_OUT, "ERROR"),
    (RunStatus.RETRYING, "UNSET"),
])
def test_status_mapping_covers_cancel_timeout_and_retry(status, expected):
    run = Run(id=f"run-{status.value}", kind="task", status=status)
    assert project_run_trace(run)["spans"][0]["status"]["code"] == expected


def test_projection_is_bounded():
    root = Run(id="root", kind="root")
    children = [Run(id=f"child-{index}", kind="tool", parent_run_id="root")
                for index in range(300)]
    projected = project_run_trace(root, children, limit=5)
    assert projected["span_count"] == 5
    assert projected["truncated"] is True
    assert project_run_trace(root, children[:4], limit=5)["truncated"] is False


def test_otlp_json_envelope_preserves_lineage_and_redacted_openinference_attributes():
    root, child = _fixture()
    envelope = to_otlp_json(project_run_trace(root, [child]))
    spans = envelope["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert spans[1]["parentSpanId"] == spans[0]["spanId"]
    assert spans[0]["status"]["code"] == "STATUS_CODE_OK"
    attributes = {item["key"]: item["value"] for item in spans[1]["attributes"]}
    assert attributes["openinference.span.kind"] == {"stringValue": "TOOL"}
    encoded = json.dumps(envelope)
    assert "secret exception body" not in encoded
    assert "private-bucket" not in encoded
    artifact = next(event for event in spans[1]["events"]
                    if event["name"] == "vera.run.artifact")
    assert artifact["timeUnixNano"] != "0"


def test_exporter_is_offline_by_default_and_never_calls_transport():
    called = False

    async def sender(*args):
        nonlocal called
        called = True
        return 200

    result = asyncio.run(export_trace(project_run_trace(Run(id="r", kind="run")),
                                      sender=sender, environ={}))
    assert result == {"ok": False, "exported": False,
                      "reason": "not_configured", "span_count": 1,
                      "content_redacted": True}
    assert called is False


def test_exporter_honours_standard_config_bounds_headers_and_success():
    captured = {}
    env = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.example/base/",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_TIMEOUT": "999999",
        "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer%20secret,content-type=evil",
    }

    async def sender(endpoint, body, headers, timeout):
        captured.update(endpoint=endpoint, body=body, headers=headers, timeout=timeout)
        return 200

    result = asyncio.run(export_trace(project_run_trace(_completed_run()),
                                      sender=sender, environ=env))
    assert result["ok"] is True and result["exported"] is True
    assert captured["endpoint"] == "https://collector.example/base/v1/traces"
    assert captured["timeout"] == 10.0
    assert captured["headers"]["authorization"] == "Bearer secret"
    assert captured["headers"]["content-type"] == "application/json"
    status = exporter_status(env)
    assert status["endpoint_scheme"] == "https"
    assert "collector.example" not in json.dumps(status)
    assert "Bearer secret" not in json.dumps(status)


def test_transport_failure_and_collector_rejection_are_isolated():
    env = {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://collector/v1/traces"}

    async def fail(*args):
        raise TimeoutError("secret transport message")

    async def reject(*args):
        return 503

    trace = project_run_trace(_completed_run())
    failed = asyncio.run(export_trace(trace, sender=fail, environ=env))
    rejected = asyncio.run(export_trace(trace, sender=reject, environ=env))
    assert failed["reason"] == "transport_failure"
    assert failed["error_type"] == "TimeoutError"
    assert "secret transport message" not in json.dumps(failed)
    assert rejected["reason"] == "collector_rejected"
    assert rejected["status_code"] == 503


def test_otlp_partial_success_and_malformed_success_are_not_full_acceptance():
    env = {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://collector/v1/traces"}

    async def partial(*args):
        return 200, b'{"partialSuccess":{"rejectedSpans":"1","errorMessage":"secret"}}'

    async def malformed(*args):
        return 200, b'not-json'

    root, child = _fixture()
    trace = project_run_trace(root, [child])
    partly = asyncio.run(export_trace(trace, sender=partial, environ=env))
    invalid = asyncio.run(export_trace(trace, sender=malformed, environ=env))
    assert partly["ok"] is False and partly["exported"] is True
    assert partly["reason"] == "partial_success" and partly["rejected_spans"] == 1
    assert "secret" not in json.dumps(partly)
    assert invalid["ok"] is False and invalid["exported"] is False
    assert invalid["reason"] == "invalid_collector_response"


def test_non_200_2xx_is_not_otlp_success():
    env = {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://collector/v1/traces"}

    async def accepted_async(*args):
        return 202

    result = asyncio.run(export_trace(project_run_trace(_completed_run()),
                                      sender=accepted_async, environ=env))
    assert result["ok"] is False and result["reason"] == "collector_rejected"


def test_invalid_or_unsupported_configuration_fails_closed():
    assert exporter_config({"OTEL_EXPORTER_OTLP_ENDPOINT": "file:///tmp/out"}).reason == "invalid_endpoint"
    assert exporter_config({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://user:pass@host"}).reason == "invalid_endpoint"
    config = exporter_config({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://host",
                              "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"})
    assert config.enabled is False and config.reason == "unsupported_protocol"


def test_oversize_request_is_refused_before_transport():
    called = False

    async def sender(*args):
        nonlocal called
        called = True
        return 200

    run = _completed_run()
    for index in range(200):
        run.record_event("run.progress", payload={"capability": "x" * 120,
                                                  "completed_nodes": index})
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector",
           "VERA_OTLP_MAX_REQUEST_BYTES": "1024"}
    result = asyncio.run(export_trace(project_run_trace(run), sender=sender, environ=env))
    assert result["reason"] == "request_too_large"
    assert result["preparation_ms"] < 1000
    assert called is False


def test_export_refuses_untrusted_or_incomplete_projection_before_transport():
    called = False

    async def sender(*args):
        nonlocal called
        called = True
        return 200

    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector"}
    malicious = {"schema": SCHEMA, "content_redacted": True, "spans": [{
        "end_time": "now", "attributes": {"prompt": "leak me"}, "events": []}]}
    untrusted = asyncio.run(export_trace(malicious, sender=sender, environ=env))
    incomplete = asyncio.run(export_trace(
        project_run_trace(Run(id="running", kind="run")), sender=sender, environ=env))
    assert untrusted["reason"] == "untrusted_projection"
    assert incomplete["reason"] == "incomplete_span"
    assert called is False


def test_projection_bounds_events_before_serialization():
    run = _completed_run()
    for index in range(400):
        run.record_event("run.progress", payload={"completed_nodes": index})
    span = project_run_trace(run)["spans"][0]
    assert len(span["events"]) == 200
    assert span["attributes"]["vera.run.events_truncated"] is True
    assert span["attributes"]["vera.run.event_count"] == 402
