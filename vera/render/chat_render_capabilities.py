"""
chat_render_capabilities.py  —  In-chat visual rendering for agents
===================================================================
Lets Vera (chat directives, the V5 agentic loop, dream pipelines, DAG steps)
put LIVE VISUALS in front of the user instead of walls of text:

  render.mermaid  — display a Mermaid diagram in the chat, drawn by Vera's own
                    <vera-mermaid> renderer (no CDN, theme-aware, pan/zoom,
                    SVG/PNG export). Supports flowchart/graph, sequenceDiagram,
                    stateDiagram and pie.
  render.html     — display an arbitrary HTML/CSS/JS snippet (charts, widgets,
                    mini-UIs) in a sandboxed iframe card — inline in the chat
                    or floating over the UI as a pop-out.
  render.chart    — quick data chart (bar | line | pie) from a simple spec;
                    drawn client-side as themed SVG.
  render.screen   — float ANY registered UI panel (including ones built
                    on-the-fly with ui.panel.create) over the chat as a
                    pop-out window. This is how the UI builder shows its work
                    without navigating away.

All four route through the existing panel-dispatch bridge: the cap publishes
a `__chat_render__` pseudo-action on the session's Redis channel; the chat
panel (chat_panel.html) renders the payload and acks. If no chat session is
listening the cap returns {ok:false, error:"timeout"} — harmless.

The <vera-mermaid> element source lives in vera_mermaid.js next to this file
and is served at /ui/elements/vera_mermaid.js (re-read per request so you can
iterate without a server restart).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from Vera.vera.capability_orchestration import (
    APP,
    CAPABILITY_REGISTRY,
    capability,
    emit_event,
    now_iso,
    register_ui,
)

log = logging.getLogger("vera.render.chat")

_HERE = Path(__file__).parent


def _cap(name: str):
    entry = CAPABILITY_REGISTRY.get(name)
    return entry.get("func") if isinstance(entry, dict) else None


async def _dispatch_render(session_id: str, payload: Dict[str, Any],
                           timeout_secs: float = 8.0) -> Dict[str, Any]:
    dispatch = _cap("panel.dispatch")
    if not dispatch:
        return {"ok": False, "error": "panel.dispatch unavailable"}
    sid = (session_id or "").strip()
    if not sid:
        return {"ok": False, "error": "session_id required (pass the chat "
                                      "session id so the render reaches the "
                                      "right conversation)"}
    try:
        reply = await dispatch(session_id=sid, action="__chat_render__",
                               payload=payload, timeout_secs=timeout_secs)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    ok = bool(reply.get("ok")) if isinstance(reply, dict) else False
    out = {"ok": ok, "rendered": ok}
    if isinstance(reply, dict) and reply.get("error"):
        out["error"] = reply["error"]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# CAPS
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "render.mermaid",
    http_method="POST", http_path="/render/mermaid", http_tags=["render", "ui"],
    memory="off",
    description="Draw a Mermaid diagram in the user's chat (Vera's own themed "
                "renderer — flowchart/graph TD|LR, sequenceDiagram, stateDiagram, "
                "pie). Prefer this over ASCII art for flows, architectures, "
                "sequences and proportions. TIP: in a chat reply you can also just "
                "emit a ```mermaid fenced block — it renders inline automatically; "
                "use this cap from loops/dreams or to pop the diagram OVER the UI. "
                "Input: code (str! — mermaid source), title (str), "
                "session_id (str! — chat session id), popout (bool — float over the "
                "UI instead of only inline, default false). Output: {ok, rendered}.",
)
async def cap_render_mermaid(code: str = "", title: str = "Diagram",
                             session_id: str = "", popout: bool = False,
                             trace_id=None) -> Dict[str, Any]:
    if not (code or "").strip():
        return {"error": "code required (mermaid source)"}
    await emit_event({"type": "render.push", "what": "mermaid",
                      "session_id": session_id, "title": title})
    return await _dispatch_render(session_id or (trace_id or ""), {
        "kind": "mermaid", "title": title or "Diagram",
        "content": code, "popout": bool(popout),
    })


@capability(
    "render.html",
    http_method="POST", http_path="/render/html", http_tags=["render", "ui"],
    memory="off",
    description="Render an HTML/CSS/JS snippet for the user — a chart, widget, "
                "table, animation or mini-UI — in a SANDBOXED iframe card in the "
                "chat, optionally floating over the whole UI as a pop-out window. "
                "The snippet is self-contained (inline styles/scripts; no external "
                "network access guaranteed). "
                "Input: html (str! — the snippet or full document), title (str), "
                "session_id (str! — chat session id), popout (bool, default true), "
                "height (int px for the inline card, default 380). "
                "Output: {ok, rendered}.",
)
async def cap_render_html(html: str = "", title: str = "Preview",
                          session_id: str = "", popout: bool = True,
                          height: int = 380, trace_id=None) -> Dict[str, Any]:
    if not (html or "").strip():
        return {"error": "html required"}
    await emit_event({"type": "render.push", "what": "html",
                      "session_id": session_id, "title": title})
    return await _dispatch_render(session_id or (trace_id or ""), {
        "kind": "html", "title": title or "Preview",
        "content": html, "popout": bool(popout),
        "height": max(120, min(1600, int(height or 380))),
    })


@capability(
    "render.chart",
    http_method="POST", http_path="/render/chart", http_tags=["render", "ui"],
    memory="off",
    description="Draw a quick data chart in the user's chat as themed SVG. "
                "Input: spec (object! — {type:'bar'|'line'|'pie', labels:[...], "
                "series:[{name,values:[...]}], title?, y_label?}), title (str), "
                "session_id (str! — chat session id), popout (bool, default false). "
                "For anything fancier (stacked, scatter, live) build it with "
                "render.html instead. Output: {ok, rendered}.",
)
async def cap_render_chart(spec: Optional[Dict[str, Any]] = None, title: str = "",
                           session_id: str = "", popout: bool = False,
                           trace_id=None) -> Dict[str, Any]:
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except Exception:
            return {"error": "spec must be a JSON object"}
    if not isinstance(spec, dict) or not spec.get("type"):
        return {"error": "spec required — {type:'bar'|'line'|'pie', labels:[…], "
                         "series:[{name,values:[…]}]}"}
    await emit_event({"type": "render.push", "what": "chart",
                      "session_id": session_id, "title": title or spec.get("title", "")})
    return await _dispatch_render(session_id or (trace_id or ""), {
        "kind": "chart", "title": title or spec.get("title") or "Chart",
        "spec": spec, "popout": bool(popout),
    })


@capability(
    "render.screen",
    http_method="POST", http_path="/render/screen", http_tags=["render", "ui"],
    memory="off",
    description="Float a registered UI panel over the chat as a pop-out window — "
                "works for built-in panels AND panels just created with "
                "ui.panel.create, so an agent can BUILD a UI and SHOW it in the "
                "same turn without navigating tabs. "
                "Input: panel_id (str! — id from ui.panel.list), session_id (str! — "
                "chat session id), title (str — window title, defaults to the "
                "panel label). Output: {ok, rendered}.",
)
async def cap_render_screen(panel_id: str = "", session_id: str = "",
                            title: str = "", trace_id=None) -> Dict[str, Any]:
    if not (panel_id or "").strip():
        return {"error": "panel_id required"}
    await emit_event({"type": "render.push", "what": "panel",
                      "session_id": session_id, "panel_id": panel_id})
    return await _dispatch_render(session_id or (trace_id or ""), {
        "kind": "panel", "panel_id": panel_id.strip(),
        "title": title, "popout": True,
    })


# ─────────────────────────────────────────────────────────────────────────────
# ELEMENT SERVING — <vera-mermaid>
# ─────────────────────────────────────────────────────────────────────────────

@APP.get("/ui/elements/vera_mermaid.js", include_in_schema=False)
async def _serve_vera_mermaid_js():
    from fastapi.responses import Response
    p = _HERE / "vera_mermaid.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript")
    return Response(content="console.warn('vera_mermaid.js not found');",
                    media_type="application/javascript")


# Injectable element registration — same pattern as agent-loop-output: any
# panel can drop <vera-mermaid> after including the script once.
register_ui(
    panel_id="vera-mermaid",
    label="Mermaid Diagram",
    icon="⧉",
    mode="inject",
    tab_order=207,
    html=('<script id="vera-mermaid-js-include" '
          'src="/ui/elements/vera_mermaid.js"></script>\n'
          '<vera-mermaid style="display:block;width:100%;height:100%"></vera-mermaid>'),
    ui_caps=[],
)

log.info("chat_render: render.mermaid/html/chart/screen registered; "
         "<vera-mermaid> served at /ui/elements/vera_mermaid.js")
