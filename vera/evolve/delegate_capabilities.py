"""evolve.delegate.* - hand a task to a Vera agentic loop (user, 2026-09-28).

Claude (or any MCP caller) briefs Vera the way it briefs one of its own
agents; a v7 loop does the work in its own detached worktree and returns a
report. Report mode only for now (see delegate_core for the safety model).

    evolve.delegate.start   -> {job_id, session_id, worktree}   (returns at once)
    evolve.delegate.status  -> job state + the loop's live progress
    evolve.delegate.result  -> the report once done
    evolve.delegate.cancel  -> stop it (task cancelled + the loop's cancel flag)
    evolve.delegate.list    -> recent jobs
    evolve.delegate.trajectory / .trajectories -> the kept run records (J7)
    evolve.delegate.rate    -> the delegator's verdict on a report; rated jobs are remembered
    evolve.delegate.fs.*    -> the loop's JAILED read tools (worktree only)
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from Vera.vera.capability_orchestration import (
    CAPABILITY_REGISTRY, capability, emit_event, now_iso,
)
import Vera.vera.capability_orchestration as _orch

try:
    from Vera.vera.evolve import delegate_core as D
except ImportError:                                   # pragma: no cover
    from vera.evolve import delegate_core as D
try:
    from Vera.vera.evolve import delegate_trajectory_core as T
except ImportError:                                   # pragma: no cover
    from vera.evolve import delegate_trajectory_core as T

log = logging.getLogger("vera.evolve.delegate")

KEY_JOB = "vera:delegate:job:%s"
KEY_INDEX = "vera:delegate:jobs"
JOB_TTL_S = 14 * 86400
# J7: a finished job's trajectory is KEPT (no TTL) - it is the training and
# analysis record, and the loop's own event log does not last.
KEY_TRAJ = "vera:delegate:trajectory:%s"
KEY_TRAJ_INDEX = "vera:delegate:trajectories"
MEMORY_CAP = "memory.store"
_TASKS: Dict[str, "asyncio.Task[Any]"] = {}
_WORKTREES: Dict[str, str] = {}                        # job id -> worktree path (this process)


def _redis():
    return _orch.REDIS


def _ev():
    return sys.modules.get("evolve_capabilities") or sys.modules.get("Vera.vera.evolve.evolve_capabilities")


def _ctx():
    # The loader registers the context module by bare filename; an import by
    # package path (tests, tools) registers it under its full name.
    return (sys.modules.get("vera_context") or sys.modules.get("context")
            or sys.modules.get("Vera.vera.fabric.context"))


async def _save(job: Dict[str, Any]) -> None:
    r = _redis()
    if not r:
        return
    job["updated_at"] = now_iso()
    await r.set(KEY_JOB % job["id"], json.dumps(job, default=str), ex=JOB_TTL_S)
    await r.zadd(KEY_INDEX, {job["id"]: time.time()})


async def _load(job_id: str) -> Optional[Dict[str, Any]]:
    r = _redis()
    if not r or not job_id:
        return None
    raw = await r.get(KEY_JOB % job_id)
    if not raw:
        return None
    try:
        return json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception:
        return None


async def _worktree_for(job_id: str) -> str:
    wt = _WORKTREES.get(job_id)
    if wt:
        return wt
    job = await _load(job_id)
    return str((job or {}).get("worktree") or "")


# ── the worktree ─────────────────────────────────────────────────────────────

async def _make_worktree(job_id: str, ref: str) -> Dict[str, Any]:
    ev = _ev()
    if ev is None:
        return {"ok": False, "error": "evolve_capabilities is not loaded"}
    root = ev._repo_root()
    path = root / ev._WORKTREE_DIR / D.worktree_name(job_id)
    # NEVER `git worktree prune` here (incident 2026-09-28): the .git is SHARED
    # by every sandbox, and a process that cannot see the other worktrees'
    # paths (any container) prunes their registrations - it severed 252.
    res = await ev._git("worktree", "add", "--detach", str(path), ref, timeout=120, repo_root=root)
    if not res.get("ok"):
        return {"ok": False, "error": "git worktree add --detach %s failed: %s"
                % (ref, (res.get("err") or res.get("out") or "")[:300])}
    head = await ev._git("-C", str(path), "rev-parse", "--short", "HEAD", repo_root=root)
    return {"ok": True, "path": str(path), "head": (head.get("out") or "").strip()}


async def _drop_worktree(path: str) -> None:
    ev = _ev()
    if ev is None or not path:
        return
    try:
        root = ev._repo_root()
        # Remove THIS worktree only - never prune (see _make_worktree).
        await ev._git("worktree", "remove", "--force", path, timeout=120, repo_root=root)
    except Exception as e:
        log.debug("delegate worktree cleanup %s: %s", path, e)


# ── the job in Loop Lab (ROADMAP J1) ─────────────────────────────────────────

async def _mark_run(job: Dict[str, Any]) -> None:
    """Write the job's facts into its loop's run hash, where the Loop Lab record
    builder (loop_record_core.delegate_job) reads them."""
    r = _redis()
    if not r:
        return
    try:
        await r.hset("vera:loop:run:%s" % job["session_id"], D.RUN_FIELD,
                     json.dumps(D.record_fields(job), default=str))
    except Exception as e:
        log.debug("delegate run mark: %s", e)


async def _record_run(session_id: str) -> None:
    cap = CAPABILITY_REGISTRY.get("evolve.loop.record") or {}
    fn = cap.get("raw") or cap.get("func")
    if not fn:
        return
    try:
        await fn(session_id=session_id, where="delegate")
    except Exception as e:
        log.debug("delegate loop record: %s", e)


# ── the trajectory (ROADMAP J7) ──────────────────────────────────────────────

def _decode(raw: Any) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        return json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception:
        return None


async def _loop_events(session_id: str) -> List[Dict[str, Any]]:
    r = _redis()
    if not r or not session_id:
        return []
    out: List[Dict[str, Any]] = []
    for raw in await r.lrange("vera:loop:events:%s" % session_id, 0, -1) or []:
        e = _decode(raw)
        if isinstance(e, dict):
            out.append(e)
    return out


async def _save_traj(traj: Dict[str, Any]) -> None:
    r = _redis()
    if not r or not traj.get("job_id"):
        return
    await r.set(KEY_TRAJ % traj["job_id"], json.dumps(traj, default=str))
    await r.zadd(KEY_TRAJ_INDEX, {traj["job_id"]: time.time()})


async def _load_traj(job_id: str) -> Optional[Dict[str, Any]]:
    r = _redis()
    if not r or not job_id:
        return None
    return _decode(await r.get(KEY_TRAJ % job_id))


async def _keep_trajectory(job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build the job's trajectory from its loop's events and keep it. A rating
    already given survives a rebuild. Best effort: never fails the job."""
    try:
        traj = T.build(job, await _loop_events(job.get("session_id", "")),
                       metrics=D.report_metrics(job.get("report") or ""))
        old = await _load_traj(job.get("id", ""))
        if old:
            for k in ("rating", "rating_history", "memory_ids"):
                if old.get(k):
                    traj[k] = old[k]
        await _save_traj(traj)
        return traj
    except Exception as e:
        log.debug("delegate trajectory %s: %s", job.get("id"), e)
        return None


