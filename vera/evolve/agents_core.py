"""The Agents page's one table: a row per AGENT SESSION, built from the five
views it replaced (Sessions, Board, Notes, Capacity, Swarm).

A Claude Code or Codex session, a Vera improvement loop, an edit-queue
agent: each is something that does work and leaves traces in the other
stores - board items it claimed or was dispatched on, pipelines it drove,
sandboxes it owns. The Sessions page listed the sessions, the Board the
items, the Swarm whatever was live across all of them; here the session is
the row and the rest are its columns. The board's items are the page's
other mode (item_rows): every item, filterable, expandable.

Pure: no I/O. agents_capabilities gathers the readers and hands their rows
in, the same shape of module as work_core (Work) and ship_core (Ship).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

LIVE_LANES = ("in_progress", "in_progress_vera")
OPEN_LANES = ("inbox", "ready", "in_progress", "blocked", "needs_review", "review", "queued_vera", "in_progress_vera")
TERMINAL_PIPELINE = ("promoted", "rolled_back")

# One word for what a session is doing; ordered, the table sorts by it.
STATE_LIVE = "live"                 # a pipeline of its is running, or the loop / editor is running
STATE_WORKING = "working"           # holds an item in progress
STATE_RECENT = "recent"             # spoke within the stalled window, whatever the board says
STATE_RESUMABLE = "resumable"       # the watch's classification: silent past the resumable window
STATE_STALLED = "stalled"
STATE_BLOCKED = "declared-block"
STATE_QUEUED = "queued"             # an edit-queue agent waiting its turn
STATE_UNREPORTED = "finished-unreported"
STATE_HUMAN = "human"
STATE_DONE = "done"                 # every item it touched is done / dropped
STATE_IDLE = "idle"                 # nothing open, nothing claimed (the watch's "untracked")
_STATE_BAND = {STATE_LIVE: 0, STATE_WORKING: 1, STATE_QUEUED: 1, STATE_RECENT: 1, STATE_RESUMABLE: 2,
               STATE_STALLED: 2, STATE_BLOCKED: 3, STATE_UNREPORTED: 3, STATE_HUMAN: 4}
ACTIVE_STATES = (STATE_LIVE, STATE_WORKING, STATE_RECENT, STATE_QUEUED, STATE_RESUMABLE, STATE_STALLED, STATE_BLOCKED)
RECENT_S = 2700.0   # the watch's stalled_after_s default: a session that spoke inside it is active

AGENT_KINDS = ("claude", "codex", "vera", "editor", "orchestrator", "other")


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def agent_of(*names: Any) -> str:
    """The agent kind from whatever named it first: the watch's `agent`, an
    item's `agent`, a pipeline's `controller`, a sandbox's `owner`."""
    for n in names:
        n = _s(n).strip().lower()
        if not n:
            continue
        if n.startswith("codex"):
            return "codex"
        if n.startswith("claude"):
            return "claude"
        if n in ("vera", "vera-loop", "loop", "improve"):
            return "vera"
        if n in ("editor", "editq", "edit-queue"):
            return "editor"
        if n == "orchestrator":
            return "orchestrator"
        return "other"
    return ""


def item_cell(it: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": _s(it.get("id")), "title": _s(it.get("title")), "lane": _s(it.get("lane")),
            "branch": _s(it.get("branch")), "pipeline": _s(it.get("pipeline")), "plan": _s(it.get("plan")),
            "updated_at": _s(it.get("updated_at")), "comment_count": int(it.get("comment_count") or 0)}


def pipeline_cell(p: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": _s(p.get("id")), "branch": _s(p.get("branch")), "decision": _s(p.get("decision")),
            "gate_passed": p.get("gate_passed"), "live": bool(p.get("live")), "kind": _s(p.get("kind")),
            "created_at": _s(p.get("created_at"))}


def sandbox_cell(s: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": _s(s.get("name")), "branch": _s(s.get("branch")), "running": bool(s.get("running")),
            "paused": bool(s.get("paused")), "port": s.get("port"), "last_activity": _s(s.get("last_activity"))}


