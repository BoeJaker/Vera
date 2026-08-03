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
import contextvars
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

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP,
    CAPABILITY_REGISTRY,
    capability,
    emit_event,
    is_dev_sandbox,
    now_iso,
    register_ui,
    schedule,
)
# Shared output-format registry — single source of truth for REVIEW_STYLES
# (previously defined inline in this module; chat and other callers reuse it).
from Vera.vera.output_formats import REVIEW_STYLES
# Shared delivery-channel registry — the routing twin of output_formats. The
# deliver stage loops these instead of a hard-coded telegram/memory/notebook
# ladder, so email/chat and skill-defined channels work without editing it.
from Vera.vera import delivery as _delivery

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
KEY_MEM_LAST_NODE = "vera:dream:mem:last_node:"   # + journal_id -> last dream-layer cycle memory id
KEY_PROGRESS      = "vera:dream:progress:"        # + cycle_id -> live progress snapshot (JSON)

# Per-cycle output workspace — every cycle collates its working material into
# real files here (gather data, findings, plan, report) instead of holding it
# all in the LLM context window. Files survive the cycle and are listed in the
# history record + cycle detail, downloadable via dream.cycle.file.
OUTPUT_ROOT = _HERE / "outputs"

# Global default agent-loop settings — mirrors the dag.agent_loop_v2 surface so
# dream loops can be tuned exactly like the DAG Workshop loop. Resolution order
# (low→high): these defaults < global override (Redis) < per-trigger/pipeline
# (trig["loop_settings"]) < project (seed["project_loop_settings"]) < seed.
DEFAULT_LOOP_SETTINGS: Dict[str, Any] = {
    # Engine selection — which dag.agent_loop variant dream stages delegate to.
    # v5 (orchestrator + scoped specialist sub-agents) is the default; the shared
    # _resolve_agent_loop_cap falls back v5 → v2 → v1 if a variant isn't loaded.
    "loop_version":              "v5",
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
    # ── v5-only knobs (ignored by older variants via _loop_kwargs_for signature
    #    filtering, so they're harmless when loop_version != v5). ──────────────
    "max_steps":                 6,
    "step_cycle_budget":         6,
    "catalog_size":              40,
    "enable_replan":             True,
    "enable_dynamic_skills":     True,
    "skill_allow":               "",
    "skill_deny":                "",
    "auto_suggest_skills":       True,
    "enable_recon":              True,
    "recon_max_rounds":          3,
    "enable_subplans":           True,
    "enable_phases":             True,
    "enable_master_planner":     True,
    "enable_code_autosave":      True,
    "code_push_gitea":           False,
    "handover":                  False,
    "handover_max_chars":        20000,
    # ── Triage / context (v2–v4; v5 ignores via signature filtering) ──────────
    "triage_category":           "",
    "triage_keywords":           "",
    "base_toolkit":              "",
    "attach_skills":             "",
    "attach_ontologies":         "",
    # ── Phase cadence + continuation (v2–v4) ─────────────────────────────────
    "phased":                    True,
    "min_explore_cycles":        2,
    "require_validate":          True,
    "long_running_force_hitl":   True,
    "allow_continue":            True,
    "continue_increment":        8,
    "auto_continue_max":         0,
    # ── Human-in-the-loop (v3/v4) ────────────────────────────────────────────
    "require_approval":          False,
    "hitl_timeout_secs":         300,
    # ── Step pipeline (v4) ───────────────────────────────────────────────────
    "enabled_steps":             "plan,explore,think,act,verify",
    "select_steps":              True,
    "require_verify":            True,
    "strict_complete":           True,
    "prefer_terminal_tools":     True,
    "long_running_caps":         "",
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
# — and are also stripped from the RECENT ACTIVITY the dream director sees, so
# high-frequency read-only telemetry/status polls don't masquerade as real work
# (the source of the "idle 99999m but activity 0m ago" phantom-urgency spiral).
# Only READ-ONLY status/telemetry namespaces belong here; actionable caps
# (e.g. jobs.recover_now / jobs.purge_pending) are intentionally left OUT so
# genuine job actions still register as activity.
_IDLE_IGNORE_PREFIXES = (
    "dream.", "obs.", "health.", "ui.", "syslog.", "tg.events.status",
    "cluster.", "ollama.", "heartbeat", "echo", "caps.", "mcp.",
    # read-only job/monitoring telemetry — pure self-surveillance polling noise
    "jobs.history", "jobs.stats", "jobs.ollama_log", "jobs.running_at_boot",
    "jobs.list", "jobs.get", "jobs.status", "sysmon.", "sysinfo.", "metrics.",
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
    # When True, idle-reset triggers default to whatever the cap-tracking
    # system currently tracks (group granularity). The moment the user edits
    # idle_reset_prefixes explicitly, dream_config_set flips this to False so
    # their list becomes an independent override. See _effective_idle_reset_prefixes.
    "idle_reset_follow_tracking": True,
}


def _tracked_reset_prefixes() -> Optional[List[str]]:
    """
    Pull the effective idle-reset prefixes from the cap-tracking system, if it
    is importable. Returns None when unavailable so callers can fall back to
    the static default. Decoupled via lazy import — dream never hard-depends on
    cap_tracking.
    """
    try:
        from Vera.vera.capabilities import cap_tracking
        prefixes = cap_tracking.tracked_group_prefixes()
        return prefixes or None
    except Exception:
        return None


def _effective_idle_reset_prefixes(cfg: Dict[str, Any]) -> List[str]:
    """
    Resolve which cap prefixes reset the idle timer.

      • follow mode (default): derive live from cap-tracking's tracked groups,
        falling back to the user's saved list, then the static default.
      • override mode: the user has amended the list — use it verbatim.
    """
    if cfg.get("idle_reset_follow_tracking", True):
        tracked = _tracked_reset_prefixes()
        if tracked:
            return tracked
    return cfg.get("idle_reset_prefixes") or DEFAULT_IDLE_RESET_PREFIXES

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
    "project.list", "project.get", "project.context", "project.context.assemble",
    # Composite-topics entity sources: schedule + review reports
    "cal.events.list", "cal.todos.list", "cal.notes.list",
    "ide.fs.read", "dream.review.area_report",
    # Sensors + stages
    "dream.sensor.memory_recent", "dream.sensor.fabric_recent",
    "dream.sensor.syslog_errors", "dream.sensor.bus_events",
    "dream.sensor.news_overnight", "dream.sensor.research_recent",
    "dream.sensor.active_projects", "dream.sensor.source_changes",
    "dream.sensor.memory_graph_walk", "dream.sensor.topics",
    "dream.stage.gather", "dream.stage.themes", "dream.stage.synthesize",
    "dream.stage.goal_refine", "dream.stage.compose_topics",
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
            "label":        "Topic Wander",
            "description":  "Composite-topic exploration — harvests interesting topics "
                            "(projects, source changes, research, schedule, errors, memory) "
                            "with their entities, picks one, and uses the agent loop to "
                            "think about it and wander to related things.",
            "enabled":      True,
            "sensors":      ["dream.sensor.topics",
                             "dream.sensor.memory_graph_walk"],
            "pipeline":     ["dream.stage.gather",
                             "dream.stage.compose_topics",
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
            "stage_config": {"compose_topics": {"top_n": 6, "per_topic_entities": 6}},
            "whitelist": [
                "memory.search", "memory.recall", "memory.similar",
                "memory.traverse", "memory.all_nodes", "memory.create",
                "memory.graph_stats",
                "fabric.query", "fabric.datasets", "fabric.sources",
                "fabric.entity_graph.snapshot",
                "project.list", "project.get", "project.context.assemble",
                "research.history", "research.job.status", "research.expand",
                "research.quick_search",
                "cal.events.list", "cal.todos.list", "cal.notes.list",
                "ide.inspect.diff_snapshot", "ide.inspect.review_file",
                "ide.fs.read", "dream.review.area_report",
                "nlp.run", "nlp.modules",
                "llm.generate", "llm.summarize", "llm.qa",
            ],
            "no_hitl_caps": [
                "memory.search", "memory.recall", "memory.similar",
                "memory.traverse", "memory.all_nodes", "memory.graph_stats",
                "fabric.query", "fabric.datasets", "fabric.sources",
                "project.list", "project.get", "project.context.assemble",
                "research.history", "research.job.status", "research.expand",
                "cal.events.list", "cal.todos.list", "cal.notes.list",
                "ide.inspect.diff_snapshot", "ide.inspect.review_file", "ide.fs.read",
                "dream.review.area_report",
                "nlp.run", "llm.summarize", "llm.qa",
            ],
            "prompt": (
                "You are wandering Vera's most interesting topics. compose_topics has "
                "already chosen ONE focus topic and listed its known entities + ways to "
                "go deeper (ids and pull hints). Build the complete picture with MINIMAL "
                "calls — prefer the given ids/hints over broad searches. Then produce a "
                "concrete, specific insight or next action grounded in real data. For a "
                "source-change topic, outline how you'd implement the change. You may "
                "wander to an adjacent topic if it proves more relevant. Never invent "
                "data; cite the entity ids you used."
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
        # ── DAILY REPORT DREAMS ─────────────────────────────────────────────
        # Collector-based (see dream.stage.gather): sensors are firing gates,
        # `collect` cap calls provide the actual content. Each produces a real
        # markdown report file (report.md in the cycle's output workspace) and
        # delivers to notebook + memory; add podcast/telegram/email per taste
        # in the trigger editor's deliver row.
        {
            "name":         "daily_ai_report",
            "label":        "Daily AI & ML Report",
            "description":  "Morning report on the wider AI/ML world: releases, "
                            "research, industry moves — from live web search.",
            "enabled":      True,
            "sensors":      [],
            "collect": [
                {"cap": "web.search", "label": "AI news",
                 "args": {"query": "artificial intelligence news today", "limit": 10}},
                {"cap": "web.search", "label": "ML research",
                 "args": {"query": "machine learning research breakthrough paper", "limit": 8}},
                {"cap": "web.search", "label": "Model releases",
                 "args": {"query": "new LLM model release announcement", "limit": 8}},
            ],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  6, "hours_end": 11,
            "min_idle_minutes":    10,
            "min_interval_minutes": 1080,
            "require_signal":       0.05,
            "depth":        "deep",
            "deliver_to":   ["notebook", "memory"],
            "prompt": (
                "Write today's AI & ML briefing from the search results above. "
                "Structure: ## Top stories (3-5, each with why it matters), "
                "## Releases & tools, ## Research worth reading, ## One-line radar "
                "(short bullets). Include source URLs inline as markdown links. "
                "Ground every item in an actual search result — never invent "
                "stories. If results are thin, produce a shorter honest report."
            ),
        },
        {
            "name":         "daily_local_ai_report",
            "label":        "Daily Local AI Report",
            "description":  "Self-hosted / local AI: open-weight releases, "
                            "llama.cpp & ollama ecosystem, quantisation, hardware.",
            "enabled":      True,
            "sensors":      [],
            "collect": [
                {"cap": "web.search", "label": "Local LLM news",
                 "args": {"query": "local LLM news llama.cpp ollama vllm", "limit": 10}},
                {"cap": "web.search", "label": "Open weights",
                 "args": {"query": "new open source model weights release huggingface", "limit": 8}},
                {"cap": "web.search", "label": "LocalLLaMA highlights",
                 "args": {"query": "reddit LocalLLaMA best new model quantization", "limit": 8}},
            ],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  6, "hours_end": 12,
            "min_idle_minutes":    10,
            "min_interval_minutes": 1080,
            "require_signal":       0.05,
            "depth":        "deep",
            "deliver_to":   ["notebook", "memory"],
            "prompt": (
                "Write today's LOCAL AI briefing for a self-hoster running an "
                "ollama cluster with mixed CPU nodes and a V100 GPU. From the "
                "search results: ## New open models & quants (sizes, licences, "
                "what they're good at), ## Runtime & tooling (llama.cpp, ollama, "
                "vllm, exllama updates), ## Hardware & performance notes, "
                "## Relevance to this homelab (which items are worth trying here "
                "and why). Include source links. Ground everything in the "
                "results — no invented items."
            ),
        },
        {
            "name":         "daily_homelab_report",
            "label":        "Daily Homelab News",
            "description":  "Homelab & self-hosting: software releases, Proxmox/"
                            "Docker/k8s updates, community highlights.",
            "enabled":      True,
            "sensors":      [],
            "collect": [
                {"cap": "web.search", "label": "Homelab news",
                 "args": {"query": "homelab self-hosted news this week", "limit": 10}},
                {"cap": "web.search", "label": "Infra releases",
                 "args": {"query": "proxmox docker release update announcement", "limit": 8}},
                {"cap": "web.search", "label": "Selfhosted apps",
                 "args": {"query": "best new self-hosted apps release", "limit": 6}},
            ],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  7, "hours_end": 12,
            "min_idle_minutes":    10,
            "min_interval_minutes": 1080,
            "require_signal":       0.05,
            "depth":        "standard",
            "deliver_to":   ["notebook", "memory"],
            "prompt": (
                "Write today's homelab briefing from the search results: "
                "## Releases & updates (Proxmox, Docker, k8s, storage, network "
                "tooling), ## New self-hosted apps worth a look, ## Community "
                "highlights, ## Applicable here (one or two concrete suggestions "
                "for THIS homelab — Proxmox cluster + Docker services). Include "
                "source links; ground everything in the results."
            ),
        },
        {
            "name":         "security_watch",
            "label":        "Security Watch (daily)",
            "description":  "Daily security sweep — CVEs, breaches, advisories. "
                            "Accretes into this trigger's journal + fabric so the "
                            "weekly_security_digest can synthesise the week.",
            "enabled":      True,
            "sensors":      [],
            "collect": [
                {"cap": "web.search", "label": "Exploited CVEs",
                 "args": {"query": "critical vulnerability CVE actively exploited", "limit": 10}},
                {"cap": "web.search", "label": "Breach news",
                 "args": {"query": "security breach ransomware campaign news today", "limit": 8}},
                {"cap": "web.search", "label": "Infra advisories",
                 "args": {"query": "linux docker proxmox vulnerability advisory", "limit": 8}},
            ],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  0, "hours_end": 24,
            "min_idle_minutes":    10,
            "min_interval_minutes": 1080,
            "require_signal":       0.05,
            "depth":        "standard",
            "deliver_to":   ["memory"],
            "prompt": (
                "You are building the day's security ledger (a weekly digest "
                "will synthesise these). Extract CONCRETE items only, as terse "
                "bullets: `- CVE-XXXX-NNNN — product — severity — status "
                "(exploited?) — action`. Then breaches/campaigns in one line "
                "each. Flag anything touching a homelab stack (Linux, Docker, "
                "Proxmox, Redis, Postgres, Ollama, WireGuard, code-server) with "
                "**[STACK]**. Facts only from the results; no filler prose."
            ),
        },
        {
            "name":         "weekly_security_digest",
            "label":        "Weekly Security Digest",
            "description":  "Synthesises the week of security_watch accretions "
                            "(journal + fabric) into one digest with actions.",
            "enabled":      True,
            "sensors":      [],
            "collect": [
                {"cap": "dream.journal.read", "label": "Week's watch journal",
                 "args": {"journal_id": "trigger:security_watch", "limit": 250},
                 "max_items": 120},
                {"cap": "dream.sensor.fabric_by_tag", "label": "Daily ledgers",
                 "args": {"tags": "security_watch", "limit": 40}, "max_items": 40},
                {"cap": "dream.history", "label": "Watch reports",
                 "args": {"trigger": "security_watch", "limit": 8}, "max_items": 8},
            ],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  8, "hours_end": 20,
            "min_idle_minutes":    15,
            "min_interval_minutes": 10020,
            "require_signal":       0.05,
            "depth":        "deep",
            "deliver_to":   ["notebook", "memory"],
            "prompt": (
                "Write the WEEKLY security digest from the accumulated daily "
                "ledgers above. Structure: ## Executive summary (3 lines), "
                "## Critical & exploited (deduplicated CVE table: id, product, "
                "severity, status), ## This homelab (every [STACK] item, with a "
                "concrete action each — patch, config change, or 'verify not "
                "exposed'), ## Notable breaches & campaigns, ## Watchlist for "
                "next week. Deduplicate repeats across days; keep only what the "
                "ledgers actually contain."
            ),
        },
        {
            "name":         "daily_ops_report",
            "label":        "Daily Operations Report",
            "description":  "Detailed self-report on Vera's own operations: dreams "
                            "run + outcomes + durations, errors, capability usage, "
                            "director activity, deliveries.",
            "enabled":      True,
            "sensors":      [],
            "collect": [
                {"cap": "dream.history", "label": "Dream cycles",
                 "args": {"limit": 30}, "max_items": 30},
                {"cap": "dream.sensor.syslog_errors", "label": "System errors",
                 "args": {"limit": 60}, "max_items": 60},
                {"cap": "dream.sensor.cap_calls", "label": "Capability activity",
                 "args": {"limit": 100}, "max_items": 60},
                {"cap": "dream.sensor.bus_events", "label": "Event bus",
                 "args": {"limit": 80}, "max_items": 50},
                {"cap": "dream.scheduler.status", "label": "Scheduler",
                 "args": {}},
                {"cap": "dream.director.journal", "label": "Director thoughts",
                 "args": {"limit": 20}, "max_items": 20},
            ],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  17, "hours_end": 23,
            "min_idle_minutes":    10,
            "min_interval_minutes": 1080,
            "require_signal":       0.05,
            "depth":        "exhaustive",
            "deliver_to":   ["notebook", "memory"],
            "prompt": (
                "Write Vera's daily OPERATIONS report — a detailed, honest "
                "self-review of the last 24h grounded ONLY in the data above. "
                "Structure: ## Summary scoreboard (dreams run / succeeded / "
                "early-exited / cancelled, avg duration, deliveries by channel), "
                "## Dream-by-dream (each cycle: trigger, duration, outcome, was "
                "the output actually useful?), ## Errors & anomalies (group "
                "syslog errors, call out recurring ones with counts), "
                "## Capability usage patterns (what ran most, anything failing), "
                "## Director activity (what it thought about / queued), "
                "## Self-assessment & tuning suggestions (3-5 concrete, e.g. "
                "'trigger X early-exits every day — lower its require_signal or "
                "fix its collectors'). Be specific with numbers and names."
            ),
        },
        # ── LOOP LAB — nightly agentic-loop QA + self-evolution ─────────────
        # Runs the benchmark suite (evolve.suite.run does the actual work as a
        # collector cap call), then reports the scoreboard + regressions. This
        # is the automated testing surface for the loops and other systems.
        {
            "name":         "loop_eval_nightly",
            "label":        "Loop Lab — Nightly QA",
            "description":  "Runs the agentic-loop benchmark suite (loop goals + "
                            "cap smoke tests) with critic assessment, then reports "
                            "the scoreboard, trend and any regressions.",
            "enabled":      True,
            "sensors":      [],
            "collect": [
                {"cap": "evolve.suite.run",
                 "args": {"tag": "core", "assess": True}, "label": "Run suite"},
                {"cap": "evolve.report", "args": {}, "label": "Scoreboard",
                 "max_items": 4},
            ],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  2, "hours_end": 6,
            "min_idle_minutes":    30,
            "min_interval_minutes": 1200,
            "require_signal":       0.0,
            "depth":        "deep",
            "deliver_to":   ["notebook", "memory"],
            "prompt": (
                "Summarise the Loop Lab benchmark suite that just ran (the "
                "Scoreboard collector holds the full report). Lead with the "
                "average combined score and whether it improved or regressed vs "
                "recent runs. Call out any task that failed its checks or scored "
                "below 6, and any regression. End with one recommendation: run an "
                "improve session on the weakest profile (evolve.improve.start), "
                "or note the loops are healthy. Ground everything in the report."
            ),
        },
        {
            "name":         "markets_evolve_nightly",
            "label":        "Markets — Nightly Self-Improve",
            "description":  "Runs one markets self-improvement iteration (backtest "
                            "sweeps → accept better strategies) and reports what "
                            "changed on the strategy leaderboard.",
            "enabled":      True,
            "sensors":      [],
            "collect": [
                {"cap": "markets.evolve.tick", "args": {}, "label": "Improve tick"},
                {"cap": "markets.evolve.status", "args": {}, "label": "Leaderboard",
                 "max_items": 4},
            ],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  3, "hours_end": 6,
            "min_idle_minutes":    30,
            "min_interval_minutes": 1200,
            "require_signal":       0.0,
            "depth":        "standard",
            "deliver_to":   ["notebook", "memory"],
            "prompt": (
                "Report the markets self-improvement iteration: which strategies "
                "improved, what new best metrics were reached, what was accepted "
                "(put live) or archived, from the tick result + leaderboard. If "
                "nothing improved, say so and note the loop widened its search. "
                "Ground everything in the data — no invented trades."
            ),
        },
        {
            "name":         "observe_selfheal",
            "label":        "Observability — Self-Heal Scan",
            "description":  "Scans perf findings, event-loop stalls and recent "
                            "errors, distils fixable patterns into code "
                            "suggestions, and reports them (does NOT auto-apply — "
                            "review + promote via Loop Lab CI/CD).",
            "enabled":      True,
            "sensors":      [],
            "collect": [
                {"cap": "evolve.observe.scan", "args": {"launch": False},
                 "label": "Self-heal scan", "max_items": 8},
            ],
            "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                             "dream.stage.synthesize", "dream.stage.deliver"],
            "mode":         "synthesize_only",
            "hitl":         False,
            "hours_start":  4, "hours_end": 7,
            "min_idle_minutes":    30,
            "min_interval_minutes": 1440,
            "require_signal":       0.0,
            "depth":        "standard",
            "deliver_to":   ["notebook", "memory"],
            "prompt": (
                "Report the observability self-heal scan: the perf/error findings "
                "and the concrete code fixes suggested. For each suggestion note "
                "the area and why it matters. Recommend which to turn into a Loop "
                "Lab code pipeline (evolve.pipeline.run kind=code). Facts only "
                "from the scan."
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
        "prompt_style": "agent_loop",   # genuinely agentic — override the one_shot default
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
        "prompt_style": "agent_loop",   # iterative agentic research — override one_shot default
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
    "think": {
        "label":       "Thinking loop",
        "description": "Point at a subject/goal/source (e.g. an RSS feed): read new "
                       "items each idle slot, extract what's interesting, and keep a "
                       "rolling, linked thought stream persisted to the dream memory "
                       "layer. Pair with a web_feed sensor via dream.think.create.",
        "pipeline":    ["dream.stage.gather", "dream.stage.think_reflect"],
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
        # Source review is analysis/documentation → one-shot LLM, no tool loop.
        # (The richer deterministic review lives in the source_review* pipelines.)
        "mode":        "one_shot",
        "prompt_style": "one_shot",
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
        "prompt_style": "agent_loop",   # needs memory tools — override one_shot default
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
        "prompt_style": "agent_loop",   # needs memory + fabric tools — override one_shot default
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
                "prompt_style": v.get("prompt_style"),
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
    # Carry the template's prompting style so an agentic template (agent_loop/
    # investigate default to one_shot) stays agentic once applied, and a
    # one-shot template (code_review) stays one-shot.
    if tmpl.get("prompt_style"): trig["prompt_style"] = tmpl["prompt_style"]
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


# Current cycle/stage, set by _run_cycle around each stage so ANY nested
# _llm_generate call streams to that cycle's live-token channel without every
# helper having to thread cycle_id through its signature.
_LLM_CTX: contextvars.ContextVar[Optional[Dict[str, str]]] = \
    contextvars.ContextVar("dream_llm_ctx", default=None)


# Chat-template control tokens occasionally leak into long generations when a
# model runs past its EOS (seen as `<|endoftext|><|im_start|>user …` glued to
# the end of dream reports). Everything from the FIRST such token is discarded
# — it is never legitimate report content.
_TEMPLATE_LEAK_RE = re.compile(
    r"<\|(?:endoftext|im_start|im_end|eot_id|start_header_id|end_header_id"
    r"|assistant|user|system)\|>")


def _strip_template_leakage(text: str) -> str:
    if not text or "<|" not in text:
        return text
    m = _TEMPLATE_LEAK_RE.search(text)
    return text[:m.start()].rstrip() if m else text


async def _llm_generate(prompt: str, system: str = "", prefer_gpu: bool = True) -> str:
    # Always go through the streaming path: a non-streaming /api/generate must
    # finish inside the whole-response read timeout (OLLAMA_GEN_TIMEOUT), so
    # long generations get killed mid-response. Streaming resets that timeout
    # on every token, keeping the request alive for as long as the model is
    # actually producing output — and, inside a cycle stage, mirrors the
    # tokens to the panel's live stream via _LLM_CTX.
    ctx = _LLM_CTX.get() or {}
    return await _llm_generate_streaming(
        prompt, system=system, prefer_gpu=prefer_gpu,
        cycle_id=str(ctx.get("cycle_id") or ""),
        stage=str(ctx.get("stage") or ""),
    )


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
    _last_prog = [0.0]

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
        # Heartbeat the poll-able progress snapshot (~every 2s) so the panel
        # shows movement even when its event-bus subscription has dropped.
        if cycle_id and time.time() - _last_prog[0] > 2.0:
            _last_prog[0] = time.time()
            await _progress_update(cycle_id, {
                "llm": {"stage": stage, "tokens": len(chunks),
                        "tail": "".join(chunks[-40:])[-280:]},
            })

    try:
        # Structured start event on main bus (only when tied to a cycle —
        # standalone calls stream for keep-alive but have no panel channel)
        if cycle_id:
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
        if cycle_id:
            await emit_event({
                "type":     "dream.llm.complete",
                "cycle_id": cycle_id,
                "stage":    stage,
                "chars":    len(out or ""),
            })
        return _strip_template_leakage(str(out or ""))
    except Exception as e:
        log.debug("dream llm.streaming: %s", e)
        if cycle_id:
            await emit_event({
                "type":     "dream.llm.error",
                "cycle_id": cycle_id,
                "stage":    stage,
                "error":    str(e),
            })
        return _strip_template_leakage("".join(chunks))


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
    """Build kwargs for whichever loop cap (v1/v2/v3) is resolved, passing
    only the parameters that cap actually accepts so we never raise TypeError on
    a setting the variant doesn't support."""
    import inspect
    try:
        accepted = set(inspect.signature(cap_func).parameters.keys())
    except (TypeError, ValueError):
        accepted = set(settings.keys()) | set(overrides.keys())
    merged = {**settings, **overrides}
    return {k: v for k, v in merged.items() if k in accepted}


# ── Agent-loop engine resolution + result normalization ──────────────────────
# All dream agent-loop stages delegate to a dag.agent_loop_v{N} cap. Historically
# four stages hard-wired v2 and only understood v2's {tool_calls, summary, cycles}
# return shape. These helpers centralise engine selection (default v5, with
# graceful fallback to whatever variant is registered) and normalise every
# variant's output onto one shape so the stages are engine-agnostic.
_LOOP_VERSION_CAPS = {
    "v7": "dag.agent_loop_v7",
    "v6": "dag.agent_loop_v6",
    "v5": "dag.agent_loop_v5",
    "v4": "dag.agent_loop_v4",
    "v3": "dag.agent_loop_v3",
    "v2": "dag.agent_loop_v2",
    "v1": "dag.agent_loop",
}
# Preference order when the requested version isn't loaded: prefer the newest
# orchestrated engine, then the stable v2 ReAct engine, then the rest.
_LOOP_FALLBACK_ORDER = ["v5", "v2", "v4", "v3", "v1"]


def _resolve_agent_loop_cap(settings: Optional[Dict[str, Any]] = None):
    """Pick the agent-loop cap for the requested loop_version (default "v5"),
    falling back through _LOOP_FALLBACK_ORDER to whatever is registered.
    Returns (cap_record_or_None, engine_cap_name)."""
    want = str((settings or {}).get("loop_version", "v5") or "v5").lower().strip()
    order = [want] + [v for v in _LOOP_FALLBACK_ORDER if v != want]
    for ver in order:
        cap_name = _LOOP_VERSION_CAPS.get(ver)
        if cap_name and cap_name in CAPABILITY_REGISTRY:
            return CAPABILITY_REGISTRY[cap_name], cap_name
    return None, ""


def _normalize_loop_result(loop_result: Dict[str, Any], engine: str) -> Dict[str, Any]:
    """Map any agent-loop variant's output onto a single shape:
        {engine, steps:[{step,cap,ok,reason,preview[,error]}], summary, cycles, error}
    v1–v4 expose tool_calls + summary + cycles; v5 exposes steps
    (id/title/ok/summary/outputs) with `final` aliased to `summary`."""
    loop_result = loop_result or {}
    summary = (loop_result.get("summary") or loop_result.get("final") or "").strip()
    error = loop_result.get("error")
    steps: List[Dict[str, Any]] = []

    v5_steps = loop_result.get("steps")
    is_v5 = (isinstance(v5_steps, list) and v5_steps
             and isinstance(v5_steps[0], dict) and "title" in v5_steps[0])
    if is_v5:
        for i, st in enumerate(v5_steps):
            if not isinstance(st, dict):
                continue
            outs = st.get("outputs") or {}
            cap_label = ", ".join(str(k) for k in outs.keys()) or f"step:{st.get('id', i)}"
            steps.append({
                "step":    i,
                "cap":     cap_label,
                "ok":      bool(st.get("ok")),
                "reason":  str(st.get("title", ""))[:200],
                "preview": str(st.get("summary", ""))[:300],
            })
        cycles = loop_result.get("cycles") or len(v5_steps)
    else:
        tool_calls = loop_result.get("tool_calls") or []
        for i, tc in enumerate(tool_calls):
            if not isinstance(tc, dict):
                continue
            cap_called = tc.get("tool") or tc.get("cap") or "?"
            preview = str(tc.get("result") or tc.get("output") or tc.get("preview") or "")[:300]
            thought = str(tc.get("thought") or tc.get("reason") or "")[:200]
            ok = not bool(tc.get("error"))
            rec: Dict[str, Any] = {"step": i, "cap": cap_called, "ok": ok,
                                   "reason": thought, "preview": preview}
            if not ok:
                rec["error"] = str(tc.get("error", ""))[:200]
            steps.append(rec)
        cycles = loop_result.get("cycles", len(tool_calls))

    return {"engine": engine, "steps": steps, "summary": summary,
            "cycles": cycles, "error": error}


def _sbx_mod():
    """The session-sandbox module (run-owner scoping helpers), or None."""
    for _n, _m in list(sys.modules.items()):
        if _m is not None and _n.endswith("session_sandbox_capabilities") \
                and hasattr(_m, "set_run_owner"):
            return _m
    return None


def _cycle_sandbox_owner(trig: Dict[str, Any], project_slug: str) -> tuple:
    """(owner_key, kind, label) for a cycle's ONE shared sandbox container:
    goal-<slug> for project/goal cycles, else a per-PIPELINE 'dream-<trigger>'
    container that every cycle of the same pipeline reuses (brought up on use,
    slept at cycle end) — never a fresh container per cycle/stage."""
    if project_slug:
        return f"goal-{project_slug}", "goal", str(project_slug)
    name = str(trig.get("name") or trig.get("label") or "dream").strip()
    m = _sbx_mod()
    if m is not None:
        return m.slug_key("dream", name), "dream", name
    safe = "".join(c if (c.isalnum() or c in "_.-") else "-"
                   for c in name.lower()).strip("-") or "x"
    return f"dream-{safe}"[:60], "dream", name


async def _sbx_link_loop(session_id: str, target: str, *, kind: str = "goal",
                         label: str = "") -> None:
    """Route this loop run's exec/code/file-IO into a SHARED sandbox container
    (goal-/project-scoped key) via session_sandbox_capabilities aliasing, so
    every cycle of a long-running goal works in ONE persistent /workspace.
    Best-effort no-op when the sandbox module isn't loaded."""
    try:
        for _n, _m in list(sys.modules.items()):
            if _m is not None and _n.endswith("session_sandbox_capabilities") \
                    and hasattr(_m, "link_session"):
                await _m.link_session(session_id, target, kind=kind, label=label)
                return
    except Exception as e:
        log.debug("sandbox link for %s → %s failed: %s", session_id, target, e)


async def _run_agent_loop(*, goal: str, allowed_caps: str,
                          settings: Dict[str, Any], session_id: str,
                          max_steps: int = 6, **overrides) -> Dict[str, Any]:
    """Resolve + run the configured agent-loop variant and return a normalized
    result (plus the raw result under "raw"). Centralises engine selection so
    every dream stage picks up v5 via DEFAULT_LOOP_SETTINGS["loop_version"].
    `max_steps` (the dream's step budget) overrides the settings value so the
    trigger's max_steps drives v5's plan length; it's filtered out for variants
    that don't accept it."""
    cap, engine = _resolve_agent_loop_cap(settings)
    if not cap:
        return {"engine": "", "steps": [], "summary": "", "cycles": 0,
                "error": "no agent_loop variant registered", "raw": {}}
    # File discipline: dream loops must leave durable output behind, not just
    # a long transcript — reports/notes go to notebook or workspace files so
    # the cycle's deliverables survive the context window.
    goal = (goal.rstrip() +
            "\n\nOUTPUT DISCIPLINE: as you work, collate substantial findings "
            "and intermediate results into durable output (notebook.append / "
            "workspace files) rather than only carrying them in your replies. "
            "Finish by stating clearly WHAT durable output you produced and where."
            "\n\nCODE AUDIENCE: code YOU will run to do the task must be complete and "
            "runnable NOW — real values, real inputs from the workspace, NO "
            "placeholder / TODO / 'your-key-here' stubs; if it needs a secret or path, "
            "read it from the environment/workspace or report it blocked, then run it "
            "and confirm it worked. Only code handed to a HUMAN as a deliverable may "
            "carry placeholders, and only when clearly labelled as such.")
    # Surface this loop run in the cycle's poll-able progress snapshot so the
    # panel can show (and re-attach to) the live session even when the event
    # feed drops. session_id is "dream:<cycle_id>:<stage>".
    _cid = ""
    try:
        _parts = str(session_id or "").split(":")
        if len(_parts) >= 2 and _parts[0] == "dream":
            _cid = _parts[1]
    except Exception:
        pass
    if _cid:
        await _progress_update(_cid, {
            "loop": {"session_id": session_id, "engine": engine,
                     "status": "running", "started_at": now_iso()},
        })
    loop_kwargs = _loop_kwargs_for(
        cap["func"], settings,
        goal=goal,
        allowed_caps=allowed_caps,
        max_cycles=settings.get("max_cycles", max_steps),
        max_steps=max_steps,
        session_id=session_id,
        # Thread the dream session id down as trace_id so stream.token frames
        # from the loop's LLM calls are attributable to this dream run — the
        # dream panel and the agent-loop SSE bridge both filter on it.
        trace_id=session_id,
        **overrides,
    )
    try:
        raw = await cap["func"](**loop_kwargs) or {}
    except Exception as e:
        log.warning("dream agent-loop (%s) error: %s", engine, e)
        if _cid:
            await _progress_update(_cid, {
                "loop": {"session_id": session_id, "engine": engine,
                         "status": "error", "error": str(e)[:300]},
            })
        return {"engine": engine, "steps": [], "summary": "", "cycles": 0,
                "error": str(e), "raw": {}}
    norm = _normalize_loop_result(raw, engine)
    norm["raw"] = raw
    if _cid:
        await _progress_update(_cid, {
            "loop": {"session_id": session_id, "engine": engine,
                     "status": "done", "steps": len(norm.get("steps") or []),
                     "summary": str(norm.get("summary", ""))[:300]},
        })
    return norm


# ─────────────────────────────────────────────────────────────────────────────
# PROMPTING STYLE — one-shot LLM prompt vs agentic (tool-using) loop
# ─────────────────────────────────────────────────────────────────────────────
# Analysis stages (investigate / agent_loop) can run in either style, chosen
# PER STAGE so a pipeline can mix them: a source review or a summary wants a
# single grounded LLM prompt (fast, no tool-calling, streams to the panel),
# while an investigation that must gather evidence wants the ReAct agent loop.
# The agentic loop is only worth its cost when the task involves DOING things
# (calling tools, editing) — not for generating analysis or documentation.
#
# Style resolution order (first hit wins), falling back to `default`:
#   1. trig["stage_config"][<stage_short>]["prompt_style"]   ← per-stage toggle
#   2. trig["prompt_style"]                                   ← pipeline/trigger default
#   3. default (the stage's historical behaviour)
_ONE_SHOT_ALIASES = {"one_shot", "oneshot", "one-shot", "llm", "prompt", "single"}
_AGENTIC_ALIASES  = {"agent_loop", "agentic", "loop", "agent", "stepwise", "react"}

# Short names of the stages that run a tool-using agent loop and can instead be
# flipped to a one-shot LLM prompt via stage_config[<short>].prompt_style.
_AGENTIC_ANALYSIS_STAGES = ("agent_loop", "investigate", "stepwise_execute")

# Default prompting style PER stage. `stepwise_execute` is the designated
# agentic stage (plan + act with tools); `agent_loop`/`investigate` default to a
# single grounded LLM prompt — analysis/documentation shouldn't pay for a tool
# loop. A pipeline opts a specific stage the other way via prompt_style.
_STAGE_DEFAULT_STYLE = {
    "stepwise_execute": "agent_loop",
    "agent_loop":       "one_shot",
    "investigate":      "one_shot",
}

# Read-only "analysis" cap prefixes: a pipeline whose whitelist consists ONLY of
# these calls no tools that change state or fetch new work — it's pure
# analysis/summary, so its agentic stages are wasted and should run one-shot.
# Anything outside this set (writes, execs, research.expand, fabric.ingest, …)
# means the loop genuinely acts, so we leave it agentic.
_READONLY_ANALYSIS_CAPS = (
    "llm.", "nlp.run", "nlp.modules",
    "memory.search", "memory.recall", "memory.similar", "memory.traverse",
    "memory.all_nodes", "memory.graph_stats", "memory.stats", "memory.get",
    "fabric.query", "fabric.datasets", "fabric.sources",
    "fabric.entity_graph.snapshot",
    "research.history", "research.db.search", "research.job.status",
    "research.bookmarks", "research.iterate.list",
    "ide.inspect.", "ide.fs.read", "ide.code.list_files",
    "cal.events.list", "cal.todos.list", "cal.notes.list",
    "project.list", "project.get", "project.context",
    "dream.review.area_report",
)


def _whitelist_is_readonly_analysis(whitelist: Optional[List[str]]) -> bool:
    """True iff every cap in the whitelist is a read-only analysis cap (and the
    whitelist is non-empty). Empty/None → False: it falls back to the global
    whitelist at runtime, which can act, so we don't assume pure analysis."""
    caps = [c for c in (whitelist or []) if isinstance(c, str)]
    if not caps:
        return False
    return all(any(c == p or c.startswith(p) for p in _READONLY_ANALYSIS_CAPS)
               for c in caps)


def _stage_prompt_style(trig: Dict[str, Any], stage_short: str,
                        default: Optional[str] = None) -> str:
    """Resolve the prompting style ('one_shot' | 'agent_loop') for one stage.
    Order: stage_config[stage].prompt_style → trig.prompt_style → the stage's
    default in _STAGE_DEFAULT_STYLE (or the caller-supplied `default`)."""
    if default is None:
        default = _STAGE_DEFAULT_STYLE.get(stage_short, "one_shot")
    sc = (trig.get("stage_config") or {}).get(stage_short) or {}
    raw = (sc.get("prompt_style") or trig.get("prompt_style") or "").strip().lower()
    if raw in _ONE_SHOT_ALIASES:
        return "one_shot"
    if raw in _AGENTIC_ALIASES:
        return "agent_loop"
    return default


async def _run_oneshot_analysis(*, goal: str, state: Dict[str, Any],
                                stage: str, system: str = "") -> Dict[str, Any]:
    """Run one grounded LLM prompt (no tool loop) and return a result shaped like
    a normalized agent-loop result — {"steps": [], "summary": str, "cycles": 0,
    "engine": "one_shot"} — so callers populate state identically for both
    styles. Tokens stream to this cycle's panel channel via _LLM_CTX (set by the
    runner around every stage), using the same ollama streaming path as the rest
    of the dream/source-review pipeline."""
    cycle_id = state.get("cycle_id", "?")
    prompt = (
        goal.rstrip()
        + "\n\n---\nProduce the finished written deliverable now — analysis, "
        "findings, or documentation grounded ONLY in the context above. Do NOT "
        "call tools, plan actions, or ask questions; write the result directly."
    )
    sys = system or (
        "You are an analyst producing a written deliverable from the context "
        "provided. Be specific, well-structured, and concrete. Do not invent "
        "facts beyond what the context supports."
    )
    await emit_event({"type": "dream.oneshot.start", "cycle_id": cycle_id,
                      "stage": stage})
    out = (await _llm_generate(prompt, system=sys) or "").strip()
    await emit_event({"type": "dream.oneshot.complete", "cycle_id": cycle_id,
                      "stage": stage, "chars": len(out)})
    return {"engine": "one_shot", "steps": [], "summary": out, "cycles": 0,
            "one_shot": True}


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
            base = (f"Action: {(pa.get('portion') or pa.get('goal') or '')[:160]} — "
                    f"{'ok' if pa.get('ok') else 'see result'}.")
            if pa.get("plan_remaining") is not None:
                base += (f" Plan: {pa.get('plan_remaining')} of "
                         f"{pa.get('plan_total','?')} portion(s) remaining.")
            return base
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


# ─────────────────────────────────────────────────────────────────────────────
# LIVE PROGRESS SNAPSHOT — poll-friendly view of a running cycle
# ─────────────────────────────────────────────────────────────────────────────
# The event bus is best-effort: iframe subscriptions drop, loop events from
# foreign session ids get filtered, and long agent-loop stages can go minutes
# without a dream.* event — which is exactly when the panel "shows no activity"
# despite ollama being busy. This snapshot is the authoritative, poll-able
# record: every stage start/end, LLM token flush, agent-loop handoff and output
# file lands here, so dream.cycle.progress always has something fresh to show.

async def _progress_update(cycle_id: str, patch: Dict[str, Any],
                           stage: str = "", stage_patch: Optional[Dict[str, Any]] = None):
    """Merge `patch` into the cycle's progress snapshot (and `stage_patch` into
    its per-stage record). Best-effort — never raises."""
    if not cycle_id:
        return
    r = _redis()
    if not r:
        return
    try:
        key = KEY_PROGRESS + str(cycle_id)
        raw = await r.get(key)
        try:
            snap = json.loads(raw.decode() if isinstance(raw, bytes) else raw) if raw else {}
        except Exception:
            snap = {}
        snap.update(patch or {})
        if stage:
            stages = snap.setdefault("stages", {})
            srec = stages.setdefault(stage, {})
            srec.update(stage_patch or {})
        snap["cycle_id"] = cycle_id
        snap["updated_at"] = now_iso()
        await r.set(key, json.dumps(snap, default=str))
        await r.expire(key, 48 * 3600)
    except Exception as e:
        log.debug("dream progress update: %s", e)


async def _progress_get(cycle_id: str) -> Dict[str, Any]:
    r = _redis()
    if not r or not cycle_id:
        return {}
    try:
        raw = await r.get(KEY_PROGRESS + str(cycle_id))
        if not raw:
            return {}
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# PER-CYCLE OUTPUT WORKSPACE — collate work into files, not context
# ─────────────────────────────────────────────────────────────────────────────

_SAFE_FILE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _cycle_dir(cycle_id: str, create: bool = True) -> Optional[Path]:
    cid = _SAFE_FILE_RE.sub("_", str(cycle_id or ""))[:64]
    if not cid:
        return None
    d = OUTPUT_ROOT / cid
    if create:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.debug("dream cycle dir %s: %s", d, e)
            return None
    return d


async def _cycle_file_write(cycle_id: str, name: str, content: str,
                            append: bool = False) -> Optional[str]:
    """Write/append a collation file in the cycle's output workspace and mirror
    the file list into the progress snapshot. Returns the file name or None."""
    d = _cycle_dir(cycle_id)
    if d is None or not name:
        return None
    fname = _SAFE_FILE_RE.sub("_", name)[:120]
    try:
        path = d / fname
        mode = "a" if append else "w"
        with open(path, mode, encoding="utf-8") as fh:
            fh.write(content if content.endswith("\n") else content + "\n")
        files = _cycle_files_list(cycle_id)
        await _progress_update(cycle_id, {"files": files})
        return fname
    except Exception as e:
        log.debug("dream cycle file %s/%s: %s", cycle_id, name, e)
        return None


def _cycle_files_list(cycle_id: str) -> List[Dict[str, Any]]:
    d = _cycle_dir(cycle_id, create=False)
    if d is None or not d.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    try:
        for p in sorted(d.iterdir()):
            if not p.is_file():
                continue
            st = p.stat()
            out.append({
                "name":  p.name,
                "bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                                 .isoformat(timespec="seconds"),
            })
    except Exception:
        pass
    return out


def _fmt_gather_markdown(gather: Dict[str, Any]) -> str:
    """Render the full gather working set (collectors + sensors) as markdown —
    the untruncated collation the synthesis stages and the user can read back."""
    lines: List[str] = [f"# Gathered material\n"]
    for sname, sres in (gather.get("results") or {}).items():
        if not isinstance(sres, dict):
            continue
        lines.append(f"\n## {sname}  (count {sres.get('count', '?')}, "
                     f"signal {sres.get('signal', 0)})\n")
        if sres.get("error"):
            lines.append(f"- error: {sres['error']}")
            continue
        for item in (sres.get("sample") or [])[:60]:
            if isinstance(item, dict):
                txt = (item.get("text") or item.get("title") or item.get("message")
                       or item.get("headline") or item.get("summary") or "")
                meta = " · ".join(str(item[k]) for k in ("url", "ts", "category", "dataset")
                                  if item.get(k))
                lines.append(f"- {str(txt)[:2000]}" + (f"\n  ({meta})" if meta else ""))
            elif isinstance(item, str):
                lines.append(f"- {item[:2000]}")
    return "\n".join(lines)


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
        # Get configured reset prefixes (allowlist approach — only these count as activity).
        # In follow mode these are derived live from the cap-tracking system.
        cfg = await _get_config()
        reset_prefixes = _effective_idle_reset_prefixes(cfg)

        rows = await r.zrevrange(KEY_RECENT_CAPS, 0, 120, withscores=True)
        now_ts = time.time()
        for raw, score in rows or []:
            try:
                rec = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                name = rec.get("name", "")
                sid  = str(rec.get("sid", "") or "")
            except Exception:
                continue
            # Never let a dream's own activity reset the idle timer — otherwise a
            # running dream (which calls llm.*/research.*/web.* under a "dream:*"
            # session) makes itself look like an active user, causing reviews to
            # yield ("user active") and the scheduler cadence to thrash. Skip
            # entries from dream sessions and from infra/dream cap names.
            if sid.startswith("dream") or any(name.startswith(p) for p in _IDLE_IGNORE_PREFIXES):
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

# Trivial chatter that shouldn't be treated as "signal" for the dream pipeline.
_GREETING_TEXTS = {"hello", "hi", "hey", "yo", "sup", "ok", "okay", "thanks",
                   "ty", "test", "ping", "hello there", "good morning"}
# Markers of the dream's OWN low-value diagnostic notes. Re-ingesting these is
# the noise feedback loop, so they're filtered out of the recent-memory signal.
_DREAM_DIAG_MARKERS = (
    "all sensors returned no usable data",
    "— no signal", "- no signal", "no usable data",
    "no new items for", "nothing new to think about",
    "skipping synthesis to avoid fabricating",
)


def _is_low_value_memory(rec: Dict[str, Any], min_chars: int = 12,
                         exclude_dream_diagnostics: bool = True) -> bool:
    """True when a memory record is chatter or one of the dream's own diagnostic
    notes — i.e. it shouldn't count as recent-activity signal. Centralised so
    sensors and the dream-layer browse share one definition of 'noise'."""
    text = (rec.get("text") or rec.get("summary") or "").strip()
    low = text.lower()
    if len(text) < int(min_chars):
        return True
    if low in _GREETING_TEXTS:
        return True
    if exclude_dream_diagnostics and any(m in low for m in _DREAM_DIAG_MARKERS):
        return True
    return False


@capability(
    "dream.sensor.memory_recent", memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/memory_recent",
    http_tags=["dream", "sensor"],
    description="Sample recent memory records — wander-friendly signal for the dream "
                "pipeline. Filters out chatter + the dream's own diagnostic notes so "
                "the signal reflects real activity, not its own noise. Params: limit, "
                "min_chars, exclude_dream_diagnostics, exclude_source_types (csv).",
)
async def dream_sensor_memory_recent(limit: int = 30, min_chars: int = 12,
                                     exclude_dream_diagnostics: bool = True,
                                     exclude_source_types: str = "",
                                     trace_id=None):
    """
    Pull recent memory records. Prefers memory.all_nodes (chronological) since
    memory.search with empty query produces no embedding and returns nothing.
    Falls back to memory.session_history then memory.search if all_nodes is unavailable.
    Quality-filters chatter + dream self-diagnostics so the firing signal reflects
    genuine recent activity rather than the dream's own accumulated noise.
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

    # ── Quality filter — drop chatter, dream self-diagnostics, and any
    #    excluded source_types so the firing signal isn't dominated by noise. ──
    _excl_src = {s.strip().lower() for s in (exclude_source_types or "").split(",") if s.strip()}
    raw_count = len(normalised)
    quality: List[Dict[str, Any]] = []
    for rec in normalised:
        if _excl_src and str(rec.get("source_type", "")).lower() in _excl_src:
            continue
        if _is_low_value_memory(rec, min_chars=min_chars,
                                exclude_dream_diagnostics=exclude_dream_diagnostics):
            continue
        quality.append(rec)

    # Signal now reflects how much *quality* recent activity exists. When recent
    # memory is all chatter/diagnostics, signal collapses → repetitive low-value
    # dreams stop firing on their own noise.
    signal = min(1.0, len(quality) / max(20, int(limit)))
    out = {
        "source":   "memory",
        "count":    len(quality),
        "raw_count": raw_count,
        "filtered": raw_count - len(quality),
        "signal":   round(signal, 3),
        "sample":   quality[:int(limit)],
        "summary":  f"{len(quality)} recent memory records"
                    + (f" ({raw_count - len(quality)} low-value filtered)"
                       if raw_count != len(quality) else ""),
    }
    if not quality and last_err:
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
    # Enumerate datasets via the fabric.datasets / fabric.sources CAPS (the fabric
    # object's own list_datasets() returns nothing in this deployment — that's why
    # this sensor read 0 while fabric_by_tag, which uses fabric.sources, saw 28).
    try:
        # 1. Get datasets
        datasets: List[Dict[str, Any]] = []
        ds_cap = CAPABILITY_REGISTRY.get("fabric.datasets")
        if ds_cap:
            try:
                res = await ds_cap["func"]() or {}
                datasets = (res.get("datasets") if isinstance(res, dict) else res) or []
            except Exception:
                datasets = []
        if not datasets:
            src_cap = CAPABILITY_REGISTRY.get("fabric.sources")
            if src_cap:
                try:
                    res = await src_cap["func"]() or {}
                    srcs = (res.get("sources") if isinstance(res, dict) else res) or []
                    # Collapse sources to unique datasets (a source ≈ a dataset here)
                    seen_ds: set = set()
                    for s in srcs:
                        if not isinstance(s, dict):
                            continue
                        did = s.get("dataset_id") or s.get("id") or s.get("name") or ""
                        if did and did not in seen_ds:
                            seen_ds.add(did)
                            datasets.append({"dataset_id": did,
                                             "record_count": s.get("record_count", s.get("count", 0))})
                except Exception:
                    pass

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


# ── Generic web/RSS feed sensor — fuel for thinking loops ────────────────────

def _clean_xml(s: str) -> str:
    """Strip CDATA / nested HTML tags and decode the common XML entities."""
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s or "", flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'").replace("&apos;", "'"))
    return re.sub(r"\s+", " ", s).strip()


def _parse_feed_items(raw: str, max_items: int = 60) -> List[Dict[str, Any]]:
    """Minimal RSS/Atom parser (no external deps). Extracts each <item>/<entry>'s
    title, link, id/guid, summary and published date."""
    if not raw:
        return []
    out: List[Dict[str, Any]] = []
    for _tag, blk in re.findall(r"<(item|entry)\b[^>]*>(.*?)</\1>", raw, re.S | re.I)[:max_items]:
        def _first(*pats):
            for p in pats:
                m = re.search(p, blk, re.S | re.I)
                if m:
                    return _clean_xml(m.group(1))
            return ""
        title = _first(r"<title[^>]*>(.*?)</title>")
        link = _first(r"<link[^>]*>(.*?)</link>")
        if not link:
            m = re.search(r"<link[^>]*href=[\"']([^\"']+)[\"']", blk, re.I)
            link = m.group(1) if m else ""
        guid = _first(r"<guid[^>]*>(.*?)</guid>", r"<id[^>]*>(.*?)</id>")
        summary = _first(r"<description[^>]*>(.*?)</description>",
                         r"<summary[^>]*>(.*?)</summary>",
                         r"<content[^>]*>(.*?)</content>")
        pub = _first(r"<pubDate[^>]*>(.*?)</pubDate>",
                     r"<updated[^>]*>(.*?)</updated>",
                     r"<published[^>]*>(.*?)</published>")
        out.append({
            "id":    guid or link or title,
            "title": title[:300],
            "link":  link[:500],
            "text":  summary[:1200],
            "ts":    pub,
        })
    return out


@capability(
    "dream.sensor.web_feed", memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/web_feed",
    http_tags=["dream", "sensor"],
    description="Read an RSS/Atom feed (or any web URL) and return only the items "
                "not seen on previous calls — the fuel for a thinking loop. Tracks "
                "seen item ids per feed in Redis so each call yields just what's new. "
                "Inputs: url (str!), feed_id (str — dedupe namespace, defaults to a "
                "hash of the url), limit (int, default 25). "
                "Output: {source, count, signal, sample, summary}.",
)
async def dream_sensor_web_feed(url: str = "", feed_id: str = "",
                                limit: int = 25, trace_id=None):
    import hashlib
    url = (url or "").strip()
    if not url:
        return {"source": "web_feed", "count": 0, "signal": 0.0, "note": "no url"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    fid = feed_id or hashlib.sha1(url.encode()).hexdigest()[:16]

    raw = ""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True,
                                     headers={"User-Agent": "Vera-Dream/1.0"}) as c:
            resp = await c.get(url)
            if resp.status_code == 200:
                raw = resp.text
            else:
                return {"source": "web_feed", "count": 0, "signal": 0.0,
                        "error": f"http {resp.status_code}", "url": url}
    except Exception as e:
        return {"source": "web_feed", "count": 0, "signal": 0.0,
                "error": str(e), "url": url}

    items = _parse_feed_items(raw)
    # Only return items we haven't surfaced before (per feed).
    r = _redis()
    seen_key = f"vera:dream:feed:seen:{fid}"
    new_items: List[Dict[str, Any]] = []
    first_run = False
    if r:
        try:
            first_run = (await r.scard(seen_key)) == 0
        except Exception:
            pass
    for it in items:
        iid = it.get("id") or it.get("link") or it.get("title")
        if not iid:
            continue
        is_new = True
        if r:
            try:
                is_new = bool(await r.sadd(seen_key, iid))
            except Exception:
                pass
        if is_new:
            new_items.append(it)
    if r:
        try:
            await r.expire(seen_key, 30 * 86400)
        except Exception:
            pass
    # On the very first run, surface a small sample so the loop has something to
    # think about rather than swallowing the whole backlog silently.
    if first_run and not new_items and items:
        new_items = items[:int(limit or 25)]
    new_items = new_items[:int(limit or 25)]
    signal = min(1.0, len(new_items) / max(5, int(limit or 25) // 2))
    return {
        "source":  "web_feed",
        "url":     url, "feed_id": fid,
        "count":   len(new_items),
        "signal":  round(signal, 3),
        "sample":  new_items,
        "summary": f"{len(new_items)} new item(s) from {url}",
    }


@capability(
    "dream.sensor.topic_research", memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/topic_research",
    http_tags=["dream", "sensor"],
    description="Subject-scoped grounding for thinking loops: blends a memory.search, "
                "a fabric.query, and (optionally) a web.search on the subject, dedupes, "
                "and surfaces only NEW items per feed so each idle slot reflects on "
                "fresh, relevant material instead of re-reading raw recent memory. "
                "Params: subject (str!), use_fabric (bool), use_web (bool), limit (int), "
                "feed_id (str — novelty cursor key, defaults from subject).",
)
async def dream_sensor_topic_research(subject: str = "", use_fabric: bool = True,
                                      use_web: bool = True, limit: int = 20,
                                      feed_id: str = "", trace_id=None):
    subject = (subject or "").strip()
    if not subject:
        return {"source": "topic_research", "count": 0, "signal": 0.0,
                "note": "no subject"}
    fid = feed_id or _think_slug(subject)
    items: List[Dict[str, Any]] = []

    # 1) Memory — what we already know / have noted about the subject.
    mem_search = CAPABILITY_REGISTRY.get("memory.search")
    if mem_search:
        try:
            sr = await mem_search["func"](query=subject, limit=int(limit))
            for it in (sr or {}).get("results", [])[:int(limit)]:
                rec = it.get("record", it) if isinstance(it, dict) else {}
                txt = (rec.get("text") or rec.get("summary") or "").strip()
                if txt and not _is_low_value_memory(rec):
                    items.append({"id": rec.get("id"), "text": txt[:600],
                                  "source": "memory", "ts": rec.get("created_at", "")})
        except Exception as e:
            log.debug("topic_research memory: %s", e)

    # 2) Fabric — structured/ingested data about the subject.
    if use_fabric:
        fab_q = CAPABILITY_REGISTRY.get("fabric.query")
        if fab_q:
            try:
                res = await fab_q["func"](query=json.dumps({
                    "text": subject, "top_k": int(limit), "include_data": True,
                    "cache": False}))
                for row in (res or {}).get("results", [])[:int(limit)]:
                    if isinstance(row, dict):
                        txt = (row.get("text") or row.get("content") or "").strip()
                        if txt:
                            items.append({"id": row.get("id"), "text": txt[:600],
                                          "source": "fabric",
                                          "dataset": row.get("dataset_id", "")})
            except Exception as e:
                log.debug("topic_research fabric: %s", e)

    # 3) Web — fresh external material (autonomous search while idle).
    if use_web:
        web = CAPABILITY_REGISTRY.get("web.search")
        if web:
            try:
                res = await web["func"](query=subject, limit=min(int(limit), 10))
                for row in (res or {}).get("results", [])[:int(limit)]:
                    if isinstance(row, dict):
                        txt = (row.get("snippet") or row.get("title")
                               or row.get("text") or "").strip()
                        url = row.get("url") or row.get("link") or ""
                        if txt:
                            items.append({"id": url or txt[:80],
                                          "title": row.get("title", ""),
                                          "text": txt[:600], "link": url, "source": "web"})
            except Exception as e:
                log.debug("topic_research web: %s", e)

    # Dedupe by id/link/text.
    seen_ids: set = set()
    deduped: List[Dict[str, Any]] = []
    for it in items:
        key = str(it.get("id") or it.get("link") or it.get("text", "")[:120])
        if key in seen_ids:
            continue
        seen_ids.add(key)
        deduped.append(it)

    # Novelty: only surface items not seen in prior slots for this subject, so a
    # thinking loop builds on fresh material each idle window (mirrors web_feed).
    r = _redis()
    seen_key = f"vera:dream:topic:seen:{fid}"
    new_items: List[Dict[str, Any]] = []
    first_run = False
    if r:
        try:
            first_run = (await r.scard(seen_key)) == 0
        except Exception:
            pass
    for it in deduped:
        iid = str(it.get("id") or it.get("link") or it.get("text", "")[:120])
        is_new = True
        if r:
            try:
                is_new = bool(await r.sadd(seen_key, iid))
            except Exception:
                pass
        if is_new:
            new_items.append(it)
    if r:
        try:
            await r.expire(seen_key, 30 * 86400)
        except Exception:
            pass
    # First run: surface a sample so the loop has something to think about.
    if first_run and not new_items and deduped:
        new_items = deduped[:int(limit)]
    new_items = new_items[:int(limit)]
    signal = min(1.0, len(new_items) / max(5, int(limit) // 2))
    srcs = "memory" + ("+fabric" if use_fabric else "") + ("+web" if use_web else "")
    return {
        "source":  "topic_research", "subject": subject, "feed_id": fid,
        "count":   len(new_items),
        "signal":  round(signal, 3),
        "sample":  new_items,
        "summary": f"{len(new_items)} new item(s) on '{subject}' ({srcs})",
    }


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

        # ── Origin agentic-loop handoff (the CLEAN, high-signal context) ──────
        # The plan + recent outputs + artifacts from the loop(s) that created and
        # advanced this project. Grounding the dream on THIS (rather than rebuilding
        # from noisy memory_recent) is what stops the nonsense-goal drift.
        loop_runs: List[Dict[str, Any]] = []
        artifacts: List[Dict[str, Any]] = []
        loops_cap = CAPABILITY_REGISTRY.get("project.loops.list")
        arts_cap  = CAPABILITY_REGISTRY.get("project.artifacts.list")
        if loops_cap:
            try:
                lr = await loops_cap["func"](slug=project_slug, limit=6)
                loop_runs = (lr or {}).get("runs", []) if isinstance(lr, dict) else []
            except Exception as e:
                log.debug("project_context loop runs: %s", e)
        origin = next((x for x in loop_runs if x.get("source") == "escalation"
                       and x.get("plan")), None)
        if origin:
            ctx_parts.insert(0, {
                "text": "ORIGIN PLAN — the documented plan from the agentic loop that "
                        "escalated this goal. CONTINUE the next unfinished portion; do "
                        "not restart or invent new scope:\n" + str(origin["plan"])[:3500],
                "kind": "origin_plan"})
        for run in [x for x in loop_runs if x.get("source") != "escalation"][:3]:
            body = (run.get("final") or "").strip()
            if body:
                ctx_parts.append({
                    "text": f"PRIOR LOOP RUN ({str(run.get('ts',''))[:16]} · "
                            f"{run.get('source','')} · {run.get('steps_total',0)} steps) — "
                            f"latest output to build on:\n" + body[:1400],
                    "kind": "loop_run"})
        if arts_cap:
            try:
                ar = await arts_cap["func"](slug=project_slug, limit=40)
                artifacts = (ar or {}).get("artifacts", []) if isinstance(ar, dict) else []
            except Exception as e:
                log.debug("project_context artifacts: %s", e)
        if artifacts:
            lines = [f"- [{a.get('type')}] {a.get('name')}"
                     + (f"  ({a.get('path')})" if a.get("path") else "")
                     for a in artifacts[:30]]
            ctx_parts.append({
                "text": "PROJECT ARTIFACTS produced so far (files/code/reports/traces — "
                        "read a specific one via `project.artifact.get(slug, id)`):\n"
                        + "\n".join(lines),
                "kind": "artifacts"})

        signal = 1.0 if ctx_parts else 0.0
        return {
            "source":       "project",
            "project_slug": project_slug,
            "count":        len(ctx_parts),
            "signal":       signal,
            "sample":       ctx_parts,
            "loop_runs":    len(loop_runs),
            "artifacts":    len(artifacts),
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

        dominant = next((p for p in projects if p.get("dominant")), None)

        # ── Real projects (project.list) — the actual content the dream should
        #    reason about. Previously this sensor only counted cap-call namespaces
        #    and exposed them under `projects` (NOT `sample`), so the pipeline —
        #    which reads `sample` — saw nothing. Surface real projects in `sample`.
        sample: List[Dict[str, Any]] = []
        real_projects: List[Dict[str, Any]] = []
        plist = CAPABILITY_REGISTRY.get("project.list")
        if plist:
            try:
                pres = await plist["func"]() or {}
                for p in (pres.get("projects") or []):
                    if not isinstance(p, dict):
                        continue
                    ctx = (p.get("summary") or p.get("llm_context")
                           or p.get("user_context") or "").strip()
                    real_projects.append({"slug": p.get("slug"), "name": p.get("name")})
                    sample.append({
                        "text": f"PROJECT {p.get('name','?')}: {ctx[:400]}".strip(),
                        "id":   p.get("slug"),
                        "name": p.get("name"),
                        "role": "project",
                        "status": p.get("status", ""),
                    })
            except Exception as e:
                log.debug("active_projects project.list: %s", e)
        # Append the cap-namespace activity as one digest item for context.
        if projects:
            sample.append({
                "text": ("Recent system activity by area: "
                         + ", ".join(f"{p['prefix']} ({p['pct']}%)" for p in projects[:6])),
                "role": "activity_digest",
            })

        # Signal from real projects + recent activity (favours having projects).
        signal = min(1.0, (len(real_projects) * 0.4) + (len(projects) / 5.0))

        return {
            "source":       "active_projects",
            "count":        len(real_projects) or len(projects),
            "signal":       round(signal, 3),
            "sample":       sample,
            "projects":     real_projects,
            "activity_by_area": projects,
            "total_calls":  total_calls,
            "hours_back":   hours_back,
            "dominant":     dominant.get("prefix") if dominant else None,
            "summary": (
                f"{len(real_projects)} project(s); {total_calls} cap calls across "
                f"{len(prefix_counter)} namespaces in last {hours_back}h. "
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
    snap_res: Dict[str, Any] = {}
    if snap_cap:
        try:
            snap_res = await snap_cap["func"]() or {}
            snapshots = snap_res.get("snapshots", [])
        except Exception:
            pass

    # CHEAP change detection — compare the latest snapshot's hash/freshness
    # against the live source hash (both already returned by list_snapshots).
    # The previous implementation ran ide.inspect.diff_snapshot, which READS
    # every changed file's content (max_chars_per_file=5000) → this sensor timed
    # out (>35s). A sensor must be fast; the exact changed-file list is available
    # on demand to the agent loop via ide.inspect.diff_snapshot when it matters.
    changed_files: List[str] = []
    source_changed = False
    cur_hash = ""
    if snapshots:
        latest_snap = snapshots[0]
        cur_hash = snap_res.get("current_source_hash", "")
        source_changed = (not latest_snap.get("is_fresh", False)) or (
            bool(cur_hash) and latest_snap.get("source_hash") != cur_hash)

    signal = 0.5 if source_changed else 0.1

    sample: List[Dict[str, Any]] = []
    for mod in modules[:15]:
        sample.append({
            "text": f"{mod['name']} ({mod['lines']} lines, {mod['bytes']} bytes)",
            "file": mod["name"],
            "lines": mod["lines"],
        })

    return {
        "source":         "source_changes",
        "count":          len(modules),
        "signal":         round(signal, 3),
        "sample":         sample,
        "source_changed": source_changed,
        "changed_files":  changed_files[:20],   # empty by design; pull on demand
        "modules_count":  len(modules),
        "cap_count":      cap_count,
        "has_snapshot":    bool(snapshots),
        "latest_snapshot": snapshots[0]["id"] if snapshots else None,
        "snapshot_fresh":  snapshots[0].get("is_fresh") if snapshots else None,
        "summary": (
            f"{len(modules)} Python modules, {cap_count} caps registered. "
            + ("Source HAS CHANGED since the last snapshot — run "
               "ide.inspect.diff_snapshot for the exact files."
               if source_changed
               else ("Source unchanged since last snapshot."
                     if snapshots else "No snapshot taken yet."))
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
            f"Edge types: {', '.join(sorted(set(edge_types))[:5]) or 'none'}."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# COMPOSITE TOPICS SENSOR — the "interesting topics + their entities" layer
# ─────────────────────────────────────────────────────────────────────────────
# Most sensors return a flat, disconnected list. The dream then sees "10 news
# items" + "32 file paths" + "8 memories" as separate piles with no notion of a
# THING to think about. This sensor harvests candidate TOPICS from many signals
# (projects, source changes, research threads, schedule, recurring errors,
# memory clusters), ranks them by interestingness, and for the top N assembles a
# compact ENTITY BUNDLE (linked memories/fabric/files/research) so the agent
# loop gets a complete-but-cheap picture and can WANDER between related things,
# pulling full detail on demand via the named caps.

def _topic_signature(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()[:80]


async def _topic_entities(title: str, limit: int = 6,
                          tags: str = "") -> List[Dict[str, Any]]:
    """Cross-source entity finder for a topic — a few related memory + fabric
    records (bounded, cheap). The agent loop pulls more on demand."""
    ents: List[Dict[str, Any]] = []
    ms = CAPABILITY_REGISTRY.get("memory.search")
    if ms and title:
        try:
            kw = {"query": title, "limit": 3}
            if tags:
                kw["tags"] = tags
            r = await ms["func"](**kw)
            for it in (r or {}).get("results", [])[:3]:
                rec = it.get("record", it) if isinstance(it, dict) else {}
                txt = (rec.get("text") or rec.get("summary") or "")
                if txt and not _is_low_value_memory(rec):
                    ents.append({"kind": "memory", "id": rec.get("id"),
                                 "label": rec.get("category") or "memory",
                                 "snippet": txt[:140]})
        except Exception:
            pass
    fq = CAPABILITY_REGISTRY.get("fabric.query")
    if fq and title:
        try:
            r = await fq["func"](query=json.dumps({
                "text": title, "top_k": 3, "include_data": True, "cache": False}))
            for row in (r or {}).get("results", [])[:3]:
                txt = (row.get("text") or "")
                if txt:
                    ents.append({"kind": "fabric", "id": row.get("id"),
                                 "label": row.get("dataset_id", ""),
                                 "snippet": txt[:140]})
        except Exception:
            pass
    return ents[:limit]


def _topic_card_text(t: Dict[str, Any]) -> str:
    """Render one topic as a compact, complete card for the LLM/agent loop."""
    lines = [f"## TOPIC [{t['type']}] {t['title']}  (interest {t.get('score', 0):.2f})"]
    if t.get("summary"):
        lines.append(t["summary"][:300])
    for e in (t.get("entities") or [])[:8]:
        lines.append(f"  · [{e.get('kind')}] {e.get('label','')}: {e.get('snippet','')}"
                     + (f"  (id={e.get('id')})" if e.get("id") else ""))
    if t.get("pull"):
        lines.append("  → to go deeper: " + "; ".join(t["pull"][:4]))
    return "\n".join(lines)


@capability(
    "dream.sensor.topics", memory="off", silent=True,
    http_method="GET", http_path="/dream/sensor/topics", http_tags=["dream", "sensor"],
    description="Composite 'interesting topics + entities' sensor. Harvests candidate "
                "topics from projects, source changes, research threads, schedule, "
                "recurring errors and memory clusters; ranks by interestingness; and "
                "for the top N assembles a compact entity bundle (related memory/fabric/"
                "files/research) so the dream can think about THINGS, not piles. Params: "
                "top_n (int), types (csv: project,source_change,research,schedule,error,"
                "memory), per_topic_entities (int), hours_back (int).",
)
async def dream_sensor_topics(top_n: int = 6, types: str = "",
                              per_topic_entities: int = 6, hours_back: int = 72,
                              trace_id=None):
    enabled = {t.strip() for t in (types or "").split(",") if t.strip()} or {
        "project", "source_change", "research", "schedule", "error", "memory"}
    cands: List[Dict[str, Any]] = []

    # ── project ──────────────────────────────────────────────────────────────
    if "project" in enabled:
        plist = CAPABILITY_REGISTRY.get("project.list")
        if plist:
            try:
                for p in (await plist["func"]() or {}).get("projects", []):
                    if not isinstance(p, dict):
                        continue
                    ctx = (p.get("summary") or p.get("llm_context")
                           or p.get("user_context") or "").strip()
                    cands.append({
                        "type": "project", "key": f"project:{p.get('slug')}",
                        "title": p.get("name", p.get("slug", "?")),
                        "summary": ctx[:400], "score": 0.8,
                        "slug": p.get("slug"), "tags": f"project:{p.get('slug')}",
                        "pull": [f"project.get(slug='{p.get('slug')}')",
                                 f"project.context.assemble(slug='{p.get('slug')}')"],
                    })
            except Exception as e:
                log.debug("topics/project: %s", e)

    # ── research threads ─────────────────────────────────────────────────────
    if "research" in enabled:
        try:
            rr = await dream_sensor_research_recent(limit=8, full_content_top=2)
            for it in (rr.get("sample") or [])[:6]:
                q = (it.get("query") or it.get("text") or it.get("title") or "").strip()
                if not q:
                    continue
                cands.append({
                    "type": "research", "key": f"research:{_topic_signature(q)}",
                    "title": q[:90], "summary": (it.get("text") or "")[:300],
                    "score": 0.7, "job_id": it.get("id"),
                    "pull": [f"research.job.status(job_id='{it.get('id')}')",
                             "research.expand(...)"],
                })
        except Exception as e:
            log.debug("topics/research: %s", e)

    # ── source changes / unreviewed areas ────────────────────────────────────
    if "source_change" in enabled:
        try:
            srs = await dream_sensor_source_review_state()
            area_counts: Counter = Counter()
            for f in (srs.get("unreviewed_files") or
                      [it.get("file") for it in (srs.get("sample") or [])]):
                if f:
                    area_counts[_source_area(f)] += 1
            sc = await dream_sensor_source_changes()
            changed = bool(sc.get("source_changed"))
            for area, n in area_counts.most_common(4):
                cands.append({
                    "type": "source_change", "key": f"source:{area}",
                    "title": f"Source area '{area}' — {n} unreviewed file(s)"
                             + (" (source changed)" if changed else ""),
                    "summary": (f"{n} files in '{area}' have no current review report"
                                + ("; live source has changed since the last snapshot." if changed else ".")),
                    "score": 0.75 if changed else 0.55, "area": area,
                    "pull": [f"dream.review.area_report(area='{area}')",
                             "ide.inspect.diff_snapshot(...)",
                             f"dream.review.run(area='{area}')"],
                })
        except Exception as e:
            log.debug("topics/source_change: %s", e)

    # ── schedule (upcoming events + open todos) ──────────────────────────────
    if "schedule" in enabled:
        ev_cap = CAPABILITY_REGISTRY.get("cal.events.list")
        td_cap = CAPABILITY_REGISTRY.get("cal.todos.list")
        now = datetime.now(timezone.utc)
        if ev_cap:
            try:
                for e in (await ev_cap["func"]() or {}).get("events", [])[:20]:
                    start = e.get("start", "")
                    try:
                        sdt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
                    except Exception:
                        continue
                    hrs = (sdt - now).total_seconds() / 3600.0
                    if -2 <= hrs <= 24 * 7:   # upcoming week (and just-started)
                        prox = max(0.0, 1.0 - (hrs / (24 * 7))) if hrs > 0 else 0.9
                        cands.append({
                            "type": "schedule", "key": f"event:{e.get('id')}",
                            "title": f"Event: {e.get('title','?')} ({str(start)[:16]})",
                            "summary": (e.get("description") or "")[:200]
                                       + (f" @ {e.get('location')}" if e.get("location") else ""),
                            "score": round(0.5 + 0.4 * prox, 3),
                            "pull": ["cal.events.list()", "cal.notes.list()"],
                        })
            except Exception as e:
                log.debug("topics/schedule events: %s", e)
        if td_cap:
            try:
                for t in (await td_cap["func"]() or {}).get("todos", [])[:20]:
                    if t.get("done"):
                        continue
                    cands.append({
                        "type": "schedule", "key": f"todo:{t.get('id')}",
                        "title": f"Todo: {t.get('title','?')}"
                                 + (f" (due {str(t.get('due'))[:10]})" if t.get("due") else ""),
                        "summary": (t.get("notes") or "")[:200],
                        "score": round(0.55 + 0.1 * int(t.get("priority", 0) or 0), 3),
                        "pull": ["cal.todos.list()"],
                    })
            except Exception as e:
                log.debug("topics/schedule todos: %s", e)

    # ── recurring errors ─────────────────────────────────────────────────────
    if "error" in enabled:
        try:
            se = await dream_sensor_syslog_errors(limit=30)
            sigs: Counter = Counter()
            examples: Dict[str, str] = {}
            for it in (se.get("sample") or []):
                msg = (it.get("text") or it.get("message") or "")
                sig = _topic_signature(msg)
                if sig:
                    sigs[sig] += 1
                    examples.setdefault(sig, msg[:200])
            for sig, n in sigs.most_common(3):
                if n < 2:
                    continue   # only RECURRING errors are interesting
                cands.append({
                    "type": "error", "key": f"error:{sig}",
                    "title": f"Recurring error ×{n}: {examples[sig][:70]}",
                    "summary": examples[sig], "score": min(0.9, 0.5 + n * 0.1),
                    "pull": ["syslog.query(...)", "syslog.errors(limit=50)"],
                })
        except Exception as e:
            log.debug("topics/error: %s", e)

    # ── memory clusters ──────────────────────────────────────────────────────
    if "memory" in enabled:
        try:
            mr = await dream_sensor_memory_recent(limit=25)
            cats: Counter = Counter()
            cat_ex: Dict[str, str] = {}
            for rec in (mr.get("sample") or []):
                c = (rec.get("category") or "uncategorised")
                cats[c] += 1
                cat_ex.setdefault(c, (rec.get("text") or rec.get("summary") or "")[:160])
            for c, n in cats.most_common(3):
                if n < 2:
                    continue
                cands.append({
                    "type": "memory", "key": f"memory:{c}",
                    "title": f"Memory cluster '{c}' ×{n}",
                    "summary": cat_ex.get(c, ""), "score": min(0.6, 0.35 + n * 0.05),
                    "pull": [f"memory.search(query='{c}')", "memory.traverse(...)"],
                })
        except Exception as e:
            log.debug("topics/memory: %s", e)

    # ── rank + bundle entities for the top N ─────────────────────────────────
    cands.sort(key=lambda t: t.get("score", 0), reverse=True)
    top = cands[:max(1, int(top_n))]
    for t in top:
        try:
            t["entities"] = await _topic_entities(
                t["title"], limit=int(per_topic_entities),
                tags=t.get("tags", ""))
        except Exception:
            t["entities"] = []

    sample = [{
        "text": _topic_card_text(t),
        "topic_type": t["type"], "title": t["title"],
        "score": t.get("score", 0), "key": t.get("key"),
        "seed": {k: t[k] for k in ("slug", "area", "job_id") if t.get(k)},
        "pull": t.get("pull", []),
    } for t in top]

    by_type = Counter(t["type"] for t in top)
    signal = min(1.0, len(top) / max(3, int(top_n)))
    return {
        "source": "topics", "count": len(top), "signal": round(signal, 3),
        "sample": sample, "topics": top, "candidates_total": len(cands),
        "summary": (f"{len(top)} interesting topic(s) from {len(cands)} candidates: "
                    + ", ".join(f"{k}×{v}" for k, v in by_type.items())),
    }


@capability(
    "dream.stage.compose_topics", memory="off", silent=True,
    description="Dream stage: assemble the composite topic list (via dream.sensor.topics), "
                "pick a focus topic (seed.focus_topic / topic_type match, else highest "
                "interest), and set state['refined_goal'] so the agent loop WANDERS that "
                "topic + its entities, pulling full detail on demand. Writes state['topics'] "
                "and folds the topic cards into gather for synthesis. Config via "
                "stage_config.compose_topics = {top_n, types, per_topic_entities, topic_type}.",
)
async def dream_stage_compose_topics(state: Optional[Dict[str, Any]] = None, trace_id=None):
    state = state or {}
    trig = state.get("trigger", {})
    seed = state.get("seed") or {}
    cycle_id = state.get("cycle_id", "?")
    cfg = (trig.get("stage_config", {}) or {}).get("compose_topics", {}) or {}

    # 1. Reuse a topics result already in gather, else call the sensor now.
    topics: Optional[List[Dict[str, Any]]] = None
    gres = ((state.get("gather") or {}).get("results") or {})
    for _k, v in gres.items():
        if isinstance(v, dict) and v.get("source") == "topics" and v.get("topics") is not None:
            topics = v.get("topics") or []
            break
    if topics is None:
        res = await dream_sensor_topics(
            top_n=int(cfg.get("top_n", 6)),
            types=cfg.get("types", "") or seed.get("topic_types", ""),
            per_topic_entities=int(cfg.get("per_topic_entities", 6)))
        topics = res.get("topics", [])
        g = state.setdefault("gather", {"results": {}, "signal": 0.0, "sensors": []})
        g.setdefault("results", {})["dream.sensor.topics"] = res
        g["sensors"] = list(g["results"].keys())
        g["signal"] = max(float(g.get("signal", 0) or 0), float(res.get("signal", 0) or 0))
    state["topics"] = topics

    if not topics:
        state["compose_topics"] = {"count": 0, "note": "no topics harvested"}
        return state

    # 2. Pick the focus topic.
    focus = (seed.get("focus_topic") or cfg.get("focus") or "").strip().lower()
    want_type = (seed.get("topic_type") or cfg.get("topic_type") or "").strip()
    chosen = None
    if focus:
        chosen = next((t for t in topics
                       if focus in (t.get("title", "") + " " + t.get("summary", "")).lower()), None)
    if not chosen and want_type:
        chosen = next((t for t in topics if t.get("type") == want_type), None)
    chosen = chosen or topics[0]

    # 3. Build a focused, entity-grounded goal for the agent loop to WANDER.
    ent_lines = "\n".join(
        f"  - [{e.get('kind')}] {e.get('label','')}: {e.get('snippet','')}"
        + (f" (id={e.get('id')})" if e.get("id") else "")
        for e in (chosen.get("entities") or [])[:8])
    pull = "; ".join(chosen.get("pull", [])[:5])
    other = ", ".join(f"{t['type']}:{t['title'][:40]}" for t in topics[1:6])
    impl_hint = ("\nThis is a SOURCE-CHANGE topic: outline concretely HOW you'd implement "
                 "the change (files, approach, risks), pulling only the code you need.\n"
                 if chosen.get("type") == "source_change" else "")
    goal = (
        f"Think about and advance this topic: [{chosen['type']}] {chosen['title']}.\n"
        f"{chosen.get('summary','')}\n"
        f"{impl_hint}\n"
        f"Known associated entities (start here; pull full detail on demand):\n"
        f"{ent_lines or '  (none yet — discover them via the hints below)'}\n\n"
        f"Ways to go deeper: {pull or '(use memory/fabric/research/ide/project caps)'}\n\n"
        f"Adjacent topics you may wander to if they prove more relevant: {other or '(none)'}\n\n"
        "Build the COMPLETE picture with MINIMAL calls — prefer the ids/pull hints above. "
        "Then produce a concrete, specific insight or next action grounded in real data. "
        "Never invent data; cite the entity ids you used."
    )
    state["refined_goal"] = goal
    state["compose_topics"] = {
        "count": len(topics),
        "chosen": {"type": chosen["type"], "title": chosen["title"], "key": chosen.get("key")},
        "focus_topic": chosen["title"],
    }
    state.setdefault("themes", [])
    if chosen["title"] not in state["themes"]:
        state["themes"].insert(0, chosen["title"][:60])

    await emit_event({"type": "dream.compose_topics", "cycle_id": cycle_id,
                      "count": len(topics), "chosen_type": chosen["type"],
                      "chosen": chosen["title"][:80]})
    return state


# ─────────────────────────────────────────────────────────────────────────────
# STAGES
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_collector_result(res: Any, max_items: int = 40) -> Dict[str, Any]:
    """Coerce an arbitrary cap result into the gather working-set shape
    ({count, signal, sample:[{text,...}]}) so collectors from any cap family
    (web.search, fabric, journal, history, research, …) feed the pipeline."""
    if not isinstance(res, dict):
        txt = str(res or "")[:4000]
        items = [{"text": txt}] if txt else []
        return {"count": len(items), "signal": min(1.0, len(items) / 5.0),
                "sample": items}
    if res.get("error"):
        return {"count": 0, "signal": 0.0, "sample": [], "error": res["error"]}
    raw_items: List[Any] = []
    for k in ("sample", "results", "items", "entries", "history", "events",
              "records", "hits"):
        v = res.get(k)
        if isinstance(v, list) and v:
            raw_items = v
            break
    if not raw_items:
        # Single-document results (web.fetch, llm caps, reports)
        for k in ("text", "content", "report", "answer", "summary"):
            if res.get(k):
                raw_items = [{"text": str(res[k])}]
                break
    if not raw_items and res:
        # Status-shaped results (scheduler status, config dumps): keep the
        # whole record as one text item so ops-style collectors still land.
        raw_items = [{"text": json.dumps(res, default=str)[:3000]}]
    sample: List[Dict[str, Any]] = []
    for it in raw_items[:max_items]:
        if isinstance(it, dict):
            rec = it.get("record", it) if isinstance(it.get("record"), dict) else it
            txt = (rec.get("text") or rec.get("snippet") or rec.get("title")
                   or rec.get("content") or rec.get("report") or rec.get("summary")
                   or rec.get("message") or rec.get("note") or rec.get("thought") or "")
            entry: Dict[str, Any] = {"text": str(txt)[:2000]}
            for meta in ("url", "title", "ts", "created_at", "dataset",
                         "category", "trigger", "label"):
                if rec.get(meta) and meta not in entry:
                    entry[meta] = str(rec[meta])[:300]
            if entry["text"] or len(entry) > 1:
                sample.append(entry)
        elif isinstance(it, str) and it.strip():
            sample.append({"text": it[:2000]})
    return {"count": len(sample), "signal": min(1.0, len(sample) / 5.0),
            "sample": sample}


@capability(
    "dream.stage.gather", memory="off", silent=True,
    description="Dream pipeline stage 1: build the cycle's working set. When the "
                "trigger declares `collect` (a list of {cap,args,label} data "
                "collectors) those provide the CONTENT and the trigger's sensors "
                "stay what they should be — cheap firing gates. Without collect, "
                "legacy behaviour: sensors are run for content.",
)
async def dream_stage_gather(state: Optional[Dict[str, Any]] = None, trace_id=None):
    state = state or {}
    trig = state.get("trigger", {})
    seed = state.get("seed") or {}
    sensors = trig.get("sensors") or ["dream.sensor.memory_recent"]
    sensor_params = trig.get("sensor_params") or {}
    results: Dict[str, Any] = {}
    total_signal = 0.0

    # ── Collector-based gather (sensors gate, collectors feed) ──────────────
    # Sensors are trigger gates, not content sources: their samples are thin,
    # truncated probes tuned for firing decisions. A trigger that wants a
    # substantial working set declares `collect`: real data-gathering cap calls
    # (web.search, fabric queries, journal reads, dream.history, …) whose full
    # results become the cycle's material.
    collectors = trig.get("collect") or seed.get("collect") or []
    if isinstance(collectors, dict):
        collectors = [collectors]
    if collectors:
        cycle_id = state.get("cycle_id", "")
        for i, spec in enumerate(collectors):
            if not isinstance(spec, dict) or not spec.get("cap"):
                continue
            cap_name = str(spec["cap"])
            args = spec.get("args") if isinstance(spec.get("args"), dict) else {}
            label = str(spec.get("label") or cap_name)
            max_items = int(spec.get("max_items", 40) or 40)
            await emit_event({"type": "dream.collect", "cycle_id": cycle_id,
                              "cap": cap_name, "label": label, "index": i})
            try:
                res = await _call_cap(cap_name, **args)
            except Exception as e:
                res = {"error": str(e)}
            norm = _normalize_collector_result(res, max_items=max_items)
            norm["source"] = "collector"
            norm["cap"] = cap_name
            results[f"collect:{label}"] = norm
            total_signal += norm.get("signal", 0.0)
        count = max(1, len(results))
        state["gather"] = {
            "sensors":    [],
            "collectors": [str(s.get("label") or s.get("cap") or "?")
                           for s in collectors if isinstance(s, dict)],
            "results":    results,
            "signal":     round(total_signal / count, 3),
        }
        return state

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


# ─────────────────────────────────────────────────────────────────────────────
# STAGE: THINK / REFLECT — the thinking-loop core
# ─────────────────────────────────────────────────────────────────────────────
# Reads the items the gather stage just collected (e.g. new RSS entries), and
# extracts what's genuinely interesting relative to the thought's subject + goal
# AND the broader system context (the recent thought stream + related dream
# memories). Appends a linked entry to the rolling thought stream and leaves
# findings on the state so _persist_cycle_to_memory writes them to the dream
# memory layer, cross-linked to the prior thoughts they build on.

@capability(
    "dream.stage.think_reflect", memory="off", silent=True,
    description="Dream pipeline stage (thinking loop): read the newly-gathered "
                "items, extract anything interesting relative to the thought's "
                "subject + goal AND the broader system context (recent thought "
                "stream + related dream memories), and append a linked entry to the "
                "rolling thought stream. Sets state['findings'], state['report'], "
                "state['title']. Config via stage_config.think_reflect = "
                "{subject, goal, max_items}.",
)
async def dream_stage_think_reflect(state: Optional[Dict[str, Any]] = None, trace_id=None):
    state = state or {}
    trig = state.get("trigger", {})
    cycle_id = state.get("cycle_id", "?")
    journal_id = state.get("journal_id") or cycle_id
    cfg = (trig.get("stage_config", {}) or {}).get("think_reflect", {}) or {}
    seed = state.get("seed") or {}
    subject = (cfg.get("subject") or seed.get("focus_topic")
               or trig.get("label") or trig.get("name") or "this topic")
    goal = cfg.get("goal") or (trig.get("goals") if isinstance(trig.get("goals"), str) else "") or ""
    max_items = int(cfg.get("max_items", 12) or 12)

    await emit_event({"type": "dream.stage.started", "cycle_id": cycle_id,
                      "stage": "dream.stage.think_reflect", "subject": subject})

    # 1) Collect the new items the gather stage surfaced.
    gather = state.get("gather", {})
    results = gather.get("results", {}) if isinstance(gather, dict) else {}
    items: List[Dict[str, Any]] = []
    for _sname, sres in results.items():
        if not isinstance(sres, dict):
            continue
        for it in (sres.get("sample") or []):
            if isinstance(it, dict):
                items.append(it)
            elif isinstance(it, str):
                items.append({"text": it})
    items = items[:max_items]

    if not items:
        await _journal_append(journal_id,
            f"Nothing new to think about on '{subject}' this cycle.",
            kind="thought", stage="think_reflect", title="No new input")
        state["report"] = f"No new items for '{subject}'."
        state["title"] = f"Thoughts on {subject}"
        state.setdefault("themes", [subject])
        # Don't persist/deliver an empty "nothing new" thought — it would just
        # become more noise for memory_recent to re-ingest next cycle.
        state["early_exit"] = {"reason": "no_new_items", "subject": subject}
        return state

    # 2) Broader context: the recent thought stream + related dream memories.
    recent = await _journal_read(journal_id, limit=12, kinds=["thought", "finding"])
    stream_ctx = _journal_to_markdown(recent, heading="Recent thoughts") if recent else ""
    mem_ctx = ""
    related_ids: List[str] = []
    mem_search = CAPABILITY_REGISTRY.get("memory.search")
    if mem_search:
        try:
            sr = await mem_search["func"](query=subject, limit=6, tags="dream")
            lines = []
            for item in (sr or {}).get("results", [])[:6]:
                rec = item.get("record", item) if isinstance(item, dict) else {}
                if rec.get("id"):
                    related_ids.append(rec["id"])
                txt = (rec.get("text") or rec.get("summary") or "")[:200]
                if txt:
                    lines.append(f"- {txt}")
            if lines:
                mem_ctx = "Related things I've noted before:\n" + "\n".join(lines)
        except Exception:
            pass

    # 3) Reflect.
    items_block = "\n\n".join(
        (f"[{i+1}] {it.get('title') or ''}\n{(it.get('text') or '')[:600]}".strip()
         + (f"\n({it.get('link')})" if it.get('link') else ""))
        for i, it in enumerate(items))
    system = (
        "You are Vera, keeping a running journal of thoughts about a subject you "
        "follow over time. You read new items and extract ONLY what is genuinely "
        "interesting or useful given the subject, the goal, and how it connects to "
        "things you already know about the broader system. Be concise and specific. "
        "Skip noise and boilerplate."
    )
    prompt = (
        f"SUBJECT you are following: {subject}\n"
        + (f"GOAL: {goal}\n" if goal else "")
        + (f"\n{stream_ctx}\n" if stream_ctx else "")
        + (f"\n{mem_ctx}\n" if mem_ctx else "")
        + f"\nNEW ITEMS just read:\n{items_block}\n\n"
        "Write a short journal entry (3–6 bullet points) capturing the genuinely "
        "interesting findings and, for each, one line on how it connects to the "
        "subject/goal or to something you already noted. End with a single line "
        "'Thread: …' summarising where your thinking on this subject now stands."
    )
    reflection = (await _llm_generate(prompt, system=system) or "").strip()
    if not reflection:
        reflection = "\n".join(
            f"- {it.get('title') or (it.get('text','') or '')[:120]}" for it in items[:6])

    # 4) Journal + accumulate a finding (persisted to the dream layer, cross-
    #    linked to the related prior memories, by _persist_cycle_to_memory).
    title = f"Thoughts on {subject} — {datetime.now().strftime('%d %b %H:%M')}"
    await _journal_append(journal_id, reflection, kind="thought",
        stage="think_reflect", title=title,
        data={"items": len(items),
              "links": [it.get("link") for it in items if it.get("link")]})

    findings = state.setdefault("findings", [])
    findings.append({
        "topic":      f"thought:{subject}",
        "content":    reflection[:1500],
        "source":     "dream.stage.think_reflect",
        "memory_ids": related_ids,
        "iter":       state.get("iteration_index", 0),
    })
    state["findings"] = findings
    state["report"] = reflection
    state["title"] = title
    state["themes"] = state.get("themes") or [subject]

    await emit_event({"type": "dream.stage.completed", "cycle_id": cycle_id,
                      "stage": "dream.stage.think_reflect",
                      "items": len(items), "chars": len(reflection)})
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
# Runs first in the source_review pipeline. Takes a fresh snapshot if the
# latest is stale (or missing), then diffs it against live source. Stores
# snapshot_id and changed_files in state so the deterministic review stage
# (review_codebase) has concrete file paths to review — a single streaming LLM
# review per file, no agentic tool loop.

@capability(
    "dream.stage.snapshot_source", memory="off", silent=True,
    description="Dream pipeline stage: ensure a fresh source snapshot exists and "
                "diff it against live. Stores snapshot_id, changed_files, and "
                "review_candidates in state for downstream stages. "
                "Place before dream.stage.review_codebase in source_review pipelines.",
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
# with a project-scoped whitelist that includes write caps. For a STRATEGIC
# project it runs as a multi-session orchestration controller (Stream B): read
# the documented plan ledger, pick the single next unfinished portion, execute
# it, and record progress — so a broad goal advances a portion at a time.

def _extract_json_list(raw: str) -> List[Any]:
    """Best-effort parse of a JSON array from an LLM response."""
    if not raw:
        return []
    s, e = raw.find("["), raw.rfind("]")
    if s == -1 or e == -1 or e <= s:
        return []
    try:
        v = json.loads(raw[s:e + 1])
        return v if isinstance(v, list) else []
    except Exception:
        return []


async def _project_origin_plan(project_slug: str) -> str:
    """The documented origin plan for a strategic project. Prefer the escalation
    loop-run's plan; fall back to the plan documented in the project's llm_context
    (so goals created before the loop-history store still orchestrate)."""
    if not project_slug:
        return ""
    cap = CAPABILITY_REGISTRY.get("project.loops.list")
    if cap:
        try:
            lr = await cap["func"](slug=project_slug, limit=12)
            runs = (lr or {}).get("runs", []) if isinstance(lr, dict) else []
            for r in runs:
                if r.get("source") == "escalation" and r.get("plan"):
                    return str(r["plan"])
            for r in runs:
                if r.get("plan"):
                    return str(r["plan"])
        except Exception:
            pass
    # Fallback: the documented strategic plan lives in the project's llm_context.
    gcap = CAPABILITY_REGISTRY.get("project.get")
    if gcap:
        try:
            pr = await gcap["func"](slug=project_slug)
            proj = (pr.get("project") if isinstance(pr, dict) and "project" in pr else pr)
            lc = (proj or {}).get("llm_context", "") if isinstance(proj, dict) else ""
            if lc and "PLAN" in lc.upper():
                return str(lc)
        except Exception:
            pass
    return ""


async def _project_ensure_ledger(project_slug: str, origin_plan: str) -> List[Dict[str, Any]]:
    """Load the plan ledger; decompose the origin plan into ordered portions ONCE
    (idempotent) if it's empty. Returns the portions list."""
    get_cap = CAPABILITY_REGISTRY.get("project.plan.get")
    set_cap = CAPABILITY_REGISTRY.get("project.plan.set")
    if not get_cap or not project_slug:
        return []
    led = await get_cap["func"](slug=project_slug)
    portions = (led or {}).get("portions", [])
    if portions or not origin_plan or not set_cap:
        return portions
    import hashlib
    plan_hash = hashlib.sha1(origin_plan.encode("utf-8", "ignore")).hexdigest()[:12]
    system = (
        "Break a multi-session project plan into 3-8 DISCRETE, ORDERED portions, each "
        "completable in ONE focused work session and each producing a concrete artifact "
        "or decision. Return ONLY a JSON array of {\"title\":\"...\",\"detail\":\"...\"} "
        "objects — no prose, no markdown.")
    raw = await _llm_generate("PLAN:\n" + origin_plan[:4000] + "\n\nPortions JSON array:",
                              system=system)
    parsed = _extract_json_list(raw)
    if not parsed:
        parsed = [{"title": ln.strip()[:200]}
                  for ln in origin_plan.splitlines()
                  if ln.strip() and not ln.strip().startswith("#")][:8]
    if not parsed:
        return portions
    await set_cap["func"](slug=project_slug, portions=json.dumps(parsed), plan_hash=plan_hash)
    led2 = await get_cap["func"](slug=project_slug)
    return (led2 or {}).get("portions", [])


@capability(
    "dream.stage.project_action", memory="off", silent=True,
    description="Dream pipeline stage: execute concrete project actions (not just "
                "propose them). For a strategic project it advances the SINGLE next "
                "unfinished portion of the documented multi-session plan (a v6-style "
                "long-horizon controller); otherwise it runs the refined_goal / "
                "proposed_action. Scoped to the project's resources.",
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

    # ── Multi-session orchestration (Stream B) ──────────────────────────────
    # For a strategic project with a documented plan, advance the SINGLE next
    # unfinished portion of that plan rather than re-deriving a goal each cycle.
    portion: Optional[Dict[str, Any]] = None
    portions: List[Dict[str, Any]] = []
    if project_slug:
        origin_plan = await _project_origin_plan(project_slug)
        if origin_plan:
            portions = await _project_ensure_ledger(project_slug, origin_plan)
            portion = (next((p for p in portions if p.get("status") == "active"), None)
                       or next((p for p in portions if p.get("status") == "pending"), None))

    # Determine what to do — plan portion first, then refined_goal, then proposal
    action_goal = ""
    if portion:
        action_goal = (f"{portion.get('title','')}. {portion.get('detail','')}").strip()
        adv = CAPABILITY_REGISTRY.get("project.plan.advance")
        if adv:
            try:
                await adv["func"](slug=project_slug, portion_id=portion["id"],
                                  status="active", cycle_id=cycle_id)
            except Exception:
                pass
    if not action_goal:
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
    explicit_scope = bool(whitelist)
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
    if explicit_scope:
        # A trigger with its OWN whitelist (e.g. project_compose's memory-
        # centric toolkit) keeps its scope: never force fabric caps back in —
        # fabric fixation on unrelated datasets is exactly what the scoped
        # toolkit exists to prevent.
        project_write_caps = [c for c in project_write_caps
                              if not c.startswith("fabric.")]
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

    # Orchestration framing: make clear this is ONE portion of a longer plan so
    # the loop stays scoped and doesn't try to finish the whole goal in one cycle.
    if portion:
        _done = [p.get("title", "") for p in portions if p.get("status") == "done"][:8]
        goal += (
            f"\n\nThis is portion '{portion.get('id')}' of a documented multi-session "
            f"plan — do ONLY this portion, then stop. "
            + ("Already-completed portions (do NOT redo): "
               + "; ".join(t for t in _done if t) + ". " if _done else ""))

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

    # Run via the configured agent-loop variant (default v5, graceful fallback).
    settings = await _resolve_loop_settings(trig, state)
    cap, engine_name = _resolve_agent_loop_cap(settings)
    if not cap:
        state["project_action"] = {"error": "no agent_loop variant available"}
        return state

    try:
        max_steps = int(trig.get("max_steps", 6))
        # Action stage caps the budget at 8 unless a smaller value is configured
        settings = dict(settings)
        settings["max_cycles"] = min(int(settings.get("max_cycles", max_steps)), 8)
        loop_sid = f"dream:{cycle_id}:project_action"
        # Shared per-project sandbox: every cycle of this goal-project executes
        # in ONE container (goal-<slug>) so its files persist across cycles.
        if project_slug:
            await _sbx_link_loop(loop_sid, f"goal-{project_slug}",
                                 kind="goal", label=project_name)
        norm = await _run_agent_loop(
            goal=goal, allowed_caps=",".join(full_whitelist), settings=settings,
            session_id=loop_sid,
            max_steps=min(max_steps, 8))
        state["project_action"] = {
            "goal": action_goal,
            "engine": engine_name,
            "summary": norm.get("summary", ""),
            "ok": bool(norm.get("summary")) and not norm.get("error"),
        }
        # Merge findings from each step
        if norm.get("steps"):
            existing_findings = state.get("findings", [])
            for st in norm["steps"]:
                existing_findings.append({
                    "source": st.get("cap", "?"),
                    "content": str(st.get("preview", ""))[:500],
                    "action": True,
                })
            state["findings"] = existing_findings
    except Exception as e:
        state["project_action"] = {"error": str(e), "ok": False}

    # ── Record plan progress (Stream B) ─────────────────────────────────────
    if portion and project_slug:
        adv = CAPABILITY_REGISTRY.get("project.plan.advance")
        done_ok = bool((state.get("project_action") or {}).get("ok"))
        if adv:
            try:
                await adv["func"](
                    slug=project_slug, portion_id=portion["id"],
                    status=("done" if done_ok else "pending"),
                    note=((state.get("project_action") or {}).get("summary", "") or "")[:800],
                    cycle_id=cycle_id)
            except Exception:
                pass
        # Re-read for accurate remaining count, then expose it + halt the
        # per-cycle iteration loop when the plan is exhausted.
        get_cap = CAPABILITY_REGISTRY.get("project.plan.get")
        if get_cap:
            try:
                fresh = await get_cap["func"](slug=project_slug)
                portions = (fresh or {}).get("portions", portions)
            except Exception:
                pass
        remaining = sum(1 for p in portions if p.get("status") in ("pending", "active"))
        pa = state.get("project_action") or {}
        pa["portion"] = portion.get("title", "")
        pa["plan_remaining"] = remaining
        pa["plan_total"] = len(portions)
        state["project_action"] = pa
        if remaining <= 0:
            it = state.get("iterate") or {}
            it["stop_requested"] = True
            it["satisfied"] = True
            state["iterate"] = it

    await emit_event({
        "type": "dream.stage.completed", "cycle_id": cycle_id,
        "stage": "dream.stage.project_action",
        "ok": state["project_action"].get("ok", False),
        "project": project_slug,
        "portion": (portion or {}).get("id", ""),
        "plan_remaining": state.get("project_action", {}).get("plan_remaining"),
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
    description="Dream pipeline stage: take inventory of fabric datasets (which "
                "exist, their size, whether an entity graph has been built) and note "
                "cross-dataset entity overlaps. This is READ-ONLY context — it does "
                "NOT extract entities and does not imply the dream should. "
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
            {"text": f"dataset '{u['name']}' — {u['records']} records available to query "
                     f"(no entity graph built yet)",
             "id": u["dataset_id"], "role": "dataset_available"}
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
        "Datasets are RESOURCES you may QUERY to serve a real objective — they are "
        "not chores. Do NOT set a goal about 'processing', 'ingesting', 'extracting "
        "entities from', 'reading', or 'reviewing' a dataset simply because it has no "
        "entity graph or is described as having '0 entities' / 'not processed'. That "
        "background maintenance is handled elsewhere and is never a valuable dream "
        "goal. Only reference a dataset if querying it advances a genuine question. "
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
        + "Build a SHORT DAG (2-4 steps). Do NOT include dream.sensor.* or dream.stage.* caps. "
        + "Datasets are resources to query, not chores: do NOT build a DAG whose purpose is "
        + "to 'process', 'ingest', or 'extract entities from' a dataset just because it lacks "
        + "an entity graph."
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
        # Mark as an early exit so _run_cycle does NOT persist this note to the
        # dream memory layer and the deliver stage skips it. Persisting/​delivering
        # "no signal" diagnostics is the root of the noise feedback loop:
        # memory_recent re-ingests them → the next dream reflects on its own
        # diagnostics → more noise.
        state["early_exit"] = {"reason": "no_signal", "signal": gather_signal}
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

    # ── Optional illustration ────────────────────────────────────────────
    # Trigger opt-in: illustrate=True (+ illustrate_mode: auto|generate|search)
    # produces ONE demonstrative image for the dream's main theme via
    # media.illustrate and appends it to the report as markdown, so every
    # delivery channel (chat/notebook/email/telegram) carries the visual.
    if trig.get("illustrate"):
        try:
            ill_cap = CAPABILITY_REGISTRY.get("media.illustrate")
            if ill_cap:
                subject = (title or ", ".join(themes[:2])
                           or trig.get("label", "") or "the dream's theme")
                ill = await ill_cap["func"](
                    subject=subject[:220],
                    style=trig.get("illustrate_style",
                                   "clean illustrative digital art, muted palette"),
                    mode=(trig.get("illustrate_mode") or "auto"),
                )
                md = (ill or {}).get("markdown", "")
                if md:
                    report = report.rstrip() + "\n\n---\n\n### Illustration\n\n" + md + "\n"
                    state["illustration"] = {
                        "mode_used": ill.get("mode_used", ""),
                        "images": [i.get("url", "") for i in ill.get("images", [])],
                    }
                    await emit_event({"type": "dream.illustrate",
                                      "cycle_id": cycle_id,
                                      "mode_used": ill.get("mode_used", ""),
                                      "subject": subject[:120]})
        except Exception as e:
            log.debug("dream illustrate skipped: %s", e)

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
    project_slug = state.get("project_scope") or seed.get("project_id") or ""

    # Project-scoped runs look in the PROJECT'S OWN material first — its
    # artifacts (generated files/code/reports live there, not in the fabric)
    # and memory; fabric is not offered at all (dataset spelunking is exactly
    # what project dreams kept derailing into).
    if project_slug:
        sources_doc = ('"project" (the project\'s own files/artifacts/generated code — '
                       'USE THIS for any source file, script or prior output), '
                       '"memory", or "web"')
    else:
        sources_doc = '"memory", "fabric", or "web"'
    prompt = (
        f"Goal: {trig.get('prompt', '(no prompt)')}\n"
        + (f"Focus: {focus}\n" if focus else "")
        + (f"Project context:\n{proj_ctx[:1500]}\n\n" if proj_ctx else "")
        + f"Already gathered:\n{summary_text}\n\n"
        + "Identify ONE specific missing piece of information that would meaningfully help. "
        + "Reply with a JSON object:\n"
        + '  {"need": "<short description>", "search_query": "<query>", "source": ' + sources_doc + '}\n'
        + 'Or {"need": null} if nothing meaningful is missing.'
    )
    raw = await _llm_generate(prompt, system="You identify information gaps. JSON only.")
    enriched = []
    try:
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e != -1:
            need = json.loads(raw[s:e+1])
            if need.get("need") and need.get("search_query"):
                src = str(need.get("source", "memory")).lower()
                q = need["search_query"]
                if project_slug and src == "fabric":
                    src = "project"     # fabric is hidden from project dreams
                # ── project: search the project's artifacts by name/content ──
                if src == "project" and project_slug:
                    rows = []
                    try:
                        lister = CAPABILITY_REGISTRY.get("project.artifacts.list")
                        getter = CAPABILITY_REGISTRY.get("project.artifact.get")
                        arts = ((await lister["func"](slug=project_slug, limit=250))
                                .get("artifacts") or []) if lister else []
                        qw = {w for w in re.findall(r"[a-z0-9_.]{3,}", q.lower())}
                        scored = []
                        for a in arts:
                            hay = (str(a.get("name") or "") + " " + str(a.get("path") or "")
                                   + " " + str(a.get("preview") or "")).lower()
                            score = sum(1 for w in qw if w in hay)
                            if score:
                                scored.append((score, a))
                        scored.sort(key=lambda x: -x[0])
                        for _, a in scored[:3]:
                            full = a
                            if getter and a.get("id"):
                                try:
                                    ga = await getter["func"](slug=project_slug, id=a["id"])
                                    if ga.get("ok"):
                                        full = ga["artifact"]
                                except Exception:
                                    pass
                            rows.append({"name": full.get("name"), "type": full.get("type"),
                                         "path": full.get("path"),
                                         "content": (full.get("content") or full.get("preview") or "")[:2500]})
                    except Exception as ex:
                        rows = [{"error": str(ex)}]
                    if not rows:
                        # Nothing matched: fall back to memory so the need
                        # still gets an honest attempt.
                        try:
                            ms = CAPABILITY_REGISTRY.get("memory.search")
                            if ms:
                                res = await ms["func"](query=q, limit=5)
                                rows = (res or {}).get("results", [])[:5]
                        except Exception:
                            pass
                    enriched.append({"need": need["need"], "query": q,
                                     "source": "project", "results": rows[:5]})
                else:
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
    description="Investigation stage. Prompting style is per-stage selectable via "
                "stage_config.investigate.prompt_style = 'one_shot' (default) | "
                "'agent_loop'. one_shot runs a single grounded LLM prompt (no "
                "tools) — right for pure analysis/documentation; agent_loop "
                "delegates to the configured agent-loop variant (default v5; falls "
                "back to v2/v1), running up to max_iterations tool-use cycles. "
                "Both accumulate findings in state['findings']. Caps in "
                "no_hitl_caps skip per-step HITL.",
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
    # Prompting style. Defaults to one_shot (a single grounded LLM prompt) —
    # investigation output is analysis, which doesn't need a tool loop. Opt into
    # the agentic ReAct loop via stage_config.investigate.prompt_style=agent_loop.
    style = _stage_prompt_style(trig, "investigate")

    # Build effective whitelist
    whitelist = trig.get("whitelist") or await _get_whitelist()
    if not whitelist and no_hitl_caps:
        whitelist = list(no_hitl_caps)
    _EXCLUDE = {"dream.", "obs.", "health.", "ui.", "caps.", "mcp.", "echo"}
    whitelist = [
        c for c in whitelist
        if c in CAPABILITY_REGISTRY and not any(c.startswith(p) for p in _EXCLUDE)
    ]
    # A one-shot analysis needs no tools, so an empty whitelist is fine there;
    # only the agentic loop requires caps to work with.
    if not whitelist and style != "one_shot":
        state.setdefault("iterate", {})["stop"] = True
        state["iterate"]["reason"] = "no available caps in whitelist"
        return state

    # Build sensor digest for the goal context
    gather_lines: List[str] = []
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
        if style != "one_shot":
            # One-shot appends its own "write directly" instruction; adding
            # tool-use phrasing here would contradict a no-tools prompt.
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
        if style != "one_shot":
            goal_parts.append(
                "Use the whitelisted capabilities to gather evidence, then stop when you "
                "have substantive findings. Write findings as structured notes — they will "
                "be used by the synthesizer in the next stage."
            )
        goal = "\n\n".join(goal_parts)

    if style == "one_shot":
        # One grounded LLM prompt, no tool loop — right for analysis/documentation.
        engine_name = "one_shot"
        await emit_event({
            "type":      "dream.investigate.start",
            "cycle_id":  cycle_id,
            "trigger":   trig.get("name"),
            "max_cycles": 1,
            "using_engine": "one_shot",
        })
        norm = await _run_oneshot_analysis(goal=goal, state=state,
                                           stage="investigate")
    else:
        # Resolve the configured agent-loop variant (default v5, graceful fallback).
        cfg = await _get_config()
        settings = await _resolve_loop_settings(trig, state)
        settings.setdefault("prefer_gpu", bool(cfg.get("llm_prefer_gpu", True)))
        agent_loop_cap, engine_name = _resolve_agent_loop_cap(settings)
        if not agent_loop_cap:
            # No agent_loop variant loaded — fall back to a simple sequential scan
            log.warning("dream.stage.investigate: no agent_loop variant registered; "
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
            state["iterate"] = {"stop": True, "reason": "fallback: no agent_loop variant", "completed": 1}
            return state

        loop_session_id = f"dream:{cycle_id}:investigate"
        await emit_event({
            "type":      "dream.investigate.start",
            "cycle_id":  cycle_id,
            "trigger":   trig.get("name"),
            "max_cycles": max_cycles,
            "using_engine": engine_name,
        })

        norm = await _run_agent_loop(
            goal=goal, allowed_caps=",".join(whitelist), settings=settings,
            session_id=loop_session_id, max_steps=max_cycles)

    # ── Extract findings from the normalized loop result ────────────────────
    # Each step becomes a finding so the synthesize stage can reference them.
    findings: List[Dict[str, Any]] = list(state.get("findings") or [])
    iterations: List[Dict[str, Any]] = list(state.get("iterations") or [])

    norm_steps = norm.get("steps") or []
    for i, st in enumerate(norm_steps):
        cap_called = st.get("cap") or "?"
        result_preview = str(st.get("preview") or "")[:800]
        thought = str(st.get("reason") or "")[:200]
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
            "ok": bool(st.get("ok")),
        })

    # If the loop produced a summary, add it as a top-level finding
    summary = (norm.get("summary") or "").strip()
    if summary:
        findings.append({
            "topic":   "agent_loop_summary",
            "content": summary[:2000],
            "source":  engine_name,
            "iter":    len(norm_steps),
        })

    state["findings"] = findings
    state["iterations"] = iterations
    state["iterate"] = {
        "stop": True,
        "reason": "agent_loop completed",
        "completed": norm.get("cycles", len(norm_steps)),
        "engine": engine_name,
    }
    if norm.get("error"):
        state["iterate"]["error"] = norm["error"]

    await emit_event({
        "type":      "dream.investigate.complete",
        "cycle_id":  cycle_id,
        "findings":  len(findings),
        "cycles":    state["iterate"]["completed"],
        "summary":   summary[:300] if summary else "",
    })

    return state


# ─────────────────────────────────────────────────────────────────────────────
# STAGE: DELIVER — route the report to the configured delivery channels
# ─────────────────────────────────────────────────────────────────────────────
# Formats whose directive yields markdown-structured prose. The dream report is
# already markdown, so delivering it verbatim is correct and we skip the (LLM)
# reshape for these — only genuinely transformative styles (short/audio/email/
# plain/slides/json) trigger a rewrite pass.
_PASSTHROUGH_FORMATS = {
    "", "markdown", "standard", "report", "docs", "long", "exhaustive",
    "architecture", "improvement", "integration", "critique",
}


async def _reshape_report(report: str, fmt: str) -> str:
    """Re-render a finished report into a delivery channel's output-format style.

    output_formats.apply_format() shapes a *system prompt*, not finished text, so
    to honour a per-channel style (telegram→short, email→email, …) we run one
    cheap LLM pass with the format directive attached. Passthrough/markdown
    formats, an unknown profile, or an unavailable LLM all return the report
    unchanged. The deliver stage caches by format so a format reshapes once.
    """
    fmt = (fmt or "").strip()
    if fmt in _PASSTHROUGH_FORMATS:
        return report
    try:
        from Vera.vera.output_formats import apply_format, get_profile
    except Exception:
        return report
    if not get_profile(fmt):
        return report
    system = apply_format(
        "You reformat an existing report into the requested style WITHOUT adding, "
        "removing or inventing any facts. Output only the reformatted text, with no "
        "preamble.", fmt)
    out = (await _llm_generate(report, system=system) or "").strip()
    return out or report



@capability(
    "dream.stage.deliver", memory="off", silent=True,
    description="Dream pipeline stage 6: route the dream report to the configured "
                "delivery channels (telegram / memory / notebook / email / chat / "
                "skill-defined) from vera.delivery. Each channel renders the report "
                "through its output-format profile before sending; per-channel "
                "format/target overrides live in trigger.deliver_config[channel] = "
                "{format, target}. The data fabric always receives a copy.",
)
async def dream_stage_deliver(state: Optional[Dict[str, Any]] = None, trace_id=None):
    state = state or {}
    trig = state.get("trigger", {})
    report = state.get("report") or ""
    targets = trig.get("deliver_to") or ["memory"]
    deliver_config = trig.get("deliver_config") or {}
    delivered: Dict[str, Any] = {}

    # Don't deliver/persist no-signal or cancelled cycles — delivering a
    # "no usable data" note to memory is exactly what pollutes memory_recent and
    # feeds the next dream more noise. Skip cleanly.
    if state.get("early_exit") or not report.strip():
        state["delivered"] = {"skipped": (state.get("early_exit") or {}).get("reason")
                                          if isinstance(state.get("early_exit"), dict)
                                          else "no_report"}
        return state

    # Shared context the channel argument builders read (see vera/delivery.py).
    ctx_base: Dict[str, Any] = {
        "trigger":  trig,
        "label":    trig.get("label", trig.get("name", "cycle")),
        "name":     trig.get("name", "cycle"),
        "themes":   list(state.get("themes", []) or []),
        "cycle_id": state.get("cycle_id", ""),
    }

    # Notebook gets the running journal appended so it captures the dream's full
    # thinking, not just the final report. This is dream-specific enrichment that
    # can't live in the pure builder, so assemble it once when notebook is picked.
    notebook_md: Optional[str] = None
    if "notebook" in targets and trig.get("journal", True):
        j_entries = await _journal_read(
            state.get("journal_id") or state.get("cycle_id", ""), limit=200)
        if j_entries:
            notebook_md = (report + "\n\n---\n\n"
                           + _journal_to_markdown(j_entries, heading="Dream Journal"))

    # Reshape the report per output-format, caching by format so channels that
    # share a format reshape only once per cycle.
    _rendered_cache: Dict[str, str] = {}
    async def _render(fmt: str) -> str:
        key = (fmt or "").strip()
        if key not in _rendered_cache:
            _rendered_cache[key] = await _reshape_report(report, key)
        return _rendered_cache[key]

    for cid in targets:
        ch = _delivery.get_channel(cid)
        if not ch:
            continue  # unknown id, or 'fabric' (always-on sink handled below)
        cap_name = ch.get("cap") or ""
        if cap_name not in CAPABILITY_REGISTRY:
            delivered[cid] = f"error: cap unavailable ({cap_name})"
            continue
        cfg = deliver_config.get(cid) or {}
        fmt = cfg.get("format") or ch.get("default_format") or ""
        rendered = await _render(fmt)
        ctx = dict(ctx_base, target=(cfg.get("target") or ch.get("fixed_target") or ""))
        if cid == "notebook" and notebook_md:
            ctx["notebook_md"] = notebook_md
        try:
            args = _delivery.build_args(cid, rendered, ctx)
            r = await _call_cap(cap_name, **args)
            if isinstance(r, dict) and r.get("error"):
                delivered[cid] = f"error: {r['error']}"
            elif isinstance(r, dict) and "ok" in r:
                delivered[cid] = bool(r["ok"])
            else:
                delivered[cid] = True
        except Exception as e:
            delivered[cid] = f"error: {e}"

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
    # Yield to the user: stop reviewing (and resume on the next idle slot) when
    # the user becomes active. Defaults ON so a running review never blocks the
    # user taking over. Mirrors _run_deep_review's pause_on_activity behaviour.
    # A forced/manual run ("run now") explicitly bypasses idle checks, so it
    # must NOT yield to activity — otherwise an actively-watching user makes the
    # review yield before reviewing a single file (files_reviewed: 0).
    pause_on_activity = bool(cfg.get("pause_on_activity", True)) and not state.get("force")
    activity_idle_min = float(cfg.get("activity_idle_min",
                                      trig.get("min_idle_minutes", 5)) or 5)

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

    # Publish live progress to the same key the Source Review panel's "Current"
    # area polls (/dream/review/status), so scheduler-driven reviews show up
    # there too — not just the deep-review engine.
    _rc_total = max(1, len(candidates))

    async def _rc_status(**kw):
        r = _redis()
        if not r:
            return
        try:
            cur = {"snapshot_id": snapshot_id, "total": _rc_total,
                   "review_type": review_type, "engine": "review_codebase",
                   "ts": now_iso(), **kw}
            await r.set(KEY_REVIEW_STATUS, json.dumps(cur, default=str))
        except Exception:
            pass

    async def _rc_is_paused() -> bool:
        r = _redis()
        if not r:
            return False
        try:
            v = await r.get(KEY_REVIEW_PAUSE)
            return (v.decode() if isinstance(v, bytes) else v) == "1"
        except Exception:
            return False

    await _rc_status(running=True, done=0, current="", phase="starting")

    results: List[Dict[str, Any]] = []
    high_sev_files: List[str] = []
    reviewed_names: List[str] = []
    total_issues = 0
    findings = state.setdefault("findings", [])
    yielded_reason = ""

    for idx, cand in enumerate(candidates):
        if _CYCLE_CANCEL:
            yielded_reason = "cancelled"
            break

        # Manual pause: stop sending review jobs until resumed or cancelled.
        while await _rc_is_paused() and not _CYCLE_CANCEL:
            await _rc_status(running=True, done=len(results), current="(paused)",
                             phase="paused")
            await asyncio.sleep(3)
        if _CYCLE_CANCEL:
            yielded_reason = "cancelled"
            break

        # Yield to the user: stop early (review resumes on the next idle slot)
        # the moment the user becomes active again. Always review at least one
        # file first (idx > 0) so a perpetually-active user still makes progress
        # rather than every cycle completing with files_reviewed: 0.
        if pause_on_activity and activity_idle_min and idx > 0:
            try:
                if (await _idle_minutes()) < activity_idle_min:
                    yielded_reason = "user active — yielding"
                    break
            except Exception:
                pass

        path = cand.get("file")
        if not path:
            continue
        await _rc_status(running=True, done=len(results),
                         current=f"{path} · review_codebase", phase="reviewing")
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

    # Plan improvements for the worst files (skip the extra LLM work when we
    # yielded early to the user — the continuation review will plan later).
    plan = None
    if do_plan and high_sev_files and yielded_reason != "user active — yielding":
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

    _yielded = bool(yielded_reason) and yielded_reason != "cancelled"
    _remaining = max(0, len(candidates) - len(results))
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
        "incomplete":       _yielded,
        "stopped_reason":   yielded_reason,
        "remaining":        _remaining if _yielded else 0,
    }
    state["findings"] = findings

    # Final status write so the panel's "Current" area clears (running:false)
    # and shows why we stopped.
    await _rc_status(running=False, done=len(results),
                     current="", phase="yielded" if _yielded else "done",
                     generated=len(results), reason=yielded_reason,
                     remaining=_remaining if _yielded else 0)

    if _yielded:
        await _journal_append(journal_id,
            f"Review yielded ({yielded_reason}) after {len(results)} file(s); "
            f"{_remaining} remaining — a continuation review will resume on the "
            f"next idle slot.",
            kind="review", stage="review_codebase", title="Review yielded")
    else:
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
# DREAM → MEMORY LAYER
# ─────────────────────────────────────────────────────────────────────────────
# Persist a dream cycle's graph (cycle, themes, findings) into the SAME memory
# graph the user's memories live in, on a distinct "dream layer" marked by
# source_type="dream". Normal (user-triggered) memories keep their own
# source_type, so the memory graph panel can show/filter each layer
# independently while still letting them link together.
#
# This is gated TWICE, by design:
#   1. per trigger/thought   — trig["persist_to_memory"] (opt-in)
#   2. the cap-activity system — cap_tracking.is_tracked(..., group="dream").
#      The cap-tracking config is the single authority over what reaches the
#      memory graph; dream persistence respects it like every other writer.

def _cap_tracking_allows(session_id: str) -> bool:
    """True if the cap-activity/tracking system permits dream writes to the
    memory graph. Fails open when the tracking module isn't loaded."""
    ct = sys.modules.get("cap_tracking")
    if not ct or not hasattr(ct, "is_tracked"):
        return True
    try:
        return bool(ct.is_tracked("dream.persist_to_memory", "dream", session_id or "dream"))
    except Exception:
        return True


async def _dream_mem_store(text: str, *, category: str, tags: List[str],
                           session_id: str, importance: float = 0.5,
                           summary: str = "", parent_id: str = "") -> str:
    """Store one dream-layer memory record (source_type='dream'). Returns its id
    or '' on failure. Routes through the memory.store capability so it lands in
    every backend (Postgres + Chroma + Neo4j) like any other memory."""
    cap = CAPABILITY_REGISTRY.get("memory.store")
    if not cap:
        return ""
    try:
        res = await cap["func"](
            text=text[:4000], session_id=session_id,
            record_type="event", source_type="dream",
            category=category, tags=",".join(t for t in tags if t),
            summary=summary[:600], human_text=False, ai_output=True,
            importance=importance, parent_id=parent_id,
        ) or {}
        return res.get("id", "") or ""
    except Exception as e:
        log.debug("dream mem store: %s", e)
        return ""


async def _dream_mem_relate(from_id: str, to_id: str, rel: str) -> None:
    if not (from_id and to_id):
        return
    cap = CAPABILITY_REGISTRY.get("memory.relate")
    if not cap:
        return
    try:
        await cap["func"](from_id=from_id, to_id=to_id, relation_type=rel)
    except Exception as e:
        log.debug("dream mem relate: %s", e)


async def _persist_cycle_to_memory(
    state: Dict[str, Any], trig: Dict[str, Any], record: Dict[str, Any],
) -> Dict[str, Any]:
    """Write the dream graph for one cycle to the memory graph's dream layer.

    Creates a cycle node (titled with the synthesis/report), theme nodes and
    finding nodes, links them (cycle -ABOUT-> theme, cycle -FOUND-> finding),
    chains successive cycles of the same journal (FOLLOWED_BY) and cross-links
    findings to any referenced existing memories. No-op unless persistence is
    enabled for this trigger AND the cap-activity system allows it."""
    if not trig.get("persist_to_memory"):
        return {"persisted": False, "reason": "persist_to_memory off"}
    session_id = state.get("session_id") or f"dream:{trig.get('name', 'cycle')}"
    if not _cap_tracking_allows(session_id):
        return {"persisted": False, "reason": "cap-tracking disabled for dream"}

    journal_id = state.get("journal_id") or state.get("cycle_id", "")
    name = trig.get("name", "dream")
    base_tags = ["dream", name]
    if state.get("project_scope"):
        base_tags.append(f"project:{state['project_scope']}")

    title = (record.get("title") or state.get("title")
             or f"Dream cycle · {name}")
    body = (record.get("report") or state.get("report")
            or state.get("synthesis") or title)
    cycle_mid = await _dream_mem_store(
        f"{title}\n\n{body}", category="dream:cycle",
        tags=base_tags + [f"cycle:{state.get('cycle_id','')}"],
        session_id=session_id, importance=0.6, summary=title)
    if not cycle_mid:
        return {"persisted": False, "reason": "store failed"}

    counts = {"cycle": 1, "theme": 0, "finding": 0, "links": 0}

    # Chain to the previous cycle of this journal (train of thought).
    r = _redis()
    if r and journal_id:
        try:
            prev_raw = await r.get(KEY_MEM_LAST_NODE + journal_id)
            prev = (prev_raw.decode() if isinstance(prev_raw, bytes) else prev_raw) if prev_raw else ""
            if prev:
                await _dream_mem_relate(prev, cycle_mid, "FOLLOWED_BY")
                counts["links"] += 1
            await r.set(KEY_MEM_LAST_NODE + journal_id, cycle_mid)
        except Exception:
            pass

    # Theme nodes.
    for theme in (state.get("themes") or [])[:10]:
        ttext = theme.get("label") or theme.get("theme") if isinstance(theme, dict) else str(theme)
        if not ttext:
            continue
        tmid = await _dream_mem_store(str(ttext), category="dream:theme",
                                      tags=base_tags + ["theme"],
                                      session_id=session_id, importance=0.4)
        if tmid:
            counts["theme"] += 1
            await _dream_mem_relate(cycle_mid, tmid, "ABOUT")
            counts["links"] += 1

    # Finding nodes (+ cross-links to referenced existing memories).
    seed = state.get("seed") or {}
    referenced = [m for m in (seed.get("pinned_memory_ids") or []) if isinstance(m, str)]
    for f in (state.get("findings") or [])[:20]:
        if not isinstance(f, dict):
            continue
        ftext = (f.get("content") or f.get("topic") or "").strip()
        if not ftext:
            continue
        fmid = await _dream_mem_store(
            ftext, category="dream:finding",
            tags=base_tags + ["finding"] + ([f.get("source")] if f.get("source") else []),
            session_id=session_id, importance=0.5,
            summary=(f.get("topic") or "")[:200], parent_id=cycle_mid)
        if fmid:
            counts["finding"] += 1
            await _dream_mem_relate(cycle_mid, fmid, "FOUND")
            counts["links"] += 1
            for ref in (f.get("memory_ids") or referenced)[:5]:
                await _dream_mem_relate(fmid, ref, "DERIVED_FROM")
                counts["links"] += 1

    await emit_event({"type": "dream.memory.persisted", "cycle_id": state.get("cycle_id"),
                      "trigger": name, "node": cycle_mid, **counts})
    return {"persisted": True, "cycle_node": cycle_mid, **counts}


# ─────────────────────────────────────────────────────────────────────────────
# CYCLE RUNNER
# ─────────────────────────────────────────────────────────────────────────────

async def _collate_stage_outputs(cycle_id: str, short: str, state: Dict[str, Any]):
    """Persist each stage's material into the cycle's output workspace as it
    completes. This is the file-first collation: gather data, themes, plans and
    findings land on disk (not only in the context window), so long cycles keep
    a durable, inspectable trail and synthesis has a full record to draw on."""
    try:
        if short == "gather" and state.get("gather"):
            await _cycle_file_write(cycle_id, "01-gather.md",
                                    _fmt_gather_markdown(state["gather"]))
        elif short in ("themes", "compose_topics") and state.get("themes"):
            await _cycle_file_write(
                cycle_id, "02-themes.md",
                "# Themes\n\n" + "\n".join(f"- {t}" for t in state.get("themes", [])))
        elif short == "plan" and state.get("plan"):
            await _cycle_file_write(
                cycle_id, "03-plan.md",
                "# Plan\n\n```json\n"
                + json.dumps(state.get("plan"), indent=2, default=str)[:20000]
                + "\n```")
        elif short in ("think_reflect", "investigate", "agent_loop",
                       "stepwise_execute", "project_action", "execute",
                       "memory_deep_traverse", "fabric_explore"):
            findings = state.get("findings") or []
            seen = int(state.get("_collated_findings", 0) or 0)
            fresh = findings[seen:]
            parts: List[str] = []
            it = state.get("iteration_index")
            hdr = f"## {short}" + (f" — iteration {it}" if it else "") \
                  + f" ({now_iso()})"
            body_bits: List[str] = []
            for f in fresh[:80]:
                if isinstance(f, dict):
                    src = f.get("source") or f.get("topic") or ""
                    body_bits.append(f"- {('`' + str(src) + '` ') if src else ''}"
                                     f"{str(f.get('content', ''))[:1500]}")
                elif isinstance(f, str):
                    body_bits.append(f"- {f[:1500]}")
            _st = state.get(f"dream.stage.{short}") or state.get(short) or {}
            if isinstance(_st, dict) and _st.get("summary"):
                body_bits.append(f"\n**Stage summary:** {str(_st['summary'])[:2000]}")
            if body_bits:
                parts.append(hdr + "\n\n" + "\n".join(body_bits) + "\n")
                await _cycle_file_write(cycle_id, "04-findings.md",
                                        "\n".join(parts), append=True)
            state["_collated_findings"] = len(findings)
        elif short == "synthesize" and state.get("report"):
            await _cycle_file_write(cycle_id, "report.md", state["report"])
    except Exception as e:
        log.debug("dream collate %s/%s: %s", cycle_id, short, e)


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

    # A caller (e.g. project.dream.run) may pre-assign the cycle_id so it can
    # register the project->cycle link BEFORE the cycle runs — that's what lets
    # the UI re-attach to this cycle's agentic loops (session `dream:{cid}:{stage}`)
    # while they're still live, instead of only after the cycle finishes.
    if seed.get("cycle_id"):
        cycle_id = str(seed["cycle_id"])[:64]

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
        # force = manual/explicit run (dream.cycle.run, continue, director,
        # pipeline run). These bypass idle checks, so stages must not yield to
        # user activity. Only the scheduler's idle-fired cycles run unforced.
        "force": bool(force),
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
        # Ensure gather has the project sensor AND — critically — its slug
        # param. The param used to be set only when the sensor was ABSENT from
        # the trigger, so triggers that already listed project_context (e.g.
        # project_compose) called it with NO project_slug → the sensor bailed
        # with "project_slug required" and gather came back empty.
        if "dream.sensor.project_context" not in (trig.get("sensors") or []):
            trig["sensors"] = ["dream.sensor.project_context"] + list(trig.get("sensors") or [])
        trig["sensor_params"] = dict(trig.get("sensor_params") or {})
        _pc = dict(trig["sensor_params"].get("project_context") or {})
        _pc["project_slug"] = project_slug
        trig["sensor_params"]["project_context"] = _pc

    await _set_running({
        "cycle_id":   cycle_id,
        "trigger":    trig.get("name"),
        "label":      trig.get("label"),
        "started_at": state["started_at"],
        "pipeline":   pipeline_seed,
        "preview":    preview_only,
        # Manual/explicit runs own the slot; scheduler-fired (unforced) cycles
        # can be preempted by a manual run (see _preempt_for_manual).
        "force":      bool(force),
    })

    await emit_event({
        "type":     "dream.cycle.started",
        "cycle_id": cycle_id,
        "trigger":  trig.get("name"),
        "label":    trig.get("label"),
        "pipeline": pipeline_seed,
        "preview":  preview_only,
        "seed_keys": list(seed.keys()) if seed else [],
    })

    # Seed the poll-able live-progress snapshot for this cycle.
    await _progress_update(cycle_id, {
        "trigger":    trig.get("name"),
        "label":      trig.get("label"),
        "project":    project_slug or "",
        "started_at": state["started_at"],
        "pipeline":   [s.replace("dream.stage.", "") for s in pipeline_seed],
        "status":     "running",
        "preview":    preview_only,
        "stages":     {},
        "files":      [],
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

    # One shared sandbox container for the WHOLE cycle (and for every future
    # cycle of the same goal/pipeline): goal-<slug> or dream-<trigger>. The
    # run-owner scope set around each stage redirects any sandbox use inside
    # the cycle into this container and blocks nested sandbox creation.
    _sbx_owner, _sbx_kind, _sbx_label = _cycle_sandbox_owner(trig, project_slug)

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
        _stage_t0 = time.time()
        _short = stage_name.replace("dream.stage.", "")
        try:
            _idx = pipeline_seed.index(stage_name)
        except ValueError:
            _idx = None
        await _progress_update(cycle_id, {
            "stage": _short, "stage_index": _idx,
            "iteration": state.get("iteration_index"),
            "status": "running",
        }, stage=_short, stage_patch={"status": "running", "started_at": now_iso()})
        # Tag the current cycle/stage so any _llm_generate call nested inside
        # the stage streams its tokens to this cycle's live channel.
        _ctx_tok = _LLM_CTX.set({
            "cycle_id": cycle_id,
            "stage":    stage_name.replace("dream.stage.", ""),
        })
        # BACKGROUND + SANDBOX-OWNER scoping for the whole stage: every LLM
        # call inside it is demoted off the GPU while a human is active, and
        # every sandbox touch (agentic or in-code) lands in this cycle's ONE
        # owner container — nested sandbox creation is redirected there.
        _bg_tok = None
        try:
            _bg_tok = _orch.BACKGROUND_LLM.set(f"dream:{cycle_id}")
        except Exception:
            _bg_tok = None
        _sbxm = _sbx_mod()
        _own_tok = _sbxm.set_run_owner(_sbx_owner, kind=_sbx_kind,
                                       label=_sbx_label) if _sbxm else None
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
        finally:
            _LLM_CTX.reset(_ctx_tok)
            try:
                if _bg_tok is not None:
                    _orch.BACKGROUND_LLM.reset(_bg_tok)
            except Exception:
                pass
            if _sbxm is not None:
                _sbxm.reset_run_owner(_own_tok)
        # Compact per-stage summary — journalled (most dream activity is) AND
        # emitted live. Many stages (synthesize, deliver, themes, gather, …) never
        # self-emit dream.stage.completed, so without this the live panel shows
        # nothing between agent-loop bursts and completed stage activity vanishes.
        # dream.stage.summary lets the panel keep a persistent per-stage log.
        short = stage_name.replace("dream.stage.", "")
        note = _stage_journal_note(short, state)
        _st_res = state.get(stage_name)
        _st_err = isinstance(_st_res, dict) and _st_res.get("error")
        await emit_event({
            "type":      "dream.stage.summary",
            "cycle_id":  cycle_id,
            "stage":     short,
            "summary":   note,
            "ok":        not _st_err,
            "iteration": state.get("iteration_index"),
        })
        await _progress_update(cycle_id, {}, stage=short, stage_patch={
            "status": ("error" if _st_err else "ok"),
            "elapsed_s": round(time.time() - _stage_t0, 1),
            "summary": note[:400],
            "iteration": state.get("iteration_index"),
        })
        if not preview_only:
            await _collate_stage_outputs(cycle_id, short, state)
        if trig.get("journal", True) and not preview_only:
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
        # delegate to the configured agent-loop variant (default v5) via the
        # shared _run_agent_loop helper. We additionally run that block
        # REPEATEDLY here — up to max_iterations — so the dream builds on its own
        # accumulated findings/journal across passes, and we halt early once a
        # pass stops producing materially new findings (convergence) after
        # min_iterations. This is the "stronger iteration".
        state.setdefault("iterations", [])
        state.setdefault("findings", [])
        state["iterate"] = {"enabled": True, "stop": False, "completed": 0}

        _iter_engine = _resolve_agent_loop_cap(
            await _resolve_loop_settings(trig, state))[1] or "agent_loop"
        await emit_event({
            "type":          "dream.iterate.start",
            "cycle_id":      cycle_id,
            "max_iterations": max_iterations,
            "min_iterations": min_iterations,
            "iterate_stages": iter_stages,
            "engine":        _iter_engine,
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
            "engine":                it_state.get("engine", _iter_engine),
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

    # ── Finalize the output workspace ────────────────────────────────────
    # journal.md (the train of thought) + meta.json land beside the stage
    # collation files; the file list rides on the history record so the UI can
    # surface real deliverables for every cycle.
    if not preview_only:
        try:
            j_entries = await _journal_read(state.get("journal_id") or cycle_id,
                                            limit=200)
            if j_entries:
                await _cycle_file_write(
                    cycle_id, "journal.md",
                    _journal_to_markdown(j_entries, heading="Dream journal"))
        except Exception:
            pass
        try:
            await _cycle_file_write(cycle_id, "meta.json", json.dumps({
                "cycle_id": cycle_id, "trigger": trig.get("name"),
                "label": trig.get("label"), "title": record.get("title"),
                "started_at": record.get("started_at"),
                "ended_at": record.get("ended_at"),
                "elapsed_s": record.get("elapsed_s"),
                "themes": record.get("themes"),
                "delivered": record.get("delivered"),
                "pipeline": trig.get("pipeline", []),
            }, indent=2, default=str))
        except Exception:
            pass
    record["files"] = _cycle_files_list(cycle_id)

    await _progress_update(cycle_id, {
        "status":   ("cancelled" if record.get("cancelled")
                     else "early_exit" if record.get("early_exit")
                     else "done"),
        "ended_at": record.get("ended_at") or now_iso(),
        "elapsed_s": record.get("elapsed_s"),
        "title":    record.get("title", ""),
    })

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
        # Output workspace — collation files written during the cycle
        "files":         record.get("files", []),
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

        # ── Persist dream graph to the memory graph's "dream layer" ──────
        # Opt-in per trigger (persist_to_memory) and gated by the cap-activity
        # system, which is the single authority over what reaches the memory
        # graph. Records land with source_type="dream" so they form a distinct,
        # toggleable layer alongside user memories while still cross-linking.
        if not record.get("early_exit"):
            try:
                state["session_id"] = state.get("session_id") or "dream"
                record["memory"] = await _persist_cycle_to_memory(state, trig, record)
            except Exception as e:
                log.debug("dream memory graph: %s", e)

    await _set_running(None)

    # Project hook — if this cycle was scoped to a project, update its rolling
    # context AND record the cycle's structured loop run (steps + report) so the
    # project's loop history / artifact area captures the dream's actual work.
    project_slug = (state.get("seed") or {}).get("project_id") or trig.get("project")
    if project_slug and not preview_only and not early_exit:
        try:
            proj_hook = CAPABILITY_REGISTRY.get("project.dream.complete_hook")
            if proj_hook:
                # Prefer the agent-loop step trace; fall back to stepwise/execute.
                _lp = (state.get("agent_loop") or state.get("stepwise")
                       or state.get("project_action") or {})
                _steps = _lp.get("steps") if isinstance(_lp, dict) else None
                await proj_hook["func"](
                    slug=project_slug,
                    cycle_id=cycle_id,
                    trigger=trig.get("name", ""),
                    report=record.get("report", "") or "",
                    steps=json.dumps(_steps or [], default=str),
                    engine=(_lp.get("engine") if isinstance(_lp, dict) else "") or "dream",
                    goal=state.get("refined_goal", "") or trig.get("label", ""),
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

    # ── Dream-scoped sandbox lifecycle ───────────────────────────────────
    # A plain pipeline's shared container (dream-<trigger>) is put to SLEEP
    # between cycles — its /workspace volume and (when archiving) snapshot
    # survive, and the next cycle of the same pipeline wakes it. Goal/project
    # containers are left to the idle-sleep policy instead (their loops may
    # still be running). No container is ever created just to be slept: the
    # helper checks it exists and is running first.
    if not preview_only and _sbx_kind == "dream":
        async def _sleep_dream_sbx(owner: str = _sbx_owner):
            try:
                st = CAPABILITY_REGISTRY.get("sandbox.session.status")
                slp = CAPABILITY_REGISTRY.get("sandbox.session.sleep")
                if not st or not slp:
                    return
                s = await st["func"](session_id=owner)
                if s.get("exists") and s.get("running"):
                    await slp["func"](session_id=owner)
                    log.info("dream sandbox %s slept after cycle %s", owner, cycle_id)
            except Exception as e:
                log.debug("dream sandbox sleep %s: %s", owner, e)
        asyncio.create_task(_sleep_dream_sbx())

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


# ─────────────────────────────────────────────────────────────────────────────
# DREAM DIRECTOR — the ambient thought orchestrator
# ─────────────────────────────────────────────────────────────────────────────
# A lightweight thinking loop that runs CONTINUOUSLY (even while the user is
# active) on a CPU node — routed via job_type="dream_director" (deny_gpu), so
# it never contends with user-facing GPU work, and BACKING OFF whenever the CPU
# pool is under pressure. It is the director for the dream system:
#   • user ACTIVE  → think about the user's recent activity and current events;
#     worthwhile thoughts are delivered straight into the active chat session.
#   • user IDLE    → think broadly, queue dream candidates, and hand over to the
#     dream scheduler which runs them as full GPU dream cycles.
KEY_DIRECTOR_CFG     = "vera:dream:director:cfg"
KEY_DIRECTOR_QUEUE   = "vera:dream:director:queue"    # LIST of JSON candidates/actions
KEY_DIRECTOR_LAST    = "vera:dream:director:last"     # last thought JSON
KEY_DIRECTOR_THOUGHTS = "vera:dream:director:thoughts" # LIST of recent thought JSON (rolling)
KEY_DIRECTOR_CONV    = "vera:dream:director:conv"     # {"session_id","until"} conversation window
KEY_DIRECTOR_CONVLOG = "vera:dream:director:convlog"  # LIST of recent user↔vera exchanges
DIRECTOR_JOURNAL_ID  = "director"                     # dream journal its thoughts log to

DIRECTOR_DEFAULTS: Dict[str, Any] = {
    "enabled":               True,
    "tick_seconds":          240,    # loop cadence (queue drain + conversation)
    # Ambient THINKING is deliberately less frequent than the tick: a richer
    # thought at most once per this gap, instead of a shallow one every tick.
    # A live conversation bypasses it. 0 = think every tick (old behaviour).
    "think_gap_min":         20.0,
    "active_idle_below_min": 6.0,    # user counts as ACTIVE when idle < this
    "deliver_to_chat":       True,   # push worthwhile thoughts into the chat UI
    "speak":                 True,   # spoken delivery (chat panel synthesises + plays)
    "max_queue":             12,     # queued dream candidates cap
    "thought_memory":        8,      # recent thoughts fed back in for continuity
    "conversation_window_min": 12.0, # how long a user reply keeps conversational mode on
    # ── Persona / when-to-speak (PA feel, not read-aloud notifications) ──────
    "user_name":             "",     # what VERA calls the user (e.g. "Boe"); "" = no name
    "tone":                  "warm", # conversational tone hint (warm/casual/professional/…)
    "only_on_activity":      True,   # only speak proactively when the user is ACTIVE (recent activity)
    "quiet_hours":           "",     # e.g. "22:00-07:00" (server-local); no proactive delivery inside it
    # ── Queue draining ────────────────────────────────────────────────────────
    # Queued actions normally fire at IDLE handover — but an always-active user
    # means they'd never fire at all (they just accumulated). Auto-drain runs
    # the OLDEST queued action once it has waited this long, active or not, as
    # long as no dream cycle is already running. 0 disables.
    "auto_drain_min":        45.0,
    # ── Delivery discipline ──────────────────────────────────────────────────
    "deliver_cooldown_min":  30.0,   # min gap between proactive chat deliveries
    # ── Scope discipline ─────────────────────────────────────────────────────
    # When True (default) the director may ONLY queue/execute actions that
    # advance an EXISTING project (kind=project/think with a target) — it can
    # never spawn tangential, unlinked dream topics or one-off loops that fork
    # new lines of work with no home. This keeps ambient thinking productive
    # instead of proliferating unrelated goals.
    "project_linked_only":   True,
}

_DIRECTOR_TASK: Optional[asyncio.Task] = None
_DIRECTOR_RUN = False
# Wall-clock of the last ambient think, so the director loop can space thinking
# out (think_gap_min) independently of its faster tick. List for in-place mutate.
_LAST_DIRECTOR_THINK: List[float] = [0.0]


async def _director_cfg() -> Dict[str, Any]:
    cfg = dict(DIRECTOR_DEFAULTS)
    r = _redis()
    if r:
        try:
            raw = await r.get(KEY_DIRECTOR_CFG)
            if raw:
                cfg.update(json.loads(raw.decode() if isinstance(raw, bytes) else raw))
        except Exception:
            pass
    return cfg


def _director_addressing(cfg: Dict[str, Any]) -> str:
    """Instruction telling VERA how to address the user, so a proactive thought
    reads like a PA speaking TO them — not a notification being read aloud."""
    name = str(cfg.get("user_name") or "").strip()
    tone = str(cfg.get("tone") or "warm").strip() or "warm"
    who = f'the user (their name is "{name}")' if name else "the user"
    return (f"Speak directly to {who} in a {tone}, natural, conversational tone — like a personal "
            "assistant talking with them, NOT a notification being read aloud. Use first person "
            "(\"I\"), address them as \"you\", use contractions, keep it human and brief. "
            + (f'Use their name ("{name}") when it feels natural — not every time. ' if name else ""))


def _director_in_quiet_hours(cfg: Dict[str, Any]) -> bool:
    """True inside the configured quiet-hours window (suppresses PROACTIVE
    delivery; direct conversation replies are unaffected). Format 'HH:MM-HH:MM'
    in server-local time; supports overnight ranges (22:00-07:00). Empty = off."""
    spec = str(cfg.get("quiet_hours") or "").strip()
    if not spec or "-" not in spec:
        return False
    try:
        a, b = spec.split("-", 1)

        def _mins(s: str) -> int:
            h, m = s.strip().split(":")
            return int(h) * 60 + int(m)

        start, end = _mins(a), _mins(b)
        if start == end:
            return False
        from datetime import datetime
        now = datetime.now().astimezone()
        cur = now.hour * 60 + now.minute
        return (start <= cur < end) if start < end else (cur >= start or cur < end)
    except Exception:
        return False


def _director_cpu_pressure() -> bool:
    """True when the CPU pool has no free node for the director's LLM call —
    the back-off condition (its work is strictly lower priority than anything
    else routed to those nodes)."""
    insts = getattr(_orch, "OLLAMA_INSTANCES", {}) or {}
    cpu = [i for i in insts.values()
           if not i.get("has_gpu") and i.get("enabled", True) and i.get("status") == "online"]
    if not cpu:
        return True
    return all(int(i.get("in_use") or 0) > 0 for i in cpu)


async def _director_recent_activity(limit: int = 14) -> tuple:
    """(activity_lines, latest_chat_session_id) from the recent-caps ring."""
    r = _redis()
    lines: List[str] = []
    latest_sid = ""
    if not r:
        return lines, latest_sid
    try:
        rows = await r.zrevrange(KEY_RECENT_CAPS, 0, 60, withscores=True)
        now_ts = time.time()
        for raw, score in rows or []:
            try:
                rec = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            except Exception:
                continue
            name = str(rec.get("name") or "")
            sid = str(rec.get("sid") or "")
            if sid.startswith("dream") or any(name.startswith(p) for p in _IDLE_IGNORE_PREFIXES):
                continue
            age_m = max(0, int((now_ts - float(score)) / 60))
            if len(lines) < limit:
                lines.append(f"- {name} ({age_m}m ago)")
            if not latest_sid and sid:
                latest_sid = sid
    except Exception:
        pass
    return lines, latest_sid


def _rd(x: Any) -> str:
    return x.decode() if isinstance(x, (bytes, bytearray)) else str(x)


async def _director_loop_snapshot(preferred_sid: str = "") -> Dict[str, str]:
    """A compact view of the agentic loop the user is watching RIGHT NOW — its
    goal, status, current step, last tool and latest reasoning — so the director
    can interleave with live foreground work instead of talking past it. Reads
    the same Redis run/event log the loop UI reattaches to. Returns
    {"sid","summary"} with summary "" when nothing relevant is running."""
    r = _redis()
    if not r:
        return {"sid": "", "summary": ""}

    async def _run(sid: str) -> Dict[str, str]:
        try:
            raw = await r.hgetall(f"vera:loop:run:{sid}")
            return {_rd(k): _rd(v) for k, v in (raw or {}).items()}
        except Exception:
            return {}

    target, run = "", {}
    if preferred_sid:
        run = await _run(preferred_sid)
        if run.get("status") == "running":
            target = preferred_sid
    if not target:
        try:
            ids = await r.zrevrange("vera:loop:sessions", 0, 12)
        except Exception:
            ids = []
        for iid in ids or []:
            sid = _rd(iid)
            rr = await _run(sid)
            if rr.get("status") == "running":
                target, run = sid, rr
                break
    if not target:
        return {"sid": "", "summary": ""}

    try:
        raw = await r.lrange(f"vera:loop:events:{target}", -40, -1)
    except Exception:
        raw = []
    last_step, last_thought, last_tool = "", "", ""
    for x in raw or []:
        try:
            ev = json.loads(_rd(x))
        except Exception:
            continue
        t = str(ev.get("type") or "")
        if t.endswith("step_start") and ev.get("title"):
            last_step = str(ev.get("title"))[:140]
        elif "think" in t and str(ev.get("thought") or "").strip():
            last_thought = str(ev.get("thought")).strip()[:320]
        elif t.endswith("tool_call") and ev.get("tool"):
            last_tool = str(ev.get("tool"))

    parts: List[str] = []
    goal = (run.get("goal") or run.get("title") or "").strip()
    if goal:
        parts.append(f"working on: {goal[:200]}")
    parts.append(f"status: {run.get('status', '?')}"
                 + (f", variant {run.get('variant')}" if run.get("variant") else ""))
    if last_step:
        parts.append(f"current step: {last_step}")
    if last_tool:
        parts.append(f"last tool run: {last_tool}")
    if last_thought:
        parts.append(f"its latest reasoning: “{last_thought}”")
    return {"sid": target, "summary": "\n".join(parts)}


def _text_sig_tokens(s: str) -> set:
    """Lowercased word set (len>3) for cheap thought-similarity checks."""
    import re as _re
    return {w for w in _re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 3}


def _too_similar(candidate: str, prior: List[str], thresh: float = 0.6) -> bool:
    """True if `candidate` overlaps any prior thought above the Jaccard
    threshold — i.e. it's a rephrase/repeat rather than a new observation."""
    ct = _text_sig_tokens(candidate)
    if len(ct) < 4:
        return False
    for p in prior:
        pt = _text_sig_tokens(p)
        if not pt:
            continue
        inter = len(ct & pt)
        union = len(ct | pt) or 1
        if inter / union >= thresh:
            return True
    return False


async def _director_queue_list(limit: int = 20) -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return []
    try:
        raw = await r.lrange(KEY_DIRECTOR_QUEUE, 0, limit - 1)
        out = []
        for it in raw or []:
            try:
                out.append(json.loads(it.decode() if isinstance(it, bytes) else it))
            except Exception:
                pass
        return out
    except Exception:
        return []


_DQ_STOPWORDS = {
    "the", "a", "an", "to", "of", "for", "and", "or", "with", "via", "on", "in",
    "your", "our", "my", "this", "that", "into", "from", "by", "is", "are", "be",
    "you", "we", "it", "its", "as", "at", "now", "new", "next", "current",
    # Domain filler that made every queued item look distinct while meaning the
    # same thing ("Kickstart Crypto MVP" vs "Crypto MVP Kickstart" vs …):
    "kickstart", "activate", "activation", "scope", "scoping", "assess",
    "assessing", "connect", "align", "aligning", "quick", "immediate",
}


def _dq_words(cand: Dict[str, Any]) -> set:
    txt = " ".join(str(cand.get(k) or "") for k in ("topic", "target", "goal", "why")).lower()
    return {w for w in re.findall(r"[a-z0-9]{3,}", txt) if w not in _DQ_STOPWORDS}


async def _director_queue_push(cand: Dict[str, Any], max_queue: int) -> bool:
    """Queue an action — with SEMANTIC dedupe across kinds. Exact-topic matching
    let the director fill the queue with rewordings of one idea ('Crypto MVP
    Scoping' / 'Kickstart Crypto MVP' / 'Crypto Scoping for Income' …); now a
    candidate whose content words substantially overlap ANY queued item is
    rejected regardless of kind."""
    r = _redis()
    if not r:
        return False
    topic = (cand.get("topic") or "").strip()
    if not topic:
        return False
    new_w = _dq_words(cand)
    for c in await _director_queue_list(max_queue):
        if (c.get("topic") or "").strip().lower() == topic.lower():
            return False
        old_w = _dq_words(c)
        if new_w and old_w:
            overlap = len(new_w & old_w) / max(1, len(new_w | old_w))
            if overlap >= 0.45:
                return False
    try:
        cand.setdefault("created", now_iso())
        await r.lpush(KEY_DIRECTOR_QUEUE, json.dumps(cand))
        await r.ltrim(KEY_DIRECTOR_QUEUE, 0, max_queue - 1)
        return True
    except Exception:
        return False


async def _director_queue_pop() -> Optional[Dict[str, Any]]:
    r = _redis()
    if not r:
        return None
    try:
        raw = await r.rpop(KEY_DIRECTOR_QUEUE)     # FIFO: oldest queued first
        if not raw:
            return None
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        return None


async def _director_recent_thoughts(limit: int = 8) -> List[Dict[str, Any]]:
    """Newest-first list of the director's own recent thoughts (for continuity)."""
    r = _redis()
    if not r or limit <= 0:
        return []
    try:
        raw = await r.lrange(KEY_DIRECTOR_THOUGHTS, 0, limit - 1)
        out = []
        for it in raw or []:
            try:
                out.append(json.loads(it.decode() if isinstance(it, bytes) else it))
            except Exception:
                pass
        return out
    except Exception:
        return []


async def _director_thought_push(thought: str, active: bool, keep: int = 8) -> None:
    """Record a thought in the rolling history (best-effort, trimmed to `keep`)."""
    r = _redis()
    thought = (thought or "").strip()
    if not r or not thought:
        return
    try:
        await r.lpush(KEY_DIRECTOR_THOUGHTS,
                      json.dumps({"thought": thought[:500],
                                  "active": bool(active), "ts": now_iso()}))
        await r.ltrim(KEY_DIRECTOR_THOUGHTS, 0, max(0, keep - 1))
    except Exception:
        pass


async def _director_cap_json(name: str, _slice: int = 900, **kw) -> str:
    """Call a cap and return a compact JSON slice of its result ('' on failure)."""
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap or not cap.get("func"):
        return ""
    try:
        res = await cap["func"](**kw)
        if not isinstance(res, (dict, list)):
            return str(res)[:_slice]
        return json.dumps(res, default=str)[:_slice]
    except Exception:
        return ""


async def _director_briefing() -> Dict[str, str]:
    """The director's PERSONAL-ASSISTANT view of the world — calendar, goals,
    projects, the dream schedule and business state. This (not raw system
    logs) is what it thinks about. Every part is best-effort."""
    out: Dict[str, str] = {}
    # Calendar: today + upcoming, todos.
    out["calendar"] = await _director_cap_json("cal.assistant.briefing", 1100)
    # Long-term goals (strategic dream projects).
    goals_txt = ""
    try:
        cap = CAPABILITY_REGISTRY.get("goals.list")
        if cap and cap.get("func"):
            res = await cap["func"]()
            lines = []
            for g in (res or {}).get("goals", [])[:8]:
                lines.append(f"- {g.get('name')} [{g.get('status')}] "
                             f"dreams:{g.get('dream_count')} last:{(g.get('last_dream_at') or 'never')[:16]}"
                             + (f" | {g.get('progress','')[:120]}" if g.get("progress") else ""))
            goals_txt = "\n".join(lines)
    except Exception:
        pass
    out["goals"] = goals_txt
    # Active dream projects (non-goal ones too) + last dream.
    proj_txt = ""
    r = _redis()
    if r:
        try:
            raw_all = await r.hgetall("vera:dream:projects")
            lines = []
            for _, raw in list((raw_all or {}).items())[:20]:
                try:
                    p = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                except Exception:
                    continue
                if (p.get("status") or "active") != "active":
                    continue
                lines.append(f"- {p.get('name')} (slug {p.get('slug')}) dreams:{p.get('dream_count',0)} "
                             f"last:{(p.get('last_dream_at') or 'never')[:16]}")
            proj_txt = "\n".join(lines[:10])
        except Exception:
            pass
    out["projects"] = proj_txt
    # Dream schedule: scheduler state + queued candidates + thinking loops.
    sched_lines: List[str] = []
    try:
        running = await _get_running()
        sched_lines.append(f"cycle running: {running.get('trigger') if running else 'no'}")
    except Exception:
        pass
    try:
        q = await _director_queue_list()
        if q:
            sched_lines.append("queued by you: " + "; ".join(
                f"[{c.get('kind', c.get('mode','dream'))}] {c.get('topic','')[:60]}" for c in q[:8]))
    except Exception:
        pass
    try:
        tl = CAPABILITY_REGISTRY.get("dream.think.list")
        if tl and tl.get("func"):
            res = await tl["func"]()
            names = [t.get("name", "") for t in (res or {}).get("thoughts", res or {}).get("loops", [])
                     if isinstance(t, dict)] if isinstance(res, dict) else []
            if names:
                sched_lines.append("thinking loops: " + ", ".join(n for n in names[:8] if n))
    except Exception:
        pass
    out["dream_schedule"] = "\n".join(sched_lines)
    # V8 loop programs in flight.
    v8_txt = ""
    try:
        cap = CAPABILITY_REGISTRY.get("loops.program.list")
        if cap and cap.get("func"):
            res = await cap["func"](status="active")
            lines = []
            for p in (res or {}).get("programs", [])[:5]:
                # Rich enough for the director to STEER: per-loop status + a slice
                # of the latest run output, so it can spot drift or a repeated
                # operation rather than just seeing "running".
                loop_rows = []
                for l in (p.get("loops") or [])[:8]:
                    lr = l.get("last_run") or {}
                    tail = (" — " + str(lr.get("summary") or "")[:90]) if lr else ""
                    loop_rows.append(f"    · {l.get('name')} [{l.get('status')}]"
                                     f"×{l.get('runs', 0)}{tail}")
                dw = str(p.get("done_when") or "")[:90]
                lines.append(f"- {p.get('name')} ({p.get('id')})"
                             + (f" done_when: {dw}" if dw else "") + "\n"
                             + "\n".join(loop_rows))
            v8_txt = "\n".join(lines)
    except Exception:
        pass
    out["loop_programs"] = v8_txt
    # Business snapshot.
    out["business"] = await _director_cap_json("business.brief", 900)
    return out


async def _director_conv_state() -> Dict[str, Any]:
    """Conversation window: set when the user talks/writes back to the director.
    While active, the director is CONVERSATIONAL (GPU); else REPORTING (CPU)."""
    r = _redis()
    if not r:
        return {}
    try:
        raw = await r.get(KEY_DIRECTOR_CONV)
        st = json.loads(raw.decode() if isinstance(raw, bytes) else raw) if raw else {}
        if st and float(st.get("until") or 0) > time.time():
            return st
    except Exception:
        pass
    return {}


async def _director_convlog(limit: int = 8) -> List[Dict[str, str]]:
    r = _redis()
    if not r:
        return []
    try:
        raw = await r.lrange(KEY_DIRECTOR_CONVLOG, 0, limit - 1)
        out = []
        for it in reversed(raw or []):     # oldest first for the prompt
            try:
                out.append(json.loads(it.decode() if isinstance(it, bytes) else it))
            except Exception:
                pass
        return out
    except Exception:
        return []


async def _director_convlog_push(role: str, text: str) -> None:
    r = _redis()
    if not r or not (text or "").strip():
        return
    try:
        await r.lpush(KEY_DIRECTOR_CONVLOG,
                      json.dumps({"role": role, "text": text.strip()[:800], "ts": now_iso()}))
        await r.ltrim(KEY_DIRECTOR_CONVLOG, 0, 19)
    except Exception:
        pass


async def _director_deliver(thought: str, session_id: str, cfg: Dict[str, Any],
                            title: str = "💭 Vera") -> bool:
    """Deliver a director utterance into chat, optionally spoken (the chat
    panel synthesises + plays when payload.speak is set)."""
    if not (thought and session_id):
        return False
    try:
        ch = CAPABILITY_REGISTRY.get("chat.deliver")
        if ch and ch.get("func"):
            res = await ch["func"](session_id=session_id, report=thought,
                                   title=title, speak=bool(cfg.get("speak", True)),
                                   timeout_secs=4.0)
            return bool((res or {}).get("ok"))
    except Exception as e:
        log.debug("director chat deliver failed: %s", e)
    return False


# Orchestration actions the director may take. Executed on the idle handover
# (except 'program', which self-schedules and is safe to start immediately).
_DIRECTOR_ACTION_KINDS = ("steer", "dream", "project", "think", "loop", "program", "business")


async def _director_active_programs() -> List[Dict[str, str]]:
    """Compact list of ACTIVE V8 programs (id / name / owning goal) so the
    director can recognise one and STEER it instead of spawning parallel work."""
    cap = CAPABILITY_REGISTRY.get("loops.program.list")
    if not cap or not cap.get("func"):
        return []
    try:
        res = await cap["func"](status="active")
    except Exception:
        return []
    out = []
    for p in (res or {}).get("programs", []):
        out.append({"id": str(p.get("id") or ""),
                    "name": str(p.get("name") or "").strip().lower(),
                    "owner_ref": str(p.get("owner_ref") or "").strip().lower()})
    return out


def _director_match_program(entry: Dict[str, Any],
                            progs: List[Dict[str, str]]) -> str:
    """The active program a director action refers to (by id, program name, or
    owning goal slug), or '' when it starts genuinely new work."""
    hay = " ".join(str(entry.get(k) or "") for k in ("target", "topic", "goal")).lower()
    if not hay.strip() or not progs:
        return ""
    for p in progs:                          # exact program id
        if p["id"] and p["id"] in hay:
            return p["id"]
    for p in progs:                          # program name (guard tiny names)
        if p["name"] and len(p["name"]) >= 4 and p["name"] in hay:
            return p["id"]
    for p in progs:                          # owning goal slug
        ref = p["owner_ref"]
        if ref and len(ref) >= 4 and (ref in hay or ref.replace("-", " ") in hay):
            return p["id"]
    return ""


async def _director_queue_action(act: Dict[str, Any], thought: str, max_q: int) -> str:
    kind = str(act.get("kind") or "dream").strip().lower()
    if kind not in _DIRECTOR_ACTION_KINDS:
        kind = "dream"
    entry = {"kind": kind,
             "topic": str(act.get("topic") or act.get("goal") or act.get("target") or "").strip()[:160],
             "target": str(act.get("target") or "").strip()[:120],
             "goal": str(act.get("goal") or "").strip()[:800],
             "why": str(act.get("why") or "").strip()[:300],
             "mode": str(act.get("mode") or "research").strip().lower(),
             "created": now_iso(), "source": "director",
             "context": (thought or "")[:500]}
    if not (entry["topic"] or entry["target"] or entry["goal"]):
        return ""
    # STEER-ONLY over V8 programs: if this action refers to a loop program the
    # orchestrator is ALREADY running (by id / name / owning goal), do NOT spawn
    # a competing loop/dream/program — inject a steering NOTE its controller and
    # loops act on. The dream orchestrator OBSERVES and CORRECTS V8; it never
    # re-runs its work. Explicit kind='steer' always routes here.
    steer_cap = CAPABILITY_REGISTRY.get("loops.program.steer")
    if steer_cap and steer_cap.get("func") \
            and kind in ("steer", "loop", "program", "dream", "project"):
        pid = _director_match_program(entry, await _director_active_programs())
        if pid:
            note = (entry["goal"] or entry["why"] or entry["topic"]).strip()
            if note:
                try:
                    res = await steer_cap["func"](id=pid, note=note, source="director")
                    if isinstance(res, dict) and res.get("ok"):
                        await emit_event({"type": "dream.director.steered",
                                          "program": pid, "note": note[:160]})
                        return f"steer:{pid}"
                except Exception as e:
                    log.debug("director steer failed: %s", e)
        if kind == "steer":
            return ""   # explicit steer, no matching program → nothing to do
    # 'program' actions start immediately — the V8 orchestrator paces itself.
    if kind == "program" and (entry["goal"] or entry["topic"]):
        cap = CAPABILITY_REGISTRY.get("loops.program.create")
        if cap and cap.get("func"):
            try:
                res = await cap["func"](brief=(entry["goal"] or entry["topic"]))
                if isinstance(res, dict) and res.get("id"):
                    return f"program:{res['id']}"
            except Exception as e:
                log.debug("director program create failed: %s", e)
        return ""
    if await _director_queue_push(entry, max_q):
        return f"{kind}:{entry['topic'] or entry['target']}"
    return ""


async def _director_think_once(cfg: Optional[Dict[str, Any]] = None,
                               force: bool = False,
                               user_message: str = "",
                               reply_session: str = "") -> Dict[str, Any]:
    """One director pass. Two modes:
      • REPORTING (default) — CPU-routed ambient thinking over the PA briefing;
        thoughts are journalled, worthwhile ones delivered (and spoken).
      • CONVERSATIONAL — the user talked/wrote back (user_message set, or a
        conversation window is open): GPU-routed, dialogue style."""
    cfg = cfg or await _director_cfg()
    conv = await _director_conv_state()
    conversational = bool(user_message) or bool(conv)
    if not force and not conversational and _director_cpu_pressure():
        await emit_event({"type": "dream.director.backoff",
                          "reason": "CPU pool busy — yielding to foreground work"})
        return {"ok": False, "backoff": True}

    idle = await _idle_minutes()
    active = idle < float(cfg.get("active_idle_below_min", 6.0))
    activity_lines, latest_sid = await _director_recent_activity()
    target_sid = reply_session or conv.get("session_id") or latest_sid
    loop_live = await _director_loop_snapshot(target_sid)
    queued = await _director_queue_list()
    queued_topics = [c.get("topic", "") for c in queued]
    keep_thoughts = int(cfg.get("thought_memory", 8))
    recent_thoughts = await _director_recent_thoughts(keep_thoughts)
    briefing = await _director_briefing()

    if idle >= 9999:
        idle_str = "idle (no recent user activity on record)"
    elif active:
        idle_str = f"active (idle {round(idle,1)}m)"
    else:
        idle_str = f"idle {round(idle,1)}m"

    def _sect(label: str, body: str) -> str:
        return f"{label}:\n{body}\n\n" if (body or "").strip() else ""

    thought_lines = [
        "- " + (t.get("thought") or "").strip()[:220]
        for t in recent_thoughts if (t.get("thought") or "").strip()
    ]
    prior_thought_texts = [(t.get("thought") or "").strip()
                           for t in recent_thoughts if (t.get("thought") or "").strip()]
    ctx = (
        f"USER STATE: {idle_str}\n\n"
        + _sect("CALENDAR & TODOS", briefing.get("calendar", ""))
        + _sect("LONG-TERM GOALS", briefing.get("goals", ""))
        + _sect("ACTIVE PROJECTS", briefing.get("projects", ""))
        + _sect("DREAM SCHEDULE / YOUR QUEUE", briefing.get("dream_schedule", ""))
        + _sect("LOOP PROGRAMS IN FLIGHT (V8)", briefing.get("loop_programs", ""))
        + _sect("BUSINESS SNAPSHOT", briefing.get("business", ""))
        # What the user is doing RIGHT NOW — recent capability activity and the
        # live agentic-loop run — so the director's thought interleaves with the
        # foreground work instead of running on a separate track.
        + _sect("RECENT ACTIVITY (newest first)",
                "\n".join(activity_lines[:10]) if activity_lines else "")
        + _sect("LIVE AGENTIC LOOP (the run the user is watching now)",
                loop_live.get("summary", ""))
        + (("YOUR RECENT THOUGHTS (newest first — treat as one running train of thought; do NOT "
            "repeat or rephrase them, build on them):\n" + "\n".join(thought_lines) + "\n\n")
           if thought_lines else "")
        + ("RULES OF FRESHNESS:\n"
           "- Activity older than ~30 minutes is STALE — never describe it as what the user "
           "is doing right now, and never re-open with the same activity you already "
           "commented on in a recent thought.\n"
           "- An OBSERVATION may be made ONCE. If you already told the user something (a flat "
           "cash flow, a queued idea, a running loop), do not tell them again in new words — "
           "only report a CHANGE in it.\n"
           "- Silence is the normal output: if nothing genuinely NEW has happened since your "
           "last thought, return an empty thought with deliver=false and no actions.\n\n")
    )
    gen = getattr(_orch, "ollama_generate", None)
    if not gen:
        return {"ok": False, "error": "ollama_generate unavailable"}

    # ── CONVERSATIONAL turn: dialogue with the user (GPU-preferred) ─────────
    if conversational and user_message:
        convlog = await _director_convlog()
        dialogue = "\n".join(f"{'USER' if e.get('role')=='user' else 'VERA'}: {e.get('text','')}"
                             for e in convlog)
        sys_p = (
            "You are VERA — the user's personal assistant and the director of her own "
            "background mind (dreams, thinking loops, goals, projects, business ops, loop "
            "programs). You are in CONVERSATION with the user. Reply naturally and concisely, "
            "grounded in the briefing. When the user asks for background work, you MAY attach "
            "actions.\n"
            + _director_addressing(cfg) + "\n"
            'Respond ONLY with JSON: {"reply":"<your conversational reply>",'
            '"actions":[{"kind":"dream|project|think|loop|program|business",'
            '"topic":"<short>","target":"<slug/name if kind is project/think>",'
            '"goal":"<full goal if kind is loop/program/business>","why":"<one line>"}]}\n'
            "actions: 0-2, only when genuinely warranted or requested.")
        prompt = ctx + (f"RECENT CONVERSATION:\n{dialogue}\n\n" if dialogue else "") \
                 + f"USER SAYS: {user_message}\n\nReply JSON."
        try:
            # Conversational replies are interactive — bound the call so a slow
            # node can't hold the exchange (and its generation slot) for the
            # full global OLLAMA_GEN_TIMEOUT budget.
            raw = await gen(prompt, system=sys_p, json_mode=True, prefer_gpu=True,
                            job_type="chat",
                            timeout=float(cfg.get("reply_timeout_s", 300) or 300))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        try:
            obj = json.loads((raw or "{}").strip())
        except Exception:
            import re as _re
            m = _re.search(r"\{.*\}", raw or "", _re.DOTALL)
            obj = json.loads(m.group(0)) if m else {}
        reply = str(obj.get("reply") or "").strip() or "(no reply)"
        fired: List[str] = []
        for act in (obj.get("actions") or [])[:2]:
            if isinstance(act, dict):
                tag = await _director_queue_action(act, reply, int(cfg.get("max_queue", 12)))
                if tag:
                    fired.append(tag)
        await _director_convlog_push("user", user_message)
        await _director_convlog_push("vera", reply)
        r = _redis()
        if r:
            try:
                await r.set(KEY_DIRECTOR_CONV, json.dumps({
                    "session_id": target_sid,
                    "until": time.time() + float(cfg.get("conversation_window_min", 12.0)) * 60}))
            except Exception:
                pass
        delivered = await _director_deliver(reply, target_sid, cfg, title="💬 Vera")
        await _journal_append(DIRECTOR_JOURNAL_ID, f"USER: {user_message}\nVERA: {reply}",
                              kind="thought", stage="conversation", title="conversation")
        out = {"ok": True, "reply": reply, "conversational": True,
               "delivered": delivered, "actions": fired, "session_id": target_sid}
        await emit_event({"type": "dream.director.thought", "thought": reply,
                          "conversational": True, "active": True,
                          "delivered": delivered, "queued": fired,
                          "session_id": target_sid})
        return out

    # ── REPORTING tick: ambient PA thinking (CPU-routed) ────────────────────
    focus = (
        "The user is ACTIVE. Think like a personal assistant looking over their shoulder at "
        "the briefing: imminent calendar items, a goal that has stalled, a project needing a "
        "decision, business numbers moving, a loop program finishing. Only set deliver=true "
        "when the thought would genuinely help RIGHT NOW."
        if active else
        "The system is IDLE. Direct the background mind: which goal/project deserves a dream "
        "cycle, what thinking loop or loop program should advance, what business/maintenance "
        "work is due. Prefer proposing actions over chat messages.")
    prompt = (
        ctx
        + (("YOUR QUEUE ALREADY HOLDS (do not repeat):\n- "
            + "\n- ".join(t for t in queued_topics if t) + "\n\n") if queued_topics else "")
        + "CONTINUITY — this thought must ADVANCE the train of thought above, not restate it:\n"
          "• Never repeat or rephrase a recent thought; react to what CHANGED (new activity, a "
          "loop step finishing, a number moving) or make a genuinely new observation.\n"
          "• If a live agentic loop is shown above, interleave with it — comment on its progress, "
          "flag a risk, or suggest the next move — instead of talking past it.\n"
          "• If nothing has meaningfully changed and you have nothing new to add, set "
          "deliver=false and keep the thought to a brief internal note.\n\n"
        + "Respond ONLY with JSON:\n"
          '{"thought":"<spoken directly to the user in a warm, conversational PA tone — not a '
          'report or a read-aloud notification. Let the LENGTH fit the substance: a quick nudge '
          'can be a single line; a real insight, a risk, or something worth connecting across the '
          'briefing can run a few sentences. Do not pad, and do not force brevity when there is '
          'genuinely more to say>","deliver":true|false,'
          '"actions":[{"kind":"steer|dream|project|think|loop|program|business",'
          '"topic":"<short subject>","target":"<project slug / think-loop name / '
          'program id or name when kind is steer>","goal":"<full goal, or for steer the '
          'CORRECTION text, when kind is loop/program/business/steer>",'
          '"why":"<one line>","mode":"research|reflect"}]}\n'
          "actions: 0-2 orchestration moves — kind 'steer' sends a course-correction to a "
          "V8 loop program ALREADY RUNNING (target=its id/name; goal=the correction) — use it "
          "when a listed program has drifted off its plan or is repeating an operation; you "
          "are OBSERVING and nudging it, not reporting on it, so steer only on a real problem. "
          "'dream' queues a dream topic; 'project' advances a named project; 'think' runs a "
          "thinking loop; 'loop' runs one agentic loop toward a goal; 'program' starts a "
          "LONG-HORIZON V8 loop program; 'business' runs the business operator. DO NOT start a "
          "new loop/dream/program for a goal a listed V8 program already drives — steer that "
          "program instead. Empty list is fine. Ignore routine system noise — only genuinely "
          "NEW problems matter.")
    try:
        # Ambient thinking is low-value background work: bound it well under
        # the global OLLAMA_GEN_TIMEOUT so a big model on a slow CPU node can't
        # occupy the node's generation slot for 15 minutes per thought (which
        # read as "stuck" ollama.generate jobs and queued everything behind it).
        # A timed-out thought is simply skipped; the loop ticks again later.
        raw = await gen(prompt, system=("You are VERA's inner director — the personal-"
                                        "assistant mind that watches the calendar, goals, "
                                        "projects, business and the dream system. "
                                        + _director_addressing(cfg) + " " + focus),
                        json_mode=True, prefer_gpu=False, job_type="dream_director",
                        timeout=float(cfg.get("think_timeout_s", 480) or 480))
    except Exception as e:
        log.debug("director think failed: %s", e)
        return {"ok": False, "error": str(e)}
    try:
        obj = json.loads((raw or "{}").strip())
    except Exception:
        try:
            import re as _re
            m = _re.search(r"\{.*\}", raw or "", _re.DOTALL)
            obj = json.loads(m.group(0)) if m else {}
        except Exception:
            obj = {}
    thought = str(obj.get("thought") or "").strip()
    # When to actually SPEAK up: the model proposed delivery, and it clears the
    # user's when-to-talk preferences — only-on-activity (default) and quiet
    # hours. Failing either keeps the thought (journalled, queued actions still
    # fire) but stays silent instead of interrupting.
    only_on_activity = bool(cfg.get("only_on_activity", True))
    quiet = _director_in_quiet_hours(cfg)
    # Belt-and-braces on top of the prompt's continuity rules: if the model
    # produced a near-rephrase of a recent thought anyway, keep it silent (it is
    # still journalled for continuity, and any queued actions still fire).
    # Threshold 0.45: the observed repeats share the same skeleton with varied
    # wording, which sat just under the old 0.6 bar.
    repeated = _too_similar(thought, prior_thought_texts, thresh=0.45) if thought else False
    # Delivery COOLDOWN: proactive chat messages at most once per
    # deliver_cooldown_min (conversation replies are unaffected — they go
    # through the conversational path).
    cooled = False
    _cool_min = float(cfg.get("deliver_cooldown_min", 30.0) or 0)
    if _cool_min > 0:
        r0 = _redis()
        if r0:
            try:
                raw_ts = await r0.get("vera:dream:director:last_delivery")
                if raw_ts:
                    last_ts = float(raw_ts.decode() if isinstance(raw_ts, bytes) else raw_ts)
                    cooled = (time.time() - last_ts) < _cool_min * 60
            except Exception:
                cooled = False
    deliver = (bool(obj.get("deliver")) and (active or not only_on_activity)
               and not quiet and not repeated and not cooled)
    fired: List[str] = []
    max_q = int(cfg.get("max_queue", 12))
    for act in (obj.get("actions") or (obj.get("dreams") or []))[:2]:
        if isinstance(act, dict):
            tag = await _director_queue_action(act, thought, max_q)
            if tag:
                fired.append(tag)

    delivered = False
    if deliver and thought and cfg.get("deliver_to_chat", True) and target_sid:
        delivered = await _director_deliver(thought, target_sid, cfg,
                                            title="💭 Vera is thinking")
        if delivered:
            r0 = _redis()
            if r0:
                try:
                    await r0.set("vera:dream:director:last_delivery", str(time.time()))
                except Exception:
                    pass

    out = {"ok": True, "thought": thought, "active": active, "idle_minutes": round(idle, 1),
           "delivered": delivered, "queued": fired, "queue_size": len(queued) + len(fired),
           "repeated": repeated, "cooldown_held": cooled,
           "loop_sid": loop_live.get("sid", "")}
    if thought:
        await _director_thought_push(thought, active, keep=keep_thoughts)
        # The director's log lives in the dream direction system: its journal.
        await _journal_append(DIRECTOR_JOURNAL_ID, thought, kind="thought",
                              stage=("active" if active else "idle"),
                              title="director thought",
                              data={"delivered": delivered, "actions": fired})
    r = _redis()
    if r:
        try:
            await r.set(KEY_DIRECTOR_LAST, json.dumps({**out, "ts": now_iso()}))
        except Exception:
            pass
    await emit_event({"type": "dream.director.thought", **out})
    return out


async def _director_auto_drain(cfg: Dict[str, Any]) -> None:
    """Queued actions normally fire at IDLE handover — but a user who is active
    all day means they NEVER fire and the queue just fills to its cap. Once the
    oldest item has waited `auto_drain_min`, execute it now (active or not), as
    long as no dream cycle is already running."""
    drain_min = float(cfg.get("auto_drain_min", 45.0) or 0)
    if drain_min <= 0:
        return
    if _CYCLE_TASK and not _CYCLE_TASK.done():
        return
    items = await _director_queue_list(50)
    if not items:
        return
    oldest = items[-1]
    try:
        created = datetime.fromisoformat(str(oldest.get("created", "")).replace("Z", "+00:00"))
        age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60.0
    except Exception:
        age_min = drain_min + 1        # unparseable timestamp → treat as overdue
    if age_min < drain_min:
        return
    cand = await _director_queue_take(-1)
    if not cand:
        return
    tag = await _director_execute_action(cand, reason=f"auto-drain ({int(age_min)}m queued)")
    if tag is None:
        await _director_queue_push(cand, int(cfg.get("max_queue", 12)))
    else:
        log.info("director auto-drain fired: %s", tag)


async def _director_loop():
    global _DIRECTOR_RUN
    log.info("dream director started")
    await emit_event({"type": "dream.director.started"})
    while _DIRECTOR_RUN:
        try:
            cfg = await _director_cfg()
            tick = max(60, int(cfg.get("tick_seconds", 240)))
            if not cfg.get("enabled", True):
                await asyncio.sleep(tick)
                continue
            # Space ambient thinking out to think_gap_min (richer, less frequent)
            # while the tick keeps draining the queue. A live conversation window
            # always thinks (it's a dialogue, not ambient musing).
            gap_min = float(cfg.get("think_gap_min", 20.0) or 0)
            conv = await _director_conv_state()
            due = (time.time() - _LAST_DIRECTOR_THINK[0]) >= gap_min * 60.0
            if conv or gap_min <= 0 or due:
                res = await _director_think_once(cfg)
                if not (isinstance(res, dict) and res.get("backoff")):
                    _LAST_DIRECTOR_THINK[0] = time.time()
            try:
                await _director_auto_drain(cfg)
            except Exception as e:
                log.debug("director auto-drain: %s", e)
            await asyncio.sleep(tick)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("dream director loop: %s", e)
            await asyncio.sleep(60)
    log.info("dream director stopped")
    await emit_event({"type": "dream.director.stopped"})


@capability(
    "dream.director.status", memory="off", silent=True,
    http_method="GET", http_path="/dream/director/status", http_tags=["dream"],
    description="Dream director (ambient thought orchestrator) status: running, config, "
                "last thought, queued dream candidates, CPU-pressure backoff state.",
)
async def dream_director_status(trace_id=None):
    r = _redis()
    last = None
    if r:
        try:
            raw = await r.get(KEY_DIRECTOR_LAST)
            if raw:
                last = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            pass
    conv = await _director_conv_state()
    return {"running": _DIRECTOR_RUN and bool(_DIRECTOR_TASK) and not (_DIRECTOR_TASK.done() if _DIRECTOR_TASK else True),
            "config": await _director_cfg(),
            "cpu_pressure": _director_cpu_pressure(),
            "conversational": bool(conv),
            "conversation": ({"session_id": conv.get("session_id"),
                              "seconds_left": max(0, int(float(conv.get("until", 0)) - time.time()))}
                             if conv else None),
            "last_thought": last,
            "queue": await _director_queue_list()}


@capability(
    "dream.director.config", memory="off",
    http_method="POST", http_path="/dream/director/config", http_tags=["dream"],
    description="Update dream-director config. Fields (all optional): enabled (bool), "
                "tick_seconds (int — loop cadence for queue-drain/conversation), "
                "think_gap_min (float — minimum minutes between ambient thoughts, so "
                "they're richer and less frequent; a live conversation ignores it), "
                "active_idle_below_min (float), deliver_to_chat (bool), "
                "speak (bool — spoken thought delivery via chat-panel TTS), "
                "conversation_window_min (float — how long a /vera reply keeps the "
                "director conversational/GPU), max_queue (int), thought_memory (int: "
                "recent thoughts fed back for continuity), "
                "user_name (str — what VERA calls you, so thoughts feel like a PA, not a "
                "notification), tone (str — warm/casual/professional/…), only_on_activity "
                "(bool — only speak up proactively when you're active), quiet_hours "
                "(str 'HH:MM-HH:MM' server-local — no proactive delivery inside it). Persists.",
)
async def dream_director_config(enabled: Optional[bool] = None, tick_seconds: Optional[int] = None,
                                active_idle_below_min: Optional[float] = None,
                                deliver_to_chat: Optional[bool] = None,
                                speak: Optional[bool] = None,
                                conversation_window_min: Optional[float] = None,
                                max_queue: Optional[int] = None,
                                thought_memory: Optional[int] = None,
                                user_name: Optional[str] = None,
                                tone: Optional[str] = None,
                                only_on_activity: Optional[bool] = None,
                                quiet_hours: Optional[str] = None,
                                auto_drain_min: Optional[float] = None,
                                deliver_cooldown_min: Optional[float] = None,
                                think_gap_min: Optional[float] = None,
                                trace_id=None):
    cfg = await _director_cfg()
    for k, v in (("enabled", enabled), ("tick_seconds", tick_seconds),
                 ("think_gap_min", think_gap_min),
                 ("active_idle_below_min", active_idle_below_min),
                 ("deliver_to_chat", deliver_to_chat), ("speak", speak),
                 ("conversation_window_min", conversation_window_min),
                 ("max_queue", max_queue),
                 ("thought_memory", thought_memory),
                 ("user_name", user_name), ("tone", tone),
                 ("only_on_activity", only_on_activity), ("quiet_hours", quiet_hours),
                 ("auto_drain_min", auto_drain_min),
                 ("deliver_cooldown_min", deliver_cooldown_min)):
        if v is not None:
            cfg[k] = v
    r = _redis()
    if r:
        try:
            await r.set(KEY_DIRECTOR_CFG, json.dumps(cfg))
        except Exception:
            pass
    return {"ok": True, "config": cfg}


@capability(
    "dream.director.start", memory="off",
    http_method="POST", http_path="/dream/director/start", http_tags=["dream"],
    description="Start the dream director (ambient CPU-side thinking loop).",
)
async def dream_director_start(trace_id=None):
    global _DIRECTOR_TASK, _DIRECTOR_RUN
    if _DIRECTOR_RUN and _DIRECTOR_TASK and not _DIRECTOR_TASK.done():
        return {"running": True, "note": "already running"}
    _DIRECTOR_RUN = True
    _DIRECTOR_TASK = asyncio.create_task(_director_loop())
    return {"running": True}


@capability(
    "dream.director.stop", memory="off",
    http_method="POST", http_path="/dream/director/stop", http_tags=["dream"],
    description="Stop the dream director.",
)
async def dream_director_stop(trace_id=None):
    global _DIRECTOR_TASK, _DIRECTOR_RUN
    _DIRECTOR_RUN = False
    if _DIRECTOR_TASK and not _DIRECTOR_TASK.done():
        _DIRECTOR_TASK.cancel()
        try:
            await asyncio.wait_for(_DIRECTOR_TASK, timeout=3)
        except Exception:
            pass
    _DIRECTOR_TASK = None
    return {"running": False}


@capability(
    "dream.director.think", memory="off",
    http_method="POST", http_path="/dream/director/think", http_tags=["dream"],
    description="Run ONE director thought cycle immediately (ignores CPU backoff). "
                "Output: {ok, thought, active, delivered, queued, queue_size}.",
)
async def dream_director_think(trace_id=None):
    return await _director_think_once(force=True)


@capability(
    "dream.director.reply", memory="on",
    http_method="POST", http_path="/dream/director/reply", http_tags=["dream", "chat"],
    description="Talk / write back to Vera's director. Opens a CONVERSATION window "
                "(configurable minutes): the director replies in dialogue style on the "
                "GPU pool, grounded in its personal-assistant briefing (calendar, goals, "
                "projects, dream schedule, business, loop programs), and may attach "
                "orchestration actions (run a dream / project / thinking loop / agentic "
                "loop / V8 program / business operator). Outside the window it returns to "
                "CPU reporting style. The reply is also delivered (and spoken) into the "
                "chat session. Inputs: message (str!), session_id (str — chat session for "
                "delivery; defaults to the caller's trace/session). "
                "Output: {ok, reply, actions, delivered, session_id}.",
)
async def dream_director_reply(message: str = "", session_id: str = "", trace_id=None):
    if not (message or "").strip():
        return {"ok": False, "error": "message required"}
    sid = (session_id or "").strip() or (str(trace_id) if trace_id else "")
    return await _director_think_once(force=True, user_message=message.strip(),
                                      reply_session=sid)


@capability(
    "dream.director.journal", memory="off", silent=True,
    http_method="GET", http_path="/dream/director/journal", http_tags=["dream"],
    description="The director's thought log — its entries in the dream journal system "
                "(journal id 'director': ambient thoughts, conversations, actions). "
                "Inputs: limit (int default 50). Output: {entries:[{ts,kind,stage,title,"
                "text,data}]}.",
)
async def dream_director_journal(limit: int = 50, trace_id=None):
    entries = await _journal_read(DIRECTOR_JOURNAL_ID, limit=max(1, min(200, int(limit))))
    return {"entries": entries, "count": len(entries)}


@capability(
    "dream.director.queue", memory="off", silent=True,
    http_method="GET", http_path="/dream/director/queue", http_tags=["dream"],
    description="List queued dream candidates the director has proposed (the dream "
                "scheduler consumes these first when the system goes idle).",
)
async def dream_director_queue(trace_id=None):
    return {"queue": await _director_queue_list(50)}


@capability(
    "dream.director.queue_clear", memory="off",
    http_method="POST", http_path="/dream/director/queue/clear", http_tags=["dream"],
    description="Clear the director's queued dream candidates.",
)
async def dream_director_queue_clear(trace_id=None):
    r = _redis()
    if r:
        try:
            await r.delete(KEY_DIRECTOR_QUEUE)
        except Exception:
            pass
    return {"ok": True}


async def _director_queue_take(index: int) -> Optional[Dict[str, Any]]:
    """Remove and return the queue item at `index` (0 = newest, as listed by
    dream.director.queue; -1 = oldest/next-to-fire). Rewrites the list."""
    r = _redis()
    if not r:
        return None
    items = await _director_queue_list(50)
    if not items:
        return None
    if index < 0:
        index = len(items) - 1
    if index >= len(items):
        return None
    taken = items.pop(index)
    try:
        pipe = r.pipeline()
        pipe.delete(KEY_DIRECTOR_QUEUE)
        for it in reversed(items):          # LPUSH restores original order
            pipe.lpush(KEY_DIRECTOR_QUEUE, json.dumps(it))
        await pipe.execute()
    except Exception:
        return None
    return taken


@capability(
    "dream.director.queue_run", memory="off",
    http_method="POST", http_path="/dream/director/queue/run", http_tags=["dream"],
    description="Execute ONE queued director action NOW (no waiting for idle "
                "handover). Inputs: index (int — position as listed by "
                "dream.director.queue; -1 (default) = the oldest item, i.e. next "
                "in line). Output: {ok, fired, remaining} or {ok:false, error}.",
)
async def dream_director_queue_run(index: int = -1, trace_id=None):
    cand = await _director_queue_take(int(index))
    if not cand:
        return {"ok": False, "error": "no queued item at that position"}
    tag = await _director_execute_action(cand, reason="manual run-now")
    if tag is None:
        await _director_queue_push(cand, int((await _director_cfg()).get("max_queue", 12)))
        return {"ok": False, "error": "action could not be executed (requeued)",
                "item": {k: cand.get(k) for k in ("kind", "topic", "target")}}
    return {"ok": True, "fired": tag,
            "remaining": len(await _director_queue_list(50))}


@capability(
    "dream.director.queue_remove", memory="off",
    http_method="POST", http_path="/dream/director/queue/remove", http_tags=["dream"],
    description="Remove ONE queued director action without executing it. "
                "Inputs: index (int — position as listed). Output: {ok, removed}.",
)
async def dream_director_queue_remove(index: int, trace_id=None):
    cand = await _director_queue_take(int(index))
    if not cand:
        return {"ok": False, "error": "no queued item at that position"}
    return {"ok": True, "removed": {k: cand.get(k) for k in ("kind", "topic", "target", "why")}}


async def _director_execute_action(cand: Dict[str, Any], *,
                                   reason: str = "idle handover") -> Optional[str]:
    """Execute ONE queued director action by kind — dream topics become GPU
    dream cycles; project/think/loop/business actions hand over to the matching
    subsystem. Used by the idle handover, the auto-drain, and the panel's
    Run-now button. Returns a 'kind:detail' tag, or None (unexecutable — the
    caller decides whether to requeue)."""
    global _CYCLE_TASK
    kind = str(cand.get("kind") or "dream").strip().lower()
    topic = (cand.get("topic") or "").strip()
    target = (cand.get("target") or "").strip()
    goal = (cand.get("goal") or "").strip() or topic

    # Scope discipline: with project_linked_only on (default), the director may
    # ONLY advance an existing project (kind=project with a valid slug) or run a
    # named project-scoped thinking loop (kind=think with a target). Tangential
    # kinds (a free-floating dream topic, a one-off loop/business goal with no
    # project) are refused — they're exactly the "unrelated goals, no progress"
    # sprawl to prevent. A project.dream.run for a target that no longer exists
    # is also refused.
    try:
        _dcfg = await _director_cfg()
    except Exception:
        _dcfg = {}
    if _dcfg.get("project_linked_only", True):
        linked = (kind == "project" and target) or (kind == "think" and target)
        if not linked:
            log.info("director: refusing unlinked %s action (project_linked_only)", kind)
            await emit_event({"type": "dream.director.refused", "kind": kind,
                              "topic": topic or goal[:80],
                              "reason": "project_linked_only — not tied to a project"})
            return None
        if kind == "project":
            _pg = CAPABILITY_REGISTRY.get("project.get")
            if _pg and _pg.get("func"):
                try:
                    _p = await _pg["func"](slug=target)
                    if not _p or _p.get("error") or not (_p.get("project") or _p.get("slug")):
                        log.info("director: project '%s' not found — refusing", target)
                        return None
                except Exception:
                    pass

    async def _handover_event(detail: str):
        await emit_event({"type": "dream.director.handover", "kind": kind,
                          "topic": topic or target or goal[:80], "detail": detail,
                          "reason": reason})

    # ── project: advance a named dream project ──────────────────────────────
    if kind == "project" and target:
        pd = CAPABILITY_REGISTRY.get("project.dream.run")
        if pd and pd.get("func"):
            await _handover_event(f"project.dream.run {target}")
            _fn = pd["func"]
            _CYCLE_TASK = asyncio.create_task(_fn(slug=target, goal=goal))
            return f"project:{target}"
        return None

    # ── think: run a named thinking loop ────────────────────────────────────
    if kind == "think" and target:
        tr = CAPABILITY_REGISTRY.get("dream.think.run")
        if tr and tr.get("func"):
            await _handover_event(f"dream.think.run {target}")
            _fn = tr["func"]
            _CYCLE_TASK = asyncio.create_task(_fn(name=target))
            return f"think:{target}"
        return None

    # ── loop / business: one specialist agentic-loop run ────────────────────
    if kind in ("loop", "business") and goal:
        lr = CAPABILITY_REGISTRY.get("loops.run")
        profile = "business-shop" if kind == "business" else "planning"
        if lr and lr.get("func"):
            await _handover_event(f"loops.run profile={profile}")
            _fn = lr["func"]
            _CYCLE_TASK = asyncio.create_task(
                _fn(profile=profile, goal=goal,
                    session_id=f"director:{kind}:{int(time.time())}"))
            return f"{kind}:{goal[:60]}"
        return None

    # ── dream (default): a full GPU dream cycle on the topic ────────────────
    trig = None
    for name in ("topic_research", "think", "research_propose", "curiosity"):
        trig = await _get_trigger(name)
        if trig:
            break
    if not trig:
        try:
            for t in await _list_triggers():
                if t.get("enabled") and ("research" in str(t.get("name", ""))
                                         or t.get("kind") == "think"):
                    trig = t
                    break
        except Exception:
            trig = None
    if not trig:
        return None
    seed = {"source": "director", "focus_topic": topic, "topic": topic,
            "extra_prompt": ("Queued by the dream director. WHY: "
                             + str(cand.get("why") or "") + "\nCONTEXT: "
                             + str(cand.get("context") or ""))[:800],
            "mode": cand.get("mode", "research")}
    log.info("dream: handing director candidate to dream system: %s", topic)
    await _handover_event(f"dream.cycle {trig.get('name')}")
    _CYCLE_TASK = asyncio.create_task(_run_cycle(trig, seed=seed))
    return topic


async def _maybe_fire_director_dream(idle_min: float) -> Optional[str]:
    """Idle handover: pop the oldest queued action and execute it."""
    cand = await _director_queue_pop()
    if not cand:
        return None
    tag = await _director_execute_action(cand, reason=f"idle handover ({round(idle_min,1)}m)")
    if tag is None:
        # Unexecutable right now (e.g. no suitable trigger) — requeue quietly.
        await _director_queue_push(cand, int((await _director_cfg()).get("max_queue", 12)))
    return tag


async def _maybe_fire_project_dream(idle_min: float) -> Optional[str]:
    """Background driver for LONG-HORIZON work: an ACTIVE project that carries
    dream_trigger_names (e.g. a V7 strategic master plan persisted as a dream
    project) gets idle dream cycles WITHOUT anyone calling project.dream.run by
    hand — this is what makes multi-session plans actually advance over days.
    Honours the attached trigger's idle/hours/cooldown gates plus the project's
    own last_dream_at cooldown. Fires the single most-starved eligible project.
    Returns the slug fired, or None."""
    global _CYCLE_TASK
    r = _redis()
    pd = CAPABILITY_REGISTRY.get("project.dream.run")
    if not r or not pd or not pd.get("func"):
        return None
    try:
        raw_all = await r.hgetall("vera:dream:projects")
    except Exception:
        return None
    cands: List[tuple] = []
    for k, raw in (raw_all or {}).items():
        try:
            p = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            continue
        if (p.get("status") or "active") != "active" or not p.get("dream_trigger_names"):
            continue
        mins_since = 1e9
        if p.get("last_dream_at"):
            try:
                dt = datetime.fromisoformat(str(p["last_dream_at"]).replace("Z", "+00:00"))
                mins_since = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
            except Exception:
                pass
        trig = None
        try:
            trig = await _get_trigger(str(p["dream_trigger_names"][0]))
        except Exception:
            trig = None
        if trig and not trig.get("enabled", True):
            continue
        if idle_min < float((trig or {}).get("min_idle_minutes", 20)):
            continue
        if mins_since < float((trig or {}).get("min_interval_minutes", 240)):
            continue
        if trig and not _within_hours(int(trig.get("hours_start", 0)),
                                      int(trig.get("hours_end", 24))):
            continue
        slug = p.get("slug") or (k.decode() if isinstance(k, bytes) else str(k))
        cands.append((mins_since, slug))
    if not cands:
        return None
    cands.sort(reverse=True)                    # most-starved project first
    slug = cands[0][1]
    log.info("dream: firing background project dream for '%s' (idle %.1fm)", slug, idle_min)
    await emit_event({"type": "dream.project.autofire", "slug": slug,
                      "idle_minutes": round(idle_min, 1)})
    _fn = pd["func"]
    _CYCLE_TASK = asyncio.create_task(_fn(slug=slug))
    return slug


async def _system_schedule_busy() -> bool:
    """True while the long-term scheduler is running a SYSTEM-side action. Dreams
    stand aside so they never interfere with committed scheduled work. Best-effort
    — any import/lookup failure means 'not busy' (dreams proceed normally)."""
    try:
        sched = (sys.modules.get("longterm_scheduler")
                 or sys.modules.get("Vera.vera.calendar.longterm_scheduler"))
        if sched is None:
            return False
        return bool(await sched.system_schedule_busy())
    except Exception:
        return False


async def _running_background_loops() -> int:
    """Count of background agentic loops (dream stages, v8 program loops) that are
    genuinely live right now — from the shared loop-session store. Stale sessions
    (last event older than the stale window) don't count."""
    r = _redis()
    if not r:
        return 0
    try:
        import Vera.vera.dag.dag_workshop_capabilities as _dw  # for the stale window
        stale = int(getattr(_dw, "_LOOP_STALE_SECS", 600) or 600)
    except Exception:
        stale = 600
    n = 0
    try:
        now = time.time()
        ids = await r.zrevrange("vera:loop:sessions", 0, 200, withscores=True)
        for iid, score in ids or []:
            sid = iid.decode() if isinstance(iid, (bytes, bytearray)) else iid
            if not (sid.startswith("dream:") or sid.startswith("v8:")):
                continue
            if (now - float(score or 0)) > stale:
                continue
            run = await r.hgetall(f"vera:loop:run:{sid}")
            status = (run.get(b"status") or run.get("status") or b"")
            status = status.decode() if isinstance(status, (bytes, bytearray)) else status
            if status == "running":
                n += 1
    except Exception:
        pass
    return n


async def dream_background_allowed() -> Dict[str, Any]:
    """The SHARED gate that decides whether ambient BACKGROUND work (v8 program
    loops, director handovers, ambient dream cycles) may start right now. The
    v8 orchestrator consults this so long-horizon loops obey exactly the same
    activity/idle discipline as the dream scheduler — instead of firing whenever
    they feel like it. Returns a dict with `allowed` + the reasons."""
    try:
        cfg = await _get_config()
    except Exception:
        cfg = {}
    idle = 0.0
    try:
        idle = await _idle_minutes()
    except Exception:
        idle = 0.0
    need = float(cfg.get("min_idle_minutes", 15) or 0)
    human_active = False
    try:
        human_active = bool(getattr(_orch, "defer_background_now", lambda: False)())
    except Exception:
        human_active = False
    busy = False
    try:
        busy = await _system_schedule_busy()
    except Exception:
        busy = False
    enabled = bool(cfg.get("enabled", True))
    running = 0
    try:
        running = await _running_background_loops()
    except Exception:
        running = 0
    reasons = []
    if not enabled:
        reasons.append("dreaming disabled")
    if human_active:
        reasons.append("human active")
    if busy:
        reasons.append("system schedule busy")
    if idle < need:
        reasons.append(f"idle {idle:.1f}<{need:.0f}m")
    allowed = enabled and not human_active and not busy and idle >= need
    return {"allowed": allowed, "reason": "; ".join(reasons) or "ok",
            "idle_minutes": round(idle, 2), "min_idle_minutes": need,
            "human_active": human_active, "system_busy": busy,
            "enabled": enabled, "running_background_loops": running}


@capability(
    "dream.background.status", memory="off", silent=True,
    http_method="GET", http_path="/dream/background/status", http_tags=["dream"],
    description="Whether ambient BACKGROUND work (v8 program loops, director "
                "handovers) may run now under the dream activity gate, and how many "
                "background loops are live. Output: {allowed, reason, idle_minutes, "
                "min_idle_minutes, human_active, system_busy, running_background_loops}.",
)
async def dream_background_status(trace_id=None):
    return await dream_background_allowed()


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

            # Interactive priority: a human actively using the system defers
            # scheduler-fired cycles outright (config: ollama.interactive.set).
            # Belt-and-braces over the idle gate below — this one also sees
            # chat LLM traffic and explicit UI activity pings.
            if getattr(_orch, "defer_background_now", lambda: False)():
                await asyncio.sleep(tick)
                continue

            idle = await _idle_minutes()
            if idle < float(cfg.get("min_idle_minutes", 15)):
                await asyncio.sleep(tick)
                continue

            # Dream exclusion: while a SYSTEM-side long-term-scheduled action is
            # running, dreams stand aside so they never interfere with, or run
            # through, committed scheduled work (see longterm_scheduler).
            if await _system_schedule_busy():
                await asyncio.sleep(tick)
                continue

            # Long-horizon project dreams (persisted strategic plans etc.) get
            # the idle slot FIRST — they are user-committed work, generic
            # curiosity triggers only run when no project is due.
            if await _maybe_fire_project_dream(idle):
                await asyncio.sleep(tick)
                continue

            # Next: candidates the DIRECTOR queued while thinking (the CPU-side
            # thought loop hands over to GPU dreaming here, in idle moments).
            if await _maybe_fire_director_dream(idle):
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
    try:
        bg_loops = await _running_background_loops()
    except Exception:
        bg_loops = 0
    return {
        "scheduler_running": _SCHED_RUN and bool(_SCHED_TASK) and not (_SCHED_TASK.done() if _SCHED_TASK else True),
        "enabled":           bool(cfg.get("enabled")),
        "idle_minutes":      round(idle, 2),
        "min_idle_minutes":  cfg.get("min_idle_minutes"),
        "in_cycle":          bool(running_cycle),
        "current_cycle":     running_cycle,
        # Ambient background loops (v8 programs, project_compose) live right now —
        # surfaced so the top-bar DREAM chip shows work even between cycles.
        "background_loops":  bg_loops,
        "config":            cfg,
    }


async def _preempt_for_manual(reason: str = "manual run") -> Dict[str, Any]:
    """Make room for a manual/explicit cycle. If a *scheduler-fired* (unforced)
    cycle is running, cancel it cooperatively so the manual run takes priority —
    fixes the "a cycle is already running" wall the user hit when triggering a
    source review while a background dream was active. If a *manual* cycle
    already owns the slot, return blocked so we don't stomp it.

    Returns {preempted: bool, blocked?: bool, was?: trigger}."""
    global _CYCLE_TASK, _CYCLE_CANCEL
    if not (_CYCLE_TASK and not _CYCLE_TASK.done()):
        return {"preempted": False}
    running = await _get_running() or {}
    if running.get("force"):
        return {"blocked": True, "running": running.get("trigger")}
    # Cooperative cancel of the scheduled cycle, then hard cancel + bounded wait.
    _CYCLE_CANCEL = True
    try:
        _CYCLE_TASK.cancel()
    except Exception:
        pass
    try:
        await asyncio.wait_for(asyncio.shield(_CYCLE_TASK), timeout=5)
    except Exception:
        pass
    _CYCLE_CANCEL = False
    await _set_running(None)
    await emit_event({"type": "dream.cycle.preempted", "reason": reason,
                      "preempted_trigger": running.get("trigger")})
    log.info("dream: preempted scheduled cycle %s for %s",
             running.get("trigger"), reason)
    return {"preempted": True, "was": running.get("trigger")}


@capability(
    "dream.cycle.run", memory="off",
    http_method="POST", http_path="/dream/cycle/run", http_tags=["dream"],
    description="Manually run a dream cycle for a named trigger. Bypasses idle/hours/cooldown checks. "
                "A manual run preempts any scheduler-fired cycle in flight (manual wins). "
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
    pre = await _preempt_for_manual(f"manual run: {trigger_name}")
    if pre.get("blocked"):
        return {"ok": False, "error": f"a manual cycle is already running "
                f"({pre.get('running')}); try again shortly"}
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
    pre = await _preempt_for_manual(f"manual continue: {cycle_id}")
    if pre.get("blocked"):
        return {"ok": False, "error": f"a manual cycle is already running "
                f"({pre.get('running')}); try again shortly"}

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
    deliver_config: Optional[Dict[str, Any]] = None,   # {channel: {format, target}} per-channel overrides
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
    persist_to_memory: Optional[bool]       = None,   # persist this dream's graph to the memory "dream layer"
    flow_graph:    Optional[Dict[str, Any]] = None,   # full <vera-flow-builder> canvas graph (editor state — caps/wiring/conditions; the cycle runner ignores it and uses sensors/pipeline)
    collect:       Optional[List[Dict[str, Any]]] = None,  # data collectors [{cap,args,label}] — gather CONTENT (sensors then only gate firing)
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
        "deliver_to": deliver_to, "deliver_config": deliver_config, "prompt": prompt,
        "sensor_params": sensor_params, "stage_params": stage_params,
        "whitelist": whitelist, "no_hitl_caps": no_hitl_caps,
        "depth": depth, "max_steps": max_steps,
        "director_managed": director_managed,
        # v4
        "stage_config": stage_config, "iterate": iterate, "pivot": pivot,
        "loop_settings": loop_settings, "handover": handover, "journal": journal,
        "max_continuation_depth": max_continuation_depth, "project": project,
        "pipeline_ref": pipeline_ref, "goals": goals,
        "persist_to_memory": persist_to_memory, "flow_graph": flow_graph,
        "collect": collect,
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
# CAPABILITIES — THINKING LOOPS (dream.think.*)
# ─────────────────────────────────────────────────────────────────────────────
# A "thought" is a first-class wrapper over the trigger/pipeline machinery: point
# Vera at a subject + goal + source and it reflects on new material each idle
# slot, keeping a rolling, linked thought stream persisted to the dream memory
# layer. Under the hood each thought is a trigger with pipeline_ref="think".

def _think_slug(subject: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (subject or "").lower()).strip("_")[:40]
    return f"think_{s or uuid.uuid4().hex[:6]}"


def _is_think_trigger(trig: Dict[str, Any]) -> bool:
    return bool(trig) and (trig.get("pipeline_ref") == "think"
                           or trig.get("kind") == "think")


@capability(
    "dream.think.create", memory="off",
    http_method="POST", http_path="/dream/think/create", http_tags=["dream", "think"],
    description="Create a thinking loop: point Vera at a subject + goal + source and "
                "it reflects on new material each idle slot, keeping a rolling, linked "
                "thought stream persisted to the dream memory layer. "
                "Inputs: subject (str!), goal (str), source (str — an RSS/web URL, a "
                "sensor id like 'dream.sensor.memory_graph_walk', or '' for a "
                "self-reflective topic), idle_minutes (int, default 20), "
                "interval_minutes (int, default 120), name (str — auto from subject), "
                "enabled (bool, default true).",
)
async def dream_think_create(
    subject: str,
    goal: str = "",
    source: str = "",
    idle_minutes: int = 20,
    interval_minutes: int = 120,
    name: str = "",
    enabled: bool = True,
    project_slug: str = "",
    trace_id=None,
):
    if not subject or not subject.strip():
        return {"ok": False, "error": "subject required"}
    subject = subject.strip()
    name = name.strip() or _think_slug(subject)
    src = (source or "").strip()
    project_slug = (project_slug or "").strip()

    # Resolve the source into a sensor + params.
    sensors: List[str] = []
    sensor_params: Dict[str, Any] = {}
    source_kind = "topic"
    if src.startswith(("http://", "https://")) or re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", src, re.I):
        sensors = ["dream.sensor.web_feed"]
        sensor_params = {"web_feed": {"url": src, "feed_id": name, "limit": 25}}
        source_kind = "feed"
    elif src in CAPABILITY_REGISTRY or f"dream.sensor.{src}" in CAPABILITY_REGISTRY \
            or src in SENSOR_REGISTRY:
        sid = src if src.startswith("dream.sensor.") or src.startswith("custom.") else (
            src if src in CAPABILITY_REGISTRY else f"dream.sensor.{src}")
        sensors = [sid]
        source_kind = "sensor"
    else:
        # Plain topic: ground the thinking loop in subject-scoped material from
        # memory + the data fabric + the web (surfacing only what's new each
        # slot) rather than re-reading the raw recent-memory dump.
        sensors = ["dream.sensor.topic_research"]
        sensor_params = {"topic_research": {"subject": subject, "use_fabric": True,
                                            "use_web": True, "limit": 20,
                                            "feed_id": name}}
        source_kind = "topic"

    res = await dream_trigger_upsert(
        name=name,
        label=f"Thought · {subject[:50]}",
        description=(f"Thinking loop on '{subject}'"
                     + (f" — {src}" if src else "")),
        enabled=enabled,
        sensors=sensors,
        sensor_params=sensor_params,
        pipeline_ref="think",
        persist_to_memory=True,
        journal=True,
        require_signal=0.0,
        min_idle_minutes=int(idle_minutes or 20),
        min_interval_minutes=int(interval_minutes or 120),
        goals=goal or f"Surface what's genuinely interesting about {subject}.",
        project=project_slug or None,
        stage_config={"think_reflect": {"subject": subject, "goal": goal,
                                        "max_items": 12}},
    )
    if not res.get("ok"):
        return res
    # The thought is scoped to the project via the trigger's `project` field
    # (set above). project.thoughts.list filters on it, so the project's own
    # Thoughts area is self-maintaining — no mutation of the project record.
    return {"ok": True, "name": name, "subject": subject, "goal": goal,
            "source": src, "source_kind": source_kind,
            "project_slug": project_slug,
            "journal_id": f"trigger:{name}",
            "trigger": res.get("trigger"),
            "note": "thinking loop created; runs on idle or via dream.think.run"}


@capability(
    "dream.think.list", memory="off", silent=True,
    http_method="GET", http_path="/dream/think/list", http_tags=["dream", "think"],
    description="List all thinking loops (triggers using the think pipeline) with "
                "their subject, source, cadence and last run. Optional project_slug "
                "filters to one project's thought area ('' = all thoughts).",
)
async def dream_think_list(project_slug: str = "", trace_id=None):
    project_slug = (project_slug or "").strip()
    triggers = await _list_triggers()
    out: List[Dict[str, Any]] = []
    for t in triggers:
        if not _is_think_trigger(t):
            continue
        if project_slug and (t.get("project") or "") != project_slug:
            continue
        sc = (t.get("stage_config", {}) or {}).get("think_reflect", {}) or {}
        sp = (t.get("sensor_params", {}) or {}).get("web_feed", {}) or {}
        out.append({
            "name":     t.get("name"),
            "subject":  sc.get("subject") or t.get("label", ""),
            "goal":     sc.get("goal", ""),
            "source":   sp.get("url", "") or ", ".join(t.get("sensors", [])),
            "project":  t.get("project") or "",
            "enabled":  t.get("enabled", False),
            "idle_minutes":     t.get("min_idle_minutes"),
            "interval_minutes": t.get("min_interval_minutes"),
            "last_run": await _last_run_ts(t.get("name", "?")),
        })
    return {"thoughts": out, "count": len(out)}


@capability(
    "dream.think.delete", memory="off",
    http_method="POST", http_path="/dream/think/delete", http_tags=["dream", "think"],
    description="Delete a thinking loop by name. Also clears its seen-feed cursor.",
)
async def dream_think_delete(name: str, trace_id=None):
    await _delete_trigger(name)
    r = _redis()
    if r:
        try:
            await r.delete(f"vera:dream:feed:seen:{name}")
        except Exception:
            pass
    return {"ok": True, "deleted": name}


@capability(
    "dream.think.run", memory="off",
    http_method="POST", http_path="/dream/think/run", http_tags=["dream", "think"],
    description="Run a thinking loop now (bypasses idle/interval gates), so it reads "
                "its source and adds to the thought stream immediately.",
)
async def dream_think_run(name: str, trace_id=None):
    return await dream_cycle_run(trigger_name=name)


@capability(
    "dream.think.stream", memory="off", silent=True,
    http_method="GET", http_path="/dream/think/stream", http_tags=["dream", "think"],
    description="Read a thinking loop's rolling thought stream — the journal of "
                "thoughts/findings plus the linked dream-layer memories it has "
                "accrued. Inputs: name (str!), limit (int, default 50). "
                "Output: {thoughts:[...], memories:[...]}.",
)
async def dream_think_stream(name: str, limit: int = 50, trace_id=None):
    journal_id = f"trigger:{name}"
    thoughts = await _journal_read(journal_id, limit=int(limit or 50),
                                   kinds=["thought", "finding"])
    memories: List[Dict[str, Any]] = []
    mem_search = CAPABILITY_REGISTRY.get("memory.search")
    if mem_search:
        try:
            sr = await mem_search["func"](query="", limit=int(limit or 50) * 2,
                                          tags=f"dream,{name}")
            for item in (sr or {}).get("results", []):
                rec = item.get("record", item) if isinstance(item, dict) else {}
                # memory.search with an empty query can fall back to relevance/
                # recency and leak non-dream chatter into this view. Keep only
                # genuine dream-layer records (source_type/tag) and drop noise so
                # "Dream-layer memories" shows real thoughts, not chat "hello"s.
                tags = rec.get("tags") or []
                is_dream = (str(rec.get("source_type", "")).lower() == "dream"
                            or "dream" in tags
                            or str(rec.get("category", "")).startswith("dream"))
                if not is_dream or _is_low_value_memory(rec):
                    continue
                memories.append({
                    "id":       rec.get("id"),
                    "category": rec.get("category"),
                    "text":     (rec.get("text") or rec.get("summary") or "")[:300],
                    "ts":       rec.get("created_at", ""),
                })
                if len(memories) >= int(limit or 50):
                    break
        except Exception:
            pass
    return {"ok": True, "name": name, "journal_id": journal_id,
            "thoughts": thoughts, "memories": memories,
            "count": len(thoughts)}


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
                    "no_hitl_caps", "mode", "depth", "persist_to_memory",
                    "collect")


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
        # Source review is deterministic + one-shot LLM (review_codebase runs a
        # single streaming ollama review per file; deep_review streams per chunk).
        # It never hands over to the agentic tool loop — analysis/documentation
        # generation doesn't need tools. Editing lives in source_review_fix.
        "kind": "source_review", "mode": "one_shot", "journal": True,
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
        {
         "name": "think",
         "kind": "think",
         "label": "Thinking loop",
         "description": "Read new items from a source each idle slot, reflect on "
                        "them against a subject/goal and the broader system context, "
                        "and keep a rolling thought stream persisted to the dream "
                        "memory layer. Created/configured via dream.think.create.",
         "mode": "synthesize_only", "depth": "standard", "max_steps": 0,
         "journal": True, "persist_to_memory": True,
         "stages": ["dream.stage.gather", "dream.stage.think_reflect"],
         "pivot": {"enabled": False}},
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
    if force:
        pre = await _preempt_for_manual(f"manual pipeline: {name}")
        if pre.get("blocked"):
            return {"ok": False, "error": f"a manual cycle is already running "
                    f"({pre.get('running')}); try again shortly"}
    elif _CYCLE_TASK and not _CYCLE_TASK.done():
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
REVIEW_STREAM_CID  = "dream-review"  # cycle_id used to stream the in-progress
                                      # review chunk's tokens for the panel's
                                      # "Current" view (see _llm_generate_streaming)

# REVIEW_STYLES (docs / critique / improvement / integration / architecture) is
# now imported at the top of this module from Vera.vera.output_formats — the
# shared registry so chat and any other caller can reuse the same palette. The
# dict shape ({label, system, instruction}) is unchanged.

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


async def _diff_snapshots(a_id: str, b_id: str,
                          size_only: bool = False) -> Dict[str, List[str]]:
    """Diff two snapshots (not snapshot-vs-live). Returns {modified, added,
    removed} of rel paths. Uses size as a prefilter, then content compare.
    size_only=True skips the per-file content read for same-size files — much
    faster (no I/O), at the cost of missing same-size edits; used for the resume
    staleness check so a review starts producing output promptly."""
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
        if size_only:
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
                           chunk_chars: int = 16000, stream_cid: str = "") -> Dict[str, Any]:
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
        if stream_cid:
            stage = f"{fname} · {sdef['label']}"
            if len(chunks) > 1:
                stage += f" (part {ci}/{len(chunks)})"
            md = await _llm_generate_streaming(prompt, system=system,
                                                cycle_id=stream_cid, stage=stage)
        else:
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

    # Build the set of already-reviewed (file, style) pairs.
    #
    # CONTENT-AWARE resume: a report counts as "done" only if it's still current
    # for the file's CURRENT content. A report made under a prior snapshot is
    # honoured only when the file is UNCHANGED between that snapshot and the
    # current one. So when the source has changed a lot since the last review,
    # the changed files are re-reviewed automatically instead of the run
    # declaring "nothing to review". (resume_scope='snapshot' restricts to the
    # current snapshot only; 'any' = the cross-snapshot, content-aware default.)
    existing: set = set()
    if resume:
        rr = _redis()
        if rr:
            try:
                h = await rr.hgetall(KEY_REVIEW_REPORTS)
                ok_reports: List[tuple] = []         # (file, style, report_snapshot_id)
                prior_snaps: set = set()             # distinct snapshots != current
                for k, v in (h or {}).items():
                    try:
                        rep = json.loads(v.decode() if isinstance(v, bytes) else v)
                        if not rep.get("ok"):
                            continue
                        rsid = rep.get("snapshot_id") or ""
                        if resume_scope == "snapshot" and rsid != snapshot_id:
                            continue
                        ok_reports.append((rep.get("file"), rep.get("style"), rsid))
                        if rsid and rsid != snapshot_id:
                            prior_snaps.add(rsid)
                    except Exception:
                        continue
                # Which files changed between each prior report-snapshot and now.
                changed_since: Dict[str, set] = {}
                if resume_scope != "snapshot" and snapshot_id:
                    for rsid in prior_snaps:
                        try:
                            # size_only → fast (no content reads) so the review
                            # starts producing output promptly rather than
                            # appearing to hang on a large resume diff.
                            d = await _diff_snapshots(rsid, snapshot_id, size_only=True)
                            changed_since[rsid] = set(d.get("modified", [])
                                                      + d.get("added", []))
                        except Exception as e:
                            # Can't diff → assume the file MIGHT have changed so we
                            # re-review (favour correctness over skipping); the
                            # whole-codebase cost is bounded by max_files.
                            log.debug("resume diff %s→%s: %s", rsid, snapshot_id, e)
                            changed_since[rsid] = set(names)
                for f, s, rsid in ok_reports:
                    if rsid and rsid != snapshot_id and f in changed_since.get(rsid, set()):
                        continue   # file changed since this report → re-review
                    existing.add((f, s))
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
            _fresh = " (fresh snapshot taken)" if snap.get("created") else \
                     " (reused existing snapshot — source may not have been re-snapshotted)"
            reason = (f"all {len(existing)} file/style report(s) are already current "
                      f"for snapshot {snapshot_id}{_fresh}. No files changed since "
                      f"their last review. Turn OFF 'Resume' to regenerate all reports.")
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
        # user is active or a time/file budget is reached. Always review at least
        # one file first (done > 0) so a run never yields with zero progress —
        # mirrors review_codebase's idx>0 guard and prevents the "yields instantly"
        # failure even if pause_on_activity is somehow left on.
        if pause_on_activity and activity_idle_min and done > 0:
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
            rep = await _deep_review_one(roots, snapshot_id, fname, style, chunk_chars,
                                          stream_cid=REVIEW_STREAM_CID)
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
        # User-initiated resume → run it through, don't yield to activity
        # (the click itself would otherwise trip the activity gate).
        asyncio.create_task(_run_deep_review(
            list(REVIEW_STYLES.keys()), "", [], 0, 16000,
            resume=True, pause_on_activity=False, activity_idle_min=5,
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
    "dream.review.search", memory="off", silent=True,
    http_method="GET", http_path="/dream/review/search", http_tags=["dream", "review"],
    description="Full-text search across stored review reports (filename, label and "
                "markdown body). Input: q (str!), limit (int, default 100). Output: "
                "{ok, q, results:[{file, style, area, label, ts, snippet}], count}.",
)
async def dream_review_search(q: str = "", limit: int = 100, trace_id=None):
    q = (q or "").strip()
    out: List[Dict[str, Any]] = []
    r = _redis()
    if r and q:
        ql = q.lower()
        try:
            h = await r.hgetall(KEY_REVIEW_REPORTS)
            for _, v in (h or {}).items():
                try:
                    rep = json.loads(v.decode() if isinstance(v, bytes) else v)
                except Exception:
                    continue
                md = rep.get("markdown", "") or ""
                hay = f"{rep.get('file','')} {rep.get('label','')} {md}".lower()
                if ql not in hay:
                    continue
                idx = md.lower().find(ql)
                if idx >= 0:
                    s = max(0, idx - 60)
                    snip = ("…" if s else "") + md[s:idx + len(q) + 80].replace("\n", " ")
                else:
                    snip = md[:140].replace("\n", " ")
                out.append({"file": rep.get("file"), "style": rep.get("style"),
                            "area": rep.get("area"), "label": rep.get("label"),
                            "ts": rep.get("ts"), "snippet": snip})
        except Exception as e:
            return {"ok": False, "error": str(e)}
    out.sort(key=lambda x: (x.get("file", ""), x.get("style", "")))
    return {"ok": True, "q": q, "results": out[:max(1, int(limit or 100))], "count": len(out)}


@capability(
    "dream.review.grep", memory="off", silent=True,
    http_method="GET", http_path="/dream/review/grep", http_tags=["dream", "review"],
    description="Search the SNAPSHOT SOURCE TREE for a literal string (independent "
                "of reports). Uses ripgrep/grep when available, else a bounded "
                "Python scan. Input: q (str!), snapshot_id (str, optional), limit "
                "(int, default 200). Output: {ok, q, matches:[{file, line, text}], count}.",
)
async def dream_review_grep(q: str = "", snapshot_id: str = "", limit: int = 200,
                            trace_id=None):
    q = (q or "").strip()
    if not q:
        return {"ok": True, "q": "", "matches": [], "count": 0}
    lim = max(1, int(limit or 200))
    roots = await _source_root_info()
    if not snapshot_id:
        snap = await _resolve_review_snapshot(label="grep")
        snapshot_id = snap.get("snapshot_id") or ""
    listing_root = ""
    if roots.get("snapshot_root") and snapshot_id:
        listing_root = f"{roots['snapshot_root']}/{snapshot_id}"
    elif roots.get("source_root"):
        listing_root = roots["source_root"]

    matches: List[Dict[str, Any]] = []

    def _rel(path: str) -> str:
        if listing_root and path.startswith(listing_root):
            return path[len(listing_root):].lstrip("/\\")
        return path

    # 1) Fast path: ripgrep (then grep) via bash, if the host provides them.
    bash = CAPABILITY_REGISTRY.get("exec.bash.run")
    if bash and listing_root:
        ql = q.replace("'", "'\\''")
        cmds = [
            f"rg -n --no-heading --color never -F -i '{ql}' '{listing_root}' "
            "-g '!*/.git/*' -g '!*/node_modules/*' -g '!*/__pycache__/*'",
            f"grep -rniF '{ql}' '{listing_root}' "
            "--exclude-dir=.git --exclude-dir=node_modules --exclude-dir=__pycache__",
        ]
        for cmd in cmds:
            try:
                res = await bash["func"](command=f"{cmd} 2>/dev/null | head -n {lim}",
                                         timeout=30) or {}
                out = res.get("stdout") or res.get("output") or ""
                if not out.strip():
                    continue
                for ln in out.splitlines()[:lim]:
                    m = re.match(r"^(.*?):(\d+):(.*)$", ln)
                    if not m:
                        continue
                    matches.append({"file": _rel(m.group(1)), "line": int(m.group(2)),
                                    "text": m.group(3)[:300]})
                if matches:
                    break
            except Exception:
                continue

    # 2) Portable fallback: bounded Python scan over enumerated files.
    if not matches:
        ql = q.lower()
        enum = await _enumerate_source_files(snapshot_id, roots)
        for f in enum[:1200]:
            rel = f.get("rel")
            if not rel:
                continue
            content = await _read_source_file(roots, snapshot_id, rel, 0)
            if not content:
                continue
            for i, line in enumerate(content.split("\n"), 1):
                if ql in line.lower():
                    matches.append({"file": rel, "line": i, "text": line.strip()[:300]})
                    if len(matches) >= lim:
                        break
            if len(matches) >= lim:
                break

    return {"ok": True, "q": q, "snapshot_id": snapshot_id,
            "matches": matches[:lim], "count": len(matches)}


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
    # Interruptibility / budget — yields to the user and other dreams. A
    # forced/manual run ("run now") must NOT yield to activity, otherwise the
    # user's own click (or the dream's own activity) makes it stop before
    # reviewing anything — the source of the "user active — yielding" failure.
    max_runtime_s = float(seed.get("review_max_runtime_s", cfg.get("max_runtime_s", 0)) or 0)
    pause_on_activity = bool(cfg.get("pause_on_activity",
                                     seed.get("review_pause_on_activity", True))) \
                        and not state.get("force")
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
    cfg = dict(await _get_config())
    # Expose the live, resolved idle-reset list (derived from cap-tracking in
    # follow mode) alongside the raw saved config.
    cfg["idle_reset_effective"] = _effective_idle_reset_prefixes(cfg)
    return {"config": cfg}


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
    idle_reset_follow_tracking: Optional[bool] = None,
    trace_id=None,
):
    cfg = await _get_config()
    if enabled is not None:                cfg["enabled"] = bool(enabled)
    if min_idle_minutes is not None:       cfg["min_idle_minutes"] = int(min_idle_minutes)
    if tick_interval_seconds is not None:  cfg["tick_interval_seconds"] = max(10, int(tick_interval_seconds))
    if telegram_bridge is not None:        cfg["telegram_bridge"] = bool(telegram_bridge)
    if default_hitl_timeout_s is not None: cfg["default_hitl_timeout_s"] = max(30, int(default_hitl_timeout_s))
    if llm_prefer_gpu is not None:         cfg["llm_prefer_gpu"] = bool(llm_prefer_gpu)
    if idle_reset_prefixes is not None:
        cfg["idle_reset_prefixes"] = [str(p).strip() for p in idle_reset_prefixes if str(p).strip()]
        # Editing the list is an explicit override — stop following cap-tracking
        # unless the caller is re-enabling follow mode in the same request.
        if idle_reset_follow_tracking is None:
            cfg["idle_reset_follow_tracking"] = False
    if idle_reset_follow_tracking is not None:
        cfg["idle_reset_follow_tracking"] = bool(idle_reset_follow_tracking)
    await _save_config(cfg)
    # Surface what's actually in effect so the UI can show derived prefixes.
    cfg = dict(cfg)
    cfg["idle_reset_effective"] = _effective_idle_reset_prefixes(cfg)
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
    "dream.cycle.progress", memory="off", silent=True,
    http_method="GET", http_path="/dream/cycle/progress", http_tags=["dream"],
    description="Live, poll-able progress snapshot of a dream cycle: current "
                "stage + per-stage status/elapsed/summary, LLM token heartbeat, "
                "agent-loop session (for re-attach), iteration index and output "
                "files. Blank cycle_id = the currently running cycle. This is "
                "the authoritative activity view — it keeps moving even when "
                "the event stream drops. Inputs: cycle_id (str).",
)
async def dream_cycle_progress(cycle_id: str = "", trace_id=None):
    running = await _get_running()
    if not cycle_id:
        cycle_id = (running or {}).get("cycle_id", "")
    if not cycle_id:
        return {"running": False, "progress": {}}
    prog = await _progress_get(cycle_id)
    is_current = bool(running and running.get("cycle_id") == cycle_id)
    return {"running": is_current, "cycle_id": cycle_id,
            "progress": prog, "current": running or {}}


@capability(
    "dream.cycle.files", memory="off", silent=True,
    http_method="GET", http_path="/dream/cycle/files", http_tags=["dream"],
    description="List the output-workspace files a dream cycle collated "
                "(gather data, themes, plan, findings, report, journal, meta). "
                "Inputs: cycle_id (str!). Output: {files:[{name,bytes,mtime}]}.",
)
async def dream_cycle_files(cycle_id: str = "", trace_id=None):
    if not cycle_id:
        return {"error": "cycle_id required"}
    return {"cycle_id": cycle_id, "files": _cycle_files_list(cycle_id)}


@capability(
    "dream.cycle.file", memory="off", silent=True,
    http_method="GET", http_path="/dream/cycle/file", http_tags=["dream"],
    description="Read one output-workspace file from a dream cycle. "
                "Inputs: cycle_id (str!), name (str!), max_chars (int default "
                "60000). Output: {name, content, truncated}.",
)
async def dream_cycle_file(cycle_id: str = "", name: str = "",
                           max_chars: int = 60000, trace_id=None):
    if not cycle_id or not name:
        return {"error": "cycle_id and name required"}
    d = _cycle_dir(cycle_id, create=False)
    fname = _SAFE_FILE_RE.sub("_", str(name))[:120]
    if d is None or not (d / fname).is_file():
        return {"error": f"file not found: {fname}"}
    try:
        text = (d / fname).read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"error": str(e)}
    lim = max(1000, int(max_chars))
    return {"cycle_id": cycle_id, "name": fname,
            "content": text[:lim], "truncated": len(text) > lim}


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
    description="Dream pipeline stage — the designated AGENTIC stage: stepwise "
                "plan+execute with tools. Defaults to the agent loop (unlike "
                "agent_loop/investigate, which default to one_shot); still "
                "per-stage overridable via stage_config.stepwise_execute."
                "prompt_style='one_shot'. Delegates to the configured agent-loop "
                "variant (default v5; falls back to v2/v1). Step results are "
                "surfaced as state['stepwise']['steps'] for the synthesize stage.",
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
    # stepwise_execute is the designated agentic stage: it defaults to the tool
    # loop (unlike agent_loop/investigate). Still per-stage overridable to one_shot.
    style = _stage_prompt_style(trig, "stepwise_execute")

    whitelist = trig.get("whitelist") or await _get_whitelist()
    _STEPWISE_EXCLUDE = {"dream.", "obs.", "health.", "ui.", "caps.", "mcp.", "echo"}
    whitelist = [
        c for c in whitelist
        if c in CAPABILITY_REGISTRY and not any(c.startswith(p) for p in _STEPWISE_EXCLUDE)
    ]
    if not whitelist and style != "one_shot":
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

    # One-shot analysis (no tools) short-circuits the entire agent-loop path.
    if style == "one_shot":
        await emit_event({
            "type":      "dream.stepwise.start",
            "cycle_id":  cycle_id,
            "max_steps": 1,
            "whitelist_count": len(whitelist),
            "engine":    "one_shot",
        })
        norm = await _run_oneshot_analysis(goal=goal, state=state,
                                           stage="stepwise_execute")
        _steps: List[Dict[str, Any]] = []
        _s = (norm.get("summary") or "").strip()
        if _s:
            _steps.append({"step": 0, "cap": "__summary__", "ok": True,
                           "reason": "one_shot analysis", "preview": _s[:600]})
        await emit_event({"type": "dream.stepwise.complete", "cycle_id": cycle_id,
                          "steps": len(_steps), "engine": "one_shot", "cycles": 0})
        state["stepwise"] = {"steps": _steps, "count": len(_steps),
                             "engine": "one_shot"}
        if "plan" not in state:
            state["plan"] = {"skipped": True, "reason": "stepwise mode (one_shot)"}
        return state

    # Resolve the configured agent-loop variant (default v5, graceful fallback).
    cfg = await _get_config()
    settings = await _resolve_loop_settings(trig, state)
    settings.setdefault("prefer_gpu", bool(cfg.get("llm_prefer_gpu", True)))
    agent_loop_cap, engine_name = _resolve_agent_loop_cap(settings)

    await emit_event({
        "type":      "dream.stepwise.start",
        "cycle_id":  cycle_id,
        "max_steps": max_steps,
        "whitelist_count": len(whitelist),
        "engine": engine_name or "fallback",
    })

    if not agent_loop_cap:
        # Lightweight fallback when no agent_loop variant is loaded yet
        log.warning("dream.stage.stepwise_execute: no agent_loop variant registered; "
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
        loop_session_id = f"dream:{cycle_id}:stepwise"
        norm = await _run_agent_loop(
            goal=goal, allowed_caps=",".join(whitelist), settings=settings,
            session_id=loop_session_id, max_steps=max_steps)

        # Normalized steps → emit per-step + keep for synthesize
        steps = []
        for step_rec in (norm.get("steps") or []):
            steps.append(step_rec)
            await emit_event({
                "type":    "dream.stepwise.result",
                "cycle_id": cycle_id,
                "step":    step_rec.get("step"),
                "cap":     step_rec.get("cap"),
                "ok":      step_rec.get("ok"),
                "preview": str(step_rec.get("preview", ""))[:200],
            })

        # Add loop summary as a final synthetic step so synthesize can see it
        summary_text = (norm.get("summary") or "").strip()
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
            "cycles":   norm.get("cycles", len(steps)),
            "error":    norm.get("error"),
        })

    state["stepwise"] = {
        "steps": steps,
        "count": len(steps),
        "engine": engine_name or "fallback",
    }
    # stepwise replaces plan+execute — mark plan as handled
    if "plan" not in state:
        state["plan"] = {"skipped": True,
                         "reason": f"stepwise mode ({engine_name or 'fallback'})"}
    return state


# ─────────────────────────────────────────────────────────────────────────────
# AGENT LOOP STAGE — named entry point for the DAG Workshop engine
# ─────────────────────────────────────────────────────────────────────────────
# dream.stage.agent_loop is a cleaner pipeline stage name that runs the
# configured agent-loop variant (default v5) via the shared _run_agent_loop
# helper, without the stepwise_execute wrapper's fallback shim. Use it in new
# pipelines; dream.stage.stepwise_execute and dream.stage.investigate are aliases.

@capability(
    "dream.stage.agent_loop", memory="off", silent=True,
    description="Analysis/action stage. Prompting style is per-stage selectable "
                "via stage_config.agent_loop.prompt_style = 'one_shot' (default) "
                "| 'agent_loop'. one_shot runs a single grounded LLM prompt with "
                "no tools (right for analysis/docs); agent_loop runs the "
                "configured agent-loop variant (default v5, orchestrator + scoped "
                "specialist engine; falls back to v2/v1) as a tool-using ReAct "
                "loop. Builds a goal from sensor context and stores results in "
                "state['agent_loop']. Engine is set globally via dream.loop.settings.",
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
    # Per-stage prompting style. Defaults to one_shot (a single grounded LLM
    # prompt, no tools) — right for analysis/documentation. Opt into the tool
    # loop via stage_config.agent_loop.prompt_style=agent_loop.
    style = _stage_prompt_style(trig, "agent_loop")

    # Build whitelist
    whitelist = trig.get("whitelist") or await _get_whitelist()
    _EXCL = {"dream.", "obs.", "health.", "ui.", "caps.", "mcp.", "echo"}
    whitelist = [c for c in whitelist
                 if c in CAPABILITY_REGISTRY and not any(c.startswith(p) for p in _EXCL)]
    if not whitelist and style != "one_shot":
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
        _default_task = ("Analyse the available context and write findings."
                         if style == "one_shot"
                         else "Use the available tools to investigate and act.")
        goal_parts = [trig.get("prompt") or _default_task]
        if focus:           goal_parts.append(f"FOCUS: {focus}")
        if project_ctx:     goal_parts.append(f"Project context:\n{project_ctx[:2000]}")
        if themes:          goal_parts.append(f"Themes: {', '.join(themes)}")
        if gather_lines:    goal_parts.append("Sensor data:\n" + "\n".join(gather_lines))
        goal = "\n\n".join(goal_parts)

    if style == "one_shot":
        # Single grounded LLM prompt, no tool loop.
        engine = "one_shot"
        await emit_event({"type": "dream.agent_loop.start", "cycle_id": cycle_id,
                          "engine": "one_shot", "max_steps": 1})
        norm = await _run_oneshot_analysis(goal=goal, state=state,
                                           stage="agent_loop")
    else:
        cfg = await _get_config()
        settings = await _resolve_loop_settings(trig, state)
        settings.setdefault("prefer_gpu", bool(cfg.get("llm_prefer_gpu", True)))
        cap, engine = _resolve_agent_loop_cap(settings)
        if not cap:
            state["agent_loop"] = {"error": "no agent_loop variant registered", "steps": []}
            return state

        await emit_event({"type": "dream.agent_loop.start", "cycle_id": cycle_id,
                          "engine": engine, "max_steps": max_steps})

        norm = await _run_agent_loop(
            goal=goal, allowed_caps=",".join(whitelist), settings=settings,
            session_id=f"dream:{cycle_id}:agent_loop", max_steps=max_steps)

    steps = list(norm.get("steps") or [])
    summary = (norm.get("summary") or "").strip()
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
        "cycles": norm.get("cycles", len(steps)),
        "error": norm.get("error"),
    }
    # Also populate stepwise so synthesize works regardless of which stage name was used
    state["stepwise"] = state["agent_loop"]
    if "plan" not in state:
        state["plan"] = {"skipped": True, "reason": "agent_loop mode"}

    await emit_event({"type": "dream.agent_loop.complete", "cycle_id": cycle_id,
                      "engine": engine, "steps": len(steps),
                      "cycles": norm.get("cycles", 0), "error": norm.get("error")})
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


# Distinct colour for dream events on the calendar overlay (matches the panel).
DREAM_EVENT_COLOR = "#a78bfa"


@capability(
    "dream.schedule.events", memory="off", silent=True,
    http_method="GET", http_path="/dream/schedule/events", http_tags=["dream"],
    description="Project scheduled dream fires as discrete calendar/timeline EVENTS "
                "(vs dream.timeline's hourly bars). Each event: "
                "{id,title,trigger,label,project,start,end,all_day,mode,recurrence,"
                "source:'dream',color,read_only}. Honours hours window, cooldown and "
                "idle. Inputs: days_ahead (int 1-30, default 7), "
                "max_per_trigger (int, default 20).",
)
async def dream_schedule_events(days_ahead: int = 7, max_per_trigger: int = 20,
                                trace_id=None):
    try:
        triggers = await _list_triggers()
    except Exception as e:
        return {"events": [], "count": 0, "error": str(e)}

    days_ahead = max(1, min(30, int(days_ahead or 7)))
    max_per_trigger = max(1, min(200, int(max_per_trigger or 20)))
    now = datetime.now()
    horizon = now + timedelta(days=days_ahead)
    events: List[Dict[str, Any]] = []

    for trig in triggers:
        try:
            if not trig.get("enabled"):
                continue
            name = trig.get("name", "?")
            label = trig.get("label", name)
            h_start = int(trig.get("hours_start") or 0)
            h_end = int(trig.get("hours_end") or 24)
            cooldown = max(15, int(trig.get("min_interval_minutes") or 60))

            # Start projecting from the end of the current cooldown (if any).
            cursor = now
            last_run = None
            try:
                last_run = await _last_run_ts(name)
            except Exception:
                pass
            if last_run and isinstance(last_run, str):
                try:
                    last_dt = datetime.fromisoformat(
                        last_run.replace("Z", "+00:00")).replace(tzinfo=None)
                    cd_until = last_dt + timedelta(minutes=cooldown)
                    if cd_until > cursor:
                        cursor = cd_until
                except Exception:
                    pass

            count = 0
            guard = 0
            while cursor < horizon and count < max_per_trigger and guard < 5000:
                guard += 1
                # Advance to the next moment inside the trigger's hours window.
                if not _within_hours(h_start, h_end, cursor):
                    cursor += timedelta(minutes=30)
                    continue
                start = cursor.replace(second=0, microsecond=0)
                end = start + timedelta(minutes=15)
                events.append({
                    "id":         f"dream:{name}:{int(start.timestamp())}",
                    "title":      f"💭 {label}",
                    "trigger":    name,
                    "label":      label,
                    "project":    trig.get("project") or "",
                    "start":      start.isoformat(),
                    "end":        end.isoformat(),
                    "all_day":    False,
                    "mode":       trig.get("mode") or "",
                    "hitl":       bool(trig.get("hitl")),
                    "recurrence": f"every ~{cooldown}m within {h_start}:00-{h_end}:00",
                    "source":     "dream",
                    "color":      DREAM_EVENT_COLOR,
                    "read_only":  True,
                })
                count += 1
                cursor = start + timedelta(minutes=cooldown)
        except Exception as e:
            log.debug("schedule.events trigger %s: %s", trig.get("name"), e)
            continue

    events.sort(key=lambda ev: ev["start"])
    return {"events": events, "count": len(events),
            "days_ahead": days_ahead, "generated_at": now_iso()}


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
    "Pulls recent records from the memory backends, filtering out chatter and the "
    "dream's own diagnostic notes so the signal reflects real activity.",
    "dream.sensor.memory_recent",
    params=[
        {"name": "limit", "type": "int", "default": 30, "help": "max records to fetch"},
        {"name": "min_chars", "type": "int", "default": 12,
         "help": "drop records shorter than this (chatter)"},
        {"name": "exclude_dream_diagnostics", "type": "bool", "default": True,
         "help": "drop the dream's own 'no signal' notes"},
        {"name": "exclude_source_types", "type": "str", "default": "",
         "help": "csv of source_types to drop (e.g. chat)"},
    ],
)
_register_sensor(
    "topic_research",
    "Topic — subject-scoped research",
    "Blends memory + fabric + web search on a subject and surfaces only NEW items "
    "per feed. The grounded fuel for a thinking loop pointed at a topic.",
    "dream.sensor.topic_research",
    params=[
        {"name": "subject", "type": "str", "default": "", "help": "subject to research"},
        {"name": "use_fabric", "type": "bool", "default": True, "help": "include fabric.query"},
        {"name": "use_web", "type": "bool", "default": True, "help": "include web.search"},
        {"name": "limit", "type": "int", "default": 20, "help": "max items"},
        {"name": "feed_id", "type": "str", "default": "", "help": "novelty cursor key"},
    ],
)
_register_sensor(
    "topics",
    "Composite — interesting topics + entities",
    "Harvests candidate topics from projects, source changes, research, schedule, "
    "recurring errors and memory clusters; ranks them; and bundles each top topic "
    "with its related memory/fabric/file entities. The richest single input for a "
    "dream — lets it think about THINGS and wander between them.",
    "dream.sensor.topics",
    params=[
        {"name": "top_n", "type": "int", "default": 6, "help": "how many topics to bundle"},
        {"name": "types", "type": "str", "default": "",
         "help": "csv: project,source_change,research,schedule,error,memory (blank=all)"},
        {"name": "per_topic_entities", "type": "int", "default": 6, "help": "entities per topic"},
        {"name": "hours_back", "type": "int", "default": 72, "help": "recency window"},
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
    "web_feed",
    "Web / RSS feed",
    "Reads an RSS/Atom feed (or any URL) and returns only items not seen before. "
    "The fuel for a thinking loop pointed at a live source (e.g. a subreddit's "
    ".rss). Tracks seen items per feed so each run yields just what's new.",
    "dream.sensor.web_feed",
    params=[
        {"name": "url", "type": "str", "default": "", "help": "feed or page URL"},
        {"name": "feed_id", "type": "str", "default": "", "help": "dedupe namespace (defaults to url hash)"},
        {"name": "limit", "type": "int", "default": 25, "help": "max new items per run"},
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
    "dream.stage.compose_topics", "Compose topics (interesting things + entities)",
    "Assembles the composite topic list (dream.sensor.topics), picks a focus topic "
    "(seed.focus_topic / topic_type, else highest interest), and sets a focused, "
    "entity-grounded refined_goal so the agent loop wanders that topic and pulls "
    "detail on demand. Place after gather, before the agent-loop stage. Config: "
    "stage_config.compose_topics = {top_n, types, per_topic_entities, topic_type}.",
    "dream.stage.compose_topics", phase="analyze", optional=True,
    params=[{"name": "topic_type", "type": "str", "default": "",
             "help": "prefer this topic type: project|source_change|research|schedule|error|memory"}],
)
_register_stage(
    "dream.stage.think_reflect", "Think / reflect (thinking loop)",
    "Reads the newly-gathered items, extracts what's interesting w.r.t. the "
    "thought's subject + goal and the broader system context, and appends a "
    "linked entry to the rolling thought stream. Pairs with a source sensor "
    "(e.g. web_feed) and persist_to_memory. Configure via "
    "stage_config.think_reflect = {subject, goal, max_items}.",
    "dream.stage.think_reflect", phase="analyze", optional=True,
    params=[
        {"name": "subject", "type": "str", "default": "", "help": "what to think about"},
        {"name": "goal", "type": "str", "default": "", "help": "what you're looking for"},
        {"name": "max_items", "type": "int", "default": 12, "help": "max new items per run"},
    ],
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
    "Place before dream.stage.review_codebase in source_review pipelines so the "
    "deterministic one-shot review has concrete file paths to work from.",
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
    "dream.stage.stepwise_execute", "Stepwise — the agentic stage (tools)",
    "The designated AGENTIC stage: defaults to the tool-using agent loop (default "
    "v5, orchestrator + scoped specialist engine; falls back to v2/v1), unlike "
    "agent_loop/investigate which default to one_shot. Step results surface as "
    "state['stepwise']['steps']. Set prompt_style='one_shot' to run a single LLM "
    "prompt with no tools instead. Engine is set globally via dream.loop.settings.",
    "dream.stage.stepwise_execute", phase="act", optional=True,
    params=[
        {"name": "prompt_style", "type": "str", "default": "agent_loop",
         "help": "agent_loop (tool-using ReAct loop, default) | one_shot (single LLM prompt, no tools)"},
    ],
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
    "dream.stage.investigate", "Investigate — one-shot LLM or agent loop",
    "Investigation stage with a selectable prompting style. prompt_style="
    "'one_shot' (default) runs a single grounded LLM prompt with no tools — "
    "right for analysis/documentation; prompt_style='agent_loop' delegates to "
    "the configured agent-loop variant (default v5; falls back to v2/v1) to run "
    "an iterative tool-using loop. Findings are stored in state['findings'] for "
    "the synthesize stage.",
    "dream.stage.investigate", phase="act", optional=True,
    params=[
        {"name": "prompt_style", "type": "str", "default": "one_shot",
         "help": "one_shot (single LLM prompt, no tools, default) | agent_loop (tool-using ReAct loop)"},
    ],
)
_register_stage(
    "dream.stage.agent_loop", "Agent Loop — one-shot LLM or agent loop",
    "Analysis/action stage with a selectable prompting style. prompt_style="
    "'one_shot' (default) runs a single grounded LLM prompt with no tools — "
    "right for analysis/docs; prompt_style='agent_loop' runs the configured "
    "agent-loop variant (default v5; falls back to v2/v1) as a tool-using ReAct "
    "loop. Populates state['agent_loop'] and state['stepwise'] for synthesize.",
    "dream.stage.agent_loop", phase="act", optional=True,
    params=[
        {"name": "prompt_style", "type": "str", "default": "one_shot",
         "help": "one_shot (single LLM prompt, no tools, default) | agent_loop (tool-using ReAct loop)"},
    ],
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
        "dream.director.assess", "dream.timeline", "dream.schedule.events",
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

    # ── Clear stale "running" flags left by a previous process ─────────────
    # KEY_RUNNING (a live cycle) and KEY_REVIEW_STATUS (a deep source review) are
    # Redis-persisted live-progress markers, but the asyncio tasks that own them
    # cannot survive a process restart. If Vera was killed mid-cycle/mid-review
    # these flags keep asserting "running" forever, so a freshly-started server
    # shows "doing source review" with nothing actually running and no new output.
    # Reset them (and any stale manual-pause flag) on startup.
    try:
        await r.delete(KEY_RUNNING)
        raw = await r.get(KEY_REVIEW_STATUS)
        if raw:
            try:
                st = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            except Exception:
                st = {}
            if st.get("running"):
                await r.set(KEY_REVIEW_STATUS, json.dumps({
                    "running": False, "done": st.get("done", 0),
                    "total": st.get("total", 0), "generated": st.get("generated", 0),
                    "snapshot_id": st.get("snapshot_id", ""),
                    "reason": "interrupted by server restart", "ts": now_iso(),
                }, default=str))
        await r.delete(KEY_REVIEW_PAUSE)
    except Exception as e:
        log.debug("dream startup: clear stale running flags: %s", e)

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

        # ── Prompting-style migration (idempotent, runs once) ───────────────
        # The default prompting style for agent_loop/investigate is now one_shot
        # (only stepwise_execute stays agentic by default). Combined with
        # create-if-absent seeding, an existing install needs two repairs:
        #   A) Any stored source_review* pipeline that still contains agentic
        #      stages (pre-refactor snapshot→goal_refine→agent_loop) → rewrite to
        #      the current one-shot built-in, or flip a custom one in place. This
        #      is what stopped source review handing over to the v5 loop.
        #   B) PRESERVE genuinely-agentic pipelines/triggers against the flipped
        #      default: any whose agent_loop/investigate stage has a whitelist
        #      that can actually ACT (not purely read-only) is pinned to
        #      prompt_style='agent_loop', so its tool loop doesn't silently turn
        #      off. Read-only analysis pipelines are left to take the one_shot
        #      default (that was the whole point of the flip).
        try:
            if not await r.get("vera:dream:migrated:prompt_style_v2"):
                _agentic_all = set(_AGENTIC_ANALYSIS_STAGES) | {"goal_refine", "plan", "execute"}
                _builtin_by_name = {p["name"]: p for p in _builtin_pipelines()}
                # Stages whose default flipped to one_shot — pin these to preserve
                # existing agentic behavior. stepwise_execute still defaults agentic.
                _flipped_stages = ("agent_loop", "investigate")
                repaired = 0
                pinned = 0

                def _set_style(sc_in, stage_shorts, only, target):
                    """Set prompt_style=target on each `only` stage that has no
                    explicit style yet. Returns (stage_config, changed)."""
                    sc = dict(sc_in or {})
                    changed = False
                    for s in stage_shorts:
                        if s not in only:
                            continue
                        cur = dict(sc.get(s) or {})
                        if not str(cur.get("prompt_style") or "").strip():
                            cur["prompt_style"] = target
                            sc[s] = cur
                            changed = True
                    return sc, changed

                try:
                    all_pipes = await r.hgetall(KEY_PIPELINES)
                except Exception:
                    all_pipes = {}
                for _raw in (all_pipes or {}).values():
                    try:
                        p = json.loads(_raw.decode() if isinstance(_raw, bytes) else _raw)
                    except Exception:
                        continue
                    name = p.get("name", "")
                    kind = str(p.get("kind", ""))
                    stages = [s.replace("dream.stage.", "") for s in (p.get("stages") or [])]
                    # A) source_review repair
                    if kind.startswith("source_review") and any(s in _agentic_all for s in stages):
                        b = _builtin_by_name.get(name)
                        if b:
                            p["stages"] = list(b["stages"])
                            p["mode"] = b.get("mode", "one_shot")
                            p["whitelist"] = list(b.get("whitelist", p.get("whitelist", [])))
                            p["no_hitl_caps"] = list(b.get("no_hitl_caps", p.get("no_hitl_caps", [])))
                        else:
                            p["mode"] = "one_shot"
                            p["stage_config"], _ = _set_style(
                                p.get("stage_config"), stages, _AGENTIC_ANALYSIS_STAGES, "one_shot")
                        await _save_pipeline(p)
                        repaired += 1
                        log.info("dream: repaired stale source_review pipeline '%s' to one-shot", name)
                        continue
                    # B) preserve genuinely-agentic pipelines against the flipped default
                    if (any(s in _flipped_stages for s in stages)
                            and not _whitelist_is_readonly_analysis(p.get("whitelist"))):
                        sc, changed = _set_style(
                            p.get("stage_config"), stages, _flipped_stages, "agent_loop")
                        if changed:
                            p["stage_config"] = sc
                            await _save_pipeline(p)
                            pinned += 1
                            log.info("dream: pinned agentic pipeline '%s' to agent_loop", name)

                # B) triggers with inline pipelines
                try:
                    for t in await _list_triggers():
                        pl = [s.replace("dream.stage.", "") for s in (t.get("pipeline") or [])]
                        if (not str(t.get("kind", "")).startswith("source_review")
                                and any(s in _flipped_stages for s in pl)
                                and not _whitelist_is_readonly_analysis(t.get("whitelist"))):
                            sc, changed = _set_style(
                                t.get("stage_config"), pl, _flipped_stages, "agent_loop")
                            if changed:
                                t["stage_config"] = sc
                                await _save_trigger(t)
                                pinned += 1
                                log.info("dream: pinned agentic trigger '%s' to agent_loop",
                                         t.get("name"))
                except Exception as e:
                    log.debug("dream prompt_style trigger pin: %s", e)

                await r.set("vera:dream:migrated:prompt_style_v2", "1")
                log.info("dream: prompt_style migration — repaired %d source_review "
                         "pipeline(s), pinned %d agentic pipeline/trigger(s) to agent_loop",
                         repaired, pinned)
        except Exception as e:
            log.debug("dream prompt_style migration: %s", e)

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

        # ── v5 engine + grounded-think migration (idempotent, fill-only) ────
        # Make v5 the default engine for existing stored triggers (preserves any
        # explicit per-trigger loop_version) and upgrade old topic "think" loops
        # from the raw memory_recent dump to the grounded topic_research sensor.
        try:
            if not await r.get("vera:dream:migrated:v5"):
                migrated_v5 = 0
                for t in await _list_triggers():
                    changed = False
                    ls = dict(t.get("loop_settings") or {})
                    if "loop_version" not in ls:
                        ls["loop_version"] = "v5"
                        t["loop_settings"] = ls
                        changed = True
                    # Old topic think loop → grounded topic_research (fabric + web)
                    if _is_think_trigger(t):
                        sensors = t.get("sensors") or []
                        sc = (t.get("stage_config") or {}).get("think_reflect") or {}
                        subj = sc.get("subject") or ""
                        is_topic = sensors in (["dream.sensor.memory_recent"],
                                               ["memory_recent"])
                        if is_topic and subj:
                            t["sensors"] = ["dream.sensor.topic_research"]
                            sp = dict(t.get("sensor_params") or {})
                            sp.pop("memory_recent", None)
                            sp["topic_research"] = {
                                "subject": subj, "use_fabric": True, "use_web": True,
                                "limit": 20, "feed_id": t.get("name", "")}
                            t["sensor_params"] = sp
                            changed = True
                    if changed:
                        await _save_trigger(t)
                        migrated_v5 += 1
                await r.set("vera:dream:migrated:v5", now_iso())
                if migrated_v5:
                    log.info("dream: v5 migration updated %d trigger(s)", migrated_v5)
        except Exception as e:
            log.debug("dream v5 migration: %s", e)

        # ── Composite-topics migration: upgrade the stored `wander` trigger to
        #    the new topic-wander pipeline (startup only merges NEW triggers, so
        #    an existing wander keeps its old broken graph-walk pipeline). One-off.
        try:
            if not await r.get("vera:dream:migrated:topics"):
                cur = await _get_trigger("wander")
                if cur and "dream.stage.compose_topics" not in (cur.get("pipeline") or []):
                    new_def = next((t for t in _default_triggers()
                                    if t["name"] == "wander"), None)
                    if new_def:
                        upgraded = dict(new_def)
                        for k in ("enabled", "hours_start", "hours_end",
                                  "min_idle_minutes", "min_interval_minutes",
                                  "deliver_to", "depth", "loop_settings"):
                            if k in cur:
                                upgraded[k] = cur[k]
                        await _save_trigger(upgraded)
                        log.info("dream: upgraded 'wander' to the composite-topics pipeline")
                await r.set("vera:dream:migrated:topics", now_iso())
        except Exception as e:
            log.debug("dream topics migration: %s", e)

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

        # A Loop Lab dev sandbox is a full Vera process sharing prod's REAL
        # Ollama nodes (no isolation there) — it exists to run one loop/test,
        # not to dream. Without this gate, every sandbox silently auto-starts
        # its own scheduler + ambient director on boot regardless of whether
        # anyone's using it, competing with the actual test for the same GPU.
        # Confirmed live 2026-08-03 (see is_dev_sandbox()'s docstring).
        if is_dev_sandbox():
            log.info("dream: scheduler + director auto-start skipped (dev sandbox)")
        else:
            cfg = await _get_config()
            if cfg.get("enabled", True):
                global _SCHED_RUN, _SCHED_TASK
                if not _SCHED_RUN:
                    _SCHED_RUN = True
                    _SCHED_TASK = asyncio.create_task(_scheduler_loop())
                    log.info("dream scheduler auto-started")
            # The director (ambient CPU-side thought loop) runs INDEPENDENTLY of
            # the idle-gated scheduler — it thinks during activity too, backing
            # off only on CPU-pool pressure. Its own config (dream.director.config)
            # gates it.
            dcfg = await _director_cfg()
            if dcfg.get("enabled", True):
                global _DIRECTOR_RUN, _DIRECTOR_TASK
                if not _DIRECTOR_RUN:
                    _DIRECTOR_RUN = True
                    _DIRECTOR_TASK = asyncio.create_task(_director_loop())
                    log.info("dream director auto-started")
    except Exception as e:
        log.warning("dream startup: %s", e)


schedule(_startup, interval=999999, name="dream_startup")