async def _trajectory_for(job_id: str) -> Optional[Dict[str, Any]]:
    """The kept trajectory, else one built now (a job a restart interrupted
    never reached the end of its run)."""
    traj = await _load_traj(job_id)
    if traj:
        return traj
    job = await _load(job_id)
    if not job:
        return None
    if not D.terminal(job.get("status", "")) and job_id not in _TASKS:
        job["status"] = "interrupted"
    return await _keep_trajectory(job)


# ── the run ──────────────────────────────────────────────────────────────────

def _engine_kwargs(fn, want: Dict[str, Any]) -> Dict[str, Any]:
    """Only what dag.agent_loop_v6 accepts (v7 forwards **kwargs to it)."""
    v6 = CAPABILITY_REGISTRY.get("dag.agent_loop_v6") or {}
    raw = v6.get("raw") or v6.get("func")
    try:
        params = set(inspect.signature(raw).parameters) if raw else set()
    except (TypeError, ValueError):
        params = set()
    return {k: v for k, v in want.items() if not params or k in params}


async def _board(job: Dict[str, Any], kind: str, body: str) -> None:
    if not job.get("board_item"):
        return
    cap = CAPABILITY_REGISTRY.get(D.BOARD_CAP) or {}
    fn = cap.get("raw") or cap.get("func")
    if not fn:
        return
    try:
        await fn(id=job["board_item"], frm=D.BOARD_FROM, kind=kind, body=body[:4000])
    except Exception as e:
        log.debug("delegate board comment: %s", e)


