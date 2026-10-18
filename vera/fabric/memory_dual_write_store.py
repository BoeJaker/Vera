"""Durable, idempotent storage for payload-free Memory dual-write receipts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
from datetime import datetime
from typing import Any

from .memory_dual_write_receipt import MemoryDualWriteReceipt
from .memory_dual_write_readiness import MemoryRollbackRehearsalReceipt


class MemoryReceiptConflict(ValueError):
    pass


class MemoryReceiptCorruption(RuntimeError):
    pass


REPAIR_TARGETS = ("revision", "postgres", "chroma", "neo4j")
REPAIR_OUTCOMES = frozenset({"committed", "failed", "timed_out", "cancelled", "skipped"})
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _verify(payload: dict[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    receipt_id = str(value.pop("receipt_id", ""))
    expected = "mdwr_" + hashlib.sha256(_canonical(value).encode()).hexdigest()
    if receipt_id != expected:
        raise MemoryReceiptCorruption("receipt identity does not match stored evidence")
    return {"receipt_id": receipt_id, **value}


def _timestamp(value: str, field: str) -> str:
    value = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.isoformat()


def _token(value: str, field: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 256 or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
            for char in value):
        raise ValueError(f"{field} must be a bounded opaque token")
    return value


class MemoryDualWriteReceiptStore:
    """SQLite evidence journal; it is not a record or projection authority."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS memory_dual_write_receipts (
                    operation_id TEXT PRIMARY KEY,
                    receipt_id TEXT NOT NULL UNIQUE,
                    evidence_json TEXT NOT NULL,
                    authority_state TEXT NOT NULL,
                    requires_reconciliation INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    producer_kind TEXT NOT NULL DEFAULT 'public_capability'
                );
                CREATE INDEX IF NOT EXISTS idx_mdwr_reconcile
                    ON memory_dual_write_receipts
                    (requires_reconciliation, observed_at, operation_id);
                CREATE TABLE IF NOT EXISTS memory_dual_write_reconciliation_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    source_receipt_id TEXT NOT NULL,
                    targets_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('started','completed')),
                    outcomes_json TEXT NOT NULL,
                    error_codes_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES memory_dual_write_receipts(operation_id)
                );
                CREATE INDEX IF NOT EXISTS idx_mdwr_attempt_state
                    ON memory_dual_write_reconciliation_attempts
                    (state, started_at, attempt_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_mdwr_one_started_attempt
                    ON memory_dual_write_reconciliation_attempts (operation_id)
                    WHERE state='started';
                CREATE TABLE IF NOT EXISTS memory_dual_write_reconciliation_resolutions (
                    operation_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    resolved_at TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES memory_dual_write_receipts(operation_id)
                );
                CREATE TABLE IF NOT EXISTS memory_canary_rollback_rehearsals (
                    rehearsal_id TEXT PRIMARY KEY,
                    receipt_id TEXT NOT NULL UNIQUE,
                    evidence_json TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );
            """)
            columns = {
                str(row[1]) for row in conn.execute(
                    "PRAGMA table_info(memory_dual_write_receipts)").fetchall()
            }
            if "producer_kind" not in columns:
                # Existing v1 receipts were all produced by the only seam that
                # existed at the time: the public memory.store capability.
                conn.execute(
                    "ALTER TABLE memory_dual_write_receipts ADD COLUMN "
                    "producer_kind TEXT NOT NULL DEFAULT 'public_capability'")

    def append(self, receipt: MemoryDualWriteReceipt) -> dict[str, Any]:
        if not isinstance(receipt, MemoryDualWriteReceipt):
            raise TypeError("receipt must be a MemoryDualWriteReceipt")
        payload = receipt.to_dict()
        encoded = _canonical(payload)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT receipt_id,evidence_json FROM memory_dual_write_receipts "
                "WHERE operation_id=?", (receipt.operation_id,)).fetchone()
            if existing:
                if existing["receipt_id"] != receipt.receipt_id or existing["evidence_json"] != encoded:
                    raise MemoryReceiptConflict("operation identity replay differs")
                conn.commit()
                return {"created": False, "receipt": _verify(json.loads(encoded))}
            conn.execute(
                "INSERT INTO memory_dual_write_receipts "
                "(operation_id,receipt_id,evidence_json,authority_state,"
                "requires_reconciliation,observed_at,producer_kind) "
                "VALUES (?,?,?,?,?,?,?)",
                (receipt.operation_id, receipt.receipt_id, encoded,
                 receipt.authority_state, int(receipt.requires_reconciliation),
                 receipt.observed_at, receipt.producer_kind),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"created": True, "receipt": _verify(json.loads(encoded))}

    def get(self, operation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT evidence_json FROM memory_dual_write_receipts WHERE operation_id=?",
                (str(operation_id),),
            ).fetchone()
        return _verify(json.loads(row["evidence_json"])) if row else None

    def _attempt_value(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "attempt_id": row["attempt_id"],
            "operation_id": row["operation_id"],
            "source_receipt_id": row["source_receipt_id"],
            "targets": json.loads(row["targets_json"]),
            "state": row["state"],
            "outcomes": json.loads(row["outcomes_json"]),
            "error_codes": json.loads(row["error_codes_json"]),
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }

    def attempt(self, attempt_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM memory_dual_write_reconciliation_attempts "
                "WHERE attempt_id=?", (str(attempt_id),)).fetchone()
        return self._attempt_value(row) if row else None

    def reconciliation_status(self, operation_id: str) -> dict[str, Any]:
        receipt = self.get(operation_id)
        if receipt is None:
            raise KeyError("dual-write receipt not found")
        effective = {"revision": receipt["revision_outcome"],
                     **dict(receipt["native_outcomes"])}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_dual_write_reconciliation_attempts "
                "WHERE operation_id=? AND state='completed' "
                "ORDER BY started_at,attempt_id", (str(operation_id),)).fetchall()
        attempts = [self._attempt_value(row) for row in rows]
        for attempt in attempts:
            for target, outcome in attempt["outcomes"].items():
                if outcome == "committed":
                    effective[target] = "committed"
        unresolved = []
        if effective["revision"] != "committed":
            unresolved.append("revision")
        for backend in REPAIR_TARGETS[1:]:
            if effective[backend] in {"failed", "timed_out", "cancelled"}:
                unresolved.append(backend)
        return {
            "operation_id": receipt["operation_id"],
            "source_receipt_id": receipt["receipt_id"],
            "effective_outcomes": effective,
            "unresolved_targets": unresolved,
            "converged": not unresolved,
            "authority_switch_allowed": False,
            "completed_attempts": len(attempts),
        }

    def start_reconciliation(
        self, *, operation_id: str, attempt_id: str, targets: list[str],
        started_at: str,
    ) -> dict[str, Any]:
        attempt_id = _token(attempt_id, "attempt_id")
        started_at = _timestamp(started_at, "started_at")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM memory_dual_write_reconciliation_attempts "
                "WHERE attempt_id=?", (attempt_id,)).fetchone()
            if existing:
                value = self._attempt_value(existing)
                if (value["operation_id"] != operation_id
                        or value["targets"] != list(targets)
                        or value["started_at"] != started_at):
                    raise MemoryReceiptConflict("reconciliation attempt replay differs")
                conn.commit()
                return value
            # Re-read after obtaining the write reservation so a completion by
            # another process cannot leave this attempt with stale targets.
            status = self.reconciliation_status(operation_id)
            expected = list(status["unresolved_targets"])
            if not expected:
                raise ValueError("receipt has no unresolved reconciliation targets")
            if list(targets) != expected:
                raise ValueError(
                    "targets must exactly match unresolved reconciliation state")
            encoded_targets = _canonical(expected)
            conn.execute(
                "INSERT INTO memory_dual_write_reconciliation_attempts "
                "VALUES (?,?,?,?,?,'{}','{}',?,'')",
                (attempt_id, operation_id, status["source_receipt_id"],
                 encoded_targets, "started", started_at))
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise MemoryReceiptConflict(
                "another reconciliation attempt already owns this operation") from exc
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.attempt(attempt_id)

    def complete_reconciliation(
        self, *, attempt_id: str, outcomes: dict[str, str],
        error_codes: dict[str, str], completed_at: str,
    ) -> dict[str, Any]:
        completed_at = _timestamp(completed_at, "completed_at")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM memory_dual_write_reconciliation_attempts "
                "WHERE attempt_id=?", (str(attempt_id),)).fetchone()
            if not row:
                raise KeyError("reconciliation attempt not found")
            value = self._attempt_value(row)
            started = datetime.fromisoformat(value["started_at"])
            completed = datetime.fromisoformat(completed_at)
            if completed < started:
                raise ValueError("completed_at cannot precede started_at")
            if set(outcomes) != set(value["targets"]):
                raise ValueError("outcomes must cover every reconciliation target")
            if any(outcome not in REPAIR_OUTCOMES for outcome in outcomes.values()):
                raise ValueError("unsupported reconciliation outcome")
            failed = {target for target, outcome in outcomes.items()
                      if outcome in {"failed", "timed_out", "cancelled"}}
            if set(error_codes) != failed or any(
                    not _ERROR_CODE.fullmatch(str(code or ""))
                    for code in error_codes.values()):
                raise ValueError("failed outcomes require bounded error classifications")
            encoded_outcomes = _canonical(outcomes)
            encoded_errors = _canonical(error_codes)
            if value["state"] == "completed":
                if (value["outcomes"] != outcomes
                        or value["error_codes"] != error_codes
                        or value["completed_at"] != completed_at):
                    raise MemoryReceiptConflict("reconciliation completion replay differs")
                conn.commit()
                return value
            conn.execute(
                "UPDATE memory_dual_write_reconciliation_attempts SET "
                "state='completed',outcomes_json=?,error_codes_json=?,completed_at=? "
                "WHERE attempt_id=?",
                (encoded_outcomes, encoded_errors, completed_at, attempt_id))
            receipt_row = conn.execute(
                "SELECT evidence_json FROM memory_dual_write_receipts "
                "WHERE operation_id=?", (value["operation_id"],)).fetchone()
            receipt = _verify(json.loads(receipt_row["evidence_json"]))
            effective = {"revision": receipt["revision_outcome"],
                         **dict(receipt["native_outcomes"])}
            attempts = conn.execute(
                "SELECT outcomes_json FROM memory_dual_write_reconciliation_attempts "
                "WHERE operation_id=? AND state='completed'",
                (value["operation_id"],)).fetchall()
            for completed_row in attempts:
                for target, outcome in json.loads(completed_row["outcomes_json"]).items():
                    if outcome == "committed":
                        effective[target] = "committed"
            unresolved = effective["revision"] != "committed" or any(
                effective[target] in {"failed", "timed_out", "cancelled"}
                for target in REPAIR_TARGETS[1:])
            if not unresolved:
                conn.execute(
                    "INSERT OR IGNORE INTO memory_dual_write_reconciliation_resolutions "
                    "VALUES (?,?,?)",
                    (value["operation_id"], attempt_id, completed_at))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.attempt(attempt_id)

    def pending_reconciliations(self, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = int(limit)
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_dual_write_reconciliation_attempts "
                "WHERE state='started' ORDER BY started_at,attempt_id LIMIT ?",
                (limit,)).fetchall()
        return [self._attempt_value(row) for row in rows]

    def reconciliation(self, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = int(limit)
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT evidence_json FROM memory_dual_write_receipts "
                "WHERE requires_reconciliation=1 AND NOT EXISTS ("
                "SELECT 1 FROM memory_dual_write_reconciliation_resolutions r "
                "WHERE r.operation_id=memory_dual_write_receipts.operation_id) "
                "ORDER BY observed_at,operation_id LIMIT ?", (limit,),
            ).fetchall()
        return [_verify(json.loads(row["evidence_json"])) for row in rows]

    def append_rollback_rehearsal(
        self, receipt: MemoryRollbackRehearsalReceipt,
    ) -> dict[str, Any]:
        """Persist one payload-free rollback proof with replay protection."""
        if not isinstance(receipt, MemoryRollbackRehearsalReceipt):
            raise TypeError("receipt must be a MemoryRollbackRehearsalReceipt")
        payload = receipt.to_dict()
        encoded = _canonical(payload)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT receipt_id,evidence_json FROM memory_canary_rollback_rehearsals "
                "WHERE rehearsal_id=?", (receipt.rehearsal_id,)).fetchone()
            if row:
                if row["receipt_id"] != receipt.receipt_id or row["evidence_json"] != encoded:
                    raise MemoryReceiptConflict("rollback rehearsal replay differs")
                conn.commit()
                return {"created": False, "receipt": payload}
            conn.execute(
                "INSERT INTO memory_canary_rollback_rehearsals VALUES (?,?,?,?)",
                (receipt.rehearsal_id, receipt.receipt_id, encoded,
                 receipt.completed_at))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"created": True, "receipt": payload}

    def latest_rollback_rehearsal(self) -> MemoryRollbackRehearsalReceipt | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT evidence_json FROM memory_canary_rollback_rehearsals "
                "ORDER BY completed_at DESC,rehearsal_id DESC LIMIT 1").fetchone()
        if not row:
            return None
        return MemoryRollbackRehearsalReceipt.from_dict(json.loads(row["evidence_json"]))

    def rollback_rehearsal(
        self, rehearsal_id: str,
    ) -> MemoryRollbackRehearsalReceipt | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT evidence_json FROM memory_canary_rollback_rehearsals "
                "WHERE rehearsal_id=?", (str(rehearsal_id),)).fetchone()
        if not row:
            return None
        return MemoryRollbackRehearsalReceipt.from_dict(json.loads(row["evidence_json"]))

    def readiness_evidence(self, *, limit: int = 100) -> dict[str, Any]:
        """Return bounded metadata only; never expose stored receipt payloads."""
        limit = int(limit)
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as conn:
            total = int(conn.execute(
                "SELECT COUNT(*) FROM memory_dual_write_receipts").fetchone()[0])
            rows = conn.execute(
                "SELECT operation_id,observed_at FROM memory_dual_write_receipts "
                "ORDER BY observed_at DESC,operation_id DESC LIMIT ?", (limit,)
            ).fetchall()
            unresolved = int(conn.execute(
                "SELECT COUNT(*) FROM memory_dual_write_receipts d "
                "WHERE d.requires_reconciliation=1 AND NOT EXISTS ("
                "SELECT 1 FROM memory_dual_write_reconciliation_resolutions r "
                "WHERE r.operation_id=d.operation_id)").fetchone()[0])
            pending = int(conn.execute(
                "SELECT COUNT(*) FROM memory_dual_write_reconciliation_attempts "
                "WHERE state='started'").fetchone()[0])
            completed = conn.execute(
                "SELECT a.outcomes_json FROM memory_dual_write_reconciliation_attempts a "
                "WHERE a.state='completed' AND NOT EXISTS ("
                "SELECT 1 FROM memory_dual_write_reconciliation_resolutions r "
                "WHERE r.operation_id=a.operation_id)").fetchall()
            producer_rows = conn.execute(
                "SELECT producer_kind,COUNT(*) AS n "
                "FROM memory_dual_write_receipts GROUP BY producer_kind "
                "ORDER BY producer_kind").fetchall()
        failed_attempts = sum(
            any(outcome != "committed" for outcome in json.loads(row["outcomes_json"]).values())
            for row in completed
        )
        return {
            "total_receipts": total,
            "returned_receipts": len(rows),
            "truncated": total > len(rows),
            "unresolved_operations": unresolved,
            "pending_attempts": pending,
            "failed_attempts": failed_attempts,
            "latest_observed_at": rows[0]["observed_at"] if rows else "",
            "producer_counts": {
                str(row["producer_kind"]): int(row["n"])
                for row in producer_rows
            },
            "retains_payload": False,
            "executes": False,
            "authority_switch_allowed": False,
        }

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM memory_dual_write_receipts").fetchone()[0])
