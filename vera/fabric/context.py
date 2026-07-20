"""
vera_context.py  —  Unified Context Loader, Stream Registry, Agent-Loop Cap
============================================================================

Three things in one module — all built on the existing @capability primitives,
no new infrastructure introduced.

1. Unified context loader
─────────────────────────
   build_context_prompt(message, *, attach_skills, attach_ontologies,
                        attach_caps, attach_dags, session_id)
   -> str   — a fully-assembled system-prompt fragment.

   Each `attach_*` argument can be:
     • ""        (or None / "off") → skip
     • "auto"                       → fuzzy search by `message` over the registry
     • "<id>,<id>,..."              → explicit ID/name list
     • "*"                          → include everything (use sparingly)

   Skills and ontologies are routed through the existing skills.active_context
   capability so the rendering rules already in vera_skills.py are honoured.

   Caps and DAGs are routed through CAP_INDEX.relevance_search and
   DAG_STORE.search respectively (both in vera_dag_store.py).

   Capability surfaces:
     context.assemble        — caller passes a query, gets the assembled prompt
     context.search_caps     — fuzzy semantic search over caps
     context.search_dags     — fuzzy semantic search over stored DAGs
     context.search_skills   — keyword/tag search over skills
     context.search_ontologies — keyword/tag search over ontologies

2. Stream registry
──────────────────
   Every long-running streaming call (LLM token stream, DAG step events,
   research jobs, etc.) can register itself so other caps and UI panels
   can subscribe to it without needing to know the Redis key layout.

   Capabilities:
     stream.register   — declare a stream { id, kind, source_cap, session_id }
     stream.list       — list active and recent streams
     stream.snapshot   — fetch the current accumulated payload
     stream.complete   — mark stream done and persist final output to fabric

   When a stream is registered with persist_full=True, on completion the
   full accumulated text is written to the data fabric (dataset
   "streams.<kind>") AND a memory record is created so the conversation
   stays connected to the rest of the graph.

3. dag.agent_loop capability
────────────────────────────
   Wraps the IDE-style ReAct agent flow as a reusable @capability. Given a
   goal and a tool list, the cap runs an LLM-controlled stepwise execution
   loop: at each cycle the LLM picks one capability + arguments, the cap is
   executed via the regular CAPABILITY_REGISTRY, the result preview is fed
   back, and the loop continues until the LLM emits {"action":"done"} or
   max_cycles is reached.

   This is structurally identical to capability_orchestration._stepwise_run
   but exposed as a callable cap (so DAGs can include an agent-loop node
   alongside deterministic steps) and with a richer manifest format.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP,                       # noqa
    CAPABILITY_REGISTRY,
    capability,
    emit_event,
    now_iso,
    ollama_generate,
)

log = logging.getLogger("vera.context")

# Lazy module accessors — these modules may not yet be loaded at import time.
def _redis():     return _orch.REDIS
def _skills():    return sys.modules.get("skills")
def _dag_store(): return sys.modules.get("dag_store")
def _fabric():    return sys.modules.get("data_fabric")
def _memory():    return sys.modules.get("memory")
def _hooks():     return sys.modules.get("memory_hooks")


# ─────────────────────────────────────────────────────────────────────────────
# 1.  UNIFIED CONTEXT LOADER
# ─────────────────────────────────────────────────────────────────────────────

# Caps that should never be surfaced to agents as "tools" — too low-level
# or self-referential.
_CONTEXT_CAP_BLACKLIST_GROUPS = {
    "obs", "syslog", "caps", "ollama", "system", "memory", "health",
    "ui", "echo", "debug", "context", "stream",
}


def _split_ids(spec: str) -> List[str]:
    if not spec:
        return []
    return [s.strip() for s in spec.split(",") if s.strip()]


import re as _re

def _extract_tool_action(action: dict, valid_tools: list) -> tuple:
    """Extract (tool_name, args, thought) from an LLM action dict, handling
    every common malformation: tool_use{name,input}, tool, capability, function,
    name, action:<capname>, action:tool_use+name+input, etc.

    Returns (tool, args, thought). tool may be empty string if unparseable.
    """
    if not isinstance(action, dict):
        return "", {}, ""
    thought = action.get("thought") or action.get("reasoning") or ""

    # 1. Canonical tool_use:{name,input}
    tu = action.get("tool_use") or action.get("tool_call")
    if isinstance(tu, dict) and tu.get("name"):
        return (str(tu["name"]).strip(),
                tu.get("input") or tu.get("args") or tu.get("arguments") or {},
                thought)
    if isinstance(tu, str) and tu.strip():
        return (tu.strip(),
                action.get("input") or action.get("args") or action.get("arguments") or {},
                thought)

    # 2. action:"tool_use" or "use_tool" with name/input at top level
    if action.get("action") in ("tool_use", "use_tool", "call", "invoke"):
        name = (action.get("name") or action.get("tool")
                or action.get("function") or action.get("cap") or "")
        if isinstance(name, str) and name.strip():
            return (name.strip(),
                    action.get("input") or action.get("args") or action.get("arguments") or {},
                    thought)

    # 3. action:"<cap.name>" with remaining keys = input
    act_val = action.get("action")
    if (isinstance(act_val, str) and act_val.strip() and "." in act_val
            and act_val in (valid_tools or [])):
        skip = {"action", "thought", "reasoning", "summary", "explanation"}
        return (act_val,
                {k: v for k, v in action.items() if k not in skip},
                thought)

    # 4. Standard fields: tool / capability / function / name
    name = (action.get("tool") or action.get("capability")
            or action.get("function") or action.get("name") or "")
    if isinstance(name, str) and name.strip():
        return (name.strip(),
                action.get("args") or action.get("arguments") or action.get("input") or {},
                thought)

    return "", {}, thought


def _strip_think(raw: str) -> tuple:
    """Strip <think>...</think> from LLM output, returning (clean, think_text).

    Handles several patterns:
      1. <think>reasoning</think> {"tool":...}   — standard
      2. <think>reasoning {"tool":...} </think>  — JSON inside think block
      3. reasoning</think> {"tool":...}          — unclosed opening, close before JSON
      4. <think>reasoning</think>\n{"tool":...}  — think then JSON
    """
    if not raw:
        return raw, ""
    # If there's no think tag at all, return as-is
    if "<think>" not in raw and "</think>" not in raw:
        return raw, ""

    think_parts = []

    # Case: properly paired <think>...</think> blocks
    for m in _re.finditer(r"<think>(.*?)</think>", raw, _re.DOTALL):
        think_parts.append(m.group(1).strip())
    clean = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL)

    # Case: unclosed <think> at the end (model didn't close it)
    clean = _re.sub(r"<think>.*$", "", clean, flags=_re.DOTALL)

    # Case: </think> appears without a matching <think> — the model's
    # think block started before our capture. Everything before </think>
    # is think text; everything after is the real response.
    if "</think>" in clean:
        parts = clean.split("</think>", 1)
        pre_think = parts[0].strip()
        post_think = parts[1].strip() if len(parts) > 1 else ""
        if pre_think:
            think_parts.append(pre_think)
        clean = post_think

    clean = clean.strip()

    # If stripping removed everything but there's a JSON object in the
    # think text itself, extract it — the model put the action inside
    # the think block.
    if not clean or (clean and not clean.startswith("{")):
        all_think = "\n\n".join(think_parts)
        json_match = _re.search(r'(\{[^{}]*"(?:tool|action)"[^{}]*\{.*?\}[^{}]*\})', all_think, _re.DOTALL)
        if not json_match:
            json_match = _re.search(r'(\{"(?:thought|tool|action)".*\})\s*$', all_think, _re.DOTALL)
        if json_match:
            clean = json_match.group(1).strip()
            # Remove the extracted JSON from think text
            think_text = all_think[:json_match.start()].strip()
            return clean, think_text

    return clean, "\n\n".join(think_parts)


def _local_is_long_running(cap_name: str) -> bool:
    _LR = ("research.", "ml.", "ml_training.", "exec.bash", "exec.python", "exec.shell", "exec.run")
    return any(cap_name.startswith(p) for p in _LR)


async def _maybe_expand_template(template: str, **kwargs) -> str:
    """Lazy-call the dag_workshop template expander."""
    if not template:
        return ""
    dw = None
    import importlib, sys
    for modpath in ("Vera.vera.dag.dag_workshop_capabilities",
                    "dag_workshop_capabilities",
                    "Orchestration.dag_workshop_capabilities"):
        try:
            dw = importlib.import_module(modpath)
            break
        except Exception:
            continue
    if dw is None:
        for name, mod in list(sys.modules.items()):
            if name.endswith("dag_workshop_capabilities") and mod is not None:
                dw = mod
                break
    if dw is None:
        return template
    try:
        fn = getattr(dw, "_expand_prompt_template", None)
        if fn:
            return fn(template, **kwargs)
    except Exception as e:
        log.debug("template expand failed: %s", e)
    return template


async def _maybe_arg_recover(*, cap_name: str,
                                  failed_args: Dict[str, Any],
                                  error_text: str,
                                  model: str = "", instance_id: str = "",
                                  prefer_gpu: bool = True,
                                  max_attempts: int = 2,
                                  call_tool: Any = None,
                                  session_id: str = "",
                                  trace_id: str = "",
                                  cycle: int = 0,
                                  stream_id: str = "",
                                  enabled: bool = True) -> Optional[Dict[str, Any]]:
    """Lazy-import + invoke _attempt_arg_recovery from dag_workshop_capabilities.

    Returns the recovery result dict or None if recovery is disabled / not
    a recoverable error / module unavailable.
    """
    if not enabled or max_attempts <= 0:
        return None
    if not error_text:
        return None
    dw = None
    import importlib, sys
    for modpath in ("Vera.vera.dag.dag_workshop_capabilities",
                    "dag_workshop_capabilities",
                    "Orchestration.dag_workshop_capabilities"):
        try:
            dw = importlib.import_module(modpath)
            break
        except Exception:
            continue
    if dw is None:
        for name, mod in list(sys.modules.items()):
            if name.endswith("dag_workshop_capabilities") and mod is not None:
                dw = mod
                break
    if dw is None:
        return None
    is_arg_err = getattr(dw, "_is_arg_error", None)
    attempt_recovery = getattr(dw, "_attempt_arg_recovery", None)
    if not is_arg_err or not attempt_recovery:
        return None
    if not is_arg_err(error_text):
        return None
    try:
        return await attempt_recovery(
            cap_name=cap_name, failed_args=failed_args or {},
            error_text=error_text,
            model=model, instance_id=instance_id, prefer_gpu=prefer_gpu,
            max_attempts=int(max_attempts),
            call_tool=call_tool,
            session_id=session_id, trace_id=trace_id,
            emit_fn=emit_event, cycle=cycle, stream_id=stream_id,
        )
    except Exception as e:
        log.debug("arg recovery failed for %s: %s", cap_name, e)
        return None


async def _maybe_await_long_running(*, cap_name: str, result: Any,
                                       session_id: str = "",
                                       trace_id: str = "",
                                       cycle: int = 0,
                                       stream_id: str = "",
                                       max_wait_secs: float = 1800.0,
                                       enabled: bool = True) -> Any:
    """Lazy-import + invoke _universal_await_job from dag_workshop_capabilities.

    Returns the result possibly augmented with the awaited payload. If the
    cap didn't return a job_id, or awaiting is disabled, returns result
    unchanged.
    """
    if not enabled or not isinstance(result, dict):
        return result
    dw = None
    import importlib, sys
    # Try the most likely module paths in order
    for modpath in ("Vera.vera.dag.dag_workshop_capabilities",
                    "dag_workshop_capabilities",
                    "Orchestration.dag_workshop_capabilities"):
        try:
            dw = importlib.import_module(modpath)
            break
        except Exception:
            continue
    if dw is None:
        # Last resort: scan sys.modules for it (already-loaded by main app)
        for name, mod in list(sys.modules.items()):
            if name.endswith("dag_workshop_capabilities") and mod is not None:
                dw = mod
                break
    if dw is None:
        log.debug("await: dag_workshop_capabilities not importable")
        return result
    universal_await = getattr(dw, "_universal_await_job", None)
    detect_job_id   = getattr(dw, "_detect_job_id", None)
    if universal_await is None or detect_job_id is None:
        log.debug("await: missing helpers in dag_workshop_capabilities")
        return result
    if not detect_job_id(result):
        return result
    try:
        awaited = await universal_await(
            cap_name=cap_name, immediate=result,
            session_id=session_id, trace_id=trace_id,
            cycle=cycle, stream_id=stream_id,
            max_wait_secs=float(max_wait_secs),
        )
        return awaited if isinstance(awaited, dict) else result
    except Exception as e:
        log.debug("universal await failed for %s: %s", cap_name, e)
        return result


_THINKING_MODEL_HINTS = ("qwen3", "qwen-3", "qwq", "deepseek-r1", "r1-distill",
                          "marco-o1", "skyt1", "phi4-reasoning")

def _is_thinking_model(model: str) -> bool:
    if not model:
        return False
    m = model.lower()
    return any(h in m for h in _THINKING_MODEL_HINTS)


async def _safe_ollama_generate(prompt: str, *, system: str = "",
                                  json_mode: bool = True,
                                  model: str = "", instance_id: str = "",
                                  prefer_gpu: bool = True) -> str:
    """ollama_generate wrapper that handles thinking-model quirks.

    Thinking models (qwen3, deepseek-r1, qwq, etc.) often return EMPTY
    responses when forced to format=json because their training conflicts
    with strict-JSON output (they want to emit <think> first).

    Strategy:
      1. If model is a known thinking model → skip json_mode, parse with strip
      2. Otherwise call with requested json_mode
      3. If response is empty/whitespace, retry once WITHOUT json_mode

    Returns the raw text (possibly containing <think> blocks — caller strips).
    """
    use_json = bool(json_mode) and not _is_thinking_model(model)
    raw = await ollama_generate(
        prompt, system=system, json_mode=use_json,
        model=model or None,
        instance_id=instance_id or None,
        prefer_gpu=bool(prefer_gpu),
    )
    cleaned = (raw or "").strip()
    # Empty or implausibly short → retry without json_mode
    if not cleaned or len(cleaned) < 4:
        try:
            raw = await ollama_generate(
                prompt + ("\n\nRespond with a single JSON object and nothing else."
                           if json_mode else ""),
                system=system, json_mode=False,
                model=model or None,
                instance_id=instance_id or None,
                prefer_gpu=bool(prefer_gpu),
            )
        except Exception:
            pass
    return raw or ""


async def _resolve_skills_block(spec: str) -> Tuple[str, List[str]]:
    """Returns (block_text, skill_names). spec may be 'auto', '*', '' or csv ids."""
    sk = _skills()
    if not sk or not spec or spec.lower() in ("off", "none"):
        return "", []

    SKILLS = getattr(sk, "SKILLS", {})
    if not SKILLS:
        return "", []

    # Resolve which ids to use
    if spec == "*":
        ids = [sid for sid, s in SKILLS.items() if s.get("enabled", True)]
    elif spec.lower() == "auto":
        # All enabled — auto means "use everything the user has turned on".
        # We could narrow by query later, but skills are typically curated.
        ids = [sid for sid, s in SKILLS.items() if s.get("enabled", True)]
    else:
        ids = [i for i in _split_ids(spec) if i in SKILLS]

    if not ids:
        return "", []

    # Use the existing skills.active_context capability so render rules are
    # consistent with vera_skills.py
    cap = CAPABILITY_REGISTRY.get("skills.active_context")
    if cap:
        try:
            res = await cap["func"](skill_ids=",".join(ids))
            # SKILLS ONLY. `combined` also carries every enabled ontology (the
            # cap renders all ontologies when no ontology_ids filter is given),
            # which would leak unrelated ontologies (test/threat-intel/…) into
            # any prompt that asked for skills but not ontologies. Ontologies
            # come exclusively through _resolve_ontologies_block(attach_ontologies).
            text = res.get("skill_context")
            if text is None:
                text = res.get("combined") or res.get("system_prompt") or ""
            names = [SKILLS[i].get("name", i) for i in ids if i in SKILLS]
            return text, names
        except Exception as e:
            log.debug("skills.active_context call failed: %s", e)

    # Fallback: build it ourselves
    parts = []
    names = []
    for sid in ids:
        s = SKILLS.get(sid) or {}
        if not s.get("enabled", True):
            continue
        parts.append(s.get("content", ""))
        names.append(s.get("name", sid))
    return "\n\n".join(p for p in parts if p), names


async def _resolve_ontologies_block(spec: str) -> Tuple[str, List[str]]:
    sk = _skills()
    if not sk or not spec or spec.lower() in ("off", "none"):
        return "", []

    ONTS = getattr(sk, "ONTOLOGIES", {})
    if not ONTS:
        return "", []

    if spec == "*" or spec.lower() == "auto":
        ids = [oid for oid, o in ONTS.items() if o.get("enabled", True)]
    else:
        ids = [i for i in _split_ids(spec) if i in ONTS]

    if not ids:
        return "", []

    sections = []
    names = []
    for oid in ids:
        ont = ONTS.get(oid) or {}
        if not ont.get("enabled", True):
            continue
        names.append(ont.get("name", oid))
        parts = [f"## Ontology: {ont.get('name','?')} (domain: {ont.get('domain','general')})"]
        if ont.get("context_hints"):
            parts.append(f"Context: {ont['context_hints']}")
        ents = ont.get("entities") or []
        if ents:
            parts.append("Entities:\n" + "\n".join(
                f"  - {e.get('name','?')}: {e.get('description','')}"
                + (f" (attrs: {', '.join(e.get('attributes',[]))})" if e.get("attributes") else "")
                for e in ents
            ))
        rels = ont.get("relationships") or []
        if rels:
            parts.append("Relationships:\n" + "\n".join(
                f"  - {r.get('from','?')} --[{r.get('label','REL')}]--> {r.get('to','?')}"
                for r in rels
            ))
        rules = ont.get("processing_rules") or []
        if rules:
            parts.append("Processing rules:\n" + "\n".join(
                f"  [{r.get('priority',0)}] {r.get('trigger','')} → {r.get('action','')}"
                for r in sorted(rules, key=lambda x: x.get("priority", 0), reverse=True)
            ))
        sections.append("\n".join(parts))

    return "\n\n".join(sections), names


async def _resolve_caps_block(spec: str, query: str, top_k: int = 18) -> Tuple[str, List[str]]:
    if not spec or spec.lower() in ("off", "none"):
        return "", []

    ds = _dag_store()
    if spec == "*":
        names = [
            n for n in CAPABILITY_REGISTRY.keys()
            if n.split(".")[0] not in _CONTEXT_CAP_BLACKLIST_GROUPS
        ][:top_k * 2]
    elif spec.lower() == "auto":
        if ds and getattr(ds, "CAP_INDEX", None):
            try:
                results = await ds.CAP_INDEX.relevance_search(query or "", top_k=top_k * 2)
                names = [
                    n for n, _s in results
                    if n.split(".")[0] not in _CONTEXT_CAP_BLACKLIST_GROUPS
                ][:top_k]
            except Exception as e:
                log.debug("cap relevance_search failed: %s", e)
                names = []
        else:
            names = []
    else:
        wanted = _split_ids(spec)
        names = [n for n in wanted if n in CAPABILITY_REGISTRY]

    if not names:
        return "", []

    # Build signatures via dag_store.CAP_INDEX if available, else inline
    if ds and getattr(ds, "CAP_INDEX", None):
        sigs = "\n".join(ds.CAP_INDEX.cap_signature(n) for n in names if n in CAPABILITY_REGISTRY)
    else:
        sigs = "\n".join(_inline_cap_sig(n) for n in names if n in CAPABILITY_REGISTRY)

    block = "Available capabilities (use [[cap:name {args}]] to invoke):\n" + sigs
    return block, names


def _inline_cap_sig(name: str) -> str:
    cap = CAPABILITY_REGISTRY.get(name, {})
    props = cap.get("schema", {}).get("properties", {}) or {}
    req   = set(cap.get("schema", {}).get("required", []))
    params = ", ".join(
        _orch._format_param_sig(p, v, req)
        for p, v in props.items() if p not in ("trace_id",)
    )
    desc = (cap.get("description") or "")[:120]
    return f"  {name}({params}) — {desc}"


async def _resolve_dags_block(spec: str, query: str, top_k: int = 8) -> Tuple[str, List[Dict]]:
    """Returns (block_text, list_of_dag_summaries)."""
    if not spec or spec.lower() in ("off", "none"):
        return "", []

    ds = _dag_store()
    if not ds or not getattr(ds, "DAG_STORE", None):
        return "", []

    DAG_STORE = ds.DAG_STORE
    matches: List[Dict] = []

    if spec == "*":
        try:
            recs = await DAG_STORE.list_all(include_archived=False)
            matches = [{"rec": r, "score": 1.0} for r in recs[:top_k]]
        except Exception:
            matches = []
    elif spec.lower() == "auto":
        try:
            results = await DAG_STORE.search(query or "", limit=top_k)
            matches = [{"rec": r, "score": s} for r, s in results]
        except Exception as e:
            log.debug("DAG_STORE.search failed: %s", e)
    else:
        wanted = _split_ids(spec)
        for w in wanted:
            r = await DAG_STORE.get(w) or await DAG_STORE.get_by_name(w)
            if r:
                matches.append({"rec": r, "score": 1.0})

    if not matches:
        return "", []

    summaries: List[Dict] = []
    lines = ["Available pre-built DAG workflows (invoke with [[suggest:dag {\"dag\":...}]] or call dag.store_run):"]
    for m in matches:
        r = m["rec"]
        steps = ", ".join((r.nodes_summary or [])[:5])
        if len(r.nodes_summary or []) > 5:
            steps += ", …"
        lines.append(f"  • {r.name} — {(r.description or '')[:100]}  [{steps}]")
        summaries.append({
            "id":    r.id,
            "name":  r.name,
            "desc":  r.description,
            "tags":  r.tags,
            "steps": r.nodes_summary,
            "score": m["score"],
        })
    return "\n".join(lines), summaries


async def _resolve_related_qa_block(
    spec:               str,
    message:            str,
    session_id:         str,
    agent_name:         str,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Resolve the related-QA spec into (block_text, pairs).

    spec values:
        ''        -> off
        'auto'    -> default limit (4 pairs)
        '<int>'   -> custom limit
    Returns ("", []) when no second-order recall is available or no
    matches are found.
    """
    if not spec or spec.lower() in ("off", "none", "false", "0"):
        return "", []

    try:
        limit = int(spec) if spec.strip().isdigit() else 4
    except Exception:
        limit = 4

    qa_cap = CAPABILITY_REGISTRY.get("context.related_qa_block") \
              or CAPABILITY_REGISTRY.get("memory.recall_2nd_order")
    if not qa_cap or not qa_cap.get("func"):
        return "", []

    try:
        result = await qa_cap["func"](
            query           = message,
            limit           = limit,
            graph_depth     = 1,
            neighbours_per  = 2,
            session_id      = "",
            exclude_session = session_id or "",
            min_score       = 0.4,
            max_chars       = 2400,
        )
    except Exception as e:
        log.debug("related_qa_block call failed: %s", e)
        return "", []

    if not isinstance(result, dict):
        return "", []

    block = result.get("block") or ""
    pairs = result.get("pairs") or []

    # If we got the lower-level recall_2nd_order cap (no 'block' field),
    # render the block ourselves so callers always see consistent shape.
    if not block and pairs:
        lines = ["## Related past Q&A (from memory)"]
        used = len(lines[0])
        cap_chars = 2400
        for p in pairs:
            chunk_lines = [
                "",
                f"### Prior question (similarity {p.get('score', 0):.2f})",
                f"> {(p.get('q') or '').strip()[:400]}",
                f"**Then-answered:** {(p.get('a') or '').strip()[:600]}",
            ]
            nbrs = p.get("neighbours") or []
            if nbrs:
                chunk_lines.append("**Linked knowledge:**")
                for n in nbrs:
                    rel = (n.get("relation") or "RELATED").upper()
                    txt = (n.get("text") or "").strip()[:240]
                    if txt:
                        chunk_lines.append(f"  - [{rel}] {txt}")
            chunk = "\n".join(chunk_lines)
            if used + len(chunk) > cap_chars:
                lines.append("\n_(more matches truncated)_")
                break
            lines.append(chunk)
            used += len(chunk)
        block = "\n".join(lines)

    return block, pairs


