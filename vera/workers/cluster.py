"""
vera_cluster.py  —  Cluster Monitor & Ollama Proxy
===================================================

Three systems in one module:

1. Enhanced cluster monitoring
   ────────────────────────────
   Polls every Ollama node for:
     /api/ps  — currently loaded models, VRAM usage per model
     /api/version — Ollama version
   Enriches OLLAMA_INSTANCES with: running[], vram_used_gb, model_count
   Writes snapshot to Redis vera:cluster:ollama every 10s.
   obs.cluster endpoint: full merged view of workers + their Ollama nodes.

2. Load-aware routing patch
   ─────────────────────────
   Replaces pick_instance() with a version that:
     - Adds co-located worker load as a penalty on the Ollama node they run on
     - Adds proxy queue depth as a penalty for the proxy target
     - Still respects prefer_gpu, model affinity, and instance_id pin

3. Ollama transparent proxy (port 11434 intercept)
   ─────────────────────────────────────────────────
   Set LOCAL_OLLAMA_INSTANCE env var (e.g. "gpu-250") to activate.
   Routes mount at /ollama/* on Vera's own port (8000).
   External clients use http://<host>:8000/ollama/ instead of :11434.

   On each request:
     a) Parse body, record to Redis stream + emit memory event (async)
     b) Check in_use vs PROXY_MAX_CONCURRENCY
        - Under limit → forward immediately (streaming preserved)
        - Over limit  → enqueue up to PROXY_QUEUE_TIMEOUT seconds
     c) Queue worker drains in FIFO order as capacity frees up
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Dict, List, Optional

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse, JSONResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, OLLAMA_INSTANCES,
    capability, emit_event, now_iso, schedule,
)

log = logging.getLogger("vera.cluster")

# ── Config ────────────────────────────────────────────────────────────────────
LOCAL_OLLAMA_INSTANCE = os.getenv("LOCAL_OLLAMA_INSTANCE", "")
PROXY_MAX_CONCURRENCY = int(os.getenv("PROXY_MAX_CONCURRENCY", "3"))
PROXY_QUEUE_TIMEOUT   = float(os.getenv("PROXY_QUEUE_TIMEOUT", "120"))
CLUSTER_POLL_INTERVAL = float(os.getenv("CLUSTER_POLL_INTERVAL", "10"))

def _redis(): return _orch.REDIS

# Proxy state
PROXY_QUEUE: asyncio.Queue = asyncio.Queue(maxsize=50)
_proxy_active = 0
# Runtime soft-pause for the Ollama mimic proxy. When True, the proxy returns 503
# without forwarding — lets the operator stop accepting mimic traffic from the
# control page without restarting Vera (the routes stay mounted). PROXY_MAX_-
# CONCURRENCY is likewise mutable at runtime via ollama.proxy.config.
_proxy_paused = False


# ─────────────────────────────────────────────────────────────────────────────
# ENHANCED INSTANCE METRICS
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_instance_detail(iid: str, inst: dict):
    """Fetch /api/ps and /api/version for a node and update inst dict in-place."""
    url = inst.get("url", "")
    if not url:
        return

    # /api/version
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(f"{url}/api/version")
            if r.status_code == 200:
                inst["version"] = r.json().get("version", "")
    except Exception:
        inst.setdefault("version", "")

    # Detect the model's true context window (POST /api/show) so the cluster
    # panel shows the real number instead of a hand-set default. Detection is
    # cached in the orchestrator helper; we only fill num_ctx when it's unset.
    if not inst.get("num_ctx"):
        model = next(iter(inst.get("models") or []), "")
        if model:
            try:
                async with httpx.AsyncClient(timeout=6) as c:
                    r = await c.post(f"{url}/api/show", json={"model": model})
                    if r.status_code == 200:
                        ctx = _orch._extract_ctx_from_show(
                            r.json().get("model_info", {}) or {})
                        if ctx:
                            inst["num_ctx"] = ctx
            except Exception:
                pass

    # /api/ps — running models + VRAM (Ollama ≥ 0.3)
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(f"{url}/api/ps")
            if r.status_code == 200:
                running = r.json().get("models", [])
                inst["running"]      = [m.get("name","") for m in running]
                inst["vram_used_gb"] = round(
                    sum(m.get("size_vram", 0) for m in running) / 1e9, 2
                )
                inst["model_count"]  = len(running)
                inst["ps_raw"]       = [
                    {"name": m.get("name",""),
                     "vram_gb": round(m.get("size_vram",0)/1e9,2),
                     "expires": m.get("expires_at","")}
                    for m in running
                ]
                return
    except Exception:
        pass

    # /api/ps unavailable — clear stale values
    inst.setdefault("running",      [])
    inst.setdefault("vram_used_gb", 0)
    inst.setdefault("model_count",  0)
    inst.setdefault("ps_raw",       [])


async def cluster_poll_loop():
    """
    Background: enrich all Ollama nodes with PS metrics, publish snapshot to Redis.
    """
    while True:
        try:
            await asyncio.gather(
                *[_fetch_instance_detail(iid, inst)
                  for iid, inst in OLLAMA_INSTANCES.items()],
                return_exceptions=True
            )
        except Exception as e:
            log.debug("cluster_poll_loop gather: %s", e)

        # Publish snapshot
        r = _redis()
        if r:
            try:
                snapshot = {}
                for iid, inst in OLLAMA_INSTANCES.items():
                    snapshot[iid] = {
                        "id":           iid,
                        "label":        inst.get("label", iid),
                        "url":          inst.get("url", ""),
                        "status":       inst.get("status", "unknown"),
                        "has_gpu":      inst.get("has_gpu", False),
                        "enabled":      inst.get("enabled", True),
                        "latency_ms":   inst.get("latency_ms"),
                        "in_use":       inst.get("in_use", 0),
                        "models":       inst.get("models", []),
                        "running":      inst.get("running", []),
                        "vram_used_gb": inst.get("vram_used_gb", 0),
                        "model_count":  inst.get("model_count", 0),
                        "errors":       inst.get("errors", 0),
                        "version":      inst.get("version", ""),
                        "last_check":   inst.get("last_check", ""),
                        "proxy_queued": PROXY_QUEUE.qsize()
                                        if LOCAL_OLLAMA_INSTANCE == iid else 0,
                    }
                await r.set("vera:cluster:ollama",
                             json.dumps(snapshot), ex=60)

                # Also push to events for dashboards
                await emit_event({
                    "type": "cluster.ollama_snapshot",
                    "instances": {
                        iid: {
                            "status": d["status"],
                            "in_use": d["in_use"],
                            "vram":   d["vram_used_gb"],
                            "running": len(d["running"]),
                        }
                        for iid, d in snapshot.items()
                    }
                })
            except Exception as e:
                log.debug("cluster snapshot Redis: %s", e)

        await asyncio.sleep(CLUSTER_POLL_INTERVAL)


# ─────────────────────────────────────────────────────────────────────────────
# LOAD-AWARE ROUTING PATCH
# ─────────────────────────────────────────────────────────────────────────────

def _colocated_worker_load() -> Dict[str, float]:
    """
    For each Ollama instance, sum the extra load from Vera workers
    co-located on the same host and currently running tasks.
    Returns {instance_id: penalty_score}.
    """
    penalty: Dict[str, float] = {}
    for wid, w in _orch.WORKER_REGISTRY.items():
        inst_id = w.get("ollama_instance", "")
        status  = str(w.get("status", "idle"))
        if inst_id and status not in ("idle", "starting", "disabled", "draining", ""):
            # Running worker on this Ollama host — penalise it
            penalty[inst_id] = penalty.get(inst_id, 0) + 1.0
    return penalty


def _pick_instance_load_aware(
    prefer_gpu:  bool = False,
    instance_id: Optional[str] = None,
    model:       Optional[str] = None,
    job_type:    Optional[str] = None,
) -> Optional[str]:
    """
    Load-aware pick_instance.

    Effective score = in_use + colocated_worker_penalty * 0.5
                    + proxy_queue_depth * 0.2 + priority * 0.01
    Picks the online instance with the lowest score.

    Honours the same cluster-routing controls as the base pick_instance:
    disabled nodes (enabled=False) are excluded, and the active routing
    profile's rule for `job_type` constrains the candidate set (pin / deny_gpu
    / allow / deny / prefer_gpu) before load-aware scoring runs.
    """
    # Only online AND enabled nodes are routable.
    online = {iid: i for iid, i in OLLAMA_INSTANCES.items()
              if i.get("status") == "online" and i.get("enabled", True)}
    if not online:
        return None
    if instance_id and instance_id in online:
        return instance_id

    # ── Job-type routing rule (reuse the orchestrator's resolver) ──────────────
    _resolve_rule = getattr(_orch, "_resolve_rule", None)
    _match_glob   = getattr(_orch, "_match_glob", None)
    rule = _resolve_rule(job_type) if (job_type and _resolve_rule) else None
    if rule:
        pin = rule.get("pin") or ""
        if pin and pin in online:
            return pin
        if rule.get("deny_gpu"):
            online = {iid: i for iid, i in online.items() if not i.get("has_gpu")} or online
        allow = rule.get("allow") or []
        if allow and _match_glob:
            filt = {iid: i for iid, i in online.items()
                    if any(_match_glob(iid, p) for p in allow)}
            if filt:
                online = filt
        deny = rule.get("deny") or []
        if deny and _match_glob:
            filt = {iid: i for iid, i in online.items()
                    if not any(_match_glob(iid, p) for p in deny)}
            if filt:
                online = filt
        prefer_gpu = prefer_gpu or bool(rule.get("prefer_gpu"))

    colocated = _colocated_worker_load()

    def _score(iid: str, inst: dict) -> float:
        s  = inst.get("in_use", 0)
        s += colocated.get(iid, 0) * 0.5
        if iid == LOCAL_OLLAMA_INSTANCE:
            s += PROXY_QUEUE.qsize() * 0.2
        s += inst.get("priority", 0) * 0.01
        return s

    if prefer_gpu:
        gpu = {iid: i for iid, i in online.items() if i.get("has_gpu")}
        if gpu:
            return min(gpu, key=lambda k: _score(k, gpu[k]))

    if model:
        has = {iid: i for iid, i in online.items()
               if model in (i.get("models") or [])}
        if has:
            return min(has, key=lambda k: _score(k, has[k]))

    return min(online, key=lambda k: _score(k, online[k]))


# Patch the orchestrator
_orch.pick_instance = _pick_instance_load_aware
log.info("pick_instance patched → load-aware routing")


# ─────────────────────────────────────────────────────────────────────────────
# WORKER OLLAMA AFFINITY  —  detect which instance this host runs
# ─────────────────────────────────────────────────────────────────────────────

def _detect_local_ollama() -> str:
    """
    Detect which OLLAMA_INSTANCES entry corresponds to this host.
    Precedence: LOCAL_OLLAMA_INSTANCE env var → hostname IP match.
    """
    if LOCAL_OLLAMA_INSTANCE and LOCAL_OLLAMA_INSTANCE in OLLAMA_INSTANCES:
        return LOCAL_OLLAMA_INSTANCE
    import socket
    try:
        hostname = socket.gethostname()
        host_ips: set = set()
        try:
            host_ips.add(socket.gethostbyname(hostname))
        except Exception:
            pass
        try:
            for info in socket.getaddrinfo(hostname, None):
                host_ips.add(info[4][0])
        except Exception:
            pass
        for iid, inst in OLLAMA_INSTANCES.items():
            url = inst.get("url", "")
            if any(ip in url for ip in host_ips):
                return iid
    except Exception:
        pass
    return ""


LOCAL_OLLAMA_ID = _detect_local_ollama()
log.info("Host Ollama affinity: %s", LOCAL_OLLAMA_ID or "none detected")


async def _worker_affinity_loop():
    """
    Keep WORKER_REGISTRY[*].ollama_instance set to LOCAL_OLLAMA_ID.
    Runs periodically so workers that register after startup are also patched.
    """
    while True:
        r = _redis()
        for wid, w in list(_orch.WORKER_REGISTRY.items()):
            if w.get("ollama_instance") != LOCAL_OLLAMA_ID:
                w["ollama_instance"] = LOCAL_OLLAMA_ID
                if r:
                    try:
                        await r.hset(f"vera:workers:{wid}",
                                     "ollama_instance", LOCAL_OLLAMA_ID)
                    except Exception:
                        pass
        await asyncio.sleep(15)


# ─────────────────────────────────────────────────────────────────────────────
# OLLAMA PROXY
# ─────────────────────────────────────────────────────────────────────────────

async def _record_proxy_request(path: str, body: dict) -> str:
    """Fire-and-forget: log request to Redis stream + emit events for Jobs panel.

    Returns the req_id so callers can emit done/error events later.
    """
    import uuid as _uuid
    req_id = str(_uuid.uuid4())[:12]
    r = _redis()
    if not r:
        return req_id
    try:
        model  = body.get("model", "")
        msgs   = body.get("messages", [])
        prompt = (
            body.get("prompt") or
            (msgs[-1].get("content", "") if msgs else "")
        )[:400]
        prompt_preview = prompt[:120].replace("\n", " ")
        await r.xadd("vera:ollama_proxy_log", {
            "data": json.dumps({
                "type":     "proxy_request",
                "path":     path,
                "model":    model,
                "prompt":   prompt,
                "instance": LOCAL_OLLAMA_ID,
                "ts":       now_iso(),
            })
        }, maxlen=1000)
        await emit_event({
            "type":       "ollama.proxy_request",
            "capability": "ollama.proxy",
            "model":      model,
            "text":       prompt,
            "source_type":"tool",
            "category":   "ollama_proxy",
            "tags":       ["proxy", "ollama", model],
            "importance": 0.25,
        })
        # Also emit ollama.request so the Jobs panel tracks this as a job
        inst = OLLAMA_INSTANCES.get(LOCAL_OLLAMA_ID, {})
        await emit_event({
            "type":         "ollama.request",
            "req_id":       req_id,
            "model":        model,
            "instance_id":  LOCAL_OLLAMA_ID,
            "instance_url": inst.get("url", ""),
            "caller_file":  "cluster.py",
            "caller_func":  "ollama_proxy",
            "caller_module": "cluster",
            "cap_name":     "ollama.proxy",
            "prompt_preview": f"[proxy] {prompt_preview}",
            "json_mode":    False,
            "prefer_gpu":   False,
            "streaming":    body.get("stream", False),
        })
    except Exception as e:
        log.debug("proxy record: %s", e)
    return req_id


async def _forward(path: str, body: dict, stream: bool):
    """Forward to local Ollama; return FastAPI response."""
    global _proxy_active
    if LOCAL_OLLAMA_ID not in OLLAMA_INSTANCES:
        return JSONResponse({"error": "No local Ollama configured"}, 503)

    target = f"{OLLAMA_INSTANCES[LOCAL_OLLAMA_ID]['url']}/{path}"
    inst   = OLLAMA_INSTANCES[LOCAL_OLLAMA_ID]
    inst["in_use"] = inst.get("in_use", 0) + 1
    _proxy_active += 1
    _t0 = time.time()
    _req_id_task = asyncio.create_task(_record_proxy_request(path, body))

    try:
        if stream:
            async def _gen():
                async with httpx.AsyncClient(timeout=300) as c:
                    async with c.stream("POST", target, json=body) as resp:
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                # Emit done event after stream completes
                try:
                    _req_id = _req_id_task.result() if _req_id_task.done() else ""
                    if _req_id:
                        await emit_event({
                            "type": "ollama.request_done", "req_id": _req_id,
                            "model": body.get("model", ""), "instance_id": LOCAL_OLLAMA_ID,
                            "caller_file": "cluster.py", "caller_func": "ollama_proxy",
                            "elapsed_s": round(time.time() - _t0, 2),
                        })
                except Exception:
                    pass
            return StreamingResponse(_gen(), media_type="application/x-ndjson")
        else:
            async with httpx.AsyncClient(timeout=300) as c:
                r = await c.post(target, json=body)
            # Emit done event for non-streaming
            try:
                _req_id = await _req_id_task
                elapsed = round(time.time() - _t0, 2)
                if r.status_code == 200:
                    await emit_event({
                        "type": "ollama.request_done", "req_id": _req_id,
                        "model": body.get("model", ""), "instance_id": LOCAL_OLLAMA_ID,
                        "caller_file": "cluster.py", "caller_func": "ollama_proxy",
                        "elapsed_s": elapsed,
                    })
                else:
                    await emit_event({
                        "type": "ollama.request_error", "req_id": _req_id,
                        "model": body.get("model", ""), "instance_id": LOCAL_OLLAMA_ID,
                        "caller_file": "cluster.py", "caller_func": "ollama_proxy",
                        "elapsed_s": elapsed, "error": f"http_{r.status_code}",
                    })
            except Exception:
                pass
            return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        log.error("Proxy forward error [%s]: %s", path, e)
        # Emit error event
        try:
            _req_id = await _req_id_task
            await emit_event({
                "type": "ollama.request_error", "req_id": _req_id,
                "model": body.get("model", ""), "instance_id": LOCAL_OLLAMA_ID,
                "caller_file": "cluster.py", "caller_func": "ollama_proxy",
                "elapsed_s": round(time.time() - _t0, 2), "error": str(e)[:200],
            })
        except Exception:
            pass
        return JSONResponse({"error": str(e)}, status_code=502)
    finally:
        _proxy_active -= 1
        inst["in_use"] = max(0, inst.get("in_use", 1) - 1)


async def _proxy_handler(path: str, req: Request):
    if not LOCAL_OLLAMA_ID:
        return JSONResponse({
            "error": "Proxy inactive — set LOCAL_OLLAMA_INSTANCE env var"
        }, status_code=503)

    if _proxy_paused:
        return JSONResponse({
            "error": "Ollama mimic proxy is paused"
        }, status_code=503)

    try:
        body = await req.json()
    except Exception:
        body = {}

    stream  = body.get("stream", False)
    in_use  = OLLAMA_INSTANCES.get(LOCAL_OLLAMA_ID, {}).get("in_use", 0)

    if in_use < PROXY_MAX_CONCURRENCY:
        return await _forward(path, body, stream)

    # Queue
    log.info("Ollama proxy: queuing (in_use=%d)", in_use)
    loop   = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    try:
        PROXY_QUEUE.put_nowait((path, body, stream, future))
    except asyncio.QueueFull:
        return JSONResponse({"error": "Proxy queue full"}, status_code=429)

    try:
        return await asyncio.wait_for(future, timeout=PROXY_QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "Proxy queue timeout"}, status_code=504)


async def _proxy_queue_worker():
    """Drain the proxy queue as capacity becomes available."""
    while True:
        try:
            path, body, stream, future = await asyncio.wait_for(
                PROXY_QUEUE.get(), timeout=5
            )
        except asyncio.TimeoutError:
            continue
        except Exception:
            await asyncio.sleep(1)
            continue

        # Wait for a free slot
        while (OLLAMA_INSTANCES.get(LOCAL_OLLAMA_ID, {}).get("in_use", 0)
               >= PROXY_MAX_CONCURRENCY):
            await asyncio.sleep(0.25)

        try:
            result = await _forward(path, body, stream)
            if not future.done():
                future.set_result(result)
        except Exception as e:
            if not future.done():
                future.set_exception(e)
        finally:
            PROXY_QUEUE.task_done()


# Mount proxy routes (only when LOCAL_OLLAMA_ID is set)
if LOCAL_OLLAMA_ID:
    log.info("Mounting Ollama proxy /ollama/* → %s (%s)",
             LOCAL_OLLAMA_ID,
             OLLAMA_INSTANCES.get(LOCAL_OLLAMA_ID, {}).get("url", "?"))

    @APP.post("/ollama/{path:path}")
    async def _ollama_proxy_post(path: str, req: Request):
        return await _proxy_handler(path, req)

    @APP.get("/ollama/{path:path}")
    async def _ollama_proxy_get(path: str, req: Request):
        if not LOCAL_OLLAMA_ID:
            return JSONResponse({"error": "Proxy not configured"}, 503)
        target = f"{OLLAMA_INSTANCES[LOCAL_OLLAMA_ID]['url']}/{path}"
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(target)
            return JSONResponse(r.json(), status_code=r.status_code)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)


# ─────────────────────────────────────────────────────────────────────────────
# OLLAMA MIMIC PROXY — monitor + control
# ─────────────────────────────────────────────────────────────────────────────
# The mimic exposes an Ollama-compatible API at /ollama/* (this is what lets Vera
# "mimic Ollama"); every request is logged to the request log + emitted as an
# ollama.request event so prompts/jobs submitted this way are observable. These
# capabilities back the "Mimic" page in the Workers & Ollama panel.
#
# NOTE on paths: when the mimic is active, the catch-all `/ollama/{path:path}`
# proxy routes (registered at import) shadow any exact `/ollama/...` route mounted
# later by _mount_all_http_routes. So the monitor/control endpoints deliberately
# live under `/cluster/mimic/*` (no catch-all there) — they keep working whether
# or not the mimic is forwarding traffic. The cap names mirror the paths so the
# auto-POST fallback doesn't also mount a shadowed `/ollama/...` alias.

@capability(
    "cluster.mimic.status", memory="off", silent=True,
    http_method="GET", http_path="/cluster/mimic/status", http_tags=["cluster"],
    description="Live status of the Ollama mimic proxy: whether it's mounted, "
                "paused, the local instance it targets, queue depth, active "
                "in-flight requests and the concurrency cap. Output: {mounted, "
                "paused, local_instance, active, queue_depth, queue_max, "
                "max_concurrency, queue_timeout}.")
async def cap_mimic_status(trace_id=None) -> Dict:
    return {
        "mounted":         bool(LOCAL_OLLAMA_ID),
        "paused":          _proxy_paused,
        "local_instance":  LOCAL_OLLAMA_ID,
        "active":          _proxy_active,
        "queue_depth":     PROXY_QUEUE.qsize(),
        "queue_max":       PROXY_QUEUE.maxsize,
        "max_concurrency": PROXY_MAX_CONCURRENCY,
        "queue_timeout":   PROXY_QUEUE_TIMEOUT,
    }


@capability(
    "cluster.mimic.config", memory="off",
    http_method="POST", http_path="/cluster/mimic/config", http_tags=["cluster"],
    description="Control the Ollama mimic proxy at runtime. Inputs: paused (bool "
                "— stop/resume forwarding without restart), max_concurrency (int "
                ">=1, simultaneous forwards before queuing). Omitted fields are "
                "left unchanged. Output: the same shape as cluster.mimic.status.")
async def cap_mimic_config(paused: Optional[bool] = None,
                           max_concurrency: Optional[int] = None,
                           trace_id=None) -> Dict:
    global _proxy_paused, PROXY_MAX_CONCURRENCY
    if paused is not None:
        _proxy_paused = bool(paused)
    if max_concurrency is not None:
        try:
            PROXY_MAX_CONCURRENCY = max(1, int(max_concurrency))
        except (TypeError, ValueError):
            return {"error": "max_concurrency must be an integer >= 1"}
    await emit_event({"type": "ollama.proxy.config",
                      "paused": _proxy_paused,
                      "max_concurrency": PROXY_MAX_CONCURRENCY})
    return await cap_mimic_status()


@capability(
    "cluster.mimic.requests", memory="off", silent=True,
    http_method="GET", http_path="/cluster/mimic/requests", http_tags=["cluster"],
    description="Recent prompts/jobs submitted through the mimic (newest first) "
                "from the in-memory Ollama request log. Query: limit (int, default "
                "100). Output: {entries:[...], total}. A non-shadowed mirror of "
                "/ollama/request_log that keeps working while the mimic forwards.")
async def cap_mimic_requests(limit: int = 100, trace_id=None) -> Dict:
    rl = getattr(_orch, "_OLLAMA_REQUEST_LOG", [])
    entries = list(reversed(rl))
    return {"entries": entries[:max(1, limit)], "total": len(rl)}


@capability(
    "cluster.mimic.requests.clear", memory="off",
    http_method="POST", http_path="/cluster/mimic/requests/clear", http_tags=["cluster"],
    description="Clear the in-memory Ollama request log (prompts/jobs submitted "
                "through the mimic + ollama_generate). Output: {ok, cleared}.")
async def cap_mimic_requests_clear(trace_id=None) -> Dict:
    n = len(getattr(_orch, "_OLLAMA_REQUEST_LOG", []))
    try:
        _orch._OLLAMA_REQUEST_LOG.clear()
    except Exception as e:
        return {"error": str(e)}
    return {"ok": True, "cleared": n}


_MIMIC_PANEL_PATH = os.path.join(os.path.dirname(__file__), "ollama_mimic_panel.html")


@APP.get("/cluster/mimic/panel", include_in_schema=False)
async def _ollama_mimic_panel():
    """Standalone monitor+control page for the Ollama mimic proxy (iframed by the
    Workers & Ollama panel's 'Mimic' pane)."""
    from fastapi.responses import HTMLResponse
    if os.path.exists(_MIMIC_PANEL_PATH):
        with open(_MIMIC_PANEL_PATH, encoding="utf-8") as fh:
            return HTMLResponse(fh.read())
    return HTMLResponse("<p style='color:#c96b6b'>ollama_mimic_panel.html not found</p>",
                        status_code=404)


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "obs.cluster", memory="off",
    http_method="GET", http_path="/cluster", http_tags=["obs"],
    description="Full cluster view: all workers cross-referenced with their Ollama nodes. "
                "Includes VRAM, running models, queue depth, and task duration.",
)
async def obs_cluster(trace_id=None):
    # ── Workers (from fixed obs_workers logic) ───────────────────────────────
    from Vera.vera.capability_orchestration import obs_workers as _obs_workers   # noqa
    workers_raw = await _obs_workers()

    # ── Ollama snapshot (prefer enriched cache) ───────────────────────────────
    ollama_snapshot = {}
    r = _redis()
    if r:
        try:
            cached = await r.get("vera:cluster:ollama")
            if cached:
                ollama_snapshot = json.loads(cached)
        except Exception:
            pass

    if not ollama_snapshot:
        # Build live
        for iid, inst in OLLAMA_INSTANCES.items():
            ollama_snapshot[iid] = {
                "id": iid, "label": inst.get("label", iid),
                "status": inst.get("status", "unknown"),
                "has_gpu": inst.get("has_gpu", False),
                "enabled": inst.get("enabled", True),
                "latency_ms": inst.get("latency_ms"),
                "in_use": inst.get("in_use", 0),
                "models": inst.get("models", []),
                "running": inst.get("running", []),
                "vram_used_gb": inst.get("vram_used_gb", 0),
                "errors": inst.get("errors", 0),
                "version": inst.get("version", ""),
                "url": inst.get("url", ""),
                "last_check": inst.get("last_check", ""),
            }

    # ── Redis streams info ────────────────────────────────────────────────────
    queue_info = {"task_queue_len": 0, "result_queue_len": 0, "pending": 0}
    if r:
        try:
            tlen = await r.xlen(_orch.TASK_STREAM)
            rlen = await r.xlen(_orch.RESULT_STREAM)
            # Pending (delivered but not acked)
            pinfo = await r.xpending(_orch.TASK_STREAM, _orch.GROUP_WORKERS)
            queue_info = {
                "task_queue_len":   tlen,
                "result_queue_len": rlen,
                "pending_tasks":    pinfo.get("pending", 0) if isinstance(pinfo, dict) else 0,
            }
        except Exception as e:
            log.debug("queue_info: %s", e)

    # ── Proxy stats ───────────────────────────────────────────────────────────
    proxy_info = {
        "active":         _proxy_active,
        "local_instance": LOCAL_OLLAMA_ID,
        "queue_depth":    PROXY_QUEUE.qsize(),
        "max_concurrency": PROXY_MAX_CONCURRENCY,
        "enabled":        bool(LOCAL_OLLAMA_ID),
    }

    # ── Enrich workers with their Ollama node data ────────────────────────────
    workers_enriched = {}
    for wid, w in workers_raw.items():
        ollama_id = w.get("ollama_instance", "")
        w["ollama_node"] = ollama_snapshot.get(ollama_id) if ollama_id else None
        # Compute task duration if running
        ts = w.get("task_started", "")
        if ts and w.get("current_task"):
            try:
                from datetime import datetime, timezone
                started  = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                now_dt   = datetime.now(timezone.utc)
                w["task_duration_s"] = round((now_dt - started).total_seconds(), 1)
            except Exception:
                w["task_duration_s"] = None
        else:
            w["task_duration_s"] = None
        workers_enriched[wid] = w

    return {
        "workers":  workers_enriched,
        "ollama":   ollama_snapshot,
        "queues":   queue_info,
        "proxy":    proxy_info,
        "local_ollama_id": LOCAL_OLLAMA_ID,
        "ts":       now_iso(),
    }


