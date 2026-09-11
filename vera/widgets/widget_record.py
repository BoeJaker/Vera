# -*- coding: utf-8 -*-
"""
The widget record (UI redesign, Notes/40 section 3; the WidgetSpec and Sizes
boards): one schema for every widget, every size, every placement - and the
catalogue data the records point into (shapes, forms, the size ladder).

Pure: no orchestrator import, no I/O. The widget registry (widget_registry.py)
normalises its templates through here, the catalogue capabilities
(widget_catalog.py) validate and resolve through here, and the element and the
chat's reply block read the same forms table - one implementation.

TWO SHAPES OF RECORD ARE ACCEPTED, ONE IS STORED.
  - the TEMPLATE shape the registry has used since the widget-record-registry
    slice: {id, name, form, reads:{cap,args,every,note}, frame:<str>,
    draw:{form,size,motion}, can[], placed[], source, version, tags};
  - the FULL record the WidgetSpec board writes: {id, form, projection,
    source, shape, title, read:{refresh,window,map,args}, frame:{size,span,
    caption,legend,motion,deep_dive,max_body}, draw:{...form options}, skin,
    actions[], place, panel, policy:{agent}}, plus the composite record
    (subject, layout, slots, children[{slot, record|id}], text);
  - the SHORT form a widget fence or a directive writes: window, size, refresh
    at the top level - sugar for read.* and frame.*.
normalise() takes any of them and returns the full record; to_template() turns
a full record back into the template shape so nothing that reads the old one
breaks; normalise_template() and template_problems() are the registry's old
_normalise() and problems(), moved here unchanged in behaviour.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# ── the vocabulary ────────────────────────────────────────────────────────────
SHAPES = ("level", "series", "values", "events", "graph", "items", "stages", "rate", "parts",
          "ohlcv", "matrix", "calendar", "string", "points", "panel", "composite")
SIZES = ("xs", "s", "m", "l", "xl")
PROJECTIONS = ("flat", "iso")
LAYOUTS = ("2x2", "rows", "report", "rail", "grid")
PLACES = ("dashboard", "canvas", "rail", "notebook", "chat", "ops")
POLICIES = ("drive", "ask", "never")
ACTIONS = ("dive", "pin", "ask", "ops", "print", "mute")
REFRESH_RE = re.compile(r"^(live|event|\d+(\.\d+)?(ms|s|m|h))$")

# a size is a COMPOSITION, not a scale (the Sizes board)
COMPOSITIONS = {
    "xs": "glyph + figure inline",
    "s": "chip, or a row on a graph node",
    "m": "a cell (iso begins here)",
    "l": "a cell with its detail list beside it",
    "xl": "a panel with its table, its log and its actions",
}

# forms that need no source (chrome, containers, the panel form reads a panel id)
NO_SOURCE_FORMS = ("header", "button", "rail", "controls", "panel", "composite", "announcement", "links", "form")

# ── the forms (the Widgets · WidgetsMotion · WidgetsIso galleries and the reply's own) ──
# id · shape · projections · glyph · motion; sizes default to all five unless the form says otherwise.
# The chat draws nine of these in a reply today (trace, radial, thermo, heat, log, lane, pipes, table, files).
_F = lambda id, shape, proj=("flat",), glyph="", motion=False, sizes=SIZES, options=(): {  # noqa: E731
    "id": id, "shape": shape, "proj": list(proj), "glyph": glyph or id, "motion": bool(motion),
    "sizes": list(sizes), "options": list(options)}
FORMS: List[Dict[str, Any]] = [
    # levels and rates
    _F("counter", "level", glyph="123", options=("unit", "delta")),
    _F("hero", "level", glyph="hero", options=("unit", "delta", "trend")),                     # hero + trend
    _F("level", "level", glyph="level", options=("min", "max", "bands")),
    _F("meter", "level", ("flat", "iso"), glyph="meter", options=("min", "max", "unit", "delta")),   # meter + delta
    _F("gauge", "level", glyph="arc", options=("min", "max", "bands")),                       # arc gauges
    _F("radial", "level", ("flat", "iso"), glyph="ring", options=("min", "max", "unit")),
    _F("ring", "level", glyph="ring", options=("min", "max")),                                # progress ring
    _F("threshold", "level", glyph="scale", options=("bands",)),                              # threshold scale
    _F("bullet", "values", glyph="bullet", options=("target", "bands")),
    _F("rate", "rate", glyph="rate", options=("unit", "window")),
    _F("meter-panel", "values", glyph="meters", options=("unit",)),                           # meter panel
    # series
    _F("trace", "series", glyph="spark", options=("palette", "bands", "fill")),               # sparkline / trace
    _F("sparkline", "series", glyph="spark", options=("fill",)),
    _F("line", "series", glyph="line", options=("palette", "legend")),
    _F("area", "series", glyph="area", options=("palette", "stacked")),                       # stacked area
    _F("step", "series", glyph="step"),                                                        # step chart
    _F("slope", "series", glyph="slope"),
    _F("horizon", "series", glyph="horizon", options=("bands",)),
    _F("bump", "series", glyph="bump"),
    _F("small-multiples", "series", glyph="multiples", options=("cols",)),
    _F("candles", "ohlcv", ("flat", "iso"), glyph="ohlc", options=("volume",)),               # candlestick
    _F("scope", "series", glyph="scope", motion=True),                                          # a trace with a sweep head
    # values (one number per key)
    _F("bars", "values", glyph="bars", options=("sort", "limit")),                            # column / ranked bars
    _F("column", "values", glyph="column"),
    _F("ranked", "values", glyph="ranked", options=("limit",)),
    _F("stacked-bar", "parts", glyph="stacked"),
    _F("diverging", "values", glyph="diverging"),
    _F("lollipop", "values", glyph="lollipop"),
    _F("histogram", "values", glyph="hist", options=("bins",)),
    _F("waterfall", "values", glyph="waterfall"),
    _F("pareto", "values", glyph="pareto"),
    _F("box", "values", glyph="box"),                                                          # box plot
    _F("radar", "values", glyph="radar"),
    _F("thermo", "values", ("flat", "iso"), glyph="thermo", options=("unit", "max")),
    _F("heat", "matrix", ("flat", "iso"), glyph="heat", options=("palette",)),                 # heat map
    _F("matrix", "matrix", glyph="matrix"),                                                    # status matrix
    _F("dots", "matrix", glyph="dots"),                                                        # dot matrix
    _F("waffle", "parts", ("flat", "iso"), glyph="waffle"),
    _F("pills", "values", glyph="pills"),                                                      # status pills
    _F("numbers", "values", glyph="grid"),                                                     # number grid
    _F("rings", "values", glyph="rings"),                                                      # activity rings
    _F("spark-table", "items", glyph="sparktable"),
    # parts of a whole
    _F("donut", "parts", glyph="donut", options=("legend",)),
    _F("treemap", "parts", ("flat", "iso"), glyph="treemap"),
    _F("funnel", "stages", ("flat", "iso"), glyph="funnel"),
    _F("stacks", "parts", glyph="stacks", motion=True),
    # stages and time
    _F("stepper", "stages", glyph="stepper"),
    _F("gantt", "stages", ("flat", "iso"), glyph="gantt"),
    _F("pipeline", "stages", glyph="pipeline"),                                                # pipeline progress
    _F("conveyor", "stages", glyph="conveyor", motion=True),
    _F("timeline", "events", glyph="timeline"),
    _F("calendar", "calendar", ("flat", "iso"), glyph="calendar"),
    _F("agenda", "calendar", ("flat", "iso"), glyph="agenda"),
    _F("comet", "events", glyph="comet", motion=True),                                          # a day as a ring
    # events
    _F("log", "events", ("flat", "iso"), glyph="log", options=("lanes", "limit")),
    _F("lane", "events", glyph="lane", options=("limit",)),
    _F("feed", "events", ("flat", "iso"), glyph="feed"),
    _F("pulse", "events", glyph="pulse", motion=True),
    # graphs
    _F("graph", "graph", glyph="graph", options=("layout",)),                                 # node graph
    _F("minigraph", "graph", glyph="minigraph"),
    _F("flow", "graph", glyph="flow"),
    _F("pipes", "graph", ("flat", "iso"), glyph="pipes"),
    _F("topology", "graph", ("flat", "iso"), glyph="topology"),
    _F("city", "items", ("iso",), glyph="city"),                                              # nodes as a city
    _F("orbit", "items", glyph="orbit", motion=True),
    # items
    _F("table", "items", ("flat", "iso"), glyph="table", options=("columns", "sort", "limit")),
    _F("list", "items", glyph="list", options=("limit",)),
    _F("rows", "items", glyph="rows"),
    _F("cards", "items", glyph="cards"),                                                       # card stack
    _F("files", "items", ("flat", "iso"), glyph="files"),
    _F("tree", "items", glyph="tree"),
    _F("people", "items", glyph="people"),
    _F("gallery", "items", glyph="gallery"),
    _F("board", "items", glyph="board"),                                                       # tasks board
    _F("checklist", "items", glyph="check"),
    _F("carousel", "items", glyph="carousel"),
    _F("shelf", "items", glyph="shelf", motion=True),
    _F("stack", "items", glyph="stack", motion=True),
    _F("links", "items", glyph="links"),
    # strings and single things
    _F("string", "string", glyph="text"),
    _F("announcement", "string", glyph="notice"),
    _F("terminal", "string", ("flat", "iso"), glyph="terminal", sizes=("m", "l", "xl")),
    _F("split-flap", "string", glyph="flap", motion=True),
    _F("tank", "level", glyph="tank", motion=True),
    _F("turbine", "rate", glyph="turbine", motion=True),
    # points
    _F("scatter", "points", ("flat", "iso"), glyph="scatter", options=("x", "y", "size")),
    _F("globe", "points", glyph="globe", options=("layer", "page", "limit")),
    # the three the spec adds, and the chrome the registry already names
    _F("panel", "panel", glyph="panel", sizes=("l", "xl")),
    _F("form", "values", glyph="form", options=("fields",)),
    _F("composite", "composite", glyph="composite"),
    _F("iso", "items", ("iso",), glyph="iso"),
    _F("program", "stages", glyph="program"),
    _F("ask", "string", glyph="ask"),
    _F("node", "items", glyph="node"),
    _F("controls", "string", glyph="controls", sizes=("xs", "s")),
    _F("button", "string", glyph="button", sizes=("xs", "s")),
    _F("header", "string", glyph="header", sizes=("xs", "s")),
    _F("rail", "items", glyph="rail", sizes=("s",)),
    _F("chart", "series", glyph="chart"),
]
_FORM_BY_ID = {f["id"]: f for f in FORMS}
# what the boards and older records call a form → the catalogue's id
FORM_ALIASES = {"hero + trend": "hero", "arc gauges": "gauge", "progress ring": "ring", "stacked area": "area",
                "heat map": "heat", "status matrix": "matrix", "dot matrix": "dots", "status pills": "pills",
                "number grid": "numbers", "activity rings": "rings", "spark table": "spark-table", "box plot": "box",
                "step chart": "step", "ranked bars": "ranked", "meter + delta": "meter", "bullet bars": "bullet",
                "stacked bar": "stacked-bar", "log stream": "log", "node graph": "graph", "mini graph": "minigraph",
                "card stack": "cards", "temp list": "thermo", "status strip": "pills", "meter panel": "meter-panel",
                "candlestick": "candles", "tasks": "board", "threshold scale": "threshold", "small multiples": "small-multiples",
                "iso columns": "bars", "iso tiles": "heat", "iso blocks": "treemap", "iso cubes": "waffle",
                "iso terraces": "thermo", "iso floors + pipes": "pipes", "galaxy": "graph", "bar": "bars"}


def form_ids() -> List[str]:
    return [f["id"] for f in FORMS]


def form(id_or_name: str) -> Optional[Dict[str, Any]]:
    k = str(id_or_name or "").strip().lower()
    return _FORM_BY_ID.get(k) or _FORM_BY_ID.get(FORM_ALIASES.get(k, ""))


def size_for_span(w: int, h: int = 1) -> str:
    """VeraDash's 12-column grid: 2-3 wide S, 4 M, 6 L, 8-12 XL; extra rows add detail then the table."""
    w = int(w or 0)
    if w <= 1:
        return "xs"
    if w <= 3:
        return "s"
    if w <= 4:
        return "m"
    if w <= 6:
        return "l" if int(h or 1) < 3 else "xl"
    return "xl"


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")
    return s[:64] or "widget"


