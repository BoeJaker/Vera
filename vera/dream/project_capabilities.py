"""
project_capabilities.py — Vera Project System
==============================================

Projects scope dream cycles to a coherent, evolving body of work. A project
points at a set of resources (fabric datasets, notebooks, chats, memory ids,
IDE workspaces) and maintains TWO context fields:

  user_context   — static, written by the human. Purpose, background, constraints.
  llm_context    — dynamic, updated by dream cycles. Current state, learnings,
                   open threads, what's next.

Triggers can be attached to projects. When a trigger fires for a project, the
seed includes the project's full context, all linked resource ids, and a
context-loading directive (full / dynamic / summary). The dream cycle's output
is then folded back into the llm_context via an incremental LLM update — so
the project context grows over time without exploding in size.

Key capabilities
────────────────
  project.list, project.get, project.upsert, project.delete
  project.link, project.unlink              — attach/detach resources
  project.context.assemble                  — build a dream seed from a project
  project.context.update                    — incremental LLM update of llm_context
  project.context.regenerate                — full rebuild from linked resources
  project.dream.run                         — fire a dream cycle scoped to a project
  project.dream.history                     — past dream cycles for a project

Storage
───────
  Redis hash  vera:dream:projects   — slug → JSON record
  Redis zset  vera:dream:project_dreams:{slug} — ts → cycle_id (history)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Import from the orchestrator that owns capability registration
from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, now_iso, register_ui, CAPABILITY_REGISTRY,
)
from Vera.vera.config import cfg

log = logging.getLogger("vera.project_caps")

# Resolve the orchestrator and dream modules at import time
_orch  = sys.modules.get("Vera.vera.capability_orchestration") or \
         sys.modules.get("capability_orchestration")
_dream = sys.modules.get("Vera.vera.dream_capabilities") or \
         sys.modules.get("dream_capabilities")

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

KEY_PROJECTS         = "vera:dream:projects"          # hash slug -> JSON
KEY_PROJECT_DREAMS   = "vera:dream:project_dreams"    # zset prefix per project
KEY_PROJECT_HISTORY  = "vera:dream:project_history"   # zset prefix per project (cycle results)

DEFAULT_PROJECT_PROMPT = (
    "You are working as Vera on this project. Use only the project context, "
    "linked resources, and recent dream-cycle output provided. Identify what "
    "the user is trying to achieve, what info is available, what's missing, "
    "what could be fetched, and what would be the best concrete next step. "
    "Be specific and grounded — never invent activity that didn't happen."
)

# ─────────────────────────────────────────────────────────────────────────────
# REDIS / DREAM MODULE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _redis():
    return getattr(_orch, "REDIS", None) if _orch else None

def _slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or f"project-{uuid.uuid4().hex[:6]}"

async def _llm_generate(prompt: str, system: str = "", prefer_gpu: bool = True) -> str:
    # Prefer the dream module's helper: it streams (so long generations are
    # kept alive token-by-token instead of hitting the whole-response timeout)
    # and relays tokens to the panel when running inside a cycle stage.
    dream = (sys.modules.get("Vera.vera.dream_capabilities")
             or sys.modules.get("dream_capabilities") or _dream)
    gen = getattr(dream, "_llm_generate", None) if dream else None
    if gen:
        try:
            return str(await gen(prompt, system=system, prefer_gpu=prefer_gpu) or "")
        except Exception as e:
            log.debug("project llm (dream helper): %s", e)
            return ""
    fn = getattr(_orch, "ollama_generate", None)
    if not fn:
        return ""
    try:
        async def _sink(tok: str):  # no UI channel — stream purely for keep-alive
            pass
        return str(await fn(prompt, system=system, prefer_gpu=prefer_gpu,
                            stream_cb=_sink) or "")
    except Exception as e:
        log.debug("project llm: %s", e)
        return ""

async def _call_cap(name: str, **kwargs) -> Any:
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap:
        return {"error": f"unknown_cap:{name}"}
    try:
        accepted = set(cap.get("schema", {}).get("properties", {}).keys())
        filtered = {k: v for k, v in kwargs.items() if k in accepted}
        return await cap["func"](**filtered)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ─────────────────────────────────────────────────────────────────────────────
# PROJECT RECORD SHAPE
# ─────────────────────────────────────────────────────────────────────────────

def _new_project(name: str, **kw) -> Dict[str, Any]:
    """Build a default project record."""
    slug = kw.get("slug") or _slugify(name)
    now = now_iso()
    return {
        "slug":              slug,
        "name":              name,
        "description":       kw.get("description", ""),
        "status":            kw.get("status", "active"),
        "created_at":        now,
        "updated_at":        now,
        # Context fields
        "user_context":      kw.get("user_context", ""),
        "llm_context":       kw.get("llm_context", ""),
        "context_mode":      kw.get("context_mode", "full"),  # full|dynamic|summary
        "summary":           kw.get("summary", ""),
        "summary_updated_at": "",
        # Linked resources
        "fabric_dataset_ids": kw.get("fabric_dataset_ids", []),
        "fabric_record_ids":  kw.get("fabric_record_ids", []),
        "notebook_ids":       kw.get("notebook_ids", []),
        "chat_ids":           kw.get("chat_ids", []),
        "memory_ids":         kw.get("memory_ids", []),
        "ide_workspaces":     kw.get("ide_workspaces", []),
        # Git repos: [{name, path, remote_url, branch}]
        "git_repos":          kw.get("git_repos", []),
        # Dream wiring
        "dream_trigger_names": kw.get("dream_trigger_names", []),
        "last_dream_at":       "",
        "dream_count":         0,
        # LLM agent/model overrides — project > trigger > pipeline > default
        "agents":            kw.get("agents", []),
        "models":            kw.get("models", []),
        # Tags
        "tags": kw.get("tags", []),
    }


async def _save_project(proj: Dict[str, Any]) -> bool:
    r = _redis()
    if not r:
        return False
    try:
        proj["updated_at"] = now_iso()
        await r.hset(KEY_PROJECTS, proj["slug"], json.dumps(proj))
        return True
    except Exception as e:
        log.warning("save project: %s", e)
        return False


async def _get_project(slug: str) -> Optional[Dict[str, Any]]:
    r = _redis()
    if not r or not slug:
        return None
    try:
        v = await r.hget(KEY_PROJECTS, slug)
        if not v:
            return None
        return json.loads(v.decode() if isinstance(v, bytes) else v)
    except Exception:
        return None


async def _list_projects() -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return []
    try:
        items = await r.hgetall(KEY_PROJECTS)
        out: List[Dict[str, Any]] = []
        for _, v in (items or {}).items():
            try:
                out.append(json.loads(v.decode() if isinstance(v, bytes) else v))
            except Exception:
                continue
        out.sort(key=lambda p: p.get("updated_at", ""), reverse=True)
        return out
    except Exception as e:
        log.warning("list projects: %s", e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# PER-PROJECT AGENTIC-LOOP HISTORY + ARTIFACT STORE
# ─────────────────────────────────────────────────────────────────────────────
# The escalating agentic loop (v5/v6/v7) and each dream cycle produce structured
# work — a plan, a step-by-step tool trace, a final output, and files/code. We
# persist all of it PER PROJECT so (a) the project page can show a full loop
# history + artifact area and (b) the project_context sensor can hand the dream a
# CLEAN, high-signal continuation context instead of rebuilding it from noisy
# memory_recent (the root cause of the weak/nonsense project dreams).

KEY_PROJECT_LOOPS     = "vera:dream:project_loops"      # + :slug -> LIST loop-run JSON
KEY_PROJECT_ARTIFACTS = "vera:dream:project_artifacts"  # + :slug -> LIST artifact JSON

_MAX_PROJECT_LOOPS     = 100
_MAX_PROJECT_ARTIFACTS = 250
_ARTIFACT_CONTENT_CAP  = 8000

_CODE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".c", ".cc",
    ".cpp", ".h", ".hpp", ".rb", ".php", ".sh", ".bash", ".sql", ".css",
    ".html", ".vue", ".svelte", ".swift", ".kt", ".cs", ".lua", ".r", ".jl",
    ".scala", ".ex", ".exs", ".pl", ".pm", ".yaml", ".yml", ".toml",
}

def _loops_key(slug: str) -> str:     return f"{KEY_PROJECT_LOOPS}:{slug}"
def _artifacts_key(slug: str) -> str: return f"{KEY_PROJECT_ARTIFACTS}:{slug}"


async def _record_artifact(slug: str, *, atype: str, name: str = "", path: str = "",
                           content: str = "", ref: str = "", run_id: str = "",
                           meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Append one artifact (file|code|report|trace) to the project's artifact index."""
    r = _redis()
    if not r or not slug:
        return None
    rec = {
        "id":      uuid.uuid4().hex[:12],
        "type":    atype,
        "name":    name or (os.path.basename(path) if path else atype),
        "path":    path,
        "ref":     ref,
        "run_id":  run_id,
        "size":    len(content or ""),
        "content": (content or "")[:_ARTIFACT_CONTENT_CAP],
        "truncated": len(content or "") > _ARTIFACT_CONTENT_CAP,
        "ts":      now_iso(),
        "meta":    meta or {},
    }
    try:
        await r.rpush(_artifacts_key(slug), json.dumps(rec, default=str))
        await r.ltrim(_artifacts_key(slug), -_MAX_PROJECT_ARTIFACTS, -1)
    except Exception as e:
        log.debug("record artifact: %s", e)
    return rec


async def _harvest_dir_artifacts(slug: str, dir_path: str, run_id: str = "",
                                 limit: int = 20) -> List[str]:
    """Snapshot files an agentic-loop run wrote into its artifact dir. Source files
    are classified as 'code', everything else as 'file'. Returns artifact ids."""
    ids: List[str] = []
    if not dir_path or not os.path.isdir(dir_path):
        return ids
    try:
        for root, _dirs, files in os.walk(dir_path):
            for fn in sorted(files):
                if len(ids) >= limit:
                    return ids
                fp = os.path.join(root, fn)
                try:
                    if os.path.getsize(fp) > 2_000_000:
                        continue
                    with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read(_ARTIFACT_CONTENT_CAP)
                except Exception:
                    continue
                rel = os.path.relpath(fp, dir_path)
                ext = os.path.splitext(fn)[1].lower()
                atype = "code" if ext in _CODE_EXTS else "file"
                rec = await _record_artifact(slug, atype=atype, name=rel, path=fp,
                                             content=content, run_id=run_id)
                if rec:
                    ids.append(rec["id"])
    except Exception as e:
        log.debug("harvest dir artifacts: %s", e)
    return ids


def _trim_steps(steps: List[Any], cap: int = 60) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for s in (steps or [])[:cap]:
        if not isinstance(s, dict):
            continue
        out.append({
            "id":      s.get("id"),
            "title":   str(s.get("title") or "")[:200],
            "cap":     s.get("cap") or s.get("tool") or "",
            "ok":      s.get("ok"),
            "summary": str(s.get("summary") or s.get("preview") or "")[:600],
        })
    return out


async def _record_loop_run(slug: str, *, run_id: str = "", source: str = "dream_cycle",
                           engine: str = "", goal: str = "", plan: str = "",
                           steps: Optional[List[Any]] = None, final: str = "",
                           artifact_ids: Optional[List[str]] = None,
                           cycle_id: str = "", trigger: str = "") -> Optional[Dict[str, Any]]:
    """Append one structured agentic-loop run to the project's loop history."""
    r = _redis()
    if not r or not slug:
        return None
    steps = steps or []
    rec = {
        "run_id":      run_id or uuid.uuid4().hex[:12],
        "source":      source,          # escalation | session | dream_cycle
        "engine":      engine,
        "goal":        (goal or "")[:1000],
        "plan":        (plan or "")[:6000],
        "steps":       _trim_steps(steps),
        "steps_total": len(steps),
        "final":       (final or "")[:8000],
        "artifact_ids": artifact_ids or [],
        "cycle_id":    cycle_id,
        "trigger":     trigger,
        "ts":          now_iso(),
    }
    try:
        await r.rpush(_loops_key(slug), json.dumps(rec, default=str))
        await r.ltrim(_loops_key(slug), -_MAX_PROJECT_LOOPS, -1)
        await emit_event({"type": "project.loop.recorded", "slug": slug,
                          "source": source, "run_id": rec["run_id"],
                          "steps": rec["steps_total"], "artifacts": len(rec["artifact_ids"])})
    except Exception as e:
        log.debug("record loop run: %s", e)
    return rec


async def _list_loop_runs(slug: str, limit: int = 50) -> List[Dict[str, Any]]:
    r = _redis()
    if not r or not slug:
        return []
    try:
        raw = await r.lrange(_loops_key(slug), -int(limit), -1)
        out = [json.loads(x.decode() if isinstance(x, bytes) else x) for x in (raw or [])]
        out.reverse()
        return out
    except Exception:
        return []


async def _list_artifacts(slug: str, limit: int = 250) -> List[Dict[str, Any]]:
    r = _redis()
    if not r or not slug:
        return []
    try:
        raw = await r.lrange(_artifacts_key(slug), -int(limit), -1)
        out = [json.loads(x.decode() if isinstance(x, bytes) else x) for x in (raw or [])]
        out.reverse()
        return out
    except Exception:
        return []


