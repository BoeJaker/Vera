"""
general_widgets_capabilities.py
===============================
Registers general-purpose, reusable injectable widgets that aren't tied to a
single feature area. Each is a self-contained custom element served as
standalone JS at /ui/elements/*.js and registered via register_ui(mode="inject")
so it shows up in the dashboard widget picker and can be dropped into any grid.

Currently:
  • <vera-chat-data>  — "Chat with Data": conversational widget backed by the
    assistant agent (which has data-fabric / capability / memory tool access).
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

log = logging.getLogger("vera.general_widgets")

_HERE = Path(__file__).resolve().parent


def _read(name: str) -> str:
    try:
        return (_HERE / name).read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"/* {name} not found */"


# ─────────────────────────────────────────────────────────────────────────────
# Chat with Data widget
# ─────────────────────────────────────────────────────────────────────────────

@APP.get("/ui/elements/chat_data.js", include_in_schema=False)
async def _serve_chat_data_js():
    return Response(
        content=_read("chat_data_element.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@capability(
    "ui.elements.chat_data_js",
    http_method="GET",
    http_path="/ui/elements/chat_data.js",
    http_tags=["ui", "widgets"],
    memory="off",
    silent=True,
    description="Serve the <vera-chat-data> (Chat with Data) custom element JS.",
)
async def serve_chat_data_js(trace_id=None):
    return Response(
        content=_read("chat_data_element.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


_CHAT_DATA_INJECT_HTML = (
    '<script id="vera-chat-data-js-include" '
    'src="/ui/elements/chat_data.js"></script>\n'
    '<vera-chat-data style="display:block;width:100%;height:100%">'
    '</vera-chat-data>'
)

register_ui(
    panel_id="chat-with-data",
    label="Chat with Data",
    icon="💬",
    mode="inject",
    tab_order=205,
    html=_CHAT_DATA_INJECT_HTML,
    ui_caps=["ui.elements.chat_data_js", "agent.chat"],
)


log.info("general-widgets: registered <vera-chat-data> (inject); JS at /ui/elements/chat_data.js")
