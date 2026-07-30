"""
loop_orchestrator.py — V8: agentic-loop GENERATOR + long-horizon PROGRAM orchestrator
=====================================================================================

The V8 "loop" is a META layer over the existing engines (v5/v6/v7) and the
specialist loop profiles (loop_profiles.py):

  • GENERATOR  (loops.generate) — given a goal / brief / thought, an LLM designs
    a PROGRAM: a small set of purpose-built loops. Each generated loop pins
    either an existing loop PROFILE (fabric-discovery, coding, devops, …) or a
    CUSTOM spec (engine version + scoped caps), a SYSTEM AGENT persona
    (agents.py DEFAULT_AGENTS — user-made agents opt-in via a flag), a cadence
    (once | recurring every N hours) and dependencies on sibling loops.

  • ORCHESTRATOR (loops.program.*) — runs a program's loops over DAYS TO MONTHS:
    a background tick fires the next due, dependency-satisfied loop (one loop
    in flight globally, so programs never stampede the cluster), records each
    run's outcome, and after every run a CONTROLLER LLM reviews progress
    against the program's done_when and ADAPTS the program: add loops, retire
    loops, reschedule, or finish. Programs persist in Redis and survive
    restarts.

  • dag.agent_loop_v8 — the variant-shaped entry point: generate + create +
    start a program from one goal, returning the program state. Each
    constituent run is a normal v5/v6/v7 run (session id ``v8:<pid>:<loop>``)
    so it is watchable/reattachable from the chat Loops pane like any other.

Registered in capability_orchestration._module_files. Absolute imports only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    CAPABILITY_REGISTRY,
    capability,
    emit_event,
    now_iso,
    schedule,
)

log = logging.getLogger("vera.loop_orchestrator")

KEY_PROGRAMS = "vera:v8:programs"          # HASH pid -> program JSON
KEY_V8_CONFIG = "vera:v8:config"           # JSON global defaults for v8 loops

_ENGINES = ("v5", "v6", "v7")
_ENGINE_CAP = {"v5": "dag.agent_loop_v5", "v6": "dag.agent_loop_v6",
               "v7": "dag.agent_loop_v7"}

# One constituent loop in flight across ALL programs — long-horizon work must
# never contend with itself or stampede the GPU pool.
_RUN_LOCK = asyncio.Lock()
_TICK_BUSY = False

# Global V8 defaults applied to EVERY constituent loop unless it pins its own.
#   model/agent/engine   — what long-horizon work runs on (user-steerable)
#   respect_dream_gate   — only fire loops when the dream activity gate allows
#                          (idle enough, human not active, system not busy)
#   max_concurrent       — hard ceiling on background loops in flight at once
#   max_loops_per_program— cap on how many loops one program may grow to
# Hydrated from Redis on first use.
_V8_CONFIG: Dict[str, Any] = {
    "model": "", "agent": "", "engine": "",
    "respect_dream_gate": True, "max_concurrent": 1, "max_loops_per_program": 8,
}
_V8_CONFIG_LOADED = False
# Count of constituent loops actually running right now (respects max_concurrent).
_RUNNING_LOOPS = 0


def _redis():
    return _orch.REDIS


def _dream_mod():
    return (sys.modules.get("dream_capabilities")
            or sys.modules.get("Vera.vera.dream.dream_capabilities"))


async def _v8_config() -> Dict[str, Any]:
    global _V8_CONFIG_LOADED
    if not _V8_CONFIG_LOADED:
        r = _redis()
        if r:
            try:
                raw = await r.get(KEY_V8_CONFIG)
                if raw:
                    doc = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                    if isinstance(doc, dict):
                        _V8_CONFIG.update({k: doc.get(k, _V8_CONFIG[k]) for k in _V8_CONFIG})
            except Exception:
                pass
        _V8_CONFIG_LOADED = True
    return _V8_CONFIG


def _sbx_mod():
    """The session-sandbox module (for run-owner scoping + collation), or None.
    Tries the known import names first (cheap, deterministic) before scanning —
    a silent miss here is exactly what lets constituent loops fan out into
    per-run containers instead of sharing the program's one owner container, so
    the lookup must be as robust as possible."""
    for _name in ("session_sandbox_capabilities",
                  "Vera.vera.remote.session_sandbox_capabilities"):
        m = sys.modules.get(_name)
        if m is not None and hasattr(m, "set_run_owner"):
            return m
    for n, m in list(sys.modules.items()):
        if m is not None and n.endswith("session_sandbox_capabilities") \
                and hasattr(m, "set_run_owner"):
            return m
    return None


def _cap_func(name: str):
    """The callable for a registered capability, or None."""
    c = CAPABILITY_REGISTRY.get(name)
    return c.get("func") if c and c.get("func") else None


def _prog_sandbox_owner(prog: Dict[str, Any]) -> tuple:
    """(owner_key, kind, label) for a program's shared sandbox container: the
    project/goal container when the program drives one, else a per-program
    container 'v8-<pid>' reused by every constituent run."""
    owner = str(prog.get("sandbox_owner") or "").strip()
    if owner:
        kind = "goal" if owner.startswith("goal-") else \
               ("project" if owner.startswith("proj") else "program")
        return owner, kind, prog.get("name", "")
    return f"v8-{prog['id']}", "program", prog.get("name", "")


async def _resolve_ide_workspace_path(spec: str) -> str:
    """Resolve a program's ide_workspace (a workspace NAME, or a host PATH) to a
    host directory the sandbox can clone into the loop containers, or '' when it
    can't be resolved."""
    spec = str(spec or "").strip()
    if not spec:
        return ""
    try:
        import os as _os
        if (("/" in spec) or ("\\" in spec)) and _os.path.isdir(spec):
            return spec
    except Exception:
        pass
    lister = CAPABILITY_REGISTRY.get("ide.workspace.list")
    if lister and lister.get("func"):
        try:
            res = await lister["func"]()
            for w in (res or {}).get("workspaces", []):
                if str(w.get("name") or "").strip().lower() == spec.lower():
                    return str(w.get("path") or "")
        except Exception:
            pass
    return ""


async def _auto_ide_workspace() -> str:
    """Best-effort IDE workspace path of the session that triggered this program
    (from the syslog trigger chain) — so a program created while the user is
    working in a workspace auto-associates without an explicit ide_workspace.
    '' when there's no triggering session or it isn't in a workspace."""
    sid = ""
    try:
        sl = sys.modules.get("syslog")
        if sl is not None and hasattr(sl, "get_trigger_chain"):
            sid = (sl.get_trigger_chain() or {}).get("session_id", "") or ""
    except Exception:
        sid = ""
    if not sid:
        return ""
    sbx = _sbx_mod()
    if sbx is not None and hasattr(sbx, "seed_path_for_session"):
        try:
            return await sbx.seed_path_for_session(sid) or ""
        except Exception:
            return ""
    return ""


def _profiles_mod():
    return (sys.modules.get("loop_profiles")
            or sys.modules.get("Vera.vera.dag.loop_profiles"))


def _agents_mod():
    return (sys.modules.get("agents")
            or sys.modules.get("Vera.vera.agents.agents"))


async def _llm_json(prompt: str, system: str) -> Dict[str, Any]:
    gen = getattr(_orch, "ollama_generate", None)
    if not gen:
        return {}
    try:
        # V8 program generation + adaptation are planning/orchestration: route
        # them through the loop/planner role (CPU reasoning model, e.g.
        # gpt-oss:20b) so they match v5/v7 planning and keep the GPU free.
        # Overridable on the Model Routing page (loop/planner role).
        raw = await gen(prompt, system=system, json_mode=True,
                        profile="loop", role="planner")
    except Exception as e:
        log.debug("v8 llm call failed: %s", e)
        return {}
    try:
        return json.loads((raw or "{}").strip())
    except Exception:
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# CATALOGS the generator chooses from
# ─────────────────────────────────────────────────────────────────────────────

