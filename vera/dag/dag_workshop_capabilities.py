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
      - v3: full message-history Anthropic-style observe/think/act
                  (dag.agent_loop_v3 — defined here)
      - v4: strict explore/think/act/verify cadence (dag.agent_loop_v4)
      - v5: orchestrator + ephemeral scoped specialist sub-agents
                  (dag.agent_loop_v5 — defined here)
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
  dag.agent_loop_v3         — Anthropic-style observe/think/act loop with
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

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, capability, emit_event, now_iso,
    register_ui,
)

log = logging.getLogger("vera.dag_workshop")

_HERE = Path(__file__).parent


def _redis():     return _orch.REDIS
def _ctx():       return sys.modules.get("vera_context") or sys.modules.get("context")
def _dag_store(): return sys.modules.get("dag_store")


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


# ─────────────────────────────────────────────────────────────────────────────
# PHASE CLASSIFICATION (think → explore → act → validate)
# ─────────────────────────────────────────────────────────────────────────────
# The phased agent loop gates "act" tools behind a minimum number of cheap,
# read-only "explore" calls so the agent gathers context before it commits to
# expensive or mutating actions. Classification is heuristic and deliberately
# conservative: anything that isn't clearly read-only counts as "act".

# Verb-ish name tokens (last dot-segment, or a prefix of it) that indicate a
# cheap, read-only information-gathering call.
_EXPLORE_NAME_HINTS = (
    "get", "list", "search", "query", "describe", "read", "inspect", "ping",
    "stat", "stats", "status", "datasets", "preview", "show", "find", "lookup",
    "fetch", "head", "count", "exists", "info", "summary", "recall", "view",
)
# Explicit caps that are read-only despite an ambiguous name.
_EXPLORE_CAP_HINTS = {
    "fabric.query", "fabric.datasets", "fabric.stats", "caps.search",
    "caps.describe", "context.search_caps", "context.search_dags",
    "context.recall_fabric", "system.ping",
}


def _cap_phase(name: str) -> str:
    """Return "explore" for cheap read-only caps, else "act".

    A long-running cap is always "act" (it is never cheap), even if its name
    looks read-only (e.g. research.deep). Otherwise we look at an explicit
    allow-list, the registry http_method (GET ⇒ read-only), and finally the
    last name segment against the explore verb hints.
    """
    if not name:
        return "act"
    if _is_long_running_cap(name):
        return "act"
    if name in _EXPLORE_CAP_HINTS:
        return "explore"
    cap = CAPABILITY_REGISTRY.get(name) or {}
    if str(cap.get("http_method", "")).upper() == "GET":
        return "explore"
    leaf = name.split(".")[-1].lower()
    for tok in _EXPLORE_NAME_HINTS:
        if leaf == tok or leaf.startswith(tok + "_") or leaf.startswith(tok):
            return "explore"
    return "act"


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
      3. Vera.vera.researcher_api.RESEARCHER_URL  (module attr)
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

    # The dynamic module loader registers each .py under its BARE filename in
    # sys.modules (researcher_api.py → sys.modules["researcher_api"]), NOT under
    # a dotted package path. importlib.import_module("Vera.vera.researcher_api")
    # therefore always raised ModuleNotFoundError (silently swallowed), which
    # skipped both the RESEARCHER_URL lookup AND the _VERA_MODE detection below —
    # so resolution fell through to localhost:8765 where nothing listens, giving
    # the "[Errno 111] Connection refused" the research WS stream reported.
    rc = sys.modules.get("researcher_api")

    if not url and rc is not None:
        try:
            url = (getattr(rc, "RESEARCHER_URL", "") or "").strip()
        except Exception:
            pass

    if not url:
        # In-process detection: if the researcher is running in Vera mode its
        # WS route is on the orchestrator app, served on the orchestrator port.
        in_vera = False
        try:
            in_vera = bool(getattr(rc, "_VERA_MODE", False)) if rc is not None else False
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


async def _emit_research_report(*, cap_name: str, job_id: str, result: Any,
                                  citations: Any = None, output_mode: str = "",
                                  session_id: str = "", cycle: int = 0) -> None:
    """Emit the full research report/result as a dedicated UI event, separate
    from the truncated tool_done preview, so the agent loop / chat panels can
    render it nicely (markdown + citations) instead of just a one-line
    summary."""
    if not cap_name.startswith("research."):
        return
    text = result if isinstance(result, str) else ""
    if not text.strip():
        return
    await emit_event({
        "type":        "agent_loop.research_report",
        "tool":        cap_name,
        "job_id":      job_id,
        "report":      text[:40000],
        "citations":   citations or [],
        "output_mode": output_mode,
        "session_id":  session_id,
        "cycle":       cycle,
    })


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
    ctx_mod = _ctx()
    stream_append = getattr(ctx_mod, "stream_append_token", None) if ctx_mod else None

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
                    await _emit_research_report(
                        cap_name=cap_name, job_id=job_id, result=res,
                        citations=final_result.get("citations"),
                        session_id=session_id, cycle=cycle,
                    )
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
    if isinstance(extracted, dict):
        await _emit_research_report(
            cap_name=cap_name, job_id=job_id, result=extracted.get("result"),
            output_mode=extracted.get("output_mode", ""),
            session_id=session_id, cycle=cycle,
        )
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

    This is the function v1/v2/v3/DAG runs all should call.
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
            await _emit_research_report(
                cap_name=cap_name, job_id=job_id, result=last_payload.get("result"),
                output_mode=last_payload.get("output_mode", ""),
                session_id=session_id, cycle=cycle,
            )

    if isinstance(last_payload, dict):
        return {**immediate, **last_payload}
    return immediate


# ═════════════════════════════════════════════════════════════════════════════
# REPETITION DETECTOR  (prevents the v3 "context.search_caps loop")
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
        "id":       "v3",
        "cap":      "dag.agent_loop_v3",
        "label":    "v3 — full message history",
        "description": "Anthropic-style observe/think/act loop. Maintains the "
                        "full message history, emits explicit tool_use blocks, "
                        "and respects HITL approval before each action when "
                        "configured.",
        "supports_satisfaction": True,
        "supports_expand":       True,
        "supports_progress":     True,
        "supports_hitl":         True,
    },
    {
        "id":       "v4",
        "cap":      "dag.agent_loop_v4",
        "label":    "v4 — strict explore/think/act/verify",
        "description": "Strict cadence loop built on v3. Selects which steps to "
                        "run (plan/explore/think/act/verify), produces a todo plan, "
                        "enforces verify-before-finish with a stricter completion "
                        "check, steers toward smart terminal tooling (grep/sed/awk "
                        "via exec.bash.run), and supports per-run long-running "
                        "overrides with an optional long-running HITL gate.",
        "supports_satisfaction": True,
        "supports_expand":       True,
        "supports_progress":     True,
        "supports_hitl":         True,
        "supports_steps":        True,
        "supports_plan":         True,
        "supports_verify":       True,
    },
    {
        "id":       "v5",
        "cap":      "dag.agent_loop_v5",
        "label":    "v5 — specialist sub-agents",
        "description": "An orchestrator decomposes the goal into a step plan in a "
                        "single call, then hands each step to an ephemeral scoped "
                        "specialist sub-agent. The orchestrator sees only cap "
                        "names+descriptions and the skill list; each step's agent "
                        "sees only full schemas for its caps, any dynamically-loaded "
                        "skills, and a curated slice of prior-step output. Fast start "
                        "and per-step scoped toolkits (never balloons).",
        "supports_satisfaction": False,
        "supports_expand":       False,
        "supports_progress":     True,
        "supports_hitl":         False,
        "supports_steps":        True,
        "supports_plan":         True,
        "supports_verify":       False,
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
#   • v3 cap-arg `handover=True`
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
# V3 — full-message-history loop with HITL-friendly tool_use blocks
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


def _v3_system_prompt(goal: str, toolkit_block: str, *,
                              extra: str = "", enable_expand: bool = True,
                              toolkit_names: list = None,
                              phased: bool = True,
                              min_explore_cycles: int = 2) -> str:
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
        "You are a Vera autonomous agent operating in V3 mode.\n\n"
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
        + (_v3_phase_block(min_explore_cycles) if phased else "")
        + (("\n" + extra) if extra else "")
    )


def _v3_phase_block(min_explore_cycles: int) -> str:
    return (
        "\nWORK IN PHASES — gather information before you commit:\n"
        "  • THINK — state, in your `thought`, what you already know and what you\n"
        "    must find out.\n"
        f"  • EXPLORE — make at least {min_explore_cycles} cheap, read-only calls\n"
        "    (get/list/search/query/describe/read) to build a picture FIRST.\n"
        "    Action tools and long-running tools are GATED until you have explored\n"
        "    enough; long-running tools additionally require human approval if\n"
        "    requested early.\n"
        "  • ACT — once you understand the situation, take the action(s) that\n"
        "    advance the GOAL.\n"
        "  • VALIDATE — after acting, run ONE read-only check to confirm it worked\n"
        "    before you emit final.\n"
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
                                     model="", instance_id="", prefer_gpu=True,
                                     stream_cb=None):
    """Thinking-model-aware ollama_generate wrapper for dag_workshop callers.

    Lazily resolves ollama_generate via the context module so we don't have
    a circular import. Disables json_mode for thinking models (they often
    return empty under format=json), and retries without json_mode if the
    response is empty. When `stream_cb` is given it is forwarded to
    ollama_generate so callers can stream tokens live (the full text is still
    returned). The empty-retry is always non-streaming.
    """
    import importlib
    try:
        ctx = importlib.import_module("Vera.vera.context")
        og = getattr(ctx, "ollama_generate", None)
    except Exception:
        og = None
    if og is None:
        # Fallback: try the orchestration module directly
        try:
            orch = importlib.import_module("Vera.vera.capability_orchestration")
            og = getattr(orch, "ollama_generate", None)
        except Exception:
            og = None
    if og is None:
        return ""

    use_json = bool(json_mode) and not _is_thinking_model(model)
    _gen_kwargs = dict(system=system, json_mode=use_json,
                       model=model or None, instance_id=instance_id or None,
                       prefer_gpu=bool(prefer_gpu))
    if stream_cb is not None:
        try:
            raw = await og(prompt, stream_cb=stream_cb, **_gen_kwargs)
        except TypeError:
            # Older ollama_generate without stream_cb support — degrade to blocking.
            raw = await og(prompt, **_gen_kwargs)
    else:
        raw = await og(prompt, **_gen_kwargs)
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


def _coerce_json_loads(text: str) -> Optional[Dict]:
    """json.loads restricted to objects, with light trailing-comma cleanup."""
    for candidate in (text, text.replace(",}", "}").replace(",]", "]")):
        try:
            out = json.loads(candidate)
        except Exception:
            continue
        if isinstance(out, dict):
            return out
    return None


def _iter_balanced_json_objects(s: str) -> List[str]:
    """Return every top-level balanced ``{...}`` substring, honouring string
    literals/escapes so braces inside quoted text don't confuse the scan.

    This is what lets us pull a real JSON action out of output that ALSO
    contains echoed prose / observation blocks (which themselves contain
    braces, e.g. ``args: {"text": ...}``) before the actual action object."""
    objs: List[str] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    objs.append(s[start:i + 1])
                    start = -1
    return objs


# Keys that mark an object as an actual agent action (vs an echoed observation
# or a chunk of result JSON the model parroted back).
_JSON_ACTION_KEYS = ("tool_use", "tool_call", "final", "todo_done",
                     "action", "tool", "capability", "name")


def _extract_json(raw: str) -> Optional[Dict]:
    # Strip thinking tokens first — qwen3/deepseek-r1 etc. wrap JSON in <think>
    s, _think = _strip_think(raw or "")
    s = s.strip()
    # Prefer an explicit ```json fenced block if one is present.
    if "```" in s:
        try:
            import re as _re
            m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, _re.DOTALL)
            if m:
                cand = _coerce_json_loads(m.group(1))
                if cand is not None:
                    return cand
        except Exception:
            pass
        if s.startswith("```"):
            s = s.split("```", 2)[-1].strip()
    # Fast path: the whole payload is already a single JSON object.
    whole = _coerce_json_loads(s)
    if whole is not None:
        return whole
    # Models frequently emit prose / echoed observations (which contain braces)
    # BEFORE the real JSON action. Scan every balanced object and prefer the
    # LAST one that carries an action key; otherwise the last that parses at all.
    best_action = None
    last_ok = None
    for obj_str in _iter_balanced_json_objects(s):
        parsed = _coerce_json_loads(obj_str)
        if parsed is None:
            continue
        last_ok = parsed
        if any(k in parsed for k in _JSON_ACTION_KEYS):
            best_action = parsed
    if best_action is not None:
        return best_action
    if last_ok is not None:
        return last_ok
    # Legacy last-ditch: first { to last } with trailing-comma cleanup.
    a = s.find("{")
    b = s.rfind("}")
    if 0 <= a < b:
        return _coerce_json_loads(s[a:b + 1])
    return None


