import copy

import pytest

from vera.execution.observability_comparison import (
    LatencyEvidence,
    ObservabilityBackendEvidence,
    ObservabilityBackendProfile,
    ObservabilityComparison,
    ObservabilityFixture,
    backend_evidence_from_dict,
    observability_fixture_from_dict,
)
from vera.execution.portable_telemetry import project_run_trace
from vera.execution.run_protocol import Run, RunStatus


pytestmark = pytest.mark.critical


def _trace():
    root = Run(id="root", kind="vera.workflow", trace_id="shared-trace")
    root.transition(RunStatus.RUNNING, occurred_at="2026-09-12T00:00:00Z",
                    payload={"prompt": "must not survive", "progress": 0.5})
    root.transition(RunStatus.COMPLETED, occurred_at="2026-09-12T00:00:02Z")
    child = Run(id="child", kind="vera.workflow.step", parent_run_id="root",
                trace_id="shared-trace", status=RunStatus.COMPLETED,
                started_at="2026-09-12T00:00:00Z",
                ended_at="2026-09-12T00:00:01Z")
    return project_run_trace(root, (child,))


def _fixture():
    return ObservabilityFixture.from_portable_trace("workflow-success", _trace())


def _evidence(fixture, kind, **overrides):
    values = {
        "fixture_id": fixture.fixture_id,
        "portable_trace_digest": fixture.portable_trace_digest,
        "otlp_export_digest": fixture.otlp_export_digest,
        "profile": ObservabilityBackendProfile(
            backend_id=f"{kind}-local", kind=kind, revision="v1"),
        "status": "completed",
        "observed_span_count": fixture.span_count,
        "preserved_parent_links": fixture.parent_link_count,
        "preserved_events": fixture.event_count,
        "preserved_attributes": fixture.attribute_count,
        "preserved_evaluation_links": fixture.evaluation_link_count,
        "query_dimensions": ("trace_id", "run_status", "time_range"),
        "ui_views": ("trace_tree", "span_detail", "search"),
        "ingest_latency": LatencyEvidence(3, 10, 20),
        "query_latency": LatencyEvidence(3, 15, 30),
        "retention_days": 30,
        "access_model": "rbac",
        "standards_export": True,
        "deletion_supported": True,
        "storage_bytes": 4096,
        "peak_memory_bytes": 8192,
        "cpu_milliseconds": 500,
        "backend_specific_field_count": 0,
        "failure_blocks_vera_runs": False,
        "recovery_ms": 100,
        "teardown_ms": 200,
        "residual_resource_count": 0,
    }
    values.update(overrides)
    return ObservabilityBackendEvidence(**values)


def test_fixture_is_stable_payload_free_and_binds_otlp_export():
    first = _fixture()
    second = _fixture()

    assert first == second
    assert first.fixture_id.startswith("ofix_")
    assert first.span_count == 2
    assert first.parent_link_count == 1
    assert first.portable_trace_digest.startswith("sha256:")
    assert first.otlp_export_digest.startswith("sha256:")
    assert "must not survive" not in repr(first.to_dict())


def test_fixture_rejects_payload_attributes_and_forged_shape():
    payload = _trace()
    payload["spans"][0]["attributes"]["llm.input.value"] = "private prompt"
    with pytest.raises(ValueError, match="payload-bearing"):
        ObservabilityFixture.from_portable_trace("unsafe", payload)

    forged = _trace()
    forged["span_count"] = 99
    with pytest.raises(ValueError, match="span count"):
        ObservabilityFixture.from_portable_trace("forged", forged)


def test_comparison_keeps_decision_dimensions_separate_and_has_no_authority():
    fixture = _fixture()
    langfuse = _evidence(fixture, "langfuse")
    phoenix = _evidence(
        fixture, "phoenix", preserved_events=max(0, fixture.event_count - 1),
        query_dimensions=("trace_id", "evaluation_id"),
        retention_days=None, standards_export=None, storage_bytes=None,
    )

    report = ObservabilityComparison(fixture, (phoenix, langfuse)).report()

    assert list(report["backends"]) == ["langfuse", "phoenix"]
    assert report["winner"] is None
    assert report["effect"] == "none"
    assert report["backends_invoked"] is False
    assert report["activation_authority"] is False
    langfuse_report = report["backends"]["langfuse"]
    assert langfuse_report["trace_fidelity"]["spans"] == {
        "expected": 2, "preserved": 2, "missing": 0,
    }
    assert langfuse_report["query_ui_value"] == {
        "dimensions": ["run_status", "time_range", "trace_id"],
        "views": ["search", "span_detail", "trace_tree"],
        "query_latency": {"samples": 3, "p50_ms": 15, "p95_ms": 30},
    }
    assert langfuse_report["governance"]["retention_days"] == 30
    assert langfuse_report["resources"]["storage_bytes"] == 4096
    assert langfuse_report["resources"]["cpu_milliseconds"] == 500
    assert langfuse_report["portability"] == {
        "ingest_protocol": "otlp_http_json",
        "semantic_convention": "openinference",
        "standards_export": True,
        "backend_specific_field_count": 0,
    }
    assert langfuse_report["outage_isolation"]["failure_blocks_vera_runs"] is False
    assert langfuse_report["teardown"]["residual_resource_count"] == 0
    assert report["backends"]["phoenix"]["governance"]["retention_days"] is None


