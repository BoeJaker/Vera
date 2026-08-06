"""
loop_profiles.py — Specialised agentic-loop PROFILES
====================================================

A *profile* is a named, ready-to-run preset for the existing agentic-loop
engine (``/workshop/agent_loop/stream`` + the ``dag.agent_loop_v5/v6/v7``
capabilities). Where a raw loop run makes the user pick an engine version, an
agent, a toolkit and a dozen knobs by hand, a profile bundles all of that into
one specialist tuned for a *panel / capability family*:

    fabric-discovery · coding · code-editing · code-testing ·
    code-verification · devops · git-operations · file-operations ·
    planning · networking · ide · business-shop · long-term-scheduling

Each profile pins:
  • an engine version (v5 / v6 / v7)
  • a specialist Vera **agent** (agents.py DEFAULT_AGENTS) — its system prompt,
    model + domain_caps are merged in by the stream endpoint's agent resolution
  • a curated **base_toolkit / allowed_caps** floor scoped to that family
    (optionally including the panel-driving caps so the loop can read and act on
    the panel currently mounted in the chat UI)
  • sensible engine defaults (phases, tiering, master planner, …)
  • the **panel family** it serves — used by the DAG workshop inventory UI

Nothing here re-implements the loop. A profile is *just a body preset*: at run
time it is merged UNDER the caller's explicit body (the caller always wins), so
a profile is a starting point, never a straitjacket.

Design mirrors delivery.py / output_formats.py: a pure declarative registry plus
merge helpers, no orchestrator import at module top beyond the capability
decorator. Three capabilities expose it:

    loops.profiles   list every profile (for the workshop inventory + pickers)
    loops.profile    resolve one profile to its full stream-body preset
    loops.run        run a goal THROUGH a profile as a single awaited call
                     (blacklisted from the loop toolkit so a loop can't recurse)
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional

from Vera.vera.capability_orchestration import (
    CAPABILITY_REGISTRY,
    capability,
)

log = logging.getLogger("vera.loop_profiles")

# Panel-driving caps — the chat UI panel bridge. A profile with panels=True can
# READ the panel currently mounted in the user's chat session (panel.query) and
# ACT on it (panel.dispatch), plus enumerate the panel/cap surface. This is what
# lets a specialist loop "make use of the panels system in the chat UI".
_PANEL_ACCESS_CAPS = ["panel.query", "panel.dispatch", "ui.panel.list", "ui.panels"]


# ─────────────────────────────────────────────────────────────────────────────
# THE REGISTRY
# ─────────────────────────────────────────────────────────────────────────────
# caps  — a toolkit FLOOR (base_toolkit + allowed_caps). Caps that aren't
#         registered are harmless: the engine filters base_toolkit against the
#         live registry and the specialist can still widen via triage/recon.
# panels— True → append _PANEL_ACCESS_CAPS so the loop can drive the mounted panel.
# defaults — extra stream-body knobs (any key the stream endpoint understands).
LOOP_PROFILES: List[Dict[str, Any]] = [
    {
        "id": "fabric-discovery",
        "label": "Fabric Discovery",
        "icon": "📚",
        "engine": "v6",
        "agent": "fabric-librarian",
        "family": ["fabric", "memory-graph", "memory-map"],
        "description": "Explores the data fabric and memory graph: finds the "
                       "right datasets/entities, crawls & ingests new sources, "
                       "and answers 'what do we know about X'.",
        "caps": ["fabric.query", "fabric.datasets", "fabric.stats",
                 "fabric.entity_graph.query", "fabric.entity_graph.snapshot",
                 "fabric.discover.crawl", "fabric.ingest",
                 "fabric.identify", "fabric.gaps", "fabric.upsert",
                 "fabric.schema.declare", "fabric.schema.get", "fabric.validate",
                 "fabric.fuse", "memory.select", "context.for_agent",
                 "memory.seek", "memory.map", "memory.browse", "memory.read",
                 "caps.search", "web.search"],
        "skills": "sys-fabric-query",
        "panels": True,
        "defaults": {"enable_phases": True, "prefer_terminal_tools": False},
    },
    {
        "id": "coding",
        "label": "Coding",
        "icon": "⌨️",
        "engine": "v6",
        "agent": "coder",
        "family": ["ide"],
        "description": "Writes and runs real code (python/bash) to accomplish a "
                       "task, iterating until it works. Auto-saves & versions "
                       "generated code to the code store.",
        "caps": ["exec.python.run", "exec.bash.run",
                 "ide.fs.read", "ide.code.grep",
                 "ide.code.outline", "ide.code.tool_manifest",
                 "code.author", "code.save", "code.read", "code.versions",
                 "evolve.pipeline.review.request"],
        "skills": "sys-exec-fileio",
        "panels": True,
        "defaults": {"enable_code_autosave": True, "prefer_terminal_tools": True},
    },
    {
        "id": "code-editing",
        "label": "Code Editing",
        "icon": "✏️",
        "engine": "v6",
        "agent": "code-editor",
        "family": ["ide"],
        "description": "Surgical, line-level edits to existing files: read the "
                       "region, change the minimum, re-read to confirm. Prefers "
                       "targeted patches over rewrites.",
        "caps": ["ide.code.read_lines", "ide.code.edit_lines",
                 "ide.code.insert_at", "ide.code.replace", "ide.code.grep",
                 "ide.code.list_files", "ide.fs.read",
                 "code.edit", "code.save", "code.diff", "code.read",
                 "evolve.pipeline.review.request"],
        "skills": "sys-exec-fileio",
        "panels": True,
        "defaults": {"prefer_terminal_tools": True, "enable_code_autosave": True},
    },
    {
        "id": "code-testing",
        "label": "Code Testing",
        "icon": "🧪",
        "engine": "v6",
        "agent": "code-tester",
        "family": ["ide", "execution"],
        "description": "Runs tests / exercises code and reports what passed, what "
                       "failed and the failing output — never claims green "
                       "without running it.",
        "caps": ["exec.python.run", "exec.bash.run", "ide.fs.read",
                 "ide.code.grep", "ide.code.list_files", "code.read"],
        "skills": "sys-exec-fileio",
        "panels": True,
        "defaults": {"require_verify": True, "prefer_terminal_tools": True},
    },
    {
        "id": "code-verification",
        "label": "Code Verification",
        "icon": "🔍",
        "engine": "v6",
        "agent": "code-verifier",
        "family": ["ide"],
        "description": "Reviews a change for correctness & regressions: reads the "
                       "diff, reasons about failure modes, and confirms the code "
                       "actually does what it should.",
        "caps": ["llm.code_review", "llm.explain", "ide.code.grep",
                 "ide.fs.read", "exec.bash.run",
                 "code.read", "code.diff", "code.versions"],
        "skills": "",
        "panels": True,
        "defaults": {"require_verify": True, "enable_final_gate": True},
    },
    {
        "id": "verification",
        "label": "Script Verification",
        "icon": "🧪",
        "engine": "v5",
        "agent": "script-verifier",
        "family": ["ide", "exec"],
        "description": "Proves whether something works by building SMALL, efficient "
                       "test scripts — code behaviour, API responses, log conditions, "
                       "applied edits, system states — printing a PASS/FAIL verdict "
                       "plus a few lines of decision-ready evidence, never terminal "
                       "spew. The active counterpart to code-verification's "
                       "read-and-reason review.",
        "caps": ["exec.python.run", "exec.bash.run", "exec.code.run",
                 "code.read", "code.diff", "ide.fs.read", "ide.code.grep",
                 "ide.code.list_files", "http.get", "health.check"],
        "skills": "",
        "panels": False,
        "defaults": {"max_steps": 4, "prefer_terminal_tools": True},
    },
    {
        "id": "git-operations",
        "label": "Git Operations",
        "icon": "🌿",
        "engine": "v5",
        "agent": "git-operator",
        "family": ["ide"],
        "description": "Version-control operations: status, diff, log, staged "
                       "commits with clear messages. Inspects before it commits.",
        "caps": ["ide.git.status", "ide.git.diff", "ide.git.log",
                 "ide.git.commit", "exec.bash.run", "ide.fs.read"],
        "skills": "",
        "panels": True,
        "defaults": {"prefer_terminal_tools": True},
    },
    {
        "id": "file-operations",
        "label": "File Operations",
        "icon": "🗂️",
        "engine": "v5",
        "agent": "file-operator",
        "family": ["ide"],
        "description": "Filesystem work: browse, read, write, move and organise "
                       "files across the workspace roots. Confirms a path before "
                       "it overwrites or deletes.",
        "caps": ["ide.fs.read", "ide.fs.list", "ide.fs.browse",
                 "ide.fs.delete", "ide.fs.roots", "ide.fs.exists",
                 "exec.bash.run"],
        "skills": "sys-exec-fileio",
        "panels": True,
        "defaults": {"prefer_terminal_tools": True},
    },
    {
        "id": "devops",
        "label": "DevOps / Infra",
        "icon": "🖧",
        "engine": "v6",
        "agent": "infra-operator",
        "family": ["execution", "proxmox", "monitor", "docker"],
        "description": "Operates the cluster: Proxmox guests, Docker workers, "
                       "system health, deployments. Checks state before it acts "
                       "and pauses long-running/destructive ops for approval.",
        "caps": ["proxmox.status", "proxmox.cluster.list", "proxmox.guest.action",
                 "docker.hosts", "docker.containers", "docker.worker.run",
                 "sysmon.status", "sysmon.history", "obs.health",
                 "exec.bash.run", "provision.deploy"],
        "skills": "",
        "panels": True,
        "defaults": {"long_running_force_hitl": True, "enable_phases": True},
    },
    {
        "id": "operator",
        "label": "Operator (container/VM)",
        "icon": "⎈",
        "engine": "v6",
        "agent": "infra-operator",
        "family": ["execution", "proxmox", "docker", "monitor"],
        "description": "Administers ONE target (a saved remote connection — Docker "
                       "container, LXC/VM guest or SSH host) end-to-end: inspects "
                       "state, runs commands, edits files, manages services & "
                       "packages, mounts apps. Point it at a goal like 'update "
                       "packages on debian-vm and restart nginx'. Checks state "
                       "before acting and pauses destructive ops for approval.",
        "caps": ["conn.list", "conn.targets", "conn.open", "conn.exec",
                 "operator.sysinfo", "operator.processes", "operator.ports",
                 "operator.services", "operator.service", "operator.pkg",
                 "fs.list", "fs.read", "fs.write", "fs.mkdir", "fs.delete", "fs.stat",
                 "app.detect", "app.mount", "app.list", "mcp.detect",
                 "docker.ps", "docker.exec", "docker.logs",
                 "proxmox.status", "proxmox.guest.action"],
        "skills": "",
        "panels": True,
        "defaults": {"long_running_force_hitl": True, "enable_phases": True,
                     "prefer_terminal_tools": True},
    },
    {
        "id": "networking",
        "label": "Networking",
        "icon": "🌐",
        "engine": "v6",
        "agent": "network-engineer",
        "family": ["netmap", "netmon", "mesh", "networking"],
        "description": "Diagnoses reachability, scans, topology and the ESP32 "
                       "mesh. Maps the network and reasons about RF/positioning.",
        "caps": ["system.ping", "http.get", "web.search",
                 "netmap.scan", "netmap.graph", "mesh.nodes", "mesh.telemetry",
                 "exec.bash.run"],
        "skills": "",
        "panels": True,
        "defaults": {"enable_phases": True, "prefer_terminal_tools": True},
    },
    {
        "id": "ide",
        "label": "IDE (revised)",
        "icon": "💻",
        "engine": "v6",
        "agent": "capability-smith",
        "family": ["ide", "execution"],
        "description": "Full software-engineering loop over the IDE: read, edit, "
                       "run, test and version code end to end. A revised, "
                       "engine-backed take on the IDE's own agent loop — drives "
                       "the IDE panel directly via the panel bridge.",
        "caps": ["ide.fs.read", "ide.fs.list", "ide.code.grep",
                 "ide.code.read_lines", "ide.code.edit_lines", "ide.code.replace",
                 "ide.code.outline", "ide.code.tool_manifest",
                 "ide.git.status", "ide.git.diff", "ide.git.commit",
                 "exec.python.run", "exec.bash.run", "code.author",
                 "code.save", "code.read", "code.diff", "code.versions"],
        "skills": "sys-exec-fileio,sys-panel-dispatch",
        "panels": True,
        "defaults": {"enable_code_autosave": True, "prefer_terminal_tools": True,
                     "enable_phases": True},
    },
    {
        "id": "planning",
        "label": "Planning / Strategy",
        "icon": "🧭",
        "engine": "v7",
        "agent": "agentic-planner",
        "family": ["dag"],
        "description": "Long-horizon planning: tiers the goal, produces a "
                       "strategic master plan and breaks it into checkable steps. "
                       "Planning is never delegated to a capability.",
        "caps": ["caps.search", "fabric.query", "web.search", "agent.consult",
                 "caps.specialist"],
        "skills": "",
        "panels": True,
        "defaults": {"enable_tiering": True, "enable_master_planner": True,
                     "plan_tier": "auto", "enable_dream_persistence": True},
    },
    {
        "id": "business-shop",
        "label": "Business & Shop",
        "icon": "💼",
        "engine": "v6",
        "agent": "business-operator",
        "family": ["business", "commerce", "markets"],
        "description": "Runs business operations: money, inventory, gigs, content, "
                       "marketing and the shop/commerce surface. Drives the "
                       "Business panel and can print to the thermal printer.",
        "caps": ["business.brief", "business.dashboard", "business.account.list",
                 "business.product.list", "business.gig.list", "business.content.list",
                 "business.task.list", "business.campaign.list", "business.store.master",
                 "business.listing.publish", "business.order.list", "print.receipt",
                 "agent.consult", "caps.specialist"],
        "skills": "",
        "panels": True,
        "defaults": {"enable_phases": True},
    },
    {
        "id": "markets-quant",
        "label": "Markets Quant",
        "icon": "📈",
        "engine": "v6",
        "agent": "quant-strategist",
        "family": ["markets"],
        "description": "Quant research loop: builds strategies from the library "
                       "(long AND short), backtests + deep-analyzes them, runs "
                       "deterministic autotune/sweeps, screens strategies across "
                       "many markets to find the best plays, runs honest ML "
                       "walk-forward tests, composes result infographics and "
                       "paper-trades winners into a sim account. Never touches "
                       "real money — sim accounts only.",
        "caps": ["markets.strategy.library", "markets.strategy.from_template",
                 "markets.strategy.save", "markets.strategy.list",
                 "markets.strategy.accept", "markets.strategy.archive",
                 "markets.backtest.run", "markets.backtest.analyze",
                 "markets.backtest.autotune", "markets.backtest.autotune_status",
                 "markets.backtest.sweep", "markets.backtest.sweep_status",
                 "markets.backtest.batch", "markets.backtest.batch_status",
                 "markets.backtest.list", "markets.backtest.get",
                 "markets.backtest.signals",
                 "markets.analysis.trendfit", "markets.analysis.pivots",
                 "markets.overview",
                 "markets.indicators", "markets.indicator.custom.save",
                 "markets.indicator.custom.test", "markets.indicator.custom.list",
                 "markets.ml.create", "markets.ml.list", "markets.ml.train",
                 "markets.ml.predict", "markets.ml.series", "markets.ml.walkforward",
                 "markets.infographic.save", "markets.infographic.list",
                 "markets.project.asset", "markets.project.portfolio",
                 "markets.portfolio.optimize", "markets.rotation.scan",
                 "markets.dynamics.fetch", "markets.dynamics.snapshot",
                 "markets.wsb.scan", "markets.news.feed",
                 "markets.sentiment.to_series",
                 "markets.bars", "markets.fetch", "markets.jobs",
                 "markets.watchlist.list", "markets.baseline.ensure",
                 "markets.sim.create", "markets.sim.list", "markets.sim.order",
                 "markets.sim.equity", "markets.monitor.status",
                 "markets.strategy.versions", "markets.strategy.revert",
                 "markets.trader.status", "markets.trader.config.set",
                 "markets.trader.tick"],
        "skills": "sys-quant-visuals",
        "panels": True,
        "defaults": {"enable_phases": True, "max_steps": 14},
    },
    {
        "id": "long-term-scheduling",
        "label": "Long-term Scheduling",
        "icon": "📅",
        "engine": "v7",
        "agent": "long-term-planner",
        "family": ["calendar", "dream"],
        "description": "Looks at your calendar and the dream calendar, then plans "
                       "actions and trigger thresholds onto the schedule. "
                       "System-side actions run unattended (dreams stand aside); "
                       "user-side actions notify you over comms and wait for a "
                       "reply.",
        "caps": ["cal.events.list", "cal.event.upsert", "cal.todo.upsert",
                 "cal.note.upsert", "cal.assistant.briefing",
                 "dream.schedule.events", "dream.timeline",
                 "sched.plan.generate", "sched.plan.list", "sched.action.upsert",
                 "sched.triggers.list"],
        "skills": "",
        "panels": True,
        "defaults": {"enable_tiering": True, "enable_dream_persistence": True,
                     "progress_report_mode": "long_term_only",
                     "progress_channel": "telegram"},
    },
    {
        "id": "operator",
        "label": "Web Operator",
        "icon": "🕹️",
        "engine": "v7",
        "agent": "operator",
        "family": ["operator"],
        "description": "Drives a web UI (or a web-served VM) toward a goal by "
                       "observe→think→act: opens a browser session, observes the "
                       "page (screenshot + element refs), and clicks/types/navigates "
                       "until done. The dedicated operator.run loop is the default "
                       "driver; this profile lets the general agent loop use the "
                       "same primitives.",
        "caps": ["operator.session.start", "operator.session.status",
                 "operator.session.close", "operator.observe", "operator.read",
                 "operator.screenshot", "operator.act", "operator.think"],
        "skills": "",
        "panels": True,
        "defaults": {"prefer_terminal_tools": False, "enable_phases": False,
                     "max_steps": 16},
    },
]

_BY_ID: Dict[str, Dict[str, Any]] = {p["id"]: p for p in LOOP_PROFILES}


# ─────────────────────────────────────────────────────────────────────────────
# PURE HELPERS (safe to import from the stream endpoint — no await, no I/O)
# ─────────────────────────────────────────────────────────────────────────────

def list_profiles() -> List[Dict[str, Any]]:
    """Public snapshot of every profile, annotated with resolved caps + whether
    the profile's engine cap is actually registered (for the inventory UI)."""
    out: List[Dict[str, Any]] = []
    for p in LOOP_PROFILES:
        caps = _resolved_caps(p)
        engine_cap = _ENGINE_CAP.get(p.get("engine", "v6"), "dag.agent_loop_v6")
        out.append({
            "id": p["id"], "label": p["label"], "icon": p.get("icon", "↻"),
            "engine": p.get("engine", "v6"), "agent": p.get("agent", ""),
            "family": list(p.get("family") or []),
            "description": p.get("description", ""),
            "caps": caps,
            "cap_count": len(caps),
            "panels": bool(p.get("panels")),
            "skills": p.get("skills", ""),
            "available": engine_cap in CAPABILITY_REGISTRY,
        })
    return out


