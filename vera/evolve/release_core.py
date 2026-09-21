"""The prod release, gated on the census - pure decisions.

A release is the two acts that put bleeding-edge on prod: fast-forward
`main` to the edge, then re-exec the process. The merge disturbs nothing;
the restart kills every loop in flight, and the census gate inside the
restart PAUSES the census goal (it is cancelled and re-run later). Until
2026-09-21 nothing checked for a census before a release; this module is
that check. A pending release waits in one of two ways:

  finish  - wait until no census is in flight at all (the harness is done,
            dropped, absent or dead); the census is never touched.
  goal    - write a YIELD: the goal in flight finishes untouched, the
            harness parks before the next goal, the release goes out, and
            the census is resumed once the new process is up.
  force   - do not wait: release now (the restart's own gate pauses and
            re-runs the goal, as it always did). The override.

Nothing here reads a file, Redis or the clock: `census` is the /health
census summary (`control.health_summary`), `now` and the timestamps are
aware ISO strings, and `decide` returns the one thing to do next.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

MODES = ("finish", "goal", "force")
STATUSES = ("waiting", "releasing", "restarting", "done", "failed", "cancelled")
BUSY_STATES = ("running", "yielding", "pausing")
CLEAR_STATES = ("none", "done", "dropped", "stale")
PARKED_STATES = ("yielded", "paused")
RESTART_TIMEOUT_S = 15 * 60       # a restart that has not come back in this long failed
MAX_WAIT_S = 12 * 3600            # a release nobody cancels stops waiting after this


def parse_iso(s: str) -> Optional[datetime]:
    t = str(s or "").strip()
    if not t:
        return None
    try:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def may_release(mode: str, census: Dict[str, Any]) -> Tuple[bool, str]:
    """(ok, why) - may the merge+restart go out now under `mode`?"""
    if mode == "force":
        return True, "forced"
    state = str((census or {}).get("state") or "none")
    busy = bool((census or {}).get("busy"))
    if busy or state in BUSY_STATES:
        goal = (census or {}).get("goal") or ""
        return False, f"census goal in flight ({goal or state})"
    if mode == "goal":
        if state in PARKED_STATES or state in CLEAR_STATES:
            return True, f"census {state}"
        return False, f"census {state}"
    # finish: a parked census is somebody's pause, not a finished census.
    if state in CLEAR_STATES:
        return True, f"census {state}"
    return False, f"census {state} (parked; waiting for it to end, or force)"


def new_pending(*, edge: str, mode: str, by: str, reason: str, restart: bool,
                now: datetime, release_id: str) -> Dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"census mode must be one of {', '.join(MODES)}")
    return {"id": release_id, "edge": edge, "mode": mode, "by": by, "reason": reason,
            "restart": bool(restart), "status": "waiting", "requested_at": now.isoformat(timespec="seconds"),
            "yield_written": False, "waits": 0, "last_why": "", "updated_at": now.isoformat(timespec="seconds")}


def decide(pending: Optional[Dict[str, Any]], census: Dict[str, Any], *, now: datetime,
           process_started_at: Optional[datetime]) -> Dict[str, Any]:
    """The next action for a pending release:
      {"action": "none"} nothing pending / terminal
      {"action": "yield"}  goal mode, yield not yet written
      {"action": "wait", "why": ...}
      {"action": "release", "why": ...}   merge (+ restart) now
      {"action": "finish", "resume_census": bool}   the new process is up
      {"action": "fail", "why": ...}
    """
    if not pending or pending.get("status") in ("done", "failed", "cancelled"):
        return {"action": "none"}
    status = pending.get("status")
    requested = parse_iso(pending.get("requested_at") or "")
    if status == "waiting":
        if requested and now - requested > timedelta(seconds=MAX_WAIT_S):
            return {"action": "fail", "why": f"waited longer than {MAX_WAIT_S // 3600} h; cancel or force"}
        if pending.get("mode") == "goal" and not pending.get("yield_written"):
            state = str(census.get("state") or "none")
            if state in BUSY_STATES:
                return {"action": "yield"}
        ok, why = may_release(pending.get("mode", "finish"), census)
        return {"action": "release" if ok else "wait", "why": why}
    if status == "releasing":
        return {"action": "wait", "why": "release in progress"}
    if status == "restarting":
        asked = parse_iso(pending.get("restart_requested_at") or "")
        if process_started_at and asked and process_started_at > asked:
            return {"action": "finish", "resume_census": bool(pending.get("yield_written"))}
        if not pending.get("restart", True):
            return {"action": "finish", "resume_census": bool(pending.get("yield_written"))}
        if asked and now - asked > timedelta(seconds=RESTART_TIMEOUT_S):
            return {"action": "fail", "why": "the restart did not come back"}
        return {"action": "wait", "why": "restarting"}
    return {"action": "none"}


def describe(pending: Optional[Dict[str, Any]], census: Dict[str, Any]) -> str:
    if not pending:
        return "no release pending"
    st = pending.get("status")
    if st == "waiting":
        _, why = may_release(pending.get("mode", "finish"), census)
        return f"release of {pending.get('edge')} waiting ({pending.get('mode')}): {why}"
    return f"release of {pending.get('edge')}: {st}"
