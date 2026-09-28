# -*- coding: utf-8 -*-
"""
Dashboard layouts: the legacy VeraDash state as the layout record (UI redesign
M5, Notes/40 section 4; the Dashboard board).

VeraDash persisted ``vera.dash.<key>`` as ``{order, hidden, sizes, dynamic}``:
the tile ids in their order, the hidden ones, ``{wid: {w, h}}`` for a resized
tile, and the loader's dynamic widgets (``{panelId, wid}`` for a registered
panel, ``{record, wid}`` for a record tile). The layout record is
``{v: 2, dashboard, layout, key, user, grid {cols 12, row 58, gap 10, widths},
widgets: [{record, at, span, hidden, refresh, floated}]}`` - a tile is a record
placed at a position with a span. The rule, one to one:

    order   -> the widgets' order, then at = [col, row] by dense flow
    sizes   -> span (a tile the state never resized keeps its markup / file span)
    hidden  -> hidden
    dynamic -> a panel record ({form: panel, panel, source: panel:<id>}) or the
               record the tile carried, with the wid as its id

``record`` is the tile's id when the grid's layout file defines it (the page's
own tiles) and the record itself when the tile was added by the user - or when
the user changed a page or file tile's record through its gear (the WidgetConfig
surface): the edited record rides inline and the tile carries ``edited: true``,
so the file's record is what a reset returns to. A legacy
``vera:wol-layout:*`` (the workers page's pre-VeraDash framework) is ignored.

Pure, no I/O in the functions; ``VeraDash.migrate`` in vera/chat/vera-dashboard.js
is the same rule and the tests hold the two to one fixture. The state lives in
each browser's localStorage, so the one-off runs per user, per key on a dump of
it: ``python migrate_layouts.py <dump.json> [-o out.json] [--layouts DIR]`` where
the dump is ``{"vera.dash.main": {...}, ...}`` (what a page's
``JSON.stringify(localStorage)`` gives) - every ``vera.dash.<key>`` entry comes
out as a layout record, entries already in the new shape pass through, and the
grid's layout file (``layouts/<key>.json``) supplies the page tiles when present.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

GRID: Dict[str, Any] = {"cols": 12, "row": 58, "gap": 10, "widths": [2, 3, 4, 6, 8, 12]}
LEGACY_KEY = re.compile(r"^vera\.dash\.([A-Za-z0-9_-]+)$")
SPAN_BY_SIZE = {"xs": [2, 1], "s": [2, 1], "m": [4, 2], "l": [6, 3], "xl": [8, 4]}


def size_for_span(w: int, h: int = 1) -> str:
    """The Sizes board's rule (widget_record.size_for_span, repeated here so this module stays import-free):
    a 2-3 wide tile one row tall is a row (S); two rows or more is a cell (M)."""
    w = int(w or 0)
    if w <= 1:
        return "xs"
    if w <= 3:
        return "s" if int(h or 1) < 2 else "m"
    if w <= 4:
        return "m"
    if w <= 6:
        return "l" if int(h or 1) < 3 else "xl"
    return "xl"


def span_for(record: Dict[str, Any]) -> List[int]:
    """The span a record asks for: its frame.span, else its size's cell; a panel is the loader's 6 x 3."""
    fr = record.get("frame") if isinstance(record.get("frame"), dict) else {}
    sp = fr.get("span")
    if isinstance(sp, list) and len(sp) == 2 and int(sp[0] or 0):
        return [int(sp[0]), int(sp[1] or 1)]
    if record.get("form") == "panel":
        return [6, 3]
    size = str(fr.get("size") or record.get("size") or (record.get("draw") or {}).get("size") if isinstance(record.get("draw"), dict) else fr.get("size") or record.get("size") or "m").lower()
    return list(SPAN_BY_SIZE.get(size, [4, 2]))


def panel_record(panel_id: str, wid: str = "", label: str = "") -> Dict[str, Any]:
    panel_id = str(panel_id or "")
    return {"id": wid or ("dyn-" + re.sub(r"[^a-zA-Z0-9_-]", "", panel_id)), "form": "panel", "panel": panel_id,
            "source": "panel:" + panel_id, "title": label or panel_id, "frame": {"size": "l", "span": [6, 3]}}


def flow(tiles: List[Dict[str, Any]], cols: int = 12) -> List[Dict[str, Any]]:
    """Dense flow: each visible tile takes the first cell (top row first, then left to right) where its span fits -
    the place a browser gives the same order in a 12-column auto-flow grid. at = [col, row]; hidden -> None."""
    cols = int(cols or 12)
    occ = set()
    out = []
    for t in tiles:
        o = dict(t)
        if t.get("hidden"):
            o["at"] = None
            out.append(o)
            continue
        sp = t.get("span") if isinstance(t.get("span"), list) else [4, 1]
        w = min(cols, max(1, int(sp[0] or 4)))
        h = max(1, int(sp[1] or 1))
        placed = False
        r = 0
        while not placed and r < 10000:
            for c in range(0, cols - w + 1):
                if all((y, x) not in occ for y in range(r, r + h) for x in range(c, c + w)):
                    occ.update((y, x) for y in range(r, r + h) for x in range(c, c + w))
                    o["at"] = [c, r]
                    placed = True
                    break
            r += 1
        if not placed:
            o["at"] = [0, r]
        out.append(o)
    return out


def arrange(tiles: List[Dict[str, Any]], cols: int = 12) -> List[Dict[str, Any]]:
    """Arrange / compact: the tiles in the order dense flow packs them (row, then column); hidden ones last."""
    placed = flow(tiles, cols)
    vis = sorted((t for t in placed if t.get("at")), key=lambda t: (t["at"][1], t["at"][0]))
    return vis + [t for t in placed if not t.get("at")]


def is_legacy(state: Any) -> bool:
    return isinstance(state, dict) and not isinstance(state.get("widgets"), list)


def page_tiles(layout_file: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The page's own tiles as a layout file names them: [{id, span}] in the file's order."""
    out = []
    for t in (layout_file or {}).get("widgets") or []:
        if not isinstance(t, dict):
            continue
        r = t.get("record")
        wid = str(r.get("id") or "") if isinstance(r, dict) else str(r or "")
        if not wid:
            continue
        sp = t.get("span") if isinstance(t.get("span"), list) and len(t.get("span")) == 2 else (span_for(r) if isinstance(r, dict) else [4, 1])
        out.append({"id": wid, "span": [int(sp[0]), int(sp[1])]})
    return out


