"""
cap_hub_capabilities.py  —  Unified Capability Hub Panel
=========================================================
Registers:
  1. cap-hub         — full tab panel (mode="tab") combining:
                         • Capability list + search + call
                         • Activity/tracking config (groups, per-cap overrides, limits)
                         • MCP server management
                         • Job / task output streaming

  2. Inject elements (mode="inject") — reusable fragments embeddable
     anywhere in the Vera UI system.  Each is both a named inject panel
     AND a web-component (<vera-*>) so it can be used multiple times on
     the same page without ID collisions:

     cap-list         → <vera-cap-list>         filterable cap list
     cap-search       → <vera-cap-search>        search dropdown
     activity-track   → <vera-activity-track>    compact tracking widget
     mcp-servers      → <vera-mcp-servers>       MCP server manager
     job-stream       → <vera-job-stream>        live job output stream

Usage in another panel HTML (inject):
    <!-- Include the web-component script once: -->
    <script src="/cap_hub/elements.js"></script>

    <!-- Then use anywhere: -->
    <vera-cap-list filter-group="llm" show-tracking="true"></vera-cap-list>
    <vera-cap-search placeholder="Pick a cap…" on-change="myHandler"></vera-cap-search>
    <vera-activity-track groups="llm,exec,dag"></vera-activity-track>
    <vera-job-stream job-id="abc123" height="180"></vera-job-stream>
    <vera-mcp-servers></vera-mcp-servers>

The inject panels also work as named sub-panels in the harness media
switcher — they register with mode="inject" so they appear in the
"Elements" sub-tab as well.

The full panel registers with mode="tab" so it creates its own top-level
harness tab.
"""

from __future__ import annotations

import logging
from pathlib import Path

from Vera.Orchestration.capability_orchestration import (
    APP, capability, register_ui,
)
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, HTMLResponse as _HTML, FileResponse as _FileResp

log = logging.getLogger("vera.cap_hub")
_HERE = Path(__file__).parent

# ─────────────────────────────────────────────────────────────────────────────
# Load the panel HTML (served both inline via register_ui and as a direct
# HTTP endpoint — so iframes in other panels can point to it too).
# ─────────────────────────────────────────────────────────────────────────────

def _panel_html() -> str:
    path = _HERE / "cap_hub_panel.html"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "<p style='color:red'>cap_hub_panel.html not found</p>"


# ─────────────────────────────────────────────────────────────────────────────
# HTTP: /cap_hub/panel  (serves the full panel HTML for iframe embedding)
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "cap_hub.panel",
    http_method="GET", http_path="/cap_hub/panel",
    http_tags=["ui", "cap_hub"],
    memory="off", silent=True,
    description="Serve the full Capability Hub panel HTML.",
)
async def cap_hub_panel(trace_id=None):
    return HTMLResponse(_panel_html())


# ─────────────────────────────────────────────────────────────────────────────
# HTTP: /cap_hub/elements.js  (web-component definitions only — no full panel)
# Other panels can do <script src="/cap_hub/elements.js"> to get all the
# <vera-*> custom elements without loading the whole panel.
# ─────────────────────────────────────────────────────────────────────────────

_ELEMENTS_MARKER_START = "<!-- ═══ INJECT ELEMENTS"
_ELEMENTS_MARKER_END   = "</script>\n\n</body>"

def _elements_js() -> str:
    """
    Extract just the <script> blocks that define the web components from
    cap_hub_panel.html so they can be served as a standalone JS include.
    """
    html = _panel_html()
    start = html.find(_ELEMENTS_MARKER_START)
    if start == -1:
        return "// cap_hub elements not found"
    chunk = html[start:]
    # Grab all <script> ... </script> blocks after the marker
    scripts = []
    pos = 0
    while True:
        s = chunk.find("<script>", pos)
        if s == -1:
            break
        e = chunk.find("</script>", s)
        if e == -1:
            break
        scripts.append(chunk[s + 8 : e])
        pos = e + 9
    return "\n\n".join(scripts)


@capability(
    "cap_hub.elements_js",
    http_method="GET", http_path="/cap_hub/elements.js",
    http_tags=["ui", "cap_hub"],
    memory="off", silent=True,
    description="Serve the vera-* web component definitions as a standalone JS file.",
)
async def cap_hub_elements_js(trace_id=None):
    from fastapi.responses import Response
    return Response(
        content=_elements_js(),
        media_type="application/javascript",
    )
# The panel HTML can sit next to this file, or anywhere on your Python path.
# Adjust this path to wherever you save vera_chat_panel.html.
_PANEL_HTML_PATH = Path(__file__).parent / "cap_hub.html"

# Fallback: try the project root
if not _PANEL_HTML_PATH.exists():
    _PANEL_HTML_PATH = Path(__file__).parent.parent / "cap_hub.html"

@APP.get("/cap_hub/panel", response_class=_HTML)
async def serve_chat_panel():
    """Serve the self-contained Vera Chat Panel HTML."""
    if _PANEL_HTML_PATH.exists():
        return _HTML(content=_PANEL_HTML_PATH.read_text(encoding="utf-8"))
    return _HTML(content="<h2 style='font-family:monospace;color:#c96b6b'>vera_chat_panel.html not found</h2>")


