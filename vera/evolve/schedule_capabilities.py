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
import re
import sys
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
                  "max_starts_per_tick": core.MAX_STARTS_PER_TICK,
                  # A2 (NIGHT-BOOKINGS, owner 17 Oct 2026): no schedule starts in these windows unless quiet_ok
                  "quiet_windows": list(core.DEFAULT_QUIET)}
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


async def census_due_now() -> List[str]:
    """Titles of enabled census schedules due right now, judged as if the box were free.
    A census=finish release holds for these (release_core.may_release) so the arms of a
    pair are not split across a restart (4 Oct 2026: OFF on 1cb3c8ba, ORDER on 1d9d47de).
    Empty when the scheduler is disabled - nothing would start, so nothing to wait for."""
    try:
        if not (await _get_config()).get("enabled", True):
            return []
        now = _now()
        free = {"census_running": False, "loops_running": 0}
        return [str(r.get("title") or r.get("id")) for r in await _load_all()
                if r.get("kind") == "census" and core.is_due(r, now, free)[0]]
    except Exception:
        return []


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
    """Patch one run record in place (newest 500 only) - under WATCH/MULTI, so an LPUSH in between
    cannot make it overwrite the neighbouring record (DEEP-21 D21-12)."""
    r = _redis()
    if r is None:
        return
    try:
        try:
            from Vera.vera.evolve.redis_list_patch import patch_list_item
        except ImportError:                                   # pragma: no cover
            from vera.evolve.redis_list_patch import patch_list_item
        await patch_list_item(r, KEY_RUNS, lambda d: d.get("id") == run_id,
                              lambda d: dict(d, **fields), scan=MAX_RUNS)
    except Exception:
        pass


async def _call(cap_name: str, /, **kw) -> Any:
    # positional-only: a scheduled capability may itself take an argument called `name`
    # (evolve.script.run does) - 18 Oct 2026, the first live run failed with "multiple values for 'name'"
    cap = CAPABILITY_REGISTRY.get(cap_name)
    if not cap or not cap.get("func"):
        return {"error": f"capability not available: {cap_name}"}
    try:
        return await cap["func"](**kw)
    except Exception as e:
        return {"error": f"{cap_name}: {e}"}


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


#: A2: declared activity kinds that hold the box (a census declares itself and is read from its own files)
_DECLARED_HOLD = ("probe", "test", "benchmark", "experiment", "other")


async def _declared_busy() -> str:
    """A2: a sys.activity declaration (a probe batch, a manual benchmark) - one line, or ""."""
    r = _redis()
    if r is None:
        return ""
    out = []
    try:
        async for k in r.scan_iter("vera:activity:declared:*"):
            raw = await r.get(k)
            if not raw:
                continue
            d = json.loads(raw if isinstance(raw, str) else raw.decode())
            if str(d.get("kind") or "other") in _DECLARED_HOLD:
                out.append("%s %s" % (d.get("kind") or "activity", d.get("label") or d.get("id") or ""))
    except Exception:
        return ""
    return ", ".join(out[:3])


async def box_state() -> Dict[str, Any]:
    owner = await _owner()
    st = await asyncio.to_thread(_census_state_sync, owner)
    st["loops_running"] = await _loops_running()
    st["census_owner_run"] = owner
    st["declared_busy"] = await _declared_busy()
    # a release waiting to restart prod holds this key (release_core.DRAIN_KEY): protected entries do not start
    try:
        r = _redis()
        st["release_draining"] = bool(r is not None and await r.get("vera:evolve:release:drain"))
    except Exception:
        st["release_draining"] = False
    return st


async def protected_running() -> List[Dict[str, Any]]:
    """Open runs of protected entries (release_capabilities.census_summary -> release_core.may_release)."""
    try:
        runs = await _runs(limit=MAX_RUNS)
        scheds = {s.get("id"): s for s in await _load_all()}
        return core.protected_runs(runs, scheds, _now())
    except Exception as e:
        log.debug("protected runs: %s", e)
        return []


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


