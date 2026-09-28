"""Loop Lab schedules - a calendar of censuses, suites, tasks, pipelines,
board items and capabilities, and the tick that starts them.

  evolve.schedule.list / get / upsert / delete / enable / run_now / history
  evolve.schedule.events      calendar projection (windows + runs) for a range
  evolve.schedule.tick        what the periodic job does, callable by hand
  evolve.schedule.config.get / set   master switch, timezone, tick cadence

The records and every decision live in `schedule_core` (pure); this module
owns Redis, the clock, the box state, and the launchers:

  census   -> the OFF-REPO harness (`census_all.sh` in VERA_CENSUS_DIR, one
              template, detached) - the instrument every archived census run
              came from. Only when no census is in flight, no agent loop is
              running, and no partial run files are lying about. The census
              module itself stays read-only by design; starting one is a
              scheduling act, so it lives here.
  suite    -> evolve.suite.start        task -> evolve.task.run
  pipeline -> evolve.pipeline.test / .adopt / .promote (to bleeding-edge only)
  board    -> board.dispatch            cap  -> the capability itself (fenced)

The job is `schedule(_tick, 60, singleton=True, skip_in_sandbox=True)`: one
orchestrator on the host runs it; a dev sandbox never starts real work.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    CAPABILITY_REGISTRY, capability, emit_event, schedule,
)

try:
    from Vera.vera.evolve import schedule_core as core
except ImportError:                                        # pragma: no cover
    from vera.evolve import schedule_core as core          # type: ignore

try:
    from Vera.vera.census import control as _ctl
    from Vera.vera.census.census_capabilities import CENSUS_DIR as _CENSUS_DIR
except Exception:                                          # pragma: no cover
    _ctl = None
    _CENSUS_DIR = Path(os.getenv("VERA_CENSUS_DIR", "") or (Path.home() / "loop-census")).expanduser()

log = logging.getLogger("vera.evolve.schedule")

KEY_SCHEDULES = "vera:evolve:schedules"          # hash id -> json
KEY_RUNS = "vera:evolve:schedule:runs"           # list, newest first
KEY_CONFIG = "vera:evolve:schedule:config"
KEY_ACTIVE_CENSUS = "vera:evolve:schedule:census"  # the census THIS scheduler started
MAX_RUNS = 500
DEFAULT_CONFIG = {"enabled": True, "timezone": core.DEFAULT_TZ,
                  "max_starts_per_tick": core.MAX_STARTS_PER_TICK}
_TICK_S = 60
_BY = "evolve.schedule"


def _redis():
    return getattr(_orch, "REDIS", None)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


async def _get_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    r = _redis()
    if r is None:
        return cfg
    try:
        raw = await r.get(KEY_CONFIG)
        if raw:
            cfg.update(json.loads(raw))
    except Exception:
        pass
    return cfg


async def _load_all() -> List[Dict[str, Any]]:
    r = _redis()
    if r is None:
        return []
    try:
        h = await r.hgetall(KEY_SCHEDULES)
    except Exception:
        return []
    out = []
    for v in (h or {}).values():
        try:
            out.append(json.loads(v if isinstance(v, str) else v.decode()))
        except Exception:
            continue
    out.sort(key=lambda s: (s.get("created") or "", s.get("id") or ""))
    return out


async def _save(rec: Dict[str, Any]) -> None:
    r = _redis()
    if r is None:
        raise RuntimeError("Redis unavailable")
    await r.hset(KEY_SCHEDULES, rec["id"], json.dumps(rec))


async def _append_run(run: Dict[str, Any]) -> None:
    r = _redis()
    if r is None:
        return
    try:
        await r.lpush(KEY_RUNS, json.dumps(run))
        await r.ltrim(KEY_RUNS, 0, MAX_RUNS - 1)
    except Exception:
        pass


async def _runs(limit: int = 100) -> List[Dict[str, Any]]:
    r = _redis()
    if r is None:
        return []
    try:
        raw = await r.lrange(KEY_RUNS, 0, max(0, limit - 1))
    except Exception:
        return []
    out = []
    for v in raw or []:
        try:
            out.append(json.loads(v if isinstance(v, str) else v.decode()))
        except Exception:
            continue
    return out


async def _update_run(run_id: str, **fields) -> None:
    """Patch one run record in place (newest 500 only)."""
    r = _redis()
    if r is None:
        return
    try:
        raw = await r.lrange(KEY_RUNS, 0, MAX_RUNS - 1)
        for i, v in enumerate(raw or []):
            d = json.loads(v if isinstance(v, str) else v.decode())
            if d.get("id") == run_id:
                d.update(fields)
                await r.lset(KEY_RUNS, i, json.dumps(d))
                return
    except Exception:
        pass


async def _call(name: str, **kw) -> Any:
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap or not cap.get("func"):
        return {"error": f"capability not available: {name}"}
    try:
        return await cap["func"](**kw)
    except Exception as e:
        return {"error": f"{name}: {e}"}


# ── the box ──────────────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


LAUNCH_GRACE_S = 180   # census_all.sh -> run_census.py -> census.active.json takes seconds, not minutes


def _census_state_sync(owner: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Is a census in flight, and can one be started? Read from the harness's
    own files, the way /health does, plus whether its process is alive.
    `owner` is the record of the census THIS scheduler started (Redis), so a
    launch still spinning up counts as running and a census parked on our
    own yield is told apart from a person's pause."""
    d = Path(_CENSUS_DIR)
    owner = owner or {}
    active = _ctl.read_json(str(d / _ctl.ACTIVE_NAME)) if _ctl else {}
    control = _ctl.read_json(str(d / _ctl.CONTROL_NAME)) if _ctl else {}
    view = _ctl.active_view(active) if _ctl else {}
    state = str(view.get("state") or "")
    pid = active.get("pid")
    alive = _pid_alive(pid) if pid else False
    running = state in ("running", "paused") and (alive or bool(view.get("live")))
    try:
        set_line = (d / "census_all.state").read_text(encoding="utf-8").strip()
    except Exception:
        set_line = ""
    if set_line.startswith("running") and alive:
        running = True
    # The launch we made moments ago: its shell is alive and the harness has
    # not written its report yet. That is a census in flight, not a free box.
    launching = False
    started = core.parse_iso(str(owner.get("started_at") or ""))
    if owner.get("pid") and started and (datetime.now(timezone.utc) - started).total_seconds() < LAUNCH_GRACE_S:
        launching = _pid_alive(owner["pid"]) or running
        running = running or launching
    cstate = _ctl.control_state(control) if _ctl else "run"
    control_by = str(control.get("by") or "")
    parked_by_us = bool(running and state == "paused" and cstate in ("yield", "pause")
                        and control_by == _BY and owner.get("schedule_id"))
    blocked = ""
    if not d.exists():
        blocked = f"census dir missing: {d}"
    elif not (d / "census_all.sh").exists():
        blocked = "census_all.sh not found"
    elif not running and cstate != "run" and control_by != _BY:
        blocked = f"a person's {cstate} is on file (by {control_by or '?'})"
    elif not running and any((d / n).exists() and (d / n).stat().st_size > 0
                             for n in ("census.jsonl", "census.log")):
        blocked = "partial run files present (census.jsonl / census.log) - archive or park them first"
    return {"census_running": running, "census_state": state, "census_pid": pid,
            "census_alive": alive, "census_launching": launching, "set_line": set_line,
            "harness_blocked": blocked, "control_state": cstate, "control_by": control_by,
            "census_parked_by_us": parked_by_us, "census_owner": owner.get("schedule_id", "") if running else "",
            "window_end_applied": bool(owner.get("window_end_applied")) if running else False,
            "census_run": active.get("census_run", ""), "template": active.get("template", "")}


