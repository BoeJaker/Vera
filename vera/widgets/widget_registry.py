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
    # the calendar panel's parts as widgets (the widget review, round 3 - the owner: "any lhm items that can be made into
    # widgets ... like the calendar controls and even the calendar from the comms ui itself - and the different parts of
    # it like the schedule view on the right"). The calendar panel (and the comms UI, which frames it) reads
    # cal.events.list for a range; these read the same, their range following the month shown.
    {"id": "cal:month", "name": "Month calendar", "form": "month",
     "reads": {"cap": "cal.events.list", "args": {"start": "@month_start", "end": "@month_end"}, "every": "5m", "note": "the month's events, the range following the month shown"},
     "frame": "month . days . events . today", "draw": {"form": "month", "size": "L"},
     "can": ["previous . next . today", "choose a day", "open an event in the drawer"], "placed": ["dashboard", "canvas", "LHM"]},
    {"id": "cal:schedule", "name": "Schedule", "form": "schedule",
     "reads": {"cap": "cal.events.list", "args": {"start": "@today", "end": "@today+14d"}, "every": "5m", "note": "what is coming, by day"},
     "frame": "day . time . title . where", "draw": {"form": "schedule", "size": "M"},
     "can": ["follow the chosen day", "open an event in the drawer"], "placed": ["dashboard", "canvas", "LHM"]},
    {"id": "cal:controls", "name": "Calendar controls", "form": "calnav",
     "reads": {"cap": "", "args": {}, "note": "drives the month and the schedule of its group on the same page"},
     "frame": "previous . month . next . today . view", "draw": {"form": "calnav", "size": "S"},
     "can": ["move the month", "back to today", "switch the view"], "placed": ["dashboard", "canvas", "LHM"]},
    {"id": "cal:todos", "name": "Todos", "form": "checklist",
     "reads": {"cap": "cal.todos.list", "args": {"include_done": False}, "every": "5m", "note": "the calendar's todos"},
     "frame": "title . due . done", "draw": {"form": "checklist", "size": "M"},
     "can": ["open a todo in the drawer"], "placed": ["dashboard", "canvas", "LHM"]},
    # the graphs as widgets: the estate's own graph (veraUI.Graph) in a tile, in any of its display modes
    {"id": "graph:fabric", "name": "Fabric graph", "form": "vgraph",
     "reads": {"cap": "fabric.graphs.snapshot", "args": {"graph": "fabric", "limit": 200}, "every": "5m"},
     "frame": "nodes . edges . modes", "draw": {"form": "vgraph", "size": "L", "motion": ""},
     "can": ["switch the mode", "open a node"], "placed": ["dashboard", "canvas"]},
    {"id": "graph:memory", "name": "Memory graph · Vera graph", "form": "vgraph",
     "reads": {"cap": "memory.graph_full", "args": {"limit_nodes": 300, "limit_edges": 1500}, "every": "5m"},
     "frame": "memories . entities . relations . modes", "draw": {"form": "vgraph", "size": "L"},
     "can": ["switch the mode", "open a node"], "placed": ["dashboard", "canvas"]},
    {"id": "graph:topology", "name": "Stack topology graph", "form": "vgraph",
     "reads": {"cap": "topology.snapshot", "args": {}, "every": "2m"},
     "frame": "the estate as a graph . estate-2d / 3d", "draw": {"form": "vgraph", "size": "L"},
     "can": ["switch the mode", "open a node"], "placed": ["dashboard", "canvas"]},
    {"id": "graph:mesh", "name": "Mesh graph", "form": "vgraph",
     "reads": {"cap": "mesh.topology", "args": {}, "every": "2m"},
     "frame": "the mesh's nodes and links", "draw": {"form": "vgraph", "size": "M"},
     "can": ["switch the mode", "open a node"], "placed": ["dashboard", "canvas"]},
    # the data fabric as widgets (owner, 2026-09-27: "id like some widgets that can display the data fabric and incoming
    # data feeds perhaps the data fabric should have a dashboard and widgets of its own"). Each reads a capability the
    # fabric already answers and maps its envelope to the form (read.map); every part a form draws carries its row, so a
    # click opens it in the drawer. The Fabric panel's Overview composes them (vera/widgets/layouts/fabric.json).
    {"id":"fabric:size","name":"Fabric · size","form":"numbers","reads":{"cap":"fabric.health","args":{},"every":"5m","note":"the catalogue's own counts and the size of its file"},"read":{"map":{"pick":{"records":"records_count","datasets":"datasets_count","sources":"sources_count","on disk":"db_size"}}},"frame":"records . datasets . sources . on disk","draw":{"form":"numbers","size":"M","bytes":["on disk"]},"can":["open a figure in the drawer"],"placed":["dashboard","canvas"]},
    {"id":"fabric:vectors","name":"Vectors · records","form":"numbers","reads":{"cap":"fabric.stats","args":{},"every":"5m","note":"the vector store's count beside the record store's"},"read":{"map":{"pick":{"vectors":"chroma.count","records":"postgres.records"}}},"frame":"chroma vectors . postgres records","draw":{"form":"numbers","size":"S"},"can":["open a figure in the drawer"],"placed":["dashboard","canvas"]},
    {"id":"fabric:stores","name":"Fabric stores","form":"pills","reads":{"cap":"fabric.stats","args":{},"every":"2m","note":"is each backend of the fabric answering"},"read":{"map":{"pick":{"postgres":"postgres.available","chroma":"chroma.available","neo4j":"neo4j.available","sqlite":"sqlite.available","object store":"object_store.available","faiss":"faiss.available"},"entries":"status"}},"frame":"one pill per backend, green when it answers","draw":{"form":"pills","size":"M"},"can":["open a backend in the drawer"],"placed":["dashboard","canvas","LHM"]},
    {"id":"fabric:ingest","name":"Ingest · writer and bus","form":"status","reads":{"cap":"fabric.health","args":{},"every":"1m","note":"the write path every ingest goes through"},"read":{"map":{"pick":{"writer":"writer_task_alive","index migrated":"index_migration_done"}}},"frame":"writer . index","draw":{"form":"status","size":"S"},"can":["open a check in the drawer"],"placed":["dashboard","canvas","LHM"]},
    {"id":"fabric:bus","name":"Fabric bus","form":"status","reads":{"cap":"fabric.bus.status","args":{},"every":"1m","note":"the bus that streams fabric events to subscribers"},"read":{"map":{"pick":{"bus enabled":"enabled","bus task":"task_alive"}}},"frame":"enabled . task","draw":{"form":"status","size":"S"},"can":["open a check in the drawer"],"placed":["dashboard","canvas","LHM"]},
    {"id":"fabric:objects","name":"Blob store","form":"status","reads":{"cap":"fabric.objects.status","args":{},"every":"2m","note":"the object store behind the fabric's artifacts"},"read":{"map":{"pick":{"enabled":"enabled","available":"available"}}},"frame":"enabled . available","draw":{"form":"status","size":"S"},"can":["open a check in the drawer"],"placed":["dashboard","canvas","LHM"]},
    {"id":"fabric:feed-fresh","name":"Feeds · last pulled","form":"table","reads":{"cap":"fabric.sources","args":{},"every":"5m","note":"every registered feed, the most recently pulled first"},"read":{"map":{"rows":"sources"}},"frame":"feed . type . last pulled . pulls . interval","draw":{"form":"table","size":"XL","columns":["label","source_type","last_pulled","pull_count","interval"],"sort":"last_pulled","limit":12},"can":["sort by a column","find a feed","open a feed in the drawer"],"placed":["dashboard","canvas"]},
    {"id":"fabric:feed-types","name":"Feeds · by type","form":"ranked","reads":{"cap":"fabric.sources","args":{},"every":"5m","note":"the registered feeds counted by source type"},"read":{"map":{"values":"sources","count":"source_type"}},"frame":"type . feeds","draw":{"form":"ranked","size":"M"},"can":["open a type in the drawer"],"placed":["dashboard","canvas","LHM"]},
    {"id":"fabric:feed-growth","name":"Feeds per day","form":"column","reads":{"cap":"fabric.sources","args":{},"every":"5m","note":"how the feed list grew: feeds registered per day, oldest first (the whole history)"},"read":{"map":{"values":"sources","count":"created_at","span":"day"}},"frame":"day . feeds registered","draw":{"form":"column","size":"L","limit":120},"can":["open a day in the drawer"],"placed":["dashboard","canvas"]},
    {"id":"fabric:feed-tags","name":"Feeds · by tag","form":"ranked","reads":{"cap":"fabric.tags.list_grouped","args":{},"every":"5m","note":"the tags on the feeds, by how many feeds carry each"},"read":{"map":{"values":"tags","name":"tag","value":"sources"}},"frame":"tag . feeds","draw":{"form":"ranked","size":"M"},"can":["open a tag in the drawer"],"placed":["dashboard","canvas"]},
    {"id":"fabric:dataset-tags","name":"Datasets · by tag","form":"ranked","reads":{"cap":"fabric.tags.list_grouped","args":{},"every":"5m","note":"the tags on the datasets, by how many datasets carry each"},"read":{"map":{"values":"tags","name":"tag","value":"datasets"}},"frame":"tag . datasets","draw":{"form":"ranked","size":"M"},"can":["open a tag in the drawer"],"placed":["dashboard","canvas"]},
    {"id":"fabric:crawls","name":"Discovery crawls","form":"log","reads":{"cap":"fabric.discover.history","args":{},"every":"2m","note":"the web crawls feeding the fabric, the latest first"},"read":{"map":{"events":"crawls","t":"updated_at","kind":"status","text":"seed_url"}},"frame":"when . state . seed","draw":{"form":"log","size":"L"},"can":["open a crawl in the drawer"],"placed":["dashboard","canvas","LHM"]},
    {"id":"fabric:crawl-state","name":"Crawl states","form":"donut","reads":{"cap":"fabric.discover.history","args":{},"every":"2m","note":"the crawls counted by state"},"read":{"map":{"parts":"crawls","count":"status"}},"frame":"state . crawls","draw":{"form":"donut","size":"M","palette":"status"},"can":["open a state in the drawer"],"placed":["dashboard","canvas","LHM"]},
    {"id":"fabric:crawl-days","name":"Crawls per day","form":"column","reads":{"cap":"fabric.discover.history","args":{},"every":"2m","note":"the discovery crawls counted by the day they started"},"read":{"map":{"values":"crawls","count":"created_at","span":"day"}},"frame":"day . crawls started","draw":{"form":"column","size":"L"},"can":["open a day in the drawer"],"placed":["dashboard","canvas"]},
    {"id":"fabric:kbs","name":"Knowledge","form":"rows","reads":{"cap":"fabric.kb.list","args":{},"every":"5m","note":"the knowledgebases built from the fabric"},"read":{"map":{"rows":"knowledgebases"}},"frame":"subject . facts . state","draw":{"form":"rows","size":"M","columns":["subject","fact_count","status"],"limit":8},"can":["open a knowledgebase in the drawer"],"placed":["dashboard","canvas","LHM"]},
    {"id":"fabric:graphs","name":"Fabric graphs","form":"pills","reads":{"cap":"fabric.graphs.list","args":{},"every":"5m","note":"the graphs registered on the fabric and whether each answers"},"read":{"map":{"values":"graphs","status":"available"}},"frame":"graph . available","draw":{"form":"pills","size":"M"},"can":["open a graph in the drawer"],"placed":["dashboard","canvas","LHM"]},
    {"id":"fabric:skills","name":"Fabric skills","form":"rows","reads":{"cap":"fabric.skills.list","args":{},"every":"5m","note":"the skills built over the fabric's datasets"},"read":{"map":{"rows":"skills"}},"frame":"skill . updated","draw":{"form":"rows","size":"M","columns":["name","updated_at"],"limit":6},"can":["open a skill in the drawer"],"placed":["dashboard","canvas","LHM"]},
    # the image studio as widgets (owner, 2026-09-27: "can we have a widget for displaying sprites and characters and images
    # from the image studio"). Each reads a capability the studio already answers - the stored generations (images.list),
    # the gallery (gallery.list), the sprite characters (spritegen.list) and the companions (character.list) - and draws it
    # with the studio's forms (images . sprite . sprites . character); every picture, sprite and character is an item, so a
    # click opens it in the drawer, large. The Image Studio's Overview composes them (vera/widgets/layouts/studio.json).
    {"id": "image:recent", "name": "Recent images", "form": "images",
     "reads": {"cap": "images.list", "args": {"limit": 48}, "every": "2m", "note": "the latest generations, newest first"},
     "read": {"map": {"rows": "images"}},
     "frame": "thumbnails . prompt . size . source . when", "draw": {"form": "images", "size": "L"},
     "can": ["open an image in the drawer", "view as a table"], "placed": ["dashboard", "canvas", "LHM"]},
    {"id": "image:wall", "name": "Image wall", "form": "images",
     "reads": {"cap": "images.list", "args": {"limit": 120}, "every": "5m", "note": "every stored generation, scrolling"},
     "read": {"map": {"rows": "images"}},
     "frame": "thumbnails . prompt . size . source . when", "draw": {"form": "images", "size": "XL", "thumb": 120},
     "can": ["open an image in the drawer"], "placed": ["dashboard", "canvas"]},
    {"id": "image:gallery", "name": "Gallery", "form": "images",
     "reads": {"cap": "gallery.list", "args": {}, "every": "5m", "note": "what was saved to the gallery - pictures, and reports as tiles of their kind"},
     "read": {"map": {"rows": "items"}},
     "frame": "thumbnail or kind . title", "draw": {"form": "images", "size": "M"},
     "can": ["open an item in the drawer"], "placed": ["dashboard", "canvas", "LHM"]},
    {"id": "sprite:library", "name": "Sprite library", "form": "sprites",
     "reads": {"cap": "spritegen.list", "args": {}, "every": "5m", "note": "every sprite character, its idle animation playing under the pointer"},
     "frame": "sprite . name . animations . size", "draw": {"form": "sprites", "size": "L"},
     "can": ["play a sprite under the pointer", "open a sprite in the drawer"], "placed": ["dashboard", "canvas"]},
    {"id": "sprite:featured", "name": "Sprite", "form": "sprite",
     "reads": {"cap": "spritegen.list", "args": {}, "every": "5m", "note": "one sprite animated (draw.id names it, else the first with a sheet)"},
     "frame": "the sheet animated . its animations", "draw": {"form": "sprite", "size": "M", "play": "auto"},
     "can": ["switch the animation", "hold a frame under the pointer", "open the sprite in the drawer"], "placed": ["dashboard", "canvas", "LHM"]},
    {"id": "sprite:companion", "name": "Companion sprite", "form": "sprite",
     "reads": {"cap": "character.list", "args": {}, "every": "5m", "note": "a companion character's sprite sheet, animated"},
     "frame": "the sheet animated . its animations", "draw": {"form": "sprite", "size": "M", "play": "auto"},
     "can": ["switch the animation", "open the sprite in the drawer"], "placed": ["dashboard", "canvas", "LHM"]},
    {"id": "character:card", "name": "Character", "form": "character",
     "reads": {"cap": "character.list", "args": {}, "every": "5m", "note": "one companion (draw.id names it, else the first with a portrait)"},
     "frame": "portrait . name . traits . expressions . sprites", "draw": {"form": "character", "size": "L"},
     "can": ["open the character, an expression or a sprite in the drawer"], "placed": ["dashboard", "canvas", "LHM"]},
    {"id": "character:roster", "name": "Characters", "form": "character",
     "reads": {"cap": "character.list", "args": {}, "every": "5m", "note": "every companion as a small card"},
     "frame": "portrait . name . style . voice", "draw": {"form": "character", "size": "XL", "roster": True},
     "can": ["open a character in the drawer"], "placed": ["dashboard", "canvas"]},
]


