"""Portable contracts for bounded source discovery and collection.

The contracts describe work and its evidence. They do not crawl, schedule a
worker, contact a model, or choose a provider. Collected values reuse Vera's
existing ``ContextItem`` and ``DatasetSnapshot`` authorities instead of
introducing a discovery-only payload family.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from vera.context_provider import ContextItem
from vera.fabric.dataset_provider import DatasetSnapshot


DISCOVERY_REQUEST_SCHEMA = "vera.discovery-request/v1"
SOURCE_CANDIDATE_SCHEMA = "vera.discovery-source-candidate/v1"
COLLECTION_OPTION_SCHEMA = "vera.discovery-collection-option/v1"
COLLECTION_RECEIPT_SCHEMA = "vera.discovery-collection-receipt/v1"
DISCOVERY_RESULT_SCHEMA = "vera.discovery-result/v1"

MAX_QUERY_CHARS = 16_384
MAX_CANDIDATES = 256
MAX_OPTIONS = 32
MAX_OUTPUTS = 1_000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_HASH_IDS = {
    "request": re.compile(r"^dsr_[0-9a-f]{64}$"),
    "option": re.compile(r"^dco_[0-9a-f]{64}$"),
    "candidate": re.compile(r"^dsc_[0-9a-f]{64}$"),
    "receipt": re.compile(r"^dcr_[0-9a-f]{64}$"),
}
_SOURCE_KINDS = frozenset({
    "api", "capability", "database", "dataset", "feed", "file",
    "repository", "sitemap", "web",
})
_METHODS = frozenset({
    "api", "capability", "crawl", "dataset_query", "feed", "file_read",
    "repository_fetch", "sitemap",
})
_RESOURCES = frozenset({"cpu", "gpu", "network", "storage"})
_OUTPUT_KINDS = frozenset({"artifact", "context", "dataset"})
_STATUSES = frozenset({
    "cancelled", "failed", "partial", "rejected", "succeeded", "timed_out",
})
_SUCCESS_STATUSES = frozenset({"partial", "succeeded"})
_ALLOWED_SCHEMES = frozenset({
    "capability", "dataset", "fabric", "file", "git", "http", "https",
    "repo", "s3",
})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _hash(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _identifier(value: Any, field_name: str) -> str:
    value = str(value or "").strip()
    if not _ID.fullmatch(value):
        raise ValueError(f"{field_name} must be a bounded identifier")
    return value


def _timestamp(value: Any, field_name: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _integer(value: Any, field_name: str, *, minimum: int,
             maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) \
            or not minimum <= value <= maximum:
        raise ValueError(
            f"{field_name} must be an integer between {minimum} and {maximum}")
    return value


def _score(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{field_name} must be between zero and one")
    return float(value)


def _locator(value: Any) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 2_048 or any(char in value for char in "\r\n\0"):
        raise ValueError("locator must be present and bounded")
    parsed = urlsplit(value)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError("locator scheme is unsupported")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("locator must not contain credentials, query, or fragment")
    if parsed.scheme in {"http", "https"} and not parsed.hostname:
        raise ValueError("network locator requires a host")
    return value


def _ordered_identifiers(values: Sequence[str], field_name: str, *,
                         allowed: frozenset[str] | None = None,
                         maximum: int = 64) -> tuple[str, ...]:
    try:
        result = tuple(_identifier(item, field_name) for item in values)
    except TypeError as exc:
        raise ValueError(f"{field_name} must be a sequence") from exc
    if not result or len(result) > maximum or len(set(result)) != len(result):
        raise ValueError(f"{field_name} must contain unique bounded values")
    if allowed is not None and any(item not in allowed for item in result):
        raise ValueError(f"{field_name} contains an unsupported value")
    return tuple(sorted(result))


@dataclass(frozen=True, init=False)
class DiscoveryRequest:
    query: str
    requester: str
    tenant_id: str
    namespace: str
    as_of: str
    source_kinds: tuple[str, ...]
    max_sources: int
    max_context_items: int
    timeout_ms: int
    max_bytes: int
    max_cost_units: int
    request_id: str = field(init=False)
    schema: str = DISCOVERY_REQUEST_SCHEMA

    def __init__(self, *, query: str, requester: str, tenant_id: str,
                 namespace: str, as_of: str, source_kinds: Sequence[str],
                 max_sources: int = 16, max_context_items: int = 64,
                 timeout_ms: int = 5_000, max_bytes: int = 10_000_000,
                 max_cost_units: int = 1_000):
        query = str(query or "").strip()
        if not query or len(query) > MAX_QUERY_CHARS:
            raise ValueError("query must be present and bounded")
        object.__setattr__(self, "query", query)
        object.__setattr__(self, "requester", _identifier(requester, "requester"))
        object.__setattr__(self, "tenant_id", _identifier(tenant_id, "tenant_id"))
        object.__setattr__(self, "namespace", _identifier(namespace, "namespace"))
        object.__setattr__(self, "as_of", _timestamp(as_of, "as_of"))
        object.__setattr__(self, "source_kinds", _ordered_identifiers(
            source_kinds, "source_kinds", allowed=_SOURCE_KINDS))
        object.__setattr__(self, "max_sources", _integer(
            max_sources, "max_sources", minimum=1, maximum=MAX_CANDIDATES))
        object.__setattr__(self, "max_context_items", _integer(
            max_context_items, "max_context_items", minimum=1,
            maximum=MAX_OUTPUTS))
        object.__setattr__(self, "timeout_ms", _integer(
            timeout_ms, "timeout_ms", minimum=1, maximum=300_000))
        object.__setattr__(self, "max_bytes", _integer(
            max_bytes, "max_bytes", minimum=0, maximum=10_000_000_000))
        object.__setattr__(self, "max_cost_units", _integer(
            max_cost_units, "max_cost_units", minimum=0,
            maximum=1_000_000_000))
        object.__setattr__(self, "request_id", _hash("dsr_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "query": self.query,
            "requester": self.requester, "tenant_id": self.tenant_id,
            "namespace": self.namespace, "as_of": self.as_of,
            "source_kinds": list(self.source_kinds),
            "max_sources": self.max_sources,
            "max_context_items": self.max_context_items,
            "timeout_ms": self.timeout_ms, "max_bytes": self.max_bytes,
            "max_cost_units": self.max_cost_units,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"request_id": self.request_id, **self.identity_dict()}


@dataclass(frozen=True, init=False)
class CollectionOption:
    source_id: str
    method: str
    provider: str
    provider_revision: str
    resource: str
    output_kinds: tuple[str, ...]
    estimated_latency_ms: int
    estimated_cost_units: int
    max_bytes: int
    network_required: bool
    option_id: str = field(init=False)
    schema: str = COLLECTION_OPTION_SCHEMA

    def __init__(self, *, source_id: str, method: str, provider: str,
                 provider_revision: str, resource: str,
                 output_kinds: Sequence[str], estimated_latency_ms: int,
                 estimated_cost_units: int = 0, max_bytes: int = 0,
                 network_required: bool = False):
        object.__setattr__(self, "source_id", _identifier(source_id, "source_id"))
        method = _identifier(method, "method")
        if method not in _METHODS:
            raise ValueError("unsupported collection method")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "provider", _identifier(provider, "provider"))
        object.__setattr__(self, "provider_revision", _identifier(
            provider_revision, "provider_revision"))
        resource = _identifier(resource, "resource")
        if resource not in _RESOURCES:
            raise ValueError("unsupported collection resource")
        object.__setattr__(self, "resource", resource)
        object.__setattr__(self, "output_kinds", _ordered_identifiers(
            output_kinds, "output_kinds", allowed=_OUTPUT_KINDS))
        object.__setattr__(self, "estimated_latency_ms", _integer(
            estimated_latency_ms, "estimated_latency_ms", minimum=0,
            maximum=86_400_000))
        object.__setattr__(self, "estimated_cost_units", _integer(
            estimated_cost_units, "estimated_cost_units", minimum=0,
            maximum=1_000_000_000))
        object.__setattr__(self, "max_bytes", _integer(
            max_bytes, "max_bytes", minimum=0, maximum=10_000_000_000))
        if not isinstance(network_required, bool):
            raise ValueError("network_required must be boolean")
        object.__setattr__(self, "network_required", network_required)
        object.__setattr__(self, "option_id", _hash("dco_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "source_id": self.source_id,
            "method": self.method, "provider": self.provider,
            "provider_revision": self.provider_revision,
            "resource": self.resource, "output_kinds": list(self.output_kinds),
            "estimated_latency_ms": self.estimated_latency_ms,
            "estimated_cost_units": self.estimated_cost_units,
            "max_bytes": self.max_bytes, "network_required": self.network_required,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"option_id": self.option_id, **self.identity_dict()}


@dataclass(frozen=True, init=False)
class SourceCandidate:
    request_id: str
    source_id: str
    locator: str
    source_kind: str
    provider: str
    revision: str
    observed_at: str
    authority_score: float
    relevance_score: float
    freshness_score: float
    options: tuple[CollectionOption, ...]
    candidate_id: str = field(init=False)
    schema: str = SOURCE_CANDIDATE_SCHEMA

    def __init__(self, *, request_id: str, source_id: str, locator: str,
                 source_kind: str, provider: str, revision: str,
                 observed_at: str, authority_score: float,
                 relevance_score: float, freshness_score: float,
                 options: Sequence[CollectionOption]):
        if not _HASH_IDS["request"].fullmatch(str(request_id or "")):
            raise ValueError("invalid request_id")
        object.__setattr__(self, "request_id", request_id)
        source_id = _identifier(source_id, "source_id")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "locator", _locator(locator))
        source_kind = _identifier(source_kind, "source_kind")
        if source_kind not in _SOURCE_KINDS:
            raise ValueError("unsupported source_kind")
        object.__setattr__(self, "source_kind", source_kind)
        object.__setattr__(self, "provider", _identifier(provider, "provider"))
        object.__setattr__(self, "revision", _identifier(revision, "revision"))
        object.__setattr__(self, "observed_at", _timestamp(
            observed_at, "observed_at"))
        object.__setattr__(self, "authority_score", _score(
            authority_score, "authority_score"))
        object.__setattr__(self, "relevance_score", _score(
            relevance_score, "relevance_score"))
        object.__setattr__(self, "freshness_score", _score(
            freshness_score, "freshness_score"))
        try:
            frozen_options = tuple(options)
        except TypeError as exc:
            raise ValueError("options must be a sequence") from exc
        if (not frozen_options or len(frozen_options) > MAX_OPTIONS or
                not all(isinstance(item, CollectionOption)
                        for item in frozen_options)):
            raise ValueError("candidate requires bounded collection options")
        if any(item.source_id != source_id for item in frozen_options):
            raise ValueError("collection option belongs to another source")
        if len({item.option_id for item in frozen_options}) != len(frozen_options):
            raise ValueError("collection options must be unique")
        frozen_options = tuple(sorted(frozen_options, key=lambda item: item.option_id))
        object.__setattr__(self, "options", frozen_options)
        object.__setattr__(self, "candidate_id", _hash(
            "dsc_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "request_id": self.request_id,
            "source_id": self.source_id, "locator": self.locator,
            "source_kind": self.source_kind, "provider": self.provider,
            "revision": self.revision, "observed_at": self.observed_at,
            "authority_score": self.authority_score,
            "relevance_score": self.relevance_score,
            "freshness_score": self.freshness_score,
            "options": [item.to_dict() for item in self.options],
        }

    def to_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, **self.identity_dict()}


@dataclass(frozen=True, init=False)
class CollectionReceipt:
    request_id: str
    candidate_id: str
    option_id: str
    provider_revision: str
    status: str
    started_at: str
    completed_at: str
    item_count: int
    byte_count: int
    duration_ms: int
    cost_units: int
    error_code: str
    receipt_id: str = field(init=False)
    schema: str = COLLECTION_RECEIPT_SCHEMA

    def __init__(self, *, request_id: str, candidate_id: str, option_id: str,
                 provider_revision: str, status: str, started_at: str,
                 completed_at: str, item_count: int = 0, byte_count: int = 0,
                 duration_ms: int = 0, cost_units: int = 0,
                 error_code: str = ""):
        for kind, value in (("request", request_id), ("candidate", candidate_id),
                            ("option", option_id)):
            if not _HASH_IDS[kind].fullmatch(str(value or "")):
                raise ValueError(f"invalid {kind}_id")
            object.__setattr__(self, f"{kind}_id", value)
        object.__setattr__(self, "provider_revision", _identifier(
            provider_revision, "provider_revision"))
        status = _identifier(status, "status")
        if status not in _STATUSES:
            raise ValueError("unsupported receipt status")
        object.__setattr__(self, "status", status)
        started_at = _timestamp(started_at, "started_at")
        completed_at = _timestamp(completed_at, "completed_at")
        if _instant(completed_at) < _instant(started_at):
            raise ValueError("completed_at cannot precede started_at")
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(self, "item_count", _integer(
            item_count, "item_count", minimum=0, maximum=MAX_OUTPUTS))
        object.__setattr__(self, "byte_count", _integer(
            byte_count, "byte_count", minimum=0, maximum=10_000_000_000))
        object.__setattr__(self, "duration_ms", _integer(
            duration_ms, "duration_ms", minimum=0, maximum=86_400_000))
        object.__setattr__(self, "cost_units", _integer(
            cost_units, "cost_units", minimum=0, maximum=1_000_000_000))
        error_code = str(error_code or "").strip()
        if error_code:
            error_code = _identifier(error_code, "error_code")
        if status in _SUCCESS_STATUSES and error_code:
            raise ValueError("successful receipt cannot carry an error_code")
        if status not in _SUCCESS_STATUSES and not error_code:
            raise ValueError("unsuccessful receipt requires an error_code")
        if status not in _SUCCESS_STATUSES and (item_count or byte_count):
            raise ValueError("unsuccessful receipt cannot claim collected output")
        object.__setattr__(self, "error_code", error_code)
        object.__setattr__(self, "receipt_id", _hash("dcr_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "request_id": self.request_id,
            "candidate_id": self.candidate_id, "option_id": self.option_id,
            "provider_revision": self.provider_revision, "status": self.status,
            "started_at": self.started_at, "completed_at": self.completed_at,
            "item_count": self.item_count, "byte_count": self.byte_count,
            "duration_ms": self.duration_ms, "cost_units": self.cost_units,
            "error_code": self.error_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"receipt_id": self.receipt_id, **self.identity_dict()}


@dataclass(frozen=True, slots=True)
class DiscoveredContext:
    receipt_id: str
    item: ContextItem

    def __post_init__(self) -> None:
        if not _HASH_IDS["receipt"].fullmatch(str(self.receipt_id or "")):
            raise ValueError("invalid receipt_id")
        if not isinstance(self.item, ContextItem):
            raise ValueError("item must be portable ContextItem")
        if self.receipt_id not in {value.source_id for value in self.item.citations}:
            raise ValueError("context must cite its collection receipt")


@dataclass(frozen=True, slots=True)
class DiscoveredDataset:
    receipt_id: str
    snapshot: DatasetSnapshot

    def __post_init__(self) -> None:
        if not _HASH_IDS["receipt"].fullmatch(str(self.receipt_id or "")):
            raise ValueError("invalid receipt_id")
        if not isinstance(self.snapshot, DatasetSnapshot):
            raise ValueError("snapshot must be portable DatasetSnapshot")
        if self.snapshot.provenance.get("collection_receipt_id") != self.receipt_id:
            raise ValueError("dataset provenance must name its collection receipt")


@dataclass(frozen=True, slots=True)
class DiscoveredArtifact:
    receipt_id: str
    artifact_id: str
    locator: str
    revision: str
    sha256: str

    def __post_init__(self) -> None:
        if not _HASH_IDS["receipt"].fullmatch(str(self.receipt_id or "")):
            raise ValueError("invalid receipt_id")
        object.__setattr__(self, "artifact_id", _identifier(
            self.artifact_id, "artifact_id"))
        object.__setattr__(self, "locator", _locator(self.locator))
        object.__setattr__(self, "revision", _identifier(self.revision, "revision"))
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(self.sha256 or "")):
            raise ValueError("artifact sha256 must be content-addressed")


@dataclass(frozen=True, init=False)
class DiscoveryResult:
    request: DiscoveryRequest
    candidates: tuple[SourceCandidate, ...]
    receipts: tuple[CollectionReceipt, ...]
    context: tuple[DiscoveredContext, ...]
    datasets: tuple[DiscoveredDataset, ...]
    artifacts: tuple[DiscoveredArtifact, ...]
    result_id: str = field(init=False)
    schema: str = DISCOVERY_RESULT_SCHEMA

    def __init__(self, *, request: DiscoveryRequest,
                 candidates: Sequence[SourceCandidate],
                 receipts: Sequence[CollectionReceipt],
                 context: Sequence[DiscoveredContext] = (),
                 datasets: Sequence[DiscoveredDataset] = (),
                 artifacts: Sequence[DiscoveredArtifact] = ()):
        if not isinstance(request, DiscoveryRequest):
            raise ValueError("request must be DiscoveryRequest")
        object.__setattr__(self, "request", request)
        frozen_candidates = self._freeze(candidates, SourceCandidate,
                                         "candidates", MAX_CANDIDATES,
                                         lambda value: value.candidate_id)
        if any(value.request_id != request.request_id
               for value in frozen_candidates):
            raise ValueError("candidate belongs to another request")
        candidate_index = {value.candidate_id: value for value in frozen_candidates}
        option_index = {
            option.option_id: (candidate.candidate_id, option)
            for candidate in frozen_candidates for option in candidate.options
        }
        frozen_receipts = self._freeze(receipts, CollectionReceipt, "receipts",
                                       MAX_OUTPUTS, lambda value: value.receipt_id)
        for receipt in frozen_receipts:
            if receipt.request_id != request.request_id:
                raise ValueError("receipt belongs to another request")
            option = option_index.get(receipt.option_id)
            if (receipt.candidate_id not in candidate_index or option is None or
                    option[0] != receipt.candidate_id or
                    option[1].provider_revision != receipt.provider_revision):
                raise ValueError("receipt does not match candidate option identity")
        receipt_index = {value.receipt_id: value for value in frozen_receipts}
        frozen_context = self._freeze(context, DiscoveredContext, "context",
                                      MAX_OUTPUTS, lambda value: (
                                          value.receipt_id, value.item.provider,
                                          value.item.item_id, value.item.revision))
        frozen_datasets = self._freeze(datasets, DiscoveredDataset, "datasets",
                                       MAX_OUTPUTS, lambda value: (
                                           value.receipt_id,
                                           value.snapshot.snapshot_id))
        frozen_artifacts = self._freeze(artifacts, DiscoveredArtifact, "artifacts",
                                        MAX_OUTPUTS, lambda value: (
                                            value.receipt_id, value.artifact_id,
                                            value.revision))
        output_counts: dict[str, int] = {}
        typed_outputs = (
            *(("context", value) for value in frozen_context),
            *(("dataset", value) for value in frozen_datasets),
            *(("artifact", value) for value in frozen_artifacts),
        )
        for output_kind, output in typed_outputs:
            receipt = receipt_index.get(output.receipt_id)
            if receipt is None or receipt.status not in _SUCCESS_STATUSES:
                raise ValueError("output requires a successful collection receipt")
            option = option_index[receipt.option_id][1]
            if output_kind not in option.output_kinds:
                raise ValueError("output kind was not declared by collection option")
            output_counts[output.receipt_id] = output_counts.get(output.receipt_id, 0) + 1
        for receipt in frozen_receipts:
            if output_counts.get(receipt.receipt_id, 0) != receipt.item_count:
                raise ValueError("receipt item_count does not match portable outputs")
        if len(frozen_candidates) > request.max_sources:
            raise ValueError("candidate count exceeds request budget")
        if len(frozen_context) > request.max_context_items:
            raise ValueError("context count exceeds request budget")
        if sum(value.byte_count for value in frozen_receipts) > request.max_bytes:
            raise ValueError("collected bytes exceed request budget")
        if sum(value.cost_units for value in frozen_receipts) > request.max_cost_units:
            raise ValueError("collection cost exceeds request budget")
        if any(value.duration_ms > request.timeout_ms for value in frozen_receipts):
            raise ValueError("collection duration exceeds request timeout")
        object.__setattr__(self, "candidates", frozen_candidates)
        object.__setattr__(self, "receipts", frozen_receipts)
        object.__setattr__(self, "context", frozen_context)
        object.__setattr__(self, "datasets", frozen_datasets)
        object.__setattr__(self, "artifacts", frozen_artifacts)
        object.__setattr__(self, "result_id", _hash("dsx_", self.identity_dict()))

    @staticmethod
    def _freeze(values: Sequence[Any], expected: type, field_name: str,
                maximum: int, key: Any) -> tuple[Any, ...]:
        try:
            frozen = tuple(values)
        except TypeError as exc:
            raise ValueError(f"{field_name} must be a sequence") from exc
        if len(frozen) > maximum or not all(isinstance(value, expected)
                                             for value in frozen):
            raise ValueError(f"{field_name} contains invalid values")
        keys = [key(value) for value in frozen]
        if len(keys) != len(set(keys)):
            raise ValueError(f"{field_name} contains duplicate identities")
        return tuple(sorted(frozen, key=key))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "request_id": self.request.request_id,
            "candidate_ids": [value.candidate_id for value in self.candidates],
            "receipt_ids": [value.receipt_id for value in self.receipts],
            "context": [{
                "receipt_id": value.receipt_id,
                "provider": value.item.provider, "item_id": value.item.item_id,
                "revision": value.item.revision,
                "text_sha256": hashlib.sha256(value.item.text.encode()).hexdigest(),
            } for value in self.context],
            "datasets": [{"receipt_id": value.receipt_id,
                           "snapshot_id": value.snapshot.snapshot_id}
                          for value in self.datasets],
            "artifacts": [{"receipt_id": value.receipt_id,
                            "artifact_id": value.artifact_id,
                            "revision": value.revision, "sha256": value.sha256}
                           for value in self.artifacts],
        }

    def to_dict(self) -> dict[str, Any]:
        def context_dict(value: DiscoveredContext) -> dict[str, Any]:
            item = value.item
            return {
                "receipt_id": value.receipt_id,
                "item": {
                    "item_id": item.item_id, "text": item.text,
                    "source": item.source, "revision": item.revision,
                    "provider": item.provider, "score": item.score,
                    "token_count": item.token_count,
                    "citations": [
                        {"source_id": citation.source_id,
                         "locator": citation.locator}
                        for citation in item.citations],
                    "ranking_evidence": [
                        {"provider": evidence.provider,
                         "revision": evidence.revision,
                         "score": evidence.score, "weight": evidence.weight,
                         "locator": evidence.locator}
                        for evidence in item.ranking_evidence],
                },
            }

        return {
            "schema": self.schema, "result_id": self.result_id,
            "request": self.request.to_dict(),
            "candidates": [value.to_dict() for value in self.candidates],
            "receipts": [value.to_dict() for value in self.receipts],
            "context": [context_dict(value) for value in self.context],
            "datasets": [{"receipt_id": value.receipt_id,
                           "snapshot": value.snapshot.to_dict()}
                          for value in self.datasets],
            "artifacts": [{
                "receipt_id": value.receipt_id,
                "artifact_id": value.artifact_id, "locator": value.locator,
                "revision": value.revision, "sha256": value.sha256,
            } for value in self.artifacts],
        }
