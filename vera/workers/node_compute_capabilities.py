"""
node_compute_capabilities.py — model training on the node workers

The runtime half of node_compute_core (read that first for the contract).

  run_on_node(fn, inputs, ...)  the host's side: route, queue, wait, return
                                outputs. Published as `_orch.NODE_COMPUTE` so
                                the training modules reach it without importing
                                this file (cap modules load under bare names).
  compute.run                   the node's side, run by a node worker's loop.
  compute.status / compute.config.set   what runs where, and the host fallback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, emit_event

try:
    from Vera.vera.workers import node_compute_core as core
except ImportError:                                        # pragma: no cover
    from vera.workers import node_compute_core as core     # type: ignore

log = logging.getLogger("vera.compute")


class NodeComputeError(RuntimeError):
    """A job that could not run where it had to: the message says why."""


def _s(v) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v or "")


async def _config() -> Dict[str, Any]:
    r = _orch.REDIS
    raw = None
    if r is not None:
        try:
            raw = await r.get(core.CONFIG_KEY)
        except Exception:
            raw = None
    try:
        return core.merge_config(json.loads(raw) if raw else {})
    except ValueError:
        return core.merge_config({})


async def _workers() -> List[Dict[str, Any]]:
    r = _orch.REDIS
    out: List[Dict[str, Any]] = []
    if r is None:
        return out
    try:
        async for k in r.scan_iter("vera:workers:*"):
            try:
                vals = await r.hmget(k, "id", "role", "status", "classes", "libs", "host",
                                     "current_task", "task_started")
            except Exception:
                continue
            keys = ("id", "role", "status", "classes", "libs", "host", "current_task",
                    "task_started")
            out.append({a: _s(b) for a, b in zip(keys, vals)})
    except Exception as e:
        log.debug("compute workers scan: %s", e)
    return out


def _find_fn(fn: str) -> Callable:
    """The allow-listed function, from a module this process already loaded."""
    spec = core.FUNCTIONS.get(fn)
    if not spec:
        raise NodeComputeError(f"unknown compute function {fn!r}")
    base, attr, _libs = spec
    for name, mod in list(sys.modules.items()):
        if mod is None or name.rsplit(".", 1)[-1] != base:
            continue
        f = getattr(mod, attr, None)
        if callable(f):
            return f
    raise NodeComputeError(f"{base}.{attr} is not loaded in this process")


def _set_threads(n: int) -> None:
    """The training loop's thread count, for libraries already imported."""
    t = sys.modules.get("torch")
    if t is not None:
        try:
            t.set_num_threads(int(n))
        except Exception:
            pass


async def _record(rec: Dict[str, Any]) -> None:
    r = _orch.REDIS
    if r is None:
        return
    try:
        await r.lpush(core.JOBS_KEY, json.dumps(rec, default=str))
        await r.ltrim(core.JOBS_KEY, 0, core.JOBS_KEEP - 1)
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
#  the host's side
# ═════════════════════════════════════════════════════════════════════════════
async def _run_here(fn: str, inputs: Dict[str, Any], threads: int,
                    on_progress, stop) -> Dict[str, Any]:
    func = _find_fn(fn)
    loop = asyncio.get_running_loop()

    def progress(d):
        if on_progress is not None:
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(on_progress(dict(d))))

    def should_stop():
        try:
            return bool(stop and stop())
        except Exception:
            return False

    inputs = dict(inputs, _threads=threads)
    return await loop.run_in_executor(None, func, inputs, progress, should_stop)


