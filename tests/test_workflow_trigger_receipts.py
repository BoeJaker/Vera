import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from vera.execution.workflow_trigger import build_workflow_trigger
from vera.execution.workflow_trigger_receipts import (
    EVENT_TYPE,
    SCHEMA,
    WorkflowTriggerReceiptConflict,
    WorkflowTriggerReceiptLedger,
    validate_workflow_trigger_receipt,
)


pytestmark = pytest.mark.critical


def _trigger(observed_at="2026-08-31T12:00:00+00:00"):
    return build_workflow_trigger(
        source_kind="calendar.action", source_id="action-1",
        source_revision="sha256:definition", target_ref="sched.action:action-1",
        schedule_kind="time", occurrence_key="one-shot",
        observed_at=observed_at, timezone_name="UTC",
        scheduled_for="2026-09-01T09:00:00+00:00",
        native_owner="vera.calendar.longterm_scheduler",
    )


def test_receipt_is_content_safe_non_executing_and_closed(tmp_path):
    ledger = WorkflowTriggerReceiptLedger(tmp_path / "receipts.sqlite3")
    receipt = ledger.record(_trigger())

    assert receipt["type"] == EVENT_TYPE
    assert receipt["schema"] == SCHEMA
    assert receipt["classification"] == "first_seen"
    assert receipt["observation_count"] == 1
    assert receipt["executes"] is False
    assert "target" not in receipt
    assert "occurrence" not in receipt
    with pytest.raises(ValueError, match="fields"):
        validate_workflow_trigger_receipt({**receipt, "payload": "private"})


def test_duplicate_classification_survives_reopen_and_tracks_time_bounds(tmp_path):
    path = tmp_path / "receipts.sqlite3"
    first = WorkflowTriggerReceiptLedger(path).record(
        _trigger("2026-08-31T12:05:00+00:00"))
    duplicate = WorkflowTriggerReceiptLedger(path).record(
        _trigger("2026-08-31T12:00:00+00:00"))
    latest = WorkflowTriggerReceiptLedger(path).record(
        _trigger("2026-08-31T13:00:00+01:00"))

    assert first["classification"] == "first_seen"
    assert duplicate["classification"] == "duplicate"
    assert latest["observation_count"] == 3
    assert latest["first_observed_at"] == "2026-08-31T12:00:00Z"
    # Equal instants do not rewrite the retained representation.
    assert latest["latest_observed_at"] == "2026-08-31T12:05:00Z"
    assert WorkflowTriggerReceiptLedger(path).get(latest["trigger_id"]) == latest


def test_concurrent_observations_are_counted_atomically(tmp_path):
    path = tmp_path / "receipts.sqlite3"

    def record(_index):
        return WorkflowTriggerReceiptLedger(path).record(_trigger())

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(record, range(24)))

    assert sum(item["classification"] == "first_seen" for item in receipts) == 1
    stored = WorkflowTriggerReceiptLedger(path).get(receipts[0]["trigger_id"])
    assert stored["observation_count"] == 24
    assert stored["classification"] == "duplicate"


def test_identity_conflict_rolls_back_without_increment(tmp_path):
    path = tmp_path / "receipts.sqlite3"
    ledger = WorkflowTriggerReceiptLedger(path)
    trigger = _trigger()
    first = ledger.record(trigger)
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT identity_json FROM workflow_trigger_receipts WHERE trigger_id=?",
            (trigger["trigger_id"],),
        ).fetchone()
        identity = json.loads(row[0])
        identity["source"]["revision"] = "sha256:tampered"
        conn.execute(
            "UPDATE workflow_trigger_receipts SET identity_json=? WHERE trigger_id=?",
            (json.dumps(identity, sort_keys=True, separators=(",", ":")),
             trigger["trigger_id"]),
        )

    with pytest.raises(WorkflowTriggerReceiptConflict, match="replay differs"):
        ledger.record(trigger)
    with sqlite3.connect(path) as conn:
        count = conn.execute(
            "SELECT observation_count FROM workflow_trigger_receipts WHERE trigger_id=?",
            (first["trigger_id"],),
        ).fetchone()[0]
    assert count == 1


def test_recent_is_bounded_and_rejects_invalid_limits(tmp_path):
    ledger = WorkflowTriggerReceiptLedger(tmp_path / "receipts.sqlite3")
    for index in range(3):
        trigger = build_workflow_trigger(
            source_kind="dream.trigger", source_id=f"dream-{index}",
            source_revision="sha256:definition",
            target_ref=f"dream.trigger:dream-{index}", schedule_kind="idle_interval",
            occurrence_key="initial",
            observed_at=f"2026-08-31T12:0{index}:00+00:00",
            native_owner="vera.dream.scheduler",
        )
        ledger.record(trigger)

    assert len(ledger.recent(2)) == 2
    with pytest.raises(ValueError, match="between 1 and 200"):
        ledger.recent(0)
    with pytest.raises(ValueError, match="between 1 and 200"):
        ledger.recent(True)


def test_validation_rejects_forged_count_class_and_time_order(tmp_path):
    receipt = WorkflowTriggerReceiptLedger(
        tmp_path / "receipts.sqlite3").record(_trigger())
    with pytest.raises(ValueError, match="classification"):
        validate_workflow_trigger_receipt({
            **receipt, "classification": "duplicate",
        })
    with pytest.raises(ValueError, match="precedes"):
        validate_workflow_trigger_receipt({
            **receipt,
            "first_observed_at": "2026-08-31T13:00:00+00:00",
            "latest_observed_at": "2026-08-31T12:00:00+00:00",
        })
