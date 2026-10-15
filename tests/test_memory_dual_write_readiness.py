import asyncio
import hashlib
import json
import sqlite3

import pytest

from vera.fabric.memory_dual_write_readiness import (
    MemoryRollbackRehearsalReceipt,
    assess_memory_authority_readiness,
)
from vera.fabric.memory_dual_write_receipt import create_memory_dual_write_receipt
from vera.fabric.memory_dual_write_repair_runtime import read_memory_authority_readiness
from vera.fabric.memory_dual_write_rollback import run_memory_rollback_rehearsal
from vera.fabric.memory_dual_write_store import (
    MemoryDualWriteReceiptStore,
    MemoryReceiptConflict,
)
from vera.fabric.record_revision import create_record_revision


pytestmark = pytest.mark.critical
NOW = "2026-01-02T00:00:00Z"


def receipt(operation, observed_at="2026-01-01T23:00:00Z", *, neo4j="committed",
            producer_kind="public_capability"):
    revision = create_record_revision(
        namespace="memory.general", record_type="fact", logical_key=f"native-{operation}",
        content="private payload", content_schema={"media_type": "text/plain"},
        source={"kind": "native_memory"}, policy={}, metadata={},
        created_at=observed_at)
    return create_memory_dual_write_receipt(
        operation_id=operation, native_memory_id=f"native-{operation}",
        native_content_hash=f"native:{operation}", revision=revision,
        observed_at=observed_at, revision_outcome="committed",
        native_outcomes={"postgres": "committed", "chroma": "committed", "neo4j": neo4j},
        error_codes={"neo4j": "backend_failed"} if neo4j == "failed" else {},
        producer_kind=producer_kind)


def rollback(**changes):
    values = {
        "rehearsal_id": "rollback-1", "source_commit": "abcdef12",
        "completed_at": "2026-01-01T23:30:00Z", "canary_disabled": True,
        "native_write_available": True, "revision_write_attempted": False,
        "reconciliation_disabled": True,
    }
    values.update(changes)
    return MemoryRollbackRehearsalReceipt(**values)


