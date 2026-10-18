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
  @capability("obs.health",   memory="off", ..., contract={"presentation": {'kind': 'status', 'form': 'status'}})

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
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    CAPABILITY_REGISTRY, capability, emit_event, now_iso, schedule,
)
try:                                    # who a recalled record came from; one hit per repeated text
    from Vera.vera.fabric import recall_core as _recall_core
except ImportError:                     # pragma: no cover
    from . import recall_core as _recall_core
try:                                    # chat turns into the retrieval eval set (FM-7.0 b)
    from Vera.vera.fabric import retrieval_eval as _reval
except Exception:                       # pragma: no cover
    try:
        from . import retrieval_eval as _reval
    except Exception:
        _reval = None
try:                                    # was a recalled memory used in the reply (CU-M.2)
    from Vera.vera.fabric import memory_usage_core as _usage_core
except ImportError:                     # pragma: no cover
    from . import memory_usage_core as _usage_core
try:                                    # Chroma calls on their own small pool, with a finite timeout (RF-77)
    from Vera.vera.fabric import chroma_bound_core as _chroma_bound
except ImportError:                     # pragma: no cover
    from . import chroma_bound_core as _chroma_bound
try:                                    # how strong a memory still is: half-life per class, renewed by use (CU-M.3)
    from Vera.vera.fabric import memory_strength_core as _strength_core
except ImportError:                     # pragma: no cover
    from . import memory_strength_core as _strength_core
try:                                    # which memories are noise: kept, ranked last (CU-M.8)
    from Vera.vera.fabric import memory_noise_core as _noise_core
except ImportError:                     # pragma: no cover
    from . import memory_noise_core as _noise_core
try:                                    # one canonical record per repeated text (CU-M.5)
    from Vera.vera.fabric import memory_canonical_core as _canon_core
except ImportError:                     # pragma: no cover
    from . import memory_canonical_core as _canon_core

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

# ── Chat I/O → entity graph ───────────────────────────────────────────────────
# When on, each stored chat message is (async, non-blocking) ingested into the
# `chat.messages` fabric dataset. That runs the NLP entity extractor and — via
# fabric.entity_graph.link_memory in the post-ingest pipeline — links the
# extracted entities back to this message's :Memory node. Heuristic-first; LLM
# extraction stays opt-in through the per-dataset fabric config. Default ON;
# set VERA_CHAT_ENTITY_EXTRACT=0 to disable.
import os as _os
CHAT_ENTITY_EXTRACT = _os.getenv("VERA_CHAT_ENTITY_EXTRACT", "1") not in \
    ("0", "false", "False", "no", "off", "")
CHAT_ENTITY_MIN_LEN = 30


def _fabric_mod():
    """Lazy handle to the data_fabric module (avoids a load-time import cycle)."""
    return (sys.modules.get("data_fabric")
            or sys.modules.get("Vera.vera.fabric.data_fabric"))


async def _ingest_message_entities(node_id: str, text: str, role: str,
                                   session_id: str, agent_name: str):
    """Fire-and-forget: push one chat message through the fabric so entity
    extraction runs and (via fabric.entity_graph.link_memory) connects the
    extracted entities to this message's :Memory node. Never raises; never
    blocks the turn."""
    if not CHAT_ENTITY_EXTRACT or not node_id or not text:
        return
    if len(text.strip()) < CHAT_ENTITY_MIN_LEN:
        return
    fabric = _fabric_mod()
    if not fabric or not hasattr(fabric, "ingest_dataset"):
        return
    try:
        await fabric.ingest_dataset(
            dataset_id="chat.messages",
            data=[{
                "text":       text[:8000],
                "role":       role,
                "node_id":    node_id,
                "session_id": session_id,
                "agent":      agent_name,
                "ts":         now_iso(),
            }],
            source="chat",
            source_id=session_id or agent_name,
            tags=["chat", role, agent_name] if agent_name else ["chat", role],
        )
    except Exception as e:
        log.debug("chat entity ingest [%s]: %s",
                  (node_id or "?")[:8], e)