def _launch_census_sync(template: str, plan_style: str = "", intent_core: str = "",
                        minds: Optional[Dict[str, str]] = None, minds_label: str = "",
                        schedule_id: str = "") -> Dict[str, Any]:
    d = Path(_CENSUS_DIR)
    _clear_own_control_sync()
    env = dict(os.environ)
    env["CENSUS_TEMPLATES"] = template or "default"
    # who drove it (DEEP-21 D21-14): the scheduler, not a hard-coded Claude session
    env["CENSUS_OPERATOR"] = "vera-scheduler"
    env["VERA_CENSUS_TOOL"] = "sched"
    env["CENSUS_SESSION_ID"] = "sched:%s" % (schedule_id or "unknown")
    # A forced planning style: run_census.py applies it to every goal and
    # reserves the run in its own series (census.<template>-style-<s>-runN),
    # which census_all.sh archives under that reservation. Never inherited from
    # this process: a census without one is the baseline series.
    env.pop("CENSUS_PLAN_STYLE", None)
    if plan_style:
        env["CENSUS_PLAN_STYLE"] = plan_style
    # The intent core likewise (roadmap G): never inherited, its own series.
    env.pop("CENSUS_INTENT_CORE", None)
    if intent_core and intent_core != "off":
        env["CENSUS_INTENT_CORE"] = intent_core
    # The switchboard set (CENSUS_MINDS contract v1, four-minds ROADMAP §4.1): compact
    # JSON plus its label; never inherited. The harness validates it against the build
    # under test before goal 1 and archives the run as <template>[-style-][-core-]-<label>.
    env.pop("CENSUS_MINDS", None)
    env.pop("CENSUS_MINDS_LABEL", None)
    if minds:
        env["CENSUS_MINDS"] = json.dumps(minds, separators=(",", ":"), sort_keys=True)
        env["CENSUS_MINDS_LABEL"] = minds_label
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


# ── background kinds and board notes ─────────────────────────────────────────
# A loop on a goal or a branch's unit tests takes minutes to an hour: the tick starts it and moves on, and the run
# record is closed (result + detail) when it ends, which also releases the schedule's next start.
_BG_TASKS: set = set()
#: RF-09: run id -> schedule id of the EXCLUSIVE background runs (cap schedules) still going. A census
#: does not start while one runs; the tick passes the count to schedule_core as `exclusive_running`.
_EXCLUSIVE_BG: Dict[str, str] = {}


def _bg_word(res: Any) -> str:
    if not isinstance(res, dict):
        return "done"
    if res.get("error"):
        return "error"
    if res.get("skipped"):
        return "skipped"                  # A1: a guard said not now - not a failure
    if "passed" in res or "summary" in res and "ok" in res:
        return "green" if res.get("ok", res.get("passed")) else "red"
    if res.get("ok") is False:
        return "failed"
    return "done"


def _bg_detail(res: Any) -> str:
    if not isinstance(res, dict):
        return str(res or "")[:200]
    v = (res.get("error") or res.get("why") or res.get("summary") or res.get("final")
         or res.get("deliverable") or res.get("state") or "")
    if isinstance(v, dict):
        v = " · ".join(f"{k} {v[k]}" for k in list(v)[:6])
    return str(v)[:200]


async def _board_note(rec: Dict[str, Any], body: str) -> None:
    """A line on the board item this work is for: when it started, how it ended. Best effort."""
    bid = str((rec or {}).get("board_id") or "")
    if not bid:
        return
    try:
        await _call("board.comment", id=bid, frm="loop-lab-scheduler", kind="progress",
                    body=f"⏰ {rec.get('title') or rec.get('id')}: {body}"[:600])
    except Exception:
        pass


#: A3: run id -> (task, schedule id) of every background run, so a window end can stop it.
_RUN_TASKS: Dict[str, Any] = {}


async def _follow(cap: str, res: Any, run_id: str) -> Optional[Dict[str, Any]]:
    """A1: `cap` started a background job - wait for THAT to end (its own status capability), so the run
    record, the 'previous run still going' hold and the exclusive slot match the real work. None when
    there is nothing to follow."""
    spec = core.follow_args(cap, res)
    if spec is None:
        return None
    status_cap, args = spec
    await _update_run(run_id, detail=f"following {status_cap}"[:200])
    deadline = time.monotonic() + core.STILL_RUNNING_HOURS * 3600
    while time.monotonic() < deadline:
        await asyncio.sleep(core.FOLLOW_POLL_S)
        ended, word, detail = core.follow_state(cap, await _call(status_cap, **args))
        if ended:
            return {"word": word, "detail": detail}
    return {"word": "error", "detail": f"{cap}: still running after {core.STILL_RUNNING_HOURS} h - no longer followed"}


