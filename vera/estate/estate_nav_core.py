"""Which top-level tabs the Estate tab replaces, where each one now opens, and
the one setting that brings them back.

Pure data plus two functions, so it tests without booting Vera
(tests/test_estate_nav_core.py). ui.panels annotates the registry with it, and
the shell hides a tab that carries `retired_into`, opening that Estate pane
instead. Routes and capabilities are untouched: turning the setting off
restores the old tab bar on the next page load.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping

RETIRE_SETTING_KEY = "vera:ui:retire_overlap_tabs"

# panel id -> the Estate pane (and sub-tab) that now covers it
RETIRED_TABS: Dict[str, Dict[str, str]] = {
    "proxmox-panel":      {"panel": "workers-ollama", "pane": "proxmox",      "sub": "",         "section": "Machines"},
    "remote-connections": {"panel": "workers-ollama", "pane": "remote",       "sub": "",         "section": "Machines"},
    "netgraph-panel":     {"panel": "workers-ollama", "pane": "network",      "sub": "graph",    "section": "Network & Access"},
    "provisioning-panel": {"panel": "workers-ollama", "pane": "provision",    "sub": "security", "section": "Identity & Trust"},
    "identity-panel":     {"panel": "workers-ollama", "pane": "provision",    "sub": "identity", "section": "Identity & Trust"},
    "provision-panel":    {"panel": "workers-ollama", "pane": "software",     "sub": "",         "section": "Build"},
    "integrations":       {"panel": "workers-ollama", "pane": "integrations", "sub": "",         "section": "Integrations"},
}

_OFF = ("0", "false", "off", "no")


def setting_enabled(raw: Any) -> bool:
    """The stored switch. Absent means on: retiring is the default."""
    if raw is None:
        return True
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in _OFF


def annotate_panels(panels: Iterable[Mapping[str, Any]], enabled: bool) -> List[Dict[str, Any]]:
    """Copies of the registered panels, with `retired_into` on each retired
    top-level tab while the setting is on. The registry itself is not changed."""
    out = []
    for p in panels:
        q = dict(p)
        target = RETIRED_TABS.get(str(q.get("id") or ""))
        if enabled and target and q.get("mode") == "tab":
            q["retired_into"] = dict(target)
        out.append(q)
    return out
