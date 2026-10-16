"""Payload-free, observe-only evidence for routed physical print attempts."""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Mapping

from ..integrations.effect_receipts import default_external_effect_receipt_ledger
from ..integrations.effect_shadow_evidence import default_external_effect_shadow_evidence
from ..integrations.external_effects import plan_external_effect


log = logging.getLogger("vera.print.effects")
SCHEMA = "vera.physical-print-effect-shadow/v1"
PHYSICAL_TRANSPORTS = frozenset({"server_usb", "server_serial", "mesh"})


def plan_physical_print_effect(
        *, transport: str, endpoint_ref: str, idempotency_key: str = "",
        approval_receipt_ref: str = "", retry: bool = False) -> dict[str, Any]:
    """Describe one routed device attempt without retaining bytes or endpoint."""
    transport = str(transport or "").strip().lower()
    if transport not in PHYSICAL_TRANSPORTS:
        raise ValueError("physical print transport is unsupported")
    endpoint = str(endpoint_ref or "").strip()
    if not endpoint:
        raise ValueError("endpoint_ref required")
    endpoint_sha256 = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
    plan = plan_external_effect(
        connection_id=f"printer:{transport}",
        operation=f"print.send:{endpoint_sha256}", method="POST",
        idempotency_key=idempotency_key,
        approval_receipt_ref=approval_receipt_ref, retry=retry)
    return {
        "schema": SCHEMA, "enforcement": "observe_only", "plan": plan,
        "replay": {"already_succeeded": False, "would_suppress": False,
                   "successful_receipt_id": ""},
        "decision": {"would_admit": bool(plan["admission"]["allowed"]),
                     "would_execute": bool(plan["admission"]["allowed"]),
                     "reasons": list(plan["admission"]["reasons"])},
        "delivery": {"stage": "physical_attempt", "transport": transport,
                     "endpoint_sha256": endpoint_sha256,
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


def observe_physical_print_effect(**arguments) -> dict[str, Any]:
    """Plan and best-effort record; never govern device/mesh routing."""
    try:
        shadow = plan_physical_print_effect(**arguments)
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
        default_external_effect_shadow_evidence(family="printing").record(shadow)
    except Exception:
        log.exception("physical print effect shadow evidence record failed")
    return shadow