def _memory():
    """Lazy import to avoid circular dependency at load time."""
    vera_memory = sys.modules.get("memory")
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

    # Only what the user typed: the page appends recalled context and capability instructions to the message it
    # sends, and storing those made every later search match them (1,191 such records by 2026-10-06, E29).
    human_text = _noise_core.user_said(human_text)
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
    await MEMORY.store(human_rec, producer_kind="conversation")
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
    await MEMORY.store(ai_rec, producer_kind="conversation")
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
        _orch = (sys.modules.get("Vera.vera.capability_orchestration")
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

    # Entity extraction on chat I/O — non-blocking. Routes each message's text
    # through the fabric so the NLP entity engine runs and connects the extracted
    # entities to this message's :Memory node, exactly like all other fabric
    # ingestion. Fire-and-forget: chat latency is unaffected.
    if CHAT_ENTITY_EXTRACT:
        try:
            asyncio.create_task(_ingest_message_entities(
                human_id, human_text, "human", session_id, agent_name))
            asyncio.create_task(_ingest_message_entities(
                ai_id, ai_text, "ai", session_id, agent_name))
        except Exception as _e:
            log.debug("chat entity ingest schedule: %s", _e)

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
    await MEMORY.store(dag_rec, producer_kind="workflow")

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
                trigger_text = _noise_core.user_said(trigger_text)
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
                await MEMORY.store(trigger_rec, producer_kind="conversation")
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
        await MEMORY.store(step_rec, producer_kind="workflow")
        rel = "STARTS" if prev_id == dag_id else "THEN"
        await _link_nodes(prev_id, step_id, rel,
                          {"step": i, "cap": cap_name}, session_id=session_id)
        prev_id = step_id

    await _update_session_last(session_id, dag_id)
    log.debug("memory_hooks: DAG execution recorded — %d steps session=%s", n_steps, session_id)

# ─────────────────────────────────────────────────────────────────────────────
# MEMORY CONTEXT INJECTION
# ─────────────────────────────────────────────────────────────────────────────

# Chat executes caps only via inline [[cap:name {json}]] markers scanned out
# of the MODEL'S OWN reply text, with no way to tell "this came from a
# recalled memory" apart from "asserted just now" -- so any recalled text
# that happens to contain that syntax is live-executable the moment it's
# echoed back. Live-caught 2026-08-13: a stored AI reply was, verbatim,
# "[[loop: stop running dags over and over]]" (itself an echo of earlier
# injected noise) -- recalling that record later re-surfaced the same
# live-executable text and it triggered a real, unwanted agentic loop on an
# unrelated later turn. Defang the opening bracket pair so recalled text
# stays readable but can never itself be re-executed, regardless of which
# backend/tier surfaced it or whether the record is human- or AI-authored.
_LIVE_MARKER_RE = re.compile(r"\[\[\s*(cap|loop|suggest)\s*:", re.I)


def _sanitize_recalled_text(text: str) -> str:
    if not text:
        return text
    text = _LIVE_MARKER_RE.sub(lambda m: f"[{m.group(1)}:", text)
    if text.strip() in ("[Retrieved Context]", "[memory]"):
        return ""
    return text


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

    items = [f for f in (_extract_record_fields(it) for it in results) if f]
    return _chatmem_format(items)


# ─────────────────────────────────────────────────────────────────────────────
# TWO-TIER MEMORY INJECTION (fast this turn, slow delivered to the next)
# ─────────────────────────────────────────────────────────────────────────────
# get_agent_memory_context() above blocks the WHOLE turn on MEMORY.search(),
# whose dominant cost is embed_text() — live-measured 3.5-8.5s PER CALL on
# this instance's shared CPU embed nodes, saturated by a near-continuous
# stream of unrelated background embed traffic (fabric ingestion, sandbox
# session syncs; live-observed back-to-back ~4s embeds with almost no gaps).
# That is the entire reported symptom: a chat turn with memory on pays that
# tax before the LLM even starts.
#
# get_agent_memory_context_v2 splits this per the requested design:
#   FAST  — Postgres + Neo4j fulltext search, neither of which needs an
#           embedding at all (HybridMemoryStore.search_fast_and_slow), capped
#           at a couple seconds. Injected THIS turn.
#   SLOW  — the full hybrid search including the Chroma vector hits, running
#           in the background against the SAME already-started embed call.
#           Its result is stored for session_id and picked up at the START
#           of the NEXT turn — so the expensive semantic pass still happens,
#           it just never blocks the turn that triggered it.
#   DECAY — memories injected in past turns don't just accumulate forever or
#           vanish the instant they're not re-surfaced; each turn a record
#           isn't refreshed by a fresh fast/slow hit, its effective weight is
#           multiplied down (attention-style recency decay) until it drops
#           out, so context stays focused on what's actually still relevant.
_CHATMEM_DECAY_FACTOR   = float(_os.getenv("VERA_CHATMEM_DECAY_FACTOR", "0.65"))
_CHATMEM_DECAY_MIN      = float(_os.getenv("VERA_CHATMEM_DECAY_MIN", "0.05"))
_CHATMEM_DECAY_MAX_TURNS = int(_os.getenv("VERA_CHATMEM_DECAY_MAX_TURNS", "6"))
_CHATMEM_FAST_TIMEOUT_S = float(_os.getenv("VERA_CHATMEM_FAST_TIMEOUT_S", "2.5"))
_CHATMEM_PENDING_TTL_S  = int(_os.getenv("VERA_CHATMEM_PENDING_TTL_S", "300"))
_CHATMEM_ACTIVE_TTL_S   = int(_os.getenv("VERA_CHATMEM_ACTIVE_TTL_S", "3600"))


def _chatmem_pending_key(session_id: str) -> str:
    return f"vera:chatmem:pending:{session_id}"


def _chatmem_active_key(session_id: str) -> str:
    return f"vera:chatmem:active:{session_id}"


def _extract_record_fields(item: Any) -> Optional[Dict[str, Any]]:
    """Normalise one HybridMemoryStore.search() result item — {"record":...,
    "score":...} dict OR a bare (MemoryRecord, float) tuple — into the flat
    shape this module stores/formats. Mirrors get_agent_memory_context's own
    inline handling of both shapes; extracted so the two-tier path can share
    it instead of re-deriving the same logic a second time."""
    repeats = 1
    if isinstance(item, dict):
        rec = item.get("record") or item
        score = item.get("score", 0.0)
        repeats = int(item.get("repeats", 1) or 1)       # set by the search's one-hit-per-text pass
    else:
        try:
            rec, score = item
        except (TypeError, ValueError):
            return None
    get = (lambda k, d=None: rec.get(k, d)) if isinstance(rec, dict) else (lambda k, d=None: getattr(rec, k, d))
    rid        = get("id", "") or ""
    created_at = get("created_at", "") or ""
    human_text = get("human_text", True)
    # The speaker is read from what the record IS. `human_text` defaults to True, so judging by it alone showed
    # every tool call, dream and event to the model as something the user had said.
    role       = _recall_core.role_label(rec)
    text       = _sanitize_recalled_text((get("text", "") or "")[:200])
    if not text or not rid:
        return None
    out = {"id": rid, "created_at": created_at, "human_text": bool(human_text), "role": role,
           "text": text, "score": float(score or 0.0)}
    if repeats > 1:
        out["repeats"] = repeats
    return out


async def _chatmem_load_active(session_id: str) -> Dict[str, Dict[str, Any]]:
    r = _redis()
    if r is None:
        return {}
    try:
        raw = await r.get(_chatmem_active_key(session_id))
        if not raw:
            return {}
        items = json.loads(raw)
        return {it["id"]: it for it in items if isinstance(it, dict) and it.get("id")}
    except Exception as e:
        log.debug("chatmem load active: %s", e)
        return {}


async def _chatmem_save_active(session_id: str, active: Dict[str, Dict[str, Any]]) -> None:
    r = _redis()
    if r is None:
        return
    try:
        await r.set(_chatmem_active_key(session_id), json.dumps(list(active.values())),
                    ex=_CHATMEM_ACTIVE_TTL_S)
    except Exception as e:
        log.debug("chatmem save active: %s", e)


async def _chatmem_take_pending(session_id: str) -> List[Dict[str, Any]]:
    """Read AND clear the slow-tier result the previous turn left behind, if
    it finished in time. GETDEL-shaped (get then delete) so it's delivered
    exactly once, to the next turn that asks."""
    r = _redis()
    if r is None:
        return []
    key = _chatmem_pending_key(session_id)
    try:
        raw = await r.get(key)
        if not raw:
            return []
        await r.delete(key)
        return json.loads(raw)
    except Exception as e:
        log.debug("chatmem take pending: %s", e)
        return []


def _chatmem_merge_refresh(active: Dict[str, Dict[str, Any]],
                           fresh: List[Dict[str, Any]]) -> None:
    """Upsert fresh hits into the active set, resetting each one's decay
    clock — a record that got re-surfaced is, by definition, still
    relevant right now. Mutates `active` in place."""
    for f in fresh:
        fid = f.get("id")
        if not fid:
            continue
        prev = active.get(fid)
        score = max(float(f.get("score", 0.0)), float(prev["score"]) if prev else 0.0)
        active[fid] = {**f, "score": score, "turns_since_seen": 0}


def _chatmem_decay_and_prune(active: Dict[str, Dict[str, Any]],
                             just_refreshed: set) -> None:
    """Age every entry NOT freshly refreshed this turn, then drop whatever
    decayed past the floor or has gone stale too many turns running —
    attention-style recency weighting so old context fades rather than
    piling up forever or needing an explicit clear."""
    drop = []
    for rid, it in active.items():
        if rid in just_refreshed:
            continue
        turns = int(it.get("turns_since_seen", 0)) + 1
        it["turns_since_seen"] = turns
        it["score"] = float(it.get("score", 0.0)) * _CHATMEM_DECAY_FACTOR
        if it["score"] < _CHATMEM_DECAY_MIN or turns > _CHATMEM_DECAY_MAX_TURNS:
            drop.append(rid)
    for rid in drop:
        active.pop(rid, None)


def _chatmem_format(items: List[Dict[str, Any]]) -> str:
    if not items:
        return ""
    lines = ["=== Relevant memories from past conversations ==="]
    # One line per distinct text: the active set is keyed by record id, so two records carrying the same sentence
    # (a line repeated across turns) would otherwise take two of the block's few lines.
    for it in _recall_core.collapse_repeats(list(items)):
        ts = (it.get("created_at") or "")[:16].replace("T", " ")
        # Entries saved before "role" was stored carry only the flag; read them as before.
        role = it.get("role") or ("User" if it.get("human_text", True) else "Assistant")
        text = it.get("text", "")
        if text:
            rep = int(it.get("repeats", 1) or 1)
            lines.append(f"[{ts}] {role}: {text}" + (f" (x{rep})" if rep > 1 else ""))
    return "\n".join(lines) if len(lines) > 1 else ""


async def _chatmem_deliver_slow(session_id: str, slow_task: "asyncio.Task") -> None:
    """Background continuation: wait for the slow tier (embed + Chroma) to
    finish, then stash its result for the NEXT turn to pick up. Runs
    detached from the request that spawned it — never awaited by the
    caller, never allowed to raise into anything."""
    try:
        slow_results = await slow_task
    except Exception as e:
        log.debug("chatmem slow tier failed for %s: %s", session_id, e)
        return
    fresh = [f for f in (_extract_record_fields(it) for it in (slow_results or [])) if f]
    if not fresh:
        return
    r = _redis()
    if r is None:
        return
    try:
        await r.set(_chatmem_pending_key(session_id), json.dumps(fresh),
                    ex=_CHATMEM_PENDING_TTL_S)
        log.info("chatmem: slow tier delivered %d hit(s) for %s, queued for next turn",
                 len(fresh), session_id[:12])
    except Exception as e:
        log.debug("chatmem stash pending for %s: %s", session_id, e)


async def get_agent_memory_context_v2(
    session_id:  str,
    query:       str,
    agent_name:  str  = "",
    limit:       int  = 5,
    tags:        Optional[List[str]] = None,
) -> str:
    """Two-tier, decaying version of get_agent_memory_context — same return
    shape (a formatted context-injection string), same call signature, but
    returns as soon as the FAST tier (+ any slow-tier delivery already
    waiting from a prior turn) is ready instead of blocking on embed_text().
    Falls back to the plain single-tier path if Redis or the two-tier
    method aren't available, so this degrades to the old (correct, just
    slower) behaviour rather than ever failing outright."""
    # search for what the user typed: the page appends recalled context and capability instructions to the message,
    # and searching with them found the last turn's recalled text again (the write side strips them too)
    query = _noise_core.user_said(query)
    MEMORY, _ = _memory()
    if not MEMORY:
        return ""
    if _redis() is None or not hasattr(MEMORY, "search_fast_and_slow") or not session_id:
        return await get_agent_memory_context(
            session_id=session_id, query=query, agent_name=agent_name,
            limit=limit, tags=tags)

    filters: Dict = {}
    if agent_name:
        filters["tags"] = [agent_name]
    elif tags:
        filters["tags"] = tags

    active = await _chatmem_load_active(session_id)

    # 1. Deliver whatever the PREVIOUS turn's slow tier finished in time for.
    pending = await _chatmem_take_pending(session_id)
    just_refreshed = {f["id"] for f in pending if f.get("id")}
    _chatmem_merge_refresh(active, pending)
    if pending:
        log.info("chatmem: turn for %s picked up %d hit(s) delivered by a prior turn's slow tier",
                 session_id[:12], len(pending))

    # 2. Fast tier for THIS turn.
    t0 = time.monotonic()
    try:
        fast_out, slow_task = await MEMORY.search_fast_and_slow(
            query=query, limit=limit, filters=filters if filters else None,
            fast_timeout=_CHATMEM_FAST_TIMEOUT_S)
    except Exception as e:
        log.debug("get_agent_memory_context_v2 fast tier: %s", e)
        fast_out, slow_task = [], None
    log.info("chatmem: fast tier for %s took %.2fs, %d hit(s)",
             session_id[:12], time.monotonic() - t0, len(fast_out))

    fresh_fast = [f for f in (_extract_record_fields(it) for it in fast_out) if f]
    just_refreshed |= {f["id"] for f in fresh_fast}
    _chatmem_merge_refresh(active, fresh_fast)

    # 3. Age everything else and drop what's decayed out.
    _chatmem_decay_and_prune(active, just_refreshed)
    await _chatmem_save_active(session_id, active)

    # 4. Kick the slow tier's delivery off in the background — NOT awaited.
    if slow_task is not None:
        asyncio.create_task(_chatmem_deliver_slow(session_id, slow_task))

    ranked = sorted(active.values(), key=lambda it: it.get("score", 0.0), reverse=True)
    _note_injected(session_id, ranked[:limit])
    if _reval is not None:
        _reval.on_chat_recall(session_id, query, [it.get("id") for it in ranked[:limit] if it.get("id")])
    return _chatmem_format(ranked[:limit])


async def get_agent_memory_context_fast(
    session_id:  str,
    query:       str,
    agent_name:  str  = "",
    limit:       int  = 5,
    tags:        Optional[List[str]] = None,
) -> str:
    """What a chat turn would be given right now, read without changing anything.

    The fast tier (Postgres + Neo4j, no embedding) for `query`, merged with the session's current two-tier active
    set as it stands. Unlike get_agent_memory_context_v2 it does not age the active set, take the pending slow-tier
    delivery or start an embed - so a live preview can call it on every keystroke without decaying the session's
    memory or loading the shared embed nodes. The full blocking search stays available as depth="full"."""
    MEMORY, _ = _memory()
    if not MEMORY:
        return ""
    if not hasattr(MEMORY, "search_fast"):
        return await get_agent_memory_context(session_id=session_id, query=query,
                                              agent_name=agent_name, limit=limit, tags=tags)
    filters: Dict = {}
    if agent_name:
        filters["tags"] = [agent_name]
    elif tags:
        filters["tags"] = tags
    try:
        hits = await MEMORY.search_fast(query=query, limit=limit, filters=filters or None,
                                        timeout=_CHATMEM_FAST_TIMEOUT_S)
    except Exception as e:
        log.debug("get_agent_memory_context_fast: %s", e)
        hits = []
    merged: Dict[str, Dict[str, Any]] = {}
    if session_id and _redis() is not None:
        merged.update(await _chatmem_load_active(session_id))      # read only - never saved back from here
    for f in (_extract_record_fields(it) for it in hits):
        if f and (f["id"] not in merged or merged[f["id"]].get("score", 0.0) < f["score"]):
            merged[f["id"]] = f
    ranked = sorted(merged.values(), key=lambda it: it.get("score", 0.0), reverse=True)
    return _chatmem_format(ranked[:limit])


# ── Usage signals (context-and-ui CU-M.2; owner 2026-10-04: useless memories fade, useful ones stay) ──────────────
# What each chat turn put in front of the model, and whether the reply drew on it. The half-life (CU-M.3) reads the
# counts. Kept per session until the turn ends; one upsert per record into vera_memory_usage.
_INJECTED: Dict[str, Dict[str, str]] = {}      # session_id -> {record_id: text}
_USAGE_TABLE_READY = False
_USAGE_DDL = """
    CREATE TABLE IF NOT EXISTS vera_memory_usage (
        record_id      TEXT PRIMARY KEY,
        injected_n     INT NOT NULL DEFAULT 0,
        used_n         INT NOT NULL DEFAULT 0,
        last_injected  TIMESTAMPTZ,
        last_used      TIMESTAMPTZ
    )"""


def _note_injected(session_id: str, items: List[Dict[str, Any]]) -> None:
    """Remember what this session's turn was given (the agent's own memory block, the deep search, the page's)."""
    if not session_id:
        return
    slot = _INJECTED.setdefault(session_id, {})
    for it in items or []:
        rid = str((it or {}).get("id") or "")
        if rid and rid not in slot:
            slot[rid] = str((it or {}).get("text") or "")
    if len(_INJECTED) > 500:                     # a session whose turn never ended does not hold memory forever
        for k in list(_INJECTED)[:100]:
            _INJECTED.pop(k, None)


async def _usage_pool():
    MEMORY, _ = _memory()
    pg = (getattr(MEMORY, "_backends", {}) or {}).get("postgres") if MEMORY else None
    return getattr(pg, "_pool", None)


async def record_turn_usage(session_id: str, reply: str, page_ids: Optional[List[str]] = None,
                            extra: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """After a turn: judge every memory it was given against the reply and count it in vera_memory_usage. Never raises;
    a census, probe or empty turn is not counted (memory_usage_core.counts_toward_use)."""
    global _USAGE_TABLE_READY
    given = _INJECTED.pop(session_id or "", {})
    _chat_pending = _reval.take_chat(session_id) if _reval is not None else None   # recorded only if counted
    out: Dict[str, Any] = {"injected": 0, "used": 0}
    try:
        if not _usage_core.counts_toward_use(session_id, reply):
            return out
        for it in extra or []:
            rid = str((it or {}).get("id") or "")
            if rid and rid not in given:
                given[rid] = str((it or {}).get("text") or "")
        pool = await _usage_pool()
        if pool is None:
            return out
        ids = [str(i) for i in (page_ids or []) if i and str(i) not in given][:40]
        async with pool.acquire() as conn:
            if ids:
                for row in await conn.fetch("SELECT id, text FROM vera_memories WHERE id = ANY($1::text[])", ids):
                    given[row["id"]] = row["text"] or ""
            if not given:
                return out
            verdicts = _usage_core.judge_all(reply, [{"id": k, "text": v} for k, v in given.items()])
            if not _USAGE_TABLE_READY:
                await conn.execute("SET lock_timeout = '3s'")
                await conn.execute(_USAGE_DDL)
                _USAGE_TABLE_READY = True
            await conn.executemany(
                """INSERT INTO vera_memory_usage (record_id, injected_n, used_n, last_injected, last_used)
                   VALUES ($1, 1, $2, NOW(), CASE WHEN $2 = 1 THEN NOW() ELSE NULL END)
                   ON CONFLICT (record_id) DO UPDATE SET
                       injected_n = vera_memory_usage.injected_n + 1,
                       used_n = vera_memory_usage.used_n + EXCLUDED.used_n,
                       last_injected = NOW(),
                       last_used = COALESCE(EXCLUDED.last_used, vera_memory_usage.last_used)""",
                [(v["id"], 1 if v["used"] else 0) for v in verdicts])
            # a faded memory the reply drew on is revived: back in the live store, its fade marker cleared (owner,
            # 2026-10-05: a useful memory must never be locked out for good)
            used_ids = [v["id"] for v in verdicts if v["used"]]
            if _reval is not None:
                _reval.on_chat_used(session_id, _chat_pending, used_ids)
            revived = []
            if used_ids:
                try:
                    revived = [r["id"] for r in await conn.fetch(
                        """UPDATE vera_memories SET archived = FALSE,
                               metadata = (COALESCE(metadata, '{}'::jsonb) - 'faded_at' - 'fade_reason')
                                          || jsonb_build_object('revived_at', NOW()::text)
                           WHERE id = ANY($1::text[]) AND archived AND metadata ? 'faded_at'
                           RETURNING id""", used_ids)]
                except Exception as e:
                    log.debug("revive faded: %s", e)
        for rid in revived:
            try:
                MEMORY, _ = _memory()
                neo = (getattr(MEMORY, "_backends", {}) or {}).get("neo4j") if MEMORY else None
                if neo is not None:
                    await neo.update(rid, {"archived": False})
            except Exception:
                pass
        out = {"injected": len(verdicts), "used": sum(1 for v in verdicts if v["used"]), "revived": len(revived)}
        try:
            await emit_event({"type": "memory.feedback", "session_id": session_id, **out,
                              "used_ids": [v["id"] for v in verdicts if v["used"]][:20],
                              # what the turn was given, so the chat can drop the runs of what it did not use (WSU-62)
                              "injected_ids": [v["id"] for v in verdicts][:60]})
        except Exception:
            pass
    except Exception as e:
        log.debug("record_turn_usage: %s", e)
    return out


_DEEP_HEADING = "=== Earlier conversations (deep memory search, all sessions) ==="


async def get_memory_context_deep(query: str, limit: int = 8, budget_s: float = 1.5) -> Dict[str, Any]:
    """Deep memory for a turn that refers back ("last time", "what did we decide"): every session and agent, the
    vector leg included when it answers inside the budget (the owner's 1.5 s, 2026-10-04).

    The fast tier (Postgres + Neo4j) is always used; the vector leg replaces it with the merged result only if it
    lands in what is left of the budget. A vector result that misses the budget is cancelled, not stashed - the
    session's pending slot belongs to the agent's own two-tier memory. Never raises.
    Returns {text, hits, vector (bool: the vector leg made it), ms, fast_ms, fast_hits (the text tier's own time and
    hits), found (hits before they were read as memories)}."""
    out: Dict[str, Any] = {"text": "", "hits": 0, "vector": False, "ms": 0.0}
    MEMORY, _ = _memory()
    if not MEMORY or not hasattr(MEMORY, "search_fast_and_slow") or not str(query or "").strip():
        return out
    t0 = time.monotonic()
    info: Dict[str, Any] = {}
    hits, out["vector"] = await _recall_core.fast_then_vector(
        lambda fast_timeout: MEMORY.search_fast_and_slow(query=query, limit=limit, fast_timeout=fast_timeout),
        budget_s, info=info)
    items = [f for f in (_extract_record_fields(it) for it in (hits or [])) if f][:limit]
    out["items"] = [{"id": it.get("id"), "text": it.get("text", "")} for it in items if it.get("id")]   # for usage (CU-M.2)
    out.update(text=_recall_core.retitle_block(_chatmem_format(items), _DEEP_HEADING), hits=len(items),
               ms=round((time.monotonic() - t0) * 1000, 1), found=len(hits or []),
               fast_ms=info.get("fast_ms"), fast_hits=info.get("fast_hits"))
    if info.get("error"):
        out["error"] = info["error"]
    return out


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
    description="Retrieve relevant past memories for injection into an agent's context, each line naming who or "
                "what it came from (User, Assistant, Tool call, Dream, ...) and repeated texts shown once. "
                "depth='fast' (default): keyword and graph search without an embedding, plus what the session's "
                "chat memory currently holds - quick, and it changes nothing. depth='full': the complete hybrid "
                "search including vector similarity (slower: it waits for an embedding).",
)
async def memory_agent_context(
    session_id:  str,
    query:       str,
    agent_name:  str  = "",
    limit:       int  = 5,
    tags:        str  = "",
    depth:       str  = "fast",
    trace_id=None,
):
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    full = str(depth or "").strip().lower() == "full"
    getter = get_agent_memory_context if full else get_agent_memory_context_fast
    context = await getter(
        session_id  = session_id,
        query       = query,
        agent_name  = agent_name,
        limit       = limit,
        tags        = tag_list,
    )
    return {"context": context, "found": bool(context), "depth": "full" if full else "fast"}


@capability(
    "memory.usage.top",
    contract={"presentation": {"kind": "table", "form": "table", "map": {"rows": "rows"}, "draw": {"columns": ["text", "record_type", "injected_n", "used_n", "use_rate", "last_used"]}, "empty": "No memory use recorded yet"}},
    http_method="GET", http_path="/memory/usage/top", http_tags=["memory"],
    memory="off", silent=True,
    description="Which memories chat turns draw on, and which they are given but ignore (counted after every chat "
                "turn: each memory put in front of the model is judged used or not against the reply). Read-only. "
                "Inputs: order ('used' | 'ignored', default used), limit (int=20). Output: {rows:[{record_id, text, "
                "record_type, injected_n, used_n, use_rate, last_injected, last_used}], count}.",
)
async def memory_usage_top(order: str = "used", limit: int = 20, trace_id=None):
    try:
        lim = max(1, min(int(limit or 20), 200))
    except (TypeError, ValueError):
        lim = 20
    by = ("u.used_n DESC, u.injected_n DESC" if str(order or "used").lower() != "ignored"
          else "(u.injected_n - u.used_n) DESC, u.injected_n DESC")
    pool = await _usage_pool()
    if pool is None:
        return {"rows": [], "count": 0, "error": "memory postgres not connected"}
    try:
        async with pool.acquire() as conn:
            exists = await conn.fetchval("SELECT to_regclass('vera_memory_usage') IS NOT NULL")
            if not exists:
                return {"rows": [], "count": 0, "note": "no turn has been counted yet"}
            rows = await conn.fetch(
                "SELECT u.record_id, left(coalesce(m.text, ''), 200) AS text, m.record_type, u.injected_n, u.used_n, "
                "u.last_injected, u.last_used FROM vera_memory_usage u LEFT JOIN vera_memories m ON m.id = u.record_id "
                "ORDER BY " + by + " LIMIT $1", lim)
    except Exception as e:
        return {"rows": [], "count": 0, "error": str(e)[:200]}
    out = []
    for r in rows:
        d = dict(r)
        d["use_rate"] = round(d["used_n"] / d["injected_n"], 2) if d.get("injected_n") else 0.0
        for k in ("last_injected", "last_used"):
            d[k] = d[k].isoformat() if d.get(k) else ""
        out.append(d)
    return {"rows": out, "count": len(out)}


_FADE_REPORT_KEY = "vera:memory:fade:report"
_FADE_POLICY_KEY = "vera:memory:fade:policy"


async def _fade_auto_on() -> bool:
    """Is nightly fading switched on? Off unless the user turns it on."""
    r = _redis()
    if r is None:
        return False
    try:
        raw = await r.get(_FADE_POLICY_KEY)
        d = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw) if raw else {}
        return bool(isinstance(d, dict) and d.get("auto"))
    except Exception:
        return False