@APP.get("/cap_hub/panel", response_class=_HTML)
async def serve_chat_panel():
    """Serve the self-contained Vera Chat Panel HTML."""
    if _PANEL_HTML_PATH.exists():
        return _HTML(content=_PANEL_HTML_PATH.read_text(encoding="utf-8"))
    return _HTML(content="<h2 style='font-family:monospace;color:#c96b6b'>vera_chat_panel.html not found</h2>")


# ─────────────────────────────────────────────────────────────────────────────
# REGISTER MAIN PANEL  (mode="tab" → gets its own top-level harness tab)
# ─────────────────────────────────────────────────────────────────────────────

register_ui(
    panel_id        = "cap-hub",
    label     = "Cap Hub",
    icon      = "⬡",
    mode      = "tab",
    tab_order = 12,
    html      = '<iframe src="/cap_hub/panel" style="width:100%;height:100%;border:none"></iframe>',
    ui_caps   = [
        "cap_hub.panel",
        "cap_tracking.get_config",
        "cap_tracking.group",
        "cap_tracking.cap",
        "cap_tracking.limits",
        "cap_tracking.stats",
    ],
)

log.info("cap-hub: main tab registered")


# ─────────────────────────────────────────────────────────────────────────────
# REGISTER INJECT ELEMENTS
# Each inject element is:
#   • A register_ui(mode="inject") call  → appears in the media sub-switcher
#     as a named sub-panel (for panels that want a full embed)
#   • A tiny HTML snippet that loads the web-component via the elements.js
#     script and then uses the custom element tag
# ─────────────────────────────────────────────────────────────────────────────

# Shared script include (deduped by harness since it only injects each id once)
_ELEMENTS_SCRIPT = (
    '<script id="vera-elements-js-include" '
    'src="/cap_hub/elements.js"></script>'
)


def _inject(tag: str, attrs: str = "") -> str:
    """Return inject HTML for a web component."""
    return (
        f'{_ELEMENTS_SCRIPT}\n'
        f'<{tag} {attrs} style="display:block;width:100%"></{tag}>'
    )


register_ui(
    panel_id        = "cap-list",
    label     = "Cap List",
    icon      = "◈",
    mode      = "inject",
    tab_order = 200,
    html      = _inject("vera-cap-list", 'show-tracking="true"'),
    ui_caps   = ["cap_tracking.get_config"],
)

register_ui(
    panel_id        = "cap-search",
    label     = "Cap Search",
    icon      = "⌕",
    mode      = "inject",
    tab_order = 201,
    html      = _inject("vera-cap-search", 'placeholder="Search capabilities…"'),
    ui_caps   = [],
)

register_ui(
    panel_id        = "activity-track",
    label     = "Activity Tracking",
    icon      = "◉",
    mode      = "inject",
    tab_order = 202,
    html      = _inject("vera-activity-track", 'groups="llm,exec,dag,research,chat"'),
    ui_caps   = [
        "cap_tracking.get_config",
        "cap_tracking.stats",
        "cap_tracking.group",
        "cap_tracking.limits",
    ],
)

register_ui(
    panel_id        = "mcp-servers",
    label     = "MCP Servers",
    icon      = "⬡",
    mode      = "inject",
    tab_order = 203,
    html      = _inject("vera-mcp-servers"),
    ui_caps   = [],
)

register_ui(
    panel_id        = "job-stream",
    label     = "Job Stream",
    icon      = "⟳",
    mode      = "inject",
    tab_order = 1,
    html      = _inject("vera-job-stream", 'height="260"'),
    ui_caps   = [],
)

log.info("cap-hub: inject elements registered (cap-list, cap-search, activity-track, mcp-servers, job-stream)")


# ─────────────────────────────────────────────────────────────────────────────
# CAP SOURCE ENDPOINT
# Attempts to return Python source for a capability from the registry so
# the "{ } Source" button in the Cap Hub can display the real code.
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "caps.source",
    http_method="GET", http_path="/caps/source",
    http_tags=["caps", "ui"],
    memory="off", silent=True,
    description=(
        "Return Python source code for a named capability. "
        "Input: name (str). "
        "Output: {name, source, file, line, module} or {error}."
    ),
)
async def cap_source(name: str, trace_id=None):
    """
    Looks up the capability in CAPABILITY_REGISTRY, then uses inspect to
    retrieve the Python source of the underlying function.
    """
    import inspect
    from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY

    cap = CAPABILITY_REGISTRY.get(name)
    if not cap:
        return {"error": f"Capability not found: {name}"}

    func = cap.get("func")
    if not func:
        return {"error": "No function attached to this capability (proxy/external?)"}

    try:
        source = inspect.getsource(func)
        file   = inspect.getfile(func)
        lines  = inspect.getsourcelines(func)
        line   = lines[1] if lines else None
        module = getattr(func, "__module__", None)
        return {
            "name":   name,
            "source": source,
            "file":   file,
            "line":   line,
            "module": module,
        }
    except (OSError, TypeError) as e:
        # Fall back to showing schema
        schema = cap.get("schema") or {}
        return {
            "name":   name,
            "source": f"# Source not available: {e}\n\n# Schema:\n" +
                      __import__("json").dumps(schema, indent=2),
            "file":   None,
            "line":   None,
            "module": None,
        }