def _profile_catalog() -> List[Dict[str, Any]]:
    pm = _profiles_mod()
    try:
        return pm.list_profiles() if pm else []
    except Exception:
        return []


async def _agent_catalog(include_user_agents: bool) -> List[Dict[str, str]]:
    """System agents (DEFAULT_AGENTS) always; user-made agents only when the
    flag is set. Compact (name — one-line description) for the generator."""
    am = _agents_mod()
    system_names = set()
    try:
        system_names = set(getattr(am, "_DEFAULT_AGENTS_BY_NAME", {}).keys())
    except Exception:
        pass
    out: List[Dict[str, str]] = []
    lister = CAPABILITY_REGISTRY.get("agent.list")
    rows: List[Dict[str, Any]] = []
    if lister and lister.get("func"):
        try:
            res = await lister["func"]()
            rows = (res or {}).get("agents") or (res if isinstance(res, list) else [])
        except Exception:
            rows = []
    if not rows and am:
        try:
            rows = [a.to_dict() for a in getattr(am, "DEFAULT_AGENTS", [])]
        except Exception:
            rows = []
    for a in rows:
        name = str(a.get("name") or "")
        if not name or a.get("archived"):
            continue
        is_system = (name in system_names) if system_names else True
        if not is_system and not include_user_agents:
            continue
        out.append({"name": name,
                    "system": "yes" if is_system else "user-made",
                    "description": str(a.get("description") or "")[:140]})
    return out[:60]


# ─────────────────────────────────────────────────────────────────────────────
# PROGRAM STORE
# ─────────────────────────────────────────────────────────────────────────────

async def _prog_all() -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return []
    try:
        raw = await r.hgetall(KEY_PROGRAMS)
    except Exception:
        return []
    out = []
    for _, v in (raw or {}).items():
        try:
            out.append(json.loads(v.decode() if isinstance(v, bytes) else v))
        except Exception:
            continue
    out.sort(key=lambda p: p.get("created", ""), reverse=True)
    return out


async def _prog_get(pid: str) -> Optional[Dict[str, Any]]:
    r = _redis()
    if not r or not pid:
        return None
    try:
        raw = await r.hget(KEY_PROGRAMS, pid)
        if raw:
            return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        pass
    return None


async def _prog_save(prog: Dict[str, Any]) -> None:
    r = _redis()
    if not r:
        return
    try:
        await r.hset(KEY_PROGRAMS, prog["id"], json.dumps(prog, default=str))
    except Exception as e:
        log.debug("v8 program save: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

def _coerce_loop_spec(raw: Dict[str, Any], profiles: set, agents: set,
                      idx: int) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or f"loop-{idx+1}").strip()[:60]
    goal = str(raw.get("goal") or "").strip()
    if not goal:
        return None
    profile = str(raw.get("profile") or "").strip()
    if profile and profile not in profiles:
        profile = ""
    engine = str(raw.get("engine") or "v6").strip().lower()
    if engine not in _ENGINES:
        engine = "v6"
    agent = str(raw.get("agent") or "").strip()
    if agent and agent not in agents:
        agent = ""
    # EPHEMERAL persona: a planner/agent invented FOR THIS TASK (e.g. a
    # marketing-plan planner, a code-change planner, an OSINT planner). It
    # directs this loop instead of a named agent.
    persona = None
    p = raw.get("persona")
    if isinstance(p, dict) and str(p.get("system_prompt") or p.get("role") or "").strip():
        persona = {
            "name": str(p.get("name") or f"{name}-persona").strip()[:60],
            "role": str(p.get("role") or "").strip()[:200],
            "system_prompt": str(p.get("system_prompt") or p.get("role") or "").strip()[:2000],
        }
    cad = raw.get("cadence") or {}
    ctype = str(cad.get("type") or "once").lower()
    if ctype not in ("once", "recurring"):
        ctype = "once"
    interval = max(1.0, float(cad.get("interval_hours") or 24)) if ctype == "recurring" else 0.0
    caps = [str(c).strip() for c in (raw.get("caps") or []) if str(c).strip()][:24]
    deps = [str(d).strip() for d in (raw.get("depends_on") or []) if str(d).strip()][:6]
    return {
        "name": name, "goal": goal[:1500],
        "profile": profile, "engine": engine, "agent": agent,
        "persona": persona,
        "caps": caps, "max_steps": max(0, int(raw.get("max_steps") or 0)),
        "cadence": {"type": ctype, "interval_hours": interval},
        "depends_on": deps,
        "success": str(raw.get("success") or "")[:300],
        "state": {"status": "pending", "runs": [], "next_due_ts": 0.0},
    }


async def _generate_program_spec(brief: str, include_user_agents: bool,
                                 max_loops: int) -> Dict[str, Any]:
    profiles = _profile_catalog()
    agents = await _agent_catalog(include_user_agents)
    prof_lines = "\n".join(
        f"  {p['id']} — engine {p.get('engine','v6')}, agent {p.get('agent','')}: "
        f"{(p.get('description') or '')[:120]}"
        for p in profiles) or "  (none)"
    agent_lines = "\n".join(
        f"  {a['name']} ({a['system']}) — {a['description']}" for a in agents) or "  (none)"
    sys_p = (
        "You are the V8 LOOP GENERATOR. Design a PROGRAM of agentic loops that will achieve "
        "the BRIEF over its natural horizon (hours to months). Each loop is one focused, "
        "independently-runnable unit of work.\n"
        "For each loop choose EITHER an existing loop PROFILE (preferred when one fits — it "
        "brings a tuned engine, agent and toolkit) OR a custom spec (engine v5=fast plan+"
        "execute, v6=adaptive controller, v7=tiered+branching for hard/strategic work; plus "
        "the exact capability names it needs).\n"
        "DIRECTION: each loop is directed by ONE of, in order of preference:\n"
        "  1. a listed AGENT (set \"agent\") when relevant expertise already exists;\n"
        "  2. an EPHEMERAL persona (set \"persona\") you INVENT for the task when nothing "
        "listed fits — a specialist planner/agent purpose-built for this loop (e.g. a "
        "marketing-plan planner, a code-change planner, an OSINT investigator). Give it a "
        "name, a one-line role, and a FULL system_prompt (expertise, method, output "
        "standards) — it will direct the loop's planning and execution;\n"
        "  3. neither (generic orchestration) for plain mechanical work.\n"
        "Use cadence type 'recurring' with interval_hours for monitoring/progress work, "
        "'once' for build/research steps. Wire order with depends_on (loop names). "
        "All loops SHARE ONE persistent /workspace, so design later loops to CONSUME "
        "the files earlier loops produce — state the expected handoff in each goal "
        "(what a loop reads from the workspace, what it leaves for the next). "
        "2–" + str(max_loops) + " loops; fewer is better.\n"
        'Respond ONLY with JSON:\n'
        '{"name":"<short program name>","done_when":"<one-line objective completion test>",'
        '"horizon_days":<int>,"loops":[{"name":"<slug>","goal":"<what this loop must achieve>",'
        '"profile":"<profile id or empty>","engine":"v5|v6|v7","agent":"<agent name or empty>",'
        '"persona":{"name":"<persona name>","role":"<one line>","system_prompt":"<full persona>"} | null,'
        '"caps":["cap.name"],"max_steps":0,"cadence":{"type":"once|recurring","interval_hours":24},'
        '"depends_on":["<loop name>"],"success":"<one-line check>"}]}')
    prompt = (f"BRIEF / GOAL / THOUGHT:\n{brief}\n\n"
              f"AVAILABLE LOOP PROFILES:\n{prof_lines}\n\n"
              f"AVAILABLE AGENTS:\n{agent_lines}\n\n"
              "Design the program JSON.")
    obj = await _llm_json(prompt, sys_p)
    prof_ids = {p["id"] for p in profiles}
    agent_names = {a["name"] for a in agents}
    loops: List[Dict[str, Any]] = []
    for i, lp in enumerate((obj.get("loops") or [])[:max_loops]):
        cs = _coerce_loop_spec(lp, prof_ids, agent_names, i)
        if cs:
            loops.append(cs)
    # Drop dangling dependencies.
    names = {l["name"] for l in loops}
    for l in loops:
        l["depends_on"] = [d for d in l["depends_on"] if d in names and d != l["name"]]
    return {
        "name": str(obj.get("name") or brief[:60]).strip()[:80],
        "done_when": str(obj.get("done_when") or "")[:300],
        "horizon_days": max(1, int(obj.get("horizon_days") or 30)),
        "loops": loops,
    }


