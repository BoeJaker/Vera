"""Bounded two-stage discovery orchestration over injected providers.

The route first scouts candidate sources, then ranks one collection method per
source and executes only assignments admitted by the worker/gate planner.  It
does not discover providers, inspect resources, or infer capacity itself.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, Sequence

from vera.discovery_contract import (
    CollectionOption,
    CollectionReceipt,
    DiscoveredArtifact,
    DiscoveredContext,
    DiscoveredDataset,
    DiscoveryRequest,
    DiscoveryResult,
    SourceCandidate,
)
from vera.discovery_routing import (
    DiscoveryAssignment,
    DiscoveryExecutionPlan,
    DiscoveryWorkerOffer,
    GpuAdmissionReceipt,
    plan_discovery_execution,
)


MAX_PARTICIPANTS = 256
MAX_SCOUT_CANDIDATES = 4_096
_LOG = logging.getLogger(__name__)
MAX_FAILURES = 4_096
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


def _identifier(value: object, field: str) -> str:
    value = str(value or "").strip()
    if not _ID.fullmatch(value):
        raise ValueError(f"{field} must be a bounded identifier")
    return value


def _positive(value: object, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{field} must be between one and {maximum}")
    return value


class DiscoveryCancellation(Protocol):
    def checkpoint(self) -> None: ...


class _RouteFailure(Exception):
    def __init__(self, code: str):
        self.code = _identifier(code, "failure code")
        super().__init__(self.code)


class DiscoveryScout(Protocol):
    scout_id: str
    revision: str

    async def scout(self, request: DiscoveryRequest, *,
                    cancellation: DiscoveryCancellation | None = None
                    ) -> Sequence[SourceCandidate]: ...


class DiscoveryCollector(Protocol):
    worker_id: str
    provider: str
    provider_revision: str
    methods: tuple[str, ...]

    async def collect(self, request: DiscoveryRequest, candidate: SourceCandidate,
                      option: CollectionOption, assignment: DiscoveryAssignment, *,
                      cancellation: DiscoveryCancellation | None = None
                      ) -> "CollectedBatch": ...


@dataclass(frozen=True, slots=True)
class DiscoveryRoutePolicy:
    max_parallel_scouts: int = 8
    max_parallel_collections: int = 8
    scout_timeout_ms: int = 1_000
    collection_timeout_ms: int = 4_000
    relevance_weight: float = 0.45
    authority_weight: float = 0.25
    freshness_weight: float = 0.15
    speed_weight: float = 0.15
    minimum_score: float = 0.0

    def __post_init__(self) -> None:
        _positive(self.max_parallel_scouts, "max_parallel_scouts", 256)
        _positive(self.max_parallel_collections, "max_parallel_collections", 256)
        _positive(self.scout_timeout_ms, "scout_timeout_ms", 300_000)
        _positive(self.collection_timeout_ms, "collection_timeout_ms", 300_000)
        weights = (self.relevance_weight, self.authority_weight,
                   self.freshness_weight, self.speed_weight)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
               not math.isfinite(value) or value < 0 for value in weights):
            raise ValueError("ranking weights must be finite and non-negative")
        if not math.isclose(sum(weights), 1.0, rel_tol=0, abs_tol=1e-9):
            raise ValueError("ranking weights must sum to one")
        if (isinstance(self.minimum_score, bool) or
                not isinstance(self.minimum_score, (int, float)) or
                not math.isfinite(self.minimum_score) or
                not 0 <= self.minimum_score <= 1):
            raise ValueError("minimum_score must be between zero and one")


@dataclass(frozen=True, slots=True)
class RankedCollectionOption:
    candidate_id: str
    source_id: str
    option_id: str
    score: float
    rank: int

    def __post_init__(self) -> None:
        for field in ("candidate_id", "source_id", "option_id"):
            _identifier(getattr(self, field), field)
        if not 0 <= self.score <= 1 or self.rank < 1:
            raise ValueError("ranked option score or rank is invalid")


@dataclass(frozen=True, slots=True)
class DiscoveryRouteFailure:
    stage: str
    participant: str
    reason: str
    candidate_id: str = ""
    option_id: str = ""

    def __post_init__(self) -> None:
        if self.stage not in {"scout", "selection", "routing", "collection"}:
            raise ValueError("unsupported discovery failure stage")
        _identifier(self.participant, "participant")
        _identifier(self.reason, "reason")
        for field in ("candidate_id", "option_id"):
            value = getattr(self, field)
            if value:
                _identifier(value, field)


@dataclass(frozen=True, slots=True)
class CollectedBatch:
    receipt: CollectionReceipt
    context: tuple[DiscoveredContext, ...] = ()
    datasets: tuple[DiscoveredDataset, ...] = ()
    artifacts: tuple[DiscoveredArtifact, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, CollectionReceipt):
            raise ValueError("collected batch requires a receipt")
        for name, expected in (("context", DiscoveredContext),
                               ("datasets", DiscoveredDataset),
                               ("artifacts", DiscoveredArtifact)):
            try:
                values = tuple(getattr(self, name))
            except TypeError as exc:
                raise ValueError(f"{name} must be a sequence") from exc
            if not all(isinstance(value, expected) for value in values):
                raise ValueError(f"{name} contains invalid output")
            if any(value.receipt_id != self.receipt.receipt_id for value in values):
                raise ValueError("collected output belongs to another receipt")
            object.__setattr__(self, name, values)
        count = len(self.context) + len(self.datasets) + len(self.artifacts)
        if count != self.receipt.item_count:
            raise ValueError("receipt item_count does not match collected batch")


@dataclass(frozen=True, slots=True)
class DiscoveryRouteReport:
    result: DiscoveryResult
    execution_plan: DiscoveryExecutionPlan
    ranked_options: tuple[RankedCollectionOption, ...]
    failures: tuple[DiscoveryRouteFailure, ...]
    rejected_option_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.result, DiscoveryResult):
            raise ValueError("route report requires DiscoveryResult")
        if not isinstance(self.execution_plan, DiscoveryExecutionPlan):
            raise ValueError("route report requires DiscoveryExecutionPlan")
        if self.execution_plan.request_id != self.result.request.request_id:
            raise ValueError("route report identities do not match")
        try:
            ranked = tuple(self.ranked_options)
            failures = tuple(self.failures)
            rejected = tuple(self.rejected_option_ids)
        except TypeError as exc:
            raise ValueError("route report collections must be sequences") from exc
        if (not all(isinstance(value, RankedCollectionOption) for value in ranked) or
                not all(isinstance(value, DiscoveryRouteFailure) for value in failures)):
            raise ValueError("route report contains invalid evidence")
        if len(failures) > MAX_FAILURES:
            raise ValueError("route report exceeds failure budget")
        if (any(not isinstance(value, str) or not value.strip() for value in rejected) or
                len(rejected) != len(set(rejected))):
            raise ValueError("rejected option identities must be unique")
        object.__setattr__(self, "ranked_options", ranked)
        object.__setattr__(self, "failures", failures)
        object.__setattr__(self, "rejected_option_ids", rejected)


def rank_collection_options(
    request: DiscoveryRequest,
    candidates: Sequence[SourceCandidate],
    policy: DiscoveryRoutePolicy,
) -> tuple[RankedCollectionOption, ...]:
    """Rank the best method for each unique source without executing it."""
    if not isinstance(request, DiscoveryRequest) or not isinstance(policy, DiscoveryRoutePolicy):
        raise ValueError("request and policy are required")
    try:
        candidates = tuple(candidates)
    except TypeError as exc:
        raise ValueError("candidates must be a sequence") from exc
    if len(candidates) > MAX_SCOUT_CANDIDATES or not all(
            isinstance(value, SourceCandidate) for value in candidates):
        raise ValueError("candidates are invalid or exceed the limit")
    if any(value.request_id != request.request_id for value in candidates):
        raise ValueError("candidate belongs to another request")

    scored: list[tuple[float, SourceCandidate, CollectionOption]] = []
    for candidate in candidates:
        for option in candidate.options:
            speed = 1.0 - min(option.estimated_latency_ms / request.timeout_ms, 1.0)
            score = round(
                candidate.relevance_score * policy.relevance_weight +
                candidate.authority_score * policy.authority_weight +
                candidate.freshness_score * policy.freshness_weight +
                speed * policy.speed_weight,
                12,
            )
            if score >= policy.minimum_score:
                scored.append((score, candidate, option))
    scored.sort(key=lambda value: (
        -value[0], value[2].estimated_latency_ms,
        value[2].estimated_cost_units, value[2].max_bytes,
        value[1].candidate_id, value[2].option_id,
    ))

    unique: list[tuple[float, SourceCandidate, CollectionOption]] = []
    seen_sources: set[str] = set()
    for value in scored:
        if value[1].source_id not in seen_sources:
            seen_sources.add(value[1].source_id)
            unique.append(value)
    return tuple(RankedCollectionOption(
        candidate.candidate_id, candidate.source_id, option.option_id,
        score, index + 1,
    ) for index, (score, candidate, option) in enumerate(unique))


async def _bounded_call(factory: Callable[[], Awaitable[object]], semaphore: asyncio.Semaphore,
                        timeout_ms: int) -> object:
    async with semaphore:
        return await asyncio.wait_for(factory(), timeout=timeout_ms / 1_000)


async def _gather_stage(awaitables: Sequence[Awaitable[object]],
                        timeout_ms: int) -> tuple[object | BaseException, ...]:
    """Collect aligned outcomes while bounding the whole stage, including queues."""
    tasks = tuple(asyncio.create_task(value) for value in awaitables)
    if not tasks:
        return ()
    done, pending = await asyncio.wait(tasks, timeout=timeout_ms / 1_000)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    outcomes: list[object | BaseException] = []
    for task in tasks:
        if task in pending:
            outcomes.append(asyncio.TimeoutError())
            continue
        try:
            outcomes.append(task.result())
        except BaseException as exc:  # preserve cancellation as a control signal
            outcomes.append(exc)
    return tuple(outcomes)


def _failure(stage: str, participant: str, exc: BaseException, *,
             candidate_id: str = "", option_id: str = "") -> DiscoveryRouteFailure:
    reason = ("timed_out" if isinstance(exc, asyncio.TimeoutError) else
              exc.code if isinstance(exc, _RouteFailure) else type(exc).__name__)
    return DiscoveryRouteFailure(stage, participant, reason,
                                 candidate_id, option_id)


async def run_discovery_route(
    request: DiscoveryRequest,
    scouts: Sequence[DiscoveryScout],
    collectors: Sequence[DiscoveryCollector],
    workers: Sequence[DiscoveryWorkerOffer],
    gpu_admissions: Sequence[GpuAdmissionReceipt],
    *,
    policy: DiscoveryRoutePolicy = DiscoveryRoutePolicy(),
    as_of_ms: int,
    cancellation: DiscoveryCancellation | None = None,
) -> DiscoveryRouteReport:
    """Scout, rank, route and collect within one caller-owned deadline."""
    if not isinstance(request, DiscoveryRequest):
        raise ValueError("request must be DiscoveryRequest")
    if not isinstance(policy, DiscoveryRoutePolicy):
        raise ValueError("policy must be DiscoveryRoutePolicy")
    if policy.scout_timeout_ms + policy.collection_timeout_ms > request.timeout_ms:
        raise ValueError("stage timeouts exceed the request deadline")
    try:
        scouts = tuple(scouts)
        collectors = tuple(collectors)
    except TypeError as exc:
        raise ValueError("providers must be sequences") from exc
    if not scouts or len(scouts) > MAX_PARTICIPANTS:
        raise ValueError("one to 256 scouts are required")
    if len(collectors) > MAX_PARTICIPANTS:
        raise ValueError("collectors exceed the route limit")
    scout_ids = [_identifier(value.scout_id, "scout_id") for value in scouts]
    for value in scouts:
        _identifier(value.revision, "scout revision")
    if len(scout_ids) != len(set(scout_ids)):
        raise ValueError("scout identities must be unique")
    collector_keys = []
    collector_routes: list[tuple[str, str, str, str]] = []
    for value in collectors:
        methods = tuple(value.methods)
        if not methods:
            raise ValueError("collector methods are required")
        collector_keys.append((
            _identifier(value.worker_id, "worker_id"),
            _identifier(value.provider, "provider"),
            _identifier(value.provider_revision, "provider_revision"),
            tuple(sorted(_identifier(method, "method") for method in methods)),
        ))
        collector_routes.extend((collector_keys[-1][0], collector_keys[-1][1],
                                 collector_keys[-1][2], method)
                                for method in collector_keys[-1][3])
    if len(collector_keys) != len(set(collector_keys)):
        raise ValueError("collector identities must be unique")
    if len(collector_routes) != len(set(collector_routes)):
        raise ValueError("collector method routes must be unique")
    if cancellation is not None:
        cancellation.checkpoint()

    failures: list[DiscoveryRouteFailure] = []
    scout_semaphore = asyncio.Semaphore(policy.max_parallel_scouts)

    async def call_scout(value: DiscoveryScout) -> tuple[SourceCandidate, ...]:
        result = await _bounded_call(
            lambda: value.scout(request, cancellation=cancellation),
            scout_semaphore, policy.scout_timeout_ms)
        try:
            batch = tuple(result)
        except TypeError as exc:
            raise ValueError("scout returned a non-sequence") from exc
        if not all(isinstance(item, SourceCandidate) for item in batch):
            raise ValueError("scout returned invalid candidates")
        if len(batch) > MAX_SCOUT_CANDIDATES:
            raise ValueError("scout returned too many candidates")
        if any(item.request_id != request.request_id for item in batch):
            raise ValueError("scout returned a foreign request candidate")
        return batch

    scout_results = await _gather_stage(
        tuple(call_scout(value) for value in scouts), policy.scout_timeout_ms)
    candidates: list[SourceCandidate] = []
    for scout_id, result in zip(scout_ids, scout_results):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            failures.append(_failure("scout", scout_id, result))
        elif isinstance(result, BaseException):
            raise result
        else:
            candidates.extend(result)
    candidate_map = {value.candidate_id: value for value in candidates}
    candidates = list(candidate_map.values())
    if len(candidates) > MAX_SCOUT_CANDIDATES:
        raise ValueError("combined scout candidates exceed the route limit")
    if cancellation is not None:
        cancellation.checkpoint()

    ranked = rank_collection_options(request, candidates, policy)
    selected_ranked = ranked[:request.max_sources]
    candidate_index = {value.candidate_id: value for value in candidates}
    selected_candidates = tuple(candidate_index[value.candidate_id]
                                for value in selected_ranked)
    selected_options = tuple(value.option_id for value in selected_ranked)
    rejected = {value.option_id for candidate in candidates for value in candidate.options}
    rejected.difference_update(selected_options)
    for value in ranked[request.max_sources:]:
        failures.append(DiscoveryRouteFailure(
            "selection", value.source_id, "source_budget_exceeded",
            value.candidate_id, value.option_id))

    execution = plan_discovery_execution(
        request, selected_candidates, selected_options, workers,
        gpu_admissions, as_of_ms=as_of_ms)
    for refusal in execution.refusals:
        rejected.add(refusal.option_id)
        candidate = candidate_index[refusal.candidate_id]
        failures.append(DiscoveryRouteFailure(
            "routing", candidate.source_id, refusal.reason,
            refusal.candidate_id, refusal.option_id))

    option_index = {
        option.option_id: (candidate, option)
        for candidate in selected_candidates for option in candidate.options
    }
    collector_index: dict[tuple[str, str, str, str], DiscoveryCollector] = {}
    for collector, key in zip(collectors, collector_keys):
        for method in key[3]:
            collector_index[(key[0], key[1], key[2], method)] = collector
    collection_semaphore = asyncio.Semaphore(policy.max_parallel_collections)

    async def call_collector(assignment: DiscoveryAssignment) -> CollectedBatch:
        candidate, option = option_index[assignment.option_id]
        collector = collector_index.get((assignment.worker_id, option.provider,
                                         option.provider_revision, option.method))
        if collector is None:
            raise _RouteFailure("collector_unavailable")
        result = await _bounded_call(
            lambda: collector.collect(request, candidate, option, assignment,
                                      cancellation=cancellation),
            collection_semaphore, policy.collection_timeout_ms)
        if not isinstance(result, CollectedBatch):
            raise ValueError("collector returned an invalid batch")
        receipt = result.receipt
        if (receipt.request_id != request.request_id or
                receipt.candidate_id != candidate.candidate_id or
                receipt.option_id != option.option_id or
                receipt.provider_revision != option.provider_revision):
            raise ValueError("collector returned foreign receipt identity")
        if receipt.byte_count > option.max_bytes:
            raise ValueError("collector exceeded reserved bytes")
        if receipt.cost_units > option.estimated_cost_units:
            raise ValueError("collector exceeded reserved cost")
        return result

    collected = await _gather_stage(
        tuple(call_collector(value) for value in execution.assignments),
        policy.collection_timeout_ms)
    batches: list[CollectedBatch] = []
    for assignment, result in zip(execution.assignments, collected):
        candidate, _ = option_index[assignment.option_id]
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            failures.append(_failure(
                "collection", assignment.worker_id, result,
                candidate_id=candidate.candidate_id,
                option_id=assignment.option_id))
            rejected.add(assignment.option_id)
        elif isinstance(result, BaseException):
            raise result
        else:
            batches.append(result)
            if result.receipt.status not in {"partial", "succeeded"}:
                failures.append(DiscoveryRouteFailure(
                    "collection", assignment.worker_id,
                    result.receipt.error_code,
                    candidate.candidate_id, assignment.option_id))
                rejected.add(assignment.option_id)
    if cancellation is not None:
        cancellation.checkpoint()

    rank_by_option = {value.option_id: value.rank for value in selected_ranked}
    batches.sort(key=lambda value: (
        rank_by_option[value.receipt.option_id], value.receipt.option_id))
    accepted: list[CollectedBatch] = []
    context_count = 0
    for value in batches:
        if context_count + len(value.context) > request.max_context_items:
            candidate = candidate_index[value.receipt.candidate_id]
            failures.append(DiscoveryRouteFailure(
                "collection", candidate.source_id, "context_budget_exceeded",
                candidate.candidate_id, value.receipt.option_id))
            rejected.add(value.receipt.option_id)
            continue
        accepted.append(value)
        context_count += len(value.context)
    receipts = tuple(value.receipt for value in accepted)
    context = tuple(item for value in accepted for item in value.context)
    datasets = tuple(item for value in accepted for item in value.datasets)
    artifacts = tuple(item for value in accepted for item in value.artifacts)
    result = DiscoveryResult(
        request=request, candidates=selected_candidates, receipts=receipts,
        context=context, datasets=datasets, artifacts=artifacts)
    failures.sort(key=lambda value: (
        value.stage, value.participant, value.candidate_id,
        value.option_id, value.reason))
    report = DiscoveryRouteReport(
        result, execution, ranked, tuple(failures), tuple(sorted(rejected)))
    # Observability is best-effort and cannot alter an authoritative route.
    try:
        try:
            from vera.discovery_operator_readmodel import DISCOVERY_OPERATOR_LEDGER
        except ImportError:
            from Vera.vera.discovery_operator_readmodel import DISCOVERY_OPERATOR_LEDGER
        DISCOVERY_OPERATOR_LEDGER.record_route(report)
    except Exception:
        _LOG.debug("discovery operator projection failed", exc_info=True)
    return report
