"""Explicit, contract-bound activation for generic Integration API enforcement."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "vera.external-effect-enforcement-activation/v1"
CONTRACT = "integration.api.call:external-effect-enforcement/v1"
CONTRACT_SHA256 = hashlib.sha256(CONTRACT.encode()).hexdigest()
ACTIONS = frozenset({"activate", "deactivate"})
_LOCK = threading.RLock()
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{2,255}")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class ActivationConflict(ValueError):
    pass


class ExternalEffectEnforcementActivations:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS external_effect_enforcement_activations ("
                "revision INTEGER PRIMARY KEY, activated_at TEXT NOT NULL, action TEXT NOT NULL, "
                "decision_revision INTEGER NOT NULL, contract_sha256 TEXT NOT NULL, "
                "actor_sha256 TEXT NOT NULL, receipt_sha256 TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def current(self, decision: Mapping[str, Any], *, runtime_gate: bool,
                history_limit: int = 20) -> dict[str, Any]:
        if isinstance(history_limit, bool) or not isinstance(history_limit, int) \
                or not 1 <= history_limit <= 100:
            raise ValueError("history_limit must be between 1 and 100")
        with _LOCK, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM external_effect_enforcement_activations "
                "ORDER BY revision DESC LIMIT ?", (history_limit,)).fetchall()
        latest = rows[0] if rows else None
        decision_matches = bool(
            latest and latest["decision_revision"] == decision.get("revision") and
            decision.get("decision") == "approve_future_enforcement")
        contract_matches = bool(latest and latest["contract_sha256"] == CONTRACT_SHA256)
        active = bool(latest and latest["action"] == "activate" and runtime_gate and
                      decision_matches and contract_matches)
        def public(row):
            return {"revision": row["revision"], "activated_at": row["activated_at"],
                    "action": row["action"], "decision_revision": row["decision_revision"],
                    "contract_sha256": row["contract_sha256"],
                    "actor_sha256": row["actor_sha256"],
                    "receipt_sha256": row["receipt_sha256"]}
        return {"schema": SCHEMA, "revision": latest["revision"] if latest else 0,
                "requested_active": bool(latest and latest["action"] == "activate"),
                "runtime_gate": bool(runtime_gate), "decision_matches": decision_matches,
                "contract_matches": contract_matches, "effective_mode": "enforce" if active else "observe_only",
                "enforcement_enabled": active,
                "history": [public(row) for row in rows],
                "window": {"requested": history_limit, "returned": len(rows)},
                "executes": False, "retries": False, "retains_payload": False}

    def apply(self, *, action: str, expected_revision: int,
              decision: Mapping[str, Any], actor_ref: str,
              activation_receipt_ref: str = "", runtime_gate: bool) -> dict[str, Any]:
        if action not in ACTIONS:
            raise ValueError("action is unsupported")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) \
                or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if not _OPAQUE.fullmatch(str(actor_ref or "")):
            raise ValueError("actor_ref must be an opaque identifier")
        if action == "activate":
            if not runtime_gate:
                raise ValueError("runtime enforcement gate is disabled")
            if decision.get("decision") != "approve_future_enforcement":
                raise ValueError("future enforcement is not approved")
            if not _OPAQUE.fullmatch(str(activation_receipt_ref or "")):
                raise ValueError("activation_receipt_ref must be an opaque identifier")
        with _LOCK, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT revision FROM external_effect_enforcement_activations "
                "ORDER BY revision DESC LIMIT 1").fetchone()
            actual = row["revision"] if row else 0
            if actual != expected_revision:
                conn.rollback()
                raise ActivationConflict(
                    f"activation revision changed: expected {expected_revision}, actual {actual}")
            revision = actual + 1
            conn.execute(
                "INSERT INTO external_effect_enforcement_activations VALUES (?,?,?,?,?,?,?)",
                (revision, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                 action, int(decision.get("revision") or 0), CONTRACT_SHA256,
                 _digest(actor_ref),
                 _digest(activation_receipt_ref) if activation_receipt_ref else ""))
            conn.commit()
        return self.current(decision, runtime_gate=runtime_gate)


@lru_cache(maxsize=1)
def default_external_effect_enforcement_activations() -> ExternalEffectEnforcementActivations:
    from Vera.vera import state_paths
    path = state_paths.state_dir("integrations") / "external-effect-enforcement-activations.sqlite3"
    state_paths.guard_out_of_tree(path)
    return ExternalEffectEnforcementActivations(path)