def _size(v: Any, default: str = "m") -> str:
    s = str(v or "").strip().lower()
    return s if s in SIZES else default


# ── the template shape (the registry's, unchanged) ────────────────────────────
def normalise_template(t: Dict[str, Any]) -> Dict[str, Any]:
    """One shape for every template, whatever came in (the registry's original _normalise)."""
    reads = t.get("reads") if isinstance(t.get("reads"), dict) else {"cap": str(t.get("reads") or "")}
    draw = t.get("draw") if isinstance(t.get("draw"), dict) else {"form": str(t.get("draw") or "")}
    form_ = str(t.get("form") or draw.get("form") or "panel").strip().lower()
    can = t.get("can") if isinstance(t.get("can"), list) else [s.strip() for s in str(t.get("can") or "").split(".") if s.strip()]
    placed = t.get("placed") if isinstance(t.get("placed"), list) else []
    placed = [str(x.get("where") if isinstance(x, dict) else x)[:32] for x in placed if x]
    name = str(t.get("name") or "").strip()[:120]          # empty stays empty: template_problems() refuses a nameless record
    return {
        "id": str(t.get("id") or _slug(name or "widget"))[:80],
        "name": name,
        "form": form_[:24],
        "reads": {"cap": str(reads.get("cap") or "")[:120], "args": reads.get("args") if isinstance(reads.get("args"), dict) else {},
                  "every": str(reads.get("every") or "")[:24], "note": str(reads.get("note") or "")[:200]},
        "frame": str(t.get("frame") or "")[:300] if not isinstance(t.get("frame"), dict) else str(t["frame"].get("caption") or "")[:300],
        "draw": {"form": str(draw.get("form") or form_)[:24], "size": str(draw.get("size") or "M")[:4], "motion": str(draw.get("motion") or "")[:40]},
        "can": [str(c)[:60] for c in can][:16],
        "placed": placed[:16],
        "source": t.get("source") if isinstance(t.get("source"), dict) else {"origin": "you", "from": "", "panel": ""},
        "version": int(t.get("version") or 1),
        "tags": [str(x)[:32] for x in (t.get("tags") or []) if x][:12],
        "created_at": str(t.get("created_at") or ""),
        "updated_at": str(t.get("updated_at") or ""),
    }


