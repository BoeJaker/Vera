"""A goal classified as complex must not be planned as one step.

## What the census actually shows

O11 was written up as "the tier heuristic picks `simple` for a short sentence
describing a big build". That is true of the pokedex chat goal — but it is NOT
what has been failing in the census, and the data says so plainly. Every
recorded run of `build-multifile` ("Create a small Python package at
/workspace/statkit with stats.py providing mean, median and mode, plus a test"):

    run1            complex  planned=4  →  done
    run10-prefixes  complex  planned=4  →  done
    run13-partial   complex  planned=4  →  done
    run6-stalled    complex  planned=3  →  done
    run9-partial    complex  planned=3  →  done
    run11           complex  planned=1  →  executed 7, wall-cap
    run12-partial   complex  planned=1  →  executed 8, gate added 3, wall-cap
    run14b-partial  complex  planned=1  →  executed 5, gate added 2

The tier is `complex` EVERY time. The tier classifier is not the problem here.
The planner intermittently returns a ONE-step plan for the same goal at the same
tier, and when it does, the completion gate ends up reconstructing the plan a
step at a time — which is how a goal that takes 420s when planned properly takes
1500s and hits the wall cap when planned as one step.

## Why a guard rather than a better prompt

The planner already produces a good plan for this goal most of the time. There is
no wording to fix: the same prompt yields four steps and one step on different
runs. What is missing is a check that the plan it returned is *shaped like* the
tier it was planned for — and a re-plan when it is not, which is machinery the
loop already has (the drift guard does exactly this).

## Why one step, specifically

Zero steps is already handled: the runner falls through to STEPWISE mode with a
bootstrap step. Two or more steps is a decomposition, whatever its quality. The
gap is exactly one step, and only above the `simple` tier — a `simple` goal
planned as one step is usually correct (`build-simple-code` is planned=1 and
finishes in 200s, run after run).

The existing `_v5_split_compound_single_step` covers a narrow slice of this: a
lone step that mixes a RESEARCH cap with a BUILD cap and has a prose seam. A pure
build goal like `build-multifile` never trips it, which is why this went
unguarded.

Pure: no app imports. Ranks are passed in rather than imported so this module
cannot drift from the runner's own tier ordering.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple


def is_underdecomposed(*, tier_rank: Optional[int], complex_rank: int,
                       steps: Sequence[Dict[str, Any]]) -> Tuple[bool, str]:
    """True when a plan is too flat for the tier it was planned at.

    `tier_rank`/`complex_rank` come from the caller's own tier ordering, so this
    cannot disagree with it. Returns (verdict, reason) — the reason is recorded
    on the event, because a silent re-plan would be one more thing that happens
    to a run without explaining itself.
    """
    n = len(steps or [])
    if tier_rank is None:
        return (False, "")
    if tier_rank < complex_rank:
        # A `simple` goal planned as one step is usually right: build-simple-code
        # is planned=1 and finishes in ~200s, run after run.
        return (False, "")
    if n != 1:
        # 0 steps has its own path (STEPWISE bootstrap); 2+ is a decomposition.
        return (False, "")
    return (True, "a goal classified at or above the complex tier was planned as "
                  "ONE step; observed live, that leaves the completion gate to "
                  "rebuild the plan a step at a time and the run hits its wall cap")


def better_plan(original: Sequence[Dict[str, Any]],
                retry: Sequence[Dict[str, Any]]) -> bool:
    """Accept a re-plan only if it actually decomposed further.

    The drift guard's rule, in this module's terms: a retry that returns the same
    flat shape is no better than what it replaced, and swapping to it would burn
    a planning call for nothing while making the run look like it recovered.
    """
    return len(retry or []) > len(original or [])
