"""
ide_capabilities.py  —  Vera IDE Capability Module
====================================================
Registers the IDE as a set of modular capabilities in the Vera framework.
All Ollama and file operations go through the capability system.

Three specialised LLM agents are auto-seeded on startup:
  • ide-thinker   — reasoning / planning / architecture (prefer_gpu, high temp)
  • ide-writer    — code generation / completion (medium temp, code model)
  • ide-analyser  — review / debug / explain (low temp, deterministic)

Sandboxing
──────────
When source-introspection is requested the IDE copies the target files into
an in-memory sandbox dict (IDE_SANDBOX).  The LLM agents can read/modify the
sandbox but NEVER touch the real filesystem paths.  Only an explicit
"promote" operation (not wired to any agent) would flush a sandbox to disk —
and that is not implemented here, keeping real source safe.

Capabilities registered
────────────────────────
  ide.agent.list          — list the three IDE agents
  ide.agent.chat          — route a prompt to thinker | writer | analyser
  ide.instances           — list Ollama instances with tier labels
  ide.models              — models available per instance
  ide.generate            — raw generation through a named agent
  ide.stream              — SSE token stream (HTTP endpoint only)
  ide.sandbox.load        — load real source files into sandbox (read-only copy)
  ide.sandbox.read        — read a file from sandbox
  ide.sandbox.write       — write/patch a file in sandbox (sandbox only)
  ide.sandbox.list        — list sandboxed files
  ide.sandbox.diff        — unified diff: sandbox vs original
  ide.sandbox.clear       — wipe sandbox
  ide.fs.list             — list directory on real FS
  ide.fs.read             — read a real file (read-only)
  ide.fs.write            — write a real file
  ide.fs.delete           — delete a real file
  ide.git.status          — git status for a path
  ide.git.commit          — git commit staged changes
  ide.git.log             — git log
  ide.git.diff            — git diff

UI panel
─────────
  ide-panel               — Full IDE UI (served from ide_panel.html via /ide/panel)
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.config import cfg
from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, OLLAMA_INSTANCES, OLLAMA_MODEL,
    UI_PANELS,
    capability, emit_event, now_iso, ollama_generate, pick_instance,
    record_stream_activity,
    register_routing_profile, register_ui, resolve_role, schedule,
)

_HERE = Path(__file__).parent

log = logging.getLogger("vera.ide")
# ─────────────────────────────────────────────────────────────────────────────
# GRAPH + FABRIC HELPERS (inline — no separate integration module)
# ─────────────────────────────────────────────────────────────────────────────

def _ide_session_id() -> str:
    """Get session_id from syslog trigger chain."""
    try:
        sl = sys.modules.get("syslog")
        if sl:
            return sl.get_trigger_chain().get("session_id", "")
    except Exception:
        pass
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# SESSION-SANDBOX ROUTING (Phase 6)
# When a session has an ACTIVE sandbox (session_sandbox_capabilities), its
# ide.fs.* operations touch the CONTAINER's filesystem, never the host. These
# helpers are no-ops (return None) when the module isn't loaded or the session
# has no active sandbox — the ide.fs cap then runs on the host as before.
# ─────────────────────────────────────────────────────────────────────────────
def _sandbox_mod():
    m = sys.modules.get("session_sandbox_capabilities")
    if m is not None and hasattr(m, "route_fs_read"):
        return m
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.endswith("session_sandbox_capabilities") \
                and hasattr(mod, "route_fs_read"):
            return mod
    return None


async def _route_fs(fn: str, session_id: str, *args, **kwargs):
    """Call session_sandbox_capabilities.<fn> for `session_id`; returns its dict
    (operation ran in the container) or None (no active sandbox → run on host)."""
    sid = session_id or _ide_session_id()
    if not sid:
        return None
    sb = _sandbox_mod()
    if sb is None or not hasattr(sb, fn):
        return None
    try:
        return await getattr(sb, fn)(sid, *args, **kwargs)
    except Exception as e:
        log.debug("ide.fs sandbox route %s failed (host): %s", fn, e)
        return None


_CONTAINER_ROOTS = ("workspace", "data", "root")


def _looks_like_container_path(path: str) -> bool:
    """True for an absolute path that carries SANDBOX semantics — a '/workspace/…'
    (or /data, /root) path. When sandbox routing is unavailable these cannot be
    honoured literally on the (Linux) backend host: a non-root process EACCESes
    trying to mkdir at the filesystem root, and a root one would create a real host
    '/workspace' that in-container exec still can't see. Either way the write
    belongs in the session's artifact dir, not at the host root."""
    p = str(path or "").replace("\\", "/")
    if not p.startswith("/"):
        return False
    low = p.lower().lstrip("/")
    return any(low == r or low.startswith(r + "/") for r in _CONTAINER_ROOTS)


def _container_relpath(path: str) -> str:
    """Strip a leading sandbox root ('/workspace/…', '/data/…', '/home/<user>/…')
    so the remainder can be written under the host artifact dir with its structure
    preserved."""
    p = str(path or "").replace("\\", "/").lstrip("/")
    for root in _CONTAINER_ROOTS:
        if p.lower().startswith(root + "/"):
            return p[len(root) + 1:]
    if p.lower().startswith("home/"):
        parts = p.split("/", 2)
        return parts[2] if len(parts) == 3 else p
    return p


async def _host_artifact_write(path: str, content: str, agent: str, sid: str) -> Optional[Dict]:
    """Fallback for a sandbox-style absolute path when NO sandbox routing is
    available (auto_create off / docker down): persist it under the session's host
    artifact dir via the sandbox-aware artifact writer, so the write still lands
    somewhere the loop can read back instead of failing with EACCES on '/workspace'."""
    try:
        import importlib as _il
        _exec = _il.import_module("Vera.vera.execution.exec_capabilities")
        rel = _container_relpath(path) or os.path.basename(str(path).replace("\\", "/"))
        fs_path = await _exec.write_artifact_file(relpath=rel, content=content, session_id=sid)
        asyncio.ensure_future(_ide_record_file(path, content, agent, sid))
        return {"path": fs_path, "bytes": len((content or "").encode("utf-8", "replace")),
                "created": True, "redirected_from": path}
    except Exception as e:
        log.debug("ide_fs_write artifact redirect failed for %s: %s", path, e)
        return None



# ─────────────────────────────────────────────────────────────────────────────
# ACTIVITY TRACKING ENGINE
# Sequential graph + fabric + Redis recorder — matches research_capabilities.
# ─────────────────────────────────────────────────────────────────────────────

_SESSION_CURSOR: dict = {}   # session_id -> last node_id
_FABRIC_DEDUP:   set  = set()

_FABRIC_DATASET_MAP_IDE = {
    "ide.workspace":      "ide.workspaces",
    "ide.agent_prompt":   "ide.agent_turns",
    "ide.agent_response": "ide.agent_turns",
    "ide.generate":       "ide.agent_turns",
    "ide.file_write":     "ide.file_writes",
    "ide.sandbox":        "ide.sandbox_ops",
    "ide.git":            "ide.git_ops",
}

def _ide_get_session_id() -> str:
    try:
        sl = sys.modules.get("syslog")
        if sl:
            return sl.get_trigger_chain().get("session_id", "")
    except Exception:
        pass
    return ""


async def _record(
    session_id:      str,
    category:        str,
    text:            str,
    full_text:       str   = "",
    tags:            list  = None,
    metadata:        dict  = None,
    importance:      float = 0.6,
    source_type:     str   = "tool",
    record_type:     str   = "event",
    capability_name: str   = "",
    broadcast_type:  str   = "activity.recorded",
    fabric_dataset:  str   = "",
    fabric_data:     dict  = None,
    dedup_key:       str   = "",
    extra_link:      tuple = None,
) -> str:
    """
    Core sequential activity recorder for all IDE operations.
    Stores MemoryRecord, links SESSION_CONTENT + FOLLOWS_ACTIVITY chain,
    broadcasts Redis event, ingests to data fabric with dedup.
    """
    node_id = str(uuid.uuid4())
    ts      = now_iso()
    tags    = tags or []
    meta    = metadata or {}
    ds      = fabric_dataset or _FABRIC_DATASET_MAP_IDE.get(
        category, category.replace(".", "_"))

    # 1. Memory graph
    graph_ok = False
    try:
        mem_mod = sys.modules.get("memory")
        if mem_mod:
            MEMORY, MemRecord = mem_mod.MEMORY, mem_mod.MemoryRecord
            rec = MemRecord(
                id=node_id, session_id=session_id,
                record_type=record_type, source_type=source_type,
                category=category, tags=tags,
                text=text[:500], full_text=full_text or text,
                importance=importance, capability=capability_name,
                metadata=meta, created_at=ts, updated_at=ts,
            )
            await MEMORY.store(rec)
            graph_ok = True
    except Exception as e:
        log.warning("ide _record graph [%s]: %s", category, e)

    # 2. Graph edges: FOLLOWS_ACTIVITY chain (the (:Session)-[:CONTAINS]->
    #    (:Memory) edge is auto-created by the Neo4j memory backend on every
    #    record store, so we don't add a separate SESSION_CONTENT edge —
    #    that previously caused duplicate parent-of edges in the graph.)
    if graph_ok and session_id:
        try:
            hooks = sys.modules.get("memory_hooks")
            if hooks:
                # Ensure :Session node exists. Returns the session_id; the
                # actual edge to this record is created by the Neo4j store.
                await hooks.get_or_create_session(session_id)
                prior = _SESSION_CURSOR.get(session_id, "")
                if prior and prior != node_id:
                    await hooks._link_nodes(
                        prior, node_id, "FOLLOWS_ACTIVITY",
                        {"category": category, "ts": ts}, session_id=session_id)
                    log.info("ide _record chain %s->[FOLLOWS]->%s session=%s",
                             prior[:8], node_id[:8], session_id[:12])
                if extra_link:
                    fid, rel = extra_link
                    if fid and fid != node_id:
                        await hooks._link_nodes(
                            fid, node_id, rel,
                            {"category": category}, session_id=session_id)
        except Exception as e:
            log.warning("ide _record edges [%s]: %s", category, e)

    if session_id and node_id:
        _SESSION_CURSOR[session_id] = node_id

    # 3. Redis broadcast
    try:
        ev = {"type": broadcast_type, "node_id": node_id, "session_id": session_id,
              "category": category, "text": text[:200], "tags": tags,
              "importance": importance, "ts": ts}
        ev.update({k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))})
        await emit_event(ev)
    except Exception as e:
        log.debug("ide _record broadcast: %s", e)

    # 4. Fabric ingest with dedup
    dk = dedup_key or (session_id + ":" + ds + ":" + node_id)
    if dk not in _FABRIC_DEDUP:
        try:
            fabric = sys.modules.get("data_fabric")
            if fabric:
                fdata = {"node_id": node_id, "session_id": session_id,
                         "category": category, "tags": tags, "ts": ts,
                         **(fabric_data or {})}
                await fabric.ingest_dataset(
                    dataset_id=ds,
                    data=[{"text": text[:4000], **fdata}],
                    source="ide",
                    source_id=session_id or node_id,
                    tags=tags,
                )
                _FABRIC_DEDUP.add(dk)
                log.info("ide _record fabric [%s] node=%s", ds, node_id[:8])
            else:
                log.warning("ide _record: data_fabric not loaded, %s not stored", category)
        except Exception as e:
            log.warning("ide _record fabric [%s]: %s", category, e)

    return node_id


# ── Recording helpers built on _record() ──────────────────────────────────────

async def _ide_record_agent_turn(prompt: str, response: str, agent: str,
                                  model: str, session_id: str,
                                  context_files: list = None):
    """Record IDE agent prompt -> response as two sequentially chained nodes."""
    prompt_id = await _record(
        session_id=session_id, category="ide.agent_prompt",
        text="[IDE/" + agent + "] " + prompt[:180],
        full_text=prompt,
        tags=["ide", "prompt", agent],
        importance=0.6, source_type="human", record_type="message",
        capability_name="ide.agent.chat", broadcast_type="ide.agent_prompt",
        fabric_dataset="ide.agent_turns",
        metadata={"agent": agent, "model": model,
                  "context_files": (context_files or [])[:10]},
        fabric_data={"agent": agent, "model": model,
                     "prompt": prompt[:5000],
                     "context_files": context_files or []},
    )
    await _record(
        session_id=session_id, category="ide.agent_response",
        text="[IDE/" + agent + " response] " + response[:180],
        full_text=response[:50000],
        tags=["ide", "response", agent, model],
        importance=0.7, source_type="ai", record_type="message",
        capability_name="ide.agent.chat", broadcast_type="ide.agent_response",
        fabric_dataset="ide.agent_turns",
        metadata={"agent": agent, "model": model},
        fabric_data={"agent": agent, "model": model,
                     "response": response[:50000],
                     "prompt_id": prompt_id},
        dedup_key="ide_resp:" + session_id + ":" + prompt_id,
        extra_link=(prompt_id, "CAUSES") if prompt_id else None,
    )


async def _ide_record_file(path: str, content: str, agent: str, session_id: str):
    """Record a file write. Content stored up to 50KB."""
    filename = path.split("/")[-1] if path else ""
    ext  = path.rsplit(".", 1)[-1].lower() if path and "." in path else "txt"
    lang = {"py": "python", "js": "javascript", "ts": "typescript", "rs": "rust",
            "go": "go", "md": "markdown", "html": "html", "sh": "shell",
            "css": "css", "json": "json", "yaml": "yaml", "toml": "toml"}.get(ext, ext)
    size = len((content or "").encode("utf-8", errors="replace"))
    content_stored = (content or "")[:51200]
    await _record(
        session_id=session_id, category="ide.file_write",
        text="File: " + filename + " (" + str(size) + "b)" + (" by " + agent if agent else ""),
        full_text="Path: " + path + "\nAgent: " + agent + "\nSize: " + str(size) + "\n\n" + content_stored[:800],
        tags=["ide", "file", "generated"] + ([agent] if agent else []),
        importance=0.65, source_type="ai" if agent else "tool",
        record_type="observation",
        capability_name="ide.fs.write", broadcast_type="ide.file_written",
        fabric_dataset="ide.file_writes",
        metadata={"path": path, "bytes": size, "agent": agent, "language": lang},
        fabric_data={"path": path, "filename": filename, "language": lang,
                     "bytes": size, "agent": agent, "content": content_stored},
        dedup_key="file:" + path + ":" + str(size),
    )


