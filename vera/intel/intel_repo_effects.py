"""Payload-free, observe-only evidence for Intel repository mutations."""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Mapping

from ..integrations.effect_receipts import default_external_effect_receipt_ledger
from ..integrations.effect_shadow_evidence import default_external_effect_shadow_evidence
from ..integrations.external_effects import plan_external_effect


log = logging.getLogger("vera.intel.repository.effects")
SCHEMA = "vera.intel-repository-effect-shadow/v1"
_METHODS = {"clone": "POST", "fetch": "PUT", "reset": "PUT"}


def plan_repository_effect(
        *, operation: str, repository_ref: str, provider: str,
        idempotency_key: str = "", approval_receipt_ref: str = "",
        retry: bool = False) -> dict[str, Any]:
    """Describe one Git mutation without retaining its URL, path or identity."""
    operation = str(operation or "").strip().lower()
    if operation not in _METHODS:
        raise ValueError("repository operation is unsupported")
    repository = str(repository_ref or "").strip()
    if not repository:
        raise ValueError("repository_ref required")
    provider = str(provider or "").strip().lower()
    if not provider or not provider.replace(".", "").replace("-", "").isalnum():
        raise ValueError("repository provider is invalid")
    repository_sha256 = hashlib.sha256(repository.encode("utf-8")).hexdigest()
    plan = plan_external_effect(
        connection_id=f"git:{provider}",
        operation=f"repository.{operation}:{repository_sha256}",
        method=_METHODS[operation], idempotency_key=idempotency_key,
        approval_receipt_ref=approval_receipt_ref, retry=retry)
    return {
        "schema": SCHEMA, "enforcement": "observe_only", "plan": plan,
        "replay": {"already_succeeded": False, "would_suppress": False,
                   "successful_receipt_id": ""},
        "decision": {"would_admit": bool(plan["admission"]["allowed"]),
                     "would_execute": bool(plan["admission"]["allowed"]),
                     "reasons": list(plan["admission"]["reasons"])},
        "repository": {"operation": operation, "provider": provider,
                       "repository_sha256": repository_sha256,
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


def observe_repository_effect(**arguments) -> dict[str, Any]:
    """Plan and best-effort record; never govern the Git subprocess."""
    try:
        shadow = plan_repository_effect(**arguments)
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
        default_external_effect_shadow_evidence(family="repository").record(shadow)
    except Exception:
        log.exception("repository effect shadow evidence record failed")
    return shadow
