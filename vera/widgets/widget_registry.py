# -*- coding: utf-8 -*-
"""
The widget registry (UI redesign, Notes/40 section 3; the WidgetRegistry,
WidgetConfig and WidgetSpec boards).

Every part of the UI that can be defined as a widget is one: a RECORD that
says what it is (its form), what it reads (a capability and its arguments),
its frame (title, unit, range, columns), how it draws (form and size), what
it can do (actions) and where it has been placed (envelopes: dashboard,
canvas, LHM, reply, notebook, ops map, iso plate). A TEMPLATE is a saved
record anyone can place again; an INSTANCE is one placement of a template.

Two kinds of template are built in and never stored:
  - every registered UI panel (UI_PANELS) as a `panel` widget - the dynamic
    widgets VeraDash already places on a dashboard are exactly these;
  - the chat LHM's own parts (rail, header, list, galaxy, budget bar, file
    tree, terminal, actions, loop program, CTA) - what the chat's edit mode
    shows a record for and can save as a template of its own.
Saved templates and instances live in Redis (vera:ui:widget:tpl:* and
vera:ui:widget:inst:*) - NOT under vera:ui:panel:*, which the startup loader
globs for dynamic panel records.

Capabilities: widget.template.list / get / save / delete / instantiate,
widget.instance.list / remove. HTTP mirrors under /ui/widgets/*. The
registry panel (widget-registry, an element panel like the agent registry)
is the UI over them; a dashboard host realises instances placed on it.
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (   # noqa: F401
    APP, UI_PANELS, capability, emit_event, now_iso, register_ui,
)


def _sibling(name: str):
    """A module beside this file, loaded once by path (the way _module_files loads us: no package to import from)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = _ilu.spec_from_file_location(name, Path(__file__).parent / (name + ".py"))
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# The record's schema and rules (m1 foundations): the registry keeps its template shape, the full record the
# WidgetSpec board writes is accepted too, and both are normalised and checked in ONE place - widget_record.py.
_rec = _sibling("widget_record")

FORMS = ("counter", "meter", "sparkline", "node", "chart", "graph", "terminal", "table",
         "program", "iso", "ask", "panel", "list", "tree", "controls", "button", "header", "rail")
WHERES = ("dashboard", "canvas", "LHM", "reply", "notebook", "ops map", "iso plate")

_TPL_KEY = "vera:ui:widget:tpl:{id}"
_INST_KEY = "vera:ui:widget:inst:{id}"
_TPL_GLOB = "vera:ui:widget:tpl:*"
_INST_GLOB = "vera:ui:widget:inst:*"


def _redis():
    return _orch.REDIS


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")
    return s[:64] or "widget"