async def _ide_record_workspace(path: str, name: str, session_id: str,
                                 file_count: int = 0, template: str = ""):
    """Record workspace open/create event."""
    ws_name = name or (path.split("/")[-1] if path else path)
    await _record(
        session_id=session_id, category="ide.workspace",
        text="IDE workspace: " + ws_name,
        full_text="Workspace: " + ws_name + "\nPath: " + path + "\nFiles: " + str(file_count),
        tags=["ide", "workspace", "opened"],
        importance=0.5, source_type="tool", record_type="event",
        capability_name="ide.workspace.open", broadcast_type="ide.workspace_opened",
        fabric_dataset="ide.workspaces",
        metadata={"path": path, "name": ws_name,
                  "file_count": file_count, "template": template},
        fabric_data={"name": ws_name, "path": path,
                     "file_count": file_count, "template": template,
                     "opened_at": now_iso()},
        dedup_key="ws:" + session_id + ":" + path,
    )


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

IDE_AGENT_THINKER  = "ide-thinker"
IDE_AGENT_WRITER   = "ide-writer"
IDE_AGENT_ANALYSER = "ide-analyser"

# In-memory sandbox: { session_id: { "original": {path: str}, "draft": {path: str} } }
IDE_SANDBOX: Dict[str, Dict[str, Dict[str, str]]] = {}

# Tier → preferred instance label mapping (matches agents.py conventions)
TIER_LABELS = {
    "thinker":  "Thinker",
    "writer":   "Writer",
    "analyser": "Analyser",
}

# Agent presets: (system_prompt, temperature, top_p, instance_pref)
_AGENT_PRESETS = {
    IDE_AGENT_THINKER: {
        "label":         "Thinker",
        "avatar":        "🧠",
        "description":   "High-level reasoning, planning and architectural analysis.",
        "system_prompt": (
            "You are Vera's Thinker — a senior software architect and reasoning engine. "
            "You excel at breaking down complex problems, planning multi-file changes, "
            "designing APIs, and explaining architectural trade-offs. "
            "Think step by step. Be precise and actionable."
        ),
        "temperature":  0.75,
        "top_p":        0.92,
        "prefer_gpu":   True,
        "model":        "",          # uses cluster default / GPU
        "instance_id":  "",
        "num_ctx":      16384,
        "tool_mode":    "none",
    },
    IDE_AGENT_WRITER: {
        "label":         "Writer",
        "avatar":        "✍️",
        "description":   "Code generation, completion, scaffolding and refactoring.",
        "system_prompt": (
            "You are Vera's Writer — a professional code generation engine. "
            "Your outputs are clean, idiomatic, production-ready code. "
            "When asked to write or modify code always output the COMPLETE file content "
            "unless explicitly told to output only a snippet. "
            "Follow the language conventions of the project. No markdown prose — "
            "wrap code in appropriate fences only when the user expects explanation."
        ),
        "temperature":  0.2,
        "top_p":        0.85,
        "prefer_gpu":   False,       # Writer routes to Writer Ollama node
        "model":        "",
        "instance_id":  "",
        "num_ctx":      32768,
        "tool_mode":    "none",
    },
    IDE_AGENT_ANALYSER: {
        "label":         "Analyser",
        "avatar":        "🔬",
        "description":   "Code review, bug detection, explanation and static analysis.",
        "system_prompt": (
            "You are Vera's Analyser — a code review and debugging specialist. "
            "You identify bugs, security issues, performance problems and style violations. "
            "You explain code clearly and suggest targeted, minimal fixes. "
            "Be specific: cite line numbers, variable names, exact function signatures. "
            "Severity labels: CRITICAL · HIGH · MEDIUM · LOW · INFO."
        ),
        "temperature":  0.05,
        "top_p":        0.80,
        "prefer_gpu":   False,
        "model":        "",
        "instance_id":  "",
        "num_ctx":      32768,
        "tool_mode":    "none",
    },
}

# ── Role-routing profile — the IDE trio resolves through the cluster router ──
# Instead of ad-hoc label matching, the three IDE agents map to the roles of
# the "ide" routing profile (editable in the Model Routing page): thinker /
# writer / verifier (the analyser IS the verifier role). Thinker prefers GPU;
# writer + verifier stay off it by default so reviews/completions never queue
# behind reasoning work — override per role in the routing UI.
IDE_ROLE_BY_AGENT = {
    IDE_AGENT_THINKER:  "thinker",
    IDE_AGENT_WRITER:   "writer",
    IDE_AGENT_ANALYSER: "verifier",
}
try:
    register_routing_profile("ide", label="IDE", owner="ide", roles={
        "thinker":  {"job_type": "code", "prefer_gpu": True,  "label": "IDE · thinker"},
        "writer":   {"job_type": "code", "prefer_gpu": False, "label": "IDE · writer"},
        "verifier": {"job_type": "code", "prefer_gpu": False, "label": "IDE · verifier"},
    })
except Exception as _rp_err:
    log.warning("ide routing profile registration failed: %s", _rp_err)

# ─────────────────────────────────────────────────────────────────────────────
# AGENT REGISTRY (lazy import to avoid circular import at load time)
# ─────────────────────────────────────────────────────────────────────────────

def _get_agent_registry():
    try:
        from Vera.Agents.agents import AGENT_REGISTRY, AGENT_RUNNER
        return AGENT_REGISTRY, AGENT_RUNNER
    except Exception:
        return None, None


async def _ensure_ide_agents():
    """Seed the three IDE agents if they don't exist yet."""
    registry, _ = _get_agent_registry()
    if registry is None:
        log.warning("ide_capabilities: agent registry not available — skipping agent seeding")
        return

    for name, cfg in _AGENT_PRESETS.items():
        existing = await registry.get_by_name(name)
        if existing:
            continue
        try:
            from Vera.Agents.agents import AgentRecord
            rec = AgentRecord(
                id=str(uuid.uuid4()),
                name=name,
                label=cfg["label"],
                description=cfg["description"],
                avatar=cfg["avatar"],
                model=cfg.get("model", ""),
                instance_id=cfg.get("instance_id", ""),
                prefer_gpu=cfg.get("prefer_gpu", False),
                temperature=cfg["temperature"],
                top_p=cfg["top_p"],
                top_k=40,
                repeat_penalty=1.1,
                repeat_last_n=64,
                num_ctx=cfg.get("num_ctx", 8192),
                num_predict=-1,
                seed=-1,
                mirostat=0,
                mirostat_tau=5.0,
                mirostat_eta=0.1,
                tfs_z=1.0,
                stop=[],
                system_prompt=cfg["system_prompt"],
                greeting="",
                voice="af_heart",
                tts_speed=1.0,
                tts_engine="",
                domain_caps=[],
                domain_description=cfg["description"],
                tool_mode=cfg.get("tool_mode", "none"),
                think=False,
                skill_ids=[],
                ontology_ids=[],
                memory_enabled=False,
                memory_inject=False,
                memory_inject_limit=0,
                memory_tags="",
            )
            await registry.save(rec)
            log.info("ide_capabilities: seeded agent '%s'", name)
        except Exception as e:
            log.error("ide_capabilities: failed to seed agent '%s': %s", name, e)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _ide_role(agent_role: str) -> str:
    """Normalise an agent name/role ('ide-thinker', 'analyser', …) to its
    routing-profile role: thinker | writer | verifier."""
    r = (agent_role or "").lower().replace("ide-", "").strip()
    return {"thinker": "thinker", "writer": "writer",
            "analyser": "verifier", "analyzer": "verifier",
            "verifier": "verifier"}.get(r, "writer")


def _pick_ide_instance(agent_role: str) -> Optional[str]:
    """Pick the Ollama node for an IDE agent role through the cluster router
    (role profile 'ide' — rules editable in the Model Routing page). Falls
    back to the plain least-busy picker when the profile can't resolve."""
    role = _ide_role(agent_role)
    try:
        res = resolve_role("ide", role)
        if res and res.get("instance_id"):
            return res["instance_id"]
    except Exception as e:
        log.debug("ide role route %s: %s", role, e)
    return pick_instance(prefer_gpu=(role == "thinker"))


async def _agent_generate(agent_name: str, prompt: str, system: str = "",
                          history: list = None, model: str = "",
                          stream_cb=None) -> str:
    """
    Generate a response using a named IDE agent.
    Falls back to plain ollama_generate if the agent system is unavailable.
    """
    registry, runner = _get_agent_registry()
    if registry and runner:
        agent = await registry.get_by_name(agent_name)
        if agent:
            import copy
            ag = copy.copy(agent)
            if model:
                ag.model = model
            result = await runner.run(ag, prompt, history or [], "")
            return result.get("text", "") if isinstance(result, dict) else str(result)

    # Fallback — use the preset config; node choice flows through the cluster
    # router via the 'ide' role profile (profile+role kwargs).
    preset = _AGENT_PRESETS.get(agent_name, {})
    full_system = (preset.get("system_prompt", "") + "\n\n" + system).strip() if system else preset.get("system_prompt", "")
    return await ollama_generate(
        prompt,
        system=full_system,
        model=model or preset.get("model") or OLLAMA_MODEL,
        profile="ide", role=IDE_ROLE_BY_AGENT.get(agent_name, "writer"),
        stream_cb=stream_cb,
    )


# ─────────────────────────────────────────────────────────────────────────────
# AGENT CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ide.agent.list",
    http_method="GET", http_path="/ide/agents", http_tags=["ide", "agents"],
    memory="off",
    description="List the three IDE agents: thinker, writer, analyser. "
                "Returns their names, labels, descriptions and current status.",
)
async def ide_agent_list(trace_id=None):
    registry, _ = _get_agent_registry()
    agents = []
    for name, preset in _AGENT_PRESETS.items():
        rec = None
        if registry:
            rec = await registry.get_by_name(name)
        agents.append({
            "name":        name,
            "label":       preset["label"],
            "avatar":      preset["avatar"],
            "description": preset["description"],
            "registered":  rec is not None,
            "id":          rec.id if rec else None,
            "model":       (rec.model if rec else preset.get("model")) or OLLAMA_MODEL,
            "temperature": rec.temperature if rec else preset["temperature"],
            "prefer_gpu":  rec.prefer_gpu if rec else preset.get("prefer_gpu", False),
        })
    return {"agents": agents}


@capability(
    "ide.agent.chat",
    http_method="POST", http_path="/ide/agents/chat", http_tags=["ide", "agents"],
    memory="off",
    description="Send a prompt to one of the IDE agents. "
                "Input: agent (thinker|writer|analyser), prompt (str!), "
                "system (str), history (JSON array), model (str), "
                "context_files (JSON: {path: content}). "
                "Output: {text, agent, model, instance}.",
)
async def ide_agent_chat(
    agent:         str  = "writer",
    prompt:        str  = "",
    system:        str  = "",
    history:       str  = "[]",
    model:         str  = "",
    context_files: str  = "{}",
    session_id:    str  = "",
    trace_id=None,
):
    agent_name = {
        "thinker":  IDE_AGENT_THINKER,
        "writer":   IDE_AGENT_WRITER,
        "analyser": IDE_AGENT_ANALYSER,
    }.get(agent.lower(), IDE_AGENT_WRITER)

    try:
        hist = json.loads(history)
    except Exception:
        hist = []

    try:
        ctx = json.loads(context_files)
    except Exception:
        ctx = {}

    # Prepend file context to prompt
    if ctx:
        file_block = "\n\n".join(
            f"--- FILE: {path} ---\n```\n{content}\n```"
            for path, content in ctx.items()
        )
        full_prompt = f"{file_block}\n\n{prompt}"
    else:
        full_prompt = prompt

    iid = _pick_ide_instance(agent_name)
    text = await _agent_generate(agent_name, full_prompt, system=system,
                                  history=hist, model=model)
    sid = session_id or _ide_session_id()
    await emit_event({"type": "ide.agent.chat", "agent": agent_name,
                      "chars": len(text), "session_id": sid,
                      "prompt_snippet": prompt[:80]})

    # Record conversation turn to memory graph + fabric
    asyncio.ensure_future(_ide_record_agent_turn(
        prompt=prompt, response=text, agent=agent_name,
        model=model or OLLAMA_MODEL, session_id=sid,
        context_files=list(ctx.keys()) if ctx else [],
    ))
    return {
        "text":     text,
        "agent":    agent_name,
        "model":    model or OLLAMA_MODEL,
        "instance": iid or "unknown",
    }


# ─────────────────────────────────────────────────────────────────────────────
# INSTANCES / MODELS
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ide.instances",
    http_method="GET", http_path="/ide/instances", http_tags=["ide", "ollama"],
    memory="off",
    description="List Ollama instances available to the IDE with tier labels, "
                "status, latency and available models. "
                "Output: {instances: [{id, label, tier, url, status, latency_ms, models, has_gpu}]}",
)
async def ide_instances(trace_id=None):
    result = []
    tier_map = {"gpu": "thinker", "cpu-246": "writer", "cpu-247": "analyser"}
    for iid, inst in OLLAMA_INSTANCES.items():
        # Derive a tier label: GPU node → thinker, CPU-A → writer, CPU-B → analyser
        tier = "writer"
        if inst.get("has_gpu"):
            tier = "thinker"
        elif "246" in inst.get("url", ""):
            tier = "writer"
        elif "247" in inst.get("url", ""):
            tier = "analyser"

        result.append({
            "id":         iid,
            "label":      inst.get("label", iid),
            "tier":       tier,
            "url":        inst.get("url", ""),
            "status":     inst.get("status", "unknown"),
            "latency_ms": inst.get("latency_ms"),
            "models":     inst.get("models", []),
            "has_gpu":    inst.get("has_gpu", False),
            "in_use":     inst.get("in_use", 0),
        })
    return {"instances": result}


