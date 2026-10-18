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


_WORKER_FIELDS = ("id", "role", "status", "classes", "libs", "host", "host_id",
                  "current_task", "task_started", "cpu_pct", "ncpu", "mem_avail_gb",
                  "ram_total_gb", "ram_used_gb", "load1", "metrics_at",
                  # several jobs per worker (MW-7): admission reads these - left
                  # out, every worker fell back to one job at a time (2026-10-11)
                  "compute_slots", "compute_running")


async def _workers() -> List[Dict[str, Any]]:
    """Every registered worker, with its node's last metrics, and the node's
    test venv state merged into `libs` as `testenv` (current = matches the
    gate image the host runs now)."""
    r = _orch.REDIS
    out: List[Dict[str, Any]] = []
    if r is None:
        return out
    try:
        async for k in r.scan_iter("vera:workers:*"):
            try:
                vals = await r.hmget(k, *_WORKER_FIELDS)
            except Exception:
                continue
            out.append({a: _s(b) for a, b in zip(_WORKER_FIELDS, vals)})
    except Exception as e:
        log.debug("compute workers scan: %s", e)
    try:
        want = _s(await r.get(core.TESTENV_WANT_KEY))
        envs = {_s(k): json.loads(v) for k, v in (await r.hgetall(core.TESTENV_KEY)).items()}
    except Exception:
        want, envs = "", {}
    for w in out:
        if not w.get("libs"):
            continue
        try:
            libs = json.loads(w["libs"])
        except ValueError:
            continue
        env = envs.get(w.get("host_id") or "") or {}
        libs["testenv"] = bool(want) and env.get("ok") is True and env.get("sha") == want
        w["libs"] = json.dumps(libs)
    return out


def _find_fn(fn: str) -> Callable:
    """The allow-listed function, from a module this process already loaded -
    or, for the few that are not capability modules, imported by the fixed
    name in core.IMPORTS (never a name from the wire)."""
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
    if base in core.IMPORTS:
        import importlib
        f = getattr(importlib.import_module(core.IMPORTS[base]), attr, None)
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

    inputs = dict(inputs, _threads=core.threads_for(fn, threads))
    if inputs.get("_blob_refs") and _orch.REDIS is not None:
        err = await core.resolve_blob_refs(inputs, _orch.REDIS.get)
        if err:
            raise NodeComputeError(f"{fn}: {err}")
    return await loop.run_in_executor(None, func, inputs, progress, should_stop)


#: What this process has sent to each node and not heard back about yet, by
#: host_id: the node's metrics lag a job by up to a tick, so admission counts
#: these on top of what the node reports.
_RESERVED: Dict[str, Dict[str, float]] = {}


def _reserve(hid: str, fn: str, sign: int, entry: Optional[list] = None) -> list:
    """+1: count a job sent to `hid` (returns its entry); -1 with that entry:
    it finished. `sent` keeps each job's send time and size, so admission
    stops counting a job once the node's metrics show it (MW-7)."""
    need = core.NEEDS.get(fn, {"cpu": 1.0, "mem_gb": 1.0})
    cur = _RESERVED.setdefault(hid, {"cpu": 0.0, "mem_gb": 0.0, "jobs": 0.0, "sent": []})
    cur.setdefault("sent", [])
    for k in ("cpu", "mem_gb"):
        cur[k] = max(0.0, cur[k] + sign * need[k])
    cur["jobs"] = max(0.0, cur.get("jobs", 0.0) + sign)
    if sign > 0:
        entry = [time.time(), float(need["cpu"]), float(need["mem_gb"])]
        cur["sent"].append(entry)
        return entry
    if entry is not None:
        try:
            cur["sent"].remove(entry)
        except ValueError:
            pass
    return entry or []


def _avoided(pick: Dict[str, Any], avoid) -> bool:
    """Whether a pick is one of `avoid` (node names or host ids, any case)."""
    want = {str(a).strip().lower() for a in avoid or () if str(a).strip()}
    if not want:
        return False
    return bool(want & {str(pick.get(k) or "").strip().lower() for k in ("host", "host_id")} - {""})


async def node_plan(fn: str, exclude=()) -> Dict[str, Any]:
    """Where `fn` would run right now (see core.plan)."""
    cfg = await _config()
    return core.plan(fn, await _workers(), cfg, reserved=_RESERVED, now=time.time(),
                     exclude=exclude)


_SERIAL_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) end return 0")


