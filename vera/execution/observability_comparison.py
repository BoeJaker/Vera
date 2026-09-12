"""Offline comparison evidence for standards-compatible observability backends."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import Any, Mapping, Optional

from .portable_telemetry import SCHEMA as PORTABLE_TRACE_SCHEMA, to_otlp_json


COMPARISON_SCHEMA = "vera.observability-comparison/v1"
FIXTURE_SCHEMA = "vera.observability-fixture/v1"
BACKEND_KINDS = frozenset({"langfuse", "phoenix"})
QUERY_DIMENSIONS = frozenset({
    "trace_id", "span_id", "parent_span_id", "span_kind", "run_kind",
    "run_status", "capability", "evaluation_id", "time_range",
})
UI_VIEWS = frozenset({
    "trace_tree", "span_detail", "search", "evaluation", "dashboard",
    "dataset", "session_timeline",
})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_HEX = re.compile(r"^[0-9a-f]+$")
_SAFE_SPAN_KEYS = {
    "name", "trace_id", "span_id", "parent_span_id", "span_kind",
    "start_time", "end_time", "status", "attributes", "events", "links",
}
_SAFE_EVENT_KEYS = {"name", "timestamp", "attributes"}
_SAFE_ATTRIBUTE_KEYS = {
    "openinference.span.kind", "error.type",
    "vera.run.protocol", "vera.run.kind", "vera.run.status",
    "vera.run.attempt", "vera.run.artifact_count", "vera.run.event_count",
    "vera.run.events_truncated", "vera.run.content_redacted",
    "vera.run.error.retryable", "vera.run.progress",
    "vera.run.event.sequence", "vera.run.event.status",
    "vera.run.event.attempt", "vera.run.event.next_attempt",
    "vera.run.event.retry_owner", "vera.run.event.progress",
    "vera.run.event.completed_nodes", "vera.run.event.total_nodes",
    "vera.run.event.capability", "vera.run.event.action",
    "vera.artifact.ref", "vera.artifact.kind", "vera.artifact.uri_scheme",
    "vera.artifact.media_type", "vera.artifact.size_bytes",
    "vera.artifact.checksum_algorithm",
    "openinference.eval.reference", "vera.evaluation.report_ref",
}


def _identifier(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not _ID.fullmatch(normalized):
        raise ValueError(f"{label} must be a bounded identifier")
    return normalized


def _nonnegative(value: Any, label: str, *, optional: bool = False) -> Optional[int]:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _mapping(value: Any, label: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{label} must contain exactly the supported fields")
    return dict(value)


def _bounded_primitive(value: Any, label: str) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise ValueError(f"{label} must be finite")
    if isinstance(value, str) and len(value) <= 512:
        return
    raise ValueError(f"{label} must be a bounded primitive")


def _validate_attributes(value: Any, *, require_redaction: bool = False) -> int:
    if not isinstance(value, Mapping):
        raise ValueError("trace attributes must be an object")
    attributes = dict(value)
    if not set(attributes) <= _SAFE_ATTRIBUTE_KEYS:
        raise ValueError("trace contains an unsupported or payload-bearing attribute")
    for key, item in attributes.items():
        _bounded_primitive(item, key)
    if require_redaction and attributes.get("vera.run.content_redacted") is not True:
        raise ValueError("every span must attest content redaction")
    return len(attributes)


def _validate_hex(value: Any, length: int, label: str, *, optional: bool = False) -> str:
    normalized = str(value or "").lower()
    if optional and not normalized:
        return ""
    if len(normalized) != length or not _HEX.fullmatch(normalized):
        raise ValueError(f"{label} must be {length} lowercase hexadecimal characters")
    return normalized


@dataclass(frozen=True)
class ObservabilityFixture:
    """Payload-free identity and expectations for one portable trace export."""

    fixture_key: str
    portable_trace_digest: str
    otlp_export_digest: str
    span_count: int
    parent_link_count: int
    event_count: int
    attribute_count: int
    evaluation_link_count: int = 0
    fixture_id: str = field(init=False)
    schema: str = FIXTURE_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "fixture_key", _identifier(
            self.fixture_key, "fixture key"))
        for name in ("portable_trace_digest", "otlp_export_digest"):
            value = str(getattr(self, name) or "").lower()
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
                raise ValueError(f"{name} must be a sha256 digest")
            object.__setattr__(self, name, value)
        for name in ("span_count", "parent_link_count", "event_count",
                     "attribute_count", "evaluation_link_count"):
            object.__setattr__(self, name, _nonnegative(getattr(self, name), name))
        if not self.span_count:
            raise ValueError("observability fixtures require at least one span")
        if self.parent_link_count >= self.span_count:
            raise ValueError("parent link count cannot equal or exceed span count")
        if self.evaluation_link_count > self.attribute_count:
            raise ValueError("evaluation links cannot exceed fixture attributes")
        object.__setattr__(self, "fixture_id", _identity("ofix_", self.identity_dict()))

    @classmethod
    def from_portable_trace(cls, fixture_key: str,
                            trace: Mapping[str, Any]) -> "ObservabilityFixture":
        if not isinstance(trace, Mapping):
            raise TypeError("trace must be a mapping")
        trace = dict(trace)
        if trace.get("schema") != PORTABLE_TRACE_SCHEMA:
            raise ValueError("unsupported portable trace schema")
        if (trace.get("content_redacted") is not True or
                trace.get("authoritative") is not False or
                trace.get("offline") is not True or
                trace.get("exported") is not False):
            raise ValueError("fixture must be offline, non-authoritative and content-redacted")
        if set(trace) != {"schema", "authoritative", "offline", "exported",
                          "content_redacted", "span_count", "truncated", "spans"}:
            raise ValueError("portable trace contains unsupported top-level fields")
        spans = trace.get("spans")
        if not isinstance(spans, list) or not 0 < len(spans) <= 200:
            raise ValueError("portable trace must contain 1..200 spans")
        if trace.get("span_count") != len(spans):
            raise ValueError("portable trace span count does not match spans")
        span_ids: set[str] = set()
        parents: list[str] = []
        events = attributes = evaluations = 0
        trace_ids: set[str] = set()
        for span in spans:
            if not isinstance(span, Mapping) or set(span) != _SAFE_SPAN_KEYS:
                raise ValueError("portable trace span shape is not supported")
            span = dict(span)
            trace_ids.add(_validate_hex(span["trace_id"], 32, "trace ID"))
            span_id = _validate_hex(span["span_id"], 16, "span ID")
            if span_id in span_ids:
                raise ValueError("portable trace span IDs must be unique")
            span_ids.add(span_id)
            parent = _validate_hex(span["parent_span_id"], 16, "parent span ID",
                                   optional=True)
            if parent:
                parents.append(parent)
            _identifier(span["name"], "span name")
            _identifier(span["span_kind"], "span kind")
            if not isinstance(span["status"], Mapping) or set(span["status"]) != {"code"}:
                raise ValueError("portable trace status shape is not supported")
            _identifier(span["status"]["code"], "span status")
            attributes += _validate_attributes(span["attributes"], require_redaction=True)
            raw_events = span["events"]
            if not isinstance(raw_events, list) or len(raw_events) > 200:
                raise ValueError("span events must be a bounded list")
            for event in raw_events:
                if not isinstance(event, Mapping) or set(event) != _SAFE_EVENT_KEYS:
                    raise ValueError("portable trace event shape is not supported")
                _identifier(event["name"], "event name")
                attributes += _validate_attributes(event["attributes"])
            events += len(raw_events)
            links = span["links"]
            if not isinstance(links, list) or links:
                raise ValueError("portable trace links must be an empty list")
            evaluations += sum(key in span["attributes"] for key in (
                "openinference.eval.reference", "vera.evaluation.report_ref"))
        if len(trace_ids) != 1:
            raise ValueError("every fixture span must share one trace ID")
        if any(parent not in span_ids for parent in parents):
            raise ValueError("parent span IDs must reference fixture spans")
        return cls(
            fixture_key=fixture_key,
            portable_trace_digest=_digest(trace),
            otlp_export_digest=_digest(to_otlp_json(trace)),
            span_count=len(spans), parent_link_count=len(parents),
            event_count=events, attribute_count=attributes,
            evaluation_link_count=evaluations,
        )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "fixture_key": self.fixture_key,
            "portable_trace_digest": self.portable_trace_digest,
            "otlp_export_digest": self.otlp_export_digest,
            "span_count": self.span_count,
            "parent_link_count": self.parent_link_count,
            "event_count": self.event_count,
            "attribute_count": self.attribute_count,
            "evaluation_link_count": self.evaluation_link_count,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"fixture_id": self.fixture_id, **self.identity_dict()}


@dataclass(frozen=True)
class ObservabilityBackendProfile:
    backend_id: str
    kind: str
    revision: str
    ingest_protocol: str = "otlp_http_json"
    semantic_convention: str = "openinference"

    def __post_init__(self) -> None:
        object.__setattr__(self, "backend_id", _identifier(self.backend_id, "backend ID"))
        if self.kind not in BACKEND_KINDS:
            raise ValueError("backend kind must be langfuse or phoenix")
        object.__setattr__(self, "revision", _identifier(self.revision, "backend revision"))
        if self.ingest_protocol != "otlp_http_json":
            raise ValueError("comparison requires the OTLP/HTTP JSON ingest path")
        if self.semantic_convention != "openinference":
            raise ValueError("comparison requires OpenInference semantics")

    def to_dict(self) -> dict[str, str]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class LatencyEvidence:
    samples: int = 0
    p50_ms: Optional[int] = None
    p95_ms: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "samples", _nonnegative(self.samples, "samples"))
        for name in ("p50_ms", "p95_ms"):
            object.__setattr__(self, name, _nonnegative(
                getattr(self, name), name, optional=True))
        if self.samples == 0 and (self.p50_ms is not None or self.p95_ms is not None):
            raise ValueError("latency percentiles require samples")
        if self.samples and (self.p50_ms is None or self.p95_ms is None):
            raise ValueError("latency samples require p50 and p95")
        if self.p50_ms is not None and self.p50_ms > self.p95_ms:
            raise ValueError("latency p50 cannot exceed p95")

    def to_dict(self) -> dict[str, Optional[int]]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class ObservabilityBackendEvidence:
    fixture_id: str
    portable_trace_digest: str
    otlp_export_digest: str
    profile: ObservabilityBackendProfile
    status: str
    observed_span_count: int = 0
    preserved_parent_links: int = 0
    preserved_events: int = 0
    preserved_attributes: int = 0
    preserved_evaluation_links: int = 0
    query_dimensions: tuple[str, ...] = ()
    ui_views: tuple[str, ...] = ()
    ingest_latency: LatencyEvidence = field(default_factory=LatencyEvidence)
    query_latency: LatencyEvidence = field(default_factory=LatencyEvidence)
    retention_days: Optional[int] = None
    access_model: str = "unmeasured"
    standards_export: Optional[bool] = None
    deletion_supported: Optional[bool] = None
    storage_bytes: Optional[int] = None
    peak_memory_bytes: Optional[int] = None
    cpu_milliseconds: Optional[int] = None
    backend_specific_field_count: Optional[int] = None
    failure_blocks_vera_runs: Optional[bool] = None
    recovery_ms: Optional[int] = None
    teardown_ms: Optional[int] = None
    residual_resource_count: Optional[int] = None
    error_code: str = ""
    evidence_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fixture_id", _identifier(self.fixture_id, "fixture ID"))
        for name in ("portable_trace_digest", "otlp_export_digest"):
            value = str(getattr(self, name) or "").lower()
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
                raise ValueError(f"{name} must be a sha256 digest")
            object.__setattr__(self, name, value)
        if not isinstance(self.profile, ObservabilityBackendProfile):
            raise TypeError("profile must be ObservabilityBackendProfile")
        if self.status not in {"completed", "failed", "cancelled"}:
            raise ValueError("unsupported backend evidence status")
        count_names = (
            "observed_span_count", "preserved_parent_links", "preserved_events",
            "preserved_attributes", "preserved_evaluation_links",
        )
        for name in count_names:
            object.__setattr__(self, name, _nonnegative(getattr(self, name), name))
        dimensions = tuple(sorted(_identifier(item, "query dimension")
                                  for item in self.query_dimensions))
        if not set(dimensions) <= QUERY_DIMENSIONS or len(set(dimensions)) != len(dimensions):
            raise ValueError("query dimensions must be unique supported dimensions")
        object.__setattr__(self, "query_dimensions", dimensions)
        views = tuple(sorted(_identifier(item, "UI view") for item in self.ui_views))
        if not set(views) <= UI_VIEWS or len(set(views)) != len(views):
            raise ValueError("UI views must be unique supported views")
        object.__setattr__(self, "ui_views", views)
        if not isinstance(self.ingest_latency, LatencyEvidence) or not isinstance(
                self.query_latency, LatencyEvidence):
            raise TypeError("latency fields must be LatencyEvidence")
        for name in ("retention_days", "storage_bytes", "peak_memory_bytes",
                     "cpu_milliseconds", "backend_specific_field_count",
                     "recovery_ms", "teardown_ms", "residual_resource_count"):
            object.__setattr__(self, name, _nonnegative(
                getattr(self, name), name, optional=True))
        if self.access_model not in {"unmeasured", "single_tenant", "rbac", "external"}:
            raise ValueError("unsupported access model")
        for name in ("standards_export", "deletion_supported", "failure_blocks_vera_runs"):
            if getattr(self, name) is not None and not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean or unmeasured")
        if self.error_code:
            object.__setattr__(self, "error_code", _identifier(self.error_code, "error code"))
        if self.status == "completed" and self.error_code:
            raise ValueError("completed evidence cannot contain an error")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed evidence requires an error code")
        if self.status != "failed" and self.error_code:
            raise ValueError("only failed evidence may contain an error code")
        if self.status != "completed" and any(getattr(self, name) for name in count_names):
            raise ValueError("unfinished evidence cannot claim preserved telemetry")
        if self.status != "completed" and (
                self.query_dimensions or self.ui_views or self.query_latency.samples):
            raise ValueError("unfinished evidence cannot claim query or UI observations")
        object.__setattr__(self, "evidence_id", _identity("obse_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "fixture_id": self.fixture_id,
            "portable_trace_digest": self.portable_trace_digest,
            "otlp_export_digest": self.otlp_export_digest,
            "profile": self.profile.to_dict(), "status": self.status,
            "fidelity_counts": {
                "spans": self.observed_span_count,
                "parent_links": self.preserved_parent_links,
                "events": self.preserved_events,
                "attributes": self.preserved_attributes,
                "evaluation_links": self.preserved_evaluation_links,
            },
            "query_dimensions": list(self.query_dimensions),
            "ui_views": list(self.ui_views),
            "ingest_latency": self.ingest_latency.to_dict(),
            "query_latency": self.query_latency.to_dict(),
            "governance": {
                "retention_days": self.retention_days,
                "access_model": self.access_model,
                "deletion_supported": self.deletion_supported,
            },
            "resources": {"storage_bytes": self.storage_bytes,
                          "peak_memory_bytes": self.peak_memory_bytes,
                          "cpu_milliseconds": self.cpu_milliseconds},
            "portability": {
                "ingest_protocol": self.profile.ingest_protocol,
                "semantic_convention": self.profile.semantic_convention,
                "standards_export": self.standards_export,
                "backend_specific_field_count": self.backend_specific_field_count,
            },
            "outage_isolation": {
                "failure_blocks_vera_runs": self.failure_blocks_vera_runs,
                "recovery_ms": self.recovery_ms,
            },
            "teardown": {"teardown_ms": self.teardown_ms,
                         "residual_resource_count": self.residual_resource_count},
            "error_code": self.error_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"evidence_id": self.evidence_id, **self.identity_dict()}


@dataclass(frozen=True)
class ObservabilityComparison:
    fixture: ObservabilityFixture
    evidence: tuple[ObservabilityBackendEvidence, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.fixture, ObservabilityFixture):
            raise TypeError("fixture must be ObservabilityFixture")
        evidence = tuple(self.evidence)
        if len(evidence) != 2 or not all(isinstance(
                item, ObservabilityBackendEvidence) for item in evidence):
            raise ValueError("comparison requires Langfuse and Phoenix evidence")
        if {item.profile.kind for item in evidence} != BACKEND_KINDS:
            raise ValueError("comparison requires exactly Langfuse and Phoenix")
        if len({item.profile.backend_id for item in evidence}) != 2:
            raise ValueError("backend IDs must be unique")
        for item in evidence:
            if (item.fixture_id != self.fixture.fixture_id or
                    item.portable_trace_digest != self.fixture.portable_trace_digest or
                    item.otlp_export_digest != self.fixture.otlp_export_digest):
                raise ValueError("every backend must report the identical fixture export")
            expected = (
                self.fixture.span_count, self.fixture.parent_link_count,
                self.fixture.event_count, self.fixture.attribute_count,
                self.fixture.evaluation_link_count,
            )
            observed = (
                item.observed_span_count, item.preserved_parent_links,
                item.preserved_events, item.preserved_attributes,
                item.preserved_evaluation_links,
            )
            if any(actual > maximum for actual, maximum in zip(observed, expected)):
                raise ValueError("preserved counts cannot exceed fixture expectations")
        object.__setattr__(self, "evidence", tuple(sorted(
            evidence, key=lambda item: item.profile.kind)))

    def report(self) -> dict[str, Any]:
        expected = {
            "spans": self.fixture.span_count,
            "parent_links": self.fixture.parent_link_count,
            "events": self.fixture.event_count,
            "attributes": self.fixture.attribute_count,
            "evaluation_links": self.fixture.evaluation_link_count,
        }
        backends = {}
        for item in self.evidence:
            counts = item.identity_dict()["fidelity_counts"]
            fidelity = {
                key: {"expected": value, "preserved": counts[key],
                      "missing": value - counts[key]}
                for key, value in expected.items()
            }
            identity = item.identity_dict()
            backends[item.profile.kind] = {
                "backend_id": item.profile.backend_id,
                "revision": item.profile.revision,
                "status": item.status,
                "evidence_id": item.evidence_id,
                "trace_fidelity": fidelity,
                "query_ui_value": {
                    "dimensions": list(item.query_dimensions),
                    "views": list(item.ui_views),
                    "query_latency": item.query_latency.to_dict(),
                },
                "evaluation_linkage": fidelity["evaluation_links"],
                "ingest_latency": item.ingest_latency.to_dict(),
                "governance": identity["governance"],
                "resources": identity["resources"],
                "portability": identity["portability"],
                "outage_isolation": identity["outage_isolation"],
                "teardown": identity["teardown"],
                "error_code": item.error_code,
            }
        return {
            "schema": COMPARISON_SCHEMA,
            "fixture": self.fixture.to_dict(),
            "backends": backends,
            "winner": None,
            "effect": "none",
            "backends_invoked": False,
            "activation_authority": False,
        }


def observability_fixture_from_dict(value: Mapping[str, Any]) -> ObservabilityFixture:
    """Strictly reconstruct a fixture without accepting trace or payload fields."""
    data = _mapping(value, "observability fixture", {
        "fixture_id", "schema", "fixture_key", "portable_trace_digest",
        "otlp_export_digest", "span_count", "parent_link_count", "event_count",
        "attribute_count", "evaluation_link_count",
    })
    if data.pop("schema") != FIXTURE_SCHEMA:
        raise ValueError("unsupported observability fixture schema")
    claimed = data.pop("fixture_id")
    fixture = ObservabilityFixture(**data)
    if claimed != fixture.fixture_id:
        raise ValueError("observability fixture identity mismatch")
    return fixture


def backend_evidence_from_dict(value: Mapping[str, Any]) -> ObservabilityBackendEvidence:
    """Strictly reconstruct payload-free backend evidence for offline replay."""
    data = _mapping(value, "observability backend evidence", {
        "evidence_id", "fixture_id", "portable_trace_digest", "otlp_export_digest",
        "profile", "status", "fidelity_counts", "query_dimensions", "ui_views",
        "ingest_latency", "query_latency", "governance", "resources",
        "portability", "outage_isolation", "teardown", "error_code",
    })
    profile_data = _mapping(data["profile"], "backend profile", {
        "backend_id", "kind", "revision", "ingest_protocol", "semantic_convention",
    })
    profile = ObservabilityBackendProfile(**profile_data)
    fidelity = _mapping(data["fidelity_counts"], "fidelity counts", {
        "spans", "parent_links", "events", "attributes", "evaluation_links",
    })
    ingest = LatencyEvidence(**_mapping(data["ingest_latency"], "ingest latency", {
        "samples", "p50_ms", "p95_ms",
    }))
    query = LatencyEvidence(**_mapping(data["query_latency"], "query latency", {
        "samples", "p50_ms", "p95_ms",
    }))
    governance = _mapping(data["governance"], "governance evidence", {
        "retention_days", "access_model", "deletion_supported",
    })
    resources = _mapping(data["resources"], "resource evidence", {
        "storage_bytes", "peak_memory_bytes", "cpu_milliseconds",
    })
    portability = _mapping(data["portability"], "portability evidence", {
        "ingest_protocol", "semantic_convention", "standards_export",
        "backend_specific_field_count",
    })
    if (portability["ingest_protocol"] != profile.ingest_protocol or
            portability["semantic_convention"] != profile.semantic_convention):
        raise ValueError("portability evidence must match the backend profile")
    outage = _mapping(data["outage_isolation"], "outage evidence", {
        "failure_blocks_vera_runs", "recovery_ms",
    })
    teardown = _mapping(data["teardown"], "teardown evidence", {
        "teardown_ms", "residual_resource_count",
    })
    evidence = ObservabilityBackendEvidence(
        fixture_id=data["fixture_id"],
        portable_trace_digest=data["portable_trace_digest"],
        otlp_export_digest=data["otlp_export_digest"],
        profile=profile, status=data["status"],
        observed_span_count=fidelity["spans"],
        preserved_parent_links=fidelity["parent_links"],
        preserved_events=fidelity["events"],
        preserved_attributes=fidelity["attributes"],
        preserved_evaluation_links=fidelity["evaluation_links"],
        query_dimensions=tuple(data["query_dimensions"]),
        ui_views=tuple(data["ui_views"]),
        ingest_latency=ingest, query_latency=query,
        retention_days=governance["retention_days"],
        access_model=governance["access_model"],
        standards_export=portability["standards_export"],
        deletion_supported=governance["deletion_supported"],
        storage_bytes=resources["storage_bytes"],
        peak_memory_bytes=resources["peak_memory_bytes"],
        cpu_milliseconds=resources["cpu_milliseconds"],
        backend_specific_field_count=portability["backend_specific_field_count"],
        failure_blocks_vera_runs=outage["failure_blocks_vera_runs"],
        recovery_ms=outage["recovery_ms"],
        teardown_ms=teardown["teardown_ms"],
        residual_resource_count=teardown["residual_resource_count"],
        error_code=data["error_code"],
    )
    if data["evidence_id"] != evidence.evidence_id:
        raise ValueError("observability backend evidence identity mismatch")
    return evidence
