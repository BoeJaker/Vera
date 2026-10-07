import asyncio

import pytest

from vera.context_provider import ContextCitation, ContextItem
from vera.discovery_contract import (
    CollectionOption, CollectionReceipt, DiscoveredContext,
    DiscoveryRequest, SourceCandidate,
)
from vera.discovery_orchestration import (
    CollectedBatch, DiscoveryRoutePolicy, rank_collection_options,
    run_discovery_route,
)
from vera.discovery_operator_readmodel import DISCOVERY_OPERATOR_LEDGER
from vera.discovery_routing import DiscoveryWorkerOffer, WorkerProviderBinding


NOW = "2026-09-28T10:00:00Z"
AS_OF = 2_000_000


def request(**changes):
    values = dict(query="find useful evidence", requester="agent.runner",
                  tenant_id="tenant.one", namespace="research.general",
                  as_of=NOW, source_kinds=("api",), max_sources=3,
                  max_context_items=4, timeout_ms=200, max_bytes=10_000,
                  max_cost_units=10)
    values.update(changes)
    return DiscoveryRequest(**values)


def option(source, *, latency=20, cost=1, max_bytes=1_000):
    return CollectionOption(
        source_id=source, method="api", provider="web.adapter",
        provider_revision="rev-1", resource="cpu", output_kinds=("context",),
        estimated_latency_ms=latency, estimated_cost_units=cost,
        max_bytes=max_bytes, network_required=True)


def candidate(req, source, *, relevance=.8, authority=.7, freshness=.9,
              options=None):
    return SourceCandidate(
        request_id=req.request_id, source_id=source,
        locator=f"https://{source}.test/data", source_kind="api",
        provider="discovery.scout", revision="index-1", observed_at=NOW,
        authority_score=authority, relevance_score=relevance,
        freshness_score=freshness, options=options or (option(source),))


def worker(concurrency=4):
    return DiscoveryWorkerOffer(
        worker_id="cpu-1", evidence_source="worker.registry",
        observed_at_ms=AS_OF - 10, valid_until_ms=AS_OF + 10,
        resources=("cpu", "network"), methods=("api",),
        bindings=(WorkerProviderBinding("web.adapter", "rev-1"),),
        concurrency_limit=concurrency, in_flight=0)


