"""Payload-free external-effect projection for Email outbound messages."""
from __future__ import annotations

import hashlib
from typing import Any, Mapping

from Vera.vera.integrations.external_effects import plan_external_effect


SCHEMA = "vera.email-send-effect-shadow/v1"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def plan_email_send_effect(*, account_ref: str = "", destination_ref: str,
                           mode: str = "send", idempotency_key: str = "",
                           approval_receipt_ref: str = "",
                           retry: bool = False) -> dict[str, Any]:
    """Describe one logical delivery without retaining addresses or content."""
    destination = str(destination_ref or "").strip()
    if not destination:
        raise ValueError("destination_ref required")
    if mode not in {"send", "reply", "event"}:
        raise ValueError("Email delivery mode is unsupported")
    account_sha256 = _digest(str(account_ref or "default"))
    destination_sha256 = _digest(destination)
    plan = plan_external_effect(
        connection_id=f"email-account:{account_sha256}",
        operation=f"message.{mode}:{destination_sha256}", method="POST",
        idempotency_key=idempotency_key,
        approval_receipt_ref=approval_receipt_ref, retry=retry)
    return {
        "schema": SCHEMA, "enforcement": "observe_only", "plan": plan,
        "replay": {"already_succeeded": False, "would_suppress": False,
                   "successful_receipt_id": ""},
        "decision": {"would_admit": bool(plan["admission"]["allowed"]),
                     "would_execute": bool(plan["admission"]["allowed"]),
                     "reasons": list(plan["admission"]["reasons"])},
        "delivery": {"mode": mode, "account_sha256": account_sha256,
                     "destination_sha256": destination_sha256,
                     "provider_idempotency_forwarded": False},
        "blocks_current_call": False, "forwards_control_references": False,
        "records_completion": False, "executes": False, "retains_payload": False,
    }


def apply_replay_evidence(shadow: Mapping[str, Any],
                          replay: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(shadow)
    succeeded = bool(replay.get("already_succeeded"))
    out["replay"] = {"already_succeeded": succeeded, "would_suppress": succeeded,
                     "successful_receipt_id": str(
                         replay.get("successful_receipt_id") or "")}
    decision = dict(out.get("decision") or {})
    decision["would_execute"] = bool(decision.get("would_admit") and not succeeded)
    out["decision"] = decision
    return out
