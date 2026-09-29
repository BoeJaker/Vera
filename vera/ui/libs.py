# -*- coding: utf-8 -*-
"""
The shared UI libraries (UI redesign, Notes/40 section 2): the one ISO projection
and the one context-menu registry, served as plain scripts so every page draws
from the same copy.

  /ui/iso.js    -> vera/ui/iso.js    window.VeraISO   the ops map 3D, the canvas
                                                       exploded view, iso widgets,
                                                       the graph's isometric galaxy
  /ui/menus.js  -> vera/ui/menus.js  window.MENUS     every context menu
  /ui/routes.js -> vera/ui/routes.js window.VeraRoutes the run routers: the exploded
                                                       scene's cards and iso runs, the
                                                       structured graph's channels
  /ui/structgraph.js -> vera/ui/structgraph_element.js  <vera-structgraph>: code and
                                                       prose as bands, columns, cards
                                                       and routed runs

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


@APP.get("/ui/menus.js", include_in_schema=False)
async def _serve_menus_js():
    return _script("menus.js")


# the ONE design every panel wears (vera-ui.js loads it): the chat's tokens, glow, blocks, density and common parts
@APP.get("/ui/design.css", include_in_schema=False)
async def _serve_design_css():
    return _style("design.css")


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
