"""Snapshot-scoped native Fabric vector retrieval for comparison evidence.

The ordinary ``fabric.query`` capability searches mutable shared projections.
That is useful product behaviour, but it cannot prove which record revision
produced an indexed vector.  This module therefore builds a small isolated
projection whose complete content is bound to one :class:`DatasetSnapshot`.
It never writes to, reads from, or relabels the shared Fabric indexes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import math
import re
import time
from typing import Any, Callable, Mapping, Sequence

from .dataset_provider import CancellationSignal, DatasetSnapshot
from .projection_provider import EmbeddingSpace, ProjectionSpec
from .retrieval_comparison import (
    RetrievalCitation,
    RetrievalLifecycleMetrics,
    RetrievalProviderProfile,
)
from .retrieval_execution import (
    RetrievalProviderFailure,
    RetrievalProviderUnavailable,
    RetrievalQueryBinding,
)


SCHEMA_VERSION = "vera.native-fabric-snapshot-projection/v1"
PROVIDER_REVISION = "native-fabric-snapshot-v1"
MAX_RECORDS = 20_000
MAX_GRAPH_EDGES = 100_000
MAX_PROJECTION_BYTES = 64 * 1024 * 1024
MAX_QUERY_BYTES = 64 * 1024
MAX_VECTOR_DIMENSION = 8_192
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{1,63}")


def _identifier(value: Any, label: str) -> str:
    value = str(value or "").strip()
    if not _ID.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _canonical(value: Any, label: str) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be canonical JSON") from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical(value, "projection material").encode("utf-8")
    ).hexdigest()


def _normalise_vector(value: Sequence[Any], *, dimension: int = 0) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError("vector must be a numeric sequence")
    try:
        vector = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError("vector must be a numeric sequence") from exc
    if not 0 < len(vector) <= MAX_VECTOR_DIMENSION:
        raise ValueError("vector dimension is outside the bounded range")
    if dimension and len(vector) != dimension:
        raise ValueError("vector dimensions must match")
    if not all(math.isfinite(item) for item in vector):
        raise ValueError("vector values must be finite")
    norm = math.sqrt(sum(item * item for item in vector))
    if norm <= 0:
        raise ValueError("vector must have non-zero magnitude")
    return tuple(item / norm for item in vector)


def _freeze_snapshot_records(
    snapshot: DatasetSnapshot,
    records: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, ...], tuple[RetrievalCitation, ...], tuple[str, ...]]:
    """Validate and freeze the common snapshot/citation boundary once."""
    if not isinstance(snapshot, DatasetSnapshot):
        raise TypeError("snapshot must be DatasetSnapshot")
    frozen_records = tuple(dict(item) for item in records)
    if not 0 < len(frozen_records) <= MAX_RECORDS:
        raise ValueError(f"records must contain 1..{MAX_RECORDS} items")
    recreated, canonical_records = DatasetSnapshot.create(
        dataset_id=snapshot.dataset_id,
        created_at=snapshot.created_at,
        records=frozen_records,
        schema=snapshot.schema,
        provenance=snapshot.provenance,
    )
    if recreated != snapshot:
        raise ValueError("records do not recreate the supplied snapshot")
    citations: list[RetrievalCitation] = []
    record_ids: list[str] = []
    for record in canonical_records:
        try:
            citation = RetrievalCitation(
                record_id=record["record_id"], revision_id=record["revision_id"])
        except KeyError as exc:
            raise ValueError("every record requires record_id and revision_id") from exc
        citations.append(citation)
        record_ids.append(citation.record_id)
    if len(set(record_ids)) != len(record_ids):
        raise ValueError("record_id values must be unique within a snapshot")
    return (
        tuple(_canonical(item, "record") for item in canonical_records),
        tuple(citations), tuple(record_ids),
    )


@dataclass(init=False)
class NativeFabricSnapshotProjection:
    """An isolated immutable vector projection over one exact snapshot."""

    snapshot: DatasetSnapshot
    provider_revision: str
    spec: ProjectionSpec
    _record_json: tuple[str, ...] = field(repr=False)
    _citations: tuple[RetrievalCitation, ...] = field(repr=False)
    _vectors: tuple[tuple[float, ...], ...] = field(repr=False)
    _projection_digest: str
    _storage_bytes: int
    _record_count: int
    _index_ms: int
    _deletion_ms: int | None
    _active: bool

    def __init__(
        self,
        *,
        snapshot: DatasetSnapshot,
        records: Sequence[Mapping[str, Any]],
        vectors_by_record_id: Mapping[str, Sequence[Any]],
        embedding: EmbeddingSpace,
        provider_revision: str = PROVIDER_REVISION,
    ) -> None:
        started = time.monotonic()
        record_json, citations, record_ids = _freeze_snapshot_records(
            snapshot, records)
        if set(vectors_by_record_id) != set(record_ids):
            raise ValueError("vectors must cover exactly the snapshot record IDs")

        if not isinstance(embedding, EmbeddingSpace):
            raise TypeError("embedding must be EmbeddingSpace")
        if embedding.metric != "cosine":
            raise ValueError("native comparison projection requires cosine embeddings")
        spec = ProjectionSpec(
            kind="vector", backend="isolated-native-fabric",
            schema_version=SCHEMA_VERSION, embedding=embedding)
        vectors: list[tuple[float, ...]] = []
        for record_id in record_ids:
            vector = _normalise_vector(
                vectors_by_record_id[record_id], dimension=embedding.dimension)
            vectors.append(vector)

        provider_revision = _identifier(provider_revision, "provider revision")
        identity = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot.snapshot_id,
            "provider_revision": provider_revision,
            "projection_spec": spec.to_dict(),
            "records": list(record_json),
            "citations": [item.to_dict() for item in citations],
            "vectors": vectors,
        }
        projection_digest = _digest(identity)
        storage_bytes = len(_canonical(identity, "projection identity").encode("utf-8"))
        if storage_bytes > MAX_PROJECTION_BYTES:
            raise ValueError("projection exceeds the bounded storage limit")

        self.snapshot = snapshot
        self.provider_revision = provider_revision
        self.spec = spec
        self._record_json = record_json
        self._citations = tuple(citations)
        self._vectors = tuple(vectors)
        self._projection_digest = projection_digest
        self._storage_bytes = storage_bytes
        self._record_count = len(citations)
        self._index_ms = max(0, int(round((time.monotonic() - started) * 1000)))
        self._deletion_ms = None
        self._active = True

    @property
    def dimension(self) -> int:
        return self.spec.embedding.dimension

    @property
    def active(self) -> bool:
        return self._active

    def _identity(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": self.snapshot.snapshot_id,
            "provider_revision": self.provider_revision,
            "projection_spec": self.spec.to_dict(),
            "records": list(self._record_json),
            "citations": [item.to_dict() for item in self._citations],
            "vectors": self._vectors,
        }

    def verify(self, snapshot: DatasetSnapshot) -> bool:
        return (
            self._active
            and snapshot == self.snapshot
            and len(self._citations) == snapshot.record_count
            and _digest(self._identity()) == self._projection_digest
        )

    def search(self, vector: Sequence[Any], limit: int) -> tuple[RetrievalCitation, ...]:
        if not self._active:
            raise RetrievalProviderUnavailable("projection_deleted")
        query = _normalise_vector(vector, dimension=self.dimension)
        ranked = sorted(
            enumerate(self._vectors),
            key=lambda item: (
                -sum(left * right for left, right in zip(query, item[1])),
                self._citations[item[0]],
            ),
        )
        return tuple(self._citations[index] for index, _ in ranked[:limit])

    def teardown(self) -> dict[str, Any]:
        if self._active:
            started = time.monotonic()
            self._active = False
            self._record_json = ()
            self._citations = ()
            self._vectors = ()
            self._deletion_ms = max(
                0, int(round((time.monotonic() - started) * 1000)))
        return self.receipt()

    def lifecycle(self) -> RetrievalLifecycleMetrics:
        return RetrievalLifecycleMetrics(
            index_ms=self._index_ms,
            storage_bytes=self._storage_bytes,
            deletion_ms=self._deletion_ms,
        )

    def receipt(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": self.snapshot.snapshot_id,
            "projection_digest": self._projection_digest,
            "provider_revision": self.provider_revision,
            "projection_spec": self.spec.to_dict(),
            "record_count": self._record_count,
            "vector_count": self._record_count,
            "dimension": self.dimension,
            "storage_bytes": self._storage_bytes,
            "index_ms": self._index_ms,
            "deletion_ms": self._deletion_ms,
            "active": self._active,
            "activation_authority": False,
        }


class NativeFabricVectorRetrievalAdapter:
    """Expose an isolated native projection to the W5-04 executor."""

    def __init__(
        self,
        *,
        projection: NativeFabricSnapshotProjection,
        embedding: EmbeddingSpace,
        embed_query: Callable[[str], Any],
    ) -> None:
        if not isinstance(projection, NativeFabricSnapshotProjection):
            raise TypeError("projection must be NativeFabricSnapshotProjection")
        if not callable(embed_query):
            raise TypeError("embed_query must be callable")
        if embedding != projection.spec.embedding:
            raise ValueError("query embedding space must match the indexed projection")
        self._projection = projection
        self._embedding = embedding
        self._embed_query = embed_query
        self.profile = RetrievalProviderProfile(
            "fabric_vector_snapshot", "fabric_vector",
            projection.provider_revision,
        )

    async def retrieve(
        self,
        snapshot: DatasetSnapshot,
        binding: RetrievalQueryBinding,
        cancellation: CancellationSignal,
    ) -> tuple[RetrievalCitation, ...]:
        cancellation.checkpoint()
        if not self._projection.verify(snapshot):
            raise RetrievalProviderUnavailable("snapshot_unavailable")
        try:
            value = self._embed_query(binding.query_text)
            vector = await value if inspect.isawaitable(value) else value
            cancellation.checkpoint()
            citations = self._projection.search(vector, binding.case.k)
        except RetrievalProviderUnavailable:
            raise
        except (TypeError, ValueError) as exc:
            raise RetrievalProviderFailure("invalid_query_embedding") from exc
        cancellation.checkpoint()
        if not self._projection.verify(snapshot):
            raise RetrievalProviderFailure("projection_integrity_failed")
        return citations

    async def lifecycle(
        self,
        snapshot: DatasetSnapshot,
        cancellation: CancellationSignal,
    ) -> RetrievalLifecycleMetrics:
        cancellation.checkpoint()
        if snapshot != self._projection.snapshot:
            return RetrievalLifecycleMetrics()
        return self._projection.lifecycle()

    async def teardown(self, cancellation: CancellationSignal | None = None) -> dict[str, Any]:
        signal = cancellation or CancellationSignal()
        signal.checkpoint()
        receipt = await asyncio.to_thread(self._projection.teardown)
        signal.checkpoint()
        return receipt

    def receipt(self) -> dict[str, Any]:
        return self._projection.receipt()


@dataclass(frozen=True, order=True)
class SnapshotGraphEdge:
    """One bounded edge whose endpoints are records in the same snapshot."""

    source_record_id: str
    target_record_id: str
    relation: str = "related_to"
    directed: bool = False
    edge_id: str = field(init=False, compare=True)

    def __post_init__(self) -> None:
        source = _identifier(self.source_record_id, "source record ID")
        target = _identifier(self.target_record_id, "target record ID")
        relation = _identifier(self.relation, "edge relation").lower()
        if source == target:
            raise ValueError("self edges are not admissible comparison evidence")
        object.__setattr__(self, "source_record_id", source)
        object.__setattr__(self, "target_record_id", target)
        object.__setattr__(self, "relation", relation)
        if not isinstance(self.directed, bool):
            raise TypeError("directed must be boolean")
        object.__setattr__(self, "edge_id", "edge_" + _digest({
            "source_record_id": source, "target_record_id": target,
            "relation": relation, "directed": self.directed,
        })[7:])

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source_record_id": self.source_record_id,
            "target_record_id": self.target_record_id,
            "relation": self.relation,
            "directed": self.directed,
        }


class NativeFabricSnapshotGraphProjection:
    """Isolated revision-bound graph with deterministic lexical seed traversal."""

    def __init__(
        self,
        *,
        snapshot: DatasetSnapshot,
        records: Sequence[Mapping[str, Any]],
        edges: Sequence[SnapshotGraphEdge],
        max_hops: int = 1,
        provider_revision: str = "native-fabric-graph-snapshot-v1",
    ) -> None:
        started = time.monotonic()
        record_json, citations, record_ids = _freeze_snapshot_records(snapshot, records)
        if isinstance(max_hops, bool) or not isinstance(max_hops, int) or not 1 <= max_hops <= 8:
            raise ValueError("max_hops must be an integer from 1 to 8")
        frozen_edges = tuple(edges)
        if len(frozen_edges) > MAX_GRAPH_EDGES:
            raise ValueError(f"edges must contain at most {MAX_GRAPH_EDGES} items")
        if not all(isinstance(item, SnapshotGraphEdge) for item in frozen_edges):
            raise TypeError("edges must contain SnapshotGraphEdge values")
        if len({item.edge_id for item in frozen_edges}) != len(frozen_edges):
            raise ValueError("graph edges must be unique")
        known = set(record_ids)
        if any(item.source_record_id not in known or item.target_record_id not in known
               for item in frozen_edges):
            raise ValueError("every graph edge endpoint must exist in the snapshot")
        spec = ProjectionSpec(
            kind="graph", backend="isolated-native-fabric",
            schema_version=SCHEMA_VERSION)
        provider_revision = _identifier(provider_revision, "provider revision")
        identity = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot.snapshot_id,
            "provider_revision": provider_revision,
            "projection_spec": spec.to_dict(),
            "max_hops": max_hops,
            "records": list(record_json),
            "citations": [item.to_dict() for item in citations],
            "edges": [item.to_dict() for item in sorted(frozen_edges)],
        }
        self.snapshot = snapshot
        self.provider_revision = provider_revision
        self.spec = spec
        self.max_hops = max_hops
        self._record_json = record_json
        self._citations = citations
        self._record_ids = record_ids
        self._edges = tuple(sorted(frozen_edges))
        self._projection_digest = _digest(identity)
        self._storage_bytes = len(_canonical(identity, "graph identity").encode("utf-8"))
        if self._storage_bytes > MAX_PROJECTION_BYTES:
            raise ValueError("graph projection exceeds the bounded storage limit")
        self._record_count = len(record_ids)
        self._edge_count = len(frozen_edges)
        self._index_ms = max(0, int(round((time.monotonic() - started) * 1000)))
        self._deletion_ms: int | None = None
        self._active = True

    def _identity(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": self.snapshot.snapshot_id,
            "provider_revision": self.provider_revision,
            "projection_spec": self.spec.to_dict(),
            "max_hops": self.max_hops,
            "records": list(self._record_json),
            "citations": [item.to_dict() for item in self._citations],
            "edges": [item.to_dict() for item in self._edges],
        }

    def verify(self, snapshot: DatasetSnapshot) -> bool:
        return (
            self._active and snapshot == self.snapshot
            and len(self._citations) == snapshot.record_count
            and _digest(self._identity()) == self._projection_digest
        )

    def search(self, text: str, limit: int) -> tuple[RetrievalCitation, ...]:
        if not self._active:
            raise RetrievalProviderUnavailable("projection_deleted")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("graph query text is required")
        if len(text.encode("utf-8")) > MAX_QUERY_BYTES:
            raise ValueError("graph query text exceeds the bounded size")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValueError("graph query limit must be an integer from 1 to 1000")
        query_tokens = tuple(dict.fromkeys(
            token.casefold() for token in _TOKEN.findall(text)[:64]))
        if not query_tokens:
            return ()
        scored: list[tuple[float, str]] = []
        for record_id, encoded in zip(self._record_ids, self._record_json):
            haystack = encoded.casefold()
            hits = sum(token in haystack for token in query_tokens)
            if hits:
                scored.append((hits / len(query_tokens), record_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if not scored:
            return ()

        adjacency: dict[str, set[str]] = {record_id: set() for record_id in self._record_ids}
        for edge in self._edges:
            adjacency[edge.source_record_id].add(edge.target_record_id)
            if not edge.directed:
                adjacency[edge.target_record_id].add(edge.source_record_id)
        citation_by_id = dict(zip(self._record_ids, self._citations))
        ordered: list[str] = []
        seen: set[str] = set()
        frontier = [record_id for _, record_id in scored]
        for record_id in frontier:
            if record_id not in seen:
                seen.add(record_id)
                ordered.append(record_id)
        for _ in range(self.max_hops):
            next_frontier: list[str] = []
            for record_id in frontier:
                for neighbour in sorted(adjacency[record_id]):
                    if neighbour not in seen:
                        seen.add(neighbour)
                        ordered.append(neighbour)
                        next_frontier.append(neighbour)
            frontier = next_frontier
            if not frontier:
                break
        return tuple(citation_by_id[item] for item in ordered[:limit])

    def lifecycle(self) -> RetrievalLifecycleMetrics:
        return RetrievalLifecycleMetrics(
            index_ms=self._index_ms, storage_bytes=self._storage_bytes,
            deletion_ms=self._deletion_ms)

    def teardown(self) -> dict[str, Any]:
        if self._active:
            started = time.monotonic()
            self._active = False
            self._record_json = ()
            self._citations = ()
            self._record_ids = ()
            self._edges = ()
            self._deletion_ms = max(0, int(round((time.monotonic() - started) * 1000)))
        return self.receipt()

    def receipt(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": self.snapshot.snapshot_id,
            "projection_digest": self._projection_digest,
            "provider_revision": self.provider_revision,
            "projection_spec": self.spec.to_dict(),
            "record_count": self._record_count,
            "edge_count": self._edge_count,
            "max_hops": self.max_hops,
            "storage_bytes": self._storage_bytes,
            "index_ms": self._index_ms,
            "deletion_ms": self._deletion_ms,
            "active": self._active,
            "activation_authority": False,
        }


class NativeFabricGraphRetrievalAdapter:
    """Expose the isolated graph projection to the W5-04 executor."""

    def __init__(self, *, projection: NativeFabricSnapshotGraphProjection) -> None:
        if not isinstance(projection, NativeFabricSnapshotGraphProjection):
            raise TypeError("projection must be NativeFabricSnapshotGraphProjection")
        self._projection = projection
        self.profile = RetrievalProviderProfile(
            "fabric_graph_snapshot", "fabric_graph", projection.provider_revision)

    async def retrieve(
        self,
        snapshot: DatasetSnapshot,
        binding: RetrievalQueryBinding,
        cancellation: CancellationSignal,
    ) -> tuple[RetrievalCitation, ...]:
        cancellation.checkpoint()
        if not self._projection.verify(snapshot):
            raise RetrievalProviderUnavailable("snapshot_unavailable")
        try:
            citations = await asyncio.to_thread(
                self._projection.search, binding.query_text, binding.case.k)
        except RetrievalProviderUnavailable:
            raise
        except (TypeError, ValueError) as exc:
            raise RetrievalProviderFailure("invalid_graph_query") from exc
        cancellation.checkpoint()
        if not self._projection.verify(snapshot):
            raise RetrievalProviderFailure("projection_integrity_failed")
        return citations

    async def lifecycle(
        self,
        snapshot: DatasetSnapshot,
        cancellation: CancellationSignal,
    ) -> RetrievalLifecycleMetrics:
        cancellation.checkpoint()
        if snapshot != self._projection.snapshot:
            return RetrievalLifecycleMetrics()
        return self._projection.lifecycle()

    async def teardown(self, cancellation: CancellationSignal | None = None) -> dict[str, Any]:
        signal = cancellation or CancellationSignal()
        signal.checkpoint()
        receipt = await asyncio.to_thread(self._projection.teardown)
        signal.checkpoint()
        return receipt

    def receipt(self) -> dict[str, Any]:
        return self._projection.receipt()
