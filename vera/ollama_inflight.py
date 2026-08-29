"""Reconcile Ollama routing slots against requests that never came back.

`in_use` per instance is the router's load signal: pick_instance sorts by it, so
a node that appears busy is avoided. It is incremented before each request and
released in a `finally` - correct accounting, PROVIDED the coroutine resumes.

It does not always resume. Observed 2026-08-28: two loop runs stalled at their
first LLM call, and afterwards the router showed in_use=2 while there were no
loop sessions, the GPU gate was free, and every model on every node had passed
its keep-alive expiry - i.e. Ollama was doing nothing. The requests had been
issued and never returned, so the `finally` never ran and the counter stayed
held. Cancelling the loop does not help: the cancel flag is only observed
BETWEEN awaits, and a coroutine blocked on an HTTP read never reaches one. Every
later run then routes around nodes that are actually idle, and with enough
stuck slots the router has nowhere to send work.

Nothing swept it. This module is that sweep, kept pure so the decision - which
slots are too old to still be real - is testable without a cluster.

Deliberately NOT a heuristic about node health: it only reclaims slots older
than a bound the caller derives from the generation timeout, so a legitimately
long generation is never stolen out from under itself.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

# A slot may outlive the generation timeout by this much before it is presumed
# lost. Generous on purpose: reclaiming a LIVE request's slot would let a second
# request pile onto a node already working, which is the failure this exists to
# prevent, not cause.
DEFAULT_GRACE_S = 120.0


def stale_ids(inflight: Dict[str, float], now: float, max_age_s: float) -> List[str]:
    """Request ids whose slot has been held longer than `max_age_s`.

    `inflight` maps request id -> monotonic start time. Entries with a
    non-numeric or future start are treated as fresh: a clock oddity must not
    cause a reclaim.
    """
    out: List[str] = []
    if not inflight or max_age_s <= 0:
        return out
    for rid, started in list(inflight.items()):
        try:
            age = float(now) - float(started)
        except (TypeError, ValueError):
            continue
        if age > max_age_s:
            out.append(str(rid))
    return out


def reconcile(in_use: int, inflight: Dict[str, float], now: float,
              max_age_s: float) -> Tuple[int, List[str]]:
    """(new_in_use, reclaimed_ids) after dropping slots that are too old.

    `in_use` is only ever DECREASED, and never below zero or below the number of
    slots still legitimately held. Other subsystems (media slots) share this
    counter, so this must not recompute it from `inflight` alone - it subtracts
    exactly what it reclaimed and leaves the rest untouched.
    """
    reclaimed = stale_ids(inflight, now, max_age_s)
    for rid in reclaimed:
        inflight.pop(rid, None)
    try:
        cur = int(in_use or 0)
    except (TypeError, ValueError):
        cur = 0
    return max(0, cur - len(reclaimed)), reclaimed


def max_age_for(timeout_s: float, grace_s: float = DEFAULT_GRACE_S) -> float:
    """The age past which a slot is presumed lost, from the request timeout."""
    try:
        t = float(timeout_s or 0)
    except (TypeError, ValueError):
        t = 0.0
    return max(60.0, t) + max(0.0, float(grace_s or 0))