async def _resolve_cap_ontology_block(spec: str) -> str:
    """Cap-mesh (cap_ontology) planner fragment.

    spec: '' (skip) | '*' / 'auto' (all caps) | CSV of cap names — normally
    the agent's domain_caps, so the fragment covers relations between the
    caps the agent can call plus edges to adjacent hidden caps."""
    if not spec:
        return ""
    co = sys.modules.get("cap_ontology")
    if not co or not hasattr(co, "build_ontology_system_prompt_fragment"):
        return ""
    caps = ["*"] if spec.strip() in ("*", "auto") else _split_ids(spec)
    if not caps:
        return ""
    try:
        return await co.build_ontology_system_prompt_fragment(caps) or ""
    except Exception as e:
        log.debug("context cap_ontology block: %s", e)
        return ""


def _resolve_session_notes_block(session_id: str, agent_name: str) -> str:
    """Session-memory notes fragment: the chat-session note plus (when an
    agent is named) the agent-level note. Empty when neither has content."""
    sn = sys.modules.get("session_notes")
    if not sn or not hasattr(sn, "build_notes_context"):
        return ""
    pairs = []
    if session_id:
        pairs.append(("chat", session_id))
    if agent_name:
        pairs.append(("agent", agent_name))
    if not pairs:
        return ""
    try:
        return sn.build_notes_context(pairs) or ""
    except Exception as e:
        log.debug("context session_notes block: %s", e)
        return ""


async def build_context_prompt(
    message:            str,
    *,
    attach_skills:      str = "",
    attach_ontologies:  str = "",
    attach_caps:        str = "",
    attach_dags:        str = "",
    attach_memory:      bool = False,
    attach_related_qa:  str  = "",
    attach_cap_ontology: str = "",
    attach_session_notes: bool = False,
    session_id:         str  = "",
    memory_limit:       int  = 5,
    memory_tags:        str  = "",
    agent_name:         str  = "",
) -> Dict[str, Any]:
    """Assemble a system-prompt fragment from skills, ontologies, caps, DAGs,
    optional first-order memory, and optional second-order related-QA recall.

    Returns:
        {
          "system_prompt":    "<combined text>",
          "skills":           ["name", ...],
          "ontologies":       ["name", ...],
          "caps":             ["cap.name", ...],
          "dags":             [ { id, name, ... } ],
          "memory_block":     "<text or empty>",
          "related_qa":       [ {q, a, score, neighbours: [...]}, ... ],
          "related_qa_count": int,
        }
    """
    skills_block, skill_names    = await _resolve_skills_block(attach_skills)
    onts_block,   ont_names      = await _resolve_ontologies_block(attach_ontologies)
    caps_block,   cap_names      = await _resolve_caps_block(attach_caps, message)
    dags_block,   dag_summaries  = await _resolve_dags_block(attach_dags, message)

    # Optional first-order memory injection
    mem_block = ""
    if attach_memory and session_id:
        try:
            mh = _hooks()
            if mh:
                mem_block = await mh.get_agent_memory_context(
                    session_id  = session_id,
                    query       = message,
                    agent_name  = agent_name,
                    limit       = memory_limit,
                    tags        = _split_ids(memory_tags) or None,
                ) or ""
        except Exception as e:
            log.debug("context memory inject: %s", e)

    # Optional second-order recall: similar past questions → paired
    # answers → graph-neighbour knowledge.
    qa_block, qa_pairs = await _resolve_related_qa_block(
        attach_related_qa, message, session_id, agent_name,
    )

    # Optional capability-mesh relations (cap_ontology) for the agent's caps
    cap_ont_block = await _resolve_cap_ontology_block(attach_cap_ontology)

    # Optional session-memory notes (chat session + agent scoped)
    notes_block = ""
    if attach_session_notes:
        notes_block = _resolve_session_notes_block(session_id, agent_name)

    parts = [p for p in [skills_block, onts_block, caps_block, dags_block,
                          cap_ont_block, notes_block, mem_block, qa_block] if p]
    combined = "\n\n".join(parts)

    return {
        "system_prompt":     combined,
        "skills":            skill_names,
        "ontologies":        ont_names,
        "caps":              cap_names,
        "dags":              dag_summaries,
        "memory_block":      mem_block,
        "cap_ontology_block": cap_ont_block,
        "session_notes_block": notes_block,
        "related_qa":        qa_pairs,
        "related_qa_count":  len(qa_pairs),
    }


@capability(
    "context.assemble",
    http_method="POST", http_path="/context/assemble", http_tags=["context"],
    memory="off",
    description=(
        "Assemble a unified context block from skills, ontologies, capability "
        "signatures, stored DAGs, optional first-order memory, and optional "
        "second-order related-QA recall. Each attach_* argument accepts: '' "
        "(skip), 'auto' (semantic search by message), '*' (include all where "
        "applicable), or a comma-separated explicit list of ids/names. "
        "attach_related_qa accepts '' (off), 'auto' (4 pairs), or a digit. "
        "attach_cap_ontology accepts '' (off), '*' (all caps) or a CSV of cap "
        "names (usually the agent's domain_caps) — injects capability-mesh "
        "relations from cap_ontology. attach_session_notes (bool) injects the "
        "agent-maintained session memory notes for session_id/agent_name."
    ),
)
async def cap_context_assemble(
    message:            str,
    attach_skills:      str  = "",
    attach_ontologies:  str  = "",
    attach_caps:        str  = "",
    attach_dags:        str  = "",
    attach_memory:      bool = False,
    attach_related_qa:  str  = "",
    attach_cap_ontology: str = "",
    attach_session_notes: bool = False,
    session_id:         str  = "",
    memory_limit:       int  = 5,
    memory_tags:        str  = "",
    agent_name:         str  = "",
    trace_id=None,
):
    res = await build_context_prompt(
        message,
        attach_skills     = attach_skills,
        attach_ontologies = attach_ontologies,
        attach_caps       = attach_caps,
        attach_dags       = attach_dags,
        attach_memory     = attach_memory,
        attach_related_qa = attach_related_qa,
        attach_cap_ontology = attach_cap_ontology,
        attach_session_notes = bool(attach_session_notes),
        session_id        = session_id,
        memory_limit      = memory_limit,
        memory_tags       = memory_tags,
        agent_name        = agent_name,
    )
    res["preview"] = res["system_prompt"][:600] + ("…" if len(res["system_prompt"]) > 600 else "")
    return res


@capability(
    "context.search_caps", memory="off",
    http_method="POST", http_path="/context/search_caps", http_tags=["context"],
    description="Semantic search over the capability registry to find tools relevant to a query. "
                "WHEN TO USE: when you need a tool but don't know its exact name, or want to discover "
                "capabilities related to a topic (e.g. 'what caps handle email?', 'find me image generation tools'). "
                "HOW TO USE: context.search_caps(query='description of what you need', top_k=10). "
                "Params: query (str!), top_k (int default 12). "
                "Output: {results: [{name, signature, score}], count} — ranked by relevance. "
                "NOTE: caps.search does the same thing with a slightly different interface.",
)
async def cap_context_search_caps(query: str, top_k: int = 12, trace_id=None):
    ds = _dag_store()
    if not ds or not getattr(ds, "CAP_INDEX", None):
        return {"results": [], "error": "dag_store.CAP_INDEX not available"}
    try:
        results = await ds.CAP_INDEX.relevance_search(query or "", top_k=top_k)
    except Exception as e:
        return {"results": [], "error": str(e)}
    out = []
    for name, score in results:
        out.append({
            "name":      name,
            "score":     round(score, 3),
            "signature": ds.CAP_INDEX.cap_signature(name),
            "category":  ds.CAP_INDEX._index.get(name, {}).get("category"),
            "tags":      ds.CAP_INDEX._index.get(name, {}).get("tags", []),
        })
    return {"results": out, "count": len(out), "query": query}