def _in_background(run_id: str, rec: Dict[str, Any], coro, *, exclusive: bool = False,
                   follow_cap: str = "") -> None:
    if exclusive:
        _EXCLUSIVE_BG[run_id] = rec.get("id", "")

    async def _go():
        try:
            res = await coro
            word, detail = _bg_word(res), _bg_detail(res)
            fol = await _follow(follow_cap, res, run_id) if follow_cap else None
            if fol:
                word, detail = fol["word"], fol["detail"] or detail
        except asyncio.CancelledError:
            word, detail = "stopped", "window ended - stopped by the schedule (at_window_end)"
        except Exception as e:                         # pragma: no cover - the call itself guards
            word, detail = "error", str(e)[:200]
        fin = core.iso(_now())
        await _update_run(run_id, finished_at=fin, result=word, detail=detail)
        cur = await _get(rec["id"]) or rec
        cur["last_finished_at"], cur["last_result"] = fin, word
        if word not in ("interrupted",):
            cur["recoveries"] = 0                  # it got through: the next restart may recover it again
        await _save(cur)
        await _board_note(cur, f"{word}" + (f" · {detail}" if detail else ""))
        await emit_event({"type": "evolve.schedule.finished", "schedule_id": rec["id"], "result": word,
                          "detail": detail})
    t = asyncio.ensure_future(_go())
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    _RUN_TASKS[run_id] = (t, rec.get("id", ""))
    t.add_done_callback(lambda _t: _RUN_TASKS.pop(run_id, None))
    if exclusive:
        t.add_done_callback(lambda _t: _EXCLUSIVE_BG.pop(run_id, None))


def _bg(run_id: str, rec: Dict[str, Any], coro, label: str, follow_cap: str = "") -> Dict[str, Any]:
    """Start `coro` as a background run (DEEP-21 D21-1): the tick never waits on the work it starts - a
    pipeline gate or a board dispatch ran inline under _TICK_LOCK for minutes and stopped window-end
    yields. Exclusive schedules keep censuses off the box while they run (RF-09)."""
    _in_background(run_id, rec, coro, exclusive=bool(rec.get("exclusive", True)), follow_cap=follow_cap)
    return {"ok": True, "state": f"{label} running", "background": True}


async def _start(action: Dict[str, Any], rec: Dict[str, Any], run_id: str = "") -> Dict[str, Any]:
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
                                      str(t.get("plan_style") or ""),
                                      str(t.get("intent_core") or ""),
                                      dict(t.get("minds") or {}),
                                      str(t.get("minds_label") or ""),
                                      str(rec.get("id") or ""))
        if res.get("ok"):
            await _set_owner({"schedule_id": rec["id"], "started_at": core.iso(_now()),
                              "pid": res.get("pid"), "template": t.get("template", "default"),
                              "plan_style": str(t.get("plan_style") or ""),
                              "intent_core": str(t.get("intent_core") or ""),
                              "minds_label": str(t.get("minds_label") or "")})
        return res
    if kind == "suite":
        return _bg(run_id, rec, _call("evolve.suite.start", tag=t.get("tag", ""), profile=t.get("profile", ""),
                                      assess=bool(t.get("assess", False))), "suite %s" % t.get("tag", ""),
                   follow_cap="evolve.suite.start")
    if kind == "task":
        return _bg(run_id, rec, _call("evolve.task.run", id=t.get("id", ""), assess=bool(t.get("assess", False))),
                   "task %s" % t.get("id", ""))
    if kind == "pipeline":
        a = t.get("action", "test")
        if a == "test":
            return _bg(run_id, rec, _call("evolve.pipeline.test", id=t.get("id", "")),
                       "pipeline test %s" % t.get("id", ""))
        if a == "adopt":
            return _bg(run_id, rec, _call("evolve.pipeline.adopt", branch=t.get("branch", ""), to="bleeding-edge",
                                          title=rec.get("title", ""), summary="scheduled by Loop Lab"),
                       "pipeline adopt %s" % t.get("branch", ""))
        if a == "promote":
            return _bg(run_id, rec, _call("evolve.pipeline.promote", id=t.get("id", ""), to="bleeding-edge"),
                       "pipeline promote %s" % t.get("id", ""))
        return {"error": f"unknown pipeline action {a!r}"}
    if kind == "board":
        return _bg(run_id, rec, _call("board.dispatch", id=t.get("id", ""),
                                      executor=t.get("executor", "deterministic"),
                                      agent=t.get("agent", "orchestrator")), "board %s" % t.get("id", ""))
    if kind == "loop":
        _in_background(run_id, rec, _call("loops.run", profile=t.get("profile") or "coding", goal=t.get("goal", ""),
                                          model=t.get("model", ""), session_id=f"sched:{rec['id']}:{run_id}"),
                       exclusive=bool(rec.get("exclusive", True)))           # A3
        return {"ok": True, "state": f"loop running · session sched:{rec['id']}:{run_id}", "background": True}
    if kind == "tests":
        _in_background(run_id, rec, _call("evolve.unittest.run", branch=t.get("branch", ""),
                                          paths=t.get("paths") or "tests", markers=t.get("markers", ""),
                                          repo=t.get("repo", "")),
                       exclusive=bool(rec.get("exclusive", True)))           # A3
        return {"ok": True, "state": f"tests running on {t.get('branch', '')}", "background": True}
    if kind == "claude_session":
        return _bg(run_id, rec, _session_request(rec, t), "request for %s" % t.get("session", ""),
                   follow_cap="agents.work.request")
    if kind == "cap":
        if core.cap_denied(t.get("name", "")):
            return {"error": f"{t.get('name')} may not run from a schedule"}
        # RF-09: in the background, not inline in the tick - a long cap held _TICK_LOCK and
        # stopped all scheduling. An exclusive one keeps censuses off the box while it runs.
        _in_background(run_id, rec, _call(t.get("name", ""), **(t.get("arguments") or {})),
                       exclusive=bool(rec.get("exclusive", True)), follow_cap=t.get("name", ""))
        return {"ok": True, "state": f"cap {t.get('name', '')} running", "background": True}
    return {"error": f"unknown kind {kind!r}"}


