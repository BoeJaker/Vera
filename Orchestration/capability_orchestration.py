"""
vera_orchestrator.py  –  v3
============================
Single-decorator architecture: @capability is the ONLY registration primitive.
Every @capability can optionally declare an HTTP route — which replaces the
need for @APP.get / @APP.post entirely.  All routes are automatically:

  • Callable via MCP  (/mcp/tools  /mcp/call  /ws/mcp)
  • Callable via REST  (auto-mounted at the declared path)
  • Observable via Redis event stream
  • Distributed via Redis Streams when mode="distributed"
  • Retried on failure
  • Schema-reflected for the harness UI

Capabilities that declare http_method + http_path are additionally mounted
as standard REST endpoints with full OpenAPI docs.

Endpoints:
  Ollama  : http://192.168.0.250:11435  (GPU)
             http://192.168.0.246:11435  (CPU A)
             http://192.168.0.247:11435  (CPU B)
  Redis   : redis://llm.int:6379
  Postgres: postgresql://postgres:password@llm.int:5432/llm
"""

import asyncio, functools, inspect, json, logging, os, sys, time, uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Union

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from pathlib import Path 
_HERE = Path(__file__).parent

# ── Optional backends ─────────────────────────────────────────────────────────
try:
    import redis.asyncio as aioredis; HAS_REDIS = True
except ImportError:
    aioredis = None; HAS_REDIS = False

try:
    import asyncpg; HAS_PG = True
except ImportError:
    asyncpg = None; HAS_PG = False

try:
    import chromadb; HAS_CHROMA = True
except ImportError:
    chromadb = None; HAS_CHROMA = False

try:
    from neo4j import AsyncGraphDatabase; HAS_NEO = True
except ImportError:
    AsyncGraphDatabase = None; HAS_NEO = False

logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("vera.orch")

# ── Config (from central cfg — single source of truth)
from Vera.Orchestration.config import cfg

# ── Config ────────────────────────────────────────────────────────────────────
REDIS_URL    = cfg.REDIS_URL
POSTGRES_URL = cfg.POSTGRES_URL
OLLAMA_MODEL       = cfg.OLLAMA_MODEL
OLLAMA_EMBED_URL   = cfg.OLLAMA_EMBED_URL
OLLAMA_EMBED_MODEL = cfg.OLLAMA_EMBED_MODEL

TASK_STREAM   = "vera:tasks"
RESULT_STREAM = "vera:results"
EVENT_STREAM  = "vera:events"
GROUP_WORKERS = "workers"
GROUP_RESULTS = "orchestrator"

# ── Runtime state ─────────────────────────────────────────────────────────────
CAPABILITY_REGISTRY: Dict[str, dict]           = {}
WORKER_REGISTRY:     Dict[str, dict]           = {}
STREAM_SUBS:         Dict[str, List[Callable]] = {}
WS_CONNECTIONS:      List[tuple]               = []
SCHEDULED_TASKS:     List[dict]                = []
PENDING_RESULTS:     Dict[str, asyncio.Future] = {}
MCP_SERVERS:         Dict[str, str]            = {}
LOADED_MODULES:      List[dict]                = []   # [{name, path, caps, status}]

# UI panel registry — modules call register_ui() to inject harness panels.
# Lives here (not in vera_capabilities) so /ui/panels always exists.
UI_PANELS: Dict[str, dict] = {}

def register_ui(panel_id: str, label: str, icon: str, html: str, js: str = "",
                ui_caps: List[str] = None,
                mode: str = "inject",
                tab_order: int = 100):
    """Register a built-in UI panel.

    mode:
      "inject"  — panel HTML is injected into the Media sub-switcher (default)
      "tab"     — panel gets its own top-level tab in the harness, auto-created on load
      "mount"   — panel is injected into a pre-declared mount point (skills, ontologies, etc.)

    tab_order: integer sort key for auto-tabs (lower = further left); default 100.
    ui_caps: list of capability names this panel uses.
    """
    UI_PANELS[panel_id] = {
        "id":        panel_id,
        "label":     label,
        "icon":      icon,
        "html":      html,
        "js":        js,
        "ui_caps":   ui_caps or [],
        "mode":      mode,
        "tab_order": tab_order,
    }

REDIS = PG_POOL = CHROMA = NEO = None

# ─────────────────────────────────────────────────────────────────────────────
# OLLAMA CLUSTER
# ─────────────────────────────────────────────────────────────────────────────
OLLAMA_INSTANCES: Dict[str, dict] = {
    "gpu-250": {"url":"http://192.168.0.250:11435","label":"GPU Node","has_gpu":True,
                "priority":0,"status":"unknown","latency_ms":None,"models":[],"in_use":0,"last_check":None,"errors":0},
    "cpu-246": {"url":"http://192.168.0.246:11435","label":"CPU Node A","has_gpu":False,
                "priority":1,"status":"unknown","latency_ms":None,"models":[],"in_use":0,"last_check":None,"errors":0},
    "cpu-247": {"url":"http://192.168.0.247:11435","label":"CPU Node B","has_gpu":False,
                "priority":2,"status":"unknown","latency_ms":None,"models":[],"in_use":0,"last_check":None,"errors":0},
}

# Per-instance concurrency semaphores for Ollama — limits simultaneous
# in-flight requests per node to 1 (Ollama queues internally but multiple
# concurrent httpx connections cause request pile-ups and timeouts).
# Callers that want parallelism across *different* nodes are unaffected.
# Use acquire/release via `async with _ollama_sem(iid):` pattern.
_OLLAMA_SEMAPHORES: Dict[str, asyncio.Semaphore] = {}
_OLLAMA_SEM_LIMIT = int(os.environ.get("OLLAMA_CONCURRENCY", "1"))

def _ollama_sem(iid: str) -> asyncio.Semaphore:
    """Return (creating if needed) the per-instance Semaphore."""
    if iid not in _OLLAMA_SEMAPHORES:
        _OLLAMA_SEMAPHORES[iid] = asyncio.Semaphore(_OLLAMA_SEM_LIMIT)
    return _OLLAMA_SEMAPHORES[iid]


def add_ollama_instance(iid: str, url: str, has_gpu: bool = False, label: str = ""):
    OLLAMA_INSTANCES[iid] = {"url":url,"label":label or iid,"has_gpu":has_gpu,
                              "priority":len(OLLAMA_INSTANCES),"status":"unknown",
                              "latency_ms":None,"models":[],"in_use":0,"last_check":None,"errors":0}

async def _ping_instance(iid: str, inst: dict):
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{inst['url']}/api/tags"); r.raise_for_status()
            ms     = round((time.monotonic()-t0)*1000)
            models = [m["name"] for m in r.json().get("models",[])]
            inst.update(status="online",latency_ms=ms,models=models,last_check=now_iso(),errors=0)
    except Exception as e:
        inst.update(status="offline",latency_ms=None,last_check=now_iso(),errors=inst["errors"]+1)
        log.debug("Ping [%s] failed: %s", iid, e)

async def instance_health_loop(interval: float = 20.0):
    while True:
        await asyncio.gather(*[_ping_instance(iid,inst) for iid,inst in OLLAMA_INSTANCES.items()],return_exceptions=True)
        await emit_event({"type":"ollama.health","instances":{iid:{"status":i["status"],"latency_ms":i["latency_ms"]} for iid,i in OLLAMA_INSTANCES.items()}})
        await asyncio.sleep(interval)

def pick_instance(prefer_gpu: bool = False, instance_id: Optional[str] = None, model: Optional[str] = None) -> Optional[str]:
    online = {iid:i for iid,i in OLLAMA_INSTANCES.items() if i["status"]=="online"}
    if not online: return None
    if instance_id and instance_id in online: return instance_id

    def _has_model(inst, mdl):
        """Check if an instance has a model — flexible name matching."""
        if not mdl: return True
        models = inst.get("models", [])
        mdl_base = mdl.split(":")[0]
        for m in models:
            if m == mdl or m.startswith(mdl + ":") or m.split(":")[0] == mdl_base:
                return True
        return False

    def _pick_best(candidates):
        return min(candidates, key=lambda k: (candidates[k]["in_use"], candidates[k]["priority"]))

    # Build model-aware candidate sets
    has_model = {iid:i for iid,i in online.items() if _has_model(i, model)} if model else online

    if prefer_gpu:
        # Best: GPU node that has the model
        gpu_with_model = {iid:i for iid,i in has_model.items() if i["has_gpu"]}
        if gpu_with_model: return _pick_best(gpu_with_model)
        # Next: any node that has the model
        if has_model: return _pick_best(has_model)
        # Last resort: any GPU node (will likely 404 but may auto-pull)
        gpu_any = {iid:i for iid,i in online.items() if i["has_gpu"]}
        if gpu_any: return _pick_best(gpu_any)

    # Non-GPU preference: prefer nodes with the model
    if has_model: return _pick_best(has_model)

    # No node has the model — pick least busy, log a warning
    if model:
        log.warning("pick_instance: model '%s' not found on any online node — routing to least busy", model)
    return _pick_best(online)

def _ollama_caller_info(depth: int = 3) -> dict:
    """Walk the call stack to identify who triggered this Ollama request.
    Returns {caller_file, caller_func, caller_module, cap_name} for logging."""
    import traceback as _tb
    info = {"caller_file": "", "caller_func": "", "caller_module": "", "cap_name": ""}
    try:
        stack = _tb.extract_stack(limit=depth + 5)
        # Walk backwards skipping frames inside this file
        this_file = str(Path(__file__).name)
        for frame in reversed(stack[:-1]):  # skip the _ollama_caller_info frame
            fname = os.path.basename(frame.filename)
            if fname != this_file and not fname.startswith("<"):
                info["caller_file"] = fname
                info["caller_func"] = frame.name
                info["caller_module"] = fname.replace(".py", "")
                break
        # Try to extract capability name from further up the stack
        for frame in reversed(stack):
            if frame.name.startswith("cap_") or "capability" in frame.name.lower():
                info["cap_name"] = frame.name
                break
    except Exception:
        pass
    return info


# ── Ollama request log (in-process ring buffer + structured event emission) ──
_OLLAMA_REQUEST_LOG: List[dict] = []      # ring buffer, max 500
_OLLAMA_REQUEST_LOG_MAX = 500


async def ollama_generate(prompt: str, system: str = "", json_mode: bool = False,
                           model: Optional[str] = None, instance_id: Optional[str] = None,
                           prefer_gpu: bool = False, stream_cb: Optional[Callable] = None,
                           caller_override: Optional[dict] = None) -> str:
    chosen = pick_instance(prefer_gpu=prefer_gpu,instance_id=instance_id,model=model) or "cpu-246"
    inst   = OLLAMA_INSTANCES[chosen]
    mdl    = model or OLLAMA_MODEL
    body   = {"model":mdl,"prompt":prompt,"stream":stream_cb is not None}
    if system:    body["system"]  = system
    if json_mode: body["format"]  = "json"

    # ── Identify caller and log the request ──────────────────────────────────
    # caller_override lets an intermediary cap (e.g. llm.generate) pass
    # through the true upstream caller rather than appearing as the caller.
    caller   = caller_override if caller_override else _ollama_caller_info()
    req_id   = str(uuid.uuid4())[:12]
    t_start  = time.time()
    prompt_preview = (prompt or "")[:120].replace("\n", " ")

    log.info(
        "ollama_req [%s] model=%s inst=%s caller=%s:%s prompt=%s",
        req_id, mdl, chosen,
        caller["caller_file"], caller["caller_func"],
        prompt_preview,
    )

    # Emit structured event so syslog + Jobs panel can track it
    try:
        await emit_event({
            "type":        "ollama.request",
            "req_id":      req_id,
            "model":       mdl,
            "instance_id": chosen,
            "instance_url": inst.get("url", ""),
            "caller_file": caller["caller_file"],
            "caller_func": caller["caller_func"],
            "caller_module": caller["caller_module"],
            "cap_name":    caller["cap_name"],
            "prompt_preview": prompt_preview,
            "json_mode":   json_mode,
            "prefer_gpu":  prefer_gpu,
            "streaming":   stream_cb is not None,
        })
    except Exception:
        pass  # never let logging break generation

    req_entry = {
        "req_id": req_id, "model": mdl, "instance": chosen,
        "caller_file": caller["caller_file"], "caller_func": caller["caller_func"],
        "prompt_preview": prompt_preview, "ts": now_iso(),
        "status": "running",
    }

    inst["in_use"] += 1
    # Acquire per-instance semaphore — prevents concurrent request pile-ups on
    # the same Ollama node. OLLAMA_CONCURRENCY env var sets the limit (default 1).
    try:
        async with _ollama_sem(chosen):
            async with httpx.AsyncClient(timeout=120) as c:
                if stream_cb:
                    async with c.stream("POST",f"{inst['url']}/api/generate",json=body) as resp:
                        if resp.status_code != 200:
                            err_body = ""
                            async for chunk in resp.aiter_bytes():
                                err_body += chunk.decode("utf-8", errors="replace")
                            raise Exception(f"ollama returned {resp.status_code}: {err_body[:500]}")
                        buf=[]
                        async for line in resp.aiter_lines():
                            if not line: continue
                            try:
                                tok=json.loads(line).get("response","")
                                if tok: buf.append(tok); await stream_cb(tok)
                            except: pass
                        result = "".join(buf)
                        elapsed = round(time.time() - t_start, 2)
                        log.info("ollama_done [%s] %.2fs tokens=%d caller=%s:%s",
                                 req_id, elapsed, len(buf),
                                 caller["caller_file"], caller["caller_func"])
                        req_entry.update({"status": "done", "elapsed_s": elapsed,
                                          "tokens": len(buf)})
                        _ollama_log_append(req_entry)
                        try:
                            await emit_event({
                                "type": "ollama.request_done", "req_id": req_id,
                                "model": mdl, "instance_id": chosen,
                                "caller_file": caller["caller_file"],
                                "caller_func": caller["caller_func"],
                                "elapsed_s": elapsed, "token_count": len(buf),
                            })
                        except Exception:
                            pass
                        return result
                else:
                    r=await c.post(f"{inst['url']}/api/generate",json=body)
                    if r.status_code != 200:
                        err_detail = r.text[:500] if r.text else f"HTTP {r.status_code}"
                        log.error("ollama_generate [%s] HTTP %d from %s: %s",
                                  req_id, r.status_code, chosen, err_detail)
                        raise Exception(f"ollama {r.status_code} on {chosen}: {err_detail}")
                    d = r.json()
                    result = d.get("response","")
                    elapsed = round(time.time() - t_start, 2)
                    eval_count = d.get("eval_count", 0)
                    log.info("ollama_done [%s] %.2fs eval_count=%s caller=%s:%s",
                             req_id, elapsed, eval_count,
                             caller["caller_file"], caller["caller_func"])
                    req_entry.update({"status": "done", "elapsed_s": elapsed,
                                      "eval_count": eval_count})
                    _ollama_log_append(req_entry)
                    try:
                        await emit_event({
                            "type": "ollama.request_done", "req_id": req_id,
                            "model": mdl, "instance_id": chosen,
                            "caller_file": caller["caller_file"],
                            "caller_func": caller["caller_func"],
                            "elapsed_s": elapsed, "eval_count": eval_count,
                        })
                    except Exception:
                        pass
                    return result
    except Exception as e:
        elapsed = round(time.time() - t_start, 2)
        log.error("ollama_generate [%s] FAILED after %.2fs inst=%s caller=%s:%s err=%s",
                  req_id, elapsed, chosen,
                  caller["caller_file"], caller["caller_func"], e)
        inst["errors"]+=1; inst["status"]="offline"
        req_entry.update({"status": "error", "elapsed_s": elapsed,
                          "error": str(e)[:200]})
        _ollama_log_append(req_entry)
        try:
            await emit_event({
                "type": "ollama.request_error", "req_id": req_id,
                "model": mdl, "instance_id": chosen,
                "caller_file": caller["caller_file"],
                "caller_func": caller["caller_func"],
                "elapsed_s": elapsed, "error": str(e)[:200],
            })
        except Exception:
            pass
        for fb_id,fb_inst in OLLAMA_INSTANCES.items():
            if fb_id==chosen or fb_inst["status"]!="online": continue
            # Skip nodes that don't have the model
            fb_models = fb_inst.get("models", [])
            mdl_base = mdl.split(":")[0]
            if fb_models and not any(m == mdl or m.startswith(mdl+":") or m.split(":")[0] == mdl_base for m in fb_models):
                log.debug("ollama_fallback [%s] skipping %s — model '%s' not available", req_id, fb_id, mdl)
                continue
            try:
                log.info("ollama_fallback [%s] trying %s", req_id, fb_id)
                async with httpx.AsyncClient(timeout=120) as c:
                    r=await c.post(f"{fb_inst['url']}/api/generate",json={**body,"stream":False})
                    if r.status_code != 200:
                        err_detail = r.text[:300] if r.text else f"HTTP {r.status_code}"
                        log.warning("ollama_fallback [%s] %s returned %d: %s", req_id, fb_id, r.status_code, err_detail)
                        continue
                    fb_elapsed = round(time.time() - t_start, 2)
                    log.info("ollama_fallback [%s] OK on %s after %.2fs",
                             req_id, fb_id, fb_elapsed)
                    req_entry.update({"status": "done_fallback",
                                      "fallback_instance": fb_id,
                                      "elapsed_s": fb_elapsed})
                    return r.json().get("response","")
            except: pass
        return ""
    finally:
        inst["in_use"]=max(0,inst["in_use"]-1)


def _ollama_log_append(entry: dict):
    """Append to the in-process ring buffer."""
    _OLLAMA_REQUEST_LOG.append(entry)
    if len(_OLLAMA_REQUEST_LOG) > _OLLAMA_REQUEST_LOG_MAX:
        del _OLLAMA_REQUEST_LOG[:-_OLLAMA_REQUEST_LOG_MAX]


