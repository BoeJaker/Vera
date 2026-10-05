"""Noticing that the operator has already finished.

Census 30, run 344edbb13e, goal "Verify inline validation behavior in a real
browser". Fifteen steps. At step 4 its own thought said

    "We've verified inline validation is working - the textbox has ..."

and at step 5

    "The form is showing an error message 'Please enter a valid e ..."

It had done the job and said so. It then spent eleven more steps clicking and
retyping into the same field, and ended on max_steps - which is reported as a
FAILURE, so a run that succeeded is recorded as one that ran out.

Across the 19 most recent runs only 3 ended in `done`: 5 max_steps, 4
time_budget, 3 too_many_errors, 2 no_progress, 2 repeating_action. Stopping is
not a rare edge case the operator gets wrong; it is the normal outcome.

Two things were feeding it, and BOTH are prompt shape rather than reasoning:

  * the reply template showed ``"done": false`` pre-filled on every single
    turn. A slot shown with an answer already in it gets copied - measured
    directly on this codebase the same day, where an editor placeholder
    "<the exact text to replace>" came back as an edit's replacement.
  * nothing ever referred back to what the model had just concluded. Its own
    "we've verified it" scrolled past in a history line that recorded only the
    action and its result, never the thought.

This module supplies the second half: a nudge, fired when the previous thought
claimed success and the action taken was not `done`. Deliberately a NUDGE and
not an automatic stop - a thought is a claim, not evidence, and ending a run on
a model's say-so is exactly the false-positive the verifier gate exists to
prevent. It only asks the question; the model still answers it.

Pure: no I/O.
"""

from __future__ import annotations

import re

#: Phrases that assert the job is done, as opposed to describing progress.
#: Kept narrow on purpose: "I need to verify" and "to verify this" are the
#: INTENT to check, not a finding, and must not fire.
_CLAIM_RE = re.compile(
    r"\b("
    r"(?:we|i)(?:'ve| have)\s+(?:now\s+)?(?:verified|confirmed|validated|observed)"
    r"|(?:has|have|is|was|are)\s+been\s+(?:verified|confirmed|validated)"
    r"|goal\s+(?:is\s+)?(?:achieved|met|complete|completed|satisfied)"
    r"|successfully\s+(?:verified|confirmed|validated|demonstrated)"
    r"|(?:this\s+)?confirms\s+(?:that\s+)?"
    r"|verification\s+(?:is\s+)?complete"
    r"|both\s+requirements\s+(?:are\s+)?(?:met|verified|confirmed)"
    r")",
    re.IGNORECASE)

#: Phrases that cancel a claim - the model saying it still has work to do.
#: Checked AFTER the claim, because "we've verified X, now I need to check Y"
#: is progress, not completion.
_PENDING_RE = re.compile(
    r"\b(still\s+need|now\s+i\s+need|next\s+i|remains?\s+to|not\s+yet|"
    r"need\s+to\s+(?:also|still)|second\s+requirement)\b",
    re.IGNORECASE)

#: Appended to the next prompt when a claim went unacted on. One line, phrased
#: as a question rather than an instruction, so a model that has NOT finished
#: is not pushed into saying it has.
NUDGE = (
    "NOTE: your previous thought said the goal appears to be verified. If it "
    "genuinely is, reply with action \"done\" and a one-line summary of what "
    "you saw that proves it. If it is not, say what is still missing and "
    "continue.")


def claims_completion(thought: str) -> bool:
    """Whether `thought` asserts the goal has been met.

    A claim followed by an explicit "but I still need ..." does not count -
    that is a model narrating progress, and nudging it to stop would be wrong.
    """
    text = str(thought or "")
    if not text.strip():
        return False
    if not _CLAIM_RE.search(text):
        return False
    return not _PENDING_RE.search(text)


#: The page looked the same after this many consecutive actions...
UNCHANGED_RUN = 3
#: ...at least one of which was meant to change it. Typing and selecting are left
#: out on purpose: filling form fields legitimately leaves the visible text alone.
CHANGE_ACTIONS = frozenset({"click", "press"})

NUDGE_UNCHANGED = (
    "NOTE: the page has shown exactly the same thing ({seen!r}) after each of your last "
    "{n} actions, including a {act}. If the GOAL asks you to confirm that something "
    "changes, you already have the answer: it did NOT change. Reply with action \"done\" "
    "and report the value you saw and that it stayed the same. Continue only if a "
    "genuinely DIFFERENT action is still untried.")


def unchanged_nudge(history) -> str:
    """A check whose answer is "nothing happened", made explicit.

    A broken countdown (Start does nothing), prod 2026-10-03, operator A/B: the model
    noted the handler was commented out, then clicked Start six times until the repeat
    guard stopped it at 370 s with no answer - while the page read "00:30 Start" after
    every single action. A frozen page after a click IS the observation the goal asked
    for. Like NUDGE this only asks; the model still decides.
    """
    tail = [h for h in list(history or [])[-UNCHANGED_RUN:] if isinstance(h, dict)]
    if len(tail) < UNCHANGED_RUN:
        return ""
    seen = [" ".join(str(h.get("seen") or "").split()) for h in tail]
    if not seen[0] or len(set(seen)) != 1:
        return ""
    acts = [str(h.get("action") or "") for h in tail]
    if "done" in acts:
        return ""
    act = next((a for a in acts if a in CHANGE_ACTIONS), "")
    if not act:
        return ""
    return NUDGE_UNCHANGED.format(seen=seen[0][:80], n=UNCHANGED_RUN, act=act)


def nudge_for(history) -> str:
    """The nudge line to append, or "" when none is warranted.

    Looks only at the MOST RECENT step: a claim two steps ago has already been
    nudged once, and repeating it every turn would train the model to ignore it.
    Failing that, a page that has not moved across the last few actions (see
    unchanged_nudge).
    """
    items = list(history or [])
    if not items:
        return ""
    last = items[-1]
    if not isinstance(last, dict):
        return ""
    if str(last.get("action") or "") == "done":
        return ""
    if claims_completion(last.get("thought") or ""):
        return NUDGE
    return unchanged_nudge(items)


_GENERIC_DONE = re.compile(r"^\W*(goal\s+(is\s+)?(complete|completed|achieved|met|done)|done|complete|"
                           r"completed|success(ful)?|verified|task\s+complete)\W*$", re.IGNORECASE)


def evidence_summary(summary: str, history, current_seen: str = "", limit: int = 4) -> str:
    """A `done` whose summary says nothing ("goal complete") gets what the page showed.

    Prod 2026-10-03 operator A/B: three of six runs on a working countdown ended `done`
    with the summary "goal complete" - the loop that asked "report the values you saw"
    got no values. The page text after each action is already in the history (`seen`);
    the distinct values in order ARE the report. A summary with any substance is kept as
    it is."""
    s = str(summary or "").strip()
    if s and not _GENERIC_DONE.match(s):
        return s
    trail = []
    for h in list(history or []) + [{"seen": current_seen}]:
        v = " ".join(str((h or {}).get("seen") or "").split())[:80] if isinstance(h, dict) else ""
        if v and (not trail or trail[-1] != v):
            trail.append(v)
    if not trail:
        return s or "goal complete"
    return "%s. Observed on the page, in order: %s" % (
        s or "goal complete", " -> ".join(repr(v) for v in trail[-limit:]))
