"""Revision-guarded operator intent for external-effect enforcement."""
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


SCHEMA = "vera.external-effect-enforcement-decision/v1"
DECISIONS = frozenset({"continue_observing", "approve_future_enforcement"})
_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_LOCK = threading.RLock()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DecisionConflict(ValueError):
    """The caller based a write on an obsolete decision revision."""


class ExternalEffectEnforcementDecisions:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS external_effect_enforcement_decisions ("
                "revision INTEGER PRIMARY KEY, decided_at TEXT NOT NULL, decision TEXT NOT NULL, "
                "actor_sha256 TEXT NOT NULL, approval_sha256 TEXT NOT NULL, "
                "readiness_json TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def _public(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {"schema": SCHEMA, "revision": 0,
                    "decision": "continue_observing", "requested_mode": "observe_only",
                    "effective_mode": "observe_only", "decided_at": "",
                    "actor_sha256": "", "approval_sha256": "",
                    "readiness": {}, "enforcement_enabled": False}
        return {"schema": SCHEMA, "revision": row["revision"],
                "decision": row["decision"],
                "requested_mode": ("enforce" if row["decision"] ==
                                   "approve_future_enforcement" else "observe_only"),
                "effective_mode": "observe_only", "decided_at": row["decided_at"],
                "actor_sha256": row["actor_sha256"],
                "approval_sha256": row["approval_sha256"],
                "readiness": json.loads(row["readiness_json"]),
                "enforcement_enabled": False}

    def current(self, *, history_limit: int = 20) -> dict[str, Any]:
        if isinstance(history_limit, bool) or not isinstance(history_limit, int) \
                or not 1 <= history_limit <= 100:
            raise ValueError("history_limit must be between 1 and 100")
        with _LOCK, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM external_effect_enforcement_decisions "
                "ORDER BY revision DESC LIMIT ?", (history_limit,)).fetchall()
        current = self._public(rows[0] if rows else None)
        current["history"] = [self._public(row) for row in rows]
        current["window"] = {"requested": history_limit, "returned": len(rows)}
        current.update({"executes": False, "changes_runtime_policy": False,
                        "retains_payload": False})
        return current

    def decide(self, *, decision: str, expected_revision: int, actor_ref: str,
               approval_receipt_ref: str = "", readiness: Mapping[str, Any]) -> dict[str, Any]:
        if decision not in DECISIONS:
            raise ValueError("decision is unsupported")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) \
                or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if not _OPAQUE.fullmatch(str(actor_ref or "")):
            raise ValueError("actor_ref must be an opaque identifier")
        eligible = readiness.get("eligible_for_operator_review") is True
        if decision == "approve_future_enforcement":
            if not eligible:
                raise ValueError("evidence is not ready for operator review")
            if not _OPAQUE.fullmatch(str(approval_receipt_ref or "")):
                raise ValueError("approval_receipt_ref must be an opaque identifier")
        snapshot = {"schema": str(readiness.get("schema") or ""),
                    "eligible_for_operator_review": eligible,
                    "decision": str(readiness.get("decision") or "")[:80],
                    "unmet_checks": sorted(str(x)[:80] for x in
                                           readiness.get("unmet_checks", []))}
        with _LOCK, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT revision FROM external_effect_enforcement_decisions "
                "ORDER BY revision DESC LIMIT 1").fetchone()
            actual = row["revision"] if row else 0
            if actual != expected_revision:
                conn.rollback()
                raise DecisionConflict(f"decision revision changed: expected {expected_revision}, actual {actual}")
            revision = actual + 1
            conn.execute(
                "INSERT INTO external_effect_enforcement_decisions VALUES (?,?,?,?,?,?)",
                (revision, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                 decision, _digest(actor_ref),
                 _digest(approval_receipt_ref) if approval_receipt_ref else "",
                 json.dumps(snapshot, sort_keys=True, separators=(",", ":"))))
            conn.commit()
        return self.current()


@lru_cache(maxsize=1)
def default_external_effect_enforcement_decisions() -> ExternalEffectEnforcementDecisions:
    from Vera.vera import state_paths
    path = state_paths.state_dir("integrations") / "external-effect-enforcement-decisions.sqlite3"
    state_paths.guard_out_of_tree(path)
    return ExternalEffectEnforcementDecisions(path)
