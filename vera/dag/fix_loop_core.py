"""A failure that two fix steps did not change ends the run (plan item 28).

Census run77 build-multifile (25 Sep 2026): the model's own test for mode()
disagreed with its own implementation. With verifies now honest (item 18b),
the step was unmet, the controller inserted "Debug and Fix Mode Failure", then
"Inspect Test Output to Isolate Root Cause" - 43 executor cycles, the SAME
failing test in every verify, and the run headed for its wall cap. Before 18b
the step was accepted on an edit's parser verdict: dishonest, but 793 s.

The rule: the failing test's identity is the failure's signature. When it
survives the original step and two remediation steps unchanged, the run has
shown it cannot fix it; it stops and REPORTS the failure - no further insert,
no gate follow-up, the deliverable names it. Pure: the ledger in, a verdict out.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Tuple

#: Unmet results carrying one signature before the run stops (original + 2 fixes).
BOUND = 3
_QUOTED_RE = re.compile(r'"([^"]{4,240})"')
_TEST_ID_RE = re.compile(r"([\w./-]+::[\w\[\]-]+(?:::[\w\[\]-]+)*)")


def failure_signature(reason: object) -> str:
    """The identity of a test failure named in a verify reason, else ''."""
    r = str(reason or "")
    if not re.search(r"reported failures|FAILED|failed", r):
        return ""
    m = _TEST_ID_RE.search(r)
    if m:
        return m.group(1).rsplit("/", 1)[-1].lower()
    q = _QUOTED_RE.search(r)
    if q:
        line = re.sub(r"\s+", " ", q.group(1)).strip().lower()
        line = re.sub(r"\[\s*\d+%\s*\]", "", line).strip()
        return line[:120]
    return ""


def same_failure_streak(results: Iterable[Dict[str, Any]]) -> Tuple[int, str]:
    """(count, signature): how many unmet results in a row carry the same failure."""
    streak, sig = 0, ""
    for r in results or []:
        if not isinstance(r, dict) or r.get("met") is not False:
            continue
        s = failure_signature(r.get("met_reason") or r.get("reason") or "")
        if not s:
            continue
        if s == sig:
            streak += 1
        else:
            streak, sig = 1, s
    return streak, sig


def should_stop(streak: int, bound: int = BOUND) -> bool:
    try:
        return int(streak) >= int(bound)
    except (TypeError, ValueError):
        return False


def report(signature: str, streak: int) -> str:
    return (f"the test failure '{signature}' survived {streak} attempts unchanged (the step and "
            f"{streak - 1} fix step(s)); the run stops here and reports it rather than loop to its "
            "wall cap. The deliverable must say the tests do not pass and name this failure.")
