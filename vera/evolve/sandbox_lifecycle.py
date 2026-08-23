"""Pure lifecycle decisions shared by Loop Lab sandbox capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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
    if (replace_primary or not current_branch or current_branch == requested_branch
            or not status):
        return None
    return {
        "error": "primary sandbox is occupied by another branch",
        "code": "primary_occupied",
        "current_branch": current_branch,
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