async def _loops_running() -> int:
    """Agent loops genuinely running, read the way the census module reads
    them (the sessions endpoint is a plain route, not a capability)."""
    try:
        from Vera.vera.census.census_capabilities import _running_loop
    except Exception:
        return 0
    try:
        res = await _running_loop()
    except Exception:
        return 0
    return 1 if isinstance(res, dict) and res else 0


async def _owner() -> Dict[str, Any]:
    r = _redis()
    if r is None:
        return {}
    try:
        raw = await r.get(KEY_ACTIVE_CENSUS)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


async def _set_owner(d: Dict[str, Any]) -> None:
    r = _redis()
    if r is not None:
        try:
            await r.set(KEY_ACTIVE_CENSUS, json.dumps(d))
        except Exception:
            pass


async def box_state() -> Dict[str, Any]:
    owner = await _owner()
    st = await asyncio.to_thread(_census_state_sync, owner)
    st["loops_running"] = await _loops_running()
    st["census_owner_run"] = owner
    return st


# ── launchers ────────────────────────────────────────────────────────────────

def _clear_own_control_sync() -> None:
    """A yield/drop THIS scheduler wrote outlives the run it was for
    (census_all.sh refuses to start under a drop); replace it with a resume."""
    if not _ctl:
        return
    d = Path(_CENSUS_DIR)
    control = _ctl.read_json(str(d / _ctl.CONTROL_NAME))
    if _ctl.control_state(control) != "run" and str(control.get("by") or "") == _BY:
        _ctl.write_json(str(d / _ctl.CONTROL_NAME),
                        _ctl.make_control("resume", reason="scheduler: clearing its own control before a new run", by=_BY))


