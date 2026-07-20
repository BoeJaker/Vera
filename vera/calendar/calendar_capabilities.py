"""
calendar_capabilities.py — Vera Scheduler & Diary
=================================================

A personal scheduling/diary system for Vera:

  • Local diary stored in Redis (events / todos / notes), with a sorted-set
    index by start time for fast date-range queries.
  • Cloud sync (pull) from configurable sources: Google Calendar, generic
    CalDAV, and ICS subscription URLs — all via httpx (no heavyweight deps).
  • Brain-dump: free text → the local Ollama cluster turns it into a coherent
    set of resources (events, todos, notes, a suggested daily plan) for review.
  • Optional persistence of the diary into the data fabric (`diary` dataset).
  • Its own top-level "Calendar" UI tab.

Cloud credentials (Google OAuth secret + refresh token, CalDAV app-password)
are sealed with Fernet (see calendar/secrets.py) before they touch Redis and
are NEVER returned to the UI.

Capabilities (group `cal.*`)
────────────────────────────
  cal.events.list / cal.event.upsert / cal.event.delete
  cal.todos.list  / cal.todo.upsert  / cal.todo.toggle / cal.todo.delete
  cal.notes.list  / cal.note.upsert  / cal.note.delete
  cal.braindump   / cal.braindump.commit
  cal.assistant.briefing / cal.assistant.config / cal.assistant.handover
  cal.sources.list / cal.source.upsert / cal.source.delete
  cal.sync.run    / cal.sync.status
  cal.google.auth_url / cal.google.auth_complete
  cal.fabric.persist
  cal.config.get  / cal.config.set
  cal.panel.html  (serves /cal/panel)

Redis layout
────────────
  vera:cal:events          hash  id   -> JSON
  vera:cal:events:by_start  zset  start_epoch -> id
  vera:cal:todos           hash  id   -> JSON
  vera:cal:notes           hash  id   -> JSON
  vera:cal:sources         hash  sid  -> JSON (secrets sealed)
  vera:cal:config          string JSON
  vera:cal:sync:status     string JSON
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import html as _html
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP,
    capability,
    emit_event,
    now_iso,
    ollama_generate,
    register_ui,
    schedule,
)
from Vera.vera.config import cfg
from Vera.vera.calendar import ical
from Vera.vera.security import secrets as cal_secrets

log = logging.getLogger("vera.calendar")

_HERE = Path(__file__).parent
_PANEL_HTML_PATH = _HERE / "calendar_panel.html"

# ─────────────────────────────────────────────────────────────────────────────
# REDIS KEYS / DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────

KEY_EVENTS      = "vera:cal:events"
KEY_EVENTS_ZS   = "vera:cal:events:by_start"
KEY_TODOS       = "vera:cal:todos"
KEY_NOTES       = "vera:cal:notes"
KEY_SOURCES     = "vera:cal:sources"
KEY_CONFIG      = "vera:cal:config"
KEY_SYNC_STATUS = "vera:cal:sync:status"

# Secret field names that must be Fernet-sealed before storage and never
# returned to the UI.
_SECRET_FIELDS = ("password", "client_secret", "refresh_token", "access_token")

DEFAULT_CONFIG: Dict[str, Any] = {
    "model":              "",         # blank -> cluster default (OLLAMA_MODEL)
    "sync_interval_min":  15,         # 0 disables the background poller
    "fabric_auto_persist": False,
    "fabric_dataset":     "diary",
    "tz":                 "UTC",
    "default_source":     "local",
}

GOOGLE_AUTH   = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN  = "https://oauth2.googleapis.com/token"
GOOGLE_API    = "https://www.googleapis.com/calendar/v3"
GOOGLE_SCOPE  = "https://www.googleapis.com/auth/calendar.readonly"

# Google OAuth uses a redirect flow: after the user approves, Google redirects
# the browser back to the orchestrator's callback route (mounted below) which
# captures the refresh token automatically. The redirect base lives in config
# (cfg.GOOGLE_OAUTH_REDIRECT_BASE) — by default the address Vera is reached on
# (BACKEND_HOST:port), which needs a Google "Web application" client with this
# exact callback URI registered; a loopback base + "Desktop app" client also
# works for same-host use. (The legacy urn:ietf:wg:oauth:2.0:oob paste-code flow
# was retired by Google for clients created after Feb 2022.)
GOOGLE_REDIRECT_PATH = "/cal/google/oauth_callback"

# OAuth `state` → source-id handoff: CSRF guard, and tells the callback which
# source to seal the token onto. Short-lived and consumed on first use.
KEY_OAUTH_STATE  = "vera:cal:google:oauth_state:"   # + state token
_OAUTH_STATE_TTL = 600   # seconds


def _google_redirect_uri() -> str:
    """The loopback callback URI registered with Google. The auth request and the
    token exchange must send the *identical* value or Google rejects it."""
    return cfg.GOOGLE_OAUTH_REDIRECT_BASE.rstrip("/") + GOOGLE_REDIRECT_PATH

SOURCE_COLORS = {"local": "#4a9eff", "ics": "#28c28a",
                 "caldav": "#f5b341", "google": "#ef5b5b"}


def _redis():
    """Return the live shared Redis pool — never open a new connection."""
    return getattr(_orch, "REDIS", None)


# ─────────────────────────────────────────────────────────────────────────────
# DATE / RANGE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> _dt.datetime:
    return _dt.datetime.utcnow()


def _epoch(iso: str) -> float:
    """Best-effort epoch seconds from an ISO date/datetime; 0.0 on failure."""
    if not iso:
        return 0.0
    try:
        s = iso.replace("Z", "+00:00")
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo:
            return d.timestamp()
        return d.replace(tzinfo=_dt.timezone.utc).timestamp()
    except Exception:
        try:                                   # date-only
            d = _dt.datetime.strptime(iso[:10], "%Y-%m-%d")
            return d.replace(tzinfo=_dt.timezone.utc).timestamp()
        except Exception:
            return 0.0


def _resolve_range(start: str, end: str) -> tuple:
    """Resolve start/end into epoch seconds.

    Accepts ISO dates/datetimes or relative keywords:
      today | tomorrow | yesterday | week | month | all | "" (no bound)
    """
    now = _now()
    start = (start or "").strip().lower()
    end = (end or "").strip().lower()

    def kw(word, which):
        d0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if word == "today":
            return d0, d0 + _dt.timedelta(days=1)
        if word == "tomorrow":
            return d0 + _dt.timedelta(days=1), d0 + _dt.timedelta(days=2)
        if word == "yesterday":
            return d0 - _dt.timedelta(days=1), d0
        if word == "week":
            mon = d0 - _dt.timedelta(days=d0.weekday())
            return mon, mon + _dt.timedelta(days=7)
        if word == "month":
            first = d0.replace(day=1)
            nxt = (first + _dt.timedelta(days=32)).replace(day=1)
            return first, nxt
        return None

    # Keyword on the start field can define the whole window.
    rng = kw(start, "start")
    if rng:
        s_dt, e_dt = rng
        s = s_dt.replace(tzinfo=_dt.timezone.utc).timestamp()
        e = e_dt.replace(tzinfo=_dt.timezone.utc).timestamp()
        return s, e

    if start in ("", "all"):
        s = 0.0
    else:
        s = _epoch(start)
    if end in ("", "all"):
        e = float(2 ** 40)            # far future
    else:
        e = _epoch(end)
    return s, e


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

async def _get_config() -> Dict[str, Any]:
    r = _redis()
    if not r:
        return dict(DEFAULT_CONFIG)
    try:
        raw = await r.get(KEY_CONFIG)
        merged = dict(DEFAULT_CONFIG)
        if raw:
            merged.update(json.loads(raw))
        return merged
    except Exception as e:
        log.warning("cal config read: %s", e)
        return dict(DEFAULT_CONFIG)


async def _save_config(data: Dict[str, Any]):
    r = _redis()
    if not r:
        return
    try:
        await r.set(KEY_CONFIG, json.dumps(data))
    except Exception as e:
        log.warning("cal config save: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# EVENT STORE
# ─────────────────────────────────────────────────────────────────────────────

async def _store_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    r = _redis()
    ev.setdefault("id", str(uuid.uuid4()))
    ev.setdefault("source", "local")
    ev.setdefault("all_day", False)
    ev.setdefault("tags", [])
    ev["updated"] = now_iso()
    ev.setdefault("created", ev["updated"])
    if r:
        await r.hset(KEY_EVENTS, ev["id"], json.dumps(ev))
        await r.zadd(KEY_EVENTS_ZS, {ev["id"]: _epoch(ev.get("start", ""))})
    return ev


async def _delete_event(eid: str) -> bool:
    r = _redis()
    if not r:
        return False
    n = await r.hdel(KEY_EVENTS, eid)
    await r.zrem(KEY_EVENTS_ZS, eid)
    return bool(n)


async def _events_in_range(s_epoch: float, e_epoch: float,
                           source: str = "") -> List[Dict]:
    r = _redis()
    if not r:
        return []
    ids = await r.zrangebyscore(KEY_EVENTS_ZS, s_epoch, e_epoch)
    if not ids:
        return []
    raw = await r.hmget(KEY_EVENTS, *ids)
    out = []
    for item in raw:
        if not item:
            continue
        try:
            ev = json.loads(item)
        except Exception:
            continue
        if source and ev.get("source") != source:
            continue
        out.append(ev)
    out.sort(key=lambda e: _epoch(e.get("start", "")))
    return out


async def _upsert_synced_event(src_id: str, color: str, vev: Dict,
                               extra: Optional[Dict] = None) -> str:
    """Upsert an event pulled from a cloud source, keyed by (source, uid).

    ``extra`` carries optional per-layer metadata (e.g. ``calendar`` /
    ``calendar_label`` for individual Google calendars) merged onto the event.
    """
    r = _redis()
    if not r:
        return ""
    eid = f"{src_id}:{vev.get('uid') or uuid.uuid4()}"
    ev = {
        "id": eid,
        "title": vev.get("title", "(untitled)"),
        "start": vev.get("start", ""),
        "end": vev.get("end", ""),
        "all_day": bool(vev.get("all_day")),
        "location": vev.get("location", ""),
        "description": vev.get("description", ""),
        "source": src_id,
        "color": color,
        "uid": vev.get("uid", ""),
        "tags": [],
    }
    if extra:
        ev.update(extra)
    existing = await r.hget(KEY_EVENTS, eid)
    created = False
    if existing:
        try:
            ev["created"] = json.loads(existing).get("created", now_iso())
        except Exception:
            ev["created"] = now_iso()
    else:
        created = True
    await _store_event(ev)
    return "added" if created else "updated"


# ─────────────────────────────────────────────────────────────────────────────
# SIMPLE HASH STORES (todos / notes)
# ─────────────────────────────────────────────────────────────────────────────

async def _hash_upsert(key: str, rec: Dict[str, Any]) -> Dict[str, Any]:
    r = _redis()
    rec.setdefault("id", str(uuid.uuid4()))
    rec["updated"] = now_iso()
    rec.setdefault("created", rec["updated"])
    if r:
        await r.hset(key, rec["id"], json.dumps(rec))
    return rec


async def _hash_list(key: str) -> List[Dict]:
    r = _redis()
    if not r:
        return []
    items = await r.hgetall(key)
    out = []
    for v in items.values():
        try:
            out.append(json.loads(v))
        except Exception:
            continue
    return out


async def _hash_delete(key: str, rid: str) -> bool:
    r = _redis()
    if not r:
        return False
    return bool(await r.hdel(key, rid))


# ═════════════════════════════════════════════════════════════════════════════
#  EVENTS
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "cal.events.list", http_method="GET", http_path="/cal/events",
    http_tags=["calendar"], memory="off", silent=True,
    description="List diary events in a date range. "
                "Input: start (ISO date or today|tomorrow|week|month|all), "
                "end (ISO date or all), source (filter by source id, optional), "
                "include_dreams (bool — overlay projected scheduled dream fires as "
                "read-only events). "
                "Output: {events:[{id,title,start,end,all_day,location,source,color}]}.",
)
async def cap_events_list(start: str = "week", end: str = "",
                          source: str = "", include_dreams: bool = False,
                          trace_id=None):
    s, e = _resolve_range(start, end)
    events = await _events_in_range(s, e, source)

    # Virtual overlay: projected scheduled dream fires, computed on the fly by
    # the dream module and merged read-only. Never stored, so they always track
    # the live trigger schedules. Suppressed when filtering to a specific source.
    if include_dreams and source in ("", "dream"):
        dream_events = await _dream_overlay_events(s, e)
        events = events + dream_events

    events.sort(key=lambda ev: ev.get("start", ""))
    return {"events": events, "count": len(events)}


async def _dream_overlay_events(s_epoch: float, e_epoch: float) -> List[Dict[str, Any]]:
    """Pull projected dream fires from dream.schedule.events and shape them like
    calendar events, filtered to the [s_epoch, e_epoch] window."""
    reg = getattr(_orch, "CAPABILITY_REGISTRY", {}) or {}
    cap = reg.get("dream.schedule.events")
    if not cap:
        return []
    # Cover the requested window (plus today) — cap days_ahead at the cap's max.
    try:
        days = int((e_epoch - _now().replace(tzinfo=_dt.timezone.utc).timestamp())
                   / 86400) + 1
    except Exception:
        days = 7
    days = max(1, min(30, days))
    try:
        res = await cap["func"](days_ahead=days, max_per_trigger=50)
    except Exception as ex:
        log.debug("dream overlay fetch failed: %s", ex)
        return []
    out: List[Dict[str, Any]] = []
    for ev in (res.get("events", []) if isinstance(res, dict) else []):
        st = _epoch(ev.get("start", ""))
        if st and (st < s_epoch or st > e_epoch):
            continue
        out.append({
            "id":       ev.get("id"),
            "title":    ev.get("title", ev.get("label", "Dream")),
            "start":    ev.get("start"),
            "end":      ev.get("end"),
            "all_day":  bool(ev.get("all_day")),
            "location": "",
            "source":   "dream",
            "color":    ev.get("color", "#a78bfa"),
            "read_only": True,
            "trigger":  ev.get("trigger"),
            "project":  ev.get("project", ""),
            "mode":     ev.get("mode", ""),
            "recurrence": ev.get("recurrence", ""),
        })
    return out


@capability(
    "cal.event.upsert", http_method="POST", http_path="/cal/event/upsert",
    http_tags=["calendar"], memory="on",
    description="Create or update a diary event. "
                "Input: id (str — omit to create), title (str!), start (ISO!), "
                "end (ISO), all_day (bool), location (str), description (str), "
                "tags (list). Output: {ok, event}.",
)
async def cap_event_upsert(
    id: str = "", title: str = "", start: str = "", end: str = "",
    all_day: bool = False, location: str = "", description: str = "",
    tags: Optional[List[str]] = None, color: str = "", trace_id=None,
):
    if not title:
        return {"error": "title is required"}
    if not start:
        return {"error": "start is required"}
    ev = {
        "id": id or str(uuid.uuid4()),
        "title": title, "start": start, "end": end,
        "all_day": bool(all_day), "location": location,
        "description": description, "source": "local",
        "color": color or SOURCE_COLORS["local"],
        "tags": tags or [],
    }
    ev = await _store_event(ev)
    await emit_event({"type": "cal.event", "stage": "upsert",
                      "message": f"saved event {title!r}", "id": ev["id"]})
    cfg = await _get_config()
    if cfg.get("fabric_auto_persist"):
        await _persist_records_to_fabric([_event_to_fabric(ev)], cfg)
    return {"ok": True, "event": ev}


@capability(
    "cal.event.delete", http_method="POST", http_path="/cal/event/delete",
    http_tags=["calendar"], memory="on",
    description="Delete a diary event by id. Input: id (str!). Output: {ok}.",
)
async def cap_event_delete(id: str = "", trace_id=None):
    if not id:
        return {"error": "id is required"}
    ok = await _delete_event(id)
    await emit_event({"type": "cal.event", "stage": "delete", "id": id})
    return {"ok": ok}


# ═════════════════════════════════════════════════════════════════════════════
#  TODOS
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "cal.todos.list", http_method="GET", http_path="/cal/todos",
    http_tags=["calendar"], memory="off", silent=True,
    description="List todo items. Input: include_done (bool, default True). "
                "Output: {todos:[{id,title,due,priority,done,notes}]}.",
)
async def cap_todos_list(include_done: bool = True, trace_id=None):
    todos = await _hash_list(KEY_TODOS)
    if not include_done:
        todos = [t for t in todos if not t.get("done")]
    todos.sort(key=lambda t: (t.get("done", False), t.get("due") or "9999",
                              -int(t.get("priority", 0) or 0)))
    return {"todos": todos, "count": len(todos)}


@capability(
    "cal.todo.upsert", http_method="POST", http_path="/cal/todo/upsert",
    http_tags=["calendar"], memory="on",
    description="Create or update a todo. Input: id (str — omit to create), "
                "title (str!), due (ISO date, optional), priority (int 0-3), "
                "notes (str), done (bool). Output: {ok, todo}.",
)
async def cap_todo_upsert(
    id: str = "", title: str = "", due: str = "", priority: int = 0,
    notes: str = "", done: bool = False, trace_id=None,
):
    if not title:
        return {"error": "title is required"}
    rec = {"id": id or str(uuid.uuid4()), "title": title, "due": due,
           "priority": int(priority or 0), "notes": notes, "done": bool(done)}
    rec = await _hash_upsert(KEY_TODOS, rec)
    await emit_event({"type": "cal.todo", "stage": "upsert", "id": rec["id"]})
    return {"ok": True, "todo": rec}


@capability(
    "cal.todo.toggle", http_method="POST", http_path="/cal/todo/toggle",
    http_tags=["calendar"], memory="on",
    description="Toggle a todo's done state. Input: id (str!). Output: {ok, done}.",
)
async def cap_todo_toggle(id: str = "", trace_id=None):
    if not id:
        return {"error": "id is required"}
    r = _redis()
    if not r:
        return {"error": "store unavailable"}
    raw = await r.hget(KEY_TODOS, id)
    if not raw:
        return {"error": "todo not found"}
    rec = json.loads(raw)
    rec["done"] = not rec.get("done")
    await _hash_upsert(KEY_TODOS, rec)
    return {"ok": True, "done": rec["done"]}


@capability(
    "cal.todo.delete", http_method="POST", http_path="/cal/todo/delete",
    http_tags=["calendar"], memory="on",
    description="Delete a todo by id. Input: id (str!). Output: {ok}.",
)
async def cap_todo_delete(id: str = "", trace_id=None):
    if not id:
        return {"error": "id is required"}
    return {"ok": await _hash_delete(KEY_TODOS, id)}


# ═════════════════════════════════════════════════════════════════════════════
#  NOTES
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "cal.notes.list", http_method="GET", http_path="/cal/notes",
    http_tags=["calendar"], memory="off", silent=True,
    description="List notes / reminders. Output: {notes:[{id,title,body,remind_at}]}.",
)
async def cap_notes_list(trace_id=None):
    notes = await _hash_list(KEY_NOTES)
    notes.sort(key=lambda n: n.get("created", ""), reverse=True)
    return {"notes": notes, "count": len(notes)}


@capability(
    "cal.note.upsert", http_method="POST", http_path="/cal/note/upsert",
    http_tags=["calendar"], memory="on",
    description="Create or update a note/reminder. Input: id (str — omit to "
                "create), title (str), body (str!), remind_at (ISO, optional). "
                "Output: {ok, note}.",
)
async def cap_note_upsert(id: str = "", title: str = "", body: str = "",
                          remind_at: str = "", trace_id=None):
    if not body and not title:
        return {"error": "title or body is required"}
    rec = {"id": id or str(uuid.uuid4()), "title": title, "body": body,
           "remind_at": remind_at}
    rec = await _hash_upsert(KEY_NOTES, rec)
    return {"ok": True, "note": rec}


@capability(
    "cal.note.delete", http_method="POST", http_path="/cal/note/delete",
    http_tags=["calendar"], memory="on",
    description="Delete a note by id. Input: id (str!). Output: {ok}.",
)
async def cap_note_delete(id: str = "", trace_id=None):
    if not id:
        return {"error": "id is required"}
    return {"ok": await _hash_delete(KEY_NOTES, id)}


# ═════════════════════════════════════════════════════════════════════════════
#  BRAIN DUMP  (the assistant)
# ═════════════════════════════════════════════════════════════════════════════

_BRAINDUMP_SYSTEM = (
    "You are Vera's scheduling assistant. The user will paste an unstructured "
    "brain dump. Turn it into a coherent, organised set of resources. "
    "Return ONLY valid minified JSON, no prose, no code fences, with EXACTLY "
    "these keys:\n"
    '{"events":[{"title","start","end","all_day","location","description"}],'
    '"todos":[{"title","due","priority","notes","for"}],'
    '"notes":[{"title","body","remind_at"}],'
    '"daily_plan":[{"time","item"}]}\n'
    "Rules:\n"
    "- EVERY meeting, appointment, call, class, booking, viewing, interview or "
    "anything that HAPPENS AT a time goes in `events` — even when you also add a "
    "prep or follow-up todo for it. NEVER represent a meeting as only a todo: the "
    "meeting itself is always an event so it lands on the calendar.\n"
    "- Use ISO 8601 for start/end (e.g. 2026-06-16T15:00:00). Resolve relative "
    "dates ('next Tuesday', 'tomorrow 1pm', 'Fri') against NOW given below. If a "
    "day is known but no clock time, set all_day=true and start to the date "
    "(YYYY-MM-DD). If you genuinely cannot tell the day, still emit the event "
    "with start=\"\" so the user can set the time — do not drop it.\n"
    "- Preparation, follow-ups and other actions WITHOUT their own fixed time go "
    "in `todos`. When a todo supports an event, set its `for` to that event's "
    "exact title (else \"\"). due is an ISO date if a deadline is implied, else "
    "\"\". priority is 0-3 (3=urgent).\n"
    "- Non-actionable thoughts/reminders go in notes.\n"
    "- daily_plan is a short suggested ordering for today's items (time as "
    "'HH:MM' or '' for flexible). Keep it realistic and brief.\n"
    "- Never invent commitments the user did not mention. Empty arrays are fine."
)


def _strip_json(text: str) -> str:
    text = (text or "").strip()
    # Thinking models (e.g. Qwen3) emit a <think>…</think> preamble before the
    # answer — strip it (and any unclosed leading think block) before parsing.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    if "<think>" in text.lower():
        text = re.split(r"</?think>", text, flags=re.IGNORECASE)[-1]
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    # Prefer a brace-balanced object so trailing prose can't break parsing.
    start = text.find("{")
    if start != -1:
        depth = 0
        in_str = False
        esc = False
        for k in range(start, len(text)):
            ch = text[k]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:k + 1]
    # Fallback: outermost braces.
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j != -1 and j > i:
        return text[i:j + 1]
    return text


async def _run_braindump(text: str, tz: str, model: str) -> Dict[str, Any]:
    now = _now()
    # `/no_think` disables the <think> preamble on Qwen3-style models so the
    # response is JSON straight away; harmless on models that ignore it.
    system = (f"{_BRAINDUMP_SYSTEM}\n\nNOW: {now.isoformat()}Z "
              f"({now.strftime('%A %d %B %Y %H:%M')} UTC). "
              f"User timezone: {tz or 'UTC'}. /no_think")

    async def _attempt(extra: str = "") -> str:
        return await ollama_generate(
            (text + extra), system=system, json_mode=True, model=model or None,
            prefer_gpu=True,
            caller_override={"caller_file": "calendar_capabilities.py",
                             "caller_func": "cal_braindump",
                             "cap_name": "cal.braindump"},
        )

    raw = await _attempt()
    data = None
    try:
        data = json.loads(_strip_json(raw))
    except Exception as e1:
        # One retry with a blunt reminder — transient models sometimes emit a
        # stray token or an empty first response.
        log.warning("braindump: first parse failed (%s), retrying", e1)
        try:
            raw2 = await _attempt(
                "\n\nReturn ONLY the JSON object described — no prose, no "
                "code fences, no <think> block.")
            data = json.loads(_strip_json(raw2))
            raw = raw2
        except Exception as e2:
            log.warning("braindump: model returned non-JSON: %s", e2)
            return {"events": [], "todos": [], "notes": [], "daily_plan": [],
                    "_parse_error": str(e2), "_raw": (raw or raw2 or "")[:2000]}
    # Normalise shape defensively.
    if not isinstance(data, dict):
        return {"events": [], "todos": [], "notes": [], "daily_plan": [],
                "_parse_error": "model did not return a JSON object",
                "_raw": raw[:2000]}
    for k in ("events", "todos", "notes", "daily_plan"):
        if not isinstance(data.get(k), list):
            data[k] = []
    return data


@capability(
    "cal.braindump", http_method="POST", http_path="/cal/braindump",
    http_tags=["calendar"], memory="on",
    description="Turn a free-text brain dump into an organised proposal of "
                "events, todos, notes and a suggested daily plan using the "
                "local Ollama cluster. Input: text (str!), apply (bool, default "
                "False — when True, immediately persist everything), tz (str). "
                "Output: {proposal:{events,todos,notes,daily_plan}, applied}. "
                "With apply=False the proposal is returned for review only.",
)
async def cap_braindump(text: str = "", apply: bool = False,
                        tz: str = "", trace_id=None):
    if not text.strip():
        return {"error": "text is required"}
    cfg = await _get_config()
    await emit_event({"type": "cal.braindump.progress", "stage": "start",
                      "message": "organising your brain dump…"})
    proposal = await _run_braindump(text, tz or cfg.get("tz", "UTC"),
                                    cfg.get("model", ""))
    await emit_event({"type": "cal.braindump.progress", "stage": "parsed",
                      "message": (f"found {len(proposal['events'])} events, "
                                  f"{len(proposal['todos'])} todos, "
                                  f"{len(proposal['notes'])} notes")})
    applied = None
    if apply:
        applied = await _commit_proposal(proposal, cfg)
    await emit_event({"type": "cal.braindump.progress", "stage": "done",
                      "message": "done"})
    return {"proposal": proposal, "applied": applied}


@capability(
    "cal.braindump.commit", http_method="POST", http_path="/cal/braindump/commit",
    http_tags=["calendar"], memory="on",
    description="Persist a reviewed/edited brain-dump proposal. "
                "Input: proposal (object with events/todos/notes arrays). "
                "Output: {ok, applied:{events,todos,notes,unscheduled}} — "
                "`unscheduled` lists titles of events that had no time set and "
                "were NOT added (the UI prompts the user to give them a time).",
)
async def cap_braindump_commit(proposal: Optional[Dict] = None, trace_id=None):
    if not proposal or not isinstance(proposal, dict):
        return {"error": "proposal object is required"}
    cfg = await _get_config()
    applied = await _commit_proposal(proposal, cfg)
    return {"ok": True, "applied": applied}


async def _commit_proposal(proposal: Dict, cfg: Dict) -> Dict[str, Any]:
    ev_ids, td_ids, nt_ids, unscheduled = [], [], [], []
    fabric_recs = []
    for ev in proposal.get("events", []) or []:
        if not ev.get("title"):
            continue
        # Never silently drop a meeting: an event with no resolvable time is
        # reported back so the UI can ask the user to set one, not discarded.
        if not ev.get("start"):
            unscheduled.append(ev.get("title"))
            continue
        rec = {
            "id": ev.get("id") or str(uuid.uuid4()),
            "title": ev["title"], "start": ev["start"], "end": ev.get("end", ""),
            "all_day": bool(ev.get("all_day")), "location": ev.get("location", ""),
            "description": ev.get("description", ""), "source": "local",
            "color": SOURCE_COLORS["local"], "tags": ["braindump"],
        }
        rec = await _store_event(rec)
        ev_ids.append(rec["id"])
        fabric_recs.append(_event_to_fabric(rec))
    for td in proposal.get("todos", []) or []:
        if not td.get("title"):
            continue
        rec = await _hash_upsert(KEY_TODOS, {
            "id": td.get("id") or str(uuid.uuid4()), "title": td["title"],
            "due": td.get("due", ""), "priority": int(td.get("priority", 0) or 0),
            "notes": td.get("notes", ""), "done": False, "tags": ["braindump"],
            "for": td.get("for", "")})        # event this todo prepares, if any
        td_ids.append(rec["id"])
    for nt in proposal.get("notes", []) or []:
        body = nt.get("body") or nt.get("title")
        if not body:
            continue
        rec = await _hash_upsert(KEY_NOTES, {
            "id": nt.get("id") or str(uuid.uuid4()), "title": nt.get("title", ""),
            "body": nt.get("body", ""), "remind_at": nt.get("remind_at", ""),
            "tags": ["braindump"]})
        nt_ids.append(rec["id"])
    if cfg.get("fabric_auto_persist") and fabric_recs:
        await _persist_records_to_fabric(fabric_recs, cfg)
    return {"events": ev_ids, "todos": td_ids, "notes": nt_ids,
            "unscheduled": unscheduled}


# ═════════════════════════════════════════════════════════════════════════════
#  CLOUD SOURCES  (secrets sealed; never returned to UI)
# ═════════════════════════════════════════════════════════════════════════════

def _redact_source(src: Dict) -> Dict:
    """Return a UI-safe copy: secret fields stripped, replaced with has_* flags."""
    out = {}
    for k, v in src.items():
        if k in _SECRET_FIELDS:
            out[f"has_{k}"] = bool(v)
        else:
            out[k] = v
    return out


async def _get_source(sid: str) -> Optional[Dict]:
    r = _redis()
    if not r:
        return None
    raw = await r.hget(KEY_SOURCES, sid)
    return json.loads(raw) if raw else None


async def _save_source(src: Dict):
    r = _redis()
    if r:
        await r.hset(KEY_SOURCES, src["id"], json.dumps(src))


@capability(
    "cal.sources.list", http_method="GET", http_path="/cal/sources",
    http_tags=["calendar"], memory="off", silent=True,
    description="List configured cloud calendar sources (REDACTED — secrets "
                "are never returned, only has_* flags). "
                "Output: {sources:[{id,type,label,enabled,calendar_id,...}]}.",
)
async def cap_sources_list(trace_id=None):
    srcs = await _hash_list(KEY_SOURCES)
    return {"sources": [_redact_source(s) for s in srcs], "count": len(srcs)}


@capability(
    "cal.source.upsert", http_method="POST", http_path="/cal/source/upsert",
    http_tags=["calendar"], memory="on",
    description="Create or update a cloud calendar source. Secrets are "
                "Fernet-sealed before storage; leave a secret field blank on "
                "edit to keep the existing value. "
                "Input: id (str — omit to create), type (ics|caldav|google), "
                "label (str), enabled (bool), url (str — ics/caldav), "
                "username (str — caldav), password (str — caldav app-password), "
                "client_id/client_secret/refresh_token (str — google), "
                "calendar_id (str — legacy single calendar), "
                "calendars (list of {id,summary,color} — google multi-select; "
                "each becomes a colour-coded layer), "
                "account_id (str — link a shared Accounts-tab account: CalDAV/ICS "
                "credentials, or for type=google the account's OAuth grant "
                "(Calendar scope) instead of embedding client_id/secret/refresh "
                "token on the source). Output: {ok, source (redacted)}.",
    schema={"properties": {"type": {"enum": ["ics", "caldav", "google"]}}},
)
async def cap_source_upsert(
    id: str = "", type: str = "", label: str = "", enabled: bool = True,
    url: str = "", username: str = "", password: str = "",
    client_id: str = "", client_secret: str = "", refresh_token: str = "",
    calendar_id: str = "", color: str = "", account_id: str = "",
    calendars: Optional[List[Dict[str, Any]]] = None, trace_id=None,
):
    if not id and type not in ("ics", "caldav", "google"):
        return {"error": "type must be one of ics|caldav|google"}
    existing = await _get_source(id) if id else None
    src = dict(existing) if existing else {"id": str(uuid.uuid4()),
                                           "created": now_iso()}
    if type:
        src["type"] = type
    src["label"] = label or src.get("label", src.get("type", "source"))
    src["enabled"] = bool(enabled)
    # Optional link to a shared account (Accounts tab) for credentials. Empty
    # string clears it (back to embedded creds); None-like absence keeps it.
    if account_id != "":
        src["account_id"] = account_id
    src["calendar_id"] = calendar_id or src.get("calendar_id", "primary")
    src["color"] = color or src.get("color", SOURCE_COLORS.get(src.get("type"), "#888"))

    # Google multi-select: each chosen calendar is a colour-coded layer.
    if calendars is not None:
        norm = []
        for c in calendars:
            cid = (c.get("id") or "").strip()
            if not cid:
                continue
            norm.append({
                "id": cid,
                "summary": c.get("summary") or cid,
                "color": c.get("color") or SOURCE_COLORS.get(src.get("type"), "#888"),
            })
        src["calendars"] = norm
        # Keep calendar_id pointing at the first layer for legacy callers.
        if norm:
            src["calendar_id"] = norm[0]["id"]
    if url:
        src["url"] = url
    if username:
        src["username"] = username
    if client_id:
        src["client_id"] = client_id

    # Seal incoming secrets; blank means "keep existing".
    try:
        if password:
            src["password"] = cal_secrets.seal(password)
        if client_secret:
            src["client_secret"] = cal_secrets.seal(client_secret)
        if refresh_token:
            src["refresh_token"] = cal_secrets.seal(refresh_token)
    except RuntimeError as e:
        return {"error": str(e)}

    src["updated"] = now_iso()
    await _save_source(src)
    return {"ok": True, "source": _redact_source(src)}


@capability(
    "cal.source.delete", http_method="POST", http_path="/cal/source/delete",
    http_tags=["calendar"], memory="on",
    description="Delete a cloud source and its synced events. Input: id (str!). "
                "Output: {ok, removed_events}.",
)
async def cap_source_delete(id: str = "", trace_id=None):
    if not id:
        return {"error": "id is required"}
    # Remove synced events belonging to this source.
    removed = 0
    for ev in await _hash_list(KEY_EVENTS):
        if ev.get("source") == id:
            await _delete_event(ev["id"])
            removed += 1
    ok = await _hash_delete(KEY_SOURCES, id)
    return {"ok": ok, "removed_events": removed}


# ═════════════════════════════════════════════════════════════════════════════
#  SYNC
# ═════════════════════════════════════════════════════════════════════════════

def _accounts():
    """Resolve the shared accounts module lazily (loaded earlier at startup)."""
    import sys
    return sys.modules.get("accounts_capabilities")


async def _resolve_source_creds(src: Dict) -> Dict[str, str]:
    """Effective creds for a source. If it links a shared account (account_id),
    overlay the account's CalDAV/ICS credentials; else use the source's own
    embedded (sealed) fields. Returns plaintext values for immediate use."""
    out = {
        "url": src.get("url", ""),
        "username": src.get("username", ""),
        "password": cal_secrets.open_secret(src.get("password", "")),
        "ics_url": src.get("url", ""),
    }
    aid = src.get("account_id", "")
    if aid:
        am = _accounts()
        if am:
            acct = await am.get_account(aid)          # secrets already opened
            if acct:
                if src.get("type") == "caldav":
                    out["url"] = acct.get("caldav_url") or out["url"]
                    out["username"] = acct.get("caldav_username") or out["username"]
                    if acct.get("caldav_password"):
                        out["password"] = acct["caldav_password"]
                elif src.get("type") == "ics":
                    out["ics_url"] = acct.get("ics_url") or out["ics_url"]
                    out["url"] = acct.get("ics_url") or out["url"]
    return out


async def _sync_ics(src: Dict) -> Dict[str, int]:
    creds = await _resolve_source_creds(src)
    url = creds["ics_url"] or creds["url"]
    if not url:
        return {"added": 0, "updated": 0, "errors": 1, "error": "no url"}
    added = updated = 0
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        resp = await c.get(url)
        resp.raise_for_status()
        for vev in ical.parse(resp.text):
            res = await _upsert_synced_event(src["id"], src.get("color", "#888"), vev)
            added += res == "added"
            updated += res == "updated"
    return {"added": added, "updated": updated, "errors": 0}


_CALDAV_REPORT_BODY = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
    '<D:prop><D:getetag/><C:calendar-data/></D:prop>'
    '<C:filter><C:comp-filter name="VCALENDAR">'
    '<C:comp-filter name="VEVENT"/></C:comp-filter></C:filter>'
    '</C:calendar-query>'
)


def _xml_unescape(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&#13;", "")
             .replace("&amp;", "&"))


async def _sync_caldav(src: Dict) -> Dict[str, int]:
    creds = await _resolve_source_creds(src)
    url = creds["url"]
    user = creds["username"]
    pw = creds["password"]
    if not url:
        return {"added": 0, "updated": 0, "errors": 1, "error": "no url"}
    added = updated = 0
    auth = (user, pw) if user else None
    async with httpx.AsyncClient(timeout=45, follow_redirects=True, auth=auth) as c:
        resp = await c.request(
            "REPORT", url,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            content=_CALDAV_REPORT_BODY.encode("utf-8"),
        )
        resp.raise_for_status()
        # Extract each <calendar-data> payload (namespace-agnostic).
        blocks = re.findall(r"<[^>]*calendar-data[^>]*>(.*?)</[^>]*calendar-data>",
                            resp.text, re.DOTALL | re.IGNORECASE)
        for block in blocks:
            ics_text = _xml_unescape(block.strip())
            for vev in ical.parse(ics_text):
                res = await _upsert_synced_event(src["id"], src.get("color", "#888"), vev)
                added += res == "added"
                updated += res == "updated"
    return {"added": added, "updated": updated, "errors": 0}


async def _google_access_token(src: Dict) -> str:
    """Refresh and return a Google access token.

    Prefers a linked Accounts-tab OAuth grant (the unified accounts OAuth) when
    the source carries an ``account_id``; falls back to the source's own embedded
    client/refresh token (legacy per-source Google credentials).
    """
    aid = src.get("account_id", "")
    if aid:
        am = _accounts()
        if am and hasattr(am, "get_oauth_token"):
            # Either calendar scope (read-only sync works under the broader
            # manage grant too).
            tok = (await am.get_oauth_token(aid, "calendar")
                   or await am.get_oauth_token(aid, "calendar_manage"))
            if tok:
                return tok
            # Linked account but no usable calendar grant, and no embedded
            # creds to fall back on → surface a clear, actionable error.
            if not src.get("refresh_token"):
                raise RuntimeError(
                    "linked account is not connected for Calendar — open the "
                    "Accounts tab and grant the Calendar scope")
    client_id = src.get("client_id", "")
    client_secret = cal_secrets.open_secret(src.get("client_secret", ""))
    refresh_token = cal_secrets.open_secret(src.get("refresh_token", ""))
    if not (client_id and client_secret and refresh_token):
        raise RuntimeError("google source not fully authorised")
    async with httpx.AsyncClient(timeout=30) as c:
        resp = await c.post(GOOGLE_TOKEN, data={
            "client_id": client_id, "client_secret": client_secret,
            "refresh_token": refresh_token, "grant_type": "refresh_token"})
        resp.raise_for_status()
        return resp.json().get("access_token", "")


def _google_calendars(src: Dict) -> List[Dict]:
    """The list of Google calendars (layers) to pull for a source.

    Prefers the multi-select ``calendars`` list; falls back to the legacy
    single ``calendar_id`` so older sources keep working.
    """
    cals = src.get("calendars") or []
    if cals:
        return cals
    return [{"id": src.get("calendar_id") or "primary",
             "summary": src.get("label") or "primary",
             "color": src.get("color", SOURCE_COLORS["google"])}]


async def _sync_google(src: Dict) -> Dict[str, int]:
    token = await _google_access_token(src)
    if not token:
        return {"added": 0, "updated": 0, "errors": 1, "error": "no access token"}
    cals = _google_calendars(src)
    now = _now()
    params = {
        "timeMin": (now - _dt.timedelta(days=30)).isoformat() + "Z",
        "timeMax": (now + _dt.timedelta(days=120)).isoformat() + "Z",
        "singleEvents": "true", "orderBy": "startTime", "maxResults": "250",
    }
    added = updated = errors = 0
    async with httpx.AsyncClient(timeout=45) as c:
        for cal in cals:
            cal_id = cal.get("id") or "primary"
            cal_color = cal.get("color") or src.get("color", SOURCE_COLORS["google"])
            cal_label = cal.get("summary") or cal_id
            try:
                resp = await c.get(
                    f"{GOOGLE_API}/calendars/{quote(cal_id, safe='')}/events"
                    f"?{urlencode(params)}",
                    headers={"Authorization": f"Bearer {token}"})
                resp.raise_for_status()
            except Exception as ex:
                log.warning("google calendar %s sync failed: %s", cal_id, ex)
                errors += 1
                continue
            for it in resp.json().get("items", []):
                s = it.get("start", {})
                e = it.get("end", {})
                all_day = "date" in s
                vev = {
                    "uid": it.get("id", str(uuid.uuid4())),
                    "title": it.get("summary", "(no title)"),
                    "start": s.get("dateTime") or s.get("date", ""),
                    "end": e.get("dateTime") or e.get("date", ""),
                    "all_day": all_day,
                    "location": it.get("location", ""),
                    "description": it.get("description", ""),
                }
                res = await _upsert_synced_event(
                    src["id"], cal_color, vev,
                    extra={"calendar": cal_id, "calendar_label": cal_label})
                added += res == "added"
                updated += res == "updated"

    # Prune events from calendars that are no longer selected.
    keep = {(cal.get("id") or "primary") for cal in cals}
    for ev in await _hash_list(KEY_EVENTS):
        if (ev.get("source") == src["id"] and ev.get("calendar")
                and ev["calendar"] not in keep):
            await _delete_event(ev["id"])

    return {"added": added, "updated": updated, "errors": errors}


async def _sync_one(src: Dict) -> Dict[str, Any]:
    t = src.get("type")
    try:
        if t == "ics":
            res = await _sync_ics(src)
        elif t == "caldav":
            res = await _sync_caldav(src)
        elif t == "google":
            res = await _sync_google(src)
        else:
            res = {"added": 0, "updated": 0, "errors": 1, "error": f"unknown type {t}"}
    except Exception as e:
        log.warning("cal sync %s failed: %s", src.get("id"), e)
        res = {"added": 0, "updated": 0, "errors": 1, "error": str(e)}
    res["source"] = src.get("id")
    res["label"] = src.get("label")
    res["ts"] = now_iso()
    # Stamp last_sync on the source record.
    src["last_sync"] = res["ts"]
    src["last_result"] = {k: res[k] for k in ("added", "updated", "errors")}
    await _save_source(src)
    return res


@capability(
    "cal.sync.run", http_method="POST", http_path="/cal/sync/run",
    http_tags=["calendar"], memory="on",
    description="Pull events from cloud sources into the local diary. "
                "Input: source (source id — omit to sync all enabled sources). "
                "Output: {ok, results:[{source,added,updated,errors}]}.",
)
async def cap_sync_run(source: str = "", trace_id=None):
    srcs = await _hash_list(KEY_SOURCES)
    if source:
        srcs = [s for s in srcs if s.get("id") == source]
        if not srcs:
            return {"error": "source not found"}
    else:
        srcs = [s for s in srcs if s.get("enabled")]
    await emit_event({"type": "cal.sync.progress", "stage": "start",
                      "message": f"syncing {len(srcs)} source(s)…"})
    results = []
    for s in srcs:
        await emit_event({"type": "cal.sync.progress", "stage": "source",
                          "message": f"syncing {s.get('label')}…", "source": s.get("id")})
        results.append(await _sync_one(s))
    status = {"ts": now_iso(), "results": results}
    r = _redis()
    if r:
        await r.set(KEY_SYNC_STATUS, json.dumps(status))
    await emit_event({"type": "cal.sync.progress", "stage": "done",
                      "message": "sync complete",
                      "total_added": sum(x["added"] for x in results),
                      "total_updated": sum(x["updated"] for x in results)})
    return {"ok": True, "results": results}


@capability(
    "cal.sync.status", http_method="GET", http_path="/cal/sync/status",
    http_tags=["calendar"], memory="off", silent=True,
    description="Last sync run status. Output: {ts, results:[...]}.",
)
async def cap_sync_status(trace_id=None):
    r = _redis()
    if not r:
        return {"results": []}
    raw = await r.get(KEY_SYNC_STATUS)
    return json.loads(raw) if raw else {"results": []}


# ─── Google OAuth (loopback-redirect flow) ───────────────────────────────────

async def _store_oauth_state(state: str, source_id: str) -> None:
    """Bind a one-shot OAuth `state` token to the source being authorised."""
    r = _redis()
    if r:
        await r.set(KEY_OAUTH_STATE + state, source_id, ex=_OAUTH_STATE_TTL)


async def _consume_oauth_state(state: str) -> Optional[str]:
    """Return the source-id bound to `state` and invalidate it (single use)."""
    r = _redis()
    if not r or not state:
        return None
    key = KEY_OAUTH_STATE + state
    raw = await r.get(key)
    if raw is None:
        return None
    await r.delete(key)
    return raw.decode() if isinstance(raw, (bytes, bytearray)) else raw


async def _google_exchange_code(source_id: str, code: str) -> Dict[str, Any]:
    """Exchange an authorisation `code` for a refresh token and seal it onto the
    source. Shared by the auto loopback callback and the manual paste fallback.
    The redirect_uri MUST match the one used to build the consent URL."""
    src = await _get_source(source_id)
    if not src or src.get("type") != "google":
        return {"error": "google source not found"}
    if not code:
        return {"error": "code is required"}
    client_secret = cal_secrets.open_secret(src.get("client_secret", ""))
    if not (src.get("client_id") and client_secret):
        return {"error": "source missing client_id/client_secret"}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(GOOGLE_TOKEN, data={
                "client_id": src["client_id"], "client_secret": client_secret,
                "code": code.strip(), "grant_type": "authorization_code",
                "redirect_uri": _google_redirect_uri()})
            resp.raise_for_status()
            tok = resp.json()
    except Exception as e:
        return {"error": f"token exchange failed: {e}"}
    rt = tok.get("refresh_token")
    if not rt:
        return {"error": "no refresh_token returned (revoke prior grant and retry)"}
    src["refresh_token"] = cal_secrets.seal(rt)
    src["updated"] = now_iso()
    await _save_source(src)
    return {"ok": True}


def _oauth_result_page(ok: bool, msg: str) -> str:
    """Minimal self-closing HTML page shown in the browser tab Google redirects
    to once the loopback callback has run."""
    accent = "#28c28a" if ok else "#ef5b5b"
    title  = "Authorised ✓" if ok else "Authorisation failed"
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Vera · Google Calendar</title></head>"
        "<body style='background:#0d0f12;color:#e8eaed;margin:0;min-height:100vh;"
        "display:flex;align-items:center;justify-content:center;font-family:"
        "system-ui,-apple-system,Segoe UI,Roboto,sans-serif'>"
        "<div style='text-align:center;max-width:440px;padding:32px'>"
        f"<h2 style='color:{accent};margin:0 0 12px'>{title}</h2>"
        f"<p style='color:#aab;line-height:1.5'>{_html.escape(str(msg))}</p>"
        "<p style='color:#667;font-size:13px;margin-top:24px'>You can close this "
        "tab and return to Vera.</p></div>"
        "<script>setTimeout(function(){window.close()},2500)</script>"
        "</body></html>")


@capability(
    "cal.google.auth_url", http_method="GET", http_path="/cal/google/auth_url",
    http_tags=["calendar"], memory="off", silent=True,
    description="Build the Google OAuth consent URL for a configured google "
                "source (loopback flow). Input: source (source id!). Output: "
                "{url}. The user visits it and approves; Google then redirects "
                "the browser back to /cal/google/oauth_callback which stores the "
                "refresh token automatically — no code-pasting needed.",
)
async def cap_google_auth_url(source: str = "", trace_id=None):
    src = await _get_source(source)
    if not src or src.get("type") != "google":
        return {"error": "google source not found"}
    if not src.get("client_id"):
        return {"error": "source has no client_id configured"}
    state = uuid.uuid4().hex
    await _store_oauth_state(state, src["id"])
    url = GOOGLE_AUTH + "?" + urlencode({
        "client_id": src["client_id"], "redirect_uri": _google_redirect_uri(),
        "response_type": "code", "scope": GOOGLE_SCOPE, "state": state,
        "access_type": "offline", "prompt": "consent"})
    return {"url": url}


@capability(
    "cal.google.auth_complete", http_method="POST",
    http_path="/cal/google/auth_complete", http_tags=["calendar"], memory="on",
    description="Manual fallback for the loopback flow: exchange a Google OAuth "
                "code for a refresh token and store it (sealed) on the source. "
                "Used when the auto callback can't reach Vera (e.g. remote "
                "access) and the user pastes the code by hand. "
                "Input: source (id!), code (str!). Output: {ok}.",
)
async def cap_google_auth_complete(source: str = "", code: str = "", trace_id=None):
    return await _google_exchange_code(source, code)


@APP.get("/cal/google/oauth_callback", include_in_schema=False)
async def _google_oauth_callback(request: Request):
    """Loopback redirect target for the Google OAuth flow. Google sends the
    browser here with ?code=&state= after the user approves; we validate the
    state, exchange the code, and seal the refresh token onto the matching
    source — then show a small self-closing confirmation page."""
    qp = request.query_params
    if qp.get("error"):
        return HTMLResponse(
            _oauth_result_page(False, "Google returned: " + qp.get("error", "")),
            status_code=400)
    source_id = await _consume_oauth_state(qp.get("state", ""))
    if not source_id:
        return HTMLResponse(
            _oauth_result_page(False, "Invalid or expired authorisation state — "
                               "start the Connect flow again from Vera."),
            status_code=400)
    res = await _google_exchange_code(source_id, qp.get("code", ""))
    if res.get("ok"):
        return HTMLResponse(
            _oauth_result_page(True, "Google Calendar is now connected."))
    return HTMLResponse(
        _oauth_result_page(False, res.get("error", "token exchange failed")),
        status_code=400)


@capability(
    "cal.google.calendars", http_method="GET", http_path="/cal/google/calendars",
    http_tags=["calendar"], memory="off", silent=True,
    description="List the Google calendars available to an authorised google "
                "source, for the multi-select layer picker. Input: source "
                "(source id!). Output: {calendars:[{id,summary,primary,color,"
                "selected}]} — `selected` reflects what the source currently pulls.",
)
async def cap_google_calendars(source: str = "", trace_id=None):
    src = await _get_source(source)
    if not src or src.get("type") != "google":
        return {"error": "google source not found"}
    try:
        token = await _google_access_token(src)
    except RuntimeError as e:
        return {"error": str(e)}
    if not token:
        return {"error": "could not obtain a Google access token"}
    # Currently-selected calendar ids (multi-select, with legacy fallback).
    selected = {c.get("id") for c in (src.get("calendars") or []) if c.get("id")}
    if not selected and src.get("calendar_id"):
        selected = {src["calendar_id"]}
    out: List[Dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.get(f"{GOOGLE_API}/users/me/calendarList",
                               headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            items = resp.json().get("items", [])
    except Exception as e:
        return {"error": f"could not list calendars: {e}"}
    for it in items:
        cid = it.get("id", "")
        if not cid:
            continue
        out.append({
            "id": cid,
            "summary": it.get("summaryOverride") or it.get("summary", cid),
            "primary": bool(it.get("primary")),
            "color": it.get("backgroundColor") or SOURCE_COLORS["google"],
            # If nothing is selected yet, default to the primary calendar.
            "selected": (cid in selected) or (not selected and bool(it.get("primary"))),
        })
    return {"calendars": out}


# ═════════════════════════════════════════════════════════════════════════════
#  FABRIC PERSISTENCE
# ═════════════════════════════════════════════════════════════════════════════

def _fabric():
    """Resolve the data_fabric module lazily (loaded earlier at startup)."""
    import sys
    return sys.modules.get("data_fabric")


def _event_to_fabric(ev: Dict) -> Dict:
    text = (f"{ev.get('title','')} — {ev.get('start','')}"
            f"{(' @ ' + ev['location']) if ev.get('location') else ''}. "
            f"{ev.get('description','')}").strip()
    return {"text": text, "kind": "event", **{k: ev.get(k) for k in
            ("id", "title", "start", "end", "all_day", "location", "source")}}


async def _persist_records_to_fabric(records: List[Dict], cfg: Dict) -> Dict:
    fab = _fabric()
    if not fab or not hasattr(fab, "ingest_dataset"):
        return {"persisted": 0, "error": "data_fabric unavailable"}
    ds = cfg.get("fabric_dataset", "diary")
    try:
        await fab.ingest_dataset(ds, records, source="calendar", tags=["diary"])
        return {"persisted": len(records), "dataset": ds}
    except Exception as e:
        log.warning("cal fabric persist failed: %s", e)
        return {"persisted": 0, "error": str(e)}


@capability(
    "cal.fabric.persist", http_method="POST", http_path="/cal/fabric/persist",
    http_tags=["calendar"], memory="on",
    description="Push diary events in a range into the data fabric dataset "
                "(default 'diary'). Input: start (ISO/keyword, default all), "
                "end (ISO/keyword). Output: {ok, persisted, dataset}.",
)
async def cap_fabric_persist(start: str = "all", end: str = "all", trace_id=None):
    cfg = await _get_config()
    s, e = _resolve_range(start, end)
    events = await _events_in_range(s, e)
    recs = [_event_to_fabric(ev) for ev in events]
    if not recs:
        return {"ok": True, "persisted": 0, "note": "no events in range"}
    res = await _persist_records_to_fabric(recs, cfg)
    return {"ok": "error" not in res, **res}


# ═════════════════════════════════════════════════════════════════════════════
#  CONFIG CAPS
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "cal.config.get", http_method="GET", http_path="/cal/config",
    http_tags=["calendar"], memory="off", silent=True,
    description="Get calendar config. Output: {model, sync_interval_min, "
                "fabric_auto_persist, fabric_dataset, tz, default_source}.",
)
async def cap_config_get(trace_id=None):
    return await _get_config()


@capability(
    "cal.config.set", http_method="POST", http_path="/cal/config/set",
    http_tags=["calendar"], memory="on",
    description="Update calendar config (partial). Input: any of model (str), "
                "sync_interval_min (int), fabric_auto_persist (bool), "
                "fabric_dataset (str), tz (str), default_source (str). "
                "Output: {ok, config}.",
)
async def cap_config_set(
    model: Optional[str] = None, sync_interval_min: Optional[int] = None,
    fabric_auto_persist: Optional[bool] = None, fabric_dataset: Optional[str] = None,
    tz: Optional[str] = None, default_source: Optional[str] = None, trace_id=None,
):
    cfg = await _get_config()
    for k, v in (("model", model), ("sync_interval_min", sync_interval_min),
                 ("fabric_auto_persist", fabric_auto_persist),
                 ("fabric_dataset", fabric_dataset), ("tz", tz),
                 ("default_source", default_source)):
        if v is not None:
            cfg[k] = v
    await _save_config(cfg)
    return {"ok": True, "config": cfg}


# ═════════════════════════════════════════════════════════════════════════════
#  SCHEDULING ASSISTANT  (agent integration / "scheduling mode")
# ═════════════════════════════════════════════════════════════════════════════

# The registry agent that fronts the diary (seeded in agents.py DEFAULT_AGENTS).
ASSISTANT_AGENT_NAME = "secretary"

# The capability toolkit the scheduling assistant works with. Single source of
# truth — the panel's embedded agent dock fetches it via cal.assistant.config
# and the server-side handover loop uses it as allowed_caps, so the two can
# never drift apart.
ASSISTANT_TOOLKIT = [
    "cal.assistant.briefing",
    "cal.events.list", "cal.event.upsert", "cal.event.delete",
    "cal.todos.list", "cal.todo.upsert", "cal.todo.toggle", "cal.todo.delete",
    "cal.notes.list", "cal.note.upsert", "cal.note.delete",
    "cal.braindump", "cal.braindump.commit",
    "cal.sync.run", "cal.sync.status",
    "system.timestamp",
]

ASSISTANT_GREETING = ("Diary open. Ask me what's on, tell me what to arrange, "
                      "or dump your day on me and I'll sort it.")


def _slim_event(ev: Dict) -> Dict:
    return {k: ev.get(k) for k in
            ("id", "title", "start", "end", "all_day", "location", "source")}


@capability(
    "cal.assistant.briefing", http_method="GET", http_path="/cal/assistant/briefing",
    http_tags=["calendar"], memory="off", silent=True,
    description="Situational briefing for the scheduling assistant: current "
                "time, today's + tomorrow's events, the upcoming week, open "
                "todos (overdue / due today / by priority) and imminent "
                "reminders — one call for full diary awareness before making "
                "changes. Input: days_ahead (int, default 7). "
                "Output: {now, today_human, tz, today, tomorrow, upcoming, "
                "todos:{overdue,due_today,open,open_count}, reminders}.",
)
async def cap_assistant_briefing(days_ahead: int = 7, trace_id=None):
    cfg = await _get_config()
    now = _now()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days_ahead = max(1, min(31, int(days_ahead or 7)))

    def _ts(d: _dt.datetime) -> float:
        return d.replace(tzinfo=_dt.timezone.utc).timestamp()

    today    = await _events_in_range(_ts(day0), _ts(day0 + _dt.timedelta(days=1)))
    tomorrow = await _events_in_range(_ts(day0 + _dt.timedelta(days=1)),
                                      _ts(day0 + _dt.timedelta(days=2)))
    upcoming = await _events_in_range(_ts(day0 + _dt.timedelta(days=2)),
                                      _ts(day0 + _dt.timedelta(days=days_ahead + 1)))

    todos = await _hash_list(KEY_TODOS)
    open_todos = [t for t in todos if not t.get("done")]
    today_iso = day0.strftime("%Y-%m-%d")
    overdue   = [t for t in open_todos if t.get("due") and t["due"][:10] < today_iso]
    due_today = [t for t in open_todos if (t.get("due") or "")[:10] == today_iso]
    open_todos.sort(key=lambda t: (-int(t.get("priority", 0) or 0),
                                   t.get("due") or "9999"))

    notes = await _hash_list(KEY_NOTES)
    horizon = _ts(day0 + _dt.timedelta(days=2))
    reminders = [n for n in notes
                 if n.get("remind_at") and 0 < _epoch(n["remind_at"]) <= horizon]

    return {
        "now": now.isoformat() + "Z",
        "today_human": now.strftime("%A %d %B %Y %H:%M UTC"),
        "tz": cfg.get("tz", "UTC"),
        "today":    [_slim_event(e) for e in today],
        "tomorrow": [_slim_event(e) for e in tomorrow],
        "upcoming": [_slim_event(e) for e in upcoming[:40]],
        "todos": {
            "overdue":    overdue[:20],
            "due_today":  due_today[:20],
            "open":       open_todos[:40],
            "open_count": len(open_todos),
        },
        "reminders": reminders[:10],
    }


@capability(
    "cal.assistant.config", http_method="GET", http_path="/cal/assistant/config",
    http_tags=["calendar"], memory="off", silent=True,
    description="Config for the Calendar panel's embedded assistant dock: the "
                "agent name, its capability toolkit and greeting. "
                "Output: {agent, toolkit, loop_version, greeting}.",
)
async def cap_assistant_config(trace_id=None):
    return {"agent": ASSISTANT_AGENT_NAME, "toolkit": ASSISTANT_TOOLKIT,
            "loop_version": "v3", "greeting": ASSISTANT_GREETING}


@capability(
    "cal.assistant.handover", http_method="POST", http_path="/cal/assistant/handover",
    http_tags=["calendar", "agents"], memory="on",
    description="Enter 'scheduling mode': hand a diary request over to the "
                "Scheduling Assistant. Runs a scoped agentic loop over the "
                "cal.* capabilities that inspects the calendar, applies the "
                "requested changes (events / todos / notes) and reports back. "
                "Call this from chat for any calendar/todo/diary request that "
                "needs real changes — pass the user's ask verbatim. "
                "Input: request (str!), context (str — optional extra context "
                "from the conversation), max_cycles (int default 8), "
                "session_id (str), require_approval (bool default False). "
                "Output: {ok, summary, cycles, used_tools, session_id}.",
)
async def cap_assistant_handover(request: str = "", context: str = "",
                                 max_cycles: int = 8, session_id: str = "",
                                 require_approval: bool = False, trace_id=None):
    if not (request or "").strip():
        return {"error": "request is required"}
    reg = getattr(_orch, "CAPABILITY_REGISTRY", {}) or {}
    loop_cap = reg.get("dag.agent_loop_v3")
    if not loop_cap:
        return {"error": "agent loop unavailable — dag workshop module not loaded"}

    cfg = await _get_config()
    now = _now()
    sid = session_id or f"sched-{uuid.uuid4().hex[:10]}"
    goal = (
        "[SCHEDULING MODE — you are Vera's scheduling assistant operating the "
        f"user's diary. Now: {now.isoformat()}Z "
        f"({now.strftime('%A %d %B %Y %H:%M')} UTC); user timezone: "
        f"{cfg.get('tz', 'UTC')}. Resolve all relative dates against this. "
        "Ground yourself first (cal.assistant.briefing or cal.events.list / "
        "cal.todos.list), then apply the request with the cal.* capabilities — "
        "meetings/appointments are events (cal.event.upsert), actions without a "
        "fixed time are todos (cal.todo.upsert). Check the day before adding a "
        "timed event so you never double-book silently. Finish with a short "
        "summary of exactly what changed.]"
        + (f"\n\n[Context from Vera: {context.strip()}]" if (context or "").strip() else "")
        + f"\n\nRequest: {request.strip()}"
    )

    await emit_event({"type": "cal.assistant", "stage": "handover_start",
                      "message": f"scheduling mode: {request.strip()[:140]}",
                      "session_id": sid})
    try:
        res = await loop_cap["func"](
            goal=goal,
            allowed_caps=",".join(ASSISTANT_TOOLKIT),
            max_cycles=max(1, min(16, int(max_cycles or 8))),
            require_approval=bool(require_approval),
            handover=True,
            min_explore_cycles=1,
            session_id=sid,
        )
    except Exception as e:
        log.warning("cal.assistant.handover loop failed: %s", e)
        await emit_event({"type": "cal.assistant", "stage": "handover_error",
                          "message": str(e)[:300], "session_id": sid})
        return {"error": f"scheduling assistant failed: {e}", "session_id": sid}

    if not isinstance(res, dict):
        res = {}
    summary = (res.get("handover_output") or res.get("final") or "").strip()
    used: List[str] = []
    for cyc in (res.get("cycles") or []):
        if not isinstance(cyc, dict):
            continue
        act = cyc.get("action")
        t = (cyc.get("tool") or cyc.get("cap")
             or (act.get("tool") if isinstance(act, dict) else ""))
        if t and t not in used:
            used.append(t)
    ok = bool(res.get("done")) and not res.get("error")
    await emit_event({"type": "cal.assistant", "stage": "handover_done", "ok": ok,
                      "message": (summary or "scheduling run finished")[:300],
                      "session_id": sid})
    out = {"ok": ok, "summary": summary or "(no summary produced)",
           "cycles": len(res.get("cycles") or []), "used_tools": used,
           "session_id": sid}
    if res.get("error"):
        out["error"] = res["error"]
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "cal.panel.html", http_method="GET", http_path="/cal/panel",
    http_tags=["calendar", "ui"], memory="off", silent=True,
    description="Serve the Calendar panel HTML.",
)
async def cap_panel_html(trace_id=None):
    try:
        html = _PANEL_HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = ("<!DOCTYPE html><html><body style='background:#0d0f12;"
                "color:#ef5b5b;font-family:monospace;padding:40px'>"
                "<h2>calendar_panel.html not found</h2>"
                f"<p>Expected at: {_PANEL_HTML_PATH}</p></body></html>")
    return HTMLResponse(html)


@APP.get("/cal/panel", include_in_schema=False)
async def _cal_panel_route():
    p = _HERE / "calendar_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>calendar_panel.html not found</p>")


# ═════════════════════════════════════════════════════════════════════════════
#  BACKGROUND SYNC POLLER
# ═════════════════════════════════════════════════════════════════════════════

_last_auto_sync = {"at": 0.0}


async def _sync_tick():
    """Periodic auto-sync of enabled sources, honouring the configured interval."""
    try:
        cfg = await _get_config()
        interval_min = int(cfg.get("sync_interval_min", 15) or 0)
        if interval_min <= 0:
            return
        import time as _t
        if _t.time() - _last_auto_sync["at"] < interval_min * 60:
            return
        srcs = [s for s in await _hash_list(KEY_SOURCES) if s.get("enabled")]
        if not srcs:
            return
        _last_auto_sync["at"] = _t.time()
        results = [await _sync_one(s) for s in srcs]
        r = _redis()
        if r:
            await r.set(KEY_SYNC_STATUS, json.dumps({"ts": now_iso(),
                                                     "results": results,
                                                     "auto": True}))
        log.info("cal auto-sync: %d source(s)", len(results))
    except Exception as e:
        log.warning("cal auto-sync tick failed: %s", e)


# Check every 60s; _sync_tick self-throttles to the configured interval.
schedule(_sync_tick, 60, name="cal_auto_sync")


# ═════════════════════════════════════════════════════════════════════════════
#  REGISTER UI TAB
# ═════════════════════════════════════════════════════════════════════════════

register_ui(
    "calendar-panel",
    "Calendar",
    "🗓",
    """<div id="calendar-panel-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/cal/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=[
        "cal.events.list", "cal.event.upsert", "cal.event.delete",
        "cal.todos.list", "cal.todo.upsert", "cal.todo.toggle", "cal.todo.delete",
        "cal.notes.list", "cal.note.upsert", "cal.note.delete",
        "cal.braindump", "cal.braindump.commit",
        "cal.assistant.briefing", "cal.assistant.config", "cal.assistant.handover",
        "cal.sources.list", "cal.source.upsert", "cal.source.delete",
        "cal.sync.run", "cal.sync.status",
        "cal.google.auth_url", "cal.google.auth_complete", "cal.google.calendars",
        "cal.fabric.persist", "cal.config.get", "cal.config.set",
    ],
    mode="inject",          # now a sub-tab of the combined "Comms" tab
    tab_order=70,
)