# ── the built-ins ─────────────────────────────────────────────────────────────
# The chat LHM's parts, as the design's records name them, with what the live
# pane actually reads (the capability behind the pane in chat_panel.html).
_LHM_BUILTINS: List[Dict[str, Any]] = [
    {"id": "lhm:rail", "name": "The rail", "form": "rail",
     "reads": {"cap": "loop.status", "args": {}, "note": "badges from the loop and the open panels"},
     "frame": "46 px . icons . badges", "draw": {"form": "rail", "size": "S"},
     "can": ["reorder", "hide an icon", "add a menu of your own"], "placed": ["LHM", "harness (absorbed)"]},
    {"id": "lhm:header", "name": "Menu header", "form": "header",
     "reads": {"cap": "", "args": {}, "note": "the menu's title and meta line"},
     "frame": "title . meta . edit", "draw": {"form": "header", "size": "XS"},
     "can": ["edit the menu"], "placed": ["LHM"]},
    {"id": "lhm:sessions", "name": "Sessions list", "form": "list",
     "reads": {"cap": "memory.search", "args": {"record_type": "message", "category": "chat"}},
     "frame": "name . when . count", "draw": {"form": "rows", "size": "S"},
     "can": ["open", "rename", "new session", "title all"], "placed": ["LHM"]},
    {"id": "lhm:ctx-galaxy", "name": "Context galaxy", "form": "graph",
     "reads": {"cap": "context.assemble", "args": {"session_id": "this"}},
     "frame": "sources as layers . galaxy . list . fabric . raw . frames", "draw": {"form": "galaxy", "size": "M"},
     "can": ["switch view", "toggle a layer", "save a frame", "expand to the graph"], "placed": ["LHM", "canvas"]},
    {"id": "lhm:ctx-budget", "name": "Budget bar", "form": "meter",
     "reads": {"cap": "context.assemble", "args": {"session_id": "this"}, "note": "tokens in context / window"},
     "frame": "used / window . free", "draw": {"form": "bar", "size": "XS"},
     "can": ["open the source"], "placed": ["LHM", "header meter"]},
    {"id": "lhm:memory-graph", "name": "Memory graph", "form": "graph",
     "reads": {"cap": "memory.session.nodes", "args": {"session_id": "this"}},
     "frame": "nodes . edges . legend", "draw": {"form": "graph", "size": "M"},
     "can": ["fit", "toggle edges", "open a node"], "placed": ["LHM", "canvas"]},
    {"id": "lhm:loop-graph", "name": "Loop graph", "form": "program",
     "reads": {"cap": "workshop.agent_loop.session_state", "args": {"session_id": "this"}, "note": "streamed"},
     "frame": "steps . current . waiting on you", "draw": {"form": "graph", "size": "M"},
     "can": ["pause", "step", "re-plan", "cancel", "loop settings"], "placed": ["LHM", "canvas"]},
    {"id": "lhm:file-tree", "name": "File tree", "form": "tree",
     "reads": {"cap": "exec.artifacts.list", "args": {"session_id": "this"}},
     "frame": "name . size . changed", "draw": {"form": "tree", "size": "S"},
     "can": ["open", "preview", "run", "save as artifact"], "placed": ["LHM", "canvas"]},
    {"id": "lhm:terminal", "name": "Session terminal", "form": "terminal",
     "reads": {"cap": "sandbox.session.exec", "args": {"session_id": "this"}, "note": "streamed"},
     "frame": "attached . input line", "draw": {"form": "terminal", "size": "M"},
     "can": ["interrupt", "restart", "VS Code", "pin"], "placed": ["LHM", "canvas", "reply"]},
    {"id": "lhm:actions", "name": "Sandbox actions", "form": "controls",
     "reads": {"cap": "sandbox.session.status", "args": {"session_id": "this"}},
     "frame": "interrupt . restart . VS Code . upload . pin shell", "draw": {"form": "controls", "size": "XS"},
     "can": ["each is a capability call"], "placed": ["LHM"]},
    {"id": "lhm:cta", "name": "The CTA", "form": "button",
     "reads": {"cap": "", "args": {}, "note": "the menu's one action"},
     "frame": "one line", "draw": {"form": "button", "size": "XS"},
     "can": ["new session", "refresh the context", "loop settings", "open a panel", "open the terminal", "full settings"],
     "placed": ["LHM"]},
]


def _builtin_lhm() -> List[Dict[str, Any]]:
    out = []
    for b in _LHM_BUILTINS:
        t = _normalise(dict(b))
        t["source"] = {"origin": "built-in", "from": "the chat LHM", "panel": ""}
        t["version"] = 1
        out.append(t)
    return out


def _builtin_panels() -> List[Dict[str, Any]]:
    out = []
    for pid, p in list(UI_PANELS.items()):
        caps = [c for c in (p.get("ui_caps") or []) if isinstance(c, str)]
        t = _normalise({
            "id": "panel:" + pid, "name": p.get("label") or pid, "form": "panel",
            "reads": {"cap": caps[0] if caps else "", "args": {}, "note": (", ".join(caps[1:6]) if len(caps) > 1 else "")},
            "frame": "the panel's own document", "draw": {"form": "panel", "size": "L"},
            "can": ["pop out", "open standalone", "drive it with panel.dispatch"],
            "placed": ["dashboard", "canvas", "notebook"],
        })
        t["source"] = {"origin": "built-in", "from": "the panel registry", "panel": pid, "icon": p.get("icon") or ""}
        t["version"] = 1
        out.append(t)
    return out


