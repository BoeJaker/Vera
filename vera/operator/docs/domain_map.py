"""domain_map.py — the documentation domains.

Each domain maps a ``documentation/NN-slug.md`` file to the **capability groups**
and **UI panels** that belong to it. Panels are matched to a domain at runtime
from the live ``/ui/panels`` registry using ``panel_ids`` (exact) and ``match``
(substring, tested against panel id + label + tags), so the map does not break
when a panel id changes.

``seed`` names a fixture in :mod:`vera.operator.missions.seeds` that populates
representative data before the panel is shot (``None`` = capture as-loaded).

Pure data + small helpers — trivially unit-testable.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# One dict per documentation file. Order == doc number.
DOMAINS: List[Dict[str, Any]] = [
    {"slug": "capability-framework", "doc": "01-capability-framework.md",
     "title": "Capability Framework", "cap_prefixes": ["mcp", "obs"],
     "panel_ids": ["cap-hub", "cap-list", "cap-search", "mcp-servers"],
     "match": ["cap", "mcp"], "seed": None},
    {"slug": "harness-ui", "doc": "02-harness-ui.md", "title": "Harness UI",
     "cap_prefixes": ["ui"], "panel_ids": ["cap-hub"],
     "match": ["dashboard", "harness"], "seed": None},
    {"slug": "dag-engine", "doc": "03-dag-engine.md", "title": "DAG & Loop Engine",
     "cap_prefixes": ["dag", "loops"], "panel_ids": ["dag-workshop"],
     "match": ["dag", "loop"], "seed": "dag"},
    {"slug": "ollama-cluster", "doc": "04-ollama-cluster.md", "title": "Ollama Cluster",
     "cap_prefixes": ["ollama"], "panel_ids": ["workers-ollama", "model-routing"],
     "match": ["ollama", "routing"], "seed": None},
    {"slug": "memory-graph", "doc": "05-memory-graph.md", "title": "Memory Graph",
     "cap_prefixes": ["memory"], "panel_ids": ["memory-graph"],
     "match": ["memory"], "seed": "memory"},
    {"slug": "data-fabric", "doc": "06-data-fabric.md", "title": "Data Fabric",
     "cap_prefixes": ["fabric"], "panel_ids": [],
     "match": ["fabric", "dataset", "vector browser"], "seed": "fabric"},
    {"slug": "research", "doc": "07-research.md", "title": "Research",
     "cap_prefixes": ["research"], "panel_ids": ["research-panel", "notebook-panel", "nlp-panel"],
     "match": ["research", "notebook", "nlp"], "seed": None},
    {"slug": "ide", "doc": "08-ide.md", "title": "IDE & Remote",
     "cap_prefixes": ["ide"], "panel_ids": ["workspaces", "remote-connections"],
     "match": ["ide", "vscode", "workspace", "remote"], "seed": None},
    {"slug": "galaxy-graph", "doc": "09-galaxy-graph.md", "title": "Galaxy Graph",
     "cap_prefixes": [], "panel_ids": [], "match": ["galaxy"], "seed": None},
    {"slug": "configuration", "doc": "10-configuration.md", "title": "Configuration",
     "cap_prefixes": ["config"], "panel_ids": [], "match": ["config"], "seed": None},
    {"slug": "worldview", "doc": "11-worldview.md", "title": "Worldview",
     "cap_prefixes": ["worldview"], "panel_ids": ["worldview"],
     "match": ["worldview"], "seed": None},
    {"slug": "execution", "doc": "12-execution.md", "title": "Execution",
     "cap_prefixes": ["exec"], "panel_ids": [], "match": ["exec", "execution"], "seed": None},
    {"slug": "docker", "doc": "13-docker.md", "title": "Docker",
     "cap_prefixes": ["docker"], "panel_ids": [], "match": ["docker"], "seed": None},
    {"slug": "mesh", "doc": "14-mesh.md", "title": "Mesh Manager",
     "cap_prefixes": ["mesh"], "panel_ids": ["mesh-panel"], "match": ["mesh"], "seed": None},
    {"slug": "markets", "doc": "15-markets.md", "title": "Markets & Quant Studio",
     "cap_prefixes": ["markets", "mkt", "backtest"], "panel_ids": ["markets-studio"],
     "match": ["markets", "quant"], "seed": "markets"},
    {"slug": "machine-learning", "doc": "16-machine-learning.md", "title": "Machine Learning",
     "cap_prefixes": ["ml"], "panel_ids": [], "match": ["machine", "training"], "seed": None},
    {"slug": "dream", "doc": "17-dream.md", "title": "Dream",
     "cap_prefixes": ["dream"], "panel_ids": ["dream"], "match": ["dream"], "seed": "dream"},
    {"slug": "skills-ontologies", "doc": "18-skills-ontologies.md", "title": "Skills & Ontologies",
     "cap_prefixes": ["skills", "ontology"],
     "panel_ids": ["skills-editor", "ontologies-browser", "agents-skills-ontologies"],
     "match": ["skill", "ontolog"], "seed": None},
    {"slug": "agents-chat", "doc": "19-agents-chat.md", "title": "Agents & Chat",
     "cap_prefixes": ["agents", "chat"], "panel_ids": ["chat2", "agents-editor"],
     "match": ["chat", "agent"], "seed": "chat"},
    {"slug": "flow-builder", "doc": "20-flow-builder.md", "title": "Flow Builder",
     "cap_prefixes": ["flow"], "panel_ids": [], "match": ["flow"], "seed": None},
    {"slug": "vllm", "doc": "21-vllm.md", "title": "vLLM",
     "cap_prefixes": ["vllm"], "panel_ids": ["vllm-panel"], "match": ["vllm"], "seed": None},
    {"slug": "workers-jobs-syslog", "doc": "22-workers-jobs-syslog.md", "title": "Workers, Jobs & Syslog",
     "cap_prefixes": ["workers"], "panel_ids": ["workers-ollama", "live-event-stream", "system-log", "job-stream"],
     "match": ["worker", "job", "syslog", "event"], "seed": None},
    {"slug": "integrations", "doc": "23-integrations.md", "title": "Integrations (Comms)",
     "cap_prefixes": ["accounts", "calendar", "email", "telegram"],
     "panel_ids": ["accounts-panel", "comms-panel", "calendar-panel", "telegram-panel"],
     "match": ["account", "comms", "calendar", "telegram", "email"], "seed": None},
    {"slug": "web-browser", "doc": "24-web-browser.md", "title": "Web Browser",
     "cap_prefixes": ["web"], "panel_ids": ["web_api"], "match": ["web", "browser"], "seed": None},
    {"slug": "vector-browser", "doc": "25-vector-browser.md", "title": "Vector Browser",
     "cap_prefixes": [], "panel_ids": [], "match": ["vector"], "seed": "fabric"},
    {"slug": "ui-builder", "doc": "26-ui-builder.md", "title": "UI Builder",
     "cap_prefixes": ["ui"], "panel_ids": ["ui-builder"], "match": ["ui-builder", "builder"], "seed": None},
    {"slug": "openclaw", "doc": "27-openclaw.md", "title": "OpenClaw",
     "cap_prefixes": ["openclaw"], "panel_ids": [], "match": ["openclaw", "claw"], "seed": None},
    {"slug": "render", "doc": "28-render.md", "title": "Render & Media",
     "cap_prefixes": ["render", "media"], "panel_ids": ["gallery", "vera-mermaid"],
     "match": ["render", "gallery", "mermaid", "media", "artifact"], "seed": None},
    {"slug": "security", "doc": "29-security.md", "title": "Security",
     "cap_prefixes": ["security", "netsec", "secrets"], "panel_ids": [],
     "match": ["security", "netsec", "secret"], "seed": None},
    {"slug": "onnx", "doc": "30-onnx.md", "title": "ONNX",
     "cap_prefixes": [], "panel_ids": [], "match": ["onnx"], "seed": None},
    {"slug": "podcast", "doc": "31-podcast.md", "title": "Podcast",
     "cap_prefixes": ["podcast"], "panel_ids": [], "match": ["podcast"], "seed": None},
    {"slug": "cluster-encryption", "doc": "32-cluster-encryption.md", "title": "Cluster Encryption",
     "cap_prefixes": ["netsec"], "panel_ids": [], "match": ["encryption", "wireguard"], "seed": None},
    {"slug": "evolve", "doc": "33-evolve.md", "title": "Loop Lab (Evolve)",
     "cap_prefixes": ["evolve"], "panel_ids": ["evolve"], "match": ["evolve", "loop lab"], "seed": None},
    {"slug": "operator", "doc": "34-operator.md", "title": "Operator",
     "cap_prefixes": ["operator", "docs"], "panel_ids": ["operator-studio"],
     "match": ["operator"], "seed": None},
]

_BY_SLUG = {d["slug"]: d for d in DOMAINS}


def all_slugs() -> List[str]:
    return [d["slug"] for d in DOMAINS]


def by_slug(slug: str) -> Optional[Dict[str, Any]]:
    return _BY_SLUG.get(slug)


def resolve_slugs(names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Return domain dicts for the given slugs (or all if empty/None). Unknown
    names are ignored."""
    if not names:
        return list(DOMAINS)
    want = {n.strip() for n in names if n and n.strip()}
    return [d for d in DOMAINS if d["slug"] in want]


def panel_matches_domain(panel: Dict[str, Any], domain: Dict[str, Any]) -> bool:
    """True if a live ``/ui/panels`` entry belongs to ``domain``."""
    pid = str(panel.get("id") or "").lower()
    if pid and pid in {p.lower() for p in domain.get("panel_ids", [])}:
        return True
    hay = " ".join([pid, str(panel.get("label") or "").lower(),
                    " ".join(str(t).lower() for t in (panel.get("tags") or []))])
    return any(m.lower() in hay for m in domain.get("match", []))


def domain_for_panel(panel: Dict[str, Any]) -> Optional[str]:
    """First domain slug that claims this panel (exact id wins over match)."""
    pid = str(panel.get("id") or "").lower()
    for d in DOMAINS:
        if pid and pid in {p.lower() for p in d.get("panel_ids", [])}:
            return d["slug"]
    for d in DOMAINS:
        if panel_matches_domain(panel, d):
            return d["slug"]
    return None