async def _fade_overrides() -> Dict[str, Any]:
    """The user's fading settings (owner, 2026-10-05: choose what gets forgotten and how quickly)."""
    r = _redis()
    if r is None:
        return {}
    try:
        raw = await r.get(_FADE_POLICY_KEY)
        if not raw:
            return {}
        d = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw)
        return d.get("overrides") or {} if isinstance(d, dict) else {}
    except Exception:
        return {}


@capability(
    "memory.fade.policy",
    contract={"presentation": {"kind": "table", "form": "table", "map": {"rows": "policy"}, "draw": {"columns": ["name", "enabled", "half_life_days", "floor"]}}},
    http_method="POST", http_path="/memory/fade/policy", http_tags=["memory"],
    memory="off",
    description="The user's controls over memory fading (owner, 2026-10-05: 'select what gets forgotten and how "
                "quickly'). Per class of memory (conversation, session, observation, research, dream, transcript, "
                "tool, estate, fact, preference): enabled (may it be forgotten at all), half_life_days (how quickly; "
                "null = never decays) and floor (the strength below which it fades). Without 'set' it reads. Inputs: "
                "set (dict {class: {enabled?, half_life_days?, floor?}} - merged over the current settings), reset "
                "(bool - back to the defaults), auto (bool - fade every night at about 04:00; off by default), by "
                "(str). Output: {policy (in force), overrides, defaults, ranges, version, updated_at, by, auto}.",
)
async def memory_fade_policy(set: Optional[Dict[str, Any]] = None, reset: bool = False, by: str = "",
                             auto: Optional[bool] = None, trace_id=None):
    r = _redis()
    current = await _fade_overrides()
    stored: Dict[str, Any] = {}
    auto_now = await _fade_auto_on()
    if auto is not None:
        auto_now = str(auto).strip().lower() in ("1", "true", "yes", "on")
    if reset or set or auto is not None:
        overrides = {} if reset else dict(current)
        for cls, o in (set or {}).items() if isinstance(set, dict) else []:
            if cls in _strength_core.HALF_LIFE and isinstance(o, dict):
                merged = dict(overrides.get(cls) or {})
                merged.update({k: v for k, v in o.items() if k in ("enabled", "half_life_days", "floor")})
                overrides[cls] = merged
        stored = {"overrides": overrides, "auto": auto_now, "updated_at": now_iso(), "by": str(by or "")[:80]}
        if r is None:
            return {"error": "redis not connected - the settings cannot be kept"}
        await r.set(_FADE_POLICY_KEY, json.dumps(stored))
        current = overrides
        try:
            await emit_event({"type": "memory.fade.policy", "by": stored["by"], "classes": sorted(set or {})})
        except Exception:
            pass
    else:
        try:
            raw = await r.get(_FADE_POLICY_KEY) if r is not None else None
            stored = json.loads(raw) if raw else {}
        except Exception:
            stored = {}
    return {"policy": _strength_core.policy_from(current), "overrides": current,
            "defaults": _strength_core.policy_from({}),
            "ranges": {"half_life_days": list(_strength_core.HALF_LIFE_RANGE), "floor": list(_strength_core.FLOOR_RANGE)},
            "version": _strength_core.POLICY_VERSION, "updated_at": stored.get("updated_at", ""),
            "by": stored.get("by", ""), "auto": auto_now}


