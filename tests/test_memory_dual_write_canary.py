import asyncio

import pytest

from vera.fabric.memory_dual_write_canary import execute_memory_dual_write_canary
from vera.fabric.memory_dual_write_store import MemoryDualWriteReceiptStore
from vera.fabric.record_revision import create_record_revision


pytestmark = pytest.mark.critical
NOW = "2026-01-01T00:00:00Z"


def revision():
    return create_record_revision(
        namespace="memory.general", record_type="fact", logical_key="native-1",
        content="memory", content_schema={"media_type": "text/plain"},
        source={"kind": "native_memory"}, policy={}, metadata={}, created_at=NOW)


def run(coro):
    return asyncio.run(coro)


def call(tmp_path, native, canonical, **changes):
    values = dict(
        enabled=True, operation_id="op-1", native_memory_id="native-1",
        native_content_hash="native:0123456789abcdef", revision=revision(),
        observed_at=NOW, native_write=native, revision_write=canonical,
        receipt_sink=MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3"),
        native_timeout_s=0.05, revision_timeout_s=0.05)
    values.update(changes)
    return execute_memory_dual_write_canary(**values)


async def native_ok():
    return {"postgres": True, "chroma": True, "neo4j": True}


async def revision_ok(value):
    return {"authority_commit": "committed", "revision_id": value.revision_id}


def test_default_off_calls_native_once_and_never_calls_revision_or_sink(tmp_path):
    calls = {"native": 0, "revision": 0}

    async def native():
        calls["native"] += 1
        return {"postgres": True}

    async def canonical(value):
        calls["revision"] += 1
        return {"authority_commit": "committed"}

    result = run(call(tmp_path, native, canonical, enabled=False))
    assert calls == {"native": 1, "revision": 0}
    assert result.enabled is False and result.receipt is None
    assert result.revision_attempted is False and result.evidence_persisted is False
    assert not (tmp_path / "receipts.sqlite3").exists() or MemoryDualWriteReceiptStore(
        tmp_path / "receipts.sqlite3").count() == 0


def test_enabled_success_persists_exact_non_authorizing_receipt(tmp_path):
    result = run(call(tmp_path, native_ok, revision_ok))
    assert result.native_result["postgres"] is True
    assert result.receipt.authority_state == "dual_committed"
    assert result.receipt.authority_switch_allowed is False
    assert result.evidence_persisted is True and result.evidence_error_code == ""
    assert MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3").count() == 1


def test_optional_outer_timeouts_preserve_injected_writer_ownership(tmp_path):
    async def native():
        await asyncio.sleep(0.06)
        return {"postgres": True}

    result = run(call(
        tmp_path, native, revision_ok,
        native_timeout_s=None, revision_timeout_s=None))
    assert result.native_result == {"postgres": True}
    assert result.receipt.revision_outcome == "committed"


@pytest.mark.parametrize("mode,expected", [("error", "failed"), ("timeout", "timed_out")])
def test_revision_failure_isolated_from_native_result_and_reconciled(tmp_path, mode, expected):
    async def canonical(value):
        if mode == "timeout":
            await asyncio.sleep(1)
        raise RuntimeError("private backend detail")

    result = run(call(tmp_path, native_ok, canonical))
    assert result.native_result == {"postgres": True, "chroma": True, "neo4j": True}
    assert result.receipt.revision_outcome == expected
    assert result.receipt.authority_state == "native_only"
    assert result.receipt.requires_reconciliation is True
    assert "private backend detail" not in repr(result.receipt.to_dict())


@pytest.mark.parametrize("result", [None, True, "committed", object()])
def test_revision_writer_requires_explicit_commit_receipt(tmp_path, result):
    async def canonical(value):
        return result

    observed = run(call(tmp_path, native_ok, canonical))
    assert observed.native_result == {
        "postgres": True, "chroma": True, "neo4j": True}
    assert observed.receipt.revision_outcome == "failed"
    assert observed.receipt.error_codes["revision"] == "revision_not_committed"
    assert observed.receipt.authority_state == "native_only"
    assert observed.receipt.requires_reconciliation is True


def test_partial_native_result_is_explicit_and_missing_backend_is_skipped(tmp_path):
    async def native():
        return {"postgres": True, "neo4j": False}

    result = run(call(tmp_path, native, revision_ok))
    assert dict(result.receipt.native_outcomes) == {
        "postgres": "committed", "chroma": "skipped", "neo4j": "failed"}
    assert result.receipt.requires_reconciliation is True


def test_native_error_and_timeout_propagate_after_receipt(tmp_path):
    async def failure():
        raise RuntimeError("native private error")

    with pytest.raises(RuntimeError, match="native private"):
        run(call(tmp_path, failure, revision_ok))
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    assert store.get("op-1")["revision_outcome"] == "skipped"
    assert "native private error" not in repr(store.get("op-1"))

    async def slow():
        await asyncio.sleep(1)
        return {}

    with pytest.raises(TimeoutError):
        run(call(tmp_path, slow, revision_ok, operation_id="op-2"))
    assert store.get("op-2")["native_outcomes"]["postgres"] == "timed_out"


def test_revision_cancellation_propagates_after_durable_receipt(tmp_path):
    async def cancelled(value):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        run(call(tmp_path, native_ok, cancelled))
    stored = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3").get("op-1")
    assert stored["revision_outcome"] == "cancelled"


def test_native_cancellation_propagates_without_starting_revision(tmp_path):
    calls = {"revision": 0}

    async def cancelled():
        raise asyncio.CancelledError()

    async def canonical(value):
        calls["revision"] += 1
        return {"authority_commit": "committed"}

    with pytest.raises(asyncio.CancelledError):
        run(call(tmp_path, cancelled, canonical))
    assert calls["revision"] == 0
    stored = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3").get("op-1")
    assert stored["revision_outcome"] == "skipped"
    assert set(stored["native_outcomes"].values()) == {"cancelled"}


def test_receipt_sink_failure_is_visible_but_does_not_retroactively_fail_native(tmp_path):
    class Broken:
        def append(self, receipt):
            raise OSError("disk detail")

    result = run(execute_memory_dual_write_canary(
        enabled=True, operation_id="op-1", native_memory_id="native-1",
        native_content_hash="native:0123456789abcdef", revision=revision(),
        observed_at=NOW, native_write=native_ok, revision_write=revision_ok,
        receipt_sink=Broken()))
    assert result.native_result["postgres"] is True
    assert result.evidence_persisted is False
    assert result.evidence_error_code == "receipt_store_failed"


def test_registered_additional_native_backend_is_preserved_in_evidence(tmp_path):
    async def native():
        return {"postgres": True, "mystery": True}

    result = run(call(tmp_path, native, revision_ok))
    assert result.native_result == {"postgres": True, "mystery": True}
    assert result.receipt.native_outcomes["mystery"] == "committed"


def test_invalid_native_evidence_cannot_retroactively_fail_native(tmp_path):
    async def native():
        return {"postgres": "not-an-outcome"}

    result = run(call(tmp_path, native, revision_ok))
    assert result.native_result == {"postgres": "not-an-outcome"}
    assert result.receipt is None
    assert result.evidence_error_code == "native_outcome_classification_failed"
