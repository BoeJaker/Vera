"""
vera_memory_hooks.py  —  Automatic Memory Integration
======================================================

This module wires the memory system into capability calls and agent turns,
creating a rich graph of linked interactions stored across all memory backends.

Architecture
────────────

Session graph model
───────────────────
Each session produces a connected subgraph:

  [session] ──CONTAINS──► [user_message]
                               │ TRIGGERS
                               ▼
                          [cap_call]  ──CAUSES──► [cap_output]
                               │ FOLLOWS
                               ▼
                          [agent_response]
                               │ FOLLOWS
                               ▼
                          [user_message]  ...

All nodes: MemoryRecord with full metadata.
Edges:     Neo4j FOLLOWS / CAUSES / CONTAINS / RESPONDS_TO relationships.

Memory routing — three modes per capability
──────────────────────────────────────────
  "auto"   Default. Records interactions for caps whose group matches
           MEMORY_AUTO_GROUPS and whose output contains substantial text.
  "on"     Always record regardless of content.
  "off"    Never record (observability caps, health checks, etc.)

The mode is set in the @capability decorator:
  @capability("llm.generate", memory="on", ...)
  @capability("obs.health",   memory="off", ...)

Memory injection for agents
───────────────────────────
When agent.memory_inject=True, the agent's system prompt is augmented with
retrieved memories relevant to the current message before calling the LLM.
These are fetched from memory.recall with the agent's session_id + tags.

Capabilities added
──────────────────
  memory.session_init      — create/resume a session node
  memory.record_turn       — store a human+AI turn pair with graph links
  memory.cap_record        — store a capability interaction
  memory.agent_context     — retrieve memories for agent context injection
  memory.session_graph     — get the full graph for a session
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import (
    CAPABILITY_REGISTRY, capability, emit_event, now_iso,
)

log = logging.getLogger("vera.memory_hooks")

# ── Groups whose cap outputs are auto-recorded ────────────────────────────────
MEMORY_AUTO_GROUPS = {
    "llm", "agent", "dag", "http", "memory", "skills", "ontologies",
    "data", "pipeline", "text",
}
# Groups explicitly excluded even in auto mode (never noisy infra calls)
MEMORY_EXCLUDE_GROUPS = {
    "obs", "system", "ollama", "syslog", "caps", "health",
}

# ── Minimum text length to bother storing ────────────────────────────────────
MEMORY_MIN_TEXT_LEN = 30


def _memory():
    """Lazy import to avoid circular dependency at load time."""
    vera_memory = sys.modules.get("Vera.Orchestration.fabric.memory")
    if vera_memory is None:
        return None, None
    return vera_memory.MEMORY, vera_memory.MemoryRecord


def _redis():
    return _orch.REDIS


# ─────────────────────────────────────────────────────────────────────────────
# SESSION MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

# In-process session registry: session_id → {node_id, agent_name, last_node_id}
_SESSIONS: Dict[str, Dict] = {}
# Last human/AI message node per session — used for causal linking of cap calls
_LAST_MSG: Dict[str, str] = {}


async def get_or_create_session(session_id: str, agent_name: str = "",
                                 metadata: Optional[Dict] = None) -> str:
    """
    Ensure the canonical Neo4j :Session node exists for `session_id`.

    Note: prior versions of this function ALSO created a proxy
    `:Memory{category:'session'}` node and returned its uuid as the "session
    root" so callers could draw SESSION_CONTENT edges from it. That created
    a duplicate parent for every session — once as `:Session` (auto-created
    by the memory.py Neo4j store on every record) and once as the proxy
    Memory node. The activity_worker now relies on the auto-created
    `(:Session)-[:CONTAINS]->(:Memory)` edge instead, so the proxy is gone.

    Returns `session_id` itself — callers that previously used the return
    value as a node uuid for linking should stop linking from it (the
    auto-CONTAINS edge already covers that relationship).
    """
    if session_id in _SESSIONS:
        return session_id

    MEMORY, _MR = _memory()
    if not MEMORY:
        _SESSIONS[session_id] = {"node_id": session_id, "last_node_id": session_id,
                                  "agent_name": agent_name}
        return session_id

    # Pre-create the :Session node so it exists even before the first cap
    # call writes a Memory record. Neo4j's MERGE is idempotent — running
    # this on every chat/IDE/exec start is safe and cheap.
    neo = MEMORY._backends.get("neo4j")
    if neo and neo._driver:
        try:
            async with neo._driver.session() as s:
                await s.run(
                    "MERGE (sess:Session {session_id: $sid}) "
                    "ON CREATE SET sess.created_at = $ts, sess.agent_name = $agent "
                    "ON MATCH SET sess.agent_name = coalesce(sess.agent_name, $agent)",
                    sid=session_id,
                    ts=__import__("datetime").datetime.utcnow().isoformat(),
                    agent=agent_name or "",
                )
        except Exception as e:
            log.debug("get_or_create_session :Session merge: %s", e)

    _SESSIONS[session_id] = {
        "node_id":      session_id,
        "last_node_id": session_id,
        "agent_name":   agent_name,
    }
    return session_id


async def _link_nodes(from_id: str, to_id: str, rel_type: str,
                      props: Optional[Dict] = None,
                      session_id: str = ""):
    """Create a relationship between two memory nodes and emit a WS edge event."""
    MEMORY, _ = _memory()
    if not MEMORY or not from_id or not to_id or from_id == to_id:
        log.debug("_link_nodes skipped: MEMORY=%s from=%s to=%s", bool(MEMORY), from_id[:8] if from_id else None, to_id[:8] if to_id else None)
        return
    full_props = dict(props or {})
    if session_id:
        full_props["session_id"] = session_id
    try:
        result = await MEMORY.relate(from_id, to_id, rel_type, full_props)
        log.info("edge %s -[%s]-> %s  backends=%s", from_id[:8], rel_type, to_id[:8], result)
        await emit_event({
            "type":       "memory.edge",
            "from_id":    from_id,
            "to_id":      to_id,
            "relation":   rel_type,
            "session_id": session_id,
        })
    except Exception as e:
        log.error("memory_hooks relate %s→%s [%s]: %s", from_id[:8], to_id[:8], rel_type, e)


async def _update_session_last(session_id: str, node_id: str):
    """Update the session's last node pointer."""
    if session_id in _SESSIONS:
        _SESSIONS[session_id]["last_node_id"] = node_id


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITY MEMORY HOOK
# ─────────────────────────────────────────────────────────────────────────────

def _should_record_cap(cap_name: str, memory_mode: str,
                        result: Any) -> bool:
    """Decide whether to store a capability interaction in memory."""
    if memory_mode == "off":
        return False
    if memory_mode == "on":
        return True
    # auto mode
    group = cap_name.split(".")[0]
    if group in MEMORY_EXCLUDE_GROUPS:
        return False
    if group not in MEMORY_AUTO_GROUPS:
        return False
    # Only store if there's substantive text content
    text = _extract_text(result)
    return len(text) >= MEMORY_MIN_TEXT_LEN