#: B5: a session with this many unanswered requests gets no more from a schedule until it catches up
MAX_UNANSWERED = 3


async def _session_request(rec: Dict[str, Any], t: Dict[str, Any]) -> Dict[str, Any]:
    """B3: write the request a claude_session schedule is for. The session picks it up at its own check
    (agents.work.pending) - Vera never starts or resumes a Claude process (owner D2, 17 Oct 2026)."""
    lst = await _call("agents.work.list", session=t.get("session", ""), kind="request")
    waiting = [e for e in ((lst or {}).get("entries") or [])
               if e.get("view_status") == "pending" and e.get("session") == t.get("session")]
    if len(waiting) >= MAX_UNANSWERED:
        return {"ok": True, "skipped": True,
                "why": "%s has %d unanswered requests - not adding another" % (t.get("session"), len(waiting))}
    w = core.current_window(rec, _now())
    res = await _call("agents.work.request", session=t.get("session", ""),
                      title=t.get("title") or rec.get("title") or "scheduled work",
                      what=t.get("what", ""), board_items=t.get("board_items") or [],
                      window_end=core.iso(w[1]) if w else "", owner="loop-lab-schedule:%s" % rec.get("id"))
    if not (res or {}).get("ok"):
        return {"error": (res or {}).get("error") or "agents.work.request failed"}
    return {"ok": True, "id": res["entry"]["id"], "state": "requested"}


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
    state["exclusive_running"] = len(_EXCLUSIVE_BG)        # RF-09: exclusive cap runs in flight
    state["quiet"] = core.in_quiet(now, cfg.get("quiet_windows"), cfg.get("timezone") or core.DEFAULT_TZ)
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
    # A3: a non-census run past its window, on a schedule that asked to stop it there
    by_id0 = {s["id"]: s for s in scheds}
    for rid, (task, sid) in list(_RUN_TASKS.items()):
        rec0 = by_id0.get(sid)
        if rec0 and not task.done() and core.window_end_stop(rec0, now):
            task.cancel()
            controls.append({"schedule_id": sid, "action": "stop", "run_id": rid, "ok": True})
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
        if action.get("reason") == core.DISPLACE:
            # Drop the census parked on another schedule's window-end yield. Not a
            # start: the harness archives the parked run (-partial-dropped) and
            # exits within seconds, and THIS schedule launches on the next tick
            # (its once-per-window record is untouched, so it is still due).
            res = await _call("census.control.set", action="drop", by=_BY,
                              reason="scheduler: %s is due - dropping the census parked on "
                                     "another schedule's window-end yield" % rec.get("title", rec["id"]))
            controls.append({"schedule_id": rec["id"], "action": "drop-parked",
                             "ok": bool((res or {}).get("ok"))})
            break                        # one census at a time: launch next tick
        run_id = uuid.uuid4().hex[:10]
        res = await _start(action, rec, run_id)
        word = _result_word(res)
        run = {"id": run_id, "schedule_id": rec["id"], "kind": rec["kind"], "title": rec.get("title", ""),
               "target": rec.get("target"), "started_at": core.iso(now), "finished_at": "",
               "result": word, "detail": str((res or {}).get("error") or (res or {}).get("state")
                                              or (res or {}).get("id") or "")[:200],
               "reason": action.get("reason", ""), "board_id": rec.get("board_id", "")}
        if (rec["kind"] != "census" and not (res or {}).get("background")) or word == "error":
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
        rec["recover_due"] = ""                    # a recovery, if this was one, is under way
        rec["runs"] = int(rec.get("runs") or 0) + 1
        rec["updated"] = core.iso(now)
        await _save(rec)
        await _board_note(rec, f"started ({action.get('reason', '')})" + (f" · {run['detail']}" if run["detail"] else ""))
        started.append({"schedule_id": rec["id"], "kind": rec["kind"], "title": rec.get("title", ""),
                        "result": word, "detail": run["detail"], "run_id": run_id})
        await emit_event({"type": "evolve.schedule.started", "schedule_id": rec["id"], "kind": rec["kind"],
                          "title": rec.get("title", ""), "result": word, "detail": run["detail"]})
    return {"ok": True, "enabled": bool(cfg.get("enabled", True)), "started": started,
            "controls": controls, "skipped": skipped, "considered": len(scheds), "state": state}


