"""
mcp_catalog_capabilities.py — Vera MCP Server Catalog
=====================================================

A persisted registry of *MCP servers* (Model Context Protocol services), so the
common ecosystem servers Vera might talk to are pre-seeded as **blank records
ready for config** and the operator only has to drop in the credential that each
one needs.

Vera already has its own MCP surface (`/mcp/tools`, `/mcp/call`, `/ws/mcp` and
the peer-proxy `mcp.register_server` in capability_orchestration.py). That proxy
speaks *Vera's* HTTP MCP dialect and is for wiring one Vera to another. This
catalog is the complementary half: a directory of the wider MCP ecosystem
(GitHub, Slack, Postgres, Notion, Linear, …), each stored with the transport +
env/secret template it expects, so they can be configured once and reused.

Record shape (group `mcp.catalog.*`)
────────────────────────────────────
  id            stable slug (also the capability namespace when proxied)
  label         display name
  category      grouping for the UI ("Dev & Code", "Databases", …)
  icon          emoji
  description   one-liner
  docs_url      where to read more / get a token
  transport     "stdio" | "sse" | "http" | "vera_proxy"
  command,args  stdio launch (e.g. npx -y @modelcontextprotocol/server-github)
  url           endpoint for sse/http/vera_proxy transports
  env           {KEY: value}  — secret values sealed at rest
  headers       {Header: value} — secret values sealed at rest
  config_fields [{key,label,target,secret,required,placeholder,hint}] — what the
                UI should prompt for; `target` ∈ {env,header,url,arg}
  enabled       operator toggle
  status        "unconfigured" | "configured" | "connected" | "error"
  seeded        True if it came from the built-in catalog (vs user-added)

Secrets (env/header values flagged secret in `config_fields`) are sealed with the
shared Fernet helper (vera/security/secrets.py) and redacted to `••••` on output.

Capabilities
────────────
  mcp.catalog.list / get / upsert / delete / reseed
  mcp.catalog.connect  — for transport=vera_proxy, register the peer via the
                         existing orchestrator proxy; others are stored configs.
  mcp.catalog.panel.html  (serves /mcp/catalog/panel)

Redis layout
────────────
  vera:mcp:catalog   hash  id -> JSON   (secrets sealed)
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, now_iso, register_ui,
)
from Vera.vera.security import secrets as vsecrets

log = logging.getLogger("vera.mcp.catalog")

_HERE = Path(__file__).parent
_PANEL_HTML_PATH = _HERE / "mcp_catalog_panel.html"

KEY_CATALOG = "vera:mcp:catalog"

_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def _redis():
    return getattr(_orch, "REDIS", None)


# ─────────────────────────────────────────────────────────────────────────────
# BUILT-IN CATALOG  — blank records ready for config
# ─────────────────────────────────────────────────────────────────────────────
# Each entry is a template. `env`/`headers`/`url` start blank; the operator fills
# the `config_fields` in the panel. Package/command names are the canonical
# published ones so a record works the moment its token is supplied. Nothing here
# is spawned automatically — these are *configs*, ready to connect.

def _npx(pkg: str, *extra: str) -> Dict[str, Any]:
    return {"transport": "stdio", "command": "npx", "args": ["-y", pkg, *extra]}


def _env_field(key: str, label: str, hint: str = "", secret: bool = True,
               required: bool = True) -> Dict[str, Any]:
    return {"key": key, "label": label, "target": "env", "secret": secret,
            "required": required, "placeholder": "", "hint": hint}


CATALOG_SEED: List[Dict[str, Any]] = [
    # ── Reference servers (official modelcontextprotocol) ────────────────────
    {"id": "filesystem", "label": "Filesystem", "category": "Reference",
     "icon": "📁", "description": "Read/write files under allowed directories.",
     "docs_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
     **_npx("@modelcontextprotocol/server-filesystem"),
     "config_fields": [{"key": "root", "label": "Allowed directory", "target": "arg",
                        "secret": False, "required": True, "placeholder": "/data/projects",
                        "hint": "Passed as a CLI arg; add one per directory to expose."}]},
    {"id": "fetch", "label": "Fetch", "category": "Reference", "icon": "🌐",
     "description": "Fetch a URL and return it as markdown for the model.",
     "docs_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
     "transport": "stdio", "command": "uvx", "args": ["mcp-server-fetch"],
     "config_fields": []},
    {"id": "memory", "label": "Memory (KG)", "category": "Reference", "icon": "🧠",
     "description": "Persistent knowledge-graph memory across chats.",
     "docs_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/memory",
     **_npx("@modelcontextprotocol/server-memory"), "config_fields": []},
    {"id": "sequential-thinking", "label": "Sequential Thinking", "category": "Reference",
     "icon": "🪜", "description": "Structured step-by-step reasoning scratchpad.",
     "docs_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking",
     **_npx("@modelcontextprotocol/server-sequential-thinking"), "config_fields": []},
    {"id": "time", "label": "Time", "category": "Reference", "icon": "🕐",
     "description": "Current time + timezone conversions.",
     "docs_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/time",
     "transport": "stdio", "command": "uvx", "args": ["mcp-server-time"],
     "config_fields": []},
    {"id": "everything", "label": "Everything (demo)", "category": "Reference",
     "icon": "🧪", "description": "Reference server exercising every MCP feature.",
     "docs_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/everything",
     **_npx("@modelcontextprotocol/server-everything"), "config_fields": []},

    # ── Dev & Code ───────────────────────────────────────────────────────────
    {"id": "github", "label": "GitHub", "category": "Dev & Code", "icon": "🐙",
     "description": "Repos, issues, PRs, code & repo search.",
     "docs_url": "https://github.com/github/github-mcp-server",
     **_npx("@modelcontextprotocol/server-github"),
     "config_fields": [_env_field("GITHUB_PERSONAL_ACCESS_TOKEN", "Personal Access Token",
                                  "github.com → Settings → Developer settings → Tokens")]},
    {"id": "gitlab", "label": "GitLab", "category": "Dev & Code", "icon": "🦊",
     "description": "GitLab projects, issues and merge requests.",
     "docs_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/gitlab",
     **_npx("@modelcontextprotocol/server-gitlab"),
     "config_fields": [_env_field("GITLAB_PERSONAL_ACCESS_TOKEN", "Personal Access Token"),
                       _env_field("GITLAB_API_URL", "API URL", "Default https://gitlab.com/api/v4",
                                  secret=False, required=False)]},
    {"id": "git", "label": "Git (local)", "category": "Dev & Code", "icon": "🌿",
     "description": "Read/search/commit a local git repository.",
     "docs_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/git",
     "transport": "stdio", "command": "uvx",
     "args": ["mcp-server-git", "--repository"],
     "config_fields": [{"key": "repository", "label": "Repository path", "target": "arg",
                        "secret": False, "required": True, "placeholder": "/data/repo",
                        "hint": "Local path to the git working tree."}]},
    {"id": "sentry", "label": "Sentry", "category": "Dev & Code", "icon": "🐛",
     "description": "Inspect Sentry issues and error events.",
     "docs_url": "https://docs.sentry.io/product/sentry-mcp/",
     "transport": "sse", "url": "https://mcp.sentry.dev/sse",
     "config_fields": [{"key": "Authorization", "label": "Bearer token", "target": "header",
                        "secret": True, "required": False, "placeholder": "Bearer …",
                        "hint": "Hosted server uses OAuth; token optional."}]},
    {"id": "e2b", "label": "E2B Sandboxes", "category": "Dev & Code", "icon": "📦",
     "description": "Run code in ephemeral cloud sandboxes.",
     "docs_url": "https://github.com/e2b-dev/mcp-server",
     **_npx("@e2b/mcp-server"),
     "config_fields": [_env_field("E2B_API_KEY", "E2B API Key")]},
    {"id": "docker", "label": "Docker", "category": "Dev & Code", "icon": "🐳",
     "description": "Manage containers, images and compose stacks.",
     "docs_url": "https://github.com/ckreiling/mcp-server-docker",
     "transport": "stdio", "command": "uvx", "args": ["mcp-server-docker"],
     "config_fields": []},
    {"id": "kubernetes", "label": "Kubernetes", "category": "Dev & Code", "icon": "☸️",
     "description": "Query and manage a Kubernetes cluster.",
     "docs_url": "https://github.com/Flux159/mcp-server-kubernetes",
     **_npx("mcp-server-kubernetes"),
     "config_fields": [_env_field("KUBECONFIG", "Kubeconfig path", "Path to kubeconfig",
                                  secret=False, required=False)]},

    # ── Databases ────────────────────────────────────────────────────────────
    {"id": "postgres", "label": "PostgreSQL", "category": "Databases", "icon": "🐘",
     "description": "Read-only SQL queries + schema introspection.",
     "docs_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/postgres",
     "transport": "stdio", "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-postgres"],
     "config_fields": [{"key": "connection", "label": "Connection URL", "target": "arg",
                        "secret": True, "required": True,
                        "placeholder": "postgresql://user:pass@host:5432/db",
                        "hint": "Passed as a CLI arg."}]},
    {"id": "sqlite", "label": "SQLite", "category": "Databases", "icon": "🗃️",
     "description": "Query a local SQLite database file.",
     "docs_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/sqlite",
     "transport": "stdio", "command": "uvx",
     "args": ["mcp-server-sqlite", "--db-path"],
     "config_fields": [{"key": "db-path", "label": "Database file", "target": "arg",
                        "secret": False, "required": True, "placeholder": "/data/app.db"}]},
    {"id": "redis", "label": "Redis", "category": "Databases", "icon": "🟥",
     "description": "Read/write keys against a Redis server.",
     "docs_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/redis",
     "transport": "stdio", "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-redis"],
     "config_fields": [{"key": "url", "label": "Redis URL", "target": "arg",
                        "secret": True, "required": True,
                        "placeholder": "redis://localhost:6379"}]},
    {"id": "neo4j", "label": "Neo4j", "category": "Databases", "icon": "🔗",
     "description": "Cypher queries + graph schema over Neo4j.",
     "docs_url": "https://github.com/neo4j-contrib/mcp-neo4j",
     "transport": "stdio", "command": "uvx", "args": ["mcp-neo4j-cypher"],
     "config_fields": [_env_field("NEO4J_URI", "Bolt URI", "bolt://localhost:7687",
                                  secret=False),
                       _env_field("NEO4J_USERNAME", "Username", secret=False),
                       _env_field("NEO4J_PASSWORD", "Password")]},
    {"id": "chroma", "label": "Chroma", "category": "Databases", "icon": "🎨",
     "description": "Query a Chroma vector store.",
     "docs_url": "https://github.com/chroma-core/chroma-mcp",
     "transport": "stdio", "command": "uvx", "args": ["chroma-mcp"],
     "config_fields": [_env_field("CHROMA_HOST", "Host", "localhost", secret=False, required=False),
                       _env_field("CHROMA_PORT", "Port", "8000", secret=False, required=False)]},
    {"id": "mongodb", "label": "MongoDB", "category": "Databases", "icon": "🍃",
     "description": "Query collections + inspect schema.",
     "docs_url": "https://github.com/mongodb-js/mongodb-mcp-server",
     **_npx("mongodb-mcp-server"),
     "config_fields": [_env_field("MDB_MCP_CONNECTION_STRING", "Connection string",
                                  "mongodb+srv://user:pass@cluster/db")]},
    {"id": "supabase", "label": "Supabase", "category": "Databases", "icon": "⚡",
     "description": "Manage Supabase projects, tables and edge functions.",
     "docs_url": "https://github.com/supabase-community/supabase-mcp",
     **_npx("@supabase/mcp-server-supabase@latest"),
     "config_fields": [_env_field("SUPABASE_ACCESS_TOKEN", "Access token")]},

    # ── Search & Web ─────────────────────────────────────────────────────────
    {"id": "brave-search", "label": "Brave Search", "category": "Search & Web",
     "icon": "🦁", "description": "Web + local search via the Brave API.",
     "docs_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/brave-search",
     **_npx("@modelcontextprotocol/server-brave-search"),
     "config_fields": [_env_field("BRAVE_API_KEY", "Brave API Key",
                                  "api.search.brave.com")]},
    {"id": "tavily", "label": "Tavily", "category": "Search & Web", "icon": "🔎",
     "description": "AI-native web search + extraction.",
     "docs_url": "https://github.com/tavily-ai/tavily-mcp",
     **_npx("tavily-mcp@latest"),
     "config_fields": [_env_field("TAVILY_API_KEY", "Tavily API Key", "app.tavily.com")]},
    {"id": "exa", "label": "Exa", "category": "Search & Web", "icon": "🧭",
     "description": "Neural web search built for LLMs.",
     "docs_url": "https://github.com/exa-labs/exa-mcp-server",
     **_npx("exa-mcp-server"),
     "config_fields": [_env_field("EXA_API_KEY", "Exa API Key", "dashboard.exa.ai")]},
    {"id": "perplexity", "label": "Perplexity", "category": "Search & Web", "icon": "❓",
     "description": "Sonar online search + answers.",
     "docs_url": "https://github.com/ppl-ai/modelcontextprotocol",
     **_npx("server-perplexity-ask"),
     "config_fields": [_env_field("PERPLEXITY_API_KEY", "Perplexity API Key")]},
    {"id": "firecrawl", "label": "Firecrawl", "category": "Search & Web", "icon": "🔥",
     "description": "Scrape, crawl and structure whole sites.",
     "docs_url": "https://github.com/mendableai/firecrawl-mcp-server",
     **_npx("firecrawl-mcp"),
     "config_fields": [_env_field("FIRECRAWL_API_KEY", "Firecrawl API Key")]},
    {"id": "puppeteer", "label": "Puppeteer", "category": "Search & Web", "icon": "🎭",
     "description": "Drive a headless Chrome browser.",
     "docs_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/puppeteer",
     **_npx("@modelcontextprotocol/server-puppeteer"), "config_fields": []},
    {"id": "playwright", "label": "Playwright", "category": "Search & Web", "icon": "🎬",
     "description": "Browser automation via Playwright.",
     "docs_url": "https://github.com/microsoft/playwright-mcp",
     **_npx("@playwright/mcp@latest"), "config_fields": []},

    # ── Productivity & Comms ─────────────────────────────────────────────────
    {"id": "slack", "label": "Slack", "category": "Productivity", "icon": "💬",
     "description": "Read channels, post messages, list users.",
     "docs_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/slack",
     **_npx("@modelcontextprotocol/server-slack"),
     "config_fields": [_env_field("SLACK_BOT_TOKEN", "Bot token", "xoxb-…"),
                       _env_field("SLACK_TEAM_ID", "Team ID", secret=False)]},
    {"id": "notion", "label": "Notion", "category": "Productivity", "icon": "📝",
     "description": "Read/write Notion pages and databases.",
     "docs_url": "https://github.com/makenotion/notion-mcp-server",
     **_npx("@notionhq/notion-mcp-server"),
     "config_fields": [_env_field("NOTION_TOKEN", "Integration token", "notion.so/my-integrations")]},
    {"id": "linear", "label": "Linear", "category": "Productivity", "icon": "📐",
     "description": "Issues, projects and cycles in Linear.",
     "docs_url": "https://linear.app/docs/mcp",
     "transport": "sse", "url": "https://mcp.linear.app/sse",
     "config_fields": [{"key": "Authorization", "label": "Bearer token", "target": "header",
                        "secret": True, "required": False, "placeholder": "Bearer …",
                        "hint": "Hosted server uses OAuth; token optional."}]},
    {"id": "todoist", "label": "Todoist", "category": "Productivity", "icon": "✅",
     "description": "Create and manage Todoist tasks.",
     "docs_url": "https://github.com/abhiz123/todoist-mcp-server",
     **_npx("@abhiz123/todoist-mcp-server"),
     "config_fields": [_env_field("TODOIST_API_TOKEN", "API token")]},
    {"id": "obsidian", "label": "Obsidian", "category": "Productivity", "icon": "🟣",
     "description": "Read/search an Obsidian vault (Local REST API).",
     "docs_url": "https://github.com/MarkusPfundstein/mcp-obsidian",
     "transport": "stdio", "command": "uvx", "args": ["mcp-obsidian"],
     "config_fields": [_env_field("OBSIDIAN_API_KEY", "Local REST API key"),
                       _env_field("OBSIDIAN_HOST", "Host", "127.0.0.1", secret=False, required=False)]},
    {"id": "airtable", "label": "Airtable", "category": "Productivity", "icon": "📊",
     "description": "Read/write Airtable bases and records.",
     "docs_url": "https://github.com/domdomegg/airtable-mcp-server",
     **_npx("airtable-mcp-server"),
     "config_fields": [_env_field("AIRTABLE_API_KEY", "Personal access token")]},
    {"id": "atlassian", "label": "Jira / Confluence", "category": "Productivity", "icon": "🧩",
     "description": "Atlassian Jira issues and Confluence pages.",
     "docs_url": "https://github.com/sooperset/mcp-atlassian",
     "transport": "stdio", "command": "uvx", "args": ["mcp-atlassian"],
     "config_fields": [_env_field("JIRA_URL", "Jira/Confluence URL",
                                  "https://you.atlassian.net", secret=False),
                       _env_field("JIRA_USERNAME", "Email", secret=False),
                       _env_field("JIRA_API_TOKEN", "API token")]},
    {"id": "google-drive", "label": "Google Drive", "category": "Productivity", "icon": "📂",
     "description": "Search and read Google Drive files.",
     "docs_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/gdrive",
     **_npx("@modelcontextprotocol/server-gdrive"),
     "config_fields": [_env_field("GDRIVE_CREDENTIALS_PATH", "OAuth credentials JSON path",
                                  "Path to the downloaded OAuth client JSON", secret=False)]},
    {"id": "discord", "label": "Discord", "category": "Productivity", "icon": "🎮",
     "description": "Send/read messages via a Discord bot.",
     "docs_url": "https://github.com/barryyip0625/mcp-discord",
     **_npx("mcp-discord"),
     "config_fields": [_env_field("DISCORD_TOKEN", "Bot token")]},

    # ── Commerce & Finance ───────────────────────────────────────────────────
    {"id": "stripe", "label": "Stripe", "category": "Commerce & Finance", "icon": "💳",
     "description": "Payments, customers, invoices via Stripe.",
     "docs_url": "https://github.com/stripe/agent-toolkit",
     **_npx("@stripe/mcp", "--tools=all"),
     "config_fields": [_env_field("STRIPE_SECRET_KEY", "Secret key", "sk_live_… / sk_test_…")]},
    {"id": "shopify", "label": "Shopify", "category": "Commerce & Finance", "icon": "🛍️",
     "description": "Storefront + admin data for a Shopify shop.",
     "docs_url": "https://github.com/Shopify/dev-mcp",
     **_npx("@shopify/dev-mcp@latest"), "config_fields": []},

    # ── Maps & Data ──────────────────────────────────────────────────────────
    {"id": "google-maps", "label": "Google Maps", "category": "Maps & Data", "icon": "🗺️",
     "description": "Geocoding, places, directions.",
     "docs_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/google-maps",
     **_npx("@modelcontextprotocol/server-google-maps"),
     "config_fields": [_env_field("GOOGLE_MAPS_API_KEY", "Maps API Key")]},

    # ── Media & AI ───────────────────────────────────────────────────────────
    {"id": "elevenlabs", "label": "ElevenLabs", "category": "Media & AI", "icon": "🔊",
     "description": "Text-to-speech and voice tooling.",
     "docs_url": "https://github.com/elevenlabs/elevenlabs-mcp",
     "transport": "stdio", "command": "uvx", "args": ["elevenlabs-mcp"],
     "config_fields": [_env_field("ELEVENLABS_API_KEY", "ElevenLabs API Key")]},
    {"id": "huggingface", "label": "Hugging Face", "category": "Media & AI", "icon": "🤗",
     "description": "Search models, datasets and Spaces.",
     "docs_url": "https://huggingface.co/settings/mcp",
     "transport": "sse", "url": "https://huggingface.co/mcp",
     "config_fields": [{"key": "Authorization", "label": "Bearer token", "target": "header",
                        "secret": True, "required": False, "placeholder": "Bearer hf_…",
                        "hint": "HF access token (optional for public content)."}]},
    {"id": "figma", "label": "Figma", "category": "Media & AI", "icon": "🎛️",
     "description": "Pull layout/design data from Figma files.",
     "docs_url": "https://github.com/GLips/Figma-Context-MCP",
     **_npx("figma-developer-mcp", "--stdio"),
     "config_fields": [_env_field("FIGMA_API_KEY", "Personal access token")]},

    # ── Cloud & Infra ────────────────────────────────────────────────────────
    {"id": "cloudflare", "label": "Cloudflare", "category": "Cloud & Infra", "icon": "☁️",
     "description": "Workers, KV, R2, DNS and analytics.",
     "docs_url": "https://github.com/cloudflare/mcp-server-cloudflare",
     "transport": "sse", "url": "https://observability.mcp.cloudflare.com/sse",
     "config_fields": [{"key": "Authorization", "label": "Bearer token", "target": "header",
                        "secret": True, "required": False, "placeholder": "Bearer …",
                        "hint": "Hosted server uses OAuth; token optional."}]},
    {"id": "grafana", "label": "Grafana", "category": "Cloud & Infra", "icon": "📈",
     "description": "Dashboards, datasources and alerts.",
     "docs_url": "https://github.com/grafana/mcp-grafana",
     "transport": "stdio", "command": "mcp-grafana", "args": [],
     "config_fields": [_env_field("GRAFANA_URL", "Grafana URL", "https://grafana.local",
                                  secret=False),
                       _env_field("GRAFANA_API_KEY", "Service account token")]},

    # ── Vera peers (this catalog's own transport) ────────────────────────────
    {"id": "vera-peer", "label": "Vera Peer", "category": "Vera", "icon": "🛰️",
     "description": "Another Vera orchestrator — proxy its capabilities over HTTP.",
     "docs_url": "",
     "transport": "vera_proxy", "url": "",
     "config_fields": [{"key": "url", "label": "Base URL", "target": "url",
                        "secret": False, "required": True,
                        "placeholder": "http://agent-b.int:8000",
                        "hint": "Registers via /mcp/servers/register (existing proxy)."}]},
]

# Fill in the invariant defaults every seed omits.
for _e in CATALOG_SEED:
    _e.setdefault("transport", "stdio")
    _e.setdefault("command", "")
    _e.setdefault("args", [])
    _e.setdefault("url", "")
    _e.setdefault("env", {})
    _e.setdefault("headers", {})
    _e.setdefault("config_fields", [])
    _e.setdefault("docs_url", "")


RECORD_DEFAULTS: Dict[str, Any] = {
    "id": "", "label": "", "category": "Custom", "icon": "🔌",
    "description": "", "docs_url": "",
    "transport": "stdio", "command": "", "args": [], "url": "",
    "env": {}, "headers": {}, "arg_values": {}, "config_fields": [],
    "enabled": False, "status": "unconfigured", "seeded": False, "notes": "",
}

# The three keyed credential/config bags and the config_field `target` that maps
# to each. Iterated together everywhere secrets are sealed/opened/redacted.
_BAGS = (("env", "env"), ("header", "headers"), ("arg", "arg_values"))


# ─────────────────────────────────────────────────────────────────────────────
# STORE
# ─────────────────────────────────────────────────────────────────────────────

def _slug(s: str) -> str:
    return _SLUG_RE.sub("-", (s or "").strip().lower()).strip("-") or "server"


def _secret_keys(rec: Dict, target: str) -> set:
    """Config-field keys flagged secret for a given target (env/header)."""
    return {f["key"] for f in rec.get("config_fields", [])
            if f.get("secret") and f.get("target") == target}


def _seal_secrets(rec: Dict) -> Dict:
    """Return a copy with secret env/header/arg values sealed (idempotent)."""
    out = dict(rec)
    for target, bag in _BAGS:
        keys = _secret_keys(rec, target)
        src = dict(rec.get(bag, {}))
        for k in keys:
            if src.get(k):
                src[k] = vsecrets.seal(src[k])
        out[bag] = src
    return out


def _open_secrets(rec: Dict) -> Dict:
    """Return a copy with secret env/header/arg values decrypted (internal use)."""
    out = dict(rec)
    for target, bag in _BAGS:
        keys = _secret_keys(rec, target)
        src = dict(rec.get(bag, {}))
        for k in keys:
            if src.get(k):
                src[k] = vsecrets.open_secret(src[k])
        out[bag] = src
    return out


def _redact(rec: Dict) -> Dict:
    """UI-safe copy: secret env/header/arg values replaced with •••• markers."""
    out = dict(rec)
    for target, bag in _BAGS:
        keys = _secret_keys(rec, target)
        src = dict(rec.get(bag, {}))
        redb: Dict[str, Any] = {}
        for k, v in src.items():
            if k in keys:
                redb[k] = "••••••••" if v else ""
            else:
                redb[k] = v
        out[bag] = redb
    return out


async def _all_raw() -> List[Dict]:
    r = _redis()
    if not r:
        return []
    items = await r.hgetall(KEY_CATALOG)
    out = []
    for v in items.values():
        try:
            out.append(json.loads(v))
        except Exception:
            continue
    return out


_seeded_flag = False


async def _ensure_seeded() -> None:
    """Idempotently write any catalog record whose id is not already stored.
    Never clobbers operator edits — a seed record present in Redis is left as-is.
    """
    global _seeded_flag
    if _seeded_flag:
        return
    r = _redis()
    if not r:
        return
    existing = set(await r.hkeys(KEY_CATALOG))
    existing = {k.decode() if isinstance(k, (bytes, bytearray)) else k for k in existing}
    added = 0
    for tmpl in CATALOG_SEED:
        if tmpl["id"] in existing:
            continue
        rec = {**RECORD_DEFAULTS, **tmpl, "seeded": True,
               "created": now_iso(), "updated": now_iso()}
        await r.hset(KEY_CATALOG, rec["id"], json.dumps(rec))
        added += 1
    _seeded_flag = True
    if added:
        log.info("mcp.catalog: seeded %d MCP server records", added)


async def _get_raw(sid: str) -> Optional[Dict]:
    r = _redis()
    if not r or not sid:
        return None
    raw = await r.hget(KEY_CATALOG, sid)
    return json.loads(raw) if raw else None


# ── Importable helper for other modules ──────────────────────────────────────

async def get_server(server_id: str, opened: bool = True) -> Optional[Dict]:
    """Fetch one catalog record. opened=True decrypts secrets (internal use)."""
    rec = await _get_raw(server_id)
    if not rec:
        return None
    return _open_secrets(rec) if opened else _redact(rec)


async def upsert_server(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Create/update a record. Present keys are updated; secret env/header values
    left blank on edit keep the stored value. Returns {ok, server(redacted)}.
    """
    r = _redis()
    if not r:
        return {"error": "store unavailable"}
    sid = fields.get("id") or _slug(fields.get("label", ""))
    existing = await _get_raw(sid)
    rec = dict(existing) if existing else {**RECORD_DEFAULTS, "id": sid,
                                           "created": now_iso()}
    for k in ("label", "category", "icon", "description", "docs_url", "transport",
              "command", "url", "enabled", "status", "notes"):
        if fields.get(k) is not None:
            rec[k] = fields[k]
    if fields.get("args") is not None:
        rec["args"] = fields["args"]
    if fields.get("config_fields") is not None:
        rec["config_fields"] = fields["config_fields"]

    # Merge env/headers/arg_values. Blank secret values keep the existing sealed
    # value (so re-saving a form that shows •••• does not wipe the secret).
    for target, bag in _BAGS:
        if fields.get(bag) is None:
            continue
        incoming = dict(fields[bag] or {})
        merged = dict(rec.get(bag, {}))
        secret_keys = _secret_keys(rec, target)
        for k, v in incoming.items():
            if k in secret_keys and (not v or v == "••••••••"):
                continue  # keep existing sealed secret
            merged[k] = v
        # Allow explicit removal of non-secret keys passed as None
        rec[bag] = {k: val for k, val in merged.items() if val is not None}

    try:
        rec = _seal_secrets(rec)
    except RuntimeError as e:
        return {"error": str(e)}

    if not rec.get("label"):
        rec["label"] = rec["id"]
    # Auto status: configured once every required field has a value.
    if rec.get("status") in ("", "unconfigured", "configured"):
        rec["status"] = "configured" if _is_configured(rec) else "unconfigured"
    rec["updated"] = now_iso()
    await r.hset(KEY_CATALOG, rec["id"], json.dumps(rec))
    return {"ok": True, "server": _redact(rec)}


