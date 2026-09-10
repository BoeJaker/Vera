"""Pure planning contract for calls that may create external effects.

The planner is deliberately non-executing.  It gives integrations and other
outbound adapters one vocabulary for connection identity, approval,
idempotency, retry safety, and receipts without accepting secrets or payloads.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict


SCHEMA = "vera.external-effect-plan/v1"
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
IDEMPOTENT_WRITE_METHODS = frozenset({"PUT", "DELETE"})
NON_IDEMPOTENT_METHODS = frozenset({"POST", "PATCH"})
SUPPORTED_METHODS = READ_METHODS | IDEMPOTENT_WRITE_METHODS | NON_IDEMPOTENT_METHODS
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


def _identity(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _IDENTITY.fullmatch(text):
        raise ValueError(f"{field} must be an opaque identifier")
    return text


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


def plan_external_effect(*, connection_id: str, operation: str,
                         method: str = "GET", idempotency_key: str = "",
                         approval_receipt_ref: str = "", retry: bool = False) -> Dict[str, Any]:
    """Return a closed, redacted admission plan; never perform the operation."""
    connection_id = _identity(connection_id, "connection_id")
    operation = _identity(operation, "operation")
    method = str(method or "GET").strip().upper()
    if method not in SUPPORTED_METHODS:
        raise ValueError("method is unsupported")
    if idempotency_key:
        _identity(idempotency_key, "idempotency_key")
    if approval_receipt_ref:
        _identity(approval_receipt_ref, "approval_receipt_ref")

    mutating = method not in READ_METHODS
    non_idempotent = method in NON_IDEMPOTENT_METHODS
    approval_required = mutating
    reasons = []
    if approval_required and not approval_receipt_ref:
        reasons.append("approval_receipt_required")
    if non_idempotent and not idempotency_key:
        reasons.append("idempotency_key_required")
    if retry and non_idempotent and not approval_receipt_ref:
        reasons.append("retry_approval_receipt_required")

    classification = (
        "read" if not mutating else
        "idempotent_write" if method in IDEMPOTENT_WRITE_METHODS else
        "non_idempotent_write"
    )
    canonical = json.dumps({
        "connection_id": connection_id,
        "operation": operation,
        "method": method,
        "idempotency_key_sha256": _digest(idempotency_key),
        "approval_receipt_ref_sha256": _digest(approval_receipt_ref),
        "retry": bool(retry),
    }, sort_keys=True, separators=(",", ":"))
    return {
        "schema": SCHEMA,
        "plan_id": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "connection": {"id": connection_id},
        "operation": operation,
        "method": method,
        "classification": classification,
        "mutating": mutating,
        "approval": {
            "required": approval_required,
            "receipt_present": bool(approval_receipt_ref),
            "receipt_ref_sha256": _digest(approval_receipt_ref),
        },
        "idempotency": {
            "required": non_idempotent,
            "key_present": bool(idempotency_key),
            "key_sha256": _digest(idempotency_key),
        },
        "retry": {"requested": bool(retry), "allowed": not reasons},
        "admission": {"allowed": not reasons, "reasons": sorted(set(reasons))},
        "executes": False,
        "resolves_secrets": False,
        "retains_payload": False,
    }
