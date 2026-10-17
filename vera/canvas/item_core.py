# -*- coding: utf-8 -*-
"""
The item record (DEEP-29 section 11A.4, Decision 2): one schema for a thing on
any surface - the desk/canvas, a dashboard grid, an LHM slot, a chat reply, a
notebook. A surface draws an item as one container (head + controls + resize +
faces) around one body; this module is the record that container reads and
writes, so the face, the rect, the view and the group live on the server and
the browser keeps them only as a cache.

Pure: no orchestrator import, no I/O. canvas_capabilities.py reads and writes
items through here (canvas.item.set, canvas.items); the JS mirror is
vera/ui/vera_item.js (VeraItem.normalise / VeraItem.applySet), and the two are
held to the same answers by tests/test_item_core.py and tests/test_vera_item.cjs.

A STORED BLOCK IS THE ITEM. The session canvas keeps keyed blocks
{id, type, key, state, size, content, anchor(s), ts, score, meta, layout}; an
item record is that block seen through the contract: `type` is the `kind`, and
the new fields (face, place, view, group, folded, source) are kept on the block
under the same names. Nothing about a block that predates them changes: a block
without a face has the face its state implies.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# the vocabulary (Decision 1-3)
STATES = ("now", "pinned", "parked", "hidden")
FACES = ("pill", "tile", "window", "full")
SIZES = ("xs", "s", "m", "l", "xl")
LAYERS = ("among", "behind", "front")
PLACED_BY = ("auto", "person")
# every view a body can offer (WSU-58 S1: the JSON lens's tree / flat / validate and an app's own drawn view were missing,
# so canvas.item.set refused them and the choice never reached the record)
VIEWS = ("list", "cards", "table", "graph", "chart", "chips", "fields", "code", "structure", "both", "tree", "flat", "validate", "drawn")

# the fields canvas.item.set may change, one call, one event (Decision 2)
SETTABLE = ("face", "place", "view", "size", "locked", "folded", "group", "state")

# the place rect's numeric bounds: fractions of the surface for fx/fy, pixels for w/h
_PLACE_NUM = {"fx": (0.0, 1.0), "fy": (0.0, 1.0), "w": (34.0, 8000.0), "h": (24.0, 8000.0), "z": (0.0, 999.0)}


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def face_of(block: Dict[str, Any]) -> str:
    """The face a block shows: its own when it has one, else the one its state implies (a parked item is a pill
    on the shelf; everything else a tile)."""
    f = str(block.get("face") or "")
    if f in FACES:
        return f
    return "pill" if block.get("state") == "parked" else "tile"


def norm_place(p: Any, prev: Optional[Dict[str, Any]] = None) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """A place merged over the previous one: fractions clamped to 0..1, sizes to their bounds, the enums checked.
    Returns (place, problems); place is None when nothing valid was given."""
    if p is None:
        return None, []
    if not isinstance(p, dict):
        return None, ["place must be an object {fx, fy, w, h, by, locked, layer, z}"]
    out: Dict[str, Any] = dict(prev or {})
    bad: List[str] = []
    for k, (lo, hi) in _PLACE_NUM.items():
        if k in p:
            n = _num(p[k])
            if n is None:
                bad.append(f"place.{k} must be a number")
                continue
            n = min(hi, max(lo, n))
            out[k] = int(round(n)) if k in ("w", "h", "z") else round(n, 4)
    if "by" in p:
        if p["by"] in PLACED_BY:
            out["by"] = p["by"]
        else:
            bad.append(f"place.by must be one of {'|'.join(PLACED_BY)}")
    if "layer" in p:
        if p["layer"] in LAYERS:
            out["layer"] = p["layer"]
        else:
            bad.append(f"place.layer must be one of {'|'.join(LAYERS)}")
    if "locked" in p:
        out["locked"] = bool(p["locked"])
    return (out if out else None), bad


def norm_group(g: Any) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    if g is None or g == {} or g == "":
        return {}, []
    if not isinstance(g, dict) or not str(g.get("id") or "").strip():
        return None, ["group must be {id, front?} or {} to separate"]
    return {"id": str(g["id"]).strip()[:80], "front": bool(g.get("front"))}, []


def normalise(block: Dict[str, Any]) -> Dict[str, Any]:
    """The item record of a stored block (Decision 2's shape). Missing fields take their defaults; unknown
    values fall back rather than fail, so an old or hand-written block always reads."""
    b = block if isinstance(block, dict) else {}
    place, _ = norm_place(b.get("place") if isinstance(b.get("place"), dict) else None)
    group, _ = norm_group(b.get("group") if isinstance(b.get("group"), dict) else None)
    size = str(b.get("size") or "m")
    view = str(b.get("view") or "")
    src = b.get("source") if isinstance(b.get("source"), dict) else None
    if src is None and b.get("type") == "result" and isinstance(b.get("content"), dict) and b["content"].get("cap"):
        c = b["content"]
        src = {"cap": c.get("cap"), "args": c.get("args") or {}, "ts": c.get("ts") or "", "ms": c.get("ms") or 0}
    anchors = b.get("anchors") if isinstance(b.get("anchors"), list) else ([b["anchor"]] if isinstance(b.get("anchor"), dict) else [])
    state = b.get("state") if b.get("state") in STATES else "now"
    return {
        "key": str(b.get("key") or b.get("id") or ""),
        "id": b.get("id"),
        "kind": str(b.get("type") or b.get("kind") or "note"),
        "content": b.get("content") if isinstance(b.get("content"), dict) else {},
        "source": src or {},
        "anchors": anchors,
        "state": state,
        "face": face_of({"face": b.get("face"), "state": state}),
        "size": size if size in SIZES else "m",
        "place": place or {},
        "locked": bool((place or {}).get("locked")),
        "view": view if view in VIEWS else "",
        "group": group or {},
        "folded": bool(b.get("folded")),
        "ts": b.get("ts") or "",
        "score": b.get("score"),
    }


def apply_set(block: Dict[str, Any], fields: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """Validate a canvas.item.set call against a block and apply it in place.
    Returns (changed, prev, problems): changed = {field: new value} for every field that moved, prev the values they
    had, so one call is undoable. Nothing is written when there are problems."""
    probs: List[str] = []
    want: Dict[str, Any] = {}
    for k, v in (fields or {}).items():
        if v is None or k not in SETTABLE:
            continue
        if k == "face":
            if v not in FACES:
                probs.append(f"face must be one of {'|'.join(FACES)}")
            else:
                want["face"] = v
        elif k == "state":
            if v not in STATES:
                probs.append(f"state must be one of {'|'.join(STATES)}")
            else:
                want["state"] = v
        elif k == "size":
            s = str(v).lower()
            if s not in SIZES:
                probs.append(f"size must be one of {'|'.join(SIZES)}")
            else:
                want["size"] = s
        elif k == "view":
            if v not in VIEWS:
                probs.append(f"view must be one of {'|'.join(VIEWS)}")
            else:
                want["view"] = v
        elif k == "place":
            p, bad = norm_place(v, block.get("place") if isinstance(block.get("place"), dict) else None)
            probs += bad
            if p is not None:
                want["place"] = p
        elif k == "locked":
            p = dict(block.get("place") or {}) if isinstance(block.get("place"), dict) else {}
            p.update(want.get("place") or {})
            p["locked"] = bool(v)
            want["place"] = p
        elif k == "folded":
            want["folded"] = bool(v)
        elif k == "group":
            g, bad = norm_group(v)
            probs += bad
            if g is not None:
                want["group"] = g
    if probs:
        return {}, {}, probs
    # a tile or window asked of a parked item brings it back into the now band: the shelf only holds pills
    if want.get("face") in ("tile", "window", "full") and "state" not in want and block.get("state") == "parked":
        want["state"] = "now"
    changed: Dict[str, Any] = {}
    prev: Dict[str, Any] = {}
    for k, v in want.items():
        old = block.get(k)
        if k == "face":
            old = face_of(block)
        if old != v:
            prev[k] = old
            changed[k] = v
            block[k] = v
    return changed, prev, []
