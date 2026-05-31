"""
observe_elements_capabilities.py
=================================
Registers two injectable custom elements ported from the Observe tab:

1. <vera-live-event-stream>  — Live WS event stream with category chips,
   search filter, subscribe, pause/resume.  Full port of the left panel.

2. <vera-system-log>  — Syslog query UI with level/category/keyword
   filtering, expandable entries with traceback, monitor controls,
   and ask-agent diagnosis.  Full port of the right panel.

Both are served as standalone JS at /ui/elements/*.js and registered
via register_ui(mode="inject") so they appear in the panel/element
picker and can be loaded into any dashboard grid slot.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi.responses import Response

from Vera.Orchestration.capability_orchestration import (
    APP,
    capability,
    register_ui,
)

log = logging.getLogger("vera.observe_elements")

_HERE = Path(__file__).resolve().parent


# ─────────────────────────────────────────────────────────────────────────────
# Live Event Stream element
# ─────────────────────────────────────────────────────────────────────────────

_LES_JS_PATH = _HERE / "live_event_stream_element.js"


def _les_js():
    try:
        return _LES_JS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "/* live_event_stream_element.js not found */"


@APP.get("/ui/elements/live_event_stream.js", include_in_schema=False)
async def _serve_les_js():
    return Response(
        content=_les_js(),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@capability(
    "ui.elements.live_event_stream_js",
    http_method="GET",
    http_path="/ui/elements/live_event_stream.js",
    http_tags=["ui", "observe"],
    memory="off",
    silent=True,
    description="Serve the <vera-live-event-stream> custom element JS.",
)
async def serve_les_js(trace_id=None):
    return Response(
        content=_les_js(),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


_LES_INJECT_HTML = (
    '<script id="vera-live-event-stream-js-include" '
    'src="/ui/elements/live_event_stream.js"></script>\n'
    '<vera-live-event-stream style="display:block;width:100%;height:100%">'
    '</vera-live-event-stream>'
)

register_ui(
    panel_id="live-event-stream",
    label="Live Event Stream",
    icon="⚡",
    mode="inject",
    tab_order=210,
    html=_LES_INJECT_HTML,
    ui_caps=["ui.elements.live_event_stream_js"],
)


# ─────────────────────────────────────────────────────────────────────────────
# System Log element
# ─────────────────────────────────────────────────────────────────────────────

_SL_JS_PATH = _HERE / "system_log_element.js"


def _sl_js():
    try:
        return _SL_JS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "/* system_log_element.js not found */"


@APP.get("/ui/elements/system_log.js", include_in_schema=False)
async def _serve_sl_js():
    return Response(
        content=_sl_js(),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@capability(
    "ui.elements.system_log_js",
    http_method="GET",
    http_path="/ui/elements/system_log.js",
    http_tags=["ui", "observe"],
    memory="off",
    silent=True,
    description="Serve the <vera-system-log> custom element JS.",
)
async def serve_sl_js(trace_id=None):
    return Response(
        content=_sl_js(),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


_SL_INJECT_HTML = (
    '<script id="vera-system-log-js-include" '
    'src="/ui/elements/system_log.js"></script>\n'
    '<vera-system-log style="display:block;width:100%;height:100%">'
    '</vera-system-log>'
)

register_ui(
    panel_id="system-log",
    label="System Log",
    icon="📋",
    mode="inject",
    tab_order=211,
    html=_SL_INJECT_HTML,
    ui_caps=[
        "ui.elements.system_log_js",
        "syslog.query",
        "syslog.clear",
        "syslog.ask",
        "syslog.monitor_start",
        "syslog.monitor_stop",
        "syslog.monitor_run",
    ],
)


log.info(
    "observe-elements: registered <vera-live-event-stream> (inject) "
    "and <vera-system-log> (inject); JS at /ui/elements/*.js"
)