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
import re
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Import from the orchestrator that owns capability registration
from Vera.Orchestration.capability_orchestration import (
    APP, capability, emit_event, now_iso, register_ui, CAPABILITY_REGISTRY,
)

log = logging.getLogger("vera.project_caps")

# Resolve the orchestrator and dream modules at import time
_orch  = sys.modules.get("Vera.Orchestration.capability_orchestration") or \
         sys.modules.get("capability_orchestration")
_dream = sys.modules.get("Vera.Orchestration.dream_capabilities") or \
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
    fn = getattr(_orch, "ollama_generate", None)
    if not fn:
        return ""
    try:
        return str(await fn(prompt, system=system, prefer_gpu=prefer_gpu) or "")
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
                "explicitly). Inputs: slug (str!), trigger_name (str, optional), "
                "goal (str, optional — overrides project description as focus).",
)
async def project_dream_run(slug: str, trigger_name: str = "",
                             goal: str = "", trace_id=None):
    proj = await _get_project(slug)
    if not proj:
        return {"ok": False, "error": f"project not found: {slug}"}

    # Determine which trigger to fire
    tname = trigger_name or (proj.get("dream_trigger_names", [""])[0] if proj.get("dream_trigger_names") else "")
    if not tname:
        # Fallback: any project-aware trigger
        tname = "project_reflect"
    cycle_run = CAPABILITY_REGISTRY.get("dream.cycle.run")
    if not cycle_run:
        return {"ok": False, "error": "dream.cycle.run not available"}

    # Assemble seed
    assemble = await project_context_assemble(slug=slug, goal=goal)
    if assemble.get("error"):
        return {"ok": False, "error": assemble["error"]}
    seed = assemble["seed"]

    try:
        result = await cycle_run["func"](trigger_name=tname, seed=seed)
    except Exception as e:
        return {"ok": False, "error": f"dream.cycle.run failed: {e}"}

    # Record the cycle
    cid = (result or {}).get("cycle_id") if isinstance(result, dict) else None
    if cid:
        r = _redis()
        if r:
            try:
                await r.zadd(f"{KEY_PROJECT_DREAMS}:{slug}", {cid: time.time()})
            except Exception:
                pass
        proj["dream_count"] = int(proj.get("dream_count", 0)) + 1
        proj["last_dream_at"] = now_iso()
        await _save_project(proj)

    await emit_event({"type": "project.dream.started", "slug": slug,
                      "trigger": tname, "cycle_id": cid})
    return {"ok": True, "cycle_id": cid, "trigger": tname,
            "seed_chars": assemble.get("context_chars"), "result": result}


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
                                       trace_id=None):
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
    if report:
        await project_context_update(
            slug=slug, new_content=report,
            source=f"dream cycle {trigger}" if trigger else "dream cycle")
    return {"ok": True}


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