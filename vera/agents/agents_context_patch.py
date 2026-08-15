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
  3. Emits an event noting which skills/ontologies/dags were applied so the
     UI can show what's loaded in this turn.

  Memory injection is deliberately NOT this module's job (2026-08-15,
  unify-v2 fix): it used to duplicate the original run/run_stream's own
  memory block with a second, hand-rolled v1 call here, forcing
  agent.memory_inject=False before calling through so the original's block
  never ran. That left two implementations doing the same thing — one dead
  (the original's, already on get_agent_memory_context_v2 and already
  SUPPRESS_MEMORY_INJECT-aware), one live (this module's, v1, and NOT
  SUPPRESS_MEMORY_INJECT-aware). The patch now passes agent.memory_inject
  through unchanged and lets the original run/run_stream inject memory
  themselves, same as before this module existed.

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

import asyncio
import logging
import sys
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import emit_event, now_iso

log = logging.getLogger("vera.agents_context_patch")

_PATCHED_FLAG = "_vera_context_patched"


def _ctx():
    return sys.modules.get("context")


def _agents_module():
    return sys.modules.get("vera_agents")


# ─────────────────────────────────────────────────────────────────────────────
# DOMAIN-CONTEXT ENRICHMENT
# ─────────────────────────────────────────────────────────────────────────────
# When the user's message is clearly ABOUT a domain (diary / email / Telegram)
# AND the agent actually owns that domain's briefing/list capability, we fetch a
# compact LIVE snapshot and fold it into the system prompt. This mirrors what
# already happens when the matching UI panel is open (its state is injected into
# the prompt) — but here it is triggered by the TOPIC, so the agent is grounded
# in real data (today's date, existing events, recent mail) without the user
# having to open anything. It is the reason the same request works when the
# calendar panel is open and flails when it isn't.
#
# Auto-scoped by capability: enrichment for a domain only runs if the agent has
# that domain's cap in domain_caps — so no new AgentRecord field is needed and a
# calendar-less agent never pays for a briefing. Keyword-gated so ordinary chat
# turns skip it entirely. Each fetch is time-boxed and best-effort: any failure
# just omits that block.

_ENRICH_KW_CAL = (
    "calendar", "diary", "schedul", "meeting", "event", "appointment", "reschedul",
    "remind", "todo", "to-do", "agenda", "tomorrow", "today", "tonight",
    "next week", "this week", "next month", "book a", "booking", "free time",
    "busy", "availab", "plan my", "what's on", "whats on", "my day", "my week",
)
_ENRICH_KW_MAIL = (
    "email", "e-mail", "inbox", "mailbox", " mail", "unread", "sender",
    "reply to", "respond to", "forward the",
)
_ENRICH_KW_TG = ("telegram", " tg ", "direct message", "dm ")


def _has(msg: str, words) -> bool:
    return any(w in msg for w in words)


def _fmt_cal(b: Dict[str, Any]) -> str:
    if not isinstance(b, dict) or b.get("error"):
        return ""
    def _evline(e):
        t = e.get("start") or e.get("start_time") or e.get("when") or ""
        ttl = e.get("title") or e.get("summary") or "(untitled)"
        loc = e.get("location") or ""
        return f"  · {t} {ttl}" + (f" @ {loc}" if loc else "")
    lines = [f"Now: {b.get('today_human') or b.get('now','')} (tz {b.get('tz','?')})"]
    today = b.get("today") or []
    tomorrow = b.get("tomorrow") or []
    upcoming = b.get("upcoming") or []
    lines.append(f"Today ({len(today)}):" + (" none" if not today else ""))
    lines += [_evline(e) for e in today[:8]]
    lines.append(f"Tomorrow ({len(tomorrow)}):" + (" none" if not tomorrow else ""))
    lines += [_evline(e) for e in tomorrow[:8]]
    if upcoming:
        lines.append(f"Upcoming this week ({len(upcoming)}):")
        lines += [_evline(e) for e in upcoming[:6]]
    todos = (b.get("todos") or {})
    oc = todos.get("open_count", 0)
    if oc:
        od = todos.get("overdue") or []
        dt = todos.get("due_today") or []
        tl = [f"{len(od)} overdue" if od else "", f"{len(dt)} due today" if dt else "",
              f"{oc} open"]
        lines.append("Todos: " + ", ".join(x for x in tl if x))
    return "\n".join(lines)


