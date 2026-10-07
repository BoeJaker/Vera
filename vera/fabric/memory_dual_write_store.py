"""Durable, idempotent storage for payload-free Memory dual-write receipts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from .memory_dual_write_receipt import MemoryDualWriteReceipt


class MemoryReceiptConflict(ValueError):
    pass


class MemoryReceiptCorruption(RuntimeError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _verify(payload: dict[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    receipt_id = str(value.pop("receipt_id", ""))
    expected = "mdwr_" + hashlib.sha256(_canonical(value).encode()).hexdigest()
    if receipt_id != expected:
        raise MemoryReceiptCorruption("receipt identity does not match stored evidence")
    return {"receipt_id": receipt_id, **value}


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
                    observed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_mdwr_reconcile
                    ON memory_dual_write_receipts
                    (requires_reconciliation, observed_at, operation_id);
            """)

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
                "INSERT INTO memory_dual_write_receipts VALUES (?,?,?,?,?,?)",
                (receipt.operation_id, receipt.receipt_id, encoded,
                 receipt.authority_state, int(receipt.requires_reconciliation),
                 receipt.observed_at),
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

    def reconciliation(self, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = int(limit)
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT evidence_json FROM memory_dual_write_receipts "
                "WHERE requires_reconciliation=1 "
                "ORDER BY observed_at,operation_id LIMIT ?", (limit,),
            ).fetchall()
        return [_verify(json.loads(row["evidence_json"])) for row in rows]

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM memory_dual_write_receipts").fetchone()[0])