def test_exact_bounded_evidence_and_passing_rollback_allow_review_only(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    for index, producer in enumerate((
            "public_capability", "conversation", "capability_activity", "workflow"), 1):
        store.append(receipt(f"op-{index}", producer_kind=producer))
    stored = store.append_rollback_rehearsal(rollback())
    assert stored["created"] is True
    restarted = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    evidence = restarted.readiness_evidence(limit=4)
    result = assess_memory_authority_readiness(
        evidence, assessed_at=NOW,
        rollback_receipt=restarted.latest_rollback_rehearsal(),
        expected_source_commit="abcdef12",
        min_receipts=4, max_evidence_age_seconds=7200)

    assert result.ready_for_activation_review is True
    assert result.authority_switch_allowed is False
    assert result.reasons == ()
    assert result.rollback_receipt_id == rollback().receipt_id
    assert "private payload" not in json.dumps(result.to_dict())
    assert evidence["executes"] is False and evidence["retains_payload"] is False


def test_v1_journal_rows_migrate_as_public_capability_evidence(tmp_path):
    path = tmp_path / "v1.sqlite3"
    value = receipt("legacy-op")
    identity = value.identity_dict()
    identity.pop("producer_kind")
    identity["schema"] = "vera.memory-dual-write-receipt/v1"
    legacy_id = "mdwr_" + hashlib.sha256(json.dumps(
        identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    payload = {"receipt_id": legacy_id, **identity}
    # Reproduce the deployed six-column table before the v2 producer field.
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE memory_dual_write_receipts (
                operation_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL UNIQUE,
                evidence_json TEXT NOT NULL, authority_state TEXT NOT NULL,
                requires_reconciliation INTEGER NOT NULL, observed_at TEXT NOT NULL)
        """)
        conn.execute(
            "INSERT INTO memory_dual_write_receipts VALUES (?,?,?,?,?,?)",
            (value.operation_id, legacy_id,
             json.dumps(payload, sort_keys=True, separators=(",", ":")),
             value.authority_state, int(value.requires_reconciliation),
             value.observed_at))

    migrated = MemoryDualWriteReceiptStore(path)
    assert migrated.get("legacy-op")["schema"] == "vera.memory-dual-write-receipt/v1"
    evidence = migrated.readiness_evidence()
    assert evidence["producer_counts"] == {"public_capability": 1}


def test_insufficient_unresolved_and_pending_evidence_fails_closed(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    store.append(receipt("op-1", neo4j="failed"))
    store.append(receipt("op-2"))
    store.start_reconciliation(
        operation_id="op-1", attempt_id="attempt-1", targets=["neo4j"],
        started_at="2026-01-01T23:15:00Z")
    result = assess_memory_authority_readiness(
        store.readiness_evidence(limit=1), assessed_at=NOW,
        rollback_receipt=None, min_receipts=3, max_evidence_age_seconds=7200)

    assert result.ready_for_activation_review is False
    assert set(result.reasons) == {
        "insufficient_receipts", "unresolved_operations", "pending_attempts",
        "rollback_rehearsal_missing", "insufficient_producer_coverage",
    }
    assert result.metrics["truncated"] is True
    assert result.authority_switch_allowed is False


def test_failed_attempt_and_stale_or_failed_rollback_are_visible(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    store.append(receipt("op-1", observed_at="2026-01-01T00:00:00Z", neo4j="failed"))
    store.start_reconciliation(
        operation_id="op-1", attempt_id="attempt-1", targets=["neo4j"],
        started_at="2026-01-01T00:01:00Z")
    store.complete_reconciliation(
        attempt_id="attempt-1", outcomes={"neo4j": "failed"},
        error_codes={"neo4j": "repair_failed"},
        completed_at="2026-01-01T00:02:00Z")
    failed_rollback = rollback(
        completed_at="2026-01-01T00:03:00Z", native_write_available=False)
    result = assess_memory_authority_readiness(
        store.readiness_evidence(), assessed_at=NOW,
        rollback_receipt=failed_rollback, expected_source_commit="different",
        min_receipts=1,
        max_evidence_age_seconds=3600)
    assert set(result.reasons) == {
        "unresolved_operations", "failed_attempts", "evidence_stale",
        "rollback_rehearsal_failed", "rollback_rehearsal_source_mismatch",
        "rollback_rehearsal_stale", "insufficient_producer_coverage",
    }


def test_rollback_receipt_replays_and_rejects_changed_intent(tmp_path):
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    assert store.append_rollback_rehearsal(rollback())["created"] is True
    assert store.append_rollback_rehearsal(rollback())["created"] is False
    with pytest.raises(MemoryReceiptConflict, match="replay differs"):
        store.append_rollback_rehearsal(rollback(native_write_available=False))


def test_corrupt_rollback_result_fails_closed_on_restart(tmp_path):
    path = tmp_path / "receipts.sqlite3"
    store = MemoryDualWriteReceiptStore(path)
    store.append_rollback_rehearsal(rollback())
    import sqlite3
    with sqlite3.connect(path) as conn:
        value = json.loads(conn.execute(
            "SELECT evidence_json FROM memory_canary_rollback_rehearsals"
        ).fetchone()[0])
        value["passed"] = False
        conn.execute(
            "UPDATE memory_canary_rollback_rehearsals SET evidence_json=?",
            (json.dumps(value),))
    with pytest.raises(ValueError, match="result does not match"):
        MemoryDualWriteReceiptStore(path).latest_rollback_rehearsal()


def test_readiness_runtime_does_not_create_missing_journal(tmp_path):
    path = tmp_path / "missing" / "receipts.sqlite3"
    result = read_memory_authority_readiness(
        environ={"VERA_MEMORY_DUAL_WRITE_RECEIPTS": str(path)},
        assessed_at=NOW, min_receipts=1)
    assert path.exists() is False
    assert set(result["reasons"]) == {
        "insufficient_receipts", "evidence_time_missing",
        "rollback_rehearsal_missing", "insufficient_producer_coverage",
    }
    assert result["ready_for_activation_review"] is False
    assert result["authority_switch_allowed"] is False


def test_default_rehearsal_and_readiness_share_one_state_journal(
        tmp_path, monkeypatch):
    monkeypatch.setenv("VERA_STATE_DIR", str(tmp_path / "state"))
    rehearsal = asyncio.run(run_memory_rollback_rehearsal(
        rehearsal_id="shared-default", source_commit="abcdef12",
        completed_at="2026-01-01T23:30:00Z"))
    assert rehearsal["created"] is True

    result = read_memory_authority_readiness(
        assessed_at=NOW, expected_source_commit="abcdef12",
        min_receipts=1, max_evidence_age_seconds=7200)
    assert result["rollback_receipt_id"] == rehearsal["receipt"]["receipt_id"]
    assert set(result["reasons"]) == {
        "insufficient_receipts", "evidence_time_missing",
        "insufficient_producer_coverage"}


@pytest.mark.parametrize("changes,match", [
    ({"completed_at": "not-time"}, "RFC3339"),
    ({"canary_disabled": "yes"}, "must be boolean"),
])
def test_rollback_receipt_validation(changes, match):
    with pytest.raises((TypeError, ValueError), match=match):
        rollback(**changes)


def test_readiness_thresholds_are_bounded():
    evidence = {
        "total_receipts": 0, "returned_receipts": 0, "truncated": False,
        "unresolved_operations": 0, "pending_attempts": 0,
        "failed_attempts": 0, "latest_observed_at": "",
    }
    with pytest.raises(ValueError, match="min_receipts"):
        assess_memory_authority_readiness(
            evidence, assessed_at=NOW, rollback_receipt=None, min_receipts=0)
    with pytest.raises(ValueError, match="max_evidence_age_seconds"):
        assess_memory_authority_readiness(
            evidence, assessed_at=NOW, rollback_receipt=None,
            max_evidence_age_seconds=1)


def test_evidence_shape_and_runtime_source_identity_fail_closed():
    evidence = {
        "total_receipts": 1, "returned_receipts": 1, "truncated": False,
        "unresolved_operations": 0, "pending_attempts": 0,
        "failed_attempts": 0, "latest_observed_at": "2026-01-01T23:00:00Z",
        "producer_counts": {
            "public_capability": 1, "conversation": 1,
            "capability_activity": 1, "workflow": 1,
        },
    }
    result = assess_memory_authority_readiness(
        evidence, assessed_at=NOW, rollback_receipt=rollback(),
        min_receipts=1, max_evidence_age_seconds=7200)
    assert result.reasons == ("runtime_source_identity_missing",)
    with pytest.raises(ValueError, match="truncated flag"):
        assess_memory_authority_readiness(
            {**evidence, "truncated": True}, assessed_at=NOW,
            rollback_receipt=None, min_receipts=1)
    with pytest.raises(ValueError, match="non-negative integers"):
        assess_memory_authority_readiness(
            {**evidence, "pending_attempts": -1}, assessed_at=NOW,
            rollback_receipt=None, min_receipts=1)
