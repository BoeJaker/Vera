# -*- coding: utf-8 -*-
"""
The widget catalogue (UI redesign, Notes/40 section 3; the WidgetSpec board §3):
the registries every widget record points into - SHAPES, FORMS and SOURCES -
and the capabilities a renderer or an editor asks before drawing.

  widget.forms(shape?, q?)        the forms, each {id, shape, proj, sizes, options, glyph, motion}
  widget.sources(shape?, q?)      the sources: one per capability of a known shape (the catalogue is
                                  built from the live capability registry at call time, with a hand
                                  list for the streams and the well-known reads) - {id, shape, cap,
                                  args, refresh_min, unit}
  widget.validate(record)         the record normalised + the problems a bind would refuse + the
                                  warnings it would log (unknown draw options dropped, a size the form
                                  lacks, a refresh it cannot parse)
  widget.render_spec(record)      what a renderer needs in one answer: the normalised record, its
                                  resolved source, its form entry, and the size's composition

Shapes, forms and the record's rules live in widget_record.py (pure data);
this module only adds the live sources and the capabilities. Not named
"registry.py": that name is the agent registry's.
"""
from __future__ import annotations

import importlib.util as _ilu
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability  # noqa: F401


def _sibling(name: str):
    """A module beside this file, loaded once by path (the way _module_files loads us: no package)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = _ilu.spec_from_file_location(name, Path(__file__).parent / (name + ".py"))
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_rec = _sibling("widget_record")
SHAPES = _rec.SHAPES
FORMS = _rec.FORMS

# ── the sources ───────────────────────────────────────────────────────────────
# the well-known reads, by hand: shape · refresh floor · unit (the WidgetSpec board's source rows)
HAND: Dict[str, Dict[str, Any]] = {
    "obs.health": {"shape": "values", "refresh_min": "10s"},
    "obs.diagnostics": {"shape": "string", "refresh_min": "30s"},
    "obs.pending": {"shape": "level", "refresh_min": "5s"},
    "obs.scheduler": {"shape": "items", "refresh_min": "30s"},
    "obs.node_temps": {"shape": "values", "refresh_min": "10s", "unit": "°C"},
    "obs.events": {"shape": "events", "refresh_min": "live"},
    "obs.provenance": {"shape": "items", "refresh_min": "30s"},
    "obs.workers": {"shape": "items", "refresh_min": "10s"},
    "sysmon.status": {"shape": "values", "refresh_min": "5s"},
    "sysmon.history": {"shape": "series", "refresh_min": "5s"},
    "memory.stats": {"shape": "values", "refresh_min": "30s"},
    "memory.select": {"shape": "items", "refresh_min": "1m"},
    "topology.snapshot": {"shape": "graph", "refresh_min": "30s"},
    "docker.stats.top": {"shape": "items", "refresh_min": "10s"},
    "evolve.sandbox.status": {"shape": "values", "refresh_min": "10s"},
    "evolve.sandbox_status": {"shape": "values", "refresh_min": "10s"},
    "evolve.pipeline.list": {"shape": "items", "refresh_min": "30s"},
    "evolve.git_graph": {"shape": "graph", "refresh_min": "1m"},
    "markets.bars": {"shape": "ohlcv", "refresh_min": "1m"},
    "sched.list": {"shape": "calendar", "refresh_min": "1m"},
    "goals.detail": {"shape": "items", "refresh_min": "1m"},
    "goals.board": {"shape": "items", "refresh_min": "30s"},
    "loops.status": {"shape": "stages", "refresh_min": "5s"},
    "workshop.agent_loop.session_state": {"shape": "stages", "refresh_min": "live"},
    "jobs.stats": {"shape": "level", "refresh_min": "10s"},
    "cluster.jobs": {"shape": "items", "refresh_min": "10s"},
    "cluster.workers": {"shape": "items", "refresh_min": "10s"},
    "cluster.ollama": {"shape": "items", "refresh_min": "10s"},
    "ollama.request_log": {"shape": "events", "refresh_min": "10s"},
    "ollama.route_stats": {"shape": "values", "refresh_min": "30s"},
    "dream.status": {"shape": "values", "refresh_min": "30s"},
    "dream.history": {"shape": "values", "refresh_min": "1m"},
    "exec.artifacts.list": {"shape": "items", "refresh_min": "30s"},
    "context.assemble": {"shape": "graph", "refresh_min": "event"},
    "memory.session.nodes": {"shape": "graph", "refresh_min": "event"},
    "canvas.list": {"shape": "items", "refresh_min": "30s"},
    "ui.panels.open": {"shape": "items", "refresh_min": "10s"},
    "widget.template.list": {"shape": "items", "refresh_min": "1m"},
    "ui.directive.log": {"shape": "events", "refresh_min": "10s"},
    "ui.script.list": {"shape": "items", "refresh_min": "1m"},
}
# the streams are not capabilities; they are sources all the same
STREAMS: List[Dict[str, Any]] = [
    {"id": "stream:events", "shape": "events", "cap": "", "args": [], "refresh_min": "live", "unit": "",
     "note": "ws subscribe_events - the live event stream"},
    {"id": "stream:loop", "shape": "stages", "cap": "", "args": [], "refresh_min": "live", "unit": "",
     "note": "the agentic loop's SSE - steps, branches, waits"},
    {"id": "stream:log", "shape": "events", "cap": "", "args": [], "refresh_min": "live", "unit": "",
     "note": "the system log tail"},
]
_REFRESH_FLOOR = {"level": "5s", "series": "5s", "values": "10s", "events": "10s", "items": "30s", "graph": "30s",
                  "stages": "5s", "rate": "5s", "parts": "30s", "ohlcv": "1m", "matrix": "30s", "calendar": "1m",
                  "string": "30s", "points": "30s"}
# how a capability's NAME says what it returns (the tail of the dotted name)
_NAME_SHAPE = (
    (("history", "series", "timeseries", "trend"), "series"),
    (("events", "log", "tail", "feed", "stream", "activity"), "events"),
    (("graph", "snapshot", "topology", "edges", "nodes"), "graph"),
    (("list", "recent", "search", "find", "instances", "templates", "top", "all", "board"), "items"),
    (("stats", "status", "health", "metrics", "summary", "state", "usage", "info"), "values"),
    (("count", "pending", "depth", "size", "total"), "level"),
    (("rate", "throughput", "tok_s"), "rate"),
    (("bars", "ohlcv", "candles"), "ohlcv"),
    (("calendar", "agenda", "schedule"), "calendar"),
)
_NEVER = ("write", "delete", "remove", "create", "run", "exec", "kill", "restart", "stop", "start", "set", "save",
          "send", "post", "push", "upsert", "generate", "author", "edit", "promote", "spawn", "provision", "ota")


def guess_shape(name: str, description: str = "") -> str:
    """The shape a capability most likely returns, from its name (a quiet read only); '' when there is no telling."""
    n = str(name or "").lower()
    parts = [p for p in n.replace("_", ".").split(".") if p]
    if not parts or any(p in _NEVER for p in parts):
        return ""
    tail = parts[-1]
    for words, shape in _NAME_SHAPE:
        if tail in words:
            return shape
    d = str(description or "").lower()
    for words, shape in _NAME_SHAPE:
        if any((" " + w + " ") in (" " + d + " ") for w in words[:3]):
            return shape
    return ""


def _cap_args(meta: Dict[str, Any], fn: Any) -> List[str]:
    try:
        import inspect
        return [p for p in inspect.signature(fn).parameters if p not in ("trace_id", "self")][:8]
    except Exception:
        return []


def sources(shape: str = "", q: str = "") -> List[Dict[str, Any]]:
    """Every source the catalogue knows: the hand list, the streams, and every registered capability whose name
    says what it returns. Built at call time so a module loaded later is a source the moment it registers."""
    out: List[Dict[str, Any]] = []
    seen = set()
    reg = getattr(_orch, "CAPABILITY_REGISTRY", {}) or {}
    for name, entry in list(reg.items()):
        meta = (entry or {}).get("meta") or {}
        hand = HAND.get(name)
        sh = (hand or {}).get("shape") or guess_shape(name, str(meta.get("description") or ""))
        if not sh:
            continue
        out.append({"id": name, "shape": sh, "cap": name, "args": _cap_args(meta, (entry or {}).get("func")),
                    "refresh_min": (hand or {}).get("refresh_min") or _REFRESH_FLOOR.get(sh, "30s"),
                    "unit": (hand or {}).get("unit") or "", "note": "" if hand else "shape from the name"})
        seen.add(name)
    for name, hand in HAND.items():          # a hand-listed read not (yet) registered still shows, marked
        if name not in seen:
            out.append({"id": name, "shape": hand["shape"], "cap": name, "args": [], "refresh_min": hand.get("refresh_min") or "30s",
                        "unit": hand.get("unit") or "", "note": "not registered here"})
    out += [dict(s) for s in STREAMS]
    s, qq = str(shape or "").strip().lower(), str(q or "").strip().lower()
    if s and s != "all":
        out = [x for x in out if x["shape"] == s]
    if qq:
        out = [x for x in out if qq in (x["id"] + " " + x.get("note", "")).lower()]
    out.sort(key=lambda x: (x["note"] == "not registered here", x["id"]))
    return out


def source(id_: str) -> Optional[Dict[str, Any]]:
    sid = str(id_ or "").strip()
    if not sid:
        return None
    if sid.startswith("panel:"):
        return {"id": sid, "shape": "panel", "cap": "", "args": [], "refresh_min": "", "unit": "", "note": "a registered panel"}
    for s in sources():
        if s["id"] == sid:
            return s
    return None


# ── capabilities ──────────────────────────────────────────────────────────────
@capability(
    "widget.forms", memory="off", silent=True,
    http_method="GET", http_path="/ui/widgets/forms", http_tags=["ui", "widgets"],
    description="The widget catalogue's FORMS (the Widgets, WidgetsMotion and WidgetsIso galleries + the three the spec "
                "adds: panel, form, scatter). Inputs: shape (str - level, series, values, events, graph, items, stages, "
                "rate, parts, ohlcv, matrix, calendar, string, points, panel, composite), q (str). Output: {ok, forms:"
                "[{id, shape, proj[], sizes[], options[], glyph, motion}], shapes[], sizes:{size: composition}, count}.")
async def widget_forms(shape: str = "", q: str = "", trace_id=None):
    s, qq = str(shape or "").strip().lower(), str(q or "").strip().lower()
    out = [dict(f) for f in FORMS if (not s or s == "all" or f["shape"] == s) and (not qq or qq in (f["id"] + " " + f["glyph"]))]
    return {"ok": True, "forms": out, "count": len(out), "shapes": list(SHAPES), "sizes": dict(_rec.COMPOSITIONS),
            "aliases": dict(_rec.FORM_ALIASES)}


@capability(
    "widget.sources", memory="off", silent=True,
    http_method="GET", http_path="/ui/widgets/sources", http_tags=["ui", "widgets"],
    description="The widget catalogue's SOURCES: one per capability of a known shape (from the live capability "
                "registry + a hand list for the well-known reads and the streams). Inputs: shape (str), q (str), "
                "limit (int, 300). Output: {ok, sources:[{id, shape, cap, args[], refresh_min, unit, note}], count}.")
async def widget_sources(shape: str = "", q: str = "", limit: int = 300, trace_id=None):
    out = sources(shape, q)
    return {"ok": True, "sources": out[: max(1, int(limit or 300))], "count": len(out)}


@capability(
    "widget.validate", memory="off", silent=True,
    http_method="POST", http_path="/ui/widgets/validate", http_tags=["ui", "widgets"],
    description="Validate a widget record before it binds: the source's shape must equal the form's; unknown draw "
                "options are dropped with a warning; a missing source is a problem unless the form needs none. Accepts "
                "the full record, the registry's template shape, or the short form (window/size/refresh at the top). "
                "Input: record (object!). Output: {ok, valid, record (normalised), problems[], warnings[], template}.")
async def widget_validate(record: Optional[dict] = None, trace_id=None):
    r0 = record if isinstance(record, dict) else {}
    src = source(str(r0.get("source") if isinstance(r0.get("source"), str) else ((r0.get("reads") or {}).get("cap") if isinstance(r0.get("reads"), dict) else "")))
    r, problems, warnings = _rec.validate(r0, (src or {}).get("shape") if src and src["shape"] != "panel" else None)
    return {"ok": True, "valid": not problems, "record": r, "problems": problems, "warnings": warnings,
            "template": _rec.to_template(r)}


@capability(
    "widget.render_spec", memory="off", silent=True,
    http_method="POST", http_path="/ui/widgets/render_spec", http_tags=["ui", "widgets"],
    description="What a renderer needs for one record, in one answer: the normalised record, its resolved source "
                "(shape, cap, args, refresh floor, unit), its form entry (shape, projections, sizes, options), and the "
                "size's composition (xs glyph+figure . s chip/row . m cell . l cell+detail . xl panel+table+log+actions; "
                "a span picks the size when the record has both). Input: record (object!). Output: {ok, record, source, "
                "form, size:{size, composition, span}, problems[], warnings[]}.")
async def widget_render_spec(record: Optional[dict] = None, trace_id=None):
    r0 = record if isinstance(record, dict) else {}
    src = source(str(r0.get("source") if isinstance(r0.get("source"), str) else ((r0.get("reads") or {}).get("cap") if isinstance(r0.get("reads"), dict) else "")))
    r, problems, warnings = _rec.validate(r0, (src or {}).get("shape") if src and src["shape"] != "panel" else None)
    f = _rec.form(r["form"])
    span = r["frame"].get("span")
    size = _rec.size_for_span(span[0], span[1]) if span else r["frame"]["size"]
    if f and size not in f["sizes"]:
        size = f["sizes"][-1]
    return {"ok": True, "record": r, "source": src, "form": f,
            "size": {"size": size, "composition": _rec.composition(size), "span": span},
            "problems": problems, "warnings": warnings}
