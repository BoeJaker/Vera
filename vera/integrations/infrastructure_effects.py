"""Provider-neutral, payload-free projection for infrastructure mutations."""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Mapping

from Vera.vera.integrations.effect_receipts import default_external_effect_receipt_ledger
from Vera.vera.integrations.effect_shadow_evidence import default_external_effect_shadow_evidence
from Vera.vera.integrations.external_effects import plan_external_effect


log = logging.getLogger("vera.integrations.infrastructure_effects")
SCHEMA = "vera.infrastructure-effect-shadow/v1"
_IDENTITY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def _digest(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def plan_infrastructure_effect(*, provider: str, target_ref: str,
                               resource_ref: str, operation_ref: str, mode: str,
                               idempotency_key: str = "",
                               approval_receipt_ref: str = "",
                               retry: bool = False) -> dict[str, Any]:
    """Describe one infrastructure mutation without retaining its arguments."""
    provider = str(provider or "").strip().lower()
    mode = str(mode or "").strip().lower()
    target = str(target_ref or "").strip()
    resource = str(resource_ref or "").strip()
    operation = str(operation_ref or "").strip()
    if not _IDENTITY.fullmatch(provider):
        raise ValueError("infrastructure provider is unsupported")
    if not _IDENTITY.fullmatch(mode):
        raise ValueError("infrastructure effect mode is unsupported")
    if not target:
        raise ValueError("target_ref required")
    if not resource:
        raise ValueError("resource_ref required")
    if not operation:
        raise ValueError("operation_ref required")
    target_sha256 = _digest(target)
    resource_sha256 = _digest(resource)
    operation_sha256 = _digest(operation)
    plan = plan_external_effect(
        connection_id=f"{provider}:{target_sha256}",
        operation=f"{provider}.{mode}:{resource_sha256}:{operation_sha256}",
        method="POST", idempotency_key=idempotency_key,
        approval_receipt_ref=approval_receipt_ref, retry=retry)
    return {
        "schema": SCHEMA, "enforcement": "observe_only", "plan": plan,
        "replay": {"already_succeeded": False, "would_suppress": False,
                   "successful_receipt_id": ""},
        "decision": {"would_admit": bool(plan["admission"]["allowed"]),
                     "would_execute": bool(plan["admission"]["allowed"]),
                     "reasons": list(plan["admission"]["reasons"])},
        "delivery": {"provider": provider, "mode": mode,
                     "target_sha256": target_sha256,
                     "resource_sha256": resource_sha256,
                     "operation_sha256": operation_sha256,
                     "provider_idempotency_forwarded": False},
        "blocks_current_call": False, "forwards_control_references": False,
        "records_completion": False, "executes": False, "retains_payload": False,
    }


def _with_replay(shadow: Mapping[str, Any], replay: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(shadow)
    succeeded = bool(replay.get("already_succeeded"))
    out["replay"] = {"already_succeeded": succeeded, "would_suppress": succeeded,
                     "successful_receipt_id": str(
                         replay.get("successful_receipt_id") or "")}
    decision = dict(out.get("decision") or {})
    decision["would_execute"] = bool(decision.get("would_admit") and not succeeded)
    out["decision"] = decision
    return out


def observe_infrastructure_effect(**arguments) -> dict[str, Any]:
    """Plan and best-effort record; never govern the provider call."""
    try:
        shadow = plan_infrastructure_effect(**arguments)
        if shadow["plan"]["admission"]["allowed"]:
            replay = default_external_effect_receipt_ledger().replay_status(shadow["plan"])
            shadow = _with_replay(shadow, replay)
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
        default_external_effect_shadow_evidence(family="infrastructure").record(shadow)
    except Exception:
        log.exception("Infrastructure effect shadow evidence record failed")
    return shadow