async def ollama_embed(text: str, model: Optional[str] = None,
                       instance_id: Optional[str] = None,
                       prefer_gpu: bool = False,
                       timeout: float = 30.0,
                       normalize: bool = False) -> Optional[List[float]]:
    """Generate a text embedding via Ollama, with full job logging.

    Tries /api/embed (Ollama ≥0.4) first, then /api/embeddings (older).
    Emits ollama.request / ollama.request_done / ollama.request_error events
    so every embed call appears in the Workers panel Jobs tab.

    Parameters
    ----------
    text         : str   — text to embed (truncated to 4096 chars)
    model        : str   — embedding model (default: OLLAMA_EMBED_MODEL)
    instance_id  : str   — pin to a specific Ollama instance
    prefer_gpu   : bool  — prefer GPU instances for routing
    timeout      : float — HTTP timeout in seconds (default 30)
    normalize    : bool  — L2-normalise the returned vector

    Returns
    -------
    List[float] or None on failure.
    """
    if not text or not text.strip():
        return None

    mdl = model or OLLAMA_EMBED_MODEL
    # Apply runtime embed config: prefer_gpu / pinned_instance from UI settings
    eff_prefer_gpu = prefer_gpu or _EMBED_PREFER_GPU
    eff_instance   = instance_id or _EMBED_INSTANCE_ID
    chosen = pick_instance(prefer_gpu=eff_prefer_gpu, instance_id=eff_instance,
                           model=mdl) or "cpu-246"
    inst = OLLAMA_INSTANCES.get(chosen)
    if not inst:
        return None
    url = inst.get("url", "")

    caller = _ollama_caller_info()
    req_id = str(uuid.uuid4())[:12]
    t_start = time.time()
    text_preview = (text or "")[:120].replace("\n", " ")

    log.info(
        "ollama_embed [%s] model=%s inst=%s caller=%s:%s text=%s",
        req_id, mdl, chosen,
        caller["caller_file"], caller["caller_func"],
        text_preview,
    )

    # Emit start event for Jobs panel
    try:
        await emit_event({
            "type":         "ollama.request",
            "req_id":       req_id,
            "model":        mdl,
            "instance_id":  chosen,
            "instance_url": url,
            "caller_file":  caller["caller_file"],
            "caller_func":  caller["caller_func"],
            "caller_module": caller["caller_module"],
            "cap_name":     caller["cap_name"] or "ollama.embed",
            "prompt_preview": f"[embed] {text_preview}",
            "json_mode":    False,
            "prefer_gpu":   prefer_gpu,
            "streaming":    False,
        })
    except Exception:
        pass

    req_entry = {
        "req_id": req_id, "model": mdl, "instance": chosen,
        "caller_file": caller["caller_file"], "caller_func": caller["caller_func"],
        "prompt_preview": f"[embed] {text_preview}", "ts": now_iso(),
        "status": "running",
    }

    inst["in_use"] = inst.get("in_use", 0) + 1
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            # Try new endpoint first (Ollama ≥0.4)
            r = await c.post(f"{url}/api/embed",
                             json={"model": mdl, "input": text[:4096]})
            if r.status_code != 200:
                # Fall back to legacy endpoint
                r = await c.post(f"{url}/api/embeddings",
                                 json={"model": mdl, "prompt": text[:4096]})
            if r.status_code != 200:
                elapsed = round(time.time() - t_start, 2)
                log.warning("ollama_embed [%s] FAILED status=%d inst=%s",
                            req_id, r.status_code, chosen)
                req_entry.update({"status": "error", "elapsed_s": elapsed,
                                  "error": f"http_{r.status_code}"})
                _ollama_log_append(req_entry)
                try:
                    await emit_event({
                        "type": "ollama.request_error", "req_id": req_id,
                        "model": mdl, "instance_id": chosen,
                        "caller_file": caller["caller_file"],
                        "caller_func": caller["caller_func"],
                        "elapsed_s": elapsed,
                        "error": f"http_{r.status_code}",
                    })
                except Exception:
                    pass
                return None

            data = r.json()
            emb = data.get("embeddings")
            if emb and isinstance(emb, list):
                vec = emb[0] if isinstance(emb[0], list) else emb
            else:
                vec = data.get("embedding")

            if not vec:
                elapsed = round(time.time() - t_start, 2)
                req_entry.update({"status": "error", "elapsed_s": elapsed,
                                  "error": "no_vector_in_response"})
                _ollama_log_append(req_entry)
                try:
                    await emit_event({
                        "type": "ollama.request_error", "req_id": req_id,
                        "model": mdl, "instance_id": chosen,
                        "caller_file": caller["caller_file"],
                        "caller_func": caller["caller_func"],
                        "elapsed_s": elapsed, "error": "no_vector_in_response",
                    })
                except Exception:
                    pass
                return None

            # Optionally L2-normalise
            if normalize:
                try:
                    import numpy as np
                    arr = np.array(vec, dtype="float32")
                    norm = np.linalg.norm(arr)
                    if norm > 0:
                        vec = (arr / norm).tolist()
                except ImportError:
                    pass  # skip normalisation if numpy unavailable

            elapsed = round(time.time() - t_start, 2)
            log.info("ollama_embed_done [%s] %.2fs dim=%d caller=%s:%s",
                     req_id, elapsed, len(vec),
                     caller["caller_file"], caller["caller_func"])
            req_entry.update({"status": "done", "elapsed_s": elapsed,
                              "dimensions": len(vec)})
            _ollama_log_append(req_entry)
            try:
                await emit_event({
                    "type": "ollama.request_done", "req_id": req_id,
                    "model": mdl, "instance_id": chosen,
                    "caller_file": caller["caller_file"],
                    "caller_func": caller["caller_func"],
                    "elapsed_s": elapsed, "dimensions": len(vec),
                })
            except Exception:
                pass
            return vec

    except Exception as e:
        elapsed = round(time.time() - t_start, 2)
        log.error("ollama_embed [%s] FAILED after %.2fs inst=%s err=%s",
                  req_id, elapsed, chosen, e)
        inst["errors"] = inst.get("errors", 0) + 1
        req_entry.update({"status": "error", "elapsed_s": elapsed,
                          "error": str(e)[:200]})
        _ollama_log_append(req_entry)
        try:
            await emit_event({
                "type": "ollama.request_error", "req_id": req_id,
                "model": mdl, "instance_id": chosen,
                "caller_file": caller["caller_file"],
                "caller_func": caller["caller_func"],
                "elapsed_s": elapsed, "error": str(e)[:200],
            })
        except Exception:
            pass

        # Fallback: try other online instances
        for fb_id, fb_inst in OLLAMA_INSTANCES.items():
            if fb_id == chosen or fb_inst.get("status") != "online":
                continue
            try:
                log.info("ollama_embed_fallback [%s] trying %s", req_id, fb_id)
                async with httpx.AsyncClient(timeout=timeout) as c:
                    r = await c.post(f"{fb_inst['url']}/api/embed",
                                     json={"model": mdl, "input": text[:4096]})
                    if r.status_code != 200:
                        r = await c.post(f"{fb_inst['url']}/api/embeddings",
                                         json={"model": mdl, "prompt": text[:4096]})
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    emb = data.get("embeddings")
                    if emb and isinstance(emb, list):
                        vec = emb[0] if isinstance(emb[0], list) else emb
                    else:
                        vec = data.get("embedding")
                    if vec:
                        fb_elapsed = round(time.time() - t_start, 2)
                        log.info("ollama_embed_fallback [%s] OK on %s dim=%d %.2fs",
                                 req_id, fb_id, len(vec), fb_elapsed)
                        req_entry.update({"status": "done_fallback",
                                          "fallback_instance": fb_id,
                                          "elapsed_s": fb_elapsed,
                                          "dimensions": len(vec)})
                        _ollama_log_append(req_entry)
                        return vec
            except Exception:
                pass
        return None
    finally:
        inst["in_use"] = max(0, inst.get("in_use", 1) - 1)

# ─────────────────────────────────────────────────────────────────────────────
# UTILS
# ─────────────────────────────────────────────────────────────────────────────
def new_id()  -> str: return str(uuid.uuid4())
def now_iso() -> str: return datetime.utcnow().isoformat()+"Z"

# ── Type annotation → JSON-Schema helpers ─────────────────────────────────────
import typing as _typing

_SCHEMA_TMAP: Dict[Any, str] = {
    int: "integer", float: "number", bool: "boolean",
    str: "string",  list: "array",   dict: "object",
    bytes: "string",
}

# Mapping of common type-name strings → JSON-schema types. Used when
# `from __future__ import annotations` is active (PEP 563), in which case
# annotations are stored as strings rather than resolved class objects.
_STRING_ANN_MAP: Dict[str, dict] = {
    "int":     {"type": "integer"},
    "float":   {"type": "number"},
    "bool":    {"type": "boolean"},
    "str":     {"type": "string"},
    "bytes":   {"type": "string"},
    "list":    {"type": "array"},
    "List":    {"type": "array"},
    "dict":    {"type": "object"},
    "Dict":    {"type": "object"},
    "Any":     {},
    "any":     {},
}


def _resolve_string_annotation(s: str) -> Optional[dict]:
    """Resolve a string annotation (from PEP 563) to a JSON-schema fragment.

    Handles:
      - Bare names: "int", "float", "bool", "str", "list", "dict"
      - Optional[T] / Optional["T"] → unwrap
      - List[T] / Dict[K,V] / Tuple[...] → array / object / array
      - Union[A, B, ...] → anyOf
      - "Optional[Dict[str,Any]]" → object (unwraps Optional)
      - typing-style "List[int]" with item types
    """
    s = s.strip()
    if not s:
        return None

    # Drop quotes for forward-references like "int"
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        s = s[1:-1].strip()

    # Bare type-name lookup
    if s in _STRING_ANN_MAP:
        return dict(_STRING_ANN_MAP[s])

    # Generic forms: Optional[X], List[X], Dict[K,V], Union[A,B], Tuple[...]
    import re as _re
    m = _re.match(r"^([A-Za-z_][\w.]*)\[(.+)\]$", s)
    if m:
        gname = m.group(1).split(".")[-1]   # strip module prefix like typing.
        inner = m.group(2).strip()
        if gname in ("Optional",):
            return _resolve_string_annotation(inner) or {"type": "string"}
        if gname in ("List", "list", "Sequence", "Iterable", "Tuple", "tuple", "Set", "set", "FrozenSet"):
            schema: dict = {"type": "array"}
            inner_schema = _resolve_string_annotation(inner)
            if inner_schema and "type" in inner_schema:
                schema["items"] = inner_schema
            return schema
        if gname in ("Dict", "dict", "Mapping"):
            return {"type": "object"}
        if gname in ("Union",):
            # Split by commas at top level
            parts = _split_top_level(inner)
            non_none = [p.strip() for p in parts if p.strip() not in ("None", "type(None)")]
            if len(non_none) == 1:
                return _resolve_string_annotation(non_none[0])
            return {"anyOf": [_resolve_string_annotation(p) or {"type": "string"} for p in non_none]}

    # Otherwise treat as opaque object (forward reference to a custom class)
    return None


def _split_top_level(s: str, sep: str = ",") -> List[str]:
    """Split string by separator at top level only (respect bracket nesting)."""
    parts: List[str] = []
    depth = 0
    cur = []
    for ch in s:
        if ch in "[(":
            depth += 1
            cur.append(ch)
        elif ch in "])":
            depth -= 1
            cur.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _resolve_annotation(ann) -> Optional[dict]:
    """
    Recursively convert a Python type annotation to a JSON-Schema fragment.

    Handles:
      - Plain types: int, float, bool, str, list, dict
      - Optional[T]  → unwrap, mark not-required at call site
      - List[T]      → {"type":"array","items":<T>}
      - Dict[K,V]    → {"type":"object"}
      - Union[A,B]   → {"anyOf":[<A>,<B>]}  (non-Optional multi-type)
      - typing.Any   → {}  (no constraint)
      - Unannotated  → None  (caller falls back to default-value inference)
      - PEP 563 string annotations (from __future__ import annotations):
        "int", "Optional[float]", "List[str]", etc. resolved via name lookup.
    """
    if ann is inspect.Parameter.empty:
        return None

    # PEP 563: when `from __future__ import annotations` is active, annotations
    # are stored as STRINGS rather than resolved class objects. Handle that
    # FIRST so we don't fall through to the string fallback at the bottom.
    if isinstance(ann, str):
        return _resolve_string_annotation(ann)

    # Plain type
    if ann in _SCHEMA_TMAP:
        return {"type": _SCHEMA_TMAP[ann]}

    # typing.Any — no constraint
    if ann is _typing.Any:
        return {}

    origin = getattr(ann, "__origin__", None)
    args   = getattr(ann, "__args__", ()) or ()

    # Union / Optional
    if origin is Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            # Optional[X] — resolve inner, caller handles required flag
            return _resolve_annotation(non_none[0])
        # Multi-type Union → anyOf
        return {"anyOf": [_resolve_annotation(a) or {"type": "string"} for a in non_none]}

    # List[T] / list
    if origin in (list,) or origin is getattr(_typing, "List", None):
        schema: dict = {"type": "array"}
        if args:
            inner = _resolve_annotation(args[0])
            if inner is not None:
                schema["items"] = inner
        return schema

    # Dict[K,V] / dict
    if origin in (dict,) or origin is getattr(_typing, "Dict", None):
        return {"type": "object"}

    # Tuple → array
    if origin is tuple or origin is getattr(_typing, "Tuple", None):
        return {"type": "array"}

    # Bare subclass (catches subclasses of int, str, etc.)
    if isinstance(ann, type):
        if issubclass(ann, bool):  return {"type": "boolean"}
        if issubclass(ann, int):   return {"type": "integer"}
        if issubclass(ann, float): return {"type": "number"}
        if issubclass(ann, str):   return {"type": "string"}
        if issubclass(ann, list):  return {"type": "array"}
        if issubclass(ann, dict):  return {"type": "object"}

    return {"type": "string"}   # safe fallback


def _infer_from_default(default) -> Optional[dict]:
    """Infer a JSON-Schema type from a default value's Python type."""
    if default is inspect.Parameter.empty or default is None:
        return None
    t = type(default)
    if t is bool:   return {"type": "boolean"}   # before int — bool is int subclass
    if t is int:    return {"type": "integer"}
    if t is float:  return {"type": "number"}
    if t is str:    return {"type": "string"}
    if t is list:   return {"type": "array"}
    if t is dict:   return {"type": "object"}
    return None


def _is_optional_annotation(ann) -> bool:
    """Return True if the annotation is Optional[X] i.e. Union[X, None].

    Also handles PEP 563 string annotations like "Optional[int]" which appear
    when modules use `from __future__ import annotations`.
    """
    if ann is inspect.Parameter.empty:
        return False
    # PEP 563: string-form annotations
    if isinstance(ann, str):
        s = ann.strip()
        # Drop quotes from forward-references
        if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
            s = s[1:-1].strip()
        if s.startswith("Optional[") and s.endswith("]"):
            return True
        if s.startswith("Union[") and s.endswith("]"):
            inner = s[len("Union["):-1]
            parts = _split_top_level(inner)
            for p in parts:
                if p.strip() in ("None", "type(None)", "NoneType"):
                    return True
        return False
    origin = getattr(ann, "__origin__", None)
    args   = getattr(ann, "__args__", ()) or ()
    return origin is Union and type(None) in args


def generate_schema(func: Callable) -> dict:
    """
    Derive a JSON-Schema object from a function's type annotations.

    Resolution order per parameter:
      1. Annotation via _resolve_annotation — handles Optional, List, Dict,
         Union, nested generics, and plain types correctly.
      2. Default-value inference — catches unannotated params that have typed
         defaults (e.g. ``limit=10`` with no annotation → integer).
      3. Falls back to {"type": "string"} — always produces valid schema.

    Required: a param is required only when it has NO default value AND its
    annotation is not Optional[...] / Union[..., None].
    """
    sig   = inspect.signature(func)
    props: dict = {}
    req:   list = []
    _SKIP = {"trace_id", "self", "request", "kwargs"}

    for k, v in sig.parameters.items():
        if k in _SKIP:
            continue

        ann     = v.annotation
        default = v.default

        # 1. Try annotation
        prop = _resolve_annotation(ann)

        # 2. Fall back to default-value type inference
        if prop is None:
            prop = _infer_from_default(default) or {"type": "string"}

        prop = dict(prop)   # ensure mutable copy

        # Add default when it's a concrete value
        if default is not inspect.Parameter.empty and default is not None:
            prop["default"] = default

        props[k] = prop

        # Required: no default AND not Optional
        if default is inspect.Parameter.empty and not _is_optional_annotation(ann):
            req.append(k)

    return {"type": "object", "properties": props, "required": req}


def _merge_schema(auto: dict, override: dict) -> dict:
    """
    Deep-merge an explicit schema override onto the auto-generated schema.

    Strategy:
      - `required` comes from the real function signature (auto) — the source
        of truth for what Python actually needs. The override may extend it.
      - `properties` are merged per-property: auto supplies `type` (and
        `items`, `anyOf`, etc.) derived from the annotation; the override
        enriches with `description`, `enum`, `format`, constraints, etc.
        Override keys win over auto keys on a per-property basis.
      - Override-only properties (e.g. a dict param described in detail) are
        accepted as-is.
      - Top-level keys beyond `properties`/`required` come from the override.
    """
    merged_props: dict = {}

    for pname, pauto in auto.get("properties", {}).items():
        merged_props[pname] = dict(pauto)

    for pname, pover in override.get("properties", {}).items():
        if pname in merged_props:
            merged_props[pname] = {**merged_props[pname], **pover}
        else:
            merged_props[pname] = dict(pover)

    merged_req = sorted(set(auto.get("required", [])) | set(override.get("required", [])))

    return {**override, "type": "object",
            "properties": merged_props, "required": merged_req}

def _remove_ws(ws,stream):
    try: WS_CONNECTIONS.remove((ws,stream))
    except ValueError: pass

# ─────────────────────────────────────────────────────────────────────────────
# EVENTS  (before capability decorator — it references emit_event)
# ─────────────────────────────────────────────────────────────────────────────
async def emit_event(event: dict):
    event.setdefault("ts", now_iso())
    ev_json = json.dumps(event)
    if REDIS:
        try:
            # Stream — persistent, replayable history
            await REDIS.xadd(EVENT_STREAM, {"data": ev_json}, maxlen=5000)
            # Pub/sub — zero-latency fan-out for any live subscribers
            await REDIS.publish("vera:events:live", ev_json)
        except Exception as _re:
            if "MISCONF" not in str(_re):
                log.debug("emit_event Redis: %s", _re)
    for ws, sub in list(WS_CONNECTIONS):
        if sub == "__events__":
            try: await ws.send_json({"type": "event", "data": event})
            except: _remove_ws(ws, sub)

async def emit_stream(name: str, trace_id: str, payload: Any, capability: str):
    msg={"stream":name,"trace_id":trace_id,"capability":capability,"payload":payload,"ts":now_iso()}
    if REDIS:
        try:
            await asyncio.gather(
                REDIS.publish(f"stream:{name}",json.dumps(msg)),
                REDIS.xadd(f"vera:stream:{name}",{"data":json.dumps(msg)},maxlen=500),
                return_exceptions=True)
        except: pass
    # ── Cross-publish token streams to vera:events:live ──────────────────
    # The agent loop SSE bridge (workshop_agent_loop_stream) subscribes ONLY
    # to vera:events:live and looks for events with type "stream.token".
    # Without this bridge, emit_stream("tokens",...) from llm.generate /
    # ollama never reaches the agent loop output UI.
    if name == "tokens" and REDIS:
        try:
            ev = {"type": "stream.token",
                  "token": (payload.get("token","") if isinstance(payload,dict) else str(payload)),
                  "trace_id": trace_id, "capability": capability,
                  "source": "llm", "ts": now_iso()}
            await REDIS.publish("vera:events:live", json.dumps(ev))
        except: pass
    for ws,sub in list(WS_CONNECTIONS):
        if sub==name:
            try: await ws.send_json({"type":"stream","data":msg})
            except: _remove_ws(ws,sub)
    for cb in STREAM_SUBS.get(name,[]):
        asyncio.create_task(cb(msg))

def subscribe_stream(name: str, cb: Callable):
    STREAM_SUBS.setdefault(name,[]).append(cb)

# ─────────────────────────────────────────────────────────────────────────────
# REDIS DISPATCH
# ─────────────────────────────────────────────────────────────────────────────
async def dispatch_task(cap_name: str, payload: dict, trace_id: str) -> str:
    task_id=new_id()
    rec={"id":task_id,"capability":cap_name,"payload":json.dumps(payload),"trace_id":trace_id,"ts":now_iso()}
    if REDIS: await REDIS.xadd(TASK_STREAM,rec)
    else:
        cap=CAPABILITY_REGISTRY.get(cap_name)
        if cap: asyncio.create_task(_run_local(cap,task_id,payload,trace_id))
    return task_id

async def _run_local(cap,task_id,payload,trace_id):
    try: result=await cap["raw"](**payload,trace_id=trace_id)
    except Exception as e: result={"error":str(e)}
    fut=PENDING_RESULTS.get(task_id)
    if fut and not fut.done(): fut.set_result(result)

# ── Per-cap timeout heuristics ──────────────────────────────────────────
# The previous 30s default was the root cause of nearly every "Task timed
# out" the user saw inside a DAG. LLM generation alone routinely takes
# 60-180s for non-trivial output; research and deep-research need minutes.
# We pick a budget based on the cap name prefix; only short, deterministic
# caps (echo, math, json) keep the snappy default.
_CAP_TIMEOUT_OVERRIDES = {
    # Long-running LLM work
    "llm.generate":     300.0,
    "llm.summarize":    240.0,
    "llm.analyze":      240.0,
    "llm.code_review":  240.0,
    "llm.translate":    180.0,
    "llm.classify":     180.0,
    "llm.chat":         300.0,
    # Research is async (returns a job_id quickly) but the cap call itself
    # may need to do auth/setup before returning. 60s is generous.
    "research.run":      60.0,
    "research.report":   60.0,
    "research.parallel": 60.0,
    "research.deep":     60.0,
    # DAG composer caps run a whole sub-DAG inside a single task — give
    # them headroom proportional to typical DAG depth.
    "dag.run":           600.0,
    "dag.plan_and_run":  600.0,
    "dag.from_goal":     180.0,
    # Browser caps (playwright launch + page load + interaction)
    "browser.screenshot": 90.0,
    "browser.content":    90.0,
    "browser.click":     120.0,
    "browser.form":      120.0,
    # Memory operations are usually fast but bulk ones can take a while
    "memory.search":      30.0,
    "memory.bulk_store": 120.0,
    
}

