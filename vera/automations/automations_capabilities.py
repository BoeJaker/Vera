"""
automations_capabilities.py — one page for everything that runs on its own
==========================================================================

Vera's automation surfaces grew up separately: DAGs in the Workshop, n8n in its
own panel and its own web UI, OpenClaw in a third, and the action list only as a
webhook returning JSON. Each is fine on its own; together they meant four places
to look and no single answer to "what is running, and is any of it broken?".

This is the hub. `automations.overview` answers that question in one call, and
the panel puts the existing surfaces behind one left-hand menu rather than
reimplementing any of them.

Every source is read through its own capability and each is optional: a
subsystem that is unavailable shrinks the overview and says why, rather than
failing the page.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability, register_ui

log = logging.getLogger("vera.automations")

_HERE = Path(__file__).parent
_PANEL = _HERE / "automations_panel.html"


async def _call(name: str, **kwargs) -> Dict[str, Any]:
    """Invoke a capability, reporting absence instead of raising.

    The hub aggregates optional subsystems — n8n may not be configured, OpenClaw
    may not be connected. A missing one should shrink the overview, not break it.
    """
    cap = _orch.CAPABILITY_REGISTRY.get(name)
    if not cap:
        return {"__unavailable": f"{name} is not registered"}
    try:
        return await cap["func"](**kwargs) or {}
    except Exception as e:                      # noqa: BLE001 — reported, not raised
        log.debug("automations: %s failed", name, exc_info=True)
        return {"__unavailable": f"{name}: {str(e)[:140]}"}


def _count(v: Any) -> int:
    if isinstance(v, list):
        return len(v)
    if isinstance(v, dict):
        for k in ("count", "total"):
            if isinstance(v.get(k), int):
                return v[k]
    return 0


@capability(
    "automations.overview", http_method="GET", http_path="/automations/overview",
    http_tags=["automations"], memory="off",
    description="One answer to 'what is running, and is any of it broken?' "
                "across DAGs, n8n workflows, the action list and the OpenClaw "
                "queue. Each source is optional and reports its own absence. "
                "Output: {sections:[{id,label,stat,detail,severity,href}], "
                "open_actions, unavailable[]}.",
)
async def cap_overview(trace_id=None):
    sections: List[Dict[str, Any]] = []
    unavailable: List[str] = []

    # ── DAGs registered as capabilities ─────────────────────────────────────
    dags = await _call("dag.list_registered")
    if dags.get("__unavailable"):
        unavailable.append(dags["__unavailable"])
    else:
        n = _count(dags.get("dags") or dags.get("registered") or dags)
        sections.append({"id": "dags", "label": "DAG flows", "stat": n,
                         "detail": "registered as capabilities",
                         "severity": "info", "href": "flows"})

    # ── n8n workflows ───────────────────────────────────────────────────────
    wf = await _call("n8n.workflow.list", limit=250)
    if wf.get("__unavailable") or wf.get("error"):
        unavailable.append(wf.get("__unavailable") or f"n8n: {wf.get('error')}")
    else:
        rows = wf.get("workflows") or []
        active = sum(1 for w in rows if w.get("active"))
        sections.append({
            "id": "n8n", "label": "n8n workflows", "stat": len(rows),
            "detail": f"{active} active, {len(rows) - active} inactive",
            "severity": "info" if active else "warning", "href": "n8n"})

    # ── the action list ─────────────────────────────────────────────────────
    acts = await _call("fabric.browse", dataset_id="vera.action_items", limit=300)
    open_actions = 0
    if acts.get("__unavailable"):
        unavailable.append(acts["__unavailable"])
    else:
        rows = [r.get("data") if isinstance(r, dict) and r.get("data") else r
                for r in (acts.get("records") or [])]
        rows = [r for r in rows if isinstance(r, dict)]
        open_rows = [r for r in rows if r.get("status") != "done"]
        open_actions = len(open_rows)
        high = sum(1 for r in open_rows
                   if str(r.get("priority", "")).lower() in ("high", "critical"))
        sections.append({
            "id": "actions", "label": "Open actions", "stat": open_actions,
            "detail": f"{high} high priority" if high else "none urgent",
            "severity": "warning" if high else "info", "href": "actions"})

    # ── OpenClaw queue ──────────────────────────────────────────────────────
    q = await _call("openclaw.queue.list")
    if q.get("__unavailable") or q.get("error"):
        unavailable.append(q.get("__unavailable") or f"openclaw: {q.get('error')}")
    else:
        n = _count(q.get("queue") or q.get("items") or q)
        sections.append({"id": "openclaw", "label": "OpenClaw queue", "stat": n,
                         "detail": "queued prompts", "severity": "info",
                         "href": "openclaw"})

    return {"sections": sections, "open_actions": open_actions,
            "unavailable": unavailable}


@capability(
    "automations.actions", http_method="GET", http_path="/automations/actions",
    http_tags=["automations"], memory="off",
    description="The open action list, read straight from the fabric rather "
                "than through n8n's webhook — no cross-origin fetch, and it "
                "still works when n8n is down. Input: include_done (bool). "
                "Output: {items:[{id,text,priority,status,created}], open, closed}.",
)
async def cap_actions(include_done: bool = False, trace_id=None):
    res = await _call("fabric.browse", dataset_id="vera.action_items", limit=300)
    if res.get("__unavailable"):
        return {"error": res["__unavailable"], "items": [], "open": 0, "closed": 0}
    rows = [r.get("data") if isinstance(r, dict) and r.get("data") else r
            for r in (res.get("records") or [])]
    rows = [r for r in rows if isinstance(r, dict) and r.get("text")]

    rank = {"critical": 0, "high": 0, "medium": 1, "warning": 1,
            "normal": 2, "low": 3}
    open_rows = [r for r in rows if r.get("status") != "done"]
    done_rows = [r for r in rows if r.get("status") == "done"]
    open_rows.sort(key=lambda r: (rank.get(str(r.get("priority", "")).lower(), 2),
                                  str(r.get("created", ""))))
    items = open_rows + (done_rows if include_done else [])
    return {"items": items, "open": len(open_rows), "closed": len(done_rows)}


@capability(
    "automations.panel.html", http_method="GET", http_path="/automations/panel",
    http_tags=["automations", "ui"], memory="off", silent=True,
    description="Serve the Automations hub HTML.",
)
async def cap_panel_html(trace_id=None):
    try:
        return HTMLResponse(_PANEL.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HTMLResponse("<p style='color:red'>automations_panel.html missing</p>")


@APP.get("/automations/panel", include_in_schema=False)
async def _automations_panel_route():
    return HTMLResponse(_PANEL.read_text(encoding="utf-8") if _PANEL.exists()
                        else "<p style='color:red'>automations_panel.html missing</p>")


register_ui(
    "automations", "Automations", "⚙",
    """<div style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/automations/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write"></iframe>
</div>""",
    "",
    ui_caps=["automations.overview", "automations.actions"],
    mode="tab",
    tab_order=67,
)

log.info("automations_capabilities: ready")
