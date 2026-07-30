"""
activity_capabilities.py — the UNIFIED ACTIVITY TIMELINE
=========================================================

One coherent view over everything Vera does, pulled from the stores each
subsystem already writes:

  • dream cycles            (dream history + per-project dream links)
  • agentic loop runs       (project loop-run ledger + the loop session store)
  • V8 loop programs        (loop_orchestrator programs + their constituent runs)
  • background loop work    (live `vera:loop:sessions` — v8:* / dream:* running now)
  • cap activity            (recent cap.ok events, mapped to the panel/element
                             that can RENDER each cap's output — fabric discovery
                             → the graph, a net scan → the netmap UI, …)
  • sandbox containers      (per project/goal/dream/program, with their sessions)

`activity.timeline` returns a time-ordered list of EVENTS for a SCOPE:

  scope = "all"                     — the master timeline
          "project:<slug>"          — one project (or goal)
          "goal:<slug>"             — alias of project scope
          "dream" | "dream:<trig>"  — dream pipelines (all, or one trigger)
          "program:<pid>"           — one V8 program
          "chat:<session_id>"       — one chat session's activity

Each event carries a `ui` block { panel, url, element, session_id } so the
front-end can open the ASSOCIATED UI (the discovery graph, the netmap, the
agent-loop-output re-attach) and, for a running loop, watch it live.

This module owns NO new storage — it is a read/compose layer over existing
keys, so it can never drift from the source of truth.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, capability, emit_event, now_iso, register_ui,
)

log = logging.getLogger("vera.activity")
_HERE = Path(__file__).parent

# Redis keys owned by other modules (read-only here).
KEY_PROJECTS          = "vera:dream:projects"
KEY_PROJECT_LOOPS     = "vera:dream:project_loops"
KEY_PROJECT_ARTIFACTS = "vera:dream:project_artifacts"
KEY_LOOP_SESSIONS     = "vera:loop:sessions"
KEY_PROGRAMS          = "vera:v8:programs"

# A loop-session whose last event is older than this (zset score = last-event
# epoch) is treated as INTERRUPTED, not running — the "27 running loops" the UI
# showed were overwhelmingly dead sessions left over from restarts.
_LOOP_STALE_SECS = int(os.getenv("VERA_LOOP_STALE_SECS", "600") or 600)

# Cache: cycle_id -> friendly dream label (progress snapshots are cheap but this
# avoids hammering Redis on every timeline render).
_CID_NAME_CACHE: Dict[str, str] = {}
_PROG_NAME_CACHE: Dict[str, str] = {}


def _redis():
    return getattr(_orch, "REDIS", None)


def _dag_mod():
    for n, m in list(sys.modules.items()):
        if m is not None and n.endswith("dag_workshop_capabilities") \
                and hasattr(m, "_AGENT_LOOP_TASKS"):
            return m
    return None


def _sbx_mod():
    for n, m in list(sys.modules.items()):
        if m is not None and n.endswith("session_sandbox_capabilities") \
                and hasattr(m, "route_fs_browse"):
            return m
    return None


async def _scope_owner_keys(scope: str) -> List[str]:
    """The sandbox-owner container key(s) that hold a scope's files/work:
    project/goal → goal-<slug>/proj-<slug>; program → its sandbox_owner (a
    goal-<slug>) or v8-<pid>; dream:<trigger> → dream-<slug(trigger)>."""
    kind, _, ref = (scope or "all").partition(":")
    kind = kind.lower()
    if kind in ("project", "goal") and ref:
        return [f"goal-{ref}", f"proj-{ref}"]
    if kind == "program" and ref:
        r = _redis()
        owner = ""
        if r:
            try:
                raw = await r.hget(KEY_PROGRAMS, ref)
                if raw:
                    owner = str(json.loads(_rd(raw)).get("sandbox_owner") or "").strip()
            except Exception:
                owner = ""
        return [owner] if owner else [f"v8-{ref}"]
    if kind == "dream" and ref:
        m = _sbx_mod()
        return [m.slug_key("dream", ref)] if m and hasattr(m, "slug_key") \
            else [f"dream-{ref}"]
    return []


def _rd(v):
    return v.decode() if isinstance(v, (bytes, bytearray)) else v


# ─────────────────────────────────────────────────────────────────────────────
#  ASSOCIATED-UI MAP — cap / group / session-prefix → the panel or element that
#  can RENDER that activity. Longest-prefix wins. `element` names an injectable
#  custom element (served under /ui/elements/) the timeline can mount inline;
#  `url` is a full-page panel to open. Extended at runtime via register_activity_ui.
# ─────────────────────────────────────────────────────────────────────────────
_UI_MAP: List[Dict[str, str]] = [
    # cap-name / group prefix, panel label, full-page url, inline element
    {"pfx": "fabric.discover",      "label": "Discovery graph", "url": "/fabric/panel#discover", "element": ""},
    {"pfx": "fabric.extract_graph", "label": "Fabric graph",    "url": "/fabric/panel#graph",    "element": ""},
    {"pfx": "fabric.loom",          "label": "Loom",            "url": "/fabric/panel#loom",     "element": ""},
    {"pfx": "fabric.",              "label": "Data fabric",     "url": "/fabric/panel",          "element": ""},
    {"pfx": "discover",             "label": "Discovery graph", "url": "/fabric/panel#discover", "element": ""},
    {"pfx": "memory.",             "label": "Memory graph",    "url": "/galaxy/panel",          "element": ""},
    {"pfx": "netmap.",             "label": "Netmap",          "url": "/netmap/panel",          "element": ""},
    {"pfx": "recon",               "label": "Netmap",          "url": "/netmap/panel",          "element": ""},
    {"pfx": "netmon.",             "label": "Net monitor",     "url": "/netmon/panel",          "element": ""},
    {"pfx": "markets.",            "label": "Markets",         "url": "/markets/panel",         "element": ""},
    {"pfx": "commerce.",           "label": "Commerce",        "url": "/commerce/panel",        "element": ""},
    {"pfx": "business.",           "label": "Business",        "url": "/biz/panel",             "element": ""},
    {"pfx": "media.",              "label": "Studio",          "url": "/media/panel",           "element": ""},
    {"pfx": "render.",             "label": "Studio",          "url": "/media/panel",           "element": ""},
    {"pfx": "spritegen.",          "label": "Spritegen",       "url": "/spritegen/panel",       "element": ""},
    {"pfx": "ide.",                "label": "IDE",             "url": "/ide/panel",             "element": ""},
    {"pfx": "exec.",               "label": "Execution",       "url": "/exec/panel",            "element": ""},
    {"pfx": "build.",              "label": "Builder",         "url": "/exec/panel",            "element": ""},
    {"pfx": "research.",           "label": "Research",        "url": "/fabric/panel#discover", "element": ""},
    {"pfx": "web.",                "label": "Discovery graph", "url": "/fabric/panel#discover", "element": ""},
    {"pfx": "mesh.",               "label": "Mesh",            "url": "/mesh/panel",            "element": ""},
    {"pfx": "provision",           "label": "Provision",       "url": "/workers",               "element": ""},
    {"pfx": "docker.",             "label": "Workers",         "url": "/workers",               "element": ""},
    {"pfx": "sandbox.",            "label": "Containers",      "url": "/workers#containers",    "element": ""},
    {"pfx": "calendar.",           "label": "Calendar",        "url": "/cal/panel",             "element": ""},
    {"pfx": "cal.",                "label": "Calendar",        "url": "/cal/panel",             "element": ""},
    {"pfx": "email.",              "label": "Mail",            "url": "/mail/panel",            "element": ""},
    {"pfx": "accounts.",           "label": "Accounts",        "url": "/accounts/panel",        "element": ""},
]


def register_activity_ui(prefix: str, label: str, url: str = "", element: str = "") -> None:
    """Subsystems can DECLARE how their cap activity should be rendered on the
    timeline (a full-page panel and/or an inline element). Longest-prefix wins."""
    _UI_MAP.insert(0, {"pfx": prefix, "label": label, "url": url or "",
                       "element": element or ""})


def _ui_for_cap(cap_name: str) -> Dict[str, str]:
    """The associated-UI descriptor for a cap name (longest matching prefix)."""
    best = None
    best_len = -1
    for row in _UI_MAP:
        p = row["pfx"]
        if cap_name.startswith(p) and len(p) > best_len:
            best, best_len = row, len(p)
    if not best:
        return {"label": "", "url": "", "element": ""}
    return {"label": best["label"], "url": best.get("url", ""),
            "element": best.get("element", "")}


def _loop_ui(session_id: str) -> Dict[str, str]:
    """A loop-run event's UI: the agent-loop-output element re-attaches live via
    /workshop/agent_loop/reattach?session_id=… (replays then tails)."""
    return {"label": "Loop output", "element": "agent-loop-output",
            "session_id": session_id,
            "reattach": f"/workshop/agent_loop/reattach?session_id={session_id}"}


def _ev(kind: str, ts: str, title: str, *, summary: str = "", status: str = "",
        session_id: str = "", cap: str = "", ref: str = "", source: str = "",
        ui: Optional[Dict] = None, extra: Optional[Dict] = None) -> Dict[str, Any]:
    e = {"kind": kind, "ts": ts or now_iso(), "title": (title or "")[:200],
         "summary": (summary or "")[:1200], "status": status,
         "session_id": session_id, "cap": cap, "ref": ref, "source": source,
         "ui": ui or {}}
    if extra:
        e["extra"] = extra
    return e


# ─────────────────────────────────────────────────────────────────────────────
#  SOURCE COLLECTORS
# ─────────────────────────────────────────────────────────────────────────────
async def _dream_events(trigger: str = "", project: str = "",
                        limit: int = 60) -> List[Dict[str, Any]]:
    cap = CAPABILITY_REGISTRY.get("dream.history")
    if not cap or not cap.get("func"):
        return []
    try:
        res = await cap["func"](limit=limit, trigger=trigger)
    except Exception as e:
        log.debug("activity dream_events: %s", e)
        return []
    out = []
    for r in (res or {}).get("cycles", res.get("history", []) if isinstance(res, dict) else []):
        if project and str(r.get("project") or "") != project:
            continue
        cid = str(r.get("cycle_id") or "")
        out.append(_ev(
            "dream_cycle", r.get("ended_at") or r.get("started_at") or "",
            r.get("title") or r.get("trigger") or "dream cycle",
            summary=str(r.get("report") or r.get("summary") or "")[:1000],
            status="early-exit" if r.get("early_exit") else "done",
            ref=str(r.get("project") or r.get("trigger") or ""),
            source="dream", cap="dream.cycle.run",
            ui={"label": "Dream cycle", "url": "/dream/panel#history",
                "cycle_id": cid},
            extra={"trigger": r.get("trigger"), "themes": (r.get("themes") or [])[:6]}))
    return out


async def _project_loop_events(slug: str, limit: int = 40) -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return []
    out = []
    try:
        rows = await r.lrange(f"{KEY_PROJECT_LOOPS}:{slug}", -limit, -1)
    except Exception:
        rows = []
    for raw in rows or []:
        try:
            run = json.loads(_rd(raw))
        except Exception:
            continue
        sid = str(run.get("session_id") or "")
        steps = run.get("steps") or []
        # derive per-step associated UIs so a run links to the tools it drove
        tool_uis = []
        for s in steps:
            capn = str(s.get("cap") or "")
            u = _ui_for_cap(capn) if capn else {}
            if u.get("url"):
                tool_uis.append({"cap": capn, **u})
        out.append(_ev(
            "loop_run", run.get("ts") or "",
            (run.get("source") or "loop run") + " · " + str(run.get("engine") or ""),
            summary=str(run.get("final") or run.get("goal") or "")[:1000],
            status="done" if run.get("final") else "run",
            session_id=sid, ref=slug, source="project", cap="dag.agent_loop",
            ui=_loop_ui(sid) if sid else {},
            extra={"steps": len(steps), "goal": str(run.get("goal") or "")[:300],
                   "artifacts": len(run.get("artifact_ids") or []),
                   "tool_uis": tool_uis[:12]}))
    return out


async def _project_artifact_events(slug: str, limit: int = 40) -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return []
    out = []
    try:
        rows = await r.lrange(f"{KEY_PROJECT_ARTIFACTS}:{slug}", -limit, -1)
    except Exception:
        rows = []
    for raw in rows or []:
        try:
            a = json.loads(_rd(raw))
        except Exception:
            continue
        out.append(_ev(
            "artifact", a.get("ts") or a.get("created") or "",
            "artifact · " + str(a.get("name") or a.get("type") or ""),
            summary=str(a.get("path") or "")[:300],
            status=str(a.get("type") or ""), ref=slug, source="project",
            cap="project.artifact.get",
            ui={"label": "Artifact", "artifact_id": str(a.get("id") or ""),
                "slug": slug},
            extra={"size": a.get("size"), "type": a.get("type")}))
    return out


async def _program_events(pid: str = "", limit: int = 40) -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return []
    out = []
    try:
        raw = await r.hgetall(KEY_PROGRAMS)
    except Exception:
        raw = {}
    for _, v in (raw or {}).items():
        try:
            prog = json.loads(_rd(v))
        except Exception:
            continue
        if pid and prog.get("id") != pid:
            continue
        # The WHOLE plan — every loop with its current state — travels on the
        # program event so the card shows all N loops (pending/waiting/running/
        # done/failed/retired), not just the handful that have produced a run.
        def _loop_session(lp, st, runs):
            """The session id to WATCH/replay for a loop: its in-flight run when
            running (record isn't appended until the run ends), else its last."""
            if str(st.get("status")) == "running":
                return f"v8:{prog.get('id')}:{lp.get('name')}:{len(runs) + 1}"
            return str(runs[-1].get("session_id") or "") if runs else ""
        plan = []
        for lp in prog.get("loops", []):
            st = lp.get("state") or {}
            runs = st.get("runs") or []
            plan.append({
                "name": str(lp.get("name") or ""),
                "status": str(st.get("status") or "pending"),
                "goal": str(lp.get("goal") or "")[:200],
                "engine": lp.get("engine", ""), "profile": lp.get("profile", ""),
                "runs": len(runs),
                "session_id": _loop_session(lp, st, runs)})
        out.append(_ev(
            "program", prog.get("created") or "",
            "V8 program · " + str(prog.get("name") or prog.get("id")),
            summary=str(prog.get("done_when") or prog.get("brief") or "")[:600],
            status=str(prog.get("status") or ""), ref=str(prog.get("id") or ""),
            source="program", cap="dag.agent_loop_v8",
            # No `url` — a program drills into its OWN timeline scope INLINE
            # (data-scope) rather than opening the dream panel in a new tab.
            ui={"label": "Program", "program_id": str(prog.get("id") or "")},
            extra={"loops": len(prog.get("loops") or []),
                   "loop_plan": plan,
                   "owner_ref": prog.get("owner_ref", ""),
                   "sandbox_owner": prog.get("sandbox_owner", "")}))
        # DRILL-IN only (a specific program): the FULL SEQUENCE as one card per
        # loop, in plan order, each watchable/replayable via the agent-loop UI.
        # (In the `all` scope we keep just the program card + its loop_plan, so
        # the master timeline isn't flooded with every loop.) A running loop is
        # emitted live by `_live_loop_events`, so skip it here to avoid a double.
        if pid:
            for idx, lp in enumerate(prog.get("loops", [])):
                st = lp.get("state") or {}
                status = str(st.get("status") or "pending")
                if status == "running":
                    continue
                runs = st.get("runs") or []
                last = runs[-1] if runs else None
                sid = str(last.get("session_id") or "") if last else ""
                body = (str(last.get("final") or last.get("summary") or "")
                        if last else str(lp.get("goal") or ""))
                out.append(_ev(
                    "v8_loop", (last.get("ts") if last else None) or prog.get("created") or "",
                    "loop · " + str(lp.get("name") or ""),
                    summary=body[:1500], status=status,
                    session_id=sid, ref=str(prog.get("id") or ""),
                    source="program", cap="dag.agent_loop",
                    ui=_loop_ui(sid) if sid else {},
                    extra={"seq": idx + 1, "runs": len(runs),
                           "engine": lp.get("engine", ""),
                           "profile": lp.get("profile", ""),
                           "elapsed_s": (last.get("elapsed_s") if last else None)}))
    return out


async def _friendly_loop_name(sid: str) -> str:
    """A human name for a loop session id. `dream:<cid>:<stage>` →
    'Dream · <trigger/project> · <stage>'; `v8:<pid>:<loop>:<n>` →
    'V8 · <program> · <loop>'; `evolve:<id>` → 'Evolve'; owner keys title-cased."""
    r = _redis()
    parts = sid.split(":")
    if sid.startswith("dream:") and len(parts) >= 2:
        cid = parts[1]
        stage = parts[2] if len(parts) > 2 else ""
        label = _CID_NAME_CACHE.get(cid)
        if label is None and r:
            label = ""
            try:
                raw = await r.get(f"vera:dream:progress:{cid}")
                if raw:
                    snap = json.loads(_rd(raw))
                    label = str(snap.get("label") or snap.get("trigger")
                                or snap.get("project") or "").strip()
            except Exception:
                label = ""
            _CID_NAME_CACHE[cid] = label
        base = label or ("dream " + cid[:8])
        return "Dream · " + base + (" · " + stage if stage else "")
    if sid.startswith("v8:") and len(parts) >= 2:
        pid = parts[1]
        loop = parts[2] if len(parts) > 2 else ""
        name = _PROG_NAME_CACHE.get(pid)
        if name is None and r:
            name = ""
            try:
                raw = await r.hget(KEY_PROGRAMS, pid)
                if raw:
                    name = str(json.loads(_rd(raw)).get("name") or "").strip()
            except Exception:
                name = ""
            _PROG_NAME_CACHE[pid] = name
        return "V8 · " + (name or pid) + (" · " + loop if loop else "")
    if sid.startswith("evolve:"):
        return "Evolve · " + (parts[1] if len(parts) > 1 else sid)
    if sid.startswith("goal-") or sid.startswith("proj-") or sid.startswith("dream-"):
        return sid.split("-", 1)[0].title() + " · " + sid.split("-", 1)[1].replace("-", " ")
    return sid


async def _live_loop_events(prefixes: Optional[List[str]] = None,
                            limit: int = 40, include_stale: bool = False
                            ) -> List[Dict[str, Any]]:
    """Loop sessions from the loop session store — surfaces v8 / project_compose
    BACKGROUND work live so it can be monitored + re-attached mid-flight. A
    session marked 'running' whose last event is older than the stale window is
    reclassified 'interrupted' (its driver died) and, unless include_stale, is
    dropped — this is what stopped the UI showing dozens of phantom runs."""
    r = _redis()
    if not r:
        return []
    out = []
    now = time.time()
    try:
        ids = await r.zrevrange(KEY_LOOP_SESSIONS, 0, max(limit * 3, 90), withscores=True)
    except Exception:
        ids = []
    for iid, score in ids or []:
        sid = _rd(iid)
        if prefixes and not any(sid.startswith(p) for p in prefixes):
            continue
        try:
            run_raw = await r.hgetall(f"vera:loop:run:{sid}")
        except Exception:
            run_raw = {}
        run = {_rd(k): _rd(v) for k, v in (run_raw or {}).items()}
        if not run:
            continue
        status = run.get("status") or ""
        age = now - float(score or 0)
        stale = status == "running" and age > _LOOP_STALE_SECS
        running = status == "running" and not stale
        if stale:
            status = "interrupted"
        if not running and not include_stale:
            # only surface genuinely-live loops in the default (live) view
            if status not in ("running",):
                continue
        out.append(_ev(
            "loop_live", run.get("updated_at") or run.get("started_at") or now_iso(),
            await _friendly_loop_name(sid),
            summary=str(run.get("goal") or "")[:600],
            status=status, session_id=sid, ref=sid, source="loop",
            cap="dag.agent_loop", ui=_loop_ui(sid),
            extra={"last_event_ts": float(score or 0), "age_s": round(age, 1),
                   "running": running, "stale": stale}))
        if len(out) >= limit:
            break
    return out


async def _cap_activity_events(session_id: str = "", limit: int = 60
                               ) -> List[Dict[str, Any]]:
    """Recent cap.ok activity from the event stream, each mapped to the panel/
    element that renders that cap's output. Optionally scoped to a session."""
    r = _redis()
    if not r:
        return []
    out = []
    try:
        rows = await r.xrevrange("vera:events", count=600)
    except Exception:
        rows = []
    for _id, fields in rows or []:
        try:
            data = json.loads(_rd(fields.get(b"data") or fields.get("data") or "{}"))
        except Exception:
            continue
        if data.get("type") != "cap.ok":
            continue
        if session_id and str(data.get("session_id") or "") != session_id:
            continue
        capn = str(data.get("name") or "")
        ui = _ui_for_cap(capn)
        if not ui.get("url"):
            continue    # only surface caps that HAVE an associated UI
        out.append(_ev(
            "cap", data.get("ts") or "", capn,
            summary=str(data.get("preview") or "")[:400], status="ok",
            session_id=str(data.get("session_id") or ""), cap=capn,
            source="cap", ui={**ui, "cap": capn},
            extra={"group": data.get("group"),
                   "elapsed_ms": data.get("elapsed_ms")}))
        if len(out) >= limit:
            break
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  THE CAPABILITY
# ─────────────────────────────────────────────────────────────────────────────
@capability(
    "activity.timeline", memory="off", silent=True,
    http_method="GET", http_path="/activity/timeline", http_tags=["activity"],
    description="The UNIFIED activity timeline: a time-ordered event list for a "
                "SCOPE, composed from dream cycles, agentic loop runs, V8 loop "
                "programs, live background loop sessions, cap activity (each mapped "
                "to the panel/element that renders it) and artifacts. Inputs: scope "
                "(str — 'all' | 'project:<slug>' | 'goal:<slug>' | 'dream[:<trig>]' "
                "| 'program:<pid>' | 'chat:<session_id>'; default 'all'), limit "
                "(int=120), kinds (str — comma-filter of event kinds). Output: "
                "{scope, events:[{kind, ts, title, summary, status, session_id, "
                "cap, ui:{...}, extra}], count}.",
)
async def cap_activity_timeline(scope: str = "all", limit: int = 120,
                                kinds: str = "", trace_id=None) -> Dict:
    scope = (scope or "all").strip()
    events: List[Dict[str, Any]] = []
    lim = max(10, min(400, int(limit or 120)))

    kind, _, ref = scope.partition(":")
    kind = kind.lower()

    # The timeline is about the AGENTIC WORK the dream/project/goal/v8 systems
    # do — dream cycles, loop runs, programs, their artifacts. It deliberately
    # does NOT surface raw cap.ok activity (docker.ps, exec.ssh.run,
    # sandbox.session.sleep, …): that infra chatter is unrelated to the loops
    # and drowned everything out. Cap activity is only shown for an explicit
    # chat: scope, and even then only for that session.
    try:
        if kind in ("project", "goal") and ref:
            events += await _project_loop_events(ref, limit=lim)
            events += await _project_artifact_events(ref, limit=lim)
            events += await _dream_events(project=ref, limit=lim)
            # a goal/project may be driven by a V8 program keyed to it
            events += [e for e in await _program_events(limit=lim)
                       if e.get("extra", {}).get("owner_ref") == ref
                       or e.get("extra", {}).get("sandbox_owner") == f"goal-{ref}"]
            events += await _live_loop_events(
                prefixes=[f"goal-{ref}", f"proj-{ref}", f"v8-", f"dream:"], limit=lim)
        elif kind == "dream":
            events += await _dream_events(trigger=ref, limit=lim)
            events += await _live_loop_events(prefixes=["dream:", "dream-"], limit=lim)
        elif kind == "program" and ref:
            events += await _program_events(pid=ref, limit=lim)
            events += await _live_loop_events(prefixes=[f"v8:{ref}", f"v8-{ref}"], limit=lim)
        elif kind == "chat" and ref:
            events += await _cap_activity_events(session_id=ref, limit=lim)
            events += await _live_loop_events(prefixes=[ref], limit=lim)
        else:  # all — the master timeline (agentic work only, no cap chatter)
            events += await _dream_events(limit=min(lim, 40))
            events += await _program_events(limit=min(lim, 30))
            events += await _live_loop_events(limit=min(lim, 40))
    except Exception as e:
        log.warning("activity.timeline compose (%s): %s", scope, e)

    if kinds:
        want = {k.strip() for k in kinds.split(",") if k.strip()}
        events = [e for e in events if e.get("kind") in want]

    # newest first, de-dup by (kind, session_id|ts, title)
    seen = set()
    uniq = []
    for e in sorted(events, key=lambda x: str(x.get("ts") or ""), reverse=True):
        key = (e.get("kind"), e.get("session_id") or "", e.get("title"), e.get("ts"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)
    uniq = uniq[:lim]
    return {"scope": scope, "count": len(uniq), "events": uniq}


@capability(
    "activity.pipelines", memory="off", silent=True,
    http_method="GET", http_path="/activity/pipelines", http_tags=["activity"],
    description="The list of scopes the timeline can show — projects, goals, dream "
                "triggers, V8 programs — so a UI can offer a picker. Output: "
                "{pipelines:[{scope, label, kind, status, ref}]}.",
)
async def cap_activity_pipelines(trace_id=None) -> Dict:
    r = _redis()
    out: List[Dict[str, Any]] = [{"scope": "all", "label": "Everything",
                                  "kind": "all", "status": "", "ref": ""}]
    if not r:
        return {"pipelines": out}
    # projects / goals
    try:
        items = await r.hgetall(KEY_PROJECTS)
        for v in (items or {}).values():
            try:
                p = json.loads(_rd(v))
            except Exception:
                continue
            slug = p.get("slug") or ""
            if not slug:
                continue
            is_goal = any(t in (p.get("tags") or []) for t in ("strategic", "v7"))
            out.append({"scope": f"project:{slug}",
                        "label": p.get("name") or slug,
                        "kind": "goal" if is_goal else "project",
                        "status": p.get("status") or "active", "ref": slug})
    except Exception as e:
        log.debug("activity.pipelines projects: %s", e)
    # V8 programs
    try:
        items = await r.hgetall(KEY_PROGRAMS)
        for v in (items or {}).values():
            try:
                p = json.loads(_rd(v))
            except Exception:
                continue
            out.append({"scope": f"program:{p.get('id')}",
                        "label": "V8 · " + str(p.get("name") or p.get("id")),
                        "kind": "program", "status": p.get("status") or "",
                        "ref": p.get("id") or ""})
    except Exception as e:
        log.debug("activity.pipelines programs: %s", e)
    # dream triggers
    tl = CAPABILITY_REGISTRY.get("dream.trigger.list") or CAPABILITY_REGISTRY.get("dream.triggers.list")
    if tl and tl.get("func"):
        try:
            res = await tl["func"]()
            for t in (res or {}).get("triggers", []):
                nm = t.get("name") or ""
                if nm:
                    out.append({"scope": f"dream:{nm}",
                                "label": "Dream · " + (t.get("label") or nm),
                                "kind": "dream", "status": t.get("enabled") and "on" or "off",
                                "ref": nm})
        except Exception as e:
            log.debug("activity.pipelines triggers: %s", e)
    return {"pipelines": out}


# ─────────────────────────────────────────────────────────────────────────────
#  LOOP CONTROL — stop a loop, flatten duplicates/stale, list owner sandboxes.
#  These make the timeline actionable: nothing runs that you can't see + stop.
# ─────────────────────────────────────────────────────────────────────────────
def _scope_prefixes(scope: str) -> Optional[List[str]]:
    """Loop-session id prefixes that belong to a scope (None = all)."""
    kind, _, ref = (scope or "all").partition(":")
    kind = kind.lower()
    if kind in ("project", "goal") and ref:
        return [f"dream:", f"v8:", f"goal-{ref}", f"proj-{ref}"]
    if kind == "dream":
        return ["dream:"]
    if kind == "program" and ref:
        return [f"v8:{ref}"]
    if kind == "chat" and ref:
        return [ref]
    return None


async def _stop_loop(sid: str) -> bool:
    """Stop one loop session: pause its V8 program (if any), cancel the in-process
    engine task, mark the run-state stopped and drop it from the live set."""
    stopped = False
    if sid.startswith("v8:"):
        pid = sid.split(":")[1] if len(sid.split(":")) > 1 else ""
        pc = CAPABILITY_REGISTRY.get("loops.program.pause")
        if pid and pc and pc.get("func"):
            try:
                await pc["func"](id=pid)
                stopped = True
            except Exception:
                pass
    dag = _dag_mod()
    if dag is not None:
        tasks = getattr(dag, "_AGENT_LOOP_TASKS", {}) or {}
        t = tasks.get(sid)
        if t is not None and not t.done():
            try:
                t.cancel()
                stopped = True
            except Exception:
                pass
        tasks.pop(sid, None)
    r = _redis()
    if r:
        try:
            await r.hset(f"vera:loop:run:{sid}",
                         mapping={"status": "stopped", "updated_at": now_iso()})
            await r.zrem(KEY_LOOP_SESSIONS, sid)
        except Exception:
            pass
    return stopped


@capability(
    "activity.loops.stop", memory="off",
    http_method="POST", http_path="/activity/loops/stop", http_tags=["activity"],
    description="Stop a background loop by session_id: pauses its V8 program (if "
                "any), cancels the in-flight engine task, marks it stopped and "
                "removes it from the live set. Input: session_id (str!). "
                "Output: {ok, session_id, stopped}.",
)
async def cap_activity_loops_stop(session_id: str = "", trace_id=None) -> Dict:
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    stopped = await _stop_loop(session_id)
    await emit_event({"type": "activity.loop.stopped", "session_id": session_id,
                      "stopped": stopped})
    return {"ok": True, "session_id": session_id, "stopped": stopped}


@capability(
    "activity.loops.flatten", memory="off",
    http_method="POST", http_path="/activity/loops/flatten", http_tags=["activity"],
    description="Clean up background loops: (1) PRUNE stale/finished sessions "
                "left in the live set after restarts, and (2) DEDUPLICATE — for "
                "genuinely-running loops sharing an owner + goal, keep the newest "
                "and stop the rest. Inputs: scope (str — limit to a scope, default "
                "'all'), dry_run (bool=false — report without stopping anything). "
                "Output: {ok, pruned_stale, stopped_duplicates, kept, groups}.",
)
async def cap_activity_loops_flatten(scope: str = "all", dry_run: bool = False,
                                     trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}
    prefixes = _scope_prefixes(scope)
    now = time.time()
    try:
        ids = await r.zrevrange(KEY_LOOP_SESSIONS, 0, 2000, withscores=True)
    except Exception:
        ids = []
    pruned = 0
    groups: Dict[str, List[tuple]] = {}
    for iid, score in ids or []:
        sid = _rd(iid)
        if prefixes and not any(sid.startswith(p) for p in prefixes):
            continue
        try:
            run = {_rd(k): _rd(v) for k, v in
                   (await r.hgetall(f"vera:loop:run:{sid}") or {}).items()}
        except Exception:
            run = {}
        status = run.get("status") or ""
        live = status == "running" and (now - float(score or 0)) <= _LOOP_STALE_SECS
        if not live:
            # stale / done / error — clear it out of the live set
            if not dry_run:
                try:
                    await r.zrem(KEY_LOOP_SESSIONS, sid)
                    if status == "running":
                        await r.hset(f"vera:loop:run:{sid}",
                                     mapping={"status": "interrupted"})
                except Exception:
                    pass
            pruned += 1
            continue
        # dedup key: owner (first two id segments) + normalized goal
        segs = sid.split(":")
        owner = ":".join(segs[:2]) if len(segs) > 1 else sid
        goalk = " ".join(str(run.get("goal") or "").lower().split())[:120]
        groups.setdefault(f"{owner}|{goalk}", []).append((sid, float(score or 0)))
    stopped = 0
    kept: List[str] = []
    group_report = []
    for key, arr in groups.items():
        arr.sort(key=lambda x: -x[1])   # newest first
        kept.append(arr[0][0])
        dupes = [s for s, _ in arr[1:]]
        if dupes and not dry_run:
            for s in dupes:
                if await _stop_loop(s):
                    stopped += 1
        elif dupes:
            stopped += len(dupes)
        if len(arr) > 1:
            group_report.append({"key": key, "kept": arr[0][0], "stopped": dupes})
    await emit_event({"type": "activity.loops.flattened", "scope": scope,
                      "pruned_stale": pruned, "stopped_duplicates": stopped,
                      "dry_run": dry_run})
    return {"ok": True, "scope": scope, "dry_run": dry_run,
            "pruned_stale": pruned, "stopped_duplicates": stopped,
            "kept": kept, "groups": group_report}


@capability(
    "activity.sandboxes", memory="off", silent=True,
    http_method="GET", http_path="/activity/sandboxes", http_tags=["activity"],
    description="The sandbox containers tied to a scope (project/goal/program/"
                "dream), each with a terminal URL and its session count — so a "
                "goal/pipeline's containers + terminals are reachable straight "
                "from the activity view. Inputs: scope (str, default 'all'). "
                "Output: {scope, sandboxes:[{session_id, kind, label, container, "
                "state, active, sessions, terminal_url, docker_host_id}]}.",
)
async def cap_activity_sandboxes(scope: str = "all", trace_id=None) -> Dict:
    lister = CAPABILITY_REGISTRY.get("sandbox.session.list")
    if not lister or not lister.get("func"):
        return {"scope": scope, "sandboxes": []}
    try:
        res = await lister["func"]()
    except Exception as e:
        return {"scope": scope, "sandboxes": [], "error": str(e)}
    boxes = (res or {}).get("sandboxes", [])
    kind, _, ref = (scope or "all").partition(":")
    kind = kind.lower()
    from urllib.parse import quote

    def _match(b):
        sid = str(b.get("session_id") or "")
        if kind in ("project", "goal") and ref:
            return sid in (f"goal-{ref}", f"proj-{ref}") or ref in sid
        if kind == "program" and ref:
            return sid == f"v8-{ref}" or sid.startswith(f"v8:{ref}")
        if kind == "dream":
            return sid.startswith("dream-") or sid.startswith("dream:")
        return True

    out = []
    for b in boxes:
        if not _match(b):
            continue
        sid = str(b.get("session_id") or "")
        out.append({**b,
                    "terminal_url": f"/remote/sandbox/terminal?session_id={quote(sid, safe='')}"})
    return {"scope": scope, "sandboxes": out, "count": len(out)}


@capability(
    "activity.stream.remove", memory="off",
    http_method="POST", http_path="/activity/stream/remove", http_tags=["activity"],
    description="Remove an activity stream and its running work: for a V8 program "
                "stops its loops + deletes the program; for a project/goal stops "
                "its loops, deletes its driving V8 program (if any) and deletes "
                "(or archives) the project. Inputs: target (str! — 'program:<pid>' "
                "| 'project:<slug>' | 'goal:<slug>'), archive (bool=false — archive "
                "the project instead of deleting). Output: {ok, removed, stopped}.",
)
async def cap_activity_stream_remove(target: str = "", archive: bool = False,
                                     trace_id=None) -> Dict:
    kind, _, ref = (target or "").partition(":")
    kind = kind.lower()
    if not ref:
        return {"ok": False, "error": "target required (program:<id>|project:<slug>|goal:<slug>)"}
    stopped = 0
    removed = []
    r = _redis()
    # Stop loops belonging to the stream first.
    fl = await cap_activity_loops_flatten(scope=target, dry_run=False)
    stopped += int(fl.get("stopped_duplicates", 0) or 0)
    if kind == "program":
        # stop the program's in-flight loop, then delete it
        try:
            for iid in (await r.zrevrange(KEY_LOOP_SESSIONS, 0, 2000) if r else []):
                sid = _rd(iid)
                if sid.startswith(f"v8:{ref}"):
                    if await _stop_loop(sid):
                        stopped += 1
        except Exception:
            pass
        dc = CAPABILITY_REGISTRY.get("loops.program.delete")
        if dc and dc.get("func"):
            await dc["func"](id=ref)
            removed.append(target)
    elif kind in ("project", "goal"):
        # delete the driving V8 program too, if this project references one
        pid = ""
        if r:
            try:
                raw = await r.hget(KEY_PROJECTS, ref)
                if raw:
                    for t in (json.loads(_rd(raw)).get("tags") or []):
                        if str(t).startswith("v8-program:"):
                            pid = str(t).split(":", 1)[1]
            except Exception:
                pid = ""
        if pid:
            dc = CAPABILITY_REGISTRY.get("loops.program.delete")
            if dc and dc.get("func"):
                await dc["func"](id=pid)
                removed.append(f"program:{pid}")
        pc = CAPABILITY_REGISTRY.get("project.delete")
        if archive:
            up = CAPABILITY_REGISTRY.get("project.upsert")
            g = CAPABILITY_REGISTRY.get("project.get")
            if g and up and g.get("func"):
                try:
                    proj = (await g["func"](slug=ref)).get("project") or {}
                    if proj:
                        proj["status"] = "archived"
                        await up["func"](**{k: proj.get(k) for k in
                                            ("name", "slug", "description")}, status="archived")
                        removed.append(f"{target} (archived)")
                except Exception:
                    pass
        elif pc and pc.get("func"):
            await pc["func"](slug=ref)
            removed.append(target)
    else:
        return {"ok": False, "error": f"unknown target kind: {kind}"}
    await emit_event({"type": "activity.stream.removed", "target": target,
                      "removed": removed, "stopped": stopped})
    return {"ok": True, "target": target, "removed": removed, "stopped": stopped}


@capability(
    "activity.files", memory="off", silent=True,
    http_method="GET", http_path="/activity/files", http_tags=["activity"],
    description="Browse the FILES the work for a scope produced — the /workspace "
                "of the scope's own sandbox container (goal-<slug> / v8-<pid> / "
                "dream-<trigger>) — plus any recorded artifacts. So the deliverables "
                "a loop claims it built (a portal, a report, …) are actually "
                "reachable from the activity view. Inputs: scope (str!), path (str — "
                "dir inside the container, default /workspace). Output: {scope, "
                "owner, container, running, path, entries:[{name,path,kind,size}], "
                "artifacts:[{id,name,type,size}]}.",
)
async def cap_activity_files(scope: str = "all", path: str = "", trace_id=None) -> Dict:
    owners = await _scope_owner_keys(scope)
    sbx = _sbx_mod()
    out: Dict[str, Any] = {"scope": scope, "owner": "", "container": "",
                           "running": False, "path": path or "/workspace",
                           "entries": [], "artifacts": []}
    # Pick the first owner key that actually has a sandbox container.
    if sbx is not None:
        st_cap = CAPABILITY_REGISTRY.get("sandbox.session.status")
        for ok in owners:
            try:
                st = await st_cap["func"](session_id=ok) if st_cap else {}
            except Exception:
                st = {}
            if st.get("exists"):
                out["owner"] = ok
                out["container"] = st.get("container", "")
                out["running"] = bool(st.get("running"))
                try:
                    br = await sbx.route_fs_browse(ok, path or "/workspace")
                    if isinstance(br, dict) and not br.get("error"):
                        out["path"] = br.get("path", path or "/workspace")
                        out["parent"] = br.get("parent")
                        out["entries"] = br.get("entries", [])
                except Exception as e:
                    out["error"] = str(e)[:200]
                break
    # Artifacts (project/goal scope) from the project ledger.
    kind, _, ref = (scope or "").partition(":")
    if kind in ("project", "goal") and ref:
        ac = CAPABILITY_REGISTRY.get("project.artifacts.list")
        if ac and ac.get("func"):
            try:
                ar = await ac["func"](slug=ref, limit=200)
                out["artifacts"] = (ar or {}).get("artifacts", [])
            except Exception:
                pass
    return out


@APP.get("/activity/file", include_in_schema=False)
async def _activity_file(scope: str = "", session_id: str = "", path: str = ""):
    """Serve one file from a scope's sandbox (or a named container session) so
    activity deliverables can be viewed/downloaded straight from the timeline."""
    from fastapi.responses import PlainTextResponse, JSONResponse
    sbx = _sbx_mod()
    if not path or sbx is None:
        return JSONResponse({"error": "path required / sandbox module unavailable"},
                            status_code=400)
    owner = session_id
    if not owner:
        for ok in await _scope_owner_keys(scope):
            st_cap = CAPABILITY_REGISTRY.get("sandbox.session.status")
            try:
                st = await st_cap["func"](session_id=ok) if st_cap else {}
            except Exception:
                st = {}
            if st.get("exists"):
                owner = ok
                break
    if not owner:
        return JSONResponse({"error": "no sandbox for this scope"}, status_code=404)
    try:
        res = await sbx.route_fs_read(owner, path, max_bytes=2_000_000)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)
    if not isinstance(res, dict) or res.get("error"):
        return JSONResponse({"error": (res or {}).get("error", "read failed")},
                            status_code=404)
    fname = path.rstrip("/").split("/")[-1] or "file"
    return PlainTextResponse(
        res.get("content", ""),
        headers={"Content-Disposition": f'inline; filename="{fname}"'})


# ─────────────────────────────────────────────────────────────────────────────
#  ELEMENT + PAGE
# ─────────────────────────────────────────────────────────────────────────────
@APP.get("/ui/elements/activity_timeline.js", include_in_schema=False)
async def _serve_activity_timeline_js():
    from fastapi.responses import Response
    p = _HERE.parent / "activity_timeline_element.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})
    return Response(content="console.warn('activity_timeline element JS not found');",
                    media_type="application/javascript")


@APP.get("/activity/panel", include_in_schema=False)
async def _activity_panel():
    from fastapi.responses import HTMLResponse
    p = _HERE / "activity_panel.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<pre>activity_panel.html not found</pre>", status_code=404)


_INJECT_HTML = (
    '<script id="vera-activity-timeline-js-include" src="/ui/elements/activity_timeline.js"></script>\n'
    '<vera-activity-timeline style="display:block;width:100%;height:100%"></vera-activity-timeline>'
)

register_ui(
    panel_id="activity-timeline",
    label="Activity",
    icon="↔",
    mode="inject",
    tab_order=203,
    html=_INJECT_HTML,
    ui_caps=["activity.timeline", "activity.pipelines"],
)

register_ui(
    panel_id="activity",
    label="Activity",
    icon="🗓",
    mode="tab",
    tab_order=42,
    html=('<div style="height:100%;display:flex;flex-direction:column">'
          '<iframe src="/activity/panel" style="flex:1;border:none;width:100%;'
          'height:100%;background:var(--bg0,#181614)" '
          'allow="clipboard-read; clipboard-write"></iframe></div>'),
    ui_caps=["activity.timeline", "activity.pipelines"],
)

log.info("activity_capabilities loaded — unified timeline (activity.timeline, "
         "<vera-activity-timeline>, /activity/panel)")
