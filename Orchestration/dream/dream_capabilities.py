"""
dream_capabilities.py  —  Vera Dream System
=====================================================
A modular, capability-based "dream" pipeline that runs when the system is idle.

Concept
───────
When the orchestrator has been quiet for a while, Vera can spin up a background
"dream cycle" — a pipeline of small capabilities (sensors + stages) strung
together by a trigger record. Each trigger says:

    • when to run      (hours window, idle threshold, cooldown)
    • what to sense    (which dream.sensor.* caps to call — memory, fabric,
                        syslog, research, event bus, RSS news …)
    • how to act       (synthesize_only | plan_execute | oneshot)
    • what to deliver  (telegram / memory / notebook / all)

The dream cycle itself is just a list of stage capability names:

    gather → themes → plan → execute → synthesize → deliver

Each stage is a real @capability — you can add new stages, swap them out,
reorder them, or write your own just by registering a new dream.stage.X cap
and listing it in a trigger's pipeline.

Human-in-the-loop
─────────────────
If a trigger has hitl=True and a Telegram admin chat is configured, the
execute stage sends an "I've been thinking about X — should I do Y?" message
and waits (up to default_hitl_timeout_s) for a reply before acting. Reply with
yes/ok/go/do it to approve, anything else to cancel.

Safety
──────
A capability whitelist gates which tools the planner can use while dreaming.
Dreams can't run arbitrary code — only caps the admin has explicitly allowed.
Sensible defaults are seeded on first start (memory, fabric, nlp, llm, syslog,
and the dream sensor/stage caps themselves).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.responses import HTMLResponse

import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import (
    APP,
    CAPABILITY_REGISTRY,
    capability,
    emit_event,
    now_iso,
    register_ui,
    schedule,
)

log = logging.getLogger("vera.dream")

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS / STATE
# ─────────────────────────────────────────────────────────────────────────────

_HERE            = Path(__file__).parent
_PANEL_HTML_PATH = _HERE / "dream_panel.html"

KEY_CONFIG       = "vera:dream:config"
KEY_TRIGGERS     = "vera:dream:triggers"
KEY_HISTORY      = "vera:dream:history"
KEY_WHITELIST    = "vera:dream:whitelist"
KEY_RUNNING      = "vera:dream:running"
KEY_HITL         = "vera:dream:hitl_pending"
KEY_HITL_RESP    = "vera:dream:hitl_response"
KEY_LAST_RUN     = "vera:dream:last_trigger_run"
KEY_RECENT_CAPS  = "vera:cap:recent"
KEY_PREVIEW      = "vera:dream:preview"
KEY_LLM_TOKENS   = "vera:dream:llm_tokens"
KEY_NO_HITL      = "vera:dream:no_hitl_caps"      # caps that bypass HITL even when trigger.hitl=true
KEY_DIRECTOR     = "vera:dream:director"          # director's recommendations cache
KEY_LOOP_SETTINGS = "vera:dream:loop_settings"    # global agent-loop settings (JSON)

# Global default agent-loop settings — mirrors the dag.agent_loop_v2 surface so
# dream loops can be tuned exactly like the DAG Workshop loop. Resolution order
# (low→high): these defaults < global override (Redis) < per-trigger/pipeline
# (trig["loop_settings"]) < project (seed["project_loop_settings"]) < seed.
DEFAULT_LOOP_SETTINGS: Dict[str, Any] = {
    "max_cycles":                8,
    "triage_top_k":              16,
    "max_search_calls":          2,
    "max_expands":               1,
    "count_failed_cycles":       False,
    "satisfaction_check":        True,
    "enable_expand":             True,
    "await_long_running":        True,
    "long_running_timeout_secs": 1800,
    "max_recovery_attempts":     2,
    "prefer_gpu":                True,
    "model":                     "",
    "instance_id":               "",
    "system_prompt_template":    "",
}

HISTORY_CAP      = 200

# ─────────────────────────────────────────────────────────────────────────────
# SENSOR + STAGE REGISTRIES
# ─────────────────────────────────────────────────────────────────────────────
# These registries make sensors and pipeline stages introspectable: the panel
# reads them to render configuration UI, and triggers reference them by id.
# Each entry is a metadata record describing what the sensor/stage does and
# what parameters it accepts. The actual @capability functions register
# themselves at import time via the helpers below.

# {sensor_id: {"id", "label", "description", "cap", "params": [{name,type,default,help}]}}
SENSOR_REGISTRY: Dict[str, Dict[str, Any]] = {}

# {stage_id: {"id", "label", "description", "cap", "phase", "optional", "params"}}
# phase is "gather"|"analyze"|"plan"|"act"|"emit" — used for pipeline ordering hints
STAGE_REGISTRY: Dict[str, Dict[str, Any]] = {}


def _register_sensor(
    sid: str, label: str, description: str, cap: str,
    params: Optional[List[Dict[str, Any]]] = None,
) -> None:
    SENSOR_REGISTRY[sid] = {
        "id":          sid,
        "label":       label,
        "description": description,
        "cap":         cap,
        "params":      params or [],
    }


def _register_stage(
    sid: str, label: str, description: str, cap: str,
    phase: str = "analyze", optional: bool = True,
    params: Optional[List[Dict[str, Any]]] = None,
) -> None:
    STAGE_REGISTRY[sid] = {
        "id":          sid,
        "label":       label,
        "description": description,
        "cap":         cap,
        "phase":       phase,
        "optional":    optional,
        "params":      params or [],
    }

# In-process runtime
_SCHED_TASK:   Optional[asyncio.Task] = None
_SCHED_RUN:    bool                    = False
_CYCLE_TASK:   Optional[asyncio.Task] = None
_CYCLE_CANCEL: bool                    = False

# Prefixes whose capability calls don't count as "activity" for idle detection
_IDLE_IGNORE_PREFIXES = (
    "dream.", "obs.", "health.", "ui.", "syslog.", "tg.events.status",
    "cluster.", "ollama.", "heartbeat", "echo", "caps.", "mcp.",
)

# Default: only these cap prefixes RESET the idle timer (everything else is ignored).
# If config has idle_reset_prefixes set, that overrides this.
DEFAULT_IDLE_RESET_PREFIXES = [
    "llm.", "agent.", "research.", "tg.",
]

# Configurable: only caps matching these prefixes RESET the idle timer.
# Everything else is ignored. Stored in Redis; defaults to LLM-related caps.
KEY_IDLE_RESET_PREFIXES = "vera:dream:idle_reset_prefixes"
DEFAULT_IDLE_RESET_PREFIXES = [
    "llm.", "agent.", "research.", "tg.send", "tg.notify",
]

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled":                 True,
    "min_idle_minutes":        15,
    "tick_interval_seconds":   60,
    "telegram_bridge":         True,
    "default_hitl_timeout_s":  600,
    "llm_prefer_gpu":          True,
    "max_history":             HISTORY_CAP,
}

DEFAULT_WHITELIST = [
    "memory.search", "memory.recall", "memory.similar", "memory.stats",
    "memory.session_history",
    # Phase 1: memory traversal + write
    "memory.traverse", "memory.all_nodes", "memory.create", "memory.graph_stats",
    "fabric.query", "fabric.datasets", "fabric.stats",
    # Phase 1: fabric entity graph + sources
    "fabric.entity_graph.snapshot", "fabric.ingest", "fabric.sources",
    "syslog.query", "syslog.errors", "obs.events", "obs.health",
    "nlp.run", "nlp.modules", "llm.generate", "llm.summarize", "llm.qa",
    "research.history", "research.db.search",
    # Phase 1: research continuation + expansion
    "research.expand", "research.quick_search",
    "research.job.status", "research.iterate.list",
    # Phase 1: IDE source inspection
    "ide.inspect.source_info", "ide.inspect.snapshot",
    "ide.inspect.list_snapshots", "ide.inspect.diff_snapshot",
    "ide.inspect.review_file", "ide.inspect.plan_improvement",
    # Phase 1: project awareness
    "project.list", "project.context",
    # Sensors + stages
    "dream.sensor.memory_recent", "dream.sensor.fabric_recent",
    "dream.sensor.syslog_errors", "dream.sensor.bus_events",
    "dream.sensor.news_overnight", "dream.sensor.research_recent",
    "dream.sensor.active_projects", "dream.sensor.source_changes",
    "dream.sensor.memory_graph_walk",
    "dream.stage.gather", "dream.stage.themes", "dream.stage.synthesize",
    "dream.stage.goal_refine",
]


def _default_triggers() -> List[Dict[str, Any]]:
    return [
        {
            "name":         "morning_news",
            "label":        "Morning News Brief",
            "description":  "Overnight RSS auto-discovered via fabric.sources tags → "
                            "morning briefing. Add a new RSS feed tagged 'news' to fabric "
                            "and it'll appear in tomorrow's brief automatically.",
            "enabled":      True,
            "sensors":      ["dream.sensor.fabric_by_tag",
                             "dream.sensor.news_overnight"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  5,
            "hours_end":    9,
            "min_idle_minutes":    20,
            "min_interval_minutes": 720,
            "require_signal":       0.15,
            "depth":        "standard",
            "deliver_to":   ["telegram", "memory"],
            "sensor_params": {
                "fabric_by_tag":   {"tags": "news,rss", "limit": 50, "per_dataset": 8},
                "news_overnight":  {"limit": 30},
            },
            "prompt": (
                "Produce a warm, concise morning briefing from the news/RSS items "
                "above. Cluster by theme, lead with the single most important item, "
                "and keep it to ~250 words. End with one line about what the user "
                "might want to act on. Cite source datasets where the items came "
                "from. If sensors returned nothing, say so honestly."
            ),
        },
        {
            "name":         "research_followup",
            "label":        "Research Follow-up",
            "description":  "Iteratively investigate the most promising open research thread. "
                            "Pulls full content of recent jobs and uses the agentic loop to "
                            "deepen understanding before recommending a follow-up.",
            "enabled":      True,
            "sensors":      ["dream.sensor.research_recent",
                             "dream.sensor.memory_recent"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.investigate",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "iterate":      {"enabled": True, "max_iterations": 6, "min_iterations": 2,
                             "convergence_min_new_findings": 1},
            "mode":         "stepwise",
            "hitl":         False,
            "hours_start":  0,
            "hours_end":    24,
            "min_idle_minutes":    30,
            "min_interval_minutes": 360,
            "require_signal":       0.2,
            "depth":        "standard",
            "max_steps":    6,
            "deliver_to":   ["notebook", "memory"],
            "sensor_params": {
                "research_recent": {"limit": 20, "full_content_top": 4},
            },
            "no_hitl_caps": [
                "memory.search", "memory.recall", "memory.all_nodes",
                "research.history", "research.db.search", "research.bookmarks",
                "research.job.status", "research.iterate.list",
                "research.quick_search", "research.expand",
                "fabric.query", "fabric.datasets",
                "llm.summarize", "llm.qa", "llm.analyze",
            ],
            "prompt": (
                "Investigate the most promising open thread from recent research. "
                "Use research.job.status to fetch full content of recent jobs, "
                "research.expand to dive deeper into specific findings, and "
                "research.quick_search if you need fresh data. After 3-5 useful "
                "investigations, propose ONE concrete next research step grounded "
                "in real prior content. Never invent topics — if sensors returned "
                "nothing or jobs are empty, say so and stop."
            ),
        },
        {
            "name":         "error_review",
            "label":        "Error Review",
            "description":  "Notice and summarise recurring system errors",
            "enabled":      True,
            "sensors":      ["dream.sensor.syslog_errors"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  0,
            "hours_end":    24,
            "min_idle_minutes":    15,
            "min_interval_minutes": 240,
            "require_signal":       0.3,
            "depth":        "brief",
            "deliver_to":   ["memory"],
            "prompt": (
                "Summarise recent system errors. Group by type, identify anything "
                "recurring, and suggest what to investigate first. Skip silently if "
                "nothing notable has happened."
            ),
        },
        {
            "name":         "wander",
            "label":        "Memory Wander",
            "description":  "Agentic graph exploration — picks an under-explored memory "
                            "node, traverses edges, runs entity extraction on sparse "
                            "records, and stores new connections.",
            "enabled":      True,
            "sensors":      ["dream.sensor.memory_graph_walk",
                             "dream.sensor.memory_recent"],
            "pipeline":     ["dream.stage.gather",
                             "dream.stage.memory_deep_traverse",
                             "dream.stage.themes",
                             "dream.stage.goal_refine",
                             "dream.stage.agent_loop",
                             "dream.stage.synthesize",
                             "dream.stage.deliver"],
            "mode":         "agent_loop",
            "hitl":         False,
            "hours_start":  0,
            "hours_end":    24,
            "min_idle_minutes":    45,
            "min_interval_minutes": 360,
            "require_signal":       0.1,
            "depth":        "standard",
            "max_steps":    6,
            "deliver_to":   ["notebook", "memory"],
            "whitelist": [
                "memory.search", "memory.recall", "memory.similar",
                "memory.traverse", "memory.all_nodes", "memory.create",
                "memory.graph_stats",
                "fabric.query", "fabric.datasets",
                "fabric.entity_graph.snapshot",
                "nlp.run", "nlp.modules",
                "llm.generate", "llm.summarize", "llm.qa",
            ],
            "no_hitl_caps": [
                "memory.search", "memory.recall", "memory.similar",
                "memory.traverse", "memory.all_nodes", "memory.graph_stats",
                "fabric.query", "fabric.datasets",
                "nlp.run", "llm.summarize", "llm.qa",
            ],
            "prompt": (
                "You are wandering the memory graph looking for unexplored territory. "
                "The sensor data includes a SEED NODE and its connections. Your goals:\n"
                "1. If the seed node has few connections, use memory.similar to find "
                "semantically related nodes that SHOULD be connected but aren't.\n"
                "2. If you find a node with rich text but no entity extraction done, "
                "use nlp.run to extract entities from it.\n"
                "3. If you find an interesting cluster, use memory.create to store a "
                "'connection' observation linking the related IDs.\n"
                "4. If you find nothing interesting, say so honestly and stop.\n"
                "Always reference specific node IDs and content. Never invent data."
            ),
        },
        {
            "name":         "bus_watcher",
            "label":        "Event Bus Digest",
            "description":  "Digest the orchestrator event bus (noisy — disabled by default)",
            "enabled":      False,
            "sensors":      ["dream.sensor.bus_events"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  0,
            "hours_end":    24,
            "min_idle_minutes":    20,
            "min_interval_minutes": 180,
            "require_signal":       0.5,
            "depth":        "brief",
            "deliver_to":   ["memory"],
            "prompt": "Digest recent event bus activity. Only report if something genuinely interesting happened.",
        },
        {
            "name":         "agentic_explore",
            "label":        "Agentic Exploration",
            "description":  "Stepwise agentic loop — let the LLM choose tools to investigate something",
            "enabled":      False,
            "sensors":      ["dream.sensor.memory_recent", "dream.sensor.fabric_recent"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.stepwise_execute",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "stepwise",
            "hitl":         True,
            "hours_start":  0,
            "hours_end":    24,
            "min_idle_minutes":    45,
            "min_interval_minutes": 480,
            "require_signal":       0.2,
            "depth":        "deep",
            "max_steps":    8,
            "deliver_to":   ["notebook", "memory"],
            "no_hitl_caps": ["memory.search", "memory.recall", "memory.all_nodes",
                             "fabric.query", "fabric.datasets", "research.quick_search"],
            "prompt": (
                "Investigate something interesting in the recent activity. "
                "Use whitelisted caps to gather more context, then synthesise findings. "
                "Stop when you have something worth reporting OR when no useful step "
                "is available. Don't take action without HITL approval."
            ),
        },
        {
            "name":         "code_reflection",
            "label":        "Code Reflection",
            "description":  "Reflect on recent IDE workspace activity and suggest improvements",
            "enabled":      False,
            "sensors":      ["dream.sensor.ide_workspace", "dream.sensor.cap_calls"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.enrich_context", "dream.stage.propose_action",
                             "dream.stage.synthesize", "dream.stage.quality_check",
                             "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  0,
            "hours_end":    24,
            "min_idle_minutes":    30,
            "min_interval_minutes": 240,
            "require_signal":       0.2,
            "depth":        "standard",
            "deliver_to":   ["notebook", "memory"],
            "sensor_params": {
                "cap_calls": {"prefix": "ide.", "limit": 30},
            },
            "prompt": (
                "Review recent IDE workspace activity. Identify patterns: what's been "
                "edited often, what looks unfinished, what could be refactored. "
                "Suggest one concrete improvement based on actual file changes."
            ),
        },
        {
            "name":         "weekly_recap",
            "label":        "Weekly Recap",
            "description":  "Sunday evening: synthesise the week's activity into a recap",
            "enabled":      False,
            "sensors":      ["dream.sensor.memory_recent", "dream.sensor.research_recent",
                             "dream.sensor.notebook_recent"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  18,
            "hours_end":    23,
            "min_idle_minutes":    20,
            "min_interval_minutes": 5760,  # weekly-ish
            "require_signal":       0.3,
            "depth":        "deep",
            "deliver_to":   ["notebook", "memory", "telegram"],
            "sensor_params": {
                "memory_recent":    {"limit": 100},
                "research_recent":  {"limit": 30},
                "notebook_recent":  {"limit": 30},
            },
            "prompt": (
                "Produce a thoughtful weekly recap. Group activity into themes, "
                "identify what made progress, what stalled, what was learned. "
                "End with three things to focus on next week."
            ),
        },
        {
            "name":         "system_health",
            "label":        "System Health Check",
            "description":  "Periodic check of cluster health, slow caps, error patterns",
            "enabled":      False,
            "sensors":      ["dream.sensor.syslog_errors", "dream.sensor.cap_calls"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.propose_action",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  0,
            "hours_end":    24,
            "min_idle_minutes":    10,
            "min_interval_minutes": 360,
            "require_signal":       0.2,
            "depth":        "brief",
            "deliver_to":   ["memory"],
            "sensor_params": {
                "syslog_errors": {"limit": 50},
                "cap_calls":     {"prefix": "", "limit": 100},
            },
            "prompt": (
                "Survey recent system health: error rate, slow caps, anything "
                "looking unhealthy. If there's nothing notable, say 'all systems "
                "nominal' in one line. If there is, propose one concrete fix."
            ),
        },
        {
            "name":         "fabric_digest",
            "label":        "Fabric Digest",
            "description":  "Survey what's new across data fabric datasets",
            "enabled":      False,
            "sensors":      ["dream.sensor.fabric_recent"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  6,
            "hours_end":    22,
            "min_idle_minutes":    30,
            "min_interval_minutes": 480,
            "require_signal":       0.2,
            "depth":        "standard",
            "deliver_to":   ["memory", "notebook"],
            "sensor_params": {"fabric_recent": {"limit": 50}},
            "prompt": (
                "Survey what's new across fabric datasets. Group by dataset, "
                "highlight anything that stands out, suggest one dataset worth "
                "investigating further. Skip if nothing notable."
            ),
        },
        {
            "name":         "memory_consolidation",
            "label":        "Memory Consolidation",
            "description":  "Late-night: identify strong memories, suggest promotions",
            "enabled":      False,
            "sensors":      ["dream.sensor.memory_recent"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.propose_action",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  2,
            "hours_end":    5,
            "min_idle_minutes":    90,
            "min_interval_minutes": 1440,
            "require_signal":       0.2,
            "depth":        "standard",
            "deliver_to":   ["memory"],
            "sensor_params": {"memory_recent": {"limit": 80}},
            "prompt": (
                "Look across recent memories. Identify which ones seem to be "
                "stable, recurring, or central — candidates for promotion to "
                "long-term importance. Identify which ones are stale or "
                "redundant. Suggest specific memory ids for action."
            ),
        },
        {
            "name":         "research_brief",
            "label":        "Research Brief",
            "description":  "Quick-search the web on a topic and add findings to memory",
            "enabled":      False,
            "sensors":      ["dream.sensor.memory_recent"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.stepwise_execute",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "stepwise",
            "hitl":         False,
            "hours_start":  6,
            "hours_end":    22,
            "min_idle_minutes":    20,
            "min_interval_minutes": 720,
            "require_signal":       0.2,
            "depth":        "standard",
            "max_steps":    5,
            "deliver_to":   ["notebook", "memory"],
            "no_hitl_caps": ["research.quick_search", "research.report",
                             "memory.search", "memory.recall", "memory.store"],
            "prompt": (
                "Pick a topic from recent activity that would benefit from a "
                "quick web search. Use research.quick_search to find current info, "
                "summarise key findings, and store the most useful one in memory. "
                "Stop when you have something useful or after 5 steps."
            ),
        },
        {
            "name":         "telegram_digest",
            "label":        "Telegram Digest",
            "description":  "Periodic Telegram-friendly digest — short, actionable",
            "enabled":      False,
            "sensors":      ["dream.sensor.memory_recent",
                             "dream.sensor.fabric_recent",
                             "dream.sensor.research_recent"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  9,
            "hours_end":    21,
            "min_idle_minutes":    45,
            "min_interval_minutes": 360,
            "require_signal":       0.3,
            "depth":        "brief",
            "deliver_to":   ["telegram"],
            "prompt": (
                "Produce a SHORT (3-5 sentences max) digest suitable for Telegram. "
                "Focus on the single most important thing happening. End with one "
                "concrete suggestion or question. Skip silently if nothing rises "
                "above the noise floor."
            ),
        },
        {
            "name":         "ide_session_recap",
            "label":        "IDE Session Recap",
            "description":  "After an IDE coding session, recap what was changed",
            "enabled":      False,
            "sensors":      ["dream.sensor.ide_workspace", "dream.sensor.cap_calls"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.enrich_context",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  0,
            "hours_end":    24,
            "min_idle_minutes":    10,
            "min_interval_minutes": 120,
            "require_signal":       0.3,
            "depth":        "standard",
            "deliver_to":   ["notebook", "memory"],
            "sensor_params": {
                "ide_workspace": {"limit": 30},
                "cap_calls":     {"prefix": "ide.", "limit": 50},
            },
            "prompt": (
                "Recap a recent IDE coding session. List which files changed, "
                "what the apparent goal was, what was completed, what's left "
                "open. Be specific — name actual file paths."
            ),
        },
        {
            "name":         "morning_planner",
            "label":        "Morning Planner",
            "description":  "Early morning: combine overnight news with project state to plan the day",
            "enabled":      False,
            "sensors":      ["dream.sensor.news_overnight",
                             "dream.sensor.memory_recent",
                             "dream.sensor.notebook_recent"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.propose_action",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  6,
            "hours_end":    9,
            "min_idle_minutes":    30,
            "min_interval_minutes": 720,
            "require_signal":       0.2,
            "depth":        "standard",
            "deliver_to":   ["telegram", "notebook", "memory"],
            "prompt": (
                "Produce a morning plan. Open with the most important overnight "
                "thing (news or memory). Then list 3 concrete things to focus on "
                "today, ranked by importance. End with one open question worth "
                "thinking about."
            ),
        },
        {
            "name":         "research_iterate_review",
            "label":        "Iterative Research Review",
            "description":  "Review active iterative research jobs and surface what's converging or stuck",
            "enabled":      False,
            "sensors":      ["dream.sensor.research_recent"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  8,
            "hours_end":    22,
            "min_idle_minutes":    20,
            "min_interval_minutes": 360,
            "require_signal":       0.2,
            "depth":        "standard",
            "deliver_to":   ["notebook", "memory"],
            "sensor_params": {"research_recent": {"limit": 30}},
            "prompt": (
                "Review the most recent research jobs (research.history, "
                "research.iterate.list). For each active iteration, identify what "
                "questions have been answered and which remain open. Suggest the "
                "single most valuable follow-up query. Be concrete — name "
                "specific job IDs and topics."
            ),
        },
        {
            "name":         "deep_research_proposal",
            "label":        "Deep Research Proposal",
            "description":  "When idle for a while, propose ONE deep-research topic worth running",
            "enabled":      False,
            "sensors":      ["dream.sensor.memory_recent",
                             "dream.sensor.notebook_recent",
                             "dream.sensor.research_recent"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.enrich_context",
                             "dream.stage.propose_action",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  10,
            "hours_end":    20,
            "min_idle_minutes":    60,
            "min_interval_minutes": 1440,
            "require_signal":       0.3,
            "depth":        "standard",
            "deliver_to":   ["notebook", "memory"],
            "no_hitl_caps": ["research.quick_search", "memory.search", "memory.recall"],
            "prompt": (
                "Looking at recent activity, identify ONE topic where a deep "
                "research run (research.deep or research.parallel) would yield "
                "useful depth. Draft a tight goal statement and 3-5 specific "
                "sub-questions. Don't run the research — just propose it well."
            ),
        },
        {
            "name":         "code_change_review",
            "label":        "Code Change Review",
            "description":  "Review recent IDE file changes and suggest improvements",
            "enabled":      False,
            "sensors":      ["dream.sensor.ide_workspace",
                             "dream.sensor.cap_calls"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.enrich_context",
                             "dream.stage.synthesize",
                             "dream.stage.quality_check",
                             "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  9,
            "hours_end":    23,
            "min_idle_minutes":    25,
            "min_interval_minutes": 240,
            "require_signal":       0.25,
            "depth":        "standard",
            "deliver_to":   ["notebook", "memory"],
            "sensor_params": {
                "ide_workspace": {"limit": 30},
                "cap_calls":     {"prefix": "ide.fs.write", "limit": 30},
            },
            "no_hitl_caps": ["ide.fs.read", "ide.fs.list", "llm.code_review", "llm.explain"],
            "prompt": (
                "Review files modified in the last few hours (use ide.fs.list / "
                "ide.fs.read). Look for: TODO/FIXME notes, half-finished functions, "
                "obvious code smells, and naming inconsistencies. Suggest the "
                "single most impactful refactor — name the actual file path and "
                "function. If the diff looks healthy, say so in one line."
            ),
        },
        {
            "name":         "memory_cluster_dream",
            "label":        "Memory Cluster Dream",
            "description":  "Find clusters of related memories and synthesise an insight from each",
            "enabled":      False,
            "sensors":      ["dream.sensor.memory_recent"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  1,
            "hours_end":    6,
            "min_idle_minutes":    90,
            "min_interval_minutes": 720,
            "require_signal":       0.3,
            "depth":        "deep",
            "deliver_to":   ["notebook", "memory"],
            "sensor_params": {"memory_recent": {"limit": 200}},
            "prompt": (
                "Look at recent memories as a cluster, not as a sequence. Find "
                "2-3 themes where multiple memories reinforce each other. For each "
                "theme, name the contributing memory ids and write 2-3 sentences "
                "of insight. End with one connection between themes that was not "
                "obvious before."
            ),
        },
        {
            "name":         "fabric_anomaly_watcher",
            "label":        "Fabric Anomaly Watcher",
            "description":  "Notice unusual patterns or sudden volume changes in fabric datasets",
            "enabled":      False,
            "sensors":      ["dream.sensor.fabric_recent"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  0,
            "hours_end":    24,
            "min_idle_minutes":    15,
            "min_interval_minutes": 180,
            "require_signal":       0.3,
            "depth":        "brief",
            "deliver_to":   ["memory"],
            "sensor_params": {"fabric_recent": {"limit": 80}},
            "prompt": (
                "Survey fabric activity. Are any datasets growing unusually fast, "
                "going silent, or producing anomalous content? Be quantitative "
                "where possible (record counts, time gaps). Skip silently if "
                "everything looks normal."
            ),
        },
        {
            "name":         "cap_usage_analytics",
            "label":        "Capability Usage Analytics",
            "description":  "Periodic analysis of which capabilities get called most, which fail, which are slow",
            "enabled":      False,
            "sensors":      ["dream.sensor.cap_calls",
                             "dream.sensor.syslog_errors"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  0,
            "hours_end":    24,
            "min_idle_minutes":    20,
            "min_interval_minutes": 360,
            "require_signal":       0.3,
            "depth":        "brief",
            "deliver_to":   ["memory"],
            "sensor_params": {
                "cap_calls":     {"prefix": "", "limit": 200},
                "syslog_errors": {"limit": 30},
            },
            "prompt": (
                "Analyse recent cap usage. Top 5 most-called caps, top 3 "
                "frequently-failing caps, anything called once and never again. "
                "Suggest one tweak — a cap to memoise, a cap to deprecate, a cap "
                "to instrument better."
            ),
        },
        {
            "name":         "stuck_detector",
            "label":        "Stuck Project Detector",
            "description":  "Detect when work seems stuck — same files edited repeatedly, same errors recurring",
            "enabled":      False,
            "sensors":      ["dream.sensor.cap_calls",
                             "dream.sensor.syslog_errors",
                             "dream.sensor.ide_workspace"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.propose_action",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  10,
            "hours_end":    22,
            "min_idle_minutes":    30,
            "min_interval_minutes": 480,
            "require_signal":       0.4,
            "depth":        "standard",
            "deliver_to":   ["notebook", "memory", "telegram"],
            "sensor_params": {
                "cap_calls":     {"prefix": "", "limit": 100},
                "syslog_errors": {"limit": 30},
                "ide_workspace": {"limit": 30},
            },
            "prompt": (
                "Look for signs of stuckness: same file edited 5+ times in a "
                "session with no apparent progress, the same error recurring, "
                "the same cap failing repeatedly. If found, name the exact "
                "pattern and suggest a different angle to try. If nothing looks "
                "stuck, skip with one line saying so."
            ),
        },
        {
            "name":         "agentic_research_run",
            "label":        "Agentic Research Run",
            "description":  "Stepwise: pick a topic from recent activity, run quick_search, store findings",
            "enabled":      False,
            "sensors":      ["dream.sensor.memory_recent",
                             "dream.sensor.notebook_recent"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.stepwise_execute",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "stepwise",
            "hitl":         True,
            "hours_start":  9,
            "hours_end":    20,
            "min_idle_minutes":    30,
            "min_interval_minutes": 720,
            "require_signal":       0.2,
            "depth":        "standard",
            "max_steps":    6,
            "deliver_to":   ["notebook", "memory"],
            "no_hitl_caps": [
                "memory.search", "memory.recall", "memory.all_nodes",
                "research.quick_search", "research.history",
                "research.db.search", "research.bookmarks",
                "fabric.query", "fabric.datasets",
                "llm.summarize", "llm.qa", "llm.analyze",
            ],
            "prompt": (
                "Pick a topic from recent activity worth investigating. "
                "Step 1: use research.quick_search to gather current info. "
                "Step 2: optionally call llm.summarize on findings. "
                "Step 3: store the most useful insight via memory.search/store. "
                "Stop after 6 steps or when you have a coherent finding."
            ),
        },
        {
            "name":         "project_pulse",
            "label":        "Project Pulse",
            "description":  "Periodic check on every active project's state — surface those needing attention",
            "enabled":      False,
            "sensors":      ["dream.sensor.memory_recent"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  9,
            "hours_end":    18,
            "min_idle_minutes":    30,
            "min_interval_minutes": 1440,
            "require_signal":       0.0,
            "depth":        "standard",
            "deliver_to":   ["notebook", "memory"],
            "prompt": (
                "List all active projects (project.list). For each, note when it "
                "last had a dream cycle, whether its llm_context looks fresh or "
                "stale, and whether it has linked resources. Flag the 1-2 projects "
                "most in need of attention. Do not invent project state — only "
                "use what project.list returns."
            ),
        },
        {
            "name":         "skills_review",
            "label":        "Skills Review",
            "description":  "Review skills used recently — note which work well, which need refinement",
            "enabled":      False,
            "sensors":      ["dream.sensor.cap_calls"],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  20,
            "hours_end":    23,
            "min_idle_minutes":    30,
            "min_interval_minutes": 1440,
            "require_signal":       0.2,
            "depth":        "standard",
            "deliver_to":   ["notebook", "memory"],
            "sensor_params": {"cap_calls": {"prefix": "skills.", "limit": 50}},
            "prompt": (
                "Review skills.* cap usage. Which skills got applied? With what "
                "results? Identify any that produced poor output and suggest a "
                "tweak. Identify any that worked well and could be composed with "
                "other skills."
            ),
        },
        # ── Phase 1 new triggers ──────────────────────────────────────────
        {
            "name":         "source_review",
            "label":        "Source Review — Recent Changes",
            "description":  "On idle, snapshot if source changed, then review the "
                            "changed files and report. Uses the source_review_changes "
                            "composite pipeline.",
            "enabled":      True,
            "pipeline_ref": "source_review_changes",
            "sensors":      ["dream.sensor.source_changes"],
            "hitl":         False,
            "journal":      True,
            "hours_start":  0,
            "hours_end":    24,
            "min_idle_minutes":    60,
            "min_interval_minutes": 720,
            "require_signal":       0.1,
        },
        {
            "name":         "source_review_wander",
            "label":        "Source Review — General Wander",
            "description":  "Periodically roam the whole codebase a window at a "
                            "time, reviewing files regardless of changes.",
            "enabled":      False,
            "pipeline_ref": "source_review_wander",
            "sensors":      ["dream.sensor.source_review_state"],
            "hitl":         False,
            "journal":      True,
            "hours_start":  1,
            "hours_end":    6,
            "min_idle_minutes":    90,
            "min_interval_minutes": 1440,
            "require_signal":       0.0,
        },
        {
            "name":         "source_review_continue",
            "label":        "Source Review — Continue",
            "description":  "Continue a previous review — looks at the actual "
                            "snapshot and last review activity to pick up files not "
                            "yet reviewed.",
            "enabled":      False,
            "pipeline_ref": "source_review_continue",
            "sensors":      ["dream.sensor.source_review_state"],
            "hitl":         False,
            "journal":      True,
            "hours_start":  0,
            "hours_end":    24,
            "min_idle_minutes":    45,
            "min_interval_minutes": 360,
            "require_signal":       0.1,
        },
        {
            "name":         "source_review_deep",
            "label":        "Source Review — Deep (Whole Project)",
            "description":  "In-depth multi-style review of the entire project, "
                            "producing long detailed reports per module/area.",
            "enabled":      False,
            "pipeline_ref": "source_review_deep",
            "sensors":      ["dream.sensor.source_review_state"],
            "hitl":         False,
            "journal":      True,
            "hours_start":  1,
            "hours_end":    7,
            "min_idle_minutes":    120,
            "min_interval_minutes": 2880,
            "require_signal":       0.0,
        },
        {
            "name":         "research_continue",
            "label":        "Research Continuator",
            "description":  "Finds incomplete or stale research jobs and continues them "
                            "using research.expand or starts related follow-ups.",
            "enabled":      True,
            "sensors":      ["dream.sensor.research_recent",
                             "dream.sensor.memory_recent"],
            "pipeline":     ["dream.stage.gather",
                             "dream.stage.themes",
                             "dream.stage.goal_refine",
                             "dream.stage.agent_loop",
                             "dream.stage.synthesize",
                             "dream.stage.deliver"],
            "iterate":      {"enabled": True, "max_iterations": 4, "min_iterations": 1,
                             "convergence_min_new_findings": 1},
            "mode":         "agent_loop",
            "hitl":         False,
            "hours_start":  8,
            "hours_end":    22,
            "min_idle_minutes":    30,
            "min_interval_minutes": 480,
            "require_signal":       0.2,
            "depth":        "standard",
            "max_steps":    6,
            "deliver_to":   ["notebook", "memory"],
            "sensor_params": {
                "research_recent": {"limit": 20, "full_content_top": 3},
            },
            "whitelist": [
                "research.history", "research.db.search", "research.bookmarks",
                "research.job.status", "research.iterate.list",
                "research.quick_search", "research.expand",
                "memory.search", "memory.recall", "memory.create",
                "fabric.query", "fabric.datasets",
                "llm.summarize", "llm.qa", "llm.generate",
            ],
            "no_hitl_caps": [
                "research.history", "research.db.search", "research.job.status",
                "research.iterate.list", "research.bookmarks",
                "research.quick_search", "research.expand",
                "memory.search", "memory.recall",
                "fabric.query", "fabric.datasets",
                "llm.summarize", "llm.qa",
            ],
            "prompt": (
                "Continue unfinished background research. Your goals:\n"
                "1. Use research.history to find recent research jobs.\n"
                "2. Use research.job.status(job_id=...) to read the full content "
                "of the most recent completed job.\n"
                "3. If the job has clear next steps or open questions, use "
                "research.expand(job_id=..., question=...) to continue.\n"
                "4. If no jobs exist or all are too old, use "
                "research.quick_search(query=...) on a topic from recent memory.\n"
                "5. Store a brief research note via memory.create when done.\n\n"
                "IMPORTANT: Do NOT call research.run — it requires specific pipeline "
                "configuration. Use research.quick_search for new searches and "
                "research.expand for continuing existing jobs.\n"
                "Never invent research topics — use only what sensors provide. "
                "If there's nothing to continue, say so and stop."
            ),
        },
        {
            "name":         "memory_gardener",
            "label":        "Memory Gardener",
            "description":  "Maintain memory health: find orphan nodes (no edges), "
                            "sparse clusters that should be connected, and redundant "
                            "records that could be merged.",
            "enabled":      True,
            "sensors":      ["dream.sensor.memory_recent",
                             "dream.sensor.memory_graph_walk"],
            "pipeline":     ["dream.stage.gather",
                             "dream.stage.themes",
                             "dream.stage.goal_refine",
                             "dream.stage.agent_loop",
                             "dream.stage.synthesize",
                             "dream.stage.deliver"],
            "mode":         "agent_loop",
            "hitl":         False,
            "hours_start":  2,
            "hours_end":    6,
            "min_idle_minutes":    60,
            "min_interval_minutes": 1440,
            "require_signal":       0.1,
            "depth":        "brief",
            "max_steps":    6,
            "deliver_to":   ["memory", "notebook"],
            "whitelist": [
                "memory.search", "memory.recall", "memory.similar",
                "memory.traverse", "memory.all_nodes", "memory.create",
                "memory.graph_stats", "memory.stats",
                "llm.generate", "llm.summarize",
            ],
            "no_hitl_caps": [
                "memory.search", "memory.recall", "memory.similar",
                "memory.traverse", "memory.all_nodes", "memory.graph_stats",
                "memory.stats", "llm.summarize",
            ],
            "prompt": (
                "You are a memory gardener. Survey the memory graph for health issues:\n"
                "1. Use memory.graph_stats to get overall counts by category/session.\n"
                "2. Use memory.all_nodes to find nodes with 0 relations (orphans).\n"
                "3. For orphan nodes with meaningful content, use memory.similar to "
                "check if related nodes exist that should be connected.\n"
                "4. If you find nodes that are nearly identical (duplicates), note "
                "their IDs and suggest merging.\n"
                "5. Store a brief 'garden report' via memory.create with category "
                "'maintenance' listing specific node IDs and suggested actions.\n"
                "Be specific — list actual node IDs and categories. Skip if the "
                "graph looks healthy (few orphans, no obvious duplicates)."
            ),
        },
        # ── Phase 2/3 proactive work triggers ─────────────────────────────
        {
            "name":         "content_creator",
            "label":        "Content Creator",
            "description":  "When the fabric has raw ingested content (articles, docs, RSS), "
                            "run entity extraction and loom stitching to build graph "
                            "connections. Turns raw data into structured knowledge.",
            "enabled":      False,
            "sensors":      ["dream.sensor.fabric_recent",
                             "dream.sensor.active_projects"],
            "pipeline":     ["dream.stage.gather",
                             "dream.stage.fabric_explore",
                             "dream.stage.themes",
                             "dream.stage.goal_refine",
                             "dream.stage.agent_loop",
                             "dream.stage.synthesize",
                             "dream.stage.deliver"],
            "mode":         "agent_loop",
            "hitl":         False,
            "hours_start":  0,
            "hours_end":    24,
            "min_idle_minutes":    30,
            "min_interval_minutes": 480,
            "require_signal":       0.2,
            "depth":        "standard",
            "max_steps":    8,
            "deliver_to":   ["memory", "notebook"],
            "whitelist": [
                "fabric.query", "fabric.datasets", "fabric.sources",
                "fabric.entity_graph.extract", "fabric.entity_graph.snapshot",
                "fabric.loom.run", "fabric.ingest",
                "nlp.run", "nlp.modules",
                "memory.search", "memory.create",
                "llm.generate", "llm.summarize",
            ],
            "no_hitl_caps": [
                "fabric.query", "fabric.datasets", "fabric.sources",
                "fabric.entity_graph.snapshot", "fabric.entity_graph.extract",
                "fabric.loom.run", "nlp.run",
                "memory.search", "llm.summarize",
            ],
            "prompt": (
                "You are a content processing agent. The fabric has raw ingested data "
                "that needs entity extraction and graph linking. Your goals:\n"
                "1. Use fabric.datasets to find datasets with recent records.\n"
                "2. Pick the one with the most unprocessed content.\n"
                "3. Run fabric.entity_graph.extract on it to extract entities.\n"
                "4. Run fabric.loom.run to find connections between this dataset "
                "and others.\n"
                "5. Store a brief processing summary in memory.\n"
                "Skip datasets that have already been processed recently. "
                "Focus on datasets with content_type 'text' or 'web'."
            ),
        },
        {
            "name":         "integration_scout",
            "label":        "Integration Scout",
            "description":  "Searches for tools, libraries, or projects that could "
                            "integrate with Vera's existing capabilities. Stores "
                            "findings for later review.",
            "enabled":      False,
            "sensors":      ["dream.sensor.source_changes",
                             "dream.sensor.active_projects"],
            "pipeline":     ["dream.stage.gather",
                             "dream.stage.themes",
                             "dream.stage.goal_refine",
                             "dream.stage.agent_loop",
                             "dream.stage.synthesize",
                             "dream.stage.deliver"],
            "mode":         "agent_loop",
            "hitl":         True,
            "hours_start":  10,
            "hours_end":    20,
            "min_idle_minutes":    60,
            "min_interval_minutes": 1440,
            "require_signal":       0.1,
            "depth":        "standard",
            "max_steps":    6,
            "deliver_to":   ["notebook", "memory"],
            "whitelist": [
                "ide.inspect.source_info", "ide.inspect.list_snapshots",
                "research.quick_search", "research.run",
                "memory.search", "memory.create",
                "fabric.query", "fabric.datasets",
                "llm.generate", "llm.summarize", "llm.qa",
            ],
            "no_hitl_caps": [
                "ide.inspect.source_info", "ide.inspect.list_snapshots",
                "memory.search", "fabric.query", "fabric.datasets",
                "llm.summarize", "llm.qa",
            ],
            "prompt": (
                "You are an integration scout. Survey what capabilities Vera "
                "currently has (use ide.inspect.source_info) and identify one area "
                "that could benefit from a new integration. Then:\n"
                "1. Use research.quick_search to find relevant open-source projects "
                "or Python libraries that could complement existing capabilities.\n"
                "2. Evaluate: would this integration be useful given recent activity "
                "(check active_projects sensor data)?\n"
                "3. Store a concise integration proposal in memory with category "
                "'integration_proposal'.\n"
                "Be specific — name the library, link to it, and explain exactly "
                "which Vera capability it would enhance. Don't propose integrations "
                "for things that already work well."
            ),
        },
        {
            "name":         "activity_summariser",
            "label":        "Activity Summariser",
            "description":  "Summarise recent system activity into a concise digest "
                            "with what happened, what progressed, and what needs attention.",
            "enabled":      True,
            "sensors":      ["dream.sensor.active_projects",
                             "dream.sensor.bus_events",
                             "dream.sensor.memory_recent"],
            "pipeline":     ["dream.stage.gather",
                             "dream.stage.themes",
                             "dream.stage.synthesize",
                             "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  18,
            "hours_end":    23,
            "min_idle_minutes":    20,
            "min_interval_minutes": 720,
            "require_signal":       0.2,
            "depth":        "standard",
            "deliver_to":   ["notebook", "memory", "telegram"],
            "sensor_params": {
                "active_projects": {"hours_back": 12, "top_n": 8},
                "bus_events":      {"limit": 100},
                "memory_recent":   {"limit": 50},
            },
            "prompt": (
                "Write a concise activity digest covering today's work. "
                "The active_projects sensor tells you what areas got attention. "
                "Structure as: what progressed, what stalled, and one suggestion "
                "for tomorrow. Keep it under 200 words. Skip if there was very "
                "little activity."
            ),
        },
    ]


# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE TEMPLATES
# ─────────────────────────────────────────────────────────────────────────────
# Pre-built pipeline configurations for common use cases. Users can import
# these into triggers via the panel or the dream.templates.list / apply API.

PIPELINE_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "passive_report": {
        "label":       "Passive report",
        "description": "Gather sensors, extract themes, write a synthesis. No tools called.",
        "pipeline":    ["dream.stage.gather", "dream.stage.themes",
                        "dream.stage.synthesize", "dream.stage.deliver"],
        "mode":        "synthesize_only",
        "depth":       "standard",
        "max_steps":   0,
    },
    "agentic_investigate": {
        "label":       "Agentic investigation",
        "description": "Gather, refine goal, run agent loop, synthesize. The workhorse pipeline.",
        "pipeline":    ["dream.stage.gather", "dream.stage.themes",
                        "dream.stage.goal_refine", "dream.stage.agent_loop",
                        "dream.stage.synthesize", "dream.stage.deliver"],
        "mode":        "agent_loop",
        "depth":       "standard",
        "max_steps":   6,
    },
    "deep_research": {
        "label":       "Deep research with iteration",
        "description": "Iterative investigation loop — runs agent_loop multiple times, "
                       "converging when no new findings emerge.",
        "pipeline":    ["dream.stage.gather", "dream.stage.themes",
                        "dream.stage.goal_refine", "dream.stage.investigate",
                        "dream.stage.synthesize", "dream.stage.deliver"],
        "mode":        "stepwise",
        "depth":       "deep",
        "max_steps":   8,
        "iterate":     {"enabled": True, "max_iterations": 6, "min_iterations": 2,
                        "convergence_min_new_findings": 1},
    },
    "enriched_synthesis": {
        "label":       "Enriched synthesis",
        "description": "Passive report enhanced with an enrichment stage that fetches "
                       "missing context from memory/fabric/web before writing.",
        "pipeline":    ["dream.stage.gather", "dream.stage.themes",
                        "dream.stage.enrich_context", "dream.stage.propose_action",
                        "dream.stage.synthesize", "dream.stage.quality_check",
                        "dream.stage.deliver"],
        "mode":        "synthesize_only",
        "depth":       "standard",
        "max_steps":   0,
    },
    "code_review": {
        "label":       "Source code review",
        "description": "Snapshot source, review changed files, store findings.",
        "pipeline":    ["dream.stage.gather", "dream.stage.themes",
                        "dream.stage.goal_refine", "dream.stage.agent_loop",
                        "dream.stage.synthesize", "dream.stage.deliver"],
        "mode":        "agent_loop",
        "depth":       "standard",
        "max_steps":   5,
        "sensors":     ["dream.sensor.source_changes"],
    },
    "memory_maintenance": {
        "label":       "Memory maintenance",
        "description": "Graph walk, find orphans, propose connections and cleanup.",
        "pipeline":    ["dream.stage.gather", "dream.stage.themes",
                        "dream.stage.goal_refine", "dream.stage.agent_loop",
                        "dream.stage.synthesize", "dream.stage.deliver"],
        "mode":        "agent_loop",
        "depth":       "brief",
        "max_steps":   6,
        "sensors":     ["dream.sensor.memory_graph_walk", "dream.sensor.memory_recent"],
    },
    "project_action": {
        "label":       "Project action",
        "description": "Full project automation — gather context, refine goal, "
                       "EXECUTE the next step (not just propose), synthesize findings.",
        "pipeline":    ["dream.stage.gather", "dream.stage.themes",
                        "dream.stage.goal_refine",
                        "dream.stage.project_action",
                        "dream.stage.synthesize", "dream.stage.deliver"],
        "mode":        "agent_loop",
        "depth":       "standard",
        "max_steps":   8,
    },
    "deep_exploration": {
        "label":       "Deep memory + fabric exploration",
        "description": "Deep graph traversal + fabric entity analysis before "
                       "agent loop. Finds orphans, clusters, unprocessed datasets.",
        "pipeline":    ["dream.stage.gather",
                        "dream.stage.memory_deep_traverse",
                        "dream.stage.fabric_explore",
                        "dream.stage.themes",
                        "dream.stage.goal_refine",
                        "dream.stage.agent_loop",
                        "dream.stage.synthesize", "dream.stage.deliver"],
        "mode":        "agent_loop",
        "depth":       "deep",
        "max_steps":   8,
    },
}


@capability(
    "dream.templates.list", memory="off", silent=True,
    http_method="GET", http_path="/dream/templates", http_tags=["dream"],
    description="List available pipeline templates for dream triggers.",
)
async def dream_templates_list(trace_id=None):
    return {
        "templates": {
            k: {
                "id":          k,
                "label":       v["label"],
                "description": v["description"],
                "pipeline":    v["pipeline"],
                "mode":        v.get("mode"),
                "depth":       v.get("depth"),
                "max_steps":   v.get("max_steps"),
                "iterate":     v.get("iterate"),
            }
            for k, v in PIPELINE_TEMPLATES.items()
        },
        "count": len(PIPELINE_TEMPLATES),
    }


@capability(
    "dream.templates.apply", memory="off",
    http_method="POST", http_path="/dream/templates/apply", http_tags=["dream"],
    description="Apply a pipeline template to an existing trigger. Overwrites the "
                "trigger's pipeline, mode, depth, and max_steps with the template's "
                "values. Preserves all other trigger settings (schedule, sensors, "
                "prompt, whitelist, etc). "
                "Inputs: trigger_name (str!), template_id (str!).",
)
async def dream_templates_apply(
    trigger_name: str, template_id: str, trace_id=None,
):
    if template_id not in PIPELINE_TEMPLATES:
        return {"ok": False, "error": f"unknown template: {template_id}",
                "available": list(PIPELINE_TEMPLATES.keys())}
    trig = await _get_trigger(trigger_name)
    if not trig:
        return {"ok": False, "error": f"trigger not found: {trigger_name}"}

    tmpl = PIPELINE_TEMPLATES[template_id]
    trig["pipeline"] = tmpl["pipeline"]
    if tmpl.get("mode"):      trig["mode"] = tmpl["mode"]
    if tmpl.get("depth"):     trig["depth"] = tmpl["depth"]
    if tmpl.get("max_steps") is not None: trig["max_steps"] = tmpl["max_steps"]
    if tmpl.get("iterate"):   trig["iterate"] = tmpl["iterate"]
    if tmpl.get("sensors"):   trig["sensors"] = tmpl["sensors"]

    await _save_trigger(trig)
    return {"ok": True, "trigger": trigger_name, "template": template_id,
            "pipeline": trig["pipeline"], "mode": trig.get("mode")}


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _redis():
    return _orch.REDIS


def _fabric():
    return sys.modules.get("data_fabric")


def _build_datetime_context() -> str:
    """Return a short date/time preamble to ground every LLM call in the present."""
    now_local = datetime.now()
    now_utc   = datetime.now(timezone.utc)
    return (
        f"Current date and time: {now_local.strftime('%A, %d %B %Y %H:%M')} (local) / "
        f"{now_utc.strftime('%Y-%m-%d %H:%M UTC')}\n"
    )


async def _llm_generate(prompt: str, system: str = "", prefer_gpu: bool = True) -> str:
    try:
        fn = getattr(_orch, "ollama_generate", None)
        if not fn:
            return ""
        full_system = _build_datetime_context() + (("\n" + system) if system else "")
        out = await fn(prompt, system=full_system, prefer_gpu=prefer_gpu)
        return str(out or "")
    except Exception as e:
        log.debug("dream llm.generate: %s", e)
        return ""


async def _llm_generate_streaming(
    prompt: str,
    system: str = "",
    prefer_gpu: bool = True,
    cycle_id: str = "",
    stage: str = "",
    flush_every: int = 8,
) -> str:
    """
    Like _llm_generate but streams tokens to two separate channels:
      1. Redis pub/sub  vera:dream:tokens:{cycle_id}  — raw token strings,
         ultra-low latency, for WebSocket subscribers in the panel.
      2. Redis list    (KEY_LLM_TOKENS:{cycle_id})    — ring buffer, for
         late-joining pollers (dream.llm.tokens cap).
    Only two structured events go to the main event bus:
      dream.llm.start   — fired once before generation begins
      dream.llm.complete — fired once when generation ends, carries char count
    This avoids flooding the main bus with thousands of per-token events.
    """
    fn = getattr(_orch, "ollama_generate", None)
    if not fn:
        return ""

    full_system = _build_datetime_context() + (("\n" + system) if system else "")

    r = _redis()
    buf_key    = f"{KEY_LLM_TOKENS}:{cycle_id}" if cycle_id else None
    pub_key    = f"vera:dream:tokens:{cycle_id}" if cycle_id else None
    chunks: List[str] = []
    pending: List[str] = []

    async def _emit(tok: str):
        chunks.append(tok)
        pending.append(tok)
        # Publish raw token to dedicated pub/sub channel (no JSON overhead)
        if r and pub_key:
            try:
                await r.publish(pub_key, tok)
            except Exception:
                pass
        # Batch-flush to ring buffer every flush_every tokens
        if r and buf_key and len(pending) >= max(1, int(flush_every)):
            try:
                pipe = r.pipeline()
                for t in pending:
                    pipe.rpush(buf_key, t)
                pipe.ltrim(buf_key, -2000, -1)
                pipe.expire(buf_key, 3600)
                await pipe.execute()
            except Exception:
                pass
            pending.clear()

    try:
        # Structured start event on main bus
        await emit_event({
            "type":     "dream.llm.start",
            "cycle_id": cycle_id,
            "stage":    stage,
        })
        if r and buf_key:
            try:
                await r.delete(buf_key)
            except Exception:
                pass

        out = await fn(prompt, system=full_system, prefer_gpu=prefer_gpu, stream_cb=_emit)

        # Final flush of any remaining buffered tokens
        if r and buf_key and pending:
            try:
                pipe = r.pipeline()
                for t in pending:
                    pipe.rpush(buf_key, t)
                pipe.ltrim(buf_key, -2000, -1)
                pipe.expire(buf_key, 3600)
                await pipe.execute()
            except Exception:
                pass

        # Structured complete event on main bus
        await emit_event({
            "type":     "dream.llm.complete",
            "cycle_id": cycle_id,
            "stage":    stage,
            "chars":    len(out or ""),
        })
        return str(out or "")
    except Exception as e:
        log.debug("dream llm.streaming: %s", e)
        await emit_event({
            "type":     "dream.llm.error",
            "cycle_id": cycle_id,
            "stage":    stage,
            "error":    str(e),
        })
        return "".join(chunks)


async def _call_cap(name: str, **kwargs) -> Any:
    """
    Call a capability by name. Robust to several common name forms:
      - exact match in CAPABILITY_REGISTRY
      - short sensor id (e.g. 'memory_recent') → tries 'dream.sensor.memory_recent'
      - short stage id (e.g. 'gather') → tries 'dream.stage.gather'
    Returns the cap result, or {"error": "..."} on failure.
    """
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap:
        # Try with dream.sensor. prefix (handles legacy short-form names)
        for prefix in ("dream.sensor.", "dream.stage.", "dream."):
            alt = f"{prefix}{name}"
            if alt in CAPABILITY_REGISTRY:
                cap = CAPABILITY_REGISTRY[alt]
                name = alt
                break
    if not cap:
        return {"error": f"unknown_cap:{name}"}
    try:
        accepted = set(cap.get("schema", {}).get("properties", {}).keys())
        filtered = {k: v for k, v in kwargs.items() if k in accepted}
        return await cap["func"](**filtered)
    except Exception as e:
        log.debug("dream _call_cap %s: %s", name, e)
        return {"error": f"{type(e).__name__}: {e}"}


def _within_hours(h_start: int, h_end: int, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    h = now.hour
    if h_start == h_end:
        return True
    if h_start < h_end:
        return h_start <= h < h_end
    return h >= h_start or h < h_end


async def _get_config() -> Dict[str, Any]:
    r = _redis()
    if not r:
        return dict(DEFAULT_CONFIG)
    try:
        raw = await r.get(KEY_CONFIG)
        if not raw:
            return dict(DEFAULT_CONFIG)
        data = json.loads(raw)
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        return merged
    except Exception:
        return dict(DEFAULT_CONFIG)


async def _save_config(cfg: Dict[str, Any]):
    r = _redis()
    if r:
        try:
            await r.set(KEY_CONFIG, json.dumps(cfg))
        except Exception as e:
            log.warning("dream save config: %s", e)


def _migrate_trigger_sensors(trig: Dict[str, Any]) -> Dict[str, Any]:
    """
    One-time migration: ensure trigger sensor names are full ids.
    Older versions of the panel saved 'memory_recent' instead of 'dream.sensor.memory_recent'.
    """
    if not isinstance(trig, dict):
        return trig
    sensors = trig.get("sensors") or []
    fixed = []
    changed = False
    for s in sensors:
        if not isinstance(s, str):
            continue
        if s.startswith("dream.sensor.") or s.startswith("custom."):
            fixed.append(s)
        else:
            full = f"dream.sensor.{s}"
            fixed.append(full)
            changed = True
    if changed:
        trig["sensors"] = fixed
    # Same for pipeline stages
    pipe = trig.get("pipeline") or []
    fixed_pipe = []
    pipe_changed = False
    for p in pipe:
        if not isinstance(p, str):
            continue
        if p.startswith("dream.stage.") or p.startswith("custom."):
            fixed_pipe.append(p)
        else:
            full = f"dream.stage.{p}"
            fixed_pipe.append(full)
            pipe_changed = True
    if pipe_changed:
        trig["pipeline"] = fixed_pipe
    return trig


async def _list_triggers() -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return []
    try:
        items = await r.hgetall(KEY_TRIGGERS)
        out = []
        for _, v in (items or {}).items():
            try:
                out.append(_migrate_trigger_sensors(
                    json.loads(v.decode() if isinstance(v, bytes) else v)))
            except Exception:
                continue
        out.sort(key=lambda t: t.get("name", ""))
        return out
    except Exception as e:
        log.warning("dream list triggers: %s", e)
        return []


async def _get_trigger(name: str) -> Optional[Dict[str, Any]]:
    r = _redis()
    if not r:
        return None
    try:
        v = await r.hget(KEY_TRIGGERS, name)
        if not v:
            return None
        return _migrate_trigger_sensors(
            json.loads(v.decode() if isinstance(v, bytes) else v))
    except Exception:
        return None


async def _save_trigger(trig: Dict[str, Any]):
    r = _redis()
    if r:
        try:
            await r.hset(KEY_TRIGGERS, trig["name"], json.dumps(trig))
        except Exception as e:
            log.warning("dream save trigger: %s", e)


async def _delete_trigger(name: str):
    r = _redis()
    if r:
        try:
            await r.hdel(KEY_TRIGGERS, name)
        except Exception:
            pass


async def _get_whitelist() -> List[str]:
    r = _redis()
    if not r:
        return list(DEFAULT_WHITELIST)
    try:
        items = await r.smembers(KEY_WHITELIST)
        if not items:
            return []
        return sorted(
            (i.decode() if isinstance(i, bytes) else str(i)) for i in items
        )
    except Exception:
        return list(DEFAULT_WHITELIST)


async def _set_whitelist(caps: List[str]):
    r = _redis()
    if not r:
        return
    try:
        await r.delete(KEY_WHITELIST)
        if caps:
            await r.sadd(KEY_WHITELIST, *caps)
    except Exception as e:
        log.warning("dream save whitelist: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# AGENT-LOOP SETTINGS — global + per-trigger/pipeline, like the DAG system
# ─────────────────────────────────────────────────────────────────────────────

async def _get_global_loop_settings() -> Dict[str, Any]:
    """Global defaults merged with any stored override in Redis."""
    out = dict(DEFAULT_LOOP_SETTINGS)
    r = _redis()
    if r:
        try:
            raw = await r.get(KEY_LOOP_SETTINGS)
            if raw:
                stored = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                if isinstance(stored, dict):
                    out.update({k: v for k, v in stored.items()
                                if k in DEFAULT_LOOP_SETTINGS})
        except Exception as e:
            log.debug("dream loop settings load: %s", e)
    return out


async def _resolve_loop_settings(trig: Optional[Dict[str, Any]],
                                 state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve effective loop settings with precedence (low→high):
    global default < per-trigger/pipeline < project < seed."""
    s = await _get_global_loop_settings()
    trig = trig or {}
    seed = (state or {}).get("seed") or {}
    for layer in (
        trig.get("loop_settings"),
        seed.get("project_loop_settings"),
        seed.get("loop_settings"),
    ):
        if isinstance(layer, dict):
            s.update({k: v for k, v in layer.items() if k in DEFAULT_LOOP_SETTINGS})
    # Legacy bridge: a trigger's iterate.max_iterations still sets max_cycles
    # unless the trigger explicitly overrides it via loop_settings.
    iter_cfg = trig.get("iterate") or {}
    if iter_cfg.get("max_iterations") and \
            "max_cycles" not in (trig.get("loop_settings") or {}):
        try:
            s["max_cycles"] = int(iter_cfg["max_iterations"])
        except Exception:
            pass
    # Honour the global prefer_gpu config flag unless overridden.
    return s


def _loop_kwargs_for(cap_func, settings: Dict[str, Any], **overrides) -> Dict[str, Any]:
    """Build kwargs for whichever loop cap (v1/v2/openclaw) is resolved, passing
    only the parameters that cap actually accepts so we never raise TypeError on
    a setting the variant doesn't support."""
    import inspect
    try:
        accepted = set(inspect.signature(cap_func).parameters.keys())
    except (TypeError, ValueError):
        accepted = set(settings.keys()) | set(overrides.keys())
    merged = {**settings, **overrides}
    return {k: v for k, v in merged.items() if k in accepted}


@capability(
    "dream.loop.settings.get", memory="off", silent=True,
    http_method="GET", http_path="/dream/loop/settings", http_tags=["dream"],
    description="Get the global dream agent-loop settings (defaults merged with "
                "stored overrides). Output: {settings, defaults, fields}.",
)
async def dream_loop_settings_get(trace_id=None):
    return {
        "settings": await _get_global_loop_settings(),
        "defaults": dict(DEFAULT_LOOP_SETTINGS),
        "fields":   list(DEFAULT_LOOP_SETTINGS.keys()),
    }


@capability(
    "dream.loop.settings.set", memory="off",
    http_method="POST", http_path="/dream/loop/settings/set", http_tags=["dream"],
    description="Update global dream agent-loop settings. Accepts any subset of the "
                "known fields (unknown keys ignored). Pass reset=true to restore "
                "defaults. These apply to every dream loop unless a trigger overrides "
                "them via its loop_settings field. "
                "Input: settings (JSON object), reset (bool). "
                "Output: {ok, settings, applied}.",
)
async def dream_loop_settings_set(
    settings: Optional[Dict[str, Any]] = None,
    reset: bool = False,
    trace_id=None,
):
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}
    if reset:
        try:
            await r.delete(KEY_LOOP_SETTINGS)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "settings": dict(DEFAULT_LOOP_SETTINGS), "reset": True}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings) if settings.strip() else {}
        except Exception:
            settings = {}
    cur = await _get_global_loop_settings()
    clean = {k: v for k, v in (settings or {}).items() if k in DEFAULT_LOOP_SETTINGS}
    cur.update(clean)
    try:
        await r.set(KEY_LOOP_SETTINGS, json.dumps(cur))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    await emit_event({"type": "dream.loop.settings.updated",
                      "applied": list(clean.keys()), "ts": now_iso()})
    return {"ok": True, "settings": cur, "applied": list(clean.keys())}


# ─────────────────────────────────────────────────────────────────────────────
# DREAM JOURNAL — a running, incrementally-updated log of a dream's thinking.
# Most stages append to it as they go; the agent loop can append its own
# thoughts via dream.journal.append. Backed by a capped Redis list per journal,
# with a registry set so journals are listable. journal_id defaults to the
# cycle_id, but project dreams use "project:<slug>" so the journal persists
# across cycles for that project.
# ─────────────────────────────────────────────────────────────────────────────

KEY_JOURNAL_PREFIX = "vera:dream:journal:"     # + journal_id  -> Redis list
KEY_JOURNAL_INDEX  = "vera:dream:journals"     # hash: journal_id -> meta JSON
_JOURNAL_MAX_ENTRIES = 500
_JOURNAL_TTL_SECS    = 30 * 86400


def _journal_key(journal_id: str) -> str:
    return KEY_JOURNAL_PREFIX + (journal_id or "default")


async def _journal_append(
    journal_id: str,
    text: str,
    kind: str = "note",
    stage: str = "",
    title: str = "",
    data: Optional[Dict[str, Any]] = None,
    emit: bool = True,
) -> Dict[str, Any]:
    """Append one entry to a dream journal. Safe to call from anywhere — never
    raises. Returns the stored entry (or {} if storage unavailable)."""
    entry = {
        "ts":    now_iso(),
        "kind":  kind,            # note | stage | finding | review | plan | action | pivot | thought
        "stage": stage,
        "title": (title or "")[:200],
        "text":  (text or "")[:4000],
        "data":  data or {},
    }
    r = _redis()
    if not r:
        return entry
    try:
        key = _journal_key(journal_id)
        await r.rpush(key, json.dumps(entry, default=str))
        await r.ltrim(key, -_JOURNAL_MAX_ENTRIES, -1)
        await r.expire(key, _JOURNAL_TTL_SECS)
        # Update index meta
        try:
            meta_raw = await r.hget(KEY_JOURNAL_INDEX, journal_id)
            meta = json.loads(meta_raw) if meta_raw else {}
        except Exception:
            meta = {}
        meta["journal_id"] = journal_id
        meta["updated"] = entry["ts"]
        meta["entries"] = int(meta.get("entries", 0)) + 1
        meta.setdefault("created", entry["ts"])
        await r.hset(KEY_JOURNAL_INDEX, journal_id, json.dumps(meta, default=str))
    except Exception as e:
        log.debug("journal append: %s", e)
    if emit:
        try:
            await emit_event({
                "type": "dream.journal.entry", "journal_id": journal_id,
                "kind": kind, "stage": stage, "title": entry["title"],
                "preview": entry["text"][:160],
            })
        except Exception:
            pass
    return entry


async def _journal_read(journal_id: str, limit: int = 100,
                        kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return []
    try:
        raw = await r.lrange(_journal_key(journal_id), -int(limit or 100), -1)
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for item in raw or []:
        try:
            e = json.loads(item.decode() if isinstance(item, bytes) else item)
            if kinds and e.get("kind") not in kinds:
                continue
            out.append(e)
        except Exception:
            continue
    return out


def _journal_to_markdown(entries: List[Dict[str, Any]], heading: str = "") -> str:
    lines: List[str] = []
    if heading:
        lines.append(f"## {heading}\n")
    for e in entries:
        ts = (e.get("ts") or "")[11:19]  # HH:MM:SS
        kind = e.get("kind", "note")
        title = e.get("title") or kind
        lines.append(f"- `{ts}` **{title}**" + (f" _({e.get('stage')})_" if e.get("stage") else ""))
        body = (e.get("text") or "").strip()
        if body and body != title:
            for bl in body.splitlines():
                lines.append(f"    {bl}")
    return "\n".join(lines)


def _stage_journal_note(short: str, state: Dict[str, Any]) -> str:
    """Best-effort one-line summary of what a stage just did, for the journal."""
    try:
        if short == "gather":
            g = state.get("gather") or {}
            return (f"Gathered {g.get('total_items', g.get('count', 0))} items "
                    f"(signal {float(g.get('signal', 0) or 0):.2f}).")
        if short == "themes":
            return f"Themes: {', '.join((state.get('themes') or [])[:6]) or '(none)'}."
        if short == "snapshot_source":
            s = state.get("snapshot") or {}
            return (f"Snapshot {s.get('snapshot_id','?')}: "
                    f"{s.get('count', 0)} review candidate(s).")
        if short == "goal_refine":
            return f"Refined goal: {(state.get('refined_goal') or '')[:200]}"
        if short in ("investigate", "agent_loop", "stepwise_execute"):
            return f"Findings so far: {len(state.get('findings') or [])}."
        if short == "project_action":
            pa = state.get("project_action") or {}
            return (f"Action: {(pa.get('goal') or '')[:160]} — "
                    f"{'ok' if pa.get('ok') else 'see result'}.")
        if short == "review_codebase":
            rv = state.get("review") or {}
            return (f"Reviewed {rv.get('files_reviewed', 0)} files, "
                    f"{rv.get('total_issues', 0)} issues.")
        if short == "synthesize":
            return f"Report: {(state.get('title') or '')[:160]}"
        if short == "deliver":
            return f"Delivered to: {', '.join((state.get('delivered') or {}).keys()) or '(none)'}."
    except Exception:
        pass
    return f"{short} complete."


@capability(
    "dream.journal.append", memory="off", silent=True,
    http_method="POST", http_path="/dream/journal/append", http_tags=["dream"],
    description="Append a thought/observation to a dream journal as you work. Use "
                "this to record reasoning, findings, decisions, and next steps "
                "incrementally so the journal builds a coherent narrative. "
                "Input: journal_id (str — usually the cycle_id or project:<slug>), "
                "text (str!), kind (note|finding|review|plan|action|thought), "
                "stage (str), title (str). Output: {ok, entry}.",
)
async def dream_journal_append(
    journal_id: str = "",
    text: str = "",
    kind: str = "thought",
    stage: str = "",
    title: str = "",
    trace_id=None,
):
    if not (text or "").strip():
        return {"ok": False, "error": "empty text"}
    entry = await _journal_append(journal_id or "default", text, kind=kind,
                                  stage=stage, title=title)
    return {"ok": True, "entry": entry}


@capability(
    "dream.journal.read", memory="off", silent=True,
    http_method="GET", http_path="/dream/journal/read", http_tags=["dream"],
    description="Read a dream journal. Input: journal_id (str!), limit (int, "
                "default 100), kinds (comma-sep filter), as_markdown (bool). "
                "Output: {ok, journal_id, count, entries|markdown}.",
)
async def dream_journal_read(
    journal_id: str,
    limit: int = 100,
    kinds: str = "",
    as_markdown: bool = False,
    trace_id=None,
):
    kind_list = [k.strip() for k in (kinds or "").split(",") if k.strip()] or None
    entries = await _journal_read(journal_id, limit=limit, kinds=kind_list)
    out = {"ok": True, "journal_id": journal_id, "count": len(entries)}
    if as_markdown:
        out["markdown"] = _journal_to_markdown(entries, heading=f"Journal — {journal_id}")
    else:
        out["entries"] = entries
    return out


@capability(
    "dream.journal.list", memory="off", silent=True,
    http_method="GET", http_path="/dream/journal/list", http_tags=["dream"],
    description="List known dream journals with entry counts and last-updated. "
                "Output: {ok, journals: [{journal_id, entries, created, updated}]}.",
)
async def dream_journal_list(trace_id=None):
    r = _redis()
    if not r:
        return {"ok": True, "journals": []}
    try:
        h = await r.hgetall(KEY_JOURNAL_INDEX)
    except Exception:
        return {"ok": True, "journals": []}
    journals: List[Dict[str, Any]] = []
    for k, v in (h or {}).items():
        try:
            journals.append(json.loads(v.decode() if isinstance(v, bytes) else v))
        except Exception:
            continue
    journals.sort(key=lambda m: m.get("updated", ""), reverse=True)
    return {"ok": True, "journals": journals}


@capability(
    "dream.journal.clear", memory="off",
    http_method="POST", http_path="/dream/journal/clear", http_tags=["dream"],
    description="Delete a dream journal and its index entry. Input: journal_id (str!).",
)
async def dream_journal_clear(journal_id: str, trace_id=None):
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}
    try:
        await r.delete(_journal_key(journal_id))
        await r.hdel(KEY_JOURNAL_INDEX, journal_id)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "journal_id": journal_id, "cleared": True}


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE REVIEW HELPERS — deterministic snapshot resolution + file selection
# ─────────────────────────────────────────────────────────────────────────────

KEY_WANDER_CURSOR  = "vera:dream:review:wander_cursor"     # int index into module list
KEY_REVIEWED_FILES = "vera:dream:review:reviewed:"         # + snapshot_id -> Redis set


async def _resolve_review_snapshot(label: str = "dream-review") -> Dict[str, Any]:
    """Deterministically resolve the snapshot to analyse.

    - Find the most recent snapshot and the current source hash.
    - If the source has changed since that snapshot (or none exists), create a
      fresh snapshot and remember the previous one as the diff baseline.
    - Otherwise reuse the most recent snapshot (no changes since).

    Returns {snapshot_id, baseline_id, created, current_hash, prev}.
    """
    out = {"snapshot_id": None, "baseline_id": None, "created": False,
           "current_hash": "", "prev": None}
    list_cap = CAPABILITY_REGISTRY.get("ide.inspect.list_snapshots")
    snap_cap = CAPABILITY_REGISTRY.get("ide.inspect.snapshot")
    snaps: List[Dict[str, Any]] = []
    cur_hash = ""
    if list_cap:
        try:
            res = await list_cap["func"]() or {}
            snaps = res.get("snapshots", []) or []
            cur_hash = res.get("current_source_hash", "")
        except Exception as e:
            log.debug("resolve snapshot list: %s", e)
    out["current_hash"] = cur_hash
    newest = snaps[0] if snaps else None
    out["prev"] = newest

    # "changes" = the newest snapshot's hash differs from live (or it isn't fresh)
    has_changes = (newest is None
                   or newest.get("source_hash") != cur_hash
                   or not newest.get("is_fresh", False))

    if has_changes and snap_cap:
        try:
            created = await snap_cap["func"](label=label) or {}
            if created.get("snapshot_id"):
                out["snapshot_id"] = created["snapshot_id"]
                out["created"] = True
                out["baseline_id"] = newest["id"] if newest else None
                return out
        except Exception as e:
            log.debug("resolve snapshot create: %s", e)

    # No changes (or snapshot cap unavailable): reuse the most recent
    if newest:
        out["snapshot_id"] = newest["id"]
        out["baseline_id"] = snaps[1]["id"] if len(snaps) > 1 else None
    return out


async def _all_source_modules() -> List[Dict[str, Any]]:
    cap = CAPABILITY_REGISTRY.get("ide.inspect.source_info")
    if not cap:
        return []
    try:
        info = await cap["func"]() or {}
        return list(info.get("modules", []))
    except Exception:
        return []


async def _select_review_files(review_type: str, snap: Dict[str, Any],
                               max_files: int) -> List[Dict[str, Any]]:
    """Choose which files to review based on the review type:
       changes   — files that differ from the diff baseline (recent changes)
       wander    — a rotating window across the whole codebase
       continue  — files not yet reviewed against this snapshot
    """
    snapshot_id = snap.get("snapshot_id")
    r = _redis()
    out: List[Dict[str, Any]] = []

    if review_type == "changes":
        for f in (snap.get("changed_files") or [])[:max_files]:
            out.append({"file": f, "snapshot_id": snapshot_id, "has_diff": True})
        return out

    modules = await _enumerate_source_files(snapshot_id)
    names = [m["rel"] for m in modules]

    if review_type == "continue":
        reviewed: set = set()
        if r and snapshot_id:
            try:
                raw = await r.smembers(KEY_REVIEWED_FILES + snapshot_id)
                reviewed = {(x.decode() if isinstance(x, bytes) else x) for x in (raw or [])}
            except Exception:
                reviewed = set()
        # Prefer changed files first, then unreviewed modules
        ordered = list(snap.get("changed_files") or []) + names
        for f in ordered:
            if f in reviewed or any(c["file"] == f for c in out):
                continue
            out.append({"file": f, "snapshot_id": snapshot_id, "has_diff": False})
            if len(out) >= max_files:
                break
        return out

    # wander: rotating window across all modules
    cursor = 0
    if r:
        try:
            raw = await r.get(KEY_WANDER_CURSOR)
            cursor = int(raw) if raw else 0
        except Exception:
            cursor = 0
    if names:
        cursor %= len(names)
        window = (names[cursor:] + names[:cursor])[:max_files]
        for f in window:
            out.append({"file": f, "snapshot_id": snapshot_id, "has_diff": False})
        if r:
            try:
                await r.set(KEY_WANDER_CURSOR, (cursor + len(window)) % max(1, len(names)))
            except Exception:
                pass
    return out


async def _mark_reviewed(snapshot_id: str, files: List[str]):
    r = _redis()
    if not (r and snapshot_id and files):
        return
    try:
        await r.sadd(KEY_REVIEWED_FILES + snapshot_id, *files)
        await r.expire(KEY_REVIEWED_FILES + snapshot_id, _JOURNAL_TTL_SECS)
    except Exception:
        pass


async def _push_history(record: Dict[str, Any]):
    r = _redis()
    if not r:
        return
    try:
        await r.lpush(KEY_HISTORY, json.dumps(record))
        await r.ltrim(KEY_HISTORY, 0, HISTORY_CAP - 1)
    except Exception as e:
        log.debug("dream history: %s", e)


async def _get_history(limit: int = 50) -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return []
    try:
        rows = await r.lrange(KEY_HISTORY, 0, limit - 1)
        out = []
        for row in rows or []:
            try:
                out.append(json.loads(row.decode() if isinstance(row, bytes) else row))
            except Exception:
                continue
        return out
    except Exception:
        return []


async def _set_running(info: Optional[Dict[str, Any]]):
    r = _redis()
    if not r:
        return
    try:
        if info:
            await r.set(KEY_RUNNING, json.dumps(info))
        else:
            await r.delete(KEY_RUNNING)
    except Exception:
        pass


async def _get_running() -> Optional[Dict[str, Any]]:
    r = _redis()
    if not r:
        return None
    try:
        raw = await r.get(KEY_RUNNING)
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        return None


async def _last_run_ts(trigger_name: str) -> Optional[str]:
    r = _redis()
    if not r:
        return None
    try:
        v = await r.hget(KEY_LAST_RUN, trigger_name)
        return v.decode() if isinstance(v, bytes) else v
    except Exception:
        return None


async def _mark_trigger_run(trigger_name: str):
    r = _redis()
    if r:
        try:
            await r.hset(KEY_LAST_RUN, trigger_name, now_iso())
        except Exception:
            pass


async def _idle_minutes() -> float:
    r = _redis()
    if not r:
        return 0.0
    try:
        # Get configured reset prefixes (allowlist approach — only these count as activity)
        cfg = await _get_config()
        reset_prefixes = cfg.get("idle_reset_prefixes") or DEFAULT_IDLE_RESET_PREFIXES

        rows = await r.zrevrange(KEY_RECENT_CAPS, 0, 120, withscores=True)
        now_ts = time.time()
        for raw, score in rows or []:
            try:
                rec = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                name = rec.get("name", "")
            except Exception:
                continue
            # Only count caps whose prefix is in the reset list
            if any(name.startswith(p) for p in reset_prefixes):
                return max(0.0, (now_ts - float(score)) / 60.0)
        return 99999.0
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# SENSORS
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.sensor.memory_recent", memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/memory_recent",
    http_tags=["dream", "sensor"],
    description="Sample recent memory records — wander-friendly signal for the dream pipeline.",
)
async def dream_sensor_memory_recent(limit: int = 30, trace_id=None):
    """
    Pull recent memory records. Prefers memory.all_nodes (chronological) since
    memory.search with empty query produces no embedding and returns nothing.
    Falls back to memory.session_history then memory.search if all_nodes is unavailable.
    """
    records: List[Any] = []
    last_err: str = ""
    # Strategy 1: all_nodes — true "recent" by created_at desc
    try:
        result = await _call_cap("memory.all_nodes", limit=int(limit))
        if isinstance(result, dict):
            records = result.get("nodes") or result.get("records") or []
    except Exception as e:
        last_err = str(e)
    # Strategy 2: session_history — session 'general' fallback
    if not records:
        try:
            result = await _call_cap("memory.session_history", session_id="", limit=int(limit))
            if isinstance(result, dict):
                records = result.get("history") or result.get("records") or []
        except Exception as e:
            last_err = str(e)
    # Strategy 3: search with a noop wildcard (some backends return latest by default)
    if not records:
        try:
            result = await _call_cap("memory.search", query="*", limit=int(limit))
            if isinstance(result, dict):
                rows = result.get("results") or result.get("records") or []
                # Unwrap {record, score} shape
                records = [(r.get("record") if isinstance(r, dict) and "record" in r else r) for r in rows]
        except Exception as e:
            last_err = str(e)

    # Normalise to plain dicts
    normalised: List[Dict[str, Any]] = []
    for rec in records:
        if isinstance(rec, dict):
            normalised.append(rec)
        elif hasattr(rec, "__dict__"):
            normalised.append({k: v for k, v in vars(rec).items() if not k.startswith("_")})
        elif hasattr(rec, "_asdict"):
            normalised.append(rec._asdict())
        else:
            try:
                normalised.append(dict(rec))
            except Exception:
                normalised.append({"text": str(rec)})

    signal = min(1.0, len(normalised) / max(20, int(limit)))
    out = {
        "source":  "memory",
        "count":   len(normalised),
        "signal":  round(signal, 3),
        "sample":  normalised[:int(limit)],
        "summary": f"{len(normalised)} recent memory records",
    }
    if not normalised and last_err:
        out["error"] = last_err
    return out


@capability(
    "dream.sensor.fabric_recent", memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/fabric_recent",
    http_tags=["dream", "sensor"],
    description="Sample recent data fabric activity.",
)
async def dream_sensor_fabric_recent(limit: int = 20, trace_id=None):
    """
    Pull recent records from the fabric. Lists datasets, then queries each for
    the most recent items. Falls back to dataset count if querying isn't available.
    """
    fabric = _fabric()
    if not fabric:
        return {"source": "fabric", "count": 0, "signal": 0.0, "note": "fabric not loaded"}
    try:
        # 1. Get datasets
        datasets: List[Dict[str, Any]] = []
        if hasattr(fabric, "list_datasets"):
            res = await fabric.list_datasets()  # type: ignore
            datasets = res if isinstance(res, list) else (res.get("datasets", []) if isinstance(res, dict) else [])
        elif hasattr(fabric, "datasets"):
            res = await fabric.datasets()  # type: ignore
            datasets = res if isinstance(res, list) else (res.get("datasets", []) if isinstance(res, dict) else [])

        # 2. Pull a few items from the most-populated datasets
        items: List[Dict[str, Any]] = []
        per_ds = max(2, int(limit) // max(1, min(5, len(datasets))))
        ds_sorted = sorted(datasets, key=lambda d: -int(d.get("record_count", d.get("count", 0)) or 0))[:5]
        for d in ds_sorted:
            did = d.get("dataset_id") or d.get("id") or d.get("name") or ""
            if not did:
                continue
            try:
                fab_q = CAPABILITY_REGISTRY.get("fabric.query")
                if fab_q:
                    q = await fab_q["func"](query=json.dumps({
                        "dataset_id": did, "top_k": per_ds, "include_data": True,
                        "cache": False,
                    }))
                    if isinstance(q, dict):
                        rows = q.get("results") or q.get("items") or []
                        for row in rows[:per_ds]:
                            if isinstance(row, dict):
                                items.append({
                                    "id":      row.get("id"),
                                    "text":    (row.get("text") or row.get("content") or "")[:300],
                                    "dataset": did,
                                    "ts":      row.get("created_at") or row.get("ts") or "",
                                })
            except Exception:
                continue

        # Signal — based on fetched item count, not dataset count
        signal = min(1.0, len(items) / max(10, int(limit) // 2))
        return {
            "source":  "fabric",
            "count":   len(items),
            "signal":  round(signal, 3),
            "sample":  items[:int(limit)],
            "datasets_scanned": len(ds_sorted),
            "datasets_total":   len(datasets),
            "summary": f"{len(items)} fabric records across {len(ds_sorted)} datasets",
        }
    except Exception as e:
        return {"source": "fabric", "count": 0, "signal": 0.0, "error": str(e)}


@capability(
    "dream.sensor.syslog_errors", memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/syslog_errors",
    http_tags=["dream", "sensor"],
    description="Recent errors from the Vera syslog feed.",
)
async def dream_sensor_syslog_errors(limit: int = 40, trace_id=None):
    errs: List[Any] = []
    for cap_name in ("syslog.errors", "syslog.query"):
        cap = CAPABILITY_REGISTRY.get(cap_name)
        if not cap:
            continue
        try:
            res = await _call_cap(cap_name, limit=int(limit), level="error")
            if isinstance(res, dict):
                errs = res.get("errors") or res.get("entries") or res.get("records") or []
            elif isinstance(res, list):
                errs = res
            if errs:
                break
        except Exception:
            continue
    signal = min(1.0, len(errs) / 10.0)
    return {
        "source":  "syslog",
        "count":   len(errs),
        "signal":  round(signal, 3),
        "sample":  errs[:20],
        "summary": f"{len(errs)} recent error entries",
    }


@capability(
    "dream.sensor.bus_events", memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/bus_events",
    http_tags=["dream", "sensor"],
    description="Recent entries from the vera:cap:recent event bus.",
)
async def dream_sensor_bus_events(limit: int = 50, trace_id=None):
    r = _redis()
    if not r:
        return {"source": "bus", "count": 0, "signal": 0.0}
    try:
        rows = await r.zrevrange(KEY_RECENT_CAPS, 0, int(limit) - 1, withscores=True)
        events: List[Dict[str, Any]] = []
        for raw, score in rows or []:
            try:
                rec = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                rec["_score"] = score
                events.append(rec)
            except Exception:
                continue
        events = [e for e in events if not any(str(e.get("name", "")).startswith(p) for p in _IDLE_IGNORE_PREFIXES)]
        signal = min(1.0, len(events) / 30.0)
        return {
            "source":  "bus",
            "count":   len(events),
            "signal":  round(signal, 3),
            "sample":  events[:30],
            "summary": f"{len(events)} recent capability events",
        }
    except Exception as e:
        return {"source": "bus", "count": 0, "signal": 0.0, "error": str(e)}


@capability(
    "dream.sensor.news_overnight", memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/news_overnight",
    http_tags=["dream", "sensor"],
    description="Find RSS/news datasets in the fabric and sample overnight items.",
)
async def dream_sensor_news_overnight(limit: int = 40, trace_id=None):
    """
    Auto-discover news/RSS sources via fabric.sources (preferred) or fall back
    to name-based matching on dataset names. The user just needs to add an RSS
    feed to fabric.sources tagged 'news' (or with source_type='rss') and this
    sensor will pick it up automatically.
    """
    sources_cap = CAPABILITY_REGISTRY.get("fabric.sources")
    fab_q = CAPABILITY_REGISTRY.get("fabric.query")
    if not fab_q:
        return {"source": "news", "count": 0, "signal": 0.0, "note": "fabric.query not loaded"}

    news_dataset_ids: set = set()
    matched_sources: List[Dict[str, Any]] = []

    # Strategy 1 (preferred): query fabric.sources for any source tagged
    # 'news' / 'rss' OR with source_type rss
    if sources_cap:
        try:
            srcs_res = await sources_cap["func"]()
            sources = (srcs_res.get("sources") or []) if isinstance(srcs_res, dict) else []
            wanted = {"news", "rss", "feed", "headline", "headlines"}
            for s in sources:
                if not isinstance(s, dict):
                    continue
                stags_raw = s.get("tags") or ""
                if isinstance(stags_raw, str):
                    stags = {t.strip().lower() for t in stags_raw.split(",") if t.strip()}
                elif isinstance(stags_raw, list):
                    stags = {str(t).strip().lower() for t in stags_raw}
                else:
                    stags = set()
                stype = (s.get("source_type") or s.get("type") or "").lower()
                if stype:
                    stags.add(stype)
                if wanted & stags:
                    did = s.get("dataset_id") or s.get("id") or ""
                    if did:
                        news_dataset_ids.add(str(did))
                        matched_sources.append({
                            "id": s.get("id"), "label": s.get("label"),
                            "url": s.get("url"), "tags": list(stags),
                        })
        except Exception:
            pass

    # Strategy 2 (fallback): scan all datasets for news-like names
    if not news_dataset_ids:
        fabric = _fabric()
        if fabric:
            try:
                if hasattr(fabric, "list_datasets"):
                    res = await fabric.list_datasets()
                elif hasattr(fabric, "datasets"):
                    res = await fabric.datasets()
                else:
                    res = None
                datasets = []
                if isinstance(res, list):
                    datasets = res
                elif isinstance(res, dict):
                    datasets = res.get("datasets", []) or []
                for d in datasets:
                    ident = d.get("dataset_id") or d.get("id") or d.get("name") or ""
                    tags = [str(t).lower() for t in (d.get("tags") or [])]
                    source = str(d.get("source", "")).lower()
                    hay = " ".join([str(ident).lower(), source] + tags)
                    if any(kw in hay for kw in ("rss", "news", "feed", "headline")):
                        news_dataset_ids.add(str(ident))
            except Exception:
                pass

    if not news_dataset_ids:
        return {
            "source":   "news",
            "count":    0,
            "signal":   0.0,
            "note":     "no news/rss datasets discovered — add a fabric.sources entry tagged 'news'",
            "summary":  "no news sources",
        }

    items: List[Any] = []
    per_ds = max(5, int(limit) // max(1, min(8, len(news_dataset_ids))))
    for did in list(news_dataset_ids)[:8]:
        try:
            q = await fab_q["func"](query=json.dumps({
                "dataset_id": did, "top_k": per_ds,
                "include_data": True, "cache": False,
            }))
            if isinstance(q, dict):
                for row in (q.get("results") or [])[:per_ds]:
                    if isinstance(row, dict):
                        items.append({
                            "id":      row.get("id"),
                            "dataset": did,
                            "title":   (row.get("title") or row.get("text") or "")[:140],
                            "text":    (row.get("text") or "")[:400],
                            "ts":      row.get("created_at", ""),
                        })
        except Exception:
            continue

    items = items[:int(limit)]
    signal = min(1.0, len(items) / max(10, int(limit) // 2))
    return {
        "source":   "news",
        "count":    len(items),
        "signal":   round(signal, 3),
        "sample":   items,
        "datasets": list(news_dataset_ids),
        "matched_sources":  matched_sources,
        "summary":  f"{len(items)} news items from {len(news_dataset_ids)} datasets",
    }


@capability(
    "dream.sensor.research_recent", memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/research_recent",
    http_tags=["dream", "sensor"],
    description="Recent research sessions / notebook activity.",
)
async def dream_sensor_research_recent(limit: int = 20, full_content_top: int = 3,
                                         trace_id=None):
    """
    Recent research jobs/notebook activity. Set full_content_top > 0 to also
    fetch the full report text for the top N most recent completed jobs via
    research.job.status — useful for evaluating research quality.
    """
    cap_names = ("research.history", "research.db.search",
                 "research.bookmarks", "research.iterate.list")
    loaded = [n for n in cap_names if CAPABILITY_REGISTRY.get(n)]
    if not loaded:
        return {
            "source":  "research",
            "count":   0,
            "signal":  0.0,
            "sample":  [],
            "summary": "research caps not loaded",
            "note":    "no research capabilities registered yet — is research_capabilities.py loaded?",
        }

    seen_ids: set = set()
    items: List[Dict[str, Any]] = []

    for cap_name in cap_names:
        if not CAPABILITY_REGISTRY.get(cap_name):
            continue
        try:
            res = await _call_cap(cap_name, limit=int(limit), query="")
            if isinstance(res, dict):
                rows = (res.get("history") or res.get("results") or
                        res.get("notebooks") or res.get("items") or [])
            elif isinstance(res, list):
                rows = res
            else:
                rows = []

            for row in rows:
                if not isinstance(row, dict):
                    if hasattr(row, "__dict__"):
                        row = {k: v for k, v in vars(row).items() if not k.startswith("_")}
                    elif hasattr(row, "_asdict"):
                        row = row._asdict()
                    else:
                        try:
                            row = dict(row)
                        except Exception:
                            row = {"text": str(row)}
                rid = row.get("id") or row.get("job_id") or row.get("notebook_id")
                if rid:
                    if rid in seen_ids:
                        continue
                    seen_ids.add(rid)
                items.append(row)

        except Exception as e:
            log.debug("dream research sensor %s: %s", cap_name, e)
            continue

    items = items[:int(limit)]

    # Fetch full content for the top N most recent COMPLETED jobs so the
    # synthesizer/agentic loop can actually evaluate research output rather
    # than just see metadata.
    job_status_cap = CAPABILITY_REGISTRY.get("research.job.status")
    if job_status_cap and full_content_top > 0:
        completed_jobs = [
            it for it in items
            if (it.get("job_id") or it.get("id"))
            and str(it.get("status", "")).lower() in ("done", "completed", "finished", "ok", "")
        ]
        for it in completed_jobs[:int(full_content_top)]:
            jid = it.get("job_id") or it.get("id")
            if not jid:
                continue
            try:
                full = await job_status_cap["func"](job_id=str(jid))
                if isinstance(full, dict) and not full.get("error"):
                    # Pull the report body — varies by pipeline
                    report = (full.get("report") or full.get("content") or
                              full.get("output") or full.get("result") or "")
                    if isinstance(report, dict):
                        report = report.get("text") or json.dumps(report)[:2000]
                    it["full_content"] = str(report)[:3000] if report else ""
                    it["full_content_chars"] = len(str(report)) if report else 0
            except Exception as e:
                it["full_content_error"] = str(e)[:120]

    signal = min(1.0, len(items) / max(5, int(limit)))
    return {
        "source":  "research",
        "count":   len(items),
        "signal":  round(signal, 3),
        "sample":  items,
        "with_full_content": min(int(full_content_top), len(items)),
        "summary": f"{len(items)} research items"
                   + (f" (top {full_content_top} with full content)" if full_content_top > 0 else ""),
    }


# ─────────────────────────────────────────────────────────────────────────────
# NEW BUILT-IN SENSORS
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.sensor.memory_session",
    memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/memory_session",
    http_tags=["dream", "sensor"],
    description="Recent memory records from a specific session id (e.g. an active chat). "
                "Inputs: session_id (str), limit (int, default 30).",
)
async def dream_sensor_memory_session(session_id: str = "", limit: int = 30, trace_id=None):
    if not session_id:
        return {"source": "memory_session", "count": 0, "signal": 0.0, "note": "session_id required"}
    try:
        result = await _call_cap("memory.session_history", session_id=session_id, limit=int(limit))
        records = []
        if isinstance(result, dict):
            records = result.get("history") or result.get("records") or []
        signal = min(1.0, len(records) / max(10, int(limit) // 2))
        return {
            "source": "memory_session", "session_id": session_id,
            "count": len(records), "signal": round(signal, 3),
            "sample": records[:int(limit)],
            "summary": f"{len(records)} records from session {session_id[:12]}",
        }
    except Exception as e:
        return {"source": "memory_session", "count": 0, "signal": 0.0, "error": str(e)}


@capability(
    "dream.sensor.fabric_dataset",
    memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/fabric_dataset",
    http_tags=["dream", "sensor"],
    description="Pull recent records from one specific fabric dataset (by id). "
                "Inputs: dataset_id (str!), limit (int, default 30), query (str, optional).",
)
async def dream_sensor_fabric_dataset(dataset_id: str = "", limit: int = 30,
                                       query: str = "", trace_id=None):
    if not dataset_id:
        return {"source": "fabric_dataset", "count": 0, "signal": 0.0,
                "note": "dataset_id required"}
    fab_q = CAPABILITY_REGISTRY.get("fabric.query")
    if not fab_q:
        return {"source": "fabric_dataset", "count": 0, "signal": 0.0,
                "note": "fabric.query not loaded"}
    try:
        dsl = {"dataset_id": dataset_id, "top_k": int(limit), "include_data": True, "cache": False}
        if query:
            dsl["text"] = query
        res = await fab_q["func"](query=json.dumps(dsl))
        items = []
        if isinstance(res, dict):
            for r in (res.get("results") or [])[:int(limit)]:
                if isinstance(r, dict):
                    items.append({
                        "id":      r.get("id"),
                        "text":    (r.get("text") or "")[:400],
                        "dataset": dataset_id,
                        "ts":      r.get("created_at") or "",
                    })
        signal = min(1.0, len(items) / max(5, int(limit) // 3))
        return {
            "source": "fabric_dataset", "dataset_id": dataset_id,
            "count": len(items), "signal": round(signal, 3),
            "sample": items,
            "summary": f"{len(items)} records from {dataset_id}",
        }
    except Exception as e:
        return {"source": "fabric_dataset", "count": 0, "signal": 0.0, "error": str(e)}


@capability(
    "dream.sensor.fabric_by_tag",
    memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/fabric_by_tag",
    http_tags=["dream", "sensor"],
    description="Auto-discover fabric records by source tag(s). Queries fabric.sources "
                "for any source whose tags match, then pulls recent records from each "
                "matching dataset. Lets triggers find data dynamically — e.g. add a "
                "new RSS feed tagged 'news' and the morning_news trigger picks it up "
                "automatically. Inputs: tags (str! comma-sep), limit (int, default 30), "
                "per_dataset (int, default 10).",
)
async def dream_sensor_fabric_by_tag(tags: str = "", limit: int = 30,
                                      per_dataset: int = 10, trace_id=None):
    if not tags:
        return {"source": "fabric_by_tag", "count": 0, "signal": 0.0,
                "note": "tags required (comma-sep)"}
    wanted = {t.strip().lower() for t in tags.split(",") if t.strip()}
    sources_cap = CAPABILITY_REGISTRY.get("fabric.sources")
    fab_q = CAPABILITY_REGISTRY.get("fabric.query")
    if not sources_cap or not fab_q:
        return {"source": "fabric_by_tag", "count": 0, "signal": 0.0,
                "note": "fabric.sources / fabric.query not loaded"}
    try:
        # Find all sources whose tags overlap our wanted set
        srcs_res = await sources_cap["func"]()
        sources = (srcs_res.get("sources") or []) if isinstance(srcs_res, dict) else []
        matched_sources = []
        dataset_ids: set = set()
        for s in sources:
            if not isinstance(s, dict):
                continue
            stags_raw = s.get("tags") or ""
            if isinstance(stags_raw, str):
                stags = {t.strip().lower() for t in stags_raw.split(",") if t.strip()}
            elif isinstance(stags_raw, list):
                stags = {str(t).strip().lower() for t in stags_raw if t}
            else:
                stags = set()
            # Also check source_type as an implicit tag
            stype = (s.get("source_type") or s.get("type") or "").lower()
            if stype:
                stags.add(stype)
            if wanted & stags:
                matched_sources.append(s)
                did = s.get("dataset_id") or s.get("id") or ""
                if did:
                    dataset_ids.add(did)

        if not dataset_ids:
            return {
                "source":  "fabric_by_tag",
                "count":   0,
                "signal":  0.0,
                "tags":    list(wanted),
                "note":    f"no fabric sources match tag(s) {tags}",
                "sources_total": len(sources),
            }

        items: List[Dict[str, Any]] = []
        for did in list(dataset_ids)[:8]:
            try:
                r = await fab_q["func"](query=json.dumps({
                    "dataset_id": did, "top_k": int(per_dataset),
                    "include_data": True, "cache": False,
                }))
                if isinstance(r, dict):
                    for row in (r.get("results") or [])[:int(per_dataset)]:
                        if isinstance(row, dict):
                            items.append({
                                "id":      row.get("id"),
                                "dataset": did,
                                "text":    (row.get("text") or "")[:400],
                                "ts":      row.get("created_at", ""),
                            })
            except Exception:
                continue

        items = items[:int(limit)]
        signal = min(1.0, len(items) / max(5, int(limit) // 3))
        return {
            "source":   "fabric_by_tag",
            "tags":     list(wanted),
            "count":    len(items),
            "signal":   round(signal, 3),
            "sample":   items,
            "datasets": list(dataset_ids),
            "matched_sources": len(matched_sources),
            "summary": f"{len(items)} records across {len(dataset_ids)} datasets matching {tags}",
        }
    except Exception as e:
        return {"source": "fabric_by_tag", "count": 0, "signal": 0.0, "error": str(e)}


@capability(
    "dream.sensor.fabric_by_source_type",
    memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/fabric_by_source_type",
    http_tags=["dream", "sensor"],
    description="Auto-discover fabric records by source type (rss|api|http|wiki). "
                "Same idea as fabric_by_tag but matches source_type instead of tags. "
                "Inputs: source_type (str!), limit (int), per_dataset (int).",
)
async def dream_sensor_fabric_by_source_type(source_type: str = "", limit: int = 30,
                                               per_dataset: int = 10, trace_id=None):
    # Re-use fabric_by_tag — source_type is treated as a tag too in that sensor
    if not source_type:
        return {"source": "fabric_by_source_type", "count": 0, "signal": 0.0,
                "note": "source_type required (rss|api|http|wiki)"}
    return await dream_sensor_fabric_by_tag(tags=source_type, limit=limit,
                                              per_dataset=per_dataset)


@capability(
    "dream.sensor.cap_calls",
    memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/cap_calls",
    http_tags=["dream", "sensor"],
    description="Recent capability calls matching a name prefix (e.g. 'llm.', 'memory.'). "
                "Inputs: prefix (str), limit (int, default 50).",
)
async def dream_sensor_cap_calls(prefix: str = "", limit: int = 50, trace_id=None):
    r = _redis()
    if not r:
        return {"source": "cap_calls", "count": 0, "signal": 0.0}
    try:
        rows = await r.zrevrange(KEY_RECENT_CAPS, 0, int(limit) * 3, withscores=True)
        events: List[Dict[str, Any]] = []
        for raw, score in rows or []:
            try:
                rec = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                rec["_score"] = score
                if not prefix or str(rec.get("name", "")).startswith(prefix):
                    events.append(rec)
            except Exception:
                continue
        events = events[:int(limit)]
        signal = min(1.0, len(events) / max(20, int(limit) // 2))
        return {
            "source": "cap_calls", "prefix": prefix,
            "count": len(events), "signal": round(signal, 3),
            "sample": events,
            "summary": f"{len(events)} cap calls matching '{prefix or '*'}'",
        }
    except Exception as e:
        return {"source": "cap_calls", "count": 0, "signal": 0.0, "error": str(e)}


@capability(
    "dream.sensor.notebook_recent",
    memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/notebook_recent",
    http_tags=["dream", "sensor"],
    description="Recent notebook entries — what has been written down lately.",
)
async def dream_sensor_notebook_recent(limit: int = 15, trace_id=None):
    """
    Notebooks live in researcher_api at :8765/api/notebooks.
    First try the HTTP API, then fall back to any local notebook.* cap.
    """
    items: List[Dict[str, Any]] = []
    last_err = ""
    # 1. HTTP fetch from researcher_api
    try:
        import os as _os, httpx
        researcher_url = _os.getenv("VERA_RESEARCHER_URL", "http://localhost:8765")
        async with httpx.AsyncClient(timeout=8.0) as c:
            resp = await c.get(f"{researcher_url}/api/notebooks", params={"limit": int(limit)})
            if resp.status_code == 200:
                data = resp.json()
                rows = data if isinstance(data, list) else (data.get("notebooks") or data.get("results") or [])
                for n in rows[:int(limit)]:
                    if not isinstance(n, dict):
                        continue
                    items.append({
                        "id":         n.get("id") or n.get("notebook_id") or "",
                        "title":      n.get("title") or n.get("name") or "",
                        "text":       (n.get("description") or n.get("summary") or "")[:300],
                        "ts":         n.get("updated_at") or n.get("created_at") or "",
                        "cell_count": n.get("cell_count") or len(n.get("cells", [])),
                    })
    except Exception as e:
        last_err = str(e)
    # 2. Fallback: any local notebook cap
    if not items:
        for cap_name in ("notebook.list", "notebook.recent", "notebook.search",
                         "research.notebook.list"):
            cap = CAPABILITY_REGISTRY.get(cap_name)
            if not cap:
                continue
            try:
                res = await _call_cap(cap_name, limit=int(limit), query="")
                if isinstance(res, dict):
                    rows = res.get("notebooks") or res.get("entries") or res.get("results") or []
                    items.extend(rows)
                    if items:
                        break
            except Exception:
                continue
    signal = min(1.0, len(items) / max(5, int(limit) // 2))
    out = {
        "source": "notebook",
        "count": len(items), "signal": round(signal, 3),
        "sample": items[:int(limit)],
        "summary": f"{len(items)} recent notebook entries",
    }
    if not items and last_err:
        out["note"] = f"could not reach researcher_api: {last_err[:120]}"
    return out


@capability(
    "dream.sensor.ide_workspace",
    memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/ide_workspace",
    http_tags=["dream", "sensor"],
    description="Recently-modified files in IDE workspaces. Lists workspaces and "
                "samples recent changes from each. "
                "Inputs: workspace (str, optional — filter by workspace name), limit (int).",
)
async def dream_sensor_ide_workspace(workspace: str = "", limit: int = 20, trace_id=None):
    """
    Use the actual cap names registered by ide_capabilities.py:
      ide.workspace.list   — list saved workspaces
      ide.fs.list / ide.fs.tree — list files (best-effort)
    """
    items: List[Dict[str, Any]] = []

    # First try: list workspaces
    ws_list = CAPABILITY_REGISTRY.get("ide.workspace.list")
    workspaces: List[Dict[str, Any]] = []
    if ws_list:
        try:
            res = await ws_list["func"]()
            if isinstance(res, dict):
                workspaces = res.get("workspaces") or []
        except Exception:
            pass

    if workspace:
        workspaces = [w for w in workspaces if isinstance(w, dict) and w.get("name") == workspace]

    # For each workspace, sample recent files via ide.fs.list / ide.fs.tree
    fs_list = (CAPABILITY_REGISTRY.get("ide.fs.list") or
               CAPABILITY_REGISTRY.get("ide.fs.tree") or
               CAPABILITY_REGISTRY.get("ide.list_files"))
    if fs_list:
        for ws in workspaces[:5]:
            wname = ws.get("name", "") if isinstance(ws, dict) else str(ws)
            wpath = ws.get("path", "") if isinstance(ws, dict) else ""
            try:
                args = {"limit": int(limit)}
                if wpath: args["path"] = wpath
                if wname: args["workspace"] = wname
                res = await _call_cap(
                    "ide.fs.list" if "ide.fs.list" in CAPABILITY_REGISTRY
                    else ("ide.fs.tree" if "ide.fs.tree" in CAPABILITY_REGISTRY else "ide.list_files"),
                    **args)
                if isinstance(res, dict):
                    files = res.get("files") or res.get("entries") or res.get("tree") or []
                    for f in files[:int(limit)]:
                        if isinstance(f, dict):
                            items.append({
                                "workspace": wname,
                                "path":      f.get("path") or f.get("name") or "",
                                "modified":  f.get("modified") or f.get("mtime") or "",
                                "size":      f.get("size", 0),
                            })
            except Exception:
                continue

    # If we have workspaces but couldn't list files, just return the workspace list as signal
    if not items and workspaces:
        for ws in workspaces[:int(limit)]:
            if isinstance(ws, dict):
                items.append({
                    "workspace": ws.get("name", ""),
                    "path":      ws.get("path", ""),
                    "exists":    ws.get("exists", True),
                })

    signal = min(1.0, len(items) / max(5, int(limit) // 2))
    return {
        "source": "ide_workspace", "workspace": workspace,
        "count": len(items), "signal": round(signal, 3),
        "sample": items[:int(limit)],
        "workspaces_found": len(workspaces),
        "summary": f"{len(items)} IDE items across {len(workspaces)} workspace(s)",
    }


@capability(
    "dream.sensor.project_context",
    memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/project_context",
    http_tags=["dream", "sensor"],
    description="Resolve a project's full context: user-provided notes, LLM-maintained state, "
                "linked fabric/notebook/chat resources. Inputs: project_slug (str!).",
)
async def dream_sensor_project_context(project_slug: str = "", trace_id=None):
    if not project_slug:
        return {"source": "project", "count": 0, "signal": 0.0, "note": "project_slug required"}
    r = _redis()
    if not r:
        return {"source": "project", "count": 0, "signal": 0.0, "note": "redis unavailable"}
    try:
        raw = await r.hget("vera:dream:projects", project_slug)
        if not raw:
            return {"source": "project", "count": 0, "signal": 0.0,
                    "note": f"project {project_slug} not found"}
        proj = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        ctx_parts = []
        if proj.get("description"):
            ctx_parts.append({"text": "DESCRIPTION: " + proj["description"], "kind": "description"})
        if proj.get("user_context"):
            ctx_parts.append({"text": "USER CONTEXT:\n" + proj["user_context"], "kind": "user_context"})
        if proj.get("llm_context"):
            ctx_parts.append({"text": "CURRENT STATE (LLM-maintained):\n" + proj["llm_context"],
                              "kind": "llm_context"})
        for did in (proj.get("fabric_dataset_ids") or [])[:10]:
            ctx_parts.append({"text": f"Linked fabric dataset: `{did}` "
                                       f"(use `fabric.query` with dataset_id={did!r} to read)",
                              "kind": "resource"})

        # Resolve IDE workspace paths — including source-inspect snapshots — so
        # the agentic loop can reach the actual files via ide.fs.read.
        ws_paths: List[Dict[str, str]] = []
        ws_cap = CAPABILITY_REGISTRY.get("ide.workspace.list")
        if ws_cap and (proj.get("ide_workspaces") or []):
            try:
                wres = await ws_cap["func"]()
                all_ws = (wres.get("workspaces") or []) if isinstance(wres, dict) else []
                wanted = set(proj.get("ide_workspaces") or [])
                for w in all_ws:
                    if not isinstance(w, dict):
                        continue
                    name = w.get("name", "")
                    if name in wanted or w.get("kind") == "snapshot" and name in wanted:
                        path = w.get("path", "")
                        ws_paths.append({"name": name, "path": path,
                                          "kind": w.get("kind", "workspace")})
                        ctx_parts.append({
                            "text": f"IDE workspace `{name}` "
                                    f"({w.get('kind','workspace')}) at path `{path}`. "
                                    f"Files browsable via `ide.fs.list(path={path!r})` "
                                    f"and readable via `ide.fs.read(path=...)`.",
                            "kind":   "workspace",
                            "name":   name,
                            "path":   path,
                            "ws_kind": w.get("kind", "workspace"),
                        })
            except Exception as e:
                log.debug("project_context ws resolution: %s", e)

        # Resolve notebooks by hitting researcher_api with their ids
        if proj.get("notebook_ids"):
            try:
                import os as _os, httpx
                researcher_url = _os.getenv("VERA_RESEARCHER_URL", "http://localhost:8765")
                async with httpx.AsyncClient(timeout=6.0) as c:
                    for nid in (proj.get("notebook_ids") or [])[:5]:
                        try:
                            resp = await c.get(f"{researcher_url}/api/notebooks/{nid}")
                            if resp.status_code == 200:
                                d = resp.json()
                                title = d.get("title") if isinstance(d, dict) else nid
                                ctx_parts.append({
                                    "text": f"Linked notebook `{nid}` ({title}) "
                                            f"— readable at /api/notebooks/{nid}",
                                    "kind": "notebook",
                                })
                        except Exception:
                            continue
            except Exception:
                pass

        signal = 1.0 if ctx_parts else 0.0
        return {
            "source":       "project",
            "project_slug": project_slug,
            "count":        len(ctx_parts),
            "signal":       signal,
            "sample":       ctx_parts,
            "ws_paths":     ws_paths,
            "project":      {k: proj.get(k) for k in (
                                "name", "slug", "description",
                                "fabric_dataset_ids", "notebook_ids",
                                "chat_ids", "context_mode", "ide_workspaces",
                                "memory_ids", "agents", "models")},
            "summary": f"Project context for {proj.get('name', project_slug)} "
                       f"({len(proj.get('fabric_dataset_ids', []))} datasets, "
                       f"{len(ws_paths)} workspaces)",
        }
    except Exception as e:
        return {"source": "project", "count": 0, "signal": 0.0, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1 SENSORS
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.sensor.active_projects",
    memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/active_projects",
    http_tags=["dream", "sensor"],
    description="Cluster recent capability calls by namespace prefix to detect "
                "what the user is actively working on. Returns top prefixes with "
                "call counts and examples. "
                "Inputs: limit (int, default 200), top_n (int, default 5), "
                "hours_back (int, default 6).",
)
async def dream_sensor_active_projects(
    limit: int = 200,
    top_n: int = 5,
    hours_back: int = 6,
    trace_id=None,
):
    r = _redis()
    if not r:
        return {"source": "active_projects", "count": 0, "signal": 0.0,
                "note": "redis unavailable"}

    try:
        cutoff = time.time() - (int(hours_back) * 3600)
        rows = await r.zrevrangebyscore(
            KEY_RECENT_CAPS, "+inf", cutoff, start=0, num=int(limit),
            withscores=True,
        )

        prefix_counter: Counter = Counter()
        prefix_examples: Dict[str, List[str]] = defaultdict(list)
        prefix_last_ts: Dict[str, float] = {}

        for raw, score in (rows or []):
            try:
                rec = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                name = str(rec.get("name", ""))
                if any(name.startswith(p) for p in _IDLE_IGNORE_PREFIXES):
                    continue
                prefix = name.split(".")[0] if "." in name else name
                prefix_counter[prefix] += 1
                if len(prefix_examples[prefix]) < 5:
                    prefix_examples[prefix].append(name)
                if prefix not in prefix_last_ts or score > prefix_last_ts[prefix]:
                    prefix_last_ts[prefix] = score
            except Exception:
                continue

        top = prefix_counter.most_common(int(top_n))
        total_calls = sum(prefix_counter.values())
        projects: List[Dict[str, Any]] = []
        for prefix, count in top:
            pct = round(count / max(1, total_calls) * 100, 1)
            projects.append({
                "prefix":   prefix,
                "calls":    count,
                "pct":      pct,
                "examples": prefix_examples[prefix],
                "last_ts":  prefix_last_ts.get(prefix, 0),
                "dominant": pct > 40,
            })

        signal = min(1.0, len(projects) / 3.0)
        dominant = next((p for p in projects if p.get("dominant")), None)

        return {
            "source":       "active_projects",
            "count":        len(projects),
            "signal":       round(signal, 3),
            "total_calls":  total_calls,
            "hours_back":   hours_back,
            "projects":     projects,
            "dominant":     dominant.get("prefix") if dominant else None,
            "summary": (
                f"{total_calls} cap calls across {len(prefix_counter)} namespaces "
                f"in last {hours_back}h. "
                + (f"Dominant area: {dominant['prefix']} ({dominant['pct']}%)"
                   if dominant else "No dominant area.")
            ),
        }
    except Exception as e:
        return {"source": "active_projects", "count": 0, "signal": 0.0,
                "error": str(e)}


@capability(
    "dream.sensor.source_changes",
    memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/source_changes",
    http_tags=["dream", "sensor"],
    description="Detect source code changes by comparing live source tree against "
                "the most recent inspect snapshot. Reports changed files, new caps, "
                "and overall code stats.",
)
async def dream_sensor_source_changes(trace_id=None):
    src_info_cap = CAPABILITY_REGISTRY.get("ide.inspect.source_info")
    if not src_info_cap:
        return {"source": "source_changes", "count": 0, "signal": 0.0,
                "note": "ide.inspect.source_info not loaded"}

    try:
        src_info = await src_info_cap["func"]()
        if isinstance(src_info, dict) and src_info.get("error"):
            return {"source": "source_changes", "count": 0, "signal": 0.0,
                    "error": src_info["error"]}
    except Exception as e:
        return {"source": "source_changes", "count": 0, "signal": 0.0,
                "error": str(e)}

    modules = src_info.get("modules", [])
    cap_count = src_info.get("capabilities_registered", 0)

    snap_cap = CAPABILITY_REGISTRY.get("ide.inspect.list_snapshots")
    snapshots = []
    if snap_cap:
        try:
            snap_res = await snap_cap["func"]()
            snapshots = snap_res.get("snapshots", [])
        except Exception:
            pass

    changed_files: List[str] = []
    if snapshots:
        latest_snap = snapshots[0]
        diff_cap = CAPABILITY_REGISTRY.get("ide.inspect.diff_snapshot")
        if diff_cap and not latest_snap.get("is_fresh"):
            try:
                diff_data = await diff_cap["func"](
                    snapshot_id=latest_snap["id"],
                    max_chars_per_file=5000,
                )
                changed_files = (
                    (diff_data or {}).get("modified", [])
                    + (diff_data or {}).get("added", [])
                )
            except Exception:
                pass

    signal = min(1.0, len(changed_files) / 3.0) if changed_files else 0.1

    sample: List[Dict[str, Any]] = []
    for mod in modules[:15]:
        sample.append({
            "text": f"{mod['name']} ({mod['lines']} lines, {mod['bytes']} bytes)",
            "file": mod["name"],
            "lines": mod["lines"],
        })

    return {
        "source":         "source_changes",
        "count":          len(changed_files) or len(modules),
        "signal":         round(signal, 3),
        "sample":         sample,
        "changed_files":  changed_files[:20],
        "modules_count":  len(modules),
        "cap_count":      cap_count,
        "has_snapshot":    bool(snapshots),
        "latest_snapshot": snapshots[0]["id"] if snapshots else None,
        "snapshot_fresh":  snapshots[0].get("is_fresh") if snapshots else None,
        "summary": (
            f"{len(modules)} Python modules, {cap_count} caps registered. "
            + (f"{len(changed_files)} files changed since snapshot {snapshots[0]['id']}."
               if changed_files
               else ("Source unchanged since last snapshot."
                     if snapshots and snapshots[0].get("is_fresh")
                     else "No snapshot taken yet."))
        ),
    }


@capability(
    "dream.sensor.source_review_state", memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/source_review_state", http_tags=["dream", "sensor"],
    description="Sense the source-review state: current snapshot, how many files "
                "exist vs have been reviewed, and what was last reviewed. Drives "
                "continuation reviews (signal is high while files remain unreviewed).",
)
async def dream_sensor_source_review_state(trace_id=None):
    snap = await _resolve_review_snapshot(label="continue")
    snapshot_id = snap.get("snapshot_id") or ""
    files = await _enumerate_source_files(snapshot_id)
    names = [f["rel"] for f in files]

    reviewed: set = set()
    last_run: Dict[str, Any] = {}
    r = _redis()
    if r and snapshot_id:
        try:
            raw = await r.smembers(KEY_REVIEWED_FILES + snapshot_id)
            reviewed = {(x.decode() if isinstance(x, bytes) else x) for x in (raw or [])}
        except Exception:
            reviewed = set()
        try:
            lr = await r.get(KEY_REVIEW_RUN)
            if lr:
                last_run = json.loads(lr.decode() if isinstance(lr, bytes) else lr)
        except Exception:
            last_run = {}

    unreviewed = [n for n in names if n not in reviewed]
    # Signal: more unreviewed files => stronger pull to continue
    signal = min(1.0, len(unreviewed) / 5.0) if unreviewed else 0.0
    sample = [{"text": f"unreviewed: {n}", "file": n} for n in unreviewed[:15]]

    return {
        "source":          "source_review_state",
        "count":           len(unreviewed),
        "signal":          round(signal, 3),
        "snapshot_id":     snapshot_id,
        "total_files":     len(names),
        "reviewed":        len(reviewed),
        "unreviewed":      len(unreviewed),
        "unreviewed_files": unreviewed[:50],
        "last_run":        last_run,
        "sample":          sample,
        "summary": (
            f"Snapshot {snapshot_id}: {len(reviewed)}/{len(names)} files reviewed, "
            f"{len(unreviewed)} remaining."
            + (f" Last run generated {last_run.get('reports_generated', 0)} reports."
               if last_run else "")
        ),
    }


@capability(
    "dream.sensor.memory_graph_walk",
    memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/memory_graph_walk",
    http_tags=["dream", "sensor"],
    description="Pick a random recent memory node (weighted toward under-explored "
                "ones) and traverse its edges to find unexplored graph neighbourhoods. "
                "Inputs: seed_limit (int, default 20), traverse_depth (int, default 2), "
                "traverse_limit (int, default 15).",
)
async def dream_sensor_memory_graph_walk(
    seed_limit: int = 20,
    traverse_depth: int = 2,
    traverse_limit: int = 15,
    trace_id=None,
):
    seed_nodes = []
    try:
        result = await _call_cap("memory.all_nodes", limit=int(seed_limit))
        if isinstance(result, dict):
            seed_nodes = result.get("nodes") or result.get("records") or []
    except Exception:
        pass

    if not seed_nodes:
        return {"source": "memory_graph_walk", "count": 0, "signal": 0.0,
                "note": "no memory nodes available"}

    # Weight toward nodes with fewer relations (under-explored)
    weighted = []
    for node in seed_nodes:
        if isinstance(node, dict):
            rels = len(node.get("relations", []))
            weight = max(1, 10 - rels)
            weighted.append((node, weight))

    if not weighted:
        return {"source": "memory_graph_walk", "count": 0, "signal": 0.0,
                "note": "no valid seed nodes"}

    total_weight = sum(w for _, w in weighted)
    pick = random.uniform(0, total_weight)
    cumulative = 0
    seed = weighted[0][0]
    for node, weight in weighted:
        cumulative += weight
        if pick <= cumulative:
            seed = node
            break

    seed_id = seed.get("id", "")
    seed_text = (seed.get("text") or seed.get("summary") or "")[:200]
    seed_category = seed.get("category", "")
    seed_tags = seed.get("tags", [])

    # Traverse from the seed
    connected: List[Dict[str, Any]] = []
    edge_types: List[str] = []
    traverse_cap = CAPABILITY_REGISTRY.get("memory.traverse")
    if traverse_cap:
        try:
            trav_result = await traverse_cap["func"](
                start_id=seed_id,
                depth=int(traverse_depth),
                limit=int(traverse_limit),
            )
            for item in (trav_result or {}).get("results", []):
                node_data = item.get("node") or item.get("record") or item
                if isinstance(node_data, dict) and node_data.get("id"):
                    connected.append({
                        "id":       node_data.get("id"),
                        "text":     (node_data.get("text") or node_data.get("summary") or "")[:150],
                        "category": node_data.get("category", ""),
                        "type":     node_data.get("record_type", ""),
                        "relation": item.get("relation", "RELATED"),
                    })
                    edge_types.append(item.get("relation", "RELATED"))
        except Exception as e:
            log.debug("memory_graph_walk traverse: %s", e)

    # Semantic neighbours via memory.similar
    similar_nodes: List[Dict[str, Any]] = []
    if seed_text:
        similar_cap = CAPABILITY_REGISTRY.get("memory.similar")
        if similar_cap:
            try:
                sim_result = await similar_cap["func"](
                    query=seed_text[:200],
                    limit=5,
                )
                for item in (sim_result or {}).get("results", []):
                    rec = item.get("record", item) if isinstance(item, dict) else {}
                    if isinstance(rec, dict) and rec.get("id") and rec["id"] != seed_id:
                        similar_nodes.append({
                            "id":       rec.get("id"),
                            "text":     (rec.get("text") or "")[:150],
                            "category": rec.get("category", ""),
                            "score":    round(item.get("score", 0), 3),
                        })
            except Exception:
                pass

    total_found = len(connected) + len(similar_nodes)
    signal = min(1.0, total_found / 8.0)

    sample: List[Dict[str, Any]] = [{
        "text": f"SEED NODE [{seed_category}]: {seed_text}",
        "id":   seed_id,
        "role": "seed",
        "tags": seed_tags,
    }]
    for c in connected[:10]:
        sample.append({
            "text": f"CONNECTED [{c.get('relation', '?')}] [{c.get('category','')}]: {c['text']}",
            "id":   c["id"],
            "role": "connected",
        })
    for s in similar_nodes[:5]:
        sample.append({
            "text": f"SIMILAR (score={s['score']}) [{s.get('category','')}]: {s['text']}",
            "id":   s["id"],
            "role": "similar",
        })

    return {
        "source":         "memory_graph_walk",
        "count":          total_found,
        "signal":         round(signal, 3),
        "sample":         sample,
        "seed_node":      {"id": seed_id, "text": seed_text,
                           "category": seed_category, "tags": seed_tags},
        "connected":      connected,
        "similar":        similar_nodes,
        "edge_types":     list(set(edge_types)),
        "traverse_depth": traverse_depth,
        "summary": (
            f"Walked from '{seed_text[:60]}' ({seed_category or 'uncat'}): "
            f"{len(connected)} connected nodes, {len(similar_nodes)} similar. "
            f"Edge types: {', '.join(set(edge_types)[:5]) or 'none'}."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# STAGES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.stage.gather", memory="off", silent=True,
    description="Dream pipeline stage 1: call all configured sensors and aggregate their signal.",
)
async def dream_stage_gather(state: Optional[Dict[str, Any]] = None, trace_id=None):
    state = state or {}
    trig = state.get("trigger", {})
    seed = state.get("seed") or {}
    sensors = trig.get("sensors") or ["dream.sensor.memory_recent"]
    sensor_params = trig.get("sensor_params") or {}
    results: Dict[str, Any] = {}
    total_signal = 0.0

    # Normalize sensor names — accept full id, short id, or custom.id
    def _normalize_sensor(s: str) -> str:
        if not s:
            return s
        # Already in registry as-is?
        if s in CAPABILITY_REGISTRY or s in SENSOR_REGISTRY:
            return s
        # Try with dream.sensor. prefix (handles legacy short-form storage)
        full = f"dream.sensor.{s}" if not s.startswith("dream.") and not s.startswith("custom.") else s
        if full in CAPABILITY_REGISTRY or full in SENSOR_REGISTRY:
            return full
        # Try as custom sensor
        cust = f"custom.{s}" if not s.startswith("custom.") else s
        if cust in SENSOR_REGISTRY:
            return cust
        # Last resort: return as-is and let _call_cap try its own resolution
        return s

    for sname_raw in sensors:
        sname = _normalize_sensor(sname_raw)
        short_id = sname.replace("dream.sensor.", "").replace("custom.", "")
        params = sensor_params.get(short_id) or sensor_params.get(sname) or sensor_params.get(sname_raw) or {}
        if not isinstance(params, dict):
            params = {}
        try:
            # Custom sensors (id starts with "custom.") go through the wrapper
            is_custom = (sname.startswith("custom.") or
                         (sname in SENSOR_REGISTRY and SENSOR_REGISTRY.get(sname, {}).get("custom")))
            if is_custom:
                res = await dream_sensor_custom_run(
                    sensor_id=sname, limit=int(params.get("limit", 30)), **params)
            else:
                # Built-in sensor — call the cap directly (with prefix fallback)
                res = await _call_cap(sname, **params)
        except Exception as e:
            res = {"error": str(e), "signal": 0.0}
        results[sname] = res
        if isinstance(res, dict):
            total_signal += float(res.get("signal", 0.0) or 0.0)

    # ── Honour seed: pinned memory ids ───────────────────────────────────────
    pinned_mem = [m for m in (seed.get("pinned_memory_ids") or []) if isinstance(m, str)]
    if pinned_mem:
        mem_get = CAPABILITY_REGISTRY.get("memory.get")
        sample: List[Dict[str, Any]] = []
        for mid in pinned_mem[:30]:
            if not mem_get:
                break
            try:
                rec = await mem_get["func"](id=mid)
                if isinstance(rec, dict) and not rec.get("error"):
                    sample.append({
                        "id":   rec.get("id"),
                        "text": (rec.get("text") or rec.get("summary") or "")[:400],
                        "category": rec.get("category", ""),
                        "ts":   rec.get("created_at", ""),
                    })
            except Exception:
                continue
        results["dream.seed.pinned_memory"] = {
            "source": "seed", "count": len(sample), "signal": min(1.0, len(sample) / 5.0),
            "sample": sample,
        }
        total_signal += results["dream.seed.pinned_memory"]["signal"]

    # ── Honour seed: extra fabric ids ────────────────────────────────────────
    extra_fab = [f for f in (seed.get("extra_fabric_ids") or []) if isinstance(f, str)]
    if extra_fab:
        fab_q = CAPABILITY_REGISTRY.get("fabric.query")
        sample: List[Dict[str, Any]] = []
        if fab_q:
            try:
                res = await fab_q["func"](query=json.dumps({
                    "ids": extra_fab[:30], "include_data": True, "cache": False,
                }))
                for r in (res or {}).get("results", [])[:30]:
                    sample.append({
                        "id":   r.get("id"),
                        "text": (r.get("text") or "")[:400],
                        "dataset": r.get("dataset_id", ""),
                    })
            except Exception:
                pass
        results["dream.seed.fabric"] = {
            "source": "seed", "count": len(sample), "signal": min(1.0, len(sample) / 5.0),
            "sample": sample,
        }
        total_signal += results["dream.seed.fabric"]["signal"]

    # ── Honour seed: focus_topic — fold into a memory.search probe ──────────
    focus = (seed.get("focus_topic") or "").strip()
    if focus:
        mem_search = CAPABILITY_REGISTRY.get("memory.search")
        if mem_search:
            try:
                res = await mem_search["func"](query=focus, limit=15)
                hits = []
                for item in (res or {}).get("results", [])[:15]:
                    rec = item.get("record", item) if isinstance(item, dict) else {}
                    hits.append({
                        "id":   rec.get("id"),
                        "text": (rec.get("text") or "")[:300],
                        "ts":   rec.get("created_at", ""),
                    })
                results["dream.seed.focus_search"] = {
                    "source": "seed", "topic": focus,
                    "count":  len(hits), "signal": min(1.0, len(hits) / 5.0),
                    "sample": hits,
                }
                total_signal += results["dream.seed.focus_search"]["signal"]
            except Exception:
                pass

    sensor_count = max(1, len(results))
    avg_signal = total_signal / sensor_count
    state["gather"] = {
        "sensors": list(results.keys()),
        "results": results,
        "signal":  round(avg_signal, 3),
    }
    return state


@capability(
    "dream.stage.themes", memory="off", silent=True,
    description="Dream pipeline stage 2: detect themes/trends across gathered sensor data using NLP or LLM fallback.",
)
async def dream_stage_themes(state: Optional[Dict[str, Any]] = None, trace_id=None):
    state = state or {}
    gather = state.get("gather", {})
    results = gather.get("results", {}) if isinstance(gather, dict) else {}

    texts: List[str] = []
    for _, sres in results.items():
        if not isinstance(sres, dict):
            continue
        for item in sres.get("sample", []) or []:
            if isinstance(item, dict):
                for k in ("text", "title", "message", "headline", "content", "body", "summary"):
                    v = item.get(k)
                    if isinstance(v, str) and v.strip():
                        texts.append(v.strip()[:400])
                        break
            elif isinstance(item, str):
                texts.append(item[:400])

    themes: List[str] = []

    nlp_cap = CAPABILITY_REGISTRY.get("nlp.run")
    if nlp_cap and texts:
        # Try the modern nlp.run signature first (module_name + text)
        # Falls back to LLM extraction if nlp.run isn't suitable
        for module in ("themes_extractor", "topics", "entities"):
            try:
                nlp_res = await _call_cap(
                    "nlp.run",
                    module_name=module,
                    text="\n".join(texts[:80]),
                )
                if isinstance(nlp_res, dict) and not nlp_res.get("error"):
                    payload = nlp_res.get("payload") or nlp_res
                    if isinstance(payload, dict):
                        for key in ("themes", "topics", "keywords", "entities"):
                            v = payload.get(key)
                            if isinstance(v, list):
                                themes.extend(str(x) for x in v[:10])
                    if themes:
                        break
            except Exception as e:
                log.debug("dream themes nlp module=%s: %s", module, e)
                continue

    if not themes and texts:
        summary_prompt = (
            "Extract 3-7 short theme keywords from the following items. "
            "Respond with a JSON array of strings only.\n\n"
            + "\n".join(f"- {t}" for t in texts[:40])
        )
        raw = await _llm_generate(summary_prompt, system="You extract themes. JSON array only.")
        try:
            start = raw.find("[")
            end = raw.rfind("]")
            if start != -1 and end != -1:
                parsed = json.loads(raw[start:end + 1])
                if isinstance(parsed, list):
                    themes = [str(x)[:60] for x in parsed[:10]]
        except Exception:
            pass

    seen = set()
    unique = []
    # Prepend focus_topic from seed so synthesis treats it as a primary theme
    seed = state.get("seed") or {}
    focus = (seed.get("focus_topic") or "").strip()
    if focus:
        unique.append(focus)
        seen.add(focus.lower())
    for t in themes:
        k = t.lower()
        if k and k not in seen:
            seen.add(k)
            unique.append(t)

    state["themes"] = unique[:10]
    state["themes_text_count"] = len(texts)
    return state


# ─────────────────────────────────────────────────────────────────────────────
# STAGE: SNAPSHOT SOURCE (Phase 1 — source review pre-step)
# ─────────────────────────────────────────────────────────────────────────────
# Runs before goal_refine in the source_review pipeline. Takes a fresh
# snapshot if the latest is stale (or missing), then diffs it against live
# source. Stores snapshot_id and changed_files in state so the agent loop
# has concrete file paths to review without wasting agentic cycles on it.

@capability(
    "dream.stage.snapshot_source", memory="off", silent=True,
    description="Dream pipeline stage: ensure a fresh source snapshot exists and "
                "diff it against live. Stores snapshot_id, changed_files, and "
                "review_candidates in state for downstream stages. "
                "Place before goal_refine in source_review pipelines.",
)
async def dream_stage_snapshot_source(
    state: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    state = state or {}
    cycle_id = state.get("cycle_id", "?")
    trig = state.get("trigger", {})
    journal_id = state.get("journal_id") or cycle_id

    await emit_event({
        "type": "dream.stage.started",
        "cycle_id": cycle_id,
        "stage": "dream.stage.snapshot_source",
    })

    # Deterministic: create a fresh snapshot iff source changed, else reuse the
    # most recent one. The diff baseline is the PREVIOUS snapshot, so changed
    # files reflect what actually changed (diffing a just-created snapshot vs
    # live is always empty — the old bug).
    snap = await _resolve_review_snapshot(label="dream_source_review")
    snapshot_id = snap.get("snapshot_id")
    baseline_id = snap.get("baseline_id")
    changed_files: List[str] = []
    diff_blob: Dict[str, Any] = {}

    diff_cap = CAPABILITY_REGISTRY.get("ide.inspect.diff_snapshot")
    # Only diff for "recent changes" — i.e. when we just created a snapshot
    # because the tree changed; diff the baseline (pre-change) against live.
    diff_target = baseline_id if snap.get("created") else None
    if diff_cap and diff_target:
        try:
            diff = await diff_cap["func"](snapshot_id=diff_target,
                                          max_chars_per_file=6000) or {}
            changed_files = (diff.get("modified", []) + diff.get("added", []))
            diff_blob = diff.get("diffs", {}) or {}
        except Exception as e:
            log.debug("dream.stage.snapshot_source diff: %s", e)

    state["snapshot"] = {
        "snapshot_id":   snapshot_id,
        "baseline_id":   baseline_id,
        "created":       snap.get("created", False),
        "current_hash":  snap.get("current_hash", ""),
        "changed_files": changed_files,
        "diffs":         diff_blob,
    }

    await _journal_append(journal_id,
        (f"Snapshot {snapshot_id} "
         + ("created (source changed)" if snap.get("created") else "reused (no changes)")
         + f"; {len(changed_files)} changed file(s) since baseline "
         + (baseline_id or "—") + "."),
        kind="note", stage="snapshot_source", title="Source snapshot resolved")

    await emit_event({
        "type": "dream.stage.completed",
        "cycle_id": cycle_id,
        "stage": "dream.stage.snapshot_source",
        "snapshot_id": snapshot_id,
        "created": snap.get("created", False),
        "changed": len(changed_files),
    })

    return state


# ─────────────────────────────────────────────────────────────────────────────
# STAGE: CAP EXECUTE (Phase 2 — run a specific capability as a stage)
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.stage.cap_execute", memory="off", silent=True,
    description="Dream pipeline stage: execute a single named capability with "
                "params from trigger config. Stores result in state['cap_execute']. "
                "Configure via stage_config: {cap_execute: {cap: 'name', params: {}}}.",
)
async def dream_stage_cap_execute(
    state: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    state = state or {}
    trig = state.get("trigger", {})
    cycle_id = state.get("cycle_id", "?")
    stage_cfg = trig.get("stage_config", {}).get("cap_execute", {})
    cap_name = stage_cfg.get("cap", "")
    params = dict(stage_cfg.get("params", {}))

    await emit_event({
        "type": "dream.stage.started", "cycle_id": cycle_id,
        "stage": "dream.stage.cap_execute", "cap": cap_name,
    })

    if not cap_name:
        state["cap_execute"] = {"error": "no cap in stage_config.cap_execute.cap"}
        return state

    # Substitute $state_key references in params
    for k, v in list(params.items()):
        if isinstance(v, str) and v.startswith("$"):
            params[k] = state.get(v[1:], v)

    try:
        result = await _call_cap(cap_name, **params)
        state["cap_execute"] = {
            "cap": cap_name, "params": params, "result": result,
            "ok": not (isinstance(result, dict) and result.get("error")),
        }
    except Exception as e:
        state["cap_execute"] = {"cap": cap_name, "error": str(e), "ok": False}

    await emit_event({
        "type": "dream.stage.completed", "cycle_id": cycle_id,
        "stage": "dream.stage.cap_execute", "cap": cap_name,
        "ok": state["cap_execute"].get("ok", False),
    })
    return state


# ─────────────────────────────────────────────────────────────────────────────
# STAGE: DAG EXECUTE (Phase 2 — run a specific DAG workflow as a stage)
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.stage.dag_execute", memory="off", silent=True,
    description="Dream pipeline stage: execute a DAG workflow from config. "
                "Configure via stage_config: {dag_execute: {dag_id: 'name'}} or "
                "{dag_execute: {steps: [['cap','key']]}}.",
)
async def dream_stage_dag_execute(
    state: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    state = state or {}
    trig = state.get("trigger", {})
    cycle_id = state.get("cycle_id", "?")
    stage_cfg = trig.get("stage_config", {}).get("dag_execute", {})

    await emit_event({
        "type": "dream.stage.started", "cycle_id": cycle_id,
        "stage": "dream.stage.dag_execute",
    })

    dag_run = CAPABILITY_REGISTRY.get("dag.run")
    if not dag_run:
        state["dag_execute"] = {"error": "dag.run not available"}
        return state

    try:
        dag_args: Dict[str, Any] = {}
        if stage_cfg.get("dag_id"):
            dag_args["dag_id"] = stage_cfg["dag_id"]
        elif stage_cfg.get("steps"):
            dag_args["steps"] = stage_cfg["steps"]
        else:
            state["dag_execute"] = {"error": "no dag_id or steps configured"}
            return state

        initial = {}
        for key in ("themes", "refined_goal", "gather", "snapshot"):
            if state.get(key):
                initial[key] = state[key]
        dag_args["initial_state"] = initial

        result = await dag_run["func"](**dag_args)
        state["dag_execute"] = {
            "result": result,
            "ok": not (isinstance(result, dict) and result.get("error")),
        }
    except Exception as e:
        state["dag_execute"] = {"error": str(e), "ok": False}

    await emit_event({
        "type": "dream.stage.completed", "cycle_id": cycle_id,
        "stage": "dream.stage.dag_execute",
        "ok": state["dag_execute"].get("ok", False),
    })
    return state


# ─────────────────────────────────────────────────────────────────────────────
# STAGE: PROJECT ACTION (Phase 3 — actually execute next steps for projects)
# ─────────────────────────────────────────────────────────────────────────────
# The existing propose_action stage only proposes — this one acts. It reads
# the proposed action (or refine goal) and executes it via the agent loop
# with a project-scoped whitelist that includes write caps.

@capability(
    "dream.stage.project_action", memory="off", silent=True,
    description="Dream pipeline stage: execute concrete project actions (not just "
                "propose them). Reads the refined_goal or proposed_action from state "
                "and uses a focused agent loop to carry it out. Scoped to the project's "
                "resources. Place after goal_refine or propose_action in project pipelines.",
)
async def dream_stage_project_action(
    state: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    state = state or {}
    trig = state.get("trigger", {})
    seed = state.get("seed") or {}
    cycle_id = state.get("cycle_id", "?")
    project_slug = seed.get("project_id") or trig.get("project", "")

    await emit_event({
        "type": "dream.stage.started", "cycle_id": cycle_id,
        "stage": "dream.stage.project_action", "project": project_slug,
    })

    # Determine what to do — prioritise refined_goal, fall back to proposed_action
    action_goal = state.get("refined_goal") or ""
    if not action_goal:
        proposed = state.get("proposed_action") or {}
        if isinstance(proposed, dict):
            action_goal = proposed.get("action") or proposed.get("proposal") or ""
        elif isinstance(proposed, str):
            action_goal = proposed

    if not action_goal or len(action_goal) < 10:
        state["project_action"] = {"skipped": True, "reason": "no actionable goal found"}
        await emit_event({
            "type": "dream.stage.completed", "cycle_id": cycle_id,
            "stage": "dream.stage.project_action", "skipped": True,
        })
        return state

    # Build a project-scoped toolkit — wider than standard dream whitelist
    # because we're actually executing actions, not just investigating
    whitelist = trig.get("whitelist") or []
    if not whitelist:
        whitelist = await _get_whitelist()

    # Add project-essential write caps
    project_write_caps = [
        "memory.create", "memory.update",
        "fabric.ingest", "fabric.entity_graph.extract",
        "nlp.run",
        "research.quick_search", "research.expand",
        "notebook.write", "notebook.append",
        "project.context.update",
    ]
    full_whitelist = list(set(whitelist + project_write_caps))

    # Constrain the goal to be project-specific
    project_name = seed.get("project_name") or project_slug or "this project"
    system_ctx = (
        f"You are executing a concrete action for the project '{project_name}'. "
        f"Project context: {(seed.get('project_context') or '')[:2000]}\n\n"
        "You MUST actually execute the action — do not just propose or describe it. "
        "Call the appropriate tools to make real changes: create memory records, "
        "ingest data, run entity extraction, write to notebooks, update project context. "
        "If the action requires information you don't have, use research.quick_search "
        "or memory.search to find it first, then proceed."
    )

    goal = f"{system_ctx}\n\nACTION TO EXECUTE:\n{action_goal}"

    # On iterations after the first, tell the agent what's already been done so
    # it advances to the NEXT step instead of repeating itself.
    _iter = int(state.get("iteration_index", 1) or 1)
    if _iter > 1:
        prior = [f.get("content", "") for f in (state.get("findings") or [])
                 if f.get("action")][-8:]
        if prior:
            goal += (
                "\n\nALREADY DONE in previous iterations (do NOT repeat these — "
                "continue with the next most valuable step, or stop if the work "
                "is complete):\n- " + "\n- ".join(p[:200] for p in prior))

    # Run via the agent loop
    agent_loop_cap = CAPABILITY_REGISTRY.get("dag.agent_loop_v2") or \
                     CAPABILITY_REGISTRY.get("dag.agent_loop")
    if not agent_loop_cap:
        state["project_action"] = {"error": "dag.agent_loop not available"}
        return state

    try:
        max_steps = int(trig.get("max_steps", 6))
        settings = await _resolve_loop_settings(trig, state)
        # Action stage caps the budget at 8 unless a smaller value is configured
        settings["max_cycles"] = min(int(settings.get("max_cycles", max_steps)), 8)
        loop_kwargs = _loop_kwargs_for(
            agent_loop_cap["func"], settings,
            goal=goal,
            allowed_caps=",".join(full_whitelist),
        )
        result = await agent_loop_cap["func"](**loop_kwargs)
        state["project_action"] = {
            "goal": action_goal,
            "result": result,
            "ok": isinstance(result, dict) and result.get("summary"),
        }
        # Merge findings
        if isinstance(result, dict) and result.get("tool_calls"):
            existing_findings = state.get("findings", [])
            for tc in result.get("tool_calls", []):
                existing_findings.append({
                    "source": tc.get("tool", "?"),
                    "content": str(tc.get("preview", ""))[:500],
                    "action": True,
                })
            state["findings"] = existing_findings
    except Exception as e:
        state["project_action"] = {"error": str(e), "ok": False}

    await emit_event({
        "type": "dream.stage.completed", "cycle_id": cycle_id,
        "stage": "dream.stage.project_action",
        "ok": state["project_action"].get("ok", False),
        "project": project_slug,
    })
    return state


# ─────────────────────────────────────────────────────────────────────────────
# STAGE: MEMORY DEEP TRAVERSE (Phase 3 — rich graph exploration)
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.stage.memory_deep_traverse", memory="off", silent=True,
    description="Dream pipeline stage: deep memory graph traversal from seed topics. "
                "Follows edges 3-4 hops deep, collects semantic neighbours, identifies "
                "clusters and orphans. Stores rich traversal data in state['memory_traverse'] "
                "for use by goal_refine and agent_loop stages.",
)
async def dream_stage_memory_deep_traverse(
    state: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    state = state or {}
    cycle_id = state.get("cycle_id", "?")
    themes = state.get("themes", [])
    gather = state.get("gather", {})

    await emit_event({
        "type": "dream.stage.started", "cycle_id": cycle_id,
        "stage": "dream.stage.memory_deep_traverse",
    })

    traversal_results: List[Dict[str, Any]] = []
    orphans: List[Dict[str, Any]] = []
    clusters: List[Dict[str, Any]] = []

    # Start from memory_graph_walk sensor data if available
    walk_data = (gather.get("results", {}) or {}).get("memory_graph_walk", {})
    seed_node = walk_data.get("seed_node") if isinstance(walk_data, dict) else None

    # Also search memory for each theme
    mem_search_cap = CAPABILITY_REGISTRY.get("memory.search")
    mem_traverse_cap = CAPABILITY_REGISTRY.get("memory.traverse")
    mem_similar_cap = CAPABILITY_REGISTRY.get("memory.similar")
    mem_stats_cap = CAPABILITY_REGISTRY.get("memory.graph_stats")

    start_ids = []
    if seed_node and seed_node.get("id"):
        start_ids.append(seed_node["id"])

    # Find starting nodes from themes
    if mem_search_cap and themes:
        for theme in themes[:3]:
            try:
                res = await mem_search_cap["func"](query=theme, limit=3)
                for rec in (res or {}).get("results", []):
                    rid = rec.get("id") or (rec.get("record", {}) or {}).get("id")
                    if rid and rid not in start_ids:
                        start_ids.append(rid)
            except Exception:
                pass

    # Deep traverse from each starting node
    if mem_traverse_cap:
        for start_id in start_ids[:5]:
            try:
                trav = await mem_traverse_cap["func"](
                    start_id=start_id, depth=3, limit=20,
                )
                for item in (trav or {}).get("results", []):
                    node = item.get("node") or item.get("record") or item
                    if isinstance(node, dict) and node.get("id"):
                        has_edges = len(item.get("relations", [])) > 0 or item.get("depth", 0) > 0
                        entry = {
                            "id": node.get("id"),
                            "text": (node.get("text") or node.get("summary") or "")[:200],
                            "category": node.get("category", ""),
                            "depth": item.get("depth", 0),
                            "relation": item.get("relation", ""),
                            "has_edges": has_edges,
                        }
                        traversal_results.append(entry)
                        if not has_edges:
                            orphans.append(entry)
            except Exception:
                pass

    # Find semantic clusters — group traversal results by similarity
    if mem_similar_cap and traversal_results:
        # Pick 3 diverse nodes and find their neighbours
        sample_nodes = traversal_results[:3]
        for sn in sample_nodes:
            if sn.get("text"):
                try:
                    sim = await mem_similar_cap["func"](
                        query=sn["text"][:150], limit=5,
                    )
                    cluster_members = [sn["id"]]
                    for item in (sim or {}).get("results", []):
                        rec = item.get("record", item) if isinstance(item, dict) else {}
                        if isinstance(rec, dict) and rec.get("id"):
                            cluster_members.append(rec["id"])
                    if len(cluster_members) > 2:
                        clusters.append({
                            "anchor": sn["id"],
                            "anchor_text": sn["text"][:100],
                            "members": cluster_members,
                            "size": len(cluster_members),
                        })
                except Exception:
                    pass

    # Get graph stats for context
    graph_stats = {}
    if mem_stats_cap:
        try:
            graph_stats = await mem_stats_cap["func"]()
        except Exception:
            pass

    state["memory_traverse"] = {
        "traversed": len(traversal_results),
        "orphans": len(orphans),
        "clusters": len(clusters),
        "start_ids": start_ids,
        "results": traversal_results[:50],
        "orphan_list": orphans[:20],
        "cluster_list": clusters[:10],
        "graph_stats": graph_stats,
    }

    # Feed traversal data into the sample pool for goal_refine
    if not gather.get("results"):
        gather["results"] = {}
    gather["results"]["memory_deep_traverse"] = {
        "source": "memory_deep_traverse",
        "count": len(traversal_results),
        "signal": min(1.0, len(orphans) / 5.0 + len(clusters) / 3.0),
        "sample": [
            {"text": f"ORPHAN [{o['category']}]: {o['text']}", "id": o["id"], "role": "orphan"}
            for o in orphans[:10]
        ] + [
            {"text": f"CLUSTER ({c['size']} nodes) anchor: {c['anchor_text']}", "id": c["anchor"], "role": "cluster"}
            for c in clusters[:5]
        ],
    }

    await emit_event({
        "type": "dream.stage.completed", "cycle_id": cycle_id,
        "stage": "dream.stage.memory_deep_traverse",
        "traversed": len(traversal_results),
        "orphans": len(orphans),
        "clusters": len(clusters),
    })
    return state


# ─────────────────────────────────────────────────────────────────────────────
# STAGE: FABRIC EXPLORE (Phase 3 — deep fabric data exploration)
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.stage.fabric_explore", memory="off", silent=True,
    description="Dream pipeline stage: explore fabric datasets, run entity extraction "
                "on unprocessed records, and identify cross-dataset connections. "
                "Stores results in state['fabric_explore'].",
)
async def dream_stage_fabric_explore(
    state: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    state = state or {}
    cycle_id = state.get("cycle_id", "?")
    gather = state.get("gather", {})

    await emit_event({
        "type": "dream.stage.started", "cycle_id": cycle_id,
        "stage": "dream.stage.fabric_explore",
    })

    datasets_cap = CAPABILITY_REGISTRY.get("fabric.datasets")
    query_cap = CAPABILITY_REGISTRY.get("fabric.query")
    entity_cap = CAPABILITY_REGISTRY.get("fabric.entity_graph.snapshot")
    sources_cap = CAPABILITY_REGISTRY.get("fabric.sources")

    datasets: List[Dict[str, Any]] = []
    unprocessed: List[Dict[str, Any]] = []
    connections: List[Dict[str, Any]] = []

    # Get all datasets
    if datasets_cap:
        try:
            ds_result = await datasets_cap["func"]()
            datasets = (ds_result or {}).get("datasets", [])
        except Exception:
            pass

    # Find datasets with unprocessed content (no entity graph)
    if entity_cap and datasets:
        for ds in datasets[:10]:
            ds_id = ds.get("id") or ds.get("dataset_id", "")
            if not ds_id:
                continue
            try:
                eg = await entity_cap["func"](dataset_id=ds_id)
                entity_count = len((eg or {}).get("nodes", []))
                record_count = ds.get("record_count", 0) or ds.get("count", 0)
                if record_count > 0 and entity_count == 0:
                    unprocessed.append({
                        "dataset_id": ds_id,
                        "name": ds.get("name", ds_id),
                        "records": record_count,
                        "entities": entity_count,
                        "needs_extraction": True,
                    })
            except Exception:
                pass

    # Check for cross-dataset entity overlap
    if entity_cap and len(datasets) >= 2:
        entity_sets: Dict[str, set] = {}
        for ds in datasets[:6]:
            ds_id = ds.get("id") or ds.get("dataset_id", "")
            if not ds_id:
                continue
            try:
                eg = await entity_cap["func"](dataset_id=ds_id)
                entities = {n.get("label", "").lower() for n in (eg or {}).get("nodes", []) if n.get("label")}
                if entities:
                    entity_sets[ds_id] = entities
            except Exception:
                pass

        # Find overlaps
        ds_ids = list(entity_sets.keys())
        for i in range(len(ds_ids)):
            for j in range(i + 1, len(ds_ids)):
                overlap = entity_sets[ds_ids[i]] & entity_sets[ds_ids[j]]
                if overlap:
                    connections.append({
                        "from": ds_ids[i],
                        "to": ds_ids[j],
                        "shared_entities": list(overlap)[:10],
                        "count": len(overlap),
                    })

    state["fabric_explore"] = {
        "datasets": len(datasets),
        "unprocessed": unprocessed,
        "connections": connections,
    }

    # Feed into gather for goal_refine
    if not gather.get("results"):
        gather["results"] = {}
    gather["results"]["fabric_explore"] = {
        "source": "fabric_explore",
        "count": len(unprocessed) + len(connections),
        "signal": min(1.0, (len(unprocessed) + len(connections)) / 5.0),
        "sample": [
            {"text": f"UNPROCESSED dataset '{u['name']}': {u['records']} records, 0 entities",
             "id": u["dataset_id"], "role": "unprocessed"}
            for u in unprocessed[:5]
        ] + [
            {"text": f"CONNECTION {c['from']} <-> {c['to']}: {c['count']} shared entities ({', '.join(c['shared_entities'][:3])})",
             "role": "connection"}
            for c in connections[:5]
        ],
    }

    await emit_event({
        "type": "dream.stage.completed", "cycle_id": cycle_id,
        "stage": "dream.stage.fabric_explore",
        "datasets": len(datasets),
        "unprocessed": len(unprocessed),
        "connections": len(connections),
    })
    return state


# ─────────────────────────────────────────────────────────────────────────────
# STAGE: GOAL REFINE (Phase 1)
# ─────────────────────────────────────────────────────────────────────────────
# Sits between dream.stage.themes and dream.stage.agent_loop.
# Distils raw themes + sensor data into ONE specific, actionable goal.

@capability(
    "dream.stage.goal_refine", memory="off", silent=True,
    description="Dream pipeline stage: refine raw themes and sensor data into ONE "
                "specific, actionable goal sentence for the agent loop. "
                "Place between dream.stage.themes and dream.stage.agent_loop. "
                "Stores result in state['refined_goal'] which the agent_loop and "
                "investigate stages will use as their goal.",
)
async def dream_stage_goal_refine(
    state: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    state = state or {}
    trig = state.get("trigger", {})
    seed = state.get("seed") or {}
    themes = state.get("themes", [])
    gather = state.get("gather", {})
    cycle_id = state.get("cycle_id", "?")

    await emit_event({
        "type":     "dream.stage.started",
        "cycle_id": cycle_id,
        "stage":    "dream.stage.goal_refine",
    })

    # Collect concrete data points from sensor results
    data_points: List[str] = []
    for sname, sres in (gather.get("results", {}) or {}).items():
        if not isinstance(sres, dict):
            continue
        for item in (sres.get("sample") or [])[:5]:
            if isinstance(item, dict):
                text = (item.get("text") or item.get("msg") or
                        item.get("title") or item.get("query") or "")
                if text:
                    data_points.append(f"[{sname}] {str(text)[:200]}")

    # Get the whitelist so we can tell the LLM what tools are available
    whitelist = trig.get("whitelist") or []
    if not whitelist:
        whitelist = await _get_whitelist()
    actionable = [c for c in whitelist
                  if not c.startswith("dream.sensor.")
                  and not c.startswith("dream.stage.")]

    focus = (seed.get("focus_topic") or "").strip()
    trigger_prompt = trig.get("prompt", "")

    system = (
        "You are a goal-refinement agent for an autonomous background system. "
        "Your job is to turn vague themes and raw sensor data into ONE specific, "
        "actionable goal sentence that a tool-using agent can accomplish. "
        "The goal must reference specific data (IDs, names, topics) from the "
        "sensor input — never be generic. "
        "If the sensor data is empty or useless, say SKIP (just that word). "
        "Reply with ONLY the goal sentence, nothing else."
    )

    prompt_parts = [
        f"TRIGGER CONTEXT: {trigger_prompt[:500]}",
    ]
    if focus:
        prompt_parts.append(f"USER FOCUS: {focus}")
    if themes:
        prompt_parts.append(f"THEMES DETECTED: {', '.join(themes[:8])}")
    if data_points:
        prompt_parts.append("CONCRETE DATA FROM SENSORS:")
        prompt_parts.extend(data_points[:20])
    if actionable:
        prompt_parts.append(f"AVAILABLE TOOLS: {', '.join(actionable[:30])}")
    prompt_parts.append(
        "\nBased on the above, write ONE specific goal sentence. "
        "Reference specific IDs, topics, or data points. "
        "Example good goals:\n"
        "- 'Memory node abc123 about Vera DAG engine has 0 edges — use "
        "memory.traverse and memory.similar to find related nodes'\n"
        "- 'Dataset rss_tech_news has 12 new items about LLM deployment — "
        "run nlp.run entity extraction on the top 3 and store entities in the graph'\n"
        "- 'Research job job_xyz about distributed systems is incomplete — "
        "use research.expand to continue from where it left off'\n"
        "Example BAD goals (too vague):\n"
        "- 'Explore recent activity'\n"
        "- 'Find interesting patterns'\n"
        "- 'Investigate the data'"
    )

    prompt = "\n".join(prompt_parts)
    refined = await _llm_generate(prompt, system=system)
    refined = (refined or "").strip()

    if refined.upper().startswith("SKIP") or not refined or len(refined) < 10:
        state["refined_goal"] = None
        state["goal_refine"] = {
            "skipped": True,
            "reason": "sensor data insufficient for specific goal",
            "raw_response": refined[:200],
        }
        await emit_event({
            "type":     "dream.stage.completed",
            "cycle_id": cycle_id,
            "stage":    "dream.stage.goal_refine",
            "skipped":  True,
        })
        return state

    state["refined_goal"] = refined
    state["goal_refine"] = {
        "goal":        refined,
        "themes_used": themes[:8],
        "data_points": len(data_points),
        "tools_shown": len(actionable),
    }

    await emit_event({
        "type":     "dream.stage.completed",
        "cycle_id": cycle_id,
        "stage":    "dream.stage.goal_refine",
        "goal":     refined[:200],
    })

    return state


@capability(
    "dream.stage.plan", memory="off", silent=True,
    description="Dream pipeline stage 3: ask the DAG workshop planner for a DAG constrained to the dream whitelist.",
)
async def dream_stage_plan(state: Optional[Dict[str, Any]] = None, trace_id=None):
    state = state or {}
    trig = state.get("trigger", {})
    if trig.get("mode") == "synthesize_only":
        state["plan"] = {"skipped": True, "reason": "synthesize_only"}
        return state

    # Build effective whitelist — trigger overrides global, filter out dream/obs/ui clutter
    trig_wl = trig.get("whitelist") or []
    global_wl = await _get_whitelist()
    whitelist = trig_wl if trig_wl else global_wl
    _EXCLUDE = {"dream.", "obs.", "health.", "ui.", "caps.", "mcp.", "echo"}
    whitelist = [
        c for c in whitelist
        if c in CAPABILITY_REGISTRY and not any(c.startswith(p) for p in _EXCLUDE)
    ]
    if not whitelist:
        state["plan"] = {"error": "whitelist empty after filtering"}
        return state

    themes = state.get("themes", [])
    gather = state.get("gather", {})
    seed = state.get("seed") or {}

    # Build sensor digest for the goal
    sensor_digest: List[str] = []
    for sname, sres in (gather.get("results", {}) or {}).items():
        if isinstance(sres, dict):
            cnt = sres.get("count", 0)
            sig = sres.get("signal", 0)
            previews = []
            for item in (sres.get("sample") or [])[:3]:
                if isinstance(item, dict):
                    txt = (item.get("text") or item.get("msg") or item.get("query") or "")
                    if txt:
                        previews.append(str(txt)[:120])
            sensor_digest.append(f"  {sname}: {cnt} items (signal {sig})"
                                 + (f" — e.g. {'; '.join(previews)}" if previews else ""))

    focus = (seed.get("focus_topic") or "").strip()
    extra_prompt = (seed.get("extra_prompt") or "").strip()

    goal = (
        f"{trig.get('prompt') or 'Explore the most interesting recent signal and propose a next step.'}\n\n"
        + (f"FOCUS: {focus}\n\n" if focus else "")
        + (f"ADDITIONAL: {extra_prompt}\n\n" if extra_prompt else "")
        + f"Themes: {', '.join(themes) if themes else '(none)'}\n"
        + f"Signal: {gather.get('signal', 0)}\n"
        + f"Sensors:\n" + "\n".join(sensor_digest or ["  (none)"]) + "\n\n"
        + "Build a SHORT DAG (2-4 steps). Do NOT include dream.sensor.* or dream.stage.* caps."
    )

    # Use the dag.plan capability — same code path as the working DAG workshop
    dag_plan_cap = CAPABILITY_REGISTRY.get("dag.plan")
    if not dag_plan_cap:
        # Fallback to direct plan_dag
        plan_fn = getattr(_orch, "plan_dag", None)
        if not plan_fn:
            state["plan"] = {"error": "neither dag.plan cap nor plan_dag function available"}
            return state
        try:
            plan = await plan_fn(goal, available_caps=whitelist)
        except Exception as e:
            plan = {"error": f"plan_dag failed: {e}", "dag": []}
    else:
        try:
            plan = await dag_plan_cap["func"](goal=goal, capabilities=whitelist)
        except Exception as e:
            plan = {"error": f"dag.plan failed: {e}", "dag": []}

    if not isinstance(plan, dict):
        plan = {"error": "planner returned non-dict", "dag": []}

    # Validate DAG structure
    dag = plan.get("dag", [])
    if isinstance(dag, list) and dag:
        valid = []
        for node in dag:
            if isinstance(node, list) and node:
                cap_name = node[0] if isinstance(node[0], str) else None
                if isinstance(node[0], list):
                    # Parallel group
                    subs = [s for s in node if isinstance(s, list) and s
                            and isinstance(s[0], str) and s[0] in CAPABILITY_REGISTRY]
                    if subs:
                        valid.append(subs)
                elif cap_name and cap_name in CAPABILITY_REGISTRY:
                    valid.append(node)
        plan["dag"] = valid

    if not plan.get("dag"):
        plan["error"] = plan.get("error") or "planner produced no valid DAG nodes"
        log.warning("dream plan: no valid DAG — raw: %s",
                     str(plan.get("raw", plan.get("rationale", "")))[:300])

    state["plan"] = plan
    return state


_YES_TOKENS = {
    "y", "yes", "yep", "yeah", "yup", "ya", "yea",
    "ok", "okay", "k", "kk",
    "go", "proceed", "sure", "fine", "alright",
    "approve", "approved", "ack", "acknowledge", "acknowledged",
    "confirm", "confirmed", "do", "doit", "yes!", "👍", "✓", "✅", "👌", "🆗",
}
_NO_TOKENS = {
    "n", "no", "nope", "nah", "skip", "cancel", "abort", "stop",
    "deny", "denied", "reject", "rejected", "decline", "declined",
    "👎", "❌", "✗",
}

def _is_yes(text: str) -> bool:
    """Lenient yes detector — handles punctuation, case, emoji, and 'do it'."""
    if not text:
        return False
    # Strip whitespace and trailing/leading punctuation; lowercase.
    t = text.strip().lower()
    # Take only first line
    t = t.split("\n", 1)[0].strip()
    # Strip trailing punctuation but keep emoji / multibyte chars
    t = t.rstrip(".!?,:;)( ").lstrip("(.,:;) ")
    if not t:
        return False
    if t in _YES_TOKENS:
        return True
    # First whitespace-separated token
    first = t.split()[0] if t.split() else ""
    if first in _YES_TOKENS:
        return True
    # Phrases
    if t.startswith(("yes ", "yep ", "yeah ", "ok ", "okay ", "go ahead",
                     "do it", "let's", "lets ", "please do", "approve",
                     "sure ", "proceed", "sounds good", "lgtm")):
        return True
    return False


def _is_no(text: str) -> bool:
    if not text:
        return False
    t = text.strip().lower().split("\n", 1)[0].strip()
    t = t.rstrip(".!?,:;)( ").lstrip("(.,:;) ")
    if not t:
        return False
    if t in _NO_TOKENS:
        return True
    first = t.split()[0] if t.split() else ""
    if first in _NO_TOKENS:
        return True
    if t.startswith(("no ", "nope ", "skip", "don't", "do not", "cancel",
                     "abort", "not now", "later", "stop")):
        return True
    return False


async def _tg_admin_chat_id() -> str:
    cap = CAPABILITY_REGISTRY.get("tg.config.get")
    if not cap:
        return ""
    try:
        res = await cap["func"]()
        if isinstance(res, dict):
            cfg = res.get("config", {}) or {}
            return str(cfg.get("admin_chat_id") or "").strip()
    except Exception:
        pass
    return ""


async def _wait_for_hitl_reply(
    chat_id: str,
    started_at: float,
    timeout_s: float,
    cycle_id: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Wait for either a Telegram reply OR a UI/API response posted via
    dream.hitl.respond. Returns:
      {"source":"tg"|"ui", "text": "...", "approved": bool, "edits": dict|None}
    or None on timeout / cancel.

    Polls every 2 seconds. UI responses land in Redis hash KEY_HITL_RESP.
    """
    tg_hist = CAPABILITY_REGISTRY.get("tg.history")
    r = _redis()
    deadline = started_at + float(timeout_s)
    # Build candidate keys to check on each poll
    keys_to_check = [cycle_id] if cycle_id else []
    if cycle_id and ":step" in cycle_id:
        parent = cycle_id.split(":step", 1)[0]
        keys_to_check.append(parent)
    while time.time() < deadline:
        if _CYCLE_CANCEL:
            return None

        # 1) Check the UI response slot — try both the exact key and the parent
        # cycle_id (so it doesn't matter which form the panel sent back).
        if r and keys_to_check:
            for ck in keys_to_check:
                try:
                    raw = await r.hget(KEY_HITL_RESP, ck)
                    if raw:
                        payload = json.loads(raw if isinstance(raw, str) else raw.decode())
                        # Consume — delete so it can't be re-read
                        try:
                            await r.hdel(KEY_HITL_RESP, ck)
                        except Exception:
                            pass
                        return {
                            "source":   "ui",
                            "text":     str(payload.get("text", "")),
                            "approved": bool(payload.get("approved", False)),
                            "edits":    payload.get("edits") or None,
                        }
                except Exception:
                    pass

        # 2) Check for a fresh Telegram reply
        if tg_hist and chat_id:
            try:
                res = await tg_hist["func"](chat_id=chat_id, limit=20)
                msgs = (res or {}).get("messages", []) if isinstance(res, dict) else []
                for m in msgs:
                    if m.get("from") != "user":
                        continue
                    if not m.get("ts"):
                        continue
                    try:
                        ts_dt = datetime.fromisoformat(m["ts"].replace("Z", "+00:00"))
                        if ts_dt.timestamp() > started_at:
                            txt = str(m.get("text") or "")
                            if _is_yes(txt):
                                return {"source": "tg", "text": txt,
                                        "approved": True, "edits": None}
                            if _is_no(txt):
                                return {"source": "tg", "text": txt,
                                        "approved": False, "edits": None}
                            # Non yes/no message — keep polling, ignore noise
                    except Exception:
                        continue
            except Exception:
                pass

        await asyncio.sleep(2)
    return None


@capability(
    "dream.stage.execute", memory="off", silent=True,
    description="Dream pipeline stage 4: optionally ask for HITL approval then execute the planned DAG.",
)
async def dream_stage_execute(state: Optional[Dict[str, Any]] = None, trace_id=None):
    state = state or {}
    trig = state.get("trigger", {})
    plan = state.get("plan", {}) or {}

    if plan.get("skipped") or plan.get("error") or not plan.get("dag"):
        state["execute"] = {"skipped": True, "reason": plan.get("reason") or plan.get("error") or "no dag"}
        return state

    dag = plan.get("dag", [])
    initial = plan.get("initial_state", {}) or {}
    rationale = plan.get("rationale", "")

    # Validate each node in the dag — must be [cap_name, ...] where cap_name is
    # a string that exists in the capability registry
    valid_nodes: List[Any] = []
    for node in dag:
        if isinstance(node, list) and node:
            if isinstance(node[0], str) and node[0] in CAPABILITY_REGISTRY:
                valid_nodes.append(node)
            elif isinstance(node[0], list):
                # Parallel group — validate each sub-node
                valid_subs = [s for s in node if isinstance(s, list) and s
                              and isinstance(s[0], str) and s[0] in CAPABILITY_REGISTRY]
                if valid_subs:
                    valid_nodes.append(valid_subs)
    if not valid_nodes:
        state["execute"] = {
            "skipped": True,
            "reason":  "DAG nodes reference unknown capabilities",
            "raw_dag": dag[:5],
            "rationale": rationale,
        }
        return state
    dag = valid_nodes

    if trig.get("hitl"):
        cfg = await _get_config()
        admin = await _tg_admin_chat_id()
        cycle_id = state.get("cycle_id", "?")

        # Build a human-readable view of the planned steps
        step_lines: List[str] = []
        for i, node in enumerate(dag):
            if isinstance(node, list) and node:
                cap_name = node[0] if isinstance(node[0], str) else "[parallel]"
                out_key = node[1] if len(node) > 1 else ""
                step_lines.append(f"  {i+1}. {cap_name}" + (f" → {out_key}" if out_key else ""))

        question_md = (
            f"💭 *I've been thinking about {trig.get('label', trig.get('name','something'))}*\n\n"
            f"{rationale or 'Would you like me to act on it?'}\n\n"
            f"*Planned steps ({len(dag)}):*\n" + "\n".join(step_lines or ["  (no steps)"]) + "\n\n"
            f"Reply *yes* to proceed, *no* to skip "
            f"(or use the panel to accept / reject / edit)."
        )
        question_short = question_md.split("\n\n", 2)[0] + " — " + (rationale or "")

        # Pending record — stores enough for the UI to render a rich approval card
        pending_rec = {
            "cycle_id":  cycle_id,
            "trigger":   trig.get("name"),
            "label":     trig.get("label"),
            "chat_id":   admin,
            "question":  question_md,
            "rationale": rationale,
            "dag":       dag,
            "initial_state": initial,
            "step_lines":    step_lines,
            "asked_at":  now_iso(),
            "timeout_s": float(cfg.get("default_hitl_timeout_s", 600)),
        }
        r = _redis()
        if r:
            try:
                await r.hset(KEY_HITL, cycle_id, json.dumps(pending_rec))
            except Exception:
                pass

        # Emit event so the UI (and any other subscriber) can show a notification
        await emit_event({
            "type":     "dream.hitl.requested",
            "cycle_id": cycle_id,
            "trigger":  trig.get("name"),
            "label":    trig.get("label"),
            "rationale": rationale,
            "step_count": len(dag),
            "step_lines": step_lines,
            "question_short": question_short[:300],
            "timeout_s": pending_rec["timeout_s"],
            "telegram_sent": False,  # updated below if it succeeds
        })

        # Try Telegram (optional)
        tg_notify = CAPABILITY_REGISTRY.get("tg.notify")
        tg_sent = False
        if cfg.get("telegram_bridge") and admin and tg_notify:
            try:
                tg_res = await tg_notify["func"](text=question_md)
                tg_sent = bool(isinstance(tg_res, dict) and tg_res.get("ok"))
            except Exception as e:
                log.debug("dream hitl notify: %s", e)

        if tg_sent:
            await emit_event({"type": "dream.hitl.telegram_sent",
                              "cycle_id": cycle_id, "trigger": trig.get("name")})

        asked_at = time.time()
        timeout_s = pending_rec["timeout_s"]
        reply = await _wait_for_hitl_reply(admin or "", asked_at, timeout_s,
                                           cycle_id=cycle_id)

        # Clear pending entry
        if r:
            try:
                await r.hdel(KEY_HITL, cycle_id)
            except Exception:
                pass

        if reply is None:
            # Timeout / cancel
            await emit_event({"type": "dream.hitl.timeout",
                              "cycle_id": cycle_id, "trigger": trig.get("name")})
            try:
                if tg_notify and tg_sent:
                    await tg_notify["func"](text="(HITL timed out — skipping.)")
            except Exception:
                pass
            state["execute"] = {"skipped": True, "reason": "hitl_timeout"}
            return state

        approved = bool(reply.get("approved"))
        edits = reply.get("edits") or {}

        await emit_event({
            "type":     "dream.hitl.responded",
            "cycle_id": cycle_id,
            "trigger":  trig.get("name"),
            "approved": approved,
            "source":   reply.get("source"),
            "edits":    bool(edits),
        })

        if not approved:
            try:
                if tg_notify and tg_sent:
                    await tg_notify["func"](text="OK — I'll let it go.")
            except Exception:
                pass
            state["execute"] = {
                "skipped": True,
                "reason":  "hitl_declined",
                "reply":   reply,
            }
            return state

        # Apply edits if the UI provided any (e.g. trimmed dag, modified initial_state)
        if isinstance(edits, dict):
            if isinstance(edits.get("dag"), list) and edits["dag"]:
                dag = edits["dag"]
                plan["dag"] = dag
            if isinstance(edits.get("initial_state"), dict):
                initial = {**initial, **edits["initial_state"]}
                plan["initial_state"] = initial

        try:
            if tg_notify and tg_sent:
                await tg_notify["func"](text=f"✓ Approved — running {len(dag)} step(s).")
        except Exception:
            pass

    # Use the dag.run capability — same execution path as the DAG workshop
    dag_run_cap = CAPABILITY_REGISTRY.get("dag.run")
    if dag_run_cap:
        try:
            exec_state = dict(initial)
            result = await dag_run_cap["func"](
                dag=dag, state=exec_state, supervised=True,
            )
            run_result = result.get("result", result) if isinstance(result, dict) else {}
            state["execute"] = {
                "ran":   True,
                "steps": len(dag),
                "dag":   dag,
                "initial_state": initial,
                "state": run_result if isinstance(run_result, dict) else {"result": str(run_result)},
            }
        except Exception as e:
            state["execute"] = {"error": f"dag.run failed: {e}", "dag": dag}
    else:
        # Fallback: use run_graph directly
        run_fn = getattr(_orch, "run_graph", None)
        if run_fn:
            try:
                exec_state = dict(initial)
                result = await run_fn(dag, exec_state, trace_id=trace_id or "")
                state["execute"] = {
                    "ran": True, "steps": len(dag), "dag": dag,
                    "state": result if isinstance(result, dict) else {"result": str(result)},
                }
            except Exception as e:
                state["execute"] = {"error": f"run_graph failed: {e}", "dag": dag}
        else:
            # Last resort: manual sequential execution
            exec_state = dict(initial)
            step_results = []
            for node in dag:
                if isinstance(node, list) and node:
                    cap_name = node[0]
                    out_key = node[1] if len(node) > 1 else None
                    try:
                        r = await _call_cap(cap_name, **exec_state)
                        if out_key:
                            exec_state[out_key] = r
                        step_results.append({"cap": cap_name, "ok": True,
                                             "preview": str(r)[:200]})
                    except Exception as e:
                        step_results.append({"cap": cap_name, "ok": False,
                                             "error": str(e)})
            state["execute"] = {"ran": True, "steps": len(dag), "dag": dag,
                                "state": exec_state, "step_results": step_results}

    # Emit execution results for the panel
    await emit_event({
        "type":     "dream.execute.completed",
        "cycle_id": state.get("cycle_id", "?"),
        "trigger":  trig.get("name"),
        "ran":      bool(state.get("execute", {}).get("ran")),
        "error":    state.get("execute", {}).get("error"),
        "steps":    len(dag),
    })

    return state


@capability(
    "dream.stage.synthesize", memory="off", silent=True,
    description="Dream pipeline stage 5: ask the LLM to write a concise synthesis of the dream cycle.",
)
async def dream_stage_synthesize(state: Optional[Dict[str, Any]] = None, trace_id=None):
    state = state or {}
    trig = state.get("trigger", {})
    seed = state.get("seed") or {}
    themes = state.get("themes", [])
    gather = state.get("gather", {})
    execute = state.get("execute", {})
    stepwise = state.get("stepwise") or {}
    cycle_id = state.get("cycle_id", "?")

    depth = (trig.get("depth") or "standard").lower()
    if depth not in ("brief", "standard", "deep", "exhaustive"):
        depth = "standard"

    # ── Early exit: if gather produced no real signal, refuse to synthesize
    # to avoid the LLM hallucinating content that doesn't exist.
    gather_signal = float((gather or {}).get("signal", 0.0) or 0.0)
    gather_results = (gather or {}).get("results", {}) or {}
    total_items = sum(
        int((v or {}).get("count", 0) or 0)
        for v in gather_results.values()
        if isinstance(v, dict)
    )
    if gather_signal < 0.05 and total_items == 0:
        # Build a diagnostic so the user knows WHICH sensors had nothing
        sensor_diag = []
        for sname, sres in gather_results.items():
            if not isinstance(sres, dict):
                continue
            err = sres.get("error") or sres.get("note") or "0 items"
            sensor_diag.append(f"- `{sname}` — {err}")
        note = (
            f"# {trig.get('label', trig.get('name', 'Dream'))} — no signal\n\n"
            f"All sensors returned no usable data. Skipping synthesis to avoid fabricating content.\n\n"
            f"## Sensor diagnostic\n" + "\n".join(sensor_diag) +
            f"\n\n## What to check\n"
            f"- Are the underlying capabilities loaded? (`memory.all_nodes`, `fabric.query`, etc.)\n"
            f"- Are the upstream services (Postgres, Neo4j, Chroma, fabric) reachable?\n"
            f"- Is there actually any recent activity to detect?\n\n"
            f"_Cycle: {cycle_id} · combined signal: {gather_signal:.2f}_"
        )
        state["report"] = note
        state["title"]  = f"{trig.get('label', 'Dream')} — no signal"
        state["depth"]  = depth
        return state

    # Sample budget per depth — how many sensor results and items to include
    sample_caps = {
        "brief":      {"sensors": 4,  "items": 4,  "chars": 180},
        "standard":   {"sensors": 8,  "items": 8,  "chars": 240},
        "deep":       {"sensors": 16, "items": 14, "chars": 380},
        "exhaustive": {"sensors": 32, "items": 24, "chars": 600},
    }[depth]

    sample_lines: List[str] = []
    sensor_items = list((gather.get("results", {}) or {}).items())[:sample_caps["sensors"]]
    for sname, sres in sensor_items:
        if not isinstance(sres, dict):
            continue
        sample_lines.append(f"### {sname}  (signal {sres.get('signal', 0)}, count {sres.get('count', '?')})")
        for item in (sres.get("sample") or [])[:sample_caps["items"]]:
            if isinstance(item, dict):
                # Prefer richer field selection for deeper output
                txt = (item.get("text") or item.get("title") or item.get("message")
                       or item.get("headline") or item.get("summary") or "")
                meta_bits = []
                if item.get("category"): meta_bits.append(str(item["category"]))
                if item.get("ts"):       meta_bits.append(str(item["ts"])[:19])
                if item.get("dataset"):  meta_bits.append(str(item["dataset"]))
                meta = (" [" + " · ".join(meta_bits) + "]") if meta_bits else ""
                if txt:
                    sample_lines.append(f"- {str(txt)[:sample_caps['chars']]}{meta}")
            elif isinstance(item, str):
                sample_lines.append(f"- {item[:sample_caps['chars']]}")

    # Stepwise activity also feeds the synthesizer
    stepwise_lines: List[str] = []
    if stepwise.get("steps"):
        for s in stepwise.get("steps", [])[:30]:
            cap_n = s.get("cap", "?")
            preview = str(s.get("preview", ""))[:200]
            ok = "✓" if s.get("ok") else "✗"
            stepwise_lines.append(f"- {ok} {cap_n} → {preview}")

    # System prompt scales with depth
    # CRITICAL: every depth gets a strict anti-hallucination preamble. The synthesizer
    # must only reflect on data that's actually in the prompt — never invent topics
    # like "machine learning for climate change" when sensors returned nothing.
    _ANTI_HALLU = (
        "STRICT GROUNDING RULES — these override everything else:\n"
        "1. You may ONLY discuss content that appears in the 'Signal samples' section below. "
        "Do not invent topics, themes, papers, projects, or activities not explicitly present in the data.\n"
        "2. If signal samples are empty or trivial, write a single short note saying so — do not fabricate content to fill space.\n"
        "3. Do NOT introduce subjects like 'machine learning for climate', 'AI advancements', "
        "'recent papers', or any other generic topic unless those exact subjects appear verbatim in the signal samples.\n"
        "4. Quote or directly reference sensor entries when making observations.\n\n"
    )
    depth_systems = {
        "brief": _ANTI_HALLU + (
            "You are Vera, reflecting quietly. Write a TIGHT 3–6 sentence note in "
            "markdown grounded in the actual data above. Start with an H1 title. Skip filler. "
            "If the data is thin, say so honestly in one line."
        ),
        "standard": _ANTI_HALLU + (
            "You are Vera, reflecting quietly during an idle moment. Write a useful "
            "synthesis in clean markdown grounded ONLY in the data shown — start with an H1 "
            "title that names a real subject from the data, use ## subsections if appropriate, "
            "and a final 'Recommended next steps' bullet list when anything actionable was "
            "actually present. Be specific, not performative. If sensors returned little or "
            "nothing, write a short honest acknowledgment instead of padding."
        ),
        "deep": _ANTI_HALLU + (
            "You are Vera, producing a thorough analytical brief grounded ONLY in the data shown. "
            "Write detailed markdown starting with an H1 title and a one-paragraph executive "
            "summary that references actual entries. Then use ## sections for: Key observations, "
            "Patterns and themes, Notable details (with quotes/snippets from the actual data), "
            "Risks or anomalies, and Recommended next steps (numbered). Cite specific sensor "
            "entries inline. 600–1200 words IF the data supports it — much shorter if it doesn't."
        ),
        "exhaustive": _ANTI_HALLU + (
            "You are Vera, producing an in-depth research-grade analysis grounded ONLY in the "
            "data shown. Markdown: H1 title (naming a real subject from the data), executive "
            "summary paragraph, then ## sections for Background, Each thematic cluster (one ## "
            "per theme actually present with detailed exposition), Cross-cutting patterns, "
            "Anomalies and outliers, Specific evidence with quotes (from real entries), Open "
            "questions, Recommended next steps (numbered, concrete), and Followup ideas. "
            "Cite sensor entries by name. 1200–2500 words IF the data supports it — much "
            "shorter and more honest if it doesn't."
        ),
    }
    system = depth_systems[depth]

    # Output style: any pipeline's synthesize (emit) stage can adopt one of the
    # shared output styles (docs / critique / improvement / integration /
    # architecture) — the same palette the source review uses — so the style
    # selection drives dream synthesis too. Configurable via stage_config.
    # synthesize.output_style, trigger.output_style, or seed.output_style.
    out_style = (seed.get("output_style")
                 or (trig.get("stage_config", {}) or {}).get("synthesize", {}).get("output_style")
                 or trig.get("output_style") or "").strip()
    if out_style in REVIEW_STYLES:
        sdef = REVIEW_STYLES[out_style]
        system = (_ANTI_HALLU + sdef["system"] + " " + sdef["instruction"]
                  + " Produce this as your deliverable, grounded ONLY in the data "
                    "shown below; if the data is thin, say so honestly.")
        state["output_style"] = out_style

    focus = (seed.get("focus_topic") or "").strip()
    extra_prompt = (seed.get("extra_prompt") or "").strip()
    focus_block = ""
    if focus:
        focus_block += f"\n\nPrimary focus: {focus}"
    if extra_prompt:
        focus_block += f"\n\nAdditional guidance: {extra_prompt}"

    exec_summary = ""
    if execute.get("ran"):
        exec_summary = f"DAG execution: completed ({execute.get('steps','?')} steps)"
    elif stepwise.get("steps"):
        exec_summary = f"Stepwise execution: {len(stepwise.get('steps',[]))} steps, " \
                       f"{sum(1 for s in stepwise['steps'] if s.get('ok'))} ok"
    elif execute.get("reason"):
        exec_summary = f"Execution skipped: {execute.get('reason')}"
    else:
        exec_summary = "No execution stage in this pipeline."

    prompt_parts = [
        f"{trig.get('prompt', 'Synthesize the recent activity.')}{focus_block}",
        "",
        f"Depth: {depth}",
        f"Themes detected: {', '.join(themes) if themes else '(none)'}",
        exec_summary,
        "",
        "Signal samples (most recent):",
        "\n".join(sample_lines[:200]),
    ]
    if stepwise_lines:
        prompt_parts.append("")
        prompt_parts.append("Stepwise actions performed:")
        prompt_parts.append("\n".join(stepwise_lines))

    # Iteration findings — produced by dream.stage.investigate across iterations
    iter_findings = state.get("findings") or []
    iter_state = state.get("iterate") or {}
    if iter_findings:
        prompt_parts.append("")
        completed = iter_state.get("completed", 0)
        prompt_parts.append(
            f"Investigation findings ({len(iter_findings)} entries across "
            f"{completed} iterations — USE THESE, they are real tool-derived data):"
        )
        for f in iter_findings[:30]:
            topic = f.get("topic", "?")
            content = f.get("content", "")[:600]
            source = f.get("source", "")
            it = f.get("iter")
            line = f"  - [iter {it}] [{topic}]" if it is not None else f"  - [{topic}]"
            if source:
                line += f" (via `{source}`)"
            line += f": {content}"
            prompt_parts.append(line)
        if iter_state.get("reason"):
            prompt_parts.append(f"\nIteration halt reason: {iter_state['reason']}")

    prompt = "\n".join(prompt_parts)

    # ── Output style (Phase 2) ───────────────────────────────────────────
    # Supports: quick, short, standard, long, exhaustive, audio
    # The style adjusts the system prompt and max tokens to produce
    # appropriately-sized output.
    output_style = trig.get("output_style") or seed.get("output_style") or ""
    if output_style == "quick":
        system += ("\n\nOUTPUT STYLE: QUICK — respond in 2-4 sentences maximum. "
                   "No markdown headers. Just the key insight.")
    elif output_style == "short":
        system += ("\n\nOUTPUT STYLE: SHORT — respond in 1-2 short paragraphs (50-150 words). "
                   "One optional header. Punchy and direct.")
    elif output_style == "long":
        system += ("\n\nOUTPUT STYLE: LONG — detailed analysis with multiple sections. "
                   "800-1500 words. Use ## headers for each section.")
    elif output_style == "audio":
        system += ("\n\nOUTPUT STYLE: AUDIO-READY — write as if this will be read aloud. "
                   "No markdown formatting, no bullet points, no headers. "
                   "Use natural speech patterns, short sentences, and clear transitions. "
                   "150-300 words. Start directly with content, no preamble.")

    # Stream tokens out for the panel and any other live subscriber.
    report = await _llm_generate_streaming(
        prompt, system=system, cycle_id=cycle_id, stage="synthesize",
    )
    if not report:
        report = f"*Dream cycle {trig.get('name','?')} produced no synthesis.*"

    # Extract / generate a short title for the dream record
    title = _extract_title(report)
    if not title:
        title = await _llm_title(report, themes, trig)

    state["report"] = report
    state["title"]  = title
    state["depth"]  = depth
    return state


def _extract_title(report: str) -> str:
    """Pull a title from the first markdown H1/H2 if present."""
    if not report:
        return ""
    for line in report.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s.lstrip("# ").strip()[:120]
        if s.startswith("## "):
            return s.lstrip("# ").strip()[:120]
    # First non-blank line as a fallback
    for line in report.splitlines():
        s = line.strip()
        if s and not s.startswith(("```", "---", "*", "_", "-")):
            return s[:120]
    return ""


async def _llm_title(report: str, themes: List[str], trig: Dict[str, Any]) -> str:
    """Ask the LLM for a 4–8 word title."""
    if not report:
        return f"{trig.get('label', trig.get('name','dream'))} — empty"
    prompt = (
        "Write a single concise title (4–8 words, Title Case, no punctuation, "
        "no quotes) summarising the dream below. Reply with the title only.\n\n"
        f"Themes: {', '.join(themes) if themes else '(none)'}\n\n"
        f"Dream:\n{report[:1500]}"
    )
    try:
        raw = await _llm_generate(prompt, system="You name documents concisely.")
        line = (raw or "").strip().splitlines()[0] if raw else ""
        line = line.strip(' "\'`*#').strip()
        if line:
            return line[:120]
    except Exception:
        pass
    return f"{trig.get('label', trig.get('name','dream'))} — {now_iso()[:16]}"


# ─────────────────────────────────────────────────────────────────────────────
# AGENTIC STAGES — enrich, propose, quality-check
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.stage.enrich_context", memory="off", silent=True,
    description="Dream pipeline stage: ask the LLM to identify what additional information "
                "would help, then attempt to fetch it via memory.search / fabric.query / "
                "research.quick_search. Result enriches state['enriched'].",
)
async def dream_stage_enrich_context(state: Optional[Dict[str, Any]] = None, trace_id=None):
    state = state or {}
    trig = state.get("trigger", {})
    seed = state.get("seed") or {}
    gather = state.get("gather", {})
    cycle_id = state.get("cycle_id", "?")

    # Build a short summary of what we already have
    sensor_summary = []
    for sname, sres in (gather.get("results", {}) or {}).items():
        if isinstance(sres, dict):
            sensor_summary.append(f"- {sname}: {sres.get('count', 0)} items, signal {sres.get('signal', 0)}")
    summary_text = "\n".join(sensor_summary) or "(no sensor data)"

    focus = (seed.get("focus_topic") or "").strip()
    proj_ctx = (seed.get("project_context") or "").strip()

    prompt = (
        f"Goal: {trig.get('prompt', '(no prompt)')}\n"
        + (f"Focus: {focus}\n" if focus else "")
        + (f"Project context:\n{proj_ctx[:1500]}\n\n" if proj_ctx else "")
        + f"Already gathered:\n{summary_text}\n\n"
        + "Identify ONE specific missing piece of information that would meaningfully help. "
        + "Reply with a JSON object:\n"
        + '  {"need": "<short description>", "search_query": "<query>", "source": "memory"|"fabric"|"web"}\n'
        + 'Or {"need": null} if nothing meaningful is missing.'
    )
    raw = await _llm_generate(prompt, system="You identify information gaps. JSON only.")
    enriched = []
    try:
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e != -1:
            need = json.loads(raw[s:e+1])
            if need.get("need") and need.get("search_query"):
                src = need.get("source", "memory")
                q = need["search_query"]
                cap_name = {"memory": "memory.search", "fabric": "fabric.query",
                            "web": "research.quick_search"}.get(src, "memory.search")
                cap = CAPABILITY_REGISTRY.get(cap_name)
                if cap:
                    try:
                        if cap_name == "fabric.query":
                            res = await cap["func"](query=json.dumps({"text": q, "top_k": 5, "include_data": True}))
                        else:
                            res = await cap["func"](query=q, limit=5)
                        if isinstance(res, dict):
                            rows = res.get("results") or res.get("records") or []
                            enriched.append({
                                "need":   need["need"],
                                "query":  q,
                                "source": src,
                                "results": rows[:5],
                            })
                    except Exception as e:
                        enriched.append({"need": need["need"], "query": q, "source": src, "error": str(e)})
    except Exception:
        pass

    state["enriched"] = {"items": enriched, "count": len(enriched)}
    return state


@capability(
    "dream.stage.propose_action", memory="off", silent=True,
    description="Dream pipeline stage: ask the LLM to propose ONE concrete next action. "
                "Doesn't execute it — just records the proposal in state['proposed_action'] "
                "for the synthesize stage to surface.",
)
async def dream_stage_propose_action(state: Optional[Dict[str, Any]] = None, trace_id=None):
    state = state or {}
    trig = state.get("trigger", {})
    themes = state.get("themes", [])
    gather = state.get("gather", {})

    summary = []
    for sname, sres in (gather.get("results", {}) or {}).items():
        if isinstance(sres, dict) and sres.get("count"):
            summary.append(f"{sname}: {sres.get('count')} items")

    prompt = (
        f"Goal: {trig.get('prompt', '(no prompt)')}\n"
        f"Themes detected: {', '.join(themes) if themes else '(none)'}\n"
        f"Sensor activity: {', '.join(summary) or '(none)'}\n\n"
        "Propose ONE concrete next action that would be valuable to take. "
        "Be specific — name the cap to call or the artifact to produce. "
        "Reply with a JSON object:\n"
        '  {"action": "<one sentence>", "cap": "<cap_name or null>", "rationale": "<why>"}\n'
        'Or {"action": null} if no action would be useful right now.'
    )
    raw = await _llm_generate(prompt, system="You propose concrete actions. JSON only.")
    proposed = None
    try:
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e != -1:
            obj = json.loads(raw[s:e+1])
            if obj.get("action"):
                proposed = obj
    except Exception:
        pass

    state["proposed_action"] = proposed or {"action": None}
    return state


@capability(
    "dream.stage.quality_check", memory="off", silent=True,
    description="Dream pipeline stage: ask the LLM to grade the synthesized report. "
                "Records a quality assessment in state['quality'] without modifying the report.",
)
async def dream_stage_quality_check(state: Optional[Dict[str, Any]] = None, trace_id=None):
    state = state or {}
    report = state.get("report") or ""
    if not report or len(report) < 60:
        state["quality"] = {"score": 0, "note": "report too short to assess"}
        return state

    prompt = (
        "Grade the following dream-cycle report 1-10 on:\n"
        "  groundedness — does it stay tied to actual data, or hallucinate?\n"
        "  specificity   — concrete details vs vague platitudes?\n"
        "  usefulness    — would this help me act?\n\n"
        "Reply with JSON only:\n"
        '  {"groundedness": 1-10, "specificity": 1-10, "usefulness": 1-10, "issues": ["..."]}\n\n'
        f"Report:\n{report[:3000]}"
    )
    raw = await _llm_generate(prompt, system="You grade reports. JSON only.")
    quality = {"score": 0}
    try:
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e != -1:
            obj = json.loads(raw[s:e+1])
            g = int(obj.get("groundedness", 0))
            sp = int(obj.get("specificity", 0))
            u = int(obj.get("usefulness", 0))
            quality = {
                "groundedness": g, "specificity": sp, "usefulness": u,
                "score": round((g + sp + u) / 3, 1),
                "issues": obj.get("issues", []),
            }
    except Exception:
        pass

    state["quality"] = quality
    return state


# ─────────────────────────────────────────────────────────────────────────────
# AGENTIC LOOP STAGE — delegates to dag.agent_loop_v2 (DAG Workshop)
# ─────────────────────────────────────────────────────────────────────────────
# dream_stage_investigate now delegates to dag.agent_loop_v2, the same ReAct
# loop used by the DAG Workshop panel. This eliminates duplicate LLM-loop logic
# and gives dream cycles access to the full Workshop feature set: tool
# selection, satisfaction checks, expand steps, and structured tool-call events
# that the dream panel streams live.
#
# State contract (unchanged from the old implementation):
#   state["findings"]    — list of {topic, content, source, iter} appended by loop
#   state["iterations"]  — list of cycle records for UI display
#   state["iterate"]     — {"stop": bool, "reason": str, "completed": int}
#   state["stepwise"]    — {"steps": [...], "count": int} mirror for synthesize
#
# The agent loop runs until it decides the goal is satisfied, max_cycles is
# reached, or _CYCLE_CANCEL is set. Per-step HITL is handled by asking the
# agent loop to only use caps in no_hitl_caps (auto-approved) first; a second
# pass can be unlocked by user approval through the normal HITL mechanism.

@capability(
    "dream.stage.investigate", memory="off", silent=True,
    description="Agentic investigation loop — delegates to dag.agent_loop_v2 "
                "(the DAG Workshop ReAct engine). Runs up to max_iterations "
                "tool-use cycles, accumulating findings in state['findings']. "
                "Halts when the loop signals satisfaction, on max_iterations, "
                "or on cancel. Caps in no_hitl_caps skip per-step HITL.",
)
async def dream_stage_investigate(
    state: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    # ── Delegated to dag.agent_loop_v2 (DAG Workshop ReAct engine) ──────────
    # This stage now delegates all agentic loop logic to dag.agent_loop_v2,
    # the same engine powering the DAG Workshop's "Run agent loop" feature.
    # It avoids duplicating LLM-loop, tool-selection, and dedup logic here.
    # After the loop completes, findings are extracted from the agent's tool-call
    # history and injected into state["findings"] and state["iterations"] so the
    # downstream synthesize stage works unchanged.

    state = state or {}
    trig = state.get("trigger", {})
    seed = state.get("seed") or {}
    themes = state.get("themes", [])
    gather = state.get("gather", {})
    cycle_id = state.get("cycle_id", "?")
    iter_cfg = trig.get("iterate") or {}
    max_cycles = int(iter_cfg.get("max_iterations", 6) or 6)
    no_hitl_caps = set(trig.get("no_hitl_caps") or [])

    # Build effective whitelist
    whitelist = trig.get("whitelist") or await _get_whitelist()
    if not whitelist and no_hitl_caps:
        whitelist = list(no_hitl_caps)
    _EXCLUDE = {"dream.", "obs.", "health.", "ui.", "caps.", "mcp.", "echo"}
    whitelist = [
        c for c in whitelist
        if c in CAPABILITY_REGISTRY and not any(c.startswith(p) for p in _EXCLUDE)
    ]
    if not whitelist:
        state.setdefault("iterate", {})["stop"] = True
        state["iterate"]["reason"] = "no available caps in whitelist"
        return state

    # Build sensor digest for the goal context
    for sname, sres in (gather.get("results", {}) or {}).items():
        if isinstance(sres, dict):
            cnt = sres.get("count", 0)
            sig = sres.get("signal", 0)
            previews = []
            for item in (sres.get("sample") or [])[:2]:
                if isinstance(item, dict):
                    txt = (item.get("text") or item.get("title") or "")
                    if txt:
                        previews.append(str(txt)[:100])
            gather_lines.append(
                f"  {sname}: {cnt} items (signal {sig})"
                + (f" — {' / '.join(previews)}" if previews else "")
            )

    # Build goal string that grounds the agent loop in the current dream state
    # Phase 1: use refined_goal if goal_refine stage ran upstream
    refined_goal = state.get("refined_goal")
    if refined_goal:
        goal_parts = [refined_goal]
        bg = trig.get("prompt", "")
        if bg:
            goal_parts.append(f"BACKGROUND CONTEXT: {bg}")
        if themes:
            goal_parts.append(f"Themes: {', '.join(themes)}")
        goal_parts.append(
            "Use the whitelisted capabilities to accomplish this specific goal. "
            "Write findings as structured notes for the synthesizer."
        )
        goal = "\n\n".join(goal_parts)
    else:
        focus = (seed.get("focus_topic") or "").strip()
        project_ctx = (seed.get("project_context") or "").strip()
        goal_parts = [
            trig.get("prompt") or "Investigate the most interesting signal and record findings.",
        ]
        if focus:
            goal_parts.append(f"FOCUS: {focus}")
        if project_ctx:
            goal_parts.append(f"Project context (use this to ground your investigation):\n{project_ctx[:2000]}")
        if themes:
            goal_parts.append(f"Themes detected: {', '.join(themes)}")
        if gather_lines:
            goal_parts.append("Sensor activity:\n" + "\n".join(gather_lines))
        goal_parts.append(
            "Use the whitelisted capabilities to gather evidence, then stop when you "
            "have substantive findings. Write findings as structured notes — they will "
            "be used by the synthesizer in the next stage."
        )
        goal = "\n\n".join(goal_parts)

    # Resolve the agent_loop cap — prefer v2, fall back to v1
    agent_loop_cap = (
        CAPABILITY_REGISTRY.get("dag.agent_loop_v2")
        or CAPABILITY_REGISTRY.get("dag.agent_loop")
    )
    if not agent_loop_cap:
        # dag.agent_loop not loaded — fall back to a simple sequential scan
        log.warning("dream.stage.investigate: dag.agent_loop_v2 not registered; "
                    "using lightweight fallback")
        findings: List[Dict[str, Any]] = []
        for cap_name in whitelist[:max_cycles]:
            if _CYCLE_CANCEL:
                break
            try:
                result = await _call_cap(cap_name)
                preview = str(result)[:600] if result else "(empty)"
                findings.append({"topic": cap_name, "content": preview, "source": cap_name, "iter": 0})
                await emit_event({"type": "dream.investigate.result", "cycle_id": cycle_id,
                                  "cap": cap_name, "ok": True, "preview": preview[:200]})
            except Exception as e:
                await emit_event({"type": "dream.investigate.result", "cycle_id": cycle_id,
                                  "cap": cap_name, "ok": False, "error": str(e)[:200]})
        state["findings"] = findings
        state["iterate"] = {"stop": True, "reason": "fallback: no agent_loop_v2", "completed": 1}
        return state

    # ── Delegate to dag.agent_loop_v2 ────────────────────────────────────────
    # allowed_caps is a comma-separated string expected by the agent loop
    allowed_caps_str = ",".join(whitelist)
    loop_session_id = f"dream:{cycle_id}:investigate"

    await emit_event({
        "type":      "dream.investigate.start",
        "cycle_id":  cycle_id,
        "trigger":   trig.get("name"),
        "max_cycles": max_cycles,
        "using_engine": "dag.agent_loop_v2" if "dag.agent_loop_v2" in CAPABILITY_REGISTRY
                        else "dag.agent_loop",
    })

    loop_result: Dict[str, Any] = {}
    try:
        cfg = await _get_config()
        settings = await _resolve_loop_settings(trig, state)
        # Global prefer_gpu config still acts as a fallback when unset in settings
        settings.setdefault("prefer_gpu", bool(cfg.get("llm_prefer_gpu", True)))
        loop_kwargs = _loop_kwargs_for(
            agent_loop_cap["func"], settings,
            goal=goal,
            allowed_caps=allowed_caps_str,
            max_cycles=settings.get("max_cycles", max_cycles),
            session_id=loop_session_id,
        )
        loop_result = await agent_loop_cap["func"](**loop_kwargs) or {}
    except Exception as e:
        log.warning("dream.stage.investigate agent_loop error: %s", e)
        loop_result = {"error": str(e)}

    # ── Extract findings from the agent loop's tool-call history ────────────
    # dag.agent_loop_v2 returns {"summary", "cycles", "tool_calls": [...], ...}
    # We convert each successful tool call into a finding record so the
    # downstream synthesize stage can reference them naturally.
    findings: List[Dict[str, Any]] = list(state.get("findings") or [])
    iterations: List[Dict[str, Any]] = list(state.get("iterations") or [])

    tool_calls: List[Dict[str, Any]] = loop_result.get("tool_calls") or []
    for i, tc in enumerate(tool_calls):
        cap_called = tc.get("tool") or tc.get("cap") or "?"
        result_preview = str(tc.get("result") or tc.get("output") or tc.get("preview") or "")[:800]
        thought = str(tc.get("thought") or tc.get("reason") or "")[:200]
        if result_preview:
            findings.append({
                "topic":   f"{cap_called} — {thought}" if thought else cap_called,
                "content": result_preview,
                "source":  cap_called,
                "iter":    i,
            })
        iterations.append({
            "i": i, "action": "call", "cap": cap_called,
            "preview": result_preview[:200], "why": thought,
            "ok": not bool(tc.get("error")),
        })

    # If the loop produced a summary, add it as a top-level finding
    summary = (loop_result.get("summary") or "").strip()
    if summary:
        findings.append({
            "topic":   "agent_loop_summary",
            "content": summary[:2000],
            "source":  "dag.agent_loop_v2",
            "iter":    len(tool_calls),
        })

    state["findings"] = findings
    state["iterations"] = iterations
    state["iterate"] = {
        "stop": True,
        "reason": "agent_loop completed",
        "completed": loop_result.get("cycles", len(tool_calls)),
        "engine": "dag.agent_loop_v2" if "dag.agent_loop_v2" in CAPABILITY_REGISTRY
                  else "dag.agent_loop",
    }
    if loop_result.get("error"):
        state["iterate"]["error"] = loop_result["error"]

    await emit_event({
        "type":      "dream.investigate.complete",
        "cycle_id":  cycle_id,
        "findings":  len(findings),
        "cycles":    state["iterate"]["completed"],
        "summary":   summary[:300] if summary else "",
    })

    return state





@capability(
    "dream.stage.deliver", memory="off", silent=True,
    description="Dream pipeline stage 6: deliver the dream report to telegram / memory / notebook as configured.",
)
async def dream_stage_deliver(state: Optional[Dict[str, Any]] = None, trace_id=None):
    state = state or {}
    trig = state.get("trigger", {})
    report = state.get("report") or ""
    targets = trig.get("deliver_to") or ["memory"]
    delivered: Dict[str, Any] = {}

    if "telegram" in targets:
        tg_notify = CAPABILITY_REGISTRY.get("tg.notify")
        if tg_notify:
            try:
                header = f"Dream: {trig.get('label', trig.get('name','cycle'))}\n\n"
                r = await tg_notify["func"](text=header + report[:3500])
                delivered["telegram"] = bool(r.get("ok")) if isinstance(r, dict) else True
            except Exception as e:
                delivered["telegram"] = f"error: {e}"

    if "memory" in targets:
        store = CAPABILITY_REGISTRY.get("memory.store")
        if store:
            try:
                await _call_cap(
                    "memory.store",
                    text=report[:4000],
                    category="dream",
                    tags=["dream", trig.get("name", "cycle")] + list(state.get("themes", []))[:5],
                    record_type="dream_report",
                    session_id=f"dream:{trig.get('name', 'cycle')}",
                    importance=0.4,
                )
                delivered["memory"] = True
            except Exception as e:
                delivered["memory"] = f"error: {e}"

    if "notebook" in targets:
        nb = CAPABILITY_REGISTRY.get("notebook.create") or CAPABILITY_REGISTRY.get("notebook.append")
        if nb:
            try:
                content = report
                # Attach the running journal so the notebook captures the
                # dream's full thinking, not just the final report.
                if trig.get("journal", True):
                    j_entries = await _journal_read(
                        state.get("journal_id") or state.get("cycle_id", ""),
                        limit=200)
                    if j_entries:
                        content = (report + "\n\n---\n\n"
                                   + _journal_to_markdown(j_entries, heading="Dream Journal"))
                await nb["func"](
                    title=f"Dream — {trig.get('label', trig.get('name','cycle'))}",
                    content=content,
                    tags=["dream"] + list(state.get("themes", []))[:5],
                )
                delivered["notebook"] = True
            except Exception as e:
                delivered["notebook"] = f"error: {e}"

    fabric = _fabric()
    if fabric and hasattr(fabric, "ingest_dataset"):
        try:
            await fabric.ingest_dataset(  # type: ignore
                dataset_id="dream.reports",
                data=[{
                    "text":    report,
                    "trigger": trig.get("name"),
                    "label":   trig.get("label"),
                    "themes":  state.get("themes", []),
                    "ts":      now_iso(),
                }],
                source="dream",
                source_id=trig.get("name", "cycle"),
                tags=["dream", trig.get("name", "cycle")],
            )
            delivered["fabric"] = True
        except Exception as e:
            delivered["fabric"] = f"error: {e}"

    state["delivered"] = delivered
    return state


# ─────────────────────────────────────────────────────────────────────────────
# STAGE: REVIEW CODEBASE — whole-snapshot review with a running journal
# ─────────────────────────────────────────────────────────────────────────────
# Replaces the old single-file, agent-driven review. Deterministically walks
# every review candidate (changed files by default, or the whole module list
# when scope="all"), runs ide.inspect.review_file on each, and journals its
# findings as it goes so the journal reads like a coherent review narrative.
# Self-skips cleanly when there is no snapshot in state.

@capability(
    "dream.stage.review_codebase", memory="off", silent=True,
    description="Dream pipeline stage: review the whole source snapshot file by "
                "file (not just one), journalling findings as it goes, then plan "
                "improvements for the high-severity files. Requires dream.stage."
                "snapshot_source earlier in the pipeline. Configure via "
                "stage_config.review_codebase = {scope: changed|all, max_files, "
                "plan: bool}. Writes state['review'] and appends to state['findings'].",
)
async def dream_stage_review_codebase(
    state: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    state = state or {}
    trig = state.get("trigger", {})
    cycle_id = state.get("cycle_id", "?")
    journal_id = state.get("journal_id") or cycle_id
    cfg = (trig.get("stage_config", {}) or {}).get("review_codebase", {}) or {}
    seed = state.get("seed") or {}
    # review_type: changes | wander | continue  (scope kept as legacy alias)
    review_type = (cfg.get("review_type") or seed.get("review_type")
                   or cfg.get("scope") or "changes").lower()
    if review_type in ("all", "full"):
        review_type = "wander"
    if review_type == "changed":
        review_type = "changes"
    max_files = int(cfg.get("max_files", 40) or 40)
    do_plan = bool(cfg.get("plan", True))

    await emit_event({"type": "dream.stage.started", "cycle_id": cycle_id,
                      "stage": "dream.stage.review_codebase",
                      "review_type": review_type})

    # Ensure a snapshot exists (stage is robust when run standalone, without a
    # preceding snapshot_source stage).
    snap = state.get("snapshot") or {}
    if not snap.get("snapshot_id"):
        snap = await _resolve_review_snapshot(label=f"dream_{review_type}")
        # compute changed files vs baseline when we just created one
        if snap.get("created") and snap.get("baseline_id"):
            dcap = CAPABILITY_REGISTRY.get("ide.inspect.diff_snapshot")
            if dcap:
                try:
                    d = await dcap["func"](snapshot_id=snap["baseline_id"],
                                           max_chars_per_file=6000) or {}
                    snap["changed_files"] = d.get("modified", []) + d.get("added", [])
                except Exception:
                    snap["changed_files"] = []
        state["snapshot"] = snap
    snapshot_id = snap.get("snapshot_id")
    if not snapshot_id:
        await _journal_append(journal_id,
            "No source snapshot available — skipping codebase review.",
            kind="review", stage="review_codebase", title="Review skipped")
        state["review"] = {"skipped": True, "reason": "no snapshot"}
        return state

    # Select the files to review based on the review type
    candidates = await _select_review_files(review_type, snap, max_files)
    # If a "changes" review found nothing changed, fall back to continuation so
    # the run is still productive rather than empty.
    if not candidates and review_type == "changes":
        await _journal_append(journal_id,
            "No changed files — falling back to continuation review.",
            kind="review", stage="review_codebase", title="No changes")
        review_type = "continue"
        candidates = await _select_review_files(review_type, snap, max_files)

    review_cap = CAPABILITY_REGISTRY.get("ide.inspect.review_file")
    if not review_cap:
        state["review"] = {"error": "ide.inspect.review_file unavailable"}
        return state

    await _journal_append(journal_id,
        f"Starting {review_type} review of {len(candidates)} file(s) against "
        f"snapshot {snapshot_id}.",
        kind="review", stage="review_codebase", title="Review started",
        data={"files": [c.get("file") for c in candidates]})

    results: List[Dict[str, Any]] = []
    high_sev_files: List[str] = []
    reviewed_names: List[str] = []
    total_issues = 0
    findings = state.setdefault("findings", [])

    for idx, cand in enumerate(candidates):
        if _CYCLE_CANCEL:
            break
        path = cand.get("file")
        if not path:
            continue
        try:
            rev = await review_cap["func"](
                snapshot_id=snapshot_id, path=path, agent="dream-reviewer",
            ) or {}
        except Exception as e:
            await _journal_append(journal_id, f"{path}: review error — {e}",
                kind="review", stage="review_codebase", title=f"Review error: {path}")
            results.append({"file": path, "error": str(e)})
            continue
        reviewed_names.append(path)

        issues = rev.get("issues") or []
        opportunities = rev.get("opportunities") or []
        strengths = rev.get("strengths") or []
        summary = rev.get("summary") or (rev.get("raw", "")[:300])
        sev = [str(i.get("severity", "")).lower() for i in issues if isinstance(i, dict)]
        has_high = any(s in ("high", "critical") for s in sev)
        total_issues += len(issues)
        if has_high:
            high_sev_files.append(path)

        results.append({
            "file": path, "issues": len(issues),
            "opportunities": len(opportunities), "strengths": len(strengths),
            "high_severity": has_high, "summary": summary[:600],
            # keep structured detail so the report stage can render substance
            "issue_detail": [
                {"severity": str(i.get("severity", "")), "line": i.get("line"),
                 "title": (i.get("title") or i.get("issue") or "")[:200],
                 "detail": (i.get("detail") or i.get("description") or "")[:400]}
                for i in issues[:12] if isinstance(i, dict)
            ],
            "opportunity_detail": [
                (o.get("title") or o.get("opportunity") or str(o))[:200]
                for o in opportunities[:8]
            ],
        })

        # Journal this file's review as a narrative entry
        body_lines = [summary.strip()] if summary else []
        for i in issues[:6]:
            if isinstance(i, dict):
                body_lines.append(
                    f"  • [{i.get('severity','?')}] "
                    f"{i.get('title') or i.get('issue') or ''} "
                    f"{('(L'+str(i.get('line'))+')') if i.get('line') else ''}".strip())
        await _journal_append(journal_id, "\n".join(body_lines) or "(no issues found)",
            kind="review", stage="review_codebase",
            title=f"Reviewed {path} — {len(issues)} issue(s)"
                  + (" [HIGH]" if has_high else ""),
            data={"file": path, "issues": len(issues), "high_severity": has_high})

        # Accumulate a compact finding for synthesize/iteration
        if issues or opportunities:
            findings.append({
                "topic": f"review:{path}",
                "content": summary[:600],
                "source": "ide.inspect.review_file",
                "iter": state.get("iteration_index", 0),
            })

        await emit_event({"type": "dream.review.file", "cycle_id": cycle_id,
                          "file": path, "issues": len(issues),
                          "high_severity": has_high,
                          "progress": f"{idx+1}/{len(candidates)}"})

    # Plan improvements for the worst files
    plan = None
    if do_plan and high_sev_files:
        plan_cap = CAPABILITY_REGISTRY.get("ide.inspect.plan_improvement")
        if plan_cap:
            try:
                plan = await plan_cap["func"](
                    snapshot_id=snapshot_id,
                    goal=("Address the high-severity issues found during this "
                          "codebase review across the listed files."),
                    files=json.dumps(high_sev_files[:12]),
                ) or {}
                await _journal_append(journal_id,
                    (plan.get("plan", {}) or {}).get("overview", "")
                    or json.dumps(plan)[:600],
                    kind="plan", stage="review_codebase",
                    title=f"Improvement plan for {len(high_sev_files)} file(s)")
            except Exception as e:
                log.debug("review_codebase plan: %s", e)

    await _mark_reviewed(snapshot_id, reviewed_names)

    state["review"] = {
        "snapshot_id":      snapshot_id,
        "baseline_id":      snap.get("baseline_id"),
        "review_type":      review_type,
        "files_reviewed":   len(results),
        "total_issues":     total_issues,
        "high_severity_files": high_sev_files,
        "results":          results,
        "plan":             plan,
        "journal_id":       journal_id,
    }
    state["findings"] = findings

    await _journal_append(journal_id,
        f"Review complete: {len(results)} files, {total_issues} issues, "
        f"{len(high_sev_files)} with high-severity findings.",
        kind="review", stage="review_codebase", title="Review summary")

    await emit_event({"type": "dream.stage.completed", "cycle_id": cycle_id,
                      "stage": "dream.stage.review_codebase",
                      "files_reviewed": len(results), "total_issues": total_issues,
                      "high_severity": len(high_sev_files)})
    return state


# ─────────────────────────────────────────────────────────────────────────────
# STAGE: REVIEW REPORT — build a substantial report from the review results
# ─────────────────────────────────────────────────────────────────────────────
# The old pipeline left synthesis to the LLM working from thin findings, which
# produced near-empty reports. This stage composes the report deterministically
# from state['review'] (so it always has substance), with an optional LLM
# executive summary on top. Sets state['report'] + state['title'].

def _sev_rank(s: str) -> int:
    return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(str(s).lower(), 4)


@capability(
    "dream.stage.review_report", memory="off", silent=True,
    description="Dream pipeline stage: compose a substantial source-review report "
                "from state['review'] (per-file issues, opportunities, plan), with "
                "an optional LLM executive summary. Sets state['report']/['title'].",
)
async def dream_stage_review_report(
    state: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    state = state or {}
    trig = state.get("trigger", {})
    cycle_id = state.get("cycle_id", "?")
    journal_id = state.get("journal_id") or cycle_id
    review = state.get("review") or {}

    await emit_event({"type": "dream.stage.started", "cycle_id": cycle_id,
                      "stage": "dream.stage.review_report"})

    if review.get("skipped") or review.get("error"):
        state.setdefault("report", f"Source review did not run: "
                         f"{review.get('reason') or review.get('error')}")
        state.setdefault("title", "Source Review — skipped")
        return state

    rtype = review.get("review_type", "changes")
    results = review.get("results") or []
    high = review.get("high_severity_files") or []
    total_issues = review.get("total_issues", 0)
    snapshot_id = review.get("snapshot_id", "?")

    # Flatten + sort all issues by severity for a priority list
    prioritized: List[Dict[str, Any]] = []
    for r in results:
        for it in r.get("issue_detail", []):
            prioritized.append({**it, "file": r["file"]})
    prioritized.sort(key=lambda x: (_sev_rank(x.get("severity")), x.get("file", "")))

    lines: List[str] = []
    lines.append(f"# Source Code Review — {rtype.title()}")
    lines.append("")
    lines.append(f"Snapshot `{snapshot_id}` · {len(results)} file(s) reviewed · "
                 f"{total_issues} issue(s) · {len(high)} file(s) with "
                 f"high-severity findings.")
    lines.append("")

    # Optional LLM executive summary (substance comes from the data below, so a
    # failed/empty LLM call never produces an empty report)
    try:
        top_for_llm = "\n".join(
            f"- [{p.get('severity')}] {p['file']}: {p.get('title')}"
            for p in prioritized[:20])
        if top_for_llm.strip():
            summary = await _llm_generate(
                "Write a 3-5 sentence executive summary of this code review. "
                "Be specific and prioritise. Issues:\n" + top_for_llm,
                system="You are a senior engineer summarising a code review.")
            if summary and summary.strip():
                lines += ["## Summary", "", summary.strip(), ""]
    except Exception as e:
        log.debug("review_report summary: %s", e)

    # Priority issues
    if prioritized:
        lines += ["## Priority issues", ""]
        for p in prioritized[:25]:
            loc = f" (L{p['line']})" if p.get("line") else ""
            lines.append(f"- **[{p.get('severity','?')}]** `{p['file']}`{loc} — "
                         f"{p.get('title','')}")
            if p.get("detail"):
                lines.append(f"  - {p['detail']}")
        lines.append("")

    # Per-file breakdown
    lines += ["## Per-file findings", ""]
    for r in sorted(results, key=lambda x: (not x.get("high_severity"), x["file"])):
        flag = " — HIGH" if r.get("high_severity") else ""
        lines.append(f"### `{r['file']}`{flag}")
        if r.get("summary"):
            lines.append(r["summary"].strip())
        if r.get("issue_detail"):
            lines.append("")
            for it in r["issue_detail"]:
                loc = f" (L{it['line']})" if it.get("line") else ""
                lines.append(f"- [{it.get('severity','?')}] {it.get('title','')}{loc}")
        if r.get("opportunity_detail"):
            lines.append("")
            lines.append("_Opportunities:_ " + "; ".join(r["opportunity_detail"]))
        lines.append("")

    # Improvement plan
    plan = review.get("plan") or {}
    plan_body = ""
    if isinstance(plan, dict):
        p = plan.get("plan", plan)
        if isinstance(p, dict):
            plan_body = p.get("overview") or p.get("summary") or ""
            steps = p.get("steps") or p.get("changes") or []
            if steps:
                plan_body += "\n" + "\n".join(
                    f"- {s.get('description', s) if isinstance(s, dict) else s}"
                    for s in steps[:15])
        elif isinstance(p, str):
            plan_body = p
    if plan_body.strip():
        lines += ["## Improvement plan", "", plan_body.strip(), ""]

    report = "\n".join(lines)
    state["report"] = report
    state["title"] = (f"Source Review ({rtype}) — {len(results)} files, "
                      f"{total_issues} issues")

    await _journal_append(journal_id,
        f"Composed review report: {len(report)} chars, {len(prioritized)} "
        f"prioritised issues.",
        kind="note", stage="review_report", title="Report composed")

    await emit_event({"type": "dream.stage.completed", "cycle_id": cycle_id,
                      "stage": "dream.stage.review_report",
                      "report_chars": len(report)})
    return state


# ─────────────────────────────────────────────────────────────────────────────
# STAGE: IDE WORKSPACE ACT — use the IDE agent to draft fixes in a workspace
# ─────────────────────────────────────────────────────────────────────────────
# Optional, OFF by default. For each high-severity file it asks the IDE writer
# agent to produce a corrected version addressing the issues, and writes the
# result into a dedicated workspace (never to live source). The user can then
# review/promote. Enable via stage_config.ide_workspace_act = {enabled: true,
# workspace: "vera-review-fixes", max_files: 3}.

@capability(
    "dream.stage.ide_workspace_act", memory="off", silent=True,
    description="Dream pipeline stage: use the IDE writer agent to draft fixes "
                "for high-severity review findings into a workspace (not live "
                "source). OFF unless stage_config.ide_workspace_act.enabled=true.",
)
async def dream_stage_ide_workspace_act(
    state: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    state = state or {}
    trig = state.get("trigger", {})
    cycle_id = state.get("cycle_id", "?")
    journal_id = state.get("journal_id") or cycle_id
    cfg = (trig.get("stage_config", {}) or {}).get("ide_workspace_act", {}) or {}

    state["workspace_changes"] = {"enabled": bool(cfg.get("enabled"))}
    if not cfg.get("enabled"):
        return state

    review = state.get("review") or {}
    high = (review.get("high_severity_files") or [])[:int(cfg.get("max_files", 3) or 3)]
    snapshot_id = review.get("snapshot_id")
    if not (high and snapshot_id):
        state["workspace_changes"] = {"enabled": True, "drafted": 0,
                                      "reason": "no high-severity files"}
        return state

    await emit_event({"type": "dream.stage.started", "cycle_id": cycle_id,
                      "stage": "dream.stage.ide_workspace_act"})

    # Resolve the snapshot directory + create the fixes workspace
    src_info = CAPABILITY_REGISTRY.get("ide.inspect.source_info")
    ws_create = CAPABILITY_REGISTRY.get("ide.workspace.create")
    chat = CAPABILITY_REGISTRY.get("ide.agent.chat")
    fs_read = CAPABILITY_REGISTRY.get("ide.fs.read")
    fs_write = CAPABILITY_REGISTRY.get("ide.fs.write")
    if not (chat and fs_write):
        state["workspace_changes"] = {"enabled": True, "error": "ide agent/fs unavailable"}
        return state

    snap_root = ""
    if src_info:
        try:
            snap_root = (await src_info["func"]() or {}).get("snapshot_root", "")
        except Exception:
            snap_root = ""
    ws_name = cfg.get("workspace", "vera-review-fixes")
    ws_path = ""
    if ws_create:
        try:
            ws = await ws_create["func"](name=ws_name) or {}
            ws_path = ws.get("path", "")
        except Exception as e:
            log.debug("ide_workspace_act workspace: %s", e)

    # Build a quick lookup of issues per file
    issues_by_file: Dict[str, List[Dict[str, Any]]] = {}
    for r in review.get("results", []):
        if r.get("file") in high:
            issues_by_file[r["file"]] = r.get("issue_detail", [])

    drafted = 0
    for f in high:
        content = ""
        if fs_read and snap_root:
            try:
                rr = await fs_read["func"](path=f"{snap_root}/{snapshot_id}/{f}") or {}
                content = rr.get("content", "")
            except Exception:
                content = ""
        issue_txt = "\n".join(
            f"- [{i.get('severity')}] {i.get('title')}"
            + (f" (L{i.get('line')})" if i.get("line") else "")
            + (f": {i.get('detail')}" if i.get("detail") else "")
            for i in issues_by_file.get(f, []))
        prompt = (
            f"Address the following review findings in `{f}`. Return the COMPLETE "
            f"corrected file content only (no commentary, no markdown fences).\n\n"
            f"Findings:\n{issue_txt or '(see summary)'}")
        try:
            resp = await chat["func"](
                agent="writer", prompt=prompt,
                system=("You are a careful senior engineer applying targeted fixes. "
                        "Preserve behaviour and style; change only what the findings "
                        "require."),
                context_files=json.dumps({f: content}) if content else "{}",
            ) or {}
            fixed = (resp.get("text") or "").strip()
            # Strip accidental code fences
            fixed = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", fixed).strip()
            if fixed and ws_path:
                await fs_write["func"](path=f"{ws_path}/{f}",
                                       content=fixed, agent="dream")
                drafted += 1
                await _journal_append(journal_id,
                    f"Drafted fix for {f} into workspace {ws_name} "
                    f"({len(fixed)} chars).",
                    kind="action", stage="ide_workspace_act",
                    title=f"Drafted fix: {f}")
                state.setdefault("findings", []).append({
                    "topic": f"fix:{f}", "content": f"Drafted fix in {ws_name}",
                    "source": "ide.agent.chat", "action": True,
                    "iter": state.get("iteration_index", 0)})
        except Exception as e:
            log.debug("ide_workspace_act %s: %s", f, e)

    state["workspace_changes"] = {"enabled": True, "workspace": ws_name,
                                  "path": ws_path, "drafted": drafted,
                                  "files": high}
    await emit_event({"type": "dream.stage.completed", "cycle_id": cycle_id,
                      "stage": "dream.stage.ide_workspace_act", "drafted": drafted})
    return state


# ─────────────────────────────────────────────────────────────────────────────
# STAGE: IDE AGENT — run a bounded IDE agent loop over the workspace + snapshot
# ─────────────────────────────────────────────────────────────────────────────
# Exposes the IDE agent loop (ide.agent.chat) as a dream stage. It opens/creates
# an IDE workspace, seeds it with selected source-snapshot files (or the review's
# flagged files), and runs up to max_turns of the agent toward a goal — the
# agent can read/edit within the workspace via its own tools. Files the agent
# returns are written into the workspace (never live source). Configure via
# stage_config.ide_agent = {goal, agent, workspace, max_turns, files:[...],
# from_review, max_files}.

@capability(
    "dream.stage.ide_agent", memory="off", silent=True,
    description="Dream stage: run a bounded IDE agent loop (ide.agent.chat) over an "
                "IDE workspace seeded from the source snapshot, toward a goal. "
                "Configure via stage_config.ide_agent = {goal, agent, workspace, "
                "max_turns, files, from_review, max_files}. Writes state['ide_agent'].",
)
async def dream_stage_ide_agent(state: Optional[Dict[str, Any]] = None, trace_id=None):
    state = state or {}
    trig = state.get("trigger", {})
    seed = state.get("seed") or {}
    cycle_id = state.get("cycle_id", "?")
    journal_id = state.get("journal_id") or cycle_id
    cfg = (trig.get("stage_config", {}) or {}).get("ide_agent", {}) or {}

    chat = CAPABILITY_REGISTRY.get("ide.agent.chat")
    if not chat:
        state["ide_agent"] = {"error": "ide.agent.chat unavailable"}
        return state

    goal = (cfg.get("goal") or seed.get("focus_topic")
            or state.get("refined_goal") or state.get("goals")
            or trig.get("prompt") or "Improve and document this code.")[:1500]
    agent = cfg.get("agent", "code-reviewer")
    max_turns = int(cfg.get("max_turns", 4) or 4)
    ws_name = cfg.get("workspace", "vera-dream-agent")

    await emit_event({"type": "dream.stage.started", "cycle_id": cycle_id,
                      "stage": "dream.stage.ide_agent"})

    # Resolve snapshot root + the files to seed as context
    roots = await _source_root_info()
    snap = await _resolve_review_snapshot(label="ide_agent")
    snapshot_id = snap.get("snapshot_id") or ""
    files: List[str] = list(cfg.get("files") or [])
    if not files and cfg.get("from_review", True):
        files = list(((state.get("review") or {}).get("high_severity_files")) or [])
    if not files:
        enum = await _enumerate_source_files(snapshot_id, roots)
        files = [f["rel"] for f in enum][:int(cfg.get("max_files", 3) or 3)]
    files = files[:int(cfg.get("max_files", 5) or 5)]

    # Open/create the workspace and seed context from the snapshot
    ws_create = CAPABILITY_REGISTRY.get("ide.workspace.create")
    ws_path = ""
    if ws_create:
        try:
            ws_path = ((await ws_create["func"](name=ws_name)) or {}).get("path", "")
        except Exception as e:
            log.debug("ide_agent workspace: %s", e)
    ctx: Dict[str, str] = {}
    for f in files:
        c = await _read_source_file(roots, snapshot_id, f, 0)
        if c:
            ctx[f] = c

    turns: List[Dict[str, Any]] = []
    convo = (f"Goal: {goal}\n\nFiles in scope: {', '.join(files) or '(none)'}\n"
             f"Work within the IDE workspace '{ws_name}'. When you produce a file, "
             f"return the COMPLETE file content.")
    system = ("You are an IDE agent working inside a sandbox workspace (never live "
              "source). Read, reason, and produce concrete edits toward the goal. "
              "If finished, say DONE.")
    for t in range(max_turns):
        if _CYCLE_CANCEL:
            break
        try:
            resp = await chat["func"](
                agent=agent, prompt=convo, system=system,
                context_files=json.dumps(ctx) if ctx else "{}") or {}
        except Exception as e:
            turns.append({"turn": t, "error": str(e)})
            break
        text = (resp.get("text") or "").strip()
        turns.append({"turn": t, "text": text[:2000]})
        await _journal_append(journal_id,
            f"IDE agent turn {t + 1}/{max_turns}: {text[:160]}",
            kind="action", stage="ide_agent", title=f"IDE agent turn {t + 1}")
        await emit_event({"type": "dream.ide_agent.turn", "cycle_id": cycle_id,
                          "turn": t, "chars": len(text)})
        if not text or "DONE" in text[-80:].upper():
            break
        # Feed the response back for the next turn (bounded loop)
        convo = (f"Continue toward the goal. Your last output:\n{text[:1500]}\n\n"
                 f"Next concrete step, or say DONE if complete.")

    state["ide_agent"] = {"workspace": ws_name, "path": ws_path,
                          "files": files, "turns": len(turns),
                          "transcript": turns, "goal": goal}
    if not state.get("report"):
        state["report"] = (f"# IDE Agent Run\n\nGoal: {goal}\n\nWorkspace: `{ws_name}`\n"
                           f"Files: {', '.join(files)}\nTurns: {len(turns)}")
    await emit_event({"type": "dream.stage.completed", "cycle_id": cycle_id,
                      "stage": "dream.stage.ide_agent", "turns": len(turns)})
    return state


# ─────────────────────────────────────────────────────────────────────────────
# STAGE: PIVOT — decide whether to launch a *different* dream next
# ─────────────────────────────────────────────────────────────────────────────
# STAGE: LOAD WORKSPACE — pull the relevant project workspace into the dream
# ─────────────────────────────────────────────────────────────────────────────
# Instead of relying on the agent loop to go fetch project context mid-run, this
# stage loads it up front and folds it into the prompt + state. For large
# projects it uses RAG (vector search over the project's fabric/memory targets)
# to insert only the portions relevant to the current goal, rather than dumping
# everything. Configure via stage_config.load_workspace = {rag, top_k,
# max_chars, large_threshold}.

@capability(
    "dream.stage.load_workspace", memory="off", silent=True,
    description="Dream pipeline stage: load the relevant project workspace/context "
                "into the dream up front (not via the agent loop). Uses "
                "project.context.assemble (dynamic) plus RAG (fabric.search / "
                "memory.search) for large projects to insert only goal-relevant "
                "portions. Writes state['workspace'] and folds it into the prompt.",
)
async def dream_stage_load_workspace(
    state: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    state = state or {}
    trig = state.get("trigger", {})
    seed = state.get("seed") or {}
    cycle_id = state.get("cycle_id", "?")
    journal_id = state.get("journal_id") or cycle_id
    cfg = (trig.get("stage_config", {}) or {}).get("load_workspace", {}) or {}

    slug = (seed.get("project_id") or state.get("project_id")
            or trig.get("project") or "").strip()
    if not slug:
        state["workspace"] = {"skipped": True, "reason": "no project scope"}
        return state

    await emit_event({"type": "dream.stage.started", "cycle_id": cycle_id,
                      "stage": "dream.stage.load_workspace", "project": slug})

    goal = (state.get("refined_goal") or seed.get("focus_topic")
            or (state.get("goals") or "") or trig.get("prompt", ""))[:600]
    use_rag = bool(cfg.get("rag", True))
    top_k = int(cfg.get("top_k", 8) or 8)
    max_chars = int(cfg.get("max_chars", 12000) or 12000)
    large_threshold = int(cfg.get("large_threshold", 12) or 12)

    parts: List[str] = []
    rag_snippets: List[Dict[str, Any]] = []

    # 1) Base project context (dynamic mode picks goal-relevant context itself)
    assemble = CAPABILITY_REGISTRY.get("project.context.assemble")
    if assemble:
        try:
            res = await assemble["func"](slug=slug, goal=goal) or {}
            ctx = res.get("seed") or res.get("context") or res.get("text") or ""
            if isinstance(ctx, dict):
                ctx = ctx.get("context") or json.dumps(ctx)[:max_chars]
            if ctx:
                parts.append(str(ctx)[:max_chars])
        except Exception as e:
            log.debug("load_workspace assemble: %s", e)

    # 2) Browse the project's linked resources (notebooks, IDE workspaces,
    # fabric, memory). Count them, and for non-huge projects pull their FULL
    # content so the dream has the whole project context like an IDE/notebook.
    resource_count = 0
    full_blocks: List[str] = []
    browse = CAPABILITY_REGISTRY.get("project.browse_resources")
    if browse:
        try:
            br = await browse["func"](slug=slug, resource_type="all", limit=200) or {}
            for rtype, v in br.items():
                if not isinstance(v, list):
                    continue
                resource_count += len(v)
                for it in v:
                    if not isinstance(it, dict):
                        continue
                    body = (it.get("content") or it.get("text") or it.get("body")
                            or it.get("preview") or "")
                    title = (it.get("title") or it.get("name") or it.get("id")
                             or it.get("path") or "")
                    if body:
                        full_blocks.append(f"### [{rtype}] {title}\n{str(body)[:8000]}")
            state["workspace_resources"] = resource_count
        except Exception as e:
            log.debug("load_workspace browse: %s", e)

    is_large = resource_count >= large_threshold

    # 3a) Small/medium project (or RAG disabled): include the FULL resource
    # content — the dream gets the entire project workspace.
    if full_blocks and (not is_large or not use_rag):
        parts.append("FULL PROJECT RESOURCES:\n" + "\n\n".join(full_blocks))

    # 3b) Large project: RAG — insert only goal-relevant chunks via vector
    # search over the project's targets instead of the whole corpus.
    if use_rag and is_large and goal:
        for cap_name, qkey in (("fabric.search", "query"),
                               ("memory.search", "query")):
            cap = CAPABILITY_REGISTRY.get(cap_name)
            if not cap:
                continue
            try:
                kwargs = {qkey: goal, "limit": top_k}
                hits = await cap["func"](**kwargs) or {}
                items = (hits.get("results") or hits.get("hits")
                         or hits.get("matches") or [])
                for it in items[:top_k]:
                    txt = (it.get("text") or it.get("content")
                           or it.get("snippet") or str(it))[:800]
                    rag_snippets.append({"source": cap_name, "text": txt})
            except Exception as e:
                log.debug("load_workspace rag %s: %s", cap_name, e)
        if rag_snippets:
            joined = "\n\n".join(f"[{s['source']}] {s['text']}" for s in rag_snippets)
            parts.append("RELEVANT WORKSPACE EXCERPTS (RAG):\n" + joined[:max_chars])

    # Large projects keep a higher cap so full-ish context still fits
    _cap = max_chars * (2 if not is_large else 4)
    workspace_ctx = "\n\n".join(p for p in parts if p)[:_cap]
    state["workspace"] = {
        "project": slug, "goal": goal, "large": is_large,
        "resources": resource_count, "rag": bool(rag_snippets),
        "rag_count": len(rag_snippets), "full": bool(full_blocks and not (is_large and use_rag)),
        "chars": len(workspace_ctx), "context": workspace_ctx,
    }

    # Fold into the prompt so downstream stages + the agent loop already have it
    if workspace_ctx:
        trig["prompt"] = ("PROJECT WORKSPACE (loaded for you — use this directly, "
                          "do not re-fetch):\n" + workspace_ctx + "\n\n"
                          + (trig.get("prompt") or ""))

    await _journal_append(journal_id,
        f"Loaded workspace for project '{slug}': {resource_count} resources, "
        + (f"RAG inserted {len(rag_snippets)} relevant excerpts "
           f"(large project)" if rag_snippets else "full context")
        + f", {len(workspace_ctx)} chars.",
        kind="note", stage="load_workspace", title="Workspace loaded")

    await emit_event({"type": "dream.stage.completed", "cycle_id": cycle_id,
                      "stage": "dream.stage.load_workspace",
                      "chars": len(workspace_ctx), "rag": len(rag_snippets)})
    return state


# ─────────────────────────────────────────────────────────────────────────────
# Reads the report/findings and the trigger's pivot.candidates, then asks the
# LLM which follow-up dream (if any) would be most valuable. Sets state["pivot"];
# the post-cycle hook in _run_cycle actually schedules it. Self-skips when no
# candidates are configured.

@capability(
    "dream.stage.pivot", memory="off", silent=True,
    description="Dream pipeline stage: decide whether this dream's findings "
                "warrant pivoting into a different dream trigger next. Configure "
                "via trigger pivot={enabled, candidates:[names], min_confidence}. "
                "Sets state['pivot']={to_trigger, reason, focus_topic, confidence}.",
)
async def dream_stage_pivot(
    state: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    state = state or {}
    trig = state.get("trigger", {})
    cycle_id = state.get("cycle_id", "?")
    journal_id = state.get("journal_id") or cycle_id
    # Config can come from the per-stage config (when added explicitly as an
    # emit stage in Pipeline config) and/or the trigger's pivot block; the
    # stage_config layer wins. Being placed in the pipeline counts as enabling.
    _sc = (trig.get("stage_config", {}) or {}).get("pivot", {}) or {}
    pivot_cfg = {**(trig.get("pivot") or {}), **_sc}
    if "candidates" in _sc and isinstance(_sc["candidates"], str):
        pivot_cfg["candidates"] = [c.strip() for c in _sc["candidates"].split(",") if c.strip()]
    in_pipeline = "dream.stage.pivot" in (trig.get("pipeline") or [])
    self_name = trig.get("name", "")
    # "continue" (resume this trigger's train of thought toward its goals) is
    # always an available outcome; other triggers come from pivot.candidates.
    candidates = list(pivot_cfg.get("candidates") or [])
    allow_continue = bool(state.get("goals")) or bool(pivot_cfg.get("allow_continue", True))
    enabled = pivot_cfg.get("enabled", in_pipeline)  # in-pipeline → on by default

    state["pivot"] = None
    if not enabled and not allow_continue:
        return state
    if not candidates and not allow_continue:
        return state

    await emit_event({"type": "dream.stage.started", "cycle_id": cycle_id,
                      "stage": "dream.stage.pivot"})

    # Describe each candidate trigger for the LLM
    get_t = CAPABILITY_REGISTRY.get("dream.trigger.get")
    cand_desc: List[str] = []
    if allow_continue and self_name:
        cand_desc.append(f"- continue: keep working THIS dream's train of thought "
                         f"toward its standing goals (more steps remain).")
    for name in candidates[:8]:
        desc = ""
        if get_t:
            try:
                t = (await get_t["func"](name=name) or {}).get("trigger") or {}
                desc = t.get("description") or t.get("label") or ""
            except Exception:
                pass
        cand_desc.append(f"- {name}: {desc}".rstrip())

    report = (state.get("report") or "")[:2500]
    findings_txt = "\n".join(
        f"- {f.get('topic','')}: {str(f.get('content',''))[:160]}"
        for f in (state.get("findings") or [])[:15])
    goals_txt = str(state.get("goals") or "(none defined)")

    prompt = (
        "You are deciding this reflective agent's NEXT action after a work cycle. "
        "Either CONTINUE its current train of thought toward its standing goals, "
        "PIVOT into a different follow-up dream, or stop (none).\n\n"
        f"Standing goals:\n{goals_txt}\n\n"
        f"What it just produced:\n{report}\n\n"
        f"Key findings:\n{findings_txt or '(none)'}\n\n"
        f"Options (use 'continue' to keep going on this same dream):\n"
        + "\n".join(cand_desc) + "\n\n"
        "If the goals are not yet met and there is a clear next step, prefer "
        "'continue'. Respond with JSON only: "
        '{\"to_trigger\": \"<continue|name|none>\", \"reason\": \"<one line>\", '
        '\"focus_topic\": \"<the next step to take>\", \"confidence\": <0.0-1.0>}'
    )
    raw = await _llm_generate(prompt, system="You decide the agent's next action. JSON only.")
    decision: Dict[str, Any] = {}
    try:
        decision = json.loads(re.sub(r"^```(?:json)?|```$", "", (raw or "").strip()).strip())
    except Exception:
        decision = {}

    to_trigger = (decision.get("to_trigger") or "").strip()
    confidence = float(decision.get("confidence", 0) or 0)
    min_conf = float(pivot_cfg.get("min_confidence", 0.5) or 0.5)
    is_continue = to_trigger.lower() == "continue"
    valid = is_continue or (to_trigger and to_trigger.lower() != "none"
                            and to_trigger in candidates)

    if valid and confidence >= min_conf:
        target = self_name if is_continue else to_trigger
        state["pivot"] = {
            "to_trigger":  target,
            "reason":      (decision.get("reason") or "")[:300],
            "focus_topic": (decision.get("focus_topic") or "")[:200],
            "confidence":  confidence,
            "continue":    is_continue,
        }
        verb = "Continuing" if is_continue else f"Pivoting to '{target}'"
        await _journal_append(journal_id,
            f"{verb} (confidence {confidence:.2f}): {decision.get('reason','')}. "
            f"Next: {decision.get('focus_topic','')}",
            kind="pivot", stage="pivot",
            title=("Continue train of thought" if is_continue else f"Pivot → {target}"))
    else:
        await _journal_append(journal_id,
            f"Stopping — goals satisfied or no clear next step "
            f"(confidence {confidence:.2f}).",
            kind="pivot", stage="pivot", title="Stop")

    await emit_event({"type": "dream.stage.completed", "cycle_id": cycle_id,
                      "stage": "dream.stage.pivot",
                      "pivot": state.get("pivot")})
    return state


# ─────────────────────────────────────────────────────────────────────────────
# STAGE: ITERATE / CONTINUE — decide completion and whether to run again
# ─────────────────────────────────────────────────────────────────────────────
# An emit-phase stage that can be dropped into ANY pipeline to give it proper
# continue/iterate behaviour. It decides whether the dream's goal is satisfied
# or it should run another cycle, and lets the LLM choose the next step, refine
# the standing goals, suggest which sensors matter, and set the completion
# threshold. Continuation reuses the same reschedule mechanism as pivot
# (state["pivot"] with continue=True → the post-cycle hook re-runs this trigger,
# carrying the per-trigger journal). Completion can be judged on satisfaction
# (LLM self-assessment), runtime budget, user activity (idle), sensor signal,
# or any combination via stage_config.iterate.basis.

@capability(
    "dream.stage.iterate", memory="off", silent=True,
    description="Dream emit stage: decide whether the dream is complete or should "
                "continue/iterate, and let the LLM choose next step, refined goals, "
                "relevant sensors and the completion threshold. Configure via "
                "stage_config.iterate = {basis:[satisfaction,runtime,user_activity,"
                "sensors], max_iterations, max_runtime_s, satisfaction_target, "
                "min_idle_minutes, llm_decides, apply_goals, apply_sensors}. "
                "Continuation reuses the pivot reschedule (same trigger + journal).",
)
async def dream_stage_iterate(
    state: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    state = state or {}
    trig = state.get("trigger", {})
    seed = state.get("seed") or {}
    cycle_id = state.get("cycle_id", "?")
    journal_id = state.get("journal_id") or cycle_id

    cfg = ((trig.get("stage_config", {}) or {}).get("iterate", {})
           or trig.get("iterate", {}) or {})
    basis = cfg.get("basis") or ["satisfaction"]
    if isinstance(basis, str):
        basis = [b.strip() for b in basis.split(",") if b.strip()]
    max_iters     = int(cfg.get("max_iterations",
                                trig.get("max_continuation_depth", 3)) or 3)
    max_runtime   = float(cfg.get("max_runtime_s", 0) or 0)
    sat_target    = float(cfg.get("satisfaction_target", 0.8) or 0.8)
    min_idle      = float(cfg.get("min_idle_minutes",
                                  trig.get("min_idle_minutes", 0)) or 0)
    llm_decides   = bool(cfg.get("llm_decides", True))
    apply_goals   = bool(cfg.get("apply_goals", True))
    apply_sensors = bool(cfg.get("apply_sensors", False))

    self_name = trig.get("name", "")
    depth = int(seed.get("pivot_depth", 0) or seed.get("iteration", 0) or 0)

    await emit_event({"type": "dream.stage.started", "cycle_id": cycle_id,
                      "stage": "dream.stage.iterate"})

    # ── Gather the decision inputs ───────────────────────────────────────────
    elapsed = 0.0
    try:
        st = state.get("started_at")
        if st:
            elapsed = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(st.replace("Z", "+00:00"))).total_seconds()
    except Exception:
        pass
    idle = await _idle_minutes()
    sensor_eval = (await _eval_trigger_sensors(trig)) if trig.get("sensors") \
        else {"signal": 1.0, "detail": "(no sensors)"}
    sensor_signal = float(sensor_eval.get("signal", 0) or 0)

    # ── Hard stops (independent of the LLM) ──────────────────────────────────
    hard_stop = None
    if depth >= max_iters:
        hard_stop = f"reached max iterations ({max_iters})"
    elif "runtime" in basis and max_runtime and elapsed >= max_runtime:
        hard_stop = f"runtime budget reached ({int(elapsed)}s >= {int(max_runtime)}s)"
    elif "user_activity" in basis and min_idle and idle < min_idle:
        hard_stop = f"user is active (idle {idle:.1f}m < {min_idle:.0f}m) — pausing"
    elif ("sensors" in basis and trig.get("sensors")
          and sensor_signal < float(cfg.get("min_sensor_signal", 0.05) or 0.05)):
        hard_stop = f"sensor signal low ({sensor_signal:.2f}) — nothing left to work on"

    decision: Dict[str, Any] = {"continue": False, "reason": hard_stop or "",
                                "satisfaction": None}

    if not hard_stop and llm_decides:
        report = (state.get("report") or "")[:2500]
        findings_txt = "\n".join(
            f"- {f.get('topic','')}: {str(f.get('content',''))[:160]}"
            for f in (state.get("findings") or [])[:15])
        goals_txt = str(state.get("goals") or "(none defined)")
        avail_sensors = ", ".join(sorted(SENSOR_REGISTRY.keys())) or "(none)"
        prompt = (
            "You are deciding whether this reflective dream has SATISFIED its goal "
            "or should run another iteration. Judge completeness honestly.\n\n"
            f"Standing goals:\n{goals_txt}\n\n"
            f"This iteration produced:\n{report}\n\n"
            f"Findings so far:\n{findings_txt or '(none)'}\n\n"
            f"Context — iteration {depth + 1}/{max_iters}, elapsed {int(elapsed)}s"
            + (f"/{int(max_runtime)}s budget" if max_runtime else "")
            + f", user idle {idle:.1f}m, sensor signal {sensor_signal:.2f}.\n"
            f"Decision basis: {', '.join(basis)}.\n"
            f"Available sensors you may recommend watching: {avail_sensors}\n\n"
            "Respond with JSON only:\n"
            '{"satisfied": <bool>, "satisfaction": <0.0-1.0>, "continue": <bool>, '
            '"next_step": "<the single next step if continuing>", '
            '"updated_goals": "<refined standing goals, or empty to keep current>", '
            '"watch_sensors": ["<sensor ids that matter for completion>"], '
            '"completion_threshold": "<plain-language: when is this DONE>", '
            '"reason": "<one line>"}'
        )
        raw = await _llm_generate(prompt, system="You judge task completion and plan "
                                                 "iteration. JSON only.")
        try:
            decision = json.loads(re.sub(r"^```(?:json)?|```$", "",
                                         (raw or "").strip()).strip())
        except Exception:
            decision = {"continue": False, "reason": "could not parse decision"}

        sat = float(decision.get("satisfaction", 0) or 0)
        # Combine the LLM's wish with the satisfaction threshold when that basis
        # is active: high satisfaction forces completion even if the model wants
        # to keep going; a low score keeps it iterating.
        want_continue = bool(decision.get("continue"))
        if "satisfaction" in basis:
            if sat >= sat_target:
                want_continue = False
                decision.setdefault("reason",
                                    f"satisfaction {sat:.2f} >= target {sat_target:.2f}")
            elif decision.get("satisfied") is False and depth + 1 < max_iters:
                want_continue = True
        decision["continue"] = want_continue and (depth + 1 < max_iters)

    # ── Apply the decision ───────────────────────────────────────────────────
    if decision.get("continue") and self_name:
        next_step = (decision.get("next_step") or "").strip()
        new_goals = (decision.get("updated_goals") or "").strip()
        if apply_goals and new_goals and new_goals.lower() not in ("", "none"):
            state["goals"] = new_goals
            try:
                up = CAPABILITY_REGISTRY.get("dream.trigger.upsert")
                if up:
                    await up["func"](name=self_name, goals=new_goals)
            except Exception as e:
                log.debug("iterate goal update: %s", e)
        watch = [s for s in (decision.get("watch_sensors") or [])
                 if isinstance(s, str)]
        if apply_sensors and watch:
            try:
                up = CAPABILITY_REGISTRY.get("dream.trigger.upsert")
                merged = sorted(set((trig.get("sensors") or []) + watch))
                if up:
                    await up["func"](name=self_name, sensors=merged)
            except Exception as e:
                log.debug("iterate sensor update: %s", e)
        # Reuse the pivot reschedule path (continue = same trigger + journal)
        state["pivot"] = {
            "to_trigger":  self_name,
            "reason":      (decision.get("reason") or "")[:300],
            "focus_topic": next_step,
            "confidence":  float(decision.get("satisfaction", 0.5) or 0.5),
            "continue":    True,
        }
        await _journal_append(journal_id,
            f"Iterating (iter {depth + 1}/{max_iters}, satisfaction "
            f"{decision.get('satisfaction','?')}): {decision.get('reason','')}. "
            f"Next: {next_step}"
            + (f" · completion = {decision.get('completion_threshold','')}"
               if decision.get('completion_threshold') else ""),
            kind="iterate", stage="iterate", title="Continue / iterate")
    else:
        state["pivot"] = None  # ensure no stale continuation from a prior stage
        await _journal_append(journal_id,
            f"Dream complete — {decision.get('reason') or hard_stop or 'goal satisfied'} "
            f"(iter {depth + 1}, satisfaction {decision.get('satisfaction','n/a')}).",
            kind="iterate", stage="iterate", title="Complete")

    state["iterate_decision"] = {
        "continue":     bool(decision.get("continue")),
        "satisfaction": decision.get("satisfaction"),
        "reason":       decision.get("reason") or hard_stop or "",
        "next_step":    decision.get("next_step", ""),
        "completion_threshold": decision.get("completion_threshold", ""),
        "iteration":    depth + 1, "max_iterations": max_iters,
        "elapsed_s":    int(elapsed), "idle_minutes": round(idle, 1),
        "sensor_signal": sensor_signal, "basis": basis,
    }
    await emit_event({"type": "dream.stage.completed", "cycle_id": cycle_id,
                      "stage": "dream.stage.iterate",
                      "decision": state["iterate_decision"]})
    return state


# ─────────────────────────────────────────────────────────────────────────────
# CYCLE RUNNER
# ─────────────────────────────────────────────────────────────────────────────

async def _run_cycle(
    trig: Dict[str, Any],
    force: bool = False,
    seed: Optional[Dict[str, Any]] = None,
    preview_only: bool = False,
) -> Dict[str, Any]:
    """
    Run a dream cycle for `trig`.
      seed: optional dict merged into state — fields the panel can curate:
        focus_topic       (str)         — extra theme/topic to direct synthesis
        pinned_memory_ids (list[str])   — memory record ids to include in gather
        extra_fabric_ids  (list[str])   — fabric record ids to include
        extra_prompt      (str)         — appended to the trigger's prompt
        force_caps        (list[str])   — restrict planner to this whitelist
        skip_stages       (list[str])   — pipeline stages to skip
        only_stages       (list[str])   — restrict the pipeline to these stages
      preview_only: if True, run gather+themes+plan only and DON'T persist a
                    history record. The cycle still emits events.
    """
    global _CYCLE_TASK, _CYCLE_CANCEL
    _CYCLE_CANCEL = False
    cycle_id = uuid.uuid4().hex[:8]
    started = time.time()

    # Apply seed adjustments to a copy of the trigger so we don't mutate it
    trig = dict(trig)
    seed = dict(seed or {})

    # Resolve a referenced composite pipeline: fields from the registered
    # pipeline fill in anything the trigger hasn't set inline (trigger wins).
    pref = trig.get("pipeline_ref") or seed.get("pipeline_ref")
    if pref:
        reg = await _get_pipeline(pref)
        if reg:
            if not trig.get("pipeline") and reg.get("stages"):
                trig["pipeline"] = list(reg["stages"])
            for fld in _PIPELINE_FIELDS:
                if fld == "stages":
                    continue
                if trig.get(fld) in (None, [], {}, "") and reg.get(fld) is not None:
                    trig[fld] = reg[fld]

    if seed.get("extra_prompt"):
        trig["prompt"] = (trig.get("prompt") or "") + "\n\n" + seed["extra_prompt"]
    if seed.get("force_caps"):
        trig["whitelist"] = [c for c in seed["force_caps"] if isinstance(c, str)]

    pipeline_seed = trig.get("pipeline") or [
        "dream.stage.gather", "dream.stage.themes", "dream.stage.plan",
        "dream.stage.execute", "dream.stage.synthesize", "dream.stage.deliver",
    ]
    if seed.get("only_stages"):
        pipeline_seed = [s for s in pipeline_seed if s in set(seed["only_stages"])]
    elif seed.get("skip_stages"):
        skip = set(seed["skip_stages"])
        pipeline_seed = [s for s in pipeline_seed if s not in skip]
    if preview_only:
        # Drop execute / deliver from preview pipelines
        pipeline_seed = [s for s in pipeline_seed
                         if s not in ("dream.stage.execute", "dream.stage.deliver")]

    state: Dict[str, Any] = {
        "trigger": trig, "cycle_id": cycle_id,
        "started_at": now_iso(), "seed": seed, "preview": preview_only,
    }

    # Journal id: each TRIGGER keeps a persistent journal so its "train of
    # thought" accrues across cycles (the unit of continuation). Project dreams
    # share a per-project journal; a seed can override.
    _proj_for_journal = seed.get("project_id") or trig.get("project", "")
    state["journal_id"] = (seed.get("journal_id")
                           or (f"project:{_proj_for_journal}" if _proj_for_journal
                               else f"trigger:{trig.get('name', cycle_id)}"))

    # Overall goals for this trigger + the recent train of thought from its
    # journal — so the dream works toward standing objectives and resumes where
    # it left off rather than starting cold each cycle.
    goals = trig.get("goals") or seed.get("goals") or ""
    if isinstance(goals, list):
        goals = "\n".join(f"- {g}" for g in goals)
    state["goals"] = goals
    if not preview_only:
        try:
            recent = await _journal_read(state["journal_id"], limit=25)
            if recent:
                state["train_of_thought"] = _journal_to_markdown(
                    recent, heading="Recent activity (continue from here)")
        except Exception:
            pass

    # Fold standing goals + the train of thought into the prompt so every stage
    # (goal_refine, agent loop, project_action) works toward the objectives and
    # continues the thread rather than restarting cold.
    _preamble = []
    if state.get("goals"):
        _preamble.append("STANDING GOALS for this dream:\n" + str(state["goals"]))
    if state.get("train_of_thought"):
        _preamble.append(state["train_of_thought"]
                         + "\n(Build on the above — do not repeat completed work.)")
    if _preamble:
        trig["prompt"] = "\n\n".join(_preamble) + "\n\n" + (trig.get("prompt") or "")

    # ── Project isolation ────────────────────────────────────────────────
    # When a cycle is scoped to a project, tag the state so downstream
    # stages know. This prevents cross-contamination between projects
    # when multiple project dreams run close together.
    project_slug = seed.get("project_id") or trig.get("project", "")
    if project_slug:
        state["project_scope"] = project_slug
        # Ensure gather only uses project-relevant sensors
        if "dream.sensor.project_context" not in (trig.get("sensors") or []):
            if trig.get("sensors"):
                trig["sensors"] = list(trig["sensors"])  # copy
                trig["sensors"].insert(0, "dream.sensor.project_context")
            if not trig.get("sensor_params"):
                trig["sensor_params"] = {}
            trig["sensor_params"]["project_context"] = {"project_slug": project_slug}

    await _set_running({
        "cycle_id":   cycle_id,
        "trigger":    trig.get("name"),
        "label":      trig.get("label"),
        "started_at": state["started_at"],
        "pipeline":   pipeline_seed,
        "preview":    preview_only,
    })

    await emit_event({
        "type":     "dream.cycle.started",
        "cycle_id": cycle_id,
        "trigger":  trig.get("name"),
        "pipeline": pipeline_seed,
        "preview":  preview_only,
        "seed_keys": list(seed.keys()) if seed else [],
    })

    pipeline = pipeline_seed

    # ── Iteration configuration ─────────────────────────────────────────
    # Any trigger can opt in to iterative execution by setting:
    #   trig["iterate"] = {
    #     "enabled": True,
    #     "max_iterations": 6,
    #     "min_iterations": 1,
    #     "iterate_stages": ["dream.stage.investigate"],   # default
    #     "convergence_min_new_findings": 1,  # halt if no new findings this iter
    #   }
    #
    # The runner partitions the pipeline into:
    #   pre_stages  — everything before the first iterate_stage  (run once)
    #   iter_stages — the contiguous block of iterate_stages       (looped)
    #   post_stages — everything after the iterate_stages          (run once)
    iter_cfg = trig.get("iterate") or {}
    iter_enabled = bool(iter_cfg.get("enabled", False))
    iter_stage_set = set(iter_cfg.get("iterate_stages") or ["dream.stage.investigate"])
    max_iterations  = int(iter_cfg.get("max_iterations", 6) or 6)
    min_iterations  = int(iter_cfg.get("min_iterations", 1) or 1)
    convergence_min_new = int(iter_cfg.get("convergence_min_new_findings", 1) or 1)

    # Detect whether the pipeline contains any iterate_stage entries
    has_iter_stages = any(s in iter_stage_set for s in pipeline)
    # Implicit enable: if user didn't set iterate.enabled but did include an
    # iterate_stage in the pipeline, treat it as enabled.
    if has_iter_stages and not iter_enabled:
        iter_enabled = True

    pre_stages: List[str] = []
    iter_stages: List[str] = []
    post_stages: List[str] = []
    if iter_enabled and has_iter_stages:
        section = "pre"
        for s in pipeline:
            if s in iter_stage_set:
                section = "iter"
                iter_stages.append(s)
            elif section == "iter":
                # Once we've left the iter block, everything else is post
                section = "post"
                post_stages.append(s)
            elif section == "pre":
                pre_stages.append(s)
            else:
                post_stages.append(s)
    else:
        pre_stages = list(pipeline)

    early_exit = False
    cancelled  = False

    async def _run_one_stage(stage_name: str) -> bool:
        """Run a single stage. Returns False if early-exit should halt."""
        nonlocal early_exit, state
        if _CYCLE_CANCEL:
            return False
        cap = CAPABILITY_REGISTRY.get(stage_name)
        if not cap:
            state[stage_name] = {"error": "unknown stage"}
            return True
        await emit_event({
            "type":     "dream.stage.started",
            "cycle_id": cycle_id, "stage": stage_name,
            "iteration": state.get("iteration_index"),
        })
        try:
            result = await cap["func"](state=state)
            # Stages return the SAME state dict (mutation pattern). Only rebind
            # if the cap returned a genuinely different dict (defensive — shouldn't
            # happen with our stages, but allows third-party stages that return
            # a fresh dict).
            if isinstance(result, dict) and result is not state:
                state = result
        except Exception as e:
            state[stage_name] = {"error": str(e)}
        # Journal a compact per-stage note (most dream activity is journalled)
        if trig.get("journal", True) and not preview_only:
            short = stage_name.replace("dream.stage.", "")
            note = _stage_journal_note(short, state)
            await _journal_append(
                state.get("journal_id") or cycle_id, note,
                kind="stage", stage=short,
                title=f"Stage: {short}"
                      + (f" (iter {state['iteration_index']})"
                         if state.get("iteration_index") else ""))
        # Low-signal early exit only after gather
        if stage_name == "dream.stage.gather":
            sig = float(((state.get("gather") or {}).get("signal") or 0.0))
            req = float(trig.get("require_signal", 0.0) or 0.0)
            if not force and sig < req:
                early_exit = True
                state["early_exit"] = {"reason": "low_signal",
                                        "signal": sig, "required": req}
                return False
        return True

    # ── Pre-iteration stages ───────────────────────────────────────────
    for stage_name in pre_stages:
        if not await _run_one_stage(stage_name):
            cancelled = _CYCLE_CANCEL
            break

    if not early_exit and not _CYCLE_CANCEL and iter_enabled and iter_stages:
        # ── Agentic iteration loop ─────────────────────────────────────────
        # The iterate stages (investigate / agent_loop / project_action) each
        # delegate to dag.agent_loop_v2's internal ReAct loop. We additionally
        # run that block REPEATEDLY here — up to max_iterations — so the dream
        # builds on its own accumulated findings/journal across passes, and we
        # halt early once a pass stops producing materially new findings
        # (convergence) after min_iterations. This is the "stronger iteration".
        state.setdefault("iterations", [])
        state.setdefault("findings", [])
        state["iterate"] = {"enabled": True, "stop": False, "completed": 0}

        await emit_event({
            "type":          "dream.iterate.start",
            "cycle_id":      cycle_id,
            "max_iterations": max_iterations,
            "min_iterations": min_iterations,
            "iterate_stages": iter_stages,
            "engine":        "dag.agent_loop_v2",
        })

        last_finding_count = 0
        completed = 0
        for i in range(1, max_iterations + 1):
            if _CYCLE_CANCEL:
                cancelled = True
                break
            state["iteration_index"] = i
            await _journal_append(
                state.get("journal_id") or cycle_id,
                f"Iteration {i}/{max_iterations} — building on "
                f"{last_finding_count} prior finding(s).",
                kind="note", stage="iterate", title=f"Iteration {i} started")
            await emit_event({"type": "dream.iterate.pass", "cycle_id": cycle_id,
                              "iteration": i, "max": max_iterations})

            for stage_name in iter_stages:
                if _CYCLE_CANCEL:
                    cancelled = True
                    break
                if not await _run_one_stage(stage_name):
                    break
            if cancelled:
                break

            completed = i
            new_count = len(state.get("findings", []))
            delta = new_count - last_finding_count
            state["iterations"].append({
                "iteration": i, "findings_total": new_count, "new_findings": delta,
            })
            it_loop = state.get("iterate") or {}

            # Respect an explicit satisfaction stop from the stage/loop
            stage_stop = bool(it_loop.get("stop_requested") or it_loop.get("satisfied"))
            # Convergence: after min_iterations, halt if too few new findings
            converged = (i >= min_iterations and delta < convergence_min_new)

            if stage_stop or converged:
                state["iterate"]["stop_reason"] = (
                    "satisfied" if stage_stop else "converged")
                await _journal_append(
                    state.get("journal_id") or cycle_id,
                    f"Stopping after iteration {i}: "
                    f"{state['iterate']['stop_reason']} "
                    f"(+{delta} new findings).",
                    kind="note", stage="iterate", title="Iteration converged")
                break
            last_finding_count = new_count

        it_state = state.get("iterate") or {}
        it_state["completed"] = completed or 1
        it_state["stop"] = True
        state["iterate"] = it_state
        state.pop("iteration_index", None)

        await emit_event({
            "type":                  "dream.iterate.end",
            "cycle_id":              cycle_id,
            "completed_iterations":  it_state.get("completed", 1),
            "total_findings":        len(state.get("findings", [])),
            "stop_reason":           it_state.get("stop_reason", "max_iterations"),
            "engine":                it_state.get("engine", "dag.agent_loop_v2"),
        })

    # ── Post-iteration stages (synthesize, deliver, etc.) ──────────────
    if not early_exit and not _CYCLE_CANCEL:
        for stage_name in post_stages:
            if not await _run_one_stage(stage_name):
                cancelled = _CYCLE_CANCEL
                break

    if cancelled or _CYCLE_CANCEL:
        state.setdefault("cancelled", True)

    elapsed = time.time() - started
    record = {
        "cycle_id":   cycle_id,
        "trigger":    trig.get("name"),
        "label":      trig.get("label"),
        "title":      state.get("title") or trig.get("label") or trig.get("name", "dream"),
        "started_at": state.get("started_at"),
        "ended_at":   now_iso(),
        "elapsed_s":  round(elapsed, 2),
        "signal":     ((state.get("gather") or {}).get("signal") or 0.0),
        "themes":     state.get("themes", []),
        "early_exit": state.get("early_exit"),
        "cancelled":  state.get("cancelled", False),
        "report":     state.get("report", "") if not early_exit else "",
        "delivered":  state.get("delivered", {}),
        "execute":    {k: v for k, v in (state.get("execute") or {}).items() if k != "state"},
        "seed":       state.get("seed") or {},
        "trigger_prompt": trig.get("prompt", ""),
        "has_detail": True,
    }

    # ── Store full cycle detail separately (too large for the history list) ──
    # This captures everything: sensor inputs, goal refinement, tool calls,
    # LLM reasoning, findings, snapshot data — the complete execution trace.
    detail = {
        "cycle_id":      cycle_id,
        "trigger":       trig.get("name"),
        "trigger_full":  {k: v for k, v in trig.items() if k != "prompt"},
        "trigger_prompt": trig.get("prompt", ""),
        "output_style":  trig.get("output_style", ""),
        "pipeline":      trig.get("pipeline", []),
        "started_at":    state.get("started_at"),
        "ended_at":      now_iso(),
        "elapsed_s":     round(elapsed, 2),
        "themes":        state.get("themes", []),
        "report":        state.get("report", ""),
        "title":         state.get("title", ""),
        # Full sensor gather data (inputs)
        "gather":        state.get("gather", {}),
        # Goal refinement
        "goal_refine":   state.get("goal_refine"),
        "refined_goal":  state.get("refined_goal"),
        # Snapshot (source review)
        "snapshot":      state.get("snapshot"),
        # Execution data — agent loop steps, tool calls, findings
        "stepwise":      state.get("stepwise"),
        "agent_loop":    state.get("agent_loop"),
        "findings":      state.get("findings"),
        "iterations":    state.get("iterations"),
        "iterate":       state.get("iterate"),
        # Plan + execute (DAG mode)
        "plan":          state.get("plan"),
        "execute":       state.get("execute"),
        # Quality check + enrichment
        "quality_check": state.get("quality_check"),
        "enriched":      state.get("enriched"),
        "proposed_action": state.get("proposed_action"),
        # Delivery
        "delivered":     state.get("delivered", {}),
        "seed":          state.get("seed") or {},
        "early_exit":    state.get("early_exit"),
        "cancelled":     state.get("cancelled", False),
    }

    # Store detail in Redis hash keyed by cycle_id (TTL 7 days)
    r = _redis()
    if r and not preview_only:
        try:
            detail_key = f"vera:dream:detail:{cycle_id}"
            await r.set(detail_key, json.dumps(detail, default=str))
            await r.expire(detail_key, 7 * 86400)  # 7 days
        except Exception as e:
            log.debug("dream detail store: %s", e)

    record["preview"] = preview_only

    if preview_only:
        # Don't persist, don't reset cooldown — just cache for the panel
        r = _redis()
        if r:
            try:
                await r.hset(KEY_PREVIEW, trig.get("name", "?"),
                             json.dumps({**record, "ts": now_iso()}))
            except Exception:
                pass
    else:
        await _push_history(record)
        await _mark_trigger_run(trig.get("name", "?"))

        # ── Store dream to memory graph ──────────────────────────────────
        try:
            mem_store = CAPABILITY_REGISTRY.get("memory.store")
            mem_relate = CAPABILITY_REGISTRY.get("memory.relate")
            if mem_store and record.get("report") and not record.get("early_exit"):
                # Create the dream node
                store_result = await mem_store["func"](
                    text=str(record.get("report", ""))[:4000],
                    session_id="dream",
                    record_type="dream",
                    source_type="system",
                    category=f"dream.{trig.get('name', 'default')}",
                    tags=",".join(["dream", trig.get("name", ""),
                                   record.get("title", "")] +
                                  (record.get("themes") or [])[:5]),
                    summary=record.get("title", ""),
                    importance=0.65,
                    ai_output=True,
                    capability_src="dream.cycle",
                )
                dream_node_id = (store_result or {}).get("id", "")

                # Link to trigger context — find or create a trigger entity node
                if dream_node_id and mem_relate:
                    # Search for an existing trigger node
                    mem_search = CAPABILITY_REGISTRY.get("memory.search")
                    trig_node_id = ""
                    if mem_search:
                        try:
                            sr = await mem_search["func"](
                                query=f"dream trigger {trig.get('name', '')}",
                                limit=1, category="dream.trigger",
                            )
                            for item in (sr or {}).get("results", [])[:1]:
                                r2 = item.get("record", item) if isinstance(item, dict) else {}
                                if r2.get("id"):
                                    trig_node_id = r2["id"]
                        except Exception:
                            pass

                    if not trig_node_id:
                        # Create trigger entity node
                        try:
                            tr = await mem_store["func"](
                                text=f"Dream trigger: {trig.get('label', trig.get('name', ''))}. "
                                     f"{trig.get('description', '')}",
                                session_id="dream",
                                record_type="entity",
                                source_type="system",
                                category="dream.trigger",
                                tags=f"dream,trigger,{trig.get('name','')}",
                                importance=0.3,
                            )
                            trig_node_id = (tr or {}).get("id", "")
                        except Exception:
                            pass

                    # Link dream → trigger
                    if trig_node_id:
                        try:
                            await mem_relate["func"](
                                from_id=dream_node_id,
                                to_id=trig_node_id,
                                relation_type="TRIGGERED_BY",
                            )
                        except Exception:
                            pass

                    # Link to sensor data — connect to any pinned memories from seed
                    for mid in (state.get("seed") or {}).get("pinned_memory_ids", [])[:10]:
                        try:
                            await mem_relate["func"](
                                from_id=dream_node_id,
                                to_id=str(mid),
                                relation_type="INFORMED_BY",
                            )
                        except Exception:
                            pass

                log.debug("dream: stored to memory graph: %s", dream_node_id[:12])
        except Exception as e:
            log.debug("dream memory graph: %s", e)

    await _set_running(None)

    # Project hook — if this cycle was scoped to a project, update its rolling context
    project_slug = (state.get("seed") or {}).get("project_id") or trig.get("project")
    if project_slug and not preview_only and not early_exit:
        try:
            proj_hook = CAPABILITY_REGISTRY.get("project.dream.complete_hook")
            if proj_hook:
                await proj_hook["func"](
                    slug=project_slug,
                    cycle_id=cycle_id,
                    trigger=trig.get("name", ""),
                    report=record.get("report", "") or "",
                )
        except Exception as e:
            log.debug("dream project hook: %s", e)

    await emit_event({
        "type":       "dream.cycle.completed",
        "cycle_id":   cycle_id,
        "trigger":    trig.get("name"),
        "title":      record.get("title"),
        "elapsed_s":  record["elapsed_s"],
        "early_exit": bool(early_exit),
        "preview":    preview_only,
        "project":    project_slug,
        "delivered":  state.get("delivered", {}),
        "has_detail": True,
    })

    # ── Auto-continue hook ───────────────────────────────────────────────
    # If the seed requested auto_continue and the cycle produced next steps,
    # schedule a follow-up after a short cooldown. Cap depth to prevent
    # infinite loops.
    if not preview_only and not early_exit and not cancelled:
        seed_data = state.get("seed") or {}
        if seed_data.get("auto_continue"):
            depth = int(seed_data.get("continuation_depth", 1))
            max_depth = int(trig.get("max_continuation_depth", 3))
            if depth < max_depth:
                # Check if the report suggests more work
                has_next = any(phrase in (record.get("report", "").lower())
                               for phrase in ["next step", "should ", "could ",
                                              "todo", "open thread", "action item",
                                              "investigate further", "follow up"])
                if has_next:
                    log.info("dream: auto-continue depth %d/%d for %s",
                             depth + 1, max_depth, cycle_id)
                    # Schedule after cooldown (don't block this return)
                    async def _schedule_continue():
                        await asyncio.sleep(30)  # 30s cooldown between continuations
                        try:
                            await dream_cycle_continue(
                                cycle_id=cycle_id,
                                auto_continue=True,
                            )
                        except Exception as e:
                            log.debug("dream auto-continue: %s", e)
                    asyncio.create_task(_schedule_continue())

    # ── Pivot hook ───────────────────────────────────────────────────────
    # If dream.stage.pivot decided to hand off to a *different* dream, launch
    # that trigger after a cooldown, carrying the focus + continuation depth so
    # pivots can't recurse forever.
    if not preview_only and not early_exit and not cancelled:
        pivot = state.get("pivot") or {}
        if pivot.get("to_trigger"):
            seed_data = state.get("seed") or {}
            depth = int(seed_data.get("pivot_depth", 0))
            _iter_cfg = ((trig.get("stage_config", {}) or {}).get("iterate", {})
                         or trig.get("iterate", {}) or {})
            max_pivots = int((trig.get("pivot") or {}).get("max_pivots",
                             max(int(trig.get("max_continuation_depth", 3) or 3),
                                 int(_iter_cfg.get("max_iterations", 0) or 0))))
            if depth < max_pivots:
                target = pivot["to_trigger"]
                is_cont = bool(pivot.get("continue"))
                _focus = pivot.get("focus_topic", "")
                pivot_seed = {
                    "focus_topic": _focus,
                    "extra_prompt": (("NEXT STEP (continue the train of thought): "
                                      if is_cont else "Follow-up focus: ") + _focus)
                                    if _focus else "",
                    "pivoted_from": trig.get("name", ""),
                    "pivot_reason": pivot.get("reason", ""),
                    "pivot_depth": depth + 1,
                    "continue": is_cont,
                    # Carry project scope so a project dream pivots within the project
                    "project_id": project_slug or seed_data.get("project_id", ""),
                    # Share the journal so the train of thought continues
                    "journal_id": state.get("journal_id"),
                }
                log.info("dream: %s %s -> %s (depth %d/%d) for %s",
                         "continue" if is_cont else "pivot",
                         trig.get("name"), target, depth + 1, max_pivots, cycle_id)

                async def _schedule_pivot():
                    await asyncio.sleep(30)
                    try:
                        run_cap = CAPABILITY_REGISTRY.get("dream.cycle.run")
                        if run_cap:
                            await run_cap["func"](trigger_name=target, seed=pivot_seed)
                    except Exception as e:
                        log.debug("dream pivot launch: %s", e)
                asyncio.create_task(_schedule_pivot())
                await emit_event({"type": "dream.pivot.scheduled",
                                  "cycle_id": cycle_id, "to_trigger": target,
                                  "continue": is_cont, "depth": depth + 1})

    # ── Stage-to-DAG handover ────────────────────────────────────────────
    # If the cycle produced next steps and has a handover config, queue
    # them as a DAG for later execution
    handover = state.get("handover") or trig.get("handover") or {}
    if handover.get("enabled") and not preview_only and not early_exit:
        dag_store_cap = CAPABILITY_REGISTRY.get("dag.save")
        if dag_store_cap:
            try:
                # Extract next steps from proposed_action or project_action
                action_data = state.get("project_action") or state.get("proposed_action") or {}
                handover_goal = ""
                if isinstance(action_data, dict):
                    handover_goal = action_data.get("goal") or action_data.get("action") or ""
                if not handover_goal:
                    handover_goal = state.get("refined_goal", "")

                if handover_goal:
                    dag_name = f"dream_handover_{cycle_id}"
                    await dag_store_cap["func"](
                        dag_id=dag_name,
                        steps=[],  # Empty steps — to be planned by agent loop
                        metadata={
                            "source": "dream_handover",
                            "cycle_id": cycle_id,
                            "trigger": trig.get("name", ""),
                            "goal": handover_goal[:1000],
                            "project": project_slug or "",
                            "themes": state.get("themes", [])[:5],
                        },
                    )
                    await emit_event({
                        "type": "dream.handover.created",
                        "cycle_id": cycle_id,
                        "dag_id": dag_name,
                        "goal": handover_goal[:200],
                    })
                    log.info("dream: handover DAG created: %s", dag_name)
            except Exception as e:
                log.debug("dream handover: %s", e)

    _CYCLE_TASK = None
    return record


async def _eval_trigger_sensors(trig: Dict[str, Any]) -> Dict[str, Any]:
    """Run a trigger's sensors and evaluate the firing condition.

    Per-sensor config lives in trig['sensor_params'][<short_name>] and may
    include, in addition to the sensor's own params (limit, etc.):
      • match        — regex (or plain substring) tested against the sensor's
                        sample/summary text; the trigger only fires if it hits
      • match_field  — which field to test ('sample'|'summary'|'all', default 'all')
      • min_signal   — per-sensor signal floor (overrides trig.require_signal
                        for that sensor)
      • negate       — if true, fire only when the match does NOT hit

    Returns {signal, matched, detail} where `signal` is the max across sensors
    and `matched` reflects all configured match conditions."""
    sensors = trig.get("sensors") or []
    if not sensors:
        return {"signal": 1.0, "matched": True, "detail": "(no sensors)"}
    params = trig.get("sensor_params") or {}
    sigs: List[float] = []
    any_match_cfg = False
    all_matched = True
    details: List[str] = []
    for sid in sensors:
        short = sid.rsplit(".", 1)[-1]
        p = dict(params.get(short) or params.get(sid) or {})
        match       = p.pop("match", "") or ""
        match_field = p.pop("match_field", "all")
        min_signal  = p.pop("min_signal", None)
        negate      = bool(p.pop("negate", False))
        cap = CAPABILITY_REGISTRY.get(sid) or CAPABILITY_REGISTRY.get(f"dream.sensor.{short}")
        if not cap:
            continue
        try:
            res = await cap["func"](**p) or {}
        except Exception as e:
            log.debug("sensor %s eval: %s", sid, e)
            continue
        sig = float(res.get("signal", 0) or 0)
        sigs.append(sig)
        if min_signal is not None and sig < float(min_signal):
            all_matched = False
            details.append(f"{short}: signal {sig:.2f}<{float(min_signal):.2f}")
            continue
        if match:
            any_match_cfg = True
            if match_field == "sample":
                blob = json.dumps(res.get("sample") or res.get("items") or "", default=str)
            elif match_field == "summary":
                blob = str(res.get("summary") or "")
            else:
                blob = json.dumps(res, default=str)
            try:
                hit = bool(re.search(match, blob, re.I))
            except re.error:
                hit = match.lower() in blob.lower()
            if negate:
                hit = not hit
            if not hit:
                all_matched = False
                details.append(f"{short}: no match /{match}/")
            else:
                details.append(f"{short}: matched /{match}/")
    signal = max(sigs) if sigs else 0.0
    return {"signal": signal, "matched": all_matched,
            "detail": "; ".join(details) or f"signal {signal:.2f}",
            "match_configured": any_match_cfg}


async def _trigger_due(trig: Dict[str, Any], idle_min: float) -> bool:
    if not trig.get("enabled"):
        return False
    if idle_min < float(trig.get("min_idle_minutes", 15)):
        return False
    if not _within_hours(int(trig.get("hours_start", 0)), int(trig.get("hours_end", 24))):
        return False
    last = await _last_run_ts(trig.get("name", "?"))
    if last:
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            mins_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60.0
            if mins_since < float(trig.get("min_interval_minutes", 60)):
                return False
        except Exception:
            pass
    # Sensor gate: only fire when the trigger's sensors clear their signal
    # threshold AND any configured match (regex/text) condition holds. This is
    # the single, coherent place firing is decided — configurable per trigger
    # via sensor_params[<sensor>].{match,match_field,min_signal,negate}.
    if trig.get("sensors"):
        ev = await _eval_trigger_sensors(trig)
        require = float(trig.get("require_signal", 0) or 0)
        if ev["signal"] < require:
            return False
        if not ev["matched"]:
            await emit_event({"type": "dream.trigger.gated", "trigger": trig.get("name"),
                              "reason": ev["detail"]})
            return False
    return True


async def _scheduler_loop():
    global _SCHED_RUN, _CYCLE_TASK
    log.info("dream scheduler started")
    await emit_event({"type": "dream.scheduler.started"})
    while _SCHED_RUN:
        try:
            cfg = await _get_config()
            tick = int(cfg.get("tick_interval_seconds", 60))
            if not cfg.get("enabled"):
                await asyncio.sleep(tick)
                continue

            if _CYCLE_TASK and not _CYCLE_TASK.done():
                await asyncio.sleep(tick)
                continue

            idle = await _idle_minutes()
            if idle < float(cfg.get("min_idle_minutes", 15)):
                await asyncio.sleep(tick)
                continue

            triggers = await _list_triggers()
            for trig in triggers:
                if await _trigger_due(trig, idle):
                    log.info("dream firing trigger: %s (idle %.1fm)", trig.get("name"), idle)
                    _CYCLE_TASK = asyncio.create_task(_run_cycle(trig))
                    break  # one per tick

            await asyncio.sleep(tick)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("dream scheduler loop: %s", e)
            await asyncio.sleep(30)

    log.info("dream scheduler stopped")
    await emit_event({"type": "dream.scheduler.stopped"})


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES — SCHEDULER / CYCLE LIFECYCLE
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.scheduler.start", memory="off",
    http_method="POST", http_path="/dream/scheduler/start", http_tags=["dream"],
    description="Start the dream scheduler (idle-triggered background loop).",
)
async def dream_scheduler_start(trace_id=None):
    global _SCHED_TASK, _SCHED_RUN
    if _SCHED_RUN and _SCHED_TASK and not _SCHED_TASK.done():
        return {"running": True, "note": "already running"}
    _SCHED_RUN = True
    _SCHED_TASK = asyncio.create_task(_scheduler_loop())
    return {"running": True}


@capability(
    "dream.scheduler.stop", memory="off",
    http_method="POST", http_path="/dream/scheduler/stop", http_tags=["dream"],
    description="Stop the dream scheduler.",
)
async def dream_scheduler_stop(trace_id=None):
    global _SCHED_TASK, _SCHED_RUN
    _SCHED_RUN = False
    if _SCHED_TASK and not _SCHED_TASK.done():
        _SCHED_TASK.cancel()
        try:
            await asyncio.wait_for(_SCHED_TASK, timeout=3)
        except Exception:
            pass
    _SCHED_TASK = None
    return {"running": False}


@capability(
    "dream.scheduler.status", memory="off", silent=True,
    http_method="GET", http_path="/dream/scheduler/status", http_tags=["dream"],
    description="Dream scheduler status — running, current cycle, idle minutes.",
)
async def dream_scheduler_status(trace_id=None):
    cfg = await _get_config()
    idle = await _idle_minutes()
    running_cycle = await _get_running()
    return {
        "scheduler_running": _SCHED_RUN and bool(_SCHED_TASK) and not (_SCHED_TASK.done() if _SCHED_TASK else True),
        "enabled":           bool(cfg.get("enabled")),
        "idle_minutes":      round(idle, 2),
        "min_idle_minutes":  cfg.get("min_idle_minutes"),
        "in_cycle":          bool(running_cycle),
        "current_cycle":     running_cycle,
        "config":            cfg,
    }


@capability(
    "dream.cycle.run", memory="off",
    http_method="POST", http_path="/dream/cycle/run", http_tags=["dream"],
    description="Manually run a dream cycle for a named trigger. Bypasses idle/hours/cooldown checks. "
                "Optional seed (JSON dict) lets you curate the dream: focus_topic, pinned_memory_ids, "
                "extra_fabric_ids, extra_prompt, force_caps, only_stages, skip_stages.",
)
async def dream_cycle_run(
    trigger_name: str,
    seed: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    global _CYCLE_TASK
    trig = await _get_trigger(trigger_name)
    if not trig:
        return {"ok": False, "error": f"unknown trigger: {trigger_name}"}
    if _CYCLE_TASK and not _CYCLE_TASK.done():
        return {"ok": False, "error": "a cycle is already running"}
    # Accept JSON-string seed too (for clients that send everything as strings)
    if isinstance(seed, str):
        try:
            seed = json.loads(seed) if seed.strip() else {}
        except Exception:
            seed = {}
    _CYCLE_TASK = asyncio.create_task(_run_cycle(trig, force=True, seed=seed or {}))
    return {"ok": True, "trigger": trigger_name,
            "seed_keys": list((seed or {}).keys()),
            "note": "cycle started in background"}


@capability(
    "dream.cycle.continue", memory="off",
    http_method="POST", http_path="/dream/cycle/continue", http_tags=["dream"],
    description="Continue from a previous dream cycle. Loads the cycle's detail, "
                "extracts its findings/next_steps/report, and feeds them as seed "
                "context into a new cycle with the same trigger. "
                "Inputs: cycle_id (str!), trigger_name (str, optional — defaults to "
                "same trigger), goal (str, optional — override the continuation goal), "
                "auto_continue (bool, default false — if true, schedule automatic "
                "follow-up after completion).",
)
async def dream_cycle_continue(
    cycle_id: str = "",
    trigger_name: str = "",
    goal: str = "",
    auto_continue: bool = False,
    trace_id=None,
):
    global _CYCLE_TASK
    if not cycle_id:
        return {"ok": False, "error": "cycle_id required"}
    if _CYCLE_TASK and not _CYCLE_TASK.done():
        return {"ok": False, "error": "a cycle is already running"}

    # Load the previous cycle's detail
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}

    detail_key = f"vera:dream:detail:{cycle_id}"
    raw = await r.get(detail_key)
    if not raw:
        return {"ok": False, "error": f"detail not found for cycle {cycle_id}"}

    prev = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    prev_trigger = prev.get("trigger", "")
    trig_name = trigger_name or prev_trigger
    if not trig_name:
        return {"ok": False, "error": "could not determine trigger name"}

    trig = await _get_trigger(trig_name)
    if not trig:
        return {"ok": False, "error": f"trigger not found: {trig_name}"}

    # Build continuation seed from previous cycle
    prev_report = prev.get("report", "")
    prev_findings = prev.get("findings", [])
    prev_goal = prev.get("refined_goal", "")
    prev_themes = prev.get("themes", [])
    prev_project = prev.get("seed", {}).get("project_id", "")
    prev_action = prev.get("project_action", {})

    # Extract next steps from the report (look for ## Next steps section)
    next_steps = ""
    if prev_report:
        import re as _re
        match = _re.search(r"(?:##\s*(?:Next steps|Open threads|TODO|Action items))(.*?)(?=\n##|\Z)",
                           prev_report, _re.IGNORECASE | _re.DOTALL)
        if match:
            next_steps = match.group(1).strip()[:1000]

    # Build the continuation prompt
    continuation_context = []
    if prev_report:
        continuation_context.append(f"PREVIOUS REPORT (from cycle {cycle_id}):\n{prev_report[:2000]}")
    if prev_findings:
        findings_text = "\n".join(f"- [{f.get('source','?')}] {str(f.get('content',''))[:200]}"
                                   for f in prev_findings[:10])
        continuation_context.append(f"PREVIOUS FINDINGS:\n{findings_text}")
    if next_steps:
        continuation_context.append(f"IDENTIFIED NEXT STEPS:\n{next_steps}")
    if prev_action and prev_action.get("goal"):
        continuation_context.append(f"PREVIOUS ACTION GOAL: {prev_action['goal']}")

    seed: Dict[str, Any] = {
        "continuation_of": cycle_id,
        "previous_themes": prev_themes,
        "extra_prompt": (
            "This is a CONTINUATION of a previous dream cycle. "
            "Pick up where the last cycle left off. "
            "Do NOT repeat work that was already done. "
            "Focus on the next steps and open threads.\n\n"
            + "\n\n".join(continuation_context)
        ),
    }
    if goal:
        seed["focus_topic"] = goal
    elif next_steps:
        seed["focus_topic"] = next_steps[:200]
    if prev_project:
        seed["project_id"] = prev_project

    # Auto-continue: tag the seed so the cycle completion hook can schedule another
    if auto_continue:
        seed["auto_continue"] = True
        seed["continuation_depth"] = prev.get("seed", {}).get("continuation_depth", 0) + 1

    _CYCLE_TASK = asyncio.create_task(_run_cycle(trig, force=True, seed=seed))

    await emit_event({
        "type": "dream.cycle.continued",
        "cycle_id": cycle_id,
        "new_trigger": trig_name,
        "auto_continue": auto_continue,
        "depth": seed.get("continuation_depth", 1),
    })

    return {"ok": True, "trigger": trig_name,
            "continuing_from": cycle_id,
            "auto_continue": auto_continue,
            "next_steps_found": bool(next_steps),
            "note": "continuation cycle started"}


@capability(
    "dream.cycle.cancel", memory="off",
    http_method="POST", http_path="/dream/cycle/cancel", http_tags=["dream"],
    description="Request the currently-running dream cycle to stop at its next stage boundary.",
)
async def dream_cycle_cancel(trace_id=None):
    global _CYCLE_CANCEL
    _CYCLE_CANCEL = True
    return {"ok": True, "note": "cancel requested"}


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES — TRIGGERS
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.trigger.list", memory="off", silent=True,
    http_method="GET", http_path="/dream/triggers", http_tags=["dream"],
    description="List all configured dream triggers.",
)
async def dream_trigger_list(trace_id=None):
    triggers = await _list_triggers()
    for t in triggers:
        t["last_run"] = await _last_run_ts(t.get("name", "?"))
    return {"triggers": triggers, "count": len(triggers)}


@capability(
    "dream.trigger.get", memory="off", silent=True,
    http_method="GET", http_path="/dream/trigger/get", http_tags=["dream"],
    description="Get a single dream trigger by name.",
)
async def dream_trigger_get(name: str, trace_id=None):
    trig = await _get_trigger(name)
    if not trig:
        return {"error": "not found"}
    trig["last_run"] = await _last_run_ts(name)
    return {"trigger": trig}


@capability(
    "dream.trigger.upsert", memory="off",
    http_method="POST", http_path="/dream/trigger/upsert", http_tags=["dream"],
    description="Create or update a dream trigger.",
)
async def dream_trigger_upsert(
    name: str,
    label: Optional[str] = None,
    description: Optional[str] = None,
    enabled: Optional[bool] = None,
    sensors: Optional[List[str]] = None,
    pipeline: Optional[List[str]] = None,
    mode: Optional[str] = None,
    hitl: Optional[bool] = None,
    hours_start: Optional[int] = None,
    hours_end: Optional[int] = None,
    min_idle_minutes: Optional[int] = None,
    min_interval_minutes: Optional[int] = None,
    require_signal: Optional[float] = None,
    deliver_to: Optional[List[str]] = None,
    prompt: Optional[str] = None,
    # NEW v3 fields ─────────────────────────────────────────────────────────
    sensor_params: Optional[Dict[str, Any]] = None,   # {sensor_id: {param: val}}
    stage_params:  Optional[Dict[str, Any]] = None,   # {stage_id:  {param: val}}
    whitelist:     Optional[List[str]]      = None,   # per-trigger cap whitelist (overrides global if set)
    no_hitl_caps:  Optional[List[str]]      = None,   # caps that bypass HITL even when hitl=True
    depth:         Optional[str]            = None,   # brief|standard|deep|exhaustive
    max_steps:     Optional[int]            = None,   # for stepwise mode
    director_managed: Optional[bool]        = None,   # if True, director may auto-fire/skip
    # NEW v4 fields ─────────────────────────────────────────────────────────
    stage_config:  Optional[Dict[str, Any]] = None,   # {stage_id: {cfg}} (review_codebase, snapshot_source, ...)
    iterate:       Optional[Dict[str, Any]] = None,   # outer convergence loop config
    pivot:         Optional[Dict[str, Any]] = None,   # {enabled, candidates, min_confidence, max_pivots}
    loop_settings: Optional[Dict[str, Any]] = None,   # per-trigger agent-loop overrides
    handover:      Optional[Dict[str, Any]] = None,   # stage->DAG handover config
    journal:       Optional[bool]           = None,   # journal this trigger's activity
    max_continuation_depth: Optional[int]   = None,   # cap auto-continue / pivot recursion
    project:       Optional[str]            = None,   # project slug this trigger is scoped to
    pipeline_ref:  Optional[str]            = None,   # name of a registered composite pipeline
    goals:         Optional[Any]            = None,   # overall objectives for this trigger (str or list)
    trace_id=None,
):
    if not name:
        return {"ok": False, "error": "name required"}
    existing = await _get_trigger(name) or {
        "name":    name,
        "enabled": True,
        "sensors": ["dream.sensor.memory_recent"],
        "pipeline": ["dream.stage.gather", "dream.stage.themes",
                     "dream.stage.synthesize", "dream.stage.deliver"],
        "mode":    "synthesize_only",
        "hitl":    False,
        "hours_start": 0, "hours_end": 24,
        "min_idle_minutes": 15,
        "min_interval_minutes": 120,
        "require_signal": 0.2,
        "deliver_to": ["memory"],
        "sensor_params": {}, "stage_params": {},
        "whitelist": [], "no_hitl_caps": [],
        "depth": "standard", "max_steps": 6,
        "director_managed": False,
    }

    fields = {
        "label": label, "description": description, "enabled": enabled,
        "sensors": sensors, "pipeline": pipeline, "mode": mode, "hitl": hitl,
        "hours_start": hours_start, "hours_end": hours_end,
        "min_idle_minutes": min_idle_minutes,
        "min_interval_minutes": min_interval_minutes,
        "require_signal": require_signal,
        "deliver_to": deliver_to, "prompt": prompt,
        "sensor_params": sensor_params, "stage_params": stage_params,
        "whitelist": whitelist, "no_hitl_caps": no_hitl_caps,
        "depth": depth, "max_steps": max_steps,
        "director_managed": director_managed,
        # v4
        "stage_config": stage_config, "iterate": iterate, "pivot": pivot,
        "loop_settings": loop_settings, "handover": handover, "journal": journal,
        "max_continuation_depth": max_continuation_depth, "project": project,
        "pipeline_ref": pipeline_ref, "goals": goals,
    }
    for k, v in fields.items():
        if v is not None:
            existing[k] = v
    existing["name"] = name

    # Validate depth
    if existing.get("depth") not in ("brief", "standard", "deep", "exhaustive"):
        existing["depth"] = "standard"

    await _save_trigger(existing)
    return {"ok": True, "trigger": existing}


@capability(
    "dream.trigger.delete", memory="off",
    http_method="POST", http_path="/dream/trigger/delete", http_tags=["dream"],
    description="Delete a dream trigger by name.",
)
async def dream_trigger_delete(name: str, trace_id=None):
    await _delete_trigger(name)
    return {"ok": True, "deleted": name}


@capability(
    "dream.trigger.toggle", memory="off",
    http_method="POST", http_path="/dream/trigger/toggle", http_tags=["dream"],
    description="Toggle a dream trigger's enabled state.",
)
async def dream_trigger_toggle(name: str, enabled: Optional[bool] = None, trace_id=None):
    trig = await _get_trigger(name)
    if not trig:
        return {"ok": False, "error": "not found"}
    trig["enabled"] = bool(enabled) if enabled is not None else (not trig.get("enabled"))
    await _save_trigger(trig)
    return {"ok": True, "trigger": trig}


# ─────────────────────────────────────────────────────────────────────────────
# COMPOSITE PIPELINE REGISTRY
# ─────────────────────────────────────────────────────────────────────────────
# A pipeline is a reusable, named composite of stages + per-stage config +
# iterate/pivot policy. Triggers may reference one by `pipeline_ref` instead of
# inlining a `pipeline` list, so the same composite can be scheduled, run ad-hoc
# (dream.pipeline.run), or managed from the UI. _run_cycle resolves the ref.

KEY_PIPELINES = "vera:dream:pipelines"   # Redis hash: name -> pipeline JSON

# Fields a pipeline contributes to the effective trigger when referenced.
_PIPELINE_FIELDS = ("stages", "stage_config", "iterate", "pivot", "sensors",
                    "deliver_to", "max_steps", "journal", "whitelist",
                    "no_hitl_caps", "mode", "depth")


def _builtin_pipelines() -> List[Dict[str, Any]]:
    """Built-in composite pipelines, seeded on startup (create-if-absent)."""
    review_stages = ["dream.stage.snapshot_source",
                     "dream.stage.review_codebase",
                     "dream.stage.review_report",
                     "dream.stage.deliver",
                     "dream.stage.pivot"]
    review_caps = ["ide.inspect.snapshot", "ide.inspect.list_snapshots",
                   "ide.inspect.diff_snapshot", "ide.inspect.review_file",
                   "ide.inspect.plan_improvement", "ide.inspect.source_info",
                   "memory.create", "dream.journal.append",
                   "llm.generate", "llm.summarize"]
    base = {
        "kind": "source_review", "mode": "agent_loop", "journal": True,
        "depth": "standard", "max_steps": 8, "deliver_to": ["notebook", "memory"],
        "sensors": ["dream.sensor.source_changes"],
        "whitelist": review_caps,
        "no_hitl_caps": review_caps,
        "stages": review_stages,
    }
    return [
        {**base, "name": "source_review_changes",
         "label": "Source Review — Recent Changes",
         "description": "Snapshot if source changed, then review the files that "
                        "changed since the last snapshot and report.",
         "stage_config": {"review_codebase": {"review_type": "changes",
                                              "max_files": 40, "plan": True}},
         "pivot": {"enabled": True, "min_confidence": 0.6,
                   "candidates": ["source_review_continue"]}},
        {**base, "name": "source_review_wander",
         "label": "Source Review — General Wander",
         "description": "Roam the whole codebase a window at a time (rotating "
                        "cursor), reviewing files regardless of changes.",
         "sensors": ["dream.sensor.source_review_state"],
         "stage_config": {"review_codebase": {"review_type": "wander",
                                              "max_files": 12, "plan": True}},
         "pivot": {"enabled": False}},
        {**base, "name": "source_review_continue",
         "label": "Source Review — Continue Previous",
         "description": "Continue a previous review: pick up files not yet "
                        "reviewed against the current snapshot.",
         "sensors": ["dream.sensor.source_review_state"],
         "stage_config": {"review_codebase": {"review_type": "continue",
                                              "max_files": 25, "plan": True}},
         "pivot": {"enabled": False}},
        {**base, "name": "source_review_fix",
         "label": "Source Review — Draft Fixes",
         "description": "Review recent changes, then use the IDE writer agent to "
                        "draft fixes for high-severity files into a workspace.",
         "stages": ["dream.stage.snapshot_source", "dream.stage.review_codebase",
                    "dream.stage.review_report", "dream.stage.ide_workspace_act",
                    "dream.stage.deliver"],
         "whitelist": review_caps + ["ide.agent.chat", "ide.workspace.create",
                                     "ide.fs.read", "ide.fs.write"],
         "stage_config": {"review_codebase": {"review_type": "changes",
                                              "max_files": 40, "plan": True},
                          "ide_workspace_act": {"enabled": True,
                                               "workspace": "vera-review-fixes",
                                               "max_files": 3}},
         "pivot": {"enabled": False}},
        {**base, "name": "source_review_deep",
         "label": "Source Review — Deep (Whole Project)",
         "description": "In-depth review of EVERY module across multiple styles "
                        "(docs, critique, improvement, integration, architecture). "
                        "Produces long detailed reports per file, browsable per "
                        "area in the Source Review panel.",
         "kind": "source_review_deep",
         "sensors": ["dream.sensor.source_review_state"],
         "stages": ["dream.stage.deep_review", "dream.stage.deliver"],
         "whitelist": review_caps + ["ide.fs.read"],
         "stage_config": {"deep_review": {
             "styles": ["docs", "critique", "improvement", "integration",
                        "architecture"],
             "area": "", "max_files": 0, "max_chars": 14000}},
         "max_steps": 1, "pivot": {"enabled": False}},
    ]


async def _get_pipeline(name: str) -> Optional[Dict[str, Any]]:
    r = _redis()
    if not r:
        for p in _builtin_pipelines():
            if p["name"] == name:
                return p
        return None
    try:
        raw = await r.hget(KEY_PIPELINES, name)
        if raw:
            return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception as e:
        log.debug("get pipeline: %s", e)
    return None


async def _save_pipeline(p: Dict[str, Any]):
    r = _redis()
    if not r:
        return
    try:
        await r.hset(KEY_PIPELINES, p["name"], json.dumps(p, default=str))
    except Exception as e:
        log.warning("save pipeline: %s", e)


@capability(
    "dream.pipeline.list", memory="off", silent=True,
    http_method="GET", http_path="/dream/pipelines", http_tags=["dream"],
    description="List registered composite pipelines. "
                "Output: {pipelines: [{name, label, description, kind, stages, ...}]}.",
)
async def dream_pipeline_list(trace_id=None):
    r = _redis()
    out: Dict[str, Dict[str, Any]] = {p["name"]: p for p in _builtin_pipelines()}
    if r:
        try:
            h = await r.hgetall(KEY_PIPELINES)
            for k, v in (h or {}).items():
                try:
                    p = json.loads(v.decode() if isinstance(v, bytes) else v)
                    out[p.get("name") or (k.decode() if isinstance(k, bytes) else k)] = p
                except Exception:
                    continue
        except Exception:
            pass
    return {"pipelines": sorted(out.values(), key=lambda x: x.get("name", ""))}


@capability(
    "dream.pipeline.get", memory="off", silent=True,
    http_method="GET", http_path="/dream/pipeline/get", http_tags=["dream"],
    description="Get one composite pipeline by name. Input: name (str!).",
)
async def dream_pipeline_get(name: str, trace_id=None):
    p = await _get_pipeline(name)
    return {"ok": bool(p), "pipeline": p} if p else {"ok": False, "error": "not found"}


@capability(
    "dream.pipeline.upsert", memory="off",
    http_method="POST", http_path="/dream/pipeline/upsert", http_tags=["dream"],
    description="Create or update a composite pipeline the user can manage and "
                "schedule. Input: name (str!), label, description, kind, "
                "stages (JSON list!), stage_config (JSON), iterate (JSON), "
                "pivot (JSON), sensors (JSON list), deliver_to (JSON list), "
                "max_steps (int), journal (bool), whitelist (JSON list), "
                "no_hitl_caps (JSON list), mode, depth. "
                "Output: {ok, pipeline}.",
)
async def dream_pipeline_upsert(
    name: str,
    label: str = "",
    description: str = "",
    kind: str = "custom",
    stages: Optional[Any] = None,
    stage_config: Optional[Any] = None,
    iterate: Optional[Any] = None,
    pivot: Optional[Any] = None,
    sensors: Optional[Any] = None,
    deliver_to: Optional[Any] = None,
    max_steps: Optional[int] = None,
    journal: Optional[bool] = None,
    whitelist: Optional[Any] = None,
    no_hitl_caps: Optional[Any] = None,
    mode: str = "",
    depth: str = "",
    trace_id=None,
):
    if not name:
        return {"ok": False, "error": "name required"}

    def _j(v, default):
        if v is None:
            return default
        if isinstance(v, str):
            try:
                return json.loads(v) if v.strip() else default
            except Exception:
                return default
        return v

    existing = await _get_pipeline(name) or {"name": name, "kind": kind}
    p = dict(existing)
    p["name"] = name
    if label:       p["label"] = label
    if description: p["description"] = description
    if kind:        p["kind"] = kind
    if mode:        p["mode"] = mode
    if depth:       p["depth"] = depth
    if max_steps is not None:  p["max_steps"] = int(max_steps)
    if journal is not None:    p["journal"] = bool(journal)
    if stages is not None:        p["stages"] = _j(stages, p.get("stages", []))
    if stage_config is not None:  p["stage_config"] = _j(stage_config, p.get("stage_config", {}))
    if iterate is not None:       p["iterate"] = _j(iterate, p.get("iterate", {}))
    if pivot is not None:         p["pivot"] = _j(pivot, p.get("pivot", {}))
    if sensors is not None:       p["sensors"] = _j(sensors, p.get("sensors", []))
    if deliver_to is not None:    p["deliver_to"] = _j(deliver_to, p.get("deliver_to", []))
    if whitelist is not None:     p["whitelist"] = _j(whitelist, p.get("whitelist", []))
    if no_hitl_caps is not None:  p["no_hitl_caps"] = _j(no_hitl_caps, p.get("no_hitl_caps", []))

    if not p.get("stages"):
        return {"ok": False, "error": "stages required (non-empty list)"}

    await _save_pipeline(p)
    return {"ok": True, "pipeline": p}


@capability(
    "dream.pipeline.delete", memory="off",
    http_method="POST", http_path="/dream/pipeline/delete", http_tags=["dream"],
    description="Delete a composite pipeline by name (built-ins re-seed on "
                "restart). Input: name (str!).",
)
async def dream_pipeline_delete(name: str, trace_id=None):
    r = _redis()
    if r:
        try:
            await r.hdel(KEY_PIPELINES, name)
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True, "deleted": name}


@capability(
    "dream.pipeline.run", memory="off",
    http_method="POST", http_path="/dream/pipeline/run", http_tags=["dream"],
    description="Run a composite pipeline ad-hoc (builds a transient trigger and "
                "runs a cycle). Input: name (str!), seed (JSON, optional), "
                "force (bool, default true). Output: dream cycle result.",
)
async def dream_pipeline_run(name: str, seed: Optional[Any] = None,
                             force: bool = True, trace_id=None):
    p = await _get_pipeline(name)
    if not p:
        return {"ok": False, "error": f"unknown pipeline: {name}"}
    if isinstance(seed, str):
        try:
            seed = json.loads(seed) if seed.strip() else {}
        except Exception:
            seed = {}
    trig = _trigger_from_pipeline(p)
    trig["enabled"] = True
    if _CYCLE_TASK and not _CYCLE_TASK.done():
        return {"ok": False, "error": "a cycle is already running"}
    return await _run_cycle(trig, force=force, seed=seed or {})


def _trigger_from_pipeline(p: Dict[str, Any]) -> Dict[str, Any]:
    """Build an effective trigger dict from a registered pipeline."""
    return {
        "name":         f"pipeline:{p['name']}",
        "label":        p.get("label", p["name"]),
        "description":  p.get("description", ""),
        "pipeline":     list(p.get("stages") or []),
        "stage_config": p.get("stage_config", {}),
        "iterate":      p.get("iterate", {}),
        "pivot":        p.get("pivot", {}),
        "sensors":      p.get("sensors", ["dream.sensor.memory_recent"]),
        "deliver_to":   p.get("deliver_to", ["memory"]),
        "mode":         p.get("mode", "agent_loop"),
        "depth":        p.get("depth", "standard"),
        "max_steps":    p.get("max_steps", 8),
        "journal":      p.get("journal", True),
        "whitelist":    p.get("whitelist", []),
        "no_hitl_caps": p.get("no_hitl_caps", []),
        "require_signal": 0.0,
        "hours_start": 0, "hours_end": 24,
    }


# ─────────────────────────────────────────────────────────────────────────────
# DEEP SOURCE REVIEW SUBSYSTEM
# ─────────────────────────────────────────────────────────────────────────────
# A thorough, whole-project review engine. It enumerates EVERY source module
# (not just changed files), groups them into subsystem "areas", and for each
# file runs one or more analysis *styles* (docs, critique, improvement,
# integration, architecture). Each style produces a long, detailed markdown
# report from the actual file content — not a surface summary. Reports are
# stored per (file, style) and can be aggregated into per-area documents, all
# browsable from the dedicated Source Review panel.

KEY_REVIEW_REPORTS = "vera:dream:review:reports"   # hash: "file::style" -> JSON
KEY_REVIEW_RUN     = "vera:dream:review:last_run"  # JSON: last run summary
KEY_REVIEW_STATUS  = "vera:dream:review:status"    # JSON: live progress
KEY_REVIEW_RUNLOG  = "vera:dream:review:runlog"    # list: past run summaries
KEY_REVIEW_PAUSE   = "vera:dream:review:paused"    # "1" = manually paused

REVIEW_STYLES: Dict[str, Dict[str, str]] = {
    "docs": {
        "label": "Documentation",
        "system": "You are a senior engineer writing authoritative developer "
                  "documentation. Be thorough, structured, and concrete.",
        "instruction":
            "Write comprehensive developer documentation for this module in "
            "Markdown. Cover: (1) purpose and responsibilities; (2) the key "
            "classes, functions and capabilities it defines, with signatures and "
            "what each does; (3) the data/control flow through the module; "
            "(4) external dependencies and the events/streams it emits or "
            "consumes; (5) configuration and important constants; (6) concrete "
            "usage examples. Use headings and tables where helpful. Be long and "
            "detailed — aim for complete coverage, not a summary.",
    },
    "critique": {
        "label": "Critique",
        "system": "You are a meticulous staff engineer doing a rigorous code "
                  "critique. Be specific and cite concrete locations.",
        "instruction":
            "Provide a detailed critique of this module in Markdown. Identify "
            "design issues, code smells, correctness risks, race conditions, "
            "error-handling gaps, unclear abstractions, and edge cases. For each "
            "finding give: a severity (critical/high/medium/low), the specific "
            "function or line context, why it matters, and a suggested direction. "
            "Group findings by severity. Be exhaustive and specific.",
    },
    "improvement": {
        "label": "Improvement notes",
        "system": "You are a pragmatic architect proposing concrete, prioritised "
                  "improvements.",
        "instruction":
            "Write detailed improvement notes for this module in Markdown. "
            "Propose concrete refactors and enhancements, prioritised (quick wins "
            "first, then larger refactors). For each: the rationale, the expected "
            "benefit, the risk/effort, and a short sketch of the change (pseudocode "
            "or signatures). Cover performance, readability, testability, and "
            "robustness. Be specific to THIS code, not generic advice.",
    },
    "integration": {
        "label": "Integration ideas",
        "system": "You are a systems architect mapping how a module fits into the "
                  "wider platform and how to integrate it better.",
        "instruction":
            "Analyse how this module integrates with the rest of the Vera system "
            "in Markdown. Cover: what it depends on and what depends on it; the "
            "capabilities it exposes and how other modules would call them; the "
            "events/streams it participates in; coupling and seams. Then propose "
            "concrete integration ideas — new connections to other subsystems "
            "(dream, ide, memory, fabric, dag, project), new capabilities to "
            "expose, or ways to make it more composable. Be specific and "
            "ambitious but grounded in the actual code.",
    },
    "architecture": {
        "label": "Architecture",
        "system": "You are a principal engineer explaining and assessing module "
                  "architecture.",
        "instruction":
            "Produce a detailed architectural analysis of this module in Markdown: "
            "its role in the overall system, the patterns it uses, its internal "
            "structure and layering, state management, concurrency model, and "
            "failure modes. Include an assessment of how well the architecture "
            "serves its purpose and where it strains. Be thorough.",
    },
}

# Grouping is derived from the file's position in the directory tree (reusable
# for any codebase), with an optional LLM-aided grouping layer for flat trees.
KEY_REVIEW_GROUPS = "vera:dream:review:groups"   # snapshot_id -> {group: [rel,...]}


def _source_area(rel: str) -> str:
    """Subsystem/area for a file, from its directory structure. Nested files
    group by their top directory; top-level files group by a leading token in
    the filename (split on '_' / '.') so flat projects still cluster sensibly."""
    rel = (rel or "").replace("\\", "/").lstrip("./")
    if "/" in rel:
        return rel.split("/", 1)[0]
    base = rel.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0]
    # leading token before first underscore (e.g. dream_capabilities -> dream)
    token = stem.split("_", 1)[0]
    return token or "(root)"


_REVIEW_INCLUDE = ("*.py,*.pyi,*.js,*.jsx,*.ts,*.tsx,*.html,*.css,*.go,*.rs,"
                   "*.java,*.rb,*.c,*.h,*.cpp,*.hpp,*.cs,*.php,*.sh,*.sql,*.yaml,*.yml")


async def _source_root_info() -> Dict[str, str]:
    cap = CAPABILITY_REGISTRY.get("ide.inspect.source_info")
    if not cap:
        return {}
    try:
        info = await cap["func"]() or {}
        return {"source_root": info.get("source_root", ""),
                "snapshot_root": info.get("snapshot_root", "")}
    except Exception:
        return {}


async def _enumerate_source_files(snapshot_id: str, roots: Optional[Dict[str, str]] = None,
                                  max_files: int = 4000) -> List[Dict[str, Any]]:
    """Recursively enumerate every code file in the snapshot tree (all subdirs).
    Falls back to live source root, then to the flat source_info module list."""
    roots = roots or await _source_root_info()
    listing_root = ""
    if roots.get("snapshot_root") and snapshot_id:
        listing_root = f"{roots['snapshot_root']}/{snapshot_id}"
    elif roots.get("source_root"):
        listing_root = roots["source_root"]

    lf = CAPABILITY_REGISTRY.get("ide.code.list_files")
    if lf and listing_root:
        try:
            res = await lf["func"](root=listing_root, include=_REVIEW_INCLUDE,
                                   exclude="*/.git/*,*/node_modules/*,*/__pycache__/*,*/.snapshots/*",
                                   max_files=max_files) or {}
            files = res.get("files", [])
            if files:
                return [{"rel": f.get("rel"), "path": f.get("path"),
                         "size": f.get("size", 0)} for f in files if f.get("rel")]
        except Exception as e:
            log.debug("enumerate via list_files: %s", e)

    # Fallback: flat top-level module list
    mods = await _all_source_modules()
    return [{"rel": m["name"], "path": "", "size": m.get("bytes", 0)} for m in mods]


async def _diff_snapshots(a_id: str, b_id: str) -> Dict[str, List[str]]:
    """Diff two snapshots (not snapshot-vs-live). Returns {modified, added,
    removed} of rel paths. Uses size as a prefilter, then content compare."""
    roots = await _source_root_info()
    a_list = await _enumerate_source_files(a_id, roots)
    b_list = await _enumerate_source_files(b_id, roots)
    a = {f["rel"]: f.get("size", 0) for f in a_list}
    b = {f["rel"]: f.get("size", 0) for f in b_list}
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    modified: List[str] = []
    for rel in sorted(set(a) & set(b)):
        if a[rel] != b[rel]:
            modified.append(rel)
            continue
        # same size — compare content to be sure
        ca = await _read_source_file(roots, a_id, rel, 0)
        cb = await _read_source_file(roots, b_id, rel, 0)
        if ca != cb:
            modified.append(rel)
    return {"modified": modified, "added": added, "removed": removed}


async def _llm_group_files(files: List[str]) -> Dict[str, List[str]]:
    """LLM-aided grouping for flat/ambiguous trees: cluster files into named
    logical groups. Best-effort; returns {} on failure."""
    if not files:
        return {}
    listing = "\n".join(files[:200])
    prompt = (
        "Group these source files into a small number of logical subsystems "
        "based on their names and likely responsibilities. Respond with JSON "
        "only: an object mapping a short lowercase group name to an array of the "
        "exact file paths in that group. Every file must appear exactly once.\n\n"
        + listing)
    raw = await _llm_generate(prompt, system="You organise codebases. JSON only.")
    try:
        obj = json.loads(re.sub(r"^```(?:json)?|```$", "", (raw or "").strip()).strip())
        if isinstance(obj, dict):
            # keep only known files
            known = set(files)
            return {str(k): [f for f in v if f in known]
                    for k, v in obj.items() if isinstance(v, list)}
    except Exception:
        pass
    return {}


async def _read_source_file(roots: Dict[str, str], snapshot_id: str,
                            fname: str, max_chars: int = 0) -> str:
    """Read a source file. max_chars<=0 means NO truncation (read the whole
    file). The snapshot copy is preferred, falling back to live source."""
    fs_read = CAPABILITY_REGISTRY.get("ide.fs.read")
    if not fs_read:
        return ""
    cap = max_chars if max_chars and max_chars > 0 else 4_000_000
    candidates = []
    if roots.get("snapshot_root") and snapshot_id:
        candidates.append(f"{roots['snapshot_root']}/{snapshot_id}/{fname}")
    if roots.get("source_root"):
        candidates.append(f"{roots['source_root']}/{fname}")
    for path in candidates:
        try:
            r = await fs_read["func"](path=path, max_bytes=cap * 2) or {}
            c = r.get("content")
            if c:
                return c if max_chars <= 0 else c[:max_chars]
        except Exception:
            continue
    return ""


def _number_lines(content: str, start: int = 1) -> str:
    out = []
    for i, ln in enumerate(content.split("\n"), start):
        out.append(f"{i:>5}| {ln}")
    return "\n".join(out)


def _chunk_by_lines(content: str, budget_chars: int) -> List[Dict[str, Any]]:
    """Split into chunks of whole lines so each chunk's numbered text fits the
    budget. Returns [{start, end, text}] with TRUE file line numbers preserved."""
    lines = content.split("\n")
    chunks: List[Dict[str, Any]] = []
    cur: List[str] = []
    cur_start = 1
    cur_len = 0
    for idx, ln in enumerate(lines, 1):
        piece = f"{idx:>5}| {ln}\n"
        if cur and cur_len + len(piece) > budget_chars:
            chunks.append({"start": cur_start, "end": idx - 1,
                           "text": "".join(cur)})
            cur, cur_len, cur_start = [], 0, idx
        cur.append(piece)
        cur_len += len(piece)
    if cur:
        chunks.append({"start": cur_start, "end": len(lines), "text": "".join(cur)})
    return chunks


_CITATION_RULE = (
    "\n\nIMPORTANT: the source below is shown with line numbers in the form "
    "`  42| code`. Whenever you reference a specific location, cite it inline "
    "as [[L<line>]] for a single line or [[L<start>-<end>]] for a range, using "
    "those exact line numbers. Cite generously so every concrete point is "
    "anchored to its line(s).")


async def _store_review_report(report: Dict[str, Any]):
    """Persist one (file, style) review report into the reports hash."""
    r = _redis()
    if not r:
        return
    try:
        field = f"{report['file']}::{report['style']}"
        await r.hset(KEY_REVIEW_REPORTS, field, json.dumps(report, default=str))
    except Exception as e:
        log.debug("store review report: %s", e)


async def _deep_review_one(roots, snapshot_id, fname, style,
                           chunk_chars: int = 16000) -> Dict[str, Any]:
    """Review one file for one style WITHOUT truncating: large files are split
    into line-numbered chunks, each reviewed, then stitched into one report."""
    sdef = REVIEW_STYLES.get(style, REVIEW_STYLES["critique"])
    area = _source_area(fname)
    content = await _read_source_file(roots, snapshot_id, fname, 0)  # full file
    if not content:
        return {"file": fname, "style": style, "area": area, "ok": False,
                "error": "could not read source", "ts": now_iso()}

    chunks = _chunk_by_lines(content, chunk_chars)
    parts: List[str] = []
    cite_line = ("When you reference a location, cite it inline as [[L<line>]] or "
                 "[[L<start>-<end>]] using the line numbers shown above.")
    system = (sdef["system"] + " Perform the requested task as your deliverable — "
              "do NOT just describe or summarise what the code does, and do NOT "
              "open with a generic overview or restate that this is Python/JS code. "
              "Begin directly with the specific deliverable, tied to the lines shown.")
    for ci, ch in enumerate(chunks, 1):
        chunk_note = (f" (part {ci} of {len(chunks)}, lines {ch['start']}-{ch['end']} "
                      f"of the file — do the task for THIS range)"
                      if len(chunks) > 1 else "")
        # Source FIRST, task LAST: the model attends most to the final
        # instruction before generating, so the style task must come after the
        # code, otherwise a large source blob makes it default to summarising.
        prompt = (
            f"Below is the source of `{fname}` (subsystem: {area}){chunk_note}, "
            f"shown with line numbers:\n\n"
            f"{ch['text']}\n"
            f"--- END SOURCE ---\n\n"
            f"YOUR TASK — {sdef['label']}:\n{sdef['instruction']}\n\n"
            f"{cite_line}\n"
            f"Produce the {sdef['label']} deliverable now (not a generic summary "
            f"of what the code does):"
        )
        md = await _llm_generate(prompt, system=system)
        md = (md or "").strip()
        if md:
            if len(chunks) > 1:
                parts.append(f"\n\n### Lines {ch['start']}–{ch['end']}\n\n{md}")
            else:
                parts.append(md)

    full_md = "\n".join(parts).strip()
    report = {
        "file": fname, "style": style, "area": area,
        "label": sdef["label"], "snapshot_id": snapshot_id,
        "markdown": full_md, "chars": len(full_md),
        "chunks": len(chunks), "truncated": False,
        "ts": now_iso(), "ok": bool(full_md),
    }
    await _store_review_report(report)
    return report


async def _run_deep_review(styles: List[str], area: str, files: List[str],
                           max_files: int, max_chars: int,
                           journal_id: str = "", resume: bool = False,
                           review_type: str = "", baseline_snapshot: str = "",
                           max_runtime_s: float = 0, pause_on_activity: bool = False,
                           activity_idle_min: float = 0, auto_resume: bool = True,
                           resume_scope: str = "any") -> Dict[str, Any]:
    """Core engine: review the whole project (or a filtered subset) across
    multiple styles, storing a report per (file, style). Enumerates the snapshot
    tree RECURSIVELY (all subdirs). max_chars is the per-CHUNK budget — large
    files are split into line-numbered chunks rather than truncated.

    review_type: '' / 'all' = whole project; 'between' = only files that differ
    between baseline_snapshot and the current snapshot.
    resume: skip files already reviewed and START FROM THE FIRST FILE WITH NO
      OUTPUT. resume_scope='any' (default) skips a (file,style) if it has an ok
      report under ANY snapshot — so continuing after a new snapshot does NOT
      reset to file 0; 'snapshot' restricts to the current snapshot only.
    Interruptibility: the run pauses (stops sending LLM jobs) while manually
      paused; it stops early and (if auto_resume) reschedules a resume run when
      it hits max_files, max_runtime_s, or — when pause_on_activity — the user
      becomes active (idle < activity_idle_min), yielding to other dreams."""
    global _CYCLE_CANCEL
    styles = [s for s in styles if s in REVIEW_STYLES] or ["critique"]
    chunk_chars = max_chars if max_chars and max_chars > 0 else 16000
    roots = await _source_root_info()
    snap = await _resolve_review_snapshot(label="deep_review")
    snapshot_id = snap.get("snapshot_id") or ""

    enumerated = await _enumerate_source_files(snapshot_id, roots)
    names = [f["rel"] for f in enumerated]

    # between-snapshots targeting
    if review_type == "between" and baseline_snapshot:
        changed = await _diff_snapshots(baseline_snapshot, snapshot_id)
        changed_set = set(changed.get("modified", []) + changed.get("added", []))
        names = [n for n in names if n in changed_set]

    if files:
        names = [n for n in names if n in set(files)]
    if area and area not in ("", "all"):
        names = [n for n in names if _source_area(n) == area]

    # Build the set of already-reviewed (file, style) pairs. resume_scope='any'
    # matches across snapshots so continuation starts at the first file lacking
    # output rather than restarting from zero on a fresh snapshot.
    existing: set = set()
    if resume:
        rr = _redis()
        if rr:
            try:
                h = await rr.hgetall(KEY_REVIEW_REPORTS)
                for k, v in (h or {}).items():
                    try:
                        rep = json.loads(v.decode() if isinstance(v, bytes) else v)
                        if not rep.get("ok"):
                            continue
                        if resume_scope == "snapshot" and rep.get("snapshot_id") != snapshot_id:
                            continue
                        existing.add((rep.get("file"), rep.get("style")))
                    except Exception:
                        continue
            except Exception:
                pass

    # Worklist preserves enumeration order so we resume at the first file with
    # no output. Cap to max_files AFTER skipping done pairs (budget per run).
    worklist = [(f, s) for f in names for s in styles if (f, s) not in existing]
    full_remaining = len(worklist)
    if max_files and max_files > 0:
        # max_files caps FILES this run, not pairs — group by file order
        capped, seen_files = [], []
        for f, s in worklist:
            if f not in seen_files:
                if len(seen_files) >= max_files:
                    break
                seen_files.append(f)
            capped.append((f, s))
        worklist = capped

    jid = journal_id or f"review:{snapshot_id or 'live'}"
    total = max(1, len(worklist))

    async def _status(**kw):
        r = _redis()
        if not r:
            return
        try:
            cur = {"running": True, "snapshot_id": snapshot_id, "styles": styles,
                   "total": total, "review_type": review_type or "all",
                   "ts": now_iso(), **kw}
            await r.set(KEY_REVIEW_STATUS, json.dumps(cur, default=str))
        except Exception:
            pass

    # All files already reviewed? Switch systems: review only files CHANGED
    # since the baseline snapshot instead of declaring "nothing to do". This is
    # the "different system once everything's analysed" behaviour.
    if resume and not worklist and existing and names and review_type != "between":
        base = snap.get("baseline_id") or snap.get("prev") or baseline_snapshot
        if base and base != snapshot_id:
            try:
                changed = await _diff_snapshots(base, snapshot_id)
                changed_set = set(changed.get("modified", []) + changed.get("added", []))
                cnames = [n for n in names if n in changed_set]
                if cnames:
                    worklist = [(f, s) for f in cnames for s in styles]
                    total = max(1, len(worklist))
                    review_type = "between"
                    await _journal_append(jid,
                        f"All files reviewed — switching to changed-files mode: "
                        f"{len(cnames)} file(s) changed since {base}.",
                        kind="review", stage="deep_review",
                        title="Switch to changed-files")
            except Exception as e:
                log.debug("all-done changed-files fallback: %s", e)

    # Nothing to do — say why instead of returning silently with no output.
    if not worklist:
        if not names:
            reason = ("no source files enumerated (snapshot empty or "
                      "ide.code.list_files unavailable)")
        elif review_type == "between":
            reason = "no files changed between the selected snapshots"
        elif existing:
            reason = (f"all {len(existing)} file/style report(s) already exist for "
                      f"this snapshot (turn off resume to regenerate)")
        else:
            reason = "no matching files for the selected scope"
        summary = {"snapshot_id": snapshot_id, "styles": styles,
                   "review_type": review_type or "all", "files_reviewed": 0,
                   "reports_generated": 0, "by_area": {}, "resumed": len(existing),
                   "empty": True, "reason": reason, "ts": now_iso()}
        r = _redis()
        if r:
            try:
                await r.set(KEY_REVIEW_STATUS, json.dumps(
                    {"running": False, "done": 0, "total": 0, "generated": 0,
                     "snapshot_id": snapshot_id, "reason": reason,
                     "ts": now_iso()}, default=str))
            except Exception:
                pass
        await _journal_append(jid, f"Deep review: nothing to do — {reason}.",
            kind="review", stage="deep_review", title="Nothing to review")
        await emit_event({"type": "dream.review.run.done", **summary})
        return summary

    await _status(done=0, current="", phase="starting")
    await _journal_append(jid,
        f"Deep review starting: {len(worklist)} report(s) "
        f"({review_type or 'whole project'}"
        + (f", resume: skipped {len(existing)} done" if resume else "")
        + f"). Snapshot {snapshot_id}.",
        kind="review", stage="deep_review", title="Deep review started")
    await emit_event({"type": "dream.review.run.start", "files": len(names),
                      "styles": styles, "snapshot_id": snapshot_id, "total": total,
                      "review_type": review_type or "all", "resumed": len(existing)})

    generated = 0
    by_area: Dict[str, int] = {}
    done = 0
    last_file = None
    _t_start = time.monotonic()
    stopped_reason = ""
    rr = _redis()

    async def _is_paused() -> bool:
        if not rr:
            return False
        try:
            v = await rr.get(KEY_REVIEW_PAUSE)
            return (v.decode() if isinstance(v, bytes) else v) == "1"
        except Exception:
            return False

    for fname, style in worklist:
        if _CYCLE_CANCEL:
            stopped_reason = "cancelled"
            break

        # Manual pause: stop sending LLM jobs and wait until resumed/cancelled.
        while await _is_paused() and not _CYCLE_CANCEL:
            await _status(done=done, current="(paused)", phase="paused",
                          generated=generated, by_area=by_area)
            await asyncio.sleep(3)
        if _CYCLE_CANCEL:
            stopped_reason = "cancelled"
            break

        # Yield to user / other dreams: stop early (and resume later) when the
        # user is active or a time/file budget is reached.
        if pause_on_activity and activity_idle_min:
            try:
                if (await _idle_minutes()) < activity_idle_min:
                    stopped_reason = "user active — yielding"
                    break
            except Exception:
                pass
        if max_runtime_s and (time.monotonic() - _t_start) >= max_runtime_s:
            stopped_reason = f"runtime budget {int(max_runtime_s)}s reached"
            break

        ar = _source_area(fname)
        try:
            rep = await _deep_review_one(roots, snapshot_id, fname, style, chunk_chars)
        except Exception as e:
            log.warning("deep review %s [%s]: %s", fname, style, e)
            rep = {"file": fname, "style": style, "ok": False, "error": str(e)}
        done += 1
        if rep.get("ok"):
            generated += 1
            by_area[ar] = by_area.get(ar, 0) + 1
        await _status(done=done, current=f"{fname} · {style}", phase="reviewing",
                      generated=generated, by_area=by_area, remaining=full_remaining - done)
        await emit_event({"type": "dream.review.progress",
                          "file": fname, "style": style, "area": ar,
                          "done": done, "total": total,
                          "chunks": rep.get("chunks", 1), "chars": rep.get("chars", 0)})
        if fname != last_file:
            await _journal_append(jid, f"Reviewing {fname} ({ar}).",
                kind="review", stage="deep_review", title=f"Reviewing {fname}")
            last_file = fname

    # If we stopped early with work still outstanding, optionally reschedule a
    # resume run so the review completes across multiple slots (giving way to
    # other dreams in between). Manual cancel never auto-resumes.
    incomplete = bool(stopped_reason) and stopped_reason != "cancelled"
    remaining_after = (full_remaining - done) if full_remaining else 0
    if incomplete and auto_resume and remaining_after > 0:
        async def _resume_later():
            await asyncio.sleep(60)
            try:
                await _run_deep_review(styles, area, files, max_files, chunk_chars,
                                       journal_id=jid, resume=True,
                                       review_type="", baseline_snapshot=baseline_snapshot,
                                       max_runtime_s=max_runtime_s,
                                       pause_on_activity=pause_on_activity,
                                       activity_idle_min=activity_idle_min,
                                       auto_resume=auto_resume, resume_scope=resume_scope)
            except Exception as e:
                log.debug("review auto-resume: %s", e)
        asyncio.create_task(_resume_later())
        await _journal_append(jid,
            f"Paused after {done} (—{stopped_reason}); {remaining_after} pair(s) "
            f"remaining, will resume.", kind="review", stage="deep_review",
            title="Yielded — will resume")

    reviewed_files = sorted({f for f, _ in worklist})
    summary = {
        "snapshot_id": snapshot_id, "styles": styles,
        "review_type": review_type or "all",
        "files_reviewed": len(reviewed_files),
        "reports_generated": generated, "by_area": by_area,
        "resumed": len(existing), "ts": now_iso(),
        "incomplete": incomplete, "stopped_reason": stopped_reason,
        "remaining": remaining_after,
    }
    r = _redis()
    if r:
        try:
            await r.set(KEY_REVIEW_RUN, json.dumps(summary, default=str))
            await r.rpush(KEY_REVIEW_RUNLOG, json.dumps(summary, default=str))
            await r.ltrim(KEY_REVIEW_RUNLOG, -100, -1)
            await r.set(KEY_REVIEW_STATUS, json.dumps(
                {"running": False, "done": done, "total": total,
                 "generated": generated, "snapshot_id": snapshot_id,
                 "by_area": by_area, "incomplete": incomplete,
                 "reason": stopped_reason, "remaining": remaining_after,
                 "ts": now_iso()}, default=str))
        except Exception:
            pass
    await _journal_append(jid,
        (f"Deep review yielded ({stopped_reason}): {generated} report(s) this slot, "
         f"{remaining_after} remaining."
         if incomplete else
         f"Deep review complete: {generated} report(s) across {len(by_area)} area(s)."),
        kind="review", stage="deep_review",
        title=("Deep review yielded" if incomplete else "Deep review complete"))
    await emit_event({"type": "dream.review.run.done", **summary})
    return summary


# ── Capabilities ─────────────────────────────────────────────────────────────

@capability(
    "dream.review.styles", memory="off", silent=True,
    http_method="GET", http_path="/dream/review/styles", http_tags=["dream", "review"],
    description="List the available deep-review styles (docs, critique, "
                "improvement, integration, architecture).",
)
async def dream_review_styles(trace_id=None):
    return {"styles": [{"id": k, "label": v["label"]} for k, v in REVIEW_STYLES.items()]}


@capability(
    "dream.review.run", memory="off",
    http_method="POST", http_path="/dream/review/run", http_tags=["dream", "review"],
    description="Run a deep, whole-project source review (RECURSIVE — all subdirs; "
                "large files are CHUNKED, never truncated). Input: styles "
                "(comma/JSON list, default all), area (str, '' = whole project), "
                "files (JSON list, optional subset), max_files (int, 0 = all), "
                "max_chars (per-CHUNK budget, default 16000), resume (bool — skip "
                "file/style pairs already reported for this snapshot, to complete a "
                "review across runs), review_type ('all' | 'between'), "
                "baseline_snapshot (for review_type='between'), background (bool, "
                "default true). Poll dream.review.status. For dashboard integration "
                "prefer the 'source_review_deep' trigger via dream.cycle.run.",
)
async def dream_review_run(
    styles: Optional[Any] = None,
    area: str = "",
    files: Optional[Any] = None,
    max_files: int = 0,
    max_chars: int = 16000,
    resume: bool = False,
    review_type: str = "",
    baseline_snapshot: str = "",
    max_runtime_s: float = 0,
    pause_on_activity: bool = False,
    activity_idle_min: float = 5,
    auto_resume: bool = True,
    background: bool = True,
    trace_id=None,
):
    def _list(v):
        if v is None:
            return []
        if isinstance(v, str):
            try:
                j = json.loads(v)
                if isinstance(j, list):
                    return j
            except Exception:
                pass
            return [s.strip() for s in v.split(",") if s.strip()]
        return list(v)
    style_list = _list(styles) or list(REVIEW_STYLES.keys())
    file_list = _list(files)
    kw = dict(journal_id="", resume=bool(resume),
              review_type=(review_type or "").lower(), baseline_snapshot=baseline_snapshot,
              max_runtime_s=float(max_runtime_s or 0),
              pause_on_activity=bool(pause_on_activity),
              activity_idle_min=float(activity_idle_min or 0),
              auto_resume=bool(auto_resume))
    if background:
        asyncio.create_task(_run_deep_review(
            style_list, area, file_list, int(max_files or 0), int(max_chars or 16000), **kw))
        return {"ok": True, "started": True, "background": True,
                "styles": style_list, "area": area or "(whole project)",
                "resume": bool(resume), "review_type": review_type or "all",
                "note": "review running in background; poll dream.review.status"}
    return await _run_deep_review(style_list, area, file_list,
                                  int(max_files or 0), int(max_chars or 16000), **kw)


@capability(
    "dream.review.snapshots", memory="off", silent=True,
    http_method="GET", http_path="/dream/review/snapshots", http_tags=["dream", "review"],
    description="List available source snapshots (id, created, label, file_count, "
                "is_fresh) for picking a baseline in between-snapshots reviews.",
)
async def dream_review_snapshots(trace_id=None):
    cap = CAPABILITY_REGISTRY.get("ide.inspect.list_snapshots")
    if not cap:
        return {"snapshots": []}
    try:
        res = await cap["func"]() or {}
        return {"snapshots": res.get("snapshots", []),
                "current_source_hash": res.get("current_source_hash", "")}
    except Exception as e:
        return {"snapshots": [], "error": str(e)}


@capability(
    "dream.review.status", memory="off", silent=True,
    http_method="GET", http_path="/dream/review/status", http_tags=["dream", "review"],
    description="Live progress of the current/last deep review run "
                "(running, done, total, current file, generated, by_area).",
)
async def dream_review_status(trace_id=None):
    r = _redis()
    if not r:
        return {"running": False}
    try:
        raw = await r.get(KEY_REVIEW_STATUS)
        if raw:
            return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        pass
    return {"running": False}


@capability(
    "dream.review.pause", memory="off",
    http_method="POST", http_path="/dream/review/pause", http_tags=["dream", "review"],
    description="Pause the deep source review — stops sending LLM jobs after the "
                "current file. Progress is preserved; resume continues from the "
                "first file with no output.",
)
async def dream_review_pause(trace_id=None):
    r = _redis()
    if r:
        try:
            await r.set(KEY_REVIEW_PAUSE, "1")
        except Exception as e:
            return {"ok": False, "error": str(e)}
    await emit_event({"type": "dream.review.paused"})
    return {"ok": True, "paused": True}


@capability(
    "dream.review.resume", memory="off",
    http_method="POST", http_path="/dream/review/resume", http_tags=["dream", "review"],
    description="Resume a paused deep source review (clears the pause flag). If no "
                "run is in-flight, start one with resume=true to continue from the "
                "first un-reviewed file.",
)
async def dream_review_resume(start: bool = True, trace_id=None):
    r = _redis()
    running = False
    if r:
        try:
            await r.delete(KEY_REVIEW_PAUSE)
            raw = await r.get(KEY_REVIEW_STATUS)
            if raw:
                st = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                running = bool(st.get("running"))
        except Exception:
            pass
    await emit_event({"type": "dream.review.resumed"})
    if start and not running:
        asyncio.create_task(_run_deep_review(
            list(REVIEW_STYLES.keys()), "", [], 0, 16000,
            resume=True, pause_on_activity=True, activity_idle_min=5,
            max_runtime_s=0, auto_resume=True))
        return {"ok": True, "resumed": True, "started": True}
    return {"ok": True, "resumed": True, "running": running}


@capability(
    "dream.review.runs", memory="off", silent=True,
    http_method="GET", http_path="/dream/review/runs", http_tags=["dream", "review"],
    description="History of past deep-review runs (most recent first): snapshot, "
                "styles, files reviewed, reports generated, by_area, ts.",
)
async def dream_review_runs(limit: int = 50, trace_id=None):
    r = _redis()
    if not r:
        return {"runs": []}
    try:
        raw = await r.lrange(KEY_REVIEW_RUNLOG, -int(limit or 50), -1)
    except Exception:
        return {"runs": []}
    runs = []
    for item in raw or []:
        try:
            runs.append(json.loads(item.decode() if isinstance(item, bytes) else item))
        except Exception:
            continue
    runs.reverse()
    return {"runs": runs, "count": len(runs)}


@capability(
    "dream.review.source", memory="off", silent=True,
    http_method="GET", http_path="/dream/review/source", http_tags=["dream", "review"],
    description="Return a source file's content (from the snapshot) plus lint "
                "annotations for the split source+report view. Input: file (str!), "
                "snapshot_id (str, optional — defaults to most recent). Output: "
                "{ok, file, content, lines, annotations:[{line, severity, msg, source}]}.",
)
async def dream_review_source(file: str, snapshot_id: str = "", trace_id=None):
    roots = await _source_root_info()
    if not snapshot_id:
        snap = await _resolve_review_snapshot(label="view")
        snapshot_id = snap.get("snapshot_id") or ""
    content = await _read_source_file(roots, snapshot_id, file, 200000)
    if not content:
        return {"ok": False, "error": "could not read source", "file": file}

    annotations: List[Dict[str, Any]] = []
    # 1) Real linting via ruff/pyflakes (best-effort) on the snapshot copy
    abs_path = ""
    if roots.get("snapshot_root") and snapshot_id:
        abs_path = f"{roots['snapshot_root']}/{snapshot_id}/{file}"
    elif roots.get("source_root"):
        abs_path = f"{roots['source_root']}/{file}"
    bash = CAPABILITY_REGISTRY.get("exec.bash.run")
    if bash and abs_path and file.endswith(".py"):
        for tool, cmd in (("ruff", f"ruff check --output-format=concise '{abs_path}'"),
                          ("pyflakes", f"python -m pyflakes '{abs_path}'")):
            try:
                res = await bash["func"](command=cmd + " 2>&1", timeout=20) or {}
                out = res.get("stdout") or res.get("output") or ""
                if not out.strip():
                    continue
                for ln in out.splitlines():
                    m = re.search(r":(\d+):(?:\d+:)?\s*(.*)$", ln)
                    if m:
                        msg = m.group(2).strip()
                        sev = ("high" if re.search(r"\b(E\d|F\d|undefined|error)\b", msg, re.I)
                               else "medium")
                        annotations.append({"line": int(m.group(1)), "severity": sev,
                                            "msg": msg[:200], "source": tool})
                if annotations:
                    break
            except Exception:
                continue
    # 2) Merge in any stored critique findings that carry line numbers
    rr = _redis()
    if rr:
        try:
            raw = await rr.hget(KEY_REVIEW_REPORTS, f"{file}::critique")
            if raw:
                rep = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                for m in re.finditer(r"L(\d+)", rep.get("markdown", "")):
                    annotations.append({"line": int(m.group(1)), "severity": "info",
                                        "msg": "referenced in critique", "source": "critique"})
        except Exception:
            pass

    return {"ok": True, "file": file, "snapshot_id": snapshot_id,
            "content": content, "lines": content.count("\n") + 1,
            "annotations": annotations}


@capability(
    "dream.review.areas", memory="off", silent=True,
    http_method="GET", http_path="/dream/review/areas", http_tags=["dream", "review"],
    description="List subsystem areas with module counts and how many reports "
                "exist per area/style. Output: {areas: [{area, modules, reports, "
                "styles:{style:count}}], total_reports}.",
)
async def dream_review_areas(trace_id=None):
    snap = await _resolve_review_snapshot(label="areas")
    files = await _enumerate_source_files(snap.get("snapshot_id") or "")
    area_mods: Dict[str, int] = {}
    for f in files:
        a = _source_area(f["rel"])
        area_mods[a] = area_mods.get(a, 0) + 1

    r = _redis()
    rep_by_area: Dict[str, Dict[str, Any]] = {}
    total = 0
    if r:
        try:
            h = await r.hgetall(KEY_REVIEW_REPORTS)
            for _, v in (h or {}).items():
                try:
                    rep = json.loads(v.decode() if isinstance(v, bytes) else v)
                except Exception:
                    continue
                total += 1
                a = rep.get("area", "core")
                bucket = rep_by_area.setdefault(a, {"reports": 0, "styles": {}})
                bucket["reports"] += 1
                st = rep.get("style", "?")
                bucket["styles"][st] = bucket["styles"].get(st, 0) + 1
        except Exception:
            pass

    areas = []
    for a in sorted(set(list(area_mods.keys()) + list(rep_by_area.keys()))):
        b = rep_by_area.get(a, {"reports": 0, "styles": {}})
        areas.append({"area": a, "modules": area_mods.get(a, 0),
                      "reports": b["reports"], "styles": b["styles"]})
    return {"areas": areas, "total_reports": total}


@capability(
    "dream.review.list", memory="off", silent=True,
    http_method="GET", http_path="/dream/review/list", http_tags=["dream", "review"],
    description="List stored review reports (metadata only). Input: area (str), "
                "style (str), both optional filters. Output: {reports: [{file, "
                "style, area, label, chars, ts}]}.",
)
async def dream_review_list(area: str = "", style: str = "", trace_id=None):
    r = _redis()
    out: List[Dict[str, Any]] = []
    if r:
        try:
            h = await r.hgetall(KEY_REVIEW_REPORTS)
            for _, v in (h or {}).items():
                try:
                    rep = json.loads(v.decode() if isinstance(v, bytes) else v)
                except Exception:
                    continue
                if area and rep.get("area") != area:
                    continue
                if style and rep.get("style") != style:
                    continue
                out.append({k: rep.get(k) for k in
                            ("file", "style", "area", "label", "chars", "ts",
                             "snapshot_id", "truncated")})
        except Exception:
            pass
    out.sort(key=lambda x: (x.get("area", ""), x.get("file", ""), x.get("style", "")))
    return {"reports": out, "count": len(out)}


@capability(
    "dream.review.get", memory="off", silent=True,
    http_method="GET", http_path="/dream/review/get", http_tags=["dream", "review"],
    description="Get one stored review report (full markdown). Input: file (str!), "
                "style (str!). Output: {ok, report}.",
)
async def dream_review_get(file: str, style: str, trace_id=None):
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}
    try:
        raw = await r.hget(KEY_REVIEW_REPORTS, f"{file}::{style}")
        if raw:
            return {"ok": True, "report": json.loads(
                raw.decode() if isinstance(raw, bytes) else raw)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "not found"}


@capability(
    "dream.review.area_report", memory="off", silent=True,
    http_method="GET", http_path="/dream/review/area_report", http_tags=["dream", "review"],
    description="Aggregate all reports for one area (optionally one style) into a "
                "single long Markdown document with a table of contents. Input: "
                "area (str!), style (str, optional). Output: {ok, area, markdown}.",
)
async def dream_review_area_report(area: str, style: str = "", trace_id=None):
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}
    reps: List[Dict[str, Any]] = []
    try:
        h = await r.hgetall(KEY_REVIEW_REPORTS)
        for _, v in (h or {}).items():
            try:
                rep = json.loads(v.decode() if isinstance(v, bytes) else v)
            except Exception:
                continue
            if rep.get("area") != area:
                continue
            if style and rep.get("style") != style:
                continue
            reps.append(rep)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    reps.sort(key=lambda x: (x.get("file", ""), x.get("style", "")))
    lines = [f"# {area.title()} — Source Review", ""]
    if reps:
        lines.append("## Contents")
        for rep in reps:
            lines.append(f"- {rep.get('file')} — _{rep.get('label', rep.get('style'))}_")
        lines.append("")
        for rep in reps:
            lines.append(f"\n---\n\n## `{rep.get('file')}` — {rep.get('label', rep.get('style'))}\n")
            lines.append(rep.get("markdown", "").strip())
    else:
        lines.append("_No reports yet for this area. Run a deep review._")
    return {"ok": True, "area": area, "report_count": len(reps),
            "markdown": "\n".join(lines)}


@capability(
    "dream.review.clear", memory="off",
    http_method="POST", http_path="/dream/review/clear", http_tags=["dream", "review"],
    description="Clear stored review reports. Input: area (str, optional — clears "
                "only that area), confirm (bool!). Output: {ok, cleared}.",
)
async def dream_review_clear(area: str = "", confirm: bool = False, trace_id=None):
    if not confirm:
        return {"ok": False, "error": "pass confirm=true"}
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}
    try:
        if not area:
            await r.delete(KEY_REVIEW_REPORTS)
            return {"ok": True, "cleared": "all"}
        h = await r.hgetall(KEY_REVIEW_REPORTS)
        removed = 0
        for k, v in (h or {}).items():
            try:
                rep = json.loads(v.decode() if isinstance(v, bytes) else v)
            except Exception:
                continue
            if rep.get("area") == area:
                await r.hdel(KEY_REVIEW_REPORTS, k)
                removed += 1
        return {"ok": True, "cleared": area, "removed": removed}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@capability(
    "dream.stage.deep_review", memory="off", silent=True,
    description="Dream pipeline stage: run the deep whole-project review engine. "
                "Configure via stage_config.deep_review = {styles:[...], area, "
                "max_files, max_chars}. Writes state['deep_review'] summary and a "
                "report index into state['report'].",
)
async def dream_stage_deep_review(state: Optional[Dict[str, Any]] = None, trace_id=None):
    state = state or {}
    trig = state.get("trigger", {})
    cycle_id = state.get("cycle_id", "?")
    journal_id = state.get("journal_id") or cycle_id
    cfg = (trig.get("stage_config", {}) or {}).get("deep_review", {}) or {}
    seed = state.get("seed") or {}

    styles = cfg.get("styles") or seed.get("review_styles") or list(REVIEW_STYLES.keys())
    area = cfg.get("area") or seed.get("review_area") or ""
    max_files = int(seed.get("review_max_files", cfg.get("max_files", 0)) or 0)
    max_chars = int(seed.get("review_max_chars", cfg.get("max_chars", 16000)) or 16000)
    # Resume defaults ON for the cycle path so re-runs continue from the first
    # un-reviewed file rather than restarting.
    resume = bool(seed.get("review_resume", cfg.get("resume", True)))
    review_type = (seed.get("review_mode") or cfg.get("review_type") or "").lower()
    baseline = seed.get("review_baseline") or cfg.get("baseline_snapshot") or ""
    # Interruptibility / budget — yields to the user and other dreams.
    max_runtime_s = float(seed.get("review_max_runtime_s", cfg.get("max_runtime_s", 0)) or 0)
    pause_on_activity = bool(cfg.get("pause_on_activity",
                                     seed.get("review_pause_on_activity", True)))
    activity_idle_min = float(cfg.get("activity_idle_min",
                                      trig.get("min_idle_minutes", 5)) or 5)

    summary = await _run_deep_review(list(styles), area, [], max_files,
                                     max_chars, journal_id=journal_id,
                                     resume=resume, review_type=review_type,
                                     baseline_snapshot=baseline,
                                     max_runtime_s=max_runtime_s,
                                     pause_on_activity=pause_on_activity,
                                     activity_idle_min=activity_idle_min,
                                     auto_resume=True)
    state["deep_review"] = summary
    state["title"] = (f"Deep Source Review — {summary.get('reports_generated', 0)} "
                      f"reports across {len(summary.get('by_area', {}))} areas")
    idx = ["# Deep Source Review", "",
           f"Snapshot `{summary.get('snapshot_id')}` · "
           f"{summary.get('reports_generated', 0)} reports · "
           f"styles: {', '.join(summary.get('styles', []))}", "", "## Coverage by area"]
    for a, n in sorted((summary.get("by_area") or {}).items()):
        idx.append(f"- **{a}** — {n} reports")
    idx.append("\nBrowse full reports in the Source Review panel.")
    state["report"] = "\n".join(idx)
    return state


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES — CONVERSATIONAL CHAT WITH DREAMS
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.chat", memory="off",
    http_method="POST", http_path="/dream/chat", http_tags=["dream"],
    description="Have a follow-up conversation about a specific dream cycle's output. "
                "Loads the cycle's report, themes, and gather data as context. "
                "Inputs: cycle_id (str!), message (str!), history (JSON list of "
                "[{role,content}], optional — prior turns).",
)
async def dream_chat(cycle_id: str, message: str = "",
                     history: Optional[Any] = None, trace_id=None):
    if not cycle_id or not message:
        return {"error": "cycle_id and message required"}
    r = _redis()
    if not r:
        return {"error": "redis unavailable"}
    # Find the cycle in history
    rec: Optional[Dict[str, Any]] = None
    try:
        items = await r.lrange(KEY_HISTORY, -200, -1)
        for raw in (items or []):
            try:
                h = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                if h.get("cycle_id") == cycle_id:
                    rec = h
                    break
            except Exception:
                continue
    except Exception as e:
        return {"error": f"history read failed: {e}"}
    if not rec:
        return {"error": f"cycle {cycle_id} not found in recent history"}

    # Parse history if string
    if isinstance(history, str):
        try:
            history = json.loads(history)
        except Exception:
            history = []
    if not isinstance(history, list):
        history = []

    context = (
        f"Dream cycle '{rec.get('label', rec.get('trigger'))}' "
        f"({(rec.get('ended_at') or '')[:16]}):\n\n"
        f"## Report\n{rec.get('report', '')}\n\n"
    )
    if rec.get("themes"):
        context += f"## Themes\n{', '.join(rec['themes'][:15])}\n\n"
    if rec.get("trigger_prompt"):
        context += f"## Original prompt\n{rec.get('trigger_prompt')}\n\n"

    # Build prompt with history
    history_text = ""
    for turn in (history or [])[-8:]:
        if isinstance(turn, dict):
            role = turn.get("role", "user")
            content = str(turn.get("content", ""))[:1500]
            history_text += f"\n[{role}] {content}"

    prompt = (
        f"You are Vera. The user is asking a follow-up question about a dream cycle "
        f"output. Answer using ONLY the cycle context provided below — don't invent "
        f"new facts. If the cycle didn't cover something, say so.\n\n"
        f"{context}"
        f"{history_text}\n\n"
        f"[user] {message}"
    )
    reply = await _llm_generate(
        prompt,
        system="You answer follow-up questions about dream cycles, grounded in their actual output.",
    )
    return {
        "ok":         True,
        "cycle_id":   cycle_id,
        "reply":      reply,
        "trigger":    rec.get("trigger"),
        "title":      rec.get("title"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES — WHITELIST + CONFIG + HISTORY
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.whitelist.list", memory="off", silent=True,
    http_method="GET", http_path="/dream/whitelist", http_tags=["dream"],
    description="List capabilities the dream planner is allowed to use.",
)
async def dream_whitelist_list(trace_id=None):
    whitelist = await _get_whitelist()
    all_caps = sorted(CAPABILITY_REGISTRY.keys())
    missing = [c for c in whitelist if c not in CAPABILITY_REGISTRY]
    return {
        "whitelist": whitelist,
        "count":     len(whitelist),
        "missing":   missing,
        "available": all_caps,
    }


@capability(
    "dream.whitelist.set", memory="off",
    http_method="POST", http_path="/dream/whitelist/set", http_tags=["dream"],
    description="Replace the dream whitelist with a new list of capability names.",
)
async def dream_whitelist_set(caps: List[str], trace_id=None):
    if not isinstance(caps, list):
        return {"ok": False, "error": "caps must be a list"}
    await _set_whitelist([str(c) for c in caps])
    return {"ok": True, "count": len(caps)}


@capability(
    "dream.config.get", memory="off", silent=True,
    http_method="GET", http_path="/dream/config", http_tags=["dream"],
    description="Get global dream config.",
)
async def dream_config_get(trace_id=None):
    return {"config": await _get_config()}


@capability(
    "dream.config.set", memory="off",
    http_method="POST", http_path="/dream/config", http_tags=["dream"],
    description="Update global dream config. Pass only the fields you want to change.",
)
async def dream_config_set(
    enabled: Optional[bool] = None,
    min_idle_minutes: Optional[int] = None,
    tick_interval_seconds: Optional[int] = None,
    telegram_bridge: Optional[bool] = None,
    default_hitl_timeout_s: Optional[int] = None,
    llm_prefer_gpu: Optional[bool] = None,
    idle_reset_prefixes: Optional[List[str]] = None,
    trace_id=None,
):
    cfg = await _get_config()
    if enabled is not None:                cfg["enabled"] = bool(enabled)
    if min_idle_minutes is not None:       cfg["min_idle_minutes"] = int(min_idle_minutes)
    if tick_interval_seconds is not None:  cfg["tick_interval_seconds"] = max(10, int(tick_interval_seconds))
    if telegram_bridge is not None:        cfg["telegram_bridge"] = bool(telegram_bridge)
    if default_hitl_timeout_s is not None: cfg["default_hitl_timeout_s"] = max(30, int(default_hitl_timeout_s))
    if llm_prefer_gpu is not None:         cfg["llm_prefer_gpu"] = bool(llm_prefer_gpu)
    if idle_reset_prefixes is not None:    cfg["idle_reset_prefixes"] = [str(p).strip() for p in idle_reset_prefixes if str(p).strip()]
    await _save_config(cfg)
    return {"ok": True, "config": cfg}


@capability(
    "dream.history", memory="off", silent=True,
    http_method="GET", http_path="/dream/history", http_tags=["dream"],
    description="Recent dream cycle records (newest first). Supports filtering by "
                "trigger name, keyword search in report/title/themes, and pagination.",
)
async def dream_history(
    limit: int = 50,
    trigger: str = "",
    query: str = "",
    offset: int = 0,
    trace_id=None,
):
    # Fetch more than needed to allow client-side filter fallback
    fetch_limit = max(int(limit) + int(offset), 200)
    rows = await _get_history(limit=fetch_limit)

    # Filter
    filtered = rows
    if trigger:
        t = trigger.strip().lower()
        filtered = [r for r in filtered
                    if t in str(r.get("trigger", "")).lower()
                    or t in str(r.get("label", "")).lower()]
    if query:
        q = query.strip().lower()
        filtered = [r for r in filtered
                    if q in str(r.get("report", "")).lower()
                    or q in str(r.get("title", "")).lower()
                    or any(q in str(th).lower() for th in (r.get("themes") or []))]

    total = len(filtered)
    page = filtered[int(offset):int(offset) + int(limit)]
    return {"history": page, "count": len(page), "total": total,
            "offset": int(offset), "has_more": int(offset) + int(limit) < total}


@capability(
    "dream.last", memory="off", silent=True,
    http_method="GET", http_path="/dream/last", http_tags=["dream"],
    description="Most recent dream cycle record.",
)
async def dream_last(trace_id=None):
    rows = await _get_history(limit=1)
    if not rows:
        return {}
    return rows[0]


@capability(
    "dream.cycle.detail", memory="off", silent=True,
    http_method="GET", http_path="/dream/cycle/detail", http_tags=["dream"],
    description="Full execution trace of a dream cycle — sensor inputs, goal "
                "refinement, every tool call with inputs/outputs, LLM reasoning, "
                "findings, and the final report. Stored for 7 days. "
                "Inputs: cycle_id (str!).",
)
async def dream_cycle_detail(cycle_id: str = "", trace_id=None):
    if not cycle_id:
        return {"error": "cycle_id required"}
    r = _redis()
    if not r:
        return {"error": "redis unavailable"}
    try:
        detail_key = f"vera:dream:detail:{cycle_id}"
        raw = await r.get(detail_key)
        if not raw:
            return {"error": f"detail not found for cycle {cycle_id} (may have expired — 7 day TTL)"}
        detail = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        return {"detail": detail, "cycle_id": cycle_id}
    except Exception as e:
        return {"error": str(e)}


@capability(
    "dream.hitl.pending", memory="off", silent=True,
    http_method="GET", http_path="/dream/hitl/pending", http_tags=["dream"],
    description="Any pending human-in-the-loop approvals the dream system is waiting on.",
)
async def dream_hitl_pending(trace_id=None):
    r = _redis()
    if not r:
        return {"pending": []}
    try:
        items = await r.hgetall(KEY_HITL)
        out = []
        for k, v in (items or {}).items():
            try:
                rec = json.loads(v.decode() if isinstance(v, bytes) else v)
                rec["_key"] = k.decode() if isinstance(k, bytes) else str(k)
                out.append(rec)
            except Exception:
                continue
        return {"pending": out, "count": len(out)}
    except Exception:
        return {"pending": []}


@capability(
    "dream.hitl.respond", memory="off",
    http_method="POST", http_path="/dream/hitl/respond", http_tags=["dream"],
    description="Respond to a pending HITL approval from the panel/API. "
                "Inputs: cycle_id (str!), approve (bool!), edits (dict, optional — "
                "may contain dag and/or initial_state to override the planned DAG), "
                "text (str, optional — note saved alongside the response).",
)
async def dream_hitl_respond(
    cycle_id: str,
    approve: bool = False,
    edits: Optional[Dict[str, Any]] = None,
    text: str = "",
    trace_id=None,
):
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis not available"}
    if not cycle_id:
        return {"ok": False, "error": "cycle_id required"}
    # Coerce 'approve' — JSON 'true'/'false' strings sneak through some clients
    if isinstance(approve, str):
        approve = approve.lower() in ("true", "1", "yes", "y", "approve", "approved", "ok")
    payload = {
        "cycle_id": cycle_id,
        "approved": bool(approve),
        "edits":    edits if isinstance(edits, dict) else None,
        "text":     text or ("approve" if approve else "reject"),
        "ts":       now_iso(),
    }
    # Build candidate keys: the exact key the panel sent, the parent if step-suffixed,
    # and any pending step keys under this cycle_id
    candidate_keys = [cycle_id]
    if ":step" in cycle_id:
        parent = cycle_id.split(":step", 1)[0]
        candidate_keys.append(parent)
    try:
        items = await r.hgetall(KEY_HITL)
        for k in (items or {}).keys():
            kstr = k.decode() if isinstance(k, bytes) else str(k)
            if kstr.startswith(cycle_id + ":step") and kstr not in candidate_keys:
                candidate_keys.append(kstr)
    except Exception:
        pass
    written = []
    for key in candidate_keys:
        try:
            await r.hset(KEY_HITL_RESP, key, json.dumps(payload))
            try:
                await r.expire(KEY_HITL_RESP, 3600)
            except Exception:
                pass
            written.append(key)
        except Exception as e:
            log.debug("dream hitl_respond write %s: %s", key, e)
    # CRITICAL: also delete the pending entries immediately so the UI list updates
    # without waiting for the cycle's own cleanup step. The wait function will still
    # see the response on its next poll because we've written to KEY_HITL_RESP first.
    for key in candidate_keys:
        try:
            await r.hdel(KEY_HITL, key)
        except Exception:
            pass
    if not written:
        return {"ok": False, "error": "failed to write response"}
    await emit_event({
        "type":     "dream.hitl.ui_response",
        "cycle_id": cycle_id,
        "approved": bool(approve),
        "has_edits": bool(edits),
        "keys":     written,
    })
    return {"ok": True, "keys": written, **payload}


@capability(
    "dream.hitl.clear", memory="off",
    http_method="POST", http_path="/dream/hitl/clear", http_tags=["dream"],
    description="Clear stale HITL pending entries that aren't being actively waited on. "
                "Inputs: cycle_id (str — clear specific entry, or 'all' to clear all).",
)
async def dream_hitl_clear(cycle_id: str = "", trace_id=None):
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}
    cleared = 0
    try:
        if cycle_id == "all":
            items = await r.hgetall(KEY_HITL)
            for k in (items or {}).keys():
                try:
                    await r.hdel(KEY_HITL, k)
                    cleared += 1
                except Exception:
                    pass
            # Also clear orphan responses
            try:
                await r.delete(KEY_HITL_RESP)
            except Exception:
                pass
        elif cycle_id:
            # Clear exact + step variants + parent
            keys_to_clear = {cycle_id}
            if ":step" in cycle_id:
                keys_to_clear.add(cycle_id.split(":step", 1)[0])
            try:
                items = await r.hgetall(KEY_HITL)
                for k in (items or {}).keys():
                    kstr = k.decode() if isinstance(k, bytes) else str(k)
                    if kstr.startswith(cycle_id + ":step"):
                        keys_to_clear.add(kstr)
            except Exception:
                pass
            for k in keys_to_clear:
                try:
                    await r.hdel(KEY_HITL, k)
                    await r.hdel(KEY_HITL_RESP, k)
                    cleared += 1
                except Exception:
                    pass
        else:
            return {"ok": False, "error": "cycle_id required (or 'all')"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    await emit_event({"type": "dream.hitl.cleared", "cleared": cleared})
    return {"ok": True, "cleared": cleared}


# ─────────────────────────────────────────────────────────────────────────────
# STEPWISE EXECUTE STAGE
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.stage.stepwise_execute", memory="off", silent=True,
    description="Dream pipeline stage — agentic stepwise plan+execute. "
                "Delegates to dag.agent_loop_v2 (the DAG Workshop ReAct engine). "
                "The loop plans one tool call at a time, observes results, and "
                "iterates until the goal is satisfied or max_steps is reached. "
                "Tool-call history is surfaced as state['stepwise']['steps'] for "
                "the synthesize stage. Falls back to dag.agent_loop if v2 is absent.",
)
async def dream_stage_stepwise_execute(
    state: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    # ── Delegated to dag.agent_loop_v2 (DAG Workshop ReAct engine) ──────────
    # This stage used to contain its own LLM-driven step-by-step planning loop.
    # It now delegates entirely to the same engine used by the DAG Workshop panel
    # (/workshop/agent_loop/stream), eliminating duplicate loop logic.
    # The state contract is preserved — state["stepwise"] is populated with the
    # same structure the synthesize stage expects.

    state = state or {}
    trig = state.get("trigger", {})
    seed = state.get("seed") or {}
    themes = state.get("themes", [])
    gather = state.get("gather", {})
    cycle_id = state.get("cycle_id", "?")
    max_steps = int(trig.get("max_steps", 6) or 6)
    hitl_enabled = bool(trig.get("hitl", False))
    no_hitl_caps = set(trig.get("no_hitl_caps") or [])

    whitelist = trig.get("whitelist") or await _get_whitelist()
    _STEPWISE_EXCLUDE = {"dream.", "obs.", "health.", "ui.", "caps.", "mcp.", "echo"}
    whitelist = [
        c for c in whitelist
        if c in CAPABILITY_REGISTRY and not any(c.startswith(p) for p in _STEPWISE_EXCLUDE)
    ]
    if not whitelist:
        state["stepwise"] = {"error": "whitelist empty", "steps": []}
        return state

    # Build goal string grounded in sensor context
    # Phase 1: use refined_goal if goal_refine stage ran upstream
    refined_goal = state.get("refined_goal")
    if refined_goal:
        goal_parts = [refined_goal]
        bg = trig.get("prompt", "")
        if bg:
            goal_parts.append(f"BACKGROUND CONTEXT: {bg}")
        if themes:
            goal_parts.append(f"Themes: {', '.join(themes)}")
        goal = "\n\n".join(goal_parts)
    else:
        gather_summary_lines: List[str] = []
        for sname, sres in (gather.get("results", {}) or {}).items():
            if isinstance(sres, dict):
                cnt = sres.get("count", 0)
                sig = sres.get("signal", 0)
                gather_summary_lines.append(f"  {sname}: {cnt} items (signal {sig})")

        focus = (seed.get("focus_topic") or "").strip()
        project_ctx = (seed.get("project_context") or "").strip()
        goal_parts = [trig.get("prompt") or "Investigate and act on the most relevant signal."]
        if focus:
            goal_parts.append(f"FOCUS: {focus}")
        if project_ctx:
            goal_parts.append(f"Project context:\n{project_ctx[:2000]}")
        if themes:
            goal_parts.append(f"Themes: {', '.join(themes)}")
        if gather_summary_lines:
            goal_parts.append("Sensor activity:\n" + "\n".join(gather_summary_lines))
        goal = "\n\n".join(goal_parts)

    # Resolve dag.agent_loop_v2 (preferred) or dag.agent_loop (fallback)
    agent_loop_cap = (
        CAPABILITY_REGISTRY.get("dag.agent_loop_v2")
        or CAPABILITY_REGISTRY.get("dag.agent_loop")
    )
    engine_name = (
        "dag.agent_loop_v2" if "dag.agent_loop_v2" in CAPABILITY_REGISTRY
        else "dag.agent_loop" if "dag.agent_loop" in CAPABILITY_REGISTRY
        else None
    )

    await emit_event({
        "type":      "dream.stepwise.start",
        "cycle_id":  cycle_id,
        "max_steps": max_steps,
        "whitelist_count": len(whitelist),
        "engine": engine_name or "fallback",
    })

    if not agent_loop_cap:
        # Lightweight fallback when dag.agent_loop is not loaded yet
        log.warning("dream.stage.stepwise_execute: dag.agent_loop_v2 not registered; "
                    "falling back to sequential whitelist scan")
        steps: List[Dict[str, Any]] = []
        for step_i, cap_name in enumerate(whitelist[:max_steps]):
            if _CYCLE_CANCEL:
                steps.append({"step": step_i, "cancelled": True})
                break
            try:
                result = await _call_cap(cap_name)
                preview = str(result)[:300] if result else "(empty)"
                steps.append({"step": step_i, "cap": cap_name, "ok": True, "preview": preview})
                await emit_event({"type": "dream.stepwise.result", "cycle_id": cycle_id,
                                  "step": step_i, "cap": cap_name, "ok": True,
                                  "preview": preview[:200]})
            except Exception as e:
                steps.append({"step": step_i, "cap": cap_name, "ok": False, "error": str(e)})
                await emit_event({"type": "dream.stepwise.result", "cycle_id": cycle_id,
                                  "step": step_i, "cap": cap_name, "ok": False,
                                  "error": str(e)[:200]})

    else:
        # ── Delegate to dag.agent_loop_v2 ────────────────────────────────
        # HITL for the overall loop is handled at the dream cycle level; the
        # agent loop itself runs non-interactively but respects _CYCLE_CANCEL.
        # Per-step HITL for specific caps (hitl_enabled + not in no_hitl_caps)
        # is signalled to the panel via dream.hitl.requested events — the loop
        # will still run but approvals can be sent via dream.hitl.respond.
        allowed_caps_str = ",".join(whitelist)
        loop_session_id = f"dream:{cycle_id}:stepwise"
        cfg = await _get_config()

        settings = await _resolve_loop_settings(trig, state)
        settings.setdefault("prefer_gpu", bool(cfg.get("llm_prefer_gpu", True)))
        loop_kwargs = _loop_kwargs_for(
            agent_loop_cap["func"], settings,
            goal=goal,
            allowed_caps=allowed_caps_str,
            max_cycles=settings.get("max_cycles", max_steps),
            session_id=loop_session_id,
        )

        loop_result: Dict[str, Any] = {}
        try:
            loop_result = await agent_loop_cap["func"](**loop_kwargs) or {}
        except Exception as e:
            log.warning("dream.stage.stepwise_execute agent_loop error: %s", e)
            loop_result = {"error": str(e)}

        # Map agent loop tool-call history → steps list
        tool_calls: List[Dict[str, Any]] = loop_result.get("tool_calls") or []
        steps = []
        for i, tc in enumerate(tool_calls):
            cap_called = tc.get("tool") or tc.get("cap") or "?"
            result_preview = str(tc.get("result") or tc.get("output") or tc.get("preview") or "")[:300]
            thought = str(tc.get("thought") or tc.get("reason") or "")[:200]
            ok = not bool(tc.get("error"))
            step_rec: Dict[str, Any] = {
                "step":    i,
                "cap":     cap_called,
                "ok":      ok,
                "reason":  thought,
                "preview": result_preview,
            }
            if not ok:
                step_rec["error"] = str(tc.get("error", ""))[:200]
            steps.append(step_rec)
            await emit_event({
                "type":    "dream.stepwise.result",
                "cycle_id": cycle_id,
                "step":    i,
                "cap":     cap_called,
                "ok":      ok,
                "preview": result_preview[:200],
            })

        # Add loop summary as a final synthetic step so synthesize can see it
        summary_text = (loop_result.get("summary") or "").strip()
        if summary_text:
            steps.append({
                "step":    len(steps),
                "cap":     "__summary__",
                "ok":      True,
                "reason":  "agent_loop completed",
                "preview": summary_text[:600],
            })

        await emit_event({
            "type":     "dream.stepwise.complete",
            "cycle_id": cycle_id,
            "steps":    len(steps),
            "engine":   engine_name,
            "cycles":   loop_result.get("cycles", len(tool_calls)),
            "error":    loop_result.get("error"),
        })

    state["stepwise"] = {
        "steps": steps,
        "count": len(steps),
        "engine": engine_name or "fallback",
    }
    # stepwise replaces plan+execute — mark plan as handled
    if "plan" not in state:
        state["plan"] = {"skipped": True, "reason": "stepwise mode (dag.agent_loop_v2)"}
    return state


# ─────────────────────────────────────────────────────────────────────────────
# AGENT LOOP STAGE — named entry point for the DAG Workshop engine
# ─────────────────────────────────────────────────────────────────────────────
# dream.stage.agent_loop is a cleaner pipeline stage name that triggers the
# DAG Workshop's dag.agent_loop_v2 engine directly, without the stepwise_execute
# wrapper's fallback compatibility shim.  Use it in new pipelines; the older
# dream.stage.stepwise_execute and dream.stage.investigate remain as aliases.

@capability(
    "dream.stage.agent_loop", memory="off", silent=True,
    description="Run the DAG Workshop ReAct agent loop (dag.agent_loop_v2) as a "
                "dream pipeline stage. Builds a goal from sensor context, runs the "
                "loop, and stores tool-call results in state['agent_loop']. "
                "Drop-in replacement for dream.stage.stepwise_execute with no "
                "legacy compatibility overhead.",
)
async def dream_stage_agent_loop(
    state: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    """Thin pipeline wrapper around dag.agent_loop_v2."""
    state = state or {}
    trig = state.get("trigger", {})
    seed = state.get("seed") or {}
    themes = state.get("themes", [])
    gather = state.get("gather", {})
    cycle_id = state.get("cycle_id", "?")
    max_steps = int(trig.get("max_steps", 8) or 8)

    # Build whitelist
    whitelist = trig.get("whitelist") or await _get_whitelist()
    _EXCL = {"dream.", "obs.", "health.", "ui.", "caps.", "mcp.", "echo"}
    whitelist = [c for c in whitelist
                 if c in CAPABILITY_REGISTRY and not any(c.startswith(p) for p in _EXCL)]
    if not whitelist:
        state["agent_loop"] = {"error": "whitelist empty", "steps": []}
        return state

    # Build goal
    # Phase 1: use refined_goal if goal_refine stage ran upstream
    refined_goal = state.get("refined_goal")
    if refined_goal:
        goal_parts = [refined_goal]
        bg = trig.get("prompt", "")
        if bg:
            goal_parts.append(f"BACKGROUND CONTEXT: {bg}")
        if themes:
            goal_parts.append(f"Themes: {', '.join(themes)}")
        goal = "\n\n".join(goal_parts)
    else:
        gather_lines: List[str] = []
        for sname, sres in (gather.get("results", {}) or {}).items():
            if isinstance(sres, dict):
                cnt = sres.get("count", 0); sig = sres.get("signal", 0)
                gather_lines.append(f"  {sname}: {cnt} items (signal {sig})")

        focus = (seed.get("focus_topic") or "").strip()
        project_ctx = (seed.get("project_context") or "").strip()
        goal_parts = [trig.get("prompt") or "Use the available tools to investigate and act."]
        if focus:           goal_parts.append(f"FOCUS: {focus}")
        if project_ctx:     goal_parts.append(f"Project context:\n{project_ctx[:2000]}")
        if themes:          goal_parts.append(f"Themes: {', '.join(themes)}")
        if gather_lines:    goal_parts.append("Sensor data:\n" + "\n".join(gather_lines))
        goal = "\n\n".join(goal_parts)

    agent_loop_cap = (
        CAPABILITY_REGISTRY.get("dag.agent_loop_v2")
        or CAPABILITY_REGISTRY.get("dag.agent_loop")
    )
    if not agent_loop_cap:
        state["agent_loop"] = {"error": "dag.agent_loop_v2 not registered", "steps": []}
        return state

    engine = "dag.agent_loop_v2" if "dag.agent_loop_v2" in CAPABILITY_REGISTRY else "dag.agent_loop"
    cfg = await _get_config()
    settings = await _resolve_loop_settings(trig, state)
    settings.setdefault("prefer_gpu", bool(cfg.get("llm_prefer_gpu", True)))
    loop_kwargs = _loop_kwargs_for(
        agent_loop_cap["func"], settings,
        goal=goal,
        allowed_caps=",".join(whitelist),
        max_cycles=settings.get("max_cycles", max_steps),
        session_id=f"dream:{cycle_id}:agent_loop",
    )

    await emit_event({"type": "dream.agent_loop.start", "cycle_id": cycle_id,
                      "engine": engine, "max_steps": loop_kwargs.get("max_cycles", max_steps)})

    loop_result: Dict[str, Any] = {}
    try:
        loop_result = await agent_loop_cap["func"](**loop_kwargs) or {}
    except Exception as e:
        log.warning("dream.stage.agent_loop error: %s", e)
        loop_result = {"error": str(e)}

    # Map tool calls → steps
    tool_calls = loop_result.get("tool_calls") or []
    steps = []
    for i, tc in enumerate(tool_calls):
        cap_called = tc.get("tool") or tc.get("cap") or "?"
        preview = str(tc.get("result") or tc.get("output") or tc.get("preview") or "")[:300]
        thought = str(tc.get("thought") or tc.get("reason") or "")[:200]
        steps.append({"step": i, "cap": cap_called, "ok": not bool(tc.get("error")),
                      "reason": thought, "preview": preview})

    summary = (loop_result.get("summary") or "").strip()
    if summary:
        steps.append({"step": len(steps), "cap": "__summary__", "ok": True,
                      "reason": "loop complete", "preview": summary[:600]})
        # Also add to findings so synthesize can use it
        state.setdefault("findings", []).append({
            "topic": "agent_loop_summary", "content": summary[:2000],
            "source": engine, "iter": 0,
        })

    state["agent_loop"] = {
        "steps": steps, "count": len(steps), "engine": engine,
        "cycles": loop_result.get("cycles", len(tool_calls)),
        "error": loop_result.get("error"),
    }
    # Also populate stepwise so synthesize works regardless of which stage name was used
    state["stepwise"] = state["agent_loop"]
    if "plan" not in state:
        state["plan"] = {"skipped": True, "reason": "agent_loop mode"}

    await emit_event({"type": "dream.agent_loop.complete", "cycle_id": cycle_id,
                      "engine": engine, "steps": len(steps),
                      "cycles": loop_result.get("cycles", 0), "error": loop_result.get("error")})
    return state


# ─────────────────────────────────────────────────────────────────────────────
# SENSOR & STAGE LISTING
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.sensors.list", memory="off", silent=True,
    http_method="GET", http_path="/dream/sensors", http_tags=["dream"],
    description="List all registered dream sensors with metadata and configurable parameters.",
)
async def dream_sensors_list(trace_id=None):
    return {"sensors": list(SENSOR_REGISTRY.values()),
            "count": len(SENSOR_REGISTRY)}


@capability(
    "dream.sensor.preview", memory="off", silent=True,
    http_method="POST", http_path="/dream/sensor/preview", http_tags=["dream", "sensor"],
    description="Run a single sensor with the given params and return its live "
                "output (signal, count, source, summary, sample) — for previewing "
                "sensor data during pipeline configuration. If match/min_signal "
                "are included in params, also reports whether the fire condition "
                "would currently hold.",
)
async def dream_sensor_preview(sensor: str, params: Optional[Dict[str, Any]] = None,
                               trace_id=None):
    params = dict(params or {})
    full = sensor if sensor.startswith("dream.sensor.") else f"dream.sensor.{sensor}"
    meta = SENSOR_REGISTRY.get(full) or SENSOR_REGISTRY.get(sensor) or {}
    cap_name = meta.get("cap") or full
    cap = (CAPABILITY_REGISTRY.get(cap_name) or CAPABILITY_REGISTRY.get(full)
           or CAPABILITY_REGISTRY.get(sensor))
    if not cap:
        return {"ok": False, "error": f"sensor not found: {sensor}"}
    # Separate the fire-condition keys from the sensor's own params
    match       = params.pop("match", "") or ""
    match_field = params.pop("match_field", "all")
    min_signal  = params.pop("min_signal", None)
    negate      = bool(params.pop("negate", False))
    try:
        res = await cap["func"](**params) or {}
    except Exception as e:
        return {"ok": False, "error": str(e), "sensor": full}
    signal = float(res.get("signal", 0) or 0)
    out = {
        "ok": True, "sensor": full,
        "signal": signal, "count": res.get("count"),
        "source": res.get("source"), "summary": res.get("summary"),
        "sample": res.get("sample") or res.get("items") or res.get("records"),
    }
    # Evaluate the fire condition so the preview shows whether it WOULD trigger
    if match or min_signal is not None:
        would = True
        if min_signal is not None and signal < float(min_signal):
            would = False
        if match:
            if match_field == "sample":
                blob = json.dumps(res.get("sample") or res.get("items") or "", default=str)
            elif match_field == "summary":
                blob = str(res.get("summary") or "")
            else:
                blob = json.dumps(res, default=str)
            try:
                hit = bool(re.search(match, blob, re.I))
            except re.error:
                hit = match.lower() in blob.lower()
            if negate:
                hit = not hit
            if not hit:
                would = False
        out["would_fire"] = would
    return out


@capability(
    "dream.stages.list", memory="off", silent=True,
    http_method="GET", http_path="/dream/stages", http_tags=["dream"],
    description="List all registered pipeline stages with metadata and configurable parameters.",
)
async def dream_stages_list(trace_id=None):
    return {"stages": list(STAGE_REGISTRY.values()),
            "count": len(STAGE_REGISTRY)}


# ─────────────────────────────────────────────────────────────────────────────
# CUSTOM SENSORS — create sensors from any capability or DAG
# ─────────────────────────────────────────────────────────────────────────────

KEY_CUSTOM_SENSORS = "vera:dream:custom_sensors"  # hash: id -> JSON


@capability(
    "dream.sensor.custom.list", memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/custom/list", http_tags=["dream"],
    description="List all custom sensors (user-created from caps or DAGs).",
)
async def dream_sensor_custom_list(trace_id=None):
    r = _redis()
    built_in = list(SENSOR_REGISTRY.values())
    custom = []
    if r:
        try:
            items = await r.hgetall(KEY_CUSTOM_SENSORS)
            for _, v in (items or {}).items():
                try:
                    custom.append(json.loads(
                        v.decode() if isinstance(v, bytes) else v))
                except Exception:
                    continue
        except Exception:
            pass
    return {"built_in": built_in, "custom": custom,
            "total": len(built_in) + len(custom)}


@capability(
    "dream.sensor.custom.create", memory="off",
    http_method="POST", http_path="/dream/sensor/custom/create", http_tags=["dream"],
    description="Create a custom sensor from any capability, DAG, or Redis key. "
                "The sensor wraps the specified source and normalises its output to "
                "{source, count, signal, sample, summary} for the gather stage. "
                "Inputs: name (str!), label (str), description (str), "
                "source_type ('cap'|'dag'|'redis'|'fabric'), "
                "source_cap (str — cap name for cap type), "
                "source_dag (JSON — DAG array for dag type), "
                "source_key (str — Redis key for redis type), "
                "source_dataset (str — fabric dataset_id for fabric type), "
                "default_params (JSON dict — default kwargs for the cap/query), "
                "signal_field (str — which result field to use for signal calculation), "
                "sample_field (str — which result field contains the items list), "
                "signal_formula ('count'|'ratio'|'threshold') — how to compute signal.",
)
async def dream_sensor_custom_create(
    name: str,
    label: str = "",
    description: str = "",
    source_type: str = "cap",
    source_cap: str = "",
    source_dag: str = "[]",
    source_key: str = "",
    source_dataset: str = "",
    default_params: str = "{}",
    signal_field: str = "",
    sample_field: str = "",
    signal_formula: str = "count",
    trace_id=None,
):
    if not name:
        return {"ok": False, "error": "name required"}
    # Validate source
    if source_type == "cap" and source_cap and source_cap not in CAPABILITY_REGISTRY:
        return {"ok": False, "error": f"cap '{source_cap}' not found in registry"}

    try:
        dag = json.loads(source_dag) if isinstance(source_dag, str) else source_dag
    except Exception:
        dag = []
    try:
        params = json.loads(default_params) if isinstance(default_params, str) else default_params
    except Exception:
        params = {}

    sensor_id = f"custom.{name}"
    rec = {
        "id":             sensor_id,
        "name":           name,
        "label":          label or name,
        "description":    description or f"Custom sensor: {source_type} → {source_cap or source_key or source_dataset}",
        "source_type":    source_type,
        "source_cap":     source_cap,
        "source_dag":     dag,
        "source_key":     source_key,
        "source_dataset": source_dataset,
        "default_params": params,
        "signal_field":   signal_field or "count",
        "sample_field":   sample_field or "results",
        "signal_formula": signal_formula,
        "created_at":     now_iso(),
        "custom":         True,
    }

    r = _redis()
    if r:
        try:
            await r.hset(KEY_CUSTOM_SENSORS, sensor_id, json.dumps(rec))
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # Also register it in the in-memory SENSOR_REGISTRY so the gather
    # stage can call it and the trigger editor sees it immediately
    SENSOR_REGISTRY[sensor_id] = {
        "id":          sensor_id,
        "label":       rec["label"],
        "description": rec["description"],
        "cap":         f"dream.sensor.custom.run",
        "custom":      True,
        "params":      [
            {"name": "sensor_id", "type": "str", "default": sensor_id,
             "help": "auto-filled"},
        ] + [
            {"name": k, "type": type(v).__name__, "default": v,
             "help": "custom param"}
            for k, v in params.items()
        ],
    }

    return {"ok": True, "sensor": rec}


@capability(
    "dream.sensor.custom.delete", memory="off",
    http_method="POST", http_path="/dream/sensor/custom/delete", http_tags=["dream"],
    description="Delete a custom sensor by id.",
)
async def dream_sensor_custom_delete(sensor_id: str, trace_id=None):
    r = _redis()
    if r:
        try:
            await r.hdel(KEY_CUSTOM_SENSORS, sensor_id)
        except Exception:
            pass
    SENSOR_REGISTRY.pop(sensor_id, None)
    return {"ok": True, "deleted": sensor_id}


@capability(
    "dream.sensor.custom.run", memory="off", silent=True,
    http_method="POST", http_path="/dream/sensor/custom/run", http_tags=["dream"],
    description="Execute a custom sensor. Called by the gather stage for custom sensors. "
                "Wraps the configured source (cap/dag/redis/fabric) and normalises output.",
)
async def dream_sensor_custom_run(
    sensor_id: str = "",
    limit: int = 30,
    trace_id=None,
    **kwargs,
):
    # Load sensor definition
    r = _redis()
    rec = None
    if r:
        try:
            raw = await r.hget(KEY_CUSTOM_SENSORS, sensor_id)
            if raw:
                rec = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            pass
    if not rec:
        return {"source": sensor_id, "count": 0, "signal": 0.0,
                "error": f"custom sensor '{sensor_id}' not found"}

    src_type = rec.get("source_type", "cap")
    params = dict(rec.get("default_params") or {})
    params.update({k: v for k, v in kwargs.items()
                   if k not in ("sensor_id", "trace_id", "limit")})
    params["limit"] = int(limit)
    sample_field = rec.get("sample_field", "results")
    signal_field = rec.get("signal_field", "count")
    signal_formula = rec.get("signal_formula", "count")

    result: Any = {}

    try:
        if src_type == "cap":
            cap_name = rec.get("source_cap", "")
            if cap_name and cap_name in CAPABILITY_REGISTRY:
                result = await _call_cap(cap_name, **params)
            else:
                return {"source": sensor_id, "count": 0, "signal": 0.0,
                        "error": f"cap '{cap_name}' not found"}

        elif src_type == "dag":
            dag = rec.get("source_dag", [])
            if dag:
                dag_run_cap = CAPABILITY_REGISTRY.get("dag.run")
                if dag_run_cap:
                    result = await dag_run_cap["func"](
                        dag=dag, state=params, supervised=False)
                    result = result.get("result", result) if isinstance(result, dict) else {}
                else:
                    return {"source": sensor_id, "count": 0, "signal": 0.0,
                            "error": "dag.run cap not available"}

        elif src_type == "redis":
            key = rec.get("source_key", "")
            if r and key:
                key_type = (await r.type(key)).decode()
                if key_type == "list":
                    items = await r.lrange(key, 0, int(limit) - 1)
                    result = {"items": [
                        x.decode() if isinstance(x, bytes) else x
                        for x in (items or [])
                    ]}
                elif key_type == "stream":
                    items = await r.xrevrange(key, count=int(limit))
                    result = {"items": [
                        json.loads(x[1].get(b"data", b"{}"))
                        for x in (items or [])
                    ]}
                elif key_type == "hash":
                    items = await r.hgetall(key)
                    result = {"items": [
                        {"key": k.decode() if isinstance(k, bytes) else k,
                         "value": v.decode() if isinstance(v, bytes) else v}
                        for k, v in (items or {}).items()
                    ][:int(limit)]}
                elif key_type == "zset":
                    items = await r.zrevrange(key, 0, int(limit) - 1, withscores=True)
                    result = {"items": [
                        {"member": m.decode() if isinstance(m, bytes) else m,
                         "score": s}
                        for m, s in (items or [])
                    ]}
                else:
                    val = await r.get(key)
                    result = {"value": val.decode() if isinstance(val, bytes) else val}

        elif src_type == "fabric":
            ds = rec.get("source_dataset", "")
            fab_cap = CAPABILITY_REGISTRY.get("fabric.query")
            if fab_cap and ds:
                dsl = {"dataset_id": ds, "top_k": int(limit),
                       "include_data": True, "cache": False}
                if params.get("query"):
                    dsl["text"] = params["query"]
                result = await fab_cap["func"](query=json.dumps(dsl))

    except Exception as e:
        return {"source": sensor_id, "count": 0, "signal": 0.0,
                "error": str(e)}

    # Normalise output
    if not isinstance(result, dict):
        result = {"raw": str(result)[:2000]}

    # Extract sample items
    sample = result.get(sample_field) or result.get("items") or result.get("results") or []
    if not isinstance(sample, list):
        sample = [sample]
    sample = sample[:int(limit)]

    # Normalise sample items to have 'text' field
    norm_sample = []
    for item in sample:
        if isinstance(item, dict):
            text = (item.get("text") or item.get("message") or item.get("title")
                    or item.get("query") or item.get("summary") or item.get("value")
                    or str(item)[:300])
            norm_sample.append({
                "text": str(text)[:400],
                **{k: v for k, v in item.items()
                   if k in ("id", "ts", "created_at", "category", "dataset",
                            "tags", "score", "key", "member")},
            })
        elif isinstance(item, str):
            norm_sample.append({"text": item[:400]})
        else:
            norm_sample.append({"text": str(item)[:400]})

    # Compute signal
    count = len(norm_sample)
    if signal_formula == "count":
        signal = min(1.0, count / max(1, int(limit)))
    elif signal_formula == "ratio":
        sig_val = result.get(signal_field, count)
        signal = min(1.0, float(sig_val) / max(1, int(limit)))
    elif signal_formula == "threshold":
        signal = 1.0 if count > 0 else 0.0
    else:
        signal = min(1.0, count / max(1, int(limit)))

    return {
        "source":  sensor_id,
        "count":   count,
        "signal":  round(signal, 3),
        "sample":  norm_sample,
        "summary": f"{count} items from {src_type}:{rec.get('source_cap') or rec.get('source_key') or rec.get('source_dataset') or '?'}",
    }


# ─────────────────────────────────────────────────────────────────────────────
# CUSTOM STAGES — create pipeline stages from any capability or DAG
# ─────────────────────────────────────────────────────────────────────────────

KEY_CUSTOM_STAGES = "vera:dream:custom_stages"


@capability(
    "dream.stage.custom.list", memory="off", silent=True,
    http_method="GET", http_path="/dream/stage/custom/list", http_tags=["dream"],
    description="List all custom pipeline stages.",
)
async def dream_stage_custom_list(trace_id=None):
    r = _redis()
    built_in = list(STAGE_REGISTRY.values())
    custom = []
    if r:
        try:
            items = await r.hgetall(KEY_CUSTOM_STAGES)
            for _, v in (items or {}).items():
                try:
                    custom.append(json.loads(
                        v.decode() if isinstance(v, bytes) else v))
                except Exception:
                    continue
        except Exception:
            pass
    return {"built_in": built_in, "custom": custom,
            "total": len(built_in) + len(custom)}


@capability(
    "dream.stage.custom.create", memory="off",
    http_method="POST", http_path="/dream/stage/custom/create", http_tags=["dream"],
    description="Create a custom pipeline stage. When this stage runs in a dream pipeline, "
                "it receives the full dream state dict and returns the modified state. "
                "Source can be a capability (called with state as kwargs) or a DAG "
                "(executed with state as initial_state). "
                "Inputs: name (str!), label (str), description (str), phase (str: "
                "sense|analyze|plan|act|emit), source_type ('cap'|'dag'), "
                "source_cap (str), source_dag (JSON), default_params (JSON).",
)
async def dream_stage_custom_create(
    name: str,
    label: str = "",
    description: str = "",
    phase: str = "analyze",
    source_type: str = "cap",
    source_cap: str = "",
    source_dag: str = "[]",
    default_params: str = "{}",
    trace_id=None,
):
    if not name:
        return {"ok": False, "error": "name required"}
    stage_id = f"dream.stage.custom.{name}" if not name.startswith("dream.stage.") else name

    try:
        dag = json.loads(source_dag) if isinstance(source_dag, str) else source_dag
    except Exception:
        dag = []
    try:
        params = json.loads(default_params) if isinstance(default_params, str) else default_params
    except Exception:
        params = {}

    rec = {
        "id":             stage_id,
        "name":           name,
        "label":          label or name,
        "description":    description or f"Custom stage: {source_type} -> {source_cap or 'DAG'}",
        "phase":          phase if phase in ("sense", "analyze", "plan", "act", "emit") else "analyze",
        "source_type":    source_type,
        "source_cap":     source_cap,
        "source_dag":     dag,
        "default_params": params,
        "created_at":     now_iso(),
        "custom":         True,
        "optional":       True,
    }

    r = _redis()
    if r:
        try:
            await r.hset(KEY_CUSTOM_STAGES, stage_id, json.dumps(rec))
        except Exception as e:
            return {"ok": False, "error": str(e)}

    STAGE_REGISTRY[stage_id] = {
        "id":          stage_id,
        "label":       rec["label"],
        "description": rec["description"],
        "cap":         stage_id,
        "phase":       rec["phase"],
        "optional":    True,
        "custom":      True,
        "params":      [],
    }

    # Register a real @capability so the pipeline runner can call it
    # The cap wraps the configured source
    async def _custom_stage_runner(state=None, trace_id=None, _rec=rec):
        state = state or {}
        st = _rec.get("source_type", "cap")
        params = dict(_rec.get("default_params") or {})
        if st == "cap":
            cap_name = _rec.get("source_cap", "")
            if cap_name and cap_name in CAPABILITY_REGISTRY:
                try:
                    result = await _call_cap(cap_name, **params,
                                             **({"state": state} if "state" in
                                                CAPABILITY_REGISTRY[cap_name].get("schema", {}).get("properties", {})
                                                else {}))
                    if isinstance(result, dict) and "state" in result:
                        state.update(result["state"])
                    elif isinstance(result, dict):
                        state[f"custom_{_rec['name']}"] = result
                except Exception as e:
                    state[f"custom_{_rec['name']}_error"] = str(e)
        elif st == "dag":
            dag = _rec.get("source_dag", [])
            if dag:
                dag_run = CAPABILITY_REGISTRY.get("dag.run")
                if dag_run:
                    try:
                        result = await dag_run["func"](dag=dag, state=dict(state))
                        run_result = result.get("result", {}) if isinstance(result, dict) else {}
                        if isinstance(run_result, dict):
                            state.update(run_result)
                    except Exception as e:
                        state[f"custom_{_rec['name']}_error"] = str(e)
        return state

    _custom_stage_runner.__name__ = f"_custom_stage_{name}"
    CAPABILITY_REGISTRY[stage_id] = {
        "func":        _custom_stage_runner,
        "raw":         _custom_stage_runner,
        "schema":      {"type": "object", "properties": {"state": {"type": "object"}}, "required": []},
        "description": rec["description"],
        "source":      "local",
        "mcp_expose":  False,
        "memory":      "off",
        "silent":      True,
        "http_method":  "",
        "http_path":    "",
        "http_tags":    ["dream"],
        "tags":         ["dream"],
    }

    return {"ok": True, "stage": rec}


@capability(
    "dream.stage.custom.delete", memory="off",
    http_method="POST", http_path="/dream/stage/custom/delete", http_tags=["dream"],
    description="Delete a custom pipeline stage by id.",
)
async def dream_stage_custom_delete(stage_id: str, trace_id=None):
    r = _redis()
    if r:
        try:
            await r.hdel(KEY_CUSTOM_STAGES, stage_id)
        except Exception:
            pass
    STAGE_REGISTRY.pop(stage_id, None)
    CAPABILITY_REGISTRY.pop(stage_id, None)
    return {"ok": True, "deleted": stage_id}


# ─────────────────────────────────────────────────────────────────────────────
# DREAM DIRECTOR — system-wide orchestrator
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.director.assess", memory="off",
    http_method="POST", http_path="/dream/director/assess", http_tags=["dream"],
    description="The dream director assesses the full system state and recommends "
                "which triggers to fire (or skip), re-prioritises them, and optionally "
                "auto-fires director_managed triggers. Returns a recommendation list "
                "and a system state summary.",
)
async def dream_director_assess(auto_fire: bool = False, trace_id=None):
    global _CYCLE_TASK
    cfg = await _get_config()
    idle = await _idle_minutes()
    triggers = await _list_triggers()

    # Gather system state for the LLM
    system_state: Dict[str, Any] = {
        "idle_minutes": round(idle, 1),
        "scheduler_enabled": bool(cfg.get("enabled")),
        "in_cycle": bool(_CYCLE_TASK and not _CYCLE_TASK.done()),
        "current_hour": datetime.now().hour,
    }

    # Quick sensor probe — run each sensor with low limits for a signal snapshot
    signal_snapshot: Dict[str, float] = {}
    for sid, smeta in SENSOR_REGISTRY.items():
        cap_name = smeta.get("cap", sid)
        cap = CAPABILITY_REGISTRY.get(cap_name)
        if not cap:
            continue
        try:
            result = await cap["func"](limit=5)
            if isinstance(result, dict):
                signal_snapshot[sid] = float(result.get("signal", 0))
        except Exception:
            signal_snapshot[sid] = 0.0

    system_state["sensor_signals"] = signal_snapshot

    # Annotate each trigger with its due-ness
    assessments: List[Dict[str, Any]] = []
    for trig in triggers:
        due = await _trigger_due(trig, idle)
        last_run = await _last_run_ts(trig.get("name", "?"))
        assessments.append({
            "name":        trig.get("name"),
            "label":       trig.get("label"),
            "enabled":     trig.get("enabled"),
            "due":         due,
            "last_run":    last_run,
            "mode":        trig.get("mode"),
            "hitl":        trig.get("hitl"),
            "director_managed": trig.get("director_managed"),
            "sensors":     trig.get("sensors", []),
            "hours":       f"{trig.get('hours_start',0)}-{trig.get('hours_end',24)}",
            "min_idle":    trig.get("min_idle_minutes"),
            "cooldown":    trig.get("min_interval_minutes"),
            "require_signal": trig.get("require_signal"),
        })

    # Ask the LLM to prioritise
    trig_lines = "\n".join(
        f"  {a['name']}: enabled={a['enabled']}, due={a['due']}, "
        f"signal sensors={[signal_snapshot.get(s,0) for s in (a['sensors'] or [])]}, "
        f"hours={a['hours']}, last_run={a['last_run'] or 'never'}"
        for a in assessments
    )

    llm_prompt = (
        "You are the Dream Director. Given the system state below, rank "
        "which triggers should run NOW. Return ONLY a JSON array of objects:\n"
        '  [{"name": "trigger_name", "action": "fire"|"skip"|"defer", '
        '"reason": "...", "priority": 1-10}]\n\n'
        f"System state:\n"
        f"  Idle: {system_state['idle_minutes']}m\n"
        f"  Hour: {system_state['current_hour']}\n"
        f"  In cycle: {system_state['in_cycle']}\n\n"
        f"Triggers:\n{trig_lines}\n\n"
        f"Sensor signal snapshot: {json.dumps(signal_snapshot)}\n\n"
        "Rules:\n"
        "- Never fire if a cycle is already running.\n"
        "- Prefer triggers whose sensors show strong signal.\n"
        "- Respect hours windows and cooldowns.\n"
        "- Prefer triggers that haven't run in a long time.\n"
        "- Fire at most 1-2; defer the rest."
    )

    recommendations: List[Dict[str, Any]] = []
    raw = await _llm_generate(llm_prompt,
                               system="You are a scheduling director. JSON array only.")
    if raw:
        try:
            start = raw.find("[")
            end = raw.rfind("]")
            if start != -1 and end != -1:
                parsed = json.loads(raw[start:end+1])
                if isinstance(parsed, list):
                    recommendations = [r for r in parsed if isinstance(r, dict)]
        except Exception:
            pass

    # Auto-fire when user explicitly requested it via the panel.
    # When auto_fire=True, the user has clicked "Assess + fire" — we fire the
    # top-priority "fire" recommendation regardless of director_managed flag
    # (that flag only gates background auto-firing from the scheduler tick).
    auto_fired: List[str] = []
    if auto_fire and not system_state["in_cycle"]:
        # Sort by priority desc so highest priority fires first
        fire_recs = sorted(
            [r for r in recommendations if r.get("action") == "fire"],
            key=lambda r: -int(r.get("priority", 0) or 0),
        )
        for rec in fire_recs:
            tname = rec.get("name", "")
            trig = next((t for t in triggers if t.get("name") == tname), None)
            if trig and trig.get("enabled"):
                if not (_CYCLE_TASK and not _CYCLE_TASK.done()):
                    _CYCLE_TASK = asyncio.create_task(
                        _run_cycle(trig, force=True,
                                   seed={"director_fired": True,
                                         "director_reason": rec.get("reason", "")}))
                    auto_fired.append(tname)
                    break  # one at a time
        # Fallback: if LLM didn't recommend any fires but user asked, fire the most due trigger
        if not auto_fired:
            due_triggers = [a for a in assessments if a.get("due") and a.get("enabled")]
            if due_triggers:
                trig = next((t for t in triggers if t.get("name") == due_triggers[0]["name"]), None)
                if trig and not (_CYCLE_TASK and not _CYCLE_TASK.done()):
                    _CYCLE_TASK = asyncio.create_task(
                        _run_cycle(trig, force=True,
                                   seed={"director_fired": True,
                                         "director_reason": "fallback: no LLM recommendations, firing most-due trigger"}))
                    auto_fired.append(trig.get("name"))

    await emit_event({
        "type": "dream.director.assessed",
        "recommendations": len(recommendations),
        "auto_fired": auto_fired,
        "idle": system_state["idle_minutes"],
    })

    return {
        "system_state": system_state,
        "assessments": assessments,
        "recommendations": recommendations,
        "auto_fired": auto_fired,
        "raw": raw[:800] if raw else "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# TRIGGER TIMELINE PROJECTION
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.timeline", memory="off", silent=True,
    http_method="GET", http_path="/dream/timeline", http_tags=["dream"],
    description="Project when each trigger will next fire, based on schedule, "
                "cooldown, current idle, and current time. Returns a list of "
                "projected fire windows for the next N hours.",
)
async def dream_timeline(hours_ahead: int = 24, trace_id=None):
    try:
        triggers = await _list_triggers()
    except Exception as e:
        return {"triggers": [], "count": 0, "error": str(e),
                "current_hour": datetime.now().hour, "current_idle": 0,
                "hours_ahead": int(hours_ahead)}

    idle = await _idle_minutes()
    now = datetime.now()
    results: List[Dict[str, Any]] = []
    hours_ahead = max(1, min(72, int(hours_ahead or 24)))

    for trig in triggers:
        try:
            if not trig.get("enabled"):
                continue

            name = trig.get("name", "?")
            h_start = int(trig.get("hours_start") or 0)
            h_end = int(trig.get("hours_end") or 24)
            min_idle = int(trig.get("min_idle_minutes") or 15)
            cooldown = int(trig.get("min_interval_minutes") or 60)

            last_run = None
            try:
                last_run = await _last_run_ts(name)
            except Exception:
                pass

            cooldown_until = None
            if last_run and isinstance(last_run, str):
                try:
                    last_dt = datetime.fromisoformat(
                        last_run.replace("Z", "+00:00"))
                    cooldown_until_dt = last_dt.replace(
                        tzinfo=None) + timedelta(minutes=cooldown)
                    if cooldown_until_dt > now:
                        cooldown_until = cooldown_until_dt.isoformat()
                except Exception:
                    pass

            windows: List[Dict[str, Any]] = []
            for h_offset in range(hours_ahead):
                check_time = now + timedelta(hours=h_offset)
                h = check_time.hour
                in_window = _within_hours(h_start, h_end, check_time)
                blocked = bool(
                    cooldown_until
                    and check_time.isoformat() < cooldown_until
                )
                windows.append({
                    "hour": h,
                    "time": check_time.strftime("%H:%M"),
                    "offset_h": h_offset,
                    "in_window": in_window,
                    "blocked_cooldown": blocked,
                    "can_fire": in_window and not blocked,
                })

            earliest = next(
                (w for w in windows if w["can_fire"]), None)

            results.append({
                "trigger": name,
                "label": trig.get("label", name),
                "hours_window": f"{h_start}-{h_end}",
                "min_idle": min_idle,
                "cooldown_minutes": cooldown,
                "cooldown_until": cooldown_until,
                "last_run": last_run,
                "mode": trig.get("mode"),
                "hitl": trig.get("hitl"),
                "earliest_slot": earliest,
                "windows": windows,
                "idle_met": idle >= min_idle,
                "idle_remaining_m": max(0, round(min_idle - idle, 1)) if idle < min_idle else 0,
                "fires_in": (
                    f"idle met, waiting for hour window"
                    if idle >= min_idle and not earliest
                    else f"~{max(0, round(min_idle - idle))}m idle remaining"
                    if idle < min_idle
                    else f"ready now"
                    if earliest and earliest.get("offset_h", 99) == 0
                    else f"~{earliest['offset_h']}h ({earliest['time']})"
                    if earliest
                    else "no slot in window"
                ),
            })
        except Exception as e:
            log.debug("timeline trigger %s: %s", trig.get("name"), e)
            continue

    results.sort(key=lambda r: (
        r.get("earliest_slot") or {}).get("offset_h", 999))

    return {
        "triggers": results,
        "count": len(results),
        "current_hour": now.hour,
        "current_idle": round(idle, 1),
        "hours_ahead": hours_ahead,
    }


@capability(
    "dream.preview", memory="off",
    http_method="POST", http_path="/dream/preview", http_tags=["dream"],
    description="Run a dream cycle in preview mode — gather + themes + plan only, "
                "no execute, no deliver, no history persist. Returns the proposed plan, "
                "themes, sensor signal and a sample of inputs for inspection. "
                "Optional seed dict (focus_topic, pinned_memory_ids, extra_fabric_ids, "
                "extra_prompt, force_caps).",
)
async def dream_preview(
    trigger_name: str,
    seed: Optional[Dict[str, Any]] = None,
    trace_id=None,
):
    global _CYCLE_TASK
    trig = await _get_trigger(trigger_name)
    if not trig:
        return {"ok": False, "error": f"unknown trigger: {trigger_name}"}
    if _CYCLE_TASK and not _CYCLE_TASK.done():
        return {"ok": False, "error": "a cycle is already running — try again shortly"}
    if isinstance(seed, str):
        try:
            seed = json.loads(seed) if seed.strip() else {}
        except Exception:
            seed = {}
    _CYCLE_TASK = asyncio.create_task(_run_cycle(
        trig, force=True, seed=seed or {}, preview_only=True,
    ))
    try:
        record = await _CYCLE_TASK
    finally:
        _CYCLE_TASK = None
    return {"ok": True, "preview": record}


@capability(
    "dream.preview.last", memory="off", silent=True,
    http_method="GET", http_path="/dream/preview/last", http_tags=["dream"],
    description="Return the most recent preview record for a named trigger, if any.",
)
async def dream_preview_last(trigger_name: str, trace_id=None):
    r = _redis()
    if not r or not trigger_name:
        return {"preview": None}
    try:
        raw = await r.hget(KEY_PREVIEW, trigger_name)
        if not raw:
            return {"preview": None}
        return {"preview": json.loads(raw.decode() if isinstance(raw, bytes) else raw)}
    except Exception:
        return {"preview": None}


@capability(
    "dream.llm.tokens", memory="off", silent=True,
    http_method="GET", http_path="/dream/llm/tokens", http_tags=["dream"],
    description="Read the per-cycle LLM token ring buffer (most recent N tokens) so the "
                "panel can poll-render streamed output without holding an SSE socket open. "
                "Inputs: cycle_id (str!), limit (int, default 500).",
)
async def dream_llm_tokens(cycle_id: str, limit: int = 500, trace_id=None):
    r = _redis()
    if not r or not cycle_id:
        return {"tokens": [], "text": "", "count": 0}
    key = f"{KEY_LLM_TOKENS}:{cycle_id}"
    try:
        # Defensive cast — limit may arrive as str when called via HTTP query params
        limit_int = max(1, min(int(limit), 2000))
        rows = await r.lrange(key, -limit_int, -1)
        toks = [(x.decode() if isinstance(x, bytes) else str(x)) for x in (rows or [])]
        return {"tokens": toks, "text": "".join(toks), "count": len(toks),
                "cycle_id": cycle_id}
    except Exception as e:
        return {"tokens": [], "text": "", "count": 0, "error": str(e)}


@capability(
    "dream.trigger.generate", memory="on",
    http_method="POST", http_path="/dream/trigger/generate", http_tags=["dream"],
    description="LLM-generate a complete dream trigger record from a natural-language "
                "description. Returns a draft trigger dict — does NOT persist it; the UI "
                "should preview / edit / save via dream.trigger.upsert. "
                "Inputs: description (str!), name_hint (str, optional).",
)
async def dream_trigger_generate(
    description: str,
    name_hint: str = "",
    trace_id=None,
):
    if not description.strip():
        return {"ok": False, "error": "description required"}

    sensors_avail = [
        "dream.sensor.memory_recent", "dream.sensor.fabric_recent",
        "dream.sensor.syslog_errors", "dream.sensor.bus_events",
        "dream.sensor.news_overnight", "dream.sensor.research_recent",
    ]
    stages_avail = [
        "dream.stage.gather", "dream.stage.themes", "dream.stage.plan",
        "dream.stage.execute", "dream.stage.synthesize", "dream.stage.deliver",
    ]
    whitelist_avail = await _get_whitelist()

    # Identify cap groups currently loaded so the suggestion is realistic
    loaded_groups = sorted({n.split(".")[0] for n in CAPABILITY_REGISTRY.keys()})

    system = (
        "You design Vera 'dream' triggers — small recipes for a background reflection. "
        "Reply with a single JSON object only (no prose, no code fences) matching this schema:\n"
        '{ "name": "snake_case_id", "label": "Human Title",\n'
        '  "description": "1 sentence", "enabled": true,\n'
        '  "sensors": [ subset of provided sensor names ],\n'
        '  "pipeline": [ ordered subset of provided stage names ],\n'
        '  "whitelist": [ cap names ],\n'
        '  "mode": "synthesize_only" | "plan_execute" | "oneshot",\n'
        '  "hitl": bool,\n'
        '  "min_idle_minutes": int,\n'
        '  "hours_start": int (0-23), "hours_end": int (0-24),\n'
        '  "min_interval_minutes": int,\n'
        '  "deliver_to": [ "memory" | "telegram" | "notebook" ],\n'
        '  "prompt": "specific guidance for the synthesizer LLM",\n'
        '  "require_signal": float (0.0-1.0)\n'
        "}\n"
        "Pick sensible defaults for omitted fields. Choose sensors and whitelist caps "
        "ONLY from the lists below. Pipeline must include dream.stage.gather first and "
        "dream.stage.deliver last; include dream.stage.plan + dream.stage.execute only "
        "if mode is plan_execute or oneshot."
    )

    prompt = (
        f"User wants a trigger for: {description}\n"
        + (f"Suggested name: {name_hint}\n" if name_hint else "")
        + f"\nAvailable sensors:\n  " + "\n  ".join(sensors_avail)
        + f"\n\nAvailable stages:\n  " + "\n  ".join(stages_avail)
        + f"\n\nWhitelisted caps available to the planner:\n  "
        + "\n  ".join(whitelist_avail[:80])
        + f"\n\nCap groups loaded in this orchestrator: {', '.join(loaded_groups)}"
    )

    raw = await _llm_generate(prompt, system=system, prefer_gpu=True)
    if not raw:
        return {"ok": False, "error": "LLM returned empty response"}

    # Extract JSON from raw text
    parsed: Optional[Dict[str, Any]] = None
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            parsed = json.loads(raw[start:end + 1])
    except Exception:
        parsed = None

    if not isinstance(parsed, dict):
        return {"ok": False, "error": "could not parse JSON from LLM",
                "raw": raw[:600]}

    # Sanitise / validate
    rec: Dict[str, Any] = {
        "name":  re.sub(r"[^a-z0-9_]", "_", str(parsed.get("name", "")).lower())[:48]
                 or re.sub(r"[^a-z0-9_]", "_", (name_hint or description[:24]).lower())[:48],
        "label": str(parsed.get("label", "") or description[:60]),
        "description": str(parsed.get("description", "") or description[:200]),
        "enabled": bool(parsed.get("enabled", True)),
        "sensors": [s for s in (parsed.get("sensors") or [])
                    if s in sensors_avail][:6],
        "pipeline": [s for s in (parsed.get("pipeline") or [])
                     if s in stages_avail][:8],
        "whitelist": [c for c in (parsed.get("whitelist") or [])
                      if isinstance(c, str)][:60],
        "mode":  parsed.get("mode") if parsed.get("mode") in
                 ("synthesize_only", "plan_execute", "oneshot") else "synthesize_only",
        "hitl":  bool(parsed.get("hitl", False)),
        "min_idle_minutes":     max(0, int(parsed.get("min_idle_minutes", 30) or 30)),
        "hours_start":          max(0, min(23, int(parsed.get("hours_start", 0) or 0))),
        "hours_end":            max(0, min(24, int(parsed.get("hours_end", 24) or 24))),
        "min_interval_minutes": max(0, int(parsed.get("min_interval_minutes", 360) or 360)),
        "deliver_to": [d for d in (parsed.get("deliver_to") or ["memory"])
                       if d in ("memory", "telegram", "notebook")] or ["memory"],
        "prompt": str(parsed.get("prompt", "") or "Synthesize the recent activity."),
        "require_signal": max(0.0, min(1.0, float(parsed.get("require_signal", 0.0) or 0.0))),
    }

    # Repair pipeline: ensure gather-first / deliver-last invariants
    if "dream.stage.gather" not in rec["pipeline"]:
        rec["pipeline"].insert(0, "dream.stage.gather")
    if "dream.stage.synthesize" not in rec["pipeline"]:
        rec["pipeline"].append("dream.stage.synthesize")
    if "dream.stage.deliver" not in rec["pipeline"]:
        rec["pipeline"].append("dream.stage.deliver")
    # Move gather to front, deliver to end
    rec["pipeline"].sort(key=lambda s: (
        0 if s == "dream.stage.gather" else
        2 if s == "dream.stage.deliver" else
        1
    ))

    return {"ok": True, "trigger": rec, "raw": raw[:1200]}


# ─────────────────────────────────────────────────────────────────────────────
# REGISTRY SEEDING — describe every built-in sensor and stage
# ─────────────────────────────────────────────────────────────────────────────
# Adding a new sensor or stage is a two-step process:
#   1. Define an @capability("dream.sensor.X" | "dream.stage.X")
#   2. Call _register_sensor / _register_stage with its metadata
# The panel reads these registries to render selection UI and parameter inputs.

_register_sensor(
    "memory_recent",
    "Memory — recent records",
    "Pulls recent records from the memory backends. Useful for spotting what the "
    "system has been thinking about.",
    "dream.sensor.memory_recent",
    params=[
        {"name": "limit", "type": "int", "default": 30, "help": "max records to fetch"},
    ],
)
_register_sensor(
    "fabric_recent",
    "Fabric — recent records",
    "Pulls recent records from the data fabric across all datasets.",
    "dream.sensor.fabric_recent",
    params=[
        {"name": "limit", "type": "int", "default": 20, "help": "max records to fetch"},
    ],
)
_register_sensor(
    "syslog_errors",
    "Syslog — errors and warnings",
    "Recent errors and warnings from the Vera syslog feed.",
    "dream.sensor.syslog_errors",
    params=[
        {"name": "limit", "type": "int", "default": 40, "help": "max entries"},
    ],
)
_register_sensor(
    "bus_events",
    "Event bus — recent activity",
    "Recent events from the cap-call bus. Noisy — best paired with a high "
    "require_signal threshold.",
    "dream.sensor.bus_events",
    params=[
        {"name": "limit", "type": "int", "default": 50, "help": "max events"},
    ],
)
_register_sensor(
    "news_overnight",
    "News — overnight RSS",
    "Pulls overnight items from RSS-style fabric datasets. Auto-detects "
    "dataset names containing 'rss', 'news', 'feed'.",
    "dream.sensor.news_overnight",
    params=[
        {"name": "limit", "type": "int", "default": 40, "help": "max items"},
    ],
)
_register_sensor(
    "research_recent",
    "Research — recent jobs",
    "Recent research jobs and notebook activity.",
    "dream.sensor.research_recent",
    params=[
        {"name": "limit", "type": "int", "default": 20, "help": "max jobs"},
    ],
)
_register_sensor(
    "memory_session",
    "Memory — specific session",
    "Recent memory records from a specific session id (e.g. an active chat thread).",
    "dream.sensor.memory_session",
    params=[
        {"name": "session_id", "type": "str", "default": "", "help": "session id"},
        {"name": "limit",      "type": "int", "default": 30, "help": "max records"},
    ],
)
_register_sensor(
    "fabric_dataset",
    "Fabric — specific dataset",
    "Pull recent records from one specific fabric dataset by id.",
    "dream.sensor.fabric_dataset",
    params=[
        {"name": "dataset_id", "type": "str", "default": "", "help": "dataset id"},
        {"name": "limit",      "type": "int", "default": 30, "help": "max records"},
        {"name": "query",      "type": "str", "default": "", "help": "optional text filter"},
    ],
)
_register_sensor(
    "fabric_by_tag",
    "Fabric — by source tag (auto-discover)",
    "Auto-discover datasets via fabric source tags. Add a new RSS feed tagged "
    "'news' and morning_news will pick it up automatically — no trigger reconfig.",
    "dream.sensor.fabric_by_tag",
    params=[
        {"name": "tags",        "type": "str", "default": "", "help": "comma-sep, e.g. 'news,rss'"},
        {"name": "limit",       "type": "int", "default": 30, "help": "max records overall"},
        {"name": "per_dataset", "type": "int", "default": 10, "help": "max records per matching dataset"},
    ],
)
_register_sensor(
    "fabric_by_source_type",
    "Fabric — by source type (rss/api/http/wiki)",
    "Same idea as fabric_by_tag but matches source_type — use 'rss' to pull "
    "from every RSS feed, 'api' for every API source, etc.",
    "dream.sensor.fabric_by_source_type",
    params=[
        {"name": "source_type", "type": "str", "default": "rss", "help": "rss|api|http|wiki"},
        {"name": "limit",       "type": "int", "default": 30, "help": "max records overall"},
        {"name": "per_dataset", "type": "int", "default": 10, "help": "max records per dataset"},
    ],
)
_register_sensor(
    "cap_calls",
    "Cap calls — by prefix",
    "Recent capability calls matching a name prefix (e.g. 'llm.', 'memory.').",
    "dream.sensor.cap_calls",
    params=[
        {"name": "prefix", "type": "str", "default": "", "help": "cap name prefix"},
        {"name": "limit",  "type": "int", "default": 50, "help": "max events"},
    ],
)
_register_sensor(
    "notebook_recent",
    "Notebook — recent entries",
    "Recently-written notebook entries — what's been jotted down lately.",
    "dream.sensor.notebook_recent",
    params=[
        {"name": "limit", "type": "int", "default": 15, "help": "max entries"},
    ],
)
_register_sensor(
    "ide_workspace",
    "IDE — recent workspace changes",
    "Recently-modified files in IDE workspaces.",
    "dream.sensor.ide_workspace",
    params=[
        {"name": "workspace", "type": "str", "default": "", "help": "filter by workspace name"},
        {"name": "limit",     "type": "int", "default": 20, "help": "max files"},
    ],
)
_register_sensor(
    "project_context",
    "Project — load project context",
    "Resolve a project's full context: user notes, LLM-maintained state, linked resources.",
    "dream.sensor.project_context",
    params=[
        {"name": "project_slug", "type": "str", "default": "", "help": "project slug"},
    ],
)
# Phase 1 sensors
_register_sensor(
    "active_projects",
    "Active projects — cap call clustering",
    "Clusters recent cap calls by namespace to detect what the user is actively "
    "working on. Returns the top N most-called prefixes with counts and examples.",
    "dream.sensor.active_projects",
    params=[
        {"name": "limit",      "type": "int", "default": 200, "help": "max cap events to scan"},
        {"name": "top_n",      "type": "int", "default": 5,   "help": "top N prefixes to return"},
        {"name": "hours_back", "type": "int", "default": 6,   "help": "look back N hours"},
    ],
)
_register_sensor(
    "source_changes",
    "Source — code changes",
    "Compares the live Vera source tree against the latest inspect snapshot. "
    "Reports changed files, module stats, and cap count.",
    "dream.sensor.source_changes",
    params=[],
)
_register_sensor(
    "source_review_state",
    "Source — review state",
    "Current snapshot + which files have/haven't been reviewed + last run. Use "
    "for continuation reviews instead of memory_recent.",
    "dream.sensor.source_review_state",
    params=[],
)
_register_sensor(
    "memory_graph_walk",
    "Memory — random graph walk",
    "Picks a random recent memory node (weighted toward under-explored ones) "
    "and traverses edges + semantic similarity to surface unexplored graph regions.",
    "dream.sensor.memory_graph_walk",
    params=[
        {"name": "seed_limit",     "type": "int", "default": 20, "help": "pool size to pick seed from"},
        {"name": "traverse_depth", "type": "int", "default": 2,  "help": "max edge hops"},
        {"name": "traverse_limit", "type": "int", "default": 15, "help": "max connected nodes"},
    ],
)

_register_stage(
    "dream.stage.gather", "Gather sensors",
    "Calls every configured sensor and aggregates their signal.",
    "dream.stage.gather", phase="gather", optional=False,
)
_register_stage(
    "dream.stage.themes", "Detect themes",
    "Extracts themes/topics from gathered data using NLP modules or LLM fallback.",
    "dream.stage.themes", phase="analyze", optional=True,
)
_register_stage(
    "dream.stage.goal_refine", "Goal refine — actionable goal from sensor data",
    "Distils raw themes and sensor data into ONE specific, actionable goal "
    "sentence for the agent loop. Place between themes and agent_loop. "
    "Prevents vague goals like 'explore recent activity' in favour of "
    "concrete, tool-oriented goals grounded in real data.",
    "dream.stage.goal_refine", phase="plan", optional=True,
)
_register_stage(
    "dream.stage.snapshot_source", "Snapshot source — pre-step for code review",
    "Takes a fresh source snapshot (or reuses a current one), diffs against "
    "live source, and stores snapshot_id + review_candidates in state. "
    "Place before goal_refine in source_review pipelines so the agent loop "
    "doesn't waste cycles on snapshot management.",
    "dream.stage.snapshot_source", phase="gather", optional=True,
)
_register_stage(
    "dream.stage.review_codebase", "Review codebase (deterministic)",
    "Deterministic source review of changed / wandered / continued files against "
    "the current snapshot (via ide.inspect.review_file). Writes state['review'] "
    "(results, high_severity_files). Configure via stage_config.review_codebase = "
    "{review_type: changes|wander|continue, max_files}.",
    "dream.stage.review_codebase", phase="analyze", optional=True,
    params=[
        {"name": "review_type", "type": "str", "default": "changes",
         "help": "changes | wander | continue"},
        {"name": "max_files", "type": "int", "default": 8, "help": "files per run"},
    ],
)
_register_stage(
    "dream.stage.review_report", "Review report (markdown)",
    "Turns state['review'] into a rich markdown report (priority issues, per-file "
    "detail, plan) with an optional LLM executive summary. Place after "
    "review_codebase in deterministic source-review pipelines.",
    "dream.stage.review_report", phase="emit", optional=True,
)
_register_stage(
    "dream.stage.deep_review", "Deep source review (LLM, chunked)",
    "Runs the whole-project deep review engine across one or more styles, chunking "
    "large files (no truncation), resumable, interruptible (pauses on user "
    "activity / pause flag, yields on file/time budget). Configure via "
    "stage_config.deep_review = {styles, area, max_files, max_chars, resume, "
    "review_type, baseline_snapshot, max_runtime_s, pause_on_activity, "
    "activity_idle_min}.",
    "dream.stage.deep_review", phase="analyze", optional=True,
    params=[
        {"name": "styles", "type": "str", "default": "",
         "help": "comma list: docs,critique,improvement,integration,architecture"},
        {"name": "area", "type": "str", "default": "", "help": "subsystem (blank = all)"},
        {"name": "max_files", "type": "int", "default": 0, "help": "files per run (0 = all)"},
        {"name": "max_chars", "type": "int", "default": 16000, "help": "per-chunk budget"},
        {"name": "max_runtime_s", "type": "int", "default": 0,
         "help": "yield after this many seconds (0 = no limit)"},
        {"name": "resume", "type": "bool", "default": True,
         "help": "continue from first un-reviewed file across runs"},
        {"name": "pause_on_activity", "type": "bool", "default": True,
         "help": "yield to the user when active"},
    ],
)
_register_stage(
    "dream.stage.ide_workspace_act", "IDE workspace — draft fixes",
    "OFF by default. Drafts fixes for high-severity review files into a sandbox "
    "IDE workspace via ide.agent.chat + ide.fs.write (never live source). "
    "Configure via stage_config.ide_workspace_act = {enabled, workspace, max_files}.",
    "dream.stage.ide_workspace_act", phase="emit", optional=True,
    params=[
        {"name": "enabled", "type": "bool", "default": False, "help": "draft fixes"},
        {"name": "workspace", "type": "str", "default": "vera-review-fixes",
         "help": "IDE workspace name"},
        {"name": "max_files", "type": "int", "default": 3, "help": "files to draft"},
    ],
)
_register_stage(
    "dream.stage.ide_agent", "IDE agent loop (workspace + snapshot)",
    "Runs a bounded IDE agent loop (ide.agent.chat) over an IDE workspace seeded "
    "from the source snapshot, toward a goal. The agent reads/edits within the "
    "sandbox workspace. Configure via stage_config.ide_agent = {goal, agent, "
    "workspace, max_turns, files, from_review, max_files}.",
    "dream.stage.ide_agent", phase="analyze", optional=True,
    params=[
        {"name": "goal", "type": "str", "default": "", "help": "agent objective"},
        {"name": "agent", "type": "str", "default": "code-reviewer", "help": "IDE agent name"},
        {"name": "workspace", "type": "str", "default": "vera-dream-agent",
         "help": "IDE workspace name"},
        {"name": "max_turns", "type": "int", "default": 4, "help": "agent loop turns"},
        {"name": "max_files", "type": "int", "default": 5, "help": "snapshot files seeded"},
        {"name": "from_review", "type": "bool", "default": True,
         "help": "seed from the review's high-severity files"},
    ],
)
_register_stage(
    "dream.stage.cap_execute", "Cap execute — run a single capability",
    "Run a specific capability as a pipeline stage. Configure in trigger's "
    "stage_config: {cap_execute: {cap: 'cap.name', params: {key: value}}}. "
    "Params can use $state_key to reference state values.",
    "dream.stage.cap_execute", phase="act", optional=True,
)
_register_stage(
    "dream.stage.dag_execute", "DAG execute — run a DAG workflow",
    "Run a named or inline DAG workflow as a pipeline stage. Configure in "
    "trigger's stage_config: {dag_execute: {dag_id: 'name'}} or "
    "{dag_execute: {steps: [['cap','output_key']]}}.",
    "dream.stage.dag_execute", phase="act", optional=True,
)
_register_stage(
    "dream.stage.project_action", "Project action — execute next steps",
    "Execute concrete project actions (not just propose them). Reads the "
    "refined_goal or proposed_action and uses a focused agent loop with "
    "write-capable whitelist to carry it out. Place after goal_refine.",
    "dream.stage.project_action", phase="act", optional=True,
)
_register_stage(
    "dream.stage.memory_deep_traverse", "Memory deep traverse",
    "Deep graph traversal (3-4 hops) from seed topics. Finds orphans, "
    "clusters, and under-explored regions. Feeds results into goal_refine.",
    "dream.stage.memory_deep_traverse", phase="gather", optional=True,
)
_register_stage(
    "dream.stage.fabric_explore", "Fabric explore — datasets + entities",
    "Explores fabric datasets: finds unprocessed records needing entity "
    "extraction, discovers cross-dataset entity overlap. Feeds into goal_refine.",
    "dream.stage.fabric_explore", phase="gather", optional=True,
)
_register_stage(
    "dream.stage.plan", "Plan a DAG (oneshot)",
    "Asks the LLM planner to produce a complete DAG of capability calls "
    "constrained to the dream whitelist. Used by oneshot mode.",
    "dream.stage.plan", phase="plan", optional=True,
)
_register_stage(
    "dream.stage.execute", "Execute the planned DAG",
    "Runs the DAG produced by the plan stage. Honours HITL if the trigger requires it.",
    "dream.stage.execute", phase="act", optional=True,
)
_register_stage(
    "dream.stage.stepwise_execute", "Stepwise — DAG Workshop agent loop (compat)",
    "Delegates to dag.agent_loop_v2 (the DAG Workshop ReAct engine). "
    "Each cycle the LLM picks ONE tool to call, observes the result, and "
    "iterates until satisfied or max_steps is reached. Tool-call history is "
    "surfaced as state['stepwise']['steps']. Prefer dream.stage.agent_loop "
    "for new pipelines.",
    "dream.stage.stepwise_execute", phase="act", optional=True,
)
_register_stage(
    "dream.stage.synthesize", "Synthesize report",
    "Asks the LLM to write the dream report. Honours the trigger's depth setting "
    "(brief / standard / deep / exhaustive) and an optional output_style.",
    "dream.stage.synthesize", phase="emit", optional=True,
    params=[{"name": "output_style", "type": "str", "default": "",
             "help": "shared output style: docs|critique|improvement|integration|"
                     "architecture (blank = depth-based default)"}],
)
_register_stage(
    "dream.stage.enrich_context", "Enrich — fetch missing info",
    "Asks the LLM what additional info would help, then fetches it via memory/fabric/web. "
    "Adds results to state['enriched']. Run between gather and synthesize for richer reports.",
    "dream.stage.enrich_context", phase="analyze", optional=True,
)
_register_stage(
    "dream.stage.propose_action", "Propose next action",
    "LLM proposes one concrete next action based on themes + sensor activity. "
    "Doesn't execute it — surfaces the proposal in the report.",
    "dream.stage.propose_action", phase="plan", optional=True,
)
_register_stage(
    "dream.stage.quality_check", "Quality check report",
    "Grades the synthesized report 1-10 on groundedness, specificity, usefulness. "
    "Run after synthesize to flag low-quality output.",
    "dream.stage.quality_check", phase="analyze", optional=True,
)
_register_stage(
    "dream.stage.investigate", "Investigate — DAG Workshop agent loop",
    "Delegates to dag.agent_loop_v2 (the DAG Workshop ReAct engine) to run "
    "an iterative investigation. The loop selects tools from the whitelist, "
    "calls them, observes results, and halts when satisfied or max_iterations "
    "is reached. Findings are stored in state['findings'] for the synthesize "
    "stage. Requires dag.agent_loop_v2 (vera_context.py) to be loaded.",
    "dream.stage.investigate", phase="act", optional=True,
)
_register_stage(
    "dream.stage.agent_loop", "Agent Loop — DAG Workshop ReAct engine",
    "Cleaner entry point for dag.agent_loop_v2. Identical behaviour to "
    "dream.stage.investigate but without legacy iteration-runner overhead. "
    "Populates state['agent_loop'] and state['stepwise'] for synthesize.",
    "dream.stage.agent_loop", phase="act", optional=True,
)
_register_stage(
    "dream.stage.deliver", "Deliver report",
    "Delivers the finished report to the configured channels (memory / telegram / notebook).",
    "dream.stage.deliver", phase="emit", optional=True,
)
_register_stage(
    "dream.stage.pivot", "Pivot — hand off to another dream",
    "Decides whether this dream's findings warrant handing off to a DIFFERENT "
    "follow-up dream (or continuing this one). The LLM picks from the candidate "
    "pipelines; the chosen one is scheduled next. An emit-phase sibling of "
    "iterate — use pivot to branch into other pipelines, iterate to keep going "
    "on this one. Place near the end (after deliver).",
    "dream.stage.pivot", phase="emit", optional=True,
    params=[
        {"name": "candidates", "type": "str", "default": "",
         "help": "comma list of pipeline/trigger names this may hand off to"},
        {"name": "min_confidence", "type": "number", "default": 0.5,
         "help": "only pivot when LLM confidence >= this (0-1)"},
        {"name": "allow_continue", "type": "bool", "default": True,
         "help": "allow 'continue this dream' as an outcome alongside pivots"},
        {"name": "max_pivots", "type": "int", "default": 3,
         "help": "hard cap on chained hand-offs"},
    ],
)
_register_stage(
    "dream.stage.iterate", "Iterate / continue",
    "Decides whether the dream is complete or should run another iteration. The "
    "LLM judges satisfaction and chooses the next step, refined goals, relevant "
    "sensors and the completion threshold. Continuation re-runs this same "
    "pipeline carrying its journal. Place LAST (after deliver) so each iteration "
    "is delivered before the next is decided.",
    "dream.stage.iterate", phase="emit", optional=True,
    params=[
        {"name": "basis", "type": "str", "default": "satisfaction",
         "help": "comma list: satisfaction,runtime,user_activity,sensors"},
        {"name": "max_iterations", "type": "int", "default": 3,
         "help": "hard cap on continuations"},
        {"name": "satisfaction_target", "type": "number", "default": 0.8,
         "help": "stop when LLM satisfaction >= this (0-1)"},
        {"name": "max_runtime_s", "type": "int", "default": 0,
         "help": "stop after this many seconds total (0 = no limit)"},
        {"name": "min_idle_minutes", "type": "int", "default": 0,
         "help": "stop if the user is active (idle < this); 0 = ignore"},
        {"name": "llm_decides", "type": "bool", "default": True,
         "help": "let the LLM judge completion + plan next step"},
        {"name": "apply_goals", "type": "bool", "default": True,
         "help": "persist the LLM's refined goals back to the pipeline"},
        {"name": "apply_sensors", "type": "bool", "default": False,
         "help": "adopt the sensors the LLM flags as relevant"},
    ],
)


# ─────────────────────────────────────────────────────────────────────────────
# PANEL HELPER CAPS — search proxies for the curate/whitelist UIs
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.caps.search", memory="off", silent=True,
    http_method="GET", http_path="/dream/caps/search", http_tags=["dream"],
    description="Search registered capabilities by name or description. "
                "Returns a grouped list for the whitelist/curate picker.",
)
async def dream_caps_search(query: str = "", limit: int = 100, trace_id=None):
    q = (query or "").lower()
    results: List[Dict[str, Any]] = []
    for name, cap in CAPABILITY_REGISTRY.items():
        if q and q not in name.lower() and q not in (cap.get("description") or "").lower():
            continue
        results.append({
            "name":  name,
            "group": name.split(".")[0],
            "desc":  (cap.get("description") or "")[:120],
        })
        if len(results) >= int(limit):
            break
    # Group by prefix
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        groups.setdefault(r["group"], []).append(r)
    return {"caps": results, "groups": groups, "count": len(results)}


@capability(
    "dream.memory.browse", memory="off", silent=True,
    http_method="POST", http_path="/dream/memory/browse", http_tags=["dream"],
    description="Search memory for the curate picker. Returns simplified results "
                "with id, text preview, category, tags, and timestamp.",
)
async def dream_memory_browse(
    query: str = "", limit: int = 20,
    category: str = "", record_type: str = "",
    trace_id=None,
):
    cap = CAPABILITY_REGISTRY.get("memory.search")
    if not cap:
        return {"results": [], "error": "memory.search not available"}
    try:
        kwargs: Dict[str, Any] = {"query": query or "", "limit": int(limit)}
        if category:    kwargs["category"] = category
        if record_type: kwargs["record_type"] = record_type
        raw = await cap["func"](**kwargs)
        items = []
        for item in (raw or {}).get("results", [])[:int(limit)]:
            rec = item.get("record", item) if isinstance(item, dict) else {}
            items.append({
                "id":       rec.get("id", ""),
                "text":     (rec.get("text") or rec.get("summary") or "")[:300],
                "category": rec.get("category", ""),
                "type":     rec.get("record_type", ""),
                "tags":     rec.get("tags", []),
                "ts":       (rec.get("created_at") or "")[:19],
                "score":    round(item.get("score", 0), 3) if isinstance(item, dict) else 0,
            })
        return {"results": items, "count": len(items), "query": query}
    except Exception as e:
        return {"results": [], "error": str(e)}


@capability(
    "dream.fabric.browse", memory="off", silent=True,
    http_method="POST", http_path="/dream/fabric/browse", http_tags=["dream"],
    description="Search the data fabric for the curate picker. Returns simplified results "
                "with id, text preview, dataset, tags, and timestamp.",
)
async def dream_fabric_browse(
    query: str = "", dataset_id: str = "",
    limit: int = 20, trace_id=None,
):
    cap = CAPABILITY_REGISTRY.get("fabric.query")
    if not cap:
        return {"results": [], "error": "fabric.query not available"}
    dsl: Dict[str, Any] = {"top_k": int(limit), "include_data": False, "cache": False}
    if query:      dsl["text"] = query
    if dataset_id: dsl["dataset_id"] = dataset_id
    try:
        raw = await cap["func"](query=json.dumps(dsl))
        items = []
        for r in (raw or {}).get("results", [])[:int(limit)]:
            items.append({
                "id":      r.get("id", ""),
                "text":    (r.get("text") or "")[:300],
                "dataset": r.get("dataset_id", ""),
                "tags":    r.get("tags", []),
                "ts":      (r.get("created_at") or "")[:19],
                "score":   round(r.get("score", 0), 3),
            })
        return {"results": items, "count": len(items), "query": query}
    except Exception as e:
        return {"results": [], "error": str(e)}


@capability(
    "dream.fabric.datasets", memory="off", silent=True,
    http_method="GET", http_path="/dream/fabric/datasets", http_tags=["dream"],
    description="List available fabric datasets for sensor configuration.",
)
async def dream_fabric_datasets(trace_id=None):
    cap = CAPABILITY_REGISTRY.get("fabric.datasets")
    if not cap:
        return {"datasets": [], "error": "fabric.datasets not available"}
    try:
        raw = await cap["func"]()
        datasets = []
        for d in (raw or {}).get("datasets", []):
            datasets.append({
                "id":    d.get("dataset_id", ""),
                "count": d.get("record_count", 0),
                "label": d.get("label", d.get("dataset_id", "")),
            })
        return {"datasets": datasets, "count": len(datasets)}
    except Exception as e:
        return {"datasets": [], "error": str(e)}

@APP.get("/dream/panel", include_in_schema=False)
async def _research_panel():
    from fastapi.responses import HTMLResponse
    p = _HERE / "dream_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>dream_panel.html not found</p>")


@APP.get("/dream/review/panel", include_in_schema=False)
async def _review_panel():
    from fastapi.responses import HTMLResponse
    p = _HERE / "dream_review_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>dream_review_panel.html not found</p>")


# ─────────────────────────────────────────────────────────────────────────────
# PANEL
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dream.panel.html", memory="off", silent=True,
    http_method="GET", http_path="/dream/panel", http_tags=["dream", "ui"],
    description="Serve the Dream panel HTML.",
)
async def dream_panel_html(trace_id=None):
    try:
        html = _PANEL_HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = (
            "<!DOCTYPE html><html><body style='background:#0d0f12;color:#ef4444;"
            "font-family:monospace;padding:40px'>"
            "<h2>dream_panel.html not found</h2>"
            f"<p>Expected at: {_PANEL_HTML_PATH}</p>"
            "<p>Place dream_panel.html alongside dream_capabilities.py</p>"
            "</body></html>"
        )
    return HTMLResponse(html)


register_ui(
    "dream-panel",
    "Dream",
    "☾",
    """<div id="dream-panel-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/dream/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=[
        "dream.scheduler.start", "dream.scheduler.stop", "dream.scheduler.status",
        "dream.cycle.run", "dream.cycle.cancel",
        "dream.cycle.continue",
        "dream.preview", "dream.preview.last",
        "dream.trigger.list", "dream.trigger.get", "dream.trigger.upsert",
        "dream.trigger.delete", "dream.trigger.toggle", "dream.trigger.generate",
        "dream.whitelist.list", "dream.whitelist.set",
        "dream.config.get", "dream.config.set",
        "dream.loop.settings.get", "dream.loop.settings.set",
        "dream.journal.read", "dream.journal.list", "dream.journal.clear",
        "dream.pipeline.list", "dream.pipeline.get", "dream.pipeline.upsert",
        "dream.pipeline.delete", "dream.pipeline.run",
        "dream.review.run", "dream.review.styles", "dream.review.areas",
        "dream.review.list", "dream.review.get", "dream.review.area_report",
        "dream.review.clear", "dream.review.status", "dream.review.source",
        "dream.stage.deep_review",
        "dream.stage.review_codebase", "dream.stage.review_report",
        "dream.stage.snapshot_source", "dream.stage.ide_workspace_act",
        "dream.history", "dream.last",
        "dream.cycle.detail",
        "dream.hitl.pending", "dream.hitl.respond", "dream.hitl.clear",
        "dream.llm.tokens",
        "dream.sensors.list", "dream.stages.list",
        "dream.director.assess", "dream.timeline",
        "dream.stage.stepwise_execute",    # compat alias
        "dream.stage.investigate",          # compat alias (now wraps agent_loop_v2)
        "dream.stage.agent_loop",           # preferred new entry point
        "dream.stage.goal_refine",          # Phase 1: actionable goal refinement
        "dream.templates.list", "dream.templates.apply",  # Phase 2: pipeline templates
        "dream.caps.search", "dream.memory.browse",
        "dream.fabric.browse", "dream.fabric.datasets",
        "dream.sensor.custom.list", "dream.sensor.custom.create",
        "dream.sensor.custom.delete", "dream.sensor.custom.run",
        "dream.stage.custom.list", "dream.stage.custom.create",
        "dream.stage.custom.delete",
        # DAG Workshop caps — used by the new agent-loop integration
        "dag.agent_loop_v2", "dag.agent_loop",
        "dag.plan", "dag.run", "dag.plan_and_run",
        "workshop.cap_tree",
    ],
    mode="tab",
    tab_order=78,
)


# The Source Review panel is injected INTO the dream panel (sidebar section),
# not registered as a separate top-level tab. It is served at /dream/review/panel.


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP — seed defaults, merge new triggers/whitelist, auto-start scheduler
# ─────────────────────────────────────────────────────────────────────────────

# Track which default trigger names + whitelist entries the code defines.
# On startup we merge: new defaults get added, existing user customizations
# are preserved. A version key in Redis records the last-merged set so we
# can detect genuinely new additions across code updates.

KEY_SEEDED_TRIGGERS  = "vera:dream:seeded_trigger_names"   # Redis set
KEY_SEEDED_WHITELIST = "vera:dream:seeded_whitelist_caps"   # Redis set
KEY_SEEDED_ITERATE   = "vera:dream:backfilled_iterate"      # Redis set


async def _startup():
    for _ in range(20):
        if _redis() is not None:
            break
        await asyncio.sleep(0.5)

    r = _redis()
    if not r:
        log.warning("dream startup: redis not available, skipping seed")
        return

    try:
        # ── Triggers: smart merge ────────────────────────────────────────
        # Strategy: keep a Redis set of trigger names we've already seeded.
        # On every startup, any trigger in _default_triggers() whose name
        # is NOT in that set gets upserted (new addition). Triggers the user
        # has already customized are left untouched.
        existing_count = await r.hlen(KEY_TRIGGERS)
        if not existing_count:
            # Fresh install — seed everything
            for trig in _default_triggers():
                await _save_trigger(trig)
            names = [t["name"] for t in _default_triggers()]
            if names:
                await r.sadd(KEY_SEEDED_TRIGGERS, *names)
            log.info("dream seeded %d default triggers", len(_default_triggers()))
        else:
            # Existing install — merge only genuinely new triggers
            try:
                already_seeded = await r.smembers(KEY_SEEDED_TRIGGERS)
                seeded_names = {
                    (n.decode() if isinstance(n, bytes) else str(n))
                    for n in (already_seeded or set())
                }
            except Exception:
                seeded_names = set()

            # Also check what's actually in Redis (user may have deleted some)
            try:
                existing_raw = await r.hkeys(KEY_TRIGGERS)
                existing_names = {
                    (k.decode() if isinstance(k, bytes) else str(k))
                    for k in (existing_raw or [])
                }
            except Exception:
                existing_names = set()

            merged = 0
            for trig in _default_triggers():
                name = trig["name"]
                if name not in seeded_names and name not in existing_names:
                    # Genuinely new trigger from a code update — add it
                    await _save_trigger(trig)
                    merged += 1
                    log.info("dream: merged new trigger '%s'", name)

            # Record all default names so we don't re-merge next time
            all_default_names = [t["name"] for t in _default_triggers()]
            if all_default_names:
                await r.sadd(KEY_SEEDED_TRIGGERS, *all_default_names)

            if merged:
                log.info("dream: merged %d new triggers into existing set", merged)

        # ── Targeted upgrade: migrate the old inline source_review trigger ──
        # to the composite pipeline_ref version so the rebuilt review pipeline
        # takes effect for existing installs (preserves enabled/schedule).
        try:
            cur = await _get_trigger("source_review")
            if cur and not cur.get("pipeline_ref"):
                new_def = next((t for t in _default_triggers()
                                if t["name"] == "source_review"), None)
                if new_def:
                    upgraded = dict(new_def)
                    # keep user's enabled state + schedule tweaks
                    for k in ("enabled", "hours_start", "hours_end",
                              "min_idle_minutes", "min_interval_minutes"):
                        if k in cur:
                            upgraded[k] = cur[k]
                    upgraded.pop("pipeline", None)   # drop stale inline pipeline
                    await _save_trigger(upgraded)
                    log.info("dream: upgraded source_review trigger to pipeline_ref")
        except Exception as e:
            log.debug("dream upgrade source_review: %s", e)

        # ── Whitelist: smart merge ───────────────────────────────────────
        # Same strategy: track what we've seeded, add only new entries.
        wl_count = await r.scard(KEY_WHITELIST)
        if not wl_count:
            # Fresh install
            await _set_whitelist(DEFAULT_WHITELIST)
            if DEFAULT_WHITELIST:
                await r.sadd(KEY_SEEDED_WHITELIST, *DEFAULT_WHITELIST)
            log.info("dream seeded default whitelist (%d caps)", len(DEFAULT_WHITELIST))
        else:
            # Merge new whitelist entries
            try:
                already_seeded_wl = await r.smembers(KEY_SEEDED_WHITELIST)
                seeded_wl = {
                    (c.decode() if isinstance(c, bytes) else str(c))
                    for c in (already_seeded_wl or set())
                }
            except Exception:
                seeded_wl = set()

            current_wl = set()
            try:
                items = await r.smembers(KEY_WHITELIST)
                current_wl = {
                    (i.decode() if isinstance(i, bytes) else str(i))
                    for i in (items or set())
                }
            except Exception:
                pass

            new_caps = [
                c for c in DEFAULT_WHITELIST
                if c not in seeded_wl and c not in current_wl
            ]
            if new_caps:
                await r.sadd(KEY_WHITELIST, *new_caps)
                log.info("dream: merged %d new caps into whitelist", len(new_caps))

            # Record all defaults
            if DEFAULT_WHITELIST:
                await r.sadd(KEY_SEEDED_WHITELIST, *DEFAULT_WHITELIST)

        # ── Composite pipelines: seed built-ins (create-if-absent) ──────────
        try:
            for p in _builtin_pipelines():
                if not await r.hexists(KEY_PIPELINES, p["name"]):
                    await _save_pipeline(p)
            log.info("dream: ensured %d built-in pipelines", len(_builtin_pipelines()))
        except Exception as e:
            log.debug("dream seed pipelines: %s", e)

        # ── Backfill continue/iterate into existing pipelines ───────────────
        # Append dream.stage.iterate (emit, last) to any trigger that has
        # continuation intent — standing goals, an iterate config, or pivot
        # enabled — and doesn't already have it. Tracked per-name in a seeded
        # set so a user who later removes it isn't re-backfilled.
        try:
            seeded_iter = set()
            try:
                s = await r.smembers(KEY_SEEDED_ITERATE)
                seeded_iter = {(n.decode() if isinstance(n, bytes) else str(n))
                               for n in (s or set())}
            except Exception:
                pass
            backfilled = 0
            for t in await _list_triggers():
                name = t.get("name", "")
                if not name or name in seeded_iter:
                    continue
                pipe = list(t.get("pipeline") or [])
                wants = bool(t.get("goals")
                             or (t.get("iterate") or {}).get("enabled")
                             or (t.get("pivot") or {}).get("enabled"))
                if pipe and wants and "dream.stage.iterate" not in pipe:
                    pipe.append("dream.stage.iterate")
                    t["pipeline"] = pipe
                    # seed a sane default iterate config if none present
                    t.setdefault("stage_config", {})
                    if "iterate" not in t["stage_config"]:
                        t["stage_config"]["iterate"] = {
                            "basis": ["satisfaction", "user_activity"],
                            "max_iterations": int(t.get("max_continuation_depth", 3) or 3),
                            "satisfaction_target": 0.8,
                            "min_idle_minutes": int(t.get("min_idle_minutes", 0) or 0),
                            "llm_decides": True, "apply_goals": True,
                        }
                    await _save_trigger(t)
                    backfilled += 1
                await r.sadd(KEY_SEEDED_ITERATE, name)
            if backfilled:
                log.info("dream: backfilled iterate stage into %d pipeline(s)", backfilled)
        except Exception as e:
            log.debug("dream backfill iterate: %s", e)

        # Reload custom sensors from Redis into the in-memory SENSOR_REGISTRY
        try:
            items = await r.hgetall(KEY_CUSTOM_SENSORS)
            loaded = 0
            for _, v in (items or {}).items():
                try:
                    rec = json.loads(v.decode() if isinstance(v, bytes) else v)
                    sid = rec.get("id", "")
                    if sid and sid not in SENSOR_REGISTRY:
                        SENSOR_REGISTRY[sid] = {
                            "id":          sid,
                            "label":       rec.get("label", sid),
                            "description": rec.get("description", ""),
                            "cap":         "dream.sensor.custom.run",
                            "custom":      True,
                            "params":      [
                                {"name": "sensor_id", "type": "str",
                                 "default": sid, "help": "auto-filled"},
                            ],
                        }
                        loaded += 1
                except Exception:
                    continue
            if loaded:
                log.info("dream: reloaded %d custom sensors from Redis", loaded)
        except Exception as e:
            log.debug("dream: custom sensor reload: %s", e)

        # Reload custom stages from Redis
        try:
            items = await r.hgetall(KEY_CUSTOM_STAGES)
            loaded = 0
            for _, v in (items or {}).items():
                try:
                    rec = json.loads(v.decode() if isinstance(v, bytes) else v)
                    sid = rec.get("id", "")
                    if sid and sid not in STAGE_REGISTRY:
                        # Re-create the stage via the create cap
                        await dream_stage_custom_create(
                            name=rec.get("name", ""),
                            label=rec.get("label", ""),
                            description=rec.get("description", ""),
                            phase=rec.get("phase", "analyze"),
                            source_type=rec.get("source_type", "cap"),
                            source_cap=rec.get("source_cap", ""),
                            source_dag=json.dumps(rec.get("source_dag", [])),
                            default_params=json.dumps(rec.get("default_params", {})),
                        )
                        loaded += 1
                except Exception:
                    continue
            if loaded:
                log.info("dream: reloaded %d custom stages from Redis", loaded)
        except Exception as e:
            log.debug("dream: custom stage reload: %s", e)

        cfg = await _get_config()
        if cfg.get("enabled", True):
            global _SCHED_RUN, _SCHED_TASK
            if not _SCHED_RUN:
                _SCHED_RUN = True
                _SCHED_TASK = asyncio.create_task(_scheduler_loop())
                log.info("dream scheduler auto-started")
    except Exception as e:
        log.warning("dream startup: %s", e)


schedule(_startup, interval=999999, name="dream_startup")