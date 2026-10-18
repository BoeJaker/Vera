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

# what a full item returns to, and what a window gives when it is sent back (WSU-58 S7: kept on the record, so a second
# browser restores a maximised item and a kept window as the first one had them; see face_state below)
BACKS = ("pill", "tile", "window")
DOCKS = ("tile", "pill")

# the fields canvas.item.set may change, one call, one event (Decision 2; back and dock: WSU-58 S7)
SETTABLE = ("face", "place", "view", "size", "locked", "folded", "group", "state", "back", "dock")

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
    """The face a block shows: a parked item is a pill on the shelf whatever its record says (WSU-58 S2: parking
    wins over a stored face); otherwise its own face when it has one, else a tile."""
    if block.get("state") == "parked":
        return "pill"
    f = str(block.get("face") or "")
    if f in FACES:
        return f
    return "tile"


def face_for_state(block: Dict[str, Any], state: str) -> Optional[str]:
    """The face a move of `block` to `state` gives, as S2's transition has it, or None when the face stays:
    'park' -> a pill; out of the shelf (parked or hidden) back to now / pinned ('unpark', 'pin') -> a tile."""
    prev = block.get("state") if block.get("state") in STATES else "now"
    if state == "parked" and prev != "parked":
        return "pill"
    if prev in ("parked", "hidden") and state in ("now", "pinned"):
        return "tile"
    return None


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


_GROUP_BAD = "group must be {id?, front?, at?, sep?} naming an id, sep or at - or {} for the surface's own grouping"