@capability(
    "project.loop.record", memory="off", silent=True,
    http_method="POST", http_path="/dream/projects/loop/record", http_tags=["project"],
    description="Record a structured agentic-loop run against a project: its plan, "
                "step trace, final output, and artifacts (report + trace are stored as "
                "artifacts automatically; files under `artifact_dir` are harvested). "
                "Called by the v5/v6/v7 loop on escalation + completion so the project "
                "accumulates a full loop history the dream can continue from. Inputs: "
                "slug (str!), source (escalation|session|dream_cycle), engine, goal, "
                "plan, steps (JSON list str), final, artifact_dir, cycle_id, trigger, "
                "run_id.",
)
async def project_loop_record(slug: str, source: str = "dream_cycle", engine: str = "",
                              goal: str = "", plan: str = "", steps: str = "",
                              final: str = "", artifact_dir: str = "",
                              cycle_id: str = "", trigger: str = "", run_id: str = "",
                              trace_id=None):
    proj = await _get_project(slug)
    if not proj:
        return {"ok": False, "error": "project not found"}
    if isinstance(steps, str):
        try:
            step_list = json.loads(steps) if steps.strip() else []
        except Exception:
            step_list = []
    else:
        step_list = steps or []
    rid = run_id or uuid.uuid4().hex[:12]
    art_ids: List[str] = []
    if artifact_dir:
        art_ids += await _harvest_dir_artifacts(slug, artifact_dir, run_id=rid)
    if final:
        rep = await _record_artifact(slug, atype="report",
                                     name=((goal or "loop output").split("\n")[0])[:80],
                                     content=final, run_id=rid)
        if rep:
            art_ids.append(rep["id"])
    if step_list:
        trace_txt = "\n".join(
            f"[{s.get('id')}] {str(s.get('title',''))[:120]} → "
            f"{'ok' if s.get('ok') else 'x'} "
            f"{str(s.get('summary') or s.get('preview') or '')[:200]}"
            for s in step_list if isinstance(s, dict))
        tr = await _record_artifact(slug, atype="trace", name="step trace",
                                    content=trace_txt, run_id=rid)
        if tr:
            art_ids.append(tr["id"])
    rec = await _record_loop_run(slug, run_id=rid, source=source, engine=engine,
                                 goal=goal, plan=plan, steps=step_list, final=final,
                                 artifact_ids=art_ids, cycle_id=cycle_id, trigger=trigger)
    # Bump the project's activity timestamp so it sorts fresh.
    try:
        proj["last_dream_at"] = now_iso()
        await _save_project(proj)
    except Exception:
        pass
    return {"ok": True, "run": rec, "artifacts": len(art_ids)}


@capability(
    "project.loops.list", memory="off", silent=True,
    http_method="GET", http_path="/dream/projects/loops", http_tags=["project"],
    description="Full agentic-loop history for a project (newest first): each run's "
                "plan, step trace, final output, and artifact ids. Inputs: slug (str!), "
                "limit (int, default 50).",
)
async def project_loops_list(slug: str = "", limit: int = 50, trace_id=None):
    if not slug:
        return {"ok": False, "error": "slug required"}
    runs = await _list_loop_runs(slug, limit=limit)
    return {"ok": True, "slug": slug, "runs": runs, "count": len(runs)}


@capability(
    "project.artifacts.list", memory="off", silent=True,
    http_method="GET", http_path="/dream/projects/artifacts", http_tags=["project"],
    description="Persistent artifacts for a project (files, code, reports, traces), "
                "newest first, with content trimmed for the list view (fetch full via "
                "project.artifact.get). Inputs: slug (str!), limit (int, default 250), "
                "type (optional filter: file|code|report|trace).",
)
async def project_artifacts_list(slug: str = "", limit: int = 250, type: str = "",
                                 trace_id=None):
    if not slug:
        return {"ok": False, "error": "slug required"}
    arts = await _list_artifacts(slug, limit=limit)
    if type:
        arts = [a for a in arts if a.get("type") == type]
    lite = [{k: v for k, v in a.items() if k != "content"} | {
        "preview": (a.get("content") or "")[:400]} for a in arts]
    return {"ok": True, "slug": slug, "artifacts": lite, "count": len(lite)}


@capability(
    "project.artifact.get", memory="off", silent=True,
    http_method="GET", http_path="/dream/projects/artifact/get", http_tags=["project"],
    description="Fetch ONE artifact with its full stored content. Inputs: slug (str!), "
                "id (str!).",
)
async def project_artifact_get(slug: str = "", id: str = "", trace_id=None):
    if not slug or not id:
        return {"ok": False, "error": "slug and id required"}
    for a in await _list_artifacts(slug, limit=1000):
        if a.get("id") == id:
            return {"ok": True, "artifact": a}
    return {"ok": False, "error": "artifact not found"}


@capability(
    "project.artifact.add", memory="off", silent=True,
    http_method="POST", http_path="/dream/projects/artifact/add", http_tags=["project"],
    description="Add a single artifact to a project. Inputs: slug (str!), type "
                "(file|code|report|trace), name, path, content, ref, run_id.",
)
async def project_artifact_add(slug: str = "", type: str = "file", name: str = "",
                               path: str = "", content: str = "", ref: str = "",
                               run_id: str = "", trace_id=None):
    if not slug:
        return {"ok": False, "error": "slug required"}
    if not await _get_project(slug):
        return {"ok": False, "error": "project not found"}
    rec = await _record_artifact(slug, atype=type, name=name, path=path,
                                 content=content, ref=ref, run_id=run_id)
    return {"ok": bool(rec), "artifact": rec}


# ─────────────────────────────────────────────────────────────────────────────
# MULTI-SESSION PLAN LEDGER (Stream B)
# ─────────────────────────────────────────────────────────────────────────────
# A strategic project's documented plan is decomposed ONCE into discrete,
# ordered PORTIONS (each completable in one session). Each project_compose dream
# cycle reads this ledger, picks the single next unfinished portion, executes it,
# and marks it done — so the goal advances a portion at a time across days.

KEY_PROJECT_PLAN = "vera:dream:project_plan"   # + :slug -> JSON ledger

def _plan_key(slug: str) -> str: return f"{KEY_PROJECT_PLAN}:{slug}"


async def _get_plan_ledger(slug: str) -> Dict[str, Any]:
    r = _redis()
    if not r or not slug:
        return {}
    try:
        v = await r.get(_plan_key(slug))
        return json.loads(v.decode() if isinstance(v, bytes) else v) if v else {}
    except Exception:
        return {}


async def _save_plan_ledger(slug: str, ledger: Dict[str, Any]) -> bool:
    r = _redis()
    if not r or not slug:
        return False
    try:
        ledger["updated_at"] = now_iso()
        await r.set(_plan_key(slug), json.dumps(ledger, default=str))
        return True
    except Exception:
        return False


@capability(
    "project.plan.get", memory="off", silent=True,
    http_method="GET", http_path="/dream/projects/plan", http_tags=["project"],
    description="Get a project's multi-session plan ledger — the ordered portions the "
                "dream advances one at a time, each with a status (pending|active|done). "
                "Inputs: slug (str!).",
)
async def project_plan_get(slug: str = "", trace_id=None):
    if not slug:
        return {"ok": False, "error": "slug required"}
    led = await _get_plan_ledger(slug)
    return {"ok": True, "slug": slug, "portions": led.get("portions", []),
            "plan_hash": led.get("plan_hash", ""), "updated_at": led.get("updated_at", "")}


@capability(
    "project.plan.set", memory="off", silent=True,
    http_method="POST", http_path="/dream/projects/plan/set", http_tags=["project"],
    description="Replace a project's plan-ledger portions (the multi-session work "
                "breakdown). Inputs: slug (str!), portions (JSON list of "
                "{title, detail, status?}), plan_hash (str — dedupe key for the source plan).",
)
async def project_plan_set(slug: str = "", portions: str = "", plan_hash: str = "",
                           trace_id=None):
    if not slug:
        return {"ok": False, "error": "slug required"}
    if isinstance(portions, str):
        try:
            plist = json.loads(portions) if portions.strip() else []
        except Exception:
            plist = []
    else:
        plist = portions or []
    norm: List[Dict[str, Any]] = []
    for i, p in enumerate(plist):
        if isinstance(p, str):
            p = {"title": p}
        if not isinstance(p, dict):
            continue
        norm.append({
            "id":       str(p.get("id") or f"p{i+1}"),
            "title":    str(p.get("title") or "")[:300],
            "detail":   str(p.get("detail") or "")[:1000],
            "status":   p.get("status") or "pending",
            "cycle_id": p.get("cycle_id", ""),
            "note":     str(p.get("note") or "")[:1000],
            "ts":       p.get("ts", ""),
        })
    led = await _get_plan_ledger(slug)
    led["portions"] = norm
    led["plan_hash"] = plan_hash or led.get("plan_hash", "")
    await _save_plan_ledger(slug, led)
    return {"ok": True, "slug": slug, "portions": norm}


@capability(
    "project.plan.advance", memory="off", silent=True,
    http_method="POST", http_path="/dream/projects/plan/advance", http_tags=["project"],
    description="Mark a plan portion's status (or append an emergent portion). Inputs: "
                "slug (str!), portion_id (str), status (pending|active|done|blocked), "
                "note, title (to add a new portion), detail, cycle_id.",
)
async def project_plan_advance(slug: str = "", portion_id: str = "", status: str = "done",
                               note: str = "", title: str = "", detail: str = "",
                               cycle_id: str = "", trace_id=None):
    if not slug:
        return {"ok": False, "error": "slug required"}
    led = await _get_plan_ledger(slug)
    ports = led.get("portions", [])
    updated = None
    for p in ports:
        if p.get("id") == portion_id and portion_id:
            p["status"] = status or p.get("status")
            if note:
                p["note"] = note[:1000]
            if cycle_id:
                p["cycle_id"] = cycle_id
            p["ts"] = now_iso()
            updated = p
            break
    if updated is None and (title or portion_id):
        updated = {
            "id":       portion_id or f"p{len(ports)+1}",
            "title":    (title or portion_id)[:300],
            "detail":   detail[:1000],
            "status":   status or "pending",
            "note":     note[:1000],
            "cycle_id": cycle_id,
            "ts":       now_iso(),
        }
        ports.append(updated)
    led["portions"] = ports
    await _save_plan_ledger(slug, led)
    return {"ok": True, "portion": updated, "portions": ports}


# ─────────────────────────────────────────────────────────────────────────────
# CRUD CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "project.list", memory="off", silent=True,
    http_method="GET", http_path="/dream/projects/list", http_tags=["project"],
    description="List all projects with their summary metadata.",
)
async def project_list(trace_id=None):
    projs = await _list_projects()
    # Trim heavy fields from list view
    light = []
    for p in projs:
        light.append({**p,
            "user_context": (p.get("user_context") or "")[:500],
            "llm_context":  (p.get("llm_context") or "")[:500],
            "summary":      p.get("summary", "")[:500],
        })
    return {"projects": light, "count": len(light)}


_GOAL_LOOP_STAGES = ("project_action", "stepwise", "agent_loop", "investigate")


async def _goal_active_loop(r, slug: str):
    """Find a goal's dream-cycle agentic loop that is (or recently was) running,
    so the UI can re-attach to it LIVE. The loop persists under session id
    `dream:{cycle_id}:{stage}` (see _run_agent_loop); a goal's cycles are in
    KEY_PROJECT_DREAMS:{slug}. Returns (session_id, status) or ("", "")."""
    if not r or not slug:
        return "", ""
    try:
        cids = await r.zrevrange(f"{KEY_PROJECT_DREAMS}:{slug}", 0, 2)
    except Exception:
        return "", ""
    best = None   # (session, status, score) — best non-running fallback
    for cid in cids or []:
        cid = cid.decode() if isinstance(cid, bytes) else cid
        for stage in _GOAL_LOOP_STAGES:
            sess = f"dream:{cid}:{stage}"
            try:
                run = await r.hgetall(f"vera:loop:run:{sess}")
            except Exception:
                run = None
            if not run:
                continue
            st = run.get(b"status") or run.get("status") or b""
            st = st.decode() if isinstance(st, bytes) else st
            try:
                score = await r.zscore("vera:loop:sessions", sess)
            except Exception:
                score = None
            fresh = (score is not None) and (time.time() - float(score) < 600)
            if st == "running" and fresh:
                # a live loop is the best possible answer — return immediately
                return sess, "running"
            eff = "interrupted" if st == "running" else st
            cand = (sess, eff, float(score or 0))
            if best is None or cand[2] > best[2]:
                best = cand
    return (best[0], best[1]) if best else ("", "")


@capability(
    "goals.list", memory="off", silent=True,
    http_method="GET", http_path="/dream/goals/list", http_tags=["project", "goals"],
    description="List the LONG-TERM GOALS the system is tracking — strategic goals a broad "
                "request escalated to, persisted as dream projects (tagged 'strategic'). Each "
                "carries its status, progress (dream-cycle count + last activity), a snippet of "
                "the documented plan/progress, and any live loop it can re-attach to. Powers the "
                "chat + dream goal trackers.",
)
async def goals_list(trace_id=None):
    projs = await _list_projects()
    r = _redis()
    goals = []
    for p in projs:
        tags = p.get("tags") or []
        if "strategic" not in tags and "v7" not in tags:
            continue
        slug = p.get("slug")
        loop_session, loop_status = await _goal_active_loop(r, slug)
        goals.append({
            "slug":          slug,
            "name":          p.get("name"),
            "description":   (p.get("description") or "")[:400],
            "status":        p.get("status", "active"),
            "dream_count":   int(p.get("dream_count", 0) or 0),
            "last_dream_at": p.get("last_dream_at", ""),
            "created_at":    p.get("created_at", ""),
            "progress":      (p.get("llm_context") or "")[:1200],
            "tags":          tags,
            "loop_session":  loop_session,
            "loop_status":   loop_status,
        })
    goals.sort(key=lambda g: (g.get("last_dream_at") or g.get("created_at") or ""), reverse=True)
    return {"goals": goals, "count": len(goals)}


