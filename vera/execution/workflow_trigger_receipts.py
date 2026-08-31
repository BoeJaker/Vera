"""Durable, non-executing receipts for shared workflow-trigger evidence."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .workflow_trigger import (
    SCHEDULE_KINDS, SOURCE_KINDS, validate_workflow_trigger,
)


SCHEMA = "vera.workflow-trigger-receipt/v1"
EVENT_TYPE = "workflow.trigger.receipt.recorded"
CLASSIFICATIONS = frozenset({"first_seen", "duplicate"})
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_INIT_LOCK = threading.RLock()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _parse_zoned(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("receipt observation time must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: str) -> str:
    return _parse_zoned(value).isoformat().replace("+00:00", "Z")


def _identity(trigger: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "trigger_id": trigger["trigger_id"],
        "idempotency_key": trigger["idempotency_key"],
        "source": dict(trigger["source"]),
        "target": dict(trigger["target"]),
        "schedule": dict(trigger["schedule"]),
        "authority": dict(trigger["authority"]),
    }


def validate_workflow_trigger_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed public receipt contract."""
    if not isinstance(value, Mapping):
        raise TypeError("workflow trigger receipt must be an object")
    allowed = {
        "type", "schema", "trigger_id", "idempotency_key", "classification",
        "observation_count", "first_observed_at", "latest_observed_at",
        "source", "schedule", "authority", "executes",
    }
    if set(value) != allowed:
        raise ValueError("workflow trigger receipt fields do not match the schema")
    if value.get("type") != EVENT_TYPE or value.get("schema") != SCHEMA:
        raise ValueError("workflow trigger receipt schema is unsupported")
    if value.get("classification") not in CLASSIFICATIONS:
        raise ValueError("workflow trigger receipt classification is unsupported")
    count = value.get("observation_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("observation_count must be a positive integer")
    expected_class = "first_seen" if count == 1 else "duplicate"
    if value.get("classification") != expected_class:
        raise ValueError("classification does not match observation_count")
    first = value.get("first_observed_at")
    latest = value.get("latest_observed_at")
    if not isinstance(first, str) or not isinstance(latest, str):
        raise ValueError("receipt observation times must be strings")
    if _parse_zoned(latest) < _parse_zoned(first):
        raise ValueError("latest_observed_at precedes first_observed_at")
    trigger_id = value.get("trigger_id")
    if not isinstance(trigger_id, str) or not _SHA256.fullmatch(trigger_id):
        raise ValueError("trigger_id is invalid")
    if value.get("idempotency_key") != trigger_id:
        raise ValueError("idempotency_key must match trigger_id")
    source = value.get("source")
    schedule = value.get("schedule")
    authority = value.get("authority")
    if not isinstance(source, Mapping) or set(source) != {"kind", "id", "revision"}:
        raise ValueError("receipt source fields do not match the schema")
    if source.get("kind") not in SOURCE_KINDS:
        raise ValueError("receipt source kind is unsupported")
    if any(not isinstance(source.get(field), str) or not source[field].strip()
           for field in ("id", "revision")):
        raise ValueError("receipt source identity is invalid")
    if not isinstance(schedule, Mapping) or set(schedule) != {
            "kind", "timezone", "scheduled_for"}:
        raise ValueError("receipt schedule fields do not match the schema")
    if schedule.get("kind") not in SCHEDULE_KINDS:
        raise ValueError("receipt schedule kind is unsupported")
    scheduled_for = schedule.get("scheduled_for")
    if not isinstance(schedule.get("timezone"), str) or not schedule["timezone"].strip():
        raise ValueError("receipt schedule timezone is invalid")
    try:
        ZoneInfo(schedule["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise ValueError("receipt schedule timezone is invalid") from exc
    if schedule["kind"] == "time" and not scheduled_for:
        raise ValueError("receipt time schedule requires scheduled_for")
    if schedule["kind"] != "time" and scheduled_for:
        raise ValueError("receipt non-time schedule cannot have scheduled_for")
    if scheduled_for:
        _parse_zoned(scheduled_for)
    if not isinstance(authority, Mapping) or set(authority) != {
            "scheduler", "execution", "projection"}:
        raise ValueError("receipt authority fields do not match the schema")
    if authority.get("execution") != "native" \
            or authority.get("projection") != "workflow_trigger":
        raise ValueError("receipt authority declaration is unsupported")
    if not isinstance(authority.get("scheduler"), str) \
            or not authority["scheduler"].strip():
        raise ValueError("receipt scheduler authority is invalid")
    if value.get("executes") is not False:
        raise ValueError("workflow trigger receipts cannot execute")
    return json.loads(_canonical(value))


class WorkflowTriggerReceiptConflict(ValueError):
    """Raised when a trigger identity is replayed with different semantics."""


class WorkflowTriggerReceiptLedger:
    """Atomic SQLite observation ledger; it neither schedules nor executes work."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with _INIT_LOCK, self._connect() as conn:
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workflow_trigger_receipts ("
                "trigger_id TEXT PRIMARY KEY, identity_json TEXT NOT NULL, "
                "observation_count INTEGER NOT NULL CHECK(observation_count > 0), "
                "first_observed_at TEXT NOT NULL, latest_observed_at TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def _receipt(row: sqlite3.Row, classification: str) -> dict[str, Any]:
        identity = json.loads(row["identity_json"])
        return validate_workflow_trigger_receipt({
            "type": EVENT_TYPE,
            "schema": SCHEMA,
            "trigger_id": identity["trigger_id"],
            "idempotency_key": identity["idempotency_key"],
            "classification": classification,
            "observation_count": row["observation_count"],
            "first_observed_at": row["first_observed_at"],
            "latest_observed_at": row["latest_observed_at"],
            "source": identity["source"],
            "schedule": identity["schedule"],
            "authority": identity["authority"],
            "executes": False,
        })

    def record(self, trigger: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically record an observation and classify it as first or duplicate."""
        trigger = validate_workflow_trigger(trigger)
        identity_json = _canonical(_identity(trigger))
        trigger_id = trigger["trigger_id"]
        observed_at = _utc_text(trigger["occurrence"]["observed_at"])
        observed_time = _parse_zoned(observed_at)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM workflow_trigger_receipts WHERE trigger_id=?",
                (trigger_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO workflow_trigger_receipts VALUES (?,?,?,?,?)",
                    (trigger_id, identity_json, 1, observed_at, observed_at),
                )
                classification = "first_seen"
            else:
                if row["identity_json"] != identity_json:
                    raise WorkflowTriggerReceiptConflict(
                        "workflow trigger identity replay differs")
                first_time = _parse_zoned(row["first_observed_at"])
                latest_time = _parse_zoned(row["latest_observed_at"])
                first = observed_at if observed_time < first_time else row["first_observed_at"]
                latest = observed_at if observed_time > latest_time else row["latest_observed_at"]
                conn.execute(
                    "UPDATE workflow_trigger_receipts SET observation_count=?, "
                    "first_observed_at=?, latest_observed_at=? WHERE trigger_id=?",
                    (row["observation_count"] + 1, first, latest, trigger_id),
                )
                classification = "duplicate"
            stored = conn.execute(
                "SELECT * FROM workflow_trigger_receipts WHERE trigger_id=?",
                (trigger_id,),
            ).fetchone()
            conn.commit()
        return self._receipt(stored, classification)

    def get(self, trigger_id: str) -> dict[str, Any] | None:
        """Return one receipt without exposing database internals or trigger content."""
        if not isinstance(trigger_id, str) or not _SHA256.fullmatch(trigger_id):
            raise ValueError("trigger_id is invalid")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_trigger_receipts WHERE trigger_id=?",
                (trigger_id,),
            ).fetchone()
        if row is None:
            return None
        classification = "first_seen" if row["observation_count"] == 1 else "duplicate"
        return self._receipt(row, classification)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return a bounded newest-observation view."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer between 1 and 200")
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_trigger_receipts "
                "ORDER BY latest_observed_at DESC, trigger_id ASC LIMIT ?", (limit,),
            ).fetchall()
        return [self._receipt(
            row, "first_seen" if row["observation_count"] == 1 else "duplicate",
        ) for row in rows]


@lru_cache(maxsize=1)
def default_workflow_trigger_receipt_ledger() -> WorkflowTriggerReceiptLedger:
    """Return the process-local handle to the shared out-of-tree receipt database."""
    from Vera.vera import state_paths

    path = state_paths.state_dir("execution") / "workflow-trigger-receipts.sqlite3"
    state_paths.guard_out_of_tree(path)
    return WorkflowTriggerReceiptLedger(path)
