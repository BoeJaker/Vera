# -*- coding: utf-8 -*-
"""
The widget catalogue (UI redesign, Notes/40 section 3; the WidgetSpec board §3):
the registries every widget record points into - SHAPES, FORMS and SOURCES -
and the capabilities a renderer or an editor asks before drawing.

  widget.forms(shape?, q?, board?)  the forms, each {id, name, boards, shape, proj, sizes, options, glyph, motion}
  widget.sources(shape?, q?, domain?, refresh?, probe?)
                                  the sources: every quiet read of the live capability registry with the
                                  shape a widget reads it as (hand > measured > declared - widget_sources.py),
                                  grouped by domain, with its arguments, a refresh floor and a one-line
                                  description - {id, cap, shape, domain, args, params, refresh_min, unit,
                                  desc, map, tier}; cached ten minutes, refresh re-derives, probe measures
                                  the unmeasured argument-free reads live
  widget.validate(record)         the record normalised + the problems a bind would refuse + the
                                  warnings it would log (unknown draw options dropped, a size the form
                                  lacks, a refresh it cannot parse)
  widget.render_spec(record)      what a renderer needs in one answer: the normalised record, its
                                  resolved source, its form entry, and the size's composition
  widget.layouts()                the dashboards' layout files (vera/widgets/layouts/<key>.json): one per
                                  VeraDash grid, every widget of the grid as a record - {key, dashboard,
                                  widgets}; GET /ui/widgets/layouts/<key> serves one file
  widget.layout.migrate(key, legacy)  a legacy vera.dash.<key> {order, hidden, sizes, dynamic} as the
                                  layout record (migrate_layouts.py - the same rule VeraDash applies in the
                                  browser)

Shapes, forms and the record's rules live in widget_record.py (pure data);
this module only adds the live sources and the capabilities. Not named
"registry.py": that name is the agent registry's.
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import re
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
_src = _sibling("widget_sources")          # the derived source registry + the redis.* read family
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
    """The shape a capability most likely returns, from its name (a quiet read only); '' when there is no telling.
    The declared tier of widget_sources - a hand or measured shape beats it."""
    return _src.guess_shape(name, description)


def _cap_args(meta: Dict[str, Any], fn: Any) -> List[str]:
    try:
        import inspect
        return [p for p in inspect.signature(fn).parameters if p not in ("trace_id", "self")][:8]
    except Exception:
        return []


def sources(shape: str = "", q: str = "", domain: str = "", refresh: bool = False) -> List[Dict[str, Any]]:
    """Every source the catalogue knows: the registry's quiet reads with their shape (hand > measured > declared), the
    hand list's reads not (yet) registered, the streams - derived by widget_sources and cached; a module loaded later is
    a source at the next derivation (or now, with refresh)."""
    out = _src.catalogue(HAND, STREAMS, refresh=refresh)
    s, qq, d = str(shape or "").strip().lower(), str(q or "").strip().lower(), str(domain or "").strip().lower()
    if s and s != "all":
        out = [x for x in out if x["shape"] == s]
    if d and d != "all":
        out = [x for x in out if str(x.get("domain") or "").lower() == d]
    if qq:
        out = [x for x in out if qq in (x["id"] + " " + str(x.get("desc") or "") + " " + str(x.get("domain") or "") + " " + x.get("note", "")).lower()]
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
                "adds: panel, form, scatter), each under the board's own name. Inputs: shape (str - level, series, values, "
                "events, graph, items, stages, rate, parts, ohlcv, matrix, calendar, string, points, panel, composite), "
                "q (str - matches the id, the name, the glyph), board (str - widgets, motion, iso, spec, reply). Output: "
                "{ok, forms:[{id, name, boards[], shape, proj[], sizes[], options[], glyph, motion}], shapes[], "
                "sizes:{size: composition}, boards:{board: count}, count}.")
async def widget_forms(shape: str = "", q: str = "", board: str = "", trace_id=None):
    s, qq, b = str(shape or "").strip().lower(), str(q or "").strip().lower(), str(board or "").strip().lower()
    out = [dict(f) for f in FORMS if (not s or s == "all" or f["shape"] == s) and (not b or b == "all" or b in f["boards"])
           and (not qq or qq in (f["id"] + " " + f["glyph"] + " " + f["name"].lower()))]
    boards = {}
    for f in FORMS:
        for bb in f["boards"]:
            boards[bb] = boards.get(bb, 0) + 1
    return {"ok": True, "forms": out, "count": len(out), "shapes": list(SHAPES), "sizes": dict(_rec.COMPOSITIONS),
            "boards": boards, "aliases": dict(_rec.FORM_ALIASES)}


@capability(
    "widget.sources", memory="off", silent=True,
    http_method="GET", http_path="/ui/widgets/sources", http_tags=["ui", "widgets"],
    description="The widget catalogue's SOURCES: every quiet read of the live capability registry with the shape a widget "
                "reads it as (hand > measured > declared), grouped by domain, with its arguments and a one-line description; "
                "plus the hand-listed reads and the streams. Inputs: shape (str), q (str), domain (str), limit (int, 300), "
                "refresh (bool - re-derive now), probe (bool - measure up to probe_limit unmeasured argument-free reads live, "
                "a few seconds each). Output: {ok, sources:[{id, cap, shape, domain, args[], params[], required[], refresh_min, "
                "unit, desc, map, keys, tier, note}], count, domains:{domain: n}, shapes:{shape: n}, tiers:{tier: n}, derived_at, probed}.")
async def widget_sources(shape: str = "", q: str = "", domain: str = "", limit: int = 300, refresh: bool = False, probe: bool = False, probe_limit: int = 40, trace_id=None):
    probed = None
    if probe:
        probed = await _src.probe(limit=int(probe_limit or 40))
        refresh = True
    all_ = sources(refresh=refresh)
    out = sources(shape, q, domain)
    domains: Dict[str, int] = {}
    shapes: Dict[str, int] = {}
    tiers: Dict[str, int] = {}
    for x in all_:
        domains[x.get("domain") or "Other"] = domains.get(x.get("domain") or "Other", 0) + 1
        shapes[x["shape"]] = shapes.get(x["shape"], 0) + 1
        tiers[x.get("tier") or "declared"] = tiers.get(x.get("tier") or "declared", 0) + 1
    return {"ok": True, "sources": out[: max(1, int(limit or 300))], "count": len(out), "total": len(all_), "domains": domains, "shapes": shapes,
            "tiers": tiers, "derived_at": _src.cached_at(), "probed": probed}


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


# ── the dashboards' layouts (UI redesign M5, Notes/40 section 4; the Dashboard board) ─────────────────────────
# One file per VeraDash grid: {dashboard, layout, key, user, grid{cols, row, gap, widths}, widgets:[{record, at,
# span, hidden, refresh}]} - every widget of the grid as a record. The page fetches its file on boot (VeraDash
# applies it under the user's persisted layout); a missing file is a 404 and the page runs on its markup alone.
_mig = _sibling("migrate_layouts")
LAYOUT_DIR = Path(__file__).parent / "layouts"


def layout_keys() -> List[str]:
    try:
        return sorted(p.stem for p in LAYOUT_DIR.glob("*.json"))
    except Exception:
        return []


def load_layout(key: str) -> Optional[Dict[str, Any]]:
    """The layout file for one grid key (main, dream, wol-workers, ...), or None. The key is a file stem only."""
    k = str(key or "").strip()
    if not k or not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", k):
        return None
    p = LAYOUT_DIR / (k + ".json")
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return rec if isinstance(rec, dict) and isinstance(rec.get("widgets"), list) else None


@capability(
    "widget.read", memory="off", silent=True,
    http_method="POST", http_path="/ui/widgets/read", http_tags=["ui", "widgets"],
    description="Many readings in one call: a dashboard of fifty tiles asks for its sources together instead of on "
                "fifty connections (the browser allows six per host and the page's other requests hold them). Each "
                "call runs as /mcp/call would - the same wrapper, the same sandbox read-through. Input: calls (list of "
                "{name, arguments}; at most 40). Output: {ok, results:[{name, ok, content | error, ms}], count}.")
async def widget_read(calls=None, trace_id=None):
    import asyncio as _aio, sys as _sys, time as _time
    co = _sys.modules.get("Vera.vera.capability_orchestration") or _sys.modules.get("vera.capability_orchestration")
    reg = getattr(co, "CAPABILITY_REGISTRY", None) or {}
    items = [c for c in (calls or []) if isinstance(c, dict) and c.get("name")][:40]

    async def one(c):
        name = str(c.get("name") or ""); args = c.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        t0 = _time.time()
        try:
            cap = reg.get(name)
            if not cap:
                # a reading this process does not load may still be prod's, through the sandbox read-through
                rt = None
                if getattr(co, "_READ_THROUGH_URL", "") and co._sg_read_through_allowed(name, "GET"):
                    rt = await co._upstream_read(name, args)
                if rt is None:
                    return {"name": name, "ok": False, "error": "unknown capability", "ms": int((_time.time() - t0) * 1000)}
                return {"name": name, "ok": True, "content": rt, "ms": int((_time.time() - t0) * 1000)}
            accepted = set((cap.get("schema") or {}).get("properties", {}).keys())
            if accepted:
                args = {k: v for k, v in args.items() if k in accepted}
            res = await cap["func"](**args)
            return {"name": name, "ok": True, "content": res, "ms": int((_time.time() - t0) * 1000)}
        except Exception as e:   # one failed reading never fails the batch
            return {"name": name, "ok": False, "error": str(e)[:200], "ms": int((_time.time() - t0) * 1000)}

    results = await _aio.gather(*[one(c) for c in items]) if items else []
    return {"ok": True, "results": list(results), "count": len(results)}


@capability(
    "widget.layouts", memory="off", silent=True,
    http_method="GET", http_path="/ui/widgets/layouts", http_tags=["ui", "widgets"],
    description="The dashboards' layout files: one per VeraDash grid, every widget of the grid as a record. "
                "Input: key (str - one grid; omit for the list). Output: {ok, layouts:[{key, dashboard, widgets, "
                "records}], count} or, with a key, {ok, key, layout}.")
async def widget_layouts(key: str = "", trace_id=None):
    if key:
        rec = load_layout(key)
        return {"ok": bool(rec), "key": key, "layout": rec, **({} if rec else {"error": "no layout %r" % key})}
    out = []
    for k in layout_keys():
        rec = load_layout(k)
        if not rec:
            continue
        out.append({"key": k, "dashboard": rec.get("dashboard") or k, "widgets": len(rec["widgets"]),
                    "records": _mig.count_records(rec)})
    return {"ok": True, "layouts": out, "count": len(out)}


@capability(
    "widget.layout.migrate", memory="off", silent=True,
    http_method="POST", http_path="/ui/widgets/layouts/migrate", http_tags=["ui", "widgets"],
    description="A legacy vera.dash.<key> state {order, hidden, sizes, dynamic} as the layout record: order -> at "
                "(dense flow), sizes -> span, hidden -> hidden, dynamic -> a panel record. The grid's layout file "
                "supplies the page tiles when one exists. Input: key (str!), legacy (object!). Output: {ok, layout}.")
async def widget_layout_migrate(key: str = "", legacy: Optional[dict] = None, trace_id=None):
    k = str(key or "").strip()
    if not k:
        return {"ok": False, "error": "key required"}
    if isinstance(legacy, dict) and isinstance(legacy.get("widgets"), list):
        return {"ok": True, "layout": legacy, "note": "already the layout record"}
    return {"ok": True, "layout": _mig.migrate(k, legacy if isinstance(legacy, dict) else {}, load_layout(k))}


@_orch.APP.get("/ui/widgets/layouts/{key}", include_in_schema=False)
async def _serve_widget_layout(key: str):
    """One grid's layout file, as the page fetches it on boot."""
    from fastapi.responses import JSONResponse
    rec = load_layout(key)
    if not rec:
        return JSONResponse({"ok": False, "error": "no layout %r" % key}, status_code=404)
    return JSONResponse(rec, headers={"Cache-Control": "no-cache"})