def _field_value(rec: Dict, f: Dict) -> Any:
    """Current value of a config field, reading from the bag its target names."""
    tgt, key = f.get("target"), f.get("key")
    if tgt == "url":
        return rec.get("url")
    if tgt == "header":
        return rec.get("headers", {}).get(key)
    if tgt == "arg":
        return rec.get("arg_values", {}).get(key)
    return rec.get("env", {}).get(key)


def _is_configured(rec: Dict) -> bool:
    """True when every required config field has a value."""
    return all(_field_value(rec, f) for f in rec.get("config_fields", [])
               if f.get("required"))


def effective_args(rec: Dict) -> List[str]:
    """Base args + resolved arg-target field values, in field order — the final
    argv an MCP client runtime would launch the stdio server with."""
    out = list(rec.get("args", []))
    for f in rec.get("config_fields", []):
        if f.get("target") == "arg":
            v = rec.get("arg_values", {}).get(f["key"])
            if v:
                out.append(str(v))
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  CAPABILITIES
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "mcp.catalog.list", http_method="GET", http_path="/mcp/catalog/list",
    http_tags=["mcp"], memory="off", silent=True,
    description="List MCP server records (REDACTED — secret env/header values "
                "never returned, only ••••). Records are grouped by `category`. "
                "Output: {servers:[...], categories:[...]}.",
)
async def cap_list(trace_id=None):
    await _ensure_seeded()
    servers = [_redact(s) for s in await _all_raw()]
    servers.sort(key=lambda s: (s.get("category", ""), s.get("label", "")))
    cats = sorted({s.get("category", "Custom") for s in servers})
    return {"servers": servers, "categories": cats}