def _state(row: Dict[str, Any], watch_state: str, recent_s: float = RECENT_S) -> str:
    if row.get("live"):
        return STATE_LIVE
    if row["kind"] == "editor":
        return STATE_QUEUED if row.get("status") == "queued" else STATE_LIVE
    if watch_state in (STATE_LIVE, STATE_RESUMABLE, STATE_STALLED, STATE_BLOCKED):
        return watch_state
    if any(i["lane"] in LIVE_LANES for i in row["items"]):
        return STATE_WORKING
    # `untracked` means no board claim - not "not running". Every ingested
    # session on this instance is untracked (90 of 90, 2026-09-07), so a list
    # that tested state alone was empty by construction; recency is the test.
    age = row.get("age_s")
    if row["chat"] and age is not None and float(age) < float(recent_s):
        return STATE_RECENT
    if watch_state in (STATE_UNREPORTED, STATE_HUMAN):
        return watch_state
    if row["items"] and all(i["lane"] in ("done", "dropped") for i in row["items"]):
        return STATE_DONE
    if any(i["lane"] in OPEN_LANES for i in row["items"]):
        return STATE_WORKING
    return STATE_IDLE


def session_rows(watch_sessions: Iterable[Dict[str, Any]], items: Iterable[Dict[str, Any]],
                 pipelines: Iterable[Dict[str, Any]], sandboxes: Iterable[Dict[str, Any]],
                 loops: Iterable[Dict[str, Any]] = (), editors: Iterable[Dict[str, Any]] = (),
                 recent_s: float = RECENT_S) -> List[Dict[str, Any]]:
    """One row per agent session: the watch's sessions, every session the
    board names (a codex run is known only from its items), live Vera loops
    and edit-queue agents. Live and working first, then who needs a person,
    then newest activity first."""
    by: Dict[str, Dict[str, Any]] = {}

    def row(sid: str, kind: str = "session") -> Dict[str, Any]:
        r = by.get(sid)
        if r is None:
            r = by[sid] = {"id": sid, "kind": kind, "agent": "", "title": "", "state": STATE_IDLE,
                           "project_dir": "", "turns": 0, "commit_count": 0, "age_s": None, "last_ts": "",
                           "reason": "", "action": "", "resume_ok": False, "chat": False, "watch_state": "",
                           "claims": [], "items": [], "items_n": 0, "items_open": 0, "by_lane": {},
                           "pipelines": [], "pipelines_n": 0, "live": False, "sandboxes": [],
                           "branches": [], "last_activity": "", "status": ""}
        return r

    for s in watch_sessions or []:
        if not isinstance(s, dict):
            continue
        sid = _s(s.get("claude_session_id") or s.get("id"))
        if not sid:
            continue
        r = row(sid)
        r["chat"] = True
        r["agent"] = agent_of(s.get("agent"), "claude")
        r["title"] = _s(s.get("title"))
        r["project_dir"] = _s(s.get("project_dir"))
        r["turns"] = int(s.get("turns") or 0)
        r["commit_count"] = int(s.get("commit_count") or 0)
        r["age_s"] = s.get("age_s")
        r["last_ts"] = _s(s.get("last_ts"))
        r["reason"] = _s(s.get("reason"))
        r["action"] = _s(s.get("action"))
        r["resume_ok"] = bool(s.get("resume_ok"))
        r["watch_state"] = _s(s.get("state"))
        r["claims"] = [{"id": _s(c.get("id")), "title": _s(c.get("title"))} for c in (s.get("claims") or []) if isinstance(c, dict)]
        for p in s.get("pipelines") or []:
            if isinstance(p, dict) and _s(p.get("id")):
                r["pipelines"].append(pipeline_cell(p))

    for it in items or []:
        if not isinstance(it, dict):
            continue
        sid = _s(it.get("session"))
        if not sid:
            continue
        r = row(sid)
        r["items"].append(item_cell(it))
        r["agent"] = r["agent"] or agent_of(it.get("agent"))
        if not r["title"]:
            r["title"] = _s(it.get("title"))
        if _s(it.get("executor")) and not r["status"]:
            r["status"] = "executor " + _s(it.get("executor"))

    for p in pipelines or []:
        if not isinstance(p, dict):
            continue
        sid = _s(p.get("session_id"))
        if not sid:
            continue
        r = row(sid)
        if not any(x["id"] == _s(p.get("id")) for x in r["pipelines"]):
            r["pipelines"].append(pipeline_cell(p))
        r["agent"] = r["agent"] or agent_of(p.get("controller"))

    for s in sandboxes or []:
        if not isinstance(s, dict):
            continue
        sid = _s(s.get("session_id"))
        if not sid:
            continue
        r = row(sid)
        r["sandboxes"].append(sandbox_cell(s))
        r["agent"] = r["agent"] or agent_of(s.get("owner"))

    for lp in loops or []:
        if not isinstance(lp, dict):
            continue
        sid = _s(lp.get("id") or lp.get("session_id"))
        if not sid:
            continue
        if not (lp.get("live") or _s(lp.get("status")) == "running"):
            continue
        r = row(sid, kind="loop")
        r["kind"] = "loop"
        r["agent"] = "vera"
        r["title"] = r["title"] or ("improve " + _s(lp.get("profile") or lp.get("target") or sid[:8]))
        r["status"] = _s(lp.get("phase") or lp.get("status") or "running") + (
            " r%s/%s" % (lp.get("rounds_done") or 0, lp.get("max_rounds") or "?"))
        r["live"] = True
        r["last_activity"] = _s(lp.get("updated_at") or lp.get("started_at") or lp.get("ts"))

    for ed in editors or []:
        if not isinstance(ed, dict):
            continue
        st = _s(ed.get("status"))
        if st not in ("running", "queued"):
            continue
        sid = _s(ed.get("id"))
        if not sid:
            continue
        r = row(sid, kind="editor")
        r["kind"] = "editor"
        r["agent"] = "editor"
        r["title"] = r["title"] or ("editor " + _s(ed.get("model") or sid[:8]))
        r["status"] = st + ((" @" + _s(ed.get("instance"))) if ed.get("instance") else "")
        r["live"] = st == "running"
        r["last_activity"] = _s(ed.get("updated_at") or ed.get("ts") or ed.get("created_at"))

    out = []
    for sid, r in by.items():
        r["items"].sort(key=lambda i: i["updated_at"], reverse=True)
        r["pipelines"].sort(key=lambda p: p["created_at"], reverse=True)
        # A session the watch does not know is labelled by its newest item.
        if not r["chat"] and r["kind"] == "session" and r["items"]:
            r["title"] = r["items"][0]["title"] or r["title"]
        r["items_n"] = len(r["items"])
        r["items_open"] = sum(1 for i in r["items"] if i["lane"] in OPEN_LANES)
        lanes: Dict[str, int] = {}
        for i in r["items"]:
            lanes[i["lane"]] = lanes.get(i["lane"], 0) + 1
        r["by_lane"] = lanes
        r["pipelines_n"] = len(r["pipelines"])
        r["live"] = r["live"] or any(p["live"] for p in r["pipelines"])
        r["branches"] = sorted({x["branch"] for x in r["items"] + r["pipelines"] + r["sandboxes"] if x.get("branch")})
        r["agent"] = r["agent"] or ("claude" if r["chat"] else "other")
        times = [r["last_ts"], r["last_activity"]] + [i["updated_at"] for i in r["items"]] \
            + [p["created_at"] for p in r["pipelines"]] + [s["last_activity"] for s in r["sandboxes"]]
        r["last_activity"] = max(times)
        r["state"] = _state(r, r["watch_state"], recent_s)
        out.append(r)
    out.sort(key=lambda r: r["last_activity"], reverse=True)
    out.sort(key=lambda r: _STATE_BAND.get(r["state"], 5))
    return out


