"""Bounded execution for snapshot-pinned retrieval comparisons.

This module is deliberately separate from :mod:`retrieval_comparison`: the
comparison remains a pure, offline scorer while this layer owns provider
invocation, deadlines, cancellation, and error normalisation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import math
import re
import time
from typing import Any, Mapping, Protocol, Sequence

from .dataset_provider import (
    CancellationSignal,
    DatasetSnapshot,
    QueryCancelled,
    QueryProvider,
    QueryRequest,
)
from .retrieval_comparison import (
    RetrievalCase,
    RetrievalCitation,
    RetrievalComparisonFixture,
    RetrievalLifecycleMetrics,
    RetrievalObservation,
    RetrievalProviderEvidence,
    RetrievalProviderProfile,
    compare_retrieval,
)


MAX_EXECUTION_CASES = 200
MAX_EXECUTION_PROVIDERS = 16
MIN_TIMEOUT_SECONDS = 0.01
MAX_TIMEOUT_SECONDS = 300.0
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _query_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _error_code(value: str) -> str:
    value = str(value or "").strip()
    if not _ERROR_CODE.fullmatch(value):
        raise ValueError("error code must be a bounded machine identifier")
    return value


@dataclass(frozen=True)
class RetrievalQueryBinding:
    """Ephemeral query material bound to a public, digest-only case.

    Query text is intentionally absent from ``repr`` and every serialisation
    surface.  Callers should discard bindings after execution.
    """

    case: RetrievalCase
    _query_text: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.case, RetrievalCase):
            raise TypeError("case must be RetrievalCase")
        if not isinstance(self._query_text, str):
            raise TypeError("query text must be a string")
        if _query_digest(self._query_text) != self.case.query_digest:
            raise ValueError("query text does not match the case digest")

    @property
    def query_text(self) -> str:
        """Return ephemeral query material for adapter invocation only."""
        return self._query_text


class RetrievalProviderUnavailable(RuntimeError):
    """An adapter cannot serve the requested immutable snapshot."""

    def __init__(self, error_code: str = "provider_unavailable") -> None:
        self.error_code = _error_code(error_code)
        super().__init__(self.error_code)


class RetrievalProviderFailure(RuntimeError):
    """A provider failed in an expected, safely classifiable way."""

    def __init__(self, error_code: str = "provider_error") -> None:
        self.error_code = _error_code(error_code)
        super().__init__(self.error_code)


class SnapshotRetrievalAdapter(Protocol):
    """Provider adapter that accepts Vera's immutable snapshot boundary."""

    profile: RetrievalProviderProfile

    async def retrieve(
        self,
        snapshot: DatasetSnapshot,
        binding: RetrievalQueryBinding,
        cancellation: CancellationSignal,
    ) -> tuple[RetrievalCitation, ...]: ...

    async def lifecycle(
        self,
        snapshot: DatasetSnapshot,
        cancellation: CancellationSignal,
    ) -> RetrievalLifecycleMetrics: ...


