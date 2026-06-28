"""
flow_builder_capabilities.py
============================
Registers the <vera-flow-builder> custom element with Vera's caps / panels /
elements system so the DAG-Workshop-grade visual flow builder can be reused
anywhere — DAG Workshop, dream pipelines, the fabric query builder, research,
or any future panel — without duplicating the implementation.

This mirrors the established reusable-element pattern (see
agent_loop_output_capabilities.py and general_widgets_capabilities.py):

1. **GET /ui/elements/flow_builder.js** — serves the element definition as a
   standalone JS include. Any panel can `<script src=…>` it once and then drop
   `<vera-flow-builder>` wherever it needs a flow builder.

2. **register_ui("flow-builder", mode="inject", …)** — registers the element as
   an injectable UI panel so it shows up in the panels menu / widget picker.

The element is DOMAIN-AGNOSTIC. Each host feeds it a small `provider` object
(via `el.setProvider(obj)` or the `provider="name"` attribute backed by the
window.VeraFlowProviders registry) that adapts the builder to its domain —
palette source, node schema, and (de)serialization to the host's own document
format. See the JS file header for the full provider interface.

The element source lives in flow_builder_element.js next to this file. We read
it from disk on each request so iterating on the JS doesn't require a restart.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi.responses import Response

from Vera.vera.capability_orchestration import (
    APP,
    capability,
    register_ui,
)

log = logging.getLogger("vera.flow_builder")

_HERE = Path(__file__).resolve().parent


def _read(name: str) -> str:
    try:
        return (_HERE / name).read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"/* {name} not found */"


# ─────────────────────────────────────────────────────────────────────────────
# Serve the element JS (raw route + @capability mirror, like chat_data)
# ─────────────────────────────────────────────────────────────────────────────
@APP.get("/ui/elements/flow_builder.js", include_in_schema=False)
async def _serve_flow_builder_js():
    return Response(
        content=_read("flow_builder_element.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@capability(
    "ui.elements.flow_builder_js",
    http_method="GET",
    http_path="/ui/elements/flow_builder.js",
    http_tags=["ui", "widgets"],
    memory="off",
    silent=True,
    description="Serve the <vera-flow-builder> reusable visual flow builder element JS.",
)
async def serve_flow_builder_js(trace_id=None):
    return Response(
        content=_read("flow_builder_element.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Serve VeraFlowCaps — the shared capabilities-palette helper so any panel's
# flow-builder provider can drop caps onto the canvas (loads /workshop/cap_tree).
# ─────────────────────────────────────────────────────────────────────────────
@APP.get("/ui/elements/flow_caps.js", include_in_schema=False)
async def _serve_flow_caps_js():
    return Response(
        content=_read("flow_caps.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@capability(
    "ui.elements.flow_caps_js",
    http_method="GET",
    http_path="/ui/elements/flow_caps.js",
    http_tags=["ui", "widgets"],
    memory="off",
    silent=True,
    description="Serve the VeraFlowCaps shared capabilities-palette JS for the flow builder.",
)
async def serve_flow_caps_js(trace_id=None):
    return Response(
        content=_read("flow_caps.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Register as an injectable UI element. The element needs a provider to do
# anything, so the bare inject is mostly a discovery/registration hook — hosts
# normally drop the <script src> + tag directly and call el.setProvider(…).
# ─────────────────────────────────────────────────────────────────────────────
_INJECT_HTML = (
    '<script id="vera-flow-builder-js-include" '
    'src="/ui/elements/flow_builder.js"></script>\n'
    '<vera-flow-builder style="display:block;width:100%;height:100%"></vera-flow-builder>'
)

register_ui(
    panel_id="flow-builder",
    label="Flow Builder",
    icon="🕸",
    mode="inject",
    tab_order=206,
    html=_INJECT_HTML,
    ui_caps=["ui.elements.flow_builder_js"],
)

log.info(
    "flow-builder: registered <vera-flow-builder> (inject); "
    "JS at /ui/elements/flow_builder.js"
)