async def run_on_node(fn: str, inputs: Dict[str, Any], *, label: str = "",
                      on_progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
                      stop: Optional[Callable[[], bool]] = None,
                      trace_id: str = "") -> Dict[str, Any]:
    """Run an allow-listed training function on a node worker and return its
    outputs. `on_progress` (async) receives each progress dict; `stop()` is
    polled and, once true, asks the node to stop at its next checkpoint.

    The outputs carry `_where` ('node' | 'host'), `_node` and `_seconds`.
    Raises NodeComputeError when the job cannot run, or failed."""
    cfg = await _config()
    r = _orch.REDIS
    workers = await _workers() if r is not None else []
    where, why = core.decide(fn, workers, host_fallback=cfg["host_fallback"],
                             is_sandbox=_orch.is_dev_sandbox())
    if r is None and where == "node":
        where, why = (("host", "no Redis - host fallback is on") if cfg["host_fallback"]
                      else ("refuse", "no Redis connection to reach the nodes"))
    job = uuid.uuid4().hex[:12]
    t0 = time.time()
    base = core.job_record(job, fn, where=where, why=why, label=label,
                           started=_orch.now_iso())
    if where == "refuse":
        await _record({**base, "state": "refused"})
        raise NodeComputeError(f"{fn}: not run - {why}")

    if where == "host":
        await _record({**base, "state": "running"})
        try:
            out = await _run_here(fn, inputs, cfg["threads"], on_progress, stop)
        except Exception as e:
            await _record({**base, "state": "error", "error": str(e)[:300],
                           "seconds": round(time.time() - t0, 1)})
            raise
        sec = round(time.time() - t0, 1)
        await _record({**base, "state": "done", "node": socket.gethostname(), "seconds": sec})
        return {**(out or {}), "_where": "host", "_node": socket.gethostname(), "_seconds": sec}

    # ── node ─────────────────────────────────────────────────────────────────
    blob = core.pack(dict(inputs, _threads=cfg["threads"]))
    await r.set(core.in_key(job), blob, ex=core.TTL_IN)
    rec = {"id": job, "capability": core.RUN_CAP,
           "payload": json.dumps({"job": job, "fn": fn}),
           "trace_id": trace_id or job, "ts": _orch.now_iso()}
    msg_id = await r.xadd(core.NODE_COMPUTE_STREAM, rec, maxlen=1000, approximate=True)
    await _record({**base, "state": "queued", "bytes_in": len(blob)})
    await emit_event({"type": "compute.queued", "job": job, "fn": fn, "label": label,
                      "bytes": len(blob)})

    seen_prog = 0
    hb_seen_at = 0.0
    stop_sent = False
    try:
        while True:
            await asyncio.sleep(1.5)
            # progress first, so the last epoch's event lands before "done"
            try:
                items = await r.lrange(core.prog_key(job), seen_prog, -1)
            except Exception:
                items = []
            for it in items:
                seen_prog += 1
                if on_progress is not None:
                    try:
                        await on_progress(json.loads(it))
                    except Exception as e:
                        log.debug("compute progress handler: %s", e)
            raw_done = await r.get(core.done_key(job))
            if raw_done:
                done = json.loads(raw_done)
                if not done.get("ok"):
                    raise NodeComputeError(f"{fn} failed on {done.get('node') or 'a node'}: "
                                           f"{done.get('error') or 'unknown error'}")
                raw_out = await r.get(core.out_key(job))
                if raw_out is None:
                    raise NodeComputeError(f"{fn}: the node's outputs expired before they were read")
                out = core.unpack(raw_out)
                sec = round(time.time() - t0, 1)
                await _record({**base, "state": "done", "node": done.get("node", ""),
                               "seconds": sec, "run_seconds": done.get("seconds"),
                               "bytes_out": len(raw_out)})
                return {**(out or {}), "_where": "node", "_node": done.get("node", ""),
                        "_seconds": sec}
            if stop and not stop_sent:
                try:
                    if stop():
                        await r.set(core.stop_key(job), "1", ex=core.TTL_OUT)
                        stop_sent = True
                except Exception:
                    pass
            hb = await r.get(core.hb_key(job))
            now = time.time()
            if hb:
                hb_seen_at = now
            alive = len(core.compute_workers(await _workers()))
            verdict, vwhy = core.liveness(now=now, dispatched_at=t0, hb_seen_at=hb_seen_at,
                                          hb_alive=bool(hb), workers_alive=alive)
            if verdict != "wait":
                raise NodeComputeError(f"{fn}: {vwhy}")
    except BaseException as e:
        # never leave a job behind for a worker to run after its caller gave up
        try:
            await r.xdel(core.NODE_COMPUTE_STREAM, msg_id)
            await r.set(core.stop_key(job), "1", ex=core.TTL_OUT)
            await r.delete(core.in_key(job))
        except Exception:
            pass
        await _record({**base, "state": "error", "error": str(e)[:300] or type(e).__name__,
                       "seconds": round(time.time() - t0, 1)})
        raise
    finally:
        try:
            await r.delete(core.prog_key(job), core.out_key(job))
        except Exception:
            pass


# Reached by the training modules through the orchestrator module.
_orch.NODE_COMPUTE = run_on_node
_orch.NodeComputeError = NodeComputeError


