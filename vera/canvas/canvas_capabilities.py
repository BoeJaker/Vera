"""
Live Canvas / Whiteboard — Feature B.

A JSON-backed, live-updating document that chat and agentic loops render into
(the "next-gen notebook"). The JSON is the SOURCE OF TRUTH; the HTML view
(<vera-canvas>, added separately) is derived from it — which is what makes a
canvas live-updatable (append/patch/move a block), scroll-back-able (every block
is timestamped + versioned), rearrangeable, and exportable.

Two modes:
  • DYNAMIC — tracks a live topic (e.g. "latest AI/ML news"); blocks accrete over
    time and the view offers a timeline to scroll back through updates.
  • STATIC  — a working area the user + agents co-edit (brainstorm / notes).

PRIMARY MECHANISM = PREDEFINED BLOCK TYPES (below), so the UIs stay consistent
over time. An agent MAY emit a raw `html` block on the fly, but that's the escape
hatch, not the default.

This module is the FOUNDATION: the doc model + storage + CRUD caps + live events.
The renderer element, the chat/loop OUTPUT MODE wiring, and the deeper
integrations (notebook, panel-bridge widgets, live SSH sessions, scheduling)
build on top of these caps.
"""
from typing import Any, Dict, List, Optional
from pathlib import Path
import asyncio
import hashlib
import inspect
import json
import re
import time
import uuid

from fastapi import Response
from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP,
    CAPABILITY_REGISTRY,
    capability,
    emit_event,
    now_iso,
    register_ui,
)

_PANEL_HTML = Path(__file__).parent / "canvas_panel.html"

def _redis():
    return _orch.REDIS

KEY_CANVAS = "vera:canvas:"            # + <id>  → the canvas doc (JSON)
KEY_CANVAS_INDEX = "vera:canvas:index"  # ZSET id → updated-at epoch (recent first)
_MAX_BLOCKS = 2000                      # hard cap so a runaway loop can't grow forever

# ── Predefined block structures. Agents fill the `content` shape; they do NOT
#    hand-roll HTML (except the `html` escape hatch). Keeping this list the
#    canonical set is what keeps canvases visually consistent across time. ──────
BLOCK_TYPES: Dict[str, Dict[str, str]] = {
    "markdown": {"desc": "Rich text, Markdown-rendered.",
                 "content": "{md:str}"},
    "code":     {"desc": "Source code — syntax-highlighted + linted in the view.",
                 "content": "{code:str, lang:str, filename?:str}"},
    "diagram":  {"desc": "A Mermaid diagram.",
                 "content": "{mermaid:str, caption?:str}"},
    "explode":  {"desc": "A STRUCTURED diagram of code or prose — the Explode contract drawn as bands, "
                         "columns, cards and routed runs. `binds` names a code item in this canvas: its code "
                         "is what gets exploded, and the two are bound by SPAN both ways — click a card and "
                         "that code scrolls and lights; select lines and the covering card lights.",
                 "content": "{binds?:str(item key), path?:str, paths?:[str], depth?:int, record?:str, "
                            "ranges?:[[int,int]], text?:str, code?:str, lang?:str, mode?:str, layers?:[str], "
                            "assess?:bool, title?:str, height?:int}"},
    "image":    {"desc": "An image (URL or data URI) with optional caption.",
                 "content": "{url:str, alt?:str, caption?:str}"},
    "note":     {"desc": "A short user/agent note or annotation.",
                 "content": "{text:str, author?:str}"},
    "table":    {"desc": "Tabular data.",
                 "content": "{columns:[str], rows:[[any]], caption?:str}"},
    "widget":   {"desc": "A defined Vera widget via the panel bridge (widget-level, "
                         "not a whole panel).",
                 "content": "{widget:str, args?:obj, title?:str}"},
    "session":  {"desc": "A live terminal (the Canvas board): <vera-terminal> over the estate's terminal "
                         "WebSocket — an SSH host of the Exec panel (host_id) or a docker container on it "
                         "(host_id + container) — or a session's results.",
                 "content": "{host_id?:str, container?:str, shell?:str, ws?:str, attached?:bool, "
                            "session_id?:str, host?:str, command?:str, output?:str}"},
    "notebook": {"desc": "A notebook cell as an item: the cell the notebook holds (its source, its output); "
                         "Run goes through the notebook's exec, Open opens the notebook at it.",
                 "content": "{notebook_id:str, cell_id:str, cell_type:str, lang?:str, content:str, "
                            "generated?:str, title?:str}"},
    "panel":    {"desc": "A WHOLE panel as an item: the panel page in its frame, driven over the one bridge "
                         "(panel.query · panel.dispatch); the add bar lists the panels open for the session.",
                 "content": "{panel:str, title?:str, src?:str}"},
    "schedule": {"desc": "A scheduled item tied to this canvas, shown nicely.",
                 "content": "{when:str, what:str, action_id?:str}"},
    "html":     {"desc": "Raw HTML — ON-THE-FLY escape hatch; prefer a predefined "
                         "type so the UI stays consistent.",
                 "content": "{html:str}"},
    "calendar": {"desc": "A month, with what is on. Backed by the diary itself (cal.events.list) rather than "
                         "by a copy of it: the item refreshes when the month changes and when an event is "
                         "written, so it is a view of the calendar and not a screenshot of one. `month` is "
                         "YYYY-MM, `selected` a YYYY-MM-DD the day list is showing.",
                 "content": "{title?:str, month?:str, selected?:str, "
                            "events:[{id,title,start,end?,all_day?,location?,color?}]}"},
    "timeline": {"desc": "Dated events in order - what a research run turned up about a subject, laid out on "
                         "a time axis. Events carry a `when` that may be a year, a month or a day, so a "
                         "timeline built from prose does not have to pretend to a precision the prose did "
                         "not have.",
                 "content": "{title?:str, events:[{when:str, label:str, text?:str, url?:str}], subject?:str}"},
    "source":   {"desc": "A page a research run read: where it came from, what it said, and what it "
                         "looked like. Landed as the run finds them - a citation, a crawled page - and "
                         "keyed by url so the same page is never on the canvas twice. `text` and `shot` "
                         "are filled in on demand by the item itself (browser.content / browser.screenshot), "
                         "so a run that reads forty pages does not fetch forty screenshots.",
                 "content": "{url:str, title?:str, domain?:str, snippet?:str, chars?:int, "
                            "text?:str, shot?:str, failed?:bool, query?:str}"},
    "result":   {"desc": "A capability's RESULT (canvas.run): the cap, its arguments, how it went, and the answer - drawn "
                         "as the element that fits it (terminal, table, widget, code, html, image, a record's fields, "
                         "a JSON tree). Keyed by the cap and its arguments, so running it again updates it.",
                 "content": "{cap:str, args:obj, result:any, ok:bool, error?:str, ms?:int, ts?:str}"},
    "app":      {"desc": "An APP from the catalogue (vera/apps/*.json, ui.apps.list / ui.apps.get) as an item: it reads its "
                         "source when opened and again on its manifest's interval while it is seen, and draws the answer the "
                         "way a capability's result is drawn. `args` fill the manifest's {arg.x} templates.",
                 "content": "{app:str(app id), args?:obj, title?:str}"},
    "json":     {"desc": "Structured data as a tree you can open and close.",
                 "content": "{data:any, title?:str}"},
    "kv":       {"desc": "A record: its fields, name beside value.",
                 "content": "{fields:{str:any}|[[str,any]], title?:str}"},
    "chat":     {"desc": "An exchange - who asked and who answered, each message rendered as Markdown.",
                 "content": "{messages:[{role:'user'|'assistant', text:str, name?:str}], title?:str}"},
    "records":  {"desc": "MANY records to look through - web or news results, research sources, the fabric's records, any "
                         "list - as a browser: a filter box, sort (relevance, newest, title), list / cards / table views, "
                         "pages, and each record opening in place (a page read through browser.content, a fabric record "
                         "read in pages through memory.read, a url landed as its own source item). Nothing is cut: the "
                         "browser pages; `next` names the read that fetches more. Landed by canvas.enrich and by the "
                         "chat when an answer is a list of links.",
                 "content": "{title?:str, source?:str(cap), args?:obj, query?:str, kind?:'web'|'news'|'memory'|"
                            "'research'|'rows', items:[{id:str, title:str, url?:str, domain?:str, snippet?:str, "
                            "when?:str, score?:num, meta?:{str:str}, ref?:{record_id, dataset_id}}], total?:int, "
                            "next?:{cap:str, args:obj}, why?:str}"},
    "entities": {"desc": "What a turn NAMES - its people, organisations, places, products, events, tickers, money and "
                         "dates - as chips grouped by kind, each with how often it was named; pressed, a chip shows "
                         "the sentences it was named in and looks it up (the fabric, the web) beside it. Landed by "
                         "turn.understand from the NLP tier's entity model.",
                 "content": "{title?:str, mid?:str, groups:[{group:str, entities:[{text:str, type:str, count:int, "
                            "sentences?:[str]}]}], count?:int}"},
    "loop":     {"desc": "An agentic run as an item: its goal, status and steps (each a "
                         "capability) — written by the chat and by the loop itself (P7).",
                 "content": "{goal:str, status:str, steps:[{n:str, cap?:str, status?:str, ms?:str}], run?:str}"},
    "activity": {"desc": "What a turn is DOING: the capabilities the chat ran for it, each as it runs - running, then "
                         "done or failed, how long it took and a line of what it answered. Written by the chat while "
                         "the turn's calls run (one item per turn, updated in place), so the desk shows work in "
                         "progress rather than only its results afterwards.",
                 "content": "{title?:str, mid?:str, status:'running'|'done'|'error', calls:[{cap:str, args?:str, "
                            "status:'running'|'ok'|'error', started?:num, ms?:int, peek?:str}]}"},
    "research": {"desc": "A research run as one item: how it stands and its step, its method (depth, style, the "
                         "questions it answers, its writing brief), every page it read (a records browser: reader "
                         "mode, the picture, explode), the sources it cited and its report - written as it streams. "
                         "Written by the chat from the run's own card (one item per job, updated in place).",
                 "content": "{title?:str, query?:str, job?:str, status:'running'|'done'|'error', step?:str, "
                            "writing?:bool, started?:num, secs?:int, plan?:{}, items:[{id:str, url:str, domain?:str, "
                            "title?:str, meta?:{}}], cites?:[{url:str, title?:str}], live?:str, report?:str, error?:str}"},
}
CANVAS_MODES = ("dynamic", "static")

