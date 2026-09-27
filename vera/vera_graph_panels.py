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
  GET /ui/vera-graph-panel-explode.js  — Explode panel (prose / fabric / code)
  GET /ui/vera-graph-embed.js          — <vera-graph-embed>, the bare inline graph

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

from Vera.vera.capability_orchestration import (
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


# ─────────────────────────────────────────────────────────────────────────────
# API browser panel
# ─────────────────────────────────────────────────────────────────────────────

@APP.get("/ui/vera-graph-panel-api.js", include_in_schema=False)
async def _serve_api_panel_js():
    return Response(
        content=_read("vera_graph_panel_api.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@capability(
    "ui.graph_panels.api_js",
    http_method="GET",
    http_path="/ui/vera-graph-panel-api.js",
    http_tags=["ui", "graph"],
    memory="off",
    silent=True,
    description="Serve the API browser sidebar panel companion JS for vera_graph.js.",
)
async def serve_api_panel_js(trace_id=None):
    return Response(
        content=_read("vera_graph_panel_api.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


# Convenience: the standard companion <script> tags to drop into any page that
# embeds the graph, AFTER the vera-graph.js include.
VERA_GRAPH_PANEL_SCRIPTS = (
    '<script src="/ui/vera-graph-panel-loom.js"></script>\n'
    '<script src="/ui/vera-graph-panel-worldview.js"></script>\n'
    '<script src="/ui/vera-graph-panel-discover.js"></script>\n'
    '<script src="/ui/vera-graph-panel-api.js"></script>\n'
    '<script src="/ui/vera-graph-panel-explode.js"></script>'
)

# ─────────────────────────────────────────────────────────────────────────────
# Discover+ panel
# ─────────────────────────────────────────────────────────────────────────────

# The unified graph's families (UI redesign, Notes/40 §7): the adapters that map
# context · memory · dag · loop · plan · estate to ONE graph document, and the
# mixer. Pure; the memory graph panel and the chat's graph column load it.
@APP.get("/ui/graph/families.js", include_in_schema=False)
async def _serve_graph_families_js():
    from fastapi.responses import Response
    from pathlib import Path
    p = Path(__file__).parent / "graph" / "families.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})
    return Response(content="console.warn('families.js not found');", media_type="application/javascript")


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


log.info("vera graph sidebar panels registered (loom, worldview, discover, api, example)")


# ─────────────────────────────────────────────────────────────────────────────
# Explode panel — render prose, a fabric graph, or a code graph as relations
# ─────────────────────────────────────────────────────────────────────────────
# One panel covers all three because fetchSnapshot() already routes by layer:
# 'entity' hits the prose entity graph and ANY OTHER name hits
# /fabric/graphs/snapshot?graph=<name>. So a code graph registered through
# fabric.graphs.register appears in this panel's source list with no change
# here and no change to vera_graph.js.

@APP.get("/ui/vera-graph-panel-explode.js", include_in_schema=False)
async def _serve_explode_panel_js():
    return Response(
        content=_read("vera_graph_panel_explode.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@capability(
    "ui.graph_panels.explode_js",
    http_method="GET",
    http_path="/ui/vera-graph-panel-explode.js",
    http_tags=["ui", "graph"],
    memory="off",
    silent=True,
    description="Serve the Explode sidebar panel companion JS for vera_graph.js "
                "— renders prose entities, any registered fabric graph, or a "
                "code graph as a relational graph.",
)
async def serve_explode_panel_js(trace_id=None):
    return Response(
        content=_read("vera_graph_panel_explode.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# <vera-graph-embed> — the bare inline graph for chat and canvas
# ─────────────────────────────────────────────────────────────────────────────
# Not a second renderer: it is vera_graph.js with every piece of chrome switched
# off through options vera_graph.js already exposes. A fork would drift, and the
# inline view and the full view would stop agreeing about what a graph looks
# like.

# the display modes (the exploded scene, the estate 3D and 2D) - vera-graph.js loads this itself
@APP.get("/ui/vera-graph-modes.js", include_in_schema=False)
async def _serve_graph_modes_js():
    return Response(content=_read("vera_graph_modes.js"), media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@APP.get("/ui/vera-graph-embed.js", include_in_schema=False)
async def _serve_graph_embed_js():
    return Response(
        content=_read("graph_embed_element.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@capability(
    "ui.graph_embed_js",
    http_method="GET",
    http_path="/ui/vera-graph-embed.js",
    http_tags=["ui", "graph"],
    memory="off",
    silent=True,
    description="Serve <vera-graph-embed>: a chrome-less graph element for "
                "inline use in chat turns and canvas blocks. Attributes: layer, "
                "params (JSON), limit, height, expand, full-url. Emits "
                "vera-graph-node on click so a host can scroll source to the "
                "symbol's start_line.",
)
async def serve_graph_embed_js(trace_id=None):
    return Response(
        content=_read("graph_embed_element.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )
