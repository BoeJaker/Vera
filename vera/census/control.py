"""Census control across prod restarts, and the routing a census recorded. Pure.

The harness (`run_census.py`, outside the repo) owns a census. Vera never
starts, stops or edits one. What Vera CAN do is ask: it writes a small control
file the harness polls every fifteen seconds, and the harness decides what to
do with it. Two requests exist:

    pause   cancel the goal in flight, wait until prod is healthy and the pause
            is lifted, then RE-RUN that goal from scratch. The abandoned attempt
            is logged on the row (`reruns`), never recorded as a result.
    drop    cancel the goal in flight and stop the whole set; the driver
            archives what finished so far as `-partial-dropped`.

Why it exists: a prod restart kills the loop under the goal, and before this
the harness recorded that as the goal's result and moved on. So restarting prod
meant waiting hours for the set to finish. `sys.dev.restart` now writes a pause
before it re-execs and the census module lifts it on the way back up; a caller
that does NOT want the census back passes resume_census=False and a drop is
written instead. Default is resume.

The harness also writes an ACTIVE file on every state change - template, goal,
session, paused/running/done/dropped - so the UI reads progress instead of
inferring it from a log tail, and a restart can tell whether there is anything
to pause at all.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Sequence

CONTROL_NAME = "census.control.json"
ACTIVE_NAME = "census.active.json"

#: An active file older than this with state=running is a harness that died
#: without saying so (the file is rewritten at least once per goal, and a goal
#: is bounded by its wall cap). Two hours covers the 3600 s research cap plus
#: the settle and quality passes around it.
ACTIVE_STALE_S = 2 * 3600

RESTART_REASON = "restart"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_json(path: str, data: Dict[str, Any]) -> bool:
    """Atomic: a harness polling every 15 s must never read a half-written file."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


# ── control ──────────────────────────────────────────────────────────────────

def control_state(control: Dict[str, Any]) -> str:
    """'drop' outranks 'pause' outranks 'run'."""
    c = control or {}
    if c.get("drop"):
        return "drop"
    if c.get("pause"):
        return "pause"
    return "run"


def make_control(action: str, reason: str = "", by: str = "",
                 resume_on_start: bool = False) -> Dict[str, Any]:
    """The file to write for one of pause / resume / drop.

    `resume_on_start` marks a pause written by a restart: the census module
    lifts it when the process comes back, so nobody has to remember to.
    """
    a = str(action or "").strip().lower()
    if a not in ("pause", "resume", "drop"):
        raise ValueError("action must be pause, resume or drop")
    return {
        "pause": a == "pause",
        "drop": a == "drop",
        "reason": str(reason or "")[:200],
        "by": str(by or "")[:80],
        "resume_on_start": bool(resume_on_start) if a == "pause" else False,
        "ts": _now_iso(),
    }


def should_lift_on_start(control: Dict[str, Any]) -> bool:
    """True when the pause on file was written by a restart that asked to
    resume - the one case the process itself is allowed to lift a pause."""
    c = control or {}
    return bool(c.get("pause")) and bool(c.get("resume_on_start")) and not c.get("drop")


# ── active ───────────────────────────────────────────────────────────────────