@capability(
    "goals.detail", memory="off", silent=True,
    http_method="GET", http_path="/dream/goals/detail", http_tags=["project", "goals"],
    description="Full detail for ONE long-term goal (strategic dream project): its documented "
                "plan + rolling progress (llm_context), its agent-notes standing brief, its "
                "snapshotted/produced artifacts, its background thought loops, recent dream "
                "cycles, and any live loop it can re-attach to. Powers the chat + dream goal "
                "trackers' expanded view. Inputs: slug (str!).",
)
async def goals_detail(slug: str = "", trace_id=None):
    if not slug:
        return {"error": "slug required"}
    p = await _get_project(slug)
    if not p:
        return {"error": f"goal not found: {slug}"}
    r = _redis()
    loop_session, loop_status = await _goal_active_loop(r, slug)

    # Agent-notes standing brief (scope=project).
    notes_md = ""
    notes_get = CAPABILITY_REGISTRY.get("notes.get")
    if notes_get and notes_get.get("func"):
        try:
            nres = await notes_get["func"](scope="project", ref_id=slug)
            notes_md = (nres or {}).get("content", "") if isinstance(nres, dict) else ""
        except Exception:
            pass

    # Background thought loops scoped to this goal.
    thoughts: List[Dict[str, Any]] = []
    think_list = CAPABILITY_REGISTRY.get("dream.think.list")
    if think_list and think_list.get("func"):
        try:
            tres = await think_list["func"](project_slug=slug)
            thoughts = (tres or {}).get("thoughts", []) if isinstance(tres, dict) else []
        except Exception:
            pass

    # Recent dream cycles.
    cycles: List[Dict[str, Any]] = []
    hist_cap = CAPABILITY_REGISTRY.get("project.dream.history")
    if hist_cap and hist_cap.get("func"):
        try:
            hres = await hist_cap["func"](slug=slug, limit=10)
            cycles = (hres or {}).get("cycles", []) if isinstance(hres, dict) else []
        except Exception:
            pass

    # Artifacts: list files under the goal-scoped artifact area (best-effort).
    # Path mirrors _v7_goal_artifact_dir: <artifact_root>/goal/<slug>.
    artifacts: List[Dict[str, Any]] = []
    try:
        import importlib as _il
        import os as _os
        _exec_mod = _il.import_module("Vera.vera.execution.exec_capabilities")
        art_root = _os.path.join(_exec_mod._artifact_root(), "goal", _exec_mod._safe_seg(slug))
        if art_root and _os.path.isdir(art_root):
            for base, _dirs, files in _os.walk(art_root):
                for fn in files:
                    fp = _os.path.join(base, fn)
                    try:
                        rel = _os.path.relpath(fp, art_root)
                        artifacts.append({"path": rel.replace("\\", "/"),
                                          "bytes": _os.path.getsize(fp)})
                    except Exception:
                        continue
                if len(artifacts) >= 100:
                    break
    except Exception:
        pass

    return {
        "slug":          slug,
        "name":          p.get("name"),
        "description":   p.get("description", ""),
        "status":        p.get("status", "active"),
        "goal":          p.get("user_context", ""),
        "plan_progress": p.get("llm_context", ""),
        "notes":         notes_md,
        "thoughts":      thoughts,
        "cycles":        cycles,
        "artifacts":     artifacts,
        "dream_count":   int(p.get("dream_count", 0) or 0),
        "last_dream_at": p.get("last_dream_at", ""),
        "created_at":    p.get("created_at", ""),
        "tags":          p.get("tags", []),
        "loop_session":  loop_session,
        "loop_status":   loop_status,
    }


@capability(
    "project.get", memory="off", silent=True,
    http_method="GET", http_path="/dream/projects/get", http_tags=["project"],
    description="Get a single project's full record by slug.",
)
async def project_get(slug: str = "", trace_id=None):
    if not slug:
        return {"error": "slug required"}
    p = await _get_project(slug)
    if not p:
        return {"error": f"project not found: {slug}"}
    return {"project": p}


@capability(
    "project.upsert", memory="off",
    http_method="POST", http_path="/dream/projects/upsert", http_tags=["project"],
    description="Create or update a project. Inputs: name (str!), slug (str, optional), "
                "description (str), user_context (str), llm_context (str), "
                "fabric_dataset_ids (list[str]), notebook_ids (list[str]), "
                "chat_ids (list[str]), memory_ids (list[str]), ide_workspaces (list[str]), "
                "git_repos (list[{name,path,remote_url,branch}]), "
                "dream_trigger_names (list[str]), context_mode (full|dynamic|summary), "
                "status (active|paused|archived), tags (list[str]).",
)
async def project_upsert(
    name: str,
    slug: str = "",
    description: str = "",
    user_context: str = "",
    llm_context: str = "",
    fabric_dataset_ids: Optional[List[str]] = None,
    fabric_record_ids: Optional[List[str]] = None,
    notebook_ids: Optional[List[str]] = None,
    chat_ids: Optional[List[str]] = None,
    memory_ids: Optional[List[str]] = None,
    ide_workspaces: Optional[List[str]] = None,
    git_repos: Optional[List[Dict[str, Any]]] = None,
    dream_trigger_names: Optional[List[str]] = None,
    agents: Optional[List[str]] = None,
    models: Optional[List[str]] = None,
    context_mode: str = "full",
    status: str = "active",
    tags: Optional[List[str]] = None,
    trace_id=None,
):
    if not name:
        return {"ok": False, "error": "name required"}
    s = slug.strip() if slug else _slugify(name)
    existing = await _get_project(s)
    if existing:
        # Merge — preserve created_at, dream_count, summary fields
        proj = existing
        proj.update({
            "name": name,
            "description": description or proj.get("description", ""),
            "user_context": user_context if user_context is not None else proj.get("user_context", ""),
            "llm_context":  llm_context  if llm_context  is not None else proj.get("llm_context", ""),
            "fabric_dataset_ids": list(fabric_dataset_ids or []),
            "fabric_record_ids":  list(fabric_record_ids or proj.get("fabric_record_ids") or []),
            "notebook_ids":       list(notebook_ids or []),
            "chat_ids":           list(chat_ids or []),
            "memory_ids":         list(memory_ids or proj.get("memory_ids") or []),
            "ide_workspaces":     list(ide_workspaces or proj.get("ide_workspaces") or []),
            "git_repos":          list(git_repos if git_repos is not None else proj.get("git_repos") or []),
            "dream_trigger_names": list(dream_trigger_names or []),
            "agents":              list(agents or []),
            "models":              list(models or []),
            "context_mode":        context_mode or proj.get("context_mode") or "full",
            "status":              status or proj.get("status") or "active",
            "tags":                list(tags or proj.get("tags") or []),
        })
    else:
        proj = _new_project(name,
            slug=s, description=description, user_context=user_context,
            llm_context=llm_context, fabric_dataset_ids=list(fabric_dataset_ids or []),
            fabric_record_ids=list(fabric_record_ids or []),
            notebook_ids=list(notebook_ids or []), chat_ids=list(chat_ids or []),
            memory_ids=list(memory_ids or []), ide_workspaces=list(ide_workspaces or []),
            git_repos=list(git_repos or []),
            dream_trigger_names=list(dream_trigger_names or []),
            agents=list(agents or []), models=list(models or []),
            context_mode=context_mode, status=status, tags=list(tags or []))
    ok = await _save_project(proj)
    if ok:
        await emit_event({"type": "project.upserted", "slug": proj["slug"],
                          "name": proj["name"], "is_new": not existing})
    return {"ok": ok, "project": proj}


@capability(
    "project.delete", memory="off",
    http_method="POST", http_path="/dream/projects/delete", http_tags=["project"],
    description="Delete a project by slug. Inputs: slug (str!).",
)
async def project_delete(slug: str, trace_id=None):
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}
    try:
        await r.hdel(KEY_PROJECTS, slug)
        await emit_event({"type": "project.deleted", "slug": slug})
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# DEDUPE — fold the near-duplicate / branched projects one goal spawned into a
# single canonical project. An escalation that re-worded a goal a few times can
# fork a dozen sibling projects (each then running its own dream/loop); this
# merges them: keep the richest one, absorb the others' resources, archive them.
# ─────────────────────────────────────────────────────────────────────────────
def _tokens(text: str) -> set:
    import re as _re
    stop = {"the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "with",
            "build", "create", "make", "set", "up", "using", "use", "project",
            "goal", "system", "tool", "app", "that", "this", "is", "it"}
    return {w for w in _re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 2 and w not in stop}


def _project_similarity(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    """Jaccard over the token sets of name+description+user_context. 1.0 = same."""
    ta = _tokens(" ".join(str(a.get(k) or "") for k in
                          ("name", "description", "user_context")))
    tb = _tokens(" ".join(str(b.get(k) or "") for k in
                          ("name", "description", "user_context")))
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / float(len(ta | tb))


def _project_richness(p: Dict[str, Any]) -> tuple:
    """Sort key for choosing the canonical project in a duplicate group: most
    dreams, then most resources, then oldest (established first)."""
    res = sum(len(p.get(k) or []) for k in
              ("fabric_dataset_ids", "notebook_ids", "chat_ids", "memory_ids",
               "ide_workspaces", "git_repos"))
    return (int(p.get("dream_count", 0) or 0), res,
            -1 * len(str(p.get("created_at") or "")))  # non-empty created earlier


async def _merge_projects(canon: Dict[str, Any], dupes: List[Dict[str, Any]],
                          delete: bool) -> None:
    """Absorb dupes' resource lists into canon, then archive (or delete) them."""
    list_fields = ("fabric_dataset_ids", "fabric_record_ids", "notebook_ids",
                   "chat_ids", "memory_ids", "ide_workspaces", "git_repos",
                   "dream_trigger_names", "agents", "models", "tags")
    for f in list_fields:
        merged = list(canon.get(f) or [])
        seen = {json.dumps(x, sort_keys=True) if isinstance(x, dict) else x
                for x in merged}
        for d in dupes:
            for x in (d.get(f) or []):
                key = json.dumps(x, sort_keys=True) if isinstance(x, dict) else x
                if key not in seen:
                    seen.add(key)
                    merged.append(x)
        canon[f] = merged
    canon["dream_count"] = sum(int(p.get("dream_count", 0) or 0)
                               for p in [canon, *dupes])
    await _save_project(canon)
    r = _redis()
    for d in dupes:
        d["status"] = "archived"
        d["merged_into"] = canon["slug"]
        await _save_project(d)
        if delete and r:
            try:
                await r.hdel(KEY_PROJECTS, d["slug"])
            except Exception:
                pass


@capability(
    "project.dedupe", memory="off",
    http_method="POST", http_path="/dream/projects/dedupe", http_tags=["project"],
    description="Find and (optionally) merge near-duplicate / branched projects — "
                "the sibling projects one re-worded goal can fork. Groups active "
                "projects by content similarity; the richest project in each group "
                "(most dreams/resources, oldest) becomes canonical, absorbs the "
                "others' resources, and the rest are archived (or deleted). Inputs: "
                "threshold (float 0-1, default 0.55), apply (bool=false — preview "
                "only), delete (bool=false — hard-delete dupes instead of archive), "
                "exclude_strategic (bool=false). Output: {ok, groups:[{canonical, "
                "duplicates, scores}], merged, applied}.",
)
async def project_dedupe(threshold: float = 0.55, apply: bool = False,
                         delete: bool = False, exclude_strategic: bool = False,
                         trace_id=None):
    projs = [p for p in await _list_projects()
             if (p.get("status") or "active") != "archived"]
    if exclude_strategic:
        projs = [p for p in projs
                 if not any(t in (p.get("tags") or []) for t in ("strategic", "v7"))]
    thr = max(0.1, min(1.0, float(threshold)))
    used = set()
    groups = []
    for i, p in enumerate(projs):
        if p["slug"] in used:
            continue
        members = [p]
        scores = {}
        for q in projs[i + 1:]:
            if q["slug"] in used:
                continue
            sim = _project_similarity(p, q)
            if sim >= thr:
                members.append(q)
                scores[q["slug"]] = round(sim, 3)
        if len(members) < 2:
            continue
        members.sort(key=_project_richness, reverse=True)
        canon = members[0]
        dupes = members[1:]
        for m in members:
            used.add(m["slug"])
        groups.append({"canonical": {"slug": canon["slug"], "name": canon.get("name")},
                       "duplicates": [{"slug": d["slug"], "name": d.get("name"),
                                       "score": scores.get(d["slug"])} for d in dupes]})
        if apply:
            await _merge_projects(canon, dupes, delete)
    merged = sum(len(g["duplicates"]) for g in groups)
    if apply:
        await emit_event({"type": "project.deduped", "groups": len(groups),
                          "merged": merged, "deleted": bool(delete)})
    return {"ok": True, "applied": bool(apply), "delete": bool(delete),
            "threshold": thr, "groups": groups, "merged": merged}


@capability(
    "project.merge", memory="off",
    http_method="POST", http_path="/dream/projects/merge", http_tags=["project"],
    description="MANUALLY merge a chosen set of projects into one (vs project.dedupe "
                "which auto-groups by similarity). The `into` project absorbs the "
                "others' resources; the others are archived (or deleted). Inputs: "
                "slugs (list[str]! or comma-str — the projects to merge), into (str — "
                "canonical slug; default = the richest of `slugs`), delete (bool=false "
                "— hard-delete the merged-away projects instead of archiving). "
                "Output: {ok, into, merged}.",
)
async def project_merge(slugs: Optional[Any] = None, into: str = "",
                        delete: bool = False, trace_id=None):
    if isinstance(slugs, str):
        slugs = [s.strip() for s in slugs.split(",") if s.strip()]
    slugs = [s for s in (slugs or []) if isinstance(s, str) and s.strip()]
    if len(slugs) < 2:
        return {"ok": False, "error": "need at least 2 project slugs to merge"}
    projs = []
    for s in slugs:
        p = await _get_project(s)
        if p:
            projs.append(p)
    if len(projs) < 2:
        return {"ok": False, "error": "fewer than 2 of those projects exist"}
    canon = None
    if into:
        canon = next((p for p in projs if p["slug"] == into), None)
        if not canon:
            return {"ok": False, "error": f"'into' project {into} not in the selection"}
    else:
        canon = sorted(projs, key=_project_richness, reverse=True)[0]
    dupes = [p for p in projs if p["slug"] != canon["slug"]]
    await _merge_projects(canon, dupes, bool(delete))
    await emit_event({"type": "project.merged", "into": canon["slug"],
                      "merged": [d["slug"] for d in dupes], "deleted": bool(delete)})
    return {"ok": True, "into": canon["slug"], "merged": [d["slug"] for d in dupes]}


# ─────────────────────────────────────────────────────────────────────────────
# CONTEXT ASSEMBLY (for dream seed)
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "project.context.assemble", memory="off",
    http_method="POST", http_path="/dream/projects/context/assemble", http_tags=["project"],
    description="Assemble a dream seed from a project's context. Honours the project's "
                "context_mode: 'full' includes everything; 'summary' uses only the rolling "
                "summary; 'dynamic' asks the LLM to pick the most relevant context for the "
                "given goal. Inputs: slug (str!), goal (str, optional — guides dynamic mode).",
)
async def project_context_assemble(slug: str, goal: str = "", trace_id=None):
    proj = await _get_project(slug)
    if not proj:
        return {"error": f"project not found: {slug}"}

    mode = proj.get("context_mode", "full")
    parts: List[str] = []
    parts.append(f"PROJECT: {proj.get('name', slug)}")
    if proj.get("description"):
        parts.append(f"DESCRIPTION: {proj['description']}")

    if mode == "summary":
        if proj.get("summary"):
            parts.append(f"SUMMARY:\n{proj['summary']}")
    elif mode == "dynamic" and goal:
        # Ask LLM which context blocks are relevant
        blocks = []
        if proj.get("user_context"):
            blocks.append(("user_context", proj["user_context"]))
        if proj.get("llm_context"):
            blocks.append(("llm_context",  proj["llm_context"]))
        if proj.get("summary"):
            blocks.append(("summary",      proj["summary"]))
        if blocks:
            block_descs = "\n".join(
                f"- {n}: {t[:200]}..." for n, t in blocks
            )
            sel_prompt = (
                f"Goal: {goal}\n\n"
                f"Available context blocks:\n{block_descs}\n\n"
                "Reply with a JSON array of the block names to include. Pick the minimum "
                "needed for the goal. Example: [\"llm_context\", \"summary\"]"
            )
            raw = await _llm_generate(sel_prompt,
                system="You select context efficiently. JSON array only.")
            picked = []
            try:
                s, e = raw.find("["), raw.rfind("]")
                if s != -1 and e != -1:
                    picked = json.loads(raw[s:e+1])
            except Exception:
                picked = ["llm_context"]  # safe fallback
            for n, t in blocks:
                if n in picked:
                    parts.append(f"{n.upper()}:\n{t}")
        else:
            parts.append("(no project context yet)")
    else:
        # 'full' — include everything
        if proj.get("user_context"):
            parts.append(f"USER CONTEXT (purpose/background):\n{proj['user_context']}")
        if proj.get("llm_context"):
            parts.append(f"CURRENT STATE (LLM-maintained):\n{proj['llm_context']}")
        if proj.get("summary"):
            parts.append(f"ROLLING SUMMARY:\n{proj['summary']}")

    # Resource manifest — names not contents (caps will fetch on demand)
    manifest = []
    if proj.get("fabric_dataset_ids"):
        manifest.append(f"Linked fabric datasets: {', '.join(proj['fabric_dataset_ids'])}")
    if proj.get("notebook_ids"):
        manifest.append(f"Linked notebooks: {', '.join(proj['notebook_ids'])}")
    if proj.get("chat_ids"):
        manifest.append(f"Linked chats: {len(proj['chat_ids'])} sessions")
    if proj.get("memory_ids"):
        manifest.append(f"Pinned memories: {len(proj['memory_ids'])}")
    if proj.get("ide_workspaces"):
        manifest.append(f"IDE workspaces: {', '.join(proj['ide_workspaces'])}")
    if manifest:
        parts.append("RESOURCES:\n" + "\n".join("- " + m for m in manifest))

    project_context = "\n\n".join(parts)
    seed = {
        "project_id":         slug,
        "project_slug":       slug,
        "project_name":       proj.get("name"),
        "project_context":    project_context,
        "focus_topic":        goal or proj.get("description", "") or proj.get("name"),
        "extra_fabric_ids":   list(proj.get("fabric_record_ids", []))[:30],
        "pinned_memory_ids":  list(proj.get("memory_ids", []))[:20],
    }
    return {"seed": seed, "context_mode": mode, "context_chars": len(project_context)}


