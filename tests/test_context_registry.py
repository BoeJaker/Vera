import asyncio
from dataclasses import replace

import pytest

from vera.context_provider import (
    ContextCitation, ContextItem, ContextRankingEvidence,
)
from vera.context_registry import ContextRegistry
from vera.context_registry import (
    DiscoveredContextAuthority, DiscoveryContextSelection,
    DiscoveryContextSelectionPolicy,
)
from vera.discovery_contract import (
    CollectionOption, CollectionReceipt, DiscoveredContext, DiscoveryRequest,
    DiscoveryResult, SourceCandidate,
)

pytestmark = pytest.mark.critical


def item(name, score, *, provider="memory", tokens=2):
    return ContextItem(name, f"text {name}", name, "revision-1", provider,
                       score, tokens,
                       (ContextCitation(name, f"fabric://{name}"),))


class Provider:
    def __init__(self, provider_id, result=(), error=None):
        self.provider_id = provider_id
        self.result = result
        self.error = error

    async def search(self, query, *, limit, cancellation=None):
        if self.error:
            raise self.error
        return self.result[:limit]


class Ranker:
    def __init__(self, ranker_id, function):
        self.ranker_id = ranker_id
        self.function = function

    def rank(self, items):
        return self.function(items)


def registry(*providers, rankers=()):
    value = ContextRegistry()
    for provider in providers:
        value.register_provider(provider)
    for ranker in rankers:
        value.register_ranker(ranker)
    return value


def discovery_result(provider="memory"):
    request = DiscoveryRequest(
        query="portable selection", requester="test.runner",
        tenant_id="tenant.one", namespace="context.registry",
        as_of="2026-09-29T10:00:00Z", source_kinds=("api",),
        max_sources=2, max_context_items=4, timeout_ms=1_000,
        max_bytes=1_000)
    option = CollectionOption(
        source_id="source.one", method="api", provider="collector.one",
        provider_revision="collector-r1", resource="cpu",
        output_kinds=("context",), estimated_latency_ms=10,
        max_bytes=1_000)
    candidate = SourceCandidate(
        request_id=request.request_id, source_id="source.one",
        locator="https://example.test/source", source_kind="api",
        provider="scout.one", revision="source-r1",
        observed_at="2026-09-29T10:00:00Z", authority_score=.9,
        relevance_score=.8, freshness_score=.7, options=(option,))
    receipt = CollectionReceipt(
        request_id=request.request_id, candidate_id=candidate.candidate_id,
        option_id=option.option_id, provider_revision=option.provider_revision,
        status="succeeded", started_at="2026-09-29T10:00:00Z",
        completed_at="2026-09-29T10:00:00Z", item_count=1,
        byte_count=20, duration_ms=10)
    discovered = DiscoveredContext(
        receipt.receipt_id,
        ContextItem("item.one", "private payload", "source.one", "source-r1",
                    provider, .8, 2,
                    (ContextCitation(receipt.receipt_id,
                                     "https://example.test/source"),)))
    return DiscoveryResult(request=request, candidates=(candidate,),
                           receipts=(receipt,), context=(discovered,))


def test_manifest_is_payload_free_and_independent_of_registration_order():
    value = registry(Provider("z", [item("secret", .5, provider="z")]),
                     Provider("a"), rankers=(Ranker("middle", tuple),))
    assert [(entry.role, entry.component_id) for entry in value.manifest()] == [
        ("provider", "a"), ("provider", "z"), ("ranker", "middle")]
    assert "secret" not in repr(value.manifest())


def test_duplicate_and_noncanonical_ids_fail_without_replacement():
    value = registry(Provider("memory"))
    with pytest.raises(ValueError, match="already registered"):
        value.register_provider(Provider("memory"))
    with pytest.raises(ValueError, match="canonical"):
        value.register_ranker(Ranker(" padded ", tuple))


