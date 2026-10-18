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
from typing import List, Any, Dict, Optional, Tuple

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


#: While a release waits for running gates it holds this key (refreshed each release tick, so
#: it lapses on its own if the release is cancelled or prod dies): a gate that has not started
#: yet waits for the release instead of starting, so the queue cannot starve the release.
DRAIN_KEY = "vera:evolve:release:drain"
DRAIN_TTL_S = 120


def waiting_on_gates(why: Any) -> bool:
    """Is this hold reason a running-gates or protected-runs hold (the release drains new starts)?"""
    return "gate(s) running" in str(why or "") or "protected test run(s) going" in str(why or "")


def live_loop_ids(tasks: Any) -> List[str]:
    """Session ids of agent-loop tasks still running in this process (the dag module's
    _AGENT_LOOP_TASKS: sid -> asyncio.Task) - what a restart would kill."""
    out: List[str] = []
    try:
        for sid, task in list((tasks or {}).items()):
            try:
                if task is not None and not task.done():
                    out.append(str(sid))
            except Exception:
                continue
    except Exception:
        return []
    return out


def may_release(mode: str, census: Dict[str, Any], restart: bool = True) -> Tuple[bool, str]:
    """(ok, why) - may the merge (+ restart, unless restart=False) go out now under `mode`?
    A merge WITHOUT a restart kills nothing, so running loops and gates do not hold it
    (4 Oct 2026: a no-restart merge sat waiting on two gates and held the drain key, which
    made every new gate wait too)."""
    if mode == "force":
        return True, "forced"
    state = str((census or {}).get("state") or "none")
    busy = bool((census or {}).get("busy"))
    if busy or state in BUSY_STATES:
        goal = (census or {}).get("goal") or ""
        return False, f"census goal in flight ({goal or state})"
    # A live agent loop dies with the process. 4 Oct 2026 15:16: a release restarted prod
    # 24 min into a probe loop (strategic-vector-db-report); the run was lost and left
    # marked 'running', which then blocked every later probe. Census-only holds missed it.
    loops = [str(x) for x in ((census or {}).get("loops") or []) if str(x).strip()] if restart else []
    if loops:
        return False, ("%d agent loop(s) running (%s) - a restart would kill them; waiting "
                       "(force to override)" % (len(loops), ", ".join(x[:8] for x in loops[:3])))
    # A running gate dies with the process too (4 Oct 2026: four restarts in 80 min each
    # threw away every gate in flight). New gates hold for the release (DRAIN_KEY), so the
    # wait is bounded by the gates already running.
    gates = [g for g in ((census or {}).get("gates") or []) if isinstance(g, dict)] if restart else []
    if gates:
        return False, ("%d gate(s) running (%s) - a restart would kill them; new gates wait for "
                       "this release (force to override)"
                       % (len(gates), ", ".join(str(g.get("branch") or g.get("id"))[:40] for g in gates[:3])))
    # A protected scheduled test run dies with the process too (owner, 18 Oct 2026: tests "protected like the
    # censuses are"; that morning a release killed the memory census mid-run). Bounded by each entry's
    # protect_minutes; a person can still stop one (evolve.schedule.stop_run) or force the release.
    prot = [p for p in ((census or {}).get("protected") or []) if isinstance(p, dict)] if restart else []
    if prot:
        return False, ("%d protected test run(s) going (%s) - a restart would kill them; new protected runs wait "
                       "for this release (stop one with evolve.schedule.stop_run, or force)"
                       % (len(prot), ", ".join(str(p.get("title") or p.get("schedule_id"))[:40] for p in prot[:3])))
    if mode == "goal":
        if state in PARKED_STATES or state in CLEAR_STATES:
            return True, f"census {state}"
        return False, f"census {state}"
    # finish: a parked census is somebody's pause, not a finished census.
    # ...and a census that is DUE right now is the next arm of a pair about to start.
    # 4 Oct 2026 00:51: a release waited out the intent-core OFF arm, restarted prod in
    # the 23 s before the ORDER arm began, and the pair ran on two builds (1cb3c8ba vs
    # 1d9d47de) - no longer a comparison. `due` comes from the scheduler (census_summary).
    due = [str(x) for x in ((census or {}).get("due") or []) if str(x).strip()]
    if due and state in CLEAR_STATES:
        return False, ("census due now (%s) - waiting so a pair's arms run on one build"
                       % ", ".join(due[:2]))
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
        ok, why = may_release(pending.get("mode", "finish"), census,
                              restart=pending.get("restart", True) is not False)
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
        _, why = may_release(pending.get("mode", "finish"), census,
                             restart=pending.get("restart", True) is not False)
        return f"release of {pending.get('edge')} waiting ({pending.get('mode')}): {why}"
    return f"release of {pending.get('edge')}: {st}"


