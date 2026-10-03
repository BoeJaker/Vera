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
     "match": ["dag", "loop"], "seed": "dag",
     "capture_states": {"dag-workshop": [
         {"name": "dag", "label": "A DAG from the library",
          "caption": "A stored DAG opened from the library: its steps, initial state and portable IR inspection.",
          "steps": [{"click": ':text("test if boejaker.com is up")', "then_ms": 3000}],
          "ready_selector": ':text("STEPS")'},
     ],
         # Its counters read for well over 30s on a busy prod.
         "automations": [{"ready_timeout_ms": 75000}]}},
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
     "match": ["ide", "vscode", "workspace", "remote"], "seed": None,
     "capture_states": {"ide-panel": [
         # VS Code in action: the central code-server with a project and a file
         # open. The login password is fetched from the target at capture time.
         {"name": "editor", "label": "VS Code with a project open",
          "caption": "The central code-server with a project open and a source file in the editor.",
          "capture_path": "/vscode/central/?folder=/home/coder/projects/vera",
          "steps": [{"code_server_login": "central", "timeout_ms": 30000},
                    {"wait_for": ".monaco-workbench", "timeout_ms": 60000, "then_ms": 5000},
                    {"press": "Control+P", "then_ms": 1500},
                    {"fill": ".quick-input-box input", "text": "operator_loop.py", "then_ms": 1500},
                    {"press": "Enter", "then_ms": 4000}],
          "ready_selector": ".monaco-editor", "settle_ms": 2000},
         {"name": "panel", "label": "The IDE panel",
          "caption": "The IDE panel: the central VS Code, the classic workbench, remotes and the dispatch queue.",
          "settle_ms": 12000},
     ]}},
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
     "cap_prefixes": ["exec"], "panel_ids": [], "match": ["exec", "execution"], "seed": None,
     "capture_states": {
         "exec-panel": [
             {"name": "run", "label": "A command run on the host",
              "caption": "A command run through the exec panel, with its captured output.",
              "steps": [{"click": ':text("Run (capture)")', "then_ms": 5000}]},
         ],
         "workspaces": [
             {"name": "postgres", "label": "A workspace bound to a container",
              "caption": "A workspace bound to a container: its terminal, files and metrics, with memory and chat widgets beside them.",
              "steps": [{"click": ':text-is("Postgres")', "then_ms": 6000}]},
         ]}},
    {"slug": "docker", "doc": "13-docker.md", "title": "Docker",
     "cap_prefixes": ["docker"], "panel_ids": [], "match": ["docker"], "seed": None},
    {"slug": "mesh", "doc": "14-mesh.md", "title": "Mesh Manager",
     "cap_prefixes": ["mesh"], "panel_ids": ["mesh"], "match": ["mesh"], "seed": None},
    {"slug": "markets", "doc": "15-markets.md", "title": "Markets & Quant Studio",
     "cap_prefixes": ["markets", "mkt", "backtest"], "panel_ids": ["markets-studio"],
     "match": ["markets", "quant"], "seed": "markets",
     # The chart draws its bars well after the page reports content.
     "capture_states": {"markets": [{"settle_ms": 15000}]}},
    {"slug": "machine-learning", "doc": "16-machine-learning.md", "title": "Machine Learning",
     "cap_prefixes": ["ml"], "panel_ids": [], "match": ["machine", "training"], "seed": None},
    {"slug": "dream", "doc": "17-dream.md", "title": "Dream",
     "cap_prefixes": ["dream"], "panel_ids": ["dream"], "match": ["dream"], "seed": "dream"},
    {"slug": "skills-ontologies", "doc": "18-skills-ontologies.md", "title": "Skills & Ontologies",
     "cap_prefixes": ["skills", "ontology"],
     "panel_ids": ["skills-editor", "ontologies-browser", "agents-skills-ontologies"],
     "match": ["skill", "ontolog"], "seed": None,
     "capture_states": {"agents-skills-ontologies": [
         {"name": "agent", "label": "An agent's identity",
          "caption": "An agent opened in the Agents editor: its identity, model, personality, tools, skills and ontologies.",
          # The editor is an iframe of this panel; steps cannot reach into frames.
          "capture_path": "/ui/panels/agents-panel",
          "steps": [{"click": ':text("agentic-planner")', "then_ms": 4000}]},
     ]}},
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
     "cap_prefixes": ["ui"], "panel_ids": ["ui-builder"], "match": ["ui-builder", "builder"], "seed": None,
     "capture_states": {"ui-builder": [{"ready_timeout_ms": 75000}]}},
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
     "match": ["foundry", "provision", "proxmox", "network"], "seed": None,
     # The estate overview checks every machine before it settles.
     "capture_states": {"workers-ollama": [{"ready_timeout_ms": 90000}]}},
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
     "match": ["image", "media", "character", "sprite", "babblefish"], "seed": None,
     "capture_states": {"image-studio": [
         {"name": "gallery", "label": "Image Studio gallery",
          "caption": "The image archive: every stored generation, durable on disk.",
          "steps": [{"click": ':text-is("Gallery")', "then_ms": 6000}]},
     ]}},
    {"slug": "activity-boards", "doc": "39-activity-boards.md",
     "title": "Activity & Boards", "cap_prefixes": ["activity", "board", "capacity"],
     "panel_ids": ["activity", "activity-timeline", "activity-track"],
     "match": ["activity", "board"], "seed": None},
    {"slug": "pwa", "doc": "47-pwa.md", "title": "Install / PWA",
     "cap_prefixes": ["pwa"], "panel_ids": [], "match": [], "seed": None},
    {"slug": "chat-ui", "doc": "50-chat-ui.md", "title": "Chat UI",
     "cap_prefixes": ["chat", "canvas"], "panel_ids": [], "match": [], "seed": None,
     # chat2 wraps /chat_panel in an iframe; steps cannot reach into a frame, so
     # every state shoots the chat page itself with one rail menu open.
     "capture_states": {
         "chat2": [
             {"name": "sessions", "label": "Sessions menu",
              "caption": "Sessions: past conversations, newest first, with New, Refresh and Title all.",
              "capture_path": "/chat_panel",
              "steps": [{"wait_ms": 6000}, {"click": '.lhm-rail [data-menu="sessions"]', "then_ms": 5000}]},
             {"name": "context", "label": "Context menu",
              "caption": "Context: what goes into the prompt, by source, with the context plot and the memory graph.",
              "capture_path": "/chat_panel",
              "steps": [{"wait_ms": 6000}, {"click": '.lhm-rail [data-menu="context"]', "then_ms": 5000}]},
             {"name": "activity", "label": "Activity menu",
              "caption": "Activity: what is running, the call DAG and the monitor stream.",
              "capture_path": "/chat_panel",
              "steps": [{"wait_ms": 6000}, {"click": '.lhm-rail [data-menu="activity"]', "then_ms": 5000}]},
             {"name": "loop", "label": "Loop menu",
              "caption": "Loop: the current loop, its activity graph, loop runs and goals.",
              "capture_path": "/chat_panel",
              "steps": [{"wait_ms": 6000}, {"click": '.lhm-rail [data-menu="loop"]', "then_ms": 5000}],
              # Its graph says "Waiting for an agentic run" until a loop runs.
              "ready_selector": '.lhm-rail [data-menu="loop"]'},
             {"name": "workspace", "label": "Workspace menu",
              "caption": "Workspace: every registered panel, ready to open beside the chat.",
              "capture_path": "/chat_panel",
              "steps": [{"wait_ms": 6000}, {"click": '.lhm-rail [data-menu="workspace"]', "then_ms": 5000}]},
             {"name": "ops", "label": "Ops glance",
              "caption": "Ops glance: the capabilities available to the agent, by family.",
              "capture_path": "/chat_panel",
              "steps": [{"wait_ms": 6000}, {"click": '.lhm-rail [data-menu="ops"]', "then_ms": 5000}]},
             {"name": "slash", "label": "Slash commands",
              "caption": "Typing / in the composer lists the slash commands.",
              "capture_path": "/chat_panel",
              "steps": [{"wait_ms": 6000},
                        {"click": 'textarea[placeholder^="Type a message"]'},
                        {"press": "/", "then_ms": 2500}]},
         ],
         "canvas": [
             {"name": "open", "label": "A canvas",
              "caption": "A live canvas: the list of canvases and one opened with its blocks.",
              "steps": [{"wait_ms": 4000}, {"click": ':text("Session canvas")', "then_ms": 5000}]},
         ]}},
    # The README and the lead image of several docs: whole routes, not panels,
    # so this domain claims no live panel and has no doc of its own.
    {"slug": "overview", "doc": "", "title": "Overview", "cap_prefixes": [],
     "panel_ids": [], "match": [], "seed": None,
     "pages": [
         # The README's first picture: a real turn, the agent driving a panel
         # open beside the chat, and the Context menu open on the left. It calls
         # the model, so it runs only when named (panels=["chat"]).
         {"name": "chat", "path": "/chat_panel", "label": "Chat", "on_request": True,
          "steps": [
              {"chat_send": "Call ollama.instances and give me a short table of each "
                            "inference node: its status, latency and the models it has "
                            "loaded. Use only what the call returns.",
               "timeout_ms": 900000},
              {"click": '.lhm-rail [data-menu="context"]', "then_ms": 6000},
          ],
          # A panel the agent opened beside the chat can keep a "Loading…" line
          # in a corner; the shot is ready once the Context menu is open.
          "ready_selector": ':text("IN THIS PROMPT")',
          "ready_timeout_ms": 60000, "settle_ms": 4000},
         # The shell fills ~70 widgets after load; a short settle caught
         # "never updated" and empty "no data" tiles.
         # The Inference layout from the dashboard's own menu: host CPU,
         # capacity gauges, fleet, census progress and live events read better
         # than the default lists.
         {"name": "dashboard", "path": "/", "label": "Main dashboard",
          # The layout menu ignores a click until the default layout has
          # finished loading.
          "steps": [{"wait_for": ':text("ROUTING")', "timeout_ms": 45000, "then_ms": 15000},
                    {"click": ':text("Inference · models")', "then_ms": 20000}],
          # Only this layout has this widget: a missed switch fails instead of
          # shooting the default layout.
          "ready_selector": ':text("HOST CPU")', "ready_timeout_ms": 60000,
          # Tiles show sample data under "reading…" until their read returns.
          "reject_texts": ["read failed", "reading…"],
          "settle_ms": 5000},
         {"name": "cap-hub", "path": "/cap_hub/panel", "label": "Capability Hub"},
         {"name": "evolve", "path": "/evolve/panel", "label": "Loop Lab",
          "ready_timeout_ms": 60000},
         {"name": "fabric-panel", "path": "/fabric/panel", "label": "Data Fabric"},
         {"name": "operator-studio", "path": "/operator/panel", "label": "Operator Studio"},
         {"name": "perf-monitor", "path": "/perf/panel", "label": "Performance Monitor"},
     ]},
]