# ─────────────────────────────────────────────────────────────────────────────
# CONTEXT UPDATE (rolling)
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "project.context.update", memory="off",
    http_method="POST", http_path="/dream/projects/context/update", http_tags=["project"],
    description="Incrementally update a project's llm_context with new dream-cycle output. "
                "The LLM is given the current llm_context plus the new report and asked to "
                "produce an updated context that integrates the new info while preserving "
                "key learnings. Inputs: slug (str!), new_content (str!), source (str — "
                "label for where the new content came from, e.g. 'dream cycle research_followup').",
)
async def project_context_update(slug: str, new_content: str = "",
                                  source: str = "dream", trace_id=None):
    proj = await _get_project(slug)
    if not proj:
        return {"error": f"project not found: {slug}"}
    if not new_content:
        return {"ok": False, "error": "new_content required"}

    current = proj.get("llm_context", "") or ""
    user_ctx = proj.get("user_context", "") or ""

    prompt = (
        f"You maintain the rolling LLM-context for project '{proj.get('name', slug)}'.\n"
        f"This context is loaded into future LLM calls about the project, so keep it "
        f"USEFUL and CONCISE — preserve key learnings, current state, open threads, "
        f"and concrete next steps. Drop stale or superseded info.\n\n"
        f"User-provided context (DO NOT modify; for reference):\n{user_ctx[:2000] or '(none)'}\n\n"
        f"Current LLM-context (you will rewrite this):\n{current[:6000] or '(empty)'}\n\n"
        f"NEW activity from {source}:\n{new_content[:6000]}\n\n"
        "Write the UPDATED LLM-context as concise markdown with these sections:\n"
        "  ## Current state\n"
        "  ## Key learnings\n"
        "  ## Open threads\n"
        "  ## Next steps\n\n"
        "Aim for 200–600 words. Reflect what's actually known — don't invent. "
        "Output the markdown directly with no preamble."
    )
    sys_prompt = (
        "You maintain rolling project context. Write tight, factual markdown. "
        "Never invent activity — only reflect what's in the input. "
        "If the new content is thin or empty, preserve current context with minimal change."
    )
    new_ctx = await _llm_generate(prompt, system=sys_prompt)
    if not new_ctx or len(new_ctx) < 30:
        return {"ok": False, "error": "LLM returned empty or too-short context",
                "raw": new_ctx[:200]}

    # Strip markdown code fences if the LLM wrapped its output
    new_ctx = new_ctx.strip()
    if new_ctx.startswith("```"):
        # Remove first line (```markdown or ```) and last line (```)
        lines = new_ctx.splitlines()
        if len(lines) > 2:
            new_ctx = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    proj["llm_context"] = new_ctx
    proj["summary_updated_at"] = now_iso()
    await _save_project(proj)
    await emit_event({"type": "project.context.updated", "slug": slug,
                      "source": source, "chars": len(new_ctx)})
    return {"ok": True, "llm_context": new_ctx, "chars": len(new_ctx)}


@capability(
    "project.note.add", memory="off",
    http_method="POST", http_path="/dream/projects/note/add", http_tags=["project"],
    description="Capture a piece of text (e.g. a chat message) into a project, folding "
                "it into the project's rolling llm_context via an incremental LLM update "
                "(same path dream cycles use). Creates the project first when `name` is "
                "given and no matching project exists, so 'add this to a new project' is "
                "one call. Inputs: content (str!), slug (str — existing project), name "
                "(str — find/create by name when slug omitted), source (str — label, "
                "default 'chat'). Output: {ok, slug, name, created, llm_context}.",
)
async def project_note_add(content: str = "", slug: str = "", name: str = "",
                           source: str = "chat", trace_id=None):
    if not (content or "").strip():
        return {"ok": False, "error": "content required"}
    slug = (slug or "").strip()
    name = (name or "").strip()
    created = False
    proj = await _get_project(slug) if slug else None
    if not proj and name:
        # Resolve by slugified name; create the project if it doesn't exist yet.
        cand = _slugify(name)
        proj = await _get_project(cand)
        if not proj:
            res = await project_upsert(name=name)
            proj = res.get("project") if isinstance(res, dict) else None
            created = bool(proj)
        slug = (proj or {}).get("slug", cand)
    if not proj:
        return {"ok": False, "error": "provide an existing slug or a name to create"}
    slug = proj.get("slug", slug)
    upd = await project_context_update(slug=slug, new_content=content, source=source)
    await emit_event({"type": "project.note.added", "slug": slug,
                      "source": source, "created": created, "chars": len(content)})
    return {"ok": bool(upd.get("ok")), "slug": slug, "name": proj.get("name"),
            "created": created, "llm_context": upd.get("llm_context", ""),
            **({"error": upd["error"]} if upd.get("error") else {})}


@capability(
    "project.context.regenerate", memory="off",
    http_method="POST", http_path="/dream/projects/context/regenerate", http_tags=["project"],
    description="Fully regenerate a project's llm_context from scratch by surveying linked "
                "resources (fabric datasets, recent dream cycles, notebooks). Use sparingly "
                "— this calls many caps and runs a long LLM synthesis. Inputs: slug (str!).",
)
async def project_context_regenerate(slug: str, trace_id=None):
    proj = await _get_project(slug)
    if not proj:
        return {"error": f"project not found: {slug}"}

    samples: List[str] = []

    # Sample fabric datasets
    fab_q = CAPABILITY_REGISTRY.get("fabric.query")
    for ds in (proj.get("fabric_dataset_ids", []) or [])[:5]:
        if not fab_q:
            break
        try:
            res = await fab_q["func"](query=json.dumps({
                "dataset_id": ds, "top_k": 5, "include_data": True, "cache": False,
            }))
            if isinstance(res, dict):
                rows = (res.get("results") or [])[:5]
                for row in rows:
                    if isinstance(row, dict):
                        samples.append(f"[fabric:{ds}] " + (row.get("text") or "")[:300])
        except Exception:
            continue

    # Sample notebooks
    nb_list = CAPABILITY_REGISTRY.get("notebook.list") or CAPABILITY_REGISTRY.get("notebook.search")
    for nb in (proj.get("notebook_ids", []) or [])[:10]:
        if not nb_list:
            break
        try:
            res = await nb_list["func"](limit=5)
            if isinstance(res, dict):
                for it in (res.get("notebooks") or res.get("results") or [])[:3]:
                    if isinstance(it, dict):
                        samples.append(f"[notebook:{nb}] " + str(it.get("title") or it.get("text") or "")[:200])
        except Exception:
            continue

    # Recent dream cycles for this project
    r = _redis()
    if r:
        try:
            zkey = f"{KEY_PROJECT_DREAMS}:{slug}"
            ids = await r.zrevrange(zkey, 0, 4)
            for cid in ids or []:
                cid_str = cid.decode() if isinstance(cid, bytes) else cid
                histkey = f"{KEY_PROJECT_HISTORY}:{slug}"
                rep_raw = await r.hget(histkey, cid_str)
                if rep_raw:
                    try:
                        rec = json.loads(rep_raw if isinstance(rep_raw, str) else rep_raw.decode())
                        samples.append(f"[dream:{rec.get('trigger', '?')}] " + (rec.get("title") or "")[:100] +
                                       " — " + (rec.get("report") or "")[:300])
                    except Exception:
                        continue
        except Exception:
            pass

    samples_text = "\n\n".join(samples[:30]) or "(no resources sampled)"
    user_ctx = proj.get("user_context", "")
    prompt = (
        f"You are building the LLM-context for project '{proj.get('name', slug)}'.\n"
        f"Description: {proj.get('description', '')}\n\n"
        f"User-provided context:\n{user_ctx[:3000] or '(none)'}\n\n"
        f"Sample of linked resources and recent activity:\n{samples_text}\n\n"
        "Write a concise project state document in markdown with sections:\n"
        "  ## Current state\n"
        "  ## Key learnings\n"
        "  ## Open threads\n"
        "  ## Next steps\n\n"
        "300–800 words. Ground every claim in the data above — don't invent."
    )
    new_ctx = await _llm_generate(prompt,
        system="You build factual project context. Markdown only.")
    if not new_ctx or len(new_ctx) < 50:
        return {"ok": False, "error": "regeneration produced no useful output"}

    proj["llm_context"] = new_ctx.strip()
    proj["summary_updated_at"] = now_iso()
    await _save_project(proj)
    await emit_event({"type": "project.context.regenerated", "slug": slug,
                      "samples": len(samples), "chars": len(new_ctx)})
    return {"ok": True, "llm_context": new_ctx, "chars": len(new_ctx),
            "samples_used": len(samples)}


# ─────────────────────────────────────────────────────────────────────────────
# DREAM INTEGRATION
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "project.dream.run", memory="off",
    http_method="POST", http_path="/dream/projects/dream", http_tags=["project"],
    description="Fire a dream cycle scoped to a project. Assembles project context as the "
                "seed, then invokes the project's first attached dream trigger (or one named "
                "explicitly). Default mode is the automated, productive COMPOSITE pipeline "
                "(deep context -> execute -> synthesise -> journal -> pivot). "
                "mode='action' runs the single-step action variant; mode='reflect' runs "
                "read-only reflection. Inputs: slug (str!), trigger_name (str, optional), "
                "goal (str, optional — overrides project description as focus), "
                "mode (compose|action|reflect, default compose), "
                "action (bool, legacy alias for mode='action').",
)
async def project_dream_run(slug: str, trigger_name: str = "",
                             goal: str = "", mode: str = "",
                             action: bool = False, trace_id=None):
    proj = await _get_project(slug)
    if not proj:
        return {"ok": False, "error": f"project not found: {slug}"}

    # Resolve mode (legacy: action=True -> mode='action')
    mode = (mode or ("action" if action else "compose")).lower()
    _MODE_TRIGGER = {
        "compose": "project_compose",
        "action":  "project_action",
        "reflect": "project_reflect",
    }

    # Determine which trigger to fire
    tname = trigger_name or (proj.get("dream_trigger_names", [""])[0] if proj.get("dream_trigger_names") else "")
    if not tname:
        # Default to the composite productive pipeline
        tname = _MODE_TRIGGER.get(mode, "project_compose")
    cycle_run = CAPABILITY_REGISTRY.get("dream.cycle.run")
    if not cycle_run:
        return {"ok": False, "error": "dream.cycle.run not available"}

    # Assemble seed
    assemble = await project_context_assemble(slug=slug, goal=goal)
    if assemble.get("error"):
        return {"ok": False, "error": assemble["error"]}
    seed = assemble["seed"]

    # Pre-assign this cycle's id and register the project->cycle link NOW, before
    # the cycle runs. dream.cycle.run launches the cycle detached (it returns no
    # cycle_id), so recording after it returns never worked; more importantly, a
    # link recorded up front lets the goals UI re-attach to this cycle's agentic
    # loops (session `dream:{cid}:{stage}`) while they're still live. _run_cycle
    # honours seed["cycle_id"].
    import uuid as _uuid
    cid = _uuid.uuid4().hex[:8]
    seed = dict(seed or {})
    seed["cycle_id"] = cid

    r = _redis()
    if r:
        try:
            await r.zadd(f"{KEY_PROJECT_DREAMS}:{slug}", {cid: time.time()})
        except Exception:
            pass
    proj["dream_count"] = int(proj.get("dream_count", 0)) + 1
    proj["last_dream_at"] = now_iso()
    await _save_project(proj)

    try:
        result = await cycle_run["func"](trigger_name=tname, seed=seed)
    except Exception as e:
        return {"ok": False, "error": f"dream.cycle.run failed: {e}"}

    await emit_event({"type": "project.dream.started", "slug": slug,
                      "trigger": tname, "cycle_id": cid})
    return {"ok": True, "cycle_id": cid, "trigger": tname,
            "seed_chars": assemble.get("context_chars"), "result": result}


