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
import subprocess
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse

from Vera.Orchestration.config import cfg
from Vera.Orchestration.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, OLLAMA_INSTANCES, OLLAMA_MODEL,
    UI_PANELS,
    capability, emit_event, now_iso, ollama_generate, pick_instance,
    record_stream_activity,
    register_ui, schedule,
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

def _pick_ide_instance(agent_role: str) -> Optional[str]:
    """
    Try to pick a suitable Ollama instance for the given IDE agent role.
    Thinker → GPU preferred; Writer / Analyser → CPU preferred but any will do.
    Falls back to any online instance.
    """
    role_lower = agent_role.lower()
    prefer_gpu = role_lower == "thinker"

    # First try to match by tier label in instance label field
    for iid, inst in OLLAMA_INSTANCES.items():
        if inst.get("status") != "online":
            continue
        lbl = inst.get("label", "").lower()
        if role_lower in lbl:
            return iid

    return pick_instance(prefer_gpu=prefer_gpu)


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

    # Fallback — use the preset config to call ollama directly
    preset = _AGENT_PRESETS.get(agent_name, {})
    iid = _pick_ide_instance(agent_name)
    full_system = (preset.get("system_prompt", "") + "\n\n" + system).strip() if system else preset.get("system_prompt", "")
    return await ollama_generate(
        prompt,
        system=full_system,
        model=model or preset.get("model") or OLLAMA_MODEL,
        instance_id=iid,
        prefer_gpu=preset.get("prefer_gpu", False),
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
    from Vera.Orchestration.capability_orchestration import (
        emit_event, _ollama_log_append, _ollama_caller_info,
    )
    _req_id = str(uuid.uuid4())[:12]
    _t0 = _time.time()
    _prompt_preview = (prompt or "")[:120].replace("\n", " ")
    log.info("ollama_req [%s] model=%s inst=%s caller=ide_capabilities:ide_generate agent=%s prompt=%s",
             _req_id, mdl, chosen, agent_name, _prompt_preview)
    try:
        await emit_event({
            "type": "ollama.request", "req_id": _req_id,
            "model": mdl, "instance_id": chosen, "instance_url": url,
            "caller_file": "ide_capabilities.py", "caller_func": "ide_generate",
            "caller_module": "ide_capabilities", "cap_name": "ide.generate",
            "prompt_preview": _prompt_preview, "json_mode": False,
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
        _elapsed = round(_time.time() - _t0, 2)
        log.error("ollama_generate [%s] FAILED after %.2fs inst=%s caller=ide_capabilities:ide_generate err=%s",
                  _req_id, _elapsed, chosen, e)
        _ollama_log_append({
            "req_id": _req_id, "model": mdl, "instance": chosen,
            "caller_file": "ide_capabilities.py", "caller_func": "ide_generate",
            "prompt_preview": _prompt_preview, "ts": now_iso(),
            "status": "error", "elapsed_s": _elapsed, "error": str(e)[:200],
        })
        try:
            await emit_event({
                "type": "ollama.request_error", "req_id": _req_id,
                "model": mdl, "instance_id": chosen,
                "caller_file": "ide_capabilities.py", "caller_func": "ide_generate",
                "elapsed_s": _elapsed, "error": str(e)[:200],
            })
        except Exception:
            pass
        return {"error": str(e), "text": "", "agent": agent_name}
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
        from Vera.Orchestration.capability_orchestration import (
            emit_event as _emit_event, _ollama_log_append,
        )
        _req_id = str(uuid.uuid4())[:12]
        _t0_stream = _time.monotonic()
        _prompt_preview = (prompt or "")[:120].replace("\n", " ")
        log.info("ollama_req [%s] model=%s inst=%s caller=ide_capabilities:ide_stream agent=%s prompt=%s",
                 _req_id, model, chosen, agent_name_short, _prompt_preview)
        try:
            await _emit_event({
                "type": "ollama.request", "req_id": _req_id,
                "model": model, "instance_id": chosen, "instance_url": url,
                "caller_file": "ide_capabilities.py", "caller_func": "ide_stream_endpoint",
                "caller_module": "ide_capabilities", "cap_name": "ide.stream",
                "prompt_preview": _prompt_preview, "json_mode": False,
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
            error_text = str(e)
            yield f"data: {json.dumps({'type':'error','text':error_text})}\n\n".encode()
            return
        finally:
            inst["in_use"] = max(0, inst.get("in_use", 1) - 1)

        full_text = "".join(full)
        # ── Log completion of the Ollama request ────────────────────────────
        _elapsed_s = round((_time.monotonic() - _t0_stream), 2)
        if error_text:
            log.error("ollama_generate [%s] FAILED after %.2fs caller=ide_capabilities:ide_stream err=%s",
                      _req_id, _elapsed_s, error_text[:120])
            _ollama_log_append({
                "req_id": _req_id, "model": model, "instance": chosen,
                "caller_file": "ide_capabilities.py", "caller_func": "ide_stream_endpoint",
                "prompt_preview": _prompt_preview, "ts": now_iso(),
                "status": "error", "elapsed_s": _elapsed_s, "error": error_text[:200],
            })
            try:
                await _emit_event({
                    "type": "ollama.request_error", "req_id": _req_id,
                    "model": model, "instance_id": chosen,
                    "caller_file": "ide_capabilities.py", "caller_func": "ide_stream_endpoint",
                    "elapsed_s": _elapsed_s, "error": error_text[:200],
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
async def ide_fs_read(path: str, max_bytes: int = 1_048_576, trace_id=None):
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


@capability(
    "ide.fs.write",
    http_method="POST", http_path="/ide/fs/write", http_tags=["ide", "fs"],
    memory="off",
    description="Write content to a real filesystem file (creates parent dirs). "
                "Input: path (str!), content (str!), agent (str), session_id (str). "
                "Output: {path, bytes, created}.",
)
async def ide_fs_write(path: str, content: str, agent: str = "", session_id: str = "", trace_id=None):
    try:
        p = Path(path)
        created = not p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        size = len(content.encode("utf-8", errors="replace"))
        sid = session_id or _ide_session_id()
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
async def ide_fs_delete(path: str, trace_id=None):
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
async def ide_fs_browse(path: str = "", trace_id=None):
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
async def ide_fs_list_v2(path: str = "", recursive: bool = False, trace_id=None):
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
    return rec
# ─────────────────────────────────────────────────────────────────────────────

def _git(args: list, cwd: str) -> tuple[int, str, str]:
    """Run a git command; returns (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            ["git"] + args, cwd=cwd,
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", str(e)


@capability(
    "ide.git.status",
    http_method="POST", http_path="/ide/git/status", http_tags=["ide", "git"],
    memory="off",
    description="Get git status for a repository path. "
                "Input: path (str!). Output: {path, status, branch, staged, unstaged, untracked}.",
)
async def ide_git_status(path: str, trace_id=None):
    rc, out, err = _git(["status", "--porcelain", "-b"], path)
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
        rc, out, err = _git(["add", "-A"], path)
        if rc != 0:
            return {"success": False, "output": err or out}
    rc, out, err = _git(["commit", "-m", message], path)
    await emit_event({"type": "ide.git.commit", "path": path, "message": message})
    return {"success": rc == 0, "output": (out + err).strip()}


@capability(
    "ide.git.log",
    http_method="POST", http_path="/ide/git/log", http_tags=["ide", "git"],
    memory="off",
    description="Get git log for a repository. "
                "Input: path (str!), n (int, default 20). "
                "Output: {commits: [{hash, author, date, message}]}.",
)
async def ide_git_log(path: str, n: int = 20, trace_id=None):
    rc, out, err = _git(
        ["log", f"-{n}", "--pretty=format:%H\x1f%an\x1f%ad\x1f%s", "--date=short"],
        path,
    )
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            commits.append({"hash": parts[0][:8], "author": parts[1],
                            "date": parts[2], "message": parts[3]})
    return {"commits": commits, "path": path}


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
    rc, out, err = _git(args, path)
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

register_ui(
    "ide-panel",
    "IDE",
    "",
    """<div id="ide-panel-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/ide/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
        ui_caps=[
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
)

# ─────────────────────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────────────────────

async def _startup():
    await _ensure_ide_agents()
    log.info("ide_capabilities ready — agents: thinker / writer / analyser")


schedule(_startup, interval=999999, name="ide_startup")