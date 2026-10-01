"""Bounded live execution harness for discovery/context benchmark fixtures."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import math
import time
from typing import Any, Awaitable, Callable, Sequence

from .context_provider import ContextCitation
from .fabric.dataset_provider import CancellationSignal, DatasetSnapshot
from .fabric.retrieval_comparison import RetrievalCitation, RetrievalCase
from .fabric.retrieval_execution import (
    RetrievalQueryBinding,
    SnapshotRetrievalAdapter,
)

from .discovery_benchmark import (
    BenchmarkAuthority,
    BenchmarkHit,
    ContextBenchmarkCase,
    ContextBenchmarkFixture,
    ContextBenchmarkObservation,
    ContextBenchmarkVariant,
)


LiveVariantCall = Callable[
    [ContextBenchmarkCase, "BenchmarkMilestones"],
    "LiveContextResult | Awaitable[LiveContextResult]",
]
ClaimSupport = Callable[
    [ContextBenchmarkCase, tuple[RetrievalCitation, ...]], Sequence[str]
]


@dataclass(frozen=True, slots=True)
class LiveContextResult:
    """Payload-free identities and accounting returned by one trusted variant."""

    discovered_sources: tuple[str, ...]
    selected_sources: tuple[str, ...]
    hits: tuple[BenchmarkHit, ...]
    supported_claim_ids: tuple[str, ...]
    policy_violations: tuple[str, ...] = ()
    byte_count: int = 0
    cost_units: int = 0
    cpu_ms: int = 0
    gpu_ms: int = 0


@dataclass(frozen=True, slots=True)
class LiveContextVariantRunner:
    variant: ContextBenchmarkVariant
    execute: LiveVariantCall
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.variant, ContextBenchmarkVariant):
            raise TypeError("live runner requires a benchmark variant")
        if not callable(self.execute):
            raise TypeError("live runner execute must be callable")
        if (isinstance(self.timeout_seconds, bool)
                or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 3_600):
            raise ValueError("live runner timeout must be between zero and one hour")
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


class BenchmarkMilestones:
    """Harness-owned monotonic scout and first-useful-context markers."""

    __slots__ = ("_start_ns", "_scout_ns", "_useful_ns", "_closed")

    def __init__(self, start_ns: int) -> None:
        self._start_ns = start_ns
        self._scout_ns: int | None = None
        self._useful_ns: int | None = None
        self._closed = False

    def scout_complete(self) -> None:
        self._mark("scout")

    def useful_context(self) -> None:
        if self._scout_ns is None:
            raise RuntimeError("useful context cannot precede scout completion")
        self._mark("useful")

    def close(self) -> None:
        self._closed = True

    def elapsed_ms(self, name: str) -> int | None:
        value = self._scout_ns if name == "scout" else self._useful_ns
        return None if value is None else max(0, (value - self._start_ns) // 1_000_000)

    def _mark(self, name: str) -> None:
        if self._closed:
            raise RuntimeError("benchmark milestones are closed")
        now = time.monotonic_ns()
        if name == "scout":
            if self._scout_ns is not None:
                raise RuntimeError("scout completion was already marked")
            self._scout_ns = now
        else:
            if self._useful_ns is not None:
                raise RuntimeError("useful context was already marked")
            self._useful_ns = now


class ContextBenchmarkRuntime:
    """Run an exact case × variant × repetition matrix sequentially.

    Sequential execution is intentional: model/GPU variants share external
    capacity, so this harness never manufactures parallel load or races one
    variant against another. Provider concurrency remains owned and bounded by
    the injected variant itself.
    """

    def __init__(self, *, snapshot: DatasetSnapshot,
                 cases: Sequence[ContextBenchmarkCase],
                 runners: Sequence[LiveContextVariantRunner],
                 repetitions: int) -> None:
        if not isinstance(snapshot, DatasetSnapshot):
            raise TypeError("live benchmark requires a DatasetSnapshot")
        self._snapshot = snapshot
        self._cases = tuple(cases)
        self._runners = tuple(runners)
        if (not self._cases or not all(isinstance(value, ContextBenchmarkCase)
                                       for value in self._cases)):
            raise ValueError("live benchmark cases are invalid or empty")
        if any(value.snapshot_id != snapshot.snapshot_id for value in self._cases):
            raise ValueError("live benchmark case crossed its dataset snapshot")
        if len({value.case_id for value in self._cases}) != len(self._cases):
            raise ValueError("live benchmark case identities must be unique")
        if len(self._runners) < 2 or not all(
                isinstance(value, LiveContextVariantRunner) for value in self._runners):
            raise ValueError("live benchmark requires at least two variant runners")
        variants = tuple(value.variant for value in self._runners)
        if len({value.variant_id for value in variants}) != len(variants):
            raise ValueError("live benchmark variant identities must be unique")
        if isinstance(repetitions, bool) or not isinstance(repetitions, int) \
                or not 1 <= repetitions <= 1_000:
            raise ValueError("live benchmark repetitions must be between 1 and 1000")
        self._variants = variants
        self._repetitions = repetitions

    async def run(self) -> ContextBenchmarkFixture:
        observations: list[ContextBenchmarkObservation] = []
        for runner in self._runners:
            for case in self._cases:
                for repetition in range(1, self._repetitions + 1):
                    observations.append(await self._run_one(
                        runner, case, repetition))
        return ContextBenchmarkFixture(
            self._snapshot, self._cases, self._variants,
            tuple(observations), self._repetitions)

    async def _run_one(self, runner: LiveContextVariantRunner,
                       case: ContextBenchmarkCase,
                       repetition: int) -> ContextBenchmarkObservation:
        started = time.monotonic_ns()
        milestones = BenchmarkMilestones(started)
        try:
            value = runner.execute(case, milestones)
            if inspect.isawaitable(value):
                value = await asyncio.wait_for(value, runner.timeout_seconds)
            if not isinstance(value, LiveContextResult):
                raise TypeError("live context variant returned an invalid result")
            ended = time.monotonic_ns()
            milestones.close()
            scout_ms = milestones.elapsed_ms("scout")
            if scout_ms is None:
                raise RuntimeError("live context variant omitted scout completion")
            useful_ms = milestones.elapsed_ms("useful")
            return ContextBenchmarkObservation(
                case_id=case.case_id, variant_id=runner.variant.variant_id,
                repetition=repetition, status="completed",
                discovered_sources=value.discovered_sources,
                selected_sources=value.selected_sources, hits=value.hits,
                supported_claim_ids=value.supported_claim_ids,
                policy_violations=value.policy_violations,
                scout_ms=scout_ms, first_useful_context_ms=useful_ms,
                end_to_end_ms=max(scout_ms, (ended - started) // 1_000_000),
                byte_count=value.byte_count, cost_units=value.cost_units,
                cpu_ms=value.cpu_ms, gpu_ms=value.gpu_ms)
        except asyncio.TimeoutError:
            milestones.close()
            return self._unfinished(case, runner.variant, repetition,
                                    "timed_out", "variant_timeout")
        except asyncio.CancelledError:
            milestones.close()
            raise
        except Exception:
            milestones.close()
            return self._unfinished(case, runner.variant, repetition,
                                    "failed", "variant_failed")

    @staticmethod
    def _unfinished(case: ContextBenchmarkCase, variant: ContextBenchmarkVariant,
                    repetition: int, status: str,
                    error_code: str) -> ContextBenchmarkObservation:
        return ContextBenchmarkObservation(
            case_id=case.case_id, variant_id=variant.variant_id,
            repetition=repetition, status=status, error_code=error_code)


def snapshot_retrieval_runner(
    *,
    snapshot: DatasetSnapshot,
    adapter: SnapshotRetrievalAdapter,
    variant: ContextBenchmarkVariant,
    source_id: str,
    timeout_seconds: float = 30.0,
    claim_support: ClaimSupport | None = None,
) -> LiveContextVariantRunner:
    """Bind an existing snapshot retriever to the live benchmark contract.

    Provider discovery is already complete when an adapter is injected, so the
    scout milestone records that exact provider selection.  Query text remains
    ephemeral inside ``RetrievalQueryBinding`` and never enters an observation.
    Claim support is deliberately caller-supplied: retrieving an authority is
    not, by itself, proof that an answer claim is supported.
    """
    if not isinstance(snapshot, DatasetSnapshot):
        raise TypeError("snapshot retrieval runner requires a DatasetSnapshot")
    profile = getattr(adapter, "profile", None)
    if (profile is None or not isinstance(getattr(profile, "provider_id", None), str)
            or not callable(getattr(adapter, "retrieve", None))):
        raise TypeError("snapshot retrieval runner requires a profiled adapter")
    source_id = str(source_id or "").strip()
    if (not source_id or len(source_id) > 256 or
            any(character.isspace() for character in source_id)):
        raise ValueError("snapshot retrieval runner requires a bounded source ID")
    if claim_support is not None and not callable(claim_support):
        raise TypeError("claim support resolver must be callable")

    async def execute(
        case: ContextBenchmarkCase,
        milestones: BenchmarkMilestones,
    ) -> LiveContextResult:
        if case.snapshot_id != snapshot.snapshot_id:
            raise ValueError("benchmark case crossed its retrieval snapshot")
        retrieval_case = RetrievalCase(
            case_key=case.case_key,
            query_digest=case.query_digest,
            relevant_citations=tuple(
                RetrievalCitation(value.identity, value.revision)
                for value in case.relevant_authorities
            ),
            k=case.request.max_context_items,
        )
        binding = RetrievalQueryBinding(retrieval_case, case.request.query)
        signal = CancellationSignal()
        milestones.scout_complete()
        try:
            citations = tuple(await adapter.retrieve(snapshot, binding, signal))
        finally:
            signal.cancel()
        if len(citations) > case.request.max_context_items:
            citations = citations[:case.request.max_context_items]
        if citations:
            milestones.useful_context()
        claims = tuple(claim_support(case, citations)) if claim_support else ()
        hits = tuple(BenchmarkHit(
            BenchmarkAuthority(value.record_id, value.revision_id),
            (ContextCitation(
                profile.provider_id,
                f"snapshot://{snapshot.snapshot_id}/{value.record_id}@{value.revision_id}",
            ),),
        ) for value in citations)
        return LiveContextResult(
            discovered_sources=(source_id,),
            selected_sources=(source_id,),
            hits=hits,
            supported_claim_ids=claims,
        )

    return LiveContextVariantRunner(variant, execute, timeout_seconds)
