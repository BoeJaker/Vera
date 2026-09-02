"""An operator run has a step budget and no clock, so a step budget is no budget.

`run_loop` stops at `max_steps` (default 15). Each step is observe -> think ->
act, and `think` is a full model generation, so 15 steps is 15 generations plus
browser time. Nothing bounds how long that takes.

CENSUS 23, build-browser-verified, step 2, one single operator.run call:

    cyc4   operator.run(...)   1,344,677 ms   -> max_steps

Twenty-two minutes, for one call, against a 1800s goal budget. The goal had
already spent its whole allowance before its second attempt. The rest of that
step went the same way - 505s, 117s, 380s, 397s - and the goal ran past its wall
cap entirely.

`max_steps` cannot express this. A run doing useful work in 15 quick steps and a
run stuck on a page that takes 90s per think are the same number of steps; only
the clock separates them, and there was no clock.

A time budget is also the honest unit for the caller. The agentic loop gives a
goal a wall budget in SECONDS and the census caps it in SECONDS; an operator
call that cannot say how long it might take cannot be scheduled against either.

DEFAULT: 8 minutes. Long enough for a genuinely slow page (the observed
legitimate runs finish inside 400s) and short enough that a stuck run leaves the
goal most of its budget. Checked BEFORE each step rather than after, so the
budget is a promise about when the run RETURNS, not a limit it may overshoot by
one whole generation.
"""

from __future__ import annotations

from typing import Optional, Tuple

#: Seconds a single operator.run may spend before it gives the budget back.
DEFAULT_MAX_SECONDS = 480

#: The stop reason, alongside max_steps / no_progress / repeating_action.
STOP_REASON = "time_budget"

#: The least a CALLER may ask for. Exposing max_seconds on the cap means the
#: model can now set it, and it does: census 24, author-then-edit cyc7 passed
#: max_seconds=95 and the run died at 103s having managed five steps of a task
#: that needed a click and a two-second wait. One observe -> think -> act cycle
#: contains a full generation, so a budget under a few of them cannot finish
#: anything and only converts a slow run into a failed one. A caller may tighten
#: the budget, but not below the point where the run is doomed.
MIN_CALLER_SECONDS = 180.0


def caller_budget(requested: float, default_s: float = DEFAULT_MAX_SECONDS) -> float:
    """The budget to actually use for a caller-supplied value.

    0 or absent means "no preference" and takes the default. Anything positive
    is honoured, but floored - a model asking for 95s is expressing urgency, not
    a considered estimate of how long a browser needs.
    """
    try:
        req = float(requested or 0)
    except (TypeError, ValueError):
        return float(default_s)
    if req <= 0:
        return float(default_s)
    return max(MIN_CALLER_SECONDS, req)


def budget_kwargs(requested: float,
                  default_s: float = DEFAULT_MAX_SECONDS) -> dict:
    """The ``max_seconds`` kwargs to hand run_loop for a caller's value.

    Empty when the caller expressed no preference, so run_loop's own default
    applies rather than this module's copy of it drifting apart from it.
    """
    try:
        req = float(requested or 0)
    except (TypeError, ValueError):
        return {}
    if req <= 0:
        return {}
    return {"max_seconds": caller_budget(req, default_s)}


def remaining(started_at: float, now: float,
              max_seconds: float = DEFAULT_MAX_SECONDS) -> float:
    """Seconds left. Negative once overspent."""
    try:
        return float(max_seconds) - (float(now) - float(started_at))
    except (TypeError, ValueError):
        return float(max_seconds)


def exhausted(started_at: float, now: float,
              max_seconds: float = DEFAULT_MAX_SECONDS) -> bool:
    """Whether the run must stop now.

    A max_seconds of 0 or less means no clock - the caller opted out, which is
    the right behaviour for a deliberate long-running drive.
    """
    try:
        if float(max_seconds) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    return remaining(started_at, now, max_seconds) <= 0


def describe(elapsed_s: float, max_seconds: float = DEFAULT_MAX_SECONDS,
             steps_done: int = 0) -> str:
    """Why it stopped, in terms the caller can act on."""
    try:
        el = int(float(elapsed_s))
    except (TypeError, ValueError):
        el = 0
    return (f"stopped after {el}s (budget {int(max_seconds)}s) having taken "
            f"{int(steps_done)} step(s) without reaching the goal. This is a "
            f"TIME limit, not evidence about the page - the run was making "
            f"progress too slowly to be worth the goal's remaining budget")