@capability(
    "memory.fade.report",
    http_method="POST", http_path="/memory/fade/report", http_tags=["memory"],
    memory="off", silent=True,
    description="DRY RUN ONLY - what the memory fading job would archive today, and why. Every live memory record is "
                "scored with its class's half-life, renewed by use (memory_strength_core + the usage counts chat turns "
                "write); a record below its class's floor would fade (a soft archive, revived on use). Nothing is "
                "changed: the owner reads this before any fading is switched on. The scan of the whole store runs in "
                "the background (one at a time) and keeps its result at " + _FADE_REPORT_KEY + "; this answers at "
                "once with the last report. Inputs: refresh (bool - start a new scan; one starts anyway when none is "
                "kept), samples (int=5 per class), batch (int=5000). Output: {report: {policy, total, fading, "
                "by_class:{cls:{total, fading, mean_strength, samples:[{id, text, strength, record_type}]}}, ms, at} "
                "| null, computing (bool)}.",
)
async def memory_fade_report(refresh: bool = False, samples: int = 5, batch: int = 5000, trace_id=None):
    global _FADE_TASK
    stored = None
    r = _redis()
    if r is not None:
        try:
            raw = await r.get(_FADE_REPORT_KEY)
            if raw:
                stored = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw)
        except Exception:
            stored = None
    running = _FADE_TASK is not None and not _FADE_TASK.done()
    want = str(refresh).strip().lower() in ("1", "true", "yes", "on")
    if (want or stored is None) and not running:
        _FADE_TASK = asyncio.create_task(_fade_compute(samples, batch))
        running = True
    return {"report": stored, "computing": running}


