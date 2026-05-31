"""
job_persistence.py — Persistent Job Tracking & Orphan Recovery
================================================================
Solves: job state lost on server reboot, orphaned stream entries,
no visibility into what Ollama was processing when the server died.

Architecture:
  • Subscribes to vera:events pub/sub (already emitted by ollama_generate,
    worker_loop, and the @capability decorator)
  • Persists every job state transition into Redis hashes (vera:jobs:{id})
    with a 7-day TTL and a sorted-set index (vera:jobs_idx)
  • On startup, scans XPENDING for orphaned stream entries and reclaims them
  • Enriches /cluster/jobs with a `history` key containing persisted records
  • Wires _pg_archive() (existing dead code) into the completion path
  • Provides /jobs/history, /jobs/stats, /jobs/ollama_log endpoints that
    the existing workers_ollama_panel.html can call

Does NOT create a separate panel — the existing Jobs pane gains a
"History" tab and "Recovered" badges through the panel patch.

Add to _module_files in capability_orchestration.py:
    os.path.join(_here, "job_persistence.py"),
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.responses import JSONResponse

from Vera.Orchestration.capability_orchestration import (
    APP,
    REDIS,
    PG_POOL,
    TASK_STREAM,
    RESULT_STREAM,
    GROUP_WORKERS,
    WORKER_REGISTRY,
    CAPABILITY_REGISTRY,
    capability,
    emit_event,
    now_iso,
    schedule,
)
import Vera.Orchestration.capability_orchestration as _orch

log = logging.getLogger("vera.job_persist")

# ── Config ────────────────────────────────────────────────────────────────────
JOB_TTL          = int(os.getenv("VERA_JOB_TTL", 86400 * 7))        # 7 days
JOB_IDX_MAX      = int(os.getenv("VERA_JOB_IDX_MAX", 5000))
RECOVERY_IDLE_MS = int(os.getenv("VERA_RECOVERY_IDLE_MS", 120_000))  # 2 min
ARCHIVE_TO_PG    = os.getenv("VERA_JOB_ARCHIVE_PG", "1") == "1"

# Redis keys
K_JOB     = "vera:jobs:"        # hash per job
K_IDX     = "vera:jobs_idx"     # sorted set: score=timestamp, member=id
K_STATS   = "vera:jobs_stats"   # aggregate counters
K_BOOT    = "vera:boot_id"
K_OLLAMA  = "vera:ollama_log"   # hash of recent ollama requests

_boot_id = ""

# ── Local ring buffer (fallback if Redis is slow / down) ─────────────────────
_LOCAL: List[dict] = []
_LOCAL_MAX = 500
_RUNNING: Dict[str, dict] = {}  # id → {start, cap, worker, ...}


# ─────────────────────────────────────────────────────────────────────────────
#  PERSIST: write/update a job record in Redis + optional PG
# ─────────────────────────────────────────────────────────────────────────────

async def _persist(
    job_id: str,
    cap: str,
    status: str,
    *,
    worker_id: str = "",
    trace_id: str = "",
    instance_id: str = "",
    model: str = "",
    caller_file: str = "",
    caller_func: str = "",
    cap_name: str = "",
    prompt_preview: str = "",
    error: str = "",
    elapsed_s: float = 0.0,
    extra: Optional[dict] = None,
):
    now = time.time()
    rec = {
        "id":             job_id,
        "capability":     cap,
        "status":         status,
        "worker_id":      worker_id,
        "trace_id":       trace_id,
        "instance_id":    instance_id,
        "model":          model,
        "caller_file":    caller_file,
        "caller_func":    caller_func,
        "cap_name":       cap_name,
        "prompt_preview": (prompt_preview or "")[:300],
        "error":          (error or "")[:500],
        "elapsed_s":      str(round(elapsed_s, 2)),
        "updated_at":     now_iso(),
        "updated_ts":     str(now),
        "boot_id":        _boot_id,
    }
    if extra:
        for k, v in extra.items():
            rec[k] = str(v) if not isinstance(v, str) else v

    # Local buffer
    _LOCAL.append(rec)
    if len(_LOCAL) > _LOCAL_MAX:
        del _LOCAL[:-_LOCAL_MAX]

    r = _orch.REDIS
    if not r:
        return

    try:
        key = f"{K_JOB}{job_id}"
        existing = await r.hgetall(key)
        if existing:
            # Preserve created_at
            for field in (b"created_at", b"created_ts"):
                val = existing.get(field, b"")
                if val:
                    fname = field.decode() if isinstance(field, bytes) else field
                    rec[fname] = val.decode() if isinstance(val, bytes) else str(val)
            # Preserve caller info if the current record doesn't have it
            # (the done/error events don't always carry caller info)
            for field in (b"caller_file", b"caller_func", b"cap_name",
                          b"prompt_preview", b"instance_id", b"model"):
                fname = field.decode() if isinstance(field, bytes) else field
                if not rec.get(fname):
                    val = existing.get(field, b"")
                    if val:
                        rec[fname] = val.decode() if isinstance(val, bytes) else str(val)
        else:
            rec["created_at"] = rec["updated_at"]
            rec["created_ts"] = rec["updated_ts"]

        await r.hset(key, mapping=rec)
        await r.expire(key, JOB_TTL)
        await r.zadd(K_IDX, {job_id: now})

        # Stats
        if status in ("done", "failed", "orphan_reclaimed"):
            await r.hincrby(K_STATS, f"total_{status}", 1)
            await r.hincrby(K_STATS, f"{status}:{cap}", 1)

    except Exception as e:
        log.debug("persist redis: %s", e)

    # PG archive on terminal states
    if ARCHIVE_TO_PG and status in ("done", "failed") and _orch.PG_POOL:
        try:
            async with _orch.PG_POOL.acquire() as conn:
                await conn.execute(
                    "INSERT INTO vera_task_results(task_id, result, ts) "
                    "VALUES($1, $2::jsonb, NOW()) ON CONFLICT DO NOTHING",
                    job_id, json.dumps(rec),
                )
        except Exception as e:
            log.debug("persist pg: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
#  EVENT LISTENER: subscribe to vera:events, track ALL job-like events
# ─────────────────────────────────────────────────────────────────────────────

async def _event_listener():
    """Subscribe to vera:events and persist every job state change."""
    r = _orch.REDIS
    if not r:
        while not _orch.REDIS:
            await asyncio.sleep(2)
        r = _orch.REDIS

    ps = r.pubsub()
    await ps.subscribe("vera:events")
    log.info("job_persist: event listener active")

    async for msg in ps.listen():
        if msg["type"] != "message":
            continue
        try:
            ev = json.loads(msg["data"])
        except Exception:
            continue

        etype = ev.get("type", "")

        try:
            # ── Worker stream jobs (distributed mode) ──
            if etype == "worker.start":
                tid = ev.get("task", "")
                if tid:
                    _RUNNING[tid] = {"start": time.time(), "cap": ev.get("capability", ""),
                                     "worker": ev.get("worker", "")}
                    await _persist(tid, ev.get("capability", ""), "running",
                                   worker_id=ev.get("worker", ""))

            elif etype == "worker.done":
                tid = ev.get("task", "")
                run = _RUNNING.pop(tid, {})
                elapsed = time.time() - run.get("start", time.time())
                await _persist(tid, run.get("cap", ""), "done",
                               worker_id=ev.get("worker", ""), elapsed_s=elapsed)

            elif etype == "worker.error":
                tid = ev.get("task", "")
                run = _RUNNING.pop(tid, {})
                elapsed = time.time() - run.get("start", time.time())
                await _persist(tid, run.get("cap", ev.get("capability", "")), "failed",
                               worker_id=ev.get("worker", ""),
                               error=ev.get("error", ""), elapsed_s=elapsed)

            # ── Ollama requests (ALL traffic, not just distributed) ──
            # These are the critical ones for diagnosing the mystery GPU load.
            elif etype == "ollama.request":
                rid = ev.get("req_id", "")
                if rid:
                    _RUNNING[rid] = {"start": time.time()}
                    await _persist(
                        rid, "ollama.generate", "running",
                        instance_id=ev.get("instance_id", ""),
                        model=ev.get("model", ""),
                        caller_file=ev.get("caller_file", ""),
                        caller_func=ev.get("caller_func", ""),
                        cap_name=ev.get("cap_name", ""),
                        prompt_preview=ev.get("prompt_preview", ""),
                        extra={"prefer_gpu": str(ev.get("prefer_gpu", "")),
                               "streaming": str(ev.get("streaming", ""))},
                    )
                    # Also persist to the dedicated ollama log hash
                    try:
                        await r.hset(K_OLLAMA, rid, json.dumps({
                            "req_id": rid,
                            "model": ev.get("model", ""),
                            "instance_id": ev.get("instance_id", ""),
                            "instance_url": ev.get("instance_url", ""),
                            "caller_file": ev.get("caller_file", ""),
                            "caller_func": ev.get("caller_func", ""),
                            "cap_name": ev.get("cap_name", ""),
                            "prompt_preview": ev.get("prompt_preview", ""),
                            "prefer_gpu": ev.get("prefer_gpu", False),
                            "ts": time.time(),
                            "status": "running",
                        }))
                    except Exception:
                        pass

            elif etype == "ollama.request_done":
                rid = ev.get("req_id", "")
                run = _RUNNING.pop(rid, {})
                elapsed = ev.get("elapsed_s", time.time() - run.get("start", time.time()))
                await _persist(
                    rid, "ollama.generate", "done",
                    instance_id=ev.get("instance_id", ""),
                    model=ev.get("model", ""),
                    caller_file=ev.get("caller_file", ""),
                    caller_func=ev.get("caller_func", ""),
                    elapsed_s=float(elapsed),
                    extra={"eval_count": str(ev.get("eval_count", "")),
                           "token_count": str(ev.get("token_count", ""))},
                )
                # Update ollama log entry
                try:
                    raw = await r.hget(K_OLLAMA, rid)
                    if raw:
                        entry = json.loads(raw)
                        entry.update({"status": "done", "elapsed_s": elapsed,
                                      "finished_ts": time.time()})
                        await r.hset(K_OLLAMA, rid, json.dumps(entry))
                except Exception:
                    pass

            elif etype == "ollama.request_error":
                rid = ev.get("req_id", "")
                run = _RUNNING.pop(rid, {})
                elapsed = ev.get("elapsed_s", time.time() - run.get("start", time.time()))
                await _persist(
                    rid, "ollama.generate", "failed",
                    instance_id=ev.get("instance_id", ""),
                    model=ev.get("model", ""),
                    caller_file=ev.get("caller_file", ""),
                    caller_func=ev.get("caller_func", ""),
                    error=ev.get("error", ""),
                    elapsed_s=float(elapsed),
                )

            # ── Capability calls (cap.call / cap.ok / cap.error) ──
            # These catch direct HTTP calls that don't go through the stream
            elif etype == "cap.call":
                cname = ev.get("name", "")
                tid = ev.get("trace_id", ev.get("id", ""))
                if tid and cname:
                    _RUNNING[tid] = {"start": time.time(), "cap": cname}
                    await _persist(tid, cname, "running",
                                   extra={"source": "direct_call"})

            elif etype == "cap.ok":
                tid = ev.get("trace_id", ev.get("id", ""))
                run = _RUNNING.pop(tid, {})
                if tid:
                    elapsed = time.time() - run.get("start", time.time())
                    await _persist(tid, run.get("cap", ev.get("name", "")), "done",
                                   elapsed_s=elapsed)

            elif etype == "cap.error":
                tid = ev.get("trace_id", ev.get("id", ""))
                run = _RUNNING.pop(tid, {})
                if tid:
                    elapsed = time.time() - run.get("start", time.time())
                    await _persist(tid, run.get("cap", ev.get("name", "")), "failed",
                                   error=ev.get("error", ""), elapsed_s=elapsed)

        except Exception as e:
            log.debug("event_listener: %s (event=%s)", e, etype)


# ─────────────────────────────────────────────────────────────────────────────
#  RECOVERY: reclaim orphaned tasks from dead consumers on startup
# ─────────────────────────────────────────────────────────────────────────────

async def _recover_orphans():
    """Scan XPENDING for stale entries and either re-queue or mark failed."""
    r = _orch.REDIS
    if not r:
        return 0

    global _boot_id
    _boot_id = f"boot-{int(time.time())}-{os.getpid()}"
    try:
        await r.set(K_BOOT, _boot_id, ex=86400 * 30)
    except Exception:
        pass

    log.info("recovery scan starting (boot=%s, idle_threshold=%dms)", _boot_id, RECOVERY_IDLE_MS)
    reclaimed = 0

    try:
        pinfo = await r.xpending(TASK_STREAM, GROUP_WORKERS)
        pending_count = pinfo.get("pending", 0) if pinfo else 0
        if pending_count == 0:
            log.info("recovery: no pending entries — clean start")
            return 0

        log.info("recovery: %d pending entries in stream", pending_count)

        details = await r.xpending_range(
            TASK_STREAM, GROUP_WORKERS,
            min="-", max="+", count=500,
        )

        recovery_consumer = f"recovery-{_boot_id}"

        for entry in (details or []):
            msg_id = entry.get("message_id", b"")
            if isinstance(msg_id, bytes):
                msg_id = msg_id.decode()

            idle_ms = entry.get("time_since_delivered", 0)
            consumer = entry.get("consumer", b"")
            if isinstance(consumer, bytes):
                consumer = consumer.decode()
            delivery_count = entry.get("times_delivered", 1)

            if idle_ms < RECOVERY_IDLE_MS:
                continue

            log.warning("recovery: orphan %s idle=%dms consumer=%s deliveries=%d",
                        msg_id, idle_ms, consumer, delivery_count)

            try:
                claimed = await r.xclaim(
                    TASK_STREAM, GROUP_WORKERS, recovery_consumer,
                    min_idle_time=RECOVERY_IDLE_MS,
                    message_ids=[msg_id],
                )

                for cid, data in claimed:
                    def dec(v):
                        return v.decode() if isinstance(v, bytes) else str(v)

                    task_id  = dec(data.get(b"id", data.get("id", b"")))
                    cap_name = dec(data.get(b"capability", data.get("capability", b"?")))
                    trace_id = dec(data.get(b"trace_id", data.get("trace_id", b"")))
                    payload  = data.get(b"payload", data.get("payload", b"{}"))
                    if isinstance(payload, bytes):
                        payload = payload.decode()

                    await _persist(
                        task_id, cap_name, "orphan_reclaimed",
                        trace_id=trace_id,
                        extra={"original_consumer": consumer,
                               "idle_ms": str(idle_ms),
                               "delivery_count": str(delivery_count),
                               "reclaimed_by": _boot_id},
                    )

                    if cap_name in CAPABILITY_REGISTRY:
                        await r.xadd(TASK_STREAM, {
                            "id": task_id, "capability": cap_name,
                            "payload": payload, "trace_id": trace_id,
                            "ts": now_iso(), "recovered": "true",
                        })
                        log.info("recovery: re-queued %s (%s)", task_id, cap_name)
                    else:
                        log.warning("recovery: %s cap %s not registered — marking failed",
                                    task_id, cap_name)
                        await _persist(task_id, cap_name, "failed",
                                       error=f"Capability '{cap_name}' not registered after reboot")

                    await r.xack(TASK_STREAM, GROUP_WORKERS, msg_id)
                    reclaimed += 1

            except Exception as e:
                log.error("recovery: xclaim failed for %s: %s", msg_id, e)

    except Exception as e:
        log.error("recovery scan failed: %s", e)

    log.info("recovery complete — %d tasks reclaimed", reclaimed)
    await emit_event({"type": "job_persist.recovery_done",
                      "boot_id": _boot_id, "reclaimed": reclaimed})
    return reclaimed


# ─────────────────────────────────────────────────────────────────────────────
#  HYDRATE COMPLETED_JOBS: load recent history from Redis into workers.py
# ─────────────────────────────────────────────────────────────────────────────

async def _hydrate_completed_jobs():
    """Load recent completed jobs from Redis into the in-memory COMPLETED_JOBS
    list in workers.py, so the existing /cluster/jobs endpoint returns
    historical data even after a reboot."""
    r = _orch.REDIS
    if not r:
        return

    try:
        from Vera.Orchestration.workers import COMPLETED_JOBS
    except ImportError:
        log.debug("hydrate: workers module not loaded")
        return

    try:
        # Get recent job IDs from the sorted set
        ids = await r.zrevrange(K_IDX, 0, 499)
        if not ids:
            return

        pipe = r.pipeline()
        for jid in ids:
            jid_str = jid.decode() if isinstance(jid, bytes) else jid
            pipe.hgetall(f"{K_JOB}{jid_str}")

        results = await pipe.execute()

        hydrated = 0
        existing_ids = {j.get("id", "") for j in COMPLETED_JOBS}

        for raw in results:
            if not raw:
                continue
            rec = {(k.decode() if isinstance(k, bytes) else k):
                   (v.decode() if isinstance(v, bytes) else v)
                   for k, v in raw.items()}

            if rec.get("status") not in ("done", "failed", "orphan_reclaimed"):
                continue
            if rec.get("id") in existing_ids:
                continue

            COMPLETED_JOBS.append({
                "id":         rec.get("id", ""),
                "capability": rec.get("capability", ""),
                "worker_id":  rec.get("worker_id", ""),
                "success":    rec.get("status") == "done",
                "error":      rec.get("error", ""),
                "ts":         rec.get("created_at", rec.get("updated_at", "")),
                "elapsed_s":  rec.get("elapsed_s", ""),
                "caller_file":  rec.get("caller_file", ""),
                "caller_func":  rec.get("caller_func", ""),
                "instance_id":  rec.get("instance_id", ""),
                "model":        rec.get("model", ""),
                "boot_id":      rec.get("boot_id", ""),
                "from_history": True,
            })
            hydrated += 1

        if hydrated:
            # Sort by timestamp
            COMPLETED_JOBS.sort(key=lambda j: j.get("ts", ""))
            # Cap
            if len(COMPLETED_JOBS) > 500:
                del COMPLETED_JOBS[:-500]
            log.info("hydrate: loaded %d historical jobs into COMPLETED_JOBS", hydrated)

    except Exception as e:
        log.debug("hydrate: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
#  CLEANUP: periodic trim of expired entries
# ─────────────────────────────────────────────────────────────────────────────

async def _cleanup():
    r = _orch.REDIS
    if not r:
        return
    try:
        cutoff = time.time() - JOB_TTL
        removed = await r.zremrangebyscore(K_IDX, "-inf", cutoff)
        if removed:
            log.info("cleanup: trimmed %d expired entries", removed)
        total = await r.zcard(K_IDX)
        if total > JOB_IDX_MAX:
            excess = total - JOB_IDX_MAX
            await r.zremrangebyrank(K_IDX, 0, excess - 1)

        # Trim ollama log hash
        try:
            count = await r.hlen(K_OLLAMA)
            if count > 600:
                all_keys = await r.hkeys(K_OLLAMA)
                to_del = all_keys[:100]
                if to_del:
                    await r.hdel(K_OLLAMA, *to_del)
        except Exception:
            pass

    except Exception as e:
        log.debug("cleanup: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
#  CAPABILITIES: query endpoints for the existing panel
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "jobs.history",
    http_method="GET", http_path="/jobs/history",
    http_tags=["jobs", "monitoring"],
    memory="off", silent=True,
    description=(
        "Persisted job history from Redis — survives reboots. "
        "Input: limit (int, default 100), status (str filter), "
        "cap (str filter), since_ts (float). "
        "Output: {jobs: [...], total: int, boot_id: str}"
    ),
)
async def jobs_history(
    limit: int = 500,
    status: str = "",
    cap: str = "",
    since_ts: float = 0,
    offset: int = 0,
    trace_id: str = "",
):
    r = _orch.REDIS
    if not r:
        return {"jobs": _LOCAL[-limit:], "total": len(_LOCAL),
                "boot_id": _boot_id, "source": "local_only"}
    try:
        if since_ts:
            ids = await r.zrangebyscore(K_IDX, since_ts, "+inf")
        else:
            # Fetch enough to cover offset + limit after filtering
            fetch_count = min((offset + limit) * 3, 5000)
            ids = await r.zrevrange(K_IDX, 0, fetch_count - 1)

        pipe = r.pipeline()
        for jid in ids:
            jid_str = jid.decode() if isinstance(jid, bytes) else jid
            pipe.hgetall(f"{K_JOB}{jid_str}")

        results = await pipe.execute()
        jobs = []
        skipped = 0
        for raw in results:
            if not raw:
                continue
            rec = {(k.decode() if isinstance(k, bytes) else k):
                   (v.decode() if isinstance(v, bytes) else v)
                   for k, v in raw.items()}
            if status and rec.get("status") != status:
                continue
            if cap and cap not in rec.get("capability", ""):
                continue
            # Handle offset
            if skipped < offset:
                skipped += 1
                continue
            jobs.append(rec)
            if len(jobs) >= limit:
                break

        total = await r.zcard(K_IDX)
        return {"jobs": jobs, "total": total, "boot_id": _boot_id,
                "offset": offset, "limit": limit}
    except Exception as e:
        return {"jobs": _LOCAL[-limit:], "total": len(_LOCAL),
                "boot_id": _boot_id, "source": "fallback", "error": str(e)}


@capability(
    "jobs.stats",
    http_method="GET", http_path="/jobs/stats",
    http_tags=["jobs", "monitoring"],
    memory="off", silent=True,
    description=(
        "Aggregate job stats: done/failed/reclaimed counts, stream health, "
        "consumer list with idle times, boot info."
    ),
)
async def jobs_stats(trace_id: str = ""):
    r = _orch.REDIS
    out = {"boot_id": _boot_id, "running_tracked": len(_RUNNING)}
    if not r:
        return out
    try:
        # Aggregate counters
        raw = await r.hgetall(K_STATS)
        stats = {}
        for k, v in raw.items():
            ks = k.decode() if isinstance(k, bytes) else k
            vs = v.decode() if isinstance(v, bytes) else v
            try:
                stats[ks] = int(vs)
            except ValueError:
                stats[ks] = vs
        out["stats"] = stats
        out["history_count"] = await r.zcard(K_IDX)

        # Stream health
        try:
            sinfo = await r.xinfo_stream(TASK_STREAM)
            pinfo = await r.xpending(TASK_STREAM, GROUP_WORKERS)
            out["stream"] = {
                "length":        sinfo.get("length", 0),
                "pending_total": pinfo.get("pending", 0) if pinfo else 0,
            }
            # Consumer details — critical for finding dead consumers
            try:
                consumers = await r.xinfo_consumers(TASK_STREAM, GROUP_WORKERS)
                out["stream"]["consumers"] = []
                for c in consumers:
                    name = c.get("name", b"")
                    if isinstance(name, bytes):
                        name = name.decode()
                    out["stream"]["consumers"].append({
                        "name":    name,
                        "pending": c.get("pending", 0),
                        "idle":    c.get("idle", 0),
                    })
            except Exception:
                pass
        except Exception:
            out["stream"] = {"error": "stream not created"}

    except Exception as e:
        out["error"] = str(e)
    return out


@capability(
    "jobs.ollama_log",
    http_method="GET", http_path="/jobs/ollama_log",
    http_tags=["jobs", "monitoring"],
    memory="off", silent=True,
    description=(
        "Ollama request log — shows which caller_file:caller_func hit which "
        "instance (GPU vs CPU), model, elapsed time. "
        "This is the key diagnostic for identifying what is loading Ollama. "
        "Input: limit (int, default 100). "
        "Output: {requests: [...], total: int, summary: {...}}"
    ),
)
async def jobs_ollama_log(limit: int = 500, trace_id: str = ""):
    r = _orch.REDIS
    if not r:
        return {"requests": [], "total": 0, "error": "no redis"}
    try:
        raw = await r.hgetall(K_OLLAMA)
        entries = []
        for k, v in raw.items():
            try:
                entries.append(json.loads(v))
            except Exception:
                pass

        entries.sort(key=lambda e: e.get("ts", 0), reverse=True)

        # Build a summary: caller_file → count, instance → count
        by_caller = {}
        by_instance = {}
        by_model = {}
        still_running = []
        for e in entries:
            cf = e.get("caller_file", "?")
            fn = e.get("caller_func", "?")
            key = f"{cf}:{fn}"
            by_caller[key] = by_caller.get(key, 0) + 1
            iid = e.get("instance_id", "?")
            by_instance[iid] = by_instance.get(iid, 0) + 1
            mdl = e.get("model", "?")
            by_model[mdl] = by_model.get(mdl, 0) + 1
            if e.get("status") == "running":
                still_running.append(e)

        return {
            "requests": entries[:limit],
            "total": len(entries),
            "summary": {
                "by_caller":   dict(sorted(by_caller.items(), key=lambda x: -x[1])),
                "by_instance": dict(sorted(by_instance.items(), key=lambda x: -x[1])),
                "by_model":    dict(sorted(by_model.items(), key=lambda x: -x[1])),
                "still_running": still_running,
            },
        }
    except Exception as e:
        return {"requests": [], "total": 0, "error": str(e)}


@capability(
    "jobs.recover_now",
    http_method="POST", http_path="/jobs/recover",
    http_tags=["jobs", "monitoring"],
    memory="off",
    description="Manually trigger orphan recovery scan.",
)
async def jobs_recover_now(trace_id: str = ""):
    count = await _recover_orphans()
    return {"status": "complete", "reclaimed": count, "boot_id": _boot_id}


@capability(
    "jobs.running_at_boot",
    http_method="GET", http_path="/jobs/running_at_boot",
    http_tags=["jobs", "monitoring"],
    memory="off", silent=True,
    description=(
        "Jobs that were in 'running' state when the server last died. "
        "These are the ones that were on Ollama (or another worker) when "
        "the reboot happened — the key diagnostic for 'what is still loading "
        "the GPU instance after reboot'. "
        "Output: {jobs: [...], boot_id: str}"
    ),
)
async def jobs_running_at_boot(trace_id: str = ""):
    """Find jobs persisted as 'running' from a PREVIOUS boot — these are
    the ones that were mid-flight when the server died."""
    r = _orch.REDIS
    if not r:
        return {"jobs": [], "boot_id": _boot_id}

    try:
        ids = await r.zrevrange(K_IDX, 0, 999)
        pipe = r.pipeline()
        for jid in ids:
            jid_str = jid.decode() if isinstance(jid, bytes) else jid
            pipe.hgetall(f"{K_JOB}{jid_str}")

        results = await pipe.execute()
        orphans = []
        for raw in results:
            if not raw:
                continue
            rec = {(k.decode() if isinstance(k, bytes) else k):
                   (v.decode() if isinstance(v, bytes) else v)
                   for k, v in raw.items()}
            # Running + from a different boot = was mid-flight when server died
            if rec.get("status") == "running" and rec.get("boot_id", "") != _boot_id:
                orphans.append(rec)

        return {"jobs": orphans, "boot_id": _boot_id,
                "previous_boots": list({j.get("boot_id", "") for j in orphans})}

    except Exception as e:
        return {"jobs": [], "boot_id": _boot_id, "error": str(e)}


@capability(
    "jobs.delete_consumer",
    http_method="POST", http_path="/jobs/delete_consumer",
    http_tags=["jobs", "monitoring"],
    memory="off",
    description=(
        "Delete a stale consumer from the Redis stream consumer group. "
        "This actually removes the consumer entry (XGROUP DELCONSUMER), "
        "not just hides it. Input: consumer_name (str). "
        "Any pending messages owned by that consumer become unowned."
    ),
)
async def jobs_delete_consumer(consumer_name: str = "", trace_id: str = ""):
    if not consumer_name:
        return {"error": "consumer_name required"}
    r = _orch.REDIS
    if not r:
        return {"error": "no redis"}
    try:
        # First ACK all pending messages for this consumer so they don't block
        try:
            details = await r.xpending_range(
                TASK_STREAM, GROUP_WORKERS,
                min="-", max="+", count=1000,
            )
            acked = 0
            for entry in (details or []):
                consumer = entry.get("consumer", b"")
                if isinstance(consumer, bytes):
                    consumer = consumer.decode()
                if consumer == consumer_name:
                    msg_id = entry.get("message_id", b"")
                    if isinstance(msg_id, bytes):
                        msg_id = msg_id.decode()
                    await r.xack(TASK_STREAM, GROUP_WORKERS, msg_id)
                    acked += 1
        except Exception as e:
            log.debug("delete_consumer ack: %s", e)
            acked = 0

        # Now delete the consumer
        pending_removed = await r.xgroup_delconsumer(
            TASK_STREAM, GROUP_WORKERS, consumer_name
        )
        log.info("deleted consumer %s (had %d pending, acked %d)",
                 consumer_name, pending_removed, acked)
        return {"ok": True, "consumer": consumer_name,
                "pending_removed": pending_removed, "acked": acked}
    except Exception as e:
        return {"error": str(e), "consumer": consumer_name}


@capability(
    "jobs.purge_pending",
    http_method="POST", http_path="/jobs/purge_pending",
    http_tags=["jobs", "monitoring"],
    memory="off",
    description=(
        "ACK and discard ALL pending (stuck) messages in the task stream. "
        "Use when jobs are stuck in pending and will never be processed. "
        "Optionally filter by consumer_name or min_idle_ms. "
        "Output: {acked: int, persisted_as_failed: int}"
    ),
)
async def jobs_purge_pending(
    consumer_name: str = "",
    min_idle_ms: int = 0,
    trace_id: str = "",
):
    r = _orch.REDIS
    if not r:
        return {"error": "no redis"}
    try:
        details = await r.xpending_range(
            TASK_STREAM, GROUP_WORKERS,
            min="-", max="+", count=2000,
        )
        acked = 0
        persisted = 0
        for entry in (details or []):
            consumer = entry.get("consumer", b"")
            if isinstance(consumer, bytes):
                consumer = consumer.decode()
            idle_ms = entry.get("time_since_delivered", 0)

            # Apply filters
            if consumer_name and consumer != consumer_name:
                continue
            if min_idle_ms and idle_ms < min_idle_ms:
                continue

            msg_id = entry.get("message_id", b"")
            if isinstance(msg_id, bytes):
                msg_id = msg_id.decode()

            # Read message data to persist as failed
            try:
                msgs = await r.xrange(TASK_STREAM, min=msg_id, max=msg_id, count=1)
                for eid, data in msgs:
                    def dec(v):
                        return v.decode() if isinstance(v, bytes) else str(v)
                    task_id = dec(data.get(b"id", data.get("id", b"")))
                    cap_name = dec(data.get(b"capability", data.get("capability", b"?")))
                    if task_id:
                        await _persist(
                            task_id, cap_name, "failed",
                            error=f"Purged: stuck pending for {idle_ms}ms on consumer {consumer}",
                            extra={"purged_by": "manual", "original_consumer": consumer,
                                   "idle_ms": str(idle_ms)},
                        )
                        persisted += 1
            except Exception:
                pass

            await r.xack(TASK_STREAM, GROUP_WORKERS, msg_id)
            acked += 1

        log.info("purge_pending: acked %d, persisted %d as failed", acked, persisted)
        return {"acked": acked, "persisted_as_failed": persisted}
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
#  STARTUP SEQUENCE
# ─────────────────────────────────────────────────────────────────────────────

async def _startup():
    while not _orch.REDIS:
        await asyncio.sleep(1)

    log.info("Redis available — starting recovery + hydration + listener")

    # 1. Recovery: reclaim orphaned stream entries
    await _recover_orphans()

    # 2. Hydrate: load historical jobs into workers.py COMPLETED_JOBS
    await _hydrate_completed_jobs()

    # 3. Start the persistent event listener
    asyncio.create_task(_event_listener())

    log.info("job_persist fully initialized (boot=%s)", _boot_id)


schedule(_startup, interval=999999, name="job_persist_startup")
schedule(_cleanup, interval=3600, name="job_persist_cleanup")

try:
    _loop = asyncio.get_event_loop()
    if _loop.is_running():
        _loop.create_task(_startup())
except Exception:
    pass


log.info("job_persistence loaded — TTL=%dd, PG=%s", JOB_TTL // 86400, ARCHIVE_TO_PG)