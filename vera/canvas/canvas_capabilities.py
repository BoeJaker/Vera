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
    "loop":     {"desc": "An agentic run as an item: its goal, status and steps (each a "
                         "capability) — written by the chat and by the loop itself (P7).",
                 "content": "{goal:str, status:str, steps:[{n:str, cap?:str, status?:str, ms?:str}], run?:str}"},
}
CANVAS_MODES = ("dynamic", "static")

# ── The SESSION canvas (Notes/38): one document per chat session, its items KEYED so the same thing is never on
#    the canvas twice. A keyed block carries key (<kind>:<ref>), state (now · parked · pinned · hidden), size
#    (s · m · l · xl) and its anchors (the turns that used it — additive, never replaced). The document carries a
#    revision (bumped on every write) and a timeline ([{rev, ts, op, key}], append-only; bounded so a runaway loop
#    cannot grow it forever). Keyless blocks — the existing canvases — are untouched by all of this. ─────────────
ITEM_STATES = ("now", "parked", "pinned", "hidden")
ITEM_SIZES = ("s", "m", "l", "xl")
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
    doc = {"id": cid, "title": "Session canvas", "mode": "session", "topic": "",
           "session": str(session_id), "created": now_iso(), "updated": now_iso(),
           "blocks": [], "revision": 0, "timeline": []}
    await _write(doc, "create")
    return doc


def _item_view(b: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": b.get("id"), "key": b.get("key"), "type": b.get("type"), "state": b.get("state"),
            "size": b.get("size"), "anchor": b.get("anchor"), "anchors": b.get("anchors") or [],
            "ts": b.get("ts"), "score": b.get("score")}


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
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        return None


async def _save(doc: Dict[str, Any]) -> None:
    r = _redis()
    if not r:
        return
    doc["updated"] = now_iso()
    doc["revision"] = int(doc.get("revision") or 0) + 1
    try:
        await r.set(KEY_CANVAS + doc["id"], json.dumps(doc))
        await r.zadd(KEY_CANVAS_INDEX, {doc["id"]: _now_epoch()})
    except Exception:
        pass


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
                            content: Any = None, meta: Any = None, trace_id=None):
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
        rev = await _write(doc, "remove", key)
        return {"ok": True, "removed": hit.get("id"), "key": key, "id": doc["id"], "revision": rev}
    doc = await _load(id)
    if not doc:
        return {"error": f"unknown canvas: {id}"}
    before = len(doc.get("blocks", []))
    doc["blocks"] = [b for b in doc.get("blocks", []) if b.get("id") != block_id]
    if len(doc["blocks"]) == before:
        return {"error": f"unknown block: {block_id}"}
    await _save(doc)
    await _emit(id, "remove", block_id=block_id)
    return {"ok": True, "removed": block_id}


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
                "('now'|'pinned'|'parked'|'hidden', default now), size ('s'|'m'|'l'|'xl'), anchor "
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
        hit["state"] = "now"
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
    hit["state"] = state
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
    description="Resize a session-canvas item. Inputs: key (str!), size ('s'|'m'|'l'|'xl'), id or session_id. "
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


def _rel_words(s: str) -> set:
    return set(_REL_WORD.findall(str(s or "").lower()))


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
               hits: set, intents: Dict[str, str]) -> Dict[str, Any]:
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
    if any((e in words) or (len(e) >= 4 and e in low) for e in ents):
        sig["entity"] = 0.85
    kinds = _rel_kind_of(b)
    for kind, best_key in intents.items():
        aliases = set(_REL_KIND_ALIASES.get(kind, ())) | {kind}
        if kinds & aliases:
            sig["intent"] = max(sig.get("intent", 0.0), 0.7 if best_key == key else 0.5)
    tw = _rel_words(text)
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
        s = _rel_score(b, turn, text, entities, recent, hits, intents)
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
            b["ts"] = now_iso()
            if turn:
                _add_anchor(b, {"turn": turn, "role": "recalled"})
            recalled.append(key)
        elif (not live) and st == "now" and "explicit" not in s["signals"]:
            b["state"] = "parked"
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


_ELEMENT_JS = Path(__file__).parent / "canvas_element.js"


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
             "canvas.update", "canvas.move", "canvas.remove", "canvas.delete",
             "canvas.block_types",
             "canvas.session.resolve", "canvas.add", "canvas.pin", "canvas.park", "canvas.size",
             "canvas.recall", "canvas.timeline", "canvas.session.room", "canvas.session.relevance", "canvas.ask"],
    mode="tab",
    tab_order=60,
)