def _extract_text(result: Any) -> str:
    """Pull the most meaningful text from a capability result."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("text", "response", "summary", "content", "answer",
                    "output", "result", "image_b64"):
            v = result.get(key)
            if v and isinstance(v, str) and key != "image_b64":
                return v
        # Fall back to JSON rep, truncated
        return json.dumps(result)[:200]
    return str(result)[:200]


def _extract_keywords(text: str, cap_name: str) -> List[str]:
    """Simple keyword extraction — cap group + significant words."""
    words = set()
    words.add(cap_name.split(".")[0])
    words.add(cap_name.replace(".", "_"))
    # Add words >4 chars that aren't common stopwords
    stops = {"that","this","with","from","have","will","been","were",
              "they","their","there","what","when","where","which"}
    for w in text.lower().split():
        w = w.strip(".,;:!?()[]\"'")
        if len(w) > 4 and w not in stops:
            words.add(w)
            if len(words) > 12:
                break
    return list(words)[:10]


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITY DECORATOR PATCH
# ─────────────────────────────────────────────────────────────────────────────

def _get_cap_memory_mode(cap_name: str) -> str:
    """Read the memory mode stored in the cap registry entry."""
    cap = CAPABILITY_REGISTRY.get(cap_name, {})
    return cap.get("memory", "auto")


async def record_cap_interaction(*args, **kwargs):
    """
    DEPRECATED — kept as a no-op for backwards compatibility.

    Capability activity recording is now handled by a single unified path:
    capability_orchestration._act_enqueue → _activity_worker. That worker
    writes the same call_node + output_node linked structure this function
    used to write, and it works for every cap with memory != "off".

    Calling this function is harmless but does nothing. The legacy import
    path exists so external modules that were calling this directly don't
    crash on upgrade.
    """
    return None


def patch_capability_for_memory():
    """
    DEPRECATED — kept as a no-op.

    Activity recording now lives in capability_orchestration: every cap that
    goes through the @capability decorator is recorded by _act_enqueue inside
    the wrapper itself. Wrapping caps a second time here would re-introduce
    the double-recording bug this rewrite fixes.
    """
    log.info("memory_hooks: patch_capability_for_memory is now a no-op "
             "(activity recording moved to capability_orchestration)")


def patch_new_cap(cap_name: str):
    """DEPRECATED — kept as a no-op (see patch_capability_for_memory)."""
    return None
    log.debug("memory_hooks: patched new cap %s", cap_name)


# ─────────────────────────────────────────────────────────────────────────────
# AGENT TURN RECORDING
# ─────────────────────────────────────────────────────────────────────────────

async def record_agent_turn(
    session_id:  str,
    agent_name:  str,
    agent_id:    str,
    human_text:  str,
    ai_text:     str,
    thinking:    str    = "",
    model:       str    = "",
    trace_id:    str    = "",
    latency_ms:  int    = 0,
    tags:        List[str] = None,
):
    """
    Store a human→agent turn as two linked MemoryRecord nodes:
      [human_msg] ──TRIGGERS──► [ai_response]
      [session_root or prev] ──FOLLOWED_BY──► [human_msg]

    This is called by AgentRunner after each turn when memory_enabled=True.
    """
    MEMORY, MemoryRecord = _memory()
    if not MEMORY or not MemoryRecord:
        return

    base_tags = (tags or []) + ["agent_turn", agent_name]
    keywords  = _extract_keywords(human_text + " " + ai_text, f"agent.{agent_name}")

    # Ensure session node exists
    await get_or_create_session(session_id, agent_name)

    # Human message node
    human_id  = str(uuid.uuid4())
    human_rec = MemoryRecord(
        id          = human_id,
        session_id  = session_id,
        trace_id    = trace_id,
        record_type = "message",
        source_type = "human",
        category    = "chat",
        tags        = base_tags + ["human"],
        keywords    = keywords,
        text        = human_text[:500],
        full_text   = human_text,
        human_text  = True,
        ai_output   = False,
        importance  = 0.65,
        capability  = "agent.chat",
        model       = model,
        metadata    = {"agent_name": agent_name, "agent_id": agent_id, "role": "human"},
    )
    await MEMORY.store(human_rec)
    # Link session root → human message
    sess_root = _SESSIONS.get(session_id, {}).get("node_id", "")
    if sess_root and sess_root != human_id:
        await _link_nodes(sess_root, human_id, "SESSION_CONTENT",
                          {"agent": agent_name, "role": "human"}, session_id=session_id)

    # AI response node
    ai_id  = str(uuid.uuid4())
    ai_rec = MemoryRecord(
        id          = ai_id,
        session_id  = session_id,
        trace_id    = trace_id,
        record_type = "message",
        source_type = "ai",
        category    = "chat",
        tags        = base_tags + ["ai", "agent_response"],
        keywords    = keywords,
        text        = ai_text[:500],
        full_text   = ai_text,
        human_text  = False,
        ai_output   = True,
        importance  = 0.7,
        capability  = "agent.chat",
        model       = model,
        metadata    = {
            "agent_id":   agent_id,
            "agent_name": agent_name,
            "latency_ms": latency_ms,
            "thinking":   thinking[:500] if thinking else "",
            "role":       "ai",
        },
    )
    await MEMORY.store(ai_rec)
    if sess_root and sess_root != ai_id:
        await _link_nodes(sess_root, ai_id, "SESSION_CONTENT",
                          {"agent": agent_name, "role": "ai"}, session_id=session_id)
    # Track last message for causal linking of subsequent cap calls
    _LAST_MSG[session_id] = ai_id

    # Link: human → ai (TRIGGERS / RESPONDS_TO)
    await _link_nodes(human_id, ai_id, "RESPONDS_TO",
                      {"agent": agent_name, "model": model}, session_id=session_id)

    # Link session chain: prev → human
    sess = _SESSIONS.get(session_id, {})
    prev = sess.get("last_node_id", "")
    if prev and prev != human_id:
        await _link_nodes(prev, human_id, "FOLLOWED_BY",
                           {"agent": agent_name, "ts": now_iso(), "turn": "human"}, session_id=session_id)
    # Link human → ai explicitly (TRIGGERS already done below, but also FOLLOWED_BY for chain)
    # This gives the conversation a clear sequential chain in the graph

    # Cross-link chat chain ↔ cap chain.
    # There are two parallel FOLLOWS_ACTIVITY chains in the graph:
    #   • The message chain: human_msg → RESPONDS_TO → ai_msg → FOLLOWED_BY → …
    #   • The cap call chain: cap_a → FOLLOWS_ACTIVITY → cap_b → …
    # We bridge them here so the graph shows which caps were invoked
    # during this turn (USED_CAPS) and which cap calls this turn triggered
    # (TRIGGERED_BY_TURN) by reading _ACT_SESSION_CURSOR from the
    # capability orchestrator.
    try:
        _orch = (sys.modules.get("Vera.Orchestration.capability_orchestration")
                 or sys.modules.get("capability_orchestration"))
        if _orch and hasattr(_orch, "_ACT_SESSION_CURSOR"):
            latest_cap_node = _orch._ACT_SESSION_CURSOR.get(session_id, "")
            if latest_cap_node and latest_cap_node != ai_id:
                # ai_msg → USED_CAPS → latest_cap_node
                await _link_nodes(ai_id, latest_cap_node, "USED_CAPS",
                                  {"agent": agent_name, "ts": now_iso()},
                                  session_id=session_id)
    except Exception as _e:
        log.debug("record_agent_turn cross-link: %s", _e)

    await _update_session_last(session_id, ai_id)
    log.debug("memory_hooks: agent turn %s→ai saved (%s/%s)",
              agent_name, human_id[:8], ai_id[:8])



async def record_dag_execution(
    session_id:   str,
    dag:          list,
    state:        dict,
    result:       dict    = None,
    agent_name:   str     = "",
    trigger:      str     = "chat",
    aborted_at:   int     = None,
    trigger_text: str     = "",   # human message that triggered this DAG
):
    """Store a DAG execution as a graph: dag_root → step_0 → step_1 → …"""
    MEMORY, MemoryRecord = _memory()
    if not MEMORY or not MemoryRecord: return
    if not dag: return

    await get_or_create_session(session_id, agent_name)
    sess_root = _SESSIONS.get(session_id, {}).get("node_id", "")
    last_msg  = _LAST_MSG.get(session_id, "")

    # Build capability sequence
    cap_seq = []
    for node in dag:
        if isinstance(node, list) and node and isinstance(node[0], list):
            cap_seq.append("[parallel:" + ",".join(n[0] for n in node if n) + "]")
        elif isinstance(node, list) and node:
            cap_seq.append(node[0])
        else:
            cap_seq.append(str(node))

    status_txt = "aborted" if aborted_at is not None else "complete"
    n_steps    = len(dag)
    dag_id     = str(uuid.uuid4())
    result_keys = list((result or state or {}).keys())

    dag_full = (
        "Trigger: " + trigger + "\n"
        "Steps: " + str(n_steps) + "\n"
        "Status: " + status_txt + "\n"
        "Capabilities: " + ", ".join(cap_seq) + "\n"
        "State keys: " + ", ".join(result_keys)
    )
    dag_rec = MemoryRecord(
        id          = dag_id,
        session_id  = session_id,
        record_type = "dag",
        source_type = "tool",
        category    = "dag_execution",
        tags        = ["dag", trigger, status_txt, agent_name] + cap_seq[:5],
        keywords    = cap_seq[:10],
        text        = "DAG(" + trigger + "): " + " → ".join(cap_seq[:6]) + ("…" if n_steps > 6 else ""),
        full_text   = dag_full,
        importance  = 0.75,
        capability  = "dag.execute",
        metadata    = {
            "trigger": trigger, "steps": n_steps, "status": status_txt,
            "aborted_at": aborted_at, "agent_name": agent_name,
            "state_keys": result_keys[:20],
        },
    )
    await MEMORY.store(dag_rec)

    if sess_root:
        await _link_nodes(sess_root, dag_id, "SESSION_CONTENT",
                          {"trigger": trigger, "type": "dag"}, session_id=session_id)
    # Link the human trigger message to the dag_root via CAUSES edge.
    # If _LAST_MSG exists (previous AI turn), use that as the causal node.
    # If trigger_text is provided and _LAST_MSG is empty (first turn),
    # create a lightweight human message node so the DAG is causally anchored.
    if last_msg and last_msg != dag_id:
        await _link_nodes(last_msg, dag_id, "CAUSES",
                          {"reason": trigger}, session_id=session_id)
    elif trigger_text and not last_msg:
        # First turn — no prior AI message. Store the human message that
        # triggered this DAG so it's visible as a CAUSES node in the graph.
        MEMORY, MemoryRecord = _memory()
        if MEMORY and MemoryRecord:
            try:
                trigger_id  = str(uuid.uuid4())
                trigger_rec = MemoryRecord(
                    id          = trigger_id,
                    session_id  = session_id,
                    record_type = "message",
                    source_type = "human",
                    category    = "chat",
                    tags        = ["human", "dag_trigger", agent_name],
                    keywords    = _extract_keywords(trigger_text),
                    text        = trigger_text[:500],
                    full_text   = trigger_text,
                    human_text  = True,
                    ai_output   = False,
                    importance  = 0.7,
                    capability  = "agent.chat",
                    metadata    = {"agent_name": agent_name, "role": "human",
                                   "dag_trigger": True},
                )
                await MEMORY.store(trigger_rec)
                if sess_root:
                    await _link_nodes(sess_root, trigger_id, "SESSION_CONTENT",
                                      {"role": "human"}, session_id=session_id)
                await _link_nodes(trigger_id, dag_id, "CAUSES",
                                  {"reason": trigger}, session_id=session_id)
                _LAST_MSG[session_id] = trigger_id   # so subsequent links work
            except Exception as _te:
                log.debug("dag trigger node: %s", _te)

    prev_id = dag_id
    for i, node in enumerate(dag):
        is_parallel = isinstance(node, list) and node and isinstance(node[0], list)
        cap_name = "[parallel]" if is_parallel else (node[0] if isinstance(node, list) and node else str(node))
        out_key  = "" if is_parallel else (node[1] if isinstance(node, list) and len(node) > 1 else "")
        aborted_here   = aborted_at is not None and i >= aborted_at
        step_status    = "aborted" if aborted_here else "complete"
        result_val     = (result or {}).get(out_key, "") if out_key else ""
        result_preview = str(result_val)[:200] if result_val else ""

        step_id  = str(uuid.uuid4())
        step_rec = MemoryRecord(
            id          = step_id,
            session_id  = session_id,
            record_type = "dag",
            source_type = "tool",
            category    = "dag_step",
            tags        = ["dag_step", cap_name, step_status, "step_" + str(i)],
            keywords    = [k for k in [cap_name, out_key, agent_name] if k],
            text        = "Step " + str(i+1) + ": " + cap_name + (" → " + out_key if out_key else "") + " (" + step_status + ")",
            full_text   = "Cap: " + cap_name + "\nOut: " + out_key + "\nStatus: " + step_status + "\nResult: " + result_preview,
            importance  = 0.55,
            capability  = cap_name,
            metadata    = {
                "step": i, "cap": cap_name, "out_key": out_key,
                "status": step_status, "result_preview": result_preview,
                "parallel": is_parallel,
            },
        )
        await MEMORY.store(step_rec)
        rel = "STARTS" if prev_id == dag_id else "THEN"
        await _link_nodes(prev_id, step_id, rel,
                          {"step": i, "cap": cap_name}, session_id=session_id)
        prev_id = step_id

    await _update_session_last(session_id, dag_id)
    log.debug("memory_hooks: DAG execution recorded — %d steps session=%s", n_steps, session_id)

# ─────────────────────────────────────────────────────────────────────────────
# MEMORY CONTEXT INJECTION
# ─────────────────────────────────────────────────────────────────────────────

async def get_agent_memory_context(
    session_id:  str,
    query:       str,
    agent_name:  str  = "",
    limit:       int  = 5,
    tags:        Optional[List[str]] = None,
) -> str:
    """
    Retrieve relevant past memories and format them for injection into
    an agent's system prompt.

    Returns a formatted string like:
      === Relevant memories ===
      [2025-01-01 12:00] You asked: "How do I X?"
      [2025-01-01 12:01] You responded: "To do X, you..."
      ...
    """
    MEMORY, _ = _memory()
    if not MEMORY:
        return ""

    filters: Dict = {}
    if agent_name:
        filters["tags"] = [agent_name]
    elif tags:
        filters["tags"] = tags

    try:
        results = await MEMORY.search(
            query   = query,
            limit   = limit,
            filters = filters if filters else None,
        )
    except Exception as e:
        log.debug("get_agent_memory_context: %s", e)
        return ""

    if not results:
        return ""

    lines = ["=== Relevant memories from past conversations ==="]
    for item in results:
        # HybridMemory.search returns List[Dict] with {"record": ..., "score": ...}
        # but individual backends return List[Tuple[MemoryRecord, float]].
        # Handle both shapes defensively.
        if isinstance(item, dict):
            rec_data = item.get("record") or item
            score = item.get("score", 0.0)
            # rec_data may be a dict (from to_dict()) or a MemoryRecord
            if isinstance(rec_data, dict):
                created_at = rec_data.get("created_at") or ""
                human_text = rec_data.get("human_text", True)
                text       = (rec_data.get("text") or "")[:200]
            else:
                created_at = getattr(rec_data, "created_at", "") or ""
                human_text = getattr(rec_data, "human_text", True)
                text       = (getattr(rec_data, "text", "") or "")[:200]
        else:
            # Tuple (MemoryRecord, float)
            try:
                rec, score = item
            except (TypeError, ValueError):
                continue
            created_at = getattr(rec, "created_at", "") or ""
            human_text = getattr(rec, "human_text", True)
            text       = (getattr(rec, "text", "") or "")[:200]

        ts = created_at[:16].replace("T", " ") if created_at else ""
        role = "User" if human_text else "Assistant"
        if text:
            lines.append(f"[{ts}] {role}: {text}")

    return "\n".join(lines) if len(lines) > 1 else ""


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "memory.session_init",
    http_method="POST", http_path="/memory/session/init", http_tags=["memory"],
    memory="off",
    description="Create or resume a memory session node. Returns the session root node id.",
)
async def memory_session_init(
    session_id:  str,
    agent_name:  str  = "",
    trace_id=None,
):
    node_id = await get_or_create_session(session_id, agent_name)
    return {"session_id": session_id, "node_id": node_id}


@capability(
    "memory.record_turn",
    http_method="POST", http_path="/memory/record/turn", http_tags=["memory"],
    memory="off",
    description="Store a human→agent conversation turn as linked graph nodes.",
)
async def memory_record_turn(
    session_id:  str,
    agent_name:  str  = "",
    agent_id:    str  = "",
    human_text:  str  = "",
    ai_text:     str  = "",
    thinking:    str  = "",
    model:       str  = "",
    latency_ms:  int  = 0,
    tags:        str  = "",   # comma-separated
    trace_id=None,
):
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    await record_agent_turn(
        session_id  = session_id,
        agent_name  = agent_name,
        agent_id    = agent_id,
        human_text  = human_text,
        ai_text     = ai_text,
        thinking    = thinking,
        model       = model,
        trace_id    = trace_id or "",
        latency_ms  = latency_ms,
        tags        = tag_list,
    )
    return {"status": "stored", "session_id": session_id}


@capability(
    "memory.agent_context",
    http_method="POST", http_path="/memory/agent/context", http_tags=["memory"],
    memory="off",
    description="Retrieve relevant past memories for injection into an agent's context.",
)
async def memory_agent_context(
    session_id:  str,
    query:       str,
    agent_name:  str  = "",
    limit:       int  = 5,
    tags:        str  = "",
    trace_id=None,
):
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    context = await get_agent_memory_context(
        session_id  = session_id,
        query       = query,
        agent_name  = agent_name,
        limit       = limit,
        tags        = tag_list,
    )
    return {"context": context, "found": bool(context)}


@capability(
    "memory.all_nodes",
    http_method="GET", http_path="/memory/all/nodes", http_tags=["memory"],
    memory="off",
    description="Get all Memory nodes from Neo4j (no session filter).",
)
async def memory_all_nodes(limit: int = 300, trace_id=None):
    MEMORY, _ = _memory()
    if not MEMORY:
        return {"nodes": [], "error": "memory module not loaded"}
    nodes = await MEMORY.get_all_nodes(limit=limit)
    return {"nodes": nodes, "count": len(nodes)}

@capability(
    "memory.all_edges",
    http_method="GET", http_path="/memory/all/edges", http_tags=["memory"],
    memory="off",
    description="Get all edges from Neo4j, including Session->Memory edges.",
)
async def memory_all_edges(limit: int = 1000, trace_id=None):
    MEMORY, _ = _memory()
    if not MEMORY:
        return {"edges": [], "error": "memory module not loaded"}
    edges = await MEMORY.get_all_edges(limit=limit)
    # Also fetch non-Memory→Memory edges (e.g. Session→Memory)
    neo = MEMORY._backends.get("neo4j")
    if neo and neo._driver:
        try:
            async with neo._driver.session() as s:
                r1 = await s.run(
                    "MATCH (a)-[r]->(b:Memory) WHERE NOT a:Memory "
                    "RETURN a.id AS from_id, b.id AS to_id, type(r) AS rel_type "
                    "LIMIT " + str(int(limit))
                )
                extra = await r1.data()
                seen = {(e["from_id"], e["to_id"]) for e in edges}
                for row in extra:
                    if row.get("from_id") and row.get("to_id"):
                        key = (row["from_id"], row["to_id"])
                        if key not in seen:
                            edges.append({"from_id": row["from_id"],
                                          "to_id":   row["to_id"],
                                          "relation": row["rel_type"]})
                            seen.add(key)
        except Exception:
            pass
    return {"edges": edges, "count": len(edges)}


@capability(
    "memory.graph_full",
    http_method="GET", http_path="/memory/graph/full", http_tags=["memory"],
    memory="off",
    description="Return nodes + edges in one call. Use mode=session (+session_id), mode=recent, or mode=all.",
)
async def memory_graph_full(session_id:  str = "",
                             limit_nodes: int = 300,
                             limit_edges: int = 1000,
                             mode:        str = "",
                             recent_hours: float = 6.0,
                             before:      str = "",
                             trace_id=None):
    """
    Return the memory graph in a single call for the frontend canvas.

    Modes
    -----
    * session  — one session_id only (requires `session_id`)
    * recent   — the most recent N records across all sessions, last
                 `recent_hours` hours (default 6h). Useful for "what's
                 been happening lately" without committing to the
                 expense of an all-history load.
    * all      — paginated time-window. Returns the most recent
                 `limit_nodes` records older than the optional ISO
                 timestamp `before`. The frontend uses this for
                 "load older" pagination when scrolling backwards
                 through history.

    Pagination
    ----------
    The frontend supplies `before=<oldest_visible_created_at>` to walk
    backwards through history one window at a time. Each response
    includes the oldest `created_at` it returned so the next call can
    use that as the new `before`. When the response is shorter than
    `limit_nodes` the caller has reached the start of history.

    For backwards compatibility, if `mode` is empty and `session_id` is
    provided → session mode; otherwise → all mode.

    Fetches three queries and merges the results:
      1. Memory nodes   — the bulk of the data
      2. Session nodes  — otherwise only referenced by edges, would appear
                          as orphan edge endpoints in the UI
      3. Edges          — Memory→Memory plus Session→Memory (CONTAINS)

    Session nodes are shaped to match the Memory-node field layout so the
    frontend can render them without any special casing.
    """
    MEMORY, _ = _memory()
    if not MEMORY:
        return {"nodes": [], "edges": [], "error": "memory module not loaded"}
    neo = MEMORY._backends.get("neo4j")
    if not neo or not neo._driver:
        return {"nodes": [], "edges": [], "error": "neo4j not connected"}

    # Resolve mode. Empty `mode` falls back to session-if-id-given, else all.
    effective_mode = (mode or "").strip().lower()
    if not effective_mode:
        effective_mode = "session" if session_id else "all"
    if effective_mode not in ("session", "recent", "all"):
        return {"nodes": [], "edges": [], "error": f"unknown mode: {mode!r}"}

    # Compute an ISO-8601 cutoff for recent-mode. Neo4j stores created_at
    # as a string — we compare lexicographically, which works because the
    # timestamps are ISO 8601 with fixed width.
    from datetime import datetime, timedelta, timezone
    try:
        hours = max(0.1, float(recent_hours))
    except Exception:
        hours = 6.0
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )

    try:
        async with neo._driver.session() as s:
            n_lim = int(limit_nodes)
            e_lim = int(limit_edges)

            if effective_mode == "session":
                nr = await s.run(
                    "MATCH (m:Memory {session_id: $sid}) "
                    "RETURN m.id AS id, m.record_type AS record_type, "
                    "m.source_type AS source_type, m.category AS category, "
                    "m.text AS text, m.summary AS summary, "
                    "m.capability AS capability, m.importance AS importance, "
                    "m.created_at AS created_at, m.tags AS tags, "
                    "m.session_id AS session_id, m.model AS model "
                    "ORDER BY m.created_at ASC LIMIT " + str(n_lim),
                    sid=session_id
                )
                sr = await s.run(
                    "MATCH (sess:Session {session_id: $sid}) "
                    "RETURN sess.session_id AS id, sess.session_id AS session_id, "
                    "sess.created_at AS created_at, sess.agent_name AS agent_name",
                    sid=session_id
                )
                er = await s.run(
                    "MATCH (a)-[r]->(b) "
                    "WHERE (a:Memory OR a:Session) AND (b:Memory OR b:Session) "
                    "AND (a.session_id = $sid OR b.session_id = $sid) "
                    "RETURN coalesce(a.id, a.session_id) AS from_id, "
                    "coalesce(b.id, b.session_id) AS to_id, type(r) AS rel_type "
                    "LIMIT " + str(e_lim),
                    sid=session_id
                )

            elif effective_mode == "recent":
                # Pull recent Memory nodes by created_at. Edges and session
                # nodes are bounded by the same cutoff so this mode is
                # cheap to load even on a very large history.
                nr = await s.run(
                    "MATCH (m:Memory) WHERE m.created_at >= $cutoff "
                    "RETURN m.id AS id, m.record_type AS record_type, "
                    "m.source_type AS source_type, m.category AS category, "
                    "m.text AS text, m.summary AS summary, "
                    "m.capability AS capability, m.importance AS importance, "
                    "m.created_at AS created_at, m.tags AS tags, "
                    "m.session_id AS session_id, m.model AS model "
                    "ORDER BY m.created_at DESC LIMIT " + str(n_lim),
                    cutoff=cutoff_iso
                )
                # Session nodes: only those that contain at least one recent
                # Memory. Bounded by n_lim/5 to avoid flooding the result.
                sr = await s.run(
                    "MATCH (sess:Session)-[:CONTAINS]->(m:Memory) "
                    "WHERE m.created_at >= $cutoff "
                    "WITH DISTINCT sess LIMIT " + str(max(1, n_lim // 5)) + " "
                    "RETURN sess.session_id AS id, sess.session_id AS session_id, "
                    "sess.created_at AS created_at, sess.agent_name AS agent_name",
                    cutoff=cutoff_iso
                )
                # Edges where at least one endpoint is a recent Memory.
                er = await s.run(
                    "MATCH (a)-[r]->(b) "
                    "WHERE (a:Memory OR a:Session) AND (b:Memory OR b:Session) "
                    "AND ((a:Memory AND a.created_at >= $cutoff) "
                    "     OR (b:Memory AND b.created_at >= $cutoff)) "
                    "WITH a, r, b, "
                    "     CASE WHEN a:Memory AND b:Memory "
                    "          THEN (CASE WHEN a.created_at > b.created_at "
                    "                     THEN a.created_at ELSE b.created_at END) "
                    "          WHEN a:Memory THEN a.created_at "
                    "          WHEN b:Memory THEN b.created_at "
                    "          ELSE '' END AS endpoint_ts "
                    "RETURN coalesce(a.id, a.session_id) AS from_id, "
                    "coalesce(b.id, b.session_id) AS to_id, type(r) AS rel_type, "
                    "endpoint_ts "
                    "ORDER BY endpoint_ts DESC "
                    "LIMIT " + str(e_lim),
                    cutoff=cutoff_iso
                )

            else:  # all
                # `before` lets the frontend paginate backwards. When set,
                # only return records strictly older than this ISO timestamp.
                # When empty, start from now and return the most recent N.
                before_iso = (before or "").strip()
                if before_iso:
                    nr = await s.run(
                        "MATCH (m:Memory) WHERE m.created_at < $before "
                        "RETURN m.id AS id, m.record_type AS record_type, "
                        "m.source_type AS source_type, m.category AS category, "
                        "m.text AS text, m.summary AS summary, "
                        "m.capability AS capability, m.importance AS importance, "
                        "m.created_at AS created_at, m.tags AS tags, "
                        "m.session_id AS session_id, m.model AS model "
                        "ORDER BY m.created_at DESC LIMIT " + str(n_lim),
                        before=before_iso
                    )
                else:
                    nr = await s.run(
                        "MATCH (m:Memory) "
                        "RETURN m.id AS id, m.record_type AS record_type, "
                        "m.source_type AS source_type, m.category AS category, "
                        "m.text AS text, m.summary AS summary, "
                        "m.capability AS capability, m.importance AS importance, "
                        "m.created_at AS created_at, m.tags AS tags, "
                        "m.session_id AS session_id, m.model AS model "
                        "ORDER BY m.created_at DESC LIMIT " + str(n_lim)
                    )
                # Return only Session nodes referenced by an edge in the
                # result set — otherwise a user with thousands of historical
                # sessions gets hit with thousands of orphan session nodes.
                sr = await s.run(
                    "MATCH (sess:Session)-[:CONTAINS]->(m:Memory) "
                    "WITH DISTINCT sess LIMIT " + str(max(1, n_lim // 5)) + " "
                    "RETURN sess.session_id AS id, sess.session_id AS session_id, "
                    "sess.created_at AS created_at, sess.agent_name AS agent_name"
                )
                # When paginating, narrow edges to ones touching the
                # current window — otherwise we'd repeatedly fetch a full
                # 3000-edge dump for every "Load older" click.
                if before_iso:
                    er = await s.run(
                        "MATCH (a)-[r]->(b) "
                        "WHERE (a:Memory OR a:Session) AND (b:Memory OR b:Session) "
                        "AND ((a:Memory AND a.created_at < $before) "
                        "     OR (b:Memory AND b.created_at < $before)) "
                        "WITH a, r, b, "
                        "     CASE WHEN a:Memory AND b:Memory "
                        "          THEN (CASE WHEN a.created_at > b.created_at "
                        "                     THEN a.created_at ELSE b.created_at END) "
                        "          WHEN a:Memory THEN a.created_at "
                        "          WHEN b:Memory THEN b.created_at "
                        "          ELSE '' END AS endpoint_ts "
                        "RETURN coalesce(a.id, a.session_id) AS from_id, "
                        "coalesce(b.id, b.session_id) AS to_id, type(r) AS rel_type, "
                        "endpoint_ts "
                        "ORDER BY endpoint_ts DESC "
                        "LIMIT " + str(e_lim),
                        before=before_iso
                    )
                else:
                    # Without `before`, return edges for the MOST RECENT
                    # nodes. The previous version had no ORDER BY so Neo4j
                    # returned whatever fit in storage order — usually the
                    # OLDEST edges, which left the freshly-created cap
                    # nodes visible on the canvas with no edges drawn.
                    er = await s.run(
                        "MATCH (a)-[r]->(b) "
                        "WHERE (a:Memory OR a:Session) AND (b:Memory OR b:Session) "
                        "WITH a, r, b, "
                        "     CASE WHEN a:Memory AND b:Memory "
                        "          THEN (CASE WHEN a.created_at > b.created_at "
                        "                     THEN a.created_at ELSE b.created_at END) "
                        "          WHEN a:Memory THEN a.created_at "
                        "          WHEN b:Memory THEN b.created_at "
                        "          ELSE '' END AS endpoint_ts "
                        "RETURN coalesce(a.id, a.session_id) AS from_id, "
                        "coalesce(b.id, b.session_id) AS to_id, type(r) AS rel_type, "
                        "endpoint_ts "
                        "ORDER BY endpoint_ts DESC "
                        "LIMIT " + str(e_lim)
                    )

            mem_nodes  = await nr.data()
            sess_nodes = await sr.data()
            raw_edges  = await er.data()

        # Merge Session nodes into the node list with a canonical shape that
        # matches the Memory-node schema. The frontend deduplicates by id so
        # it's safe if a node id appears in both lists.
        nodes = list(mem_nodes)
        seen_ids = {n.get("id") for n in mem_nodes if n.get("id")}
        for sn in sess_nodes:
            sid = sn.get("id")
            if not sid or sid in seen_ids:
                continue
            agent = sn.get("agent_name") or ""
            nodes.append({
                "id":          sid,
                "record_type": "session",
                "source_type": "system",
                "category":    "session",
                "text":        "",
                "summary":     f"session · {agent}" if agent else "session",
                "capability":  "",
                "importance":  0.4,
                "created_at":  sn.get("created_at") or "",
                "tags":        [agent] if agent else [],
                "session_id":  sid,
                "model":       "",
            })
            seen_ids.add(sid)

        # Deduplicate edges while preserving insertion order
        seen = set()
        edges = []
        for e in raw_edges:
            if e.get("from_id") and e.get("to_id"):
                k = (e["from_id"], e["to_id"])
                if k not in seen:
                    seen.add(k)
                    edges.append({"from_id": e["from_id"], "to_id": e["to_id"],
                                  "relation": e["rel_type"]})
        # Compute oldest/newest timestamps from Memory nodes (Session
        # nodes don't have meaningful created_at for windowing).
        ts_list = [n.get("created_at") for n in mem_nodes if n.get("created_at")]
        oldest_ts = min(ts_list) if ts_list else ""
        newest_ts = max(ts_list) if ts_list else ""

        return {"nodes": nodes, "edges": edges,
                "node_count": len(nodes), "edge_count": len(edges),
                "session_id": session_id, "mode": effective_mode,
                "recent_hours": hours if effective_mode == "recent" else None,
                "oldest_ts": oldest_ts, "newest_ts": newest_ts,
                "page_complete": len(mem_nodes) < n_lim}
    except Exception as ex:
        return {"nodes": [], "edges": [], "error": str(ex)}

@capability(
    "memory.session_nodes",
    http_method="GET", http_path="/memory/session/nodes", http_tags=["memory"],
    memory="off",
    description="Get all Memory nodes for a session from Neo4j (not Postgres).",
)
async def memory_session_nodes(session_id: str, limit: int = 200, trace_id=None):
    MEMORY, _ = _memory()
    if not MEMORY:
        return {"nodes": [], "error": "memory module not loaded"}
    nodes = await MEMORY.get_session_nodes(session_id, limit=limit)
    return {"nodes": nodes, "count": len(nodes), "session_id": session_id}

@capability(
    "memory.session_edges",
    http_method="GET", http_path="/memory/session/edges", http_tags=["memory"],
    memory="off",
    description="Get all graph edges for a session from Neo4j.",
)
async def memory_session_edges(session_id: str, limit: int = 200, trace_id=None):
    MEMORY, _ = _memory()
    if not MEMORY:
        return {"edges": [], "error": "memory module not loaded"}
    edges = await MEMORY.session_edges(session_id, limit=limit)
    return {"edges": edges, "count": len(edges), "session_id": session_id}


@capability(
    "memory.session_graph",
    http_method="GET", http_path="/memory/session/graph", http_tags=["memory"],
    memory="off",
    description="Get all memory nodes for a session, ordered chronologically.",
)
async def memory_session_graph(
    session_id:  str,
    limit:       int  = 50,
    trace_id=None,
):
    MEMORY, _ = _memory()
    if not MEMORY:
        return {"nodes": [], "session_id": session_id}
    try:
        results = await MEMORY.search(
            query   = "",
            limit   = limit,
            filters = {"session_id": session_id},
        )
        nodes = []
        for item in results:
            # results are [{record: {...}, score: n}] dicts from HybridMemoryStore
            rec = item.get("record", {}) if isinstance(item, dict) else {}
            if not rec.get("id"): continue
            nodes.append({
                "id":          rec.get("id",""),
                "record_type": rec.get("record_type","event"),
                "source_type": rec.get("source_type","system"),
                "category":    rec.get("category",""),
                "tags":        rec.get("tags",[]),
                "text":        rec.get("text",""),
                "importance":  rec.get("importance",0.5),
                "capability":  rec.get("capability",""),
                "created_at":  rec.get("created_at",""),
                "relations":   rec.get("relations",[]),
            })
        return {"nodes": nodes, "count": len(nodes), "session_id": session_id}
    except Exception as e:
        return {"error": str(e), "nodes": []}


# ─────────────────────────────────────────────────────────────────────────────
# GRAPH MAINTENANCE / CLEANUP
# ─────────────────────────────────────────────────────────────────────────────
# These capabilities give the user a way to keep the memory graph tidy:
#
#   memory.graph_clear           — wipe all :Memory and :Session nodes
#   memory.graph_stats           — counts by label/category/session
#   memory.graph_normalize       — merge duplicate :Session nodes by id
#   memory.session_delete        — remove one session_id (Session + Memory + edges)
#   memory.session_delete_bulk   — remove multiple session_ids by pattern
#
# All destructive operations require an explicit `confirm=True` flag (or a
# specific session_id) so they can't be triggered accidentally by an LLM
# planner exploring the capability registry.

@capability(
    "memory.graph_stats",
    http_method="GET", http_path="/memory/graph/stats", http_tags=["memory"],
    memory="off",
    description="Counts by label / category / per-session for the memory graph.",
)
async def memory_graph_stats(trace_id=None):
    """Return a small dashboard-friendly breakdown of what's in the graph."""
    MEMORY, _ = _memory()
    if not MEMORY:
        return {"error": "memory module not loaded"}
    neo = MEMORY._backends.get("neo4j")
    if not neo or not neo._driver:
        return {"error": "neo4j not connected"}
    try:
        async with neo._driver.session() as s:
            r1 = await s.run("MATCH (m:Memory) RETURN count(m) AS n")
            mem_count = (await r1.single())["n"]
            r2 = await s.run("MATCH (s:Session) RETURN count(s) AS n")
            sess_count = (await r2.single())["n"]
            r3 = await s.run(
                "MATCH (m:Memory) "
                "RETURN m.category AS cat, count(*) AS n "
                "ORDER BY n DESC LIMIT 20")
            categories = await r3.data()
            r4 = await s.run(
                "MATCH (s:Session) "
                "OPTIONAL MATCH (s)-[:CONTAINS]->(m:Memory) "
                "WITH s, count(m) AS records "
                "RETURN s.session_id AS session_id, "
                "       coalesce(s.agent_name,'') AS agent_name, "
                "       coalesce(s.created_at,'') AS created_at, "
                "       records "
                "ORDER BY created_at DESC LIMIT 50")
            sessions = await r4.data()
            r5 = await s.run(
                "MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS n "
                "ORDER BY n DESC LIMIT 20")
            edges = await r5.data()
            # Detect Session nodes that share the same session_id —
            # there should never be more than one. This is the diagnostic
            # for the "duplicate Session" symptom.
            r6 = await s.run(
                "MATCH (s:Session) "
                "WITH s.session_id AS sid, collect(s) AS group "
                "WHERE size(group) > 1 "
                "RETURN sid, size(group) AS dup_count "
                "ORDER BY dup_count DESC LIMIT 20")
            duplicates = await r6.data()
        return {
            "memory_count":   mem_count,
            "session_count":  sess_count,
            "categories":     categories,
            "sessions":       sessions,
            "edge_types":     edges,
            "duplicates":     duplicates,
            "duplicate_count": sum(d["dup_count"] - 1 for d in duplicates),
        }
    except Exception as e:
        return {"error": str(e)}