def _launch_census_sync(template: str, plan_style: str = "") -> Dict[str, Any]:
    d = Path(_CENSUS_DIR)
    _clear_own_control_sync()
    env = dict(os.environ)
    env["CENSUS_TEMPLATES"] = template or "default"
    # A forced planning style: run_census.py applies it to every goal and
    # reserves the run in its own series (census.<template>-style-<s>-runN),
    # which census_all.sh archives under that reservation. Never inherited from
    # this process: a census without one is the baseline series.
    env.pop("CENSUS_PLAN_STYLE", None)
    if plan_style:
        env["CENSUS_PLAN_STYLE"] = plan_style
    try:
        p = subprocess.Popen(["sh", "census_all.sh"], cwd=str(d), env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        return {"ok": False, "error": f"could not start census_all.sh: {e}"}
    time.sleep(4)
    try:
        line = (d / "census_all.state").read_text(encoding="utf-8").strip()
    except Exception:
        line = ""
    if p.poll() is not None and not line.startswith("running"):
        return {"ok": False, "error": f"census_all.sh exited {p.returncode} at once: {line[:120]}"}
    return {"ok": True, "pid": p.pid, "state": line}


async def _start(action: Dict[str, Any], rec: Dict[str, Any]) -> Dict[str, Any]:
    kind, t = action["kind"], dict(action.get("target") or {})
    if kind == "census":
        if action.get("reason") == "resume":
            # Parked on this schedule's own yield: lift it, the harness carries on.
            res = await _call("census.control.set", action="resume",
                              reason="scheduler: window open again", by=_BY)
            own = await _owner()
            own.pop("window_end_applied", None)
            await _set_owner(own)
            return {"ok": bool((res or {}).get("ok")), "resumed": True,
                    "error": (res or {}).get("error", "")}
        res = await asyncio.to_thread(_launch_census_sync, t.get("template", "default"),
                                      str(t.get("plan_style") or ""))
        if res.get("ok"):
            await _set_owner({"schedule_id": rec["id"], "started_at": core.iso(_now()),
                              "pid": res.get("pid"), "template": t.get("template", "default"),
                              "plan_style": str(t.get("plan_style") or "")})
        return res
    if kind == "suite":
        return await _call("evolve.suite.start", tag=t.get("tag", ""), profile=t.get("profile", ""),
                           assess=bool(t.get("assess", False)))
    if kind == "task":
        return await _call("evolve.task.run", id=t.get("id", ""), assess=bool(t.get("assess", False)))
    if kind == "pipeline":
        a = t.get("action", "test")
        if a == "test":
            return await _call("evolve.pipeline.test", id=t.get("id", ""))
        if a == "adopt":
            return await _call("evolve.pipeline.adopt", branch=t.get("branch", ""), to="bleeding-edge",
                               title=rec.get("title", ""), summary="scheduled by Loop Lab")
        if a == "promote":
            return await _call("evolve.pipeline.promote", id=t.get("id", ""), to="bleeding-edge")
        return {"error": f"unknown pipeline action {a!r}"}
    if kind == "board":
        return await _call("board.dispatch", id=t.get("id", ""), executor=t.get("executor", "deterministic"),
                           agent=t.get("agent", "orchestrator"))
    if kind == "cap":
        if core.cap_denied(t.get("name", "")):
            return {"error": f"{t.get('name')} may not run from a schedule"}
        return await _call(t.get("name", ""), **(t.get("arguments") or {}))
    return {"error": f"unknown kind {kind!r}"}


def _result_word(res: Any) -> str:
    if isinstance(res, dict):
        if res.get("error"):
            return "error"
        if res.get("ok") is False:
            return "error"
        return "started"
    return "started"


# ── the tick ─────────────────────────────────────────────────────────────────

async def _finalize_census_if_done(state: Dict[str, Any]) -> None:
    """The census this scheduler started has ended: close its run record."""
    owner = state.get("census_owner_run") or {}
    if not owner or state.get("census_running") or state.get("census_launching"):
        return
    r = _redis()
    result = "done" if str(state.get("set_line", "")).startswith("done") else \
             ("dropped" if str(state.get("set_line", "")).startswith("dropped") else "ended")
    detail = f"{state.get('set_line', '')[:80]} · {state.get('census_run', '')}".strip(" ·")
    await _update_run(owner.get("run_id", ""), finished_at=core.iso(_now()), result=result, detail=detail)
    sid = owner.get("schedule_id", "")
    if sid:
        rec = await _get(sid)
        if rec:
            rec["last_finished_at"] = core.iso(_now())
            rec["last_result"] = result
            await _save(rec)
    if r is not None:
        try:
            await r.delete(KEY_ACTIVE_CENSUS)
        except Exception:
            pass
    await emit_event({"type": "evolve.schedule.finished", "schedule_id": sid, "result": result,
                      "detail": detail})


async def _get(sid: str) -> Optional[Dict[str, Any]]:
    r = _redis()
    if r is None:
        return None
    try:
        raw = await r.hget(KEY_SCHEDULES, sid)
        return json.loads(raw if isinstance(raw, str) else raw.decode()) if raw else None
    except Exception:
        return None


async def run_tick(*, force_ids: Optional[List[str]] = None, now: Optional[datetime] = None
                   ) -> Dict[str, Any]:
    """One pass: close a finished census, apply window-end controls, start
    what is due. `force_ids` starts those schedules now regardless of window
    (run_now), still subject to the box gating."""
    now = now or _now()
    cfg = await _get_config()
    state = await box_state()
    await _finalize_census_if_done(state)
    if not state.get("census_running"):
        state["census_owner"] = ""
    scheds = await _load_all()          # after finalize: last_finished_at is on the records now
    started, controls, skipped = [], [], []
    # Window-end controls for censuses this scheduler owns - applied once per run.
    for rec in scheds:
        act = core.window_end_action(rec, now, state)
        if act:
            res = await _call("census.control.set", action=act["action"], reason=act["reason"], by=_BY)
            ok = bool((res or {}).get("ok"))
            controls.append({"schedule_id": rec["id"], "action": act["action"], "ok": ok})
            if ok:
                own = await _owner()
                own["window_end_applied"] = act["action"]
                await _set_owner(own)
                state["window_end_applied"] = True
    if not cfg.get("enabled", True) and not force_ids:
        return {"ok": True, "enabled": False, "started": [], "controls": controls, "state": state}
    if force_ids:
        plan = []
        for rec in scheds:
            if rec["id"] in force_ids:
                r2 = dict(rec, enabled=True, once_at="", days=list(range(7)), start="00:00", end="23:59",
                          last_started_at="", last_finished_at="")
                due, why = core.is_due(r2, now, state)
                if due:
                    plan.append({"schedule_id": rec["id"], "kind": rec["kind"], "target": rec["target"],
                                 "title": rec.get("title", ""), "reason": "run now"})
                else:
                    skipped.append({"schedule_id": rec["id"], "reason": why})
    else:
        plan = core.plan_tick(scheds, now, state, max_starts=int(cfg.get("max_starts_per_tick") or 3))
    by_id = {s["id"]: s for s in scheds}
    for action in plan:
        rec = by_id.get(action["schedule_id"])
        if not rec:
            continue
        run_id = uuid.uuid4().hex[:10]
        res = await _start(action, rec)
        word = _result_word(res)
        run = {"id": run_id, "schedule_id": rec["id"], "kind": rec["kind"], "title": rec.get("title", ""),
               "target": rec.get("target"), "started_at": core.iso(now), "finished_at": "",
               "result": word, "detail": str((res or {}).get("error") or (res or {}).get("state")
                                              or (res or {}).get("id") or "")[:200],
               "reason": action.get("reason", "")}
        if rec["kind"] != "census" or word == "error":
            run["finished_at"] = core.iso(_now())
        if rec["kind"] == "census" and word != "error":
            own = await _owner()
            if not (res or {}).get("resumed"):
                own["run_id"] = run_id
                await _set_owner(own)
            else:
                run["finished_at"] = core.iso(_now())
                run["result"] = "resumed"
            state["census_running"] = True
            state["census_owner"] = rec["id"]
            state["census_parked_by_us"] = False
        await _append_run(run)
        rec["last_started_at"] = core.iso(now)
        rec["last_result"] = word
        rec["runs"] = int(rec.get("runs") or 0) + 1
        rec["updated"] = core.iso(now)
        await _save(rec)
        started.append({"schedule_id": rec["id"], "kind": rec["kind"], "title": rec.get("title", ""),
                        "result": word, "detail": run["detail"], "run_id": run_id})
        await emit_event({"type": "evolve.schedule.started", "schedule_id": rec["id"], "kind": rec["kind"],
                          "title": rec.get("title", ""), "result": word, "detail": run["detail"]})
    return {"ok": True, "enabled": bool(cfg.get("enabled", True)), "started": started,
            "controls": controls, "skipped": skipped, "considered": len(scheds), "state": state}


_TICK_LOCK = asyncio.Lock()


async def _tick() -> None:
    if _TICK_LOCK.locked():
        return
    async with _TICK_LOCK:
        try:
            await run_tick()
        except Exception as e:
            log.warning("evolve.schedule tick failed: %s", e)


schedule(_tick, _TICK_S, name="evolve.schedule.tick", skip_in_sandbox=True, singleton=True)


# ── capabilities ─────────────────────────────────────────────────────────────

@capability("evolve.schedule.list", memory="off", silent=True,
            http_method="GET", http_path="/evolve/schedule/list", http_tags=["evolve", "schedule"],
            description="Loop Lab schedules: when censuses, suites, tasks, pipeline steps, board items "
                        "or capabilities may run (weekly windows in a timezone, or one-shot). "
                        "Output: {schedules[], next[] (id -> next window), config, state}.")
async def cap_schedule_list(trace_id=None) -> Dict[str, Any]:
    scheds = await _load_all()
    now = _now()
    nxt = {}
    for s in scheds:
        try:
            w = core.next_window(s, now)
            nxt[s["id"]] = {"start": core.iso(w[0]), "end": core.iso(w[1])} if w else None
        except Exception:
            nxt[s["id"]] = None
    return {"schedules": scheds, "next": nxt, "config": await _get_config(),
            "state": await box_state(), "kinds": list(core.KINDS), "repeats": list(core.REPEATS)}


@capability("evolve.schedule.get", memory="off", silent=True,
            http_method="GET", http_path="/evolve/schedule/get", http_tags=["evolve", "schedule"],
            description="One Loop Lab schedule by id. Input: id (str!).")
async def cap_schedule_get(id: str = "", trace_id=None) -> Dict[str, Any]:
    rec = await _get((id or "").strip())
    return {"schedule": rec} if rec else {"error": f"no schedule {id!r}"}


@capability("evolve.schedule.upsert", memory="off",
            http_method="POST", http_path="/evolve/schedule/upsert", http_tags=["evolve", "schedule"],
            description="Create or update a Loop Lab schedule. Input: schedule (object!) with kind "
                        "(census|suite|task|pipeline|board|cap), target (kind-specific: census "
                        "{template}; suite {tag,profile,assess}; task {id}; pipeline {id, action "
                        "test|promote} or {branch, action adopt}; board {id, executor, agent}; cap "
                        "{name, arguments}), days [0..6] (Mon=0), start/end HH:MM, timezone, repeat "
                        "(continuous|once_per_window|every), every_minutes, at_window_end "
                        "(finish|yield|drop, census), exclusive, enabled, title, notes; or once_at "
                        "(ISO) for a one-shot. Pipelines may reach bleeding-edge, never main. "
                        "Output: {ok, schedule, next}.")
async def cap_schedule_upsert(schedule: Optional[Dict[str, Any]] = None, trace_id=None) -> Dict[str, Any]:
    if isinstance(schedule, str):
        try:
            schedule = json.loads(schedule)
        except Exception:
            return {"ok": False, "error": "schedule must be a JSON object"}
    if not isinstance(schedule, dict):
        return {"ok": False, "error": "schedule (object) is required"}
    now = _now()
    prev = await _get(str(schedule.get("id") or "")) if schedule.get("id") else None
    merged = dict(prev or {})
    merged.update(schedule)
    if prev:
        for k in ("created", "last_started_at", "last_finished_at", "last_result", "runs"):
            merged[k] = prev.get(k, merged.get(k))
    try:
        rec = core.normalize(merged, now=now)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if rec["kind"] == "cap":
        if rec["target"]["name"] not in CAPABILITY_REGISTRY:
            return {"ok": False, "error": f"unknown capability: {rec['target']['name']}"}
    await _save(rec)
    await emit_event({"type": "evolve.schedule.saved", "schedule_id": rec["id"], "kind": rec["kind"],
                      "title": rec["title"]})
    w = core.next_window(rec, now)
    return {"ok": True, "schedule": rec,
            "next": {"start": core.iso(w[0]), "end": core.iso(w[1])} if w else None}


@capability("evolve.schedule.delete", memory="off",
            http_method="POST", http_path="/evolve/schedule/delete", http_tags=["evolve", "schedule"],
            description="Delete a Loop Lab schedule. Input: id (str!). A census it started keeps running.")
async def cap_schedule_delete(id: str = "", trace_id=None) -> Dict[str, Any]:
    r = _redis()
    sid = (id or "").strip()
    if r is None or not sid:
        return {"ok": False, "error": "id required"}
    n = await r.hdel(KEY_SCHEDULES, sid)
    await emit_event({"type": "evolve.schedule.deleted", "schedule_id": sid})
    return {"ok": bool(n), "deleted": sid}


@capability("evolve.schedule.enable", memory="off",
            http_method="POST", http_path="/evolve/schedule/enable", http_tags=["evolve", "schedule"],
            description="Switch one Loop Lab schedule on or off. Input: id (str!), enabled (bool).")
async def cap_schedule_enable(id: str = "", enabled: bool = True, trace_id=None) -> Dict[str, Any]:
    rec = await _get((id or "").strip())
    if not rec:
        return {"ok": False, "error": f"no schedule {id!r}"}
    rec["enabled"] = bool(enabled)
    rec["updated"] = core.iso(_now())
    await _save(rec)
    return {"ok": True, "schedule": rec}


@capability("evolve.schedule.run_now", memory="off",
            http_method="POST", http_path="/evolve/schedule/run_now", http_tags=["evolve", "schedule"],
            description="Start one schedule's work now, outside its window, still subject to the box "
                        "(no census beside a census, no census beside a loop). Input: id (str!).")
async def cap_schedule_run_now(id: str = "", trace_id=None) -> Dict[str, Any]:
    sid = (id or "").strip()
    if not sid:
        return {"ok": False, "error": "id required"}
    async with _TICK_LOCK:
        return await run_tick(force_ids=[sid])


@capability("evolve.schedule.tick", memory="off",
            http_method="POST", http_path="/evolve/schedule/tick", http_tags=["evolve", "schedule"],
            description="Run the scheduler's periodic pass by hand: close a finished census, apply "
                        "window-end controls, start what is due. Output: {started[], controls[], state}.")
async def cap_schedule_tick(trace_id=None) -> Dict[str, Any]:
    async with _TICK_LOCK:
        return await run_tick()


@capability("evolve.schedule.events", memory="off", silent=True,
            http_method="GET", http_path="/evolve/schedule/events", http_tags=["evolve", "schedule", "calendar"],
            description="Calendar events for the Loop Lab schedules. mode=windows (default): one "
                        "read-only event per window per day (source loop-lab) plus the runs the "
                        "scheduler started (source loop-lab-run); mode=results: every archived census "
                        "run and suite as a span coloured by its pass rate (source results; "
                        "granularity=runs|goals); mode=both. Input: start, end (ISO; default this "
                        "week ± 3 days), mode, granularity. Output: {events[], count}.")
async def cap_schedule_events(start: str = "", end: str = "", mode: str = "windows",
                              granularity: str = "runs", trace_id=None) -> Dict[str, Any]:
    now = _now()
    s = core.parse_iso(start) or (now - __import__("datetime").timedelta(days=3))
    e = core.parse_iso(end) or (now + __import__("datetime").timedelta(days=10))
    mode = (mode or "windows").strip().lower()
    ev: List[Dict[str, Any]] = []
    if mode in ("windows", "both"):
        scheds = await _load_all()
        ev += core.project_events(scheds, s, e) + core.history_events(await _runs(MAX_RUNS), s, e)
    if mode in ("results", "both"):
        res = await _call("evolve.suites", limit=400)
        suites = (res.get("suites") if isinstance(res, dict) else None) or []
        ev += core.results_events(suites, s, e, granularity=(granularity or "runs").strip().lower())
    ev.sort(key=lambda x: x["start"])
    return {"events": ev, "count": len(ev), "mode": mode}


@capability("evolve.schedule.history", memory="off", silent=True,
            http_method="GET", http_path="/evolve/schedule/history", http_tags=["evolve", "schedule"],
            description="Runs the scheduler started, newest first. Input: limit (int, default 50).")
async def cap_schedule_history(limit: int = 50, trace_id=None) -> Dict[str, Any]:
    runs = await _runs(max(1, min(MAX_RUNS, int(limit or 50))))
    return {"runs": runs, "count": len(runs)}


@capability("evolve.schedule.config.get", memory="off", silent=True,
            http_method="GET", http_path="/evolve/schedule/config", http_tags=["evolve", "schedule"],
            description="Scheduler config: enabled (master switch), timezone (default for new "
                        "schedules), max_starts_per_tick.")
async def cap_schedule_config_get(trace_id=None) -> Dict[str, Any]:
    return {"config": await _get_config(), "tick_s": _TICK_S}


@capability("evolve.schedule.config.set", memory="off",
            http_method="POST", http_path="/evolve/schedule/config/set", http_tags=["evolve", "schedule"],
            description="Set scheduler config fields: enabled (bool), timezone (str), max_starts_per_tick (int).")
async def cap_schedule_config_set(enabled: Optional[bool] = None, timezone: str = "",
                                  max_starts_per_tick: Optional[int] = None, trace_id=None) -> Dict[str, Any]:
    cfg = await _get_config()
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    if timezone:
        try:
            core.resolve_tz(timezone)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        cfg["timezone"] = timezone
    if max_starts_per_tick is not None:
        cfg["max_starts_per_tick"] = max(1, min(10, int(max_starts_per_tick)))
    r = _redis()
    if r is not None:
        await r.set(KEY_CONFIG, json.dumps(cfg))
    return {"ok": True, "config": cfg}


@capability("evolve.schedule.seed_weekday_census", memory="off",
            http_method="POST", http_path="/evolve/schedule/seed_weekday_census", http_tags=["evolve", "schedule"],
            description="Create the standard schedule: censuses of `template` back to back on weekdays "
                        "between start and end (HH:MM, default 05:00-17:00) in timezone (default "
                        "Europe/London). Input: template, start, end, timezone. Idempotent per title.")
async def cap_schedule_seed_weekday_census(template: str = "default", start: str = "05:00", end: str = "17:00",
                                           timezone: str = "", trace_id=None) -> Dict[str, Any]:
    cfg = await _get_config()
    try:
        rec = core.weekday_census(template or "default", start=start or "05:00", end=end or "17:00",
                                  timezone_name=timezone or cfg.get("timezone") or core.DEFAULT_TZ, now=_now())
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    for s in await _load_all():
        if s.get("title") == rec["title"]:
            return {"ok": True, "schedule": s, "existing": True}
    await _save(rec)
    await emit_event({"type": "evolve.schedule.saved", "schedule_id": rec["id"], "kind": "census",
                      "title": rec["title"]})
    return {"ok": True, "schedule": rec, "existing": False}
