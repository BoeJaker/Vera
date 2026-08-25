"""Pure, content-free policy previews for capability execution.

This module does not authorize or execute anything. It gives the existing
capability wrapper one deterministic policy vocabulary while contracts and
trusted approval receipts are migrated incrementally.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


POLICY_SCHEMA = "vera.capability-policy-shadow/v1"
BASELINE_EFFECTS = {"none", "read"}
SENSITIVE_EFFECTS = {
    "write", "delete", "execute", "network", "filesystem", "secrets",
    "approval", "model", "accelerator", "external_side_effect",
}
APPROVAL_REQUIRED = {"required", "human_required", "user_required", "per_call"}
SESSION_SCOPED = {"session_scoped", "session_required"}
TENANT_SCOPED = {"tenant_scoped", "tenant_required"}
OPAQUE_SECRET_STATUSES = {
    "opaque_reference", "opaque_references", "secret_ref", "secret_refs_only",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _strings(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {_text(item) for item in value if _text(item)}


def evaluate_policy_shadow(name: str, contract: Mapping[str, Any] | None,
                           context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Predict a policy verdict without granting authority or inspecting payloads."""
    declared = _mapping(contract)
    scope = _mapping(context)
    reasons = []
    deny = False
    indeterminate = False

    if not declared:
        reasons.append({"code": "contract_unknown"})
        indeterminate = True

    effects_present = "effects" in declared
    effects = _strings(declared.get("effects"))
    if not effects_present or not effects:
        reasons.append({"code": "effects_unknown"})
        indeterminate = True

    allowed_effects = (_strings(scope.get("allowed_effects"))
                       if "allowed_effects" in scope else set(BASELINE_EFFECTS))
    missing_effect_grants = sorted(effects - allowed_effects)
    if missing_effect_grants:
        reasons.append({"code": "effect_grant_missing", "effects": missing_effect_grants})
        deny = True

    approval_status = _text(_mapping(declared.get("approval")).get("status")) or "unknown"
    approval_required = approval_status in APPROVAL_REQUIRED
    if approval_required and scope.get("approval_present") is not True:
        reasons.append({"code": "approval_missing"})
        deny = True
    elif approval_status == "unknown" and effects & SENSITIVE_EFFECTS:
        reasons.append({"code": "approval_posture_unknown"})
        indeterminate = True

    tenant_status = _text(_mapping(declared.get("tenant")).get("status")) or "unknown"
    if tenant_status in SESSION_SCOPED and not _text(scope.get("session_id")):
        reasons.append({"code": "session_scope_missing"})
        deny = True
    if tenant_status in TENANT_SCOPED and not _text(scope.get("tenant_id")):
        reasons.append({"code": "tenant_scope_missing"})
        deny = True
    if tenant_status == "unknown" and effects & SENSITIVE_EFFECTS:
        reasons.append({"code": "tenant_posture_unknown"})
        indeterminate = True

    secrets_status = _text(_mapping(declared.get("secrets")).get("status")) or "unknown"
    if "secrets" in effects:
        if secrets_status not in OPAQUE_SECRET_STATUSES:
            reasons.append({"code": "opaque_secret_contract_required"})
            deny = True
        elif scope.get("opaque_secret_refs") is not True:
            reasons.append({"code": "opaque_secret_refs_unconfirmed"})
            deny = True

    verdict = "deny" if deny else ("indeterminate" if indeterminate else "allow")
    return {
        "schema": POLICY_SCHEMA,
        "mode": "shadow",
        "name": _text(name),
        "verdict": verdict,
        "authorized": False,
        "executed": False,
        "declared_effects": sorted(effects),
        "allowed_effects": sorted(allowed_effects),
        "missing_effect_grants": missing_effect_grants,
        "approval": {"status": approval_status, "required": approval_required,
                     "present": scope.get("approval_present") is True},
        "scope": {"tenant": tenant_status,
                  "session_present": bool(_text(scope.get("session_id"))),
                  "tenant_present": bool(_text(scope.get("tenant_id")))},
        "secrets": {"status": secrets_status,
                    "opaque_refs_confirmed": scope.get("opaque_secret_refs") is True},
        "reasons": reasons,
    }
