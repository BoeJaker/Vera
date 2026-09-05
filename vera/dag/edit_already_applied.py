"""An edit whose work has already been done is not a failure.

Census 34, author-then-edit. The goal is "create timer.html with a 60 second
countdown, then change the countdown to 90". The run made that change, and then
asked for it again:

    code.edit FAILED - edit 1: `find` text not present in the file
    (first 60 chars: 'let countdown = 60;').
    Closest text actually in the file - line 35: 'let countdown = 90;'

The editor was shown the CURRENT file - _v5_load_current reads the versioned
store first, so it saw the 90. What it was not told is that the task it had
been handed was already satisfied. So it did the only thing the instruction
allowed, anchored on the text the task named, and the anchor was gone because
its own earlier edit had removed it. The loop then re-ran code.edit four times
against a file that was already correct, and the goal wall-capped.

The signal is objective and needs no judgement: `find` is ABSENT and `replace`
is PRESENT. That is what an applied edit looks like from the outside, and it is
the difference between "you are editing the wrong file" and "this is done".

Deliberately narrow. A replacement must be substantial before its presence
means anything:

  * an EMPTY replace (a deletion) is in every file by definition;
  * a short one - "}", "0", "true" - occurs everywhere by accident, and
    treating a coincidence as completion would silently skip a real edit,
    which is far worse than one wasted retry;
  * a replace IDENTICAL to the find says nothing at all.

Pure: no I/O.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

#: A replacement shorter than this proves nothing by being present. Chosen
#: above the length of the punctuation and keywords that litter every file;
#: the census case ("let countdown = 90;") is 19.
MIN_EVIDENCE = 10


def already_applied(content: str, find: str, replace: str) -> bool:
    """True when this exact edit appears to have been made already.

    Requires `find` absent AND `replace` present, because either alone is
    ambiguous: an absent find is usually a wrong anchor, and a present replace
    is usually just the file containing the text the edit would add.
    """
    body = str(content or "")
    f = str(find or "")
    r = str(replace or "")
    if not f or not r or f == r:
        return False
    if len(r.strip()) < MIN_EVIDENCE:
        return False
    return f not in body and r in body


def describe(edit_no: int, replace: str) -> str:
    """What to tell the caller when an edit was already in place."""
    head = str(replace or "").strip().splitlines()[0] if str(replace or "").strip() else ""
    shown = (head[:60] + "...") if len(head) > 60 else head
    return ("edit %d was already applied - its `find` text is gone and its "
            "`replace` text is present (%r). Nothing to do; the file already "
            "says what this edit was asking for." % (edit_no, shown))


def partition(content: str, edits: Optional[List[Dict[str, Any]]]):
    """Split edits into (to_apply, already_done) against `content`.

    Returned in the caller's order so an index reported to the model still
    lines up with the edit it sent.
    """
    todo, done = [], []
    for i, e in enumerate(edits or []):
        if not isinstance(e, dict):
            continue
        find = str(e.get("find") or "")
        repl = str(e.get("replace") if e.get("replace") is not None else "")
        if already_applied(content, find, repl):
            done.append((i + 1, e))
        else:
            todo.append((i + 1, e))
    return todo, done
