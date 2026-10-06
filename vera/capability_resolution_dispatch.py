"""Fail-closed resolution and dispatch for explicitly migrated capabilities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .capability_contract_core import manifest_fingerprint, project_contract
from .capability_policy_core import evaluate_policy_shadow
from .capability_resolver_core import resolve_shadow


DISPATCH_SCHEMA = "vera.capability-resolution-dispatch/v1"
SAFE_TASKS = {
    "capability.contract.coverage": "cap.contract.coverage",
    "capability.contract.gate": "cap.contract.gate",
    "capability.contract.observe": "cap.contract.observations",
}
RESERVED_ARGUMENTS = {"trace_id", "_local", "_timeout"}
PARITY_TASKS = {"capability.contract.coverage", "capability.contract.gate"}


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _refusal(code: str, canonical_task: str, **detail: Any) -> dict[str, Any]:
    return {
        "schema": DISPATCH_SCHEMA,
        "ok": False,
        "authorized": False,
        "executed": False,
        "canonical_task": str(canonical_task or "").strip(),
        "error": code,
        **detail,
    }


def _validate_arguments(schema: Mapping[str, Any], arguments: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not isinstance(arguments, Mapping):
        return {}, {"code": "arguments_not_object"}
    supplied = set(arguments)
    reserved = sorted(supplied & RESERVED_ARGUMENTS)
    if reserved:
        return {}, {"code": "arguments_reserved", "names": reserved}
    properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
    unknown = sorted(supplied - set(properties))
    if unknown:
        return {}, {"code": "arguments_unknown", "names": unknown}
    required = {str(name) for name in schema.get("required", []) if str(name)}
    missing = sorted(required - supplied)
    if missing:
        return {}, {"code": "arguments_required", "names": missing}
    return dict(arguments), None


async def resolve_authorize_dispatch(
    registry: Mapping[str, Mapping[str, Any]], canonical_task: str,
    arguments: Any = None, *, enabled: bool, trace_id: str | None = None,
) -> dict[str, Any]:
    """Resolve, authorize, and invoke one allowlisted read-only capability."""
    task = str(canonical_task or "").strip()
    if not enabled:
        return _refusal("dispatch_disabled", task)
    expected = SAFE_TASKS.get(task)
    if expected is None:
        return _refusal("task_not_migrated", task)

    entries = {name: registry.get(name) for name in SAFE_TASKS.values()}
    if not all(isinstance(entry, Mapping) for entry in entries.values()):
        missing = sorted(name for name, entry in entries.items() if not isinstance(entry, Mapping))
        return _refusal("registry_incomplete", task, missing=missing)
    manifests = [project_contract(name, entry) for name, entry in entries.items()]
    manifest_revision = manifest_fingerprint(manifests)
    evidence_revision = _fingerprint([])
    resolution = resolve_shadow(manifests, {
        "canonical_task": task,
        "allowed_effects": ["read"],
        "candidate_limit": len(manifests),
        "policy_requirements": {
            "approval": ["not_required"],
            "secrets": ["not_required"],
            "filesystem": ["not_required"],
            "network": ["not_required"],
        },
    })
    selected = resolution.get("selected")
    eligible = resolution.get("eligible") or []
    if resolution.get("truncated") or len(eligible) != 1 or selected != expected:
        return _refusal("resolution_drift", task, expected=expected, selected=selected)

    entry = entries[expected]
    target = entry.get("func")
    if not callable(target):
        return _refusal("registry_target_invalid", task, selected=expected)
    policy = evaluate_policy_shadow(expected, entry.get("contract"), {"allowed_effects": ["read"]})
    if policy.get("verdict") != "allow":
        return _refusal("policy_denied", task, selected=expected, verdict=policy.get("verdict"),
                        reasons=policy.get("reasons", []))
    call_args, issue = _validate_arguments(
        entry.get("schema") or {}, {} if arguments is None else arguments)
    if issue:
        return _refusal(issue.pop("code"), task, selected=expected, **issue)

    # Bind the authorization to the exact declarations used for selection.
    # No await occurs between the first projection and this check, but the
    # second projection makes registry mutation fail closed if that changes.
    current_revision = manifest_fingerprint(
        [project_contract(name, registry[name]) for name in SAFE_TASKS.values()])
    if current_revision != manifest_revision:
        return _refusal("manifest_revision_changed", task, selected=expected)
    decision = {
        "manifest_revision": manifest_revision,
        "evidence_revision": evidence_revision,
        "policy_revision": _fingerprint(policy),
    }
    decision["revision"] = _fingerprint({"task": task, "selected": expected, **decision})

    result = await target(**call_args, trace_id=trace_id)
    return {
        "schema": DISPATCH_SCHEMA,
        "ok": True,
        "authorized": True,
        "executed": True,
        "canonical_task": task,
        "selected": expected,
        "policy_verdict": "allow",
        "decision": decision,
        "result": result,
    }


async def compare_direct_parity(
    registry: Mapping[str, Mapping[str, Any]], canonical_task: str,
    arguments: Any = None, *, enabled: bool, trace_id: str | None = None,
) -> dict[str, Any]:
    """Compare direct and resolved results only for side-effect-free canaries."""
    task = str(canonical_task or "").strip()
    if task not in PARITY_TASKS:
        return _refusal("parity_task_not_supported", task)
    expected = SAFE_TASKS[task]
    entry = registry.get(expected)
    if not isinstance(entry, Mapping) or not callable(entry.get("func")):
        return _refusal("registry_target_invalid", task, selected=expected)
    contract = entry.get("contract") if isinstance(entry.get("contract"), Mapping) else {}
    idempotency = contract.get("idempotency")
    if not isinstance(idempotency, Mapping) or idempotency.get("status") != "idempotent":
        return _refusal("parity_idempotency_required", task, selected=expected)
    resolved = await resolve_authorize_dispatch(
        registry, task, arguments, enabled=enabled, trace_id=trace_id)
    if not resolved.get("ok"):
        return {**resolved, "parity": False}
    args, issue = _validate_arguments(
        entry.get("schema") or {}, {} if arguments is None else arguments)
    if issue:  # Defensive: the resolved call already validated the same schema.
        return _refusal(issue.pop("code"), task, selected=expected, **issue)
    direct_result = await entry["func"](**args, trace_id=trace_id)
    direct_hash = _fingerprint(direct_result)
    resolved_hash = _fingerprint(resolved.get("result"))
    return {
        "schema": DISPATCH_SCHEMA,
        "ok": direct_hash == resolved_hash,
        "authorized": True,
        "executed": True,
        "canonical_task": task,
        "selected": expected,
        "parity": direct_hash == resolved_hash,
        "direct_result_revision": direct_hash,
        "resolved_result_revision": resolved_hash,
        "decision": resolved["decision"],
    }
