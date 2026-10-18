"""Fail-closed readiness evidence for Memory authority review.

Readiness is deliberately not activation.  The evaluator consumes bounded,
payload-free journal evidence and an exact rollback rehearsal receipt.  Even a
passing assessment only permits a human activation review; it never grants or
moves read/write authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping


REQUIRED_PRODUCER_KINDS = (
    "public_capability", "conversation", "capability_activity", "workflow",
)


SCHEMA = "vera.memory-authority-readiness/v1"
ROLLBACK_SCHEMA = "vera.memory-canary-rollback-rehearsal/v1"
_TOKEN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:@/-]{0,255}$")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _timestamp(value: str, field_name: str) -> datetime:
    value = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed


def _token(value: str, field_name: str) -> str:
    value = str(value or "").strip()
    if not _TOKEN.fullmatch(value):
        raise ValueError(f"{field_name} must be a bounded opaque token")
    return value


@dataclass(frozen=True, slots=True)
class MemoryRollbackRehearsalReceipt:
    rehearsal_id: str
    source_commit: str
    completed_at: str
    canary_disabled: bool
    native_write_available: bool
    revision_write_attempted: bool
    reconciliation_disabled: bool
    schema: str = ROLLBACK_SCHEMA
    receipt_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema != ROLLBACK_SCHEMA:
            raise ValueError("unsupported rollback rehearsal schema")
        rehearsal_id = _token(self.rehearsal_id, "rehearsal_id")
        source_commit = _token(self.source_commit, "source_commit")
        completed_at = _timestamp(self.completed_at, "completed_at").isoformat()
        for name in (
            "canary_disabled", "native_write_available",
            "revision_write_attempted", "reconciliation_disabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")
        object.__setattr__(self, "rehearsal_id", rehearsal_id)
        object.__setattr__(self, "source_commit", source_commit)
        object.__setattr__(self, "completed_at", completed_at)
        digest = hashlib.sha256(_canonical(self.identity_dict()).encode()).hexdigest()
        object.__setattr__(self, "receipt_id", f"mrr_{digest}")

    @property
    def passed(self) -> bool:
        return (
            self.canary_disabled
            and self.native_write_available
            and not self.revision_write_attempted
            and self.reconciliation_disabled
        )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "rehearsal_id": self.rehearsal_id,
            "source_commit": self.source_commit,
            "completed_at": self.completed_at,
            "canary_disabled": self.canary_disabled,
            "native_write_available": self.native_write_available,
            "revision_write_attempted": self.revision_write_attempted,
            "reconciliation_disabled": self.reconciliation_disabled,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"receipt_id": self.receipt_id, **self.identity_dict(), "passed": self.passed}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MemoryRollbackRehearsalReceipt":
        supplied = dict(value)
        receipt_id = str(supplied.pop("receipt_id", ""))
        passed = supplied.pop("passed", None)
        receipt = cls(**supplied)
        if receipt.receipt_id != receipt_id:
            raise ValueError("rollback rehearsal receipt identity does not match")
        if passed is not receipt.passed:
            raise ValueError("rollback rehearsal result does not match evidence")
        return receipt


@dataclass(frozen=True, slots=True)
class MemoryAuthorityReadiness:
    assessed_at: str
    reasons: tuple[str, ...]
    metrics: Mapping[str, int | bool | str]
    rollback_receipt_id: str
    ready_for_activation_review: bool
    schema: str = SCHEMA
    authority_switch_allowed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "assessed_at": self.assessed_at,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
            "rollback_receipt_id": self.rollback_receipt_id,
            "ready_for_activation_review": self.ready_for_activation_review,
            "authority_switch_allowed": False,
        }


def assess_memory_authority_readiness(
    evidence: Mapping[str, Any], *, assessed_at: str,
    rollback_receipt: MemoryRollbackRehearsalReceipt | None,
    expected_source_commit: str = "",
    min_receipts: int = 25, max_evidence_age_seconds: int = 86_400,
) -> MemoryAuthorityReadiness:
    """Classify bounded evidence without activating or executing anything."""
    now = _timestamp(assessed_at, "assessed_at")
    if isinstance(min_receipts, bool) or not 1 <= int(min_receipts) <= 1000:
        raise ValueError("min_receipts must be between 1 and 1000")
    if (isinstance(max_evidence_age_seconds, bool)
            or not 60 <= int(max_evidence_age_seconds) <= 31_536_000):
        raise ValueError("max_evidence_age_seconds must be between 60 and 31536000")

    required = {
        "total_receipts", "returned_receipts", "truncated",
        "unresolved_operations", "pending_attempts", "failed_attempts",
        "latest_observed_at",
    }
    if not required.issubset(evidence):
        raise ValueError("readiness evidence is incomplete")
    count_fields = (
        "total_receipts", "returned_receipts", "unresolved_operations",
        "pending_attempts", "failed_attempts",
    )
    if any(isinstance(evidence[name], bool) or not isinstance(evidence[name], int)
           or evidence[name] < 0 for name in count_fields):
        raise ValueError("readiness evidence counts must be non-negative integers")
    if not isinstance(evidence["truncated"], bool):
        raise ValueError("readiness evidence truncated must be boolean")
    if evidence["returned_receipts"] > evidence["total_receipts"]:
        raise ValueError("returned receipts cannot exceed total receipts")
    if evidence["truncated"] != (
            evidence["total_receipts"] > evidence["returned_receipts"]):
        raise ValueError("truncated flag does not match the evidence window")
    metrics = {
        "total_receipts": evidence["total_receipts"],
        "returned_receipts": evidence["returned_receipts"],
        "truncated": evidence["truncated"],
        "unresolved_operations": evidence["unresolved_operations"],
        "pending_attempts": evidence["pending_attempts"],
        "failed_attempts": evidence["failed_attempts"],
        "latest_observed_at": str(evidence["latest_observed_at"] or ""),
        "producer_counts": {
            str(key): int(value)
            for key, value in dict(evidence.get("producer_counts") or {}).items()
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        },
    }
    reasons: list[str] = []
    if metrics["returned_receipts"] < int(min_receipts):
        reasons.append("insufficient_receipts")
    if metrics["unresolved_operations"]:
        reasons.append("unresolved_operations")
    if metrics["pending_attempts"]:
        reasons.append("pending_attempts")
    if metrics["failed_attempts"]:
        reasons.append("failed_attempts")
    missing_producers = [
        kind for kind in REQUIRED_PRODUCER_KINDS
        if metrics["producer_counts"].get(kind, 0) < 1
    ]
    metrics["required_producer_kinds"] = list(REQUIRED_PRODUCER_KINDS)
    metrics["missing_producer_kinds"] = missing_producers
    if missing_producers:
        reasons.append("insufficient_producer_coverage")
    latest = metrics["latest_observed_at"]
    if not latest:
        reasons.append("evidence_time_missing")
    else:
        age = (now - _timestamp(str(latest), "latest_observed_at")).total_seconds()
        if age < 0:
            reasons.append("evidence_from_future")
        elif age > int(max_evidence_age_seconds):
            reasons.append("evidence_stale")
    if rollback_receipt is None:
        reasons.append("rollback_rehearsal_missing")
        rollback_id = ""
    else:
        rollback_id = rollback_receipt.receipt_id
        rollback_age = (now - _timestamp(
            rollback_receipt.completed_at, "rollback.completed_at")).total_seconds()
        if not rollback_receipt.passed:
            reasons.append("rollback_rehearsal_failed")
        expected_commit = str(expected_source_commit or "").strip()
        if not expected_commit:
            reasons.append("runtime_source_identity_missing")
        elif rollback_receipt.source_commit != expected_commit:
            reasons.append("rollback_rehearsal_source_mismatch")
        if rollback_age < 0:
            reasons.append("rollback_rehearsal_from_future")
        elif rollback_age > int(max_evidence_age_seconds):
            reasons.append("rollback_rehearsal_stale")
    unique = tuple(dict.fromkeys(reasons))
    return MemoryAuthorityReadiness(
        assessed_at=now.isoformat(), reasons=unique, metrics=metrics,
        rollback_receipt_id=rollback_id,
        ready_for_activation_review=not unique,
    )
