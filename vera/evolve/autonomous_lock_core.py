"""Pure: the hard main-lockout decision for autonomous mode (closed-loop safety).

When the autonomous loop is engaged it must be IMPOSSIBLE for any agent in the
loop to reach prod: promoting/merging into the real mainline (main/master) is
refused UNCONDITIONALLY — no `authorize_main` sentinel, no `force` override. Only
a deliberate, human-confirmed release turns the lock off. This module decides that
with no I/O, so the rule is unit-tested; the capability layer reads/writes the flag
and MUST honour a `locked` verdict without any bypass.
"""
from __future__ import annotations

from typing import Dict

_ENGAGED_VALUES = {"on", "engaged", "1", "true", "yes"}


def is_engaged(state: Dict) -> bool:
    """True if autonomous mode is currently engaged, per the stored flag."""
    return str((state or {}).get("mode", "")).strip().lower() in _ENGAGED_VALUES


def main_is_locked(state: Dict, to: str, mainline: str = "main") -> Dict:
    """Whether a merge/promote into `to` is LOCKED right now.

    Locked iff autonomous mode is engaged AND `to` targets the protected mainline
    (the resolved mainline plus the conventional main/master). Returns
    {locked: bool, reason: str}. Pure and UNCONDITIONAL — callers must never let a
    sentinel or force flag bypass a locked verdict; that is the whole point.
    `bleeding-edge` and feature branches are never locked (the loop lands there
    freely), so autonomous work continues; only prod is sealed off.
    """
    if not is_engaged(state):
        return {"locked": False, "reason": ""}
    target = (to or "").strip().lower()
    protected = {(mainline or "main").strip().lower(), "main", "master"}
    protected.discard("")
    if target not in protected:
        return {"locked": False, "reason": ""}
    since = (state or {}).get("since") or "?"
    why = (state or {}).get("reason") or ""
    return {"locked": True, "reason": (
        f"AUTONOMOUS MODE ENGAGED (since {since}"
        + (f"; {why}" if why else "")
        + f"): '{to}' is the protected mainline and is LOCKED — no promote/merge to "
        "main, not even with authorize_main or force. A human must call "
        "autonomous.release (confirm=true) to regain main access.")}
