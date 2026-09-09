"""
syscomms_capabilities.py — the System Comms pane
================================================

One ordered stream of everything Vera has said, flagged or filed: Telegram
traffic, the open action list, the nightly n8n review, and archived briefs.

Each of those already had a home; none of them had a shared one, so keeping up
meant opening four places and merging them by eye. `syscomms.feed` does that
merge, and the pane renders it as the System tab on the Comms page.

Sources are read through the existing capabilities rather than their stores, so
this adds no new coupling to Redis layouts or table shapes — and a source that
is unavailable degrades to an empty section with a reason, never a failed page.

Normalisation lives in `syscomms_core.py` (pure, unit-tested).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability, register_ui
from Vera.vera.syscomms import syscomms_core as sc

log = logging.getLogger("vera.syscomms")

_HERE = Path(__file__).parent
_PANEL = _HERE / "syscomms_panel.html"


async def _call(name: str, **kwargs) -> Dict[str, Any]:
    """Invoke another capability, tolerating its absence.

    The pane aggregates optional subsystems: Telegram may not be configured,
    the n8n datasets may not exist yet. A missing source should shrink the feed,
    not break the page, so every failure is returned as a reason instead.
    """
    cap = _orch.CAPABILITY_REGISTRY.get(name)
    if not cap:
        return {"__unavailable": f"{name} is not registered"}
    try:
        return await cap["func"](**kwargs) or {}
    except Exception as e:                       # noqa: BLE001 - reported, not raised
        log.debug("syscomms: %s failed", name, exc_info=True)
        return {"__unavailable": f"{name}: {str(e)[:160]}"}


def _rows(res: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Records out of a fabric.browse result."""
    out = []
    for r in (res or {}).get("records") or []:
        out.append(r.get("data") if isinstance(r, dict) and r.get("data") else r)
    return [r for r in out if isinstance(r, dict)]


@capability(
    "syscomms.feed", http_method="GET", http_path="/syscomms/feed",
    http_tags=["comms"], memory="off",
    description="One ordered stream of everything the system has said or "
                "flagged: Telegram traffic, open action items, failing n8n "
                "workflows and archived briefs. Newest first. "
                "Input: limit (int=80), sources (csv — telegram,actions,n8n,"
                "reports), min_severity (''|info|warning|critical). "
                "Output: {feed:[{ts,source,kind,severity,title,detail,ref}], "
                "summary, unavailable[]}.",
)
async def cap_feed(limit: int = 80, sources: str = "", min_severity: str = "",
                   trace_id=None):
    want = {s.strip().lower() for s in (sources or "").split(",") if s.strip()}
    streams: List[List[Dict[str, Any]]] = []
    unavailable: List[str] = []

    def _maybe(name: str) -> bool:
        return not want or name in want

    if _maybe("telegram"):
        res = await _call("tg.messages", limit=60)
        if res.get("__unavailable"):
            unavailable.append(res["__unavailable"])
        else:
            msgs = res.get("messages") or res.get("history") or []
            streams.append(sc.from_telegram(msgs))

    if _maybe("actions"):
        res = await _call("fabric.browse", dataset_id="vera.action_items",
                          limit=200)
        if res.get("__unavailable"):
            unavailable.append(res["__unavailable"])
        else:
            streams.append(sc.from_actions(_rows(res)))

    if _maybe("n8n"):
        res = await _call("fabric.browse", dataset_id="vera.n8n.health",
                          limit=100)
        if res.get("__unavailable"):
            unavailable.append(res["__unavailable"])
        else:
            streams.append(sc.from_n8n_health(_rows(res)))

    if _maybe("reports"):
        res = await _call("memory.seek", query="daily brief report digest",
                          k=12, max_chars=400)
        if res.get("__unavailable"):
            unavailable.append(res["__unavailable"])
        else:
            recs = res.get("records") or res.get("results") or []
            streams.append(sc.from_reports(
                [r.get("data") if isinstance(r, dict) and r.get("data") else r
                 for r in recs if isinstance(r, dict)]))

    feed = sc.merge_feed(streams, limit=limit, sources=want or None,
                         min_severity=min_severity)
    return {"feed": feed, "summary": sc.summarise(feed),
            "unavailable": unavailable}


@capability(
    "syscomms.panel.html", http_method="GET", http_path="/syscomms/panel",
    http_tags=["comms", "ui"], memory="off", silent=True,
    description="Serve the System Comms pane HTML.",
)
async def cap_panel_html(trace_id=None):
    try:
        return HTMLResponse(_PANEL.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HTMLResponse("<p style='color:red'>syscomms_panel.html missing</p>")


@APP.get("/syscomms/panel", include_in_schema=False)
async def _syscomms_panel_route():
    return HTMLResponse(_PANEL.read_text(encoding="utf-8") if _PANEL.exists()
                        else "<p style='color:red'>syscomms_panel.html missing</p>")


# Registered as an element rather than a top-level tab: its home is the System
# sub-tab of the Comms page, which embeds /syscomms/panel directly.
register_ui(
    "syscomms", "System Comms", "📡",
    """<div style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/syscomms/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write"></iframe>
</div>""",
    "",
    ui_caps=["syscomms.feed"],
    mode="inject",
    tab_order=71,
)

log.info("syscomms_capabilities: ready")
