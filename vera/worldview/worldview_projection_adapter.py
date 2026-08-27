"""Bind compatible Fabric projections into a reproducible JEPA input manifest."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Sequence

from vera.fabric.dataset_provider import CancellationSignal
from vera.fabric.projection_provider import (
    MAX_PROJECTION_RECORDS,
    ProjectionEntry,
    ProjectionSpec,
    validate_projection_entry,
)


WORLDVIEW_MANIFEST_SCHEMA = "vera.worldview-projection-manifest/v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class WorldviewProjectionManifest:
    manifest_id: str
    vector_spec_id: str
    graph_spec_id: str
    vector_generation: int
    graph_generation: int
    embedding_model_package_id: str
    embedding_dimension: int
    preprocessing: str
    metric: str
    record_count: int
    tombstone_count: int
    record_revisions: tuple[tuple[str, str], ...]
    record_content_hashes: tuple[tuple[str, str], ...]
    vector_snapshot_hash: str
    graph_snapshot_hash: str
    schema: str = WORLDVIEW_MANIFEST_SCHEMA

    def to_dict(self) -> dict:
        return {**self.__dict__,
                "record_revisions": [
                    {"record_id": record_id, "revision_id": revision_id}
                    for record_id, revision_id in self.record_revisions],
                "record_content_hashes": [
                    {"record_id": record_id, "content_hash": content_hash}
                    for record_id, content_hash in self.record_content_hashes]}


class WorldviewProjectionAdapter:
    """Offline compatibility seam; never trains JEPA or reads a live backend."""

    def __init__(self, *, expected_input_dimension: int,
                 max_records: int = MAX_PROJECTION_RECORDS):
        expected_input_dimension = int(expected_input_dimension)
        if expected_input_dimension < 1:
            raise ValueError("expected_input_dimension must be positive")
        self.expected_input_dimension = expected_input_dimension
        self.max_records = max(1, min(int(max_records), MAX_PROJECTION_RECORDS))

    @staticmethod
    def _index(entries: Sequence[ProjectionEntry], spec: ProjectionSpec,
               signal: CancellationSignal, limit: int) -> dict[str, ProjectionEntry]:
        indexed: dict[str, ProjectionEntry] = {}
        for entry in entries:
            signal.checkpoint()
            validate_projection_entry(entry, spec)
            if entry.record_id in indexed:
                raise ValueError("projection snapshot contains duplicate record IDs")
            if len(indexed) >= limit:
                raise ValueError("projection snapshot exceeds Worldview record limit")
            indexed[entry.record_id] = entry
        return indexed

    def build_manifest(
            self, *, vector_spec: ProjectionSpec, graph_spec: ProjectionSpec,
            vector_entries: Sequence[ProjectionEntry],
            graph_entries: Sequence[ProjectionEntry],
            vector_generation: int, graph_generation: int,
            cancellation: CancellationSignal | None = None,
            allow_empty: bool = False) -> WorldviewProjectionManifest:
        if vector_spec.kind != "vector" or graph_spec.kind != "graph":
            raise ValueError("Worldview requires one vector and one graph specification")
        if vector_spec.embedding.dimension != self.expected_input_dimension:
            raise ValueError("vector dimension does not match Worldview input dimension")
        vector_generation, graph_generation = int(vector_generation), int(graph_generation)
        if vector_generation < 0 or graph_generation < 0:
            raise ValueError("projection generations cannot be negative")
        signal = cancellation or CancellationSignal()
        vectors = self._index(vector_entries, vector_spec, signal, self.max_records)
        graphs = self._index(graph_entries, graph_spec, signal, self.max_records)
        if set(vectors) != set(graphs):
            missing_graph = sorted(set(vectors) - set(graphs))
            missing_vector = sorted(set(graphs) - set(vectors))
            raise ValueError("projection record sets differ: "
                             f"missing_graph={len(missing_graph)}, "
                             f"missing_vector={len(missing_vector)}")
        active: list[tuple[str, str]] = []
        active_hashes: list[tuple[str, str]] = []
        tombstones = 0
        for record_id in sorted(vectors):
            signal.checkpoint()
            vector, graph = vectors[record_id], graphs[record_id]
            if (vector.revision_id != graph.revision_id or
                    vector.source_content_hash != graph.source_content_hash):
                raise ValueError("graph/vector authority revision drift")
            if vector.tombstone != graph.tombstone:
                raise ValueError("graph/vector tombstone state differs")
            if vector.tombstone:
                tombstones += 1
            else:
                active.append((record_id, vector.revision_id))
                active_hashes.append((record_id, vector.source_content_hash))
        if not active and not allow_empty:
            raise ValueError("Worldview training manifest has no active records")

        vector_evidence = [vectors[key].to_dict() for key in sorted(vectors)]
        graph_evidence = [graphs[key].to_dict() for key in sorted(graphs)]
        vector_hash, graph_hash = _hash(vector_evidence), _hash(graph_evidence)
        embedding = vector_spec.embedding
        identity = {
            "schema": WORLDVIEW_MANIFEST_SCHEMA,
            "vector_spec_id": vector_spec.spec_id,
            "graph_spec_id": graph_spec.spec_id,
            "vector_generation": vector_generation,
            "graph_generation": graph_generation,
            "embedding": embedding.to_dict(),
            "record_revisions": active,
            "record_content_hashes": active_hashes,
            "tombstone_count": tombstones,
            "vector_snapshot_hash": vector_hash,
            "graph_snapshot_hash": graph_hash,
        }
        return WorldviewProjectionManifest(
            manifest_id="wvmanifest_" + hashlib.sha256(
                _canonical(identity).encode()).hexdigest(),
            vector_spec_id=vector_spec.spec_id, graph_spec_id=graph_spec.spec_id,
            vector_generation=vector_generation, graph_generation=graph_generation,
            embedding_model_package_id=embedding.model_package_id,
            embedding_dimension=embedding.dimension,
            preprocessing=embedding.preprocessing, metric=embedding.metric,
            record_count=len(active), tombstone_count=tombstones,
            record_revisions=tuple(active), record_content_hashes=tuple(active_hashes),
            vector_snapshot_hash=vector_hash,
            graph_snapshot_hash=graph_hash)
