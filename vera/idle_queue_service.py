"""The awaiting-idle queue's RUNNER: the part that actually makes it a queue.

`idle_queue.py` decides *what may run*; it is pure and has no I/O. This module
is the half that does something about it:

  * a durable store (Redis), so a restart keeps the backlog;
  * a handler registry, so a producer can enqueue work by name and let the
    runner decide when it happens;
  * `drain_once()`, the tick that starts one job when the box is idle and
    PRE-EMPTS the running one the moment it is not.

Without this, `background.enqueue` wrote rows nobody read. That was the state
prod shipped in: a queue with no consumer, so only the one producer that
happened to call its own scheduler (the transcript ingest) ever ran.

WHY PRE-EMPTION IS NOT OPTIONAL
Deferring at the start only asks "is the box free *now*". A narration or a
transcript backfill then runs for minutes, and everything that begins after it
contends for the same Ollama node. Taking the node back means stopping work
already in flight, so:

  1. the handler is asked to stop, via the `should_continue` callback it is
     given (the ingest checkpoints per file and returns);
  2. if it has not stopped within `PREEMPT_GRACE_S`, its task is cancelled.

Either way the job goes BACK on the queue rather than failing - it was
interrupted, not broken.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import types
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

try:                                                       # pragma: no cover
    from Vera.vera import idle_queue as _iq
except ImportError:                                        # pragma: no cover
    import idle_queue as _iq                               # type: ignore

try:                                                       # pragma: no cover
    from Vera.vera import idle_queue_eta as _eta
except ImportError:                                        # pragma: no cover
    try:
        import idle_queue_eta as _eta                      # type: ignore
    except ImportError:
        _eta = None                                        # type: ignore

log = logging.getLogger("vera.idle_queue_service")

#: Redis hash: job id -> job JSON. Shared by every instance on the box, which
#: is the point - one queue, not one per process.
REDIS_KEY = "vera:idle_queue:jobs"
#: Measured cost per kind, learned from completed runs. Beside the jobs rather
#: than inside them: a rate is a property of the KIND, and storing it per job
#: would throw the measurement away with the job that produced it.
RATES_KEY = "vera:idle_queue:rates"

#: How long a pre-empted handler gets to stop politely before it is cancelled.
#: Long enough for the ingest to finish the file it is on (checkpointed), short
#: enough that "give the node back" means something.
PREEMPT_GRACE_S = 20.0

# This module is imported under BOTH spellings - `Vera.vera.idle_queue_service`
# and `vera.idle_queue_service` - which Python treats as two unrelated modules
# with two sets of globals. A handler registered through one spelling is then
# invisible to a runner holding the other, and the queue silently drops every
# job as "no handler". Observed here in the container the moment the ingest was
# converted. So the registry and the running slot live in ONE object shared via
# sys.modules, not in this module's own namespace.
_SHARED_KEY = "_vera_idle_queue_shared"
_shared = sys.modules.get(_SHARED_KEY)
if _shared is None:
    _shared = types.ModuleType(_SHARED_KEY)
    _shared.HANDLERS = {}
    _shared.RUNNING = {}
    sys.modules[_SHARED_KEY] = _shared

#: kind -> coroutine(job, should_continue) -> dict
_HANDLERS: Dict[str, Callable[..., Awaitable[Any]]] = _shared.HANDLERS

#: The one job in flight, if any: {"id", "kind", "task", "asked_to_stop_at"}.
_RUNNING: Dict[str, Any] = _shared.RUNNING


# ── handler registry ────────────────────────────────────────────────────────
def register_handler(kind: str, fn: Callable[..., Awaitable[Any]]) -> None:
    """Producers register once at import; the runner dispatches by kind.

    A second producer claiming a kind that is already taken REPLACES the first
    silently - this is a plain dict - and its queued jobs then run the other
    producer's work under their own name. Nothing would look wrong until the
    output did. Registration still wins (refusing could leave a kind with no
    handler at all, which drops jobs), but it says so loudly.
    """
    prev = _HANDLERS.get(str(kind))
    if prev is not None and prev is not fn:
        log.warning("idle queue: handler for %r replaced (%s -> %s) - two "
                    "producers claim this kind, and jobs of it will now run "
                    "the SECOND one's work", kind,
                    getattr(prev, "__name__", prev),
                    getattr(fn, "__name__", fn))
    _HANDLERS[str(kind)] = fn


def handlers() -> Dict[str, Callable[..., Awaitable[Any]]]:
    return dict(_HANDLERS)


def has_handler(kind: str) -> bool:
    return str(kind) in _HANDLERS


# ── durable store ───────────────────────────────────────────────────────────
def _redis():
    """The live connection, or None.

    `get_redis()` does not exist - that was invented, and every store call
    failed with ImportError until a live instance proved it. The real accessor
    is the module-level `REDIS` global, which is None until startup connects
    it, so this is read fresh each time rather than cached at import.
    """
    try:
        from Vera.vera import capability_orchestration as _orch
    except ImportError:                                    # pragma: no cover
        try:
            from vera import capability_orchestration as _orch   # type: ignore
        except ImportError:
            return None
    return getattr(_orch, "REDIS", None)


async def load_jobs() -> List[Dict[str, Any]]:
    """Every job, waiting or running. Bad rows are skipped, never fatal."""
    r = _redis()
    if r is None:
        return []
    try:
        raw = await r.hgetall(REDIS_KEY)
    except Exception as e:
        log.debug("idle queue load: %s", e)
        return []
    out: List[Dict[str, Any]] = []
    for v in (raw or {}).values():
        try:
            out.append(json.loads(v if isinstance(v, str) else v.decode()))
        except Exception:
            continue
    return out


async def load_rates() -> Dict[str, Any]:
    """Measured cost per kind. Empty is the correct answer before anything has
    completed - the estimator returns None on an empty rate rather than
    inventing one."""
    r = _redis()
    if r is None:
        return {}
    try:
        raw = await r.get(RATES_KEY)
        return json.loads(raw) if raw else {}
    except Exception as e:
        log.debug("idle queue rates load: %s", e)
        return {}


async def save_rates(rates: Dict[str, Any]) -> None:
    r = _redis()
    if r is None or not rates:
        return
    try:
        await r.set(RATES_KEY, json.dumps(rates))
    except Exception as e:                                 # pragma: no cover
        log.debug("idle queue rates save: %s", e)


async def save_job(job: Dict[str, Any]) -> bool:
    """True only if it is actually stored.

    Returns a verdict rather than swallowing: `background.enqueue` reported
    ok:True while storing nothing, because the write failed into a debug log.
    A queue that says "queued" without queueing is worse than one that refuses.
    """
    r = _redis()
    if r is None:
        log.warning("idle queue: no redis - %s NOT queued", job.get("id"))
        return False
    try:
        await r.hset(REDIS_KEY, job["id"], json.dumps(job, default=str))
        return True
    except Exception as e:
        log.warning("idle queue save %s: %s", job.get("id"), e)
        return False


async def drop_job(job_id: str) -> None:
    r = _redis()
    if r is None:
        return
    try:
        await r.hdel(REDIS_KEY, str(job_id))
    except Exception as e:
        log.debug("idle queue drop %s: %s", job_id, e)


# ── producing ───────────────────────────────────────────────────────────────
async def submit(kind: str, title: str = "", *, dedupe_key: str = "",
                 priority: Optional[int] = None,
                 payload: Optional[Dict[str, Any]] = None,
                 total: Optional[int] = None,
                 jobs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Enqueue work, unless an equivalent job is already waiting.

    Dedupe matters more here than in a normal queue: producers are on timers,
    so an hour of a busy box would otherwise leave twenty identical narrator
    jobs to run back-to-back the moment it went quiet - turning a queue that
    exists to protect the box into a thundering herd aimed at it.
    """
    existing = jobs if jobs is not None else await load_jobs()
    key = dedupe_key or "%s:%s" % (kind, title)
    for j in existing:
        if (j.get("dedupe_key") == key
                and j.get("state") in (_iq.WAITING, _iq.RUNNING)):
            return {"queued": False, "reason": "already queued", "id": j.get("id")}
    jid = "%s:%s" % (kind, uuid.uuid4().hex[:8])
    job = _iq.make_job(jid, kind, title, payload, enqueued_at=time.time())
    job["dedupe_key"] = key
    if priority is not None:
        job["priority"] = int(priority)
    # How much work this is. Optional, and an absent total means "unknown"
    # rather than "none" - the estimator then reports no estimate instead of
    # confidently reporting no work. A producer that can count cheaply should
    # pass it; one that cannot should not invent a number.
    if total is not None:
        job = _iq.with_progress(job, total=total)
    if not await save_job(job):
        return {"queued": False, "reason": "the queue store is unavailable",
                "id": job["id"]}
    return {"queued": True, "id": job["id"], "kind": kind}


