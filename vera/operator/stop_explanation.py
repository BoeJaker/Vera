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

import re
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


def last_seen(steps: Optional[List[Dict[str, Any]]] = None) -> str:
    """The most recent text the PAGE actually displayed, or "".

    Steps recorded before `seen` was carried simply do not have it; those
    runs get the old explanation, not a wrong one.
    """
    for rec in reversed(list(steps or [])):
        if not isinstance(rec, dict):
            continue
        text = " ".join(str(rec.get("seen") or "").split())
        if text:
            return text
    return ""


#: How many distinct readings to quote. Enough to show load -> change -> reset,
#: which is the shape that exposes an inconsistency, without pasting a whole
#: countdown tick by tick.
MAX_TRAIL = 6


def seen_sequence(steps: Optional[List[Dict[str, Any]]] = None,
                  limit: int = MAX_TRAIL) -> List[str]:
    """The DISTINCT values the page showed, in order.

    Consecutive repeats collapse - a page sitting at 01:00 for six steps is one
    reading, not six - so what survives is the transitions, which is where the
    evidence is.
    """
    out: List[str] = []
    for rec in (steps or []):
        if not isinstance(rec, dict):
            continue
        text = " ".join(str(rec.get("seen") or "").split())
        if not text:
            continue
        if out and out[-1] == text:
            continue
        out.append(text[:120])
    if len(out) <= limit:
        return out
    # Keep the START and the END: the first reading is what it loaded as, the
    # last is where it ended up, and those two are what get compared.
    head = max(1, limit // 2)
    return out[:head] + out[len(out) - (limit - head):]


#: A reading is the whole page text ('01:30 Start Pause Reset'), so the value
#: under discussion is whatever it LEADS with. Negative parts are matched on
#: purpose: '-1:-1' is what a countdown that decrements before testing shows,
#: and it must compare as below zero rather than fail to parse.
_CLOCK_RE = re.compile(r"^\s*(-?\d+):(-?\d+)\b")
_NUM_RE = re.compile(r"^\s*(-?\d+)\b")


def leading_value(text: Any) -> Optional[int]:
    """The number a reading starts with, in seconds for mm:ss, else None."""
    s = str(text or "")
    m = _CLOCK_RE.match(s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = _NUM_RE.match(s)
    if m:
        return int(m.group(1))
    return None


def _monotonic(values: List[int]) -> bool:
    """Did it only ever go one way? Then it never came back."""
    ups = any(b > a for a, b in zip(values, values[1:]))
    downs = any(b < a for a, b in zip(values, values[1:]))
    return not (ups and downs)


def looks_like_a_return(trail: Optional[List[str]] = None) -> bool:
    """Did the value go away and come back to something ELSE?

    That is the whole claim the verdict sentence makes, and it was being
    asserted from "there is more than one reading". Three recorded cases where
    that was wrong, each on a CORRECT artifact:

      census 48 author-then-edit  '01:30' -> '00:00' -> '01:30'
          It came back to exactly where it started - the timer working.
      census 49 build-browser-verified
          'Email Validation Form Email:'
          -> 'Email Validation Form Email: Invalid email address'
          TWO readings. It changed once and stayed. You cannot go away and come
          back in two readings, so no return is even expressible here - and the
          run was told a 4/4 form was broken moments after the form displayed
          the exact behaviour its goal asked to verify.
      author-then-edit re-test     '01:30' -> '01:06' -> '00:32' -> '00:00'
          A countdown descending. It ends below where it started and never
          returns to anything. observed_note's own note predicted this one.

    So a return needs THREE things, and all three are checked here:
      * at least three readings - away, and back;
      * an end DIFFERENT from the start (a trail ending where it began refutes
        the claim outright);
      * a change of direction. A sequence that only ever descends (or only ever
        climbs) never came back, whatever its endpoints. Only applied when
        every reading yields a number, so this never guesses about prose.

    Kept true for the cases that ARE defects: census 47's
    '60' -> '01:05' -> ... -> '-1:-1' -> '01:30' turns around, and census 39's
    '01:30' -> '00:57' -> '01:00' - Reset landing on 01:00 instead of 01:30 -
    turns around too. Both still earn the sentence.
    """
    t = [x for x in (trail or [])]
    if len(t) < 3:
        return False
    if t[0] == t[-1]:
        return False
    values = [leading_value(x) for x in t]
    if all(v is not None for v in values) and _monotonic([v for v in values if v is not None]):
        return False
    return True


def observed_note(steps: Optional[List[Dict[str, Any]]] = None) -> str:
    """What the page showed, phrased so the caller can act on it.

    WHY THIS IS THE SENTENCE THAT WAS MISSING. Census 37, author-then-edit: the
    operator was asked to confirm a countdown started at 01:30, and its own
    thoughts record reading 01:00, 00:47, 00:31 off the page. 01:00 was the
    truth - `code.edit` had changed `remainingSeconds` to 90 and left the two
    DISPLAY strings at 01:00, so the file really was half-edited. The operator
    found the defect on its first observation and reported it as
    "repeating_action", because `reason` is the only channel it had.

    So the run was blamed for thrashing while the artifact was wrong, and the
    loop answered by re-running the browser three more times instead of fixing
    the file. Naming the observed value turns a stop code into a finding.

    Deliberately NOT a verdict: the text is quoted, not judged. A countdown
    caught mid-tick legitimately reads 00:47, and asserting that as "wrong"
    here would invent a defect as confidently as the old silence hid one.
    """
    seen = last_seen(steps)
    if not seen:
        return ""
    trail = seen_sequence(steps)
    # The SEQUENCE, not just the final frame. Census 39's operator watched
    # 01:30 -> 00:57 -> 01:00: it loaded correctly, counted down correctly, and
    # then Reset put it at 01:00 instead of 01:30 - which is the whole defect,
    # and is invisible in any single reading. Reporting only the last value said
    # "01:00" and left the reader to guess whether that was wrong or just a
    # countdown caught late.
    # The verdict is only earned when the comparison it describes actually
    # holds. Census 48, author-then-edit: the page displayed
    # '01:30' -> '00:00' -> '01:30' - it loaded at the edited value, ran down to
    # zero and Reset put it back exactly where it started, which is the timer
    # WORKING. The sentence fired anyway, because it was attached to "there is
    # more than one reading" rather than to "it came back to something else",
    # and told the loop the FILE was wrong about a file that then passed all
    # four of that goal's checks. The loop believed it and spent the rest of a
    # 1806s budget editing a correct file.
    #
    # The verdict now requires an actual RETURN - see looks_like_a_return.
    trail_note = ""
    if len(trail) > 1:
        verdict = (
            " A value that changes and then returns to something DIFFERENT "
            "from where it started is a defect in the file, not a browser "
            "problem.") if looks_like_a_return(trail) else ""
        trail_note = (" It displayed, in order: %s.%s"
                      % (" -> ".join(repr(t) for t in trail), verdict))
    return (" What the page actually DISPLAYED when it stopped: %r.%s Compare "
            "that with what the goal expected: if it does not match, the FILE "
            "is wrong and re-running the browser will keep reporting the same "
            "thing - fix the file instead." % (seen[:200], trail_note))


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
    out = f"{code}: {text}" if text else code
    return out + observed_note(steps)
