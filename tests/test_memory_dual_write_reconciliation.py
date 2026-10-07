import asyncio

import pytest

from vera.fabric.memory_dual_write_receipt import create_memory_dual_write_receipt
from vera.fabric.memory_dual_write_reconciliation import reconcile_memory_dual_write
from vera.fabric.memory_dual_write_store import MemoryDualWriteReceiptStore
from vera.fabric.record_revision import create_record_revision


pytestmark = pytest.mark.critical
NOW = "2026-01-01T00:00:00Z"
DONE = "2026-01-01T00:00:01Z"


def run(value):
    return asyncio.run(value)


def revision():
    return create_record_revision(
        namespace="memory.native", record_type="memory", logical_key="native-1",
        content={"text": "memory"}, created_at=NOW,
        source={"kind": "native_memory"}, policy={}, metadata={})


def add_split(store, *, revision_outcome="failed", postgres="committed",
              chroma="failed", neo4j="skipped"):
    outcomes = {"postgres": postgres, "chroma": chroma, "neo4j": neo4j}
    errors = {}
    if revision_outcome in {"failed", "timed_out", "cancelled"}:
        errors["revision"] = "revision_failed"
    for key, outcome in outcomes.items():
        if outcome in {"failed", "timed_out", "cancelled"}:
            errors[key] = f"{key}_failed"
    store.append(create_memory_dual_write_receipt(
        operation_id="op-1", native_memory_id="native-1",
        native_content_hash="native:0123456789abcdef", revision=revision(),
        observed_at=NOW, revision_outcome=revision_outcome,
        native_outcomes=outcomes, error_codes=errors))


def test_executor_repairs_exact_targets_and_replay_runs_no_effects(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    add_split(store)
    calls = []

    async def repair(target, evidence, key):
        calls.append((target, evidence["receipt_id"], key))
        return "committed"

    result = run(reconcile_memory_dual_write(
        store, operation_id="op-1", attempt_id="attempt-1",
        started_at=NOW, completed_at=DONE, repair=repair))
    assert result.targets == ("revision", "chroma")
    assert [call[0] for call in calls] == ["revision", "chroma"]
    assert [call[2] for call in calls] == [
        "attempt-1:revision", "attempt-1:chroma"]
    assert result.converged is True
    assert result.authority_switch_allowed is False
    replay = run(reconcile_memory_dual_write(
        MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3"),
        operation_id="op-1", attempt_id="attempt-1",
        started_at=NOW, completed_at=DONE, repair=repair))
    assert replay.outcomes == result.outcomes
    assert len(calls) == 2


def test_started_attempt_resumes_after_restart_with_same_idempotency_keys(tmp_path):
    path = tmp_path / "receipts.sqlite3"
    store = MemoryDualWriteReceiptStore(path)
    add_split(store, revision_outcome="committed")
    store.start_reconciliation(
        operation_id="op-1", attempt_id="attempt-1", targets=["chroma"],
        started_at=NOW)
    calls = []

    async def repair(target, evidence, key):
        calls.append(key)
        return "committed"

    result = run(reconcile_memory_dual_write(
        MemoryDualWriteReceiptStore(path), operation_id="op-1",
        attempt_id="attempt-1", started_at=NOW, completed_at=DONE,
        repair=repair))
    assert calls == ["attempt-1:chroma"]
    assert result.converged is True


def test_failures_are_classified_without_private_error_text_and_can_retry(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    add_split(store, revision_outcome="committed")

    async def fail(target, evidence, key):
        raise OSError("private backend path")

    first = run(reconcile_memory_dual_write(
        store, operation_id="op-1", attempt_id="attempt-1",
        started_at=NOW, completed_at=DONE, repair=fail))
    assert first.converged is False
    assert first.outcomes == {"chroma": "failed"}
    assert first.error_codes == {"chroma": "oserror"}
    assert "private backend path" not in repr(store.attempt("attempt-1"))

    async def succeed(target, evidence, key):
        return "committed"

    second = run(reconcile_memory_dual_write(
        store, operation_id="op-1", attempt_id="attempt-2",
        started_at="2026-01-01T00:00:02Z",
        completed_at="2026-01-01T00:00:03Z", repair=succeed))
    assert second.converged is True


def test_timeout_is_recorded_for_cancellable_repairs(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    add_split(store, revision_outcome="committed")

    async def slow(target, evidence, key):
        await asyncio.sleep(1)
        return "committed"

    result = run(reconcile_memory_dual_write(
        store, operation_id="op-1", attempt_id="attempt-1",
        started_at=NOW, completed_at=DONE, repair=slow, timeout_s=0.01))
    assert result.outcomes == {"chroma": "timed_out"}
    assert result.error_codes == {"chroma": "repair_timeout"}
    assert result.converged is False


def test_cancellation_is_durable_then_propagates_and_leaves_remaining_unresolved(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    add_split(store)
    calls = 0

    async def cancel(target, evidence, key):
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        run(reconcile_memory_dual_write(
            store, operation_id="op-1", attempt_id="attempt-1",
            started_at=NOW, completed_at=DONE, repair=cancel))
    attempt = store.attempt("attempt-1")
    assert attempt["state"] == "completed"
    assert attempt["outcomes"] == {"revision": "cancelled", "chroma": "skipped"}
    assert attempt["error_codes"] == {"revision": "repair_cancelled"}
    assert calls == 1
    assert store.reconciliation_status("op-1")["unresolved_targets"] == [
        "revision", "chroma"]