async def _run(job: Dict[str, Any], goal: str) -> None:
    sid = job["session_id"]
    ctx = _ctx()
    guard = None
    try:
        if ctx is not None and hasattr(ctx, "set_session_cap_guard"):
            guard = ctx.set_session_cap_guard(sid, D.guard_spec(job["id"], job.get("board_item", "")))
        cap = CAPABILITY_REGISTRY.get("dag.agent_loop_v7") or {}
        fn = cap.get("func") or cap.get("raw")
        if not fn:
            raise RuntimeError("dag.agent_loop_v7 is not loaded")
        kwargs = _engine_kwargs(fn, {
            "session_id": sid, "max_steps": job["max_steps"], "plan_style": job["plan_style"],
            "effort": job["effort"], "base_toolkit": " ".join(D.FS_CAPS),
            "prefer_terminal_tools": False, "enable_step_questions": False,
            "enable_dream_persistence": False,
            # HEADLESS: nobody answers inside a delegated job, and the brief IS
            # the clarification. First live job (2026-09-28): v7 tiered the
            # brief 'strategic' and stopped on a clarify_request. The strategic
            # tier also opens a dream project / master plan - wrong for a report.
            "clarify_mode": "off", "plan_tier": "complex", "auto_escalate": False})
        job["status"] = "running"
        await _save(job)
        await _mark_run(job)
        await _board(job, "progress", "Delegated to Vera (%s): %s - loop session %s, worktree %s @ %s"
                     % (job["mode"], job["title"], sid, job["ref"], job.get("head") or "?"))
        result = await fn(goal=goal, trace_id=sid, **kwargs)
        job["report"] = D.report_from(result)
        job["status"] = "done" if job["report"] else "error"
        if not job["report"]:
            job["error"] = str((result or {}).get("error") or "the loop returned no report")[:500]
    except asyncio.CancelledError:
        job["status"] = "cancelled"
        raise
    except Exception as e:
        job["status"] = "error"
        job["error"] = "%s: %s" % (type(e).__name__, str(e)[:400])
    finally:
        job["ended_at"] = now_iso()
        if guard is not None and ctx is not None:
            try:
                ctx.clear_session_cap_guard(sid, guard.get("token", ""))
            except Exception:
                pass
        await _drop_worktree(job.get("worktree", ""))
        _WORKTREES.pop(job["id"], None)
        _TASKS.pop(job["id"], None)
        try:
            await _save(job)
            await _board(job, "progress" if job["status"] == "done" else "blocked",
                         ("Report from Vera:\n\n" + job["report"][:3500]) if job.get("report")
                         else "Delegated job %s: %s" % (job["status"], job.get("error", "")))
            await emit_event({"type": "evolve.delegate.done", "job_id": job["id"],
                              "session_id": sid, "status": job["status"],
                              "report_chars": len(job.get("report") or "")})
        except Exception:
            pass
        # Loop Lab: the job's final facts onto its loop's run hash, then the record
        # rebuilt from them (the automatic record at the loop's end may have been
        # built before the report existed).
        await _mark_run(job)
        await _record_run(sid)
        # J7: the trajectory, while the loop's events are still there.
        await _keep_trajectory(job)