import inspect as _inspect

def _filter_kwargs_for_func(func: Callable, kw: dict) -> dict:
    """Return a copy of *kw* containing only keys that *func* actually accepts.

    If *func* has a **kwargs catch-all, all keys are passed through unchanged.
    Otherwise, any keys not in the function's signature are silently dropped
    (and logged at DEBUG level) so the caller never hits a TypeError from an
    unexpected keyword argument — a common issue when the dream/DAG system
    passes config dicts that have more keys than the target cap expects.
    """
    try:
        sig = _inspect.signature(func)
    except (ValueError, TypeError):
        return kw  # can't introspect — pass everything

    # If function has **kwargs, anything goes
    if any(p.kind == _inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return kw

    accepted = set(sig.parameters.keys())
    filtered = {}
    dropped  = []
    for k, v in kw.items():
        if k in accepted:
            filtered[k] = v
        else:
            dropped.append(k)
    if dropped:
        log.debug("_filter_kwargs: dropped %s for %s", dropped, getattr(func, "__name__", "?"))
    return filtered


def _cap_timeout(name: str, default: float = 60.0) -> float:
    """Pick a sensible timeout for a cap by name. Specific override wins;
    otherwise the prefix gives a hint (llm.* → long, exec.* → medium).
    The caller can pass an explicit timeout in the payload to override."""
    if name in _CAP_TIMEOUT_OVERRIDES:
        return _CAP_TIMEOUT_OVERRIDES[name]
    if name.startswith("llm."):     return 240.0
    if name.startswith("research."):return 60.0
    if name.startswith("exec."):    return 300.0
    if name.startswith("ml."):      return 600.0
    if name.startswith("ide."):     return 120.0
    if name.startswith("browser."):return 90.0
    return default


async def wait_for_result(task_id: str, timeout: float = 60.0) -> Any:
    fut=asyncio.get_event_loop().create_future()
    PENDING_RESULTS[task_id]=fut
    try: return await asyncio.wait_for(asyncio.shield(fut),timeout)
    except asyncio.TimeoutError:
        PENDING_RESULTS.pop(task_id,None)
        return {"error":"timeout","task_id":task_id,"timeout_s":timeout}

async def worker_loop(worker_id: str):
    """
    Worker loop with Redis retry.  If Redis is unavailable at startup the loop
    waits and retries every 5 s rather than exiting — so workers on Host B will
    pick up tasks as soon as the Redis connection recovers.
    """
    global REDIS

    # ── Wait for Redis (retry indefinitely) ──────────────────────────────────
    while not REDIS:
        log.warning("Worker %s: Redis not connected — retrying in 5s "
                    "(check REDIS_URL=%s, Redis bind-address, and requirepass)", worker_id, REDIS_URL)
        await asyncio.sleep(5)
        if not REDIS and HAS_REDIS:
            try:
                candidate = aioredis.from_url(REDIS_URL, decode_responses=False,
                                              socket_connect_timeout=4,
                                              socket_timeout=4)
                await candidate.ping()
                REDIS = candidate
                log.info("Worker %s: Redis reconnected ✓", worker_id)
            except Exception as e:
                log.warning("Worker %s: Redis reconnect failed: %s", worker_id, e)

    # ── Register in shared Redis hash (visible to ALL hosts) ─────────────────
    reg = {
        "id":           worker_id,
        "status":       "starting",
        "capabilities": json.dumps(list(CAPABILITY_REGISTRY.keys())),
        "cap_count":    len(CAPABILITY_REGISTRY),
        "tasks_done":   0,
        "tasks_failed": 0,
        "started":      now_iso(),
        "host":         os.uname().nodename,
        "pid":          os.getpid(),
        "current_task": "",
        "task_started": "",
        "ollama_instance": "",
    }
    WORKER_REGISTRY[worker_id] = dict(reg)
    try:
        # Explicitly JSON-encode list/dict fields — str() produces invalid JSON
        redis_reg = {k: (json.dumps(v) if isinstance(v, (list, dict)) else str(v))
                     for k, v in reg.items()}
        await REDIS.hset(f"vera:workers:{worker_id}", mapping=redis_reg)
        await REDIS.expire(f"vera:workers:{worker_id}", 120)
    except Exception as e:
        log.warning("Worker registry push failed: %s", e)

    try:
        await REDIS.xgroup_create(TASK_STREAM, GROUP_WORKERS, id="$", mkstream=True)
    except Exception:
        pass   # group already exists

    WORKER_REGISTRY[worker_id]["status"] = "idle"
    log.info("Worker %s ready (%d caps)", worker_id, len(CAPABILITY_REGISTRY))

    while True:
        # Refresh TTL and write all live fields — not just status
        try:
            w = WORKER_REGISTRY[worker_id]
            await REDIS.hset(f"vera:workers:{worker_id}", mapping={
                "status":       str(w.get("status", "idle")),
                "tasks_done":   str(w.get("tasks_done", 0)),
                "tasks_failed": str(w.get("tasks_failed", 0)),
                "current_task": str(w.get("current_task", "")),
                "task_started": str(w.get("task_started", "")),
                "ollama_instance": str(w.get("ollama_instance", "")),
            })
            await REDIS.expire(f"vera:workers:{worker_id}", 120)
        except Exception:
            pass

        try:
            resp = await REDIS.xreadgroup(
                GROUP_WORKERS, worker_id, {TASK_STREAM: ">"}, count=1, block=5000
            )
        except Exception as e:
            err_str = str(e)
            if "MISCONF" in err_str or "unable to persist" in err_str:
                # Redis can't write to disk — back off 30s and warn once per minute
                log.warning("worker: Redis persistence error (MISCONF) — "
                            "check disk space or set 'save \"\"' in redis.conf. "
                            "Backing off 30s.")
                await asyncio.sleep(30)
            elif "timeout" in err_str.lower() or "timed out" in err_str.lower():
                pass  # normal block-read keepalive, not an error
            else:
                log.error("xreadgroup: %s", e)
                await asyncio.sleep(2)
            continue

        if not resp:
            continue

        for _, messages in resp:
            for msg_id, data in messages:
                task_id  = data[b"id"].decode()
                cap_name = data[b"capability"].decode()
                payload  = json.loads(data[b"payload"])
                trace_id = data[b"trace_id"].decode()
                cap      = CAPABILITY_REGISTRY.get(cap_name)

                WORKER_REGISTRY[worker_id]["status"] = f"running:{cap_name}"
                WORKER_REGISTRY[worker_id]["current_task"] = cap_name
                WORKER_REGISTRY[worker_id]["task_started"] = now_iso()
                await emit_event({
                    "type": "worker.start", "worker": worker_id,
                    "capability": cap_name, "task": task_id,
                })

                if not cap:
                    # Check Redis for workers on OTHER hosts that have this cap
                    other_has_cap = False
                    try:
                        rkeys = await REDIS.keys("vera:workers:*")
                        for rk in rkeys:
                            raw_w = await REDIS.hgetall(rk)
                            if not raw_w: continue
                            wid_b = raw_w.get(b"id", b"")
                            other_wid = wid_b.decode() if isinstance(wid_b, bytes) else str(wid_b)
                            if other_wid == worker_id: continue
                            caps_b = raw_w.get(b"capabilities", b"[]")
                            caps_str = caps_b.decode() if isinstance(caps_b, bytes) else str(caps_b)
                            try:
                                other_caps = json.loads(caps_str)
                            except Exception:
                                try:
                                    import ast as _ast
                                    other_caps = _ast.literal_eval(caps_str)
                                except Exception:
                                    other_caps = []
                            if cap_name in other_caps:
                                other_has_cap = True
                                break
                    except Exception as e:
                        log.debug("other_has_cap Redis check: %s", e)
                    if other_has_cap:
                        log.debug("Worker %s: skipping %s — another worker has it", worker_id, cap_name)
                        await asyncio.sleep(0.1)
                        await REDIS.xack(TASK_STREAM, GROUP_WORKERS, msg_id)
                        # Re-add so another consumer picks it up
                        await REDIS.xadd(TASK_STREAM, {
                            "id": task_id, "capability": cap_name,
                            "payload": json.dumps(payload), "trace_id": trace_id, "ts": now_iso(),
                        })
                    else:
                        log.warning("Worker %s: no handler for %s on any worker", worker_id, cap_name)
                        await REDIS.xadd(RESULT_STREAM, {
                            "id": task_id, "error": f"no_worker_for:{cap_name}", "trace_id": trace_id,
                        })
                        await REDIS.xack(TASK_STREAM, GROUP_WORKERS, msg_id)
                else:
                    try:
                        result = await cap["raw"](**payload, trace_id=trace_id)
                        await REDIS.xadd(RESULT_STREAM, {
                            "id": task_id, "result": json.dumps(result), "trace_id": trace_id,
                        }, maxlen=5000)
                        WORKER_REGISTRY[worker_id]["tasks_done"] += 1
                        await emit_event({"type": "worker.done", "worker": worker_id, "task": task_id})
                    except Exception as e:
                        await REDIS.xadd(RESULT_STREAM, {
                            "id": task_id, "error": str(e), "trace_id": trace_id,
                        })
                        WORKER_REGISTRY[worker_id]["tasks_failed"] += 1
                        await emit_event({
                            "type": "worker.error", "worker": worker_id,
                            "task": task_id, "error": str(e),
                        })
                    finally:
                        await REDIS.xack(TASK_STREAM, GROUP_WORKERS, msg_id)

                WORKER_REGISTRY[worker_id]["status"] = "idle"
                WORKER_REGISTRY[worker_id]["current_task"] = ""
                WORKER_REGISTRY[worker_id]["task_started"] = ""

async def result_listener():
    """
    Listen for task results on the shared result stream.

    Each host uses its own hostname as the consumer name within the shared
    GROUP_RESULTS consumer group.  This means every result is delivered to
    exactly one host — the one whose PENDING_RESULTS dict holds the future.

    Cross-host delivery: if Host A dispatched the task, the future lives in
    Host A's PENDING_RESULTS. Host B executes the task and writes the result
    to RESULT_STREAM. All hosts read the stream; only Host A finds the future
    and resolves it. The others ACK without doing anything (no future found).
    This is correct — ACKing without a matching future is a no-op.
    """
    if not REDIS: return
    # Per-host consumer name prevents two hosts sharing one consumer slot
    consumer_name = f"host-{os.uname().nodename}"
    try:
        await REDIS.xgroup_create(RESULT_STREAM, GROUP_RESULTS, id="$", mkstream=True)
    except Exception:
        pass   # group already exists
    log.info("Result listener started (consumer=%s)", consumer_name)
    while True:
        try:
            resp = await REDIS.xreadgroup(
                GROUP_RESULTS, consumer_name, {RESULT_STREAM: ">"}, count=10, block=5000
            )
        except Exception as e:
            log.error("result_listener: %s", e)
            await asyncio.sleep(2)
            continue
        if not resp:
            continue
        for _, messages in resp:
            for msg_id, data in messages:
                task_id = data[b"id"].decode()
                fut     = PENDING_RESULTS.get(task_id)
                if fut and not fut.done():
                    if b"result" in data:
                        fut.set_result(json.loads(data[b"result"]))
                    else:
                        fut.set_result({"error": data.get(b"error", b"unknown").decode()})
                    PENDING_RESULTS.pop(task_id, None)
                # Always ACK — even if this host didn't own the future
                # (another host may have already resolved it from its own listener)
                await REDIS.xack(RESULT_STREAM, GROUP_RESULTS, msg_id)

async def _pg_archive(task_id,result_json):
    try:
        async with PG_POOL.acquire() as conn:
            await conn.execute("INSERT INTO vera_task_results(task_id,result,ts) VALUES($1,$2::jsonb,NOW()) ON CONFLICT DO NOTHING",task_id,result_json)
    except Exception as e: log.warning("PG archive: %s",e)

# ─────────────────────────────────────────────────────────────────────────────
#  ██  CAPABILITY DECORATOR  — the single registration primitive
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# FRAMEWORK-WIDE ACTIVITY RECORDING
# Every non-silent, non-infrastructure capability call with a session_id is
# recorded to the memory graph (FOLLOWS_ACTIVITY chain) and data fabric.
#
# Architecture: fire-and-forget queue drained by a background worker.
# The capability wrapper enqueues a lightweight dict (no awaiting, no blocking).
# The _activity_worker() coroutine drains the queue every 2s, batching writes.
# This means zero overhead on the hot path and no risk of breaking cap calls.
# ─────────────────────────────────────────────────────────────────────────────

import asyncio as _act_asyncio
import queue as _queue_mod

# In-process state
_ACT_QUEUE: "asyncio.Queue" = None           # created lazily in first enqueue
_ACT_SESSION_CURSOR: dict   = {}             # session_id -> last node_id
_ACT_SESSION_ROOT:   dict   = {}             # session_id -> root node_id (cached)
_ACT_FABRIC_DEDUP:   set    = set()          # trace_id dedup
_CURRENT_SESSION:    str    = ""             # last known session_id (fallback for caps without explicit session)

# Capability groups that are too noisy / infrastructure — skip recording
_ACT_SKIP_GROUPS = frozenset({
    # Infrastructure / polling
    "obs", "health", "ollama", "ui", "mcp", "memory",
    "syslog", "cluster", "db", "stream", "caps", "session",
    # Fabric would create infinite recording loop
    "fabric",
    # Agent infrastructure — high frequency, handled separately
    "agent",
})

# NOTE: there used to be a separate _ACT_RICH_GROUPS set for ide/research/nlp
# that suppressed activity recording for those groups on the assumption they
# had their own richer per-module recording. That's no longer true — there
# is one unified recording path now (_act_enqueue → _activity_worker) and
# every tracked group writes the same call+output linked structure.


# Activity-recording size limits. Tuned so a single queue item stays under
# ~16 KB even for chatty caps. The underlying activity_worker truncates again
# when writing to the graph so individual graph nodes stay reasonable.
_ACT_PARAMS_MAX_BYTES   = 4096    # serialised params dict cap
_ACT_RESULT_MAX_BYTES   = 8192    # serialised result cap
_ACT_PREVIEW_MAX_CHARS  = 400     # human-readable preview line


def _act_safe_params(kw: dict) -> dict:
    """
    Return a sanitised copy of cap params suitable for storage.

    Drops anything that's clearly too large or sensitive-shaped (binary blobs,
    secrets-looking keys). Truncates long string values. Keeps the dict
    structure so the recorded params are still queryable.
    """
    SECRETY = ("password", "secret", "token", "api_key", "apikey",
               "auth", "credential", "ssh_key", "private_key")
    out: dict = {}
    for k, v in kw.items():
        if k == "trace_id":
            continue
        kl = k.lower()
        if any(s in kl for s in SECRETY):
            out[k] = "[redacted]"
            continue
        if isinstance(v, str):
            out[k] = v[:1000]            # trim very long strings
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [str(x)[:200] for x in v[:25]]
        elif isinstance(v, dict):
            out[k] = {kk: (str(vv)[:200] if not isinstance(vv, (int, float, bool, type(None))) else vv)
                      for kk, vv in list(v.items())[:25]}
        else:
            out[k] = str(v)[:300]
    return out


def _act_extract_text(result):
    """Pick the most informative text out of a cap result for the preview line."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("text", "response", "summary", "content", "answer",
                    "output", "result", "preview", "status", "error",
                    "path", "name", "job_id"):
            v = result.get(key)
            if v and isinstance(v, str):
                return v
        # Fall back to JSON repr (truncated)
        try:
            return json.dumps(result)[:_ACT_PREVIEW_MAX_CHARS]
        except Exception:
            return str(result)[:_ACT_PREVIEW_MAX_CHARS]
    return str(result)[:_ACT_PREVIEW_MAX_CHARS]


def _act_enqueue(cap_name: str, group: str, session_id: str,
                 trace_id: str, kw: dict, result: object,
                 elapsed_ms: int,
                 trigger_id: str = "", trigger_cap: str = ""):
    """
    Non-blocking enqueue of a capability call for background recording.

    Called from inside the capability wrapper AND from streaming endpoints
    that don't go through the wrapper. Must never await or raise.

    The recorded payload contains:
      - cap_name, group, session_id, trace_id, trigger_id, trigger_cap
      - safe_params : sanitised copy of input kwargs (size-bounded)
      - result_full : serialised cap result (size-bounded)
      - preview     : short human-readable preview line for syslog/UI
      - elapsed_ms, ts

    The activity_worker drains this queue and writes ONE rich graph node per
    cap call carrying both input and output, linked via FOLLOWS_ACTIVITY to
    the previous chain step.
    """
    global _ACT_QUEUE
    # Fall back to the orchestrator's current session id when the caller
    # didn't supply one. This matters for raw streaming endpoints (bash,
    # ssh, ide) where the panel may not include session_id in the request
    # body but the syslog trigger context does have it.
    if not session_id:
        try:
            _vera_syslog = sys.modules.get("syslog")
            if _vera_syslog:
                session_id = (_vera_syslog.get_trigger_chain() or {}).get(
                    "session_id", "")
        except Exception:
            pass
    if not session_id:
        session_id = _CURRENT_SESSION
    if not session_id:
        return  # genuinely orphaned call — drop
    if group in _ACT_SKIP_GROUPS:
        return
    if not sys.modules.get("data_fabric"):
        return  # backends not loaded yet
    try:
        if _ACT_QUEUE is None:
            try:
                _act_asyncio.get_running_loop()
                _ACT_QUEUE = _act_asyncio.Queue(maxsize=2000)
            except RuntimeError:
                return  # no running loop yet — skip

        safe_params = _act_safe_params(kw or {})
        # Truncate the JSON repr to keep the queue item compact even for
        # chatty caps. We store both a human preview and the full structured
        # result so downstream queries can drill in.
        try:
            params_json = json.dumps(safe_params, default=str)[:_ACT_PARAMS_MAX_BYTES]
        except Exception:
            params_json = str(safe_params)[:_ACT_PARAMS_MAX_BYTES]
        try:
            if isinstance(result, (dict, list)):
                result_json = json.dumps(result, default=str)[:_ACT_RESULT_MAX_BYTES]
            else:
                result_json = str(result)[:_ACT_RESULT_MAX_BYTES]
        except Exception:
            result_json = str(result)[:_ACT_RESULT_MAX_BYTES]

        preview_text = _act_extract_text(result)[:_ACT_PREVIEW_MAX_CHARS]

        _ACT_QUEUE.put_nowait({
            "cap_name":    cap_name,
            "group":       group,
            "session_id":  session_id,
            "trace_id":    trace_id,
            "trigger_id":  trigger_id,
            "trigger_cap": trigger_cap,
            "safe_params": safe_params,        # for graph metadata
            "params_json": params_json,        # for full-text storage
            "result_json": result_json,        # for full-text storage
            "preview":     preview_text,
            "elapsed_ms":  elapsed_ms,
            "ts":          now_iso(),
        })
    except Exception:
        pass  # full queue or other error — always silent


async def begin_stream_activity(
    cap_name:    str,
    session_id:  str,
    *,
    trace_id:    str = "",
    group:       str = "",
    silent:      bool = False,
):
    """
    Emit a cap.call event for a streaming endpoint at the START of the stream.

    Returns a dict carrying state needed by the matching end_stream_activity
    call (trace_id, group, trigger_chain, t0). Pass it into end_stream_activity
    when the stream completes.

    Returns None if no session_id can be resolved (recording is dropped).
    Falls back to the syslog trigger chain and then `_CURRENT_SESSION` so
    streaming endpoints that don't take session_id in their body still record.
    """
    if not session_id:
        try:
            _vera_syslog = sys.modules.get("syslog")
            if _vera_syslog:
                session_id = (_vera_syslog.get_trigger_chain() or {}).get(
                    "session_id", "")
        except Exception:
            pass
    if not session_id:
        session_id = _CURRENT_SESSION
    if not session_id:
        return None
    tid   = trace_id or new_id()
    grp   = group or cap_name.split(".", 1)[0]
    chain = {}
    try:
        _vera_syslog = sys.modules.get("syslog")
        if _vera_syslog:
            chain = _vera_syslog.get_trigger_chain() or {}
    except Exception:
        pass
    if not silent:
        try:
            await emit_event({
                "type":        "cap.call",
                "name":        cap_name,
                "trace_id":    tid,
                "session_id":  session_id,
                "trigger_id":  chain.get("trigger_id", ""),
                "trigger_cap": chain.get("trigger_cap", ""),
                "group":       grp,
            })
        except Exception:
            pass
    return {
        "cap_name":   cap_name,
        "session_id": session_id,
        "trace_id":   tid,
        "group":      grp,
        "chain":      chain,
        "t0":         time.monotonic(),
        "silent":     silent,
    }


async def end_stream_activity(
    handle: dict,
    params: dict,
    result: object,
    elapsed_ms: int = 0,
):
    """
    Companion to begin_stream_activity. Emits cap.ok and enqueues the rich
    activity item for the activity_worker to write a graph node-pair.

    `handle` is the dict returned by begin_stream_activity. If it's None
    (the begin call was dropped because session_id was empty), this is a no-op.
    """
    if not handle:
        return
    cap_name   = handle["cap_name"]
    session_id = handle["session_id"]
    tid        = handle["trace_id"]
    grp        = handle["group"]
    chain      = handle.get("chain", {})
    silent     = handle.get("silent", False)
    if elapsed_ms <= 0:
        elapsed_ms = round((time.monotonic() - handle["t0"]) * 1000)
    if not silent:
        _preview = _act_extract_text(result)[:200]
        try:
            await emit_event({
                "type":        "cap.ok",
                "name":        cap_name,
                "trace_id":    tid,
                "session_id":  session_id,
                "group":       grp,
                "elapsed_ms":  elapsed_ms,
                "preview":     _preview,
            })
        except Exception:
            pass
    _act_enqueue(
        cap_name=cap_name, group=grp, session_id=session_id,
        trace_id=tid, kw=params, result=result,
        elapsed_ms=elapsed_ms,
        trigger_id=chain.get("trigger_id", ""),
        trigger_cap=chain.get("trigger_cap", ""),
    )


async def record_stream_activity(
    cap_name:    str,
    session_id:  str,
    params:      dict,
    result:      object,
    elapsed_ms:  int,
    *,
    trace_id:    str = "",
    group:       str = "",
    silent:      bool = False,
):
    """
    Convenience wrapper: emit cap.call, cap.ok, and enqueue activity in one
    shot. Use when you don't need to interleave the cap.call event with
    other output (i.e. the recording happens in a `finally:` block at the
    very end of a stream).

    For richer stream visualisation in the Observe panel — where you want
    the cap.call to appear at the START of the stream, not at the end —
    use begin_stream_activity / end_stream_activity instead.
    """
    handle = await begin_stream_activity(
        cap_name, session_id, trace_id=trace_id, group=group, silent=silent,
    )
    if handle is None:
        return
    await end_stream_activity(handle, params, result, elapsed_ms)


async def _activity_worker():
    """
    Background coroutine. Drains _ACT_QUEUE every 2s, writes ONE rich
    graph node per cap call, plus a fabric entry. Runs for the lifetime
    of the server.

    Graph structure produced per cap call (single node, not a pair):

                    [previous chain step]
                            │
                            │  FOLLOWS_ACTIVITY
                            ▼
                    [cap_call_node : event/tool]
                       (carries both input and output)

        ─ The cap node carries:
            • `text`       — short [cap_name] hint with first few params
            • `full_text`  — Cap / Trace / Group / Trigger / Params / Result
            • `metadata`   — full structured params + preview + elapsed_ms

        ─ Edges:
            • Neo4j backend auto-creates `(:Session)-[:CONTAINS]->(:Memory)`
              for every Memory node with a session_id; we don't add an
              extra SESSION_CONTENT edge. The Neo4j auto-edge is the single
              authoritative session→cap relationship.
            • FOLLOWS_ACTIVITY links the previous chain step → this node.
            • TRIGGERED_BY is intentionally NOT emitted — the trigger_id
              we receive from syslog is a trace_id (call-correlation key),
              not a graph node id, so wiring an edge to it would create
              the "unresolved" placeholder nodes the user sees in the
              memory graph panel. Trigger context is preserved inside the
              cap node's `metadata` for queries.
    """
    global _ACT_QUEUE
    if _ACT_QUEUE is None:
        _ACT_QUEUE = _act_asyncio.Queue(maxsize=2000)

    log.info("activity_worker: started (single-node mode)")
    while True:
        try:
            await _act_asyncio.sleep(2.0)
            # Only process if memory system is loaded (avoids hammering during startup)
            if not sys.modules.get("data_fabric"):
                continue
            batch = []
            while not _ACT_QUEUE.empty() and len(batch) < 50:
                try:
                    batch.append(_ACT_QUEUE.get_nowait())
                except Exception:
                    break
            if not batch:
                continue

            mem_mod = sys.modules.get("memory")
            hooks   = sys.modules.get("memory_hooks")
            fabric  = sys.modules.get("data_fabric")

            for item in batch:
                sid          = item["session_id"]
                cap_name     = item["cap_name"]
                group        = item["group"]
                trace_id     = item["trace_id"]
                trigger_id   = item.get("trigger_id", "")
                trigger_cap  = item.get("trigger_cap", "")
                safe_params  = item.get("safe_params", {})
                params_json  = item.get("params_json", "")
                result_json  = item.get("result_json", "")
                preview      = item.get("preview", "")
                elapsed_ms   = item["elapsed_ms"]
                ts           = item["ts"]

                _is_dag = group == "dag" or "dag" in cap_name

                cap_id = str(uuid.uuid4())

                # Short text shown as the node label in the graph panel.
                # Includes 1–3 of the most descriptive param fields so the
                # node is informative without expanding it.
                text = "[" + cap_name + "]"
                if safe_params:
                    hint_parts = []
                    for k, v in list(safe_params.items())[:3]:
                        hv = str(v)
                        if len(hv) > 40:
                            hv = hv[:38] + "…"
                        hint_parts.append(f"{k}={hv}")
                    if hint_parts:
                        text += " " + ", ".join(hint_parts)
                if preview:
                    # Append a → preview so the node text reflects what
                    # actually happened, not just what was asked
                    p = preview[:120].replace("\n", " ").strip()
                    if p:
                        text += " → " + p
                text = text[:500]

                # Full-text body: structured for grep-style searches and
                # for the panel's expanded-node detail view.
                full_text = (
                    "Cap: " + cap_name + "\n"
                    + "Trace: " + trace_id + "\n"
                    + "Group: " + group + "\n"
                    + "Elapsed: " + str(elapsed_ms) + "ms\n"
                    + ("Trigger: " + trigger_cap
                       + (" via " + trigger_id[:12] if trigger_id else "")
                       + "\n" if trigger_cap else "")
                    + "\n--- INPUT ---\n" + params_json
                    + "\n\n--- OUTPUT ---\n" + result_json
                )

                # ── Persist via memory backend (single node) ──────────────
                graph_ok = False
                if mem_mod:
                    try:
                        MEMORY, MemRecord = mem_mod.MEMORY, mem_mod.MemoryRecord
                        rec = MemRecord(
                            id=cap_id, session_id=sid, trace_id=trace_id,
                            record_type=("dag_step" if _is_dag else "cap_call"),
                            source_type="tool",
                            category=("dag.step" if _is_dag else "cap." + group),
                            tags=[group, cap_name, "capability"]
                                 + (["dag"] if _is_dag else []),
                            keywords=[cap_name, group] + list(safe_params.keys())[:5],
                            text=text,
                            full_text=full_text,
                            importance=0.5 if _is_dag else 0.4,
                            capability=cap_name,
                            ai_output=group in ("llm", "agent", "chat"),
                            metadata={
                                "trace_id":    trace_id,
                                "trigger_id":  trigger_id,
                                "trigger_cap": trigger_cap,
                                "elapsed_ms":  elapsed_ms,
                                "group":       group,
                                "is_dag":      _is_dag,
                                "params":      safe_params,
                                "preview":     preview,
                            },
                            created_at=ts, updated_at=ts,
                        )
                        await MEMORY.store(rec)
                        graph_ok = True
                    except Exception as e:
                        log.warning("activity_worker store [%s]: %s",
                                    cap_name, e)

                # ── Edges ─────────────────────────────────────────────────
                # FOLLOWS_ACTIVITY links cap nodes in sequence.
                # TRIGGERED_BY_MSG links from the most recent human/AI
                # message to the first cap call in a new turn — bridging
                # the message chain and the cap chain. We only emit this
                # edge once per turn (when the prior cursor was a message
                # node, not another cap), detected via memory_hooks._LAST_MSG.
                if graph_ok and hooks:
                    try:
                        prior = _ACT_SESSION_CURSOR.get(sid, "")
                        if prior and prior != cap_id:
                            await hooks._link_nodes(
                                prior, cap_id, "FOLLOWS_ACTIVITY",
                                {"cap": cap_name, "ts": ts},
                                session_id=sid)

                        # Cross-link: if memory_hooks knows the last AI
                        # message node for this session, and we don't
                        # already have a cap node as the prior (i.e. this
                        # is the FIRST cap in a new turn), add a
                        # TRIGGERED_BY_MSG edge from the message → cap.
                        last_msg = getattr(hooks, "_LAST_MSG", {}).get(sid, "")
                        if last_msg and last_msg != cap_id and last_msg != prior:
                            await hooks._link_nodes(
                                last_msg, cap_id, "TRIGGERED_BY_MSG",
                                {"cap": cap_name, "ts": ts},
                                session_id=sid)
                    except Exception as e:
                        log.debug("activity_worker edges [%s]: %s", cap_name, e)

                # Cursor advances to this node — sets the chain head for
                # the next FOLLOWS_ACTIVITY link. The _TrackingCursor wrapper
                # also bumps the per-session chain counter.
                if graph_ok:
                    _ACT_SESSION_CURSOR[sid] = cap_id

                # ── Fabric ─────────────────────────────────────────────────
                # Never ingest fabric/memory/obs cap activity back into
                # fabric — doing so creates an event → ingest → event
                # cascade that doubles memory every ~20s.
                _SKIP_GROUPS = {"fabric", "memory", "obs", "health", "ui"}
                if group not in _SKIP_GROUPS and fabric:
                    dk = "cap:" + sid + ":" + trace_id
                    if dk not in _ACT_FABRIC_DEDUP:
                        try:
                            await fabric.ingest_dataset(
                                dataset_id="caps." + group,
                                data=[{
                                    "text":       text,
                                    "cap_name":   cap_name,
                                    "group":      group,
                                    "trace_id":   trace_id,
                                    "session_id": sid,
                                    "elapsed_ms": elapsed_ms,
                                    "params":     params_json,
                                    "result":     result_json,
                                    "preview":    preview,
                                    "node_id":    cap_id,
                                    "ts":         ts,
                                }],
                                source="capability_framework",
                                source_id=sid,
                                tags=[group, cap_name, "capability"],
                            )
                            _ACT_FABRIC_DEDUP.add(dk)
                            if len(_ACT_FABRIC_DEDUP) > 50000:
                                _ACT_FABRIC_DEDUP.clear()
                        except Exception as e:
                            log.debug("activity_worker fabric [%s]: %s", cap_name, e)

        except _act_asyncio.CancelledError:
            break
        except Exception as e:
            log.debug("activity_worker loop: %s", e)


def capability(
    name:        str,
    *,
    mode:        str            = "local",
    retries:     int            = 0,
    streams:     List[str]      = None,
    description: str            = None,
    tags:        List[str]      = None,
    # memory: how the unified activity recorder treats this cap.
    #   "on"   — record richly (call + output graph nodes, full params/result
    #            text, FOLLOWS_ACTIVITY chain links). DEFAULT.
    #   "off"  — opt out entirely (no graph node, no fabric entry).
    # The legacy "auto" value is accepted for compatibility and treated as "on".
    memory:      str            = "on",
    silent:      bool           = False,   # suppress cap.call/cap.ok events (polling caps)
    # ── Schema override ─────────────────────────────────────────────────────
    # Optional JSON-Schema fragment to enrich the auto-generated schema.
    # The decorator always runs generate_schema(func) to derive types and the
    # required list from the real Python signature.  If `schema` is provided
    # it is deep-merged on top via _merge_schema: descriptions, enums,
    # formats, and constraints from `schema` win, while auto-detected types
    # and defaults fill in anything the override omits.
    # Only supply `properties` (and optionally `required`) — the top-level
    # "type":"object" wrapper is always set automatically.
    schema:      Optional[dict] = None,
    # ── HTTP route options ──────────────────────────────────────────────────
    http_method: Optional[str]  = None,   # "GET" | "POST" | "PUT" | "DELETE"
    http_path:   Optional[str]  = None,   # e.g. "/ui/panels"  "/health"
    http_tags:   List[str]      = None,   # OpenAPI tags  (defaults to [group])
    # ── MCP config ─────────────────────────────────────────────────────────
    mcp_expose:  bool           = True,   # include in /mcp/tools listing
):
    """
    Unified registration decorator.

    @capability("ui.panels", memory="off", silent=True, http_method="GET", http_path="/ui/panels")
    async def get_ui_panels(trace_id=None):
        ...

    The decorated function is:
      1. Registered in CAPABILITY_REGISTRY (MCP + harness)
      2. Optionally mounted as a REST endpoint (if http_method + http_path given)
      3. All calls emitted as Redis events (observable) — unless silent=True
      4. Available via distributed dispatch if mode="distributed"

    silent=True suppresses cap.call/cap.ok events — use for high-frequency
    polling capabilities (health checks, obs.*, ui.panels etc.) to keep the
    syslog and terminal clean.

    http_method/path can be set AFTER decoration via cap["http_method"] etc
    because routes are mounted during lifespan, not at import time.
    """
    def deco(func):
        _auto_schema   = generate_schema(func)
        _final_schema  = _merge_schema(_auto_schema, schema) if schema else _auto_schema
        group  = name.split(".")[0]

        @functools.wraps(func)
        async def wrap(**kw):
            tid     = kw.pop("trace_id",None) or new_id()
            attempt = 0; last_err = None
            # Pull trigger chain from context vars (set by vera_syslog patcher)
            _vera_syslog = sys.modules.get("syslog")
            chain = _vera_syslog.get_trigger_chain() if _vera_syslog else {}
            _t0 = time.monotonic()
            while attempt <= retries:
                try:
                    _sid = (kw.get("session_id","") or chain.get("session_id","")
                            or _CURRENT_SESSION)
                    if not silent:
                        await emit_event({
                            "type":        "cap.call",
                            "name":        name,
                            "attempt":     attempt,
                            "trace_id":    tid,
                            "session_id":  _sid,
                            "trigger_id":  chain.get("trigger_id",""),
                            "trigger_cap": chain.get("trigger_cap",""),
                            "group":       group,
                        })
                    if mode=="distributed" and REDIS:
                        task_id=await dispatch_task(name,kw,tid)
                        # Per-cap timeout: LLM caps need 240-300s, research
                        # needs 60s, DAG composer caps need 600s. The previous
                        # 30s blanket default caused every llm.generate inside
                        # a DAG to fail with a useless "task timed out" error.
                        # Honour an explicit `_timeout` field in payload kw if
                        # the caller has special needs.
                        _t = float(kw.pop("_timeout", 0)) or _cap_timeout(name)
                        result =await wait_for_result(task_id, timeout=_t)
                    else:
                        # Filter kwargs to only those the function accepts,
                        # preventing TypeError on unexpected keyword arguments
                        # (e.g. dream system passing 'iterate' to a cap that
                        # doesn't have it in its signature).
                        _call_kw = _filter_kwargs_for_func(func, kw)
                        result=await func(**_call_kw,trace_id=tid)
                    for s in (streams or []):
                        await emit_stream(s,tid,result,name)
                    _elapsed_ms = round((time.monotonic()-_t0)*1000)
                    # Build result preview regardless of silent
                    _preview = ""
                    if isinstance(result, dict):
                        for _k in ("text","response","content","summary","result",
                                   "status","job_id","error","path","name"):
                            _v = result.get(_k)
                            if _v and isinstance(_v, str):
                                _preview = _v[:200]; break

                    # Cache result in Redis (all caps, even silent) for state inspection
                    if REDIS:
                        try:
                            _cache = {"name": name, "trace_id": tid,
                                      "session_id": _sid, "elapsed_ms": _elapsed_ms,
                                      "ts": now_iso(), "preview": _preview,
                                      "result": json.dumps(result)[:4096]
                                               if isinstance(result, (dict,list)) else str(result)[:4096]}
                            await REDIS.setex(
                                f"vera:cap:result:{name}",
                                300,  # 5 min TTL — recent state always inspectable
                                json.dumps(_cache)
                            )
                            # Also keep a sorted set of recent cap calls for monitoring
                            await REDIS.zadd("vera:cap:recent",
                                {json.dumps({"name": name, "tid": tid, "sid": _sid,
                                             "ts": now_iso(), "elapsed_ms": _elapsed_ms,
                                             "preview": _preview}): time.time()})
                            await REDIS.zremrangebyrank("vera:cap:recent", 0, -501)
                        except Exception:
                            pass

                    if not silent:
                        await emit_event({
                            "type":        "cap.ok",
                            "name":        name,
                            "trace_id":    tid,
                            "session_id":  _sid,
                            "group":       group,
                            "elapsed_ms":  _elapsed_ms,
                            "preview":     _preview,
                        })
                    # Unified activity recording. memory="off" opts out;
                    # everything else (default "on", legacy "auto") records
                    # richly via the activity worker. silent=True caps still
                    # opt out so polling/health caps don't flood the graph.
                    if (_sid and memory != "off" and not silent):
                        _act_enqueue(
                            cap_name=name, group=group, session_id=_sid,
                            trace_id=tid, kw=kw, result=result,
                            elapsed_ms=_elapsed_ms,
                            trigger_id=chain.get("trigger_id", ""),
                            trigger_cap=chain.get("trigger_cap", ""),
                        )
                    return result
                except Exception as e:
                    last_err=e; attempt+=1
                    _elapsed_ms = round((time.monotonic()-_t0)*1000)
                    _err_str = str(e)
                    # Cache error in Redis
                    if REDIS:
                        try:
                            await REDIS.setex(
                                f"vera:cap:error:{name}",
                                600,  # 10 min TTL
                                json.dumps({"name": name, "error": _err_str,
                                            "ts": now_iso(), "trace_id": tid,
                                            "elapsed_ms": _elapsed_ms})
                            )
                        except Exception:
                            pass
                    # cap.error ALWAYS emits — never silenced
                    await emit_event({
                        "type":        "cap.error",
                        "name":        name,
                        "error":       _err_str,
                        "attempt":     attempt,
                        "trace_id":    tid,
                        "session_id":  kw.get("session_id","") or chain.get("session_id",""),
                        "trigger_id":  chain.get("trigger_id",""),
                        "trigger_cap": chain.get("trigger_cap",""),
                        "group":       group,
                        "elapsed_ms":  _elapsed_ms,
                    })
                    if attempt<=retries: await asyncio.sleep(0.5*attempt)
            raise last_err

        CAPABILITY_REGISTRY[name]={
            "func":        wrap,
            "raw":         func,
            "schema":      _final_schema,
            "description": description or func.__doc__ or "",
            "streams":     streams or [],
            "mode":        mode,
            "retries":     retries,
            "tags":        tags or [group],
            "source":      "local",
            "mcp_expose":  mcp_expose,
            "memory":      memory,
            "silent":      silent,
            # HTTP route metadata — used at lifespan mount time
            "http_method": http_method,
            "http_path":   http_path,
            "http_tags":   http_tags or [http_path.split("/")[1] if http_path else group],
        }
        return wrap
    return deco


# ─────────────────────────────────────────────────────────────────────────────
# MCP PROXY REGISTRATION
# ─────────────────────────────────────────────────────────────────────────────
async def register_mcp_server(base_url: str, server_name: str) -> List[str]:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r=await c.get(f"{base_url}/mcp/tools"); r.raise_for_status(); tools=r.json()
    except Exception as e:
        log.error("register_mcp_server %s: %s",server_name,e); return []
    registered=[]
    for tool in tools:
        tool_name=f"{server_name}.{tool['name']}"
        async def _proxy(_url=base_url,_tool=tool["name"],**kwargs):
            tid=kwargs.pop("trace_id",new_id())
            async with httpx.AsyncClient(timeout=60) as c:
                r=await c.post(f"{_url}/mcp/call",json={"name":_tool,"arguments":kwargs,"trace_id":tid})
                r.raise_for_status(); return r.json()
        CAPABILITY_REGISTRY[tool_name]={
            "func":_proxy,"raw":_proxy,
            "schema":tool.get("schema",{"type":"object","properties":{}}),
            "description":tool.get("description",f"Proxied from {server_name}"),
            "streams":[],"mode":"proxy","source":"mcp_proxy",
            "server":server_name,"server_url":base_url,
            "tags":["proxy",server_name],"mcp_expose":True,
            "http_method":None,"http_path":None,"http_tags":["proxy"],
        }
        registered.append(tool_name)
    MCP_SERVERS[server_name]=base_url
    await emit_event({"type":"mcp_server.registered","name":server_name,"tools":registered,"url":base_url})
    return registered

# ─────────────────────────────────────────────────────────────────────────────
# DAG ENGINE
# ─────────────────────────────────────────────────────────────────────────────
async def run_graph(graph: list, state: dict, trace_id: str = "") -> dict:
    """Execute a DAG graph. trace_id flows through all cap calls for traceability."""
    _dag_trace = trace_id or new_id()
    for node in graph:
        if isinstance(node,list) and isinstance(node[0],list):
            results=await asyncio.gather(*[run_graph([n],dict(state),_dag_trace) for n in node],return_exceptions=True)
            for r in results:
                if isinstance(r,dict): state.update(r)
            continue
        cap_name,out_key,*rest=node; cond=rest[0] if rest else None
        if cond:
            if callable(cond) and not cond(state): continue
            if isinstance(cond,str) and cond.startswith("CONDITION:") and not state.get(cond.split(":",1)[1]): continue
        cap=CAPABILITY_REGISTRY.get(cap_name)
        if not cap:
            if out_key: state[out_key]={"error":f"unknown_cap:{cap_name}"}
            continue
        try:
            accepted=set(cap["schema"].get("properties",{}).keys())
            params={k:v for k,v in state.items() if k in accepted}
            result=await cap["func"](**params, trace_id=_dag_trace)
            if out_key: state[out_key]=result
            # Detect silent errors and enrich state with syslog context
            if isinstance(result, dict) and "error" in result:
                ctx = await _get_syslog_context(cap_name, str(result["error"]))
                if ctx and out_key:
                    state[f"_err_ctx_{out_key}"] = ctx
        except Exception as e:
            err_msg = str(e)
            ctx = await _get_syslog_context(cap_name, err_msg)
            state[out_key or f"_err_{cap_name}"] = {"error": err_msg, "syslog_context": ctx}
    return state


async def _get_syslog_context(cap_name: str, error_msg: str) -> str:
    """Fetch syslog error context for a cap — non-blocking, returns empty string on failure."""
    try:
        vera_syslog = sys.modules.get("syslog")
        if vera_syslog:
            return await vera_syslog.get_dag_error_context(cap_name, error_msg)
    except Exception:
        pass
    return ""

async def supervised_run_graph(graph: list, state: dict, supervision_every: int = 1, max_node_retries: int = 2) -> dict:
    log_entries=[]
    i=0
    while i<len(graph):
        node=graph[i]
        if isinstance(node,list) and isinstance(node[0],list):
            results=await asyncio.gather(*[run_graph([n],dict(state)) for n in node],return_exceptions=True)
            for r in results:
                if isinstance(r,dict): state.update(r)
            log_entries.append({"step":i,"type":"parallel","branches":len(node)})
        else:
            cap_name,out_key,*_=node; cap=CAPABILITY_REGISTRY.get(cap_name)
            if not cap:
                if out_key: state[out_key]={"error":"unknown"}
                log_entries.append({"step":i,"cap":cap_name,"error":"unknown"})
            else:
                attempt=0
                while attempt<=max_node_retries:
                    try:
                        accepted=set(cap["schema"].get("properties",{}).keys())
                        result=await cap["func"](**{k:v for k,v in state.items() if k in accepted})
                        if out_key: state[out_key]=result
                        log_entries.append({"step":i,"cap":cap_name,"attempt":attempt}); break
                    except Exception as e:
                        attempt+=1
                        if attempt>max_node_retries:
                            if out_key: state[out_key]={"error":str(e)}
                            log_entries.append({"step":i,"cap":cap_name,"error":str(e)})
        if (i+1)%supervision_every==0 and i<len(graph)-1:
            decision=await _llm_supervise(log_entries,state,graph[i+1:])
            action=decision.get("action","continue")
            await emit_event({"type":"supervision.checkpoint","step":i,"action":action,"decision":decision})
            if action=="abort":
                state["__aborted__"]=decision.get("reason","LLM aborted"); break
            elif action=="retry_node": graph.insert(i+1,node)
            elif action=="insert_node":
                cap_ins=decision.get("capability")
                if cap_ins and cap_ins in CAPABILITY_REGISTRY:
                    graph.insert(i+1,[cap_ins,decision.get("output_key","_inserted")])
        i+=1
    return state

async def _llm_supervise(log_entries, state, remaining):
    system=('You supervise a DAG. Review results, decide: continue|abort|retry_node|insert_node. '
            'Return ONLY JSON: {"action":"...","reason":"...","capability":"...","output_key":"..."}')
    raw=await ollama_generate(
        f"Log(last 5):\n{json.dumps(log_entries[-5:],indent=2)}\nState keys:{list(state.keys())}\nNext:{[n[0] if not isinstance(n[0],list) else 'parallel' for n in remaining[:3]]}",
        system=system,json_mode=True)
    try: return json.loads(raw)
    except: return {"action":"continue","reason":"parse_failed"}

async def plan_dag(goal: str, available_caps: Optional[List[str]] = None) -> dict:
    """
    Ask the LLM to produce a validated DAG plan for a natural-language goal.

    System prompt includes:
      - Full parameter schemas for every capability (not just names)
      - Strict DAG syntax rules with a worked example
      - Explicit instruction to ONLY use caps from the provided list
      - JSON extraction that tolerates chatty LLM responses
    """
    cap_keys = available_caps or list(CAPABILITY_REGISTRY.keys())

    # Build rich capability reference with types and required markers
    def _cap_sig(k):
        cap = CAPABILITY_REGISTRY.get(k, {})
        props = cap.get("schema", {}).get("properties", {})
        req   = set(cap.get("schema", {}).get("required", []))
        params = ", ".join(
            f"{p}:{v.get('type','str')}{'!' if p in req else ''}"
            for p, v in props.items()
            if p not in ("trace_id",)
        )
        desc = cap.get("description", "")[:80]
        return f"  {k}({params}) — {desc}"

    cap_desc = "\n".join(_cap_sig(k) for k in cap_keys)

    system = (
        "You are a Vera DAG planner. Build a minimal, correct DAG for the user's goal.\n\n"
        "RULES (violating any rule produces a broken DAG):\n"
        "1. ONLY use capability names from the provided list — no invented names.\n"
        "2. Output keys are arbitrary snake_case strings — they become state keys for later nodes.\n"
        "3. Capability inputs are matched from the state dict by parameter name.\n"
        "   Put required inputs in initial_state.\n"
        "4. Node formats:\n"
        "   Sequential  : [\"cap_name\", \"output_key\"]\n"
        "   Parallel    : [[\"cap_a\",\"key_a\"],[\"cap_b\",\"key_b\"]]  <-- array of arrays\n"
        "   Conditional : [\"cap_name\", \"output_key\", \"CONDITION:prior_key\"]\n"
        "5. Keep DAGs short (3-7 nodes). Do not add redundant steps.\n"
        "6. system.ping accepts: host(str!). Use it to check if a URL/host is reachable.\n"
        "7. http.get accepts: url(str!). Use it to fetch a URL and check the response.\n\n"
        "EXAMPLE — check if example.com is up:\n"
        '{"dag":[["http.get","site_resp"],["llm.generate","summary","CONDITION:site_resp"]],'
        '"initial_state":{"url":"http://example.com","prompt":"Summarise this HTTP response: {{site_resp}}"},'
        '"rationale":"Fetch the URL then summarise the result"}\n\n'
        "Return your response as brief prose explanation followed by a single ```json code block "
        "containing the plan. No other JSON blocks."
    )

    raw = await ollama_generate(
        f"Goal: {goal}\n\nAvailable capabilities:\n{cap_desc}",
        system=system,
        prefer_gpu=True,
        # Don't use json_mode — it prevents the LLM from adding the code fence we parse
    )

    if not raw:
        return {"error": "LLM returned empty response", "dag": [], "initial_state": {}}

    # Robust extraction — same 4-strategy approach as the client-side extractor
    def _extract(text: str) -> Optional[dict]:
        import re as _re
        # Strategy 1: fenced ```json blocks (preferred)
        for block in _re.findall(r'```(?:json)?\s*([\s\S]*?)```', text):
            try:
                p = json.loads(block.strip())
                if isinstance(p, dict) and isinstance(p.get("dag"), list):
                    return p
            except Exception:
                pass
        # Strategy 2: outermost {} object
        m = _re.search(r'\{[\s\S]*\}', text)
        if m:
            try:
                p = json.loads(m.group())
                if isinstance(p, dict) and isinstance(p.get("dag"), list):
                    return p
            except Exception:
                pass
        # Strategy 3: whole text
        try:
            p = json.loads(text.strip())
            if isinstance(p, dict) and isinstance(p.get("dag"), list):
                return p
        except Exception:
            pass
        return None

    plan = _extract(raw)
    if not plan:
        return {"error": f"Could not parse DAG from LLM response", "raw": raw[:500],
                "dag": [], "initial_state": {}}

    # Validate — reject any node whose capability is not in the registry
    unknown = []
    for node in _flatten_dag(plan.get("dag", [])):
        cap_name = node[0]
        if cap_name not in CAPABILITY_REGISTRY:
            unknown.append(cap_name)
    if unknown:
        log.warning("plan_dag: unknown caps in plan: %s", unknown)
        # Attempt self-correction: re-run with the bad caps highlighted
        fix_prompt = (
            f"Goal: {goal}\n\n"
            f"Your previous plan used these INVALID capability names that don't exist: {unknown}\n"
            f"You MUST only use capabilities from this list. Try again.\n\n"
            f"Available capabilities:\n{cap_desc}"
        )
        raw2 = await ollama_generate(fix_prompt, system=system, prefer_gpu=True)
        plan2 = _extract(raw2 or "")
        if plan2:
            unknown2 = [
                node[0] for node in _flatten_dag(plan2.get("dag", []))
                if node[0] not in CAPABILITY_REGISTRY
            ]
            if len(unknown2) < len(unknown):
                plan = plan2
                unknown = unknown2

        if unknown:
            plan["warnings"] = [f"Unknown capability in plan: {u}" for u in unknown]

    plan.setdefault("initial_state", {})
    plan.setdefault("rationale", "")
    return plan

def _flatten_dag(dag):
    flat=[]
    for node in dag:
        if isinstance(node,list) and isinstance(node[0],list):
            for n in node: flat.extend(_flatten_dag([n]))
        else: flat.append(node)
    return flat

async def route_llm(prompt: str, prefer: Optional[str] = None) -> Any:
    candidates=sorted([k for k in CAPABILITY_REGISTRY if "llm" in k.lower()],
                      key=lambda k:(0 if k==prefer else 1))
    for cap_name in candidates:
        try: return await CAPABILITY_REGISTRY[cap_name]["func"](prompt=prompt)
        except Exception as e: log.warning("route_llm %s: %s",cap_name,e)
    text=await ollama_generate(prompt)
    return {"text":text,"model":OLLAMA_MODEL,"fallback":True}

# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────────────────────────────────────
def schedule(fn: Callable, interval: float, name: Optional[str] = None):
    SCHEDULED_TASKS.append({"fn":fn,"int":interval,"name":name or fn.__name__,"last":None,"runs":0})

async def scheduler_loop():
    while True:
        now=datetime.utcnow()
        for task in SCHEDULED_TASKS:
            last=task["last"]
            if last is None or (now-last).total_seconds()>=task["int"]:
                task["last"]=now; task["runs"]+=1
                asyncio.create_task(task["fn"]())
                # NOTE: do NOT emit scheduler.run events — they flood the WS/obsIngest
                # causing O(n) unshift() churn on _obsEvents every second. Log only.
                log.debug("scheduler: %s run #%d", task["name"], task["runs"])
        await asyncio.sleep(1)

# ─────────────────────────────────────────────────────────────────────────────
#  ██  BUILT-IN CAPABILITIES  (all declared with @capability)
#      These replace every former @APP.get / @APP.post route.
# ─────────────────────────────────────────────────────────────────────────────

# ── MCP ───────────────────────────────────────────────────────────────────────

@capability("mcp.tools", memory="off", silent=True,
            http_method="GET", http_path="/mcp/tools", http_tags=["mcp"],
            mcp_expose=False,
            description="List all registered capabilities (MCP tool manifest).")
async def mcp_tools(trace_id=None):
    return [
        {"name":k,"description":v.get("description",""),"schema":v.get("schema",{}),
         "mode":v.get("mode","local"),"source":v.get("source","local"),
         "streams":v.get("streams",[]),"tags":v.get("tags",[])}
        for k,v in CAPABILITY_REGISTRY.items()
        if v.get("mcp_expose",True)
    ]

@capability("mcp.call", memory="auto",
            http_method="POST", http_path="/mcp/call", http_tags=["mcp"],
            mcp_expose=False,
            description="Invoke any capability by name via MCP protocol.")
async def mcp_call_endpoint(name: str, arguments: str = "", trace_id=None):
    """
    Called internally (from WS or DAG) with arguments already a dict.
    When called via REST the generic handler passes **body, so name and arguments
    arrive as kwargs — arguments will be a dict from JSON body.
    The str type hint is just for schema generation; we handle both.
    """
    if isinstance(arguments, str):
        try:    args = json.loads(arguments) if arguments.strip() else {}
        except: args = {}
    else:
        args = arguments or {}
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap:
        raise HTTPException(404, f"Unknown capability: {name}")
    tid    = trace_id or new_id()
    result = await cap["func"](**args, trace_id=tid)
    return {"type": "tool_result", "tool_name": name, "trace_id": tid, "content": result}


def _make_mcp_call_handler():
    """
    Dedicated handler for POST /mcp/call.
    Accepts body: {"name": "...", "arguments": {...}, "trace_id": "..."}
    where arguments is always a plain dict (not a JSON string).
    This bypasses the generic _make_post_handler to avoid the str/dict type confusion.
    """
    async def _handler(request: Request):
        try:
            raw  = await request.body()
            body = json.loads(raw) if raw.strip() else {}
        except Exception:
            raise HTTPException(400, "Invalid JSON body")

        name = body.get("name")
        if not name:
            raise HTTPException(400, "Missing 'name' in request body")

        args = body.get("arguments") or {}
        if isinstance(args, str):
            try:    args = json.loads(args)
            except: args = {}

        cap = CAPABILITY_REGISTRY.get(name)
        if not cap:
            raise HTTPException(404, f"Unknown capability: {name}")

        # Filter args to accepted params — prevents unexpected kwarg errors
        accepted = set(cap.get("schema", {}).get("properties", {}).keys())
        if accepted:
            args = {k: v for k, v in args.items() if k in accepted}

        # Server-side type coercion using the cap schema.
        # The LLM often emits integers and booleans as strings
        # (e.g. timeout="300" or timeout="300s" instead of timeout=300).
        # Without coercion these reach the cap function as strings and cause
        # TypeErrors at runtime (e.g. asyncio.wait_for(coro, timeout="300s")).
        import re as _re
        _UNIT_RE = _re.compile(r'^([\d.]+)\s*(ms|s|m|h|kb|mb|gb)?$', _re.I)
        schema_props = cap.get("schema", {}).get("properties", {})
        if schema_props:
            for k, v in list(args.items()):
                if v is None or k not in schema_props:
                    continue
                declared = schema_props[k].get("type", "")
                try:
                    if declared in ("integer", "number") and not isinstance(v, (int, float)):
                        sv = str(v).strip()
                        # Strip common unit suffixes that LLMs append:
                        # "300s" → "300", "10ms" → "10", "5m" → "5"
                        um = _UNIT_RE.match(sv)
                        if um:
                            sv = um.group(1)
                        n = float(sv)
                        args[k] = int(round(n)) if declared == "integer" else n
                    elif declared == "boolean" and not isinstance(v, bool):
                        sv = str(v).lower()
                        args[k] = sv in ("true", "1", "yes", "on")
                    elif declared == "string" and not isinstance(v, str):
                        args[k] = str(v)
                    elif declared == "array" and isinstance(v, str):
                        try:
                            args[k] = json.loads(v)
                        except Exception:
                            pass
                    elif declared == "object" and isinstance(v, str):
                        try:
                            args[k] = json.loads(v)
                        except Exception:
                            pass
                except Exception:
                    pass  # keep original if coercion fails

        tid = body.get("trace_id") or new_id()
        try:
            result = await cap["func"](**args, trace_id=tid)
            return JSONResponse(content=_json_safe(
                {"type": "tool_result", "tool_name": name, "trace_id": tid, "content": result}
            ))
        except HTTPException:
            raise
        except asyncio.CancelledError:
            log.debug("mcp/call cancelled (client disconnected) for %s", name)
            raise
        except Exception as e:
            log.error("mcp/call cap %s: %s", name, e)
            raise HTTPException(500, str(e))

    _handler.__name__ = "_post_mcp_call"
    return _handler

@capability("mcp.servers", memory="off", silent=True,
            http_method="GET", http_path="/mcp/servers", http_tags=["mcp"],
            description="List registered external MCP servers.")
async def mcp_servers_list(trace_id=None):
    return {"servers":MCP_SERVERS}

@capability("mcp.register_server", memory="off",
            http_method="POST", http_path="/mcp/servers/register", http_tags=["mcp"],
            description="Register an external MCP server and proxy its capabilities.")
async def mcp_register_server(url: str, name: str = "", trace_id=None):
    srv_name=name or url
    registered=await register_mcp_server(url,srv_name)
    return {"registered":registered,"server":srv_name,"count":len(registered)}

# ── Observability ─────────────────────────────────────────────────────────────

@capability("obs.health", memory="off", silent=True,
            http_method="GET", http_path="/health", http_tags=["obs"],
            description="Overall orchestrator health: backends, workers, caps, Ollama nodes.")
async def obs_health(trace_id=None):
    return {"redis":bool(REDIS),"postgres":bool(PG_POOL),"chroma":bool(CHROMA),
            "neo4j":bool(NEO),"workers":len(WORKER_REGISTRY),"caps":len(CAPABILITY_REGISTRY),
            "mcp_servers":len(MCP_SERVERS),
            "ollama":{iid:{"status":i["status"],"latency_ms":i["latency_ms"],"has_gpu":i["has_gpu"]}
                      for iid,i in OLLAMA_INSTANCES.items()},
            "mode":"distributed" if REDIS else "local"}

@capability("obs.workers", memory="off", silent=True,
            http_method="GET", http_path="/workers", http_tags=["obs"],
            description="Worker registry — all hosts merged from Redis + local fallback.")
async def obs_workers(trace_id=None):
    merged = {}

    # Read ALL workers from Redis first (source of truth across hosts)
    if REDIS:
        try:
            keys = await REDIS.keys("vera:workers:*")
            for k in keys:
                raw = await REDIS.hgetall(k)
                if not raw:
                    continue
                # Decode bytes keys/values
                rec = {
                    (rk.decode() if isinstance(rk, bytes) else rk):
                    (rv.decode() if isinstance(rv, bytes) else rv)
                    for rk, rv in raw.items()
                }
                # Derive worker id from key if not in record
                key_str = k.decode() if isinstance(k, bytes) else k
                wid = rec.get("id") or key_str.rsplit(":", 1)[-1]

                # Deserialise capabilities — handle both JSON and str() formats
                caps_raw = rec.get("capabilities", "[]")
                try:
                    rec["capabilities"] = json.loads(caps_raw)
                except (json.JSONDecodeError, TypeError):
                    # Fallback: try to parse Python repr single-quoted list
                    try:
                        import ast
                        rec["capabilities"] = ast.literal_eval(caps_raw)
                    except Exception:
                        rec["capabilities"] = []

                # Coerce all numeric fields
                for field in ("tasks_done", "tasks_failed", "cap_count"):
                    try:
                        rec[field] = int(float(rec.get(field, 0) or 0))
                    except (ValueError, TypeError):
                        rec[field] = 0

                # Ensure all expected fields exist
                rec.setdefault("host", "unknown")
                rec.setdefault("status", "unknown")
                rec.setdefault("started", "")
                rec.setdefault("current_task", "")
                rec.setdefault("task_started", "")
                rec.setdefault("ollama_instance", "")
                rec.setdefault("pid", "")

                merged[wid] = rec
        except Exception as e:
            log.warning("obs.workers Redis scan: %s", e)

    # Overlay local in-process data (more accurate for this host's workers)
    for wid, local in WORKER_REGISTRY.items():
        if wid in merged:
            # Update Redis record with live in-process values
            merged[wid].update({
                "status":       local.get("status", "idle"),
                "tasks_done":   local.get("tasks_done", 0),
                "tasks_failed": local.get("tasks_failed", 0),
                "current_task": local.get("current_task", ""),
                "task_started": local.get("task_started", ""),
                "ollama_instance": local.get("ollama_instance", ""),
                "capabilities": local.get("capabilities", []) if isinstance(local.get("capabilities"), list)
                                 else merged[wid].get("capabilities", []),
            })
        else:
            # Worker exists locally but not in Redis yet — include it
            rec = dict(local)
            if isinstance(rec.get("capabilities"), str):
                try:
                    rec["capabilities"] = json.loads(rec["capabilities"])
                except Exception:
                    rec["capabilities"] = []
            merged[wid] = rec

    return merged

@capability("obs.pending", memory="off", silent=True,
            http_method="GET", http_path="/pending", http_tags=["obs"],
            description="Pending result futures (tasks awaiting distributed completion).")
async def obs_pending(trace_id=None):
    return {"count":len(PENDING_RESULTS),"ids":list(PENDING_RESULTS.keys())}

@capability("obs.scheduler", memory="off", silent=True,
            http_method="GET", http_path="/scheduler", http_tags=["obs"],
            description="Scheduled background jobs — name, interval, run count, last run.")
async def obs_scheduler(trace_id=None):
    return [{"name":t["name"],"interval":t["int"],"runs":t["runs"],
             "last":t["last"].isoformat() if t["last"] else None}
            for t in SCHEDULED_TASKS]

@capability("obs.events", memory="off", silent=True,
            http_method="GET", http_path="/events", http_tags=["obs"],
            description="Recent events from Redis event stream.")
async def obs_events(limit: int = 100, trace_id=None):
    if not REDIS: return []
    data=await REDIS.xrevrange(EVENT_STREAM,count=min(limit,500))
    return [json.loads(x[1][b"data"]) for x in data]

@capability("obs.stream_history", memory="off", silent=True,
            http_method="GET", http_path="/streams/history", http_tags=["obs"],
            description="Recent messages from a named Redis stream. Pass ?name=stream_name&limit=50")
async def obs_stream_history(name: str = "", limit: int = 50, trace_id=None):
    if not REDIS or not name: return []
    data=await REDIS.xrevrange(f"vera:stream:{name}",count=min(limit,500))
    return [json.loads(x[1][b"data"]) for x in data]

@capability("obs.redis", memory="off", silent=True,
            http_method="GET", http_path="/redis/inspect", http_tags=["obs"],
            description="Redis server info and vera:* stream key statistics.")
async def obs_redis(trace_id=None):
    if not REDIS: return {"error":"Redis not connected"}
    try:
        info=await REDIS.info(); keys=await REDIS.keys("vera:*"); stats={}
        for k in keys:
            ks=k.decode() if isinstance(k,bytes) else k
            t=(await REDIS.type(k)).decode()
            stats[ks]={"type":t,**({"length":await REDIS.xlen(k)} if t=="stream" else {})}
        return {"connected_clients":info.get("connected_clients"),
                "used_memory_human":info.get("used_memory_human"),
                "uptime_days":info.get("uptime_in_days"),"keys":stats}
    except Exception as e: return {"error":str(e)}

@capability("obs.diagnostics", memory="off", silent=True,
            http_method="GET", http_path="/diagnostics", http_tags=["obs"],
            description="Full connection diagnostics — Redis reachability, bind config, "
                        "worker count, host identity. Use to debug multi-host setup.")
async def obs_diagnostics(trace_id=None):
    import socket
    diag: dict = {
        "host":        socket.gethostname(),
        "redis_url":   REDIS_URL,
        "redis_connected": bool(REDIS),
        "worker_count_local": len(WORKER_REGISTRY),
        "caps": len(CAPABILITY_REGISTRY),
        "mode": "distributed" if REDIS else "local",
    }

    # Try a live Redis ping even if REDIS is already set
    if HAS_REDIS:
        try:
            probe = aioredis.from_url(REDIS_URL, decode_responses=False,
                                      socket_connect_timeout=3, socket_timeout=3)
            await probe.ping()
            info  = await probe.info("server")
            diag["redis_ping"]    = "ok"
            diag["redis_version"] = info.get("redis_version")
            diag["redis_bind"]    = info.get("bind", "not reported")
            diag["redis_port"]    = info.get("tcp_port")
            # Check worker keys from ALL hosts
            wkeys = await probe.keys("vera:workers:*")
            diag["workers_in_redis"] = len(wkeys)
            diag["worker_ids"] = [k.decode().split(":")[-1] for k in wkeys]
            await probe.aclose()
        except Exception as e:
            diag["redis_ping"]  = f"FAILED: {e}"
            diag["redis_hint"]  = (
                "Ping succeeded but Redis connection failed. Common causes: "
                "(1) redis.conf 'bind 127.0.0.1' — change to 'bind 0.0.0.0' or add this host's IP. "
                "(2) 'requirepass' set — add :password@ to REDIS_URL. "
                "(3) 'protected-mode yes' with no bind — set protected-mode no. "
                "(4) firewall blocking port 6379."
            )
    else:
        diag["redis_hint"] = "redis.asyncio not installed"

    return diag

# ── Ollama ────────────────────────────────────────────────────────────────────

@capability("obs.modules", memory="off", silent=True,
            http_method="GET", http_path="/modules", http_tags=["obs"],
            description="List all capability modules loaded at startup — name, path, caps added, status.")
async def obs_modules(trace_id=None):
    return {"modules": LOADED_MODULES, "count": len(LOADED_MODULES)}


@capability("ollama.instances", memory="off", silent=True,
            http_method="GET", http_path="/ollama/instances", http_tags=["ollama"],
            description="Live status of all Ollama cluster nodes.")
async def cap_ollama_instances(trace_id=None):
    return {iid:{"url":i["url"],"label":i["label"],"has_gpu":i["has_gpu"],
                 "status":i["status"],"latency_ms":i["latency_ms"],"models":i["models"],
                 "in_use":i["in_use"],"errors":i["errors"],"last_check":i["last_check"],
                 "num_ctx":i.get("num_ctx", 4096)}
            for iid,i in OLLAMA_INSTANCES.items()}

@capability("ollama.add_instance", memory="off",
            http_method="POST", http_path="/ollama/instances/add", http_tags=["ollama"],
            description="Dynamically add an Ollama instance to the cluster.")
async def cap_add_instance(id: str, url: str, has_gpu: bool = False, label: str = "", trace_id=None):
    add_ollama_instance(id,url,has_gpu=has_gpu,label=label)
    await _ping_instance(id,OLLAMA_INSTANCES[id])
    return OLLAMA_INSTANCES[id]

@capability("ollama.ping_instance", memory="off",
            http_method="POST", http_path="/ollama/ping", http_tags=["ollama"],
            description="Ping a specific Ollama instance and update its status.")
async def cap_ping_instance(instance_id: str, trace_id=None):
    inst=OLLAMA_INSTANCES.get(instance_id)
    if not inst: return {"error":f"Unknown instance: {instance_id}"}
    await _ping_instance(instance_id,inst); return inst

@capability("ollama.pull", memory="off",
            http_method="POST", http_path="/ollama/pull", http_tags=["ollama"],
            description="Pull a model onto a specific Ollama instance.")
async def cap_ollama_pull(model: str, instance_id: str, trace_id=None):
    inst=OLLAMA_INSTANCES.get(instance_id)
    if not inst: return {"error":f"Unknown instance: {instance_id}"}
    try:
        async with httpx.AsyncClient(timeout=600) as c:
            r=await c.post(f"{inst['url']}/api/pull",json={"name":model,"stream":False})
            r.raise_for_status(); return {"model":model,"instance":instance_id,"status":"pulled",**r.json()}
    except Exception as e: return {"model":model,"instance":instance_id,"error":str(e)}

@capability("ollama.request_log", memory="off",
            http_method="GET", http_path="/ollama/request_log", http_tags=["ollama"],
            description="Return the recent Ollama request log (in-process ring buffer). "
                        "Shows caller, model, instance, timing, and status for every "
                        "ollama_generate call. Query: limit (int, default 50), "
                        "caller_file (str, filter), status (str, filter).")
async def cap_ollama_request_log(limit: int = 50, caller_file: str = "",
                                  status: str = "", trace_id=None):
    entries = list(reversed(_OLLAMA_REQUEST_LOG))  # newest first
    if caller_file:
        entries = [e for e in entries if caller_file in e.get("caller_file", "")]
    if status:
        entries = [e for e in entries if e.get("status", "") == status]
    return {"entries": entries[:limit], "total": len(_OLLAMA_REQUEST_LOG)}


# ── Embedding configuration (runtime-adjustable) ────────────────────────────

_EMBED_PREFER_GPU: bool = False   # default: route via normal pick_instance
_EMBED_INSTANCE_ID: Optional[str] = None  # pin to specific instance, or None

@capability("ollama.embed_config", memory="off",
            http_method="GET", http_path="/ollama/embed_config",
            http_tags=["ollama"],
            description="Return current embedding configuration: model, URL, "
                        "preferred instance, GPU preference.")
async def cap_ollama_embed_config(trace_id=None):
    return {
        "embed_model":      OLLAMA_EMBED_MODEL,
        "embed_url":        OLLAMA_EMBED_URL,
        "prefer_gpu":       _EMBED_PREFER_GPU,
        "pinned_instance":  _EMBED_INSTANCE_ID,
        "instances":        {iid: {"label": i.get("label",""), "has_gpu": i.get("has_gpu", False),
                                   "status": i.get("status",""), "models": i.get("models",[])}
                             for iid, i in OLLAMA_INSTANCES.items()},
    }

@capability("ollama.embed_config_set", memory="off",
            http_method="POST", http_path="/ollama/embed_config",
            http_tags=["ollama"],
            description="Update embedding configuration at runtime. "
                        "Fields: embed_model (str), prefer_gpu (bool), "
                        "pinned_instance (str or empty to clear).")
async def cap_ollama_embed_config_set(
    embed_model: str = "",
    prefer_gpu: Optional[bool] = None,
    pinned_instance: str = "",
    trace_id=None,
):
    global OLLAMA_EMBED_MODEL, OLLAMA_EMBED_URL
    global _EMBED_PREFER_GPU, _EMBED_INSTANCE_ID
    changes = {}
    if embed_model:
        OLLAMA_EMBED_MODEL = embed_model
        changes["embed_model"] = embed_model
    if prefer_gpu is not None:
        _EMBED_PREFER_GPU = prefer_gpu
        changes["prefer_gpu"] = prefer_gpu
    if pinned_instance == "__clear__":
        _EMBED_INSTANCE_ID = None
        changes["pinned_instance"] = None
    elif pinned_instance:
        if pinned_instance in OLLAMA_INSTANCES:
            _EMBED_INSTANCE_ID = pinned_instance
            changes["pinned_instance"] = pinned_instance
        else:
            return {"error": f"Unknown instance: {pinned_instance}",
                    "available": list(OLLAMA_INSTANCES.keys())}
    await emit_event({"type": "ollama.embed_config_changed", **changes})
    return {
        "embed_model":      OLLAMA_EMBED_MODEL,
        "embed_url":        OLLAMA_EMBED_URL,
        "prefer_gpu":       _EMBED_PREFER_GPU,
        "pinned_instance":  _EMBED_INSTANCE_ID,
        "changes":          changes,
    }


# ── DAG ───────────────────────────────────────────────────────────────────────

@capability("dag.run", memory="on",
            http_method="POST", http_path="/dag/run", http_tags=["dag"],
            description="Execute a DAG against an initial state. Set supervised=true for LLM checkpoints.")
async def cap_dag_run(dag: list = None, state: dict = None, supervised: bool = False, trace_id=None):
    fn=supervised_run_graph if supervised else run_graph
    result=await fn(dag or [],state or {})
    return {"trace_id":trace_id or new_id(),"result":result}

@capability("dag.plan", memory="on",
            http_method="POST", http_path="/dag/plan", http_tags=["dag"],
            description="Ask the LLM to produce a DAG execution plan for a natural-language goal.")
async def cap_dag_plan(goal: str, capabilities: list = None, trace_id=None):
    return await plan_dag(goal,capabilities)

@capability("dag.plan_and_run", memory="on",
            http_method="POST", http_path="/dag/plan_and_run", http_tags=["dag"],
            description="Plan a DAG from a goal then immediately execute it.")
async def cap_dag_plan_and_run(goal: str, supervised: bool = True, trace_id=None):
    plan=await plan_dag(goal)
    if plan.get("error") and not plan.get("dag"): return {"error":plan["error"]}
    fn=supervised_run_graph if supervised else run_graph
    result=await fn(plan.get("dag",[]),plan.get("initial_state",{}))
    return {"plan":plan,"result":result,"supervised":supervised}

@capability("cluster.instance_update", memory="off",
            http_method="POST", http_path="/cluster/instance/update",
            http_tags=["cluster"],
            description="Update mutable fields on an Ollama instance (num_ctx, label, etc).")
async def cluster_instance_update(id: str, num_ctx: int = 0, label: str = "",
                                   trace_id=None):
    inst = OLLAMA_INSTANCES.get(id)
    if not inst:
        return {"error": f"Instance not found: {id}"}
    if num_ctx > 0:
        inst["num_ctx"] = num_ctx
    if label:
        inst["label"] = label
    await emit_event({"type": "cluster.instance_updated", "id": id, "num_ctx": num_ctx})
    return {"id": id, "num_ctx": inst.get("num_ctx", 0), "label": inst.get("label", id)}


# ── LLM ───────────────────────────────────────────────────────────────────────

@capability("llm.route", memory="off",
            http_method="POST", http_path="/llm/route", http_tags=["llm"],
            description="Route a prompt through the best available LLM capability with fallback chain.")
async def cap_llm_route(prompt: str, prefer: str = "", trace_id=None):
    return await route_llm(prompt,prefer=prefer or None)

# ── Minimal built-ins (full LLM group lives in vera_capabilities.py) ──────────

@capability("echo", http_method="POST", http_path="/debug/echo", http_tags=["debug"], memory="off", description="Echo a message back with timestamp.")
async def _echo(message: str, trace_id=None):
    return {"echo":message,"ts":now_iso(),"trace_id":trace_id}

@capability("health.check", http_method="GET", http_path="/debug/health", http_tags=["debug"], memory="off", silent=True, description="Quick health check — alias of obs.health.")
async def _health(trace_id=None):
    return await obs_health(trace_id=trace_id)

# ui.panels lives here (not in vera_capabilities) so /ui/panels is ALWAYS available
# even when vera_capabilities.py hasn't loaded yet.
@capability("ui.panels", memory="off", silent=True,
            http_method="GET", http_path="/ui/panels", http_tags=["ui"],
            description="List all registered built-in UI panels injected by capability modules.")
async def _ui_panels(trace_id=None):
    return list(UI_PANELS.values())

async def _heartbeat():
    await emit_event({"type":"heartbeat","caps":len(CAPABILITY_REGISTRY),"workers":len(WORKER_REGISTRY)})

schedule(_heartbeat,30,"heartbeat")

# ─────────────────────────────────────────────────────────────────────────────
# APP + LIFESPAN
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global REDIS, PG_POOL, CHROMA, NEO

    # ── DB connections run in background so the server is available immediately ──
    # On reboot, Redis/Postgres may take 10-30s to start. Running them in lifespan
    # before yield blocks ALL HTTP/WS connections until they complete or timeout.
    # Instead: attempt once quickly, then retry in background every 5s.

    async def _connect_backends():
        global REDIS, PG_POOL, CHROMA, NEO
        # Redis
        if HAS_REDIS and REDIS is None:
            for _attempt in range(1, 999):
                try:
                    _r = aioredis.from_url(
                        REDIS_URL, decode_responses=False,
                        socket_connect_timeout=4, socket_timeout=4,
                    )
                    await _r.ping()
                    info = await _r.info("server")
                    REDIS = _r
                    log.info("✓ Redis connected (attempt %d): %s v%s",
                             _attempt, REDIS_URL, info.get("redis_version", "?"))
                    await emit_event({"type": "backend.connected", "backend": "redis"})
                    break
                except Exception as e:
                    if _attempt == 1:
                        log.error(
                            "✗ Redis not ready yet (will retry every 5s): %s\n"
                            "  URL  : %s\n"
                            "  Hint : check bind address in redis.conf, requirepass, firewall",
                            e, REDIS_URL,
                        )
                    await asyncio.sleep(5)
        else:
            if not HAS_REDIS:
                log.warning("redis.asyncio not installed — pip install 'redis[asyncio]'")

        # Postgres
        if HAS_PG and PG_POOL is None:
            for _attempt in range(1, 999):
                try:
                    PG_POOL = await asyncpg.create_pool(POSTGRES_URL, min_size=2, max_size=10)
                    async with PG_POOL.acquire() as conn:
                        await conn.execute(
                            "CREATE TABLE IF NOT EXISTS vera_task_results"
                            "(task_id TEXT PRIMARY KEY,result JSONB,"
                            "ts TIMESTAMPTZ DEFAULT NOW())"
                        )
                    log.info("✓ Postgres connected (attempt %d)", _attempt)
                    await emit_event({"type": "backend.connected", "backend": "postgres"})
                    break
                except Exception as e:
                    if _attempt == 1:
                        log.warning("Postgres not ready yet (will retry): %s", e)
                    await asyncio.sleep(5)

        # ChromaDB (fast — local HTTP, no retry loop needed)
        if HAS_CHROMA and CHROMA is None:
            try:
                CHROMA = chromadb.HttpClient(host="localhost", port=8008)
                log.info("✓ ChromaDB")
                await emit_event({"type": "backend.connected", "backend": "chroma"})
            except Exception as e:
                log.warning("ChromaDB unavail: %s", e)

        # Neo4j (fast driver init — no retry loop needed)
        if HAS_NEO and NEO is None:
            try:
                NEO = AsyncGraphDatabase.driver("bolt://localhost:7687")
                log.info("✓ Neo4j")
                await emit_event({"type": "backend.connected", "backend": "neo4j"})
            except Exception as e:
                log.warning("Neo4j unavail: %s", e)

    # ── Load companion capability modules ────────────────────────────────────
    # Search for vera_capabilities.py / vera_skills.py relative to THIS file,
    # so they load correctly regardless of the working directory.
    import importlib.util as _ilu
    _here = os.path.dirname(os.path.abspath(__file__))

    # Extra module paths: env var VERA_MODULES="path1.py,path2.py" adds more
    _module_files = [
        os.path.join(_here, "capabilities/capabilities.py"),
        os.path.join(_here, "capabilities/cap_hub_capabilities.py"),
        os.path.join(_here, "capabilities/cap_tracking.py"),
        os.path.join(_here, "fabric/memory.py"),
        os.path.join(_here, "fabric/memory_hooks.py"),
        os.path.join(_here, "fabric/data_fabric_collectors.py"),
        os.path.join(_here, "fabric/data_fabric.py"),
        os.path.join(_here, "fabric/fabric_web_acquisition.py"),
        os.path.join(_here, "fabric/memory_second_order.py"),
        os.path.join(_here, "fabric/context.py"),
        os.path.join(_here, "fabric/discovery.py"),
        os.path.join(_here, "skills/skills.py"),
        os.path.join(_here, "skills/skills_owl.py"),
        os.path.join(_here, "dag/dag_store.py"),
        os.path.join(_here, "dag/dag_workshop_capabilities.py"),
        os.path.join(_here, "agents/agents.py"),
        os.path.join(_here, "workers/cluster.py"),
        os.path.join(_here, "workers/syslog.py"),
        os.path.join(_here, "ui builder/ui_capabilities.py"),
        os.path.join(_here, "ide/ide_capabilities.py"),
        os.path.join(_here, "ide/ide_code_capabilities.py"),
        os.path.join(_here, "ide/ide_inspect_capabilities.py"),
        os.path.join(_here, "research/research_fabric.py"),        
        # os.path.join(_here, "research_capabilities.py"),
        # os.path.join(_here, "research_recall_capabilities.py"),
        # os.path.join(_here, "research_activity_capabilities.py"),
        os.path.join(_here, "web/web_capabilities.py"),
        os.path.join(_here, "telegram/telegram_capabilities.py"),
        os.path.join(_here, "dream/dream_capabilities.py"),
        os.path.join(_here, "dream/project_capabilities.py"),
        os.path.join(_here, "execution/exec_capabilities.py"),
        os.path.join(_here, "workers/workers.py"),
        os.path.join(_here, "web/browser_capabilities.py"),
        # os.path.join(_here, "vllm/vllm_capabilities.py"),
        os.path.join(_here, "machine learning/ml_workshop.py"),
        os.path.join(_here, "machine learning/ml_training.py"),
        # os.path.join(_here, "openclaw/openclaw_capabilities.py"),
        # os.path.join(_here, "dream/dream_research_integration.py"),
        # os.path.join(_here, "project_research_extension.py"),
        os.path.join(_here, "ontologies/cap_ontology.py"),
        os.path.join(_here, "chat/chat_panels_capabilities.py"),
        os.path.join(_here, "agent_loop_output_capabilities.py"),
        os.path.join(_here, "worldview/worldview_jepa.py"),
        os.path.join(_here, "research/researcher_api.py"),
        os.path.join(_here, "vector browser/vector_browser_capabilites.py"),
        os.path.join(_here, "workers/job_persistance.py"),
        os.path.join(_here, "vera_graph_panels.py")
        
    ]
    _extra = os.getenv("VERA_MODULES", "")
    if _extra:
        _module_files += [p.strip() for p in _extra.split(",") if p.strip()]

    for _fpath in _module_files:
        _mod_name = os.path.splitext(os.path.basename(_fpath))[0]
        if not os.path.exists(_fpath):
            log.warning("Module not found (skipping): %s", _fpath)
            continue
        if _mod_name in sys.modules:
            log.debug("Module already loaded: %s", _mod_name)
            continue
        try:
            _spec = _ilu.spec_from_file_location(_mod_name, _fpath)
            _mod  = _ilu.module_from_spec(_spec)
            sys.modules[_mod_name] = _mod
            _caps_before = len(CAPABILITY_REGISTRY)
            _spec.loader.exec_module(_mod)
            _caps_added = len(CAPABILITY_REGISTRY) - _caps_before
            log.info("✓ %-25s caps=%-3d  ui_panels=%d",
                     _mod_name, len(CAPABILITY_REGISTRY), len(UI_PANELS))
            LOADED_MODULES.append({
                "name": _mod_name, "path": _fpath,
                "caps_added": _caps_added, "status": "ok",
            })
        except Exception as e:
            log.error("✗ %s failed to load: %s", _mod_name, e)
            import traceback as _tb; log.error(_tb.format_exc())
            LOADED_MODULES.append({
                "name": _mod_name, "path": _fpath,
                "caps_added": 0, "status": f"error: {e}",
            })
            _caps_before = len(CAPABILITY_REGISTRY)
    
    # import Vera.Orchestration.cap_tracking as cap_tracking
    # cap_tracking.install(sys.modules[__name__])
    
    import Vera.Orchestration.agents.agents_context_patch

    _ct = sys.modules.get("cap_tracking")
    if _ct:
        _ct.install(sys.modules[__name__])
        asyncio.create_task(_activity_worker())
        log.info("activity_worker: started via cap_tracking_config")
    elif os.getenv("VERA_ACTIVITY_RECORDING", "0") == "1":
        asyncio.create_task(_activity_worker())

    # Mount ALL capability HTTP routes in one pass
    _mount_all_http_routes(app)

    # Install asyncio uncaught-exception handler
    try:
        import asyncio as _aio
        _aio.get_event_loop().set_exception_handler(_asyncio_exception_handler)
    except Exception:
        pass

    # Background tasks — start before yield so they run immediately
    worker_id=f"worker-{new_id()[:8]}"
    asyncio.create_task(_connect_backends())       # DB connections with retry — non-blocking
    asyncio.create_task(worker_loop(worker_id))
    asyncio.create_task(result_listener())
    asyncio.create_task(scheduler_loop())
    asyncio.create_task(instance_health_loop(interval=20))
    # Activity recording — disabled by default, enable with VERA_ACTIVITY_RECORDING=1
    if os.getenv("VERA_ACTIVITY_RECORDING", "0") == "1":
        asyncio.create_task(_activity_worker())
        log.info("activity_worker: started (VERA_ACTIVITY_RECORDING=1)")
    else:
        log.debug("activity_worker: disabled (set VERA_ACTIVITY_RECORDING=1 to enable)")

    log.info("Vera Orchestrator v3 ready — %d caps, %d Ollama nodes",len(CAPABILITY_REGISTRY),len(OLLAMA_INSTANCES))
    yield
    if REDIS:   await REDIS.aclose()
    if PG_POOL: await PG_POOL.close()
    if NEO:     await NEO.close()
    log.info("Vera shut down")


def _make_get_handler(cap: dict, cap_name: str):
    """
    Build a GET handler that reads query params, type-coerces them from the
    capability schema, and wraps execution with full disconnect safety.
    Uses a factory function to avoid the closure-in-loop variable capture bug.
    """
    schema_props = cap["schema"].get("properties", {})

    async def _handler(request: Request):
        params   = dict(request.query_params)
        coerced  = {}
        for k, v in params.items():
            if k == "trace_id":
                continue
            stype = schema_props.get(k, {}).get("type", "string")
            try:
                if   stype == "integer": coerced[k] = int(v)
                elif stype == "number":  coerced[k] = float(v)
                elif stype == "boolean": coerced[k] = v.lower() in ("true", "1", "yes")
                else:                    coerced[k] = v
            except (ValueError, TypeError):
                coerced[k] = v
        try:
            result = await cap["func"](**coerced, trace_id=new_id())
            return JSONResponse(content=_json_safe(result))
        except HTTPException:
            raise
        except Exception as e:
            log.error("GET cap %s: %s", cap_name, e)
            raise HTTPException(500, str(e))

    _handler.__name__ = f"_get_{cap_name.replace('.','_')}"
    return _handler


def _make_post_handler(cap: dict, cap_name: str):
    """
    Build a POST handler.  Body is received as a raw Request so we can:
      - avoid the mutable-default-dict  body: dict = {}  footgun
      - handle disconnect (client gone before slow cap finishes) cleanly
      - support both {"name":"x","arguments":{}} envelope and flat {"key":"val"} bodies

    Body keys are filtered to the function's schema so unknown UI fields never
    cause 'unexpected keyword argument' errors — forward/backward compatible.
    """
    # Pre-compute accepted parameter names from schema (excludes trace_id)
    _accepted = set(cap.get("schema", {}).get("properties", {}).keys())

    async def _handler(request: Request):
        # Read body — empty body is fine (no params needed)
        try:
            raw = await request.body()
            body: dict = json.loads(raw) if raw.strip() else {}
        except Exception:
            body = {}

        tid = body.pop("trace_id", None) or new_id()

        # Filter to only accepted params — prevents 'unexpected keyword argument'
        # when the UI sends fields the current function version doesn't know about yet.
        # If _accepted is empty (no schema), pass everything through unchanged.
        if _accepted:
            body = {k: v for k, v in body.items() if k in _accepted}

        try:
            result = await cap["func"](**body, trace_id=tid)
            return JSONResponse(content=_json_safe(result))
        except HTTPException:
            raise
        except (asyncio.CancelledError, RuntimeError) as e:
            if isinstance(e, asyncio.CancelledError) or \
               "transport" in str(e).lower() or "closed" in str(e).lower():
                log.debug("Client disconnected: %s", cap_name)
                raise HTTPException(499, "Client disconnected")
            raise
        except Exception as e:
            log.error("POST cap %s: %s", cap_name, e)
            raise HTTPException(500, str(e))

    _handler.__name__ = f"_post_{cap_name.replace('.','_')}"
    return _handler


def _json_safe(obj: Any) -> Any:
    """Recursively make an object JSON-serialisable, replacing unserializable values."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


def _mount_all_http_routes(app: FastAPI):
    """
    Single pass — mounts every @capability that declared http_method + http_path,
    then auto-mounts POST /<cap>/<name> for every cap without an explicit route.

    Key correctness guarantees:
      • No mutable default arguments (body: dict = {} bug avoided via Request)
      • No closure-in-loop variable capture (factory functions used)
      • RuntimeError / CancelledError from client disconnect caught per-handler
      • GET params type-coerced from schema
      • POST body read from raw Request, not FastAPI model binding
    """
    claimed_paths: set = set()

    # ── Pass 1: explicit http_method + http_path declared on the capability ──────
    for cap_name, cap in list(CAPABILITY_REGISTRY.items()):
        method = (cap.get("http_method") or "").upper()
        path   = cap.get("http_path") or ""
        if not method or not path:
            continue

        claimed_paths.add((method, path))
        api_tags = cap.get("http_tags") or [cap_name.split(".")[0]]
        summary  = (cap.get("description") or cap_name)[:120]

        # /mcp/call gets its own hand-crafted handler (name+arguments envelope)
        if path == "/mcp/call":
            handler = _make_mcp_call_handler()
        elif method == "GET":
            handler = _make_get_handler(cap, cap_name)
        else:
            handler = _make_post_handler(cap, cap_name)

        try:
            app.add_api_route(path, handler, methods=[method],
                              tags=api_tags, summary=summary)
            log.debug("Mounted: %s %s → %s", method, path, cap_name)
        except Exception as e:
            log.warning("Route mount failed %s %s: %s", method, path, e)

    # ── Pass 2: auto-mount POST /<group>/<name> for every remaining cap ──────────
    for cap_name, cap in list(CAPABILITY_REGISTRY.items()):
        if cap.get("source") == "mcp_proxy":
            continue  # proxy caps forward to remote — no local REST needed

        auto_path = "/" + cap_name.replace(".", "/")
        if any((m, auto_path) in claimed_paths for m in ("GET", "POST", "PUT", "DELETE")):
            continue  # already explicitly mounted

        handler = _make_post_handler(cap, cap_name)
        try:
            app.add_api_route(
                auto_path, handler, methods=["POST"],
                tags=cap.get("tags") or [cap_name.split(".")[0]],
                summary=(cap.get("description") or cap_name)[:120],
            )
        except Exception as e:
            log.warning("Auto-route failed %s: %s", cap_name, e)

    log.info("HTTP routes mounted: %d explicit, %d auto-POST",
             len(claimed_paths),
             sum(1 for c in CAPABILITY_REGISTRY.values()
                 if not c.get("http_path") and c.get("source") != "mcp_proxy"))


APP = FastAPI(title="Vera Orchestrator", version="3.0", lifespan=lifespan)
APP.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@APP.get("/memgraph/panel", include_in_schema=False)
async def _memgraph_panel_route():
    from fastapi.responses import HTMLResponse
    from pathlib import Path as _P
    p = _P(__file__).parent / "fabric/memory_graph_panel.html"
    return HTMLResponse(
        p.read_text(encoding="utf-8") if p.exists()
        else "<p style='color:red'>memory_graph_panel.html not found</p>"
    )

try:
    register_ui(
        "memory-graph", "Memory Graph", "",
        """<div style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/memgraph/panel"
          style="flex:1;border:none;width:100%;height:100%"
          allow="clipboard-read; clipboard-write">
  </iframe>
  </div>""",
        "", ui_caps=["memory.graph_full","memory.session_nodes","memory.session_edges",
                     "memory.all_nodes","memory.all_edges","memory.traverse",
                     "memory.label","memory.label_session","cap_tracking.set_session"],
        mode="tab", tab_order=72,
    )
except Exception as _mge:
    log.warning("memgraph register_ui: %s", _mge)


# ── Global exception capture → syslog WS feed ─────────────────────────────────
import traceback as _traceback
import logging   as _logging

class _VeraWsLogHandler(_logging.Handler):
    """
    Routes Python ERROR/CRITICAL log records to the WS syslog feed.
    Installed once at startup so uvicorn, httpx, asyncio errors all show up.
    """
    def emit(self, record: _logging.LogRecord):
        if record.levelno < _logging.ERROR:
            return
        try:
            msg = self.format(record)
            exc = ""
            if record.exc_info:
                exc = "".join(_traceback.format_exception(*record.exc_info))
            payload = {
                "type":    "syslog.error",
                "level":   record.levelname,
                "logger":  record.name,
                "message": msg,
                "exc":     exc[-2000:] if exc else "",
                "file":    f"{record.pathname}:{record.lineno}",
            }
            import asyncio as _aio
            loop = None
            try:
                loop = _aio.get_running_loop()
            except RuntimeError:
                pass
            if loop and loop.is_running():
                loop.create_task(emit_event(payload))
        except Exception:
            pass  # never let the handler crash the app

_ws_log_handler = _VeraWsLogHandler()
_ws_log_handler.setLevel(_logging.ERROR)
_ws_log_handler.setFormatter(_logging.Formatter("%(name)s: %(message)s"))
# Install on root logger — catches everything (uvicorn, httpx, vera.*)
_logging.getLogger().addHandler(_ws_log_handler)

@APP.exception_handler(Exception)
async def _global_exception_handler(request, exc):
    """Catch unhandled route exceptions and emit to syslog before re-raising."""
    from fastapi.responses import JSONResponse
    # Never intercept WebSocket upgrade requests — returning JSONResponse here
    # sends HTTP 500 instead of 101, which silently breaks the WS handshake
    if request.headers.get("upgrade", "").lower() == "websocket":
        raise exc
    tb = _traceback.format_exc()
    await emit_event({
        "type":    "syslog.error",
        "level":   "CRITICAL",
        "logger":  "vera.asgi",
        "message": f"Unhandled exception in {request.url.path}: {exc}",
        "exc":     tb[-2000:],
        "file":    "",
    })
    return JSONResponse({"error": str(exc)}, status_code=500)

def _asyncio_exception_handler(loop, context):
    """Catch unhandled asyncio task exceptions and emit to syslog."""
    exc  = context.get("exception")
    msg  = context.get("message", "unknown")
    tb   = "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else ""
    loop.create_task(emit_event({
        "type":    "syslog.error",
        "level":   "ERROR",
        "logger":  "vera.asyncio",
        "message": f"Async task error: {msg}",
        "exc":     tb[-2000:],
        "file":    "",
    }))


# ── DAG HITL streaming ───────────────────────────────────────────────────────

_HITL_PENDING: dict = {}

@APP.post("/dag/hitl/respond")
async def dag_hitl_respond(request: Request):
    """Receive human approval/rejection for a paused HITL DAG step."""
    import json as _json
    try:    body = await request.json()
    except: body = {}
    trace_id     = body.get("trace_id", "")
    action       = body.get("action", "approve")
    edited_raw   = body.get("edited_params", "{}")
    try:    ep = _json.loads(edited_raw) if isinstance(edited_raw, str) else (edited_raw or {})
    except: ep = {}
    fut = _HITL_PENDING.get(trace_id)
    if not fut or fut.done():
        return {"error": f"No pending HITL for trace_id={trace_id}"}
    fut.set_result({"action": action, "edited_params": ep})
    return {"status": "received", "action": action, "trace_id": trace_id}


async def _hitl_run_graph_stream(graph, state, hitl, auto_approve_secs):
    import json as _json, uuid as _uuid
    for i, node in enumerate(graph):
        is_parallel = isinstance(node, list) and isinstance(node[0], list)
        cap_name = None if is_parallel else (node[0] if isinstance(node, list) else node)
        out_key  = None if is_parallel else (node[1] if isinstance(node, list) and len(node)>1 else None)

        yield "dag.step_start", {"step":i,"total":len(graph),
                                  "cap":cap_name or "[parallel]","out_key":out_key}

        if hitl and not is_parallel:
            step_trace = str(_uuid.uuid4())
            cap_obj = CAPABILITY_REGISTRY.get(cap_name, {})
            accepted = set(cap_obj.get("schema",{}).get("properties",{}).keys())
            step_params = {k:v for k,v in state.items() if k in accepted}
            fut = asyncio.get_event_loop().create_future()
            _HITL_PENDING[step_trace] = fut
            yield "dag.hitl_request", {"step":i,"cap":cap_name,"out_key":out_key,
                                        "params":step_params,"trace_id":step_trace,
                                        "auto_approve_secs":auto_approve_secs}
            try:    decision = await asyncio.wait_for(fut, timeout=float(auto_approve_secs))
            except asyncio.TimeoutError: decision = {"action":"approve","edited_params":{}}
            finally: _HITL_PENDING.pop(step_trace, None)

            if decision["action"] == "reject":
                yield "dag.hitl_rejected", {"step":i,"cap":cap_name}
                yield "dag.complete", {"state":state,"aborted_at":i,"reason":"user rejected"}
                return
            if decision["action"] == "edit" and decision.get("edited_params"):
                state.update(decision["edited_params"])

        try:
            if is_parallel:
                results = await asyncio.gather(*[run_graph([n],dict(state)) for n in node],
                                               return_exceptions=True)
                for r in results:
                    if isinstance(r, dict): state.update(r)
                yield "dag.step_done", {"step":i,"parallel":True}
            else:
                cap_obj = CAPABILITY_REGISTRY.get(cap_name, {})
                if not cap_obj:
                    if out_key: state[out_key] = {"error":"unknown cap"}
                    yield "dag.step_error", {"step":i,"cap":cap_name,"error":"unknown cap"}
                else:
                    accepted = set(cap_obj["schema"].get("properties",{}).keys())
                    result = await cap_obj["func"](**{k:v for k,v in state.items() if k in accepted})
                    if out_key: state[out_key] = result
                    yield "dag.step_done", {"step":i,"cap":cap_name,"out_key":out_key,
                                             "result_preview":str(result)[:200] if result else None}
        except Exception as e:
            if out_key: state[out_key] = {"error":str(e)}
            yield "dag.step_error", {"step":i,"cap":cap_name or "[parallel]","error":str(e)}

    yield "dag.complete", {"state":state}
    # Record to memory graph
    try:
        import sys as _sys
        _mh = _sys.modules.get('memory_hooks')
        if _mh and hasattr(_mh, 'record_dag_execution'):
            import asyncio as _aio
            _aio.create_task(_mh.record_dag_execution(
                session_id=state.get('__session_id__', ''),
                dag=graph, state=state, result=state,
                agent_name=state.get('__agent_name__', ''),
                trigger='chat_dag',
            ))
    except Exception:
        pass


@APP.post("/dag/plan_stream")
async def dag_plan_stream_endpoint(request: Request):
    """
    SSE endpoint for DAG planning and execution.

    Body fields:
      goal              : str  — natural language goal
      mode              : str  — "oneshot" | "stepwise"
                          oneshot  = plan entire DAG then execute it
                          stepwise = plan one cap at a time, execute, observe, repeat
      execute           : bool — run after planning (default true)
      hitl              : bool — pause for human approval before each step
      auto_approve_secs : int  — seconds before auto-approve (default 30)
      state             : dict — seed state (merged with plan's initial_state)
      session_id        : str  — caller's session id; required for activity
                                 recording. Without it, the call still runs
                                 but does not appear in syslog as a cap.call.

    Activity recording
    ──────────────────
    This is a raw FastAPI route — it doesn't go through the @capability
    wrapper. We call record_stream_activity() in the finally block of the
    inner async generator so the stream appears as a cap.call/cap.ok pair
    in syslog and in the FOLLOWS_ACTIVITY chain like any other cap.
    Internal cap calls executed by the planner (via run_graph etc.) emit
    their own cap.call/cap.ok independently — they're TRIGGERED_BY this one.
    """
    import json as _json
    try:    body = await request.json()
    except: body = {}
    goal              = body.get("goal", "")
    mode              = body.get("mode", "oneshot")   # "oneshot" | "stepwise"
    do_execute        = bool(body.get("execute", True))
    hitl              = bool(body.get("hitl", True))
    auto_approve_secs = int(body.get("auto_approve_secs", 30))
    seed_state        = dict(body.get("state") or {})
    session_id        = body.get("session_id", "") or ""

    async def _gen():
        import time as _time
        _t0_stream = _time.monotonic()
        # Counters / accumulators for the recorded activity entry
        plan_dag_arr  = []
        plan_rationale = ""
        plan_error     = ""
        steps_emitted  = 0
        last_state_keys: list = []

        def _sse(t, d):
            return f"data: {_json.dumps({'type':t,**d})}\n\n".encode()

        try:
            if not goal:
                plan_error = "No goal provided"
                yield _sse("dag.error", {"error": plan_error}); return

            # ── STEPWISE MODE ─────────────────────────────────────────────────
            # The LLM plans one capability at a time, executes it, observes the
            # result, then decides what to do next.
            if mode == "stepwise":
                async for ev_type, ev_data in _stepwise_run(
                        goal, seed_state, hitl, auto_approve_secs):
                    if ev_type == "step.complete":
                        steps_emitted += 1
                    elif ev_type == "dag.error":
                        plan_error = (ev_data or {}).get("error", "")
                    yield _sse(ev_type, ev_data)
                yield b"data: [DONE]\n\n"
                return

            # ── ONESHOT MODE ──────────────────────────────────────────────────
            yield _sse("dag.planning", {"goal": goal})
            try:
                plan = await plan_dag(goal)
            except Exception as e:
                plan_error = str(e)
                yield _sse("dag.error", {"error": plan_error}); return

            if plan.get("error") and not plan.get("dag"):
                plan_error = plan["error"]
                yield _sse("dag.error", {"error": plan_error}); return

            dag_arr      = plan.get("dag", [])
            plan_dag_arr = dag_arr
            plan_rationale = plan.get("rationale", "")
            # CRITICAL: merge the plan's initial_state with any seed state from caller
            plan_state   = dict(plan.get("initial_state") or {})
            plan_state.update(seed_state)          # caller seed takes precedence

            yield _sse("dag.plan_ready", {
                "dag":          dag_arr,
                "initial_state": plan_state,
                "rationale":    plan_rationale,
                "steps":        len(dag_arr),
                "execute":      do_execute,
                "hitl":         hitl,
            })

            if not do_execute:
                yield _sse("dag.done", {"dag": dag_arr}); return

            async for ev_type, ev_data in _hitl_run_graph_stream(
                    dag_arr, plan_state, hitl, auto_approve_secs):
                if ev_type == "step.complete":
                    steps_emitted += 1
                yield _sse(ev_type, ev_data)
                if isinstance(ev_data, dict) and "state" in ev_data:
                    last_state_keys = list((ev_data.get("state") or {}).keys())[:20]

            yield b"data: [DONE]\n\n"
        finally:
            elapsed_ms = round((_time.monotonic() - _t0_stream) * 1000)
            try:
                await record_stream_activity(
                    cap_name="dag.plan.stream", session_id=session_id,
                    params={
                        "goal":              goal,
                        "mode":              mode,
                        "execute":           do_execute,
                        "hitl":              hitl,
                        "auto_approve_secs": auto_approve_secs,
                        "seed_state_keys":   list(seed_state.keys())[:20],
                    },
                    result={
                        "dag":             plan_dag_arr,
                        "dag_steps":       len(plan_dag_arr),
                        "steps_emitted":   steps_emitted,
                        "rationale":       plan_rationale[:600],
                        "error":           plan_error or None,
                        "final_state_keys": last_state_keys,
                        "elapsed_ms":      elapsed_ms,
                    },
                    elapsed_ms=elapsed_ms,
                    group="dag",
                )
            except Exception as _e:
                log.debug("record_stream_activity dag.plan.stream: %s", _e)

    return StreamingResponse(_gen(), media_type="text/event-stream",
                              headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


async def _stepwise_run(goal: str, state: dict, hitl: bool, auto_approve_secs: int):
    """
    Agentic step-by-step execution loop.
    Each iteration:
      1. LLM decides what single capability to call next (given goal + state)
      2. User approves (if HITL)
      3. Capability executes
      4. Result added to state
      5. LLM decides whether to continue or stop
    """
    import json as _json, uuid as _uuid

    cap_keys = list(CAPABILITY_REGISTRY.keys())

    def _cap_sig(k):
        cap  = CAPABILITY_REGISTRY.get(k, {})
        props = cap.get("schema", {}).get("properties", {})
        req  = set(cap.get("schema", {}).get("required", []))
        params = ", ".join(
            f"{p}:{v.get('type','str')}{'!' if p in req else ''}"
            for p, v in props.items() if p not in ("trace_id",)
        )
        return f"  {k}({params})"

    cap_desc = "\n".join(_cap_sig(k) for k in cap_keys)

    SYSTEM = (
        "You are a Vera agent executing a goal step by step. "
        "At each step you output a JSON object with one of two shapes:\n"
        '  NEXT STEP:  {"action":"call","cap":"capability_name","params":{"key":"value"},"out_key":"result_key","reason":"why"}\n'
        '  FINISHED:   {"action":"done","summary":"what was accomplished"}\n'
        "Rules:\n"
        "- Only use capability names from the provided list.\n"
        "- params must match the capability signature exactly.\n"
        "- out_key names the state key where the result will be stored.\n"
        "- Output a SINGLE JSON object, no markdown, no explanation outside the JSON.\n"
    )

    step = 0
    MAX_STEPS = 12
    history = []  # list of {cap, result_summary} for context

    while step < MAX_STEPS:
        # Build context for the LLM
        state_summary = {k: str(v)[:200] for k, v in state.items()}
        hist_text = "\n".join(
            f"Step {i+1}: called {h['cap']} → {h['result'][:100]}"
            for i, h in enumerate(history)
        )
        prompt = (
            f"Goal: {goal}\n\n"
            f"Steps taken so far:\n{hist_text or 'None yet'}\n\n"
            f"Current state keys: {list(state.keys())}\n\n"
            f"Available capabilities:\n{cap_desc}\n\n"
            "What is the next single step to take? Output JSON only."
        )

        yield "dag.step_planning", {"step": step, "goal": goal}

        try:
            raw = await ollama_generate(prompt, system=SYSTEM, prefer_gpu=True)
        except Exception as e:
            yield "dag.error", {"error": f"LLM step planning failed: {e}"}
            return

        # Parse JSON response
        import re as _re
        decision = None
        for attempt in [raw, _re.search(r'\{[\s\S]*\}', raw or "")]:
            txt = attempt if isinstance(attempt, str) else (attempt.group() if attempt else "")
            try:
                d = json.loads(txt.strip())
                if isinstance(d, dict) and d.get("action") in ("call", "done"):
                    decision = d; break
            except Exception:
                pass

        if not decision:
            yield "dag.error", {"error": f"Could not parse step decision from LLM: {(raw or '')[:200]}"}
            return

        if decision["action"] == "done":
            yield "dag.complete", {
                "state": state,
                "summary": decision.get("summary", "Goal completed"),
                "steps_taken": step,
            }
            return

        # action == "call"
        cap_name = decision.get("cap", "")
        params   = dict(decision.get("params") or {})
        out_key  = decision.get("out_key", f"result_{step}")
        reason   = decision.get("reason", "")

        # Merge params into state so cap can find them
        run_state = dict(state)
        run_state.update(params)

        yield "dag.step_start", {
            "step": step, "cap": cap_name, "out_key": out_key,
            "params": params, "reason": reason,
        }

        if hitl:
            step_trace = str(_uuid.uuid4())
            fut = asyncio.get_event_loop().create_future()
            _HITL_PENDING[step_trace] = fut
            yield "dag.hitl_request", {
                "step": step, "cap": cap_name, "out_key": out_key,
                "params": params, "trace_id": step_trace,
                "auto_approve_secs": auto_approve_secs,
                "reason": reason,
            }
            try:
                dec = await asyncio.wait_for(fut, timeout=float(auto_approve_secs))
            except asyncio.TimeoutError:
                dec = {"action": "approve", "edited_params": {}}
            finally:
                _HITL_PENDING.pop(step_trace, None)

            if dec["action"] == "reject":
                yield "dag.hitl_rejected", {"step": step, "cap": cap_name}
                yield "dag.complete", {"state": state, "aborted_at": step, "reason": "user rejected"}
                return
            if dec["action"] == "edit" and dec.get("edited_params"):
                run_state.update(dec["edited_params"])
                params.update(dec["edited_params"])

        # Execute the capability
        cap_obj = CAPABILITY_REGISTRY.get(cap_name)
        if not cap_obj:
            yield "dag.step_error", {"step": step, "cap": cap_name, "error": "unknown capability"}
            state[out_key] = {"error": "unknown capability"}
        else:
            try:
                accepted = set(cap_obj["schema"].get("properties", {}).keys())
                result   = await cap_obj["func"](**{k: v for k, v in run_state.items() if k in accepted})
                state[out_key] = result
                result_preview = str(result)[:300] if result is not None else "null"
                history.append({"cap": cap_name, "result": result_preview})
                yield "dag.step_done", {
                    "step": step, "cap": cap_name, "out_key": out_key,
                    "result_preview": result_preview,
                }
            except Exception as e:
                err = str(e)
                state[out_key] = {"error": err}
                history.append({"cap": cap_name, "result": f"ERROR: {err}"})
                yield "dag.step_error", {"step": step, "cap": cap_name, "error": err}

        step += 1

    # Hit MAX_STEPS
    yield "dag.complete", {
        "state": state,
        "summary": f"Reached maximum {MAX_STEPS} steps",
        "steps_taken": step,
    }


@APP.get("/", include_in_schema=False)
async def _home():
    from fastapi.responses import HTMLResponse
    p = _HERE / "capability_orchestration.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>capability_orchestration.html not found</p>")


@APP.get("/ui/panels/agents-skills-ontologies", include_in_schema=False)
async def _aso_panel():
    """Combined agents/skills/ontologies shell panel."""
    from fastapi.responses import HTMLResponse
    p = _HERE / "agents_skills_ontologies_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>agents_skills_ontologies_panel.html not found</p>")


