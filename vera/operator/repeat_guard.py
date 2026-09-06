"""repeat_guard.py -- stop a run that keeps doing the same thing to a page whose
STRUCTURE never answers.

Census 35, author-then-edit. Three operator.run calls, 1452s between them, and
all three failed the same way:

    run 9c81d67747 (14 steps, time_budget): click e1 x7, click e3 x4, click e2
    run 15a94be192 (10 steps, time_budget): click e1 x6, click e2, click e3

No two identical attempts were ever ADJACENT -- e1, wait, e1, e3, e1, e1, e2 --
so operator_loop's consecutive guard (``same_sig_count + 1 if sig == last_sig
else 1``) reset on every alternation and never fired. operator.trace saw it
perfectly and said so ("the same action was attempted 7x"); nothing acted on it.

Why the OTHER guard did not catch it either is the part worth recording.
operator_progress stops a run whose page stops changing, and until 2026-09-05
that would have caught exactly this. Then page_signature was given the page's
TEXT, so that a countdown ticking 01:30 -> 00:54 would read as progress rather
than as a dead page. It does -- and the same change means a run thrashing on a
LIVE page can no longer be seen by that guard at all, because the clock keeps
the signature moving no matter what the operator does. Fixing the false
negative opened a false positive-shaped hole underneath it.

So the instrument here is deliberately the page's STRUCTURE -- url, title and
element refs, with the text left out. That is the half a click is supposed to
change:

  * a countdown ticking on its own moves the TEXT and not the structure, so a
    click that does nothing still counts as a repeat here (runs 1 and 3);
  * "load more" / pagination / a wizard advancing adds or replaces elements, so
    the structure moves and the count resets -- legitimate repetition of the
    identical action stays safe, which is the property operator_loop's URL-keyed
    signature was protecting and this must not lose.

Counting is scoped to a structural EPOCH: when the structure changes the counts
are dropped, because a click that reshapes the page is working.

Pure: no browser, no I/O, no clock.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

#: How many times one identical (action, args) may be attempted while the page
#: structure stays put. Five, not four, on purpose: operator_loop's consecutive
#: guard already stops five in a row, and test_an_interrupted_streak_resets pins
#: that four attempts broken by another action is tolerated. Matching that
#: ceiling makes this strictly a generalisation of the existing rule -- the same
#: budget, no longer defeated by alternating -- rather than a tightening that
#: could stop runs the old guard allowed.
DEFAULT_LIMIT = max(2, int(os.getenv("VERA_OPERATOR_REPEAT_TOTAL", "5") or 5))

STOP_REASON = "repeating_action"


def signature(action: str, args: Optional[Dict[str, Any]]) -> str:
    """Identity of an attempt: the verb AND its arguments.

    Without the args "click x7" cannot be told from seven clicks on seven
    different elements, which is progress -- the ambiguity that made census run
    15 hard to read. Key order must not change identity, so args are sorted.
    """
    try:
        a = json.dumps(args or {}, sort_keys=True, default=str)[:400]
    except Exception:
        a = str(args)[:400]
    return "%s|%s" % (str(action or ""), a)


def page_key(url: str = "", title: str = "", refs=None) -> str:
    """The page's STRUCTURE -- deliberately not its text. See the module note."""
    try:
        r = ",".join(str(x) for x in (refs or []))
    except Exception:
        r = str(refs)
    return "%s|%s|%s" % (str(url or ""), str(title or ""), r)


def update(state: Optional[Dict[str, Any]], key: str,
           action: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Record one attempt against structural ``key``. Returns the new state.

    A changed key starts a fresh epoch: the previous counts are dropped rather
    than carried, so a run that reshapes the page keeps its full allowance.
    """
    sig = signature(action, args)
    st = state if isinstance(state, dict) else None
    if st is None or st.get("page") != key:
        st = {"page": key, "counts": {}, "last": ""}
    else:
        st = {"page": key, "counts": dict(st.get("counts") or {}), "last": ""}
    counts = st["counts"]
    counts[sig] = int(counts.get(sig, 0)) + 1
    st["last"] = sig
    return st


def count(state: Optional[Dict[str, Any]]) -> int:
    """How many times the most recent attempt has been made this epoch."""
    if not isinstance(state, dict):
        return 0
    return int((state.get("counts") or {}).get(state.get("last") or "", 0))


def should_stop(state: Optional[Dict[str, Any]], limit: int = DEFAULT_LIMIT) -> bool:
    return count(state) >= max(2, int(limit or DEFAULT_LIMIT))


def describe(state: Optional[Dict[str, Any]], limit: int = DEFAULT_LIMIT) -> str:
    """Say what repeated and what stayed still -- the next reader of this stop
    should not have to reconstruct it from the step list."""
    n = count(state)
    sig = (state or {}).get("last") or ""
    verb = sig.split("|", 1)[0] or "the same action"
    args = sig.split("|", 1)[1] if "|" in sig else ""
    return ("%s was attempted %d times (limit %d) without the page's structure "
            "changing -- same url, same title, same elements%s. A click that "
            "changes nothing about the page is not going to start working on "
            "the next try, so the run stopped rather than spending the rest of "
            "its budget on it." % (verb, n, max(2, int(limit or DEFAULT_LIMIT)),
                                   (" (args %s)" % args) if args else ""))
