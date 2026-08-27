"""Portable graph/vector projection contracts and deterministic reference store."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from vera.fabric.dataset_provider import CancellationSignal
from vera.fabric.record_revision import RecordRevision


PROJECTION_SPEC_SCHEMA = "vera.fabric-projection-spec/v1"
PROJECTION_ENTRY_SCHEMA = "vera.fabric-projection-entry/v1"
PROJECTION_REPORT_SCHEMA = "vera.fabric-projection-reconciliation/v1"
MAX_PROJECTION_RECORDS = 10_000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_RECORD_ID = re.compile(r"^rec_[A-Za-z0-9._:-]{1,251}$")
_REVISION_ID = re.compile(r"^rev_[0-9a-f]{64}$")
_PROJECTION_ID = re.compile(r"^proj_[0-9a-f]{64}$")


def _canonical(value: Any, field_name: str, limit: int = 1_048_576) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{field_name} must be finite JSON") from exc
    if json.loads(encoded) != value:
        raise ValueError(f"{field_name} must use JSON string object keys")
    if len(encoded.encode()) > limit:
        raise ValueError(f"{field_name} exceeds size limit")
    return encoded


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value, "identity").encode()).hexdigest()


def _identifier(value: str, field_name: str) -> str:
    value = str(value or "").strip()
    if not _ID.fullmatch(value):
        raise ValueError(f"invalid {field_name}")
    return value


@dataclass(frozen=True)
class EmbeddingSpace:
    model_package_id: str
    dimension: int
    preprocessing: str
    metric: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_package_id", _identifier(
            self.model_package_id, "model_package_id"))
        object.__setattr__(self, "preprocessing", _identifier(
            self.preprocessing, "preprocessing"))
        if int(self.dimension) < 1 or int(self.dimension) > 1_000_000:
            raise ValueError("embedding dimension is out of bounds")
        object.__setattr__(self, "dimension", int(self.dimension))
        metric = str(self.metric or "").strip().lower()
        if metric not in {"cosine", "dot", "euclidean"}:
            raise ValueError("unsupported embedding metric")
        object.__setattr__(self, "metric", metric)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class ProjectionSpec:
    kind: str
    backend: str
    schema_version: str
    embedding: EmbeddingSpace | None = None
    schema: str = PROJECTION_SPEC_SCHEMA
    spec_id: str = field(init=False)

    def __post_init__(self) -> None:
        kind = str(self.kind or "").strip().lower()
        if kind not in {"graph", "vector"}:
            raise ValueError("projection kind must be graph or vector")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "backend", _identifier(self.backend, "backend"))
        object.__setattr__(self, "schema_version", _identifier(
            self.schema_version, "schema_version"))
        if (kind == "vector") != isinstance(self.embedding, EmbeddingSpace):
            raise ValueError("vector projections require one embedding space only")
        identity = {"schema": self.schema, "kind": kind, "backend": self.backend,
                    "schema_version": self.schema_version,
                    "embedding": self.embedding.to_dict() if self.embedding else None}
        object.__setattr__(self, "spec_id", "pspec_" + _hash(identity)[7:])

    def to_dict(self) -> dict:
        return {"schema": self.schema, "spec_id": self.spec_id, "kind": self.kind,
                "backend": self.backend, "schema_version": self.schema_version,
                "embedding": self.embedding.to_dict() if self.embedding else None}


@dataclass(frozen=True)
class ProjectionEntry:
    projection_id: str
    spec_id: str
    record_id: str
    revision_id: str
    source_content_hash: str
    projection_hash: str
    tombstone: bool
    schema: str = PROJECTION_ENTRY_SCHEMA

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def build_projection_entry(revision: RecordRevision, spec: ProjectionSpec,
                           payload: Mapping[str, Any] | None) -> ProjectionEntry:
    if not isinstance(revision, RecordRevision) or not isinstance(spec, ProjectionSpec):
        raise TypeError("revision and spec must use portable contract values")
    payload = dict(payload or {})
    if revision.tombstone:
        if payload:
            raise ValueError("tombstone projections cannot carry payload")
    elif not payload:
        raise ValueError("active projection requires payload")
    if spec.kind == "vector" and not revision.tombstone:
        vector = payload.get("embedding")
        if not isinstance(vector, list) or len(vector) != spec.embedding.dimension:
            raise ValueError("embedding does not match pinned dimension")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
               not math.isfinite(float(value)) for value in vector):
            raise ValueError("embedding must contain finite numbers")
    payload_hash = _hash(payload)
    projection_id = "proj_" + hashlib.sha256(
        f"{spec.spec_id}\0{revision.record_id}".encode()).hexdigest()
    return ProjectionEntry(projection_id, spec.spec_id, revision.record_id,
                           revision.revision_id, revision.content_hash,
                           payload_hash, revision.tombstone)


def validate_projection_entry(entry: ProjectionEntry, spec: ProjectionSpec) -> None:
    """Validate externally supplied projection evidence against one spec."""
    if not isinstance(entry, ProjectionEntry) or entry.spec_id != spec.spec_id:
        raise ValueError("projection entry does not match provider specification")
    if not _PROJECTION_ID.fullmatch(entry.projection_id) or not _RECORD_ID.fullmatch(
            entry.record_id) or not _REVISION_ID.fullmatch(entry.revision_id):
        raise ValueError("projection entry has invalid identity")
    expected_id = "proj_" + hashlib.sha256(
        f"{entry.spec_id}\0{entry.record_id}".encode()).hexdigest()
    if entry.projection_id != expected_id or not isinstance(entry.tombstone, bool):
        raise ValueError("projection entry identity checksum mismatch")
    if not _SHA256.fullmatch(entry.source_content_hash) or not _SHA256.fullmatch(
            entry.projection_hash):
        raise ValueError("projection entry has invalid hashes")


@dataclass(frozen=True)
class ProjectionReconciliation:
    provider: str
    spec_id: str
    generation: int
    expected_count: int
    observed_count: int
    missing_projection_ids: tuple[str, ...]
    unexpected_projection_ids: tuple[str, ...]
    drifted_projection_ids: tuple[str, ...]
    expected_hash: str
    observed_hash: str
    schema: str = PROJECTION_REPORT_SCHEMA

    @property
    def converged(self) -> bool:
        return not (self.missing_projection_ids or self.unexpected_projection_ids or
                    self.drifted_projection_ids)

    def to_dict(self) -> dict:
        return {**self.__dict__,
                "missing_projection_ids": list(self.missing_projection_ids),
                "unexpected_projection_ids": list(self.unexpected_projection_ids),
                "drifted_projection_ids": list(self.drifted_projection_ids),
                "converged": self.converged}


class FrozenProjectionProvider:
    """Bounded offline conformance store; it is never Fabric authority."""

    def __init__(self, spec: ProjectionSpec, *, max_records: int = MAX_PROJECTION_RECORDS):
        if not isinstance(spec, ProjectionSpec):
            raise TypeError("spec must be ProjectionSpec")
        self.spec = spec
        self.name = f"frozen-{spec.kind}-projection"
        self._max_records = max(1, min(int(max_records), MAX_PROJECTION_RECORDS))
        self._entries: dict[str, ProjectionEntry] = {}
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    def apply(self, entry: ProjectionEntry, *, expected_revision_id: str = "") -> ProjectionEntry:
        self._validate(entry)
        current = self._entries.get(entry.projection_id)
        if current == entry:
            return current
        if current and current.revision_id != str(expected_revision_id or ""):
            raise ValueError("projection compare-and-swap revision mismatch")
        if not current and expected_revision_id:
            raise ValueError("projection compare-and-swap expected missing record")
        if not current and len(self._entries) >= self._max_records:
            raise ValueError("projection provider record limit reached")
        self._entries[entry.projection_id] = entry
        self._generation += 1
        return entry

    def _validate(self, entry: ProjectionEntry) -> None:
        validate_projection_entry(entry, self.spec)

    def snapshot(self, *, cancellation: CancellationSignal | None = None) -> tuple[ProjectionEntry, ...]:
        signal = cancellation or CancellationSignal()
        values = []
        for key in sorted(self._entries):
            signal.checkpoint()
            values.append(self._entries[key])
        return tuple(values)

    def reconcile(self, expected: Sequence[ProjectionEntry], *,
                  cancellation: CancellationSignal | None = None) -> ProjectionReconciliation:
        signal = cancellation or CancellationSignal()
        expected_map: dict[str, ProjectionEntry] = {}
        for entry in expected:
            signal.checkpoint()
            self._validate(entry)
            if entry.projection_id in expected_map:
                raise ValueError("expected projection IDs must be unique")
            expected_map[entry.projection_id] = entry
            if len(expected_map) > self._max_records:
                raise ValueError("expected projection set exceeds provider limit")
        observed_map = {entry.projection_id: entry for entry in self.snapshot(
            cancellation=signal)}
        expected_ids, observed_ids = set(expected_map), set(observed_map)
        missing = tuple(sorted(expected_ids - observed_ids))
        unexpected = tuple(sorted(observed_ids - expected_ids))
        drifted = tuple(sorted(key for key in expected_ids & observed_ids
                               if expected_map[key] != observed_map[key]))
        expected_hash = _hash([expected_map[key].to_dict() for key in sorted(expected_map)])
        observed_hash = _hash([observed_map[key].to_dict() for key in sorted(observed_map)])
        return ProjectionReconciliation(
            self.name, self.spec.spec_id, self._generation, len(expected_map),
            len(observed_map), missing, unexpected, drifted, expected_hash, observed_hash)

    def rebuild(self, expected: Sequence[ProjectionEntry], *, expected_generation: int,
                cancellation: CancellationSignal | None = None) -> ProjectionReconciliation:
        if int(expected_generation) != self._generation:
            raise ValueError("projection rebuild generation mismatch")
        signal = cancellation or CancellationSignal()
        replacement: dict[str, ProjectionEntry] = {}
        for entry in expected:
            signal.checkpoint()
            self._validate(entry)
            if entry.projection_id in replacement:
                raise ValueError("rebuild projection IDs must be unique")
            if len(replacement) >= self._max_records:
                raise ValueError("rebuild projection set exceeds provider limit")
            replacement[entry.projection_id] = entry
        self._entries = replacement
        self._generation += 1
        # Cancellable validation completed before the atomic replacement. Do not
        # introduce a post-mutation checkpoint that could report cancellation
        # after the state has already changed.
        values = [replacement[key].to_dict() for key in sorted(replacement)]
        digest = _hash(values)
        return ProjectionReconciliation(
            self.name, self.spec.spec_id, self._generation, len(replacement),
            len(replacement), (), (), (), digest, digest)