def test_both_backends_must_report_the_exact_same_export():
    fixture = _fixture()
    langfuse = _evidence(fixture, "langfuse")
    wrong_digest = "sha256:" + "0" * 64
    phoenix = _evidence(fixture, "phoenix", otlp_export_digest=wrong_digest)

    with pytest.raises(ValueError, match="identical fixture export"):
        ObservabilityComparison(fixture, (langfuse, phoenix))


def test_comparison_requires_exactly_langfuse_and_phoenix():
    fixture = _fixture()
    first = _evidence(fixture, "langfuse")
    duplicate = _evidence(
        fixture, "langfuse",
        profile=ObservabilityBackendProfile("langfuse-second", "langfuse", "v2"),
    )

    with pytest.raises(ValueError, match="exactly Langfuse and Phoenix"):
        ObservabilityComparison(fixture, (first, duplicate))
    with pytest.raises(ValueError, match="requires Langfuse and Phoenix"):
        ObservabilityComparison(fixture, (first,))


def test_backend_cannot_claim_more_evidence_than_fixture_contains():
    fixture = _fixture()
    langfuse = _evidence(fixture, "langfuse")
    phoenix = _evidence(
        fixture, "phoenix", preserved_attributes=fixture.attribute_count + 1)

    with pytest.raises(ValueError, match="cannot exceed"):
        ObservabilityComparison(fixture, (langfuse, phoenix))


def test_failed_import_is_explicit_and_cannot_claim_preserved_spans():
    fixture = _fixture()
    with pytest.raises(ValueError, match="unfinished evidence"):
        _evidence(
            fixture, "phoenix", status="failed", error_code="ingest_rejected",
            observed_span_count=1, preserved_parent_links=0,
            preserved_events=0, preserved_attributes=0,
            query_dimensions=(), ui_views=(), ingest_latency=LatencyEvidence(),
            query_latency=LatencyEvidence(),
        )

    failed = _evidence(
        fixture, "phoenix", status="failed", error_code="ingest_rejected",
        observed_span_count=0, preserved_parent_links=0,
        preserved_events=0, preserved_attributes=0,
        query_dimensions=(), ui_views=(), ingest_latency=LatencyEvidence(),
        query_latency=LatencyEvidence(),
    )
    assert failed.error_code == "ingest_rejected"


@pytest.mark.parametrize("kind", ["custom", "grafana", "worldview"])
def test_backend_kind_is_deliberately_bounded_for_this_decision(kind):
    with pytest.raises(ValueError, match="langfuse or phoenix"):
        ObservabilityBackendProfile("candidate", kind, "v1")


def test_validation_rejects_bools_as_counts_and_invalid_latency():
    with pytest.raises(ValueError, match="non-negative integer"):
        LatencyEvidence(samples=True)
    with pytest.raises(ValueError, match="p50 cannot exceed"):
        LatencyEvidence(samples=2, p50_ms=20, p95_ms=10)


def test_fixture_and_backend_evidence_round_trip_strictly():
    fixture = _fixture()
    evidence = _evidence(fixture, "langfuse")

    assert observability_fixture_from_dict(fixture.to_dict()) == fixture
    assert backend_evidence_from_dict(evidence.to_dict()) == evidence

    unsafe = copy.deepcopy(evidence.to_dict())
    unsafe["raw_trace"] = {"prompt": "do not import"}
    with pytest.raises(ValueError, match="exactly the supported fields"):
        backend_evidence_from_dict(unsafe)

    tampered = copy.deepcopy(evidence.to_dict())
    tampered["resources"]["storage_bytes"] += 1
    with pytest.raises(ValueError, match="identity mismatch"):
        backend_evidence_from_dict(tampered)
    unsafe["raw_trace"] = {"prompt": "do not import"}
    with pytest.raises(ValueError, match="exactly the supported fields"):
        backend_evidence_from_dict(unsafe)

    forged = copy.deepcopy(evidence.to_dict())
    forged["resources"]["storage_bytes"] += 1
    with pytest.raises(ValueError, match="identity mismatch"):
        backend_evidence_from_dict(forged)


def test_source_never_imports_or_invokes_backend_libraries():
    from pathlib import Path
    import vera.execution.observability_comparison as module

    source = Path(module.__file__).read_text(encoding="utf-8").lower()
    assert "import langfuse" not in source
    assert "import phoenix" not in source
    assert "requests." not in source
    assert "httpx." not in source