# ── The SESSION canvas (Notes/38): one document per chat session, its items KEYED so the same thing is never on
#    the canvas twice. A keyed block carries key (<kind>:<ref>), state (now · parked · pinned · hidden), size
#    (xs · s · m · l · xl) and its anchors (the turns that used it — additive, never replaced). The document carries a
#    revision (bumped on every write) and a timeline ([{rev, ts, op, key}], append-only; bounded so a runaway loop
#    cannot grow it forever). Keyless blocks — the existing canvases — are untouched by all of this. ─────────────
ITEM_STATES = ("now", "parked", "pinned", "hidden")
ITEM_SIZES = ("xs", "s", "m", "l", "xl")   # = item_core.SIZES (WSU-58 S8, Decision 5.1: the canvas gains xs)
_MAX_TIMELINE = 2000
_SESSION_PREFIX = "cv_session_"


def _session_canvas_id(session_id: str) -> str:
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id or "").strip())[:64]
    return _SESSION_PREFIX + sid if sid else ""


def _as_obj(v: Any) -> Any:
    """Arguments arrive as JSON text over MCP as often as as objects."""
    if isinstance(v, str) and v.strip()[:1] in ("{", "["):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _record(doc: Dict[str, Any], op: str, key: str = "", **extra) -> int:
    """Append the timeline entry for the write about to be saved; returns the revision it will carry."""
    nxt = int(doc.get("revision") or 0) + 1
    tl = doc.setdefault("timeline", [])
    ent = {"rev": nxt, "ts": now_iso(), "op": op, "key": key}
    ent.update({k: v for k, v in extra.items() if v not in (None, "")})
    tl.append(ent)
    if len(tl) > _MAX_TIMELINE:
        del tl[: len(tl) - _MAX_TIMELINE]
    return nxt


async def _write(doc: Dict[str, Any], op: str, key: str = "", **extra) -> int:
    """One keyed write: timeline → save (bumps the revision) → canvas.updated {id, revision, op, key}."""
    _record(doc, op, key, **extra)
    await _save(doc)
    await _emit(doc["id"], op, id=doc["id"], revision=doc.get("revision"), key=key)
    return int(doc.get("revision") or 0)


REMOVED_KEEP = 30   # the "recently removed" a canvas keeps (WSU-66)


def _tombstone(doc: Dict[str, Any], blocks: List[Dict[str, Any]]) -> None:
    """Keep what is removed so it can be put back (WSU-66, owner 2026-10-11: "removed canvas element should be
    restorable"): the whole block with its key, newest last, the last REMOVED_KEEP of them. Keyless blocks of an agent's
    canvas are kept by their id."""
    gone = [b for b in blocks if isinstance(b, dict)]
    if not gone:
        return
    rem = [r for r in (doc.get("removed") or []) if isinstance(r, dict)]
    keys = {str(b.get("key") or "") for b in gone if b.get("key")}
    rem = [r for r in rem if not (r.get("key") and str(r.get("key")) in keys)]   # one entry per key: the latest removal
    rem += [{"key": str(b.get("key") or ""), "id": str(b.get("id") or ""), "at": now_iso(), "block": b} for b in gone]
    doc["removed"] = rem[-REMOVED_KEEP:]