def _salvage_action(raw: str) -> Optional[Dict[str, Any]]:
    """Regex salvage for when JSON parsing fails outright — e.g. the model
    truncated mid-object (ran out of output budget) or left an unescaped quote
    inside ``thought``. Pulls out a recognizable action so a near-miss doesn't
    burn a whole cycle. Returns a canonical-ish payload or None."""
    s, _ = _strip_think(raw or "")
    if not s.strip():
        return None
    import re as _re
    tm = _re.search(r'"thought"\s*:\s*"((?:[^"\\]|\\.){0,300})', s)
    thought = (tm.group(1) if tm else "")
    # final — take everything after the opening quote (it's usually last/longest)
    fm = _re.search(r'"final"\s*:\s*"((?:[^"\\]|\\.)+)', s)
    if fm and fm.group(1).strip():
        return {"thought": thought, "final": fm.group(1)}
    # todo_done
    dm = _re.search(r'"todo_done"\s*:\s*"?(\d+)', s)
    if dm:
        return {"thought": thought, "todo_done": int(dm.group(1))}
    # a tool name (tool_use.name, tool_call.name, or top-level name/tool)
    nm = _re.search(r'"(?:name|tool|capability)"\s*:\s*"([A-Za-z0-9_.\-]+)"', s)
    if nm:
        inp: Dict[str, Any] = {}
        im = _re.search(r'"(?:input|args|arguments|parameters)"\s*:\s*\{', s)
        if im:
            objs = _iter_balanced_json_objects(s[im.end() - 1:])
            if objs:
                cand = _coerce_json_loads(objs[0])
                if isinstance(cand, dict):
                    inp = cand
        return {"thought": thought, "tool_use": {"name": nm.group(1), "input": inp}}
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
    """Resolve a paused HITL decision in an active v3 run.

    Body:
      {session_id: str!, step: int!,
       decision: "approve"|"reject"|"edit"|"abort"   (per-step HITL), or
                 "continue"|"wrap"                    (budget-pause),
       args?: {...}, comment?: str, increment?: int}

    Note: budget-pause steps use a negative `step` id, so step is not
    range-checked here — resolution is gated on a matching pending future.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid  = body.get("session_id", "")
    step = int(body.get("step", -1))
    decision = body.get("decision", "")
    _valid = ("approve", "reject", "edit", "abort", "continue", "wrap")
    if not sid or decision not in _valid:
        return {"error": "session_id and a valid decision are required"}
    pending = _HITL_PENDING_LOOP.get(sid, {})
    fut = pending.get(step)
    if not fut or fut.done():
        return {"error": f"No pending step {step} for session {sid}"}
    result = {
        "decision": decision,
        "args":     body.get("args") or {},
        "comment":  body.get("comment", ""),
    }
    if "increment" in body:
        try:
            result["increment"] = int(body.get("increment"))
        except Exception:
            pass
    fut.set_result(result)
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
#      heavy-hitters for the task category — and v3's expand path
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
    "code_task":      ["ide.code.", "ide.inspect.", "ide.agent.", "ide.fs.",
                       "exec.bash", "exec.python", "exec.run", "research.code"],
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
    "code_task":      ["llm.generate", "exec.bash.run",
                       "ide.inspect.snapshot", "ide.inspect.list_snapshots",
                       "ide.inspect.diff_snapshot", "ide.inspect.review_file",
                       "ide.inspect.source_info"],
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


async def _search_relevant_datasets(
    goal: str,
    keywords: List[str],
    *,
    max_results: int = 6,
    model: str = "",
    instance_id: str = "",
    prefer_gpu: bool = True,
    allow_llm_fallback: bool = True,
) -> List[Dict[str, Any]]:
    """Search fabric datasets for ones relevant to the goal.

    Step 1 — keyword matching: score each dataset_id against triage keywords
    and words extracted from the goal.  Fast, no extra LLM call.

    Step 2 — LLM fallback: if keyword scoring yields nothing AND
    `allow_llm_fallback` is set, ask the LLM to pick relevant datasets from the
    full list. Callers gate this off for goals that clearly aren't data-shaped
    (e.g. a `whoami`) so it doesn't add a pointless LLM round-trip before the
    first cycle.

    Returns a list of dataset dicts: [{dataset_id, record_count, ...}]
    """
    import re as _re

    fabric_cap = CAPABILITY_REGISTRY.get("fabric.datasets")
    if not fabric_cap:
        return []
    try:
        ds_result = await fabric_cap["func"]()
    except Exception:
        return []
    datasets = (ds_result or {}).get("datasets", [])
    if not datasets:
        return []

    # Build broad search terms from triage keywords + meaningful goal words
    stop = {"the", "and", "for", "with", "that", "this", "from", "have",
            "not", "are", "but", "can", "you", "will", "get", "all", "any",
            "some", "one", "into", "then", "than", "its", "also", "using"}
    goal_words = {w.lower().strip(".,!?:;\"'()[]{}")
                  for w in goal.split()
                  if len(w) > 3 and w.lower() not in stop}
    search_terms = set(keywords) | goal_words

    def _score(ds: Dict[str, Any]) -> int:
        did = (ds.get("dataset_id") or "").lower()
        parts = set(_re.split(r"[._\-/]", did))
        # count exact-part matches and substring hits
        return (len(search_terms & parts) * 2
                + sum(1 for t in search_terms if t in did and t not in parts))

    scored = sorted(datasets, key=_score, reverse=True)
    top = [ds for ds in scored if _score(ds) > 0][:max_results]
    if top:
        return top

    if not allow_llm_fallback:
        return []

    # LLM fallback — ask model to pick from the list using goal-seeded queries
    ollama_generate = getattr(_ctx(), "ollama_generate", None)
    if not ollama_generate:
        return []
    ds_ids = [ds.get("dataset_id", "") for ds in datasets[:80]]
    ds_list = "\n".join(f"- {d}" for d in ds_ids if d)
    if not ds_list:
        return []
    try:
        try:
            raw = await ollama_generate(
                f"Goal: {goal.strip()}\n\nAvailable datasets:\n{ds_list}",
                system=(
                    "You select datasets from a list that are most relevant to the user's goal.\n"
                    "Respond ONLY with a JSON array of dataset_id strings (max 6, can be empty):\n"
                    '[\"dataset_id_1\", \"dataset_id_2\"]\n'
                    "Choose datasets whose names suggest they contain data useful for the goal. "
                    "If none are relevant, return []."
                ),
                model=model or None,
                instance_id=instance_id or None,
                prefer_gpu=bool(prefer_gpu),
                json_mode=True,
                temperature=0.0,
            )
        except TypeError:
            raw = await ollama_generate(
                f"Goal: {goal.strip()}\n\nAvailable datasets:\n{ds_list}",
                system=(
                    "You select datasets from a list that are most relevant to the user's goal.\n"
                    "Respond ONLY with a JSON array of dataset_id strings (max 6, can be empty):\n"
                    '[\"dataset_id_1\", \"dataset_id_2\"]\n'
                    "Choose datasets whose names suggest they contain data useful for the goal. "
                    "If none are relevant, return []."
                ),
                model=model or None,
                instance_id=instance_id or None,
                prefer_gpu=bool(prefer_gpu),
                json_mode=True,
            )
        parsed = _parse_json_object(raw or "")
        # LLM may return a plain list or a dict wrapping a list
        selected_ids: List[str] = []
        if isinstance(parsed, list):
            selected_ids = [str(x) for x in parsed if x]
        elif isinstance(parsed, dict):
            arr = parsed.get("datasets") or parsed.get("ids") or parsed.get("dataset_ids") or []
            selected_ids = [str(x) for x in arr if x]
        if selected_ids:
            ds_map = {d.get("dataset_id"): d for d in datasets}
            return [ds_map[did] for did in selected_ids if did in ds_map][:max_results]
    except Exception as e:
        log.debug("dataset search LLM fallback failed: %s", e)
    return []


_TRIAGE_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "not", "are",
    "but", "can", "you", "will", "get", "all", "any", "some", "one", "into",
    "then", "than", "its", "also", "using", "please", "make", "create", "give",
    "want", "need", "find", "show", "tell", "about", "what", "which", "how",
    "when", "where", "your", "our", "their", "them", "they", "and", "use",
}


def _goal_terms(goal: str, limit: int = 8) -> List[str]:
    """Meaningful lowercased words from a goal (minus stopwords) — a fallback
    source of keywords when triage produces none, so the toolkit's semantic cap
    search always has something to match against."""
    import re as _re
    out: List[str] = []
    for w in _re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", goal or ""):
        wl = w.lower()
        if wl in _TRIAGE_STOPWORDS or wl in out:
            continue
        out.append(wl)
        if len(out) >= limit:
            break
    return out


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
        # Thinking models often return an EMPTY string under format=json, which
        # is the #1 reason triage collapses to "other"/no-keywords on complex
        # goals. Retry once without json_mode before giving up on the LLM.
        if len((raw or "").strip()) < 4:
            raw = await ollama_generate(
                f"Goal: {goal.strip()}\n\nRespond with a single JSON object and nothing else.",
                system=sys,
                model=model or None,
                instance_id=instance_id or None,
                prefer_gpu=bool(prefer_gpu),
                json_mode=False,
            )
    except Exception as e:
        log.debug("workshop triage failed: %s", e)
        return {"category": "other", "keywords": _goal_terms(goal), "reasoning": ""}

    parsed = _parse_json_object(raw or "")
    if not parsed:
        # Even if LLM failed, never strand the toolkit with no signal: prefer the
        # heuristic, and ALWAYS fall back to goal-derived keywords so the toolkit
        # builder can still surface relevant caps via semantic search.
        cat0 = heuristic[0] if heuristic else "other"
        kws0 = list(dict.fromkeys((heuristic[1] if heuristic else []) + _goal_terms(goal)))[:6]
        out = {"category": cat0,
               "categories": [cat0] if cat0 != "other" else [],
               "keywords": kws0,
               "reasoning": (("(heuristic) " + heuristic[2]) if heuristic
                             else "(fallback) classifier unavailable — keywords derived from goal text")}
        _TRIAGE_CACHE[cache_key] = dict(out)
        return out

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

    # Never return an empty keyword set — a complex goal that the LLM left
    # keyword-less would otherwise get only a generic toolkit. Backfill from the
    # goal text so semantic cap discovery always has fuel.
    if not cleaned_kws:
        cleaned_kws = list(dict.fromkeys((heuristic[1] if heuristic else []) + _goal_terms(goal)))[:6]

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


async def _workshop_build_toolkit(*, allowed_caps: str, category: str,
                              categories: Optional[List[str]] = None,
                              keywords: List[str], top_k: int = 16,
                              extra_caps: Optional[List[str]] = None,
                              goal: str = "",
                              base_caps: Optional[List[str]] = None,
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

    # 0. BASE TOOLKIT — always present, highest priority (mirrors the agent's
    #    baseline domain_caps, or the default Web/browser + skills set). These
    #    are never truncated and are the floor when triage can't narrow things.
    base_list = [c.strip() for c in (base_caps or []) if c and c.strip()]
    for c in base_list:
        add(c)
    n_base = len(toolkit)  # everything up to here is protected from the size cap

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

    # 3. Prefix expansion for ALL categories — round-robin per prefix so a
    #    deep namespace (e.g. ide.inspect.*) is never alphabetically starved
    #    out of the budget by a shallower sibling (e.g. ide.code.*).
    for cat_norm in cats_list:
        buckets = [
            b for b in (_expand_prefixes([p], seen)
                        for p in CATEGORY_PREFIX_HINTS.get(cat_norm, []))
            if b
        ]
        budget = max(8, top_k) * 2
        idx, added = 0, 0
        while buckets and added < budget:
            progressed = False
            for b in buckets:
                if idx < len(b):
                    before = len(seen)
                    add(b[idx])
                    if len(seen) > before:
                        added += 1
                        progressed = True
            if not progressed:
                break
            idx += 1

    # 5. Keyword-driven semantic search via the cap index when available.
    # This is what makes toolkit selection dynamic rather than purely
    # category/prefix driven — without it, goals whose wording doesn't
    # match a category's hardcoded prefix list (e.g. "give me a guide on
    # X") never surface caps like research.guide.
    semantic_added = 0
    semantic_budget = max(4, top_k // 2)
    # When triage was weak (category "other" or sparse keywords), widen the net
    # so a misclassified complex goal still gets relevant caps.
    weak_triage = (len(keywords or []) < 2) or all(c == "other" for c in cats_list)
    if weak_triage:
        semantic_budget = max(semantic_budget, top_k)
    try:
        ds = _dag_store()
        cap_index = getattr(ds, "CAP_INDEX", None) if ds else None
        # Query from keywords AND the goal text, so relevant caps surface even
        # when triage produced no/poor keywords.
        query_parts = list(keywords or [])
        if goal:
            query_parts.append(goal)
        kw_query = " ".join(query_parts).strip()
        if cap_index is not None and kw_query:
            hits = await cap_index.relevance_search(kw_query, top_k=top_k * 2)
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

    # Hard size cap so a weak triage can never balloon the prompt toward "all
    # tools" (the cause of slow first responses). The base toolkit and discovery
    # caps (the first n_base + len(WORKSHOP_DISCOVERY_CAPS) entries) are always
    # protected; only the category/keyword-discovered tail is trimmed.
    max_total = max(int(top_k) * 2, n_base + len(WORKSHOP_DISCOVERY_CAPS) + 8)
    if len(toolkit) > max_total:
        toolkit = toolkit[:max_total]

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
                             prior_attempts: List[Dict[str, Any]] = None,
                             goal: str = "", thought: str = "") -> str:
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

    # Give the fixer the original intent so it can supply REAL values for
    # missing required fields (e.g. a search `query`) instead of inventing a
    # generic placeholder. Without this the fixer is blind and guesses.
    context_block = ""
    if goal and goal.strip():
        context_block += f"\nORIGINAL USER GOAL:\n{str(goal).strip()[:600]}\n"
    if thought and thought.strip():
        context_block += (f"\nWHY THIS TOOL WAS CALLED (agent's own reasoning "
                          f"for this step):\n{str(thought).strip()[:400]}\n")

    return (
        f"TOOL CALL FAILED -- recovery attempt {attempt}/{max_attempts}\n\n"
        f"Tool: {cap_name}\n"
        f"Schema:\n{schema_block}\n"
        f"{context_block}\n"
        f"Failed args: {failed_args_s}\n"
        f"Error: {str(error_text)[:400]}"
        f"{history_block}\n\n"
        "Fix the input args so the call succeeds. Derive missing required "
        "values (e.g. a search query) from the ORIGINAL USER GOAL and the "
        "agent's reasoning above -- do NOT invent generic placeholders. The "
        f"tool MUST stay as `{cap_name}` -- do not change it. Respond with "
        "EXACTLY:\n"
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
    "3b. When a required field is missing, infer its value from the ORIGINAL "
    "USER GOAL and the agent's reasoning provided in the message. NEVER fill "
    "a field with a generic placeholder like 'default search query' -- use the "
    "real intended value.\n"
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
                                  stream_id: str = "",
                                  goal: str = "",
                                  thought: str = "") -> Dict[str, Any]:
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
            goal=goal, thought=thought,
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

    The v3 protocol expects:
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
    # tool_call as an alias of tool_use (the v4 loop reads either key).
    tc = parsed.get("tool_call")
    if isinstance(tc, dict) and "name" in tc:
        return {"thought": thought,
                "tool_use": {"name": tc.get("name") or "",
                             "input": tc.get("input") or tc.get("arguments")
                                      or tc.get("args") or {}}}
    if "final" in parsed:
        return {"thought": thought, "final": str(parsed["final"])}
    # v4 control action: {"thought":"…","todo_done":<id>} marks a plan item
    # complete without a tool call this turn. Without this branch the
    # canonicaliser returned None and the turn was mis-reported as a parse error
    # even though the model's JSON was perfectly valid — burning cycles and
    # stalling the plan. (final takes priority above, matching the loop.)
    if parsed.get("todo_done") is not None:
        return {"thought": thought, "todo_done": parsed.get("todo_done")}
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

    # Thought-only payload: {"thought":"…"} with no action. This is the model
    # reasoning out loud — a valid (if incomplete) turn, NOT a parse error. Return
    # it so the loop can render/stream the thought and re-prompt for an action.
    if thought:
        return {"thought": thought}

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
        from Vera.vera.fabric.data_fabric import _sqlite_query
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
        from Vera.vera.fabric.data_fabric import ingest_dataset
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
        from Vera.vera.fabric.data_fabric import delete_record
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
        "— must include {variant: 'v1'|'v2'|'v3', max_cycles, allowed_caps, "
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
    base = config.get("variant", "v3")
    if base not in {"v1", "v2", "v3"}:
        return {"error": "variant must be v1, v2, or v3"}
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
    toolkit = await _workshop_build_toolkit(
        allowed_caps=allowed_caps,
        category=triage.get("category", "other"),
        categories=triage.get("categories"),
        keywords=triage.get("keywords", []),
        top_k=int(triage_top_k),
        goal=goal,
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
    "dag.agent_loop_v3",
    http_method="POST", http_path="/dag/agent_loop_v3",
    http_tags=["dag", "agents"],
    memory="on",
    streams=["dag.agent_loop_v3"],
    description=(
        "v3-style agent loop: maintains full message history and emits "
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
        "phased (bool default True — enforce think→explore→act→validate gating), "
        "min_explore_cycles (int default 2 — read-only calls required before acting/finishing), "
        "require_validate (bool default True — demand a validation step before final), "
        "long_running_force_hitl (bool default True — long-running caps need approval until explored), "
        "allow_continue (bool default True — pause for a continue decision at the budget limit), "
        "continue_increment (int default 8), auto_continue_max (int default 0 — auto-extend N times), "
        "model (str), instance_id (str), prefer_gpu (bool), session_id (str). "
        "Output: {goal, history, messages, cycles, done, final, toolkit, triage, stream_id}."
    ),
)
async def cap_dag_agent_loop_v3(
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
    inject_fabric_records: bool = False,  # opt-in: inject actual fabric records + DAGs (off by default)
    # ── Phase model (think → explore → act → validate) ──────────────────────
    phased:             bool = True,   # enforce explore-before-act gating
    min_explore_cycles: int  = 2,      # explore calls required before acting / finishing
    require_validate:   bool = True,   # demand a validation step before accepting final
    long_running_force_hitl: bool = True,  # long-running caps need HITL approval until explored
    # ── Continue / budget extension ─────────────────────────────────────────
    allow_continue:     bool = True,   # pause for a continue decision at the budget limit
    continue_increment: int  = 8,      # cycles added per continue
    auto_continue_max:  int  = 0,      # auto-extend up to N times before pausing (0 = manual only)
    trace_id=None,
):
    if not goal:
        return {"error": "goal required"}
    max_cycles = max(1, min(40, int(max_cycles)))
    min_explore_cycles = max(0, int(min_explore_cycles))
    continue_increment = max(1, min(40, int(continue_increment)))
    auto_continue_max  = max(0, int(auto_continue_max))
    triage_top_k = max(1, min(64, int(triage_top_k)))
    sid = session_id or str(uuid.uuid4())

    # Scope ollama.* events to this run so the UI can show which node served each
    # planner call (task-local contextvar — no leak across concurrent runs).
    try:
        _orch.OLLAMA_EVENT_SESSION.set(sid)
    except Exception:
        pass

    ctx = _ctx()
    ds  = _dag_store()
    ollama_generate = getattr(ctx, "ollama_generate", None) if ctx else None
    if ollama_generate is None:
        return {"error": "context module not loaded — ollama_generate missing"}

    # ── Stage 1: TRIAGE (workshop's improved, anchored, cached version) ─────
    await emit_event({
        "type": "agent_loop_v3.triage_start",
        "goal": goal[:200], "session_id": sid,
    })
    try:
        triage = await _workshop_triage_goal(
            goal, model=model, instance_id=instance_id, prefer_gpu=prefer_gpu,
        )
    except Exception as e:
        log.debug("v3 triage failed: %s", e)
        triage = {"category": "other", "keywords": [], "reasoning": ""}
    await emit_event({
        "type": "agent_loop_v3.triage_done",
        "triage": triage, "session_id": sid,
    })

    # ── Stage 2: SEED TOOLKIT (category-aware, prefix-expanded) ─────────────
    toolkit = await _workshop_build_toolkit(
        allowed_caps=allowed_caps,
        category=triage.get("category", "other"),
        categories=triage.get("categories"),
        keywords=triage.get("keywords", []),
        top_k=int(triage_top_k),
        goal=goal,
    )

    if not toolkit:
        return {"error": "No usable tools after triage", "triage": triage}

    # ── Stage 2b: RELEVANT DATASETS from fabric ───────────────────────────────
    # Run in parallel with context build below — datasets search is async and fast.
    relevant_datasets = await _search_relevant_datasets(
        goal,
        keywords=triage.get("keywords", []),
        model=model, instance_id=instance_id, prefer_gpu=bool(prefer_gpu),
    )
    # Ensure fabric.query and fabric.datasets are in toolkit if datasets found
    if relevant_datasets:
        for ds_cap in ("fabric.datasets", "fabric.query"):
            if ds_cap in CAPABILITY_REGISTRY and ds_cap not in toolkit:
                toolkit.insert(0, ds_cap)

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
            log.debug("v3 context build: %s", e)

    # Append relevant-dataset hints to ctx_extra so the LLM knows what data exists
    if relevant_datasets:
        ds_lines = "\n".join(
            f"  • {d.get('dataset_id', '?')} ({d.get('record_count', 0)} records)"
            for d in relevant_datasets
        )
        ds_hint = (
            "\n\nRELEVANT DATASETS IN FABRIC:\n"
            + ds_lines
            + "\nUse fabric.query(dataset_id=\"<name>\", text=\"<query>\") to search these."
        )
        ctx_extra = (ctx_extra.rstrip() + ds_hint) if ctx_extra else ds_hint.strip()

    # ── Stage 2c: OPTIONAL real fabric records + DAGs via the context system ───
    # Off by default — keeps the lightweight name-hint behaviour above. When the
    # user opts in, route through context.recall_fabric to inject actual records
    # (and matching stored DAGs) — deliberately excluding memory.
    if inject_fabric_records:
        recall_fabric = CAPABILITY_REGISTRY.get("context.recall_fabric", {}).get("func")
        if recall_fabric:
            try:
                fr = await recall_fabric(
                    query=goal,
                    keywords=", ".join(triage.get("keywords") or []),
                    dataset_ids=[d.get("dataset_id") for d in relevant_datasets
                                 if d.get("dataset_id")],
                    top_k=12,
                    include_dags=True,
                )
            except Exception as e:
                log.debug("v3 recall_fabric inject: %s", e)
                fr = {}
            rec_lines = []
            for r in (fr or {}).get("records", [])[:12]:
                summ = (r.get("summary") or r.get("text") or "")[:240].replace("\n", " ")
                rec_lines.append(f"  • [{r.get('dataset_id','?')}] {summ}")
            dag_lines = []
            for d in (fr or {}).get("dags", [])[:6]:
                dag_lines.append(f"  • {d.get('name','?')} — {(d.get('desc') or '')[:100]}")
            blocks = []
            if rec_lines:
                blocks.append("\n\nRELEVANT FABRIC RECORDS:\n" + "\n".join(rec_lines))
            if dag_lines:
                blocks.append("\n\nRELATED DAG WORKFLOWS:\n" + "\n".join(dag_lines))
            if blocks:
                inj = "".join(blocks)
                ctx_extra = (ctx_extra.rstrip() + inj) if ctx_extra else inj.strip()

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
        system_prompt = _v3_system_prompt(
            goal, _toolkit_block(toolkit),
            extra=ctx_extra, enable_expand=enable_expand,
            toolkit_names=list(toolkit),
            phased=phased, min_explore_cycles=min_explore_cycles,
        )

    # ── Stream registration ─────────────────────────────────────────────────
    stream_register = getattr(ctx, "stream_register", None)
    stream_complete = getattr(ctx, "stream_complete", None)
    stream_append   = getattr(ctx, "stream_append_token", None)
    stream_id = ""
    if stream_register:
        try:
            stream_id = await stream_register(
                kind          = "agent_loop_v3",
                source_cap    = "dag.agent_loop_v3",
                session_id    = sid,
                label         = goal[:80],
                persist_full  = True,
                fabric_dataset = "streams.agent_loop_v3",
                metadata      = {"goal": goal, "max_cycles": max_cycles,
                                  "triage": triage, "initial_toolkit": list(toolkit),
                                  "require_approval": require_approval},
            )
        except Exception:
            stream_id = ""

    await emit_event({
        "type": "agent_loop_v3.toolkit",
        "stream_id": stream_id, "toolkit": list(toolkit),
        "session_id": sid,
        "relevant_datasets": [d.get("dataset_id") for d in relevant_datasets] if relevant_datasets else [],
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

    # ── Phase-model state (think → explore → act → validate) ────────────────
    phase            = "think"   # current high-level phase, surfaced via events
    explore_done     = 0         # successful explore (read-only) tool calls so far
    validated        = False     # a validation/re-check explore call ran after acting
    acted            = False     # at least one act-cap call succeeded
    validation_requested = False # we already nudged the agent to validate once
    auto_continues   = 0         # times the budget was auto-extended

    async def _emit_phase(new_phase: str, **extra):
        nonlocal phase
        phase = new_phase
        await emit_event({
            "type": "agent_loop_v3.phase",
            "stream_id": stream_id, "cycle": cycles, "phase": new_phase,
            "explore_done": explore_done, "min_explore": min_explore_cycles,
            "session_id": sid, **extra,
        })

    def _explore_satisfied() -> bool:
        return (not phased) or explore_done >= min_explore_cycles

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

    async def _budget_reached() -> bool:
        """Productive/iteration budget reached for the current max_cycles."""
        if count_failed_cycles:
            return cycle_i >= max_cycles
        return productive_cycles >= max_cycles

    async def _maybe_continue() -> bool:
        """At the budget limit, decide whether to extend (return True) or stop
        (return False). Auto-extends up to auto_continue_max, otherwise pauses
        for a human continue/wrap decision via the HITL channel."""
        nonlocal max_cycles, auto_continues
        if not allow_continue:
            return False
        await emit_event({
            "type": "agent_loop_v3.budget_pause",
            "stream_id": stream_id, "cycle": cycles,
            "cycles": cycles, "max_cycles": max_cycles,
            "increment": continue_increment, "step": -(cycles + 1),
            "auto_continues": auto_continues, "auto_continue_max": auto_continue_max,
            "session_id": sid,
        })
        # Auto-extend without prompting while we still have auto-continues left.
        if auto_continue_max > 0 and auto_continues < auto_continue_max:
            auto_continues += 1
            max_cycles += continue_increment
            await emit_event({
                "type": "agent_loop_v3.budget_continue",
                "stream_id": stream_id, "cycle": cycles, "mode": "auto",
                "max_cycles": max_cycles, "auto_continues": auto_continues,
                "session_id": sid,
            })
            return True
        # Otherwise wait for a human decision on the HITL channel. Use a
        # negative step id so it never collides with a real per-step HITL.
        decision_obj = await _await_hitl_decision(
            sid, -(cycles + 1), timeout=float(max(hitl_timeout_secs, 600)),
        )
        decision = (decision_obj or {}).get("decision", "")
        if decision == "continue":
            inc = int((decision_obj or {}).get("increment") or continue_increment)
            max_cycles += max(1, inc)
            await emit_event({
                "type": "agent_loop_v3.budget_continue",
                "stream_id": stream_id, "cycle": cycles, "mode": "manual",
                "max_cycles": max_cycles, "session_id": sid,
            })
            return True
        # wrap / timeout / abort → stop and let the wrap-up path run.
        return False

    try:
        cycle_i = 0
        while True:
            # Productive-only counting: errored or blocked steps don't consume the
            # cycle budget when count_failed_cycles is False.
            if await _budget_reached():
                if await _maybe_continue():
                    pass  # budget extended — keep going
                else:
                    break
            if not count_failed_cycles:
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

            # Keep the observation block COMPACT: a large result dump (e.g. a
            # multi-KB fabric.query) bloats the prompt and tempts weaker models
            # to echo it back verbatim instead of acting. Only the most recent
            # result needs detail; older ones get a short tail.
            _recent = history[-5:]
            obs_block = "\n\n".join(
                f"[Observation {i+1}] tool={h['tool']} ok={h.get('ok')}\n"
                f"args: {json.dumps(h.get('args', {}), default=str)[:160]}\n"
                f"result: {(h.get('preview') or '')[:(700 if i == len(_recent) - 1 else 250)]}"
                for i, h in enumerate(_recent)
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
            phase_hint = ""
            if phased:
                if not _explore_satisfied():
                    phase_hint = (
                        f"\n\nPHASE: EXPLORE ({explore_done}/{min_explore_cycles} done). "
                        "Use cheap, read-only tools (get/list/search/query/describe) to "
                        "build a picture of the situation. Acting tools and long-running "
                        "tools are gated until you have explored enough."
                    )
                elif acted and require_validate and not validated:
                    phase_hint = (
                        "\n\nPHASE: VALIDATE. You have acted — before you finish, verify "
                        "the result with a read-only check (re-query / re-read / confirm), "
                        "or state explicitly why no validation is possible."
                    )
                else:
                    phase_hint = "\n\nPHASE: ACT. You have explored enough — take the action that advances the GOAL."
            user_msg = (
                f"REMINDER: GOAL = \"{goal}\"\n\n"
                "These are your past tool results (NOT a new user message):\n\n"
                + obs_block
                + quota_hint
                + phase_hint
                + "\n\nEmit the next JSON action toward the GOAL."
            )

            await emit_event({
                "type": "agent_loop_v3.cycle_planning",
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
                    "type": "agent_loop_v3.tool_done",
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
                    "type": "agent_loop_v3.think",
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
                    "type": "agent_loop_v3.tool_done",
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
                # Validation gate: if the agent acted but never validated, push
                # back once and ask it to verify before we accept the final.
                if (phased and require_validate and acted and not validated
                        and not validation_requested):
                    validation_requested = True
                    await _emit_phase("validate", reason="validation_required")
                    history.append({
                        "tool": "(validation_required)", "args": {}, "ok": False,
                        "preview": ("Before finishing: validate your result with a "
                                    "read-only check (re-query / re-read / confirm it "
                                    "worked), then emit final — or state why validation "
                                    "is not possible."),
                    })
                    messages.append({"role": "user",
                                      "content": "[system] You acted but did not validate. "
                                                 "Run ONE read-only verification (re-query / "
                                                 "re-read / confirm the change), then emit final. "
                                                 "If validation is genuinely impossible, say so "
                                                 "explicitly in your final."})
                    continue
                final = str(action["final"])
                done = True
                await emit_event({
                    "type":      "agent_loop_v3.done",
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
                        "type": "agent_loop_v3.tool_done",
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
                        "type": "agent_loop_v3.tool_done",
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
                        "type": "agent_loop_v3.tool_done",
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
                    system_prompt = _v3_system_prompt(
                        goal, _toolkit_block(toolkit),
                        extra=ctx_extra, enable_expand=enable_expand,
                        toolkit_names=list(toolkit),
                        phased=phased, min_explore_cycles=min_explore_cycles,
                    )
                history.append({"tool": "(expand_tools)",
                                 "args": {"keywords": kws}, "ok": True,
                                 "preview": f"Toolkit expanded by {len(added)}: {added}"})
                await emit_event({
                    "type": "agent_loop_v3.toolkit",
                    "stream_id": stream_id, "toolkit": list(toolkit),
                    "added": added, "session_id": sid,
                })
                await emit_event({
                    "type": "agent_loop_v3.tool_done",
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
                    "type": "agent_loop_v3.tool_done",
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
                    "type": "agent_loop_v3.tool_done",
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
                    "type":       "agent_loop_v3.repetition_block",
                    "stream_id":  stream_id, "cycle": cycles,
                    "tool":       tool, "args": args,
                    "session_id": sid,
                })
                continue

            # ── Phase gate: explore before you act ──────────────────────────
            _forced_hitl_done = False
            if phased and _cap_phase(tool) == "act" and not _explore_satisfied():
                if long_running_force_hitl and _is_long_running_cap(tool):
                    # Long-running tool requested before the explore phase is
                    # satisfied — require an explicit human go/no-go regardless
                    # of the global HITL setting.
                    await _emit_phase("act", reason="long_running_pre_explore", tool=tool)
                    await emit_event({
                        "type": "agent_loop_v3.hitl_request",
                        "stream_id": stream_id, "cycle": cycles, "step": cycles - 1,
                        "tool": tool, "args": args, "thought": thought,
                        "reason": "long_running_pre_explore",
                        "session_id": sid, "timeout_secs": hitl_timeout_secs,
                    })
                    decision_obj = await _await_hitl_decision(
                        sid, cycles - 1, timeout=float(hitl_timeout_secs),
                    )
                    decision = (decision_obj or {}).get("decision", "")
                    if decision == "abort":
                        final = "Aborted by user before a long-running tool ran."
                        done = True
                        await emit_event({
                            "type": "agent_loop_v3.done",
                            "stream_id": stream_id, "cycles": cycles,
                            "summary": final, "reason": "hitl_abort", "session_id": sid,
                        })
                        break
                    if decision == "reject":
                        history.append({
                            "tool": tool, "args": args, "ok": False,
                            "preview": ("HITL: user declined this long-running call before "
                                        "exploration. Gather context with read-only tools "
                                        "first. " + (decision_obj.get("comment") or "")),
                        })
                        await emit_event({
                            "type": "agent_loop_v3.hitl_resolved",
                            "stream_id": stream_id, "cycle": cycles,
                            "decision": "reject", "session_id": sid,
                        })
                        continue
                    if decision == "edit":
                        new_args = (decision_obj or {}).get("args") or {}
                        if isinstance(new_args, dict):
                            args = new_args
                    await emit_event({
                        "type": "agent_loop_v3.hitl_resolved",
                        "stream_id": stream_id, "cycle": cycles,
                        "decision": decision or "auto_approve_timeout", "session_id": sid,
                    })
                    _forced_hitl_done = True
                else:
                    # Generic act cap requested before exploring — hard-block with
                    # a nudge. Does NOT consume the productive-cycle budget.
                    await _emit_phase("explore", reason="act_blocked", tool=tool)
                    _blk = (f"BLOCKED: '{tool}' is an action tool, but you have only "
                            f"explored {explore_done}/{min_explore_cycles} times. Use "
                            "read-only tools (get/list/search/query/describe) to gather "
                            "context first, then act.")
                    history.append({"tool": "(act_blocked)", "args": {"tool": tool},
                                     "ok": False, "preview": _blk})
                    messages.append({"role": "user", "content": "[system] " + _blk})
                    await emit_event({
                        "type": "agent_loop_v3.tool_done",
                        "stream_id": stream_id, "cycle": cycles, "tool": tool,
                        "ok": False, "elapsed_ms": 0, "preview": _blk[:400],
                        "error": "act blocked pre-explore", "session_id": sid,
                    })
                    continue

            # ── HITL pause ──────────────────────────────────────────────────
            if require_approval and not _forced_hitl_done:
                await emit_event({
                    "type": "agent_loop_v3.hitl_request",
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
                        "type": "agent_loop_v3.done",
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
                        "type": "agent_loop_v3.hitl_resolved",
                        "stream_id": stream_id, "cycle": cycles,
                        "decision": "reject", "session_id": sid,
                    })
                    continue
                if decision == "edit":
                    new_args = decision_obj.get("args") or {}
                    if isinstance(new_args, dict):
                        args = new_args
                    await emit_event({
                        "type": "agent_loop_v3.hitl_resolved",
                        "stream_id": stream_id, "cycle": cycles,
                        "decision": "edit", "args": args, "session_id": sid,
                    })
                elif decision == "approve":
                    await emit_event({
                        "type": "agent_loop_v3.hitl_resolved",
                        "stream_id": stream_id, "cycle": cycles,
                        "decision": "approve", "session_id": sid,
                    })
                else:
                    await emit_event({
                        "type": "agent_loop_v3.hitl_resolved",
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
                    "type": "agent_loop_v3.args_coerced",
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
                "type": "agent_loop_v3.tool_call",
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
                    goal=goal, thought=thought,
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

            # ── Phase accounting ────────────────────────────────────────────
            # Successful read-only calls advance the explore phase; a read-only
            # call AFTER acting counts as validation. Anything else is an act.
            if invoke.get("ok") and not empty_search:
                if _cap_phase(tool) == "explore":
                    explore_done += 1
                    if acted and not validated:
                        validated = True
                        await _emit_phase("validate", validated=True, tool=tool)
                    else:
                        await _emit_phase("explore", tool=tool)
                else:
                    acted = True
                    await _emit_phase("act", tool=tool)

            messages.append({"role": "user",
                              "content": f"[tool_result {tool}]\n{preview[:1200]}"})

            if stream_append and stream_id:
                try:
                    await stream_append(stream_id,
                                         f"\n[exec #{cycles}] {tool}({json.dumps(args, default=str)[:200]}) → {preview[:400]}\n")
                except Exception:
                    pass

            await emit_event({
                "type": "agent_loop_v3.tool_done",
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
                        "type":      "agent_loop_v3.done",
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
            "type":      "agent_loop_v3.done",
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
                "type": "agent_loop_v3.handover_error",
                "error": str(e), "session_id": sid,
            })

    return {
        "goal":               goal,
        "triage":             triage,
        "toolkit":            toolkit,
        "relevant_datasets":  [d.get("dataset_id") for d in relevant_datasets],
        "history":            history,
        "messages":           messages,
        "cycles":             cycles,
        "done":               done,
        "summary":            final,
        "final":              final,
        "handover_output":    handover_output,
        "stream_id":          stream_id,
        "session_id":         sid,
        "phase":              phase,
        "explore_done":       explore_done,
        "validated":          validated,
        "auto_continues":     auto_continues,
    }


# ═════════════════════════════════════════════════════════════════════════════
# V4 — strict explore→think→act→verify loop with step-selection, plan/todo step,
#      stricter completion verification, and smart terminal-tool steering.
# ─────────────────────────────────────────────────────────────────────────────
# v4 builds on v3 (full message history, HITL, long-running awaiting, recovery,
# repetition guard, budget/continue) and adds:
#   • A user-gated, agent-picked SET of steps (plan/explore/think/act/verify).
#   • A PLAN step that emits a todo list and tracks it across the run.
#   • A tight per-item cadence (explore → think → act → verify) with a hard
#     verify-before-finish gate.
#   • A strict completion check before `final` is accepted (all todos done,
#     every act followed by a read-only verify, and an evidence-backed judge).
#   • Smart terminal tooling: when exec.bash.run is available it steers the
#     model to grep/sed/awk for cheap, composable file inspection/manipulation.
#   • Per-run long-running overrides (mark arbitrary caps as long-running) and a
#     genuinely optional long-running HITL gate.
# All v4 events are namespaced agent_loop_v4.* and are twin-aliased to the v3
# names by the SSE wrapper so existing renderers light up unchanged.
# ═════════════════════════════════════════════════════════════════════════════

_V4_ALL_STEPS = ["plan", "explore", "think", "act", "verify"]

# Categories where searching the data fabric for relevant datasets is worthwhile.
# For everything else (system_info, network_scan, exec-style goals, etc.) the
# dataset LLM-fallback is pure latency before the first cycle — skip it.
_V4_DATA_CATEGORIES = {
    "data_lookup", "data_pipeline", "research", "analysis",
    "summarisation", "search", "memory_recall", "report_generation",
}

# Read-only "discovery/listing" caps — useful for orienting, but repeating them
# once a plan item is pending is the classic v4 stall (it re-lists datasets/caps
# instead of executing the todo). The plan-progress logic excludes these so a
# discovery call never counts as completing a todo item.
_V4_DISCOVERY_CAPS = {
    "caps.search", "caps.describe", "context.search_caps",
    "context.search_dags", "fabric.datasets",
}

# Default BASE TOOLKIT — used when the loop isn't given one (e.g. an agent with
# no configured baseline). Web & browser + skills, per product default. Only the
# entries actually present in the registry are seeded (the builder filters).
_V4_DEFAULT_BASE_TOOLKIT = [
    # Terminal — write/read/edit files (echo > file, cat, grep, sed) + run code.
    "exec.bash.run", "exec.ps.run", "exec.code.run",
    # Structured code/file tools — write artifact, list (tree), read, grep, replace, edit.
    "exec.sandbox.write_artifact",
    "ide.code.list_files", "ide.code.read_lines", "ide.code.grep",
    "ide.code.replace", "ide.code.edit_lines", "ide.code.insert_at",
    # Web & browser.
    "web.search", "web.fetch", "http.get",
    "browser.navigate", "browser.action", "scrape.fetch",
    # Skills.
    "fabric.skills.list", "fabric.skills.get",
]


async def _v4_select_steps(goal: str, enabled: List[str], *, model: str = "",
                           instance_id: str = "", prefer_gpu: bool = True) -> Dict[str, Any]:
    """Agent-side meta-step: choose which of the *enabled* steps to run for this
    goal. The returned list is always a subset of `enabled` (never exceeds the
    user's gate). Falls back to the full enabled set on any failure."""
    enabled = [s for s in (enabled or []) if s in _V4_ALL_STEPS] or list(_V4_ALL_STEPS)
    sys = (
        "You are configuring an autonomous agent's workflow. Given a GOAL and the set of "
        "ALLOWED steps, choose the MINIMAL subset actually needed to do the job well.\n"
        "Steps: plan (break the goal into a todo list), explore (read-only investigation "
        "before acting), think (explicit reasoning), act (take actions / mutations), "
        "verify (confirm each action worked).\n"
        "Rules: keep 'act' if the goal requires doing anything; keep 'verify' for any goal "
        "that changes state; include 'plan' for multi-step goals; include 'explore' unless "
        "the goal is trivial.\n"
        'Respond ONLY with JSON: {"steps":["plan","explore","think","act","verify"],"reason":"<one sentence>"}'
    )
    prompt = f"GOAL: {goal}\nALLOWED STEPS: {', '.join(enabled)}\nChoose the steps to run."
    chosen: List[str] = []
    reason = ""
    try:
        raw = await _safe_ollama_generate_dw(
            prompt, system=sys, model=model, instance_id=instance_id,
            prefer_gpu=prefer_gpu, json_mode=True,
        )
        parsed = _extract_json(_strip_think(raw or "")[0]) or {}
        chosen = [s for s in (parsed.get("steps") or []) if s in enabled]
        reason = str(parsed.get("reason") or "")
    except Exception as e:
        log.debug("v4 select_steps failed: %s", e)
    if not chosen:
        chosen = list(enabled)
        reason = reason or "Defaulted to all allowed steps."
    # Canonical order, and 'act'/'think' are always kept if allowed (a loop that
    # cannot act or reason is useless).
    for must in ("think", "act"):
        if must in enabled and must not in chosen:
            chosen.append(must)
    chosen = [s for s in _V4_ALL_STEPS if s in chosen]
    return {"steps": chosen, "reason": reason}


async def _v4_make_plan(goal: str, toolkit_brief: str, *, model: str = "",
                        instance_id: str = "", prefer_gpu: bool = True,
                        steps: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """PLAN step: break the goal into a short, ordered, verifiable todo list.

    `steps` are the stages actually enabled for this run — the plan must not
    assume stages that aren't active (e.g. don't add a 'verify' item when verify
    isn't an enabled step), which is what made plans contradict the stage set."""
    stage_note = ""
    if steps:
        active = ", ".join(s for s in steps if s != "plan")
        stage_note = (f"\nThe agent will run ONLY these stages: {active}. Each todo must be "
                      "achievable within those stages — do NOT add steps for stages that are "
                      "not enabled (e.g. no explicit 'verify the result' item if verify is off).\n")
    sys = (
        "You are planning an autonomous agent's work. Break the GOAL into a short ordered "
        "todo list (2-6 concrete, individually verifiable steps). Each item is a single "
        "action or check. Prefer steps achievable with the AVAILABLE TOOLS.\n"
        + stage_note +
        'Respond ONLY with JSON: {"todos":[{"task":"..."},{"task":"..."}]}'
    )
    prompt = f"GOAL: {goal}\nAVAILABLE TOOLS: {toolkit_brief}\nProduce the todo list."
    todos: List[Dict[str, Any]] = []
    try:
        raw = await _safe_ollama_generate_dw(
            prompt, system=sys, model=model, instance_id=instance_id,
            prefer_gpu=prefer_gpu, json_mode=True,
        )
        parsed = _extract_json(_strip_think(raw or "")[0]) or {}
        items = parsed.get("todos") or parsed.get("plan") or []
        for i, it in enumerate(items[:8]):
            task = it.get("task") if isinstance(it, dict) else it
            if task:
                todos.append({"id": i + 1, "task": str(task)[:200], "done": False})
    except Exception as e:
        log.debug("v4 make_plan failed: %s", e)
    if not todos:
        todos = [{"id": 1, "task": goal[:200], "done": False}]
    return todos


def _v4_smart_tools_block(toolkit_names: Optional[List[str]]) -> str:
    if not toolkit_names or "exec.bash.run" not in toolkit_names:
        return ""
    return (
        "\nSMART TERMINAL TOOL USE:\n"
        "• You have exec.bash.run — a real shell. To inspect or manipulate files and text, "
        "prefer small composable Unix tools over bespoke caps:\n"
        "    - grep / rg to FIND text + locations (grep -rn \"pattern\" path).\n"
        "    - sed to VIEW ranges or edit in place (sed -n '10,40p' file ; sed -i 's/a/b/g' file).\n"
        "    - awk for column/field extraction and quick aggregation.\n"
        "    - head / tail / wc / find / ls / cat for cheap reads.\n"
        "• Chain with pipes to get exactly what you need in ONE call instead of many.\n"
        "• Read-only shell (grep/ls/find/head/tail/wc/cat/sed -n) counts as EXPLORE; "
        "writes (sed -i, >, mv, rm, mkdir, install) count as ACT and need verification after.\n"
    )


def _v4_cadence_block(steps: List[str], min_explore_cycles: int) -> str:
    parts = ["\nWORK IN A TIGHT CADENCE — repeat for each todo item, one item at a time:\n"]
    if "plan" in steps:
        parts.append("  • PLAN — work the todo list top-to-bottom. Emit "
                     '{"thought":"...","todo_done":<id>} when an item is fully done & verified.\n')
    if "explore" in steps:
        parts.append(f"  • EXPLORE — for the CURRENT item, make at least "
                     f"{max(1, min_explore_cycles)} cheap read-only call(s) "
                     "(grep/ls/get/list/search/query/read) BEFORE acting.\n")
    if "think" in steps:
        parts.append("  • THINK — in `thought`, state what exploration showed and your exact next action.\n")
    if "act" in steps:
        parts.append("  • ACT — take the single action that advances the CURRENT item.\n")
    if "verify" in steps:
        parts.append("  • VERIFY — immediately confirm that action worked with a read-only check "
                     "(re-grep / re-read / re-query). Only then move to the next item.\n")
    parts.append("Do NOT batch several actions before verifying. One act → one verify → next item.\n")
    return "".join(parts)


def _v4_system_prompt(goal: str, toolkit_block: str, *, steps: List[str],
                      todos: Optional[List[Dict[str, Any]]] = None,
                      extra: str = "", enable_expand: bool = True,
                      toolkit_names: Optional[List[str]] = None,
                      min_explore_cycles: int = 1) -> str:
    todo_block = ""
    if todos:
        todo_lines = "\n".join(f"  {t['id']}. [{'x' if t.get('done') else ' '}] {t['task']}"
                                for t in todos)
        todo_block = ("\n═════════════════════════════════════════════════════════════\n"
                      "YOUR PLAN (todo list) — work it in order, keep it updated\n"
                      "═════════════════════════════════════════════════════════════\n"
                      + todo_lines + "\n")
    return (
        "You are a Vera autonomous agent operating in V4 mode — a strict "
        "explore→think→act→verify worker.\n\n"
        f"GOAL: {goal}\n"
        + todo_block +
        "\n═════════════════════════════════════════════════════════════\n"
        "YOUR TOOLKIT — curated for this goal by a triage step. Start here.\n"
        "═════════════════════════════════════════════════════════════\n"
        f"{toolkit_block}\n\n"
        "ON EACH TURN: do your reasoning FIRST inside a <think>...</think> block (or a brief "
        "line of prose), THEN emit EXACTLY ONE compact JSON object — the ACTION ONLY — as the "
        "last thing in your reply:\n"
        '  {"tool_use":{"name":"<cap.name>","input":{...}}}\n'
        '  {"todo_done":<id>}   (mark a verified todo item complete)\n'
        '  {"final":"<answer addressing the GOAL>"}\n'
        'An optional "thought":"<one line>" key is allowed, but keep any longer rationale '
        "OUTSIDE the JSON (in <think>) so it can never truncate your action.\n"
        "EVERY object MUST contain an action key (tool_use, todo_done, or final). A bare "
        '{"thought":"…"} with no action is NOT a valid turn — pair reasoning with an action.\n'
        "STAGE CONTROL — you order the stages: if you have already learned enough, add "
        '"explore_done":true to your tool_use to act immediately (skip the explore minimum); '
        'add "verified":true when an action is already confirmed so you can finish without a '
        "separate re-check. Use these when a stage's goal is met.\n\n"
        "RULES:\n"
        "1. PICK A TOOL FROM THE TOOLKIT on the FIRST turn — it is already filtered.\n"
        "2. Tool result messages tagged [tool_result <name>] are YOUR observations, "
        "not new user requests.\n"
        "3. Inspect each schema. Required params are marked [REQUIRED]; enums must use the literals.\n"
        "4. NEVER repeat the same (tool, args) pair — the result is identical.\n"
        "5. caps.search / context.search_caps / expand_tools are LAST RESORT "
        + ("(at most 1 expand + 2 searches per run).\n" if enable_expand else "(at most 2 searches per run).\n") +
        "6. Emit final ONLY after every todo item is done AND each action was verified. "
        "A strict completion check runs before your final is accepted.\n"
        "7. Reasoning goes in <think> (or a line before the JSON) — NEVER as a long string "
        "inside the action object. Emit only the single JSON action; do not restate observations.\n"
        + _v4_cadence_block(steps, min_explore_cycles)
        + _v4_smart_tools_block(toolkit_names)
        + (("\n" + extra) if extra else "")
    )


# ── Command-aware classification for shell caps ──────────────────────────────
# The module-level _cap_phase / _is_long_running_cap treat ALL exec.* as "act"
# and "long-running". That breaks v4's cadence for terminal-tool goals: a
# read-only `whoami`/`grep`/`ls` can never count as explore/verify, so todos
# never complete, the verify gate is never satisfied, and long-running HITL
# fires on every command. v4 inspects the actual command instead.

_SHELL_CAPS = {"exec.bash.run", "exec.ps.run", "exec.ssh.run"}

# First-token commands that only READ state (safe → "explore").
_SHELL_READONLY_CMDS = {
    "whoami", "id", "hostname", "uname", "pwd", "echo", "date", "uptime",
    "ls", "ll", "dir", "cat", "head", "tail", "wc", "stat", "file", "du", "df",
    "grep", "egrep", "fgrep", "rg", "find", "locate", "which", "type", "env",
    "printenv", "ps", "top", "free", "lsblk", "lscpu", "mount", "ip", "ifconfig",
    "netstat", "ss", "ping", "dig", "nslookup", "host", "curl", "wget", "awk",
    "cut", "sort", "uniq", "tr", "jq", "tree", "readlink", "realpath", "basename",
    "dirname", "test", "true", "false", "git", "diff", "cmp", "md5sum", "sha256sum",
    "get-childitem", "get-content", "get-location", "select-string",
}
# Patterns anywhere in the command that imply a WRITE/MUTATION (→ "act").
_SHELL_WRITE_HINTS = (
    " rm ", " rm -", "mkdir", " mv ", " cp ", "rmdir", " dd ", "truncate", "tee ",
    "sed -i", ">>", " > ", " >", "chmod", "chown", "ln -", "touch ", "install ",
    "apt ", "apt-get", "yum ", "dnf ", "pip install", "npm install", "git push",
    "git commit", "git clean", "kill ", "pkill", "systemctl", "service ",
    "docker run", "docker build", "set-content", "out-file", "remove-item",
)
# Patterns that imply a LONG-RUNNING command (→ HITL/await candidate).
_SHELL_LONG_HINTS = (
    "sleep ", "apt ", "apt-get", "yum ", "dnf ", "pip install", "npm install",
    "docker build", "docker run", "make", "cmake", "gcc ", "g++ ", "cargo build",
    "nmap", "ffmpeg", "rsync", "scp ", "train", "wget ", "curl ", "git clone",
    "./configure", "pytest", "npm run build", "terraform apply", "ansible",
)


def _shell_command_text(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    return str(args.get("command") or args.get("script") or args.get("cmd") or "")


def _shell_first_token(cmd: str) -> str:
    cmd = (cmd or "").strip().lstrip("$( ").lstrip()
    # strip leading env assignments like FOO=bar cmd
    parts = cmd.split()
    i = 0
    while i < len(parts) and "=" in parts[i] and not parts[i].startswith("-"):
        i += 1
    tok = parts[i] if i < len(parts) else ""
    return tok.split("/")[-1].lower()


def _shell_is_readonly(cmd: str) -> bool:
    low = " " + (cmd or "").lower() + " "
    if any(h in low for h in _SHELL_WRITE_HINTS):
        return False
    # If any sub-command (split on |,&&,;) has a write first-token, it's an act.
    import re as _re
    segs = _re.split(r"\|\||&&|;|\|", cmd or "")
    tokens = [_shell_first_token(s) for s in segs if s.strip()]
    if not tokens:
        return False
    return all(t in _SHELL_READONLY_CMDS for t in tokens)


def _shell_is_long(cmd: str) -> bool:
    low = " " + (cmd or "").lower() + " "
    return any(h in low for h in _SHELL_LONG_HINTS)


def _v4_phase(tool: str, args: Any = None) -> str:
    """v4 phase classifier — command-aware for shell caps, else falls back to
    the module-level heuristic."""
    if tool in _SHELL_CAPS:
        return "explore" if _shell_is_readonly(_shell_command_text(args)) else "act"
    return _cap_phase(tool)


def _v4_is_long_running(tool: str, args: Any = None, *, extra: Optional[set] = None) -> bool:
    """v4 long-running detector. Shell caps are judged by the command (so a
    50ms `whoami` is NOT long-running) instead of the blanket exec-group rule."""
    if extra and tool in extra:
        return True
    if tool in _SHELL_CAPS:
        return _shell_is_long(_shell_command_text(args))
    return _is_long_running_cap(tool)


async def _v4_completion_check(goal: str, history: List[Dict[str, Any]],
                               todos: Optional[List[Dict[str, Any]]], *,
                               model: str = "", instance_id: str = "",
                               prefer_gpu: bool = True) -> Dict[str, Any]:
    """Gate before accepting `final`. Returns {passed, missing, summary}.

    Structural-primary: the goal-satisfaction judge is a flaky LLM (it silently
    returns "not satisfied" whenever its JSON reply comes back empty), so it must
    NOT be able to veto an otherwise-complete run. The only blocking reasons are
    deterministic: open todo items, and any 'act' lacking a later read-only
    verification. A convinced judge is still a fast-path PASS; an unconvinced
    judge is purely advisory and never blocks on its own."""
    missing: List[str] = []
    summary = ""
    ctx = _ctx()
    judge = getattr(ctx, "_check_goal_satisfied", None) if ctx else None

    judged = None
    if judge:
        evidence = " | ".join(h.get("preview", "")[:200]
                              for h in history[-5:] if h.get("ok"))
        try:
            sat = await judge(goal, evidence, model=model,
                              instance_id=instance_id, prefer_gpu=prefer_gpu)
            judged = bool(sat.get("satisfied"))
            summary = sat.get("summary", "") or ""
        except Exception as e:
            log.debug("v4 completion judge failed: %s", e)

    if judged is True:
        return {"passed": True, "missing": [], "summary": summary}

    # Judge unconvinced or unavailable → apply the deterministic structural
    # gates only. (The judge's opinion is advisory and never blocks by itself.)
    undone = [t for t in (todos or []) if not t.get("done")]
    if undone:
        missing.append(f"{len(undone)} todo item(s) still open: "
                       + "; ".join(t.get("task", "")[:50] for t in undone[:4]))
    for idx, h in enumerate(history):
        if not (h.get("ok") and not str(h.get("tool", "")).startswith("(")):
            continue
        if _v4_phase(h.get("tool", ""), h.get("args")) != "act":
            continue
        verified_after = any(
            hh.get("ok") and _v4_phase(hh.get("tool", ""), hh.get("args")) == "explore"
            for hh in history[idx + 1:]
        )
        if not verified_after:
            missing.append(f"action '{h.get('tool')}' has no read-only verification after it")
            break

    return {"passed": not missing, "missing": missing, "summary": summary}


@capability(
    "dag.agent_loop_v4",
    http_method="POST", http_path="/dag/agent_loop_v4",
    http_tags=["dag", "agents"],
    memory="on",
    streams=["dag.agent_loop_v4"],
    description=(
        "v4 agent loop: a strict explore→think→act→verify worker built on v3. Adds a "
        "user-gated + agent-picked step set (plan/explore/think/act/verify), a PLAN/todo step, "
        "a tight per-item cadence with a hard verify-before-finish gate, a strict completion "
        "check before final, smart terminal-tool steering (grep/sed/awk via exec.bash.run), "
        "per-run long-running overrides, and an optional long-running HITL gate. "
        "Inputs: goal (str!), allowed_caps (csv), max_cycles (int default 12), "
        "enabled_steps (csv default 'plan,explore,think,act,verify'), select_steps (bool default True), "
        "require_verify (bool default True), strict_complete (bool default True), "
        "prefer_terminal_tools (bool default True), long_running_caps (csv default ''), "
        "long_running_force_hitl (bool default True), require_approval (bool default False), "
        "plus the v3 args (satisfaction_check, enable_expand, triage_top_k, await_long_running, "
        "long_running_timeout_secs, max_search_calls, max_expands, count_failed_cycles, "
        "min_explore_cycles, allow_continue, continue_increment, auto_continue_max, model, "
        "instance_id, prefer_gpu, session_id). "
        "Output: {goal, history, messages, cycles, done, final, toolkit, triage, steps, todos, stream_id}."
    ),
)
async def cap_dag_agent_loop_v4(
    goal:               str,
    allowed_caps:       str  = "",
    max_cycles:         int  = 12,
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
    triage_category:    str  = "",
    triage_keywords:    str  = "",
    base_toolkit:       str  = "",
    hitl_timeout_secs:  int  = 300,
    await_long_running: bool = True,
    long_running_timeout_secs: int = 1800,
    handover:           bool = False,
    handover_max_chars: int  = 20000,
    max_search_calls:   int  = 2,
    max_expands:        int  = 1,
    count_failed_cycles: bool = False,
    max_recovery_attempts: int = 2,
    system_prompt_template: str = "",
    inject_fabric_records: bool = False,
    # ── v3 phase / continue params (reused) ─────────────────────────────────
    min_explore_cycles: int  = 1,
    allow_continue:     bool = True,
    continue_increment: int  = 8,
    auto_continue_max:  int  = 0,
    # ── v4-specific ─────────────────────────────────────────────────────────
    enabled_steps:      str  = "plan,explore,think,act,verify",
    select_steps:       bool = True,
    require_verify:     bool = True,
    strict_complete:    bool = True,
    prefer_terminal_tools: bool = True,
    long_running_caps:  str  = "",
    long_running_force_hitl: bool = True,
    trace_id=None,
):
    if not goal:
        return {"error": "goal required"}
    max_cycles = max(1, min(60, int(max_cycles)))
    min_explore_cycles = max(0, int(min_explore_cycles))
    continue_increment = max(1, min(40, int(continue_increment)))
    auto_continue_max  = max(0, int(auto_continue_max))
    triage_top_k = max(1, min(64, int(triage_top_k)))
    sid = session_id or str(uuid.uuid4())

    ctx = _ctx()
    ds  = _dag_store()
    ollama_generate = getattr(ctx, "ollama_generate", None) if ctx else None
    if ollama_generate is None:
        return {"error": "context module not loaded — ollama_generate missing"}

    # Per-run long-running override set — used by the local _lr() helper so v3's
    # module-level detection stays untouched. v4 is command-aware: a quick
    # read-only shell command is NOT long-running (see _v4_is_long_running).
    lr_extra = {c.strip() for c in (long_running_caps or "").split(",") if c.strip()}
    def _lr(name: str, args: Any = None) -> bool:
        return _v4_is_long_running(name, args, extra=lr_extra)

    user_enabled = [s.strip() for s in (enabled_steps or "").split(",") if s.strip()]
    user_enabled = [s for s in user_enabled if s in _V4_ALL_STEPS] or list(_V4_ALL_STEPS)

    # ── Stage 1: TRIAGE ──────────────────────────────────────────────────────
    await emit_event({"type": "agent_loop_v4.triage_start",
                      "goal": goal[:200], "session_id": sid})
    try:
        triage = await _workshop_triage_goal(
            goal, model=model, instance_id=instance_id, prefer_gpu=prefer_gpu)
    except Exception as e:
        log.debug("v4 triage failed: %s", e)
        triage = {"category": "other", "keywords": [], "reasoning": ""}
    # ── Caller triage overrides (from chat UI / workshop panel) ───────────────
    # A forced category pins toolkit seeding; seed keywords are merged in front of
    # the triaged ones so the user can steer cap discovery when auto-triage is weak.
    _force_cat = (triage_category or "").strip().lower()
    if _force_cat and _force_cat != "auto":
        triage["category"] = _force_cat
        cats = [c for c in (triage.get("categories") or []) if c != _force_cat]
        triage["categories"] = [_force_cat] + cats
        triage["reasoning"] = (triage.get("reasoning") or "") + " [category forced by user]"
    _seed_kws = [k.strip().lower() for k in (triage_keywords or "").replace(",", " ").split() if k.strip()]
    if _seed_kws:
        triage["keywords"] = list(dict.fromkeys(_seed_kws + (triage.get("keywords") or [])))[:8]
    await emit_event({"type": "agent_loop_v4.triage_done",
                      "triage": triage, "session_id": sid})

    # ── Base toolkit ─────────────────────────────────────────────────────────
    # The caller (chat / workshop UI) passes the active agent's baseline caps
    # here (or a decoupled per-run set). These are ALWAYS in the toolkit and act
    # as the floor when triage can't narrow things down. Empty → product default
    # (Web & browser + skills).
    base_caps = [c.strip() for c in (base_toolkit or "").replace(",", " ").split() if c.strip()]
    if not base_caps:
        base_caps = list(_V4_DEFAULT_BASE_TOOLKIT)

    # ── Stage 2: TOOLKIT ─────────────────────────────────────────────────────
    toolkit = await _workshop_build_toolkit(
        allowed_caps=allowed_caps,
        category=triage.get("category", "other"),
        categories=triage.get("categories"),
        keywords=triage.get("keywords", []),
        top_k=int(triage_top_k),
        goal=goal,
        base_caps=base_caps,
    )
    if not toolkit:
        return {"error": "No usable tools after triage", "triage": triage}

    # Seed the terminal tool so smart grep/sed/awk usage is always available.
    if prefer_terminal_tools and "exec.bash.run" in CAPABILITY_REGISTRY \
            and "exec.bash.run" not in toolkit:
        toolkit.insert(0, "exec.bash.run")

    # ── Artifact directory (sandbox-configured) ──────────────────────────────
    # Where the agent writes generated files (code/documents). Resolved per the
    # exec sandbox's artifact_scope; the dir is sandbox-allowed for writes and
    # used as the default cwd for exec.* calls so output lands there.
    artifact_dir_path = ""
    try:
        import importlib as _il
        _exec_mod = _il.import_module("Vera.vera.execution.exec_capabilities")
        artifact_dir_path = _exec_mod.artifact_dir(session_id=sid)
    except Exception as e:
        log.debug("v4 artifact dir resolve failed: %s", e)
    if artifact_dir_path:
        await emit_event({"type": "agent_loop_v4.artifact_dir",
                          "session_id": sid, "dir": artifact_dir_path})

    # ── Dataset discovery + step selection (run CONCURRENTLY) ────────────────
    # Both are independent of the toolkit and of each other. v4 has more
    # pre-loop LLM calls than v2/v3 (dataset search + step selection + plan),
    # and running them serially is the main reason the first cycle feels slow.
    # The dataset LLM-fallback is also gated to data-shaped goals so a `whoami`
    # never pays for a fabric scan it can't use.
    _data_shaped = (triage.get("category", "") in _V4_DATA_CATEGORIES
                    or any(c in _V4_DATA_CATEGORIES
                           for c in (triage.get("categories") or [])))

    async def _ds_task():
        return await _search_relevant_datasets(
            goal, keywords=triage.get("keywords", []),
            model=model, instance_id=instance_id, prefer_gpu=bool(prefer_gpu),
            allow_llm_fallback=_data_shaped)

    async def _steps_task():
        if select_steps:
            return await _v4_select_steps(goal, user_enabled, model=model,
                                          instance_id=instance_id, prefer_gpu=prefer_gpu)
        return {"steps": list(user_enabled), "reason": "user-fixed step set"}

    relevant_datasets, sel = await asyncio.gather(_ds_task(), _steps_task())
    if relevant_datasets:
        for ds_cap in ("fabric.datasets", "fabric.query"):
            if ds_cap in CAPABILITY_REGISTRY and ds_cap not in toolkit:
                toolkit.insert(0, ds_cap)

    # ── Optional skills/ontologies context ──────────────────────────────────
    ctx_extra = ""
    build_context_prompt = getattr(ctx, "build_context_prompt", None)
    if (attach_skills or attach_ontologies) and build_context_prompt:
        try:
            cobj = await build_context_prompt(
                goal, attach_skills=attach_skills, attach_ontologies=attach_ontologies)
            ctx_extra = cobj.get("system_prompt", "")
        except Exception as e:
            log.debug("v4 context build: %s", e)
    if relevant_datasets:
        ds_lines = "\n".join(
            f"  • {d.get('dataset_id', '?')} ({d.get('record_count', 0)} records)"
            for d in relevant_datasets)
        ds_hint = ("\n\nRELEVANT DATASETS IN FABRIC:\n" + ds_lines
                   + "\nUse fabric.query(dataset_id=\"<name>\", text=\"<query>\") to search these.")
        ctx_extra = (ctx_extra.rstrip() + ds_hint) if ctx_extra else ds_hint.strip()
    if artifact_dir_path:
        art_hint = ("\n\nARTIFACT DIRECTORY: write any generated files (code, documents, "
                    f"reports) into:\n  {artifact_dir_path}\n"
                    "Use a relative filename (e.g. exec.bash.run with `cat > script.py`, or "
                    "ide.code.* paths under this dir) — exec.* calls already default their cwd "
                    "here. Read/grep/replace these files to edit prior output.")
        ctx_extra = (ctx_extra.rstrip() + art_hint) if ctx_extra else art_hint.strip()

    def _toolkit_block(names: List[str]) -> str:
        return "\n".join(rich_cap_signature(n) for n in names)

    # ── Stage 3: STEP SELECTION (computed above, agent-picked within the gate) ─
    steps = [s for s in (sel.get("steps") or user_enabled) if s in _V4_ALL_STEPS] \
        or list(user_enabled)

    # Single-action task: the agent decided this goal needs neither planning nor
    # exploration — i.e. one capability call should answer it. We let the first
    # successful, informative result finish the run (see the fast-path in the
    # loop below) instead of dragging it through a verify/completion dance.
    single_action = ("plan" not in steps) and ("explore" not in steps)
    await emit_event({"type": "agent_loop_v4.step_plan", "session_id": sid,
                      "steps": steps, "enabled": user_enabled,
                      "single_action": single_action,
                      "reason": sel.get("reason", "")})

    # ── Stage 4: PLAN / todo list ────────────────────────────────────────────
    todos: List[Dict[str, Any]] = []
    if "plan" in steps:
        todos = await _v4_make_plan(goal, ", ".join(toolkit[:30]), model=model,
                                    instance_id=instance_id, prefer_gpu=prefer_gpu, steps=steps)
        await emit_event({"type": "agent_loop_v4.plan", "session_id": sid, "todos": todos})

    async def _emit_plan():
        await emit_event({"type": "agent_loop_v4.plan", "session_id": sid, "todos": todos})

    def _mark_next_todo_done():
        for t in todos:
            if not t.get("done"):
                t["done"] = True
                return t
        return None

    # ── System prompt ─────────────────────────────────────────────────────────
    if system_prompt_template and system_prompt_template.strip():
        system_prompt = _expand_prompt_template(
            system_prompt_template, goal=goal,
            category=triage.get("category", ""),
            keywords=", ".join(triage.get("keywords") or []),
            reasoning=triage.get("reasoning", ""),
            toolkit_block=_toolkit_block(toolkit), toolkit_brief=", ".join(toolkit),
            toolkit_count=len(toolkit), ctx_extra=ctx_extra, enable_expand=enable_expand)
    else:
        system_prompt = _v4_system_prompt(
            goal, _toolkit_block(toolkit), steps=steps, todos=todos,
            extra=ctx_extra, enable_expand=enable_expand, toolkit_names=list(toolkit),
            min_explore_cycles=min_explore_cycles)

    def _rebuild_prompt():
        nonlocal system_prompt
        if system_prompt_template and system_prompt_template.strip():
            return
        system_prompt = _v4_system_prompt(
            goal, _toolkit_block(toolkit), steps=steps, todos=todos,
            extra=ctx_extra, enable_expand=enable_expand, toolkit_names=list(toolkit),
            min_explore_cycles=min_explore_cycles)

    # ── Stream registration ─────────────────────────────────────────────────
    stream_register = getattr(ctx, "stream_register", None)
    stream_complete = getattr(ctx, "stream_complete", None)
    stream_append   = getattr(ctx, "stream_append_token", None)
    stream_id = ""
    if stream_register:
        try:
            stream_id = await stream_register(
                kind="agent_loop_v4", source_cap="dag.agent_loop_v4",
                session_id=sid, label=goal[:80], persist_full=True,
                fabric_dataset="streams.agent_loop_v4",
                metadata={"goal": goal, "max_cycles": max_cycles, "triage": triage,
                          "initial_toolkit": list(toolkit), "steps": steps,
                          "require_approval": require_approval})
        except Exception:
            stream_id = ""

    await emit_event({"type": "agent_loop_v4.toolkit", "stream_id": stream_id,
                      "toolkit": list(toolkit), "session_id": sid,
                      "relevant_datasets": [d.get("dataset_id") for d in relevant_datasets]
                      if relevant_datasets else []})

    # ── State ─────────────────────────────────────────────────────────────────
    messages: List[Dict[str, str]] = []
    history:  List[Dict[str, Any]] = []
    cycles = 0
    productive_cycles = 0
    done = False
    final = ""
    expand_count = 0
    search_count = 0
    MAX_EXPANDS = max(0, int(max_expands))
    MAX_SEARCH_CALLS = max(1, int(max_search_calls))
    SEARCH_CAPS = {"caps.search", "caps.describe", "context.search_caps", "context.search_dags"}

    phase = "think"
    explore_done = 0
    acted = False
    validated = False
    completion_bounced = 0           # how many times the strict check bounced final
    MAX_COMPLETION_BOUNCES = 2
    auto_continues = 0
    gate_explore = "explore" in steps   # only gate act-before-explore if explore is enabled
    gate_verify  = "verify" in steps and require_verify

    async def _emit_phase(new_phase: str, **extra):
        nonlocal phase
        phase = new_phase
        await emit_event({"type": "agent_loop_v4.phase", "stream_id": stream_id,
                          "cycle": cycles, "phase": new_phase, "explore_done": explore_done,
                          "min_explore": min_explore_cycles, "session_id": sid, **extra})

    def _explore_satisfied() -> bool:
        return (not gate_explore) or explore_done >= max(1, min_explore_cycles)

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

    async def _budget_reached() -> bool:
        if count_failed_cycles:
            return cycle_i >= max_cycles
        return productive_cycles >= max_cycles

    async def _maybe_continue() -> bool:
        nonlocal max_cycles, auto_continues
        if not allow_continue:
            return False
        await emit_event({"type": "agent_loop_v4.budget_pause", "stream_id": stream_id,
                          "cycle": cycles, "cycles": cycles, "max_cycles": max_cycles,
                          "increment": continue_increment, "step": -(cycles + 1),
                          "auto_continues": auto_continues, "auto_continue_max": auto_continue_max,
                          "session_id": sid})
        if auto_continue_max > 0 and auto_continues < auto_continue_max:
            auto_continues += 1
            max_cycles += continue_increment
            await emit_event({"type": "agent_loop_v4.budget_continue", "stream_id": stream_id,
                              "cycle": cycles, "mode": "auto", "max_cycles": max_cycles,
                              "auto_continues": auto_continues, "session_id": sid})
            return True
        decision_obj = await _await_hitl_decision(
            sid, -(cycles + 1), timeout=float(max(hitl_timeout_secs, 600)))
        decision = (decision_obj or {}).get("decision", "")
        if decision == "continue":
            inc = int((decision_obj or {}).get("increment") or continue_increment)
            max_cycles += max(1, inc)
            await emit_event({"type": "agent_loop_v4.budget_continue", "stream_id": stream_id,
                              "cycle": cycles, "mode": "manual", "max_cycles": max_cycles,
                              "session_id": sid})
            return True
        return False

    try:
        cycle_i = 0
        while True:
            if await _budget_reached():
                if await _maybe_continue():
                    pass
                else:
                    break
            if not count_failed_cycles and cycle_i >= max_cycles * 3:
                history.append({"tool": "(hard_limit)", "args": {}, "ok": False,
                                "preview": f"Aborted: hit hard iteration limit ({cycle_i} cycles)."})
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

            remaining = max_cycles - productive_cycles
            quota_hint = (f"\n\nQUOTA: searches {search_count}/{MAX_SEARCH_CALLS}, "
                          f"expansions {expand_count}/{MAX_EXPANDS}, "
                          f"productive cycles {productive_cycles}/{max_cycles}.")
            if remaining <= 2:
                quota_hint += ("\n\n⚠ BUDGET WARNING: finish your verified items and emit "
                               '{"thought":"...","final":"<answer>"} NOW.')
            open_todos = [t for t in todos if not t.get("done")]
            cur = open_todos[0] if open_todos else None
            if todos:
                quota_hint += ("\n\nTODO STATUS: "
                               + (f"current item #{cur['id']}: {cur['task']}" if cur
                                  else "all items marked done — run a final verification then emit final."))
            phase_hint = ""
            if gate_explore and not _explore_satisfied():
                phase_hint = (f"\n\nPHASE: EXPLORE ({explore_done}/{max(1,min_explore_cycles)} done). "
                              "Use cheap read-only tools to build context before acting.")
            elif gate_verify and acted and not validated:
                phase_hint = ("\n\nPHASE: VERIFY. You acted — confirm it worked with ONE read-only "
                              "check, then emit final. Do NOT re-run the same checks repeatedly.")
            elif cur:
                # A PLAN with work remaining → drive the current item to done.
                # Crucially, do NOT invite `final` or more discovery here: that is
                # exactly what makes v4 stall (premature final → completion-check
                # bounce, or endless re-listing) instead of following its plan.
                phase_hint = (f"\n\nPHASE: EXECUTE todo #{cur['id']} — {cur['task']}\n"
                              "Make the SINGLE tool call that completes THIS item now. You have "
                              "explored enough — do NOT run more discovery/listing calls "
                              "(caps.search, caps.describe, fabric.datasets). When the item is "
                              f'done, reply {{"thought":"...","todo_done":{cur["id"]}}}. Do NOT emit '
                              "`final` until EVERY todo item is checked off.")
            elif explore_done > 0 and not acted:
                # Read-only / informational goal: observations may already answer it.
                phase_hint = ("\n\nPHASE: DECIDE. If your observations already answer the GOAL, emit "
                              '{"thought":"...","final":"<answer>"} NOW — do not repeat read-only '
                              "calls you have already made. Otherwise take the next needed action.")
            else:
                phase_hint = ("\n\nPHASE: ACT. Take the action that advances the current todo item. "
                              "If the GOAL is already answered, emit final instead.")

            # Anti-repeat nudge: repeated read-only calls with no forward progress.
            recent_ok = [h for h in history[-3:] if h.get("ok") and not str(h.get("tool","")).startswith("(")]
            if len(recent_ok) >= 2 and all(_v4_phase(h.get("tool",""), h.get("args")) == "explore" for h in recent_ok):
                if cur:
                    phase_hint += (f"\n\nYou keep running read-only/discovery calls without progress. "
                                   f'STOP and EXECUTE todo #{cur["id"]} now: {cur["task"]}')
                else:
                    phase_hint += ("\n\nYou have already gathered enough read-only evidence. STOP re-checking "
                                   'and emit {"thought":"...","final":"<answer>"} now.')

            user_msg = (f"REMINDER: GOAL = \"{goal}\"\n\n"
                        "These are your past tool results (NOT a new user message). "
                        "Do NOT repeat or quote them back — use them to decide:\n\n"
                        + obs_block + quota_hint + phase_hint
                        + "\n\nReply with ONE JSON action toward the GOAL (do not restate the observations).")

            await emit_event({"type": "agent_loop_v4.cycle_planning", "stream_id": stream_id,
                              "cycle": cycles, "session_id": sid})

            # Live thought streaming: forward the model's reasoning to the UI
            # token-by-token as it generates. Models put reasoning in different
            # places — a <think> block, prose before the JSON, OR (most commonly
            # here) the JSON "thought" field — so we extract from ALL of them and
            # recompute the cumulative reasoning each token (idempotent live view).
            _think_stream = {"acc": "", "emitted": 0}
            async def _think_stream_cb(tok, _ts=_think_stream, _cyc=cycles):
                if not stream_id:
                    return
                try:
                    import re as _re
                    _ts["acc"] += tok
                    s = _ts["acc"]
                    parts = []
                    tm = _re.search(r"<think>(.*?)(?:</think>|$)", s, _re.DOTALL)
                    if tm and tm.group(1).strip():
                        parts.append(tm.group(1).strip())
                    # JSON "thought":"…" value (handles escaped quotes; open-ended
                    # while still streaming).
                    jm = _re.search(r'"thought"\s*:\s*"((?:[^"\\]|\\.)*)', s)
                    if jm and jm.group(1).strip():
                        parts.append(jm.group(1).strip())
                    reasoning = "\n".join(parts).strip()
                    if reasoning and len(reasoning) - _ts["emitted"] >= 24:
                        _ts["emitted"] = len(reasoning)
                        await emit_event({"type": "agent_loop_v4.think_delta",
                                          "stream_id": stream_id, "cycle": _cyc,
                                          "text": reasoning[:4000], "session_id": sid})
                except Exception:
                    pass

            try:
                raw = await _safe_ollama_generate_dw(
                    user_msg, system=system_prompt, model=model, instance_id=instance_id,
                    prefer_gpu=bool(prefer_gpu), json_mode=True,
                    stream_cb=(_think_stream_cb if stream_id else None))
            except Exception as e:
                history.append({"tool": "(planner_error)", "args": {}, "ok": False,
                                "preview": f"Planner LLM failed: {e}"})
                await emit_event({"type": "agent_loop_v4.tool_done", "stream_id": stream_id,
                                  "cycle": cycles, "tool": "(planner)", "ok": False,
                                  "elapsed_ms": 0, "preview": f"Planner LLM failed: {e}"[:400],
                                  "error": str(e)[:500], "session_id": sid})
                continue

            _raw_clean, _think_text = _strip_think(raw or "")
            if _think_text:
                await emit_event({"type": "agent_loop_v4.think", "stream_id": stream_id,
                                  "cycle": cycles, "thought": _think_text[:2000], "session_id": sid})
            if stream_append and stream_id:
                try:
                    if _think_text:
                        await stream_append(stream_id, f"\n[think #{cycles}] {_think_text[:400]}\n")
                    await stream_append(stream_id, f"\n[plan #{cycles}] {_raw_clean[:600]}\n")
                except Exception:
                    pass

            messages.append({"role": "assistant", "content": _raw_clean.strip()[:4000]})

            raw_action = _extract_json(_raw_clean)
            action = _canonicalise_tool_use_payload(raw_action) if raw_action else None
            if not isinstance(action, dict):
                # Last-resort regex salvage (truncated / unescaped-quote JSON)
                # before giving up the cycle.
                _salv = _salvage_action(raw or _raw_clean)
                action = _canonicalise_tool_use_payload(_salv) if _salv else None
                if isinstance(action, dict):
                    await emit_event({"type": "agent_loop_v4.args_coerced", "stream_id": stream_id,
                                      "cycle": cycles, "tool": "(planner)",
                                      "notes": ["recovered an action from malformed/partial JSON"],
                                      "session_id": sid})
            if not isinstance(action, dict):
                # Surface WHAT failed — show the FULL output (don't truncate the
                # error, that's what made the cause unreadable). Distinguish the
                # common cases so the fix is obvious.
                _snippet = (_raw_clean or raw or "").strip()
                _echoed = ("[Observation" in _snippet or "[tool_result" in _snippet
                           or _snippet.startswith("REMINDER:"))
                if not _snippet:
                    _detail = ("Planner returned an EMPTY response (no JSON, no text) — "
                               "likely an Ollama json_mode/timeout issue, not bad formatting.")
                elif _echoed:
                    _detail = ("Planner echoed the observation/prompt text instead of emitting an "
                               "action. Full output:\n" + _snippet)
                else:
                    _detail = "Could not parse a JSON action. Full planner output:\n" + _snippet
                history.append({"tool": "(parse_error)", "args": {}, "ok": False,
                                "preview": _detail})
                await emit_event({"type": "agent_loop_v4.tool_done", "stream_id": stream_id,
                                  "cycle": cycles, "tool": "(planner)", "ok": False,
                                  "elapsed_ms": 0, "preview": _detail[:6000],
                                  "error": _detail[:6000], "session_id": sid})
                if _echoed:
                    _fix = ("[system] Do NOT restate the observations or results — they are context, "
                            "not your output. Reply with ONLY one compact JSON object and nothing "
                            'else: {"thought":"<one short sentence>","tool_use":{"name":"<cap>","input":{...}}} '
                            'or {"thought":"...","final":"..."}.')
                else:
                    _fix = ("[system] Your previous response was not valid JSON (it may have been cut "
                            "off — keep `thought` to ONE short sentence). Reply with ONLY one compact "
                            'JSON object: {"thought":"...","tool_use":{"name":"<cap.name>","input":{...}}} '
                            'or {"thought":"...","final":"..."}.')
                messages.append({"role": "user", "content": _fix})
                continue

            # Thought lives OUT of the action JSON (in <think> or as prose before
            # it), so a long rationale can never truncate the action. Prefer an
            # inline "thought" if the model included one, else the <think> text,
            # else the prose preceding the JSON object.
            thought = (action.get("thought") or "").strip() or (_think_text or "").strip()
            if not thought and "{" in _raw_clean:
                _pre = _raw_clean.split("{", 1)[0].strip()
                # Ignore echoed observation/prompt text masquerading as prose.
                if _pre and not _pre.startswith(("[Observation", "[tool_result", "REMINDER:")):
                    thought = _pre[:2000]

            # Finalize the (possibly live-streamed) think block with the full
            # thought — covers reasoning carried in the JSON "thought" field,
            # which the early <think>-only emit above misses.
            if thought and not _think_text:
                await emit_event({"type": "agent_loop_v4.think", "stream_id": stream_id,
                                  "cycle": cycles, "thought": thought[:2000], "session_id": sid})

            # ── Agent-driven stage control ───────────────────────────────────
            # The LLM may judge a stage's goal already met and override the rigid
            # gates: "explore_done" lets it act without the minimum explore quota;
            # "verified" lets it finish without a forced read-only re-check. This
            # is what lets the agent order/skip stages per its own judgement.
            _explore_override = bool(action.get("explore_done") or action.get("skip_explore")
                                     or action.get("ready_to_act"))
            _self_verified    = bool(action.get("verified") or action.get("verify_done"))
            if _self_verified and acted and not validated:
                validated = True
                await _emit_phase("verify", reason="self_verified", validated=True)

            # ── Thinking-only turn ───────────────────────────────────────────
            # The model reasoned but emitted no action (e.g. {"thought":"…"}).
            # That's a valid thinking step, NOT a parse error: record it and ask
            # for the action instead of burning the turn.
            _has_action = any(action.get(k) for k in
                              ("tool_use", "tool_call", "final", "todo_done",
                               "action", "expand_tools", "tool", "capability"))
            if not _has_action:
                history.append({"tool": "(thinking)", "args": {}, "ok": True,
                                "preview": (thought or "(no action emitted)")[:400]})
                # Short marker only — the full reasoning is already in the think
                # block (streamed + finalized), so don't duplicate it in a result.
                await emit_event({"type": "agent_loop_v4.tool_done", "stream_id": stream_id,
                                  "cycle": cycles, "tool": "(thinking)", "ok": True,
                                  "elapsed_ms": 0, "preview": "reasoning step — no action taken",
                                  "session_id": sid})
                cur_t = next((t for t in todos if not t.get("done")), None)
                _nudge = ("[system] That was reasoning only — no action was taken. Now emit ONE "
                          'JSON action: {"tool_use":{"name":"<cap>","input":{…}}}, '
                          '{"todo_done":<id>}, or {"final":"…"}.')
                if cur_t:
                    _nudge += f' Work todo #{cur_t["id"]}: {cur_t["task"]}'
                messages.append({"role": "user", "content": _nudge})
                continue

            # ── Explicit todo completion ────────────────────────────────────
            if action.get("todo_done") is not None and not action.get("final"):
                try:
                    tid = int(action.get("todo_done"))
                except Exception:
                    tid = None
                marked = None
                for t in todos:
                    if t.get("id") == tid and not t.get("done"):
                        t["done"] = True
                        marked = t
                        break
                if marked:
                    await _emit_plan()
                    history.append({"tool": "(todo_done)", "args": {"id": tid}, "ok": True,
                                    "preview": f"Marked todo #{tid} done: {marked['task']}"})
                    messages.append({"role": "user",
                                     "content": f"[system] Todo #{tid} marked done. Continue with the next item."})
                else:
                    messages.append({"role": "user",
                                     "content": f"[system] Could not mark todo #{tid} (unknown or already done)."})
                continue

            # ── Final answer + strict completion gate ───────────────────────
            if action.get("final"):
                # Soft verify gate (per the v3 validation pattern)
                if gate_verify and acted and not validated and completion_bounced == 0:
                    completion_bounced += 1
                    await _emit_phase("verify", reason="verify_required")
                    history.append({"tool": "(verify_required)", "args": {}, "ok": False,
                                    "preview": "Before finishing: verify your last action with a "
                                    "read-only check, then emit final."})
                    messages.append({"role": "user",
                                     "content": "[system] You acted but did not verify. Run ONE "
                                     "read-only verification (re-read / re-query / re-grep), then emit final."})
                    continue
                # Strict completion check
                if strict_complete:
                    chk = await _v4_completion_check(
                        goal, history, todos, model=model,
                        instance_id=instance_id, prefer_gpu=prefer_gpu)
                    await emit_event({"type": "agent_loop_v4.completion_check",
                                      "stream_id": stream_id, "cycle": cycles,
                                      "passed": chk["passed"], "missing": chk["missing"],
                                      "session_id": sid})
                    if not chk["passed"] and completion_bounced < MAX_COMPLETION_BOUNCES:
                        completion_bounced += 1
                        history.append({"tool": "(completion_incomplete)", "args": {}, "ok": False,
                                        "preview": "Completion check failed: " + "; ".join(chk["missing"])})
                        messages.append({"role": "user",
                                         "content": "[system] Completion check FAILED. Resolve these before "
                                         "finishing: " + "; ".join(chk["missing"])
                                         + ". Complete/verify the open items, then emit final."})
                        continue
                    if not chk["passed"]:
                        final = (str(action["final"])
                                 + "\n\n[verification incomplete: " + "; ".join(chk["missing"]) + "]")
                        done = True
                        await emit_event({"type": "agent_loop_v4.done", "stream_id": stream_id,
                                          "cycles": cycles, "summary": final,
                                          "reason": "final_unverified", "session_id": sid})
                        break
                # Accepted — the goal is satisfied; mark any open todos done so the
                # UI plan reflects completion.
                if todos and any(not t.get("done") for t in todos):
                    for t in todos:
                        t["done"] = True
                    await _emit_plan()
                final = str(action["final"])
                done = True
                await emit_event({"type": "agent_loop_v4.done", "stream_id": stream_id,
                                  "cycles": cycles, "summary": final, "reason": "final",
                                  "session_id": sid})
                break

            # ── Toolkit expand (condensed; same quota semantics as v3) ───────
            if action.get("action") == "expand_tools" or action.get("expand_tools"):
                if (not enable_expand or expand_count >= MAX_EXPANDS
                        or _have_useful_caps(toolkit, triage.get("category", "other"))):
                    history.append({"tool": "(expand_blocked)", "args": {}, "ok": False,
                                    "preview": "Expand unavailable — use the existing toolkit or emit final."})
                    messages.append({"role": "user",
                                     "content": "[system] Toolkit expansion is unavailable or unnecessary. "
                                     "Pick a tool from your toolkit or emit final."})
                    continue
                expand_count += 1
                def _norm_kw(v):
                    if isinstance(v, list):
                        return " ".join(str(x).strip() for x in v if str(x).strip())
                    if isinstance(v, dict):
                        return " ".join(str(x).strip() for x in v.values() if str(x).strip())
                    return v.strip() if isinstance(v, str) else ""
                kws = _norm_kw(action.get("keywords")) or _norm_kw(action.get("expand_tools")) or ""
                added: List[str] = []
                if kws and ds and getattr(ds, "CAP_INDEX", None):
                    try:
                        hits = await ds.CAP_INDEX.relevance_search(kws, top_k=8)
                        for n, _s in hits:
                            if n in CAPABILITY_REGISTRY and n not in toolkit:
                                toolkit.append(n); added.append(n)
                                if len(added) >= 5:
                                    break
                    except Exception:
                        pass
                _rebuild_prompt()
                history.append({"tool": "(expand_tools)", "args": {"keywords": kws}, "ok": True,
                                "preview": f"Toolkit expanded by {len(added)}: {added}"})
                await emit_event({"type": "agent_loop_v4.toolkit", "stream_id": stream_id,
                                  "toolkit": list(toolkit), "added": added, "session_id": sid})
                await emit_event({"type": "agent_loop_v4.tool_done", "stream_id": stream_id,
                                  "cycle": cycles, "tool": "expand_tools", "ok": True,
                                  "elapsed_ms": 0,
                                  "preview": f"Expanded: +{len(added)} caps", "session_id": sid})
                continue

            # ── tool_use ─────────────────────────────────────────────────────
            tu = action.get("tool_use") or action.get("tool_call") or {}
            if not isinstance(tu, dict):
                tu = {}
            tool = (tu.get("name") or action.get("tool") or action.get("capability") or "").strip()
            args = tu.get("input") or action.get("args") or action.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}

            if not tool:
                history.append({"tool": "(none)", "args": args, "ok": False,
                                "preview": "Action did not include tool_use.name or final"})
                messages.append({"role": "user",
                                 "content": "[system] Your previous response had no tool name. "
                                 'Pick a tool or emit {"thought":"...","final":"..."}.'})
                continue

            if tool not in toolkit and tool not in CAPABILITY_REGISTRY:
                history.append({"tool": tool, "args": args, "ok": False,
                                "preview": f"ERROR: '{tool}' not in toolkit. Visible: {', '.join(toolkit[:10])}"})
                await emit_event({"type": "agent_loop_v4.tool_done", "stream_id": stream_id,
                                  "cycle": cycles, "tool": tool, "ok": False, "elapsed_ms": 0,
                                  "preview": f"Unknown tool: {tool}", "error": f"Unknown tool: {tool}",
                                  "session_id": sid})
                continue

            if _detect_repetition(history, tool, args, lookback=4, threshold=2):
                msg = (f"REPETITION DETECTED: '{tool}' called with identical args repeatedly. "
                       "Try a different tool/args or emit final.")
                history.append({"tool": "(repetition_block)", "args": {"tool": tool, "args": args},
                                "ok": False, "preview": msg})
                messages.append({"role": "user", "content": "[system] " + msg})
                await emit_event({"type": "agent_loop_v4.repetition_block", "stream_id": stream_id,
                                  "cycle": cycles, "tool": tool, "args": args, "session_id": sid})
                continue

            # ── Phase gate: explore before act (command-aware) ───────────────
            _forced_hitl_done = False
            _tool_is_long = _lr(tool, args)
            # Agent override: if it declares exploration done, satisfy the gate so
            # it can act now (it ordered the stages itself).
            if _explore_override and gate_explore and not _explore_satisfied():
                explore_done = max(explore_done, max(1, min_explore_cycles))
                await _emit_phase("act", reason="explore_overridden", tool=tool)
            if gate_explore and _v4_phase(tool, args) == "act" and not _explore_satisfied():
                await _emit_phase("explore", reason="act_blocked", tool=tool)
                _blk = (f"BLOCKED: '{tool}' is an action tool but you have explored only "
                        f"{explore_done}/{max(1,min_explore_cycles)} times. Use a read-only "
                        "call (grep/ls/get/list/query) first.")
                history.append({"tool": "(act_blocked)", "args": {"tool": tool}, "ok": False,
                                "preview": _blk})
                messages.append({"role": "user", "content": "[system] " + _blk})
                await emit_event({"type": "agent_loop_v4.tool_done", "stream_id": stream_id,
                                  "cycle": cycles, "tool": tool, "ok": False, "elapsed_ms": 0,
                                  "preview": _blk[:400], "error": "act blocked pre-explore",
                                  "session_id": sid})
                continue

            # ── Plan-progress guard: don't re-run discovery/listing once a plan
            #    item is pending and exploration is satisfied. Re-listing the same
            #    datasets/caps instead of executing the todo is the classic stall. ─
            if (cur is not None and _explore_satisfied() and tool in _V4_DISCOVERY_CAPS
                    and any(h.get("ok") and h.get("tool") == tool for h in history)):
                _dblk = (f"BLOCKED: '{tool}' is a discovery/listing call you already ran. "
                         f"Execute todo #{cur['id']} now instead: {cur['task']}")
                history.append({"tool": "(discovery_blocked)", "args": {"tool": tool}, "ok": False,
                                "preview": _dblk})
                messages.append({"role": "user", "content": "[system] " + _dblk})
                await emit_event({"type": "agent_loop_v4.tool_done", "stream_id": stream_id,
                                  "cycle": cycles, "tool": tool, "ok": False, "elapsed_ms": 0,
                                  "preview": _dblk[:400], "error": "discovery repeat blocked",
                                  "session_id": sid})
                continue

            # ── Long-running HITL (optional) — only for genuinely long commands,
            #    and skipped entirely when global require_approval handles it. ──
            if (not require_approval and long_running_force_hitl and _tool_is_long):
                await emit_event({"type": "agent_loop_v4.hitl_request", "stream_id": stream_id,
                                  "cycle": cycles, "step": cycles - 1, "tool": tool, "args": args,
                                  "thought": thought, "reason": "long_running",
                                  "session_id": sid, "timeout_secs": hitl_timeout_secs})
                decision_obj = await _await_hitl_decision(sid, cycles - 1, timeout=float(hitl_timeout_secs))
                decision = (decision_obj or {}).get("decision", "")
                if decision == "abort":
                    final = "Aborted by user before a long-running tool ran."
                    done = True
                    await emit_event({"type": "agent_loop_v4.done", "stream_id": stream_id,
                                      "cycles": cycles, "summary": final, "reason": "hitl_abort",
                                      "session_id": sid})
                    break
                if decision == "reject":
                    history.append({"tool": tool, "args": args, "ok": False,
                                    "preview": "HITL: user declined this long-running call. "
                                    + (decision_obj.get("comment") or "")})
                    await emit_event({"type": "agent_loop_v4.hitl_resolved", "stream_id": stream_id,
                                      "cycle": cycles, "decision": "reject", "session_id": sid})
                    continue
                if decision == "edit":
                    new_args = (decision_obj or {}).get("args") or {}
                    if isinstance(new_args, dict):
                        args = new_args
                await emit_event({"type": "agent_loop_v4.hitl_resolved", "stream_id": stream_id,
                                  "cycle": cycles, "decision": decision or "auto_approve_timeout",
                                  "session_id": sid})
                _forced_hitl_done = True

            # ── HITL pause (global require_approval) ─────────────────────────
            if require_approval and not _forced_hitl_done:
                await emit_event({"type": "agent_loop_v4.hitl_request", "stream_id": stream_id,
                                  "cycle": cycles, "step": cycles - 1, "tool": tool, "args": args,
                                  "thought": thought, "session_id": sid, "timeout_secs": hitl_timeout_secs})
                decision_obj = await _await_hitl_decision(sid, cycles - 1, timeout=float(hitl_timeout_secs))
                decision = decision_obj.get("decision", "")
                if decision == "abort":
                    final = "Aborted by user during HITL approval."
                    done = True
                    await emit_event({"type": "agent_loop_v4.done", "stream_id": stream_id,
                                      "cycles": cycles, "summary": final, "reason": "hitl_abort",
                                      "session_id": sid})
                    break
                if decision == "reject":
                    history.append({"tool": tool, "args": args, "ok": False,
                                    "preview": "HITL: user rejected this step. "
                                    + (decision_obj.get("comment") or "")})
                    await emit_event({"type": "agent_loop_v4.hitl_resolved", "stream_id": stream_id,
                                      "cycle": cycles, "decision": "reject", "session_id": sid})
                    continue
                if decision == "edit":
                    new_args = decision_obj.get("args") or {}
                    if isinstance(new_args, dict):
                        args = new_args
                    await emit_event({"type": "agent_loop_v4.hitl_resolved", "stream_id": stream_id,
                                      "cycle": cycles, "decision": "edit", "args": args, "session_id": sid})
                elif decision == "approve":
                    await emit_event({"type": "agent_loop_v4.hitl_resolved", "stream_id": stream_id,
                                      "cycle": cycles, "decision": "approve", "session_id": sid})
                else:
                    await emit_event({"type": "agent_loop_v4.hitl_resolved", "stream_id": stream_id,
                                      "cycle": cycles, "decision": "auto_approve_timeout", "session_id": sid})

            # ── Arg coercion ─────────────────────────────────────────────────
            coerced_args, coerce_notes = _coerce_args(tool, args)
            if coerce_notes:
                args = coerced_args
                await emit_event({"type": "agent_loop_v4.args_coerced", "stream_id": stream_id,
                                  "cycle": cycles, "tool": tool, "notes": coerce_notes, "session_id": sid})

            # ── Search quota ────────────────────────────────────────────────
            if tool in SEARCH_CAPS:
                search_count += 1
                if search_count > MAX_SEARCH_CALLS:
                    history.append({"tool": "(search_quota_exceeded)", "args": {"tool": tool},
                                    "ok": False, "preview": f"Search quota exhausted ({MAX_SEARCH_CALLS})."})
                    messages.append({"role": "user",
                                     "content": "[system] Search quota exhausted. Pick a real tool or emit final."})
                    continue

            # Default the local exec working dir to the artifact dir so generated
            # files land in the sandbox-configured, UI-browsable location. (Not
            # exec.ssh.run — that's remote.)
            if (artifact_dir_path and tool in ("exec.bash.run", "exec.ps.run", "exec.code.run")
                    and isinstance(args, dict) and not str(args.get("cwd") or "").strip()):
                args["cwd"] = artifact_dir_path

            # ── Execute ──────────────────────────────────────────────────────
            productive_cycles += 1
            await emit_event({"type": "agent_loop_v4.tool_call", "stream_id": stream_id,
                              "cycle": cycles, "tool": tool, "args": args, "thought": thought,
                              "long_running": _tool_is_long,
                              "phase": _v4_phase(tool, args),
                              "will_await": await_long_running and _should_await(tool),
                              "session_id": sid})

            t0 = time.monotonic()
            invoke = await _agent_loop_call_tool(tool, args, session_id=sid, trace_id=trace_id or "")
            if invoke.get("ok") and isinstance(invoke.get("result"), dict):
                rerr = invoke["result"].get("error")
                if rerr:
                    invoke["ok"] = False
                    invoke["error"] = str(rerr)

            if (not invoke.get("ok") and max_recovery_attempts > 0
                    and _is_arg_error(invoke.get("error", ""))):
                recovery_result = await _attempt_arg_recovery(
                    cap_name=tool, failed_args=args, error_text=invoke.get("error", ""),
                    model=model, instance_id=instance_id, prefer_gpu=prefer_gpu,
                    max_attempts=int(max_recovery_attempts), call_tool=_agent_loop_call_tool,
                    session_id=sid, trace_id=trace_id or "", emit_fn=emit_event,
                    cycle=cycles, stream_id=stream_id,
                    goal=goal, thought=thought)
                if recovery_result.get("recovered"):
                    invoke = recovery_result["final_invoke"]
                    last_a = recovery_result.get("attempts") or []
                    if last_a:
                        args = last_a[-1].get("args", args)

            if (invoke.get("ok") and await_long_running
                    and isinstance(invoke.get("result"), dict)):
                immediate = invoke["result"]
                job_id_detected = _detect_job_id(immediate)
                if job_id_detected:
                    awaited = await _universal_await_job(
                        cap_name=tool, immediate=immediate, session_id=sid,
                        trace_id=trace_id or "", cycle=cycles,
                        max_wait_secs=float(long_running_timeout_secs), stream_id=stream_id)
                    invoke["result"] = awaited
                    if isinstance(awaited, dict) and awaited.get("_await_error"):
                        invoke["ok"] = False
                        invoke["error"] = awaited["_await_error"]
                    elif isinstance(awaited, dict) and awaited.get("error"):
                        invoke["ok"] = False
                        invoke["error"] = str(awaited["error"])

            elapsed = round((time.monotonic() - t0) * 1000)

            empty_search = False
            if invoke.get("ok") and isinstance(invoke.get("result"), dict) \
                    and tool in ("caps.search", "context.search_caps", "context.search_dags"):
                rd = invoke["result"]
                if (rd.get("count") or len(rd.get("results") or [])
                        or len(rd.get("hits") or []) or len(rd.get("caps") or [])) == 0:
                    empty_search = True

            if invoke.get("ok"):
                preview = _result_preview(invoke["result"])
                if empty_search:
                    preview = "WARNING: search returned 0 results. Use the existing toolkit or emit final.\n\n" + preview
            else:
                preview = "ERROR: " + invoke.get("error", "unknown error")
                if coerce_notes:
                    preview += "\n\nNote: args auto-coerced: " + "; ".join(coerce_notes[:4])

            history.append({"tool": tool, "args": args, "ok": bool(invoke.get("ok")),
                            "preview": preview, "ms": elapsed, "thought": thought,
                            "coerce_notes": coerce_notes or None, "empty_search": empty_search})

            # ── Phase accounting (command-aware) ─────────────────────────────
            if invoke.get("ok") and not empty_search:
                if _v4_phase(tool, args) == "explore":
                    explore_done += 1
                    if acted and not validated:
                        validated = True
                        await _emit_phase("verify", validated=True, tool=tool)
                    else:
                        await _emit_phase("explore", tool=tool)
                else:
                    acted = True
                    validated = False  # a fresh action needs a fresh verification
                    await _emit_phase("act", tool=tool)

            messages.append({"role": "user", "content": f"[tool_result {tool}]\n{preview[:1200]}"})

            if stream_append and stream_id:
                try:
                    await stream_append(stream_id,
                                        f"\n[exec #{cycles}] {tool}({json.dumps(args, default=str)[:200]}) → {preview[:400]}\n")
                except Exception:
                    pass

            await emit_event({"type": "agent_loop_v4.tool_done", "stream_id": stream_id,
                              "cycle": cycles, "tool": tool, "ok": invoke.get("ok"),
                              "elapsed_ms": elapsed, "preview": preview[:2000],
                              "error": invoke.get("error", "") if not invoke.get("ok") else "",
                              "empty_search": empty_search, "session_id": sid})

            # Auto-advance the plan on real forward progress so it actually moves
            # (and the completion gate can eventually clear) without relying on the
            # model to always emit todo_done. Progress = a verified action, OR any
            # successful, non-repeat, non-discovery tool call once exploration is
            # satisfied. Discovery/listing calls never advance a todo.
            made_progress = (
                invoke.get("ok") and not empty_search
                and not str(tool).startswith("(")
                and tool not in _V4_DISCOVERY_CAPS
                and _explore_satisfied()
            )
            if todos and (validated or made_progress):
                m = _mark_next_todo_done()
                if m:
                    await _emit_plan()

            # ── Single-action fast-path ──────────────────────────────────────
            # The agent decided this goal needs no planning or exploration, and
            # the very first real (non-discovery) tool call just succeeded with a
            # usable result. That result IS the answer — return it now instead of
            # forcing a verify/re-check cycle. We still ask the judge for a clean
            # one-line summary, but (unlike the generic check) we DON'T require it
            # to be convinced — the flaky judge must not strand a done task.
            informative = (invoke.get("ok") and not empty_search
                           and tool not in SEARCH_CAPS
                           and not str(tool).startswith("("))
            if single_action and informative and not todos:
                summary = ""
                if _check_goal_satisfied:
                    try:
                        sat = await _check_goal_satisfied(goal, preview, model=model,
                                                          instance_id=instance_id, prefer_gpu=prefer_gpu)
                        summary = (sat or {}).get("summary") or ""
                    except Exception:
                        summary = ""
                final = summary or f"Done via {tool}.\n\n{preview[:600]}"
                done = True
                await emit_event({"type": "agent_loop_v4.done", "stream_id": stream_id,
                                  "cycles": cycles, "summary": final,
                                  "reason": "single_action", "session_id": sid})
                break

            # ── Satisfaction check (still gated by strict completion on final) ─
            if satisfaction_check and invoke.get("ok") and _check_goal_satisfied and not todos:
                try:
                    sat = await _check_goal_satisfied(goal, preview, model=model,
                                                      instance_id=instance_id, prefer_gpu=prefer_gpu)
                except Exception:
                    sat = {"satisfied": False, "summary": ""}
                # A read-only/informational goal that never `acted` has nothing to
                # verify — don't let the verify gate block its acceptance.
                if sat.get("satisfied") and (not gate_verify or validated or not acted):
                    final = sat.get("summary") or "Goal satisfied."
                    done = True
                    await emit_event({"type": "agent_loop_v4.done", "stream_id": stream_id,
                                      "cycles": cycles, "summary": final,
                                      "reason": "satisfaction_check", "session_id": sid})
                    break

    finally:
        if stream_complete and stream_id:
            try:
                await stream_complete(stream_id)
            except Exception:
                pass

    if not done and not final and history:
        ok_steps = [h for h in history if h.get("ok") and not h.get("tool", "").startswith("(")]
        if ok_steps:
            final = (f"Budget exhausted ({cycles} cycles) before final. Last results: "
                     + " | ".join(h.get("preview", "")[:200] for h in ok_steps[-3:]))
        else:
            final = f"Budget exhausted ({cycles} cycles) — no successful tool calls."
        done = True
        await emit_event({"type": "agent_loop_v4.done", "stream_id": stream_id, "cycles": cycles,
                          "summary": final, "reason": "budget_exhausted", "session_id": sid})

    handover_output = ""
    if handover and history:
        try:
            ho = await _run_handover_stage(
                goal=goal, history=history, triage=triage, cur_final=final,
                model=model, instance_id=instance_id, prefer_gpu=prefer_gpu,
                max_chars=int(handover_max_chars), session_id=sid)
            handover_output = ho or ""
            if handover_output:
                final = handover_output
        except Exception as e:
            log.debug("v4 handover stage failed: %s", e)

    return {
        "goal": goal, "triage": triage, "toolkit": toolkit,
        "relevant_datasets": [d.get("dataset_id") for d in relevant_datasets],
        "history": history, "messages": messages, "cycles": cycles, "done": done,
        "summary": final, "final": final, "handover_output": handover_output,
        "stream_id": stream_id, "session_id": sid, "phase": phase,
        "explore_done": explore_done, "validated": validated,
        "auto_continues": auto_continues, "steps": steps, "todos": todos,
    }


# ═════════════════════════════════════════════════════════════════════════════
# v5 AGENT LOOP — ORCHESTRATED SPECIALIST SUB-AGENTS
# ─────────────────────────────────────────────────────────────────────────────
# Unlike v1–v4 (one planner that sees the whole toolkit), v5 splits the work:
#   • The ORCHESTRATOR sees only cap NAME+DESCRIPTION (a brief catalog) and a
#     list of available SKILLS, and emits an ordered step plan in ONE LLM call
#     (no separate triage→step-select→plan round-trips → fast start).
#   • Each STEP runs an EPHEMERAL SCOPED SUB-AGENT that sees only its slice:
#     full schemas for just that step's caps, any dynamically-loaded skills, and
#     a curated context slice (the outputs of the prior steps it depends on).
# Events are namespaced agent_loop_v5.* and reuse the shared renderer: the
# suffix-matched ones (.triage_done/.toolkit/.cycle_planning/.tool_call/
# .tool_done/.think/.done/.phase) render natively; v5-only events (.plan with a
# step shape, .step_start/.step_done/.replan) get dedicated handlers.
# ═════════════════════════════════════════════════════════════════════════════

_V5_CATALOG_MAX_DEFAULT = 40      # caps shown to the orchestrator (name+desc)

# Action primitives ALWAYS seeded into the v5 catalog so the orchestrator always
# has a real way to *do* things: run a command, persist/read a file, hit a URL.
# Without this, a goal like "get the current bash user" semantically matches read
# caps with "get" in the name and the orchestrator wrongly delegates the action to
# a generative cap; and "create a script" produces no file to iterate on because
# no file-write cap was offered.
_V5_ESSENTIAL_ACTION_CAPS = ("exec.bash.run", "ide.fs.write", "ide.fs.read", "http.get")

# Pre-plan recon + one-level sub-plan bounds.
_V5_RECON_MAX = 3                  # max read-only recon actions run before planning
_V5_SUBPLAN_MAX_STEPS = 6          # max sub-steps a single "complex" step may expand into
_V5_RECOVERY_MAX = 10              # max recovery caps auto-granted to a failing step

# Optional v4-style per-step phase model. The orchestrator may give a step a
# `phases` subset; each phase runs as its OWN scoped ephemeral sub-agent in this
# canonical order. explore = read-only recon, think = reasoning with NO tools,
# act = do the work (full caps), verify = read-only check of the act result.
_V5_PHASES = ("explore", "think", "act", "verify")
_V5_PHASE_GUIDE = {
    "explore": ("PHASE: EXPLORE (recon). Gather the information the later phases need using "
                "ONLY read-only capabilities — do NOT write files, run commands, or modify "
                "anything. When you have enough context, emit `done` with a concise digest of "
                "what you found."),
    "think":   ("PHASE: THINK. You have NO tools this phase. Reason about the goal and the "
                "EXPLORE findings, then emit `done` with a concrete plan/decision/analysis. Do "
                "NOT claim to have performed any action — that happens in the ACT phase."),
    "act":     ("PHASE: ACT. Carry out the actual work using your capabilities, building on the "
                "prior phases' context. Emit `done` with the concrete result or the artifact you "
                "produced."),
    "verify":  ("PHASE: VERIFY. Check whether the ACT phase actually satisfied the step goal, "
                "using read-only capabilities to inspect the result if helpful. Emit `done` "
                "starting with 'PASS' if the goal is met, or 'FAIL: <what is wrong or missing>' "
                "otherwise."),
}

# Caps that only GENERATE/PROCESS text — they need a REAL model name (or none),
# never an invented one. Used to inject the cluster's model list into a step.
_V5_GENERATIVE_PREFIXES = ("llm.", "ollama.")
_V5_GENERATIVE_EXACT = {"agent.chat", "agent.chat_voice"}


def _v5_is_generative(tool: str) -> bool:
    """True for llm.*/ollama.*/agent.chat* — caps that only emit text."""
    return bool(tool) and (tool in _V5_GENERATIVE_EXACT
                           or any(tool.startswith(p) for p in _V5_GENERATIVE_PREFIXES))


# Read-only vs mutating verbs — gate pre-plan recon to SAFE caps only, and pick a
# step's recovery toolkit. Anti-verbs win (e.g. "fabric.objects.bucket_create").
_V5_READONLY_TOKENS = ("search", "list", "get", "read", "describe", "query",
                       "inspect", "status", "fetch", "find", "lookup", "discover",
                       "expand", "landscape")
_V5_MUTATING_TOKENS = ("write", "create", "delete", "update", "set", "remove",
                       "run", "exec", "send", "build", "train", "deploy", "apply",
                       "install", "start", "stop", "kill", "save", "put", "post",
                       "edit", "move", "rename", "drop", "generate", "synthesize")


def _v5_is_read_only(cap_name: str) -> bool:
    """Heuristic: a cap is recon-safe if its name implies reading, not mutating.
    `http.get` is explicitly allowed (HTTP GET); exec.*/ide.fs.write are not."""
    n = (cap_name or "").lower()
    if not n:
        return False
    if n == "http.get":
        return True
    last = n.rsplit(".", 1)[-1]
    if any(tok in last for tok in _V5_MUTATING_TOKENS):
        return False
    return any(tok in n for tok in _V5_READONLY_TOKENS)


# Markers that a "successful" fetch actually returned an unusable page (consent
# walls, bot checks, JS-required shells). Lets a step treat an ok-but-useless
# result as a SOFT failure and pivot to a different query/URL/cap instead of
# re-hammering the same call until the stuck-loop guard fires.
_V5_UNHELPFUL_MARKERS = (
    "consent.google.com", "before you continue", "enable javascript",
    "captcha", "are you a robot", "unusual traffic", "access denied",
    "verify you are human", "cookies to continue", "/sorry/index",
    "please enable cookies",
)


def _v5_looks_unhelpful(text: str) -> bool:
    """Cheap heuristic: did an ok result actually return a blocked/consent page?"""
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in _V5_UNHELPFUL_MARKERS)


async def _v5_available_models(trace_id: str = "", *, limit: int = 40) -> List[str]:
    """Distinct model names available across the Ollama cluster, so a specialist
    that uses llm.*/ollama.* caps picks a REAL model (or omits it) instead of
    inventing names like 'gpt-3.5-turbo'. Best-effort; returns [] if unknown."""
    names: List[str] = []
    seen: set = set()
    cap = CAPABILITY_REGISTRY.get("ollama.list_models")
    if cap:
        try:
            r = await cap["func"](trace_id=trace_id or "")
            if isinstance(r, dict):
                for node in r.values():
                    if not isinstance(node, dict):
                        continue
                    for m in (node.get("models") or []):
                        nm = m.get("name") if isinstance(m, dict) else str(m)
                        if nm and nm not in seen:
                            seen.add(nm); names.append(nm)
        except Exception as e:
            log.debug("v5 model list (ollama.list_models) failed: %s", e)
    if not names:
        cap = CAPABILITY_REGISTRY.get("ollama.instances")
        if cap:
            try:
                r = await cap["func"](trace_id=trace_id or "")
                if isinstance(r, dict):
                    for node in r.values():
                        for m in ((node or {}).get("models") or []):
                            nm = m if isinstance(m, str) else (m.get("name") if isinstance(m, dict) else "")
                            if nm and nm not in seen:
                                seen.add(nm); names.append(nm)
            except Exception as e:
                log.debug("v5 model list (ollama.instances) failed: %s", e)
    return names[:limit]


# Global default skill-injection blacklist (mirrors context._AGENT_LOOP_BLACKLIST
# for caps). Skills whose id is listed here are never offered to / injected by the
# loop, regardless of per-run allow lists. Starts empty — curate as needed.
_SKILL_INJECT_BLACKLIST: set = set()


def _v5_brief_cap_line(name: str) -> str:
    """One-line 'name — description' for the orchestrator catalog (no schema)."""
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap:
        return name
    desc = (cap.get("description") or "").strip().replace("\n", " ")[:120]
    return f"{name} — {desc}" if desc else name


def _v5_cap_skill_map(skills: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Build {cap_name: [skill_id, ...]} from each skill's applies_to_caps, so a
    step that uses a cap can be offered the skill(s) that teach it."""
    out: Dict[str, List[str]] = {}
    for s in skills:
        for c in (s.get("applies_to_caps") or []):
            out.setdefault(c, []).append(s["id"])
    return out


def _v5_apply_skill_suggestions(steps: List[Dict[str, Any]],
                                cap_skill_map: Dict[str, List[str]],
                                eligible_ids: set, enabled: bool = True) -> None:
    """Fallback auto-attach: for steps that list caps but NO skills, soft-merge the
    cap-suggested (and eligible) skills. The orchestrator's explicit skill choices
    are never overwritten — this only fills the gap when it picked none."""
    if not enabled:
        return
    for st in steps:
        if st.get("caps") and not st.get("skills"):
            sug: List[str] = []
            for c in st["caps"]:
                for sid_ in cap_skill_map.get(c, []):
                    if sid_ in eligible_ids and sid_ not in sug:
                        sug.append(sid_)
            if sug:
                st["skills"] = sug[:4]


async def _v5_list_skills(trace_id: str = "", *, allow: Optional[set] = None,
                          deny: Optional[set] = None) -> List[Dict[str, Any]]:
    """List ELIGIBLE skills for the orchestrator to pick from, with the metadata
    v5 needs (id/name/description/tags/applies_to_caps).

    Eligibility: skill is enabled AND not in the global blacklist AND not in the
    per-run `deny` set AND (per-run `allow` empty OR skill in `allow`)."""
    allow = allow or set()
    deny = deny or set()

    def _eligible(sid: str, rec: Dict[str, Any]) -> bool:
        if not sid or not rec.get("enabled", True):
            return False
        if sid in _SKILL_INJECT_BLACKLIST or sid in deny:
            return False
        if allow and sid not in allow:
            return False
        return True

    for cap_name in ("skills.list", "fabric.skills.list", "skills.registry", "skills.all"):
        cap = CAPABILITY_REGISTRY.get(cap_name)
        if not cap:
            continue
        try:
            r = await cap["func"](trace_id=trace_id or "")
            items = (r.get("skills") or r.get("items") or r.get("list") or []) if isinstance(r, dict) else []
            if isinstance(items, list) and items:
                out: List[Dict[str, Any]] = []
                for x in items:
                    if not isinstance(x, dict):
                        x = {"id": str(x), "name": str(x)}
                    sid = x.get("id") or x.get("name") or ""
                    if not _eligible(sid, x):
                        continue
                    out.append({"id": sid,
                                "name": x.get("name") or sid,
                                "description": (x.get("description") or "")[:160],
                                "tags": x.get("tags") or [],
                                "applies_to_caps": x.get("applies_to_caps") or []})
                return out[:60]
        except Exception:
            continue
    return []


async def _v5_orchestrate_plan(goal: str, catalog_names: List[str], skills: List[Dict[str, Any]],
                               cap_skill_map: Optional[Dict[str, List[str]]] = None,
                               *, model: str = "", instance_id: str = "",
                               prefer_gpu: bool = True, max_steps: int = 8,
                               recon_findings: str = "", master_plan: str = "") -> Dict[str, Any]:
    """ONE LLM call: decompose the goal into an ordered step plan. Each step names
    only the few caps and skills it needs. Folds triage+step-select+plan into a
    single call so the loop starts working almost immediately.

    Also returns `complexity` (simple|complex) and an optional `recon` list of
    READ-ONLY actions the orchestrator wants run BEFORE the plan is committed
    (only on the fast path when `recon_findings` is empty). When `recon_findings`
    is supplied, those findings are fed back in so the plan is informed by them.
    A step may set `complex: true` to be expanded into its own sub-plan.

    The catalog is annotated with each cap's SUGGESTED skills (from the skill↔cap
    map) — the orchestrator sees the suggestions and may keep, drop, or add to
    them. Skills with no cap link are still pickable from the skill catalog by
    their description."""
    cap_skill_map = cap_skill_map or {}
    cap_line_parts = []
    for n in catalog_names:
        line = "  " + _v5_brief_cap_line(n)
        sug = cap_skill_map.get(n) or []
        if sug:
            line += f"   [suggested skills: {', '.join(sug)}]"
        cap_line_parts.append(line)
    cap_lines = "\n".join(cap_line_parts) or "  (none)"
    skill_lines = "\n".join(
        f"  {s['id']} — {s['description']}"
        f"{(' (teaches: ' + ', '.join(s['applies_to_caps']) + ')') if s.get('applies_to_caps') else ''}"
        for s in skills) or "  (none)"
    sys = (
        "You are an ORCHESTRATOR. Turn the GOAL into a COMPLETE, ordered plan of steps. "
        "Each step is handed to a focused specialist sub-agent that can ONLY use the "
        "capabilities and skills you assign it.\n"
        "RIGHT-SIZE THE PLAN: use as many steps as the task genuinely needs — and no more. "
        "A trivial goal may be ONE step; a substantial goal (e.g. research + design + build + "
        "test + document) deserves a thorough multi-step plan with a SEPARATE step for each "
        "distinct unit of work. Do NOT under-plan: cramming unrelated work into one step is the "
        "main cause of failure — prefer more, well-scoped steps over fewer overloaded ones (up "
        "to " + str(max_steps) + "). But do NOT pad the plan with artifacts the user did not "
        "ask for — no UI panels, sensors, ML models, datasets, ontologies, or DAGs unless they "
        "were explicitly requested.\n"
        "INFORMATION FIRST: when a later step depends on something you don't yet know (facts to "
        "research, files to read, the environment/data to inspect), put the information-gathering "
        "step BEFORE the step that uses it and wire them with `needs`.\n"
        "For each step choose the capabilities (by exact name from the catalog) it needs, plus "
        "any skills that would help. Each cap lists its SUGGESTED skills — include them when "
        "relevant; you may drop a suggestion or add a different (e.g. conceptual) skill. Only "
        "assign a skill when the step genuinely needs that expertise.\n"
        "CRITICAL — generative vs action caps: capabilities under llm.*, ollama.*, and "
        "agent.chat/agent.chat_voice only GENERATE or PROCESS text. They CANNOT run "
        "commands, read/write files, query data, or call services. For any step that must "
        "DO something, assign a cap that actually performs it (e.g. exec.bash.run to run a "
        "shell command, http.get to fetch a URL, fabric.query to search data). NEVER use "
        "llm.* or agent.chat to 'execute', 'run', 'fetch', or 'retrieve' — they will just "
        "make up an answer. Example: 'get the current bash user' → a step with "
        "caps:[\"exec.bash.run\"] running `whoami`, NOT agent.chat.\n"
        "WRITING CODE / SCRIPTS / FILES / ARTIFACTS: generate the content with llm.generate and "
        "SAVE it to a FILE with ide.fs.write (into the artifact directory, so it PERSISTS and the "
        "user can read/edit/iterate on it later), then OPTIONALLY run it with "
        "exec.python.run(path=\"<artifact_dir>/<file>\") / exec.bash.run. For a SMALL file one "
        "step is fine; for a SUBSTANTIAL program use separate steps (e.g. implement → run/test → "
        "refine). To EDIT an existing file, use ide.fs.read then ide.fs.write the updated content "
        "(or sed -i via exec.bash.run). Do NOT just run throwaway code with no saved file — the "
        "user must end up with an artifact. Do NOT use llm.plan to 'plan a DAG' for a coding task "
        "(llm.plan builds a DAG WORKFLOW, only for when the user explicitly asks for a "
        "DAG/pipeline).\n"
        "COMPLEX STEPS: if a single step is itself a big sub-project with several parts, mark it "
        "\"complex\": true and give it a clear `goal` plus the caps the whole sub-project may use "
        "— it will be expanded into its OWN sub-plan by a sub-orchestrator.\n"
        "STEP PHASES (optional): a step may carry a `phases` list — any of "
        "\"explore\",\"think\",\"act\",\"verify\" — and EACH phase runs as its own scoped sub-agent "
        "(explore = read-only recon, think = reasoning with no tools, act = do the work, verify = "
        "read-only check of the result). Add phases to steps that benefit from gathering context "
        "first and/or validating the outcome (e.g. [\"explore\",\"act\",\"verify\"]); OMIT `phases` "
        "for simple, direct steps so they stay fast.\n"
        "RECON (optional, only when needed): if you cannot make a good plan without first "
        "inspecting the environment/data/web, put up to " + str(_V5_RECON_MAX) + " READ-ONLY "
        "actions in `recon` (e.g. caps.search, context.search_caps, fabric.query, http.get, "
        "ide.fs.read). They run BEFORE the plan is finalised and their results are fed back to "
        "you. For straightforward goals leave `recon` EMPTY and just produce the steps — do not "
        "slow the simple path down. Recon actions MUST be safe and read-only (never write, run, "
        "delete, or send).\n"
        "Set \"complexity\" to \"simple\", \"complex\", or \"extreme\" for the whole goal. Use "
        "\"extreme\" ONLY for very large, multi-domain, or research-heavy goals that warrant a "
        "strategic long-form plan before step breakdown (a specialist planner will draft one and "
        "hand it back to you). "
        "Use `needs` to list ids of EARLIER steps whose output a step depends on.\n"
        'Respond ONLY with JSON:\n'
        '{"complexity":"simple|complex|extreme","recon":[{"cap":"cap.name","args":{},"why":"<short>"}],'
        '"steps":[{"id":1,"title":"<short>","goal":"<what this step must achieve>",'
        '"caps":["cap.name"],"skills":["skill_id"],"needs":[],"complex":false,"phases":[]}],'
        '"reason":"<one sentence>"}'
    )
    prompt = (f"GOAL: {goal}\n\n"
              + (f"STRATEGIC MASTER PLAN (a specialist planner wrote this — BREAK IT INTO concrete, "
                 f"ordered, executable steps; keep its intent and sequencing):\n{master_plan}\n\n"
                 if master_plan else "")
              + (f"RECON FINDINGS (already gathered — use these to inform the plan):\n{recon_findings}\n\n"
                 if recon_findings else "")
              + f"AVAILABLE CAPABILITIES (name — description [suggested skills]):\n{cap_lines}\n\n"
              f"AVAILABLE SKILLS (id — description):\n{skill_lines}\n\nProduce the plan.")
    steps: List[Dict[str, Any]] = []
    reason = ""
    complexity = ""
    recon_actions: List[Dict[str, Any]] = []
    catalog_set = set(catalog_names)
    try:
        raw = await _safe_ollama_generate_dw(
            prompt, system=sys, model=model, instance_id=instance_id,
            prefer_gpu=prefer_gpu, json_mode=True)
        parsed = _extract_json(_strip_think(raw or "")[0]) or {}
        reason = str(parsed.get("reason") or "")
        complexity = str(parsed.get("complexity") or "").strip().lower()
        # Read-only recon actions — only honoured on the fast path (no findings yet).
        if not recon_findings:
            for ra in (parsed.get("recon") or [])[:_V5_RECON_MAX]:
                if not isinstance(ra, dict):
                    continue
                rc = str(ra.get("cap") or "").strip()
                if (rc in CAPABILITY_REGISTRY and rc in catalog_set
                        and _v5_is_read_only(rc)):
                    recon_actions.append({
                        "cap": rc,
                        "args": ra.get("args") if isinstance(ra.get("args"), dict) else {},
                        "why": str(ra.get("why") or "")[:160]})
        valid_skill_ids = {s["id"] for s in skills}
        for i, st in enumerate(parsed.get("steps") or []):
            if not isinstance(st, dict):
                continue
            caps = [c for c in (st.get("caps") or []) if isinstance(c, str) and c in CAPABILITY_REGISTRY][:8]
            sk = [s for s in (st.get("skills") or []) if isinstance(s, str) and s in valid_skill_ids][:4]
            needs = [n for n in (st.get("needs") or []) if isinstance(n, int)]
            phases = [p for p in _V5_PHASES if p in set(st.get("phases") or [])]
            steps.append({
                "id": i + 1,
                "title": str(st.get("title") or st.get("goal") or f"Step {i+1}")[:120],
                "goal": str(st.get("goal") or st.get("title") or goal)[:400],
                "caps": caps, "skills": sk, "needs": needs,
                "complex": bool(st.get("complex")), "phases": phases,
            })
    except Exception as e:
        log.debug("v5 orchestrate_plan failed: %s", e)
    return {"steps": steps[:max_steps], "reason": reason,
            "complexity": complexity, "recon": recon_actions}


async def _v5_run_recon(actions: List[Dict[str, Any]], *, session_id: str, stream_id: str,
                        trace_id: Any, call_tool) -> str:
    """Run the orchestrator's READ-ONLY recon actions before the plan is finalised,
    and return a compact findings digest to feed back into planning. Bounded and
    best-effort; only runs when the orchestrator actually requested recon (so the
    simple path stays a single LLM call with no tool calls)."""
    findings: List[str] = []
    for a in actions[:_V5_RECON_MAX]:
        cap = a.get("cap"); args = a.get("args") or {}
        await emit_event({"type": "agent_loop_v5.recon", "session_id": session_id,
                          "stream_id": stream_id, "cap": cap, "args": args,
                          "why": a.get("why", ""), "phase": "start"})
        ok = False
        try:
            inv = await call_tool(cap, args, session_id=session_id, trace_id=trace_id or "")
            ok = bool(inv.get("ok"))
            if ok and isinstance(inv.get("result"), dict) and inv["result"].get("error"):
                ok = False
                preview = "ERROR: " + str(inv["result"]["error"])
            else:
                preview = _result_preview(inv["result"]) if ok else ("ERROR: " + str(inv.get("error", "")))
        except Exception as e:
            preview = "ERROR: " + str(e)
        findings.append(f"[{cap}] {'ok' if ok else 'failed'}\n{preview[:700]}")
        await emit_event({"type": "agent_loop_v5.recon", "session_id": session_id,
                          "stream_id": stream_id, "cap": cap, "ok": ok,
                          "preview": preview[:600], "phase": "done"})
    return "\n\n".join(findings)


async def _v5_run_step(step: Dict[str, Any], *, goal: str, blackboard: Dict[int, Dict[str, Any]],
                       model: str, instance_id: str, prefer_gpu: bool,
                       session_id: str, stream_id: str, trace_id: Any,
                       cycle_budget: int, cycle_offset: int,
                       artifact_dir_path: str, call_tool, build_ctx,
                       catalog_caps: Optional[List[str]] = None,
                       available_models: Optional[List[str]] = None,
                       await_long_running: bool = True,
                       long_running_timeout_secs: int = 1800,
                       phase: str = "") -> Dict[str, Any]:
    """Run ONE step as an ephemeral scoped sub-agent. Sees only: full schemas for
    its assigned caps, dynamically-loaded skill instructions, and the outputs of
    the prior steps it depends on. Returns {id,title,ok,summary,outputs,cycle_end}.

    `cycle_offset` seeds a GLOBAL monotonic cycle counter so each step's cycle
    cards get distinct ids in the shared renderer (which keys cards by cycle).

    `phase` (explore/think/act/verify) runs the step as ONE phase of a v4-style
    cadence: it reframes the prompt and auto-scopes the caps (read-only for
    explore/verify, none for think, full for act). When `phase` is empty and the
    step itself carries a `phases` list, the step is delegated to
    `_v5_run_phased_step`, which runs each phase as its own scoped sub-agent."""
    sid = session_id
    step_id = step["id"]

    # Per-step phase cadence (opt-in, planner-chosen): hand each phase to its own
    # scoped sub-agent. Guarded by `not phase` so the phase sub-calls don't recurse.
    if not phase:
        valid_phases = [p for p in _V5_PHASES if p in set(step.get("phases") or [])]
        if valid_phases:
            return await _v5_run_phased_step(
                step, valid_phases, goal=goal, blackboard=blackboard, model=model,
                instance_id=instance_id, prefer_gpu=prefer_gpu, session_id=session_id,
                stream_id=stream_id, trace_id=trace_id, cycle_budget=cycle_budget,
                cycle_offset=cycle_offset, artifact_dir_path=artifact_dir_path,
                call_tool=call_tool, build_ctx=build_ctx, catalog_caps=catalog_caps,
                available_models=available_models, await_long_running=await_long_running,
                long_running_timeout_secs=long_running_timeout_secs)

    caps = list(dict.fromkeys(step.get("caps") or []))
    # Phase auto-scoping: explore/verify see only READ-ONLY caps; think has no
    # tools; act keeps the full set. If a read-only phase's step listed no
    # read-only caps, seed a few from the catalog so it can actually gather/check.
    if phase in ("explore", "verify"):
        caps = [c for c in caps if _v5_is_read_only(c)]
        if not caps and catalog_caps:
            caps = [c for c in catalog_caps if _v5_is_read_only(c)][:6]
    elif phase == "think":
        caps = []
    # `allowed` is the MUTABLE working scope: it starts as the orchestrator's
    # assigned caps but can be widened mid-step — on request (`need_caps`) or
    # automatically after a failure — so a specialist can self-correct instead
    # of being hard-blocked. Widening is bounded to the run's catalog.
    allowed = list(caps)
    catalog_set = set(catalog_caps or []) | set(caps)
    model_set = set(available_models or [])
    # Recovery toolkit a failing step may escalate to (read-only/search caps +
    # the essential action primitives, drawn only from the catalog). In a
    # read-only phase (explore/verify) the mutating essentials are withheld; the
    # think phase gets no recovery caps at all (it is tool-free by design).
    _essentials = () if phase in ("explore", "verify", "think") else _V5_ESSENTIAL_ACTION_CAPS
    recovery_caps = [] if phase == "think" else [
        c for c in (catalog_caps or [])
        if c not in allowed and (c in _essentials or _v5_is_read_only(c))][:_V5_RECOVERY_MAX]

    # Full schemas — for this step's caps. Newly granted caps are appended to
    # `dynamic_caps_block` so the specialist learns their schemas mid-step.
    sig_block = "\n".join(rich_cap_signature(c) for c in caps) \
        or "  (no caps assigned — reason about the step goal and report your findings)"
    dynamic_caps_block = ""

    # Dynamic skills — inject the chosen skills' instructions for this step only.
    skill_prompt = ""
    loaded_skills: List[str] = []
    if step.get("skills") and build_ctx:
        try:
            cobj = await build_ctx(step.get("goal") or goal, attach_skills=",".join(step["skills"]))
            skill_prompt = (cobj or {}).get("system_prompt", "") or ""
            loaded_skills = (cobj or {}).get("skills", []) or []
        except Exception as e:
            log.debug("v5 step %s skill load failed: %s", step_id, e)

    # Curated context slice — outputs of the steps this one depends on.
    needs = step.get("needs") or []
    rel = [blackboard[n] for n in needs if n in blackboard]
    if not rel:
        rel = list(blackboard.values())  # no explicit deps → all prior results
    ctx_slice = "\n\n".join(
        f"[from step {r['id']} · {r['title']}]\n{(r.get('summary') or '')[:800]}" for r in rel)

    await emit_event({"type": "agent_loop_v5.step_start", "session_id": sid, "stream_id": stream_id,
                      "step_id": step_id, "title": step["title"], "goal": step["goal"],
                      "caps": caps, "skills": loaded_skills, "phase": phase})

    # Model guidance — only when the step actually uses a generative cap, so the
    # specialist picks a REAL cluster model (or omits it) instead of inventing one.
    model_block = ""
    if model_set and any(_v5_is_generative(c) for c in caps):
        sample = ", ".join(list(model_set)[:20])
        model_block = ("\nAVAILABLE MODELS for any `model` argument (this is an Ollama cluster): "
                       + sample + ".\nOMIT the `model` argument to use the cluster default "
                       "(recommended). NEVER invent a model name such as 'gpt-3.5-turbo' or 'gpt-4' "
                       "— use a name from this list or omit it.\n")

    phase_guide = _V5_PHASE_GUIDE.get(phase, "")
    sys = (
        "You are a FOCUSED SPECIALIST sub-agent. Complete ONE step of a larger task and "
        "nothing else. Stay strictly within the step goal.\n"
        f"STEP GOAL: {step['goal']}\n\n"
        + ((phase_guide + "\n\n") if phase_guide else "")
        + "You may use these capabilities (full schemas):\n" + sig_block + "\n"
        + model_block
        + (("\nRELEVANT SKILLS (follow this guidance):\n" + skill_prompt + "\n") if skill_prompt else "")
        + (("\nCONTEXT FROM PRIOR STEPS:\n" + ctx_slice + "\n") if ctx_slice else "")
        + (("\nARTIFACT DIRECTORY for generated files: " + artifact_dir_path + "\n") if artifact_dir_path else "")
        + "\nWork in a tight loop. Each turn reply with ONE compact JSON object — ONE of:\n"
          '  {"thought":"<one sentence>","tool_use":{"name":"<cap>","input":{...}}}  to ACT, OR\n'
          '  {"thought":"<your reasoning>"}  to just THINK (no tool_use, no done) when you need to plan, '
          "are unsure, or are missing something — this is allowed and does NOT consume a cycle, OR\n"
          '  {"thought":"<why>","need_caps":["cap.name"]}  to REQUEST extra capabilities when your '
          "assigned ones are insufficient or a tool keeps failing/returning unusable results "
          "(granted if they exist in the broader toolkit; their schemas are then provided), OR\n"
          '  {"thought":"<one sentence>","done":"<concise result for the orchestrator>"}  when finished.\n'
          "Only emit a tool_use when you can fill in ALL of that cap's REQUIRED inputs — never call a cap "
          "with empty or placeholder args (e.g. llm.generate with no `prompt`). If you don't yet have an "
          "argument, THINK first (no tool_use) to work it out, then act.\n"
          "SELF-CORRECT: if a call FAILS or returns a USELESS result (an error, an empty body, or a "
          "consent/login/captcha/redirect page), do NOT repeat it with reworded args — try a DIFFERENT "
          "approach: a different URL/query, or request a different capability via need_caps. "
          "Use as many tool calls as the step genuinely needs; as soon as the goal is met, emit `done`."
    )

    history: List[Dict[str, Any]] = []
    outputs: Dict[str, Any] = {}
    all_thoughts: List[str] = []
    result_summary = ""
    ok = False
    had_useful = False          # at least one tool returned a genuinely usable result
    pending_note = ""           # one-shot note injected into the next user message
    gc = cycle_offset
    productive = 0
    # Thought-only turns accomplish nothing actionable, so they must NOT consume
    # the step's cycle budget (which limits real, productive cycles). A separate
    # `turns` cap stops a model that only ever "thinks" from looping forever.
    max_turns = max(2, max(1, cycle_budget) * 3)
    turns = 0
    think_cycle: Optional[int] = None     # current thinking-streak card id
    streak_thoughts: List[str] = []
    tool_calls: Dict[str, int] = {}       # per-tool call count (stuck-loop guard)
    _MAX_SAME_TOOL = 3                     # break the step after this many calls to one cap

    while productive < max(1, cycle_budget) and turns < max_turns:
        turns += 1
        obs = "\n\n".join(
            f"[result {i+1}] tool={h['tool']} ok={h['ok']}\n{h['preview']}"
            for i, h in enumerate(history[-4:])
        ) or "(no tool calls yet — make your first call or emit done)"
        _rep_tool = next((t for t, n in tool_calls.items() if n >= 2), "")
        _rep_hint = (f"\n\nNOTE: you have already called {_rep_tool} {tool_calls.get(_rep_tool,0)}× — "
                     "do NOT call it again with reworded args. Either try a DIFFERENT capability "
                     "(or request one via need_caps), or emit a `done` summary with what you have "
                     "now.") if _rep_tool else ""
        _grant_block = (f"\n\nNEWLY AVAILABLE CAPABILITIES (full schemas):\n{dynamic_caps_block}"
                        if dynamic_caps_block else "")
        _note = ("\n\n" + pending_note) if pending_note else ""
        pending_note = ""
        user_msg = (f"STEP GOAL: {step['goal']}\n\nYour results so far:\n{obs}{_rep_hint}{_note}{_grant_block}\n\n"
                    "Reply with ONE JSON action, or a `done` summary if the step goal is met.")

        raw = await _safe_ollama_generate_dw(
            user_msg, system=sys, model=model, instance_id=instance_id,
            prefer_gpu=prefer_gpu, json_mode=True)
        clean, think_text = _strip_think(raw or "")
        raw_obj = _extract_json(clean) or {}
        action = _canonicalise_tool_use_payload(raw_obj) or {}
        # `done`/`final`/`thought` are read from the RAW object: the shared
        # canonicaliser only preserves tool_use/final/todo_done and silently
        # drops v5's `done` key, which would strand a finished step.
        done_val = raw_obj.get("done") or raw_obj.get("final") or action.get("final")
        thought = (raw_obj.get("thought") or action.get("thought") or think_text or "").strip()
        if thought:
            all_thoughts.append(thought)

        # Step complete?
        if done_val:
            result_summary = str(done_val)[:1500]
            ok = True
            break

        tu = action.get("tool_use") or action.get("tool_call") or {}
        if not isinstance(tu, dict):
            tu = {}
        tool = (tu.get("name") or action.get("tool") or action.get("capability") or "").strip()
        args = tu.get("input") or action.get("args") or action.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}

        # ── Capability request — widen this step's scope on demand, bounded to
        #    the run's catalog. Lets an under-scoped/stuck specialist pivot
        #    instead of being hard-blocked (the plan is flexible on failure). ──
        req_caps = raw_obj.get("need_caps") or action.get("need_caps")
        if req_caps:
            if not isinstance(req_caps, list):
                req_caps = [req_caps]
            granted, denied = [], []
            for rc in req_caps:
                rc = str(rc).strip()
                if not rc or rc in allowed:
                    continue
                if rc in catalog_set and rc in CAPABILITY_REGISTRY:
                    allowed.append(rc); granted.append(rc)
                else:
                    denied.append(rc)
            if granted:
                new_sigs = "\n".join(rich_cap_signature(c) for c in granted)
                dynamic_caps_block = (dynamic_caps_block + "\n" + new_sigs) if dynamic_caps_block else new_sigs
                await emit_event({"type": "agent_loop_v5.scope_widened", "session_id": sid,
                                  "stream_id": stream_id, "step_id": step_id,
                                  "added": granted, "reason": "requested"})
            if granted or denied:
                pending_note = ("Scope updated. "
                                + (f"Now also available: {', '.join(granted)}. " if granted else "")
                                + (f"Not in the toolkit (denied): {', '.join(denied)}." if denied else ""))
            # A request-only turn (no tool/done) is the whole turn — loop again
            # so the specialist can act with its widened scope.
            if not tool:
                continue

        # ── Thought-only turn — NOT an error and NOT a real cycle. Join the
        #    reasoning into a single "thinking" card and continue without
        #    spending the budget. ─────────────────────────────────────────────
        if not tool:
            if thought:
                if think_cycle is None:
                    gc += 1
                    think_cycle = gc
                    streak_thoughts = []
                streak_thoughts.append(thought)
                await emit_event({"type": "agent_loop_v5.thinking", "stream_id": stream_id,
                                  "cycle": think_cycle, "step_id": step_id,
                                  "thought": "\n\n".join(streak_thoughts)[:4000],
                                  "session_id": sid})
            continue

        # ── Productive turn (a tool attempt) — opens a real cycle + budget. ───
        productive += 1
        gc += 1
        cur_cycle = gc
        think_cycle = None          # the thinking streak (if any) ends here
        streak_thoughts = []
        await emit_event({"type": "agent_loop_v5.cycle_planning", "stream_id": stream_id,
                          "cycle": cur_cycle, "step_id": step_id, "session_id": sid})
        if thought:
            await emit_event({"type": "agent_loop_v5.think", "stream_id": stream_id,
                              "cycle": cur_cycle, "step_id": step_id,
                              "thought": thought[:1500], "session_id": sid})

        if tool not in allowed:
            # Soft scope: the step is scoped to its assigned (+ any widened) caps,
            # but a cap that IS in the broader toolkit can be requested via
            # need_caps rather than hard-failing the specialist.
            _avail = ", ".join(allowed) or "(none — emit done)"
            _can_req = [c for c in catalog_set if c not in allowed][:12]
            _msg = (f"'{tool}' is not in this step's scope. Allowed now: {_avail}. "
                    + ("Request it with need_caps (it IS in the toolkit). " if tool in catalog_set
                       else "It is not in the toolkit. ")
                    + (f"Other requestable caps: {', '.join(_can_req)}." if _can_req else ""))
            # Recorded as a meta entry ("(denied …)") so it doesn't pollute the
            # unique-tool / ok stats on the final card.
            history.append({"tool": f"(denied {tool})", "ok": False, "preview": _msg,
                            "args": args, "ms": 0})
            pending_note = _msg
            await emit_event({"type": "agent_loop_v5.tool_done", "stream_id": stream_id,
                              "cycle": cur_cycle, "step_id": step_id, "tool": tool, "ok": False,
                              "elapsed_ms": 0, "preview": _msg, "error": _msg, "session_id": sid})
            continue

        tool_calls[tool] = tool_calls.get(tool, 0) + 1

        coerced_args, coerce_notes = _coerce_args(tool, args)
        if coerce_notes:
            args = coerced_args
        if (artifact_dir_path and tool in ("exec.bash.run", "exec.ps.run", "exec.code.run")
                and isinstance(args, dict) and not str(args.get("cwd") or "").strip()):
            args["cwd"] = artifact_dir_path
        # Safety net for generative caps: never let an INVENTED model name through
        # (e.g. 'gpt-3.5-turbo' on an Ollama cluster → 0 tokens). Drop it so the
        # cluster default is used. pending_note was just consumed, so this won't
        # clobber a later failure note (which would correctly take precedence).
        if model_set and _v5_is_generative(tool):
            _m = str(args.get("model") or "").strip()
            if _m and _m not in model_set:
                args.pop("model", None)
                pending_note = f"(note: dropped unknown model '{_m}' — used the cluster default instead)"

        await emit_event({"type": "agent_loop_v5.tool_call", "stream_id": stream_id,
                          "cycle": cur_cycle, "step_id": step_id, "tool": tool, "args": args,
                          "thought": thought, "session_id": sid})
        t0 = time.monotonic()
        invoke = await call_tool(tool, args, session_id=sid, trace_id=trace_id or "")
        if invoke.get("ok") and isinstance(invoke.get("result"), dict) and invoke["result"].get("error"):
            invoke["ok"] = False
            invoke["error"] = str(invoke["result"]["error"])

        # ── Long-running jobs: a cap like research.*/ml.*/exec.* returns a job_id
        #    immediately and streams the REAL output over seconds–minutes. Await
        #    it (WS-stream for research.*, else poll the status cap) so the step
        #    collects the actual result instead of a `{job_id, status:queued}`
        #    blob and racing ahead. Mid-run sub-service errors are NOT treated as
        #    total failure — the job still returns (partial) output; only a true
        #    await-level failure (timeout/unreachable status cap) fails the call. ─
        if (invoke.get("ok") and await_long_running
                and isinstance(invoke.get("result"), dict)
                and _detect_job_id(invoke["result"])):
            try:
                awaited = await _universal_await_job(
                    cap_name=tool, immediate=invoke["result"],
                    session_id=sid, trace_id=trace_id or "", cycle=cur_cycle,
                    max_wait_secs=float(long_running_timeout_secs), stream_id=stream_id)
                if isinstance(awaited, dict):
                    invoke["result"] = awaited
                    if awaited.get("_await_error"):
                        invoke["ok"] = False
                        invoke["error"] = str(awaited["_await_error"])
            except Exception as e:
                log.debug("v5 long-running await failed for %s: %s", tool, e)
        elapsed = round((time.monotonic() - t0) * 1000)

        invoke_ok = bool(invoke.get("ok"))
        # A call can SUCCEED yet return junk (consent/captcha/redirect page). Treat
        # that as a soft failure so the step pivots instead of declaring victory.
        unhelpful = False
        if invoke_ok:
            preview = _result_preview(invoke["result"])
            unhelpful = _v5_looks_unhelpful(preview)
            if unhelpful:
                preview = "(result looks like a consent/blocked/login page — not usable)\n" + preview
            else:
                outputs[tool] = preview[:1000]
                had_useful = True
                ok = True
        else:
            preview = "ERROR: " + str(invoke.get("error", "unknown error"))
            if coerce_notes:
                preview += "\n\nNote: args auto-coerced: " + "; ".join(coerce_notes[:4])
        entry_ok = invoke_ok and not unhelpful
        history.append({"tool": tool, "ok": entry_ok, "preview": preview[:1200],
                        "args": args, "ms": elapsed})
        await emit_event({"type": "agent_loop_v5.tool_done", "stream_id": stream_id,
                          "cycle": cur_cycle, "step_id": step_id, "tool": tool,
                          "ok": entry_ok, "elapsed_ms": elapsed,
                          "preview": preview[:1800],
                          "error": (str(invoke.get("error", "")) if not invoke_ok
                                    else ("unusable result" if unhelpful else "")),
                          "session_id": sid})

        # ── Self-correction: a hard failure OR an ok-but-useless result widens
        #    the step's scope to its recovery toolkit so it can try another cap. ─
        if not entry_ok:
            newly = [c for c in recovery_caps if c not in allowed]
            if newly:
                allowed.extend(newly)
                show = newly[:8]
                new_sigs = "\n".join(rich_cap_signature(c) for c in show)
                dynamic_caps_block = (dynamic_caps_block + "\n" + new_sigs) if dynamic_caps_block else new_sigs
                await emit_event({"type": "agent_loop_v5.scope_widened", "session_id": sid,
                                  "stream_id": stream_id, "step_id": step_id,
                                  "added": newly, "reason": "auto (last call failed)"})
                pending_note = ("The last call did not yield a usable result. You may now also use: "
                                + ", ".join(show) + ". Try a DIFFERENT approach.")
            else:
                pending_note = ("The last call did not yield a usable result — try a different "
                                "query/URL, or request another capability via need_caps.")

        # Stuck-loop guard: the specialist keeps hammering one cap without
        # finishing. Stop the step and keep the best result it produced.
        if tool_calls[tool] >= _MAX_SAME_TOOL:
            result_summary = (outputs.get(tool) or preview)[:1500]
            ok = ok or had_useful
            await emit_event({"type": "agent_loop_v5.thinking", "stream_id": stream_id,
                              "cycle": (gc + 1), "step_id": step_id,
                              "thought": f"(auto-wrapped: '{tool}' was called {tool_calls[tool]}× "
                                         "without finishing — using the best result so far.)",
                              "session_id": sid})
            break

    if not result_summary:
        if outputs:
            # Prefer the last genuinely useful tool output over a trailing error.
            result_summary = list(outputs.values())[-1][:800]
        elif history:
            result_summary = history[-1]["preview"][:800]
        elif all_thoughts:
            # A reasoning-only step (e.g. analysis) never called a tool — its
            # reasoning IS the deliverable, so keep it and don't mark it failed.
            result_summary = ("\n\n".join(all_thoughts))[:1200]
            ok = True
        else:
            result_summary = "Step finished with no explicit result."
    res = {"id": step_id, "title": step["title"], "ok": ok,
           "summary": result_summary, "outputs": outputs, "cycle_end": gc,
           "history": history}
    await emit_event({"type": "agent_loop_v5.step_done", "session_id": sid, "stream_id": stream_id,
                      "step_id": step_id, "ok": ok, "summary": result_summary[:1500]})
    return res


async def _v5_run_phased_step(step: Dict[str, Any], phases: List[str], *, goal: str,
                              blackboard: Dict[int, Dict[str, Any]], model: str, instance_id: str,
                              prefer_gpu: bool, session_id: str, stream_id: str, trace_id: Any,
                              cycle_budget: int, cycle_offset: int, artifact_dir_path: str,
                              call_tool, build_ctx, catalog_caps: Optional[List[str]] = None,
                              available_models: Optional[List[str]] = None,
                              await_long_running: bool = True,
                              long_running_timeout_secs: int = 1800) -> Dict[str, Any]:
    """Run a step as a v4-style cadence: each chosen phase (explore/think/act/verify)
    is handed to its OWN scoped ephemeral sub-agent, in canonical order, threading
    each phase's output to the next. Aggregates into a single step result; a VERIFY
    phase that reports 'FAIL…' marks the step not-ok so the normal failure→replan
    fires."""
    sid = session_id
    parent_id = step["id"]
    await emit_event({"type": "agent_loop_v5.step_start", "session_id": sid, "stream_id": stream_id,
                      "step_id": parent_id, "title": step["title"], "goal": step["goal"],
                      "caps": step.get("caps") or [], "skills": [], "phases": phases})
    await emit_event({"type": "agent_loop_v5.phases", "session_id": sid, "stream_id": stream_id,
                      "parent_id": parent_id, "title": step["title"], "phases": phases})

    gc = cycle_offset
    merged_bb = dict(blackboard)
    phase_results: List[Dict[str, Any]] = []
    history: List[Dict[str, Any]] = []
    for k, ph in enumerate(phases):
        sub_id = parent_id * 100 + 90 + k        # collision-free with sub-plan ids (parent*100+1..6)
        sub = {"id": sub_id, "title": step["title"], "goal": step["goal"],
               "caps": list(step.get("caps") or []), "skills": list(step.get("skills") or []),
               "needs": []}
        r = await _v5_run_step(
            sub, goal=goal, blackboard=merged_bb, model=model, instance_id=instance_id,
            prefer_gpu=prefer_gpu, session_id=sid, stream_id=stream_id, trace_id=trace_id,
            cycle_budget=cycle_budget, cycle_offset=gc, artifact_dir_path=artifact_dir_path,
            call_tool=call_tool, build_ctx=build_ctx, catalog_caps=catalog_caps,
            available_models=available_models, await_long_running=await_long_running,
            long_running_timeout_secs=long_running_timeout_secs, phase=ph)
        gc = r.get("cycle_end", gc)
        merged_bb[sub_id] = r
        phase_results.append(r)
        history.extend(r.get("history") or [])

    verify_failed = any(ph == "verify" and str(r.get("summary") or "").strip().upper().startswith("FAIL")
                        for ph, r in zip(phases, phase_results))
    agg_ok = bool(phase_results) and all(r.get("ok") for r in phase_results) and not verify_failed
    summary = "\n\n".join(f"[{ph}] {(r.get('summary') or '')[:500]}"
                          for ph, r in zip(phases, phase_results))[:1800]
    await emit_event({"type": "agent_loop_v5.step_done", "session_id": sid, "stream_id": stream_id,
                      "step_id": parent_id, "ok": agg_ok, "summary": summary[:1500]})
    return {"id": parent_id, "title": step["title"], "ok": agg_ok, "summary": summary,
            "outputs": {}, "cycle_end": gc, "history": history, "phased": True,
            "phase_results": phase_results}


async def _v5_master_plan(goal: str, catalog_brief: str, *, model: str = "",
                          instance_id: str = "", prefer_gpu: bool = True) -> Dict[str, str]:
    """For an EXTREME goal, build a specialist planner ON THE FLY and have it write
    a long-form strategic plan. Two cheap calls: (1) generate a domain-expert
    planner persona tailored to this goal, (2) use that persona to produce a
    comprehensive prose/outline plan. The normal orchestrator then breaks the
    long-form plan into actionable steps (passed in as `master_plan`)."""
    persona = ("a world-class strategic planner with deep, relevant domain expertise for the goal")
    try:
        p_raw = await _safe_ollama_generate_dw(
            f"GOAL: {goal}\n\nIn ONE sentence, describe the ideal expert PLANNER persona to "
            "design a strategy for this goal (their domain expertise and planning style). "
            "Reply with just the persona description.",
            system=("You assemble expert planner personas on demand. Name the specific domain "
                    "expertise the goal demands."),
            model=model, instance_id=instance_id, prefer_gpu=prefer_gpu, json_mode=False)
        cand = _strip_think(p_raw or "")[0].strip()
        if cand:
            persona = cand[:400]
    except Exception as e:
        log.debug("v5 master-planner persona build failed: %s", e)

    long_form = ""
    try:
        lf_raw = await _safe_ollama_generate_dw(
            (f"GOAL: {goal}\n\nAVAILABLE CAPABILITY AREAS (for grounding):\n{catalog_brief}\n\n"
             "Write a COMPREHENSIVE long-form plan: the overall strategy, the major phases/"
             "work-streams in order, key sub-goals and their dependencies, milestones, the main "
             "risks/unknowns and how to de-risk them, and clear success criteria. Prose and "
             "outline — NOT JSON. Be thorough; this will be broken into concrete executable steps."),
            system=(f"You are {persona}. Produce rigorous, actionable strategic plans."),
            model=model, instance_id=instance_id, prefer_gpu=prefer_gpu, json_mode=False)
        long_form = _strip_think(lf_raw or "")[0].strip()[:6000]
    except Exception as e:
        log.debug("v5 master-planner long-form build failed: %s", e)
    return {"persona": persona, "long_form": long_form}


async def _v5_synthesize_final(goal: str, results: List[Dict[str, Any]], *,
                               model: str = "", instance_id: str = "",
                               prefer_gpu: bool = True) -> str:
    """Compose a final answer from the per-step results (the blackboard)."""
    block = "\n\n".join(
        f"STEP {r['id']} — {r['title']} ({'ok' if r.get('ok') else 'failed'}):\n{(r.get('summary') or '')[:1000]}"
        for r in results) or "(no steps were executed)"
    sys = ("Write the final answer to the user's GOAL using the results of the executed steps. "
           "Be direct and concrete. Do not mention 'steps' or internal orchestration unless the "
           "goal asked for a process. If something failed, state what is known and what is missing.")
    prompt = f"GOAL: {goal}\n\nSTEP RESULTS:\n{block}\n\nWrite the final answer."
    try:
        raw = await _safe_ollama_generate_dw(
            prompt, system=sys, model=model, instance_id=instance_id,
            prefer_gpu=prefer_gpu, json_mode=False)
        out = _strip_think(raw or "")[0].strip()
        if out:
            return out[:8000]
    except Exception as e:
        log.debug("v5 synthesize_final failed: %s", e)
    # Fallback: concatenate the step summaries.
    return block[:8000]


@capability(
    "dag.agent_loop_v5",
    http_method="POST", http_path="/dag/agent_loop_v5",
    http_tags=["dag", "agents"],
    memory="on",
    streams=["dag.agent_loop_v5"],
    description=(
        "v5 agent loop: an ORCHESTRATOR decomposes the goal into an ordered step plan in a "
        "single LLM call, then hands each step to an EPHEMERAL SCOPED SPECIALIST sub-agent. "
        "The orchestrator sees only cap name+description (a capped catalog) and the skill list; "
        "each step's sub-agent sees only full schemas for its assigned caps, any dynamically "
        "loaded skills, and a curated slice of prior-step outputs. Fast start (no separate "
        "triage/step-select/plan calls) and per-step scoped toolkits (never balloons). "
        "Inputs: goal (str!), allowed_caps (csv), base_toolkit (csv — only the first few act as "
        "a catalog floor), max_steps (int default 8), step_cycle_budget (int default 6), "
        "catalog_size (int default 40), enable_replan (bool default True), "
        "enable_dynamic_skills (bool default True), skill_allow (csv — only these skills are "
        "eligible), skill_deny (csv — exclude these skills), auto_suggest_skills (bool default "
        "True — soft-attach a cap's suggested skills when a step picks none), "
        "enable_recon (bool default True — let the orchestrator run a few READ-ONLY recon "
        "actions before finalising the plan when it needs grounding; the simple path stays one "
        "LLM call), enable_subplans (bool default True — a step marked `complex` is expanded into "
        "its own one-level sub-plan), enable_phases (bool default True — the planner may give a "
        "step a `phases` subset of explore/think/act/verify, each run as its own scoped sub-agent), "
        "enable_master_planner (bool default True — an EXTREME goal is first handed to a specialist "
        "long-form planner built on the fly, whose strategy the orchestrator then breaks into "
        "steps), await_long_running (bool default True — when a cap returns a job_id, WS-stream/poll "
        "it to completion so the step gets the REAL result, not a {job_id,queued} blob), "
        "long_running_timeout_secs (int default 1800), handover (bool), "
        "handover_max_chars (int), plus model/instance_id/prefer_gpu/session_id. A specialist "
        "whose caps are insufficient or whose call fails/returns junk can request more caps "
        "(need_caps) or is auto-granted a recovery toolkit, and is shown the cluster's real model "
        "names so it never invents one. "
        "Output: {goal, steps, blackboard, history, cycles, final, toolkit, stream_id, done}."
    ),
)
async def cap_dag_agent_loop_v5(
    goal:               str,
    allowed_caps:       str  = "",
    max_cycles:         int  = 12,
    model:              str  = "",
    instance_id:        str  = "",
    prefer_gpu:         bool = True,
    attach_skills:      str  = "",
    attach_ontologies:  str  = "",
    session_id:         str  = "",
    triage_top_k:       int  = 16,
    base_toolkit:       str  = "",
    handover:           bool = False,
    handover_max_chars: int  = 20000,
    max_steps:          int  = 8,
    step_cycle_budget:  int  = 6,
    catalog_size:       int  = _V5_CATALOG_MAX_DEFAULT,
    enable_replan:      bool = True,
    enable_dynamic_skills: bool = True,
    skill_allow:        str  = "",
    skill_deny:         str  = "",
    auto_suggest_skills: bool = True,
    enable_recon:       bool = True,
    enable_subplans:    bool = True,
    enable_phases:      bool = True,
    enable_master_planner: bool = True,
    await_long_running: bool = True,
    long_running_timeout_secs: int = 1800,
    trace_id=None,
):
    if not goal:
        return {"error": "goal required"}
    sid = session_id or str(uuid.uuid4())
    max_steps = max(1, min(20, int(max_steps)))
    step_cycle_budget = max(1, min(20, int(step_cycle_budget)))
    catalog_size = max(8, min(80, int(catalog_size)))
    long_running_timeout_secs = max(30, int(long_running_timeout_secs))

    ctx = _ctx()
    ollama_generate = getattr(ctx, "ollama_generate", None) if ctx else None
    if ollama_generate is None:
        return {"error": "context module not loaded — ollama_generate missing"}

    # Tool-call shim (reuses ctx helper when present, else a minimal local caller).
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
    build_ctx = getattr(ctx, "build_context_prompt", None) if enable_dynamic_skills else None

    await emit_event({"type": "agent_loop_v5.triage_start", "goal": goal[:200], "session_id": sid})

    # ── Catalog: relevance-discovered, name+desc only, HARD-capped (no base-cap
    #    ballooning — only the first few base caps act as a floor). ────────────
    base_caps = [c.strip() for c in (base_toolkit or "").replace(",", " ").split() if c.strip()]
    try:
        catalog_names = await _workshop_build_toolkit(
            allowed_caps=allowed_caps, category="other", keywords=[],
            top_k=max(8, catalog_size // 2), goal=goal, base_caps=base_caps[:8])
    except Exception as e:
        log.debug("v5 catalog build failed: %s", e)
        catalog_names = list(base_caps[:8])
    catalog_names = catalog_names[:catalog_size]
    # Guarantee a real action tool is always offered (see _V5_ESSENTIAL_ACTION_CAPS).
    for _ess in reversed(_V5_ESSENTIAL_ACTION_CAPS):
        if _ess in CAPABILITY_REGISTRY and _ess not in catalog_names:
            catalog_names.insert(0, _ess)
    if not catalog_names:
        return {"error": "No capabilities available to orchestrate"}

    # Eligible skills (enabled, not blacklisted, honoring per-run allow/deny).
    _skill_allow = {s.strip() for s in (skill_allow or "").replace(",", " ").split() if s.strip()}
    _skill_deny = {s.strip() for s in (skill_deny or "").replace(",", " ").split() if s.strip()}
    skills = (await _v5_list_skills(trace_id or "", allow=_skill_allow, deny=_skill_deny)
              if enable_dynamic_skills else [])
    cap_skill_map = _v5_cap_skill_map(skills)
    eligible_skill_ids = {s["id"] for s in skills}

    await emit_event({"type": "agent_loop_v5.triage_done", "session_id": sid,
                      "triage": {"category": "orchestrated", "keywords": [],
                                 "reasoning": "v5 plans steps and delegates to scoped specialists"}})
    await emit_event({"type": "agent_loop_v5.toolkit", "stream_id": "", "session_id": sid,
                      "toolkit": list(catalog_names)})

    # ── Artifact directory (sandbox-configured) ───────────────────────────────
    artifact_dir_path = ""
    try:
        import importlib as _il
        _exec_mod = _il.import_module("Vera.vera.execution.exec_capabilities")
        artifact_dir_path = _exec_mod.artifact_dir(session_id=sid)
    except Exception as e:
        log.debug("v5 artifact dir resolve failed: %s", e)

    # ── Stream registration ───────────────────────────────────────────────────
    stream_register = getattr(ctx, "stream_register", None)
    stream_complete = getattr(ctx, "stream_complete", None)
    stream_id = ""
    if stream_register:
        try:
            stream_id = await stream_register(
                kind="agent_loop_v5", source_cap="dag.agent_loop_v5",
                session_id=sid, label=goal[:80], persist_full=True,
                fabric_dataset="streams.agent_loop_v5",
                metadata={"goal": goal, "catalog": list(catalog_names), "max_steps": max_steps})
        except Exception:
            stream_id = ""

    # Available cluster models — so specialists using llm.*/ollama.* caps pick a
    # REAL model (or omit it) instead of inventing one. Best-effort, fetched once.
    available_models = await _v5_available_models(trace_id or "")

    # ── Orchestrate (ONE LLM call on the fast path) ───────────────────────────
    plan = await _v5_orchestrate_plan(
        goal, catalog_names, skills, cap_skill_map,
        model=model, instance_id=instance_id, prefer_gpu=prefer_gpu, max_steps=max_steps)
    complexity = (plan.get("complexity") or "").lower()

    # ── EXTREME goals: defer to a specialist long-form planner built ON THE FLY,
    #    then re-break its master plan into concrete actionable steps. Only fires
    #    for goals the orchestrator itself classed "extreme", so simple/complex
    #    goals are untouched. ───────────────────────────────────────────────────
    if enable_master_planner and complexity == "extreme":
        try:
            catalog_brief = "\n".join("  " + _v5_brief_cap_line(n) for n in catalog_names[:24])
            mp = await _v5_master_plan(goal, catalog_brief, model=model,
                                       instance_id=instance_id, prefer_gpu=prefer_gpu)
            await emit_event({"type": "agent_loop_v5.master_plan", "session_id": sid,
                              "stream_id": stream_id, "persona": mp.get("persona", ""),
                              "long_form": mp.get("long_form", "")})
            if mp.get("long_form"):
                plan2 = await _v5_orchestrate_plan(
                    goal, catalog_names, skills, cap_skill_map,
                    model=model, instance_id=instance_id, prefer_gpu=prefer_gpu,
                    max_steps=max_steps, master_plan=mp["long_form"])
                if plan2.get("steps"):
                    plan = plan2
        except Exception as e:
            log.debug("v5 master-planner stage failed: %s", e)

    # Optional pre-plan recon: ONLY when the orchestrator actually asked for it, so
    # simple goals stay a single LLM call with no tool calls. When recon runs, a
    # SECOND orchestration call is made with the findings to finalise the plan.
    recon = plan.get("recon") or []
    if enable_recon and recon:
        try:
            findings = await _v5_run_recon(
                recon, session_id=sid, stream_id=stream_id, trace_id=trace_id,
                call_tool=_agent_loop_call_tool)
            if findings:
                plan2 = await _v5_orchestrate_plan(
                    goal, catalog_names, skills, cap_skill_map,
                    model=model, instance_id=instance_id, prefer_gpu=prefer_gpu,
                    max_steps=max_steps, recon_findings=findings)
                if plan2.get("steps"):
                    plan = plan2
        except Exception as e:
            log.debug("v5 recon stage failed: %s", e)
    steps = plan.get("steps") or []
    if not steps:
        steps = [{"id": 1, "title": goal[:120], "goal": goal,
                  "caps": list(catalog_names[:6]), "skills": [], "needs": [],
                  "complex": False, "phases": []}]
    if not enable_phases:
        for s in steps:
            s["phases"] = []
    # Soft-merge cap-suggested skills into steps that picked none (orchestrator
    # choices are preserved; this only fills gaps).
    _v5_apply_skill_suggestions(steps, cap_skill_map, eligible_skill_ids, auto_suggest_skills)
    await emit_event({"type": "agent_loop_v5.plan", "session_id": sid, "stream_id": stream_id,
                      "steps": [{"id": s["id"], "title": s["title"], "caps": s["caps"],
                                 "skills": s["skills"], "complex": bool(s.get("complex")),
                                 "phases": s.get("phases") or []} for s in steps],
                      "reason": plan.get("reason", ""),
                      "complexity": plan.get("complexity", "")})

    # ── Execute steps over a shared blackboard (cheap, failure-triggered replan) ─
    blackboard: Dict[int, Dict[str, Any]] = {}
    results: List[Dict[str, Any]] = []
    flat_history: List[Dict[str, Any]] = []   # flat tool-call log → final-card stats
    queue = list(steps)
    executed = 0
    gcycle = 0

    async def _run_complex_step(cstep, gc_in):
        """Expand a step flagged `complex` into its OWN one-level sub-plan (a
        master plan of phases → sub-steps), run the sub-steps as scoped
        specialists, then aggregate them into a single parent result."""
        parent_id = cstep["id"]
        sub_catalog = list(dict.fromkeys(
            list(cstep.get("caps") or []) + list(catalog_names)))[:catalog_size]
        await emit_event({"type": "agent_loop_v5.step_start", "session_id": sid, "stream_id": stream_id,
                          "step_id": parent_id, "title": cstep["title"], "goal": cstep["goal"],
                          "caps": cstep.get("caps") or [], "skills": []})
        sub = await _v5_orchestrate_plan(
            cstep["goal"], sub_catalog, skills, cap_skill_map,
            model=model, instance_id=instance_id, prefer_gpu=prefer_gpu,
            max_steps=min(_V5_SUBPLAN_MAX_STEPS, max_steps))
        sub_steps = sub.get("steps") or []
        if not sub_steps:
            # Nothing to decompose — fall back to a normal scoped mini-loop.
            return await _v5_run_step(
                cstep, goal=goal, blackboard=blackboard, model=model, instance_id=instance_id,
                prefer_gpu=prefer_gpu, session_id=sid, stream_id=stream_id, trace_id=trace_id,
                cycle_budget=step_cycle_budget, cycle_offset=gc_in,
                artifact_dir_path=artifact_dir_path, call_tool=_agent_loop_call_tool,
                build_ctx=build_ctx, catalog_caps=sub_catalog, available_models=available_models,
                await_long_running=await_long_running,
                long_running_timeout_secs=long_running_timeout_secs)
        # Renumber sub-steps to collision-free display ids and remap their needs.
        idmap: Dict[int, int] = {}
        for j, ss in enumerate(sub_steps):
            idmap[ss.get("id", j + 1)] = parent_id * 100 + j + 1
        for j, ss in enumerate(sub_steps):
            ss["id"] = parent_id * 100 + j + 1
            ss["needs"] = [idmap.get(n, n) for n in (ss.get("needs") or [])]
            ss["title"] = (f"{parent_id}.{j + 1} " + str(ss.get("title") or "")).strip()[:120]
            ss["complex"] = False   # ONE nesting level only
            ss["phases"] = []       # sub-plan sub-steps stay flat (bounds nesting)
        _v5_apply_skill_suggestions(sub_steps, cap_skill_map, eligible_skill_ids, auto_suggest_skills)
        await emit_event({"type": "agent_loop_v5.subplan", "session_id": sid, "stream_id": stream_id,
                          "parent_id": parent_id, "title": cstep["title"],
                          "steps": [{"id": s["id"], "title": s["title"], "caps": s["caps"]}
                                    for s in sub_steps],
                          "reason": sub.get("reason", "")})
        sub_bb: Dict[int, Dict[str, Any]] = {}
        sub_results: List[Dict[str, Any]] = []
        sub_history: List[Dict[str, Any]] = []
        gc = gc_in
        for ss in sub_steps:
            r = await _v5_run_step(
                ss, goal=cstep["goal"], blackboard={**blackboard, **sub_bb},
                model=model, instance_id=instance_id, prefer_gpu=prefer_gpu, session_id=sid,
                stream_id=stream_id, trace_id=trace_id, cycle_budget=step_cycle_budget,
                cycle_offset=gc, artifact_dir_path=artifact_dir_path,
                call_tool=_agent_loop_call_tool, build_ctx=build_ctx,
                catalog_caps=sub_catalog, available_models=available_models,
                await_long_running=await_long_running,
                long_running_timeout_secs=long_running_timeout_secs)
            gc = r.get("cycle_end", gc)
            sub_bb[ss["id"]] = r
            sub_results.append(r)
            sub_history.extend(r.get("history") or [])
        agg_ok = all(r.get("ok") for r in sub_results) if sub_results else False
        agg_summary = "\n\n".join(
            f"[{r['title']}] {(r.get('summary') or '')[:500]}" for r in sub_results)[:1800]
        await emit_event({"type": "agent_loop_v5.step_done", "session_id": sid, "stream_id": stream_id,
                          "step_id": parent_id, "ok": agg_ok, "summary": agg_summary[:1500]})
        return {"id": parent_id, "title": cstep["title"], "ok": agg_ok, "summary": agg_summary,
                "outputs": {}, "cycle_end": gc, "history": sub_history, "subplan": True,
                "sub_steps": sub_results}

    while queue and executed < max_steps:
        step = queue.pop(0)
        executed += 1
        if enable_subplans and step.get("complex"):
            res = await _run_complex_step(step, gcycle)
        else:
            res = await _v5_run_step(
                step, goal=goal, blackboard=blackboard, model=model, instance_id=instance_id,
                prefer_gpu=prefer_gpu, session_id=sid, stream_id=stream_id, trace_id=trace_id,
                cycle_budget=step_cycle_budget, cycle_offset=gcycle,
                artifact_dir_path=artifact_dir_path, call_tool=_agent_loop_call_tool,
                build_ctx=build_ctx, catalog_caps=catalog_names, available_models=available_models,
                await_long_running=await_long_running,
                long_running_timeout_secs=long_running_timeout_secs)
        gcycle = res.get("cycle_end", gcycle)
        blackboard[step["id"]] = res
        results.append(res)
        flat_history.extend(res.get("history") or [])
        # Failure-triggered re-plan: only spend an extra LLM call when a step
        # failed and work remains (keeps the happy path fast).
        if enable_replan and not res.get("ok") and queue and executed < max_steps:
            try:
                adj = await _v5_orchestrate_plan(
                    goal + f"\n\n[Step {step['id']} ('{step['title']}') failed: "
                    f"{(res.get('summary') or '')[:300]}. Re-plan the REMAINING work only.]",
                    catalog_names, skills, cap_skill_map,
                    model=model, instance_id=instance_id, prefer_gpu=prefer_gpu,
                    max_steps=max(1, max_steps - executed))
                new_steps = adj.get("steps") or []
                if new_steps:
                    base = executed
                    for j, ns in enumerate(new_steps):
                        ns["id"] = base + j + 1
                        if not enable_phases:
                            ns["phases"] = []
                    _v5_apply_skill_suggestions(new_steps, cap_skill_map, eligible_skill_ids, auto_suggest_skills)
                    queue = new_steps
                    await emit_event({"type": "agent_loop_v5.replan", "session_id": sid,
                                      "stream_id": stream_id, "after_step": step["id"],
                                      "remaining": [{"id": s["id"], "title": s["title"]} for s in queue],
                                      "reason": adj.get("reason", "")})
            except Exception as e:
                log.debug("v5 replan failed: %s", e)

    # ── Synthesize final ──────────────────────────────────────────────────────
    final = await _v5_synthesize_final(
        goal, results, model=model, instance_id=instance_id, prefer_gpu=prefer_gpu)
    handover_output = ""
    if handover and results:
        try:
            ho = await _run_handover_stage(
                goal=goal,
                history=[{"tool": f"step{r['id']}:{r['title']}", "args": {},
                          "ok": r.get("ok"), "preview": r.get("summary") or ""} for r in results],
                triage={}, cur_final=final, model=model, instance_id=instance_id,
                prefer_gpu=prefer_gpu, max_chars=int(handover_max_chars), session_id=sid)
            handover_output = ho or ""
            if handover_output:
                final = handover_output
        except Exception as e:
            log.debug("v5 handover stage failed: %s", e)

    await emit_event({"type": "agent_loop_v5.done", "stream_id": stream_id, "session_id": sid,
                      "summary": final, "cycles": gcycle, "steps_run": len(results),
                      "reason": "complete"})
    if stream_complete and stream_id:
        try:
            await stream_complete(stream_id, final)
        except Exception:
            pass

    return {
        "goal": goal, "steps": results,
        "blackboard": {str(k): v for k, v in blackboard.items()},
        "toolkit": list(catalog_names), "plan": steps,
        # Flat tool-call log + cycle count drive the final-card stats (the shared
        # renderer reads ev.history / ev.cycles).
        "history": flat_history, "cycles": gcycle,
        "final": final, "summary": final, "handover_output": handover_output,
        "stream_id": stream_id, "session_id": sid, "done": True,
    }


# ═════════════════════════════════════════════════════════════════════════════
# UNIFIED SSE WRAPPER FOR ALL THREE LOOP VARIANTS
# ─────────────────────────────────────────────────────────────────────────────
# Body: {goal, version: "v1"|"v2"|"v3", ...other args}
# Forwards an enriched event stream including:
#   • The agent_loop_*.* events
#   • Long-running tool progress (research.*, exec.*, ml_training.*)
#   • LLM token streams — surfaced as tool_progress
#   • HITL request events (v3 only)
#   • A final "result" event with the full structured response
# ═════════════════════════════════════════════════════════════════════════════

# In-flight agent-loop runner tasks, keyed by session_id, so a Stop button
# (POST /workshop/agent_loop/cancel) — or a client disconnect — can actually
# stop the loop and cancel the in-flight ollama request via task cancellation.
_AGENT_LOOP_TASKS: Dict[str, "asyncio.Task"] = {}


@APP.post("/workshop/agent_loop/cancel")
async def workshop_agent_loop_cancel(request: Request):
    """Stop a running agent loop for a session. Cancels the runner task, which
    propagates CancelledError into the in-flight LLM call so ollama generation
    is aborted too."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    session_id = (body.get("session_id") or "").strip()
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    task = _AGENT_LOOP_TASKS.get(session_id)
    if task and not task.done():
        task.cancel()
        _AGENT_LOOP_TASKS.pop(session_id, None)
        return {"ok": True, "cancelled": True, "session_id": session_id}
    _AGENT_LOOP_TASKS.pop(session_id, None)
    return {"ok": True, "cancelled": False, "session_id": session_id,
            "note": "no running loop for this session"}


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
    triage_category    = (body.get("triage_category", "") or "").strip()
    triage_keywords    = body.get("triage_keywords", "") or ""
    base_toolkit       = body.get("base_toolkit", "") or ""
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
    # ── Phase model + continue (v2/v3) ──────────────────────────────────────
    phased              = bool(body.get("phased", True))
    min_explore_cycles  = int(body.get("min_explore_cycles", 2) or 0)
    require_validate    = bool(body.get("require_validate", True))
    long_running_force_hitl = bool(body.get("long_running_force_hitl", True))
    allow_continue      = bool(body.get("allow_continue", True))
    continue_increment  = int(body.get("continue_increment", 8) or 8)
    auto_continue_max   = int(body.get("auto_continue_max", 0) or 0)
    # ── v4-specific ──────────────────────────────────────────────────────────
    enabled_steps       = body.get("enabled_steps", "plan,explore,think,act,verify") or "plan,explore,think,act,verify"
    select_steps        = bool(body.get("select_steps", True))
    require_verify      = bool(body.get("require_verify", True))
    strict_complete     = bool(body.get("strict_complete", True))
    prefer_terminal_tools = bool(body.get("prefer_terminal_tools", True))
    long_running_caps   = body.get("long_running_caps", "") or ""
    # ── v5-specific ──────────────────────────────────────────────────────────
    v5_max_steps        = int(body.get("max_steps", 8) or 8)
    v5_step_cycle_budget = int(body.get("step_cycle_budget", 6) or 6)
    v5_catalog_size     = int(body.get("catalog_size", 40) or 40)
    v5_enable_replan    = bool(body.get("enable_replan", True))
    v5_enable_dynamic_skills = bool(body.get("enable_dynamic_skills", True))
    v5_skill_allow      = body.get("skill_allow", "") or ""
    v5_skill_deny       = body.get("skill_deny", "") or ""
    v5_auto_suggest_skills = bool(body.get("auto_suggest_skills", True))
    v5_enable_recon     = bool(body.get("enable_recon", True))
    v5_enable_subplans  = bool(body.get("enable_subplans", True))
    v5_enable_phases    = bool(body.get("enable_phases", True))
    v5_enable_master_planner = bool(body.get("enable_master_planner", True))

    def _phase_kwargs():
        return dict(
            phased=phased, min_explore_cycles=min_explore_cycles,
            require_validate=require_validate,
            long_running_force_hitl=long_running_force_hitl,
            allow_continue=allow_continue, continue_increment=continue_increment,
            auto_continue_max=auto_continue_max,
        )

    def _v4_kwargs():
        return dict(
            satisfaction_check=satisfaction_check, enable_expand=enable_expand,
            require_approval=require_approval, hitl_timeout_secs=hitl_timeout_secs,
            triage_top_k=triage_top_k, triage_category=triage_category,
            triage_keywords=triage_keywords, base_toolkit=base_toolkit,
            await_long_running=await_long_running,
            long_running_timeout_secs=long_running_timeout_secs,
            handover=handover, handover_max_chars=handover_max_chars,
            max_search_calls=max_search_calls, max_expands=max_expands,
            count_failed_cycles=count_failed_cycles,
            max_recovery_attempts=max_recovery_attempts,
            system_prompt_template=system_prompt_template,
            min_explore_cycles=min_explore_cycles, allow_continue=allow_continue,
            continue_increment=continue_increment, auto_continue_max=auto_continue_max,
            enabled_steps=enabled_steps, select_steps=select_steps,
            require_verify=require_verify, strict_complete=strict_complete,
            prefer_terminal_tools=prefer_terminal_tools,
            long_running_caps=long_running_caps,
            long_running_force_hitl=long_running_force_hitl,
        )

    def _v5_kwargs():
        return dict(
            triage_top_k=triage_top_k, base_toolkit=base_toolkit,
            handover=handover, handover_max_chars=handover_max_chars,
            max_steps=v5_max_steps, step_cycle_budget=v5_step_cycle_budget,
            catalog_size=v5_catalog_size, enable_replan=v5_enable_replan,
            enable_dynamic_skills=v5_enable_dynamic_skills,
            skill_allow=v5_skill_allow, skill_deny=v5_skill_deny,
            auto_suggest_skills=v5_auto_suggest_skills,
            enable_recon=v5_enable_recon, enable_subplans=v5_enable_subplans,
            enable_phases=v5_enable_phases,
            enable_master_planner=v5_enable_master_planner,
            await_long_running=await_long_running,
            long_running_timeout_secs=long_running_timeout_secs,
        )

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
        "v3":       "dag.agent_loop_v3",
        "v4":       "dag.agent_loop_v4",
        "v5":       "dag.agent_loop_v5",
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
                    kwargs["hitl_timeout_secs"]  = hitl_timeout_secs
                    kwargs.update(_phase_kwargs())
                elif version == "v3":
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
                    kwargs.update(_phase_kwargs())
                elif version == "v4":
                    kwargs.update(_v4_kwargs())
                elif version == "v5":
                    kwargs.update(_v5_kwargs())
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
            "agent_loop_v2.hitl_request",
            "agent_loop_v2.hitl_resolved",
            # think (any variant — thinking-model token blocks)
            "agent_loop.think",
            "agent_loop_v2.think",
            "agent_loop_v3.think",
            # v3
            "agent_loop_v3.triage_start",
            "agent_loop_v3.triage_done",
            "agent_loop_v3.toolkit",
            "agent_loop_v3.cycle_planning",
            "agent_loop_v3.tool_call",
            "agent_loop_v3.tool_done",
            "agent_loop_v3.done",
            "agent_loop_v3.hitl_request",
            "agent_loop_v3.hitl_resolved",
            "agent_loop_v3.repetition_block",
            "agent_loop_v3.args_coerced",
            # v4
            "agent_loop_v4.triage_start",
            "agent_loop_v4.triage_done",
            "agent_loop_v4.toolkit",
            "agent_loop_v4.cycle_planning",
            "agent_loop_v4.tool_call",
            "agent_loop_v4.tool_done",
            "agent_loop_v4.done",
            "agent_loop_v4.hitl_request",
            "agent_loop_v4.hitl_resolved",
            "agent_loop_v4.repetition_block",
            "agent_loop_v4.args_coerced",
            "agent_loop_v4.think",
            "agent_loop_v4.think_delta",
            "agent_loop_v4.phase",
            "agent_loop_v4.budget_pause",
            "agent_loop_v4.budget_continue",
            "agent_loop_v4.step_plan",
            "agent_loop_v4.plan",
            "agent_loop_v4.completion_check",
            "agent_loop_v4.artifact_dir",
            # v5 — orchestrated specialist sub-agents
            "agent_loop_v5.triage_start",
            "agent_loop_v5.triage_done",
            "agent_loop_v5.toolkit",
            "agent_loop_v5.recon",
            "agent_loop_v5.master_plan",
            "agent_loop_v5.plan",
            "agent_loop_v5.subplan",
            "agent_loop_v5.phases",
            "agent_loop_v5.step_start",
            "agent_loop_v5.step_done",
            "agent_loop_v5.replan",
            "agent_loop_v5.scope_widened",
            "agent_loop_v5.cycle_planning",
            "agent_loop_v5.tool_call",
            "agent_loop_v5.tool_done",
            "agent_loop_v5.think",
            "agent_loop_v5.thinking",
            "agent_loop_v5.done",
            # phase model + continue (v2/v3)
            "agent_loop_v3.phase",
            "agent_loop_v3.budget_pause",
            "agent_loop_v3.budget_continue",
            "agent_loop_v2.phase",
            "agent_loop_v2.budget_pause",
            "agent_loop_v2.budget_continue",
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
            "agent_loop.research_report",
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
            # routed-node visibility — which Ollama instance served the planner
            "ollama.request",
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
            "agent_loop.research_report",
            "agent_loop.error_recovery_start",
            "agent_loop.error_recovery_attempt",
            "agent_loop.error_recovery_done",
            "agent_loop.think",
            "agent_loop_v2.think",
            "agent_loop_v3.think",
            "agent_loop_v4.think",
            "agent_loop_v5.think",
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
        # NOTE: v4 events are NOT aliased onto v3 names. The shared
        # <vera-agent-loop-output> renderer matches most events by suffix
        # (e.g. t.endsWith('.cycle_planning')), so emitting a v3 twin would
        # render every cycle TWICE (two cards, one blank). v4 events render
        # natively; the v4-only events (step_plan/plan/completion_check) have
        # dedicated handlers.

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
                    kwargs["hitl_timeout_secs"]  = hitl_timeout_secs
                    kwargs.update(_phase_kwargs())
                elif version == "v3":
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
                    kwargs.update(_phase_kwargs())
                elif version == "v4":
                    kwargs.update(_v4_kwargs())
                elif version == "v5":
                    kwargs.update(_v5_kwargs())
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
        # Register this run so /workshop/agent_loop/cancel (or a client
        # disconnect, handled in the finally below) can stop the loop and
        # cancel the in-flight ollama request via task cancellation. Cancel any
        # stale run for the same session first.
        if session_id:
            _prev = _AGENT_LOOP_TASKS.get(session_id)
            if _prev and not _prev.done():
                _prev.cancel()
            _AGENT_LOOP_TASKS[session_id] = runner

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

                # Filter out events from other sessions. This applies to every
                # forwarded event type, not just agent_loop* — research/exec/
                # stream/workshop.tool_* events published on the same shared
                # Redis channel by a concurrent run would otherwise bleed into
                # this run's output (e.g. two agentic-loop tasks running at
                # once in chat).
                if ev.get("session_id") and ev.get("session_id") != session_id:
                    continue

                # ollama.* events are global (published by every caller); only
                # forward the ones this run stamped with its own session_id.
                if ev_type.startswith("ollama.") and ev.get("session_id") != session_id:
                    continue

                if ev_type.startswith(("agent_loop.", "agent_loop_v2.",
                                         "agent_loop_v3.", "agent_loop_v4.",
                                         "agent_loop_v5.")):
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
            except asyncio.CancelledError:
                # Cancelled via /workshop/agent_loop/cancel (or the cancel in
                # the finally on client disconnect). Surface a clean result.
                final = {"error": "cancelled by user", "cancelled": True}
            except Exception as e:
                final = {"error": str(e)}
            yield _sse({"type": "result", **(final or {})})
        finally:
            # Cancel the runner if it's still alive — this is what stops the
            # loop and propagates CancelledError into the in-flight ollama
            # await when the client disconnects (fetch abort). Idempotent with
            # the explicit /cancel endpoint.
            try:
                if not runner.done():
                    runner.cancel()
            except Exception:
                pass
            if session_id and _AGENT_LOOP_TASKS.get(session_id) is runner:
                _AGENT_LOOP_TASKS.pop(session_id, None)
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
                    orch = importlib.import_module("Vera.vera.capability_orchestration")
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
# loops, not just v3.
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
            "type":     "agent_loop_v3.args_coerced",
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
    description="Return the default system prompts for v1/v2/v3 plus the "
                "list of variables available for templating. UI uses this to "
                "populate the system-prompt editor.",
)
async def cap_workshop_prompt_templates(trace_id=None):
    EQ = "============================================================"
    v3_default = (
        "You are a Vera autonomous agent operating in V3 mode.\n\n"
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
            "v3":       v3_default,
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
            "dag.agent_loop", "dag.agent_loop_v2", "dag.agent_loop_v3",
            "caps.describe", "caps.list",
        ],
        mode      = "tab",
        tab_order = 17,
    )
    log.info("dag-workshop UI panel registered")
except Exception as e:
    log.warning("Could not register dag-workshop panel: %s", e)