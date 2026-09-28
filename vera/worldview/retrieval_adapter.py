"""Adapter from provenance-qualified JEPA queries to retrieval evidence."""

from __future__ import annotations

from typing import Awaitable, Callable, Mapping, Any

from ..fabric.dataset_provider import CancellationSignal, DatasetSnapshot
from ..fabric.retrieval_comparison import (
    RetrievalCitation,
    RetrievalLifecycleMetrics,
    RetrievalProviderProfile,
)
from ..fabric.retrieval_execution import (
    RetrievalProviderFailure,
    RetrievalProviderUnavailable,
    RetrievalQueryBinding,
)
from .retrieval_provenance import JepaRetrievalProvenance


JepaQueryRunner = Callable[[str, int, str], Awaitable[Mapping[str, Any]]]


class JepaWorldviewRetrievalAdapter:
    """Require a live JEPA response to reproduce its immutable binding receipt."""

    def __init__(self, *, binding: JepaRetrievalProvenance,
                 query: JepaQueryRunner) -> None:
        if not isinstance(binding, JepaRetrievalProvenance):
            raise TypeError("binding must be JepaRetrievalProvenance")
        if not callable(query):
            raise TypeError("query must be callable")
        self.binding = binding
        self._query = query
        self.profile = RetrievalProviderProfile(
            provider_id="jepa-worldview",
            kind="jepa_worldview_evidence",
            revision=binding.provider_revision,
        )

    async def retrieve(
        self,
        snapshot: DatasetSnapshot,
        binding: RetrievalQueryBinding,
        cancellation: CancellationSignal,
    ) -> tuple[RetrievalCitation, ...]:
        cancellation.checkpoint()
        if snapshot.snapshot_id != self.binding.snapshot.snapshot_id:
            raise RetrievalProviderUnavailable("snapshot_unavailable")
        result = await self._query(
            binding.query_text, binding.case.k, snapshot.snapshot_id)
        cancellation.checkpoint()
        if not isinstance(result, Mapping):
            raise RetrievalProviderFailure("jepa_query_failed")
        if not result.get("ok") or not result.get("eligible"):
            code = str(result.get("error_code") or "")
            if code in {"provenance_unavailable", "snapshot_mismatch",
                        "worldview_not_ready"}:
                raise RetrievalProviderUnavailable(code)
            raise RetrievalProviderFailure("jepa_query_failed")
        if result.get("query_digest") != binding.case.query_digest:
            raise RetrievalProviderFailure("query_digest_mismatch")
        receipt = result.get("provenance")
        expected = self.binding.receipt()
        if not isinstance(receipt, Mapping) or any(
                receipt.get(key) != expected[key] for key in (
                    "schema", "snapshot_id", "model_package_id",
                    "provider_revision", "record_count", "checkpoint_sha256")):
            raise RetrievalProviderFailure("provenance_mismatch")
        raw_citations = result.get("citations")
        if not isinstance(raw_citations, (list, tuple)) or len(raw_citations) > binding.case.k:
            raise RetrievalProviderFailure("invalid_citation")
        try:
            citations = tuple(RetrievalCitation(**item) for item in raw_citations)
        except (TypeError, ValueError):
            raise RetrievalProviderFailure("invalid_citation") from None
        allowed = set(self.binding.citations)
        if len(set(citations)) != len(citations) or not set(citations).issubset(allowed):
            raise RetrievalProviderFailure("invalid_citation")
        return citations

    async def lifecycle(self, snapshot: DatasetSnapshot,
                        cancellation: CancellationSignal
                        ) -> RetrievalLifecycleMetrics:
        cancellation.checkpoint()
        return RetrievalLifecycleMetrics()
