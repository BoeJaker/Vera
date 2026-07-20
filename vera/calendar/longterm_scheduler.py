"""
longterm_scheduler.py — Long-term plan scheduling for the agentic loops
=======================================================================

The scheduling brain that sits between the CALENDAR (what the user has
committed to) and the AGENTIC LOOPS (what Vera can actually do). It lets Vera —
or the ``long-term-scheduling`` loop profile — look at the user's calendar AND
the dream calendar, then lay down **scheduled actions** and **trigger
thresholds** that fire later:

  • SYSTEM-side actions run UNATTENDED — Vera executes the action's goal through
    a specialised loop when its time comes or its trigger trips. While a system
    action runs, the dream scheduler stands aside (a busy flag it checks), and a
    system action never spawns a dream — so scheduled work is never interfered
    with by, or run through, the dream system.

  • USER-side actions need the human. When one comes due, Vera sends a
    notification with instructions over the comms system (Telegram) and WAITS
    for the user's reply — the inbound comms channel routes the reply back here
    via :func:`resolve_schedule_reply` (see comms_inbox + telegram_capabilities).

Capabilities (group ``sched.*``)
────────────────────────────────
  sched.plan.generate   read calendar + dream calendar → LLM proposes actions
  sched.plan.list       list scheduled actions
  sched.action.upsert   create / update one action
  sched.action.delete   remove an action
  sched.action.respond  record a user reply to a user-side notification
  sched.triggers.list   actions that carry a threshold trigger + last eval
  sched.tick            run one evaluation pass now (also runs on a timer)
  sched.config.get / sched.config.set

Redis layout
────────────
  vera:sched:actions      hash  id -> JSON action
  vera:sched:config       string JSON
  vera:sched:system_busy  string (TTL) — set while a SYSTEM action runs; the
                          dream scheduler checks this to stand aside.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    CAPABILITY_REGISTRY,
    capability,
    emit_event,
    now_iso,
    ollama_generate,
    schedule,
)

log = logging.getLogger("vera.longterm_scheduler")

KEY_ACTIONS      = "vera:sched:actions"
KEY_CONFIG       = "vera:sched:config"
KEY_SYSTEM_BUSY  = "vera:sched:system_busy"

# The busy flag has a TTL so a crashed/hung system run can't wedge dreams forever.
_SYSTEM_BUSY_TTL = 3600

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled":          True,
    "tick_seconds":     60,      # evaluation cadence
    "default_profile":  "planning",   # loop profile used to run system actions
    "comms_channel":    "telegram",
    "model":            "",
    "max_concurrent_system": 1,
}

_STATUSES = ("pending", "scheduled", "notified", "awaiting_reply",
             "running", "done", "failed", "cancelled")

# in-flight system action ids (avoid double-firing across ticks)
_RUNNING: set = set()


def _redis():
    return getattr(_orch, "REDIS", None)


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

async def _get_config() -> Dict[str, Any]:
    r = _redis()
    merged = dict(DEFAULT_CONFIG)
    if not r:
        return merged
    try:
        raw = await r.get(KEY_CONFIG)
        if raw:
            merged.update(json.loads(raw))
    except Exception as e:
        log.debug("sched config read: %s", e)
    return merged


async def _set_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = await _get_config()
    cur.update({k: v for k, v in (patch or {}).items() if k in DEFAULT_CONFIG})
    r = _redis()
    if r:
        try:
            await r.set(KEY_CONFIG, json.dumps(cur))
        except Exception as e:
            log.debug("sched config write: %s", e)
    return cur


# ─────────────────────────────────────────────────────────────────────────────
# ACTION STORE
# ─────────────────────────────────────────────────────────────────────────────

def _norm_action(a: Dict[str, Any]) -> Dict[str, Any]:
    side = (a.get("side") or "system").strip().lower()
    if side not in ("system", "user"):
        side = "system"
    status = (a.get("status") or "pending").strip().lower()
    if status not in _STATUSES:
        status = "pending"
    return {
        "id":          str(a.get("id") or uuid.uuid4()),
        "title":       str(a.get("title") or "").strip() or "Untitled action",
        "description": str(a.get("description") or ""),
        "side":        side,
        "goal":        str(a.get("goal") or ""),
        "profile":     str(a.get("profile") or ""),
        "when":        str(a.get("when") or ""),
        "trigger":     a.get("trigger") or {},
        "instructions": str(a.get("instructions") or ""),
        "comms_channel": str(a.get("comms_channel") or ""),
        "status":      status,
        "response":    str(a.get("response") or ""),
        "last_eval":   a.get("last_eval") or "",
        "last_run":    a.get("last_run") or "",
        "result_summary": a.get("result_summary") or "",
        "created":     a.get("created") or now_iso(),
        "updated":     now_iso(),
    }


async def _save_action(a: Dict[str, Any]) -> Dict[str, Any]:
    a = _norm_action(a)
    r = _redis()
    if r:
        try:
            existing = await r.hget(KEY_ACTIONS, a["id"])
            if existing:
                try:
                    a["created"] = json.loads(existing).get("created", a["created"])
                except Exception:
                    pass
            await r.hset(KEY_ACTIONS, a["id"], json.dumps(a))
        except Exception as e:
            log.warning("sched save: %s", e)
    return a


async def _list_actions() -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return []
    out: List[Dict[str, Any]] = []
    try:
        items = await r.hgetall(KEY_ACTIONS)
        for v in (items or {}).values():
            try:
                out.append(json.loads(v))
            except Exception:
                continue
    except Exception as e:
        log.debug("sched list: %s", e)
    out.sort(key=lambda x: (x.get("when") or "9999", x.get("created") or ""))
    return out


async def _get_action(aid: str) -> Optional[Dict[str, Any]]:
    r = _redis()
    if not r or not aid:
        return None
    try:
        raw = await r.hget(KEY_ACTIONS, aid)
        return json.loads(raw) if raw else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# DREAM EXCLUSION — system busy flag the dream scheduler checks
# ─────────────────────────────────────────────────────────────────────────────

async def _set_system_busy(action_id: str) -> None:
    r = _redis()
    if not r:
        return
    try:
        await r.set(KEY_SYSTEM_BUSY, action_id, ex=_SYSTEM_BUSY_TTL)
    except Exception:
        pass


async def _clear_system_busy() -> None:
    r = _redis()
    if not r:
        return
    try:
        await r.delete(KEY_SYSTEM_BUSY)
    except Exception:
        pass


async def system_schedule_busy() -> bool:
    """True while a SYSTEM-side scheduled action is running. The dream scheduler
    imports and checks this so dreams stand aside (they never run through or
    interfere with system-side scheduled work)."""
    r = _redis()
    if not r:
        return False
    try:
        return bool(await r.get(KEY_SYSTEM_BUSY))
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# TRIGGER EVALUATION
# ─────────────────────────────────────────────────────────────────────────────

def _dig(obj: Any, path: str) -> Any:
    """Pull a dotted path out of a nested dict/list result."""
    cur = obj
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except Exception:
                return None
        else:
            return None
    return cur


def _compare(left: Any, op: str, right: Any) -> bool:
    op = (op or ">=").strip()
    try:
        if op in (">=", ">", "<=", "<"):
            lf, rf = float(left), float(right)
            return {">=": lf >= rf, ">": lf > rf,
                    "<=": lf <= rf, "<": lf < rf}[op]
        if op == "==":
            return str(left) == str(right)
        if op == "!=":
            return str(left) != str(right)
        if op == "contains":
            return str(right).lower() in str(left).lower()
    except Exception:
        return False
    return False


async def _eval_trigger(trig: Dict[str, Any]) -> Optional[bool]:
    """Evaluate a threshold trigger by calling its cap and comparing a field.

    trigger = {cap, args, path, op, value}. Returns True/False, or None when it
    can't be evaluated (missing cap / error) so the caller leaves the action
    pending rather than firing on a mistake."""
    if not isinstance(trig, dict) or not trig.get("cap"):
        return None
    cap = CAPABILITY_REGISTRY.get(trig["cap"])
    if not cap or not cap.get("func"):
        return None
    try:
        args = trig.get("args") or {}
        res = await cap["func"](**args) if isinstance(args, dict) else await cap["func"]()
        left = _dig(res, trig.get("path", ""))
        if left is None and not trig.get("path"):
            left = res
        return _compare(left, trig.get("op", ">="), trig.get("value"))
    except Exception as e:
        log.debug("sched trigger eval (%s): %s", trig.get("cap"), e)
        return None


def _due_by_time(action: Dict[str, Any]) -> bool:
    dt = _parse_iso(action.get("when", ""))
    return bool(dt and _now_dt() >= dt)


# ─────────────────────────────────────────────────────────────────────────────
# COMMS — user-side notification + reply round-trip
# ─────────────────────────────────────────────────────────────────────────────

async def _comms_address() -> str:
    """Resolve the Telegram admin chat id (the reply address)."""
    cfg_cap = CAPABILITY_REGISTRY.get("tg.config.get")
    try:
        cfg = await cfg_cap["func"]() if cfg_cap and cfg_cap.get("func") else {}
        conf = (cfg or {}).get("config", cfg) or {}
        return str(conf.get("admin_chat_id") or "").strip()
    except Exception:
        return ""


async def _notify_user(action: Dict[str, Any], channel: str) -> bool:
    """Send the user-side notification out over comms and register a pending
    reply so an inbound message resolves this action."""
    text = (f"🗓️ Scheduled action: {action['title']}\n\n"
            + (action.get("instructions") or action.get("description")
               or action.get("goal") or "")
            + "\n\n(Reply to this message with your response.)")
    tg = CAPABILITY_REGISTRY.get("tg.notify")
    if not tg or not tg.get("func"):
        log.debug("sched: tg.notify unavailable — cannot notify user")
        return False
    try:
        await tg["func"](text=text)
    except Exception as e:
        log.warning("sched notify send failed: %s", e)
        return False
    addr = await _comms_address()
    if addr:
        try:
            import Vera.vera.comms_inbox as _inbox
            _inbox.register(addr, kind="schedule", channel=channel or "telegram",
                            question=action["title"],
                            meta={"action_id": action["id"]},
                            ttl_secs=7 * 86400.0)
        except Exception as e:
            log.debug("sched comms inbox register failed: %s", e)
    return True


async def resolve_schedule_reply(action_id: str, text: str) -> bool:
    """Record a user reply to a user-side notification. Called by the inbound
    comms channel (telegram) when the user answers. Returns True if matched."""
    a = await _get_action(action_id)
    if not a:
        return False
    a["response"] = str(text or "")[:4000]
    a["status"] = "done"
    a["last_run"] = now_iso()
    await _save_action(a)
    await emit_event({"type": "sched.action.responded", "id": action_id,
                      "title": a.get("title", "")})
    log.info("sched: user replied to action %s", action_id)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM-side execution — run the action's goal through a specialised loop
# ─────────────────────────────────────────────────────────────────────────────

async def _run_system_action(action: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    aid = action["id"]
    _RUNNING.add(aid)
    await _set_system_busy(aid)
    action["status"] = "running"
    action["last_run"] = now_iso()
    await _save_action(action)
    await emit_event({"type": "sched.action.started", "id": aid,
                      "title": action.get("title", ""), "side": "system"})
    ok = False
    summary = ""
    try:
        goal = action.get("goal") or action.get("description") or action.get("title")
        profile = action.get("profile") or cfg.get("default_profile") or "planning"
        runner = CAPABILITY_REGISTRY.get("loops.run")
        if runner and runner.get("func"):
            res = await runner["func"](profile=profile, goal=goal,
                                       session_id=f"sched:{aid}",
                                       model=cfg.get("model", ""))
        else:
            # Fallback: call the v6 engine directly.
            eng = CAPABILITY_REGISTRY.get("dag.agent_loop_v6")
            res = (await eng["func"](goal=goal, session_id=f"sched:{aid}")
                   if eng and eng.get("func") else {"error": "no loop engine"})
        if isinstance(res, dict):
            summary = str(res.get("deliverable") or res.get("final")
                          or res.get("summary") or res.get("error") or "")[:1500]
            ok = not res.get("error")
    except Exception as e:
        summary = f"exception: {e}"
        log.warning("sched system action %s failed: %s", aid, e)
    finally:
        await _clear_system_busy()
        _RUNNING.discard(aid)
    fresh = await _get_action(aid) or action
    fresh["status"] = "done" if ok else "failed"
    fresh["result_summary"] = summary
    fresh["last_run"] = now_iso()
    await _save_action(fresh)
    await emit_event({"type": "sched.action.finished", "id": aid, "ok": ok,
                      "title": fresh.get("title", ""), "summary": summary[:300]})


# ─────────────────────────────────────────────────────────────────────────────
# THE TICK — evaluate all actions, fire the due ones
# ─────────────────────────────────────────────────────────────────────────────

async def _evaluate_once() -> Dict[str, Any]:
    cfg = await _get_config()
    actions = await _list_actions()
    fired: List[str] = []
    for a in actions:
        status = a.get("status")
        if status in ("done", "failed", "cancelled", "running", "awaiting_reply"):
            continue
        trig = a.get("trigger") or {}
        due = False
        if trig and trig.get("cap"):
            verdict = await _eval_trigger(trig)
            a["last_eval"] = now_iso()
            await _save_action(a)
            if verdict is True:
                due = True
        if not due and a.get("when"):
            due = _due_by_time(a)
        if not due:
            continue

        if a.get("side") == "system":
            # Respect the concurrency cap so parallel system runs don't stampede.
            if len(_RUNNING) >= int(cfg.get("max_concurrent_system", 1)):
                continue
            asyncio.create_task(_run_system_action(a, cfg))
            fired.append(a["id"])
        else:
            channel = a.get("comms_channel") or cfg.get("comms_channel") or "telegram"
            sent = await _notify_user(a, channel)
            a["status"] = "awaiting_reply" if sent else "notified"
            a["last_run"] = now_iso()
            await _save_action(a)
            fired.append(a["id"])
    return {"evaluated": len(actions), "fired": fired}


async def _tick():
    """Timer entrypoint (schedule). Self-throttles to the configured cadence and
    is a no-op when disabled."""
    try:
        cfg = await _get_config()
        if not cfg.get("enabled", True):
            return
        # Self-throttle: only run when at least tick_seconds have passed.
        global _LAST_TICK
        now = time.time()
        if now - _LAST_TICK < float(cfg.get("tick_seconds", 60)) - 1:
            return
        _LAST_TICK = now
        await _evaluate_once()
    except Exception as e:
        log.debug("sched tick: %s", e)


_LAST_TICK: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# LLM PLAN GENERATION — calendar + dream calendar → proposed actions
# ─────────────────────────────────────────────────────────────────────────────

_PLAN_SYSTEM = (
    "You are Vera's long-term planner. Given the user's upcoming CALENDAR events, "
    "the DREAM calendar (background cognition fires), and a planning goal, propose "
    "concrete SCHEDULED ACTIONS and TRIGGER THRESHOLDS.\n\n"
    "Rules:\n"
    "• Each action has a SIDE: 'system' (Vera runs it unattended) or 'user' (the "
    "user must do/decide something — Vera will notify them over comms and wait).\n"
    "• Plan AROUND existing calendar and dream events, never over them.\n"
    "• Prefer a TRIGGER (a threshold condition) over a fixed time when a condition "
    "expresses the intent better.\n"
    "• Give system actions a concrete 'goal' (a natural-language task Vera can run "
    "through an agentic loop). Give user actions clear 'instructions'.\n\n"
    "Respond with STRICT JSON: {\"actions\": [ {\"title\": str, \"side\": "
    "\"system\"|\"user\", \"goal\": str, \"instructions\": str, \"when\": "
    "\"ISO8601 or empty\", \"trigger\": {\"cap\": str, \"args\": {}, \"path\": "
    "str, \"op\": \">=|<=|>|<|==|!=|contains\", \"value\": any} , \"why\": str } ] }"
)


async def _gather_calendar(horizon_days: int) -> Dict[str, Any]:
    """Pull upcoming calendar events + dream schedule events for the horizon."""
    out: Dict[str, Any] = {"calendar": [], "dream": []}
    start = _now_dt()
    end = start + timedelta(days=max(1, horizon_days))
    cal = CAPABILITY_REGISTRY.get("cal.events.list")
    if cal and cal.get("func"):
        try:
            res = await cal["func"](start=start.date().isoformat(),
                                    end=end.date().isoformat())
            out["calendar"] = (res or {}).get("events", res) if isinstance(res, dict) else res
        except Exception as e:
            log.debug("sched gather calendar: %s", e)
    dream = CAPABILITY_REGISTRY.get("dream.schedule.events")
    if dream and dream.get("func"):
        try:
            res = await dream["func"]()
            out["dream"] = (res or {}).get("events", res) if isinstance(res, dict) else res
        except Exception as e:
            log.debug("sched gather dream: %s", e)
    return out


def _strip_json(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if "```" in s[3:] else s[3:]
        if s.lstrip().startswith("json"):
            s = s.lstrip()[4:]
    a, b = s.find("{"), s.rfind("}")
    return s[a:b + 1] if a >= 0 and b > a else s


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "sched.plan.generate", memory="on",
    http_method="POST", http_path="/sched/plan/generate", http_tags=["scheduler"],
    description="Look at the user's calendar AND the dream calendar over a horizon "
                "and propose long-term SCHEDULED ACTIONS + TRIGGER THRESHOLDS. "
                "Inputs: goal (str — planning intent), horizon_days (int, default "
                "14), commit (bool, default False — also persist the proposed "
                "actions), model (str). "
                "Output: {proposal:[...], committed:[ids], calendar, dream}.",
)
async def cap_sched_plan_generate(goal: str = "", horizon_days: int = 14,
                                  commit: bool = False, model: str = "",
                                  trace_id=None):
    cfg = await _get_config()
    ctx = await _gather_calendar(int(horizon_days or 14))
    prompt = json.dumps({
        "planning_goal": goal or "Plan the coming period proactively.",
        "now": now_iso(),
        "horizon_days": int(horizon_days or 14),
        "calendar_events": ctx.get("calendar"),
        "dream_events": ctx.get("dream"),
    }, default=str)[:12000]
    actions: List[Dict[str, Any]] = []
    try:
        raw = await ollama_generate(prompt, system=_PLAN_SYSTEM, json_mode=True,
                                    model=(model or cfg.get("model") or None),
                                    prefer_gpu=True)
        parsed = json.loads(_strip_json(raw))
        actions = parsed.get("actions", []) if isinstance(parsed, dict) else []
    except Exception as e:
        return {"error": f"plan generation failed: {e}", "calendar": ctx.get("calendar"),
                "dream": ctx.get("dream")}
    committed: List[str] = []
    if commit:
        for a in actions:
            saved = await _save_action({**a, "status": "pending"})
            committed.append(saved["id"])
        await emit_event({"type": "sched.plan.generated", "count": len(actions),
                          "committed": len(committed)})
    return {"proposal": actions, "committed": committed,
            "calendar": ctx.get("calendar"), "dream": ctx.get("dream")}


@capability(
    "sched.plan.list", memory="off", silent=True,
    http_method="GET", http_path="/sched/plan", http_tags=["scheduler"],
    description="List all scheduled actions. Output: {actions:[...], "
                "system_busy: bool}.",
)
async def cap_sched_plan_list(trace_id=None):
    return {"actions": await _list_actions(),
            "system_busy": await system_schedule_busy()}


@capability(
    "sched.action.upsert", memory="on",
    http_method="POST", http_path="/sched/action/upsert", http_tags=["scheduler"],
    description="Create or update a scheduled action. Inputs: id (str — omit to "
                "create), title (str!), side ('system'|'user'), goal (str — system "
                "task), instructions (str — user notification text), when (ISO8601 "
                "time, optional), trigger (object {cap,args,path,op,value}, "
                "optional), profile (str — loop profile for system runs), "
                "comms_channel (str), status (str). Output: the saved action.",
)
async def cap_sched_action_upsert(id: str = "", title: str = "", side: str = "system",
                                  goal: str = "", instructions: str = "",
                                  description: str = "", when: str = "",
                                  trigger: dict = None, profile: str = "",
                                  comms_channel: str = "", status: str = "",
                                  trace_id=None):
    if not id and not (title or goal):
        return {"error": "title or goal required"}
    rec: Dict[str, Any] = {"title": title, "side": side, "goal": goal,
                           "instructions": instructions, "description": description,
                           "when": when, "trigger": trigger or {}, "profile": profile,
                           "comms_channel": comms_channel}
    if id:
        rec["id"] = id
    if status:
        rec["status"] = status
    saved = await _save_action(rec)
    await emit_event({"type": "sched.action.upserted", "id": saved["id"],
                      "title": saved["title"], "side": saved["side"]})
    return saved


@capability(
    "sched.action.delete", memory="on",
    http_method="POST", http_path="/sched/action/delete", http_tags=["scheduler"],
    description="Delete a scheduled action by id. Input: id (str!). Output: {ok}.",
)
async def cap_sched_action_delete(id: str = "", trace_id=None):
    r = _redis()
    if not r or not id:
        return {"ok": False, "error": "id required"}
    try:
        n = await r.hdel(KEY_ACTIONS, id)
        await emit_event({"type": "sched.action.deleted", "id": id})
        return {"ok": bool(n)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@capability(
    "sched.action.respond", memory="on",
    http_method="POST", http_path="/sched/action/respond", http_tags=["scheduler"],
    description="Record a user reply to a user-side scheduled notification "
                "(also called by the inbound comms channel). Inputs: id (str!), "
                "text (str!). Output: {ok}.",
)
async def cap_sched_action_respond(id: str = "", text: str = "", trace_id=None):
    ok = await resolve_schedule_reply(id, text)
    return {"ok": ok}


@capability(
    "sched.triggers.list", memory="off", silent=True,
    http_method="GET", http_path="/sched/triggers", http_tags=["scheduler"],
    description="List scheduled actions that carry a threshold trigger, with their "
                "last evaluation time. Output: {triggers:[{id,title,trigger,"
                "last_eval,status}]}.",
)
async def cap_sched_triggers_list(trace_id=None):
    out = []
    for a in await _list_actions():
        t = a.get("trigger") or {}
        if t and t.get("cap"):
            out.append({"id": a["id"], "title": a["title"], "trigger": t,
                        "last_eval": a.get("last_eval", ""), "status": a.get("status"),
                        "side": a.get("side")})
    return {"triggers": out}


@capability(
    "sched.tick", memory="off", silent=True,
    http_method="POST", http_path="/sched/tick", http_tags=["scheduler"],
    description="Run one scheduler evaluation pass immediately (fire any due "
                "actions/triggers). Output: {evaluated, fired:[ids]}.",
)
async def cap_sched_tick(trace_id=None):
    return await _evaluate_once()


@capability(
    "sched.config.get", memory="off", silent=True,
    http_method="GET", http_path="/sched/config", http_tags=["scheduler"],
    description="Get the long-term scheduler config. Output: the config object.",
)
async def cap_sched_config_get(trace_id=None):
    return await _get_config()


@capability(
    "sched.config.set", memory="on",
    http_method="POST", http_path="/sched/config/set", http_tags=["scheduler"],
    description="Update the long-term scheduler config. Inputs: enabled (bool), "
                "tick_seconds (int), default_profile (str), comms_channel (str), "
                "model (str), max_concurrent_system (int). Output: the config.",
)
async def cap_sched_config_set(enabled: bool = None, tick_seconds: int = None,
                               default_profile: str = None, comms_channel: str = None,
                               model: str = None, max_concurrent_system: int = None,
                               trace_id=None):
    patch = {k: v for k, v in {
        "enabled": enabled, "tick_seconds": tick_seconds,
        "default_profile": default_profile, "comms_channel": comms_channel,
        "model": model, "max_concurrent_system": max_concurrent_system,
    }.items() if v is not None}
    return await _set_config(patch)


# Evaluate every 30s; _tick self-throttles to the configured cadence.
schedule(_tick, 30, name="longterm_scheduler")

log.info("longterm_scheduler: ready (sched.* capabilities + timer)")