_FADE_TASK = None


async def _fade_compute(samples: int = 5, batch: int = 5000) -> Dict[str, Any]:
    """The scan behind memory.fade.report - a background task, never inside a request's 60 s."""
    t0 = time.monotonic()
    try:
        n_samples = max(0, min(int(samples or 5), 20))
        size = max(500, min(int(batch or 5000), 20000))
    except (TypeError, ValueError):
        n_samples, size = 5, 5000
    pool = await _usage_pool()
    if pool is None:
        return {"error": "memory postgres not connected"}
    now = time.time()
    policy = _strength_core.policy_from(await _fade_overrides())
    usage: Dict[str, Dict[str, Any]] = {}
    by: Dict[str, Dict[str, Any]] = {}
    total = fading = 0
    try:
        async with pool.acquire() as conn:
            if await conn.fetchval("SELECT to_regclass('vera_memory_usage') IS NOT NULL"):
                for r in await conn.fetch("SELECT record_id, injected_n, used_n, last_used FROM vera_memory_usage"):
                    usage[r["record_id"]] = {"injected_n": r["injected_n"], "used_n": r["used_n"], "last_used": r["last_used"]}
            last = ""
            while True:      # keyset pages, the loop handed back between them - never one long hold
                rows = await conn.fetch(
                    "SELECT id, record_type, source_type, category, importance, created_at, "
                    "left(coalesce(text, ''), 24) AS head FROM vera_memories "
                    "WHERE NOT archived AND id > $1 ORDER BY id LIMIT $2", last, size)
                if not rows:
                    break
                for r in rows:
                    rec = dict(r)
                    rec["text"] = rec.pop("head") or ""
                    cls = _strength_core.class_of(rec)
                    s = _strength_core.strength(rec, usage.get(rec["id"]), now, policy)
                    b = by.setdefault(cls, {"total": 0, "fading": 0, "sum": 0.0, "samples": []})
                    b["total"] += 1
                    b["sum"] += s
                    total += 1
                    if _strength_core.fades(rec, usage.get(rec["id"]), now, policy):
                        b["fading"] += 1
                        fading += 1
                        if len(b["samples"]) < n_samples:
                            b["samples"].append({"id": rec["id"], "record_type": rec.get("record_type") or "",
                                                 "strength": round(s, 4)})
                last = rows[-1]["id"]
                await asyncio.sleep(0)
            ids = [x["id"] for b in by.values() for x in b["samples"]]
            if ids:
                texts = {r["id"]: r["t"] for r in await conn.fetch(
                    "SELECT id, left(coalesce(text, ''), 160) AS t FROM vera_memories WHERE id = ANY($1::text[])", ids)}
                for b in by.values():
                    for x in b["samples"]:
                        x["text"] = texts.get(x["id"], "")
    except Exception as e:
        return {"error": str(e)[:200], "total": total}
    out = {"policy": _strength_core.POLICY_VERSION, "policy_in_force": policy, "dry_run": True, "total": total, "fading": fading,
           "by_class": {k: {"total": v["total"], "fading": v["fading"],
                            "mean_strength": round(v["sum"] / v["total"], 4) if v["total"] else 0.0,
                            "samples": v["samples"]} for k, v in sorted(by.items(), key=lambda kv: -kv[1]["total"])},
           "usage_rows": len(usage), "ms": round((time.monotonic() - t0) * 1000)}
    r = _redis()
    if r is not None:
        try:
            await r.set(_FADE_REPORT_KEY, json.dumps(dict(out, at=now_iso())))
        except Exception:
            pass
    return out


# ── the fading job itself (owner, 2026-10-05: "apply the policy as is", with controls and resurfacing in place) ──
_FADE_RUN_KEY = "vera:memory:fade:last_run"
_FADE_RUN_TASK = None
_FADE_BATCH = 1000                 # Postgres / Neo4j rows per statement
_FADE_CHROMA_BATCH = 40            # a Chroma write holds its database ~60 ms per id (E13): small, paced
_FADE_CHROMA_PAUSE_S = 1.0


def _in_backup_window(t: Optional[time.struct_time] = None) -> bool:
    """01:00-03:30 local: the nightly pg_dump holds every table (E24) - no bulk writes then."""
    t = t or time.localtime()
    m = t.tm_hour * 60 + t.tm_min
    return 60 <= m < 210


async def _fade_candidates(pool, policy: Dict[str, Any], limit: int, classes: Optional[List[str]] = None
                           ) -> List[Tuple[str, str, float, float]]:
    """(id, class, strength, floor) of live records that fade under `policy`, up to `limit`."""
    now = time.time()
    usage: Dict[str, Dict[str, Any]] = {}
    out: List[Tuple[str, str, float, float]] = []
    want = set(classes or [])
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('vera_memory_usage') IS NOT NULL"):
            for r in await conn.fetch("SELECT record_id, injected_n, used_n, last_used FROM vera_memory_usage"):
                usage[r["record_id"]] = {"injected_n": r["injected_n"], "used_n": r["used_n"], "last_used": r["last_used"]}
        last = ""
        while len(out) < limit:
            rows = await conn.fetch(
                "SELECT id, record_type, source_type, category, importance, created_at, "
                "left(coalesce(text, ''), 24) AS head FROM vera_memories "
                "WHERE NOT archived AND id > $1 ORDER BY id LIMIT 5000", last)
            if not rows:
                break
            for r in rows:
                rec = dict(r)
                rec["text"] = rec.pop("head") or ""
                cls = _strength_core.class_of(rec)
                if want and cls not in want:
                    continue
                if _strength_core.fades(rec, usage.get(rec["id"]), now, policy):
                    out.append((rec["id"], cls, _strength_core.strength(rec, usage.get(rec["id"]), now, policy),
                                float(policy.get(cls, {}).get("floor") or 0.0)))
                    if len(out) >= limit:
                        break
            last = rows[-1]["id"]
            await asyncio.sleep(0)
    return out


def _fade_stores():
    """(neo4j backend or None, chroma collection or None, memory class or None) of the live memory store."""
    MEMORY, _ = _memory()
    backs = (getattr(MEMORY, "_backends", {}) or {}) if MEMORY else {}
    neo = backs.get("neo4j")
    neo = neo if neo is not None and getattr(neo, "_driver", None) else None
    cb = backs.get("chroma")
    return neo, (getattr(cb, "_collection", None) if cb else None), (type(MEMORY) if MEMORY else None)


