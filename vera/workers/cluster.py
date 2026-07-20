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
import ssl
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

# One shared SSL context reused by every httpx.AsyncClient() in this module.
# Without it, httpx calls ssl.create_default_context(cafile=certifi.where()) on
# EVERY client construction — that cafile read is blocking disk I/O, and on the
# event loop it surfaces as multi-hundred-ms watchdog hangs (ssl.py:770
# load_verify_locations), multiplied across every polled node. Build it once at
# import; pass verify=_SSL_CTX to each AsyncClient so the read never recurs.
try:
    import certifi
    _SSL_CTX: Optional[ssl.SSLContext] = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover - fall back to httpx's per-call default
    _SSL_CTX = None

# ── Config ────────────────────────────────────────────────────────────────────
LOCAL_OLLAMA_INSTANCE = os.getenv("LOCAL_OLLAMA_INSTANCE", "")
PROXY_MAX_CONCURRENCY = int(os.getenv("PROXY_MAX_CONCURRENCY", "3"))
PROXY_QUEUE_TIMEOUT   = float(os.getenv("PROXY_QUEUE_TIMEOUT", "120"))
CLUSTER_POLL_INTERVAL = float(os.getenv("CLUSTER_POLL_INTERVAL", "10"))

def _redis(): return _orch.REDIS

# Proxy state — PER-NODE queues. A single global queue head-of-line-blocked the
# whole proxy: its one worker polled up to PROXY_QUEUE_TIMEOUT for a busy CPU
# node's slot while requests routed to the (free) GPU node sat behind it. Each
# node now has its own queue + drain worker (created lazily), and
# PROXY_MAX_CONCURRENCY gates each NODE's in-flight count independently.
PROXY_QUEUE_MAX = int(os.getenv("PROXY_QUEUE_MAX", "50"))
PROXY_QUEUES: Dict[str, asyncio.Queue] = {}
_PROXY_QUEUE_WORKERS: Dict[str, "asyncio.Task"] = {}
_proxy_active = 0


def _proxy_queue(target: str) -> asyncio.Queue:
    """This node's proxy queue, creating it (and its drain worker) on first use.
    The worker is also restarted here if it ever crashed."""
    q = PROXY_QUEUES.get(target)
    if q is None:
        q = PROXY_QUEUES[target] = asyncio.Queue(maxsize=PROXY_QUEUE_MAX)
    t = _PROXY_QUEUE_WORKERS.get(target)
    if t is None or t.done():
        _PROXY_QUEUE_WORKERS[target] = asyncio.create_task(_proxy_queue_worker(target))
    return q


def _proxy_queue_depth(target: str) -> int:
    q = PROXY_QUEUES.get(target)
    return q.qsize() if q is not None else 0


def _proxy_qsize_total() -> int:
    return sum(q.qsize() for q in PROXY_QUEUES.values())
