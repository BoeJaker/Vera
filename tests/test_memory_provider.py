import math

import pytest

from vera.fabric.dataset_provider import CancellationSignal, QueryCancelled
from vera.fabric.memory_provider import (
    FrozenMemoryProvider,
    MemoryAccessContext,
    MemoryAccessDenied,
    MemoryCitation,
    MemoryProjection,
    MemoryQuery,
)


pytestmark = pytest.mark.critical
NOW = "2026-01-01T00:00:00Z"
LATER = "2026-01-02T00:00:00Z"
RECORD = "rec_" + "a" * 64
REVISION = "rev_" + "b" * 64
REVISION_2 = "rev_" + "c" * 64
CONTENT_HASH = "sha256:" + "2" * 64


def access(tenant="tenant.one", principal="user.one", **kwargs):
    return MemoryAccessContext(tenant_id=tenant, principal_id=principal, **kwargs)


def citation(record_id=RECORD, revision_id=REVISION, **kwargs):
    values = dict(citation_id="source.primary", uri=f"fabric://records/{record_id}",
                  record_id=record_id, revision_id=revision_id,
                  label="authoritative record", locator={"field": "content"})
    values.update(kwargs)
    return MemoryCitation.create(**values)


def projection(**overrides):
    values = dict(tenant_id="tenant.one", namespace="memory.general",
                  record_id=RECORD, revision_id=REVISION,
                  source_content_hash=CONTENT_HASH,
                  record_type="fact", session_id="session.one",
                  created_at=NOW, text="Vera uses cited portable memory",
                  citations=[citation()], tags=["vera", "portable"],
                  policy={"classification": "internal"},
                  metadata={"source": "fixture"}, importance=0.8)
    values.update(overrides)
    return MemoryProjection(**values)


def allow_all(operation, actor, context):
    return True


def test_projection_has_stable_memory_identity_and_frozen_nested_values():
    tags = ["vera", "portable"]
    policy = {"acl": ["user.one"]}
    locator = {"page": 1}
    item = projection(tags=tags, policy=policy,
                      citations=[citation(locator=locator)])
    changed = projection(revision_id=REVISION_2, updated_at=LATER,
                         citations=[citation(revision_id=REVISION_2)])
    tags.append("changed")
    policy["acl"].append("other")
    locator["page"] = 999
    assert item.memory_id == changed.memory_id
    assert item.tags == ("portable", "vera")
    assert item.policy == {"acl": ["user.one"]}
    assert item.citations[0].locator == {"page": 1}


def test_projection_requires_authoritative_citation_and_valid_lifecycle():
    with pytest.raises(ValueError, match="authoritative revision"):
        projection(citations=[citation(revision_id=REVISION_2)])
    with pytest.raises(ValueError, match="active memory needs text"):
        projection(text="")
    with pytest.raises(ValueError, match="tombstone must not carry text"):
        projection(tombstone=True)
    tombstone = projection(revision_id=REVISION_2, updated_at=LATER, text="",
                           tombstone=True,
                           citations=[citation(revision_id=REVISION_2)])
    assert tombstone.tombstone is True and tombstone.text == ""
    with pytest.raises(ValueError, match="source_content_hash"):
        projection(source_content_hash="md5:bad")


def test_projection_keeps_authority_hash_distinct_from_derived_text_hash():
    item = projection()
    assert item.source_content_hash == CONTENT_HASH
    assert item.projection_hash.startswith("sha256:")
    assert item.projection_hash != item.source_content_hash


def test_citations_reject_credentials_invalid_ids_and_non_json_locator():
    with pytest.raises(ValueError, match="credential-bearing"):
        citation(uri="https://user:secret@example.test/source")
    with pytest.raises(ValueError, match="record_id"):
        citation(record_id="record")
    with pytest.raises(ValueError, match="finite JSON"):
        citation(locator={"score": math.nan})
    with pytest.raises(ValueError, match="revision_id"):
        MemoryCitation(citation_id="direct", uri="memory://source",
                       record_id=RECORD, revision_id="bad")


def test_caller_supplied_fabric_record_ids_are_supported():
    item = projection(record_id="rec_external:item-1",
                      citations=[citation(record_id="rec_external:item-1")])
    assert item.record_id == "rec_external:item-1"


def test_default_policy_denies_and_cross_tenant_is_never_visible():
    denied = FrozenMemoryProvider()
    with pytest.raises(MemoryAccessDenied):
        denied.apply(projection(), access())
    provider = FrozenMemoryProvider(authorizer=allow_all)
    item = provider.apply(projection(), access())
    with pytest.raises(KeyError, match="not found"):
        provider.get(item.memory_id, access("tenant.two"))
    with pytest.raises(MemoryAccessDenied, match="cross-tenant"):
        provider.search(MemoryQuery(tenant_id="tenant.one"),
                        access("tenant.two"))


def test_apply_is_idempotent_and_updates_must_advance_authoritative_projection():
    provider = FrozenMemoryProvider(authorizer=allow_all)
    first = projection()
    assert provider.apply(first, access()) is first
    assert provider.apply(first, access()) is first
    changed = projection(revision_id=REVISION_2, updated_at=LATER,
                         text="updated cited memory",
                         citations=[citation(revision_id=REVISION_2)])
    assert provider.apply(changed, access()).revision_id == REVISION_2
    with pytest.raises(ValueError, match="advance"):
        provider.apply(projection(revision_id=REVISION_2,
                                  citations=[citation(revision_id=REVISION_2)]),
                       access())
    with pytest.raises(ValueError, match="identity context"):
        provider.apply(projection(revision_id=REVISION_2, updated_at=LATER,
                                  session_id="session.changed",
                                  citations=[citation(revision_id=REVISION_2)]),
                       access())


