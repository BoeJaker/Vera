"""Pure lifecycle decisions shared by Loop Lab sandbox capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


PROTECTED_BRANCHES = {"bleeding-edge", "loop-lab/bleeding-edge-mirror"}


def classify_sandbox(*, docker_observable: bool, container_status: str,
                     worktree_exists: bool) -> str:
    """Return one unambiguous lifecycle state from read-only observations."""
    if not docker_observable:
        return "docker_unreachable"
    status = str(container_status or "").strip().lower()
    if not worktree_exists:
        return "missing_worktree"
    if not status:
        return "stale_descriptor"
    if status == "running":
        return "healthy"
    if status == "paused":
        return "paused"
    return "exited"


def lifecycle_preflight(descriptor: Mapping[str, Any], *, action: str,
                        docker_observable: bool, container_status: str,
                        worktree_exists: bool, dirty: bool | None,
                        merged_to_bleeding_edge: bool | None) -> dict[str, Any]:
    """Fail-closed decision for a lifecycle action; never performs mutation."""
    action = str(action or "").strip().lower()
    branch = str(descriptor.get("branch") or "")
    pinned = bool(descriptor.get("pinned"))
    protected = pinned or branch in PROTECTED_BRANCHES
    state = classify_sandbox(
        docker_observable=docker_observable,
        container_status=container_status,
        worktree_exists=worktree_exists,
    )
    reasons: list[str] = []
    if action not in {"restart", "stop", "reuse", "remove", "reconcile"}:
        reasons.append("unknown_action")
    if not docker_observable:
        reasons.append("docker_state_unknown")
    if protected and action in {"stop", "reuse", "remove", "reconcile"}:
        reasons.append("protected_sandbox")
    if action == "restart":
        if not container_status:
            reasons.append("container_missing")
        if not worktree_exists:
            reasons.append("worktree_missing")
    elif action == "stop" and not container_status:
        reasons.append("container_missing")
    elif action in {"reuse", "remove", "reconcile"}:
        if dirty is not False:
            reasons.append("worktree_not_proven_clean")
        if merged_to_bleeding_edge is not True:
            reasons.append("branch_not_proven_merged")
        if action == "reuse" and not worktree_exists:
            reasons.append("worktree_missing")
        if action == "remove" and not worktree_exists:
            reasons.append("worktree_missing")
        if action in {"remove", "reconcile"} and container_status:
            reasons.append("container_still_present")
        if action == "reconcile" and state not in {"stale_descriptor", "missing_worktree"}:
            reasons.append("descriptor_not_stale")
    return {
        "action": action,
        "allowed": not reasons,
        "reasons": reasons,
        "state": state,
        "protected": protected,
        "dry_run": True,
        "mutated": False,
    }


def primary_replacement_conflict(
    current: Mapping[str, Any] | None,
    requested_branch: str,
    container_status: str,
    replace_primary: bool = False,
) -> dict[str, Any] | None:
    """Describe an unsafe primary replacement, or return ``None`` when safe."""
    current = current or {}
    current_branch = str(current.get("branch") or "").strip()
    requested_branch = str(requested_branch or "").strip()
    status = str(container_status or "").strip().lower()
    if replace_primary or not status or (current_branch and current_branch == requested_branch):
        return None
    return {
        "error": "primary sandbox is occupied by another branch",
        "code": "primary_occupied",
        "current_branch": current_branch or "(unknown)",
        "requested_branch": requested_branch,
        "container_status": status,
        "hint": ("use evolve.sandbox.spawn for an additive sandbox; only pass "
                 "replace_primary=true when the current owner has explicitly released it"),
    }


def resolve_restart_target(
    primary: Mapping[str, Any] | None,
    pool: Mapping[str, Mapping[str, Any]],
    *,
    primary_name: str,
    name: str = "",
    branch: str = "",
) -> dict[str, Any]:
    """Resolve one exact sandbox while preserving whether it is primary or spawned."""
    primary = dict(primary or {})
    name = str(name or "").strip()
    branch = str(branch or "").strip()
    if not name and not branch:
        if not primary:
            return {"error": "no primary sandbox descriptor", "code": "sandbox_not_found"}
        return {**primary, "role": "primary", "name": primary_name}
    if primary and (name == primary_name or (branch and branch == primary.get("branch"))):
        return {**primary, "role": "primary", "name": primary_name}
    for slug, descriptor in pool.items():
        item = dict(descriptor or {})
        if name in (str(item.get("name") or ""), str(slug)) or (
                branch and branch == item.get("branch")):
            return {**item, "role": "spawned", "slug": slug}
    return {
        "error": f"no sandbox matching name='{name}' branch='{branch}'",
        "code": "sandbox_not_found",
    }
