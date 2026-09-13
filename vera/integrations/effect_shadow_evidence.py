"""Bounded, payload-free evidence for observe-only external-effect decisions."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "vera.external-effect-shadow-evidence/v1"
_LOCK = threading.RLock()
READINESS_THRESHOLDS = {
    "minimum_observations": 100,
    "minimum_reads": 25,
    "minimum_mutations": 25,
    "minimum_admitted": 10,
    "minimum_denied": 10,
    "minimum_replay_suppressions": 1,
}
EVIDENCE_FAMILIES = frozenset({
    "integration_api", "telegram", "email", "commerce", "infrastructure"})


def evaluate_enforcement_readiness(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Assess evidence sufficiency; never authorize or enable enforcement."""
    totals = summary.get("totals") or {}
    classes = summary.get("classifications") or {}
    observations = int(totals.get("observations") or 0)
    admitted = int(totals.get("would_admit") or 0)
    denied = max(0, observations - admitted)
    reads = int(classes.get("read") or 0)
    mutations = sum(int(classes.get(name) or 0) for name in
                    ("idempotent_write", "non_idempotent_write"))
    suppressions = int(totals.get("would_suppress") or 0)
    values = {
        "minimum_observations": observations,
        "minimum_reads": reads,
        "minimum_mutations": mutations,
        "minimum_admitted": admitted,
        "minimum_denied": denied,
        "minimum_replay_suppressions": suppressions,
    }
    checks = {name: {"required": required, "observed": values[name],
                     "passed": values[name] >= required}
              for name, required in READINESS_THRESHOLDS.items()}
    unmet = sorted(name for name, check in checks.items() if not check["passed"])
    return {
        "schema": "vera.external-effect-enforcement-readiness/v1",
        "eligible_for_operator_review": not unmet,
        "enforcement_enabled": False,
        "decision": "ready_for_operator_review" if not unmet else "collect_more_evidence",
        "unmet_checks": unmet, "checks": checks,
        "meaning": "evidence_sufficiency_only_not_safety_or_authorization",
        "executes": False, "changes_policy": False, "retains_payload": False,
    }


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


class ExternalEffectShadowEvidence:
    """Append-only policy observations; never an execution or approval authority."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS external_effect_shadow_evidence ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL, "
                "plan_id TEXT NOT NULL, connection_sha256 TEXT NOT NULL, "
                "classification TEXT NOT NULL, method TEXT NOT NULL, "
                "would_admit INTEGER NOT NULL, would_execute INTEGER NOT NULL, "
                "would_suppress INTEGER NOT NULL, reasons_json TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def record(self, shadow: Mapping[str, Any], *, observed_at: str = "") -> None:
        decision = shadow.get("decision") or {}
        plan = shadow.get("plan") or {}
        replay = shadow.get("replay") or {}
        reasons = sorted({str(x)[:80] for x in decision.get("reasons", []) if str(x)})
        timestamp = observed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        connection = (plan.get("connection") or {}).get("id", "")
        values = (
            timestamp, str(plan.get("plan_id") or ""), _digest(str(connection)),
            str(plan.get("classification") or "unknown")[:80],
            str(plan.get("method") or "")[:16], bool(decision.get("would_admit")),
            bool(decision.get("would_execute")), bool(replay.get("would_suppress")),
            json.dumps(reasons, separators=(",", ":")),
        )
        with _LOCK, self._connect() as conn:
            conn.execute("INSERT INTO external_effect_shadow_evidence "
                         "(observed_at,plan_id,connection_sha256,classification,method,"
                         "would_admit,would_execute,would_suppress,reasons_json) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", values)
            conn.commit()

    def summary(self, *, limit: int = 50) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with _LOCK, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM external_effect_shadow_evidence ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) AS observations, SUM(would_admit) AS admitted, "
                "SUM(would_execute) AS executable, SUM(would_suppress) AS suppressed "
                "FROM external_effect_shadow_evidence").fetchone()
            classes = conn.execute(
                "SELECT classification, COUNT(*) AS observations FROM "
                "external_effect_shadow_evidence GROUP BY classification").fetchall()
        reason_counts: dict[str, int] = {}
        recent = []
        for row in rows:
            reasons = json.loads(row["reasons_json"])
            for reason in reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            recent.append({
                "observed_at": row["observed_at"], "plan_id": row["plan_id"],
                "connection_sha256": row["connection_sha256"],
                "classification": row["classification"], "method": row["method"],
                "would_admit": bool(row["would_admit"]),
                "would_execute": bool(row["would_execute"]),
                "would_suppress": bool(row["would_suppress"]), "reasons": reasons,
            })
        return {
            "schema": SCHEMA,
            "totals": {"observations": total["observations"],
                       "would_admit": total["admitted"] or 0,
                       "would_execute": total["executable"] or 0,
                       "would_suppress": total["suppressed"] or 0},
            "classifications": {row["classification"]: row["observations"]
                                for row in classes},
            "reasons_in_window": dict(sorted(reason_counts.items())),
            "recent": recent, "window": {"requested": limit, "returned": len(rows)},
            "enforcement": "observe_only", "executes": False, "retries": False,
            "retains_payload": False,
        }


@lru_cache(maxsize=8)
def default_external_effect_shadow_evidence(
        family: str = "integration_api") -> ExternalEffectShadowEvidence:
    from Vera.vera import state_paths
    if family not in EVIDENCE_FAMILIES:
        raise ValueError("external-effect evidence family is unsupported")
    filename = ("external-effect-shadow.sqlite3" if family == "integration_api" else
                f"external-effect-shadow-{family}.sqlite3")
    path = state_paths.state_dir("integrations") / filename
    state_paths.guard_out_of_tree(path)
    return ExternalEffectShadowEvidence(path)