def get_profile(profile_id: str) -> Optional[Dict[str, Any]]:
    return _BY_ID.get((profile_id or "").strip())


def _dedupe(seq: List[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for s in seq:
        s = (s or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _resolved_caps(p: Dict[str, Any]) -> List[str]:
    caps = list(p.get("caps") or [])
    if p.get("panels"):
        caps = caps + _PANEL_ACCESS_CAPS
    return _dedupe(caps)


_ENGINE_CAP = {
    "v1": "dag.agent_loop", "v2": "dag.agent_loop_v2", "v3": "dag.agent_loop_v3",
    "v4": "dag.agent_loop_v4", "v5": "dag.agent_loop_v5",
    "v6": "dag.agent_loop_v6", "v7": "dag.agent_loop_v7",
}


def profile_stream_body(profile_id: str) -> Dict[str, Any]:
    """Resolve a profile to a ``/workshop/agent_loop/stream`` body preset.

    Returns {} for an unknown id. The caller merges this UNDER its own explicit
    body via :func:`merge_profile_into_body` so the caller always wins.
    """
    p = get_profile(profile_id)
    if not p:
        return {}
    body: Dict[str, Any] = dict(p.get("defaults") or {})
    body["version"] = p.get("engine", "v6")
    if p.get("agent"):
        body["agent_name"] = p["agent"]
    caps = _resolved_caps(p)
    if caps:
        csv = ",".join(caps)
        body["base_toolkit"] = csv
        body["allowed_caps"] = csv
    if p.get("skills"):
        body["attach_skills"] = p["skills"]
    body["loop_profile"] = p["id"]     # marker: which profile drove this run
    return body


def _csv_union(a: str, b: str) -> str:
    parts = [x.strip() for x in (str(a or "") + "," + str(b or "")).split(",")]
    return ",".join(_dedupe(parts))


def merge_profile_into_body(profile_body: Dict[str, Any],
                            body: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a profile preset UNDER an explicit request body.

    Scalars: the explicit body wins (a profile only fills gaps). The cap/skill
    CSVs are UNIONed so a caller can *add* caps without losing the profile's
    curated floor. Returns a new merged dict."""
    merged = {**profile_body, **(body or {})}
    for key in ("base_toolkit", "allowed_caps", "attach_skills"):
        if profile_body.get(key) or (body or {}).get(key):
            merged[key] = _csv_union(profile_body.get(key, ""),
                                     (body or {}).get(key, ""))
    return merged


async def _apply_evolve_overlay(profile_id: str,
                                body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Merge the active Loop Lab overlay for this profile into the body (under
    the caller's later overrides). Returns the new body or None if no overlay."""
    getter = CAPABILITY_REGISTRY.get("evolve.overlay.get")
    if not getter or not getter.get("func"):
        return None
    res = await getter["func"](profile=profile_id)
    overlay = (res or {}).get("overlay") if isinstance(res, dict) else None
    if not overlay:
        return None
    merged = dict(body)
    for k, v in (overlay.get("overrides") or {}).items():
        merged[k] = v
    pre = (overlay.get("prompt_preamble") or "").strip()
    if pre:
        cur = merged.get("system_prompt_template") or ""
        merged["system_prompt_template"] = (pre + "\n\n" + cur) if cur else pre
    merged["_evolve_overlay"] = overlay.get("variant_id", "")
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "loops.profiles", memory="off", silent=True,
    http_method="GET", http_path="/loops/profiles", http_tags=["dag", "agents"],
    description="List the specialised agentic-loop PROFILES (fabric-discovery, "
                "coding, devops, ide, networking, long-term-scheduling, …). Each "
                "bundles an engine version + a specialist agent + a scoped "
                "panel/cap family into a ready-to-run preset. "
                "Output: {profiles:[{id,label,icon,engine,agent,family,caps,"
                "panels,available,...}]}.",
)
async def cap_loops_profiles(trace_id=None):
    return {"profiles": list_profiles()}


@capability(
    "loops.profile", memory="off", silent=True,
    http_method="POST", http_path="/loops/profile", http_tags=["dag", "agents"],
    description="Resolve ONE loop profile to its full stream-body preset. "
                "Input: id (str! — e.g. 'coding'). "
                "Output: {id, profile, stream_body} or {error}.",
)
async def cap_loops_profile(id: str = "", trace_id=None):
    p = get_profile(id)
    if not p:
        return {"error": f"unknown loop profile: {id!r}",
                "known": list(_BY_ID.keys())}
    return {"id": p["id"], "profile": p, "stream_body": profile_stream_body(p["id"])}


@capability(
    "loops.run", memory="on",
    http_method="POST", http_path="/loops/run", http_tags=["dag", "agents"],
    streams=["loops.run"],
    description="Run a goal THROUGH a specialised loop profile as a single "
                "awaited call (no SSE). Resolves the profile's engine + agent + "
                "scoped toolkit, merges any overrides you pass, and invokes the "
                "underlying dag.agent_loop_v5/v6/v7 capability. "
                "Inputs: profile (str! — e.g. 'devops'), goal (str!), "
                "allowed_caps (csv — UNIONed with the profile floor), model, "
                "session_id, plus any engine knob to override a profile default. "
                "Output: the engine cap's result {goal, steps, final, history, …}.",
)
async def cap_loops_run(profile: str = "", goal: str = "",
                        allowed_caps: str = "", model: str = "",
                        instance_id: str = "", session_id: str = "",
                        max_steps: int = 0, trace_id=None, **overrides):
    p = get_profile(profile)
    if not p:
        return {"error": f"unknown loop profile: {profile!r}",
                "known": list(_BY_ID.keys())}
    if not (goal or "").strip():
        return {"error": "goal required"}

    body = profile_stream_body(p["id"])

    # ── Loop Lab overlay ────────────────────────────────────────────────────
    # A promoted tuning variant (evolve.variant.promote) is merged in BETWEEN
    # the profile defaults and the caller's explicit overrides: the improver's
    # learned knobs + prompt preamble benefit every production run of this
    # profile, while a caller can still override any of them. Best-effort — no
    # hard dependency on the evolve module being loaded.
    try:
        overlay = await _apply_evolve_overlay(p["id"], body)
        if overlay:
            body = overlay
    except Exception as e:
        log.debug("loops.run overlay for %s: %s", p["id"], e)

    # Fold caller overrides (goal/model/etc. + any engine knob) in on top.
    caller: Dict[str, Any] = {"goal": goal}
    if allowed_caps:
        caller["allowed_caps"] = allowed_caps
    if model:
        caller["model"] = model
    if instance_id:
        caller["instance_id"] = instance_id
    if session_id:
        caller["session_id"] = session_id
    if max_steps:
        caller["max_steps"] = int(max_steps)
    for k, v in (overrides or {}).items():
        if v is not None:
            caller[k] = v
    body = merge_profile_into_body(body, caller)

    engine_cap = _ENGINE_CAP.get(body.get("version", "v6"), "dag.agent_loop_v6")
    cap = CAPABILITY_REGISTRY.get(engine_cap)
    if not cap or not cap.get("func"):
        return {"error": f"engine capability {engine_cap} not registered"}

    # Merge the profile agent's domain_caps into allowed_caps (the SSE endpoint
    # does this for streamed runs; replicate it for the direct call).
    agent_name = body.get("agent_name", "")
    if agent_name:
        agc = CAPABILITY_REGISTRY.get("agent.get")
        if agc and agc.get("func"):
            try:
                ag = await agc["func"](name=agent_name, trace_id=session_id or trace_id)
                if ag and not ag.get("error"):
                    dom = ag.get("domain_caps") or []
                    if dom:
                        body["allowed_caps"] = _csv_union(body.get("allowed_caps", ""),
                                                          ",".join(dom))
                    if not body.get("model") and ag.get("model"):
                        body["model"] = ag["model"]
            except Exception as e:
                log.debug("loops.run agent merge failed for %s: %s", agent_name, e)

    # Filter to the engine cap's accepted parameters so unrelated stream-only
    # knobs (loop_profile marker, UI fields, etc.) don't raise TypeError.
    accepted = set(cap.get("schema", {}).get("properties", {}).keys()) | {"trace_id"}
    # Every loop engine accepts session_id — v1–v6 as a named parameter, v7/v8
    # via **kwargs — but v7/v8 don't DECLARE it in their schema, so a schema-only
    # `accepted` filter silently drops it and the engine mints its own UUID
    # session. The loop's events then persist under a key nothing maps back to
    # the caller, so the Loop Lab timeline (which follows evolve:<run_id>) stays
    # empty for every v7/v8-profile run — the exact "sandbox events aren't
    # streamed to the UI" symptom (and it bites in-process runs too). Force
    # session_id through so the caller's session id is always the loop's.
    accepted.add("session_id")
    kwargs = {k: v for k, v in body.items() if k in accepted}
    kwargs["goal"] = goal
    kwargs["trace_id"] = session_id or trace_id
    if session_id:
        kwargs["session_id"] = session_id
    try:
        result = await cap["func"](**kwargs)
    except TypeError:
        # Last-resort: retry with only the core args the engines share.
        core = {"goal": goal, "allowed_caps": body.get("allowed_caps", ""),
                "base_toolkit": body.get("base_toolkit", ""),
                "model": body.get("model", ""), "trace_id": session_id or trace_id}
        core = {k: v for k, v in core.items() if k in accepted or k == "goal"}
        result = await cap["func"](**core)
    if isinstance(result, dict):
        result.setdefault("loop_profile", p["id"])
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Keep loops.run OUT of every agent-loop toolkit so a loop can't recurse into a
# specialist loop via this cap (mirrors the dag.agent_loop* blacklist entries).
# ─────────────────────────────────────────────────────────────────────────────
def _extend_loop_blacklist() -> None:
    dw = (sys.modules.get("dag_workshop_capabilities")
          or sys.modules.get("Vera.vera.dag.dag_workshop_capabilities"))
    try:
        bl = getattr(dw, "_DEFAULT_CAP_BLACKLIST", None)
        if isinstance(bl, set):
            bl.add("loops.run")
    except Exception as e:
        log.debug("loop_profiles: could not extend loop blacklist: %s", e)


_extend_loop_blacklist()

log.info("loop_profiles: registered %d specialised loop profiles", len(LOOP_PROFILES))