def test_discovery_selection_reuses_portable_authorities_and_is_payload_free():
    value = registry(Provider("memory"),
                     rankers=(Ranker("stable", tuple),))
    result = discovery_result()
    selection = value.select_discovery_result(
        result, ranker_ids=("stable",))
    assert selection.provider_ids == ("memory",)
    assert selection.ranker_ids == ("stable",)
    assert selection.result_id == result.result_id
    assert selection.authorities[0].item_id == "item.one"
    assert selection.registry_manifest_id == value.manifest_id()
    assert "private payload" not in repr(selection)


@pytest.mark.asyncio
async def test_discovery_selection_composes_only_against_exact_registry_snapshot():
    value = registry(Provider("memory", [item("fresh", .9)]))
    selection = value.select_discovery_result(discovery_result())
    composed = await value.compose_selection(
        selection, "query", limit_per_provider=1, budget_tokens=2)
    assert composed.providers == ("memory",)
    value.register_provider(Provider("later"))
    with pytest.raises(ValueError, match="registry changed"):
        await value.compose_selection(
            selection, "query", limit_per_provider=1, budget_tokens=2)


def test_discovery_selection_policy_is_explicit_and_fail_closed():
    result = discovery_result("external")
    value = registry(Provider("memory"))
    with pytest.raises(ValueError, match="unregistered discovered"):
        value.select_discovery_result(result)
    with pytest.raises(ValueError, match="selects no registered"):
        value.select_discovery_result(
            result, policy=DiscoveryContextSelectionPolicy(
                require_all_registered=False))
    with pytest.raises(ValueError, match="selects no registered"):
        registry(Provider("external")).select_discovery_result(
            result, policy=DiscoveryContextSelectionPolicy(
                allowed_provider_ids=("memory",)))


def test_discovery_selection_rejects_unknown_ranker_and_provider_overflow():
    result = discovery_result()
    value = registry(Provider("memory"))
    with pytest.raises(ValueError, match="unknown context ranker"):
        value.select_discovery_result(result, ranker_ids=("missing",))
    with pytest.raises(ValueError, match="between one and 256"):
        DiscoveryContextSelectionPolicy(maximum_providers=0)


def test_forged_selection_cannot_cross_provider_or_policy_boundaries():
    result = discovery_result()
    value = registry(Provider("memory"))
    selected = value.select_discovery_result(result)
    with pytest.raises(ValueError, match="unselected provider"):
        DiscoveryContextSelection(
            selected.request_id, selected.result_id,
            selected.registry_manifest_id, selected.policy,
            selected.provider_ids, selected.ranker_ids,
            (DiscoveredContextAuthority("other", "item", "source", "r1"),))
    with pytest.raises(ValueError, match="allowlist"):
        DiscoveryContextSelection(
            selected.request_id, selected.result_id,
            selected.registry_manifest_id,
            DiscoveryContextSelectionPolicy(
                allowed_provider_ids=("other",)),
            selected.provider_ids, selected.ranker_ids,
            selected.authorities)


@pytest.mark.asyncio
async def test_selection_is_explicit_ordered_and_rejects_unknown_or_duplicate_ids():
    value = registry(Provider("a"), Provider("b"))
    result = await value.compose("query", provider_ids=("b", "a"),
                                 limit_per_provider=1, budget_tokens=1)
    assert result.providers == ("b", "a")
    with pytest.raises(ValueError, match="unknown context provider: missing"):
        await value.compose("query", provider_ids=("missing",),
                            limit_per_provider=1, budget_tokens=1)
    with pytest.raises(ValueError, match="must be unique"):
        await value.compose("query", provider_ids=("a", "a"),
                            limit_per_provider=1, budget_tokens=1)
    with pytest.raises(ValueError, match="selection is required"):
        await value.compose("query", provider_ids=(),
                            limit_per_provider=1, budget_tokens=1)