@APP.get("/ui/panels/workers-ollama", include_in_schema=False)
async def _workers_ollama_panel():
    """Combined workers / ollama / jobs panel with configurable dashboard."""
    from fastapi.responses import HTMLResponse
    p = _HERE / "workers/workers_ollama_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>workers_ollama_panel.html not found</p>")


# Generic catch-all panel server. Lets the chat panel embed any registered
# UI panel by id (e.g. /ui/panels/file/dag_workshop_panel.html) without
# every panel needing its own hand-written endpoint. Strict suffix check
# blocks path traversal and arbitrary file reads — only files matching
# *_panel.html in the project root are served.
@APP.get("/ui/panels/file/{panel_filename:path}", include_in_schema=False)
async def _serve_panel_file(panel_filename: str):
    from fastapi.responses import HTMLResponse
    # Whitelist: only serve sibling files matching *_panel.html, no traversal
    if (".." in panel_filename or "/" in panel_filename or "\\" in panel_filename
        or not panel_filename.endswith("_panel.html")):
        return HTMLResponse("<p style='color:red'>Bad panel filename.</p>", status_code=400)
    p = _HERE / panel_filename
    if not p.exists():
        return HTMLResponse(f"<p style='color:red'>{panel_filename} not found</p>",
                            status_code=404)
    return HTMLResponse(p.read_text(encoding="utf-8"))


