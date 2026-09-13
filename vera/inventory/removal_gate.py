"""Fail-closed, non-executing gate for one compatibility-path removal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

SCHEMA = "vera.compatibility-removal-gate/v1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
REQUIRED_EVIDENCE = frozenset({
    "semantic_diff", "caller_inventory", "state_inventory",
    "configuration_inventory", "success_fixture", "error_fixture",
    "timeout_fixture", "cancel_fixture", "restart_fixture",
    "recovery_fixture", "shadow_assessment", "conformance",
    "quality_reliability", "state_export", "rollback_rehearsal",
    "documentation_migration", "explicit_approval",
})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _identifier(value: Any, label: str) -> str:
    value = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def _instant(value: Any, label: str) -> str:
    value = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value


@dataclass(frozen=True, slots=True)
class ZeroUseCycle:
    cycle_id: str
    started_at: str
    ended_at: str
    non_probe_calls: int
    telemetry_complete: bool
    telemetry_digest: str
    normal_operation: bool = True
    cycle_evidence_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cycle_id", _identifier(self.cycle_id, "cycle ID"))
        start = _instant(self.started_at, "cycle start")
        end = _instant(self.ended_at, "cycle end")
        if datetime.fromisoformat(end.replace("Z", "+00:00")) <= datetime.fromisoformat(
                start.replace("Z", "+00:00")):
            raise ValueError("cycle end must follow cycle start")
        object.__setattr__(self, "started_at", start)
        object.__setattr__(self, "ended_at", end)
        if (isinstance(self.non_probe_calls, bool) or
                not isinstance(self.non_probe_calls, int) or self.non_probe_calls < 0):
            raise ValueError("non-probe call count must be a non-negative integer")
        if not isinstance(self.telemetry_complete, bool) or not isinstance(
                self.normal_operation, bool):
            raise ValueError("cycle flags must be boolean")
        digest = str(self.telemetry_digest or "").lower()
        if not _DIGEST.fullmatch(digest):
            raise ValueError("cycle telemetry must use a sha256 digest")
        object.__setattr__(self, "telemetry_digest", digest)
        object.__setattr__(self, "cycle_evidence_id", _identity(
            "zcycle_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id, "started_at": self.started_at,
            "ended_at": self.ended_at, "non_probe_calls": self.non_probe_calls,
            "telemetry_complete": self.telemetry_complete,
            "telemetry_digest": self.telemetry_digest,
            "normal_operation": self.normal_operation,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"cycle_evidence_id": self.cycle_evidence_id, **self.identity_dict()}


@dataclass(frozen=True, slots=True)
class RemovalGateSubmission:
    candidate_id: str
    candidate_name: str
    replacement: str
    owner: str
    review_id: str
    review_recommendation: str
    evidence_digests: Mapping[str, str]
    coverage_complete: bool
    telemetry_complete: bool
    unmigrated_stored_definitions: int
    zero_use_cycles: tuple[ZeroUseCycle, ...]
    shadow_disposition: str
    conformance_passed: bool
    quality_regression: bool
    reliability_regression: bool
    baseline_p95_ms: float
    candidate_p95_ms: float
    performance_exception_justified: bool
    performance_gain_digest: str
    state_export_checksum_verified: bool
    rollback_retained_for_release: bool
    documentation_migrated: bool
    explicitly_approved: bool
    approval_principal: str
    approval_scope: str
    approval_at: str

    def __post_init__(self) -> None:
        for attr, label in (
            ("candidate_id", "candidate ID"), ("candidate_name", "candidate name"),
            ("replacement", "replacement"), ("owner", "owner"),
            ("review_id", "review ID"), ("approval_principal", "approval principal"),
            ("approval_scope", "approval scope"),
        ):
            object.__setattr__(self, attr, _identifier(getattr(self, attr), label))
        if self.candidate_name == self.replacement:
            raise ValueError("replacement must differ from candidate")
        if self.review_recommendation not in {
                "retain", "adapt", "migrate", "removal_candidate",
                "insufficient_evidence"}:
            raise ValueError("unsupported review recommendation")
        evidence: dict[str, str] = {}
        unknown = set(self.evidence_digests) - REQUIRED_EVIDENCE
        if unknown:
            raise ValueError(f"unknown removal evidence: {sorted(unknown)}")
        for kind, digest in self.evidence_digests.items():
            normalized = str(digest or "").lower()
            if not _DIGEST.fullmatch(normalized):
                raise ValueError(f"{kind} evidence must use a sha256 digest")
            evidence[str(kind)] = normalized
        object.__setattr__(self, "evidence_digests", MappingProxyType(
            dict(sorted(evidence.items()))))
        bool_fields = (
            "coverage_complete", "telemetry_complete", "conformance_passed",
            "quality_regression", "reliability_regression",
            "performance_exception_justified", "state_export_checksum_verified",
            "rollback_retained_for_release", "documentation_migrated",
            "explicitly_approved",
        )
        if any(not isinstance(getattr(self, name), bool) for name in bool_fields):
            raise ValueError("gate flags must be boolean")
        if (isinstance(self.unmigrated_stored_definitions, bool) or
                not isinstance(self.unmigrated_stored_definitions, int) or
                self.unmigrated_stored_definitions < 0):
            raise ValueError("unmigrated definition count must be a non-negative integer")
        cycles = tuple(self.zero_use_cycles)
        if not all(isinstance(item, ZeroUseCycle) for item in cycles):
            raise ValueError("zero-use cycles must be typed evidence")
        if len({item.cycle_id for item in cycles}) != len(cycles):
            raise ValueError("zero-use cycle IDs must be unique")
        object.__setattr__(self, "zero_use_cycles", tuple(sorted(
            cycles, key=lambda item: (item.started_at, item.cycle_id))))
        if self.shadow_disposition not in {"completed", "not_safe"}:
            raise ValueError("shadow disposition must be completed or not_safe")
        for name in ("baseline_p95_ms", "candidate_p95_ms"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                    not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be a positive finite number")
            object.__setattr__(self, name, float(value))
        gain = str(self.performance_gain_digest or "").lower()
        if gain and not _DIGEST.fullmatch(gain):
            raise ValueError("performance gain evidence must use a sha256 digest")
        object.__setattr__(self, "performance_gain_digest", gain)
        object.__setattr__(self, "approval_at", _instant(
            self.approval_at, "approval timestamp"))


def evaluate_removal_gate(submission: RemovalGateSubmission) -> dict[str, Any]:
    """Return a deterministic eligibility receipt; never execute removal."""
    blockers: list[str] = []
    missing = sorted(REQUIRED_EVIDENCE - set(submission.evidence_digests))
    blockers.extend(f"missing_evidence:{kind}" for kind in missing)
    if submission.review_recommendation != "removal_candidate":
        blockers.append("independent_review_not_removal_candidate")
    if not submission.coverage_complete:
        blockers.append("inventory_coverage_incomplete")
    if not submission.telemetry_complete:
        blockers.append("telemetry_incomplete_not_zero_use")
    if submission.unmigrated_stored_definitions:
        blockers.append("unmigrated_stored_definitions_present")
    if len(submission.zero_use_cycles) < 2:
        blockers.append("two_normal_zero_use_cycles_required")
    elif any(not item.normal_operation for item in submission.zero_use_cycles):
        blockers.append("non_normal_observation_cycle")
    elif any(
        datetime.fromisoformat(later.started_at.replace("Z", "+00:00")) <
        datetime.fromisoformat(earlier.ended_at.replace("Z", "+00:00"))
        for earlier, later in zip(
            submission.zero_use_cycles, submission.zero_use_cycles[1:])
    ):
        blockers.append("observation_cycles_overlap")
    if any(not item.telemetry_complete for item in submission.zero_use_cycles):
        blockers.append("cycle_telemetry_incomplete_not_zero_use")
    if any(item.non_probe_calls for item in submission.zero_use_cycles):
        blockers.append("non_probe_calls_observed")
    if not submission.conformance_passed:
        blockers.append("full_conformance_not_passed")
    if submission.quality_regression:
        blockers.append("meaningful_quality_regression")
    if submission.reliability_regression:
        blockers.append("meaningful_reliability_regression")
    p95_limit = submission.baseline_p95_ms * 1.10
    if submission.candidate_p95_ms > p95_limit and not (
            submission.performance_exception_justified and
            submission.performance_gain_digest):
        blockers.append("p95_regression_exceeds_ten_percent")
    if not submission.state_export_checksum_verified:
        blockers.append("state_export_checksum_unverified")
    if not submission.rollback_retained_for_release:
        blockers.append("rollback_not_rehearsed_and_retained")
    if not submission.documentation_migrated:
        blockers.append("documentation_not_migrated")
    if not submission.explicitly_approved:
        blockers.append("explicit_approval_missing")
    if submission.approval_scope not in {
            submission.candidate_id, submission.candidate_name}:
        blockers.append("approval_scope_mismatch")
    blockers = sorted(set(blockers))
    evidence = dict(submission.evidence_digests)
    identity = {
        "schema": SCHEMA, "candidate_id": submission.candidate_id,
        "candidate_name": submission.candidate_name,
        "replacement": submission.replacement, "owner": submission.owner,
        "review_id": submission.review_id,
        "review_recommendation": submission.review_recommendation,
        "evidence_digests": evidence,
        "coverage_complete": submission.coverage_complete,
        "telemetry_complete": submission.telemetry_complete,
        "unmigrated_stored_definitions": submission.unmigrated_stored_definitions,
        "zero_use_cycles": [item.to_dict() for item in submission.zero_use_cycles],
        "shadow_disposition": submission.shadow_disposition,
        "conformance_passed": submission.conformance_passed,
        "quality_regression": submission.quality_regression,
        "reliability_regression": submission.reliability_regression,
        "baseline_p95_ms": submission.baseline_p95_ms,
        "candidate_p95_ms": submission.candidate_p95_ms,
        "performance_exception_justified": submission.performance_exception_justified,
        "performance_gain_digest": submission.performance_gain_digest,
        "state_export_checksum_verified": submission.state_export_checksum_verified,
        "rollback_retained_for_release": submission.rollback_retained_for_release,
        "documentation_migrated": submission.documentation_migrated,
        "explicitly_approved": submission.explicitly_approved,
        "approval_principal": submission.approval_principal,
        "approval_scope": submission.approval_scope,
        "approval_at": submission.approval_at,
        "blockers": blockers,
    }
    return {
        "gate_receipt_id": _identity("remgate_", identity), **identity,
        "gate_passed": not blockers,
        "eligible_for_separately_executed_removal": not blockers,
        "removes_anything": False, "executes": False, "mutates": False,
        "missing_telemetry_means_zero_use": False,
    }