@dataclass(frozen=True)
class RetrievalExecutionResult:
    fixture: RetrievalComparisonFixture
    report: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return evidence and scores without exposing ephemeral queries."""
        return {
            "snapshot": self.fixture.snapshot.to_dict(),
            "cases": [case.to_dict() for case in self.fixture.cases],
            "evidence": [item.identity_dict() for item in self.fixture.evidence],
            "report": dict(self.report),
        }


class UnavailableRetrievalAdapter:
    """Explicit evidence for a configured integration that is not available."""

    def __init__(self, profile: RetrievalProviderProfile, *,
                 error_code: str = "provider_unavailable") -> None:
        self.profile = profile
        self._error_code = _error_code(error_code)

    async def retrieve(self, snapshot: DatasetSnapshot,
                       binding: RetrievalQueryBinding,
                       cancellation: CancellationSignal
                       ) -> tuple[RetrievalCitation, ...]:
        cancellation.checkpoint()
        raise RetrievalProviderUnavailable(self._error_code)

    async def lifecycle(self, snapshot: DatasetSnapshot,
                        cancellation: CancellationSignal
                        ) -> RetrievalLifecycleMetrics:
        cancellation.checkpoint()
        return RetrievalLifecycleMetrics()


class QueryProviderRetrievalAdapter:
    """Adapt a snapshot-aware ``QueryProvider`` to comparison evidence.

    ``QueryPage`` deliberately uses provider-local record indexes.  The caller
    therefore supplies the immutable index-to-revision map from the same
    snapshot instead of this adapter guessing revision identity from live data.
    """

    def __init__(
        self,
        *,
        profile: RetrievalProviderProfile,
        provider: QueryProvider,
        citations_by_record_index: Sequence[RetrievalCitation],
        lifecycle: RetrievalLifecycleMetrics | None = None,
    ) -> None:
        if not isinstance(profile, RetrievalProviderProfile):
            raise TypeError("profile must be RetrievalProviderProfile")
        if not callable(getattr(provider, "query", None)):
            raise TypeError("provider must implement query")
        citations = tuple(citations_by_record_index)
        if not citations or not all(isinstance(item, RetrievalCitation)
                                    for item in citations):
            raise ValueError("citation map must contain RetrievalCitation values")
        self.profile = profile
        self._provider = provider
        self._citations = citations
        self._lifecycle = lifecycle or RetrievalLifecycleMetrics()

    async def retrieve(self, snapshot: DatasetSnapshot,
                       binding: RetrievalQueryBinding,
                       cancellation: CancellationSignal
                       ) -> tuple[RetrievalCitation, ...]:
        cancellation.checkpoint()
        if len(self._citations) != snapshot.record_count:
            raise RetrievalProviderFailure("citation_map_mismatch")
        request = QueryRequest(
            dataset_id=snapshot.dataset_id,
            snapshot_id=snapshot.snapshot_id,
            text=binding.query_text,
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
        cancellation.checkpoint()
        if page.snapshot_id != snapshot.snapshot_id:
            raise RetrievalProviderFailure("snapshot_mismatch")
        citations: list[RetrievalCitation] = []
        seen: set[RetrievalCitation] = set()
        for match in page.matches:
            if not isinstance(match, Mapping):
                raise RetrievalProviderFailure("invalid_citation")
            index = match.get("record_index")
            if isinstance(index, bool) or not isinstance(index, int):
                raise RetrievalProviderFailure("invalid_citation")
            if index < 0 or index >= len(self._citations):
                raise RetrievalProviderFailure("invalid_citation")
            citation = self._citations[index]
            if citation not in seen:
                citations.append(citation)
                seen.add(citation)
        return tuple(citations)

    async def lifecycle(self, snapshot: DatasetSnapshot,
                        cancellation: CancellationSignal
                        ) -> RetrievalLifecycleMetrics:
        cancellation.checkpoint()
        return self._lifecycle


def _validate_execution(
    snapshot: DatasetSnapshot,
    bindings: Sequence[RetrievalQueryBinding],
    adapters: Sequence[SnapshotRetrievalAdapter],
    timeout_seconds: float,
) -> tuple[tuple[RetrievalQueryBinding, ...], tuple[SnapshotRetrievalAdapter, ...]]:
    if not isinstance(snapshot, DatasetSnapshot):
        raise TypeError("snapshot must be DatasetSnapshot")
    bound = tuple(bindings)
    if not 0 < len(bound) <= MAX_EXECUTION_CASES:
        raise ValueError(f"bindings must contain 1..{MAX_EXECUTION_CASES} cases")
    if not all(isinstance(item, RetrievalQueryBinding) for item in bound):
        raise TypeError("bindings must contain RetrievalQueryBinding values")
    if len({item.case.case_id for item in bound}) != len(bound):
        raise ValueError("binding case IDs must be unique")
    providers = tuple(adapters)
    if not 2 <= len(providers) <= MAX_EXECUTION_PROVIDERS:
        raise ValueError(f"adapters must contain 2..{MAX_EXECUTION_PROVIDERS} providers")
    profiles = [getattr(item, "profile", None) for item in providers]
    if not all(isinstance(item, RetrievalProviderProfile) for item in profiles):
        raise TypeError("every adapter must expose a RetrievalProviderProfile")
    if len({item.provider_id for item in profiles}) != len(profiles):
        raise ValueError("adapter provider IDs must be unique")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise TypeError("timeout_seconds must be numeric")
    timeout_seconds = float(timeout_seconds)
    if (not math.isfinite(timeout_seconds) or
            not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS):
        raise ValueError("timeout_seconds is outside the bounded execution range")
    return bound, providers


async def _invoke_case(
    adapter: SnapshotRetrievalAdapter,
    snapshot: DatasetSnapshot,
    binding: RetrievalQueryBinding,
    timeout_seconds: float,
    caller_signal: CancellationSignal,
) -> RetrievalObservation:
    if caller_signal.cancelled:
        return RetrievalObservation(binding.case.case_id, "cancelled")
    signal = CancellationSignal()
    started = time.monotonic()
    retrieval_task = asyncio.create_task(
        adapter.retrieve(snapshot, binding, signal))

    async def wait_for_caller_cancellation() -> None:
        while not caller_signal.cancelled:
            await asyncio.sleep(0.01)

    cancellation_task = asyncio.create_task(wait_for_caller_cancellation())
    try:
        done, _ = await asyncio.wait(
            {retrieval_task, cancellation_task},
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_task in done:
            signal.cancel()
            retrieval_task.cancel()
            await asyncio.gather(retrieval_task, return_exceptions=True)
            return RetrievalObservation(binding.case.case_id, "cancelled")
        if retrieval_task not in done:
            signal.cancel()
            retrieval_task.cancel()
            await asyncio.gather(retrieval_task, return_exceptions=True)
            return RetrievalObservation(
                binding.case.case_id, "failed", error_code="timeout")
        citations = retrieval_task.result()
        citations = tuple(citations)
        if not all(isinstance(item, RetrievalCitation) for item in citations):
            raise RetrievalProviderFailure("invalid_citation")
        latency_ms = max(0, int(round((time.monotonic() - started) * 1000)))
        return RetrievalObservation(
            binding.case.case_id, "completed", citations[:binding.case.k],
            latency_ms=latency_ms)
    except asyncio.CancelledError:
        signal.cancel()
        retrieval_task.cancel()
        raise
    except QueryCancelled:
        return RetrievalObservation(binding.case.case_id, "cancelled")
    except RetrievalProviderUnavailable as exc:
        return RetrievalObservation(
            binding.case.case_id, "failed", error_code=exc.error_code)
    except RetrievalProviderFailure as exc:
        return RetrievalObservation(
            binding.case.case_id, "failed", error_code=exc.error_code)
    except Exception:
        # Provider exception text can contain queries, credentials, or payloads.
        return RetrievalObservation(
            binding.case.case_id, "failed", error_code="provider_error")
    finally:
        cancellation_task.cancel()
        await asyncio.gather(cancellation_task, return_exceptions=True)


async def execute_retrieval_comparison(
    *,
    snapshot: DatasetSnapshot,
    bindings: Sequence[RetrievalQueryBinding],
    adapters: Sequence[SnapshotRetrievalAdapter],
    timeout_seconds: float = 30.0,
    cancellation: CancellationSignal | None = None,
) -> RetrievalExecutionResult:
    """Invoke each adapter over identical cases, then score immutable evidence.

    Execution is sequential by design: it places a strict upper bound on
    provider pressure and avoids benchmark results being distorted by adapters
    competing for the same model, database, or accelerator.
    """
    bound, providers = _validate_execution(
        snapshot, bindings, adapters, timeout_seconds)
    timeout_seconds = float(timeout_seconds)
    caller_signal = cancellation or CancellationSignal()
    evidence: list[RetrievalProviderEvidence] = []
    ordered_bindings = tuple(sorted(bound, key=lambda item: item.case.case_id))
    for adapter in providers:
        observations = tuple([
            await _invoke_case(
                adapter, snapshot, binding, timeout_seconds, caller_signal)
            for binding in ordered_bindings
        ])
        try:
            lifecycle = await asyncio.wait_for(
                adapter.lifecycle(snapshot, caller_signal), timeout=timeout_seconds)
            if not isinstance(lifecycle, RetrievalLifecycleMetrics):
                lifecycle = RetrievalLifecycleMetrics()
        except (asyncio.TimeoutError, QueryCancelled, Exception):
            # Lifecycle availability is independent from retrieval quality.
            lifecycle = RetrievalLifecycleMetrics()
        evidence.append(RetrievalProviderEvidence(
            snapshot.snapshot_id, adapter.profile, observations, lifecycle))
    fixture = RetrievalComparisonFixture(
        snapshot=snapshot,
        cases=tuple(item.case for item in ordered_bindings),
        evidence=tuple(evidence),
    )
    return RetrievalExecutionResult(fixture, compare_retrieval(fixture))
