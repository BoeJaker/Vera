"""The controller's steer is context for the executor, never part of a task.

After every step the controller appends its direction to the NEXT step's goal
as a "CONTROLLER STEER (after step N, alignment: ...): ..." block. The
executor reads that goal, and when it authors a file it copies the goal -
steer included - into `task`; the missing-task heal copies the goal on
purpose. The author then writes a script whose stated purpose is the steer
("Confirm that the Python script successfully executed... CONTROLLER STEER
(after step 1...)": census run70-73, 24 Sep 2026, six times, yielding files
like verify_statistical_output.py). Pure: strings in, strings out.
"""
from __future__ import annotations

import re

STEER_RE = re.compile(r"\s*CONTROLLER STEER \(after step [^)]*\):.*\Z", re.S)


def has_steer(text: object) -> bool:
    return bool(STEER_RE.search(str(text or "")))


def strip_steer(text: object) -> str:
    """`text` without a trailing controller-steer block; unchanged otherwise."""
    return STEER_RE.sub("", str(text or "")).rstrip()
