"""A browser result that says `done` closes the step's browser work (plan item 24a).

Census run70-73 (24 Sep 2026), every browser goal:

    run71 build-browser-verified  c8 operator.run -> {"ok": true, "done": true,
                                  "summary": "Goal complete - ..."}; then c9, c10,
                                  c11, c12 operator.run again - all refused by the
                                  step's time budget, four executor turns for nothing.
    run72 build-browser-verified  c13 done -> c14, c15 refused.
    run73 build-browser-verified  c3 done -> c4 a REAL second run, 234 s, same answer.
    run73 author-then-edit        c5 done -> c6 a REAL second run, 552 s, time_budget.

The operator's done result IS the step's answer: it says what the browser
observed. The executor re-issuing the browser after it is the same fixation
the duplicate-call short-circuit already handles for other tools, and it is
treated the same way: the done result is served back once with a note, and a
second re-issue ends the step on that result.

Pure: dicts and strings in, strings and booleans out.
"""
from __future__ import annotations

from typing import Any

#: A re-issue after the done result is served once with a note; the next ends the step.
MAX_SERVED = 1
SUMMARY_MAX = 1200


def done_summary(result: Any) -> str:
    """The summary of a browser result that reported the goal done, else ''."""
    if not isinstance(result, dict):
        return ""
    if result.get("done") is not True or result.get("ok") is False:
        return ""
    text = str(result.get("summary") or result.get("reason") or "done").strip()
    return text[:SUMMARY_MAX]


def should_end(served: int) -> bool:
    """After MAX_SERVED re-issues have been served with a note, the next one ends the step."""
    try:
        return int(served) > MAX_SERVED
    except (TypeError, ValueError):
        return False


def describe(tool: str, summary: str) -> str:
    """What to tell the executor instead of driving the browser again."""
    return (
        "the browser has ALREADY answered this step: your earlier `%s` came back done. Its "
        "report is the step's evidence - use it. Do not run the browser again for this step; "
        "say what it observed and emit `done` (or, if its report shows the FILE is wrong, "
        "edit the file).\n\nWhat the browser reported:\n%s"
        % (str(tool), str(summary or "").strip()[:SUMMARY_MAX]))