async def run_on_node(fn: str, inputs: Dict[str, Any], *, label: str = "",
                      on_progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
                      stop: Optional[Callable[[], bool]] = None,
                      trace_id: str = "", max_wait_s: Optional[int] = None,
                      host_fallback: Optional[bool] = None,
                      serial: str = "", avoid=()) -> Dict[str, Any]:
    """_run_on_node, optionally inside a cluster-wide one-at-a-time slot.

    `serial` names the slot (e.g. "training"): of all the jobs - on every
    host process - that name the same slot, one runs at a time. The slot is a
    Redis key the holder refreshes while its job runs (TTL core.SERIAL_TTL_S),
    so a host that dies holding it frees it on its own. Waiting for the slot
    counts toward max_wait_s."""
    r = _orch.REDIS
    name = core.serial_name(serial)
    if serial and not name:
        raise NodeComputeError(f"{fn}: bad serial slot name {serial!r}")
    if not name or r is None:
        return await _run_on_node(fn, inputs, label=label, on_progress=on_progress, stop=stop,
                                  trace_id=trace_id, max_wait_s=max_wait_s,
                                  host_fallback=host_fallback, avoid=avoid)
    cfg = await _config()
    cap = cfg["max_wait_s"] if max_wait_s is None else max(0, int(max_wait_s))
    key, token, t0 = core.serial_key(name), uuid.uuid4().hex, time.time()
    noted = False
    while not await r.set(key, token, nx=True, ex=core.SERIAL_TTL_S):
        if stop and stop():
            raise NodeComputeError(f"{fn}: stopped while waiting for the '{name}' slot")
        if time.time() - t0 >= cap:
            holder = _s(await r.get(key))
            raise NodeComputeError(f"{fn}: the '{name}' slot stayed taken for "
                                   f"{int(time.time() - t0)} s (held by {holder or 'another job'})")
        if not noted:
            noted = True
            await emit_event({"type": "compute.waiting", "fn": fn, "label": label,
                              "why": f"waiting for the '{name}' slot"})
        await asyncio.sleep(5)

    async def keep():
        while True:
            await asyncio.sleep(core.SERIAL_TTL_S / 3)
            try:
                if _s(await r.get(key)) == token:
                    await r.expire(key, core.SERIAL_TTL_S)
            except Exception:
                pass
    keeper = asyncio.create_task(keep())
    try:
        left = max(0, int(cap - (time.time() - t0)))
        return await _run_on_node(fn, inputs, label=label, on_progress=on_progress, stop=stop,
                                  trace_id=trace_id, max_wait_s=left,
                                  host_fallback=host_fallback, avoid=avoid)
    finally:
        keeper.cancel()
        try:
            await r.eval(_SERIAL_RELEASE_LUA, 1, key, token)
        except Exception:
            pass


