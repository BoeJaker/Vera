# -*- coding: utf-8 -*-
"""
The shared UI libraries (UI redesign, Notes/40 section 2): the one ISO projection
and the one context-menu registry, served as plain scripts so every page draws
from the same copy.

  /ui/iso.js    -> vera/ui/iso.js    window.VeraISO   the ops map 3D, the canvas
                                                       exploded view, iso widgets,
                                                       the graph's isometric galaxy
  /ui/menus.js  -> vera/ui/menus.js  window.MENUS     every context menu

Routes only - no state, no capabilities - so the module body may run more than
once (the namespace import trap) without a side effect.
"""
from __future__ import annotations

from pathlib import Path

from Vera.vera.capability_orchestration import APP  # noqa: F401

_HERE = Path(__file__).parent


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