@capability(
    "memory.graph_clear",
    http_method="POST", http_path="/memory/graph/clear", http_tags=["memory"],
    memory="off",
    description="DESTRUCTIVE: delete ALL :Memory and :Session nodes. Requires confirm=True.",
)
async def memory_graph_clear(confirm: bool = False, trace_id=None):
    """
    Wipe the entire memory graph. Returns the deletion counts so the caller
    can verify. Refuses without confirm=True.
    """
    if not confirm:
        return {
            "error": "confirm=True required",
            "hint":  "POST {\"confirm\": true} to actually delete",
            "deleted_memory":   0,
            "deleted_sessions": 0,
        }
    MEMORY, _ = _memory()
    if not MEMORY:
        return {"error": "memory module not loaded"}
    neo = MEMORY._backends.get("neo4j")
    if not neo or not neo._driver:
        return {"error": "neo4j not connected"}
    try:
        async with neo._driver.session() as s:
            r1 = await s.run("MATCH (m:Memory) DETACH DELETE m RETURN count(*) AS n")
            del_mem = (await r1.single())["n"]
            r2 = await s.run("MATCH (s:Session) DETACH DELETE s RETURN count(*) AS n")
            del_sess = (await r2.single())["n"]
        # Also clear in-process state so the next cap call doesn't try to
        # link to an id that no longer exists.
        try:
            _SESSIONS.clear()
        except Exception:
            pass
        try:
            _orch = (sys.modules.get("Vera.Orchestration.capability_orchestration")
                     or sys.modules.get("capability_orchestration"))
            if _orch:
                if hasattr(_orch, "_ACT_SESSION_CURSOR"):
                    _orch._ACT_SESSION_CURSOR.clear()
                if hasattr(_orch, "_ACT_SESSION_ROOT"):
                    _orch._ACT_SESSION_ROOT.clear()
                if hasattr(_orch, "_ACT_FABRIC_DEDUP"):
                    _orch._ACT_FABRIC_DEDUP.clear()
        except Exception:
            pass
        log.warning("memory.graph_clear: deleted %d Memory + %d Session nodes",
                    del_mem, del_sess)
        return {"deleted_memory": del_mem, "deleted_sessions": del_sess}
    except Exception as e:
        return {"error": str(e)}