async def _run_on_node(fn: str, inputs: Dict[str, Any], *, label: str = "",
                       on_progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
                       stop: Optional[Callable[[], bool]] = None,
                       trace_id: str = "", max_wait_s: Optional[int] = None,
                       host_fallback: Optional[bool] = None, avoid=()) -> Dict[str, Any]:
    """Run an allow-listed function on the node worker with the most room for
    it and return its outputs. `on_progress` (async) receives each progress
    dict; `stop()` is polled and, once true, asks the node to stop at its next
    checkpoint.

    Scheduling: the job goes to one node whose measured free CPU and memory,
    less the node's reserve and what this host already sent it, cover the
    function's need (core.NEEDS). While no node has room it waits, up to
    `max_wait_s` (config max_wait_s); then it runs on the host if host
    fallback is on, else fails saying why. A node that loses the job (it never
    started it, or its heartbeat stopped) is excluded and the job is planned
    once more.

    The outputs carry `_where` ('node' | 'host'), `_node` and `_seconds`.
    Raises NodeComputeError when the job cannot run, or failed."""
    cfg = await _config()
    if host_fallback is not None:
        cfg["host_fallback"] = bool(host_fallback)
    wait_cap = cfg["max_wait_s"] if max_wait_s is None else max(0, int(max_wait_s))
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

    async def on_host(reason: str) -> Dict[str, Any]:
        rec = {**base, "where": "host", "why": reason}
        await _record({**rec, "state": "running"})
        try:
            out = await _run_here(fn, inputs, cfg["threads"], on_progress, stop)
        except Exception as e:
            await _record({**rec, "state": "error", "error": str(e)[:300],
                           "seconds": round(time.time() - t0, 1)})
            raise
        sec = round(time.time() - t0, 1)
        await _record({**rec, "state": "done", "node": socket.gethostname(), "seconds": sec})
        return {**(out or {}), "_where": "host", "_node": socket.gethostname(), "_seconds": sec}

    if where == "host":
        return await on_host(why)

    # ── node ─────────────────────────────────────────────────────────────────
    blob = core.pack(dict(inputs, _threads=core.threads_for(fn, cfg["threads"])))
    exclude: List[str] = []
    last_err = ""
    for attempt in range(2):
        # wait for a node with room
        waited_note = ""
        while True:
            p = await node_plan(fn, exclude=exclude)
            if p["pick"] is not None and _avoided(p["pick"], avoid):
                # a node the caller leaves alone (a gate beside a census: compute-roles K)
                exclude.append(str(p["pick"].get("host_id")))
                continue
            if p["pick"] is not None:
                break
            if stop and stop():
                await _record({**base, "state": "stopped", "why": "stopped while waiting"})
                raise NodeComputeError(f"{fn}: stopped while waiting for a node")
            if time.time() - t0 >= wait_cap:
                reason = f"no node had room for {int(time.time() - t0)} s: {p['why']}"
                if cfg["host_fallback"]:
                    return await on_host(reason + " - host fallback is on")
                await _record({**base, "state": "refused", "why": reason})
                raise NodeComputeError(f"{fn}: not run - {reason}")
            if p["why"] != waited_note:
                waited_note = p["why"]
                await _record({**base, "state": "waiting", "why": waited_note})
                await emit_event({"type": "compute.waiting", "job": job, "fn": fn,
                                  "label": label, "why": waited_note})
            await asyncio.sleep(5)
        w = p["pick"]
        hid, node = str(w.get("host_id")), str(w.get("host") or "")
        # reserve in the same step as the pick (no await between): jobs planned
        # side by side - a gate's shards - then see it and pick other nodes
        # (2026-10-04: two of a gate's three shards picked the same node)
        _entry = _reserve(hid, fn, +1)
        try:
            out = await _dispatch_and_wait(r, job, fn, blob, hid, node, base, t0,
                                           on_progress, stop, trace_id, attempt)
            return out
        except _Lost as e:
            exclude.append(hid)
            last_err = str(e)
            await _record({**base, "state": "retrying", "node": node, "why": last_err})
            job = uuid.uuid4().hex[:12]
            base = {**base, "job": job}
        finally:
            _reserve(hid, fn, -1, _entry)
    await _record({**base, "state": "error", "error": last_err[:300],
                   "seconds": round(time.time() - t0, 1)})
    raise NodeComputeError(f"{fn}: {last_err} (twice)")


class _Lost(Exception):
    """The chosen node did not run the job to the end: plan it elsewhere."""