def _looplab_templates() -> List[Dict[str, Any]]:
    """The Loop Lab's widgets as templates any surface can place (the canvas's add bar, a dashboard's widget sheet):
    read from the lens layout files (layouts/looplab*.json - the one place they are written), one template per
    distinct reading. Best-effort: a missing or unreadable file leaves the registry as it was."""
    import json as _json
    from pathlib import Path as _Path
    out: List[Dict[str, Any]] = []
    seen = set()
    try:
        files = sorted((_Path(__file__).parent / "layouts").glob("looplab*.json"))
    except Exception:
        return out
    for f in files:
        try:
            lay = _json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        lens = str(lay.get("name") or lay.get("key") or "")
        for t in lay.get("widgets") or []:
            r = (t or {}).get("record") or {}
            rd = r.get("read") or {}
            dr = r.get("draw") or {}
            sig = (r.get("form"), r.get("source"), _json.dumps(rd.get("args") or {}, sort_keys=True), dr.get("tag", ""))
            if not r.get("form") or sig in seen:
                continue
            seen.add(sig)
            rid = str(r.get("id") or "")
            rid = rid[len(str(lay.get("key") or "")) + 1:] if rid.startswith(str(lay.get("key") or "") + "-") else rid
            tid = "looplab:" + _slug(str(lay.get("key") or "").replace("looplab", "").strip("-") or "live") + "-" + _slug(rid)
            draw = {"form": r.get("form"), "size": str((r.get("frame") or {}).get("size") or "m").upper()}
            draw.update({k: v for k, v in dr.items() if k in ("tag", "attrs")})
            out.append({"id": tid, "name": r.get("title") or tid, "form": r.get("form"),
                        "reads": {"cap": r.get("source") or "", "args": rd.get("args") or {}, "every": rd.get("refresh") or "",
                                  "note": "the Loop Lab's " + lens + " lens"},
                        "read": {"map": rd.get("map") or {}}, "frame": lens, "draw": draw,
                        "can": ["open a run, a lane, a test or a card in the drawer", "place it on the canvas"],
                        "placed": ["dashboard", "canvas"]})
    return out