@capability(
    "project.advance.v8", memory="on",
    http_method="POST", http_path="/dream/projects/advance_v8", http_tags=["project", "dag"],
    description="Advance a project/goal via the V8 loop-program orchestrator: builds a "
                "brief from the project's own context (objective, notes, progress) and "
                "creates a V8 PROGRAM (generated v5/v6/v7 loops run and adapt over the "
                "horizon). The project is tagged v8-program:<id> and its dream triggers "
                "are cleared so dream cycles REVIEW progress instead of re-executing. "
                "Inputs: slug (str!), horizon_days (int — 0 = generator's choice), "
                "keep_dream_trigger (bool default False). Output: {ok, program, slug}.",
)
async def project_advance_v8(slug: str, horizon_days: int = 0,
                             keep_dream_trigger: bool = False, trace_id=None):
    proj = await _get_project(slug)
    if not proj:
        return {"ok": False, "error": f"project not found: {slug}"}
    creator = CAPABILITY_REGISTRY.get("loops.program.create")
    if not creator or not creator.get("func"):
        return {"ok": False, "error": "V8 orchestrator (loops.program.create) not available"}
    # Already driven by a program? Point at it instead of forking a second one.
    for t in (proj.get("tags") or []):
        if str(t).startswith("v8-program:"):
            return {"ok": False, "error": f"project already driven by {t} — "
                    "delete that program first to re-generate",
                    "program_id": str(t).split(":", 1)[1]}
    brief = (f"PROJECT: {proj.get('name')}\n"
             f"OBJECTIVE: {proj.get('description') or proj.get('user_context') or ''}\n\n"
             + (f"CURRENT STATE / PROGRESS:\n{(proj.get('llm_context') or '')[:3000]}\n\n"
                if proj.get("llm_context") else "")
             + "Advance this project toward its objective.")
    try:
        # Share the goal's sandbox container across every constituent run of
        # the program (one container per goal — no per-run junk containers).
        prog = await creator["func"](brief=brief, horizon_days=int(horizon_days),
                                     autostart=True,
                                     sandbox_owner=f"goal-{slug}",
                                     owner_ref=slug)
    except Exception as e:
        return {"ok": False, "error": f"program creation failed: {e}"}
    if not isinstance(prog, dict) or not prog.get("id"):
        return {"ok": False, "error": "generator produced no program", "detail": prog}
    tags = [t for t in (proj.get("tags") or [])] + [f"v8-program:{prog['id']}"]
    proj["tags"] = tags
    if not keep_dream_trigger:
        proj["dream_trigger_names"] = []
    proj["llm_context"] = ((proj.get("llm_context") or "")
                           + f"\n\nEXECUTION: driven by V8 loop program {prog['id']} "
                             "(loops.program.get for state) — dream cycles should REVIEW "
                             "its progress, not re-execute the plan.")[:12000]
    await _save_project(proj)
    await emit_event({"type": "project.advance.v8", "slug": slug,
                      "program": prog["id"]})
    return {"ok": True, "slug": slug, "program": prog}


@capability(
    "project.dream.history", memory="off", silent=True,
    http_method="GET", http_path="/dream/projects/dreams", http_tags=["project"],
    description="Recent dream cycles linked to a project. Inputs: slug (str!), limit (int).",
)
async def project_dream_history(slug: str, limit: int = 10, trace_id=None):
    r = _redis()
    if not r or not slug:
        return {"cycles": []}
    try:
        zkey = f"{KEY_PROJECT_DREAMS}:{slug}"
        ids = await r.zrevrange(zkey, 0, int(limit) - 1, withscores=True)
        cycles = []
        for cid, ts in ids or []:
            cid_str = cid.decode() if isinstance(cid, bytes) else cid
            cycles.append({"cycle_id": cid_str, "ts": float(ts)})
        # Try to enrich each with the dream history record
        hist_cap = CAPABILITY_REGISTRY.get("dream.history")
        if hist_cap and cycles:
            try:
                full = await hist_cap["func"](limit=200)
                if isinstance(full, dict):
                    by_cid = {h.get("cycle_id"): h for h in (full.get("history") or [])}
                    for c in cycles:
                        meta = by_cid.get(c["cycle_id"])
                        if meta:
                            c.update({"title": meta.get("title"), "trigger": meta.get("trigger"),
                                      "ended_at": meta.get("ended_at"),
                                      "elapsed_s": meta.get("elapsed_s")})
            except Exception:
                pass
        return {"cycles": cycles, "count": len(cycles)}
    except Exception as e:
        return {"cycles": [], "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# PER-PROJECT THOUGHT AREA — thinking loops scoped to this project
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "project.thoughts.list", memory="off", silent=True,
    http_method="GET", http_path="/dream/projects/thoughts", http_tags=["project", "think"],
    description="List the thinking loops (thoughts) scoped to a project — its own "
                "thought area. Reuses dream.think.list filtered by project. "
                "Inputs: slug (str!). Output: {thoughts:[...], count}.",
)
async def project_thoughts_list(slug: str = "", trace_id=None):
    if not slug:
        return {"thoughts": [], "count": 0, "error": "slug required"}
    think_list = CAPABILITY_REGISTRY.get("dream.think.list")
    if not think_list:
        return {"thoughts": [], "count": 0, "error": "dream.think.list unavailable"}
    try:
        res = await think_list["func"](project_slug=slug)
    except Exception as e:
        return {"thoughts": [], "count": 0, "error": str(e)}
    thoughts = res.get("thoughts", []) if isinstance(res, dict) else []
    return {"slug": slug, "thoughts": thoughts, "count": len(thoughts)}


# ─────────────────────────────────────────────────────────────────────────────
# RESOURCE BROWSING — view actual content of linked resources
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "project.search_targets", memory="off", silent=True,
    http_method="GET", http_path="/dream/projects/search_targets", http_tags=["project"],
    description="Searchable targets for project multi-select pickers. "
                "Inputs: type (fabric|notebook|memory|trigger|workspace), "
                "query (str, optional), limit (int, default 30).",
)
async def project_search_targets(type: str = "fabric", query: str = "",
                                   limit: int = 30, trace_id=None):
    q = (query or "").lower().strip()
    items: List[Dict[str, Any]] = []

    if type == "fabric":
        fab_ds = CAPABILITY_REGISTRY.get("fabric.datasets")
        if fab_ds:
            try:
                res = await fab_ds["func"]()
                rows = res.get("datasets", []) if isinstance(res, dict) else (res or [])
                for d in rows:
                    did = d.get("dataset_id") or d.get("id") or d.get("name") or ""
                    if not did:
                        continue
                    if q and q not in did.lower():
                        continue
                    items.append({
                        "id":    did,
                        "label": did,
                        "meta":  f"{d.get('record_count', d.get('count', 0))} records",
                    })
            except Exception as e:
                return {"error": str(e), "items": []}

    elif type == "notebook":
        # Notebooks are served by researcher_api on a different port via /api/notebooks.
        # Try the HTTP proxy first; fall back to any local notebook.* cap.
        try:
            import os as _os
            import httpx
            researcher_url = _os.getenv("VERA_RESEARCHER_URL", "http://localhost:8765")
            async with httpx.AsyncClient(timeout=8.0) as c:
                resp = await c.get(f"{researcher_url}/api/notebooks", params={"limit": int(limit) * 2})
                if resp.status_code == 200:
                    data = resp.json()
                    rows = data if isinstance(data, list) else (data.get("notebooks") or data.get("results") or [])
                    for n in rows:
                        if not isinstance(n, dict):
                            continue
                        nid = n.get("id") or n.get("notebook_id") or n.get("slug") or ""
                        title = n.get("title") or n.get("name") or nid
                        if q and q not in (str(nid) + " " + str(title)).lower():
                            continue
                        items.append({
                            "id":    str(nid),
                            "label": str(title),
                            "meta":  (n.get("created_at") or n.get("updated_at") or "")[:10] +
                                     (f" · {n.get('cell_count', 0)} cells" if "cell_count" in n else ""),
                        })
        except Exception as e:
            log.debug("project notebook search via researcher_api: %s", e)
        # Fallback: any cap named notebook.list
        if not items:
            nb_list = (CAPABILITY_REGISTRY.get("notebook.list") or
                       CAPABILITY_REGISTRY.get("research.notebook.list"))
            if nb_list:
                try:
                    res = await nb_list["func"](limit=int(limit) * 2)
                    rows = []
                    if isinstance(res, dict):
                        rows = res.get("notebooks") or res.get("results") or res.get("items") or []
                    for n in rows:
                        if not isinstance(n, dict):
                            continue
                        nid = n.get("id") or n.get("notebook_id") or ""
                        title = n.get("title") or n.get("name") or nid
                        if q and q not in (str(nid) + " " + str(title)).lower():
                            continue
                        items.append({"id": str(nid), "label": str(title),
                                      "meta": (n.get("created_at") or n.get("updated_at") or "")[:10]})
                except Exception as e:
                    log.debug("project notebook fallback: %s", e)

    elif type == "memory":
        mem_search = CAPABILITY_REGISTRY.get("memory.search")
        if mem_search and q:
            try:
                res = await mem_search["func"](query=q, limit=int(limit))
                rows = []
                if isinstance(res, dict):
                    rows = res.get("results") or []
                for r2 in rows:
                    rec = r2.get("record") if isinstance(r2, dict) and "record" in r2 else r2
                    if not isinstance(rec, dict):
                        continue
                    items.append({
                        "id":    rec.get("id"),
                        "label": (rec.get("text") or rec.get("summary") or "")[:120],
                        "meta":  rec.get("category", "") or rec.get("source_type", ""),
                    })
            except Exception as e:
                return {"error": str(e), "items": []}
        elif not q:
            mem_recent = CAPABILITY_REGISTRY.get("memory.all_nodes")
            if mem_recent:
                try:
                    res = await mem_recent["func"](limit=int(limit))
                    rows = res.get("nodes") or res.get("records") or [] if isinstance(res, dict) else []
                    for rec in rows:
                        if not isinstance(rec, dict):
                            continue
                        items.append({
                            "id":    rec.get("id"),
                            "label": (rec.get("text") or rec.get("summary") or "")[:120],
                            "meta":  rec.get("category", "") or "",
                        })
                except Exception:
                    pass

    elif type == "trigger":
        list_t = CAPABILITY_REGISTRY.get("dream.trigger.list")
        if list_t:
            try:
                res = await list_t["func"]()
                for t in (res.get("triggers", []) if isinstance(res, dict) else []):
                    name = t.get("name", "")
                    label = t.get("label") or name
                    if q and q not in (name + " " + label).lower():
                        continue
                    items.append({
                        "id":    name, "label": label,
                        "meta":  t.get("mode", "") + (" • on" if t.get("enabled") else " • off"),
                    })
            except Exception:
                pass

    elif type == "workspace":
        # Correct cap name is ide.workspace.list (singular workspace, then .list)
        ws_list = (CAPABILITY_REGISTRY.get("ide.workspace.list") or
                   CAPABILITY_REGISTRY.get("ide.workspaces") or
                   CAPABILITY_REGISTRY.get("ide.list_workspaces"))
        if ws_list:
            try:
                res = await ws_list["func"]()
                rows = []
                if isinstance(res, dict):
                    rows = res.get("workspaces", []) or res.get("results", []) or []
                elif isinstance(res, list):
                    rows = res
                for w in rows:
                    if isinstance(w, dict):
                        name = w.get("name") or w.get("id") or ""
                        path = w.get("path") or ""
                    else:
                        name = str(w); path = ""
                    if not name:
                        continue
                    if q and q not in (str(name) + " " + str(path)).lower():
                        continue
                    items.append({
                        "id":    str(name),
                        "label": str(name),
                        "meta":  str(path)[:60] if path else "",
                    })
            except Exception as e:
                log.debug("project workspace search: %s", e)

    return {"items": items[:int(limit)], "type": type, "query": query}


