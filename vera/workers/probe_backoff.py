"""Stop re-probing a host that just refused us.

The node temperature probe fans out to every registered SSH host every
TEMP_PROBE_SEC (60s) and keeps no memory of what happened last time. Measured
on prod 2026-09-04, of 28 registered hosts:

    21  OSError: [Errno 113] No route to host
     4  PermissionDenied for user root
     3  reachable

So 25 hosts that cannot answer were being dialled 1,440 times a day each. The
log had 92,928 cumulative SSH opens and was adding ~64 a minute; the Vera host
process sat at 57% CPU with no loops and no census running.

The module already backs the tool-INSTALL step off by six hours ("don't hammer
apt on a host that keeps failing"). The connection itself never got the same
treatment, which is the whole defect: the expensive part is the dialling, not
the apt-get.

Two failure classes, because they need different patience:

  * REFUSED - the credentials are wrong (PermissionDenied, auth failed). This
    cannot fix itself; only a human changing the stored credential will. Backs
    off hard and caps long.
  * UNREACHABLE - no route, connection refused, timeout. A host that is off or
    rebooting will come back on its own, so this caps at an hour: long enough
    to stop the storm, short enough that a machine coming back is noticed
    within one cap window rather than at the next restart.

Anything else is treated as transient and retried on the ordinary schedule -
an unrecognised error is not evidence that a host is dead, and silently
parking a healthy host would be worse than the storm.

Pure: no I/O, no clock of its own (callers pass `now`), no imports from the app.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: Wrong credentials. Needs a human, so wait a long time between attempts.
REFUSED = "refused"
#: Off, rebooting, or behind a dead route. May return by itself.
UNREACHABLE = "unreachable"
#: Anything we do not recognise - retried normally.
TRANSIENT = "transient"

#: First delay after a failure, per class.
BASE_SECONDS = {REFUSED: 1800.0, UNREACHABLE: 300.0}
#: Ceiling per class. REFUSED matches the existing six-hour install backoff.
MAX_SECONDS = {REFUSED: 6 * 3600.0, UNREACHABLE: 3600.0}

_REFUSED_MARKERS = (
    "permissiondenied", "permission denied", "auth failed",
    "authentication failed", "no authentication methods",
)
_UNREACHABLE_MARKERS = (
    "no route to host", "errno 113", "connection refused", "errno 111",
    "timed out", "timeout", "network is unreachable", "errno 101",
    "name or service not known", "errno -2", "host is unreachable",
)


def classify(error: Any) -> str:
    """Which kind of failure `error` is. Empty/None is TRANSIENT, never a
    reason to park a host."""
    text = str(error or "").lower()
    if not text.strip():
        return TRANSIENT
    for m in _REFUSED_MARKERS:
        if m in text:
            return REFUSED
    for m in _UNREACHABLE_MARKERS:
        if m in text:
            return UNREACHABLE
    return TRANSIENT


def record_success(state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A host that answered starts from a clean slate.

    Deliberately total amnesia: a machine that was down for a day and is now
    up must not be treated as suspect, or the backoff becomes a punishment
    rather than a rate limit.
    """
    return {}


def record_failure(state: Optional[Dict[str, Any]], now: float,
                   error: Any) -> Dict[str, Any]:
    """Fold one failure into a host's state. Returns the new state.

    Exponential in the number of CONSECUTIVE failures of the same class;
    changing class resets the count, because "refused" after "no route" is new
    information about a host that is now at least answering its port.
    """
    prev = dict(state or {})
    kind = classify(error)
    if kind == TRANSIENT:
        # Not evidence of anything durable - keep the ordinary schedule.
        return {}
    fails = int(prev.get("fails") or 0) + 1 if prev.get("kind") == kind else 1
    base = BASE_SECONDS[kind]
    cap = MAX_SECONDS[kind]
    delay = min(base * (2 ** (fails - 1)), cap)
    return {"kind": kind, "fails": fails, "delay": delay,
            "until": float(now) + delay, "error": str(error or "")[:200]}


def should_skip(state: Optional[Dict[str, Any]], now: float) -> bool:
    """Whether this host should be left alone for now."""
    if not state:
        return False
    try:
        return float(now) < float(state.get("until") or 0)
    except (TypeError, ValueError):
        return False


def describe(state: Optional[Dict[str, Any]], now: float) -> str:
    """Why a host is being skipped, for the cached entry the UI reads.

    A skipped host must not look identical to a host that was probed and came
    back empty, or the estate view silently turns into a lie.
    """
    if not should_skip(state, now):
        return ""
    remain = int(float(state.get("until") or 0) - float(now))
    mins = max(1, remain // 60)
    return ("not probed: %s (%d consecutive), retrying in ~%dm - %s"
            % (state.get("kind"), int(state.get("fails") or 0), mins,
               str(state.get("error") or "")[:120]))