# Runtime soft-pause for the Ollama mimic proxy. When True, the proxy returns 503
# without forwarding — lets the operator stop accepting mimic traffic from the
# control page without restarting Vera (the routes stay mounted). PROXY_MAX_-
# CONCURRENCY is likewise mutable at runtime via ollama.proxy.config.
_proxy_paused = False
# Routing mode for the mimic. True (default) = pin to GPU: each proxied request
# flows through the cluster's load-aware pick_instance with prefer_gpu, so GPU
# nodes are preferred (and load-balanced if there are several), falling back to
# any online node. False = pure load-aware routing across all nodes. Toggle at
# runtime via cluster.mimic.config.
_PROXY_PREFER_GPU = True


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
        async with httpx.AsyncClient(timeout=4, verify=_SSL_CTX or True) as c:
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
                async with httpx.AsyncClient(timeout=6, verify=_SSL_CTX or True) as c:
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
        async with httpx.AsyncClient(timeout=4, verify=_SSL_CTX or True) as c:
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
                        "proxy_queued": _proxy_queue_depth(iid),
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
    rule_override: Optional[dict] = None,
    explain:     Optional[dict] = None,
    **_kw,
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
    # Decision trail — populated into `explain` so the ollama_req log line shows
    # WHY a node was chosen (the base pick_instance does this; this patch
    # REPLACES it, so it must too or every route reason is blank in the logs).
    trail = []
    def _note(m): trail.append(m)
    def _out(chosen):
        if explain is not None:
            explain["reason"] = trail
            explain["chosen"] = chosen
        return chosen

    # Only online AND enabled nodes are routable.
    online = {iid: i for iid, i in OLLAMA_INSTANCES.items()
              if i.get("status") == "online" and i.get("enabled", True)}
    if not online:
        _note("no online nodes")
        return _out(None)
    if instance_id and instance_id in online:
        _note(f"caller pinned '{instance_id}'")
        return _out(instance_id)

    # ── Job-type routing rule (reuse the orchestrator's resolver) ──────────────
    # Honour a caller-supplied per-cap rule override (as the base pick_instance
    # does), else fall back to the active profile's job-type rule. `explain` and
    # any future kwargs are accepted so this monkey-patch stays signature-
    # compatible with pick_instance (a prior drift here crashed EVERY LLM call).
    _resolve_rule = getattr(_orch, "_resolve_rule", None)
    _match_glob   = getattr(_orch, "_match_glob", None)
    rule = rule_override if rule_override else (
        _resolve_rule(job_type) if (job_type and _resolve_rule) else None)
    if rule:
        _note(f"job-type rule '{job_type}' "
              f"(deny_gpu={bool(rule.get('deny_gpu'))}, "
              f"avoid_embed={bool(rule.get('avoid_embed'))}, "
              f"pin={rule.get('pin') or '-'})")
        pin = rule.get("pin") or ""
        if pin and pin in online:
            _note(f"rule pin → {pin}")
            return _out(pin)
        if rule.get("deny_gpu"):
            nong = {iid: i for iid, i in online.items() if not i.get("has_gpu")}
            if nong:
                online = nong; _note(f"deny_gpu → {sorted(online)}")
            else:
                _note("deny_gpu: no non-GPU node online — keeping GPU nodes")
        allow = rule.get("allow") or []
        if allow and _match_glob:
            filt = {iid: i for iid, i in online.items()
                    if any(_match_glob(iid, p) for p in allow)}
            if filt:
                online = filt; _note(f"allow {allow} → {sorted(online)}")
        deny = rule.get("deny") or []
        if deny and _match_glob:
            filt = {iid: i for iid, i in online.items()
                    if not any(_match_glob(iid, p) for p in deny)}
            if filt:
                online = filt; _note(f"deny {deny} → {sorted(online)}")
        # avoid_embed: keep this job type off the live embedding node (mirrors
        # the base pick_instance — this patch REPLACES it, so the flag must be
        # honoured here too or rules like dream_director's silently lose it).
        if rule.get("avoid_embed"):
            _embed_id = ""
            try:
                _embed_id = getattr(_orch, "_embed_node_id", lambda: "")()
            except Exception:
                pass
            if not _embed_id:
                _note("avoid_embed: embed node UNRESOLVED (no pin / no "
                      "OLLAMA_EMBED_URL match) — cannot exclude, staying put")
            elif _embed_id not in online:
                _note(f"avoid_embed: embed node '{_embed_id}' not a candidate — noop")
            elif len(online) <= 1:
                _note(f"avoid_embed: '{_embed_id}' is the ONLY candidate — "
                      "keeping it (nothing else to route to)")
            else:
                online = {iid: i for iid, i in online.items() if iid != _embed_id}
                _note(f"avoid_embed: excluded '{_embed_id}' → {sorted(online)}")
        prefer_gpu = prefer_gpu or bool(rule.get("prefer_gpu"))
    elif job_type:
        _note(f"no rule for job-type '{job_type}'")

    colocated = _colocated_worker_load()

    def _score(iid: str, inst: dict) -> float:
        s  = inst.get("in_use", 0)
        s += colocated.get(iid, 0) * 0.5
        s += _proxy_queue_depth(iid) * 0.2   # this node's own proxy backlog
        s += inst.get("priority", 0) * 0.01
        return s

    def _has_model(inst: dict) -> bool:
        # Flexible name match (mirrors the base pick_instance): exact, tag
        # prefix, or same base name.
        if not model:
            return True
        base = model.split(":")[0]
        for m in (inst.get("models") or []):
            if m == model or m.startswith(model + ":") or m.split(":")[0] == base:
                return True
        return False

    def _best(cands, why):
        chosen = min(cands, key=lambda k: _score(k, cands[k]))
        _note(f"{why}: picked '{chosen}' (in_use={cands[chosen].get('in_use',0)}) "
              f"from {sorted(cands)}")
        return _out(chosen)

    if prefer_gpu:
        gpu = {iid: i for iid, i in online.items() if i.get("has_gpu")}
        gpu_model = {iid: i for iid, i in gpu.items() if _has_model(i)}
        if gpu_model:
            return _best(gpu_model, "prefer_gpu + model")
        # No GPU node has this model: a node that HAS it beats a GPU node that
        # must cold-pull/load it — routing a chat to a modelless GPU node used
        # to stall the request for minutes while Ollama fetched the model.
        if model:
            has = {iid: i for iid, i in online.items() if _has_model(i)}
            if has:
                return _best(has, "prefer_gpu: no GPU has model — node with model")
        if gpu:
            return _best(gpu, "prefer_gpu: model nowhere — any GPU")

    if model:
        has = {iid: i for iid, i in online.items() if _has_model(i)}
        if has:
            return _best(has, "node with model")
        _note(f"model '{model}' on NONE of {sorted(online)} — least-busy fallback "
              "(node will cold-pull the model)")

    return _best(online, "least busy")


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

