"""Deterministic, payload-free evidence for deprecation candidate usage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping


SCHEMA = "vera.deprecation-inventory/v1"
SOURCE_KINDS = (
    "code_reference", "http_caller", "mcp_caller", "stored_workflow",
    "schedule", "ui_link", "configuration", "artifact",
    "external_consumer",
)
CANDIDATE_KINDS = frozenset({
    "compatibility_alias", "ontology_projection", "scheduler",
    "transition_engine", "agent_loop", "generation_capability",
    "onnx_capability", "bridge_surface", "context_probe",
    "memory_authority", "superseded_path",
})
OBSERVATION_CLASSES = frozenset({"consumer", "health_check", "migration_probe"})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
ALIAS_USAGE_COUNT_KEY = "vera:deprecation:alias-usage:v1:counts"
ALIAS_USAGE_META_KEY = "vera:deprecation:alias-usage:v1:metadata"


def _identifier(value: Any, label: str) -> str:
    value = str(value or "").strip()
    if not _ID.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def _digest(value: Any, label: str) -> str:
    value = str(value or "").lower()
    if not _DIGEST.fullmatch(value):
        raise ValueError(f"{label} must be a sha256 digest")
    return value


def _count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _identity(prefix: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    return prefix + hashlib.sha256(encoded).hexdigest()


def _timestamp(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def alias_usage_field(candidate_name: str, source_kind: str) -> str:
    """Bounded Redis hash field; it contains identifiers and no caller payload."""
    candidate_name = _identifier(candidate_name, "candidate name")
    if source_kind not in {"http_caller", "mcp_caller"}:
        raise ValueError("runtime alias usage must be HTTP or MCP")
    return f"{candidate_name}|{source_kind}"


async def record_alias_usage(redis: Any, *, candidate_name: str,
                             replacement: str, source_kind: str,
                             observed_at: str) -> bool:
    """Persist one conservative consumer observation without affecting dispatch."""
    if redis is None:
        return False
    candidate_name = _identifier(candidate_name, "candidate name")
    replacement = _identifier(replacement, "replacement")
    if replacement == candidate_name:
        raise ValueError("replacement must differ from the candidate")
    field = alias_usage_field(candidate_name, source_kind)
    metadata = json.dumps({
        "candidate": candidate_name, "replacement": replacement,
        "source_kind": source_kind, "classification": "consumer",
        "last_observed_at": _timestamp(observed_at),
    }, sort_keys=True, separators=(",", ":"))
    await redis.hincrby(ALIAS_USAGE_COUNT_KEY, field, 1)
    await redis.hset(ALIAS_USAGE_META_KEY, field, metadata)
    return True


def observations_from_alias_usage_counts(
    counts: Mapping[str, Any], candidates: tuple["DeprecationCandidate", ...], *,
    window_id: str,
) -> tuple["UsageObservation", ...]:
    """Convert bounded Redis counters into conservative consumer evidence."""
    if not isinstance(counts, Mapping):
        raise TypeError("alias usage counts must be a mapping")
    by_name = {item.name: item for item in candidates}
    if len(by_name) != len(candidates):
        raise ValueError("candidate names must be unique")
    results = []
    for raw_field, raw_count in counts.items():
        field = str(raw_field.decode() if isinstance(raw_field, bytes) else raw_field)
        name, separator, source_kind = field.partition("|")
        if not separator or source_kind not in {"http_caller", "mcp_caller"}:
            raise ValueError("malformed alias usage counter field")
        candidate = by_name.get(name)
        if candidate is None:
            raise ValueError("alias usage references an undeclared candidate")
        count_value = (raw_count.decode() if isinstance(raw_count, bytes)
                       else raw_count)
        if (isinstance(count_value, bool) or
                not isinstance(count_value, (int, str)) or
                not str(count_value).isdigit()):
            raise ValueError("alias usage counter must be a positive integer")
        count = int(count_value)
        if count <= 0:
            raise ValueError("alias usage counter must be a positive integer")
        source_digest = "sha256:" + hashlib.sha256(
            f"runtime:{source_kind}".encode()).hexdigest()
        results.append(UsageObservation(
            candidate_id=candidate.candidate_id, source_kind=source_kind,
            classification="consumer", source_ref_digest=source_digest,
            count=count, window_id=window_id,
        ))
    return tuple(sorted(results, key=lambda item: item.observation_id))


def candidates_from_registry(registry: Mapping[str, Mapping[str, Any]], *,
                             owner: str = "vera.capabilities") -> tuple[
                                 "DeprecationCandidate", ...]:
    """Project only explicitly declared aliases; never infer from their names."""
    if not isinstance(registry, Mapping):
        raise TypeError("registry must be a mapping")
    candidates = []
    for name, entry in registry.items():
        if not isinstance(entry, Mapping):
            continue
        replacement = str(entry.get("compatibility_alias_for") or "").strip()
        if not replacement:
            continue
        candidates.append(DeprecationCandidate(
            name=str(name), kind="compatibility_alias", replacement=replacement,
            owner=owner, reason_code="compatibility_surface",
        ))
    return tuple(sorted(candidates, key=lambda item: item.candidate_id))


def scan_reference_documents(
    candidates: tuple["DeprecationCandidate", ...], *, source_kind: str,
    documents: Mapping[str, str], window_id: str,
    classifications: Mapping[str, str] | None = None,
) -> tuple["UsageObservation", ...]:
    """Scan supplied text snapshots and retain only reference digests and counts."""
    if source_kind not in SOURCE_KINDS:
        raise ValueError("unsupported usage source kind")
    if not isinstance(documents, Mapping):
        raise TypeError("documents must be a mapping")
    classifications = dict(classifications or {})
    unknown = set(classifications) - set(documents)
    if unknown:
        raise ValueError("classifications must reference supplied documents")
    results = []
    token_chars = r"A-Za-z0-9._:/-"
    for ref, text in documents.items():
        if not isinstance(ref, str) or not isinstance(text, str):
            raise ValueError("document references and content must be strings")
        classification = classifications.get(ref, "consumer")
        if classification not in OBSERVATION_CLASSES:
            raise ValueError("unsupported observation classification")
        ref_digest = "sha256:" + hashlib.sha256(ref.encode()).hexdigest()
        for candidate in candidates:
            pattern = re.compile(
                rf"(?<![{token_chars}]){re.escape(candidate.name)}(?![{token_chars}])")
            count = len(pattern.findall(text))
            if count:
                results.append(UsageObservation(
                    candidate_id=candidate.candidate_id,
                    source_kind=source_kind, classification=classification,
                    source_ref_digest=ref_digest, count=count,
                    window_id=window_id,
                ))
    return tuple(sorted(results, key=lambda item: item.observation_id))


@dataclass(frozen=True)
class DeprecationCandidate:
    name: str
    kind: str
    replacement: str
    owner: str
    reason_code: str
    candidate_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name, label in (("name", "candidate name"),
                            ("replacement", "replacement"),
                            ("owner", "owner"),
                            ("reason_code", "reason code")):
            object.__setattr__(self, name, _identifier(getattr(self, name), label))
        if self.kind not in CANDIDATE_KINDS:
            raise ValueError("unsupported deprecation candidate kind")
        if self.name == self.replacement:
            raise ValueError("replacement must differ from the candidate")
        object.__setattr__(self, "candidate_id", _identity(
            "depc_", self.identity_dict()))

    def identity_dict(self) -> dict[str, str]:
        return {
            "name": self.name, "kind": self.kind,
            "replacement": self.replacement, "owner": self.owner,
            "reason_code": self.reason_code,
        }

    def to_dict(self) -> dict[str, str]:
        return {"candidate_id": self.candidate_id, **self.identity_dict()}


@dataclass(frozen=True)
class UsageObservation:
    candidate_id: str
    source_kind: str
    classification: str
    source_ref_digest: str
    count: int
    window_id: str
    observation_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _identifier(
            self.candidate_id, "candidate ID"))
        if self.source_kind not in SOURCE_KINDS:
            raise ValueError("unsupported usage source kind")
        if self.classification not in OBSERVATION_CLASSES:
            raise ValueError("unsupported observation classification")
        object.__setattr__(self, "source_ref_digest", _digest(
            self.source_ref_digest, "source reference digest"))
        object.__setattr__(self, "count", _count(self.count, "observation count"))
        if not self.count:
            raise ValueError("usage observations must have a positive count")
        object.__setattr__(self, "window_id", _identifier(
            self.window_id, "observation window ID"))
        object.__setattr__(self, "observation_id", _identity(
            "depo_", self.identity_dict()))

    @property
    def excluded(self) -> bool:
        return self.classification != "consumer"

    def identity_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "source_kind": self.source_kind,
            "classification": self.classification,
            "source_ref_digest": self.source_ref_digest,
            "count": self.count, "window_id": self.window_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"observation_id": self.observation_id,
                "excluded": self.excluded, **self.identity_dict()}


@dataclass(frozen=True)
class SourceCoverage:
    source_kind: str
    status: str
    snapshot_digest: str = ""
    reason_code: str = ""

    def __post_init__(self) -> None:
        if self.source_kind not in SOURCE_KINDS:
            raise ValueError("unsupported coverage source kind")
        if self.status not in {"complete", "partial", "unavailable"}:
            raise ValueError("unsupported coverage status")
        if self.snapshot_digest:
            object.__setattr__(self, "snapshot_digest", _digest(
                self.snapshot_digest, "coverage snapshot digest"))
        if self.reason_code:
            object.__setattr__(self, "reason_code", _identifier(
                self.reason_code, "coverage reason code"))
        if self.status == "complete" and (not self.snapshot_digest or self.reason_code):
            raise ValueError("complete coverage requires a snapshot and no reason")
        if self.status != "complete" and not self.reason_code:
            raise ValueError("incomplete coverage requires a reason code")

    def to_dict(self) -> dict[str, str]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class DeprecationInventory:
    candidates: tuple[DeprecationCandidate, ...]
    observations: tuple[UsageObservation, ...]
    coverage: tuple[SourceCoverage, ...]
    inventory_id: str = field(init=False)
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        candidates = tuple(self.candidates)
        observations = tuple(self.observations)
        coverage = tuple(self.coverage)
        if not candidates or not all(isinstance(item, DeprecationCandidate)
                                     for item in candidates):
            raise ValueError("inventory requires deprecation candidates")
        if not all(isinstance(item, UsageObservation) for item in observations):
            raise ValueError("observations must contain UsageObservation values")
        if not all(isinstance(item, SourceCoverage) for item in coverage):
            raise ValueError("coverage must contain SourceCoverage values")
        candidates = tuple(sorted(candidates, key=lambda item: item.candidate_id))
        observations = tuple(sorted(observations, key=lambda item: item.observation_id))
        coverage = tuple(sorted(coverage, key=lambda item: item.source_kind))
        if len({item.candidate_id for item in candidates}) != len(candidates):
            raise ValueError("candidate identities must be unique")
        if len({item.observation_id for item in observations}) != len(observations):
            raise ValueError("observation identities must be unique")
        candidate_ids = {item.candidate_id for item in candidates}
        if any(item.candidate_id not in candidate_ids for item in observations):
            raise ValueError("observations must reference inventory candidates")
        if tuple(item.source_kind for item in coverage) != tuple(sorted(SOURCE_KINDS)):
            raise ValueError("coverage must report every required source kind exactly once")
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "coverage", coverage)
        object.__setattr__(self, "inventory_id", _identity(
            "depi_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "candidates": [item.to_dict() for item in self.candidates],
            "observations": [item.to_dict() for item in self.observations],
            "coverage": [item.to_dict() for item in self.coverage],
        }

    def to_dict(self) -> dict[str, Any]:
        return {"inventory_id": self.inventory_id, **self.identity_dict()}

    def report(self) -> dict[str, Any]:
        coverage = {item.source_kind: item.to_dict() for item in self.coverage}
        complete = all(item.status == "complete" for item in self.coverage)
        by_candidate = {}
        for candidate in self.candidates:
            relevant = [item for item in self.observations
                        if item.candidate_id == candidate.candidate_id]
            consumers = [item for item in relevant if not item.excluded]
            health = sum(item.count for item in relevant
                         if item.classification == "health_check")
            migration = sum(item.count for item in relevant
                            if item.classification == "migration_probe")
            by_source = {
                source: sum(item.count for item in consumers
                            if item.source_kind == source)
                for source in SOURCE_KINDS
            }
            total = sum(by_source.values())
            by_candidate[candidate.candidate_id] = {
                "name": candidate.name, "kind": candidate.kind,
                "replacement": candidate.replacement, "owner": candidate.owner,
                "consumer_evidence": {"total": total, "by_source": by_source},
                "excluded_probes": {
                    "health_check": health, "migration_probe": migration,
                },
                "zero_consumer_evidence": total == 0,
                "independent_review_candidate": complete and total == 0,
                "removal_authority": False,
            }
        return {
            "schema": SCHEMA, "inventory_id": self.inventory_id,
            "coverage": coverage, "coverage_complete": complete,
            "candidates": by_candidate,
            "effects": [], "mutates": False, "removal_authority": False,
        }