@capability(
    "mcp.catalog.get", http_method="GET", http_path="/mcp/catalog/get",
    http_tags=["mcp"], memory="off", silent=True,
    description="Get one MCP server record (REDACTED). Input: id (str!).",
)
async def cap_get(id: str = "", trace_id=None):
    await _ensure_seeded()
    rec = await _get_raw(id)
    if not rec:
        return {"error": "server not found"}
    return _redact(rec)


@capability(
    "mcp.catalog.upsert", http_method="POST", http_path="/mcp/catalog/upsert",
    http_tags=["mcp"], memory="on",
    description="Create or update an MCP server record. Secret env/header values "
                "are sealed before storage; leave a secret blank on edit to keep "
                "the stored value. Input: id (omit to create from label), label, "
                "category, icon, description, docs_url, transport "
                "(stdio|sse|http|vera_proxy), command, args (list), url, "
                "env (object), headers (object), arg_values (object — values for "
                "arg-target config fields), config_fields (list), enabled "
                "(bool), notes. Output: {ok, server (redacted)}.",
)
async def cap_upsert(id: str = "", label: str = "", category: str = "",
                     icon: str = "", description: str = "", docs_url: str = "",
                     transport: str = "", command: str = "",
                     args: Optional[List] = None, url: str = "",
                     env: Optional[Dict] = None, headers: Optional[Dict] = None,
                     arg_values: Optional[Dict] = None,
                     config_fields: Optional[List] = None,
                     enabled: Optional[bool] = None, notes: str = "",
                     trace_id=None):
    fields: Dict[str, Any] = {"id": id}
    for k, v in (("label", label or None), ("category", category or None),
                 ("icon", icon or None), ("description", description or None),
                 ("docs_url", docs_url or None), ("transport", transport or None),
                 ("command", command or None), ("url", url or None),
                 ("notes", notes or None), ("enabled", enabled),
                 ("args", args), ("env", env), ("headers", headers),
                 ("arg_values", arg_values), ("config_fields", config_fields)):
        if v is not None:
            fields[k] = v
    return await upsert_server(fields)


