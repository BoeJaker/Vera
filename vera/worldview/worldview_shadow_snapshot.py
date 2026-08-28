"""Immutable offline snapshots for Worldview projection shadow comparison."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .worldview_projection_adapter import WorldviewProjectionManifest
from .worldview_shadow_parity import (
    WorldviewShadowParityReport,
    compare_legacy_worldview_snapshot,
)


MAX_SHADOW_RECORDS = 50_000
MAX_SHADOW_EDGES = 250_000
MAX_RECORD_ID_LENGTH = 256


@dataclass(frozen=True)
class LegacyWorldviewShadowRecord:
    record_id: str
    embedding: tuple[Any, ...]
    revision_id: str
    content_hash: str


@dataclass(frozen=True)
class LegacyWorldviewShadowSnapshot:
    """A copied, payload-minimal view of explicit legacy loader outputs."""

    records: tuple[LegacyWorldviewShadowRecord, ...]
    edges: tuple[tuple[str, str, str], ...]

    def compare(self, manifest: WorldviewProjectionManifest) -> WorldviewShadowParityReport:
        records = {
            item.record_id: {
                "embedding": item.embedding,
                "revision_id": item.revision_id,
                "content_hash": item.content_hash,
            }
            for item in self.records
        }
        return compare_legacy_worldview_snapshot(
            manifest=manifest, records=records, edges=self.edges)


def assemble_legacy_worldview_shadow_snapshot(
        *, record_ids: Sequence[Any], embeddings: Sequence[Any],
        metadata: Mapping[str, Any], edges: Sequence[Any],
        cancelled: Callable[[], bool] | None = None,
        max_records: int = MAX_SHADOW_RECORDS,
        max_edges: int = MAX_SHADOW_EDGES) -> LegacyWorldviewShadowSnapshot:
    """Copy aligned loader outputs into an immutable, non-serializing snapshot."""
    if cancelled and cancelled():
        raise RuntimeError("shadow snapshot assembly cancelled")
    if not isinstance(record_ids, Sequence) or isinstance(record_ids, (str, bytes)):
        raise TypeError("record_ids must be a sequence")
    if not isinstance(embeddings, Sequence) or isinstance(embeddings, (str, bytes)):
        raise TypeError("embeddings must be a sequence")
    if not isinstance(metadata, Mapping):
        raise TypeError("metadata must be a mapping")
    if not isinstance(edges, Sequence) or isinstance(edges, (str, bytes)):
        raise TypeError("edges must be a sequence")
    record_limit = max(1, min(int(max_records), MAX_SHADOW_RECORDS))
    edge_limit = max(0, min(int(max_edges), MAX_SHADOW_EDGES))
    if len(record_ids) != len(embeddings):
        raise ValueError("record IDs and embeddings must be aligned")
    if len(record_ids) > record_limit:
        raise ValueError("shadow snapshot record limit exceeded")
    if len(edges) > edge_limit:
        raise ValueError("shadow snapshot edge limit exceeded")

    frozen_records: list[LegacyWorldviewShadowRecord] = []
    seen: set[str] = set()
    for index, raw_id in enumerate(record_ids):
        if cancelled and cancelled():
            raise RuntimeError("shadow snapshot assembly cancelled")
        if not isinstance(raw_id, str) or not raw_id or len(raw_id) > MAX_RECORD_ID_LENGTH:
            raise ValueError("record IDs must be non-empty bounded strings")
        if raw_id in seen:
            raise ValueError("duplicate record ID in shadow snapshot")
        seen.add(raw_id)
        vector = embeddings[index]
        if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes)):
            vector = ()
        value = metadata.get(raw_id, {})
        if not isinstance(value, Mapping):
            value = {}
        revision_id = value.get("revision_id", "")
        content_hash = value.get("content_hash", "")
        frozen_records.append(LegacyWorldviewShadowRecord(
            record_id=raw_id,
            embedding=tuple(vector),
            revision_id=revision_id if isinstance(revision_id, str) else "",
            content_hash=content_hash if isinstance(content_hash, str) else "",
        ))

    frozen_edges: list[tuple[str, str, str]] = []
    for edge in edges:
        if (not isinstance(edge, Sequence) or isinstance(edge, (str, bytes)) or
                len(edge) != 3 or not all(isinstance(part, str) and part for part in edge)):
            raise ValueError("edges must be non-empty (source, target, relation) strings")
        frozen_edges.append((edge[0], edge[1], edge[2]))
    return LegacyWorldviewShadowSnapshot(tuple(frozen_records), tuple(frozen_edges))
