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
import json
import time
import uuid

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    CAPABILITY_REGISTRY,
    capability,
    emit_event,
    now_iso,
)

_redis = _orch._redis

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
    "image":    {"desc": "An image (URL or data URI) with optional caption.",
                 "content": "{url:str, alt?:str, caption?:str}"},
    "note":     {"desc": "A short user/agent note or annotation.",
                 "content": "{text:str, author?:str}"},
    "table":    {"desc": "Tabular data.",
                 "content": "{columns:[str], rows:[[any]], caption?:str}"},
    "widget":   {"desc": "A defined Vera widget via the panel bridge (widget-level, "
                         "not a whole panel).",
                 "content": "{widget:str, args?:obj, title?:str}"},
    "session":  {"desc": "A live remote/SSH session or its results (e.g. an agent "
                         "connected into the estate).",
                 "content": "{session_id?:str, host?:str, command?:str, output?:str}"},
    "schedule": {"desc": "A scheduled item tied to this canvas, shown nicely.",
                 "content": "{when:str, what:str, action_id?:str}"},
    "html":     {"desc": "Raw HTML — ON-THE-FLY escape hatch; prefer a predefined "
                         "type so the UI stays consistent.",
                 "content": "{html:str}"},
}
CANVAS_MODES = ("dynamic", "static")


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
               "note": "text", "html": "html"}.get(btype, "text")
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
                "dynamic topic). Inputs: id (str!), block_id (str!), content (JSON), "
                "meta (JSON, optional).",
)
async def cap_canvas_update(id: str = "", block_id: str = "",
                            content: Any = None, meta: Any = None, trace_id=None):
    doc = await _load(id)
    if not doc:
        return {"error": f"unknown canvas: {id}"}
    if isinstance(content, str) and content.strip().startswith(("{", "[")):
        try:
            content = json.loads(content)
        except Exception:
            pass
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
    description="Remove a block from a canvas. Inputs: id (str!), block_id (str!).",
)
async def cap_canvas_remove(id: str = "", block_id: str = "", trace_id=None):
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
