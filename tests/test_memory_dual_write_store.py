import json
import sqlite3

import pytest

from vera.fabric.memory_dual_write_receipt import create_memory_dual_write_receipt
from vera.fabric.memory_dual_write_store import (
    MemoryDualWriteReceiptStore,
    MemoryReceiptConflict,
    MemoryReceiptCorruption,
)
from vera.fabric.record_revision import create_record_revision


pytestmark = pytest.mark.critical
NOW = "2026-01-01T00:00:00Z"


def revision(text="memory"):
    return create_record_revision(
        namespace="memory.general", record_type="fact", logical_key="native-1",
        content=text, content_schema={"media_type": "text/plain"},
        source={"kind": "native_memory"}, policy={}, metadata={}, created_at=NOW)


def receipt(operation="op-1", *, neo4j="committed"):
    errors = {"neo4j": "backend_failed"} if neo4j == "failed" else {}
    return create_memory_dual_write_receipt(
        operation_id=operation, native_memory_id="native-1",
        native_content_hash="native:0123456789abcdef", revision=revision(),
        observed_at=NOW, revision_outcome="committed",
        native_outcomes={"postgres": "committed", "chroma": "committed", "neo4j": neo4j},
        error_codes=errors)


def test_append_replay_restart_and_bounded_reconciliation(tmp_path):
    path = tmp_path / "receipts.sqlite3"
    store = MemoryDualWriteReceiptStore(path)
    first = store.append(receipt())
    second = store.append(receipt())
    store.append(receipt("op-2", neo4j="failed"))
    assert first["created"] is True and second["created"] is False
    assert store.count() == 2
    assert [item["operation_id"] for item in store.reconciliation()] == ["op-2"]
    restarted = MemoryDualWriteReceiptStore(path)
    assert restarted.get("op-1")["receipt_id"] == receipt().receipt_id
    assert restarted.count() == 2
    with pytest.raises(ValueError, match="between"):
        restarted.reconciliation(limit=0)


def test_operation_replay_with_different_evidence_conflicts(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    store.append(receipt())
    changed = create_memory_dual_write_receipt(
        operation_id="op-1", native_memory_id="native-1",
        native_content_hash="native:different", revision=revision(), observed_at=NOW,
        revision_outcome="committed",
        native_outcomes={"postgres": "committed", "chroma": "committed", "neo4j": "committed"})
    with pytest.raises(MemoryReceiptConflict, match="replay differs"):
        store.append(changed)


def test_corrupt_stored_evidence_fails_closed(tmp_path):
    path = tmp_path / "receipts.sqlite3"
    store = MemoryDualWriteReceiptStore(path)
    store.append(receipt())
    with sqlite3.connect(path) as conn:
        payload = json.loads(conn.execute(
            "SELECT evidence_json FROM memory_dual_write_receipts").fetchone()[0])
        payload["authority_state"] = "dual_committed" if payload["authority_state"] != "dual_committed" else "native_only"
        conn.execute("UPDATE memory_dual_write_receipts SET evidence_json=?",
                     (json.dumps(payload),))
    with pytest.raises(MemoryReceiptCorruption, match="identity"):
        store.get("op-1")


def test_store_rejects_non_receipt(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    with pytest.raises(TypeError, match="MemoryDualWriteReceipt"):
        store.append({})


def test_reconciliation_attempt_restarts_replays_and_converges(tmp_path):
    path = tmp_path / "receipts.sqlite3"
    store = MemoryDualWriteReceiptStore(path)
    store.append(receipt("op-1", neo4j="failed"))
    assert store.reconciliation_status("op-1")["unresolved_targets"] == ["neo4j"]
    started = store.start_reconciliation(
        operation_id="op-1", attempt_id="attempt-1", targets=["neo4j"],
        started_at=NOW)
    assert started["state"] == "started"
    assert MemoryDualWriteReceiptStore(path).pending_reconciliations() == [started]
    replay = MemoryDualWriteReceiptStore(path).start_reconciliation(
        operation_id="op-1", attempt_id="attempt-1", targets=["neo4j"],
        started_at=NOW)
    assert replay == started
    completed = store.complete_reconciliation(
        attempt_id="attempt-1", outcomes={"neo4j": "committed"},
        error_codes={}, completed_at="2026-01-01T00:00:01Z")
    assert completed["state"] == "completed"
    assert store.pending_reconciliations() == []
    status = MemoryDualWriteReceiptStore(path).reconciliation_status("op-1")
    assert status["converged"] is True
    assert status["authority_switch_allowed"] is False
    assert MemoryDualWriteReceiptStore(path).reconciliation() == []


def test_reconciliation_attempt_conflicts_and_validates_exact_targets(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    store.append(receipt("op-1", neo4j="failed"))
    with pytest.raises(ValueError, match="exactly match"):
        store.start_reconciliation(
            operation_id="op-1", attempt_id="attempt-1", targets=["postgres"],
            started_at=NOW)
    store.start_reconciliation(
        operation_id="op-1", attempt_id="attempt-1", targets=["neo4j"],
        started_at=NOW)
    with pytest.raises(MemoryReceiptConflict, match="replay differs"):
        store.start_reconciliation(
            operation_id="op-1", attempt_id="attempt-1", targets=["neo4j"],
            started_at="2026-01-01T00:00:01Z")
    with pytest.raises(ValueError, match="every reconciliation target"):
        store.complete_reconciliation(
            attempt_id="attempt-1", outcomes={}, error_codes={},
            completed_at="2026-01-01T00:00:02Z")
    with pytest.raises(ValueError, match="cannot precede"):
        store.complete_reconciliation(
            attempt_id="attempt-1", outcomes={"neo4j": "failed"},
            error_codes={"neo4j": "repair_failed"},
            completed_at="2025-12-31T23:59:59Z")
    with pytest.raises(ValueError, match="bounded error"):
        store.complete_reconciliation(
            attempt_id="attempt-1", outcomes={"neo4j": "failed"},
            error_codes={"neo4j": "private path leaked"},
            completed_at="2026-01-01T00:00:02Z")


def test_only_one_started_attempt_can_own_an_operation(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    store.append(receipt("op-1", neo4j="failed"))
    store.start_reconciliation(
        operation_id="op-1", attempt_id="attempt-1", targets=["neo4j"],
        started_at=NOW)
    with pytest.raises(MemoryReceiptConflict, match="already owns"):
        store.start_reconciliation(
            operation_id="op-1", attempt_id="attempt-2", targets=["neo4j"],
            started_at="2026-01-01T00:00:01Z")


def test_reconciliation_success_is_sticky_across_later_failed_attempts(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    store.append(receipt("op-1", neo4j="failed"))
    store.start_reconciliation(
        operation_id="op-1", attempt_id="attempt-1", targets=["neo4j"],
        started_at=NOW)
    store.complete_reconciliation(
        attempt_id="attempt-1", outcomes={"neo4j": "committed"},
        error_codes={}, completed_at="2026-01-01T00:00:01Z")
    assert store.reconciliation_status("op-1")["converged"] is True
    with pytest.raises(ValueError, match="no unresolved"):
        store.start_reconciliation(
            operation_id="op-1", attempt_id="attempt-2", targets=["neo4j"],
            started_at="2026-01-01T00:00:02Z")