async def _fade_mark_neo4j(neo, ids: List[str], out: Dict[str, Any], key: str = "neo4j") -> None:
    """Set archived on the Memory nodes of `ids`; counts only the nodes that changed."""
    try:
        async with neo._driver.session() as ses:
            res = await ses.run("UNWIND $ids AS id MATCH (m:Memory {id: id}) WHERE coalesce(m.archived, false) = false "
                                "SET m.archived = true RETURN count(m) AS n", {"ids": ids})
            row = await res.single()
            out[key] = out.get(key, 0) + int((row or {}).get("n", 0) or 0)
    except Exception as e:
        out.setdefault("errors", []).append("neo4j: %s" % str(e)[:120])


async def _fade_mark_chroma(coll, ids: List[str], out: Dict[str, Any], key: str = "chroma") -> None:
    """Set archived on the vectors of `ids` that exist, keeping the rest of their metadata; paced (E13)."""
    for i in range(0, len(ids), _FADE_CHROMA_BATCH):
        part = ids[i:i + _FADE_CHROMA_BATCH]
        try:
            got = await _chroma_bound.run(lambda: coll.get(ids=part, include=["metadatas"]))
            have = got.get("ids") or []
            if have:
                metas = [dict(m or {}, archived=True) for m in (got.get("metadatas") or [{}] * len(have))]
                await _chroma_bound.run(lambda: coll.update(ids=have, metadatas=metas))
                out[key] = out.get(key, 0) + len(have)
                await asyncio.sleep(_FADE_CHROMA_PAUSE_S)
        except Exception as e:
            out.setdefault("errors", []).append("chroma: %s" % str(e)[:120])
            await asyncio.sleep(_FADE_CHROMA_PAUSE_S * 5)


async def _fade_reconcile(pool, neo, coll, out: Dict[str, Any]) -> None:
    """Postgres is the record of what has faded (archived with faded_at; a revive clears both). A run cut short - a
    restart mid-run, 2026-10-06 - leaves Neo4j and Chroma behind it: bring them level before fading more."""
    faded: set = set()
    last = ""
    async with pool.acquire() as conn:
        while True:
            rows = await conn.fetch("SELECT id FROM vera_memories WHERE archived AND metadata ? 'faded_at' "
                                    "AND id > $1 ORDER BY id LIMIT 20000", last)
            if not rows:
                break
            faded.update(r["id"] for r in rows)
            last = rows[-1]["id"]
            await asyncio.sleep(0)
    out["reconciled"] = {"faded_in_postgres": len(faded), "neo4j": 0, "chroma": 0}
    if not faded:
        return
    if neo is not None:
        ids = sorted(faded)
        for i in range(0, len(ids), _FADE_BATCH):
            await _fade_mark_neo4j(neo, ids[i:i + _FADE_BATCH], out["reconciled"])
            await asyncio.sleep(0)
    if coll is not None:                # the vector store is far smaller than the faded set: walk it, not the ids
        behind: List[str] = []
        off = 0
        while True:
            try:
                got = await _chroma_bound.run(lambda: coll.get(include=["metadatas"], limit=500, offset=off))
            except Exception as e:
                out.setdefault("errors", []).append("chroma: %s" % str(e)[:120])
                break
            ids = got.get("ids") or []
            if not ids:
                break
            for vid, meta in zip(ids, got.get("metadatas") or [{}] * len(ids)):
                if vid in faded and not (meta or {}).get("archived"):
                    behind.append(vid)
            off += len(ids)
            await asyncio.sleep(0)
        if behind:
            await _fade_mark_chroma(coll, behind, out["reconciled"])


async def _fade_apply(limit: int = 20000, classes: Optional[List[str]] = None, by: str = "") -> Dict[str, Any]:
    """Soft-archive what fades: archived in Postgres (with faded_at and the reason), Neo4j and - where a vector is
    left - Chroma, a batch at a time in all three, so a run cut short leaves at most one batch behind and the next run
    levels it first. Stops at the nightly backup. Revived on use; a strong match still finds it (resurfacing). Never
    raises; progress is recorded after every batch."""
    t0 = time.monotonic()
    out: Dict[str, Any] = {"by": by, "started_at": now_iso(), "policy": _strength_core.POLICY_VERSION,
                           "faded": 0, "neo4j": 0, "chroma": 0, "by_class": {}, "running": True}
    r = _redis()

    async def _record():
        if r is not None:
            try:
                await r.set(_FADE_RUN_KEY, json.dumps(out))
            except Exception:
                pass

    MEMORY_CLS = None
    try:
        pool = await _usage_pool()
        if pool is None:
            out["error"] = "memory postgres not connected"
            out["running"] = False
            return out
        neo, coll, MEMORY_CLS = _fade_stores()
        await _fade_reconcile(pool, neo, coll, out)
        await _record()
        policy = _strength_core.policy_from(await _fade_overrides())
        picked = await _fade_candidates(pool, policy, limit, classes)
        out["picked"] = len(picked)
        for i in range(0, len(picked), _FADE_BATCH):
            if _in_backup_window():
                out["stopped"] = "the nightly backup window began - the rest waits for the next run"
                break
            chunk = picked[i:i + _FADE_BATCH]
            ids = [c[0] for c in chunk]
            reasons = ["%s: strength %.4f below floor %.3f (policy %s)" % (c[1], c[2], c[3], _strength_core.POLICY_VERSION)
                       for c in chunk]
            async with pool.acquire() as conn:
                done = await conn.fetch(
                    """UPDATE vera_memories m SET archived = TRUE,
                           metadata = COALESCE(m.metadata, '{}'::jsonb)
                                      || jsonb_build_object('faded_at', NOW()::text, 'fade_reason', r.reason)
                       FROM unnest($1::text[], $2::text[]) AS r(id, reason)
                       WHERE m.id = r.id AND NOT m.archived
                       RETURNING m.id""", ids, reasons)
            done_ids = [d["id"] for d in done]
            cls_of = {c[0]: c[1] for c in chunk}
            for d in done_ids:
                out["by_class"][cls_of[d]] = out["by_class"].get(cls_of[d], 0) + 1
            out["faded"] += len(done_ids)
            if done_ids and neo is not None:
                await _fade_mark_neo4j(neo, done_ids, out)
            if done_ids and coll is not None:
                await _fade_mark_chroma(coll, done_ids, out)
            await _record()
            await asyncio.sleep(0)
    except Exception as e:
        out["error"] = str(e)[:200]
    finally:
        try:   # recall asks for faded matches from now on, not in ten minutes
            if MEMORY_CLS is not None:
                MEMORY_CLS._FADED_SEEN["t"] = 0.0
        except Exception:
            pass
    out["running"] = False
    out["ms"] = round((time.monotonic() - t0) * 1000)
    out["finished_at"] = now_iso()
    await _record()
    try:
        await emit_event({"type": "memory.faded", **{k: out[k] for k in ("faded", "neo4j", "chroma", "by_class", "by")}})
    except Exception:
        pass
    return out


@capability(
    "memory.fade.run",
    http_method="POST", http_path="/memory/fade/run", http_tags=["memory"],
    memory="off",
    description="Apply the fading policy: soft-archive the memories that have faded under the policy in force "
                "(memory.fade.policy) - archived and marked with when and why, never deleted; a strong match still "
                "brings one back and using it revives it. Without confirm=true it only says how many would fade. "
                "Runs in the background, one at a time, never 01:00-03:30 (the nightly backup). Inputs: confirm "
                "(bool), limit (int=20000 per run), classes (csv - only these), by (str). Output: {started | dry, "
                "would_fade, last_run}.",
)
async def memory_fade_run(confirm: bool = False, limit: int = 20000, classes: str = "", by: str = "", trace_id=None):
    global _FADE_RUN_TASK
    r = _redis()
    last = None
    if r is not None:
        try:
            raw = await r.get(_FADE_RUN_KEY)
            last = json.loads(raw) if raw else None
        except Exception:
            last = None
    running = _FADE_RUN_TASK is not None and not _FADE_RUN_TASK.done()
    if isinstance(last, dict) and last.get("running") and not running:
        last["interrupted"] = "the run stopped part-way (a restart); the next run levels the stores first"
    try:
        lim = max(1, min(int(limit or 20000), 200000))
    except (TypeError, ValueError):
        lim = 20000
    cls = [c.strip() for c in str(classes or "").split(",") if c.strip() in _strength_core.HALF_LIFE]
    if running:
        return {"running": True, "last_run": last}
    if str(confirm).strip().lower() not in ("1", "true", "yes", "on"):
        rep = None
        if r is not None:
            try:
                raw = await r.get(_FADE_REPORT_KEY)
                rep = json.loads(raw) if raw else None
            except Exception:
                rep = None
        return {"dry": True, "would_fade": (rep or {}).get("fading"), "report_at": (rep or {}).get("at", ""),
                "last_run": last, "note": "nothing changed - pass confirm=true to fade (memory.fade.report refresh=true "
                                          "recounts first)"}
    if _in_backup_window():
        return {"refused": "the nightly backup runs 01:00-03:30 - fading waits", "last_run": last}
    _FADE_RUN_TASK = asyncio.create_task(_fade_apply(lim, cls or None, str(by or "")[:80]))
    return {"started": True, "limit": lim, "classes": cls, "last_run": last}


