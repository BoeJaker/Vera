import asyncio

import pytest

from vera.fabric.memory_dual_write_rollback import (
    produce_memory_rollback_rehearsal,
    run_memory_rollback_rehearsal,
)
from vera.fabric.memory_dual_write_store import (
    MemoryDualWriteReceiptStore,
    MemoryReceiptConflict,
)


pytestmark = pytest.mark.critical
DONE = "2026-01-01T00:00:00Z"


def run(value):
    return asyncio.run(value)


def test_real_disabled_gates_produce_exact_payload_free_receipt(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    result = run(produce_memory_rollback_rehearsal(
        store=store, rehearsal_id="rollback-1", source_commit="abcdef12",
        completed_at=DONE))
    assert result["created"] is True
    receipt = result["receipt"]
    assert receipt["passed"] is True
    assert receipt["canary_disabled"] is True
    assert receipt["native_write_available"] is True
    assert receipt["revision_write_attempted"] is False
    assert receipt["reconciliation_disabled"] is True
    assert receipt["source_commit"] == "abcdef12"
    assert "deliberately" not in repr(receipt)
    assert store.latest_rollback_rehearsal().receipt_id == receipt["receipt_id"]


def test_exact_replay_runs_no_probe_and_source_retarget_conflicts(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    first = run(produce_memory_rollback_rehearsal(
        store=store, rehearsal_id="rollback-1", source_commit="abcdef12",
        completed_at=DONE))

    async def must_not_run(*args, **kwargs):
        raise AssertionError("replay executed a probe")

    replay = run(produce_memory_rollback_rehearsal(
        store=store, rehearsal_id="rollback-1", source_commit="abcdef12",
        canary_runner=must_not_run, reconciliation_runner=must_not_run))
    assert replay == {"created": False, "receipt": first["receipt"]}
    with pytest.raises(MemoryReceiptConflict, match="source commit differs"):
        run(produce_memory_rollback_rehearsal(
            store=store, rehearsal_id="rollback-1", source_commit="different",
            canary_runner=must_not_run, reconciliation_runner=must_not_run))


def test_concurrent_winner_with_same_source_is_accepted(tmp_path, monkeypatch):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    original = store.append_rollback_rehearsal
    raced = False

    def concurrent_append(receipt):
        nonlocal raced
        if not raced:
            raced = True
            original(receipt)
            raise MemoryReceiptConflict("simulated concurrent winner")
        return original(receipt)

    monkeypatch.setattr(store, "append_rollback_rehearsal", concurrent_append)
    result = run(produce_memory_rollback_rehearsal(
        store=store, rehearsal_id="rollback-1", source_commit="abcdef12",
        completed_at=DONE))
    assert result["created"] is False
    assert result["receipt"]["source_commit"] == "abcdef12"


def test_false_canary_proof_writes_no_receipt(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")

    async def false_canary(*args, **kwargs):
        return {"not": "the native sentinel"}

    async def disabled_reconciliation(*args, **kwargs):
        raise PermissionError("disabled")

    with pytest.raises(RuntimeError, match="every disabled-path invariant"):
        run(produce_memory_rollback_rehearsal(
            store=store, rehearsal_id="rollback-1", source_commit="abcdef12",
            canary_runner=false_canary,
            reconciliation_runner=disabled_reconciliation))
    assert store.latest_rollback_rehearsal() is None


def test_false_reconciliation_proof_writes_no_receipt(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")

    async def not_disabled(*args, **kwargs):
        return {"unexpected": "success"}

    with pytest.raises(RuntimeError, match="every disabled-path invariant"):
        run(produce_memory_rollback_rehearsal(
            store=store, rehearsal_id="rollback-1", source_commit="abcdef12",
            reconciliation_runner=not_disabled))
    assert store.latest_rollback_rehearsal() is None


def test_probe_exception_propagates_without_evidence(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")

    async def broken(*args, **kwargs):
        raise ValueError("probe broke")

    with pytest.raises(ValueError, match="probe broke"):
        run(produce_memory_rollback_rehearsal(
            store=store, rehearsal_id="rollback-1", source_commit="abcdef12",
            canary_runner=broken))
    assert store.latest_rollback_rehearsal() is None


def test_runtime_binding_uses_only_configured_evidence_journal(tmp_path):
    path = tmp_path / "evidence.sqlite3"
    result = run(run_memory_rollback_rehearsal(
        rehearsal_id="rollback-1", source_commit="abcdef12",
        completed_at=DONE,
        environ={"VERA_MEMORY_DUAL_WRITE_RECEIPTS": str(path)}))
    assert result["created"] is True
    assert list(tmp_path.iterdir()) == [path]
    assert MemoryDualWriteReceiptStore(path).rollback_rehearsal(
        "rollback-1").source_commit == "abcdef12"


def test_invalid_store_runner_or_source_identity_fails_closed(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    with pytest.raises(TypeError, match="store"):
        run(produce_memory_rollback_rehearsal(
            store={}, rehearsal_id="rollback-1", source_commit="abcdef12"))
    with pytest.raises(TypeError, match="canary_runner"):
        run(produce_memory_rollback_rehearsal(
            store=store, rehearsal_id="rollback-1", source_commit="abcdef12",
            canary_runner=None))
    with pytest.raises(ValueError, match="source_commit"):
        run(produce_memory_rollback_rehearsal(
            store=store, rehearsal_id="rollback-1", source_commit=""))