@capability(
    "mcp.catalog.delete", http_method="POST", http_path="/mcp/catalog/delete",
    http_tags=["mcp"], memory="on",
    description="Delete an MCP server record by id. Input: id (str!). "
                "Output: {ok}. (Reseed restores built-in records.)",
)
async def cap_delete(id: str = "", trace_id=None):
    if not id:
        return {"error": "id is required"}
    r = _redis()
    if not r:
        return {"error": "store unavailable"}
    return {"ok": bool(await r.hdel(KEY_CATALOG, id))}


@capability(
    "mcp.catalog.reseed", http_method="POST", http_path="/mcp/catalog/reseed",
    http_tags=["mcp"], memory="off",
    description="Re-add any built-in catalog records that are missing (never "
                "overwrites existing records). Input: force (bool — when true, "
                "also reset built-in records to their template, discarding edits/"
                "secrets). Output: {added, reset}.",
)
async def cap_reseed(force: bool = False, trace_id=None):
    global _seeded_flag
    r = _redis()
    if not r:
        return {"error": "store unavailable"}
    existing = set(await r.hkeys(KEY_CATALOG))
    existing = {k.decode() if isinstance(k, (bytes, bytearray)) else k for k in existing}
    added = reset = 0
    for tmpl in CATALOG_SEED:
        present = tmpl["id"] in existing
        if present and not force:
            continue
        rec = {**RECORD_DEFAULTS, **tmpl, "seeded": True,
               "created": now_iso(), "updated": now_iso()}
        await r.hset(KEY_CATALOG, rec["id"], json.dumps(rec))
        if present:
            reset += 1
        else:
            added += 1
    _seeded_flag = True
    return {"ok": True, "added": added, "reset": reset,
            "catalog_size": len(CATALOG_SEED)}


