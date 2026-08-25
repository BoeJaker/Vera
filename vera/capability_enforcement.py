"""Bounded rollout policy for central capability enforcement.

The default is shadow-only.  This first enforcement slice supports exactly the
four migrated ``run.shadow`` inspection capabilities and requires two explicit
runtime flags.  Removing either flag is the kill switch.
"""

from __future__ import annotations

import os
from typing import Any


ENFORCEMENT_SCHEMA = "vera.capability-enforcement/v1"
SUPPORTED_FAMILIES = {
    "run.shadow": frozenset({
        "run.shadow.list", "run.shadow.graph", "run.shadow.get",
        "run.shadow.export",
    }),
}


class PolicyEnforcementDenied(PermissionError):
    """Raised before dispatch when an explicitly enforced call is denied."""

    def __init__(self, name: str, verdict: str):
        self.name = name
        self.verdict = verdict
        super().__init__(f"policy denied capability {name} ({verdict})")


def _selected_families(value: str) -> tuple[list[str], list[str]]:
    requested = sorted({item.strip() for item in value.split(",") if item.strip()})
    return ([item for item in requested if item in SUPPORTED_FAMILIES],
            [item for item in requested if item not in SUPPORTED_FAMILIES])


def enforcement_projection(name: str, policy: dict[str, Any], *,
                           mode_value: str | None = None,
                           families_value: str | None = None) -> dict[str, Any]:
    """Project one deterministic enforcement decision from runtime flags."""
    raw_mode = (os.getenv("VERA_POLICY_MODE", "shadow")
                if mode_value is None else mode_value)
    raw_families = (os.getenv("VERA_POLICY_ENFORCE_FAMILIES", "")
                    if families_value is None else families_value)
    mode = str(raw_mode or "shadow").strip().lower()
    configured, unsupported = _selected_families(str(raw_families or ""))
    selected = any(name in SUPPORTED_FAMILIES[family] for family in configured)
    enforce_enabled = mode == "enforce" and selected
    verdict = str(policy.get("verdict") or "indeterminate")
    would_block = selected and verdict != "allow"
    blocked = enforce_enabled and would_block
    config_valid = mode in {"shadow", "enforce"} and not unsupported
    return {
        "schema": ENFORCEMENT_SCHEMA,
        "mode": mode if mode in {"shadow", "enforce"} else "shadow",
        "config_valid": config_valid,
        "selected": selected,
        "supported_family": "run.shadow" if name in SUPPORTED_FAMILIES["run.shadow"] else "",
        "configured_families": configured,
        "unsupported_families": unsupported,
        "policy_verdict": verdict,
        "would_block": would_block,
        "blocked": blocked,
        "executed": False,
        "rollback": "set VERA_POLICY_MODE=shadow or clear VERA_POLICY_ENFORCE_FAMILIES",
    }


def enforcement_status(*, mode_value: str | None = None,
                       families_value: str | None = None) -> dict[str, Any]:
    """Return content-free rollout configuration for operators and UI."""
    raw_mode = (os.getenv("VERA_POLICY_MODE", "shadow")
                if mode_value is None else mode_value)
    raw_families = (os.getenv("VERA_POLICY_ENFORCE_FAMILIES", "")
                    if families_value is None else families_value)
    mode = str(raw_mode or "shadow").strip().lower()
    configured, unsupported = _selected_families(str(raw_families or ""))
    selected_caps = sorted({name for family in configured
                            for name in SUPPORTED_FAMILIES[family]})
    return {
        "schema": ENFORCEMENT_SCHEMA,
        "mode": mode if mode in {"shadow", "enforce"} else "shadow",
        "enabled": mode == "enforce" and bool(selected_caps),
        "config_valid": mode in {"shadow", "enforce"} and not unsupported,
        "supported_families": sorted(SUPPORTED_FAMILIES),
        "configured_families": configured,
        "unsupported_families": unsupported,
        "selected_capabilities": selected_caps,
        "rollback": "set VERA_POLICY_MODE=shadow or clear VERA_POLICY_ENFORCE_FAMILIES",
    }
