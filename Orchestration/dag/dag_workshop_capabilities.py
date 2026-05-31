"""
dag_workshop_capabilities.py  —  DAG Workshop panel + helper caps
==================================================================

Registers a dedicated DAG Workshop UI panel with rich features for:

  • Browsing the DAG library with semantic search & tag filtering
  • Visual DAG creation (drag/drop or text editor)
  • Cap palette with fuzzy-search (uses context.search_caps)
  • Promoting DAGs to live capabilities (calls dag.register)
  • Launching agentic-DAG flows in three variants:
      - v1: ReAct loop                (dag.agent_loop)
      - v2: triage + dynamic toolkit  (dag.agent_loop_v2)
      - openclaw: full message-history Anthropic-style observe/think/act
                  (dag.agent_loop_openclaw — defined here)
  • Reviewing / approving / retrying / editing planned tool calls (HITL)
  • Composing custom agentic flows from primitives (the Loop Builder pane)
  • Rich tool-call progress including long-running research jobs &
    streaming LLM tokens

Helper capabilities registered
──────────────────────────────
  workshop.dag_to_cap_preview     — preview cap signature for a stored DAG
  workshop.tag_cloud              — aggregate tag/category counts
  workshop.cap_tree               — caps grouped by namespace prefix
  workshop.cap_signature_rich     — full schema sig including enums + sub-schemas
                                    (used by both the LLM prompts and the UI)
  workshop.history_to_dag         — convert an agent-loop history into a saved DAG
                                    keeping only working (ok=true) tool calls
  workshop.list_loop_variants     — describe the available loop variants for the UI
  dag.agent_loop_openclaw         — Anthropic-style observe/think/act loop with
                                    full message history and explicit tool_use blocks

HTTP endpoints
──────────────
  /workshop/panel                  GET   — the panel HTML
  /workshop/agent_loop/stream      POST  — SSE stream of an agent run
  /workshop/agent_loop/hitl/respond POST — approve/reject/edit a paused step

Dependencies
────────────
  • context.py        (for stream registration + dag.agent_loop / _v2)
  • dag_store.py      (for DAG_STORE / CAP_INDEX)
  • capability_orchestration.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Request
from fastapi.responses import HTMLResponse, StreamingResponse

import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, capability, emit_event, now_iso,
    register_ui,
)

log = logging.getLogger("vera.dag_workshop")

_HERE = Path(__file__).parent


def _redis():     return _orch.REDIS
def _ctx():       return sys.modules.get("vera_context") or sys.modules.get("context")
def _dag_store(): return sys.modules.get("Vera.Orchestration.dag.dag_store")


# ═════════════════════════════════════════════════════════════════════════════
# RICH CAPABILITY SIGNATURE
# ─────────────────────────────────────────────────────────────────────────────
# The default cap_signature only emits "name:type!" for each parameter, which
# leaves the LLM blind to enum options and nested-object sub-schemas. Most of
# the "missing required arg" errors in agent-loop runs come from the model
# inventing key names because it has no idea what shape the param expects.
#
# This produces a multi-line block per cap that includes:
#   • required marker (!) and default value
#   • parameter description
#   • enum options when present (literal valid values)
#   • nested object/array shape (one level of properties recursed)
#
# Both the agent loops AND the UI palette tooltip use this.
# ═════════════════════════════════════════════════════════════════════════════

def _format_param_detail(pname: str, pschema: Dict[str, Any], required: bool,
                          indent: str = "      ") -> str:
    """Render a single parameter with full detail (type, default, enum, sub-schema, desc)."""
    ptype = pschema.get("type", "string")
    parts: List[str] = []

    head = f"{pname}: {ptype}"
    if required:
        head += "  [REQUIRED]"
    if "default" in pschema and pschema.get("default") is not None:
        try:
            d = pschema["default"]
            head += f"  (default: {json.dumps(d, default=str)[:60]})"
        except Exception:
            pass
    parts.append(indent + "- " + head)

    desc = pschema.get("description", "") or ""
    if desc:
        for line in desc[:280].split("\n"):
            line = line.strip()
            if line:
                parts.append(indent + "    " + line)

    enum_vals = pschema.get("enum")
    if enum_vals:
        try:
            parts.append(indent + "    valid options: " +
                          " | ".join(json.dumps(v, default=str) for v in enum_vals))
        except Exception:
            pass

    for key in ("anyOf", "oneOf"):
        opts = pschema.get(key)
        if isinstance(opts, list) and opts:
            shapes = []
            for o in opts:
                if isinstance(o, dict):
                    if "enum" in o:
                        shapes.extend(json.dumps(v, default=str) for v in o["enum"])
                    elif "type" in o:
                        shapes.append(o["type"])
            if shapes:
                parts.append(indent + f"    {key}: " + " | ".join(shapes[:8]))

    if ptype == "object":
        nested = pschema.get("properties") or {}
        if nested:
            req_set = set(pschema.get("required", []) or [])
            parts.append(indent + "    fields:")
            for nname, nspec in list(nested.items())[:12]:
                ntype = nspec.get("type", "any") if isinstance(nspec, dict) else "any"
                ndesc = nspec.get("description", "") if isinstance(nspec, dict) else ""
                marker = "!" if nname in req_set else ""
                line = indent + f"      .{nname}: {ntype}{marker}"
                if ndesc:
                    line += f" — {ndesc[:80]}"
                parts.append(line)
                if isinstance(nspec, dict) and nspec.get("enum"):
                    try:
                        parts.append(indent + "        valid: " +
                                     " | ".join(json.dumps(v, default=str) for v in nspec["enum"]))
                    except Exception:
                        pass

    if ptype == "array":
        items = pschema.get("items")
        if isinstance(items, dict):
            itype = items.get("type", "any")
            parts.append(indent + f"    items: {itype}")
            if items.get("enum"):
                try:
                    parts.append(indent + "      valid values: " +
                                 " | ".join(json.dumps(v, default=str) for v in items["enum"]))
                except Exception:
                    pass

    return "\n".join(parts)


def rich_cap_signature(name: str, *, max_param_detail: int = 12) -> str:
    """
    Multi-line signature for a capability with full parameter detail.
    The agent loops use this so the LLM can supply correct args.
    """
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap:
        return f"  {name}(unknown)"

    schema = cap.get("schema", {}) or {}
    props  = schema.get("properties", {}) or {}
    req    = set(schema.get("required", []) or [])
    desc   = (cap.get("description") or "")[:300]

    short_params = ", ".join(
        f"{p}:{v.get('type','str')}{'!' if p in req else ''}"
        for p, v in props.items() if p != "trace_id"
    )
    out: List[str] = [f"  {name}({short_params})"]
    if desc:
        out.append(f"    → {desc}")

    detail_count = 0
    for pname, pschema in props.items():
        if pname == "trace_id":
            continue
        if not isinstance(pschema, dict):
            continue
        is_required = pname in req
        has_extra = bool(
            pschema.get("enum") or pschema.get("description")
            or pschema.get("properties") or pschema.get("items")
            or pschema.get("anyOf") or pschema.get("oneOf")
        )
        if not (is_required or has_extra) and detail_count >= max_param_detail:
            continue
        out.append(_format_param_detail(pname, pschema, is_required))
        detail_count += 1
        if detail_count >= max_param_detail:
            break

    io = cap.get("io")
    if io and getattr(io, "outputs", None):
        out.append("    writes:")
        for k, d in list(io.outputs.items())[:8]:
            out.append(f"      {k} — {(d or '')[:80]}")

    return "\n".join(out)


@capability(
    "workshop.cap_signature_rich", memory="off", silent=True,
    http_method="POST", http_path="/workshop/cap_signature_rich",
    http_tags=["workshop", "caps"],
    description="Return a multi-line signature for one or more capabilities, "
                "including required markers, defaults, descriptions, enum "
                "options, and nested-object fields. The agent loops use this "
                "to give the LLM enough info to produce correct args. "
                "Input: names (csv str! — comma-separated cap names) OR name "
                "(str — single cap). "
                "Output: {sigs: {name: signature_string}, block: combined_string}.",
)
async def cap_workshop_cap_signature_rich(name: str = "", names: str = "",
                                            trace_id=None):
    targets: List[str] = []
    if name:
        targets.append(name)
    if names:
        targets += [n.strip() for n in names.split(",") if n.strip()]
    targets = list(dict.fromkeys(targets))

    sigs: Dict[str, str] = {}
    for n in targets:
        sigs[n] = rich_cap_signature(n)

    return {
        "sigs":  sigs,
        "block": "\n\n".join(sigs.values()),
        "count": len(sigs),
    }


# ═════════════════════════════════════════════════════════════════════════════
# DAG PROMOTION PREVIEW (now accepts both `id` and legacy `dag_id`)
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "workshop.dag_to_cap_preview", memory="off",
    http_method="POST", http_path="/workshop/dag/cap_preview",
    http_tags=["workshop", "dag"],
    description="Preview the cap signature that would be registered if this DAG "
                "were promoted to a live capability via dag.register. "
                "Accepts either dag_id/dag_name (legacy) or id/name. "
                "Output: {cap_name, signature, schema, inputs, outputs}.",
)
async def cap_workshop_dag_preview(dag_id: str = "", dag_name: str = "",
                                     id: str = "", name: str = "",
                                     trace_id=None):
    ds = _dag_store()
    if not ds or not getattr(ds, "DAG_STORE", None):
        return {"error": "DAG_STORE not available"}

    eff_id   = id or dag_id
    eff_name = name or dag_name

    rec = None
    if eff_id:
        rec = await ds.DAG_STORE.get(eff_id)
    if not rec and eff_name:
        rec = await ds.DAG_STORE.get_by_name(eff_name)
    if not rec:
        return {"error": f"DAG not found: {eff_id or eff_name}"}

    safe_name = rec.name.lower().replace(" ", "_").replace("/", ".")
    cap_name  = f"dag.{safe_name}"

    inputs = sorted(list((rec.initial_state or {}).keys()))
    outputs = []
    for node in (rec.dag or []):
        if isinstance(node, list):
            if node and isinstance(node[0], list):
                for sub in node:
                    if isinstance(sub, list) and len(sub) >= 2 and sub[1]:
                        outputs.append(sub[1])
            else:
                if len(node) >= 2 and node[1]:
                    outputs.append(node[1])

    sig_params = ", ".join(f"{i}:str" for i in inputs)
    signature = f"{cap_name}({sig_params}) — {(rec.description or rec.name)[:120]}"

    return {
        "cap_name":   cap_name,
        "signature":  signature,
        "inputs":     inputs,
        "outputs":    sorted(set(outputs)),
        "step_count": len(rec.dag or []),
        "tags":       rec.tags,
        "category":   rec.category,
    }


# ═════════════════════════════════════════════════════════════════════════════
# TAG CLOUD + CAP TREE
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "workshop.tag_cloud", memory="off", silent=True,
    http_method="GET", http_path="/workshop/tag_cloud",
    http_tags=["workshop", "dag"],
    description="Aggregate tag counts across all stored DAGs for the workshop "
                "library filter UI. Output: {tags: [{tag, count}], categories: [{name, count}]}",
)
async def cap_workshop_tag_cloud(trace_id=None):
    ds = _dag_store()
    if not ds or not getattr(ds, "DAG_STORE", None):
        return {"tags": [], "categories": []}
    try:
        recs = await ds.DAG_STORE.list_all(include_archived=False)
    except Exception:
        return {"tags": [], "categories": []}

    tag_counts: Dict[str, int] = {}
    cat_counts: Dict[str, int] = {}
    for r in recs:
        for t in r.tags or []:
            tag_counts[t] = tag_counts.get(t, 0) + 1
        c = r.category or "general"
        cat_counts[c] = cat_counts.get(c, 0) + 1

    tags = sorted(
        [{"tag": k, "count": v} for k, v in tag_counts.items()],
        key=lambda x: x["count"], reverse=True,
    )
    cats = sorted(
        [{"name": k, "count": v} for k, v in cat_counts.items()],
        key=lambda x: x["count"], reverse=True,
    )
    return {"tags": tags, "categories": cats, "total_dags": len(recs)}


@capability(
    "workshop.cap_tree", memory="off", silent=True,
    http_method="GET", http_path="/workshop/cap_tree",
    http_tags=["workshop", "dag"],
    description="List capabilities grouped by namespace prefix (the part before "
                "the first dot) for the workshop palette tree. Each entry "
                "includes name, signature, required-param list, enum hints, "
                "and io descriptor.",
)
async def cap_workshop_cap_tree(query: str = "", trace_id=None):
    q = (query or "").lower()
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for nm, cap in CAPABILITY_REGISTRY.items():
        if nm.split(".")[0] in {"obs", "syslog", "ui", "echo", "debug",
                                 "system", "memory", "health"}:
            continue
        if q:
            hay = (nm + " " + (cap.get("description") or "")).lower()
            if q not in hay:
                continue

        props  = cap.get("schema", {}).get("properties", {}) or {}
        req    = list(cap.get("schema", {}).get("required", []) or [])
        io     = cap.get("io")
        params = []
        for p, v in props.items():
            if p == "trace_id":
                continue
            params.append({
                "name":        p,
                "type":        v.get("type", "string"),
                "required":    p in req,
                "default":     v.get("default"),
                "description": v.get("description", ""),
                "enum":        v.get("enum"),
                "properties":  v.get("properties"),  # nested schema
                "items":       v.get("items"),       # array item info
            })

        outputs = []
        if io and getattr(io, "outputs", None):
            for k, d in io.outputs.items():
                outputs.append({"name": k, "description": d})

        prefix = nm.split(".")[0]
        groups.setdefault(prefix, []).append({
            "name":         nm,
            "description":  (cap.get("description") or "")[:300],
            "tags":         cap.get("tags", []),
            "params":       params,
            "outputs":      outputs,
            "source":       cap.get("source", "local"),
            "streams":      cap.get("streams", []),
            "long_running": _is_long_running_cap(nm),
        })

    for k in groups:
        groups[k].sort(key=lambda x: x["name"])
    sorted_groups = [
        {"prefix": k, "count": len(v), "caps": v}
        for k, v in sorted(groups.items())
    ]
    return {"groups": sorted_groups, "total": sum(g["count"] for g in sorted_groups)}


# ═════════════════════════════════════════════════════════════════════════════
# LONG-RUNNING CAP DETECTION
# ─────────────────────────────────────────────────────────────────────────────
# The agent loop emits "tool_progress" events for caps known to be long-running
# so the UI can show a live progress indicator. Detection is heuristic: caps
# whose group is known to take time, or whose decorator declares streams=[...],
# are flagged.
# ═════════════════════════════════════════════════════════════════════════════

_LONG_RUNNING_GROUPS = {
    "research", "ml_training", "ml_workshop", "exec", "ide_code",
    "vllm", "browser", "scrape",
}
_LONG_RUNNING_NAME_HINTS = {
    "research.run", "research.report", "research.parallel", "research.deep",
    "research.guide", "research.code", "research.filestore",
    "ml_training.start", "ml.train", "ml.fit",
    "exec.run", "exec.bash", "exec.shell", "exec.python",
    "browser.navigate", "browser.action",
    "llm.generate",
}

def _is_long_running_cap(name: str) -> bool:
    if name in _LONG_RUNNING_NAME_HINTS:
        return True
    g = name.split(".")[0]
    if g in _LONG_RUNNING_GROUPS:
        return True
    cap = CAPABILITY_REGISTRY.get(name) or {}
    streams = cap.get("streams") or []
    return bool(streams)


# ═════════════════════════════════════════════════════════════════════════════
# LONG-RUNNING JOB AWAITING
# ─────────────────────────────────────────────────────────────────────────────
# Caps like research.run / ml.train return a job_id immediately and let an
# external worker complete the actual work. For the agent loop and DAG runs to
# be useful, the system must wait for the real result before treating the call
# as "done" — otherwise the agent gets a {job_id} blob and has no clue whether
# the actual research finished.
#
# This module defines:
#   • LONG_RUNNING_AWAIT_MAP   — cap_name → (status_cap, status_args_factory,
#                                            done_predicate, result_extractor)
#   • _await_job_via_status    — polls the status cap until done_predicate true
#   • _maybe_await_long_result — given an immediate cap result + cap_name,
#                                returns the awaited result or the original
# ═════════════════════════════════════════════════════════════════════════════

# How to build {kwargs} for the status cap from the immediate result dict
def _research_status_args(immediate: Dict[str, Any]) -> Dict[str, Any]:
    return {"job_id": immediate.get("job_id", "")}


# ──────────────────────────────────────────────────────────────────────────────
# RESEARCH WEBSOCKET STREAMER
# ──────────────────────────────────────────────────────────────────────────────
# When a research.* cap returns a job_id, we want to:
#   1. Connect to ws://{researcher}/ws/stream/{job_id}
#   2. Forward each {type:"token"} as stream.token events into the loop's stream
#   3. Forward {type:"step", "citations", "file_*"} as agent_loop.research_*
#   4. Return the final result on {type:"done"}
#
# Falls back to polling-only if websockets is unavailable or WS connect fails.
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# JOBS REGISTRY — in-memory record of awaited/streaming jobs across loops
# ──────────────────────────────────────────────────────────────────────────────
# Each agent loop run that calls _universal_await_job / _stream_research_websocket
# registers the job here so the UI Observatory can show what's happening.
# ──────────────────────────────────────────────────────────────────────────────

_AWAITED_JOBS: Dict[str, Dict[str, Any]] = {}
_AWAITED_JOBS_HISTORY: List[Dict[str, Any]] = []  # capped at 200
_AWAITED_JOBS_HISTORY_MAX = 200

def _jobs_register(*, job_id: str, cap_name: str, session_id: str = "",
                     stream_id: str = "", cycle: int = 0, mode: str = "polling"):
    if not job_id:
        return
    _AWAITED_JOBS[job_id] = {
        "job_id":     job_id,
        "cap":        cap_name,
        "session_id": session_id,
        "stream_id":  stream_id,
        "cycle":      cycle,
        "mode":       mode,            # "polling" | "websocket"
        "status":     "starting",
        "started_at": time.time(),
        "tokens":     0,
        "steps":      0,
        "last_event": time.time(),
        "preview":    "",
    }

def _jobs_update(job_id: str, **fields):
    rec = _AWAITED_JOBS.get(job_id)
    if not rec:
        return
    rec.update(fields)
    rec["last_event"] = time.time()

def _jobs_finish(job_id: str, *, status: str = "completed",
                   result_preview: str = "", error: str = ""):
    rec = _AWAITED_JOBS.pop(job_id, None)
    if not rec:
        return
    rec["status"] = status
    rec["finished_at"] = time.time()
    rec["elapsed"] = round(rec["finished_at"] - rec.get("started_at", rec["finished_at"]), 1)
    if result_preview:
        rec["preview"] = result_preview[:400]
    if error:
        rec["error"] = error[:300]
    _AWAITED_JOBS_HISTORY.append(rec)
    if len(_AWAITED_JOBS_HISTORY) > _AWAITED_JOBS_HISTORY_MAX:
        _AWAITED_JOBS_HISTORY.pop(0)


@capability(
    "workshop.jobs_observatory",
    http_method="GET", http_path="/workshop/jobs_observatory",
    http_tags=["workshop", "agent_loop"],
    description="List active long-running jobs being awaited by agent loops and "
                "DAG runs, plus recent history. Used by the Jobs & Streams Observatory "
                "panel. "
                "Output: {active: [{job_id, cap, session_id, stream_id, cycle, mode, "
                "status, started_at, tokens, steps, preview}], "
                "history: [<same shape with finished_at, elapsed>], "
                "stats: {active_count, completed_count}}.",
)
async def cap_workshop_jobs_observatory(trace_id=None):
    return {
        "active":  list(_AWAITED_JOBS.values()),
        "history": list(reversed(_AWAITED_JOBS_HISTORY[-50:])),
        "stats":   {
            "active_count":    len(_AWAITED_JOBS),
            "completed_count": len(_AWAITED_JOBS_HISTORY),
        },
    }


_RESOLVED_RESEARCHER_URL: Optional[str] = None


def _resolve_researcher_url() -> str:
    """Resolve the base URL of the researcher's HTTP/WS server.

    In Vera mode the researcher's routes (incl. /ws/stream/{job_id}) are
    mounted on the orchestrator's own FastAPI app, which uvicorn serves on
    port 8999 (see capability_orchestration.py). The historical default of
    localhost:8765 only applies to the *standalone* researcher_api process
    and causes [Errno 111] Connection refused when nothing listens there.

    Resolution order:
      1. VERA_RESEARCHER_URL env var (explicit override — always wins)
      2. researcher_api.app._vera_base_url  (set if the app exposed one)
      3. Vera.Orchestration.researcher_api.RESEARCHER_URL  (module attr)
      4. In-process orchestrator: detect _VERA_MODE and use the orchestrator
         port (VERA_ORCH_PORT env or 8999) on loopback — guaranteed reachable
         because it is the very process we are running inside.
      5. Last resort: localhost:8765 (standalone researcher default)
    """
    global _RESOLVED_RESEARCHER_URL
    if _RESOLVED_RESEARCHER_URL:
        return _RESOLVED_RESEARCHER_URL

    import os as _os
    url = _os.environ.get("VERA_RESEARCHER_URL", "").strip()

    if not url:
        try:
            from researcher_api import app as _rapp  # type: ignore
            url = (getattr(_rapp, "_vera_base_url", "") or "").strip()
        except Exception:
            pass

    if not url:
        try:
            import importlib
            rc = importlib.import_module("Vera.Orchestration.researcher_api")
            url = (getattr(rc, "RESEARCHER_URL", "") or "").strip()
        except Exception:
            pass

    if not url:
        # In-process detection: if the researcher is running in Vera mode its
        # WS route is on the orchestrator app, served on the orchestrator port.
        in_vera = False
        try:
            import importlib
            rc = importlib.import_module("Vera.Orchestration.researcher_api")
            in_vera = bool(getattr(rc, "_VERA_MODE", False))
        except Exception:
            pass
        if in_vera:
            port = _os.environ.get("VERA_ORCH_PORT", "8999").strip() or "8999"
            url = f"http://localhost:{port}"
            log.info("research WS: in-process Vera mode — using orchestrator "
                     "URL %s", url)

    if not url:
        url = "http://localhost:8765"
        log.warning("research WS: VERA_RESEARCHER_URL unset and not in Vera "
                    "mode — falling back to standalone default %s", url)

    _RESOLVED_RESEARCHER_URL = url
    return url


async def _stream_research_websocket(*, job_id: str, cap_name: str,
                                       immediate: Dict[str, Any],
                                       session_id: str = "",
                                       cycle: int = 0,
                                       max_wait_secs: float = 1800.0,
                                       stream_id: str = "") -> Optional[Dict[str, Any]]:
    """Connect to the researcher WebSocket, forward tokens/steps into the loop
    stream, and return the final result dict on `done`. Returns None if the WS
    is unavailable so the caller can fall back to polling.
    """
    try:
        import websockets  # type: ignore
    except Exception:
        log.debug("websockets pkg not available, falling back to polling")
        return None

    # Resolve researcher URL — see _resolve_researcher_url() for the full
    # resolution chain (env var → in-process orchestrator port → defaults).
    _RURL = _resolve_researcher_url()

    ws_url = (_RURL.replace("http://", "ws://").replace("https://", "wss://")
              + "/ws/stream/" + job_id)

    # Lazily resolve the loop's stream-token writer
    import importlib
    try:
        ctx_mod = importlib.import_module("Vera.Orchestration.context")
        stream_append = getattr(ctx_mod, "stream_append_token", None)
    except Exception:
        stream_append = None

    started = time.monotonic()
    final_result: Optional[Dict[str, Any]] = None
    token_count = 0
    step_count = 0
    citations: List[Any] = []
    file_tree: List[Any] = []

    _jobs_register(job_id=job_id, cap_name=cap_name,
                    session_id=session_id, stream_id=stream_id,
                    cycle=cycle, mode="websocket")
    await emit_event({
        "type":       "agent_loop.research_stream_open",
        "tool":       cap_name,
        "job_id":     job_id,
        "ws_url":     ws_url,
        "cycle":      cycle,
        "session_id": session_id,
    })

    try:
        async with websockets.connect(ws_url, open_timeout=10,
                                        ping_interval=30, close_timeout=5) as ws:
            while True:
                if time.monotonic() - started > max_wait_secs:
                    await emit_event({
                        "type":     "agent_loop.long_running_await_timeout",
                        "tool":     cap_name, "job_id": job_id,
                        "elapsed":  int(time.monotonic() - started),
                        "session_id": session_id, "cycle": cycle,
                    })
                    break
                try:
                    raw_msg = await asyncio.wait_for(ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    # Idle ping — keep looping
                    continue
                except Exception as e:
                    log.debug("research ws recv error: %s", e)
                    break

                try:
                    msg = json.loads(raw_msg)
                except Exception:
                    continue
                mtype = msg.get("type", "")

                if mtype == "token":
                    token_count += 1
                    if token_count % 5 == 0:
                        _jobs_update(job_id, tokens=token_count, status="streaming")
                    txt = msg.get("text") or msg.get("token") or ""
                    if txt and stream_append and stream_id:
                        try:
                            await stream_append(stream_id, txt)
                        except Exception:
                            pass
                    # NOTE: stream_append_token already emits a stream.token
                    # event internally (context.py emit_event). We do NOT emit
                    # a second one here — that was causing doubled text in the UI.
                    # If stream_append is unavailable, emit directly as fallback.
                    elif txt:
                        await emit_event({
                            "type":       "stream.token",
                            "stream_id":  stream_id,
                            "token":      txt,
                            "source":     "research",
                            "job_id":     job_id,
                            "cycle":      cycle,
                            "session_id": session_id,
                        })
                elif mtype == "thinking":
                    # Surface thinking tokens separately so they can be hidden/shown
                    await emit_event({
                        "type":       "agent_loop.research_thinking",
                        "stream_id":  stream_id,
                        "text":       msg.get("text", ""),
                        "job_id":     job_id, "cycle": cycle,
                        "session_id": session_id,
                    })
                elif mtype == "step":
                    step_count += 1
                    _jobs_update(job_id, steps=step_count,
                                  preview=(msg.get("label","") or "")[:200])
                    await emit_event({
                        "type":       "agent_loop.research_step",
                        "stream_id":  stream_id,
                        "label":      msg.get("label", ""),
                        "detail":     msg.get("detail", ""),
                        "job_id":     job_id, "cycle": cycle,
                        "session_id": session_id,
                    })
                elif mtype == "citations":
                    citations = msg.get("citations") or []
                    await emit_event({
                        "type":       "agent_loop.research_citations",
                        "stream_id":  stream_id,
                        "count":      len(citations),
                        "job_id":     job_id, "cycle": cycle,
                        "session_id": session_id,
                    })
                elif mtype == "file_tree":
                    file_tree = msg.get("files") or []
                elif mtype == "file_created":
                    await emit_event({
                        "type":       "agent_loop.research_file",
                        "path":       msg.get("path", ""),
                        "job_id":     job_id, "cycle": cycle,
                        "session_id": session_id,
                    })
                elif mtype == "error":
                    # The researcher emits {type:"error"} for NON-FATAL sub-step
                    # failures (e.g. a single source returned 422, an upstream
                    # API is rate-limited, etc.) — the job itself usually keeps
                    # running and produces a {type:"done"} eventually. The
                    # research panel's own WS handler treats these as
                    # non-terminal and keeps listening; orchestration should
                    # too, otherwise we mark the call failed prematurely and
                    # the agent loop is forced to retry with a different cap
                    # (which happens to resume the same job via dedupe — but
                    # that's accidental recovery, not correct behaviour).
                    err_text = (msg.get("text") or msg.get("error")
                                or "stream error")
                    _jobs_update(job_id,
                                  preview=("⚠ " + str(err_text))[:200],
                                  status="streaming")
                    await emit_event({
                        "type":       "agent_loop.research_step",
                        "stream_id":  stream_id,
                        "label":      "warning",
                        "detail":     str(err_text)[:300],
                        "job_id":     job_id, "cycle": cycle,
                        "session_id": session_id,
                    })
                    # Keep the partial error text in final_result so that if
                    # the WS closes WITHOUT a 'done' message we still surface
                    # what went wrong instead of returning bare immediate.
                    if final_result is None:
                        final_result = {
                            **(immediate or {}),
                            "job_id":  job_id,
                            "warning": str(err_text)[:500],
                        }
                    else:
                        final_result["warning"] = str(err_text)[:500]
                    # Do NOT break — wait for done or genuine WS close.
                    continue
                elif mtype == "done":
                    # The done message carries the final result + status
                    _jobs_finish(job_id, status="completed",
                                  result_preview=str(msg.get("result",""))[:400])
                    res = msg.get("result", "") or ""
                    final_result = {
                        **(immediate or {}),
                        "job_id":     job_id,
                        "status":     msg.get("status") or "completed",
                        "result":     res,
                        "report":     res,    # alias for downstream extractors
                        "elapsed":    msg.get("elapsed"),
                        "citations":  citations or msg.get("citations") or [],
                        "file_tree":  file_tree or msg.get("file_tree") or [],
                        "finished_at": msg.get("finished_at") or time.time(),
                    }
                    break
    except Exception as e:
        log.debug("research websocket failed (%s): %s — falling back to polling",
                   ws_url, e)
        await emit_event({
            "type":       "agent_loop.research_stream_failed",
            "tool":       cap_name, "job_id": job_id,
            "error":      str(e)[:200],
            "session_id": session_id, "cycle": cycle,
        })
        return None

    elapsed = int(time.monotonic() - started)
    await emit_event({
        "type":       "agent_loop.research_stream_done",
        "tool":       cap_name,
        "job_id":     job_id,
        "tokens":     token_count,
        "steps":      step_count,
        "citations":  len(citations),
        "elapsed":    elapsed,
        "session_id": session_id, "cycle": cycle,
    })

    if final_result is None:
        # WS closed without `done` — return None to trigger polling fallback
        return None
    return final_result

def _ml_train_status_args(immediate: Dict[str, Any]) -> Dict[str, Any]:
    return {"job_id": immediate.get("job_id", "")}

def _research_done(status_result: Any) -> bool:
    if not isinstance(status_result, dict):
        return False
    # Check explicit terminal status FIRST (handles "JobStatus.QUEUED"/"queued"/etc.)
    s = str(status_result.get("status") or status_result.get("state") or "").lower().strip()
    # Strip enum-style prefix like "JobStatus.QUEUED" → "queued"
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    # "not_found" is NOT terminal — the job may still be registering, or the
    # status lookup is transiently failing. Keep polling; the await loop's
    # own max_wait_secs timeout is the real backstop.
    if s in {"queued", "pending", "running", "in_progress", "started", "submitted",
             "active", "processing", "analysing", "analyzing", "directing",
             "not_found", "unknown", "thinking", "searching", "crawling",
             "architecting", "coding", "reviewing", "writing", "verifying",
             "chaining"}:
        return False
    if s in {"completed", "complete", "done", "finished", "finalized",
             "succeeded", "success", "failed", "error", "errored", "stopped",
             "cancelled", "canceled", "timed_out", "timeout"}:
        return True
    # Fallback: only consider "done" if a TERMINAL field has a NON-NULL value.
    # Never treat presence-of-key alone as done (research.run returns
    # {result: null, finished_at: null} on QUEUED — not finished!).
    for k in ("result", "answer", "report", "output", "finished_at", "completed_at"):
        v = status_result.get(k)
        if v is not None and v != "" and v != []:
            return True
    # An 'error' field only counts as terminal if it is NOT a transient
    # not-found / still-running signal. A bare "Job <id> not found" means the
    # status cap could not locate the job yet — keep polling, do not finish.
    err = str(status_result.get("error", "")).lower()
    if err:
        transient = ("still" in err or "running" in err or "pending" in err
                     or "not found" in err or "not_found" in err
                     or "queued" in err)
        if not transient:
            return True
    return False

def _ml_train_done(status_result: Any) -> bool:
    if not isinstance(status_result, dict):
        return False
    s = (status_result.get("status") or "").lower()
    return s in {"completed", "done", "finished", "error", "failed", "stopped", "cancelled"}

def _passthrough_extract(status_result: Any) -> Any:
    return status_result

LONG_RUNNING_AWAIT_MAP: Dict[str, Dict[str, Any]] = {
    "research.run":      {"status_cap": "research.job.status",
                           "args": _research_status_args,
                           "done": _research_done,
                           "extract": _passthrough_extract},
    "research.report":   {"status_cap": "research.job.status",
                           "args": _research_status_args,
                           "done": _research_done,
                           "extract": _passthrough_extract},
    "research.parallel": {"status_cap": "research.job.status",
                           "args": _research_status_args,
                           "done": _research_done,
                           "extract": _passthrough_extract},
    "research.deep":     {"status_cap": "research.job.status",
                           "args": _research_status_args,
                           "done": _research_done,
                           "extract": _passthrough_extract},
    "research.guide":    {"status_cap": "research.job.status",
                           "args": _research_status_args,
                           "done": _research_done,
                           "extract": _passthrough_extract},
    "research.code":     {"status_cap": "research.job.status",
                           "args": _research_status_args,
                           "done": _research_done,
                           "extract": _passthrough_extract},
    "research.filestore":{"status_cap": "research.job.status",
                           "args": _research_status_args,
                           "done": _research_done,
                           "extract": _passthrough_extract},
    "research.quick_search":{"status_cap": "research.job.status",
                              "args": _research_status_args,
                              "done": _research_done,
                              "extract": _passthrough_extract},
    "research.academic": {"status_cap": "research.job.status",
                           "args": _research_status_args,
                           "done": _research_done,
                           "extract": _passthrough_extract},
    "research.security": {"status_cap": "research.job.status",
                           "args": _research_status_args,
                           "done": _research_done,
                           "extract": _passthrough_extract},
    "research.analysis": {"status_cap": "research.job.status",
                           "args": _research_status_args,
                           "done": _research_done,
                           "extract": _passthrough_extract},
    "ml.train":          {"status_cap": "ml.train.status",
                           "args": _ml_train_status_args,
                           "done": _ml_train_done,
                           "extract": _passthrough_extract},
}


async def _await_job_via_status(cap_name: str, immediate: Dict[str, Any], *,
                                  poll_interval: float = 4.0,
                                  max_wait_secs: float = 1800.0,
                                  session_id: str = "",
                                  trace_id: str = "",
                                  cycle: int = 0) -> Dict[str, Any]:
    """Poll the status cap until done. Emits awaiting events for UI feedback.

    Returns the awaited result dict (the status cap's output once `done()` true),
    or the immediate dict augmented with `_await_error` on timeout/failure.
    """
    spec = LONG_RUNNING_AWAIT_MAP.get(cap_name)
    if not spec:
        return immediate

    status_cap_name = spec["status_cap"]
    status_cap = CAPABILITY_REGISTRY.get(status_cap_name)
    if not status_cap:
        log.debug("await: %s status cap %s not registered", cap_name, status_cap_name)
        return immediate

    args = spec["args"](immediate or {})
    if not args.get("job_id"):
        log.debug("await: %s did not return a job_id, skipping wait", cap_name)
        return immediate

    job_id = args["job_id"]
    started = time.monotonic()
    polls   = 0
    last_status_payload: Any = None

    # ── For research caps: try WebSocket streaming first ──────────────────
    if cap_name.startswith("research."):
        try:
            ws_result = await _stream_research_websocket(
                job_id=job_id, cap_name=cap_name,
                immediate=immediate, session_id=session_id,
                cycle=cycle, max_wait_secs=max_wait_secs,
                stream_id=(immediate or {}).get("_stream_id", ""),
            )
            if ws_result is not None:
                return ws_result
        except Exception as _ws_e:
            log.debug("WS streaming failed in legacy await: %s", _ws_e)

    await emit_event({
        "type":       "agent_loop.long_running_await_start",
        "tool":       cap_name,
        "job_id":     job_id,
        "status_cap": status_cap_name,
        "session_id": session_id,
        "cycle":      cycle,
    })

    # For research jobs, emit the WebSocket URL so the panel can stream tokens live
    if cap_name.startswith("research.") and job_id:
        _RURL = _resolve_researcher_url()
        if _RURL:
            ws_url = _RURL.replace("http://", "ws://").replace("https://", "wss://")
            await emit_event({
                "type":    "agent_loop.research_stream_hint",
                "tool":    cap_name,
                "job_id":  job_id,
                "ws_url":  ws_url + "/ws/stream/" + job_id,
                "cycle":   cycle,
                "session_id": session_id,
            })

    while True:
        polls += 1
        elapsed = time.monotonic() - started
        if elapsed > max_wait_secs:
            _jobs_finish(job_id, status="timeout",
                          error=f"timed out after {int(elapsed)}s")
            await emit_event({
                "type":       "agent_loop.long_running_await_timeout",
                "tool":       cap_name,
                "job_id":     job_id,
                "elapsed":    int(elapsed),
                "session_id": session_id,
                "cycle":      cycle,
            })
            return {**(immediate or {}),
                    "_await_error": f"timed out waiting for {cap_name} job {job_id}",
                    "_last_status": last_status_payload,
                    "_polls": polls}
        try:
            status_payload = await status_cap["func"](
                **args, trace_id=trace_id or "")
        except Exception as e:
            log.debug("await poll %s: %s", cap_name, e)
            status_payload = {"error": str(e)}
        last_status_payload = status_payload

        # Periodic progress event for the UI
        if polls == 1 or polls % 3 == 0:
            await emit_event({
                "type":       "agent_loop.long_running_await_tick",
                "tool":       cap_name,
                "job_id":     job_id,
                "polls":      polls,
                "elapsed":    int(elapsed),
                "status":     (status_payload or {}).get("status", "")
                              if isinstance(status_payload, dict) else "",
                "session_id": session_id,
                "cycle":      cycle,
            })

        if spec["done"](status_payload):
            break
        # Backoff: 4s for first 30s, then 10s thereafter
        wait = poll_interval if elapsed < 30 else max(poll_interval, 10.0)
        await asyncio.sleep(wait)

    extracted = spec["extract"](last_status_payload)
    await emit_event({
        "type":       "agent_loop.long_running_await_done",
        "tool":       cap_name,
        "job_id":     job_id,
        "elapsed":    int(time.monotonic() - started),
        "polls":      polls,
        "session_id": session_id,
        "cycle":      cycle,
    })
    # Merge job_id into the awaited payload so downstream still sees it
    if isinstance(extracted, dict):
        extracted = {**(immediate or {}), **extracted}
    return extracted if extracted is not None else immediate


def _should_await(cap_name: str) -> bool:
    """Stub — kept for backwards compat. Real decision is now made AT
    invocation time by inspecting the immediate result for a job_id (see
    _detect_job_id_in_result + _resolve_status_cap)."""
    return cap_name in LONG_RUNNING_AWAIT_MAP


# ─── Universal long-running detection ───────────────────────────────────────
# Beyond the static LONG_RUNNING_AWAIT_MAP, we also handle ANY cap that
# returns a job_id in its result. The strategy:
#
# 1. Detect a job_id in the immediate result (`job_id`/`jobId`/`id`/`task_id`).
# 2. Resolve a status cap, in this order:
#       (a) explicit hint in the result (`status_cap`, `_status_cap`)
#       (b) `<cap.group>.job.status` (e.g. research.run → research.job.status)
#       (c) `<cap.group>.status`     (e.g. ml.train    → ml.train.status)
#       (d) `<cap_name>.status`       (e.g. exec.bash.run → exec.bash.run.status)
#       (e) static LONG_RUNNING_AWAIT_MAP if matching
# 3. Poll the status cap with {job_id} until completion, with backoff.

_JOB_ID_KEYS = ("job_id", "jobId", "task_id", "taskId", "run_id", "runId")
_STATUS_CAP_HINT_KEYS = ("status_cap", "_status_cap", "status_capability")


def _detect_job_id(result: Any) -> Optional[str]:
    """Return the job_id from an immediate cap result, or None."""
    if not isinstance(result, dict):
        return None
    # Direct keys
    for k in _JOB_ID_KEYS:
        v = result.get(k)
        if v and isinstance(v, (str, int)):
            return str(v)
    # Some caps put it inside nested 'job' / 'task' / 'data'
    for wrapper in ("job", "task", "data"):
        sub = result.get(wrapper)
        if isinstance(sub, dict):
            for k in _JOB_ID_KEYS:
                v = sub.get(k)
                if v and isinstance(v, (str, int)):
                    return str(v)
    # Some caps use the result's `id` field as the job id when status is set
    if (result.get("status") in ("queued", "running", "pending", "submitted",
                                   "in_progress", "started")
            and result.get("id") and isinstance(result["id"], (str, int))):
        return str(result["id"])
    return None


def _resolve_status_cap(cap_name: str, result: Dict[str, Any]) -> Optional[str]:
    """Pick the best status cap to poll for this (cap_name, result) pair."""
    # 1. Explicit hint in result
    for k in _STATUS_CAP_HINT_KEYS:
        v = result.get(k)
        if isinstance(v, str) and v in CAPABILITY_REGISTRY:
            return v
    # 2. Static map (still respected for known caps)
    spec = LONG_RUNNING_AWAIT_MAP.get(cap_name)
    if spec and spec.get("status_cap") in CAPABILITY_REGISTRY:
        return spec["status_cap"]
    # 3. Convention: <group>.job.status
    parts = cap_name.split(".")
    candidates: List[str] = []
    if len(parts) >= 1:
        candidates.append(f"{parts[0]}.job.status")
        candidates.append(f"{parts[0]}.status")
    if len(parts) >= 2:
        candidates.append(f"{parts[0]}.{parts[1]}.status")
        candidates.append(f"{parts[0]}.{parts[1]}.job.status")
    candidates.append(f"{cap_name}.status")
    for c in candidates:
        if c in CAPABILITY_REGISTRY:
            return c
    return None


def _is_terminal_status_payload(payload: Any) -> bool:
    """Best-effort 'is the job done' check across status-cap return shapes."""
    if not isinstance(payload, dict):
        return False
    s = str(payload.get("status") or payload.get("state") or "").lower().strip()
    # Strip enum-style prefix like "JobStatus.QUEUED" → "queued"
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    # Explicit running states beat any field-presence heuristic
    if s in {"queued", "pending", "running", "in_progress", "started", "submitted",
             "active", "processing", "analysing", "analyzing", "directing"}:
        return False
    if s in {"completed", "complete", "done", "finished", "finalized",
             "succeeded", "success", "failed", "error", "errored", "stopped",
             "cancelled", "canceled", "timed_out", "timeout"}:
        return True
    # Field-presence ONLY counts if value is non-null/non-empty
    for k in ("result", "answer", "report", "output", "finished_at", "completed_at"):
        v = payload.get(k)
        if v is not None and v != "" and v != []:
            return True
    # A hard error from the status cap itself
    err = str(payload.get("error", "")).lower()
    if err and "still" not in err and "running" not in err and "pending" not in err:
        return True
    return False


async def _universal_await_job(*, cap_name: str,
                                  immediate: Dict[str, Any],
                                  session_id: str = "",
                                  trace_id: str = "",
                                  cycle: int = 0,
                                  max_wait_secs: float = 1800.0,
                                  poll_interval: float = 4.0,
                                  emit_progress: bool = True,
                                  stream_id: str = "") -> Dict[str, Any]:
    """Generic poll-until-done. Returns the awaited payload (status cap's
    output once terminal) merged with the original immediate result. If no
    job_id is detectable or no status cap can be resolved, returns the
    original immediate dict unchanged.

    For research.* caps, attempts WebSocket streaming first (forwarding
    tokens into the loop's stream as they arrive). Falls back to polling
    if the WS is unavailable.

    This is the function v1/v2/openclaw/DAG runs all should call.
    """
    if not isinstance(immediate, dict):
        return immediate

    job_id = _detect_job_id(immediate)
    if not job_id:
        return immediate

    # Prefer the explicit stream_id arg, fall back to one carried in immediate
    if not stream_id:
        try:
            stream_id = (immediate or {}).get("_stream_id") or ""
        except Exception:
            stream_id = ""

    # ── For research caps: try WebSocket streaming first ──────────────────
    # This forwards tokens into the loop's stream as they arrive AND awaits
    # completion. Falls through to polling on failure.
    if cap_name.startswith("research."):
        ws_result = await _stream_research_websocket(
            job_id=job_id, cap_name=cap_name,
            immediate=immediate, session_id=session_id,
            cycle=cycle, max_wait_secs=max_wait_secs,
            stream_id=stream_id,
        )
        if ws_result is not None:
            return ws_result
        # else: WS failed, fall through to polling

    status_cap_name = _resolve_status_cap(cap_name, immediate)
    if not status_cap_name:
        if emit_progress:
            await emit_event({
                "type":         "agent_loop.long_running_await_skipped",
                "tool":         cap_name,
                "reason":       "no_status_cap_resolved",
                "job_id":       job_id,
                "tried":        [f"{cap_name.split('.')[0]}.job.status",
                                 f"{cap_name.split('.')[0]}.status"],
                "session_id":   session_id, "cycle": cycle,
            })
        return immediate

    status_cap = CAPABILITY_REGISTRY[status_cap_name]
    started = time.monotonic()
    polls = 0
    last_payload: Any = None

    _jobs_register(job_id=job_id, cap_name=cap_name,
                    session_id=session_id, stream_id=stream_id,
                    cycle=cycle, mode="polling")
    if emit_progress:
        await emit_event({
            "type":       "agent_loop.long_running_await_start",
            "tool":       cap_name,
            "job_id":     job_id,
            "status_cap": status_cap_name,
            "session_id": session_id,
            "cycle":      cycle,
        })
        # For research jobs, tell the panel where to open a live WebSocket stream
        if cap_name.startswith("research.") and job_id:
            _RURL = _resolve_researcher_url()
            if _RURL:
                _ws = _RURL.replace("http://", "ws://").replace("https://", "wss://")
                await emit_event({
                    "type":    "agent_loop.research_stream_hint",
                    "tool":    cap_name,
                    "job_id":  job_id,
                    "ws_url":  _ws + "/ws/stream/" + job_id,
                    "cycle":   cycle,
                    "session_id": session_id,
                })

    # Build args for the status cap. Most accept job_id; some prefer id.
    accepted = set(status_cap.get("schema", {}).get("properties", {}).keys()) | {"trace_id"}

    while True:
        polls += 1
        elapsed = time.monotonic() - started
        if elapsed > max_wait_secs:
            if emit_progress:
                await emit_event({
                    "type": "agent_loop.long_running_await_timeout",
                    "tool": cap_name, "job_id": job_id,
                    "elapsed": int(elapsed),
                    "session_id": session_id, "cycle": cycle,
                })
            return {**immediate,
                    "_await_error": f"timed out waiting for {cap_name} job {job_id}",
                    "_last_status": last_payload, "_polls": polls}

        # Try multiple kwarg names since status caps vary
        kwargs = {"trace_id": trace_id}
        if "job_id" in accepted: kwargs["job_id"] = job_id
        elif "id" in accepted:    kwargs["id"]    = job_id
        elif "task_id" in accepted: kwargs["task_id"] = job_id
        elif "run_id" in accepted:  kwargs["run_id"]  = job_id
        try:
            payload = await status_cap["func"](**kwargs)
        except Exception as e:
            payload = {"error": str(e)}
        last_payload = payload

        if emit_progress and (polls == 1 or polls % 3 == 0):
            await emit_event({
                "type":       "agent_loop.long_running_await_tick",
                "tool":       cap_name,
                "job_id":     job_id,
                "polls":      polls,
                "elapsed":    int(elapsed),
                "status":     (payload or {}).get("status", "")
                              if isinstance(payload, dict) else "",
                "session_id": session_id, "cycle": cycle,
            })

        if _is_terminal_status_payload(payload):
            break

        # Backoff: 4s for first 30s, then 10s after
        wait = poll_interval if elapsed < 30 else max(poll_interval, 10.0)
        await asyncio.sleep(wait)

    _jobs_finish(job_id, status="completed",
                  result_preview=str((last_payload or {}).get("result",""))[:400]
                                 if isinstance(last_payload, dict) else "")
    if emit_progress:
        await emit_event({
            "type":       "agent_loop.long_running_await_done",
            "tool":       cap_name,
            "job_id":     job_id,
            "elapsed":    int(time.monotonic() - started),
            "polls":      polls,
            "session_id": session_id, "cycle": cycle,
        })

    if isinstance(last_payload, dict):
        return {**immediate, **last_payload}
    return immediate


# ═════════════════════════════════════════════════════════════════════════════
# REPETITION DETECTOR  (prevents the openclaw "context.search_caps loop")
# ─────────────────────────────────────────────────────────────────────────────
# Hash recent (tool, args) pairs in history. If the agent emits the same pair
# 3+ times without intervening progress, force-inject a stop instruction.
# ═════════════════════════════════════════════════════════════════════════════

def _args_hash(args: Any) -> str:
    try:
        return json.dumps(args, sort_keys=True, default=str)[:240]
    except Exception:
        return repr(args)[:240]


def _detect_repetition(history: List[Dict[str, Any]], tool: str, args: Any,
                        *, lookback: int = 4, threshold: int = 2) -> bool:
    """True if (tool, args_hash) has appeared `threshold` times in last `lookback` cycles."""
    if not history:
        return False
    h = _args_hash(args)
    recent = history[-lookback:]
    same = sum(1 for r in recent
               if r.get("tool") == tool and _args_hash(r.get("args")) == h)
    return same >= threshold


# ═════════════════════════════════════════════════════════════════════════════
# HISTORY → DAG  (save an agent loop run as a stored DAG)
# ─────────────────────────────────────────────────────────────────────────────
# Takes a list of {tool, args, ok, ...} dicts and produces a DAG containing
# only the working tool calls. Each successful call becomes a node:
#
#   [tool_name, output_key, condition, input_map, output_map]
#
# Inputs that came from initial state get put in initial_state literally; ones
# that referenced a previous output get wired up via input_map.
# ═════════════════════════════════════════════════════════════════════════════

def _safe_key(s: str, prefix: str = "step") -> str:
    """Coerce arbitrary text into a snake_case state key."""
    keep = "abcdefghijklmnopqrstuvwxyz0123456789_"
    s = (s or "").lower().replace(".", "_").replace("-", "_").replace(" ", "_")
    out = "".join(ch if ch in keep else "_" for ch in s).strip("_") or prefix
    return out[:60]


@capability(
    "workshop.history_to_dag", memory="on",
    http_method="POST", http_path="/workshop/history_to_dag",
    http_tags=["workshop", "dag"],
    description="Convert an agent-loop history (list of cycle dicts) into a "
                "stored DAG, keeping ONLY successful tool calls (ok=true) and "
                "skipping parser errors, expand_tools, loop_break, and any "
                "step that errored. "
                "Input: history (list of dicts!), name (str!), description (str), "
                "tags (csv str), category (str default agent), goal (str), "
                "save (bool default True). "
                "Output: {dag_id, name, dag, initial_state, kept, skipped}.",
)
async def cap_workshop_history_to_dag(
    history: list = None,
    name:    str  = "",
    description: str = "",
    tags:    str  = "",
    category: str = "agent",
    goal:    str  = "",
    save:    bool = True,
    trace_id=None,
):
    ds = _dag_store()
    if not ds or not getattr(ds, "DAG_STORE", None):
        return {"error": "DAG_STORE not available"}
    if not history:
        return {"error": "history is required (the agent loop's history list)"}
    if not name:
        return {"error": "name is required"}

    META_TOOLS = {"(parse_error)", "(planner_error)", "(none)",
                  "(loop_break)", "(expand_tools)", "(expand_blocked)"}
    kept:    List[Dict] = []
    skipped: List[Dict] = []
    for h in history:
        if not isinstance(h, dict):
            continue
        tool = h.get("tool", "")
        if tool in META_TOOLS or tool.startswith("("):
            skipped.append({"tool": tool, "reason": "meta_tool"})
            continue
        if h.get("ok") is False:
            skipped.append({"tool": tool, "reason": "errored",
                             "preview": (h.get("preview") or "")[:120]})
            continue
        if tool not in CAPABILITY_REGISTRY:
            skipped.append({"tool": tool, "reason": "cap_no_longer_registered"})
            continue
        kept.append(h)

    if not kept:
        return {"error": "no successful tool calls in history",
                "skipped": skipped}

    initial_state: Dict[str, Any] = {}
    if goal:
        initial_state["goal"] = goal

    dag_nodes: List[List] = []
    used_keys: set = set()
    name_counts: Dict[str, int] = {}

    for i, h in enumerate(kept):
        tool = h["tool"]
        args = h.get("args") or {}

        base = _safe_key(tool.split(".")[-1] or tool, prefix=f"step{i+1}")
        if base in name_counts:
            name_counts[base] += 1
            out_key = f"{base}_{name_counts[base]}"
        else:
            name_counts[base] = 1
            out_key = base if base not in used_keys else f"{base}_1"
        used_keys.add(out_key)

        input_map: Dict[str, str] = {}
        for k, v in args.items():
            if k == "trace_id":
                continue
            state_key = f"{out_key}_arg_{k}"
            initial_state[state_key] = v
            input_map[k] = state_key

        dag_nodes.append([
            tool,
            out_key,
            None,
            input_map or None,
            None,
        ])

    save_result: Dict[str, Any] = {}
    if save:
        try:
            cap_save = CAPABILITY_REGISTRY.get("dag.store_save")
            if not cap_save:
                return {"error": "dag.store_save capability not available"}
            save_result = await cap_save["func"](
                name          = name,
                dag           = json.dumps(dag_nodes),
                description   = description or (
                    f"Saved from agent loop run. Goal: {goal[:200]}"
                    if goal else "Saved from agent loop run."
                ),
                tags          = tags or "agent,from_loop",
                category      = category or "agent",
                initial_state = json.dumps(initial_state),
                rationale     = f"Auto-extracted from agent loop history, kept {len(kept)} of {len(history)} steps",
                trace_id      = trace_id,
            )
        except Exception as e:
            log.warning("history_to_dag save failed: %s", e)
            save_result = {"error": str(e)}

    return {
        "name":          name,
        "dag":           dag_nodes,
        "initial_state": initial_state,
        "kept":          [{"tool": h["tool"], "args": h.get("args", {})} for h in kept],
        "skipped":       skipped,
        "kept_count":    len(kept),
        "skipped_count": len(skipped),
        "save_result":   save_result,
    }


# ═════════════════════════════════════════════════════════════════════════════
# LOOP VARIANTS REGISTRY
# ═════════════════════════════════════════════════════════════════════════════

_LOOP_VARIANTS = [
    {
        "id":       "v1",
        "cap":      "dag.agent_loop",
        "label":    "v1 — simple ReAct",
        "description": "Plain observe/think/act loop with a fixed seed toolkit "
                        "selected by goal-keyword relevance.",
        "supports_satisfaction": False,
        "supports_expand":       False,
        "supports_progress":     True,
        "supports_hitl":         False,
    },
    {
        "id":       "v2",
        "cap":      "dag.agent_loop_v2",
        "label":    "v2 — triage + dynamic toolkit",
        "description": "Triages the goal first, seeds the toolkit by category, "
                        "supports mid-run toolkit expansion, and runs a "
                        "satisfaction check after every tool result.",
        "supports_satisfaction": True,
        "supports_expand":       True,
        "supports_progress":     True,
        "supports_hitl":         False,
    },
    {
        "id":       "openclaw",
        "cap":      "dag.agent_loop_openclaw",
        "label":    "Openclaw — full message history",
        "description": "Anthropic-style observe/think/act loop. Maintains the "
                        "full message history, emits explicit tool_use blocks, "
                        "and respects HITL approval before each action when "
                        "configured.",
        "supports_satisfaction": True,
        "supports_expand":       True,
        "supports_progress":     True,
        "supports_hitl":         True,
    },
]


@capability(
    "workshop.list_loop_variants", memory="off", silent=True,
    http_method="GET", http_path="/workshop/loop_variants",
    http_tags=["workshop"],
    description="Describe the available agent-loop variants for the workshop UI. "
                "Output: {variants: [...]}",
)
async def cap_workshop_list_loop_variants(trace_id=None):
    return {"variants": _LOOP_VARIANTS}


# ═════════════════════════════════════════════════════════════════════════════
# HANDOVER STAGE — synthesise a real answer from a completed loop run
# ─────────────────────────────────────────────────────────────────────────────
# The agent's own `final` is frequently terse, evasive, or a count rather
# than the answer the user wanted. The handover stage runs a SEPARATE LLM
# pass with no tools — input is the full history of tool calls + previews
# + the goal + the agent's own final. Output is a real synthesised answer.
#
# Triggered by:
#   • openclaw cap-arg `handover=True`
#   • SSE wrapper request body field `handover: true`
#   • Standalone cap `workshop.handover` (POST /workshop/handover)
#
# Emits:
#   agent_loop.handover_start
#   stream.token (during synthesis — wired through the existing SSE bridge)
#   agent_loop.handover_done {output, length}
# ═════════════════════════════════════════════════════════════════════════════

def _format_history_for_handover(history: List[Dict[str, Any]],
                                   max_chars: int = 18000) -> str:
    """Render the full agent history into a compact text block for the LLM.

    Truncates intelligently — keeps step headers always, trims long
    previews proportionally so the budget is met.
    """
    blocks = []
    total = 0
    META = {"(parse_error)", "(planner_error)", "(none)", "(loop_break)",
            "(expand_tools)", "(expand_blocked)", "(repetition_block)"}
    # First pass: build full blocks (always include header, truncate preview)
    work_steps = [h for h in history if h.get("tool") not in META]
    if not work_steps:
        work_steps = history  # fall back to all if no real work
    per_step_budget = max(800, (max_chars - 600) // max(1, len(work_steps)))

    for i, h in enumerate(work_steps, 1):
        tool   = h.get("tool", "?")
        ok     = bool(h.get("ok"))
        ms     = h.get("ms", 0)
        args   = h.get("args", {})
        prev   = h.get("preview", "") or ""
        thought = h.get("thought", "") or ""
        try:
            args_s = json.dumps(args, default=str, ensure_ascii=False)
        except Exception:
            args_s = str(args)
        if len(args_s) > 240:
            args_s = args_s[:240] + "…"

        if len(prev) > per_step_budget:
            prev = prev[:per_step_budget] + f"\n[…truncated {len(prev) - per_step_budget} chars…]"

        block = (
            f"--- Step {i}: {tool} ({'ok' if ok else 'ERROR'}, {ms}ms) ---\n"
            + (f"thought: {thought}\n" if thought else "")
            + f"args:    {args_s}\n"
            + f"result:  {prev}\n"
        )
        if total + len(block) > max_chars:
            blocks.append(f"\n[…{len(work_steps) - i + 1} more steps elided due to budget…]\n")
            break
        blocks.append(block)
        total += len(block)

    return "\n".join(blocks)


async def _run_handover_stage(*, goal: str,
                                history: List[Dict[str, Any]],
                                triage: Dict[str, Any],
                                cur_final: str = "",
                                model: str = "",
                                instance_id: str = "",
                                prefer_gpu: bool = True,
                                max_chars: int = 20000,
                                session_id: str = "") -> str:
    """Run the handover-stage LLM pass. Returns synthesised text."""
    ctx = _ctx()
    ollama_generate = getattr(ctx, "ollama_generate", None) if ctx else None
    if ollama_generate is None:
        return ""

    history_block = _format_history_for_handover(history, max_chars=max_chars)
    cat = (triage or {}).get("category", "other")

    sys = (
        "You are a senior synthesis agent. Another agent just executed a "
        "series of tool calls to satisfy the user's goal. Your job is to "
        "review ALL the tool calls and their results, then write the BEST "
        "POSSIBLE FINAL ANSWER to the user's original goal — directly, "
        "concretely, and without meta-commentary about the agent's process.\n\n"
        "RULES:\n"
        "  • Address the user's goal directly. Do not say 'eight relevant "
        "results were returned' — open them up and synthesize what they "
        "actually found.\n"
        "  • If the goal asked for a report, write the report. If it asked "
        "for a fact, give the fact. If it asked for a recommendation, give "
        "the recommendation.\n"
        "  • Use plain markdown — sections with ## headings if the answer "
        "is long; bullets for lists; nothing else.\n"
        "  • If the tool calls FAILED to gather enough information, say so "
        "honestly and explain what's missing — do not pretend to have an "
        "answer you don't have.\n"
        "  • Do not mention 'the agent', 'tools', or 'cycles' unless the "
        "user explicitly asked about the process.\n"
        "  • Cite specific facts from the tool results when you make claims."
    )
    user_prompt = (
        f"USER'S ORIGINAL GOAL:\n{goal.strip()}\n\n"
        f"GOAL CATEGORY: {cat}\n\n"
        f"AGENT'S OWN ATTEMPTED ANSWER (often terse — improve on it):\n"
        f"{(cur_final or '(none)').strip()[:1500]}\n\n"
        f"FULL HISTORY OF TOOL CALLS AND RESULTS:\n"
        f"{history_block}\n\n"
        "Now write the final answer to the user's goal. Begin with the "
        "answer itself — no preamble like 'Based on the tool results…'."
    )

    await emit_event({
        "type": "agent_loop.handover_start",
        "session_id": session_id, "category": cat,
        "history_len": len(history),
        "history_chars": len(history_block),
    })

    try:
        try:
            text = await ollama_generate(
                user_prompt, system=sys,
                model=model or None,
                instance_id=instance_id or None,
                prefer_gpu=bool(prefer_gpu),
                json_mode=False,
                temperature=0.3,
            )
        except TypeError:
            text = await ollama_generate(
                user_prompt, system=sys,
                model=model or None,
                instance_id=instance_id or None,
                prefer_gpu=bool(prefer_gpu),
                json_mode=False,
            )
    except Exception as e:
        await emit_event({
            "type": "agent_loop.handover_error",
            "error": str(e), "session_id": session_id,
        })
        return ""

    text = (text or "").strip()
    # Strip code fences if the LLM wrapped its answer
    if text.startswith("```"):
        try:
            text = text.split("```", 2)[1]
            text = text.split("\n", 1)[1] if "\n" in text else text
            text = text.rstrip("`").strip()
        except Exception:
            pass

    await emit_event({
        "type": "agent_loop.handover_done",
        "session_id": session_id, "output": text[:4000],
        "length": len(text),
    })
    return text


@capability(
    "workshop.handover", memory="off",
    http_method="POST", http_path="/workshop/handover",
    http_tags=["workshop", "agents"],
    description=(
        "Standalone handover-stage synthesis. Given a goal + history of tool "
        "calls (typically from a completed agent loop run), runs a separate "
        "LLM pass to produce a real synthesised answer. Use this to clean up "
        "a terse agent final, or to re-synthesise from history without "
        "re-running the loop. "
        "Inputs: goal (str!), history (list[object]!), triage (object), "
        "cur_final (str), model (str), instance_id (str), prefer_gpu (bool), "
        "max_chars (int default 20000). "
        "Output: {output, length}."
    ),
)
async def cap_workshop_handover(goal: str = "",
                                  history: list = None,
                                  triage: dict = None,
                                  cur_final: str = "",
                                  model: str = "",
                                  instance_id: str = "",
                                  prefer_gpu: bool = True,
                                  max_chars: int = 20000,
                                  session_id: str = "",
                                  trace_id=None):
    if not goal:
        return {"error": "goal required"}
    if not isinstance(history, list) or not history:
        return {"error": "history must be a non-empty list of step dicts"}
    out = await _run_handover_stage(
        goal=goal, history=history,
        triage=triage or {}, cur_final=cur_final,
        model=model, instance_id=instance_id, prefer_gpu=prefer_gpu,
        max_chars=int(max_chars),
        session_id=session_id or str(uuid.uuid4()),
    )
    return {"output": out, "length": len(out)}


# ═════════════════════════════════════════════════════════════════════════════
# OPENCLAW — full-message-history loop with HITL-friendly tool_use blocks
# ─────────────────────────────────────────────────────────────────────────────
# Modeled after Anthropic's tool-use harness:
#   • System prompt is fixed; user/assistant message pairs accumulate
#   • Each assistant turn emits {thought, tool_use:{name, input}} or {final}
#   • Each tool result is appended as a user message
#   • Long-running tools have their progress events forwarded by the SSE bridge
#   • HITL: when require_approval=True, every planned tool_use pauses the loop
#     until /workshop/agent_loop/hitl/respond resolves it
# ═════════════════════════════════════════════════════════════════════════════

# session_id → {step_index → asyncio.Future}
_HITL_PENDING_LOOP: Dict[str, Dict[int, asyncio.Future]] = {}


# ──────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT TEMPLATING
# ──────────────────────────────────────────────────────────────────────────────
# Users can override the default system prompt for any agent loop variant by
# passing `system_prompt_template`. The template supports the following tokens:
#
#   {goal}             — the user-supplied goal string
#   {category}         — triage category (research, web_check, ...)
#   {keywords}         — triage keywords joined by ", "
#   {reasoning}        — triage reasoning string
#   {toolkit}          — full multi-line toolkit block with rich signatures
#   {toolkit_brief}    — comma-separated cap names only
#   {toolkit_count}    — number of caps in the toolkit
#   {ctx_extra}        — skill/ontology context block (may be empty)
#   {expand_help}      — the expand_tools help line (empty if disabled)
#   {cap:<name>:desc}  — a specific cap's description
#   {cap:<name>:sig}   — a specific cap's full signature
#
# Unknown placeholders are left as-is. The template author is responsible for
# ensuring the JSON-action protocol survives the rewrite.
# ──────────────────────────────────────────────────────────────────────────────

def _expand_prompt_template(template: str, *, goal: str = "",
                              category: str = "", keywords: str = "",
                              reasoning: str = "",
                              toolkit_block: str = "",
                              toolkit_brief: str = "",
                              toolkit_count: int = 0,
                              ctx_extra: str = "",
                              enable_expand: bool = True) -> str:
    """Expand the user-supplied system prompt template."""
    if not template:
        return ""
    expand_help = ""
    if enable_expand:
        expand_help = ('  {"action":"expand_tools","keywords":"<search query>"}\n'
                        '       — ask runner to add more capabilities (LIMITED quota)')
    out = template
    SIMPLE_VARS = {
        "{goal}": goal,
        "{category}": category,
        "{keywords}": keywords,
        "{reasoning}": reasoning,
        "{toolkit}": toolkit_block,
        "{toolkit_brief}": toolkit_brief,
        "{toolkit_count}": str(toolkit_count),
        "{ctx_extra}": ctx_extra,
        "{expand_help}": expand_help,
    }
    for k, v in SIMPLE_VARS.items():
        if k in out:
            out = out.replace(k, v or "")

    # Cap-specific placeholders: {cap:<name>:<field>}
    import re as _re
    def _cap_repl(m):
        cap_name = m.group(1).strip()
        field = m.group(2).strip().lower()
        cap = CAPABILITY_REGISTRY.get(cap_name)
        if not cap:
            return f"[unknown cap: {cap_name}]"
        if field in ("desc", "description"):
            return (cap.get("description") or "")[:400]
        if field in ("sig", "signature"):
            try:
                return rich_cap_signature(cap_name)
            except Exception:
                return cap_name
        if field == "name":
            return cap_name
        return f"[unknown field {field} for cap {cap_name}]"
    out = _re.sub(r"\{cap:([^:}]+):([^}]+)\}", _cap_repl, out)
    return out


PROMPT_TEMPLATE_VARIABLES_HELP = """
Available template variables (insert anywhere in your custom system prompt):
  {goal}             — the user goal text
  {category}         — triage category (research, web_check, ...)
  {keywords}         — triage keywords (comma-separated)
  {reasoning}        — triage reasoning (one sentence)
  {toolkit}          — full multi-line toolkit block (cap signatures + descs)
  {toolkit_brief}    — comma-separated cap names only
  {toolkit_count}    — number of caps in the toolkit
  {ctx_extra}        — attached skills/ontologies block (may be empty)
  {expand_help}      — the expand_tools help line (empty when expand disabled)
  {cap:<name>:desc}  — description of a specific cap
  {cap:<name>:sig}   — full signature of a specific cap
""".strip()


def _openclaw_system_prompt(goal: str, toolkit_block: str, *,
                              extra: str = "", enable_expand: bool = True,
                              toolkit_names: list = None) -> str:
    # Build fabric hints if fabric tools are in the toolkit
    fabric_hint = ""
    if toolkit_names:
        fabric_tools = {"fabric.query", "fabric.datasets", "fabric.ingest",
                        "fabric.skills.list", "fabric.skills.get", "fabric.stats"}
        if any(t in fabric_tools for t in toolkit_names):
            fabric_hint = (
                "\nDATA FABRIC TIPS:\n"
                "• Call fabric.datasets first to see available datasets and record counts.\n"
                "• Search with: fabric.query(text=\"your search\") for keyword search,\n"
                "  or fabric.query(vector=\"your search\") for semantic search.\n"
                "• Add dataset_id=\"name\" to restrict to a specific dataset.\n"
                "• Set include_data=True to get full record content, not just summaries.\n"
                "• You can also pass query=\"plain text\" — it auto-converts to text+vector search.\n\n"
            )
    return (
        "You are a Vera autonomous agent operating in OPENCLAW mode.\n\n"
        f"GOAL: {goal}\n\n"
        "═════════════════════════════════════════════════════════════\n"
        "YOUR TOOLKIT — these tools were CURATED for this specific goal\n"
        "by a triage step. Start here. Read the schemas. Call them.\n"
        "═════════════════════════════════════════════════════════════\n"
        f"{toolkit_block}\n\n"
        + fabric_hint
        + "ON EACH TURN, RESPOND WITH EXACTLY ONE JSON OBJECT. No prose, no fences:\n"
        '  {"thought":"<reasoning>","tool_use":{"name":"<cap.name>","input":{...}}}\n'
        '  {"thought":"<reasoning>","final":"<answer addressing the GOAL above>"}\n\n'
        "RULES:\n"
        "1. PICK A TOOL FROM THE TOOLKIT ABOVE on the FIRST turn. The toolkit\n"
        "   was already filtered for this goal — do not start by searching for\n"
        "   more tools. Searching first wastes a cycle and burns the quota.\n"
        "2. The GOAL is the user request. Tool result messages tagged\n"
        "   [tool_result <name>] are observations from YOUR previous calls —\n"
        "   they are NOT new user requests.\n"
        "3. Inspect the schema for each tool. Required parameters are marked\n"
        "   [REQUIRED]. Parameters with 'valid options' must use those literals.\n"
        "4. NEVER repeat the same (tool, args) pair — the result is identical.\n"
        "5. If a tool call FAILS with a bad-args error, the runner will retry\n"
        "   with corrected args automatically. Don't give up after one failure.\n"
        "6. caps.search / context.search_caps / expand_tools are LAST RESORT.\n"
        "   Only use them when the curated toolkit clearly lacks what you need.\n"
        "   Hard quota: at most "
        + ("1 expand + 2 searches" if enable_expand else "2 searches") +
        " per run.\n"
        "7. End with {\"final\":...} as soon as the goal is satisfied OR as soon\n"
        "   as you have established the toolkit cannot satisfy it.\n"
        + (("\n" + extra) if extra else "")
    )


def _strip_think(raw: str) -> tuple:
    """Strip <think>...</think> blocks. Returns (clean_text, think_text)."""
    if not raw or "<think>" not in raw:
        return raw, ""
    import re as _re
    think_parts = []
    for m in _re.finditer(r"<think>(.*?)</think>", raw, _re.DOTALL):
        think_parts.append(m.group(1).strip())
    clean = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL)
    clean = _re.sub(r"<think>.*$", "", clean, flags=_re.DOTALL)
    return clean.strip(), "\n\n".join(think_parts)


_THINKING_MODEL_HINTS = ("qwen3", "qwen-3", "qwq", "deepseek-r1", "r1-distill",
                          "marco-o1", "skyt1", "phi4-reasoning")

def _is_thinking_model(model: str) -> bool:
    if not model:
        return False
    m = model.lower()
    return any(h in m for h in _THINKING_MODEL_HINTS)


async def _safe_ollama_generate_dw(prompt, *, system="", json_mode=True,
                                     model="", instance_id="", prefer_gpu=True):
    """Thinking-model-aware ollama_generate wrapper for dag_workshop callers.

    Lazily resolves ollama_generate via the context module so we don't have
    a circular import. Disables json_mode for thinking models (they often
    return empty under format=json), and retries without json_mode if the
    response is empty.
    """
    import importlib
    try:
        ctx = importlib.import_module("Vera.Orchestration.context")
        og = getattr(ctx, "ollama_generate", None)
    except Exception:
        og = None
    if og is None:
        # Fallback: try the orchestration module directly
        try:
            orch = importlib.import_module("Vera.Orchestration.capability_orchestration")
            og = getattr(orch, "ollama_generate", None)
        except Exception:
            og = None
    if og is None:
        return ""

    use_json = bool(json_mode) and not _is_thinking_model(model)
    raw = await og(
        prompt, system=system, json_mode=use_json,
        model=model or None,
        instance_id=instance_id or None,
        prefer_gpu=bool(prefer_gpu),
    )
    cleaned = (raw or "").strip()
    if not cleaned or len(cleaned) < 4:
        try:
            raw = await og(
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


def _extract_json(raw: str) -> Optional[Dict]:
    # Strip thinking tokens first — qwen3/deepseek-r1 etc. wrap JSON in <think>
    s, _think = _strip_think(raw or "")
    s = s.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    a = s.find("{")
    b = s.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(s[a:b+1])
    except Exception:
        cleaned = s[a:b+1].replace(",}", "}").replace(",]", "]")
        try:
            return json.loads(cleaned)
        except Exception:
            return None


def _result_preview(result: Any, max_len: int = 1500) -> str:
    if result is None:
        return "null"
    if isinstance(result, str):
        return result if len(result) <= max_len else result[:max_len] + "\n[truncated]"
    try:
        s = json.dumps(result, default=str, ensure_ascii=False)
    except Exception:
        s = str(result)
    return s if len(s) <= max_len else s[:max_len] + "\n[truncated]"


async def _await_hitl_decision(session_id: str, step: int, *,
                                 timeout: float = 300.0) -> Dict[str, Any]:
    """
    Block until /workshop/agent_loop/hitl/respond resolves this step,
    or `timeout` seconds elapse.
    """
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    _HITL_PENDING_LOOP.setdefault(session_id, {})[step] = fut
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        return {"decision": "timeout"}
    finally:
        try:
            _HITL_PENDING_LOOP.get(session_id, {}).pop(step, None)
        except Exception:
            pass


@APP.post("/workshop/agent_loop/hitl/respond")
async def workshop_hitl_respond(request: Request):
    """Resolve a paused HITL decision in an active openclaw run.

    Body:
      {session_id: str!, step: int!, decision: "approve"|"reject"|"edit"|"abort",
       args?: {...}, comment?: str}
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid  = body.get("session_id", "")
    step = int(body.get("step", -1))
    decision = body.get("decision", "")
    if not sid or step < 0 or decision not in ("approve", "reject", "edit", "abort"):
        return {"error": "session_id, step, decision required"}
    pending = _HITL_PENDING_LOOP.get(sid, {})
    fut = pending.get(step)
    if not fut or fut.done():
        return {"error": f"No pending step {step} for session {sid}"}
    fut.set_result({
        "decision": decision,
        "args":     body.get("args") or {},
        "comment":  body.get("comment", ""),
    })
    return {"resolved": True, "session_id": sid, "step": step, "decision": decision}


# ═════════════════════════════════════════════════════════════════════════════
# IMPROVED TRIAGE + TOOLKIT BUILDING + ARG COERCION
# ─────────────────────────────────────────────────────────────────────────────
# This block fixes several systemic problems observed in production runs:
#
#   1. Triage non-determinism: same goal → different categories on different
#      runs because the LLM was unanchored to a real vocabulary. Fix: anchored
#      enum, deterministic-temperature ollama call, hash-keyed cache.
#
#   2. Tool-set incompleteness: keyword search for "network scan" returned
#      ONE netscan cap, missing siblings. Fix: prefix expansion — when a
#      group like "netscan.X" is hit, sweep the full netscan.* prefix.
#
#   3. Useless caps in context: the global base toolkit (http.get, system.ping
#      etc.) bled into research/file/etc. tasks. Fix: category→base-toolkit
#      map; `system.*` only seeded for system_info / monitoring tasks.
#
#   4. "(none)" steps from invalid LLM JSON: parser silently logged garbage.
#      Fix: `_canonicalise_tool_use_payload` rescues common malformations
#      (wrapper objects, name-as-key, etc.) before declaring failure.
#
#   5. Arg errors triggering LLM retry storm: every "wrong type" or "unknown
#      arg" went through another LLM cycle. Fix: deterministic
#      `_coerce_args` runs FIRST — coerces booleans/ints/JSON-strings,
#      drops unknown args, supplies defaults, and only escalates to the LLM
#      if those didn't fix it.
#
#   6. Endless expand_tools loops: agents that already had the right caps
#      kept asking for "more research tools". Fix: `_have_useful_caps`
#      returns True if the visible toolkit already contains the obvious
#      heavy-hitters for the task category — and openclaw's expand path
#      refuses to re-expand when this holds.
# ═════════════════════════════════════════════════════════════════════════════

# Frozen vocabulary — the LLM is anchored to these and only these.
TRIAGE_CATEGORIES = [
    "research",          # info gathering, reports, deep investigation
    "web_check",         # is X up, is X reachable, fetch a URL
    "data_lookup",       # query a structured store
    "file_edit",         # read/modify files
    "summarisation",     # condense given content
    "analysis",          # interpret/process given content
    "search",            # general search across stores
    "monitoring",        # watch a system, alerting
    "code_task",         # generate / edit / inspect code
    "system_info",       # ping, health, sysinfo, network probe
    "network_scan",      # port scan, recon, fingerprint
    "ml_task",           # training, inference, prediction
    "data_pipeline",     # ingest / ETL / fabric
    "memory_recall",     # retrieve from memory graph / past notes
    "creative",          # write a story / poem / brainstorm
    "general_qa",        # answer a question from world knowledge
    "messaging",         # send/receive Telegram, email, chat
    "image_gen",         # generate images
    "audio",             # TTS, STT, speech, voice
    "browser_task",      # browse / navigate / interact with web pages
    "agent_op",          # check agent status / list / configure
    "report_generation", # compile / write / format a report or document
    "other",
]

# Per-category "preferred prefix" lists: when triage returns category X, we
# auto-include all caps under these prefixes (filtered by registry membership).
# Keeping these short and high-signal is the whole point.
CATEGORY_PREFIX_HINTS: Dict[str, List[str]] = {
    # Research: keep a TIGHT prefix list — collector. has 30+ caps that flood
    # the toolkit. Only headline research caps + key collectors get included.
    "research":       ["research.run", "research.report", "research.quick_search",
                       "research.deep", "research.parallel", "research.academic",
                       "research.security", "research.code", "research.guide",
                       "web.search", "web.fetch", "http.get",
                       "memory.recall", "scrape.fetch"],
    "web_check":      ["http.get", "http.head", "system.ping"],
    "data_lookup":    ["fabric.query", "fabric.datasets", "fabric.search",
                       "fabric.stats", "data.", "memory.recall"],
    "file_edit":      ["text.", "ide.code.", "fs.", "data.json_"],
    "summarisation":  ["llm.summarize", "llm.generate", "text."],
    "analysis":       ["llm.analyze", "llm.summarize", "llm.classify",
                       "data.json_", "research.analysis", "research.nlp", "nlp."],
    "search":         ["caps.search", "context.search_caps", "research.recall",
                       "research.activity", "memory.recall", "fabric.search"],
    "monitoring":     ["obs.", "health.", "system.ping", "research.health"],
    "code_task":      ["ide.", "exec.bash", "exec.python", "exec.run", "research.code"],
    "system_info":    ["system.", "obs.", "health.", "exec.bash"],
    "network_scan":   ["netscan.", "system.ping", "http.head"],
    "ml_task":        ["ml.", "vllm."],
    "data_pipeline":  ["fabric.", "data.", "collector.ingest", "pipeline."],
    "memory_recall":  ["memory.", "research.recall", "research.history",
                       "research.bookmarks", "research.session"],
    "creative":       ["llm.generate", "llm.brainstorm", "llm.rewrite"],
    "general_qa":     ["llm.generate", "research.quick_search", "web.search"],
    "messaging":      ["tg.", "mcp.", "llm.generate"],
    "image_gen":      ["sd.", "image.", "llm.generate"],
    "audio":          ["tts.", "stt.", "llm.generate"],
    "browser_task":   ["browser.", "http.get", "scrape."],
    "agent_op":       ["agent.", "research.agents.", "workshop.",
                       "ide.agent.", "context.search_dags"],
    "other":          [],
}

# Per-category essential base tools (replaces global _BASE_ESSENTIAL_CAPS).
# Discovery caps are still added universally (caps.search etc.) but these
# only seed when the category benefits from them.
CATEGORY_BASE_ESSENTIALS: Dict[str, List[str]] = {
    "research":       ["llm.generate", "llm.summarize", "research.run", "research.report",
                       "research.quick_search", "web.search", "http.get"],
    "web_check":      ["http.get", "system.ping"],
    "data_lookup":    ["fabric.query", "fabric.datasets", "llm.generate"],
    "file_edit":      ["llm.generate", "text.find_replace"],
    "summarisation":  ["llm.summarize", "llm.generate"],
    "analysis":       ["llm.analyze", "llm.summarize"],
    "search":         ["llm.generate"],
    "monitoring":     ["system.ping", "http.get"],
    "code_task":      ["llm.generate", "exec.bash.run"],
    "system_info":    ["system.ping", "exec.bash.run"],
    "network_scan":   ["system.ping"],
    "ml_task":        ["llm.generate"],
    "data_pipeline":  ["llm.generate"],
    "memory_recall":  ["memory.recall", "llm.generate"],
    "creative":       ["llm.generate"],
    "general_qa":     ["llm.generate"],
    "messaging":      ["llm.generate"],
    "image_gen":      ["llm.generate"],
    "audio":          ["llm.generate"],
    "browser_task":   ["http.get"],
    "agent_op":       ["llm.generate"],
    "report_generation": ["llm.generate", "llm.summarize"],
    "other":          ["llm.generate"],
}

# Discovery caps — always seeded because the agent must be able to look more up.
WORKSHOP_DISCOVERY_CAPS = [
    "caps.search", "caps.describe",
    "context.search_caps", "context.search_dags",
]

# "Useful cap" detection by category. If the toolkit ALREADY contains any of
# these, refuse further expansion for this category — the agent has what it
# needs and is just looping.
CATEGORY_USEFUL_CAP_PATTERNS: Dict[str, List[str]] = {
    "research":      ["research.run", "research.report", "research.deep",
                      "research.parallel", "research.quick_search", "web.search"],
    "web_check":     ["http.get"],
    "network_scan":  ["netscan.target.ports", "netscan.target.tech",
                      "netscan.discover"],
    "ml_task":       ["ml.train", "ml.predict", "ml.run"],
    "summarisation": ["llm.summarize", "llm.generate"],
    "code_task":     ["ide.code.tool_manifest", "exec.bash.run", "llm.generate"],
    "system_info":   ["exec.bash.run", "system.ping"],
    "messaging":     ["tg.send", "tg.broadcast"],
    "image_gen":     ["sd.txt2img", "image.generate"],
    "audio":         ["tts.speak", "stt.transcribe"],
    "browser_task":  ["browser.navigate", "browser.fetch"],
    "agent_op":      ["research.agents.status", "agent.list"],
}


def _expand_prefixes(prefixes: List[str], skip: set) -> List[str]:
    """Return all registered caps that start with any of the given prefixes."""
    out: List[str] = []
    for name in CAPABILITY_REGISTRY:
        if name in skip:
            continue
        for pref in prefixes:
            # Exact match (e.g. "system.ping") OR prefix match ("netscan.")
            if name == pref or (pref.endswith(".") and name.startswith(pref)):
                out.append(name)
                break
    out.sort()
    return out


# Triage cache (process-local, hash-keyed by goal text).
_TRIAGE_CACHE: Dict[str, Dict[str, Any]] = {}
_TRIAGE_CACHE_MAX = 256


def _triage_cache_key(goal: str) -> str:
    h = hashlib.sha256(goal.strip().lower().encode("utf-8")).hexdigest()[:16]
    return h


async def _workshop_triage_goal(goal: str, *, model: str = "",
                                  instance_id: str = "",
                                  prefer_gpu: bool = True) -> Dict[str, Any]:
    """Improved triage with anchored vocabulary, deterministic temperature,
    and a process-local cache so the same goal always produces the same
    classification within a process lifetime."""
    if not goal:
        return {"category": "other", "keywords": [], "reasoning": ""}

    cache_key = _triage_cache_key(goal)
    if cache_key in _TRIAGE_CACHE:
        return dict(_TRIAGE_CACHE[cache_key])

    # ── Heuristic pre-classification ─────────────────────────────────────
    # Some goals are unambiguous and shouldn't depend on LLM whim. These
    # patterns short-circuit to a confident category. The LLM still runs
    # to extract good keywords, but its category is overridden if the
    # heuristic was confident.
    heuristic = _heuristic_classify(goal)
    cats_csv = ", ".join(TRIAGE_CATEGORIES)
    sys = (
        "You are a goal-triage classifier. Read the user's goal and respond "
        "ONLY with a JSON object — no prose, no fences:\n"
        '{"category":"<primary>","categories":["<primary>","<secondary>",...],'
        '"keywords":["kw1","kw2","kw3","kw4"],'
        '"reasoning":"<one short sentence>"}\n\n'
        f"Category values MUST be from: {cats_csv}\n\n"
        "RULES:\n"
        "  • `category` is the PRIMARY category (most important action).\n"
        "  • `categories` is a list of ALL relevant categories for compound goals.\n"
        "    For simple goals, this is just [\"<primary>\"].\n"
        "    For compound goals like 'search fabric for CVEs and write a report',\n"
        "    use [\"data_lookup\", \"report_generation\"] to seed tools for BOTH stages.\n"
        "  • If the goal contains the word 'research', 'investigate', "
        "'find out about', 'tell me about', or asks for a 'report' on a "
        "subject → category is ALWAYS 'research', not 'data_lookup' or 'search'.\n"
        "  • 'data_lookup' is only for querying a structured store the user "
        "already named (a database, a CSV, a known dataset). NOT for general "
        "investigation of a topic.\n"
        "  • 'search' is for searching INSIDE the system (caps, memory, dags), "
        "NOT for searching the web for a topic.\n"
        "  • Compound goals that involve data retrieval AND report writing should\n"
        "    include BOTH relevant categories, e.g. [\"data_lookup\", \"report_generation\"]\n"
        "    or [\"research\", \"summarisation\"].\n\n"
        "Examples (study these — do not deviate):\n"
        "  Goal: 'is example.com up' → "
        '{"category":"web_check","categories":["web_check"],"keywords":["http","ping","reachability","website"],'
        '"reasoning":"Reachability check on a public URL"}\n'
        "  Goal: 'scan ports on 192.168.1.0/24' → "
        '{"category":"network_scan","categories":["network_scan"],"keywords":["netscan","ports","subnet","target"],'
        '"reasoning":"Port scan on a network range"}\n'
        "  Goal: 'search the fabric for CVEs and compile a report' → "
        '{"category":"data_lookup","categories":["data_lookup","report_generation"],'
        '"keywords":["fabric","query","dataset","report","llm","generate"],'
        '"reasoning":"Query structured data then generate a written report"}\n'
        "  Goal: 'research best gen1 pokemon teams and write report' → "
        '{"category":"research","categories":["research","report_generation"],'
        '"keywords":["research","report","web","gaming","generate"],'
        '"reasoning":"Investigate a topic on the web and produce a written report"}\n'
        "  Goal: 'query the fabric for CVEs relating to microsoft' → "
        '{"category":"data_lookup","categories":["data_lookup"],'
        '"keywords":["fabric","query","dataset","search","data"],'
        '"reasoning":"Query the data fabric for specific records"}\n'
        "  Goal: 'summarise this PDF' → "
        '{"category":"summarisation","categories":["summarisation"],'
        '"keywords":["llm","summarize","document","text"],'
        '"reasoning":"Condense given content into a shorter form"}\n\n'
        "Keywords should be capability-vocabulary terms (research, http, scrape, "
        "netscan, ml, llm, fabric, memory, ide, exec, etc.) — not paraphrases of "
        "the goal text. Do NOT include proper nouns from the goal. 4-6 keywords "
        "is ideal."
    )
    ctx = _ctx()
    ollama_generate = getattr(ctx, "ollama_generate", None)
    if ollama_generate is None:
        return {"category": "other", "keywords": [], "reasoning": ""}

    try:
        # Some ollama_generate signatures don't accept temperature — fall back
        try:
            raw = await ollama_generate(
                f"Goal: {goal.strip()}",
                system=sys,
                model=model or None,
                instance_id=instance_id or None,
                prefer_gpu=bool(prefer_gpu),
                json_mode=True,
                temperature=0.0,
            )
        except TypeError:
            raw = await ollama_generate(
                f"Goal: {goal.strip()}",
                system=sys,
                model=model or None,
                instance_id=instance_id or None,
                prefer_gpu=bool(prefer_gpu),
                json_mode=True,
            )
    except Exception as e:
        log.debug("workshop triage failed: %s", e)
        return {"category": "other", "keywords": [], "reasoning": ""}

    parsed = _parse_json_object(raw or "")
    if not parsed:
        # Even if LLM failed, prefer heuristic over total failure
        if heuristic:
            out = {"category": heuristic[0], "keywords": heuristic[1],
                   "reasoning": "(heuristic) " + heuristic[2]}
            _TRIAGE_CACHE[cache_key] = dict(out)
            return out
        return {"category": "other", "keywords": [], "reasoning": ""}

    cat = str(parsed.get("category", "other")).strip().lower()
    if cat not in TRIAGE_CATEGORIES:
        cat = _coerce_to_known_category(cat, parsed.get("keywords") or [])
    kws = parsed.get("keywords") or []
    if isinstance(kws, str):
        kws = [k.strip() for k in kws.split(",") if k.strip()]
    # Strip proper-noun-looking keywords (capitalised words or anything in
    # the goal text). Triage keywords should be capability-vocabulary, not
    # subject names — those poison cap searches.
    goal_tokens = set(t.strip(".,!?:;\"'()[]").lower() for t in goal.split())
    cleaned_kws = []
    for k in kws:
        ks = str(k).strip()
        if not ks:
            continue
        kl = ks.lower()
        # Drop if it's a literal goal-token longer than 3 chars (proper-noun heuristic)
        if kl in goal_tokens and len(kl) > 3 and ks[:1].isupper():
            continue
        cleaned_kws.append(kl)
    cleaned_kws = cleaned_kws[:6]

    # Override category if heuristic was confident and LLM disagreed
    if heuristic and heuristic[0] != cat and heuristic[3] >= 0.8:
        log.debug("triage: overriding LLM '%s' with heuristic '%s' (conf %.2f)",
                   cat, heuristic[0], heuristic[3])
        cat = heuristic[0]
        # Merge heuristic keywords too
        cleaned_kws = list(dict.fromkeys(heuristic[1] + cleaned_kws))[:6]

    out = {
        "category":   cat,
        "categories": _parse_categories(parsed, cat, heuristic, goal=goal),
        "keywords":   cleaned_kws,
        "reasoning":  str(parsed.get("reasoning", ""))[:400],
    }
    # Bound cache
    if len(_TRIAGE_CACHE) >= _TRIAGE_CACHE_MAX:
        _TRIAGE_CACHE.pop(next(iter(_TRIAGE_CACHE)))
    _TRIAGE_CACHE[cache_key] = dict(out)
    return out


def _parse_categories(parsed: dict, primary: str,
                       heuristic=None, goal: str = "") -> List[str]:
    """Extract and normalise the categories list from triage output."""
    raw = parsed.get("categories")
    cats: List[str] = []
    if isinstance(raw, list):
        for c in raw:
            cn = str(c).strip().lower()
            if cn and cn in TRIAGE_CATEGORIES and cn not in cats:
                cats.append(cn)
    # Ensure primary is always first
    if primary not in cats:
        cats.insert(0, primary)
    elif cats[0] != primary:
        cats.remove(primary)
        cats.insert(0, primary)
    # If heuristic was confident, merge its category too
    if heuristic and heuristic[3] >= 0.6:
        hcat = heuristic[0]
        if hcat not in cats:
            cats.append(hcat)
    # Compound goal detection — if the goal mentions report/compile/write AND
    # the primary category is a data/research category, auto-add report_generation.
    if goal:
        gl = goal.lower()
        import re as _re
        has_report = bool(_re.search(
            r'\b(?:compile|write|create|generate|produce|format)\s+(?:a\s+)?'
            r'(?:report|summary|document|overview|brief)\b'
            r'|\band\s+(?:compile|write|create|produce)\b'
            r'|\breport\b', gl))
        if has_report and "report_generation" not in cats:
            cats.append("report_generation")
        has_summarise = bool(_re.search(r'\bsummari[sz]e\b|\bcondense\b', gl))
        if has_summarise and "summarisation" not in cats:
            cats.append("summarisation")
    return cats


def _heuristic_classify(goal: str) -> Optional[Tuple[str, List[str], str, float]]:
    """Pattern-based pre-classifier. Returns (category, keywords, reasoning,
    confidence 0..1) or None if no high-confidence match.

    Confidence ≥ 0.8 will OVERRIDE an LLM disagreement.
    """
    g = goal.lower().strip()
    if not g:
        return None

    import re as _re

    # ── Messaging / Telegram / email — high priority because keywords are unambiguous ──
    if _re.search(r'\btelegram\b|\btg\.|\bsend\s+(?:a\s+)?(?:message|telegram|notification|alert)\b'
                  r'|\bnotify\b|\bbroadcast\b|\b(?:email|gmail|mail)\s+(?:to|me|the)\b'
                  r'|\bdm\b.{0,10}\b(user|chat|channel)\b', g):
        return ("messaging",
                ["telegram", "tg", "send", "message", "notify"],
                "Heuristic: messaging/telegram intent",
                0.92)

    # ── Image generation ──
    if _re.search(r'\b(?:generate|create|make|draw|render)\s+(?:an?\s+)?(?:image|picture|photo|illustration|art|painting)\b'
                  r'|\btxt2img\b|\bstable\s+diffusion\b|\bdall[-\s]?e\b|\bsd\.|\bimage\.gen', g):
        return ("image_gen",
                ["image", "generate", "diffusion", "sd"],
                "Heuristic: image generation intent",
                0.9)

    # ── Audio / TTS / STT ──
    if _re.search(r'\btext[-\s]to[-\s]speech\b|\bspeech[-\s]to[-\s]text\b|\btts\b|\bstt\b'
                  r'|\btranscri(?:be|ption)\b|\bspeak\s+(?:this|out|aloud)\b'
                  r'|\bvoice\s+(?:over|note|message)\b', g):
        return ("audio",
                ["tts", "stt", "speech", "transcribe"],
                "Heuristic: audio intent",
                0.88)

    # ── Browser task ──
    if _re.search(r'\bbrowse\b|\bopen\s+(?:the\s+)?(?:url|website|page|browser)\b'
                  r'|\bnavigate\s+to\b|\binteract\s+with\s+(?:page|website)\b'
                  r'|\bclick\s+(?:on\s+)?(?:the\s+)?(?:button|link|element)\b', g):
        return ("browser_task",
                ["browser", "navigate", "fetch", "scrape"],
                "Heuristic: browser interaction intent",
                0.85)

    # ── Agent operations ──
    if _re.search(r'\b(?:list|show|get)\s+(?:all\s+)?(?:my\s+)?agents?\b'
                  r'|\bagent\s+status\b|\bagent\.\b|\bresearch\.agents\.\b'
                  r'|\bresearcher\s+(?:slot|status|tier)\b', g):
        return ("agent_op",
                ["agent", "status", "list"],
                "Heuristic: agent operations intent",
                0.85)

    # Strong research signals — handles "research X", "investigate X", etc.
    research_patterns = [
        r'^\s*research\s+\S',          # "research <topic>" — direct command
        r'\bresearch(?:ing)?\s+(?:on|about|into|for)\b',
        r'\binvestigate\b', r'\binvestigation\b',
        r'\bproduce a report\b', r'\bwrite a report\b', r'\breport on\b',
        r'\btell me about\b', r'\bfind out about\b', r'\bfind information\b',
        r'\blearn about\b', r'\bbackground on\b', r'\bdetailed (?:report|analysis|writeup)\b',
        r'\bdeep dive\b', r'\bcomprehensive (?:report|analysis|overview)\b',
        r'\bweb[-\s]?search\b', r'\bgoogle\s+\S+',
    ]
    for pat in research_patterns:
        if _re.search(pat, g):
            return ("research",
                    ["research", "report", "web", "investigate"],
                    f"Heuristic: matched pattern '{pat}'",
                    0.9)

    # Network scan
    netscan_patterns = [
        r'\bport[s]? scan\b', r'\bscan port[s]?\b', r'\bnmap\b',
        r'\bnetwork scan\b', r'\brecon\b', r'\bopen ports?\b',
        r'\bsubnet\b', r'\b\d+\.\d+\.\d+\.\d+/\d+\b',
    ]
    # (re imported above as _re)
    for pat in netscan_patterns:
        if _re.search(pat, g):
            return ("network_scan",
                    ["netscan", "ports", "scan", "discover"],
                    f"Heuristic: matched pattern '{pat}'",
                    0.9)

    # Web check
    web_check_patterns = [
        r'\bis\s+\S+\.\S+\s+(up|online|down|reachable|alive)\b',
        r'\bping\s+\S+\b', r'\bcheck\s+if\s+\S+\.\S+\b',
        r'\bhttp[s]?://\S+\b.{0,30}\b(up|down|online|status)\b',
    ]
    for pat in web_check_patterns:
        if _re.search(pat, g):
            return ("web_check",
                    ["http", "ping", "reachability"],
                    f"Heuristic: matched pattern '{pat}'",
                    0.85)

    # Summarisation
    if _re.search(r'\bsumma(?:rise|rize)\b|\btl;?dr\b|\bcondense\b', g):
        return ("summarisation",
                ["llm", "summarize", "text"],
                "Heuristic: summarisation verb",
                0.85)

    # Code task
    if _re.search(r'\bwrite\s+(?:a\s+)?(?:python|code|script|function|program)\b'
                  r'|\brefactor\b|\bdebug\b|\bfix\s+(?:the\s+)?bug\b', g):
        return ("code_task",
                ["code", "ide", "exec"],
                "Heuristic: code verb",
                0.85)

    # Data lookup / fabric query
    if _re.search(r'\bfabric\b|\bquery\s+(?:the\s+)?(?:fabric|data|dataset|store)\b'
                  r'|\bsearch\s+(?:the\s+)?(?:fabric|data|datasets?)\b'
                  r'|\bfabric\.query\b|\bfabric\.datasets\b'
                  r'|\blook\s*up\s+(?:in|from)\s+(?:the\s+)?(?:data|fabric)\b', g):
        return ("data_lookup",
                ["fabric", "query", "dataset", "search", "data"],
                "Heuristic: data fabric / structured data query",
                0.9)

    # ML task
    if _re.search(r'\btrain\s+(?:a\s+)?model\b|\bml\.train\b|\brun\s+inference\b', g):
        return ("ml_task",
                ["ml", "train", "predict"],
                "Heuristic: ML verb",
                0.85)

    # System info — bash user, whoami, hostname, env
    if _re.search(r'\b(current\s+)?(?:bash\s+)?user\b|\bwhoami\b|\bhostname\b'
                  r'|\bgetenv\b|\benv\s+var\b|\bos\.environ\b'
                  r'|\bwhich\s+user\b|\bwhat\s+user\b', g):
        return ("system_info",
                ["exec", "bash", "system", "user"],
                "Heuristic: system user/env query",
                0.88)

    # Exec / bash task
    if _re.search(r'\brun\s+(?:a\s+)?(?:bash|shell|command|script)\b'
                  r'|\bexec(?:ute)?\s+(?:bash|shell|command)\b'
                  r'|\bexec\.bash\b', g):
        return ("system_info",
                ["exec", "bash", "shell", "system"],
                "Heuristic: exec/bash verb",
                0.85)

    return None


def _parse_json_object(raw: str) -> Optional[Dict[str, Any]]:
    """Best-effort JSON object extraction from LLM output."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        try:
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
        except Exception:
            pass
    try:
        s = raw.find("{"); e = raw.rfind("}")
        if s >= 0 and e > s:
            return json.loads(raw[s:e+1])
    except Exception:
        return None
    return None


# Soft-mapping for off-vocabulary categories the LLM might emit.
_CATEGORY_SOFT_MAP = {
    "search_engine": "search", "web": "research", "websearch": "research",
    "ping": "web_check", "url_check": "web_check", "uptime": "web_check",
    "scan": "network_scan", "port_scan": "network_scan", "recon": "network_scan",
    "investigate": "research", "investigation": "research", "report": "research",
    "summary": "summarisation", "summarize": "summarisation",
    "summarize_document": "summarisation", "summary_task": "summarisation",
    "code": "code_task", "coding": "code_task", "programming": "code_task",
    "training": "ml_task", "inference": "ml_task", "ml": "ml_task",
    "ingest": "data_pipeline", "etl": "data_pipeline",
    "memory": "memory_recall", "recall": "memory_recall",
    "story": "creative", "fiction": "creative", "writing": "creative",
    "qa": "general_qa", "question": "general_qa", "ask": "general_qa",
    # Messaging family
    "telegram": "messaging", "tg": "messaging", "send_message": "messaging",
    "notification": "messaging", "notify": "messaging", "broadcast": "messaging",
    "messaging": "messaging", "chat": "messaging", "email": "messaging", "mail": "messaging",
    # Image generation family
    "image": "image_gen", "image_generation": "image_gen", "img_gen": "image_gen",
    "txt2img": "image_gen", "diffusion": "image_gen", "stable_diffusion": "image_gen",
    # Audio family
    "tts": "audio", "stt": "audio", "speech": "audio", "voice": "audio",
    "transcribe": "audio", "audio": "audio",
    # Browser
    "browser": "browser_task", "navigate": "browser_task", "scrape": "browser_task",
    # Agent ops
    "agent": "agent_op", "agents": "agent_op",
}


def _coerce_to_known_category(cat: str, keywords: List[Any]) -> str:
    """If the LLM hallucinates a category outside our enum, map it back."""
    norm = (cat or "").lower().strip().replace("-", "_").replace(" ", "_")
    if norm in TRIAGE_CATEGORIES:
        return norm
    if norm in _CATEGORY_SOFT_MAP:
        return _CATEGORY_SOFT_MAP[norm]
    # Try to match keyword against category names
    kw_join = " ".join(str(k) for k in keywords).lower()
    for c in TRIAGE_CATEGORIES:
        if c in kw_join:
            return c
    # Last-ditch: substring match on the cat itself
    for c in TRIAGE_CATEGORIES:
        if c in norm or norm in c:
            return c
    return "other"


def _workshop_build_toolkit(*, allowed_caps: str, category: str,
                              categories: Optional[List[str]] = None,
                              keywords: List[str], top_k: int = 16,
                              extra_caps: Optional[List[str]] = None,
                              skip_useless_essentials: bool = True) -> List[str]:
    """Build a category-aware toolkit.

    Triage discovers tools from the FULL capability registry — the
    allowed_caps parameter is NOT used as a filter here. Tool access
    control happens at execution time, not at triage time.

    `categories` (plural) supports compound goals — triage may return
    multiple categories like ["data_lookup", "report_generation"]. Each
    category's essentials and prefix hints are merged into the toolkit.
    Falls back to `category` (singular) for backward compatibility.

    Order:
      1. Universal discovery caps (caps.search etc.)
      2. Universal essentials (llm.generate, llm.summarize)
      3. Category-specific essentials for ALL categories
      4. Prefix-expanded caps for ALL categories
      5. Keyword-driven semantic search (top_k)
      6. Any caller-provided extras

    Truncates keyword-discovered caps to keep total ≤ top_k * 2.
    """
    blacklist: set = set()
    try:
        ctx = _ctx()
        bl = getattr(ctx, "_AGENT_LOOP_BLACKLIST", None)
        if isinstance(bl, set):
            blacklist = bl
    except Exception:
        pass

    toolkit: List[str] = []
    seen: set = set()

    def add(name: str):
        if name and name in CAPABILITY_REGISTRY and name not in seen \
                and name not in blacklist:
            toolkit.append(name)
            seen.add(name)

    # Resolve category list — use categories (plural) if provided, else wrap singular
    cats_list: List[str] = []
    if categories and isinstance(categories, list):
        cats_list = [c.lower().strip() for c in categories if c]
    if not cats_list:
        cats_list = [(category or "other").lower().strip()]
    # Normalise: only keep known categories
    cats_list = [c if c in TRIAGE_CATEGORIES else "other" for c in cats_list]
    # Deduplicate while preserving order
    seen_cats: set = set()
    deduped: List[str] = []
    for c in cats_list:
        if c not in seen_cats:
            deduped.append(c)
            seen_cats.add(c)
    cats_list = deduped

    # 1. Discovery caps — always present (bypass pool)
    for c in WORKSHOP_DISCOVERY_CAPS:
        add(c)

    # 1b. Universal essentials — always present (bypass pool)
    _UNIVERSAL_ESSENTIALS = ["llm.generate", "llm.summarize"]
    for c in _UNIVERSAL_ESSENTIALS:
        add(c)

    # 2. Category-specific essentials — for ALL resolved categories (bypass pool)
    for cat_norm in cats_list:
        for c in CATEGORY_BASE_ESSENTIALS.get(cat_norm, []):
            add(c)

    # 3. Prefix expansion for ALL categories (respects pool)
    for cat_norm in cats_list:
        cat_caps = _expand_prefixes(CATEGORY_PREFIX_HINTS.get(cat_norm, []), seen)
        for c in cat_caps[:max(8, top_k)]:
            add(c)

    # 5. Keyword-driven semantic search via the cap index when available
    semantic_added = 0
    semantic_budget = max(4, top_k // 2)
    try:
        import importlib
        ds = importlib.import_module("Vera.Orchestration.dag_store")
        cap_index = getattr(ds, "CAP_INDEX", None)
        if cap_index is not None and keywords:
            kw_query = " ".join(keywords)
            # _act_relevance_search may be sync or async — handle both
            search_fn = getattr(cap_index, "relevance_search", None)
            if search_fn:
                hits = search_fn(kw_query, top_k=top_k * 2)
                if asyncio.iscoroutine(hits):
                    hits = []
                for entry in hits or []:
                    name = entry[0] if isinstance(entry, tuple) else (
                        entry.get("name") if isinstance(entry, dict) else None
                    )
                    if not name or name in seen:
                        continue
                    add(name)
                    semantic_added += 1
                    if semantic_added >= semantic_budget:
                        break
    except Exception:
        pass

    # 6. Extras — user-provided or expand_tools-driven
    for c in (extra_caps or []):
        add(c)

    return toolkit


def _have_useful_caps(toolkit: List[str], category: str) -> bool:
    """Is the toolkit already adequate for this category?"""
    patterns = CATEGORY_USEFUL_CAP_PATTERNS.get(category, [])
    if not patterns:
        return False  # unknown category, can't judge
    visible = set(toolkit)
    for p in patterns:
        if p in visible:
            return True
    return False


# ─── Argument coercion: make wrong-type-but-recoverable inputs work ─────────
def _coerce_args(cap_name: str, args: Any) -> Tuple[Dict[str, Any], List[str]]:
    """Apply deterministic fixes to arg dicts before invoking the cap.

    Returns (coerced_args, notes).
    Notes are human-readable descriptions of what we changed — fed back to
    the LLM in the next message so it learns.
    """
    notes: List[str] = []
    cap = CAPABILITY_REGISTRY.get(cap_name)
    if not cap or not isinstance(args, dict):
        return (args if isinstance(args, dict) else {}), notes

    schema = cap.get("schema", {}) or {}
    props  = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    accepted = set(props.keys()) - {"trace_id"}

    out: Dict[str, Any] = {}

    # 1. Drop completely unknown args (the LLM commonly invents ones)
    dropped = []
    for k, v in args.items():
        if k == "trace_id":
            continue
        if k in accepted:
            out[k] = v
        else:
            dropped.append(k)
    if dropped:
        notes.append(
            f"dropped unknown args: {', '.join(dropped)} "
            f"(valid: {', '.join(sorted(accepted)[:8])}"
            f"{' …' if len(accepted) > 8 else ''})"
        )

    # 2. Coerce types where the LLM passed a string we can parse
    for pname, pspec in props.items():
        if pname not in out or pname == "trace_id":
            continue
        val   = out[pname]
        ptype = (pspec or {}).get("type")
        try:
            if ptype == "boolean" and isinstance(val, str):
                lv = val.lower().strip()
                if lv in ("true", "yes", "1", "on"):
                    out[pname] = True; notes.append(f"{pname}: '{val}' → True")
                elif lv in ("false", "no", "0", "off", ""):
                    out[pname] = False; notes.append(f"{pname}: '{val}' → False")
            elif ptype == "integer" and isinstance(val, str) and val.strip():
                try:
                    out[pname] = int(val); notes.append(f"{pname}: '{val}' → int")
                except Exception:
                    try:
                        out[pname] = int(float(val)); notes.append(f"{pname}: '{val}' → int (via float)")
                    except Exception:
                        pass
            elif ptype == "number" and isinstance(val, str) and val.strip():
                try:
                    out[pname] = float(val); notes.append(f"{pname}: '{val}' → float")
                except Exception:
                    pass
            elif ptype == "array" and isinstance(val, str):
                # Try JSON, then comma-split
                try:
                    parsed = json.loads(val)
                    if isinstance(parsed, list):
                        out[pname] = parsed
                        notes.append(f"{pname}: parsed as JSON array")
                    else:
                        out[pname] = [parsed]
                        notes.append(f"{pname}: wrapped scalar in list")
                except Exception:
                    pieces = [s.strip() for s in val.split(",") if s.strip()]
                    if pieces:
                        out[pname] = pieces
                        notes.append(f"{pname}: comma-split into {len(pieces)} items")
            elif ptype == "object" and isinstance(val, str):
                try:
                    parsed = json.loads(val)
                    if isinstance(parsed, dict):
                        out[pname] = parsed
                        notes.append(f"{pname}: parsed as JSON object")
                except Exception:
                    pass
            elif ptype == "string" and not isinstance(val, str):
                # Coerce numbers/bools to string if the cap wants string
                try:
                    out[pname] = json.dumps(val) if isinstance(val, (dict, list)) else str(val)
                    notes.append(f"{pname}: stringified")
                except Exception:
                    pass

            # 3. Enum validation: if value isn't in the enum, try case-fix
            enum = (pspec or {}).get("enum")
            if enum and out.get(pname) not in enum and isinstance(out.get(pname), str):
                lv = out[pname].lower()
                for e in enum:
                    if isinstance(e, str) and e.lower() == lv:
                        out[pname] = e
                        notes.append(f"{pname}: '{out[pname]}' → '{e}' (enum case fix)")
                        break
        except Exception:
            pass

    # 4. Supply defaults for missing required if they have one in schema
    for r in required:
        if r in out or r == "trace_id":
            continue
        d = (props.get(r) or {}).get("default")
        if d is not None:
            out[r] = d
            notes.append(f"{r}: filled from default = {d!r}")

    return out, notes


# ──────────────────────────────────────────────────────────────────────────────
# ERROR RECOVERY — tool-call retry with arg fixes (tool stays the same)
# ──────────────────────────────────────────────────────────────────────────────
# When a tool call fails with a validation/coercion/arg error, instead of
# bouncing back to the planning LLM (which often picks a totally different
# tool or just gives up), we run a tightly-scoped recovery sub-cycle:
#
#   "The tool X failed with error E. Here's its schema. Fix ONLY the input
#    args so it succeeds. Do not change the tool."
#
# The LLM can only return new args (the cap name is fixed). We retry up to
# `max_recovery_attempts` times before giving up. Each attempt is reflected
# in history but flagged as a recovery attempt.
# ──────────────────────────────────────────────────────────────────────────────

def _is_arg_error(error_text: str) -> bool:
    """Is this error likely to be fixable by changing the args?

    We treat schema/validation/type/coercion errors as recoverable. We
    do NOT treat connection/auth/network errors as recoverable.
    """
    if not error_text:
        return False
    e = str(error_text).lower()
    # Hard rejects — narrowly worded to avoid false positives (e.g. "timeout"
    # matching a parameter named timeout, "401" appearing inside a payload).
    NON_RECOVERABLE = (
        "connection refused", "connection reset", "connection aborted",
        "request timeout", "read timeout", "connect timeout", "timed out waiting",
        "service unavailable", "503 service", "502 bad gateway", "504 gateway",
        "ssl error", "ssl handshake", "certificate verify", "tls handshake",
        "name or service not known", "name resolution failed",
        "permission denied", "401 unauthorized", "403 forbidden",
        "not implemented", "501 not implemented",
        "rate limit", "429 too many",
        "researcher_api unavailable", "researcher unavailable",
        "no such file or directory",  # filesystem — rarely fixable by arg change
    )
    if any(p in e for p in NON_RECOVERABLE):
        return False

    # Positive signals — schema/type/validation errors
    RECOVERABLE = (
        # Schema / validation
        "validation", "schema", "required", "missing", "missing required",
        "must be", "expected", "invalid", "unknown arg", "unexpected keyword",
        "type mismatch", "must provide", "argument", "param", "field",
        "wrong type", "got ", "should be", "is not allowed",
        "json decode", "expects",
        # Python type errors from bad arg shapes
        "not supported between instances",  # comparing wrong types
        "unsupported operand",
        "object is not iterable", "object is not subscriptable",
        "object has no attribute",
        "takes no keyword arguments", "got an unexpected keyword",
        "takes ", "positional argument", "missing 1 required",
        "could not convert", "invalid literal",
        "string indices must be integers",
        "must be str, not", "must be int, not", "must be a",
        # Cap-side validation
        "no such", "not in toolkit", "unknown capability",
        "invalid cidr", "invalid url", "invalid path",
    )
    return any(p in e for p in RECOVERABLE)


def _build_recovery_prompt(*, cap_name: str, failed_args: Dict[str, Any],
                             error_text: str, attempt: int,
                             max_attempts: int,
                             prior_attempts: List[Dict[str, Any]] = None) -> str:
    """Build the user message for an error-recovery sub-cycle."""
    cap = CAPABILITY_REGISTRY.get(cap_name) or {}
    schema = cap.get("schema", {}) or {}
    props  = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []

    schema_lines = []
    for pname, pspec in props.items():
        if pname == "trace_id":
            continue
        ptype = (pspec or {}).get("type", "any")
        is_req = pname in required
        desc = (pspec or {}).get("description", "") or ""
        if len(desc) > 100:
            desc = desc[:100] + "..."
        enum = (pspec or {}).get("enum")
        line = f"  - {pname} ({ptype}{', REQUIRED' if is_req else ''})"
        if enum:
            line += f" -- must be one of: {enum}"
        if desc:
            line += f" -- {desc}"
        schema_lines.append(line)
    schema_block = "\n".join(schema_lines) or "  (no schema available)"

    history_block = ""
    if prior_attempts:
        history_block = "\n\nPRIOR FAILED ATTEMPTS:\n"
        for i, a in enumerate(prior_attempts, 1):
            try:
                args_s = json.dumps(a.get("args") or {}, default=str)[:200]
            except Exception:
                args_s = str(a.get("args"))[:200]
            history_block += f"  attempt {i}: args={args_s} -> error: {str(a.get('error',''))[:200]}\n"

    try:
        failed_args_s = json.dumps(failed_args, default=str)[:300]
    except Exception:
        failed_args_s = str(failed_args)[:300]

    return (
        f"TOOL CALL FAILED -- recovery attempt {attempt}/{max_attempts}\n\n"
        f"Tool: {cap_name}\n"
        f"Schema:\n{schema_block}\n\n"
        f"Failed args: {failed_args_s}\n"
        f"Error: {str(error_text)[:400]}"
        f"{history_block}\n\n"
        "Fix the input args so the call succeeds. The tool MUST stay as "
        f"`{cap_name}` -- do not change it. Respond with EXACTLY:\n"
        '  {"input": { ... new args ... }}\n'
        '  OR if recovery is impossible:\n'
        '  {"give_up": true, "reason": "<why>"}\n'
        "No prose. No fences. Just the JSON object."
    )


_RECOVERY_SYSTEM_PROMPT = (
    "You are an argument-fixing assistant. A previous tool call failed due "
    "to bad input arguments. Your only job is to fix the arguments so the "
    "tool call succeeds.\n\n"
    "RULES:\n"
    "1. You CANNOT change the tool -- it is fixed.\n"
    "2. You CAN change any input field, add missing required fields, or "
    "remove unknown fields.\n"
    "3. Read the schema carefully -- required fields are marked REQUIRED. "
    "Enum fields have a fixed list of valid values.\n"
    "4. If the schema is unclear or the error is not fixable by changing "
    "args (network, auth, etc.), respond with give_up.\n"
    "5. Respond with EXACTLY one JSON object: "
    '{"input": {...}} or {"give_up": true, "reason": "..."}. '
    "No prose, no fences."
)


async def _attempt_arg_recovery(*, cap_name: str, failed_args: Dict[str, Any],
                                  error_text: str,
                                  model: str = "", instance_id: str = "",
                                  prefer_gpu: bool = True,
                                  max_attempts: int = 2,
                                  call_tool: Any = None,
                                  session_id: str = "",
                                  trace_id: str = "",
                                  emit_fn: Any = None,
                                  cycle: int = 0,
                                  stream_id: str = "") -> Dict[str, Any]:
    """Run an error-recovery sub-cycle. Returns dict:

    {
        "recovered": bool,        # True if a retry succeeded
        "attempts": [{args, error}, ...],
        "final_invoke": {ok, result, error},  # invoke result of last attempt
        "gave_up": bool,
        "give_up_reason": str,
    }

    `call_tool` is the tool dispatcher (signature: async (cap, args, *, session_id, trace_id) -> {ok, result, error}).
    `emit_fn` is the event emitter (optional).
    """
    attempts: List[Dict[str, Any]] = []
    final_invoke: Dict[str, Any] = {"ok": False, "error": error_text or "unknown"}
    last_error = error_text
    gave_up = False
    give_up_reason = ""
    last_args = dict(failed_args) if isinstance(failed_args, dict) else {}

    if emit_fn:
        try:
            await emit_fn({
                "type":       "agent_loop.error_recovery_start",
                "tool":       cap_name,
                "error":      str(error_text)[:300],
                "max_attempts": max_attempts,
                "cycle":      cycle, "session_id": session_id,
                "stream_id":  stream_id,
            })
        except Exception:
            pass

    for attempt_i in range(1, max_attempts + 1):
        prompt = _build_recovery_prompt(
            cap_name=cap_name, failed_args=last_args,
            error_text=last_error, attempt=attempt_i,
            max_attempts=max_attempts, prior_attempts=attempts,
        )
        try:
            raw = await _safe_ollama_generate_dw(
                prompt, system=_RECOVERY_SYSTEM_PROMPT,
                model=model, instance_id=instance_id,
                prefer_gpu=prefer_gpu, json_mode=True,
            )
        except Exception as e:
            last_error = f"recovery LLM call failed: {e}"
            attempts.append({"args": last_args, "error": last_error})
            break

        clean_raw, _think = _strip_think(raw or "")
        parsed = _extract_json(clean_raw)
        if not isinstance(parsed, dict):
            last_error = "recovery LLM returned unparseable JSON"
            attempts.append({"args": last_args, "error": last_error})
            continue

        if parsed.get("give_up"):
            gave_up = True
            give_up_reason = str(parsed.get("reason") or "")[:200]
            attempts.append({"args": last_args, "error": "agent gave up: " + give_up_reason})
            break

        new_args = parsed.get("input") or parsed.get("args") or {}
        if not isinstance(new_args, dict):
            last_error = "recovery LLM returned non-dict input"
            attempts.append({"args": last_args, "error": last_error})
            continue

        # Coerce types via the standard pipeline
        coerced, _coerce_notes = _coerce_args(cap_name, new_args)

        if emit_fn:
            try:
                await emit_fn({
                    "type":       "agent_loop.error_recovery_attempt",
                    "tool":       cap_name,
                    "attempt":    attempt_i,
                    "args":       coerced,
                    "prev_error": str(last_error)[:200],
                    "cycle":      cycle, "session_id": session_id,
                    "stream_id":  stream_id,
                })
            except Exception:
                pass

        # Try the call
        if call_tool is None:
            attempts.append({"args": coerced, "error": "no call_tool dispatcher provided"})
            break
        try:
            invoke = await call_tool(cap_name, coerced,
                                       session_id=session_id, trace_id=trace_id)
        except Exception as e:
            invoke = {"ok": False, "error": f"dispatcher exception: {e}"}

        # Promote inner errors
        if invoke.get("ok") and isinstance(invoke.get("result"), dict):
            rerr = invoke["result"].get("error")
            if rerr:
                invoke["ok"] = False
                invoke["error"] = str(rerr)

        attempts.append({"args": coerced,
                          "ok": invoke.get("ok"),
                          "error": invoke.get("error", "")})
        last_args = coerced
        last_error = invoke.get("error", "")
        final_invoke = invoke

        if invoke.get("ok"):
            if emit_fn:
                try:
                    await emit_fn({
                        "type":       "agent_loop.error_recovery_done",
                        "tool":       cap_name,
                        "recovered":  True,
                        "attempts":   attempt_i,
                        "cycle":      cycle, "session_id": session_id,
                        "stream_id":  stream_id,
                    })
                except Exception:
                    pass
            return {"recovered": True, "attempts": attempts,
                    "final_invoke": invoke, "gave_up": False,
                    "give_up_reason": ""}

        # Bail early if the new error isn't recoverable
        if not _is_arg_error(last_error):
            break

    if emit_fn:
        try:
            await emit_fn({
                "type":       "agent_loop.error_recovery_done",
                "tool":       cap_name,
                "recovered":  False,
                "attempts":   len(attempts),
                "gave_up":    gave_up,
                "reason":     give_up_reason or last_error[:200],
                "cycle":      cycle, "session_id": session_id,
                "stream_id":  stream_id,
            })
        except Exception:
            pass
    return {"recovered": False, "attempts": attempts,
            "final_invoke": final_invoke, "gave_up": gave_up,
            "give_up_reason": give_up_reason}


def _canonicalise_tool_use_payload(parsed: Any) -> Optional[Dict[str, Any]]:
    """Rescue common LLM JSON malformations.

    The openclaw protocol expects:
        {thought, tool_use:{name, input}}
        {thought, final}
        {action:'expand_tools', keywords}

    But LLMs frequently emit:
        {tool: 'foo', args: {…}}
        {action:'use_tool', name:'foo', input:{…}}
        {tool_use: 'foo', input: {…}}     # tool_use as string
        {function: 'foo', arguments: …}
        {name: 'foo', input: {…}}
        nested wrappers, etc.

    This function returns a canonical {tool_use:{name, input}, thought?}
    dict if it can rescue the payload, otherwise None.
    """
    if not isinstance(parsed, dict):
        return None

    thought = parsed.get("thought") or parsed.get("reasoning") or ""

    # Already canonical
    if "tool_use" in parsed:
        tu = parsed["tool_use"]
        if isinstance(tu, dict) and "name" in tu:
            return {"thought": thought,
                    "tool_use": {"name": tu.get("name") or "",
                                 "input": tu.get("input") or tu.get("arguments")
                                          or tu.get("args") or {}}}
        if isinstance(tu, str):
            inp = parsed.get("input") or parsed.get("args") or parsed.get("arguments") or {}
            return {"thought": thought,
                    "tool_use": {"name": tu, "input": inp}}
    if "final" in parsed:
        return {"thought": thought, "final": str(parsed["final"])}
    if parsed.get("action") in ("expand_tools", "expand"):
        return {"thought": thought,
                "action": "expand_tools",
                "keywords": parsed.get("keywords") or parsed.get("query") or ""}
    if parsed.get("action") in ("done", "finish", "stop", "complete"):
        return {"thought": thought,
                "final": str(parsed.get("summary") or parsed.get("final")
                             or parsed.get("answer") or "Goal complete.")}

    # ── action: "tool_use" with name/input at top level ──
    if parsed.get("action") in ("tool_use", "use_tool", "call", "invoke"):
        name = (parsed.get("name") or parsed.get("tool")
                or parsed.get("function") or parsed.get("cap") or "")
        if isinstance(name, str) and name.strip():
            inp = (parsed.get("input") or parsed.get("args")
                   or parsed.get("arguments") or parsed.get("parameters") or {})
            return {"thought": thought, "tool_use": {"name": name, "input": inp}}

    # ── action: "<cap.name>" with remaining keys = input ──
    # Pattern: {"action": "collector.site_profile", "url": "..."}
    act_val = parsed.get("action")
    if isinstance(act_val, str) and "." in act_val and act_val in CAPABILITY_REGISTRY:
        # Strip metadata keys; everything else is the input
        skip = {"action", "thought", "reasoning", "summary", "explanation"}
        inp = {k: v for k, v in parsed.items() if k not in skip}
        return {"thought": thought, "tool_use": {"name": act_val, "input": inp}}

    # Common malformations
    name = (parsed.get("tool") or parsed.get("name")
            or parsed.get("function") or parsed.get("cap"))
    if isinstance(name, str) and name.strip():
        inp = (parsed.get("input") or parsed.get("args")
               or parsed.get("arguments") or parsed.get("parameters") or {})
        return {"thought": thought, "tool_use": {"name": name, "input": inp}}

    # Wrapper objects: {"call": {…}} or {"action": {…}} or {"step": {…}}
    for wrap_key in ("call", "action_obj", "step", "next"):
        wrapped = parsed.get(wrap_key)
        if isinstance(wrapped, dict):
            r = _canonicalise_tool_use_payload(wrapped)
            if r:
                if not r.get("thought"): r["thought"] = thought
                return r

    return None


# ═════════════════════════════════════════════════════════════════════════════
# AGENT-LOOP PRESETS  (Loop Builder → Agent Loop variant menu)
# ─────────────────────────────────────────────────────────────────────────────
# When the user composes a custom flow in Loop Builder and saves it as a
# preset, it lands in this in-process registry and is exposed under
# /workshop/agent_loop/presets so the agent-loop UI can list it as a
# selectable variant.
# ═════════════════════════════════════════════════════════════════════════════

_AGENT_LOOP_PRESETS: Dict[str, Dict[str, Any]] = {}
_PRESETS_LOADED = False

_PRESET_DATASET = "vera_agent_loop_presets"


async def _load_presets_from_fabric():
    """Load presets from the data fabric on first access."""
    global _PRESETS_LOADED
    if _PRESETS_LOADED:
        return
    _PRESETS_LOADED = True
    try:
        from Vera.Orchestration.fabric.data_fabric import _sqlite_query
        rows = await _sqlite_query(dataset_id=_PRESET_DATASET, limit=500)
        for row in rows:
            data = row.get("data") or row
            pid = data.get("id") or row.get("record_id", "")
            if pid:
                _AGENT_LOOP_PRESETS[pid] = {
                    "id":          pid,
                    "name":        data.get("name", pid),
                    "description": data.get("description", ""),
                    "config":      data.get("config", {}),
                    "saved_at":    data.get("saved_at", ""),
                }
    except Exception as e:
        log.debug("Failed to load presets from fabric: %s", e)


async def _save_preset_to_fabric(preset: dict):
    """Persist a single preset to the data fabric."""
    try:
        from Vera.Orchestration.fabric.data_fabric import ingest_dataset
        await ingest_dataset(
            _PRESET_DATASET,
            [{"id": preset["id"], **preset}],
            source="workshop_preset",
            tags=["preset", "agent_loop"],
        )
    except Exception as e:
        log.debug("Failed to save preset to fabric: %s", e)


async def _delete_preset_from_fabric(preset_id: str):
    """Remove a preset from the fabric dataset."""
    try:
        from Vera.Orchestration.fabric.data_fabric import delete_record
        await delete_record(_PRESET_DATASET, preset_id)
    except Exception as e:
        log.debug("Failed to delete preset from fabric: %s", e)


@capability(
    "workshop.agent_loop.preset_save", memory="off",
    http_method="POST", http_path="/workshop/agent_loop/preset_save",
    http_tags=["workshop", "agents"], silent=True,
    description=(
        "Save a Loop Builder configuration as an agent-loop preset that "
        "appears in the Agent Loop variant dropdown. "
        "Inputs: id (str!), name (str!), description (str), config (object!) "
        "— must include {variant: 'v1'|'v2'|'openclaw', max_cycles, allowed_caps, "
        "satisfaction_check, enable_expand, require_approval, hitl_timeout_secs}. "
        "Output: {ok, id}."
    ),
)
async def cap_workshop_preset_save(id: str = "", name: str = "",
                                     description: str = "",
                                     config: dict = None,
                                     trace_id=None):
    await _load_presets_from_fabric()
    if not id or not name:
        return {"error": "id and name are required"}
    if not isinstance(config, dict):
        return {"error": "config must be an object"}
    base = config.get("variant", "openclaw")
    if base not in {"v1", "v2", "openclaw"}:
        return {"error": "variant must be v1, v2, or openclaw"}
    preset = {
        "id":          id,
        "name":        name,
        "description": description,
        "config":      dict(config),
        "saved_at":    now_iso(),
    }
    _AGENT_LOOP_PRESETS[id] = preset
    await _save_preset_to_fabric(preset)
    return {"ok": True, "id": id, "count": len(_AGENT_LOOP_PRESETS)}


@capability(
    "workshop.agent_loop.preset_list", memory="off",
    http_method="POST", http_path="/workshop/agent_loop/preset_list",
    http_tags=["workshop", "agents"], silent=True,
    description=("List saved agent-loop presets. "
                 "Output: {presets: [{id, name, description, config}]}."),
)
async def cap_workshop_preset_list(trace_id=None):
    await _load_presets_from_fabric()
    return {"presets": list(_AGENT_LOOP_PRESETS.values())}


@capability(
    "workshop.agent_loop.preset_delete", memory="off",
    http_method="POST", http_path="/workshop/agent_loop/preset_delete",
    http_tags=["workshop", "agents"], silent=True,
    description="Delete a preset by id. Inputs: id (str!). Output: {ok}.",
)
async def cap_workshop_preset_delete(id: str = "", trace_id=None):
    await _load_presets_from_fabric()
    if not id:
        return {"error": "id required"}
    existed = _AGENT_LOOP_PRESETS.pop(id, None) is not None
    if existed:
        await _delete_preset_from_fabric(id)
    return {"ok": True, "existed": existed}


# ═════════════════════════════════════════════════════════════════════════════
# Multi-select discovery caps (for UI: skills / ontologies / agents / models)
# ─────────────────────────────────────────────────────────────────────────────
# The agent loop pane offered free-text inputs for skills/ontologies/etc.,
# which is error-prone. These caps return the listings the UI needs to render
# searchable multi-selects without forcing the panel to know each subsystem's
# private endpoint.
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "workshop.discover.options", memory="off",
    http_method="POST", http_path="/workshop/discover/options",
    http_tags=["workshop"], silent=True,
    description=(
        "Aggregate discovery for the agent loop UI: skills, ontologies, "
        "agents, models. Each list is best-effort — missing subsystems are "
        "returned as []. "
        "Output: {skills, ontologies, agents, models, current_model}."
    ),
)
async def cap_workshop_discover_options(trace_id=None):
    out = {
        "skills":        [],
        "ontologies":    [],
        "agents":        [],
        "models":        [],
        "current_model": "",
    }

    # Skills — try skills.list / skills.registry
    for cap_name in ("skills.list", "skills.registry", "skills.all"):
        cap = CAPABILITY_REGISTRY.get(cap_name)
        if cap:
            try:
                r = await cap["func"](trace_id=trace_id or "")
                items = r.get("skills") or r.get("items") or r.get("list") or []
                if isinstance(items, list):
                    out["skills"] = [
                        {"id":   x.get("id") or x.get("name") or str(x),
                         "name": x.get("name") or x.get("id") or str(x),
                         "description": (x.get("description") or "")[:160]}
                        if isinstance(x, dict) else
                        {"id": str(x), "name": str(x), "description": ""}
                        for x in items
                    ]
                    break
            except Exception:
                continue

    # Ontologies
    for cap_name in ("ontologies.list", "ontology.list", "ontologies.registry"):
        cap = CAPABILITY_REGISTRY.get(cap_name)
        if cap:
            try:
                r = await cap["func"](trace_id=trace_id or "")
                items = r.get("ontologies") or r.get("items") or r.get("list") or []
                if isinstance(items, list):
                    out["ontologies"] = [
                        {"id":   x.get("id") or x.get("name") or str(x),
                         "name": x.get("name") or x.get("id") or str(x),
                         "description": (x.get("description") or "")[:160]}
                        if isinstance(x, dict) else
                        {"id": str(x), "name": str(x), "description": ""}
                        for x in items
                    ]
                    break
            except Exception:
                continue

    # Agents
    for cap_name in ("agents.list", "agent.list", "agents.registry"):
        cap = CAPABILITY_REGISTRY.get(cap_name)
        if cap:
            try:
                r = await cap["func"](trace_id=trace_id or "")
                items = r.get("agents") or r.get("items") or r.get("list") or []
                if isinstance(items, list):
                    out["agents"] = [
                        {"id":          x.get("id") or x.get("name") or str(x),
                         "name":        x.get("name") or x.get("id") or str(x),
                         "label":       x.get("label") or x.get("name") or x.get("id") or str(x),
                         "avatar":      x.get("avatar") or "",
                         "description": (x.get("description") or "")[:160]}
                        if isinstance(x, dict) else
                        {"id": str(x), "name": str(x), "label": str(x), "avatar": "", "description": ""}
                        for x in items
                    ]
                    break
            except Exception:
                continue

    # Models / instances
    for cap_name in ("cluster.instances", "cluster.list", "ollama.instances",
                       "llm.instances", "llm.list_models", "ollama.models"):
        cap = CAPABILITY_REGISTRY.get(cap_name)
        if cap:
            try:
                r = await cap["func"](trace_id=trace_id or "")
                # Various shapes
                items = (r.get("instances") or r.get("models")
                         or r.get("items") or r.get("list") or [])
                if isinstance(items, list) and items:
                    out["models"] = [
                        {"id":   x.get("id") or x.get("name") or x.get("model") or str(x),
                         "name": x.get("name") or x.get("model") or x.get("id") or str(x),
                         "instance_id": x.get("instance_id") or x.get("id") or "",
                         "is_gpu":   bool(x.get("is_gpu") or x.get("gpu")),
                         "healthy":  bool(x.get("healthy", True)),
                         "description": (x.get("description") or x.get("model") or "")[:160]}
                        if isinstance(x, dict) else
                        {"id": str(x), "name": str(x), "description": ""}
                        for x in items
                    ]
                    if r.get("current_model"):
                        out["current_model"] = r["current_model"]
                    elif r.get("default_model"):
                        out["current_model"] = r["default_model"]
                    break
            except Exception:
                continue

    return out


@capability(
    "workshop.triage.preview", memory="off",
    http_method="POST", http_path="/workshop/triage/preview",
    http_tags=["workshop", "agents"], silent=True,
    description=(
        "Preview the workshop's improved triage + toolkit-build for a goal "
        "WITHOUT running the agent loop. Useful for debugging cap selection. "
        "Inputs: goal (str!), allowed_caps (str), triage_top_k (int default 16), "
        "model (str). "
        "Output: {triage, toolkit, useful_caps_present, category_essentials, "
        "category_prefixes}."
    ),
)
async def cap_workshop_triage_preview(goal: str = "", allowed_caps: str = "",
                                        triage_top_k: int = 16,
                                        model: str = "",
                                        trace_id=None):
    if not goal:
        return {"error": "goal required"}
    triage = await _workshop_triage_goal(goal, model=model)
    toolkit = _workshop_build_toolkit(
        allowed_caps=allowed_caps,
        category=triage.get("category", "other"),
        categories=triage.get("categories"),
        keywords=triage.get("keywords", []),
        top_k=int(triage_top_k),
    )
    return {
        "triage":              triage,
        "toolkit":             toolkit,
        "toolkit_size":        len(toolkit),
        "useful_caps_present": _have_useful_caps(toolkit, triage.get("category", "other")),
        "category_essentials": CATEGORY_BASE_ESSENTIALS.get(triage.get("category", "other"), []),
        "category_prefixes":   CATEGORY_PREFIX_HINTS.get(triage.get("category", "other"), []),
    }


@capability(
    "dag.agent_loop_openclaw",
    http_method="POST", http_path="/dag/agent_loop_openclaw",
    http_tags=["dag", "agents"],
    memory="on",
    streams=["dag.agent_loop_openclaw"],
    description=(
        "Openclaw-style agent loop: maintains full message history and emits "
        "explicit tool_use blocks, supports HITL approval, expand_tools, and "
        "satisfaction checks. Long-running tools (research.run, ml.train, etc.) "
        "are awaited until the underlying job actually completes (poll-based). "
        "Inputs: goal (str!), allowed_caps (csv str — empty = auto), "
        "max_cycles (int default 10), require_approval (bool default False), "
        "satisfaction_check (bool default True), enable_expand (bool default True), "
        "triage_top_k (int default 16), await_long_running (bool default True), "
        "long_running_timeout_secs (int default 1800), "
        "max_search_calls (int default 2 — hard quota for caps.search/context.search_caps), "
        "max_expands (int default 1 — hard quota for expand_tools), "
        "count_failed_cycles (bool default False — errored cycles don't consume budget), "
        "model (str), instance_id (str), prefer_gpu (bool), session_id (str). "
        "Output: {goal, history, messages, cycles, done, final, toolkit, triage, stream_id}."
    ),
)
async def cap_dag_agent_loop_openclaw(
    goal:               str,
    allowed_caps:       str  = "",
    max_cycles:         int  = 10,
    require_approval:   bool = False,
    satisfaction_check: bool = True,
    enable_expand:      bool = True,
    model:              str  = "",
    instance_id:        str  = "",
    prefer_gpu:         bool = True,
    attach_skills:      str  = "",
    attach_ontologies:  str  = "",
    session_id:         str  = "",
    triage_top_k:       int  = 16,
    hitl_timeout_secs:  int  = 300,
    await_long_running: bool = True,
    long_running_timeout_secs: int = 1800,
    handover:           bool = False,
    handover_max_chars: int  = 20000,
    max_search_calls:   int  = 2,    # cap on caps.search/context.search_caps calls
    max_expands:        int  = 1,    # cap on toolkit-expand calls (was 3 internal)
    count_failed_cycles: bool = False,  # if False, errored cycles don't consume max_cycles
    max_recovery_attempts: int = 2,  # arg-only retries when a tool fails with a recoverable error
    system_prompt_template: str = "",  # optional user-provided system prompt template
    trace_id=None,
):
    if not goal:
        return {"error": "goal required"}
    max_cycles = max(1, min(40, int(max_cycles)))
    triage_top_k = max(1, min(64, int(triage_top_k)))
    sid = session_id or str(uuid.uuid4())

    ctx = _ctx()
    ds  = _dag_store()
    ollama_generate = getattr(ctx, "ollama_generate", None) if ctx else None
    if ollama_generate is None:
        return {"error": "context module not loaded — ollama_generate missing"}

    # ── Stage 1: TRIAGE (workshop's improved, anchored, cached version) ─────
    await emit_event({
        "type": "agent_loop_openclaw.triage_start",
        "goal": goal[:200], "session_id": sid,
    })
    try:
        triage = await _workshop_triage_goal(
            goal, model=model, instance_id=instance_id, prefer_gpu=prefer_gpu,
        )
    except Exception as e:
        log.debug("openclaw triage failed: %s", e)
        triage = {"category": "other", "keywords": [], "reasoning": ""}
    await emit_event({
        "type": "agent_loop_openclaw.triage_done",
        "triage": triage, "session_id": sid,
    })

    # ── Stage 2: SEED TOOLKIT (category-aware, prefix-expanded) ─────────────
    toolkit = _workshop_build_toolkit(
        allowed_caps=allowed_caps,
        category=triage.get("category", "other"),
        categories=triage.get("categories"),
        keywords=triage.get("keywords", []),
        top_k=int(triage_top_k),
    )

    if not toolkit:
        return {"error": "No usable tools after triage", "triage": triage}

    # ── Optional skills/ontologies context ──────────────────────────────────
    ctx_extra = ""
    build_context_prompt = getattr(ctx, "build_context_prompt", None)
    if (attach_skills or attach_ontologies) and build_context_prompt:
        try:
            cobj = await build_context_prompt(
                goal,
                attach_skills=attach_skills,
                attach_ontologies=attach_ontologies,
            )
            ctx_extra = cobj.get("system_prompt", "")
        except Exception as e:
            log.debug("openclaw context build: %s", e)

    def _toolkit_block(names: List[str]) -> str:
        return "\n".join(rich_cap_signature(n) for n in names)

    if system_prompt_template and system_prompt_template.strip():
        # User-supplied template — expand variables
        system_prompt = _expand_prompt_template(
            system_prompt_template,
            goal=goal,
            category=triage.get("category", ""),
            keywords=", ".join(triage.get("keywords") or []),
            reasoning=triage.get("reasoning", ""),
            toolkit_block=_toolkit_block(toolkit),
            toolkit_brief=", ".join(toolkit),
            toolkit_count=len(toolkit),
            ctx_extra=ctx_extra,
            enable_expand=enable_expand,
        )
    else:
        system_prompt = _openclaw_system_prompt(
            goal, _toolkit_block(toolkit),
            extra=ctx_extra, enable_expand=enable_expand,
            toolkit_names=list(toolkit),
        )

    # ── Stream registration ─────────────────────────────────────────────────
    stream_register = getattr(ctx, "stream_register", None)
    stream_complete = getattr(ctx, "stream_complete", None)
    stream_append   = getattr(ctx, "stream_append_token", None)
    stream_id = ""
    if stream_register:
        try:
            stream_id = await stream_register(
                kind          = "agent_loop_openclaw",
                source_cap    = "dag.agent_loop_openclaw",
                session_id    = sid,
                label         = goal[:80],
                persist_full  = True,
                fabric_dataset = "streams.agent_loop_openclaw",
                metadata      = {"goal": goal, "max_cycles": max_cycles,
                                  "triage": triage, "initial_toolkit": list(toolkit),
                                  "require_approval": require_approval},
            )
        except Exception:
            stream_id = ""

    await emit_event({
        "type": "agent_loop_openclaw.toolkit",
        "stream_id": stream_id, "toolkit": list(toolkit),
        "session_id": sid,
    })

    # ── Message history ─────────────────────────────────────────────────────
    messages: List[Dict[str, str]] = []
    history:  List[Dict[str, Any]] = []

    cycles  = 0
    productive_cycles = 0  # cycles that resulted in a tool call (used when count_failed_cycles=False)
    done    = False
    final   = ""
    expand_count = 0
    search_count = 0
    MAX_EXPANDS = max(0, int(max_expands))
    MAX_SEARCH_CALLS = max(1, int(max_search_calls))
    SEARCH_CAPS = {"caps.search", "caps.describe", "context.search_caps", "context.search_dags"}

    _check_goal_satisfied = getattr(ctx, "_check_goal_satisfied", None)
    _agent_loop_call_tool = getattr(ctx, "_agent_loop_call_tool", None)
    if _agent_loop_call_tool is None:
        async def _call(cap_name, args, **kw):
            cap = CAPABILITY_REGISTRY.get(cap_name)
            if not cap:
                return {"ok": False, "error": f"Unknown cap: {cap_name}"}
            accepted = set(cap.get("schema", {}).get("properties", {}).keys()) | {"trace_id"}
            kwargs = {k: v for k, v in (args or {}).items() if k in accepted}
            try:
                result = await cap["func"](**kwargs, trace_id=kw.get("trace_id", "") or "")
                return {"ok": True, "result": result}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        _agent_loop_call_tool = _call  # type: ignore

    try:
        cycle_i = 0
        while True:
            # Productive-only counting: errored or blocked steps don't consume the
            # cycle budget when count_failed_cycles is False.
            if count_failed_cycles:
                if cycle_i >= max_cycles:
                    break
            else:
                if productive_cycles >= max_cycles:
                    break
                # Hard-limit absolute iterations to prevent infinite loops on
                # constant errors. 3x max_cycles is a generous safety margin.
                if cycle_i >= max_cycles * 3:
                    summary = (f"Aborted: hit hard iteration limit ({cycle_i} cycles, "
                                f"{cycle_i - productive_cycles} errored).")
                    history.append({"tool": "(hard_limit)", "args": {},
                                     "ok": False, "preview": summary})
                    done = True
                    break
            cycle_i += 1
            cycles = cycle_i

            obs_block = "\n\n".join(
                f"[Observation {i+1}] tool={h['tool']} ok={h.get('ok')}\n"
                f"args: {json.dumps(h.get('args', {}), default=str)[:240]}\n"
                f"result: {h['preview']}"
                for i, h in enumerate(history[-5:])
            ) or "(no observations yet — make your first tool call)"

            # Quota status hint so the LLM knows when to wrap up
            remaining = max_cycles - productive_cycles
            quota_hint = (
                f"\n\nQUOTA: searches used {search_count}/{MAX_SEARCH_CALLS}, "
                f"expansions used {expand_count}/{MAX_EXPANDS}, "
                f"productive cycles {productive_cycles}/{max_cycles}."
            )
            if remaining <= 2:
                quota_hint += (
                    f"\n\n⚠ BUDGET WARNING: Only {remaining} cycle(s) remaining! "
                    "You MUST emit {\"thought\":\"...\",\"final\":\"<your answer>\"} on your next turn "
                    "to deliver results before the budget expires. Summarize everything you've "
                    "found so far into a final answer NOW."
                )
            elif remaining <= 4:
                quota_hint += (
                    f"\nNote: {remaining} cycles remaining — start wrapping up. "
                    "If you have useful results, emit final soon."
                )
            user_msg = (
                f"REMINDER: GOAL = \"{goal}\"\n\n"
                "These are your past tool results (NOT a new user message):\n\n"
                + obs_block
                + quota_hint
                + "\n\nEmit the next JSON action toward the GOAL."
            )

            await emit_event({
                "type": "agent_loop_openclaw.cycle_planning",
                "stream_id": stream_id, "cycle": cycles, "session_id": sid,
            })

            try:
                raw = await _safe_ollama_generate_dw(
                    user_msg, system=system_prompt,
                    model=model, instance_id=instance_id,
                    prefer_gpu=bool(prefer_gpu),
                    json_mode=True,
                )
            except Exception as e:
                _err_preview = f"Planner LLM failed: {e}"
                history.append({"tool": "(planner_error)", "args": {},
                                 "ok": False,
                                 "preview": _err_preview})
                await emit_event({
                    "type": "agent_loop_openclaw.tool_done",
                    "stream_id": stream_id, "cycle": cycles,
                    "tool": "(planner)", "ok": False,
                    "elapsed_ms": 0,
                    "preview": _err_preview[:400],
                    "error": str(e)[:500],
                    "session_id": sid,
                })
                continue

            # Strip thinking tokens; emit think event for UI toggle
            _raw_clean, _think_text = _strip_think(raw or "")
            if _think_text:
                await emit_event({
                    "type": "agent_loop_openclaw.think",
                    "stream_id": stream_id, "cycle": cycles,
                    "thought": _think_text[:2000], "session_id": sid,
                })
            if stream_append and stream_id:
                try:
                    if _think_text:
                        await stream_append(stream_id, f"\n[think #{cycles}] {_think_text[:400]}\n")
                    await stream_append(stream_id, f"\n[plan #{cycles}] {_raw_clean[:600]}\n")
                except Exception:
                    pass

            messages.append({"role": "assistant", "content": _raw_clean.strip()[:4000]})

            raw_action = _extract_json(_raw_clean)
            # Try canonicalisation FIRST — it rescues common LLM
            # malformations (tool/name/function/etc. → tool_use:{name,input}).
            action = _canonicalise_tool_use_payload(raw_action) if raw_action else None
            if not isinstance(action, dict):
                # Couldn't even rescue — record as parse_error and inject a
                # corrective system message so the next cycle gets clearer
                # guidance.
                _parse_preview = f"Could not parse JSON: {(raw or '')[:300]}"
                history.append({"tool": "(parse_error)", "args": {},
                                 "ok": False,
                                 "preview": _parse_preview})
                await emit_event({
                    "type": "agent_loop_openclaw.tool_done",
                    "stream_id": stream_id, "cycle": cycles,
                    "tool": "(planner)", "ok": False,
                    "elapsed_ms": 0,
                    "preview": _parse_preview[:400],
                    "error": _parse_preview[:500],
                    "session_id": sid,
                })
                messages.append({"role": "user",
                                  "content": "[system] Your previous response was not valid JSON. "
                                              "Reply ONLY with one JSON object: "
                                              '{"thought":"...","tool_use":{"name":"<cap.name>","input":{...}}} '
                                              'or {"thought":"...","final":"..."}.'})
                continue

            thought = action.get("thought", "")

            # ── Final answer? ───────────────────────────────────────────────
            if action.get("final"):
                final = str(action["final"])
                done = True
                await emit_event({
                    "type":      "agent_loop_openclaw.done",
                    "stream_id": stream_id, "cycles": cycles,
                    "summary":   final, "reason": "final",
                    "session_id": sid,
                })
                break

            # ── Toolkit expand ──────────────────────────────────────────────
            if action.get("action") == "expand_tools" or action.get("expand_tools"):
                # Cycle-1 expand block when toolkit is already well-populated
                if cycles == 1 and len(toolkit) >= 5:
                    _disc_preview = (
                        f"Skipped expand_tools on cycle 1 — your toolkit "
                        f"already contains {len(toolkit)} curated tools."
                    )
                    history.append({
                        "tool": "(cycle1_expand_blocked)",
                        "args": {}, "ok": False,
                        "preview": _disc_preview,
                    })
                    await emit_event({
                        "type": "agent_loop_openclaw.tool_done",
                        "stream_id": stream_id, "cycle": cycles,
                        "tool": "expand_tools", "ok": False,
                        "elapsed_ms": 0,
                        "preview": _disc_preview[:400],
                        "error": "cycle-1 expand blocked",
                        "session_id": sid,
                    })
                    messages.append({"role": "user",
                                      "content": "[system] You called expand_tools as "
                                                 "your FIRST action without trying the "
                                                 "curated toolkit. Read it carefully — "
                                                 "the right tool is likely already there."})
                    continue
                # Hard quota: max_expands reached?
                if not enable_expand or expand_count >= MAX_EXPANDS:
                    # If we've already blocked twice in a row, force-finalise.
                    consec_blocked = sum(1 for h in history[-2:]
                                          if h.get("tool") == "(expand_blocked)")
                    if consec_blocked >= 2:
                        summary = (f"Aborted: agent kept requesting expand_tools after "
                                    f"{MAX_EXPANDS} expansions. Use the existing toolkit.")
                        history.append({"tool": "(force_final)", "args": {},
                                         "ok": False, "preview": summary})
                        final = summary
                        done  = True
                        break
                    history.append({"tool": "(expand_blocked)", "args": {},
                                     "ok": False,
                                     "preview": f"Expand quota exhausted ({MAX_EXPANDS} used). "
                                                "Pick from existing toolkit or emit final."})
                    await emit_event({
                        "type": "agent_loop_openclaw.tool_done",
                        "stream_id": stream_id, "cycle": cycles,
                        "tool": "expand_tools", "ok": False,
                        "elapsed_ms": 0,
                        "preview": f"Expand quota exhausted ({MAX_EXPANDS} used)",
                        "error": "expand quota exhausted",
                        "session_id": sid,
                    })
                    messages.append({"role": "user",
                                      "content": f"[system] You have used your "
                                                 f"{MAX_EXPANDS} toolkit expansions. "
                                                 "Stop requesting expand_tools. Either pick a tool "
                                                 'from your existing toolkit or emit {"thought":"...","final":"..."}.'})
                    continue
                # Block if toolkit already has the obvious useful caps for category
                if _have_useful_caps(toolkit, triage.get("category", "other")):
                    consec_blocked = sum(1 for h in history[-2:]
                                          if h.get("tool") == "(expand_blocked)")
                    if consec_blocked >= 2:
                        summary = ("Aborted: agent kept requesting expand_tools after "
                                    "useful caps were already present.")
                        history.append({"tool": "(force_final)", "args": {},
                                         "ok": False, "preview": summary})
                        final = summary
                        done  = True
                        break
                    history.append({
                        "tool": "(expand_blocked)", "args": {},
                        "ok": False,
                        "preview": (f"Expansion blocked — toolkit already contains the "
                                     f"primary caps for category '{triage.get('category','')}'. "
                                     f"Pick from your existing toolkit or emit final."),
                    })
                    await emit_event({
                        "type": "agent_loop_openclaw.tool_done",
                        "stream_id": stream_id, "cycle": cycles,
                        "tool": "expand_tools", "ok": False,
                        "elapsed_ms": 0,
                        "preview": f"Expansion blocked — useful caps already in toolkit",
                        "error": "expand blocked: useful caps present",
                        "session_id": sid,
                    })
                    messages.append({"role": "user",
                                      "content": "[system] Expansion blocked: your toolkit "
                                                 "already contains the right tools for this task. "
                                                 "Pick a tool from the toolkit list or emit "
                                                 '{"thought":"...","final":"..."} if you cannot proceed.'})
                    continue
                expand_count += 1
                # Normalize keywords — agents commonly emit a list, sometimes a
                # string, occasionally a dict. Coerce all of them to a single
                # search-friendly string. Without this we crash on
                # `'list' object has no attribute 'strip'` further down when the
                # LLM emits {action:"expand_tools", keywords:["a","b","c"]}.
                def _norm_kw(v):
                    if isinstance(v, list):
                        return " ".join(str(x).strip() for x in v if str(x).strip())
                    if isinstance(v, dict):
                        return " ".join(str(x).strip() for x in v.values() if str(x).strip())
                    if isinstance(v, str):
                        return v.strip()
                    return ""
                kws = (_norm_kw(action.get("keywords"))
                        or _norm_kw(action.get("expand_tools"))
                        or "")
                added: List[str] = []
                if kws and ds and getattr(ds, "CAP_INDEX", None):
                    try:
                        hits = await ds.CAP_INDEX.relevance_search(kws, top_k=8)
                        for n, _s in hits:
                            if n in CAPABILITY_REGISTRY and n not in toolkit:
                                toolkit.append(n)
                                added.append(n)
                                if len(added) >= 5:
                                    break
                    except Exception:
                        pass
                if system_prompt_template and system_prompt_template.strip():
                    system_prompt = _expand_prompt_template(
                        system_prompt_template,
                        goal=goal,
                        category=triage.get("category", ""),
                        keywords=", ".join(triage.get("keywords") or []),
                        reasoning=triage.get("reasoning", ""),
                        toolkit_block=_toolkit_block(toolkit),
                        toolkit_brief=", ".join(toolkit),
                        toolkit_count=len(toolkit),
                        ctx_extra=ctx_extra,
                        enable_expand=enable_expand,
                    )
                else:
                    system_prompt = _openclaw_system_prompt(
                        goal, _toolkit_block(toolkit),
                        extra=ctx_extra, enable_expand=enable_expand,
                        toolkit_names=list(toolkit),
                    )
                history.append({"tool": "(expand_tools)",
                                 "args": {"keywords": kws}, "ok": True,
                                 "preview": f"Toolkit expanded by {len(added)}: {added}"})
                await emit_event({
                    "type": "agent_loop_openclaw.toolkit",
                    "stream_id": stream_id, "toolkit": list(toolkit),
                    "added": added, "session_id": sid,
                })
                await emit_event({
                    "type": "agent_loop_openclaw.tool_done",
                    "stream_id": stream_id, "cycle": cycles,
                    "tool": "expand_tools", "ok": True,
                    "elapsed_ms": 0,
                    "preview": f"Expanded: +{len(added)} caps ({', '.join(added[:5]) or 'none found'})",
                    "session_id": sid,
                })
                continue

            # ── tool_use ────────────────────────────────────────────────────
            tu = action.get("tool_use") or action.get("tool_call") or {}
            if not isinstance(tu, dict):
                tu = {}
            tool = (tu.get("name") or action.get("tool")
                     or action.get("capability") or "").strip()
            args = tu.get("input") or action.get("args") or action.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}

            if not tool:
                history.append({"tool": "(none)", "args": args, "ok": False,
                                 "preview": "Action did not include tool_use.name or final"})
                await emit_event({
                    "type": "agent_loop_openclaw.tool_done",
                    "stream_id": stream_id, "cycle": cycles,
                    "tool": "(none)", "ok": False,
                    "elapsed_ms": 0,
                    "preview": "No tool name in action — need tool_use.name or final",
                    "error": "missing tool name",
                    "session_id": sid,
                })
                messages.append({"role": "user",
                                  "content": "[system] Your previous response did not include a tool name. "
                                             'Either pick a tool: {"thought":"...","tool_use":{"name":"<cap.name>","input":{...}}} '
                                             'or finish: {"thought":"...","final":"..."}'})
                continue

            if tool not in toolkit and tool not in CAPABILITY_REGISTRY:
                _bad_tool_preview = (
                    f"ERROR: '{tool}' not in toolkit. Currently visible: "
                    f"{', '.join(toolkit[:10])}"
                    + ("…" if len(toolkit) > 10 else "")
                )
                history.append({
                    "tool": tool, "args": args, "ok": False,
                    "preview": _bad_tool_preview,
                })
                await emit_event({
                    "type": "agent_loop_openclaw.tool_done",
                    "stream_id": stream_id, "cycle": cycles,
                    "tool": tool, "ok": False,
                    "elapsed_ms": 0,
                    "preview": _bad_tool_preview[:400],
                    "error": f"Unknown tool: {tool}",
                    "session_id": sid,
                })
                continue

            # ── Repetition guard: same (tool, args) seen recently? ──────────
            if _detect_repetition(history, tool, args, lookback=4, threshold=2):
                msg = (f"REPETITION DETECTED: tool '{tool}' has been called with "
                       f"identical arguments multiple times. The result will be the same. "
                       f"You MUST either: (a) call expand_tools with new keywords, "
                       f"(b) try a different tool, or (c) emit final with what you've found.")
                history.append({
                    "tool":    "(repetition_block)",
                    "args":    {"tool": tool, "args": args},
                    "ok":      False,
                    "preview": msg,
                })
                messages.append({"role": "user", "content": "[system] " + msg})
                await emit_event({
                    "type":       "agent_loop_openclaw.repetition_block",
                    "stream_id":  stream_id, "cycle": cycles,
                    "tool":       tool, "args": args,
                    "session_id": sid,
                })
                continue

            # ── HITL pause ──────────────────────────────────────────────────
            if require_approval:
                await emit_event({
                    "type": "agent_loop_openclaw.hitl_request",
                    "stream_id": stream_id, "cycle": cycles, "step": cycles - 1,
                    "tool": tool, "args": args, "thought": thought,
                    "session_id": sid,
                    "timeout_secs": hitl_timeout_secs,
                })
                decision_obj = await _await_hitl_decision(
                    sid, cycles - 1, timeout=float(hitl_timeout_secs),
                )
                decision = decision_obj.get("decision", "")
                if decision == "abort":
                    final = "Aborted by user during HITL approval."
                    done = True
                    await emit_event({
                        "type": "agent_loop_openclaw.done",
                        "stream_id": stream_id, "cycles": cycles,
                        "summary": final, "reason": "hitl_abort",
                        "session_id": sid,
                    })
                    break
                if decision == "reject":
                    history.append({
                        "tool": tool, "args": args, "ok": False,
                        "preview": ("HITL: user rejected this step. "
                                     + (decision_obj.get("comment") or "")),
                    })
                    await emit_event({
                        "type": "agent_loop_openclaw.hitl_resolved",
                        "stream_id": stream_id, "cycle": cycles,
                        "decision": "reject", "session_id": sid,
                    })
                    continue
                if decision == "edit":
                    new_args = decision_obj.get("args") or {}
                    if isinstance(new_args, dict):
                        args = new_args
                    await emit_event({
                        "type": "agent_loop_openclaw.hitl_resolved",
                        "stream_id": stream_id, "cycle": cycles,
                        "decision": "edit", "args": args, "session_id": sid,
                    })
                elif decision == "approve":
                    await emit_event({
                        "type": "agent_loop_openclaw.hitl_resolved",
                        "stream_id": stream_id, "cycle": cycles,
                        "decision": "approve", "session_id": sid,
                    })
                else:
                    await emit_event({
                        "type": "agent_loop_openclaw.hitl_resolved",
                        "stream_id": stream_id, "cycle": cycles,
                        "decision": "auto_approve_timeout", "session_id": sid,
                    })

            # ── Deterministic arg coercion (pre-LLM) ────────────────────────
            # Drops unknown args, coerces types, fixes enum case, fills defaults.
            # If anything was changed, we tell the LLM in the next message so
            # it learns from the correction without us having to re-prompt.
            coerced_args, coerce_notes = _coerce_args(tool, args)
            if coerce_notes:
                args = coerced_args
                await emit_event({
                    "type": "agent_loop_openclaw.args_coerced",
                    "stream_id": stream_id, "cycle": cycles,
                    "tool": tool, "notes": coerce_notes, "session_id": sid,
                })

            # ── Cycle-1 discovery block ─────────────────────────────────────
            # Even if the search quota allows it, calling caps.search /
            # context.search_caps / expand_tools as the FIRST action when
            # the toolkit is well-populated is a sign the agent didn't read
            # the toolkit. Bounce them back with a clear nudge.
            if (cycles == 1 and tool in SEARCH_CAPS and len(toolkit) >= 5):
                history.append({
                    "tool": "(cycle1_discovery_blocked)",
                    "args": args, "ok": False,
                    "preview": (f"Skipped {tool} on cycle 1 — your toolkit already "
                                 f"contains {len(toolkit)} curated tools. Read them and "
                                 "pick one that matches the goal."),
                })
                messages.append({"role": "user",
                                  "content": f"[system] You called {tool} as your "
                                             "FIRST action without reading the toolkit. "
                                             "The toolkit was already filtered for this "
                                             "goal — please pick a tool from it. If after "
                                             "reviewing the toolkit you genuinely need to "
                                             "search, you can do so on cycle 2+."})
                continue

            # ── Search/discovery quota enforcement ──────────────────────────
            # Block over-use of caps.search / context.search_caps. Once the
            # quota is hit, the agent must either use a real tool or emit final.
            if tool in SEARCH_CAPS:
                search_count += 1
                if search_count > MAX_SEARCH_CALLS:
                    history.append({
                        "tool": "(search_quota_exceeded)",
                        "args": {"tool": tool, "limit": MAX_SEARCH_CALLS},
                        "ok": False,
                        "preview": (f"Search quota exhausted ({MAX_SEARCH_CALLS} calls used). "
                                     f"Stop searching — pick a tool from the existing toolkit "
                                     f"or emit final."),
                    })
                    messages.append({"role": "user",
                                      "content": f"[system] You have used your "
                                                 f"{MAX_SEARCH_CALLS} discovery searches. "
                                                 "Stop calling caps.search / context.search_caps. "
                                                 "Either pick an actual capability from your toolkit, "
                                                 'or emit {"thought":"...","final":"..."}.'})
                    continue

            # ── Tool execution ──────────────────────────────────────────────
            productive_cycles += 1  # this counts toward max_cycles only on real attempts
            await emit_event({
                "type": "agent_loop_openclaw.tool_call",
                "stream_id": stream_id, "cycle": cycles,
                "tool": tool, "args": args, "thought": thought,
                "long_running": _is_long_running_cap(tool),
                "will_await":   await_long_running and _should_await(tool),
                "session_id": sid,
            })

            t0 = time.monotonic()
            invoke = await _agent_loop_call_tool(tool, args,
                                                   session_id=sid,
                                                   trace_id=trace_id or "")
            # ── Detect error-in-result ─────────────────────────────────────
            # Caps often return {"error": "..."} on bad args while the wrapper
            # still says ok=True. Without this check, the agent treats those
            # as successes and never sees the real failure. Promote them.
            if invoke.get("ok") and isinstance(invoke.get("result"), dict):
                rerr = invoke["result"].get("error")
                if rerr:
                    invoke["ok"] = False
                    invoke["error"] = str(rerr)

            # ── Error recovery: re-call the SAME tool with fixed args ─────
            # When the failure looks like a bad-arg / schema problem, run
            # a tightly-scoped LLM sub-cycle that can only edit `input`.
            # This avoids burning a planning cycle on a fixable mistake AND
            # prevents the agent from picking a different, wrong tool.
            recovery_result = None
            if (not invoke.get("ok")
                    and max_recovery_attempts > 0
                    and _is_arg_error(invoke.get("error", ""))):
                recovery_result = await _attempt_arg_recovery(
                    cap_name=tool, failed_args=args,
                    error_text=invoke.get("error", ""),
                    model=model, instance_id=instance_id,
                    prefer_gpu=prefer_gpu,
                    max_attempts=int(max_recovery_attempts),
                    call_tool=_agent_loop_call_tool,
                    session_id=sid, trace_id=trace_id or "",
                    emit_fn=emit_event, cycle=cycles,
                    stream_id=stream_id,
                )
                if recovery_result.get("recovered"):
                    invoke = recovery_result["final_invoke"]
                    # Update args to the successful recovery args for history
                    last_a = recovery_result.get("attempts") or []
                    if last_a:
                        args = last_a[-1].get("args", args)
                else:
                    # Recovery failed — annotate the error so history shows it
                    invoke["error"] = (str(invoke.get("error", "")) + " "
                        f"[recovery: {len(recovery_result.get('attempts', []))} attempt(s) failed]")

            # ── Wait for long-running jobs to actually finish ───────────────
            # Universal: any cap returning a job_id gets polled, regardless
            # of whether it's in the static LONG_RUNNING_AWAIT_MAP.
            if (invoke.get("ok") and await_long_running
                    and isinstance(invoke.get("result"), dict)):
                immediate = invoke["result"]
                job_id_detected = _detect_job_id(immediate)
                if job_id_detected:
                    awaited = await _universal_await_job(
                        cap_name=tool, immediate=immediate,
                        session_id=sid, trace_id=trace_id or "",
                        cycle=cycles,
                        max_wait_secs=float(long_running_timeout_secs),
                        stream_id=stream_id,
                    )
                    invoke["result"] = awaited
                    if isinstance(awaited, dict) and awaited.get("_await_error"):
                        invoke["ok"] = False
                        invoke["error"] = awaited["_await_error"]
                    elif isinstance(awaited, dict) and awaited.get("error"):
                        invoke["ok"] = False
                        invoke["error"] = str(awaited["error"])
                elif _should_await(tool):
                    # Tool was tagged long-running but didn't return a job_id —
                    # likely an arg error. Surface that explicitly.
                    log.debug("await: %s tagged long-running but no job_id; result keys=%s",
                              tool, list(immediate.keys())[:8])
                    await emit_event({
                        "type":       "agent_loop.long_running_await_skipped",
                        "tool":       tool,
                        "reason":     "no_job_id",
                        "result_keys": list(immediate.keys())[:12],
                        "session_id": sid, "cycle":  cycles,
                    })

            elapsed = round((time.monotonic() - t0) * 1000)

            # ── Detect "search returned nothing useful" ────────────────────
            # caps.search / context.search_caps that return count=0 are not
            # technical errors but they ARE actionable: the agent must change
            # query OR drop the search and pick from the existing toolkit.
            empty_search = False
            if invoke.get("ok") and isinstance(invoke.get("result"), dict):
                rd = invoke["result"]
                if tool in ("caps.search", "context.search_caps", "context.search_dags"):
                    n = (rd.get("count")
                         or len(rd.get("results") or [])
                         or len(rd.get("hits") or [])
                         or len(rd.get("caps") or []))
                    if n == 0:
                        empty_search = True

            if invoke.get("ok"):
                preview = _result_preview(invoke["result"])
                if empty_search:
                    preview = (
                        "WARNING: search returned 0 results. Stop searching with this query — "
                        "either use a tool from your existing toolkit, broaden the keywords "
                        "(drop proper nouns), or emit final.\n\n"
                        "Raw result: " + preview
                    )
            else:
                preview = "ERROR: " + invoke.get("error", "unknown error")
                # Append coercion hint to error so the next LLM cycle sees it
                if coerce_notes:
                    preview += "\n\nNote: arguments were auto-coerced before "
                    preview += "this call: " + "; ".join(coerce_notes[:4])

            history.append({
                "tool":    tool,
                "args":    args,
                "ok":      bool(invoke.get("ok")),
                "preview": preview,
                "ms":      elapsed,
                "thought": thought,
                "coerce_notes": coerce_notes if coerce_notes else None,
                "empty_search": empty_search,
            })
            messages.append({"role": "user",
                              "content": f"[tool_result {tool}]\n{preview[:1200]}"})

            if stream_append and stream_id:
                try:
                    await stream_append(stream_id,
                                         f"\n[exec #{cycles}] {tool}({json.dumps(args, default=str)[:200]}) → {preview[:400]}\n")
                except Exception:
                    pass

            await emit_event({
                "type": "agent_loop_openclaw.tool_done",
                "stream_id": stream_id, "cycle": cycles,
                "tool":          tool,
                "ok":            invoke.get("ok"),
                "elapsed_ms":    elapsed,
                "preview":       preview[:2000],
                "error":         invoke.get("error", "") if not invoke.get("ok") else "",
                "empty_search":  empty_search,
                "session_id":    sid,
            })

            # ── Satisfaction check ──────────────────────────────────────────
            if satisfaction_check and invoke.get("ok") and _check_goal_satisfied:
                try:
                    sat = await _check_goal_satisfied(
                        goal, preview,
                        model=model, instance_id=instance_id,
                        prefer_gpu=prefer_gpu,
                    )
                except Exception:
                    sat = {"satisfied": False, "summary": ""}
                if sat.get("satisfied"):
                    final = sat.get("summary") or "Goal satisfied."
                    done  = True
                    await emit_event({
                        "type":      "agent_loop_openclaw.done",
                        "stream_id": stream_id, "cycles": cycles,
                        "summary":   final, "reason": "satisfaction_check",
                        "session_id": sid,
                    })
                    break

    finally:
        if stream_complete and stream_id:
            try:
                await stream_complete(stream_id)
            except Exception:
                pass

    # ── Optional: HANDOVER stage ─────────────────────────────────────────
    # The agent's `final` is often terse or a result-count rather than a
    # synthesized answer. When handover=True, we run a separate LLM pass
    # that takes the FULL history (every tool call + observation) and
    # writes a real answer. No tools, no looping — just synthesis.

    # Auto-summary when budget exhausted without final
    if not done and not final and history:
        ok_steps = [h for h in history if h.get("ok") and not h.get("tool","").startswith("(")]
        if ok_steps:
            last_previews = [h.get("preview","")[:200] for h in ok_steps[-3:]]
            final = (
                f"Budget exhausted ({cycles} cycles) before agent emitted done. "
                f"Last {len(ok_steps)} successful calls produced: "
                + " | ".join(last_previews)
            )
        else:
            final = f"Budget exhausted ({cycles} cycles) — no successful tool calls."
        done = True
        await emit_event({
            "type":      "agent_loop_openclaw.done",
            "stream_id": stream_id, "cycles": cycles,
            "summary":   final, "reason": "budget_exhausted",
            "session_id": sid,
        })

    handover_output = ""
    if handover and history:
        try:
            ho = await _run_handover_stage(
                goal=goal, history=history, triage=triage,
                cur_final=final,
                model=model, instance_id=instance_id,
                prefer_gpu=prefer_gpu,
                max_chars=int(handover_max_chars),
                session_id=sid,
            )
            handover_output = ho or ""
            if handover_output:
                final = handover_output  # prefer the synthesized output
        except Exception as e:
            log.debug("handover stage failed: %s", e)
            await emit_event({
                "type": "agent_loop_openclaw.handover_error",
                "error": str(e), "session_id": sid,
            })

    return {
        "goal":      goal,
        "triage":    triage,
        "toolkit":   toolkit,
        "history":   history,
        "messages":  messages,
        "cycles":    cycles,
        "done":      done,
        "summary":   final,
        "final":     final,
        "handover_output":  handover_output,
        "stream_id": stream_id,
        "session_id": sid,
    }


# ═════════════════════════════════════════════════════════════════════════════
# UNIFIED SSE WRAPPER FOR ALL THREE LOOP VARIANTS
# ─────────────────────────────────────────────────────────────────────────────
# Body: {goal, version: "v1"|"v2"|"openclaw", ...other args}
# Forwards an enriched event stream including:
#   • The agent_loop_*.* events
#   • Long-running tool progress (research.*, exec.*, ml_training.*)
#   • LLM token streams — surfaced as tool_progress
#   • HITL request events (openclaw only)
#   • A final "result" event with the full structured response
# ═════════════════════════════════════════════════════════════════════════════


@APP.post("/workshop/agent_loop/stream")
async def workshop_agent_loop_stream(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    goal               = body.get("goal", "")
    allowed_caps       = body.get("allowed_caps", "")
    max_cycles         = int(body.get("max_cycles", 8) or 8)
    model              = body.get("model", "")
    instance_id        = body.get("instance_id", "")
    prefer_gpu         = bool(body.get("prefer_gpu", True))
    attach_skills      = body.get("attach_skills", "")
    attach_ontologies  = body.get("attach_ontologies", "")
    session_id         = body.get("session_id", "") or str(uuid.uuid4())
    version            = (body.get("version") or "v2").lower()
    satisfaction_check = bool(body.get("satisfaction_check", True))
    enable_expand      = bool(body.get("enable_expand", True))
    require_approval   = bool(body.get("require_approval", False))
    hitl_timeout_secs  = int(body.get("hitl_timeout_secs", 300))
    triage_top_k       = int(body.get("triage_top_k", 16) or 16)
    await_long_running = bool(body.get("await_long_running", True))
    long_running_timeout_secs = int(body.get("long_running_timeout_secs", 1800))
    handover           = bool(body.get("handover", False))
    handover_max_chars = int(body.get("handover_max_chars", 20000))
    agent_name         = body.get("agent_name", "") or ""
    run_id             = body.get("run_id", "") or ""
    max_search_calls   = int(body.get("max_search_calls", 2) or 2)
    max_expands        = int(body.get("max_expands", 1) or 1)
    count_failed_cycles = bool(body.get("count_failed_cycles", False))
    max_recovery_attempts = int(body.get("max_recovery_attempts", 2) or 0)
    system_prompt_template = body.get("system_prompt_template", "") or ""

    def _sse(payload):
        if run_id and isinstance(payload, dict):
            payload = {**payload, "run_id": run_id}
        return f"data: {json.dumps(payload, default=str)}\n\n".encode()

    # ── Agent resolution — if caller picked an agent, merge its config ──────
    # Precedence: explicit body params > agent record fields.
    # We resolve here so both the Redis path and the no-Redis fallback see it.
    if agent_name:
        agent_get_cap = CAPABILITY_REGISTRY.get("agent.get")
        if agent_get_cap:
            try:
                ag = await agent_get_cap["func"](name=agent_name, trace_id=session_id)
                if ag and not ag.get("error"):
                    # Only override model/instance if caller left them blank
                    if not model and ag.get("model"):
                        model = ag["model"]
                    if not instance_id and ag.get("instance_id"):
                        instance_id = ag["instance_id"]
                    # Merge domain_caps into allowed_caps (CSV union)
                    dom = ag.get("domain_caps") or []
                    if dom:
                        existing = {c.strip() for c in (allowed_caps or "").split(",") if c.strip()}
                        merged   = existing | set(dom)
                        allowed_caps = ",".join(sorted(merged))
                    # Merge agent skills/ontologies
                    if not attach_skills and ag.get("attach_skills"):
                        attach_skills = ag["attach_skills"]
                    if not attach_ontologies and ag.get("attach_ontologies"):
                        attach_ontologies = ag["attach_ontologies"]
            except Exception as e:
                log.debug("agent_name resolution failed for %s: %s", agent_name, e)

    cap_name_map = {
        "v1":       "dag.agent_loop",
        "v2":       "dag.agent_loop_v2",
        "openclaw": "dag.agent_loop_openclaw",
    }
    cap_name = cap_name_map.get(version, "dag.agent_loop_v2")

    async def _gen():
        if not goal:
            yield _sse({"type": "error", "error": "goal is required"})
            yield b"data: [DONE]\n\n"
            return

        if cap_name not in CAPABILITY_REGISTRY:
            yield _sse({
                "type": "error",
                "error": f"{cap_name} not registered — ensure context.py is loaded",
            })
            yield b"data: [DONE]\n\n"
            return

        yield _sse({
            "type":             "start",
            "goal":             goal,
            "version":          version,
            "max_cycles":       max_cycles,
            "session_id":       session_id,
            "require_approval": require_approval,
            "agent_name":       agent_name,
        })

        r = _redis()
        if not r:
            # Fallback: no Redis → just await and emit done
            try:
                cap = CAPABILITY_REGISTRY[cap_name]
                kwargs = dict(
                    goal=goal, allowed_caps=allowed_caps,
                    max_cycles=max_cycles, model=model,
                    instance_id=instance_id, prefer_gpu=prefer_gpu,
                    attach_skills=attach_skills,
                    attach_ontologies=attach_ontologies,
                    session_id=session_id,
                )
                if version == "v1":
                    kwargs["await_long_running"] = await_long_running
                    kwargs["long_running_timeout_secs"] = long_running_timeout_secs
                    kwargs["max_recovery_attempts"] = max_recovery_attempts
                    kwargs["system_prompt_template"] = system_prompt_template
                elif version == "v2":
                    kwargs["satisfaction_check"] = satisfaction_check
                    kwargs["enable_expand"]      = enable_expand
                    kwargs["triage_top_k"]       = triage_top_k
                    kwargs["max_search_calls"]   = max_search_calls
                    kwargs["max_expands"]        = max_expands
                    kwargs["count_failed_cycles"] = count_failed_cycles
                    kwargs["await_long_running"] = await_long_running
                    kwargs["long_running_timeout_secs"] = long_running_timeout_secs
                    kwargs["max_recovery_attempts"] = max_recovery_attempts
                    kwargs["system_prompt_template"] = system_prompt_template
                elif version == "openclaw":
                    kwargs["satisfaction_check"] = satisfaction_check
                    kwargs["enable_expand"]      = enable_expand
                    kwargs["require_approval"]   = require_approval
                    kwargs["hitl_timeout_secs"]  = hitl_timeout_secs
                    kwargs["triage_top_k"]       = triage_top_k
                    kwargs["await_long_running"] = await_long_running
                    kwargs["long_running_timeout_secs"] = long_running_timeout_secs
                    kwargs["handover"]           = handover
                    kwargs["handover_max_chars"] = handover_max_chars
                    kwargs["max_search_calls"]   = max_search_calls
                    kwargs["max_expands"]        = max_expands
                    kwargs["count_failed_cycles"] = count_failed_cycles
                    kwargs["max_recovery_attempts"] = max_recovery_attempts
                    kwargs["system_prompt_template"] = system_prompt_template
                result = await cap["func"](**kwargs)
                # Run handover post-hoc for v1/v2 if requested (they don't
                # accept a handover param themselves).
                if handover and version in ("v1", "v2"):
                    try:
                        ho = await _run_handover_stage(
                            goal=goal,
                            history=(result or {}).get("history") or [],
                            triage=(result or {}).get("triage") or {},
                            cur_final=((result or {}).get("summary")
                                       or (result or {}).get("final") or ""),
                            model=model, instance_id=instance_id,
                            prefer_gpu=prefer_gpu,
                            max_chars=handover_max_chars,
                            session_id=session_id,
                        )
                        if isinstance(result, dict) and ho:
                            result["handover_output"] = ho
                            result["final"]   = ho
                            result["summary"] = ho
                    except Exception as e:
                        log.debug("handover (v1/v2) failed: %s", e)
                yield _sse({"type": "result", **(result or {})})
            except Exception as e:
                yield _sse({"type": "error", "error": str(e)})
            yield b"data: [DONE]\n\n"
            return

        # Live mode: pubsub bridge with progress forwarding
        pubsub = r.pubsub()
        await pubsub.subscribe("vera:events:live")

        ALWAYS_FORWARD = {
            # v1
            "agent_loop.cycle_planning",
            "agent_loop.tool_call",
            "agent_loop.tool_done",
            "agent_loop.done",
            # v2
            "agent_loop_v2.triage_start",
            "agent_loop_v2.triage_done",
            "agent_loop_v2.toolkit",
            "agent_loop_v2.cycle_planning",
            "agent_loop_v2.tool_call",
            "agent_loop_v2.tool_done",
            "agent_loop_v2.done",
            # think (any variant — thinking-model token blocks)
            "agent_loop.think",
            "agent_loop_v2.think",
            "agent_loop_openclaw.think",
            # openclaw
            "agent_loop_openclaw.triage_start",
            "agent_loop_openclaw.triage_done",
            "agent_loop_openclaw.toolkit",
            "agent_loop_openclaw.cycle_planning",
            "agent_loop_openclaw.tool_call",
            "agent_loop_openclaw.tool_done",
            "agent_loop_openclaw.done",
            "agent_loop_openclaw.hitl_request",
            "agent_loop_openclaw.hitl_resolved",
            "agent_loop_openclaw.repetition_block",
            "agent_loop_openclaw.args_coerced",
            # long-running awaiting (emitted from any variant via _await_job_via_status)
            "agent_loop.long_running_await_start",
            "agent_loop.long_running_await_tick",
            "agent_loop.long_running_await_done",
            "agent_loop.long_running_await_timeout",
            "agent_loop.long_running_await_skipped",
            "agent_loop.research_stream_hint",
            "agent_loop.research_stream_open",
            "agent_loop.research_stream_done",
            "agent_loop.research_stream_failed",
            "agent_loop.research_step",
            "agent_loop.research_citations",
            "agent_loop.research_file",
            "agent_loop.research_thinking",
            "agent_loop.error_recovery_start",
            "agent_loop.error_recovery_attempt",
            "agent_loop.error_recovery_done",
            # handover synthesis stage
            "agent_loop.handover_start",
            "agent_loop.handover_done",
            "agent_loop.handover_error",
            # workshop tool invocation enrichment (covers v1/v2 too)
            "workshop.tool_invoked",
            "workshop.tool_finished",
            # generic streaming
            "stream.token", "stream.complete",
            # long-running progress
            "research.submitted", "research.job_started",
            "research.job_progress", "research.completed", "research.error",
            "exec.stdout", "exec.stderr", "exec.line",
            "exec.complete", "exec.error",
            "ml_training.epoch", "ml_training.metric", "ml_training.complete",
            "ml.train_epoch", "ml.train_complete",
            # planning (from /dag/plan_stream — when bridged)
            "dag.planning", "dag.step_planning", "dag.plan_ready",
            "dag.step_start", "dag.step_done", "dag.step_error",
            "dag.complete", "dag.error",
        }

        PROGRESS_TYPES = {
            "stream.token", "stream.complete",
            "research.submitted", "research.job_started",
            "research.job_progress", "research.completed", "research.error",
            "exec.stdout", "exec.stderr", "exec.line",
            "exec.complete", "exec.error",
            "ml_training.epoch", "ml_training.metric", "ml_training.complete",
            "ml.train_epoch", "ml.train_complete",
            # await polling counts as "progress" for whatever tool is running
            "agent_loop.long_running_await_start",
            "agent_loop.long_running_await_tick",
            "agent_loop.long_running_await_done",
            "agent_loop.long_running_await_timeout",
            "agent_loop.long_running_await_skipped",
            "agent_loop.research_stream_hint",
            "agent_loop.research_stream_open",
            "agent_loop.research_stream_done",
            "agent_loop.research_stream_failed",
            "agent_loop.research_step",
            "agent_loop.research_citations",
            "agent_loop.research_file",
            "agent_loop.research_thinking",
            "agent_loop.error_recovery_start",
            "agent_loop.error_recovery_attempt",
            "agent_loop.error_recovery_done",
            "agent_loop.think",
            "agent_loop_v2.think",
            "agent_loop_openclaw.think",
        }

        # Map v1 event types to v2-style names so the UI can use a single renderer.
        # The original event still flows through if NOT remapped, so older listeners
        # work too. We add a remapped twin event with the v2-equivalent name.
        V1_TO_V2_ALIAS = {
            "agent_loop.cycle_planning": "agent_loop_v2.cycle_planning",
            "agent_loop.tool_call":      "agent_loop_v2.tool_call",
            "agent_loop.tool_done":      "agent_loop_v2.tool_done",
            "agent_loop.done":           "agent_loop_v2.done",
        }

        async def _runner():
            cap = CAPABILITY_REGISTRY[cap_name]
            try:
                kwargs = dict(
                    goal=goal, allowed_caps=allowed_caps,
                    max_cycles=max_cycles, model=model,
                    instance_id=instance_id, prefer_gpu=prefer_gpu,
                    attach_skills=attach_skills,
                    attach_ontologies=attach_ontologies,
                    session_id=session_id,
                )
                if version == "v1":
                    kwargs["await_long_running"] = await_long_running
                    kwargs["long_running_timeout_secs"] = long_running_timeout_secs
                    kwargs["max_recovery_attempts"] = max_recovery_attempts
                    kwargs["system_prompt_template"] = system_prompt_template
                elif version == "v2":
                    kwargs["satisfaction_check"] = satisfaction_check
                    kwargs["enable_expand"]      = enable_expand
                    kwargs["triage_top_k"]       = triage_top_k
                    kwargs["max_search_calls"]   = max_search_calls
                    kwargs["max_expands"]        = max_expands
                    kwargs["count_failed_cycles"] = count_failed_cycles
                    kwargs["await_long_running"] = await_long_running
                    kwargs["long_running_timeout_secs"] = long_running_timeout_secs
                    kwargs["max_recovery_attempts"] = max_recovery_attempts
                    kwargs["system_prompt_template"] = system_prompt_template
                elif version == "openclaw":
                    kwargs["satisfaction_check"] = satisfaction_check
                    kwargs["enable_expand"]      = enable_expand
                    kwargs["require_approval"]   = require_approval
                    kwargs["hitl_timeout_secs"]  = hitl_timeout_secs
                    kwargs["triage_top_k"]       = triage_top_k
                    kwargs["await_long_running"] = await_long_running
                    kwargs["long_running_timeout_secs"] = long_running_timeout_secs
                    kwargs["handover"]           = handover
                    kwargs["handover_max_chars"] = handover_max_chars
                    kwargs["max_search_calls"]   = max_search_calls
                    kwargs["max_expands"]        = max_expands
                    kwargs["count_failed_cycles"] = count_failed_cycles
                    kwargs["max_recovery_attempts"] = max_recovery_attempts
                    kwargs["system_prompt_template"] = system_prompt_template
                result = await cap["func"](**kwargs)
                # Post-hoc handover for v1/v2 (they don't have the param)
                if handover and version in ("v1", "v2") and isinstance(result, dict):
                    try:
                        ho = await _run_handover_stage(
                            goal=goal,
                            history=result.get("history") or [],
                            triage=result.get("triage") or {},
                            cur_final=(result.get("summary") or result.get("final") or ""),
                            model=model, instance_id=instance_id,
                            prefer_gpu=prefer_gpu,
                            max_chars=handover_max_chars,
                            session_id=session_id,
                        )
                        if ho:
                            result["handover_output"] = ho
                            result["final"]   = ho
                            result["summary"] = ho
                    except Exception as e:
                        log.debug("handover (v1/v2 runner) failed: %s", e)
                return result
            except Exception as e:
                log.exception("agent loop runner failed")
                return {"error": str(e)}

        runner = asyncio.create_task(_runner())

        # Track currently-running tool to tag progress events
        active_tool: Dict[str, Any] = {"name": "", "cycle": 0, "long": False}

        try:
            while True:
                if runner.done():
                    break
                try:
                    msg = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    msg = None
                if not msg or msg.get("type") != "message":
                    continue
                raw = msg.get("data")
                if isinstance(raw, bytes):
                    raw = raw.decode(errors="ignore")
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue

                ev_type = ev.get("type", "")

                # Filter out other sessions' agent-loop events
                if ev_type.startswith(("agent_loop.", "agent_loop_v2.",
                                         "agent_loop_openclaw.")):
                    if ev.get("session_id") and ev.get("session_id") != session_id:
                        continue
                    if ev_type.endswith(".tool_call"):
                        active_tool["name"]  = ev.get("tool", "")
                        active_tool["cycle"] = ev.get("cycle", 0)
                        active_tool["long"]  = bool(ev.get("long_running")) \
                            or _is_long_running_cap(active_tool["name"])
                    elif ev_type.endswith(".tool_done"):
                        active_tool["name"]  = ""
                        active_tool["cycle"] = 0
                        active_tool["long"]  = False

                if ev_type not in ALWAYS_FORWARD:
                    continue

                # Tag progress events with the active tool/cycle
                if active_tool["name"] and ev_type in PROGRESS_TYPES:
                    yield _sse({
                        "type":       "tool_progress",
                        "raw_type":   ev_type,
                        "tool":       active_tool["name"],
                        "cycle":      active_tool["cycle"],
                        "session_id": session_id,
                        "data":       ev,
                    })
                else:
                    yield _sse(ev)
                    # Emit a v2-aliased twin so the UI can use one renderer
                    if ev_type in V1_TO_V2_ALIAS:
                        twin = dict(ev)
                        twin["type"]     = V1_TO_V2_ALIAS[ev_type]
                        twin["_aliased"] = True
                        twin["_origin"]  = ev_type
                        yield _sse(twin)

            try:
                final = await runner
            except Exception as e:
                final = {"error": str(e)}
            yield _sse({"type": "result", **(final or {})})
        finally:
            try:
                await pubsub.unsubscribe("vera:events:live")
                await pubsub.close()
            except Exception:
                pass
            yield b"data: [DONE]\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream",
                              headers={"Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})


# ═════════════════════════════════════════════════════════════════════════════
# DAG RUN STREAM — per-node events + long-running awaiting
# ─────────────────────────────────────────────────────────────────────────────
# Body: {dag: [...tuples...], state: {...}, supervised: bool,
#        await_long_running: bool, long_running_timeout_secs: int,
#        session_id: str}
#
# Emits SSE events:
#   {type: "start", dag_size, session_id}
#   {type: "node_start", index, cap, out_key, args, long_running, will_await}
#   {type: "node_done",  index, cap, out_key, ok, elapsed_ms, preview}
#   {type: "node_error", index, cap, error}
#   {type: "long_running_await_start" | "...tick" | "...done", ...}
#   {type: "result", state, errors}
# ═════════════════════════════════════════════════════════════════════════════

@APP.post("/workshop/dag/run_stream")
async def workshop_dag_run_stream(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    dag                 = body.get("dag") or []
    init_state          = body.get("state") or {}
    await_lr            = bool(body.get("await_long_running", True))
    lr_timeout          = int(body.get("long_running_timeout_secs", 1800))
    session_id          = body.get("session_id", "") or str(uuid.uuid4())

    def _sse(payload):
        return f"data: {json.dumps(payload, default=str)}\n\n".encode()

    async def _gen():
        if not isinstance(dag, list) or not dag:
            yield _sse({"type": "error", "error": "dag must be a non-empty list"})
            yield b"data: [DONE]\n\n"
            return

        yield _sse({
            "type":       "start",
            "dag_size":   len(dag),
            "session_id": session_id,
            "await_long_running": await_lr,
        })

        state: Dict[str, Any] = dict(init_state) if isinstance(init_state, dict) else {}
        errors: List[Dict[str, Any]] = []

        for idx, node in enumerate(dag):
            # Skip parallel branches for now — emit a notice and recurse via run_graph
            if isinstance(node, list) and node and isinstance(node[0], list):
                yield _sse({
                    "type":  "node_start",
                    "index": idx,
                    "cap":   "(parallel)",
                    "branches": len(node),
                })
                # Use run_graph for the parallel execution (no streaming inside branches)
                try:
                    import importlib
                    orch = importlib.import_module("Vera.Orchestration.capability_orchestration")
                    branch_state = await orch.run_graph([node], dict(state))
                    if isinstance(branch_state, dict):
                        state.update(branch_state)
                    yield _sse({
                        "type":  "node_done",
                        "index": idx,
                        "cap":   "(parallel)",
                        "ok":    True,
                        "preview": f"merged {len(node)} branches",
                    })
                except Exception as e:
                    errors.append({"index": idx, "error": str(e)})
                    yield _sse({
                        "type":  "node_error",
                        "index": idx,
                        "cap":   "(parallel)",
                        "error": str(e),
                    })
                continue

            try:
                cap_name, out_key, *rest = node
            except Exception:
                errors.append({"index": idx, "error": "malformed node"})
                yield _sse({
                    "type":  "node_error",
                    "index": idx, "cap": "?",
                    "error": "malformed node — expected [cap, out_key, ...]",
                })
                continue

            cond      = rest[0] if len(rest) > 0 else None
            input_map = rest[1] if len(rest) > 1 else None

            # Conditional skip
            if cond:
                if callable(cond) and not cond(state):
                    yield _sse({"type": "node_skipped", "index": idx, "cap": cap_name,
                                 "reason": "condition False"})
                    continue
                if isinstance(cond, str) and cond.startswith("CONDITION:"):
                    if not state.get(cond.split(":", 1)[1]):
                        yield _sse({"type": "node_skipped", "index": idx, "cap": cap_name,
                                     "reason": f"state[{cond.split(':',1)[1]}] falsy"})
                        continue

            cap = CAPABILITY_REGISTRY.get(cap_name)
            if not cap:
                err = f"unknown_cap:{cap_name}"
                if out_key:
                    state[out_key] = {"error": err}
                errors.append({"index": idx, "cap": cap_name, "error": err})
                yield _sse({"type": "node_error", "index": idx,
                             "cap": cap_name, "error": err})
                continue

            # Resolve args via input_map (if dict of {param_name: state_key})
            # else fall back to all matching state keys
            accepted = set(cap.get("schema", {}).get("properties", {}).keys())
            if isinstance(input_map, dict) and input_map:
                args = {}
                for pname, src_key in input_map.items():
                    if pname not in accepted:
                        continue
                    if isinstance(src_key, str) and src_key in state:
                        args[pname] = state[src_key]
                    else:
                        # Treat as literal value
                        args[pname] = src_key
            else:
                args = {k: v for k, v in state.items() if k in accepted}

            long_running = _is_long_running_cap(cap_name)
            # Will-await is now optimistic: if the cap MIGHT return a job_id
            # (or is in the static map, or is tagged long-running by group),
            # we'll attempt to await. Final decision happens at runtime via
            # _detect_job_id on the actual immediate result.
            will_await   = bool(await_lr and (long_running or _should_await(cap_name)))

            yield _sse({
                "type":         "node_start",
                "index":        idx,
                "cap":          cap_name,
                "out_key":      out_key,
                "args":         args,
                "long_running": long_running,
                "will_await":   will_await,
            })

            t0 = time.monotonic()
            try:
                result = await cap["func"](**args, trace_id=session_id)
                # Promote in-result errors before awaiting
                if isinstance(result, dict) and result.get("error"):
                    pass  # treat as terminal failure, skip await
                # Universal await: any cap returning a job_id gets polled,
                # regardless of whether it's in the static map.
                elif await_lr and isinstance(result, dict):
                    job_id_detected = _detect_job_id(result)
                    if job_id_detected:
                        awaited = await _universal_await_job(
                            cap_name=cap_name, immediate=result,
                            session_id=session_id, trace_id=session_id,
                            cycle=idx,
                            max_wait_secs=float(lr_timeout),
                        )
                        if isinstance(awaited, dict):
                            result = awaited

                if out_key:
                    state[out_key] = result

                preview = _result_preview(result, max_len=600)
                ok = not (isinstance(result, dict)
                          and (result.get("error") or result.get("_await_error")))
                elapsed = round((time.monotonic() - t0) * 1000)

                yield _sse({
                    "type":       "node_done",
                    "index":      idx,
                    "cap":        cap_name,
                    "out_key":    out_key,
                    "ok":         ok,
                    "elapsed_ms": elapsed,
                    "preview":    preview,
                })
                if not ok:
                    errors.append({"index": idx, "cap": cap_name,
                                    "error": ((result or {}).get("error")
                                              or (result or {}).get("_await_error")
                                              or "?")})
            except Exception as e:
                err = str(e)
                if out_key:
                    state[out_key] = {"error": err}
                errors.append({"index": idx, "cap": cap_name, "error": err})
                yield _sse({"type": "node_error", "index": idx,
                             "cap": cap_name, "error": err})

        yield _sse({
            "type":   "result",
            "state":  state,
            "errors": errors,
            "ok":     not errors,
        })
        yield b"data: [DONE]\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream",
                              headers={"Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})


# ═════════════════════════════════════════════════════════════════════════════
# CAP IO SCHEMA — for Visual Builder dropdown enrichment
# ─────────────────────────────────────────────────────────────────────────────
# Returns rich per-param info so the inspector can render dropdowns:
#   { name, properties: { pname: {type, required, enum, default, description,
#                                  is_object, is_array, item_type, fields: [...] } },
#     output_keys: [...], output_shape: {...}  (best-effort) }
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "workshop.cap_io_schema",
    http_method="POST", http_path="/workshop/cap_io_schema",
    http_tags=["workshop"], memory="off", silent=True,
    description="Detailed schema for a cap's inputs (with enum/defaults/types) "
                "and output keys. Used by the Visual Builder inspector to "
                "render dropdowns and output-key wiring. "
                "Input: name (str!). Output: {name, properties, required, output_keys}.",
)
async def cap_workshop_cap_io_schema(name: str, trace_id=None):
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap:
        return {"error": f"Unknown cap: {name}"}
    schema = cap.get("schema", {}) or {}
    props  = schema.get("properties", {}) or {}
    req    = set(schema.get("required", []) or [])

    enriched: Dict[str, Any] = {}
    for pname, pspec in props.items():
        if pname == "trace_id":
            continue
        ptype = pspec.get("type", "string") if isinstance(pspec, dict) else "string"
        item  = pspec.get("items") if isinstance(pspec, dict) else None
        nested_props = pspec.get("properties") if isinstance(pspec, dict) else None
        nested_req   = set(pspec.get("required", []) or []) if isinstance(pspec, dict) else set()
        fields: List[Dict[str, Any]] = []
        if isinstance(nested_props, dict):
            for nname, nspec in list(nested_props.items())[:24]:
                if isinstance(nspec, dict):
                    fields.append({
                        "name":     nname,
                        "type":     nspec.get("type", "any"),
                        "required": nname in nested_req,
                        "enum":     nspec.get("enum"),
                        "default":  nspec.get("default"),
                        "description": nspec.get("description", ""),
                    })

        enriched[pname] = {
            "type":        ptype,
            "required":    pname in req,
            "enum":        pspec.get("enum") if isinstance(pspec, dict) else None,
            "default":     pspec.get("default") if isinstance(pspec, dict) else None,
            "description": pspec.get("description", "") if isinstance(pspec, dict) else "",
            "is_object":   ptype == "object",
            "is_array":    ptype == "array",
            "item_type":   (item.get("type") if isinstance(item, dict) else None),
            "fields":      fields,
            "long_running": _is_long_running_cap(name),
        }

    # Best-effort output keys: parse the description for "Output: {...}" pattern
    out_keys: List[str] = []
    desc = cap.get("description", "") or ""
    try:
        import re as _re
        m = _re.search(r"Output[s]?\s*:\s*\{([^}]+)\}", desc)
        if m:
            for tok in m.group(1).split(","):
                k = tok.strip().split(":", 1)[0].strip().strip("'\"")
                if k and k.replace("_", "").isalnum():
                    out_keys.append(k)
    except Exception:
        pass

    return {
        "name":         name,
        "properties":   enriched,
        "required":     sorted(req),
        "output_keys":  out_keys,
        "description":  desc,
        "long_running": _is_long_running_cap(name),
        "group":        name.split(".")[0] if "." in name else "",
    }


# ═════════════════════════════════════════════════════════════════════════════
# V1/V2 EVENT ENRICHMENT — wrap _agent_loop_call_tool to emit richer events
# ─────────────────────────────────────────────────────────────────────────────
# Stock context.py emits agent_loop.tool_call / agent_loop_v2.tool_call with
# only {tool, cycle, session_id} — no args, no preview. The cycle UI is
# starved of detail. We wrap _agent_loop_call_tool so that EVERY invocation
# (regardless of which loop variant calls it) emits a workshop.tool_invoked
# event with the full args, and a workshop.tool_finished event with the
# preview / error / empty_search / coercion-notes.
#
# This also gives us a single hook to apply long-running awaiting to v1/v2
# loops, not just openclaw.
# ═════════════════════════════════════════════════════════════════════════════

_TOOL_WRAPPER_INSTALLED = False
_TOOL_INVOCATION_SEQ: int = 0


async def _workshop_call_tool_enriched(cap_name: str, args: Any, *,
                                         session_id: str = "",
                                         trace_id: str = "",
                                         _orig_call=None):
    """Replacement for context._agent_loop_call_tool that emits enriched
    events around each call. Falls through to the original implementation
    after enrichment."""
    global _TOOL_INVOCATION_SEQ
    _TOOL_INVOCATION_SEQ += 1
    seq = _TOOL_INVOCATION_SEQ

    # Coerce args deterministically before invoke
    if isinstance(args, dict):
        coerced, coerce_notes = _coerce_args(cap_name, args)
    else:
        coerced, coerce_notes = (args, [])

    long_running = _is_long_running_cap(cap_name)
    will_await   = _should_await(cap_name)

    await emit_event({
        "type":         "workshop.tool_invoked",
        "seq":          seq,
        "tool":         cap_name,
        "args":         coerced,
        "raw_args":     args,
        "coerce_notes": coerce_notes,
        "long_running": long_running,
        "will_await":   will_await,
        "session_id":   session_id,
    })
    if coerce_notes:
        await emit_event({
            "type":     "agent_loop_openclaw.args_coerced",
            "tool":     cap_name,
            "notes":    coerce_notes,
            "session_id": session_id,
            "cycle":    seq,  # so UI can attach to a generic "seq" cycle
        })

    t0 = time.monotonic()
    if _orig_call is None:
        # Fallback: call directly
        cap = CAPABILITY_REGISTRY.get(cap_name)
        if not cap:
            invoke = {"ok": False, "error": f"Unknown capability: {cap_name}"}
        else:
            accepted = set(cap.get("schema", {}).get("properties", {}).keys()) | {"trace_id"}
            kwargs = {k: v for k, v in (coerced or {}).items() if k in accepted}
            if session_id and "session_id" in accepted:
                kwargs.setdefault("session_id", session_id)
            try:
                result = await cap["func"](**kwargs, trace_id=trace_id)
                invoke = {"ok": True, "result": result}
            except Exception as e:
                invoke = {"ok": False, "error": str(e)}
    else:
        try:
            invoke = await _orig_call(cap_name, coerced,
                                        session_id=session_id,
                                        trace_id=trace_id)
        except Exception as e:
            invoke = {"ok": False, "error": str(e)}

    # Promote in-result errors to ok=False
    if invoke.get("ok") and isinstance(invoke.get("result"), dict):
        rerr = invoke["result"].get("error")
        if rerr:
            invoke["ok"] = False
            invoke["error"] = str(rerr)

    # Long-running awaiting — universal: any cap returning a job_id gets
    # polled, not just the ones in LONG_RUNNING_AWAIT_MAP. v1/v2 loops get
    # this for free since they go through this wrapper.
    if invoke.get("ok") and isinstance(invoke.get("result"), dict):
        immediate = invoke["result"]
        job_id_detected = _detect_job_id(immediate)
        if job_id_detected:
            try:
                awaited = await _universal_await_job(
                    cap_name=cap_name, immediate=immediate,
                    session_id=session_id, trace_id=trace_id,
                    cycle=seq,
                )
                invoke["result"] = awaited
                if isinstance(awaited, dict) and awaited.get("_await_error"):
                    invoke["ok"] = False
                    invoke["error"] = awaited["_await_error"]
                elif isinstance(awaited, dict) and awaited.get("error"):
                    invoke["ok"] = False
                    invoke["error"] = str(awaited["error"])
            except Exception as e:
                log.debug("await wrapper failed for %s: %s", cap_name, e)
        elif will_await:
            # Tagged long-running but no job_id — surface it so the agent
            # knows the call probably had bad args.
            await emit_event({
                "type":         "agent_loop.long_running_await_skipped",
                "tool":         cap_name,
                "reason":       "no_job_id",
                "result_keys":  list(immediate.keys())[:12],
                "session_id":   session_id, "cycle": seq,
            })

    elapsed_ms = round((time.monotonic() - t0) * 1000)

    # Empty-search detection
    empty_search = False
    if invoke.get("ok") and isinstance(invoke.get("result"), dict):
        if cap_name in ("caps.search", "context.search_caps", "context.search_dags"):
            rd = invoke["result"]
            n = (rd.get("count")
                 or len(rd.get("results") or [])
                 or len(rd.get("hits") or [])
                 or len(rd.get("caps") or []))
            empty_search = (n == 0)

    if invoke.get("ok"):
        preview = _result_preview(invoke["result"])
    else:
        preview = "ERROR: " + invoke.get("error", "unknown error")

    await emit_event({
        "type":         "workshop.tool_finished",
        "seq":          seq,
        "tool":         cap_name,
        "ok":           bool(invoke.get("ok")),
        "preview":      preview[:2000],
        "error":        invoke.get("error", "") if not invoke.get("ok") else "",
        "empty_search": empty_search,
        "elapsed_ms":   elapsed_ms,
        "session_id":   session_id,
    })

    return invoke


def _install_tool_wrapper() -> bool:
    """Patch context._agent_loop_call_tool with our enriched version."""
    global _TOOL_WRAPPER_INSTALLED
    if _TOOL_WRAPPER_INSTALLED:
        return True
    ctx = _ctx()
    if not ctx:
        return False
    orig = getattr(ctx, "_agent_loop_call_tool", None)
    if orig is None:
        return False
    if getattr(orig, "_workshop_wrapped", False):
        _TOOL_WRAPPER_INSTALLED = True
        return True

    async def wrapped(cap_name: str, args: Any, *,
                       session_id: str = "", trace_id: str = ""):
        return await _workshop_call_tool_enriched(
            cap_name, args,
            session_id=session_id, trace_id=trace_id,
            _orig_call=orig,
        )
    wrapped._workshop_wrapped = True  # type: ignore
    setattr(ctx, "_agent_loop_call_tool", wrapped)
    _TOOL_WRAPPER_INSTALLED = True
    log.info("workshop: installed enriched _agent_loop_call_tool wrapper")
    return True


@capability(
    "workshop.prompt_templates",
    http_method="GET", http_path="/workshop/prompt_templates",
    http_tags=["workshop", "agent_loop"],
    description="Return the default system prompts for v1/v2/openclaw plus the "
                "list of variables available for templating. UI uses this to "
                "populate the system-prompt editor.",
)
async def cap_workshop_prompt_templates(trace_id=None):
    EQ = "============================================================"
    openclaw_default = (
        "You are a Vera autonomous agent operating in OPENCLAW mode.\n\n"
        "GOAL: {goal}\n\n"
        + EQ + "\n"
        "YOUR TOOLKIT - these tools were CURATED for this specific goal\n"
        "by a triage step. Start here. Read the schemas. Call them.\n"
        + EQ + "\n"
        "{toolkit}\n\n"
        "ON EACH TURN, RESPOND WITH EXACTLY ONE JSON OBJECT. No prose, no fences:\n"
        '  {{\"thought\":\"<reasoning>\",\"tool_use\":{{\"name\":\"<cap.name>\",\"input\":{{...}}}}}}\n'
        '  {{\"thought\":\"<reasoning>\",\"final\":\"<answer addressing the GOAL above>\"}}\n\n'
        "RULES:\n"
        "1. PICK A TOOL FROM THE TOOLKIT ABOVE on the FIRST turn.\n"
        "2. The GOAL is the user request. [tool_result] messages are YOUR previous outputs.\n"
        "3. Inspect the schema. Required parameters are marked [REQUIRED].\n"
        "4. NEVER repeat the same (tool, args) pair.\n"
        "5. If a tool fails with bad-args, the runner will auto-recover.\n"
        "6. caps.search / context.search_caps / expand_tools are LAST RESORT.\n"
        "7. End with final as soon as the goal is satisfied.\n"
        "{ctx_extra}"
    )

    v1_default = (
        "You are a Vera autonomous agent. You work by calling TOOLS one at a time.\n\n"
        "GOAL: {goal}\n\n"
        + EQ + "\n"
        "YOUR TOOLKIT - already filtered for this goal. Use these tools:\n"
        + EQ + "\n"
        "{toolkit}\n\n"
        "ON EACH TURN, RESPOND WITH EXACTLY ONE JSON OBJECT (no prose, no fences):\n"
        '  {{\"thought\": \"brief reasoning\", \"tool\": \"<cap.name>\", \"args\": {{ ... }}}}\n'
        '  {{\"action\": \"done\", \"summary\": \"what was accomplished\"}}\n\n'
        "RULES:\n"
        "1. PICK A TOOL FROM THE TOOLKIT ABOVE on the FIRST turn.\n"
        "2. Only use tools from the list above. Inventing names will fail.\n"
        "3. Inspect tool signatures.\n"
        "4. Do not repeat the same tool with identical args > 2 times.\n"
        "5. When the goal is achieved, emit done.\n"
        "{ctx_extra}"
    )

    v2_default = (
        "You are a Vera autonomous agent. You work by calling TOOLS one at a time.\n\n"
        "GOAL: {goal}\n\n"
        "TRIAGE: category={category}, keywords={keywords}\n\n"
        + EQ + "\n"
        "YOUR TOOLKIT - CURATED FOR THIS GOAL. Use these tools first.\n"
        + EQ + "\n"
        "{toolkit}\n\n"
        "ON EACH TURN, RESPOND WITH EXACTLY ONE JSON OBJECT (no prose, no fences):\n"
        '  {{\"thought\": \"brief reasoning\", \"tool\": \"<cap.name>\", \"args\": {{ ... }}}}\n'
        '  {{\"action\": \"done\", \"summary\": \"what was accomplished\"}}\n\n'
        "RULES:\n"
        "1. PICK A TOOL FROM THE TOOLKIT on the FIRST turn.\n"
        "2. Only use tools currently in your toolkit.\n"
        "3. Inspect tool signatures.\n"
        "4. When the goal is achieved, emit done.\n"
        "5. Tool failures with bad-args trigger automatic recovery.\n"
        "6. expand_tools / caps.search are LAST RESORT.\n"
        "{ctx_extra}"
    )

    return {
        "variables": PROMPT_TEMPLATE_VARIABLES_HELP,
        "templates": {
            "v1":       v1_default,
            "v2":       v2_default,
            "openclaw": openclaw_default,
        },
    }


@APP.on_event("startup")
async def _workshop_install_hooks():
    # Try to install on startup; if context.py wasn't loaded yet, retry once
    if not _install_tool_wrapper():
        await asyncio.sleep(2.0)
        _install_tool_wrapper()




# ═════════════════════════════════════════════════════════════════════════════
# Panel HTML server + registration
# ═════════════════════════════════════════════════════════════════════════════

@APP.get("/workshop/panel", include_in_schema=False)
async def _workshop_panel_html():
    p = _HERE / "dag_workshop_panel.html"
    if not p.exists():
        return HTMLResponse("<p style='color:red'>dag_workshop_panel.html not found</p>",
                              status_code=404)
    return HTMLResponse(p.read_text(encoding="utf-8"))


_PANEL_HTML_CACHE: Optional[str] = None


def _panel_html() -> str:
    global _PANEL_HTML_CACHE
    if _PANEL_HTML_CACHE is None:
        p = _HERE / "dag_workshop_panel.html"
        if p.exists():
            _PANEL_HTML_CACHE = p.read_text(encoding="utf-8")
        else:
            _PANEL_HTML_CACHE = "<p style='color:red'>dag_workshop_panel.html missing</p>"
    return _PANEL_HTML_CACHE


try:
    register_ui(
        "dag-workshop",
        "DAG Workshop",
        "",            # no bare emoji per style guide
        """
            <iframe src="/workshop/panel"
            style="flex:1;border:none;width:100%;height:100%"
            allow="clipboard-read; clipboard-write"></iframe>
        """,
        "",
        ui_caps   = [
            "dag.store_list", "dag.store_save", "dag.store_get",
            "dag.store_search", "dag.store_delete", "dag.store_run",
            "dag.register", "dag.unregister", "dag.list_registered",
            "dag.run", "dag.plan_stream",
            "context.search_caps", "context.search_dags",
            "workshop.dag_to_cap_preview", "workshop.tag_cloud",
            "workshop.cap_tree", "workshop.cap_signature_rich",
            "workshop.history_to_dag", "workshop.list_loop_variants",
            "workshop.cap_io_schema",
            "workshop.agent_loop.preset_save",
            "workshop.agent_loop.preset_list",
            "workshop.agent_loop.preset_delete",
            "workshop.discover.options",
            "workshop.triage.preview",
            "workshop.handover",
            "dag.agent_loop", "dag.agent_loop_v2", "dag.agent_loop_openclaw",
            "caps.describe", "caps.list",
        ],
        mode      = "tab",
        tab_order = 17,
    )
    log.info("dag-workshop UI panel registered")
except Exception as e:
    log.warning("Could not register dag-workshop panel: %s", e)