# ── the record ────────────────────────────────────────────────────────────────
def _normalise(t: Dict[str, Any]) -> Dict[str, Any]:
    """One shape for every template, whatever came in - the template shape, or the WidgetSpec board's full record
    (title . source . read . frame{size} . draw options), which is folded to the template shape first."""
    if isinstance(t, dict) and ("title" in t or isinstance(t.get("read"), dict) or isinstance(t.get("frame"), dict)) and "reads" not in t:
        return _rec.to_template(t)
    return _rec.normalise_template(t if isinstance(t, dict) else {})


def problems(t: Dict[str, Any]) -> List[str]:
    """What a save refuses: the catalogue's forms are as valid as the registry's own eighteen."""
    return _rec.template_problems(t, FORMS, WHERES)


async def _load_saved() -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return []
    out = []
    try:
        async for key in r.scan_iter(match=_TPL_GLOB, count=200):
            raw = await r.get(key)
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "replace")
            try:
                out.append(_normalise(json.loads(raw)))
            except Exception:
                continue
    except Exception:
        return out
    return out


async def _all_templates() -> List[Dict[str, Any]]:
    saved = await _load_saved()
    ids = {t["id"] for t in saved}
    return saved + [t for t in _builtin_lhm() if t["id"] not in ids] + [t for t in _builtin_panels() if t["id"] not in ids]


async def _instances() -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return []
    out = []
    try:
        async for key in r.scan_iter(match=_INST_GLOB, count=200):
            raw = await r.get(key)
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "replace")
            try:
                out.append(json.loads(raw))
            except Exception:
                continue
    except Exception:
        return out
    return out