@APP.get("/ui/panels/agents-panel", include_in_schema=False)
async def _agents_panel():
    """Standalone agents editor panel."""
    from fastapi.responses import HTMLResponse
    p = _HERE / "agents/agent_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>agent_panel.html not found</p>")


@APP.get("/ui/panels/skills-panel", include_in_schema=False)
async def _skills_panel():
    """Standalone skills editor panel."""
    from fastapi.responses import HTMLResponse
    p = _HERE / "skills/skills_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>skills_panel.html not found</p>")


@APP.get("/ui/panels/ontologies-panel", include_in_schema=False)
async def _ontologies_panel():
    """Standalone ontologies browser panel."""
    from fastapi.responses import HTMLResponse
    p = _HERE / "ontologies/ontologies_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>ontologies_panel.html not found</p>")


# @APP.get("/worldview/panel", include_in_schema=False)
# async def _worldview():
#     from fastapi.responses import HTMLResponse
#     p = _HERE / "worldview.html"
#     return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
#         else "<p style='color:red'>worldview_panel.html not found</p>")

# register_ui(
#     "worldview-ui",
#     "worldview",
#     "",
#     """<div id="worldview-panel-mount" style="height:100%;display:flex;flex-direction:column;">
#   <iframe src="/worldview/panel"
#           style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
#           allow="clipboard-read; clipboard-write">
#   </iframe>
# </div>""",
#  "",
#     ui_caps=[