@pytest.mark.asyncio
async def test_ranking_happens_before_the_only_budget_selection():
    values = [item("initial-winner", .9), item("rescued", .4)]
    def rescue(items):
        return [replace(
            value, score=1 if value.item_id == "rescued" else 0,
            ranking_evidence=value.ranking_evidence + (
                ContextRankingEvidence("rescue", "r1", 1, 1),))
                for value in items]
    value = registry(Provider("memory", values),
                     rankers=(Ranker("rescue", rescue),))
    result = await value.compose(
        "query", provider_ids=("memory",), ranker_ids=("rescue",),
        limit_per_provider=5, budget_tokens=2)
    assert [entry.item_id for entry in result.assembly.items] == ["rescued"]
    assert result.rankers == ("rescue",)


@pytest.mark.asyncio
async def test_provider_and_ranker_failures_are_separate_and_last_valid_items_survive():
    value = registry(
        Provider("broken", error=RuntimeError("secret provider detail")),
        Provider("memory", [item("kept", .8)]),
        rankers=(Ranker("bad", lambda _: (_ for _ in ()).throw(
            LookupError("secret ranker detail"))),
                 Ranker("good", lambda items: items)))
    result = await value.compose(
        "query", provider_ids=("broken", "memory"),
        ranker_ids=("bad", "good"), limit_per_provider=2, budget_tokens=2)
    assert [entry.item_id for entry in result.assembly.items] == ["kept"]
    assert [(failure.provider, failure.reason)
            for failure in result.provider_failures] == [("broken", "RuntimeError")]
    assert [(failure.ranker, failure.reason)
            for failure in result.ranker_failures] == [("bad", "LookupError")]
    assert result.rankers == ("good",)


@pytest.mark.asyncio
async def test_ranker_cannot_add_drop_or_rewrite_authoritative_context():
    original = item("kept", .8)
    rewritten = item("kept", 1, tokens=3)
    value = registry(
        Provider("memory", [original]),
        rankers=(Ranker("rewrite", lambda _: [rewritten]),))
    result = await value.compose(
        "query", provider_ids=("memory",), ranker_ids=("rewrite",),
        limit_per_provider=2, budget_tokens=2)
    assert result.assembly.items == (original,)
    assert result.rankers == ()
    assert result.ranker_failures[0].reason == "ValueError"


@pytest.mark.asyncio
async def test_ranker_cannot_strip_evidence_or_change_score_without_evidence():
    ranked = replace(
        item("kept", .8),
        ranking_evidence=(ContextRankingEvidence("first", "r1", .8, .2),))
    strip = Ranker("strip", lambda values: [replace(
        value, ranking_evidence=()) for value in values])
    unsupported = Ranker("unsupported", lambda values: [replace(
        value, score=1) for value in values])
    value = registry(Provider("memory", [ranked]), rankers=(strip, unsupported))
    result = await value.compose(
        "query", provider_ids=("memory",),
        ranker_ids=("strip", "unsupported"), limit_per_provider=2,
        budget_tokens=2)
    assert result.assembly.items == (ranked,)
    assert result.rankers == ()
    assert [failure.reason for failure in result.ranker_failures] == [
        "ValueError", "ValueError"]


@pytest.mark.asyncio
async def test_invalid_budget_fails_before_provider_dispatch():
    class CountingProvider(Provider):
        calls = 0
        async def search(self, query, *, limit, cancellation=None):
            self.calls += 1
            return ()

    provider = CountingProvider("memory")
    value = registry(provider)
    with pytest.raises(ValueError, match="non-negative"):
        await value.compose("query", provider_ids=("memory",),
                            limit_per_provider=1, budget_tokens=-1)
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_cancellation_propagates_from_ranker_boundary():
    class Cancel:
        calls = 0
        def checkpoint(self):
            self.calls += 1
            if self.calls >= 3:
                raise asyncio.CancelledError

    value = registry(Provider("memory", [item("kept", .8)]),
                     rankers=(Ranker("rank", tuple),))
    with pytest.raises(asyncio.CancelledError):
        await value.compose(
            "query", provider_ids=("memory",), ranker_ids=("rank",),
            limit_per_provider=2, budget_tokens=2, cancellation=Cancel())
