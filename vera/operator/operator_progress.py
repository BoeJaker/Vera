"""Stop a run that is getting nowhere - not a run that is merely long.

The operator already refuses to repeat an IDENTICAL action
(`_repeat_signature` on action+args+url, limit 5). Census run 18 walked
straight past it:

    "the same action was attempted 6x: click {ref: e7}"
    thought: "Previous clicks on e7 failed after waits too"
    thought: "Multiple clicks have been timing out (e9, e7, e15)"

Six clicks on one element, each timing out, interleaved with waits and scrolls -
so no two consecutive actions were byte-identical and the guard never fired. It
ran to `max_steps` and reported nothing useful, twice, for 21 minutes.

A step limit is the wrong instrument for this. A run doing real work should be
allowed to take as many turns as it needs; the thing worth stopping is a run
whose actions stop CHANGING anything. So judge the PAGE, not the action:

    url + page title + the set of interactive element refs

If that triple is unchanged across N consecutive acts, the operator is not
making progress, whatever it is clicking. A page that navigates, re-renders,
opens a dialog, or reveals a new control has changed - and the counter resets.

Deliberately NOT counted as progress: the action being different. Clicking a
different dead element is the exact behaviour observed, and treating it as
progress is what let this run to the ceiling.

Deliberately generous: the default tolerance is 5 unchanged acts, so a page
needing a few waits before it responds is never cut short. This never shortens
a run that is doing something; it ends one that demonstrably is not.

Pure: no I/O, no browser. The caller passes what it observed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional

#: Consecutive acts with no observable page change before a run is stopped.
#: Generous on purpose - the aim is ending a dead run, not policing a slow one.
DEFAULT_TOLERANCE = 5

STOP_REASON = "no_progress"


def page_signature(*, url: str = "", title: str = "",
                   refs: Optional[Iterable[str]] = None) -> str:
    """A stable digest of what the page IS, independent of what was done to it.

    Element refs are sorted, so the same controls in a different enumeration
    order are the same page - otherwise a re-render with identical content
    would read as progress and reset the counter forever.
    """
    payload = json.dumps({
        "url": str(url or "").strip(),
        "title": str(title or "").strip(),
        "refs": sorted({str(r).strip() for r in (refs or []) if str(r).strip()}),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]


#: Actions whose whole purpose is to make the page do something. Only these
#: count as a stall when the page does not move.
#:
#: Typing, selecting and scrolling are deliberately EXCLUDED: filling five form
#: fields legitimately leaves url/title/controls identical, and counting those
#: would stop a working run - the precise thing this must not do. They do not
#: reset the counter either; they simply are not evidence either way.
CHANGE_SEEKING_ACTIONS = frozenset({
    "click", "navigate", "goto", "submit", "press", "back", "forward", "reload",
})


def update(state: Optional[Dict[str, Any]], signature: str,
           last_action: Optional[str] = None) -> Dict[str, Any]:
    """Fold one observation into the progress state.

    ``{"last": sig, "stalled": n}``. The observation reflects the page AFTER
    `last_action`, so a stall is counted only when a CHANGE-SEEKING action left
    the page identical. `last_action=None` (the first observation, or an action
    that asserts nothing) never counts.
    """
    prev = dict(state or {})
    last = prev.get("last")
    stalled = int(prev.get("stalled") or 0)

    if last is None or signature != last:
        return {"last": signature, "stalled": 0}       # the page moved

    act = str(last_action or "").strip().lower()
    if act in CHANGE_SEEKING_ACTIONS:
        return {"last": signature, "stalled": stalled + 1}
    return {"last": signature, "stalled": stalled}      # neither evidence


def should_stop(state: Optional[Dict[str, Any]],
                tolerance: int = DEFAULT_TOLERANCE) -> bool:
    """True when the page has not changed for `tolerance` consecutive acts."""
    if not state:
        return False
    try:
        tol = int(tolerance)
    except Exception:
        tol = DEFAULT_TOLERANCE
    if tol <= 0:                       # tolerance 0 disables the guard entirely
        return False
    return int(state.get("stalled") or 0) >= tol


def describe(state: Optional[Dict[str, Any]], tolerance: int = DEFAULT_TOLERANCE,
             recent_actions: Optional[List[str]] = None) -> str:
    """Why the run stopped, in terms the caller can act on.

    Names what did NOT change, because "ran out of steps" was the message that
    made run 18's two failures indistinguishable from a run that was simply
    long.
    """
    n = int((state or {}).get("stalled") or 0)
    msg = (f"stopped after {n} consecutive actions that changed nothing on the "
           f"page - same URL, same title, same controls. The page is not "
           f"responding to what is being tried")
    if recent_actions:
        uniq = []
        for a in recent_actions[-6:]:
            if a and a not in uniq:
                uniq.append(str(a))
        if uniq:
            msg += f" (recent actions: {', '.join(uniq)})"
    return msg + ". This is not evidence the goal was reached."
