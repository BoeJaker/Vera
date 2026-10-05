"""Offline evidence contract for Lance-format versus Parquet experiments."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from ..execution.run_protocol import ArtifactRef
from .dataset_provider import DatasetSnapshot, _identifier


PLAN_SCHEMA = "vera.lance-format-experiment/v1"
DECISION_SCHEMA = "vera.lance-format-decision/v1"
_SHA256 = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_SNAPSHOT_ID = re.compile(r"snap_[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?\Z")
_FORMATS = {"lance", "parquet"}
_MEDIA_TYPES = {
    "lance": "application/vnd.apache.lance",
    "parquet": "application/vnd.apache.parquet",
}
_MODALITIES = {"tabular", "text", "image", "audio", "video", "vector"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _sha256(value: Any, label: str) -> str:
    digest = str(value or "").strip().lower()
    if not _SHA256.fullmatch(digest):
        raise ValueError(f"{label} must be a canonical SHA-256 digest")
    return "sha256:" + digest.removeprefix("sha256:")


def _positive_int(value: Any, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if not 1 <= value <= maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}")
    return value


def _nonnegative_int(value: Any, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if not 0 <= value <= maximum:
        raise ValueError(f"{label} must be between 0 and {maximum}")
    return value


@dataclass(frozen=True)
class FormatArtifactBinding:
    format: str
    artifact: ArtifactRef = field(repr=False)
    format_version: str
    reader_version: str
    revision: str

    def __post_init__(self) -> None:
        value_format = _identifier(self.format, "format").lower()
        if value_format not in _FORMATS:
            raise ValueError("unsupported dataset format")
        object.__setattr__(self, "format", value_format)
        if not isinstance(self.artifact, ArtifactRef):
            raise TypeError("format artifact must be an ArtifactRef")
        if (self.artifact.media_type != _MEDIA_TYPES[value_format] or
                self.artifact.kind != f"dataset.{value_format}" or
                isinstance(self.artifact.size_bytes, bool) or
                not isinstance(self.artifact.size_bytes, int) or
                self.artifact.size_bytes <= 0):
            raise ValueError("format artifact type and size must be verified")
        _sha256(self.artifact.checksum, "format artifact checksum")
        for name in ("format_version", "reader_version"):
            version = str(getattr(self, name) or "")
            if not _VERSION.fullmatch(version):
                raise ValueError(f"{name} must be a pinned release version")
            object.__setattr__(self, name, version)
        object.__setattr__(self, "revision", _identifier(
            self.revision, "format revision"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "artifact_id": _identifier(self.artifact.id, "artifact ID"),
            "artifact_kind": _identifier(self.artifact.kind, "artifact kind"),
            "uri_digest": _digest(self.artifact.uri),
            "checksum": _sha256(self.artifact.checksum, "artifact checksum"),
            "media_type": self.artifact.media_type,
            "size_bytes": self.artifact.size_bytes,
            "format_version": self.format_version,
            "reader_version": self.reader_version,
            "revision": self.revision,
        }


@dataclass(frozen=True, init=False)
class LanceFormatExperimentPlan:
    snapshot: DatasetSnapshot
    bindings: tuple[FormatArtifactBinding, ...]
    modalities: tuple[str, ...]
    query_fixture_ids: tuple[str, ...]
    previous_snapshot_id: str
    previous_lance_revision: str
    maximum_read_ms: float
    maximum_peak_memory_bytes: int
    plan_id: str
    schema: str = PLAN_SCHEMA

    def __init__(
            self, *, snapshot: DatasetSnapshot,
            bindings: Sequence[FormatArtifactBinding],
            modalities: Sequence[str], query_fixture_ids: Sequence[str],
            maximum_read_ms: float, maximum_peak_memory_bytes: int,
            previous_snapshot_id: str = "",
            previous_lance_revision: str = "") -> None:
        if not isinstance(snapshot, DatasetSnapshot):
            raise TypeError("snapshot must be a DatasetSnapshot")
        values = tuple(bindings)
        if (len(values) != 2 or not all(
                isinstance(item, FormatArtifactBinding) for item in values) or
                {item.format for item in values} != _FORMATS):
            raise ValueError("plan requires exactly one Lance and one Parquet binding")
        if len({item.artifact.id for item in values}) != 2:
            raise ValueError("format bindings must use distinct artifacts")
        if (isinstance(modalities, (str, bytes)) or
                not isinstance(modalities, Sequence)):
            raise TypeError("modalities must be a sequence")
        normalized_modalities = tuple(sorted({
            _identifier(item, "modality").lower() for item in modalities}))
        if (not normalized_modalities or len(normalized_modalities) > 6 or
                set(normalized_modalities) - _MODALITIES):
            raise ValueError("modalities contain unsupported values")
        if (isinstance(query_fixture_ids, (str, bytes)) or
                not isinstance(query_fixture_ids, Sequence)):
            raise TypeError("query fixture IDs must be a sequence")
        fixtures = tuple(sorted({_identifier(item, "query fixture ID")
                                 for item in query_fixture_ids}))
        if not fixtures or len(fixtures) > 256:
            raise ValueError("query fixture IDs must be bounded and non-empty")
        if (isinstance(maximum_read_ms, bool) or
                not isinstance(maximum_read_ms, (int, float)) or
                not math.isfinite(float(maximum_read_ms)) or maximum_read_ms <= 0):
            raise ValueError("maximum read time must be positive and finite")
        memory = _positive_int(
            maximum_peak_memory_bytes, "maximum peak memory", 1 << 50)
        previous_snapshot_id = str(previous_snapshot_id or "").strip()
        previous_lance_revision = str(previous_lance_revision or "").strip()
        if bool(previous_snapshot_id) != bool(previous_lance_revision):
            raise ValueError("time-travel expectation requires both prior identities")
        if previous_snapshot_id:
            if not _SNAPSHOT_ID.fullmatch(previous_snapshot_id):
                raise ValueError("previous snapshot ID must be content-addressed")
            previous_lance_revision = _identifier(
                previous_lance_revision, "previous Lance revision")
            if previous_snapshot_id == snapshot.snapshot_id:
                raise ValueError("previous snapshot must differ from current snapshot")
        object.__setattr__(self, "snapshot", snapshot)
        object.__setattr__(self, "bindings", tuple(sorted(
            values, key=lambda item: item.format)))
        object.__setattr__(self, "modalities", normalized_modalities)
        object.__setattr__(self, "query_fixture_ids", fixtures)
        object.__setattr__(self, "previous_snapshot_id", previous_snapshot_id)
        object.__setattr__(self, "previous_lance_revision", previous_lance_revision)
        object.__setattr__(self, "maximum_read_ms", float(maximum_read_ms))
        object.__setattr__(self, "maximum_peak_memory_bytes", memory)
        object.__setattr__(self, "plan_id", "lfxp_" + _digest(
            self.identity_dict()).removeprefix("sha256:"))

    @property
    def schema_digest(self) -> str:
        return _digest(self.snapshot.schema)

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "dataset_id": self.snapshot.dataset_id,
            "snapshot_id": self.snapshot.snapshot_id,
            "record_count": self.snapshot.record_count,
            "schema_digest": self.schema_digest,
            "provenance_digest": _digest(self.snapshot.provenance),
            "bindings": [item.to_dict() for item in self.bindings],
            "modalities": list(self.modalities),
            "query_fixture_ids": list(self.query_fixture_ids),
            "previous_snapshot_id": self.previous_snapshot_id,
            "previous_lance_revision": self.previous_lance_revision,
            "maximum_read_ms": self.maximum_read_ms,
            "maximum_peak_memory_bytes": self.maximum_peak_memory_bytes,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id, **self.identity_dict(),
            "effect": "none", "runtime_invoked": False,
            "artifact_read": False, "cleanup_performed": False,
            "canonical_store_authority": False,
        }


@dataclass(frozen=True)
class FormatObservation:
    plan_id: str
    format: str
    snapshot_id: str
    artifact_checksum: str
    revision: str
    schema_digest: str
    record_count: int
    query_result_digests: tuple[tuple[str, str], ...]
    modality_digests: tuple[tuple[str, str], ...]
    evidence_digests: tuple[tuple[str, str], ...]
    read_ms: float
    peak_memory_bytes: int
    corruption_rejected: bool
    portable_reader_verified: bool
    removal_verified: bool
    cleanup_verified: bool
    transaction_verified: bool = False
    schema_evolution_verified: bool = False
    time_travel_snapshot_id: str = ""
    time_travel_revision: str = ""

    def __post_init__(self) -> None:
        for name in ("plan_id", "snapshot_id", "revision"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        value_format = _identifier(self.format, "format").lower()
        if value_format not in _FORMATS:
            raise ValueError("unsupported observation format")
        object.__setattr__(self, "format", value_format)
        for name in ("artifact_checksum", "schema_digest"):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        object.__setattr__(self, "record_count", _nonnegative_int(
            self.record_count, "record count", 1_000_000_000_000))
        if (isinstance(self.read_ms, bool) or
                not isinstance(self.read_ms, (int, float)) or
                not math.isfinite(float(self.read_ms)) or self.read_ms < 0):
            raise ValueError("read time must be finite and non-negative")
        object.__setattr__(self, "read_ms", float(self.read_ms))
        object.__setattr__(self, "peak_memory_bytes", _positive_int(
            self.peak_memory_bytes, "peak memory", 1 << 50))
        for name in ("query_result_digests", "modality_digests",
                     "evidence_digests"):
            raw = getattr(self, name)
            if len(raw) > 256:
                raise ValueError(f"{name} exceeds evidence limit")
            values = tuple(sorted(
                (_identifier(key, name), _sha256(value, name))
                for key, value in raw))
            if len({key for key, _ in values}) != len(values):
                raise ValueError(f"{name} keys must be unique")
            object.__setattr__(self, name, values)
        for name in (
                "corruption_rejected", "portable_reader_verified",
                "removal_verified", "cleanup_verified", "transaction_verified",
                "schema_evolution_verified"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")
        if self.time_travel_revision:
            object.__setattr__(self, "time_travel_revision", _identifier(
                self.time_travel_revision, "time-travel revision"))
        if self.time_travel_snapshot_id:
            if not _SNAPSHOT_ID.fullmatch(self.time_travel_snapshot_id):
                raise ValueError("time-travel snapshot ID must be content-addressed")


@dataclass(frozen=True)
class LanceFormatDecision:
    plan_id: str
    verdict: str
    reasons: tuple[str, ...]
    observation_digests: tuple[tuple[str, str], ...]
    decision_id: str = field(init=False)
    schema: str = DECISION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", "lfxd_" + _digest(
            self.identity_dict()).removeprefix("sha256:"))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "plan_id": self.plan_id,
            "verdict": self.verdict, "reasons": list(self.reasons),
            "observation_digests": dict(self.observation_digests),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id, **self.identity_dict(),
            "effect": "none", "canonical_store_changed": False,
            "query_routing_changed": False, "artifacts_deleted": False,
            "migration_authority": False,
        }


def evaluate_lance_format_experiment(
        plan: LanceFormatExperimentPlan,
        observations: Sequence[FormatObservation]) -> LanceFormatDecision:
    """Validate same-fixture evidence and return a non-authoritative decision."""
    if not isinstance(plan, LanceFormatExperimentPlan):
        raise TypeError("plan must be a LanceFormatExperimentPlan")
    values = tuple(observations)
    if (len(values) != 2 or not all(isinstance(item, FormatObservation)
                                    for item in values) or
            {item.format for item in values} != _FORMATS):
        raise ValueError("exactly one Lance and one Parquet observation is required")
    by_format = {item.format: item for item in values}
    bindings = {item.format: item for item in plan.bindings}
    expected_queries = set(plan.query_fixture_ids)
    expected_modalities = set(plan.modalities)
    for value_format, observation in by_format.items():
        binding = bindings[value_format]
        if (observation.plan_id != plan.plan_id or
                observation.snapshot_id != plan.snapshot.snapshot_id or
                observation.artifact_checksum != binding.to_dict()["checksum"] or
                observation.revision != binding.revision or
                observation.schema_digest != plan.schema_digest or
                observation.record_count != plan.snapshot.record_count):
            raise ValueError("format observation identity drifted from plan")
        if {key for key, _ in observation.query_result_digests} != expected_queries:
            raise ValueError("query evidence does not cover the planned fixtures")
        if {key for key, _ in observation.modality_digests} != expected_modalities:
            raise ValueError("modality evidence does not cover the planned modalities")
        required_evidence = {
            "corruption", "portable_reader", "removal", "cleanup"}
        if value_format == "lance":
            required_evidence.update({"transaction", "schema_evolution"})
            if plan.previous_snapshot_id:
                required_evidence.add("time_travel")
        if {key for key, _ in observation.evidence_digests} != required_evidence:
            raise ValueError("receipt evidence does not cover the planned checks")
    lance = by_format["lance"]
    parquet = by_format["parquet"]
    reasons = []
    if lance.query_result_digests != parquet.query_result_digests:
        reasons.append("query_result_mismatch")
    if lance.modality_digests != parquet.modality_digests:
        reasons.append("modality_fidelity_mismatch")
    for observation in values:
        if observation.read_ms > plan.maximum_read_ms:
            reasons.append(f"{observation.format}_read_budget_exceeded")
        if observation.peak_memory_bytes > plan.maximum_peak_memory_bytes:
            reasons.append(f"{observation.format}_memory_budget_exceeded")
        for name in ("corruption_rejected", "portable_reader_verified",
                     "removal_verified", "cleanup_verified"):
            if not getattr(observation, name):
                reasons.append(f"{observation.format}_{name}_missing")
    if not lance.transaction_verified:
        reasons.append("lance_transaction_evidence_missing")
    if not lance.schema_evolution_verified:
        reasons.append("lance_schema_evolution_evidence_missing")
    if plan.previous_snapshot_id:
        if (lance.time_travel_snapshot_id != plan.previous_snapshot_id or
                lance.time_travel_revision != plan.previous_lance_revision):
            reasons.append("lance_time_travel_evidence_missing")
    elif lance.time_travel_revision or lance.time_travel_snapshot_id:
        raise ValueError("observation claims unplanned time-travel evidence")
    reasons = tuple(sorted(set(reasons)))
    observation_digests = tuple(sorted(
        (item.format, _digest(item.__dict__)) for item in values))
    return LanceFormatDecision(
        plan.plan_id, "experimental_candidate" if not reasons else "rejected",
        reasons, observation_digests)