def _fmt_mail(b: Dict[str, Any]) -> str:
    if not isinstance(b, dict) or b.get("error") or b.get("disabled"):
        return ""
    msgs = b.get("messages") or b.get("inbox") or b.get("items") or []
    if not isinstance(msgs, list) or not msgs:
        return "Inbox reachable, no recent messages."
    out = [f"{len(msgs)} recent (newest first):"]
    for m in msgs[:8]:
        if not isinstance(m, dict):
            continue
        frm = m.get("from") or m.get("sender") or m.get("from_addr") or "?"
        subj = m.get("subject") or m.get("title") or "(no subject)"
        unread = "" if (m.get("seen") or m.get("read")) else "[unread] "
        when = m.get("date") or m.get("when") or ""
        out.append(f"  · {unread}{str(frm)[:40]} — {str(subj)[:60]} {when}".rstrip())
    return "\n".join(out)


def _fmt_tg(b: Dict[str, Any]) -> str:
    if not isinstance(b, dict) or b.get("error"):
        return ""
    chats = b.get("chats") or b.get("items") or []
    if not isinstance(chats, list) or not chats:
        return ""
    out = [f"Known chats ({len(chats)}):"]
    for c in chats[:10]:
        if not isinstance(c, dict):
            continue
        name = c.get("title") or c.get("name") or c.get("chat_id") or "?"
        allowed = "allowed" if c.get("allowed") else "not-allowed"
        seen = c.get("last_seen") or c.get("last") or ""
        out.append(f"  · {str(name)[:40]} ({allowed}) {seen}".rstrip())
    return "\n".join(out)


# (message-keyword tuple, cap name, call kwargs, formatter, human label)
_ENRICH_SOURCES = [
    (_ENRICH_KW_CAL,  "cal.assistant.briefing", {"days_ahead": 7}, _fmt_cal, "calendar / diary"),
    (_ENRICH_KW_MAIL, "mail.inbox.list",        {"limit": 8},      _fmt_mail, "email inbox"),
    (_ENRICH_KW_TG,   "tg.chats.list",          {},                _fmt_tg,   "Telegram chats"),
]


async def _enrich_domain_context(agent, message: str) -> str:
    """Fetch compact live snapshots for whichever domains the message is about
    AND the agent owns the cap for. Returns a single markdown block or ''."""
    caps = set(getattr(agent, "domain_caps", []) or [])
    if not caps:
        return ""
    msg = (message or "")[:400].lower()   # user text leads the message; caps hint trails
    am = _agents_module()
    call = getattr(am, "_call_registered_cap", None) if am else None
    if not call:
        return ""

    async def _one(cap, kwargs, fmt, label):
        try:
            res = await asyncio.wait_for(call(cap, **kwargs), timeout=8.0)
            body = fmt(res)
            return (label, body) if body else None
        except Exception as e:
            log.debug("enrich %s: %s", cap, e)
            return None

    jobs = [_one(cap, kwargs, fmt, label)
            for (kw, cap, kwargs, fmt, label) in _ENRICH_SOURCES
            if cap in caps and _has(msg, kw)]
    if not jobs:
        return ""

    results = await asyncio.gather(*jobs, return_exceptions=True)
    blocks = [r for r in results if isinstance(r, tuple) and r]
    if not blocks:
        return ""

    parts = ["## Live context (auto-loaded — AUTHORITATIVE, use these real values)"]
    for label, body in blocks:
        parts.append(f"### {label}\n{body}")
    parts.append("Use the real dates/ids/titles above; do not invent them. If an "
                 "action is needed, perform it with the matching capability and "
                 "report the actual result.")
    return "\n\n".join(parts)


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

    # Domain-context enrichment — live diary/mail/telegram snapshot when the
    # message is about that domain and the agent owns the cap. Grounds the model
    # in real data (today's date, existing events) the way an open panel does.
    try:
        enrich = await _enrich_domain_context(agent, message)
        if enrich:
            base += "\n\n" + enrich
    except Exception as e:
        log.debug("domain enrich failed for %s: %s", getattr(agent, "name", "?"), e)

    # Memory injection intentionally lives back in the original run/run_stream
    # (see module docstring, unify-v2) — this function no longer touches it.

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
        # Tell the inner run to skip its own domain_caps — we've already done
        # that. memory_inject is left as the agent's real setting: the inner
        # run injects memory itself (unify-v2, see module docstring).
        agent2.domain_caps        = []
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
        # memory_inject left as the agent's real setting — see _patch_run_method.
        agent2.domain_caps        = []
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