@capability(
    "memory.session_delete",
    http_method="POST", http_path="/memory/session/delete", http_tags=["memory"],
    memory="off",
    description="DESTRUCTIVE: delete one session and all its memories. Requires session_id.",
)
async def memory_session_delete(session_id: str = "", trace_id=None):
    """
    Drop a single session: the :Session node, every :Memory node tagged with
    that session_id, and every edge between them. Caller must pass the
    session_id explicitly — there's no confirm flag because the session_id
    requirement IS the safeguard.
    """
    if not session_id:
        return {"error": "session_id required"}
    MEMORY, _ = _memory()
    if not MEMORY:
        return {"error": "memory module not loaded"}
    neo = MEMORY._backends.get("neo4j")
    if not neo or not neo._driver:
        return {"error": "neo4j not connected"}
    try:
        async with neo._driver.session() as s:
            r1 = await s.run(
                "MATCH (m:Memory {session_id: $sid}) DETACH DELETE m "
                "RETURN count(*) AS n", sid=session_id)
            del_mem = (await r1.single())["n"]
            r2 = await s.run(
                "MATCH (s:Session {session_id: $sid}) DETACH DELETE s "
                "RETURN count(*) AS n", sid=session_id)
            del_sess = (await r2.single())["n"]
        # Clear in-process caches that reference this session
        try: _SESSIONS.pop(session_id, None)
        except Exception: pass
        try:
            _orch = (sys.modules.get("Vera.Orchestration.capability_orchestration")
                     or sys.modules.get("capability_orchestration"))
            if _orch:
                if hasattr(_orch, "_ACT_SESSION_CURSOR"):
                    _orch._ACT_SESSION_CURSOR.pop(session_id, None)
                if hasattr(_orch, "_ACT_SESSION_ROOT"):
                    _orch._ACT_SESSION_ROOT.pop(session_id, None)
        except Exception:
            pass
        log.info("memory.session_delete: %s — %d records, %d session nodes",
                 session_id, del_mem, del_sess)
        return {"session_id": session_id,
                "deleted_memory":   del_mem,
                "deleted_sessions": del_sess}
    except Exception as e:
        return {"error": str(e)}