@capability(
    "ide.models",
    http_method="GET", http_path="/ide/models", http_tags=["ide", "ollama"],
    memory="off",
    description="List all models available across all online Ollama instances. "
                "Output: {models: [{name, instances: [id]}]}",
)
async def ide_models(trace_id=None):
    model_map: Dict[str, list] = {}
    for iid, inst in OLLAMA_INSTANCES.items():
        if inst.get("status") != "online":
            continue
        for m in inst.get("models", []):
            model_map.setdefault(m, []).append(iid)
    models = [{"name": m, "instances": iids} for m, iids in sorted(model_map.items())]
    return {"models": models}


@capability(
    "ide.generate",
    http_method="POST", http_path="/ide/generate", http_tags=["ide", "llm"],
    memory="off",
    description="Generate text via a named IDE agent (thinker|writer|analyser). "
                "Input: agent (str), prompt (str!), system (str), model (str), "
                "instance_id (str), temperature (float). "
                "Output: {text, agent, model}.",
)
async def ide_generate(
    agent:       str   = "writer",
    prompt:      str   = "",
    system:      str   = "",
    model:       str   = "",
    instance_id: str   = "",
    temperature: float = -1.0,   # -1 = use agent default
    session_id:  str   = "",
    trace_id=None,
):
    agent_name = {
        "thinker":  IDE_AGENT_THINKER,
        "writer":   IDE_AGENT_WRITER,
        "analyser": IDE_AGENT_ANALYSER,
    }.get(agent.lower(), IDE_AGENT_WRITER)

    preset = _AGENT_PRESETS.get(agent_name, {})
    iid = instance_id or _pick_ide_instance(agent_name)
    mdl = model or preset.get("model") or OLLAMA_MODEL
    sys_p = (preset.get("system_prompt", "") + "\n\n" + system).strip() if system else preset.get("system_prompt", "")

    # Build options
    opts: dict = {"num_ctx": preset.get("num_ctx", 8192)}
    if temperature >= 0:
        opts["temperature"] = temperature
    else:
        opts["temperature"] = preset.get("temperature", 0.3)

    chosen = iid or pick_instance(prefer_gpu=preset.get("prefer_gpu", False))
    if not chosen:
        return {"error": "No online Ollama instance", "text": ""}

    inst = OLLAMA_INSTANCES.get(chosen, {})
    url  = inst.get("url", "")
    body = {"model": mdl, "prompt": prompt, "stream": False, "options": opts}
    if sys_p:
        body["system"] = sys_p

    # ── Log the Ollama request ───────────────────────────────────────────────
    import time as _time
    from Vera.vera.capability_orchestration import (
        emit_event, _ollama_log_append, _ollama_caller_info,
    )
    _req_id = str(uuid.uuid4())[:12]
    _t0 = _time.time()
    _prompt_preview = (prompt or "")[:120].replace("\n", " ")
    _prompt_full = (prompt or "")[:16000]
    log.info("ollama_req [%s] model=%s inst=%s caller=ide_capabilities:ide_generate agent=%s prompt=%s",
             _req_id, mdl, chosen, agent_name, _prompt_preview)
    try:
        await emit_event({
            "type": "ollama.request", "req_id": _req_id,
            "model": mdl, "instance_id": chosen, "instance_url": url,
            "caller_file": "ide_capabilities.py", "caller_func": "ide_generate",
            "caller_module": "ide_capabilities", "cap_name": "ide.generate",
            "prompt_preview": _prompt_preview, "prompt_full": _prompt_full, "json_mode": False,
            "prefer_gpu": preset.get("prefer_gpu", False), "streaming": False,
        })
    except Exception:
        pass

    inst["in_use"] = inst.get("in_use", 0) + 1
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{url}/api/generate", json=body)
            r.raise_for_status()
            d = r.json()
            text = d.get("response", "")
        _elapsed = round(_time.time() - _t0, 2)
        log.info("ollama_done [%s] %.2fs caller=ide_capabilities:ide_generate agent=%s",
                 _req_id, _elapsed, agent_name)
        _ollama_log_append({
            "req_id": _req_id, "model": mdl, "instance": chosen,
            "caller_file": "ide_capabilities.py", "caller_func": "ide_generate",
            "prompt_preview": _prompt_preview, "ts": now_iso(),
            "status": "done", "elapsed_s": _elapsed,
            "eval_count": d.get("eval_count", 0),
        })
        try:
            await emit_event({
                "type": "ollama.request_done", "req_id": _req_id,
                "model": mdl, "instance_id": chosen,
                "caller_file": "ide_capabilities.py", "caller_func": "ide_generate",
                "elapsed_s": _elapsed, "eval_count": d.get("eval_count", 0),
            })
        except Exception:
            pass
        sid = session_id or _ide_get_session_id()
        asyncio.ensure_future(_record(
            session_id=sid, category="ide.generate",
            text="[IDE/" + agent_name + "] " + prompt[:180],
            full_text="Prompt: " + prompt + "\n\nResponse: " + text[:50000],
            tags=["ide", "generate", agent_name, mdl],
            importance=0.7, source_type="ai", record_type="message",
            capability_name="ide.generate", broadcast_type="ide.generation",
            fabric_dataset="ide.agent_turns",
            metadata={"agent": agent_name, "model": mdl, "instance": chosen},
            fabric_data={"agent": agent_name, "model": mdl,
                         "prompt": prompt[:5000], "response": text[:50000]},
        ))
        return {"text": text, "agent": agent_name, "model": mdl, "instance": chosen}
    except Exception as e:
        from Vera.vera.capability_orchestration import _err_text
        _elapsed = round(_time.time() - _t0, 2)
        _err = _err_text(e)
        log.error("ollama_generate [%s] FAILED after %.2fs inst=%s caller=ide_capabilities:ide_generate err=%s",
                  _req_id, _elapsed, chosen, _err)
        _ollama_log_append({
            "req_id": _req_id, "model": mdl, "instance": chosen,
            "caller_file": "ide_capabilities.py", "caller_func": "ide_generate",
            "prompt_preview": _prompt_preview, "ts": now_iso(),
            "status": "error", "elapsed_s": _elapsed, "error": _err,
        })
        try:
            await emit_event({
                "type": "ollama.request_error", "req_id": _req_id,
                "model": mdl, "instance_id": chosen,
                "caller_file": "ide_capabilities.py", "caller_func": "ide_generate",
                "elapsed_s": _elapsed, "error": _err,
                "error_type": type(e).__name__,
            })
        except Exception:
            pass
        return {"error": _err, "text": "", "agent": agent_name}
    finally:
        inst["in_use"] = max(0, inst.get("in_use", 1) - 1)


# ─────────────────────────────────────────────────────────────────────────────
# SSE STREAM ENDPOINT  (not a @capability — needs raw StreamingResponse)
# ─────────────────────────────────────────────────────────────────────────────

