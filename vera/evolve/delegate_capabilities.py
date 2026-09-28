"""evolve.delegate.* - hand a task to a Vera agentic loop (user, 2026-09-28).

Claude (or any MCP caller) briefs Vera the way it briefs one of its own
agents; a v7 loop does the work in its own detached worktree and returns a
report. Report mode only for now (see delegate_core for the safety model).

    evolve.delegate.start   -> {job_id, session_id, worktree}   (returns at once)
    evolve.delegate.status  -> job state + the loop's live progress
    evolve.delegate.result  -> the report once done
    evolve.delegate.cancel  -> stop it (task cancelled + the loop's cancel flag)
    evolve.delegate.list    -> recent jobs
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

log = logging.getLogger("vera.evolve.delegate")

KEY_JOB = "vera:delegate:job:%s"
KEY_INDEX = "vera:delegate:jobs"
JOB_TTL_S = 14 * 86400
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
    await ev._git("worktree", "prune", repo_root=root)
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
        await ev._git("worktree", "remove", "--force", path, timeout=120, repo_root=root)
        await ev._git("worktree", "prune", repo_root=root)
    except Exception as e:
        log.debug("delegate worktree cleanup %s: %s", path, e)


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
            "enable_dream_persistence": False})
        job["status"] = "running"
        await _save(job)
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
        "(str=stepwise), board_item (str - a board item id to keep updated), mode (report). "
        "Output: {ok, job_id, session_id, worktree, head}."),
)
async def cap_evolve_delegate_start(title: str = "", brief: str = "", plan: Any = "",
                                    suggest_caps: Any = "", suggest_commands: Any = "",
                                    ref: str = "bleeding-edge", paths: Any = "",
                                    effort: str = "standard", max_steps: int = D.DEFAULT_MAX_STEPS,
                                    plan_style: str = "stepwise", board_item: str = "",
                                    mode: str = "report", repo: str = "vera",
                                    trace_id=None) -> Dict[str, Any]:
    if not (brief or "").strip():
        return {"error": "brief is required - the handover, as you would brief an agent"}
    mode = (mode or "report").strip().lower()
    if mode not in D.MODES:
        return {"error": "mode must be one of %s (editing comes once reports are confirmed good)"
                % ", ".join(D.MODES)}
    if (repo or "vera") != "vera":
        return {"error": "only repo=vera is supported for now"}
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
           "status": "starting", "created_at": now_iso(), "report": "", "error": ""}
    _WORKTREES[job_id] = wt["path"]
    goal = D.compose_goal(title=job["title"], brief=brief, plan=plan, suggest_caps=suggest_caps,
                          suggest_commands=suggest_commands, ref=ref, repo=repo, paths=paths,
                          board_item=job["board_item"])
    job["goal_chars"] = len(goal)
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
