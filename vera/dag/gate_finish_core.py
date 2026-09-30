"""What a finished loop may claim - the final gate's last word, carried to the end.

Census 2026-09-21..30: in 16 of 119 sessions the final completion gate said the
goal was NOT met ("missing: pause, resume, reset"; "missing: timer.html with a
90-second countdown"), yet the loop ended with reason 'complete' and the
deliverable claimed the missing parts ("Pause/Resume features", "updated to
01:30"). The gate's verdict never reached the synthesiser, the delivery agent or
the done event.

Rule: the goal is still open when the LAST gate said incomplete and no
follow-up step ran after it (a follow-up that ran may have closed the gap, and
the loop simply had no rounds left to re-check - then nothing is claimed either
way). Pure.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

MAX_ITEMS = 8


def unmet(last_gate: Optional[Dict[str, Any]], ran_after: int = 0) -> List[str]:
    """The parts of the goal the final gate found missing, or [] when the gate
    passed, never ran, or work ran after its verdict."""
    g = last_gate or {}
    if not g or g.get("complete") or int(ran_after or 0) > 0:
        return []
    items = [str(m).strip() for m in (g.get("missing") or []) if str(m).strip()]
    if not items:
        items = ["the final completion check did not confirm the goal was met"]
    return items[:MAX_ITEMS]


def note(items: List[str]) -> str:
    """The instruction the synthesiser and the delivery agent receive."""
    if not items:
        return ""
    return ("\nNOT DONE - the run's final completion check found these parts of the goal "
            "NOT met. Say so plainly in the result (what is missing and that it was not "
            "done); never describe them as done or present:\n"
            + "\n".join("- %s" % m for m in items) + "\n")


def reason(items: List[str]) -> str:
    return "incomplete" if items else "complete"
