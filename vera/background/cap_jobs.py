"""
cap_jobs.py — run any capability as idle-queue work, through the job system
============================================================================

Vera has two things that were never joined:

  the idle queue     `vera/idle_queue.py` + `idle_queue_service.py`. Work that
                     waits until the box has been quiet for ten minutes, is
                     listed in the Background panel, can be stopped, and is
                     dropped if the runner dies. It could run six built-in
                     kinds and nothing else.

  the job system     `dispatch_task` → the `vera:tasks` Redis stream → a
                     worker consumer group → `vera:results`. Every capability
                     can already run this way; `cluster.job.stop` cancels it,
                     `jobs.history` remembers it.

An automation that wanted "run this capability, but only when nobody is using
Vera" therefore had no way to say it. The first intel pipelines called
`llm.generate` directly every twenty minutes and were switched off for
hammering the box. This module is the join:

  background.cap.enqueue(name, arguments)   queues a `cap` job
  the `cap` handler                          when the box is quiet, dispatches
                                             the capability INTO the task
                                             stream with a background label
  the worker                                 runs it inside BACKGROUND_LLM, so
                                             every Ollama call it makes is
                                             demoted off the GPU while a person
                                             is active, and logged as
                                             background — the same adapter the
                                             dream scheduler uses
  background.cap.result(id)                  the stored outcome, for 48 hours

The kind is deliberately NOT pre-emptible. A cap that calls the LLM holds the
GPU gate, and the queue's busy probe would then read the queue's own job as
somebody else's activity and cancel it milliseconds after starting — the
livelock `idle_queue.PREEMPTIBLE_KINDS` documents. Gate-at-start is the right
semantics: a job never begins during use, and once begun it finishes or hits
its own timeout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    capability, emit_event, now_iso,
)
from Vera.vera.background import cap_jobs_core as core

try:
    from Vera.vera import idle_queue_service as _svc
except Exception:                                      # pragma: no cover
    try:
        from vera import idle_queue_service as _svc    # type: ignore
    except Exception:
        _svc = None

log = logging.getLogger("vera.background.cap_jobs")


def _redis():
    return getattr(_orch, "REDIS", None)


# ═════════════════════════════════════════════════════════════════════════════
#  THE HANDLER  —  what the idle queue runs for kind="cap"
# ═════════════════════════════════════════════════════════════════════════════

async def _store_result(job_id: str, record: Dict[str, Any]) -> None:
    r = _redis()
    if not r:
        return
    try:
        await r.set(core.result_key(job_id), json.dumps(record),
                    ex=core.RESULT_TTL_S)
    except Exception as e:                             # pragma: no cover
        log.warning("cap job %s: result not stored: %s", job_id, e)


async def _cap_job(job: Dict[str, Any], should_continue) -> Dict[str, Any]:
    """Dispatch the capability into the task stream and wait for its result.

    `should_continue` is honoured between polls: if the runner asks this job
    to yield, the dispatched task is cancelled through the same path
    `cluster.job.stop` uses, and the job goes back on the queue.
    """
    job_id = str(job.get("id") or "")
    payload = job.get("payload") or {}
    name = str(payload.get("name") or "")
    args = payload.get("arguments") or {}
    timeout_s = core.clamp_timeout(payload.get("timeout_s"))
    started = time.time()

    ok, why = core.validate_request(name, args,
                                    known=set(_orch.CAPABILITY_REGISTRY.keys()))
    if not ok:
        rec = {"id": job_id, "name": name, "state": "failed", "error": why,
               "started": now_iso(), "finished": now_iso(), "elapsed_s": 0}
        await _store_result(job_id, rec)
        return {"items": 1, "note": "refused: " + why}

    label = core.bg_label(name, job_id)
    task_id = await _orch.dispatch_task(name, dict(args), trace_id=job_id,
                                        bg=label)
    await emit_event({"type": "background.cap.start", "id": job_id,
                      "capability": name, "task": task_id, "bg": label})

    # Wait in short slices so a yield request is noticed promptly. The result
    # future is registered once; wait_for_result would pop it on timeout, so
    # this polls the same future by hand instead.
    fut = asyncio.get_event_loop().create_future()
    _orch.PENDING_RESULTS[task_id] = fut
    result: Any = None
    deadline = started + timeout_s
    try:
        while True:
            try:
                result = await asyncio.wait_for(asyncio.shield(fut), timeout=2.0)
                break
            except asyncio.TimeoutError:
                pass
            if time.time() > deadline:
                await _cancel_task(task_id)
                result = {"error": "timeout", "timeout_s": timeout_s}
                break
            reason = ""
            try:
                reason = await should_continue()
            except Exception:
                reason = ""
            if reason and "pre-empted" in str(reason):
                await _cancel_task(task_id)
                _orch.PENDING_RESULTS.pop(task_id, None)
                return {"yielded": str(reason)}
    finally:
        _orch.PENDING_RESULTS.pop(task_id, None)

    elapsed = round(time.time() - started, 1)
    state = core.outcome(result)
    rec = {"id": job_id, "name": name, "task_id": task_id, "state": state,
           "result": result, "started": now_iso(), "elapsed_s": elapsed,
           "submitted_by": payload.get("submitted_by") or ""}
    await _store_result(job_id, rec)
    await emit_event({"type": "background.cap.done", "id": job_id,
                      "capability": name, "state": state, "elapsed_s": elapsed})
    return {"items": 1, "note": state + ": " + core.summarise_result(result)}


async def _cancel_task(task_id: str) -> None:
    """The same cooperative cancel cluster.job.stop performs."""
    try:
        inner = _orch.RUNNING_TASKS.get(task_id)
        if inner and not inner.done():
            inner.cancel()
        r = _redis()
        if r:
            await r.sadd(_orch.REDIS_CANCEL_SET, task_id)
            await r.publish(_orch.REDIS_CANCEL_CHANNEL, task_id)
    except Exception as e:                             # pragma: no cover
        log.debug("cap job cancel %s: %s", task_id, e)


if _svc is not None:
    _svc.register_handler(core.KIND_CAP, _cap_job)
    log.info("cap_jobs: idle-queue handler registered for kind=%s", core.KIND_CAP)
else:                                                  # pragma: no cover
    log.warning("cap_jobs: idle_queue_service unavailable - kind=cap will not run")


# ═════════════════════════════════════════════════════════════════════════════
#  CAPABILITIES
# ═════════════════════════════════════════════════════════════════════════════

async def _read_result(job_id: str) -> Optional[Dict[str, Any]]:
    r = _redis()
    if not r:
        return None
    raw = await r.get(core.result_key(job_id))
    if not raw:
        return None
    try:
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        return None


async def _queue_state(job_id: str) -> Optional[Dict[str, Any]]:
    if _svc is None:
        return None
    for j in await _svc.load_jobs():
        if j.get("id") == job_id:
            return {"state": j.get("state"), "waiting_for_s": j.get("waiting_for_s"),
                    "last_note": j.get("last_note") or j.get("note") or ""}
    return None


@capability(
    "background.cap.enqueue", http_method="POST",
    http_path="/background/cap/enqueue", http_tags=["obs", "background"],
    memory="on",
    description="Queue ANY capability to run when Vera is idle, through the "
                "idle queue and the task stream. It starts only after the box "
                "has been quiet for the queue's minimum (600s), runs inside "
                "the background LLM context (its Ollama calls are demoted off "
                "the GPU while a person is active), and its result is kept "
                "for 48h. Pass wait_s to block until it has run (or that many "
                "seconds), which lets an automation make one call instead of "
                "polling. Refuses sys.*, background.*, promotions and queue "
                "controls. Input: name (str!), arguments (dict), title (str), "
                "timeout_s (float=900, max 3600), wait_s (float=0), "
                "id (str - reuse to replace a queued job), submitted_by (str). "
                "Output: {ok, id, queued, state, result?, pending?}.",
)
async def cap_enqueue(name: str = "", arguments: Optional[Dict[str, Any]] = None,
                      title: str = "", timeout_s: float = 0.0,
                      wait_s: float = 0.0, id: str = "",
                      submitted_by: str = "", trace_id=None):
    ok, why = core.validate_request(name, arguments,
                                    known=set(_orch.CAPABILITY_REGISTRY.keys()))
    if not ok:
        return {"ok": False, "error": why}
    if _svc is None:
        return {"ok": False, "error": "idle queue service unavailable"}
    payload = core.build_payload(name, arguments, timeout_s, submitted_by)
    # A caller-supplied id replaces a queued job of the same id rather than
    # queueing a duplicate; otherwise dedupe on the title so a producer on a
    # timer cannot pile up identical jobs while the box is busy.
    dedupe = ("id:" + id.strip()) if id.strip() else ""
    sub = await _svc.submit(core.KIND_CAP, core.job_title(name, title),
                            dedupe_key=dedupe or f"{core.KIND_CAP}:{name}:{json.dumps(payload['arguments'], sort_keys=True)[:200]}",
                            payload=payload, total=1)
    job_id = sub.get("id", "")
    out: Dict[str, Any] = {"ok": True, "id": job_id, "queued": sub.get("queued", False),
                           "name": name, "reason": sub.get("reason", "")}
    if wait_s and wait_s > 0:
        deadline = time.time() + min(float(wait_s), core.MAX_TIMEOUT_S)
        while time.time() < deadline:
            rec = await _read_result(job_id)
            if rec:
                out.update({"state": rec.get("state"), "result": rec.get("result"),
                            "elapsed_s": rec.get("elapsed_s")})
                return out
            await asyncio.sleep(3.0)
        out["pending"] = True
        out["queue"] = await _queue_state(job_id)
        out["note"] = "not run within wait_s - fetch later with background.cap.result"
        return out
    out["queue"] = await _queue_state(job_id)
    return out


@capability(
    "background.cap.result", http_method="GET",
    http_path="/background/cap/result", http_tags=["obs", "background"],
    memory="off",
    description="The stored outcome of a queued capability job (48h). "
                "Input: id (str!). Output: {found, state, result, elapsed_s, "
                "queue} - `queue` is the live queue record while it is still "
                "waiting or running.",
)
async def cap_result(id: str = "", trace_id=None):
    if not id.strip():
        return {"found": False, "error": "id is required"}
    rec = await _read_result(id.strip())
    if rec:
        return dict(rec, found=True)
    q = await _queue_state(id.strip())
    return {"found": False, "id": id.strip(), "queue": q,
            "state": (q or {}).get("state") or "unknown"}


@capability(
    "background.cap.list", http_method="GET",
    http_path="/background/cap/list", http_tags=["obs", "background"],
    memory="off",
    description="Queued and recently finished capability jobs. "
                "Input: limit (int=50). Output: {queued:[...], finished:[...]}.",
)
async def cap_list(limit: int = 50, trace_id=None):
    queued = []
    if _svc is not None:
        for j in await _svc.load_jobs():
            if j.get("kind") == core.KIND_CAP:
                p = j.get("payload") or {}
                queued.append({"id": j.get("id"), "state": j.get("state"),
                               "name": p.get("name"), "title": j.get("title"),
                               "waiting_for_s": j.get("waiting_for_s")})
    finished = []
    r = _redis()
    if r:
        try:
            keys = await r.keys(core.result_key("*"))
            for k in list(keys)[: max(1, int(limit))]:
                raw = await r.get(k)
                if raw:
                    d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                    finished.append({"id": d.get("id"), "name": d.get("name"),
                                     "state": d.get("state"),
                                     "elapsed_s": d.get("elapsed_s"),
                                     "started": d.get("started")})
        except Exception as e:                         # pragma: no cover
            log.debug("cap list: %s", e)
    finished.sort(key=lambda d: str(d.get("started") or ""), reverse=True)
    return {"queued": queued, "finished": finished[: max(1, int(limit))]}


log.info("cap_jobs: ready")
