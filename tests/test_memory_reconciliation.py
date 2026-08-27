import pytest

from vera.fabric.dataset_provider import CancellationSignal, QueryCancelled
from vera.fabric.memory_provider import (
    FrozenMemoryProvider, MemoryAccessContext, MemoryCitation, MemoryProjection,
)
from vera.fabric.memory_reconciliation import reconcile_memory_provider


pytestmark = pytest.mark.critical
NOW = "2026-01-01T00:00:00Z"
LATER = "2026-01-02T00:00:00Z"


def allow_all(operation, actor, context):
    return True


def access(tenant="tenant.one"):
    return MemoryAccessContext(tenant_id=tenant, principal_id="user.one")


def projection(seed: str, *, revision_seed: str = "b", updated_at: str = NOW,
               text: str = "portable memory", tombstone: bool = False,
               tenant: str = "tenant.one"):
    record_id = "rec_" + seed * 64
    revision_id = "rev_" + revision_seed * 64
    citation = MemoryCitation.create(
        citation_id="source.primary", uri=f"fabric://records/{record_id}",
        record_id=record_id, revision_id=revision_id)
    return MemoryProjection(
        tenant_id=tenant, namespace="memory.general", record_id=record_id,
        revision_id=revision_id, source_content_hash="sha256:" + seed * 64,
        record_type="fact", created_at=NOW, updated_at=updated_at,
        text=text, tombstone=tombstone, citations=[citation])


def test_export_is_bounded_deterministic_policy_filtered_and_text_redacted():
    provider = FrozenMemoryProvider(authorizer=allow_all)
    items = [provider.apply(projection(seed), access()) for seed in ("c", "a", "d")]
    first = provider.export(access(), limit=2)
    second = provider.export(access(), limit=2, cursor=first.next_cursor)
    exported = list(first.projections + second.projections)
    assert [item["memory_id"] for item in exported] == sorted(
        item.memory_id for item in items)
    assert all(item["text"] == "" for item in exported)
    assert first.export_id == second.export_id
    assert first.generation == second.generation == 3


def test_export_cursor_is_generation_bound_and_checks_cancellation():
    provider = FrozenMemoryProvider(authorizer=allow_all)
    provider.apply(projection("a"), access())
    provider.apply(projection("c"), access())
    first = provider.export(access(), limit=1)
    provider.apply(projection("d"), access())
    with pytest.raises(ValueError, match="mismatched cursor"):
        provider.export(access(), cursor=first.next_cursor)
    signal = CancellationSignal()
    signal.cancel()
    with pytest.raises(QueryCancelled):
        provider.export(access(), cancellation=signal)


def test_export_cursor_is_principal_bound_and_per_record_denials_are_hidden():
    def authorize(operation, actor, context):
        if operation == "read":
            return context["record_id"] != "rec_" + "c" * 64
        return True
    provider = FrozenMemoryProvider(authorizer=allow_all)
    provider.apply(projection("a"), access())
    provider.apply(projection("c"), access())
    provider._authorize = authorize
    page = provider.export(access(), limit=1)
    assert len(page.projections) == 1
    assert page.projections[0]["record_id"] == "rec_" + "a" * 64
    provider._authorize = allow_all
    other = MemoryAccessContext(tenant_id="tenant.one", principal_id="user.two")
    # Create a real cursor under the first principal, then prove it cannot move.
    first = provider.export(access(), limit=1)
    with pytest.raises(ValueError, match="mismatched cursor"):
        provider.export(other, limit=1, cursor=first.next_cursor)


def test_reconciliation_reports_convergence_without_payloads():
    provider = FrozenMemoryProvider(authorizer=allow_all)
    expected = [projection("a"), projection("c")]
    for item in expected:
        provider.apply(item, access())
    report = reconcile_memory_provider(provider, expected, access(), page_size=1)
    data = report.to_dict()
    assert report.converged and report.matched_count == 2
    assert data["expected_set_hash"] == data["observed_set_hash"]
    assert "portable memory" not in repr(data)


def test_reconciliation_classifies_missing_unexpected_and_drift():
    provider = FrozenMemoryProvider(authorizer=allow_all)
    original = projection("a")
    provider.apply(original, access())
    changed = projection("a", revision_seed="c", updated_at=LATER,
                         text="changed projection")
    provider.apply(changed, access())
    unexpected = projection("d")
    provider.apply(unexpected, access())
    missing = projection("e")
    report = reconcile_memory_provider(provider, [original, missing], access())
    assert report.drifted_memory_ids == (original.memory_id,)
    assert report.missing_memory_ids == (missing.memory_id,)
    assert report.unexpected_memory_ids == (unexpected.memory_id,)
    assert not report.converged and report.matched_count == 0


def test_reconciliation_rejects_cross_tenant_expected_and_duplicate_ids():
    provider = FrozenMemoryProvider(authorizer=allow_all)
    item = projection("a")
    with pytest.raises(ValueError, match="unique"):
        reconcile_memory_provider(provider, [item, item], access())
    other = projection("a", tenant="tenant.two")
    with pytest.raises(ValueError, match="tenant"):
        reconcile_memory_provider(provider, [other], access())