def test_search_filters_ranks_pages_and_preserves_citations():
    provider = FrozenMemoryProvider(authorizer=allow_all)
    first = provider.apply(projection(), access())
    second_record = "rec_" + "d" * 64
    second_revision = "rev_" + "e" * 64
    provider.apply(projection(record_id=second_record, revision_id=second_revision,
                              session_id="session.two", created_at=LATER,
                              updated_at=LATER, importance=0.4,
                              text="portable memory from another session",
                              tags=["portable", "other"],
                              citations=[citation(record_id=second_record,
                                                  revision_id=second_revision)]),
                   access())
    query = MemoryQuery(tenant_id="tenant.one", text="portable memory",
                        tags=["portable"], limit=1)
    page_one = provider.search(query, access())
    page_two = provider.search(MemoryQuery(
        tenant_id=query.tenant_id, text=query.text, tags=query.tags, limit=1,
        cursor=page_one.next_cursor), access())
    assert page_one.hits[0].projection["memory_id"] == first.memory_id
    assert page_one.hits[0].projection["citations"][0]["revision_id"] == REVISION
    assert page_two.hits[0].projection["session_id"] == "session.two"
    assert not page_two.next_cursor
    session = provider.search(MemoryQuery(
        tenant_id="tenant.one", session_id="session.two"), access())
    assert len(session.hits) == 1


def test_cursor_is_query_and_provider_generation_bound():
    provider = FrozenMemoryProvider(authorizer=allow_all)
    provider.apply(projection(), access())
    second_record = "rec_" + "d" * 64
    second_revision = "rev_" + "e" * 64
    provider.apply(projection(record_id=second_record, revision_id=second_revision,
                              citations=[citation(record_id=second_record,
                                                  revision_id=second_revision)]),
                   access())
    first = provider.search(MemoryQuery(tenant_id="tenant.one", limit=1), access())
    with pytest.raises(ValueError, match="mismatched cursor"):
        provider.search(MemoryQuery(tenant_id="tenant.one", tags=["vera"],
                                    cursor=first.next_cursor), access())
    third_record = "rec_" + "f" * 64
    third_revision = "rev_" + "1" * 64
    provider.apply(projection(record_id=third_record, revision_id=third_revision,
                              citations=[citation(record_id=third_record,
                                                  revision_id=third_revision)]),
                   access())
    with pytest.raises(ValueError, match="mismatched cursor"):
        provider.search(MemoryQuery(tenant_id="tenant.one",
                                    cursor=first.next_cursor), access())


def test_per_record_policy_filters_hits_without_exposing_text_to_authorizer():
    observed = []
    def authorize(operation, actor, context):
        observed.append((operation, context))
        return context.get("policy", {}).get("deny") is not True
    provider = FrozenMemoryProvider(authorizer=authorize)
    provider.apply(projection(), access())
    hidden_record = "rec_" + "d" * 64
    hidden_revision = "rev_" + "e" * 64
    # Apply through an admin authorizer, then restore the filtering policy.
    provider._authorize = allow_all
    provider.apply(projection(record_id=hidden_record, revision_id=hidden_revision,
                              policy={"deny": True},
                              citations=[citation(record_id=hidden_record,
                                                  revision_id=hidden_revision)]),
                   access())
    provider._authorize = authorize
    page = provider.search(MemoryQuery(tenant_id="tenant.one"), access())
    assert len(page.hits) == 1
    assert all("text" not in context for _, context in observed)


def test_tombstones_hidden_by_default_and_text_can_be_redacted():
    provider = FrozenMemoryProvider(authorizer=allow_all)
    active = provider.apply(projection(), access())
    provider.apply(projection(revision_id=REVISION_2, updated_at=LATER, text="",
                              tombstone=True,
                              citations=[citation(revision_id=REVISION_2)]), access())
    assert provider.search(MemoryQuery(tenant_id="tenant.one"), access()).hits == ()
    page = provider.search(MemoryQuery(tenant_id="tenant.one",
                                       include_tombstones=True,
                                       include_text=False), access())
    assert page.hits[0].projection["memory_id"] == active.memory_id
    assert page.hits[0].projection["text"] == ""


def test_returned_nested_values_cannot_mutate_provider_state():
    provider = FrozenMemoryProvider(authorizer=allow_all)
    item = provider.apply(projection(), access())
    hit = provider.search(MemoryQuery(tenant_id="tenant.one"), access()).hits[0]
    exposed = hit.projection
    exposed["policy"]["classification"] = "public"
    assert provider.get(item.memory_id, access())["policy"] == {
        "classification": "internal"}


def test_cancellation_limits_and_record_ceiling_fail_closed():
    provider = FrozenMemoryProvider(authorizer=allow_all, max_records=1)
    provider.apply(projection(), access())
    signal = CancellationSignal()
    signal.cancel()
    with pytest.raises(QueryCancelled):
        provider.search(MemoryQuery(tenant_id="tenant.one"), access(),
                        cancellation=signal)
    with pytest.raises(ValueError, match="limit"):
        MemoryQuery(tenant_id="tenant.one", limit=0)
    second_record = "rec_" + "d" * 64
    second_revision = "rev_" + "e" * 64
    with pytest.raises(ValueError, match="record limit"):
        provider.apply(projection(record_id=second_record,
                                  revision_id=second_revision,
                                  citations=[citation(record_id=second_record,
                                                      revision_id=second_revision)]),
                       access())