# ── the runner ──────────────────────────────────────────────────────────────
async def report_progress(job_id: str, done: Optional[int] = None,
                          total: Optional[int] = None) -> bool:
    """A running handler says how far it has got.

    This is what turns an estimate from a one-shot guess made at enqueue time
    into something that sharpens as the job runs, and it is what lets the panel
    show a half-finished backfill as half-finished rather than as pending.
    Handlers already checkpoint; this asks them to write the number down.

    Best-effort: a failed progress write must never take down the work it was
    describing.
    """
    try:
        for j in await load_jobs():
            if j.get("id") == job_id:
                await save_job(_iq.with_progress(j, done=done, total=total))
                return True
    except Exception as e:                                 # pragma: no cover
        log.debug("idle queue progress %s: %s", job_id, e)
    return False


def _still_running() -> bool:
    t = _RUNNING.get("task")
    return bool(t) and not t.done()


async def _stop_running(reason: str, now: float) -> Optional[Dict[str, Any]]:
    """Ask the in-flight job to stop, cancel it if it will not, re-queue it."""
    job_id = _RUNNING.get("id")
    task = _RUNNING.get("task")
    if not job_id:
        return None
    asked = _RUNNING.get("asked_to_stop_at")
    if asked is None:
        # First notice. The handler sees this through should_continue() and is
        # given PREEMPT_GRACE_S to reach a checkpoint of its own choosing.
        _RUNNING["asked_to_stop_at"] = now
        log.info("idle queue: asking %s to yield - %s", job_id, reason)
        return None
    if now - float(asked) < PREEMPT_GRACE_S:
        return None                                   # still within its grace
    if task and not task.done():
        task.cancel()
        log.warning("idle queue: cancelled %s after %.0fs - %s",
                    job_id, now - float(asked), reason)
    for j in await load_jobs():
        if j.get("id") == job_id:
            back = _iq.preempt(j, reason, now)
            await save_job(back)
            _RUNNING.clear()
            return back
    _RUNNING.clear()
    return None