@capability(
    "project.browse_resources", memory="off", silent=True,
    http_method="POST", http_path="/dream/projects/browse_resources", http_tags=["project"],
    description="Browse the actual content of a project's linked resources. "
                "Inputs: slug (str!), resource_type (fabric|notebook|memory|chat|dream — "
                "default 'all'), limit (int, default 30 per type).",
)
async def project_browse_resources(
    slug: str,
    resource_type: str = "all",
    limit: int = 30,
    trace_id=None,
):
    proj = await _get_project(slug)
    if not proj:
        return {"error": f"project not found: {slug}"}

    out: Dict[str, Any] = {"slug": slug, "name": proj.get("name")}

    if resource_type in ("fabric", "all"):
        fab_q = CAPABILITY_REGISTRY.get("fabric.query")
        fab_items: List[Dict[str, Any]] = []
        for did in (proj.get("fabric_dataset_ids", []) or []):
            if not fab_q:
                break
            try:
                res = await fab_q["func"](query=json.dumps({
                    "dataset_id": did, "top_k": int(limit), "include_data": True, "cache": False,
                }))
                if isinstance(res, dict):
                    for row in (res.get("results") or [])[:int(limit)]:
                        if isinstance(row, dict):
                            fab_items.append({
                                "id":       row.get("id"),
                                "dataset":  did,
                                "text":     (row.get("text") or "")[:600],
                                "ts":       row.get("created_at", ""),
                            })
            except Exception:
                continue
        out["fabric"] = fab_items

    if resource_type in ("notebook", "all"):
        nb_items: List[Dict[str, Any]] = []
        # Notebooks live in researcher_api at /api/notebooks/{id}
        try:
            import os as _os
            import httpx
            researcher_url = _os.getenv("VERA_RESEARCHER_URL", "http://localhost:8765")
            async with httpx.AsyncClient(timeout=8.0) as c:
                for nid in (proj.get("notebook_ids", []) or [])[:int(limit)]:
                    if not nid:
                        continue
                    try:
                        resp = await c.get(f"{researcher_url}/api/notebooks/{nid}")
                        if resp.status_code == 200:
                            d = resp.json()
                            cells = d.get("cells", []) if isinstance(d, dict) else []
                            preview = "\n\n".join(
                                (c.get("content") or c.get("text") or "")[:300]
                                for c in cells[:5] if isinstance(c, dict)
                            )[:1500]
                            nb_items.append({
                                "id":      str(nid),
                                "title":   d.get("title", ""),
                                "content": preview,
                                "cell_count": len(cells),
                            })
                        else:
                            nb_items.append({"id": str(nid), "error": f"http {resp.status_code}"})
                    except Exception as e:
                        nb_items.append({"id": str(nid), "error": str(e)[:80]})
        except Exception as e:
            log.debug("project notebook browse: %s", e)
        out["notebook"] = nb_items

    if resource_type in ("memory", "all"):
        mem_get = CAPABILITY_REGISTRY.get("memory.get")
        mem_items = []
        for mid in (proj.get("memory_ids", []) or [])[:int(limit)]:
            if mem_get:
                try:
                    res = await mem_get["func"](id=mid)
                    if isinstance(res, dict) and not res.get("error"):
                        mem_items.append({
                            "id": mid,
                            "text": (res.get("text") or res.get("summary") or "")[:600],
                            "category": res.get("category", ""),
                            "ts": res.get("created_at", ""),
                        })
                except Exception:
                    mem_items.append({"id": mid, "error": "fetch failed"})
            else:
                mem_items.append({"id": mid})
        out["memory"] = mem_items

    if resource_type in ("chat", "all"):
        chat_items = []
        for cid in (proj.get("chat_ids", []) or [])[:int(limit)]:
            chat_items.append({"id": cid})
        out["chat"] = chat_items

    if resource_type in ("dream", "all"):
        r = _redis()
        dream_items = []
        if r:
            try:
                zkey = f"{KEY_PROJECT_DREAMS}:{slug}"
                ids = await r.zrevrange(zkey, 0, int(limit) - 1)
                histkey = f"{KEY_PROJECT_HISTORY}:{slug}"
                for cid in ids or []:
                    cid_str = cid.decode() if isinstance(cid, bytes) else cid
                    rep_raw = await r.hget(histkey, cid_str)
                    if rep_raw:
                        try:
                            d = json.loads(rep_raw if isinstance(rep_raw, str) else rep_raw.decode())
                            dream_items.append({
                                "cycle_id": cid_str, "trigger": d.get("trigger"),
                                "title": d.get("title"), "ts": d.get("ts"),
                                "report_excerpt": (d.get("report") or "")[:800],
                            })
                        except Exception:
                            continue
            except Exception:
                pass
        out["dream"] = dream_items

    return out


# ─────────────────────────────────────────────────────────────────────────────
# CONVERSATIONAL CHAT WITH PROJECT CONTEXT
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "project.chat", memory="off",
    http_method="POST", http_path="/dream/projects/chat", http_tags=["project"],
    description="Have a conversation grounded in a project's full context. "
                "The LLM receives the project's user_context, llm_context, summary, "
                "and resource manifest, plus the current message and any history. "
                "Inputs: slug (str!), message (str!), history (JSON list, optional), "
                "include_recent_dreams (bool, default true).",
)
async def project_chat(
    slug: str,
    message: str = "",
    history: Optional[Any] = None,
    include_recent_dreams: bool = True,
    trace_id=None,
):
    if not slug or not message:
        return {"error": "slug and message required"}
    proj = await _get_project(slug)
    if not proj:
        return {"error": f"project not found: {slug}"}

    # Parse history if string
    if isinstance(history, str):
        try:
            history = json.loads(history)
        except Exception:
            history = []
    if not isinstance(history, list):
        history = []

    # Build context
    ctx_parts = [f"PROJECT: {proj.get('name', slug)}"]
    if proj.get("description"):
        ctx_parts.append(f"DESCRIPTION: {proj['description']}")
    if proj.get("user_context"):
        ctx_parts.append(f"USER CONTEXT (background):\n{proj['user_context']}")
    if proj.get("llm_context"):
        ctx_parts.append(f"CURRENT STATE:\n{proj['llm_context']}")
    if proj.get("summary") and proj["summary"] != proj.get("llm_context"):
        ctx_parts.append(f"ROLLING SUMMARY:\n{proj['summary']}")

    # Resource manifest
    res_lines = []
    if proj.get("fabric_dataset_ids"):
        res_lines.append(f"- Fabric datasets: {', '.join(proj['fabric_dataset_ids'])}")
    if proj.get("notebook_ids"):
        res_lines.append(f"- Notebooks: {', '.join(proj['notebook_ids'])}")
    if proj.get("ide_workspaces"):
        res_lines.append(f"- IDE workspaces: {', '.join(proj['ide_workspaces'])}")
    if proj.get("memory_ids"):
        res_lines.append(f"- Pinned memories: {len(proj['memory_ids'])}")
    if res_lines:
        ctx_parts.append("RESOURCES AVAILABLE:\n" + "\n".join(res_lines))

    # Recent dream cycles
    if include_recent_dreams:
        r = _redis()
        if r:
            try:
                zkey = f"{KEY_PROJECT_DREAMS}:{slug}"
                ids = await r.zrevrange(zkey, 0, 3)
                histkey = f"{KEY_PROJECT_HISTORY}:{slug}"
                dream_lines = []
                for cid in ids or []:
                    cid_str = cid.decode() if isinstance(cid, bytes) else cid
                    rep_raw = await r.hget(histkey, cid_str)
                    if rep_raw:
                        try:
                            d = json.loads(rep_raw if isinstance(rep_raw, str) else rep_raw.decode())
                            dream_lines.append(
                                f"- {d.get('trigger', '?')} ({d.get('ts', '')[:10]}): "
                                f"{(d.get('title') or '')[:80]}"
                            )
                        except Exception:
                            continue
                if dream_lines:
                    ctx_parts.append("RECENT DREAM CYCLES:\n" + "\n".join(dream_lines))
            except Exception:
                pass

    full_ctx = "\n\n".join(ctx_parts)

    history_text = ""
    for turn in (history or [])[-10:]:
        if isinstance(turn, dict):
            role = turn.get("role", "user")
            content = str(turn.get("content", ""))[:1500]
            history_text += f"\n[{role}] {content}"

    prompt = (
        f"You are Vera, having a conversation about a project. Use the project "
        f"context below. Be concrete and grounded — reference what's actually in "
        f"the context. If the user asks something the context doesn't cover, say "
        f"so and suggest what would help (e.g. 'I'd need to see X — should I run "
        f"a dream cycle or fetch from Y?').\n\n"
        f"{full_ctx}\n"
        f"{history_text}\n\n"
        f"[user] {message}"
    )
    reply = await _llm_generate(
        prompt,
        system="You converse about projects, grounded in their context. Concrete, helpful, never inventing facts.",
    )
    await emit_event({"type": "project.chat.message", "slug": slug,
                      "user_chars": len(message), "reply_chars": len(reply or "")})
    return {
        "ok":      True,
        "slug":    slug,
        "name":    proj.get("name"),
        "reply":   reply,
        "context_chars": len(full_ctx),
    }


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-UPDATE HOOK — called from dream cycle completion
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "project.dream.complete_hook", memory="off", silent=True,
    description="Internal: called when a dream cycle completes for a project. "
                "Records the cycle in the project's dream history and triggers an "
                "incremental llm_context update. Inputs: slug (str!), cycle_id (str), "
                "trigger (str), report (str).",
)
async def project_dream_complete_hook(slug: str, cycle_id: str = "",
                                       trigger: str = "", report: str = "",
                                       steps: str = "", engine: str = "",
                                       goal: str = "", trace_id=None):
    proj = await _get_project(slug)
    if not proj:
        return {"ok": False, "error": "project not found"}
    r = _redis()
    if r and cycle_id:
        try:
            await r.hset(f"{KEY_PROJECT_HISTORY}:{slug}", cycle_id, json.dumps({
                "cycle_id": cycle_id, "trigger": trigger,
                "title": (report or "").splitlines()[0].lstrip("# ").strip()[:120] if report else "",
                "report": report[:5000],
                "ts": now_iso(),
            }))
        except Exception:
            pass
    # Persist the dream cycle as a structured loop run (report + step trace become
    # artifacts) so the project page shows its full loop history alongside the
    # session escalations. Best-effort; never blocks context update.
    try:
        await project_loop_record(
            slug=slug, source="dream_cycle", engine=engine or "dream",
            goal=goal or trigger,
            steps=steps if isinstance(steps, str) else json.dumps(steps or [], default=str),
            final=report or "", cycle_id=cycle_id, trigger=trigger, run_id=cycle_id)
    except Exception as e:
        log.debug("project dream loop record: %s", e)
    if report:
        await project_context_update(
            slug=slug, new_content=report,
            source=f"dream cycle {trigger}" if trigger else "dream cycle")
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# GIT REPO CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

async def _git_run_async(repo_path: str, args: List[str], timeout: int = 30) -> Dict[str, Any]:
    """Async wrapper for _git_run — runs the blocking subprocess in a thread so
    it NEVER blocks the event loop. A git pull/push/clone is synchronous and can
    take tens of seconds (clone: minutes); calling _git_run directly from an
    async capability froze the whole event loop for that duration, which stalled
    every WebSocket (the whole-UI 1005/1006 reconnect flap). Always prefer this
    from async code."""
    import functools as _ft
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _ft.partial(_git_run, repo_path, args, timeout))


def _git_run(repo_path: str, args: List[str], timeout: int = 30) -> Dict[str, Any]:
    """Run a git command in repo_path. Returns {ok, stdout, stderr, returncode}.

    BLOCKING — do not call directly from async code; use _git_run_async so the
    subprocess runs off the event loop.
    """
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode,
        }
    except FileNotFoundError:
        return {"ok": False, "stdout": "", "stderr": "git not found in PATH", "returncode": -1}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "git command timed out", "returncode": -1}
    except Exception as e:
        return {"ok": False, "stdout": "", "stderr": str(e), "returncode": -1}


@capability(
    "project.git.status", memory="off", silent=True,
    http_method="GET", http_path="/dream/projects/git/status", http_tags=["project", "git"],
    description="Get git status and recent log for a repo linked to a project. "
                "Inputs: slug (str!), repo_name (str, optional — first repo if omitted). "
                "Output: {ok, name, path, branch, status, log, remote_url}.",
)
async def project_git_status(slug: str, repo_name: str = "", trace_id=None):
    proj = await _get_project(slug)
    if not proj:
        return {"ok": False, "error": "project not found"}
    repos = proj.get("git_repos") or []
    if not repos:
        return {"ok": False, "error": "no git repos linked to this project"}
    repo = next((r for r in repos if r.get("name") == repo_name), repos[0]) if repo_name else repos[0]
    path = repo.get("path", "")
    if not path or not Path(path).is_dir():
        return {"ok": False, "error": f"repo path not found: {path}"}

    status = await _git_run_async(path, ["status", "--short", "--branch"])
    log_r  = await _git_run_async(path, ["log", "--oneline", "--decorate", "-20"])
    remote = await _git_run_async(path, ["remote", "get-url", "origin"])
    branch = await _git_run_async(path, ["rev-parse", "--abbrev-ref", "HEAD"])

    return {
        "ok":         True,
        "name":       repo.get("name", path),
        "path":       path,
        "branch":     branch["stdout"] if branch["ok"] else "",
        "status":     status["stdout"] if status["ok"] else status["stderr"],
        "log":        log_r["stdout"] if log_r["ok"] else "",
        "remote_url": remote["stdout"] if remote["ok"] else repo.get("remote_url", ""),
    }


@capability(
    "project.git.pull", memory="off",
    http_method="POST", http_path="/dream/projects/git/pull", http_tags=["project", "git"],
    description="Run git pull on a repo linked to a project. "
                "Inputs: slug (str!), repo_name (str, optional). "
                "Output: {ok, output}.",
)
async def project_git_pull(slug: str, repo_name: str = "", trace_id=None):
    proj = await _get_project(slug)
    if not proj:
        return {"ok": False, "error": "project not found"}
    repos = proj.get("git_repos") or []
    if not repos:
        return {"ok": False, "error": "no git repos linked"}
    repo = next((r for r in repos if r.get("name") == repo_name), repos[0]) if repo_name else repos[0]
    path = repo.get("path", "")
    if not path or not Path(path).is_dir():
        return {"ok": False, "error": f"repo path not found: {path}"}
    r = await _git_run_async(path, ["pull"], timeout=60)
    return {"ok": r["ok"], "output": r["stdout"] or r["stderr"]}