async def _record_proxy_request(target: str, path: str, body: dict) -> str:
    """Fire-and-forget: log request to Redis stream + emit events for Jobs panel.

    `target` is the node the request was routed to. Returns the req_id so callers
    can emit done/error events later.
    """
    import uuid as _uuid
    req_id = str(_uuid.uuid4())[:12]
    r = _redis()
    if not r:
        return req_id
    try:
        model  = body.get("model", "")
        msgs   = body.get("messages", [])
        prompt_full = (
            body.get("prompt") or
            (msgs[-1].get("content", "") if msgs else "")
        )
        prompt = prompt_full[:400]
        prompt_preview = prompt[:120].replace("\n", " ")
        await r.xadd("vera:ollama_proxy_log", {
            "data": json.dumps({
                "type":     "proxy_request",
                "path":     path,
                "model":    model,
                "prompt":   prompt,
                "instance": target,
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
        inst = OLLAMA_INSTANCES.get(target, {})
        await emit_event({
            "type":         "ollama.request",
            "req_id":       req_id,
            "model":        model,
            "instance_id":  target,
            "instance_url": inst.get("url", ""),
            "caller_file":  "cluster.py",
            "caller_func":  "ollama_proxy",
            "caller_module": "cluster",
            "cap_name":     "ollama.proxy",
            "prompt_preview": f"[proxy] {prompt_preview}",
            "prompt_full":  prompt_full[:16000],
            "json_mode":    False,
            "prefer_gpu":   False,
            "streaming":    body.get("stream", False),
        })
    except Exception as e:
        log.debug("proxy record: %s", e)
    return req_id


def _online_nodes() -> List[str]:
    return [iid for iid, i in OLLAMA_INSTANCES.items()
            if i.get("status") == "online" and i.get("enabled", True)]


def _gpu_nodes_online() -> List[str]:
    return [iid for iid in _online_nodes() if OLLAMA_INSTANCES[iid].get("has_gpu")]


def _mimic_rule() -> Optional[dict]:
    """The mimic's routing-table rule ('mimic.proxy' — user rules win over the
    declared baseline), so proxied traffic is governed by the Model Routing
    page like every other caller: pin/allow/deny/prefer_gpu/model."""
    try:
        return _orch._resolve_cap_routing("mimic.proxy")
    except Exception:
        return None


def _pick_proxy_target(body: dict) -> Optional[str]:
    """Choose the Ollama node for a proxied request.

    Resolves the 'mimic.proxy' rule from the Model Routing table (pin / allow /
    deny / prefer_gpu / model apply), then flows through the cluster's
    load-aware pick_instance (multiple GPU nodes load-balance; disabled/offline
    nodes are skipped). With _PROXY_PREFER_GPU the selection is pinned to GPU
    nodes. Falls back to a GPU node, then any online node, then LOCAL_OLLAMA_ID.
    """
    model = (body or {}).get("model") or None
    rule = _mimic_rule()
    if rule and not model:
        model = rule.get("model") or None
    pick = getattr(_orch, "pick_instance", None)
    if pick:
        try:
            t = pick(prefer_gpu=_PROXY_PREFER_GPU, model=model,
                     job_type=(rule or {}).get("job_type") or "proxy",
                     rule_override=rule)
            if t and t in OLLAMA_INSTANCES:
                return t
        except Exception as e:
            log.debug("proxy pick_instance: %s", e)
    gpu = _gpu_nodes_online()
    if gpu:
        return gpu[0]
    online = _online_nodes()
    if online:
        return online[0]
    return LOCAL_OLLAMA_ID or None


async def _forward(target_id: str, path: str, body: dict, stream: bool):
    """Forward a proxied request to the routed Ollama node; return FastAPI response."""
    global _proxy_active
    if not target_id or target_id not in OLLAMA_INSTANCES:
        return JSONResponse({"error": "No online Ollama node to route to"}, 503)

    inst   = OLLAMA_INSTANCES[target_id]
    target = f"{inst['url']}/{path}"
    # Keep upstream in lockstep with the branch we take: if we picked the
    # non-streaming path (we'll call r.json()), the upstream MUST return a
    # single JSON object. Ollama defaults a missing "stream" to True, so we
    # set it explicitly to avoid getting NDJSON back and failing r.json().
    body = {**body, "stream": stream}
    inst["in_use"] = inst.get("in_use", 0) + 1
    _proxy_active += 1
    _t0 = time.time()
    _req_id_task = asyncio.create_task(_record_proxy_request(target_id, path, body))

    # Release the node slot exactly once. For STREAMING responses the actual
    # work happens after this function returns (the generator runs during ASGI
    # send) — releasing in the finally below would free the slot while the node
    # is still generating, letting PROXY_MAX_CONCURRENCY over-admit. The stream
    # generator owns the release in that case; every other path releases here.
    _released = {"v": False}
    def _release_slot():
        global _proxy_active
        if not _released["v"]:
            _released["v"] = True
            _proxy_active = max(0, _proxy_active - 1)
            inst["in_use"] = max(0, inst.get("in_use", 1) - 1)

    _stream_owns_release = False
    try:
        if stream:
            async def _gen():
                try:
                    try:
                        async with httpx.AsyncClient(timeout=300, verify=_SSL_CTX or True) as c:
                            async with c.stream("POST", target, json=body) as resp:
                                async for chunk in resp.aiter_bytes():
                                    yield chunk
                    except Exception as _se:
                        # Yield an Ollama-style error line rather than abruptly closing
                        # the socket (which surfaces to clients as "socket hang up").
                        log.error("proxy stream error [%s→%s]: %s", path, target_id, _se)
                        yield (json.dumps({"error": str(_se), "done": True}) + "\n").encode()
                finally:
                    _release_slot()
                # Emit done event after stream completes
                try:
                    _req_id = _req_id_task.result() if _req_id_task.done() else ""
                    if _req_id:
                        await emit_event({
                            "type": "ollama.request_done", "req_id": _req_id,
                            "model": body.get("model", ""), "instance_id": target_id,
                            "caller_file": "cluster.py", "caller_func": "ollama_proxy",
                            "elapsed_s": round(time.time() - _t0, 2),
                        })
                except Exception:
                    pass
            resp_obj = StreamingResponse(_gen(), media_type="application/x-ndjson")
            _stream_owns_release = True
            return resp_obj
        else:
            async with httpx.AsyncClient(timeout=300, verify=_SSL_CTX or True) as c:
                r = await c.post(target, json=body)
            # Emit done event for non-streaming
            try:
                _req_id = await _req_id_task
                elapsed = round(time.time() - _t0, 2)
                if r.status_code == 200:
                    await emit_event({
                        "type": "ollama.request_done", "req_id": _req_id,
                        "model": body.get("model", ""), "instance_id": target_id,
                        "caller_file": "cluster.py", "caller_func": "ollama_proxy",
                        "elapsed_s": elapsed,
                    })
                else:
                    await emit_event({
                        "type": "ollama.request_error", "req_id": _req_id,
                        "model": body.get("model", ""), "instance_id": target_id,
                        "caller_file": "cluster.py", "caller_func": "ollama_proxy",
                        "elapsed_s": elapsed, "error": f"http_{r.status_code}",
                    })
            except Exception:
                pass
            return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        log.error("Proxy forward error [%s→%s]: %s", path, target_id, e)
        # Emit error event
        try:
            _req_id = await _req_id_task
            await emit_event({
                "type": "ollama.request_error", "req_id": _req_id,
                "model": body.get("model", ""), "instance_id": target_id,
                "caller_file": "cluster.py", "caller_func": "ollama_proxy",
                "elapsed_s": round(time.time() - _t0, 2), "error": str(e)[:200],
            })
        except Exception:
            pass
        return JSONResponse({"error": str(e)}, status_code=502)
    finally:
        if not _stream_owns_release:
            _release_slot()


async def _proxy_handler(path: str, req: Request):
    if _proxy_paused:
        return JSONResponse({
            "error": "Ollama mimic proxy is paused"
        }, status_code=503)

    try:
        body = await req.json()
    except Exception:
        body = {}

    # Routing-table integration: the 'mimic.proxy' rule's model pin fills in
    # when the client didn't name a model (the client's explicit model wins).
    _rule = _mimic_rule()
    if _rule and _rule.get("model") and not body.get("model"):
        body["model"] = _rule["model"]

    target = _pick_proxy_target(body)
    if not target:
        return JSONResponse({
            "error": "No online Ollama node to route to"
        }, status_code=503)

    # Ollama's API treats a missing "stream" as True. Mirror that so a client
    # (e.g. Continue) that omits the flag gets real streaming instead of the
    # proxy taking the non-stream path and trying to r.json() an NDJSON body.
    stream  = body.get("stream", True)
    in_use  = OLLAMA_INSTANCES.get(target, {}).get("in_use", 0)

    if in_use < PROXY_MAX_CONCURRENCY:
        return await _forward(target, path, body, stream)

    # Queue on the routed NODE's own queue — other nodes' traffic is unaffected.
    log.info("Ollama proxy: queuing for %s (in_use=%d, node queue=%d)",
             target, in_use, _proxy_queue_depth(target))
    loop   = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    try:
        _proxy_queue(target).put_nowait((path, body, stream, future))
    except asyncio.QueueFull:
        return JSONResponse({"error": f"Proxy queue full for node {target}"},
                            status_code=429)

    try:
        return await asyncio.wait_for(future, timeout=PROXY_QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "Proxy queue timeout"}, status_code=504)


async def _proxy_dispatch(target: str, path: str, body: dict, stream: bool,
                          future: "asyncio.Future"):
    """Forward one queued request and resolve its waiter."""
    try:
        result = await _forward(target, path, body, stream)
        if not future.done():
            future.set_result(result)
    except Exception as e:
        if not future.done():
            future.set_exception(e)


async def _proxy_queue_worker(target: str):
    """Drain ONE node's proxy queue. Each node has its own worker, so a slow
    node only ever delays its own backlog. Requests are dispatched as
    concurrent tasks up to PROXY_MAX_CONCURRENCY per node (the node's in_use
    is raised synchronously inside _forward, so the capacity check below sees
    each dispatch immediately) — the cap is genuinely per-node concurrency,
    not a limit on how fast this loop can shuttle jobs."""
    q = PROXY_QUEUES[target]
    while True:
        try:
            path, body, stream, future = await asyncio.wait_for(q.get(), timeout=5)
        except asyncio.TimeoutError:
            continue
        except Exception:
            await asyncio.sleep(1)
            continue

        try:
            # Wait for a free slot on THIS node (bounded by the queue timeout —
            # the waiter's future times out at the same bound anyway).
            waited = 0.0
            while (OLLAMA_INSTANCES.get(target, {}).get("in_use", 0)
                   >= PROXY_MAX_CONCURRENCY):
                await asyncio.sleep(0.25)
                waited += 0.25
                if waited >= PROXY_QUEUE_TIMEOUT or future.done():
                    break
            if future.done():   # waiter already timed out — don't forward
                continue
            asyncio.create_task(_proxy_dispatch(target, path, body, stream, future))
            # Let the dispatch task reach _forward's synchronous in_use
            # increment before we examine capacity for the next item.
            await asyncio.sleep(0)
        finally:
            q.task_done()


# The mimic is a first-class caller in the Model Routing table: this declared
# baseline makes 'mimic.proxy' show up there, and a USER rule on the same
# pattern (pin / allow / deny / model / prefer_gpu) overrides it.
try:
    _orch.register_cap_routing("mimic.proxy", label="Ollama mimic proxy",
                               declared_by="mimic", job_type="proxy")
except Exception as _e:
    log.debug("register mimic routing: %s", _e)

# Mount the Ollama mimic proxy. Scoped to /ollama/api/* and /ollama/v1/* (the only
# paths real Ollama / OpenAI-compatible clients use) so it does NOT shadow Vera's
# own /ollama/* control endpoints (routing, request_log, ping, pull, …). Mounted
# unconditionally and routes each request through the cluster (pick_instance,
# GPU-preferred), so external clients (e.g. Continue) get cluster routing without
# needing LOCAL_OLLAMA_INSTANCE pinned.
log.info("Mounting Ollama mimic proxy /ollama/{api,v1}/* (routing=%s, local=%s)",
         "gpu-preferred" if _PROXY_PREFER_GPU else "load-aware",
         LOCAL_OLLAMA_ID or "none")


async def _proxy_get(full_path: str):
    """GET passthrough (e.g. /api/tags, /api/version, /api/ps) to a routed node."""
    if _proxy_paused:
        return JSONResponse({"error": "Ollama mimic proxy is paused"}, 503)
    target_id = _pick_proxy_target({})
    if not target_id or target_id not in OLLAMA_INSTANCES:
        return JSONResponse({"error": "No online Ollama node to route to"}, 503)
    url = f"{OLLAMA_INSTANCES[target_id]['url']}/{full_path}"
    try:
        async with httpx.AsyncClient(timeout=10, verify=_SSL_CTX or True) as c:
            r = await c.get(url)
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@APP.post("/ollama/api/{path:path}")
async def _ollama_proxy_post_api(path: str, req: Request):
    return await _proxy_handler("api/" + path, req)


@APP.get("/ollama/api/{path:path}")
async def _ollama_proxy_get_api(path: str, req: Request):
    return await _proxy_get("api/" + path)


@APP.post("/ollama/v1/{path:path}")
async def _ollama_proxy_post_v1(path: str, req: Request):
    return await _proxy_handler("v1/" + path, req)


@APP.get("/ollama/v1/{path:path}")
async def _ollama_proxy_get_v1(path: str, req: Request):
    return await _proxy_get("v1/" + path)


# ─────────────────────────────────────────────────────────────────────────────
# OLLAMA MIMIC PROXY — monitor + control
# ─────────────────────────────────────────────────────────────────────────────
# The mimic exposes an Ollama-compatible API at /ollama/* (this is what lets Vera
# "mimic Ollama"); every request is logged to the request log + emitted as an
# ollama.request event so prompts/jobs submitted this way are observable. These
# capabilities back the "Mimic" page in the Workers & Ollama panel.
#
# NOTE on paths: the proxy itself is scoped to /ollama/api/* and /ollama/v1/* so
# it doesn't shadow Vera's own /ollama/* control routes. The monitor/control
# endpoints below live under /cluster/mimic/* (cap names mirror the paths so the
# auto-POST fallback doesn't mount a duplicate).

@capability(
    "cluster.mimic.status", memory="off", silent=True,
    http_method="GET", http_path="/cluster/mimic/status", http_tags=["cluster"],
    description="Live status of the Ollama mimic proxy: routing mode, the node a "
                "request would route to right now, online nodes, paused state, "
                "queue depth, active in-flight requests and the concurrency cap. "
                "Output: {mounted, paused, prefer_gpu, routing, current_target, "
                "online_nodes, local_instance, active, queue_depth, queue_max, "
                "max_concurrency, queue_timeout}.")
async def cap_mimic_status(trace_id=None) -> Dict:
    online = _online_nodes()
    return {
        "mounted":         True,   # always mounted now (cluster-routed)
        "paused":          _proxy_paused,
        "prefer_gpu":      _PROXY_PREFER_GPU,
        "routing":         "gpu-preferred" if _PROXY_PREFER_GPU else "load-aware",
        "current_target":  _pick_proxy_target({}) or "",
        "online_nodes":    online,
        "local_instance":  LOCAL_OLLAMA_ID,
        "active":          _proxy_active,
        "queue_depth":     _proxy_qsize_total(),
        "queue_per_node":  {iid: q.qsize() for iid, q in PROXY_QUEUES.items()},
        "queue_max":       PROXY_QUEUE_MAX,
        "max_concurrency": PROXY_MAX_CONCURRENCY,
        "queue_timeout":   PROXY_QUEUE_TIMEOUT,
    }


@capability(
    "cluster.mimic.config", memory="off",
    http_method="POST", http_path="/cluster/mimic/config", http_tags=["cluster"],
    description="Control the Ollama mimic proxy at runtime. Inputs: paused (bool "
                "— stop/resume forwarding without restart), max_concurrency (int "
                ">=1, simultaneous forwards before queuing), prefer_gpu (bool — "
                "True pins routing to GPU nodes, False = load-aware across all "
                "nodes). Omitted fields are left unchanged. Output: same shape as "
                "cluster.mimic.status.")
async def cap_mimic_config(paused: Optional[bool] = None,
                           max_concurrency: Optional[int] = None,
                           prefer_gpu: Optional[bool] = None,
                           trace_id=None) -> Dict:
    global _proxy_paused, PROXY_MAX_CONCURRENCY, _PROXY_PREFER_GPU
    if paused is not None:
        _proxy_paused = bool(paused)
    if prefer_gpu is not None:
        _PROXY_PREFER_GPU = bool(prefer_gpu)
    if max_concurrency is not None:
        try:
            PROXY_MAX_CONCURRENCY = max(1, int(max_concurrency))
        except (TypeError, ValueError):
            return {"error": "max_concurrency must be an integer >= 1"}
    await emit_event({"type": "ollama.proxy.config",
                      "paused": _proxy_paused,
                      "prefer_gpu": _PROXY_PREFER_GPU,
                      "max_concurrency": PROXY_MAX_CONCURRENCY})
    return await cap_mimic_status()


_PROXY_LOG_STREAM = "vera:ollama_proxy_log"


@capability(
    "cluster.mimic.requests", memory="off", silent=True,
    http_method="GET", http_path="/cluster/mimic/requests", http_tags=["cluster"],
    description="Requests that actually came through the Ollama mimic proxy "
                "(the /ollama/* endpoint), newest first, from the dedicated "
                "vera:ollama_proxy_log stream — NOT Vera's own internal "
                "generate/embed traffic. Query: limit (int, default 100). "
                "Output: {entries:[{path,model,prompt,instance,ts}], total}.")
async def cap_mimic_requests(limit: int = 100, trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"entries": [], "total": 0}
    entries: List[dict] = []
    try:
        rows = await r.xrevrange(_PROXY_LOG_STREAM, count=max(1, limit))  # newest first
        for _id, fields in rows:
            raw = fields.get(b"data") or fields.get("data")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            if not raw:
                continue
            try:
                entries.append(json.loads(raw))
            except Exception:
                continue
        total = await r.xlen(_PROXY_LOG_STREAM)
    except Exception as e:
        return {"entries": [], "total": 0, "error": str(e)}
    return {"entries": entries, "total": total}


@capability(
    "cluster.mimic.requests.clear", memory="off",
    http_method="POST", http_path="/cluster/mimic/requests/clear", http_tags=["cluster"],
    description="Clear the mimic proxy request stream (vera:ollama_proxy_log). "
                "Output: {ok, cleared}.")
async def cap_mimic_requests_clear(trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"error": "redis unavailable"}
    try:
        n = await r.xlen(_PROXY_LOG_STREAM)
        await r.delete(_PROXY_LOG_STREAM)
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
# ONNX RUNTIME ADVERTISE  (§4 of ONNX_TODO.md)
# ─────────────────────────────────────────────────────────────────────────────

def _onnx_models_dir() -> str:
    """Shared ONNX artifact dir (prefer ml_onnx's resolved path)."""
    import sys
    mo = sys.modules.get("ml_onnx")
    d = getattr(mo, "ONNX_DIR", None)
    if d:
        return d
    _repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.getenv("ML_ONNX_DIR", os.path.join(_repo, "edge", "models"))


def _scan_onnx_artifacts() -> list:
    out = []
    d = _onnx_models_dir()
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                m = json.load(f)
            out.append({"slug": m.get("slug"), "module_id": m.get("module_id"),
                        "dtype": m.get("dtype"), "cap_name": m.get("cap_name")})
        except Exception:
            pass
    return out


async def _gather_onnx_runtimes() -> dict:
    """Advertise ONNX Runtime availability across the cluster:
      • shared artifacts in the (network-shared) models dir — any edge node can serve them
      • per-node edge ORT servers in ONNX_RUNTIME_URLS (GET /health)
    Best-effort and additive; never raises into the cluster view."""
    artifacts = _scan_onnx_artifacts()
    info = {"shared_dir": _onnx_models_dir(),
            "shared_artifacts": artifacts, "count": len(artifacts),
            "runtimes": []}
    urls = [u.strip() for u in os.getenv("ONNX_RUNTIME_URLS", "").split(",") if u.strip()]
    if urls:
        import httpx as _httpx

        async def _ping(u):
            try:
                async with _httpx.AsyncClient(timeout=3.0, verify=_SSL_CTX or True) as c:
                    r = await c.get(u.rstrip("/") + "/health")
                if r.status_code == 200:
                    h = r.json()
                    return {"url": u, "status": "online",
                            "selected": h.get("selected"),
                            "providers": h.get("providers", []),
                            "models": h.get("models", 0)}
                return {"url": u, "status": f"http_{r.status_code}"}
            except Exception as e:
                return {"url": u, "status": "offline", "error": str(e)[:80]}

        info["runtimes"] = list(await asyncio.gather(*[_ping(u) for u in urls]))
    return info


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
            rlen = await r.xlen(_orch.RESULT_STREAM)
            # Backlog = not-yet-delivered (lag) + delivered-but-unacked (pending).
            # XLEN counts the whole stream including acked history, so it is
            # NOT the backlog — see monitor_capabilities._queue_len.
            pending = 0
            backlog = None
            for g in await r.xinfo_groups(_orch.TASK_STREAM):
                gname = g.get("name")
                if isinstance(gname, bytes):
                    gname = gname.decode()
                if gname == _orch.GROUP_WORKERS:
                    pending = int(g.get("pending") or 0)
                    backlog = int(g.get("lag") or 0) + pending
                    break
            if backlog is None:   # no group yet → nothing consumes the stream
                backlog = await r.xlen(_orch.TASK_STREAM)
            queue_info = {
                "task_queue_len":   backlog,
                "result_queue_len": rlen,
                "pending_tasks":    pending,
            }
        except Exception as e:
            log.debug("queue_info: %s", e)

    # ── Proxy stats ───────────────────────────────────────────────────────────
    proxy_info = {
        "active":         _proxy_active,
        "local_instance": LOCAL_OLLAMA_ID,
        "queue_depth":    _proxy_qsize_total(),
        "queue_per_node": {iid: q.qsize() for iid, q in PROXY_QUEUES.items()},
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
        "onnx":     await _gather_onnx_runtimes(),
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
    # Per-node proxy queue workers start lazily on each node's first queued
    # request (see _proxy_queue) — no global worker to start here.
    log.info("vera_cluster ready — local_ollama=%s  mimic=mounted (routing=%s)",
             LOCAL_OLLAMA_ID or "none",
             "gpu-preferred" if _PROXY_PREFER_GPU else "load-aware")


schedule(_startup, interval=999999, name="cluster_startup")
try:
    _loop = asyncio.get_event_loop()
    if _loop.is_running():
        _loop.create_task(_startup())
except Exception:
    pass