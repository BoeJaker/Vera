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

import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, UI_PANELS,
    capability, emit_event, now_iso, register_ui, schedule,
)

log = logging.getLogger("vera.ui")

def _redis():
    return _orch.REDIS

# ─────────────────────────────────────────────────────────────────────────────
# THEME SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

BUILTIN_THEMES = {
    "ash":   {"label":"Ash",   "type":"light", "accent":"#2e6da4", "vars":{"--bg":"#f0ede8","--s1":"#e8e5df","--s2":"#dedbd4","--s3":"#d4d1c8","--bd":"rgba(0,0,0,.08)","--bd2":"rgba(0,0,0,.15)","--t1":"#1a1a18","--t2":"#6a6860","--t3":"#a8a69e","--ac":"#2e6da4","--ac2":"#228060","--ac3":"#c47020","--ac4":"#c03030","--ac5":"#6040a0"}},
    "dusk":  {"label":"Dusk",  "type":"dark",  "accent":"#6ea8d8", "vars":{"--bg":"#0e0f12","--s1":"#13151a","--s2":"#1a1d24","--s3":"#222630","--bd":"rgba(255,255,255,.07)","--bd2":"rgba(255,255,255,.14)","--t1":"#d4dae4","--t2":"#6b7585","--t3":"#3b4252","--ac":"#6ea8d8","--ac2":"#5ec9a0","--ac3":"#e09a55","--ac4":"#e06060","--ac5":"#a78bfa"}},
    "void":  {"label":"Void",  "type":"dark",  "accent":"#9b8dfa", "vars":{"--bg":"#000","--s1":"#070707","--s2":"#0d0d0d","--s3":"#141414","--bd":"rgba(255,255,255,.05)","--bd2":"rgba(255,255,255,.1)","--t1":"#e0e0e0","--t2":"#929292","--t3":"#636363","--ac":"#9b8dfa","--ac2":"#5ecab0","--ac3":"#dba355","--ac4":"#e06060","--ac5":"#f472b6"}},
    "chalk": {"label":"Chalk", "type":"dark",  "accent":"#d4a96a", "vars":{"--bg":"#1c1c1e","--s1":"#242428","--s2":"#2c2c32","--s3":"#34343c","--bd":"rgba(255,255,255,.08)","--bd2":"rgba(255,255,255,.16)","--t1":"#e8e4d8","--t2":"#b6ac97","--t3":"#6e6b5b","--ac":"#d4a96a","--ac2":"#80c090","--ac3":"#c08878","--ac4":"#e07070","--ac5":"#9080c0"}},
    "ice":   {"label":"Ice",   "type":"dark",  "accent":"#5ab0f0", "vars":{"--bg":"#090f18","--s1":"#0d1620","--s2":"#121e2c","--s3":"#182638","--bd":"rgba(80,160,255,.1)","--bd2":"rgba(80,160,255,.2)","--t1":"#c0d8f0","--t2":"#406880","--t3":"#1e3850","--ac":"#5ab0f0","--ac2":"#38d0b0","--ac3":"#e0b060","--ac4":"#e06868","--ac5":"#8070e0"}},
}

# Custom themes stored at runtime (persisted to Redis if available)
CUSTOM_THEMES: Dict[str, dict] = {}

# Current active theme
_ACTIVE_THEME = "dusk"


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
    return Response(content=css, media_type="text/css")


# Serve vera-ui.js — the universal theme integration script
@APP.get("/ui/vera-ui.js", include_in_schema=False)
async def _serve_vera_ui_js():
    from fastapi.responses import Response
    from pathlib import Path
    p = Path(__file__).parent / "vera-ui.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript")
    return Response(content="console.warn('vera-ui.js not found');",
                    media_type="application/javascript")


