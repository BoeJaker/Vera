"""Which edit unbalanced the markup - not just that something did.

`code.edit` applies anchored find/replace edits, then syntax-checks the result.
When an HTML file comes back unbalanced the checker says:

    the edited file no longer parses - html-structure: unbalanced <script> tags
    - 1 opening vs 2 closing </script>. Every <script> must be closed exactly
    once.

True, and useless: the editor sent six edits and is told only that the FILE is
broken, so its retry re-derives all six from scratch and reproduces the fault.
Census run 20 did exactly that three times - 267s, 267s and 1193s - and the goal
hit its wall cap having produced nothing. Same family as the missing-anchor hint
(edit_anchor_hint) and the discarded decline note (editor_reply): the
information needed to fix it exists and nobody hands it over.

The culprit is identifiable without guessing, because each edit's effect on tag
balance is arithmetic. An edit is balance-neutral when

    (opens - closes) in `replace`  ==  (opens - closes) in `find`

Adding a whole new balanced <script>...</script> block is neutral. Deleting a
balanced block is neutral. What is NOT neutral is anchoring on a bare `<script>`
and replacing it with a block that carries its own `</script>` while the file's
original closing tag stays where it was - which is the edit that broke census
run 20, and which this names.

Measured 2026-08-31, qwen2.5-coder:14b editing a 74-line index.html: three of
seven applied edit batches unbalanced a tag this way, across both a whole-file
task and a narrow one. It is not rare and it is not specific to big requests.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

# Tags whose imbalance actually breaks a document rather than merely offending a
# linter. `<script`/`<style` are matched as prefixes so attributes are counted
# (`<script src=...>`); the closing forms are literal.
TAG_PAIRS = (
    ("script", re.compile(r"<script\b", re.I), re.compile(r"</script\s*>", re.I)),
    ("style", re.compile(r"<style\b", re.I), re.compile(r"</style\s*>", re.I)),
)


def edit_tag_delta(find: str, replace: str) -> Dict[str, int]:
    """Net change in (opens - closes) this edit makes, per tag. 0 == neutral."""
    out: Dict[str, int] = {}
    f, r = str(find or ""), str(replace or "")
    for name, open_re, close_re in TAG_PAIRS:
        before = len(open_re.findall(f)) - len(close_re.findall(f))
        after = len(open_re.findall(r)) - len(close_re.findall(r))
        if after - before:
            out[name] = after - before
    return out


def _phrase(name: str, delta: int) -> str:
    if delta > 0:
        return (f"leaves {delta} more <{name}> opening tag(s) than closing ones"
                if delta > 1 else f"leaves an unclosed <{name}>")
    n = -delta
    return (f"leaves {n} more </{name}> closing tag(s) than opening ones"
            if n > 1 else f"leaves a </{name}> with nothing opening it")


def unbalancing_edits(edits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The edits that change tag balance on their own, with their deltas."""
    found: List[Dict[str, Any]] = []
    for i, e in enumerate(edits or []):
        if not isinstance(e, dict):
            continue
        delta = edit_tag_delta(e.get("find", ""), e.get("replace", ""))
        if delta:
            found.append({"edit_no": i + 1, "delta": delta,
                          "find_preview": str(e.get("find") or "")[:60]})
    return found


def describe(edits: List[Dict[str, Any]]) -> str:
    """The line to hand back to the editor. Empty when nothing is unbalanced -
    the caller must then fall back to the generic parse error, because the fault
    is somewhere this cannot see."""
    rows = unbalancing_edits(edits)
    if not rows:
        return ""
    parts = []
    for row in rows:
        what = "; ".join(_phrase(n, d) for n, d in sorted(row["delta"].items()))
        anchor = " ".join(row["find_preview"].split())[:60]
        parts.append(f"edit {row['edit_no']} (anchored on {anchor!r}) {what}")
    return (("EDIT " if len(parts) == 1 else "EDITS ")
            + "THAT BROKE THE TAG BALANCE: " + "; ".join(parts)
            + ". Either anchor on the WHOLE block including its closing tag, or "
              "do not re-emit the tag at all - leave the existing one in place "
              "and edit only what is inside it.")