def template_problems(t: Dict[str, Any], forms: Tuple[str, ...], wheres: Tuple[str, ...]) -> List[str]:
    """What a save refuses (the registry's original problems()). `forms` are the registry's own; the catalogue's
    forms are accepted too, so a record the WidgetSpec board writes is a valid template."""
    out = []
    if not t.get("name"):
        out.append("a template needs a name")
    if t.get("form") not in forms and not form(t.get("form") or ""):
        out.append("unknown form %r (one of: %s)" % (t.get("form"), ", ".join(forms)))
    if not (t.get("reads") or {}).get("cap") and t.get("form") not in NO_SOURCE_FORMS:
        out.append("a %s widget must say what it reads (reads.cap)" % t.get("form"))
    for w in t.get("placed") or []:
        if w not in wheres and not w.startswith("harness") and not w.startswith("header"):
            out.append("unknown placement %r (one of: %s)" % (w, ", ".join(wheres)))
    return out


# ── the full record ───────────────────────────────────────────────────────────
def _is_template_shape(r: Dict[str, Any]) -> bool:
    return isinstance(r.get("reads"), (dict, str)) or ("name" in r and "title" not in r) or isinstance(r.get("frame"), str)


def normalise(record: Any) -> Dict[str, Any]:
    """Any accepted shape → the full record the WidgetSpec board writes. Stored records are always the full one."""
    r = dict(record) if isinstance(record, dict) else {}
    legacy = _is_template_shape(r)
    reads = r.get("reads") if isinstance(r.get("reads"), dict) else ({"cap": r["reads"]} if isinstance(r.get("reads"), str) else {})
    read_in = r.get("read") if isinstance(r.get("read"), dict) else {}
    draw_in = r.get("draw") if isinstance(r.get("draw"), dict) else {}
    frame_in = r.get("frame") if isinstance(r.get("frame"), dict) else {}
    form_id = str(r.get("form") or draw_in.get("form") or "").strip().lower()
    f = form(form_id)
    if f:
        form_id = f["id"]
    title = str(r.get("title") or r.get("name") or "").strip()[:120]
    source = str(r.get("source") if isinstance(r.get("source"), str) else (reads.get("cap") or r.get("cap") or "")).strip()[:120]
    # the short form's top-level sugar
    refresh = str(read_in.get("refresh") or r.get("refresh") or reads.get("every") or "").strip()[:24]
    window = str(read_in.get("window") or r.get("window") or "").strip()[:24]
    size = _size(frame_in.get("size") or r.get("size") or draw_in.get("size"), "m")
    args = read_in.get("args") if isinstance(read_in.get("args"), dict) else (reads.get("args") if isinstance(reads.get("args"), dict) else (r.get("args") if isinstance(r.get("args"), dict) else {}))
    motion = frame_in.get("motion")
    if motion is None:
        motion = bool(draw_in.get("motion")) if legacy else (f["motion"] if f else False)
    span = frame_in.get("span") if isinstance(frame_in.get("span"), list) and len(frame_in.get("span")) == 2 else None
    if span and not (frame_in.get("size") or r.get("size")):
        size = size_for_span(span[0], span[1])
    draw = {k: v for k, v in draw_in.items() if k not in ("form", "size", "motion")}
    if isinstance(r.get("data"), (list, dict)):
        pass  # inline data rides beside the record (a fence with its own numbers); it is not part of the schema
    proj = str(r.get("projection") or draw_in.get("projection") or "").strip().lower()
    if proj not in PROJECTIONS:
        proj = "iso" if (f and f["proj"] == ["iso"]) else "flat"
    actions = r.get("actions") if isinstance(r.get("actions"), list) else (["dive", "pin", "ask"] if not legacy else [])
    policy = r.get("policy") if isinstance(r.get("policy"), dict) else {}
    out: Dict[str, Any] = {
        "id": str(r.get("id") or _slug(title or form_id or "widget"))[:80],
        "form": form_id[:24],
        "projection": proj,
        "source": source,
        "shape": str(r.get("shape") or (f["shape"] if f else "")).strip().lower()[:16],
        "title": title,
        "read": {"refresh": refresh, "window": window,
                 "map": read_in.get("map") if isinstance(read_in.get("map"), dict) else {}, "args": args},
        "frame": {"size": size, "span": span, "caption": bool(frame_in.get("caption", True)),
                  "legend": bool(frame_in.get("legend", False)), "motion": bool(motion),
                  "deep_dive": bool(frame_in.get("deep_dive", True)),
                  "max_body": int(frame_in.get("max_body") or 0) or None,
                  "note": str(r.get("frame") if isinstance(r.get("frame"), str) else frame_in.get("note") or "")[:300]},
        "draw": draw,
        "skin": str(r.get("skin") or "inherit")[:32],
        "actions": [str(a)[:24] for a in actions if str(a) in ACTIONS][:8],
        "place": str(r.get("place") or "")[:16] if str(r.get("place") or "") in PLACES else "",
        "panel": str(r.get("panel") or (r.get("source", {}) or {}).get("panel") if isinstance(r.get("source"), dict) else r.get("panel") or "")[:64],
        "policy": {"agent": str(policy.get("agent") or "ask") if str(policy.get("agent") or "ask") in POLICIES else "ask"},
    }
    if out["form"] == "panel" and not out["source"] and out["panel"]:
        out["source"] = "panel:" + out["panel"]
    if out["form"] == "panel" and out["source"].startswith("panel:") and not out["panel"]:
        out["panel"] = out["source"][6:]
    if isinstance(r.get("data"), (list, dict)):
        out["data"] = r["data"]
    # the composite record
    if out["form"] == "composite":
        layout = str(r.get("layout") or "grid").strip().lower()
        kids = []
        for c in (r.get("children") if isinstance(r.get("children"), list) else []):
            if not isinstance(c, dict):
                continue
            rec = c.get("record")
            kids.append({"slot": str(c.get("slot") or "")[:16],
                         "record": normalise(rec) if isinstance(rec, dict) else str(rec or "")[:80]})
        out.update({"subject": str(r.get("subject") or "")[:120], "layout": layout if layout in LAYOUTS else "grid",
                    "slots": r.get("slots") if isinstance(r.get("slots"), dict) else {}, "children": kids[:16],
                    "text": str(r.get("text") or "")[:2000]})
        out["shape"] = "composite"
    # the template shape's extras ride along so to_template() is lossless
    for k in ("can", "placed", "tags", "version", "created_at", "updated_at"):
        if k in r:
            out[k] = r[k]
    if isinstance(r.get("source"), dict):
        out["origin"] = r["source"]
    return out