def item_rows(items: Iterable[Dict[str, Any]], sessions_by_id: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Every board item as a row (the page's other mode), newest first, the
    live lanes first; each names the agent session it belongs to."""
    sessions_by_id = sessions_by_id or {}
    out = []
    for it in items or []:
        if not isinstance(it, dict) or not _s(it.get("id")):
            continue
        sid = _s(it.get("session"))
        out.append({
            "id": _s(it.get("id")), "title": _s(it.get("title")), "lane": _s(it.get("lane")),
            "labels": [_s(x) for x in (it.get("labels") or [])], "agent": agent_of(it.get("agent")) or _s(it.get("agent")),
            "agent_raw": _s(it.get("agent")), "session": sid, "session_state": _s((sessions_by_id.get(sid) or {}).get("state")),
            "repo": _s(it.get("repo")), "project": _s(it.get("project")), "plan": _s(it.get("plan")),
            "branch": _s(it.get("branch")), "pipeline": _s(it.get("pipeline")), "executor": _s(it.get("executor")),
            "model": _s(it.get("model")), "comment_count": int(it.get("comment_count") or 0),
            "created_at": _s(it.get("created_at")), "updated_at": _s(it.get("updated_at")),
            "heartbeat": _s(it.get("heartbeat")),
        })
    out.sort(key=lambda r: r["updated_at"], reverse=True)
    out.sort(key=lambda r: 0 if r["lane"] in LIVE_LANES else 1 if r["lane"] in OPEN_LANES else 2)
    return out


def filter_sessions(rows: Iterable[Dict[str, Any]], *, agent: str = "", state: str = "", text: str = "",
                    branch: str = "", hide_idle: bool = False) -> List[Dict[str, Any]]:
    q = (text or "").strip().lower()
    out = []
    for r in rows:
        if agent and r.get("agent") != agent:
            continue
        if state and r.get("state") != state:
            continue
        if hide_idle and r.get("state") in (STATE_IDLE, STATE_DONE):
            continue
        if branch and branch not in (r.get("branches") or []):
            continue
        if q:
            hay = " ".join([r.get("id", ""), r.get("title", ""), r.get("project_dir", ""), r.get("status", ""),
                            " ".join(r.get("branches") or []), " ".join(i["title"] for i in r.get("items") or [])]).lower()
            if q not in hay:
                continue
        out.append(r)
    return out


def filter_items(rows: Iterable[Dict[str, Any]], *, lane: str = "", agent: str = "", text: str = "", repo: str = "",
                 project: str = "", branch: str = "", plan: str = "", hide_done: bool = False) -> List[Dict[str, Any]]:
    q = (text or "").strip().lower()
    out = []
    for r in rows:
        if lane and r.get("lane") != lane:
            continue
        if hide_done and r.get("lane") in ("done", "dropped"):
            continue
        if agent and r.get("agent") != agent:
            continue
        if repo and r.get("repo") != repo:
            continue
        if project and r.get("project") != project:
            continue
        if branch and r.get("branch") != branch:
            continue
        if plan and r.get("plan") != plan:
            continue
        if q:
            hay = " ".join([r.get("id", ""), r.get("title", ""), " ".join(r.get("labels") or []), r.get("session", ""),
                            r.get("branch", ""), r.get("pipeline", ""), r.get("plan", "")]).lower()
            if q not in hay:
                continue
        out.append(r)
    return out


def swarm_counts(rows: Iterable[Dict[str, Any]], items: Iterable[Dict[str, Any]], sandboxes: Iterable[Dict[str, Any]],
                 pipelines: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """What the Swarm page counted, from the same rows."""
    rows = list(rows)
    items = list(items)
    sandboxes = [s for s in sandboxes if isinstance(s, dict)]
    pipelines = [p for p in pipelines if isinstance(p, dict)]
    return {
        "loops": sum(1 for r in rows if r.get("kind") == "loop"),
        "editors": sum(1 for r in rows if r.get("kind") == "editor"),
        "pipelines_live": sum(1 for p in pipelines if p.get("live")),
        "dispatched": sum(1 for i in items if _s(i.get("lane")) in LIVE_LANES),
        "sessions_active": sum(1 for r in rows if r.get("kind") == "session" and r.get("state") in ACTIVE_STATES),
        "sessions": sum(1 for r in rows if r.get("kind") == "session"),
        "containers": len(sandboxes), "containers_running": sum(1 for s in sandboxes if s.get("running")),
        "items_open": sum(1 for i in items if _s(i.get("lane")) in OPEN_LANES), "items": len(items),
    }


def summary(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    st: Dict[str, int] = {}
    ag: Dict[str, int] = {}
    n = 0
    for r in rows:
        n += 1
        st[r.get("state") or STATE_IDLE] = st.get(r.get("state") or STATE_IDLE, 0) + 1
        ag[r.get("agent") or "other"] = ag.get(r.get("agent") or "other", 0) + 1
    return {"count": n, "states": st, "agents": ag}
