"""One browser budget per loop STEP, not per operator.run call.

`operator_budget.DEFAULT_MAX_SECONDS` caps a single `operator.run` at 480s, and
`MIN_CALLER_SECONDS` stops a caller shrinking it - both deliberate, because
every caller-supplied value observed so far came from the model and every one
was too small. Neither caps how MANY times one loop step may call it.

Census run61, build-browser-verified (1,694s, quality 1.0 - the artifact was
fine): **29 think calls, 1,109s, 35-50s each at 17.5 tok/s**, and the step
still ended

    operator.run FAILED - time_budget: stopped after 504s (budget 480s)
    having taken 11 step(s) without reaching the goal

so the executor called it again. The thinks are not slow any more (item 1 fixed
that); there are simply too many of them, and the per-call clock cannot see
that because each call starts a fresh one. Two thirds of that goal went to
verifying a page that was already correct.

So a step gets ONE allowance across every browser call it makes. When it is
spent the next call is refused with what was already learnt, the same way a
repeated failure is refused - not silently truncated, because a step that is
told why can still finish with what it has.

Pure: a tally dict in, a decision out. No clock of its own - the caller passes
the elapsed time it already measured.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

#: Seconds of browser work one loop STEP may spend in total. 600 is one full
#: `operator.run` (480) plus a short second look, and a third of a 1800s goal.
#: Overridable for a deliberately browser-heavy profile.
try:
    STEP_MAX_SECONDS = float(os.environ.get("VERA_OPERATOR_STEP_BUDGET_S", "600") or 600)
except (TypeError, ValueError):                       # pragma: no cover
    STEP_MAX_SECONDS = 600.0

#: The capabilities that drive the browser and therefore draw on the budget.
#: `operator.think` is deliberately absent - it is what the others spend their
#: time ON, so counting both would charge every second twice.
BROWSER_CAPS = frozenset({"operator.run", "operator.act", "operator.step",
                          "browser.navigate"})


def is_browser_call(tool: str) -> bool:
    return str(tool or "") in BROWSER_CAPS


def spent(tally: Optional[Dict[str, float]], step_id: Any) -> float:
    """Seconds this step has already spent driving the browser."""
    return float((tally or {}).get(str(step_id), 0.0))


def record(tally: Optional[Dict[str, float]], step_id: Any, tool: str,
           seconds: float) -> Dict[str, float]:
    """Charge `seconds` to this step. Returns the (possibly new) tally.

    Mutates in place when given a real dict, so the caller keeps one tally for
    the whole run. A non-browser call costs nothing.
    """
    out = tally if isinstance(tally, dict) else {}
    if not is_browser_call(tool):
        return out
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return out
    if s <= 0:
        return out
    key = str(step_id)
    out[key] = out.get(key, 0.0) + s
    return out


def exhausted(tally: Optional[Dict[str, float]], step_id: Any, tool: str,
              limit: float = STEP_MAX_SECONDS) -> bool:
    """Whether this browser call must be refused.

    A limit of 0 or less means no step clock - the whole mechanism off.
    """
    if not is_browser_call(tool):
        return False
    try:
        lim = float(limit)
    except (TypeError, ValueError):                   # pragma: no cover
        lim = STEP_MAX_SECONDS
    if lim <= 0:
        return False
    return spent(tally, step_id) >= lim


def describe(tool: str, spent_s: float, limit: float = STEP_MAX_SECONDS,
             last_stop: str = "") -> str:
    """What to tell the executor instead of driving the browser again."""
    tail = ""
    if str(last_stop or "").strip():
        tail = ("\nThe last run stopped because: %s"
                % str(last_stop).strip()[:200])
    return (
        "this step has already spent %ds driving a browser (the step's whole "
        "allowance is %ds), across every `%s`-style call it has made. More "
        "browser time is not going to answer this: either the page does what "
        "you are checking for and you can say so from what you have already "
        "seen, or it does not and the fix belongs in the FILE, not in another "
        "look at it. Write down what you observed and move on - report the "
        "finding, edit the file, or emit `done`.%s"
        % (int(spent_s), int(limit), str(tool), tail))


def status(tally: Optional[Dict[str, float]], step_id: Any,
           limit: float = STEP_MAX_SECONDS) -> Tuple[float, float]:
    """(spent, remaining) for this step, for an event or a log line."""
    used = spent(tally, step_id)
    try:
        lim = float(limit)
    except (TypeError, ValueError):                   # pragma: no cover
        lim = STEP_MAX_SECONDS
    return used, max(0.0, lim - used)