# ─────────────────────────────────────────────────────────────────────────────
# RUNNING one constituent loop
# ─────────────────────────────────────────────────────────────────────────────

def _result_summary(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result)[:800]
    for k in ("final", "summary", "answer", "output"):
        v = result.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:800]
    if result.get("error"):
        return f"ERROR: {str(result['error'])[:400]}"
    return json.dumps({k: v for k, v in result.items()
                       if k in ("done", "cycles", "steps")}, default=str)[:400]


def _result_final(result: Any) -> str:
    """A fuller final-output slice (≤2000) for the activity UI + project record —
    the actual deliverable text, not the controller's short summary. Empty when
    the run produced no textual final (the summary still carries the gist)."""
    if not isinstance(result, dict):
        return str(result)[:2000]
    for k in ("final", "summary", "answer", "output"):
        v = result.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:2000]
    if result.get("error"):
        return f"ERROR: {str(result['error'])[:1000]}"
    return ""


def _unconsumed_steer(prog: Dict[str, Any]) -> List[str]:
    """Steering-note texts on a program that the controller hasn't consumed yet.
    Written by `loops.program.steer` (the dream/operator), read by the run
    preamble and the controller so the dream can course-correct a program
    WITHOUT spawning its own competing loops."""
    return [str(s.get("note") or "").strip()
            for s in (prog.get("steer_notes") or [])
            if isinstance(s, dict) and not s.get("consumed")
            and str(s.get("note") or "").strip()]


def _run_steps(result: Any) -> List[Dict[str, Any]]:
    """Best-effort step trace out of an engine/profile result, for the project
    loop history. Empty when the engine didn't surface steps."""
    if not isinstance(result, dict):
        return []
    for k in ("steps", "trace", "plan_steps"):
        v = result.get(k)
        if isinstance(v, list):
            return v[:60]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# COLLATION — deliver a program's work back to the OWNING project/goal.
#
# All of a program's constituent loops share ONE sandbox container (its
# sandbox_owner, e.g. goal-<slug>), so their files already co-exist there. What
# was missing is DELIVERY of that work to the project the program drives:
#   • per loop run  → a lightweight loop-history record (source 'v8_loop'), no
#     file harvest, so the goal page shows V8 progress as it happens WITHOUT
#     re-copying the same /workspace on every run (minimal duplication);
#   • at program close → the shared /workspace is exported ONCE, its files
#     harvested into the project's artifact store, and the container snapshotted
#     to the durable session store so the work survives.
# owner_ref is the project slug (blank for standalone director/chat programs —
# those still get the durable snapshot, just no project record).
# ─────────────────────────────────────────────────────────────────────────────
async def _collate_loop_run(prog: Dict[str, Any], lp: Dict[str, Any],
                            run_rec: Dict[str, Any], result: Any) -> None:
    slug = str(prog.get("owner_ref") or "").strip()
    if not slug:
        return
    rec = _cap_func("project.loop.record")
    if not rec:
        return
    try:
        await rec(slug=slug, source="v8_loop",
                  engine=(lp.get("engine") or lp.get("profile") or ""),
                  goal=str(lp.get("goal") or "")[:1000],
                  final=str(run_rec.get("final") or run_rec.get("summary") or "")[:8000],
                  steps=json.dumps(_run_steps(result), default=str),
                  run_id=str(run_rec.get("session_id") or ""),
                  trigger=f"v8:{prog['id']}")
    except Exception as e:
        log.debug("v8 collate loop run for %s: %s", prog.get("id"), e)


async def _collate_program_close(prog: Dict[str, Any]) -> None:
    """On program close: export the shared owner container's CHANGED /workspace
    files ONCE and (a) harvest them into the owning project's artifact store,
    (b) snapshot the container to the durable session store, (c) when the program
    cloned an IDE workspace, propose the changes back to it for gated review.
    Idempotent (guards on `collated_at`)."""
    if prog.get("collated_at"):
        return
    prog["collated_at"] = now_iso()
    owner, _kind, _label = _prog_sandbox_owner(prog)
    slug = str(prog.get("owner_ref") or "").strip()
    sbx = _sbx_mod()
    tmp = None
    try:
        # Prefer the CHANGED-only export so a container seeded from an IDE
        # workspace doesn't harvest the whole cloned project as "output".
        _exp = (getattr(sbx, "export_workspace_changes", None)
                or getattr(sbx, "export_workspace", None)) if sbx is not None else None
        if _exp:
            try:
                tmp = await _exp(owner)
            except Exception as e:
                log.debug("v8 export workspace(%s): %s", owner, e)
        # (a) harvest the final workspace into the project (once).
        if slug and tmp:
            rec = _cap_func("project.loop.record")
            if rec:
                notes = "; ".join(str(n.get("note") or "")
                                  for n in (prog.get("notes") or [])[-8:])
                roll = (f"V8 program '{prog.get('name')}' closed "
                        f"({prog.get('status')}). Done-when: "
                        f"{prog.get('done_when', '')}\n\nController notes: {notes}")
                try:
                    await rec(slug=slug, source="v8_program",
                              goal=str(prog.get("name") or "")[:1000],
                              final=roll[:8000], artifact_dir=tmp,
                              run_id=str(prog.get("id") or ""),
                              trigger=f"v8:{prog['id']}")
                except Exception as e:
                    log.debug("v8 close harvest for %s: %s", slug, e)
        # (b) durable snapshot of the shared container (owner_ref or not).
        sync = _cap_func("sandbox.session.sync")
        if sync:
            try:
                await sync(session_id=owner,
                           message=f"v8 program {prog.get('id')} {prog.get('status')}")
            except Exception as e:
                log.debug("v8 close sync for %s: %s", owner, e)
        # (c) gated write-back: if the program cloned an IDE workspace, propose
        # its changed files back to that workspace as a PR-style review — nothing
        # is written until a human accepts (ide.workspace.changes.accept).
        if prog.get("ide_workspace"):
            propose = _cap_func("ide.workspace.changes.propose")
            if propose:
                try:
                    await propose(session_id=owner, workspace=prog["ide_workspace"],
                                  source=f"v8:{prog['id']}")
                except Exception as e:
                    log.debug("v8 close propose for %s: %s", prog.get("id"), e)
    finally:
        if tmp:
            try:
                import shutil as _sh
                _sh.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass
        await _prog_save(prog)