@APP.post("/ide/stream")
async def ide_stream_endpoint(request: Request):
    """
    SSE streaming endpoint for the IDE.
    Body: {agent, prompt, system, model, instance_id, context_files}
    Yields: text/event-stream  data: {"type":"token","text":"..."}
                               data: {"type":"done","text":"<full>"}
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    agent_name_short = body.get("agent", "writer")
    agent_name = {
        "thinker":  IDE_AGENT_THINKER,
        "writer":   IDE_AGENT_WRITER,
        "analyser": IDE_AGENT_ANALYSER,
    }.get(agent_name_short.lower(), IDE_AGENT_WRITER)

    prompt      = body.get("prompt", "")
    system      = body.get("system", "")
    model       = body.get("model") or OLLAMA_MODEL
    instance_id = body.get("instance_id") or None
    ctx_raw     = body.get("context_files", {})
    if isinstance(ctx_raw, str):
        try:    ctx_raw = json.loads(ctx_raw)
        except: ctx_raw = {}

    # Inject context files into prompt
    if ctx_raw:
        file_block = "\n\n".join(
            f"--- FILE: {p} ---\n```\n{c}\n```"
            for p, c in ctx_raw.items()
        )
        prompt = f"{file_block}\n\n{prompt}"

    preset = _AGENT_PRESETS.get(agent_name, {})
    full_system = (preset.get("system_prompt", "") + "\n\n" + system).strip() if system else preset.get("system_prompt", "")

    chosen = instance_id or _pick_ide_instance(agent_name)
    if not chosen:
        async def _err():
            yield b'data: {"type":"error","text":"No online Ollama instance"}\n\n'
        return StreamingResponse(_err(), media_type="text/event-stream")

    inst = OLLAMA_INSTANCES.get(chosen, {})
    url  = inst.get("url", "")
    opts = {
        "num_ctx":     preset.get("num_ctx", 8192),
        "temperature": preset.get("temperature", 0.3),
    }
    ol_body = {"model": model, "prompt": prompt, "stream": True, "options": opts}
    if full_system:
        ol_body["system"] = full_system

    async def _generate():
        import time as _time
        from Vera.vera.capability_orchestration import (
            emit_event as _emit_event, _ollama_log_append,
        )
        _req_id = str(uuid.uuid4())[:12]
        _t0_stream = _time.monotonic()
        _prompt_preview = (prompt or "")[:120].replace("\n", " ")
        _prompt_full = (prompt or "")[:16000]
        log.info("ollama_req [%s] model=%s inst=%s caller=ide_capabilities:ide_stream agent=%s prompt=%s",
                 _req_id, model, chosen, agent_name_short, _prompt_preview)
        try:
            await _emit_event({
                "type": "ollama.request", "req_id": _req_id,
                "model": model, "instance_id": chosen, "instance_url": url,
                "caller_file": "ide_capabilities.py", "caller_func": "ide_stream_endpoint",
                "caller_module": "ide_capabilities", "cap_name": "ide.stream",
                "prompt_preview": _prompt_preview, "prompt_full": _prompt_full, "json_mode": False,
                "prefer_gpu": preset.get("prefer_gpu", False), "streaming": True,
            })
        except Exception:
            pass
        yield b": ping\n\n"
        full = []
        error_text = ""
        inst["in_use"] = inst.get("in_use", 0) + 1
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as c:
                async with c.stream("POST", f"{url}/api/generate", json=ol_body) as resp:
                    if resp.status_code != 200:
                        err = await resp.aread()
                        error_text = err.decode()[:500]
                        yield f"data: {json.dumps({'type':'error','text':error_text[:200]})}\n\n".encode()
                        return
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            token = json.loads(line).get("response", "")
                        except Exception:
                            continue
                        if token:
                            full.append(token)
                            yield f"data: {json.dumps({'type':'token','text':token})}\n\n".encode()
        except Exception as e:
            from Vera.vera.capability_orchestration import _err_text
            error_text = _err_text(e)
            yield f"data: {json.dumps({'type':'error','text':error_text})}\n\n".encode()
            return
        finally:
            inst["in_use"] = max(0, inst.get("in_use", 1) - 1)
            # ── Log completion of the Ollama request ─────────────────────────
            # Must live in the finally: the error paths above `return` out of
            # the generator, so code after this block never runs for them —
            # previously errors left the request dangling as "running" forever.
            _elapsed_s = round((_time.monotonic() - _t0_stream), 2)
            if error_text:
                log.error("ollama_generate [%s] FAILED after %.2fs caller=ide_capabilities:ide_stream err=%s",
                          _req_id, _elapsed_s, error_text[:120])
                _ollama_log_append({
                    "req_id": _req_id, "model": model, "instance": chosen,
                    "caller_file": "ide_capabilities.py", "caller_func": "ide_stream_endpoint",
                    "prompt_preview": _prompt_preview, "ts": now_iso(),
                    "status": "error", "elapsed_s": _elapsed_s, "error": error_text[:300],
                })
                try:
                    await _emit_event({
                        "type": "ollama.request_error", "req_id": _req_id,
                        "model": model, "instance_id": chosen,
                        "caller_file": "ide_capabilities.py", "caller_func": "ide_stream_endpoint",
                        "elapsed_s": _elapsed_s, "error": error_text[:300],
                    })
                except Exception:
                    pass
            else:
                log.info("ollama_done [%s] %.2fs tokens=%d caller=ide_capabilities:ide_stream",
                         _req_id, _elapsed_s, len(full))
                _ollama_log_append({
                    "req_id": _req_id, "model": model, "instance": chosen,
                    "caller_file": "ide_capabilities.py", "caller_func": "ide_stream_endpoint",
                    "prompt_preview": _prompt_preview, "ts": now_iso(),
                    "status": "done", "elapsed_s": _elapsed_s, "tokens": len(full),
                })
                try:
                    await _emit_event({
                        "type": "ollama.request_done", "req_id": _req_id,
                        "model": model, "instance_id": chosen,
                        "caller_file": "ide_capabilities.py", "caller_func": "ide_stream_endpoint",
                        "elapsed_s": _elapsed_s, "token_count": len(full),
                    })
                except Exception:
                    pass

        full_text = "".join(full)
        _sid = body.get("session_id", "") or _ide_get_session_id()
        # 1) IDE-domain event recording — keeps the IDE module's own
        #    FOLLOWS_ACTIVITY chain (used by the IDE panel's history view)
        #    intact. Writes a single ide.generate node.
        asyncio.ensure_future(_record(
            session_id=_sid, category="ide.generate",
            text="[IDE/" + agent_name_short + " stream] " + prompt[:180],
            full_text="Prompt: " + prompt + "\n\nResponse: " + full_text[:50000],
            tags=["ide", "stream", agent_name_short],
            importance=0.7, source_type="ai", record_type="message",
            capability_name="ide.stream", broadcast_type="ide.stream_done",
            fabric_dataset="ide.agent_turns",
            metadata={"agent": agent_name_short, "model": model, "instance": chosen},
            fabric_data={"agent": agent_name_short, "model": model,
                         "prompt": prompt[:5000], "response": full_text[:50000]},
        ))
        # 2) Unified-path recording — emits cap.call/cap.ok so this raw
        #    streaming endpoint is visible in syslog and the cap_tracking
        #    panel like every @capability call. Uses cap_name='ide.stream'.
        elapsed_ms = round((_time.monotonic() - _t0_stream) * 1000)
        try:
            await record_stream_activity(
                cap_name="ide.stream", session_id=_sid,
                params={
                    "agent":         agent_name_short,
                    "model":         model,
                    "instance_id":   chosen,
                    "prompt":        prompt,
                    "system":        system,
                    "context_files": list(ctx_raw.keys())[:20] if isinstance(ctx_raw, dict) else [],
                },
                result={
                    "agent":          agent_name_short,
                    "response_chars": len(full_text),
                    "preview":        full_text[:800],
                    "elapsed_ms":     elapsed_ms,
                    "error":          error_text or None,
                },
                elapsed_ms=elapsed_ms,
                group="ide",
            )
        except Exception as _e:
            log.debug("record_stream_activity ide.stream: %s", _e)
        yield f"data: {json.dumps({'type':'done','text':full_text})}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# SANDBOX CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

def _session(session_id: str) -> dict:
    """Return or create a sandbox session dict."""
    if session_id not in IDE_SANDBOX:
        IDE_SANDBOX[session_id] = {"original": {}, "draft": {}}
    return IDE_SANDBOX[session_id]


@capability(
    "ide.sandbox.load",
    http_method="POST", http_path="/ide/sandbox/load", http_tags=["ide", "sandbox"],
    memory="off",
    description="Load real filesystem files into the sandbox (read-only snapshot). "
                "The original source files are NEVER modified. "
                "Input: paths (JSON list of absolute paths), session_id (str). "
                "Output: {session_id, loaded: [path], errors: {path: error}}.",
)
async def ide_sandbox_load(
    paths:      str  = "[]",
    session_id: str  = "",
    trace_id=None,
):
    session_id = session_id or str(uuid.uuid4())
    sess = _session(session_id)
    try:
        path_list: List[str] = json.loads(paths)
    except Exception:
        return {"error": "Invalid paths JSON", "session_id": session_id}

    loaded = []
    errors = {}
    for p in path_list:
        try:
            content = Path(p).read_text(errors="replace")
            sess["original"][p] = content
            sess["draft"][p]    = content      # start draft == original
            loaded.append(p)
        except Exception as e:
            errors[p] = str(e)

    await emit_event({"type": "ide.sandbox.load", "session_id": session_id, "loaded": len(loaded)})
    return {"session_id": session_id, "loaded": loaded, "errors": errors}


@capability(
    "ide.sandbox.read",
    http_method="POST", http_path="/ide/sandbox/read", http_tags=["ide", "sandbox"],
    memory="off",
    description="Read a file from the sandbox draft (not from real FS). "
                "Input: path (str!), session_id (str!). "
                "Output: {path, content, lines}.",
)
async def ide_sandbox_read(path: str, session_id: str, trace_id=None):
    sess = IDE_SANDBOX.get(session_id)
    if not sess:
        return {"error": f"Session '{session_id}' not found"}
    draft = sess["draft"]
    if path not in draft:
        return {"error": f"'{path}' not in sandbox session '{session_id}'"}
    content = draft[path]
    return {"path": path, "content": content, "lines": content.count("\n") + 1}


@capability(
    "ide.sandbox.write",
    http_method="POST", http_path="/ide/sandbox/write", http_tags=["ide", "sandbox"],
    memory="off",
    description="Write or replace a file in the sandbox. "
                "This NEVER touches the real filesystem. "
                "Input: path (str!), content (str!), session_id (str!). "
                "Output: {path, bytes, session_id}.",
)
async def ide_sandbox_write(path: str, content: str, session_id: str, trace_id=None):
    sess = _session(session_id)
    sess["draft"][path] = content
    return {"path": path, "bytes": len(content.encode()), "session_id": session_id}


@capability(
    "ide.sandbox.list",
    http_method="POST", http_path="/ide/sandbox/list", http_tags=["ide", "sandbox"],
    memory="off",
    description="List files in the sandbox for a session. "
                "Input: session_id (str!). "
                "Output: {session_id, files: [{path, original_lines, draft_lines, modified}]}.",
)
async def ide_sandbox_list(session_id: str, trace_id=None):
    sess = IDE_SANDBOX.get(session_id)
    if not sess:
        return {"session_id": session_id, "files": []}
    files = []
    for path, orig in sess["original"].items():
        draft = sess["draft"].get(path, orig)
        files.append({
            "path":           path,
            "original_lines": orig.count("\n") + 1,
            "draft_lines":    draft.count("\n") + 1,
            "modified":       draft != orig,
        })
    # Also include files added to draft but not in original
    for path, draft in sess["draft"].items():
        if path not in sess["original"]:
            files.append({
                "path":           path,
                "original_lines": 0,
                "draft_lines":    draft.count("\n") + 1,
                "modified":       True,
            })
    return {"session_id": session_id, "files": files}


@capability(
    "ide.sandbox.diff",
    http_method="POST", http_path="/ide/sandbox/diff", http_tags=["ide", "sandbox"],
    memory="off",
    description="Get a unified diff between the sandbox draft and the original for one or all files. "
                "Input: session_id (str!), path (str, optional — omit for all). "
                "Output: {diffs: {path: unified_diff_str}}.",
)
async def ide_sandbox_diff(session_id: str, path: str = "", trace_id=None):
    sess = IDE_SANDBOX.get(session_id)
    if not sess:
        return {"error": f"Session '{session_id}' not found", "diffs": {}}
    targets = [path] if path else list(sess["draft"].keys())
    diffs = {}
    for p in targets:
        orig  = sess["original"].get(p, "").splitlines(keepends=True)
        draft = sess["draft"].get(p, "").splitlines(keepends=True)
        udiff = "".join(difflib.unified_diff(orig, draft, fromfile=f"original/{p}", tofile=f"sandbox/{p}"))
        if udiff:
            diffs[p] = udiff
    return {"session_id": session_id, "diffs": diffs}


@capability(
    "ide.sandbox.clear",
    http_method="POST", http_path="/ide/sandbox/clear", http_tags=["ide", "sandbox"],
    memory="off",
    description="Clear / delete a sandbox session. "
                "Input: session_id (str!). Output: {cleared: bool}.",
)
async def ide_sandbox_clear(session_id: str, trace_id=None):
    existed = session_id in IDE_SANDBOX
    IDE_SANDBOX.pop(session_id, None)
    return {"cleared": existed, "session_id": session_id}


# ─────────────────────────────────────────────────────────────────────────────
# FILESYSTEM CAPABILITIES (real FS — read + write)
# ─────────────────────────────────────────────────────────────────────────────
# ide.fs.list, ide.fs.browse, ide.fs.roots, ide.workspace.* are defined
# further below with the full implementation including readable flag and GET method.
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ide.fs.read",
    http_method="GET", http_path="/ide/fs/read", http_tags=["ide", "fs"],
    memory="off", silent=True,
    description="Read a file from the real filesystem. GET with ?path=... "
                "Output: {path, content, size, truncated}.",
)
async def ide_fs_read(path: str, max_bytes: int = 1_048_576,
                      session_id: str = "", trace_id=None):
    routed = await _route_fs("route_fs_read", session_id, path, max_bytes=max_bytes)
    if routed is not None:
        return routed
    try:
        p = Path(path)
        if not p.exists():
            return {"error": f"File not found: {path}"}
        size = p.stat().st_size
        content = p.read_text(errors="replace")[:max_bytes]
        return {"path": path, "content": content, "size": size,
                "truncated": size > max_bytes}
    except Exception as e:
        return {"error": str(e)}


def _unescape_collapsed(content: str) -> str:
    """Recover a file an LLM double-escaped — it emitted literal "\\n"/"\\t"
    instead of real newlines/tabs, collapsing the whole file onto one line
    (a frequent small-model failure that makes saved scripts unrunnable).

    Conservative: only fires when there is NO real newline at all and at least
    two literal "\\n" sequences are present, so normal multi-line content (and a
    one-off "\\n" inside a string literal) is left untouched."""
    if not content or "\n" in content:
        return content
    if content.count("\\n") < 2:
        return content
    return (content.replace("\\r\\n", "\n").replace("\\n", "\n")
                   .replace("\\t", "\t"))


def _strip_wrapping_fence(content: str) -> str:
    """Strip a SINGLE markdown code fence that wraps the ENTIRE payload — the
    ```json / ```python fences an agent leaves on when it pipes a generative
    cap's raw output straight into a write. Only strips when the fenced body has
    no inner fences (so a real markdown/docs file that legitimately contains code
    blocks is never mangled). Conservative — returns content unchanged otherwise."""
    if not content:
        return content
    t = content.strip()
    if not t.startswith("```"):
        return content
    nl = t.find("\n")
    if nl == -1:
        return content
    body = t[nl + 1:].rstrip()
    if not body.endswith("```"):
        return content
    inner = body[:-3].rstrip("\n")
    if "```" in inner:            # inner fences → a real md/docs file, leave it
        return content
    return inner


def _sanitize_file_content(content: str) -> str:
    """Best-effort cleanup for content an agent piped straight from a generative
    cap: unwrap a lone {"text"|"code"|"content"|"output": "..."} JSON envelope
    (llm.generate's return shape), then strip a single fence wrapping the whole
    payload. Prevents saved files from containing ```json braces or a `{"text":
    "...\\n..."}` wrapper with escaped newlines. Leaves normal file content alone."""
    if not content or not isinstance(content, str):
        return content
    s = content.strip()
    if s[:1] == "{" and s[-1:] == "}" and '"' in s:
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                for k in ("text", "code", "content", "output"):
                    v = obj.get(k)
                    if isinstance(v, str) and v.strip():
                        content = v
                        break
        except Exception:
            pass
    return _strip_wrapping_fence(content)


@capability(
    "ide.fs.write",
    http_method="POST", http_path="/ide/fs/write", http_tags=["ide", "fs"],
    memory="off",
    description="Write content to a real filesystem file (creates parent dirs). "
                "Input: path (str!), content (str!), agent (str), session_id (str). "
                "Output: {path, bytes, created}.",
)
async def ide_fs_write(path: str, content: str, agent: str = "", session_id: str = "", trace_id=None):
    content = _sanitize_file_content(_unescape_collapsed(content))
    routed = await _route_fs("route_fs_write", session_id, path, content)
    if routed is not None:
        # Record the write to the graph/fabric even when it lands in the container.
        if not routed.get("error"):
            asyncio.ensure_future(_ide_record_file(
                path, content, agent, session_id or _ide_session_id()))
        return routed
    sid = session_id or _ide_session_id()
    # No sandbox routing (auto_create off / docker down). An absolute sandbox-style
    # path can't be written literally on the host — redirect it into the session's
    # host artifact dir rather than failing with EACCES on '/workspace'.
    if _looks_like_container_path(path):
        redirected = await _host_artifact_write(path, content, agent, sid)
        if redirected is not None:
            return redirected
    try:
        p = Path(path)
        created = not p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        size = len(content.encode("utf-8", errors="replace"))
        asyncio.ensure_future(_ide_record_file(path, content, agent, sid))
        return {"path": path, "bytes": size, "created": created}
    except Exception as e:
        log.warning("ide_fs_write: %s — path=%s", e, path)
        return {"error": str(e)}


@capability(
    "ide.fs.delete",
    http_method="POST", http_path="/ide/fs/delete", http_tags=["ide", "fs"],
    memory="off",
    description="Delete a file from the real filesystem. "
                "Input: path (str!). Output: {path, deleted}.",
)
async def ide_fs_delete(path: str, session_id: str = "", trace_id=None):
    routed = await _route_fs("route_fs_delete", session_id, path)
    if routed is not None:
        return routed
    try:
        p = Path(path)
        if not p.exists():
            return {"error": f"File not found: {path}"}
        p.unlink()
        await emit_event({"type": "ide.fs.delete", "path": path})
        return {"path": path, "deleted": True}
    except Exception as e:
        return {"error": str(e), "deleted": False}


@capability(
    "ide.fs.roots",
    http_method="GET", http_path="/ide/fs/roots", http_tags=["ide", "fs"],
    memory="off", silent=True,
    description="List filesystem root mount points available to the IDE. "
                "Returns common project roots: home, /tmp, and any env-configured paths. "
                "Output: {roots: [str]}",
)
async def ide_fs_roots(trace_id=None):
    """Return root locations as objects with name/path/kind for the folder browser."""
    try:
        roots = []
        seen = set()

        def _add(path: str, name: str, kind: str = "directory"):
            p = str(Path(path).resolve())
            if p not in seen and Path(p).exists():
                seen.add(p)
                roots.append({"name": name, "path": p, "kind": kind})

        # Always include PROJECT_ROOT first (most useful for IDE work)
        _add(str(PROJECT_ROOT), "📁 Projects (vera_projects)", "workspace")

        # User home
        _add(str(Path.home()), "🏠 Home", "directory")

        # Env-configured workspace
        ws = os.getenv("VERA_WORKSPACE", "")
        if ws:
            _add(ws, f"⚙ VERA_WORKSPACE ({Path(ws).name})", "directory")

        # Saved workspaces
        for w in _load_workspaces():
            wp = w.get("path", "")
            wn = w.get("name", Path(wp).name if wp else "?")
            if wp:
                _add(wp, f"🗂 {wn}", "workspace")

        # Common server paths
        for candidate, label in [("/opt","📦 /opt"), ("/srv","📦 /srv"),
                                  ("/data","📦 /data"), ("/workspace","📦 /workspace"),
                                  ("/projects","📦 /projects")]:
            _add(candidate, label, "directory")

        if not roots:
            _add(str(Path.home()), "🏠 Home", "directory")

        return {"roots": roots}
    except Exception as e:
        log.warning("ide_fs_roots: %s", e)
        home = str(Path.home())
        return {"roots": [{"name": "🏠 Home", "path": home, "kind": "directory"}]}


@capability(
    "ide.fs.browse",
    http_method="GET", http_path="/ide/fs/browse", http_tags=["ide", "fs"],
    memory="off", silent=True,
    description="Browse a directory, returning entries with breadcrumb navigation data. "
                "Input: path (str — query param). "
                "Output: {path, parent, crumbs: [{name,path}], "
                "entries: [{name, path, kind, size, mtime, readable}]}",
)
async def ide_fs_browse(path: str = "", session_id: str = "", trace_id=None):
    routed = await _route_fs("route_fs_browse", session_id, path)
    if routed is not None:
        return routed
    target = Path(path) if path else Path.home()
    try:
        if not target.exists():
            return {"error": f"Path not found: {target}"}
        if not target.is_dir():
            target = target.parent

        # Breadcrumbs
        crumbs = []
        parts = target.parts
        for i, part in enumerate(parts):
            crumb_path = str(Path(*parts[:i+1])) if i > 0 else str(Path(parts[0]))
            crumbs.append({"name": part or "/", "path": crumb_path})

        parent = str(target.parent) if target != target.parent else None

        entries = []
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                st = entry.stat()
                entries.append({
                    "name":     entry.name,
                    "path":     str(entry),
                    "kind":     "directory" if entry.is_dir() else "file",
                    "size":     st.st_size,
                    "mtime":    st.st_mtime,
                    "readable": os.access(str(entry), os.R_OK),
                })
            except (PermissionError, OSError):
                entries.append({
                    "name": entry.name, "path": str(entry),
                    "kind": "directory" if entry.is_dir() else "file",
                    "size": 0, "mtime": 0, "readable": False,
                })

        return {
            "path":    str(target),
            "parent":  parent,
            "crumbs":  crumbs,
            "entries": entries,
        }
    except Exception as e:
        return {"error": str(e), "path": str(target), "parent": None, "crumbs": [], "entries": []}


# Update ide.fs.list to include readable flag (matches what the panel expects)
# The original capability is already defined above — we re-register with the
# readable field added. Override the existing registration.
@capability(
    "ide.fs.list",
    http_method="GET", http_path="/ide/fs/list", http_tags=["ide", "fs"],
    memory="off", silent=True,
    description="List a directory. GET with ?path=... query param. "
                "Output: {path, entries: [{name, path, kind, size, mtime, readable}]}",
)
async def ide_fs_list_v2(path: str = "", recursive: bool = False,
                         session_id: str = "", trace_id=None):
    routed = await _route_fs("route_fs_list", session_id, path)
    if routed is not None:
        return routed
    target = Path(path) if path else Path.home()
    try:
        if not target.exists():
            return {"error": f"Path not found: {path}", "entries": []}
        entries = []
        iterator = target.rglob("*") if recursive else target.iterdir()
        for entry in sorted(iterator, key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                st = entry.stat()
                entries.append({
                    "name":     entry.name,
                    "path":     str(entry),
                    "kind":     "directory" if entry.is_dir() else "file",
                    "size":     st.st_size,
                    "mtime":    st.st_mtime,
                    "readable": os.access(str(entry), os.R_OK),
                })
            except (PermissionError, OSError):
                pass
        return {"path": str(target), "entries": entries}
    except Exception as e:
        return {"error": str(e), "entries": []}


# ─── Workspace helpers (named project shortcuts) ──────────────────────────────
# ── Project / Workspace root ─────────────────────────────────────────────────
# Use cfg.VERA_PROJECT_ROOT if available (requires updated config.py),
# otherwise fall back to ~/vera_projects so old config.py still works.
try:
    _proj_root_str = cfg.VERA_PROJECT_ROOT
except AttributeError:
    _proj_root_str = os.path.join(os.path.expanduser("~"), "vera_projects")

PROJECT_ROOT = Path(_proj_root_str)
try:
    PROJECT_ROOT.mkdir(parents=True, exist_ok=True)
    log.info("ide: PROJECT_ROOT = %s", PROJECT_ROOT)
except Exception as _e:
    log.warning("ide: could not create PROJECT_ROOT %s: %s", PROJECT_ROOT, _e)

_WORKSPACES_FILE = Path(__file__).parent / ".vera_workspaces.json"

def _load_workspaces() -> list:
    try:
        return json.loads(_WORKSPACES_FILE.read_text()) if _WORKSPACES_FILE.exists() else []
    except Exception:
        return []

def _save_workspaces(ws: list):
    try:
        _WORKSPACES_FILE.write_text(json.dumps(ws, indent=2))
    except Exception as e:
        log.warning("ide: could not save workspaces: %s", e)


@capability(
    "ide.workspace.list",
    http_method="GET", http_path="/ide/workspace/list", http_tags=["ide", "workspace"],
    memory="off", silent=True,
    description="List saved IDE workspaces (named project folder shortcuts) and "
                "any source-inspection snapshots. Output: {workspaces: [{name, "
                "path, created_at, kind}], project_root, snapshot_count}. "
                "kind is 'workspace' for user-created workspaces or 'snapshot' "
                "for source-inspection snapshots.",
)
async def ide_workspace_list(trace_id=None):
    """List saved workspaces + source-inspection snapshots. Always returns
    {workspaces: [...], project_root: str, snapshot_count: int}."""
    result = []
    snapshot_count = 0
    try:
        ws = _load_workspaces()
        result = []
        for w in ws:
            p = Path(w.get("path", ""))
            result.append({**w, "exists": p.exists(), "kind": w.get("kind", "workspace")})
    except Exception as e:
        log.warning("ide_workspace_list: %s", e)

    # Also surface source-inspection snapshots as workspaces so dream sensors
    # (and anything else looking for a "workspace") can target them. The inspect
    # module exposes snapshots under PROJECT_ROOT/__vera_inspect__/<stamp>/ —
    # we prefer to query the cap if it's loaded so we don't reimplement path logic.
    try:
        inspect_cap = CAPABILITY_REGISTRY.get("ide.inspect.list_snapshots")
        if inspect_cap:
            try:
                snap_res = await inspect_cap["func"]()
                if isinstance(snap_res, dict):
                    for s in (snap_res.get("snapshots") or []):
                        sp = s.get("path", "")
                        if not sp:
                            continue
                        result.append({
                            "name":        f"snapshot:{s.get('id', '?')}",
                            "path":        sp,
                            "created_at":  s.get("created_at", ""),
                            "exists":      Path(sp).exists() if sp else False,
                            "kind":        "snapshot",
                            "label":       s.get("label", ""),
                            "file_count":  s.get("file_count", 0),
                            "is_fresh":    s.get("is_fresh", False),
                            "source_hash": s.get("source_hash", ""),
                        })
                        snapshot_count += 1
            except Exception as e:
                log.debug("ide_workspace_list: inspect snapshot listing failed: %s", e)
    except Exception:
        pass

    return {
        "workspaces":     result,
        "project_root":   str(PROJECT_ROOT),
        "snapshot_count": snapshot_count,
    }
@capability(
    "ide.workspace.create",
    http_method="POST", http_path="/ide/workspace/create", http_tags=["ide", "workspace"],
    memory="off",
    description="Create a new IDE workspace (named project folder). "
                "Input: name (str!), path (str, optional — defaults to ~/name), "
                "template (str: empty|python|node|rust). "
                "Output: {name, path, created_at}.",
)
async def ide_workspace_create(
    name:       str,
    path:       str  = "",
    template:   str  = "empty",
    session_id: str  = "",
    trace_id=None,
):
    # Resolve workspace path — prefer explicit path, then PROJECT_ROOT/name
    ws_path = Path(path) if path else PROJECT_ROOT / name

    already_exists = ws_path.exists()

    if not already_exists:
        try:
            ws_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"error": f"Could not create directory: {e}"}

        # Apply template only on fresh directories
        if template == "python":
            (ws_path / "main.py").write_text('#!/usr/bin/env python3\n\ndef main():\n    print("Hello, world!")\n\nif __name__ == "__main__":\n    main()\n')
            (ws_path / "requirements.txt").write_text("# Add your dependencies here\n")
            (ws_path / ".gitignore").write_text("__pycache__/\n*.pyc\n.venv/\ndist/\nbuild/\n*.egg-info/\n")
        elif template == "node":
            (ws_path / "index.js").write_text("'use strict';\nconsole.log('Hello, world!');\n")
            (ws_path / "package.json").write_text(json.dumps({"name": name, "version": "1.0.0", "main": "index.js"}, indent=2) + "\n")
            (ws_path / ".gitignore").write_text("node_modules/\n.env\ndist/\n")
        elif template == "rust":
            src = ws_path / "src"
            src.mkdir(exist_ok=True)
            (src / "main.rs").write_text('fn main() {\n    println!("Hello, world!");\n}\n')
            (ws_path / "Cargo.toml").write_text(f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n')
        # Always init README for new workspaces
        readme = ws_path / "README.md"
        if not readme.exists():
            readme.write_text(f"# {name}\n\nCreated by Vera IDE.\n")

    rec = {
        "name":       name,
        "path":       str(ws_path),
        "created_at": now_iso(),
        "template":   template,
        "opened":     already_exists,  # True if directory already existed
    }
    ws = _load_workspaces()
    ws = [w for w in ws if w.get("name") != name]
    ws.append(rec)
    _save_workspaces(ws)

    event_type = "ide.workspace.opened" if already_exists else "ide.workspace.created"
    await emit_event({"type": event_type, "name": name, "path": str(ws_path),
                      "existed": already_exists})

    # Record to graph + fabric — prefer explicit session_id, fall back to chain
    sid = session_id or _ide_session_id()
    asyncio.ensure_future(_ide_record_workspace(str(ws_path), name, sid))

    # Per-workspace sandbox: link the opening session to a SHARED `ws-<name>`
    # container so every session working in this workspace shares one confined
    # environment (auto-created on first exec when auto_create is on).
    if sid:
        try:
            sb = _sandbox_mod()
            if sb is not None and hasattr(sb, "link_session"):
                await sb.link_session(sid, f"ws-{name}", kind="workspace", label=name)
                rec["sandbox"] = f"ws-{name}"
                # Seed the shared container from the workspace's HOST files so any
                # session OR agentic loop working in ws-<name> sees the real
                # project (applied when a fresh container comes up empty).
                if hasattr(sb, "set_seed_path"):
                    await sb.set_seed_path(f"ws-{name}", str(ws_path))
        except Exception as e:
            log.debug("workspace sandbox link failed for %s: %s", name, e)
    return rec


# ─────────────────────────────────────────────────────────────────────────────
# WORKSPACE CHANGE PROPOSALS — PR-style, GATED write-back of a sandbox/loop's
# work to the human's IDE workspace folder. An agentic loop operates on a CLONE
# of the workspace in its container; on close (or on demand) its CHANGED files
# become a reviewable PROPOSAL the human accepts or rejects per-file. Nothing is
# ever written to the workspace without an explicit accept.
# ─────────────────────────────────────────────────────────────────────────────
KEY_WS_PROPOSALS   = "vera:ide:change_proposals"   # HASH id -> proposal JSON
_WS_PROP_MAX_FILES = 300
_WS_PROP_MAX_BYTES = 500_000        # per-file content cap; larger → not auto-appliable


def _redis():
    return getattr(_orch, "REDIS", None)


def _within(base: str, target: str) -> bool:
    """True when `target` resolves INSIDE `base` (blocks ../ escapes)."""
    try:
        b = os.path.realpath(base)
        t = os.path.realpath(target)
        return t == b or t.startswith(b + os.sep)
    except Exception:
        return False


def _resolve_ws_host_path(workspace: str, host_path: str) -> str:
    hp = (host_path or "").strip()
    if hp and os.path.isdir(hp):
        return hp
    ws = (workspace or "").strip()
    if ws:
        for w in _load_workspaces():
            if str(w.get("name") or "").strip().lower() == ws.lower():
                p = str(w.get("path") or "")
                if p and os.path.isdir(p):
                    return p
        if (("/" in ws) or ("\\" in ws)) and os.path.isdir(ws):
            return ws
    return ""


# Clobber-safety compare-and-swap primitives live in a pure, unit-testable module.
from Vera.vera.ide.ws_changes_core import (          # noqa: E402
    sha256_file as _sha256_file,
    accept_conflict as _ws_accept_conflict,
)


def _build_ws_proposal_files(host_path: str, export_dir: str,
                             only_paths=None) -> List[Dict]:
    """Compare a NEW-versions dir (`export_dir` — a container export, or a git
    worktree) against a TARGET dir (`host_path` — the workspace/repo to apply to)
    and build per-file proposal entries (added/modified, with a unified diff).
    `only_paths` limits the comparison to specific rel paths (used when the
    source dir is huge, e.g. a repo worktree — pass the git-changed set); when
    None the whole export_dir is walked. Unchanged / identical files are skipped."""
    if only_paths is not None:
        rels = sorted(str(p).replace("\\", "/").strip() for p in only_paths if str(p).strip())
    else:
        rels = []
        for root, _dirs, fnames in os.walk(export_dir):
            for fn in fnames:
                rels.append(os.path.relpath(os.path.join(root, fn),
                                            export_dir).replace("\\", "/"))
        rels.sort()
    files: List[Dict] = []
    for rel in rels:
        if rel == ".vera_seed_marker" or os.path.basename(rel) == ".vera_seed_marker":
            continue
        if rel.startswith(".git/") or "/.git/" in ("/" + rel):
            continue
        fp = os.path.join(export_dir, rel)
        if not os.path.isfile(fp):
            continue
        try:
            data = open(fp, "rb").read()
        except Exception:
            continue
        try:
            new_text = data.decode("utf-8")
            is_text = True
        except Exception:
            new_text, is_text = "", False
        host_file = os.path.join(host_path, rel)
        exists = os.path.exists(host_file)
        # Hash the target's CURRENT bytes (None if absent) so accept can do a
        # compare-and-swap: it will refuse to write any file whose live target
        # has drifted from this reviewed base, making a clobber impossible.
        base_sha = _sha256_file(host_file) if exists else None
        old_text = ""
        if exists and is_text:
            try:
                old_text = open(host_file, "r", encoding="utf-8",
                                errors="replace").read()
            except Exception:
                old_text = ""
        if is_text and exists and old_text == new_text:
            continue                          # genuinely unchanged
        status = "modified" if exists else "added"
        entry: Dict = {"rel": rel, "status": status, "bytes": len(data),
                       "decision": "pending", "text": is_text, "base_sha": base_sha}
        if not is_text:
            entry["binary"] = True            # recorded, not auto-appliable as text
        else:
            too_big = len(new_text) > _WS_PROP_MAX_BYTES
            entry["truncated"] = too_big
            entry["new_content"] = None if too_big else new_text
            diff = "\n".join(difflib.unified_diff(
                old_text.splitlines(), new_text.splitlines(),
                fromfile=("a/" + rel if exists else "/dev/null"),
                tofile="b/" + rel, lineterm=""))
            entry["diff"] = diff[:20000]
        files.append(entry)
        if len(files) >= _WS_PROP_MAX_FILES:
            break
    return files


async def _ws_proposal_get(pid: str) -> Optional[Dict]:
    r = _redis()
    if not r or not pid:
        return None
    try:
        raw = await r.hget(KEY_WS_PROPOSALS, pid)
        if raw:
            return json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception:
        pass
    return None


async def _ws_proposal_save(prop: Dict) -> None:
    r = _redis()
    if r:
        try:
            await r.hset(KEY_WS_PROPOSALS, prop["id"], json.dumps(prop, default=str))
        except Exception as e:
            log.debug("ws proposal save: %s", e)


@capability(
    "ide.workspace.changes.propose", memory="off",
    http_method="POST", http_path="/ide/workspace/changes/propose", http_tags=["ide", "workspace"],
    description="Build a PR-style change PROPOSAL from a sandbox/loop container's "
                "CHANGED files vs the human's IDE workspace folder — nothing is written "
                "yet. Inputs: session_id (str! — the container/owner key, e.g. goal-<slug> "
                "or ws-<name>), workspace (str — IDE workspace name) OR host_path (str), "
                "source (str — who produced it, e.g. v8:<pid>). Output: {ok, proposal:{id, "
                "files, status}}.",
)
async def ide_ws_changes_propose(session_id: str = "", workspace: str = "",
                                 host_path: str = "", source: str = "", trace_id=None):
    hp = _resolve_ws_host_path(workspace, host_path)
    if not hp:
        return {"ok": False, "error": "workspace/host_path not resolvable"}
    sb = _sandbox_mod()
    exp = getattr(sb, "export_workspace_changes", None) if sb else None
    if not exp:
        return {"ok": False, "error": "sandbox export unavailable"}
    tmp = await exp(session_id)
    if not tmp:
        return {"ok": True, "proposal": None, "note": "no container to diff"}
    try:
        files = _build_ws_proposal_files(hp, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not files:
        return {"ok": True, "proposal": None, "note": "no changes vs workspace"}
    pid = uuid.uuid4().hex[:8]
    prop = {"id": pid, "workspace": (workspace or "").strip(), "host_path": hp,
            "source": (source or "").strip(), "created": now_iso(),
            "status": "pending", "files": files}
    await _ws_proposal_save(prop)
    await emit_event({"type": "ide.workspace.changes.proposed", "id": pid,
                      "workspace": prop["workspace"], "host_path": hp,
                      "files": len(files), "source": prop["source"]})
    return {"ok": True, "proposal": {"id": pid, "workspace": prop["workspace"],
            "files": len(files), "status": "pending"}}


@capability(
    "ide.workspace.changes.propose_dir", memory="off",
    http_method="POST", http_path="/ide/workspace/changes/propose_dir", http_tags=["ide", "workspace"],
    description="Build a PR-style change PROPOSAL by diffing a NEW-versions host "
                "directory against a TARGET directory — the general form used by any "
                "code-change source (e.g. Loop Lab's git worktree vs the base repo), so "
                "its changes land in the same Workspace Changes review panel. Inputs: "
                "source_dir (str! — dir holding the new file versions), target_dir "
                "(str! — dir the changes apply to; accept writes here), paths (csv of "
                "rel paths to limit to — pass the changed set for a large source like a "
                "repo worktree; empty = walk source_dir), source (str), workspace (str "
                "— display label). Output: {ok, proposal:{id, files, status}}.",
)
async def ide_ws_changes_propose_dir(source_dir: str = "", target_dir: str = "",
                                     paths: str = "", source: str = "",
                                     workspace: str = "", trace_id=None):
    if not source_dir or not os.path.isdir(source_dir):
        return {"ok": False, "error": f"source_dir not found: {source_dir}"}
    if not target_dir or not os.path.isdir(target_dir):
        return {"ok": False, "error": f"target_dir not found: {target_dir}"}
    only = {p.strip() for p in (paths or "").split(",") if p.strip()} or None
    files = _build_ws_proposal_files(target_dir, source_dir, only_paths=only)
    if not files:
        return {"ok": True, "proposal": None, "note": "no changes vs target"}
    pid = uuid.uuid4().hex[:8]
    prop = {"id": pid, "workspace": (workspace or "").strip(), "host_path": target_dir,
            "source": (source or "").strip(), "created": now_iso(),
            "status": "pending", "files": files}
    await _ws_proposal_save(prop)
    await emit_event({"type": "ide.workspace.changes.proposed", "id": pid,
                      "workspace": prop["workspace"], "host_path": target_dir,
                      "files": len(files), "source": prop["source"]})
    return {"ok": True, "proposal": {"id": pid, "workspace": prop["workspace"],
            "files": len(files), "status": "pending"}}


@capability(
    "ide.workspace.changes.list", memory="off", silent=True,
    http_method="GET", http_path="/ide/workspace/changes/list", http_tags=["ide", "workspace"],
    description="List workspace change proposals awaiting review. Input: status "
                "(pending|applied|rejected|partial, default pending; empty = all). "
                "Output: {proposals:[{id, workspace, source, files, pending, status}]}.",
)
async def ide_ws_changes_list(status: str = "pending", trace_id=None):
    r = _redis()
    out: List[Dict] = []
    if r:
        try:
            raw = await r.hgetall(KEY_WS_PROPOSALS)
        except Exception:
            raw = {}
        for _k, v in (raw or {}).items():
            try:
                p = json.loads(v.decode() if isinstance(v, (bytes, bytearray)) else v)
            except Exception:
                continue
            if status and p.get("status") != status:
                continue
            pend = sum(1 for f in p.get("files", []) if f.get("decision") == "pending")
            out.append({"id": p.get("id"), "workspace": p.get("workspace"),
                        "host_path": p.get("host_path"), "source": p.get("source"),
                        "created": p.get("created"), "status": p.get("status"),
                        "files": len(p.get("files", [])), "pending": pend})
    out.sort(key=lambda x: str(x.get("created") or ""), reverse=True)
    return {"proposals": out, "count": len(out)}


@capability(
    "ide.workspace.changes.get", memory="off", silent=True,
    http_method="GET", http_path="/ide/workspace/changes/get", http_tags=["ide", "workspace"],
    description="Full detail of one change proposal: every file with its status "
                "(added/modified/binary), unified diff and per-file decision. Input: "
                "id (str!). Output: the proposal (heavy file bodies omitted; use the diff).",
)
async def ide_ws_changes_get(id: str = "", trace_id=None):
    prop = await _ws_proposal_get(id)
    if not prop:
        return {"error": f"unknown proposal: {id}"}
    view = dict(prop)
    view["files"] = [{k: v for k, v in f.items() if k != "new_content"}
                     for f in prop.get("files", [])]
    return view


@capability(
    "ide.workspace.changes.accept", memory="on",
    http_method="POST", http_path="/ide/workspace/changes/accept", http_tags=["ide", "workspace"],
    description="ACCEPT (write to the workspace) files from a change proposal — the "
                "gated write-back. Inputs: id (str!), paths (csv of rels to accept), "
                "apply_all (bool default False — accept every text file). Binary and "
                "over-size files are skipped (returned in `skipped`). A file whose "
                "live target has changed since the proposal was built is NEVER "
                "overwritten — it is returned in `conflicts` and left pending "
                "(regenerate the proposal to pick up the new base). Output: {ok, "
                "applied:[rel], skipped:[rel], conflicts:[rel], status}.",
)
async def ide_ws_changes_accept(id: str = "", paths: str = "", apply_all: bool = False,
                                trace_id=None):
    prop = await _ws_proposal_get(id)
    if not prop:
        return {"ok": False, "error": f"unknown proposal: {id}"}
    host_path = prop.get("host_path") or ""
    if not host_path or not os.path.isdir(host_path):
        return {"ok": False, "error": "workspace host path no longer exists"}
    sel = {p.strip() for p in (paths or "").split(",") if p.strip()}
    applied, skipped, conflicts = [], [], []
    for f in prop.get("files", []):
        rel = f.get("rel")
        if not (apply_all or rel in sel):
            continue
        if f.get("binary") or f.get("truncated") or f.get("new_content") is None:
            skipped.append(rel)
            continue
        dest = os.path.join(host_path, rel)
        if not _within(host_path, dest):
            skipped.append(rel)
            continue
        # Compare-and-swap: refuse to write if the live target has drifted from
        # the base this proposal was reviewed against — never clobber newer work.
        cur_sha = _sha256_file(dest) if os.path.exists(dest) else None
        if _ws_accept_conflict(f, cur_sha):
            conflicts.append(rel)          # left pending; regenerate the proposal
            continue
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(f.get("new_content") or "")
            f["decision"] = "accept"
            applied.append(rel)
        except Exception as e:
            log.debug("ws accept write %s: %s", rel, e)
            skipped.append(rel)
    decs = [f.get("decision") for f in prop.get("files", [])]
    if all(d != "pending" for d in decs):
        prop["status"] = "applied" if any(d == "accept" for d in decs) else "rejected"
    elif any(d == "accept" for d in decs):
        prop["status"] = "partial"
    await _ws_proposal_save(prop)
    await emit_event({"type": "ide.workspace.changes.applied", "id": id,
                      "applied": len(applied), "conflicts": len(conflicts),
                      "workspace": prop.get("workspace")})
    return {"ok": True, "applied": applied, "skipped": skipped,
            "conflicts": conflicts, "status": prop.get("status")}


@capability(
    "ide.workspace.changes.reject", memory="on",
    http_method="POST", http_path="/ide/workspace/changes/reject", http_tags=["ide", "workspace"],
    description="REJECT files from a change proposal (discard — never written). "
                "Inputs: id (str!), paths (csv of rels), reject_all (bool default "
                "False — reject the whole proposal and delete it). Output: {ok, "
                "rejected, status}.",
)
async def ide_ws_changes_reject(id: str = "", paths: str = "", reject_all: bool = False,
                                trace_id=None):
    prop = await _ws_proposal_get(id)
    if not prop:
        return {"ok": False, "error": f"unknown proposal: {id}"}
    r = _redis()
    if reject_all:
        if r:
            try:
                await r.hdel(KEY_WS_PROPOSALS, id)
            except Exception:
                pass
        await emit_event({"type": "ide.workspace.changes.rejected", "id": id,
                          "workspace": prop.get("workspace"), "all": True})
        return {"ok": True, "rejected": len(prop.get("files", [])), "status": "rejected"}
    sel = {p.strip() for p in (paths or "").split(",") if p.strip()}
    n = 0
    for f in prop.get("files", []):
        if f.get("rel") in sel:
            f["decision"] = "reject"
            n += 1
    decs = [f.get("decision") for f in prop.get("files", [])]
    if all(d != "pending" for d in decs):
        prop["status"] = "applied" if any(d == "accept" for d in decs) else "rejected"
    await _ws_proposal_save(prop)
    return {"ok": True, "rejected": n, "status": prop.get("status")}


@capability(
    "ide.workspace.changes.mark_merged", memory="off", silent=True,
    http_method="POST", http_path="/ide/workspace/changes/mark_merged", http_tags=["ide", "workspace"],
    description="Mark a change proposal as MERGED (landed via git) WITHOUT writing "
                "any files — used when approval integrates the source branch through a "
                "git merge (evolve.sandbox.approve) instead of a file write-back, so a "
                "landed proposal clears the review queue without ever touching a live "
                "working tree. Inputs: id (str!), into (str), commit (str). "
                "Output: {ok, status}.",
)
async def ide_ws_changes_mark_merged(id: str = "", into: str = "", commit: str = "",
                                     trace_id=None):
    prop = await _ws_proposal_get(id)
    if not prop:
        return {"ok": False, "error": f"unknown proposal: {id}"}
    for f in prop.get("files", []):
        if f.get("decision") == "pending":
            f["decision"] = "merged"
    prop["status"] = "applied"
    prop["merged_into"] = into
    prop["merged_commit"] = commit
    await _ws_proposal_save(prop)
    await emit_event({"type": "ide.workspace.changes.merged", "id": id,
                      "into": into, "commit": commit, "workspace": prop.get("workspace")})
    return {"ok": True, "status": "applied"}


_CHANGES_PANEL_FILE = _HERE / "ide_changes_panel.html"


@capability(
    "ide.workspace.changes.panel_html",
    http_method="GET", http_path="/ide/changes/panel", http_tags=["ide", "workspace", "ui"],
    memory="off", silent=True,
    description="Serve the Workspace Changes review panel HTML (PR-style accept/reject "
                "of a loop's edits before they touch the workspace). Loaded fresh from "
                "disk each request so HTML edits need no restart.",
)
async def ide_ws_changes_panel_html(trace_id=None):
    from fastapi.responses import HTMLResponse as _HR
    try:
        return _HR(_CHANGES_PANEL_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return _HR(f"<h3>Workspace Changes panel missing: {e}</h3>", status_code=500)


register_ui(
    "ide-workspace-changes", "Workspace Changes", "🔀",
    """<div style="height:100%;display:flex;flex-direction:column;">
      <iframe src="/ide/changes/panel"
              style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#14161a);"
              allow="clipboard-read; clipboard-write"></iframe>
    </div>""",
    "", ui_caps=["ide.workspace.changes.list", "ide.workspace.changes.get",
                 "ide.workspace.changes.accept", "ide.workspace.changes.reject"],
    mode="inject",
    specialist_agent="code-editor",
    specialist_loop_profile="code-editing",
)


# ─────────────────────────────────────────────────────────────────────────────

def _git_sync(args: list, cwd: str) -> tuple[int, str, str]:
    """Run a git command; returns (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            ["git"] + args, cwd=cwd,
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", str(e)


async def _git(args: list, cwd: str) -> tuple[int, str, str]:
    """Async wrapper — every ide.git.* capability below is an async def
    calling this from the event loop, and subprocess.run() blocks for its
    FULL duration (up to the 30s timeout), not just the fork spawn cost.
    A caller that loops this per-item (ide.claude_sessions.list_sessions
    used to call ide_git_log once per session) turned one request into a
    long, fully-serialized freeze of the entire process — confirmed live as
    what took Vera offline. to_thread() keeps the loop free for everything
    else while this one call runs."""
    return await asyncio.to_thread(_git_sync, args, cwd)


@capability(
    "ide.git.status",
    http_method="POST", http_path="/ide/git/status", http_tags=["ide", "git"],
    memory="off",
    description="Get git status for a repository path. "
                "Input: path (str!). Output: {path, status, branch, staged, unstaged, untracked}.",
)
async def ide_git_status(path: str, trace_id=None):
    rc, out, err = await _git(["status", "--porcelain", "-b"], path)
    if rc != 0 and "not a git repository" in err:
        return {"error": "Not a git repository", "path": path}
    lines = out.splitlines()
    branch = ""
    staged, unstaged, untracked = [], [], []
    for line in lines:
        if line.startswith("## "):
            branch = line[3:].split("...")[0]
        elif line.startswith("??"):
            untracked.append(line[3:])
        elif line[:2].strip():
            if line[0] != " ":  staged.append(line[3:])
            if line[1] != " ":  unstaged.append(line[3:])
    return {"path": path, "branch": branch, "staged": staged,
            "unstaged": unstaged, "untracked": untracked, "raw": out}


@capability(
    "ide.git.commit",
    http_method="POST", http_path="/ide/git/commit", http_tags=["ide", "git"],
    memory="off",
    description="Stage all changes and commit. "
                "Input: path (str!), message (str!), add_all (bool, default True). "
                "Output: {success, output}.",
)
async def ide_git_commit(path: str, message: str, add_all: bool = True, trace_id=None):
    if add_all:
        rc, out, err = await _git(["add", "-A"], path)
        if rc != 0:
            return {"success": False, "output": err or out}
    rc, out, err = await _git(["commit", "-m", message], path)
    await emit_event({"type": "ide.git.commit", "path": path, "message": message})
    return {"success": rc == 0, "output": (out + err).strip()}


@capability(
    "ide.git.log",
    http_method="POST", http_path="/ide/git/log", http_tags=["ide", "git"],
    memory="off",
    description="Get git log for a repository, optionally windowed by time — the "
                "shared correlation primitive for tying a chat session or a Loop "
                "Lab run back to the commit(s) it produced (see "
                "ide.claude_sessions.* and evolve.* callers). "
                "Input: path (str!), n (int, default 20 — ignored when since/until "
                "given), since (str — git-parseable date/time, e.g. an ISO "
                "timestamp or unix epoch, inclusive lower bound), until (str — "
                "same, inclusive upper bound). "
                "Output: {commits: [{hash, author, date, ts, message}]}.",
)
async def ide_git_log(path: str, n: int = 20, since: str = "", until: str = "", trace_id=None):
    args = ["log", "--pretty=format:%H\x1f%an\x1f%ad\x1f%ct\x1f%s", "--date=short"]
    if since or until:
        # A time-windowed query is answering "what landed during this
        # session/run" — unbounded by count, since we want everything in
        # the window, not just the most recent N.
        if since:
            args.append(f"--since={since}")
        if until:
            args.append(f"--until={until}")
    else:
        args.append(f"-{n}")
    rc, out, err = await _git(args, path)
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 5:
            commits.append({"hash": parts[0][:8], "author": parts[1],
                            "date": parts[2], "ts": int(parts[3]), "message": parts[4]})
    return {"commits": commits, "path": path}


@capability(
    "ide.git.branches",
    http_method="POST", http_path="/ide/git/branches", http_tags=["ide", "git"],
    memory="off",
    description="List every local branch with its last commit and worktree status — "
                "the 'what is Vera/Claude Code actually working on right now' view, "
                "for the Dispatch panel's branch section. "
                "Input: path (str — repo path, defaults to this checkout's own "
                "root), base (str — branch to compare ahead/behind against, "
                "default 'main'). "
                "Output: {branches: [{name, current, hash, author, date, ts, "
                "message, ahead, behind, worktree}], path, base}.",
)
async def ide_git_branches(path: str = "", base: str = "main", trace_id=None):
    # ide_capabilities.py lives at <repo>/vera/ide/ — parents[2] is <repo>,
    # not parents[1] (<repo>/vera). Git itself doesn't care (it walks up to
    # find .git regardless of which subdirectory cwd is), so this was
    # functionally harmless, but the reported `path` field was wrong.
    path = path or str(Path(__file__).resolve().parents[2])
    rc, out, _err = await _git(
        ["for-each-ref", "--sort=-committerdate", "refs/heads/",
         "--format=%(refname:short)\x1f%(objectname:short)\x1f%(authorname)\x1f"
         "%(committerdate:short)\x1f%(committerdate:unix)\x1f%(subject)"],
        path)
    branches: list = []
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 6:
                continue
            name, h, author, date, ts, subject = parts
            branches.append({"name": name, "hash": h, "author": author,
                             "date": date, "ts": int(ts) if ts.isdigit() else 0,
                             "message": subject, "ahead": None, "behind": None,
                             "current": False, "worktree": ""})
    # Ahead/behind vs `base` — how far each branch has diverged, in both
    # directions, so a branch that's fallen behind main is visible, not just
    # branches that are ahead of it.
    for b in branches:
        if b["name"] == base:
            continue
        rc2, out2, _err2 = await _git(
            ["rev-list", "--left-right", "--count", f"{base}...{b['name']}"], path)
        if rc2 == 0:
            counts = out2.strip().split()
            if len(counts) == 2:
                b["behind"], b["ahead"] = int(counts[0]), int(counts[1])
    # Which branches have a LIVE worktree right now (this is prod's own
    # checkout, a Loop Lab dev-sandbox worktree, or any other active
    # checkout) — the "actually being worked on" signal, distinct from just
    # "has commits".
    rc3, out3, _err3 = await _git(["worktree", "list", "--porcelain"], path)
    by_branch_wt: dict = {}
    cur_wt_path = ""
    for line in out3.splitlines():
        if line.startswith("worktree "):
            cur_wt_path = line[len("worktree "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            by_branch_wt[ref.rsplit("/", 1)[-1]] = cur_wt_path
    rc4, out4, _err4 = await _git(["branch", "--show-current"], path)
    cur_name = out4.strip()
    for b in branches:
        b["current"] = (b["name"] == cur_name)
        b["worktree"] = by_branch_wt.get(b["name"], "")
    return {"branches": branches, "path": path, "base": base}


@capability(
    "ide.git.diff",
    http_method="POST", http_path="/ide/git/diff", http_tags=["ide", "git"],
    memory="off",
    description="Get git diff for a repository. "
                "Input: path (str!), staged (bool). "
                "Output: {diff, path}.",
)
async def ide_git_diff(path: str, staged: bool = False, trace_id=None):
    args = ["diff"]
    if staged:
        args.append("--cached")
    rc, out, err = await _git(args, path)
    return {"diff": out, "path": path, "error": err if rc != 0 else ""}



# ─────────────────────────────────────────────────────────────────────────────
# IDE SESSION GRAPH + FABRIC INTEGRATION
# Records IDE activity on the memory session graph and persists generated
# content to the data fabric. Requires memory_hooks and data_fabric modules.
# ─────────────────────────────────────────────────────────────────────────────

def _mem():
    import sys as _sys
    m = _sys.modules.get("memory")
    return (m.MEMORY, m.MemoryRecord) if m else (None, None)

def _hooks():
    import sys as _sys
    return _sys.modules.get("memory_hooks")

def _fabric():
    import sys as _sys
    return _sys.modules.get("data_fabric")

async def _get_session_root(session_id: str) -> str:
    hooks = _hooks()
    if not hooks: return session_id
    try:
        return await hooks.get_or_create_session(session_id)
    except Exception:
        return session_id

async def _store_mem_node(node_id, session_id, record_type, source_type,
                           category, tags, text, full_text,
                           importance=0.6, capability_name="", metadata=None):
    MEMORY, MemoryRecord = _mem()
    if not MEMORY or not MemoryRecord: return False
    try:
        rec = MemoryRecord(
            id=node_id, session_id=session_id, record_type=record_type,
            source_type=source_type, category=category, tags=tags,
            text=text[:500], full_text=full_text, importance=importance,
            capability=capability_name, metadata=metadata or {},
        )
        await MEMORY.store(rec)
        return True
    except Exception as e:
        log.debug("ide store_mem_node: %s", e)
        return False

async def _link_mem(from_id, to_id, rel, session_id, props=None):
    hooks = _hooks()
    if not hooks or not from_id or not to_id or from_id == to_id: return
    try:
        await hooks._link_nodes(from_id, to_id, rel, props or {}, session_id=session_id)
    except Exception as e:
        log.debug("ide link_mem %s→%s: %s", from_id[:8] if from_id else "?", to_id[:8] if to_id else "?", e)

async def _fabric_ingest(dataset_id, text, data, source_id="", tags=None):
    fabric = _fabric()
    if not fabric: return
    try:
        await fabric.ingest_dataset(
            dataset_id=dataset_id,
            data=[{"text": text[:2000], **data}],
            source="ide", source_id=source_id, tags=tags or [],
        )
    except Exception as e:
        log.debug("ide fabric_ingest %s: %s", dataset_id, e)


@capability(
    "ide.session.workspace_opened",
    http_method="POST", http_path="/ide/session/workspace", http_tags=["ide", "session"],
    memory="off",
    description="Record a workspace/folder open event on the session graph "
                "and store workspace metadata in the data fabric. "
                "Input: session_id (str!), path (str!), name (str), file_count (int). "
                "Output: {node_id, stored}.",
)
async def ide_session_workspace_opened(
    session_id: str, path: str, name: str = "", file_count: int = 0, trace_id=None,
):
    ws_name   = name or path.split("/")[-1] or path
    node_id   = str(uuid.uuid4())
    sess_root = await _get_session_root(session_id)
    ok = await _store_mem_node(
        node_id=node_id, session_id=session_id, record_type="event",
        source_type="tool", category="ide.workspace",
        tags=["ide","workspace","opened"],
        text=f"IDE workspace: {ws_name}",
        full_text=f"Workspace: {ws_name}\nPath: {path}\nFiles: {file_count}",
        importance=0.5, capability_name="ide.session.workspace_opened",
        metadata={"path": path, "name": ws_name, "file_count": file_count},
    )
    if ok:
        await _link_mem(sess_root, node_id, "CONTAINS", session_id,
                        {"type": "workspace", "path": path})
    await _fabric_ingest(
        "ide.workspaces", f"Workspace: {ws_name} at {path}",
        {"name": ws_name, "path": path, "file_count": file_count,
         "session_id": session_id, "opened_at": now_iso()},
    )
    return {"node_id": node_id, "stored": ok}


@capability(
    "ide.session.file_written",
    http_method="POST", http_path="/ide/session/file", http_tags=["ide", "session"],
    memory="off",
    description="Record a file write event on the graph and store content in the fabric. "
                "Input: session_id (str!), path (str!), content (str), "
                "agent (str), bytes_ (int). "
                "Output: {node_id, stored}.",
)
async def ide_session_file_written(
    session_id: str, path: str, content: str = "",
    agent: str = "", bytes_: int = 0, trace_id=None,
):
    node_id  = str(uuid.uuid4())
    sess_root= await _get_session_root(session_id)
    filename = path.split("/")[-1]
    size     = bytes_ or len(content.encode("utf-8", errors="replace"))
    ok = await _store_mem_node(
        node_id=node_id, session_id=session_id, record_type="observation",
        source_type="ai" if agent else "tool", category="ide.file_write",
        tags=["ide","file","generated"] + ([agent] if agent else []),
        text=f"File: {filename} ({size}b)" + (f" by {agent}" if agent else ""),
        full_text=f"Path: {path}\nAgent: {agent}\nSize: {size}\n\n{content[:800]}",
        importance=0.65, capability_name="ide.session.file_written",
        metadata={"path": path, "bytes": size, "agent": agent},
    )
    if ok:
        await _link_mem(sess_root, node_id, "GENERATES", session_id,
                        {"type": "file", "path": path, "agent": agent})
    ext  = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"
    lang = {"py":"python","js":"javascript","ts":"typescript","rs":"rust",
             "go":"go","md":"markdown","html":"html","sh":"shell"}.get(ext, ext)
    await _fabric_ingest(
        "ide.generated", f"{path}\n\n{content[:3000]}",
        {"path": path, "filename": filename, "language": lang, "bytes": size,
         "agent": agent, "session_id": session_id, "content": content[:50000]},
        source_id=session_id, tags=["ide","generated","file", lang],
    )
    return {"node_id": node_id, "stored": ok}


@capability(
    "ide.session.summary",
    http_method="GET", http_path="/ide/session/summary", http_tags=["ide", "session"],
    memory="off",
    description="Summarise all IDE events for a session from the memory graph. "
                "Input: session_id (str! — query param). "
                "Output: {session_id, node_count, workspaces, files, agent_turns}.",
)
async def ide_session_summary(session_id: str, trace_id=None):
    MEMORY, _ = _mem()
    if not MEMORY:
        return {"session_id": session_id, "error": "memory not available"}
    try:
        results = await MEMORY.search("", limit=100, filters={"session_id": session_id})
        cats = {}
        for item in (results or []):
            rec = item.get("record", item) if isinstance(item, dict) else item
            cat = getattr(rec, "category", None) or ""
            if cat.startswith("ide."):
                cats[cat] = cats.get(cat, 0) + 1
        return {
            "session_id": session_id,
            "node_count": sum(cats.values()),
            "workspaces": cats.get("ide.workspace", 0),
            "files":      cats.get("ide.file_write", 0),
            "categories": cats,
        }
    except Exception as e:
        return {"session_id": session_id, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# PANEL HTML ENDPOINT
# Uses @capability with http_method="GET" so it stays inside the capability
# system. FastAPI checks isinstance(result, Response) before JSON-serialising —
# returning an HTMLResponse from a cap function passes it through as-is.
# See: fastapi/routing.py get_request_handler() line: if isinstance(raw_response, Response)
# ─────────────────────────────────────────────────────────────────────────────

from fastapi.responses import HTMLResponse as _HTMLResponse

_IDE_PANEL_PATH = Path(__file__).parent / "ide_panel.html"


@capability(
    "ide.panel.html",
    http_method="GET", http_path="/ide/panel", http_tags=["ide", "ui"],
    memory="off", silent=True,
    description="Serve the IDE panel HTML page (text/html).",
)
async def ide_panel_html(trace_id=None):
    """Serve ide_panel.html as HTMLResponse — FastAPI passes Response objects through directly."""
    try:
        html = _IDE_PANEL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = (
            "<!DOCTYPE html><html><body style=\"background:#0d0f12;color:#ef4444;"
            "font-family:monospace;padding:40px\">"
            "<h2>ide_panel.html not found</h2>"
            f"<p>Expected path: {_IDE_PANEL_PATH}</p>"
            "<p>Ensure ide_panel.html is in the same directory as ide_capabilities.py</p>"
            "</body></html>"
        )
    return _HTMLResponse(html)

@APP.get("/ide/panel", include_in_schema=False)
async def _research_panel():
    from fastapi.responses import HTMLResponse
    p = _HERE / "ide_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>research_panel.html not found</p>")

# ─────────────────────────────────────────────────────────────────────────────
# REGISTER UI PANEL
# ─────────────────────────────────────────────────────────────────────────────

# The IDE tab now mounts the merged wrapper (vscode_capabilities.py serves
# /ide/vscode/panel): central VS Code (code-server, same-origin proxied) first,
# with the classic workbench (/ide/panel, everything below unchanged) and the
# old Remote-IDE panel as sibling views inside the same tab.
register_ui(
    "ide-panel",
    "IDE",
    "",
    """<div id="ide-panel-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/ide/vscode/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
        ui_caps=[
            "ide.vscode.instances", "ide.vscode.central.ensure",
            "ide.vscode.central.status", "ide.vscode.password.set",
            "ide.vscode.password.reveal", "ide.vscode.sandbox.workers",
            "ide.vscode.sandbox.attach", "ide.vscode.sandbox.detach",
            "ide.agent.list", "ide.agent.chat",
            "ide.instances", "ide.models", "ide.generate",
            "ide.sandbox.load", "ide.sandbox.read", "ide.sandbox.write",
            "ide.sandbox.list", "ide.sandbox.diff", "ide.sandbox.clear",
            "ide.fs.list", "ide.fs.read", "ide.fs.write", "ide.fs.delete",
            "ide.fs.exists",
            "ide.code.read_lines", "ide.code.edit_lines", "ide.code.insert_at",
            "ide.code.grep", "ide.code.replace", "ide.code.list_files",
            "ide.code.outline",
            "ide.code.tool_dispatch", "ide.code.tool_manifest",
            "ide.code.whitelist", "ide.code.whitelist_update",
            "ide.code.registry_search",
            "ide.git.status", "ide.git.commit", "ide.git.log", "ide.git.diff",
        ],
    mode="tab",
    tab_order=50,
    specialist_agent="coder",
    specialist_loop_profile="coding",
)

# ─────────────────────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────────────────────

async def _startup():
    await _ensure_ide_agents()
    log.info("ide_capabilities ready — agents: thinker / writer / analyser")


schedule(_startup, interval=999999, name="ide_startup")


# ─────────────────────────────────────────────────────────────────────────────
# RUN TERMINAL  —  /ide-api/exec/{run,stop,stdin}
# ─────────────────────────────────────────────────────────────────────────────
# The IDE panel's Run tab streams shell commands here. Every command is gated by
# the SAME exec sandbox policy that governs exec.bash.run / exec.code.run — we
# locate the loaded exec_capabilities module and reuse its _sandbox_check, so the
# <vera-sandbox-controls> editor (Exec panel OR IDE panel) controls both. SSE
# frames match what ide_panel.html's _streamSSE expects:
#     data: {"event": "pid"|"stdout"|"stderr"|"exit", "data": "..."}
# ─────────────────────────────────────────────────────────────────────────────
import sys as _sys
import time as _time

# pid -> live process, so /stop and /stdin can reach a running run.
_IDE_RUN_PROCS: "Dict[int, asyncio.subprocess.Process]" = {}


def _exec_sandbox_mod():
    """Return the loaded exec_capabilities module (owner of the single exec
    sandbox policy + _sandbox_check), or None if it hasn't been imported yet."""
    for _name, _mod in list(_sys.modules.items()):
        if _mod is not None and _name.endswith("exec_capabilities") \
                and hasattr(_mod, "_sandbox_check"):
            return _mod
    return None


def _ide_run_sse(event: str, data) -> bytes:
    return ("data: " + json.dumps({"event": event, "data": data}) + "\n\n").encode("utf-8")


@APP.post("/ide-api/exec/run", include_in_schema=False)
async def ide_exec_run(request: Request):
    """SSE-stream a shell command for the IDE Run terminal, gated by the shared
    exec sandbox policy."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    cmd = (body.get("cmd") or "").strip()
    cwd = body.get("cwd") or ""
    session_id = body.get("session_id") or _ide_get_session_id()

    def _err_stream(msg: str, code: int = 126):
        async def _g():
            yield _ide_run_sse("stderr", msg)
            yield _ide_run_sse("exit", str(code))
        return StreamingResponse(
            _g(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    if not cmd:
        return _err_stream("sandbox: empty command")

    # Opt-in per-session sandbox: when this session (e.g. a notebook cell's
    # `notebook:<id>` session, or an IDE session) has an ACTIVE sandbox, run the
    # command INSIDE the container — the same routing the exec.* streams use —
    # instead of on the host. Falls through to the host path otherwise.
    sbx_argv = None
    _sbx = _sandbox_mod()
    if _sbx is not None and hasattr(_sbx, "route_shell_argv"):
        try:
            sbx_argv = await _sbx.route_shell_argv(session_id, cmd)
        except Exception as e:
            log.debug("ide run sandbox route failed (host): %s", e)
            sbx_argv = None

    timeout = 0
    if sbx_argv is None:
        # Host path — gate through the shared exec sandbox policy (identical rules
        # to exec.* runs). The sandboxed path is isolated by the container instead.
        sb = _exec_sandbox_mod()
        if sb is not None:
            try:
                ok, reason = sb._sandbox_check(cmd, cwd=cwd)
            except Exception as e:
                ok, reason = True, ""
                log.warning("ide run sandbox check errored (allowing): %s", e)
            if not ok:
                await emit_event({"type": "exec.sandbox.blocked", "shell": "ide.run",
                                  "reason": reason, "session_id": session_id})
                return _err_stream(f"⛔ sandbox blocked: {reason}")
            try:
                cwd = sb._sandbox_effective_cwd(cwd)
                # Pass a large sentinel so the policy's max_timeout (if any) caps it;
                # an unset cap leaves it effectively uncapped.
                timeout = sb._sandbox_clamp_timeout(10 ** 9)
                if timeout >= 10 ** 9:
                    timeout = 0
            except Exception:
                timeout = 0
        else:
            log.warning("ide run: exec sandbox module not loaded — running ungated")

    run_cwd = (cwd or None) if sbx_argv is None else None

    async def _gen():
        try:
            if sbx_argv is not None:
                # docker exec … sh -lc <cmd> — streamed from inside the container.
                proc = await asyncio.create_subprocess_exec(
                    *sbx_argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.PIPE,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    cmd, cwd=run_cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.PIPE,
                )
        except Exception as e:
            yield _ide_run_sse("stderr", f"spawn failed: {e}")
            yield _ide_run_sse("exit", "-1")
            return

        pid = proc.pid
        _IDE_RUN_PROCS[pid] = proc
        yield _ide_run_sse("pid", str(pid))
        await emit_event({"type": "ide.run.started", "pid": pid, "cmd": cmd[:200],
                          "cwd": run_cwd or "", "session_id": session_id,
                          "sandboxed": sbx_argv is not None})

        q: "asyncio.Queue" = asyncio.Queue()

        async def _pump(stream, kind):
            try:
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    await q.put((kind, line.decode("utf-8", "replace").rstrip("\r\n")))
            except Exception:
                pass
            finally:
                await q.put((kind + ":eof", ""))

        t_out = asyncio.create_task(_pump(proc.stdout, "stdout"))
        t_err = asyncio.create_task(_pump(proc.stderr, "stderr"))
        t0 = _time.monotonic()
        eofs = 0
        rc = -1
        try:
            while eofs < 2:
                try:
                    kind, text = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if timeout and (_time.monotonic() - t0) > timeout:
                        try: proc.kill()
                        except Exception: pass
                        yield _ide_run_sse("stderr", f"⛔ sandbox: timeout after {timeout}s")
                        break
                    continue
                if kind.endswith(":eof"):
                    eofs += 1
                    continue
                yield _ide_run_sse(kind, text)
            rc = await proc.wait()
        except asyncio.CancelledError:
            try: proc.kill()
            except Exception: pass
            raise
        finally:
            for _t in (t_out, t_err):
                if not _t.done():
                    _t.cancel()
            _IDE_RUN_PROCS.pop(pid, None)

        yield _ide_run_sse("exit", str(rc))
        await emit_event({"type": "ide.run.exited", "pid": pid, "rc": rc,
                          "session_id": session_id})

    return StreamingResponse(
        _gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@APP.post("/ide-api/exec/stop", include_in_schema=False)
async def ide_exec_stop(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        pid = int(body.get("pid"))
    except Exception:
        return {"ok": False, "error": "pid required"}
    proc = _IDE_RUN_PROCS.get(pid)
    if not proc:
        return {"ok": False, "error": f"no running process with pid {pid}"}
    try:
        proc.kill()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "pid": pid}


@APP.post("/ide-api/exec/stdin", include_in_schema=False)
async def ide_exec_stdin(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        pid = int(body.get("pid"))
    except Exception:
        return {"ok": False, "error": "pid required"}
    data = body.get("data") or ""
    proc = _IDE_RUN_PROCS.get(pid)
    if not proc or proc.stdin is None:
        return {"ok": False, "error": f"no stdin for pid {pid}"}
    try:
        proc.stdin.write(data.encode("utf-8"))
        await proc.stdin.drain()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "pid": pid, "bytes": len(data)}


# ─────────────────────────────────────────────────────────────────────────────
# IDE DOCKER BUTTONS  —  /ide-api/docker/{build,run,stop,ping}
# ─────────────────────────────────────────────────────────────────────────────
# Thin aliases that delegate to the docker_capabilities subsystem so the IDE
# Run tab's Build / Run / Stop buttons (which post {docker_host|host, ...}) work
# and are sandbox-gated like everything else. The docker_host is either 'local'
# or an ad-hoc tcp URL — mapped to a transient host record.
# ─────────────────────────────────────────────────────────────────────────────
def _docker_mod():
    m = _sys.modules.get("docker_capabilities")
    return m if (m and hasattr(m, "_gated_stream_response")) else None


def _ide_docker_host(ref: str):
    ref = (ref or "").strip()
    if not ref or ref == "local":
        return {"id": "local", "kind": "local",
                "socket": os.getenv("DOCKER_SOCK", "/var/run/docker.sock")}
    return {"id": "adhoc", "kind": "tcp", "url": ref}


def _ide_docker_err_stream(msg: str):
    async def _g():
        yield ("data: " + json.dumps({"event": "stderr", "data": msg}) + "\n\n").encode("utf-8")
        yield ("data: " + json.dumps({"event": "exit", "data": "-1"}) + "\n\n").encode("utf-8")
    return StreamingResponse(_g(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@APP.post("/ide-api/docker/build", include_in_schema=False)
async def ide_docker_build(request: Request):
    dm = _docker_mod()
    if not dm:
        return _ide_docker_err_stream("docker subsystem not loaded")
    try: body = await request.json()
    except Exception: body = {}
    rec = _ide_docker_host(body.get("docker_host") or body.get("host") or "")
    cwd = body.get("cwd", "") or ""
    tag = body.get("tag") or "vera-app:latest"
    return await dm._gated_stream_response(rec, ["build", "-t", tag, cwd or "."], cwd=cwd)


@APP.post("/ide-api/docker/run", include_in_schema=False)
async def ide_docker_run(request: Request):
    dm = _docker_mod()
    if not dm:
        return _ide_docker_err_stream("docker subsystem not loaded")
    try: body = await request.json()
    except Exception: body = {}
    rec = _ide_docker_host(body.get("docker_host") or body.get("host") or "")
    image = (body.get("image") or "").strip()
    if not image:
        return _ide_docker_err_stream("image required")
    detach = bool(body.get("detach", False))
    args = (["run", "-d"] if detach else ["run", "--rm"]) + [image]
    return await dm._gated_stream_response(rec, args,
                                           on_first_line_as="name" if detach else "")


@APP.post("/ide-api/docker/stop", include_in_schema=False)
async def ide_docker_stop(request: Request):
    dm = _docker_mod()
    if not dm:
        return {"ok": False, "error": "docker subsystem not loaded"}
    try: body = await request.json()
    except Exception: body = {}
    rec = _ide_docker_host(body.get("docker_host") or body.get("host") or "")
    cid = body.get("container_id") or body.get("container") or ""
    if not cid:
        return {"ok": False, "error": "container_id required"}
    argv = await dm._docker_argv(rec, ["stop", cid])
    res = await dm._run_local(argv, timeout=40)
    return {"ok": res.get("ok", False), "stdout": res.get("stdout", ""),
            "stderr": res.get("stderr", "")}


@APP.post("/ide-api/docker/ping", include_in_schema=False)
async def ide_docker_ping(request: Request):
    dm = _docker_mod()
    if not dm:
        return {"ok": False, "error": "docker subsystem not loaded"}
    try: body = await request.json()
    except Exception: body = {}
    rec = _ide_docker_host(body.get("host") or body.get("docker_host") or "")
    try:
        status, payload, _ = await dm._engine_request(rec, "GET", "/version", timeout=6)
        if status != 200:
            return {"ok": False, "error": f"HTTP {status}"}
        j = json.loads(payload or b"{}")
        return {"ok": True, "version": j.get("Version", ""),
                "api_version": j.get("ApiVersion", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}