@capability(
    "evolve.delegate.start", memory="off",
    http_method="POST", http_path="/evolve/delegate/start", http_tags=["evolve", "delegate"],
    description=(
        "DELEGATE a task to Vera, the way you would brief one of your own agents: a Vera "
        "agentic loop (v7) carries it out in its OWN detached worktree of `ref` and returns "
        "a markdown REPORT (Summary / Findings with path:line / Structure / Open questions). "
        "Mode 'report' only for now: the loop is read-only - it can grep, list, read and "
        "outline files in its worktree (evolve.delegate.fs.*) and comment on a board item, "
        "nothing else. Returns AT ONCE with a job_id; poll evolve.delegate.status, fetch "
        "evolve.delegate.result, stop with evolve.delegate.cancel. Inputs: title (str!), "
        "brief (str! - the handover), plan (str|list - rough steps), suggest_caps (str|list), "
        "suggest_commands (str|list - searches/commands to try), ref (str=bleeding-edge), "
        "paths (str|list - where to start), effort (standard|max - max uses the CPU nodes' "
        "larger models: the critic reviews every step), max_steps (int=12), plan_style "
        "(str=stepwise), board_item (str - a board item id to keep updated), mode (report), "
        "parent_task (str - YOUR overarching task this delegation is part of: kept with the "
        "job's trajectory for analysis and training), delegator (str - who is delegating, "
        "e.g. claude:<session>). When the report is in, RATE it with evolve.delegate.rate "
        "(a rated job is remembered). Output: {ok, job_id, session_id, worktree, head}."),
)
async def cap_evolve_delegate_start(title: str = "", brief: str = "", plan: Any = "",
                                    suggest_caps: Any = "", suggest_commands: Any = "",
                                    ref: str = "bleeding-edge", paths: Any = "",
                                    effort: str = "standard", max_steps: int = D.DEFAULT_MAX_STEPS,
                                    plan_style: str = "stepwise", board_item: str = "",
                                    mode: str = "report", repo: str = "vera",
                                    parent_task: str = "", delegator: str = "",
                                    trace_id=None) -> Dict[str, Any]:
    if not (brief or "").strip():
        return {"error": "brief is required - the handover, as you would brief an agent"}
    mode = (mode or "report").strip().lower()
    if mode not in D.MODES:
        return {"error": "mode must be one of %s (editing comes once reports are confirmed good)"
                % ", ".join(D.MODES)}
    if (repo or "vera") != "vera":
        return {"error": "only repo=vera is supported for now"}
    if _orch.is_dev_sandbox():
        # A sandbox shares prod's .git but not its view of the worktrees;
        # git worktree bookkeeping from inside one damages every other.
        return {"error": "evolve.delegate runs on prod only - a dev sandbox shares the "
                         "repo's .git and must not add or remove worktrees in it"}
    if not _redis():
        return {"error": "no Redis - a delegated job needs somewhere to keep its state"}
    effort = (effort or "standard").strip().lower()
    effort = effort if effort in D.EFFORTS else "standard"
    job_id = D.new_job_id()
    wt = await _make_worktree(job_id, (ref or "bleeding-edge").strip())
    if not wt.get("ok"):
        return {"error": wt.get("error")}
    job = {"id": job_id, "session_id": D.session_for(job_id), "title": (title or "code report")[:200],
           "mode": mode, "effort": effort, "ref": ref, "head": wt.get("head", ""),
           "worktree": wt["path"], "board_item": (board_item or "").strip(),
           "plan_style": (plan_style or "stepwise").strip().lower(),
           "max_steps": max(2, min(40, int(max_steps or D.DEFAULT_MAX_STEPS))),
           "status": "starting", "created_at": now_iso(), "report": "", "error": "",
           # J7: why (the delegator's task) and what (the brief as given) travel
           # with the job into its trajectory.
           "parent_task": str(parent_task or "")[:T.MAX_PARENT_TASK],
           "delegator": str(delegator or "")[:T.MAX_DELEGATOR],
           "brief": str(brief or "")[:D.MAX_BRIEF_CHARS],
           "plan": D._list(plan), "suggest_caps": D._list(suggest_caps)}
    _WORKTREES[job_id] = wt["path"]
    goal = D.compose_goal(title=job["title"], brief=brief, plan=plan, suggest_caps=suggest_caps,
                          suggest_commands=suggest_commands, ref=ref, repo=repo, paths=paths,
                          board_item=job["board_item"])
    job["goal_chars"] = len(goal)
    job["goal"] = goal
    await _save(job)
    _TASKS[job_id] = asyncio.ensure_future(_run(job, goal))
    return {"ok": True, "job_id": job_id, "session_id": job["session_id"],
            "worktree": wt["path"], "head": wt.get("head", ""),
            "next": "poll evolve.delegate.status(job_id=%s); evolve.delegate.result when done" % job_id}