def _find_key(doc: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    key = str(key or "")
    if not key:
        return None
    return next((b for b in doc.get("blocks", []) if b.get("key") == key), None)


async def _target(id: str = "", session_id: str = "", create: bool = True) -> Optional[Dict[str, Any]]:
    """The document a keyed call means: an explicit id, else the session's canvas (made on demand)."""
    if id:
        return await _load(id)
    cid = _session_canvas_id(session_id)
    if not cid:
        return None
    doc = await _load(cid)
    if doc or not create:
        return doc
    # NO STORE, NO CANVAS (mirror-diag 2026-10-17): with Redis absent the save is a no-op, and a "created" canvas that was
    # never stored made canvas.session.resolve answer ok/created while /canvas/get said "unknown canvas". Say there is none.
    if not _redis():
        return None
    doc = {"id": cid, "title": "Session canvas", "mode": "session", "topic": "",
           "session": str(session_id), "created": now_iso(), "updated": now_iso(),
           "blocks": [], "revision": 0, "timeline": []}
    await _write(doc, "create")
    return doc


def _item_view(b: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": b.get("id"), "key": b.get("key"), "type": b.get("type"), "state": b.get("state"),
            "size": b.get("size"), "anchor": b.get("anchor"), "anchors": b.get("anchors") or [],
            "ts": b.get("ts"), "score": b.get("score"),
            # the item contract's fields (canvas.item.set; DEEP-29 11A.4) - only those the item has
            **{k: b[k] for k in ("face", "back", "dock", "place", "view", "group", "folded") if k in b}}


def _anchor_turn(anchor: Any) -> str:
    """The turn an anchor names — the timeline records it so the relevance engine knows what THIS turn touched."""
    a = _as_obj(anchor)
    return str(a.get("turn") or a.get("mid") or "") if isinstance(a, dict) else ""


def _add_anchor(b: Dict[str, Any], anchor: Any) -> None:
    """Anchors only grow: the latest is 'anchor', every one it ever had is in 'anchors'."""
    anchor = _as_obj(anchor)
    if not isinstance(anchor, dict) or not anchor:
        return
    b["anchor"] = anchor
    arr = b.setdefault("anchors", [])
    if anchor not in arr:
        arr.append(anchor)


def _now_epoch() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def _load(canvas_id: str) -> Optional[Dict[str, Any]]:
    r = _redis()
    if not r or not canvas_id:
        return None
    try:
        raw = await r.get(KEY_CANVAS + canvas_id)
    except Exception:
        return None
    if not raw:
        return None
    try:
        doc = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        return None
    _remember_base(doc)
    return doc


# ── TWO WRITERS, ONE DOCUMENT (B1; DEEP-29 11A.4 Decision 2). A canvas is saved whole, and every write is read - change -
#    save with awaits between, so two writes at once (two items set from one page, a turn's items landing while the person
#    drags) each saved what they had read and the later lost the earlier's change (found 2026-10-16: a lock and a pill sent
#    together kept only the pill). Each save now knows what it read (the base): when the stored document moved on since,
#    the save is a three-way merge - its own changed, added and removed items over the stored ones, the other writer's left
#    as they are; the timeline and the removed list keep both; a document field it did not change keeps the stored value.
#    The read-merge-write of one canvas is serialised in this process. Same format as before, so nothing that reads a
#    canvas changes; per-item storage (a HASH per canvas) can follow on the same merge rule. ──────────────────────────
_BASES: Dict[int, Any] = {}          # id(doc) -> (doc, base): the document a write loaded and what it was when loaded
_BASES_MAX = 256
_SAVE_LOCKS: Dict[str, asyncio.Lock] = {}


def _ident(b: Dict[str, Any]) -> str:
    return str(b.get("key") or b.get("id") or "")


def _dump(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)


def _snapshot(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {"rev": int(doc.get("revision") or 0),
            "blocks": {_ident(b): _dump(b) for b in doc.get("blocks", []) if isinstance(b, dict)},
            "order": [_ident(b) for b in doc.get("blocks", []) if isinstance(b, dict)],
            "fields": {k: _dump(v) for k, v in doc.items() if k not in ("blocks", "timeline", "removed", "revision", "updated")},
            "removed": {_dump(x) for x in (doc.get("removed") or [])}}


def _remember_base(doc: Dict[str, Any]) -> None:
    if len(_BASES) >= _BASES_MAX:
        for k in list(_BASES)[: _BASES_MAX // 4]:
            _BASES.pop(k, None)
    _BASES[id(doc)] = (doc, _snapshot(doc))


def _merge(cur: Dict[str, Any], mine: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    """Three-way merge of a write (mine, read at base) over the stored document (cur). Pure."""
    mb = [b for b in mine.get("blocks", []) if isinstance(b, dict)]
    mine_map = {_ident(b): b for b in mb}
    bb = base["blocks"]
    changed = {k for k, b in mine_map.items() if bb.get(k) != _dump(b)}
    removed = set(bb) - set(mine_map)
    reordered = [k for k in base["order"] if k in mine_map] != [_ident(b) for b in mb if _ident(b) in bb]
    cur_blocks = [b for b in cur.get("blocks", []) if isinstance(b, dict) and _ident(b) not in removed]
    cur_map = {_ident(b): b for b in cur_blocks}
    if reordered:   # my order wins for the items I knew; the other writer's new ones follow
        out = [mine_map[k] if k in changed or k not in cur_map else cur_map[k] for k in (_ident(b) for b in mb) if k in cur_map or k in changed]
        known = {_ident(b) for b in out}
        out += [b for b in cur_blocks if _ident(b) not in known]
    else:
        out = [mine_map[k] if k in changed else b for k, b in ((_ident(b), b) for b in cur_blocks)]
        known = {_ident(b) for b in out}
        out += [mine_map[k] for k in (_ident(b) for b in mb) if k in changed and k not in known]
    merged = dict(cur)
    for k, v in mine.items():
        if k in ("blocks", "timeline", "removed", "revision", "updated"):
            continue
        if base["fields"].get(k) != _dump(v):
            merged[k] = v
    merged["blocks"] = out
    tl = list(cur.get("timeline") or [])
    tl += [e for e in (mine.get("timeline") or []) if isinstance(e, dict) and int(e.get("rev") or 0) > base["rev"]]
    merged["timeline"] = tl[-_MAX_TIMELINE:]
    rem = list(cur.get("removed") or [])
    rem += [x for x in (mine.get("removed") or []) if _dump(x) not in base["removed"]]
    merged["removed"] = rem[-REMOVED_KEEP:]
    merged["revision"] = int(cur.get("revision") or 0)
    return merged


async def _save(doc: Dict[str, Any]) -> None:
    r = _redis()
    if not r:
        return
    cid = doc["id"]
    lock = _SAVE_LOCKS.setdefault(cid, asyncio.Lock())
    async with lock:
        held = _BASES.get(id(doc))
        base = held[1] if held and held[0] is doc else None
        if base is not None:
            try:
                raw = await r.get(KEY_CANVAS + cid)
                cur = json.loads(raw.decode() if isinstance(raw, bytes) else raw) if raw else None
            except Exception:
                cur = None
            if cur and int(cur.get("revision") or 0) != base["rev"]:
                mine_new = [e for e in (doc.get("timeline") or []) if isinstance(e, dict) and int(e.get("rev") or 0) > base["rev"]]
                merged = _merge(cur, doc, base)
                for e in mine_new:                                 # my entries carry the revision they now land at
                    e["rev"] = int(merged.get("revision") or 0) + 1
                doc.clear()
                doc.update(merged)
        doc["updated"] = now_iso()
        doc["revision"] = int(doc.get("revision") or 0) + 1
        try:
            await r.set(KEY_CANVAS + cid, json.dumps(doc))
            await r.zadd(KEY_CANVAS_INDEX, {cid: _now_epoch()})
        except Exception:
            pass
        _remember_base(doc)


async def _emit(canvas_id: str, op: str, **extra) -> None:
    """Notify live viewers (the <vera-canvas> element subscribes to these)."""
    try:
        await emit_event({"type": "canvas.updated", "canvas_id": canvas_id,
                          "op": op, **extra})
    except Exception:
        pass


def _validate_block(btype: str, content: Any) -> Dict[str, Any]:
    """Coerce a block into a stored shape. Unknown types fall back to `note` so a
    bad emit never breaks the canvas; content is always a dict."""
    if btype not in BLOCK_TYPES:
        btype = "note"
    if isinstance(content, str):
        # A bare string is treated as the natural field for the type.
        key = {"markdown": "md", "code": "code", "diagram": "mermaid",
               "note": "text", "html": "html", "loop": "goal", "notebook": "content",
               "panel": "panel"}.get(btype, "text")
        content = {key: content}
    if not isinstance(content, dict):
        content = {"text": str(content)}
    return {"type": btype, "content": content}


@capability(
    "canvas.create", memory="off",
    http_method="POST", http_path="/canvas/create", http_tags=["canvas"],
    description="Create a live canvas/whiteboard. Inputs: title (str), mode "
                "('dynamic'|'static', default 'static'), topic (str, for dynamic). "
                "Output: {id, title, mode, topic}. Chat/loops append blocks to it.",
)
async def cap_canvas_create(title: str = "", mode: str = "static",
                            topic: str = "", trace_id=None):
    mode = mode if mode in CANVAS_MODES else "static"
    doc = {"id": _new_id("cv"), "title": (title or "Untitled canvas")[:200],
           "mode": mode, "topic": (topic or "")[:300],
           "created": now_iso(), "updated": now_iso(), "blocks": []}
    await _save(doc)
    await _emit(doc["id"], "create", title=doc["title"], mode=mode)
    return {"id": doc["id"], "title": doc["title"], "mode": mode, "topic": doc["topic"]}


@capability(
    "canvas.get", memory="off", silent=True,
    http_method="GET", http_path="/canvas/get", http_tags=["canvas"],
    description="Get a canvas document (its blocks in order). Input: id (str!).",
)
async def cap_canvas_get(id: str = "", trace_id=None):
    doc = await _load(id)
    if not doc:
        return {"error": f"unknown canvas: {id}"}
    return doc


@capability(
    "canvas.list", memory="off", silent=True,
    http_method="GET", http_path="/canvas/list", http_tags=["canvas"],
    description="List canvases (most-recently-updated first). Input: limit (int, 30).",
)
async def cap_canvas_list(limit: int = 30, trace_id=None):
    r = _redis()
    if not r:
        return {"canvases": []}
    try:
        ids = await r.zrevrange(KEY_CANVAS_INDEX, 0, max(0, int(limit) - 1))
    except Exception:
        ids = []
    out = []
    for x in ids or []:
        cid = x.decode() if isinstance(x, bytes) else x
        d = await _load(cid)
        if d:
            out.append({"id": d["id"], "title": d.get("title"), "mode": d.get("mode"),
                        "topic": d.get("topic"), "blocks": len(d.get("blocks") or []),
                        "updated": d.get("updated")})
    return {"canvases": out}


@capability(
    "canvas.append", memory="off",
    http_method="POST", http_path="/canvas/append", http_tags=["canvas"],
    description="Append a block to a canvas (the main live-update path). Inputs: id "
                "(str!), type (one of: markdown|code|diagram|image|note|table|widget|"
                "session|schedule|html), content (JSON matching the type), meta "
                "(JSON, optional). Output: {ok, block_id}. Prefer predefined types "
                "over raw html so the UI stays consistent.",
)
async def cap_canvas_append(id: str = "", type: str = "markdown",
                            content: Any = None, meta: Any = None, key: str = "", size: str = "",
                            trace_id=None):
    doc = await _load(id)
    if not doc:
        return {"error": f"unknown canvas: {id}"}
    if isinstance(content, str) and content.strip().startswith(("{", "[")):
        try:
            content = json.loads(content)
        except Exception:
            pass
    if isinstance(meta, str) and meta.strip().startswith("{"):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = None
    v = _validate_block(type, content)
    blocks = doc.setdefault("blocks", [])
    if len(blocks) >= _MAX_BLOCKS:
        return {"error": f"canvas is full ({_MAX_BLOCKS} blocks)"}
    block = {"id": _new_id("bk"), "type": v["type"], "ts": now_iso(),
             "content": v["content"], "meta": (meta if isinstance(meta, dict) else {}),
             "layout": {"order": len(blocks)}}
    # A KEY IS HOW ONE ITEM NAMES ANOTHER. An explode item binds to a code item BY KEY, and this -- the obvious
    # way to put both on a canvas -- had no key at all, so the binding could never resolve and the diagram drew
    # nothing (owner, 2026-09-22: "the explode mode in the canvas doesnt draw anything"). A key given here is
    # kept; one already taken is refused rather than silently doubled.
    # AN ITEM'S HEIGHT IS ITS SIZE. A diagram asking for 320px in an "m" item (110px of slot) was cut off at the
    # bottom, so an explode item lands at the size that can hold what it asked for -- and a caller can still say.
    size = str(size or "").strip().lower()
    if size not in ("s", "m", "l", "xl"):
        size = ""
    if not size and v["type"] == "explode":
        want = 0
        try:
            want = int((v["content"] or {}).get("height") or 0)
        except Exception:
            want = 0
        size = "xl" if want >= 340 else ("l" if want >= 180 else "m")
    if size:
        block["size"] = size
    key = str(key or "").strip()
    if key:
        if _find_key(doc, key) is not None:
            return {"error": "key already on this canvas: %s" % key, "key": key}
        block["key"] = key
    blocks.append(block)
    await _save(doc)
    await _emit(id, "append", block=block)
    return {"ok": True, "block_id": block["id"], "type": block["type"]}


@capability(
    "canvas.update", memory="off",
    http_method="POST", http_path="/canvas/update", http_tags=["canvas"],
    description="Update a block's content/meta in place (live patch — e.g. refresh a "
                "dynamic topic, a run's steps). Inputs: id (str) + block_id (str), or — "
                "through the resolver — key (str, <kind>:<ref>) with id or session_id; "
                "content (JSON), meta (JSON, optional). A keyed update is a write like "
                "every other: the revision bumps, the timeline records it, canvas.updated "
                "fires. Output: {ok, key?, block_id, revision?}.",
)
async def cap_canvas_update(id: str = "", block_id: str = "",
                            content: Any = None, meta: Any = None,
                            key: str = "", session_id: str = "", trace_id=None):
    if isinstance(content, str) and content.strip().startswith(("{", "[")):
        try:
            content = json.loads(content)
        except Exception:
            pass
    key = str(key or "").strip()
    if key and not block_id:
        # by key, on the session canvas (or an explicit id): the resolver's own path
        doc = await _target(id, session_id, create=False)
        if not doc:
            return {"ok": False, "error": f"unknown canvas: {id or session_id or '(no id or session_id)'}"}
        hit = _find_key(doc, key)
        if not hit:
            return {"ok": False, "error": f"unknown key: {key}"}
        if content is not None:
            v = _validate_block(hit.get("type", "note"), _as_obj(content))
            hit["content"] = v["content"]
        if isinstance(meta, dict):
            hit.setdefault("meta", {}).update(meta)
        hit["ts"] = now_iso()
        rev = await _write(doc, "update", key)
        return {"ok": True, "key": key, "block_id": hit.get("id"), "item": _item_view(hit),
                "id": doc["id"], "revision": rev}
    doc = await _load(id)
    if not doc:
        return {"error": f"unknown canvas: {id}"}
    for b in doc.get("blocks", []):
        if b.get("id") == block_id:
            if content is not None:
                v = _validate_block(b.get("type", "note"), content)
                b["content"] = v["content"]
            if isinstance(meta, dict):
                b.setdefault("meta", {}).update(meta)
            b["ts"] = now_iso()
            await _save(doc)
            await _emit(id, "update", block=b)
            return {"ok": True, "block_id": block_id}
    return {"error": f"unknown block: {block_id}"}


@capability(
    "canvas.move", memory="off",
    http_method="POST", http_path="/canvas/move", http_tags=["canvas"],
    description="Reorder / reposition a block (user rearrange or agent layout). "
                "Inputs: id (str!), block_id (str!), order (int, optional), layout "
                "(JSON {x,y,w,h}, optional).",
)
async def cap_canvas_move(id: str = "", block_id: str = "",
                          order: Optional[int] = None, layout: Any = None, trace_id=None):
    doc = await _load(id)
    if not doc:
        return {"error": f"unknown canvas: {id}"}
    if isinstance(layout, str) and layout.strip().startswith("{"):
        try:
            layout = json.loads(layout)
        except Exception:
            layout = None
    blocks = doc.get("blocks", [])
    target = next((b for b in blocks if b.get("id") == block_id), None)
    if not target:
        return {"error": f"unknown block: {block_id}"}
    lay = target.setdefault("layout", {})
    if isinstance(layout, dict):
        lay.update({k: layout[k] for k in ("x", "y", "w", "h") if k in layout})
    if order is not None:
        # Reinsert at the requested position and renumber `order` for all.
        blocks.remove(target)
        blocks.insert(max(0, min(int(order), len(blocks))), target)
        for i, b in enumerate(blocks):
            b.setdefault("layout", {})["order"] = i
    await _save(doc)
    await _emit(id, "move", block_id=block_id)
    return {"ok": True, "block_id": block_id}


@capability(
    "canvas.remove", memory="off",
    http_method="POST", http_path="/canvas/remove", http_tags=["canvas"],
    description="Remove a block from a canvas. Inputs: id (str!), block_id (str!) — or, for a "
                "keyed item on the session canvas, key (str) with id or session_id.",
)
async def cap_canvas_remove(id: str = "", block_id: str = "", key: str = "",
                            session_id: str = "", trace_id=None):
    if key:
        doc = await _target(id, session_id, create=False)
        if not doc:
            return {"ok": False, "error": f"unknown canvas: {id or session_id}"}
        hit = _find_key(doc, key)
        if not hit:
            return {"ok": False, "error": f"unknown key: {key}"}
        doc["blocks"] = [b for b in doc.get("blocks", []) if b is not hit]
        _tombstone(doc, [hit])
        rev = await _write(doc, "remove", key)
        return {"ok": True, "removed": hit.get("id"), "key": key, "id": doc["id"], "revision": rev, "restorable": True}
    doc = await _load(id)
    if not doc:
        return {"error": f"unknown canvas: {id}"}
    before = len(doc.get("blocks", []))
    gone = [b for b in doc.get("blocks", []) if b.get("id") == block_id]
    doc["blocks"] = [b for b in doc.get("blocks", []) if b.get("id") != block_id]
    if len(doc["blocks"]) == before:
        return {"error": f"unknown block: {block_id}"}
    _tombstone(doc, gone)
    await _save(doc)
    await _emit(id, "remove", block_id=block_id)
    return {"ok": True, "removed": block_id}


@capability(
    "canvas.removed", memory="off", silent=True,
    http_method="GET", http_path="/canvas/removed", http_tags=["canvas"],
    description="What was recently removed from a canvas and can be put back (newest first, up to 30). Inputs: id (str) "
                "or session_id (str). Output: {ok, removed: [{key, id, at, type, title}]}.",
)
async def cap_canvas_removed(id: str = "", session_id: str = "", trace_id=None):
    doc = await _target(id, session_id, create=False)
    if not doc:
        return {"ok": False, "error": f"unknown canvas: {id or session_id}"}
    out = []
    for r in reversed([r for r in (doc.get("removed") or []) if isinstance(r, dict)]):
        b = r.get("block") or {}
        c = b.get("content") if isinstance(b.get("content"), dict) else {}
        out.append({"key": r.get("key") or "", "id": r.get("id") or "", "at": r.get("at") or "",
                    "type": b.get("type") or "", "title": str(c.get("title") or c.get("name") or r.get("key") or b.get("type") or "")[:120]})
    return {"ok": True, "removed": out, "id": doc["id"]}


@capability(
    "canvas.restore", memory="off",
    http_method="POST", http_path="/canvas/restore", http_tags=["canvas"],
    description="Put a removed item back on its canvas (removing is undoable). Inputs: key (str) for a keyed "
                "item, or block_id (str) for a keyless block; id (str) or session_id (str). The item comes back as it "
                "was, in view. Fails if nothing with that key was removed, or an item with that key is on the canvas.",
)
async def cap_canvas_restore(key: str = "", block_id: str = "", id: str = "", session_id: str = "", trace_id=None):
    doc = await _target(id, session_id, create=False)
    if not doc:
        return {"ok": False, "error": f"unknown canvas: {id or session_id}"}
    rem = [r for r in (doc.get("removed") or []) if isinstance(r, dict)]
    want = next((r for r in reversed(rem) if (key and r.get("key") == key) or (block_id and r.get("id") == block_id)), None)
    if not want:
        return {"ok": False, "error": f"nothing removed with {'key ' + key if key else 'id ' + block_id}"}
    blk = dict(want.get("block") or {})
    if blk.get("key") and _find_key(doc, blk["key"]):
        return {"ok": False, "error": f"already on the canvas: {blk['key']}"}
    if blk.get("key"):
        blk["state"] = "now" if blk.get("state") in (None, "", "hidden") else blk["state"]
    doc.setdefault("blocks", []).append(blk)
    doc["removed"] = [r for r in rem if r is not want]
    rev = await _write(doc, "restore", blk.get("key") or blk.get("id") or "")
    return {"ok": True, "restored": blk.get("key") or blk.get("id"), "id": doc["id"], "revision": rev}


@capability(
    "canvas.clear", memory="off",
    http_method="POST", http_path="/canvas/clear", http_tags=["canvas"],
    description="Clear a canvas: every item goes, the canvas itself stays (its id, its title, its history). "
                "The session canvas is the chat's own working area and is never deleted — this is how you "
                "start it again. Inputs: id (str) or session_id (str); keep (str, comma-separated keys to "
                "leave behind, e.g. the items you pinned).",
)
async def cap_canvas_clear(id: str = "", session_id: str = "", keep: str = "", trace_id=None):
    doc = await _target(id, session_id, create=False)
    if not doc:
        return {"ok": False, "error": f"unknown canvas: {id or session_id}"}
    keys = {k.strip() for k in str(keep or "").split(",") if k.strip()}
    before = len(doc.get("blocks", []))
    # a kept key survives; everything else goes, including the keyless blocks of an agent's canvas
    gone = [b for b in doc.get("blocks", []) if not (b.get("key") and str(b.get("key")) in keys)]
    doc["blocks"] = [b for b in doc.get("blocks", []) if b.get("key") and str(b.get("key")) in keys]
    _tombstone(doc, gone)   # a clear can be taken back item by item too (WSU-66)
    rev = await _write(doc, "clear", ",".join(sorted(keys)) or "all")
    return {"ok": True, "id": doc["id"], "cleared": before - len(doc["blocks"]),
            "kept": len(doc["blocks"]), "revision": rev}


@capability(
    "canvas.delete", memory="off",
    http_method="POST", http_path="/canvas/delete", http_tags=["canvas"],
    description="Delete an entire canvas. Input: id (str!).",
)
async def cap_canvas_delete(id: str = "", trace_id=None):
    r = _redis()
    if not r:
        return {"error": "no store"}
    if not await _load(id):
        return {"error": f"unknown canvas: {id}"}
    try:
        await r.delete(KEY_CANVAS + id)
        await r.zrem(KEY_CANVAS_INDEX, id)
    except Exception as e:
        return {"error": str(e)}
    await _emit(id, "delete")
    return {"ok": True, "deleted": id}


@capability(
    "canvas.block_types", memory="off", silent=True,
    http_method="GET", http_path="/canvas/block_types", http_tags=["canvas"],
    description="The predefined block types + their content shapes — the canonical, "
                "consistent structures agents should fill (raw html is the escape hatch).",
)
async def cap_canvas_block_types(trace_id=None):
    return {"block_types": BLOCK_TYPES, "modes": list(CANVAS_MODES)}


# ── The session canvas and THE RESOLVER (Notes/38 §3.1–3.2, P0–P1). Argument names follow the directive
#    vocabulary in vera/ui/directives.py (key · kind · at · size), so a directive maps straight onto a call. ────
@capability(
    "canvas.session.resolve", memory="off", silent=True,
    http_method="POST", http_path="/canvas/session/resolve", http_tags=["canvas"],
    description="The chat session's canvas — cv_session_<sid>, created on demand (\"Session canvas\"). "
                "Input: session_id (str!). Output: {ok, id, revision, count, created}.",
)
async def cap_canvas_session_resolve(session_id: str = "", trace_id=None):
    if not _session_canvas_id(session_id):
        return {"ok": False, "error": "session_id is required"}
    had = await _load(_session_canvas_id(session_id))
    doc = await _target("", session_id)
    if not doc:
        return {"ok": False, "error": "no store"}
    return {"ok": True, "id": doc["id"], "revision": doc.get("revision", 0),
            "count": len(doc.get("blocks") or []), "created": had is None, "title": doc.get("title")}


@capability(
    "canvas.add", memory="off",
    http_method="POST", http_path="/canvas/add", http_tags=["canvas"],
    description="Put an item on the session canvas THROUGH THE RESOLVER — recall over recreate: an item whose "
                "key already exists is not added again, it is brought back into the NOW band (resolved: "
                "'shown'). Inputs: kind (block type), content (JSON for the kind), key (str, <kind>:<ref>; "
                "generated if absent), id (canvas id) or session_id (its session canvas), at "
                "('now'|'pinned'|'parked'|'hidden', default now), size ('xs'|'s'|'m'|'l'|'xl'), anchor "
                "({turn, mid, step} — the turn using it). Output: {ok, resolved: 'added'|'shown', existing, "
                "key, item, id, revision}.",
)
async def cap_canvas_add(id: str = "", session_id: str = "", kind: str = "note", content: Any = None,
                         key: str = "", at: str = "", size: str = "", anchor: Any = None, trace_id=None):
    doc = await _target(id, session_id)
    if not doc:
        return {"ok": False, "error": f"unknown canvas: {id or session_id or '(no id or session_id)'}"}
    key = str(key or "").strip()
    hit = _find_key(doc, key)
    if hit is not None:
        # the resolver's whole point: the same key comes back, it does not double
        face = _ic.face_for_state(hit, "now")   # un-parked (S2 transition 'unpark'): back as a tile, not its old face
        hit["state"] = "now"
        if face:
            hit["face"] = face
        hit["ts"] = now_iso()
        _add_anchor(hit, anchor)
        rev = await _write(doc, "show", key, turn=_anchor_turn(anchor))
        return {"ok": True, "resolved": "shown", "existing": True, "key": key, "item": _item_view(hit),
                "id": doc["id"], "revision": rev}
    if key and content is None:
        # a bare key is a request to SHOW; an unknown one is not made up out of nothing
        return {"ok": False, "error": f"unknown key: {key} — give kind and content to add it", "key": key}
    blocks = doc.setdefault("blocks", [])
    if len(blocks) >= _MAX_BLOCKS:
        return {"ok": False, "error": f"canvas is full ({_MAX_BLOCKS} blocks)"}
    v = _validate_block(str(kind or "note"), _as_obj(content))
    bid = _new_id("bk")
    if not key:
        key = f"{v['type']}:{bid}"
    block = {"id": bid, "type": v["type"], "ts": now_iso(), "content": v["content"], "meta": {},
             "layout": {"order": len(blocks)}, "key": key,
             "state": at if at in ITEM_STATES else "now",
             "size": size if size in ITEM_SIZES else "m"}
    _add_anchor(block, anchor)
    blocks.append(block)
    rev = await _write(doc, "add", key, turn=_anchor_turn(anchor))
    return {"ok": True, "resolved": "added", "existing": False, "key": key, "item": _item_view(block),
            "id": doc["id"], "revision": rev}


async def _set_state(id: str, session_id: str, key: str, state: str, op: str) -> Dict[str, Any]:
    doc = await _target(id, session_id, create=False)
    if not doc:
        return {"ok": False, "error": f"unknown canvas: {id or session_id}"}
    hit = _find_key(doc, key)
    if not hit:
        return {"ok": False, "error": f"unknown key: {key}"}
    prev = hit.get("state")
    face = _ic.face_for_state(hit, state)    # park -> a pill on the shelf; pinned back off it -> a tile (S2 transition)
    hit["state"] = state
    if face:
        hit["face"] = face
    rev = await _write(doc, op, key)
    return {"ok": True, "key": key, "state": state, "prev": prev, "item": _item_view(hit),
            "id": doc["id"], "revision": rev}


@capability(
    "canvas.pin", memory="off",
    http_method="POST", http_path="/canvas/pin", http_tags=["canvas"],
    description="Keep a session-canvas item above the flow. Inputs: key (str!), id or session_id.",
)
async def cap_canvas_pin(key: str = "", id: str = "", session_id: str = "", trace_id=None):
    return await _set_state(id, session_id, key, "pinned", "pin")


@capability(
    "canvas.park", memory="off",
    http_method="POST", http_path="/canvas/park", http_tags=["canvas"],
    description="Put a session-canvas item below the flow (the parked chip line). Inputs: key (str!), "
                "id or session_id.",
)
async def cap_canvas_park(key: str = "", id: str = "", session_id: str = "", trace_id=None):
    return await _set_state(id, session_id, key, "parked", "park")


@capability(
    "canvas.size", memory="off",
    http_method="POST", http_path="/canvas/size", http_tags=["canvas"],
    description="Resize a session-canvas item. Inputs: key (str!), size ('xs'|'s'|'m'|'l'|'xl'), id or session_id. "
                "Output includes prev so it can be undone.",
)
async def cap_canvas_size(key: str = "", size: str = "m", id: str = "", session_id: str = "", trace_id=None):
    size = str(size or "").lower()
    if size not in ITEM_SIZES:
        return {"ok": False, "error": f"size must be one of {'|'.join(ITEM_SIZES)}"}
    doc = await _target(id, session_id, create=False)
    if not doc:
        return {"ok": False, "error": f"unknown canvas: {id or session_id}"}
    hit = _find_key(doc, key)
    if not hit:
        return {"ok": False, "error": f"unknown key: {key}"}
    prev = hit.get("size")
    hit["size"] = size
    rev = await _write(doc, "size", key, size=size)
    return {"ok": True, "key": key, "size": size, "prev": prev, "item": _item_view(hit),
            "id": doc["id"], "revision": rev}


try:
    from Vera.vera.canvas import item_core as _ic
except ImportError:                                   # pragma: no cover
    from vera.canvas import item_core as _ic


@capability(
    "canvas.item.set", memory="off",
    http_method="POST", http_path="/canvas/item/set", http_tags=["canvas"],
    description="Change how one session-canvas item is shown - its face, rect, view, size, lock, fold, group or state - "
                "in one write and one canvas.updated event that names the fields. The item record is the same on every "
                "surface (the desk, a dashboard, the chat): face 'pill'|'tile'|'window'|'full'; place {fx, fy (0-1 of the "
                "surface), w, h (px), by 'auto'|'person', locked, layer 'among'|'behind'|'front', z}, merged over the "
                "item's place; view (list, cards, table, graph, chart, chips, fields, code, structure, both, tree, flat, validate, drawn); size 'xs'|'s'|'m'|'l'|'xl'; "
                "group {id?, front?, at?, sep?} (id: joined to that item; sep: taken out of its group; at: ms its tab was "
                "last brought to the front) or {} for the surface's own grouping; back 'pill'|'tile'|'window' (what a full "
                "item returns to) and dock 'tile'|'pill' (what a window gives when sent back), '' clears either; "
                "state 'now'|'pinned'|'parked'|'hidden'. Inputs: key (str!), id or "
                "session_id, and any of those fields. Output: {ok, key, changed, prev (to undo), item, revision}; a call "
                "that changes nothing writes nothing.",
)
async def cap_canvas_item_set(key: str = "", id: str = "", session_id: str = "", face: str = None, place: Any = None,
                              view: str = None, size: str = None, locked: Any = None, folded: Any = None,
                              group: Any = None, state: str = None, back: str = None, dock: str = None, trace_id=None):
    doc = await _target(id, session_id, create=False)
    if not doc:
        return {"ok": False, "error": f"unknown canvas: {id or session_id}"}
    hit = _find_key(doc, key)
    if not hit:
        return {"ok": False, "error": f"unknown key: {key}"}
    fields = {"face": face, "place": _as_obj(place), "view": view, "size": size, "locked": locked,
              "folded": folded, "group": _as_obj(group), "state": state, "back": back, "dock": dock}
    changed, prev, probs = _ic.apply_set(hit, fields)
    if probs:
        return {"ok": False, "error": "; ".join(probs), "key": key}
    if not changed:
        return {"ok": True, "key": key, "changed": {}, "prev": {}, "item": _ic.normalise(hit),
                "id": doc["id"], "revision": int(doc.get("revision") or 0)}
    _record(doc, "item", key, fields=",".join(sorted(changed)))
    await _save(doc)
    await _emit(doc["id"], "item", id=doc["id"], revision=doc.get("revision"), key=key, fields=changed)
    return {"ok": True, "key": key, "changed": changed, "prev": prev, "item": _ic.normalise(hit),
            "id": doc["id"], "revision": int(doc.get("revision") or 0)}


@capability(
    "canvas.items", memory="off", silent=True,
    http_method="GET", http_path="/canvas/items", http_tags=["canvas"],
    description="The session canvas's items as item records - the one shape every surface reads: key, kind, content, "
                "source, anchors, state, face, back, dock, size, place, locked, view, group, folded, ts, score. Inputs: id or "
                "session_id, state (optional filter), if_rev (int: returns {ok, unchanged:true} when the canvas is still "
                "at that revision). Output: {ok, id, revision, items}. Read-only.",
)
async def cap_canvas_items(id: str = "", session_id: str = "", state: str = "", if_rev: Any = None, trace_id=None):
    doc = await _target(id, session_id, create=False)
    if not doc:
        return {"ok": False, "error": f"unknown canvas: {id or session_id}", "items": []}
    rev = int(doc.get("revision") or 0)
    try:
        if if_rev not in (None, "") and int(if_rev) == rev:
            return {"ok": True, "id": doc["id"], "revision": rev, "unchanged": True}
    except (TypeError, ValueError):
        pass
    items = [_ic.normalise(b) for b in doc.get("blocks", []) if isinstance(b, dict) and b.get("key")]
    if state:
        items = [i for i in items if i["state"] == state]
    return {"ok": True, "id": doc["id"], "revision": rev, "items": items}


@capability(
    "canvas.recall", memory="off", silent=True,
    http_method="GET", http_path="/canvas/recall", http_tags=["canvas"],
    description="Find items already on a canvas before creating one (recall over recreate). Inputs: id (str!), "
                "q (str — matched against key, kind, title and content). Output: {ok, matches:[item]}. "
                "Read-only; canvas.add with the match's key brings it back.",
)
async def cap_canvas_recall(id: str = "", q: str = "", session_id: str = "", trace_id=None):
    doc = await _target(id, session_id, create=False)
    if not doc:
        return {"ok": False, "error": f"unknown canvas: {id or session_id}", "matches": []}
    needle = str(q or "").strip().lower()
    out = []
    for b in doc.get("blocks", []):
        if not b.get("key"):
            continue
        hay = " ".join([str(b.get("key") or ""), str(b.get("type") or ""),
                        json.dumps(b.get("content") or {}, ensure_ascii=False)]).lower()
        if not needle or needle in hay:
            out.append(_item_view(b))
    return {"ok": True, "id": doc["id"], "q": q, "matches": out}


@capability(
    "canvas.timeline", memory="off", silent=True,
    http_method="GET", http_path="/canvas/timeline", http_tags=["canvas"],
    description="A canvas's write history: {ok, id, revision, timeline:[{rev, ts, op, key}]}. Input: id (str!).",
)
async def cap_canvas_timeline(id: str = "", session_id: str = "", trace_id=None):
    doc = await _target(id, session_id, create=False)
    if not doc:
        return {"ok": False, "error": f"unknown canvas: {id or session_id}"}
    return {"ok": True, "id": doc["id"], "revision": doc.get("revision", 0),
            "timeline": list(doc.get("timeline") or [])}


@capability(
    "canvas.session.room", memory="off", silent=True,
    http_method="GET", http_path="/canvas/session/room", http_tags=["canvas"],
    description="What the session canvas holds, for the room manifest: {id, revision, now:[keys], pinned:[keys], "
                "parked:[keys], sizes:{key:size}, count}. Input: session_id (str!). Never creates the canvas.",
)
async def cap_canvas_session_room(session_id: str = "", trace_id=None):
    cid = _session_canvas_id(session_id)
    doc = await _load(cid) if cid else None
    if not doc:
        return {"ok": True, "id": cid, "revision": 0, "now": [], "pinned": [], "parked": [], "sizes": {},
                "count": 0}
    keyed = [b for b in doc.get("blocks", []) if b.get("key")]
    by = lambda st: [b["key"] for b in keyed if b.get("state") == st]
    return {"ok": True, "id": doc["id"], "revision": doc.get("revision", 0),
            "now": by("now"), "pinned": by("pinned"), "parked": by("parked"),
            "sizes": {b["key"]: b.get("size") or "m" for b in keyed}, "count": len(keyed)}


# ── THE RELEVANCE ENGINE (Notes/38 §3.3, P2): what is in focus now. Six signals, in order of weight; the score is
#    the strongest of them. Items above the threshold are live — a parked one is recalled (its anchors grow, the
#    timeline says "recall"); below it they park; pinned items never leave focus. The engine proposes, then applies
#    within the document (apply=True) or only answers (apply=False, a focus change in the chat). ─────────────────
_REL_THRESHOLD = 0.35
_REL_RECENT_LIVE = 2           # an item stays live for this many turns after its last anchor, then decays
_REL_WORD = re.compile(r"[a-z0-9][a-z0-9_.:/-]{2,}")
# the user's intent: a small phrase table → the item kinds it means (the model's own directive is the resolver hit)
_REL_INTENT: Dict[str, tuple] = {
    "notebook": ("my notes", "the notes", "the notebook", "that notebook", "our notes"),
    "session":  ("that terminal", "the terminal", "the shell", "that shell", "the ssh", "that session"),
    "widget":   ("the chart", "that chart", "the graph from before", "the plot", "the widget", "that widget", "the tile"),
    "table":    ("the table", "that table", "the rows", "those rows"),
    "diagram":  ("the diagram", "that diagram"),
    "markdown": ("the document", "the doc", "that document", "the notes", "the report"),
    "note":     ("the note", "that note", "the notes"),
    "code":     ("the code", "that snippet", "the file", "that file", "the script"),
    "loop":     ("the loop", "that run", "the run", "that loop"),
    "image":    ("the image", "that image", "the picture", "that picture"),
}
_REL_KIND_ALIASES = {"notebook": ("notebook", "note"), "session": ("session", "terminal"), "widget": ("widget", "chart"),
                     "diagram": ("diagram", "mermaid"), "markdown": ("markdown", "document")}


def _rel_strings(v: Any, out: List[str], depth: int = 0) -> None:
    if depth > 3 or v is None:
        return
    if isinstance(v, str):
        out.append(v)
    elif isinstance(v, dict):
        for x in v.values():
            _rel_strings(x, out, depth + 1)
    elif isinstance(v, (list, tuple)):
        for x in v[:40]:
            _rel_strings(x, out, depth + 1)


_REL_SPLIT = re.compile(r"[:/._-]+")
"""NAMES AND THEIR SYMBOLS, FROM LIVE DATA (owner 2026-10-11: a turn about "bitcoin" scored the BTC-USD chart 0.12 - the
chart says BTC, the turn says bitcoin; and "you hardcoded aliases for tickers - bad idea create a proper converter using
live data you dont have to maintain"). Two sources, nothing written by hand:
- the tracked market universe (markets.baseline.list, markets.overview, markets.watchlist.list): each asset's symbol and
  name make one group (BTC-USD + "Bitcoin USD" -> {btc, bitcoin}); built in the background, refreshed hourly;
- an entity of the turn the universe does not know is asked of the market search once (markets.lookup, 1.2 s at most)
  and the answer kept (an empty answer too, for an hour).
Quote currencies and company words (usd, inc, etf, ...) are never a link. Relevance never waits on the network for more
than that one bounded lookup; with the markets subsystem absent every map is empty and scoring is as before."""
_ALIAS_TTL = 3600.0
_ALIAS_GENERIC = {"usd", "eur", "gbp", "jpy", "usdt", "usdc", "inc", "corp", "corporation", "ltd", "plc", "co", "the", "and",
                  "etf", "fund", "trust", "class", "index", "group", "holdings", "company", "shares", "share", "common",
                  "stock", "spdr", "ishares", "vanguard", "invesco", "yahoo", "binance", "coinbase", "kraken", "spot", "perp"}
_ALIAS: Dict[str, Any] = {"at": 0.0, "of": {}, "task": None}
_ALIAS_LOOK: Dict[str, Any] = {}   # entity -> (time, set of words)


def _alias_group(symbol: str, name: str) -> set:
    """One asset's words: its symbol's parts and its name's words, less quote currencies and company words."""
    sym = {p for p in re.split(r"[^a-z0-9]+", str(symbol or "").lower()) if len(p) >= 2 and not p.isdigit()}
    nam = {w for w in re.split(r"[^a-z0-9&]+", str(name or "").lower()) if len(w) >= 3 and not w.isdigit()}
    return (sym | nam) - _ALIAS_GENERIC


async def _alias_call(name: str, timeout: float, **kw) -> Dict[str, Any]:
    e = CAPABILITY_REGISTRY.get(name) or {}
    fn = isinstance(e, dict) and (e.get("func") or e.get("raw"))
    if not fn:
        return {}
    try:
        params = inspect.signature(e.get("raw") or fn).parameters
        if not any(p.kind == p.VAR_KEYWORD for p in params.values()):
            kw = {k: v for k, v in kw.items() if k in params}
    except (TypeError, ValueError):
        pass
    try:
        r = await asyncio.wait_for(fn(**kw), timeout=timeout)
        return r if isinstance(r, dict) else {}
    except Exception:
        return {}


def _alias_assets(r: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for k in ("assets", "results", "watchlist", "positions", "quotes"):
        out += [x for x in (r.get(k) or []) if isinstance(x, dict)]
    for g in r.get("groups") or []:
        if isinstance(g, dict):
            out += [x for x in (g.get("assets") or []) if isinstance(x, dict)]
    return out


async def _alias_build() -> None:
    of: Dict[str, set] = {}
    for cap in ("markets.baseline.list", "markets.overview", "markets.watchlist.list"):
        for a in _alias_assets(await _alias_call(cap, 20.0)):
            grp = _alias_group(a.get("symbol") or a.get("key") or "", a.get("name") or "")
            if len(grp) < 2:
                continue
            for w in grp:
                of.setdefault(w, set()).update(grp)
    _ALIAS["of"] = of
    _ALIAS["at"] = time.time()


def _alias_refresh_soon() -> None:
    """Rebuild the universe map in the background when it is stale; never awaited by a turn."""
    t = _ALIAS.get("task")
    if time.time() - float(_ALIAS.get("at") or 0) < _ALIAS_TTL or (t is not None and not t.done()):
        return
    try:
        _ALIAS["task"] = asyncio.get_event_loop().create_task(_alias_build())
    except Exception:
        pass


async def _rel_alias_map(entities: List[str]) -> Dict[str, set]:
    """word -> its group, for this turn: the universe map, plus a bounded live lookup of each entity it does not know."""
    _alias_refresh_soon()
    amap: Dict[str, set] = dict(_ALIAS.get("of") or {})
    for e in [str(x).strip().lower() for x in entities or []][:4]:
        if len(e) < 3 or e in amap or e in _ALIAS_GENERIC:
            continue
        hit = _ALIAS_LOOK.get(e)
        if not hit or time.time() - hit[0] > _ALIAS_TTL:
            grp: set = set()
            # the first result whose NAME has the entity as a whole word (not "pineapple" for "apple"), and only its
            # symbol is linked - not every word of its name
            for a in _alias_assets(await _alias_call("markets.lookup", 1.2, query=e, limit=3))[:3]:
                if e in set(re.split(r"[^a-z0-9&]+", str(a.get("name") or "").lower())):
                    grp = {p for p in re.split(r"[^a-z0-9]+", str(a.get("symbol") or "").lower()) if len(p) >= 2 and not p.isdigit()} - _ALIAS_GENERIC
                    break
            hit = (time.time(), grp)
            _ALIAS_LOOK[e] = hit
        if hit[1]:
            for w in hit[1]:
                amap.setdefault(w, set()).update(hit[1] | {e})
            amap.setdefault(e, set()).update(hit[1])
    return amap


def _rel_expand(words: set, amap: Optional[Dict[str, set]] = None) -> set:
    """Words plus their live aliases (a name's symbols, a symbol's name)."""
    out = set(words)
    for w in words:
        out |= (amap or {}).get(w, set())
    return out


def _rel_words(s: str) -> set:
    """The words of a text: whole tokens and, for compound ones (yahoo:btc-usd, markets.chart), their parts too."""
    ws = set(_REL_WORD.findall(str(s or "").lower()))
    return ws | {p for w in ws for p in _REL_SPLIT.split(w) if len(p) >= 2}


def _rel_item_text(b: Dict[str, Any]) -> str:
    parts: List[str] = [str(b.get("key") or ""), str(b.get("type") or "")]
    _rel_strings(b.get("content"), parts)
    for a in b.get("anchors") or []:
        if isinstance(a, dict):
            parts.extend(str(a.get(k) or "") for k in ("entity", "topic", "subject"))
    return " ".join(parts)[:4000]


def _rel_anchor_turns(b: Dict[str, Any]) -> List[str]:
    out = []
    for a in b.get("anchors") or []:
        t = _anchor_turn(a)
        if t:
            out.append(t)
    return out


def _rel_kind_of(b: Dict[str, Any]) -> set:
    kinds = {str(b.get("type") or "")}
    key = str(b.get("key") or "")
    if ":" in key:
        kinds.add(key.split(":", 1)[0])
    return {k for k in kinds if k}


def _rel_score(b: Dict[str, Any], turn: str, text: str, entities: List[str], recent: List[str],
               hits: set, intents: Dict[str, str], amap: Optional[Dict[str, set]] = None) -> Dict[str, Any]:
    """One item's score and the signal that carried it."""
    key = str(b.get("key") or "")
    sig: Dict[str, float] = {}
    if key in hits or (turn and turn in _rel_anchor_turns(b)):
        sig["explicit"] = 1.0
    if b.get("state") == "pinned":
        sig["pin"] = 1.0
    item_text = _rel_item_text(b)
    low = item_text.lower()
    words = _rel_words(item_text)
    ents = [e for e in (str(x).strip().lower() for x in entities or []) if len(e) >= 3]
    ents = sorted(_rel_expand(set(ents), amap) | set(ents), key=len, reverse=True)   # an entity's live aliases count as it
    if any((e in words) or (len(e) >= 4 and e in low) for e in ents if len(e) >= 2):
        sig["entity"] = 0.85
    kinds = _rel_kind_of(b)
    for kind, best_key in intents.items():
        aliases = set(_REL_KIND_ALIASES.get(kind, ())) | {kind}
        if kinds & aliases:
            sig["intent"] = max(sig.get("intent", 0.0), 0.7 if best_key == key else 0.5)
    tw = _rel_expand(_rel_words(text), amap)
    if tw and words:
        overlap = len(tw & words) / float(max(1, min(len(tw), 12)))
        if overlap > 0:
            sig["similarity"] = round(min(0.6, 0.6 * overlap), 3)
    turns = _rel_anchor_turns(b)
    pos = None
    for t in turns:
        if t in recent:
            i = recent.index(t)
            pos = i if pos is None else min(pos, i)
    if pos is not None:
        sig["recency"] = 0.5 if pos <= _REL_RECENT_LIVE else round(max(0.0, 0.5 * (1 - (pos - _REL_RECENT_LIVE) / 3.0)), 3)
    score = max(sig.values()) if sig else 0.0
    return {"score": round(score, 3), "signals": sig}


@capability(
    "canvas.session.relevance", memory="off", silent=True,
    http_method="POST", http_path="/canvas/session/relevance", http_tags=["canvas"],
    description="What is in focus on the session canvas now (Notes/38 §3.3). Six signals, strongest wins: an "
                "explicit resolver hit this turn (1.0) · an entity of the turn in the item (0.85) · the user's "
                "intent — 'my notes', 'that terminal', 'the chart from before' (0.7) · similarity of the turn to the "
                "item's text (≤0.6) · recency of the item's anchors (0.5 for two turns, then decays) · a pin (1.0, "
                "never leaves focus). Above the threshold an item is live — a parked one is RECALLED (anchors grow, "
                "timeline op 'recall'); below it a live one parks; pinned stay; hidden are untouched. Inputs: "
                "session_id (or id), turn (the turn's mid), text (the turn's words), entities ([str]), recent "
                "([mids], newest first), apply (bool, default true — false only answers), threshold (float). "
                "Output: {ok, id, revision, turn, focus:[keys], scores:{key:{score, signals}}, recalled:[keys], "
                "parked:[keys], applied}.",
)
async def cap_canvas_session_relevance(session_id: str = "", turn: str = "", text: str = "", entities: Any = None,
                                       recent: Any = None, apply: bool = True, threshold: float = _REL_THRESHOLD,
                                       id: str = "", trace_id=None):
    doc = await _target(id, session_id, create=False)
    if not doc:
        return {"ok": True, "id": _session_canvas_id(session_id) or id, "revision": 0, "turn": turn, "focus": [],
                "scores": {}, "recalled": [], "parked": [], "applied": False}
    turn = str(turn or "")
    entities = _as_obj(entities) if entities is not None else []
    entities = [str(x) for x in entities] if isinstance(entities, (list, tuple)) else []
    amap = await _rel_alias_map(entities)   # names <-> symbols from live market data (bounded; empty without markets)
    recent = _as_obj(recent) if recent is not None else []
    recent = [str(x) for x in recent] if isinstance(recent, (list, tuple)) else []
    if turn and turn not in recent:
        recent.insert(0, turn)
    try:
        threshold = float(threshold)
    except Exception:
        threshold = _REL_THRESHOLD
    keyed = [b for b in doc.get("blocks", []) if b.get("key")]
    hits = {e.get("key") for e in doc.get("timeline") or [] if turn and e.get("turn") == turn and e.get("key")}
    # the intent phrases the turn contains → the kind meant; the most recently anchored item of that kind wins
    low = str(text or "").lower()
    intents: Dict[str, str] = {}
    for kind, phrases in _REL_INTENT.items():
        if any(ph in low for ph in phrases):
            aliases = set(_REL_KIND_ALIASES.get(kind, ())) | {kind}
            cands = [b for b in keyed if _rel_kind_of(b) & aliases]
            cands.sort(key=lambda b: str(b.get("ts") or ""), reverse=True)
            intents[kind] = str(cands[0].get("key")) if cands else ""
    scores: Dict[str, Any] = {}
    focus: List[str] = []
    recalled: List[str] = []
    parked: List[str] = []
    for b in keyed:
        s = _rel_score(b, turn, text, entities, recent, hits, intents, amap)
        key = str(b["key"])
        scores[key] = s
        live = s["score"] >= threshold or b.get("state") == "pinned"
        if live:
            focus.append(key)
        if not apply:
            continue
        b["score"] = s["score"]
        st = b.get("state")
        if live and st == "parked":
            b["state"] = "now"
            b["face"] = "tile"                     # recalled off the shelf: a tile (S2 transition 'unpark')
            b["ts"] = now_iso()
            if turn:
                _add_anchor(b, {"turn": turn, "role": "recalled"})
            recalled.append(key)
        elif (not live) and st == "now" and "explicit" not in s["signals"]:
            b["state"] = "parked"
            b["face"] = "pill"                     # parked: a pill on the shelf (S2 transition 'park')
            parked.append(key)
    rev = int(doc.get("revision") or 0)
    if apply and (recalled or parked):
        for key in recalled:
            _record(doc, "recall", key, turn=turn)
        for key in parked:
            _record(doc, "park", key, turn=turn)
        rev = await _write(doc, "relevance", "", turn=turn, focus=focus[:50])
    elif apply:
        await _save(doc)      # the scores on the items, no revision — nothing moved
    return {"ok": True, "id": doc["id"], "revision": rev, "turn": turn, "focus": focus, "scores": scores,
            "recalled": recalled, "parked": parked, "applied": bool(apply)}


@capability(
    "canvas.ask", memory="off",
    http_method="POST", http_path="/canvas/ask", http_tags=["canvas"],
    description="Record a question asked about a canvas item (the ask chip itself is drawn by the chat). Inputs: "
                "key (str!), question (str!), id or session_id. Output: {ok, key, question, revision}.",
)
async def cap_canvas_ask(key: str = "", question: str = "", id: str = "", session_id: str = "", trace_id=None):
    doc = await _target(id, session_id, create=False)
    if not doc:
        return {"ok": False, "error": f"unknown canvas: {id or session_id}"}
    if not _find_key(doc, key):
        return {"ok": False, "error": f"unknown key: {key}"}
    rev = await _write(doc, "ask", key, q=str(question or "")[:500])
    return {"ok": True, "key": key, "question": question, "id": doc["id"], "revision": rev}


# ── UI: the <canvas panel> — a live whiteboard renderer (its own page + a tab) ──
@capability(
    "canvas.panel.html", memory="off", silent=True,
    http_method="GET", http_path="/canvas/panel", http_tags=["canvas", "ui"],
    description="Serve the Canvas whiteboard panel HTML.",
)
async def cap_canvas_panel_html(trace_id=None):
    try:
        html = _PANEL_HTML.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = ("<!DOCTYPE html><html><body style='background:#0d0f12;color:#c96a5a;"
                "font-family:monospace;padding:40px'><h2>canvas_panel.html not found</h2>"
                f"<p>Expected at {_PANEL_HTML}</p></body></html>")
    return HTMLResponse(html)


@capability(
    "canvas.show", memory="off",
    http_method="POST", http_path="/canvas/show", http_tags=["canvas", "ui"],
    description="DISPLAY a canvas to the user IN THEIR CHAT — the agent-facing way "
                "to put a canvas in front of someone instead of handing them a link. "
                "It renders as a live artifact card that follows the document, so "
                "anything you append afterwards appears in place. Inputs: id (str! — "
                "canvas id), session_id (str — target chat session; falls back to the "
                "calling turn), title (str — card header), pinned (bool — open it as a "
                "FLOATING window that stays put while the conversation scrolls, "
                "instead of sitting inline in the transcript). Output: {ok, shown}.",
)
async def cap_canvas_show(id: str = "", session_id: str = "", title: str = "",
                          pinned: bool = False, trace_id=None) -> Dict[str, Any]:
    cid = (id or "").strip()
    if not cid:
        return {"ok": False, "error": "id is required"}
    doc = await _load(cid)
    if not doc:
        return {"ok": False, "error": f"no such canvas: {cid}"}
    disp = (CAPABILITY_REGISTRY.get("panel.dispatch") or {}).get("func")
    if not disp:
        return {"ok": False, "error": "panel.dispatch unavailable"}
    try:
        reply = await disp(
            session_id=session_id,
            action="__chat_render__",
            payload={"kind": "canvas", "canvas_id": cid,
                     "title": title or doc.get("title") or "Canvas",
                     "popout": bool(pinned)},
            timeout_secs=8.0,
        )
    except Exception as e:
        return {"ok": False, "error": f"dispatch failed: {e}"}
    ok = bool(reply.get("ok")) if isinstance(reply, dict) else False
    out: Dict[str, Any] = {"ok": ok, "shown": ok, "id": cid, "pinned": bool(pinned)}
    if not ok and isinstance(reply, dict) and reply.get("error"):
        out["error"] = reply["error"]
    return out


# ── canvas.run: a capability's answer, as an item ─────────────────────────────────────────────────────────────────
_RUN_MAX_CHARS = 300_000


def _json_safe(v: Any) -> Any:
    """What a capability answered, as something the canvas can store: JSON, strings for what is not, and bounded -
    an answer too big to keep is kept as the start of its text rather than dropped."""
    try:
        s = json.dumps(v, default=str)
    except Exception:
        s = json.dumps(str(v))
    if len(s) > _RUN_MAX_CHARS:
        return {"_truncated": True, "chars": len(s), "text": s[:_RUN_MAX_CHARS]}
    return json.loads(s)


def _args_key(args: Dict[str, Any]) -> str:
    """The same cap with the same arguments is the same item: a short, stable digest of the arguments."""
    try:
        raw = json.dumps(args or {}, sort_keys=True, default=str)
    except Exception:
        raw = str(args)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:10]


@capability(
    "canvas.run", memory="off",
    http_method="POST", http_path="/canvas/run", http_tags=["canvas"],
    description="RUN a capability and put its RESULT on the session canvas as the element that fits it - a command's "
                "output as a terminal, rows as a table, numbers as a widget, a file as a code item, a page as html, "
                "a record as its fields, anything else as a JSON tree. The item is keyed by the cap and its "
                "arguments, so running it again UPDATES the same item (its 'run again' button does exactly that). "
                "Inputs: cap (str! - the capability to run; canvas.* is refused), args (obj - its arguments), id or "
                "session_id (the canvas), key (str - default result:<cap>:<digest of args>), at, size, anchor "
                "({turn, mid}), timeout_s (default 120, at most 900). Output: {ok, key, cap_ok, cap_error?, ms, "
                "resolved: 'added'|'updated', id, revision}.",
)
async def cap_canvas_run(cap: str = "", args: Any = None, id: str = "", session_id: str = "", key: str = "",
                         at: str = "", size: str = "", anchor: Any = None, timeout_s: float = 120.0, trace_id=None):
    name = str(cap or "").strip()
    if not name:
        return {"ok": False, "error": "cap is required"}
    if name.startswith("canvas."):
        return {"ok": False, "error": "canvas.* capabilities describe the canvas; they are not run onto it"}
    entry = CAPABILITY_REGISTRY.get(name) or {}
    fn = entry.get("func") or entry.get("raw")
    if not fn:
        return {"ok": False, "error": f"unknown capability: {name}"}
    a = _as_obj(args) if args not in (None, "") else {}
    if not isinstance(a, dict):
        return {"ok": False, "error": "args must be an object"}
    a = {k: v for k, v in a.items() if k != "trace_id"}
    doc = await _target(id, session_id)
    if not doc:
        return {"ok": False, "error": f"unknown canvas: {id or session_id or '(no id or session_id)'}"}
    try:
        limit = max(1.0, min(float(timeout_s or 120.0), 900.0))
    except Exception:
        limit = 120.0
    t0 = time.time()
    err = ""
    res: Any = None
    try:
        if inspect.iscoroutinefunction(fn):
            res = await asyncio.wait_for(fn(**a, trace_id=trace_id), timeout=limit)
        else:
            # a synchronous capability runs off the event loop, so a slow one cannot stall the server
            res = await asyncio.wait_for(asyncio.to_thread(lambda: fn(**a, trace_id=trace_id)), timeout=limit)
            if inspect.isawaitable(res):
                res = await asyncio.wait_for(res, timeout=limit)
    except asyncio.TimeoutError:
        err = f"timed out after {int(limit)} s"
    except TypeError as e:
        err = f"bad arguments for {name}: {e}"
    except Exception as e:  # the canvas says what happened; it never takes the chat down with it
        err = f"{type(e).__name__}: {e}"
    ms = int((time.time() - t0) * 1000)
    safe = _json_safe(res)
    failed = bool(err) or (isinstance(safe, dict) and safe.get("ok") is False)
    content: Dict[str, Any] = {"cap": name, "args": a, "result": safe, "ok": not failed, "ms": ms, "ts": now_iso()}
    if err:
        content["error"] = err
    elif failed and isinstance(safe, dict) and (safe.get("error") or safe.get("message")):
        content["error"] = str(safe.get("error") or safe.get("message"))
    k = str(key or "").strip() or f"result:{name}:{_args_key(a)}"
    hit = _find_key(doc, k)
    if hit is not None:
        hit["type"] = "result"
        hit["content"] = content
        st = at if at in ITEM_STATES else "now"
        face = _ic.face_for_state(hit, st)       # park -> pill, back off the shelf -> tile (S2 transition)
        hit["state"] = st
        if face:
            hit["face"] = face
        hit["ts"] = now_iso()
        if size in ITEM_SIZES:
            hit["size"] = size
        _add_anchor(hit, anchor)
        rev = await _write(doc, "update", k, turn=_anchor_turn(anchor))
        resolved = "updated"
    else:
        blocks = doc.setdefault("blocks", [])
        if len(blocks) >= _MAX_BLOCKS:
            return {"ok": False, "error": f"canvas is full ({_MAX_BLOCKS} blocks)"}
        block = {"id": _new_id("bk"), "type": "result", "ts": now_iso(), "content": content, "meta": {},
                 "layout": {"order": len(blocks)}, "key": k,
                 "state": at if at in ITEM_STATES else "now",
                 "size": size if size in ITEM_SIZES else "m"}
        _add_anchor(block, anchor)
        blocks.append(block)
        rev = await _write(doc, "add", k, turn=_anchor_turn(anchor))
        resolved = "added"
    out: Dict[str, Any] = {"ok": True, "key": k, "cap_ok": not failed, "ms": ms, "resolved": resolved,
                           "id": doc["id"], "revision": rev}
    if content.get("error"):
        out["cap_error"] = content["error"]
    return out


_ELEMENT_JS = Path(__file__).parent / "canvas_element.js"
# the item contract (window.VeraItem): served in front of the element, which registers its bodies with it and draws each
# item's head row and menu from it (WSU-58 S6). It loads once per page - a second copy keeps the first's registry.
_VERA_ITEM_JS = Path(__file__).parent.parent / "ui" / "vera_item.js"


@APP.get("/ui/elements/canvas_element.js", include_in_schema=False)
async def _canvas_element_js():
    """Serve <vera-canvas> — the embeddable live canvas view.

    Loaded globally by the harness AND by the chat panel (its own document), so
    a canvas can be dropped straight into a conversation with
    `<vera-canvas canvas-id="cv_…">`. Cached briefly: this is fetched on every
    page load but changes only on deploy.
    """
    try:
        js = _ELEMENT_JS.read_text(encoding="utf-8")
    except FileNotFoundError:
        js = "/* canvas_element.js missing */"
    try:
        js = _VERA_ITEM_JS.read_text(encoding="utf-8") + "\n" + js
    except FileNotFoundError:
        js = "console.warn('vera_item.js missing: the canvas draws no item heads');\n" + js
    return Response(content=js, media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=60"})


register_ui(
    "canvas",
    "Canvas",
    "◮",
    """<div style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/canvas/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write"></iframe>
</div>""",
    "",
    ui_caps=["canvas.create", "canvas.get", "canvas.list", "canvas.append",
             "canvas.update", "canvas.move", "canvas.remove", "canvas.removed", "canvas.restore", "canvas.clear", "canvas.delete",
             "canvas.block_types",
             "canvas.session.resolve", "canvas.add", "canvas.pin", "canvas.park", "canvas.size",
             "canvas.recall", "canvas.timeline", "canvas.session.room", "canvas.session.relevance", "canvas.ask",
             "canvas.run"],
    mode="tab",
    tab_order=60,
)