@capability(
    "context.search_dags", memory="off",
    http_method="POST", http_path="/context/search_dags", http_tags=["context"],
    description="Fuzzy semantic search over the DAG store. Returns ranked DAG "
                "summaries (name, description, step list, score).",
)
async def cap_context_search_dags(query: str, top_k: int = 8, trace_id=None):
    ds = _dag_store()
    if not ds or not getattr(ds, "DAG_STORE", None):
        return {"results": [], "error": "DAG_STORE not available"}
    try:
        results = await ds.DAG_STORE.search(query or "", limit=top_k)
    except Exception as e:
        return {"results": [], "error": str(e)}
    out = []
    for r, s in results:
        out.append({
            "id":     r.id,
            "name":   r.name,
            "desc":   r.description,
            "tags":   r.tags,
            "steps":  r.nodes_summary,
            "score":  round(s, 3),
        })
    return {"results": out, "count": len(out), "query": query}


@capability(
    "context.search_skills", memory="off",
    http_method="POST", http_path="/context/search_skills", http_tags=["context"],
    description="Keyword/tag search over registered skills.",
)
async def cap_context_search_skills(query: str = "", tag: str = "",
                                     enabled_only: bool = True, trace_id=None):
    sk = _skills()
    if not sk:
        return {"results": [], "error": "skills module not loaded"}
    SKILLS = getattr(sk, "SKILLS", {})
    q = (query or "").lower()
    out = []
    for s in SKILLS.values():
        if enabled_only and not s.get("enabled", True):
            continue
        if tag and tag not in s.get("tags", []):
            continue
        hay = (s.get("name", "") + " " + s.get("description", "") + " "
               + s.get("content", "")[:400] + " " + " ".join(s.get("tags", []))).lower()
        if q and q not in hay:
            continue
        out.append({
            "id":          s["id"],
            "name":        s.get("name", ""),
            "description": s.get("description", ""),
            "type":        s.get("type", ""),
            "tags":        s.get("tags", []),
            "preview":     (s.get("content", "") or "")[:200],
        })
    return {"results": out, "count": len(out), "query": query}


@capability(
    "context.search_ontologies", memory="off",
    http_method="POST", http_path="/context/search_ontologies", http_tags=["context"],
    description="Keyword/domain/tag search over registered ontologies.",
)
async def cap_context_search_ontologies(query: str = "", domain: str = "", tag: str = "",
                                         enabled_only: bool = True, trace_id=None):
    sk = _skills()
    if not sk:
        return {"results": [], "error": "skills module not loaded"}
    ONTS = getattr(sk, "ONTOLOGIES", {})
    q = (query or "").lower()
    out = []
    for o in ONTS.values():
        if enabled_only and not o.get("enabled", True):
            continue
        if domain and o.get("domain") != domain:
            continue
        if tag and tag not in o.get("tags", []):
            continue
        hay = (o.get("name", "") + " " + o.get("description", "") + " "
               + o.get("context_hints", "") + " " + " ".join(o.get("tags", []))).lower()
        if q and q not in hay:
            continue
        out.append({
            "id":            o["id"],
            "name":          o.get("name", ""),
            "domain":        o.get("domain", "general"),
            "description":   o.get("description", ""),
            "tags":          o.get("tags", []),
            "context_hints": (o.get("context_hints", "") or "")[:200],
        })
    return {"results": out, "count": len(out), "query": query}


# ─────────────────────────────────────────────────────────────────────────────
# 1b.  UNIFIED RECALL  —  entity-crawl + worldview + related-QA breadth
# ─────────────────────────────────────────────────────────────────────────────
#
# context.assemble builds the skills/ontologies/DAGs system-prompt fragment.
# context.recall is the complementary "what does the rest of the system already
# know about this query?" surface. It widens recall beyond the chat panel's own
# memory + fabric vector search by, concurrently:
#   • crawling the data-fabric entity graph out from seed records,
#   • pulling latent-space neighbours (and optionally predicted-next-concept
#     members) from the JEPA worldview model, and
#   • recalling the paired answer + graph neighbours for similar past questions.
# Every record-backed result is keyed by its canonical id and deduped across all
# of these sources, so a record surfaced by several systems appears once with
# merged provenance. The chat Context tab calls this additively — it keeps its
# own memory/fabric fetches and merges these extra results into CTX_NODES.

_RECALL_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_RECALL_CACHE_TTL = 25.0    # seconds — covers rapid on-typing + on-send fetches
_RECALL_CACHE_MAX = 128


def _recall_cache_get(key: str) -> Optional[Dict[str, Any]]:
    hit = _RECALL_CACHE.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > _RECALL_CACHE_TTL:
        _RECALL_CACHE.pop(key, None)
        return None
    return val


def _recall_cache_put(key: str, val: Dict[str, Any]):
    if len(_RECALL_CACHE) >= _RECALL_CACHE_MAX:
        oldest = min(_RECALL_CACHE.items(), key=lambda kv: kv[1][0])[0]
        _RECALL_CACHE.pop(oldest, None)
    _RECALL_CACHE[key] = (time.time(), val)


async def _recall_entity_crawl(seed_ids: List[str], query: str,
                                top_k: int, fabric_dataset: str,
                                max_entity_fanout: int = 30,
                                ) -> Tuple[List[Dict], List[Dict]]:
    """Crawl the data-fabric entity graph out from seed records.

    Finds the :Entity nodes mentioned in the seed records, then pulls the OTHER
    FabricRecords those entities are mentioned in — i.e. records that share an
    entity with the seeds. Returns (results, edges) where edges are ENTITY_LINK
    seed→crawled. Mirrors the entity-link pattern in
    worldview_jepa._fetch_entity_links. Best-effort: returns ([],[]) if the
    fabric Neo4j graph isn't available.
    """
    fab = _fabric()
    if not fab:
        return [], []
    neo = getattr(fab, "FABRIC_NEO", None)
    pg  = getattr(fab, "FABRIC_PG", None)
    if not neo or not getattr(neo, "available", False):
        return [], []

    # If the caller didn't supply seeds, do a quick fabric query to find some.
    seeds = [s for s in (seed_ids or []) if s]
    if not seeds and hasattr(fab, "execute_query"):
        try:
            q = {"text": query, "vector": query, "top_k": max(6, top_k),
                 "include_data": False}
            if fabric_dataset:
                q["dataset_id"] = fabric_dataset
            fq = await fab.execute_query(q)
            seeds = [r["id"] for r in (fq.get("results") or []) if r.get("id")][:8]
        except Exception as e:
            log.debug("recall entity seed query: %s", e)
    if not seeds:
        return [], []

    # LIMIT is concatenated (not parameterised) to match the rest of the
    # codebase and stay compatible across Neo4j versions.
    lim = max(1, int(top_k) * 4)
    try:
        rows = await neo.query(
            "MATCH (e:Entity)-[:MENTIONED_IN]->(r:FabricRecord) "
            "WHERE r.id IN $seeds "
            "WITH e, collect(DISTINCT r.id) AS seed_recs "
            "MATCH (e)-[:MENTIONED_IN]->(r2:FabricRecord) "
            "WHERE NOT r2.id IN $seeds "
            "RETURN e.id AS entity, e.name AS entity_name, "
            "       seed_recs[0] AS seed_id, r2.id AS rid "
            "LIMIT " + str(lim),
            {"seeds": seeds},
        )
    except Exception as e:
        log.debug("recall entity crawl query: %s", e)
        return [], []
    if rows and isinstance(rows[0], dict) and rows[0].get("error"):
        log.debug("recall entity crawl: %s", rows[0]["error"])
        return [], []

    # Bound per-entity fanout so a hub entity can't flood the result set.
    by_entity: Dict[str, int] = {}
    crawled: Dict[str, Dict] = {}     # rid -> {entities:set, seed_id}
    for row in rows:
        eid = row.get("entity") or ""
        rid = row.get("rid") or ""
        if not rid:
            continue
        by_entity[eid] = by_entity.get(eid, 0) + 1
        if by_entity[eid] > max_entity_fanout:
            continue
        c = crawled.setdefault(rid, {"entities": set(),
                                     "seed_id": row.get("seed_id") or ""})
        if row.get("entity_name"):
            c["entities"].add(row["entity_name"])

    if not crawled:
        return [], []

    # Hydrate record text from Postgres.
    rec_map: Dict[str, Any] = {}
    if pg and getattr(pg, "available", False):
        try:
            rec_map = await pg.get_by_ids(list(crawled.keys())[:top_k * 2]) or {}
        except Exception as e:
            log.debug("recall entity hydrate: %s", e)

    results, edges = [], []
    for rid, meta in crawled.items():
        rec  = rec_map.get(rid)
        text = (getattr(rec, "text", "") or "")[:300] if rec else ""
        ents = sorted(meta["entities"])
        # Score scales with how many shared entities connect it to the seeds.
        score = min(0.9, 0.45 + 0.08 * len(ents))
        results.append({
            "id": rid, "text": text, "source": "entity",
            "score": round(score, 4),
            "dataset_id": getattr(rec, "dataset_id", "") if rec else "",
            "entities": ents, "via": "entity_crawl",
        })
        if meta["seed_id"] and meta["seed_id"] != rid:
            edges.append({"from": meta["seed_id"], "to": rid,
                          "relation": "ENTITY_LINK"})
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top_k], edges


async def _recall_worldview(query: str, top_k: int, fabric_dataset: str,
                             use_rollout: bool, rollout_steps: int,
                             ) -> Tuple[List[Dict], List[Dict]]:
    """Best-effort worldview augmentation. Silently returns ([],[]) when the
    JEPA model / index isn't ready (the caps guard that themselves)."""
    results: List[Dict] = []

    wv_query = CAPABILITY_REGISTRY.get("worldview.query")
    if wv_query and wv_query.get("func"):
        try:
            res = await wv_query["func"](text=query, top_k=top_k,
                                          dataset_id=fabric_dataset or "")
            for r in (res or {}).get("results", []) or []:
                if not r.get("id"):
                    continue
                results.append({
                    "id": r["id"], "text": (r.get("text") or "")[:300],
                    "source": "worldview",
                    "score": round(float(r.get("score", 0.5)), 4),
                    "dataset_id": r.get("dataset_id", ""),
                    "concept": r.get("concept", -1),
                    "concept_label": r.get("concept_label", ""),
                    "via": "latent_nn",
                })
        except Exception as e:
            log.debug("recall worldview.query: %s", e)

    if use_rollout:
        wv_roll = CAPABILITY_REGISTRY.get("worldview.rollout")
        if wv_roll and wv_roll.get("func"):
            try:
                roll = await wv_roll["func"](text=query,
                                              steps=max(1, int(rollout_steps)))
                member_ids: List[str] = []
                concept_of: Dict[str, int] = {}
                label_of:   Dict[str, str] = {}
                # Skip step 0 (the query's own concept) — we want where it's heading.
                for step in (roll or {}).get("trajectory", [])[1:]:
                    for rid in (step.get("members") or []):
                        if rid and rid not in concept_of:
                            member_ids.append(rid)
                            concept_of[rid] = step.get("concept", -1)
                            label_of[rid]   = step.get("label", "")
                rec_map: Dict[str, Any] = {}
                fab = _fabric()
                pg  = getattr(fab, "FABRIC_PG", None) if fab else None
                if pg and getattr(pg, "available", False) and member_ids:
                    rec_map = await pg.get_by_ids(member_ids[:top_k]) or {}
                for rid in member_ids[:top_k]:
                    rec = rec_map.get(rid)
                    results.append({
                        "id": rid,
                        "text": (getattr(rec, "text", "") or "")[:300] if rec else "",
                        "source": "worldview", "score": 0.5,
                        "dataset_id": getattr(rec, "dataset_id", "") if rec else "",
                        "concept": concept_of.get(rid, -1),
                        "concept_label": label_of.get(rid, ""),
                        "via": "rollout",
                    })
            except Exception as e:
                log.debug("recall worldview.rollout: %s", e)

    return results, []