#: when this process started - a background run begun before it cannot still be running here
_PROCESS_STARTED = _now()


async def _reconcile_runs() -> Dict[str, Any]:
    """Once at start (DEEP-21 D21-5): close the background runs a restart killed as `interrupted` and
    release their schedules (last_finished_at), instead of 'previous run still going' for 6 h."""
    out: Dict[str, Any] = {"closed": []}
    try:
        started = _PROCESS_STARTED
        runs = await _runs(limit=MAX_RUNS)
        for run in core.interrupted_runs(runs, started):
            fin = core.iso(_now())
            await _update_run(run["id"], finished_at=fin, result="interrupted",
                              detail="prod restarted while it ran")
            rec = await _get(run.get("schedule_id", ""))
            if rec and (rec.get("last_finished_at") or "") < (run.get("started_at") or ""):
                rec["last_finished_at"], rec["last_result"] = fin, "interrupted"
                # recover (owner, 18 Oct): an entry that recovers runs again once the box allows
                if core.recover_after_interrupt(rec, fin):
                    out.setdefault("recovering", []).append(rec.get("id"))
                await _save(rec)
            out["closed"].append(run["id"])
        if out["closed"]:
            log.info("schedule: closed %d run(s) a restart interrupted: %s",
                     len(out["closed"]), ", ".join(out["closed"]))
    except Exception as e:
        log.debug("schedule reconcile: %s", e)
    return out


async def _reconcile_startup() -> None:
    await asyncio.sleep(20)
    await _reconcile_runs()


_orch.start_at_import(_reconcile_startup, "schedule_reconcile")

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
async def cap_schedule_list(board_id: str = "", trace_id=None) -> Dict[str, Any]:
    scheds = await _load_all()
    if (board_id or "").strip():
        scheds = core.for_board(scheds, board_id)
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
                        "week ± 3 days), mode, granularity, include_calendar (bool - Vera's own "
                        "calendar events beside them, source local/ics/caldav/google), include_agents (bool, "
                        "default true - Claude sessions' triggers and requests, layer agents). Every event carries "
                        "`layer` (windows|runs|results|board|calendar) so a viewer can switch each on and off. "
                        "Output: {events[], count, layers{layer: n}}.")
async def cap_schedule_events(start: str = "", end: str = "", mode: str = "windows",
                              granularity: str = "runs", include_calendar: bool = False,
                              include_agents: bool = True, trace_id=None) -> Dict[str, Any]:
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
    if include_agents in (True, 1, "1", "true", "yes"):
        ev += await _agent_events(s, e)                    # B1: the agents layer
    if include_calendar in (True, 1, "1", "true", "yes"):
        # Vera's calendar (the one the Comms panel shows), without its own Loop Lab overlay - that is this list
        cal = await _call("cal.events.list", start=core.iso(s), end=core.iso(e))
        for x in ((cal.get("events") if isinstance(cal, dict) else None) or []):
            if isinstance(x, dict) and x.get("source") not in ("loop-lab", "loop-lab-run"):
                ev.append(dict(x, read_only=True))
    layers: Dict[str, int] = {}
    for x in ev:
        x["layer"] = core.layer_of(x)
        layers[x["layer"]] = layers.get(x["layer"], 0) + 1
    ev.sort(key=lambda x: str(x.get("start") or ""))
    return {"events": ev, "count": len(ev), "mode": mode, "layers": layers}


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
            description="Set scheduler config fields: enabled (bool), timezone (str), max_starts_per_tick (int), "
                        "quiet_windows (list of {start, end} HH:MM - no schedule starts inside one unless it "
                        "says quiet_ok; [] turns quiet hours off).")
async def cap_schedule_config_set(enabled: Optional[bool] = None, timezone: str = "",
                                  max_starts_per_tick: Optional[int] = None,
                                  quiet_windows: Optional[List[Dict[str, Any]]] = None,
                                  trace_id=None) -> Dict[str, Any]:
    cfg = await _get_config()
    if quiet_windows is not None:
        try:
            cfg["quiet_windows"] = [{"start": core.parse_hhmm(q["start"]).strftime("%H:%M"),
                                     "end": core.parse_hhmm(q["end"]).strftime("%H:%M")}
                                    for q in (quiet_windows or [])]
        except Exception as e:
            return {"ok": False, "error": f"quiet_windows: {e}"}
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



