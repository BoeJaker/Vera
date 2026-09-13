"""Payload-free external-effect projection for marketplace listing writes."""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Mapping

from Vera.vera.integrations.effect_receipts import default_external_effect_receipt_ledger
from Vera.vera.integrations.effect_shadow_evidence import default_external_effect_shadow_evidence
from Vera.vera.integrations.external_effects import plan_external_effect

log = logging.getLogger("vera.commerce.effects")
SCHEMA = "vera.marketplace-listing-effect-shadow/v1"
MODES = frozenset({"push", "publish", "archive"})


def _digest(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def plan_marketplace_listing_effect(*, account_ref: str, listing_ref: str,
                                    provider: str, mode: str,
                                    idempotency_key: str = "",
                                    approval_receipt_ref: str = "",
                                    retry: bool = False) -> dict[str, Any]:
    """Describe one logical provider write without retaining identifiers."""
    account = str(account_ref or "").strip()
    listing = str(listing_ref or "").strip()
    provider = str(provider or "").strip().lower()
    if not account:
        raise ValueError("account_ref required")
    if not listing:
        raise ValueError("listing_ref required")
    if not provider:
        raise ValueError("provider required")
    if mode not in MODES:
        raise ValueError("marketplace listing mode is unsupported")
    account_sha256 = _digest(account)
    listing_sha256 = _digest(listing)
    provider_sha256 = _digest(provider)
    plan = plan_external_effect(
        connection_id=f"marketplace-account:{provider_sha256}:{account_sha256}",
        operation=f"listing.{mode}:{listing_sha256}", method="POST",
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
                     "listing_sha256": listing_sha256,
                     "provider_sha256": provider_sha256,
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


def observe_marketplace_listing_effect(**arguments) -> dict[str, Any]:
    """Plan, consult replay evidence, and best-effort record one observation."""
    try:
        shadow = plan_marketplace_listing_effect(**arguments)
        if shadow["plan"]["admission"]["allowed"]:
            replay = default_external_effect_receipt_ledger().replay_status(shadow["plan"])
            shadow = apply_replay_evidence(shadow, replay)
    except Exception:
        shadow = {"schema": SCHEMA, "enforcement": "observe_only",
                  "error": "shadow_unavailable",
                  "decision": {"would_admit": False, "would_execute": False,
                               "reasons": ["invalid_policy_evidence"]},
                  "blocks_current_call": False,
                  "forwards_control_references": False,
                  "records_completion": False, "executes": False,
                  "retains_payload": False}
    try:
        default_external_effect_shadow_evidence(family="commerce").record(shadow)
    except Exception:
        log.exception("Marketplace effect shadow evidence record failed")
    return shadow