# ── noise marks for the records already stored (new ones are marked as they are written) ──
_NOISE_RUN_KEY = "vera:memory:noise:last_run"
_NOISE_RUN_TASK = None


async def _noise_scan(pool) -> Tuple[Dict[str, List[str]], List[str], Dict[str, int], int]:
    """Over the live records recall can return (not archived, not a tool call, not an imported transcript line):
    ({id: reasons} to mark, [ids] whose mark no longer holds, {reason: count}, records scanned)."""
    mark: Dict[str, List[str]] = {}
    unmark: List[str] = []
    by: Dict[str, int] = {}
    seen = 0
    last = ""
    async with pool.acquire() as conn:
        while True:
            rows = await conn.fetch(
                "SELECT id, record_type, left(coalesce(text, ''), 2000) AS text, metadata ? 'noise' AS marked "
                "FROM vera_memories WHERE NOT archived AND id > $1 "
                "AND record_type <> ALL($2::text[]) AND NOT (text LIKE '[Claude Code%' OR text LIKE '[Codex%') "
                "ORDER BY id LIMIT 5000", last, list(_recall_core.ACTIVITY_TYPES))
            if not rows:
                break
            for r in rows:
                seen += 1
                why = _noise_core.noise_reasons({"record_type": r["record_type"], "text": r["text"]})
                for w in why:
                    by[w] = by.get(w, 0) + 1
                if why and not r["marked"]:
                    mark[r["id"]] = why
                elif r["marked"] and not why:
                    unmark.append(r["id"])
            last = rows[-1]["id"]
            await asyncio.sleep(0)
    return mark, unmark, by, seen


async def _noise_apply() -> Dict[str, Any]:
    """Mark noise in Postgres (metadata.noise = reasons) and Neo4j (m.noise), clear marks the rules no longer give.
    Never raises; the run is recorded."""
    t0 = time.monotonic()
    out: Dict[str, Any] = {"started_at": now_iso(), "marked": 0, "unmarked": 0, "neo4j": 0}
    try:
        pool = await _usage_pool()
        if pool is None:
            out["error"] = "memory postgres not connected"
            return out
        mark, unmark, out["by_reason"], out["scanned"] = await _noise_scan(pool)
        items = list(mark.items())
        for i in range(0, len(items), _FADE_BATCH):
            chunk = items[i:i + _FADE_BATCH]
            async with pool.acquire() as conn:
                done = await conn.fetch(
                    """UPDATE vera_memories m SET metadata = COALESCE(m.metadata, '{}'::jsonb)
                                                         || jsonb_build_object('noise', r.why::jsonb)
                       FROM unnest($1::text[], $2::text[]) AS r(id, why)
                       WHERE m.id = r.id RETURNING m.id""",
                    [c[0] for c in chunk], [json.dumps(c[1]) for c in chunk])
            out["marked"] += len(done)
            await asyncio.sleep(0)
        for i in range(0, len(unmark), _FADE_BATCH):
            async with pool.acquire() as conn:
                done = await conn.fetch("UPDATE vera_memories SET metadata = metadata - 'noise' "
                                        "WHERE id = ANY($1::text[]) RETURNING id", unmark[i:i + _FADE_BATCH])
            out["unmarked"] += len(done)
            await asyncio.sleep(0)
        neo, _coll, _cls = _fade_stores()
        if neo is not None:
            for flag, ids in ((True, [c[0] for c in items]), (False, unmark)):
                for i in range(0, len(ids), _FADE_BATCH):
                    try:
                        async with neo._driver.session() as ses:
                            res = await ses.run("UNWIND $ids AS id MATCH (m:Memory {id: id}) SET m.noise = $flag "
                                                "RETURN count(m) AS n", {"ids": ids[i:i + _FADE_BATCH], "flag": flag})
                            row = await res.single()
                            out["neo4j"] += int((row or {}).get("n", 0) or 0)
                    except Exception as e:
                        out.setdefault("errors", []).append("neo4j: %s" % str(e)[:120])
                    await asyncio.sleep(0)
    except Exception as e:
        out["error"] = str(e)[:200]
    out["ms"] = round((time.monotonic() - t0) * 1000)
    out["finished_at"] = now_iso()
    r = _redis()
    if r is not None:
        try:
            await r.set(_NOISE_RUN_KEY, json.dumps(out))
        except Exception:
            pass
    return out


@capability(
    "memory.noise.mark",
    http_method="POST", http_path="/memory/noise/mark", http_tags=["memory"],
    memory="off",
    description="Mark the stored memories that are noise - status lines, prompt text stored as a message, context "
                "injected into a message, raw tool JSON, empty reports, one-word lines - so recall ranks them after "
                "every real memory. Nothing is deleted or archived; new memories are marked as they are written. "
                "Without confirm=true it only counts. Runs in the background, never 01:00-03:30. Inputs: confirm "
                "(bool). Output: {dry, would_mark, would_unmark, by_reason | started | running, last_run}.",
)
async def memory_noise_mark(confirm: Any = False, trace_id=None) -> Dict[str, Any]:
    global _NOISE_RUN_TASK
    r = _redis()
    last = None
    if r is not None:
        try:
            raw = await r.get(_NOISE_RUN_KEY)
            last = json.loads(raw) if raw else None
        except Exception:
            last = None
    if _NOISE_RUN_TASK is not None and not _NOISE_RUN_TASK.done():
        return {"running": True, "last_run": last}
    if str(confirm).strip().lower() not in ("1", "true", "yes", "on"):
        pool = await _usage_pool()
        if pool is None:
            return {"error": "memory postgres not connected", "last_run": last}
        mark, unmark, by, seen = await _noise_scan(pool)
        return {"dry": True, "scanned": seen, "would_mark": len(mark), "would_unmark": len(unmark), "by_reason": by,
                "last_run": last}
    if _in_backup_window():
        return {"refused": "the nightly backup runs 01:00-03:30 - marking waits", "last_run": last}
    _NOISE_RUN_TASK = asyncio.create_task(_noise_apply())
    return {"started": True, "last_run": last}


# ── canonical records for the copies already stored (new ones are folded in as they are written) ──
_CANON_RUN_KEY = "vera:memory:canonical:last_run"
_CANON_RUN_TASK = None


async def _canon_scan(pool) -> Dict[str, Any]:
    """Over the live records recall can return: the text keys still to store, {occurrence: canonical} to set, the
    canonical marks to clear, and counts."""
    rows: List[Dict[str, Any]] = []
    keys: Dict[str, str] = {}
    last = ""
    async with pool.acquire() as conn:
        while True:
            batch = await conn.fetch(
                "SELECT id, record_type, created_at, left(coalesce(text, ''), 4000) AS text, "
                "metadata->>'text_key' AS text_key, metadata->>'canonical_id' AS canonical_id "
                "FROM vera_memories WHERE NOT archived AND id > $1 AND record_type <> ALL($2::text[]) "
                "AND NOT (text LIKE '[Claude Code%' OR text LIKE '[Codex%') ORDER BY id LIMIT 5000",
                last, list(_canon_core.SKIP_TYPES))
            if not batch:
                break
            for r in batch:
                d = dict(r)
                if not _canon_core.eligible(d):
                    continue
                if not d.get("text_key"):
                    d["text_key"] = _canon_core.text_key(d["text"])
                    if d["text_key"]:
                        keys[d["id"]] = d["text_key"]
                d["created_at"] = str(d.get("created_at") or "")
                d.pop("text", None)
                rows.append(d)
            last = batch[-1]["id"]
            await asyncio.sleep(0)
    point = _canon_core.plan(rows)
    clear = _canon_core.unmark(rows)
    return {"rows": len(rows), "keys": keys, "point": point, "clear": clear,
            "canonicals": len(set(point.values()))}