# ── node sync after a release (2026-09-28) ────────────────────────────────────
# A release changes what prod RUNS; the node-side services (nlp_server,
# gpu_inference, model_builder) and the node workers are shipped copies of
# the same tree, and until now every release left them on the old version
# until someone ran provision.component.sync by hand. With `sync_nodes` (the
# default) the release queues one sync that the release job carries out once
# the release is done - after the restart, in the NEW process, so the host
# compares nodes against the code it actually runs - and never while a census
# goal is in flight (a redeploy drops the census's NLP calls).
SYNC_STALE_S = 45 * 60            # a sync "running" this long without an update died with its process
SYNC_MAX_ATTEMPTS = 3


def new_node_sync(*, release_id: str, commit: str, now: datetime,
                  resume_census: bool = False) -> Dict[str, Any]:
    return {"release_id": release_id, "commit": commit, "status": "pending", "attempts": 0,
            "requested_at": now.isoformat(timespec="seconds"),
            "updated_at": now.isoformat(timespec="seconds"), "last_why": "", "results": {},
            "resume_census": bool(resume_census)}


def goal_yield_needed(census: Dict[str, Any]) -> bool:
    """A census=goal release parks the census only when one is actually in flight. A yield
    written over an idle census is never lifted by anyone - the release resumes only a
    census it parked - and it blocks every later census slot (6 Oct 2026: release
    8061bbc5 yielded an idle census, nothing ran for two days)."""
    return str((census or {}).get("state") or "none") in BUSY_STATES


def carry_census_resume(old: Optional[Dict[str, Any]], new: Dict[str, Any]) -> Dict[str, Any]:
    """A new node-sync record replacing one that still owes a census resume inherits that
    duty: back-to-back releases replaced the record and the parked census was never let
    go (6-7 Oct 2026)."""
    if old and old.get("resume_census") and not new.get("resume_census"):
        new = dict(new)
        new["resume_census"] = True
        new["resume_inherited_from"] = str(old.get("release_id") or "")
    return new


def hold_census_for_sync(pending: Dict[str, Any]) -> bool:
    """Whether the release keeps the census it parked parked until the node
    sync has run (2026-10-05). Resuming it first meant the sync then found a
    goal in flight and waited for the whole census - hours with the nodes on
    the old commit. Only a yield the release wrote itself is held."""
    return bool(pending.get("sync_nodes", True)) and bool(pending.get("yield_written"))


def node_sync_releases_census(rec: Optional[Dict[str, Any]], why: str) -> bool:
    """A held census is let go once the sync can no longer run: it finished,
    failed or gave up. Never left parked behind a sync that will not come."""
    if not rec or not rec.get("resume_census"):
        return False
    return str(rec.get("status") or "") in ("done", "failed") or str(why).startswith("gave up")


def node_sync_due(rec: Optional[Dict[str, Any]], census: Dict[str, Any], *, now: datetime,
                  running_here: bool) -> Tuple[bool, str]:
    """(run now?, why) for the queued node sync."""
    if not rec:
        return False, "nothing queued"
    status = rec.get("status")
    if status in ("done", "failed"):
        return False, f"sync {status}"
    if running_here:
        return False, "running"
    if status == "running":
        upd = parse_iso(rec.get("updated_at") or "")
        if upd and now - upd < timedelta(seconds=SYNC_STALE_S):
            return False, "running in another process"
    if int(rec.get("attempts") or 0) >= SYNC_MAX_ATTEMPTS:
        return False, "gave up after %d attempts" % SYNC_MAX_ATTEMPTS
    busy = bool((census or {}).get("busy")) or str((census or {}).get("state") or "") in BUSY_STATES
    if busy:
        return False, "census goal in flight - the sync waits for it"
    return True, "release done, census clear"


def node_sync_outcome(results: Dict[str, Dict[str, Any]]) -> Tuple[str, str]:
    """(status, why) from the per-step results: done when every step ran
    (a node that was already current, or skipped, is fine); pending again when
    a step was refused only because a census started; failed otherwise."""
    bad = {k: v for k, v in (results or {}).items() if not v.get("ok")}
    if not bad:
        return "done", "all steps ok"
    if all("census" in str(v.get("error") or "") for v in bad.values()):
        return "pending", "a census started - retrying when it clears"
    return "failed", "; ".join(f"{k}: {str(v.get('error') or 'failed')[:120]}" for k, v in bad.items())
