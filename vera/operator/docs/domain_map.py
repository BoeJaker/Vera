"""domain_map.py — the documentation domains.

Each domain maps a ``documentation/NN-slug.md`` file to the **capability groups**
and **UI panels** that belong to it. Which doc a live panel belongs to is decided
by ONE explicit table, ``PANEL_DOCS`` - never by substring matching. Matching on
id/label/tags put the Estate Map and the Workspaces page in the IDE doc, Markets
/ Email / Home Assistant / Godseye under Configuration, Dream under the
Capability Framework (2026-10-03). A panel missing from the table is reported
as unmapped by the mission rather than guessed into a doc.

``seed`` names a fixture in :mod:`vera.operator.missions.seeds` that populates
representative data before the panel is shot (``None`` = capture as-loaded).

Pure data + small helpers — trivially unit-testable.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# One dict per documentation file. Order == doc number.
DOMAINS: List[Dict[str, Any]] = [
    {"slug": "performance", "doc": "00-performance-and-sizing.md",
     "title": "Performance & Sizing", "cap_prefixes": ["perf"], "panel_ids": [],
     "match": [], "seed": None},
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
     "match": ["memory"], "seed": "memory",
     "capture_states": {"memory-graph": [
         {"name": "recent", "label": "Recent memory activity",
          "caption": "A populated recent-activity graph with memory records, sessions, and their relationships.",
          "click": "#btn-recent", "ready_selector": "#cv",
          "ready_text": "#stat-bar", "ready_count": "#stat-bar", "ready_painted": "#cv",
          "settle_ms": 3200,
          # prod's recent graph (~900 nodes) took 49s to arrive
          "ready_timeout_ms": 75000},
     ]}},
    {"slug": "data-fabric", "doc": "06-data-fabric.md", "title": "Data Fabric",
     "cap_prefixes": ["fabric"], "panel_ids": ["fabric-panel"],
     "match": ["fabric", "dataset", "vector browser"], "seed": "fabric",
     "capture_states": {"fabric-panel": [
         {"name": "graph", "label": "Data Fabric graph",
          "caption": "The populated structural graph connects datasets, sources, agents, skills, and ontologies.",
          "click": "#fnav-graph", "ready_selector": "#fgGraphHost canvas",
          "ready_text": "#fgGraphMeta", "ready_count": "#fgGraphMeta",
          "settle_ms": 3200, "ready_timeout_ms": 60000},
         {"name": "sources", "label": "Data Fabric sources",
          "caption": "Source management, acquisition status, and dataset routing.",
          "click": "#fnav-sources", "ready_selector": "#fsec-sources",
          "ready_text": "#fabSrcFilterMeta", "settle_ms": 2200},
         {"name": "stats", "label": "Data Fabric statistics",
          "caption": "Backend health and storage statistics across the polyglot fabric.",
          "click": "#fnav-stats", "ready_selector": "#fsec-stats",
          "ready_text": "#fabStatsGrid", "settle_ms": 2400},
     ]}},
    {"slug": "research", "doc": "07-research.md", "title": "Research",
     "cap_prefixes": ["research"], "panel_ids": ["research-panel", "notebook-panel", "nlp-panel"],
     "match": ["research", "notebook", "nlp"], "seed": None},
    {"slug": "ide", "doc": "08-ide.md", "title": "IDE & Remote",
     "cap_prefixes": ["ide"], "panel_ids": ["ide-panel", "ide-remote-panel", "workspaces", "remote-connections"],
     "match": ["ide", "vscode", "workspace", "remote"], "seed": None},
    {"slug": "galaxy-graph", "doc": "09-galaxy-graph.md", "title": "Galaxy Graph",
     "cap_prefixes": [], "panel_ids": ["memory-galaxy-panel"],
     "match": ["galaxy"], "seed": "memory",
     "capture_states": {"memory-galaxy-panel": [
         {"name": "galaxy", "label": "Memory galaxy",
          "caption": "The navigable memory galaxy renders records and relationships as a spatial graph.",
          "ready_selector": "body > canvas", "ready_text": "#ticker-text",
          "settle_ms": 4200},
         {"name": "physics", "label": "Memory galaxy 2D physics view",
          "caption": "The alternate physics view makes clusters and relationship structure directly inspectable.",
          "click_after_count": "#s-nodes",
          "click": "#view2d-btn", "ready_selector": "#view2d-canvas",
          "ready_text": "#view2d-label", "ready_count": "#view2d-label",
          "settle_ms": 2600},
     ]}},
    {"slug": "configuration", "doc": "10-configuration.md", "title": "Configuration",
     "cap_prefixes": ["config"], "panel_ids": [], "match": ["config"], "seed": None},
    {"slug": "worldview", "doc": "11-worldview.md", "title": "Worldview",
     "cap_prefixes": ["worldview"], "panel_ids": ["worldview"],
     "match": ["worldview"], "seed": None,
     "capture_states": {"worldview": [
         {"name": "latent-map", "label": "WorldView latent map",
          "caption": "The latent map exposes WorldView concepts, projection controls, and model state.",
          "capture_path": "/ui/panels/worldview-panel",
          # the latent map only loads when Render is pressed
          "click": "button[onclick='wvLoadScatter()']",
          "ready_selector": "#wvScatter", "ready_text": "#wvTabDescText",
          "ready_count": "#wvScatterInfo",
          "settle_ms": 2800, "ready_timeout_ms": 90000},
     ]}},
    {"slug": "execution", "doc": "12-execution.md", "title": "Execution",
     "cap_prefixes": ["exec"], "panel_ids": [], "match": ["exec", "execution"], "seed": None},
    {"slug": "docker", "doc": "13-docker.md", "title": "Docker",
     "cap_prefixes": ["docker"], "panel_ids": [], "match": ["docker"], "seed": None},
    {"slug": "mesh", "doc": "14-mesh.md", "title": "Mesh Manager",
     "cap_prefixes": ["mesh"], "panel_ids": ["mesh"], "match": ["mesh"], "seed": None},
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
     "cap_prefixes": [], "panel_ids": ["vector-browser-panel"],
     "match": ["vector"], "seed": "fabric",
     "capture_states": {"vector-browser-panel": [
         {"name": "overview", "label": "Vector storage overview",
          "caption": "The overview reports vector counts, dimensions, and alignment across configured stores.",
          "ready_selector": "#overviewStats .stat-box", "ready_text": "#overviewAlignment",
          "settle_ms": 2200},
         {"name": "chroma", "label": "Chroma vector records",
          "caption": "The record browser shows dataset ownership and embedding health without exposing full vectors.",
          "click": ".tab[data-tab='chroma']", "ready_selector": "#panel-chroma.active",
          "ready_text": "#chromaPageInfo", "settle_ms": 2200},
     ]}},
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
     "cap_prefixes": ["evolve"], "panel_ids": ["evolve"], "match": ["evolve", "loop lab"], "seed": None,
     # Loop Lab's widgets read ~10-30s before their placeholders clear.
     "capture_states": {"evolve": [{"ready_timeout_ms": 60000}]}},
    {"slug": "operator", "doc": "34-operator.md", "title": "Operator",
     "cap_prefixes": ["operator", "docs"], "panel_ids": ["operator-studio"],
     "match": ["operator"], "seed": None},
    {"slug": "infrastructure-provisioning", "doc": "35-infrastructure-provisioning.md",
     "title": "Infrastructure & Provisioning",
     "cap_prefixes": ["foundry", "build", "provision", "proxmox", "network", "netmon", "remote"],
     "panel_ids": ["foundry"],
     "match": ["foundry", "provision", "proxmox", "network"], "seed": None},
    {"slug": "agent-runtimes-providers", "doc": "36-agent-runtimes-providers.md",
     "title": "Agent Runtimes & Providers",
     "cap_prefixes": ["agentbridge", "providers", "catalog", "smolagents", "langgraph", "pydanticai"],
     "panel_ids": ["agentbridge-catalog-panel"],
     "match": ["agentbridge", "provider", "catalog", "langgraph"], "seed": None},
    {"slug": "business-commerce", "doc": "37-business-commerce.md",
     "title": "Business & Commerce", "cap_prefixes": ["business", "commerce", "print"],
     "panel_ids": ["business"], "match": ["business", "commerce", "store"], "seed": None},
    {"slug": "media-characters", "doc": "38-media-characters.md",
     "title": "Media & Characters",
     "cap_prefixes": ["image", "images", "media", "character", "spritegen", "stt", "tts", "babblefish"],
     "panel_ids": ["image-studio", "character-studio", "sprite-companion", "babblefish"],
     "match": ["image", "media", "character", "sprite", "babblefish"], "seed": None},
    {"slug": "activity-boards", "doc": "39-activity-boards.md",
     "title": "Activity & Boards", "cap_prefixes": ["activity", "board", "capacity"],
     "panel_ids": ["activity", "activity-timeline", "activity-track"],
     "match": ["activity", "board"], "seed": None},
    {"slug": "pwa", "doc": "47-pwa.md", "title": "Install / PWA",
     "cap_prefixes": ["pwa"], "panel_ids": [], "match": [], "seed": None},
    {"slug": "chat-ui", "doc": "50-chat-ui.md", "title": "Chat UI",
     "cap_prefixes": ["chat", "canvas"], "panel_ids": [], "match": [], "seed": None},
    # The README and the lead image of several docs: whole routes, not panels,
    # so this domain claims no live panel and has no doc of its own.
    {"slug": "overview", "doc": "", "title": "Overview", "cap_prefixes": [],
     "panel_ids": [], "match": [], "seed": None,
     "pages": [
         # The shell fills ~70 widgets after load; a short settle caught
         # "never updated" and empty "no data" tiles.
         {"name": "dashboard", "path": "/", "label": "Main dashboard", "settle_ms": 15000},
         {"name": "cap-hub", "path": "/cap_hub/panel", "label": "Capability Hub"},
         {"name": "evolve", "path": "/evolve/panel", "label": "Loop Lab",
          "ready_timeout_ms": 60000},
         {"name": "fabric-panel", "path": "/fabric/panel", "label": "Data Fabric"},
         {"name": "loop-graph", "path": "/ui/panel/window?id=loop-graph",
          "label": "Agent loop graph"},
         {"name": "operator-studio", "path": "/operator/panel", "label": "Operator Studio"},
         {"name": "perf-monitor", "path": "/perf/panel", "label": "Performance Monitor"},
     ]},
]

# The ONLY assignment of live panels to docs. Reviewed panel by panel against
# what each one shows (2026-10-03). Left out on purpose: godseye (a 3D intel
# globe with no doc of its own) and pokedex_app (a demo app).
PANEL_DOCS: Dict[str, str] = {
    "perf-monitor": "performance",
    # capability framework
    "cap-hub": "capability-framework", "cap-list": "capability-framework",
    "cap-ontology": "capability-framework", "cap-search": "capability-framework",
    "mcp-servers": "capability-framework", "mcp-catalog-panel": "capability-framework",
    # harness: the control plane that drives the UI
    "driven": "harness-ui",
    # DAG + loop engine, and the one page for everything that runs on its own
    "dag-workshop": "dag-engine", "agent-loop-output": "dag-engine",
    "agent-loop-config": "dag-engine", "loop-graph": "dag-engine",
    "automations": "dag-engine",
    # inference cluster
    "models": "ollama-cluster", "node-models": "ollama-cluster",
    "node-activity": "ollama-cluster", "specialist-models": "ollama-cluster",
    "memory-graph": "memory-graph",
    "fabric-panel": "data-fabric", "fabric-stats": "data-fabric",
    "research-panel": "research", "notebook-panel": "research", "nlp-panel": "research",
    # the IDE and nothing else
    "ide-panel": "ide", "ide-remote-panel": "ide", "ide-workspace-changes": "ide",
    "ide-claude-dispatch": "ide", "ide-inspect-panel": "ide",
    "memory-galaxy-panel": "galaxy-graph",
    "platform-config": "configuration",
    "worldview": "worldview",
    # execution surfaces: one-shot commands and long-lived bound workspaces
    "exec-panel": "execution", "workspaces": "execution",
    "mesh": "mesh",
    "markets": "markets",
    "ml-lab": "machine-learning",
    "dream-panel": "dream", "project-panel": "dream",
    "skills-editor": "skills-ontologies", "ontologies-browser": "skills-ontologies",
    "agents-skills-ontologies": "skills-ontologies", "agent-registry": "skills-ontologies",
    "skills-list": "skills-ontologies",
    "flow-builder": "flow-builder",
    "job-stream": "workers-jobs-syslog", "live-event-stream": "workers-jobs-syslog",
    "system-log": "workers-jobs-syslog", "node-workers": "workers-jobs-syslog",
    "accounts-panel": "integrations", "comms-panel": "integrations",
    "calendar-panel": "integrations", "email-panel": "integrations",
    "telegram-panel": "integrations", "integrations": "integrations",
    "n8n-panel": "integrations", "ha-panel": "integrations", "syscomms": "integrations",
    "web_api": "web-browser",
    "vector-browser-panel": "vector-browser",
    "ui-builder": "ui-builder", "widget-gallery": "ui-builder", "widget-registry": "ui-builder",
    "gallery": "render", "vera-mermaid": "render",
    "evolve": "evolve",
    "operator-studio": "operator",
    # the estate: machines, their map and the stack monitor
    "foundry": "infrastructure-provisioning", "workers-ollama": "infrastructure-provisioning",
    "interaction-map": "infrastructure-provisioning",
    "system-monitor": "infrastructure-provisioning",
    "agentbridge-catalog-panel": "agent-runtimes-providers",
    "business": "business-commerce",
    "image-studio": "media-characters", "character-studio": "media-characters",
    "sprite-companion": "media-characters", "babblefish": "media-characters",
    "stable-diffusion": "media-characters", "thermal-printer": "media-characters",
    "kokoro-tts": "media-characters", "whisper-stt": "media-characters",
    "activity": "activity-boards", "activity-timeline": "activity-boards",
    "activity-track": "activity-boards",
    "pwa": "pwa",
    "chat2": "chat-ui", "chat-with-data": "chat-ui", "canvas": "chat-ui",
}

# panel_ids/match are derived from PANEL_DOCS so nothing else can claim a panel.
for _d in DOMAINS:
    _d["panel_ids"] = [pid for pid, slug in PANEL_DOCS.items() if slug == _d["slug"]]
    _d["match"] = []

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
    """The doc slug PANEL_DOCS assigns this panel, or None if it is unmapped."""
    return PANEL_DOCS.get(str(panel.get("id") or ""))
