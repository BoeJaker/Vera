"""Payload-free external-effect projection for Telegram outbound messages."""
from __future__ import annotations

import hashlib
from typing import Any, Mapping

from Vera.vera.integrations.external_effects import plan_external_effect


SCHEMA = "vera.telegram-send-effect-shadow/v1"


def plan_telegram_send_effect(*, chat_id: str, mode: str = "plain",
                              idempotency_key: str = "",
                              approval_receipt_ref: str = "",
                              retry: bool = False) -> dict[str, Any]:
    """Describe one recipient delivery without retaining its address or body."""
    recipient = str(chat_id or "").strip()
    if not recipient:
        raise ValueError("chat_id required")
    if mode not in {"plain", "markdown", "notification", "broadcast"}:
        raise ValueError("Telegram delivery mode is unsupported")
    recipient_sha256 = hashlib.sha256(recipient.encode("utf-8")).hexdigest()
    plan = plan_external_effect(
        connection_id="telegram:bot",
        operation=f"message.send:{mode}:{recipient_sha256}",
        method="POST", idempotency_key=idempotency_key,
        approval_receipt_ref=approval_receipt_ref, retry=retry)
    return {
        "schema": SCHEMA,
        "enforcement": "observe_only",
        "plan": plan,
        "replay": {"already_succeeded": False, "would_suppress": False,
                   "successful_receipt_id": ""},
        "decision": {"would_admit": bool(plan["admission"]["allowed"]),
                     "would_execute": bool(plan["admission"]["allowed"]),
                     "reasons": list(plan["admission"]["reasons"])},
        "delivery": {"mode": mode, "recipient_sha256": recipient_sha256,
                     "provider_idempotency_forwarded": False},
        "blocks_current_call": False,
        "forwards_control_references": False,
        "records_completion": False,
        "executes": False,
        "retains_payload": False,
    }


def apply_replay_evidence(shadow: Mapping[str, Any],
                          replay: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copied shadow with durable replay evidence applied."""
    out = dict(shadow)
    out["replay"] = {
        "already_succeeded": bool(replay.get("already_succeeded")),
        "would_suppress": bool(replay.get("already_succeeded")),
        "successful_receipt_id": str(replay.get("successful_receipt_id") or ""),
    }
    decision = dict(out.get("decision") or {})
    decision["would_execute"] = bool(
        decision.get("would_admit") and not replay.get("already_succeeded"))
    out["decision"] = decision
    return out