async def _live(job: Dict[str, Any]) -> Dict[str, Any]:
    """The loop's own progress: its run hash and a count of its events."""
    r = _redis()
    if not r:
        return {}
    sid = job.get("session_id", "")
    out: Dict[str, Any] = {}
    try:
        h = await r.hgetall("vera:loop:run:%s" % sid) or {}
        out = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
               for k, v in h.items()}
        out["events"] = await r.llen("vera:loop:events:%s" % sid)
        tail = await r.lrange("vera:loop:events:%s" % sid, -40, -1) or []
        steps = []
        for raw in tail:
            try:
                e = json.loads(raw)
            except Exception:
                continue
            if str(e.get("type", "")).endswith(".step_start"):
                steps.append(str(e.get("title") or "")[:100])
        if steps:
            out["recent_steps"] = steps[-5:]
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


@capability("evolve.delegate.status", memory="off", silent=True,
            http_method="GET", http_path="/evolve/delegate/status", http_tags=["evolve", "delegate"],
            description=("A delegated job's state (starting|running|done|error|cancelled|"
                         "interrupted) and its loop's live progress (events, recent steps). "
                         "Input: job_id (str!). Output: {job, loop}."))
async def cap_evolve_delegate_status(job_id: str = "", trace_id=None) -> Dict[str, Any]:
    job = await _load(job_id)
    if not job:
        return {"error": "no such delegated job: %s" % job_id}
    if not D.terminal(job.get("status", "")) and job_id not in _TASKS:
        # Not finished, yet no task runs it in this process: a restart ended it.
        job["status"] = "interrupted"
    view = {k: v for k, v in job.items() if k != "report"}
    view["report_chars"] = len(job.get("report") or "")
    return {"ok": True, "job": view, "loop": await _live(job)}


@capability("evolve.delegate.result", memory="off", silent=True,
            http_method="GET", http_path="/evolve/delegate/result", http_tags=["evolve", "delegate"],
            description=("The REPORT of a finished delegated job (markdown: Summary / Findings "
                         "with path:line / Structure / Open questions). Input: job_id (str!). "
                         "Output: {ok, status, report, error, head, ref}."))
async def cap_evolve_delegate_result(job_id: str = "", trace_id=None) -> Dict[str, Any]:
    job = await _load(job_id)
    if not job:
        return {"error": "no such delegated job: %s" % job_id}
    if not D.terminal(job.get("status", "")):
        return {"ok": False, "status": job.get("status"), "note": "still running - poll evolve.delegate.status"}
    return {"ok": job.get("status") == "done", "status": job.get("status"),
            "report": job.get("report", ""), "error": job.get("error", ""),
            "ref": job.get("ref"), "head": job.get("head"), "session_id": job.get("session_id")}


@capability("evolve.delegate.cancel", memory="off",
            http_method="POST", http_path="/evolve/delegate/cancel", http_tags=["evolve", "delegate"],
            description=("Stop a delegated job: cancels its task and sets its loop's cancel flag "
                         "(the loop stops at its next turn). Input: job_id (str!)."))
async def cap_evolve_delegate_cancel(job_id: str = "", trace_id=None) -> Dict[str, Any]:
    job = await _load(job_id)
    if not job:
        return {"error": "no such delegated job: %s" % job_id}
    r = _redis()
    if r:
        await r.hset("vera:loop:run:%s" % job["session_id"],
                     mapping={"status": "cancelled", "updated_at": now_iso()})
    t = _TASKS.get(job_id)
    if t and not t.done():
        t.cancel()
        return {"ok": True, "cancelled": True}
    return {"ok": True, "cancelled": False, "note": "no running task in this process; flag set"}


@capability("evolve.delegate.list", memory="off", silent=True,
            http_method="GET", http_path="/evolve/delegate/list", http_tags=["evolve", "delegate"],
            description="Recent delegated jobs, newest first. Input: limit (int=20).")
async def cap_evolve_delegate_list(limit: int = 20, trace_id=None) -> Dict[str, Any]:
    r = _redis()
    if not r:
        return {"jobs": []}
    ids = await r.zrevrange(KEY_INDEX, 0, max(0, int(limit) - 1)) or []
    jobs = []
    for i in ids:
        j = await _load(i.decode() if isinstance(i, bytes) else i)
        if j:
            jobs.append({k: j.get(k) for k in ("id", "title", "status", "mode", "effort", "ref",
                                               "created_at", "ended_at", "board_item")})
    return {"jobs": jobs}


