"""
vera_graph_panels.py
====================================================================
Serves the modular sidebar *companion* panels for vera_graph.js as
standalone JS at /ui/ routes, mirroring how observe_elements_capabilities.py
serves its custom elements.

vera_graph.js (served separately at /ui/vera-graph.js) exposes
window.veraUI.Graph.registerPanel(). Each companion file here calls that to
add a left-rail sidebar tab to EVERY graph on the page. Loading a companion
once enables it everywhere the graph is embedded.

Routes:
  GET /ui/vera-graph-panel-loom.js     — Loom workbench panel
  GET /ui/vera-graph-panel-example.js  — reference/example panel

To make a panel appear, the host page must include the companion <script>
AFTER vera-graph.js, e.g.:

  <script src="/ui/vera-graph.js"></script>
  <script src="/ui/vera-graph-panel-loom.js"></script>

A convenience constant VERA_GRAPH_PANEL_SCRIPTS holds the standard set of
<script> tags so host panels can inject them in one go.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi.responses import Response

from Vera.Orchestration.capability_orchestration import (
    APP,
    capability,
)

log = logging.getLogger("vera.graph_panels")

_HERE = Path(__file__).resolve().parent


def _read(name: str) -> str:
    p = _HERE / name
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"/* {name} not found at {p} */"


# ─────────────────────────────────────────────────────────────────────────────
# Loom panel
# ─────────────────────────────────────────────────────────────────────────────

@APP.get("/ui/vera-graph-panel-loom.js", include_in_schema=False)
async def _serve_loom_panel_js():
    return Response(
        content=_read("vera_graph_panel_loom.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@capability(
    "ui.graph_panels.loom_js",
    http_method="GET",
    http_path="/ui/vera-graph-panel-loom.js",
    http_tags=["ui", "graph"],
    memory="off",
    silent=True,
    description="Serve the Loom sidebar panel companion JS for vera_graph.js.",
)
async def serve_loom_panel_js(trace_id=None):
    return Response(
        content=_read("vera_graph_panel_loom.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Example / reference panel
# ─────────────────────────────────────────────────────────────────────────────

@APP.get("/ui/vera-graph-panel-example.js", include_in_schema=False)
async def _serve_example_panel_js():
    return Response(
        content=_read("vera_graph_panel_example.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@capability(
    "ui.graph_panels.example_js",
    http_method="GET",
    http_path="/ui/vera-graph-panel-example.js",
    http_tags=["ui", "graph"],
    memory="off",
    silent=True,
    description="Serve the example/reference sidebar panel companion JS.",
)
async def serve_example_panel_js(trace_id=None):
    return Response(
        content=_read("vera_graph_panel_example.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# WorldView panel
# ─────────────────────────────────────────────────────────────────────────────

@APP.get("/ui/vera-graph-panel-worldview.js", include_in_schema=False)
async def _serve_worldview_panel_js():
    return Response(
        content=_read("vera_graph_panel_worldview.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@capability(
    "ui.graph_panels.worldview_js",
    http_method="GET",
    http_path="/ui/vera-graph-panel-worldview.js",
    http_tags=["ui", "graph"],
    memory="off",
    silent=True,
    description="Serve the WorldView sidebar panel companion JS for vera_graph.js.",
)
async def serve_worldview_panel_js(trace_id=None):
    return Response(
        content=_read("vera_graph_panel_worldview.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


# Convenience: the standard companion <script> tags to drop into any page that
# embeds the graph, AFTER the vera-graph.js include.
VERA_GRAPH_PANEL_SCRIPTS = (
    '<script src="/ui/vera-graph-panel-loom.js"></script>\n'
    '<script src="/ui/vera-graph-panel-worldview.js"></script>\n'
    '<script src="/ui/vera-graph-panel-discover.js"></script>'
)

# ─────────────────────────────────────────────────────────────────────────────
# Discover+ panel
# ─────────────────────────────────────────────────────────────────────────────

@APP.get("/ui/vera-graph-panel-discover.js", include_in_schema=False)
async def _serve_discover_panel_js():
    return Response(
        content=_read("vera_graph_panel_discover.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@capability(
    "ui.graph_panels.discover_js",
    http_method="GET",
    http_path="/ui/vera-graph-panel-discover.js",
    http_tags=["ui", "graph"],
    memory="off",
    silent=True,
    description="Serve the Discover+ sidebar panel companion JS for vera_graph.js.",
)
async def serve_discover_panel_js(trace_id=None):
    return Response(
        content=_read("vera_graph_panel_discover.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


log.info("vera graph sidebar panels registered (loom, worldview, discover, example)")