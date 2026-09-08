import pytest
from vera.context_provider import (
    ContextCitation, ContextItem, assemble_context, collect_context,
    collect_context_candidates,
)
pytestmark = pytest.mark.critical

def make(name, score=.5, tokens=1, revision="r1"):
    return ContextItem(name, "text", "record", revision, "memory", score, tokens,
                       (ContextCitation(name, f"record:{name}"),))

def test_rejects_uncited_unbudgeted_or_invalid_context():
    with pytest.raises(ValueError, match="uncited"):
        ContextItem("id", "text", "source", "r1", "memory", .5, 2, ())
    with pytest.raises(ValueError, match="positive"):
        ContextItem("id", "text", "source", "r1", "memory", .5, 0,
                    (ContextCitation("id", "record:id"),))
    with pytest.raises(ValueError, match="between"):
        make("id", score=2)

def test_assembly_is_stable_budgeted_and_whole_item_only():
    result = assemble_context([make("low", .4, 2), make("high", .9, 3),
                               make("mid", .7, 2)], budget_tokens=4)
    assert [x.item_id for x in result.items] == ["high"]
    assert (result.used_tokens, result.omitted_items) == (3, 2)

def test_identity_includes_provider_item_and_revision_and_keeps_best_duplicate():
    result = assemble_context([make("same", .2, 2), make("same", .8, 2),
                               make("same", .7, 2, "r2")], budget_tokens=10)
    assert [(x.revision, x.score) for x in result.items] == [("r1", .8), ("r2", .7)]

def test_zero_budget_is_explicit_and_negative_fails():
    assert assemble_context([make("a")], budget_tokens=0).items == ()
    with pytest.raises(ValueError, match="non-negative"):
        assemble_context([], budget_tokens=-1)


class Provider:
    def __init__(self, provider_id, result=None, error=None):
        self.provider_id = provider_id
        self.result = result or []
        self.error = error

    async def search(self, query, *, limit, cancellation=None):
        if cancellation is not None:
            cancellation.checkpoint()
        if self.error:
            raise self.error
        return self.result[:limit]


@pytest.mark.asyncio
async def test_collection_isolates_provider_failure_and_preserves_healthy_context():
    healthy = Provider("memory", [make("kept", .8, 2)])
    broken = Provider("worldview", error=RuntimeError("sensitive detail"))
    result = await collect_context(
        [broken, healthy], "query", limit_per_provider=3, budget_tokens=4)
    assert [item.item_id for item in result.assembly.items] == ["kept"]
    assert [(failure.provider, failure.reason) for failure in result.failures] == [
        ("worldview", "RuntimeError")]


@pytest.mark.asyncio
async def test_collection_rejects_foreign_identity_without_losing_other_providers():
    foreign = Provider("worldview", [make("wrong")])
    healthy = Provider("memory", [make("kept")])
    result = await collect_context(
        [foreign, healthy], "query", limit_per_provider=2, budget_tokens=3)
    assert [item.item_id for item in result.assembly.items] == ["kept"]
    assert result.failures[0].provider == "worldview"


@pytest.mark.asyncio
async def test_collection_rejects_ambiguous_provider_ids_before_dispatch():
    with pytest.raises(ValueError, match="unique"):
        await collect_context([Provider("memory"), Provider("memory")], "query",
                              limit_per_provider=1, budget_tokens=1)


@pytest.mark.asyncio
async def test_candidate_collection_does_not_apply_budget_early():
    result = await collect_context_candidates(
        [Provider("memory", [make("a", .9, 3), make("b", .4, 3)])],
        "query", limit_per_provider=3)
    assert [item.item_id for item in result.items] == ["a", "b"]