@capability(
    "obs.proxy_log", memory="off",
    http_method="GET", http_path="/cluster/proxy_log", http_tags=["obs"],
    description="Recent requests through the local Ollama proxy.",
)
async def obs_proxy_log(limit: int = 50, trace_id=None):
    r = _redis()
    if not r:
        return {"log": [], "count": 0}
    try:
        entries = await r.xrevrange("vera:ollama_proxy_log", count=limit)
        records = []
        for _id, data in entries:
            try:
                raw = data.get(b"data", b"{}")
                records.append(json.loads(
                    raw.decode() if isinstance(raw, bytes) else raw
                ))
            except Exception:
                pass
        return {"log": records, "count": len(records)}
    except Exception as e:
        return {"log": [], "error": str(e)}


@capability(
    "cluster.job.stop", memory="off",
    http_method="POST", http_path="/cluster/job/stop", http_tags=["cluster"],
    description="Stop a job by task_id. If it is running, cancel it cooperatively "
                "(works across hosts via a Redis cancel broadcast — cancellation "
                "takes effect at the cap's next await). If it is still queued, it is "
                "marked so the worker discards it instead of starting it. "
                "Input: task_id (str!). Output: {ok, task_id, action}.",
)
async def cap_cluster_job_stop(task_id: str = "", trace_id=None):
    if not task_id:
        return {"ok": False, "error": "task_id required"}

    action = "queued_cancel"   # default: prevent a not-yet-started job from running

    # 1. Cancel directly if it's running in THIS process.
    t = _orch.RUNNING_TASKS.get(task_id)
    if t and not t.done():
        t.cancel()
        action = "cancelled_running"

    # 2. Mark cancelled locally (queued guard) + broadcast to all hosts so the
    #    process actually running it (or about to) cancels/discards it.
    _orch.CANCELLED_TASKS.add(task_id)
    r = _redis()
    if r:
        try:
            await r.sadd(_orch.REDIS_CANCEL_SET, task_id)
            await r.expire(_orch.REDIS_CANCEL_SET, 3600)
            await r.publish(_orch.REDIS_CANCEL_CHANNEL, task_id)
        except Exception as e:
            log.debug("cluster.job.stop redis: %s", e)

    # 3. Unblock any local caller awaiting this task's result future.
    fut = _orch.PENDING_RESULTS.get(task_id)
    if fut and not fut.done():
        fut.set_result({"error": "cancelled", "cancelled": True})

    await emit_event({"type": "worker.cancelled", "task": task_id, "via": "job.stop"})
    return {"ok": True, "task_id": task_id, "action": action}


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────────────────────

async def _startup():
    asyncio.create_task(cluster_poll_loop())
    asyncio.create_task(_worker_affinity_loop())
    if LOCAL_OLLAMA_ID:
        asyncio.create_task(_proxy_queue_worker())
        log.info("Ollama proxy queue worker started")
    log.info("vera_cluster ready — local_ollama=%s  proxy=%s",
             LOCAL_OLLAMA_ID or "none",
             "enabled" if LOCAL_OLLAMA_ID else "disabled")


schedule(_startup, interval=999999, name="cluster_startup")
try:
    _loop = asyncio.get_event_loop()
    if _loop.is_running():
        _loop.create_task(_startup())
except Exception:
    pass