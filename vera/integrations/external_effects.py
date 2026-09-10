"""Pure planning contract for calls that may create external effects.

The planner is deliberately non-executing.  It gives integrations and other
outbound adapters one vocabulary for connection identity, approval,
idempotency, retry safety, and receipts without accepting secrets or payloads.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Mapping


SCHEMA = "vera.external-effect-plan/v1"
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
IDEMPOTENT_WRITE_METHODS = frozenset({"PUT", "DELETE"})
NON_IDEMPOTENT_METHODS = frozenset({"POST", "PATCH"})
SUPPORTED_METHODS = READ_METHODS | IDEMPOTENT_WRITE_METHODS | NON_IDEMPOTENT_METHODS
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _identity(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _IDENTITY.fullmatch(text):
        raise ValueError(f"{field} must be an opaque identifier")
    return text


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


def _plan_id(*, connection_id: str, operation: str, method: str,
             idempotency_key_sha256: str) -> str:
    canonical = json.dumps({
        "connection_id": connection_id, "operation": operation, "method": method,
        "idempotency_key_sha256": idempotency_key_sha256,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    key_digest = _digest(idempotency_key)
    return {
        "schema": SCHEMA,
        # Approval can be renewed and retry intent can change. Neither is part of
        # the effect identity; otherwise replay lookup could be bypassed merely by
        # asking again with a fresh approval receipt.
        "plan_id": _plan_id(connection_id=connection_id, operation=operation,
                            method=method, idempotency_key_sha256=key_digest),
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
            "key_sha256": key_digest,
        },
        "retry": {"requested": bool(retry), "allowed": not reasons},
        "admission": {"allowed": not reasons, "reasons": sorted(set(reasons))},
        "executes": False,
        "resolves_secrets": False,
        "retains_payload": False,
    }


def validate_external_effect_plan(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate the closed public plan before it can anchor durable evidence."""
    if not isinstance(value, Mapping):
        raise TypeError("external effect plan must be an object")
    allowed = {"schema", "plan_id", "connection", "operation", "method",
               "classification", "mutating", "approval", "idempotency",
               "retry", "admission", "executes", "resolves_secrets",
               "retains_payload"}
    if set(value) != allowed or value.get("schema") != SCHEMA:
        raise ValueError("external effect plan fields or schema are unsupported")
    connection = value.get("connection")
    if not isinstance(connection, Mapping) or set(connection) != {"id"}:
        raise ValueError("connection fields are invalid")
    connection_id = _identity(connection.get("id"), "connection_id")
    operation = _identity(value.get("operation"), "operation")
    method = str(value.get("method") or "")
    if method not in SUPPORTED_METHODS:
        raise ValueError("method is unsupported")
    expected_class = ("read" if method in READ_METHODS else "idempotent_write"
                      if method in IDEMPOTENT_WRITE_METHODS else "non_idempotent_write")
    if value.get("classification") != expected_class:
        raise ValueError("classification does not match method")
    if value.get("mutating") is not (method not in READ_METHODS):
        raise ValueError("mutating does not match method")
    approval = value.get("approval")
    idempotency = value.get("idempotency")
    retry = value.get("retry")
    admission = value.get("admission")
    if not all(isinstance(item, Mapping) for item in
               (approval, idempotency, retry, admission)):
        raise ValueError("policy sections must be objects")
    if set(approval) != {"required", "receipt_present", "receipt_ref_sha256"} \
            or set(idempotency) != {"required", "key_present", "key_sha256"} \
            or set(retry) != {"requested", "allowed"} \
            or set(admission) != {"allowed", "reasons"}:
        raise ValueError("policy section fields are invalid")
    for section, present_name, digest_name in (
            (approval, "receipt_present", "receipt_ref_sha256"),
            (idempotency, "key_present", "key_sha256")):
        digest = section.get(digest_name)
        if not isinstance(digest, str) or (digest and not _SHA256.fullmatch(digest)):
            raise ValueError("reference digest is invalid")
        if section.get(present_name) is not bool(digest):
            raise ValueError("reference presence does not match digest")
    if approval.get("required") is not (method not in READ_METHODS) \
            or idempotency.get("required") is not (method in NON_IDEMPOTENT_METHODS):
        raise ValueError("policy requirements do not match method")
    if not isinstance(retry.get("requested"), bool):
        raise ValueError("retry requested flag is invalid")
    if not isinstance(admission.get("reasons"), list) \
            or any(not isinstance(reason, str) for reason in admission["reasons"]):
        raise ValueError("admission reasons are invalid")
    expected_reasons = []
    if approval["required"] and not approval["receipt_present"]:
        expected_reasons.append("approval_receipt_required")
    if idempotency["required"] and not idempotency["key_present"]:
        expected_reasons.append("idempotency_key_required")
    if retry["requested"] and idempotency["required"] \
            and not approval["receipt_present"]:
        expected_reasons.append("retry_approval_receipt_required")
    expected_reasons = sorted(set(expected_reasons))
    if admission["reasons"] != expected_reasons:
        raise ValueError("admission reasons do not match policy evidence")
    expected_allowed = not expected_reasons
    if admission.get("allowed") is not expected_allowed \
            or retry.get("allowed") is not expected_allowed:
        raise ValueError("admission decision is inconsistent")
    if any(value.get(field) is not False for field in
           ("executes", "resolves_secrets", "retains_payload")):
        raise ValueError("external effect plans cannot execute or retain sensitive data")
    expected_id = _plan_id(
        connection_id=connection_id, operation=operation, method=method,
        idempotency_key_sha256=idempotency["key_sha256"])
    if value.get("plan_id") != expected_id:
        raise ValueError("plan_id does not match effect identity")
    return json.loads(json.dumps(value, sort_keys=True))