# ── trajectories and ratings (ROADMAP J7) ────────────────────────────────────

@capability("evolve.delegate.trajectory", memory="off", silent=True,
            http_method="GET", http_path="/evolve/delegate/trajectory", http_tags=["evolve", "delegate"],
            description=("A delegated job's TRAJECTORY - the record kept for analysis and "
                         "training: parent_task (the delegator's overarching task), brief, plan, "
                         "the loop's tier/intent/catalogue, every step with its tool calls (cap, "
                         "args, the model's stated reason, ok/error, result), the report and its "
                         "rating. Input: job_id (str!). Output: {ok, trajectory}."))
async def cap_evolve_delegate_trajectory(job_id: str = "", trace_id=None) -> Dict[str, Any]:
    traj = await _trajectory_for(job_id)
    if not traj:
        return {"error": "no such delegated job: %s" % job_id}
    return {"ok": True, "trajectory": traj}


@capability("evolve.delegate.trajectories", memory="off", silent=True,
            http_method="GET", http_path="/evolve/delegate/trajectories", http_tags=["evolve", "delegate"],
            description=("Kept delegated-job trajectories, newest first: title, parent task, "
                         "status, steps, calls, verdict. Inputs: limit (int=20), rated "
                         "(str all|rated|unrated)."))
async def cap_evolve_delegate_trajectories(limit: int = 20, rated: str = "all",
                                           trace_id=None) -> Dict[str, Any]:
    r = _redis()
    if not r:
        return {"trajectories": []}
    want = (rated or "all").strip().lower()
    rows = []
    for i in await r.zrevrange(KEY_TRAJ_INDEX, 0, -1) or []:
        t = await _load_traj(i.decode() if isinstance(i, bytes) else i)
        if not t:
            continue
        has = bool((t.get("rating") or {}).get("verdict"))
        if (want == "rated" and not has) or (want == "unrated" and has):
            continue
        rows.append(T.summary_row(t))
        if len(rows) >= max(1, int(limit or 20)):
            break
    return {"trajectories": rows}


async def _remember(traj: Dict[str, Any]) -> str:
    """A rated job into long-term memory (memory.store). Returns the id, or ''."""
    text = T.memory_text(traj)
    cap = CAPABILITY_REGISTRY.get(MEMORY_CAP) or {}
    fn = cap.get("raw") or cap.get("func")
    if not text or not fn:
        return ""
    try:
        res = await fn(text=text, session_id=traj.get("session_id", ""), category="delegate",
                       tags=T.memory_tags(traj), importance=T.memory_importance(traj))
        return str((res or {}).get("id") or "") if isinstance(res, dict) else ""
    except Exception as e:
        log.debug("delegate memory %s: %s", traj.get("job_id"), e)
        return ""


@capability("evolve.delegate.rate", memory="off",
            http_method="POST", http_path="/evolve/delegate/rate", http_tags=["evolve", "delegate"],
            description=("RATE a delegated job's report - the delegating agent does this once it "
                         "has checked the report against the code. verdict useful|partly|wrong, "
                         "notes = why (what was right, what was missed or invented). The rating "
                         "labels the job's trajectory (training data), shows on its Loop Lab "
                         "record, and a RATED job is written to long-term memory (a 'wrong' one "
                         "as a warning, not an answer). Re-rating replaces the rating and keeps "
                         "the history. Inputs: job_id (str!), verdict (str!), notes (str), by "
                         "(str - who rates, e.g. claude:<session>). Output: {ok, verdict, memory_id}."))
async def cap_evolve_delegate_rate(job_id: str = "", verdict: str = "", notes: str = "",
                                   by: str = "", trace_id=None) -> Dict[str, Any]:
    traj = await _trajectory_for(job_id)
    if not traj:
        return {"error": "no such delegated job: %s" % job_id}
    if not D.terminal(traj.get("status", "")) and traj.get("status") != "interrupted":
        return {"error": "the job is still %s - rate its report once it is done" % traj.get("status")}
    try:
        traj = T.rate(traj, verdict, notes=notes, by=by or "", at=now_iso())
    except ValueError as e:
        return {"error": str(e)}
    mem = await _remember(traj)
    if mem:
        traj["memory_ids"] = list(traj.get("memory_ids") or []) + [mem]
    await _save_traj(traj)
    job = await _load(job_id)
    if job:
        job["rating"] = traj["rating"]["verdict"]
        await _save(job)
        await _mark_run(job)
        await _record_run(job.get("session_id", ""))
    return {"ok": True, "verdict": traj["rating"]["verdict"], "memory_id": mem}