@capability(
    "memory.session_delete_bulk",
    http_method="POST", http_path="/memory/session/delete_bulk", http_tags=["memory"],
    memory="off",
    description="DESTRUCTIVE: delete multiple sessions matching a session_id prefix. Requires confirm=True.",
)
async def memory_session_delete_bulk(prefix: str = "",
                                      confirm: bool = False,
                                      trace_id=None):
    """
    Delete every session whose session_id starts with `prefix`.
    Useful for clearing things like all `chat-*` sessions or all `dream:*`
    sessions in one call. Requires both a non-empty prefix AND confirm=True.
    """
    if not prefix or not confirm:
        return {"error": "prefix and confirm=True required"}
    MEMORY, _ = _memory()
    if not MEMORY:
        return {"error": "memory module not loaded"}
    neo = MEMORY._backends.get("neo4j")
    if not neo or not neo._driver:
        return {"error": "neo4j not connected"}
    try:
        async with neo._driver.session() as s:
            r1 = await s.run(
                "MATCH (m:Memory) WHERE m.session_id STARTS WITH $p "
                "DETACH DELETE m RETURN count(*) AS n", p=prefix)
            del_mem = (await r1.single())["n"]
            r2 = await s.run(
                "MATCH (s:Session) WHERE s.session_id STARTS WITH $p "
                "DETACH DELETE s RETURN count(*) AS n", p=prefix)
            del_sess = (await r2.single())["n"]
        # In-process cache cleanup for matching keys
        try:
            for sid in [k for k in _SESSIONS if k.startswith(prefix)]:
                _SESSIONS.pop(sid, None)
        except Exception:
            pass
        try:
            _orch = (sys.modules.get("Vera.Orchestration.capability_orchestration")
                     or sys.modules.get("capability_orchestration"))
            if _orch and hasattr(_orch, "_ACT_SESSION_CURSOR"):
                for sid in [k for k in list(_orch._ACT_SESSION_CURSOR)
                            if k.startswith(prefix)]:
                    _orch._ACT_SESSION_CURSOR.pop(sid, None)
        except Exception:
            pass
        log.warning("memory.session_delete_bulk %s: %d records, %d sessions",
                    prefix, del_mem, del_sess)
        return {"prefix": prefix,
                "deleted_memory":   del_mem,
                "deleted_sessions": del_sess}
    except Exception as e:
        return {"error": str(e)}