def _program_context_preamble(prog: Dict[str, Any],
                              current_lp: Dict[str, Any]) -> str:
    """The inter-loop handoff: a compact block prepended to a constituent loop's
    goal so it runs WITH knowledge of the program — the shared objective, that it
    shares one persistent workspace with its siblings, and what prior loops (and
    its own earlier runs) already produced. Without this each loop is a cold
    start that can't build on the others; the result reads sensible in isolation
    but the program as a whole never becomes coherent."""
    parts: List[str] = [
        "[PROGRAM CONTEXT — you are ONE loop in a multi-loop program working "
        "toward a SHARED objective. Collaborate with the other loops; do not "
        "duplicate or restart their work."]
    name = str(prog.get("name") or "").strip()
    if name:
        parts.append(f"PROGRAM: {name}")
    done_when = str(prog.get("done_when") or "").strip()
    if done_when:
        parts.append(f"PROGRAM OBJECTIVE (done when): {done_when}")
    # Steering notes injected by the operator / dream orchestrator (course
    # corrections — off-plan, repeating an operation). High priority: surfaced
    # before the task so a running loop acts on them immediately.
    steers = _unconsumed_steer(prog)
    if steers:
        parts.append("OPERATOR / DREAM STEERING — apply these now:\n"
                     + "\n".join(f"  • {s}" for s in steers[-5:]))
    parts.append(
        "SHARED WORKSPACE: you and every sibling loop share ONE persistent "
        "working directory (/workspace) that carries across loops and runs. "
        "Before doing anything, LIST and READ what prior loops left there and "
        "BUILD ON IT — never redo work that is already present.")
    parts.append(
        "CODE AUDIENCE — before writing any script or code, decide WHO runs it:\n"
        "  • Code YOU will execute in this loop MUST be complete and runnable NOW: "
        "real values, reading real inputs from /workspace, NO placeholder / TODO / "
        "'your-key-here' / example stubs. If it needs a secret, path or credential, "
        "read it from the environment or a workspace file — or STOP and report it as "
        "blocked — never fake it, then run it and confirm it actually worked.\n"
        "  • Only code you are explicitly handing to a HUMAN as a deliverable may "
        "contain placeholders, and then you MUST label them (e.g. '# TODO: set X') "
        "and say so in your summary.\n"
        "Never hand YOURSELF placeholder code and then build or test around it — that "
        "produces output that looks finished but does nothing.")
    # Sibling loops that have produced output (their most recent run summary).
    prior: List[str] = []
    for l in prog.get("loops", []):
        if l.get("name") == current_lp.get("name"):
            continue
        st = l.get("state") or {}
        runs = st.get("runs") or []
        if not runs:
            continue
        prior.append(f"  - {l.get('name')} [{st.get('status', '?')}]: "
                     f"{str(runs[-1].get('summary') or '').strip()[:300]}")
    if prior:
        parts.append("WHAT SIBLING LOOPS HAVE PRODUCED (latest run each):\n"
                     + "\n".join(prior[:8]))
    deps = [d for d in (current_lp.get("depends_on") or []) if d]
    if deps:
        parts.append(f"YOUR INPUTS come from these completed loops: "
                     f"{', '.join(deps)} — their deliverables are in the workspace.")
    my_runs = (current_lp.get("state") or {}).get("runs") or []
    if my_runs:
        parts.append("YOUR OWN LAST RUN of this loop: "
                     + str(my_runs[-1].get("summary") or "").strip()[:400]
                     + "\nContinue from there; do not repeat completed steps.")
    parts.append("]")
    return "\n".join(parts)


def _spawn_loop(prog: Dict[str, Any], lp: Dict[str, Any]) -> None:
    """Launch a constituent loop as a task, tracking the live-loop count with a
    done-callback so `_RUNNING_LOOPS` can never leak (the count backs the
    max_concurrent ceiling and the dream 'background loops running' display)."""
    global _RUNNING_LOOPS
    _RUNNING_LOOPS += 1

    def _dec(_t):
        global _RUNNING_LOOPS
        _RUNNING_LOOPS = max(0, _RUNNING_LOOPS - 1)
    t = asyncio.create_task(_run_program_loop(prog, lp))
    t.add_done_callback(_dec)