# Serve vera-panel-bridge.js — opt-in shim that lets a panel iframe
# participate in the chat ↔ panel postMessage protocol. Panels include
# this with <script src="/ui/vera-panel-bridge.js"></script>; doing so
# enables the chat UI to inject the panel's current state into the
# agent's prompt, and lets the agent dispatch actions back to the panel
# (via the upcoming panel.dispatch cap, when wired through the UI).
@APP.get("/ui/vera-panel-bridge.js", include_in_schema=False)
async def _serve_vera_panel_bridge_js():
    from fastapi.responses import Response
    from pathlib import Path
    p = Path(__file__).parent / "vera-panel-bridge.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript")
    # Inline fallback — exact same logic as the file version, kept here
    # so panels keep working even if the file isn't present in the deploy.
    fallback = (
        "/* vera-panel-bridge.js (inline fallback) */\n"
        "(function(){if(window.VeraPanelBridge)return;"
        "var _sp=null,_ah={},_pid='',_sid='',_t=null,_last=null;"
        "function _build(){if(_sp){try{var s=_sp();return(s&&typeof s==='object')?s:{};}catch(e){return{error:String(e)};}}"
        "var st={url:location.href,title:document.title,hash:location.hash};"
        "try{var f=document.activeElement;if(f&&f!==document.body&&f.id)st.focused_id=f.id;"
        "var a=document.querySelectorAll('.active,.on.rtab,.selected,[aria-selected=\"true\"]');"
        "if(a.length)st.active=Array.prototype.slice.call(a,0,6).map(function(el){return(el.id||el.textContent||el.tagName).toString().slice(0,80);});"
        "}catch(e){}return st;}"
        "function pub(){var s=_build();try{var sg=JSON.stringify(s);if(sg===_last)return;_last=sg;}catch(e){}"
        "try{window.parent.postMessage({type:'vera:panel:state',panel_id:_pid,session_id:_sid,state:s},'*');}catch(e){}}"
        "function pubD(){if(_t)clearTimeout(_t);_t=setTimeout(pub,250);}"
        "function pubE(n,p){try{window.parent.postMessage({type:'vera:panel:event',panel_id:_pid,event:n,payload:p||{}},'*');}catch(e){}}"
        "function pubR(aid,ok,res,err,act){if(!aid)return;try{window.parent.postMessage("
        "{type:'vera:panel:action_result',panel_id:_pid,action_id:aid,action:act||'',"
        "ok:!!ok,result:(res===undefined?null:res),error:err||null},'*');}catch(e){}}"
        "window.addEventListener('message',function(ev){var d=ev.data;if(!d||typeof d!=='object')return;var t=d.type||'';"
        "if(t==='vera:panel:init'){_pid=d.panel_id||_pid;_sid=d.session_id||_sid;setTimeout(pub,50);}"
        "else if(t==='vera:panel:query'){pub();}"
        "else if(t==='vera:panel:action'){var act=String(d.action||''),aid=d.action_id||'',pl=d.payload||{};"
        "if(act==='__query__'){pubR(aid,true,_build(),null,act);pubD();return;}"
        "var h=_ah[act]||_ah['*'];"
        "if(!h){pubE('action_unhandled',{action:act});pubR(aid,false,null,'no handler for action: '+act,act);return;}"
        "var ret;try{ret=h(pl,act);}catch(e){pubE('action_error',{action:act,error:String(e)});pubR(aid,false,null,String(e),act);return;}"
        "if(ret&&typeof ret.then==='function'){ret.then(function(v){pubR(aid,true,v===undefined?null:v,null,act);pubD();},"
        "function(e){pubR(aid,false,null,String(e),act);});}"
        "else{pubR(aid,true,ret===undefined?null:ret,null,act);pubD();}}});"
        "['click','change','input'].forEach(function(t){document.addEventListener(t,pubD,{passive:true,capture:true});});"
        "setInterval(pubD,30000);"
        "window.VeraPanelBridge={registerStateProvider:function(fn){_sp=fn;pubD();},"
        "registerActionHandler:function(n,fn){_ah[String(n)]=fn;},"
        "publishState:pub,publishStateDebounced:pubD,publishEvent:pubE,publishActionResult:pubR,"
        "panelId:function(){return _pid;},sessionId:function(){return _sid;}};"
        "if(document.readyState==='complete'||document.readyState==='interactive')setTimeout(pubD,100);"
        "else document.addEventListener('DOMContentLoaded',function(){setTimeout(pubD,100);});})();"
    )
    return Response(content=fallback, media_type="application/javascript")


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
# PANEL ↔ AGENT BRIDGE
# ─────────────────────────────────────────────────────────────────────────────
# Lets agents read state from / send actions to the UI panel currently mounted
# in the user's chat session. Architecture:
#
#   agent calls panel.dispatch / panel.query  (this file)
#       ──→ publishes request on Redis channel  vera:panel:dispatch:{sid}
#               ──→ chat panel SSE  /ui/panels/dispatch/stream?session_id=sid
#                       ──→ chat panel postMessages the iframe
#                               ──→ panel handler runs (vera-panel-bridge.js)
#                       ←── chat panel postMessage receives result
#               ←── chat panel POSTs  /ui/panels/dispatch/ack
#       ←── server publishes on  vera:panel:dispatch:reply:{sid}
#   cap awaits its request_id and returns
#
# All channels are session-scoped so multiple users / browser tabs don't
# collide. Reply-channel keying is request_id-aware so concurrent requests
# from the same session also stay separate.