def _should_continue(job_id: str, busy_probe: Callable[[], Awaitable[str]]):
    """Handed to the handler. Truthy return = stop, and it says why.

    Two ways to be told to stop: the box got busy, or the runner explicitly
    asked this job to yield.
    """
    async def _cb() -> str:
        if _RUNNING.get("id") == job_id and _RUNNING.get("asked_to_stop_at"):
            return "pre-empted by the idle queue"
        try:
            return await busy_probe()
        except Exception:
            return ""
    return _cb


async def _run_job(job: Dict[str, Any],
                   busy_probe: Callable[[], Awaitable[str]]) -> None:
    kind = job.get("kind", "")
    fn = _HANDLERS.get(kind)
    if fn is None:
        log.warning("idle queue: no handler for %s - dropping %s", kind, job["id"])
        await drop_job(job["id"])
        _RUNNING.clear()
        return
    ok, note = True, ""
    try:
        res = await fn(job, _should_continue(job["id"], busy_probe))
        if isinstance(res, dict):
            note = str(res.get("yielded", "") or res.get("note", ""))[:160]
    except asyncio.CancelledError:
        # Pre-emption already re-queued it; do not mark it finished.
        raise
    except Exception as e:
        ok, note = False, str(e)[:160]
        log.warning("idle queue: %s (%s) failed: %s", job["id"], kind, e)
    now = time.time()
    if note and "pre-empted" in note:
        # It yielded on its own. Back on the queue, not finished.
        for j in await load_jobs():
            if j.get("id") == job["id"]:
                await save_job(_iq.preempt(j, note, now))
                break
    else:
        # Learn what it actually cost, but only from a run that FINISHED. A
        # pre-empted or failed run's elapsed time measures the interruption,
        # not the work, and folding it in would teach the estimator that a
        # backfill takes however long the box happened to stay quiet.
        if ok and _eta is not None:
            started = job.get("started_at")
            try:
                elapsed = float(now) - float(started)
            except (TypeError, ValueError):
                elapsed = 0.0
            # What the job actually got through. The handler's own report wins;
            # progress.done is the fallback, and only then 1 - so a rate learned
            # from a large backfill is per RECORD rather than per run, and stays
            # comparable with the next job of a different size.
            items = 1
            if isinstance(res, dict):
                for key in ("items", "records", "processed"):
                    if res.get(key):
                        items = res[key]
                        break
            if items == 1:
                fresh = next((j for j in await load_jobs()
                              if j.get("id") == job["id"]), None)
                done = ((fresh or {}).get("progress") or {}).get("done")
                if done:
                    items = done
            if elapsed > 0:
                try:
                    await save_rates(_eta.observe(await load_rates(), kind,
                                                  elapsed, items))
                except Exception as e:                     # pragma: no cover
                    log.debug("idle queue rate observe: %s", e)
        await drop_job(job["id"])
        log.info("idle queue: %s (%s) %s%s", job["id"], kind,
                 "done" if ok else "FAILED", " - " + note if note else "")
    _RUNNING.clear()