# The ONLY assignment of live panels to docs. Reviewed panel by panel against
# what each one shows (2026-10-03). Left out on purpose: godseye (a 3D intel
# globe with no doc of its own), pokedex_app (a demo app), and panels whose only
# state on prod is empty or broken - a doc should not lead with "Waiting for
# events" or "none registered": cap-search, mcp-servers, agent-loop-output,
# loop-graph, node-activity, fabric-stats, nlp-panel, ide-workspace-changes,
# skills-editor, ontologies-browser, skills-list, job-stream, live-event-stream,
# accounts-panel, email-panel, web_api, gallery, vera-mermaid, sprite-companion,
# stable-diffusion, kokoro-tts, whisper-stt, activity-track, chat-with-data, and
# pwa (a capture browser on another hostname only ever shows its TLS error),
# research-panel, notebook-panel, ml-lab and dream-panel (empty on prod today);
# automations and ui-builder (never settle on prod within 75 s).
# telegram-panel shows the admin chat id
# and the bot's identity, so it is never captured for the docs.
PANEL_DOCS: Dict[str, str] = {
    "perf-monitor": "performance",
    # capability framework
    "cap-hub": "capability-framework", "cap-list": "capability-framework",
    "cap-ontology": "capability-framework", "mcp-catalog-panel": "capability-framework",
    # harness: the control plane that drives the UI
    "driven": "harness-ui",
    # DAG + loop engine, and the one page for everything that runs on its own
    "dag-workshop": "dag-engine", "agent-loop-config": "dag-engine",
    # inference cluster
    "models": "ollama-cluster", "node-models": "ollama-cluster",
    "specialist-models": "ollama-cluster",
    "memory-graph": "memory-graph",
    "fabric-panel": "data-fabric",
    # the IDE and nothing else
    "ide-panel": "ide", "ide-remote-panel": "ide",
    "ide-claude-dispatch": "ide", "ide-inspect-panel": "ide",
    "memory-galaxy-panel": "galaxy-graph",
    "platform-config": "configuration",
    "worldview": "worldview",
    # execution surfaces: one-shot commands and long-lived bound workspaces
    "exec-panel": "execution", "workspaces": "execution",
    "mesh": "mesh",
    "markets": "markets",
    "project-panel": "dream",
    "agents-skills-ontologies": "skills-ontologies", "agent-registry": "skills-ontologies",
    "flow-builder": "flow-builder",
    "system-log": "workers-jobs-syslog", "node-workers": "workers-jobs-syslog",
    "comms-panel": "integrations", "calendar-panel": "integrations",
    "integrations": "integrations", "n8n-panel": "integrations",
    "ha-panel": "integrations", "syscomms": "integrations",
    "vector-browser-panel": "vector-browser",
    "widget-gallery": "ui-builder", "widget-registry": "ui-builder",
    "evolve": "evolve",
    "operator-studio": "operator",
    # the estate: machines, their map and the stack monitor
    "foundry": "infrastructure-provisioning", "workers-ollama": "infrastructure-provisioning",
    "interaction-map": "infrastructure-provisioning",
    "system-monitor": "infrastructure-provisioning",
    "agentbridge-catalog-panel": "agent-runtimes-providers",
    "business": "business-commerce",
    "image-studio": "media-characters", "character-studio": "media-characters",
    "babblefish": "media-characters", "thermal-printer": "media-characters",
    "activity": "activity-boards", "activity-timeline": "activity-boards",
    "chat2": "chat-ui", "canvas": "chat-ui",
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
