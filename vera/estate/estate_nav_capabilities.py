"""
estate_nav_capabilities.py -- the switch for the top-level tabs Estate replaced
============================================================================

Seven top-level tabs manage the same machines, network and identity as the
Estate tab: Proxmox, Remote, Net Policy, Security, Identity, Provision and
Integrations. While this setting is on (the default) they leave the tab bar,
and anything that opens one lands on the matching Estate pane instead. Their
routes and capabilities keep working either way.

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
    description="Whether the top-level tabs that the Estate tab replaced (Proxmox, Remote, "
                "Net Policy, Security, Identity, Provision, Integrations) are retired, and "
                "which Estate pane each one opens instead. Output: {enabled, tabs:{panel_id: "
                "{label, panel, pane, sub, section}}, note}.",
)
async def cap_tabs_retired(trace_id=None) -> Dict[str, Any]:
    return _state(await _enabled())


@capability(
    "ui.tabs.retired.set",
    http_method="POST", http_path="/ui/tabs/retired/set", http_tags=["ui", "estate"],
    memory="off",
    description="Retire (enabled=true, the default) or bring back (enabled=false) the "
                "top-level tabs that the Estate tab replaced. Nothing is deleted: routes and "
                "capabilities keep working, and the tab bar changes on the next page load. "
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
