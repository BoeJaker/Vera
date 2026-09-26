"""Fail-closed external retrieval adapters for W5-04 comparison evidence.

The adapters in this module do not import, install, select, or activate an
external backend.  A caller must inject a driver that has already materialised
one exact :class:`DatasetSnapshot`.  Every request and receipt is bound to the
snapshot, provider revision, projection identity and retrieval mode; an absent
driver is represented by :class:`UnavailableRetrievalAdapter`, never by a
fallback to another index.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import inspect
import json
from typing import Any, Mapping, Protocol, Sequence

from .dataset_provider import CancellationSignal, DatasetSnapshot, QueryCancelled
from .native_retrieval import _freeze_snapshot_records, _identifier
from .retrieval_comparison import (
    RetrievalCitation,
    RetrievalLifecycleMetrics,
    RetrievalProviderProfile,
)
from .retrieval_execution import (
    RetrievalProviderFailure,
    RetrievalProviderUnavailable,
    RetrievalQueryBinding,
    UnavailableRetrievalAdapter,
)


SCHEMA_VERSION = "vera.external-snapshot-retrieval/v1"
MAX_RECEIPT_MATCHES = 1_000
QDRANT_MODES = frozenset({"dense", "sparse", "hybrid", "multivector"})
GRAPHRAG_MODES = frozenset({"local", "global", "drift"})


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("external retrieval identity must be finite JSON") from exc


def _projection_id(value: Mapping[str, Any]) -> str:
    return "xproj_" + hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True, init=False)
class ExternalSnapshotBinding:
    """Complete, immutable identity for one external comparison projection."""

    snapshot: DatasetSnapshot
    kind: str
    provider_revision: str
    mode: str
    projection_revision: str
    citations: tuple[RetrievalCitation, ...]
    projection_id: str = field(init=False)

    def __init__(
        self,
        *,
        snapshot: DatasetSnapshot,
        records: Sequence[Mapping[str, Any]],
        kind: str,
        provider_revision: str,
        mode: str,
        projection_revision: str,
    ) -> None:
        _, citations, _ = _freeze_snapshot_records(snapshot, records)
        object.__setattr__(self, "snapshot", snapshot)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "provider_revision", provider_revision)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "projection_revision", projection_revision)
        object.__setattr__(self, "citations", citations)
        self.__post_init__()

    @classmethod
    def create(
        cls,
        *,
        snapshot: DatasetSnapshot,
        records: Sequence[Mapping[str, Any]],
        kind: str,
        provider_revision: str,
        mode: str,
        projection_revision: str,
    ) -> "ExternalSnapshotBinding":
        return cls(
            snapshot=snapshot,
            records=records,
            kind=kind,
            provider_revision=provider_revision,
            mode=mode,
            projection_revision=projection_revision,
        )

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, DatasetSnapshot):
            raise TypeError("snapshot must be DatasetSnapshot")
        modes = QDRANT_MODES if self.kind == "qdrant" else (
            GRAPHRAG_MODES if self.kind == "graphrag" else frozenset())
        if not modes:
            raise ValueError("external retrieval kind must be qdrant or graphrag")
        if self.mode not in modes:
            raise ValueError(f"unsupported {self.kind} retrieval mode")
        object.__setattr__(self, "provider_revision", _identifier(
            self.provider_revision, "provider revision"))
        object.__setattr__(self, "projection_revision", _identifier(
            self.projection_revision, "projection revision"))
        citations = tuple(self.citations)
        if len(citations) != self.snapshot.record_count:
            raise ValueError("citation manifest must cover the complete snapshot")
        if len(set(citations)) != len(citations):
            raise ValueError("citation manifest must be unique")
        object.__setattr__(self, "citations", citations)
        object.__setattr__(self, "projection_id", _projection_id({
            "schema": SCHEMA_VERSION,
            "snapshot_id": self.snapshot.snapshot_id,
            "kind": self.kind,
            "provider_revision": self.provider_revision,
            "mode": self.mode,
            "projection_revision": self.projection_revision,
            "citations": [item.to_dict() for item in citations],
        }))

    @property
    def profile(self) -> RetrievalProviderProfile:
        return RetrievalProviderProfile(
            provider_id=f"{self.kind}-{self.mode}",
            kind=self.kind,
            revision=self.provider_revision,
        )


@dataclass(frozen=True)
class ExternalRetrievalRequest:
    snapshot_id: str
    projection_id: str
    provider_revision: str
    mode: str
    limit: int
    _query_text: str = field(repr=False)

    @property
    def query_text(self) -> str:
        return self._query_text


class ExternalRetrievalDriver(Protocol):
    """Injected integration seam; core Vera owns none of its authority."""

    def query(self, request: ExternalRetrievalRequest, *,
              cancellation: CancellationSignal) -> Mapping[str, Any]: ...

    def lifecycle(self, binding: ExternalSnapshotBinding, *,
                  cancellation: CancellationSignal) -> Mapping[str, Any]: ...


async def _invoke(method: Any, *args: Any, **kwargs: Any) -> Any:
    if inspect.iscoroutinefunction(method):
        return await method(*args, **kwargs)
    return await asyncio.to_thread(method, *args, **kwargs)


class ExternalSnapshotRetrievalAdapter:
    """Validate an injected Qdrant/GraphRAG driver at Vera's evidence boundary."""

    def __init__(self, *, binding: ExternalSnapshotBinding,
                 driver: ExternalRetrievalDriver) -> None:
        if not isinstance(binding, ExternalSnapshotBinding):
            raise TypeError("binding must be ExternalSnapshotBinding")
        if not callable(getattr(driver, "query", None)) or not callable(
                getattr(driver, "lifecycle", None)):
            raise TypeError("driver must implement query and lifecycle")
        self.binding = binding
        self.profile = binding.profile
        self._driver = driver
        self._citation_set = frozenset(binding.citations)

    def _assert_snapshot(self, snapshot: DatasetSnapshot) -> None:
        if not isinstance(snapshot, DatasetSnapshot) or snapshot != self.binding.snapshot:
            raise RetrievalProviderUnavailable("snapshot_unavailable")

    async def retrieve(
        self,
        snapshot: DatasetSnapshot,
        binding: RetrievalQueryBinding,
        cancellation: CancellationSignal,
    ) -> tuple[RetrievalCitation, ...]:
        self._assert_snapshot(snapshot)
        cancellation.checkpoint()
        request = ExternalRetrievalRequest(
            snapshot_id=snapshot.snapshot_id,
            projection_id=self.binding.projection_id,
            provider_revision=self.binding.provider_revision,
            mode=self.binding.mode,
            limit=binding.case.k,
            _query_text=binding.query_text,
        )
        try:
            receipt = await _invoke(
                self._driver.query, request, cancellation=cancellation)
        except QueryCancelled:
            raise
        except RetrievalProviderUnavailable:
            raise
        except Exception as exc:
            raise RetrievalProviderFailure("provider_error") from exc
        cancellation.checkpoint()
        return self._validate_query_receipt(receipt, limit=binding.case.k)

    def _validate_identity(self, receipt: Mapping[str, Any]) -> None:
        if not isinstance(receipt, Mapping):
            raise RetrievalProviderFailure("invalid_receipt")
        expected = {
            "schema": SCHEMA_VERSION,
            "snapshot_id": self.binding.snapshot.snapshot_id,
            "projection_id": self.binding.projection_id,
            "provider_revision": self.binding.provider_revision,
            "mode": self.binding.mode,
        }
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise RetrievalProviderFailure("receipt_identity_mismatch")

    def _validate_query_receipt(
        self, receipt: Mapping[str, Any], *, limit: int,
    ) -> tuple[RetrievalCitation, ...]:
        self._validate_identity(receipt)
        matches = receipt.get("matches")
        if not isinstance(matches, Sequence) or isinstance(matches, (str, bytes)):
            raise RetrievalProviderFailure("invalid_citation")
        if len(matches) > min(limit, MAX_RECEIPT_MATCHES):
            raise RetrievalProviderFailure("result_limit_exceeded")
        citations: list[RetrievalCitation] = []
        for match in matches:
            if not isinstance(match, Mapping):
                raise RetrievalProviderFailure("invalid_citation")
            try:
                citation = RetrievalCitation(
                    record_id=match["record_id"], revision_id=match["revision_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RetrievalProviderFailure("invalid_citation") from exc
            if citation not in self._citation_set:
                raise RetrievalProviderFailure("citation_outside_snapshot")
            citations.append(citation)
        if len(set(citations)) != len(citations):
            raise RetrievalProviderFailure("duplicate_citation")
        return tuple(citations)

    async def lifecycle(
        self, snapshot: DatasetSnapshot, cancellation: CancellationSignal,
    ) -> RetrievalLifecycleMetrics:
        self._assert_snapshot(snapshot)
        cancellation.checkpoint()
        try:
            receipt = await _invoke(
                self._driver.lifecycle, self.binding, cancellation=cancellation)
        except QueryCancelled:
            raise
        except RetrievalProviderUnavailable:
            raise
        except Exception as exc:
            raise RetrievalProviderFailure("provider_error") from exc
        cancellation.checkpoint()
        self._validate_identity(receipt)
        metrics = receipt.get("metrics")
        if not isinstance(metrics, Mapping):
            raise RetrievalProviderFailure("invalid_lifecycle_receipt")
        try:
            return RetrievalLifecycleMetrics(
                index_ms=metrics.get("index_ms"),
                update_ms=metrics.get("update_ms"),
                storage_bytes=metrics.get("storage_bytes"),
                rebuild_ms=metrics.get("rebuild_ms"),
                deletion_ms=metrics.get("deletion_ms"),
            )
        except (TypeError, ValueError) as exc:
            raise RetrievalProviderFailure("invalid_lifecycle_receipt") from exc


def external_adapter(
    *, binding: ExternalSnapshotBinding, driver: ExternalRetrievalDriver | None,
) -> ExternalSnapshotRetrievalAdapter | UnavailableRetrievalAdapter:
    """Return an exact adapter or explicit unavailability—never a fallback."""
    if driver is None:
        return UnavailableRetrievalAdapter(
            binding.profile, error_code=f"{binding.kind}_unavailable")
    return ExternalSnapshotRetrievalAdapter(binding=binding, driver=driver)