# ── A5: side timers stand down when a schedule covers them ───────────────────
async def covered(name: str, match: Optional[Dict[str, Any]] = None) -> bool:
    """Is there an ENABLED `cap` schedule for `name` (and, if given, these argument values)? A side timer
    (the student nightly pass, the dream nightly QA) checks this and stands down: the schedule owns it."""
    r = _redis()
    if r is None:
        return False
    try:
        for raw in (await r.hgetall(KEY_SCHEDULES)).values():
            rec = json.loads(raw if isinstance(raw, str) else raw.decode())
            t = rec.get("target") or {}
            if (rec.get("enabled", True) and rec.get("kind") == "cap" and t.get("name") == name
                    and all((t.get("arguments") or {}).get(k) == v for k, v in (match or {}).items())):
                return True
    except Exception:
        return False
    return False


@capability("evolve.schedule.covers", memory="off", silent=True,
            http_method="GET", http_path="/evolve/schedule/covers", http_tags=["evolve", "schedule"],
            description="Whether an enabled cap schedule runs this capability (looplab-control A5: a side timer "
                        "stands down when one does). Inputs: name (str!), match (object - argument values that "
                        "must match). Output: {covered}.")
async def cap_schedule_covers(name: str = "", match: Optional[Dict[str, Any]] = None, trace_id=None):
    return {"covered": await covered(name, match or {})}


# ── A4: allow-listed scripts as schedulable jobs ─────────────────────────────
#: Scripts a schedule may start (by name; the path is fixed here, never from the wire). Each runs detached
#: from the repo root with the prod interpreter; its output goes to a log under the census dir.
SCRIPTS = {
    "probe": "tools/loop-census/probes/probe_layout.py",
    "tts_roundtrip": "tools/tts-roundtrip/roundtrip.py",
}
_SCRIPT_ARG = re.compile(r"^[A-Za-z0-9_.:/=@,+-]{1,200}$")
KEY_SCRIPT_JOBS = "vera:evolve:script_jobs"
_SCRIPT_PROCS: Dict[str, Any] = {}
_REPO = Path(__file__).resolve().parents[2]


@capability("evolve.script.run", memory="off",
            http_method="POST", http_path="/evolve/script/run", http_tags=["evolve", "schedule"],
            description="Start an allow-listed script as a background job (looplab-control A4): name "
                        "(probe | tts_roundtrip), args (list of plain tokens - letters, digits, _.:/=@,+-). "
                        "Output: {ok, job_id, pid, log}. Follow it with evolve.script.status.")