def active_view(active: Dict[str, Any], *, now: Optional[float] = None) -> Dict[str, Any]:
    """The harness's own report, with liveness judged from its timestamp.

    `state` is what the harness last said (running / paused / done / dropped);
    `live` is whether that claim can still be believed. A running claim that has
    not been refreshed in ACTIVE_STALE_S is a dead harness, and the view says so
    rather than showing a census that stopped hours ago as in flight.
    """
    a = dict(active or {})
    if not a:
        return {"present": False, "live": False, "state": "none"}
    state = str(a.get("state") or "unknown")
    updated = str(a.get("updated_at") or "")
    age = None
    try:
        t = time.mktime(time.strptime(updated, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        age = round((now if now is not None else time.time()) - t)
    except Exception:
        pass
    live = state in ("running", "paused") and (age is not None and age <= ACTIVE_STALE_S)
    total = int(a.get("goals_total") or 0)
    done = int(a.get("goals_done") or 0)
    a.update({"present": True, "live": live, "state": state, "age_s": age,
              "stale": (state in ("running", "paused") and not live),
              "progress": {"done": done, "total": total,
                           "current": a.get("current_goal") or "",
                           "remaining": max(0, total - done)}})
    return a


def restart_plan(active: Dict[str, Any], resume: bool) -> Dict[str, Any]:
    """What a restart should write, given what the harness says it is doing.

    Nothing to pause when no census is live: writing a pause anyway would leave
    a stale pause on file that the NEXT census would obey on its first poll.
    """
    v = active_view(active)
    if not v.get("live"):
        return {"action": "none", "why": "no live census", "active": v}
    if resume:
        return {"action": "pause", "why": "census in flight; will resume after restart",
                "active": v}
    return {"action": "drop", "why": "census in flight; caller asked not to resume",
            "active": v}


#: How long a restart waits for the harness to say it has paused (or dropped)
#: before re-exec'ing anyway. The harness reads the control file every 3s and a
#: cancel takes a few seconds; a harness that says nothing in this long is not
#: going to, and the restart must not hang on it.
PAUSE_ACK_MAX_S = 45.0


def pause_acked(active: Dict[str, Any], control_written_at: str, action: str = "pause") -> bool:
    """Has the harness acted on the control written at `control_written_at`?

    True when the active file says `paused` (or `dropped`, for a drop) AND was
    updated at or after the control was written - an older "paused" is from an
    earlier pause and says nothing about this one. Found live 2026-09-10: a
    restart wrote its pause and the new process lifted it 9s later, inside the
    harness's 15s poll, so the harness never saw it and sat on a loop the
    restart had killed for the rest of its wall cap.
    """
    a = active or {}
    want = "dropped" if action == "drop" else "paused"
    if str(a.get("state") or "") != want:
        return False
    updated = str(a.get("updated_at") or "")
    return bool(updated) and updated >= str(control_written_at or "")


# ── routing recorded on census rows ──────────────────────────────────────────

def routing_of(record: Dict[str, Any]) -> Dict[str, Any]:
    """One row's routing as the table shows it: the coder and planner in
    force, the nodes and models the calls actually hit, and the re-routes.
    A row from before routing was recorded says so instead of showing zeros."""
    r = (record or {}).get("routing")
    if not isinstance(r, dict):
        return {"recorded": False}
    start = r.get("at_start") or {}
    roles = start.get("roles") or {}
    calls = r.get("calls") or {}
    if not isinstance(calls, dict):
        calls = {}
    coder = roles.get("coder") or {}
    planner = roles.get("planner") or {}
    return {
        "recorded": True,
        # EVERY role the profile declared, with the model in force for each -
        # a census is run by planner, controller, tier, executor, writer and
        # coder together, and showing only the coder hid five of them.
        "roles": {r: {"model": (v or {}).get("model") or "",
                      "overridden": bool((v or {}).get("overridden")),
                      "declared_model": (v or {}).get("declared_model") or ""}
                  for r, v in roles.items()},
        # What actually ran, by role: {role: {model: calls}}. Calls with no
        # role (embeddings, chat) are keyed by their job type.
        "by_role": calls_by_role(calls.get("calls") or []),
        "coder": coder.get("model") or "",
        "coder_overridden": bool(coder.get("overridden")),
        "coder_declared": coder.get("declared_model") or "",
        "planner": planner.get("model") or "",
        "user_overrides": list(start.get("user_overrides") or []),
        "calls": int(calls.get("n") or 0),
        "nodes": calls.get("nodes") or {},
        "models": calls.get("models") or {},
        "reroutes": len(calls.get("reroutes") or []),
        "spill_calls": int(calls.get("spill_calls") or 0),
        "truncated": bool(calls.get("truncated")),
        "error": calls.get("error") or "",
    }


def calls_by_role(calls: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """{role: {model: n}} over recorded calls. Role first, then the job type
    (chat, embed...), then 'other' - never a blank key."""
    out: Dict[str, Dict[str, int]] = {}
    for c in calls or []:
        if not isinstance(c, dict):
            continue
        role = str(c.get("role") or "").strip() or str(c.get("job") or "").strip() or "other"
        if role.startswith("loop_"):
            role = role[5:]
        model = str(c.get("model") or "?")
        if "embed" in model and role == "other":
            role = "embed"
        out.setdefault(role, {})
        out[role][model] = out[role].get(model, 0) + 1
    return out


def routing_rollup(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """A run's routing in one line: which coder/planner it ran on, whether that
    changed mid-run, how many calls hit which node, and how many were re-routed
    or spilled. A run whose coder changed between goals is flagged - that is two
    instruments in one file."""
    coders: List[str] = []
    planners: List[str] = []
    roles: Dict[str, List[str]] = {}          # role -> models seen in force, in order
    by_role: Dict[str, Dict[str, int]] = {}   # role -> {model: calls}
    nodes: Dict[str, int] = {}
    models: Dict[str, int] = {}
    calls = reroutes = spill = recorded = 0
    for rec in records or []:
        ro = routing_of(rec)
        if not ro.get("recorded"):
            continue
        recorded += 1
        if ro["coder"] and ro["coder"] not in coders:
            coders.append(ro["coder"])
        if ro["planner"] and ro["planner"] not in planners:
            planners.append(ro["planner"])
        for r, v in (ro.get("roles") or {}).items():
            m = (v or {}).get("model") or "(default)"
            roles.setdefault(r, [])
            if m not in roles[r]:
                roles[r].append(m)
        for r, mm in (ro.get("by_role") or {}).items():
            by_role.setdefault(r, {})
            for m, n in (mm or {}).items():
                by_role[r][m] = by_role[r].get(m, 0) + int(n or 0)
        for k, v in (ro.get("nodes") or {}).items():
            nodes[str(k)] = nodes.get(str(k), 0) + int(v or 0)
        for k, v in (ro.get("models") or {}).items():
            models[str(k)] = models.get(str(k), 0) + int(v or 0)
        calls += ro["calls"]
        reroutes += ro["reroutes"]
        spill += ro["spill_calls"]
    return {
        "recorded_goals": recorded,
        "coders": coders, "planners": planners,
        "coder_changed": len(coders) > 1,
        "roles": roles,
        "roles_changed": sorted(r for r, ms in roles.items() if len(ms) > 1),
        "by_role": by_role,
        "nodes": nodes, "models": models,
        "calls": calls, "reroutes": reroutes, "spill_calls": spill,
    }


# ── the running goal's own routing, live ─────────────────────────────────────

def live_routing(entries: Sequence[Dict[str, Any]], since_iso: str, *, session_id: str = "",
                 last_n: int = 6) -> Dict[str, Any]:
    """What the goal in flight has asked of the LLMs so far, from the
    in-process request log: calls by node and by model, CPU spills and
    re-routes, the median tokens/s, and the last few calls with their speed
    and GPU residency - the live half of what a finished row records.

    A call belongs to the goal when the log stamped it with the goal's loop
    session, or (the v7 runner stamps no session) when it was made since the
    goal started. The census holds the box alone, so the window is the goal.
    """
    lo = str(since_iso or "")[:19]
    mine: List[Dict[str, Any]] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        sid = str(e.get("session_id") or "")
        ts = str(e.get("ts") or "")[:19]
        if (session_id and sid == session_id) or (lo and ts >= lo and (not sid or sid == session_id)):
            mine.append(e)
    by_node: Dict[str, int] = {}
    by_model: Dict[str, int] = {}
    spill = reroutes = 0
    toks: List[float] = []
    for e in mine:
        node = str(e.get("instance") or e.get("fallback_instance") or "?")
        model = str(e.get("model") or "?")
        by_node[node] = by_node.get(node, 0) + 1
        by_model[model] = by_model.get(model, 0) + 1
        if e.get("cpu_spill"):
            spill += 1
        if e.get("escalated") or e.get("fallback_instance") or str(e.get("status") or "") == "done_fallback":
            reroutes += 1
        t = e.get("tok_per_s")
        if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0:
            toks.append(float(t))
    toks.sort()
    median = (toks[len(toks) // 2] if toks else None)
    last = [{"ts": str(e.get("ts") or "")[11:19], "job": e.get("job_type") or "", "role": e.get("role") or "",
             "model": str(e.get("model") or ""), "node": str(e.get("instance") or e.get("fallback_instance") or ""),
             "tok_s": e.get("tok_per_s"), "gpu_pct": e.get("gpu_resident_pct"), "s": e.get("elapsed_s"),
             "status": str(e.get("status") or ""), "spill": bool(e.get("cpu_spill"))}
            for e in mine[-max(0, int(last_n)):]]
    return {"calls": len(mine), "by_node": by_node, "by_model": by_model, "spill_calls": spill,
            "reroutes": reroutes, "tok_s_median": (round(median, 1) if median is not None else None),
            "last": last, "since": lo}