async def drain_once(busy_reason: str,
                     busy_probe: Callable[[], Awaitable[str]],
                     now: Optional[float] = None) -> Dict[str, Any]:
    """One tick. Start at most one job, or take the box back.

    `busy_reason` is the full gate verdict from background_work.quiet_gate -
    busy right now, OR not quiet for long enough. Nothing starts on either,
    because the queue must not fire at all during active use.
    """
    t = float(now if now is not None else time.time())

    # 0. RECONCILE FIRST. The store is durable; the asyncio task is not. A
    #    restart, or a cancellation that never reached the re-queue, leaves a
    #    job marked `running` with nothing running it - and next_job refuses to
    #    start anything while ANY job is running, so one stale record stops the
    #    queue permanently. Observed on prod 2026-09-09: embed.sessions
    #    stranded, runs=0, embed.sources waiting 17.6h behind it.
    #
    #    Every tick, not just at startup: the same thing happens whenever a
    #    runner dies, and a self-healing queue should not need a restart to
    #    notice. This only moves records BACK to waiting - it never starts
    #    anything, so the gate below still decides whether the box is free.
    _live = {_RUNNING["id"]} if (_still_running() and _RUNNING.get("id")) else set()
    for _orphan in _iq.stranded(await load_jobs(), _live):
        await save_job(_iq.requeue_stranded(_orphan, t))
        log.warning("idle queue: requeued %s (%s) - it was marked running with "
                    "no live runner", _orphan.get("id"), _orphan.get("kind"))

    # 1. Activity wins - take the node back before considering anything new.
    #    But only for kinds that CAN be pre-empted: a dream or a narration is
    #    itself the reason the box looks busy, so stopping it on that signal
    #    would cancel our own work moments after starting it. See
    #    idle_queue.PREEMPTIBLE_KINDS.
    if busy_reason and _still_running():
        if _iq.is_preemptible(_RUNNING.get("kind", "")):
            await _stop_running(busy_reason, t)
            return {"action": "preempting", "reason": busy_reason}
        return {"action": "busy", "running": _RUNNING.get("id"),
                "note": "%s is not pre-emptible - letting it finish"
                        % _RUNNING.get("kind", "")}
    if _still_running():
        return {"action": "busy", "running": _RUNNING.get("id")}
    if _RUNNING:
        _RUNNING.clear()               # task finished; clear the slot

    # 2. Otherwise start the next job, if the box has been quiet long enough.
    jobs = await load_jobs()
    nxt = _iq.next_job(jobs, busy_reason)
    if nxt is None:
        return {"action": "idle", "reason": busy_reason or "nothing queued",
                "depth": len(_iq.pending(jobs))}
    if not has_handler(nxt.get("kind", "")):
        await drop_job(nxt["id"])
        return {"action": "dropped", "reason": "no handler for %s" % nxt.get("kind")}
    nxt = dict(nxt)
    nxt["state"] = _iq.RUNNING
    nxt["started_at"] = t
    nxt["attempts"] = int(nxt.get("attempts", 0)) + 1
    await save_job(nxt)
    _RUNNING.clear()
    _RUNNING.update({"id": nxt["id"], "kind": nxt.get("kind", ""),
                     "asked_to_stop_at": None})
    _RUNNING["task"] = asyncio.create_task(_run_job(nxt, busy_probe))
    return {"action": "started", "id": nxt["id"], "kind": nxt.get("kind")}
