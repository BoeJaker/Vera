"""Offline, provider-neutral retrieval comparison over one immutable snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import Any, Optional

from .dataset_provider import DatasetSnapshot


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SNAPSHOT_ID = re.compile(r"^snap_[0-9a-f]{64}$")
PROVIDER_KINDS = frozenset({
    "fabric_graph", "fabric_vector", "qdrant", "graphrag",
    "jepa_worldview_evidence", "analytical",
})


def _id(value: Any, label: str) -> str:
    value = str(value or "").strip()
    if not _ID.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def _integer(value: Any, label: str, *, optional: bool = False) -> Optional[int]:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _hash(prefix: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    return prefix + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, order=True)
class RetrievalCitation:
    record_id: str
    revision_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_id", _id(self.record_id, "record ID"))
        object.__setattr__(self, "revision_id", _id(self.revision_id, "revision ID"))

    def to_dict(self) -> dict[str, str]:
        return {"record_id": self.record_id, "revision_id": self.revision_id}


@dataclass(frozen=True)
class RetrievalCase:
    case_key: str
    query_digest: str
    relevant_citations: tuple[RetrievalCitation, ...]
    k: int = 10
    case_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_key", _id(self.case_key, "case key"))
        digest = str(self.query_digest or "").lower()
        if not _DIGEST.fullmatch(digest):
            raise ValueError("query digest must be sha256")
        object.__setattr__(self, "query_digest", digest)
        citations = tuple(sorted(self.relevant_citations))
        if not citations or len(citations) > 10_000 or not all(
                isinstance(item, RetrievalCitation) for item in citations):
            raise ValueError("relevant citations must contain 1..10000 citations")
        if len(set(citations)) != len(citations):
            raise ValueError("relevant citations must be unique")
        object.__setattr__(self, "relevant_citations", citations)
        if isinstance(self.k, bool) or not isinstance(self.k, int) or not 0 < self.k <= 1_000:
            raise ValueError("k must be an integer from 1 to 1000")
        object.__setattr__(self, "case_id", _hash("rcase_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {"case_key": self.case_key, "query_digest": self.query_digest,
                "relevant_citations": [item.to_dict() for item in self.relevant_citations],
                "k": self.k}

    def to_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id, **self.identity_dict()}


@dataclass(frozen=True)
class RetrievalProviderProfile:
    provider_id: str
    kind: str
    revision: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _id(self.provider_id, "provider ID"))
        if self.kind not in PROVIDER_KINDS:
            raise ValueError("unsupported retrieval provider kind")
        object.__setattr__(self, "revision", _id(self.revision, "provider revision"))

    def to_dict(self) -> dict[str, str]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class RetrievalObservation:
    case_id: str
    status: str
    citations: tuple[RetrievalCitation, ...] = ()
    latency_ms: Optional[int] = None
    error_code: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", _id(self.case_id, "case ID"))
        if self.status not in {"completed", "failed", "cancelled"}:
            raise ValueError("unsupported retrieval observation status")
        citations = tuple(self.citations)
        if len(citations) > 1_000 or not all(isinstance(item, RetrievalCitation)
                                             for item in citations):
            raise ValueError("returned citations must contain at most 1000 citations")
        if len(set(citations)) != len(citations):
            raise ValueError("returned citations must be unique")
        object.__setattr__(self, "citations", citations)
        object.__setattr__(self, "latency_ms", _integer(
            self.latency_ms, "latency", optional=True))
        if self.error_code:
            object.__setattr__(self, "error_code", _id(self.error_code, "error code"))
        if self.status == "completed" and (self.latency_ms is None or self.error_code):
            raise ValueError("completed observations require latency and no error")
        if self.status != "completed" and (self.citations or self.latency_ms is not None):
            raise ValueError("unfinished observations cannot claim results or latency")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed observations require an error code")
        if self.status != "failed" and self.error_code:
            raise ValueError("only failed observations may contain an error code")

    def to_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "status": self.status,
                "citations": [item.to_dict() for item in self.citations],
                "latency_ms": self.latency_ms, "error_code": self.error_code}


@dataclass(frozen=True)
class RetrievalLifecycleMetrics:
    index_ms: Optional[int] = None
    update_ms: Optional[int] = None
    storage_bytes: Optional[int] = None
    rebuild_ms: Optional[int] = None
    deletion_ms: Optional[int] = None

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, _integer(getattr(self, name), name, optional=True))

    def to_dict(self) -> dict[str, Optional[int]]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class RetrievalProviderEvidence:
    snapshot_id: str
    profile: RetrievalProviderProfile
    observations: tuple[RetrievalObservation, ...]
    lifecycle: RetrievalLifecycleMetrics = field(default_factory=RetrievalLifecycleMetrics)
    evidence_id: str = field(init=False)

    def __post_init__(self) -> None:
        snapshot_id = str(self.snapshot_id or "")
        if not _SNAPSHOT_ID.fullmatch(snapshot_id):
            raise ValueError("snapshot ID must identify a DatasetSnapshot")
        object.__setattr__(self, "snapshot_id", snapshot_id)
        if not isinstance(self.profile, RetrievalProviderProfile):
            raise TypeError("profile must be RetrievalProviderProfile")
        observations = tuple(self.observations)
        if not observations or not all(isinstance(item, RetrievalObservation)
                                       for item in observations):
            raise ValueError("observations cannot be empty")
        observations = tuple(sorted(observations, key=lambda item: item.case_id))
        if len({item.case_id for item in observations}) != len(observations):
            raise ValueError("observation case IDs must be unique")
        object.__setattr__(self, "observations", observations)
        if not isinstance(self.lifecycle, RetrievalLifecycleMetrics):
            raise TypeError("lifecycle must be RetrievalLifecycleMetrics")
        object.__setattr__(self, "evidence_id", _hash("revid_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {"schema": "vera.retrieval-provider-evidence/v1",
                "snapshot_id": self.snapshot_id, "profile": self.profile.to_dict(),
                "observations": [item.to_dict() for item in self.observations],
                "lifecycle": self.lifecycle.to_dict()}


@dataclass(frozen=True)
class RetrievalComparisonFixture:
    snapshot: DatasetSnapshot
    cases: tuple[RetrievalCase, ...]
    evidence: tuple[RetrievalProviderEvidence, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, DatasetSnapshot):
            raise TypeError("snapshot must be DatasetSnapshot")
        cases = tuple(self.cases)
        if not cases or not all(isinstance(item, RetrievalCase) for item in cases):
            raise ValueError("cases cannot be empty")
        cases = tuple(sorted(cases, key=lambda item: item.case_id))
        if len({item.case_id for item in cases}) != len(cases):
            raise ValueError("retrieval case IDs must be unique")
        object.__setattr__(self, "cases", cases)
        evidence = tuple(self.evidence)
        if len(evidence) < 2 or not all(isinstance(item, RetrievalProviderEvidence)
                                       for item in evidence):
            raise ValueError("comparison requires at least two provider evidence sets")
        evidence = tuple(sorted(evidence, key=lambda item: item.profile.provider_id))
        if len({item.profile.provider_id for item in evidence}) != len(evidence):
            raise ValueError("provider IDs must be unique")
        expected = {item.case_id for item in cases}
        for item in evidence:
            if item.snapshot_id != self.snapshot.snapshot_id:
                raise ValueError("all providers must use the identical DatasetSnapshot")
            if {row.case_id for row in item.observations} != expected:
                raise ValueError("every provider must report the identical case set")
        object.__setattr__(self, "evidence", evidence)


def _percentile(values: list[int], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def compare_retrieval(fixture: RetrievalComparisonFixture) -> dict[str, Any]:
    """Score supplied observations independently; never calls a provider."""
    if not isinstance(fixture, RetrievalComparisonFixture):
        raise TypeError("fixture must be RetrievalComparisonFixture")
    cases = {item.case_id: item for item in fixture.cases}
    providers = {}
    for evidence in fixture.evidence:
        recalls, precisions, reciprocals, revision_hits, revision_total, latencies = [], [], [], 0, 0, []
        failures = 0
        cancellations = 0
        for observation in evidence.observations:
            case = cases[observation.case_id]
            if observation.status == "failed":
                failures += 1
                continue
            if observation.status == "cancelled":
                cancellations += 1
                continue
            returned = observation.citations[:case.k]
            relevant = set(case.relevant_citations)
            hits = [item in relevant for item in returned]
            recalls.append(sum(hits) / len(relevant))
            precisions.append(sum(hits) / case.k)
            reciprocals.append(next((1 / (index + 1) for index, hit in enumerate(hits) if hit), 0.0))
            relevant_records = {item.record_id: item.revision_id for item in relevant}
            comparable = [item for item in returned if item.record_id in relevant_records]
            revision_total += len(comparable)
            revision_hits += sum(relevant_records[item.record_id] == item.revision_id
                                 for item in comparable)
            latencies.append(observation.latency_ms)
        completed = len(evidence.observations) - failures - cancellations
        providers[evidence.profile.provider_id] = {
            "kind": evidence.profile.kind, "revision": evidence.profile.revision,
            "evidence_id": evidence.evidence_id,
            "quality": {
                "evaluated_cases": completed,
                "recall_at_k": math.fsum(recalls) / completed if completed else None,
                "precision_at_k": math.fsum(precisions) / completed if completed else None,
                "mrr": math.fsum(reciprocals) / completed if completed else None,
                "citation_revision_accuracy": (
                    revision_hits / revision_total if revision_total else None),
            },
            "latency_ms": {"p50": _percentile(latencies, .5),
                           "p95": _percentile(latencies, .95)},
            "outcomes": {"completed": completed, "failed": failures,
                         "cancelled": cancellations,
                         "unsuccessful_rate": ((failures + cancellations) /
                                               len(evidence.observations))},
            "lifecycle": evidence.lifecycle.to_dict(),
        }
    return {"schema": "vera.retrieval-comparison/v1",
            "snapshot": fixture.snapshot.to_dict(),
            "case_ids": [item.case_id for item in fixture.cases],
            "providers": providers, "winner": None,
            "effect": "none", "providers_invoked": False}