def migrate(key: str, legacy: Optional[Dict[str, Any]], layout_file: Optional[Dict[str, Any]] = None,
            page: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The legacy state of one grid -> its layout record. ``page`` (or the grid's layout file) lists the tiles the
    page draws itself, in the page's order; the legacy order names which of them the user arranged."""
    legacy = legacy if isinstance(legacy, dict) else {}
    page = page if page is not None else page_tiles(layout_file)
    order = [str(x) for x in legacy.get("order")] if isinstance(legacy.get("order"), list) else []
    hidden = set(str(x) for x in legacy.get("hidden")) if isinstance(legacy.get("hidden"), list) else set()
    sizes = legacy.get("sizes") if isinstance(legacy.get("sizes"), dict) else {}
    dynamic = legacy.get("dynamic") if isinstance(legacy.get("dynamic"), dict) else {}
    known: Dict[str, Dict[str, Any]] = {}
    for p in page:
        if isinstance(p, dict) and p.get("id"):
            known[str(p["id"])] = {"span": p.get("span")}
    for wid, dd in dynamic.items():
        dd = dd if isinstance(dd, dict) else {}
        rec = dd.get("record") if isinstance(dd.get("record"), dict) else None
        if rec:
            r = {"id": wid}
            r.update(rec)
            r["id"] = r.get("id") or wid
            known[str(wid)] = {"record": r}
        else:
            known[str(wid)] = {"record": panel_record(dd.get("panelId"), str(wid))}
    ids: List[str] = []
    for wid in order + [str(p.get("id")) for p in page if isinstance(p, dict)] + [str(k) for k in dynamic]:
        if wid and wid in known and wid not in ids:
            ids.append(wid)
    tiles = []
    for wid in ids:
        k = known[wid]
        sz = sizes.get(wid) if isinstance(sizes.get(wid), dict) else None
        if sz and int(sz.get("w") or 0):
            span = [int(sz["w"]), int(sz.get("h") or 1)]
        elif k.get("span"):
            span = [int(k["span"][0]), int(k["span"][1])]
        elif k.get("record"):
            span = span_for(k["record"])
        else:
            span = [4, 1]
        rec = k.get("record")
        refresh = ""
        if rec:
            rd = rec.get("read") if isinstance(rec.get("read"), dict) else {}
            refresh = str(rd.get("refresh") or rec.get("refresh") or "")
        tiles.append({"record": rec or wid, "at": None, "span": span, "hidden": wid in hidden, "refresh": refresh, "floated": False})
    return {"v": 2, "dashboard": (layout_file or {}).get("dashboard") or key, "layout": "default", "key": key, "user": "",
            "grid": {"cols": GRID["cols"], "row": GRID["row"], "gap": GRID["gap"], "widths": list(GRID["widths"])},
            "widgets": flow(tiles, GRID["cols"])}


def count_records(layout: Dict[str, Any]) -> int:
    """Every record a layout carries: the tiles plus the children of its composites."""
    n = 0
    for t in (layout or {}).get("widgets") or []:
        n += 1
        r = t.get("record") if isinstance(t, dict) else None
        if isinstance(r, dict):
            n += len([c for c in (r.get("children") or []) if isinstance(c, dict)])
    return n


def migrate_store(entries: Dict[str, Any], layouts: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    """A dump of one browser's localStorage -> {key: layout record} for every vera.dash.<key> entry. Values may be
    the JSON strings localStorage holds or the parsed objects; an entry already in the new shape passes through;
    vera:wol-layout:* and everything else is ignored."""
    layouts = layouts or {}
    out: Dict[str, Dict[str, Any]] = {}
    for name, val in (entries or {}).items():
        m = LEGACY_KEY.match(str(name))
        if not m:
            continue
        key = m.group(1)
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except Exception:
                continue
        if not isinstance(val, dict):
            continue
        out[key] = val if not is_legacy(val) else migrate(key, val, layouts.get(key))
    return out


def load_layout_dir(d: Path) -> Dict[str, Dict[str, Any]]:
    out = {}
    for p in sorted(Path(d).glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(rec, dict) and isinstance(rec.get("widgets"), list):
                out[p.stem] = rec
        except Exception:
            pass
    return out


def main(argv: List[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    src = Path(argv[0])
    out_path = None
    layouts_dir = Path(__file__).parent / "layouts"
    i = 1
    while i < len(argv):
        if argv[i] == "-o" and i + 1 < len(argv):
            out_path = Path(argv[i + 1]); i += 2
        elif argv[i] == "--layouts" and i + 1 < len(argv):
            layouts_dir = Path(argv[i + 1]); i += 2
        else:
            i += 1
    entries = json.loads(src.read_text(encoding="utf-8"))
    result = migrate_store(entries, load_layout_dir(layouts_dir) if layouts_dir.exists() else {})
    text = json.dumps(result, indent=1)
    if out_path:
        out_path.write_text(text, encoding="utf-8")
        print("wrote %s: %d layouts (%s)" % (out_path, len(result), ", ".join(sorted(result))))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
