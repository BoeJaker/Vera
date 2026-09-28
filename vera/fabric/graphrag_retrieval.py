"""Exact-snapshot boundary for an externally configured GraphRAG runtime.

GraphRAG owns model configuration, indexing and query execution.  Vera owns the
comparison identity and accepts evidence only when the injected runtime proves
that it indexed, queried and removed the complete requested snapshot.  This
module imports no GraphRAG package, resolves no credentials and grants no
fallback or activation authority.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import inspect
import json
import time
from typing import Any, Mapping, Protocol, Sequence

from .dataset_provider import CancellationSignal, QueryCancelled
from .external_retrieval import (
    ExternalRetrievalRequest,
    ExternalSnapshotBinding,
    SCHEMA_VERSION,
)
from .native_retrieval import _freeze_snapshot_records
from .retrieval_comparison import RetrievalCitation
from .retrieval_execution import RetrievalProviderFailure, RetrievalProviderUnavailable


RUNTIME_SCHEMA = "vera.graphrag-runtime/v1"
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_SNAPSHOT_TEXT_BYTES = 64 * 1024 * 1024
MAX_RUNTIME_CITATIONS = 20_000


@dataclass(frozen=True)
class GraphRAGDocument:
    """One revision-bound document supplied to the external indexer."""

    record_id: str
    revision_id: str
    _text: str = field(repr=False)

    @property
    def text(self) -> str:
        return self._text

    def to_runtime_dict(self) -> dict[str, str]:
        return {
            "record_id": self.record_id,
            "revision_id": self.revision_id,
            "text": self._text,
        }


class GraphRAGRuntime(Protocol):
    """Configured runtime supplied by an integration host."""

    def index(self, request: Mapping[str, Any], *,
              cancellation: CancellationSignal) -> Any: ...

    def query(self, request: Mapping[str, Any], *,
              cancellation: CancellationSignal) -> Any: ...

    def inspect(self, request: Mapping[str, Any], *,
                cancellation: CancellationSignal) -> Any: ...

    def delete(self, request: Mapping[str, Any], *,
               cancellation: CancellationSignal) -> Any: ...


async def _invoke(method: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        if inspect.iscoroutinefunction(method):
            return await method(*args, **kwargs)
        value = await asyncio.to_thread(method, *args, **kwargs)
        if inspect.isawaitable(value):
            return await value
        return value
    except QueryCancelled:
        raise
    except RetrievalProviderUnavailable:
        raise
    except RetrievalProviderFailure:
        raise
    except Exception as exc:
        raise RetrievalProviderFailure("graphrag_runtime_error") from exc


class GraphRAGSnapshotDriver:
    """Bind one GraphRAG workspace to one complete immutable snapshot."""

    def __init__(
        self,
        *,
        binding: ExternalSnapshotBinding,
        records: Sequence[Mapping[str, Any]],
        runtime: GraphRAGRuntime,
        text_field: str = "text",
    ) -> None:
        if binding.kind != "graphrag":
            raise ValueError("GraphRAG driver requires a graphrag binding")
        for name in ("index", "query", "inspect", "delete"):
            if not callable(getattr(runtime, name, None)):
                raise TypeError(f"GraphRAG runtime must implement {name}")
        if not isinstance(text_field, str) or not text_field.strip():
            raise ValueError("text_field is required")

        record_json, citations, record_ids = _freeze_snapshot_records(
            binding.snapshot, records)
        if citations != binding.citations:
            raise ValueError("records must match the binding citation manifest")
        documents: list[GraphRAGDocument] = []
        total_bytes = 0
        for raw, citation, record_id in zip(record_json, citations, record_ids):
            record = json.loads(raw)
            text = record.get(text_field)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("every GraphRAG record requires non-empty text")
            size = len(text.encode("utf-8"))
            if size > MAX_DOCUMENT_BYTES:
                raise ValueError("GraphRAG document exceeds the byte limit")
            total_bytes += size
            documents.append(GraphRAGDocument(
                record_id=record_id,
                revision_id=citation.revision_id,
                _text=text,
            ))
        if total_bytes > MAX_SNAPSHOT_TEXT_BYTES:
            raise ValueError("GraphRAG snapshot text exceeds the byte limit")

        self.binding = binding
        self._runtime = runtime
        self._documents = tuple(documents)
        self._citation_set = frozenset(citations)
        self._workspace_id = "vera_graphrag_" + binding.projection_id[6:46]
        self._provisioned = False
        self._index_ms: int | None = None
        self._deletion_ms: int | None = None

    def _identity(self) -> dict[str, Any]:
        return {
            "schema": RUNTIME_SCHEMA,
            "snapshot_id": self.binding.snapshot.snapshot_id,
            "projection_id": self.binding.projection_id,
            "provider_revision": self.binding.provider_revision,
            "mode": self.binding.mode,
            "workspace_id": self._workspace_id,
        }

    def _external_identity(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "snapshot_id": self.binding.snapshot.snapshot_id,
            "projection_id": self.binding.projection_id,
            "provider_revision": self.binding.provider_revision,
            "mode": self.binding.mode,
        }

    def _request(self) -> dict[str, Any]:
        return {
            **self._identity(),
            "documents": [item.to_runtime_dict() for item in self._documents],
            "citation_manifest": [item.to_dict() for item in self.binding.citations],
        }

    def _validate_identity(self, receipt: Any) -> Mapping[str, Any]:
        if not isinstance(receipt, Mapping):
            raise RetrievalProviderFailure("invalid_graphrag_receipt")
        if any(receipt.get(key) != value for key, value in self._identity().items()):
            raise RetrievalProviderFailure("graphrag_identity_mismatch")
        return receipt

    def _citations(self, value: Any, *, complete: bool) -> tuple[RetrievalCitation, ...]:
        if (not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or
                len(value) > MAX_RUNTIME_CITATIONS):
            raise RetrievalProviderFailure("invalid_graphrag_citations")
        citations: list[RetrievalCitation] = []
        for raw in value:
            if not isinstance(raw, Mapping):
                raise RetrievalProviderFailure("invalid_graphrag_citations")
            try:
                citation = RetrievalCitation(
                    record_id=raw["record_id"], revision_id=raw["revision_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RetrievalProviderFailure("invalid_graphrag_citations") from exc
            if citation not in self._citation_set:
                raise RetrievalProviderFailure("graphrag_citation_outside_snapshot")
            citations.append(citation)
        if len(set(citations)) != len(citations):
            raise RetrievalProviderFailure("duplicate_graphrag_citation")
        if complete and frozenset(citations) != self._citation_set:
            raise RetrievalProviderFailure("incomplete_graphrag_manifest")
        return tuple(citations)

    async def provision(self, *, cancellation: CancellationSignal) -> Mapping[str, Any]:
        cancellation.checkpoint()
        started = time.monotonic()
        receipt = self._validate_identity(await _invoke(
            self._runtime.index, self._request(), cancellation=cancellation))
        self._citations(receipt.get("citation_manifest"), complete=True)
        if receipt.get("active") is not True:
            raise RetrievalProviderFailure("graphrag_index_not_active")
        if receipt.get("record_count") != len(self._documents):
            raise RetrievalProviderFailure("graphrag_record_count_mismatch")
        cancellation.checkpoint()
        self._index_ms = max(0, int((time.monotonic() - started) * 1000))
        self._provisioned = True
        return {
            **self._external_identity(),
            "workspace_id": self._workspace_id,
            "record_count": len(self._documents),
            "index_ms": self._index_ms,
            "active": True,
            "activation_authority": False,
        }

    async def query(self, request: ExternalRetrievalRequest, *,
                    cancellation: CancellationSignal) -> Mapping[str, Any]:
        cancellation.checkpoint()
        if not self._provisioned:
            raise RetrievalProviderUnavailable("graphrag_unavailable")
        expected = self._external_identity()
        if any(getattr(request, key) != value for key, value in expected.items()
               if key != "schema"):
            raise RetrievalProviderFailure("receipt_identity_mismatch")
        runtime_request = {
            **self._identity(),
            "query": request.query_text,
            "limit": request.limit,
            "return_answer": False,
            "return_context": False,
            "citation_fields": ["record_id", "revision_id"],
        }
        receipt = self._validate_identity(await _invoke(
            self._runtime.query, runtime_request, cancellation=cancellation))
        citations = self._citations(receipt.get("citations"), complete=False)
        if len(citations) > request.limit:
            raise RetrievalProviderFailure("result_limit_exceeded")
        cancellation.checkpoint()
        return {
            **self._external_identity(),
            "matches": [item.to_dict() for item in citations],
        }

    async def lifecycle(self, binding: ExternalSnapshotBinding, *,
                        cancellation: CancellationSignal) -> Mapping[str, Any]:
        cancellation.checkpoint()
        if binding != self.binding or not self._provisioned:
            raise RetrievalProviderUnavailable("graphrag_unavailable")
        receipt = self._validate_identity(await _invoke(
            self._runtime.inspect, self._identity(), cancellation=cancellation))
        self._citations(receipt.get("citation_manifest"), complete=True)
        if receipt.get("active") is not True:
            raise RetrievalProviderUnavailable("graphrag_unavailable")
        if receipt.get("record_count") != len(self._documents):
            raise RetrievalProviderFailure("graphrag_record_count_mismatch")
        metrics = receipt.get("metrics")
        if not isinstance(metrics, Mapping):
            raise RetrievalProviderFailure("invalid_graphrag_lifecycle")
        clean: dict[str, int | None] = {"index_ms": self._index_ms}
        for name in ("update_ms", "storage_bytes", "rebuild_ms"):
            value = metrics.get(name)
            if value is not None and (isinstance(value, bool) or
                                      not isinstance(value, int) or value < 0):
                raise RetrievalProviderFailure("invalid_graphrag_lifecycle")
            clean[name] = value
        clean["deletion_ms"] = self._deletion_ms
        return {**self._external_identity(), "metrics": clean}

    async def recover(self, binding: ExternalSnapshotBinding,
                      cancellation: CancellationSignal) -> None:
        cancellation.checkpoint()
        if binding != self.binding:
            raise RetrievalProviderFailure("receipt_identity_mismatch")
        recover = getattr(self._runtime, "recover", None)
        if callable(recover):
            receipt = self._validate_identity(await _invoke(
                recover, self._identity(), cancellation=cancellation))
            if receipt.get("active") is not True:
                raise RetrievalProviderUnavailable("graphrag_unavailable")
        await self.lifecycle(binding, cancellation=cancellation)

    async def teardown(self, binding: ExternalSnapshotBinding, *,
                       cancellation: CancellationSignal) -> Mapping[str, Any]:
        cancellation.checkpoint()
        if binding != self.binding:
            raise RetrievalProviderFailure("receipt_identity_mismatch")
        started = time.monotonic()
        receipt = self._validate_identity(await _invoke(
            self._runtime.delete, self._identity(), cancellation=cancellation))
        if receipt.get("active") is not False:
            raise RetrievalProviderFailure("graphrag_teardown_not_confirmed")
        self._deletion_ms = max(0, int((time.monotonic() - started) * 1000))
        self._provisioned = False
        return {
            **self._external_identity(),
            "snapshot_id": self.binding.snapshot.snapshot_id,
            "active": False,
            "deletion_ms": self._deletion_ms,
            "activation_authority": False,
        }