async def _run_program_loop(prog: Dict[str, Any], lp: Dict[str, Any]) -> None:
    """Execute ONE constituent loop run, record the outcome, then let the
    controller adapt the program. Holds the global run lock."""
    pid = prog["id"]
    run_n = len(lp["state"]["runs"]) + 1
    session_id = f"v8:{pid}:{lp['name']}:{run_n}"
    # BACKGROUND marking: every LLM call in this run (and the controller pass
    # after it) is demoted off the GPU while a human is actively using the
    # system (see capability_orchestration.INTERACTIVE_PRIORITY).
    _bg_tok = None
    try:
        _bg_tok = _orch.BACKGROUND_LLM.set(f"v8:{pid}")
    except Exception:
        _bg_tok = None
    # SANDBOX ownership: the whole program shares ONE container (the driven
    # project/goal's container when there is one, else v8-<pid>). The run-owner
    # scope also blocks anything inside the run from nesting more sandboxes.
    sbx = _sbx_mod()
    _own_tok = None
    if sbx is not None:
        owner, okind, olabel = _prog_sandbox_owner(prog)
        _own_tok = sbx.set_run_owner(owner, kind=okind, label=olabel)
        try:
            await sbx.link_session(session_id, owner, kind=okind, label=olabel)
        except Exception:
            pass
        # IDE workspace: clone the project's HOST files into the shared owner
        # container so this loop operates on the REAL workspace. only_if_empty →
        # seeds the fresh container once, never clobbers accumulated program work.
        if prog.get("ide_workspace") and hasattr(sbx, "set_seed_path"):
            try:
                _wp = await _resolve_ide_workspace_path(prog["ide_workspace"])
                if _wp:
                    await sbx.set_seed_path(owner, _wp)
                    if hasattr(sbx, "import_workspace"):
                        await sbx.import_workspace(owner, _wp, only_if_empty=True)
            except Exception as e:
                log.debug("v8 %s: ide_workspace seed failed: %s", pid, e)
    else:
        # No owner scope → this run's exec/file-IO is NOT pinned to the program's
        # shared container; a nested sandbox.session.start would fan out into a
        # per-run container and the run may fall through to the host. Loud on
        # purpose: this is the failure mode behind "each loop isolated".
        log.warning("v8 %s: sandbox module unavailable — loop %s runs WITHOUT an "
                    "owner container (fan-out / host-exec risk)", pid, lp["name"])
    # Global V8 config: model / agent / engine the user chose for ALL long-
    # horizon work. When set it overrides the per-loop pick, so one control
    # steers everything the orchestrator runs.
    cfg8 = await _v8_config()
    cfg_model = str(cfg8.get("model") or "").strip()
    cfg_agent = str(cfg8.get("agent") or "").strip()
    cfg_engine = str(cfg8.get("engine") or "").strip().lower()
    # Ephemeral persona: a task-specific planner/agent the generator invented
    # for this loop. It DIRECTS the run — prepended to the goal so it shapes
    # the orchestrator's planning AND every specialist step, engine-agnostic.
    run_goal = lp["goal"]
    if lp.get("persona"):
        pe = lp["persona"]
        run_goal = (f"[OPERATING PERSONA — adopt fully for this ENTIRE run: "
                    f"{pe.get('name','specialist')} — {pe.get('role','')}\n"
                    f"{pe.get('system_prompt','')}]\n\nTASK: {lp['goal']}")
    # Inter-loop coherence: lead with the program context (shared objective +
    # what sibling loops already produced in the shared workspace) so this run
    # builds on the others instead of cold-starting.
    _ctx = _program_context_preamble(prog, lp)
    if _ctx:
        run_goal = _ctx + "\n\n" + run_goal
    async with _RUN_LOCK:
        lp["state"]["status"] = "running"
        await _prog_save(prog)
        await emit_event({"type": "agent_loop_v8.loop_started", "program": pid,
                          "loop": lp["name"], "run": run_n, "session_id": session_id,
                          "profile": lp.get("profile", ""), "engine": lp.get("engine", ""),
                          "persona": (lp.get("persona") or {}).get("name", "")})
        t0 = time.time()
        try:
            if lp.get("profile"):
                runner = CAPABILITY_REGISTRY.get("loops.run")
                kw: Dict[str, Any] = {"profile": lp["profile"], "goal": run_goal,
                                      "session_id": session_id}
                if lp.get("caps"):
                    kw["allowed_caps"] = ",".join(lp["caps"])
                if lp.get("max_steps"):
                    kw["max_steps"] = int(lp["max_steps"])
                if cfg_agent:
                    kw["agent"] = cfg_agent
                if cfg_model and runner:
                    _acc = set(runner.get("schema", {}).get("properties", {}).keys())
                    if "model" in _acc:
                        kw["model"] = cfg_model
                result = await runner["func"](**kw) if runner and runner.get("func") \
                    else {"error": "loops.run unavailable"}
            else:
                eng = cfg_engine if cfg_engine in _ENGINES else lp.get("engine", "v6")
                cap = CAPABILITY_REGISTRY.get(_ENGINE_CAP.get(eng, "dag.agent_loop_v6"))
                if not cap or not cap.get("func"):
                    result = {"error": f"engine {eng} unavailable"}
                else:
                    accepted = set(cap.get("schema", {}).get("properties", {}).keys()) | {"trace_id"}
                    body: Dict[str, Any] = {"goal": run_goal, "trace_id": session_id}
                    if lp.get("caps"):
                        body["allowed_caps"] = ",".join(lp["caps"])
                    if lp.get("max_steps"):
                        body["max_steps"] = int(lp["max_steps"])
                    if "session_id" in accepted:
                        body["session_id"] = session_id
                    # Agent persona: model + domain caps folded in (mirrors loops.run).
                    # A global config agent overrides the loop's own.
                    _agent = cfg_agent or lp.get("agent")
                    if _agent:
                        agc = CAPABILITY_REGISTRY.get("agent.get")
                        if agc and agc.get("func"):
                            try:
                                ag = await agc["func"](name=_agent)
                                if ag and not ag.get("error"):
                                    dom = ag.get("domain_caps") or []
                                    if dom:
                                        body["allowed_caps"] = ",".join(
                                            sorted(set((body.get("allowed_caps", "").split(","))
                                                       + list(dom)) - {""}))
                                    if ag.get("model"):
                                        body["model"] = ag["model"]
                            except Exception:
                                pass
                    # Explicit global model override wins over the agent's model.
                    if cfg_model:
                        body["model"] = cfg_model
                    kwargs = {k: v for k, v in body.items() if k in accepted or k == "goal"}
                    result = await cap["func"](**kwargs)
        except Exception as e:
            result = {"error": str(e)}
        elapsed = round(time.time() - t0, 1)
        ok = isinstance(result, dict) and not result.get("error")
        summary = _result_summary(result)
        # `summary` (≤800) is what the controller reasons over; `final` keeps a
        # fuller slice of the actual deliverable so the activity UI can show real
        # output without depending on a (TTL-limited) replay of the run.
        final_full = _result_final(result)
        lp["state"]["runs"].append({"ts": now_iso(), "session_id": session_id,
                                    "ok": ok, "elapsed_s": elapsed,
                                    "summary": summary, "final": final_full})
        lp["state"]["runs"] = lp["state"]["runs"][-12:]
        cad = lp.get("cadence") or {}
        if cad.get("type") == "recurring":
            lp["state"]["status"] = "waiting"
            lp["state"]["next_due_ts"] = time.time() + float(cad.get("interval_hours") or 24) * 3600
        else:
            lp["state"]["status"] = "done" if ok else "failed"
        await _prog_save(prog)
        await emit_event({"type": "agent_loop_v8.loop_done", "program": pid,
                          "loop": lp["name"], "run": run_n, "ok": ok,
                          "elapsed_s": elapsed, "summary": summary[:400],
                          "session_id": session_id})
    # Deliver this run to the owning project's loop history so the goal/project
    # page shows V8 work as it lands (files are collated once at program close).
    try:
        await _collate_loop_run(prog, lp, lp["state"]["runs"][-1], result)
    except Exception as e:
        log.debug("v8 collate loop run for %s: %s", pid, e)
    try:
        await _adapt_program(prog, lp, summary, ok)
    except Exception as e:
        log.debug("v8 adapt failed for %s: %s", pid, e)
    # Reset the background/owner context (belt-and-braces: this function always
    # runs as its own task, so the context dies with it anyway).
    try:
        if _bg_tok is not None:
            _orch.BACKGROUND_LLM.reset(_bg_tok)
    except Exception:
        pass
    if sbx is not None:
        sbx.reset_run_owner(_own_tok)