@capability(
    "memory.graph_normalize",
    http_method="POST", http_path="/memory/graph/normalize", http_tags=["memory"],
    memory="off",
    description="Merge duplicate :Session nodes (same session_id), prune orphan Memory.",
)
async def memory_graph_normalize(trace_id=None):
    """
    Idempotent housekeeping pass:
      1. Merge any duplicate :Session nodes by session_id (keeps the
         oldest, redirects all incoming/outgoing edges to it).
      2. Drop any :Memory node whose session_id no longer matches an
         existing :Session (genuine orphans). Skipped if there are no
         orphans.

    Returns counts so the caller can see what was changed.
    """
    MEMORY, _ = _memory()
    if not MEMORY:
        return {"error": "memory module not loaded"}
    neo = MEMORY._backends.get("neo4j")
    if not neo or not neo._driver:
        return {"error": "neo4j not connected"}
    try:
        async with neo._driver.session() as s:
            # 1. Merge duplicate :Session nodes. apoc.refactor.mergeNodes
            #    is the canonical solution but isn't always installed —
            #    fall back to a manual edge-redirect approach.
            r1 = await s.run(
                "MATCH (s:Session) "
                "WITH s.session_id AS sid, collect(s) AS group "
                "WHERE size(group) > 1 "
                "RETURN sid, [n IN group | id(n)] AS internal_ids")
            dup_groups = await r1.data()

            merged = 0
            for grp in dup_groups:
                ids = grp["internal_ids"]
                if len(ids) < 2:
                    continue
                keeper = ids[0]
                for losers in [ids[1:]]:  # batch of losers per group
                    pass
                losers = ids[1:]
                # Redirect inbound edges (anything pointing to a loser)
                # to point to the keeper instead.
                await s.run(
                    "MATCH (n) WHERE id(n) IN $losers "
                    "MATCH (k) WHERE id(k) = $keeper "
                    "OPTIONAL MATCH (a)-[r]->(n) "
                    "WHERE NOT id(a) IN $losers AND NOT id(a) = $keeper "
                    "WITH a, r, k, type(r) AS rt WHERE r IS NOT NULL "
                    "CALL apoc.merge.relationship(a, rt, {}, properties(r), k) "
                    "YIELD rel RETURN count(*)",
                    keeper=keeper, losers=losers
                ) if False else None
                # Simpler portable approach: just delete losers.
                # (Edges pointing TO the loser were :CONTAINS edges from
                # auto-store; the keeper has its own such edges already
                # because both sessions share session_id and the store
                # writes against session_id, not internal node id.)
                await s.run(
                    "MATCH (n) WHERE id(n) IN $losers DETACH DELETE n",
                    losers=losers)
                merged += len(losers)

            # 2. Find orphan Memory nodes (session_id present but no
            #    matching :Session). The activity_worker now MERGEs the
            #    :Session before storing, but old data may have orphans.
            r2 = await s.run(
                "MATCH (m:Memory) WHERE m.session_id IS NOT NULL "
                "AND NOT EXISTS { MATCH (s:Session {session_id: m.session_id}) } "
                "RETURN count(m) AS n")
            orphan_count = (await r2.single())["n"]

            # We DON'T auto-delete orphans — they may be valuable and the
            # user should review them via memory.graph_stats first. Just
            # report the count here. The user can call session_delete
            # explicitly to clean specific session_ids.

        log.info("memory.graph_normalize: merged %d duplicate sessions, "
                 "%d orphan Memory nodes detected", merged, orphan_count)
        return {"merged_duplicate_sessions": merged,
                "orphan_memory_count":       orphan_count,
                "note": "Orphan Memory nodes are NOT auto-deleted. "
                        "Use memory.session_delete with the relevant "
                        "session_id to remove them, or call "
                        "memory.graph_clear to wipe everything."}
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITY DECORATOR EXTENSION
# ─────────────────────────────────────────────────────────────────────────────