async def cap_script_run(name: str = "", args: Optional[List[str]] = None, trace_id=None):
    rel = SCRIPTS.get((name or "").strip())
    if not rel:
        return {"ok": False, "error": f"unknown script {name!r} - one of {', '.join(SCRIPTS)}"}
    argv = [str(a) for a in (args or [])]
    bad = [a for a in argv if not _SCRIPT_ARG.match(a)]
    if bad:
        return {"ok": False, "error": f"argument not allowed: {bad[0][:60]!r}"}
    path = _REPO / rel
    if not path.is_file():
        return {"ok": False, "error": f"script missing: {path}"}
    job_id = uuid.uuid4().hex[:10]
    logdir = Path(_CENSUS_DIR) / "script-jobs"
    logdir.mkdir(parents=True, exist_ok=True)
    logp = logdir / f"{name}-{job_id}.log"
    try:
        fh = open(logp, "ab")
        p = subprocess.Popen([sys.executable, str(path), *argv], cwd=str(_REPO), stdout=fh,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        return {"ok": False, "error": f"could not start {name}: {e}"}
    _SCRIPT_PROCS[job_id] = p
    rec = {"job_id": job_id, "name": name, "args": argv, "pid": p.pid, "log": str(logp),
           "started_at": core.iso(_now()), "state": "running"}
    r = _redis()
    if r is not None:
        await r.hset(KEY_SCRIPT_JOBS, job_id, json.dumps(rec))
    return {"ok": True, **rec}


@capability("evolve.script.status", memory="off", silent=True,
            http_method="GET", http_path="/evolve/script/status", http_tags=["evolve", "schedule"],
            description="A script job's state (evolve.script.run): job_id (str!). Output: {job_id, name, state "
                        "(running|done|error|exited), rc, pid, log, started_at}.")
async def cap_script_status(job_id: str = "", trace_id=None):
    r = _redis()
    raw = await r.hget(KEY_SCRIPT_JOBS, job_id) if r is not None else None
    if not raw:
        return {"error": f"unknown script job {job_id!r}"}
    rec = json.loads(raw if isinstance(raw, str) else raw.decode())
    p = _SCRIPT_PROCS.get(job_id)
    if p is not None:
        rc = p.poll()
        if rc is None:
            return {**rec, "state": "running"}
        rec.update(state="done" if rc == 0 else "error", rc=rc)
    elif rec.get("state") == "running" and not _pid_alive(rec.get("pid") or 0):
        rec["state"] = "exited"            # started by an earlier process: its exit code is lost
    if rec.get("state") != "running" and r is not None:
        await r.hset(KEY_SCRIPT_JOBS, job_id, json.dumps(rec))
    return rec



# ── B1: the central cron view (looplab-control, owner 17 Oct 2026) ──────────
async def _host_rows() -> List[Dict[str, Any]]:
    """The host's own timers: the prod user's crontab and systemd timers (read-only)."""
    def _read():
        out = []
        try:
            p = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
            if p.returncode == 0:
                out += core.parse_crontab(p.stdout)
        except Exception:
            pass
        try:
            p = subprocess.run(["systemctl", "list-timers", "--all", "--no-legend", "--no-pager"],
                               capture_output=True, text=True, timeout=10)
            if p.returncode == 0:
                out += core.parse_systemd_timers(p.stdout)
        except Exception:
            pass
        return out
    return await asyncio.to_thread(_read)


def _in_process_rows() -> List[Dict[str, Any]]:
    """Vera's own periodic jobs (schedule()): name and interval, read-only."""
    rows = []
    for t in list(getattr(_orch, "SCHEDULED_TASKS", []) or []):
        try:
            rows.append(core.central_row("in-process", "interval", str(t.get("name")),
                                         repeat="every %ds" % int(t.get("int") or 0), status="armed",
                                         owner="vera", last=str(t.get("last") or "")))
        except Exception:
            continue
    return rows


@capability("evolve.schedule.all", memory="off", silent=True,
            http_method="GET", http_path="/evolve/schedule/all", http_tags=["evolve", "schedule"],
            description="The central cron view (looplab-control B1): EVERYTHING scheduled on the box in one shape - "
                        "Loop Lab schedules (next window), Claude sessions' registered triggers and the owner's "
                        "requests for them (agents.work), Vera's in-process periodic jobs, and the host crontab and "
                        "systemd timers. Input: sources (comma list: loop-lab,agents-work,in-process,host; default "
                        "all), session (filter agents-work by session). Output: {rows[{source, kind, title, owner, "
                        "next_fire, repeat, status, last, target, ref}], counts}.")
async def cap_schedule_all(sources: str = "", session: str = "", trace_id=None) -> Dict[str, Any]:
    want = {s.strip() for s in (sources or "loop-lab,agents-work,in-process,host").split(",") if s.strip()}
    rows: List[Dict[str, Any]] = []
    now = _now()
    if "loop-lab" in want:
        for s in await _load_all():
            nw = core.next_window(s, now) if s.get("enabled", True) else None
            rows.append(core.central_row(
                "loop-lab", s.get("kind", ""), s.get("title", ""), owner="loop-lab",
                next_fire=core.iso(nw[0]) if nw else (s.get("once_at") or ""),
                repeat=s.get("repeat", "") + (" every %dm" % s.get("every_minutes") if s.get("repeat") == "every" else ""),
                status="enabled" if s.get("enabled", True) else "disabled", last=s.get("last_result", ""),
                target=s.get("target"), ref=s.get("id", "")))
    if "agents-work" in want:
        lst = await _call("agents.work.list", session=session or "", include_done=False)
        for e in (lst or {}).get("entries") or []:
            rows.append(core.central_row(
                "agents-work", e.get("kind", ""), e.get("title", ""), owner=e.get("session", ""),
                next_fire=e.get("when", ""), repeat=e.get("repeat", ""), status=e.get("view_status", ""),
                last=e.get("last_fired_at", ""), target={"what": (e.get("what") or "")[:200]}, ref=e.get("id", "")))
    if "in-process" in want:
        rows += _in_process_rows()
    if "host" in want:
        rows += await _host_rows()
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["source"]] = counts.get(r["source"], 0) + 1
    rows.sort(key=lambda r: (r.get("next_fire") or "9999", r["source"], r["title"]))
    return {"rows": rows, "counts": counts, "count": len(rows)}


