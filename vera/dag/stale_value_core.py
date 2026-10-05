"""An edit that changes a value must change what the page SHOWS too.

Census default-style-auto-run1 (3 Oct 2026), author-then-edit: "Create timer.html with a
60 second countdown, then change the countdown to 90 seconds". code.edit changed
`const countdownTime = 60 * 1000;` and the <title>, and left the initial display at
`01:00`. The file parsed, so the verifier's fast path passed the step; the goal scored
0.75 ("the DISPLAYED value was updated too"). The same miss recurs across every style.

A bare-number search would be useless - a timer is full of legitimate 60s (`t / 60`,
`% 60`). So this looks only for the OLD value's DISPLAYED forms left in lines the edit did
not touch: its MM:SS rendering (60 -> `01:00`, `1:00`), `60 seconds` / `60s` / `60 min`,
and a bare text node `>60<`. Pure: two strings in, findings out.
"""

from __future__ import annotations

import difflib
import re
from typing import Dict, List, Tuple

_NUM = re.compile(r"(?<![\w.])\d+(?![\w.])")
MAX_FINDINGS = 3


def changed_numbers(before: str, after: str) -> Tuple[List[Tuple[int, int]], set]:
    """(old, new) number pairs the edit changed, and the AFTER line numbers it touched.
    Numbers are paired by position inside each replaced block; a block whose number count
    changed is skipped rather than guessed."""
    a, b = before.splitlines(), after.splitlines()
    pairs: List[Tuple[int, int]] = []
    touched: set = set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        touched.update(range(j1, j2))
        if tag != "replace":
            continue
        old = [int(x) for x in _NUM.findall("\n".join(a[i1:i2]))]
        new = [int(x) for x in _NUM.findall("\n".join(b[j1:j2]))]
        if len(old) != len(new):
            continue
        for o, n in zip(old, new):
            if o != n and (o, n) not in pairs:
                pairs.append((o, n))
    return pairs, touched


def display_forms(old: int, new: int) -> List[Tuple[str, "re.Pattern[str]"]]:
    """How `old` would read on a page, as (label, pattern). Only forms that `new`
    does not share (a 60 -> 600 change keeps no `>60<` false alarm on `>600<`)."""
    forms: List[Tuple[str, "re.Pattern[str]"]] = []
    if 60 <= old < 6000:
        m, s = divmod(old, 60)
        for txt in {"%02d:%02d" % (m, s), "%d:%02d" % (m, s)}:
            forms.append((txt, re.compile(r"(?<![\d:])%s(?![\d:])" % re.escape(txt))))
    forms.append(("%d seconds" % old, re.compile(
        r"(?<![\w.])%d\s*(?:s|sec|secs|second|seconds)\b" % old, re.I)))
    forms.append(("%d minutes" % old, re.compile(
        r"(?<![\w.])%d\s*(?:min|mins|minute|minutes)\b" % old, re.I)))
    forms.append((">%d<" % old, re.compile(r">\s*%d\s*<" % old)))
    # Text that ENDS on the value: `Seconds Remaining: 60</div>` (probe 3 Oct, e364ef40).
    forms.append(("%d before a closing tag" % old, re.compile(r"(?<![\w.])%d\s*</" % old)))
    return forms


_TASK_FROM_TO = re.compile(r"\bfrom\s+(\d+)\b[^\n.;]{0,40}?\bto\s+(\d+)\b", re.I)


def task_pairs(task: str) -> List[Tuple[int, int]]:
    """(old, new) value changes the edit's TASK names: "from 60 seconds to 90 seconds".

    Probe 3 Oct (e364ef40, author-then-edit): the editor turned `- 1` into `- 90 / 60`
    and never touched the 60 the page shows, so the diff had no 60 -> 90 pair to
    follow. The task said it plainly."""
    out: List[Tuple[int, int]] = []
    for m in _TASK_FROM_TO.finditer(task or ""):
        o, n = int(m.group(1)), int(m.group(2))
        if o != n and (o, n) not in out:
            out.append((o, n))
    return out


def stale_display_values(before: str, after: str, task: str = "") -> List[Dict[str, object]]:
    """The old value still DISPLAYED after an edit changed it: [{old, new, form, line, text}].
    Pairs come from the diff (checked outside the lines the edit touched) and from the
    edit's task (checked everywhere - the edit may never have reached that value)."""
    pairs, touched = changed_numbers(before, after)
    from_task = [p for p in task_pairs(task) if p not in pairs]
    if not pairs and not from_task:
        return []
    lines = after.splitlines()
    out: List[Dict[str, object]] = []
    for old, new in [(p, True) for p in pairs] + [(p, False) for p in from_task]:
        old, new, skip_touched = old[0], old[1], new
        if old < 2:
            continue                              # 0/1 are everywhere and mean nothing
        for label, pat in display_forms(old, new):
            for i, ln in enumerate(lines):
                if (skip_touched and i in touched) or not pat.search(ln):
                    continue
                out.append({"old": old, "new": new, "form": label, "line": i + 1,
                            "text": " ".join(ln.split())[:120]})
                if len(out) >= MAX_FINDINGS:
                    return out
    return out


MARKER = "STALE OLD VALUE"


def describe(findings: List[Dict[str, object]]) -> str:
    """One message for the edit result (and the verifier, which looks for MARKER)."""
    if not findings:
        return ""
    f0 = findings[0]
    where = "; ".join("line %s: %s" % (f["line"], f["text"]) for f in findings)
    return ("%s: this edit changed %s -> %s, but the page still SHOWS the old value (%s) - %s. "
            "Update the displayed value too, or the change is only half done."
            % (MARKER, f0["old"], f0["new"], f0["form"], where))