def extend_capability_decorator():
    """No-op: memory= is now a native parameter of the @capability decorator."""
    log.info("memory_hooks: memory= param is native in @capability — no patching needed")


# Retro-apply memory modes to existing caps based on their group
_CAP_MEMORY_DEFAULTS = {
    # Explicitly ON
    "llm":      "auto",
    "agent":    "on",
    # Explicitly OFF - observability/infra
    "obs":      "off",
    "syslog":   "off",
    "caps":     "off",
    "ollama":   "off",
    "system":   "off",
    "memory":   "off",   # memory caps don't record themselves
    "health":   "off",
}


def apply_default_memory_modes():
    """
    Fallback: apply default memory modes to any caps that somehow didn't get
    an explicit memory= param (e.g. dynamically added caps).
    All statically defined caps already have memory= set explicitly.
    """
    applied = 0
    for cap_name, cap in CAPABILITY_REGISTRY.items():
        if "memory" not in cap:
            group = cap_name.split(".")[0]
            cap["memory"] = _CAP_MEMORY_DEFAULTS.get(group, "auto")
            applied += 1
    if applied:
        log.info("memory_hooks: applied default memory modes to %d dynamic caps", applied)




@capability(
    "memory.label_node",
    http_method="POST", http_path="/memory/label", http_tags=["memory"],
    memory="off",
    description="Use LLM to generate a short readable label for a memory node based on its content.",
)
async def memory_label_node(
    record_id: str,
    trace_id=None,
):
    """Ask the LLM to produce a concise ≤6-word label for a memory node."""
    import sys as _sys
    MEMORY, _ = _memory()
    if not MEMORY:
        return {"error": "memory not available"}
    rec = await MEMORY.get(record_id)
    if not rec:
        return {"error": f"record not found: {record_id}"}

    # Build context string
    parts = []
    if rec.record_type: parts.append(f"type:{rec.record_type}")
    if rec.source_type: parts.append(f"source:{rec.source_type}")
    if rec.capability:  parts.append(f"cap:{rec.capability}")
    if rec.category:    parts.append(f"cat:{rec.category}")
    if rec.text:        parts.append(f"text:{rec.text[:300]}")
    context = " | ".join(parts)

    try:
        from Vera.Orchestration.capability_orchestration import ollama_generate
        label = await ollama_generate(
            context,
            system=(
                "Produce a short label for this memory node. "
                "Rules: max 5 words, no quotes, no punctuation at end, "
                "capture the key action or content. "
                "Examples: 'User asked about Redis', 'LLM explained DAG nodes', "
                "'DAG weather pipeline ran', 'Session started with assistant'. "
                "Reply with ONLY the label, nothing else."
            ),
            prefer_gpu=True,
        )
        label = label.strip().strip('"').strip("'")[:60]
        # Update the record summary with the generated label
        await MEMORY.update(record_id, {"summary": label})
        return {"record_id": record_id, "label": label}
    except Exception as e:
        log.warning("memory_label_node: %s", e)
        return {"error": str(e)}