async def _adapt_program(prog: Dict[str, Any], last_loop: Dict[str, Any],
                         last_summary: str, last_ok: bool) -> None:
    """Controller pass after every run: review progress vs done_when and adapt."""
    state_lines = []
    for l in prog.get("loops", []):
        st = l.get("state") or {}
        runs = st.get("runs") or []
        state_lines.append(
            f"- {l['name']} [{st.get('status','pending')}] "
            f"({len(runs)} runs{', last ok' if runs and runs[-1].get('ok') else (', last FAILED' if runs else '')})"
            f": {l['goal'][:100]}"
            + (f" | last: {runs[-1]['summary'][:120]}" if runs else ""))
    sys_p = (
        "You are the V8 PROGRAM CONTROLLER. A constituent loop just finished. Review the "
        "program and decide adaptations. Be conservative: adapt only when the evidence "
        "demands it.\n"
        'Respond ONLY with JSON: {"program_status":"active|done|failed",'
        '"note":"<one-line progress assessment>",'
        '"adaptations":[{"action":"add","loop":{"name":"...","goal":"...","profile":"",'
        '"engine":"v6","agent":"","persona":null,"caps":[],'
        '"cadence":{"type":"once","interval_hours":0},'
        '"depends_on":[],"success":""}} | {"action":"retire","name":"<loop>"} | '
        '{"action":"reschedule","name":"<loop>","in_hours":<float>}]}\n'
        "Mark program_status done ONLY when done_when is genuinely met; failed only when "
        "the program cannot proceed at all.")
    # Steering notes from the dream orchestrator / operator: the highest-priority
    # input — the controller must act on them (retarget, add/retire, or refocus).
    steers = [s for s in (prog.get("steer_notes") or [])
              if isinstance(s, dict) and not s.get("consumed")
              and str(s.get("note") or "").strip()]
    steer_txt = ""
    if steers:
        steer_txt = ("\n\nSTEERING FROM THE DREAM ORCHESTRATOR / OPERATOR (act on "
                     "these — they see the program from outside and are correcting "
                     "drift or a repeated operation):\n"
                     + "\n".join(f"- {str(s.get('note') or '')[:300]}" for s in steers[-5:]))
    prompt = (f"PROGRAM: {prog.get('name')}\nBRIEF: {prog.get('brief','')[:600]}\n"
              f"DONE WHEN: {prog.get('done_when','(unspecified)')}\n"
              f"HORIZON: {prog.get('horizon_days')} days (created {prog.get('created')})\n\n"
              f"LOOPS:\n" + "\n".join(state_lines) + "\n\n"
              f"JUST FINISHED: {last_loop['name']} — {'ok' if last_ok else 'FAILED'}\n"
              f"RESULT SUMMARY:\n{last_summary[:1200]}"
              + steer_txt + "\n\nDecide the adaptations JSON.")
    obj = await _llm_json(prompt, sys_p)
    if not obj:
        return
    # Consume the steering notes we just showed the controller (idempotent — a
    # note steers one controller pass, then the run preamble + this prompt stop
    # repeating it).
    for s in steers:
        s["consumed"] = True
    note = str(obj.get("note") or "")[:300]
    if note:
        prog.setdefault("notes", []).append({"ts": now_iso(), "note": note})
        prog["notes"] = prog["notes"][-30:]
    status = str(obj.get("program_status") or "active").lower()
    prof_ids = {p["id"] for p in _profile_catalog()}
    agent_names = {a["name"] for a in await _agent_catalog(bool(prog.get("include_user_agents")))}
    names = {l["name"] for l in prog.get("loops", [])}
    _max_loops = int((await _v8_config()).get("max_loops_per_program", 8) or 8)
    adapted = []
    for ad in (obj.get("adaptations") or [])[:3]:
        if not isinstance(ad, dict):
            continue
        act = str(ad.get("action") or "")
        if act == "add" and len(prog.get("loops", [])) < _max_loops:
            cs = _coerce_loop_spec(ad.get("loop") or {}, prof_ids, agent_names,
                                   len(prog.get("loops", [])))
            if cs and cs["name"] not in names:
                cs["depends_on"] = [d for d in cs["depends_on"] if d in names]
                prog["loops"].append(cs)
                names.add(cs["name"])
                adapted.append(f"add {cs['name']}")
        elif act == "retire":
            for l in prog.get("loops", []):
                if l["name"] == ad.get("name") and l["state"]["status"] != "done":
                    l["state"]["status"] = "retired"
                    adapted.append(f"retire {l['name']}")
        elif act == "reschedule":
            for l in prog.get("loops", []):
                if l["name"] == ad.get("name"):
                    l["state"]["status"] = "waiting"
                    l["state"]["next_due_ts"] = time.time() + max(0.1, float(ad.get("in_hours") or 1)) * 3600
                    adapted.append(f"reschedule {l['name']}")
    if status in ("done", "failed"):
        prog["status"] = status
        prog["finished"] = now_iso()
    await _prog_save(prog)
    if status in ("done", "failed"):
        await _collate_program_close(prog)
    await emit_event({"type": "agent_loop_v8.program_adapted", "program": prog["id"],
                      "status": prog.get("status"), "note": note,
                      "adaptations": adapted})


def _deps_satisfied(prog: Dict[str, Any], lp: Dict[str, Any]) -> bool:
    by_name = {l["name"]: l for l in prog.get("loops", [])}
    for d in lp.get("depends_on") or []:
        dep = by_name.get(d)
        if not dep:
            continue
        st = dep.get("state") or {}
        runs = st.get("runs") or []
        if st.get("status") == "done" or any(r.get("ok") for r in runs):
            continue
        return False
    return True


