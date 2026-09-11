"""Portable, authority-preserving evidence contracts for JEPA Worldview.

The module is deliberately independent of the operational JEPA runtime.  It
validates already-produced observations against immutable DatasetSnapshot and
ModelPackage identities; it never loads a checkpoint, imports a model framework,
queries a dataset, ranks context, or executes a prediction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any, Mapping, Protocol, Sequence

from vera.fabric.dataset_provider import DatasetSnapshot
from vera.models.model_package import ModelPackage


EVIDENCE_SCHEMA = "vera.worldview-evidence/v1"
EVIDENCE_KINDS = frozenset({
    "concept", "prediction", "anomaly", "counterfactual", "drift", "reranking",
})
MAX_OBSERVATIONS = 1000
MAX_CITATIONS = 64
MAX_ATTRIBUTES = 32
MAX_ATTRIBUTES_BYTES = 16_384
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA_ID = re.compile(r"^wve_[0-9a-f]{64}$")
_FORBIDDEN_ATTRIBUTE_KEYS = frozenset({
    "body", "content", "credential", "embedding", "password", "payload",
    "prompt", "secret", "text", "token", "vector",
})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


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


def _reject_sensitive_keys(value: Any, field_name: str, *, depth: int = 0) -> None:
    if depth > 8:
        raise ValueError(f"{field_name} exceeds the nesting limit")
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = _identifier(raw_key, f"{field_name} key")
            if key.casefold() in _FORBIDDEN_ATTRIBUTE_KEYS:
                raise ValueError(
                    f"{field_name} contains a payload-bearing or secret-like key")
            _reject_sensitive_keys(child, field_name, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_sensitive_keys(child, field_name, depth=depth + 1)


def _json_object(value: Mapping[str, Any], field_name: str) -> tuple[tuple[str, Any], ...]:
    if not isinstance(value, Mapping) or len(value) > MAX_ATTRIBUTES:
        raise ValueError(f"{field_name} must be a bounded JSON object")
    frozen: list[tuple[str, Any]] = []
    _reject_sensitive_keys(value, field_name)
    try:
        encoded = _canonical(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain canonical JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_ATTRIBUTES_BYTES:
        raise ValueError(f"{field_name} exceeds the encoded size limit")
    for raw_key, raw_value in value.items():
        key = _identifier(raw_key, f"{field_name} key")
        try:
            copied = json.loads(_canonical(raw_value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must contain canonical JSON") from exc
        frozen.append((key, copied))
    return tuple(sorted(frozen))


@dataclass(frozen=True)
class EvidenceCitation:
    record_id: str
    revision_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_id", _identifier(self.record_id, "record_id"))
        object.__setattr__(self, "revision_id", _identifier(self.revision_id, "revision_id"))

    def to_dict(self) -> dict[str, str]:
        return {"record_id": self.record_id, "revision_id": self.revision_id}


@dataclass(frozen=True, init=False)
class EvidenceObservation:
    observation_id: str
    subject_id: str
    score: float | None
    citations: tuple[EvidenceCitation, ...]
    _attributes: tuple[tuple[str, Any], ...] = field(repr=False)

    def __init__(self, *, observation_id: str, subject_id: str,
                 citations: Sequence[EvidenceCitation], score: float | None = None,
                 attributes: Mapping[str, Any] | None = None):
        object.__setattr__(self, "observation_id", _identifier(
            observation_id, "observation_id"))
        object.__setattr__(self, "subject_id", _identifier(subject_id, "subject_id"))
        if score is not None and (isinstance(score, bool) or
                                  not isinstance(score, (int, float)) or
                                  not math.isfinite(score) or not 0 <= score <= 1):
            raise ValueError("score must be null or between zero and one")
        object.__setattr__(self, "score", None if score is None else float(score))
        try:
            frozen_citations = tuple(citations)
        except TypeError as exc:
            raise ValueError("citations must be a sequence") from exc
        if (not frozen_citations or len(frozen_citations) > MAX_CITATIONS or
                not all(isinstance(item, EvidenceCitation) for item in frozen_citations)):
            raise ValueError("observations require 1..64 valid citations")
        if len(set(frozen_citations)) != len(frozen_citations):
            raise ValueError("observation citations must be unique")
        object.__setattr__(self, "citations", tuple(sorted(
            frozen_citations, key=lambda item: (item.record_id, item.revision_id))))
        object.__setattr__(self, "_attributes", _json_object(
            attributes or {}, "attributes"))

    @property
    def attributes(self) -> dict[str, Any]:
        return json.loads(_canonical(dict(self._attributes)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "subject_id": self.subject_id,
            "score": self.score,
            "citations": [item.to_dict() for item in self.citations],
            "attributes": self.attributes,
        }


@dataclass(frozen=True, init=False)
class WorldviewEvidence:
    kind: str
    dataset_id: str
    snapshot_id: str
    model_package_id: str
    provider_revision: str
    observed_at: str
    observations: tuple[EvidenceObservation, ...]
    evidence_id: str = field(init=False)
    schema: str = EVIDENCE_SCHEMA

    def __init__(self, *, kind: str, snapshot: DatasetSnapshot,
                 checkpoint: ModelPackage, provider_revision: str,
                 observed_at: str, observations: Sequence[EvidenceObservation]):
        kind = str(kind or "").strip().casefold()
        if kind not in EVIDENCE_KINDS:
            raise ValueError("unsupported Worldview evidence kind")
        if not isinstance(snapshot, DatasetSnapshot):
            raise TypeError("snapshot must be DatasetSnapshot")
        if not isinstance(checkpoint, ModelPackage):
            raise TypeError("checkpoint must be ModelPackage")
        architecture = checkpoint.architecture.casefold()
        if "jepa" not in architecture or "worldview" not in architecture:
            raise ValueError("checkpoint must identify the JEPA Worldview architecture")
        compatibility = checkpoint.compatibility
        if ("worldview.evidence" not in compatibility.tasks or
                compatibility.input_contract != "vera.dataset-snapshot/v1" or
                compatibility.output_contract != EVIDENCE_SCHEMA):
            raise ValueError("checkpoint is not compatible with Worldview evidence")
        provider_revision = _identifier(provider_revision, "provider_revision")
        observed_at = _timestamp(observed_at, "observed_at")
        if datetime.fromisoformat(observed_at.replace("Z", "+00:00")) < datetime.fromisoformat(
                snapshot.created_at.replace("Z", "+00:00")):
            raise ValueError("evidence cannot predate its dataset snapshot")
        try:
            frozen = tuple(observations)
        except TypeError as exc:
            raise ValueError("observations must be a sequence") from exc
        if len(frozen) > MAX_OBSERVATIONS or not all(
                isinstance(item, EvidenceObservation) for item in frozen):
            raise ValueError("observations must contain at most 1000 valid items")
        if len({item.observation_id for item in frozen}) != len(frozen):
            raise ValueError("observation_id must be unique within evidence")
        frozen = tuple(sorted(frozen, key=lambda item: item.observation_id))
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "dataset_id", snapshot.dataset_id)
        object.__setattr__(self, "snapshot_id", snapshot.snapshot_id)
        object.__setattr__(self, "model_package_id", checkpoint.package_id)
        object.__setattr__(self, "provider_revision", provider_revision)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "observations", frozen)
        object.__setattr__(self, "schema", EVIDENCE_SCHEMA)
        object.__setattr__(self, "evidence_id", "wve_" + _hash(self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "kind": self.kind,
            "dataset_id": self.dataset_id, "snapshot_id": self.snapshot_id,
            "model_package_id": self.model_package_id,
            "provider_revision": self.provider_revision,
            "observed_at": self.observed_at,
            "observations": [item.to_dict() for item in self.observations],
        }

    def to_dict(self) -> dict[str, Any]:
        return {"evidence_id": self.evidence_id, **self.identity_dict(),
                "authority": "derived_evidence_only", "executes": False}


class EvidenceProvider(Protocol):
    provider_id: str

    def get(self, evidence_id: str) -> WorldviewEvidence | None: ...

    def list(self, *, kind: str = "", snapshot_id: str = "",
             model_package_id: str = "") -> tuple[WorldviewEvidence, ...]: ...


class FrozenEvidenceProvider:
    """Deterministic reference store for conformance and offline composition."""

    provider_id = "worldview.frozen"

    def __init__(self, evidence: Sequence[WorldviewEvidence] = ()):
        self._items: dict[str, WorldviewEvidence] = {}
        for item in evidence:
            self.add(item)

    def add(self, evidence: WorldviewEvidence) -> WorldviewEvidence:
        if not isinstance(evidence, WorldviewEvidence):
            raise TypeError("evidence must be WorldviewEvidence")
        current = self._items.get(evidence.evidence_id)
        if current is not None and current != evidence:
            raise ValueError("Worldview evidence identity collision")
        self._items[evidence.evidence_id] = evidence
        return evidence

    def get(self, evidence_id: str) -> WorldviewEvidence | None:
        if not _SHA_ID.fullmatch(str(evidence_id or "")):
            raise ValueError("invalid evidence_id")
        return self._items.get(evidence_id)

    def list(self, *, kind: str = "", snapshot_id: str = "",
             model_package_id: str = "") -> tuple[WorldviewEvidence, ...]:
        if kind and kind not in EVIDENCE_KINDS:
            raise ValueError("unsupported Worldview evidence kind")
        items = self._items.values()
        return tuple(sorted((item for item in items
                             if (not kind or item.kind == kind)
                             and (not snapshot_id or item.snapshot_id == snapshot_id)
                             and (not model_package_id or
                                  item.model_package_id == model_package_id)),
                            key=lambda item: item.evidence_id))


def evidence_availability(evidence: WorldviewEvidence | None, *,
                          expected_snapshot_id: str,
                          expected_model_package_id: str) -> dict[str, Any]:
    """Classify availability without falling back to another revision."""
    if evidence is None:
        return {"status": "unavailable", "usable": False,
                "reason": "evidence_not_available"}
    if not isinstance(evidence, WorldviewEvidence):
        raise TypeError("evidence must be WorldviewEvidence or None")
    if evidence.snapshot_id != expected_snapshot_id:
        return {"status": "stale", "usable": False,
                "reason": "dataset_snapshot_mismatch",
                "evidence_id": evidence.evidence_id}
    if evidence.model_package_id != expected_model_package_id:
        return {"status": "stale", "usable": False,
                "reason": "model_package_mismatch",
                "evidence_id": evidence.evidence_id}
    return {"status": "ready", "usable": True, "reason": "exact_identity_match",
            "evidence_id": evidence.evidence_id}
