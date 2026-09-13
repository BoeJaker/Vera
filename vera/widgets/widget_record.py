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
    source, shape, title, read:{refresh,window,map,args,range}, frame:{size,span,
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
# what a host calls its envelope (the surface's into:) → the record's placement name
PLACE_ALIASES = {"lhm": "rail", "side": "rail", "reply": "chat", "dash": "dashboard", "ops map": "ops", "iso plate": "canvas", "harness": "rail"}
# draw keys every form has (the WidgetConfig board's Drawing section), beside the form's own options
DRAW_COMMON = ("palette", "bands")
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
# id · shape · projections · glyph · motion · sizes · options · the board's name · the boards it is on. Every form the three
# boards draw is here under the board's own name, so a record can be written from the gallery and the sheet lists what
# the gallery shows; the chat's own nine (trace, radial, thermo, heat, log, lane, pipes, table, files) are among them.
# name: what the board calls the form (the sheet's row, the gallery's card); boards: which of the three galleries draw it
# (widgets · motion · iso), "spec" for the WidgetSpec/WidgetConfig additions, "reply" for the chat's own and the chrome.
_F = lambda id, shape, proj=("flat",), glyph="", motion=False, sizes=SIZES, options=(), name="", boards=(): {  # noqa: E731
    "id": id, "shape": shape, "proj": list(proj), "glyph": glyph or id, "motion": bool(motion),
    "sizes": list(sizes), "options": list(options), "name": name or id, "boards": list(boards)}
FORMS: List[Dict[str, Any]] = [
    # ── levels and rates ──
    _F("counter", "level", glyph="123", options=("unit", "delta", "digits"), name="Counter", boards=("widgets",)),
    _F("hero", "level", glyph="hero", options=("unit", "delta", "trend"), name="Hero + trend", boards=("widgets",)),
    _F("level", "level", glyph="level", options=("min", "max", "bands", "cells"), name="Level", boards=("widgets",)),
    _F("meter", "level", ("flat", "iso"), glyph="meter", options=("min", "max", "unit", "delta"), name="Meter + delta", boards=("widgets",)),
    _F("gauge", "level", glyph="arc", options=("min", "max", "bands"), name="Arc gauges", boards=("widgets",)),
    _F("radial", "level", ("flat", "iso"), glyph="ring", options=("min", "max", "unit"), name="Radial", boards=("motion", "spec")),
    _F("ring", "level", glyph="ring", options=("min", "max"), name="Progress ring", boards=("widgets",)),
    _F("threshold", "values", glyph="scale", options=("bands",), name="Threshold scale", boards=("widgets",)),
    _F("bullet", "values", glyph="bullet", options=("target", "bands"), name="Bullet bars", boards=("widgets",)),
    _F("rate", "rate", glyph="rate", options=("unit", "window"), name="Rate", boards=("spec",)),
    _F("meter-panel", "values", ("iso",), glyph="meters", motion=True, options=("unit", "columns", "studs", "hot"), name="Meter panel", boards=("motion",)),
    _F("dial", "level", ("iso",), glyph="dial", motion=True, options=("studs", "sweep", "bands"), name="Dial", boards=("motion",)),
    _F("tank", "level", ("iso",), glyph="tank", motion=True, options=("wave", "bubbles", "feed"), name="Tank", boards=("motion",)),
    _F("turbine", "rate", glyph="turbine", motion=True, options=("blades", "blur"), name="Turbine", boards=("motion",)),
    _F("ticker", "rate", glyph="ticker", motion=True, options=("bars",), name="Ticker", boards=("spec",)),
    # ── series ──
    _F("trace", "series", glyph="spark", options=("palette", "bands", "fill"), name="Trace", boards=("motion", "spec")),
    _F("sparkline", "series", glyph="spark", options=("fill",), name="Sparkline", boards=("reply",)),
    _F("line", "series", glyph="line", options=("palette", "legend"), name="Line", boards=("reply",)),
    _F("area", "series", ("flat", "iso"), glyph="area", options=("palette", "stacked", "order"), name="Stacked area", boards=("widgets",)),
    _F("step", "series", glyph="step", name="Step chart", boards=("widgets",)),
    _F("slope", "series", glyph="slope", name="Slope", boards=("widgets",)),
    _F("horizon", "series", glyph="horizon", options=("bands",), name="Horizon", boards=("widgets",)),
    _F("bump", "series", glyph="bump", options=("ranks",), name="Bump", boards=("widgets",)),
    _F("small-multiples", "series", ("flat", "iso"), glyph="multiples", options=("cols", "rows"), name="Small multiples", boards=("widgets",)),
    _F("candles", "ohlcv", ("flat", "iso"), glyph="ohlc", options=("volume", "bars"), name="Candlestick", boards=("widgets", "motion", "iso")),
    _F("scope", "series", glyph="scope", motion=True, options=("sweep", "ghost", "graticule"), name="Scope", boards=("motion",)),
    _F("chart", "series", glyph="chart", name="Chart", boards=("reply",)),
    # ── values (one number per key) ──
    _F("bars", "values", ("flat", "iso"), glyph="bars", options=("sort", "limit"), name="Bars", boards=("reply",)),
    _F("column", "values", ("flat", "iso"), glyph="column", options=("limit",), name="Column", boards=("widgets",)),
    _F("ranked", "values", glyph="ranked", options=("limit",), name="Ranked bars", boards=("widgets",)),
    _F("stacked-bar", "parts", glyph="stacked", name="Stacked bar", boards=("widgets",)),
    _F("diverging", "values", glyph="diverging", options=("centre",), name="Diverging", boards=("widgets",)),
    _F("lollipop", "values", glyph="lollipop", name="Lollipop", boards=("widgets",)),
    _F("histogram", "values", glyph="hist", options=("bins",), name="Histogram", boards=("widgets",)),
    _F("waterfall", "values", glyph="waterfall", name="Waterfall", boards=("widgets",)),
    _F("pareto", "values", glyph="pareto", options=("line",), name="Pareto", boards=("widgets",)),
    _F("box", "values", glyph="box", name="Box plot", boards=("widgets",)),
    _F("radar", "values", glyph="radar", name="Radar", boards=("widgets",)),
    _F("thermo", "values", ("flat", "iso"), glyph="thermo", motion=True, options=("unit", "max", "throttle", "scale"), name="Thermometers", boards=("motion", "reply")),
    _F("heat", "matrix", ("flat", "iso"), glyph="heat", options=("palette", "bands"), name="Heat map", boards=("widgets", "motion")),
    _F("matrix", "matrix", glyph="matrix", name="Status matrix", boards=("widgets",)),
    _F("dots", "matrix", glyph="dots", name="Dot matrix", boards=("widgets",)),
    _F("waffle", "parts", ("flat", "iso"), glyph="waffle", options=("cells",), name="Waffle", boards=("widgets",)),
    _F("pills", "values", glyph="pills", name="Status pills", boards=("widgets",)),
    _F("numbers", "values", glyph="grid", name="Number grid", boards=("widgets",)),
    _F("rings", "values", glyph="rings", name="Activity rings", boards=("widgets",)),
    _F("spark-table", "items", glyph="sparktable", options=("rows", "figure"), name="Spark table", boards=("widgets",)),
    _F("tabs", "matrix", glyph="tabs", name="Node health · tabs", boards=("widgets",)),
    _F("slider", "values", glyph="slider", options=("min", "max", "threshold"), name="Alert threshold · slider", boards=("widgets",)),
    _F("node", "values", glyph="node", name="Node composite", boards=("widgets", "reply")),
    _F("glance", "values", glyph="glance", options=("figures",), name="Glance", boards=("widgets",)),
    _F("compare", "values", glyph="compare", name="Compare", boards=("widgets",)),
    # ── parts of a whole ──
    _F("donut", "parts", glyph="donut", options=("legend", "gap"), name="Donut", boards=("widgets",)),
    _F("treemap", "parts", ("flat", "iso"), glyph="treemap", name="Treemap", boards=("widgets",)),
    _F("funnel", "stages", ("flat", "iso"), glyph="funnel", name="Funnel", boards=("widgets",)),
    _F("stacks", "parts", ("iso",), glyph="stacks", motion=True, options=("columns", "coin"), name="Coin stacks", boards=("motion",)),
    # ── stages and time ──
    _F("stepper", "stages", glyph="stepper", name="Stepper", boards=("widgets",)),
    _F("gantt", "stages", ("flat", "iso"), glyph="gantt", options=("rows",), name="Gantt", boards=("widgets",)),
    _F("pipeline", "stages", ("flat", "iso"), glyph="pipeline", name="Pipeline", boards=("widgets", "iso")),
    _F("conveyor", "stages", ("iso",), glyph="conveyor", motion=True, options=("belt", "work"), name="Conveyor", boards=("motion",)),
    _F("approvals", "stages", ("iso",), glyph="approvals", name="Approvals", boards=("iso",)),
    _F("timeline", "events", glyph="timeline", options=("now",), name="Timeline", boards=("widgets",)),
    _F("calendar", "calendar", ("flat", "iso"), glyph="calendar", options=("weeks",), name="Calendar", boards=("widgets", "iso")),
    _F("agenda", "calendar", ("flat", "iso"), glyph="agenda", options=("window", "now"), name="Agenda", boards=("widgets", "motion")),
    _F("comet", "events", glyph="comet", motion=True, options=("window", "hours"), name="Comet ring", boards=("motion",)),
    # ── events ──
    _F("log", "events", ("flat", "iso"), glyph="log", motion=True, options=("lanes", "limit", "tail"), name="Log stream", boards=("widgets", "motion")),
    _F("lane", "events", glyph="lane", options=("limit", "lanes"), name="Lane", boards=("widgets",)),
    _F("feed", "events", ("flat", "iso"), glyph="feed", motion=True, options=("page", "show"), name="News feed", boards=("widgets", "motion", "iso")),
    _F("pulse", "events", glyph="pulse", motion=True, options=("classes", "ring_life"), name="Pulse", boards=("motion",)),
    _F("sweep", "events", ("iso",), glyph="sweep", motion=True, options=("sweep", "range"), name="Radar sweep", boards=("motion",)),
    _F("activity", "events", ("iso",), glyph="activity", name="Activity", boards=("iso",)),
    _F("notices", "events", ("iso",), glyph="notices", name="Notices", boards=("iso",)),
    # ── graphs ──
    _F("graph", "graph", glyph="graph", options=("layout",), name="Node graph", boards=("widgets",)),
    _F("minigraph", "graph", glyph="minigraph", options=("hollow",), name="Mini graph", boards=("widgets",)),
    _F("flow", "graph", glyph="flow", options=("nodes",), name="Flow", boards=("widgets",)),
    _F("pipes", "graph", ("flat", "iso"), glyph="pipes", motion=True, options=("flow", "idle"), name="Pipes", boards=("motion",)),
    _F("topology", "graph", ("flat", "iso"), glyph="topology", options=("floors",), name="Topology", boards=("widgets",)),
    _F("diagram", "graph", glyph="diagram", name="Diagram", boards=("reply",)),
    _F("city", "graph", ("iso",), glyph="city", motion=True, options=("footprint", "height", "colour", "lamp"), name="City", boards=("motion",)),
    _F("orbit", "items", glyph="orbit", motion=True, options=("rings", "size"), name="Orbit", boards=("motion",)),
    _F("context_graph", "graph", glyph="context_graph", options=("lanes", "labels")),
    # ── items ──
    _F("table", "items", ("flat", "iso"), glyph="table", options=("columns", "sort", "limit", "lit", "search"), name="Table", boards=("widgets", "motion")),
    _F("list", "items", ("flat", "iso"), glyph="list", options=("limit",), name="List", boards=("reply", "iso")),
    _F("rows", "items", glyph="rows", options=("columns", "sort", "limit", "lit"), name="Rows", boards=("reply", "spec")),           # a table's rows without its header: a composite's child
    _F("cards", "items", glyph="cards", options=("columns", "limit"), name="Cards", boards=("reply", "spec")),
    _F("temps", "values", glyph="temps", options=("unit", "max", "throttle", "sort", "limit"), name="Temp list", boards=("spec",)),
    _F("files", "items", ("flat", "iso"), glyph="files", options=("sort", "show"), name="Files", boards=("widgets", "motion", "iso")),
    _F("tree", "items", glyph="tree", name="Tree", boards=("reply",)),
    _F("people", "items", ("flat", "iso"), glyph="people", options=("presence",), name="People", boards=("widgets", "iso")),
    _F("gallery", "items", ("flat", "iso"), glyph="gallery", options=("thumbs", "lightbox"), name="Gallery", boards=("widgets", "iso")),
    _F("board", "items", ("flat", "iso"), glyph="board", options=("columns", "drag"), name="Task board", boards=("widgets", "motion", "iso")),
    _F("checklist", "items", glyph="check", name="Checklist", boards=("widgets",)),
    _F("carousel", "items", glyph="carousel", options=("pages",), name="Carousel", boards=("widgets",)),
    _F("shelf", "items", ("iso",), glyph="shelf", motion=True, options=("depth",), name="Shelf", boards=("motion",)),
    _F("stack", "items", ("iso",), glyph="stack", motion=True, options=("columns", "lift"), name="Card stack", boards=("motion",)),
    _F("links", "items", glyph="links", options=("tiles",), name="Quick links", boards=("widgets",)),
    _F("library", "items", ("iso",), glyph="library", name="Document library", boards=("iso",)),
    _F("pages", "items", ("iso",), glyph="pages", name="Site pages", boards=("iso",)),
    _F("wiki", "items", ("iso",), glyph="wiki", name="Wiki", boards=("iso",)),
    _F("devices", "items", ("iso",), glyph="devices", name="Mesh devices", boards=("iso",)),
    _F("notebook", "items", ("iso",), glyph="notebook", name="Notebook", boards=("iso",)),
    _F("hosts", "items", ("iso",), glyph="hosts", name="Proxmox hosts", boards=("iso",)),
    _F("containers", "items", ("iso",), glyph="containers", name="Docker containers", boards=("iso",)),
    _F("models", "items", ("iso",), glyph="models", name="Model catalogue", boards=("iso",)),
    _F("datasets", "items", ("iso",), glyph="datasets", name="Fabric datasets", boards=("iso",)),
    _F("sandboxes", "items", ("iso",), glyph="sandboxes", name="Sandboxes", boards=("iso",)),
    # ── strings and single things ──
    _F("string", "string", glyph="text", name="String", boards=("reply",)),
    _F("announcement", "string", glyph="notice", options=("priority", "actions"), name="Announcement", boards=("widgets",)),
    _F("terminal", "string", ("flat", "iso"), glyph="terminal", motion=True, sizes=("m", "l", "xl"), options=("lines", "typing"), name="Terminal", boards=("widgets", "motion", "iso")),
    _F("split-flap", "string", glyph="flap", motion=True, options=("lines", "highlight"), name="Split-flap", boards=("motion",)),
    _F("frame", "string", ("iso",), glyph="frame", options=("kind", "stand"), name="Iso frame", boards=("iso",)),
    # ── points ──
    _F("scatter", "points", ("flat", "iso"), glyph="scatter", options=("x", "y", "size"), name="Scatter", boards=("widgets", "spec")),
    _F("globe", "points", glyph="globe", options=("layer", "page", "limit"), name="Globe", boards=("reply",)),
    # ── the three the spec adds, the composites, and the chrome the registry already names ──
    _F("panel", "panel", glyph="panel", sizes=("l", "xl"), name="Registered panel", boards=("spec",)),
    _F("form", "values", ("flat", "iso"), glyph="form", options=("fields",), name="Form", boards=("spec", "iso")),
    _F("kv", "values", glyph="kv", options=("keys", "limit"), name="Key · value", boards=("spec", "reply")),   # a record's plain values, one per line (the element's kv)
    _F("composite", "composite", ("flat", "iso"), glyph="composite", options=("layout",), name="Composite", boards=("motion",)),
    _F("iso", "items", ("iso",), glyph="iso", name="Iso", boards=("reply",)),
    _F("program", "stages", glyph="program", name="Program", boards=("reply",)),
    _F("ask", "string", glyph="ask", name="Ask", boards=("reply",)),
    _F("controls", "string", glyph="controls", sizes=("xs", "s"), name="Controls", boards=("reply",)),
    _F("button", "string", glyph="button", sizes=("xs", "s"), name="Button", boards=("reply",)),
    _F("header", "string", glyph="header", sizes=("xs", "s"), name="Header", boards=("reply",)),
    _F("rail", "items", glyph="rail", sizes=("s",), options=("width", "slots"), name="Rail", boards=("motion",)),
]
_FORM_BY_ID = {f["id"]: f for f in FORMS}
# the context graph's line above is held verbatim by the chat's explode test; its name and board ride in here
_FORM_BY_ID["context_graph"].update({"name": "Context graph", "boards": ["spec"]})
# what the boards and older records call a form → the catalogue's id
FORM_ALIASES = {"hero + trend": "hero", "arc gauges": "gauge", "progress ring": "ring", "stacked area": "area",
                "heat map": "heat", "status matrix": "matrix", "dot matrix": "dots", "status pills": "pills",
                "number grid": "numbers", "activity rings": "rings", "spark table": "spark-table", "box plot": "box",
                "step chart": "step", "ranked bars": "ranked", "meter + delta": "meter", "bullet bars": "bullet",
                "stacked bar": "stacked-bar", "log stream": "log", "node graph": "graph", "mini graph": "minigraph",
                "card stack": "cards", "temp list": "thermo", "status strip": "pills", "meter panel": "meter-panel",
                "candlestick": "candles", "tasks": "board", "threshold scale": "threshold", "small multiples": "small-multiples",
                "iso columns": "bars", "iso tiles": "heat", "iso blocks": "treemap", "iso cubes": "waffle",
                "iso terraces": "small-multiples", "iso floors + pipes": "topology", "galaxy": "graph", "bar": "bars",
                # the WidgetConfig board's own ids for a few of these
                "battery": "level", "tablei": "table", "checks": "checklist", "memgraph": "minigraph",
                "logi": "log", "notice": "announcement", "ohlcv": "candles", "sparks": "small-multiples", "trend": "hero"}
# the boards' NAMES resolve too ("Thermometers" → thermo), so a record may be written the way the gallery labels it
for _f in FORMS:
    FORM_ALIASES.setdefault(_f["name"].lower(), _f["id"])


def form_ids() -> List[str]:
    return [f["id"] for f in FORMS]


def forms_on(board: str) -> List[Dict[str, Any]]:
    """The forms one gallery board draws (widgets · motion · iso · spec · reply)."""
    b = str(board or "").strip().lower()
    return [f for f in FORMS if b in f["boards"]]


def form(id_or_name: str) -> Optional[Dict[str, Any]]:
    k = str(id_or_name or "").strip().lower()
    return _FORM_BY_ID.get(k) or _FORM_BY_ID.get(FORM_ALIASES.get(k, ""))


def size_for_span(w: int, h: int = 1) -> str:
    """VeraDash's 12-column grid: 2-3 wide S, 4 M, 6 L, 8-12 XL; extra rows add detail then the table.

    A 2-3 wide tile one row tall is a row (S); two rows or more is a cell (M) - the figure and its sub line, as the
    Dashboard board's stat tiles draw (the Sizes board: "S is a chip or a row ... M is a cell")."""
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


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")
    return s[:64] or "widget"


def _place(v: Any) -> str:
    """A placement name the record knows, from any host's word for it ('' when it is none)."""
    s = str(v or "").strip().lower()
    s = PLACE_ALIASES.get(s, s)
    return s if s in PLACES else ""


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
    rng = read_in.get("range") if isinstance(read_in.get("range"), (list, tuple)) else r.get("range")
    try:
        rng = [float(rng[0]), float(rng[1])] if isinstance(rng, (list, tuple)) and len(rng) == 2 else None
    except (TypeError, ValueError):
        rng = None
    motion = frame_in.get("motion")
    if motion is None:
        motion = bool(draw_in.get("motion")) if legacy else (f["motion"] if f else False)
    span = frame_in.get("span") if isinstance(frame_in.get("span"), list) and len(frame_in.get("span")) == 2 else None
    if span and not (frame_in.get("size") or r.get("size")):
        size = size_for_span(span[0], span[1])
    draw = {k: v for k, v in draw_in.items() if k not in ("form", "size", "motion", "proj")}
    if isinstance(r.get("data"), (list, dict)):
        pass  # inline data rides beside the record (a fence with its own numbers); it is not part of the schema
    # the board's draw.proj is the projection; frame.dive is deep_dive; placement[] lists every envelope (place = the first)
    proj = str(r.get("projection") or draw_in.get("projection") or draw_in.get("proj") or "").strip().lower()
    placement = [_place(x) for x in (r.get("placement") if isinstance(r.get("placement"), list) else [])]
    placement = [x for x in placement if x]
    place = _place(r.get("place") or (placement[0] if placement else ""))
    dive = frame_in.get("deep_dive", frame_in.get("dive", True))
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
                 "map": read_in.get("map") if isinstance(read_in.get("map"), dict) else {}, "args": args, "range": rng},
        "frame": {"size": size, "span": span, "caption": bool(frame_in.get("caption", True)),
                  "legend": bool(frame_in.get("legend", False)), "motion": bool(motion),
                  "deep_dive": bool(dive), "dive": bool(dive),
                  "max_body": int(frame_in.get("max_body") or 0) or None,
                  "note": str(r.get("frame") if isinstance(r.get("frame"), str) else frame_in.get("note") or "")[:300]},
        "draw": draw,
        "skin": str(r.get("skin") or "inherit")[:32],
        "actions": [str(a)[:24] for a in actions if str(a) in ACTIONS][:8],
        "place": place, "placement": placement[:8],
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
        "placed": r.get("placed") if isinstance(r.get("placed"), list) else (list(r.get("placement") or []) or ([r["place"]] if r.get("place") else [])),
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
        known = set(f["options"]) | set(DRAW_COMMON)
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