async def _dispatch_and_wait(r, job, fn, blob, hid, node, base, t0, on_progress, stop,
                             trace_id, attempt) -> Dict[str, Any]:
    await r.set(core.in_key(job), blob, ex=core.TTL_IN)
    rec = {"id": job, "capability": core.RUN_CAP,
           "payload": json.dumps({"job": job, "fn": fn}),
           "trace_id": trace_id or job, "ts": _orch.now_iso()}
    stream = core.node_stream(hid)
    msg_id = await r.xadd(stream, rec, maxlen=1000, approximate=True)
    sent_at = time.time()
    await _record({**base, "state": "queued", "node": node, "bytes_in": len(blob),
                   "attempt": attempt + 1})
    await emit_event({"type": "compute.queued", "job": job, "fn": fn, "node": node,
                      "bytes": len(blob)})
    seen_prog = 0
    hb_seen_at = 0.0
    stop_sent = False
    finished = False
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
                finished = True
                done = json.loads(raw_done)
                if not done.get("ok"):
                    raise NodeComputeError(f"{fn} failed on {done.get('node') or node}: "
                                           f"{done.get('error') or 'unknown error'}")
                raw_out = await r.get(core.out_key(job))
                if raw_out is None:
                    raise NodeComputeError(f"{fn}: the node's outputs expired before they were read")
                out = core.unpack(raw_out)
                sec = round(time.time() - t0, 1)
                await _record({**base, "state": "done", "node": done.get("node", node),
                               "seconds": sec, "run_seconds": done.get("seconds"),
                               "bytes_out": len(raw_out)})
                return {**(out or {}), "_where": "node", "_node": done.get("node", node),
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
            elif hb_seen_at and now - hb_seen_at > core.HEARTBEAT_LOST:
                raise _Lost(f"{node} stopped answering (no heartbeat for "
                            f"{int(now - hb_seen_at)} s)")
            elif not hb_seen_at and now - sent_at > core.QUEUE_ORPHAN:
                raise _Lost(f"{node} did not start it within {core.QUEUE_ORPHAN} s")
    except BaseException as e:
        # never leave a job behind for a worker to run after its caller gave up
        if not finished:
            try:
                await r.xdel(stream, msg_id)
                await r.set(core.stop_key(job), "1", ex=core.TTL_OUT)
                await r.delete(core.in_key(job))
            except Exception:
                pass
        if not isinstance(e, _Lost):
            await _record({**base, "state": "error", "node": node,
                           "error": str(e)[:300] or type(e).__name__,
                           "seconds": round(time.time() - t0, 1)})
        raise
    finally:
        try:
            await r.delete(core.prog_key(job), core.out_key(job))
        except Exception:
            pass


# Reached by the training modules through the orchestrator module.
_orch.NODE_COMPUTE = run_on_node
_orch.NODE_PLAN = node_plan
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
    # only importable libraries are checked here; a requirement that is not
    # one (`testenv`, a venv on disk) is checked by the function itself - this
    # check refused every gate shard before it ran (2026-10-04)
    missing = core.node_missing(fn, libs)
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
    # inputs too big to send with every job (a gate's code archive, shared by
    # its chunks) are stored once in Redis by the caller and named here
    err = await core.resolve_blob_refs(inputs, r.get)
    if err:
        return await finish(False, err)

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
    contract={"presentation": {"kind": "composite", "parts": [{"kind": "table", "form": "table", "map": {"rows": "workers"}, "draw": {"columns": ["host", "status", "compute_slots", "compute_running", "current_task"]}, "title": "Workers", "empty": "No workers"}, {"kind": "table", "form": "table", "map": {"rows": "functions"}, "draw": {"columns": ["name", "would_run", "why", "next_node"]}, "title": "Functions", "empty": "No functions"}, {"kind": "table", "form": "table", "map": {"rows": "jobs"}, "draw": {"columns": ["fn", "label", "state", "node", "started", "attempt"]}, "title": "Recent jobs", "empty": "No recent jobs"}, {"kind": "kv", "map": {"fields": "config"}, "title": "Config"}]}},
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
    now = time.time()
    for name, (_m, _a, libs) in core.FUNCTIONS.items():
        where, why = core.decide(name, ws, host_fallback=cfg["host_fallback"],
                                 is_sandbox=_orch.is_dev_sandbox())
        row = {"needs": list(libs), "resources": core.NEEDS.get(name, {}),
               "would_run": where, "why": why}
        if where == "node":
            p = core.plan(name, ws, cfg, reserved=_RESERVED, now=now)
            row["room_now"] = p["pick"] is not None
            row["next_node"] = (p["pick"] or {}).get("host", "")
            row["nodes"] = p["nodes"]
            if p["pick"] is None:
                row["why"] = "waits: " + p["why"]
        funcs[name] = row
    return {"config": cfg,
            "workers": [{"id": w.get("id"), "host": w.get("host"), "host_id": w.get("host_id"),
                         "status": w.get("status"),
                         "libs": json.loads(w["libs"]) if w.get("libs") else {},
                         "capacity": core.capacity(w),
                         "reserved": {k: v for k, v in _RESERVED.get(w.get("host_id") or "", {}).items()
                                      if k != "sent"},
                         "compute_slots": w.get("compute_slots") or "",
                         "compute_running": w.get("compute_running") or "",
                         "current_task": w.get("current_task")} for w in ws],
            "functions": funcs, "jobs": jobs}


@capability(
    "compute.config.set",
    http_method="POST", http_path="/compute/config", http_tags=["compute", "workers"],
    memory="off",
    description="Set how node jobs (training, test shards) are placed. Inputs: "
                "host_fallback (bool - run a job on the Vera host when no node worker "
                "can take it; off by default), threads (int 1-32 - threads one training "
                "job uses on a node, default 4), reserve_cpu (float cores, default 2) and "
                "reserve_mem_gb (float, default 8) - what each node always keeps for its "
                "own services, a job is admitted only on top of it; max_wait_s (int, "
                "default 900 - how long a job waits for a node with room). "
                "Output: {ok, config}.",
)
async def cap_compute_config_set(host_fallback: Optional[bool] = None,
                                 threads: Optional[int] = None,
                                 reserve_cpu: Optional[float] = None,
                                 reserve_mem_gb: Optional[float] = None,
                                 max_wait_s: Optional[int] = None, trace_id=None) -> Dict:
    r = _orch.REDIS
    if r is None:
        return {"ok": False, "error": "no Redis"}
    cfg = await _config()
    if host_fallback is not None:
        cfg["host_fallback"] = host_fallback
    for k, v in (("threads", threads), ("reserve_cpu", reserve_cpu),
                 ("reserve_mem_gb", reserve_mem_gb), ("max_wait_s", max_wait_s)):
        if v is not None:
            cfg[k] = v
    cfg = core.merge_config(cfg)
    await r.set(core.CONFIG_KEY, json.dumps(cfg))
    await emit_event({"type": "compute.config", **cfg})
    return {"ok": True, "config": cfg}