@capability(
    "project.git.push", memory="off",
    http_method="POST", http_path="/dream/projects/git/push", http_tags=["project", "git"],
    description="Run git push on a repo linked to a project. "
                "Inputs: slug (str!), repo_name (str, optional), remote (str, default 'origin'), "
                "branch (str, optional — current branch if omitted). "
                "Output: {ok, output}.",
)
async def project_git_push(slug: str, repo_name: str = "", remote: str = "origin",
                           branch: str = "", trace_id=None):
    proj = await _get_project(slug)
    if not proj:
        return {"ok": False, "error": "project not found"}
    repos = proj.get("git_repos") or []
    if not repos:
        return {"ok": False, "error": "no git repos linked"}
    repo = next((r for r in repos if r.get("name") == repo_name), repos[0]) if repo_name else repos[0]
    path = repo.get("path", "")
    if not path or not Path(path).is_dir():
        return {"ok": False, "error": f"repo path not found: {path}"}
    args = ["push", remote]
    if branch:
        args.append(branch)
    r = await _git_run_async(path, args, timeout=60)
    return {"ok": r["ok"], "output": r["stdout"] or r["stderr"]}


@capability(
    "project.git.log", memory="off", silent=True,
    http_method="GET", http_path="/dream/projects/git/log", http_tags=["project", "git"],
    description="Get detailed git log for a project-linked repo. "
                "Inputs: slug (str!), repo_name (str, optional), limit (int, default 50). "
                "Output: {ok, log}.",
)
async def project_git_log(slug: str, repo_name: str = "", limit: int = 50, trace_id=None):
    proj = await _get_project(slug)
    if not proj:
        return {"ok": False, "error": "project not found"}
    repos = proj.get("git_repos") or []
    if not repos:
        return {"ok": False, "error": "no git repos linked"}
    repo = next((r for r in repos if r.get("name") == repo_name), repos[0]) if repo_name else repos[0]
    path = repo.get("path", "")
    if not path or not Path(path).is_dir():
        return {"ok": False, "error": f"repo path not found: {path}"}
    r = await _git_run_async(path, ["log", f"-{int(limit)}", "--pretty=format:%h %ad %an: %s", "--date=short"])
    return {"ok": r["ok"], "log": r["stdout"] if r["ok"] else r["stderr"]}


# ─────────────────────────────────────────────────────────────────────────────
# GIT URL PARSING + PROVISIONING (clone / Gitea mirror / data-fabric source)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_git_url(url: str) -> Dict[str, str]:
    """Best-effort parse of a git remote URL into host/owner/repo/provider.

    Handles https://host/owner/repo(.git), git@host:owner/repo.git, and nested
    groups (gitlab) by treating the last path segment as the repo and the one
    before it as the owner. provider is inferred from the host.
    """
    raw = (url or "").strip()
    host = owner = repo = ""
    if raw.startswith("git@") or (":" in raw and "//" not in raw and "@" in raw):
        # scp-style: git@host:owner/repo.git
        try:
            host = raw.split("@", 1)[1].split(":", 1)[0]
            path = raw.split(":", 1)[1]
        except Exception:
            path = ""
    else:
        from urllib.parse import urlparse
        u = raw if "//" in raw else ("https://" + raw)
        p = urlparse(u)
        host = p.hostname or ""
        path = (p.path or "").lstrip("/")
    path = re.sub(r"\.git$", "", path).strip("/")
    parts = [s for s in path.split("/") if s]
    if parts:
        repo = parts[-1]
        owner = parts[-2] if len(parts) >= 2 else ""
    h = host.lower()
    if "github.com" in h:
        provider, api_base = "github", "https://api.github.com"
    elif "gitlab" in h:
        provider, api_base = "gitlab", f"https://{host}"
    else:
        # self-hosted → assume Gitea/Forgejo-compatible API
        provider, api_base = "gitea", f"https://{host}" if host else ""
    return {"host": host, "owner": owner, "repo": repo,
            "provider": provider, "api_base": api_base}


async def _gitea_migrate(clone_url: str, repo_name: str, owner: str = "",
                         mirror: bool = True, private: bool = False) -> Dict[str, Any]:
    """Migrate (clone/mirror) a remote repo into the configured Gitea instance.
    Idempotent: an already-existing repo is treated as success."""
    base = (cfg.GITEA_BASE_URL or "").rstrip("/")
    token = cfg.GITEA_TOKEN or ""
    if not base or not token:
        return {"ok": False, "error": "Gitea not configured (set GITEA_BASE_URL + GITEA_TOKEN)"}
    target_owner = owner or cfg.GITEA_OWNER or ""
    body = {"clone_addr": clone_url, "repo_name": repo_name,
            "mirror": bool(mirror), "private": bool(private), "service": "git"}
    if target_owner:
        body["repo_owner"] = target_owner
    import httpx
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{base}/api/v1/repos/migrate",
                             headers={"Authorization": f"token {token}",
                                      "Content-Type": "application/json"},
                             json=body)
        if r.status_code in (200, 201):
            d = r.json()
            return {"ok": True, "html_url": d.get("html_url"),
                    "full_name": d.get("full_name"), "clone_url": d.get("clone_url")}
        if r.status_code == 409:
            who = target_owner or "?"
            return {"ok": True, "already_exists": True,
                    "html_url": f"{base}/{who}/{repo_name}"}
        return {"ok": False, "error": f"gitea {r.status_code}: {r.text[:300]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@capability(
    "project.git.provision", memory="off",
    http_method="POST", http_path="/dream/projects/git/provision", http_tags=["project", "git"],
    description="Provision a git repo for a project from a remote URL: optionally "
                "clone it locally and/or mirror it into the configured Gitea, and "
                "register a data-fabric source that ingests its commits/issues. "
                "Inputs: slug (str!), remote_url (str!), name (str, optional — "
                "derived from URL), branch (str, optional), mode "
                "('none'|'local'|'gitea'|'both', default 'local'), fabric (bool, "
                "default true — create a data-fabric git source), fabric_mode "
                "('commits'|'issues'|'repos', default 'commits'). "
                "Output: {ok, repo, steps}.",
)
async def project_git_provision(
    slug: str,
    remote_url: str,
    name: str = "",
    branch: str = "",
    mode: str = "local",
    fabric: bool = True,
    fabric_mode: str = "commits",
    trace_id=None,
):
    proj = await _get_project(slug)
    if not proj:
        return {"ok": False, "error": "project not found"}
    remote_url = (remote_url or "").strip()
    if not remote_url:
        return {"ok": False, "error": "remote_url required"}
    info = _parse_git_url(remote_url)
    repo_name = (name or info.get("repo") or "repo").strip()
    mode = (mode or "local").lower()
    steps: List[Dict[str, Any]] = []

    repos = list(proj.get("git_repos") or [])
    entry = next((r for r in repos if (r.get("remote_url") == remote_url
                                       or r.get("name") == repo_name)), None)
    if entry is None:
        entry = {"name": repo_name, "remote_url": remote_url}
        repos.append(entry)
    entry["name"] = repo_name
    entry["remote_url"] = remote_url
    entry["branch"] = branch or entry.get("branch", "")
    entry["provider"] = info.get("provider", "")
    entry["mode"] = mode

    # ── 1. local clone (modes: local, both) ──────────────────────────────────
    if mode in ("local", "both"):
        try:
            clone_root = Path(cfg.GIT_CLONE_ROOT) / _slugify(slug)
            clone_root.mkdir(parents=True, exist_ok=True)
            dest = clone_root / re.sub(r"[^A-Za-z0-9._-]", "_", repo_name)
            if (dest / ".git").is_dir():
                r = await _git_run_async(str(dest), ["pull"], timeout=120)
                steps.append({"step": "clone", "action": "pull",
                              "ok": r["ok"], "detail": r["stdout"] or r["stderr"]})
            else:
                args = ["clone"]
                if branch:
                    args += ["--branch", branch]
                args += [remote_url, str(dest)]
                r = await _git_run_async(str(clone_root), args, timeout=300)
                steps.append({"step": "clone", "action": "clone",
                              "ok": r["ok"], "detail": r["stdout"] or r["stderr"]})
            if (dest / ".git").is_dir():
                entry["path"] = str(dest)
        except Exception as e:
            steps.append({"step": "clone", "ok": False, "detail": str(e)})

    # ── 2. Gitea mirror (modes: gitea, both) ─────────────────────────────────
    if mode in ("gitea", "both"):
        g = await _gitea_migrate(remote_url, repo_name)
        steps.append({"step": "gitea", "ok": g.get("ok", False),
                      "detail": g.get("error") or g.get("html_url") or "migrated"})
        if g.get("ok") and g.get("html_url"):
            entry["gitea_url"] = g["html_url"]

    # ── 3. data-fabric git source ────────────────────────────────────────────
    if fabric:
        add = CAPABILITY_REGISTRY.get("fabric.sources.add")
        if not add:
            steps.append({"step": "fabric", "ok": False, "detail": "fabric.sources.add unavailable"})
        elif not (info.get("owner") and info.get("repo")):
            steps.append({"step": "fabric", "ok": False,
                          "detail": "could not derive owner/repo from URL"})
        else:
            ds_id = re.sub(r"[^a-zA-Z0-9_]", "_",
                           f"git_{info['provider']}_{info['owner']}_{info['repo']}")[:80]
            src_cfg = {"base_url": info["api_base"], "owner": info["owner"],
                       "repo": info["repo"], "mode": fabric_mode or "commits"}
            try:
                res = await add["func"](
                    url=remote_url, source_type=info["provider"],
                    label=f"{info['owner']}/{info['repo']} ({fabric_mode})",
                    dataset_id=ds_id, interval=3600,
                    tags=f"git,{info['provider']},project:{slug}",
                    config=json.dumps(src_cfg), limit=50,
                    id=f"gitsrc-{ds_id}",
                )
                real_ds = (res or {}).get("dataset_id", ds_id)
                entry["fabric_source_id"] = (res or {}).get("id")
                entry["dataset_id"] = real_ds
                # link the dataset to the project so cycles see it
                ds_ids = list(proj.get("fabric_dataset_ids") or [])
                if real_ds not in ds_ids:
                    ds_ids.append(real_ds)
                    proj["fabric_dataset_ids"] = ds_ids
                steps.append({"step": "fabric", "ok": True,
                              "detail": f"source {entry['fabric_source_id']} → dataset {real_ds}"})
            except Exception as e:
                steps.append({"step": "fabric", "ok": False, "detail": str(e)})

    proj["git_repos"] = repos
    proj["updated_at"] = now_iso()
    await _save_project(proj)
    await emit_event({"type": "project.git.provisioned", "slug": slug,
                      "repo": repo_name, "mode": mode,
                      "steps": [s.get("step") for s in steps if s.get("ok")]})
    return {"ok": True, "repo": entry, "steps": steps}


# ─────────────────────────────────────────────────────────────────────────────
# PROJECT-AWARE DEFAULT TRIGGER
# ─────────────────────────────────────────────────────────────────────────────

PROJECT_TRIGGER_DEFAULTS = {
    "name":         "project_reflect",
    "label":        "Project Reflection",
    "description":  "Autonomous agentic project reflection. Loads project context, "
                    "iterates through the available tools (memory, fabric, research, "
                    "IDE files & snapshots) to deepen understanding, then synthesises "
                    "concrete next steps. Runs WITHOUT user intervention by default.",
    "enabled":      True,
    "sensors":      ["dream.sensor.project_context",
                     "dream.sensor.memory_recent",
                     "dream.sensor.research_recent",
                     "dream.sensor.notebook_recent"],
    "pipeline":     ["dream.stage.gather", "dream.stage.themes",
                     "dream.stage.investigate",
                     "dream.stage.synthesize", "dream.stage.deliver"],
    "iterate":      {"enabled": True, "max_iterations": 10, "min_iterations": 3,
                     "convergence_min_new_findings": 1},
    "mode":         "stepwise",
    "depth":        "deep",
    "hitl":         False,             # autonomous — no per-step approval
    "hours_start":  0,
    "hours_end":    24,
    "min_idle_minutes":    20,
    "min_interval_minutes": 240,
    "require_signal":       0.0,        # always fire when invoked from a project
    "max_steps":            10,         # plenty of room to iterate
    "deliver_to":   ["notebook", "memory"],
    "sensor_params": {
        "research_recent": {"limit": 15, "full_content_top": 3},
        "memory_recent":   {"limit": 40},
    },
    # Caps the agentic loop is auto-approved to call (read-only by nature)
    "no_hitl_caps": [
        # Memory
        "memory.search", "memory.recall", "memory.all_nodes", "memory.get",
        "memory.session_history",
        # Fabric
        "fabric.query", "fabric.datasets", "fabric.search",
        "fabric.schema", "fabric.stats", "fabric.sources",
        # Research — full read access
        "research.history", "research.db.search", "research.bookmarks",
        "research.quick_search", "research.iterate.list",
        "research.job.status", "research.expand", "research.chat",
        # IDE — including snapshot reads via ide.fs.read on snapshot paths
        "ide.workspace.list", "ide.fs.list", "ide.fs.tree", "ide.fs.read",
        "ide.fs.roots",
        "ide.inspect.list_snapshots", "ide.inspect.source_info",
        "ide.inspect.diff_snapshot", "ide.inspect.review_file",
        # LLM analysis tools
        "llm.summarize", "llm.qa", "llm.analyze", "llm.explain",
        "llm.classify", "llm.brainstorm",
        # Project metadata
        "project.list", "project.get", "project.browse_resources",
        "project.dream.history", "project.context.assemble",
    ],
    "prompt": (
        "Work autonomously on this project. The project context above tells you "
        "what the user is trying to achieve, what's been linked (datasets, "
        "notebooks, IDE workspaces & source snapshots), and the current state "
        "(maintained across cycles).\n\n"
        "ITERATE: at each step, pick ONE investigation that would meaningfully "
        "advance your understanding:\n"
        "  - read project files via `ide.fs.read` (workspaces & snapshots are "
        "exposed as paths in the project context)\n"
        "  - query linked fabric datasets via `fabric.query` for relevant data\n"
        "  - search memory via `memory.search` for past decisions and learnings\n"
        "  - call `research.job.status(job_id=...)` to read full research output\n"
        "  - if a piece of info is missing, call `research.quick_search` for it\n\n"
        "After 4-7 useful investigations, STOP and let the synthesizer write a "
        "concrete actionable report:\n"
        "  - what is the user trying to achieve\n"
        "  - what is the current state (concretely, with evidence)\n"
        "  - what are the open questions or blockers\n"
        "  - what is the BEST next concrete step\n\n"
        "Ground every claim in actual data — never invent activity, project "
        "scope, or files that weren't mentioned in the context."
    ),
}


