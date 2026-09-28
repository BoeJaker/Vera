"""Optional Qdrant HTTP driver for exact-snapshot retrieval evidence.

No Qdrant package is imported.  The driver owns one deterministic collection
whose name is derived from an :class:`ExternalSnapshotBinding`, and exposes the
injected-driver seam consumed by ``ExternalSnapshotRetrievalAdapter``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import time
from typing import Any, Callable, Mapping, Sequence
from urllib import error, parse, request
import uuid

from .dataset_provider import CancellationSignal, QueryCancelled
from .external_retrieval import (
    ExternalRetrievalRequest,
    ExternalSnapshotBinding,
    SCHEMA_VERSION,
)
from .retrieval_execution import RetrievalProviderFailure, RetrievalProviderUnavailable


MAX_HTTP_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_POINTS = 20_000
MAX_VECTOR_DIMENSION = 8_192
MAX_MULTIVECTOR_ROWS = 256
MAX_SPARSE_VALUES = 65_536


def _finite_vector(value: Any, *, dimension: int = 0) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("dense vector must be a numeric sequence")
    try:
        vector = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError("dense vector must be a numeric sequence") from exc
    if not 0 < len(vector) <= MAX_VECTOR_DIMENSION:
        raise ValueError("dense vector dimension is outside the bounded range")
    if dimension and len(vector) != dimension:
        raise ValueError("dense vector dimensions must match")
    if not all(math.isfinite(item) for item in vector):
        raise ValueError("dense vector values must be finite")
    return vector


def _sparse_vector(value: Any) -> dict[str, list[Any]]:
    if not isinstance(value, Mapping):
        raise ValueError("sparse vector must be a mapping")
    indexes, values = value.get("indices"), value.get("values")
    if (not isinstance(indexes, Sequence) or isinstance(indexes, (str, bytes)) or
            not isinstance(values, Sequence) or isinstance(values, (str, bytes))):
        raise ValueError("sparse vector requires indices and values")
    if not 0 < len(indexes) == len(values) <= MAX_SPARSE_VALUES:
        raise ValueError("sparse vector shape is invalid")
    clean_indexes = []
    clean_values = []
    previous = -1
    for index, item in zip(indexes, values):
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("sparse vector indices must be non-negative integers")
        if index <= previous:
            raise ValueError("sparse vector indices must be unique and increasing")
        score = float(item)
        if not math.isfinite(score):
            raise ValueError("sparse vector values must be finite")
        clean_indexes.append(index)
        clean_values.append(score)
        previous = index
    return {"indices": clean_indexes, "values": clean_values}


def _multivector(value: Any, *, dimension: int = 0) -> tuple[tuple[float, ...], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("multivector must be a sequence of dense vectors")
    rows = tuple(_finite_vector(item, dimension=dimension) for item in value)
    if not 0 < len(rows) <= MAX_MULTIVECTOR_ROWS:
        raise ValueError("multivector row count is outside the bounded range")
    expected = dimension or len(rows[0])
    if any(len(item) != expected for item in rows):
        raise ValueError("multivector dimensions must match")
    return rows


@dataclass(frozen=True)
class QdrantVectorMaterial:
    dense: tuple[float, ...] | None = None
    sparse: Mapping[str, Sequence[Any]] | None = None
    multivector: tuple[tuple[float, ...], ...] | None = None


class QdrantHTTPTransport:
    """Small bounded JSON transport; credentials remain outside this object."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 30.0) -> None:
        parsed = parse.urlsplit(str(base_url or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Qdrant base URL must be absolute HTTP(S)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Qdrant base URL cannot contain credentials, query or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("Qdrant base URL cannot contain a path")
        if (isinstance(timeout_seconds, bool) or
                not 0.01 <= float(timeout_seconds) <= 300.0):
            raise ValueError("timeout_seconds is outside the bounded range")
        self._base_url = base_url.rstrip("/")
        self._timeout = float(timeout_seconds)

    def request(self, method: str, path: str,
                body: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        if not path.startswith("/") or ".." in path:
            raise ValueError("invalid Qdrant request path")
        payload = (json.dumps(body, sort_keys=True, separators=(",", ":"),
                              allow_nan=False).encode() if body is not None else None)
        req = request.Request(
            self._base_url + path, data=payload, method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with request.urlopen(req, timeout=self._timeout) as response:
                raw = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
        except (error.HTTPError, error.URLError, TimeoutError, OSError) as exc:
            raise RetrievalProviderUnavailable("qdrant_unavailable") from exc
        if len(raw) > MAX_HTTP_RESPONSE_BYTES:
            raise RetrievalProviderFailure("qdrant_response_too_large")
        try:
            value = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RetrievalProviderFailure("invalid_qdrant_response") from exc
        if not isinstance(value, Mapping):
            raise RetrievalProviderFailure("invalid_qdrant_response")
        return value


class QdrantSnapshotDriver:
    """Provision and query one isolated collection for one exact snapshot."""

    def __init__(
        self,
        *,
        binding: ExternalSnapshotBinding,
        transport: Any,
        vectors_by_record_id: Mapping[str, QdrantVectorMaterial],
        encode_query: Callable[[str, str], QdrantVectorMaterial],
    ) -> None:
        if binding.kind != "qdrant":
            raise ValueError("Qdrant driver requires a qdrant binding")
        if not callable(getattr(transport, "request", None)):
            raise TypeError("transport must implement request")
        if not callable(encode_query):
            raise TypeError("encode_query must be callable")
        record_ids = tuple(item.record_id for item in binding.citations)
        if not 0 < len(record_ids) <= MAX_POINTS:
            raise ValueError("Qdrant projection size is outside the bounded range")
        if set(vectors_by_record_id) != set(record_ids):
            raise ValueError("Qdrant vectors must cover exactly the snapshot records")
        self.binding = binding
        self._transport = transport
        self._encode_query = encode_query
        self._collection = "vera_" + binding.projection_id[6:46]
        self._vectors, self._dimension = self._validate_materials(
            record_ids, vectors_by_record_id)
        self._provisioned = False
        self._index_ms: int | None = None
        self._deletion_ms: int | None = None

    def _validate_materials(self, record_ids, supplied):
        dimension = 0
        values = {}
        for record_id in record_ids:
            material = supplied[record_id]
            if not isinstance(material, QdrantVectorMaterial):
                raise TypeError("Qdrant vector material has the wrong type")
            item = {}
            if self.binding.mode in {"dense", "hybrid"}:
                dense = _finite_vector(material.dense or (), dimension=dimension)
                dimension = dimension or len(dense)
                item["dense"] = list(dense)
            if self.binding.mode in {"sparse", "hybrid"}:
                item["sparse"] = _sparse_vector(material.sparse)
            if self.binding.mode == "multivector":
                multi = _multivector(material.multivector or (), dimension=dimension)
                dimension = dimension or len(multi[0])
                item["multivector"] = [list(row) for row in multi]
            values[record_id] = item
        return values, dimension

    def _identity(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "snapshot_id": self.binding.snapshot.snapshot_id,
            "projection_id": self.binding.projection_id,
            "provider_revision": self.binding.provider_revision,
            "mode": self.binding.mode,
        }

    def _path(self, suffix: str = "") -> str:
        return "/collections/" + parse.quote(self._collection, safe="") + suffix

    @staticmethod
    def _ok(response: Mapping[str, Any]) -> None:
        if response.get("status") not in {"ok", "acknowledged"}:
            raise RetrievalProviderFailure("qdrant_operation_failed")

    def _collection_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {"vectors": {}}
        if self.binding.mode in {"dense", "hybrid"}:
            config["vectors"]["dense"] = {
                "size": self._dimension, "distance": "Cosine"}
        if self.binding.mode in {"sparse", "hybrid"}:
            config["sparse_vectors"] = {"sparse": {}}
        if self.binding.mode == "multivector":
            config["vectors"]["multivector"] = {
                "size": self._dimension,
                "distance": "Cosine",
                "multivector_config": {"comparator": "max_sim"},
            }
        return config

    def provision(self, *, cancellation: CancellationSignal) -> Mapping[str, Any]:
        cancellation.checkpoint()
        started = time.monotonic()
        created = self._transport.request("PUT", self._path(), self._collection_config())
        self._ok(created)
        points = []
        revisions = {item.record_id: item.revision_id for item in self.binding.citations}
        for record_id, vectors in self._vectors.items():
            points.append({
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                     self.binding.projection_id + ":" + record_id)),
                "vector": vectors,
                "payload": {
                    "snapshot_id": self.binding.snapshot.snapshot_id,
                    "record_id": record_id,
                    "revision_id": revisions[record_id],
                },
            })
        uploaded = self._transport.request(
            "PUT", self._path("/points?wait=true"), {"points": points})
        self._ok(uploaded)
        cancellation.checkpoint()
        info = self._transport.request("GET", self._path())
        self._ok(info)
        count = (info.get("result") or {}).get("points_count")
        if count != len(points):
            raise RetrievalProviderFailure("qdrant_point_count_mismatch")
        self._index_ms = max(0, int((time.monotonic() - started) * 1000))
        self._provisioned = True
        return {**self._identity(), "collection": self._collection,
                "record_count": len(points), "index_ms": self._index_ms,
                "active": True, "activation_authority": False}

    def _query_material(self, text: str) -> QdrantVectorMaterial:
        try:
            material = self._encode_query(text, self.binding.mode)
        except Exception as exc:
            raise RetrievalProviderFailure("query_encoding_failed") from exc
        if not isinstance(material, QdrantVectorMaterial):
            raise RetrievalProviderFailure("query_encoding_failed")
        return material

    def _query_body(self, material: QdrantVectorMaterial, limit: int) -> dict[str, Any]:
        body: dict[str, Any] = {
            "limit": limit,
            "with_payload": ["snapshot_id", "record_id", "revision_id"],
            "with_vector": False,
            "filter": {"must": [{"key": "snapshot_id", "match": {
                "value": self.binding.snapshot.snapshot_id}}]},
        }
        mode = self.binding.mode
        if mode == "dense":
            body.update(query=list(_finite_vector(material.dense or (),
                                                  dimension=self._dimension)), using="dense")
        elif mode == "sparse":
            body.update(query=_sparse_vector(material.sparse), using="sparse")
        elif mode == "multivector":
            body.update(query=[list(row) for row in _multivector(
                material.multivector or (), dimension=self._dimension)],
                using="multivector")
        else:
            dense = list(_finite_vector(material.dense or (), dimension=self._dimension))
            sparse = _sparse_vector(material.sparse)
            body.update(
                prefetch=[
                    {"query": dense, "using": "dense", "limit": limit},
                    {"query": sparse, "using": "sparse", "limit": limit},
                ],
                query={"fusion": "rrf"},
            )
        return body

    def query(self, request_value: ExternalRetrievalRequest, *,
              cancellation: CancellationSignal) -> Mapping[str, Any]:
        cancellation.checkpoint()
        if not self._provisioned:
            raise RetrievalProviderUnavailable("qdrant_unavailable")
        expected = self._identity()
        if any(getattr(request_value, key) != value for key, value in expected.items()
               if key != "schema"):
            raise RetrievalProviderFailure("receipt_identity_mismatch")
        response = self._transport.request(
            "POST", self._path("/points/query"),
            self._query_body(self._query_material(request_value.query_text),
                             request_value.limit))
        self._ok(response)
        result = response.get("result")
        points = result.get("points") if isinstance(result, Mapping) else None
        if not isinstance(points, Sequence) or isinstance(points, (str, bytes)):
            raise RetrievalProviderFailure("invalid_qdrant_response")
        matches = []
        for point in points:
            payload = point.get("payload") if isinstance(point, Mapping) else None
            if not isinstance(payload, Mapping):
                raise RetrievalProviderFailure("invalid_qdrant_response")
            if payload.get("snapshot_id") != self.binding.snapshot.snapshot_id:
                raise RetrievalProviderFailure("qdrant_snapshot_mismatch")
            matches.append({"record_id": payload.get("record_id"),
                            "revision_id": payload.get("revision_id")})
        cancellation.checkpoint()
        return {**self._identity(), "matches": matches}

    def lifecycle(self, binding: ExternalSnapshotBinding, *,
                  cancellation: CancellationSignal) -> Mapping[str, Any]:
        cancellation.checkpoint()
        if binding != self.binding or not self._provisioned:
            raise RetrievalProviderUnavailable("qdrant_unavailable")
        info = self._transport.request("GET", self._path())
        self._ok(info)
        result = info.get("result") if isinstance(info, Mapping) else None
        if not isinstance(result, Mapping) or result.get("points_count") != len(
                self.binding.citations):
            raise RetrievalProviderFailure("qdrant_point_count_mismatch")
        storage = result.get("disk_data_size")
        if isinstance(storage, bool) or not isinstance(storage, int) or storage < 0:
            storage = None
        return {**self._identity(), "metrics": {
            "index_ms": self._index_ms, "update_ms": None,
            "storage_bytes": storage, "rebuild_ms": None,
            "deletion_ms": self._deletion_ms}}

    def recover(self, binding: ExternalSnapshotBinding,
                cancellation: CancellationSignal) -> None:
        self.lifecycle(binding, cancellation=cancellation)

    def teardown(self, binding: ExternalSnapshotBinding, *,
                 cancellation: CancellationSignal) -> Mapping[str, Any]:
        cancellation.checkpoint()
        if binding != self.binding:
            raise RetrievalProviderFailure("receipt_identity_mismatch")
        started = time.monotonic()
        deleted = self._transport.request("DELETE", self._path())
        self._ok(deleted)
        self._deletion_ms = max(0, int((time.monotonic() - started) * 1000))
        self._provisioned = False
        return {**self._identity(), "snapshot_id": self.binding.snapshot.snapshot_id,
                "active": False, "deletion_ms": self._deletion_ms,
                "activation_authority": False}
