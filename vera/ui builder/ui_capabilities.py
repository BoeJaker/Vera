"""
ui_capabilities.py  —  UI System Capabilities
=================================================
Provides:
  1. Unified theme system — 5 shared themes (ash, dusk, void, chalk, ice)
     plus custom themes, stored in Redis, broadcast to all UIs via event
  2. UI panel CRUD — LLMs can list, create, update, delete UI panels
  3. Capability access control — whitelist/blacklist caps per scope
     (dag_builder, ui_builder, agent, general)
  4. Panel browser — view all registered panels, their capabilities, status

Capabilities:
  ui.themes            — list available themes with their CSS variables
  ui.theme.get         — get current active theme
  ui.theme.set         — set active theme (broadcasts to all UIs)
  ui.theme.create      — create a custom theme
  ui.theme.css         — get theme CSS (servable as a stylesheet)
  ui.panel.list        — list all registered UI panels with metadata
  ui.panel.get         — get a panel's HTML/JS/metadata
  ui.panel.create      — create a new panel (LLM can build UI)
  ui.panel.update      — update a panel's HTML/JS
  ui.panel.delete      — delete a dynamic panel
  ui.caps.acl          — get/set capability access control lists
  ui.caps.scopes       — list available scopes and their cap lists
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, UI_PANELS,
    capability, emit_event, now_iso, register_ui, schedule,
)

log = logging.getLogger("vera.ui")

def _redis():
    return _orch.REDIS

# ─────────────────────────────────────────────────────────────────────────────
# THEME SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

# Theme tables live in Vera.vera.theme_defs (single source of truth shared with
# chat/chat_panels_capabilities.py). This module serves /ui/themes.css from the
# same table, so the stylesheet and the JSON theme API stay in lockstep.
from Vera.vera.theme_defs import (
    BUILTIN_THEMES, DEFAULT_THEME, ensure_contrast,
)

# Custom themes stored at runtime (persisted to Redis if available)
CUSTOM_THEMES: Dict[str, dict] = {}

# Current active theme
_ACTIVE_THEME = DEFAULT_THEME


def _all_themes() -> Dict[str, dict]:
    merged = dict(BUILTIN_THEMES)
    merged.update(CUSTOM_THEMES)
    return merged


def _theme_to_css(theme_id: str) -> str:
    """Generate CSS variable block for a theme."""
    themes = _all_themes()
    t = themes.get(theme_id)
    if not t:
        return ""
    vars_css = "; ".join(f"{k}:{v}" for k, v in t["vars"].items())
    return f'[data-theme="{theme_id}"] {{ {vars_css} }}'


def _all_themes_css() -> str:
    """Generate all theme CSS for injection."""
    lines = []
    for tid in _all_themes():
        lines.append(_theme_to_css(tid))
    return "\n".join(lines)


# ── Theme capabilities ───────────────────────────────────────────────────────

@capability(
    "ui.themes",
    http_method="GET", http_path="/ui/themes", http_tags=["ui"],
    memory="off", silent=True,
    description="List all available themes with their CSS variables and metadata.",
)
async def cap_themes(trace_id=None):
    themes = _all_themes()
    return {
        "active": _ACTIVE_THEME,
        "themes": {
            tid: {
                "id": tid,
                "label": t["label"],
                "type": t["type"],
                "accent": t["accent"],
                "builtin": tid in BUILTIN_THEMES,
                "vars": t["vars"],
            }
            for tid, t in themes.items()
        },
    }


@capability(
    "ui.theme.get",
    http_method="GET", http_path="/ui/theme", http_tags=["ui"],
    memory="off", silent=True,
    description="Get current active theme ID and its CSS variables.",
)
async def cap_theme_get(trace_id=None):
    themes = _all_themes()
    t = themes.get(_ACTIVE_THEME, themes.get("dusk", {}))
    return {"theme": _ACTIVE_THEME, "vars": t.get("vars", {}), "type": t.get("type", "dark")}


@capability(
    "ui.theme.set",
    http_method="POST", http_path="/ui/theme/set", http_tags=["ui"],
    memory="off", silent=True,
    description="Set the active theme. Broadcasts to all connected UIs. "
                "Input: theme (str — theme ID).",
)
async def cap_theme_set(theme: str, trace_id=None):
    global _ACTIVE_THEME
    themes = _all_themes()
    if theme not in themes:
        return {"error": f"Unknown theme: {theme}. Available: {list(themes.keys())}"}
    _ACTIVE_THEME = theme
    # Persist to Redis
    r = _redis()
    if r:
        try:
            await r.set("vera:ui:theme", theme)
        except Exception:
            pass
    # Broadcast to all UIs
    await emit_event({
        "type": "ui.theme.changed",
        "theme": theme,
        "vars": themes[theme]["vars"],
    })
    return {"theme": _ACTIVE_THEME, "vars": themes[theme]["vars"]}


@capability(
    "ui.theme.create",
    http_method="POST", http_path="/ui/theme/create", http_tags=["ui"],
    memory="off", silent=True,
    description="Create a custom theme. Input: id (str), label (str), "
                "type (light|dark), accent (hex color), vars (JSON object of CSS vars).",
)
async def cap_theme_create(id: str, label: str = "", type: str = "dark",
                            accent: str = "#5a9e8f", vars: str = "{}",
                            trace_id=None):
    try:
        var_dict = json.loads(vars) if isinstance(vars, str) else vars
    except Exception:
        return {"error": "vars must be valid JSON object"}
    if not id or not id.isalnum():
        return {"error": "id must be alphanumeric"}
    # Repair unreadable colour choices (text ~ background, weak on-accent) so a
    # saved custom theme is always legible.
    var_dict = ensure_contrast(var_dict)
    CUSTOM_THEMES[id] = {
        "label": label or id,
        "type": type,
        "accent": accent,
        "vars": var_dict,
    }
    # Persist
    r = _redis()
    if r:
        try:
            await r.set(f"vera:ui:theme:{id}", json.dumps(CUSTOM_THEMES[id]))
        except Exception:
            pass
    return {"id": id, "created": True}


@capability(
    "ui.theme.delete",
    http_method="POST", http_path="/ui/theme/delete", http_tags=["ui"],
    memory="off", silent=True,
    description="Delete a custom theme by ID. Built-in themes (ash, dusk, void, chalk, ice) "
                "cannot be deleted. If the deleted theme is active, switches to 'dusk'. "
                "Input: id (str — theme ID to delete).",
)
async def cap_theme_delete(id: str, trace_id=None):
    if id in BUILTIN_THEMES:
        return {"error": f"Cannot delete built-in theme: {id}. "
                         f"Built-in themes: {list(BUILTIN_THEMES.keys())}"}
    if id not in CUSTOM_THEMES:
        return {"error": f"Theme not found: {id}. "
                         f"Custom themes: {list(CUSTOM_THEMES.keys())}"}
    global _ACTIVE_THEME
    del CUSTOM_THEMES[id]
    # Remove from Redis
    r = _redis()
    if r:
        try:
            await r.delete(f"vera:ui:theme:{id}")
        except Exception:
            pass
    # If it was active, fall back to dusk
    if _ACTIVE_THEME == id:
        _ACTIVE_THEME = "dusk"
        fallback = BUILTIN_THEMES["dusk"]
        await emit_event({
            "type": "ui.theme.changed",
            "theme": "dusk",
            "vars": fallback["vars"],
        })
    await emit_event({"type": "ui.theme.deleted", "id": id})
    return {"deleted": True, "id": id, "active": _ACTIVE_THEME}


@capability(
    "ui.theme.css",
    http_method="GET", http_path="/ui/theme/css", http_tags=["ui"],
    memory="off", silent=True,
    description="Get all theme definitions as a CSS stylesheet string.",
)
async def cap_theme_css(trace_id=None):
    return {"css": _all_themes_css()}


# Serve the CSS as a proper stylesheet endpoint
@APP.get("/ui/themes.css", include_in_schema=False)
async def _serve_theme_css():
    from fastapi.responses import Response
    css = _all_themes_css()
    # Off the critical path (active theme is painted from the inline var cache),
    # so a short shared cache is safe and spares every iframe a fresh fetch.
    # Custom-theme edits reflect within the window; the picker reads /ui/themes.
    return Response(content=css, media_type="text/css",
                    headers={"Cache-Control": "public, max-age=300"})


# Serve vera-ui.js — the universal theme integration script
@APP.get("/ui/vera-ui.js", include_in_schema=False)
async def _serve_vera_ui_js():
    from fastapi.responses import Response
    from pathlib import Path
    p = Path(__file__).parent.parent / "vera-ui.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})
    return Response(content="console.warn('vera-ui.js not found');",
                    media_type="application/javascript")


# Serve vera-panel.css — the canonical LHM + form-control chrome
@APP.get("/ui/vera-panel.css", include_in_schema=False)
async def _serve_vera_panel_css():
    from fastapi.responses import Response
    from pathlib import Path
    p = Path(__file__).parent.parent / "vera-panel.css"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="text/css",
                        headers={"Cache-Control": "no-cache"})
    return Response(content="/* vera-panel.css not found */",
                    media_type="text/css")


# Serve vera-panel.js — canonical LHM collapse + collapsible-section behavior
@APP.get("/ui/vera-panel.js", include_in_schema=False)
async def _serve_vera_panel_js():
    from fastapi.responses import Response
    from pathlib import Path
    p = Path(__file__).parent.parent / "vera-panel.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})
    return Response(content="console.warn('vera-panel.js not found');",
                    media_type="application/javascript")


# Serve vera-loader.js — configurable loading animation (default: evolving graph)
@APP.get("/ui/vera-loader.js", include_in_schema=False)
async def _serve_vera_loader_js():
    from fastapi.responses import Response
    from pathlib import Path
    p = Path(__file__).parent.parent / "vera-loader.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})
    return Response(content="console.warn('vera-loader.js not found');",
                    media_type="application/javascript")


# ── Loading-animation config (mirrors the theme API) ─────────────────────────
_LOADER_DEFAULT = {"type": "graph", "speed": 1, "density": 1, "sprite": None}
_LOADER_CFG: Dict = dict(_LOADER_DEFAULT)
_LOADER_LOADED = False


async def _loader_cfg() -> Dict:
    global _LOADER_LOADED, _LOADER_CFG
    if not _LOADER_LOADED:
        r = _redis()
        if r:
            try:
                raw = await r.get("vera:ui:loader")
                if raw:
                    _LOADER_CFG = {**_LOADER_DEFAULT,
                                   **json.loads(raw if isinstance(raw, str) else raw.decode())}
            except Exception:
                pass
        _LOADER_LOADED = True
    return _LOADER_CFG


@capability(
    "ui.loader.get",
    http_method="GET", http_path="/ui/loader", http_tags=["ui"],
    memory="off", silent=True,
    description="Get the active loading-animation config {type,speed,density,"
                "sprite}. type ∈ graph|pulse|orbit|spinner|sprite.",
)
async def cap_loader_get(trace_id=None):
    return {"config": await _loader_cfg(),
            "animations": ["graph", "pulse", "orbit", "spinner", "sprite"]}


@capability(
    "ui.loader.set",
    http_method="POST", http_path="/ui/loader/set", http_tags=["ui"],
    memory="off", silent=True,
    description="Set the loading animation shown across the UI. Broadcasts to all "
                "panels. Inputs: type (str — graph|pulse|orbit|spinner|sprite), "
                "speed (float=1), density (float=1), sprite (dict — {url,frames,"
                "fps,w,h} for type=sprite; url should be a data: URI under CSP). "
                "Output: {config}.",
)
async def cap_loader_set(type: str = "", speed: Optional[float] = None,
                         density: Optional[float] = None,
                         sprite: Optional[Dict] = None, trace_id=None):
    cfg = dict(await _loader_cfg())
    if type:
        if type not in ("graph", "pulse", "orbit", "spinner", "sprite"):
            return {"error": f"unknown loader type: {type}"}
        cfg["type"] = type
    if speed is not None:
        cfg["speed"] = max(0.1, min(4.0, float(speed)))
    if density is not None:
        cfg["density"] = max(0.3, min(3.0, float(density)))
    if sprite is not None:
        cfg["sprite"] = sprite or None
    global _LOADER_CFG
    _LOADER_CFG = cfg
    r = _redis()
    if r:
        try:
            await r.set("vera:ui:loader", json.dumps(cfg))
        except Exception:
            pass
    await emit_event({"type": "ui.loader.changed", "config": cfg})
    return {"config": cfg}


# ── UI scale (global zoom) — mirrors the theme/loader API ─────────────────────
# A single magnification factor applied to every panel (CSS `zoom` on <html> in
# vera-ui.js). localStorage is the live per-device store; this is the persistence
# seed for fresh browsers and the broadcast channel for programmatic changes.
_UI_SCALE_DEFAULT = 1.0
_UI_SCALE: float = _UI_SCALE_DEFAULT


def _clamp_scale(v) -> float:
    try:
        v = float(v)
    except Exception:
        return _UI_SCALE_DEFAULT
    if v <= 0:
        return _UI_SCALE_DEFAULT
    return round(max(0.6, min(2.0, v)), 2)


@capability(
    "ui.scale.get",
    http_method="GET", http_path="/ui/scale", http_tags=["ui"],
    memory="off", silent=True,
    description="Get the global UI scale (zoom) factor. 1.0 = 100%.",
)
async def cap_scale_get(trace_id=None):
    return {"scale": _UI_SCALE, "min": 0.6, "max": 2.0, "step": 0.1}


@capability(
    "ui.scale.set",
    http_method="POST", http_path="/ui/scale/set", http_tags=["ui"],
    memory="off", silent=True,
    description="Set the global UI scale (zoom) applied across every panel. "
                "Input: scale (float, 0.6–2.0; 1.0 = 100%). Broadcasts to all UIs.",
)
async def cap_scale_set(scale: float, trace_id=None):
    global _UI_SCALE
    _UI_SCALE = _clamp_scale(scale)
    r = _redis()
    if r:
        try:
            await r.set("vera:ui:scale", str(_UI_SCALE))
        except Exception:
            pass
    await emit_event({"type": "ui.scale.changed", "scale": _UI_SCALE})
    return {"scale": _UI_SCALE}


# Serve vera-graph.js — the unified reusable graph element
@APP.get("/ui/vera-graph.js", include_in_schema=False)
async def _serve_vera_graph_js():
    from fastapi.responses import Response
    from pathlib import Path
    p = Path(__file__).parent.parent / "vera_graph.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript")
    return Response(content="console.warn('vera-graph.js not found');",
                    media_type="application/javascript")


# Serve agent_loop_output.js — the <vera-agent-loop-output> custom element
@APP.get("/ui/elements/agent_loop_output.js", include_in_schema=False)
async def _serve_agent_loop_output_js():
    from fastapi.responses import Response
    from pathlib import Path
    _here = Path(__file__).parent
    # Try canonical name first, then the legacy typo filename
    for name in ("agent_loop_output_element.js", "agent_loop_ouput.js"):
        p = _here.parent / name
        if p.exists():
            return Response(content=p.read_text(encoding="utf-8"),
                            media_type="application/javascript",
                            headers={"Cache-Control": "no-cache"})
    return Response(
        content="console.warn('agent_loop_output element JS not found');",
        media_type="application/javascript"
    )


# Serve vera_markdown.js — the shared <vera-markdown> renderer (+ VeraMD.render)
# with its built-in rendered/raw toggle. Used anywhere a panel shows markdown
# content (project context, goal plans, loop-run modals, artifacts, reports).
@APP.get("/ui/elements/vera_markdown.js", include_in_schema=False)
async def _serve_vera_markdown_js():
    from fastapi.responses import Response
    from pathlib import Path
    p = Path(__file__).parent.parent / "vera_markdown_element.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})
    return Response(
        content="console.warn('vera_markdown element JS not found');",
        media_type="application/javascript"
    )


# Serve loop_graph.js — the <vera-loop-graph> live agentic-loop activity graph.
@APP.get("/ui/elements/loop_graph.js", include_in_schema=False)
async def _serve_loop_graph_js():
    from fastapi.responses import Response
    from pathlib import Path
    p = Path(__file__).parent.parent / "loop_graph_element.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})
    return Response(
        content="console.warn('loop_graph element JS not found');",
        media_type="application/javascript"
    )


# Serve loop_throbber.js — the <vera-wiremesh-throbber> "the loop is doing
# work" indicator: a small wireframe polygon whose vertices deform in place
# continuously while active. Deliberately its own tiny element (not folded
# into <vera-agent-loop-output>) so ANY panel that launches a loop — chat,
# dream, netmap, dag workshop — can drop one somewhere persistently visible
# (e.g. the top bar) rather than relying on the many per-step spinners inside
# the scrolling loop transcript, which age out of view as a long run goes on.
@APP.get("/ui/elements/loop_throbber.js", include_in_schema=False)
async def _serve_loop_throbber_js():
    from fastapi.responses import Response
    from pathlib import Path
    p = Path(__file__).parent.parent / "loop_throbber_element.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})
    return Response(
        content="console.warn('loop_throbber element JS not found');",
        media_type="application/javascript"
    )


# Serve sparkline_element.js — the <vera-sparkline> dashboard history sparkline.
@APP.get("/ui/elements/sparkline.js", include_in_schema=False)
async def _serve_sparkline_js():
    from fastapi.responses import Response
    from pathlib import Path
    p = Path(__file__).parent.parent / "sparkline_element.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})
    return Response(
        content="console.warn('sparkline element JS not found');",
        media_type="application/javascript"
    )


# Serve topology_map_element.js — the <vera-topology-map> live SVG stack map.
@APP.get("/ui/elements/topology_map.js", include_in_schema=False)
async def _serve_topology_map_js():
    from fastapi.responses import Response
    from pathlib import Path
    p = Path(__file__).parent.parent / "topology_map_element.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})
    return Response(
        content="console.warn('topology_map element JS not found');",
        media_type="application/javascript"
    )


# Serve sandbox_controls.js — the <vera-sandbox-controls> exec-sandbox editor.
# Shared by the Exec panel and the IDE panel; both edit the one server-side
# policy (exec.sandbox.get/set), so the controls stay linked.
@APP.get("/ui/elements/sandbox_controls.js", include_in_schema=False)
async def _serve_sandbox_controls_js():
    from fastapi.responses import Response
    from pathlib import Path
    p = Path(__file__).parent.parent / "sandbox_controls_element.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})
    return Response(
        content="console.warn('sandbox_controls element JS not found');",
        media_type="application/javascript"
    )


# Serve agent_loop_config.js — the <vera-agent-loop-config> unified config control.
# Shared by every panel that launches the agentic loop (dream, netmap, chat, dag
# workshop, …) so they all expose the SAME complete config surface.
@APP.get("/ui/elements/agent_loop_config.js", include_in_schema=False)
async def _serve_agent_loop_config_js():
    from fastapi.responses import Response
    from pathlib import Path
    p = Path(__file__).parent.parent / "agent_loop_config_element.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})
    return Response(
        content="console.warn('agent_loop_config element JS not found');",
        media_type="application/javascript"
    )


# Serve character.js — the <vera-character> animated companion element.
@APP.get("/ui/elements/character.js", include_in_schema=False)
async def _serve_character_js():
    from fastapi.responses import Response
    from pathlib import Path
    p = Path(__file__).parent.parent / "character" / "character_element.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})
    return Response(
        content="console.warn('character element JS not found');",
        media_type="application/javascript"
    )


# Serve the UI builder panel
@APP.get("/ui/builder/panel", include_in_schema=False)
async def _serve_builder_panel():
    from fastapi.responses import HTMLResponse
    from pathlib import Path
    p = Path(__file__).parent / "ui_builder_panel.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<p style='color:red'>ui_builder_panel.html not found</p>")


_IFRAME_STYLE = (
    "border:none;width:100%;height:100%;flex:1;min-height:0;"
    "background:transparent;"
)

register_ui(
    "ui-builder",
    "UI Builder",
    "⎈",
    f'<div style="height:100%;display:flex;flex-direction:column;">'
    f'<iframe src="/ui/builder/panel" style="{_IFRAME_STYLE}" '
    f'allow="clipboard-read; clipboard-write"></iframe></div>',
    "",
    ui_caps=[
        "ui.themes", "ui.theme.get", "ui.theme.set", "ui.theme.create", "ui.theme.css",
        "ui.panel.list", "ui.panel.get", "ui.panel.create", "ui.panel.update", "ui.panel.delete",
        "ui.caps.acl", "ui.caps.scopes", "ui.caps.allowed",
    ],
    mode="tab",
    tab_order=85,
)


# Dynamic panels created at runtime (vs register_ui which is done at import time)
DYNAMIC_PANELS: Dict[str, dict] = {}


@capability(
    "ui.panel.list",
    http_method="GET", http_path="/ui/panel/list", http_tags=["ui"],
    memory="off", silent=True,
    description="List all registered UI panels with metadata. "
                "Includes both built-in (registered via register_ui) and dynamic panels.",
)
async def cap_panel_list(trace_id=None):
    panels = []
    for pid, p in UI_PANELS.items():
        panels.append({
            "id": pid,
            "label": p.get("label", pid),
            "icon": p.get("icon", ""),
            "mode": p.get("mode", "inject"),
            "tab_order": p.get("tab_order", 100),
            "ui_caps": p.get("ui_caps", []),
            "dynamic": False,
            "has_html": bool(p.get("html")),
            "has_js": bool(p.get("js")),
            "html_length": len(p.get("html", "")),
        })
    for pid, p in DYNAMIC_PANELS.items():
        if pid not in UI_PANELS:  # don't double-list
            panels.append({
                "id": pid,
                "label": p.get("label", pid),
                "icon": p.get("icon", ""),
                "mode": p.get("mode", "tab"),
                "tab_order": p.get("tab_order", 200),
                "ui_caps": p.get("ui_caps", []),
                "dynamic": True,
                "has_html": bool(p.get("html")),
                "has_js": bool(p.get("js")),
                "html_length": len(p.get("html", "")),
            })
    return {"panels": panels, "count": len(panels)}


@capability(
    "ui.panel.get",
    http_method="GET", http_path="/ui/panel/get", http_tags=["ui"],
    memory="off", silent=True,
    description="Get a panel's full definition including HTML, JS, metadata. "
                "Input: id (str — panel ID).",
)
async def cap_panel_get(id: str, trace_id=None):
    p = UI_PANELS.get(id) or DYNAMIC_PANELS.get(id)
    if not p:
        return {"error": f"Panel not found: {id}"}
    return {
        "id": id,
        "label": p.get("label", id),
        "icon": p.get("icon", ""),
        "mode": p.get("mode", "inject"),
        "tab_order": p.get("tab_order", 100),
        "ui_caps": p.get("ui_caps", []),
        "html": p.get("html", ""),
        "js": p.get("js", ""),
        "dynamic": id in DYNAMIC_PANELS,
    }


@capability(
    "ui.panel.create",
    http_method="POST", http_path="/ui/panel/create", http_tags=["ui"],
    memory="off",
    description="Create a new UI panel. LLMs can use this to build custom UI elements. "
                "Input: id (str!), label (str), html (str — HTML content), "
                "js (str — JavaScript), mode (inject|tab), "
                "tab_order (int), ui_caps (comma-sep cap names). "
                "Output: {id, registered}.",
)
async def cap_panel_create(id: str, label: str = "", html: str = "",
                            js: str = "", mode: str = "tab",
                            tab_order: int = 200, ui_caps: str = "",
                            icon: str = "", trace_id=None):
    if not id:
        return {"error": "id is required"}
    cap_list = [c.strip() for c in ui_caps.split(",") if c.strip()] if ui_caps else []

    panel = {
        "id": id,
        "label": label or id,
        "icon": icon,
        "html": html,
        "js": js,
        "ui_caps": cap_list,
        "mode": mode,
        "tab_order": tab_order,
    }
    DYNAMIC_PANELS[id] = panel
    # Also register via register_ui so it appears in /ui/panels
    register_ui(id, label or id, icon, html, js,
                ui_caps=cap_list, mode=mode, tab_order=tab_order)

    # Persist to Redis
    r = _redis()
    if r:
        try:
            await r.set(f"vera:ui:panel:{id}", json.dumps(panel))
        except Exception:
            pass

    await emit_event({"type": "ui.panel.created", "id": id, "label": label or id})
    return {"id": id, "registered": True}


@capability(
    "ui.panel.update",
    http_method="POST", http_path="/ui/panel/update", http_tags=["ui"],
    memory="off",
    description="Update an existing panel's HTML and/or JS. "
                "Input: id (str!), html (str, optional), js (str, optional), "
                "label (str, optional). Output: {id, updated}.",
)
async def cap_panel_update(id: str, html: str = "", js: str = "",
                            label: str = "", trace_id=None):
    p = UI_PANELS.get(id) or DYNAMIC_PANELS.get(id)
    if not p:
        return {"error": f"Panel not found: {id}"}
    if html:
        p["html"] = html
    if js:
        p["js"] = js
    if label:
        p["label"] = label
    # Update in both registries
    if id in UI_PANELS:
        UI_PANELS[id] = p
    DYNAMIC_PANELS[id] = p

    # Persist
    r = _redis()
    if r:
        try:
            await r.set(f"vera:ui:panel:{id}", json.dumps(p))
        except Exception:
            pass

    await emit_event({"type": "ui.panel.updated", "id": id})
    return {"id": id, "updated": True}


@capability(
    "ui.panel.delete",
    http_method="POST", http_path="/ui/panel/delete", http_tags=["ui"],
    memory="off",
    description="Delete a dynamic panel. Built-in panels cannot be deleted. "
                "Input: id (str!). Output: {id, deleted}.",
)
async def cap_panel_delete(id: str, trace_id=None):
    if id not in DYNAMIC_PANELS:
        return {"error": f"Cannot delete: {id} is not a dynamic panel"}
    DYNAMIC_PANELS.pop(id, None)
    UI_PANELS.pop(id, None)
    r = _redis()
    if r:
        try:
            await r.delete(f"vera:ui:panel:{id}")
        except Exception:
            pass
    await emit_event({"type": "ui.panel.deleted", "id": id})
    return {"id": id, "deleted": True}


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITY ACCESS CONTROL
# ─────────────────────────────────────────────────────────────────────────────

# Scopes define what capabilities are available for specific use-cases
# Each scope has a whitelist (allowed) and blacklist (denied)
# If whitelist is empty, all caps are allowed except blacklisted
# If whitelist is non-empty, only those caps are allowed
CAP_ACL: Dict[str, dict] = {
    "general": {
        "label": "General LLM use",
        "whitelist": [],  # empty = all allowed
        "blacklist": [
            # Infrastructure — never expose to general LLM
            "obs.health", "obs.cluster", "obs.redis", "obs.events",
            "health.check", "cluster.status",
            "ui.panel.delete", "ui.panel.update",
        ],
    },
    "dag_builder": {
        "label": "DAG workflow builder",
        "whitelist": [],  # all caps available for DAG nodes
        "blacklist": [
            "ui.panel.create", "ui.panel.delete", "ui.panel.update",
        ],
    },
    "ui_builder": {
        "label": "UI panel builder (LLM)",
        "whitelist": [
            "ui.panel.create", "ui.panel.update", "ui.panel.list", "ui.panel.get",
            "ui.themes", "ui.theme.get", "ui.theme.set", "ui.theme.create",
            "ui.caps.acl", "ui.caps.scopes",
            "fabric.datasets", "fabric.query", "fabric.stats",
            "memory.search", "memory.recall",
            "llm.generate", "echo",
        ],
        "blacklist": [],
    },
    "agent": {
        "label": "Agent tool use",
        "whitelist": [],  # all available
        "blacklist": [
            "ui.panel.delete",
            "fabric.reset",
        ],
    },
}


@capability(
    "ui.caps.acl",
    http_method="POST", http_path="/ui/caps/acl", http_tags=["ui"],
    memory="off", silent=True,
    description="Get or set capability access control for a scope. "
                "Input: scope (str!), whitelist (comma-sep, optional), "
                "blacklist (comma-sep, optional). "
                "If only scope is provided, returns current ACL. "
                "If whitelist/blacklist provided, updates the ACL.",
)
async def cap_acl(scope: str, whitelist: str = "", blacklist: str = "",
                   trace_id=None):
    if scope not in CAP_ACL:
        return {"error": f"Unknown scope: {scope}. Available: {list(CAP_ACL.keys())}"}

    # If no updates provided, just return current state
    if not whitelist and not blacklist:
        acl = CAP_ACL[scope]
        return {
            "scope": scope,
            "label": acl["label"],
            "whitelist": acl["whitelist"],
            "blacklist": acl["blacklist"],
            "effective_count": _effective_cap_count(scope),
        }

    # Update
    if whitelist:
        CAP_ACL[scope]["whitelist"] = [c.strip() for c in whitelist.split(",") if c.strip()]
    if blacklist:
        CAP_ACL[scope]["blacklist"] = [c.strip() for c in blacklist.split(",") if c.strip()]

    # Persist
    r = _redis()
    if r:
        try:
            await r.set(f"vera:ui:acl:{scope}", json.dumps(CAP_ACL[scope]))
        except Exception:
            pass

    return {
        "scope": scope,
        "whitelist": CAP_ACL[scope]["whitelist"],
        "blacklist": CAP_ACL[scope]["blacklist"],
        "effective_count": _effective_cap_count(scope),
        "updated": True,
    }


@capability(
    "ui.caps.scopes",
    http_method="GET", http_path="/ui/caps/scopes", http_tags=["ui"],
    memory="off", silent=True,
    description="List all capability scopes and their access control settings.",
)
async def cap_scopes(trace_id=None):
    scopes = {}
    for sid, acl in CAP_ACL.items():
        scopes[sid] = {
            "label": acl["label"],
            "whitelist_count": len(acl["whitelist"]),
            "blacklist_count": len(acl["blacklist"]),
            "effective_count": _effective_cap_count(sid),
        }
    return {"scopes": scopes, "total_caps": len(CAPABILITY_REGISTRY)}


@capability(
    "ui.caps.allowed",
    http_method="GET", http_path="/ui/caps/allowed", http_tags=["ui"],
    memory="off", silent=True,
    description="Get list of capabilities allowed for a scope. "
                "Input: scope (str, default 'general').",
)
async def cap_allowed(scope: str = "general", trace_id=None):
    caps = get_allowed_caps(scope)
    return {"scope": scope, "caps": caps, "count": len(caps)}


def _effective_cap_count(scope: str) -> int:
    return len(get_allowed_caps(scope))


def get_allowed_caps(scope: str = "general") -> List[str]:
    """Get the list of capability names allowed for a scope."""
    acl = CAP_ACL.get(scope, CAP_ACL.get("general", {}))
    whitelist = set(acl.get("whitelist", []))
    blacklist = set(acl.get("blacklist", []))

    all_caps = list(CAPABILITY_REGISTRY.keys())

    if whitelist:
        # Only whitelisted caps
        return [c for c in all_caps if c in whitelist and c not in blacklist]
    else:
        # All caps except blacklisted
        return [c for c in all_caps if c not in blacklist]


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP — load persisted themes, ACLs, and dynamic panels from Redis
# ─────────────────────────────────────────────────────────────────────────────

async def _startup():
    global _ACTIVE_THEME, _UI_SCALE
    r = _redis()
    if not r:
        return

    # Load active theme
    try:
        saved = await r.get("vera:ui:theme")
        if saved:
            _ACTIVE_THEME = saved.decode() if isinstance(saved, bytes) else saved
    except Exception:
        pass

    # Load UI scale
    try:
        saved = await r.get("vera:ui:scale")
        if saved:
            _UI_SCALE = _clamp_scale(saved.decode() if isinstance(saved, bytes) else saved)
    except Exception:
        pass

    # Load custom themes
    try:
        keys = await r.keys("vera:ui:theme:*")
        for key in (keys or []):
            try:
                raw = await r.get(key)
                if raw:
                    tid = (key.decode() if isinstance(key, bytes) else key).split(":")[-1]
                    CUSTOM_THEMES[tid] = json.loads(raw)
            except Exception:
                pass
    except Exception:
        pass

    # Load ACLs
    try:
        keys = await r.keys("vera:ui:acl:*")
        for key in (keys or []):
            try:
                raw = await r.get(key)
                if raw:
                    scope = (key.decode() if isinstance(key, bytes) else key).split(":")[-1]
                    if scope in CAP_ACL:
                        CAP_ACL[scope].update(json.loads(raw))
            except Exception:
                pass
    except Exception:
        pass

    # Load dynamic panels
    try:
        keys = await r.keys("vera:ui:panel:*")
        for key in (keys or []):
            try:
                raw = await r.get(key)
                if raw:
                    panel = json.loads(raw)
                    pid = panel.get("id", "")
                    if pid:
                        DYNAMIC_PANELS[pid] = panel
                        register_ui(pid, panel.get("label", pid),
                                    panel.get("icon", ""),
                                    panel.get("html", ""),
                                    panel.get("js", ""),
                                    ui_caps=panel.get("ui_caps", []),
                                    mode=panel.get("mode", "tab"),
                                    tab_order=panel.get("tab_order", 200))
            except Exception:
                pass
    except Exception:
        pass

    log.info("ui_capabilities ready — theme=%s, %d custom themes, %d dynamic panels, %d scopes",
             _ACTIVE_THEME, len(CUSTOM_THEMES), len(DYNAMIC_PANELS), len(CAP_ACL))


schedule(_startup, interval=999999, name="ui_startup")

# Ensure _startup() fires shortly after the backend connects, not just on the
# first 999999s scheduler tick.  We piggyback on the existing schedule() system
# with a short one-shot interval — the function is idempotent so multiple calls
# are harmless.  This guarantees custom themes from Redis are available before
# the harness's first /ui/themes request (which arrives ~800ms after page load).
async def _ui_startup_retry():
    """Run _startup() up to 5 times until Redis is available."""
    for _ in range(5):
        r = _redis()
        if r:
            await _startup()
            return
        import asyncio as _a; await _a.sleep(2)

schedule(_ui_startup_retry, interval=999999, name="ui_startup_retry")

# Also ensure _startup runs immediately at import time via the event loop,
# not waiting for the scheduler tick — this guarantees Redis-persisted custom
# themes and dynamic panels are available before the harness's first connect().
import asyncio as _ui_asyncio
try:
    _ui_asyncio.get_event_loop().create_task(_startup())
except RuntimeError:
    pass  # no running loop at import time — scheduler will handle it