# ── the loop's jailed read tools ─────────────────────────────────────────────

async def _ide(cap_name: str, **kw) -> Any:
    cap = CAPABILITY_REGISTRY.get(cap_name) or {}
    fn = cap.get("raw") or cap.get("func")
    if not fn:
        return {"error": "%s is not loaded" % cap_name}
    return await fn(**kw)


def _clean(root: str, res: Any) -> Any:
    try:
        return json.loads(D.relativise(root, json.dumps(res, default=str)))
    except Exception:
        return res


@capability("evolve.delegate.fs.grep", memory="off", silent=True,
            description=("Search a delegated job's checkout (read-only). Inputs: job (str!), "
                         "pattern (str!), include (str glob), exclude (str glob), is_regex "
                         "(bool), case_sensitive (bool), max_results (int=100). Paths in the "
                         "output are relative to the checkout."))
async def cap_evolve_delegate_fs_grep(job: str = "", pattern: str = "", include: str = "",
                                      exclude: str = "", is_regex: bool = False,
                                      case_sensitive: bool = False, max_results: int = 100,
                                      trace_id=None) -> Any:
    root = await _worktree_for(job)
    if not root:
        return {"error": "unknown job"}
    if not pattern:
        return {"error": "pattern is required"}
    res = await _ide("ide.code.grep", pattern=pattern, root=root, is_regex=bool(is_regex),
                     case_sensitive=bool(case_sensitive), include=include, exclude=exclude,
                     max_results=max(1, min(500, int(max_results or 100))), context_lines=1,
                     session_id="")
    return _clean(root, res)


@capability("evolve.delegate.fs.list", memory="off", silent=True,
            description=("List files in a delegated job's checkout (read-only). Inputs: job "
                         "(str!), root (str - a sub-directory, relative), include (str glob), "
                         "max_files (int=500)."))
async def cap_evolve_delegate_fs_list(job: str = "", root: str = "", include: str = "",
                                      max_files: int = 500, trace_id=None) -> Any:
    base = await _worktree_for(job)
    if not base:
        return {"error": "unknown job"}
    sub = D.jail_path(base, root or ".")
    if not sub:
        return {"error": "path escapes the checkout"}
    res = await _ide("ide.code.list_files", root=sub, include=include,
                     max_files=max(1, min(5000, int(max_files or 500))))
    return _clean(base, res)


@capability("evolve.delegate.fs.read", memory="off", silent=True,
            description=("Read lines of a file in a delegated job's checkout (read-only). "
                         "Inputs: job (str!), path (str! - relative), start (int=1), end "
                         "(int=start+199)."))
async def cap_evolve_delegate_fs_read(job: str = "", path: str = "", start: int = 1,
                                      end: int = 0, trace_id=None) -> Any:
    base = await _worktree_for(job)
    if not base:
        return {"error": "unknown job"}
    full = D.jail_path(base, path)
    if not full or not Path(full).is_file():
        return {"error": "not a file inside the checkout: %s" % path}
    s = max(1, int(start or 1))
    e = int(end or 0) or s + 199
    res = await _ide("ide.code.read_lines", path=full, start=s, end=min(e, s + 599), root=base)
    return _clean(base, res)


@capability("evolve.delegate.fs.outline", memory="off", silent=True,
            description=("A file's classes/functions with line numbers, in a delegated job's "
                         "checkout (read-only). Inputs: job (str!), path (str! - relative)."))
async def cap_evolve_delegate_fs_outline(job: str = "", path: str = "", trace_id=None) -> Any:
    base = await _worktree_for(job)
    if not base:
        return {"error": "unknown job"}
    full = D.jail_path(base, path)
    if not full or not Path(full).is_file():
        return {"error": "not a file inside the checkout: %s" % path}
    res = await _ide("ide.code.outline", path=full, root=base, session_id="")
    return _clean(base, res)