# ═════════════════════════════════════════════════════════════════════════════
#  the node's side
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "compute.run",
    memory="off", silent=True,
    description="Node worker side of a training job the Vera host queued: reads "
                "the job's inputs from Redis, runs the allow-listed function "
                "(worldview.train | ml.train | markets.ml.train) and writes its "
                "outputs back. Inputs: job (str), fn (str). Not called directly - "
                "the host queues it on the node-only compute stream.",
)
async def cap_compute_run(job: str = "", fn: str = "", trace_id=None) -> Dict:
    r = _orch.REDIS
    node = socket.gethostname()
    t0 = time.time()

    async def finish(ok: bool, error: str = "", **extra) -> Dict:
        rec = {"ok": ok, "error": error, "node": node,
               "seconds": round(time.time() - t0, 1), **extra}
        try:
            await r.set(core.done_key(job), json.dumps(rec), ex=core.TTL_OUT)
            await r.delete(core.in_key(job), core.hb_key(job))
        except Exception:
            pass
        return rec

    if r is None or not job:
        return {"ok": False, "error": "no Redis or no job"}
    if await r.get(core.stop_key(job)):
        return await finish(False, "stopped before it started")
    need = core.requirements(fn)
    libs = core.available_libs()
    missing = [m for m in need if not libs.get(m)]
    if missing:
        return await finish(False, f"{node} has no {', '.join(missing)}")
    blob = await r.get(core.in_key(job))
    if blob is None:
        return await finish(False, "inputs expired or were withdrawn")
    try:
        func = _find_fn(fn)
        inputs = core.unpack(blob)
    except Exception as e:
        return await finish(False, str(e)[:300])
    del blob

    stop_ev = threading.Event()
    pending: List[str] = []
    lock = threading.Lock()

    def progress(d):
        with lock:
            pending.append(json.dumps(d, default=str))

    async def flush():
        with lock:
            batch = pending[:]
            pending.clear()
        if batch:
            await r.rpush(core.prog_key(job), *batch)
            await r.expire(core.prog_key(job), core.TTL_OUT)

    async def beat():
        while True:
            try:
                await r.set(core.hb_key(job), str(time.time()), ex=core.HEARTBEAT_LOST)
                await flush()
                if await r.get(core.stop_key(job)):
                    stop_ev.set()
            except Exception as e:
                log.debug("compute heartbeat: %s", e)
            await asyncio.sleep(core.HEARTBEAT_EVERY)

    _set_threads(int(inputs.get("_threads") or 4))
    hb = asyncio.create_task(beat())
    try:
        out = await asyncio.get_running_loop().run_in_executor(
            None, func, inputs, progress, stop_ev.is_set)
    except Exception as e:
        log.warning("compute %s %s failed: %s", fn, job, e, exc_info=True)
        hb.cancel()
        try:
            await flush()
        except Exception:
            pass
        return await finish(False, f"{type(e).__name__}: {e}"[:300])
    hb.cancel()
    try:
        await flush()
        packed = core.pack(out or {})
        await r.set(core.out_key(job), packed, ex=core.TTL_OUT)
    except Exception as e:
        return await finish(False, f"outputs could not be stored: {e}"[:300])
    return await finish(True, bytes_out=len(packed))


# ═════════════════════════════════════════════════════════════════════════════
#  status + config
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "compute.status",
    http_method="GET", http_path="/compute/status", http_tags=["compute", "workers"],
    memory="off", silent=True,
    description="Model training on the nodes: the config (host_fallback, threads), "
                "each node worker that takes training jobs with its libraries and "
                "current task, the functions that can run there, and the recent "
                "jobs (where each ran, why, how long). Output: {config, workers, "
                "functions, jobs}.",
)
async def cap_compute_status(trace_id=None) -> Dict:
    cfg = await _config()
    ws = core.compute_workers(await _workers())
    jobs: List[Dict[str, Any]] = []
    r = _orch.REDIS
    if r is not None:
        try:
            # newest record per job (a job is recorded at each state change)
            seen = set()
            for raw in await r.lrange(core.JOBS_KEY, 0, core.JOBS_KEEP - 1):
                try:
                    j = json.loads(raw)
                except ValueError:
                    continue
                if j.get("job") in seen:
                    continue
                seen.add(j.get("job"))
                jobs.append(j)
        except Exception:
            pass
    funcs = {}
    for name, (_m, _a, libs) in core.FUNCTIONS.items():
        where, why = core.decide(name, ws, host_fallback=cfg["host_fallback"],
                                 is_sandbox=_orch.is_dev_sandbox())
        funcs[name] = {"needs": list(libs), "would_run": where, "why": why}
    return {"config": cfg,
            "workers": [{"id": w.get("id"), "host": w.get("host"), "status": w.get("status"),
                         "libs": json.loads(w["libs"]) if w.get("libs") else {},
                         "current_task": w.get("current_task")} for w in ws],
            "functions": funcs, "jobs": jobs}


@capability(
    "compute.config.set",
    http_method="POST", http_path="/compute/config", http_tags=["compute", "workers"],
    memory="off",
    description="Set how model training is placed. Inputs: host_fallback (bool - "
                "run a job on the Vera host when no node worker can take it; off "
                "by default), threads (int 1-32 - threads one training job uses on "
                "a node, default 4). Output: {ok, config}.",
)
async def cap_compute_config_set(host_fallback: Optional[bool] = None,
                                 threads: Optional[int] = None, trace_id=None) -> Dict:
    r = _orch.REDIS
    if r is None:
        return {"ok": False, "error": "no Redis"}
    cfg = await _config()
    if host_fallback is not None:
        cfg["host_fallback"] = host_fallback
    if threads is not None:
        cfg["threads"] = threads
    cfg = core.merge_config(cfg)
    await r.set(core.CONFIG_KEY, json.dumps(cfg))
    await emit_event({"type": "compute.config", **cfg})
    return {"ok": True, "config": cfg}
