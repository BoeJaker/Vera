"""agents.work.* - the agent work registry (compute-roles L1, owner 11 Oct 2026). See agent_work_core.

An agent session registers each scheduled trigger (session cron, host job, Loop Lab schedule, one-shot)
and heartbeats it; the owner schedules work FOR a session (`agents.work.request` - session, brief, board
items, time) and the session picks it up (`agents.work.pending` at its own wake-ups, then `ack`). Vera's
UI reads `agents.work.list` to draw the work windows. A read model: nothing here marks the box busy.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, emit_event

try:
    from Vera.vera.agents import agent_work_core as core
except ImportError:                                          # pragma: no cover
    from vera.agents import agent_work_core as core          # type: ignore

log = logging.getLogger("vera.agents.work")

KEY = "vera:agents:work"
MAX_ENTRIES = 500


def _r():
    return _orch.REDIS


async def _all() -> List[Dict[str, Any]]:
    r = _r()
    if r is None:
        return []
    raw = await r.hgetall(KEY) or {}
    out = []
    for v in raw.values():
        try:
            out.append(json.loads(v.decode() if isinstance(v, (bytes, bytearray)) else v))
        except Exception:
            continue
    return out


async def _get(eid: str) -> Optional[Dict[str, Any]]:
    r = _r()
    if r is None or not eid:
        return None
    v = await r.hget(KEY, eid)
    try:
        return json.loads(v.decode() if isinstance(v, (bytes, bytearray)) else v) if v else None
    except Exception:
        return None


async def _put(e: Dict[str, Any]) -> None:
    r = _r()
    if r is None:
        raise RuntimeError("no Redis")
    await r.hset(KEY, e["id"], json.dumps(e, default=str))
    if await r.hlen(KEY) > MAX_ENTRIES:                       # drop the oldest finished ones
        ents = sorted(await _all(), key=lambda x: x.get("updated_at", ""))
        for x in ents:
            if await r.hlen(KEY) <= MAX_ENTRIES:
                break
            if core.view_status(x) in ("done", "cancelled", "declined", "expired", "failed"):
                await r.hdel(KEY, x["id"])


def _obj(x: Any) -> Dict[str, Any]:
    if isinstance(x, str):
        try:
            x = json.loads(x)
        except ValueError:
            return {}
    return x if isinstance(x, dict) else {}


@capability("agents.work.register", memory="off",
            http_method="POST", http_path="/agents/work/register", http_tags=["agents", "schedule"],
            description=("Register (or update) one of an agent session's scheduled triggers so Vera's UI shows "
                         "when it runs and on what. Input: entry (object!) - kind (session_cron|host_job|loop_lab|"
                         "one_shot), title, session (name), session_id, when (ISO UTC next fire), window_end, "
                         "repeat, what (prompt/script), board_items, branch, log, status (armed|running|done|"
                         "failed|cancelled), note; id to update. Never marks the box busy. Output: {ok, entry}."))
async def cap_agents_work_register(entry: Any = None, trace_id=None) -> Dict[str, Any]:
    e = _obj(entry)
    if e.get("kind") == "request":
        return {"ok": False, "error": "use agents.work.request for work FOR a session"}
    try:
        prev = await _get(str(e.get("id") or "")) if e.get("id") else None
        rec = core.normalize(e, prev=prev)
        await _put(rec)
    except (ValueError, RuntimeError) as ex:
        return {"ok": False, "error": str(ex)}
    await emit_event({"type": "agents.work.changed", "id": rec["id"], "kind": rec["kind"],
                      "session": rec["session"], "status": rec["status"]})
    return {"ok": True, "entry": rec}


@capability("agents.work.heartbeat", memory="off", silent=True,
            http_method="POST", http_path="/agents/work/heartbeat", http_tags=["agents", "schedule"],
            description=("A registered trigger or claimed request is alive / changed state. Input: id (str!), "
                         "status (optional), note (optional), when (optional next fire). Output: {ok, entry}."))
async def cap_agents_work_heartbeat(id: str = "", status: str = "", note: str = "", when: str = "",
                                    trace_id=None) -> Dict[str, Any]:
    prev = await _get((id or "").strip())
    if not prev:
        return {"ok": False, "error": "no work entry %r" % id}
    upd = {"id": prev["id"]}
    for k, v in (("status", status), ("note", note), ("when", when)):
        if v:
            upd[k] = v
    try:
        rec = core.normalize(upd, prev=prev)
        if status == "done":
            rec = core.roll(rec) or rec          # B2: a repeating trigger re-arms at its next fire
        await _put(rec)
    except (ValueError, RuntimeError) as ex:
        return {"ok": False, "error": str(ex)}
    await emit_event({"type": "agents.work.changed", "id": rec["id"], "kind": rec["kind"],
                      "session": rec["session"], "status": rec["status"]})
    return {"ok": True, "entry": rec}


@capability("agents.work.list", memory="off", silent=True,
            http_method="GET", http_path="/agents/work/list", http_tags=["agents", "schedule"],
            description=("Agent work windows for the UI: every registered trigger and owner request, soonest "
                         "first, with view_status (a trigger long past its time with no heartbeat shows "
                         "'expired'). Input: session (filter), kind (filter), include_done (bool). "
                         "Output: {entries, count}."))
async def cap_agents_work_list(session: str = "", kind: str = "", include_done: bool = False,
                               trace_id=None) -> Dict[str, Any]:
    ents = core.listing(await _all(), session=(session or "").strip(), kind=(kind or "").strip(),
                        include_done=bool(include_done))
    return {"entries": ents, "count": len(ents)}


@capability("agents.work.request", memory="off",
            http_method="POST", http_path="/agents/work/request", http_tags=["agents", "schedule"],
            description=("Schedule work FOR an agent session from Vera: session (str! - its name, or 'any'), "
                         "title (str!), what (the brief), board_items (list), when (ISO UTC; empty = now), "
                         "window_end, limits ({gpu: bool, max_hours: n}), owner. The session picks it up with "
                         "agents.work.pending and acks it. Output: {ok, entry}."))
async def cap_agents_work_request(session: str = "", title: str = "", what: str = "", board_items: Any = None,
                                  when: str = "", window_end: str = "", limits: Any = None, owner: str = "",
                                  repeat: str = "", trace_id=None) -> Dict[str, Any]:
    try:
        rec = core.normalize({"kind": "request", "session": session, "title": title, "what": what,
                              "board_items": board_items or [], "when": when, "window_end": window_end,
                              "limits": _obj(limits), "owner": owner or "owner", "status": "pending",
                              "repeat": repeat})
        await _put(rec)
    except (ValueError, RuntimeError) as ex:
        return {"ok": False, "error": str(ex)}
    await emit_event({"type": "agents.work.requested", "id": rec["id"], "session": rec["session"],
                      "title": rec["title"]})
    return {"ok": True, "entry": rec}


@capability("agents.work.pending", memory="off", silent=True,
            http_method="GET", http_path="/agents/work/pending", http_tags=["agents", "schedule"],
            description=("The owner's requests a session should pick up now (pending, for it or 'any', due). "
                         "Input: session (str!). Output: {requests}."))
async def cap_agents_work_pending(session: str = "", trace_id=None) -> Dict[str, Any]:
    if not (session or "").strip():
        return {"requests": [], "error": "session is required"}
    return {"requests": core.pending_for(await _all(), session.strip())}


@capability("agents.work.ack", memory="off",
            http_method="POST", http_path="/agents/work/ack", http_tags=["agents", "schedule"],
            description=("A session takes or settles an owner request. Input: id (str!), status (claimed|done|"
                         "declined), session (who took it), note. Output: {ok, entry}."))
async def cap_agents_work_ack(id: str = "", status: str = "claimed", session: str = "", note: str = "",
                              trace_id=None) -> Dict[str, Any]:
    prev = await _get((id or "").strip())
    if not prev or prev.get("kind") != "request":
        return {"ok": False, "error": "no request %r" % id}
    if status == "claimed" and prev.get("status") != "pending":
        return {"ok": False, "error": "request is %s, not pending" % prev.get("status")}
    upd = {"id": prev["id"], "status": status, "note": note}
    if session:
        upd["session_id"] = session
    try:
        rec = core.normalize(upd, prev=prev)
        if status == "done":
            rec = core.roll(rec) or rec          # B2: a repeating request comes back at its next time
        await _put(rec)
    except (ValueError, RuntimeError) as ex:
        return {"ok": False, "error": str(ex)}
    await emit_event({"type": "agents.work.changed", "id": rec["id"], "kind": "request",
                      "session": rec["session"], "status": rec["status"]})
    return {"ok": True, "entry": rec}



# ── looplab-control B2/B5 (17 Oct 2026): read one, cancel one, cancel a session's whole schedule ─────
@capability("agents.work.get", memory="off", silent=True,
            http_method="GET", http_path="/agents/work/get", http_tags=["agents", "schedule"],
            description="One work entry with its view_status (as `state`). Input: id (str!). Output: {entry, state}.")
async def cap_agents_work_get(id: str = "", trace_id=None) -> Dict[str, Any]:
    e = await _get((id or "").strip())
    if not e:
        return {"error": "no work entry %r" % id}
    return {"entry": e, "state": core.view_status(e)}


@capability("agents.work.cancel", memory="off",
            http_method="POST", http_path="/agents/work/cancel", http_tags=["agents", "schedule"],
            description="Cancel one trigger or request (it stops repeating). Input: id (str!), note. Output: {ok, entry}.")
async def cap_agents_work_cancel(id: str = "", note: str = "", trace_id=None) -> Dict[str, Any]:
    prev = await _get((id or "").strip())
    if not prev:
        return {"ok": False, "error": "no work entry %r" % id}
    rec = dict(prev, status="cancelled", note=(note or prev.get("note") or "")[:400],
               updated_at=core._iso())
    await _put(rec)
    await emit_event({"type": "agents.work.changed", "id": rec["id"], "kind": rec["kind"],
                      "session": rec["session"], "status": "cancelled"})
    return {"ok": True, "entry": rec}


@capability("agents.work.cancel_session", memory="off",
            http_method="POST", http_path="/agents/work/cancel_session", http_tags=["agents", "schedule"],
            description="Cancel everything scheduled for or by one session (its triggers and the requests for it). "
                        "Input: session (str!). Output: {ok, cancelled}.")
async def cap_agents_work_cancel_session(session: str = "", trace_id=None) -> Dict[str, Any]:
    s = (session or "").strip()
    if not s:
        return {"ok": False, "error": "session is required"}
    n = 0
    for e in await _all():
        if e.get("session") == s and core.view_status(e) in ("armed", "running", "pending", "claimed"):
            await cap_agents_work_cancel(id=e["id"], note="cancelled with the session's schedule")
            n += 1
    return {"ok": True, "cancelled": n}
