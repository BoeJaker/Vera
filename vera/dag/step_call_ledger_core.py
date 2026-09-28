"""The executor sees every call its step already made, not only the last four.

The step executor rebuilds its prompt every cycle and shows the model its
results as `history[-4:]` - the four most recent calls in full. A step that runs
10-18 cycles (census build-multifile's test step, runs 70-78) therefore loses
sight of its own early calls: by cycle 6 the pytest that failed in cycle 1 is
gone from the prompt, and the model issues it again. The repeat guard then
refuses the call ("already failed this way twice - not re-run"), which stops the
waste but not the forgetting.

This renders the calls OLDER than the four shown in full as one line each - the
tool, its (masked, bounded) arguments, and whether it worked, with the first line
of the error when it did not - so the model can see what it already tried. Pure:
the step's history in, a block of text out. Empty until the step has made more
calls than are shown in full, so a short step's prompt is unchanged.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

#: Calls shown in full by the executor (its `history[-4:]`).
SHOWN_IN_FULL = 4
#: Older calls listed, most recent last; anything before is counted, not listed.
MAX_LINES = 16
#: Hard ceiling on the whole block, so a long step cannot crowd the prompt.
MAX_CHARS = 1800
_LINE_DETAIL = 140

HEADER = ("EARLIER CALLS IN THIS STEP (older than the results below - already made. "
          "Do NOT repeat one that FAILED unless you change what caused the failure; "
          "reuse what one that worked returned):\n")


def _first_line(text: Any) -> str:
    for ln in str(text or "").splitlines():
        ln = ln.strip()
        if ln:
            return ln
    return ""


def earlier_calls_block(history: List[Dict[str, Any]], *,
                        shown: int = SHOWN_IN_FULL,
                        summarise: Optional[Callable[[Any], str]] = None,
                        max_lines: int = MAX_LINES,
                        max_chars: int = MAX_CHARS) -> str:
    """One line per call older than the `shown` most recent, or '' when there are none."""
    hist = [h for h in (history or []) if isinstance(h, dict)]
    if len(hist) <= shown:
        return ""
    earlier = hist[:-shown] if shown > 0 else hist
    omitted = max(0, len(earlier) - max_lines)
    lines: List[str] = []
    for idx, h in enumerate(earlier[omitted:], start=omitted + 1):
        tool = str(h.get("tool") or "?")
        try:
            args = summarise(h.get("args")) if summarise else ""
        except Exception:
            args = ""
        head = f"  call {idx}: {tool}" + (f"({args})" if args else "")
        detail = _first_line(h.get("preview"))[:_LINE_DETAIL]
        if h.get("ok"):
            lines.append(head + " -> ok" + (f": {detail}" if detail else ""))
        else:
            lines.append(head + " -> FAILED" + (f": {detail}" if detail else ""))
    body = "\n".join(lines)
    note = f"  ({omitted} earlier call(s) not listed)\n" if omitted else ""
    block = HEADER + note + body
    if len(block) > max_chars:
        # Keep the MOST RECENT lines: drop from the oldest end, whole lines only.
        keep: List[str] = []
        room = max_chars - len(HEADER) - 60
        for ln in reversed(lines):
            if len(ln) + 1 > room:
                break
            keep.insert(0, ln)
            room -= len(ln) + 1
        dropped = omitted + (len(lines) - len(keep))
        block = HEADER + f"  ({dropped} earlier call(s) not listed)\n" + "\n".join(keep)
    return block
