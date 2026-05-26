"""
job_persistence.py — Job Lifecycle Persistence & Recovery
==========================================================
Addresses the gap where job state is lost on server reboot:

  1. COMPLETED_JOBS is an in-memory list — wiped on restart
  2. _pg_archive() exists but is never called — dead code
  3. Worker loop emits events but never persists job records
  4. xreadgroup starts from "$" — orphaned tasks stay in the stream
  5. No mechanism to reclaim tasks stuck on dead consumers

This module provides:

  • Redis-backed job history (vera:job_history:{id}) with configurable TTL
  • Postgres archival for long-term retention
  • Startup recovery: scans for orphaned pending entries and re-queues them
  • Stale consumer cleanup: detects consumers that haven't read in N seconds
  • Monitoring capabilities for the UI panel
  • Hooks into the worker loop via emit_event subscription

Add to _module_files in capability_orchestration.py:
    os.path.join(_here, "job_persistence.py"),
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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
    register_ui,
    schedule,
)
import Vera.Orchestration.capability_orchestration as _orch

log = logging.getLogger("vera.job_persist")

# ── Configuration ─────────────────────────────────────────────────────────────
JOB_HISTORY_TTL    = int(os.getenv("VERA_JOB_HISTORY_TTL", 86400 * 7))   # 7 days
JOB_HISTORY_MAX    = int(os.getenv("VERA_JOB_HISTORY_MAX", 2000))
STALE_CONSUMER_SEC = int(os.getenv("VERA_STALE_CONSUMER_SEC", 300))       # 5 min
RECOVERY_IDLE_MS   = int(os.getenv("VERA_RECOVERY_IDLE_MS", 120_000))     # 2 min
ARCHIVE_TO_PG      = os.getenv("VERA_JOB_ARCHIVE_PG", "1") == "1"

# Redis key prefixes
_K_JOB       = "vera:job_history:"     # per-job hash
_K_JOBS_IDX  = "vera:job_history_idx"  # sorted set: score=timestamp, member=job_id
_K_STATS     = "vera:job_stats"        # hash of aggregate counters
_K_BOOT      = "vera:boot_id"         # current boot id for orphan detection

# ── In-memory ring buffer (fast local lookups, supplements Redis) ─────────────
_LOCAL_HISTORY: List[dict] = []
_LOCAL_HISTORY_MAX = 500


# ─────────────────────────────────────────────────────────────────────────────
#  PERSISTENCE: write job records to Redis + optional PG
# ─────────────────────────────────────────────────────────────────────────────

async def persist_job(
    job_id: str,
    capability_name: str,
    status: str,                     # "pending" | "running" | "done" | "failed" | "orphan_reclaimed"
    worker_id: str = "",
    trace_id: str = "",
    result_preview: str = "",
    error: str = "",
    elapsed_s: float = 0.0,
    extra: Optional[dict] = None,
):
    """Write or update a job record in Redis (with TTL) and optionally Postgres."""
    now = time.time()
    record = {
        "id":           job_id,
        "capability":   capability_name,
        "status":       status,
        "worker_id":    worker_id,
        "trace_id":     trace_id,
        "result_preview": (result_preview or "")[:500],
        "error":        (error or "")[:500],
        "elapsed_s":    str(round(elapsed_s, 2)),
        "updated_at":   now_iso(),
        "updated_ts":   str(now),
        "boot_id":      _current_boot_id,
    }
    if extra:
        for k, v in extra.items():
            record[k] = str(v) if not isinstance(v, str) else v

    # Local ring buffer
    _LOCAL_HISTORY.append(record)
    if len(_LOCAL_HISTORY) > _LOCAL_HISTORY_MAX:
        del _LOCAL_HISTORY[:-_LOCAL_HISTORY_MAX]

    # Redis persistence
    r = _orch.REDIS
    if r:
        try:
            key = f"{_K_JOB}{job_id}"
            # Merge — don't overwrite fields set by earlier phases
            existing = await r.hgetall(key)
            if existing:
                # Preserve created_at from the original record
                created = existing.get(b"created_at", b"")
                if created:
                    record["created_at"] = created.decode() if isinstance(created, bytes) else str(created)
                created_ts = existing.get(b"created_ts", b"")
                if created_ts:
                    record["created_ts"] = created_ts.decode() if isinstance(created_ts, bytes) else str(created_ts)
            else:
                record["created_at"] = record["updated_at"]
                record["created_ts"] = record["updated_ts"]

            await r.hset(key, mapping=record)
            await r.expire(key, JOB_HISTORY_TTL)

            # Sorted set index (for range queries / cleanup)
            await r.zadd(_K_JOBS_IDX, {job_id: now})

            # Aggregate stats
            if status == "done":
                await r.hincrby(_K_STATS, "total_done", 1)
                await r.hincrby(_K_STATS, f"done:{capability_name}", 1)
            elif status == "failed":
                await r.hincrby(_K_STATS, "total_failed", 1)
                await r.hincrby(_K_STATS, f"failed:{capability_name}", 1)
            elif status == "orphan_reclaimed":
                await r.hincrby(_K_STATS, "total_reclaimed", 1)

        except Exception as e:
            log.debug("persist_job redis: %s", e)

    # Postgres archival
    if ARCHIVE_TO_PG and status in ("done", "failed") and PG_POOL:
        try:
            async with PG_POOL.acquire() as conn:
                await conn.execute(
                    "INSERT INTO vera_task_results(task_id, result, ts) "
                    "VALUES($1, $2::jsonb, NOW()) ON CONFLICT DO NOTHING",
                    job_id,
                    json.dumps(record),
                )
        except Exception as e:
            log.debug("persist_job pg: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
#  EVENT HOOKS: subscribe to worker events and persist automatically
# ─────────────────────────────────────────────────────────────────────────────

_RUNNING_JOBS: Dict[str, float] = {}   # job_id -> start_time

async def _event_listener():
    """Subscribe to vera events and persist job state transitions."""
    r = _orch.REDIS
    if not r:
        log.info("job_persist: waiting for Redis…")
        while not _orch.REDIS:
            await asyncio.sleep(2)
        r = _orch.REDIS

    ps = r.pubsub()
    await ps.subscribe("vera:events")
    log.info("job_persist: event listener started")

    async for msg in ps.listen():
        if msg["type"] != "message":
            continue
        try:
            data = json.loads(msg["data"])
        except Exception:
            continue

        etype = data.get("type", "")

        if etype == "worker.start":
            task_id = data.get("task", "")
            cap     = data.get("capability", "")
            worker  = data.get("worker", "")
            if task_id:
                _RUNNING_JOBS[task_id] = time.time()
                await persist_job(
                    task_id, cap, "running",
                    worker_id=worker,
                )

        elif etype == "worker.done":
            task_id = data.get("task", "")
            worker  = data.get("worker", "")
            start_t = _RUNNING_JOBS.pop(task_id, 0)
            elapsed = time.time() - start_t if start_t else 0
            # Get capability name from the worker registry or prior record
            cap = ""
            w = WORKER_REGISTRY.get(worker, {})
            cap = w.get("current_task", "") or data.get("capability", "")
            await persist_job(
                task_id, cap, "done",
                worker_id=worker,
                elapsed_s=elapsed,
            )

        elif etype == "worker.error":
            task_id = data.get("task", "")
            worker  = data.get("worker", "")
            error   = data.get("error", "")
            start_t = _RUNNING_JOBS.pop(task_id, 0)
            elapsed = time.time() - start_t if start_t else 0
            cap     = data.get("capability", "")
            await persist_job(
                task_id, cap, "failed",
                worker_id=worker,
                error=error,
                elapsed_s=elapsed,
            )

        elif etype == "ollama.request":
            # Track Ollama requests for GPU/CPU monitoring
            req_id = data.get("req_id", "")
            if req_id:
                try:
                    await r.hset("vera:ollama_req_log", req_id, json.dumps({
                        "req_id":       req_id,
                        "model":        data.get("model", ""),
                        "instance_id":  data.get("instance_id", ""),
                        "instance_url": data.get("instance_url", ""),
                        "caller_file":  data.get("caller_file", ""),
                        "caller_func":  data.get("caller_func", ""),
                        "cap_name":     data.get("cap_name", ""),
                        "prefer_gpu":   data.get("prefer_gpu", False),
                        "ts":           time.time(),
                    }))
                    # Keep only last 500 entries
                    count = await r.hlen("vera:ollama_req_log")
                    if count > 600:
                        all_keys = await r.hkeys("vera:ollama_req_log")
                        # Delete oldest 100
                        to_del = all_keys[:100]
                        if to_del:
                            await r.hdel("vera:ollama_req_log", *to_del)
                except Exception:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
#  RECOVERY: on startup, find orphaned tasks and re-queue or mark them
# ─────────────────────────────────────────────────────────────────────────────

_current_boot_id = ""

async def _recover_orphaned_tasks():
    """
    Scan for tasks that were pending/running when the server last died.
    
    Strategy:
    1. Check xpending for entries idle > RECOVERY_IDLE_MS  
    2. XCLAIM them to a recovery consumer
    3. Re-add them to the stream so active workers pick them up
    4. Log everything for the monitoring panel
    """
    r = _orch.REDIS
    if not r:
        return

    global _current_boot_id
    _current_boot_id = f"boot-{int(time.time())}-{os.getpid()}"
    await r.set(_K_BOOT, _current_boot_id, ex=86400 * 30)

    log.info("job_persist: recovery scan starting (boot=%s, idle_threshold=%dms)",
             _current_boot_id, RECOVERY_IDLE_MS)

    reclaimed = 0
    try:
        # Get pending summary
        pinfo = await r.xpending(TASK_STREAM, GROUP_WORKERS)
        pending_count = pinfo.get("pending", 0) if pinfo else 0

        if pending_count == 0:
            log.info("job_persist: no pending entries — clean start")
            return

        log.info("job_persist: %d pending entries in stream — scanning for orphans", pending_count)

        # Get detailed pending entries
        details = await r.xpending_range(
            TASK_STREAM, GROUP_WORKERS,
            min="-", max="+", count=500,
        )

        recovery_consumer = f"recovery-{_current_boot_id}"

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
                continue  # still fresh, a live worker might be on it

            # This entry has been idle too long — the consumer is probably dead
            log.warning(
                "job_persist: orphaned entry %s idle=%dms consumer=%s deliveries=%d — reclaiming",
                msg_id, idle_ms, consumer, delivery_count,
            )

            try:
                # XCLAIM: take ownership
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

                    # Persist the orphan record
                    await persist_job(
                        task_id, cap_name, "orphan_reclaimed",
                        trace_id=trace_id,
                        extra={
                            "original_consumer": consumer,
                            "idle_ms":           idle_ms,
                            "delivery_count":    delivery_count,
                            "reclaimed_by":      _current_boot_id,
                        },
                    )

                    # Check if we still have a handler for this capability
                    if cap_name in CAPABILITY_REGISTRY:
                        # Re-add to stream for a live worker to pick up
                        await r.xadd(TASK_STREAM, {
                            "id":         task_id,
                            "capability": cap_name,
                            "payload":    payload,
                            "trace_id":   trace_id,
                            "ts":         now_iso(),
                            "recovered":  "true",
                        })
                        log.info("job_persist: re-queued orphan %s (%s)", task_id, cap_name)
                    else:
                        log.warning("job_persist: orphan %s capability %s no longer registered — marking failed",
                                    task_id, cap_name)
                        await persist_job(
                            task_id, cap_name, "failed",
                            error=f"Capability '{cap_name}' not registered after reboot",
                        )

                    # ACK the original so it doesn't get reclaimed again
                    await r.xack(TASK_STREAM, GROUP_WORKERS, msg_id)
                    reclaimed += 1

            except Exception as e:
                log.error("job_persist: xclaim failed for %s: %s", msg_id, e)

    except Exception as e:
        log.error("job_persist: recovery scan failed: %s", e)

    log.info("job_persist: recovery complete — %d tasks reclaimed", reclaimed)
    await emit_event({
        "type":      "job_persist.recovery_done",
        "boot_id":   _current_boot_id,
        "reclaimed": reclaimed,
    })


# ─────────────────────────────────────────────────────────────────────────────
#  CLEANUP: trim old entries from the sorted set index + expire Redis keys
# ─────────────────────────────────────────────────────────────────────────────

async def _cleanup_old_jobs():
    """Periodic trim of the job history index."""
    r = _orch.REDIS
    if not r:
        return

    try:
        # Remove entries older than TTL from the sorted set
        cutoff = time.time() - JOB_HISTORY_TTL
        removed = await r.zremrangebyscore(_K_JOBS_IDX, "-inf", cutoff)
        if removed:
            log.info("job_persist: cleaned %d expired job index entries", removed)

        # Cap the index size
        total = await r.zcard(_K_JOBS_IDX)
        if total > JOB_HISTORY_MAX:
            excess = total - JOB_HISTORY_MAX
            await r.zremrangebyrank(_K_JOBS_IDX, 0, excess - 1)
            log.info("job_persist: trimmed %d excess entries (max=%d)", excess, JOB_HISTORY_MAX)

    except Exception as e:
        log.debug("job_persist cleanup: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
#  CAPABILITIES: query job history, stats, recovery info
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "jobs.history",
    http_method="GET", http_path="/jobs/history",
    http_tags=["jobs", "monitoring"],
    memory="off", silent=True,
    description=(
        "Query persisted job history from Redis. "
        "Input: limit (int, default 50), status (str, optional filter), "
        "capability (str, optional filter), since_ts (float, optional). "
        "Output: {jobs: [...], total: int, boot_id: str}"
    ),
)
async def jobs_history(
    limit: int = 50,
    status: str = "",
    capability: str = "",
    since_ts: float = 0,
    trace_id: str = "",
):
    r = _orch.REDIS
    if not r:
        return {"jobs": _LOCAL_HISTORY[-limit:], "total": len(_LOCAL_HISTORY),
                "boot_id": _current_boot_id, "source": "local_only"}

    try:
        # Get job IDs from sorted index (newest first)
        if since_ts:
            ids = await r.zrangebyscore(_K_JOBS_IDX, since_ts, "+inf")
        else:
            ids = await r.zrevrange(_K_JOBS_IDX, 0, min(limit * 2, 500) - 1)

        jobs = []
        pipeline = r.pipeline()
        for jid in ids:
            jid_str = jid.decode() if isinstance(jid, bytes) else jid
            pipeline.hgetall(f"{_K_JOB}{jid_str}")

        results = await pipeline.execute()

        for raw in results:
            if not raw:
                continue
            record = {
                (k.decode() if isinstance(k, bytes) else k):
                (v.decode() if isinstance(v, bytes) else v)
                for k, v in raw.items()
            }
            # Apply filters
            if status and record.get("status") != status:
                continue
            if capability and record.get("capability") != capability:
                continue
            jobs.append(record)
            if len(jobs) >= limit:
                break

        total = await r.zcard(_K_JOBS_IDX)
        return {"jobs": jobs, "total": total, "boot_id": _current_boot_id}

    except Exception as e:
        log.debug("jobs.history: %s", e)
        return {"jobs": _LOCAL_HISTORY[-limit:], "total": len(_LOCAL_HISTORY),
                "boot_id": _current_boot_id, "source": "local_fallback", "error": str(e)}


@capability(
    "jobs.stats",
    http_method="GET", http_path="/jobs/stats",
    http_tags=["jobs", "monitoring"],
    memory="off", silent=True,
    description=(
        "Aggregate job statistics: total done/failed/reclaimed, per-capability counts, "
        "current boot info, stream health. "
        "Output: {stats: {...}, stream: {...}, boot_id: str}"
    ),
)
async def jobs_stats(trace_id: str = ""):
    r = _orch.REDIS
    result = {
        "boot_id":      _current_boot_id,
        "running_local": len(_RUNNING_JOBS),
        "history_local": len(_LOCAL_HISTORY),
    }

    if not r:
        return result

    try:
        # Aggregate stats
        raw_stats = await r.hgetall(_K_STATS)
        stats = {}
        for k, v in raw_stats.items():
            k_str = k.decode() if isinstance(k, bytes) else k
            v_str = v.decode() if isinstance(v, bytes) else v
            try:
                stats[k_str] = int(v_str)
            except ValueError:
                stats[k_str] = v_str
        result["stats"] = stats

        # Stream health
        try:
            stream_info = await r.xinfo_stream(TASK_STREAM)
            pinfo = await r.xpending(TASK_STREAM, GROUP_WORKERS)

            result["stream"] = {
                "length":          stream_info.get("length", 0),
                "first_entry":     str(stream_info.get("first-entry", "")),
                "last_entry":      str(stream_info.get("last-entry", "")),
                "pending_total":   pinfo.get("pending", 0) if pinfo else 0,
                "consumers":       [],
            }

            # Consumer details
            try:
                consumers = await r.xinfo_consumers(TASK_STREAM, GROUP_WORKERS)
                for c in consumers:
                    name = c.get("name", b"")
                    if isinstance(name, bytes):
                        name = name.decode()
                    result["stream"]["consumers"].append({
                        "name":    name,
                        "pending": c.get("pending", 0),
                        "idle":    c.get("idle", 0),
                    })
            except Exception:
                pass

        except Exception:
            result["stream"] = {"error": "stream not initialized"}

        # Ollama request log summary
        try:
            req_count = await r.hlen("vera:ollama_req_log")
            result["ollama_requests_logged"] = req_count
        except Exception:
            pass

        # Index size
        result["history_redis"] = await r.zcard(_K_JOBS_IDX)

    except Exception as e:
        result["error"] = str(e)

    return result


@capability(
    "jobs.ollama_log",
    http_method="GET", http_path="/jobs/ollama_log",
    http_tags=["jobs", "monitoring"],
    memory="off", silent=True,
    description=(
        "Recent Ollama request log — shows which capabilities are hitting which "
        "instances (GPU vs CPU), caller info, timestamps. "
        "Input: limit (int, default 50). "
        "Output: {requests: [...], total: int}"
    ),
)
async def jobs_ollama_log(limit: int = 50, trace_id: str = ""):
    r = _orch.REDIS
    if not r:
        return {"requests": [], "total": 0, "error": "no redis"}

    try:
        raw = await r.hgetall("vera:ollama_req_log")
        entries = []
        for k, v in raw.items():
            try:
                entry = json.loads(v)
                entries.append(entry)
            except Exception:
                pass

        # Sort by timestamp descending
        entries.sort(key=lambda e: e.get("ts", 0), reverse=True)
        return {"requests": entries[:limit], "total": len(entries)}

    except Exception as e:
        return {"requests": [], "total": 0, "error": str(e)}


@capability(
    "jobs.recover_now",
    http_method="POST", http_path="/jobs/recover",
    http_tags=["jobs", "monitoring"],
    memory="off",
    description=(
        "Manually trigger orphan recovery scan. "
        "Use when you suspect tasks are stuck in the stream. "
        "Output: {status: 'started'}"
    ),
)
async def jobs_recover_now(trace_id: str = ""):
    asyncio.create_task(_recover_orphaned_tasks())
    return {"status": "started", "boot_id": _current_boot_id}


# ─────────────────────────────────────────────────────────────────────────────
#  STARTUP
# ─────────────────────────────────────────────────────────────────────────────

async def _startup():
    """Wait for Redis, run recovery, start event listener."""
    # Wait for Redis
    while not _orch.REDIS:
        await asyncio.sleep(1)

    log.info("job_persist: Redis available — starting recovery + listener")

    # Recovery first
    await _recover_orphaned_tasks()

    # Then start the event listener
    asyncio.create_task(_event_listener())

    log.info("job_persist: fully initialized")


# Schedule startup and periodic cleanup
schedule(_startup, interval=999999, name="job_persist_startup")
schedule(_cleanup_old_jobs, interval=3600, name="job_persist_cleanup")  # hourly

# Attempt immediate start if loop is already running
try:
    _loop = asyncio.get_event_loop()
    if _loop.is_running():
        _loop.create_task(_startup())
except Exception:
    pass


# ─────────────────────────────────────────────────────────────────────────────
#  UI PANEL
# ─────────────────────────────────────────────────────────────────────────────

_PANEL_HTML_PATH = Path(__file__).parent / "job_persistence_panel.html"

def _load_panel():
    try:
        return _PANEL_HTML_PATH.read_text()
    except Exception:
        return "<div style='padding:20px;color:#aaa'>job_persistence_panel.html not found</div>"


_MOUNT_JS = r"""
(function mountJobPersistPanel() {
  var mount = document.getElementById('panel-job-persist');
  if (!mount || mount._mounted) return;
  mount._mounted = true;
  var frame = document.createElement('iframe');
  var backendBase = (document.getElementById('backendUrl') || {}).value || '';
  backendBase = backendBase.replace(/\/$/, '') || window._veraBase || 'http://llm.int:8999';
  frame.src = backendBase + '/ui/panels/job-persist';
  frame.style.cssText = 'width:100%;height:100%;border:none;display:block;background:#181614';
  frame.allow = 'clipboard-read; clipboard-write';
  mount.appendChild(frame);
})();
"""

from fastapi.responses import HTMLResponse

@APP.get("/ui/panels/job-persist", response_class=HTMLResponse, include_in_schema=False)
async def _serve_panel():
    return HTMLResponse(_load_panel())

register_ui(
    panel_id  = "job-persist",
    label     = "Job Monitor",
    icon      = "",
    mode      = "tab",
    tab_order = 2,
    html      = '<div id="panel-job-persist" style="height:100%;overflow:hidden;background:var(--bg0)"></div>',
    mount_js  = _MOUNT_JS,
    ui_caps   = ["jobs.history", "jobs.stats", "jobs.ollama_log", "jobs.recover_now"],
)

log.info("job_persistence loaded — history TTL=%ds, PG archive=%s",
         JOB_HISTORY_TTL, ARCHIVE_TO_PG)