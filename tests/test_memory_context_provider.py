import pytest

from vera.context_provider import collect_context
from vera.fabric.memory_context_provider import MemoryContextProvider
from vera.fabric.dataset_provider import CancellationSignal, QueryCancelled
from vera.fabric.memory_provider import (
    FrozenMemoryProvider, MemoryAccessContext, MemoryCitation, MemoryHit,
    MemoryPage, MemoryProjection,
)

pytestmark = pytest.mark.critical
RECORD = "rec_" + "a" * 64
REVISION = "rev_" + "b" * 64


def access():
    return MemoryAccessContext("tenant.one", "user.one")


def projection():
    citation = MemoryCitation.create(
        citation_id="source.primary", uri=f"fabric://records/{RECORD}",
        record_id=RECORD, revision_id=REVISION)
    return MemoryProjection(
        tenant_id="tenant.one", namespace="memory.general", record_id=RECORD,
        revision_id=REVISION, source_content_hash="sha256:" + "c" * 64,
        record_type="fact", created_at="2026-01-01T00:00:00Z",
        text="portable cited memory", citations=[citation])


def adapter(provider):
    return MemoryContextProvider(
        provider, access(), provider_id="native-memory",
        token_counter=lambda text: len(text.split()))


@pytest.mark.asyncio
async def test_authorized_memory_hit_keeps_revision_citation_and_explicit_tokens():
    provider = FrozenMemoryProvider(authorizer=lambda *_: True)
    memory = provider.apply(projection(), access())
    items = await adapter(provider).search("portable memory", limit=5)
    assert len(items) == 1
    item = items[0]
    assert (item.item_id, item.source, item.revision) == (
        memory.memory_id, RECORD, REVISION)
    assert item.token_count == 3
    assert item.citations[0].locator == f"fabric://records/{RECORD}"


@pytest.mark.asyncio
async def test_memory_policy_denial_is_isolated_by_context_collection():
    denied = adapter(FrozenMemoryProvider())
    result = await collect_context(
        [denied], "memory", limit_per_provider=2, budget_tokens=20)
    assert result.assembly.items == ()
    assert result.failures[0].provider == "native-memory"
    assert result.failures[0].reason == "MemoryAccessDenied"


@pytest.mark.asyncio
async def test_cancelled_collection_remains_a_control_signal():
    signal = CancellationSignal()
    signal.cancel()
    with pytest.raises(QueryCancelled):
        await collect_context(
            [adapter(FrozenMemoryProvider())], "memory", limit_per_provider=2,
            budget_tokens=20, cancellation=signal)


def test_adapter_requires_explicit_provider_identity_and_token_counter():
    provider = FrozenMemoryProvider()
    with pytest.raises(ValueError, match="provider_id"):
        MemoryContextProvider(provider, access(), provider_id="",
                              token_counter=len)
    with pytest.raises(TypeError, match="token_counter"):
        MemoryContextProvider(provider, access(), provider_id="memory",
                              token_counter=None)


@pytest.mark.asyncio
async def test_adapter_rejects_projection_without_matching_revision_citation():
    class BadProvider:
        name = "bad"

        def search(self, query, actor, *, cancellation=None):
            value = projection().to_dict()
            value["citations"][0]["revision_id"] = "rev_" + "d" * 64
            return MemoryPage(query.query_id, (MemoryHit(.8, value),), "", "bad", 1)

    with pytest.raises(ValueError, match="authoritative citation"):
        await adapter(BadProvider()).search("memory", limit=2)