async def _agent_events(s: datetime, e: datetime) -> List[Dict[str, Any]]:
    """B1: Claude sessions' triggers and requests as calendar events (layer agents)."""
    lst = await _call("agents.work.list", include_done=True)
    out = []
    for x in (lst or {}).get("entries") or []:
        st = core.parse_iso(x.get("when") or "")
        if st is None or not (s <= st <= e):
            continue
        en = core.parse_iso(x.get("window_end") or "") or (st + __import__("datetime").timedelta(minutes=30))
        out.append({"id": "agents:%s" % x.get("id"), "title": "%s · %s" % (x.get("session") or "?", x.get("title")),
                    "start": core.iso(st), "end": core.iso(en), "source": "agents-work", "read_only": True,
                    "kind": x.get("kind"), "status": x.get("view_status"), "color": "#e0a458"})
    return out



# ── scheduled background work (owner, 18 Oct 2026) ──────────────────────────
# The idle queue starts a job only after 600 s with nothing running - on a box that runs censuses, probes,
# gates and loops all day and night that quiet never comes (intel's batch waited six days, 12-17 Oct). The
# owner: "this stuff should just be scheduled instead of waiting for a quiet period that never comes". A
# window here RESERVES the queue for every kind that has work waiting (the reservation sets the quiet gate
# aside for those kinds, as intel.night.run does for intel alone) and drains them one after another until the
# time is up. As the target of an exclusive Loop Lab schedule it keeps censuses off meanwhile.
_BG_WINDOW_POLL_S = 15


def _idle_svc():
    for name in ("Vera.vera.idle_queue_service", "vera.idle_queue_service"):
        mod = sys.modules.get(name)
        if mod is not None:
            return mod
    try:
        import importlib
        return importlib.import_module("Vera.vera.idle_queue_service")
    except Exception:
        return None


@capability("evolve.background.window", memory="off",
            http_method="POST", http_path="/evolve/background/window", http_tags=["evolve", "schedule", "background"],
            description="Run the background queue NOW for up to `minutes` (default 90, max 600): reserve it for every "
                        "kind with work waiting (or only `kinds`, comma list) and drain those jobs one after another, "
                        "without waiting for the box to go quiet. Meant as the target of an exclusive Loop Lab schedule "
                        "entry - a scheduled window for background work. Output: {ok, ran, remaining, kinds, minutes, reason}.")
async def cap_background_window(minutes: int = 90, kinds: str = "", trace_id=None) -> Dict[str, Any]:
    svc = _idle_svc()
    if svc is None or not hasattr(svc, "reserve"):
        return {"ok": False, "error": "the idle queue service is not loaded"}
    only = {k.strip() for k in (kinds or "").split(",") if k.strip()}
    budget = max(5, min(int(minutes or 90), 600)) * 60
    t0 = time.time()
    ran, reason, seen = 0, "queue empty", set()

    async def _waiting():
        return [j for j in await svc.load_jobs()
                if j.get("state") in ("waiting", "running") and (not only or j.get("kind") in only)]

    try:
        while True:
            jobs = await _waiting()
            if not jobs:
                break
            if time.time() - t0 >= budget:
                reason = "time is up"
                break
            ks = sorted({str(j.get("kind")) for j in jobs})
            seen.update(ks)
            svc.reserve(ks, t0 + budget, by="evolve.background.window")     # refreshed: new kinds join
            async def _never_busy():
                return ""
            r = await svc.drain_once("", _never_busy, time.time())
            if r.get("action") == "started":
                ran += 1
            await asyncio.sleep(_BG_WINDOW_POLL_S)
    finally:
        svc.release_reservation("evolve.background.window")
    left = len([j for j in await _waiting() if j.get("state") == "waiting"])
    await emit_event({"type": "evolve.background.window", "ran": ran, "remaining": left, "reason": reason})
    return {"ok": True, "ran": ran, "remaining": left, "kinds": sorted(seen),
            "minutes": round((time.time() - t0) / 60, 1), "reason": reason,
            "summary": "ran %d background job(s), %d left (%s)" % (ran, left, reason)}



@capability("evolve.schedule.stop_run", memory="off",
            http_method="POST", http_path="/evolve/schedule/stop_run", http_tags=["evolve", "schedule"],
            description="Stop (yield) one scheduled run that is still going - any kind but census (a census yields with "
                        "census.control.set). A background window stops starting jobs and gives the queue back. "
                        "Input: run_id (str!). Output: {ok, run_id}.")
async def cap_schedule_stop_run(run_id: str = "", trace_id=None) -> Dict[str, Any]:
    ent = _RUN_TASKS.get((run_id or "").strip())
    if not ent:
        return {"ok": False, "error": "run %r is not running in this process" % run_id}
    task, sid = ent
    if task.done():
        return {"ok": False, "error": "run %r has already ended" % run_id}
    task.cancel()
    await emit_event({"type": "evolve.schedule.stopped", "schedule_id": sid, "run_id": run_id})
    return {"ok": True, "run_id": run_id, "schedule_id": sid}
