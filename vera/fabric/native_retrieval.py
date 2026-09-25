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
MAX_VECTOR_DIMENSION = 8_192
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


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
                    record_id=record["record_id"],
                    revision_id=record["revision_id"],
                )
            except KeyError as exc:
                raise ValueError("every record requires record_id and revision_id") from exc
            citations.append(citation)
            record_ids.append(citation.record_id)
        if len(set(record_ids)) != len(record_ids):
            raise ValueError("record_id values must be unique within a snapshot")
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

        record_json = tuple(_canonical(item, "record") for item in canonical_records)
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