_LHM_BUILTINS.extend(_looplab_templates())


def _builtin_lhm() -> List[Dict[str, Any]]:
    out = []
    for b in _LHM_BUILTINS:
        t = _normalise(dict(b))
        t["source"] = {"origin": "built-in", "from": "the data fabric" if t["id"].startswith("fabric:") else ("the Loop Lab" if t["id"].startswith("looplab:") else ("the image studio" if t["id"].startswith(("image:", "sprite:", "character:")) else "the chat LHM")), "panel": ""}
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


@APP.get("/ui/widgets/gallery", include_in_schema=False)
async def _widget_gallery_page():
    """The gallery view of the same document: every form of the catalogue drawn from its sample at the size picked
    (the Widgets, WidgetsMotion and WidgetsIso boards as one gallery). The page reads its path and opens on the gallery."""
    from fastapi.responses import HTMLResponse
    p = Path(__file__).parent / "widget_registry_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:#c96b6b'>widget_registry_panel.html not found</p>")


_GALLERY_HTML = """
<div style="height:100%;display:flex;flex-direction:column">
  <iframe src="/ui/widgets/gallery" style="flex:1;border:none;width:100%;background:transparent"></iframe>
</div>
"""

# mode="element": registered and listed (the picker, the chat's Panels list, a beside-panel) but not a tab by
# itself - it is one registry among the others, opened where it is needed.
register_ui("widget-registry", "Widgets", "⧉", _PANEL_HTML, js="",
            ui_caps=["widget.template.list", "widget.template.get", "widget.template.save",
                     "widget.template.delete", "widget.template.instantiate",
                     "widget.instance.list", "widget.instance.remove"],
            mode="element", tab_order=63)
# the gallery: every form of the catalogue at its sizes, drawn from its sample - how a form is judged before it is placed
register_ui("widget-gallery", "Widget gallery", "▦", _GALLERY_HTML, js="",
            ui_caps=["widget.forms", "widget.sources", "widget.validate", "widget.template.save"],
            mode="element", tab_order=64)