# Action variant: same project awareness, but EXECUTES the next step (writes)
# instead of only investigating + reporting. Used by project.dream.run(action=True).
PROJECT_ACTION_TRIGGER_DEFAULTS = {
    "name":        "project_action",
    "label":       "Project Action",
    "description": "Autonomous project execution — refine the next concrete step "
                   "and CARRY IT OUT (writes to memory/fabric/notebook/context), "
                   "then synthesise what was done.",
    "enabled":     True,
    "sensors":     ["dream.sensor.project_context",
                    "dream.sensor.memory_recent",
                    "dream.sensor.notebook_recent"],
    "pipeline":    ["dream.stage.gather", "dream.stage.themes",
                    "dream.stage.goal_refine",
                    "dream.stage.project_action",
                    "dream.stage.synthesize", "dream.stage.deliver"],
    "mode":        "agent_loop",
    "depth":       "standard",
    "hitl":        False,             # autonomous by default — see project.dream.run
    "hours_start": 0,
    "hours_end":   24,
    "min_idle_minutes":     20,
    "min_interval_minutes": 240,
    "require_signal":       0.0,       # always fire when invoked from a project
    "max_steps":            8,
    "deliver_to":  ["notebook", "memory"],
    "sensor_params": {
        "memory_recent": {"limit": 40},
    },
    # Read caps inherited from reflect + the write caps the action stage uses
    "no_hitl_caps": list(set(PROJECT_TRIGGER_DEFAULTS["no_hitl_caps"] + [
        "memory.create", "memory.update",
        "fabric.ingest", "fabric.entity_graph.extract",
        "nlp.run", "notebook.write", "notebook.append",
        "project.context.update",
    ])),
    "prompt": (
        "Work autonomously on this project and EXECUTE the single most valuable "
        "next step — do not merely propose or describe it. Use the write "
        "capabilities to make real changes: create memory records, ingest data, "
        "run entity extraction, write to notebooks, update the project context. "
        "If you need information you don't have, look it up first, then act. "
        "Ground every change in the actual project context — never invent scope, "
        "activity, or files that weren't mentioned."
    ),
}


# Composite: groups deep-context gathering + execution + journalling + pivoting
# into one automated, productive pipeline. This is the DEFAULT for project
# dreams (project.dream.run with no explicit trigger/mode).
# Caps for the composite project dream. Deliberately PROJECT-SCOPED: memory
# caps replace the fabric caps (project dreams kept fixating on big, unrelated
# fabric datasets instead of the project's own resources — fabric access is
# hidden; a dataset only enters via the project context / linked resources).
_PROJECT_COMPOSE_CAPS = [
    # Memory — the primary knowledge substrate
    "memory.search", "memory.recall", "memory.all_nodes", "memory.get",
    "memory.session_history", "memory.seek", "memory.map",
    "memory.create", "memory.update",
    # Project's own surface
    "project.list", "project.get", "project.browse_resources",
    "project.dream.history", "project.context.assemble",
    "project.context.update", "project.note.add",
    # Linked workspaces / files (read) + notebooks (write)
    "ide.workspace.list", "ide.fs.list", "ide.fs.tree", "ide.fs.read",
    "notebook.write", "notebook.append",
    # External grounding when the project genuinely needs a lookup
    "web.search", "web.fetch",
    # Analysis + journalling
    "llm.summarize", "llm.qa", "llm.analyze", "llm.explain",
    "llm.classify", "llm.brainstorm", "llm.generate",
    "dream.journal.append",
]

PROJECT_COMPOSITE_TRIGGER_DEFAULTS = {
    "name":        "project_compose",
    "label":       "Project (Composite)",
    "description": "Automated, productive project dream: assemble the PROJECT'S OWN "
                   "context (its notes, memory records, linked workspaces/notebooks), "
                   "refine the next goal, EXECUTE it, then synthesise, journal, and "
                   "decide whether to pivot into a follow-up dream — iterating until "
                   "it converges. Deliberately project-scoped: memory caps, no fabric "
                   "dataset spelunking.",
    "enabled":     True,
    "journal":     True,
    # Project-scoped sensing only — the broad memory_recent / notebook_recent
    # sensors fed the dream unrelated global activity (and fabric_explore fed
    # it unrelated datasets), which it then fixated on instead of the goal.
    "sensors":     ["dream.sensor.project_context"],
    "pipeline":    ["dream.stage.gather",
                    "dream.stage.load_workspace",
                    "dream.stage.memory_deep_traverse",
                    "dream.stage.enrich_context",
                    "dream.stage.themes",
                    "dream.stage.goal_refine",
                    "dream.stage.project_action",
                    "dream.stage.synthesize",
                    "dream.stage.deliver",
                    "dream.stage.pivot"],
    "mode":        "agent_loop",
    "depth":       "deep",
    "hitl":        False,
    "hours_start": 0,
    "hours_end":   24,
    "min_idle_minutes":     20,
    "min_interval_minutes": 240,
    "require_signal":       0.0,
    "max_steps":            8,
    "deliver_to":  ["notebook", "memory"],
    "sensor_params": {},
    "iterate": {
        "enabled": True, "max_iterations": 3, "min_iterations": 1,
        "iterate_stages": ["dream.stage.project_action"],
        "convergence_min_new_findings": 1,
    },
    "pivot": {
        "enabled": True, "min_confidence": 0.6, "max_pivots": 2,
        "candidates": ["project_reflect", "source_review"],
    },
    # Explicit whitelist = the loop's toolkit (falls back to no_hitl_caps
    # otherwise, which used to include the fabric caps).
    "whitelist":    list(_PROJECT_COMPOSE_CAPS),
    "no_hitl_caps": list(_PROJECT_COMPOSE_CAPS),
    "prompt": (
        "Drive this project forward using the PROJECT'S OWN resources: its context "
        "and notes, its memory records, its linked workspaces and notebooks. Refine "
        "the single most valuable next goal TOWARD THE PROJECT'S STATED OBJECTIVE "
        "and EXECUTE it with the write capabilities — make real, grounded changes. "
        "Journal your reasoning as you go. STAY ON GOAL: do not chase datasets, "
        "records or topics merely because they are large or nearby — something is "
        "relevant ONLY if the project context links it or the goal requires it. If "
        "you need outside information, look up the SPECIFIC fact (web.search) and "
        "return to the goal. After acting, decide whether the work opens a "
        "worthwhile follow-up dream. Never invent project scope, activity, or files."
    ),
}


async def _safe_trigger_upsert(upsert_func, params: dict):
    """Call dream.trigger.upsert, filtering params to only those the function
    actually accepts.  Any extra keys (like 'iterate', 'no_hitl_caps',
    'sensor_params', etc.) that aren't in the function signature get bundled
    into 'extra' (a JSON-safe dict kwarg that many dream caps accept as a
    catch-all).  This prevents TypeError on signature mismatches between the
    project defaults dict and the dream module's actual parameter list."""
    import inspect
    try:
        sig = inspect.signature(upsert_func)
        accepted = set(sig.parameters.keys())
    except (ValueError, TypeError):
        accepted = set()

    if not accepted:
        # Can't introspect — just try passing everything and hope
        return await upsert_func(**params)

    # Split into accepted kwargs and extras
    call_kw = {}
    extras  = {}
    for k, v in params.items():
        if k in accepted:
            call_kw[k] = v
        else:
            extras[k] = v

    # If the function has a **kwargs catch-all, pass everything
    has_var_kw = any(
        p.kind == inspect.Parameter.VAR_KEYWORD
        for p in sig.parameters.values()
    )
    if has_var_kw:
        return await upsert_func(**params)

    # Otherwise, try to stuff extras into known catch-all params
    if extras:
        if "extra" in accepted:
            # Merge with any existing 'extra' dict from params
            existing_extra = call_kw.get("extra") or {}
            if isinstance(existing_extra, str):
                try: existing_extra = json.loads(existing_extra)
                except Exception: existing_extra = {}
            existing_extra.update(extras)
            call_kw["extra"] = existing_extra
        elif "config" in accepted:
            existing_cfg = call_kw.get("config") or {}
            if isinstance(existing_cfg, str):
                try: existing_cfg = json.loads(existing_cfg)
                except Exception: existing_cfg = {}
            existing_cfg.update(extras)
            call_kw["config"] = existing_cfg
        else:
            # Last resort: log what we're dropping
            log.debug("dream.trigger.upsert: dropping unsupported kwargs: %s",
                      list(extras.keys()))

    return await upsert_func(**call_kw)


async def _ensure_project_trigger():
    """
    On startup, ensure project_reflect exists. If it exists with the OLD
    non-agentic config (synthesize_only mode, missing agentic_loop stage),
    upgrade it in-place. This catches users who got the old version on a
    previous deploy.
    """
    if not _dream:
        return
    try:
        get_t = CAPABILITY_REGISTRY.get("dream.trigger.get")
        upsert_t = CAPABILITY_REGISTRY.get("dream.trigger.upsert")
        if not (get_t and upsert_t):
            return

        # Ensure the action-variant trigger exists (idempotent — create if absent).
        try:
            act = await get_t["func"](name="project_action")
            if not (act and act.get("trigger")):
                await _safe_trigger_upsert(upsert_t["func"], PROJECT_ACTION_TRIGGER_DEFAULTS)
                log.info("project: created default project_action trigger")
        except Exception as e:
            log.debug("ensure project_action trigger: %s", e)

        # Ensure the composite (default) trigger exists — and UPGRADE a stored
        # copy that still has the old broad config (fabric_explore stage /
        # fabric caps / global sensors): that config made project dreams fixate
        # on big unrelated fabric datasets instead of the project's resources.
        try:
            comp = await get_t["func"](name="project_compose")
            comp_trig = comp.get("trigger") if comp else None
            if not comp_trig:
                await _safe_trigger_upsert(upsert_t["func"], PROJECT_COMPOSITE_TRIGGER_DEFAULTS)
                log.info("project: created default project_compose trigger")
            else:
                pipe = comp_trig.get("pipeline") or []
                caps_now = set((comp_trig.get("no_hitl_caps") or [])
                               + (comp_trig.get("whitelist") or []))
                needs_upgrade = ("dream.stage.fabric_explore" in pipe
                                 or any(c.startswith("fabric.") for c in caps_now)
                                 or not comp_trig.get("whitelist"))
                if needs_upgrade:
                    preserved = {k: comp_trig.get(k, PROJECT_COMPOSITE_TRIGGER_DEFAULTS[k])
                                 for k in ("enabled", "hours_start", "hours_end",
                                           "min_idle_minutes", "min_interval_minutes")}
                    await _safe_trigger_upsert(
                        upsert_t["func"],
                        {**PROJECT_COMPOSITE_TRIGGER_DEFAULTS, **preserved})
                    log.info("project: UPGRADED project_compose trigger to "
                             "project-scoped config (fabric caps removed)")
        except Exception as e:
            log.debug("ensure project_compose trigger: %s", e)

        existing = await get_t["func"](name="project_reflect")
        existing_trig = existing.get("trigger") if existing else None
        if existing_trig:
            # Detect old config — missing investigate in pipeline OR
            # missing iterate config OR mode mismatch OR no_hitl_caps too narrow
            pipe = existing_trig.get("pipeline") or []
            iter_cfg = existing_trig.get("iterate") or {}
            needs_upgrade = (
                "dream.stage.investigate" not in pipe
                or "dream.stage.agentic_loop" in pipe   # old stage
                or not iter_cfg.get("enabled")
                or len(existing_trig.get("no_hitl_caps") or []) < 15
            )
            if needs_upgrade:
                # Preserve user's enabled flag, custom prompt edits, and any extra
                # caps they've added — but reset the structural pipeline + sensors
                # + no_hitl_caps to the new defaults.
                preserved = {
                    "enabled":       existing_trig.get("enabled", True),
                    "hours_start":   existing_trig.get("hours_start", PROJECT_TRIGGER_DEFAULTS["hours_start"]),
                    "hours_end":     existing_trig.get("hours_end",   PROJECT_TRIGGER_DEFAULTS["hours_end"]),
                    "min_idle_minutes":     existing_trig.get("min_idle_minutes", PROJECT_TRIGGER_DEFAULTS["min_idle_minutes"]),
                    "min_interval_minutes": existing_trig.get("min_interval_minutes", PROJECT_TRIGGER_DEFAULTS["min_interval_minutes"]),
                }
                # Merge any user-added no_hitl_caps with the new defaults
                user_caps = set(existing_trig.get("no_hitl_caps") or [])
                merged_caps = list(set(PROJECT_TRIGGER_DEFAULTS["no_hitl_caps"]) | user_caps)
                upgrade = {**PROJECT_TRIGGER_DEFAULTS, **preserved,
                           "no_hitl_caps": merged_caps}
                await _safe_trigger_upsert(upsert_t["func"], upgrade)
                log.info("project: UPGRADED project_reflect trigger to agentic config")
            return
        await _safe_trigger_upsert(upsert_t["func"], PROJECT_TRIGGER_DEFAULTS)
        log.info("project: created default project_reflect trigger")
    except Exception as e:
        log.debug("ensure project trigger: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# UI PANEL REGISTRATION
# ─────────────────────────────────────────────────────────────────────────────

register_ui(
    "project-panel",
    "Projects",
    "▣",
    """<div id="project-panel-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/dream/panel#projects"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=[
        "project.list", "project.get", "project.upsert", "project.delete",
        "project.context.assemble", "project.context.update", "project.context.regenerate",
        "project.dream.run", "project.dream.history",
    ],
)


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────────────────────

async def _startup():
    """Initialise project trigger and any pending background tasks."""
    # Wait briefly for the dream module + redis to be ready
    for _ in range(30):
        if _redis() is not None and _dream is not None:
            break
        await asyncio.sleep(1)
    await _ensure_project_trigger()
    log.info("project_capabilities: ready")


# Use the standard module-startup pattern from the rest of the codebase
try:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        asyncio.create_task(_startup())
    else:
        loop.run_until_complete(_startup())
except Exception as e:
    log.debug("project startup: %s", e)