def to_template(record: Dict[str, Any]) -> Dict[str, Any]:
    """The full record as the registry's template shape (what widget.template.* store and list)."""
    r = normalise(record)
    origin = r.get("origin") if isinstance(r.get("origin"), dict) else {"origin": "you", "from": "", "panel": r.get("panel") or ""}
    return normalise_template({
        "id": r["id"], "name": r["title"], "form": r["form"],
        "reads": {"cap": r["source"] if not r["source"].startswith("panel:") else "", "args": r["read"]["args"],
                  "every": r["read"]["refresh"], "note": (("window " + r["read"]["window"]) if r["read"]["window"] else "")},
        "frame": r["frame"].get("note") or "",
        "draw": {"form": r["form"], "size": r["frame"]["size"].upper(), "motion": "motion" if r["frame"]["motion"] else ""},
        "can": r.get("can") if isinstance(r.get("can"), list) else list(r.get("actions") or []),
        "placed": r.get("placed") if isinstance(r.get("placed"), list) else ([r["place"]] if r.get("place") else []),
        "source": origin, "version": r.get("version") or 1, "tags": r.get("tags") or [],
        "created_at": r.get("created_at") or "", "updated_at": r.get("updated_at") or "",
    })


def validate(record: Any, source_shape: Optional[str] = None) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Bind-time validation (the WidgetSpec board): the source's shape must equal the form's shape; unknown draw
    options are dropped with a warning; a missing source renders "no source" and the record stays editable.
    Returns (record, problems, warnings). `source_shape` is what the catalogue knows about the source, if anything."""
    r = normalise(record)
    problems: List[str] = []
    warnings: List[str] = []
    f = form(r["form"])
    if not r["form"]:
        problems.append("a widget needs a form")
    elif not f:
        problems.append("unknown form %r" % r["form"])
    if f:
        if not r["shape"]:
            r["shape"] = f["shape"]
        elif r["shape"] != f["shape"] and f["shape"] != "composite":
            problems.append("form %s draws %s, the record says %s" % (f["id"], f["shape"], r["shape"]))
        if r["projection"] not in f["proj"]:
            warnings.append("form %s has no %s projection; drawn %s" % (f["id"], r["projection"], f["proj"][0]))
            r["projection"] = f["proj"][0]
        if r["frame"]["size"] not in f["sizes"]:
            warnings.append("form %s has no %s size; drawn %s" % (f["id"], r["frame"]["size"], f["sizes"][-1]))
            r["frame"]["size"] = f["sizes"][-1]
        known = set(f["options"])
        dropped = [k for k in r["draw"] if k not in known]
        if dropped:
            warnings.append("draw options %s are not %s's (%s); dropped" % (", ".join(sorted(dropped)), f["id"], ", ".join(f["options"]) or "none"))
            r["draw"] = {k: v for k, v in r["draw"].items() if k in known}
    if not r["source"] and "data" not in r and r["form"] not in NO_SOURCE_FORMS:
        problems.append("no source")
    if source_shape and r["shape"] and source_shape != r["shape"] and r["form"] not in NO_SOURCE_FORMS:
        problems.append("source %s is %s, form %s wants %s" % (r["source"], source_shape, r["form"], r["shape"]))
    if r["read"]["refresh"] and not REFRESH_RE.match(r["read"]["refresh"]):
        warnings.append("refresh %r is not live | event | <n>s | <n>m | <n>h; ignored" % r["read"]["refresh"])
        r["read"]["refresh"] = ""
    if r["form"] == "composite":
        for c in r.get("children") or []:
            if isinstance(c.get("record"), dict):
                _, p2, w2 = validate(c["record"])
                problems += ["child %s: %s" % (c.get("slot") or "?", p) for p in p2]
                warnings += ["child %s: %s" % (c.get("slot") or "?", w) for w in w2]
    return r, problems, warnings


def composition(size: str) -> str:
    return COMPOSITIONS.get(_size(size, "m"), COMPOSITIONS["m"])