class Scout:
    revision = "r1"

    def __init__(self, scout_id, values=(), *, delay=0, error=None):
        self.scout_id = scout_id
        self.values = values
        self.delay = delay
        self.error = error

    async def scout(self, req, *, cancellation=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.values


class Collector:
    worker_id = "cpu-1"
    provider = "web.adapter"
    provider_revision = "rev-1"
    methods = ("api",)

    def __init__(self, *, failures=(), delay=0, tracker=None, items=1):
        self.failures = set(failures)
        self.delay = delay
        self.tracker = tracker
        self.items = items
        self.calls = []

    async def collect(self, req, cand, opt, assignment, *, cancellation=None):
        self.calls.append(opt.option_id)
        if self.tracker is not None:
            self.tracker["current"] += 1
            self.tracker["maximum"] = max(self.tracker["maximum"],
                                           self.tracker["current"])
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if cand.source_id in self.failures:
                raise RuntimeError("untrusted provider body must not escape")
            receipt = CollectionReceipt(
                request_id=req.request_id, candidate_id=cand.candidate_id,
                option_id=opt.option_id, provider_revision=opt.provider_revision,
                status="succeeded", started_at=NOW, completed_at=NOW,
                item_count=self.items, byte_count=100, duration_ms=10,
                cost_units=1)
            outputs = tuple(DiscoveredContext(
                receipt.receipt_id,
                ContextItem(f"{cand.source_id}-{index}", "cited result",
                            cand.source_id, "rev-1", self.provider, .9, 2,
                            (ContextCitation(receipt.receipt_id,
                                             f"https://{cand.source_id}.test/data"),)))
                for index in range(self.items))
            return CollectedBatch(receipt, outputs)
        finally:
            if self.tracker is not None:
                self.tracker["current"] -= 1


class FailedReceiptCollector(Collector):
    async def collect(self, req, cand, opt, assignment, *, cancellation=None):
        receipt = CollectionReceipt(
            request_id=req.request_id, candidate_id=cand.candidate_id,
            option_id=opt.option_id, provider_revision=opt.provider_revision,
            status="failed", started_at=NOW, completed_at=NOW,
            error_code="source_unavailable")
        return CollectedBatch(receipt)


def policy(**changes):
    values = dict(max_parallel_scouts=2, max_parallel_collections=2,
                  scout_timeout_ms=40, collection_timeout_ms=80)
    values.update(changes)
    return DiscoveryRoutePolicy(**values)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_two_stage_route_isolates_failed_scout_and_returns_cited_context():
    req = request()
    best = candidate(req, "best", relevance=.95)
    lower = candidate(req, "lower", relevance=.6)
    collector = Collector()
    report = await run_discovery_route(
        req, (Scout("healthy", (lower, best)),
              Scout("failed", error=RuntimeError("raw provider response"))),
        (collector,), (worker(),), (), policy=policy(), as_of_ms=AS_OF)

    assert {value.source_id for value in report.result.candidates} == {"best", "lower"}
    assert report.ranked_options[0].source_id == "best"
    assert len(report.result.context) == 2
    assert all(value.item.citations[0].source_id == value.receipt_id
               for value in report.result.context)
    failure = next(value for value in report.failures if value.stage == "scout")
    assert failure.participant == "failed"
    assert failure.reason == "RuntimeError"
    assert "provider response" not in repr(report.failures)
    operator_view = DISCOVERY_OPERATOR_LEDGER.snapshot()["routes"][0]
    assert operator_view["request_id"] == req.request_id
    assert operator_view["counts"]["context"] == 2
    assert "find useful evidence" not in str(operator_view)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_slow_scout_times_out_without_blocking_healthy_source():
    req = request(timeout_ms=100)
    useful = candidate(req, "useful")
    report = await run_discovery_route(
        req, (Scout("slow", (useful,), delay=.05), Scout("fast", (useful,))),
        (Collector(),), (worker(),), (),
        policy=policy(scout_timeout_ms=10, collection_timeout_ms=80),
        as_of_ms=AS_OF)
    assert len(report.result.context) == 1
    assert any(value.stage == "scout" and value.participant == "slow" and
               value.reason == "timed_out" for value in report.failures)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_collection_failure_is_partial_and_does_not_discard_healthy_output():
    req = request()
    healthy = candidate(req, "healthy", relevance=.9)
    broken = candidate(req, "broken", relevance=.8)
    report = await run_discovery_route(
        req, (Scout("scout", (broken, healthy)),),
        (Collector(failures=("broken",)),), (worker(),), (),
        policy=policy(), as_of_ms=AS_OF)
    assert [value.item.source for value in report.result.context] == ["healthy"]
    assert any(value.stage == "collection" and value.reason == "RuntimeError"
               for value in report.failures)
    assert len(report.rejected_option_ids) == 1


@pytest.mark.asyncio
async def test_backpressure_limits_collection_concurrency():
    req = request()
    candidates = tuple(candidate(req, f"source-{index}", relevance=.9 - index / 10)
                       for index in range(3))
    tracker = {"current": 0, "maximum": 0}
    report = await run_discovery_route(
        req, (Scout("scout", candidates),),
        (Collector(delay=.01, tracker=tracker),), (worker(),), (),
        policy=policy(max_parallel_collections=1), as_of_ms=AS_OF)
    assert len(report.result.context) == 3
    assert tracker["maximum"] == 1


def test_ranking_selects_one_method_per_source_and_is_order_stable():
    req = request()
    fast = option("source", latency=10)
    slow = option("source", latency=100)
    first = candidate(req, "source", options=(slow, fast))
    second = candidate(req, "other", relevance=.7)
    left = rank_collection_options(req, (first, second), policy())
    right = rank_collection_options(req, (second, first), policy())
    assert left == right
    assert left[0].option_id == fast.option_id
    assert len({value.source_id for value in left}) == len(left)


@pytest.mark.asyncio
async def test_router_refusal_never_calls_collector_or_infers_capacity():
    req = request()
    value = candidate(req, "source")
    collector = Collector()
    report = await run_discovery_route(
        req, (Scout("scout", (value,)),), (collector,), (), (),
        policy=policy(), as_of_ms=AS_OF)
    assert not collector.calls
    assert not report.result.receipts
    assert report.failures[0].stage == "routing"
    assert report.failures[0].reason == "no_current_worker_offer"


@pytest.mark.asyncio
async def test_missing_collector_is_explicit_without_losing_route_identity():
    req = request()
    value = candidate(req, "source")
    report = await run_discovery_route(
        req, (Scout("scout", (value,)),), (), (worker(),), (),
        policy=policy(), as_of_ms=AS_OF)
    failure = next(value for value in report.failures
                   if value.stage == "collection")
    assert failure.reason == "collector_unavailable"
    assert failure.candidate_id == value.candidate_id
    assert failure.option_id == value.options[0].option_id


@pytest.mark.asyncio
async def test_unsuccessful_receipt_is_retained_and_classified():
    req = request()
    value = candidate(req, "source")
    report = await run_discovery_route(
        req, (Scout("scout", (value,)),), (FailedReceiptCollector(),),
        (worker(),), (), policy=policy(), as_of_ms=AS_OF)
    assert report.result.receipts[0].status == "failed"
    assert report.failures[0].reason == "source_unavailable"
    assert report.rejected_option_ids == (value.options[0].option_id,)


@pytest.mark.asyncio
async def test_context_budget_rejects_whole_batch_and_preserves_other_batch():
    req = request(max_context_items=2)
    first = candidate(req, "first", relevance=.9)
    second = candidate(req, "second", relevance=.8)
    report = await run_discovery_route(
        req, (Scout("scout", (first, second)),), (Collector(items=2),),
        (worker(),), (), policy=policy(), as_of_ms=AS_OF)
    assert len(report.result.context) == 2
    assert len(report.result.receipts) == 1
    assert any(value.reason == "context_budget_exceeded"
               for value in report.failures)


@pytest.mark.asyncio
async def test_cancellation_remains_a_control_signal():
    class Cancellation:
        def checkpoint(self):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_discovery_route(
            request(), (Scout("scout"),), (), (), (), policy=policy(),
            as_of_ms=AS_OF, cancellation=Cancellation())


def test_policy_refuses_unbounded_or_impossible_deadline_configuration():
    with pytest.raises(ValueError, match="sum to one"):
        DiscoveryRoutePolicy(relevance_weight=1)


def test_orchestrator_has_no_source_model_or_resource_probe_dependency():
    from pathlib import Path
    source = (Path(__file__).parents[1] / "vera" /
              "discovery_orchestration.py").read_text()
    for forbidden in ("requests", "httpx", "CAPABILITY_REGISTRY", "ollama",
                      "torch", "tensorflow", "subprocess", "docker"):
        assert forbidden not in source


@pytest.mark.asyncio
async def test_stage_timeouts_must_fit_caller_deadline():
    with pytest.raises(ValueError, match="exceed"):
        await run_discovery_route(
            request(timeout_ms=50), (Scout("scout"),), (), (), (),
            policy=policy(scout_timeout_ms=30, collection_timeout_ms=30),
            as_of_ms=AS_OF)
