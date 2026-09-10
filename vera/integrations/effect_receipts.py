"""Durable, payload-free receipts for external-effect attempts."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from .external_effects import validate_external_effect_plan


SCHEMA = "vera.external-effect-receipt/v1"
EVENT_TYPE = "external.effect.receipt.recorded"
OUTCOMES = frozenset({"succeeded", "failed", "cancelled", "unknown"})
_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INIT_LOCK = threading.RLock()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


def _utc(value: str) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at must include a timezone offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class EffectReceiptConflict(ValueError):
    """The same attempt identity was replayed with different evidence."""


class ExternalEffectReceiptLedger:
    """Atomic attempt ledger; it never performs or retries an external effect."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with _INIT_LOCK, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS external_effect_receipts ("
                "receipt_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, "
                "identity_json TEXT NOT NULL, evidence_json TEXT NOT NULL, "
                "observation_count INTEGER NOT NULL CHECK(observation_count > 0), "
                "first_observed_at TEXT NOT NULL, latest_observed_at TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS external_effect_plan_idx "
                         "ON external_effect_receipts(plan_id, latest_observed_at)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        identity = json.loads(row["identity_json"])
        evidence = json.loads(row["evidence_json"])
        return {
            "type": EVENT_TYPE, "schema": SCHEMA,
            "receipt_id": row["receipt_id"], "plan_id": row["plan_id"],
            **identity, **evidence,
            "observation_count": row["observation_count"],
            "classification": "first_seen" if row["observation_count"] == 1 else "duplicate",
            "first_observed_at": row["first_observed_at"],
            "latest_observed_at": row["latest_observed_at"],
            "executes": False, "retries": False, "retains_payload": False,
        }

    def record(self, plan: Mapping[str, Any], *, attempt_id: str, outcome: str,
               observed_at: str, response_sha256: str = "",
               provider_receipt_ref: str = "") -> dict[str, Any]:
        plan = validate_external_effect_plan(plan)
        if not plan["admission"]["allowed"]:
            raise ValueError("denied plans cannot produce effect receipts")
        if not _OPAQUE.fullmatch(str(attempt_id or "")):
            raise ValueError("attempt_id must be an opaque identifier")
        if outcome not in OUTCOMES:
            raise ValueError("outcome is unsupported")
        if response_sha256 and not _SHA256.fullmatch(response_sha256):
            raise ValueError("response_sha256 is invalid")
        if provider_receipt_ref and not _OPAQUE.fullmatch(provider_receipt_ref):
            raise ValueError("provider_receipt_ref must be an opaque identifier")
        observed_at = _utc(observed_at)
        attempt_digest = _digest(attempt_id)
        receipt_id = _digest(f"{plan['plan_id']}:{attempt_digest}")
        identity = {
            "attempt_id_sha256": attempt_digest,
            "connection": plan["connection"], "operation": plan["operation"],
            "method": plan["method"], "effect_classification": plan["classification"],
            "idempotency_key_sha256": plan["idempotency"]["key_sha256"],
        }
        evidence = {"outcome": outcome, "response_sha256": response_sha256,
                    "provider_receipt_ref_sha256": _digest(provider_receipt_ref)}
        identity_json = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        evidence_json = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM external_effect_receipts WHERE receipt_id=?",
                               (receipt_id,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO external_effect_receipts VALUES (?,?,?,?,?,?,?)",
                             (receipt_id, plan["plan_id"], identity_json, evidence_json,
                              1, observed_at, observed_at))
            else:
                if row["plan_id"] != plan["plan_id"] or row["identity_json"] != identity_json \
                        or row["evidence_json"] != evidence_json:
                    raise EffectReceiptConflict("effect attempt replay differs")
                latest = max(row["latest_observed_at"], observed_at)
                first = min(row["first_observed_at"], observed_at)
                conn.execute("UPDATE external_effect_receipts SET observation_count=?, "
                             "first_observed_at=?, latest_observed_at=? WHERE receipt_id=?",
                             (row["observation_count"] + 1, first, latest, receipt_id))
            stored = conn.execute("SELECT * FROM external_effect_receipts WHERE receipt_id=?",
                                  (receipt_id,)).fetchone()
            conn.commit()
        return self._public(stored)

    def replay_status(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        """Return whether a matching successful effect is already durable."""
        plan = validate_external_effect_plan(plan)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM external_effect_receipts WHERE plan_id=? "
                "ORDER BY latest_observed_at DESC, receipt_id ASC", (plan["plan_id"],),
            ).fetchall()
        receipts = [self._public(row) for row in rows]
        succeeded = next((item for item in receipts if item["outcome"] == "succeeded"), None)
        return {"schema": "vera.external-effect-replay-status/v1",
                "plan_id": plan["plan_id"], "attempts": len(receipts),
                "already_succeeded": succeeded is not None,
                "decision": "do_not_repeat" if succeeded else "no_success_receipt",
                "successful_receipt_id": succeeded["receipt_id"] if succeeded else "",
                "executes": False, "retries": False, "retains_payload": False}


@lru_cache(maxsize=1)
def default_external_effect_receipt_ledger() -> ExternalEffectReceiptLedger:
    from Vera.vera import state_paths

    path = state_paths.state_dir("integrations") / "external-effect-receipts.sqlite3"
    state_paths.guard_out_of_tree(path)
    return ExternalEffectReceiptLedger(path)
