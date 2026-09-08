import asyncio

import pytest

from vera.context_provider import ContextCitation, ContextItem
from vera.context_registry import ContextRegistry

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
        return [item(value.item_id, 1 if value.item_id == "rescued" else 0,
                     provider=value.provider) for value in items]
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