#     ],
#     mode="tab",
#     tab_order=78,
# )

# @APP.get("/chat/panel", include_in_schema=False)
# async def _chat():
#     from fastapi.responses import HTMLResponse
#     p = _HERE / "chat_panel.html"
#     return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
#                         else "<p style='color:red'>chat_panel.html not found</p>")

# register_ui(
#     "chat-panel",
#     "Chat 2",
#     "",
#     """<div id="chat-panel-mount" style="height:100%;display:flex;flex-direction:column;">
#   <iframe src="/chat/panel"
#           style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
#           allow="clipboard-read; clipboard-write">
#   </iframe>
# </div>""",
#  "",
#     ui_caps=[
#         "dream.scheduler.start", "dream.scheduler.stop", "dream.scheduler.status",
#         "dream.cycle.run", "dream.cycle.cancel",
#         "dream.preview", "dream.preview.last",
#         "dream.trigger.list", "dream.trigger.get", "dream.trigger.upsert",
#         "dream.trigger.delete", "dream.trigger.toggle", "dream.trigger.generate",
#         "dream.whitelist.list", "dream.whitelist.set",
#         "dream.config.get", "dream.config.set",
#         "dream.history", "dream.last",
#         "dream.hitl.pending", "dream.hitl.respond",
#         "dream.llm.tokens",
#         "dream.sensors.list", "dream.stages.list",
#         "dream.director.assess", "dream.timeline",
#         "dream.stage.stepwise_execute",
#     ],
#     mode="tab",
#     tab_order=78,
# )