def _next_due_loop(prog: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    now_ts = time.time()
    for lp in prog.get("loops", []):
        st = lp.get("state") or {}
        status = st.get("status", "pending")
        if status == "pending" and _deps_satisfied(prog, lp):
            return lp
        if status == "waiting" and float(st.get("next_due_ts") or 0) <= now_ts \
                and _deps_satisfied(prog, lp):
            return lp
    return None


async def _v8_tick():
    """Background driver: advance at most ONE program loop per tick, only when
    the dream activity gate allows and we're under the concurrency ceiling."""
    global _TICK_BUSY
    if _TICK_BUSY:
        return
    cfg = await _v8_config()
    # Concurrency ceiling — never run more than max_concurrent background loops.
    max_conc = max(1, int(cfg.get("max_concurrent", 1) or 1))
    if _RUNNING_LOOPS >= max_conc or _RUN_LOCK.locked():
        return
    # Dream activity gate: long-horizon loops obey the SAME idle/activity
    # discipline as the dream scheduler (idle enough, human not active, system
    # not busy) instead of firing whenever. Toggle with loops.config
    # respect_dream_gate; without it, only the interactive backoff applies.
    if cfg.get("respect_dream_gate", True):
        dm = _dream_mod()
        if dm is not None and hasattr(dm, "dream_background_allowed"):
            try:
                gate = await dm.dream_background_allowed()
                if not gate.get("allowed"):
                    return
            except Exception:
                pass
    else:
        try:
            if getattr(_orch, "defer_background_now", lambda: False)():
                return
        except Exception:
            pass
    _TICK_BUSY = True
    try:
        for prog in await _prog_all():
            if prog.get("status") != "active":
                continue
            # Horizon expiry → close the program out.
            try:
                from datetime import datetime as _dt
                created = _dt.fromisoformat(str(prog.get("created")).replace("Z", "+00:00"))
                age_days = (time.time() - created.timestamp()) / 86400.0
            except Exception:
                age_days = 0.0
            if age_days > float(prog.get("horizon_days") or 30):
                prog["status"] = "done"
                prog["finished"] = now_iso()
                prog.setdefault("notes", []).append(
                    {"ts": now_iso(), "note": "horizon reached — program closed"})
                await _prog_save(prog)
                await _collate_program_close(prog)
                await emit_event({"type": "agent_loop_v8.program_done",
                                  "program": prog["id"], "reason": "horizon"})
                continue
            if any((l.get("state") or {}).get("status") == "running"
                   for l in prog.get("loops", [])):
                continue
            # All loops terminal and none recurring-due → nothing left; close.
            lp = _next_due_loop(prog)
            if lp is None:
                terminal = all((l.get("state") or {}).get("status")
                               in ("done", "failed", "retired")
                               for l in prog.get("loops", []))
                if terminal:
                    prog["status"] = "done"
                    prog["finished"] = now_iso()
                    await _prog_save(prog)
                    await _collate_program_close(prog)
                    await emit_event({"type": "agent_loop_v8.program_done",
                                      "program": prog["id"], "reason": "all loops terminal"})
                continue
            _spawn_loop(prog, lp)
            break   # one loop in flight globally
    except Exception as e:
        log.warning("v8 tick: %s", e)
    finally:
        _TICK_BUSY = False


schedule(_v8_tick, interval=300, name="v8_program_tick")


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

def _prog_public(prog: Dict[str, Any], full: bool = False) -> Dict[str, Any]:
    loops = []
    for l in prog.get("loops", []):
        st = l.get("state") or {}
        runs = st.get("runs") or []
        row = {"name": l["name"], "goal": l["goal"][:200], "profile": l.get("profile", ""),
               "engine": l.get("engine", ""), "agent": l.get("agent", ""),
               "persona": (l.get("persona") or {}).get("name", ""),
               "cadence": l.get("cadence"), "depends_on": l.get("depends_on"),
               "status": st.get("status"), "runs": len(runs),
               "last_run": ({"ts": runs[-1].get("ts"), "ok": runs[-1].get("ok"),
                             "elapsed_s": runs[-1].get("elapsed_s"),
                             "session_id": runs[-1].get("session_id"),
                             "summary": str(runs[-1].get("summary") or "")[:300]}
                            if runs else None),
               "next_due_ts": st.get("next_due_ts") or 0}
        if full:
            row["run_history"] = runs
            row["caps"] = l.get("caps") or []
            row["success"] = l.get("success", "")
        loops.append(row)
    out = {"id": prog["id"], "name": prog.get("name"), "status": prog.get("status"),
           "brief": prog.get("brief", "")[:300], "done_when": prog.get("done_when", ""),
           "horizon_days": prog.get("horizon_days"), "created": prog.get("created"),
           "sandbox_owner": prog.get("sandbox_owner", ""),
           "owner_ref": prog.get("owner_ref", ""),
           "loops": loops}
    if full:
        out["notes"] = prog.get("notes") or []
        out["brief"] = prog.get("brief", "")
    return out


@capability(
    "loops.generate", memory="off",
    http_method="POST", http_path="/loops/generate", http_tags=["dag", "agents"],
    description="V8 loop GENERATOR (preview only — creates nothing): design a custom "
                "multi-loop PROGRAM from a goal/brief/thought. Each loop pins a loop "
                "profile or a custom engine (v5/v6/v7) + caps, an agent persona from the "
                "system agents (user-made agents only when include_user_agents), a cadence "
                "and dependencies. Inputs: brief (str!), include_user_agents (bool default "
                "False), max_loops (int default 5). Output: {name, done_when, horizon_days, "
                "loops:[...]}.",
)
async def cap_loops_generate(brief: str = "", include_user_agents: bool = False,
                             max_loops: int = 5, trace_id=None):
    if not (brief or "").strip():
        return {"error": "brief required"}
    return await _generate_program_spec(brief.strip(), bool(include_user_agents),
                                        max(2, min(8, int(max_loops))))


@capability(
    "loops.program.create", memory="on",
    http_method="POST", http_path="/loops/program/create", http_tags=["dag", "agents"],
    description="Create (and by default START) a V8 loop PROGRAM from a goal/brief: the "
                "generator designs the constituent loops, the orchestrator then runs them "
                "over the program's horizon (days to months) — dependency-ordered, one at a "
                "time, adapting after every run. Inputs: brief (str!), horizon_days (int — "
                "0 = generator's choice), include_user_agents (bool default False), "
                "autostart (bool default True), sandbox_owner (str — shared sandbox "
                "container key all runs use, e.g. goal-<slug>; default = a per-program "
                "container), owner_ref (str — spawning entity, e.g. project slug), "
                "ide_workspace (str — an IDE workspace name or host path whose project "
                "files are cloned into the program's container so its loops operate on "
                "the real workspace). Output: the program state.",
)
async def cap_loops_program_create(brief: str = "", horizon_days: int = 0,
                                   include_user_agents: bool = False,
                                   autostart: bool = True,
                                   sandbox_owner: str = "",
                                   owner_ref: str = "",
                                   ide_workspace: str = "", trace_id=None):
    if not (brief or "").strip():
        return {"error": "brief required"}
    # Auto-associate the triggering session's IDE workspace when one wasn't given
    # (e.g. a V8 loop launched from chat while working in a workspace).
    if not (ide_workspace or "").strip():
        try:
            ide_workspace = await _auto_ide_workspace()
        except Exception:
            ide_workspace = ""
    spec = await _generate_program_spec(brief.strip(), bool(include_user_agents), 5)
    if not spec.get("loops"):
        return {"error": "generator produced no loops", "spec": spec}
    prog = {
        "id": str(uuid.uuid4())[:8],
        "name": spec["name"], "brief": brief.strip()[:2000],
        "done_when": spec["done_when"],
        "horizon_days": int(horizon_days) or spec["horizon_days"],
        "include_user_agents": bool(include_user_agents),
        "status": "active", "created": now_iso(),
        # sandbox_owner: the shared container key every constituent run uses
        # (e.g. 'goal-<slug>' when a project/goal spawned this program; blank →
        # a per-program 'v8-<pid>' container). owner_ref: the spawning entity
        # (project slug) for the activity timeline.
        "sandbox_owner": str(sandbox_owner or "").strip()[:60],
        "owner_ref": str(owner_ref or "").strip()[:80],
        # ide_workspace: a workspace name / host path whose files are cloned into
        # the program's shared container so its loops operate on the real project.
        "ide_workspace": str(ide_workspace or "").strip()[:200],
        "loops": spec["loops"], "notes": [],
    }
    await _prog_save(prog)
    await emit_event({"type": "agent_loop_v8.program_created", "program": prog["id"],
                      "name": prog["name"], "loops": [l["name"] for l in prog["loops"]],
                      "horizon_days": prog["horizon_days"]})
    if autostart:
        lp = _next_due_loop(prog)
        # A program CREATED inside a background context (e.g. a dream cycle
        # escalating a strategic goal) must NOT fire a loop immediately — that
        # would bypass the dream activity gate. The gated tick starts it. A
        # human/foreground create autostarts right away.
        in_bg = False
        try:
            in_bg = bool(_orch.BACKGROUND_LLM.get(""))
        except Exception:
            in_bg = False
        cfg = await _v8_config()
        max_conc = max(1, int(cfg.get("max_concurrent", 1) or 1))
        if lp and not (in_bg and cfg.get("respect_dream_gate", True)) \
                and _RUNNING_LOOPS < max_conc and not _RUN_LOCK.locked():
            _spawn_loop(prog, lp)
    return _prog_public(prog, full=True)


@capability(
    "dag.agent_loop_v8", memory="on",
    http_method="POST", http_path="/dag/agent_loop_v8", http_tags=["dag", "agents"],
    description="V8 agent loop — the loop GENERATOR + long-horizon PROGRAM orchestrator. "
                "Designs a custom program of v5/v6/v7 loops (profiles, system-agent "
                "personas, cadences, dependencies) from the goal and runs it over days to "
                "months in the background, adapting after every run. Returns the program "
                "state immediately (NOT a streaming run — watch constituent runs in the "
                "Loops pane via session ids v8:<program>:<loop>). Inputs: goal (str!), "
                "horizon_days (int), include_user_agents (bool), sandbox_owner (str — "
                "shared container key all runs use, e.g. goal-<slug>, so the program's "
                "loops share the goal's workspace instead of a standalone one), owner_ref "
                "(str — owning project/goal slug; its loop history + artifacts receive "
                "this program's work), ide_workspace (str — IDE workspace name/host path "
                "cloned into the program container so loops operate on the real project). "
                "Output: program state.",
)
async def cap_agent_loop_v8(goal: str = "", horizon_days: int = 0,
                            include_user_agents: bool = False,
                            sandbox_owner: str = "", owner_ref: str = "",
                            ide_workspace: str = "", trace_id=None):
    return await cap_loops_program_create(brief=goal, horizon_days=horizon_days,
                                          include_user_agents=include_user_agents,
                                          autostart=True, sandbox_owner=sandbox_owner,
                                          owner_ref=owner_ref, ide_workspace=ide_workspace,
                                          trace_id=trace_id)


@capability(
    "loops.config.get", memory="off", silent=True,
    http_method="GET", http_path="/loops/config", http_tags=["dag", "agents"],
    description="Get the global V8 loop defaults (model / agent / engine) applied "
                "to EVERY constituent loop the orchestrator runs unless a loop pins "
                "its own. Output: {model, agent, engine}.",
)
async def cap_loops_config_get(trace_id=None):
    return dict(await _v8_config())


@capability(
    "loops.config.set", memory="off",
    http_method="POST", http_path="/loops/config/set", http_tags=["dag", "agents"],
    description="Set the global V8 loop defaults (persisted). Any set field is "
                "applied to every constituent loop the orchestrator runs, overriding "
                "the per-loop pick — the one control that steers all long-horizon "
                "work. Inputs: model (str — '' clears), agent (str — a registered "
                "agent whose model + domain caps are folded in), engine (str — "
                "v5|v6|v7, '' = per-loop choice), respect_dream_gate (bool — only "
                "fire loops when the dream activity gate allows: idle enough, human "
                "not active, system not busy), max_concurrent (int — hard ceiling on "
                "background loops in flight at once), max_loops_per_program (int — "
                "cap on how many loops one program may grow to). Output: {ok, config}.",
)
async def cap_loops_config_set(model: Optional[str] = None,
                               agent: Optional[str] = None,
                               engine: Optional[str] = None,
                               respect_dream_gate: Optional[bool] = None,
                               max_concurrent: Optional[int] = None,
                               max_loops_per_program: Optional[int] = None,
                               trace_id=None):
    global _V8_CONFIG_LOADED
    await _v8_config()   # ensure hydrated first
    if model is not None:
        _V8_CONFIG["model"] = str(model).strip()
    if agent is not None:
        _V8_CONFIG["agent"] = str(agent).strip()
    if engine is not None:
        e = str(engine).strip().lower()
        _V8_CONFIG["engine"] = e if e in _ENGINES else ""
    if respect_dream_gate is not None:
        _V8_CONFIG["respect_dream_gate"] = bool(respect_dream_gate)
    if max_concurrent is not None:
        try:
            _V8_CONFIG["max_concurrent"] = max(1, min(8, int(max_concurrent)))
        except Exception:
            return {"ok": False, "error": "max_concurrent must be an integer"}
    if max_loops_per_program is not None:
        try:
            _V8_CONFIG["max_loops_per_program"] = max(1, min(24, int(max_loops_per_program)))
        except Exception:
            return {"ok": False, "error": "max_loops_per_program must be an integer"}
    r = _redis()
    if r:
        try:
            await r.set(KEY_V8_CONFIG, json.dumps(_V8_CONFIG))
        except Exception as e:
            log.debug("v8 config save: %s", e)
    _V8_CONFIG_LOADED = True
    await emit_event({"type": "agent_loop_v8.config", **_V8_CONFIG})
    return {"ok": True, "config": dict(_V8_CONFIG)}


@capability(
    "loops.program.list", memory="off", silent=True,
    http_method="GET", http_path="/loops/program/list", http_tags=["dag", "agents"],
    description="List V8 loop programs (id, name, status, loop states). "
                "Input: status (str filter — active|done|failed|paused, empty = all).",
)
async def cap_loops_program_list(status: str = "", trace_id=None):
    progs = await _prog_all()
    if status:
        progs = [p for p in progs if p.get("status") == status]
    return {"programs": [_prog_public(p) for p in progs], "count": len(progs)}


@capability(
    "loops.program.get", memory="off", silent=True,
    http_method="GET", http_path="/loops/program/get", http_tags=["dag", "agents"],
    description="Full detail for one V8 program: loops with run history + controller "
                "notes. Input: id (str!).",
)
async def cap_loops_program_get(id: str = "", trace_id=None):
    prog = await _prog_get(id)
    if not prog:
        return {"error": f"unknown program: {id}"}
    return _prog_public(prog, full=True)


@capability(
    "loops.program.pause", memory="off",
    http_method="POST", http_path="/loops/program/pause", http_tags=["dag", "agents"],
    description="Pause an active V8 program (no further loops fire). Input: id (str!).",
)
async def cap_loops_program_pause(id: str = "", trace_id=None):
    prog = await _prog_get(id)
    if not prog:
        return {"error": f"unknown program: {id}"}
    prog["status"] = "paused"
    await _prog_save(prog)
    return {"ok": True, "id": id, "status": "paused"}


@capability(
    "loops.program.resume", memory="off",
    http_method="POST", http_path="/loops/program/resume", http_tags=["dag", "agents"],
    description="Resume a paused V8 program. Input: id (str!).",
)
async def cap_loops_program_resume(id: str = "", trace_id=None):
    prog = await _prog_get(id)
    if not prog:
        return {"error": f"unknown program: {id}"}
    prog["status"] = "active"
    await _prog_save(prog)
    return {"ok": True, "id": id, "status": "active"}


@capability(
    "loops.program.delete", memory="off",
    http_method="POST", http_path="/loops/program/delete", http_tags=["dag", "agents"],
    description="Delete a V8 program (does not cancel a run already in flight). "
                "Input: id (str!).",
)
async def cap_loops_program_delete(id: str = "", trace_id=None):
    r = _redis()
    if not r:
        return {"error": "redis unavailable"}
    try:
        n = await r.hdel(KEY_PROGRAMS, id)
    except Exception as e:
        return {"error": str(e)}
    return {"ok": bool(n), "id": id}


@capability(
    "loops.program.tick", memory="off",
    http_method="POST", http_path="/loops/program/tick", http_tags=["dag", "agents"],
    description="Manually advance the V8 orchestrator one tick (fires the next due, "
                "dependency-satisfied loop across active programs).",
)
async def cap_loops_program_tick(trace_id=None):
    await _v8_tick()
    return {"ok": True, "in_flight": _RUN_LOCK.locked()}


@capability(
    "loops.program.steer", memory="off",
    http_method="POST", http_path="/loops/program/steer", http_tags=["dag", "agents"],
    description="Inject a STEERING NOTE into a running V8 program — a high-level "
                "course-correction the program controller and its loops read and act "
                "on (e.g. 'you keep re-running the same search — move on to drafting', "
                "'this has drifted off the master plan; refocus on X'). This is how the "
                "dream orchestrator / operator steers a program WITHOUT spawning its own "
                "competing loops: the note reaches the controller's next adaptation pass "
                "and the next loop run's context, then is consumed. Inputs: id (str!), "
                "note (str!), source (str default 'dream'). Output: {ok, id, notes}.",
)
async def cap_loops_program_steer(id: str = "", note: str = "", source: str = "dream",
                                  trace_id=None):
    prog = await _prog_get(id)
    if not prog:
        return {"error": f"unknown program: {id}"}
    note = str(note or "").strip()
    if not note:
        return {"error": "note required"}
    prog.setdefault("steer_notes", []).append(
        {"ts": now_iso(), "note": note[:600],
         "source": str(source or "dream")[:40], "consumed": False})
    prog["steer_notes"] = prog["steer_notes"][-20:]
    await _prog_save(prog)
    await emit_event({"type": "agent_loop_v8.program_steered", "program": id,
                      "note": note[:200], "source": source})
    return {"ok": True, "id": id, "notes": len(prog["steer_notes"])}


# Keep V8's caps OUT of loop toolkits — a loop must never spawn programs.
def _extend_loop_blacklist() -> None:
    dw = (sys.modules.get("dag_workshop_capabilities")
          or sys.modules.get("Vera.vera.dag.dag_workshop_capabilities"))
    try:
        bl = getattr(dw, "_DEFAULT_CAP_BLACKLIST", None)
        if isinstance(bl, set):
            bl.update({"dag.agent_loop_v8", "loops.program.create", "loops.generate",
                       "loops.program.tick", "loops.config.set", "loops.program.steer"})
    except Exception as e:
        log.debug("loop_orchestrator: could not extend loop blacklist: %s", e)


_extend_loop_blacklist()

log.info("loop_orchestrator: V8 program orchestrator ready")