async def _recall_related_qa(query: str, session_id: str, agent_name: str,
                              limit: int = 4,
                              ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """Second-order recall: similar past question → its paired answer →
    graph neighbours. Reuses _resolve_related_qa_block. Returns
    (results, edges, pairs); each prior answer becomes a source='qa' node."""
    try:
        _block, pairs = await _resolve_related_qa_block(
            str(limit), query, session_id, agent_name)
    except Exception as e:
        log.debug("recall related_qa: %s", e)
        return [], [], []
    results: List[Dict] = []
    for i, p in enumerate(pairs or []):
        a = (p.get("a") or "").strip()
        if not a:
            continue
        node_id = p.get("a_id") or p.get("id") or ("qa-" + str(i))
        results.append({
            "id": node_id, "text": a[:300], "source": "qa",
            "score": round(float(p.get("score", 0.5)), 4),
            "question": (p.get("q") or "")[:200], "via": "related_qa",
        })
    return results, [], (pairs or [])


def _merge_recall_results(buckets: List[List[Dict]], top_k: int) -> List[Dict]:
    """Dedup across sources by canonical record id (text-only results by
    normalised text), union provenance, take max score + a small corroboration
    bonus when ≥2 sources agree, then re-rank and trim to top_k."""
    by_key: Dict[str, Dict] = {}
    order:  List[str] = []
    for bucket in buckets:
        for r in bucket:
            rid = (r.get("id") or "").strip()
            key = rid if rid else ("txt:" + (r.get("text") or r.get("label") or "")[:80].lower().strip())
            if key in ("", "txt:"):
                continue
            src = r.get("source") or "unknown"
            if key in by_key:
                node = by_key[key]
                srcs = node["sources"]
                if src not in srcs:
                    srcs.add(src)
                    # Cross-source agreement is signal — small bonus on top of max.
                    node["score"] = round(min(1.0,
                        max(node.get("score", 0), r.get("score", 0)) + 0.05), 4)
                else:
                    node["score"] = max(node.get("score", 0), r.get("score", 0))
                for f in ("text", "dataset_id", "concept_label", "entities",
                          "question", "via"):
                    if not node.get(f) and r.get(f):
                        node[f] = r[f]
            else:
                node = dict(r)
                node["sources"] = {src}
                by_key[key] = node
                order.append(key)
    out = [by_key[k] for k in order]
    for n in out:
        n["sources"] = sorted(n["sources"])
        if len(n["sources"]) > 1:
            n["source"] = "multi"
    out.sort(key=lambda r: r.get("score", 0), reverse=True)
    return out[:top_k]


@capability(
    "context.recall",
    http_method="POST", http_path="/context/recall", http_tags=["context"],
    memory="off",
    description=(
        "Widen context recall beyond plain vector search. Concurrently: crawls "
        "the data-fabric entity graph out from seed records (records sharing an "
        "entity with the seeds), pulls worldview latent-space neighbours (and "
        "optionally predicted-next-concept members via rollout), and recalls "
        "paired answers for similar past questions. Results are deduped across "
        "every source by record id with merged provenance. "
        "Inputs: query (str!), session_id, agent_name, seed_ids (list[str] — ids "
        "the caller already found, used as crawl seeds), top_k (int 12), sources "
        "(csv of entities,worldview,related_qa — default all), fabric_dataset "
        "(str), min_score (float 0.4), use_rollout (bool), rollout_steps (int 4). "
        "Output: {results:[{id,text,source,sources,score,dataset_id,concept_label,"
        "entities,via}], edges, pairs, count, sources_used, cached}."
    ),
)
async def cap_context_recall(
    query:          str,
    session_id:     str  = "",
    agent_name:     str  = "",
    seed_ids:       List[str] = None,
    top_k:          int  = 12,
    sources:        str  = "entities,worldview,related_qa",
    fabric_dataset: str  = "",
    min_score:      float = 0.4,
    use_rollout:    bool = False,
    rollout_steps:  int  = 4,
    trace_id=None,
):
    if not query or not query.strip():
        return {"results": [], "edges": [], "pairs": [], "count": 0,
                "sources_used": [], "cached": False, "error": "query required"}

    top_k = max(1, min(60, int(top_k)))
    want = {s.strip().lower() for s in (sources or "").split(",") if s.strip()}
    if not want:
        want = {"entities", "worldview", "related_qa"}
    seed_ids = [s for s in (seed_ids or []) if s]

    cache_key = json.dumps({
        "q": query, "s": session_id, "src": sorted(want),
        "ds": fabric_dataset, "ro": bool(use_rollout), "k": top_k,
        "seeds": sorted(seed_ids)[:20],
    }, sort_keys=True)
    cached = _recall_cache_get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    tasks: List[Any] = []
    labels: List[str] = []
    if "entities" in want:
        tasks.append(_recall_entity_crawl(seed_ids, query, top_k, fabric_dataset))
        labels.append("entities")
    if "worldview" in want:
        tasks.append(_recall_worldview(query, top_k, fabric_dataset,
                                        use_rollout, rollout_steps))
        labels.append("worldview")
    if "related_qa" in want:
        tasks.append(_recall_related_qa(query, session_id, agent_name))
        labels.append("related_qa")

    gathered = await asyncio.gather(*tasks, return_exceptions=True)

    buckets: List[List[Dict]] = []
    all_edges: List[Dict] = []
    pairs: List[Dict] = []
    sources_used: List[str] = []
    for label, res in zip(labels, gathered):
        if isinstance(res, Exception):
            log.debug("recall %s failed: %s", label, res)
            continue
        if label == "related_qa":
            r_results, r_edges, r_pairs = res
            pairs = r_pairs
        else:
            r_results, r_edges = res
        if r_results:
            buckets.append(r_results)
            sources_used.append(label)
        all_edges.extend(r_edges or [])

    merged = _merge_recall_results(buckets, top_k)
    # Structural results (entity-crawl, related-QA) and corroborated multi-source
    # hits bypass the similarity floor; pure vector/latent hits must clear it.
    merged = [m for m in merged
              if m.get("score", 0) >= min_score
              or m.get("source") in ("entity", "qa", "multi")]

    keep_ids = {m.get("id") for m in merged}
    edges = [e for e in all_edges
             if e.get("from") in keep_ids and e.get("to") in keep_ids]

    out = {"results": merged, "edges": edges, "pairs": pairs,
           "count": len(merged), "sources_used": sources_used, "cached": False}
    _recall_cache_put(cache_key, out)
    return out


@capability(
    "context.recall_fabric",
    http_method="POST", http_path="/context/recall_fabric", http_tags=["context"],
    memory="off",
    description=(
        "Fabric- and DAG-only context recall (deliberately NO memory). Retrieves "
        "actual records from the data fabric for a query — optionally restricted to "
        "a set of dataset_ids the caller already deemed relevant — plus matching "
        "stored DAG workflows. Intended for the DAG agent loop to inject real fabric "
        "data instead of only dataset-name hints. "
        "Inputs: query (str!), keywords (csv str), dataset_ids (list[str] — restrict "
        "fabric search to these datasets), top_k (int 12), include_dags (bool true). "
        "Output: {records:[{dataset_id,record_id,score,summary}], dags:[{id,name,...}], "
        "count, datasets_used}."
    ),
)
async def cap_context_recall_fabric(
    query:        str,
    keywords:     str = "",
    dataset_ids:  List[str] = None,
    top_k:        int = 12,
    include_dags: bool = True,
    trace_id=None,
):
    if not query or not query.strip():
        return {"records": [], "dags": [], "count": 0, "datasets_used": [],
                "error": "query required"}

    top_k = max(1, min(60, int(top_k)))
    dataset_ids = [d for d in (dataset_ids or []) if d]
    kw = ", ".join(_split_ids(keywords)) if keywords else ""
    search_text = (query + (" " + kw if kw else "")).strip()

    fabric_query = CAPABILITY_REGISTRY.get("fabric.query", {}).get("func")
    records: List[Dict] = []
    datasets_used: List[str] = []
    if fabric_query:
        try:
            if dataset_ids:
                # Pull from each relevant dataset and merge (dedupe by record id)
                per = max(3, top_k // max(1, len(dataset_ids)))
                seen = set()
                for did in dataset_ids:
                    try:
                        res = await fabric_query(text=search_text, vector=search_text,
                                                 dataset_id=did, top_k=per)
                    except Exception as e:
                        log.debug("recall_fabric query [%s]: %s", did, e)
                        continue
                    hits = (res or {}).get("results", []) or []
                    if hits:
                        datasets_used.append(did)
                    for h in hits:
                        rid = h.get("record_id") or h.get("id")
                        key = (h.get("dataset_id"), rid)
                        if key in seen:
                            continue
                        seen.add(key)
                        records.append(h)
            else:
                res = await fabric_query(text=search_text, vector=search_text, top_k=top_k)
                records = (res or {}).get("results", []) or []
                datasets_used = sorted({h.get("dataset_id") for h in records
                                        if h.get("dataset_id")})
        except Exception as e:
            log.debug("recall_fabric: %s", e)

    records.sort(key=lambda h: h.get("score", 0) or 0, reverse=True)
    records = records[:top_k]

    dags: List[Dict] = []
    if include_dags:
        try:
            _, dags = await _resolve_dags_block("auto", query, top_k=8)
        except Exception as e:
            log.debug("recall_fabric dags: %s", e)

    return {"records": records, "dags": dags, "count": len(records),
            "datasets_used": datasets_used}


# ─────────────────────────────────────────────────────────────────────────────
# 2.  STREAM REGISTRY
# ─────────────────────────────────────────────────────────────────────────────

# In-process registry. Active streams have their token buffer in memory; a
# Redis ring buffer is used as a durable mirror so other processes can read.
STREAM_REGISTRY: Dict[str, Dict[str, Any]] = {}
STREAM_HISTORY: List[Dict[str, Any]] = []   # last 200 completed streams
_STREAM_HISTORY_MAX = 200

REDIS_STREAM_BUF_PREFIX = "vera:stream_buf:"
REDIS_STREAM_BUF_TTL    = 3600


def _stream_buf_key(stream_id: str) -> str:
    return f"{REDIS_STREAM_BUF_PREFIX}{stream_id}"


async def stream_append_token(stream_id: str, token: str):
    """Helper for producers — appends a token to a registered stream."""
    s = STREAM_REGISTRY.get(stream_id)
    if not s:
        return
    s["tokens"].append(token)
    s["last_at"] = now_iso()
    s["seq"] += 1

    # Mirror to Redis ring buffer for cross-process subscribers
    r = _redis()
    if r:
        try:
            pipe = r.pipeline()
            pipe.rpush(_stream_buf_key(stream_id), token)
            pipe.ltrim(_stream_buf_key(stream_id), -4000, -1)
            pipe.expire(_stream_buf_key(stream_id), REDIS_STREAM_BUF_TTL)
            await pipe.execute()
        except Exception:
            pass

    # Also broadcast as an event so existing pub/sub subscribers see it
    try:
        await emit_event({
            "type":      "stream.token",
            "stream_id": stream_id,
            "kind":      s.get("kind", "llm"),
            "seq":       s["seq"],
            "token":     token,
            "session_id": s.get("session_id", ""),
        })
    except Exception:
        pass


async def stream_register(
    *,
    kind:         str,
    source_cap:   str  = "",
    session_id:   str  = "",
    label:        str  = "",
    persist_full: bool = True,
    fabric_dataset: str = "",
    metadata:     Optional[Dict] = None,
) -> str:
    """Producer-side helper. Returns the new stream_id."""
    sid = str(uuid.uuid4())
    rec = {
        "id":             sid,
        "kind":           kind,
        "source_cap":     source_cap,
        "session_id":     session_id,
        "label":          label or f"{kind}:{source_cap}",
        "persist_full":   bool(persist_full),
        "fabric_dataset": fabric_dataset or f"streams.{kind}",
        "metadata":       metadata or {},
        "tokens":         [],
        "seq":            0,
        "started_at":     now_iso(),
        "last_at":        now_iso(),
        "completed_at":   "",
        "status":         "active",
        "final_text":     "",
        "byte_count":     0,
    }
    STREAM_REGISTRY[sid] = rec
    try:
        await emit_event({
            "type":      "stream.register",
            "stream_id": sid,
            "kind":      kind,
            "source_cap": source_cap,
            "session_id": session_id,
            "label":     rec["label"],
        })
    except Exception:
        pass
    return sid


async def stream_complete(stream_id: str, final_text: str = "") -> Dict[str, Any]:
    rec = STREAM_REGISTRY.get(stream_id)
    if not rec:
        return {"ok": False, "error": "stream not found"}

    full = final_text or "".join(rec["tokens"])
    rec["final_text"]   = full
    rec["byte_count"]   = len(full)
    rec["completed_at"] = now_iso()
    rec["status"]       = "complete"

    # Persist final output to fabric and memory
    if rec["persist_full"] and full:
        try:
            fab = _fabric()
            if fab and hasattr(fab, "ingest_dataset"):
                await fab.ingest_dataset(
                    dataset_id = rec["fabric_dataset"],
                    data       = [{
                        "text":       full,
                        "stream_id":  stream_id,
                        "kind":       rec["kind"],
                        "source_cap": rec["source_cap"],
                        "session_id": rec["session_id"],
                        "label":      rec["label"],
                        "started_at": rec["started_at"],
                        "completed_at": rec["completed_at"],
                        **(rec["metadata"] or {}),
                    }],
                    source = "stream_registry",
                    tags   = ["stream", rec["kind"], rec["source_cap"]],
                )
        except Exception as e:
            log.debug("stream_complete fabric ingest: %s", e)

        # Memory record so it shows up on the session graph
        try:
            mem = _memory()
            if mem and rec.get("session_id"):
                MemRec = mem.MemoryRecord
                node = MemRec(
                    id          = str(uuid.uuid4()),
                    session_id  = rec["session_id"],
                    record_type = "event",
                    source_type = "tool",
                    category    = f"stream.{rec['kind']}",
                    tags        = ["stream", rec["kind"], rec["source_cap"]],
                    text        = full[:6000],
                    capability  = rec["source_cap"],
                    importance  = 0.55,
                    metadata    = {"stream_id": stream_id, **(rec["metadata"] or {})},
                )
                # Use store() directly — record_*_turn paths require human/AI pairing
                if hasattr(mem.MEMORY, "store"):
                    asyncio.create_task(mem.MEMORY.store(node))
        except Exception as e:
            log.debug("stream_complete memory store: %s", e)

    # Move to history, drop from active
    STREAM_HISTORY.insert(0, dict(rec, tokens=[]))   # don't keep tokens in history
    if len(STREAM_HISTORY) > _STREAM_HISTORY_MAX:
        STREAM_HISTORY.pop()
    STREAM_REGISTRY.pop(stream_id, None)

    try:
        await emit_event({
            "type":      "stream.complete",
            "stream_id": stream_id,
            "kind":      rec["kind"],
            "byte_count": rec["byte_count"],
            "session_id": rec["session_id"],
        })
    except Exception:
        pass

    return {"ok": True, "stream_id": stream_id, "byte_count": rec["byte_count"]}


@capability(
    "stream.register", memory="off",
    http_method="POST", http_path="/stream/register", http_tags=["stream"],
    description="Manually register a new stream so other caps and panels can "
                "subscribe. Most stream producers use stream_register() helper "
                "directly. Returns {stream_id}.",
)
async def cap_stream_register(
    kind:           str,
    source_cap:     str = "",
    session_id:     str = "",
    label:          str = "",
    persist_full:   bool = True,
    fabric_dataset: str = "",
    trace_id=None,
):
    sid = await stream_register(
        kind=kind, source_cap=source_cap, session_id=session_id,
        label=label, persist_full=persist_full, fabric_dataset=fabric_dataset,
    )
    return {"stream_id": sid}


@capability(
    "stream.list", memory="off", silent=True,
    http_method="GET", http_path="/stream/list", http_tags=["stream"],
    description="List active and recent streams. Filter by kind/session/source_cap.",
)
async def cap_stream_list(kind: str = "", session_id: str = "",
                          include_history: bool = True,
                          history_limit: int = 50,
                          trace_id=None):
    def _match(rec):
        if kind and rec.get("kind") != kind:
            return False
        if session_id and rec.get("session_id") != session_id:
            return False
        return True

    active = [
        {k: v for k, v in r.items() if k != "tokens"}
        for r in STREAM_REGISTRY.values() if _match(r)
    ]
    # Add live byte count from token buffer
    for r in active:
        full = STREAM_REGISTRY.get(r["id"], {}).get("tokens") or []
        r["byte_count"] = sum(len(t) for t in full)
        r["preview"]    = ("".join(full))[:200]

    history = []
    if include_history:
        history = [r for r in STREAM_HISTORY if _match(r)][:history_limit]

    return {"active": active, "history": history,
            "active_count": len(active), "history_count": len(history)}


@capability(
    "stream.snapshot", memory="off", silent=True,
    http_method="GET", http_path="/stream/snapshot", http_tags=["stream"],
    description="Fetch the current accumulated text and metadata of a stream "
                "by id. Reads in-process token buffer; falls back to Redis "
                "ring buffer for cross-process visibility.",
)
async def cap_stream_snapshot(stream_id: str, trace_id=None):
    rec = STREAM_REGISTRY.get(stream_id)
    text = ""
    if rec:
        text = "".join(rec["tokens"])
        return {
            "stream_id":  stream_id,
            "status":     rec["status"],
            "kind":       rec["kind"],
            "source_cap": rec["source_cap"],
            "session_id": rec["session_id"],
            "seq":        rec["seq"],
            "started_at": rec["started_at"],
            "last_at":    rec["last_at"],
            "byte_count": len(text),
            "text":       text,
        }

    # Maybe completed — search history
    for h in STREAM_HISTORY:
        if h["id"] == stream_id:
            return {**h, "text": h.get("final_text", "")}

    # Last resort: Redis ring buffer
    r = _redis()
    if r:
        try:
            chunks = await r.lrange(_stream_buf_key(stream_id), 0, -1)
            if chunks:
                joined = "".join(c.decode() if isinstance(c, bytes) else c for c in chunks)
                return {"stream_id": stream_id, "status": "active_remote",
                        "text": joined, "byte_count": len(joined)}
        except Exception:
            pass

    return {"error": "stream not found", "stream_id": stream_id}


@capability(
    "stream.complete", memory="off",
    http_method="POST", http_path="/stream/complete", http_tags=["stream"],
    description="Mark a stream complete and persist the full accumulated "
                "output to the data fabric and memory graph. Producers should "
                "call this when their stream finishes.",
)
async def cap_stream_complete(stream_id: str, final_text: str = "", trace_id=None):
    return await stream_complete(stream_id, final_text)


# ─────────────────────────────────────────────────────────────────────────────
# 3.  dag.agent_loop  —  IDE-style ReAct agent as a reusable capability
# ─────────────────────────────────────────────────────────────────────────────

_AGENT_LOOP_BLACKLIST = {
    # Never let the loop call itself or anything that would cause recursion
    "dag.agent_loop", "dag.agent_loop_v2", "dag.agent_loop_v3",
    "dag.agent_loop_v4", "dag.agent_loop_v5", "dag.agent_loop_v6",
    # Planning/DAG handover caps — the loop must decompose and execute goals
    # ITSELF; delegating planning (or plan execution) to a capability derails
    # the run into "planning a plan" instead of acting.
    "dag.plan", "dag.plan_and_run", "dag.run", "dag.from_goal",
    "dag.store_run", "llm.plan",
    # Skip ultra-low-level meta caps
    "ui.panel.create", "ui.panel.update", "ui.panel.delete",
}


def _gated_read_caps() -> set:
    """Caps the canonical-retrieval gate (fabric/memory_retrieval.py) hides
    from agent discovery. Soft gate — explicit calls still work. Empty set
    when the module isn't loaded or tooling mode is 'full'."""
    mr = (sys.modules.get("memory_retrieval")
          or sys.modules.get("Vera.vera.fabric.memory_retrieval"))
    try:
        return mr.gated_discovery_caps() if mr else set()
    except Exception:
        return set()


def _fabric_usage_hints(toolkit) -> str:
    """Return a retrieval-tips block matched to the tools in the toolkit."""
    parts = []
    if "memory.seek" in toolkit:
        parts.append(
            "MEMORY RETRIEVAL TIPS:\n"
            "• memory.seek(query=\"…\") is THE way to search stored knowledge — "
            "hybrid search with duplicates collapsed, sized to max_chars.\n"
            "• Narrow with scope=\"<dataset or prefix>\" and since=\"7d\"; "
            "read the returned `text` field.\n"
            "• Expand any hit IN FULL with memory.read(record_id=\"<id>\").\n"
            "• Browse what data exists with memory.map() / memory.map(prefix=\"caps\").\n\n"
        )
    fabric_tools = {"fabric.query", "fabric.datasets", "fabric.ingest",
                    "fabric.skills.list", "fabric.skills.get", "fabric.stats"}
    if any(t in fabric_tools for t in toolkit):
        parts.append(
            "DATA FABRIC TIPS:\n"
            "• Search with: fabric.query(text=\"your search\") for keyword search,\n"
            "  or fabric.query(vector=\"your search\") for semantic search.\n"
            "• Add dataset_id=\"name\" to restrict to a specific dataset.\n"
            "• Set include_data=True to get full record content, not just summaries.\n"
            "• fabric.datasets can return THOUSANDS of rows — always pass parent=\"<namespace>\".\n"
            "• You can also pass query=\"plain text\" — it auto-converts to text+vector search.\n\n"
        )
    return "".join(parts)


def _agent_loop_cap_signature(name: str) -> str:
    ds = _dag_store()
    if ds and getattr(ds, "CAP_INDEX", None):
        try:
            return ds.CAP_INDEX.cap_signature(name)
        except Exception:
            pass
    return _inline_cap_sig(name)


def _coerce_arg_types(cap_name: str, args: Dict) -> Tuple[Dict, List[str]]:
    """Best-effort type coercion based on the cap's schema.

    Handles the common LLM mistake of quoting numbers/booleans as strings.
    Returns (coerced_args, notes). Notes describe each conversion done.
    """
    cap = CAPABILITY_REGISTRY.get(cap_name)
    if not cap or not isinstance(args, dict):
        return (args if isinstance(args, dict) else {}), []
    schema = cap.get("schema", {}) or {}
    props = schema.get("properties", {}) or {}
    out: Dict = dict(args)
    notes: List[str] = []
    for pname, pspec in props.items():
        if pname not in out or pname == "trace_id":
            continue
        val = out[pname]
        ptype = (pspec or {}).get("type")
        try:
            if ptype == "boolean" and isinstance(val, str):
                lv = val.lower().strip()
                if lv in ("true", "yes", "1", "on"):
                    out[pname] = True; notes.append(f"{pname}: '{val}' -> True")
                elif lv in ("false", "no", "0", "off", ""):
                    out[pname] = False; notes.append(f"{pname}: '{val}' -> False")
            elif ptype == "integer" and isinstance(val, str) and val.strip():
                try:
                    out[pname] = int(val); notes.append(f"{pname}: '{val}' -> int")
                except Exception:
                    try:
                        out[pname] = int(float(val)); notes.append(f"{pname}: '{val}' -> int (via float)")
                    except Exception:
                        pass
            elif ptype == "number" and isinstance(val, str) and val.strip():
                try:
                    out[pname] = float(val); notes.append(f"{pname}: '{val}' -> float")
                except Exception:
                    pass
            elif ptype == "array" and isinstance(val, str):
                try:
                    parsed = json.loads(val)
                    if isinstance(parsed, list):
                        out[pname] = parsed; notes.append(f"{pname}: parsed JSON array")
                    else:
                        out[pname] = [parsed]; notes.append(f"{pname}: wrapped scalar in list")
                except Exception:
                    pieces = [s.strip() for s in val.split(",") if s.strip()]
                    if pieces:
                        out[pname] = pieces; notes.append(f"{pname}: comma-split into {len(pieces)} items")
            elif ptype == "object" and isinstance(val, str):
                try:
                    parsed = json.loads(val)
                    if isinstance(parsed, dict):
                        out[pname] = parsed; notes.append(f"{pname}: parsed JSON object")
                except Exception:
                    pass
        except Exception:
            pass
    return out, notes


# ─────────────────────────────────────────────────────────────────────────────
# Session capability guards — a HARD sandbox for an agent-loop run.
#
# While a guard is registered for a session, every tool call the loop makes is
# checked at EXECUTION time (allowed_caps alone only shapes the catalog — it
# was never enforced). A guard makes two promises the prompt cannot:
#   • caps outside `allow` (or matching `deny`) are refused, so toolkit
#     expansion / need_caps grants / discovery can never escape the sandbox;
#   • `pin_args` are force-applied AFTER the LLM's args, so the agent cannot
#     amend e.g. is_sim=0 to reach the real business from a simulation run.
# Patterns are exact cap names or prefix globs ("business.*"). Deny beats allow.
# ─────────────────────────────────────────────────────────────────────────────

_SESSION_CAP_GUARDS: Dict[str, Dict[str, Any]] = {}


def _guard_match(patterns, cap_name: str) -> bool:
    for p in patterns or []:
        p = str(p).strip()
        if not p:
            continue
        if p == "*" or p == cap_name:
            return True
        if p.endswith("*") and cap_name.startswith(p[:-1]):
            return True
    return False


def set_session_cap_guard(session_id: str, guard: Dict[str, Any]) -> Dict[str, Any]:
    """Register (replace) the cap guard for a session. Guard keys:
    label (str), allow (list of cap names/globs — empty = allow all),
    deny (list of names/globs — beats allow), pin_args ({pattern: {arg: val}}).
    The returned dict carries a `token` — pass it to clear_session_cap_guard so
    a superseded run's cleanup can never strip a NEWER run's guard."""
    g = {
        "label":    str((guard or {}).get("label") or "sandbox"),
        "allow":    [str(c).strip() for c in (guard or {}).get("allow") or [] if str(c).strip()],
        "deny":     [str(c).strip() for c in (guard or {}).get("deny") or [] if str(c).strip()],
        "pin_args": {str(k): dict(v) for k, v in ((guard or {}).get("pin_args") or {}).items()
                     if isinstance(v, dict)},
        "token":    uuid.uuid4().hex,
    }
    _SESSION_CAP_GUARDS[session_id] = g
    return g


def get_session_cap_guard(session_id: str) -> Optional[Dict[str, Any]]:
    return _SESSION_CAP_GUARDS.get(session_id or "")


def clear_session_cap_guard(session_id: str, token: str = "") -> None:
    cur = _SESSION_CAP_GUARDS.get(session_id or "")
    if cur is None:
        return
    if token and cur.get("token") != token:
        return    # a newer run re-guarded this session — leave its guard alone
    _SESSION_CAP_GUARDS.pop(session_id or "", None)


def _guard_blocks(guard: Dict[str, Any], cap_name: str) -> str:
    """Return a refusal message when the guard blocks cap_name, else ''."""
    if _guard_match(guard.get("deny"), cap_name):
        return (f"'{cap_name}' is blocked by the '{guard.get('label')}' sandbox. "
                f"Pick a different capability from your approved toolkit.")
    allow = guard.get("allow") or []
    if allow and not _guard_match(allow, cap_name):
        sample = ", ".join(allow[:8]) + ("…" if len(allow) > 8 else "")
        return (f"'{cap_name}' is outside the '{guard.get('label')}' sandbox — this run "
                f"may ONLY use its approved toolkit ({sample}). Do not retry this "
                f"capability; achieve the step with an approved one instead.")
    return ""


def _guard_pin_kwargs(guard: Dict[str, Any], cap_name: str,
                      kwargs: Dict, accepted: set) -> List[str]:
    """Force-apply pinned args for every matching pattern. Returns notes."""
    notes: List[str] = []
    for pattern, pins in (guard.get("pin_args") or {}).items():
        if not _guard_match([pattern], cap_name):
            continue
        for k, v in (pins or {}).items():
            if k in accepted and kwargs.get(k) != v:
                kwargs[k] = v
                notes.append(f"{k} pinned to {v!r} by '{guard.get('label')}' sandbox")
    return notes


async def _agent_loop_call_tool(cap_name: str, args: Dict, *,
                                 session_id: str = "", trace_id: str = "") -> Dict:
    cap = CAPABILITY_REGISTRY.get(cap_name)
    if not cap:
        return {"ok": False, "error": f"Unknown capability: {cap_name}"}

    guard = _SESSION_CAP_GUARDS.get(session_id) if session_id else None
    if guard:
        blocked = _guard_blocks(guard, cap_name)
        if blocked:
            return {"ok": False, "error": blocked, "guard": guard.get("label")}

    # Type-coerce args BEFORE filtering — this fixes the common LLM mistake
    # of passing ints/floats/bools as strings (e.g. "64" instead of 64).
    coerced, _coerce_notes = _coerce_arg_types(cap_name, args or {})

    accepted = set(cap.get("schema", {}).get("properties", {}).keys()) | {"trace_id"}
    kwargs = {k: v for k, v in coerced.items() if k in accepted}
    if session_id and "session_id" in accepted:
        kwargs.setdefault("session_id", session_id)
    if guard:
        _coerce_notes = list(_coerce_notes) + _guard_pin_kwargs(
            guard, cap_name, kwargs, accepted)
    try:
        result = await cap["func"](**kwargs, trace_id=trace_id)
        return {"ok": True, "result": result, "_coerce_notes": _coerce_notes,
                "_coerced_args": coerced}
    except Exception as e:
        return {"ok": False, "error": str(e), "_coerce_notes": _coerce_notes,
                "_coerced_args": coerced}


# Caps whose output is already sized to the caller's budget (memory.seek's
# max_chars) or is inherently a document — don't re-truncate them at 1500.
_LOOP_PREVIEW_LONGFORM = {"memory.seek", "memory.read", "memory.map",
                          "web.fetch", "llm.summarize"}


def _preview_for_loop(result: Any, max_len: int = 1500, tool: str = "") -> str:
    if tool in _LOOP_PREVIEW_LONGFORM:
        max_len = max(max_len, 8000)
    if result is None:
        return "null"
    if isinstance(result, str):
        return result if len(result) <= max_len else result[:max_len] + "\n[__truncated__: result longer than preview limit]"
    try:
        s = json.dumps(result, default=str, ensure_ascii=False)
    except Exception:
        s = str(result)
    if len(s) <= max_len:
        return s
    return s[:max_len] + "\n[__truncated__: result longer than preview limit]"


@capability(
    "dag.agent_loop",
    http_method="POST", http_path="/dag/agent_loop", http_tags=["dag", "agents"],
    memory="on",
    streams=["dag.agent_loop"],
    description=(
        "ReAct-style agent loop. Given a goal and a tool list, the LLM picks "
        "ONE capability per cycle, executes it, and feeds the result back as "
        "the next observation. Loops until {action:done} or max_cycles. "
        "Inputs: goal (str!), allowed_caps (csv str — empty = auto-pick by goal), "
        "max_cycles (int, default 8), model (str), instance_id (str), "
        "prefer_gpu (bool), attach_skills (str), attach_ontologies (str), "
        "session_id (str), context_top_k (int, default 14). "
        "Output: {final_state, history, cycles, done, summary}."
    ),
)
async def cap_dag_agent_loop(
    goal:               str,
    allowed_caps:       str  = "",
    max_cycles:         int  = 8,
    model:              str  = "",
    instance_id:        str  = "",
    prefer_gpu:         bool = True,
    await_long_running: bool = True,
    long_running_timeout_secs: int = 1800,
    max_recovery_attempts: int = 2,
    system_prompt_template: str = "",
    attach_skills:      str  = "",
    attach_ontologies:  str  = "",
    session_id:         str  = "",
    context_top_k:      int  = 14,
    trace_id=None,
):
    if not goal:
        return {"error": "goal required"}

    max_cycles = max(1, min(40, int(max_cycles)))

    # ── Resolve tool list ─────────────────────────────────────────────────
    _gated = _gated_read_caps()
    if allowed_caps and allowed_caps != "*":
        # Explicit list — the caller decides; the soft gate never blocks these.
        tool_names = [c for c in _split_ids(allowed_caps) if c in CAPABILITY_REGISTRY]
    elif allowed_caps == "*":
        tool_names = [
            n for n in CAPABILITY_REGISTRY.keys()
            if n.split(".")[0] not in _CONTEXT_CAP_BLACKLIST_GROUPS
            and n not in _AGENT_LOOP_BLACKLIST
            and n not in _gated
        ][:30]
    else:
        # Auto: top-K via relevance search
        ds = _dag_store()
        if ds and getattr(ds, "CAP_INDEX", None):
            try:
                hits = await ds.CAP_INDEX.relevance_search(goal, top_k=context_top_k * 2)
                tool_names = [
                    n for n, _s in hits
                    if n.split(".")[0] not in _CONTEXT_CAP_BLACKLIST_GROUPS
                    and n not in _AGENT_LOOP_BLACKLIST
                    and n not in _gated
                ][:context_top_k]
            except Exception:
                tool_names = []
        else:
            tool_names = []

    if not tool_names:
        return {"error": "no tools available — check allowed_caps or capability registry"}

    # ── Build context block (skills + ontologies optional) ───────────────
    ctx_extra = ""
    if attach_skills or attach_ontologies:
        try:
            ctx = await build_context_prompt(
                goal,
                attach_skills=attach_skills,
                attach_ontologies=attach_ontologies,
            )
            ctx_extra = ctx.get("system_prompt", "")
        except Exception as e:
            log.debug("agent_loop context build: %s", e)

    tool_block = "\n".join(_agent_loop_cap_signature(n) for n in tool_names)

    if system_prompt_template and system_prompt_template.strip():
        sys_prompt = await _maybe_expand_template(
            system_prompt_template,
            goal=goal,
            toolkit_block=tool_block,
            toolkit_brief=", ".join(tool_names),
            toolkit_count=len(tool_names),
            ctx_extra=ctx_extra,
            enable_expand=False,
        )
    else:
        sys_prompt = (
            "You are a Vera autonomous agent. You work by calling TOOLS one at a time.\n\n"
            f"GOAL: {goal}\n\n"
            "============================================================\n"
            "YOUR TOOLKIT — already filtered for this goal. Use these tools:\n"
            "============================================================\n"
            f"{tool_block}\n\n"
            + _fabric_usage_hints(tool_names)
            + "ON EACH TURN, RESPOND WITH EXACTLY ONE JSON OBJECT (no prose, no fences):\n"
            '  {"thought": "brief reasoning", "tool": "<cap.name>", "args": { ... }}\n'
            '  {"action": "done", "summary": "what was accomplished"}\n\n'
            "RULES:\n"
            "1. PICK A TOOL FROM THE TOOLKIT ABOVE on the FIRST turn. Do NOT start by\n"
            "   calling caps.search / context.search_caps — the toolkit is already\n"
            "   filtered. Discovery on cycle 1 wastes a step.\n"
            "2. Only use tools from the list above. Inventing names will fail.\n"
            "3. Inspect tool signatures (param:type — required marked with !).\n"
            "4. Do not repeat the same tool with identical args more than twice in a row.\n"
            "5. When the goal is achieved, emit {\"action\":\"done\",\"summary\":\"...\"}.\n"
            "6. If a tool fails with bad-args, the runner retries with corrected\n"
            "   args automatically. Don't give up — keep working toward the goal.\n"
            "7. WORK IN PHASES: THINK, then EXPLORE with cheap read-only tools to\n"
            "   gather context BEFORE you act, then ACT, then VALIDATE the result\n"
            "   with a read-only check before emitting done. Avoid long-running\n"
            "   tools until you understand the situation.\n"
            + (("\n" + ctx_extra) if ctx_extra else "")
        )

    # ── Register stream so others can watch live ─────────────────────────
    stream_id = await stream_register(
        kind          = "agent_loop",
        source_cap    = "dag.agent_loop",
        session_id    = session_id,
        label         = goal[:80],
        persist_full  = True,
        fabric_dataset = "streams.agent_loop",
        metadata      = {"goal": goal, "max_cycles": max_cycles,
                          "tools": tool_names},
    )

    history: List[Dict] = []
    cycles  = 0
    done    = False
    summary = ""

    try:
        for cycle_i in range(max_cycles):
            cycles = cycle_i + 1

            obs_block = "\n\n".join(
                f"[Observation {i+1}] tool={h['tool']}\n{h['preview']}"
                for i, h in enumerate(history[-6:])
            ) or "(no observations yet — make your first tool call)"

            user_msg = (
                "Continue. Recent tool results:\n\n"
                + obs_block
                + "\n\nEmit the next JSON action."
            )

            await emit_event({
                "type": "agent_loop.cycle_planning",
                "stream_id": stream_id, "cycle": cycles,
                "session_id": session_id,
            })

            # Plan one step (thinking-model-aware)
            try:
                raw = await _safe_ollama_generate(
                    user_msg, system=sys_prompt,
                    model=model, instance_id=instance_id,
                    prefer_gpu=bool(prefer_gpu),
                    json_mode=True,
                )
            except Exception as e:
                _err_preview = f"Planner LLM call failed: {e}"
                history.append({"tool": "(planner_error)", "args": {},
                                "preview": _err_preview})
                await emit_event({
                    "type": "agent_loop.tool_done",
                    "stream_id": stream_id, "cycle": cycles,
                    "tool": "(planner)", "ok": False,
                    "elapsed_ms": 0,
                    "preview": _err_preview[:400],
                    "error": str(e)[:500],
                    "session_id": session_id,
                })
                continue

            # Strip thinking tokens before parsing/streaming
            clean_raw, think_text = _strip_think(raw or "")
            if think_text:
                await stream_append_token(stream_id, f"\n[think #{cycles}] {think_text[:600]}\n")
                await emit_event({
                    "type": "agent_loop.think",
                    "stream_id": stream_id, "cycle": cycles,
                    "thought": think_text[:2000], "session_id": session_id,
                })
            await stream_append_token(stream_id, f"\n[plan #{cycles}] {clean_raw[:800]}\n")

            action = None
            try:
                # Tolerant JSON extraction (think-stripped)
                cleaned = clean_raw.strip()
                if cleaned.startswith("```"):
                    cleaned = cleaned.split("```", 2)[1]
                    if cleaned.startswith("json"):
                        cleaned = cleaned[4:]
                start = cleaned.find("{")
                end   = cleaned.rfind("}")
                if start >= 0 and end > start:
                    action = json.loads(cleaned[start:end+1])
            except Exception:
                action = None

            if not isinstance(action, dict):
                _parse_preview = f"Planner output was not parseable JSON: {(raw or '')[:300]}"
                history.append({"tool": "(parse_error)", "args": {},
                                "preview": _parse_preview})
                await emit_event({
                    "type": "agent_loop.tool_done",
                    "stream_id": stream_id, "cycle": cycles,
                    "tool": "(planner)", "ok": False,
                    "elapsed_ms": 0,
                    "preview": _parse_preview[:400],
                    "error": _parse_preview[:500],
                    "session_id": session_id,
                })
                continue

            # Done?
            _tool_raw_v1 = action.get("tool", "")
            _is_done_via_tool_v1 = (isinstance(_tool_raw_v1, str)
                                     and _tool_raw_v1.strip().strip("()").lower()
                                     in ("done", "finish", "stop", "complete", "final"))
            if (action.get("action") in ("done", "finish", "stop", "complete")
                    or action.get("final") or action.get("answer")
                    or _is_done_via_tool_v1):
                done    = True
                summary = (action.get("summary") or action.get("final")
                            or action.get("answer") or "")
                await stream_append_token(stream_id, f"\n[done] {summary}\n")
                await emit_event({
                    "type": "agent_loop.done",
                    "stream_id": stream_id,
                    "summary": summary, "cycles": cycles,
                    "session_id": session_id,
                })
                break

            tool, args, _thought = _extract_tool_action(action, tool_names)
            if not isinstance(args, dict):
                args = {}

            if tool not in tool_names:
                _bad_tool_preview = (
                    f"ERROR: tool '{tool}' is not in the allowed tool list. "
                    f"Allowed: {', '.join(tool_names[:12])}"
                    + ("…" if len(tool_names) > 12 else "")
                )
                history.append({
                    "tool": tool or "(none)", "args": args,
                    "preview": _bad_tool_preview,
                })
                await emit_event({
                    "type": "agent_loop.tool_done",
                    "stream_id": stream_id, "cycle": cycles,
                    "tool": tool or "(none)", "ok": False,
                    "elapsed_ms": 0,
                    "preview": _bad_tool_preview[:400],
                    "error": f"Unknown tool: {tool}",
                    "session_id": session_id,
                })
                continue

            # ── Cycle-1 discovery block ──
            _SEARCH_CAPS_V1 = {"caps.search", "caps.describe",
                                "context.search_caps", "context.search_dags"}
            if cycles == 1 and tool in _SEARCH_CAPS_V1 and len(tool_names) >= 5:
                _disc_preview = (
                    f"Skipped {tool} on cycle 1 — toolkit has {len(tool_names)} "
                    "curated tools. Read them and pick one."
                )
                history.append({
                    "tool": "(cycle1_discovery_blocked)",
                    "args": args, "ok": False,
                    "preview": _disc_preview,
                })
                await emit_event({
                    "type": "agent_loop.tool_done",
                    "stream_id": stream_id, "cycle": cycles,
                    "tool": tool, "ok": False,
                    "elapsed_ms": 0,
                    "preview": _disc_preview[:400],
                    "error": "cycle-1 discovery blocked",
                    "session_id": session_id,
                })
                continue

            await emit_event({
                "type": "agent_loop.tool_call",
                "stream_id": stream_id, "cycle": cycles,
                "tool": tool, "args": args,
                "thought": action.get("thought", ""),
                "long_running": _local_is_long_running(tool),
                "session_id": session_id,
            })

            t0 = time.monotonic()
            invoke = await _agent_loop_call_tool(tool, args,
                                                 session_id=session_id,
                                                 trace_id=trace_id or "")

            # ── Promote inner errors so recovery can see them ──
            if invoke.get("ok") and isinstance(invoke.get("result"), dict):
                _ierr = invoke["result"].get("error")
                if _ierr:
                    invoke["ok"] = False
                    invoke["error"] = str(_ierr)

            # ── Error recovery: re-call same tool with fixed args ──
            if not invoke.get("ok"):
                recov = await _maybe_arg_recover(
                    cap_name=tool, failed_args=args,
                    error_text=invoke.get("error", ""),
                    model=model, instance_id=instance_id,
                    prefer_gpu=bool(prefer_gpu),
                    max_attempts=int(max_recovery_attempts),
                    call_tool=_agent_loop_call_tool,
                    session_id=session_id, trace_id=trace_id or "",
                    cycle=cycles, stream_id=stream_id, enabled=True,
                )
                if recov and recov.get("recovered"):
                    invoke = recov["final_invoke"]
                    last_a = recov.get("attempts") or []
                    if last_a:
                        args = last_a[-1].get("args", args)

            # ── Wait for long-running jobs (research, ml, exec, etc.) ──
            # Streams research tokens live via WebSocket when available.
            if invoke.get("ok") and isinstance(invoke.get("result"), dict):
                awaited = await _maybe_await_long_running(
                    cap_name=tool, result=invoke["result"],
                    session_id=session_id, trace_id=trace_id or "",
                    cycle=cycles, stream_id=stream_id,
                    max_wait_secs=float(long_running_timeout_secs),
                    enabled=bool(await_long_running),
                )
                invoke["result"] = awaited
                if isinstance(awaited, dict) and awaited.get("_await_error"):
                    invoke["ok"] = False
                    invoke["error"] = awaited["_await_error"]

            elapsed = round((time.monotonic() - t0) * 1000)

            if invoke.get("ok"):
                preview = _preview_for_loop(invoke["result"], tool=tool)
            else:
                preview = "ERROR: " + invoke.get("error", "unknown error")

            history.append({
                "tool":    tool,
                "args":    args,
                "ok":      bool(invoke.get("ok")),
                "preview": preview,
                "ms":      elapsed,
                "thought": action.get("thought", ""),
            })

            # ── Phase accounting ────────────────────────────────────────────
            if invoke.get("ok"):
                if _dw_cap_phase(tool) == "explore":
                    explore_done += 1
                    if acted and not validated:
                        validated = True
                        await _emit_phase_v2("validate", validated=True, tool=tool)
                    else:
                        await _emit_phase_v2("explore", tool=tool)
                else:
                    acted = True
                    await _emit_phase_v2("act", tool=tool)

            await stream_append_token(
                stream_id,
                f"\n[exec #{cycles}] {tool}({json.dumps(args, default=str)[:200]}) → "
                f"{preview[:400]}\n"
            )
            await emit_event({
                "type": "agent_loop.tool_done",
                "stream_id": stream_id, "cycle": cycles,
                "tool": tool, "ok": invoke.get("ok"),
                "elapsed_ms": elapsed, "preview": preview[:2000],
                "error": invoke.get("error", "") if not invoke.get("ok") else "",
                "session_id": session_id,
            })

            # Light loop-detection
            if len(history) >= 3:
                last3 = history[-3:]
                if all(h.get("tool") == last3[0].get("tool")
                       and json.dumps(h.get("args"), default=str, sort_keys=True)
                          == json.dumps(last3[0].get("args"), default=str, sort_keys=True)
                       for h in last3):
                    history.append({
                        "tool": "(loop_break)", "args": {},
                        "preview": "STOP. Same tool with identical args called 3 times. "
                                   "Either change strategy, finish with action:done, or "
                                   "return a different tool."
                    })

    finally:
        # Always close the stream cleanly
        try:
            await stream_complete(stream_id)
        except Exception:
            pass

    final_state = {
        "goal":     goal,
        "tools":    tool_names,
        "history":  history,
        "cycles":   cycles,
        "done":     done,
        "summary":  summary,
        "stream_id": stream_id,
    }

    return final_state


# ─────────────────────────────────────────────────────────────────────────────
# 4.  dag.agent_loop_v2 — triage + dynamic context + better termination
# ─────────────────────────────────────────────────────────────────────────────
#
# The original dag.agent_loop is a straight ReAct loop with a fixed toolkit.
# In practice users hit two problems:
#
#   (1) The seed-toolkit selection is goal-keyword-based, so a question like
#       "is maxhodl.com up?" never surfaces http.get / system.ping because the
#       string "is X up" doesn't keyword-match any web tool description.
#
#   (2) The loop tends to keep going after the goal is satisfied because the
#       sole termination signal is the LLM emitting {action:done}, which is
#       fragile when the model is small or the prompt long.
#
# v2 fixes both:
#   • TRIAGE — before the main loop, ask the LLM to classify the goal and
#     propose keywords. Run context.search_caps with those keywords to seed
#     the toolkit. This handles the maxhodl.com case: triage → "this is a
#     web reachability check, keywords: http, ping, website" → http.get and
#     system.ping get pulled into the toolkit.
#
#   • BASE DISCOVERY TOOLKIT — caps.search / caps.describe / context.search_*
#     are ALWAYS loaded so the agent can self-expand mid-run by calling them.
#     Essentials (http.get, system.ping, llm.generate) are also always loaded.
#
#   • EXPAND_TOOLS ACTION — the agent can emit {action:"expand_tools",
#     keywords:"X"} on any cycle. We append the search results to the
#     visible toolkit and feed back which caps were added.
#
#   • POST-EXEC SATISFACTION CHECK — after each tool result we ask the LLM a
#     short yes/no question: "given the result, is the goal satisfied? answer
#     in 1 word: yes or no." If yes, terminate immediately with a summary
#     pulled from the most recent observation.
#
#   • DEDUP TERMINATION — three identical tool calls in a row force termination.

# ── Always-loaded discovery + essential caps ────────────────────────────────
_BASE_DISCOVERY_CAPS = [
    "caps.search",          # find caps by relevance
    "caps.describe",        # see a cap's I/O contract
    "context.search_caps",  # alternate cap search (if dag_store route differs)
    "context.search_dags",  # find prebuilt DAGs that might already do this
    "memory.seek",          # canonical stored-knowledge search
    "memory.read",          # full record by id
    "memory.map",           # dataset namespace browser
]
_BASE_ESSENTIAL_CAPS = [
    "http.get",
    "system.ping",
    "llm.generate",
    "llm.summarize",
    "text.extract_urls",
]


async def _triage_goal(goal: str, *, model: str = "", instance_id: str = "",
                        prefer_gpu: bool = True) -> Dict[str, Any]:
    """Classify the goal and propose discovery keywords.

    Returns:
      {category: str, keywords: [str], reasoning: str}
    """
    sys = (
        "You are a goal-triage classifier. Read the user's goal and respond "
        "ONLY with a JSON object:\n"
        '{"category":"<one-word category>","keywords":["kw1","kw2","kw3"],'
        '"reasoning":"<one sentence>"}\n\n'
        "Categories: web_check, data_lookup, file_edit, summarisation, analysis, "
        "search, monitoring, code_task, system_info, other.\n\n"
        "Category guidance:\n"
        "  • data_lookup = query a structured store, database, dataset, or the "
        "data fabric. Examples: 'query the fabric for CVEs', 'search datasets for X', "
        "'look up entries in the data store'.\n"
        "  • search = search inside the system (caps, memory, dags).\n"
        "  • web_check = check if a URL/site is reachable.\n\n"
        "Keywords should be 2-5 capability-vocabulary terms for finding tools "
        "that could help. NOT goal-paraphrases or proper nouns.\n"
        "Examples:\n"
        "  'is example.com up?' → keywords: [\"http\",\"ping\",\"website\",\"reachability\"]\n"
        "  'query the fabric for CVEs' → keywords: [\"fabric\",\"query\",\"dataset\",\"search\"]"
    )
    try:
        raw = await ollama_generate(
            f"Goal: {goal}",
            system=sys,
            model=model or None,
            instance_id=instance_id or None,
            prefer_gpu=bool(prefer_gpu),
            json_mode=True,
        )
    except Exception as e:
        log.debug("triage failed: %s", e)
        return {"category": "other", "keywords": [], "reasoning": ""}

    raw = (raw or "").strip()
    try:
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
        s = raw.find("{"); e = raw.rfind("}")
        if s >= 0 and e > s:
            obj = json.loads(raw[s:e+1])
            kws = obj.get("keywords") or []
            if isinstance(kws, str):
                kws = [k.strip() for k in kws.split(",") if k.strip()]
            return {
                "category":  str(obj.get("category", "other")),
                "keywords":  [str(k) for k in kws][:6],
                "reasoning": str(obj.get("reasoning", ""))[:400],
            }
    except Exception:
        pass
    return {"category": "other", "keywords": [], "reasoning": ""}


async def _check_goal_satisfied(goal: str, last_observation: str,
                                  *, model: str = "", instance_id: str = "",
                                  prefer_gpu: bool = True) -> Dict[str, Any]:
    """One-shot yes/no termination check.

    Returns: {satisfied: bool, summary: str}
    """
    sys = (
        "You judge whether a goal has been satisfied based on the latest tool "
        "observation. Reply ONLY with a JSON object:\n"
        '{"satisfied":true|false,"summary":"<one short sentence>"}\n'
        "Be strict — only say satisfied=true if the observation directly answers "
        "or accomplishes the goal. If more work is clearly needed, say false."
    )
    prompt = (
        f"GOAL:\n{goal}\n\n"
        f"LATEST OBSERVATION:\n{last_observation[:1800]}\n\n"
        "Has the goal been satisfied?"
    )
    try:
        raw = await ollama_generate(
            prompt, system=sys,
            model=model or None,
            instance_id=instance_id or None,
            prefer_gpu=bool(prefer_gpu),
            json_mode=True,
        )
    except Exception:
        return {"satisfied": False, "summary": ""}

    raw = (raw or "").strip()
    # Thinking models frequently return an EMPTY string under format=json, which
    # would silently collapse to satisfied=False and make callers (e.g. the v4
    # completion check) loop forever on an already-answered goal. Retry once
    # without json_mode before giving up.
    if len(raw) < 4:
        try:
            raw = (await ollama_generate(
                prompt + "\n\nRespond with a single JSON object and nothing else.",
                system=sys,
                model=model or None,
                instance_id=instance_id or None,
                prefer_gpu=bool(prefer_gpu),
                json_mode=False,
            ) or "").strip()
        except Exception:
            return {"satisfied": False, "summary": ""}
    try:
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
        s = raw.find("{"); e = raw.rfind("}")
        if s >= 0 and e > s:
            obj = json.loads(raw[s:e+1])
            return {
                "satisfied": bool(obj.get("satisfied", False)),
                "summary":   str(obj.get("summary", ""))[:300],
            }
    except Exception:
        pass
    return {"satisfied": False, "summary": ""}


def _build_v2_toolkit(*, allowed_caps: str, triage_keywords: List[str],
                       extra_caps: List[str]) -> List[str]:
    """Compose the visible toolkit from triage-discovered caps + base essentials.

    Triage discovers tools from the full capability registry. The
    allowed_caps parameter is NOT used as a filter — tool access control
    happens at execution time, not at triage time.
    """
    toolkit: List[str] = []

    # Discovery caps — always present
    for c in _BASE_DISCOVERY_CAPS:
        if c in CAPABILITY_REGISTRY and c not in toolkit:
            toolkit.append(c)
    # Essentials — always present
    for c in _BASE_ESSENTIAL_CAPS:
        if c in CAPABILITY_REGISTRY and c not in toolkit:
            toolkit.append(c)

    # Triage-discovered extras
    _gated = _gated_read_caps()
    for c in extra_caps:
        if (c in CAPABILITY_REGISTRY and c not in toolkit
                and c not in _AGENT_LOOP_BLACKLIST and c not in _gated):
            toolkit.append(c)

    return toolkit


@capability(
    "dag.agent_loop_v2",
    http_method="POST", http_path="/dag/agent_loop_v2", http_tags=["dag", "agents"],
    memory="on",
    streams=["dag.agent_loop_v2"],
    description=(
        "Smarter ReAct agent loop with triage + dynamic context expansion + "
        "post-exec satisfaction check. Triage classifies the goal and seeds "
        "the toolkit. The agent can call caps.search / caps.describe / "
        "context.search_caps mid-run to discover more tools, or emit "
        "{action:'expand_tools', keywords:'X'} for the runner to do it. After "
        "every tool result we ask the LLM if the goal is satisfied; if yes, "
        "we terminate immediately. "
        "Inputs: goal (str!), allowed_caps (csv str — empty = auto-pick + base toolkit), "
        "max_cycles (int default 8), model (str), instance_id (str), prefer_gpu (bool), "
        "attach_skills (str), attach_ontologies (str), session_id (str), "
        "satisfaction_check (bool default True), enable_expand (bool default True). "
        "Output: {final_state, history, cycles, done, summary, toolkit, triage}."
    ),
)
async def cap_dag_agent_loop_v2(
    goal:               str,
    allowed_caps:       str  = "",
    max_cycles:         int  = 8,
    model:              str  = "",
    instance_id:        str  = "",
    prefer_gpu:         bool = True,
    attach_skills:      str  = "",
    attach_ontologies:  str  = "",
    session_id:         str  = "",
    satisfaction_check: bool = True,
    enable_expand:      bool = True,
    triage_top_k:       int  = 8,
    max_search_calls:   int  = 2,
    max_expands:        int  = 1,
    count_failed_cycles: bool = False,
    await_long_running: bool = True,
    long_running_timeout_secs: int = 1800,
    max_recovery_attempts: int = 2,
    system_prompt_template: str = "",
    # ── Phase model (think → explore → act → validate) + continue ────────────
    phased:             bool = True,
    min_explore_cycles: int  = 2,
    require_validate:   bool = True,
    long_running_force_hitl: bool = True,
    allow_continue:     bool = True,
    continue_increment: int  = 8,
    auto_continue_max:  int  = 0,
    hitl_timeout_secs:  int  = 300,
    trace_id=None,
):
    if not goal:
        return {"error": "goal required"}

    max_cycles = max(1, min(40, int(max_cycles)))
    min_explore_cycles = max(0, int(min_explore_cycles))
    continue_increment = max(1, min(40, int(continue_increment)))
    auto_continue_max  = max(0, int(auto_continue_max))

    # ── Stage 1: TRIAGE — uses the same workshop triage as openclaw/DAG workshop
    from Vera.vera.dag.dag_workshop_capabilities import (
        _workshop_triage_goal, _workshop_build_toolkit,
        _cap_phase as _dw_cap_phase, _is_long_running_cap as _dw_is_long_running,
        _await_hitl_decision as _dw_await_hitl,
    )
    await emit_event({
        "type": "agent_loop_v2.triage_start",
        "goal": goal[:200], "session_id": session_id,
    })
    triage = await _workshop_triage_goal(goal, model=model,
                                           instance_id=instance_id,
                                           prefer_gpu=prefer_gpu)
    await emit_event({
        "type": "agent_loop_v2.triage_done",
        "triage": triage, "session_id": session_id,
    })

    # ── Stage 2: SEED TOOLKIT — same builder as openclaw/DAG workshop
    toolkit = await _workshop_build_toolkit(
        allowed_caps=allowed_caps,
        category=triage.get("category", "other"),
        categories=triage.get("categories"),
        keywords=triage.get("keywords", []),
        top_k=int(triage_top_k),
    )

    if not toolkit:
        return {"error": "No usable tools after triage — check capability registry"}

    # ── Build context block (skills + ontologies optional) ────────────────
    ctx_extra = ""
    if attach_skills or attach_ontologies:
        try:
            ctx = await build_context_prompt(
                goal,
                attach_skills=attach_skills,
                attach_ontologies=attach_ontologies,
            )
            ctx_extra = ctx.get("system_prompt", "")
        except Exception:
            pass

    def _toolkit_block(names: List[str]) -> str:
        return "\n".join(_agent_loop_cap_signature(n) for n in names)

    _user_template = (system_prompt_template or "").strip()

    async def _build_sys(kit):
        if _user_template:
            return await _maybe_expand_template(
                _user_template,
                goal=goal,
                category=triage.get("category", ""),
                keywords=", ".join(triage.get("keywords") or []),
                reasoning=triage.get("reasoning", ""),
                toolkit_block=_toolkit_block(kit),
                toolkit_brief=", ".join(kit),
                toolkit_count=len(kit),
                ctx_extra=ctx_extra,
                enable_expand=enable_expand,
            )
        return (
            "You are a Vera autonomous agent. You work by calling TOOLS one at a time.\n\n"
            f"GOAL: {goal}\n\n"
            f"TRIAGE: category={triage.get('category')}, "
            f"keywords={', '.join(triage.get('keywords') or []) or '(none)'}\n\n"
            "============================================================\n"
            "YOUR TOOLKIT — CURATED FOR THIS GOAL. Use these tools first.\n"
            "============================================================\n"
            f"{_toolkit_block(kit)}\n\n"
            + (_fabric_usage_hints(kit))
            + "ON EACH TURN, RESPOND WITH EXACTLY ONE JSON OBJECT (no prose, no fences):\n"
            '  {"thought": "brief reasoning", "tool": "<cap.name>", "args": { ... }}\n'
            '  {"action": "done", "summary": "what was accomplished"}\n\n'
            + "RULES:\n"
            "1. PICK A TOOL FROM THE TOOLKIT on the FIRST turn.\n"
            "2. Only use tools currently in your toolkit.\n"
            "3. Inspect tool signatures (param:type, ! marks required).\n"
            "4. When the goal is achieved, emit done.\n"
            "5. Tool failures with bad-args trigger automatic recovery.\n"
            "6. expand_tools / caps.search are LAST RESORT.\n"
            + (
                ("\nWORK IN PHASES: THINK about what you must find out; EXPLORE with "
                 f"at least {min_explore_cycles} cheap read-only calls (get/list/search/"
                 "query/describe) FIRST — action and long-running tools are gated until "
                 "you have explored enough; then ACT; then VALIDATE the result with a "
                 "read-only check before you emit done.\n")
                if phased else ""
            )
            + (("\n" + ctx_extra) if ctx_extra else "")
        )

    # Maintain backward-compat lambda alias used inside the cycle loop
    base_sys = _build_sys

    # ── Register stream ───────────────────────────────────────────────────
    stream_id = await stream_register(
        kind          = "agent_loop_v2",
        source_cap    = "dag.agent_loop_v2",
        session_id    = session_id,
        label         = goal[:80],
        persist_full  = True,
        fabric_dataset = "streams.agent_loop_v2",
        metadata      = {"goal": goal, "max_cycles": max_cycles,
                          "triage": triage, "initial_toolkit": list(toolkit)},
    )

    history: List[Dict] = []
    cycles  = 0
    done    = False
    summary = ""
    expand_count = 0
    MAX_EXPANDS = 3

    # Quota counters
    SEARCH_CAPS = {"caps.search", "caps.describe", "context.search_caps", "context.search_dags"}
    search_count = 0
    productive_cycles = 0
    MAX_SEARCH_CALLS = max(1, int(max_search_calls))

    # ── Phase-model state (think → explore → act → validate) ────────────────
    phase = "think"
    explore_done = 0
    validated = False
    acted = False
    validation_requested = False
    auto_continues = 0

    async def _emit_phase_v2(new_phase: str, **extra):
        nonlocal phase
        phase = new_phase
        await emit_event({
            "type": "agent_loop_v2.phase",
            "stream_id": stream_id, "cycle": cycles, "phase": new_phase,
            "explore_done": explore_done, "min_explore": min_explore_cycles,
            "session_id": session_id, **extra,
        })

    def _explore_satisfied() -> bool:
        return (not phased) or explore_done >= min_explore_cycles

    async def _maybe_continue_v2() -> bool:
        nonlocal max_cycles, auto_continues
        if not allow_continue:
            return False
        await emit_event({
            "type": "agent_loop_v2.budget_pause",
            "stream_id": stream_id, "cycle": cycles,
            "cycles": cycles, "max_cycles": max_cycles,
            "increment": continue_increment, "step": -(cycles + 1),
            "auto_continues": auto_continues, "auto_continue_max": auto_continue_max,
            "session_id": session_id,
        })
        if auto_continue_max > 0 and auto_continues < auto_continue_max:
            auto_continues += 1
            max_cycles += continue_increment
            await emit_event({
                "type": "agent_loop_v2.budget_continue",
                "stream_id": stream_id, "cycle": cycles, "mode": "auto",
                "max_cycles": max_cycles, "auto_continues": auto_continues,
                "session_id": session_id,
            })
            return True
        decision_obj = await _dw_await_hitl(
            session_id, -(cycles + 1), timeout=float(max(hitl_timeout_secs, 600)),
        )
        decision = (decision_obj or {}).get("decision", "")
        if decision == "continue":
            inc = int((decision_obj or {}).get("increment") or continue_increment)
            max_cycles += max(1, inc)
            await emit_event({
                "type": "agent_loop_v2.budget_continue",
                "stream_id": stream_id, "cycle": cycles, "mode": "manual",
                "max_cycles": max_cycles, "session_id": session_id,
            })
            return True
        return False

    try:
        # Emit initial toolkit snapshot
        await emit_event({
            "type": "agent_loop_v2.toolkit",
            "stream_id": stream_id, "toolkit": list(toolkit),
            "session_id": session_id,
        })

        cycle_i = 0
        while True:
            _budget_hit = (cycle_i >= max_cycles) if count_failed_cycles \
                          else (productive_cycles >= max_cycles)
            if _budget_hit:
                if await _maybe_continue_v2():
                    pass  # budget extended
                else:
                    break
            if not count_failed_cycles and cycle_i >= max_cycles * 3:
                history.append({"tool": "(hard_limit)", "args": {},
                                "preview": f"Aborted: hit {cycle_i} iterations"})
                done = True; break
            cycle_i += 1
            cycles = cycle_i

            obs_block = "\n\n".join(
                f"[Observation {i+1}] tool={h['tool']}\n{h['preview']}"
                for i, h in enumerate(history[-6:])
            ) or "(no observations yet — make your first tool call or call caps.search to discover tools)"

            remaining_v2 = max_cycles - productive_cycles
            budget_note = ""
            if remaining_v2 <= 2:
                budget_note = (
                    f"\n\n⚠ BUDGET WARNING: Only {remaining_v2} cycle(s) remaining! "
                    "You MUST emit {\"action\":\"done\",\"summary\":\"<your answer>\"} NOW. "
                    "Summarize everything you've found so far into a final answer."
                )
            elif remaining_v2 <= 4:
                budget_note = (
                    f"\nNote: {remaining_v2} cycles remaining — start wrapping up. "
                    "If you have useful results, emit done soon."
                )

            phase_note = ""
            if phased:
                if not _explore_satisfied():
                    phase_note = (f"\n\nPHASE: EXPLORE ({explore_done}/{min_explore_cycles}). "
                                  "Use read-only tools to build a picture first; action and "
                                  "long-running tools are gated until then.")
                elif acted and require_validate and not validated:
                    phase_note = ("\n\nPHASE: VALIDATE. Verify your result with a read-only "
                                  "check before emitting done.")
                else:
                    phase_note = "\n\nPHASE: ACT. You have explored enough — take the action that advances the GOAL."
            user_msg = (
                "Continue. Recent tool results:\n\n"
                + obs_block
                + budget_note
                + phase_note
                + "\n\nEmit the next JSON action."
            )

            await emit_event({
                "type": "agent_loop_v2.cycle_planning",
                "stream_id": stream_id, "cycle": cycles,
                "session_id": session_id,
            })

            try:
                _sys = await base_sys(toolkit) if asyncio.iscoroutinefunction(base_sys) else base_sys(toolkit)
                raw = await _safe_ollama_generate(
                    user_msg, system=_sys,
                    model=model, instance_id=instance_id,
                    prefer_gpu=bool(prefer_gpu),
                    json_mode=True,
                )
            except Exception as e:
                _err_preview = f"Planner LLM call failed: {e}"
                history.append({"tool": "(planner_error)", "args": {},
                                "preview": _err_preview})
                await emit_event({
                    "type": "agent_loop_v2.tool_done",
                    "stream_id": stream_id, "cycle": cycles,
                    "tool": "(planner)", "ok": False,
                    "elapsed_ms": 0,
                    "preview": _err_preview[:400],
                    "error": str(e)[:500],
                    "session_id": session_id,
                })
                continue

            # Strip thinking tokens before parsing/streaming
            clean_raw, think_text = _strip_think(raw or "")
            if think_text:
                await stream_append_token(stream_id, f"\n[think #{cycles}] {think_text[:600]}\n")
                await emit_event({
                    "type": "agent_loop_v2.think",
                    "stream_id": stream_id, "cycle": cycles,
                    "thought": think_text[:2000], "session_id": session_id,
                })
            await stream_append_token(stream_id, f"\n[plan #{cycles}] {clean_raw[:600]}\n")

            action = None
            try:
                cleaned = clean_raw.strip()
                if cleaned.startswith("```"):
                    cleaned = cleaned.split("```", 2)[1]
                    if cleaned.startswith("json"):
                        cleaned = cleaned[4:]
                start = cleaned.find("{"); end = cleaned.rfind("}")
                if start >= 0 and end > start:
                    action = json.loads(cleaned[start:end+1])
            except Exception:
                action = None

            if not isinstance(action, dict):
                _parse_preview = f"Planner output was not parseable JSON: {(raw or '')[:300]}"
                history.append({"tool": "(parse_error)", "args": {},
                                "preview": _parse_preview})
                await emit_event({
                    "type": "agent_loop_v2.tool_done",
                    "stream_id": stream_id, "cycle": cycles,
                    "tool": "(planner)", "ok": False,
                    "elapsed_ms": 0,
                    "preview": _parse_preview[:400],
                    "error": _parse_preview[:500],
                    "session_id": session_id,
                })
                continue

            # ── Done? ────────────────────────────────────────────────────
            # The LLM sometimes emits done via different field patterns:
            #   {action: "done", summary: "..."} — canonical
            #   {tool: "(done)", ...} or {tool: "done", ...} — common mistake
            #   {final: "..."} or {answer: "..."} — also accepted
            _tool_raw = action.get("tool", "")
            _is_done_via_tool = (isinstance(_tool_raw, str)
                                  and _tool_raw.strip().strip("()").lower()
                                  in ("done", "finish", "stop", "complete", "final"))
            if (action.get("action") in ("done", "finish", "stop", "complete")
                    or action.get("final") or action.get("answer")
                    or _is_done_via_tool):
                # Validation gate: if the agent acted but never validated, push
                # back once before accepting done.
                if (phased and require_validate and acted and not validated
                        and not validation_requested):
                    validation_requested = True
                    await _emit_phase_v2("validate", reason="validation_required")
                    history.append({
                        "tool": "(validation_required)", "args": {}, "ok": False,
                        "preview": ("Before finishing: validate your result with a "
                                    "read-only check (re-query / re-read / confirm), "
                                    "then emit done — or state why validation is "
                                    "not possible."),
                    })
                    continue
                done    = True
                summary = (action.get("summary") or action.get("final")
                            or action.get("answer") or "")
                await stream_append_token(stream_id, f"\n[done] {summary}\n")
                await emit_event({
                    "type": "agent_loop_v2.done",
                    "stream_id": stream_id,
                    "summary": summary, "cycles": cycles,
                    "session_id": session_id,
                })
                break

            # ── Expand toolkit? ──────────────────────────────────────────
            if action.get("action") == "expand_tools":
                if not enable_expand or expand_count >= max(0, int(max_expands)):
                    # Force-finalise after 2 consecutive blocks
                    consec = sum(1 for h in history[-2:]
                                  if h.get("tool") == "(expand_blocked)")
                    if consec >= 2:
                        summary = f"Aborted: expand_tools blocked {consec+1}× in a row."
                        history.append({"tool": "(force_final)", "args": {},
                                         "preview": summary, "ok": False})
                        done = True
                        break
                    history.append({
                        "tool": "(expand_blocked)", "args": {},
                        "ok": False,
                        "preview": (f"Expand quota exhausted ({max_expands} expansions used). "
                                     "Pick from the existing toolkit or emit done."),
                    })
                    await emit_event({
                        "type": "agent_loop_v2.tool_done",
                        "stream_id": stream_id, "cycle": cycles,
                        "tool": "expand_tools", "ok": False,
                        "elapsed_ms": 0,
                        "preview": f"Expand quota exhausted ({max_expands} used)",
                        "error": "expand quota exhausted",
                        "session_id": session_id,
                    })
                    continue
                expand_count += 1
                # Normalize keywords — agents commonly emit a list, sometimes a
                # string, occasionally a dict. Coerce all of them to a single
                # search-friendly string. Without this we hit
                # "'list' object has no attribute 'strip'" on the LLM's first
                # array-style {action:expand_tools, keywords:[...]} reply.
                _raw_kw = action.get("keywords")
                if isinstance(_raw_kw, list):
                    kws = " ".join(str(x).strip() for x in _raw_kw if str(x).strip())
                elif isinstance(_raw_kw, dict):
                    kws = " ".join(str(v).strip() for v in _raw_kw.values() if str(v).strip())
                elif isinstance(_raw_kw, str):
                    kws = _raw_kw.strip()
                else:
                    kws = ""
                added: List[str] = []
                if kws and ds and getattr(ds, "CAP_INDEX", None):
                    try:
                        hits = await ds.CAP_INDEX.relevance_search(kws, top_k=8)
                        for n, _s in hits:
                            if (n in CAPABILITY_REGISTRY
                                    and n.split(".")[0] not in _CONTEXT_CAP_BLACKLIST_GROUPS
                                    and n not in _AGENT_LOOP_BLACKLIST
                                    and n not in toolkit):
                                toolkit.append(n)
                                added.append(n)
                                if len(added) >= 5:
                                    break
                    except Exception:
                        pass
                msg = (f"Expanded toolkit: added {', '.join(added) or '(no new caps found)'}. "
                       f"Keywords used: {kws}.")
                history.append({"tool": "(expand_tools)", "args": {"keywords": kws},
                                 "preview": msg})
                await emit_event({
                    "type": "agent_loop_v2.toolkit",
                    "stream_id": stream_id, "toolkit": list(toolkit),
                    "added": added, "session_id": session_id,
                })
                await emit_event({
                    "type": "agent_loop_v2.tool_done",
                    "stream_id": stream_id, "cycle": cycles,
                    "tool": "expand_tools", "ok": True,
                    "elapsed_ms": 0,
                    "preview": f"Expanded: +{len(added)} caps ({', '.join(added[:5]) or 'none found'})",
                    "session_id": session_id,
                })
                continue

            # ── Tool call ────────────────────────────────────────────────
            tool, args, _thought = _extract_tool_action(action, list(toolkit) + list(CAPABILITY_REGISTRY.keys()))
            if not isinstance(args, dict):
                args = {}

            # ── Cycle-1 discovery block ──
            _SEARCH_CAPS_V2 = {"caps.search", "caps.describe",
                                "context.search_caps", "context.search_dags"}
            if cycles == 1 and tool in _SEARCH_CAPS_V2 and len(toolkit) >= 5:
                _disc_preview = (
                    f"Skipped {tool} on cycle 1 — toolkit has {len(toolkit)} "
                    "curated tools. Read them and pick one."
                )
                history.append({
                    "tool": "(cycle1_discovery_blocked)",
                    "args": args, "ok": False,
                    "preview": _disc_preview,
                })
                await emit_event({
                    "type": "agent_loop_v2.tool_done",
                    "stream_id": stream_id, "cycle": cycles,
                    "tool": tool, "ok": False,
                    "elapsed_ms": 0,
                    "preview": _disc_preview[:400],
                    "error": "cycle-1 discovery blocked",
                    "session_id": session_id,
                })
                continue

            if tool not in toolkit:
                # Allow calls to any base discovery cap even if not listed yet
                if tool in CAPABILITY_REGISTRY and tool in _BASE_DISCOVERY_CAPS:
                    toolkit.append(tool)
                else:
                    _bad_tool_preview = (
                        f"ERROR: tool '{tool}' is not in your toolkit. "
                        f"Either pick from: {', '.join(toolkit[:10])}"
                        + ("…" if len(toolkit) > 10 else "")
                        + " — or emit {\"action\":\"expand_tools\",\"keywords\":\"…\"}"
                    )
                    history.append({
                        "tool": tool or "(none)", "args": args,
                        "preview": _bad_tool_preview,
                    })
                    await emit_event({
                        "type": "agent_loop_v2.tool_done",
                        "stream_id": stream_id, "cycle": cycles,
                        "tool": tool or "(none)", "ok": False,
                        "elapsed_ms": 0,
                        "preview": _bad_tool_preview[:400],
                        "error": f"Unknown tool: {tool}",
                        "session_id": session_id,
                    })
                    continue

            # ── Search quota enforcement ─────────────────────────────────
            if tool in SEARCH_CAPS:
                search_count += 1
                if search_count > MAX_SEARCH_CALLS:
                    _quota_preview = (
                        f"Search quota exhausted ({MAX_SEARCH_CALLS} calls). "
                        "Pick a tool from your toolkit or emit done."
                    )
                    history.append({
                        "tool": "(search_quota_exceeded)",
                        "args": {"tool": tool, "limit": MAX_SEARCH_CALLS},
                        "ok": False,
                        "preview": _quota_preview,
                    })
                    await emit_event({
                        "type": "agent_loop_v2.tool_done",
                        "stream_id": stream_id, "cycle": cycles,
                        "tool": tool, "ok": False,
                        "elapsed_ms": 0,
                        "preview": _quota_preview[:400],
                        "error": "search quota exceeded",
                        "session_id": session_id,
                    })
                    continue

            # ── Phase gate: explore before you act ──────────────────────────
            if phased and _dw_cap_phase(tool) == "act" and not _explore_satisfied():
                if long_running_force_hitl and _dw_is_long_running(tool):
                    await _emit_phase_v2("act", reason="long_running_pre_explore", tool=tool)
                    await emit_event({
                        "type": "agent_loop_v2.hitl_request",
                        "stream_id": stream_id, "cycle": cycles, "step": cycles - 1,
                        "tool": tool, "args": args,
                        "thought": action.get("thought", ""),
                        "reason": "long_running_pre_explore",
                        "session_id": session_id, "timeout_secs": hitl_timeout_secs,
                    })
                    decision_obj = await _dw_await_hitl(
                        session_id, cycles - 1, timeout=float(hitl_timeout_secs),
                    )
                    decision = (decision_obj or {}).get("decision", "")
                    if decision == "abort":
                        summary = "Aborted by user before a long-running tool ran."
                        done = True
                        await emit_event({
                            "type": "agent_loop_v2.done", "stream_id": stream_id,
                            "summary": summary, "cycles": cycles,
                            "reason": "hitl_abort", "session_id": session_id,
                        })
                        break
                    if decision == "reject":
                        history.append({
                            "tool": tool, "args": args, "ok": False,
                            "preview": ("HITL: user declined this long-running call before "
                                        "exploration. Gather context with read-only tools first."),
                        })
                        await emit_event({
                            "type": "agent_loop_v2.hitl_resolved", "stream_id": stream_id,
                            "cycle": cycles, "decision": "reject", "session_id": session_id,
                        })
                        continue
                    if decision == "edit":
                        _na = (decision_obj or {}).get("args") or {}
                        if isinstance(_na, dict):
                            args = _na
                    await emit_event({
                        "type": "agent_loop_v2.hitl_resolved", "stream_id": stream_id,
                        "cycle": cycles, "decision": decision or "auto_approve_timeout",
                        "session_id": session_id,
                    })
                else:
                    await _emit_phase_v2("explore", reason="act_blocked", tool=tool)
                    _blk = (f"BLOCKED: '{tool}' is an action tool, but you have only explored "
                            f"{explore_done}/{min_explore_cycles} times. Use read-only tools "
                            "(get/list/search/query/describe) to gather context first, then act.")
                    history.append({"tool": "(act_blocked)", "args": {"tool": tool},
                                     "ok": False, "preview": _blk})
                    await emit_event({
                        "type": "agent_loop_v2.tool_done", "stream_id": stream_id,
                        "cycle": cycles, "tool": tool, "ok": False, "elapsed_ms": 0,
                        "preview": _blk[:400], "error": "act blocked pre-explore",
                        "session_id": session_id,
                    })
                    continue

            productive_cycles += 1

            # Dedup detection — three identical in a row force termination
            if len(history) >= 2:
                last_two = history[-2:]
                cur_sig  = (tool, json.dumps(args, default=str, sort_keys=True))
                prev_sigs = [
                    (h.get("tool"), json.dumps(h.get("args"), default=str, sort_keys=True))
                    for h in last_two
                ]
                if all(s == cur_sig for s in prev_sigs):
                    summary = "Terminated: same tool with identical args was called 3 times in a row."
                    history.append({"tool": "(loop_break)", "args": {},
                                    "preview": summary})
                    done = True
                    await emit_event({
                        "type": "agent_loop_v2.done",
                        "stream_id": stream_id, "summary": summary,
                        "cycles": cycles, "reason": "dedup",
                        "session_id": session_id,
                    })
                    break

            await emit_event({
                "type": "agent_loop_v2.tool_call",
                "stream_id": stream_id, "cycle": cycles,
                "tool": tool, "args": args,
                "thought": action.get("thought", ""),
                "long_running": _local_is_long_running(tool),
                "session_id": session_id,
            })

            t0 = time.monotonic()
            invoke = await _agent_loop_call_tool(tool, args,
                                                  session_id=session_id,
                                                  trace_id=trace_id or "")

            # ── Promote inner errors so recovery can see them ──
            if invoke.get("ok") and isinstance(invoke.get("result"), dict):
                _ierr = invoke["result"].get("error")
                if _ierr:
                    invoke["ok"] = False
                    invoke["error"] = str(_ierr)

            # ── Error recovery: re-call same tool with fixed args ──
            if not invoke.get("ok"):
                recov = await _maybe_arg_recover(
                    cap_name=tool, failed_args=args,
                    error_text=invoke.get("error", ""),
                    model=model, instance_id=instance_id,
                    prefer_gpu=bool(prefer_gpu),
                    max_attempts=int(max_recovery_attempts),
                    call_tool=_agent_loop_call_tool,
                    session_id=session_id, trace_id=trace_id or "",
                    cycle=cycles, stream_id=stream_id, enabled=True,
                )
                if recov and recov.get("recovered"):
                    invoke = recov["final_invoke"]
                    last_a = recov.get("attempts") or []
                    if last_a:
                        args = last_a[-1].get("args", args)

            # ── Wait for long-running jobs (research, ml, exec, etc.) ──
            if invoke.get("ok") and isinstance(invoke.get("result"), dict):
                awaited = await _maybe_await_long_running(
                    cap_name=tool, result=invoke["result"],
                    session_id=session_id, trace_id=trace_id or "",
                    cycle=cycles, stream_id=stream_id,
                    max_wait_secs=float(long_running_timeout_secs),
                    enabled=bool(await_long_running),
                )
                invoke["result"] = awaited
                if isinstance(awaited, dict) and awaited.get("_await_error"):
                    invoke["ok"] = False
                    invoke["error"] = awaited["_await_error"]

            elapsed = round((time.monotonic() - t0) * 1000)

            preview = (_preview_for_loop(invoke["result"], tool=tool)
                        if invoke.get("ok")
                        else "ERROR: " + invoke.get("error", "unknown error"))

            history.append({
                "tool":    tool,
                "args":    args,
                "ok":      bool(invoke.get("ok")),
                "preview": preview,
                "ms":      elapsed,
                "thought": action.get("thought", ""),
            })
            await stream_append_token(
                stream_id,
                f"\n[exec #{cycles}] {tool}({json.dumps(args, default=str)[:200]}) → "
                f"{preview[:400]}\n"
            )
            await emit_event({
                "type": "agent_loop_v2.tool_done",
                "stream_id": stream_id, "cycle": cycles,
                "tool": tool, "ok": invoke.get("ok"),
                "elapsed_ms": elapsed, "preview": preview[:2000],
                "error": invoke.get("error", "") if not invoke.get("ok") else "",
                "session_id": session_id,
            })

            # ── Post-exec satisfaction check ─────────────────────────────
            if satisfaction_check and invoke.get("ok"):
                # Only check after non-discovery tools — discovery itself
                # rarely satisfies a goal on its own.
                if tool not in _BASE_DISCOVERY_CAPS:
                    try:
                        sat = await _check_goal_satisfied(
                            goal, preview,
                            model=model, instance_id=instance_id,
                            prefer_gpu=prefer_gpu,
                        )
                    except Exception:
                        sat = {"satisfied": False, "summary": ""}
                    if sat.get("satisfied"):
                        done    = True
                        summary = sat.get("summary") or "Goal satisfied."
                        await stream_append_token(
                            stream_id, f"\n[auto-done] {summary}\n"
                        )
                        await emit_event({
                            "type":      "agent_loop_v2.done",
                            "stream_id": stream_id,
                            "summary":   summary,
                            "cycles":    cycles,
                            "reason":    "satisfaction_check",
                            "session_id": session_id,
                        })
                        break

    finally:
        try:
            await stream_complete(stream_id)
        except Exception:
            pass

    # ── Auto-summary when budget exhausted without done ──────────────────
    if not done and not summary and history:
        # Build a terse summary from the last few successful tool results
        ok_steps = [h for h in history if h.get("ok") and not h.get("tool","").startswith("(")]
        if ok_steps:
            last_previews = [h.get("preview","")[:200] for h in ok_steps[-3:]]
            summary = (
                f"Budget exhausted ({cycles} cycles) before the agent emitted done. "
                f"Last {len(ok_steps)} successful tool calls produced: "
                + " | ".join(last_previews)
            )
        else:
            summary = f"Budget exhausted ({cycles} cycles) — no successful tool calls."
        done = True
        await emit_event({
            "type":      "agent_loop_v2.done",
            "stream_id": stream_id,
            "summary":   summary,
            "cycles":    cycles,
            "reason":    "budget_exhausted",
            "session_id": session_id,
        })

    return {
        "goal":      goal,
        "triage":    triage,
        "toolkit":   toolkit,
        "history":   history,
        "cycles":    cycles,
        "done":      done,
        "summary":   summary,
        "stream_id": stream_id,
        "phase":         phase,
        "explore_done":  explore_done,
        "validated":     validated,
        "auto_continues": auto_continues,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Module bootstrap message
# ─────────────────────────────────────────────────────────────────────────────
log.info("vera_context loaded — context.* + stream.* + dag.agent_loop[_v2] caps registered")