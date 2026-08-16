"""
evolve_capabilities.py — Loop Lab: evaluate & evolve the agentic loops
======================================================================

A closed improvement loop for Vera's agent engines (and, via cap-type tasks,
any other subsystem):

    run → check → assess (critic LLM) → edit (editor LLM) → rerun

  • **Tasks** — a registry of benchmark tasks. Two types:
      - ``loop``: a goal run through a loop profile (``loops.run``) — exercises
        the full agentic engine (triage, planning, tool calls, synthesis).
      - ``cap``:  a single capability call — a smoke test for any subsystem.
    Every task carries programmatic **checks** (ground truth: regex/contains/
    cap_called/json_valid/…) and an LLM **rubric**.

  • **Critic** — pluggable assessor provider. ``ollama[:model]`` runs on the
    local cluster (llm.generate); ``anthropic[:model]`` / ``openai[:model]`` /
    any stored provider id routes through ``providers.chat`` (Claude/ChatGPT,
    sealed keys, usage+cost tracked). The critic returns JSON: score 0-10,
    critique, failures, and *edit suggestions*.

  • **Improver** — a background session that runs the task set, has the critic
    score each run, then asks the **editor** LLM (typically Claude while local
    models catch up) for a better tuning **variant**: engine-knob overrides
    (max_cycles, enabled_steps, triage_top_k, …) + a system-prompt preamble.
    The new variant is applied and the set is rerun — up to ``max_rounds`` or
    until ``target_score``. Rounds, scores and variant lineage are persisted.

  • **Variants & overlay** — the best variant can be **promoted**: it lands in
    ``vera:evolve:overlay:<profile>`` and ``loops.run`` merges it between the
    profile defaults and caller overrides, so every production run of that
    profile benefits. Clearable/rollbackable at any time.

  • **Code suggestions** — the critic/editor may also propose source edits.
    These are NEVER auto-applied: they accumulate on the session and can be
    dispatched (one click / one cap call) to the Claude Code work queue
    (``ide.remote.queue.add``) — "Vera runs, Claude edits" — until local
    assessment agreement (``evolve.assess.compare``) shows Vera can take over.

  • **Automation** — ``evolve.suite.run`` executes every enabled task and
    stores a scoreboard; the ``loop_eval_nightly`` dream trigger (see
    dream_capabilities._default_triggers) runs it during idle hours and
    delivers a QA report with trend + regressions.

Storage (Redis):
    vera:evolve:config              global config JSON
    vera:evolve:tasks               hash task_id -> task JSON
    vera:evolve:seeded              set of seeded task ids (merge-on-update)
    vera:evolve:runs                list of compact run records (newest first)
    vera:evolve:run:<id>            full run detail (TTL 14d)
    vera:evolve:suites              list of suite scoreboards (newest first)
    vera:evolve:sessions            list of improve-session summaries
    vera:evolve:session:<id>        full session detail (TTL 30d)
    vera:evolve:variants:<profile>  hash variant_id -> variant JSON
    vera:evolve:overlay:<profile>   the ACTIVE promoted variant
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP,
    BACKGROUND_LLM,
    CALLER_KIND,
    CAPABILITY_REGISTRY,
    capability,
    emit_event,
    enum_schema,
    now_iso,
    register_ui,
    schedule,
)

log = logging.getLogger("vera.evolve")


def _triggered_by() -> str:
    """Real, honest run-trigger bucket — never guessed beyond what's actually
    knowable: CALLER_KIND is set only by the MCP bridge (a real Claude Code
    session), BACKGROUND_LLM only by autonomous drivers (dream/V8/fabric
    ingest); anything else (the browser chat UI, or any other caller) is
    the residual default "user" — not invented, just what's left over."""
    if CALLER_KIND.get() == "mcp":
        return "claude_code"
    if BACKGROUND_LLM.get():
        return "autonomous"
    return "user"

_HERE = Path(__file__).parent
_PANEL_PATH = _HERE / "evolve_panel.html"
_REPO_ROOT = _HERE.resolve().parents[1]   # vera/evolve -> vera -> repo root

KEY_CONFIG   = "vera:evolve:config"
KEY_TASKS    = "vera:evolve:tasks"
KEY_SEEDED   = "vera:evolve:seeded"
KEY_RUNS     = "vera:evolve:runs"
KEY_RUN      = "vera:evolve:run:"        # + run_id
KEY_SUITES   = "vera:evolve:suites"
KEY_SESSIONS = "vera:evolve:sessions"
KEY_SESSION  = "vera:evolve:session:"    # + session_id
KEY_VARIANTS = "vera:evolve:variants:"   # + profile
KEY_OVERLAY  = "vera:evolve:overlay:"    # + profile
KEY_AUDIT    = "vera:evolve:audit"       # verbose activity/change log (newest first)
KEY_REPOS    = "vera:evolve:repos"       # hash: repo_id -> repo record JSON — see
                                          # the REPO REGISTRY section below for the
                                          # functions/capabilities; these two names
                                          # are hoisted up here because they're used
                                          # as default-parameter values (evaluated at
                                          # def-time, not call-time) by capabilities
                                          # defined earlier in the file than that
                                          # section — a plain forward reference there
                                          # would NameError at import.
DEFAULT_REPO_ID  = "vera"
# A bare `pytest` isn't guaranteed on PATH for the user/env Vera itself runs
# under (confirmed missing on the primary host — venv installs don't always
# symlink console-scripts onto PATH). `sys.executable` is the exact
# interpreter Vera is running under right now, so `-m pytest` always resolves
# to the same env pytest is actually installed in, if it's installed at all.
DEFAULT_TEST_CMD = f"{sys.executable} -m pytest -q --tb=no"

RUNS_CAP     = 400
SUITES_CAP   = 60
SESSIONS_CAP = 60
AUDIT_CAP    = 1000

# Cap families a TEST loop must never touch — external effects on the real world
# / real Vera state. Stripped from every test loop's toolkit by default so a
# benchmark can't send mail, deploy, exec on prod, push git, etc. Override via
# config.test_denylist.
DEFAULT_TEST_DENYLIST = [
    "mail.", "tg.", "exec.", "docker.", "git.", "provision.", "ssh.", "mesh.",
    "deploy.", "comms.", "netsec.", "ide.remote.", "sandbox.host.",
    "loops.run", "dream.cycle.run", "dream.scheduler.", "evolve.",
    "markets.evolve.", "business.", "commerce.", "accounts.",
]

DEFAULT_CONFIG: Dict[str, Any] = {
    # Critic = scores runs. Editor = proposes the next variant. Either accepts
    # "ollama[:model]" (local cluster) or "<provider_id>[:model]" from the
    # providers registry (anthropic / openai / any OpenAI-compatible record).
    "critic_provider": "ollama",
    "editor_provider": "anthropic",
    "target_score":    8.0,
    "max_rounds":      4,
    # Source-edit suggestions are queued to Claude Code ONLY when this is on
    # (and even then via the ide.remote work queue — never applied in-process).
    "allow_code_edits": False,
    "default_profile": "planning",
    # ── Sandbox-first execution ───────────────────────────────────────────
    # off     — run tests in-process (fast, but a loop CAN touch real Vera)
    # prefer  — run tests in the dev sandbox when one is up, else in-process
    # require — tests MUST run in the sandbox; refuse to run in-process
    # Default is 'require': Loop Lab must ONLY ever operate on a containerised
    # copy of the source, NEVER the real one — 'prefer' silently falls back to
    # in-process (touching real caps/files) the moment no sandbox happens to be
    # up, which is not an isolation guarantee. A stored config always overrides
    # this default (see _get_config), so this only sets the out-of-the-box
    # posture for a fresh install.
    "sandbox_mode":    "require",
    # Strip these cap prefixes from every TEST loop's toolkit (defence in depth,
    # applied even in-process) so a benchmark can't act on the real world.
    "test_denylist":   DEFAULT_TEST_DENYLIST,
    # ── Background edit queue ─────────────────────────────────────────────
    # The synthesis step (proposing the next variant) runs as a background
    # queue on a cheap LOCAL model on a CPU node, so it doesn't block, doesn't
    # burn GPU/API, and its actions are visible + editable. When off, the
    # editor_provider runs inline instead.
    "editq_enabled":   True,
    "editq_provider":  "ollama",       # provider family for the queue worker
    "editq_model":     "gpt-oss:20b",  # the model the edits run on
    "editq_instance":  "",             # pin a CPU node (e.g. 'cpu-246'); '' = auto CPU
    "editq_timeout_s": 600,            # per-action budget on the CPU node
    # ── Adversarial evaluation (Bun-article Loop 1) ───────────────────────
    # Implementer (the loop) → N adversarial reviewers (find why it's wrong,
    # given only the trace+output) → fixer (the edit queue). More reviewers =
    # harsher, more thorough evaluation.
    "adversarial":     True,
    "reviewers":       2,
    # ── Interactive-run timeouts (activity-aware) ─────────────────────────
    # Single tests launched from the composer are NOT killed on a fixed clock —
    # a loop under test may legitimately run for a very long time. Instead they
    # are watched for ACTIVITY (loop events, incl. a strategic engine's child
    # sessions): killed only after `run_idle_timeout_s` with no new events, or
    # at the `run_max_s` hard ceiling (0 = unlimited). Suite/benchmark runs
    # keep their fixed per-task timeout_s — benchmarks must be bounded.
    "run_idle_timeout_s":  300,
    "run_max_s":           7200,
    # ── Errors work-queue auto-sync ───────────────────────────────────────
    # When on, Loop Lab periodically pulls the observability signals (perf,
    # event-loop stalls, syslog/ollama errors) into its errors work-queue and
    # distils a suggested fix for each, so errors flow toward a commit without
    # anyone watching. Nothing is applied without human approve. Off by default
    # so it's opt-in; the panel toggles it.
    "errors_autosync":     False,
    "errors_autosync_s":   900,        # how often to pull (seconds)
    # ── Dev sandbox host port ─────────────────────────────────────────────
    # The isolated dev-sandbox Vera binds THIS host port (the container still
    # listens on 8999 internally). It MUST differ from the prod port (8999) or
    # `sandbox up` collides with the running Vera — the "port 8999 already in
    # use" failure. Default 8998; override here (the panel exposes it) or via
    # env VERA_DEV_PORT.
    "dev_port":            int(os.getenv("VERA_DEV_PORT", "8998")),
    # ── Tie the dev sandbox's lifecycle to THIS Vera ──────────────────────
    # The sandbox is a SECOND, complete Vera: its own scheduler, its own worker,
    # its own dream/evolve loops — sharing this host's Redis, Neo4j and Ollama
    # nodes. Nothing brings it down, so it outlives every restart of the host
    # Vera and keeps issuing work: found running 8 loops continuously since a
    # restart two days earlier, re-loading Ollama within seconds of the cluster
    # being cleared, while the operator believed "Vera is stopped".
    #
    # With this on, the sandbox comes UP when Vera starts and goes DOWN when Vera
    # stops, so there is exactly one lifecycle to reason about. Default OFF: a
    # sandbox deliberately left running to finish long work must not be killed
    # by an unrelated restart. Env VERA_DEV_FOLLOW_HOST=1 presets it.
    "sandbox_follow_host": os.getenv("VERA_DEV_FOLLOW_HOST", "").strip().lower()
                           in ("1", "true", "yes"),
}

# Engine knobs the editor LLM is allowed to tune, with clamps. Anything the
# editor proposes outside this table is dropped — it cannot invent kwargs.
TUNABLE_KNOBS: Dict[str, Dict[str, Any]] = {
    "max_cycles":           {"type": int,  "min": 2,  "max": 24},
    "max_steps":            {"type": int,  "min": 1,  "max": 16},
    "triage_top_k":         {"type": int,  "min": 4,  "max": 48},
    "catalog_size":         {"type": int,  "min": 10, "max": 120},
    "max_search_calls":     {"type": int,  "min": 0,  "max": 6},
    "max_expands":          {"type": int,  "min": 0,  "max": 4},
    "min_explore_cycles":   {"type": int,  "min": 0,  "max": 6},
    "recon_max_rounds":     {"type": int,  "min": 0,  "max": 6},
    "step_cycle_budget":    {"type": int,  "min": 1,  "max": 12},
    "enabled_steps":        {"type": str},
    "model":                {"type": str},
    "satisfaction_check":   {"type": bool},
    "require_verify":       {"type": bool},
    "strict_complete":      {"type": bool},
    "enable_replan":        {"type": bool},
    "enable_recon":         {"type": bool},
    "enable_subplans":      {"type": bool},
    "enable_phases":        {"type": bool},
    "enable_master_planner": {"type": bool},
    "phased":               {"type": bool},
    "prefer_terminal_tools": {"type": bool},
    "select_steps":         {"type": bool},
}

_KNOB_GUIDE = (
    "TUNABLE ENGINE KNOBS (only these may appear in edits.overrides):\n"
    "- max_cycles (int 2-24): total reasoning/tool cycles budget\n"
    "- max_steps (int 1-16): plan length budget\n"
    "- triage_top_k (int 4-48): how many candidate caps triage keeps\n"
    "- catalog_size (int 10-120): tool catalog size shown to the planner\n"
    "- max_search_calls / max_expands (int): research budgets\n"
    "- min_explore_cycles (int 0-6): forced exploration before acting\n"
    "- enabled_steps (csv of plan,explore,think,act,verify): step pipeline\n"
    "- satisfaction_check / require_verify / strict_complete (bool): rigor\n"
    "- enable_replan / enable_recon / recon_max_rounds: recovery behaviour\n"
    "- enable_subplans / enable_phases / enable_master_planner / phased (bool)\n"
    "- prefer_terminal_tools / select_steps (bool)\n"
    "- model (str): local model override (leave alone unless clearly needed)\n"
    "Additionally 'prompt_preamble' (str) is allowed: extra system-prompt "
    "guidance prepended for every run of this profile."
)


def _redis():
    return getattr(_orch, "REDIS", None)


async def _call(name: str, **kw) -> Any:
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap or not cap.get("func"):
        return {"error": f"capability not available: {name}"}
    try:
        return await cap["func"](**kw)
    except Exception as e:
        return {"error": f"{name}: {e}"}


# ─────────────────────────────────────────────────────────────────────────────
# AUDIT LOG — a verbose, durable record of every change & rollback
# ─────────────────────────────────────────────────────────────────────────────
# Every mutating action (promote, rollback, code queue, branch create/delete,
# pipeline decision, sandbox up/down, config change, merge/push to real source)
# is appended here so there is a full trail of what changed, when, and why —
# and every rollback is itself a logged event.

async def _audit(action: str, summary: str = "", **fields):
    r = _redis()
    entry = {"ts": now_iso(), "action": action, "summary": str(summary)[:400],
             **{k: (str(v)[:300] if isinstance(v, str) else v)
                for k, v in fields.items()}}
    if r:
        try:
            await r.lpush(KEY_AUDIT, json.dumps(entry, default=str))
            await r.ltrim(KEY_AUDIT, 0, AUDIT_CAP - 1)
        except Exception as e:
            log.debug("evolve audit: %s", e)
    await emit_event({"type": "evolve.audit", **entry})
    return entry


@capability("evolve.audit.list", memory="off", silent=True,
            http_method="GET", http_path="/evolve/audit", http_tags=["evolve"],
            description="Verbose activity/change log — every promote, rollback, "
                        "code-queue, branch op, pipeline decision, sandbox op and "
                        "config change, newest first. Query: limit (int=100), "
                        "action (str filter).")
async def evolve_audit_list(limit: int = 100, action: str = "", trace_id=None):
    r = _redis()
    out: List[Dict[str, Any]] = []
    if r:
        try:
            rows = await r.lrange(KEY_AUDIT, 0, AUDIT_CAP - 1)
            for row in rows or []:
                try:
                    e = json.loads(row.decode() if isinstance(row, bytes) else row)
                except Exception:
                    continue
                if action and action not in str(e.get("action", "")):
                    continue
                out.append(e)
                if len(out) >= int(limit):
                    break
        except Exception:
            pass
    return {"audit": out, "count": len(out)}


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

async def _get_config() -> Dict[str, Any]:
    r = _redis()
    cfg = dict(DEFAULT_CONFIG)
    if r:
        try:
            raw = await r.get(KEY_CONFIG)
            if raw:
                cfg.update(json.loads(raw.decode() if isinstance(raw, bytes) else raw))
        except Exception:
            pass
    return cfg


@capability("evolve.config.get", memory="off", silent=True,
            http_method="GET", http_path="/evolve/config", http_tags=["evolve"],
            description="Loop Lab global config: critic/editor providers, target "
                        "score, rounds, allow_code_edits, default profile.")
async def evolve_config_get(trace_id=None):
    return {"config": await _get_config()}


@capability("evolve.config.set", memory="off",
            http_method="POST", http_path="/evolve/config/set", http_tags=["evolve"],
            description="Update Loop Lab config. Pass only fields to change: "
                        "critic_provider, editor_provider, target_score, max_rounds, "
                        "allow_code_edits, default_profile, sandbox_mode "
                        "(off|prefer|require), test_denylist (list of cap prefixes), "
                        "dev_port (sandbox host port, must differ from prod's 8999), "
                        "sandbox_follow_host (bool — bring the dev sandbox UP when Vera "
                        "starts and DOWN when Vera stops. The sandbox is a second full "
                        "Vera sharing this host's Redis/Neo4j/Ollama, and nothing "
                        "otherwise stops it: left alone it outlives every restart and "
                        "keeps driving the cluster. Default off so a sandbox deliberately "
                        "left running is not killed by an unrelated restart).")
async def evolve_config_set(critic_provider: str = None, editor_provider: str = None,
                            target_score: float = None, max_rounds: int = None,
                            allow_code_edits: bool = None, default_profile: str = None,
                            sandbox_mode: str = None,
                            test_denylist: Optional[List[str]] = None,
                            editq_enabled: bool = None, editq_provider: str = None,
                            editq_model: str = None, editq_instance: str = None,
                            adversarial: bool = None, reviewers: int = None,
                            errors_autosync: bool = None, errors_autosync_s: int = None,
                            run_idle_timeout_s: int = None, run_max_s: int = None,
                            dev_port: int = None,
                            sandbox_follow_host: bool = None,
                            trace_id=None):
    cfg = await _get_config()
    changed = []
    if critic_provider is not None: cfg["critic_provider"] = critic_provider.strip(); changed.append("critic")
    if editor_provider is not None: cfg["editor_provider"] = editor_provider.strip(); changed.append("editor")
    if target_score is not None:    cfg["target_score"] = max(0.0, min(10.0, float(target_score)))
    if max_rounds is not None:      cfg["max_rounds"] = max(1, min(12, int(max_rounds)))
    if allow_code_edits is not None: cfg["allow_code_edits"] = bool(allow_code_edits); changed.append("allow_code_edits")
    if default_profile is not None: cfg["default_profile"] = default_profile.strip()
    if sandbox_mode is not None and sandbox_mode in ("off", "prefer", "require"):
        cfg["sandbox_mode"] = sandbox_mode; changed.append(f"sandbox_mode={sandbox_mode}")
    if sandbox_follow_host is not None:
        cfg["sandbox_follow_host"] = bool(sandbox_follow_host)
        changed.append(f"sandbox_follow_host={bool(sandbox_follow_host)}")
    if test_denylist is not None:
        cfg["test_denylist"] = [str(p) for p in test_denylist if str(p).strip()]
    if editq_enabled is not None:  cfg["editq_enabled"] = bool(editq_enabled); changed.append("editq_enabled")
    if editq_provider is not None: cfg["editq_provider"] = editq_provider.strip()
    if editq_model is not None:    cfg["editq_model"] = editq_model.strip(); changed.append(f"editq_model={editq_model}")
    if editq_instance is not None: cfg["editq_instance"] = editq_instance.strip()
    if adversarial is not None:    cfg["adversarial"] = bool(adversarial); changed.append("adversarial")
    if reviewers is not None:      cfg["reviewers"] = max(1, min(5, int(reviewers))); changed.append(f"reviewers={reviewers}")
    if errors_autosync is not None: cfg["errors_autosync"] = bool(errors_autosync); changed.append(f"errors_autosync={bool(errors_autosync)}")
    if errors_autosync_s is not None: cfg["errors_autosync_s"] = max(60, min(86400, int(errors_autosync_s)))
    if run_idle_timeout_s is not None: cfg["run_idle_timeout_s"] = max(60, min(7200, int(run_idle_timeout_s))); changed.append(f"run_idle_timeout_s={run_idle_timeout_s}")
    if run_max_s is not None: cfg["run_max_s"] = max(0, min(86400, int(run_max_s))); changed.append(f"run_max_s={run_max_s}")
    if dev_port is not None:
        p = int(dev_port)
        if not (1024 <= p <= 65535):
            return {"error": f"dev_port must be 1024-65535 (got {p})"}
        if p == PROD_PORT:
            return {"error": f"dev_port {p} is prod's own port — pick a different "
                             f"free port (e.g. 8998) so the sandbox doesn't collide"}
        cfg["dev_port"] = p; changed.append(f"dev_port={p}")
    r = _redis()
    if r:
        try:
            await r.set(KEY_CONFIG, json.dumps(cfg))
        except Exception:
            pass
    if changed:
        await _audit("config.set", ", ".join(changed))
    return {"ok": True, "config": cfg}


@capability("evolve.providers", memory="off", silent=True,
            http_method="GET", http_path="/evolve/providers", http_tags=["evolve"],
            description="Providers usable as critic/editor: 'ollama' (local "
                        "cluster) plus every enabled record from the providers "
                        "registry (anthropic/openai/custom).")
async def evolve_providers(trace_id=None):
    out = [{"id": "ollama", "label": "Ollama (local cluster)", "kind": "local"}]
    res = await _call("providers.list")
    for p in (res or {}).get("providers", []) if isinstance(res, dict) else []:
        if p.get("enabled", True):
            out.append({"id": p.get("id"), "label": p.get("label") or p.get("id"),
                        "kind": p.get("kind", "api"),
                        "default_model": p.get("default_model", "")})
    return {"providers": out}


# ═════════════════════════════════════════════════════════════════════════════
# IMPROVEMENT TARGETS — "what am I improving?" as a clear taxonomy
# ═════════════════════════════════════════════════════════════════════════════
# A target id is "<category>:<id>", e.g. "specialist:coding", "agent:coder",
# "engine:v6", "chat:main", "system:dream". Each resolves to the loop PROFILE
# used to exercise it, the source FILE for code edits, and whether it is
# variant-TUNABLE (specialist loops + engines) or code-edit-only.

_ENGINE_VERSIONS = ["v5", "v6", "v7", "v8"]


async def _resolve_target(target_id: str) -> Dict[str, Any]:
    """Resolve a target id to {category, id, label, profile, code_file, tunable}.
    Bare/blank or a legacy profile name maps to a specialist-loop target so old
    callers keep working."""
    tid = (target_id or "").strip()
    if ":" not in tid:
        # legacy: a bare profile name → specialist loop
        tid = f"specialist:{tid}" if tid else "specialist:planning"
    cat, _, sub = tid.partition(":")
    cat = cat.lower()
    if cat == "specialist":
        return {"category": "specialist", "id": tid, "label": sub or "planning",
                "profile": sub or "planning",
                "code_file": "vera/dag/loop_profiles.py", "tunable": True}
    if cat == "engine":
        return {"category": "engine", "id": tid, "label": f"Agent loop {sub}",
                "profile": "planning", "engine": sub,
                "code_file": "vera/dag/dag_workshop_capabilities.py", "tunable": True}
    if cat == "agent":
        # An agent: target should exercise the loop PROFILE actually bound to
        # that agent persona (loop_profiles.py's own agent field), not silently
        # fall back to the generic planning profile — a target=agent:coder run
        # used to always run "planning" regardless of which agent was named.
        # If no profile is bound to this agent, fall through to a plain
        # single-turn agent.consult-style run instead of mislabeling it.
        bound_profile = ""
        try:
            profs = (await _call("loops.profiles") or {}).get("profiles", [])
            match = next((p for p in profs if p.get("agent") == sub), None)
            if match:
                bound_profile = match.get("id", "")
        except Exception as e:
            log.debug("_resolve_target: agent->profile lookup failed for %s: %s", sub, e)
        return {"category": "agent", "id": tid, "label": sub, "agent": sub,
                "profile": bound_profile or "", "no_bound_profile": not bound_profile,
                "code_file": "vera/agents/agents.py", "tunable": True}
    if cat == "chat":
        return {"category": "chat", "id": tid, "label": "Chat", "profile": "planning",
                "code_file": "vera/chat/chat_panels_capabilities.py", "tunable": False}
    if cat == "system":
        return {"category": "system", "id": tid, "label": sub, "group": sub,
                "profile": "planning", "code_file": f"vera/{sub}", "tunable": False}
    if cat == "ide":
        # a registered remote IDE (code-server / tunnel / ssh) — Loop Lab tests &
        # loops over the code in THAT workspace via ide.remote.run.
        return {"category": "ide", "id": tid, "label": sub, "instance": sub,
                "profile": "coding", "code_file": "", "tunable": False,
                "remote_ide": True}
    return {"category": "specialist", "id": "specialist:planning",
            "label": "planning", "profile": "planning",
            "code_file": "vera/dag/loop_profiles.py", "tunable": True}


@capability("evolve.targets", memory="off", silent=True,
            http_method="GET", http_path="/evolve/targets", http_tags=["evolve"],
            description="The categorised tree of things Loop Lab can improve: "
                        "Agents · Agentic loops (engines) · Specialist loops "
                        "(profiles) · Chat · System components. Each target carries "
                        "the loop profile used to exercise it, its source file, and "
                        "whether it is variant-tunable. Output: {categories:[{key,"
                        "label,targets:[{id,label,tunable}]}]}.")
async def evolve_targets(trace_id=None):
    cats: List[Dict[str, Any]] = []

    # Specialist loops (the loop profiles)
    prof = await _call("loops.profiles")
    # `streams` = the loop runs inline under the run session and streams
    # agent_loop_v* events (v5/v6). Strategic engines (v7+) run piecewise in
    # SUB-sessions, so the inline implementer timeline stays empty — flag them.
    specialist = [{"id": f"specialist:{p.get('id')}", "label": p.get("label") or p.get("id"),
                   "tunable": True, "icon": p.get("icon", ""),
                   "engine": p.get("engine", "v6"),
                   "streams": str(p.get("engine", "v6")) in ("v5", "v6")}
                  for p in (prof or {}).get("profiles", []) if isinstance(prof, dict)]
    cats.append({"key": "specialist", "label": "Specialist loops", "icon": "⚡",
                 "targets": specialist})

    # Agents
    ags = await _call("agent.list")
    agents = [{"id": f"agent:{a.get('name')}", "label": a.get('name'),
               "tunable": True, "desc": (a.get("description") or a.get("role") or "")[:80]}
              for a in (ags or {}).get("agents", []) if isinstance(ags, dict) and a.get("name")]
    cats.append({"key": "agent", "label": "Agents", "icon": "🤖", "targets": agents})

    # Agentic loop engines
    cats.append({"key": "engine", "label": "Agentic loops (engine)", "icon": "🔁",
                 "targets": [{"id": f"engine:{v}", "label": f"Agent loop {v}",
                              "tunable": True} for v in _ENGINE_VERSIONS]})

    # Chat
    cats.append({"key": "chat", "label": "Chat", "icon": "💬",
                 "targets": [{"id": "chat:main", "label": "Chat system", "tunable": False}]})

    # System components (cap groups)
    grp = await _call("caps.list_categories")
    groups = []
    raw = (grp or {}).get("categories") if isinstance(grp, dict) else None
    names = list(raw.keys()) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    for g in sorted(str(n) for n in (names or []))[:40]:
        groups.append({"id": f"system:{g}", "label": g, "tunable": False})
    cats.append({"key": "system", "label": "System components", "icon": "⚙️",
                 "targets": groups})

    # IDE code — every registered remote IDE workspace is a target Loop Lab can
    # test & loop over (via ide.remote.run: Claude Code CLI or a Vera agent).
    ide = await _call("ide.remote.instances")
    ides = [{"id": f"ide:{i.get('id')}",
             "label": (i.get('label') or i.get('id')) + " · " + (i.get('kind') or ''),
             "tunable": False, "desc": (i.get('workdir') or i.get('url') or '')[:80]}
            for i in (ide or {}).get("instances", [])
            if isinstance(ide, dict) and i.get("id")]
    cats.append({"key": "ide", "label": "IDE code (remote)", "icon": "🖥️",
                 "targets": ides})

    return {"categories": cats}


@capability("evolve.target.info", memory="off", silent=True,
            http_method="GET", http_path="/evolve/target/info", http_tags=["evolve"],
            description="Full description + CONFIG of a test target — what exactly "
                        "is being tested. specialist/engine → loop profile (engine, "
                        "agent, caps, defaults, skills) + any promoted tuning "
                        "overlay; agent → model/domain_caps/system prompt; engine → "
                        "tunable knobs; ide → instance record. Query: target "
                        "(str — category:id). Output: {target, description, "
                        "profile?, overlay?, agent?, knobs?, code_file, tunable}.")
async def evolve_target_info(target: str = "", trace_id=None):
    tgt = await _resolve_target(target)
    out: Dict[str, Any] = {"target": tgt, "description": "",
                           "code_file": tgt.get("code_file", ""),
                           "tunable": tgt.get("tunable", False)}
    cat = tgt.get("category")
    if cat in ("specialist", "engine"):
        p = await _call("loops.profile", id=tgt.get("profile", ""))
        if isinstance(p, dict) and p.get("profile"):
            prof = p["profile"]
            out["description"] = prof.get("description", "")
            out["profile"] = {k: prof.get(k) for k in
                              ("id", "label", "icon", "engine", "agent", "caps",
                               "skills", "family", "defaults") if prof.get(k) is not None}
        r = _redis()
        if r:
            try:
                raw = await r.get(KEY_OVERLAY + tgt.get("profile", ""))
                if raw:
                    out["overlay"] = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            except Exception:
                pass
    if cat == "engine":
        out["description"] = (f"Agent-loop engine {tgt.get('engine')} — the shared "
                              f"runner with this version's feature set. Tunable "
                              f"knobs listed below; exercised via the "
                              f"'{tgt.get('profile')}' profile.")
        out["knobs"] = sorted(TUNABLE_KNOBS.keys())
    if cat == "agent":
        a = await _call("agent.get", name=tgt.get("agent", ""))
        if isinstance(a, dict) and not a.get("error"):
            out["description"] = a.get("description") or a.get("role") or ""
            out["agent"] = {"name": a.get("name"), "model": a.get("model", ""),
                            "domain_caps": a.get("domain_caps") or [],
                            "system_prompt": (a.get("system_prompt") or "")[:800]}
    if cat == "ide":
        insts = await _call("ide.remote.instances")
        for i in (insts or {}).get("instances", []) if isinstance(insts, dict) else []:
            if i.get("id") == tgt.get("instance"):
                out["description"] = (f"Remote IDE workspace ({i.get('kind')}) — "
                                      f"Loop Lab runs goals over its code via "
                                      f"ide.remote.run (diagnose-only by default).")
                out["ide"] = {k: i.get(k) for k in
                              ("id", "label", "kind", "host_id", "workdir", "url", "status")}
                break
    if cat == "chat":
        out["description"] = ("The chat system (prompt + behaviour). Code-edit "
                              "target — improved via the gated CI pipeline.")
    if cat == "system":
        out["description"] = (f"Capability group '{tgt.get('group')}' — a Vera "
                              f"system component. Code-edit target — tested via "
                              f"cap tests / unittest, improved via the CI pipeline.")
    return out


@capability("evolve.ide.improve", memory="on",
            http_method="POST", http_path="/evolve/ide/improve", http_tags=["evolve"],
            description="Test / loop over the code in a remote IDE workspace. Runs "
                        "the goal against that workspace via ide.remote.run (Claude "
                        "Code CLI or a Vera agent editing over SSH), streaming into "
                        "the Loop Lab workflow diagram + Recent runs. DIAGNOSE-ONLY "
                        "by default (apply=False → it reviews and proposes edits "
                        "without changing files); set apply=True to let it make the "
                        "changes. Inputs: instance_id (str! — from evolve.targets ide "
                        "category / ide.remote.instances), goal (str!), engine "
                        "(claude|vera-agent), agent (writer|thinker|analyser), apply "
                        "(bool default False). Output: {ok, run_id, applied, changed, "
                        "summary}.",
            schema=enum_schema(engine=["claude", "vera-agent"],
                               agent=["writer", "thinker", "analyser"]))
async def evolve_ide_improve(instance_id: str = "", goal: str = "",
                             engine: str = "claude", agent: str = "writer",
                             apply: bool = False, run_id: str = "", trace_id=None):
    if not instance_id:
        return {"error": "instance_id required"}
    if not (goal or "").strip():
        return {"error": "goal required"}
    run_id = run_id or uuid.uuid4().hex[:10]
    t0 = time.time()
    await emit_event({"type": "evolve.run.started", "run_id": run_id,
                      "task": "ide:" + instance_id, "task_type": "ide",
                      "where": "remote-ide", "source": "ide"})
    await emit_event({"type": "evolve.workflow", "run_id": run_id,
                      "node": "implementer", "state": "running", "where": "remote-ide"})
    # In diagnose mode we forbid edits and ask for concrete findings + the exact
    # edits it WOULD make — so the human can approve before anything changes.
    task = goal if apply else (
        "Do NOT modify, create or delete any files. Review the codebase against "
        "this goal, then report: (1) concrete problems you find, and (2) the exact "
        "edits you WOULD make to address the goal.\n\nGOAL:\n" + goal)
    res = await _call("ide.remote.run", instance_id=instance_id, task=task,
                      engine=engine, agent=agent, session_id=f"evolve:{run_id}")
    ok = bool(isinstance(res, dict) and res.get("ok"))
    summary = ((res or {}).get("summary", "") if isinstance(res, dict)
               else str(res))[:12000]
    changed = (res or {}).get("changed") if isinstance(res, dict) else None
    elapsed = round(time.time() - t0, 1)
    error = "" if ok else str((res or {}).get("error") or "remote run failed")

    compact = {"run_id": run_id, "task": "ide:" + instance_id, "label": instance_id,
               "task_type": "ide", "profile": "coding", "ts": now_iso(),
               "elapsed_s": elapsed, "pass_rate": 1.0 if ok else 0.0,
               "checks_ok": 1 if ok else 0, "checks_n": 1, "score": None,
               "combined": None, "variant": "", "source": "ide", "session": "",
               "error": error[:200], "where": "remote-ide", "engine": engine,
               "triggered_by": _triggered_by()}
    detail = dict(compact)
    detail.update({"goal": goal, "final": summary, "steps": [],
                   "checks": [{"type": ("applied" if apply else "diagnosed"),
                               "ok": ok}],
                   "raw_keys": list((res or {}).keys())[:20]
                               if isinstance(res, dict) else [],
                   "assessment": {"critique": summary[:2000], "score": None,
                                  "failures": [], "applied": bool(apply),
                                  "changed": changed}})
    await _push_run(compact, detail)
    await emit_event({"type": "evolve.workflow", "run_id": run_id,
                      "node": "implementer", "state": "done" if ok else "error"})
    await emit_event({"type": "evolve.workflow", "run_id": run_id,
                      "node": "output", "state": "done" if ok else "error"})
    await _audit("ide.improve",
                 f"{instance_id}: {goal[:80]} (apply={apply}, ok={ok})",
                 instance=instance_id, applied=bool(apply), changed=changed)
    await emit_event({"type": "evolve.run.done", "run_id": run_id,
                      "task": "ide:" + instance_id, "where": "remote-ide",
                      "combined": None, "elapsed_s": elapsed, "error": error[:120]})
    await emit_event({"type": "evolve.run.reviewed", "run_id": run_id,
                      "combined": None, "failures": 0})
    return {"ok": ok, "run_id": run_id, "applied": bool(apply),
            "changed": changed, "engine": engine, "summary": summary[:4000],
            "error": error or None}


@capability("evolve.selftest", memory="off",
            http_method="POST", http_path="/evolve/selftest", http_tags=["evolve"],
            description="Pre-flight the whole harness so you know WHY a run does "
                        "nothing: checks Redis, task seeding, loops.run engine "
                        "(trivial 1-cycle echo run), and that the critic + editor "
                        "providers actually respond. Output: {ok, checks:[{name,"
                        "ok,detail}]}.")
async def evolve_selftest(trace_id=None):
    checks: List[Dict[str, Any]] = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})

    # 1. Redis
    add("redis", _redis() is not None,
        "connected" if _redis() else "REDIS unavailable — nothing persists")

    # 2. Tasks seeded
    await _ensure_seeded()
    tasks = await _get_tasks()
    loop_tasks = [t for t in tasks if t.get("type") == "loop"]
    add("tasks", bool(tasks),
        f"{len(tasks)} tasks ({len(loop_tasks)} loop). "
        f"Loop profiles: {', '.join(sorted({t.get('profile','') for t in loop_tasks})) or '(none)'}")

    # 3. loops.run engine — a real, tiny run so we know the cluster answers
    cfg = await _get_config()
    try:
        res = await asyncio.wait_for(
            _call("loops.run", profile=cfg.get("default_profile", "planning"),
                  goal="Reply with exactly the word: ok",
                  allowed_caps="", session_id="evolve:selftest", max_steps=2),
            timeout=180)
        if isinstance(res, dict) and res.get("error"):
            add("loops.run", False, res["error"])
        else:
            fin = _norm_final(res) if isinstance(res, dict) else str(res)
            add("loops.run", True, f"engine responded ({len(fin)} chars final)")
    except asyncio.TimeoutError:
        add("loops.run", False, "timed out after 180s — the loop engine/cluster "
                                "is very slow or a model is unreachable")
    except Exception as e:
        add("loops.run", False, str(e))

    # 4/5. Critic + editor providers
    for role in ("critic", "editor"):
        spec = cfg.get(f"{role}_provider", "ollama")
        try:
            pr = await asyncio.wait_for(
                _provider_chat(spec, "Reply with exactly: ok", max_tokens=16),
                timeout=120)
            if pr.get("error"):
                add(f"{role} ({spec})", False, pr["error"])
            else:
                add(f"{role} ({spec})", bool(pr.get("text")),
                    f"responded via {pr.get('provider')}"
                    + (f" ${pr.get('cost_usd')}" if pr.get("cost_usd") else ""))
        except asyncio.TimeoutError:
            add(f"{role} ({spec})", False, "timed out")
        except Exception as e:
            add(f"{role} ({spec})", False, str(e))

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks,
            "hint": "" if ok else "Fix the failing checks above before running a "
                                  "suite or improve session — that's why nothing "
                                  "appears to happen."}


# ─────────────────────────────────────────────────────────────────────────────
# CRITIC / EDITOR LLM ROUTING — "vera can run, claude can edit"
# ─────────────────────────────────────────────────────────────────────────────

def _parse_provider(spec: str) -> Dict[str, str]:
    """'ollama' | 'ollama:qwen3:14b' | 'anthropic' | 'anthropic:claude-opus-4-8'
    | 'openai:gpt-4o' | '<stored provider id>[:model]'."""
    spec = (spec or "").strip() or "ollama"
    head, _, model = spec.partition(":")
    head = head.strip().lower()
    if head in ("ollama", "local", "vllm"):
        return {"kind": "local", "id": "", "model": model.strip()}
    return {"kind": "api", "id": head, "model": model.strip()}


async def _provider_chat(spec: str, prompt: str, system: str = "",
                         max_tokens: int = 2500, instance: str = "",
                         prefer_gpu: bool = True) -> Dict[str, Any]:
    """Route one generation to the chosen provider. Returns {text, provider,
    model, instance?, cost_usd?} or {error}. `instance` pins a specific ollama
    node (e.g. a CPU node) and forces prefer_gpu off — used by the background
    edit queue to run on gpt-oss:20b on a CPU node."""
    p = _parse_provider(spec)
    if p["kind"] == "local":
        res = await _call("llm.generate", prompt=prompt, system=system,
                          model=(p["model"] or None),
                          instance_id=(instance or None),
                          prefer_gpu=(False if instance else prefer_gpu),
                          job_type="chat", caller="evolve.editor")
        if isinstance(res, dict) and res.get("error"):
            return {"error": res["error"], "provider": "ollama"}
        return {"text": (res or {}).get("text", "") if isinstance(res, dict) else str(res),
                "provider": "ollama", "model": (res or {}).get("model", p["model"]),
                "instance": (res or {}).get("instance", instance)}
    res = await _call("providers.chat", provider=p["id"], model=p["model"],
                      prompt=prompt, system=system, max_tokens=max_tokens,
                      caller="evolve")
    if isinstance(res, dict) and res.get("error"):
        return {"error": res["error"], "provider": p["id"]}
    return {"text": (res or {}).get("text", ""), "provider": p["id"],
            "model": (res or {}).get("model", p["model"]),
            "cost_usd": (res or {}).get("cost_usd", 0.0)}


def _extract_json(text: str) -> Optional[Any]:
    """Pull the first JSON object out of an LLM reply (tolerates fences/prose)."""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return None


def _clamp_overrides(raw: Any) -> Dict[str, Any]:
    """Filter+clamp editor-proposed overrides to the tunable-knob whitelist."""
    out: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        spec = TUNABLE_KNOBS.get(k)
        if not spec:
            continue
        try:
            if spec["type"] is int:
                iv = int(v)
                out[k] = max(spec.get("min", iv), min(spec.get("max", iv), iv))
            elif spec["type"] is bool:
                out[k] = bool(v) if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            else:
                sv = str(v).strip()
                if sv:
                    out[k] = sv[:400]
        except Exception:
            continue
    return out


# ─────────────────────────────────────────────────────────────────────────────
# TASKS
# ─────────────────────────────────────────────────────────────────────────────

def _default_tasks() -> List[Dict[str, Any]]:
    return [
        {
            "id": "tool-echo", "label": "Tool use — echo roundtrip",
            "type": "loop", "profile": "operator", "tags": ["core", "loop"],
            "goal": "Call the echo capability with message='evolve-ping' and "
                    "report exactly what it returned.",
            "allowed_caps": "echo",
            "max_steps": 4, "timeout_s": 420, "enabled": True,
            "checks": [
                {"type": "cap_called", "value": "echo"},
                {"type": "contains", "value": "evolve-ping"},
            ],
            "rubric": "Did the loop actually call the echo tool (not hallucinate "
                      "a response) and report its output faithfully, in few steps?",
        },
        {
            "id": "memory-roundtrip", "label": "Memory — store & retrieve",
            "type": "loop", "profile": "fabric-discovery", "tags": ["core", "loop"],
            "goal": "Store a memory note with the exact text 'evolve marker alpha' "
                    "using memory.create, then find it again with memory.search "
                    "and quote the stored text back.",
            "allowed_caps": "memory.create,memory.search,memory.recall",
            "max_steps": 6, "timeout_s": 480, "enabled": True,
            "checks": [
                {"type": "cap_called", "value": "memory.create"},
                {"type": "cap_called", "value": "memory.search"},
                {"type": "contains", "value": "evolve marker alpha"},
            ],
            "rubric": "Both tools used in the right order; the quoted text matches "
                      "exactly; no invented memories.",
        },
        {
            "id": "fabric-lookup", "label": "Fabric — dataset survey",
            "type": "loop", "profile": "fabric-discovery", "tags": ["core", "loop"],
            # Deliberately does NOT name a capability — a scripted goal ("use
            # fabric.datasets") only tests instruction-following, not discovery.
            "goal": "Find out what the three most active (most-updated) datasets in "
                    "Vera's data fabric are, and roughly how many records each holds.",
            "allowed_caps": "",
            "max_steps": 6, "timeout_s": 420, "enabled": True,
            "checks": [
                {"type": "cap_called", "value": "memory.map"},
                {"type": "final_nonempty"},
            ],
            "rubric": "Used a real dataset-discovery tool it found on its own (not told "
                      "which one), named REAL dataset ids from the tool's actual output "
                      "(never guessed/invented), and ranked by recency/activity.",
        },
        {
            # Added 2026-08-03 after a live agent_loop_v5 run against real Vera
            # (goal: browse the fabric for agent-loop-failure knowledge, no query
            # given) showed the specialist correctly received the real dataset ids
            # from a prior step in its context, then STILL called fabric.browse on
            # two invented near-miss variants ('web.en.wikipedia_org',
            # 'web.en_wikipedia_org') that appeared nowhere in that context, wasting
            # cycles, before separately getting one real id right. Browsing (no
            # query) is a genuinely different failure surface from search.
            "id": "fabric-browse-records", "label": "Fabric — browse without a query",
            "type": "loop", "profile": "fabric-discovery", "tags": ["core", "loop", "memory"],
            "goal": "Pick any one real dataset that exists in Vera's data fabric and show "
                    "me 3 actual records from it. You do not have a search query — just "
                    "look at what's really there.",
            "allowed_caps": "",
            "max_steps": 6, "timeout_s": 420, "enabled": True,
            "checks": [
                {"type": "cap_called", "value": "memory.map"},
                {"type": "cap_called", "value": "memory.browse"},
                {"type": "final_nonempty"},
            ],
            "rubric": "CRITICAL: the dataset id passed to memory.browse must be a REAL id "
                      "that actually appeared in a prior memory.map/discovery call's output "
                      "— not a plausible-looking guess or invented variant. If a browse call "
                      "returns zero records, the loop should recover via memory.map again, "
                      "NOT retry slightly-different guessed spellings of the same wrong id. "
                      "The final answer must quote real record text, never fabricated "
                      "placeholder content.",
        },
        {
            # Added 2026-08-03 alongside fabric-browse-records — the SEARCH-shaped
            # counterpart. The same live run confirmed memory.seek + memory.read
            # worked well (found a real agent_loop.journal record, collapsed 33
            # near-duplicates, read it in full); this locks that behaviour in as a
            # regression guard.
            "id": "memory-seek-relevance", "label": "Memory — seek then read a real record",
            "type": "loop", "profile": "fabric-discovery", "tags": ["core", "loop", "memory"],
            "goal": "Find out what Vera's stored knowledge says about agent loop failure "
                    "recovery strategies, then quote one specific detail from the single "
                    "most relevant record you find, citing its dataset.",
            "allowed_caps": "",
            "max_steps": 6, "timeout_s": 420, "enabled": True,
            "checks": [
                {"type": "cap_called", "value": "memory.seek"},
                {"type": "cap_called", "value": "memory.read"},
                {"type": "final_nonempty"},
            ],
            "rubric": "Used memory.seek to find candidates (the canonical door, not raw "
                      "fabric.query), expanded the best one with memory.read rather than "
                      "settling for a truncated snippet, and the final answer cites a real "
                      "dataset id and quotes real record text.",
        },
        {
            "id": "web-brief", "label": "Web — grounded one-liner",
            "type": "loop", "profile": "fabric-discovery", "tags": ["loop", "web"],
            "goal": "Use web.search to find the current stable Python 3 release "
                    "series and answer in ONE sentence, citing one source URL.",
            "allowed_caps": "web.search,web.fetch",
            "max_steps": 5, "timeout_s": 480, "enabled": True,
            "checks": [
                {"type": "cap_called", "value": "web.search"},
                {"type": "regex", "value": r"https?://"},
            ],
            "rubric": "Search actually performed; answer is one sentence, current, "
                      "and cites a plausible source URL from the results.",
        },
        {
            "id": "format-json", "label": "Output discipline — strict JSON",
            "type": "loop", "profile": "coding", "tags": ["core", "loop"],
            "goal": "Output ONLY a JSON object mapping the keys a, b and c to the "
                    "integers 1, 2 and 3. No prose, no code fences.",
            "allowed_caps": "llm.generate",
            "max_steps": 3, "timeout_s": 300, "enabled": True,
            "checks": [
                {"type": "json_valid"},
                {"type": "contains", "value": '"a"'},
            ],
            "rubric": "Final answer is exactly the JSON object (no wrapper text). "
                      "Fewer steps is better — this needs no tools.",
        },
        {
            "id": "date-math", "label": "Multi-step — timestamp arithmetic",
            "type": "loop", "profile": "planning", "tags": ["loop"],
            "goal": "Get the current time with system.timestamp, then answer: how "
                    "many whole days remain until the end of this calendar year? "
                    "Answer with the number and one line of working.",
            "allowed_caps": "system.timestamp,llm.generate",
            "max_steps": 5, "timeout_s": 420, "enabled": True,
            "checks": [
                {"type": "cap_called", "value": "system.timestamp"},
                {"type": "regex", "value": r"\d{1,3}"},
            ],
            "rubric": "Used the real clock (not training-data guesses); arithmetic "
                      "is correct for the timestamp it fetched.",
        },
        # ── cap-type smoke tests — 'all other systems' regression checks ────
        {
            "id": "smoke-fabric", "label": "Smoke — fabric datasets cap",
            "type": "cap", "tags": ["core", "smoke"],
            "cap": "fabric.datasets", "args": {},
            "timeout_s": 60, "enabled": True,
            "checks": [{"type": "no_error"}, {"type": "final_nonempty"}],
            "rubric": "",
        },
        {
            "id": "smoke-dream", "label": "Smoke — dream scheduler status",
            "type": "cap", "tags": ["core", "smoke"],
            "cap": "dream.scheduler.status", "args": {},
            "timeout_s": 60, "enabled": True,
            "checks": [{"type": "no_error"}],
            "rubric": "",
        },
        {
            "id": "smoke-llm", "label": "Smoke — local LLM generate",
            "type": "cap", "tags": ["core", "smoke"],
            "cap": "llm.generate",
            "args": {"prompt": "Reply with exactly the word: pong", "caller": "evolve.smoke"},
            "timeout_s": 120, "enabled": True,
            "checks": [{"type": "no_error"}, {"type": "contains", "value": "pong"}],
            "rubric": "",
        },
        # ── dream smoke tests (the self-improve loop covers the dream system) ──
        {
            "id": "smoke-dream-preview", "label": "Smoke — dream pipeline preview",
            "type": "cap", "tags": ["core", "smoke", "dream"],
            "cap": "dream.preview",
            "args": {"trigger_name": "daily_ops_report"},
            "timeout_s": 120, "enabled": False,   # off by default (can be slow)
            "checks": [{"type": "no_error"}],
            "rubric": "",
        },
        {
            "id": "dream-reason", "label": "Dream — inspect the dream system",
            "type": "loop", "profile": "planning", "tags": ["dream", "loop"],
            "goal": "Report the dream scheduler status (dream.scheduler.status) and "
                    "name the most recent dream cycle from dream.last with its "
                    "trigger and whether it produced a report.",
            "allowed_caps": "dream.scheduler.status,dream.last,dream.history",
            "max_steps": 5, "timeout_s": 240, "enabled": True,
            "checks": [
                {"type": "cap_called", "value": "dream.scheduler.status"},
                {"type": "final_nonempty"},
            ],
            "rubric": "Used the real dream caps; reported grounded status + last "
                      "cycle (no invented dreams).",
        },
        # ── sim task — GROUND-TRUTH scoring from the business simulation ──────
        {
            "id": "sim-reseller-grow", "label": "Sim — grow the reseller net",
            "type": "sim", "profile": "operator", "scenario": "reseller",
            "tags": ["sim", "loop"],
            "goal": "Operate the SIMULATED reselling business (is_sim=1) toward a "
                    "higher net this period: review the dashboard, then take "
                    "sensible actions (pricing, listing, restock) using the "
                    "business capabilities. Every money movement must pass is_sim=1.",
            "rubric": "Improved the simulated net vs baseline through grounded, "
                      "valid business actions.",
            "allowed_caps": "",   # operator profile supplies the business toolkit
            "max_steps": 8, "timeout_s": 420, "enabled": False,   # opt-in (needs sim)
            "checks": [{"type": "final_nonempty"}],
        },
    ]


async def _ensure_seeded() -> None:
    """Guarantee the task hash is populated. The startup seeder can miss (Redis
    slow to come up, or a botched merge set), which left the suite returning
    'no matching tasks' and nothing running. Any entry point that needs tasks
    calls this first, so the harness is self-healing."""
    r = _redis()
    if not r:
        return
    try:
        if await r.hlen(KEY_TASKS):
            return
        for t in _default_tasks():
            await _save_task(t)
        names = [t["id"] for t in _default_tasks()]
        if names:
            await r.sadd(KEY_SEEDED, *names)
        log.info("evolve: auto-seeded %d default tasks (hash was empty)", len(names))
    except Exception as e:
        log.debug("evolve ensure_seeded: %s", e)


async def _get_tasks() -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return _default_tasks()
    await _ensure_seeded()
    try:
        raw = await r.hgetall(KEY_TASKS)
        out = []
        for v in (raw or {}).values():
            try:
                out.append(json.loads(v.decode() if isinstance(v, bytes) else v))
            except Exception:
                continue
        # If somehow still empty, fall back to the in-code defaults so the
        # panel and suite are never blank.
        return sorted(out, key=lambda t: t.get("id", "")) or _default_tasks()
    except Exception:
        return _default_tasks()


async def _get_task(task_id: str) -> Optional[Dict[str, Any]]:
    r = _redis()
    if not r:
        return next((t for t in _default_tasks() if t["id"] == task_id), None)
    try:
        raw = await r.hget(KEY_TASKS, task_id)
        if not raw:
            return None
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        return None


async def _save_task(task: Dict[str, Any]):
    r = _redis()
    if r:
        try:
            await r.hset(KEY_TASKS, task["id"], json.dumps(task, default=str))
        except Exception:
            pass


@capability("evolve.tasks", memory="off", silent=True,
            http_method="GET", http_path="/evolve/tasks", http_tags=["evolve"],
            description="List benchmark tasks (loop-type goals + cap-type smoke "
                        "tests). Query: tag (str filter).")
async def evolve_tasks(tag: str = "", trace_id=None):
    tasks = await _get_tasks()
    if tag:
        tasks = [t for t in tasks if tag in (t.get("tags") or [])]
    return {"tasks": tasks, "count": len(tasks)}


@capability("evolve.task.upsert", memory="off",
            http_method="POST", http_path="/evolve/task/upsert", http_tags=["evolve"],
            description="Create/update a benchmark task. Pass the full task record "
                        "(id!, label, type: loop|cap, goal/profile/allowed_caps or "
                        "cap/args, checks:[{type,value}], rubric, tags, max_steps, "
                        "timeout_s, enabled).")
async def evolve_task_upsert(task: Optional[Dict[str, Any]] = None, trace_id=None):
    # The full task record arrives via `task` (both the evolve panel and the
    # markets seeder call with task=<dict>). A previous `**fields` catch-all was
    # mis-rendered by the MCP bridge as a spurious REQUIRED `fields` string,
    # which then leaked a junk "fields" key into the saved task and mangled
    # non-ASCII labels (em-dash → mojibake). Take only the task dict.
    rec = dict(task or {})
    if not rec.get("id"):
        return {"error": "task id required"}
    rec.setdefault("type", "loop")
    rec.setdefault("enabled", True)
    rec.setdefault("tags", [])
    rec.setdefault("checks", [])
    await _save_task(rec)
    return {"ok": True, "task": rec}


@capability("evolve.task.delete", memory="off",
            http_method="POST", http_path="/evolve/task/delete", http_tags=["evolve"],
            description="Delete a benchmark task by id.")
async def evolve_task_delete(id: str = "", trace_id=None):
    r = _redis()
    if r and id:
        try:
            await r.hdel(KEY_TASKS, id)
        except Exception:
            pass
    return {"ok": True}


_GEN_SYSTEM = (
    "You are a test engineer for an autonomous agent platform (Vera). Given a "
    "goal or a subsystem to exercise, and the list of capability names available, "
    "you design concrete BENCHMARK TASKS that verify the agent loop does the "
    "right thing. Reply with ONLY a JSON array; each task is:\n"
    '{"id":"kebab-id","label":"short","type":"loop|cap","profile":"<loop profile '
    'id>","goal":"<precise instruction the agent must satisfy>","allowed_caps":'
    '"csv of caps it may use","cap":"<for type=cap>","args":{...},"checks":'
    '[{"type":"cap_called|contains|regex|json_valid|final_nonempty|no_error",'
    '"value":"..."}],"rubric":"<how a human judges success>","max_steps":5,'
    '"timeout_s":240,"tags":["generated"]}\n'
    "Checks must be programmatically verifiable against the trace/output. Prefer "
    "cap_called for tool use and contains/regex for grounded facts. Keep goals "
    "specific and gradeable. No prose outside the JSON array."
)


@capability("evolve.tasks.generate", memory="on",
            http_method="POST", http_path="/evolve/tasks/generate", http_tags=["evolve"],
            description="LLM-generate benchmark tasks on the fly from a goal or "
                        "subsystem, saved (tagged 'generated') for comparative "
                        "runs. Inputs: goal (str! — what to test / a subsystem "
                        "name), count (int default 3), profile (str — loop profile "
                        "for generated loop tasks), cap_prefix (str — bias caps to "
                        "this family, e.g. 'dream.'), provider (str — editor LLM), "
                        "save (bool default True), tag (str default 'generated'). "
                        "Output: {ok, tasks:[…], saved}.")
async def evolve_tasks_generate(goal: str = "", count: int = 3, profile: str = "",
                                cap_prefix: str = "", provider: str = "",
                                save: bool = True, tag: str = "generated",
                                trace_id=None):
    if not goal.strip():
        return {"error": "goal required"}
    cfg = await _get_config()
    profile = (profile or cfg["default_profile"]).strip()
    # Offer the LLM the relevant capability surface so it references real caps.
    caps: List[str] = []
    search = CAPABILITY_REGISTRY.get("caps.search") or CAPABILITY_REGISTRY.get("caps.list")
    if search and search.get("func"):
        try:
            res = await search["func"](query=(cap_prefix or goal), limit=60)
            for c in (res or {}).get("caps", []) if isinstance(res, dict) else []:
                nm = c.get("name") if isinstance(c, dict) else str(c)
                if nm and (not cap_prefix or nm.startswith(cap_prefix)):
                    caps.append(nm)
        except Exception:
            pass
    if not caps:
        caps = [k for k in CAPABILITY_REGISTRY
                if not cap_prefix or k.startswith(cap_prefix)][:80]
    prof_ids = []
    lp = CAPABILITY_REGISTRY.get("loops.profiles")
    if lp and lp.get("func"):
        try:
            prof_ids = [p.get("id") for p in (await lp["func"]()).get("profiles", [])]
        except Exception:
            pass
    prompt = (f"GOAL / SUBSYSTEM TO TEST:\n{goal}\n\n"
              f"Design {max(1, min(8, int(count)))} tasks. Default loop profile: "
              f"{profile}. Available loop profiles: {', '.join(p for p in prof_ids if p) or profile}.\n"
              f"Capabilities available (reference REAL ones):\n"
              + ", ".join(caps[:80]))
    res = await _provider_chat(provider or cfg["editor_provider"], prompt,
                               system=_GEN_SYSTEM, max_tokens=2500)
    if res.get("error"):
        return {"error": res["error"]}
    arr = _extract_json_array(res.get("text", ""))
    if not arr:
        return {"error": "LLM did not return a usable task array",
                "raw": res.get("text", "")[:600]}
    out = []
    for i, t in enumerate(arr[: int(count) + 2]):
        if not isinstance(t, dict) or not (t.get("goal") or t.get("cap")):
            continue
        tid = re.sub(r"[^a-z0-9-]+", "-",
                     str(t.get("id") or f"gen-{uuid.uuid4().hex[:6]}").lower()).strip("-")
        rec = {
            "id": tid, "label": str(t.get("label") or tid)[:80],
            "type": "cap" if t.get("type") == "cap" else "loop",
            "profile": t.get("profile") or profile,
            "goal": str(t.get("goal") or "")[:2000],
            "allowed_caps": t.get("allowed_caps") or "",
            "cap": t.get("cap") or "", "args": t.get("args") or {},
            "checks": [c for c in (t.get("checks") or []) if isinstance(c, dict)][:6],
            "rubric": str(t.get("rubric") or "")[:800],
            "max_steps": int(t.get("max_steps", 5) or 5),
            "timeout_s": int(t.get("timeout_s", 240) or 240),
            "tags": list({*(t.get("tags") or []), tag, "generated"}),
            "enabled": True, "generated_from": goal[:200],
        }
        out.append(rec)
        if save:
            await _save_task(rec)
    await emit_event({"type": "evolve.tasks.generated", "count": len(out),
                      "goal": goal[:120], "tag": tag})
    return {"ok": True, "tasks": out, "saved": save, "tag": tag}


def _extract_json_array(text: str) -> Optional[List[Any]]:
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return None


@capability("evolve.goal.run", memory="on",
            http_method="POST", http_path="/evolve/goal/run", http_tags=["evolve"],
            description="Run an ad-hoc GOAL through a loop and analyse the result "
                        "with the critic — the quickest way to probe the loop and "
                        "read a grounded critique. Optionally save it as a reusable "
                        "benchmark task for comparative runs. Inputs: goal (str!), "
                        "profile (str), allowed_caps (csv), max_steps (int), "
                        "provider (str — critic), checks (list — optional ground-"
                        "truth), save_as (str — task id to persist). "
                        "Output: the run record + assessment.")
async def evolve_goal_run(goal: str = "", profile: str = "", allowed_caps: str = "",
                          max_steps: int = 6, provider: str = "",
                          checks: Optional[List[Dict[str, Any]]] = None,
                          save_as: str = "", trace_id=None):
    if not goal.strip():
        return {"error": "goal required"}
    cfg = await _get_config()
    task = {
        "id": save_as or f"adhoc-{uuid.uuid4().hex[:6]}",
        "label": goal[:60], "type": "loop",
        "profile": (profile or cfg["default_profile"]).strip(),
        "goal": goal, "allowed_caps": allowed_caps,
        "checks": [c for c in (checks or []) if isinstance(c, dict)],
        "rubric": "Did the loop accomplish the goal with grounded tool use and no "
                  "hallucination, economically?",
        "max_steps": int(max_steps or 6), "timeout_s": 300,
        "tags": ["adhoc"], "enabled": True,
    }
    if save_as:
        task["tags"] = ["saved", "generated"]
        await _save_task(task)
    detail = await _run_task(task, source="goal")
    a = await _assess_run(detail, task, provider or cfg["critic_provider"])
    detail["assessment"] = a
    detail["score"] = a.get("score")
    detail["combined"] = a.get("combined", detail["combined"])
    return {**detail, "saved_task": save_as or ""}


@capability("evolve.cap.test", memory="off",
            http_method="POST", http_path="/evolve/cap/test", http_tags=["evolve"],
            description="Test ANY capability ad-hoc: call it with args and grade the "
                        "result against checks — respecting sandbox-first mode so a "
                        "write cap runs against isolated state, not real Vera. "
                        "Inputs: cap (str!), args (object), checks (list "
                        "[{type,value}]), save_as (str — persist as a cap task). "
                        "Output: the run record.")
async def evolve_cap_test(cap: str = "", args: Optional[Dict[str, Any]] = None,
                          checks: Optional[List[Dict[str, Any]]] = None,
                          save_as: str = "", trace_id=None):
    if not cap:
        return {"error": "cap required"}
    task = {
        "id": save_as or f"captest-{uuid.uuid4().hex[:6]}",
        "label": f"cap test: {cap}", "type": "cap", "cap": cap,
        "args": args or {}, "checks": [c for c in (checks or []) if isinstance(c, dict)]
                 or [{"type": "no_error"}],
        "tags": ["captest"], "timeout_s": 120, "enabled": True,
    }
    if save_as:
        task["tags"] = ["saved", "captest"]
        await _save_task(task)
    detail = await _run_task(task, source="captest")
    return {**detail, "saved_task": save_as or ""}


@capability("evolve.unittest.run", memory="on",
            http_method="POST", http_path="/evolve/unittest/run", http_tags=["evolve"],
            description="Unit-test any part of Vera (or a registered repo): run "
                        "pytest or a py_compile import-check over a path and gate "
                        "on the exit code. Inputs: path (str default 'vera'), mode "
                        "(pytest|compile, default compile — safe, no code "
                        "execution), pattern (str — pytest -k), timeout (int=300), "
                        "repo (str, default 'vera' — id of a repo registered via "
                        "evolve.repo.add). Output: {ok, rc, passed, summary, "
                        "output}.",
            schema=enum_schema(mode=["compile", "pytest"]))
async def evolve_unittest_run(path: str = "vera", mode: str = "compile",
                              pattern: str = "", timeout: int = 300,
                              repo: str = DEFAULT_REPO_ID, trace_id=None):
    root = await _resolve_repo_root(repo)
    target = (root / path)
    if not target.exists():
        return {"error": f"path not found: {path}"}
    if mode == "pytest":
        cmd = [sys.executable, "-m", "pytest", path, "-q"]
        if pattern:
            cmd += ["-k", pattern]
    else:
        # Safe default: byte-compile every .py under path (catches syntax/import
        # -time errors without executing anything).
        pyfiles = [str(p.relative_to(root)) for p in target.rglob("*.py")
                   if "__pycache__" not in str(p)][:2000] if target.is_dir() \
            else [path]
        if not pyfiles:
            return {"error": "no .py files under path"}
        cmd = [sys.executable, "-m", "py_compile"] + pyfiles
    res = await _sh(cmd, cwd=str(root), timeout=int(timeout))
    passed = res["ok"]
    out = (res.get("out", "") + "\n" + res.get("err", "")).strip()
    await _audit("unittest", f"{mode} {path}: {'PASS' if passed else 'FAIL'}",
                 mode=mode, path=path, rc=res.get("code"))
    await emit_event({"type": "evolve.unittest.done", "path": path, "mode": mode,
                      "passed": passed, "rc": res.get("code")})
    return {"ok": passed, "rc": res.get("code"), "passed": passed,
            "mode": mode, "path": path,
            "summary": ("all files compiled" if passed and mode == "compile"
                        else "tests passed" if passed else "failures — see output"),
            "output": out[-8000:]}


# ─────────────────────────────────────────────────────────────────────────────
# CHECKS — programmatic ground truth
# ─────────────────────────────────────────────────────────────────────────────

def _run_checks(task: Dict[str, Any], final: str, steps: List[Dict[str, Any]],
                elapsed_s: float, error: str = "") -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    caps_called = {str(s.get("cap") or s.get("tool") or "") for s in steps}
    blob = final or ""
    for chk in task.get("checks") or []:
        ctype = str(chk.get("type", ""))
        val = chk.get("value", "")
        ok, note = False, ""
        try:
            if ctype == "contains":
                ok = str(val).lower() in blob.lower()
            elif ctype == "not_contains":
                ok = str(val).lower() not in blob.lower()
            elif ctype == "regex":
                ok = bool(re.search(str(val), blob, re.I | re.S))
            elif ctype == "cap_called":
                v = str(val)
                ok = any(c == v or c.startswith(v) for c in caps_called)
                if not ok:
                    note = f"called: {sorted(c for c in caps_called if c)[:8]}"
            elif ctype == "min_steps":
                ok = len(steps) >= int(val)
            elif ctype == "max_steps":
                ok = len(steps) <= int(val)
            elif ctype == "max_seconds":
                ok = elapsed_s <= float(val)
            elif ctype == "final_nonempty":
                ok = bool(blob.strip())
            elif ctype == "json_valid":
                ok = _extract_json(blob) is not None
            elif ctype == "no_error":
                ok = not error
                note = error[:200] if error else ""
            else:
                note = f"unknown check type {ctype!r}"
        except Exception as e:
            note = str(e)
        results.append({"type": ctype, "value": val, "ok": ok, "note": note})
    return results


def _norm_steps(res: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = res.get("steps") or res.get("history") or []
    out = []
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict):
            continue
        out.append({
            "cap":     s.get("cap") or s.get("tool") or s.get("name") or "",
            "ok":      (s.get("ok") if s.get("ok") is not None
                        else not s.get("error")),
            "preview": str(s.get("preview") or s.get("result") or s.get("output")
                           or s.get("observation") or "")[:300],
            "thought": str(s.get("thought") or s.get("reason") or "")[:200],
        })
    return out[:60]


def _norm_final(res: Dict[str, Any]) -> str:
    for k in ("final", "answer", "summary", "result", "text", "report"):
        v = res.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


async def _steps_from_events(run_id: str) -> List[Dict[str, Any]]:
    """Rebuild the TOOL TRACE from the run's persisted agent-loop events
    (vera:loop:events:evolve:<run_id>). The loops.run RESULT often carries no
    usable steps list (engines return only the final synthesis), which left the
    adversarial reviewer judging '(no tool calls)' after a 25-step run. The
    event log is the ground truth — every tool_call/tool_done is there."""
    r = _redis()
    if not r:
        return []
    key = f"vera:loop:events:evolve:{run_id}"
    raw = []
    try:
        raw = await r.lrange(key, 0, -1)
    except Exception:
        pass
    if not raw:
        # A SANDBOX run persisted its events in the dev Redis DB (same server,
        # DB VERA_DEV_REDIS_DB) — read them from there with a throwaway client.
        try:
            import redis.asyncio as _aredis
            url = _redis_url_with_db(os.getenv("REDIS_URL", "redis://localhost:6379"),
                                     DEV_REDIS_DB)
            dev = _aredis.from_url(url)
            try:
                raw = await dev.lrange(key, 0, -1)
            finally:
                try:
                    await dev.aclose()
                except Exception:
                    try:
                        await dev.close()
                    except Exception:
                        pass
        except Exception:
            raw = []
    steps: List[Dict[str, Any]] = []
    pending: Dict[str, Any] = {}
    for x in raw or []:
        try:
            ev = json.loads(x.decode() if isinstance(x, bytes) else x)
        except Exception:
            continue
        t = str(ev.get("type", ""))
        if t.endswith(".tool_call"):
            pending = {"cap": ev.get("tool") or ev.get("cap") or "",
                       "thought": str(ev.get("thought") or "")[:200],
                       "args": str(ev.get("args") or "")[:150]}
        elif t.endswith(".tool_done"):
            steps.append({
                "cap": ev.get("tool") or ev.get("cap") or pending.get("cap", ""),
                "ok": bool(ev.get("ok", not ev.get("error"))),
                "preview": str(ev.get("preview") or ev.get("error") or "")[:300],
                "thought": pending.get("thought", ""),
            })
            pending = {}
    return steps[:60]


# ─────────────────────────────────────────────────────────────────────────────
# RUN — execute one task (loop or cap), check it, persist the record
# ─────────────────────────────────────────────────────────────────────────────

async def _push_run(compact: Dict[str, Any], detail: Dict[str, Any]):
    r = _redis()
    if not r:
        return
    try:
        await r.lpush(KEY_RUNS, json.dumps(compact, default=str))
        await r.ltrim(KEY_RUNS, 0, RUNS_CAP - 1)
        await r.set(KEY_RUN + compact["run_id"], json.dumps(detail, default=str))
        await r.expire(KEY_RUN + compact["run_id"], 14 * 86400)
    except Exception as e:
        log.debug("evolve push run: %s", e)


async def _update_run(run_id: str, patch: Dict[str, Any]):
    """Merge patch into both the detail record and the compact list entry."""
    r = _redis()
    if not r:
        return
    try:
        raw = await r.get(KEY_RUN + run_id)
        if raw:
            det = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            det.update(patch)
            await r.set(KEY_RUN + run_id, json.dumps(det, default=str))
            await r.expire(KEY_RUN + run_id, 14 * 86400)
        rows = await r.lrange(KEY_RUNS, 0, RUNS_CAP - 1)
        for i, row in enumerate(rows or []):
            try:
                rec = json.loads(row.decode() if isinstance(row, bytes) else row)
            except Exception:
                continue
            if rec.get("run_id") == run_id:
                for k in ("score", "combined", "assessed_by", "passed"):
                    if k in patch:
                        rec[k] = patch[k]
                await r.lset(KEY_RUNS, i, json.dumps(rec, default=str))
                break
    except Exception as e:
        log.debug("evolve update run: %s", e)


def _variant_call_overrides(variant: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Turn a variant into loops.run kwargs: whitelisted knobs + the prompt
    preamble folded into system_prompt_template (engines that don't accept it
    filter it out via their schema)."""
    if not variant:
        return {}
    kw = dict(_clamp_overrides(variant.get("overrides") or {}))
    pre = (variant.get("prompt_preamble") or "").strip()
    if pre:
        kw["system_prompt_template"] = pre
    return kw


def _hget(h: Dict[Any, Any], key: str) -> str:
    """hgetall keys/values may be bytes or str depending on the client's
    decode_responses setting — normalise a single lookup."""
    for k, v in (h or {}).items():
        kk = k.decode() if isinstance(k, bytes) else k
        if kk == key:
            return v.decode() if isinstance(v, bytes) else str(v)
    return ""


async def _loop_activity(run_id: str, goal: str, t0_epoch: float) -> Tuple[str, int]:
    """An ACTIVITY signal for a running test: (fingerprint, event_count).
    `fingerprint` is the latest `updated_at` seen across the run's own session
    PLUS any goal-matched child sessions started since the run began (strategic
    v7 engines execute in their own generated sessions) — used by the watchdog
    as the liveness check because it changes on EVERY event and never
    saturates (unlike a raw event-list length, which Redis trims at
    _RESUME_MAX_EVENTS for long-running loops, making a genuinely busy loop
    look idle). `event_count` is only for status-line reporting.
    Returns ("", -1) on a Redis failure (caller treats that as unknown, not
    idle — everything in Loop Lab already depends on Redis)."""
    r = _redis()
    if not r:
        return ("", -1)
    fp = ""
    n = 0
    try:
        my_run = await r.hgetall(f"vera:loop:run:evolve:{run_id}")
        u = _hget(my_run, "updated_at")
        if u:
            fp = max(fp, u)
        n += int(await r.llen(f"vera:loop:events:evolve:{run_id}") or 0)
        g = (goal or "").strip()[:400]
        if g:
            ids = await r.zrevrange("vera:loop:sessions", 0, 19, withscores=True)
            for iid, score in ids or []:
                sid = iid.decode() if isinstance(iid, bytes) else str(iid)
                if sid.startswith("evolve:"):
                    continue
                run = await r.hgetall(f"vera:loop:run:{sid}")
                # Match on the child's own recorded START time, not the zset
                # score (which is bumped to "now" on every event a session
                # emits — a long-lived unrelated session with the same goal
                # text would otherwise pass a "started recently" check forever).
                started = _hget(run, "started_at")
                try:
                    # now_iso() is UTC (datetime.utcnow()+"Z") — timegm (NOT
                    # mktime, which assumes local time) gives the matching UTC
                    # epoch so this compares correctly against time.time().
                    st_epoch = calendar.timegm(time.strptime(started[:19], "%Y-%m-%dT%H:%M:%S")) \
                        if started else float(score)
                except Exception:
                    st_epoch = float(score)
                if st_epoch < t0_epoch - 30:
                    continue
                if _hget(run, "goal").strip()[:400] != g:
                    continue
                cu = _hget(run, "updated_at")
                if cu:
                    fp = max(fp, cu)
                n += int(await r.llen(f"vera:loop:events:{sid}") or 0)
    except Exception as e:
        log.debug("evolve loop activity: %s", e)
        return ("", -1)
    return (fp, n)


def _dev_redis_client():
    """A throwaway async client on the DEV sandbox's Redis DB (same server, DB
    VERA_DEV_REDIS_DB). The sandbox persists its loop events + run-state there;
    prod reads DB 0, so this bridges the two. Caller must aclose() it."""
    try:
        import redis.asyncio as _aredis
        url = _redis_url_with_db(os.getenv("REDIS_URL", "redis://localhost:6379"),
                                 DEV_REDIS_DB)
        return _aredis.from_url(url)
    except Exception:
        return None


async def _mirror_loop_session(run_id: str, stop: asyncio.Event) -> None:
    """LIVE-mirror a sandbox loop's persisted state from the dev Redis DB into
    prod's DB, so the prod UI at :8999 — which polls
    /workshop/agent_loop/session_state against DB 0 — shows a SANDBOX run's
    implementer timeline live, exactly like an in-process run. Without this the
    events land only in the sandbox's DB 3 and the timeline stays empty.

    Mirrors the three keys the UI reads for session evolve:<run_id>:
      • vera:loop:run:<sid>     hash — run status / goal / counters
      • vera:loop:events:<sid>  list — the step events (appended incrementally)
      • vera:loop:sessions      zset — activity score (liveness + child discovery)
    Runs until `stop` is set, then does ONE final pass so the terminal '…done'
    event and the final status are never missed."""
    dst = _redis()
    src = _dev_redis_client()
    if dst is None or src is None:
        return
    sid = f"evolve:{run_id}"
    run_key = f"vera:loop:run:{sid}"
    ev_key = f"vera:loop:events:{sid}"
    try:
        while True:
            final = stop.is_set()
            try:
                h = await src.hgetall(run_key)
                if h:
                    await dst.hset(run_key, mapping=h)
                # events: append only the new tail; full resync if it shrank
                s_tot = int(await src.llen(ev_key) or 0)
                d_tot = int(await dst.llen(ev_key) or 0)
                if s_tot > d_tot:
                    tail = await src.lrange(ev_key, d_tot, -1)
                    if tail:
                        await dst.rpush(ev_key, *tail)
                elif s_tot < d_tot:
                    await dst.delete(ev_key)
                    allv = await src.lrange(ev_key, 0, -1)
                    if allv:
                        await dst.rpush(ev_key, *allv)
                sc = await src.zscore("vera:loop:sessions", sid)
                if sc is not None:
                    await dst.zadd("vera:loop:sessions", {sid: sc})
            except Exception as e:
                log.debug("sandbox mirror %s: %s", sid, e)
            if final:
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.7)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await src.aclose()
        except Exception:
            try:
                await src.close()
            except Exception:
                pass


@contextlib.asynccontextmanager
async def _sandbox_mirror(run_id: str, use_sandbox: bool):
    """Scope a live sandbox→prod loop-session mirror around a sandbox run so its
    timeline streams into the prod UI. A no-op for in-process runs (their events
    already land in prod's DB)."""
    if not use_sandbox:
        yield
        return
    stop = asyncio.Event()
    task = asyncio.ensure_future(_mirror_loop_session(run_id, stop))
    try:
        yield
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=8)
        except Exception:
            if not task.done():
                task.cancel()


async def _run_loop_task(task: Dict[str, Any], variant: Optional[Dict[str, Any]],
                         run_id: str, timeout: int) -> Dict[str, Any]:
    kw = dict(task.get("overrides") or {})
    kw.update(_variant_call_overrides(variant))
    # A Loop Lab test runs HEADLESS — never let the loop pause for a human
    # (enable_step_questions/HITL) or it hangs until the 300s timeout with no
    # visible progress. The caller/variant can still override.
    kw.setdefault("enable_step_questions", False)
    args = dict(profile=task.get("profile", "planning"),
                goal=task.get("goal", ""),
                allowed_caps=_deny_filter(task.get("allowed_caps", ""),
                                          task.get("_denylist") or []),
                session_id=f"evolve:{run_id}",
                max_steps=int(task.get("max_steps", 6) or 6), **kw)
    use_sandbox = task.get("_sandbox", False)
    if not task.get("_indefinite"):
        # bounded run (suite / benchmark / improve variant test). Mirror the
        # sandbox session into prod's Redis so its timeline shows in the :8999 UI.
        async with _sandbox_mirror(run_id, use_sandbox):
            coro = _exec_cap("loops.run", args, use_sandbox, http_timeout=timeout)
            res = await asyncio.wait_for(coro, timeout=timeout)
        return res if isinstance(res, dict) else {"final": str(res)[:4000]}

    # ── Interactive test: ACTIVITY-AWARE watchdog, not a fixed clock ─────────
    # The loop under test may run for a very long time; kill it only when it
    # stops making progress (no new loop events for run_idle_timeout_s) or at
    # the run_max_s hard ceiling (0 = unlimited). When the run executes in the
    # dev sandbox, the call crosses HTTP (_sandbox_call) — that request's OWN
    # timeout must be at least run_max_s (or None or unlimited), or the sandbox
    # kills an actively-progressing loop underneath this watchdog regardless.
    idle_s = max(60, int(task.get("_idle_s", 300) or 300))
    max_s = int(task.get("_max_s", 7200) or 0)
    http_timeout = float(max_s) if max_s else None   # None = no client-side cap
    coro = _exec_cap("loops.run", args, use_sandbox, http_timeout=http_timeout)
    t0 = time.time()
    # Mirror the sandbox session into prod's Redis for the whole watchdog window
    # so long-horizon sandbox runs stream their timeline into the :8999 UI live.
    async with _sandbox_mirror(run_id, use_sandbox):
        runner = asyncio.ensure_future(coro)
        last_fp, last_n, last_change = "", -1, time.time()
        try:
            while True:
                done, _ = await asyncio.wait({runner}, timeout=10)
                if done:
                    res = runner.result()
                    return res if isinstance(res, dict) else {"final": str(res)[:4000]}
                fp, n = await _loop_activity(run_id, task.get("goal", ""), t0)
                # fp ("" only on a Redis error) is the liveness signal — it changes
                # on every event and, unlike a raw list length, never saturates.
                if fp and fp != last_fp:
                    last_fp, last_n, last_change = fp, n, time.time()
                    _RUN_LIVE["events"] = n
                    _RUN_LIVE["last_activity"] = now_iso()
                idle = time.time() - last_change
                if max_s and (time.time() - t0) > max_s:
                    runner.cancel()
                    return {"error": f"exceeded run_max_s hard ceiling ({max_s}s) "
                                     f"after {last_n} events"}
                if idle > idle_s:
                    runner.cancel()
                    return {"error": f"no loop activity for {int(idle)}s "
                                     f"(idle timeout; {last_n} events emitted) — "
                                     "the loop stopped making progress"}
        finally:
            if not runner.done():
                runner.cancel()
                # swallow the cancellation so it doesn't leak as an unretrieved error
                try:
                    await runner
                except BaseException:
                    pass


# ── Sandbox-first execution helpers ───────────────────────────────────────────

def _deny_filter(allowed_caps: str, denylist: List[str]) -> str:
    """Strip denied cap prefixes from an explicit allowed_caps csv (defence in
    depth). Empty allowed_caps is left empty (the profile toolkit applies — real
    isolation then comes from running in the sandbox)."""
    if not allowed_caps or not denylist:
        return allowed_caps
    keep = [c.strip() for c in allowed_caps.split(",") if c.strip()
            and not any(c.strip().startswith(d) for d in denylist)]
    return ",".join(keep)


async def _sandbox_call(name: str, args: Dict[str, Any],
                        timeout: Optional[float] = 300) -> Any:
    """Invoke a capability INSIDE the dev sandbox via its /mcp/call endpoint, so
    it runs against the sandbox's isolated state — never real Vera. `timeout`
    is the HTTP client's own ceiling on top of whatever's calling us — pass
    None for no client-side cap (used by the activity-aware watchdog, which
    does its own idle/max-duration cancellation instead of a fixed clock)."""
    await _dev_port()          # ensure the base url uses the configured port
    _base = await _dev_resolve_base()
    if not _base:
        return {"error": "dev sandbox not reachable (no http/https Vera on its port)"}
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False) as c:
            r = await c.post(_base + "/mcp/call",
                             json={"name": name, "arguments": args})
        r.raise_for_status()
        d = r.json()
        # /mcp/call wraps the result; unwrap common shapes.
        if isinstance(d, dict):
            return d.get("result", d.get("content", d))
        return d
    except Exception as e:
        return {"error": f"sandbox call {name}: {e}"}


async def _exec_cap(name: str, args: Dict[str, Any], use_sandbox: bool,
                    http_timeout: Optional[float] = 300) -> Any:
    """Run a cap in-process OR in the dev sandbox depending on the resolved
    mode. `http_timeout` only matters for the sandbox path (the HTTP hop) —
    None disables the client-side cap so an outer watchdog (activity-aware
    interactive runs) is the only thing that can end the call."""
    if use_sandbox:
        return await _sandbox_call(name, args, timeout=http_timeout)
    return await _call(name, **args)


async def _resolve_sandbox(cfg: Dict[str, Any], ttype: str) -> Dict[str, Any]:
    """Decide whether this task runs in the dev sandbox. sim tasks use the
    business.sim's own is_sim=1 isolation, so they never route to the dev
    sandbox. Returns {use, blocked, reason}."""
    mode = cfg.get("sandbox_mode", "prefer")
    if ttype == "sim" or mode == "off":
        return {"use": False, "blocked": False, "reason": ""}
    exists = bool(await _get_sandbox())
    probe = (await _sandbox_probe()) if exists else {"reachable": False}
    up = exists and probe.get("reachable")
    # A stale-image sandbox (up but missing the workhorse cap) is a distinct,
    # actionable state — surface the rebuild hint instead of a generic "not up".
    stale = exists and probe.get("stale")
    down_reason = (probe.get("error") if stale else None) or \
        ("no dev sandbox is up — bring one up (evolve.sandbox.ensure) first"
         if not exists else (probe.get("error") or "dev sandbox is not reachable"))
    if mode == "require":
        if not up:
            return {"use": False, "blocked": True,
                    "reason": f"sandbox_mode=require but the sandbox is unusable: {down_reason}",
                    "stale": bool(stale)}
        await _sandbox_touch()
        return {"use": True, "blocked": False, "reason": "require"}
    # prefer — never block; fall back to in-process, but say WHY when it's stale.
    if up:
        await _sandbox_touch()
    return {"use": bool(up), "blocked": False, "stale": bool(stale),
            "reason": "sandbox" if up else (f"in-process ({down_reason})")}


async def _run_sim_task(task: Dict[str, Any], variant: Optional[Dict[str, Any]],
                        run_id: str, timeout: int) -> Dict[str, Any]:
    """Run an agent loop against the BUSINESS SIMULATION and score it from the
    simulated ledger — mechanical ground truth, no LLM critic needed. Flow:
    business.sim.start → business.sim.evaluate (baseline + agent_request) →
    loops.run (agent operates the sim) → business.sim.score."""
    scenario = task.get("scenario", "reseller")
    st = await _call("business.sim.start", scenario=scenario,
                     seed=int(task.get("seed", 0) or 0))
    if isinstance(st, dict) and st.get("error"):
        return {"error": f"sim start: {st['error']}"}
    ev = await _call("business.sim.evaluate", goal=task.get("goal", ""),
                     rubric=task.get("rubric", ""),
                     agent_name=task.get("agent_name", "business-operator"))
    if not isinstance(ev, dict) or ev.get("error"):
        return {"error": f"sim evaluate: {(ev or {}).get('error')}"}
    eval_id = (ev.get("eval") or {}).get("id", "")
    req = ev.get("agent_request") or {}
    kw = dict(task.get("overrides") or {})
    kw.update(_variant_call_overrides(variant))
    loop_res = await asyncio.wait_for(
        _call("loops.run",
              profile=task.get("profile", "operator"),
              goal=req.get("goal") or task.get("goal", ""),
              allowed_caps=task.get("allowed_caps", ""),
              session_id=f"evolve:{run_id}",
              max_steps=int(task.get("max_steps", 8) or 8),
              **kw),
        timeout=timeout)
    steps = _norm_steps(loop_res if isinstance(loop_res, dict) else {})
    transcript = _norm_final(loop_res if isinstance(loop_res, dict) else {})
    sc = await _call("business.sim.score", eval_id=eval_id, transcript=transcript)
    sim_score = None
    if isinstance(sc, dict) and not sc.get("error"):
        sim_score = float(sc.get("score", 0))   # 0..100
    return {"final": transcript, "steps": steps,
            "sim_score": sim_score, "sim_components": (sc or {}).get("components"),
            "sim_delta": (sc or {}).get("delta"), "eval_id": eval_id}


# In-flight run state so the suite/session can show a live per-task heartbeat.
_RUN_LIVE: Dict[str, Any] = {}


async def _run_task(task: Dict[str, Any], variant: Optional[Dict[str, Any]] = None,
                    source: str = "manual", session_id: str = "",
                    run_id: str = "") -> Dict[str, Any]:
    """Execute one task and return the full run detail (also persisted)."""
    run_id = run_id or uuid.uuid4().hex[:10]
    t0 = time.time()
    ttype = task.get("type", "loop")
    # cap tasks are quick; loop/sim tasks pay for an agent loop — give them
    # a sane default ceiling but let the task override.
    default_to = 90 if ttype == "cap" else 240
    timeout = max(20, int(task.get("timeout_s", default_to) or default_to))
    error, final, steps, raw_keys = "", "", [], []
    sim_score = None
    extra: Dict[str, Any] = {}

    # ── Sandbox-first: decide where this runs (loop/cap in the dev sandbox when
    # active; sim uses the business-sim's own isolation). Denylist strips
    # external-effect caps from test loops as defence in depth.
    cfg = task.get("_cfg") or await _get_config()
    sb = await _resolve_sandbox(cfg, ttype)
    task = dict(task)
    task["_sandbox"] = sb["use"]
    task["_denylist"] = cfg.get("test_denylist") or []
    # Interactive tests (run.start) AND manual one-at-a-time task runs
    # (evolve.task.run) are ACTIVITY-watched, not clock-killed — the loop
    # under test may take an indefinite amount of time, and a run that's
    # genuinely still producing events should never be judged "timed out"
    # just because it crossed some fixed clock threshold (found live,
    # 2026-08-03: chain-preserve-existing-file was killed at its 480s
    # timeout_s while apparently still working). Suites and improve-session
    # variant tests stay bounded by their per-task timeout_s on purpose — a
    # suite runs many tasks back to back, and one hung task must not stall
    # every task queued behind it; that tradeoff doesn't apply to a single
    # manually-triggered run with nothing else waiting.
    if source in ("run", "manual") and ttype == "loop":
        task["_indefinite"] = True
        task["_idle_s"] = cfg.get("run_idle_timeout_s", 300)
        task["_max_s"] = cfg.get("run_max_s", 7200)
    where = "sandbox" if sb["use"] else "in-process"

    _RUN_LIVE.update({"run_id": run_id, "task": task.get("id"),
                      "type": ttype, "started_at": now_iso(), "t0": t0,
                      "where": where, "triggered_by": _triggered_by(),
                      "profile": task.get("profile", ""), "target": task.get("target", ""),
                      "goal": task.get("goal", "") or task.get("cap", "")})

    log.info("evolve run %s: task=%s type=%s where=%s (timeout %ss)",
             run_id, task.get("id"), ttype, where, timeout)
    await emit_event({"type": "evolve.run.started", "run_id": run_id,
                      "task": task.get("id"), "task_type": ttype, "where": where,
                      "source": source, "variant": (variant or {}).get("id", "")})
    # Drive the dynamic-workflow diagram for EVERY run (single or suite), not
    # just improve sessions: the implementer node runs while the loop executes.
    await emit_event({"type": "evolve.workflow", "run_id": run_id,
                      "node": "implementer", "state": "running", "where": where})

    if sb["blocked"]:
        error = sb["reason"]

    try:
        if sb["blocked"]:
            pass
        elif ttype == "cap":
            res = await asyncio.wait_for(
                _exec_cap(task.get("cap", ""), dict(task.get("args") or {}),
                          sb["use"]),
                timeout=timeout)
            if isinstance(res, dict):
                error = str(res.get("error") or "")
                final = _norm_final(res) or json.dumps(res, default=str)[:4000]
                raw_keys = list(res.keys())[:20]
            else:
                final = str(res)[:4000]
        elif ttype == "sim":
            res = await _run_sim_task(task, variant, run_id, timeout)
            error = str(res.get("error") or "")
            final = _norm_final(res) or res.get("final", "")
            steps = res.get("steps") or []
            sim_score = res.get("sim_score")
            extra = {k: res.get(k) for k in ("sim_score", "sim_components",
                                             "sim_delta", "eval_id")}
        else:
            res = await _run_loop_task(task, variant, run_id, timeout)
            error = str(res.get("error") or "")
            final = _norm_final(res)
            steps = _norm_steps(res)
            raw_keys = list(res.keys())[:20]
    except asyncio.TimeoutError:
        error = f"timeout after {timeout}s"
    except Exception as e:
        error = str(e)
    finally:
        _RUN_LIVE.clear()

    # The engines' results rarely include a full steps list — rebuild the tool
    # trace from the persisted loop events so the reviewer (and Runs detail)
    # sees EVERY tool call, not just the last card. Also salvages the trace of
    # a timed-out run (partial progress is still in the event log).
    # Fire not only when the result carried too FEW steps, but also when the
    # steps it did carry name NO capability: engines emit phase/cycle markers
    # with an empty `cap`, so a run that genuinely called its tool would still
    # false-fail every cap_called check (observed: tool-echo returned the real
    # echo payload yet scored `called: []` because it came back as 2 empty
    # markers, so `len(steps) < 2` never tripped). Prefer whichever source
    # names more real caps, with step count as the tie-breaker.
    if ttype in ("loop", "sim"):
        def _n_caps(_sts):
            return sum(1 for _s in _sts if str(_s.get("cap") or "").strip())
        if len(steps) < 2 or _n_caps(steps) == 0:
            ev_steps = await _steps_from_events(run_id)
            if _n_caps(ev_steps) > _n_caps(steps) or len(ev_steps) > len(steps):
                steps = ev_steps

    elapsed = round(time.time() - t0, 1)
    checks = _run_checks(task, final, steps, elapsed, error=error)
    n_ok = sum(1 for c in checks if c["ok"])
    pass_rate = round(n_ok / len(checks), 3) if checks else (0.0 if error else 1.0)
    # A sim task's combined score IS the ground-truth sim score (0-100 → 0-10),
    # blended lightly with any checks so the number reflects real outcome.
    if sim_score is not None:
        combined = round(sim_score / 10.0, 1)
        if checks:
            combined = round(0.7 * (sim_score / 10.0) + 0.3 * (pass_rate * 10), 1)
    else:
        combined = round(pass_rate * 10, 1)

    compact = {
        "run_id": run_id, "task": task.get("id"), "label": task.get("label", ""),
        "task_type": ttype, "profile": task.get("profile", ""),
        "ts": now_iso(), "elapsed_s": elapsed,
        "pass_rate": pass_rate, "checks_ok": n_ok, "checks_n": len(checks),
        "score": (round(sim_score / 10.0, 1) if sim_score is not None else None),
        "combined": combined,
        "variant": (variant or {}).get("id", ""), "source": source,
        "session": session_id, "error": error[:200], "where": where,
        "triggered_by": _triggered_by(),
    }
    detail = dict(compact)
    detail.update({
        "goal": task.get("goal", "") or task.get("cap", ""),
        "final": final[:12000], "steps": steps, "checks": checks,
        "raw_keys": raw_keys, "assessment": None,
        "blocked": bool(sb["blocked"]),   # environment not ready, not a test failure
        **extra,
    })
    await _push_run(compact, detail)
    await emit_event({"type": "evolve.workflow", "run_id": run_id,
                      "node": "implementer",
                      "state": ("error" if error else "done"),
                      "steps": len(steps)})
    await emit_event({"type": "evolve.run.done", "run_id": run_id,
                      "task": task.get("id"), "pass_rate": pass_rate,
                      "combined": combined, "elapsed_s": elapsed,
                      "where": where, "error": error[:120]})
    return detail


@capability("evolve.task.run", memory="off",
            http_method="POST", http_path="/evolve/task/run", http_tags=["evolve"],
            description="Run one benchmark task now. Inputs: id (str!), assess "
                        "(bool — also have the critic score it), provider (str — "
                        "critic override, e.g. 'ollama' or 'anthropic'). "
                        "Output: the full run record {run_id, checks, steps, final, "
                        "pass_rate, assessment?}.")
async def evolve_task_run(id: str = "", assess: bool = False, provider: str = "",
                          trace_id=None):
    task = await _get_task(id)
    if not task:
        return {"error": f"unknown task: {id}"}
    detail = await _run_task(task, source="manual")
    if assess:
        cfg = await _get_config()
        a = await _assess_run(detail, task, provider or cfg["critic_provider"])
        detail["assessment"] = a
        detail["score"] = a.get("score")
        detail["combined"] = a.get("combined", detail["combined"])
    return detail


async def _run_window_commits(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Commits landed during this run's own time window — the same
    correlation primitive ide.claude_sessions.list_sessions uses on the
    Dispatch side (ide_capabilities.ide_git_log's since/until support), so
    a Loop Lab run and the chat session that triggered it share a join key
    (the commit hash) without either subsystem needing to know about the
    other directly. Best-effort: a run with no `ts`/`elapsed_s`, or a repo
    with nothing committed in the window, just gets an empty list.

    Single-run helper — for a LIST of runs use _runs_window_commits_batch
    below instead of calling this once per run (that N-calls-per-request
    pattern, one real git subprocess spawn each, is what flooded Vera and
    took it offline — see evolve_runs)."""
    ts = rec.get("ts") or ""
    elapsed_s = rec.get("elapsed_s")
    if not ts or elapsed_s is None:
        return []
    try:
        from datetime import datetime, timedelta
        until_dt = datetime.fromisoformat(ts.rstrip("Z"))
        since_dt = until_dt - timedelta(seconds=float(elapsed_s) + 1)
        since = since_dt.isoformat() + "Z"
        until = until_dt.isoformat() + "Z"
        ide_caps = sys.modules.get("ide_capabilities")
        if not ide_caps:
            return []
        res = await ide_caps.ide_git_log(path=str(_REPO_ROOT), since=since, until=until)
        return res.get("commits", [])
    except Exception as e:
        log.debug("evolve: commit correlation failed for run %s: %s", rec.get("run_id"), e)
        return []


async def _runs_window_commits_batch(recs: List[Dict[str, Any]]) -> None:
    """Same correlation as _run_window_commits, but for a whole run LIST:
    one ide_git_log call covering the union of every run's window, then
    bucketed back out per run in Python — not one subprocess spawn per run.
    Mutates each rec in place, setting rec["commits"]."""
    from datetime import datetime, timedelta
    windows: List[Any] = []   # (rec, since_dt, until_dt)
    for rec in recs:
        ts = rec.get("ts") or ""
        elapsed_s = rec.get("elapsed_s")
        if not ts or elapsed_s is None:
            rec["commits"] = []
            continue
        try:
            until_dt = datetime.fromisoformat(ts.rstrip("Z"))
            since_dt = until_dt - timedelta(seconds=float(elapsed_s) + 1)
            windows.append((rec, since_dt, until_dt))
        except Exception:
            rec["commits"] = []
    if not windows:
        return
    ide_caps = sys.modules.get("ide_capabilities")
    all_commits: List[Dict[str, Any]] = []
    if ide_caps:
        try:
            since = min(w[1] for w in windows).isoformat() + "Z"
            until = max(w[2] for w in windows).isoformat() + "Z"
            res = await ide_caps.ide_git_log(path=str(_REPO_ROOT), since=since, until=until)
            all_commits = res.get("commits", [])
        except Exception as e:
            log.debug("evolve: batched commit correlation failed: %s", e)
    for rec, since_dt, until_dt in windows:
        lo, hi = since_dt.timestamp(), until_dt.timestamp()
        rec["commits"] = [c for c in all_commits if lo <= c.get("ts", 0) <= hi]


@capability("evolve.authors", memory="off", silent=True,
            http_method="GET", http_path="/evolve/authors", http_tags=["evolve"],
            description="Real authorship map: recent commits to this repo, each "
                        "tagged with WHO/WHAT actually produced it — a Claude Code "
                        "session (via ide.claude_sessions.list_sessions' own git-"
                        "log time-window correlation), a Vera evolve.ide.improve "
                        "run (tagged with its real engine: claude|vera-agent), or "
                        "left 'direct' when no known automated session/run "
                        "overlapped that commit's timestamp — never guessed. "
                        "Query: hours (int, default 72), branch (str, optional — "
                        "log this branch instead of the checked-out HEAD).")
async def evolve_authors(hours: int = 72, branch: str = "", trace_id=None):
    from datetime import datetime, timedelta, timezone
    since_dt = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))
    since = since_dt.isoformat()
    ide_caps = sys.modules.get("ide_capabilities")
    commits: List[Dict[str, Any]] = []
    if branch:
        r = await _sh(["git", "log", branch,
                       "--pretty=format:%H\x1f%an\x1f%ad\x1f%ct\x1f%s",
                       "--date=short", f"--since={since}"], cwd=str(_repo_root()))
        for line in (r.get("out") or "").splitlines():
            parts = line.split("\x1f")
            if len(parts) >= 5:
                commits.append({"hash": parts[0], "author": parts[1], "date": parts[2],
                                "ts": int(parts[3]), "message": parts[4]})
    elif ide_caps:
        try:
            res = await ide_caps.ide_git_log(path=str(_REPO_ROOT), since=since)
            commits = res.get("commits", [])
        except Exception as e:
            log.debug("evolve.authors: git log failed: %s", e)

    claude_sess_caps = sys.modules.get("ide_claude_sessions_capabilities")
    claude_by_hash: Dict[str, Dict[str, Any]] = {}
    if claude_sess_caps:
        try:
            sess_res = await claude_sess_caps.cap_claude_sessions_list_sessions(scan_limit=3000)
            for s in sess_res.get("sessions", []):
                for c in (s.get("commits") or []):
                    claude_by_hash[c["hash"]] = {
                        "session_id": s.get("claude_session_id", ""),
                        "project_dir": s.get("project_dir", ""),
                    }
        except Exception as e:
            log.debug("evolve.authors: claude session correlation failed: %s", e)

    engine_runs = [r for r in (await evolve_runs(limit=300)).get("runs", []) if r.get("engine")]
    engine_by_hash: Dict[str, Dict[str, Any]] = {}
    for r in engine_runs:
        for c in (r.get("commits") or []):
            engine_by_hash[c["hash"]] = {"engine": r.get("engine"), "run_id": r.get("run_id"),
                                         "task": r.get("task", "")}

    out_commits = []
    for c in commits:
        h = c.get("hash", "")
        cs = claude_by_hash.get(h)
        er = engine_by_hash.get(h)
        if cs:
            out_commits.append({**c, "agent": "claude", "agent_label": "Claude Code",
                                "session_id": cs["session_id"], "project_dir": cs["project_dir"]})
        elif er:
            eng = er.get("engine") or "vera-agent"
            out_commits.append({**c, "agent": eng,
                                "agent_label": "Claude Code" if eng == "claude" else "Vera agent",
                                "run_id": er.get("run_id"), "task": er.get("task")})
        else:
            out_commits.append({**c, "agent": "direct", "agent_label": "direct"})
    return {"commits": out_commits, "count": len(out_commits), "hours": hours, "branch": branch}


@capability("evolve.runs", memory="off", silent=True,
            http_method="GET", http_path="/evolve/runs", http_tags=["evolve"],
            description="Recent benchmark runs (compact, newest first), each "
                        "correlated against this repo's git log for its own "
                        "run window (commits: [{hash, author, date, ts, "
                        "message}]) — the same join key Dispatch's chat-session "
                        "list uses, for connecting a run back to whatever "
                        "session (Claude Code or otherwise) produced it. "
                        "Query: limit, task, session.")
async def evolve_runs(limit: int = 50, task: str = "", session: str = "",
                      trace_id=None):
    r = _redis()
    out: List[Dict[str, Any]] = []
    if r:
        try:
            rows = await r.lrange(KEY_RUNS, 0, RUNS_CAP - 1)
            for row in rows or []:
                try:
                    rec = json.loads(row.decode() if isinstance(row, bytes) else row)
                except Exception:
                    continue
                if task and rec.get("task") != task:
                    continue
                if session and rec.get("session") != session:
                    continue
                out.append(rec)
                if len(out) >= int(limit):
                    break
        except Exception:
            pass
    await _runs_window_commits_batch(out)
    return {"runs": out, "count": len(out)}


@capability("evolve.compare", memory="off", silent=True,
            http_method="GET", http_path="/evolve/compare", http_tags=["evolve"],
            description="Aggregate evolve.runs history into a per-task comparison "
                        "between two groups (variant ids — empty string means the "
                        "un-varianted baseline): run count, avg pass_rate, avg "
                        "combined score, and the A-vs-B delta per task. Query: "
                        "group_a (str!), group_b (str!), task (str — restrict to "
                        "one task id), limit (int, runs scanned, default 500).")
async def evolve_compare(group_a: str = "", group_b: str = "", task: str = "",
                         limit: int = 500, trace_id=None):
    r = _redis()
    runs: List[Dict[str, Any]] = []
    if r:
        try:
            rows = await r.lrange(KEY_RUNS, 0, max(int(limit), 1) - 1)
            for row in rows or []:
                try:
                    rec = json.loads(row.decode() if isinstance(row, bytes) else row)
                except Exception:
                    continue
                if task and rec.get("task") != task:
                    continue
                runs.append(rec)
        except Exception:
            pass

    def _bucket(group: str) -> Dict[str, List[Dict[str, Any]]]:
        by_task: Dict[str, List[Dict[str, Any]]] = {}
        for rec in runs:
            if (rec.get("variant") or "") != group:
                continue
            by_task.setdefault(rec.get("task", ""), []).append(rec)
        return by_task

    a_by_task = _bucket(group_a)
    b_by_task = _bucket(group_b)
    task_ids = sorted(set(a_by_task) | set(b_by_task))
    tasks_meta = {t.get("id"): t for t in await _get_tasks()}

    def _stats(recs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not recs:
            return None
        combined = [x["combined"] for x in recs if x.get("combined") is not None]
        return {
            "n": len(recs),
            "avg_pass_rate": round(mean(x.get("pass_rate", 0.0) for x in recs), 3),
            "avg_combined": round(mean(combined), 2) if combined else None,
        }

    rows = []
    for tid in task_ids:
        a_stats = _stats(a_by_task.get(tid, []))
        b_stats = _stats(b_by_task.get(tid, []))
        delta = None
        if a_stats and b_stats and a_stats["avg_combined"] is not None and b_stats["avg_combined"] is not None:
            delta = round(a_stats["avg_combined"] - b_stats["avg_combined"], 2)
        rows.append({
            "task": tid, "label": (tasks_meta.get(tid) or {}).get("label", tid),
            "a": a_stats, "b": b_stats, "delta": delta,
        })
    return {"rows": rows, "group_a": group_a, "group_b": group_b, "runs_scanned": len(runs)}


@capability("evolve.run.get", memory="off", silent=True,
            http_method="GET", http_path="/evolve/run/get", http_tags=["evolve"],
            description="Full detail of one benchmark run (steps, final output, "
                        "checks, assessment). Input: run_id (str!).")
async def evolve_run_get(run_id: str = "", trace_id=None):
    r = _redis()
    if not r or not run_id:
        return {"error": "run_id required"}
    raw = await r.get(KEY_RUN + run_id)
    if not raw:
        return {"error": "run not found (expired?)"}
    return {"run": json.loads(raw.decode() if isinstance(raw, bytes) else raw)}


# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND SINGLE RUN — the fix for the blocking "failed" popup
# ─────────────────────────────────────────────────────────────────────────────
# A single test used to be a synchronous POST that held the request open for the
# whole loop + critic pass — on the CPU cluster that outlives the proxy timeout,
# so the UI saw a bare "failed" and nothing live. evolve.run.start launches the
# run in the background and returns a run_id immediately; the panel streams it
# via evolve:<run_id> agent events + polls evolve.run.status.

async def _build_run_task(kind: str, *, target: str = "", goal: str = "",
                          allowed_caps: str = "", cap: str = "", args: Any = None,
                          task_id: str = "", checks: Any = None,
                          max_steps: int = 6) -> Optional[Dict[str, Any]]:
    if kind == "task":
        t = await _get_task(task_id)
        return dict(t) if t else None
    tgt = await _resolve_target(target)
    if kind == "cap":
        return {"id": f"run-{uuid.uuid4().hex[:6]}", "type": "cap", "cap": cap,
                "args": args if isinstance(args, dict) else {},
                "label": f"cap: {cap}", "target": tgt["id"],
                "checks": checks if isinstance(checks, list) else [{"type": "no_error"}],
                "timeout_s": 120}
    # An agent: target with no loop profile bound to it (loop_profiles.py has
    # no preset for this persona) has nothing to run as a multi-step loop —
    # rather than silently mislabeling it as the generic planning profile
    # (the bug this whole resolution path used to have), exercise it the
    # honest way: one bounded agent.consult call against the real persona.
    if tgt.get("category") == "agent" and tgt.get("no_bound_profile"):
        return {"id": f"run-{uuid.uuid4().hex[:6]}", "type": "cap",
                "cap": "agent.consult",
                "args": {"agent_name": tgt.get("agent", ""), "message": goal},
                "label": f"consult: {tgt.get('agent', '')}"[:60], "target": tgt["id"],
                "checks": checks if isinstance(checks, list) else [{"type": "no_error"}],
                "timeout_s": 120}
    # loop / goal
    return {"id": f"run-{uuid.uuid4().hex[:6]}", "type": "loop",
            "profile": tgt["profile"] or "planning", "target": tgt["id"],
            "label": (goal or tgt["label"])[:60], "goal": goal,
            "allowed_caps": allowed_caps,
            "checks": checks if isinstance(checks, list) else [{"type": "final_nonempty"}],
            "rubric": "Did the loop accomplish the goal with grounded tool use, no "
                      "hallucination, economically?",
            "max_steps": int(max_steps or 6), "timeout_s": 300}


async def _bg_run(task: Dict[str, Any], run_id: str, assess: bool, provider: str):
    try:
        detail = await _run_task(task, source="run", run_id=run_id)
        # Track TEST failures in the errors work-queue (linked by run_id) so a
        # broken component flows: test → error item → suggest → approve →
        # gated remediation pipeline. Dedup makes repeats bump a counter.
        # `blocked` (e.g. sandbox_mode=require with no sandbox up) is an
        # ENVIRONMENT problem, not a defect in the thing under test — guarded
        # out of both branches below, never filed as one.
        try:
            meta = {"run_id": run_id, "task": task.get("id", ""),
                    "profile": task.get("profile", ""),
                    "component": f"loop-lab:{task.get('profile') or task.get('cap') or 'test'}",
                    "severity": "warn"}
            # The dedup TITLE must be STABLE across repeated runs of the same
            # test — task["id"] for an ad-hoc loop/cap run (composer) is a
            # fresh f"run-{uuid4().hex[:6]}" every single invocation, so using
            # it here defeated dedup entirely (every retry looked like a brand
            # new error instead of bumping one item's counter). Saved tasks DO
            # have a stable id; ad-hoc runs are identified by profile/cap+goal.
            stable = (task.get("profile") or task.get("cap")
                     or (task.get("id", "") if not str(task.get("id", "")).startswith("run-") else "")
                     or "loop-lab-test")
            descr = stable + (f": {task['goal'][:80]}" if task.get("goal")
                              else f": {task['label'][:80]}" if task.get("label") else "")
            if detail.get("error") and not detail.get("blocked"):
                await _err_ingest_one(
                    "test", f"test [{descr}] {detail['error'][:150]}",
                    detail=(f"goal: {task.get('goal', '') or task.get('cap', '')}\n"
                            f"profile: {task.get('profile', '')}\n"
                            f"error: {detail['error']}\n"
                            f"final: {(detail.get('final') or '')[:800]}"),
                    meta=meta)
            elif not detail.get("error"):
                fails = [s for s in (detail.get("steps") or []) if not s.get("ok")]
                if len(fails) >= 3:
                    await _err_ingest_one(
                        "test", (f"test [{descr}] {len(fails)} failed "
                                 f"tool calls ({fails[0].get('cap', '?')} …)"),
                        detail=json.dumps(fails[:8], default=str)[:2000], meta=meta)
        except Exception as e:
            log.debug("evolve test-error ingest: %s", e)
        assessment = None
        if assess:
            cfg = await _get_config()
            assessment = await _review_run(detail, task,
                                           provider or cfg["critic_provider"],
                                           session="", rnd=0)
        # Signal completion AFTER review so the panel populates the critique /
        # adversarial failures / result from a fully-assessed run.
        await emit_event({"type": "evolve.workflow", "run_id": run_id,
                          "node": "output", "state": "done",
                          "score": (assessment or {}).get("combined",
                                                          detail.get("combined"))})
        await emit_event({"type": "evolve.run.reviewed", "run_id": run_id,
                          "combined": (assessment or {}).get("combined",
                                                            detail.get("combined")),
                          "score": (assessment or {}).get("score"),
                          "failures": len((assessment or {}).get("failures") or [])})
    except Exception as e:
        log.warning("evolve bg run %s: %s", run_id, e)
    finally:
        _BG_RUNS.pop(run_id, None)


_BG_RUNS: Dict[str, asyncio.Task] = {}


@capability("evolve.run.start", memory="on",
            http_method="POST", http_path="/evolve/run/start", http_tags=["evolve"],
            description="Run ONE test in the BACKGROUND and return immediately — the "
                        "UI streams it live (evolve:<run_id> agent events) and polls "
                        "evolve.run.status, so a slow run never times out. Inputs: "
                        "kind (loop|cap|task|goal), target (str — category:id, e.g. "
                        "'specialist:coding' | 'agent:coder'), goal, allowed_caps, "
                        "cap, args, task_id, checks (list), assess (bool), provider "
                        "(critic). Output: {ok, run_id}.",
            schema=enum_schema(kind=["loop", "goal", "cap", "task"]))
async def evolve_run_start(kind: str = "loop", target: str = "", goal: str = "",
                           allowed_caps: str = "", cap: str = "",
                           args: Optional[Dict[str, Any]] = None, task_id: str = "",
                           checks: Optional[List[Dict[str, Any]]] = None,
                           assess: bool = True, provider: str = "", max_steps: int = 6,
                           trace_id=None):
    task = await _build_run_task(kind, target=target, goal=goal,
                                 allowed_caps=allowed_caps, cap=cap, args=args,
                                 task_id=task_id, checks=checks, max_steps=max_steps)
    if not task:
        return {"error": "could not build run (unknown task_id / missing goal or cap)"}
    if kind in ("loop", "goal") and not (goal or "").strip():
        return {"error": "goal required for a loop run"}
    if kind == "cap" and not cap:
        return {"error": "cap required for a cap run"}
    run_id = uuid.uuid4().hex[:10]
    _BG_RUNS[run_id] = asyncio.create_task(
        _bg_run(task, run_id, bool(assess) and task.get("type") != "cap", provider))
    return {"ok": True, "run_id": run_id, "task": task.get("id"),
            "type": task.get("type"), "target": task.get("target")}


@capability("evolve.run.status", memory="off", silent=True,
            http_method="GET", http_path="/evolve/run/status", http_tags=["evolve"],
            description="Live snapshot of the most-recent/named background run "
                        "(current stage, tool, elapsed, where) for poll fallback "
                        "when the event stream drops. Query: run_id (str). Output: "
                        "{live, run}.")
async def evolve_run_status(run_id: str = "", trace_id=None):
    live = dict(_RUN_LIVE)
    if run_id and live.get("run_id") != run_id:
        # not the in-flight one — return the persisted record
        got = await evolve_run_get(run_id=run_id)
        return {"live": False, "run": got.get("run"), "current": {}}
    return {"live": bool(live.get("run_id")), "current": live,
            "running": run_id in _BG_RUNS or bool(live.get("run_id"))}


# ─────────────────────────────────────────────────────────────────────────────
# ASSESS — critic LLM scores a run and proposes edits
# ─────────────────────────────────────────────────────────────────────────────

_ASSESS_SYSTEM = (
    "You are a rigorous QA critic for an autonomous agent system. You are given "
    "one benchmark task, the agent's tool-call trace and final output, and the "
    "programmatic check results. Judge REAL behaviour only — reward grounded "
    "tool use, correctness, and economy of steps; punish hallucination, ignored "
    "tools, wasted cycles and format violations. Reply with ONLY a JSON object:\n"
    '{"score": <0-10 number>, "passed": <bool>, "critique": "<2-6 sentences>", '
    '"failures": ["<specific defect>", ...], '
    '"edits": {"overrides": {<knob>: <value>}, "prompt_preamble": "<extra system '
    'guidance or empty>", "code_suggestions": [{"area": "<module/behaviour>", '
    '"suggestion": "<concrete source-level change>"}]}}\n'
    "Suggest overrides ONLY from the knob list you are given. No prose outside "
    "the JSON."
)


def _assess_prompt(detail: Dict[str, Any], task: Dict[str, Any],
                   variant: Optional[Dict[str, Any]] = None) -> str:
    steps_txt = "\n".join(
        f"  {i+1}. [{'ok' if s.get('ok') else 'ERR'}] {s.get('cap','?')} — "
        f"{(s.get('thought') or '')[:120]} → {(s.get('preview') or '')[:180]}"
        for i, s in enumerate(detail.get("steps") or [])) or "  (no tool calls)"
    checks_txt = "\n".join(
        f"  - {c['type']}({str(c.get('value',''))[:60]}): "
        f"{'PASS' if c['ok'] else 'FAIL'}{(' — ' + c['note']) if c.get('note') else ''}"
        for c in detail.get("checks") or []) or "  (none)"
    parts = [
        f"TASK: {task.get('label', task.get('id'))}",
        f"GOAL:\n{task.get('goal', '') or task.get('cap', '')}",
        f"RUBRIC: {task.get('rubric', '') or '(none — judge on checks + general quality)'}",
        f"PROGRAMMATIC CHECKS ({detail.get('checks_ok')}/{detail.get('checks_n')} passed):\n{checks_txt}",
        f"TOOL TRACE ({len(detail.get('steps') or [])} steps, "
        f"{detail.get('elapsed_s')}s{', ERROR: ' + detail['error'] if detail.get('error') else ''}):\n{steps_txt}",
        f"FINAL OUTPUT:\n{(detail.get('final') or '(empty)')[:4000]}",
    ]
    if variant:
        parts.append("CURRENT TUNING VARIANT:\n"
                     + json.dumps({"overrides": variant.get("overrides", {}),
                                   "prompt_preamble": variant.get("prompt_preamble", "")},
                                  indent=1)[:1500])
    parts.append(_KNOB_GUIDE)
    return "\n\n".join(parts)


async def _assess_run(detail: Dict[str, Any], task: Dict[str, Any],
                      provider: str,
                      variant: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    res = await _provider_chat(provider, _assess_prompt(detail, task, variant),
                               system=_ASSESS_SYSTEM)
    if res.get("error"):
        return {"error": res["error"], "provider": provider}
    parsed = _extract_json(res.get("text", "")) or {}
    try:
        score = max(0.0, min(10.0, float(parsed.get("score", 0))))
    except Exception:
        score = 0.0
    edits = parsed.get("edits") or {}
    assessment = {
        "score": round(score, 1),
        "passed": bool(parsed.get("passed", score >= 7)),
        "critique": str(parsed.get("critique", ""))[:2000],
        "failures": [str(f)[:300] for f in (parsed.get("failures") or [])][:10],
        "edits": {
            "overrides": _clamp_overrides(edits.get("overrides")),
            "prompt_preamble": str(edits.get("prompt_preamble") or "")[:1500],
            "code_suggestions": [
                {"area": str((c or {}).get("area", ""))[:120],
                 "suggestion": str((c or {}).get("suggestion", ""))[:800]}
                for c in (edits.get("code_suggestions") or [])[:6]
                if isinstance(c, dict)],
        },
        "provider": res.get("provider"), "model": res.get("model", ""),
        "cost_usd": res.get("cost_usd", 0.0), "ts": now_iso(),
    }
    # combined = ground truth (checks) 50% + critic 50%
    combined = round((detail.get("pass_rate", 0) * 10) * 0.5 + score * 0.5, 1)
    assessment["combined"] = combined
    await _update_run(detail["run_id"], {
        "assessment": assessment, "score": assessment["score"],
        "combined": combined, "assessed_by": provider,
        "passed": assessment["passed"],
    })
    await emit_event({"type": "evolve.assessed", "run_id": detail["run_id"],
                      "task": task.get("id"), "score": assessment["score"],
                      "combined": combined, "provider": provider})
    return assessment


# ─────────────────────────────────────────────────────────────────────────────
# ADVERSARIAL REVIEW — implementer → N reviewers → fixer (Bun-article Loop 1)
# ─────────────────────────────────────────────────────────────────────────────

_ADVERSARIAL_SYSTEM = (
    "You are an ADVERSARIAL reviewer. You are shown ONLY a task goal, an agent's "
    "tool-call trace and its final output — NOT any rubric or grading hints. "
    "Assume the output is WRONG. Your only job is to exhaustively enumerate "
    "concrete, specific reasons the agent FAILED or could be wrong: hallucinated "
    "facts, tools it should have called but didn't, unverified claims, wasted "
    "steps, format/spec violations, edge cases missed. Reply with ONLY JSON: "
    '{"score": <0-10, how well it actually did>, "failures": ["<specific defect>", '
    '...]}. Be harsh and specific. No prose outside the JSON.')


def _adv_prompt(detail: Dict[str, Any], task: Dict[str, Any]) -> str:
    steps = "\n".join(
        f"  {i+1}. [{'ok' if s.get('ok') else 'ERR'}] {s.get('cap','?')} → "
        f"{(s.get('preview') or '')[:180]}"
        for i, s in enumerate(detail.get("steps") or [])) or "  (no tool calls)"
    return (f"GOAL:\n{task.get('goal','') or task.get('cap','')}\n\n"
            f"TOOL TRACE ({len(detail.get('steps') or [])} steps"
            f"{', ERROR: ' + detail['error'] if detail.get('error') else ''}):\n{steps}\n\n"
            f"FINAL OUTPUT:\n{(detail.get('final') or '(empty)')[:3500]}")


async def _adversarial_pass(detail: Dict[str, Any], task: Dict[str, Any],
                            provider: str) -> Dict[str, Any]:
    res = await _provider_chat(provider, _adv_prompt(detail, task),
                               system=_ADVERSARIAL_SYSTEM, max_tokens=1200)
    if res.get("error"):
        return {"error": res["error"], "failures": [], "score": None}
    p = _extract_json(res.get("text", "")) or {}
    try:
        score = max(0.0, min(10.0, float(p.get("score", 0))))
    except Exception:
        score = None
    return {"score": score,
            "failures": [str(f)[:300] for f in (p.get("failures") or [])][:10],
            "provider": res.get("provider", provider)}


async def _review_run(detail: Dict[str, Any], task: Dict[str, Any], provider: str,
                      session: str = "", rnd: int = 0) -> Dict[str, Any]:
    """The standard evaluation path: one scoring critic (stores + emits), then —
    when adversarial is on — N extra adversarial reviewers whose unioned failures
    harden the assessment. Emits evolve.workflow events so the live diagram
    animates implementer → reviewers → fixer.

    NB: the round-number param is `rnd`, NOT `round` — a `round` parameter
    shadows the builtin round() used just below to aggregate scores, so with
    adversarial reviewers on (the default) every assessment died with
    "'int' object is not callable"."""
    rid = detail.get("run_id", "")
    cfg = await _get_config()
    n = int(cfg.get("reviewers", 2)) if cfg.get("adversarial", True) else 1

    await emit_event({"type": "evolve.workflow", "session": session, "run_id": rid,
                      "node": "reviewers", "state": "running", "round": rnd,
                      "count": n})
    # primary scoring critic (persists the assessment + emits evolve.assessed)
    assessment = await _assess_run(detail, task, provider)
    if assessment.get("error"):
        await emit_event({"type": "evolve.workflow", "session": session, "run_id": rid,
                          "node": "reviewers", "state": "error", "round": rnd})
        return assessment

    if n > 1:
        adv_failures: List[str] = list(assessment.get("failures") or [])
        adv_scores: List[float] = []
        if isinstance(assessment.get("score"), (int, float)):
            adv_scores.append(float(assessment["score"]))
        for i in range(1, n):
            ap = await _adversarial_pass(detail, task, provider)
            if ap.get("score") is not None:
                adv_scores.append(ap["score"])
            for f in ap.get("failures") or []:
                if f not in adv_failures:
                    adv_failures.append(f)
            await emit_event({"type": "evolve.workflow", "session": session,
                              "run_id": rid, "node": f"reviewer-{i+1}",
                              "state": "found", "round": rnd,
                              "failures": len(ap.get("failures") or [])})
        # harsher aggregate: min of critic + adversarial scores
        harsh = round(min(adv_scores), 1) if adv_scores else assessment.get("score")
        assessment["failures"] = adv_failures[:15]
        assessment["adversarial_scores"] = adv_scores
        assessment["score"] = harsh
        combined = round((detail.get("pass_rate", 0) * 10) * 0.5
                         + (harsh or 0) * 0.5, 1)
        assessment["combined"] = combined
        await _update_run(rid, {"assessment": assessment, "score": harsh,
                                "combined": combined, "reviewers": n})

    await emit_event({"type": "evolve.workflow", "session": session, "run_id": rid,
                      "node": "reviewers", "state": "done", "round": rnd,
                      "score": assessment.get("score"),
                      "failures": len(assessment.get("failures") or [])})
    return assessment


@capability("evolve.assess", memory="off",
            http_method="POST", http_path="/evolve/assess", http_tags=["evolve"],
            description="Have a critic LLM assess a stored run: score 0-10, "
                        "critique, failures, edit suggestions. Inputs: run_id "
                        "(str!), provider (str — 'ollama[:model]' | 'anthropic"
                        "[:model]' | 'openai[:model]' | stored provider id; "
                        "blank = configured critic).")
async def evolve_assess(run_id: str = "", provider: str = "", trace_id=None):
    got = await evolve_run_get(run_id=run_id)
    if got.get("error"):
        return got
    detail = got["run"]
    task = await _get_task(detail.get("task", "")) or {"id": detail.get("task")}
    cfg = await _get_config()
    return await _assess_run(detail, task, provider or cfg["critic_provider"])


@capability("evolve.assess.compare", memory="off",
            http_method="POST", http_path="/evolve/assess/compare", http_tags=["evolve"],
            description="Run TWO critics on the same run and report agreement — "
                        "how you know when the local model is good enough to take "
                        "over from Claude. Inputs: run_id (str!), providers (csv, "
                        "default 'ollama,anthropic'). Output: {assessments, "
                        "score_delta, agree_pass}.")
async def evolve_assess_compare(run_id: str = "", providers: str = "",
                                trace_id=None):
    got = await evolve_run_get(run_id=run_id)
    if got.get("error"):
        return got
    detail = got["run"]
    task = await _get_task(detail.get("task", "")) or {"id": detail.get("task")}
    provs = [p.strip() for p in (providers or "ollama,anthropic").split(",") if p.strip()][:3]
    results = {}
    for p in provs:
        results[p] = await _assess_run(dict(detail), task, p)
    scores = [a.get("score") for a in results.values()
              if isinstance(a.get("score"), (int, float))]
    passes = [a.get("passed") for a in results.values() if "passed" in a]
    return {
        "run_id": run_id, "assessments": results,
        "score_delta": round(max(scores) - min(scores), 1) if len(scores) > 1 else None,
        "agree_pass": (len(set(passes)) == 1) if len(passes) > 1 else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SUITE — run everything, keep a scoreboard (the automated regression harness)
# ─────────────────────────────────────────────────────────────────────────────

_SUITE_RUNNING = False
_SUITE_STATE: Dict[str, Any] = {"running": False}
_SUITE_TASK: Optional[asyncio.Task] = None


@capability("evolve.suite.run", memory="off",
            http_method="POST", http_path="/evolve/suite/run", http_tags=["evolve"],
            description="Run the whole benchmark suite (all enabled tasks — loop "
                        "goals AND cap smoke tests) sequentially, optionally with "
                        "critic assessment, and store the scoreboard. Inputs: tag "
                        "(str filter), profile (str filter), assess (bool), "
                        "provider (str — critic), variant_id (str — test a tuning "
                        "variant for its profile). Output: {suite_id, results:[...], "
                        "avg_combined, pass_rate}. This is the automation surface — "
                        "the loop_eval_nightly dream trigger calls it.")
async def evolve_suite_run(tag: str = "", profile: str = "", assess: bool = True,
                           provider: str = "", variant_id: str = "",
                           trace_id=None):
    global _SUITE_RUNNING, _SUITE_STATE
    if _SUITE_RUNNING:
        return {"error": "a suite run is already in progress"}
    _SUITE_RUNNING = True
    try:
        cfg = await _get_config()
        critic = provider or cfg["critic_provider"]
        tasks = [t for t in await _get_tasks() if t.get("enabled", True)]
        if tag:
            tasks = [t for t in tasks if tag in (t.get("tags") or [])]
        if profile:
            tasks = [t for t in tasks
                     if t.get("type") == "cap" or t.get("profile") == profile]
        if not tasks:
            _SUITE_STATE = {"running": False,
                            "error": "no matching tasks (check tag/profile filter)"}
            return {"error": "no matching tasks — check the tag/profile filter, "
                             "or add tasks in the Tasks tab"}
        # Run FAST cap smoke-tests first so the counter moves immediately (0→1→2
        # in seconds) — proving the plumbing works — before the slow agent-loop
        # tasks. Nothing is more reassuring than visible early progress.
        _order = {"cap": 0, "loop": 1, "sim": 2}
        tasks.sort(key=lambda t: (_order.get(t.get("type", "loop"), 1), t.get("id", "")))
        suite_id = uuid.uuid4().hex[:8]
        _SUITE_STATE = {"running": True, "suite_id": suite_id, "done": 0,
                        "total": len(tasks), "current": "", "current_started_at": "",
                        "started_at": now_iso(), "critic": critic if assess else ""}
        await emit_event({"type": "evolve.suite.started", "suite_id": suite_id,
                          "tasks": [t["id"] for t in tasks], "critic": critic,
                          "total": len(tasks)})
        results = []
        for t in tasks:
            variant = None
            if variant_id and t.get("type") == "loop":
                variant = await _get_variant(t.get("profile", ""), variant_id)
            _SUITE_STATE["current"] = f"{t.get('type','loop')}: {t['id']}"
            _SUITE_STATE["current_started_at"] = now_iso()
            await emit_event({"type": "evolve.suite.progress", "suite_id": suite_id,
                              "done": len(results), "total": len(tasks),
                              "task": t["id"], "phase": "running"})
            detail = await _run_task(t, variant=variant, source="suite",
                                     session_id=suite_id)
            if assess and t.get("type") == "loop":
                _SUITE_STATE["current"] = f"critic scoring: {t['id']}"
            row = {"task": t["id"], "label": t.get("label", ""),
                   "type": t.get("type", "loop"), "profile": t.get("profile", ""),
                   "run_id": detail["run_id"], "pass_rate": detail["pass_rate"],
                   "checks": f"{detail['checks_ok']}/{detail['checks_n']}",
                   "elapsed_s": detail["elapsed_s"],
                   "error": detail.get("error", ""),
                   "score": None, "combined": detail["combined"]}
            if assess and t.get("type") == "loop":
                a = await _assess_run(detail, t, critic, variant)
                if not a.get("error"):
                    row["score"] = a["score"]
                    row["combined"] = a["combined"]
            results.append(row)
            _SUITE_STATE["done"] = len(results)
            await emit_event({"type": "evolve.suite.progress", "suite_id": suite_id,
                              "done": len(results), "total": len(tasks),
                              "task": t["id"], "combined": row["combined"],
                              "phase": "done"})
        combined = [r["combined"] for r in results if r.get("combined") is not None]
        summary = {
            "suite_id": suite_id, "ts": now_iso(),
            "tasks_n": len(results),
            "avg_combined": round(mean(combined), 2) if combined else 0.0,
            "pass_rate": round(mean(r["pass_rate"] for r in results), 3) if results else 0.0,
            "critic": critic if assess else "",
            "variant": variant_id, "tag": tag, "profile": profile,
            "results": results,
        }
        r = _redis()
        if r:
            try:
                await r.lpush(KEY_SUITES, json.dumps(summary, default=str))
                await r.ltrim(KEY_SUITES, 0, SUITES_CAP - 1)
            except Exception:
                pass
        _SUITE_STATE = {"running": False, "suite_id": suite_id,
                        "done": len(results), "total": len(tasks),
                        "avg_combined": summary["avg_combined"],
                        "finished_at": now_iso()}
        await emit_event({"type": "evolve.suite.done", "suite_id": suite_id,
                          "avg_combined": summary["avg_combined"],
                          "pass_rate": summary["pass_rate"]})
        return summary
    finally:
        _SUITE_RUNNING = False
        if _SUITE_STATE.get("running"):
            _SUITE_STATE["running"] = False


@capability("evolve.suite.start", memory="off",
            http_method="POST", http_path="/evolve/suite/start", http_tags=["evolve"],
            description="Launch a benchmark suite in the BACKGROUND and return "
                        "immediately (the panel polls evolve.suite.status). Same "
                        "inputs as evolve.suite.run: tag, profile, assess, provider, "
                        "variant_id. Use this from the UI so a slow local run "
                        "doesn't block the request.")
async def evolve_suite_start(tag: str = "", profile: str = "", assess: bool = True,
                             provider: str = "", variant_id: str = "", trace_id=None):
    global _SUITE_TASK
    if _SUITE_RUNNING:
        return {"error": "a suite run is already in progress",
                "status": _SUITE_STATE}
    _SUITE_TASK = asyncio.create_task(evolve_suite_run(
        tag=tag, profile=profile, assess=assess, provider=provider,
        variant_id=variant_id))
    return {"ok": True, "started": True}


@capability("evolve.suite.status", memory="off", silent=True,
            http_method="GET", http_path="/evolve/suite/status", http_tags=["evolve"],
            description="Live status of the running (or last) benchmark suite: "
                        "{running, suite_id, done, total, current, avg_combined}.")
async def evolve_suite_status(trace_id=None):
    return {"status": _SUITE_STATE, "running": bool(_SUITE_RUNNING)}


@capability("evolve.suites", memory="off", silent=True,
            http_method="GET", http_path="/evolve/suites", http_tags=["evolve"],
            description="Recent suite scoreboards (newest first). Query: limit.")
async def evolve_suites(limit: int = 12, trace_id=None):
    r = _redis()
    out = []
    if r:
        try:
            rows = await r.lrange(KEY_SUITES, 0, max(0, int(limit) - 1))
            for row in rows or []:
                try:
                    out.append(json.loads(row.decode() if isinstance(row, bytes) else row))
                except Exception:
                    continue
        except Exception:
            pass
    return {"suites": out, "count": len(out)}


@capability("evolve.report", memory="off", silent=True,
            http_method="GET", http_path="/evolve/report", http_tags=["evolve"],
            description="Markdown QA report: latest suite scoreboard + trend over "
                        "recent suites + regressions vs the previous suite. Used "
                        "by the loop_eval_nightly dream as a collector. "
                        "Output: {text}.")
async def evolve_report(trace_id=None):
    suites = (await evolve_suites(limit=10)).get("suites", [])
    if not suites:
        return {"text": "No benchmark suites have run yet.", "count": 0}
    cur = suites[0]
    prev = suites[1] if len(suites) > 1 else None
    lines = [f"# Loop Lab suite report — {cur.get('ts', '')[:16]}",
             "",
             f"**Average combined score:** {cur.get('avg_combined')} / 10 · "
             f"**check pass rate:** {round(cur.get('pass_rate', 0) * 100)}% · "
             f"{cur.get('tasks_n')} tasks · critic: {cur.get('critic') or 'checks only'}",
             "", "| task | type | checks | score | combined | time | error |",
             "|---|---|---|---|---|---|---|"]
    prev_by = {r["task"]: r for r in (prev or {}).get("results", [])}
    regressions = []
    for row in cur.get("results", []):
        lines.append(f"| {row['task']} | {row['type']} | {row['checks']} | "
                     f"{row.get('score') if row.get('score') is not None else '—'} | "
                     f"{row.get('combined')} | {row['elapsed_s']}s | "
                     f"{(row.get('error') or '')[:40]} |")
        p = prev_by.get(row["task"])
        if p and p.get("combined") is not None and row.get("combined") is not None:
            delta = round(row["combined"] - p["combined"], 1)
            if delta <= -1.5:
                regressions.append(f"- **{row['task']}** dropped {abs(delta)} "
                                   f"({p['combined']} → {row['combined']})")
    lines.append("")
    lines.append("## Trend (recent suites)")
    for s in suites:
        bar = "█" * int(round((s.get("avg_combined") or 0)))
        lines.append(f"- {s.get('ts', '')[:16]} · avg {s.get('avg_combined')} {bar}")
    if regressions:
        lines.append("")
        lines.append("## Regressions vs previous suite")
        lines.extend(regressions)
    return {"text": "\n".join(lines), "count": len(suites),
            "avg_combined": cur.get("avg_combined")}


@capability("evolve.board", memory="off", silent=True,
            http_method="GET", http_path="/evolve/board", http_tags=["evolve"],
            description="Race-to-green board data: one lane per task, cells = its "
                        "result across the recent suites (combined score → green/"
                        "amber/red), so you can watch scores converge. Output: "
                        "{suites:[ts…], lanes:[{task,cells:[{score,ts}]}]}.")
async def evolve_board(limit: int = 20, trace_id=None):
    suites = (await evolve_suites(limit=int(limit))).get("suites", [])
    suites = list(reversed(suites))   # oldest → newest, left → right
    tasks: List[str] = []
    for s in suites:
        for r in s.get("results", []):
            if r.get("task") not in tasks:
                tasks.append(r.get("task"))
    lanes = []
    for t in tasks:
        cells = []
        for s in suites:
            row = next((r for r in s.get("results", []) if r.get("task") == t), None)
            cells.append({"score": (row or {}).get("combined"),
                          "ts": s.get("ts", ""), "err": bool((row or {}).get("error"))}
                         if row else {"score": None})
        lanes.append({"task": t, "cells": cells})
    return {"suites": [s.get("ts", "") for s in suites], "lanes": lanes,
            "count": len(suites)}


@capability("evolve.activity", memory="off", silent=True,
            http_method="GET", http_path="/evolve/activity", http_tags=["evolve"],
            description="Commit-chart data: hourly buckets of Loop Lab activity — "
                        "runs (pass/fail) + edit-queue actions — over the last "
                        "`hours`. Output: {buckets:[{hour,pass,fail,edits}]}.")
async def evolve_activity(hours: int = 72, trace_id=None):
    import datetime as _dt
    from datetime import timezone
    now = _dt.datetime.now(timezone.utc)
    span = max(6, min(336, int(hours)))
    start = now - _dt.timedelta(hours=span)
    buckets: Dict[str, Dict[str, int]] = {}

    def _key(iso: str):
        try:
            d = _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            if d < start:
                return None
            return d.strftime("%Y-%m-%dT%H")
        except Exception:
            return None

    runs = (await evolve_runs(limit=RUNS_CAP)).get("runs", [])
    for r in runs:
        k = _key(r.get("ts", ""))
        if not k:
            continue
        b = buckets.setdefault(k, {"pass": 0, "fail": 0, "edits": 0})
        ok = (r.get("combined") or 0) >= 6 and not r.get("error")
        b["pass" if ok else "fail"] += 1
    # edit-queue actions
    for a in (await evolve_editq_list(limit=EDITQ_CAP)).get("queue", []):
        k = _key(a.get("created_at", ""))
        if k:
            buckets.setdefault(k, {"pass": 0, "fail": 0, "edits": 0})["edits"] += 1

    out = []
    cur = start.replace(minute=0, second=0, microsecond=0)
    while cur <= now:
        k = cur.strftime("%Y-%m-%dT%H")
        b = buckets.get(k, {"pass": 0, "fail": 0, "edits": 0})
        out.append({"hour": k, **b})
        cur += _dt.timedelta(hours=1)
    return {"buckets": out, "hours": span}


# ─────────────────────────────────────────────────────────────────────────────
# VARIANTS + OVERLAY — the tuning state the improver evolves
# ─────────────────────────────────────────────────────────────────────────────

async def _get_variant(profile: str, variant_id: str) -> Optional[Dict[str, Any]]:
    r = _redis()
    if not r or not profile or not variant_id:
        return None
    try:
        raw = await r.hget(KEY_VARIANTS + profile, variant_id)
        if not raw:
            return None
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        return None


async def _save_variant(profile: str, variant: Dict[str, Any]):
    r = _redis()
    if r:
        try:
            await r.hset(KEY_VARIANTS + profile, variant["id"],
                         json.dumps(variant, default=str))
        except Exception:
            pass


@capability("evolve.variants", memory="off", silent=True,
            http_method="GET", http_path="/evolve/variants", http_tags=["evolve"],
            description="Tuning variants for a loop profile (+ the active promoted "
                        "overlay). Query: profile (str!).")
async def evolve_variants(profile: str = "", trace_id=None):
    r = _redis()
    out, overlay = [], None
    if r and profile:
        try:
            raw = await r.hgetall(KEY_VARIANTS + profile)
            for v in (raw or {}).values():
                try:
                    out.append(json.loads(v.decode() if isinstance(v, bytes) else v))
                except Exception:
                    continue
            oraw = await r.get(KEY_OVERLAY + profile)
            if oraw:
                overlay = json.loads(oraw.decode() if isinstance(oraw, bytes) else oraw)
        except Exception:
            pass
    out.sort(key=lambda v: v.get("created", ""), reverse=True)
    return {"profile": profile, "variants": out, "overlay": overlay}


@capability("evolve.variant.promote", memory="off",
            http_method="POST", http_path="/evolve/variant/promote", http_tags=["evolve"],
            description="Promote a tuning variant to the ACTIVE overlay for its "
                        "profile — loops.run merges it into every run of that "
                        "profile (between profile defaults and caller overrides). "
                        "Inputs: profile (str!), variant_id (str!).")
async def evolve_variant_promote(profile: str = "", variant_id: str = "",
                                 trace_id=None):
    v = await _get_variant(profile, variant_id)
    if not v:
        return {"error": "variant not found"}
    r = _redis()
    if not r:
        return {"error": "redis unavailable"}
    overlay = {"variant_id": v["id"], "profile": profile,
               "overrides": _clamp_overrides(v.get("overrides") or {}),
               "prompt_preamble": (v.get("prompt_preamble") or "")[:1500],
               "score": v.get("score"), "promoted_at": now_iso()}
    await r.set(KEY_OVERLAY + profile, json.dumps(overlay, default=str))
    await _audit("variant.promote", f"{profile} ← {variant_id} (score {v.get('score')})",
                 profile=profile, variant_id=variant_id, overrides=overlay["overrides"])
    await emit_event({"type": "evolve.overlay.promoted", "profile": profile,
                      "variant_id": variant_id, "score": v.get("score")})
    return {"ok": True, "overlay": overlay}


@capability("evolve.variant.clear", memory="off",
            http_method="POST", http_path="/evolve/variant/clear", http_tags=["evolve"],
            description="Remove the active overlay for a profile (revert to stock "
                        "profile behaviour). Input: profile (str!).")
async def evolve_variant_clear(profile: str = "", trace_id=None):
    r = _redis()
    if r and profile:
        try:
            await r.delete(KEY_OVERLAY + profile)
        except Exception:
            pass
    await _audit("variant.clear", f"{profile} overlay cleared (rollback)", profile=profile)
    await emit_event({"type": "evolve.overlay.cleared", "profile": profile})
    return {"ok": True}


@capability("evolve.overlay.get", memory="off", silent=True,
            http_method="GET", http_path="/evolve/overlay", http_tags=["evolve"],
            description="The active promoted overlay for a profile (or none). "
                        "Query: profile (str!).")
async def evolve_overlay_get(profile: str = "", trace_id=None):
    r = _redis()
    if not r or not profile:
        return {"overlay": None}
    try:
        raw = await r.get(KEY_OVERLAY + profile)
        if not raw:
            return {"overlay": None}
        return {"overlay": json.loads(raw.decode() if isinstance(raw, bytes) else raw)}
    except Exception:
        return {"overlay": None}


# ─────────────────────────────────────────────────────────────────────────────
# IMPROVE — the run → assess → edit → rerun session (background)
# ─────────────────────────────────────────────────────────────────────────────

_EDITOR_SYSTEM = (
    "You are the optimisation engineer for an autonomous agent engine. You are "
    "given several benchmark runs (traces, check results, critic critiques) all "
    "executed with the CURRENT tuning variant, plus the list of engine knobs you "
    "may change. Propose ONE improved variant that fixes the observed failures "
    "without over-fitting to a single task. Reply with ONLY a JSON object:\n"
    '{"rationale": "<3-5 sentences>", "overrides": {<knob>: <value>}, '
    '"prompt_preamble": "<system-prompt guidance for the agent, or empty>", '
    '"code_suggestions": [{"area": "...", "suggestion": "..."}]}\n'
    "overrides may ONLY use the given knobs. prompt_preamble is the highest-"
    "leverage tool: use it to correct behavioural failures (hallucination, "
    "skipping tools, format violations) — it is prepended to the agent's SYSTEM "
    "PROMPT for this profile. Keep it under 150 words.\n"
    "For deeper changes, code_suggestions may target: the SPECIALIST AGENT's "
    "system prompt / model / domain_caps (vera/agents/agents.py), the LOOP "
    "PROFILE's engine defaults or toolkit (vera/dag/loop_profiles.py), or the "
    "engine itself (vera/dag/dag_workshop_capabilities.py). Name the file/area "
    "precisely; these are applied on a branch and gated, never in-process."
)

_IMPROVE_TASKS: Dict[str, asyncio.Task] = {}
_IMPROVE_CANCEL: Dict[str, bool] = {}


# ═════════════════════════════════════════════════════════════════════════════
# BACKGROUND EDIT QUEUE — synthesis runs on gpt-oss:20b on a CPU node
# ═════════════════════════════════════════════════════════════════════════════
# The "synthesize" phase (propose the next variant, or any edit action) is
# enqueued here instead of being called inline, and a single background worker
# drains it on a cheap LOCAL model pinned to a CPU node. So editing happens off
# the critical path, off the GPU/API, and every action is visible + editable in
# the UI before it runs.

KEY_EDITQ   = "vera:evolve:editq"        # FIFO list of action ids
KEY_EDITQ_A = "vera:evolve:editq:"       # + id -> action JSON
EDITQ_CAP   = 400
_EDITQ_TASK: Optional[asyncio.Task] = None


async def _editq_save(a: Dict[str, Any]):
    r = _redis()
    if r:
        try:
            await r.set(KEY_EDITQ_A + a["id"], json.dumps(a, default=str))
            await r.expire(KEY_EDITQ_A + a["id"], 14 * 86400)
        except Exception as e:
            log.debug("editq save: %s", e)


async def _editq_get(aid: str) -> Optional[Dict[str, Any]]:
    r = _redis()
    if not r or not aid:
        return None
    try:
        raw = await r.get(KEY_EDITQ_A + aid)
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw) if raw else None
    except Exception:
        return None


async def _editq_enqueue(*, kind: str, system: str, prompt: str, session: str = "",
                         round: int = 0, profile: str = "", label: str = "") -> str:
    aid = uuid.uuid4().hex[:10]
    cfg = await _get_config()
    action = {
        "id": aid, "kind": kind, "system": system, "prompt": prompt,
        "session": session, "round": round, "profile": profile,
        "label": label or f"{kind} · {profile}",
        "status": "queued", "created_at": now_iso(), "started_at": "", "ended_at": "",
        "model": cfg.get("editq_model", "gpt-oss:20b"),
        "instance": cfg.get("editq_instance", ""),
        "provider": cfg.get("editq_provider", "ollama"),
        "result_text": "", "parsed": None, "error": "",
    }
    await _editq_save(action)
    r = _redis()
    if r:
        try:
            await r.rpush(KEY_EDITQ, aid)
            await r.ltrim(KEY_EDITQ, -EDITQ_CAP, -1)
        except Exception:
            pass
    await _audit("editq.enqueue", f"{kind} for {profile} (round {round})",
                 id=aid, session=session)
    await emit_event({"type": "evolve.editq.enqueued", "id": aid, "kind": kind,
                      "session": session, "round": round, "profile": profile})
    _editq_start()
    return aid


async def _editq_await(aid: str, timeout_s: int) -> Dict[str, Any]:
    """Block until an enqueued action reaches a terminal state (or timeout)."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        a = await _editq_get(aid)
        if not a:
            return {"error": "action vanished"}
        if a.get("status") in ("done", "error", "cancelled"):
            return a
        await asyncio.sleep(2)
    return {"error": "editq await timed out", "timeout": True}


def _editq_start():
    global _EDITQ_TASK
    if _EDITQ_TASK is None or _EDITQ_TASK.done():
        _EDITQ_TASK = asyncio.create_task(_editq_worker())


async def _editq_worker():
    """Drain the edit queue one action at a time on the pinned CPU model."""
    log.info("evolve editq worker started")
    r = _redis()
    if not r:
        return
    idle = 0
    try:
        while True:
            try:
                aid = await r.lpop(KEY_EDITQ)
            except Exception:
                aid = None
            aid = aid.decode() if isinstance(aid, bytes) else aid
            if not aid:
                idle += 1
                if idle > 60:        # exit after ~5 min idle; re-armed on enqueue
                    break
                await asyncio.sleep(5)
                continue
            idle = 0
            a = await _editq_get(aid)
            if not a or a.get("status") == "cancelled":
                continue
            cfg = await _get_config()
            a["status"] = "running"
            a["started_at"] = now_iso()
            a["model"] = cfg.get("editq_model", a.get("model"))
            a["instance"] = cfg.get("editq_instance", a.get("instance"))
            await _editq_save(a)
            await emit_event({"type": "evolve.editq.running", "id": aid,
                              "model": a["model"], "instance": a["instance"],
                              "session": a.get("session"), "round": a.get("round")})
            spec = f"{cfg.get('editq_provider','ollama')}:{cfg.get('editq_model','gpt-oss:20b')}"
            try:
                res = await asyncio.wait_for(
                    _provider_chat(spec, a["prompt"], system=a["system"],
                                   max_tokens=2000,
                                   instance=cfg.get("editq_instance", ""),
                                   prefer_gpu=False),
                    timeout=int(cfg.get("editq_timeout_s", 600)))
                if res.get("error"):
                    a["status"] = "error"; a["error"] = res["error"]
                else:
                    a["result_text"] = res.get("text", "")
                    a["parsed"] = _extract_json(res.get("text", ""))
                    a["ran_on"] = res.get("instance", a.get("instance"))
                    a["status"] = "done"
            except asyncio.TimeoutError:
                a["status"] = "error"; a["error"] = "timed out on CPU node"
            except Exception as e:
                a["status"] = "error"; a["error"] = str(e)[:300]
            a["ended_at"] = now_iso()
            await _editq_save(a)
            await emit_event({"type": "evolve.editq.done", "id": aid,
                              "status": a["status"], "session": a.get("session"),
                              "round": a.get("round"), "ran_on": a.get("ran_on", "")})
    finally:
        log.info("evolve editq worker stopped")


@capability("evolve.editq.list", memory="off", silent=True,
            http_method="GET", http_path="/evolve/editq", http_tags=["evolve"],
            description="The background edit queue — actions synthesised on the "
                        "pinned CPU model (gpt-oss:20b), newest first. Query: limit, "
                        "session, status.")
async def evolve_editq_list(limit: int = 60, session: str = "", status: str = "",
                            trace_id=None):
    r = _redis()
    out: List[Dict[str, Any]] = []
    if r:
        try:
            ids = await r.lrange(KEY_EDITQ, 0, -1)  # live queued order
        except Exception:
            ids = []
        # Also scan stored action records for terminal ones not in the live list.
        seen = set()
        for aid in reversed([i.decode() if isinstance(i, bytes) else i for i in (ids or [])]):
            a = await _editq_get(aid)
            if a:
                out.append(a); seen.add(aid)
        try:
            cur = 0
            while True:
                cur, batch = await r.scan(cur, match=KEY_EDITQ_A + "*", count=200)
                for k in batch:
                    aid = (k.decode() if isinstance(k, bytes) else k).split(":")[-1]
                    if aid in seen:
                        continue
                    a = await _editq_get(aid)
                    if a:
                        out.append(a); seen.add(aid)
                if cur == 0:
                    break
        except Exception:
            pass
    out.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    if session:
        out = [a for a in out if a.get("session") == session]
    if status:
        out = [a for a in out if a.get("status") == status]
    return {"queue": out[:int(limit)], "count": len(out),
            "worker_running": bool(_EDITQ_TASK and not _EDITQ_TASK.done())}


@capability("evolve.editq.get", memory="off", silent=True,
            http_method="GET", http_path="/evolve/editq/get", http_tags=["evolve"],
            description="One edit-queue action (prompt, result, status). Input: id.")
async def evolve_editq_get(id: str = "", trace_id=None):
    a = await _editq_get(id)
    return {"action": a} if a else {"error": "not found"}


@capability("evolve.editq.update", memory="off",
            http_method="POST", http_path="/evolve/editq/update", http_tags=["evolve"],
            description="Edit a QUEUED action before it runs — tweak its prompt or "
                        "system. Inputs: id (str!), prompt (str), system (str). "
                        "Only queued actions can be edited.")
async def evolve_editq_update(id: str = "", prompt: str = "", system: str = "",
                              trace_id=None):
    a = await _editq_get(id)
    if not a:
        return {"error": "not found"}
    if a.get("status") != "queued":
        return {"error": f"action is '{a.get('status')}' — only queued actions can be edited"}
    if prompt:
        a["prompt"] = prompt
    if system:
        a["system"] = system
    a["edited_at"] = now_iso()
    await _editq_save(a)
    await _audit("editq.update", f"edited queued action {id}", id=id)
    return {"ok": True, "action": a}


@capability("evolve.editq.cancel", memory="off",
            http_method="POST", http_path="/evolve/editq/cancel", http_tags=["evolve"],
            description="Cancel a queued edit action. Input: id (str!).")
async def evolve_editq_cancel(id: str = "", trace_id=None):
    a = await _editq_get(id)
    if not a:
        return {"error": "not found"}
    if a.get("status") in ("done", "error"):
        return {"error": "already finished"}
    a["status"] = "cancelled"; a["ended_at"] = now_iso()
    await _editq_save(a)
    r = _redis()
    if r:
        try:
            await r.lrem(KEY_EDITQ, 0, id)
        except Exception:
            pass
    await emit_event({"type": "evolve.editq.done", "id": id, "status": "cancelled"})
    return {"ok": True}


@capability("evolve.editq.worker", memory="off",
            http_method="POST", http_path="/evolve/editq/worker", http_tags=["evolve"],
            description="Ensure the background edit-queue worker is running. "
                        "Output: {ok, running}.")
async def evolve_editq_worker_start(trace_id=None):
    _editq_start()
    return {"ok": True, "running": bool(_EDITQ_TASK and not _EDITQ_TASK.done())}


@capability("evolve.instances", memory="off", silent=True,
            http_method="GET", http_path="/evolve/instances", http_tags=["evolve"],
            description="Ollama instances (for pinning the edit queue to a CPU "
                        "node). Output: {instances:[{id,label,has_gpu,status}]}.")
async def evolve_instances(trace_id=None):
    res = await _call("ollama.instances")
    out = []
    if isinstance(res, dict):
        items = res.get("instances") or res.get("ollama_instances") or {}
        if isinstance(items, dict):
            for iid, i in items.items():
                out.append({"id": iid, "label": (i or {}).get("label", iid),
                            "has_gpu": bool((i or {}).get("has_gpu")),
                            "status": (i or {}).get("status", "")})
        elif isinstance(items, list):
            for i in items:
                out.append({"id": i.get("id") or i.get("instance_id"),
                            "label": i.get("label", ""), "has_gpu": bool(i.get("has_gpu")),
                            "status": i.get("status", "")})
    return {"instances": out}


async def _save_session(sess: Dict[str, Any]):
    r = _redis()
    if not r:
        return
    try:
        await r.set(KEY_SESSION + sess["id"], json.dumps(sess, default=str))
        await r.expire(KEY_SESSION + sess["id"], 30 * 86400)
        # maintain compact list
        rows = await r.lrange(KEY_SESSIONS, 0, SESSIONS_CAP - 1)
        compact = {k: sess.get(k) for k in
                   ("id", "profile", "status", "started_at", "ended_at",
                    "rounds_done", "max_rounds", "best_score", "best_variant",
                    "critic", "editor", "target_score", "current", "error", "phase")}
        compact["code_suggestions"] = len(sess.get("code_suggestions") or [])
        found = False
        for i, row in enumerate(rows or []):
            try:
                rec = json.loads(row.decode() if isinstance(row, bytes) else row)
            except Exception:
                continue
            if rec.get("id") == sess["id"]:
                await r.lset(KEY_SESSIONS, i, json.dumps(compact, default=str))
                found = True
                break
        if not found:
            await r.lpush(KEY_SESSIONS, json.dumps(compact, default=str))
            await r.ltrim(KEY_SESSIONS, 0, SESSIONS_CAP - 1)
    except Exception as e:
        log.debug("evolve save session: %s", e)


def _editor_prompt(sess: Dict[str, Any], round_rows: List[Dict[str, Any]],
                   variant: Optional[Dict[str, Any]]) -> str:
    parts = [f"PROFILE UNDER TUNING: {sess['profile']}",
             f"TARGET SCORE: {sess['target_score']} / 10",
             "CURRENT VARIANT:\n" + json.dumps(
                 {"overrides": (variant or {}).get("overrides", {}),
                  "prompt_preamble": (variant or {}).get("prompt_preamble", "")},
                 indent=1)[:1500]]
    for row in round_rows:
        a = row.get("assessment") or {}
        parts.append(
            f"--- TASK {row['task']} (combined {row.get('combined')}, "
            f"checks {row.get('checks')}) ---\n"
            f"critique: {(a.get('critique') or '(none)')[:900]}\n"
            f"failures: {json.dumps(a.get('failures') or [])[:600]}\n"
            f"trace: " + "; ".join(
                f"{s.get('cap')}{'✓' if s.get('ok') else '✗'}"
                for s in (row.get('steps') or [])[:14]))
    parts.append(_KNOB_GUIDE)
    parts.append("Propose the next variant now (JSON only).")
    return "\n\n".join(parts)


async def _improve_worker(sess: Dict[str, Any]):
    sid = sess["id"]
    profile = sess["profile"]
    try:
        # Goal-sourced sessions carry their own tasks (goals / generated);
        # otherwise use the profile's existing benchmark loop tasks.
        if sess.get("tasks_override"):
            tasks = sess["tasks_override"]
        else:
            all_tasks = await _get_tasks()
            tasks = [t for t in all_tasks
                     if t.get("enabled", True) and t.get("type") == "loop"
                     and t.get("profile") == profile
                     and (not sess.get("tag") or sess["tag"] in (t.get("tags") or []))]
        if not tasks:
            all_tasks = await _get_tasks()
            have = sorted({t.get("profile") for t in all_tasks
                           if t.get("type") == "loop" and t.get("enabled", True)})
            sess["status"] = "error"
            sess["current"] = ""
            sess["error"] = (f"no loop tasks for profile '{profile}' / this goal "
                             f"source. Profiles with tasks: "
                             f"{', '.join(p for p in have if p) or '(none)'}. "
                             "Add a task, or use goal_source=goals|generate.")
            await _save_session(sess)
            await emit_event({"type": "evolve.improve.done", "session": sid,
                              "status": "error", "error": sess["error"]})
            return
        variant: Optional[Dict[str, Any]] = None
        if sess.get("base_variant"):
            variant = await _get_variant(profile, sess["base_variant"])
        best = {"score": -1.0, "variant_id": (variant or {}).get("id", "")}

        for rnd in range(1, int(sess["max_rounds"]) + 1):
            if _IMPROVE_CANCEL.get(sid):
                sess["status"] = "cancelled"
                break
            sess["current"] = f"round {rnd}/{sess['max_rounds']} starting"
            await _save_session(sess)
            await emit_event({"type": "evolve.improve.round", "session": sid,
                              "round": rnd, "variant": (variant or {}).get("id", "")})
            round_rows = []
            for ti, t in enumerate(tasks, 1):
                if _IMPROVE_CANCEL.get(sid):
                    break
                # ── PHASE 1: TEST — run the agent loop (watch it live) ────────
                sess["current"] = (f"round {rnd}/{sess['max_rounds']} · "
                                   f"testing {ti}/{len(tasks)}: {t['id']}")
                sess["phase"] = "test"
                await _save_session(sess)
                await emit_event({"type": "evolve.phase", "session": sid, "phase": "test",
                                  "status": "start", "round": rnd, "task": t["id"],
                                  "index": ti, "total": len(tasks)})
                await emit_event({"type": "evolve.workflow", "session": sid,
                                  "node": "implementer", "state": "running",
                                  "round": rnd, "task": t["id"]})
                await emit_event({"type": "evolve.improve.task", "session": sid,
                                  "round": rnd, "task": t["id"],
                                  "index": ti, "total": len(tasks)})
                detail = await _run_task(t, variant=variant, source="improve",
                                         session_id=sid)
                await emit_event({"type": "evolve.workflow", "session": sid,
                                  "node": "implementer", "state": "done",
                                  "round": rnd, "run_id": detail["run_id"]})
                await emit_event({"type": "evolve.phase", "session": sid, "phase": "test",
                                  "status": "done", "round": rnd, "task": t["id"],
                                  "run_id": detail["run_id"], "where": detail.get("where")})
                # ── PHASE 2: EVALUATE — adversarial reviewers score the run ───
                sess["current"] = (f"round {rnd}/{sess['max_rounds']} · "
                                   f"evaluating {ti}/{len(tasks)}: {t['id']}")
                sess["phase"] = "evaluate"
                await _save_session(sess)
                await emit_event({"type": "evolve.phase", "session": sid, "phase": "evaluate",
                                  "status": "start", "round": rnd, "task": t["id"],
                                  "run_id": detail["run_id"]})
                a = await _review_run(detail, t, sess["critic"], session=sid, rnd=rnd)
                await emit_event({"type": "evolve.phase", "session": sid, "phase": "evaluate",
                                  "status": "done", "round": rnd, "task": t["id"],
                                  "run_id": detail["run_id"], "score": a.get("score"),
                                  "combined": a.get("combined")})
                detail["assessment"] = a
                round_rows.append({
                    "task": t["id"], "run_id": detail["run_id"],
                    "checks": f"{detail['checks_ok']}/{detail['checks_n']}",
                    "combined": (a.get("combined")
                                 if not a.get("error") else detail["combined"]),
                    "steps": detail.get("steps"), "assessment": a,
                })
            if _IMPROVE_CANCEL.get(sid):
                sess["status"] = "cancelled"
                break
            combined = [r["combined"] for r in round_rows
                        if r.get("combined") is not None]
            avg = round(mean(combined), 2) if combined else 0.0

            # collect code suggestions from critics this round
            for row in round_rows:
                for cs in ((row.get("assessment") or {}).get("edits") or {}) \
                        .get("code_suggestions", []):
                    sess.setdefault("code_suggestions", [])
                    if cs and cs not in sess["code_suggestions"]:
                        sess["code_suggestions"].append(cs)

            round_rec = {"round": rnd, "avg": avg,
                         "variant": (variant or {}).get("id", ""),
                         "results": [{k: r[k] for k in
                                      ("task", "run_id", "checks", "combined")}
                                     for r in round_rows]}
            sess.setdefault("rounds", []).append(round_rec)
            sess["rounds_done"] = rnd
            if avg > best["score"]:
                best = {"score": avg, "variant_id": (variant or {}).get("id", "")}
            sess["best_score"] = best["score"]
            sess["best_variant"] = best["variant_id"]
            await _save_session(sess)
            await emit_event({"type": "evolve.improve.scored", "session": sid,
                              "round": rnd, "avg": avg, "best": best["score"]})

            if avg >= float(sess["target_score"]):
                sess["status"] = "satisfied"
                break
            if rnd >= int(sess["max_rounds"]):
                sess["status"] = "max_rounds"
                break

            # ── PHASE 3: SYNTHESIZE — propose the next variant ───────────────
            # Runs on the background edit queue (gpt-oss:20b on a CPU node) when
            # editq_enabled, so synthesis is off the critical path and visible/
            # editable; else the editor_provider runs inline.
            cfg = await _get_config()
            editor_prompt = _editor_prompt(sess, round_rows, variant)
            sess["phase"] = "synthesize"
            await emit_event({"type": "evolve.phase", "session": sid, "phase": "synthesize",
                              "status": "start", "round": rnd})
            await emit_event({"type": "evolve.workflow", "session": sid, "node": "fixer",
                              "state": "running", "round": rnd,
                              "model": cfg.get("editq_model", "gpt-oss:20b")})
            if cfg.get("editq_enabled", True):
                sess["current"] = (f"round {rnd} · synthesising on "
                                   f"{cfg.get('editq_model','gpt-oss:20b')} (background CPU)")
                await _save_session(sess)
                aid = await _editq_enqueue(kind="variant", system=_EDITOR_SYSTEM,
                                           prompt=editor_prompt, session=sid,
                                           round=rnd, profile=profile,
                                           label=f"variant {profile} r{rnd}")
                sess.setdefault("editq_ids", []).append(aid)
                await _save_session(sess)
                got = await _editq_await(aid, int(cfg.get("editq_timeout_s", 600)) + 120)
                parsed = got.get("parsed")
                editor_name = f"{cfg.get('editq_provider','ollama')}:{cfg.get('editq_model','gpt-oss:20b')}"
                err = got.get("error") or (got.get("status") == "error" and got.get("error"))
            else:
                sess["current"] = f"round {rnd} · editor ({sess['editor']}) proposing variant"
                await _save_session(sess)
                await emit_event({"type": "evolve.improve.editing", "session": sid,
                                  "round": rnd, "editor": sess["editor"]})
                eres = await _provider_chat(sess["editor"], editor_prompt,
                                            system=_EDITOR_SYSTEM, max_tokens=2000)
                parsed = _extract_json(eres.get("text", "")) if not eres.get("error") else None
                editor_name = eres.get("provider", sess["editor"])
                err = eres.get("error")
            await emit_event({"type": "evolve.phase", "session": sid, "phase": "synthesize",
                              "status": "done", "round": rnd,
                              "ok": bool(parsed), "editor": editor_name})
            await emit_event({"type": "evolve.workflow", "session": sid, "node": "fixer",
                              "state": ("done" if parsed else "error"), "round": rnd})
            if not parsed:
                sess.setdefault("notes", []).append(
                    f"round {rnd}: synthesis returned no usable JSON "
                    f"({(err or 'parse failure')[:160]})")
                sess["status"] = "editor_failed"
                break
            new_variant = {
                "id": f"{sid}-r{rnd}",
                "profile": profile,
                "overrides": _clamp_overrides(parsed.get("overrides")),
                "prompt_preamble": str(parsed.get("prompt_preamble") or "")[:1500],
                "rationale": str(parsed.get("rationale") or "")[:1200],
                "parent": (variant or {}).get("id", ""),
                "round": rnd, "session": sid, "score": None,
                "created": now_iso(),
                "editor": editor_name,
            }
            for cs in (parsed.get("code_suggestions") or [])[:6]:
                if isinstance(cs, dict):
                    sess.setdefault("code_suggestions", []).append(
                        {"area": str(cs.get("area", ""))[:120],
                         "suggestion": str(cs.get("suggestion", ""))[:800]})
            await _save_variant(profile, new_variant)
            variant = new_variant
            await _save_session(sess)

        else:
            sess["status"] = sess.get("status") or "max_rounds"

        # score the last variant with its round average (for the variants table)
        if variant and sess.get("rounds"):
            last_avg = sess["rounds"][-1]["avg"]
            variant["score"] = last_avg
            await _save_variant(profile, variant)
            if last_avg >= best["score"]:
                best = {"score": last_avg, "variant_id": variant["id"]}
                sess["best_score"] = best["score"]
                sess["best_variant"] = best["variant_id"]

        # optionally hand code suggestions to the Claude Code work queue
        cfg = await _get_config()
        if cfg.get("allow_code_edits") and sess.get("code_suggestions"):
            queued = 0
            for cs in sess["code_suggestions"][:5]:
                res = await _call(
                    "ide.remote.queue.add",
                    task=("Loop Lab improvement suggestion for the Vera agent "
                          f"engine (profile '{profile}'). Area: {cs.get('area')}\n"
                          f"Suggestion: {cs.get('suggestion')}\n"
                          "Locate the relevant code, apply a minimal safe change, "
                          "and run any nearby tests. Do not refactor broadly."),
                    engine="claude", source="dream", priority=6)
                if isinstance(res, dict) and res.get("ok"):
                    queued += 1
            sess["code_edits_queued"] = queued

        if sess["status"] in ("", None, "running"):
            sess["status"] = "done"
    except asyncio.CancelledError:
        sess["status"] = "cancelled"
        sess["current"] = ""
        log.info("evolve improve session %s hard-cancelled", sid)
    except Exception as e:
        log.warning("evolve improve session %s error: %s", sid, e)
        sess["status"] = "error"
        sess["error"] = str(e)[:400]
    finally:
        sess["ended_at"] = now_iso()
        sess["current"] = ""
        sess["phase"] = ""
        await _save_session(sess)
        _IMPROVE_TASKS.pop(sid, None)
        _IMPROVE_CANCEL.pop(sid, None)
        await emit_event({"type": "evolve.improve.done", "session": sid,
                          "status": sess.get("status"),
                          "best_score": sess.get("best_score"),
                          "best_variant": sess.get("best_variant"),
                          "code_suggestions": len(sess.get("code_suggestions") or [])})


async def _resolve_improve_tasks(profile: str, goal_source: str, goals: Any,
                                 generate_from: str, tag: str,
                                 cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Resolve the loop tasks an improve session tunes against. Sources:
      tasks    — the profile's existing benchmark loop tasks (default)
      goals    — an explicit `goals` list, else the system's long-term goals
                 (goals.list) — each becomes an ephemeral loop task
      generate — LLM-generate fresh tasks from `generate_from` (a subsystem/goal)
    """
    src = (goal_source or "tasks").lower()
    if src == "generate" and generate_from:
        gen = await evolve_tasks_generate(goal=generate_from, count=4,
                                          profile=profile, save=False,
                                          provider=cfg.get("editor_provider", ""))
        return [t for t in (gen.get("tasks") or []) if t.get("type") == "loop"]
    if src == "goals":
        glist = goals if isinstance(goals, list) else (
            [g.strip() for g in str(goals).split("\n") if g.strip()] if goals else [])
        if not glist:
            res = await _call("goals.list")
            for g in (res or {}).get("goals", []) if isinstance(res, dict) else []:
                txt = g.get("goal") or g.get("title") or g.get("name") or ""
                if txt:
                    glist.append(txt)
        out = []
        for i, g in enumerate(glist[:8]):
            out.append({
                "id": f"goal-{i}-{uuid.uuid4().hex[:4]}", "type": "loop",
                "label": str(g)[:60], "profile": profile, "goal": str(g),
                "allowed_caps": "", "checks": [{"type": "final_nonempty"}],
                "rubric": "Did the loop make real, grounded progress toward the "
                          "goal with valid tool use?",
                "max_steps": 6, "timeout_s": 300, "tags": ["goal"], "enabled": True,
            })
        return out
    # default: existing benchmark tasks for the profile
    return [t for t in await _get_tasks()
            if t.get("enabled", True) and t.get("type") == "loop"
            and t.get("profile") == profile
            and (not tag or tag in (t.get("tags") or []))]


@capability("evolve.improve.start", memory="on",
            http_method="POST", http_path="/evolve/improve/start", http_tags=["evolve"],
            description="Start a background improvement session: TEST (run the "
                        "agent loop) → EVALUATE (critic scores) → SYNTHESIZE "
                        "(propose a better variant, on the background edit queue / "
                        "gpt-oss:20b CPU node), repeated until target_score or "
                        "max_rounds. Inputs: profile (str), goal_source "
                        "(tasks|goals|generate), goals (list — explicit goals for "
                        "goal_source=goals), generate_from (str — subsystem/goal "
                        "for goal_source=generate), tag, critic, editor, "
                        "max_rounds, target_score, base_variant. Output: {ok, "
                        "session_id, tasks}. Poll evolve.improve.status.",
            schema=enum_schema(goal_source=["tasks", "goals", "generate"]))
async def evolve_improve_start(profile: str = "", target: str = "", tag: str = "",
                               critic: str = "", editor: str = "", max_rounds: int = 0,
                               target_score: float = 0.0, base_variant: str = "",
                               goal_source: str = "tasks",
                               goals: Optional[List[str]] = None,
                               generate_from: str = "", trace_id=None):
    cfg = await _get_config()
    # A categorised target ("specialist:coding" | "agent:coder" | …) resolves to
    # the loop profile used to exercise it; a bare profile still works.
    tgt = await _resolve_target(target) if target else None
    profile = (profile or (tgt or {}).get("profile") or cfg["default_profile"]).strip()
    tasks = await _resolve_improve_tasks(profile, goal_source, goals,
                                         generate_from, tag.strip(), cfg)
    sess = {
        "id": uuid.uuid4().hex[:8],
        "profile": profile, "tag": tag.strip(),
        "target": (tgt or {}).get("id", f"specialist:{profile}"),
        "target_label": (tgt or {}).get("label", profile),
        "goal_source": goal_source,
        "critic": (critic or cfg["critic_provider"]).strip(),
        "editor": (editor or cfg["editor_provider"]).strip(),
        "max_rounds": int(max_rounds) or int(cfg["max_rounds"]),
        "target_score": float(target_score) or float(cfg["target_score"]),
        "base_variant": base_variant.strip(),
        "tasks_override": tasks if goal_source != "tasks" else None,
        "status": "running", "started_at": now_iso(), "ended_at": "", "phase": "",
        "rounds": [], "rounds_done": 0,
        "best_score": None, "best_variant": "", "code_suggestions": [],
    }
    await _save_session(sess)
    _editq_start()
    _IMPROVE_CANCEL[sess["id"]] = False
    _IMPROVE_TASKS[sess["id"]] = asyncio.create_task(_improve_worker(sess))
    await emit_event({"type": "evolve.improve.started", "session": sess["id"],
                      "profile": profile, "critic": sess["critic"],
                      "editor": sess["editor"], "goal_source": goal_source,
                      "tasks": len(tasks)})
    return {"ok": True, "session_id": sess["id"], "session": sess,
            "tasks": [t.get("id") for t in tasks]}


@capability("evolve.improve.status", memory="off", silent=True,
            http_method="GET", http_path="/evolve/improve/status", http_tags=["evolve"],
            description="Full state of one improvement session (rounds, scores, "
                        "variants, code suggestions). Input: session_id (str!).")
async def evolve_improve_status(session_id: str = "", trace_id=None):
    r = _redis()
    if not r or not session_id:
        return {"error": "session_id required"}
    raw = await r.get(KEY_SESSION + session_id)
    if not raw:
        return {"error": "session not found"}
    sess = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    sess["live"] = session_id in _IMPROVE_TASKS
    return {"session": sess}


@capability("evolve.improve.list", memory="off", silent=True,
            http_method="GET", http_path="/evolve/improve/list", http_tags=["evolve"],
            description="Recent improvement sessions (compact, newest first).")
async def evolve_improve_list(limit: int = 20, trace_id=None):
    r = _redis()
    out = []
    if r:
        try:
            rows = await r.lrange(KEY_SESSIONS, 0, max(0, int(limit) - 1))
            for row in rows or []:
                try:
                    rec = json.loads(row.decode() if isinstance(row, bytes) else row)
                    rec["live"] = rec.get("id") in _IMPROVE_TASKS
                    out.append(rec)
                except Exception:
                    continue
        except Exception:
            pass
    return {"sessions": out, "count": len(out)}


@capability("evolve.improve.cancel", memory="off",
            http_method="POST", http_path="/evolve/improve/cancel", http_tags=["evolve"],
            description="Stop a running improvement session. Sets the cooperative "
                        "cancel flag AND hard-cancels the asyncio task so a session "
                        "stuck inside a slow loop run stops promptly. Also force-"
                        "marks a stale 'running' record done if its task is gone. "
                        "Input: session_id (str!).")
async def evolve_improve_cancel(session_id: str = "", trace_id=None):
    task = _IMPROVE_TASKS.get(session_id)
    if task:
        _IMPROVE_CANCEL[session_id] = True
        task.cancel()   # interrupt an in-flight await (e.g. a slow loops.run)
        await emit_event({"type": "evolve.improve.cancelling", "session": session_id})
        return {"ok": True, "cancelling": True}
    # No live task — the record may be a stale "running" left by a crash/restart.
    r = _redis()
    if r and session_id:
        try:
            raw = await r.get(KEY_SESSION + session_id)
            if raw:
                sess = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                if sess.get("status") == "running":
                    sess["status"] = "cancelled"
                    sess["current"] = ""
                    sess["ended_at"] = now_iso()
                    await _save_session(sess)
                    return {"ok": True, "cleared_stale": True}
        except Exception:
            pass
    return {"ok": False, "error": "session not running"}


@capability("evolve.code.queue", memory="on",
            http_method="POST", http_path="/evolve/code/queue", http_tags=["evolve"],
            description="Dispatch ONE code suggestion from an improvement session "
                        "to the Claude Code work queue (ide.remote.queue.add) — the "
                        "'Claude edits' half of the loop. Inputs: session_id (str!), "
                        "index (int — position in the session's code_suggestions).")
async def evolve_code_queue(session_id: str = "", index: int = 0, trace_id=None):
    st = await evolve_improve_status(session_id=session_id)
    if st.get("error"):
        return st
    sugg = (st["session"].get("code_suggestions") or [])
    if not (0 <= int(index) < len(sugg)):
        return {"error": f"index out of range (0..{len(sugg)-1})"}
    cs = sugg[int(index)]
    # Route through the gated CODE PIPELINE (branch → worktree-pinned edit →
    # sandbox test → manual merge) so the edit can NEVER land on the real
    # source directly — the worktree copy is the only thing the editor sees.
    res = await evolve_pipeline_run(
        kind="code", profile=st["session"].get("profile", ""),
        edits=[cs], auto_promote=False)
    await _audit("code.queue",
                 f"suggestion → code pipeline {res.get('id', '?')}: {cs.get('area','')}",
                 session=session_id, area=cs.get("area", ""),
                 pipeline_id=res.get("id"))
    return res if isinstance(res, dict) else {"ok": bool(res)}


# ═════════════════════════════════════════════════════════════════════════════
# CI/CD — the pipeline for loop changes: baseline → branch → edit → test → gate
#         → promote or roll back. Git branches make every change reversible.
# ═════════════════════════════════════════════════════════════════════════════
# Two change KINDS flow through one pipeline:
#   • variant — a tuning variant (engine knobs + prompt preamble). Applied at
#     runtime by loops.run via the overlay, so it needs NO code reload: the
#     pipeline tests it as a temporary overlay, gates it, and promotes the
#     overlay (or discards it). Fully in-process.
#   • code — a source change proposed by the editor/critic. The pipeline cuts a
#     git branch, hands the edit to the Claude Code queue on that branch, tests
#     it (in the dev sandbox when one is up — see evolve.sandbox.*), gates it,
#     then MERGES the branch to main (promote) or DELETES it (roll back). Git is
#     the rollback mechanism — a bad change never touches main.

KEY_PIPELINES = "vera:evolve:pipelines"       # list of pipeline records (newest first)
KEY_PIPELINE  = "vera:evolve:pipeline:"       # + id -> full record
PIPELINES_CAP = 100
BRANCH_PREFIX = "loop-lab/"

# ─────────────────────────────────────────────────────────────────────────────
# REPO REGISTRY — other git repos Loop Lab can branch/test/promote/rollback,
# alongside Vera's own. 'vera' is always present (protected, seeded from this
# checkout) even with nothing in Redis yet, so every existing call site that
# never passes `repo=` keeps operating on the Vera repo exactly as before —
# this is purely additive. (KEY_REPOS / DEFAULT_REPO_ID / DEFAULT_TEST_CMD are
# defined up near the other KEY_* constants — see the comment there.)
# ─────────────────────────────────────────────────────────────────────────────


def _repo_root() -> Path:
    """The Vera git repo root (…/Vera, which contains .git)."""
    return _HERE.parent.parent


async def _get_repos() -> Dict[str, Dict[str, Any]]:
    """All registered repos, keyed by id."""
    out: Dict[str, Dict[str, Any]] = {
        DEFAULT_REPO_ID: {"id": DEFAULT_REPO_ID, "label": "Vera",
                          "path": str(_repo_root()), "remote_url": "",
                          "test_cmd": "", "protected": True, "created_at": ""},
    }
    r = _redis()
    if r:
        try:
            rows = await r.hgetall(KEY_REPOS)
            for k, v in (rows or {}).items():
                rid = k.decode() if isinstance(k, bytes) else k
                if rid == DEFAULT_REPO_ID:
                    continue
                try:
                    out[rid] = json.loads(v.decode() if isinstance(v, bytes) else v)
                except Exception:
                    continue
        except Exception:
            pass
    return out


async def _resolve_repo_root(repo: str) -> Path:
    """repo id -> its filesystem root. '' / 'vera' resolve with NO lookup (the
    zero-risk default path every existing caller already takes). Anything else
    must be registered via evolve.repo.add; an unknown/stale id falls back to
    the Vera repo root rather than raising, so a typo degrades to the safe
    default instead of throwing deep inside a background pipeline worker."""
    if not repo or repo == DEFAULT_REPO_ID:
        return _repo_root()
    repos = await _get_repos()
    rec = repos.get(repo)
    return Path(rec["path"]) if rec else _repo_root()


@capability("evolve.repo.add", memory="off",
            http_method="POST", http_path="/evolve/repo/add", http_tags=["evolve"],
            description="Register a git repo Loop Lab can branch/worktree/pipeline/"
                        "promote/rollback, in addition to Vera's own repo. Inputs: "
                        "id (str! — short slug), label (str), path (str! — absolute "
                        "local path to an existing git repo), remote_url (str, "
                        "optional), test_cmd (str, optional — shell command run in "
                        "the worktree to gate a code pipeline for this repo; default "
                        "'" + DEFAULT_TEST_CMD + "'). Output: {ok, repo}.")
async def evolve_repo_add(id: str = "", label: str = "", path: str = "",
                          remote_url: str = "", test_cmd: str = "", trace_id=None):
    rid = re.sub(r"[^a-z0-9_-]+", "-", (id or "").strip().lower())
    if not rid:
        return {"error": "id required"}
    if rid == DEFAULT_REPO_ID:
        return {"error": f"'{DEFAULT_REPO_ID}' is reserved for the Vera repo itself"}
    p = Path(path or "")
    if not p.is_absolute() or not p.is_dir():
        return {"error": f"path must be an existing absolute directory: {path}"}
    if not (p / ".git").exists():
        return {"error": f"not a git repo (no .git): {path}"}
    rec = {"id": rid, "label": (label or rid).strip(), "path": str(p),
          "remote_url": (remote_url or "").strip(),
          "test_cmd": (test_cmd or DEFAULT_TEST_CMD).strip(),
          "protected": False, "created_at": now_iso()}
    r = _redis()
    if r:
        await r.hset(KEY_REPOS, rid, json.dumps(rec))
    await _audit("repo.add", f"registered repo {rid} ({p})", repo=rid)
    await emit_event({"type": "evolve.repo.added", "id": rid})
    return {"ok": True, "repo": rec}


@capability("evolve.repo.list", memory="off", silent=True,
            http_method="GET", http_path="/evolve/repo/list", http_tags=["evolve"],
            description="Repos Loop Lab can manage — 'vera' plus any registered "
                        "via evolve.repo.add.")
async def evolve_repo_list(trace_id=None):
    repos = await _get_repos()
    return {"repos": list(repos.values())}


@capability("evolve.repo.get", memory="off", silent=True,
            http_method="GET", http_path="/evolve/repo/get", http_tags=["evolve"],
            description="One registered repo's record. Input: id (str!).")
async def evolve_repo_get(id: str = "", trace_id=None):
    if not id:
        return {"error": "id required"}
    repos = await _get_repos()
    rec = repos.get(id)
    if not rec:
        return {"error": f"repo not registered: {id}"}
    return {"repo": rec}


@capability("evolve.repo.gitea_push", memory="off",
            http_method="POST", http_path="/evolve/repo/gitea_push", http_tags=["evolve"],
            description="Provision a Gitea remote for an already-registered repo "
                        "and push its current branch to it: creates the Gitea repo "
                        "if it doesn't exist yet (idempotent), then `git push`. "
                        "Uses the server's configured GITEA_BASE_URL/TOKEN/OWNER — "
                        "the token never leaves the server. No-op error if Gitea "
                        "isn't configured. Input: id (str! — a repo registered via "
                        "evolve.repo.add), branch (str, default the repo's current "
                        "branch). Output: {ok, remote_url}.")
async def evolve_repo_gitea_push(id: str = "", branch: str = "", trace_id=None):
    got = await evolve_repo_get(id=id)
    rec = got.get("repo")
    if not rec:
        return {"error": got.get("error") or "repo not registered"}
    try:
        from Vera.vera.config import cfg
    except Exception:
        cfg = None
    base = (getattr(cfg, "GITEA_BASE_URL", "") or "").rstrip("/") if cfg else ""
    token = getattr(cfg, "GITEA_TOKEN", "") if cfg else ""
    owner = getattr(cfg, "GITEA_OWNER", "") if cfg else ""
    if not (base and token and owner):
        return {"error": "Gitea not configured (GITEA_BASE_URL/GITEA_TOKEN/GITEA_OWNER)"}
    repo_name = re.sub(r"[^A-Za-z0-9_.-]", "-", id) or id
    import httpx
    from urllib.parse import urlparse
    headers = {"Authorization": f"token {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            await c.post(f"{base}/api/v1/orgs/{owner}/repos", headers=headers,
                        json={"name": repo_name, "auto_init": False, "private": True})
            r = await c.post(f"{base}/api/v1/user/repos", headers=headers,
                             json={"name": repo_name, "auto_init": False, "private": True})
        clone_url = f"{base}/{owner}/{repo_name}.git"
        if r.status_code in (200, 201):
            clone_url = r.json().get("clone_url", clone_url)
    except Exception as e:
        return {"error": f"gitea repo create failed: {e}"}
    root = Path(rec["path"])
    parsed = urlparse(clone_url)
    push_url = parsed._replace(netloc=f"oauth2:{token}@{parsed.netloc}").geturl()
    branch = branch or (await _default_branch(repo_root=root))
    await _git("remote", "remove", "origin", repo_root=root)
    await _git("remote", "add", "origin", push_url, repo_root=root)
    pu = await _git("push", "-u", "origin", branch, repo_root=root, timeout=120)
    if not pu["ok"]:
        return {"error": f"git push failed: {pu['err']}"}
    rec["remote_url"] = clone_url
    r2 = _redis()
    if r2:
        await r2.hset(KEY_REPOS, id, json.dumps(rec))
    await _audit("repo.gitea_push", f"{id} → {clone_url}", repo=id)
    return {"ok": True, "remote_url": clone_url}


@capability("evolve.repo.remove", memory="off",
            http_method="POST", http_path="/evolve/repo/remove", http_tags=["evolve"],
            description="Unregister a repo (does not delete anything on disk — "
                        "just stops Loop Lab tracking it). Refuses the protected "
                        "'vera' entry. Input: id (str!).")
async def evolve_repo_remove(id: str = "", trace_id=None):
    if not id:
        return {"error": "id required"}
    if id == DEFAULT_REPO_ID:
        return {"error": f"'{DEFAULT_REPO_ID}' is protected and cannot be removed"}
    r = _redis()
    if r:
        await r.hdel(KEY_REPOS, id)
    await _audit("repo.remove", f"unregistered repo {id}", repo=id)
    await emit_event({"type": "evolve.repo.removed", "id": id})
    return {"ok": True}


async def _git(*args: str, timeout: int = 60,
               repo_root: Optional[Path] = None) -> Dict[str, Any]:
    """Run a git command in a repo root, off the event loop. Defaults to the
    Vera repo — pass repo_root to target a different registered repo."""
    root = repo_root or _repo_root()
    def _run():
        try:
            p = subprocess.run(["git", *args], cwd=str(root),
                               capture_output=True, text=True, timeout=timeout)
            return {"ok": p.returncode == 0, "out": p.stdout.strip(),
                    "err": p.stderr.strip(), "code": p.returncode}
        except FileNotFoundError:
            return {"ok": False, "out": "", "err": "git not found in PATH", "code": -1}
        except subprocess.TimeoutExpired:
            return {"ok": False, "out": "", "err": "git timed out", "code": -1}
        except Exception as e:
            return {"ok": False, "out": "", "err": str(e), "code": -1}
    return await asyncio.get_event_loop().run_in_executor(None, _run)


async def _default_branch(repo_root: Optional[Path] = None) -> str:
    """The repo's mainline branch — what Loop Lab should test against unless the
    user deliberately picks a branch. Asks git rather than assuming 'main'."""
    for args in (("symbolic-ref", "--short", "refs/remotes/origin/HEAD"),
                 ("config", "--get", "init.defaultBranch")):
        r = await _git(*args, repo_root=repo_root)
        if r.get("ok"):
            nm = (r.get("out") or "").strip().rsplit("/", 1)[-1]
            if nm:
                return nm
    for cand in ("main", "master"):
        r = await _git("rev-parse", "--verify", cand, repo_root=repo_root)
        if r.get("ok"):
            return cand
    return "main"


async def _default_pipeline_base(repo_root: Optional[Path] = None) -> str:
    """Where new Loop Lab branches fork from, and where evolve.pipeline.promote
    merges back to, by default (2026-08-16 bleeding-edge-trunk-workflow):
    'bleeding-edge' — the staging trunk — when it exists for this repo,
    otherwise the repo's real mainline (_default_branch). Explicit base=/to=
    callers bypass this entirely; promoting bleeding-edge itself to the real
    mainline is a separate, deliberate action
    (evolve.bleeding_edge.promote_to_main), never automatic."""
    root = repo_root or _repo_root()
    have = await _git("rev-parse", "--verify", f"refs/heads/{BLEEDING_EDGE_BRANCH}",
                      repo_root=root)
    if have["ok"]:
        return BLEEDING_EDGE_BRANCH
    return await _default_branch(repo_root=root)


async def _refresh_worktree(wt_abs: str, branch: str) -> Dict[str, Any]:
    """Fast-forward an EXISTING worktree to the latest commit on `branch`.

    Without this, a worktree created once is frozen forever: _ensure_worktree
    short-circuits on any healthy directory, so Loop Lab kept testing whatever
    the mainline looked like the first time the sandbox came up. Only ever a
    fast-forward — if the worktree has local commits or a dirty tree we leave it
    alone and say so, rather than throwing away work."""
    st = await _sh(_git_wt_argv(wt_abs, "status", "--porcelain"), cwd=wt_abs)
    if (st.get("out") or "").strip():
        return {"ok": False, "reason": "worktree has uncommitted changes — not refreshed"}
    await _sh(_git_wt_argv(wt_abs, "fetch", "--quiet", "origin", branch), cwd=wt_abs)
    ff = await _sh(_git_wt_argv(wt_abs, "merge", "--ff-only", f"origin/{branch}"), cwd=wt_abs)
    if not ff.get("ok"):
        # No remote / diverged: a local-only repo is normal here, not an error.
        return {"ok": False, "reason": (ff.get("err") or "not fast-forwardable")[:160]}
    head = await _sh(_git_wt_argv(wt_abs, "rev-parse", "--short", "HEAD"), cwd=wt_abs)
    return {"ok": True, "head": (head.get("out") or "").strip()}


# 2026-08-15 chat-send-latency / infra-audit follow-up: "sandbox.up with no
# branch" used to resolve to _default_branch() — literally "main" — and try to
# git-worktree it. Git refuses that unconditionally: a branch can only be
# checked out in ONE place at a time, and the primary repo checkout already
# holds main. Every "reset the sandbox to mainline" call either hung or failed
# outright with "already exists"/"already checked out" for exactly this
# reason — not a stale-directory bug (_ensure_worktree's self-heal handles
# those fine), a structural one: the request was asking git to do something
# it cannot do, no matter how many times you retry or clean up first.
#
# Fix: a dedicated, LOCAL-ONLY branch that mirrors mainline and is what
# "sandbox.up with no branch" actually targets. Nothing is ever committed
# onto it directly and it is never pushed anywhere — it exists solely so a
# second worktree has a legal branch name to hold. Kept fresh two ways: a
# scheduled nightly fast-forward (see _scheduled_mainline_mirror_refresh
# below), and an opportunistic refresh inline in evolve_sandbox_up itself so
# a sandbox brought up between nightly runs still gets today's mainline.
MAINLINE_MIRROR_BRANCH = "loop-lab/mainline-mirror"


async def _refresh_loop_lab_mirror(mirror_branch: str, base: str,
                                   repo_root: Optional[Path] = None) -> Dict[str, Any]:
    """Create (first run) or fast-forward `mirror_branch` to `base`'s current
    tip. Shared implementation behind _refresh_mainline_mirror (base=main) and
    _refresh_bleeding_edge_mirror (base=bleeding-edge) — see MAINLINE_MIRROR_BRANCH
    for why a mirror branch exists at all. Safe whether or not the mirror
    currently has a live worktree — sourced from LOCAL `base` (not a remote),
    since a mirror branch is never pushed. If a worktree has it checked out,
    the fast-forward happens IN that worktree (ref + files move together,
    exactly like _refresh_worktree); otherwise it's a plain ref move, safe
    because nothing has the branch checked out to conflict with."""
    root = repo_root or _repo_root()
    have = await _git("rev-parse", "--verify", f"refs/heads/{mirror_branch}",
                      repo_root=root)
    if not have["ok"]:
        cr = await _git("branch", mirror_branch, base, repo_root=root)
        if not cr["ok"]:
            return {"error": f"mirror branch create failed: {cr['err'] or cr['out']}"}
        return {"ok": True, "action": "created", "base": base}
    wt_abs = root / _WORKTREE_DIR / _safe_branch(mirror_branch)
    if wt_abs.exists() and (wt_abs / ".git").exists():
        st = await _sh(_git_wt_argv(str(wt_abs), "status", "--porcelain"), cwd=str(wt_abs))
        if (st.get("out") or "").strip():
            # Should never happen — nothing is meant to commit onto the mirror
            # directly — but if it does, don't silently discard it.
            return {"ok": False,
                    "reason": "mirror worktree has uncommitted changes — not refreshed"}
        ff = await _sh(_git_wt_argv(str(wt_abs), "merge", "--ff-only", base), cwd=str(wt_abs))
        if not ff.get("ok"):
            return {"ok": False, "reason": (ff.get("err") or "not fast-forwardable")[:200]}
        head = await _sh(_git_wt_argv(str(wt_abs), "rev-parse", "--short", "HEAD"),
                         cwd=str(wt_abs))
        return {"ok": True, "action": "fast-forwarded worktree",
                "head": (head.get("out") or "").strip()}
    mv = await _git("branch", "-f", mirror_branch, base, repo_root=root)
    if not mv["ok"]:
        return {"error": f"mirror branch fast-forward failed: {mv['err'] or mv['out']}"}
    return {"ok": True, "action": "fast-forwarded ref (no live worktree)", "base": base}


async def _refresh_mainline_mirror(repo_root: Optional[Path] = None) -> Dict[str, Any]:
    root = repo_root or _repo_root()
    base = await _default_branch(repo_root=root)  # the REAL mainline, e.g. "main"
    return await _refresh_loop_lab_mirror(MAINLINE_MIRROR_BRANCH, base, repo_root=root)


# 2026-08-16 bleeding-edge-trunk-workflow: bleeding-edge is now the default
# fork/promote target (see _default_pipeline_base, evolve.pipeline.promote).
# Once the standing bleeding-edge container (below) permanently holds a
# worktree, "bleeding-edge" is encumbered the same way "main" always was —
# hence its own mirror, same reasoning as MAINLINE_MIRROR_BRANCH above.
BLEEDING_EDGE_BRANCH = "bleeding-edge"
BLEEDING_EDGE_MIRROR_BRANCH = "loop-lab/bleeding-edge-mirror"


async def _refresh_bleeding_edge_mirror(repo_root: Optional[Path] = None) -> Dict[str, Any]:
    root = repo_root or _repo_root()
    have_be = await _git("rev-parse", "--verify", f"refs/heads/{BLEEDING_EDGE_BRANCH}",
                         repo_root=root)
    if not have_be["ok"]:
        return {"error": f"'{BLEEDING_EDGE_BRANCH}' branch does not exist in this repo"}
    return await _refresh_loop_lab_mirror(BLEEDING_EDGE_MIRROR_BRANCH, BLEEDING_EDGE_BRANCH,
                                          repo_root=root)


_MAINLINE_MIRROR_REFRESH_INTERVAL_S = int(
    os.getenv("VERA_MAINLINE_MIRROR_REFRESH_INTERVAL_S", str(24 * 3600)))


async def _scheduled_mainline_mirror_refresh() -> None:
    try:
        res = await _refresh_mainline_mirror()
        if res.get("ok"):
            log.info("evolve: mainline mirror refreshed (%s)", res.get("action"))
        elif res.get("error"):
            log.warning("evolve: mainline mirror refresh failed: %s", res["error"])
    except Exception as e:
        log.debug("mainline mirror refresh: %s", e)
    try:
        res2 = await _refresh_bleeding_edge_mirror()
        if res2.get("ok"):
            log.info("evolve: bleeding-edge mirror refreshed (%s)", res2.get("action"))
        elif res2.get("error"):
            log.debug("evolve: bleeding-edge mirror refresh skipped: %s", res2["error"])
    except Exception as e:
        log.debug("bleeding-edge mirror refresh: %s", e)


schedule(_scheduled_mainline_mirror_refresh, _MAINLINE_MIRROR_REFRESH_INTERVAL_S,
         name="evolve.mainline_mirror.refresh", skip_in_sandbox=True)


async def _ensure_worktree(branch: str, repo_root: Optional[Path] = None) -> Dict[str, Any]:
    """Materialise the branch's WORKTREE at <repo>/.loop-lab-worktrees/<branch>
    WITHOUT ever switching the real working tree — this is the ONLY place a
    loop-lab branch's code exists on disk; all edits/tests happen inside it.
    Idempotent + self-healing: prunes stale registrations, and if the branch is
    (illegally) checked out in the MAIN tree — legacy `checkout -b` state —
    restores prod to main first. repo_root defaults to the Vera repo; pass a
    different registered repo's root to work a branch there instead."""
    root = repo_root or _repo_root()
    wt_abs = root / _WORKTREE_DIR / _safe_branch(branch)
    if wt_abs.exists():
        # A directory that exists is NOT proof of a healthy worktree. A clobbered
        # or hand-copied dir (no .git link, or a registration git has since
        # forgotten) passes .exists() yet git treats it as "not a repository" —
        # and the container bind-mounts it anyway, serving stale/partial source
        # (the exact "1024 caps, missing loops.run" failure this incident chased).
        # Verify it's a REAL worktree; only short-circuit when it genuinely is.
        chk = await _sh(_git_wt_argv(str(wt_abs), "rev-parse",
                                     "--is-inside-work-tree"), cwd=str(wt_abs))
        if (wt_abs / ".git").exists() and chk.get("ok") \
                and (chk.get("out") or "").strip() == "true":
            return {"ok": True, "path": str(wt_abs)}
        # Broken: drop the stale dir + registration, then recreate below.
        await _audit("sandbox.heal",
                     f"{wt_abs} existed but was NOT a valid git worktree — "
                     f"removed + recreating (it was serving a stale mount)")
        try:
            shutil.rmtree(wt_abs, ignore_errors=True)
        except Exception:
            pass
    await _git("worktree", "prune", repo_root=root)
    wt = await _git("worktree", "add", str(wt_abs), branch, timeout=120, repo_root=root)
    if not wt["ok"] and "already checked out" in (wt["err"] or ""):
        cur = await _git("rev-parse", "--abbrev-ref", "HEAD", repo_root=root)
        if (cur.get("out") or "").strip() == branch:
            await _git("checkout", "main", repo_root=root)
            await _audit("sandbox.heal",
                         f"prod working tree was on {branch}; restored to main "
                         f"so the worktree could be created")
            wt = await _git("worktree", "add", str(wt_abs), branch, timeout=120, repo_root=root)
    if not wt["ok"]:
        return {"error": f"git worktree add failed: {wt['err']}"}
    # Mark the fresh worktree safe for the Vera process user's global config, so
    # EVERY later git op there works (sandbox terminal, and any tool that shells
    # git in the worktree) — not just the diff cap, which also guards per-call.
    # git may be created by a different UID; without this it aborts with
    # "detected dubious ownership". Best-effort — the per-call guard is the
    # backstop.
    await _git("config", "--global", "--add", "safe.directory", str(wt_abs))
    return {"ok": True, "path": str(wt_abs)}


async def _repo_test_gate(cwd: str, test_cmd: str, timeout: int = 300) -> Dict[str, Any]:
    """Run a generic repo's own test command in `cwd` (its root, for a baseline,
    or a branch worktree, for a candidate) and reduce the result to a 0.0-1.0
    pass_rate — the same shape evolve_suite_run's avg_combined score already
    fills for Vera loop-profile pipelines, so the pipeline record's existing
    baseline_score/candidate_score/gate_delta/gate_passed fields (and the
    panel's generic rendering of them) need no changes to carry this too."""
    try:
        cmd = shlex.split(test_cmd)
    except ValueError as e:
        return {"error": f"invalid test_cmd: {e}"}
    if not cmd:
        return {"error": "empty test_cmd"}
    res = await _sh(cmd, cwd=cwd, timeout=timeout)
    passed = bool(res.get("ok"))
    out = (res.get("out", "") + "\n" + res.get("err", "")).strip()
    return {"ok": True, "pass_rate": 1.0 if passed else 0.0, "passed": passed,
            "rc": res.get("code"),
            "summary": ("tests passed" if passed else f"tests failed (rc {res.get('code')})"),
            "output": out[-4000:]}


@capability("evolve.git.status", memory="off", silent=True,
            http_method="GET", http_path="/evolve/git/status", http_tags=["evolve"],
            description="Git state of a repo for CI/CD: current branch, dirty "
                        "flag, and the loop-lab/* branches. Input: repo (str, "
                        "default 'vera' — id of a repo registered via "
                        "evolve.repo.add, or 'vera' for Vera's own repo). "
                        "Output: {branch, dirty, dirty_files, branches:[...], repo}.")
async def evolve_git_status(repo: str = DEFAULT_REPO_ID, trace_id=None):
    root = await _resolve_repo_root(repo)
    br = await _git("rev-parse", "--abbrev-ref", "HEAD", repo_root=root)
    st = await _git("status", "--porcelain", repo_root=root)
    ls = await _git("branch", "--list", f"{BRANCH_PREFIX}*", "--format=%(refname:short)",
                    repo_root=root)
    if not br["ok"]:
        return {"error": br["err"] or "not a git repo", "repo": str(root)}
    dirty_files = [l.strip() for l in (st["out"] or "").splitlines() if l.strip()]
    branches = [l.strip() for l in (ls["out"] or "").splitlines() if l.strip()]
    return {"repo": str(root), "branch": br["out"],
            "dirty": bool(dirty_files), "dirty_files": dirty_files[:50],
            "branches": branches}


_GRAPH_FIELD_SEP = "\x1f"
_GRAPH_RECORD_SEP = "\x1e"


async def _commit_attribution_map(limit: int = 500) -> Dict[str, Dict[str, Any]]:
    """short-SHA → {controller, session_id, via, pipeline_id} from pipeline
    records, so the commit DAG can attribute each commit to the session/agent
    that produced it (and drill into its chat). Keyed by the short SHAs each
    pipeline stores in its `commits` list (the branch's authored commits)."""
    out: Dict[str, Dict[str, Any]] = {}
    r = _redis()
    if not r:
        return out
    # The compact list records (KEY_PIPELINES) omit `commits`; the FULL record
    # (KEY_PIPELINE + id) carries them. Read the ids from the list, then fetch
    # each full record.
    try:
        rows = await r.lrange(KEY_PIPELINES, 0, max(0, int(limit) - 1))
    except Exception:
        return out
    for row in rows or []:
        try:
            head = json.loads(row.decode() if isinstance(row, (bytes, bytearray)) else row)
            pid = head.get("id")
            if not pid:
                continue
            full = await r.get(KEY_PIPELINE + pid)
            rec = json.loads(full.decode() if isinstance(full, (bytes, bytearray)) else full) if full else head
        except Exception:
            continue
        attr = {"controller": rec.get("controller") or "",
                "session_id": rec.get("session_id") or "",
                "via": rec.get("via") or "",
                "pipeline_id": rec.get("id") or ""}
        if not (attr["controller"] or attr["session_id"]):
            continue
        for c in rec.get("commits") or []:
            sha = (str(c).split(" ", 1)[0] or "").strip()
            if sha and sha not in out:
                out[sha] = attr
    return out


@capability("evolve.git.graph", memory="off", silent=True,
            http_method="GET", http_path="/evolve/git/graph", http_tags=["evolve"],
            description="Real commit graph for a repo — every commit (across ALL "
                        "branches) with its parents and ref decorations, newest-"
                        "first (git's natural log order — what a standard gitk-"
                        "style lane-assignment walk expects). Inputs: repo (str, "
                        "default 'vera'), limit (int default 150 — most recent N "
                        "commits). Output: {ok, repo, commits: [{hash, "
                        "parents:[...], author, date, refs:[...], subject}]}.")
async def evolve_git_graph(repo: str = DEFAULT_REPO_ID, limit: int = 150, trace_id=None):
    root = await _resolve_repo_root(repo)
    fmt = _GRAPH_FIELD_SEP.join(["%H", "%P", "%an", "%ad", "%D", "%s"]) + _GRAPH_RECORD_SEP
    r = await _git("log", "--all", "--topo-order", "-n", str(max(1, int(limit or 150))),
                   "--date=iso-strict", f"--format={fmt}", repo_root=root, timeout=30)
    if not r["ok"]:
        return {"error": r["err"] or "git log failed", "repo": str(root)}
    commits = []
    for rec in (r["out"] or "").split(_GRAPH_RECORD_SEP):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        parts = rec.split(_GRAPH_FIELD_SEP)
        if len(parts) < 6:
            continue
        h, p, an, ad, refs, subj = parts[:6]
        commits.append({
            "hash": h, "parents": [x for x in p.split(" ") if x],
            "author": an, "date": ad,
            "refs": [x.strip() for x in refs.split(",") if x.strip()],
            "subject": subj,
        })
    # Stamp per-commit attribution (which session/agent produced it) so the DAG
    # is chat-drillable — from a commit node into the driving Claude/Vera session.
    amap = await _commit_attribution_map()
    if amap:
        for c in commits:
            hh = c.get("hash") or ""
            for sha, attr in amap.items():
                if hh.startswith(sha):
                    c["attribution"] = attr
                    break
    return {"ok": True, "repo": str(root), "commits": commits}


@capability("evolve.branch.create", memory="off",
            http_method="POST", http_path="/evolve/branch/create", http_tags=["evolve"],
            description="Create (and check out) a loop-lab work branch from the "
                        "current HEAD. Inputs: name (str — suffix; auto if blank), "
                        "base (str — base ref, default current branch), repo (str, "
                        "default 'vera' — id of a repo registered via "
                        "evolve.repo.add). Output: {ok, branch}.")
async def evolve_branch_create(name: str = "", base: str = "",
                               repo: str = DEFAULT_REPO_ID, trace_id=None):
    root = await _resolve_repo_root(repo)
    suffix = re.sub(r"[^a-z0-9._-]+", "-", (name or uuid.uuid4().hex[:8]).lower())
    branch = BRANCH_PREFIX + suffix
    # NEVER checkout on the real working tree — prod's source stays untouched.
    # `git branch <name> <base>` creates the ref without switching anything; the
    # branch's code is only ever materialised in the sandbox WORKTREE. (The old
    # `checkout -b` also left the branch checked out in the main tree, which
    # made the later `git worktree add` fail with "already checked out".)
    r = await _git("branch", branch, base or "HEAD", repo_root=root)
    if not r["ok"] and "already exists" not in (r["err"] or ""):
        return {"error": r["err"]}
    await _audit("branch.create", branch, branch=branch, repo=repo)
    await emit_event({"type": "evolve.branch.created", "branch": branch, "repo": repo})
    return {"ok": True, "branch": branch}


@capability("evolve.branch.delete", memory="off",
            http_method="POST", http_path="/evolve/branch/delete", http_tags=["evolve"],
            description="Delete a loop-lab work branch (the rollback primitive). "
                        "Checks out `to` first (default main). Inputs: branch "
                        "(str!), to (str, default main), force (bool default True), "
                        "repo (str, default 'vera' — id of a repo registered via "
                        "evolve.repo.add).")
async def evolve_branch_delete(branch: str = "", to: str = "main",
                               force: bool = True, repo: str = DEFAULT_REPO_ID,
                               trace_id=None):
    if not branch.startswith(BRANCH_PREFIX):
        return {"error": f"refuses to delete non loop-lab branch: {branch}"}
    root = await _resolve_repo_root(repo)
    # Never checkout on the real working tree. If the branch is only checked out
    # in a loop-lab WORKTREE, remove that worktree first so the delete succeeds.
    wt_abs = root / _WORKTREE_DIR / _safe_branch(branch)
    if wt_abs.exists():
        await _git("worktree", "remove", "--force", str(wt_abs), timeout=120, repo_root=root)
    await _git("worktree", "prune", repo_root=root)
    r = await _git("branch", "-D" if force else "-d", branch, repo_root=root)
    if not r["ok"]:
        return {"error": r["err"]}
    await _audit("branch.delete", f"{branch} (rollback — change discarded)",
                 branch=branch, repo=repo)
    await emit_event({"type": "evolve.branch.deleted", "branch": branch, "repo": repo})
    return {"ok": True, "deleted": branch}


async def _save_pipeline(rec: Dict[str, Any]):
    r = _redis()
    if not r:
        return
    try:
        await r.set(KEY_PIPELINE + rec["id"], json.dumps(rec, default=str))
        await r.expire(KEY_PIPELINE + rec["id"], 60 * 86400)
        rows = await r.lrange(KEY_PIPELINES, 0, PIPELINES_CAP - 1)
        compact = {k: rec.get(k) for k in
                   ("id", "kind", "profile", "status", "decision", "created_at",
                    "ended_at", "branch", "variant_id", "baseline_score",
                    "candidate_score", "gate_delta", "gate_passed",
                    "repo", "controller", "review_requested",
                    # attribution — so the CI/CD row shows the adopted badge +
                    # driving session without a full per-row fetch.
                    "adopted", "session_id", "via")}
        for i, row in enumerate(rows or []):
            try:
                if json.loads(row).get("id") == rec["id"]:
                    await r.lset(KEY_PIPELINES, i, json.dumps(compact, default=str))
                    return
            except Exception:
                continue
        await r.lpush(KEY_PIPELINES, json.dumps(compact, default=str))
        await r.ltrim(KEY_PIPELINES, 0, PIPELINES_CAP - 1)
    except Exception as e:
        log.debug("evolve save pipeline: %s", e)


def _pstep(rec: Dict[str, Any], stage: str, ok: bool, detail: str = ""):
    rec.setdefault("steps", []).append(
        {"stage": stage, "ok": bool(ok), "detail": str(detail)[:400], "ts": now_iso()})


async def _pipeline_worker(rec: Dict[str, Any]):
    pid = rec["id"]
    repo = rec.get("repo") or DEFAULT_REPO_ID
    # A code pipeline on a repo other than Vera has no loop profile to run —
    # its gate is that repo's own test_cmd instead (see the 'else: # code'
    # branch below), so the Vera-suite baseline step is skipped for it.
    generic_repo = rec["kind"] == "code" and repo != DEFAULT_REPO_ID
    try:
        cfg = await _get_config()
        profile = rec["profile"]
        threshold = float(rec.get("gate_threshold", 0.0))

        if not generic_repo:
            # ── 1. Baseline: score the profile as it stands now ──────────────
            rec["status"] = "baseline"
            rec["current"] = "measuring baseline"
            await _save_pipeline(rec)
            await emit_event({"type": "evolve.pipeline.stage", "id": pid, "stage": "baseline"})
            base = await evolve_suite_run(profile=profile, assess=True,
                                          provider=rec.get("critic", cfg["critic_provider"]))
            if base.get("error"):
                _pstep(rec, "baseline", False, base["error"])
                rec["status"] = "error"; rec["decision"] = "held"
                raise RuntimeError(base["error"])
            rec["baseline_suite"] = base["suite_id"]
            rec["baseline_score"] = base["avg_combined"]
            _pstep(rec, "baseline", True, f"avg {base['avg_combined']}")
            await _save_pipeline(rec)

        if rec["kind"] == "variant":
            # ── 2v. Apply candidate variant as a temporary overlay ───────────
            vid = rec.get("variant_id")
            v = await _get_variant(profile, vid)
            if not v:
                _pstep(rec, "apply", False, f"variant {vid} not found")
                raise RuntimeError("variant not found")
            rec["status"] = "testing"; rec["current"] = "testing candidate variant"
            await _save_pipeline(rec)
            await emit_event({"type": "evolve.pipeline.stage", "id": pid, "stage": "test"})
            cand = await evolve_suite_run(profile=profile, assess=True,
                                          provider=rec.get("critic", cfg["critic_provider"]),
                                          variant_id=vid)
            if cand.get("error"):
                _pstep(rec, "test", False, cand["error"])
                raise RuntimeError(cand["error"])
            rec["candidate_suite"] = cand["suite_id"]
            rec["candidate_score"] = cand["avg_combined"]
            _pstep(rec, "test", True, f"avg {cand['avg_combined']}")

        else:  # code
            root = await _resolve_repo_root(repo)
            # ── 2c. Cut a branch and hand the edit to Claude Code ────────────
            rec["status"] = "branching"; rec["current"] = "creating work branch"
            await _save_pipeline(rec)
            bc = await evolve_branch_create(name=pid, repo=repo)
            if bc.get("error"):
                _pstep(rec, "branch", False, bc["error"])
                raise RuntimeError(bc["error"])
            rec["branch"] = bc["branch"]
            _pstep(rec, "branch", True, bc["branch"])
            # Materialise the branch's WORKTREE and pin every queued edit to it
            # (workdir override) — the editor works ONLY on the containerised /
            # worktree copy of the source, NEVER the real working tree.
            wt = await _ensure_worktree(rec["branch"], repo_root=root)
            if wt.get("error"):
                _pstep(rec, "worktree", False, wt["error"])
                raise RuntimeError(wt["error"])
            rec["worktree"] = wt["path"]
            _pstep(rec, "worktree", True, wt["path"])

            if generic_repo:
                # Baseline for a generic repo: run ITS OWN test_cmd against the
                # repo root as it stands now (main — untouched, safe to read).
                # This has to happen before the edit is queued (async — may not
                # land for a while), so the delta computed once the branch is
                # tested (evolve.pipeline.test, after the edit lands) is real.
                repo_rec = (await evolve_repo_get(id=repo)).get("repo") or {}
                test_cmd = repo_rec.get("test_cmd") or DEFAULT_TEST_CMD
                rec["test_cmd"] = test_cmd
                rec["status"] = "baseline"
                rec["current"] = f"measuring baseline ({test_cmd})"
                await _save_pipeline(rec)
                await emit_event({"type": "evolve.pipeline.stage", "id": pid, "stage": "baseline"})
                base = await _repo_test_gate(str(root), test_cmd)
                if base.get("error"):
                    _pstep(rec, "baseline", False, base["error"])
                    rec["status"] = "error"; rec["decision"] = "held"
                    raise RuntimeError(base["error"])
                rec["baseline_score"] = base["pass_rate"]
                _pstep(rec, "baseline", True, base["summary"])
                await _save_pipeline(rec)

            for cs in (rec.get("edits") or [])[:5]:
                await _call("ide.remote.queue.add",
                            task=(f"[Loop Lab CI · branch {rec['branch']}] "
                                  f"Improve the Vera agent engine for profile "
                                  f"'{profile}'.\nArea: {cs.get('area')}\n"
                                  f"Change: {cs.get('suggestion')}\n"
                                  f"You are in a git WORKTREE for branch "
                                  f"{rec['branch']} — work ONLY here. Commit to "
                                  f"this branch with a clear message. Apply a "
                                  "minimal, safe change; run any nearby tests. "
                                  "Never touch main or the primary checkout."),
                            engine="claude", source="dream", priority=5,
                            workdir=wt["path"])
            _pstep(rec, "edit_queued", True,
                   f"{len(rec.get('edits') or [])} edit(s) queued to Claude Code "
                   f"(worktree-pinned)")
            await _save_pipeline(rec)

            # Code changes need the change actually applied + reloaded to test.
            # If auto_test is on AND the dev sandbox is running THIS branch, run
            # the suite through the sandbox (branch code) and gate on it. Merge
            # to main is ALWAYS manual for code (evolve.pipeline.promote) — we
            # never auto-merge source. Otherwise we hold for review. The dev
            # sandbox is Vera-app-specific — a generic repo never takes this
            # path (its retest is evolve.pipeline.test once the edit lands).
            sb = None if generic_repo else await _get_sandbox()
            sandbox_on_branch = bool(sb) and sb.get("branch") == rec["branch"] \
                and (await _sandbox_probe()).get("reachable")
            if rec.get("auto_test") and sandbox_on_branch:
                rec["status"] = "testing"
                rec["current"] = "testing branch on dev sandbox"
                await _save_pipeline(rec)
                await emit_event({"type": "evolve.pipeline.stage", "id": pid,
                                  "stage": "sandbox_test"})
                cand = await _sandbox_suite(rec["branch"], profile,
                                            rec.get("critic", cfg["critic_provider"]))
                if cand.get("error"):
                    _pstep(rec, "sandbox_test", False, cand["error"])
                    rec["status"] = "awaiting_edit"
                    rec["decision"] = "pending"
                    rec["current"] = ("sandbox test could not run — apply the edit "
                                      "and re-run, or promote/rollback manually")
                    await _save_pipeline(rec)
                    await emit_event({"type": "evolve.pipeline.done", "id": pid,
                                      "decision": "pending", "kind": "code"})
                    return
                rec["candidate_suite"] = cand.get("suite_id")
                rec["candidate_score"] = cand.get("avg_combined")
                delta = round((rec.get("candidate_score") or 0)
                              - (rec.get("baseline_score") or 0), 2)
                rec["gate_delta"] = delta
                passed = delta >= threshold
                rec["gate_passed"] = passed
                _pstep(rec, "sandbox_test", True, f"branch avg {cand.get('avg_combined')}")
                _pstep(rec, "gate", passed, f"Δ {delta:+} vs {threshold} — "
                       f"{'PASS' if passed else 'FAIL'}")
                rec["status"] = "tested"
                rec["decision"] = "pending"  # merge stays manual
                rec["current"] = ("branch tested — "
                                  + ("gate PASSED, ready to promote (merge)"
                                     if passed else "gate FAILED, recommend rollback"))
                await _save_pipeline(rec)
                await emit_event({"type": "evolve.pipeline.done", "id": pid,
                                  "decision": "pending", "kind": "code",
                                  "gate_passed": passed})
                return

            rec["status"] = "awaiting_edit"
            rec["current"] = (
                "edit queued to Claude Code. Once it's landed on the branch, "
                "call evolve.pipeline.test to gate it, then promote (merge) or "
                "rollback (delete branch)." if generic_repo else
                "edit queued to Claude Code. Bring up the dev sandbox "
                "on this branch (Sandbox tab) to test it, then "
                "promote (merge) or rollback (delete branch).")
            rec["decision"] = "pending"
            _pstep(rec, "hold", True, "code change awaiting Claude edit + test")
            await _save_pipeline(rec)
            await emit_event({"type": "evolve.pipeline.done", "id": pid,
                              "decision": "pending", "kind": "code"})
            return

        # ── 3. Gate + auto-promote (variant kind) ────────────────────────────
        delta = round((rec.get("candidate_score") or 0) - (rec.get("baseline_score") or 0), 2)
        rec["gate_delta"] = delta
        passed = delta >= threshold and (rec.get("candidate_score") or 0) >= (rec.get("baseline_score") or 0)
        rec["gate_passed"] = passed
        _pstep(rec, "gate", passed,
               f"Δ {delta:+} vs threshold {threshold} — {'PASS' if passed else 'FAIL'}")
        await emit_event({"type": "evolve.pipeline.gate", "id": pid,
                          "passed": passed, "delta": delta})

        if passed and rec.get("auto_promote", True):
            await evolve_variant_promote(profile=profile, variant_id=rec["variant_id"])
            rec["decision"] = "promoted"
            _pstep(rec, "promote", True, f"overlay ← {rec['variant_id']}")
        else:
            rec["decision"] = "held" if not passed else "pending"
            _pstep(rec, "promote", passed,
                   "gate failed — variant NOT promoted" if not passed
                   else "gate passed — awaiting manual promote")
        rec["status"] = "done"
    except Exception as e:
        log.warning("evolve pipeline %s: %s", pid, e)
        rec["status"] = "error"
        rec["error"] = str(e)[:400]
        rec.setdefault("decision", "held")
    finally:
        rec["ended_at"] = now_iso()
        rec["current"] = ""
        _PIPELINE_TASKS.pop(pid, None)
        await _save_pipeline(rec)
        await emit_event({"type": "evolve.pipeline.done", "id": pid,
                          "decision": rec.get("decision"),
                          "status": rec.get("status")})


_PIPELINE_TASKS: Dict[str, asyncio.Task] = {}


@capability("evolve.pipeline.run", memory="on",
            http_method="POST", http_path="/evolve/pipeline/run", http_tags=["evolve"],
            description="Run a CI/CD pipeline for a code/loop change: baseline → "
                        "apply → test → gate → promote/hold, with git branches for "
                        "rollback. Inputs: kind (variant|code), profile (str! — a "
                        "loop profile; for kind=code on a non-vera repo this is "
                        "just a label, no loop profile is exercised), variant_id "
                        "(str — for kind=variant), edits (list [{area,suggestion}] "
                        "— for kind=code), gate_threshold (float, default 0.0 = "
                        "must not regress), auto_promote (bool default True), "
                        "critic (str provider), repo (str, default 'vera' — id of "
                        "a repo registered via evolve.repo.add; for kind=code on a "
                        "non-vera repo the gate runs that repo's own test_cmd "
                        "instead of a Vera loop-profile suite). Runs in the "
                        "background — poll evolve.pipeline.get. Output: {ok, id}.",
            schema=enum_schema(kind=["variant", "code"]))
async def evolve_pipeline_run(kind: str = "variant", profile: str = "",
                              variant_id: str = "", edits: Optional[List[Dict[str, Any]]] = None,
                              gate_threshold: float = 0.0, auto_promote: bool = True,
                              auto_test: bool = True, critic: str = "",
                              repo: str = DEFAULT_REPO_ID, session_id: str = "", trace_id=None):
    cfg = await _get_config()
    profile = (profile or cfg["default_profile"]).strip()
    if kind == "variant" and not variant_id:
        return {"error": "variant_id required for kind=variant"}
    if kind == "code" and not edits:
        return {"error": "edits required for kind=code"}
    if repo != DEFAULT_REPO_ID and not (await evolve_repo_get(id=repo)).get("repo"):
        return {"error": f"repo not registered: {repo}"}
    rec = {
        "id": uuid.uuid4().hex[:8], "kind": kind, "profile": profile,
        "variant_id": variant_id, "edits": edits or [],
        "gate_threshold": float(gate_threshold), "auto_promote": bool(auto_promote),
        "auto_test": bool(auto_test),
        "critic": (critic or cfg["critic_provider"]).strip(),
        "repo": repo, "controller": _triggered_by(),
        "session_id": (session_id or "").strip(), "via": (CALLER_KIND.get() or ""),
        "status": "starting", "decision": "pending", "current": "",
        "created_at": now_iso(), "ended_at": "", "steps": [],
        "baseline_score": None, "candidate_score": None,
        "gate_delta": None, "gate_passed": None, "branch": "",
        "reviews": [], "review_requested": False,
    }
    await _save_pipeline(rec)
    await _audit("pipeline.run", f"started {kind} pipeline for {profile}",
                 id=rec["id"], kind=kind, profile=profile)
    _PIPELINE_TASKS[rec["id"]] = asyncio.create_task(_pipeline_worker(rec))
    await emit_event({"type": "evolve.pipeline.started", "id": rec["id"],
                      "kind": kind, "profile": profile})
    return {"ok": True, "id": rec["id"]}


@capability("evolve.pipeline.adopt", memory="on",
            http_method="POST", http_path="/evolve/pipeline/adopt", http_tags=["evolve"],
            description="Register an ALREADY-edited branch as a code pipeline run — for a "
                        "HAND-AUTHORED change (edits made directly in a worktree, e.g. by a "
                        "Claude Code session, NOT dispatched to the edit queue). Creates the "
                        "same pipeline-record shape so the change is tracked + ATTRIBUTED "
                        "(controller = the triggering agent/session) in the CI/CD UI and "
                        "flows through the SAME review + safe promote as a generated change — "
                        "it just skips the code-gen dispatch. Gate has two parts: a fast "
                        "compile/smoke check (ast.parse of the branch's changed .py) and, "
                        "when the branch has a live worktree (evolve.sandbox.up), the "
                        "critical-system regression tier (`pytest -m critical`, tests/conftest.py) "
                        "run in an ISOLATED ephemeral container via evolve.unittest.run — never "
                        "the container serving this request. gate_passed is true only when BOTH "
                        "pass; a docs/UI-only branch (no .py changed) or one with no live worktree "
                        "yet falls back to the compile check alone. Inputs: branch (str!), to "
                        "(str — default bleeding-edge, the staging trunk; 2026-08-16), title "
                        "(str), summary (str), repo (str=vera), authorize_main (str). "
                        "M3.6 GUARDRAIL: adopting toward the real mainline (main/master) is "
                        "REFUSED unless authorize_main is the explicit sentinel — all code lands "
                        "on bleeding-edge; main advances only via evolve.bleeding_edge."
                        "promote_to_main on the user's explicit, unambiguous go-ahead. "
                        "Output: {ok, id, ahead_by, changed_files, gate_passed}.")
async def evolve_pipeline_adopt(branch: str = "", to: str = "bleeding-edge", title: str = "",
                                summary: str = "", repo: str = DEFAULT_REPO_ID,
                                session_id: str = "", authorize_main: str = "", trace_id=None):
    branch = (branch or "").strip()
    if not branch:
        return {"error": "branch required"}
    to = (to or "bleeding-edge").strip()
    if repo != DEFAULT_REPO_ID and not (await evolve_repo_get(id=repo)).get("repo"):
        return {"error": f"repo not registered: {repo}"}
    root = await _resolve_repo_root(repo)
    if not (await _git("rev-parse", "--verify", f"refs/heads/{branch}", repo_root=root))["ok"]:
        return {"error": f"unknown branch: {branch}"}
    if not (await _git("rev-parse", "--verify", f"refs/heads/{to}", repo_root=root))["ok"]:
        return {"error": f"unknown target branch: {to}"}
    # M3.6 main-merge guardrail: refuse adopting a feature branch toward the real
    # mainline unless the caller passes the explicit authorization sentinel. All
    # code lands on bleeding-edge; main advances only on the user's go-ahead via
    # evolve.bleeding_edge.promote_to_main (which does NOT route through here).
    from Vera.vera.evolve.evolve_git_core import main_merge_refusal as _main_merge_refusal  # noqa: E402
    _mrefuse = _main_merge_refusal(to, await _default_branch(repo_root=root), authorize_main)
    if _mrefuse:
        await _audit("pipeline.adopt", f"REFUSED adopt {branch} -> {to}: main-merge guard",
                     kind="code", branch=branch, ok=False, repo=repo)
        return {"error": _mrefuse, "refused": "main-merge-guard"}
    # Commits + changed files this branch adds on top of `to`.
    lg = await _git("log", "--oneline", f"{to}..{branch}", repo_root=root)
    commits = [ln for ln in (lg.get("out", "") or "").splitlines() if ln.strip()]
    if not commits:
        return {"error": f"branch '{branch}' has no commits ahead of '{to}' — nothing to adopt"}
    df = await _git("diff", "--name-only", f"{to}...{branch}", repo_root=root)
    changed = [ln for ln in (df.get("out", "") or "").splitlines() if ln.strip()]

    rec = {
        "id": uuid.uuid4().hex[:8], "kind": "code", "profile": "adopted",
        "variant_id": "",
        "edits": [{"area": (title or branch), "suggestion": (summary or "hand-authored change")}],
        "gate_threshold": 0.0, "auto_promote": False, "auto_test": False,
        "critic": "", "repo": repo, "controller": _triggered_by(), "adopted": True,
        # Link this run to the exact driving session (the Claude Code session
        # UUID for controller=claude_code) so the CI/CD UI can drill from a
        # pipeline/commit into the chat that produced it. Resolves against
        # ide.claude_sessions once that session is ingested (remote/Windows
        # sessions need the deferred SSH ingestion — see §8-Phase-A).
        "session_id": (session_id or "").strip(),
        "via": (CALLER_KIND.get() or ""),
        "to": to, "status": "adopted", "decision": "pending",
        "current": "adopted an existing hand-authored branch",
        "created_at": now_iso(), "ended_at": "", "steps": [],
        "baseline_score": None, "candidate_score": None,
        "gate_delta": None, "gate_passed": None, "branch": branch,
        "commits": commits, "changed_files": changed,
        "reviews": [], "review_requested": False,
    }
    _pstep(rec, "adopt", True,
           f"{len(commits)} commit(s), {len(changed)} file(s) ahead of {to} "
           f"(controller: {rec['controller']})")

    # Compile/smoke gate: ast.parse each changed .py AT THE BRANCH (git show) —
    # dependency-free, fast, and catches the syntax breakage that would stop the
    # branch booting. Proves the branch PARSES, nothing about BEHAVIOUR.
    import ast as _ast
    py = [f for f in changed if f.endswith(".py")]
    bad: List[str] = []
    for f in py:
        show = await _git("show", f"{branch}:{f}", repo_root=root)
        if not show["ok"]:
            continue  # deleted/renamed on the branch — nothing to parse
        try:
            _ast.parse(show.get("out", "") or "")
        except SyntaxError as e:
            bad.append(f"{f}: {e}")
    if py:
        compile_ok = not bad
        _pstep(rec, "gate", compile_ok,
               f"compile-check {len(py)} .py file(s): "
               + ("PASS" if compile_ok else "FAIL — " + "; ".join(bad[:3])))
    else:
        compile_ok = True
        _pstep(rec, "gate", True,
               "no .py changes — compile gate n/a; promote with force for docs/infra")

    # Critical-system regression gate (dev-lifecycle §6 / route-forward M3) — the
    # compile check above never behaviourally exercised anything, so a fix could
    # parse clean and still be wrong; every fix landed by hand this session had to
    # be manually unit-tested and live-verified in a sandbox to get real confidence
    # because THIS gate couldn't provide it (route-forward.md T6). Run the
    # critical-tier pytest suite (tests/conftest.py's `critical` marker — pure,
    # deterministic tests guarding systems where a regression is expensive and was
    # actually hit) against the branch's OWN worktree, via the existing ephemeral-
    # container runner (evolve.unittest.run — ISOLATED from whatever container is
    # serving this request, so it can never hang the app it's gating). Only for
    # the vera repo — a registered generic repo already gates on its own test_cmd
    # via evolve.pipeline.test. Only when .py actually changed — a docs/UI-only
    # branch has nothing for pytest to exercise. `critical_ok` stays None (not
    # counted against the gate) when the branch has no live worktree yet or the
    # ephemeral-container run itself fails to execute (infra hiccup, not a code
    # regression) — surfaced either way so it's never silently skipped unnoticed.
    critical_ok: Optional[bool] = None
    if py and repo == DEFAULT_REPO_ID:
        wt = await _branch_worktree(branch)   # pool sandbox OR any plain git worktree
        if wt:
            try:
                crit = await evolve_unittest_run(branch=branch, paths="tests",
                                                 markers="critical", timeout=300)
            except Exception as e:
                crit = {"error": str(e)}
            if crit.get("error"):
                _pstep(rec, "critical-tests", False,
                       f"could not run critical-tier tests: {crit['error']}")
            else:
                critical_ok = bool(crit.get("ok"))
                _pstep(rec, "critical-tests", critical_ok,
                       crit.get("summary", "") or ("PASS" if critical_ok else "FAIL"))
        else:
            _pstep(rec, "critical-tests", True,
                   "no worktree for this branch — critical-tier gate skipped "
                   "(git worktree add it, or evolve.sandbox.up, for full gating)")

    rec["gate_passed"] = compile_ok if critical_ok is None else (compile_ok and critical_ok)

    await _save_pipeline(rec)
    await _audit("pipeline.adopt",
                 f"adopted {branch} ({len(commits)} commit(s)) → {to} "
                 f"[gate_passed={rec['gate_passed']}]",
                 id=rec["id"], kind="code", branch=branch, repo=repo)
    await emit_event({"type": "evolve.pipeline.adopted", "id": rec["id"], "branch": branch,
                      "into": to, "commits": len(commits), "gate_passed": rec["gate_passed"]})
    return {"ok": True, "id": rec["id"], "branch": branch, "into": to,
            "ahead_by": len(commits), "changed_files": len(changed),
            "gate_passed": rec["gate_passed"],
            "next": "evolve.pipeline.review_request to raise for review, then "
                    "evolve.pipeline.promote (force=true for docs/infra) to merge."}


@capability("evolve.pipeline.begin", memory="on",
            http_method="POST", http_path="/evolve/pipeline/begin", http_tags=["evolve"],
            description="ATOMIC pipeline start — ONE call does everything an agent needs to begin "
                        "work on Vera, so it never has to reinvent the flow: creates a TYPED branch "
                        "off bleeding-edge (the staging trunk — falls back to the repo's real "
                        "mainline if it has no bleeding-edge branch; pass base= to fork from "
                        "somewhere else explicitly), materialises its worktree + dev sandbox (its "
                        "OWN container with spawn=true, for multi-agent), and records the CI/CD "
                        "pipeline WITH its worktree (so diff/test work). Returns branch, worktree, "
                        "sandbox url, pipeline id, and the exact NEXT caps. Then: edit the worktree, "
                        "commit via evolve.sandbox.exec(where='worktree', branch=...) — git-over-SMB "
                        "fails in the worktree — then review_request + promote (which now also "
                        "targets bleeding-edge by default). Inputs: title (str!), branch (str — full "
                        "typed name, else feat/<slug>), spawn (bool), session_id (str), repo "
                        "(str=vera), base (str — explicit fork point; default bleeding-edge). "
                        "Output: {ok, id, branch, worktree, url, base, next[]}.")
async def evolve_pipeline_begin(title: str = "", branch: str = "", spawn: bool = False,
                                session_id: str = "", repo: str = DEFAULT_REPO_ID,
                                base: str = "", trace_id=None):
    title = (title or "").strip()
    if not title and not branch:
        return {"error": "title (or branch) required"}
    if repo != DEFAULT_REPO_ID and not (await evolve_repo_get(id=repo)).get("repo"):
        return {"error": f"repo not registered: {repo}"}
    root = await _resolve_repo_root(repo)
    base = (base or "").strip() or await _default_pipeline_base(repo_root=root)
    if not (await _git("rev-parse", "--verify", f"refs/heads/{base}", repo_root=root))["ok"]:
        return {"error": f"unknown base branch: {base}"}
    br = (branch or "").strip()
    if not br:
        slug = re.sub(r"[^a-z0-9._-]+", "-", title.lower()).strip("-")[:40] or uuid.uuid4().hex[:8]
        br = f"feat/{slug}"
    if not (await _git("rev-parse", "--verify", f"refs/heads/{br}", repo_root=root))["ok"]:
        cr = await _git("branch", br, base, repo_root=root)
        if not cr["ok"]:
            return {"error": f"branch create failed: {cr['err'] or cr['out']}"}
    up = await (evolve_sandbox_spawn(branch=br) if spawn else evolve_sandbox_up(branch=br))
    if up.get("error"):
        return {"error": f"sandbox up failed: {up['error']}", "branch": br}
    wt = (up.get("sandbox") or {}).get("worktree") or up.get("worktree", "")
    url = up.get("url", "")
    rec = {
        "id": uuid.uuid4().hex[:8], "kind": "code", "profile": "adopted", "variant_id": "",
        "edits": [{"area": br, "suggestion": title or "hand-authored change"}],
        "gate_threshold": 0.0, "auto_promote": False, "auto_test": False, "critic": "",
        "repo": repo, "controller": _triggered_by(), "adopted": True, "began": True,
        "session_id": (session_id or "").strip(), "via": (CALLER_KIND.get() or ""),
        "to": base, "status": "drafting", "decision": "pending",
        "current": "branch + worktree ready — edit, commit, then promote",
        "created_at": now_iso(), "ended_at": "", "steps": [],
        "baseline_score": None, "candidate_score": None, "gate_delta": None,
        "gate_passed": None, "branch": br, "worktree": wt, "commits": [], "changed_files": [],
        "reviews": [], "review_requested": False,
    }
    _pstep(rec, "begin", True,
           f"branch {br} + worktree ready ({'own container' if spawn else 'primary sandbox'})")
    await _save_pipeline(rec)
    await _audit("pipeline.begin", f"began {br} → {base}", id=rec["id"], branch=br, repo=repo)
    await emit_event({"type": "evolve.pipeline.begun", "id": rec["id"], "branch": br})
    return {"ok": True, "id": rec["id"], "branch": br, "worktree": wt, "url": url, "base": base,
            "next": [
                f"1. edit files in the worktree ({wt})",
                f"2. commit ON THE HOST: evolve.sandbox.exec(where='worktree', branch='{br}', "
                f"cmd='git add -A && git commit -m \"...\"')",
                f"3. raise: evolve.pipeline.review_request(id='{rec['id']}', reason='...')",
                f"4. land: evolve.pipeline.promote(id='{rec['id']}'[, force=true for docs/UI]) "
                "— it refreshes the branch's commits + does the safe merge",
            ]}


@capability("evolve.pipeline.list", memory="off", silent=True,
            http_method="GET", http_path="/evolve/pipeline/list", http_tags=["evolve"],
            description="Recent CI/CD pipeline runs (compact, newest first). "
                        "Query: limit.")
async def evolve_pipeline_list(limit: int = 30, trace_id=None):
    r = _redis()
    out = []
    if r:
        try:
            rows = await r.lrange(KEY_PIPELINES, 0, max(0, int(limit) - 1))
            for row in rows or []:
                try:
                    rec = json.loads(row.decode() if isinstance(row, bytes) else row)
                    rec["live"] = rec.get("id") in _PIPELINE_TASKS
                    out.append(rec)
                except Exception:
                    continue
        except Exception:
            pass
    return {"pipelines": out, "count": len(out)}


@capability("evolve.pipeline.get", memory="off", silent=True,
            http_method="GET", http_path="/evolve/pipeline/get", http_tags=["evolve"],
            description="Full record of one pipeline run (stages, scores, gate, "
                        "decision). Input: id (str!).")
async def evolve_pipeline_get(id: str = "", trace_id=None):
    r = _redis()
    if not r or not id:
        return {"error": "id required"}
    raw = await r.get(KEY_PIPELINE + id)
    if not raw:
        return {"error": "pipeline not found"}
    rec = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    rec["live"] = id in _PIPELINE_TASKS
    return {"pipeline": rec}


@capability("evolve.pipeline.review.request", memory="off",
            http_method="POST", http_path="/evolve/pipeline/review/request", http_tags=["evolve"],
            description="Flag a pipeline as needing an external adversarial "
                        "review before its gate/promote decision — the "
                        "coding-specialist-loop side of the handoff: a loop "
                        "that just finished a candidate edit can request "
                        "review instead of self-gating. Input: id (str!), "
                        "reason (str — why review is wanted). Output: {ok}.")
async def evolve_pipeline_review_request(id: str = "", reason: str = "", trace_id=None):
    got = await evolve_pipeline_get(id=id)
    rec = got.get("pipeline")
    if not rec:
        return {"error": got.get("error") or "pipeline not found"}
    rec["review_requested"] = True
    rec["review_request_reason"] = reason[:500]
    await _save_pipeline(rec)
    await emit_event({"type": "evolve.pipeline.review_requested", "id": id, "reason": reason[:200]})
    return {"ok": True}


@capability("evolve.pipeline.review", memory="on",
            http_method="POST", http_path="/evolve/pipeline/review", http_tags=["evolve"],
            description="Submit an adversarial review against a real pipeline "
                        "record — the mechanism for a review done via Loop Lab "
                        "(rather than ad hoc in conversation) to be logged as "
                        "such: reviewer identity (real, from triggered_by — "
                        "never guessed), verdict, findings, and any edits made "
                        "during the review (already applied separately via "
                        "code.author/code.edit — pass their file/summary here "
                        "to link them to this review, not to author them). "
                        "Clears review_requested. Inputs: id (str!), verdict "
                        "(approved|changes_requested|blocked), findings (str!), "
                        "edits_made (list of {file, summary}, optional). "
                        "Output: {ok, reviewer}.",
            schema=enum_schema(verdict=["approved", "changes_requested", "blocked"]))
async def evolve_pipeline_review(id: str = "", verdict: str = "", findings: str = "",
                                 edits_made: Optional[List[Dict[str, Any]]] = None,
                                 trace_id=None):
    if verdict not in ("approved", "changes_requested", "blocked"):
        return {"error": "verdict must be approved|changes_requested|blocked"}
    if not findings.strip():
        return {"error": "findings required"}
    got = await evolve_pipeline_get(id=id)
    rec = got.get("pipeline")
    if not rec:
        return {"error": got.get("error") or "pipeline not found"}
    reviewer = _triggered_by()   # real signal (claude_code/autonomous/user) — never guessed
    review = {
        "reviewer": reviewer, "verdict": verdict, "findings": findings[:4000],
        "edits_made": (edits_made or [])[:20], "ts": now_iso(),
    }
    rec.setdefault("reviews", []).append(review)
    rec["review_requested"] = False
    await _save_pipeline(rec)
    await _audit("pipeline.review", f"{reviewer}: {verdict} on pipeline {id}",
                 id=id, verdict=verdict, reviewer=reviewer)
    await emit_event({"type": "evolve.pipeline.reviewed", "id": id,
                      "reviewer": reviewer, "verdict": verdict})
    return {"ok": True, "reviewer": reviewer}


@capability("evolve.pipeline.test", memory="on",
            http_method="POST", http_path="/evolve/pipeline/test", http_tags=["evolve"],
            description="(Generic-repo code pipelines only — repo != 'vera'.) "
                        "Gate a code pipeline's branch once its edit has landed: "
                        "runs the repo's test_cmd in the branch worktree, computes "
                        "gate_delta against the baseline already recorded at "
                        "branch-creation time, and sets gate_passed/status. Merge "
                        "stays manual (evolve.pipeline.promote/.rollback). For "
                        "repo='vera' use the dev sandbox (Sandbox tab) instead — "
                        "this capability refuses that case. Input: id (str!). "
                        "Output: {ok, gate_passed, gate_delta}.")
async def evolve_pipeline_test(id: str = "", trace_id=None):
    got = await evolve_pipeline_get(id=id)
    rec = got.get("pipeline")
    if not rec:
        return {"error": got.get("error") or "pipeline not found"}
    repo = rec.get("repo") or DEFAULT_REPO_ID
    if rec.get("kind") != "code" or repo == DEFAULT_REPO_ID:
        return {"error": "evolve.pipeline.test is only for code pipelines with "
                         "repo != 'vera' — use the dev sandbox for the Vera repo"}
    worktree = rec.get("worktree")
    if not worktree or not Path(worktree).exists():
        return {"error": "no worktree for this pipeline (branch step may have failed)"}
    test_cmd = rec.get("test_cmd") or DEFAULT_TEST_CMD
    cand = await _repo_test_gate(worktree, test_cmd)
    if cand.get("error"):
        return {"error": cand["error"]}
    rec["candidate_score"] = cand["pass_rate"]
    threshold = float(rec.get("gate_threshold", 0.0))
    delta = round((rec.get("candidate_score") or 0) - (rec.get("baseline_score") or 0), 2)
    rec["gate_delta"] = delta
    passed = delta >= threshold
    rec["gate_passed"] = passed
    _pstep(rec, "test", True, cand["summary"])
    _pstep(rec, "gate", passed, f"Δ {delta:+} vs {threshold} — {'PASS' if passed else 'FAIL'}")
    rec["status"] = "tested"
    rec["decision"] = "pending"  # merge stays manual
    rec["current"] = ("branch tested — gate PASSED, ready to promote (merge)"
                      if passed else "branch tested — gate FAILED, recommend rollback")
    await _save_pipeline(rec)
    await _audit("pipeline.test", f"{repo}:{rec['branch']} — "
                 f"{'PASS' if passed else 'FAIL'} (Δ {delta:+})",
                 id=id, repo=repo, gate_passed=passed)
    await emit_event({"type": "evolve.pipeline.done", "id": id, "decision": "pending",
                      "kind": "code", "gate_passed": passed})
    return {"ok": True, "gate_passed": passed, "gate_delta": delta, "output": cand["output"]}


from Vera.vera.evolve.evolve_git_core import worktree_paths_by_branch as _worktree_paths_by_branch  # noqa: E402
from Vera.vera.evolve.evolve_git_core import tracked_dirty_lines as _tracked_dirty_lines  # noqa: E402


async def _sync_branch_with_target(root: str, branch: str, to: str) -> Dict[str, Any]:
    """Merge `to`'s current tip into `branch`'s own worktree, BEFORE `branch`
    gets merged into `to` — so commits that landed on `to` after `branch` was
    created or last synced (e.g. another pipeline promoted into the same
    trunk in the interim) aren't silently overwritten by this branch's now-
    stale state. Uses the same merge-tree conflict-preflight as
    _merge_isolated/_merge_in_checkout; a conflict halts and is reported here
    — never auto-resolved. No-ops cleanly if `branch` already contains `to`."""
    wl = await _git("worktree", "list", "--porcelain", repo_root=root)
    wt_of = _worktree_paths_by_branch(wl.get("out", ""))
    branch_wt = wt_of.get(branch)
    if not branch_wt:
        return {"ok": False, "error": f"no worktree found for {branch} — cannot sync"}
    anc = await _sh(_git_wt_argv(branch_wt, "merge-base", "--is-ancestor", to, branch), cwd=branch_wt)
    if anc.get("ok"):
        return {"ok": True, "action": "already up to date"}
    st = await _sh(_git_wt_argv(branch_wt, "status", "--porcelain"), cwd=branch_wt)
    if (st.get("out") or "").strip():
        return {"ok": False, "error": f"{branch}'s worktree has uncommitted changes — cannot sync"}
    mt = await _sh(_git_wt_argv(branch_wt, "merge-tree", "--write-tree", branch, to), cwd=branch_wt)
    if not mt.get("ok"):
        return {"ok": False,
                "error": f"{to} conflicts with {branch} — resolve manually on the branch, "
                         f"then re-promote",
                "conflicts": ["(merge-tree reported conflicts)"]}
    mg = await _sh(_git_wt_argv(branch_wt, "merge", "--no-ff", "-m",
                                f"sync: merge {to} into {branch} before promote", to),
                   cwd=branch_wt)
    if not mg.get("ok"):
        await _sh(_git_wt_argv(branch_wt, "merge", "--abort"), cwd=branch_wt)
        return {"ok": False, "error": f"sync merge failed: {mg.get('err') or mg.get('out')}"}
    return {"ok": True, "action": "synced"}


async def _merge_isolated(root: str, branch: str, into: str, msg: str) -> Dict[str, Any]:
    """Merge `branch` into `into` in a THROWAWAY worktree — for when `into` is NOT
    checked out anywhere, so no live working tree is touched. {ok, commit, conflicts}."""
    import uuid
    tmp = str(Path(root) / ".loop-lab-worktrees" / f"_merge-{uuid.uuid4().hex[:8]}")
    add = await _sh(_git_wt_argv(str(root), "worktree", "add", tmp, into), cwd=str(root))
    if not add["ok"]:
        return {"ok": False, "commit": "", "conflicts": [],
                "error": f"worktree add failed: {add['err'] or add['out']}"}
    conflicts: List[str] = []
    sha = ""
    try:
        # M3.6 part 2: sanctioned-deploy override so the pre-merge-commit hook allows
        # THIS pipeline merge (a hand-run merge onto main carries no such env → blocked).
        mg = await _sh(["env", "VERA_ALLOW_MAIN_COMMIT=1",
                        *_git_wt_argv(tmp, "merge", "--no-ff", "-m", msg, branch)], cwd=tmp)
        if mg["ok"]:
            sha = (await _sh(_git_wt_argv(tmp, "rev-parse", "HEAD"), cwd=tmp)).get("out", "")
        else:
            cf = await _sh(_git_wt_argv(tmp, "diff", "--name-only", "--diff-filter=U"), cwd=tmp)
            conflicts = [ln for ln in (cf.get("out", "") or "").splitlines() if ln.strip()]
            await _sh(_git_wt_argv(tmp, "merge", "--abort"), cwd=tmp)
    finally:
        await _sh(_git_wt_argv(str(root), "worktree", "remove", "--force", tmp), cwd=str(root))
    return {"ok": bool(sha), "commit": sha, "conflicts": conflicts,
            "error": "" if sha else "merge conflict"}


async def _merge_in_checkout(root: str, branch: str, into: str, wt: str, msg: str) -> Dict[str, Any]:
    """DEPLOY merge: `into` is checked out LIVE at `wt` (e.g. prod on main), so an
    isolated worktree can't touch it. Merge `branch` into it IN PLACE — committed +
    hook-validated — but GUARDED: it must already be on `into` (never switch a live
    checkout), its tree must be clean (protect uncommitted work), and it must merge
    without conflict. Requires a deliberate restart to activate — flagged, never
    performed here. Replaces the old promote's blind `git checkout <to>`."""
    cur = (await _sh(_git_wt_argv(wt, "symbolic-ref", "--short", "-q", "HEAD"), cwd=wt)).get("out", "")
    if cur != into:
        return {"ok": False, "commit": "", "conflicts": [],
                "error": f"target worktree is on '{cur}', not '{into}' — refusing to switch a live checkout"}
    st = await _sh(_git_wt_argv(wt, "status", "--porcelain"), cwd=wt)
    # Refuse only on TRACKED uncommitted work (staged/modified) — that's the WIP a
    # merge could clobber. Unrelated UNTRACKED files (e.g. a scratch/spec doc left
    # open in the standing bleeding-edge worktree) must not block an in-checkout
    # promote: git merge below refuses on its own to overwrite an untracked file it
    # would actually touch, so a real collision still fails safely.
    _dirty = _tracked_dirty_lines(st.get("out", "") or "")
    if _dirty:
        return {"ok": False, "commit": "", "conflicts": [],
                "error": "target checkout has uncommitted TRACKED changes — refusing "
                         "(protect WIP); commit or clean it first",
                "dirty": _dirty[:20]}
    mt = await _sh(_git_wt_argv(wt, "merge-tree", "--write-tree", into, branch), cwd=wt)
    if not mt["ok"]:
        return {"ok": False, "commit": "", "conflicts": ["(merge-tree reported conflicts)"],
                "error": "merge conflict — resolve on the branch, then re-promote"}
    # M3.6 part 2: sanctioned-deploy override so the pre-merge-commit hook allows THIS
    # pipeline merge (a hand-run merge onto main carries no such env, so it stays blocked).
    mg = await _sh(["env", "VERA_ALLOW_MAIN_COMMIT=1",
                    *_git_wt_argv(wt, "merge", "--no-ff", "-m", msg, branch)], cwd=wt)
    if not mg["ok"]:
        await _sh(_git_wt_argv(wt, "merge", "--abort"), cwd=wt)
        return {"ok": False, "commit": "", "conflicts": [],
                "error": f"merge failed: {mg['err'] or mg['out']}"}
    sha = (await _sh(_git_wt_argv(wt, "rev-parse", "HEAD"), cwd=wt)).get("out", "")
    return {"ok": True, "commit": sha, "conflicts": [], "restart_required": True, "error": ""}


@capability("evolve.pipeline.promote", memory="on",
            http_method="POST", http_path="/evolve/pipeline/promote", http_tags=["evolve"],
            description="Promote a pipeline's change. Variant → set the overlay. Code → "
                        "first syncs `branch` up to date with `to` (merges `to`'s latest "
                        "into the branch's own worktree — halts with a conflict report, "
                        "never auto-resolves, if that doesn't merge cleanly), then MERGEs "
                        "the branch into `to` (default bleeding-edge — the staging trunk; "
                        "2026-08-16) SAFELY: an isolated throwaway worktree when `to` isn't "
                        "checked out, or a guarded in-checkout merge (no branch switch, "
                        "refuses a dirty tree or a conflict, returns restart_required) when "
                        "`to` is a live checkout like the standing bleeding-edge container "
                        "or prod on main. Never the old blind `git checkout <to>`. "
                        "Promoting bleeding-edge itself to main is a separate, deliberate "
                        "action — evolve.bleeding_edge.promote_to_main — not this. "
                        "Input: id (str!), to (str — default bleeding-edge).")
async def evolve_pipeline_promote(id: str = "", to: str = "bleeding-edge", force: bool = False,
                                  authorize_main: str = "", trace_id=None):
    got = await evolve_pipeline_get(id=id)
    if got.get("error"):
        return got
    rec = got["pipeline"]
    if rec["kind"] == "variant":
        res = await evolve_variant_promote(profile=rec["profile"],
                                           variant_id=rec["variant_id"])
        rec["decision"] = "promoted"
        _pstep(rec, "promote", not res.get("error"), res.get("error") or "overlay set")
        await _save_pipeline(rec)
        await _audit("pipeline.promote", f"variant overlay ← {rec['variant_id']} "
                     f"({rec['profile']})", id=id, kind="variant")
        return {"ok": not res.get("error"), "decision": "promoted"}
    # code: merge branch → `to`. Route SAFELY — never the blind `git checkout <to>`
    # in the repo root the old promote used (that switched prod's live checkout and
    # could clobber uncommitted work).
    branch = rec.get("branch")
    if not branch:
        return {"error": "pipeline has no branch"}
    to = (to or "bleeding-edge").strip()
    root = await _resolve_repo_root(rec.get("repo") or DEFAULT_REPO_ID)
    if not (await _git("rev-parse", "--verify", f"refs/heads/{branch}", repo_root=root))["ok"]:
        return {"error": f"unknown branch: {branch}"}
    if not (await _git("rev-parse", "--verify", f"refs/heads/{to}", repo_root=root))["ok"]:
        return {"error": f"unknown target branch: {to}"}
    # M3.6 main-merge guardrail (see adopt): refuse promoting a feature branch to
    # the real mainline unless the explicit authorization sentinel is present. The
    # sanctioned mainline path is evolve.bleeding_edge.promote_to_main (does NOT
    # route through here), on the user's explicit, unambiguous go-ahead.
    from Vera.vera.evolve.evolve_git_core import main_merge_refusal as _main_merge_refusal  # noqa: E402
    _mrefuse = _main_merge_refusal(to, await _default_branch(repo_root=root), authorize_main)
    if _mrefuse:
        rec["decision"] = "held"
        _pstep(rec, "promote", False, "promote blocked - main-merge guard")
        await _save_pipeline(rec)
        await _audit("pipeline.promote", f"REFUSED {branch} -> {to}: main-merge guard",
                     id=id, kind="code", branch=branch, ok=False, repo=rec.get("repo"))
        return {"ok": False, "held": True, "refused": "main-merge-guard", "error": _mrefuse}
    # Refresh the record's commits/changed_files from git so attribution + the UI
    # reflect what's actually on the branch NOW — essential for evolve.pipeline.begin
    # stubs (recorded before any commit) and harmless for adopt (keeps them current).
    _lg = await _git("log", "--oneline", f"{to}..{branch}", repo_root=root)
    _cm = [ln for ln in (_lg.get("out", "") or "").splitlines() if ln.strip()]
    if _cm:
        rec["commits"] = _cm
        _dfp = await _git("diff", "--name-only", f"{to}...{branch}", repo_root=root)
        rec["changed_files"] = [ln for ln in (_dfp.get("out", "") or "").splitlines() if ln.strip()]
    # ── GATE (§3/§6): a code change only merges to `to` when its pipeline gate
    # PASSED — green tests on the branch in the dev sandbox, which also boots it
    # (catching the import-time breakage py_compile misses). No gate pass → no
    # promote. `force=True` is the sanctioned override for a change the score-gate
    # can't evaluate (docs/infra) — still hook-validated at the merge.
    if rec.get("gate_passed") is not True and not force:
        rec["decision"] = "held"
        _pstep(rec, "gate", False, "promote blocked — gate not passed")
        await _save_pipeline(rec)
        await _audit("pipeline.promote", f"BLOCKED {branch} → {to}: gate not passed",
                     id=id, kind="code", branch=branch, ok=False, repo=rec.get("repo"))
        return {"ok": False, "held": True, "gate_passed": rec.get("gate_passed"),
                "error": "gate not passed — run evolve.pipeline.test to gate the branch "
                         "first, or promote with force=true for a change the score-gate "
                         "can't evaluate (docs/infra)."}
    # ── Anti-clobber sync (2026-08-16 bleeding-edge-trunk-workflow): bring
    # `branch` up to date with `to` BEFORE merging back. Without this, a
    # branch that forked from `to` a while ago can silently overwrite commits
    # that landed on `to` in the interim (another branch promoted first) —
    # exactly the scenario multiple branches sharing bleeding-edge as a
    # trunk makes routine. Conflicts halt here and are reported; never
    # auto-resolved.
    sync_res = await _sync_branch_with_target(str(root), branch, to)
    if not sync_res.get("ok"):
        rec["decision"] = "held"
        _pstep(rec, "sync", False, sync_res.get("error") or "sync conflict")
        await _save_pipeline(rec)
        await _audit("pipeline.promote", f"BLOCKED {branch} → {to}: sync conflict",
                     id=id, kind="code", branch=branch, ok=False, repo=rec.get("repo"))
        return {"ok": False, "held": True, "error": sync_res.get("error"),
                "conflicts": sync_res.get("conflicts", []),
                "hint": f"resolve the conflict between {branch} and {to} on the branch's "
                        f"own worktree, then re-promote"}
    msg = f"Loop Lab: merge {branch} (pipeline {id})"
    wl = await _git("worktree", "list", "--porcelain", repo_root=root)
    wt_of = _worktree_paths_by_branch(wl.get("out", ""))
    if to in wt_of:
        res = await _merge_in_checkout(str(root), branch, to, wt_of[to], msg)   # guarded deploy in place
    else:
        res = await _merge_isolated(str(root), branch, to, msg)                 # isolated — no live tree
    ok = res["ok"]
    rec["decision"] = "promoted" if ok else rec.get("decision", "held")
    _pstep(rec, "merge", ok, res.get("error") or f"merged {branch} → {to} @ {(res.get('commit') or '')[:10]}")
    await _save_pipeline(rec)
    await _audit("pipeline.promote",
                 f"MERGED {branch} → {to} @ {(res.get('commit') or '')[:10]}"
                 if ok else f"merge {branch} → {to} FAILED: {(res.get('error') or '')[:120]}",
                 id=id, kind="code", branch=branch, ok=ok, repo=rec.get("repo"))
    await emit_event({"type": "evolve.pipeline.promoted", "id": id, "branch": branch,
                      "into": to, "ok": ok, "commit": res.get("commit", "")})
    out = {"ok": ok, "merged": branch if ok else "", "into": to,
           "commit": res.get("commit", ""), "conflicts": res.get("conflicts", []),
           "error": "" if ok else res.get("error", "")}
    if res.get("restart_required"):
        out["restart_required"] = True
        out["note"] = "merged into the live checkout — a deliberate restart is required to activate it"
    # Standing bleeding-edge container: refresh right after any successful
    # promote INTO bleeding-edge, not on a timer, so it's always testing the
    # current tip (2026-08-16 bleeding-edge-trunk-workflow). Best-effort —
    # never blocks or fails the promote itself if the standing container
    # isn't up (it's opt-in via evolve.bleeding_edge.container.ensure).
    if ok and to == BLEEDING_EDGE_BRANCH:
        try:
            refresh = await _refresh_standing_bleeding_edge_container()
            out["standing_container_refresh"] = refresh
        except Exception as e:
            log.debug("standing bleeding-edge container refresh: %s", e)
    return out


@capability("evolve.bleeding_edge.promote_to_main", memory="on",
            http_method="POST", http_path="/evolve/bleeding_edge/promote_to_main",
            http_tags=["evolve"],
            description="Deliberately promote the WHOLE bleeding-edge branch into "
                        "the repo's real mainline — the release step. Separate from "
                        "the normal per-pipeline evolve.pipeline.promote (which now "
                        "targets bleeding-edge by default): this is manual-only, "
                        "never called automatically by any gate, scheduler, or "
                        "other capability. Same safe-merge machinery as pipeline "
                        "promote (in-checkout guarded merge or isolated worktree "
                        "merge, never a blind checkout). Input: repo (str=vera). "
                        "Output: {ok, into, commit, conflicts, restart_required}.")
async def evolve_bleeding_edge_promote_to_main(repo: str = DEFAULT_REPO_ID, trace_id=None):
    root = await _resolve_repo_root(repo)
    if not (await _git("rev-parse", "--verify", f"refs/heads/{BLEEDING_EDGE_BRANCH}",
                       repo_root=root))["ok"]:
        return {"error": f"'{BLEEDING_EDGE_BRANCH}' branch does not exist in this repo"}
    to = await _default_branch(repo_root=root)
    if not (await _git("rev-parse", "--verify", f"refs/heads/{to}", repo_root=root))["ok"]:
        return {"error": f"unknown target branch: {to}"}
    msg = f"Loop Lab: release {BLEEDING_EDGE_BRANCH} → {to}"
    wl = await _git("worktree", "list", "--porcelain", repo_root=root)
    wt_of = _worktree_paths_by_branch(wl.get("out", ""))
    if to in wt_of:
        res = await _merge_in_checkout(str(root), BLEEDING_EDGE_BRANCH, to, wt_of[to], msg)
    else:
        res = await _merge_isolated(str(root), BLEEDING_EDGE_BRANCH, to, msg)
    ok = res["ok"]
    await _audit("bleeding_edge.promote_to_main",
                 f"MERGED {BLEEDING_EDGE_BRANCH} → {to} @ {(res.get('commit') or '')[:10]}"
                 if ok else f"merge {BLEEDING_EDGE_BRANCH} → {to} FAILED: "
                            f"{(res.get('error') or '')[:120]}",
                 kind="release", branch=BLEEDING_EDGE_BRANCH, ok=ok, repo=repo)
    await emit_event({"type": "evolve.bleeding_edge.promoted_to_main", "ok": ok,
                      "into": to, "commit": res.get("commit", "")})
    out = {"ok": ok, "into": to, "commit": res.get("commit", ""),
           "conflicts": res.get("conflicts", []), "error": "" if ok else res.get("error", "")}
    if res.get("restart_required"):
        out["restart_required"] = True
        out["note"] = "merged into the live checkout — a deliberate restart is required to activate it"
    if ok:
        # main just moved — keep the mainline mirror (and anything sourced
        # from it) current too.
        try:
            await _refresh_mainline_mirror(repo_root=root)
        except Exception as e:
            log.debug("mainline mirror refresh after release: %s", e)
    return out


@capability("evolve.pipeline.rollback", memory="on",
            http_method="POST", http_path="/evolve/pipeline/rollback", http_tags=["evolve"],
            description="Roll back a pipeline's change: for a variant pipeline, "
                        "clear the overlay; for a code pipeline, delete its branch "
                        "(discards the change — main is untouched). Input: id (str!).")
async def evolve_pipeline_rollback(id: str = "", trace_id=None):
    got = await evolve_pipeline_get(id=id)
    if got.get("error"):
        return got
    rec = got["pipeline"]
    if rec["kind"] == "variant":
        await evolve_variant_clear(profile=rec["profile"])
        rec["decision"] = "rolled_back"
        _pstep(rec, "rollback", True, "overlay cleared")
    else:
        branch = rec.get("branch")
        if branch:
            res = await evolve_branch_delete(branch=branch,
                                             repo=rec.get("repo") or DEFAULT_REPO_ID)
            _pstep(rec, "rollback", not res.get("error"),
                   res.get("error") or f"deleted {branch}")
        rec["decision"] = "rolled_back"
    await _save_pipeline(rec)
    await _audit("pipeline.rollback",
                 f"{rec['kind']} pipeline {id} rolled back ({rec['profile']})",
                 id=id, kind=rec["kind"])
    await emit_event({"type": "evolve.pipeline.rolledback", "id": id})
    return {"ok": True, "decision": "rolled_back"}


# ─────────────────────────────────────────────────────────────────────────────
# CHANGE SOURCES — turn external findings into CI code pipelines
# ─────────────────────────────────────────────────────────────────────────────
# The critic isn't the only source of improvements. Two rich streams already
# exist in Vera: the DREAM SOURCE REVIEW (technical recommendations + file
# descriptions per subsystem area) and the PERF/OBSERVE monitors (errors, event-
# loop stalls, saturation). Both are distilled into concrete edits and fed into
# the same gated, branch-based code pipeline.

_DISTILL_SYSTEM = (
    "You convert technical findings into a SHORT list of concrete, minimal source "
    "changes for the Vera codebase. Reply with ONLY a JSON array of "
    '{"area":"<module or behaviour>","suggestion":"<one specific, safe change — '
    'what to change and why, small in scope>"}. Prefer the few highest-leverage, '
    "lowest-risk fixes. No broad refactors. No prose outside the JSON array.")


async def _distill_edits(text: str, provider: str, limit: int = 5) -> List[Dict[str, Any]]:
    if not (text or "").strip():
        return []
    res = await _provider_chat(provider, text[:12000], system=_DISTILL_SYSTEM,
                               max_tokens=1500)
    if res.get("error"):
        return []
    arr = _extract_json_array(res.get("text", "")) or []
    out = []
    for c in arr[:limit]:
        if isinstance(c, dict) and c.get("suggestion"):
            out.append({"area": str(c.get("area", ""))[:120],
                        "suggestion": str(c["suggestion"])[:800]})
    return out


@capability("evolve.pipeline.from_review", memory="on",
            http_method="POST", http_path="/evolve/pipeline/from_review",
            http_tags=["evolve"],
            description="Turn the DREAM SOURCE REVIEW's technical recommendations "
                        "for an area into a gated code pipeline: pulls the area "
                        "report (dream.review.area_report), distils it into "
                        "concrete edits, and launches a code pipeline (branch + "
                        "Claude edits + sandbox test + manual merge). Inputs: area "
                        "(str! — from dream.review.areas), style (str), profile "
                        "(str), provider (str — distiller), max_edits (int=4), "
                        "launch (bool default True). Output: {ok, edits, pipeline_id?}.")
async def evolve_pipeline_from_review(area: str = "", style: str = "",
                                      profile: str = "", provider: str = "",
                                      max_edits: int = 4, launch: bool = True,
                                      trace_id=None):
    if not area:
        return {"error": "area required (see dream.review.areas)"}
    cfg = await _get_config()
    rep = await _call("dream.review.area_report", area=area, style=style)
    md = (rep or {}).get("markdown", "") if isinstance(rep, dict) else ""
    if not md:
        return {"error": f"no review report for area '{area}' — run a dream "
                         "source review first (Dream → Dream Review)"}
    edits = await _distill_edits(md, provider or cfg["editor_provider"],
                                 limit=int(max_edits))
    if not edits:
        return {"error": "could not distil actionable edits from the review"}
    out = {"ok": True, "area": area, "edits": edits}
    if launch:
        pr = await evolve_pipeline_run(kind="code",
                                       profile=(profile or cfg["default_profile"]),
                                       edits=edits)
        out["pipeline_id"] = pr.get("id")
    await emit_event({"type": "evolve.from_review", "area": area,
                      "edits": len(edits), "launched": launch})
    return out


@capability("evolve.observe.scan", memory="on",
            http_method="POST", http_path="/evolve/observe/scan", http_tags=["evolve"],
            description="Self-improve from OBSERVABILITY: pull perf findings "
                        "(perf.scan), event-loop stalls (perf.stalls) and recent "
                        "errors (syslog), distil them into concrete code fixes, and "
                        "(optionally) launch gated code pipelines. The perf/observe "
                        "twin of the critic loop. Inputs: provider (str — distiller), "
                        "max_edits (int=4), launch (bool default False — return "
                        "suggestions only unless set), profile (str). Output: "
                        "{ok, findings_summary, edits, pipeline_id?}.")
async def evolve_observe_scan(provider: str = "", max_edits: int = 4,
                              launch: bool = False, profile: str = "",
                              trace_id=None):
    cfg = await _get_config()
    parts: List[str] = []
    scan = await _call("perf.scan")
    summary = {}
    if isinstance(scan, dict) and scan.get("findings") is not None:
        summary = scan.get("summary", {})
        # only ACTUAL problems (crit/warn) — not healthy ok/info statuses
        probs = [f for f in scan.get("findings", [])
                 if isinstance(f, dict) and f.get("severity") in ("crit", "warn")]
        if probs:
            parts.append("PERF FINDINGS:\n" + json.dumps(probs[:20],
                                                         default=str)[:4000])
    stalls = await _call("perf.stalls", limit=30)
    if isinstance(stalls, dict):
        # perf.stalls → {events:[...]}; 'stalls' is a COUNT. Keep real stall/hang.
        evs = [e for e in (stalls.get("events") or [])
               if isinstance(e, dict) and e.get("kind") in ("stall", "hang")]
        if evs:
            parts.append("EVENT-LOOP STALLS:\n" + json.dumps(evs[:15],
                                                            default=str)[:2500])
    errs = await _call("dream.sensor.syslog_errors", limit=40)
    if isinstance(errs, dict) and errs.get("sample"):
        parts.append("RECENT ERRORS:\n" + json.dumps(errs["sample"][:30],
                                                     default=str)[:3500])
    if not parts:
        return {"ok": True, "findings_summary": summary, "edits": [],
                "note": "no perf findings / errors to act on — system looks healthy"}
    edits = await _distill_edits("\n\n".join(parts),
                                 provider or cfg["editor_provider"],
                                 limit=int(max_edits))
    out = {"ok": True, "findings_summary": summary, "edits": edits}
    if launch and edits:
        pr = await evolve_pipeline_run(kind="code",
                                       profile=(profile or cfg["default_profile"]),
                                       edits=edits)
        out["pipeline_id"] = pr.get("id")
    await emit_event({"type": "evolve.observe.scan", "edits": len(edits),
                      "launched": bool(launch and edits)})
    return out


# ═════════════════════════════════════════════════════════════════════════════
# ERRORS WORK-QUEUE — errors flow in → get a suggested fix → human approves →
# a gated code pipeline commits it. The Bun "errors-as-work-queue → race to
# green" model: the observability systems (perf, event-loop, syslog) and the
# ollama/workers monitors don't MOVE here — they *push* their errors in via
# evolve.errors.ingest, and Loop Lab distils each into an edit the human can
# approve. Approval launches the normal branch→edit→sandbox-test→merge pipeline.
# ═════════════════════════════════════════════════════════════════════════════
KEY_ERRORS  = "vera:evolve:errors"       # hash: id -> error-item JSON
ERRORS_CAP  = 200                        # keep the newest N items

# lifecycle states, in flow order (drives the panel's kanban columns)
ERR_STATES = ["new", "suggested", "approved", "applied", "dismissed"]


def _err_sig(source: str, title: str) -> str:
    """Stable dedup signature: same source + normalised message = same error
    (so a repeating log line increments a counter instead of flooding)."""
    norm = re.sub(r"\d+", "#", (title or "").lower())          # digits → #
    norm = re.sub(r"0x[0-9a-f]+", "#", norm)                    # hex addrs
    norm = re.sub(r"\s+", " ", norm).strip()[:160]
    import hashlib
    return hashlib.sha1(f"{source}|{norm}".encode()).hexdigest()[:16]


async def _err_all() -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return []
    try:
        raw = await r.hgetall(KEY_ERRORS)
    except Exception:
        return []
    items = []
    for v in (raw or {}).values():
        try:
            items.append(json.loads(v.decode() if isinstance(v, bytes) else v))
        except Exception:
            continue
    items.sort(key=lambda x: x.get("updated", x.get("ts", "")), reverse=True)
    return items


async def _err_get(eid: str) -> Optional[Dict[str, Any]]:
    r = _redis()
    if not r:
        return None
    try:
        raw = await r.hget(KEY_ERRORS, eid)
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw) if raw else None
    except Exception:
        return None


async def _err_put(item: Dict[str, Any]):
    r = _redis()
    if not r:
        return
    item["updated"] = now_iso()
    try:
        await r.hset(KEY_ERRORS, item["id"], json.dumps(item, default=str))
        # trim to the newest ERRORS_CAP by dropping oldest 'updated'
        allrows = await _err_all()
        for stale in allrows[ERRORS_CAP:]:
            await r.hdel(KEY_ERRORS, stale["id"])
    except Exception as e:
        log.debug("evolve err put: %s", e)


async def _err_ingest_one(source: str, title: str, detail: str = "",
                          meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Insert or dedup-bump a single error. Returns the stored item."""
    sig = _err_sig(source, title)
    for it in await _err_all():
        if it.get("sig") == sig and it.get("state") not in ("dismissed",):
            it["count"] = int(it.get("count", 1)) + 1
            it["last_seen"] = now_iso()
            if detail:
                it["detail"] = detail[:4000]
            if meta:
                # refresh meta (esp. run_id) so the panel's "open the run this
                # came from" link always points at the LATEST occurrence, not
                # whichever run happened to create the item first.
                it["meta"] = {**(it.get("meta") or {}), **meta}
            await _err_put(it)
            return it
    item = {"id": "err_" + uuid.uuid4().hex[:10], "ts": now_iso(),
            "source": (source or "manual")[:40], "sig": sig,
            "title": (title or "error")[:200], "detail": (detail or title)[:4000],
            "meta": meta or {}, "count": 1, "state": "new",
            "suggestion": None, "pipeline_id": "", "last_seen": now_iso()}
    await _err_put(item)
    await emit_event({"type": "evolve.errors.new", "id": item["id"],
                      "source": item["source"], "title": item["title"]})
    return item


@capability("evolve.errors.ingest", memory="on",
            http_method="POST", http_path="/evolve/errors/ingest", http_tags=["evolve"],
            description="Send an error INTO Loop Lab's work-queue so it can suggest "
                        "a fix a human approves. This is the entry point the "
                        "observability + ollama/workers monitors call to flow their "
                        "errors here (they stay where they are; they just push). "
                        "Repeats dedup into a counter. Inputs: source (str — e.g. "
                        "'ollama','workers','perf','syslog','manual'), title (str! — "
                        "the error/short message), detail (str — full text/trace), "
                        "meta (dict — e.g. node, component), suggest (bool default "
                        "True — immediately distil a fix). Output: {ok, id, item}.")
async def evolve_errors_ingest(source: str = "manual", title: str = "",
                               detail: str = "", meta: Optional[Dict[str, Any]] = None,
                               suggest: bool = True, trace_id=None):
    if not (title or detail).strip():
        return {"error": "title (or detail) required"}
    item = await _err_ingest_one(source, title or detail[:120], detail, meta)
    if suggest and item.get("state") == "new":
        item = await _err_suggest(item)
    return {"ok": True, "id": item["id"], "item": item}


async def _err_suggest(item: Dict[str, Any], provider: str = "") -> Dict[str, Any]:
    # A perf finding with a built-in safe remediation (perf.remediate) doesn't
    # need an LLM code edit — the fix is a one-click, self-contained action.
    rid = (item.get("meta") or {}).get("remediation_id")
    if rid:
        item["suggestion"] = {"area": (item.get("meta") or {}).get("component", ""),
                              "suggestion": f"Apply the built-in safe remediation "
                                            f"'{rid}' (perf.remediate).",
                              "remediation_id": rid, "kind": "remediation"}
        item["state"] = "suggested"
        await _err_put(item)
        await emit_event({"type": "evolve.errors.suggested", "id": item["id"],
                          "area": item["suggestion"]["area"], "remediation_id": rid})
        return item
    cfg = await _get_config()
    text = (f"COMPONENT: {item.get('meta', {}).get('component', '')}\n"
            f"SOURCE: {item.get('source')}\n"
            f"ERROR: {item.get('title')}\n\n{item.get('detail', '')}")
    edits = await _distill_edits(text, provider or cfg["editor_provider"], limit=1)
    item["suggestion"] = edits[0] if edits else None
    item["state"] = "suggested" if edits else item.get("state", "new")
    await _err_put(item)
    if edits:
        await emit_event({"type": "evolve.errors.suggested", "id": item["id"],
                          "area": edits[0].get("area", "")})
    return item


@capability("evolve.errors.suggest", memory="on",
            http_method="POST", http_path="/evolve/errors/suggest", http_tags=["evolve"],
            description="Distil a concrete code fix for a queued error (moves it "
                        "new → suggested). Input: id (str!), provider (str — the "
                        "editor model). Output: {ok, item}.")
async def evolve_errors_suggest(id: str = "", provider: str = "", trace_id=None):
    item = await _err_get(id)
    if not item:
        return {"error": f"no such error: {id}"}
    item = await _err_suggest(item, provider)
    return {"ok": True, "item": item}


@capability("evolve.errors.list", memory="off", silent=True,
            http_method="GET", http_path="/evolve/errors", http_tags=["evolve"],
            description="The errors work-queue, newest first — for the errors→edits→"
                        "commits board. Query: state (str filter — new|suggested|"
                        "approved|applied|dismissed), source (str filter), limit "
                        "(int=100). Output: {items, counts:{state:n}, total}.")
async def evolve_errors_list(state: str = "", source: str = "", limit: int = 100,
                             trace_id=None):
    items = await _err_all()
    counts = {s: 0 for s in ERR_STATES}
    for it in items:
        counts[it.get("state", "new")] = counts.get(it.get("state", "new"), 0) + 1
    if state:
        items = [i for i in items if i.get("state") == state]
    if source:
        items = [i for i in items if i.get("source") == source]
    return {"items": items[:int(limit)], "counts": counts, "total": len(items)}


@capability("evolve.errors.approve", memory="on",
            http_method="POST", http_path="/evolve/errors/approve", http_tags=["evolve"],
            description="Human-approve a suggested fix: launches a gated code "
                        "pipeline (branch → edit → sandbox test → manual merge) from "
                        "the error's suggestion, so the fix flows to a commit. Moves "
                        "the error suggested → approved. Inputs: id (str!), profile "
                        "(str — target loop for the test gate), auto_promote (bool "
                        "default False — keep a human in the loop at merge). Output: "
                        "{ok, item, pipeline_id}.")
async def evolve_errors_approve(id: str = "", profile: str = "",
                                auto_promote: bool = False, trace_id=None):
    item = await _err_get(id)
    if not item:
        return {"error": f"no such error: {id}"}
    if not item.get("suggestion"):
        item = await _err_suggest(item)
    if not item.get("suggestion"):
        return {"error": "no suggestion to approve — could not distil a fix"}
    # Safe built-in remediation → apply it directly (perf.remediate), no pipeline.
    rid = (item.get("suggestion") or {}).get("remediation_id") \
        or (item.get("meta") or {}).get("remediation_id")
    if rid:
        res = await _call("perf.remediate", remediation_id=rid)
        ok = bool(isinstance(res, dict) and res.get("ok"))
        item["state"] = "applied" if ok else "suggested"
        item["remediation_result"] = (res or {}).get("detail") or (res or {}).get("error")
        await _err_put(item)
        await _audit("errors.remediate", f"{item['title'][:80]} → {rid} (ok={ok})",
                     error_id=id, remediation_id=rid, ok=ok)
        await emit_event({"type": "evolve.errors.approved", "id": id,
                          "remediation_id": rid, "applied": ok})
        return {"ok": ok, "item": item, "remediation_id": rid,
                "detail": item["remediation_result"]}
    cfg = await _get_config()
    # Gate the remediation against the loop that ACTUALLY failed, not whatever
    # the caller happens to default to — an approve from the Errors tab never
    # passes profile explicitly, so without this a "coding" test's fix would
    # get tested against the "planning" default, validating the wrong loop.
    item_profile = (item.get("meta") or {}).get("profile") or ""
    pr = await evolve_pipeline_run(kind="code",
                                   profile=(profile or item_profile or cfg["default_profile"]),
                                   edits=[item["suggestion"]],
                                   auto_promote=bool(auto_promote))
    item["state"] = "approved"
    item["pipeline_id"] = pr.get("id", "")
    await _err_put(item)
    await _audit("errors.approve", f"{item['title'][:80]} → pipeline {pr.get('id')}",
                 error_id=id, pipeline_id=pr.get("id"))
    await emit_event({"type": "evolve.errors.approved", "id": id,
                      "pipeline_id": pr.get("id", "")})
    return {"ok": True, "item": item, "pipeline_id": pr.get("id", "")}


@capability("evolve.errors.dismiss", memory="on",
            http_method="POST", http_path="/evolve/errors/dismiss", http_tags=["evolve"],
            description="Dismiss a queued error (won't be re-suggested; a fresh "
                        "occurrence re-opens it). Input: id (str!). Output: {ok}.")
async def evolve_errors_dismiss(id: str = "", trace_id=None):
    item = await _err_get(id)
    if not item:
        return {"error": f"no such error: {id}"}
    item["state"] = "dismissed"
    await _err_put(item)
    await emit_event({"type": "evolve.errors.dismissed", "id": id})
    return {"ok": True}


@capability("evolve.errors.clear", memory="on",
            http_method="POST", http_path="/evolve/errors/clear", http_tags=["evolve"],
            description="Purge the errors work-queue — hard-delete items (use to "
                        "reset a polluted queue). Input: state (str — only clear this "
                        "state, e.g. 'dismissed'; blank = ALL). Output: {ok, removed}.")
async def evolve_errors_clear(state: str = "", trace_id=None):
    r = _redis()
    if not r:
        return {"error": "redis unavailable"}
    removed = 0
    for it in await _err_all():
        if state and it.get("state") != state:
            continue
        try:
            await r.hdel(KEY_ERRORS, it["id"])
            removed += 1
        except Exception:
            continue
    await emit_event({"type": "evolve.errors.cleared", "removed": removed,
                      "state": state or "all"})
    return {"ok": True, "removed": removed}


@capability("evolve.errors.sync", memory="on",
            http_method="POST", http_path="/evolve/errors/sync", http_tags=["evolve"],
            description="Pull the current observability signals (perf.scan, "
                        "perf.stalls, syslog errors) INTO the errors work-queue as "
                        "discrete, deduped items and (optionally) suggest a fix for "
                        "each — the bridge that lets the perf/observe systems feed "
                        "Loop Lab without moving them. Inputs: suggest (bool default "
                        "True), provider (str), limit (int=20 — max new items). "
                        "Output: {ok, ingested, suggested}.")
async def evolve_errors_sync(suggest: bool = True, provider: str = "",
                             limit: int = 20, trace_id=None):
    ingested = 0
    suggested = 0
    feeds: List[Dict[str, Any]] = []
    # perf.scan findings carry a severity crit|warn|info|ok — ONLY crit/warn are
    # actual problems. info/ok are healthy "all clear" statuses ("no loop stall
    # for 15 min", "no zombie running jobs") and must NEVER become work items.
    scan = await _call("perf.scan")
    if isinstance(scan, dict):
        for f in (scan.get("findings") or []):
            if not isinstance(f, dict) or f.get("severity") not in ("crit", "warn"):
                continue
            rem = f.get("remediation") or ""
            feeds.append({
                "source": "perf",
                "title": str(f.get("title") or f.get("id") or "perf issue")[:200],
                "detail": (str(f.get("detail") or "")
                           + (f"\n\nSafe remediation available: {rem}" if rem else ""))[:2000],
                "meta": {"component": f.get("area", ""),
                         "severity": f.get("severity"),
                         "remediable": bool(f.get("remediable")),
                         "remediation_id": f.get("remediation_id") or ""}})
    # perf.stalls returns {events:[{kind,ts,stalled_ms,where,stack}], count,
    # hangs, stalls(int), worst_ms} — iterate EVENTS, and only real stall/hang
    # rows (skip 'note'). (The 'stalls' key is a COUNT, not a list.)
    stalls = await _call("perf.stalls", limit=30)
    if isinstance(stalls, dict):
        for e in (stalls.get("events") or []):
            if not isinstance(e, dict) or e.get("kind") not in ("stall", "hang"):
                continue
            feeds.append({
                "source": "stalls",
                "title": (f"event-loop {e.get('kind')} "
                          f"{e.get('stalled_ms') or '?'}ms @ "
                          f"{e.get('where') or 'unknown'}")[:200],
                "detail": (str(e.get("stack") or "")
                           or json.dumps(e, default=str))[:2000],
                "meta": {"component": e.get("where", "event-loop"),
                         "severity": "crit" if e.get("kind") == "hang" else "warn"}})
    errs = await _call("dream.sensor.syslog_errors", limit=40)
    if isinstance(errs, dict):
        for e in (errs.get("sample") or []):
            msg = e.get("message") if isinstance(e, dict) else str(e)
            if not str(msg or "").strip():
                continue
            feeds.append({"source": "syslog", "title": str(msg)[:200],
                          "detail": json.dumps(e, default=str)[:2000],
                          "meta": {"component": (e.get("unit", "")
                                                 if isinstance(e, dict) else ""),
                                   "severity": "warn"}})
    for f in feeds[:limit]:
        before = await _err_get_by_sig(_err_sig(f["source"], f["title"]))
        item = await _err_ingest_one(f["source"], f["title"], f["detail"], f["meta"])
        if not before:
            ingested += 1
            if suggest:
                item = await _err_suggest(item, provider)
                if item.get("suggestion"):
                    suggested += 1
    await emit_event({"type": "evolve.errors.sync", "ingested": ingested,
                      "suggested": suggested})
    return {"ok": True, "ingested": ingested, "suggested": suggested,
            "queue": (await evolve_errors_list()).get("counts")}


async def _err_get_by_sig(sig: str) -> Optional[Dict[str, Any]]:
    for it in await _err_all():
        if it.get("sig") == sig:
            return it
    return None


# ═════════════════════════════════════════════════════════════════════════════
# DEV SANDBOX — an isolated Vera on another port that runs the BRANCH's code
# ═════════════════════════════════════════════════════════════════════════════
# So code changes are tested for real (running, reloaded) before they touch
# main. Design:
#   • git WORKTREE of the branch at <repo>/.loop-lab-worktrees/<branch> — prod's
#     working tree stays on main; the worktree holds the branch's code.
#   • a generated docker-compose.dev.yml runs `vera-dev` from the vera:latest
#     image (deps baked in) with the worktree BIND-MOUNTED over /app/Vera, on a
#     separate port (VERA_DEV_PORT, default 8998) and an ISOLATED Redis DB
#     (…/3) so its state never collides with prod's DB 0.
#   • evolve.sandbox.snapshot copies prod's evolve config/tasks into the dev DB
#     so the sandbox tests with the same suite.
#   • pipelines of kind=code, when the sandbox is healthy, run the benchmark
#     suite THROUGH the sandbox's own /evolve HTTP API (the branch's code) and
#     gate on that — then promote (merge) or roll back (delete branch+worktree).
#
# Everything degrades gracefully: no docker / no compose → clear error, and the
# code pipeline simply holds for manual review instead of failing.

import httpx  # noqa: E402

KEY_SANDBOX = "vera:evolve:sandbox"           # current sandbox descriptor
# Prod's own host port — the dev sandbox must never bind this one.
PROD_PORT = int(os.getenv("ORCHESTRATOR_PORT", "8999"))
# Dev-sandbox HOST port. Default 8998 (NOT prod's 8999, or `sandbox up` collides
# with the running Vera). The live value comes from evolve.config.dev_port
# (panel-settable) — _dev_port() refreshes this cache before each sandbox op so
# the sync helpers (_dev_base_url / _dev_compose_yaml) can stay synchronous.
DEV_PORT = int(os.getenv("VERA_DEV_PORT", "8998"))
_DEV_PORT_ACTIVE = DEV_PORT
DEV_REDIS_DB = int(os.getenv("VERA_DEV_REDIS_DB", "3"))


async def _dev_port() -> int:
    """Resolve + cache the dev-sandbox host port (config dev_port → env → 8998).
    Rejects prod's port and out-of-range values so a misconfig can't recreate
    the 8999 collision."""
    global _DEV_PORT_ACTIVE
    try:
        cfg = await _get_config()
        p = int(cfg.get("dev_port") or DEV_PORT)
        if 1024 <= p <= 65535 and p != PROD_PORT:
            _DEV_PORT_ACTIVE = p
    except Exception:
        pass
    return _DEV_PORT_ACTIVE
DEV_IMAGE = os.getenv("VERA_WORKER_IMAGE", "vera:latest")  # local-only, never pulled
_WORKTREE_DIR = ".loop-lab-worktrees"
_DEV_COMPOSE = "docker-compose.dev.yml"


def _safe_branch(branch: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", branch.replace(BRANCH_PREFIX, "")).strip("-")


async def _sh(cmd: List[str], cwd: Optional[str] = None,
              timeout: int = 240) -> Dict[str, Any]:
    def _run():
        try:
            p = subprocess.run(cmd, cwd=cwd or str(_repo_root()),
                               capture_output=True, text=True, timeout=timeout)
            return {"ok": p.returncode == 0, "out": p.stdout.strip(),
                    "err": p.stderr.strip(), "code": p.returncode}
        except FileNotFoundError as e:
            return {"ok": False, "out": "", "err": f"not found: {e}", "code": -1}
        except subprocess.TimeoutExpired:
            return {"ok": False, "out": "", "err": "command timed out", "code": -1}
        except Exception as e:
            return {"ok": False, "out": "", "err": str(e), "code": -1}
    return await asyncio.get_event_loop().run_in_executor(None, _run)


def _git_wt_argv(wt: str, *args: str) -> List[str]:
    """git argv for a command run INSIDE a sandbox worktree, made resilient to
    "detected dubious ownership". The worktree is often created by a different
    UID than the Vera process runs as (the host shell / a manual repair), and
    modern git then refuses every operation there — status/diff degrade into the
    misleading `--no-index` usage dump ("git diff failed: … Not a git
    repository"). Marking the path (and '*') safe per-invocation fixes it without
    mutating global config or depending on any earlier heal having run."""
    return ["git", "-c", f"safe.directory={wt}", "-c", "safe.directory=*", *args]


async def _remove_worktree_robust(wt_abs: str) -> Dict[str, Any]:
    """Remove a git worktree, resilient to ROOT-OWNED files a container left
    behind. The dev containers run as root and bind-mount the worktree, so a
    `pip install`/build inside them drops root-owned paths there. Vera runs as an
    unprivileged user, so `git worktree remove --force` then fails with
    Permission denied and the sandbox teardown reports worktree_removed=false —
    the exact bug seen tearing down a spawned pool container.

    Strategy: try the clean git removal first; on failure, delete the tree from
    INSIDE a throwaway root `alpine` container that bind-mounts ONLY the parent
    `.loop-lab-worktrees` dir (so a bad path can't reach outside it), then
    `git worktree prune` to clear the now-dangling admin entry. Guard-railed:
    refuses any path not under _WORKTREE_DIR and any suspicious leaf, so it can
    never rm the main checkout or an arbitrary directory."""
    wt = str(wt_abs).replace("\\", "/").rstrip("/")
    r = await _git("worktree", "remove", "--force", wt, timeout=120)
    if r.get("ok"):
        return {"ok": True, "method": "git", "detail": ""}
    parent, _, leaf = wt.rpartition("/")
    # Safety: only ever operate inside a loop-lab worktree with a sane leaf name.
    if (_WORKTREE_DIR not in wt or not leaf
            or not re.fullmatch(r"[A-Za-z0-9._-]+", leaf)
            or not parent.endswith(_WORKTREE_DIR)):
        return {"ok": False, "method": "git",
                "detail": r.get("err") or "unsafe path — refusing root rm"}
    rm = await _sh(["docker", "run", "--rm", "-v", f"{parent}:/wt", "alpine",
                    "sh", "-c", f"rm -rf /wt/{leaf}"], timeout=120)
    await _git("worktree", "prune", timeout=60)
    gone = not Path(wt).exists()
    return {"ok": gone, "method": "root-container" if gone else "failed",
            "detail": ((rm.get("err") or "") + " " + (r.get("err") or "")).strip()}


def _host_for_docker(value: str) -> str:
    """Rewrite loopback host references so they resolve from INSIDE a container:
    prod (running natively on the host) reaches its backing services on
    localhost's published ports, but 'localhost' inside the sandbox container is
    the container itself. host.docker.internal is mapped to the docker
    host-gateway via extra_hosts in the generated compose."""
    return re.sub(r"(?<![\w.])(localhost|127\.0\.0\.1)(?![\w.])",
                  "host.docker.internal", value or "")


def _redis_url_with_db(url: str, db: int) -> str:
    """Force the db path component of a redis URL. NEVER pass db= as a kwarg to
    from_url alongside a URL that may carry its own /n path — redis-py's
    ConnectionPool.from_url does kwargs.update(url_options), so the URL's db
    silently wins and the 'isolated' client lands on the wrong database."""
    m = re.match(r"^(rediss?://[^/?]+)", url or "")
    return f"{m.group(1) if m else 'redis://localhost:6379'}/{int(db)}"


def _dev_compose_yaml(worktree_rel: str, name: str = "vera-dev",
                      port: int = None, db: int = None) -> str:
    """A compose override defining a dev-sandbox container. Parameterized per
    branch (name/port/db) so MANY can run concurrently; the defaults reproduce
    the original single `vera-dev` on the active port + DEV_REDIS_DB, so existing
    callers are unchanged. Prod image, branch source bind-mounted, isolated port +
    Redis DB, shares the other backing services.

    Backing-service endpoints are INHERITED from prod's own live config, not
    hard-coded compose service names: this estate runs redis/postgres/chroma in
    a different compose project, so names like `redis:6379` don't resolve on
    vera_vera-net — the sandbox came up with every backing service dead, its
    loops persisted events nowhere, and the Loop Lab timeline sat on 'Waiting
    for events…' until the idle watchdog killed the run. Loopback hosts are
    rewritten to host.docker.internal (docker host-gateway) so the sandbox uses
    the same published ports prod itself connects through."""
    if port is None:
        port = _DEV_PORT_ACTIVE
    if db is None:
        db = DEV_REDIS_DB
    c = getattr(_orch, "cfg", None)
    redis_url = _redis_url_with_db(
        _host_for_docker(getattr(c, "REDIS_URL", "redis://localhost:6379")),
        db)
    pg_url = _host_for_docker(getattr(
        c, "POSTGRES_URL", "postgresql://admin:admin@localhost:5433/postgres"))
    chroma_host = _host_for_docker(getattr(c, "CHROMA_HOST", "localhost"))
    chroma_port = int(getattr(c, "CHROMA_PORT", 8008) or 8008)
    neo4j_uri = _host_for_docker(getattr(c, "NEO4J_URI", "bolt://localhost:7687"))
    # prod's TLS cert dir on the host — mounted read-only so the sandbox serves the
    # SAME cert (browser trusts it identically → embeddable).
    _tls_dir = os.path.dirname(getattr(c, "TLS_CERTFILE",
                                       os.path.expanduser("~/.vera/tls/cert.pem")))
    # For IN-CONTAINER git (so you can commit from the container's VS Code): the
    # worktree's `.git` file references host paths (`<repo>/.git/worktrees/<name>`
    # and the worktree's own host path), so mount the main `.git` AND the worktree
    # at its HOST-ABSOLUTE path — then every git-worktree pointer resolves exactly
    # as on the host. The worktree is ALSO at /app/Vera (the app's import root).
    _repo_abs = str(_repo_root())
    _wt_abs = str((_repo_root() / worktree_rel).resolve())
    return f"""# Auto-generated by Loop Lab (evolve.sandbox.up). Safe to delete.
services:
  {name}:
    image: {DEV_IMAGE}
    pull_policy: never
    container_name: {name}
    command: ["python", "-m", "Vera.vera.capability_orchestration"]
    ports:
      - "{port}:8999"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      ORCHESTRATOR_HOST: "0.0.0.0"
      ORCHESTRATOR_PORT: "8999"
      # HTTPS like prod, using prod's SAME cert (mounted read-only below) so the
      # browser trusts it identically and a sandbox UI can be EMBEDDED same-securely
      # in the prod UI (no mixed-content block). The cert must carry a SAN covering
      # how you reach it (llm.int etc.) — regenerate with TLS_EXTRA_SANS if not.
      TLS_ENABLED: "1"
      TLS_CERTFILE: "/certs/cert.pem"
      TLS_KEYFILE: "/certs/key.pem"
      REDIS_URL: "{redis_url}"
      POSTGRES_URL: "{pg_url}"
      CHROMA_HOST: "{chroma_host}"
      CHROMA_PORT: "{chroma_port}"
      NEO4J_URI: "{neo4j_uri}"
      NEO4J_USER: "{getattr(c, 'NEO4J_USER', 'neo4j')}"
      NEO4J_PASS: "{getattr(c, 'NEO4J_PASS', 'neo4j')}"
      OLLAMA_GPU_URL: "{_host_for_docker(getattr(c, 'OLLAMA_GPU_URL', 'http://192.168.0.250:11435'))}"
      OLLAMA_CPU_A_URL: "{_host_for_docker(getattr(c, 'OLLAMA_CPU_A_URL', 'http://192.168.0.246:11435'))}"
      OLLAMA_CPU_B_URL: "{_host_for_docker(getattr(c, 'OLLAMA_CPU_B_URL', 'http://192.168.0.247:11435'))}"
      OLLAMA_EMBED_URL: "{_host_for_docker(getattr(c, 'OLLAMA_EMBED_URL', 'http://192.168.0.246:11435'))}"
      OLLAMA_MODEL: "{getattr(c, 'OLLAMA_MODEL', '')}"
      VERA_IS_DEV_SANDBOX: "1"
      # Dev containers ARE the sanctioned place for sys.dev.* (restart/env) — a
      # sandbox needs to restart its own process to pick up module-import changes.
      # Prod never sets this by default, so enable it here (the skill relied on it
      # being on but it wasn't — an agent couldn't restart its own sandbox).
      VERA_DEV_MODE: "1"
      EMBED_CAPS_ON_START: "0"
      SYSLOG_MONITOR: "0"
      # Cross-process Ollama GPU gate ('one big queue'): inherit prod's setting
      # so a dev container joins the SAME queue when prod has it on, and pin the
      # coordination DB to the shared 0 (its data lives on an isolated DB).
      VERA_OLLAMA_GATE: "{os.getenv('VERA_OLLAMA_GATE', '0')}"
      VERA_COORD_REDIS_DB: "0"
    volumes:
      - ./{worktree_rel}:/app/Vera:rw
      - {_tls_dir}:/certs:ro
      # in-container git: the main .git + the worktree at its HOST-ABSOLUTE path,
      # so `git` inside the container (e.g. the VS Code terminal) resolves the
      # worktree pointers and can commit. Git's worktree locking keeps concurrent
      # containers/prod safe (separate index/HEAD per worktree, shared objects).
      # safe.directory='*' is baked into the image (Dockerfile) so the root-user
      # container isn't blocked by git's dubious-ownership guard on host-owned files.
      - {_repo_abs}/.git:{_repo_abs}/.git:rw
      - {_wt_abs}:{_wt_abs}:rw
    restart: "no"
    networks:
      - vera-net
networks:
  vera-net:
    external: true
    name: vera_vera-net
"""


def _dev_base_url() -> str:
    return f"http://localhost:{_DEV_PORT_ACTIVE}"


_DEV_SCHEME_CACHE: Dict[int, str] = {}   # port -> "https" | "http" (resolved once)


async def _dev_resolve_base(port: int = 0, host: str = "localhost") -> Optional[str]:
    """Resolve a running sandbox's base URL by SCHEME. Dev containers now serve
    HTTPS (prod's self-signed cert), but a container spawned before that — e.g.
    another agent's live one — may still be HTTP, so try https (verify off, the
    cert is self-signed) then http and cache the winner per port. None if neither
    answers /health. This keeps the http/https transition non-breaking."""
    port = int(port or _DEV_PORT_ACTIVE)
    cached = _DEV_SCHEME_CACHE.get(port)
    order = ([cached] if cached else []) + [s for s in ("https", "http") if s != cached]
    for scheme in order:
        base = f"{scheme}://{host}:{port}"
        try:
            async with httpx.AsyncClient(timeout=5, verify=False) as cl:
                r = await cl.get(base + "/health")
            if r.status_code < 500:
                _DEV_SCHEME_CACHE[port] = scheme
                return base
        except Exception:
            continue
    return None


# ── Per-branch dev containers (§4, Phase C): spawn ADDITIONAL sandboxes alongside
#    the primary vera-dev, each on an allocated port + Redis DB. Additive — the
#    single-container up path above is untouched.
from Vera.vera.evolve.sandbox_pool import (          # noqa: E402
    container_name as _pool_cname,
    alloc_port as _pool_alloc_port,
    alloc_db as _pool_alloc_db,
)
from Vera.vera.evolve.sandbox_reap import (          # noqa: E402
    plan_reap as _plan_reap,
    orphan_composes as _orphan_composes,
    is_trunk_protected as _is_trunk_protected,
)

KEY_SANDBOX_POOL = "vera:evolve:sandbox:pool"         # hash: slug -> per-branch descriptor


async def _host_bound_ports() -> set:
    """Host TCP ports currently LISTENing — skipped when allocating a sandbox port."""
    import re as _re
    r = await _sh(["sh", "-c", "ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null"])
    return {int(m.group(1)) for m in _re.finditer(r":(\d{4,5})\b", r.get("out", "") or "")}


async def _sandbox_pool() -> Dict[str, Dict[str, Any]]:
    """The per-branch sandbox descriptors (slug -> {branch,name,port,redis_db,…})."""
    r = _redis()
    if not r:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    try:
        for k, v in (await r.hgetall(KEY_SANDBOX_POOL) or {}).items():
            k = k.decode() if isinstance(k, (bytes, bytearray)) else k
            out[k] = json.loads(v.decode() if isinstance(v, (bytes, bytearray)) else v)
    except Exception:
        pass
    return out


@capability("evolve.sandbox.spawn", memory="on",
            http_method="POST", http_path="/evolve/sandbox/spawn", http_tags=["evolve"],
            description="Spawn an ADDITIONAL per-branch dev container ALONGSIDE the "
                        "primary vera-dev and any others — its own worktree, container "
                        "(vera-dev-<slug>), and an allocated host port + Redis DB from "
                        "the pool. Additive: never disturbs existing sandboxes. Inputs: "
                        "branch (str!), rebuild_image (bool). Output: {ok, name, port, "
                        "redis_db, branch, url, reachable}.")
async def evolve_sandbox_spawn(branch: str = "", rebuild_image: bool = False, trace_id=None):
    branch = (branch or "").strip()
    if not branch:
        return {"error": "branch required"}
    safe = _safe_branch(branch)
    name = _pool_cname(branch)
    wt_rel = f"{_WORKTREE_DIR}/{safe}"
    wt_abs = _repo_root() / _WORKTREE_DIR / safe
    # allocate a free host port (skip anything LISTENing) + a free Redis DB
    port = _pool_alloc_port(await _host_bound_ports())
    if not port:
        return {"error": "no free dev port in the pool (8980–8998)"}
    pool = await _sandbox_pool()
    used_dbs = {DEV_REDIS_DB} | {int(d["redis_db"]) for d in pool.values()
                                if d.get("redis_db") is not None and d.get("slug") != safe}
    db = _pool_alloc_db(used_dbs)
    if db is None:
        return {"error": "no free Redis DB in the pool (3–15)"}
    wt = await _ensure_worktree(branch)
    if wt.get("error"):
        return {"error": wt["error"], "hint": "branch checked out elsewhere? (git worktree list)"}
    ens = await _call("docker.image.ensure", host_id="", image=DEV_IMAGE, force=bool(rebuild_image))
    if not (isinstance(ens, dict) and ens.get("ok")):
        return {"error": f"{DEV_IMAGE} unavailable: {(ens or {}).get('error', ens)}"}
    compose_file = f"docker-compose.dev-{safe}.yml"
    try:
        (_repo_root() / compose_file).write_text(
            _dev_compose_yaml(wt_rel, name=name, port=port, db=db), encoding="utf-8")
    except Exception as e:
        return {"error": f"could not write {compose_file}: {e}"}
    up = await _sh(["docker", "compose", "-f", "docker-compose.yml", "-f", compose_file,
                    "-p", name, "up", "-d", "--no-build", "--force-recreate", name], timeout=300)
    if not up["ok"]:
        return {"error": f"docker compose up failed: {up['err'] or up['out']}",
                "hint": "requires docker + the vera:latest image on this host"}
    desc = {"branch": branch, "slug": safe, "name": name, "port": port, "redis_db": db,
            "compose": compose_file, "worktree": str(wt_abs), "started_at": now_iso()}
    r = _redis()
    if r:
        try:
            await r.hset(KEY_SANDBOX_POOL, safe, json.dumps(desc, default=str))
        except Exception:
            pass
    reachable = False
    scheme = "http"
    for _ in range(30):
        await asyncio.sleep(3)
        # scheme-agnostic (curl -k for the self-signed HTTPS dev cert; falls to http)
        pr = await _sh(["curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}",
                        f"https://localhost:{port}/mcp/tools"], timeout=8)
        if (pr.get("out", "") or "").strip() in ("200", "401", "403"):
            reachable, scheme = True, "https"; break
        pr = await _sh(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                        f"http://localhost:{port}/mcp/tools"], timeout=8)
        if (pr.get("out", "") or "").strip() in ("200", "401", "403"):
            reachable, scheme = True, "http"; break
    if reachable:
        _DEV_SCHEME_CACHE[port] = scheme
    await _audit("sandbox.spawn", f"{name} for {branch} on :{port} db{db} ({scheme}, reachable={reachable})",
                 branch=branch, name=name, port=port)
    await emit_event({"type": "evolve.sandbox.spawned", "branch": branch, "name": name,
                      "port": port, "redis_db": db, "reachable": reachable})
    return {"ok": True, "name": name, "port": port, "redis_db": db, "branch": branch,
            "url": f"{scheme}://localhost:{port}", "scheme": scheme,
            "reachable": reachable, "worktree": str(wt_abs)}


@capability("evolve.bleeding_edge.container.ensure", memory="on",
            http_method="POST", http_path="/evolve/bleeding_edge/container/ensure",
            http_tags=["evolve"],
            description="Bring up (or confirm) the STANDING bleeding-edge container — "
                        "a persistent dev sandbox permanently tracking the bleeding-edge "
                        "mirror, distinct from the ephemeral per-pipeline sandbox every "
                        "branch gets via evolve.pipeline.begin. Pinned so the idle-reaper "
                        "never pauses it. Refreshed automatically right after any "
                        "successful evolve.pipeline.promote into bleeding-edge — this "
                        "call is for bringing it up the first time (or after a manual "
                        "teardown), not routine refresh. Idempotent. Input: "
                        "rebuild_image (bool). Output: same shape as evolve.sandbox.spawn.")
async def evolve_bleeding_edge_container_ensure(rebuild_image: bool = False, trace_id=None):
    mirror = await _refresh_bleeding_edge_mirror()
    if mirror.get("error"):
        return {"error": mirror["error"]}
    up = await evolve_sandbox_spawn(branch=BLEEDING_EDGE_MIRROR_BRANCH,
                                    rebuild_image=rebuild_image)
    if up.get("error"):
        return up
    name = up.get("name", "")
    if name:
        await evolve_sandbox_pin(name=name, on=True)
    return up


async def _refresh_standing_bleeding_edge_container() -> Dict[str, Any]:
    """Fast-forward the bleeding-edge mirror, then restart the standing
    bleeding-edge container if it's currently up, so it actually picks up
    the new tip — matching the mirror-branch pattern used everywhere else in
    Loop Lab. A no-op (not an error) if the standing container was never
    brought up: evolve.bleeding_edge.container.ensure is opt-in, not implied
    by every promote. Called from evolve.pipeline.promote right after a
    successful merge into bleeding-edge; never raises."""
    mirror = await _refresh_bleeding_edge_mirror()
    if mirror.get("error"):
        return {"ok": False, "error": mirror["error"]}
    pool = await _sandbox_pool()
    entry = pool.get(_safe_branch(BLEEDING_EDGE_MIRROR_BRANCH))
    if not entry or not entry.get("name"):
        return {"ok": True, "action": "mirror refreshed; standing container not up"}
    name = entry["name"]
    r = await _sh(["docker", "restart", name])
    if not r.get("ok"):
        return {"ok": False, "error": f"container restart failed: {r.get('err') or r.get('out')}"}
    return {"ok": True, "action": "mirror refreshed + container restarted", "name": name}


# The cap Loop Lab actually routes into the sandbox (loop tasks). If the
# sandbox's registry is missing it, the sandbox is up but NOT READY to serve —
# routing a run there would 404 with "Unknown capability: loops.run". This is
# exactly the stale-image case: an old vera:latest that predates loops.run /
# agent_loop_v6+ registers far fewer caps than the current code.
_SANDBOX_READY_CAP = os.getenv("VERA_SANDBOX_READY_CAP", "loops.run")


async def _diagnose_stale(n_tools: int) -> Dict[str, Any]:
    """The sandbox is up but MISSING its workhorse cap. The cap count alone can't
    tell WHY — and the three causes need different fixes. Interrogate the worktree
    on disk (which the orchestrator can read directly) to pick the right one:

      • branch  — the worktree's branch is behind main, so its source genuinely
                  predates the cap. Fix: update the branch (no image rebuild).
      • mount   — the worktree source HAS the cap but the running container does
                  not, i.e. the container is serving an OLD bind-mount. Fix:
                  recreate the container (down → up; up now force-recreates).
      • image   — the source doesn't have the cap and we can't attribute it to a
                  behind branch: the baked image / checked-out code is stale.
                  Fix: rebuild the image.

    Returns {cause, error} merged into the probe result so callers (and the UI)
    stop blaming the image by default."""
    cap = _SANDBOX_READY_CAP
    sb = await _get_sandbox()
    branch = sb.get("branch", "?")
    wt = sb.get("worktree", "")
    behind: Optional[int] = None
    src_has = False
    if wt and Path(wt).exists():
        rc = await _sh(_git_wt_argv(wt, "rev-list", "--count", "HEAD..main"), cwd=wt)
        if rc.get("ok"):
            try:
                behind = int((rc.get("out") or "0").strip() or "0")
            except Exception:
                behind = None
        # Is the cap defined anywhere in the worktree's tracked source?
        g = await _sh(_git_wt_argv(wt, "grep", "-l", "-F", f'"{cap}"', "HEAD"), cwd=wt)
        src_has = bool(g.get("ok") and (g.get("out") or "").strip())
    if behind and behind > 0 and not src_has:
        return {"cause": "branch",
                "error": f"dev sandbox is missing '{cap}': its branch ({branch}) is "
                         f"{behind} commit(s) behind main, so its source predates "
                         f"the cap. Update the branch — no image rebuild needed."}
    if src_has:
        return {"cause": "mount",
                "error": f"dev sandbox is up but its container is serving a STALE "
                         f"MOUNT — the worktree source has '{cap}' but the running "
                         f"container predates it. Recreate the container (sandbox "
                         f"down → up; up force-recreates). No image rebuild needed."}
    return {"cause": "image",
            "error": f"dev sandbox is up but MISSING '{cap}' (only {n_tools} caps) — "
                     f"the {DEV_IMAGE} image / checked-out source predates it. "
                     f"Rebuild the image (🛠 Rebuild image & recreate)."}


_SANDBOX_CONTAINER = "vera-dev"
# How long the sandbox container can sit with no test/dev activity before the
# idle sweep pauses it. `docker pause` (SIGSTOP-equivalent) freezes every
# process inside — including the ambient background jobs is_dev_sandbox()
# doesn't stop from STARTING (dream's director loop, third-party schedulers
# added later, etc.) — without losing container state, so resume is a plain
# unpause + a few seconds' wait, not a full evolve.sandbox.up rebuild.
_SANDBOX_IDLE_PAUSE_S = int(os.getenv("VERA_SANDBOX_IDLE_PAUSE_S", "1800"))
KEY_SANDBOX_PINNED = "vera:evolve:sandbox:pinned"      # set: pinned container names (never auto-paused)
KEY_SANDBOX_ACTIVITY = "vera:evolve:sandbox:activity"  # hash: container name -> last-activity iso
_SANDBOX_KEEP_ALWAYS = {"vera-dev-code"}               # the VS Code sidecar is not an Ollama consumer


async def _sandbox_pool_touch(name: str) -> None:
    """Mark a SPAWNED container as just-used so the idle-reaper keeps it warm
    (the primary uses _sandbox_touch/KEY_SANDBOX instead)."""
    r = _redis()
    if not r or not name:
        return
    try:
        await r.hset(KEY_SANDBOX_ACTIVITY, name, now_iso())
    except Exception:
        pass


async def _sandbox_pinned() -> set:
    r = _redis()
    if not r:
        return set()
    try:
        vals = await r.smembers(KEY_SANDBOX_PINNED) or set()
        return {v.decode() if isinstance(v, (bytes, bytearray)) else v for v in vals}
    except Exception:
        return set()


async def _sandbox_unpause_if_paused(name: str) -> bool:
    """Auto-resume a paused container before docker-exec'ing into it. The idle
    reaper may have frozen it; this makes over-pausing HARMLESS — the next real
    use transparently wakes it. (fs/diff read the host worktree and never need
    the container running, so exec is the only path that must unpause.)"""
    if not name:
        return False
    st = await _sh(["docker", "inspect", "-f", "{{.State.Status}}", name], timeout=10)
    if (st.get("out") or "").strip() == "paused":
        await _sh(["docker", "unpause", name], timeout=15)
        return True
    return False


async def _sandbox_touch() -> None:
    """Record that the sandbox was just actually used — the idle-pause sweep's
    only signal that something real (a test, a manual exec) is happening in
    there, as opposed to the container merely existing."""
    r = _redis()
    if not r:
        return
    try:
        sb = await _get_sandbox()
        if sb:
            sb["last_activity"] = now_iso()
            await r.set(KEY_SANDBOX, json.dumps(sb, default=str))
    except Exception:
        pass


async def _sandbox_container_status() -> str:
    """docker inspect's raw state: 'running' | 'paused' | 'exited' | '' (absent)."""
    r = await _sh(["docker", "inspect", "-f", "{{.State.Status}}", _SANDBOX_CONTAINER],
                 timeout=15)
    return r["out"].strip() if r["ok"] else ""


async def _sandbox_ensure_unpaused() -> bool:
    """If the container is paused (idle-swept), unpause it and give it a moment
    to resume serving before the caller's own health probe runs. No-op (True)
    if it's already running or doesn't exist — the normal up/down paths handle
    those. Returns False only on a genuine unpause failure."""
    if await _sandbox_container_status() != "paused":
        return True
    r = await _sh(["docker", "unpause", _SANDBOX_CONTAINER], timeout=15)
    if r["ok"]:
        await _audit("sandbox.resume", "auto-unpaused on demand (idle sweep had paused it)")
        await emit_event({"type": "evolve.sandbox.resumed"})
        await asyncio.sleep(2)  # let its event loop actually start servicing requests
    return r["ok"]


async def _sandbox_probe(auto_unpause: bool = True) -> Dict[str, Any]:
    """Confirm a HEALTHY, READY Vera is serving the dev-sandbox port before we
    route caps into it. A bare /health 2xx is NOT enough, and neither is a 200
    from /mcp/tools: during boot (and on a STALE image) the tools surface answers
    200 while the capability registry is still partial — so a run routed there
    then 404s with "Unknown capability: loops.run" (the exact
    'sandbox call loops.run: 404 for http://localhost:8998/mcp/call' failure).
    We now require (1) /health 2xx/3xx, (2) /mcp/tools mounted, AND (3) the
    workhorse cap (_SANDBOX_READY_CAP) actually present in the registry. If it's
    up but missing that cap, reachable=False + stale=True with a REBUILD hint, so
    'prefer' falls back to in-process and 'require' blocks with a clear reason
    instead of a cryptic 404.

    `auto_unpause=False` (used by passive status/UI polling) reports a paused
    container as simply unreachable-because-paused WITHOUT waking it — the
    whole point of idle-pausing is defeated if a status panel poll wakes the
    sandbox back up every few seconds. Only a REAL routing decision
    (_resolve_sandbox, an explicit exec/pause/resume call) should wake it."""
    await _dev_port()          # refresh the active port from config first
    if await _sandbox_container_status() == "paused":
        if not auto_unpause:
            return {"reachable": False, "paused": True,
                    "error": "sandbox is paused (idle) — resumes automatically "
                             "on the next real use, or evolve.sandbox.resume now"}
        if not await _sandbox_ensure_unpaused():
            return {"reachable": False, "error": "sandbox is paused (idle) and failed to "
                                                 "unpause — check docker on the host"}
    base = await _dev_resolve_base()
    if not base:
        return {"reachable": False,
                "error": f"no Vera (http or https) on the dev-sandbox port {_DEV_PORT_ACTIVE}"}
    try:
        async with httpx.AsyncClient(timeout=6, verify=False) as c:
            h = await c.get(base + "/health")
            if h.status_code >= 400:
                return {"reachable": False, "status_code": h.status_code,
                        "error": f"/health {h.status_code} — no healthy Vera on {base}"}
            # /health answers 200 even with every backing service down — parse
            # the body. Redis is a HARD requirement: the sandbox persists its
            # loop run-state + events there and the prod mirror streams the
            # timeline from it; without it a routed run executes blind (the UI
            # sits on 'Waiting for events…' until the idle watchdog kills it).
            hj: Dict[str, Any] = {}
            try:
                hj = h.json() if isinstance(h.json(), dict) else {}
            except Exception:
                hj = {}
            if hj and not hj.get("redis", True):
                dead = [k for k in ("redis", "postgres", "chroma", "neo4j")
                        if not hj.get(k, True)]
                return {"reachable": False, "stale": False, "cause": "services",
                        "status_code": h.status_code, "services_down": dead,
                        "error": f"dev sandbox is up but cannot reach its backing "
                                 f"services ({', '.join(dead)}) — its loop events "
                                 f"have nowhere to persist, so runs routed there "
                                 f"stream nothing and die on the idle watchdog. "
                                 f"Recreate it (evolve.sandbox.up) so the compose "
                                 f"env is regenerated with prod's real endpoints."}
            # The /mcp/call route _sandbox_call uses lives on the same router as
            # /mcp/tools; if tools 404s the dev port is not a full Vera.
            t = await c.get(base + "/mcp/tools")
        if t.status_code >= 400:
            return {"reachable": False, "status_code": t.status_code,
                    "error": f"/mcp/tools {t.status_code} on {base} — dev-sandbox "
                             "port is not serving Vera's MCP surface (no isolated "
                             "Vera up here)"}
        # Parse the tool list and confirm the workhorse cap is registered. The
        # list is a bare JSON array of {name, …}.
        names: set = set()
        try:
            for tool in (t.json() or []):
                nm = (tool or {}).get("name") if isinstance(tool, dict) else None
                if nm:
                    names.add(nm)
        except Exception:
            names = set()
        n_tools = len(names)
        if _SANDBOX_READY_CAP and _SANDBOX_READY_CAP not in names:
            diag = await _diagnose_stale(n_tools)
            return {"reachable": False, "stale": True, "status_code": t.status_code,
                    "tool_count": n_tools, **diag}
        # Non-Redis services down = usable but degraded (loops that touch
        # pg/chroma/neo4j will limp) — surface it without blocking.
        degraded = [k for k in ("postgres", "chroma", "neo4j")
                    if hj and not hj.get(k, True)]
        return {"reachable": True, "status_code": t.status_code,
                "tool_count": n_tools, "stale": False, "error": "", "cause": "",
                "services_degraded": degraded}
    except Exception as e:
        return {"reachable": False, "error": str(e)[:160]}


async def _get_sandbox() -> Dict[str, Any]:
    r = _redis()
    if not r:
        return {}
    try:
        raw = await r.get(KEY_SANDBOX)
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw) if raw else {}
    except Exception:
        return {}


# ── Sandbox ACCESS: terminal + file explorer (the VS Code sidecar is
#    evolve.sandbox.code.attach) — all scoped to the WORKTREE / dev container,
#    never the real source.

async def _wt_jail(path: str, name: str = "", branch: str = "") -> Any:
    """Resolve `path` inside a sandbox worktree; refuse escapes. Targets the
    PRIMARY vera-dev worktree by default, or a SPAWNED per-branch sandbox by
    name/branch (same routing as evolve.sandbox.exec, so the file explorer /
    diff can reach ANY container — not just the primary). Returns a
    (root, absolute Path) tuple or {'error': ...}."""
    if (name or "").strip() or (branch or "").strip():
        tgt = await _resolve_exec_target(name, branch)
        if tgt.get("error"):
            return tgt
        wt = tgt.get("worktree", "")
        await _sandbox_pool_touch(tgt.get("container"))   # browsing files keeps it warm
    else:
        sb = await _get_sandbox()
        wt = sb.get("worktree", "")
    if not wt:
        return {"error": "no sandbox worktree — bring the sandbox up first "
                         "(evolve.sandbox.ensure)"}
    root = Path(wt).resolve()
    p = (root / (path or ".").lstrip("/\\")).resolve()
    if root != p and root not in p.parents:
        return {"error": f"path escapes the sandbox worktree: {path}"}
    return (root, p)


async def _resolve_exec_target(name: str = "", branch: str = "") -> Dict[str, Any]:
    """Which sandbox does an exec target? Primary vera-dev by default, or a
    SPAWNED per-branch container/worktree selected by container name/slug or
    branch. Returns {container, worktree} or {error}."""
    name = (name or "").strip()
    branch = (branch or "").strip()
    if not name and not branch:
        sb = await _get_sandbox()
        return {"container": _SANDBOX_CONTAINER, "worktree": sb.get("worktree", "")}
    for slug, d in (await _sandbox_pool()).items():
        if name in (d.get("name"), slug) or (branch and branch == d.get("branch")):
            return {"container": d.get("name"), "worktree": d.get("worktree", "")}
    sb = await _get_sandbox()
    if name == _SANDBOX_CONTAINER or (branch and branch == sb.get("branch")):
        return {"container": _SANDBOX_CONTAINER, "worktree": sb.get("worktree", "")}
    return {"error": f"no sandbox matching name='{name}' branch='{branch}' "
                     "(evolve.sandbox.list shows the running ones)"}


async def _branch_worktree(branch: str) -> str:
    """Filesystem path of the worktree checked out on `branch` — the sandbox POOL
    first (a running/paused dev container), else ANY plain git worktree created by
    `git worktree add`. This lets the unit-test GATE run for the NORMAL branch flow,
    not only pool sandboxes: the ephemeral test runner just bind-mounts the path, so
    a plain worktree (no running container) is enough. branch='' -> the primary."""
    branch = (branch or "").strip()
    tgt = await _resolve_exec_target("", branch)   # branch='' resolves the primary
    wt = tgt.get("worktree") or ""
    if wt and Path(wt).exists():
        return wt
    if branch:
        for w in await _list_worktrees():
            if w.get("branch") == branch and not w.get("is_main"):
                p = w.get("path", "")
                if p and Path(p).exists():
                    return p
    return ""


@capability("evolve.sandbox.exec", memory="on",
            http_method="POST", http_path="/evolve/sandbox/exec", http_tags=["evolve"],
            description="TERMINAL into a sandbox: run a shell command inside the "
                        "CONTAINER (docker exec — the branch's code, isolated Redis) "
                        "or, with where='worktree', in the branch worktree ON THE HOST "
                        "(where git works natively — use this to commit; a Windows/SMB "
                        "agent CAN'T run git in the worktree directly). Targets the "
                        "primary vera-dev by default; pass name (container/slug) or "
                        "branch to reach a SPAWNED per-branch sandbox (a second agent "
                        "uses this to reach its OWN container). Never the real source "
                        "tree. Inputs: cmd (str!), where (container|worktree), name "
                        "(str), branch (str), timeout (int=60). Output: {ok, out, err, "
                        "code, where, target}.",
            schema=enum_schema(where=["container", "worktree"]))
async def evolve_sandbox_exec(cmd: str = "", where: str = "container",
                              timeout: int = 60, name: str = "", branch: str = "",
                              trace_id=None):
    if not (cmd or "").strip():
        return {"error": "cmd required"}
    timeout = max(3, min(300, int(timeout)))
    # Route to the RIGHT sandbox — primary vera-dev by default, or a SPAWNED
    # per-branch container/worktree by name/branch. Without this, a second agent
    # on its own spawned container could only ever reach the primary (the
    # 'hardcoded to vera-dev' trap). where='worktree' runs on the HOST in that
    # branch's worktree — which is ALSO the git-over-SMB fix: a Windows/SMB agent
    # can't run git in the worktree directly (its .git points at a Linux host
    # path), so it commits by exec'ing here (git runs natively on the host).
    tgt = await _resolve_exec_target(name, branch)
    if tgt.get("error"):
        return tgt
    if where == "worktree":
        wt = tgt.get("worktree") or ""
        if not wt:
            return {"error": "no worktree for the target sandbox — bring it up first"}
        root = Path(wt).resolve()
        cwd_p = (root / ".").resolve()
        if root != cwd_p and root not in cwd_p.parents:
            return {"error": "worktree path escape"}
        res = await _sh(["sh", "-lc", cmd] if os.name != "nt"
                        else ["cmd", "/c", cmd], cwd=str(root), timeout=timeout)
    else:
        # The idle-reaper may have frozen this spawned container — wake it first so
        # a terminal command transparently resumes it (over-pausing stays harmless).
        await _sandbox_unpause_if_paused(tgt["container"])
        res = await _sh(["docker", "exec", tgt["container"], "sh", "-lc", cmd],
                        timeout=timeout)
        if not res["ok"] and "No such container" in (res["err"] or ""):
            return {"error": f"{tgt['container']} container is not running — bring the "
                             "sandbox up first (evolve.sandbox.ensure/spawn)",
                    "where": where}
    await _audit("sandbox.exec", f"[{where}:{tgt.get('container')}] {cmd[:120]}", ok=res["ok"])
    await _sandbox_pool_touch(tgt.get("container"))   # keep this container warm
    return {"ok": res["ok"], "out": res["out"][-8000:], "err": res["err"][-2000:],
            "code": res["code"], "where": where, "target": tgt.get("container")}


# Pure arg-sanitising / docker-argv / summary-parsing for the ephemeral test runner
# below live in an app-free core so they're unit-testable without booting the app.
from Vera.vera.evolve.evolve_unittest_core import (   # noqa: E402
    sanitize_pytest_args as _ut_sanitize, build_inner_cmd as _ut_inner,
    build_docker_argv as _ut_argv, parse_pytest_output as _ut_parse,
)


@capability("evolve.unittest.run", memory="off",
            http_method="POST", http_path="/evolve/unittest/run", http_tags=["evolve"],
            description="Run a branch's pytest suite in a FRESH, EPHEMERAL vera:latest "
                        "container (docker run --rm) — NEVER the sandbox container that's "
                        "serving HTTP, so it cannot hang the serving app (the real fix for "
                        "dev-lifecycle §8.3 #9). The branch worktree is mounted READ-ONLY at "
                        "/app/Vera with PYTHONPATH=/app:/app/Vera so BOTH Vera.vera.* and "
                        "lowercase vera.* imports bind to the BRANCH code; no bytecode is written "
                        "back (no root-owned .pyc pollution). pytest is pip-installed inside the "
                        "throwaway if the image lacks it (bake it in via requirements-dev.txt to "
                        "skip). MUST run on the managing instance (prod/native, which has docker) — "
                        "a sandbox container has no docker socket. Inputs: branch (str — defaults "
                        "to the primary sandbox's branch), paths (str='tests'), markers (str, e.g. "
                        "'critical'), extra (str — extra pytest flags), timeout (int=600), "
                        "repo (str — a registered non-vera repo runs ITS OWN test_cmd in its "
                        "checkout instead of Vera pytest; default '' / 'vera' keeps the ephemeral "
                        "pytest path). Output: {ok, summary, code, out, repo, branch, ...}.")
async def evolve_unittest_run(branch: str = "", paths: str = "tests", markers: str = "",
                              extra: str = "", timeout: int = 600, repo: str = "",
                              trace_id=None):
    # Non-Vera repo: run ITS OWN test_cmd in its checkout (the same gate the code
    # pipeline uses), so the Test tab can exercise ANY registered repo — not just
    # Vera. Vera (repo empty/'vera') keeps the ephemeral-container pytest below.
    if repo and repo != DEFAULT_REPO_ID:
        rec = (await _get_repos()).get(repo)
        if not rec:
            return {"error": f"unknown repo '{repo}' — register it via evolve.repo.add"}
        root = Path(rec.get("path", ""))
        if not root.exists():
            return {"error": f"repo '{repo}' path missing: {root}"}
        test_cmd = (rec.get("test_cmd") or DEFAULT_TEST_CMD).strip()
        if not test_cmd:
            return {"error": f"repo '{repo}' has no test_cmd — set one via evolve.repo.add"}
        g = await _repo_test_gate(str(root), test_cmd,
                                  timeout=max(30, min(1800, int(timeout))))
        if g.get("error"):
            return {"error": g["error"], "repo": repo}
        return {"ok": bool(g.get("passed")), "summary": g.get("summary", ""),
                "code": g.get("rc"), "out": g.get("output", ""),
                "repo": repo, "branch": "", "test_cmd": test_cmd}
    # 1. resolve the branch's worktree — pool sandbox OR any plain git worktree
    wt = await _branch_worktree(branch)
    if not wt:
        return {"error": f"no worktree for branch '{branch or '(primary)'}' — "
                         "git worktree add it, or spawn a sandbox (evolve.sandbox.spawn)"}
    # 2. sanitise the caller's pytest args (they're interpolated into a shell cmd)
    tokens, err = _ut_sanitize(paths, markers, extra)
    if err:
        return {"error": err}
    # 3. run the ephemeral test container. `docker` runs on THIS host (the managing
    #    instance) — a sandbox container has no docker socket, prod-native does.
    inner = _ut_inner(tokens)
    argv = _ut_argv(DEV_IMAGE, str(Path(wt).resolve()), inner)
    timeout = max(30, min(1800, int(timeout)))
    res = await _sh(argv, timeout=timeout)
    combined = ((res.get("out") or "") + "\n" + (res.get("err") or "")).strip()
    if res.get("code") == -1 and "not found" in (res.get("err") or "").lower():
        return {"error": "docker not available here — evolve.unittest.run must run on the "
                         "managing instance (prod/native), not inside a sandbox container",
                "detail": res.get("err")}
    # No RC marker means pytest never ran — the container itself failed (image
    # missing, bad volume, daemon error). Surface that instead of a bogus 0/0.
    if "__VERA_RC=" not in combined:
        return {"error": "ephemeral test container failed to run pytest",
                "detail": combined[-1500:], "code": res.get("code", -1)}
    parsed = _ut_parse(combined)
    label = branch or tgt.get("container") or "primary"
    await _audit("unittest.run", f"[{label}] {parsed['summary']}", ok=parsed["ok"])
    await emit_event({"type": "evolve.unittest.done", "branch": branch or label,
                      "ok": parsed["ok"], "passed": parsed["passed"],
                      "failed": parsed["failed"], "errors": parsed["errors"]})
    return {**parsed, "code": parsed["rc"], "image": DEV_IMAGE,
            "branch": branch or label, "out": combined[-8000:], "repo": DEFAULT_REPO_ID}


@capability("evolve.tests.matrix", memory="off", silent=True,
            http_method="GET", http_path="/evolve/tests/matrix", http_tags=["evolve"],
            description="Unit-test COVERAGE MATRIX: every module in tests/, its test count, and "
                        "whether it is in the CRITICAL gate tier (conftest _CRITICAL_MODULES — the "
                        "set the pre-merge gate runs and must keep green). Feeds the Loop Lab Tests "
                        "view. Pure collect-only — runs no test bodies. Output: {ok, "
                        "modules:[{module,tests,critical}], total_tests, total_modules, "
                        "critical_modules, critical_tests, critical_names}.")
async def evolve_tests_matrix(trace_id=None):
    root = str(_repo_root())
    coll = await _sh([sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q",
                      "--no-header", "-p", "no:cacheprovider"], cwd=root, timeout=120)
    modules: Dict[str, int] = {}
    for ln in (coll.get("out", "") or "").splitlines():
        ln = ln.strip()
        # pytest -q --collect-only prints one line per module: "tests/test_x.py: 27"
        mm2 = re.match(r"^(tests/[^:]+\.py):\s*(\d+)\s*$", ln)
        if mm2:
            base = os.path.basename(mm2.group(1))
            modules[base[:-3] if base.endswith(".py") else base] = int(mm2.group(2))
            continue
        # fallback (older pytest): one node id per line "tests/test_x.py::test_y"
        if ln.startswith("tests/") and "::" in ln:
            base = os.path.basename(ln.split("::", 1)[0])
            name = base[:-3] if base.endswith(".py") else base
            modules[name] = modules.get(name, 0) + 1
    critical: set = set()
    try:
        conf = (Path(root) / "tests" / "conftest.py").read_text(encoding="utf-8")
        mm = re.search(r"_CRITICAL_MODULES\s*=\s*\{([^}]*)\}", conf, re.S)
        if mm:
            critical = set(re.findall(r'"([^"]+)"', mm.group(1)))
    except Exception:
        pass
    mods = [{"module": k, "tests": v, "critical": k in critical}
            for k, v in sorted(modules.items())]
    return {"ok": True, "modules": mods,
            "total_tests": sum(modules.values()), "total_modules": len(mods),
            "critical_modules": sum(1 for x in mods if x["critical"]),
            "critical_tests": sum(x["tests"] for x in mods if x["critical"]),
            "critical_names": sorted(critical)}


# ── M3.4 test generation (evolve.tests.generate) ──────────────────────────────
_TEST_GEN_SYSTEM = (
    "You are a senior Python test engineer. You write focused, DETERMINISTIC "
    "pytest unit tests for PURE functions only — no network, no filesystem, no "
    "database, no async, no global state, no mocks unless trivial. You output ONLY "
    "the complete test file content: no prose, no explanation, no markdown fences."
)
_TEST_GEN_PROMPT = (
    "Write pytest unit tests for the pure functions in the module below.\n\n"
    "Source path: {path}\n"
    "The test file MUST start like this (repo convention — the Vera.vera namespace "
    "trap means only the lowercase import binds to the branch code):\n"
    "    import os, sys\n"
    "    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
    "    from {import_name} import <names>\n"
    "Target test file: {suggested_test}\n\n"
    "Rules:\n"
    "- Test ONLY pure, deterministic functions (inputs -> outputs). SKIP anything "
    "doing I/O, network, DB, async, or relying on global/mutable state.\n"
    "- Cover normal, edge, empty/None and boundary cases; descriptive test names, "
    "one behaviour per test.\n"
    "- Output ONLY the complete test file content, nothing else.\n\n"
    "Module source:\n{src}\n"
)


@capability("evolve.tests.generate", memory="off",
            http_method="POST", http_path="/evolve/tests/generate", http_tags=["evolve"],
            description="M3.4 TEST GENERATION: for a branch's changed pure-logic modules "
                        "(vera/**/*.py, excluding tests/ and __init__), LLM-PROPOSE pytest unit "
                        "tests that follow the repo's sys.path-insert + lowercase-import style so "
                        "new code can arrive with coverage. PROPOSES content for review in the Loop "
                        "Lab Unit-tests panel; does NOT write files (review, then save via a branch "
                        "commit / content.edit). LLM-backed — slow, one call per module. Inputs: "
                        "branch (str!), base (str — default bleeding-edge), module (str — one "
                        "explicit vera/ path, else the branch's changed modules), provider (str — "
                        "editor LLM), max_modules (int=3). Output: {ok, base, branch, "
                        "proposals:[{module, import_name, suggested_test, exists, tests_code}], count}.")
async def evolve_tests_generate(branch: str = "", base: str = "", module: str = "",
                                provider: str = "", max_modules: int = 3, trace_id=None):
    from Vera.vera.evolve.test_gen_core import (          # noqa: E402
        strip_code_fence as _strip_fence,
        test_target_for as _test_target,
        generatable_modules as _gen_mods,
    )
    branch = (branch or "").strip()
    if not branch:
        return {"error": "branch required"}
    root = str(_repo_root())
    base = (base or "").strip() or await _default_pipeline_base()
    if not (await _git("rev-parse", "--verify", f"refs/heads/{branch}", repo_root=root))["ok"]:
        return {"error": f"unknown branch: {branch}"}
    if module.strip():
        candidates = _gen_mods([module.strip()], max_n=1)
    else:
        df = await _git("diff", "--name-only", f"{base}...{branch}", repo_root=root)
        changed = [ln for ln in (df.get("out", "") or "").splitlines() if ln.strip()]
        candidates = _gen_mods(changed, max_n=max_modules)
    if not candidates:
        return {"ok": True, "base": base, "branch": branch, "proposals": [], "count": 0,
                "note": "no changed vera/*.py source modules to generate tests for"}
    proposals: List[Dict[str, Any]] = []
    for path in candidates:
        show = await _git("show", f"{branch}:{path}", repo_root=root)
        if not show.get("ok"):
            continue
        src = (show.get("out", "") or "")[:12000]        # bound the prompt
        tgt = _test_target(path)
        prompt = _TEST_GEN_PROMPT.format(path=path, import_name=tgt["import_name"],
                                         suggested_test=tgt["suggested_test"], src=src)
        gen = await _call("llm.generate", prompt=prompt, system=_TEST_GEN_SYSTEM,
                          model=(provider or None), job_type="chat",
                          caller="evolve.tests.generate")
        if isinstance(gen, dict) and gen.get("error"):
            proposals.append({"module": path, "import_name": tgt["import_name"],
                              "suggested_test": tgt["suggested_test"], "exists": False,
                              "tests_code": "", "error": gen["error"]})
            continue
        code = _strip_fence((gen or {}).get("text", "") if isinstance(gen, dict) else str(gen))
        proposals.append({"module": path, "import_name": tgt["import_name"],
                          "suggested_test": tgt["suggested_test"],
                          "exists": (Path(root) / tgt["suggested_test"]).exists(),
                          "tests_code": code})
    await emit_event({"type": "evolve.tests.generated", "branch": branch,
                      "count": len(proposals)})
    return {"ok": True, "base": base, "branch": branch,
            "proposals": proposals, "count": len(proposals)}


@capability("evolve.sandbox.fs.list", memory="off", silent=True,
            http_method="GET", http_path="/evolve/sandbox/fs/list", http_tags=["evolve"],
            description="FILE EXPLORER: list a directory inside a sandbox "
                        "worktree (the branch's copy of the source — never the "
                        "real tree). Targets the primary vera-dev by default; pass "
                        "name (container/slug) or branch to browse a SPAWNED "
                        "per-branch sandbox. Query: path (str — relative, default "
                        "root), name (str), branch (str). Output: {path, dirs:[..], "
                        "files:[{name,size}]}.")
async def evolve_sandbox_fs_list(path: str = "", name: str = "", branch: str = "",
                                 trace_id=None):
    j = await _wt_jail(path, name, branch)
    if isinstance(j, dict):
        return j
    root, p = j
    if not p.is_dir():
        return {"error": f"not a directory: {path}"}
    dirs, files = [], []
    try:
        for c in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if c.name in (".git", "__pycache__", "node_modules"):
                continue
            if c.is_dir():
                dirs.append(c.name)
            else:
                try:
                    files.append({"name": c.name, "size": c.stat().st_size})
                except Exception:
                    files.append({"name": c.name, "size": 0})
    except Exception as e:
        return {"error": str(e)}
    rel = str(p.relative_to(root)).replace("\\", "/")
    return {"path": "" if rel == "." else rel, "dirs": dirs, "files": files,
            "worktree": str(root)}


@capability("evolve.sandbox.fs.read", memory="off", silent=True,
            http_method="GET", http_path="/evolve/sandbox/fs/read", http_tags=["evolve"],
            description="FILE EXPLORER: read a file from the sandbox worktree. "
                        "Query: path (str!), max_bytes (int=120000). Output: "
                        "{path, content, truncated}.")
async def evolve_sandbox_fs_read(path: str = "", max_bytes: int = 120000,
                                 name: str = "", branch: str = "", trace_id=None):
    j = await _wt_jail(path, name, branch)
    if isinstance(j, dict):
        return j
    root, p = j
    if not p.is_file():
        return {"error": f"not a file: {path}"}
    try:
        data = p.read_bytes()
    except Exception as e:
        return {"error": str(e)}
    trunc = len(data) > int(max_bytes)
    return {"path": path, "content": data[:int(max_bytes)].decode("utf-8", "replace"),
            "truncated": trunc, "size": len(data)}


@capability("evolve.sandbox.fs.write", memory="on",
            http_method="POST", http_path="/evolve/sandbox/fs/write", http_tags=["evolve"],
            description="FILE EXPLORER: write a file INSIDE the sandbox worktree "
                        "(the branch's copy — the real source is never touched; "
                        "changes reach main only via pipeline promote). Inputs: "
                        "path (str!), content (str). Output: {ok, path, bytes}.")
async def evolve_sandbox_fs_write(path: str = "", content: str = "",
                                  name: str = "", branch: str = "", trace_id=None):
    if not path:
        return {"error": "path required"}
    j = await _wt_jail(path, name, branch)
    if isinstance(j, dict):
        return j
    root, p = j
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except Exception as e:
        return {"error": str(e)}
    await _audit("sandbox.fs.write", f"{path} ({len(content)}B)", path=path)
    await emit_event({"type": "evolve.sandbox.fs.write", "path": path,
                      "bytes": len(content)})
    return {"ok": True, "path": path, "bytes": len(content)}


async def _sandbox_routing_drift() -> Dict[str, Any]:
    """Compare the sandbox's EFFECTIVE loop-profile routing against prod's own —
    the live tripwire for the exact bug found 2026-08-03: evolve.sandbox.snapshot
    silently omitting the Ollama routing-override keys made every sandboxed
    loop test run its planner/controller/tier calls on CPU with a 13.79GB model
    (prod pins them to GPU + a 7GB model), with no error and no GPU activity —
    indistinguishable from a hang from any external vantage point, for HOURS.
    Snapshotting the right keys by default (see evolve.sandbox.snapshot) is the
    real fix; this is the belt-and-suspenders check so if that ever regresses —
    a future refactor drops a prefix, a custom snapshot call omits one, prod's
    routing changes after the sandbox was already created and never re-synced —
    it surfaces immediately as a clear status field instead of silently
    reproducing the same multi-hour mystery. Best-effort: returns {ok: True} if
    the sandbox is unreachable or either side's routing can't be read, rather
    than blocking sandbox.status on it."""
    try:
        prod_cap = CAPABILITY_REGISTRY.get("ollama.role_profiles.get")
        if not prod_cap:
            return {"ok": True, "checked": False}
        prod_rp = await prod_cap["func"]()
        prod_loop = ((prod_rp.get("effective") or {}).get("loop") or {}).get("roles") or {}
        sb_raw = await _sandbox_call("ollama.role_profiles.get", {}, timeout=15)
        if not isinstance(sb_raw, dict) or sb_raw.get("error"):
            return {"ok": True, "checked": False}
        sb_loop = ((sb_raw.get("effective") or {}).get("loop") or {}).get("roles") or {}
        mismatches = []
        for role in ("planner", "controller", "tier"):
            p, s = prod_loop.get(role) or {}, sb_loop.get(role) or {}
            if not p or not s:
                continue
            if bool(p.get("deny_gpu")) != bool(s.get("deny_gpu")) or \
                    (p.get("model") or "") != (s.get("model") or ""):
                mismatches.append({
                    "role": role,
                    "prod": {"deny_gpu": bool(p.get("deny_gpu")), "model": p.get("model") or ""},
                    "sandbox": {"deny_gpu": bool(s.get("deny_gpu")), "model": s.get("model") or ""},
                })
        if mismatches:
            return {"ok": False, "checked": True, "mismatches": mismatches,
                    "hint": "sandbox routing differs from prod for loop role(s) above — "
                            "loop tests here may be dramatically slower with no visible "
                            "error. Re-run evolve.sandbox.snapshot then recreate the "
                            "sandbox (evolve.sandbox.up) to pick up prod's current routing."}
        return {"ok": True, "checked": True, "mismatches": []}
    except Exception as e:
        return {"ok": True, "checked": False, "error": str(e)[:200]}


@capability("evolve.sandbox.status", memory="off", silent=True,
            http_method="GET", http_path="/evolve/sandbox/status", http_tags=["evolve"],
            description="State of the dev sandbox Vera: descriptor (branch, port, "
                        "worktree, last_activity) + a live health probe of the dev "
                        "port + a routing-drift check (does the sandbox's Ollama "
                        "role routing for planner/controller/tier still match prod's? "
                        "— see _sandbox_routing_drift's docstring for the incident "
                        "this guards against). A PAUSED sandbox (idle-swept — see "
                        "VERA_SANDBOX_IDLE_PAUSE_S) is reported as such WITHOUT being "
                        "woken by this check — only real use wakes it (the drift check "
                        "is skipped while paused, for the same reason). Output: "
                        "{sandbox, probe, up, paused, routing_drift}.")
async def evolve_sandbox_status(trace_id=None):
    port = await _dev_port()
    sb = await _get_sandbox()
    probe = await _sandbox_probe(auto_unpause=False) if sb else {"reachable": False}
    up = bool(sb) and probe.get("reachable", False)
    drift = await _sandbox_routing_drift() if up else {"ok": True, "checked": False}
    return {"sandbox": sb, "probe": probe,
            "up": up,
            "paused": bool(probe.get("paused")),
            "routing_drift": drift,
            "idle_pause_s": _SANDBOX_IDLE_PAUSE_S,
            "dev_port": port, "prod_port": PROD_PORT, "dev_redis_db": DEV_REDIS_DB}


@capability("evolve.sandbox.pause", memory="on",
            http_method="POST", http_path="/evolve/sandbox/pause", http_tags=["evolve"],
            description="Manually pause the dev sandbox container (docker pause — "
                        "freezes every process inside, including ambient background "
                        "jobs, without losing state). Same mechanism the idle sweep "
                        "uses automatically after VERA_SANDBOX_IDLE_PAUSE_S seconds "
                        "of no test/dev activity. Refuses while a run is actively "
                        "routed to the sandbox. Output: {ok, error}.")
async def evolve_sandbox_pause(trace_id=None):
    if _RUN_LIVE.get("where") == "sandbox" and _RUN_LIVE.get("run_id") in _BG_RUNS \
            and not _BG_RUNS[_RUN_LIVE["run_id"]].done():
        return {"ok": False, "error": "a run is actively using the sandbox right now — "
                                      "wait for it to finish first"}
    status = await _sandbox_container_status()
    if status != "running":
        return {"ok": False, "error": f"sandbox container is '{status or 'absent'}', not running"}
    r = await _sh(["docker", "pause", _SANDBOX_CONTAINER], timeout=15)
    if r["ok"]:
        await _audit("sandbox.pause", "paused manually")
        await emit_event({"type": "evolve.sandbox.paused", "manual": True})
    return {"ok": r["ok"], "error": r["err"] if not r["ok"] else ""}


@capability("evolve.sandbox.resume", memory="off",
            http_method="POST", http_path="/evolve/sandbox/resume", http_tags=["evolve"],
            description="Manually unpause the dev sandbox container. Normally "
                        "unnecessary — any real use (a test, an exec call) auto-"
                        "resumes it — but useful to pre-warm it before a burst of "
                        "testing. Output: {ok, error}.")
async def evolve_sandbox_resume(trace_id=None):
    if await _sandbox_container_status() != "paused":
        return {"ok": True, "error": "", "note": "was not paused"}
    ok = await _sandbox_ensure_unpaused()
    return {"ok": ok, "error": "" if ok else "docker unpause failed"}


@capability("evolve.sandbox.reap", memory="off",
            http_method="POST", http_path="/evolve/sandbox/reap", http_tags=["evolve"],
            description="Pause SPAWNED dev containers idle past the threshold "
                        "(VERA_SANDBOX_IDLE_PAUSE_S) so they stop sharing prod's Ollama "
                        "nodes — the policy 'only up if pinned or actively worked in'. "
                        "Exempts the primary, the VS Code sidecar, PINNED containers, and "
                        "branches with a live board dispatch. A paused container wakes "
                        "transparently on its next exec. Input: dry_run (bool=true — preview "
                        "the plan without pausing). Output: {ok, dry_run, "
                        "candidates:[{name,branch,idle_s,paused}], pinned, threshold_s}.")
async def evolve_sandbox_reap(dry_run: bool = True, trace_id=None):
    return await _sandbox_reap(dry_run=dry_run)


@capability("evolve.sandbox.pin", memory="off",
            http_method="POST", http_path="/evolve/sandbox/pin", http_tags=["evolve"],
            description="Pin/unpin a spawned dev container so the idle-reaper never "
                        "auto-pauses it (keep it up for a burst of work). Inputs: name "
                        "(str! — container name), on (bool=true to pin, false to unpin). "
                        "Output: {ok, name, pinned}.")
async def evolve_sandbox_pin(name: str = "", on: bool = True, trace_id=None):
    if not name:
        return {"error": "name required"}
    r = _redis()
    if not r:
        return {"error": "redis unavailable"}
    try:
        if on:
            await r.sadd(KEY_SANDBOX_PINNED, name)
        else:
            await r.srem(KEY_SANDBOX_PINNED, name)
    except Exception as e:
        return {"error": str(e)}
    await _audit("sandbox.pin", f"{'pinned' if on else 'unpinned'} {name}")
    await emit_event({"type": "evolve.sandbox.pin", "name": name, "pinned": bool(on)})
    return {"ok": True, "name": name, "pinned": bool(on)}


_SANDBOX_IDLE_SWEEP_INTERVAL_S = int(os.getenv("VERA_SANDBOX_IDLE_SWEEP_INTERVAL_S", "300"))


async def _sandbox_reap(dry_run: bool = False) -> Dict[str, Any]:
    """Pause SPAWNED dev containers idle past VERA_SANDBOX_IDLE_PAUSE_S — the
    policy 'a sandbox should only be up if it is pinned or actively being worked
    in' (each idle-but-running one is a full Vera quietly sharing prod's Ollama
    nodes). Exempt: the primary (its own sweep handles it), the VS Code sidecar,
    PINNED containers, branches with a live board dispatch, and anything used
    within the idle window. Safe by design — a wrongly-paused container is woken
    transparently on its next exec (_sandbox_unpause_if_paused)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    primary = _SANDBOX_CONTAINER
    pinned = await _sandbox_pinned()
    activity: Dict[str, str] = {}
    r = _redis()
    if r:
        try:
            h = await r.hgetall(KEY_SANDBOX_ACTIVITY) or {}
            activity = {(k.decode() if isinstance(k, (bytes, bytearray)) else k):
                        (v.decode() if isinstance(v, (bytes, bytearray)) else v)
                        for k, v in h.items()}
        except Exception:
            pass
    # branches with a live board dispatch = actively being worked -> never pause
    active_branches: set = set()
    try:
        bi = await _call("board.items")
        for it in (bi.get("items") if isinstance(bi, dict) else None) or []:
            if it.get("lane") == "in_progress" and it.get("branch"):
                active_branches.add(it["branch"])
    except Exception:
        pass
    pool = await _sandbox_pool()
    branch_by_name = {d.get("name"): d.get("branch", "") for d in pool.values()}
    ps = await _sh(["docker", "ps", "--filter", "name=vera-dev", "--format", "{{.Names}}"],
                   timeout=15)
    names = [ln.strip() for ln in (ps.get("out") or "").splitlines() if ln.strip()]
    plan: List[Dict[str, Any]] = []
    for name in names:
        if name == primary or name in _SANDBOX_KEEP_ALWAYS or name in pinned:
            continue
        insp = await _sh(["docker", "inspect", "-f",
                          "{{.State.Status}}|{{.State.StartedAt}}", name], timeout=10)
        status, _sep, started = (insp.get("out") or "").strip().partition("|")
        if status != "running":
            continue
        branch = branch_by_name.get(name, "")
        if branch and branch in active_branches:
            continue
        ref = activity.get(name) or started
        try:
            ref_dt = datetime.fromisoformat(str(ref).replace("Z", "+00:00"))
            idle_s = int((now - ref_dt).total_seconds())
        except Exception:
            idle_s = 0
        if idle_s < _SANDBOX_IDLE_PAUSE_S:
            continue
        plan.append({"name": name, "branch": branch, "idle_s": idle_s})
    if not dry_run:
        for p in plan:
            rr = await _sh(["docker", "pause", p["name"]], timeout=15)
            p["paused"] = bool(rr["ok"])
            if rr["ok"]:
                await _audit("sandbox.reap", f"idle-paused {p['name']} ({p['idle_s']}s idle)")
        n = sum(1 for p in plan if p.get("paused"))
        if n:
            await emit_event({"type": "evolve.sandbox.reaped", "count": n})
            log.info("evolve: idle-reaped %d spawned sandbox container(s)", n)
    return {"ok": True, "dry_run": dry_run, "candidates": plan,
            "pinned": sorted(pinned), "threshold_s": _SANDBOX_IDLE_PAUSE_S}


async def _sandbox_idle_sweep() -> None:
    """Scheduled: pause the sandbox container after VERA_SANDBOX_IDLE_PAUSE_S
    seconds with no test/dev activity (see _sandbox_touch/_resolve_sandbox).
    'It needs to be functional but if a loop test or development isn't
    happening it shouldn't be active' (2026-08-03) — an idle-but-RUNNING
    sandbox is a full Vera process quietly sharing prod's real Ollama nodes;
    pausing freezes it completely (see _SANDBOX_CONTAINER's docstring) until
    genuinely needed again."""
    try:
        # Reap idle SPAWNED containers every sweep (independent of the primary's
        # own idle state below) — this is what stops leftover per-branch sandboxes
        # from quietly sharing prod's Ollama nodes forever.
        await _sandbox_reap(dry_run=False)
        if _RUN_LIVE.get("where") == "sandbox" and _RUN_LIVE.get("run_id") in _BG_RUNS \
                and not _BG_RUNS[_RUN_LIVE["run_id"]].done():
            return  # actively in use right now — never pause out from under it
        sb = await _get_sandbox()
        if not sb:
            return
        if await _sandbox_container_status() != "running":
            return  # already paused/exited/absent — nothing to do
        last = sb.get("last_activity") or sb.get("created_at") or ""
        try:
            from datetime import datetime, timezone
            last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            idle_s = (datetime.now(timezone.utc) - last_dt).total_seconds()
        except Exception:
            idle_s = 0  # unknown last_activity — be conservative, don't pause
        if idle_s < _SANDBOX_IDLE_PAUSE_S:
            return
        r = await _sh(["docker", "pause", _SANDBOX_CONTAINER], timeout=15)
        if r["ok"]:
            await _audit("sandbox.pause", f"idle-paused after {int(idle_s)}s with no activity")
            await emit_event({"type": "evolve.sandbox.paused", "manual": False,
                              "idle_s": int(idle_s)})
            log.info("evolve: sandbox idle-paused after %ds inactivity", int(idle_s))
    except Exception as e:
        log.debug("sandbox idle sweep: %s", e)


schedule(_sandbox_idle_sweep, _SANDBOX_IDLE_SWEEP_INTERVAL_S, name="evolve.sandbox.idle_sweep")


def _fabric_sqlite_path(root: Optional[Path] = None) -> Path:
    """Mirrors data_fabric.py's own SQLITE_PATH resolution (FABRIC_SQLITE env
    override, else <repo>/vera/fabric/vera_fabric.db) so the snapshot targets
    the EXACT file data_fabric.py itself would open for whichever checkout
    `root` points at — prod's own root by default, a sandbox worktree when
    given one explicitly."""
    override = os.environ.get("FABRIC_SQLITE", "")
    if override and root is None:
        return Path(override)
    base = root or _repo_root()
    return base / "vera" / "fabric" / "vera_fabric.db"


def _sqlite_backup_sync(src_path: Path, dst_path: Path) -> Dict[str, Any]:
    """Blocking — always run via run_in_executor. Uses sqlite3's own backup
    API rather than a raw file copy: a live multi-GB db under WAL can be
    mid-write at any instant, and a plain copy risks grabbing a torn/corrupt
    snapshot. backup() takes the proper sqlite-level lock and streams a
    consistent copy regardless of concurrent writers on the source."""
    if not src_path.is_file():
        return {"ok": False, "error": f"source db not found: {src_path}"}
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if dst_path.exists():
        # The sandbox CONTAINER runs as root, and its own normal fabric
        # writes during a test (not just this snapshot) touch this same
        # file — so it can be root-owned again by the time the NEXT
        # snapshot runs, not just the first time. Found live 2026-08-03:
        # "attempt to write a readonly database" on a perfectly-0644 file,
        # because it was root:root and this process runs as boejaker.
        # boejaker has passwordless sudo (see .vera-ops precedent) so this
        # is a no-prompt, best-effort fix-up, not a hard dependency —
        # if sudo itself is unavailable the real error still surfaces below.
        try:
            subprocess.run(["sudo", "-n", "chown", "boejaker:boejaker", str(dst_path)],
                           capture_output=True, timeout=15, check=False)
        except Exception:
            pass
    src = sqlite3.connect(str(src_path))
    try:
        dst = sqlite3.connect(str(dst_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return {"ok": True, "bytes": dst_path.stat().st_size}


@capability("evolve.sandbox.snapshot", memory="off",
            http_method="POST", http_path="/evolve/sandbox/snapshot", http_tags=["evolve"],
            description="Copy a point-in-time snapshot of Loop Lab state (evolve "
                        "config + benchmark tasks), the Ollama routing overrides "
                        "(role_profiles/cap_routing/routing) from prod's Redis DB, "
                        "AND prod's real data-fabric SQLite db (vera_fabric.db — "
                        "fabric_records, mkt_strategies, everything memory.seek/"
                        "fabric.query/markets.strategy.list actually read) into the "
                        "sandbox. Without this a fresh sandbox's fabric layer is "
                        "whatever empty/default file its OWN worktree checkout "
                        "happens to have — found live 2026-08-03 chasing why "
                        "markets-sweep-propose took 32 minutes of honest but "
                        "unnecessary improvisation: markets.strategy.list returned "
                        "zero strategies in the sandbox despite 11 real ones on prod, "
                        "because the sqlite file lives INSIDE the repo tree "
                        "(vera/fabric/vera_fabric.db, relative to data_fabric.py) and "
                        "the sandbox's worktree is a separate checkout with its own "
                        "copy. The sqlite copy uses sqlite3's own backup API (safe "
                        "against a concurrently-written multi-GB source) and can take "
                        "real time on a large db — this call may run long; that's "
                        "normal, not a hang. Input: prefixes (csv, default "
                        "'vera:evolve:tasks,vera:evolve:config,vera:evolve:seeded,"
                        "vera:ollama:role_profiles,vera:ollama:cap_routing,"
                        "vera:ollama:routing'), sqlite (bool default True — set False "
                        "to skip the (slower) fabric db copy and only sync Redis).")
async def evolve_sandbox_snapshot(prefixes: str = "", sqlite: bool = True, trace_id=None):
    r = _redis()
    if not r:
        return {"error": "redis unavailable"}
    pats = [p.strip() for p in (prefixes or
            "vera:evolve:tasks,vera:evolve:config,vera:evolve:seeded,"
            "vera:ollama:role_profiles,vera:ollama:cap_routing,vera:ollama:routing"
            ).split(",") if p.strip()]
    copied = 0
    try:
        for pat in pats:
            # exact key or a SCAN pattern
            keys = [pat]
            if await r.exists(pat) == 0:
                keys = []
                cur = 0
                while True:
                    cur, batch = await r.scan(cur, match=pat + "*", count=200)
                    keys.extend([k.decode() if isinstance(k, bytes) else k for k in batch])
                    if cur == 0:
                        break
            for k in keys:
                try:
                    # COPY across DBs (Redis ≥6.2; this stack runs redis:7).
                    # REPLACE so re-snapshotting overwrites.
                    ok = await r.copy(k, k, destination_db=DEV_REDIS_DB, replace=True)
                    if ok:
                        copied += 1
                except Exception as e:
                    log.debug("snapshot copy %s: %s", k, e)
                    continue
    except Exception as e:
        return {"error": str(e), "copied": copied}
    fabric_db: Dict[str, Any] = {"ok": True, "skipped": "sqlite=False"}
    if sqlite:
        sb = await _get_sandbox()
        if sb and sb.get("worktree"):
            try:
                src = _fabric_sqlite_path()
                dst = _fabric_sqlite_path(Path(sb["worktree"]))
                fabric_db = await asyncio.get_event_loop().run_in_executor(
                    None, _sqlite_backup_sync, src, dst)
            except Exception as e:
                fabric_db = {"ok": False, "error": str(e)[:200]}
        else:
            fabric_db = {"ok": False, "skipped": "no sandbox worktree"}
    snapshotted_at = now_iso()
    try:
        sb2 = await _get_sandbox()
        if sb2:
            sb2["snapshotted_at"] = snapshotted_at
            await r.set(KEY_SANDBOX, json.dumps(sb2, default=str))
    except Exception:
        pass
    await emit_event({"type": "evolve.sandbox.snapshot", "copied": copied,
                      "db": DEV_REDIS_DB, "fabric_db": fabric_db})
    return {"ok": True, "copied": copied, "dev_redis_db": DEV_REDIS_DB,
            "fabric_db": fabric_db, "snapshotted_at": snapshotted_at}


@capability("evolve.sandbox.up", memory="on",
            http_method="POST", http_path="/evolve/sandbox/up", http_tags=["evolve"],
            description="Bring up the dev sandbox for a branch: create a git "
                        "worktree of the branch, generate docker-compose.dev.yml, "
                        "start the vera-dev container (port VERA_DEV_PORT, isolated "
                        "Redis DB), and snapshot Loop Lab state into it. Requires "
                        "docker on the orchestrator host. Inputs: branch (str — "
                        "OMIT to run against the CURRENT MAINLINE, which is "
                        "fast-forwarded to latest first; that is the default so a "
                        "Loop Lab test exercises today's Vera rather than whatever "
                        "branch was last activated. Pass a branch name only to "
                        "deliberately pin the sandbox to it — e.g. when re-running "
                        "a specific improvement or test — and it is then left "
                        "exactly where it is, never auto-moved), "
                        "snapshot (bool default True), "
                        "rebuild_image (bool default False — force-rebuild "
                        "vera:latest from source first; use when the sandbox is "
                        "running a STALE image missing newer caps like loops.run), "
                        "target (str default 'bleeding-edge' — which mirror to use "
                        "when branch is omitted: 'bleeding-edge' or 'main'. Falls "
                        "back to 'main' automatically if this repo has no "
                        "bleeding-edge branch.).")
async def evolve_sandbox_up(branch: str = "", snapshot: bool = True,
                            rebuild_image: bool = False, target: str = "bleeding-edge",
                            trace_id=None):
    # ── Default to the LATEST trunk, not "whatever was last activated" ───────
    # Requiring an explicit branch meant Loop Lab always tested some branch's
    # snapshot, and because _ensure_worktree short-circuits on an existing
    # directory, that snapshot never moved. So a "Loop Lab test" could silently
    # be exercising week-old code. The sensible default is the current trunk,
    # refreshed; running against a BRANCH is the deliberate act (pass one
    # explicitly — which is what re-running a specific improvement/test does).
    #
    # "The current trunk" is NOT the literal branch name (e.g. "main" or
    # "bleeding-edge") — git refuses to check the same branch out in a second
    # worktree while it's already held elsewhere (the primary checkout holds
    # main; the standing bleeding-edge container holds bleeding-edge), so that
    # always failed or hung. Target the matching dedicated mirror branch
    # instead (kept fast-forwarded nightly + opportunistically here) — same
    # effect, no collision. bleeding-edge is the default target
    # (2026-08-16 bleeding-edge-trunk-workflow) since it's now where Loop Lab
    # branches normally fork from and merge back to; main stays available via
    # target="main" for testing against the release branch specifically.
    _explicit = bool(branch)
    _mirror_refresh = None
    _mirror_branch_used = None
    if not branch:
        target = (target or "bleeding-edge").strip().lower()
        if target not in ("main", "bleeding-edge"):
            return {"error": f"unknown target '{target}' — expected 'main' or 'bleeding-edge'"}
        if target == "bleeding-edge":
            _mirror_refresh = await _refresh_bleeding_edge_mirror()
            if _mirror_refresh.get("error"):
                # No bleeding-edge branch in this repo (yet) — degrade to the
                # mainline mirror rather than hard-failing every sandbox.up.
                log.debug("evolve: bleeding-edge mirror unavailable (%s) — "
                         "falling back to mainline mirror",
                         _mirror_refresh["error"])
                target = "main"
                _mirror_refresh = None
            else:
                _mirror_branch_used = BLEEDING_EDGE_MIRROR_BRANCH
        if target == "main":
            _mirror_refresh = await _refresh_mainline_mirror()
            if _mirror_refresh.get("error"):
                return {"error": f"could not prepare the mainline mirror: "
                                 f"{_mirror_refresh['error']}"}
            _mirror_branch_used = MAINLINE_MIRROR_BRANCH
        branch = _mirror_branch_used
    # Resolve the configured host port up front. Refuse the prod port outright —
    # binding it is the "port 8999 already in use" failure — and point at the fix.
    port = await _dev_port()
    if port == PROD_PORT:
        return {"error": f"dev sandbox port {port} is prod's own port — change it "
                         f"(Sandbox → dev port, or evolve.config.set dev_port=…) "
                         f"to a free port such as 8998",
                "dev_port": port, "prod_port": PROD_PORT}
    safe = _safe_branch(branch)
    wt_rel = f"{_WORKTREE_DIR}/{safe}"
    wt_abs = _repo_root() / _WORKTREE_DIR / safe
    await emit_event({"type": "evolve.sandbox.up.start", "branch": branch,
                      "port": port})

    # 1. worktree for the branch (idempotent + self-healing — _ensure_worktree)
    wt_res = await _ensure_worktree(branch)
    if wt_res.get("error"):
        return {"error": wt_res["error"],
                "hint": "is the branch checked out elsewhere? "
                        "(git worktree list) — prod must stay on main"}
    # Bring the mainline worktree up to date. Only for the DEFAULT branch: a
    # feature branch is a deliberate, pinned choice and must not be moved under
    # the user. Best-effort — a local-only repo with no remote is normal.
    refreshed = None
    if not _explicit:
        # The mirror branch was already refreshed (from local mainline) above,
        # before the worktree even existed — _refresh_worktree pulls from a
        # REMOTE (origin/<branch>), which the mirror never has, so calling it
        # here too would just fail harmlessly but pointlessly. Reuse that
        # result instead of re-deriving it.
        refreshed = _mirror_refresh if branch in (MAINLINE_MIRROR_BRANCH, BLEEDING_EDGE_MIRROR_BRANCH) \
            else await _refresh_worktree(str(wt_abs), branch)
        await emit_event({"type": "evolve.sandbox.refresh", "branch": branch,
                          "ok": bool(refreshed.get("ok")),
                          "head": refreshed.get("head", ""),
                          "reason": refreshed.get("reason", "")})

    # 2. Ensure the vera:latest image exists LOCALLY before compose touches it.
    #    vera:latest is local-only (not on any registry), so `docker compose up`
    #    with the default "missing" pull policy tries a doomed registry pull
    #    ("pull access denied for vera") the moment the image isn't already on
    #    the host. docker.image.ensure builds it from this repo's Dockerfile (or
    #    transfers from the local daemon) instead — no pull. This is THE fix for
    #    "the sandbox is trying to pull a vera image that doesn't exist".
    #    rebuild_image=True forces a fresh build even when the image already
    #    exists — the fix for a sandbox stuck on a stale vera:latest.
    if rebuild_image:
        await emit_event({"type": "evolve.sandbox.image.rebuild", "image": DEV_IMAGE})
    ens = await _call("docker.image.ensure", host_id="", image=DEV_IMAGE,
                      force=bool(rebuild_image))
    if not (isinstance(ens, dict) and ens.get("ok")):
        await emit_event({"type": "evolve.sandbox.up.done", "branch": branch,
                          "healthy": False, "error": "image"})
        return {"error": f"{DEV_IMAGE} is not available and could not be built "
                         f"locally: {(ens or {}).get('error', ens)}",
                "hint": "vera:latest is local-only — it is built from the repo "
                        "Dockerfile, never pulled. Check docker is reachable and "
                        "the build succeeds (docker.image.ensure).",
                "ensure": ens if isinstance(ens, dict) else {"raw": str(ens)}}

    # 3. compose override
    try:
        (_repo_root() / _DEV_COMPOSE).write_text(_dev_compose_yaml(wt_rel),
                                                 encoding="utf-8")
    except Exception as e:
        return {"error": f"could not write {_DEV_COMPOSE}: {e}"}

    # 4. docker compose up vera-dev — image is now present, so no pull attempt.
    #    ALWAYS --force-recreate. The bind-mounted worktree can go stale
    #    INDEPENDENT of the image — the branch source changed, or the worktree dir
    #    was repaired — while compose still sees an unchanged service definition
    #    and would leave the OLD container (serving the OLD mount) running. That
    #    is exactly the "up but missing loops.run / 1024 caps" trap: a plain `up`
    #    reported "up to date" and never remounted. Recreating is cheap (no
    #    rebuild) and is the only reliable way `up` picks up new source.
    up_argv = ["docker", "compose", "-f", "docker-compose.yml",
               "-f", _DEV_COMPOSE, "up", "-d", "--no-build",
               "--force-recreate", "vera-dev"]
    up = await _sh(up_argv, timeout=300)
    if not up["ok"]:
        return {"error": f"docker compose up failed: {up['err'] or up['out']}",
                "hint": "requires docker + the vera:latest image on this host"}

    sb = {"branch": branch, "worktree": str(wt_abs), "port": port,
          "redis_db": DEV_REDIS_DB, "compose": _DEV_COMPOSE,
          "started_at": now_iso()}
    r = _redis()
    if r:
        await r.set(KEY_SANDBOX, json.dumps(sb, default=str))

    # 5. wait for readiness (up to ~90s — first boot embeds/loads modules). The
    #    probe requires the workhorse cap to be registered, so a STALE image
    #    (up but missing loops.run) never reports healthy — it reports why.
    healthy = False
    probe: Dict[str, Any] = {}
    for _ in range(30):
        await asyncio.sleep(3)
        probe = await _sandbox_probe()
        if probe.get("reachable"):
            healthy = True
            break

    # 5b. SELF-HEAL a genuinely stale image. We already force-recreated (so a
    #     stale MOUNT is impossible) — if the sandbox is still missing its
    #     workhorse cap AND the diagnosis is the baked image (not a behind
    #     branch), the image itself is out of date. Rebuild it ONCE, automatically
    #     — this is "keep the image up-to-date so it doesn't go stale": the
    #     sandbox refreshes itself instead of parking on the ▲ STALE banner
    #     waiting for someone to press the button. Bounded by the rebuild_image
    #     guard (no recursion) and gated by config for operators who'd rather not
    #     eat a multi-minute build inside `up`.
    if not healthy and probe.get("cause") == "image" and not rebuild_image:
        cfg = await _get_config()
        if cfg.get("auto_rebuild_stale", True):
            await emit_event({"type": "evolve.sandbox.image.autorebuild",
                              "image": DEV_IMAGE, "branch": branch})
            await _audit("sandbox.autorebuild",
                         f"{branch}: image stale (missing {_SANDBOX_READY_CAP}) — "
                         f"auto-rebuilding {DEV_IMAGE}")
            return await evolve_sandbox_up(branch=branch, snapshot=snapshot,
                                           rebuild_image=True, trace_id=trace_id)

    # 6. snapshot Loop Lab state into the dev DB
    snap = None
    if snapshot:
        snap = await evolve_sandbox_snapshot()

    await _audit("sandbox.up", f"{branch} on :{port} (healthy={healthy})",
                 branch=branch, healthy=healthy)
    await emit_event({"type": "evolve.sandbox.up.done", "branch": branch,
                      "healthy": healthy, "stale": bool(probe.get("stale"))})
    # A stale sandbox is a distinct, actionable failure — surface the cause-
    # specific diagnosis (branch behind / stale mount / stale image) rather than
    # the generic "give it a moment".
    if healthy:
        note = ""
    elif probe.get("stale"):
        note = probe.get("error") or "sandbox is stale"
    else:
        note = "container started but not answering /health yet — give it a " \
               "moment, then re-check status"
    return {"ok": True, "sandbox": sb, "healthy": healthy,
            "stale": bool(probe.get("stale")), "cause": probe.get("cause", ""),
            "url": _dev_base_url(), "snapshot": snap, "probe": probe, "note": note}


@capability("evolve.sandbox.ensure", memory="on",
            http_method="POST", http_path="/evolve/sandbox/ensure", http_tags=["evolve"],
            description="Ensure an ISOLATION sandbox is up so tests run out of "
                        "harm's way (the default posture). If one is already "
                        "reachable, no-op; otherwise create a loop-lab branch off "
                        "the current HEAD and bring the sandbox up on it. Input: "
                        "branch (str — reuse/name; default 'loop-lab/sandbox'), "
                        "rebuild_image (bool — force-rebuild vera:latest first). "
                        "Output: {ok, already_up, sandbox}.")
async def evolve_sandbox_ensure(branch: str = "", rebuild_image: bool = False,
                                trace_id=None):
    # A reachable (ready) sandbox is a no-op UNLESS a rebuild was explicitly
    # requested — then we fall through and bring it up with a fresh image.
    if not rebuild_image and (await _get_sandbox()) and (await _sandbox_probe()).get("reachable"):
        return {"ok": True, "already_up": True, "sandbox": await _get_sandbox()}
    br = (branch or f"{BRANCH_PREFIX}sandbox").strip()
    if not br.startswith(BRANCH_PREFIX):
        br = BRANCH_PREFIX + _safe_branch(br)
    # create the branch if it doesn't exist (idempotent — a bare `git branch`,
    # prod's working tree is never switched), then bring the sandbox up on it
    made = await evolve_branch_create(name=_safe_branch(br))
    target = made.get("branch", br) if isinstance(made, dict) else br
    up = await evolve_sandbox_up(branch=target, snapshot=True,
                                 rebuild_image=bool(rebuild_image))
    return {"ok": bool(up.get("ok")), "already_up": False, "sandbox": up.get("sandbox"),
            "healthy": up.get("healthy"), "stale": up.get("stale"), "error": up.get("error")}


@capability("evolve.sandbox.list", memory="off", silent=True,
            http_method="GET", http_path="/evolve/sandbox/list", http_tags=["evolve"],
            description="List ALL dev sandboxes — the primary vera-dev plus every "
                        "spawned per-branch container — each with branch, container "
                        "name, host port, Redis DB, whether it is running, and its URL. "
                        "The unified view for the Loop Lab sandbox selector. Output: "
                        "{sandboxes:[{role,branch,name,port,redis_db,running,url}], count}.")
async def evolve_sandbox_list(trace_id=None):
    ps = await _sh(["docker", "ps", "--format", "{{.Names}}"])
    running = {n.strip() for n in (ps.get("out", "") or "").splitlines() if n.strip()}
    # docker ps lists PAUSED containers too — separate them so 'running' is honest
    # and the UI can show a paused (idle-reaped) state distinctly.
    psp = await _sh(["docker", "ps", "--filter", "status=paused", "--format", "{{.Names}}"])
    paused = {n.strip() for n in (psp.get("out", "") or "").splitlines() if n.strip()}
    pinned = await _sandbox_pinned()
    out: List[Dict[str, Any]] = []
    r = _redis()
    if r:
        try:
            raw = await r.get(KEY_SANDBOX)
            if raw:
                pri = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
                _pp = pri.get("port")
                _psc = _DEV_SCHEME_CACHE.get(int(_pp)) if _pp else None
                out.append({"role": "primary", "branch": pri.get("branch"),
                            "name": _SANDBOX_CONTAINER, "port": _pp,
                            "redis_db": pri.get("redis_db"),
                            "running": _SANDBOX_CONTAINER in running and _SANDBOX_CONTAINER not in paused,
                            "paused": _SANDBOX_CONTAINER in paused,
                            "pinned": _SANDBOX_CONTAINER in pinned,
                            "url": f"{_psc or 'http'}://localhost:{_pp}",
                            "scheme": _psc or "http",
                            "worktree": pri.get("worktree")})
        except Exception:
            pass
    for slug, d in (await _sandbox_pool()).items():
        _nm = d.get("name")
        out.append({"role": "spawned", "branch": d.get("branch"), "slug": slug,
                    "name": _nm, "port": d.get("port"),
                    "redis_db": d.get("redis_db"),
                    "running": _nm in running and _nm not in paused,
                    "paused": _nm in paused,
                    "pinned": _nm in pinned,
                    "url": d.get("url") or f"http://localhost:{d.get('port')}",
                    "worktree": d.get("worktree")})
    return {"sandboxes": out, "count": len(out)}


@capability("evolve.sandbox.down", memory="on",
            http_method="POST", http_path="/evolve/sandbox/down", http_tags=["evolve"],
            description="Tear down a dev sandbox: stop+remove its container and "
                        "(optionally) its git worktree. With NO name/branch, tears down "
                        "the PRIMARY vera-dev (original behavior). Pass name (container "
                        "or slug) or branch to tear down a specific SPAWNED per-branch "
                        "container instead. Inputs: name (str), branch (str), "
                        "remove_worktree (bool default True).")
async def evolve_sandbox_down(remove_worktree: bool = True, name: str = "",
                              branch: str = "", trace_id=None):
    # ── a specific SPAWNED container (by container name, slug, or branch) ──
    if name or branch:
        target = None
        for slug, d in (await _sandbox_pool()).items():
            if name in (d.get("name"), slug) or (branch and branch == d.get("branch")):
                target = (slug, d)
                break
        if not target:
            return {"error": f"no spawned sandbox matching '{name or branch}'"}
        slug, d = target
        compose = d.get("compose", f"docker-compose.dev-{slug}.yml")
        dn = await _sh(["docker", "compose", "-f", "docker-compose.yml", "-f", compose,
                        "-p", d.get("name"), "down"], timeout=180)
        removed_wt = False
        if remove_worktree and d.get("worktree"):
            wr = await _remove_worktree_robust(d["worktree"])
            removed_wt = wr["ok"]
        r = _redis()
        if r:
            try:
                await r.hdel(KEY_SANDBOX_POOL, slug)
            except Exception:
                pass
        try:
            (_repo_root() / compose).unlink()
        except Exception:
            pass
        await _audit("sandbox.down", f"spawned {d.get('name')} torn down "
                     f"(worktree_removed={removed_wt})", branch=d.get("branch"))
        await emit_event({"type": "evolve.sandbox.down", "name": d.get("name"),
                          "worktree_removed": removed_wt})
        return {"ok": dn["ok"], "name": d.get("name"), "worktree_removed": removed_wt,
                "detail": dn["err"] or dn["out"]}
    # ── the PRIMARY vera-dev (original behavior) ──
    sb = await _get_sandbox()
    dn = await _sh(["docker", "compose", "-f", "docker-compose.yml",
                    "-f", _DEV_COMPOSE, "down"], timeout=180)
    removed_wt = False
    if remove_worktree and sb.get("worktree"):
        wr = await _remove_worktree_robust(sb["worktree"])
        removed_wt = wr["ok"]
    r = _redis()
    if r:
        try:
            await r.delete(KEY_SANDBOX)
        except Exception:
            pass
    # best-effort: the VS Code sidecar (if attached) dies with the sandbox
    if sb.get("code"):
        await _code_sidecar_teardown(sb)
    await _audit("sandbox.down", f"torn down (worktree_removed={removed_wt})")
    await emit_event({"type": "evolve.sandbox.down", "worktree_removed": removed_wt})
    return {"ok": dn["ok"], "worktree_removed": removed_wt,
            "detail": dn["err"] or dn["out"]}


async def _list_worktrees() -> List[Dict[str, Any]]:
    """Parse `git worktree list --porcelain` into [{path, branch, is_main}]."""
    r = await _git("worktree", "list", "--porcelain")
    root = str(_repo_root()).replace("\\", "/").rstrip("/")
    wts: List[Dict[str, Any]] = []
    cur: Dict[str, Any] = {}
    for line in (r.get("out", "") or "").splitlines():
        if line.startswith("worktree "):
            if cur:
                wts.append(cur)
            p = line[len("worktree "):].strip()
            cur = {"path": p, "branch": "",
                   "is_main": p.replace("\\", "/").rstrip("/") == root}
        elif line.startswith("branch "):
            b = line[len("branch "):].strip()
            cur["branch"] = b[len("refs/heads/"):] if b.startswith("refs/heads/") else b
    if cur:
        wts.append(cur)
    return wts


@capability("evolve.sandbox.prune", memory="on",
            http_method="POST", http_path="/evolve/sandbox/prune", http_tags=["evolve"],
            description="Reap STALE sandbox leftovers: loop-lab worktrees whose "
                        "branch is fully merged (0 unique commits) with no live "
                        "container, orphaned per-branch compose files, and dead pool "
                        "entries. DRY-RUN by default — reports keep/reap/review "
                        "without changing anything. review = worktrees with UNMERGED "
                        "commits (never auto-removed; needs a manual decision). A live "
                        "sandbox's worktree and the main checkout are NEVER touched. "
                        "Inputs: dry_run (bool=True), delete_branches (bool=False — "
                        "off keeps every reap fully restorable via git worktree add), "
                        "delete_merged_branches (bool=False — also delete STANDALONE "
                        "fully-merged typed branches that have no worktree/live sandbox; "
                        "uses `git branch -d` which re-verifies merged, never loses WIP), "
                        "base (str=''=default branch), protect (list[str] branch names).")
async def evolve_sandbox_prune(dry_run: bool = True, delete_branches: bool = False,
                               base: str = "", protect: List[str] = None,
                               delete_merged_branches: bool = False, trace_id=None):
    base = (base or "").strip() or await _default_branch()
    protect = list(protect or [])
    wts = await _list_worktrees()
    # Which worktree paths are OWNED by a sandbox → protected. **T1 FIX (2026-08-12):** a
    # container that EXISTS in ANY state (running/paused/stopped/exited) still owns its
    # worktree; only a fully-REMOVED container frees it. The old check used `docker ps`
    # (running/paused only), so a paused or transitioning sandbox looked dead during the
    # restart window and its worktree got reaped — breaking the primary and leaving stale
    # pool descriptors. Now: existence is keyed off `docker ps -a`, AND every registered
    # sandbox's worktree is protected regardless of container state (a pool worktree is
    # owned by the sandbox lifecycle / evolve.sandbox.down, never by the merged-reap here).
    ps = await _sh(["docker", "ps", "-a", "--format", "{{.Names}}"])
    exists = {n.strip() for n in (ps.get("out", "") or "").splitlines() if n.strip()}
    protected_paths: List[str] = []
    pool = await _sandbox_pool()
    r = _redis()
    prim = {}
    if r:
        try:
            raw = await r.get(KEY_SANDBOX)
            if raw:
                prim = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
        except Exception:
            prim = {}
    if prim.get("worktree"):
        protected_paths.append(prim["worktree"])          # ALWAYS protect the primary's worktree
    live_composes: List[str] = []
    stale_pool: List[str] = []
    heal_pool: List[str] = []                             # T2: pool entries whose worktree is GONE
    for slug, d in pool.items():
        wt = d.get("worktree")
        if wt:
            protected_paths.append(wt)                    # ALWAYS protect a registered sandbox's worktree
            if not Path(wt).exists():
                heal_pool.append(slug)                    # half-alive: descriptor kept, worktree gone
        if d.get("name") in exists:
            live_composes.append(d.get("compose", f"docker-compose.dev-{slug}.yml"))
        else:
            stale_pool.append(slug)                       # container TRULY removed (not in docker ps -a)
    # which candidate branches are fully merged into base (0 unique commits) —
    # and which worktrees hold UNCOMMITTED changes (never reap those, even if
    # merged: rev-list only sees committed history, so WIP left in a worktree
    # would otherwise be silently clobbered).
    merged: List[str] = []
    dirty_paths: List[str] = []
    for w in wts:
        path = w.get("path", "")
        br = w.get("branch")
        if w.get("is_main") or _WORKTREE_DIR not in path:
            continue
        st = await _sh(_git_wt_argv(path, "-C", path, "status", "--porcelain"))
        if (st.get("out", "") or "").strip():
            dirty_paths.append(path)
        if br:
            cnt = await _git("rev-list", "--count", f"{base}..{br}")
            if cnt.get("ok") and (cnt.get("out", "").strip() == "0"):
                merged.append(br)
    plan = _plan_reap(worktrees=wts, protected_paths=protected_paths,
                      merged_branches=merged, protected_branches=protect,
                      dirty_paths=dirty_paths, base_branch=base)
    # STANDALONE fully-merged typed branches — dead refs with NO worktree and no
    # live sandbox, whose commits are all in `base` (so deleting loses nothing;
    # they stay recoverable from base's history). Never an unmerged branch.
    _wt_branches = {w.get("branch") for w in wts if w.get("branch")}
    _live_branches = {prim.get("branch")} | {d.get("branch") for d in pool.values()}
    _typed = ("feat/", "fix/", "refactor/", "perf/", "docs/", "test/",
              "chore/", "spike/", "hotfix/", "loop-lab/")
    merged_branches: List[str] = []
    _mb = await _git("branch", "--merged", base, "--format=%(refname:short)", timeout=60)
    for _b in (_mb.get("out", "") or "").splitlines():
        _b = _b.strip()
        if (not _b or _b == base or _b in protect or _b in _wt_branches
                or _b in _live_branches or _is_trunk_protected(_b)
                or not _b.startswith(_typed)):
            continue
        merged_branches.append(_b)
    # orphaned auto-generated compose files
    try:
        composes = [p.name for p in _repo_root().glob("docker-compose.dev-*.yml")]
    except Exception:
        composes = []
    orphan_yml = _orphan_composes(compose_files=composes, live_composes=live_composes)

    result = {"dry_run": dry_run, "base": base, "keep": plan["keep"],
              "reap": plan["reap"], "review": plan["review"],
              "merged_branches": merged_branches,
              "orphan_composes": orphan_yml, "stale_pool_entries": stale_pool,
              "heal_pool_entries": heal_pool,
              "removed": [], "errors": [], "removed_branches": [], "reconciled_pool": []}
    if dry_run:
        result["note"] = ("dry-run — nothing changed. Re-run with dry_run=false to "
                          "remove the `reap` worktrees + orphan composes + dead pool "
                          "entries, and RECONCILE `heal_pool_entries` (broken sandboxes "
                          "whose worktree is gone). `review` items are left for a manual "
                          "decision; every registered sandbox's worktree is protected.")
        return result

    # ── execute: only the proven-safe reap set ──
    for e in plan["reap"]:
        rm = await _remove_worktree_robust(e["path"])
        (result["removed"] if rm["ok"] else result["errors"]).append(
            {**e, "method": rm.get("method"), "detail": rm.get("detail", "")})
        if (rm["ok"] and delete_branches and e.get("branch")
                and not _is_trunk_protected(e["branch"])):
            await _git("branch", "-D", e["branch"], timeout=60)
    await _git("worktree", "prune", timeout=60)
    for slug in stale_pool:
        if r:
            try:
                await r.hdel(KEY_SANDBOX_POOL, slug)
            except Exception:
                pass
    # T2 self-heal: a pool entry whose worktree is GONE is a broken (half-alive) sandbox —
    # remove its useless, port-holding container and drop the descriptor so the branch can
    # be re-spawned clean. (The primary is not in the pool, so this never touches vera-dev.)
    for slug in heal_pool:
        d = pool.get(slug) or {}
        nm = d.get("name")
        if nm:
            await _sh(["docker", "rm", "-f", nm], timeout=60)
        if r:
            try:
                await r.hdel(KEY_SANDBOX_POOL, slug)
            except Exception:
                pass
        result["reconciled_pool"].append(slug)
    for fn in orphan_yml:
        try:
            (_repo_root() / fn).unlink()
        except Exception:
            pass
    # delete standalone fully-merged branches — `git branch -d` (NOT -D) re-verifies
    # merged-into-HEAD as a safety belt, so an unmerged branch can never be lost here.
    if delete_merged_branches:
        for _b in merged_branches:
            dr = await _git("branch", "-d", _b, timeout=30)
            if dr.get("ok"):
                result["removed_branches"].append(_b)
    await _audit("sandbox.prune",
                 f"reaped {len(result['removed'])} worktree(s), "
                 f"{len(result['removed_branches'])} merged branch(es), "
                 f"{len(orphan_yml)} compose(s), {len(stale_pool)} dead pool entr(ies), "
                 f"{len(result['reconciled_pool'])} reconciled (worktree-gone)")
    await emit_event({"type": "evolve.sandbox.prune",
                      "removed": len(result["removed"]),
                      "removed_branches": len(result["removed_branches"]),
                      "review": len(plan["review"])})
    return result


_SCAFFOLD_SWEEP_INTERVAL_S = int(os.getenv("VERA_SCAFFOLD_SWEEP_INTERVAL_S", "3600"))
_SCAFFOLD_SWEEP_ENABLED = os.getenv("VERA_SCAFFOLD_SWEEP_ENABLED", "1") != "0"
_SWEEP_STARTUP_GRACE_S = int(os.getenv("VERA_SWEEP_STARTUP_GRACE_S", "180"))
_SWEEP_PROC_START = time.time()


async def _scaffolding_sweep() -> None:
    """Scheduled 'leave no scaffolding behind' guardrail (§2.2b / §8.1 #4): reap
    merged+clean worktrees with no live container, and delete standalone
    fully-merged branches. SAFE by construction — every removal is gated on
    'fully merged into base' (0 unique commits) AND 'not dirty', so work-in-
    progress is never touched (an unmerged worktree lands in `review`, left
    alone; `git branch -d` re-verifies each branch). Disable with
    VERA_SCAFFOLD_SWEEP_ENABLED=0."""
    if not _SCAFFOLD_SWEEP_ENABLED:
        return
    # T1 belt-and-suspenders: never sweep during the startup settling window — docker/pool
    # state can be mid-transition right after a restart (that was one way T1 fired).
    if (time.time() - _SWEEP_PROC_START) < _SWEEP_STARTUP_GRACE_S:
        return
    try:
        res = await evolve_sandbox_prune(dry_run=False, delete_branches=True,
                                         delete_merged_branches=True)
        n_wt, n_br = len(res.get("removed", [])), len(res.get("removed_branches", []))
        if n_wt or n_br:
            log.info("evolve: scaffolding sweep reaped %d worktree(s) + %d merged branch(es)",
                     n_wt, n_br)
    except Exception as e:
        log.debug("scaffolding sweep: %s", e)


schedule(_scaffolding_sweep, _SCAFFOLD_SWEEP_INTERVAL_S, name="evolve.scaffolding.sweep")


# ═════════════════════════════════════════════════════════════════════════════
# CONTENT WRITEBACK (Phase E) — a sanctioned, PATH-LOCKED way for Vera (running on
# prod) to land docs / skills / notes / tracked images onto `main` WITHOUT a dev
# container and WITHOUT dirtying prod's live checkout. Writes go into a machine-
# managed worktree on `docs/content-sync` (OUT of the repo tree, under the state
# root), commit through the repo hooks (secret-scan / no-AI-trailer / human author),
# and safe-merge into main so prod serves them immediately. The allowlist means it
# can NEVER touch runtime code (vera/, arbitrary paths).
# ═════════════════════════════════════════════════════════════════════════════
_CONTENT_ALLOW_PREFIXES = ("documentation/", ".claude/skills/")
_CONTENT_DENY_PREFIXES = ("documentation/assets/",)   # gitignored machine output
_CONTENT_BRANCH = "docs/content-sync"


def _content_path_ok(path: str):
    """Validate a content-writeback path against the allowlist. Returns (True, rel)
    or (False, reason). Structurally excludes runtime code — only documentation/
    (not assets/) and .claude/skills/ are writable."""
    p = (path or "").strip().replace("\\", "/").lstrip("/")
    if not p:
        return False, "path required"
    if ".." in p.split("/"):
        return False, "path may not contain '..'"
    if not p.startswith(_CONTENT_ALLOW_PREFIXES):
        return False, ("path must be under " + " or ".join(_CONTENT_ALLOW_PREFIXES)
                       + " — content-writeback is locked to docs + skills, never runtime code")
    if p.startswith(_CONTENT_DENY_PREFIXES):
        return False, "documentation/assets/ is gitignored machine output — not a content target"
    return True, p


async def _content_sync_worktree():
    """Ensure the machine-managed content worktree on docs/content-sync exists and
    is reset to the current main tip. Lives OUTSIDE the repo tree (state root) so it
    never dirties prod's checkout, and it is EXCLUSIVELY machine-managed (only
    content.edit writes here, every edit commits+merges+resets), so a hard reset is
    always safe — there is never human WIP to lose here."""
    from Vera.vera import state_paths
    root = str(_repo_root())
    wt = str(state_paths.state_root() / "content-sync-wt")
    if not Path(wt, ".git").exists():
        await _sh(["git", "-C", root, "worktree", "prune"], timeout=60)
        has_branch = (await _git("rev-parse", "--verify", f"refs/heads/{_CONTENT_BRANCH}",
                                 repo_root=root)).get("ok")
        argv = (["git", "-C", root, "worktree", "add", "--force", wt, _CONTENT_BRANCH]
                if has_branch else
                ["git", "-C", root, "worktree", "add", "-b", _CONTENT_BRANCH, wt, "main"])
        add = await _sh(argv, timeout=120)
        if not Path(wt, ".git").exists():
            return {"error": f"content worktree add failed: {(add.get('err') or '')[:200]}"}
    await _sh(_git_wt_argv(wt, "reset", "--hard", "main"), cwd=wt, timeout=60)
    await _sh(_git_wt_argv(wt, "clean", "-fd"), cwd=wt, timeout=60)
    return {"path": wt, "branch": _CONTENT_BRANCH}


@capability("content.edit", memory="on",
            http_method="POST", http_path="/content/edit", http_tags=["content"],
            description="Land a DOC / SKILL / note / tracked image onto main WITHOUT a dev "
                        "container and WITHOUT dirtying prod's checkout (Phase E). Writes "
                        "`body` to `path` inside a machine-managed docs/content-sync worktree "
                        "(out-of-tree), commits through the repo hooks (secret-scan, "
                        "no-AI-trailer, human author), and safe-merges into main so prod serves "
                        "it immediately. LOCKED to an allowlist: path must be under "
                        "documentation/ (not assets/) or .claude/skills/ — never runtime code. "
                        "Inputs: path (str!), body (str!), message (str — commit subject). "
                        "Output: {ok, path, sha, merged, commit, push_pending}.")
async def content_edit(path: str = "", body: str = "", message: str = "", trace_id=None):
    okp, res = _content_path_ok(path)
    if not okp:
        return {"error": res}
    rel = res
    if body is None:
        return {"error": "body required"}
    wtr = await _content_sync_worktree()
    if wtr.get("error"):
        return wtr
    wt = wtr["path"]
    target = Path(wt) / rel
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    except Exception as e:
        return {"error": f"write failed: {e}"}
    await _sh(_git_wt_argv(wt, "add", "--", rel), cwd=wt, timeout=60)
    # identical content → nothing staged → no-op success
    if (await _sh(_git_wt_argv(wt, "diff", "--cached", "--quiet"), cwd=wt, timeout=60)).get("ok"):
        return {"ok": True, "path": rel, "sha": "", "merged": False, "note": "no change (identical content)"}
    subj = ((message or "").strip().splitlines() or [""])[0][:120] or f"docs: update {rel}"
    commit = await _sh(_git_wt_argv(wt, "-c", "user.name=BoeJaker",
                                    "-c", "user.email=boejaker80@gmail.com",
                                    "commit", "-m", subj), cwd=wt, timeout=180)
    if not commit.get("ok"):
        # a hook (secret-scan / AI-trailer / author) blocked it — surface why
        return {"error": "commit blocked (hook or empty)",
                "detail": (commit.get("err") or commit.get("out") or "")[-600:]}
    sha = (await _sh(_git_wt_argv(wt, "rev-parse", "HEAD"), cwd=wt)).get("out", "").strip()
    mres = await _merge_in_checkout(str(_repo_root()), _CONTENT_BRANCH, "main",
                                    str(_repo_root()), f"content: {subj}")
    if not mres.get("ok"):
        return {"ok": False, "path": rel, "sha": sha, "merged": False,
                "error": f"merge to main failed: {mres.get('error')}"}
    await _audit("content.edit", f"{rel} -> main ({sha[:8]})", path=rel)
    await emit_event({"type": "content.edit", "path": rel, "sha": sha})
    return {"ok": True, "path": rel, "sha": sha, "merged": True, "commit": mres.get("commit"),
            "push_pending": True,
            "note": "landed on main (prod serves it); GitHub push piggybacks the normal flow / prod's deploy key"}


@capability("content.status", memory="off", silent=True,
            http_method="GET", http_path="/content/status", http_tags=["content"],
            description="Content-writeback status: the allowlist, the docs/content-sync "
                        "worktree, and how many local main commits are AHEAD of origin/main "
                        "(pending a GitHub push). Output: {ok, worktree, branch, "
                        "unpushed_main_commits, allow, deny}.")
async def content_status(trace_id=None):
    from Vera.vera import state_paths
    root = str(_repo_root())
    ahead = await _git("rev-list", "--count", "origin/main..main", repo_root=root)
    return {"ok": True, "worktree": str(state_paths.state_root() / "content-sync-wt"),
            "branch": _CONTENT_BRANCH,
            "unpushed_main_commits": ((ahead.get("out", "") or "0").strip()
                                      if ahead.get("ok") else "unknown (no origin/main ref)"),
            "allow": list(_CONTENT_ALLOW_PREFIXES), "deny": list(_CONTENT_DENY_PREFIXES)}


# ─────────────────────────────────────────────────────────────────────────────
# SANDBOX DIFF + VS CODE SIDECAR — see the branch's edits as they happen
# ─────────────────────────────────────────────────────────────────────────────
# The worktree already holds the branch's code (committed AND the pipeline's
# uncommitted edits). evolve.sandbox.diff surfaces `git diff <base>` from
# inside it for the panel's Changes pane; evolve.sandbox.code.attach runs a
# code-server container with the worktree bind-mounted and registers it behind
# the IDE's same-origin /vscode/ proxy, so the Loop Lab tab can embed a real
# VS Code on the branch.

DEV_CODE_PORT = int(os.getenv("VERA_DEV_CODE_PORT", "8996"))
DEV_CODE_IMAGE = os.getenv("VERA_DEV_CODE_IMAGE", "codercom/code-server:latest")
DEV_CODE_CONTAINER = "vera-dev-code"
DEV_CODE_IID = "loop-lab-dev"        # instance id behind /vscode/{iid}/


def _vscode_mod():
    """The IDE's vscode module (instance registry + proxy), when loaded."""
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.endswith("vscode_capabilities") \
                and hasattr(mod, "_upsert_instance"):
            return mod
    return None


async def _set_sandbox(sb: Dict[str, Any]) -> None:
    r = _redis()
    if r:
        try:
            await r.set(KEY_SANDBOX, json.dumps(sb, default=str))
        except Exception as e:
            log.debug("sandbox descriptor save: %s", e)


async def _code_sidecar_teardown(sb: Dict[str, Any]) -> None:
    await _sh(["docker", "rm", "-f", DEV_CODE_CONTAINER], timeout=60)
    vs = _vscode_mod()
    if vs:
        try:
            vs._delete_instance(DEV_CODE_IID)
        except Exception:
            pass
    sb.pop("code", None)
    await _set_sandbox(sb)


async def _worktree_diff(wt: str, base: str = "main", file: str = "",
                         context: int = 3, max_bytes: int = 200000,
                         head: str = "") -> Dict[str, Any]:
    """Unified diff of a worktree (committed + uncommitted edits) against a base
    ref — the shared logic behind evolve.sandbox.diff and evolve.pipeline.diff.
    When `head` is given, diff `base...head` (a COMMITTED branch, e.g. an adopted
    pipeline that has no worktree) from `wt` = the repo root instead — no working
    tree needed. Callers resolve `wt`/`base`/`head` for their own case."""
    if not wt or not Path(wt).exists():
        return {"error": "worktree not found"}
    base = (base or "main").strip()
    head = (head or "").strip()
    spec = f"{base}...{head}" if head else base   # branch diff vs working-tree diff
    context = max(0, int(context or 3))
    max_bytes = int(max_bytes or 200000)

    # changed files: status letters + adds/dels, rename-aware
    ns = await _sh(_git_wt_argv(wt, "diff", spec, "--name-status", "-M"), cwd=wt)
    if not ns["ok"]:
        return {"error": f"git diff failed: {ns['err'] or ns['out']}"}
    num = await _sh(_git_wt_argv(wt, "diff", spec, "--numstat", "-M"), cwd=wt)
    stats: Dict[str, Any] = {}
    for line in (num["out"] or "").splitlines():
        p = line.split("\t")
        if len(p) >= 3:
            stats[p[-1]] = {"adds": p[0], "dels": p[1]}
    files = []
    for line in (ns["out"] or "").splitlines():
        p = line.split("\t")
        if len(p) >= 2:
            path = p[-1]
            files.append({"path": path, "status": p[0][:1],
                          **stats.get(path, {"adds": "?", "dels": "?"})})

    # Untracked files only exist for a working-tree diff (head=""); a committed
    # branch diff (head set) has none.
    untracked: List[str] = []
    if not head:
        st = await _sh(_git_wt_argv(wt, "status", "--porcelain"), cwd=wt)
        untracked = [l[3:] for l in (st["out"] or "").splitlines() if l.startswith("??")]

    # the diff text itself (an untracked file has no git diff — synthesize one)
    if file and file in untracked:
        try:
            body = (Path(wt) / file).read_text(encoding="utf-8", errors="replace")
            diff = (f"--- /dev/null\n+++ b/{file}\n"
                    + "\n".join("+" + l for l in body.splitlines()))
        except Exception as e:
            diff = f"(could not read untracked file: {e})"
    else:
        args = _git_wt_argv(wt, "diff", spec, "-M", f"-U{context}")
        if file:
            args += ["--", file]
        d = await _sh(args, cwd=wt)
        diff = d["out"] if d["ok"] else f"(git diff failed: {d['err']})"
    truncated = len(diff) > max_bytes
    return {"ok": True, "base": base, "worktree": wt, "files": files,
            "untracked": untracked, "diff": diff[:max_bytes], "truncated": truncated}


@capability("evolve.sandbox.diff", memory="off", silent=True,
            http_method="GET", http_path="/evolve/sandbox/diff", http_tags=["evolve"],
            description="Unified diff of a dev-sandbox worktree (committed + "
                        "uncommitted branch edits) against a base ref, so you can "
                        "see exactly what Loop Lab changed. Targets the primary "
                        "vera-dev by default; pass name (container/slug) or branch "
                        "to diff a SPAWNED per-branch sandbox. Inputs: base (str, "
                        "default 'main'), file (str — scope to one path), context "
                        "(int=3), max_bytes (int=200000), name (str), branch (str). "
                        "Output: {ok, branch, base, files: [{path, status, adds, "
                        "dels}], untracked, diff, truncated}.")
async def evolve_sandbox_diff(base: str = "main", file: str = "",
                              context: int = 3, max_bytes: int = 200000,
                              name: str = "", branch: str = "", trace_id=None):
    # Target the primary vera-dev worktree by default, or a SPAWNED per-branch
    # sandbox by name/branch (same routing as evolve.sandbox.exec) so the Sandbox
    # tab's Diff works for ANY container, not only the active one.
    if (name or "").strip() or (branch or "").strip():
        tgt = await _resolve_exec_target(name, branch)
        if tgt.get("error"):
            return tgt
        wt = tgt.get("worktree", "")
        sb_branch = branch or ""
    else:
        sb = await _get_sandbox()
        wt = sb.get("worktree", "")
        sb_branch = sb.get("branch", "")
    if not wt or not Path(wt).exists():
        return {"error": "no dev sandbox worktree — bring one up first "
                         "(evolve.sandbox.up / ensure)"}
    res = await _worktree_diff(wt, base, file, context, max_bytes)
    if res.get("error"):
        return res
    res["branch"] = sb_branch
    return res


@capability("evolve.pipeline.diff", memory="off", silent=True,
            http_method="GET", http_path="/evolve/pipeline/diff", http_tags=["evolve"],
            description="Unified diff of a code pipeline's branch worktree against "
                        "its repo's default branch — works for any registered repo, "
                        "not just Vera's dev sandbox. Inputs: id (str!), file (str "
                        "— scope to one path), context (int=3), max_bytes "
                        "(int=200000). Output: {ok, repo, branch, base, files, "
                        "untracked, diff, truncated}.")
async def evolve_pipeline_diff(id: str = "", file: str = "", context: int = 3,
                               max_bytes: int = 200000, trace_id=None):
    got = await evolve_pipeline_get(id=id)
    rec = got.get("pipeline")
    if not rec:
        return {"error": got.get("error") or "pipeline not found"}
    if rec.get("kind") != "code":
        return {"error": "evolve.pipeline.diff is only for kind=code pipelines"}
    repo = rec.get("repo") or DEFAULT_REPO_ID
    root = await _resolve_repo_root(repo)
    base = rec.get("to") or await _default_branch(repo_root=root)
    branch = rec.get("branch", "")
    wt = rec.get("worktree")
    if wt and Path(wt).exists():
        # live worktree: committed + uncommitted edits vs base
        res = await _worktree_diff(wt, base, file, context, max_bytes)
    elif branch:
        # adopted / no-worktree pipeline: diff the branch's COMMITTED changes vs
        # base straight from git (root repo, base...branch) — no worktree needed.
        res = await _worktree_diff(str(root), base, file, context, max_bytes, head=branch)
    else:
        return {"error": "pipeline has neither a worktree nor a branch to diff"}
    if res.get("error"):
        return res
    res["repo"] = repo
    res["branch"] = branch
    return res


@capability("evolve.sandbox.review", memory="on",
            http_method="POST", http_path="/evolve/sandbox/review", http_tags=["evolve"],
            description="Send Loop Lab's dev-sandbox changes to the shared Workspace "
                        "Changes review panel (the same PR-style accept/reject UI used "
                        "for loop/IDE edits): diffs the worktree's changed files against "
                        "the base repo and creates a proposal — accepting a file writes "
                        "the worktree version into the base checkout. Inputs: base (str, "
                        "default 'main'). Output: {ok, proposal:{id, files, status}}.")
async def evolve_sandbox_review(base: str = "main", trace_id=None):
    sb = await _get_sandbox()
    wt = sb.get("worktree", "")
    if not wt or not Path(wt).exists():
        return {"ok": False, "error": "no dev sandbox worktree — bring one up first"}
    base = (base or "main").strip()
    # Changed files (added/modified/renamed-new + untracked); deletions are not
    # handled by the review UI, so they're skipped here.
    ns = await _sh(_git_wt_argv(wt, "diff", base, "--name-status", "-M"), cwd=wt)
    if not ns["ok"]:
        return {"ok": False, "error": f"git diff failed: {ns['err'] or ns['out']}"}
    paths: List[str] = []
    for line in (ns["out"] or "").splitlines():
        p = line.split("\t")
        if len(p) >= 2 and p[0][:1] != "D":
            paths.append(p[-1])                 # rename → new path
    st = await _sh(_git_wt_argv(wt, "status", "--porcelain"), cwd=wt)
    for l in (st["out"] or "").splitlines():
        if l.startswith("??"):
            paths.append(l[3:])
    paths = sorted({p for p in paths if p})
    if not paths:
        return {"ok": True, "proposal": None, "note": "no changes to review"}
    # Target = the MAIN worktree (first entry of `git worktree list`), where an
    # accepted file is written.
    wl = await _sh(_git_wt_argv(wt, "worktree", "list", "--porcelain"), cwd=wt)
    target = ""
    for line in (wl["out"] or "").splitlines():
        if line.startswith("worktree "):
            target = line[len("worktree "):].strip()
            break
    if not target or not Path(target).exists():
        return {"ok": False, "error": "could not resolve base repo path from worktree"}
    propose = CAPABILITY_REGISTRY.get("ide.workspace.changes.propose_dir")
    if not propose or not propose.get("func"):
        return {"ok": False, "error": "review proposal cap (ide.workspace.changes.propose_dir) unavailable"}
    label = f"Loop Lab · {sb.get('branch', '') or base}"
    res = await propose["func"](source_dir=wt, target_dir=target, paths=",".join(paths),
                                source=f"loop-lab:{sb.get('branch', '') or base}",
                                workspace=label)
    await emit_event({"type": "evolve.sandbox.review", "branch": sb.get("branch", ""),
                      "files": len(paths), "proposal": (res or {}).get("proposal")})
    return res


from Vera.vera.evolve.evolve_git_core import branches_checked_out as _branches_checked_out  # noqa: E402


@capability("evolve.sandbox.approve", memory="on",
            http_method="POST", http_path="/evolve/sandbox/approve", http_tags=["evolve"],
            description="Approve a sandbox change by MERGING its branch into an "
                        "integration branch via git, in a throwaway isolated worktree — "
                        "NEVER writing into a live working tree. Prod's checkout is left "
                        "untouched (no dirty tree, no forced restart); deploying the "
                        "integration branch to prod stays a separate, deliberate step. "
                        "Refuses if `into` is checked out by a live worktree, or on merge "
                        "conflict (resolve on the branch, then re-approve). Inputs: branch "
                        "(str! — the loop-lab/… branch to merge), into (str — target "
                        "integration branch; default the repo's mainline), proposal_id "
                        "(str — Workspace-Changes proposal to mark merged), repo (str). "
                        "Output: {ok, merged, into, commit, conflicts}.")
async def evolve_sandbox_approve(branch: str = "", into: str = "", proposal_id: str = "",
                                 repo: str = DEFAULT_REPO_ID, trace_id=None):
    root = await _resolve_repo_root(repo or DEFAULT_REPO_ID)
    branch = (branch or "").strip()
    if not branch:
        return {"ok": False, "error": "branch required"}
    if not (await _git("rev-parse", "--verify", f"refs/heads/{branch}", repo_root=root))["ok"]:
        return {"ok": False, "error": f"unknown branch: {branch}"}
    into = (into or "").strip() or await _default_branch(root)
    if into == branch:
        return {"ok": False, "error": "into must differ from branch"}
    if not (await _git("rev-parse", "--verify", f"refs/heads/{into}", repo_root=root))["ok"]:
        return {"ok": False, "error": f"unknown target branch: {into}"}
    # SAFETY GATE: never advance a branch a live worktree (e.g. prod's checkout)
    # has checked out — moving that ref changes files under a running process.
    wl = await _git("worktree", "list", "--porcelain", repo_root=root)
    if into in _branches_checked_out(wl.get("out", "")):
        return {"ok": False, "error": f"'{into}' is checked out by a live worktree; "
                "approve into an integration branch that isn't currently checked out "
                "(deploying it to prod is a separate, deliberate step)"}
    # Merge in a throwaway worktree (into is NOT checked out — gated just above), so
    # no live working tree is touched. Shared with evolve.pipeline.promote.
    res = await _merge_isolated(str(root), branch, into,
                                f"Loop Lab: approve+merge {branch} → {into}")
    if not res["ok"]:
        return {"ok": False, "error": "merge conflict — resolve on the branch, then re-approve",
                "conflicts": res.get("conflicts", [])}
    merged_sha = res["commit"]
    # Mark the Workspace-Changes proposal MERGED (no file write-back).
    if proposal_id:
        mark = CAPABILITY_REGISTRY.get("ide.workspace.changes.mark_merged")
        if mark and mark.get("func"):
            try:
                await mark["func"](id=proposal_id, into=into, commit=merged_sha)
            except Exception as _e:
                log.debug("mark_merged %s: %s", proposal_id, _e)
    await _audit("sandbox.approve", f"MERGED {branch} → {into} @ {merged_sha[:10]}",
                 branch=branch, into=into, repo=repo, ok=True)
    await emit_event({"type": "evolve.sandbox.approved", "branch": branch,
                      "into": into, "commit": merged_sha})
    return {"ok": True, "merged": branch, "into": into, "commit": merged_sha, "conflicts": []}


# ── Unified sandbox log / error / perf collector ────────────────────────────
#   A background task tails each loop-lab sandbox container's docker logs (as
#   they run) into capped Redis streams, samples docker stats, and routes each
#   DISTINCT error into the existing evolve.errors queue (Error Radar/postmortem)
#   — every record stamped with the sandbox branch + running-code short sha, so a
#   log line or error ties straight back to the commit that produced it.
from Vera.vera.evolve.evolve_logs_core import (          # noqa: E402
    parse_log_line as _log_parse, classify_level as _log_level,
    error_signature as _log_sig, parse_stats_line as _stats_parse)

_LOG_PREFIXES    = ("vera-dev",)                        # loop-lab sandbox containers
_LOG_STREAM      = "vera:evolve:sandbox:logs"           # + :<container>
_LOG_ERRS        = "vera:evolve:sandbox:errors"         # + :<container>
_LOG_PERF        = "vera:evolve:sandbox:perf"           # + :<container>
_LOG_CURSOR      = "vera:evolve:sandbox:logcursor"      # hash container -> last ts
_LOG_SEEN_ERR    = "vera:evolve:sandbox:errsig"         # set of routed signatures
_LOG_MAXLEN      = 2000
_LOG_PERF_MAXLEN = 500
_LOG_INTERVAL    = int(os.getenv("VERA_SANDBOX_LOG_INTERVAL", "10"))
_LOG_TASK = None


def _b2s(v) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else (v or "")


async def _log_containers() -> List[str]:
    r = await _sh(["docker", "ps", "--format", "{{.Names}}"], timeout=15)
    names = [n.strip() for n in (r.get("out", "") or "").splitlines() if n.strip()]
    return [n for n in names if any(n.startswith(p) for p in _LOG_PREFIXES)]


async def _collect_container(c: str, branch: str, ver: str) -> Dict[str, int]:
    R = _redis()
    if not R:
        return {"lines": 0, "errors": 0}
    try:
        last = _b2s(await R.hget(_LOG_CURSOR, c))
    except Exception:
        last = ""
    args = ["docker", "logs", "--timestamps"]
    args += (["--since", last] if last else ["--since", "5m", "--tail", "300"])
    args += [c]
    res = await _sh(args, timeout=20)
    raw = (res.get("out", "") or "") + "\n" + (res.get("err", "") or "")   # docker splits streams
    nlines = nerr = 0
    newlast = last
    for line in raw.splitlines():
        if not line.strip():
            continue
        ts, text = _log_parse(line)
        if ts and last and ts <= last:
            continue                                    # already collected
        level = _log_level(text)
        entry = {"ts": ts, "text": text[:2000], "level": level, "c": c, "br": branch, "ver": ver}
        try:
            await R.xadd(f"{_LOG_STREAM}:{c}", {"data": json.dumps(entry)},
                         maxlen=_LOG_MAXLEN, approximate=True)
        except Exception:
            pass
        nlines += 1
        if ts and ts > newlast:
            newlast = ts
        if level == "error":
            nerr += 1
            try:
                await R.xadd(f"{_LOG_ERRS}:{c}", {"data": json.dumps(entry)},
                             maxlen=_LOG_MAXLEN, approximate=True)
            except Exception:
                pass
            try:                                        # route only DISTINCT errors → no LLM spam
                if await R.sadd(_LOG_SEEN_ERR, f"{c}|{_log_sig(text)}"):
                    await evolve_errors_ingest(
                        source="sandbox", title=f"[{c}] {text[:120]}", detail=text[:1500],
                        meta={"component": c, "branch": branch, "ver": ver}, suggest=False)
            except Exception:
                pass
    if newlast and newlast != last:
        try:
            await R.hset(_LOG_CURSOR, c, newlast)
        except Exception:
            pass
    try:                                                # perf sample
        st = await _sh(["docker", "stats", "--no-stream", "--format",
                        "{{.Name}}\t{{.CPUPerc}}\t{{.MemPerc}}\t{{.MemUsage}}", c], timeout=15)
        for l in (st.get("out", "") or "").splitlines():
            p = _stats_parse(l)
            if p:
                p.update({"ts": now_iso(), "br": branch})
                await R.xadd(f"{_LOG_PERF}:{c}", {"data": json.dumps(p)},
                             maxlen=_LOG_PERF_MAXLEN, approximate=True)
    except Exception:
        pass
    return {"lines": nlines, "errors": nerr}


async def _collect_once() -> Dict[str, int]:
    try:
        ver = ""
        try:
            from Vera.vera.provenance import get_provenance
            ver = get_provenance().get("git_sha_short", "")
        except Exception:
            pass
        try:
            branch = (await _get_sandbox() or {}).get("branch", "")
        except Exception:
            branch = ""
        total = {"containers": 0, "lines": 0, "errors": 0}
        for c in await _log_containers():
            r = await _collect_container(c, branch, ver)
            total["containers"] += 1
            total["lines"] += r["lines"]
            total["errors"] += r["errors"]
        return total
    except Exception as e:
        log.debug("sandbox log collect: %s", e)
        return {"containers": 0, "lines": 0, "errors": 0}


async def _collector_loop():
    while True:
        try:
            t = await _collect_once()
            await asyncio.sleep(_LOG_INTERVAL if t.get("containers") else _LOG_INTERVAL * 3)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(_LOG_INTERVAL)


def _ensure_collector():
    """Start the background collector once; idempotent and safe to call from any
    cap (lazy start) or from evolve.sandbox.up (start when a sandbox comes up)."""
    global _LOG_TASK
    if _LOG_TASK is None or _LOG_TASK.done():
        try:
            _LOG_TASK = asyncio.create_task(_collector_loop())
        except RuntimeError:                            # no running loop yet
            _LOG_TASK = None


async def _read_stream(key: str, limit: int) -> List[Dict]:
    R = _redis()
    if not R:
        return []
    out = []
    try:
        for _id, v in await R.xrevrange(key, count=int(limit)):
            data = v.get(b"data") or v.get("data")
            if data:
                out.append(json.loads(_b2s(data)))
    except Exception:
        pass
    return out


@capability("evolve.sandbox.logs", memory="off", silent=True,
            http_method="GET", http_path="/evolve/sandbox/logs", http_tags=["evolve"],
            description="Recent captured logs for loop-lab sandbox container(s), newest "
                        "first, each stamped with branch + running-code sha. Inputs: "
                        "container (str — blank = all vera-dev* sandboxes), limit (int, "
                        "default 200), level (error|warn|info — filter), errors_only "
                        "(bool). Output: {logs:[{ts,text,level,c,br,ver}], containers}.")
async def evolve_sandbox_logs(container: str = "", limit: int = 200, level: str = "",
                              errors_only: bool = False, trace_id=None):
    _ensure_collector()
    containers = [container] if container else await _log_containers()
    base = _LOG_ERRS if errors_only else _LOG_STREAM
    out: List[Dict] = []
    for c in containers:
        for e in await _read_stream(f"{base}:{c}", limit):
            if level and e.get("level") != level:
                continue
            out.append(e)
    out.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return {"logs": out[:int(limit)], "containers": containers}


@capability("evolve.sandbox.metrics", memory="off", silent=True,
            http_method="GET", http_path="/evolve/sandbox/metrics", http_tags=["evolve"],
            description="Recent CPU%/mem% samples for loop-lab sandbox container(s), "
                        "oldest→newest (for sparklines). Inputs: container (str — blank "
                        "= all), limit (int, default 60). Output: {metrics:{<c>:[{ts,"
                        "cpu_pct,mem_pct,mem,br}]}, containers}.")
async def evolve_sandbox_metrics(container: str = "", limit: int = 60, trace_id=None):
    _ensure_collector()
    containers = [container] if container else await _log_containers()
    series = {}
    for c in containers:
        series[c] = list(reversed(await _read_stream(f"{_LOG_PERF}:{c}", limit)))
    return {"metrics": series, "containers": containers}


@capability("evolve.sandbox.log_status", memory="off", silent=True,
            http_method="GET", http_path="/evolve/sandbox/log_status", http_tags=["evolve"],
            description="Collector health + per-container capture counts (log lines, "
                        "errors, perf samples, cursor). Starts the collector if idle. "
                        "Output: {collector_running, interval_s, containers:{<c>:{...}}}.")
async def evolve_sandbox_log_status(trace_id=None):
    _ensure_collector()
    R = _redis()
    containers = await _log_containers()
    st = {}
    for c in containers:
        row = {}
        if R:
            try:
                row = {"lines": await R.xlen(f"{_LOG_STREAM}:{c}"),
                       "errors": await R.xlen(f"{_LOG_ERRS}:{c}"),
                       "perf": await R.xlen(f"{_LOG_PERF}:{c}"),
                       "cursor": _b2s(await R.hget(_LOG_CURSOR, c))}
            except Exception:
                row = {}
        st[c] = row
    return {"collector_running": bool(_LOG_TASK and not _LOG_TASK.done()),
            "interval_s": _LOG_INTERVAL, "containers": st}


@capability("evolve.sandbox.code.attach", memory="on",
            http_method="POST", http_path="/evolve/sandbox/code/attach", http_tags=["evolve"],
            description="Start a VS Code (code-server) sidecar with the dev-sandbox "
                        "worktree bind-mounted, registered behind the IDE's "
                        "same-origin /vscode/ proxy so the Loop Lab panel can embed "
                        "it. Inputs: port (int — default VERA_DEV_CODE_PORT/8996), "
                        "password (str — generated), image (str). "
                        "Output: {ok, url, proxy, password, container, port}.")
async def evolve_sandbox_code_attach(port: int = 0, password: str = "",
                                     image: str = "", trace_id=None):
    sb = await _get_sandbox()
    wt = sb.get("worktree", "")
    if not wt or not Path(wt).exists():
        return {"error": "no dev sandbox worktree — bring one up first "
                         "(evolve.sandbox.up / ensure)"}
    port = int(port or DEV_CODE_PORT)
    pw = password or uuid.uuid4().hex

    await _sh(["docker", "rm", "-f", DEV_CODE_CONTAINER], timeout=60)
    # In-container git for the VS Code terminal: the worktree's .git file points at
    # the main repo's .git/worktrees/<name> AND records its own host-absolute path,
    # so mount BOTH the main .git and the worktree at its host-absolute path (in
    # addition to /home/coder/workspace) so every worktree pointer resolves and
    # `git commit` works from the terminal. This is a FOREIGN image (code-server),
    # so safe.directory can't be baked in — pass it via git's env-based config so
    # the root user isn't blocked by the dubious-ownership guard on host-owned files.
    _repo_abs = str(_repo_root())
    _wt_abs = str(Path(wt).resolve())
    # -u 0: worktree files are the repo owner's; root avoids uid mismatch inside
    run = await _sh(["docker", "run", "-d", "--name", DEV_CODE_CONTAINER,
                     "-p", f"{port}:8080", "-e", f"PASSWORD={pw}", "-u", "0",
                     "-e", "GIT_CONFIG_COUNT=1",
                     "-e", "GIT_CONFIG_KEY_0=safe.directory",
                     "-e", "GIT_CONFIG_VALUE_0=*",
                     "--entrypoint", "/usr/bin/code-server",
                     "-v", f"{wt}:/home/coder/workspace",
                     "-v", f"{_repo_abs}/.git:{_repo_abs}/.git:rw",
                     "-v", f"{_wt_abs}:{_wt_abs}:rw",
                     "--label", "vera.loop-lab.code=1", image or DEV_CODE_IMAGE,
                     "--bind-addr", "0.0.0.0:8080", "--auth", "password",
                     "/home/coder/workspace"], timeout=300)
    if not run["ok"]:
        return {"error": f"docker run failed: {run['err'] or run['out']}",
                "hint": "requires docker + the code-server image on this host"}

    host = (os.getenv("VSCODE_PUBLIC_HOST", "").strip()
            or os.getenv("VERA_HOST", "").strip() or "127.0.0.1")
    url = f"http://{host}:{port}"
    proxy = ""
    vs = _vscode_mod()
    if vs:
        vs._upsert_instance({
            "id": DEV_CODE_IID, "label": f"Loop Lab · {sb.get('branch', 'dev')}",
            "kind": "sandbox-worker", "url": url, "port": port,
            "container": DEV_CODE_CONTAINER, "docker_host_id": "local",
            "workdir": "/home/coder/workspace", "status": "running",
            "token_sealed": vs._seal(pw)})
        proxy = f"/vscode/{DEV_CODE_IID}/"

    sb["code"] = {"container": DEV_CODE_CONTAINER, "port": port,
                  "iid": DEV_CODE_IID, "proxy": proxy, "attached_at": now_iso()}
    await _set_sandbox(sb)
    await _audit("sandbox.code.attach", f"VS Code on {sb.get('branch','?')} :{port}")
    await emit_event({"type": "evolve.sandbox.code", "action": "attached",
                      "port": port, "proxy": proxy})
    return {"ok": True, "url": url, "proxy": proxy, "password": pw,
            "container": DEV_CODE_CONTAINER, "port": port}


@capability("evolve.sandbox.code.detach", memory="on",
            http_method="POST", http_path="/evolve/sandbox/code/detach", http_tags=["evolve"],
            description="Stop and remove the Loop Lab VS Code sidecar (the sandbox "
                        "and its worktree are untouched). Output: {ok}.")
async def evolve_sandbox_code_detach(trace_id=None):
    sb = await _get_sandbox()
    await _code_sidecar_teardown(sb)
    await _audit("sandbox.code.detach", "VS Code sidecar removed")
    await emit_event({"type": "evolve.sandbox.code", "action": "detached"})
    return {"ok": True, "removed": DEV_CODE_CONTAINER}


async def _sandbox_suite(branch: str, profile: str, critic: str) -> Dict[str, Any]:
    """Run the benchmark suite THROUGH the sandbox's own HTTP API (branch code).
    Returns the sandbox's suite summary, or {error} if unreachable."""
    if not (await _sandbox_probe()).get("reachable"):
        return {"error": "sandbox not reachable"}
    try:
        async with httpx.AsyncClient(timeout=1800) as c:
            r = await c.post(_dev_base_url() + "/evolve/suite/run",
                             json={"profile": profile, "assess": True,
                                   "provider": critic})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": f"sandbox suite failed: {e}"}


# ─────────────────────────────────────────────────────────────────────────────
# PANEL + STARTUP
# ─────────────────────────────────────────────────────────────────────────────

@APP.get("/evolve/panel", include_in_schema=False)
async def _evolve_panel_html():
    from fastapi.responses import HTMLResponse as _HTMLResp
    # no-cache: the panel is redeployed constantly (every UI landing) and loads in
    # an iframe — without this the browser serves a STALE cached panel, which can
    # mismatch the always-fresh /ui/elements/*.js and render inconsistently. Match
    # the no-cache the element routes already set.
    _hdrs = {"Cache-Control": "no-cache, no-store, must-revalidate"}
    if _PANEL_PATH.exists():
        return _HTMLResp(_PANEL_PATH.read_text(encoding="utf-8"), headers=_hdrs)
    return _HTMLResp("<p style='color:#c96b6b'>evolve_panel.html not found</p>",
                     status_code=404, headers=_hdrs)


# ── Loop Lab infographic elements — served the same way activity_capabilities.py
#    serves activity_timeline_element.js: a dedicated /ui/elements/<name>.js
#    route per file, reading straight off disk (no build step, no caching —
#    edit the .js, reload the panel, done). All live at the repo's vera/ root
#    alongside the other injectable custom elements.
def _serve_element_js_from(source_filename: str, missing_note: str):
    path = _HERE.parent / source_filename
    async def _handler():
        from fastapi.responses import Response as _Resp
        if path.exists():
            return _Resp(content=path.read_text(encoding="utf-8"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})
        return _Resp(content=f"console.warn({missing_note!r});",
                    media_type="application/javascript")
    return _handler


APP.get("/ui/elements/error_radar.js", include_in_schema=False)(
    _serve_element_js_from("error_radar_element.js", "vera-error-radar element JS not found"))
APP.get("/ui/elements/branch_pipeline.js", include_in_schema=False)(
    _serve_element_js_from("branch_pipeline_element.js", "vera-branch-pipeline element JS not found"))
APP.get("/ui/elements/task_matrix.js", include_in_schema=False)(
    _serve_element_js_from("task_matrix_element.js", "vera-task-matrix element JS not found"))
APP.get("/ui/elements/bench_compare.js", include_in_schema=False)(
    _serve_element_js_from("bench_compare_element.js", "vera-bench-compare element JS not found"))
APP.get("/ui/elements/ollama_map.js", include_in_schema=False)(
    _serve_element_js_from("ollama_routing_map_element.js", "vera-ollama-map element JS not found"))
APP.get("/ui/elements/test_activity_timeline.js", include_in_schema=False)(
    _serve_element_js_from("test_activity_timeline_element.js", "vera-test-activity-timeline element JS not found"))
APP.get("/ui/elements/author_map.js", include_in_schema=False)(
    _serve_element_js_from("author_map_element.js", "vera-author-map element JS not found"))
APP.get("/ui/elements/git_graph.js", include_in_schema=False)(
    _serve_element_js_from("git_graph_element.js", "vera-git-graph element JS not found"))


register_ui(
    "evolve", "Loop Lab", "🧪",
    """<div id="evolve-mount" style="height:100%;display:flex;flex-direction:column;">
        <iframe src="/evolve/panel"
                style="flex:1;border:none;width:100%;height:100%"></iframe>
    </div>""",
    "",
    ui_caps=["evolve.tasks", "evolve.task.run", "evolve.suite.run",
             "evolve.suite.start", "evolve.suite.status", "evolve.runs",
             "evolve.assess", "evolve.assess.compare", "evolve.selftest",
             "evolve.tasks.generate", "evolve.goal.run",
             "evolve.improve.start", "evolve.improve.status", "evolve.improve.list",
             "evolve.improve.cancel", "evolve.variants", "evolve.variant.promote",
             "evolve.report", "evolve.config.get", "evolve.git.status",
             "evolve.pipeline.run", "evolve.pipeline.list", "evolve.pipeline.get",
             "evolve.pipeline.promote", "evolve.pipeline.rollback",
             "evolve.pipeline.from_review", "evolve.observe.scan",
             "evolve.sandbox.status", "evolve.sandbox.up", "evolve.sandbox.down",
             "evolve.sandbox.ensure", "evolve.audit.list", "evolve.cap.test",
             "evolve.unittest.run", "evolve.editq.list", "evolve.editq.get",
             "evolve.editq.update", "evolve.editq.cancel", "evolve.instances",
             "evolve.targets", "evolve.run.start", "evolve.run.status",
             "evolve.errors.list", "evolve.errors.ingest", "evolve.errors.suggest",
             "evolve.errors.approve", "evolve.errors.dismiss", "evolve.errors.sync",
             "evolve.errors.clear", "evolve.ide.improve", "evolve.config.set",
             "evolve.sandbox.exec", "evolve.sandbox.fs.list",
             "evolve.sandbox.fs.read", "evolve.sandbox.fs.write",
             "evolve.target.info"],
    mode="tab", tab_order=72,
)


_STARTED = False


async def _startup():
    # Runs on a long-interval scheduler slot (fires once shortly after boot; the
    # guard makes re-fires no-ops). Wait for Redis, then merge-seed the default
    # tasks (never clobber user edits).
    global _STARTED
    if _STARTED:
        return
    _STARTED = True
    for _ in range(20):
        if _redis() is not None:
            break
        await asyncio.sleep(0.5)
    r = _redis()
    if not r:
        log.warning("evolve startup: redis unavailable — tasks not seeded")
        _STARTED = False   # allow a later retry
        return
    try:
        seeded_raw = await r.smembers(KEY_SEEDED)
        seeded = {(s.decode() if isinstance(s, bytes) else str(s))
                  for s in (seeded_raw or set())}
        existing_raw = await r.hkeys(KEY_TASKS)
        existing = {(k.decode() if isinstance(k, bytes) else str(k))
                    for k in (existing_raw or [])}
        added = 0
        for t in _default_tasks():
            if t["id"] not in seeded and t["id"] not in existing:
                await _save_task(t)
                added += 1
        names = [t["id"] for t in _default_tasks()]
        if names:
            await r.sadd(KEY_SEEDED, *names)
        if added:
            log.info("evolve: seeded %d benchmark tasks", added)
    except Exception as e:
        log.debug("evolve startup: %s", e)

    # Reap stale "running" sessions — a process restart orphans the asyncio
    # task but leaves the Redis record asserting "running" forever, which is
    # exactly the "says a session is running but isn't" symptom.
    try:
        rows = await r.lrange(KEY_SESSIONS, 0, SESSIONS_CAP - 1)
        for i, row in enumerate(rows or []):
            try:
                rec = json.loads(row.decode() if isinstance(row, bytes) else row)
            except Exception:
                continue
            if rec.get("status") == "running" and rec.get("id") not in _IMPROVE_TASKS:
                rec["status"] = "interrupted"
                rec["current"] = ""
                rec["ended_at"] = now_iso()
                await r.lset(KEY_SESSIONS, i, json.dumps(rec, default=str))
                sraw = await r.get(KEY_SESSION + rec["id"])
                if sraw:
                    full = json.loads(sraw.decode() if isinstance(sraw, bytes) else sraw)
                    full["status"] = "interrupted"
                    full["current"] = ""
                    await r.set(KEY_SESSION + rec["id"], json.dumps(full, default=str))
    except Exception as e:
        log.debug("evolve reap sessions: %s", e)

    # Re-arm the background edit-queue worker if anything is pending.
    try:
        if await r.llen(KEY_EDITQ):
            _editq_start()
    except Exception:
        pass


def _extend_loop_blacklist() -> None:
    """Keep the loop-driving evolve caps OUT of every agent-loop toolkit so a
    loop under evaluation can't recurse into the harness that's running it
    (mirrors loops.run's own blacklist entry)."""
    dw = (sys.modules.get("dag_workshop_capabilities")
          or sys.modules.get("Vera.vera.dag.dag_workshop_capabilities"))
    try:
        bl = getattr(dw, "_DEFAULT_CAP_BLACKLIST", None)
        if isinstance(bl, set):
            bl.update({"evolve.suite.run", "evolve.task.run", "evolve.improve.start",
                       "evolve.assess.compare", "evolve.code.queue",
                       "evolve.variant.promote"})
    except Exception as e:
        log.debug("evolve: could not extend loop blacklist: %s", e)


_extend_loop_blacklist()

_LAST_AUTOSYNC = 0.0


async def _errors_autosync_tick():
    """Config-gated: pull observability errors into the work-queue on a cadence
    so errors flow toward a commit without anyone watching. Nothing is applied —
    each item still waits for a human approve. Throttled by errors_autosync_s so
    the scheduler slot can fire often but the pull only runs when due."""
    global _LAST_AUTOSYNC
    try:
        cfg = await _get_config()
    except Exception:
        return
    if not cfg.get("errors_autosync"):
        return
    now = time.time()
    if now - _LAST_AUTOSYNC < float(cfg.get("errors_autosync_s", 900)):
        return
    _LAST_AUTOSYNC = now
    try:
        res = await evolve_errors_sync(suggest=True)
        if res.get("ingested"):
            log.info("evolve: errors autosync ingested %d new item(s)",
                     res["ingested"])
    except Exception as e:
        log.debug("evolve errors autosync: %s", e)


async def _sandbox_follow_up():
    """Bring the dev sandbox up with this Vera, when configured to follow.

    One-shot (long interval + guard). Best-effort and non-fatal: a sandbox that
    fails to start must never stop the host Vera from finishing boot."""
    global _FOLLOW_STARTED
    if _FOLLOW_STARTED:
        return
    _FOLLOW_STARTED = True
    try:
        cfg = await _get_config()
    except Exception:
        return
    if not cfg.get("sandbox_follow_host"):
        return
    try:
        # No branch → current mainline, fast-forwarded (see evolve_sandbox_up).
        res = await evolve_sandbox_up()
        if res.get("error"):
            log.warning("evolve: follow-host sandbox up failed: %s", res["error"])
        else:
            log.info("evolve: dev sandbox brought up with Vera (follow_host) on port %s",
                     res.get("port"))
    except Exception as e:
        log.warning("evolve: follow-host sandbox up failed: %s", e)


async def _sandbox_follow_down():
    """Take the dev sandbox down when this Vera shuts down (follow mode only).

    Registered as a shutdown hook so the second Vera cannot outlive the first —
    the failure this exists to prevent. The worktree is LEFT in place: it may
    hold uncommitted work, and it is cheap to reuse on the next boot."""
    try:
        cfg = await _get_config()
    except Exception:
        return
    if not cfg.get("sandbox_follow_host"):
        return
    try:
        res = await evolve_sandbox_down(remove_worktree=False)
        log.info("evolve: dev sandbox taken down with Vera (follow_host): %s",
                 "ok" if not res.get("error") else res.get("error"))
    except Exception as e:
        log.warning("evolve: follow-host sandbox down failed: %s", e)


_FOLLOW_STARTED = False

schedule(_startup, interval=999999, name="evolve_startup")
schedule(_errors_autosync_tick, interval=120, name="evolve_errors_autosync")
schedule(_sandbox_follow_up, interval=999999, name="evolve_sandbox_follow")

# Shutdown hook. Registered via the orchestrator's SHUTDOWN_HOOKS, NOT
# @APP.on_event("shutdown") — FastAPI ignores on_event handlers when the app is
# built with a `lifespan` (it is), so that route would silently never run and the
# sandbox would keep outliving Vera exactly as before.
try:
    from Vera.vera.capability_orchestration import register_shutdown_hook as _reg_sd
    _reg_sd(_sandbox_follow_down)
except Exception as _she:                          # pragma: no cover
    log.debug("evolve: could not register sandbox shutdown hook: %s", _she)

log.info("evolve: Loop Lab module loaded (%d default tasks, %d tunable knobs)",
         len(_default_tasks()), len(TUNABLE_KNOBS))
