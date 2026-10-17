import pytest

from vera.discovery_contract import DiscoveryRequest
from vera.discovery_native_web import (
    CapabilityDiscoveryCollector, CapabilityDiscoveryScout, NativeWebTargetStore,
)
from vera.discovery_orchestration import DiscoveryRoutePolicy, run_discovery_route
from vera.discovery_routing import DiscoveryWorkerOffer, WorkerProviderBinding


NOW = "2026-10-17T12:00:00Z"
AS_OF = 2_000_000


def request(**changes):
    values = dict(query="portable ONNX NER", requester="research.quick",
                  tenant_id="tenant.one", namespace="research.general",
                  as_of=NOW, source_kinds=("web",), max_sources=3,
                  max_context_items=3, timeout_ms=3_000,
                  max_bytes=10_000, max_cost_units=0)
    values.update(changes)
    return DiscoveryRequest(**values)


def worker():
    return DiscoveryWorkerOffer(
        worker_id="cpu-246", evidence_source="worker.registry",
        observed_at_ms=AS_OF - 10, valid_until_ms=AS_OF + 10,
        resources=("network",), methods=("capability",),
        bindings=(WorkerProviderBinding("web.fetch", "fetch-r1"),),
        concurrency_limit=2, in_flight=0)


def policy():
    return DiscoveryRoutePolicy(scout_timeout_ms=1_000,
                                collection_timeout_ms=2_000)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_native_web_adapters_run_existing_callables_through_portable_route():
    calls = []

    async def search(**kwargs):
        calls.append(("search", kwargs))
        return {"results": [
            {"title": "ONNX and NER", "url": "https://example.test/a?view=full",
             "snippet": "portable models", "score": .9},
            {"title": "duplicate", "url": "https://example.test/a?view=full"},
            {"title": "unsafe", "url": "file:///secret"},
        ]}

    async def fetch(**kwargs):
        calls.append(("fetch", kwargs))
        return {"text": "ONNX packages and distributed NER share portable evidence."}

    targets = NativeWebTargetStore()
    scout = CapabilityDiscoveryScout(search, targets, search_revision="search-r1",
                                       fetch_revision="fetch-r1", clock=lambda: NOW)
    collector = CapabilityDiscoveryCollector(fetch, targets, worker_id="cpu-246",
                                               provider_revision="fetch-r1",
                                               clock=lambda: NOW,
                                               monotonic=iter((1.0, 1.01)).__next__)
    report = await run_discovery_route(
        request(), (scout,), (collector,), (worker(),), (), policy=policy(),
        as_of_ms=AS_OF)

    assert calls == [
        ("search", {"query": "portable ONNX NER", "limit": 3}),
        ("fetch", {"url": "https://example.test/a?view=full"}),
    ]
    assert len(report.result.candidates) == 1
    assert report.result.candidates[0].locator.startswith("capability://web.fetch/")
    assert "view=full" not in report.result.candidates[0].locator
    assert report.result.receipts[0].status == "succeeded"
    assert report.result.context[0].item.provider == "web.fetch"
    assert report.result.context[0].item.citations[0].source_id == report.result.receipts[0].receipt_id


def test_adapter_construction_is_inert_and_revisions_are_explicit():
    calls = []
    targets = NativeWebTargetStore()
    scout = CapabilityDiscoveryScout(lambda **kw: calls.append(kw), targets,
                                       search_revision="search-r1", fetch_revision="fetch-r1")
    collector = CapabilityDiscoveryCollector(lambda **kw: calls.append(kw), targets,
                                               worker_id="cpu-246", provider_revision="fetch-r1")
    assert calls == []
    assert scout.revision == "search-r1"
    assert collector.provider_revision == "fetch-r1"


@pytest.mark.asyncio
async def test_missing_worker_offer_never_fetches():
    fetched = []

    async def search(**kwargs):
        return [{"url": "https://example.test/a", "title": "result"}]

    targets = NativeWebTargetStore()
    scout = CapabilityDiscoveryScout(search, targets, search_revision="search-r1",
                                       fetch_revision="fetch-r1", clock=lambda: NOW)
    collector = CapabilityDiscoveryCollector(lambda **kw: fetched.append(kw), targets,
                                               worker_id="cpu-246", provider_revision="fetch-r1")
    report = await run_discovery_route(
        request(), (scout,), (collector,), (), (), policy=policy(), as_of_ms=AS_OF)
    assert fetched == []
    assert report.result.receipts == ()
    assert report.failures[0].reason == "no_current_worker_offer"


@pytest.mark.asyncio
async def test_collection_bounds_utf8_and_reports_empty_without_output():
    async def search(**kwargs):
        return [{"url": "https://example.test/a", "title": "result"}]

    targets = NativeWebTargetStore()
    scout = CapabilityDiscoveryScout(search, targets, search_revision="search-r1",
                                       fetch_revision="fetch-r1", clock=lambda: NOW)
    collector = CapabilityDiscoveryCollector(lambda **kw: {"text": ""}, targets,
                                               worker_id="cpu-246", provider_revision="fetch-r1",
                                               clock=lambda: NOW,
                                               monotonic=iter((1.0, 1.0)).__next__)
    report = await run_discovery_route(
        request(), (scout,), (collector,), (worker(),), (), policy=policy(), as_of_ms=AS_OF)
    assert report.result.receipts[0].status == "failed"
    assert report.result.receipts[0].error_code == "empty_result"
    assert report.result.context == ()


@pytest.mark.asyncio
async def test_native_exceptions_are_not_retried_or_exposed():
    calls = 0

    async def search(**kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("secret provider body")

    targets = NativeWebTargetStore()
    scout = CapabilityDiscoveryScout(search, targets, search_revision="search-r1",
                                       fetch_revision="fetch-r1", clock=lambda: NOW)
    report = await run_discovery_route(
        request(), (scout,), (), (), (), policy=policy(), as_of_ms=AS_OF)
    assert calls == 1
    assert report.failures[0].reason == "RuntimeError"
    assert "secret provider body" not in repr(report.failures)
