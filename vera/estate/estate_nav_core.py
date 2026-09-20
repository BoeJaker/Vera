"""Which top-level tabs now open inside a broader tab, where each one opens,
and the one setting that brings them back.

Estate covers eight machine, network, identity and platform tabs; Capabilities covers
Cap Ontology and MCP Servers; Agents covers Agent Bridges; Image Studio covers
Companion.

Pure data plus two functions, so it tests without booting Vera
(tests/test_estate_nav_core.py). ui.panels annotates the registry with it, and
the shell hides a tab that carries `retired_into`, opening that pane of the
host tab instead (the host panel answers `vera:estate:open` with
`vera:estate:opened`). Routes and capabilities are untouched: turning the
setting off restores the old tab bar on the next page load.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping

RETIRE_SETTING_KEY = "vera:ui:retire_overlap_tabs"

# panel id -> the host panel's pane (and sub-tab) that now covers it
RETIRED_TABS: Dict[str, Dict[str, str]] = {
    # Estate
    "proxmox-panel":      {"panel": "workers-ollama", "pane": "proxmox",      "sub": "",         "section": "Machines"},
    "remote-connections": {"panel": "workers-ollama", "pane": "remote",       "sub": "",         "section": "Machines"},
    "netgraph-panel":     {"panel": "workers-ollama", "pane": "network",      "sub": "graph",    "section": "Network & Access"},
    "provisioning-panel": {"panel": "workers-ollama", "pane": "provision",    "sub": "security", "section": "Identity & Trust"},
    "identity-panel":     {"panel": "workers-ollama", "pane": "provision",    "sub": "identity", "section": "Identity & Trust"},
    "provision-panel":    {"panel": "workers-ollama", "pane": "software",     "sub": "",         "section": "Build"},
    "integrations":       {"panel": "workers-ollama", "pane": "integrations", "sub": "",         "section": "Integrations"},
    "platform-config":    {"panel": "workers-ollama", "pane": "platforms",    "sub": "",         "section": "Integrations"},
    # Capabilities
    "cap-ontology":       {"panel": "cap-hub", "pane": "ontology", "sub": "",        "section": "Capabilities"},
    "mcp-catalog-panel":  {"panel": "cap-hub", "pane": "mcp",      "sub": "catalog", "section": "Capabilities"},
    # Agents
    "agentbridge-catalog-panel": {"panel": "agents-skills-ontologies", "pane": "bridges", "sub": "", "section": "Agents"},
    # Image Studio
    "character-studio":   {"panel": "image-studio", "pane": "companion", "sub": "", "section": "Image Studio"},
}

_OFF = ("0", "false", "off", "no")


NETCTL_LABEL = "netctl"


def netctl_record(integrations: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """The Integrations record that fronts netctl's own pages: its label starts
    with 'netctl'. NWM-02, the node with the access tab, comes first."""
    rows = [dict(i) for i in integrations
            if str(i.get("label") or "").strip().lower().startswith(NETCTL_LABEL)]
    rows.sort(key=lambda i: ("nwm-02" not in str(i.get("label") or "").lower(), str(i.get("label") or "")))
    return rows[0] if rows else {}


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


# ── entities in deep links ───────────────────────────────────────────────────
# Every mechanism that moves a reader between panes carried only the pane:
# `?pane=&sub=`, `vera:estate:open {pane, sub}` and the injected-menu relay. So
# "open Machines focused on VM 145" could not be said, and the storage view
# listed guests with no way out of the list. An entity reference is
# `<kind>:<id>`; each kind names the pane (and sub-tab) that can focus it.

ENTITY_KINDS: Dict[str, Dict[str, str]] = {
    "guest":       {"pane": "machines",     "sub": "",         "noun": "machine"},
    "host":        {"pane": "machines",     "sub": "",         "noun": "machine"},
    "docker-host": {"pane": "docker",       "sub": "",         "noun": "Docker host"},
    "container":   {"pane": "docker",       "sub": "",         "noun": "container"},
    "pool":        {"pane": "storage",      "sub": "",         "noun": "pool"},
    "dataset":     {"pane": "storage",      "sub": "",         "noun": "dataset"},
    "integration": {"pane": "integrations", "sub": "",         "noun": "service"},
    "identity":    {"pane": "provision",    "sub": "identity", "noun": "directory host"},
    "mesh":        {"pane": "provision",    "sub": "mesh",     "noun": "mesh member"},
    "secret":      {"pane": "provision",    "sub": "secrets",  "noun": "secret"},
    "cert":        {"pane": "provision",    "sub": "certs",    "noun": "certificate"},
    "backup-job":  {"pane": "storage",      "sub": "estate",   "noun": "backup job"},
    "model":       {"pane": "ollama",       "sub": "",         "noun": "model"},
}


def parse_entity(ref: Any) -> Dict[str, str]:
    """`kind:id` -> {kind, id}, or {} when it names nothing this estate knows.
    The id may itself contain colons (a guest is `cluster:vmid`, a container
    `host/name`), so only the first colon divides."""
    s = str(ref or "").strip()
    if ":" not in s:
        return {}
    kind, ident = s.split(":", 1)
    kind, ident = kind.strip().lower(), ident.strip()
    if kind not in ENTITY_KINDS or not ident:
        return {}
    return {"kind": kind, "id": ident}


def entity_target(ref: Any) -> Dict[str, str]:
    """Where a reference opens: {kind, id, pane, sub, noun}, or {}."""
    ent = parse_entity(ref)
    if not ent:
        return {}
    return {**ent, **ENTITY_KINDS[ent["kind"]]}


def entity_ref(kind: str, ident: Any) -> str:
    return f"{kind}:{ident}" if kind in ENTITY_KINDS and str(ident or "").strip() else ""
