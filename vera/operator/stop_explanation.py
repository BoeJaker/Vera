"""Carry the operator's own account of why it stopped out to the caller.

`operator_loop` already builds a usable explanation at every stopping point. The
no-progress branch calls `operator_progress.describe()`, which says which page
did not change and what was tried; the repeat branch writes "the same action was
repeated N times on the same page with no change". Both go into the step record
- and the function returns

    {"ok": ..., "done": ..., "reason": reason, ...}

where `reason` is the bare code. So the agentic loop is handed the string
"no_progress", and nothing else.

WHAT THAT COST, census run 21, goal author-then-edit, step 3:

    cycle 3  operator.run(timer.html)  243228ms  -> no_progress
    cycle 4  operator.run(timer.html)  162733ms  -> no_progress
    cycle 5  operator.run(timer.html)  329288ms  -> no_progress

735 seconds - twelve minutes - on the same preview URL, the executor only
rewording its `goal` between attempts, because "no_progress" does not tell it
that the page never changed, that two earlier attempts already established
that, or that a fourth navigation will do the same thing. The goal hit its wall
cap at 1513s.

Same shape as O21 (a silent success read as no information), O23 (a populated
`reason` never read) and O4 (a decline reported as "no edits"): the explanation
was written, and then dropped on the way out.

`reason` stays exactly as it is - it is the machine-readable code, and the UI
and tests switch on it. This adds the sentence beside it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Stops that mean "this run did not reach the goal". `done` and `cancelled` are
# deliberately absent: the first is success, the second is the user's decision
# and needs no explaining to the model.
UNSUCCESSFUL = ("no_progress", "max_steps", "repeating_action", "too_many_errors",
                "observe_error", "think_error", "blocked", "time_budget")

#: Pages that are just a file this run wrote. Driving a browser to assert their
#: CONTENT is the expensive way to answer a cheap question.
_STATIC_SUFFIXES = (".html", ".htm", ".svg", ".txt", ".md", ".json", ".css", ".js")

STATIC_HINT = (
    " This page is a static file this run produced. A Python check can settle "
    "questions about its CONTENT - does the handler exist, does the pattern "
    "match the spec, does the markup parse - and for those, read the file FROM "
    "DISK with exec.python.run and assert on what you read. Two things it "
    "cannot do: it cannot establish BEHAVIOUR (that the error actually appears "
    "when you type) because that needs a browser to run the JS, and a script "
    "printing PASS is not evidence of anything - report what you READ, not a "
    "verdict you printed about yourself.")


def static_check_hint(url: str) -> str:
    """What a Python check CAN settle when the target is a file, and what it cannot.

    NARROWED 2026-09-01 after checking the evidence I first cited for it. I had
    claimed census run 21 "passed build-browser-verified by verifying with
    exec.python.run instead". It did not. Reading that run\'s three calls:

      cyc8   real Selenium -> Traceback, no Chrome in the sandbox. The script
             caught it, printed it and exited 0, so the cap reported ok/rc=0.
      cyc11  a regex "DOM simulator" -> IndentationError, never ran.
      cyc12  pasted a COPY of the HTML into its own source, regex-matched
             "not-an-email" in Python, and printed "SUCCESS CRITERION MET: PASS".

    The page was never loaded, the JS was never executed, and form.html was
    never read from disk - cyc12 tested a string literal in its own source. The
    step was then marked ok and the verifier agreed, quoting the script\'s own
    "SUCCESS CRITERION MET" back as its reason. A false positive end to end, and
    the run 21 census score is inflated by it.

    So the earlier wording here - "read it with exec.python.run and assert on
    the text" - licensed exactly what went wrong. The distinction that matters
    is CONTENT versus BEHAVIOUR, and reading from DISK versus inlining a copy;
    run 21 got both halves wrong.
    """
    u = str(url or "").split("?", 1)[0].split("#", 1)[0].strip().lower()
    return STATIC_HINT if u.endswith(_STATIC_SUFFIXES) else ""

# What to say when the loop stopped for a reason that left no step record to
# quote - so the caller still gets a next move rather than a bare code.
FALLBACK = {
    "max_steps": ("the run used its whole step budget without reaching the goal. "
                  "That is not evidence the goal was met"),
    "too_many_errors": ("too many actions failed in a row - the page is probably not "
                        "in the state the goal assumes"),
    "observe_error": "the page could not be observed at all",
    "think_error": "the operator model did not return a usable decision",
    "blocked": "the action was refused by the safety gate",
    "time_budget": ("the run used its whole TIME budget without reaching the goal - "
                    "this says nothing about the page, only that it was too slow to "
                    "be worth the rest of the goal's allowance"),
}


def _last_text(steps: Optional[List[Dict[str, Any]]]) -> str:
    """The explanation the loop already wrote into its final step record."""
    for rec in reversed(list(steps or [])):
        if not isinstance(rec, dict):
            continue
        for key in ("reason", "error"):
            text = str(rec.get(key) or "").strip()
            # A step record whose `reason` is just the code again adds nothing.
            if text and text not in UNSUCCESSFUL:
                return text
    return ""


def explain(reason: str, steps: Optional[List[Dict[str, Any]]] = None) -> str:
    """One sentence the caller can act on, or "" when the run succeeded.

    Empty for a successful or cancelled run - there is nothing to explain, and
    inventing a failure sentence for a run that worked would be worse than
    saying nothing.
    """
    code = str(reason or "").strip()
    if code not in UNSUCCESSFUL:
        return ""
    text = _last_text(steps) or FALLBACK.get(code, "")
    return f"{code}: {text}" if text else code
