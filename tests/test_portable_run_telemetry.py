import json

import pytest

from vera.execution.portable_telemetry import SCHEMA, project_run_trace
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
