"""Payload-free parity evidence for legacy Worldview input snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from vera.fabric.dataset_provider import CancellationSignal
from vera.fabric.projection_provider import MAX_PROJECTION_RECORDS
from vera.worldview.worldview_projection_adapter import WorldviewProjectionManifest


WORLDVIEW_SHADOW_PARITY_SCHEMA = "vera.worldview-shadow-parity/v1"
MAX_SHADOW_EDGES = 100_000


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class WorldviewShadowParityReport:
    manifest_id: str
    legacy_snapshot_hash: str
    expected_records: int
    observed_records: int
    matched_records: int
    missing_record_ids: tuple[str, ...]
    unexpected_record_ids: tuple[str, ...]
    invalid_embedding_record_ids: tuple[str, ...]
    missing_revision_evidence_record_ids: tuple[str, ...]
    drifted_revision_record_ids: tuple[str, ...]
    edge_count: int
    dangling_edge_count: int
    relation_types: tuple[str, ...]
    schema: str = WORLDVIEW_SHADOW_PARITY_SCHEMA

    @property
    def ready_for_shadow_comparison(self) -> bool:
        return not (self.missing_record_ids or self.unexpected_record_ids or
                    self.invalid_embedding_record_ids or
                    self.missing_revision_evidence_record_ids or
                    self.drifted_revision_record_ids or self.dangling_edge_count)

    def to_dict(self) -> dict:
        return {**self.__dict__,
                "missing_record_ids": list(self.missing_record_ids),
                "unexpected_record_ids": list(self.unexpected_record_ids),
                "invalid_embedding_record_ids": list(
                    self.invalid_embedding_record_ids),
                "missing_revision_evidence_record_ids": list(
                    self.missing_revision_evidence_record_ids),
                "drifted_revision_record_ids": list(
                    self.drifted_revision_record_ids),
                "relation_types": list(self.relation_types),
                "ready_for_shadow_comparison": self.ready_for_shadow_comparison}


def compare_legacy_worldview_snapshot(
        manifest: WorldviewProjectionManifest,
        records: Mapping[str, Mapping[str, Any]],
        edges: Sequence[Sequence[str]], *,
        cancellation: CancellationSignal | None = None,
        max_records: int = MAX_PROJECTION_RECORDS,
        max_edges: int = MAX_SHADOW_EDGES) -> WorldviewShadowParityReport:
    """Compare frozen legacy Chroma/Neo4j-shaped values with one manifest.

    Records may contain text and embedding payloads, but the returned report
    retains only bounded identity lists, counts, relation names and checksums.
    """
    if not isinstance(manifest, WorldviewProjectionManifest):
        raise TypeError("manifest must be WorldviewProjectionManifest")
    if not isinstance(records, Mapping):
        raise TypeError("records must be a mapping")
    max_records, max_edges = int(max_records), int(max_edges)
    if max_records < 1 or max_records > MAX_PROJECTION_RECORDS:
        raise ValueError("max_records is outside the supported record limit")
    if max_edges < 1 or max_edges > MAX_SHADOW_EDGES:
        raise ValueError("max_edges is outside the supported edge limit")
    if len(records) > max_records:
        raise ValueError("legacy Worldview snapshot exceeds record limit")
    if len(edges) > max_edges:
        raise ValueError("legacy Worldview snapshot exceeds edge limit")
    signal = cancellation or CancellationSignal()
    expected = dict(manifest.record_revisions)
    expected_hashes = dict(manifest.record_content_hashes)
    if len(expected) != len(manifest.record_revisions) or set(expected) != set(
            expected_hashes) or len(expected_hashes) != len(manifest.record_content_hashes):
        raise ValueError("Worldview manifest record evidence is inconsistent")
    observed: dict[str, Mapping[str, Any]] = {}
    invalid_embedding: list[str] = []
    missing_revision: list[str] = []
    drifted_revision: list[str] = []
    fingerprints = []
    if any(not isinstance(raw_id, str) or not raw_id for raw_id in records):
        raise ValueError("legacy Worldview record IDs must be non-empty strings")
    for raw_id in sorted(records):
        signal.checkpoint()
        record_id = str(raw_id or "")
        value = records[raw_id]
        if not record_id or not isinstance(value, Mapping):
            raise ValueError("legacy Worldview record is malformed")
        if record_id in observed:
            raise ValueError("legacy Worldview record IDs must be unique")
        observed[record_id] = value
        embedding = value.get("embedding")
        valid_embedding = (isinstance(embedding, (list, tuple)) and
                           len(embedding) == manifest.embedding_dimension and
                           all(not isinstance(item, bool) and
                               isinstance(item, (int, float)) and
                               math.isfinite(float(item)) for item in embedding))
        if not valid_embedding:
            invalid_embedding.append(record_id)
        revision_id = str(value.get("revision_id") or "")
        content_hash = str(value.get("content_hash") or "")
        if record_id in expected:
            if not revision_id or not content_hash:
                missing_revision.append(record_id)
            elif (revision_id != expected[record_id] or
                  content_hash != expected_hashes[record_id]):
                drifted_revision.append(record_id)
        fingerprints.append({
            "record_id": record_id,
            "revision_id": revision_id,
            "content_hash": content_hash,
            "embedding_hash": _hash(list(embedding)) if valid_embedding else "invalid",
            "dataset_id": str(value.get("dataset_id") or ""),
        })

    expected_ids, observed_ids = set(expected), set(observed)
    missing = tuple(sorted(expected_ids - observed_ids))
    unexpected = tuple(sorted(observed_ids - expected_ids))
    matched = len(expected_ids & observed_ids)
    dangling = 0
    relation_types = set()
    edge_fingerprints = []
    for edge in edges:
        signal.checkpoint()
        if not isinstance(edge, (list, tuple)) or len(edge) != 3:
            raise ValueError("legacy Worldview edge must be (source, target, relation)")
        source, target, relation = (str(item or "") for item in edge)
        if not source or not target or not relation or len(relation) > 128:
            raise ValueError("legacy Worldview edge is malformed")
        relation_types.add(relation)
        if source not in observed_ids or target not in observed_ids:
            dangling += 1
        edge_fingerprints.append((source, target, relation))
    snapshot_hash = _hash({"records": fingerprints,
                           "edges": sorted(edge_fingerprints)})
    return WorldviewShadowParityReport(
        manifest_id=manifest.manifest_id,
        legacy_snapshot_hash=snapshot_hash,
        expected_records=len(expected), observed_records=len(observed),
        matched_records=matched, missing_record_ids=missing,
        unexpected_record_ids=unexpected,
        invalid_embedding_record_ids=tuple(sorted(invalid_embedding)),
        missing_revision_evidence_record_ids=tuple(sorted(missing_revision)),
        drifted_revision_record_ids=tuple(sorted(drifted_revision)),
        edge_count=len(edges), dangling_edge_count=dangling,
        relation_types=tuple(sorted(relation_types)))