async def _canon_apply() -> Dict[str, Any]:
    """Store text keys, point every copy at its canonical (Postgres metadata, Neo4j property, an OCCURRENCE_OF edge
    in both), clear marks that no longer hold. Never raises; the run is recorded."""
    t0 = time.monotonic()
    out: Dict[str, Any] = {"started_at": now_iso(), "keys": 0, "pointed": 0, "cleared": 0, "neo4j": 0, "edges": 0}
    try:
        pool = await _usage_pool()
        if pool is None:
            out["error"] = "memory postgres not connected"
            return out
        scan = await _canon_scan(pool)
        out["scanned"] = scan["rows"]
        kitems = list(scan["keys"].items())
        for i in range(0, len(kitems), _FADE_BATCH):
            chunk = kitems[i:i + _FADE_BATCH]
            async with pool.acquire() as conn:
                done = await conn.fetch(
                    """UPDATE vera_memories m SET metadata = COALESCE(m.metadata, '{}'::jsonb)
                                                         || jsonb_build_object('text_key', r.k)
                       FROM unnest($1::text[], $2::text[]) AS r(id, k) WHERE m.id = r.id RETURNING m.id""",
                    [c[0] for c in chunk], [c[1] for c in chunk])
            out["keys"] += len(done)
            await asyncio.sleep(0)
        pitems = list(scan["point"].items())
        for i in range(0, len(pitems), _FADE_BATCH):
            chunk = pitems[i:i + _FADE_BATCH]
            ids, canons = [c[0] for c in chunk], [c[1] for c in chunk]
            async with pool.acquire() as conn:
                done = await conn.fetch(
                    """UPDATE vera_memories m SET metadata = COALESCE(m.metadata, '{}'::jsonb)
                                                         || jsonb_build_object('canonical_id', r.c)
                       FROM unnest($1::text[], $2::text[]) AS r(id, c) WHERE m.id = r.id RETURNING m.id""",
                    ids, canons)
                out["pointed"] += len(done)
                res = await conn.execute(
                    """INSERT INTO vera_memory_edges (from_id, to_id, relation, properties, session_id, created_at)
                       SELECT r.id, r.c, 'OCCURRENCE_OF', '{}'::jsonb, '', NOW()
                       FROM unnest($1::text[], $2::text[]) AS r(id, c)
                       ON CONFLICT (from_id, to_id, relation) DO NOTHING""", ids, canons)
                try:
                    out["edges"] += int(str(res).split()[-1])
                except Exception:
                    pass
            await asyncio.sleep(0)
        for i in range(0, len(scan["clear"]), _FADE_BATCH):
            async with pool.acquire() as conn:
                done = await conn.fetch("UPDATE vera_memories SET metadata = metadata - 'canonical_id' "
                                        "WHERE id = ANY($1::text[]) RETURNING id", scan["clear"][i:i + _FADE_BATCH])
            out["cleared"] += len(done)
        neo, _coll, _cls = _fade_stores()
        if neo is not None:
            for i in range(0, len(pitems), _FADE_BATCH):
                rows = [{"id": a, "c": b} for a, b in pitems[i:i + _FADE_BATCH]]
                try:
                    async with neo._driver.session() as ses:
                        res = await ses.run(
                            "UNWIND $rows AS r MATCH (m:Memory {id: r.id}) SET m.canonical_id = r.c "
                            "WITH m, r MATCH (c:Memory {id: r.c}) MERGE (m)-[:OCCURRENCE_OF]->(c) "
                            "RETURN count(m) AS n", {"rows": rows})
                        row = await res.single()
                        out["neo4j"] += int((row or {}).get("n", 0) or 0)
                except Exception as e:
                    out.setdefault("errors", []).append("neo4j: %s" % str(e)[:120])
                await asyncio.sleep(0)
            if scan["clear"]:
                try:
                    async with neo._driver.session() as ses:
                        await ses.run("UNWIND $ids AS id MATCH (m:Memory {id: id}) REMOVE m.canonical_id",
                                      {"ids": scan["clear"]})
                except Exception as e:
                    out.setdefault("errors", []).append("neo4j: %s" % str(e)[:120])
    except Exception as e:
        out["error"] = str(e)[:200]
    out["ms"] = round((time.monotonic() - t0) * 1000)
    out["finished_at"] = now_iso()
    r = _redis()
    if r is not None:
        try:
            await r.set(_CANON_RUN_KEY, json.dumps(out))
        except Exception:
            pass
    return out


@capability(
    "memory.canonical.mark",
    http_method="POST", http_path="/memory/canonical/mark", http_tags=["memory"],
    memory="off",
    description="Fold the stored copies of one text into one canonical record: the oldest record of each text (per "
                "record type) is the canonical one, every other copy is kept as its occurrence (linked "
                "OCCURRENCE_OF) and recall returns the canonical record once. Nothing is deleted or archived; new "
                "copies are folded in as they are written. Without confirm=true it only counts. Runs in the "
                "background, never 01:00-03:30. Inputs: confirm (bool). Output: {dry, scanned, would_point, "
                "canonicals, would_clear | started | running, last_run}.",
)
async def memory_canonical_mark(confirm: Any = False, trace_id=None) -> Dict[str, Any]:
    global _CANON_RUN_TASK
    r = _redis()
    last = None
    if r is not None:
        try:
            raw = await r.get(_CANON_RUN_KEY)
            last = json.loads(raw) if raw else None
        except Exception:
            last = None
    if _CANON_RUN_TASK is not None and not _CANON_RUN_TASK.done():
        return {"running": True, "last_run": last}
    if str(confirm).strip().lower() not in ("1", "true", "yes", "on"):
        pool = await _usage_pool()
        if pool is None:
            return {"error": "memory postgres not connected", "last_run": last}
        scan = await _canon_scan(pool)
        return {"dry": True, "scanned": scan["rows"], "would_key": len(scan["keys"]),
                "would_point": len(scan["point"]), "canonicals": scan["canonicals"],
                "would_clear": len(scan["clear"]), "last_run": last}
    if _in_backup_window():
        return {"refused": "the nightly backup runs 01:00-03:30 - folding waits", "last_run": last}
    _CANON_RUN_TASK = asyncio.create_task(_canon_apply())
    return {"started": True, "last_run": last}


_FADE_NIGHT = {"day": ""}


async def _fade_nightly_tick() -> None:
    """Once a day at about 04:00 local - after the nightly backup - when the user has switched nightly fading on."""
    global _FADE_RUN_TASK
    try:
        t = time.localtime()
        if t.tm_hour != 4:
            return
        day = time.strftime("%Y-%m-%d", t)
        if _FADE_NIGHT["day"] == day or not await _fade_auto_on():
            return
        if _FADE_RUN_TASK is not None and not _FADE_RUN_TASK.done():
            return
        _FADE_NIGHT["day"] = day
        _FADE_RUN_TASK = asyncio.create_task(_fade_apply(50000, None, "nightly"))
    except Exception as e:
        log.debug("fade nightly tick: %s", e)


schedule(_fade_nightly_tick, 1200, name="memory_fade_nightly", skip_in_sandbox=True, singleton=True)


@capability(
    "memory.context.deep",
    http_method="POST", http_path="/memory/context/deep", http_tags=["memory"],
    memory="off",
    description="The deep memory search a chat turn gets when it refers back ('last time', 'what did we decide'): "
                "every session and agent, the keyword and graph tier always and the vector tier only if it answers "
                "inside the budget. Read-only. Inputs: query, limit (int=8), budget_s (float=1.5). Output: {text, "
                "hits, vector, ms, fast_ms, fast_hits, found, items} - the block a turn would be given, how long each "
                "tier took, and the records in it.",
)
async def memory_context_deep(query: str, limit: int = 8, budget_s: float = 1.5, trace_id=None):
    try:
        limit = max(1, min(int(limit or 8), 30))
        budget_s = max(0.2, min(float(budget_s or 1.5), 10.0))
    except (TypeError, ValueError):
        limit, budget_s = 8, 1.5
    return await get_memory_context_deep(query, limit=limit, budget_s=budget_s)


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
    description="Return nodes + edges in one call. Use mode=session (+session_id), mode=recent, or mode=all.", contract={"presentation": {'kind': 'graph', 'form': 'vgraph'}}
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
    contract={"presentation": {"kind": "composite", "parts": [{"kind": "table", "map": {"rows": "categories"}, "title": "Categories"}, {"kind": "table", "map": {"rows": "edge_types"}, "title": "Edge types"}, {"kind": "table", "map": {"rows": "sessions"}, "title": "Recent sessions"}]}},
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
            _orch = (sys.modules.get("Vera.vera.capability_orchestration")
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
            _orch = (sys.modules.get("Vera.vera.capability_orchestration")
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
            _orch = (sys.modules.get("Vera.vera.capability_orchestration")
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
        from Vera.vera.capability_orchestration import ollama_generate
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
    log.info("vera_memory_hooks ready")


try:
    # Through the orchestrator, so a node worker skips it unless it is
    # on the worker allow-list (worker_placement_core).
    import Vera.vera.capability_orchestration as _co_start
    _co_start.start_at_import(_startup, "memory_hooks_startup")
except Exception:
    pass
