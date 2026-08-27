import pytest

from vera.fabric.dataset_provider import CancellationSignal, QueryCancelled
from vera.fabric.memory_audit import (
    AuditedMemoryProvider, FrozenMemoryAuditSink, MemoryOperationReceipt,
)
from vera.fabric.memory_provider import (
    FrozenMemoryProvider, MemoryAccessContext, MemoryAccessDenied,
    MemoryCitation, MemoryProjection, MemoryQuery,
)


pytestmark = pytest.mark.critical
RECORD = "rec_" + "a" * 64
REVISION = "rev_" + "b" * 64


def access(request_id="request-1", tenant="tenant.one"):
    return MemoryAccessContext(tenant_id=tenant, principal_id="user.one",
                               request_id=request_id)


def projection(text="private memory text"):
    citation = MemoryCitation.create(
        citation_id="source.primary", uri=f"fabric://records/{RECORD}",
        record_id=RECORD, revision_id=REVISION)
    return MemoryProjection(
        tenant_id="tenant.one", namespace="memory.general", record_id=RECORD,
        revision_id=REVISION, source_content_hash="sha256:" + "c" * 64,
        record_type="fact", created_at="2026-01-01T00:00:00Z", text=text,
        citations=[citation], policy={"classification": "private"})


def allow_all(operation, actor, context):
    return True


def test_apply_read_and_search_emit_payload_free_success_receipts():
    sink = FrozenMemoryAuditSink()
    provider = AuditedMemoryProvider(
        FrozenMemoryProvider(authorizer=allow_all), sink)
    item = provider.apply(projection(), access("apply-1"))
    provider.get(item.memory_id, access("read-1"))
    provider.search(MemoryQuery(tenant_id="tenant.one", text="private"),
                    access("search-1"))
    receipts = sink.receipts()
    assert [item.operation for item in receipts] == ["apply", "read", "search"]
    assert all(item.outcome == "succeeded" for item in receipts)
    assert receipts[-1].result_count == 1
    encoded = str([item.to_dict() for item in receipts])
    assert "private memory text" not in encoded
    assert "private'" not in encoded


def test_denial_and_not_found_are_audited_without_swallowing_errors():
    sink = FrozenMemoryAuditSink()
    denied = AuditedMemoryProvider(FrozenMemoryProvider(), sink)
    with pytest.raises(MemoryAccessDenied):
        denied.apply(projection(), access("denied-1"))
    readable = AuditedMemoryProvider(
        FrozenMemoryProvider(authorizer=allow_all), sink)
    with pytest.raises(KeyError):
        readable.get("mem_" + "0" * 64, access("missing-1"))
    assert [(r.outcome, r.reason_code) for r in sink.receipts()] == [
        ("denied", "access_denied"), ("not_found", "not_found")]


def test_request_id_is_required_before_provider_is_called():
    class Explodes:
        name = "must-not-run"
        def apply(self, projection, access):
            raise AssertionError("delegate called")
    provider = AuditedMemoryProvider(Explodes(), FrozenMemoryAuditSink())
    with pytest.raises(ValueError, match="request_id"):
        provider.apply(projection(), access(""))


def test_receipt_identity_is_deterministic_for_idempotent_retry():
    sink = FrozenMemoryAuditSink()
    provider = AuditedMemoryProvider(
        FrozenMemoryProvider(authorizer=allow_all), sink)
    provider.apply(projection(), access("same-request"))
    provider.apply(projection(), access("same-request"))
    assert len(sink.receipts()) == 1


def test_same_operation_with_new_request_id_is_a_distinct_receipt():
    sink = FrozenMemoryAuditSink()
    provider = AuditedMemoryProvider(
        FrozenMemoryProvider(authorizer=allow_all), sink)
    provider.apply(projection(), access("request-a"))
    provider.apply(projection(), access("request-b"))
    assert len(sink.receipts()) == 2


def test_sink_limit_fails_closed_after_successful_provider_call():
    sink = FrozenMemoryAuditSink(max_receipts=1)
    provider = AuditedMemoryProvider(
        FrozenMemoryProvider(authorizer=allow_all), sink)
    item = provider.apply(projection(), access("apply-1"))
    with pytest.raises(ValueError, match="sink limit"):
        provider.get(item.memory_id, access("read-1"))


def test_provider_error_remains_primary_when_audit_sink_also_fails():
    class BrokenProvider:
        name = "broken"
        def get(self, memory_id, access, include_text=True):
            raise RuntimeError("provider exploded")
    class BrokenSink:
        def append(self, receipt):
            raise OSError("sink exploded")
    provider = AuditedMemoryProvider(BrokenProvider(), BrokenSink())
    with pytest.raises(RuntimeError, match="provider exploded") as caught:
        provider.get("mem_" + "0" * 64, access())
    assert any("audit sink also failed" in note for note in caught.value.__notes__)


def test_access_request_id_is_bounded_identifier():
    with pytest.raises(ValueError, match="request_id"):
        access("contains spaces")


def test_cancelled_search_and_cross_tenant_denial_are_audited():
    sink = FrozenMemoryAuditSink()
    provider = AuditedMemoryProvider(
        FrozenMemoryProvider(authorizer=allow_all), sink)
    signal = CancellationSignal()
    signal.cancel()
    with pytest.raises(QueryCancelled):
        provider.search(MemoryQuery(tenant_id="tenant.one"), access("cancel-1"),
                        cancellation=signal)
    with pytest.raises(MemoryAccessDenied):
        provider.search(MemoryQuery(tenant_id="tenant.one"),
                        access("tenant-denied", tenant="tenant.two"))
    assert [item.outcome for item in sink.receipts()] == ["cancelled", "denied"]


def test_receipt_rejects_mutation_without_matching_checksum():
    sink = FrozenMemoryAuditSink()
    provider = AuditedMemoryProvider(
        FrozenMemoryProvider(authorizer=allow_all), sink)
    provider.apply(projection(), access("apply-1"))
    original = sink.receipts()[0]
    values = original.to_dict()
    values["outcome"] = "denied"
    with pytest.raises(ValueError, match="checksum"):
        MemoryOperationReceipt(**values)


def test_invalid_memory_id_is_hashed_not_copied_into_failure_receipt():
    sink = FrozenMemoryAuditSink()
    provider = AuditedMemoryProvider(
        FrozenMemoryProvider(authorizer=allow_all), sink)
    with pytest.raises(ValueError, match="memory_id"):
        provider.get("secret free-form value", access("invalid-id"))
    receipt = sink.receipts()[0]
    assert receipt.outcome == "invalid"
    assert receipt.memory_id == ""
    assert "secret free-form value" not in str(receipt.to_dict())
