"""
agents_context_patch.py  —  Wire skills/ontologies/dags into AgentRunner
========================================================================

The original AgentRunner.run / run_stream methods build their system prompt
from agent.system_prompt + agent.domain_description (+ memory if enabled).
They never consult agent.skill_ids or agent.ontology_ids — which is why
skills/ontologies set in the agent builder never reach the LLM.

This module patches both runners with a single helper that:

  1. Starts with agent.system_prompt + domain_description (existing behaviour).
  2. Calls vera_context.build_context_prompt(...) using:
       attach_skills      = ",".join(agent.skill_ids)      (or "" if none)
       attach_ontologies  = ",".join(agent.ontology_ids)
       attach_caps        = "auto" if tool_mode != "none" and agent.domain_caps else
                            ",".join(agent.domain_caps) for explicit list
       attach_dags        = "auto" if agent.attach_dags is True (new optional field)
  3. Injects memory context (existing behaviour kept).
  4. Emits an event noting which skills/ontologies/dags were applied so the
     UI can show what's loaded in this turn.

Import order
────────────
  Load this module AFTER agents.py and AFTER vera_context.py.
  Both must be importable (sys.modules) by the time this module runs.

Usage
─────
  In your bootstrap file, after importing the other modules:

      import agents
      import skills
      import dag_store
      import vera_context
      import agents_context_patch          # applies the patch

  The patch is idempotent — applying it twice is a no-op.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional

import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import emit_event, now_iso

log = logging.getLogger("vera.agents_context_patch")

_PATCHED_FLAG = "_vera_context_patched"


def _ctx():
    return sys.modules.get("vera_context")


def _agents_module():
    return sys.modules.get("agents") or sys.modules.get("vera_agents")


def _build_attach_specs(agent) -> Dict[str, str]:
    """Map AgentRecord fields onto build_context_prompt() arg specs."""
    skill_ids    = list(getattr(agent, "skill_ids", []) or [])
    ontology_ids = list(getattr(agent, "ontology_ids", []) or [])

    # Caps spec — agent.domain_caps controls
    tool_mode = getattr(agent, "tool_mode", "none") or "none"
    domain    = list(getattr(agent, "domain_caps", []) or [])
    if tool_mode == "none":
        caps_spec = ""
    elif domain == ["*"]:
        caps_spec = "auto"           # full registry is too much — let it search
    elif domain:
        caps_spec = ",".join(domain)
    else:
        caps_spec = ""

    # DAGs — controlled by an optional bool field. Falls back gracefully
    # if AgentRecord doesn't have it yet.
    attach_dags = bool(getattr(agent, "attach_dags", False))
    dags_spec = "auto" if attach_dags else ""

    return {
        "attach_skills":     ",".join(skill_ids) if skill_ids else "",
        "attach_ontologies": ",".join(ontology_ids) if ontology_ids else "",
        "attach_caps":       caps_spec,
        "attach_dags":       dags_spec,
    }


async def build_agent_system_prompt(agent, message: str, session_id: str = "") -> str:
    """The shared system-prompt builder used by both runners after the patch."""
    base = agent.system_prompt or ""

    if getattr(agent, "domain_description", ""):
        base += f"\n\nDomain: {agent.domain_description}"

    specs = _build_attach_specs(agent)
    ctx_module = _ctx()

    # If vera_context isn't loaded for some reason, fall back to memory-only
    # injection (matches the old behaviour for skills/ontologies — they just
    # won't be loaded, which is what was happening before the patch).
    skills_loaded:    List[str] = []
    onts_loaded:      List[str] = []
    caps_loaded:      List[str] = []
    dags_loaded:      List[Dict] = []

    if ctx_module and any(specs.values()):
        try:
            ctx = await ctx_module.build_context_prompt(
                message,
                attach_skills     = specs["attach_skills"],
                attach_ontologies = specs["attach_ontologies"],
                attach_caps       = specs["attach_caps"],
                attach_dags       = specs["attach_dags"],
                attach_memory     = False,         # memory injected below — old code path
                session_id        = session_id,
                agent_name        = agent.name,
            )
            block = ctx.get("system_prompt") or ""
            if block:
                base += "\n\n" + block
            skills_loaded = ctx.get("skills") or []
            onts_loaded   = ctx.get("ontologies") or []
            caps_loaded   = ctx.get("caps") or []
            dags_loaded   = ctx.get("dags") or []
        except Exception as e:
            log.warning("agent context build failed for %s: %s", agent.name, e)

    # Memory injection — preserved from original
    if getattr(agent, "memory_inject", False) and session_id:
        try:
            mh = sys.modules.get("memory_hooks")
            if mh:
                mem_ctx = await mh.get_agent_memory_context(
                    session_id  = session_id,
                    query       = message,
                    agent_name  = agent.name,
                    limit       = getattr(agent, "memory_inject_limit", 5),
                    tags        = [t.strip() for t in
                                    getattr(agent, "memory_tags", "").split(",")
                                    if t.strip()] or None,
                )
                if mem_ctx:
                    base += "\n\n" + mem_ctx
        except Exception as e:
            log.debug("agent memory inject: %s", e)

    # Emit a single event so the UI can show what was loaded this turn
    try:
        await emit_event({
            "type":         "agent.context_applied",
            "agent_name":   agent.name,
            "session_id":   session_id,
            "skills":       skills_loaded,
            "ontologies":   onts_loaded,
            "caps":         caps_loaded,
            "dags":         [d.get("name") for d in dags_loaded],
            "ts":           now_iso(),
        })
    except Exception:
        pass

    return base


def _patch_run_method(orig_run):
    """Wrap AgentRunner.run with shared system-prompt assembly."""
    async def patched_run(self, agent, message, history=None, session_id=""):
        # Compute the new system prompt up front and store on a per-call attr
        # that we'll splice into the body via a temporary copy of the agent.
        new_system = await build_agent_system_prompt(agent, message, session_id)
        # Use a shallow copy so we don't mutate the registered AgentRecord
        import copy
        agent2 = copy.copy(agent)
        agent2.system_prompt      = new_system
        # Preserve domain_description=None so the inner code doesn't append it
        # again — we've already included it in the assembled prompt.
        agent2.domain_description = ""
        # Tell the inner run to skip its own domain_caps + memory injection —
        # we've already done both.
        agent2.domain_caps        = []
        agent2.memory_inject      = False
        return await orig_run(self, agent2, message, history, session_id)

    patched_run.__name__ = "run"
    patched_run.__qualname__ = orig_run.__qualname__
    return patched_run


def _patch_run_stream_method(orig_stream):
    """Wrap AgentRunner.run_stream with shared system-prompt assembly.

    run_stream is an async generator. We materialise the agent copy first,
    then yield from the original.
    """
    async def patched_run_stream(self, agent, message, history=None,
                                  session_id="", use_tts=False):
        new_system = await build_agent_system_prompt(agent, message, session_id)
        import copy
        agent2 = copy.copy(agent)
        agent2.system_prompt      = new_system
        agent2.domain_description = ""
        agent2.domain_caps        = []
        agent2.memory_inject      = False
        async for chunk in orig_stream(self, agent2, message, history,
                                        session_id, use_tts):
            yield chunk

    patched_run_stream.__name__ = "run_stream"
    patched_run_stream.__qualname__ = orig_stream.__qualname__
    return patched_run_stream


def apply():
    """Apply the patch. Idempotent — safe to call multiple times."""
    am = _agents_module()
    if not am:
        log.warning("agents_context_patch: agents module not loaded — skipping")
        return False

    AgentRunner = getattr(am, "AgentRunner", None)
    if AgentRunner is None:
        log.warning("agents_context_patch: AgentRunner class not found — skipping")
        return False

    if getattr(AgentRunner, _PATCHED_FLAG, False):
        return True   # already patched

    if not _ctx():
        log.warning("agents_context_patch: vera_context not loaded yet — "
                    "patch deferred. Import vera_context before this module.")
        return False

    orig_run    = AgentRunner.run
    orig_stream = AgentRunner.run_stream

    AgentRunner.run        = _patch_run_method(orig_run)
    AgentRunner.run_stream = _patch_run_stream_method(orig_stream)
    setattr(AgentRunner, _PATCHED_FLAG, True)

    log.info("agents_context_patch: AgentRunner.run / run_stream patched — "
             "skills, ontologies, caps and DAG hints now flow into agent context")
    return True


# Auto-apply on import
apply()