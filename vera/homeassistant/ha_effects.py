"""Payload-free, observe-only evidence for Home Assistant notifications."""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Mapping

from ..integrations.effect_receipts import default_external_effect_receipt_ledger
from ..integrations.effect_shadow_evidence import default_external_effect_shadow_evidence
from ..integrations.external_effects import plan_external_effect


log = logging.getLogger("vera.homeassistant.effects")
SCHEMA = "vera.homeassistant-notification-effect-shadow/v1"


def plan_homeassistant_notification_effect(
        *, target_ref: str, idempotency_key: str = "",
        approval_receipt_ref: str = "", retry: bool = False) -> dict[str, Any]:
    """Describe one notification without retaining its target or payload."""
    target = str(target_ref or "").strip()
    if not target:
        raise ValueError("target_ref required")
    target_sha256 = hashlib.sha256(target.encode("utf-8")).hexdigest()
    plan = plan_external_effect(
        connection_id="homeassistant:notify",
        operation=f"notification.send:{target_sha256}", method="POST",
        idempotency_key=idempotency_key,
        approval_receipt_ref=approval_receipt_ref, retry=retry)
    return {
        "schema": SCHEMA, "enforcement": "observe_only", "plan": plan,
        "replay": {"already_succeeded": False, "would_suppress": False,
                   "successful_receipt_id": ""},
        "decision": {"would_admit": bool(plan["admission"]["allowed"]),
                     "would_execute": bool(plan["admission"]["allowed"]),
                     "reasons": list(plan["admission"]["reasons"])},
        "delivery": {"target_sha256": target_sha256,
                     "provider_idempotency_forwarded": False},
        "blocks_current_call": False, "forwards_control_references": False,
        "records_completion": False, "executes": False,
        "retains_payload": False,
    }


def _with_replay(shadow: Mapping[str, Any], replay: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(shadow)
    succeeded = bool(replay.get("already_succeeded"))
    out["replay"] = {
        "already_succeeded": succeeded, "would_suppress": succeeded,
        "successful_receipt_id": str(replay.get("successful_receipt_id") or ""),
    }
    decision = dict(out.get("decision") or {})
    decision["would_execute"] = bool(decision.get("would_admit") and not succeeded)
    out["decision"] = decision
    return out


def observe_homeassistant_notification_effect(**arguments) -> dict[str, Any]:
    """Plan and best-effort record; never govern the provider call."""
    try:
        shadow = plan_homeassistant_notification_effect(**arguments)
        if shadow["plan"]["admission"]["allowed"]:
            replay = default_external_effect_receipt_ledger().replay_status(shadow["plan"])
            shadow = _with_replay(shadow, replay)
    except Exception:
        shadow = {
            "schema": SCHEMA, "enforcement": "observe_only",
            "error": "shadow_unavailable",
            "decision": {"would_admit": False, "would_execute": False,
                         "reasons": ["invalid_policy_evidence"]},
            "blocks_current_call": False, "forwards_control_references": False,
            "records_completion": False, "executes": False,
            "retains_payload": False,
        }
    try:
        default_external_effect_shadow_evidence(family="homeassistant").record(shadow)
    except Exception:
        log.exception("Home Assistant effect shadow evidence record failed")
    return shadow