def _with_counts(templates: List[Dict[str, Any]], insts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """placed = [{where, count}] from the live instances, on top of the declared placements."""
    for t in templates:
        counts: Dict[str, int] = {}
        for w in t.get("placed") or []:
            counts.setdefault(w, 0)
        for i in insts:
            if i.get("template") == t["id"]:
                counts[i.get("where") or "?"] = counts.get(i.get("where") or "?", 0) + 1
        t["placements"] = [{"where": w, "count": n} for w, n in counts.items()]
        t["instances"] = sum(1 for i in insts if i.get("template") == t["id"])
    return templates


# ── capabilities ──────────────────────────────────────────────────────────────
@capability(
    "widget.template.list", memory="off", silent=True,
    http_method="GET", http_path="/ui/widgets/templates", http_tags=["ui", "widgets"],
    description="The widget registry: every widget template - the saved ones, the chat LHM's "
                "own parts, and every registered UI panel as a `panel` widget. Inputs: kind "
                "(str - a form: counter, meter, sparkline, node, chart, graph, terminal, table, "
                "program, iso, ask, panel, list, tree, controls, button, header, rail), where "
                "(str - a placement: dashboard, canvas, LHM, reply, notebook, ops map, iso "
                "plate), q (str - search name/reads/frame), limit (int, default 200). Output: "
                "{ok, templates:[{id, name, form, reads:{cap,args,every,note}, frame, "
                "draw:{form,size}, can[], placements:[{where,count}], instances, source:{origin,"
                "from,panel}, version}], count, kinds:{form:n}, wheres:{where:n}}.")
async def widget_template_list(kind: str = "", where: str = "", q: str = "", limit: int = 200, trace_id=None):
    insts = await _instances()
    all_t = _with_counts(await _all_templates(), insts)
    kinds: Dict[str, int] = {}
    wheres: Dict[str, int] = {}
    for t in all_t:
        kinds[t["form"]] = kinds.get(t["form"], 0) + 1
        for p in t["placements"]:
            wheres[p["where"]] = wheres.get(p["where"], 0) + 1
    k, w, qq = str(kind or "").strip().lower(), str(where or "").strip(), str(q or "").strip().lower()
    out = []
    for t in all_t:
        if k and k != "all" and t["form"] != k:
            continue
        if w and w != "all" and not any(p["where"].lower().startswith(w.lower()) for p in t["placements"]):
            continue
        if qq and qq not in (t["name"] + " " + t["reads"]["cap"] + " " + t["frame"] + " " + t["id"]).lower():
            continue
        out.append(t)
    out.sort(key=lambda t: (0 if t["source"].get("origin") != "built-in" else 1, t["name"].lower()))
    return {"ok": True, "templates": out[: max(1, int(limit or 200))], "count": len(out), "total": len(all_t),
            "kinds": kinds, "wheres": wheres, "forms": list(FORMS), "places": list(WHERES)}


@capability(
    "widget.template.get", memory="off", silent=True,
    http_method="GET", http_path="/ui/widgets/template", http_tags=["ui", "widgets"],
    description="One widget template by id, with its instances. Input: id (str!). Output: {ok, template, instances:[...]}.")
async def widget_template_get(id: str = "", trace_id=None):
    tid = str(id or "").strip()
    insts = await _instances()
    for t in _with_counts(await _all_templates(), insts):
        if t["id"] == tid:
            return {"ok": True, "template": t, "instances": [i for i in insts if i.get("template") == tid]}
    return {"ok": False, "error": "no template %r" % tid}


@capability(
    "widget.template.save", memory="off",
    http_method="POST", http_path="/ui/widgets/templates/save", http_tags=["ui", "widgets"],
    description="Save a widget template (create or update). Pass the record: {id?, name!, form!, "
                "reads:{cap!, args, every}, frame, draw:{form,size}, can[], placed[], source:{origin,"
                "from}, tags[]}. A built-in saved under its own id becomes yours (a copy with a new "
                "version). REFUSES a record with no name, an unknown form, or a form that reads "
                "nothing - pass force=true to keep it anyway. Output: {ok, template, problems[], created}.")
async def widget_template_save(template: Optional[dict] = None, force: bool = False, trace_id=None):
    t = _normalise(template if isinstance(template, dict) else {})
    probs = problems(t)
    if probs and not force:
        return {"ok": False, "problems": probs, "hint": "pass force=true to store it anyway"}
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}
    key = _TPL_KEY.format(id=t["id"])
    existing = None
    try:
        raw = await r.get(key)
        if raw:
            existing = _normalise(json.loads(raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw))
    except Exception:
        existing = None
    if existing:
        t["created_at"] = existing.get("created_at") or now_iso()
        t["version"] = int(existing.get("version") or 1) + 1
    else:
        t["created_at"] = now_iso()
        t["version"] = max(1, int(t.get("version") or 1))
    t["updated_at"] = now_iso()
    src = t.get("source") or {}
    if src.get("origin") == "built-in":
        src = dict(src, origin="you", from_builtin=t["id"])
    t["source"] = src
    try:
        await r.set(key, json.dumps(t))
    except Exception as e:
        return {"ok": False, "error": "store failed: %s" % e}
    await emit_event({"type": "widget.template.save", "id": t["id"], "form": t["form"], "created": existing is None})
    return {"ok": True, "template": t, "problems": probs, "created": existing is None}


@capability(
    "widget.template.delete", memory="off",
    http_method="POST", http_path="/ui/widgets/templates/delete", http_tags=["ui", "widgets"],
    description="Delete a saved widget template (its instances stay until removed). A built-in "
                "cannot be deleted. Input: id (str!). Output: {ok, id}.")
async def widget_template_delete(id: str = "", trace_id=None):
    tid = str(id or "").strip()
    if tid.startswith("panel:") or tid.startswith("lhm:"):
        saved = {t["id"] for t in await _load_saved()}
        if tid not in saved:
            return {"ok": False, "error": "%s is built in; save a copy under another id instead" % tid}
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}
    n = await r.delete(_TPL_KEY.format(id=tid))
    if not n:
        return {"ok": False, "error": "no saved template %r" % tid}
    await emit_event({"type": "widget.template.delete", "id": tid})
    return {"ok": True, "id": tid}


@capability(
    "widget.template.instantiate", memory="off",
    http_method="POST", http_path="/ui/widgets/instantiate", http_tags=["ui", "widgets"],
    description="Place a widget: one instance of a template in an envelope. Inputs: id (str! - "
                "the template), where (str! - dashboard | canvas | LHM | reply | notebook | ops "
                "map | iso plate), host (str - the grid / page that holds it, e.g. 'main' for the "
                "harness dashboard), config (object - overrides for the record's frame/draw), "
                "session_id (str - the chat session for session-scoped envelopes). The host page "
                "realises the placement (a dashboard adds a panel-backed widget on its next look). "
                "Output: {ok, instance:{id, template, where, host, config, session_id, created_at}, template}.")