@capability(
    "mcp.catalog.connect", http_method="POST", http_path="/mcp/catalog/connect",
    http_tags=["mcp"], memory="on",
    description="Connect/enable a configured MCP server. For transport="
                "'vera_proxy' this registers the peer via the orchestrator proxy "
                "(its capabilities become callable immediately). For stdio/sse/"
                "http transports the config is validated and marked 'configured' "
                "— launching those requires an external MCP client runtime. "
                "Input: id (str!). Output: {ok, status, registered?}.",
)
async def cap_connect(id: str = "", trace_id=None):
    rec = await _get_raw(id)
    if not rec:
        return {"error": "server not found"}
    opened = _open_secrets(rec)

    if rec.get("transport") == "vera_proxy":
        url = opened.get("url", "").strip()
        if not url:
            return {"error": "set the peer Base URL first"}
        try:
            registered = await _orch.register_mcp_server(url, rec["id"])
        except Exception as e:
            await _set_status(id, "error")
            return {"error": f"register failed: {e}"}
        await _set_status(id, "connected" if registered else "error", enabled=True)
        return {"ok": bool(registered), "status": "connected" if registered else "error",
                "registered": registered, "count": len(registered)}

    # Non-Vera transports: validate config, store as configured/enabled.
    if not _is_configured(rec):
        missing = [f["label"] for f in rec.get("config_fields", [])
                   if f.get("required") and not _field_value(opened, f)]
        return {"error": "missing required config: " + ", ".join(missing)}
    await _set_status(id, "configured", enabled=True)
    return {"ok": True, "status": "configured",
            "note": "Config stored. Launch via your MCP client runtime "
                    "(command/args/env below) or a Vera stdio bridge."}


