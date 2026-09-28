"""A step that needs step N also reads whatever recovered step N.

A step may declare `needs: [N]`, and the step runner then gives it exactly the
results of the steps it named. When step N misses its success bar, the
`extra_step` strategy inserts a RECOVERY step with a NEW id that finishes N's
work - and nothing tied the two together: the step that needed N read N's failed
attempt and never the recovery that actually produced the output. Its context
said the work was unfinished while the files on disk said otherwise.

Recovery steps now carry `_recovers: <the original step id>` (chained, so a
recovery of a recovery still names the original), and their results carry it
too. This module picks a step's dependency results with that link followed.
Pure: the blackboard in, an ordered list out.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List


def recovers_id(step: Dict[str, Any]) -> Any:
    """The ORIGINAL step a recovery of `step` stands in for: a recovery of a
    recovery points at the first step in the line, not at the failed recovery."""
    if not isinstance(step, dict):
        return None
    r = step.get("_recovers")
    return r if r is not None else step.get("id")


def dependency_results(blackboard: Dict[Any, Dict[str, Any]],
                       needs: Iterable[Any]) -> List[Dict[str, Any]]:
    """Each needed step's result, each followed by the results of the steps that
    recovered it, in the order they ran. Empty when no needed step has run - the
    caller keeps its own fallback (every prior result) for that case."""
    bb = blackboard or {}
    out: List[Dict[str, Any]] = []
    seen = set()
    for n in (needs or []):
        if n not in bb or n in seen:
            continue
        seen.add(n)
        out.append(bb[n])
        for r in bb.values():
            if isinstance(r, dict) and r is not bb[n] and r.get("_recovers") == n \
                    and not any(r is o for o in out):
                out.append(r)
    return out
