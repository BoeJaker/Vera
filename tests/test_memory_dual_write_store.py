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
