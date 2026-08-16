"""Pure decision core for the closed-loop orchestrator (M7 Phase B).

Given the current board + autonomous-mode state, decide the SINGLE next action the
orchestrator should take — dispatch a ready item into a container, or hold/idle when
there is nothing to dispatch. Pure so the decision is unit-tested; the capability
layer executes it, and ONLY when autonomous mode is engaged (Phase A) — which also
locks main, so the loop can never reach prod.

Operator directive (2026-08-16): the empty-board fallback does NOT run the v7 (or
any v1–v8) agentic loop — when the board is dry the orchestrator simply idles and
waits for work to be added.
"""
from __future__ import annotations

from typing import Dict, List, Optional

# Lanes with work waiting to START — an orchestrator picks from these.
READY_LANES = ("ready", "inbox")
# Lanes meaning the item is actively being worked (never re-dispatch these).
IN_FLIGHT_LANES = ("in_progress", "in_progress_vera")


def is_dispatchable(item: Dict, my_agent: str) -> bool:
    """A board item the orchestrator may dispatch NOW: in a ready lane, not already
    dispatched (no linked pipeline), and either unclaimed or already claimed by us.
    An item another agent holds is theirs — never dispatched here (that is how two
    machines on the same board avoid double-working an item)."""
    if (item.get("lane") or "").strip() not in READY_LANES:
        return False
    if (item.get("pipeline") or "").strip():
        return False
    agent = (item.get("agent") or "").strip()
    if agent and agent != (my_agent or "").strip():
        return False
    return True


def pick_dispatchable(items: List[Dict], my_agent: str) -> Optional[Dict]:
    """First dispatchable item (list order = caller's priority); None if none."""
    for it in items or []:
        if is_dispatchable(it, my_agent):
            return it
    return None


def next_action(items: List[Dict], engaged: bool, my_agent: str = "claude_code",
                config: Dict = None) -> Dict:
    """Decide the orchestrator's next action. Pure. Returns {action, reason, ...}:

      not engaged            -> 'blocked'  (HARD interlock: the loop must never run
                                            without the Phase-A lock engaged)
      a dispatchable item    -> 'dispatch' (+ item, title, lane)
      nothing to dispatch    -> 'idle'     (wait for work; NEVER auto-run v7 or any
                                            v1–v8 loop — operator directive)
    """
    if not engaged:
        return {"action": "blocked",
                "reason": "autonomous mode is not engaged — engage it (which LOCKS main) "
                          "before the orchestrator will act"}
    item = pick_dispatchable(items or [], my_agent)
    if item:
        return {"action": "dispatch", "item": item.get("id"),
                "title": item.get("title", ""), "lane": item.get("lane"),
                "reason": f"dispatch ready board item {item.get('id')}"}
    in_flight = any((it.get("lane") or "").strip() in IN_FLIGHT_LANES
                    for it in (items or []))
    return {"action": "idle",
            "reason": ("no dispatchable items; work is in flight — waiting"
                       if in_flight else "board has no ready work — idle (waiting; v7 "
                       "auto-run is disabled)")}