import asyncio as _bridge_asyncio

_PANEL_DISPATCH_CHANNEL_REQ   = "vera:panel:dispatch:{sid}"
_PANEL_DISPATCH_CHANNEL_REPLY = "vera:panel:dispatch:reply:{sid}"


async def _panel_dispatch_await_reply(sid: str, request_id: str, timeout: float):
    """Subscribe to the session reply channel and await the matching reply."""
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}

    channel = _PANEL_DISPATCH_CHANNEL_REPLY.format(sid=sid)
    pubsub = r.pubsub()
    try:
        await pubsub.subscribe(channel)
        deadline = _bridge_asyncio.get_event_loop().time() + max(0.5, float(timeout))
        while True:
            remaining = deadline - _bridge_asyncio.get_event_loop().time()
            if remaining <= 0:
                return {"ok": False, "error": "timeout",
                        "request_id": request_id, "timeout_secs": float(timeout)}
            try:
                msg = await _bridge_asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining),
                    timeout=remaining + 0.5,
                )
            except _bridge_asyncio.TimeoutError:
                return {"ok": False, "error": "timeout",
                        "request_id": request_id, "timeout_secs": float(timeout)}
            if not msg:
                continue
            data = msg.get("data")
            if isinstance(data, (bytes, bytearray)):
                try:
                    data = data.decode("utf-8", "replace")
                except Exception:
                    continue
            try:
                obj = json.loads(data) if isinstance(data, str) else data
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("request_id") == request_id:
                return obj
    finally:
        try: await pubsub.unsubscribe(channel)
        except Exception: pass
        try: await pubsub.close()
        except Exception: pass


@capability(
    "panel.dispatch",
    http_method="POST", http_path="/panel/dispatch", http_tags=["ui", "panel"],
    memory="off",
    description=(
        "Send an action to the UI panel currently mounted in the user's chat "
        "session. The panel's vera-panel-bridge.js shim runs the matching "
        "action handler and returns the result. Inputs: session_id (str! — "
        "the chat session ID; usually the trace_id of the calling turn), "
        "action (str! — handler name registered via "
        "VeraPanelBridge.registerActionHandler), payload (object — handler "
        "args, default {}), timeout_secs (number — max wait for ack, "
        "default 8). Returns the panel handler's result, or "
        "{ok:false, error:'…'} on timeout / no panel mounted."
    ),
)
async def cap_panel_dispatch(
    session_id: str = "",
    action: str = "",
    payload: dict = None,
    timeout_secs: float = 8.0,
    trace_id=None,
):
    sid = (session_id or trace_id or "").strip()
    if not sid:
        return {"ok": False, "error": "session_id is required"}
    act = (action or "").strip()
    if not act:
        return {"ok": False, "error": "action is required"}
    if payload is not None and not isinstance(payload, dict):
        return {"ok": False, "error": "payload must be an object"}

    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}

    request_id = str(uuid.uuid4())
    req_channel = _PANEL_DISPATCH_CHANNEL_REQ.format(sid=sid)
    msg = {
        "request_id": request_id,
        "session_id": sid,
        "action":     act,
        "payload":    payload or {},
        "ts":         now_iso(),
    }
    # Publish first so the SSE has something to forward when the chat
    # panel arrives. We start the await-task afterwards because pubsub
    # subscription is synchronous setup, and we don't want a race where
    # the panel replies before we've subscribed.
    await_task = _bridge_asyncio.ensure_future(
        _panel_dispatch_await_reply(sid, request_id, float(timeout_secs))
    )
    try:
        await r.publish(req_channel, json.dumps(msg))
    except Exception as e:
        await_task.cancel()
        return {"ok": False, "error": f"publish failed: {e}"}

    try:
        reply = await await_task
    except _bridge_asyncio.CancelledError:
        return {"ok": False, "error": "cancelled"}
    return reply


