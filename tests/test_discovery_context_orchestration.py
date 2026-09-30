import asyncio

import pytest

from vera.context_provider import ContextCitation, ContextItem
from vera.context_registry import ContextRegistry, DiscoveryContextSelectionPolicy
from vera.discovery_context_orchestration import run_discovery_context_route
from vera.discovery_contract import (
    CollectionOption, CollectionReceipt, DiscoveredContext, DiscoveryRequest,
    SourceCandidate,
)
from vera.discovery_orchestration import CollectedBatch, DiscoveryRoutePolicy
from vera.discovery_routing import DiscoveryWorkerOffer, WorkerProviderBinding


pytestmark = pytest.mark.critical
AS_OF = 1_800_000_000_000


def request():
    return DiscoveryRequest(
        query="exact portable query", requester="test.runner",
        tenant_id="tenant.one", namespace="context.route",
        as_of="2027-01-15T08:00:00Z", source_kinds=("api",),
        max_sources=2, max_context_items=4, timeout_ms=1_000,
        max_bytes=1_000)


def option(source_id="source.one"):
    return CollectionOption(
        source_id=source_id, method="api", provider="collector.one",
        provider_revision="collector-r1", resource="cpu",
        output_kinds=("context",), estimated_latency_ms=10,
        max_bytes=1_000)


class Scout:
    scout_id = "scout.one"
    revision = "scout-r1"

    async def scout(self, req, *, cancellation=None):
        chosen = option()
        return (SourceCandidate(
            request_id=req.request_id, source_id="source.one",
            locator="https://example.test/source", source_kind="api",
            provider=self.scout_id, revision="source-r1",
            observed_at=req.as_of, authority_score=.9, relevance_score=.8,
            freshness_score=.7, options=(chosen,)),)


class Collector:
    worker_id = "worker.one"
    provider = "collector.one"
    provider_revision = "collector-r1"
    methods = ("api",)

    async def collect(self, req, candidate, chosen, assignment, *, cancellation=None):
        receipt = CollectionReceipt(
            request_id=req.request_id, candidate_id=candidate.candidate_id,
            option_id=chosen.option_id, provider_revision=self.provider_revision,
            status="succeeded", started_at=req.as_of, completed_at=req.as_of,
            item_count=1, byte_count=20, duration_ms=10)
        item = ContextItem(
            "discovered", "discovered payload", candidate.source_id,
            candidate.revision, "memory", .8, 2,
            (ContextCitation(receipt.receipt_id, candidate.locator),))
        return CollectedBatch(receipt, (DiscoveredContext(receipt.receipt_id, item),))


class Provider:
    provider_id = "memory"

    def __init__(self):
        self.queries = []

    async def search(self, query, *, limit, cancellation=None):
        self.queries.append(query)
        return (ContextItem(
            "fresh", "fresh payload", "memory.source", "r2", self.provider_id,
            .95, 2, (ContextCitation("memory-r2", "fabric://fresh"),)),)


class Ranker:
    ranker_id = "stable"

    def rank(self, items):
        return items


def registry(provider=None):
    value = ContextRegistry()
    value.register_provider(provider or Provider())
    value.register_ranker(Ranker())
    return value


def worker():
    return DiscoveryWorkerOffer(
        worker_id="worker.one", evidence_source="worker.registry",
        observed_at_ms=AS_OF, valid_until_ms=AS_OF + 1_000,
        resources=("cpu",), methods=("api",),
        bindings=(WorkerProviderBinding("collector.one", "collector-r1"),),
        concurrency_limit=1, in_flight=0)


def policy():
    return DiscoveryRoutePolicy(scout_timeout_ms=100, collection_timeout_ms=100)


@pytest.mark.asyncio
async def test_route_binds_discovery_to_registry_and_composes_exact_request_query():
    provider = Provider()
    report = await run_discovery_context_route(
        request(), (Scout(),), (Collector(),), (worker(),), (),
        registry=registry(provider), ranker_ids=("stable",),
        limit_per_provider=2, budget_tokens=4, as_of_ms=AS_OF,
        route_policy=policy())
    assert report.selection.result_id == report.route.result.result_id
    assert report.selection.provider_ids == ("memory",)
    assert report.composition.providers == ("memory",)
    assert report.composition.rankers == ("stable",)
    assert report.composition.assembly.items[0].item_id == "fresh"
    assert provider.queries == ["exact portable query"]
    assert "exact portable query" not in repr(report.selection)


@pytest.mark.asyncio
async def test_configuration_fails_before_discovery_has_effects():
    scout = Scout()
    scout.calls = 0
    original = scout.scout

    async def counted(*args, **kwargs):
        scout.calls += 1
        return await original(*args, **kwargs)

    scout.scout = counted
    with pytest.raises(ValueError, match="unknown context ranker"):
        await run_discovery_context_route(
            request(), (scout,), (Collector(),), (worker(),), (),
            registry=registry(), ranker_ids=("missing",),
            limit_per_provider=1, budget_tokens=2, as_of_ms=AS_OF,
            route_policy=policy())
    assert scout.calls == 0


@pytest.mark.asyncio
async def test_unregistered_discovered_provider_fails_closed_after_route():
    value = ContextRegistry()
    value.register_provider(type("Other", (), {
        "provider_id": "other",
        "search": lambda self, query, **kwargs: asyncio.sleep(0, result=()),
    })())
    with pytest.raises(ValueError, match="unregistered discovered"):
        await run_discovery_context_route(
            request(), (Scout(),), (Collector(),), (worker(),), (),
            registry=value, limit_per_provider=1, budget_tokens=2,
            as_of_ms=AS_OF, route_policy=policy())


@pytest.mark.asyncio
async def test_allowlisted_exclusion_cannot_silently_produce_empty_selection():
    with pytest.raises(ValueError, match="selects no registered"):
        await run_discovery_context_route(
            request(), (Scout(),), (Collector(),), (worker(),), (),
            registry=registry(), limit_per_provider=1, budget_tokens=2,
            as_of_ms=AS_OF, route_policy=policy(),
            selection_policy=DiscoveryContextSelectionPolicy(
                allowed_provider_ids=("other",)))


@pytest.mark.asyncio
async def test_registry_drift_during_discovery_fails_before_context_dispatch():
    value = registry()

    class MutatingScout(Scout):
        async def scout(self, req, *, cancellation=None):
            value.register_provider(type("Later", (), {
                "provider_id": "later",
                "search": lambda self, query, **kwargs: asyncio.sleep(0, result=()),
            })())
            return await super().scout(req, cancellation=cancellation)

    with pytest.raises(ValueError, match="changed during discovery"):
        await run_discovery_context_route(
            request(), (MutatingScout(),), (Collector(),), (worker(),), (),
            registry=value, limit_per_provider=1, budget_tokens=2,
            as_of_ms=AS_OF, route_policy=policy())
