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


def nudge_for(history) -> str:
    """The nudge line to append, or "" when none is warranted.

    Looks only at the MOST RECENT step: a claim two steps ago has already been
    nudged once, and repeating it every turn would train the model to ignore it.
    """
    items = list(history or [])
    if not items:
        return ""
    last = items[-1]
    if not isinstance(last, dict):
        return ""
    if str(last.get("action") or "") == "done":
        return ""
    return NUDGE if claims_completion(last.get("thought") or "") else ""
