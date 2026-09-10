"""Deterministic retry/rate-limit planning for external effects.

This module never sleeps or executes an operation.  It turns a validated,
payload-free effect plan plus bounded outcome evidence into a scheduling
decision that an execution adapter may enforce later.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .external_effects import validate_external_effect_plan


SCHEMA = "vera.external-effect-retry-plan/v1"
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
RETRYABLE_ERRORS = frozenset({"connection_error", "timeout", "temporarily_unavailable"})
MAX_ATTEMPTS_LIMIT = 20
MAX_DELAY_MS = 86_400_000
REASON_DESCRIPTIONS = {
    "effect_not_admitted": "The original effect did not pass policy admission.",
    "successful_receipt_exists": "A durable success receipt already exists.",
    "retry_not_requested": "The original effect did not explicitly request retry handling.",
    "attempt_budget_exhausted": "The bounded attempt budget has been exhausted.",
    "outcome_not_retryable": "The observed outcome is not classified as transient.",
}


def describe_retry_policy() -> dict[str, Any]:
    """Return the bounded, non-executing retry vocabulary for inspection UIs."""
    return {
        "schema": "vera.external-effect-retry-policy/v1",
        "retryable_http_statuses": sorted(RETRYABLE_STATUS),
        "retryable_error_codes": sorted(RETRYABLE_ERRORS),
        "reason_descriptions": dict(sorted(REASON_DESCRIPTIONS.items())),
        "bounds": {"maximum_attempts": MAX_ATTEMPTS_LIMIT,
                   "maximum_delay_ms": MAX_DELAY_MS},
        "requirements": ["effect_admitted", "retry_explicitly_requested",
                         "attempt_budget_remaining", "transient_outcome",
                         "no_successful_receipt"],
        "delay_semantics": "provider_floor_plus_caller_jitter_window",
        "executes": False, "sleeps": False, "records_receipt": False,
        "resolves_secrets": False, "retains_payload": False,
    }


def _integer(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) \
            or not minimum <= value <= maximum:
        raise ValueError(f"{field} is outside its supported bounds")
    return value


def _optional_integer(value: Any, field: str, *, minimum: int,
                      maximum: int) -> int | None:
    if value is None:
        return None
    return _integer(value, field, minimum=minimum, maximum=maximum)


def plan_effect_retry(
        plan: Mapping[str, Any], *, attempts_completed: int,
        max_attempts: int = 3, status_code: int = 0, error_code: str = "",
        retry_after_ms: int | None = None, rate_limit: int | None = None,
        rate_remaining: int | None = None, rate_reset_after_ms: int | None = None,
        successful_receipt: bool = False, base_delay_ms: int = 250,
        backoff_cap_ms: int = 30_000) -> dict[str, Any]:
    """Project whether and when another attempt may be scheduled."""
    plan = validate_external_effect_plan(plan)
    attempts_completed = _integer(
        attempts_completed, "attempts_completed", minimum=1,
        maximum=MAX_ATTEMPTS_LIMIT)
    max_attempts = _integer(
        max_attempts, "max_attempts", minimum=1, maximum=MAX_ATTEMPTS_LIMIT)
    status_code = _integer(status_code, "status_code", minimum=0, maximum=599)
    base_delay_ms = _integer(
        base_delay_ms, "base_delay_ms", minimum=1, maximum=MAX_DELAY_MS)
    backoff_cap_ms = _integer(
        backoff_cap_ms, "backoff_cap_ms", minimum=base_delay_ms,
        maximum=MAX_DELAY_MS)
    retry_after_ms = _optional_integer(
        retry_after_ms, "retry_after_ms", minimum=0, maximum=MAX_DELAY_MS)
    rate_limit = _optional_integer(
        rate_limit, "rate_limit", minimum=0, maximum=1_000_000_000)
    rate_remaining = _optional_integer(
        rate_remaining, "rate_remaining", minimum=0, maximum=1_000_000_000)
    rate_reset_after_ms = _optional_integer(
        rate_reset_after_ms, "rate_reset_after_ms", minimum=0,
        maximum=MAX_DELAY_MS)
    if rate_remaining is not None and rate_limit is None:
        raise ValueError("rate_remaining requires rate_limit")
    if rate_limit is not None and rate_remaining is not None \
            and rate_remaining > rate_limit:
        raise ValueError("rate_remaining cannot exceed rate_limit")
    if not isinstance(successful_receipt, bool):
        raise ValueError("successful_receipt must be boolean")
    error_code = str(error_code or "").strip()
    if error_code and error_code not in RETRYABLE_ERRORS | {"cancelled", "permanent_error"}:
        raise ValueError("error_code is unsupported")
    if status_code and error_code:
        raise ValueError("status_code and error_code are mutually exclusive")

    retryable = status_code in RETRYABLE_STATUS or error_code in RETRYABLE_ERRORS
    reasons: list[str] = []
    if not plan["admission"]["allowed"]:
        reasons.append("effect_not_admitted")
    if successful_receipt:
        reasons.append("successful_receipt_exists")
    if not plan["retry"]["requested"]:
        reasons.append("retry_not_requested")
    if attempts_completed >= max_attempts:
        reasons.append("attempt_budget_exhausted")
    if not retryable:
        reasons.append("outcome_not_retryable")
    reasons = sorted(set(reasons))
    allowed = not reasons

    # The local exponential bound is a jitter window, not a chosen sleep.
    # Provider evidence is a minimum and is never shortened by the local cap.
    exponent = min(attempts_completed, MAX_ATTEMPTS_LIMIT)
    backoff_ceiling = min(backoff_cap_ms, base_delay_ms * (2 ** exponent))
    provider_floor = max(
        retry_after_ms or 0,
        rate_reset_after_ms or 0 if rate_remaining == 0 else 0,
    )
    earliest = provider_floor if allowed else 0
    latest = max(provider_floor, backoff_ceiling) if allowed else 0
    identity = json.dumps({
        "plan_id": plan["plan_id"], "attempts_completed": attempts_completed,
        "max_attempts": max_attempts, "status_code": status_code,
        "error_code": error_code, "successful_receipt": successful_receipt,
        "retry_after_ms": retry_after_ms, "rate_limit": rate_limit,
        "rate_remaining": rate_remaining,
        "rate_reset_after_ms": rate_reset_after_ms,
        "base_delay_ms": base_delay_ms, "backoff_cap_ms": backoff_cap_ms,
    }, sort_keys=True, separators=(",", ":"))
    return {
        "schema": SCHEMA,
        "retry_plan_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "effect_plan_id": plan["plan_id"],
        "effect_classification": plan["classification"],
        "attempts": {"completed": attempts_completed, "maximum": max_attempts,
                     "remaining": max(0, max_attempts - attempts_completed)},
        "outcome": {"status_code": status_code, "error_code": error_code,
                    "retryable": retryable},
        "rate_limit": {"limit": rate_limit, "remaining": rate_remaining,
                       "retry_after_ms": retry_after_ms,
                       "reset_after_ms": rate_reset_after_ms},
        "schedule": {"allowed": allowed, "reasons": reasons,
                     "earliest_delay_ms": earliest,
                     "latest_delay_ms": latest,
                     "selection": "caller_jitter_within_window" if allowed else "none"},
        "executes": False, "sleeps": False, "records_receipt": False,
        "resolves_secrets": False, "retains_payload": False,
    }