# Note: _disconnect_guard middleware was removed because starlette's
# `call_next()` in @app.middleware("http") consumes the entire response
# body before returning — this silently kills all streaming responses.
# Disconnect errors are handled per-generator instead (try/except in each
# streaming endpoint's async generator).

# ── WebSocket MCP (stays as raw WebSocket — cannot be a @capability) ──────────

@APP.websocket("/ws/mcp")
async def ws_mcp(ws: WebSocket):
    await ws.accept(); client_id=new_id()[:8]
    try:
        await ws.send_json({
            "type":"connected","client_id":client_id,
            "capabilities":list(CAPABILITY_REGISTRY.keys()),
            "servers":list(MCP_SERVERS.keys()),
            "ollama_instances":{iid:{"label":i["label"],"has_gpu":i["has_gpu"],"status":i["status"]}
                                for iid,i in OLLAMA_INSTANCES.items()},
            "mode":"distributed" if REDIS else "local",
        })
    except Exception:
        return  # client disconnected before we could greet them
    try:
        while True:
            msg=await ws.receive_json(); action=msg.get("action")

            if action=="call":
                cap_name=msg.get("name"); cap=CAPABILITY_REGISTRY.get(cap_name)
                tid=msg.get("trace_id") or new_id()
                # Keep _CURRENT_SESSION up to date so cap activity recording works
                global _CURRENT_SESSION
                _ws_sid = (msg.get("arguments") or {}).get("session_id","") if isinstance(msg.get("arguments"),dict) else ""
                if _ws_sid:
                    _CURRENT_SESSION = _ws_sid
                if not cap:
                    await ws.send_json({"type":"error","trace_id":tid,"message":f"Unknown: {cap_name}"}); continue
                # Snapshot args now — avoid capturing loop variable in closure
                _args = dict(msg.get("arguments") or {})
                async def _call(_cap=cap, _name=cap_name, _tid=tid, _args=_args, _ws=ws):
                    try:
                        result = await _cap["func"](**_args, trace_id=_tid)
                        safe   = _json_safe(result)
                        await _ws.send_json({"type":"tool_result","tool_name":_name,"trace_id":_tid,"content":safe})
                    except WebSocketDisconnect:
                        pass
                    except RuntimeError as e:
                        if "closed" in str(e).lower() or "transport" in str(e).lower():
                            log.debug("WS send failed (client gone) for %s", _name)
                        else:
                            log.error("WS cap %s RuntimeError: %s", _name, e)
                    except Exception as e:
                        try:
                            await _ws.send_json({"type":"error","tool_name":_name,"trace_id":_tid,"message":str(e)})
                        except Exception:
                            pass
                asyncio.create_task(_call())

            elif action=="subscribe":
                WS_CONNECTIONS.append((ws,msg.get("stream")))
                await ws.send_json({"type":"subscribed","stream":msg.get("stream")})
            elif action=="subscribe_events":
                WS_CONNECTIONS.append((ws,"__events__"))
                await ws.send_json({"type":"subscribed","stream":"__events__"})
            elif action=="unsubscribe":
                _remove_ws(ws,msg.get("stream"))
                await ws.send_json({"type":"unsubscribed","stream":msg.get("stream")})

            elif action=="dag_run":
                tid=new_id(); graph=list(msg.get("dag",[])); state=dict(msg.get("state",{}))
                sup=bool(msg.get("supervised",False))
                async def _dag(_g=graph,_s=state,_tid=tid,_sup=sup,_ws=ws):
                    try:
                        fn=supervised_run_graph if _sup else run_graph
                        result=await fn(_g,_s)
                        await _ws.send_json({"type":"dag_result","trace_id":_tid,"result":_json_safe(result)})
                    except (WebSocketDisconnect,RuntimeError): pass
                    except Exception as e: log.error("WS dag_run: %s",e)
                asyncio.create_task(_dag())

            elif action=="plan_and_run":
                goal=str(msg.get("goal","")); tid=new_id()
                async def _par(_goal=goal,_tid=tid,_ws=ws):
                    async def _send(obj):
                        try: await _ws.send_json(obj)
                        except (WebSocketDisconnect,RuntimeError): pass
                    await _send({"type":"planning","trace_id":_tid,"goal":_goal})
                    plan=await plan_dag(_goal)
                    await _send({"type":"plan_ready","trace_id":_tid,"plan":_json_safe(plan)})
                    result=await supervised_run_graph(plan.get("dag",[]),plan.get("initial_state",{}))
                    await _send({"type":"dag_result","trace_id":_tid,"result":_json_safe(result)})
                asyncio.create_task(_par())

            elif action=="register_server":
                url=msg.get("url")
                if url:
                    registered=await register_mcp_server(url,msg.get("name") or url)
                    await ws.send_json({"type":"server_registered","name":msg.get("name"),"capabilities":registered})

            elif action=="ollama_instances":
                await ws.send_json({"type":"ollama_instances","instances":OLLAMA_INSTANCES})

            elif action=="ping":
                await ws.send_json({"type":"pong","ts":now_iso()})

    except WebSocketDisconnect: log.info("WS disconnected: %s",client_id)
    except Exception as e: log.error("WS error [%s]: %s",client_id,e)
    finally:
        WS_CONNECTIONS[:]=[p for p in WS_CONNECTIONS if p[0] is not ws]


if __name__=="__main__":
    uvicorn.run("Vera.Orchestration.capability_orchestration:APP",host="0.0.0.0",port=8999,reload=False)