@capability(
    "memory.label_session",
    http_method="POST", http_path="/memory/label/session", http_tags=["memory"],
    memory="off",
    description="Label all unlabelled nodes in a session. Runs label_node for each node missing a summary.",
)
async def memory_label_session(
    session_id: str,
    limit: int = 30,
    trace_id=None,
):
    MEMORY, _ = _memory()
    if not MEMORY:
        return {"error": "memory not available"}
    results = await MEMORY.search("", limit=limit, filters={"session_id": session_id})
    labelled = 0
    errors = []
    for item in results:
        rec_d = item.get("record", {}) if isinstance(item, dict) else {}
        rid = rec_d.get("id")
        # Skip nodes that already have a meaningful summary
        if not rid or (rec_d.get("summary","").strip() and len(rec_d.get("summary","")) > 5):
            continue
        try:
            r = await memory_label_node(record_id=rid)
            if "label" in r:
                labelled += 1
        except Exception as e:
            errors.append(str(e))
    return {"labelled": labelled, "errors": errors, "session_id": session_id}

# ─────────────────────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────────────────────

async def _startup():
    # Must run after all other modules have loaded their caps
    await asyncio.sleep(1.0)
    extend_capability_decorator()
    apply_default_memory_modes()
    patch_capability_for_memory()
    log.info("vera_memory_hooks ready")


try:
    _loop = asyncio.get_event_loop()
    if _loop.is_running():
        _loop.create_task(_startup())
except Exception:
    pass