async def _set_status(sid: str, status: str, enabled: Optional[bool] = None) -> None:
    r = _redis()
    rec = await _get_raw(sid)
    if not r or not rec:
        return
    rec["status"] = status
    if enabled is not None:
        rec["enabled"] = enabled
    rec["updated"] = now_iso()
    await r.hset(KEY_CATALOG, sid, json.dumps(rec))


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "mcp.catalog.panel.html", http_method="GET", http_path="/mcp/catalog/panel",
    http_tags=["mcp", "ui"], memory="off", silent=True,
    description="Serve the MCP Catalog panel HTML.",
)
async def cap_panel_html(trace_id=None):
    try:
        html = _PANEL_HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = ("<!DOCTYPE html><html><body style='background:#0d0f12;"
                "color:#ef5b5b;font-family:monospace;padding:40px'>"
                "<h2>mcp_catalog_panel.html not found</h2>"
                f"<p>Expected at: {_PANEL_HTML_PATH}</p></body></html>")
    return HTMLResponse(html)


@APP.get("/mcp/catalog/panel", include_in_schema=False)
async def _mcp_catalog_panel_route():
    p = _HERE / "mcp_catalog_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>mcp_catalog_panel.html not found</p>")


register_ui(
    "mcp-catalog-panel",
    "MCP Servers",
    "🔌",
    """<div id="mcp-catalog-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/mcp/catalog/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=["mcp.catalog.list", "mcp.catalog.get", "mcp.catalog.upsert",
             "mcp.catalog.delete", "mcp.catalog.reseed", "mcp.catalog.connect"],
    # mode="tab" (2026-08-16 fix, was "inject" — invisible by default:
    # "inject" only shows up inside the Media sub-switcher, a specific host
    # panel a user has to already know to open; "tab" auto-creates its own
    # top-level harness tab, which is what "surface it somewhere" needs).
    mode="tab",
    tab_order=72,
)

log.info("mcp_catalog_capabilities: ready — %d catalog templates", len(CATALOG_SEED))
