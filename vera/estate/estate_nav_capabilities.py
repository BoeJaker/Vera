"""
estate_nav_capabilities.py -- the switch for top-level tabs folded into other tabs
==================================================================================

Some top-level tabs now open inside a broader tab. Estate covers Proxmox,
Remote, Net Policy, Security, Identity, Provision, Integrations and Platforms;
Capabilities covers Cap Ontology and MCP Servers; Agents covers Agent Bridges;
Image Studio covers Companion. While this setting is on (the default) those
tabs leave the tab bar, and anything that opens one lands on the matching pane
instead. Their routes and capabilities keep working either way.

The map lives in estate_nav_core.py; ui.panels applies it.

Capabilities
------------
  ui.tabs.retired       whether the setting is on, and where each tab now opens
  ui.tabs.retired.set   turn it on or off (the tab bar changes on the next load)

Redis layout
------------
  vera:ui:retire_overlap_tabs   string "1" | "0"; absent means on
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi.responses import HTMLResponse, RedirectResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, emit_event
from Vera.vera.estate import estate_nav_core as nav


async def _enabled() -> bool:
    r = getattr(_orch, "REDIS", None)
    raw = None
    if r is not None:
        try:
            raw = await r.get(nav.RETIRE_SETTING_KEY)
        except Exception:
            raw = None
    return nav.setting_enabled(raw)


def _state(enabled: bool) -> Dict[str, Any]:
    tabs = {}
    for pid, target in nav.RETIRED_TABS.items():
        tabs[pid] = dict(target, label=(_orch.UI_PANELS.get(pid) or {}).get("label", pid))
    return {"enabled": enabled, "tabs": tabs,
            "note": "The tab bar changes on the next page load."}


@capability(
    "ui.tabs.retired",
    http_method="GET", http_path="/ui/tabs/retired", http_tags=["ui", "estate"],
    memory="off", silent=True,
    description="Whether the top-level tabs folded into a broader tab are retired, and which "
                "pane each one opens instead. Estate covers Proxmox, Remote, Net Policy, "
                "Security, Identity, Provision, Integrations and Platforms; Capabilities covers Cap "
                "Ontology and MCP Servers; Agents covers Agent Bridges; Image Studio covers "
                "Companion. Output: {enabled, tabs:{panel_id: {label, panel, pane, sub, "
                "section}}, note}.",
)
async def cap_tabs_retired(trace_id=None) -> Dict[str, Any]:
    return _state(await _enabled())


@capability(
    "ui.tabs.retired.set",
    http_method="POST", http_path="/ui/tabs/retired/set", http_tags=["ui", "estate"],
    memory="off",
    description="Retire (enabled=true, the default) or bring back (enabled=false) the "
                "top-level tabs folded into a broader tab (Estate, Capabilities, Agents, "
                "Image Studio). Nothing is deleted: routes and capabilities keep working, and "
                "the tab bar changes on the next page load. "
                "Input: enabled (bool). Output: same as ui.tabs.retired.",
)
async def cap_tabs_retired_set(enabled: bool = True, trace_id=None) -> Dict[str, Any]:
    on = nav.setting_enabled(enabled)
    r = getattr(_orch, "REDIS", None)
    if r is None:
        return {"error": "Vera has no Redis connection, so the setting cannot be saved"}
    await r.set(nav.RETIRE_SETTING_KEY, "1" if on else "0")
    await emit_event({"type": "ui.tabs.retired", "enabled": on})
    return _state(on)


_NETCTL_MISSING = """<!doctype html><meta charset="utf-8">
<body style="margin:0;font:12px system-ui,sans-serif;background:var(--bg0,#0d0f12);color:#9aa3ad;
display:flex;align-items:center;justify-content:center;height:100vh">
<div style="max-width:52ch;line-height:1.6">
<b style="color:#d8dde3">netctl is not registered yet.</b><br>
Add it under Estate &rsaquo; Integrations &rsaquo; Services with a label that starts with
<code>netctl</code> (for example <code>netctl (NWM-02)</code>, base URL
<code>http://192.168.0.221:8088</code>) and allow embedding. Its pages still ask for
netctl's own password.</div></body>"""


async def _netctl_integration() -> Dict[str, Any]:
    fn = (_orch.CAPABILITY_REGISTRY.get("integration.list") or {}).get("func")
    if fn is None:
        return {}
    try:
        res = await fn()
    except Exception:
        return {}
    return nav.netctl_record(res.get("integrations") or [] if isinstance(res, dict) else [])


@_orch.APP.get("/estate/netctl", include_in_schema=False)
async def _estate_netctl():
    """Network & Access > netctl: netctl's own pages through Vera's embed proxy.
    netctl still asks for its own password; Vera only relays the pages."""
    rec = await _netctl_integration()
    if not rec.get("id"):
        return HTMLResponse(_NETCTL_MISSING, status_code=404)
    return RedirectResponse(f"/integrations/{rec['id']}/embed/", status_code=307)
