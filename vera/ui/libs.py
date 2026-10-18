# -*- coding: utf-8 -*-
"""
The shared UI libraries (UI redesign, Notes/40 section 2): the one ISO projection
and the one context-menu registry, served as plain scripts so every page draws
from the same copy.

  /ui/iso.js    -> vera/ui/iso.js    window.VeraISO   the ops map 3D, the canvas
                                                       exploded view, iso widgets,
                                                       the graph's isometric galaxy
  /ui/iso-lattice.js -> vera/ui/iso_lattice.js  window.VeraLattice  the iso lattice: planes x areas,
                                                       pipes, widgets, the view (Live ops, graph modes)
  /ui/menus.js  -> vera/ui/menus.js  window.MENUS     every context menu
  /ui/vera-kit.js -> vera/ui/vera_kit.js  window.VeraDrill / VeraLive / VeraStates + <vera-drill>, <vera-live-strip>:
                                           THE drill panel, live strip and states (looplab-control §9 K3)
  /ui/vera-timeline.js -> vera/ui/vera_timeline.js  window.VeraTimeline + <vera-timeline>: THE timeline
                                                       (lanes are records; looplab-control §9 K1)
  /ui/vera-matrix.js -> vera/ui/vera_matrix.js  window.VeraMatrix + <vera-matrix>: THE matrix (K2)
  /ui/routes.js -> vera/ui/routes.js window.VeraRoutes the run routers: the exploded
                                                       scene's cards and iso runs, the
                                                       structured graph's channels
  /ui/structgraph.js -> vera/ui/structgraph_element.js  <vera-structgraph>: code and
                                                       prose as bands, columns, cards
                                                       and routed runs
  /ui/desk-layout.js -> vera/ui/desk_layout_core.js  window.VeraDesk  the desk's layout
                                                       rules (sizes by distance to the chat,
                                                       arrangements, layers, making room)

Routes only - no state, no capabilities - so the module body may run more than
once (the namespace import trap) without a side effect.
"""
from __future__ import annotations

from pathlib import Path

from Vera.vera.capability_orchestration import APP  # noqa: F401

_HERE = Path(__file__).parent


def _style(name: str):
    from fastapi.responses import Response
    p = _HERE / name
    return Response(content=p.read_text(encoding="utf-8") if p.exists() else "/* %s not found */" % name, media_type="text/css")


def _script(name: str):
    from fastapi.responses import Response
    p = _HERE / name
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"), media_type="application/javascript")
    return Response(content="console.warn('%s not found');" % name, media_type="application/javascript")


@APP.get("/ui/iso.js", include_in_schema=False)
async def _serve_iso_js():
    return _script("iso.js")


@APP.get("/ui/iso-lattice.js", include_in_schema=False)
async def _serve_iso_lattice_js():
    return _script("iso_lattice.js")


@APP.get("/ui/vera-kit.js", include_in_schema=False)
async def _serve_vera_kit_js():
    return _script("vera_kit.js")


@APP.get("/ui/vera-timeline.js", include_in_schema=False)
async def _serve_vera_timeline_js():
    return _script("vera_timeline.js")


@APP.get("/ui/vera-matrix.js", include_in_schema=False)
async def _serve_vera_matrix_js():
    return _script("vera_matrix.js")


@APP.get("/ui/menus.js", include_in_schema=False)
async def _serve_menus_js():
    return _script("menus.js")


# the ONE design every panel wears (vera-ui.js loads it): the chat's tokens, glow, blocks, density and common parts
@APP.get("/ui/design.css", include_in_schema=False)
async def _serve_design_css():
    return _style("design.css")


# the PAGE a panel is built on (owner, 2026-10-04: "a bootstrap for widgets and ui panels"): grid, top bar, sections,
# cards, buttons, badges, key/value rows, tables - in the theme's names only. documentation/51-ui-design-system.md
@APP.get("/ui/bootstrap.css", include_in_schema=False)
async def _serve_bootstrap_css():
    return _style("bootstrap.css")


# the starting files - a panel, a widget (its record and its placing), a graph - copied to begin one; the capability
# ui.templates lists them for an agent. Only the files in templates/ - a name is never a path.
TEMPLATES = {
    "panel.html": "text/html", "widget.json": "application/json", "widget.html": "text/html", "graph.html": "text/html",
}


@APP.get("/ui/templates/{name}", include_in_schema=False)
async def _serve_template(name: str):
    from fastapi.responses import Response
    ct = TEMPLATES.get(name)
    p = _HERE / "templates" / name
    if not ct or not p.exists():
        return Response(content="no template %r - one of: %s" % (name, ", ".join(sorted(TEMPLATES))), status_code=404, media_type="text/plain")
    return Response(content=p.read_text(encoding="utf-8"), media_type=ct + "; charset=utf-8", headers={"Cache-Control": "no-cache"})

# the chat UI as an element (<vera-chat agent system session title>) any page can place
@APP.get("/ui/chat.js", include_in_schema=False)
async def _serve_chat_js():
    return _script("chat.js")


# the runtime every page draws the menu with (window.VeraRCM): its targets, the menu, the runner, thermal print
@APP.get("/ui/rcm.js", include_in_schema=False)
async def _serve_rcm_js():
    return _script("rcm.js")


@APP.get("/ui/routes.js", include_in_schema=False)
async def _serve_routes_js():
    return _script("routes.js")


@APP.get("/ui/structgraph.js", include_in_schema=False)
async def _serve_structgraph_js():
    return _script("structgraph_element.js")


# the desk's layout rules (window.VeraDesk; owner approved 2026-10-06): sizes by distance to the chat, arrangements,
# the chat's path, stacking, the layers and a panel's widgets making room - pure, Node-tested
@APP.get("/ui/desk-layout.js", include_in_schema=False)
async def _serve_desk_layout_js():
    return _script("desk_layout_core.js")
