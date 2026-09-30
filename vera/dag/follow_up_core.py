"""When the final gate's follow-up step is redundant - the pure rule.

The gate proposes follow-up steps for what is still missing; a batch can hold
two steps for the SAME gap, so a step whose files an EARLIER step in the batch
already created is skipped. The old test was only "every file the step names
exists now" - which is also true of every step that EDITS an existing file.
Seen in the census 2026-09-30 (author-then-edit, broad-stepwise): the gate said
"missing: timer.html with a 90-second countdown" three rounds running, proposed
"Change initial time to 90 seconds" each time, and each was skipped as
"already satisfied ... timer.html" - the edit never ran, the loop ended
'complete' and the report claimed the change.

Redundant now means: every named file exists now AND did not exist when the
batch began (so a step in this batch made it). Unknown (a probe that could not
decide) is never evidence either way. Pure: dicts in, bool out.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional


def redundant(paths: Iterable[str], before: Optional[Dict[str, bool]],
              now: Optional[Dict[str, bool]]) -> bool:
    """True only when each path exists now and was known ABSENT at the
    batch's start. No paths -> False (a step that names no file always runs)."""
    ps = [p for p in (paths or []) if p]
    if not ps:
        return False
    b = before or {}
    n = now or {}
    return all(n.get(p) is True and b.get(p) is False for p in ps)