async def widget_template_instantiate(id: str = "", where: str = "", host: str = "", config: Optional[dict] = None,
                                      session_id: str = "", trace_id=None):
    tid, w = str(id or "").strip(), str(where or "").strip()
    if not tid or not w:
        return {"ok": False, "error": "id and where are required"}
    if w not in WHERES:
        return {"ok": False, "error": "unknown placement %r (one of: %s)" % (w, ", ".join(WHERES))}
    got = await widget_template_get(id=tid, trace_id=trace_id)
    if not got.get("ok"):
        return got
    t = got["template"]
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}
    inst = {"id": "inst-" + uuid.uuid4().hex[:10], "template": tid, "name": t["name"], "form": t["form"], "where": w,
            "host": str(host or "")[:64], "config": config if isinstance(config, dict) else {},
            "session_id": str(session_id or trace_id or "")[:80], "panel": (t.get("source") or {}).get("panel") or "",
            "created_at": now_iso()}
    try:
        await r.set(_INST_KEY.format(id=inst["id"]), json.dumps(inst))
    except Exception as e:
        return {"ok": False, "error": "store failed: %s" % e}
    await emit_event({"type": "widget.instance.place", "id": inst["id"], "template": tid, "where": w, "host": inst["host"]})
    return {"ok": True, "instance": inst, "template": t}


@capability(
    "widget.instance.list", memory="off", silent=True,
    http_method="GET", http_path="/ui/widgets/instances", http_tags=["ui", "widgets"],
    description="Placed widgets. Inputs: where (str), host (str), session_id (str), template (str) - "
                "all optional filters. Output: {ok, instances:[{id, template, name, form, where, host, "
                "config, session_id, panel, created_at}], count}.")
async def widget_instance_list(where: str = "", host: str = "", session_id: str = "", template: str = "", trace_id=None):
    out = []
    for i in await _instances():
        if where and i.get("where") != where:
            continue
        if host and i.get("host") != host:
            continue
        if session_id and i.get("session_id") != session_id:
            continue
        if template and i.get("template") != template:
            continue
        out.append(i)
    out.sort(key=lambda i: i.get("created_at") or "")
    return {"ok": True, "instances": out, "count": len(out)}


@capability(
    "widget.instance.remove", memory="off",
    http_method="POST", http_path="/ui/widgets/instances/remove", http_tags=["ui", "widgets"],
    description="Remove a placed widget. Input: id (str! - the instance). Output: {ok, id}.")
async def widget_instance_remove(id: str = "", trace_id=None):
    iid = str(id or "").strip()
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}
    n = await r.delete(_INST_KEY.format(id=iid))
    if not n:
        return {"ok": False, "error": "no instance %r" % iid}
    await emit_event({"type": "widget.instance.remove", "id": iid})
    return {"ok": True, "id": iid}


# ── the registry panel ────────────────────────────────────────────────────────
_PANEL_HTML = """
<div style="height:100%;display:flex;flex-direction:column">
  <iframe src="/ui/widgets/registry" style="flex:1;border:none;width:100%;background:transparent"></iframe>
</div>
"""


@APP.get("/ui/widgets/registry", include_in_schema=False)
async def _widget_registry_page():
    """The registry as a standalone document (the Widgets tab mounts it as an iframe, like the agent registry)."""
    from fastapi.responses import HTMLResponse
    p = Path(__file__).parent / "widget_registry_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:#c96b6b'>widget_registry_panel.html not found</p>")


# mode="element": registered and listed (the picker, the chat's Panels list, a beside-panel) but not a tab by
# itself - it is one registry among the others, opened where it is needed.
register_ui("widget-registry", "Widgets", "⧉", _PANEL_HTML, js="",
            ui_caps=["widget.template.list", "widget.template.get", "widget.template.save",
                     "widget.template.delete", "widget.template.instantiate",
                     "widget.instance.list", "widget.instance.remove"],
            mode="element", tab_order=63)