def norm_group(g: Any) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """The item's composite (WSU-58 S7): {} = the surface groups it as it does (no choice of the person's);
    id = the item the person joined it to (a card dropped on another, LV-11); sep = the person took it out of its group;
    at = when its tab was last brought to the front (ms; the latest in a group is in front); front = it was, as written."""
    if g is None or g == {} or g == "":
        return {}, []
    if not isinstance(g, dict):
        return None, [_GROUP_BAD]
    gid = str(g.get("id") or "").strip()[:80]
    sep = bool(g.get("sep"))
    at = None
    if g.get("at") not in (None, ""):
        at = _num(g.get("at"))
        if at is None:
            return None, ["group.at must be a number (ms)"]
    if not gid and not sep and at is None:
        return None, [_GROUP_BAD]
    out: Dict[str, Any] = {}
    if gid and not sep:
        out["id"] = gid
    out["front"] = bool(g.get("front"))
    if at is not None:
        out["at"] = int(round(max(0.0, at)))
    if sep:
        out["sep"] = True
    return out, []


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
    face = face_of({"face": b.get("face"), "state": state})
    # back only on a full item; dock only on a window (or a full item that came from one) - as face_state reads them
    back = b.get("back") if face == "full" and b.get("back") in BACKS else ""
    dock = b.get("dock") if (face == "window" or (face == "full" and back == "window")) and b.get("dock") in DOCKS else ""
    return {
        "key": str(b.get("key") or b.get("id") or ""),
        "id": b.get("id"),
        "kind": str(b.get("type") or b.get("kind") or "note"),
        "content": b.get("content") if isinstance(b.get("content"), dict) else {},
        "source": src or {},
        "anchors": anchors,
        "state": state,
        "face": face,
        "back": back,
        "dock": dock,
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
        elif k in ("back", "dock"):
            ok = BACKS if k == "back" else DOCKS
            if v != "" and v not in ok:                  # "" clears it (a full item restored, a window sent back)
                probs.append(f"{k} must be one of {'|'.join(ok)} or ''")
            else:
                want[k] = v
    if probs:
        return {}, {}, probs
    # a tile or window asked of a parked item brings it back into the now band: the shelf only holds pills
    if want.get("face") in ("tile", "window", "full") and "state" not in want and block.get("state") == "parked":
        want["state"] = "now"
    # a state move with no face asked takes the face the move gives (park -> pill, unpark / pin off the shelf -> tile)
    if "state" in want and "face" not in want:
        f = face_for_state(block, want["state"])
        if f:
            want["face"] = f
    changed: Dict[str, Any] = {}
    prev: Dict[str, Any] = {}
    face0 = face_of(block)                   # read before the state moves, so prev (the undo) holds the face it had
    for k, v in want.items():
        old = block.get(k)
        if k == "face":
            old = face0
        if old != v:
            prev[k] = old
            changed[k] = v
            block[k] = v
    return changed, prev, []


# ── how an item changes face (DEEP-29 11A.4 Decision 3; WSU-58 S2) ──────────────────────────────────────────────────
# One rule for every kind, read off what the desk does today (vera/canvas/canvas_element.js) and held to it by
# tests/test_item_faces.py, tests/test_item_faces.cjs (the element itself, every BLOCK kind, on a stub DOM) and the
# shared cases in tests/fixtures/item_face_cases.json. The JS mirror is VeraItem.transition.
#
# The face state of an item:
#   face    pill | tile | window | full
#   state   now | pinned | parked | hidden        (parked = a pill on the shelf; hidden = not drawn)
#   locked  the person locked it: the layout moves it no more, and it is not dragged, resized, pilled or unpilled
#   back    for a full item, the face Esc / restore returns to (pill | tile | window)
#   dock    for a window (or a full item that came from one), what sending it back gives: 'tile' for a window the
#           desk made when a resize had no room (window mode), 'pill' for a pill opened as a window (WSU-55)
#   pilled  the PERSON made it a pill (its pill control); a pill the layout made (far from the chat, the topic moved
#           past it) is not pilled, and a click opens it as a window rather than back into the layout
#
# Owner O2 (2026-10-17): a window STAYS until it is sent back (its dock control, Esc, the pill control, a drag to the
# pills, or a resize that lets it fit) - blur and focus never change a face.

FACE_EVENTS = ("pill", "unpill", "open", "max", "restore", "send_back", "drag", "resize_end",
               "lock", "unlock", "park", "unpark", "pin", "unpin", "focus", "blur")
LAYOUT_EVENTS = ("no_room", "to_border", "from_border")
EVENTS = FACE_EVENTS + LAYOUT_EVENTS
_BACK = BACKS
_DOCK = DOCKS


def face_state(item: Dict[str, Any]) -> Dict[str, Any]:
    """The face state of an item record (or a stored block): the six fields transition() reads and returns."""
    it = item if isinstance(item, dict) else {}
    state = it.get("state") if it.get("state") in STATES else "now"
    face = face_of(it)
    if state == "parked":
        face = "pill"
    place = it.get("place") if isinstance(it.get("place"), dict) else {}
    locked = bool(it.get("locked") or place.get("locked"))
    back = it.get("back") if it.get("back") in _BACK else ""
    dock = it.get("dock") if it.get("dock") in _DOCK else ""
    if face == "full":
        back = back or "tile"
    else:
        back = ""
    if not (face == "window" or (face == "full" and back == "window")):
        dock = ""
    else:
        dock = dock or "tile"
    if "pilled" in it:
        pilled = bool(it.get("pilled"))
    else:
        pilled = (face == "pill" or (face == "full" and back == "pill")) and state != "parked"
    return {"face": face, "state": state, "locked": locked, "back": back, "dock": dock, "pilled": pilled}


def _event(ev: Any) -> Dict[str, Any]:
    if isinstance(ev, dict):
        return {"type": str(ev.get("type") or ev.get("event") or ""), "to": str(ev.get("to") or "desk"),
                "arrange": str(ev.get("arrange") or "")}
    return {"type": str(ev or ""), "to": "desk", "arrange": ""}


def _send_back(s: Dict[str, Any]) -> None:
    """full -> the face it came from; a window -> what it docks to."""
    if s["face"] == "full":
        s["face"], s["back"] = s["back"] or "tile", ""
        if s["face"] != "window":
            s["dock"] = ""
    elif s["face"] == "window":
        s["face"], s["dock"] = s["dock"] or "tile", ""


def transition(item: Dict[str, Any], event: Any) -> Dict[str, Any]:
    """The face state an item has after an event. Pure: the item is not changed. An event that does not apply (a locked
    item asked to move, a parked one to fill the desk, a blur) gives the face state back as it was.
    event: one of EVENTS, or {type, to: 'desk'|'pills' (drag), arrange: 'slots' (open)}."""
    s = face_state(item)
    e = _event(event)
    t = e["type"]
    if s["state"] in ("parked", "hidden"):
        # on the shelf (or not drawn): it comes back, or it is pinned back; nothing else reaches it
        if t == "unpark" or (t == "pin" and s["state"] == "parked"):
            s.update(state="pinned" if t == "pin" else "now", face="tile", back="", dock="", pilled=False)
        return s
    if t == "lock":
        s["locked"] = True
    elif t == "unlock":
        s["locked"] = False
    elif t == "pin":
        s["state"] = "pinned"
    elif t == "unpin":
        if s["state"] == "pinned":
            s["state"] = "now"
    elif t == "park":
        s.update(state="parked", face="pill", back="", dock="", pilled=False)
    elif t == "unpark":
        pass
    elif t == "max":
        if s["face"] == "full":
            _send_back(s)
        else:
            s["back"], s["face"] = s["face"], "full"
    elif t in ("restore", "send_back"):
        if s["face"] == "window" or (t == "restore" and s["face"] == "full"):
            _send_back(s)
    elif t in ("focus", "blur"):
        pass                                     # O2: a window stays until it is sent back
    elif s["locked"]:
        pass                                     # locked: no pill / unpill / open / drag / resize, nothing from the layout
    elif t == "pill":
        if s["face"] == "full" and s["back"] == "window" and s["dock"] == "pill":
            s.update(back="pill", dock="")
        elif s["face"] == "full":
            s.update(back="pill", dock="", pilled=True)
        elif s["face"] == "window" and s["dock"] == "pill":
            s.update(face="pill", dock="")       # a pill opened as a window closes back to the layout's pill
        else:
            s.update(face="pill", dock="", pilled=True)
    elif t == "unpill":
        if s["face"] == "pill":
            s.update(face="tile", pilled=False)
        elif s["face"] == "full" and s["back"] == "pill":
            s.update(back="tile", pilled=False)
    elif t == "open":
        if s["face"] == "pill":
            if s["pilled"] or e["arrange"] == "slots":
                s.update(face="tile", pilled=False)
            else:
                s.update(face="window", dock="pill")
    elif t == "drag":
        if s["face"] == "full":
            _send_back(s)                        # carried out of full: it goes where it is let go
        if e["to"] == "pills":
            s.update(face="pill", dock="")
        elif s["face"] == "pill":
            s.update(face="tile", pilled=False)
        elif s["face"] == "window" and s["dock"] == "pill":
            s.update(face="tile", dock="")
    elif t == "resize_end":
        if s["face"] == "window" and s["dock"] == "tile":
            s.update(face="tile", dock="")       # decided again by the layout (no_room makes it a window again)
    elif t == "no_room":
        if s["face"] == "tile":
            s.update(face="window", dock="tile")
    elif t == "to_border":
        if s["face"] == "tile":
            s.update(face="pill", pilled=False)
    elif t == "from_border":
        if s["face"] == "pill" and not s["pilled"]:
            s["face"] = "tile"
    return s
