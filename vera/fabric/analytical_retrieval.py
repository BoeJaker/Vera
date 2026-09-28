"""Snapshot-pinned analytical retrieval evidence over a QueryProvider.

Analytical comparison cases use predeclared structured filters rather than
pretending that a SQL/Parquet engine implements semantic text search.  Query
text remains ephemeral and digest-bound by ``RetrievalQueryBinding``; only its
case identity selects a frozen structured plan.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .dataset_provider import (
    CancellationSignal,
    DatasetSnapshot,
    QueryCancelled,
    QueryProvider,
    QueryRequest,
)
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
)


MAX_ANALYTICAL_PLANS = 200


@dataclass(frozen=True)
class AnalyticalQueryPlan:
    case_id: str
    filters: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", _identifier(self.case_id, "case ID"))
        if not isinstance(self.filters, Mapping) or len(self.filters) > 64:
            raise ValueError("analytical filters must be a bounded mapping")
        frozen = {}
        for key, value in self.filters.items():
            key = _identifier(key, "filter name")
            if value is not None and not isinstance(value, (bool, int, float, str)):
                raise ValueError("analytical filter values must be scalar")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("analytical filter values must be finite")
            frozen[key] = value
        object.__setattr__(self, "filters", MappingProxyType(frozen))


class AnalyticalSnapshotRetrievalAdapter:
    """Adapt a stable-index QueryProvider without exposing SQL or row content."""

    def __init__(
        self,
        *,
        snapshot: DatasetSnapshot,
        records: Sequence[Mapping[str, Any]],
        provider: QueryProvider,
        provider_revision: str,
        plans: Sequence[AnalyticalQueryPlan],
        lifecycle: RetrievalLifecycleMetrics | None = None,
    ) -> None:
        _, citations, _ = _freeze_snapshot_records(snapshot, records)
        binding = getattr(provider, "binding", None)
        if (getattr(binding, "snapshot_id", None) != snapshot.snapshot_id or
                getattr(binding, "dataset_id", None) != snapshot.dataset_id):
            raise ValueError("analytical provider is not bound to the exact snapshot")
        if not callable(getattr(provider, "query", None)):
            raise TypeError("provider must implement query")
        plans = tuple(plans)
        if not 0 < len(plans) <= MAX_ANALYTICAL_PLANS:
            raise ValueError("analytical plans must contain 1..200 cases")
        if len({item.case_id for item in plans}) != len(plans):
            raise ValueError("analytical plan case IDs must be unique")
        if not all(isinstance(item, AnalyticalQueryPlan) for item in plans):
            raise TypeError("plans must contain AnalyticalQueryPlan values")
        revision = _identifier(provider_revision, "provider revision")
        self.snapshot = snapshot
        self.profile = RetrievalProviderProfile(
            provider_id="analytical-structured", kind="analytical", revision=revision)
        self._provider = provider
        self._citations = citations
        self._plans = {item.case_id: item for item in plans}
        self._lifecycle = lifecycle or RetrievalLifecycleMetrics()

    async def retrieve(
        self,
        snapshot: DatasetSnapshot,
        binding: RetrievalQueryBinding,
        cancellation: CancellationSignal,
    ) -> tuple[RetrievalCitation, ...]:
        cancellation.checkpoint()
        if snapshot != self.snapshot:
            raise RetrievalProviderUnavailable("snapshot_unavailable")
        plan = self._plans.get(binding.case.case_id)
        if plan is None:
            raise RetrievalProviderUnavailable("query_plan_unavailable")
        request = QueryRequest(
            dataset_id=snapshot.dataset_id,
            snapshot_id=snapshot.snapshot_id,
            filters=dict(plan.filters),
            text="",
            limit=binding.case.k,
            include_data=False,
        )
        try:
            page = await asyncio.to_thread(
                self._provider.query, request, cancellation=cancellation)
        except QueryCancelled:
            raise
        except KeyError as exc:
            raise RetrievalProviderUnavailable("snapshot_unavailable") from exc
        except Exception as exc:
            raise RetrievalProviderFailure("provider_error") from exc
        cancellation.checkpoint()
        if page.snapshot_id != snapshot.snapshot_id:
            raise RetrievalProviderFailure("snapshot_mismatch")
        citations = []
        for match in page.matches:
            if not isinstance(match, Mapping):
                raise RetrievalProviderFailure("invalid_citation")
            index = match.get("record_index")
            if isinstance(index, bool) or not isinstance(index, int):
                raise RetrievalProviderFailure("invalid_citation")
            if index < 0 or index >= len(self._citations):
                raise RetrievalProviderFailure("invalid_citation")
            citations.append(self._citations[index])
        if len(set(citations)) != len(citations):
            raise RetrievalProviderFailure("duplicate_citation")
        return tuple(citations)

    async def lifecycle(
        self, snapshot: DatasetSnapshot, cancellation: CancellationSignal,
    ) -> RetrievalLifecycleMetrics:
        cancellation.checkpoint()
        if snapshot != self.snapshot:
            raise RetrievalProviderUnavailable("snapshot_unavailable")
        return self._lifecycle