@capability(
    "panel.query",
    http_method="POST", http_path="/panel/query", http_tags=["ui", "panel"],
    memory="off",
    description=(
        "Read the current state of the UI panel mounted in the user's chat "
        "session. Equivalent to panel.dispatch with action='__query__' but "
        "lighter — returns the panel's last state snapshot directly. "
        "Inputs: session_id (str! — chat session ID; usually trace_id), "
        "timeout_secs (number — default 4). Returns the panel state object "
        "or {ok:false, error:'…'} on timeout."
    ),
)
async def cap_panel_query(
    session_id: str = "",
    timeout_secs: float = 4.0,
    trace_id=None,
):
    return await cap_panel_dispatch(
        session_id=session_id,
        action="__query__",
        payload={},
        timeout_secs=float(timeout_secs),
        trace_id=trace_id,
    )


# ── HTTP routes the chat panel uses to participate in the bridge ────────────

@APP.get("/ui/panels/dispatch/stream", include_in_schema=False)
async def _panel_dispatch_stream(session_id: str = ""):
    """SSE stream of dispatch requests targeted at this chat session.
    Chat panel opens this on init and forwards each event to the
    embedded panel iframe via postMessage."""
    from fastapi.responses import StreamingResponse
    sid = (session_id or "").strip()
    if not sid:
        async def _err():
            yield b"event: error\ndata: session_id required\n\n"
        return StreamingResponse(_err(), media_type="text/event-stream")

    r = _redis()
    if not r:
        async def _noredis():
            yield b"event: error\ndata: redis unavailable\n\n"
        return StreamingResponse(_noredis(), media_type="text/event-stream")

    channel = _PANEL_DISPATCH_CHANNEL_REQ.format(sid=sid)

    async def _gen():
        pubsub = r.pubsub()
        await pubsub.subscribe(channel)
        # Initial hello so the client knows it's connected
        yield (b"event: ready\ndata: " + json.dumps({"session_id": sid}).encode() + b"\n\n")
        try:
            # Keep-alive comment every 20s so proxies don't kill idle connections
            last_ka = _bridge_asyncio.get_event_loop().time()
            while True:
                try:
                    msg = await _bridge_asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0),
                        timeout=6.0,
                    )
                except _bridge_asyncio.TimeoutError:
                    msg = None

                now_t = _bridge_asyncio.get_event_loop().time()
                if msg:
                    data = msg.get("data")
                    if isinstance(data, (bytes, bytearray)):
                        try: data = data.decode("utf-8", "replace")
                        except Exception: data = ""
                    payload = data if isinstance(data, str) else ""
                    if payload:
                        yield (b"event: dispatch\ndata: " + payload.encode("utf-8") + b"\n\n")
                if (now_t - last_ka) > 20:
                    yield b": keep-alive\n\n"
                    last_ka = now_t
        finally:
            try: await pubsub.unsubscribe(channel)
            except Exception: pass
            try: await pubsub.close()
            except Exception: pass

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


from fastapi import Request as _BridgeRequest

@APP.post("/ui/panels/dispatch/ack", include_in_schema=False)
async def _panel_dispatch_ack(request: _BridgeRequest):
    """Chat panel POSTs the panel's reply here so the awaiting cap resolves.
    Body: {request_id, session_id, ok, result?, error?}.
    """
    from fastapi.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    rid = (body or {}).get("request_id", "")
    sid = (body or {}).get("session_id", "")
    if not rid or not sid:
        return JSONResponse({"ok": False, "error": "request_id and session_id required"},
                            status_code=400)
    r = _redis()
    if not r:
        return JSONResponse({"ok": False, "error": "redis unavailable"}, status_code=503)

    reply_channel = _PANEL_DISPATCH_CHANNEL_REPLY.format(sid=sid)
    try:
        await r.publish(reply_channel, json.dumps(body))
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"publish failed: {e}"}, status_code=500)
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP — load persisted themes, ACLs, and dynamic panels from Redis
# ─────────────────────────────────────────────────────────────────────────────

async def _startup():
    global _ACTIVE_THEME
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