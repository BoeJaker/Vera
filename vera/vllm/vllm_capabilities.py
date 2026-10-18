"""
vllm_capabilities.py  —  vLLM Backend Integration for Vera
===========================================================
Mirrors the Ollama backend pattern but targets vLLM's OpenAI-compatible
server, unlocking features that matter on a home lab:

  PagedAttention        — vLLM's KV-cache allocator; eliminates memory
                          fragmentation and enables concurrent requests with
                          radically better GPU utilisation.

  Continuous Batching   — requests are batched at the iteration level, not the
                          request level; new prompts join in-flight batches as
                          tokens free up, maximising throughput.

  Speculative Decoding  — a small draft model runs ahead; the target model
                          verifies multiple tokens per step, cutting wall-clock
                          latency without sacrificing quality.  Enabled with
                          VLLM_SPEC_MODEL.

  Prefix Caching        — repeated prompt prefixes (system prompts, few-shot
                          examples) share KV blocks automatically via
                          automatic prefix caching (APC).  Dramatically reduces
                          TTFT for repeated templates.

  Chunked Prefill       — long prompts are chunked so prefill doesn't starve
                          decode iterations; keeps latency flat on large
                          contexts.  Enabled by default ≥ vLLM 0.4.

  TurboQuant / GPTQ /
  AWQ / FP8 / GGUF      — quantised checkpoints load via --quantization flag.
                          Vera passes this through; set VLLM_QUANTIZATION.

  Multi-GPU tensor       — set VLLM_TENSOR_PARALLEL_SIZE > 1 to shard across
  parallelism              multiple GPUs transparently.

  CPU Offload            — set VLLM_GPU_MEMORY_UTILIZATION < 1 and
                           VLLM_CPU_OFFLOAD_GB to spill KV cache to host RAM.
                           Useful on 12 GB GPUs with 256 GB host RAM.

  LoRA hot-swap          — load multiple LoRA adapters at runtime and select
                           per-request with `lora_request`.

  Logprobs / guided      — full logprob access and JSON-schema / regex / grammar
  decoding                 guided generation via structured output modes.

  Embedding mode         — expose embedding endpoints alongside generation.

Architecture
────────────
  VLLMInstance          — dataclass tracking one vLLM server endpoint
  VLLMRegistry          — collection of instances with health-check + routing
  vllm_generate()       — drop-in companion to ollama_generate()
  vllm_chat()           — OpenAI-compatible /v1/chat/completions
  vllm_embed()          — /v1/embeddings

Capabilities registered
────────────────────────
  vllm.status           — cluster health summary (every configured instance)
  vllm.instances.add    — add a new instance at runtime (persisted in Redis)
  vllm.instances.remove — remove an instance (persisted in Redis)
  vllm.models           — list models served by all instances
  vllm.generate         — raw /v1/completions call
  vllm.chat             — /v1/chat/completions
  vllm.embed            — /v1/embeddings
  vllm.lora.load        — load a LoRA adapter onto an instance
  vllm.lora.list        — list loaded LoRA adapters
  vllm.metrics          — Prometheus /metrics scrape (parsed)
  vllm.server.start     — launch a vLLM server subprocess (optional)
  vllm.server.stop      — stop a managed subprocess

Configuration (env vars)
─────────────────────────
  VLLM_INSTANCES        JSON list: [{"id":"gpu0","url":"http://host:8001","has_gpu":true}]
                        OR single URL: "http://192.168.0.250:8001"
                        Runtime add/remove is stored in Redis (vera:vllm:instances)
                        and merged over this list at boot (vllm_registry_core).
  VLLM_MODEL            default model name
  VLLM_API_KEY          optional API key (vLLM supports --api-key)
  VLLM_QUANTIZATION     gptq | awq | fp8 | gguf | turbomind (passed at launch)
  VLLM_TENSOR_PARALLEL  tensor parallel size (default 1)
  VLLM_GPU_MEM_UTIL     fraction of GPU VRAM to use (default 0.90)
  VLLM_CPU_OFFLOAD_GB   GiB of host RAM for KV offload (default 0)
  VLLM_SPEC_MODEL       speculative decoding draft model name or path
  VLLM_MAX_MODEL_LEN    override context window (tokens)
  VLLM_ENABLE_LORA      "1" to enable LoRA support (default "0")
  VLLM_MAX_LORAS        max concurrent LoRA adapters (default 4)
  VLLM_DTYPE            bfloat16 | float16 | float32 | auto (default auto)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import httpx

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP,
    capability,
    emit_event,
    now_iso,
    register_ui,
    schedule,
)
from Vera.vera.config import cfg

try:
    from Vera.vera.vllm import vllm_registry_core as _reg_core
    from Vera.vera.vllm import vllm_route_core as _route_core
    from Vera.vera.vllm import node_backend_core as _backend_core
    from Vera.vera.vllm import vllm_install_core as _install_core
except Exception:                                    # worktree / app-free import
    from vera.vllm import vllm_registry_core as _reg_core
    from vera.vllm import vllm_route_core as _route_core
    from vera.vllm import node_backend_core as _backend_core
    from vera.vllm import vllm_install_core as _install_core

log = logging.getLogger("vera.vllm")

# ══════════════════════════════════════════════════════════════════════════════
# Instance model
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class VLLMInstance:
    id: str
    url: str                              # http://host:port  (no trailing slash)
    label: str = ""
    has_gpu: bool = True
    priority: int = 0
    api_key: str = ""                     # optional bearer token
    # Ollama node id this server shares hardware with (e.g. vLLM on the V100 of
    # gpu-250): its calls take THAT node's semaphore + cross-process gate, so a
    # vLLM and an Ollama generation can never overlap on one device.
    gate_as: str = ""
    # thinking for requests that do not choose: "" model default | "on" | "off"
    think_default: str = ""
    # model names routed here though this server runs another model (stand-ins)
    stands_in_for: List[str] = field(default_factory=list)
    # runtime state
    status: str = "unknown"              # unknown | online | offline | degraded
    latency_ms: Optional[float] = None
    in_use: int = 0
    errors: int = 0
    last_check: Optional[str] = None
    models: List[str] = field(default_factory=list)
    # vLLM-specific metrics (populated from /metrics)
    running_requests: int = 0
    waiting_requests: int = 0
    gpu_cache_usage: float = 0.0         # 0.0 – 1.0
    cpu_cache_usage: float = 0.0
    tokens_per_second: float = 0.0
    # LoRA adapters loaded on this instance
    loaded_loras: Dict[str, int] = field(default_factory=dict)  # name → lora_int_id
    # managed subprocess PID (if launched by Vera)
    pid: Optional[int] = None

    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h


# ══════════════════════════════════════════════════════════════════════════════
# Registry
# ══════════════════════════════════════════════════════════════════════════════

VLLM_INSTANCES: Dict[str, VLLMInstance] = {}
_LORA_ID_COUNTER = 1

# ── Default config ─────────────────────────────────────────────────────────────
VLLM_MODEL          = os.environ.get("VLLM_MODEL", "")
VLLM_API_KEY        = os.environ.get("VLLM_API_KEY", "")
# How long a request waits on the server: between bytes when streaming, for the
# whole reply when not. A CPU node decodes about a token a second and compiles on
# its first request - a fixed 180 s cut ordinary replies off (5 Oct 2026).
VLLM_READ_TIMEOUT_S = float(os.environ.get("VLLM_READ_TIMEOUT_S", "1800") or 1800)


def _req_timeout() -> "httpx.Timeout":
    return httpx.Timeout(VLLM_READ_TIMEOUT_S, connect=10.0)


def _err(exc: BaseException) -> str:
    """An exception as text - a ReadTimeout's own message is empty."""
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
VLLM_QUANTIZATION   = os.environ.get("VLLM_QUANTIZATION", "")
VLLM_TENSOR_PAR     = int(os.environ.get("VLLM_TENSOR_PARALLEL", "1"))
VLLM_GPU_MEM_UTIL   = float(os.environ.get("VLLM_GPU_MEM_UTIL", "0.90"))
VLLM_CPU_OFFLOAD_GB = float(os.environ.get("VLLM_CPU_OFFLOAD_GB", "0"))
VLLM_SPEC_MODEL     = os.environ.get("VLLM_SPEC_MODEL", "")
VLLM_MAX_MODEL_LEN  = int(os.environ.get("VLLM_MAX_MODEL_LEN", "0")) or None
VLLM_ENABLE_LORA    = os.environ.get("VLLM_ENABLE_LORA", "0") == "1"
VLLM_MAX_LORAS      = int(os.environ.get("VLLM_MAX_LORAS", "4"))
VLLM_DTYPE          = os.environ.get("VLLM_DTYPE", "auto")


def _parse_instances_env() -> List[dict]:
    raw = os.environ.get("VLLM_INSTANCES", "")
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("http"):
        return [{"id": "vllm-0", "url": raw.rstrip("/"), "has_gpu": True, "label": "vLLM default"}]
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else [parsed]
    except Exception as e:
        log.warning("vllm: VLLM_INSTANCES parse error: %s", e)
        return []


def _init_instances():
    for spec in _parse_instances_env():
        iid = spec.get("id", f"vllm-{len(VLLM_INSTANCES)}")
        inst = VLLMInstance(
            id=iid,
            url=spec.get("url", "").rstrip("/"),
            label=spec.get("label", iid),
            has_gpu=spec.get("has_gpu", True),
            priority=spec.get("priority", len(VLLM_INSTANCES)),
            api_key=spec.get("api_key", VLLM_API_KEY),
            gate_as=str(spec.get("gate_as") or ""),
        )
        VLLM_INSTANCES[iid] = inst
        log.info("vllm: registered instance %s → %s", iid, inst.url)

_init_instances()

KEY_VLLM_INSTANCES = "vera:vllm:instances"
_REG_DOC: Dict[str, Any] = _reg_core.empty_doc()
_REG_LOADED = False
_REG_LOCK: Optional[asyncio.Lock] = None


async def _ensure_registry_loaded() -> None:
    """Load the stored registry once. add/remove call this first, so an add made
    while the boot load is still waiting for Redis cannot overwrite the stored
    document with a one-entry copy."""
    global _REG_LOCK
    if _REG_LOADED:
        return
    if _REG_LOCK is None:
        _REG_LOCK = asyncio.Lock()
    async with _REG_LOCK:
        if not _REG_LOADED:
            await _load_registry()


def _env_ids() -> List[str]:
    return [str(s.get("id") or f"vllm-{i}") for i, s in enumerate(_parse_instances_env())]


async def _save_registry() -> None:
    r = getattr(_orch, "REDIS", None)
    if not r or not _REG_LOADED:
        return
    try:
        await r.set(KEY_VLLM_INSTANCES, json.dumps(_REG_DOC))
    except Exception as e:
        log.warning("vllm: save registry: %s", e)


async def _load_registry() -> None:
    """Restore runtime add/remove from Redis once it is connected (boot only)."""
    global _REG_DOC, _REG_LOADED
    for _ in range(60):
        if getattr(_orch, "REDIS", None):
            break
        await asyncio.sleep(1)
    r = getattr(_orch, "REDIS", None)
    if not r:
        log.info("vllm: registry persistence unavailable (no Redis)")
        _REG_LOADED = True       # nothing to restore; saves are no-ops too
        return
    try:
        raw = await r.get(KEY_VLLM_INSTANCES)
        _REG_DOC = _reg_core.normalise_doc(json.loads(raw) if raw else None)
    except Exception as e:
        # Leave _REG_LOADED False: a later add/remove retries the read
        # rather than saving over a document it could not read.
        log.warning("vllm: load registry: %s", e)
        return
    _REG_LOADED = True
    _apply_registry()
    if _REG_DOC["instances"] or _REG_DOC["removed"]:
        log.info("vllm: registry restored - %d instance(s)", len(VLLM_INSTANCES))


def _apply_registry() -> None:
    """Make VLLM_INSTANCES what the environment plus the stored document say,
    keeping each live instance's runtime state (status, load, models)."""
    wanted = {s["id"]: s for s in _reg_core.merge(_parse_instances_env(), _REG_DOC)}
    for iid in [i for i in VLLM_INSTANCES if i not in wanted]:
        del VLLM_INSTANCES[iid]
    for iid, spec in wanted.items():
        cur = VLLM_INSTANCES.get(iid)
        if cur is None:
            VLLM_INSTANCES[iid] = VLLMInstance(
                id=iid, url=str(spec.get("url", "")).rstrip("/"),
                label=spec.get("label") or iid, has_gpu=bool(spec.get("has_gpu", True)),
                priority=int(spec.get("priority") or 0),
                api_key=spec.get("api_key", VLLM_API_KEY),
                gate_as=str(spec.get("gate_as") or ""),
                think_default=_reg_core.think_default(spec.get("think_default")),
                stands_in_for=_reg_core.names(spec.get("stands_in_for")))
        else:
            cur.url = str(spec.get("url", cur.url)).rstrip("/")
            cur.label = spec.get("label") or cur.label
            cur.has_gpu = bool(spec.get("has_gpu", cur.has_gpu))
            cur.priority = int(spec.get("priority", cur.priority) or 0)
            cur.gate_as = str(spec.get("gate_as", cur.gate_as) or "")
            cur.think_default = _reg_core.think_default(spec.get("think_default", cur.think_default))
            cur.stands_in_for = _reg_core.names(spec.get("stands_in_for", cur.stands_in_for))


def _reg_lock() -> asyncio.Lock:
    global _REG_LOCK
    if _REG_LOCK is None:
        _REG_LOCK = asyncio.Lock()
    return _REG_LOCK


async def _refresh_registry() -> None:
    """Re-read the stored registry - every health cycle. A node worker loads it
    at boot like the host, but an instance added later (a node switched to
    vLLM) stayed invisible to it until it restarted (5 Oct 2026). On the host
    its own adds are already in the document, so this changes nothing there."""
    global _REG_DOC
    r = getattr(_orch, "REDIS", None)
    if not r or not _REG_LOADED:
        return
    async with _reg_lock():
        try:
            raw = await r.get(KEY_VLLM_INSTANCES)
            doc = _reg_core.normalise_doc(json.loads(raw) if raw else None)
        except Exception as e:
            log.debug("vllm: refresh registry: %s", e)
            return
        if doc == _REG_DOC:
            return
        _REG_DOC = doc
        before = set(VLLM_INSTANCES)
        _apply_registry()
        new = sorted(set(VLLM_INSTANCES) - before)
        for iid in new:
            await _ping_vllm(VLLM_INSTANCES[iid])
        if new or before - set(VLLM_INSTANCES):
            log.info("vllm: registry refreshed - now %d instance(s)", len(VLLM_INSTANCES))


# ══════════════════════════════════════════════════════════════════════════════
# Health monitoring
# ══════════════════════════════════════════════════════════════════════════════

async def _ping_vllm(inst: VLLMInstance) -> None:
    """Health-check one vLLM instance via /v1/models."""
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5, headers=inst.headers()) as c:
            r = await c.get(f"{inst.url}/v1/models")
            r.raise_for_status()
            data = r.json().get("data", [])
            inst.models = [m["id"] for m in data]
            inst.latency_ms = round((time.monotonic() - t0) * 1000, 1)
            inst.status = "online"
            inst.last_check = now_iso()
            inst.errors = 0
    except Exception as exc:
        inst.status = "offline"
        inst.latency_ms = None
        inst.errors += 1
        inst.last_check = now_iso()
        log.debug("vllm ping [%s] failed: %s", inst.id, exc)


async def _scrape_metrics(inst: VLLMInstance) -> None:
    """Parse Prometheus /metrics from vLLM to get queue depths and cache stats."""
    try:
        async with httpx.AsyncClient(timeout=4, headers=inst.headers()) as c:
            r = await c.get(f"{inst.url}/metrics")
            if r.status_code != 200:
                return
        text = r.text
        def _gauge(name: str) -> Optional[float]:
            m = re.search(rf'^{re.escape(name)}\s+([\d.e+\-]+)', text, re.MULTILINE)
            return float(m.group(1)) if m else None

        rr = _gauge("vllm:num_requests_running")
        wr = _gauge("vllm:num_requests_waiting")
        gc = _gauge("vllm:gpu_cache_usage_perc")
        cc = _gauge("vllm:cpu_cache_usage_perc")
        tps = _gauge("vllm:avg_generation_throughput_toks_per_s")

        if rr is not None: inst.running_requests = int(rr)
        if wr is not None: inst.waiting_requests = int(wr)
        if gc is not None: inst.gpu_cache_usage = gc / 100.0
        if cc is not None: inst.cpu_cache_usage = cc / 100.0
        if tps is not None: inst.tokens_per_second = tps

        # Mark degraded if queue is deep or cache is near full
        if inst.status == "online" and (inst.waiting_requests > 32 or inst.gpu_cache_usage > 0.97):
            inst.status = "degraded"
    except Exception:
        pass


async def _health_loop(interval: float = 20.0) -> None:
    await _ensure_registry_loaded()
    while True:
        await _refresh_registry()
        # Nothing to probe until at least one instance is configured. Idle
        # quietly (long sleep, no event) rather than pinging hosts and emitting
        # an empty vllm.health event every cycle — this is what made the module
        # noisy on the event bus when no vLLM backend was set up.
        if not VLLM_INSTANCES:
            await asyncio.sleep(max(interval, 60))
            continue
        tasks = []
        for inst in VLLM_INSTANCES.values():
            tasks.append(_ping_vllm(inst))
            if inst.status == "online":
                tasks.append(_scrape_metrics(inst))
        await asyncio.gather(*tasks, return_exceptions=True)
        await emit_event({
            "type": "vllm.health",
            "instances": {
                iid: {
                    "status": i.status,
                    "latency_ms": i.latency_ms,
                    "running": i.running_requests,
                    "waiting": i.waiting_requests,
                    "gpu_cache": round(i.gpu_cache_usage, 3),
                    "tps": round(i.tokens_per_second, 1),
                }
                for iid, i in VLLM_INSTANCES.items()
            },
        })
        await asyncio.sleep(interval)


# ══════════════════════════════════════════════════════════════════════════════
# Routing
# ══════════════════════════════════════════════════════════════════════════════

def pick_vllm_instance(
    prefer_gpu: bool = True,
    instance_id: Optional[str] = None,
    model: Optional[str] = None,
    require_lora: Optional[str] = None,
) -> Optional[VLLMInstance]:
    """
    Select the best vLLM instance using a composite score:
      - Prefer online over degraded (never offline)
      - Penalise by in_use concurrency + waiting_requests queue depth
      - Prefer GPU when requested
      - Prefer instance that already has the model or LoRA loaded
      - Fall back through priority
    """
    if instance_id and instance_id in VLLM_INSTANCES:
        return VLLM_INSTANCES[instance_id]

    candidates = {
        iid: i for iid, i in VLLM_INSTANCES.items()
        if i.status in ("online", "degraded")
    }
    if not candidates:
        return None

    if require_lora:
        lora_has = {iid: i for iid, i in candidates.items() if require_lora in i.loaded_loras}
        if lora_has:
            candidates = lora_has

    if model:
        model_has = {iid: i for iid, i in candidates.items() if model in i.models}
        if model_has:
            candidates = model_has

    if prefer_gpu:
        gpu_cands = {iid: i for iid, i in candidates.items() if i.has_gpu}
        if gpu_cands:
            candidates = gpu_cands

    def _score(inst: VLLMInstance) -> float:
        status_penalty = 0.0 if inst.status == "online" else 10.0
        queue_penalty  = inst.in_use * 2 + inst.waiting_requests * 0.5
        cache_penalty  = max(0.0, inst.gpu_cache_usage - 0.85) * 20.0
        return status_penalty + queue_penalty + cache_penalty + inst.priority

    return min(candidates.values(), key=_score)


def standins(model: Optional[str] = None) -> Dict[str, dict]:
    """Switched nodes the Ollama picker may choose, as Ollama-shaped records
    (node_backend_core.standins): a node moved to vLLM keeps taking its share
    of load-balanced work instead of dropping out of routing."""
    return _backend_core.standins(getattr(_orch, "OLLAMA_INSTANCES", {}) or {},
                                  VLLM_INSTANCES, model=model, serves=_route_core.serves)


def standin_for(node_id: str) -> str:
    """The vLLM instance a picked node id is served by ('' for an Ollama node)."""
    if not node_id or node_id not in _backend_core.switched_ids(
            getattr(_orch, "OLLAMA_INSTANCES", {}) or {}, VLLM_INSTANCES):
        return ""
    return _backend_core.vllm_of(node_id, VLLM_INSTANCES)


def effective_rule(rule: Optional[dict]) -> Optional[dict]:
    """The rule as routing applies it: a pin naming a node that is switched to
    vLLM (nodes.backend.switch) becomes "vllm:<its vLLM instance>", so rules keep
    naming the node ("cpu-246") whichever backend it serves."""
    if not rule or not rule.get("pin"):
        return rule
    pin = _backend_core.resolve_pin(str(rule["pin"]),
                                    getattr(_orch, "OLLAMA_INSTANCES", {}) or {},
                                    VLLM_INSTANCES)
    return rule if pin == rule["pin"] else dict(rule, pin=pin)


async def routed_call(kind: str, rule: Optional[dict], *, model: Optional[str] = None,
                      prefer_gpu: bool = False, job_type: str = "",
                      slot_timeout: Optional[float] = None,
                      stream_cb: Optional[Callable] = None, **kw) -> Optional[dict]:
    """Serve one routed request on vLLM when the routing-table rule pins it.

    The routing-table counterpart of an Ollama node: target chosen by
    vllm_route_core (pin / deny_gpu / prefer_gpu / model), the hardware's
    semaphore + cross-process gate held for generate and chat (embeddings never
    take it, as on Ollama), route demand recorded, and the server's real model
    reported. kind: 'generate' (kw: prompt, max_tokens, temperature, top_p,
    guided_json, caller_override) | 'chat' (kw: messages, max_tokens, ...) |
    'embed' (kw: inputs).

    Returns None when the rule does not pin vLLM (the caller routes to Ollama).
    Otherwise {ok, result, instance, model, gate, error}; on ok=False the caller
    falls back to Ollama routing, exactly as for a failed Ollama node.
    """
    rule = effective_rule(rule)
    pin = _route_core.pin_target(rule)
    if not pin:
        return None
    rule = rule or {}
    iid = _route_core.choose(VLLM_INSTANCES, pin, model=model,
                             prefer_gpu=bool(prefer_gpu or rule.get("prefer_gpu")),
                             deny_gpu=bool(rule.get("deny_gpu")))
    if not iid:
        return {"ok": False, "error": f"no vLLM server up for pin vllm:{pin}",
                "instance": "", "model": model or "", "gate": ""}
    inst = VLLM_INSTANCES[iid]
    mdl = _route_core.served_model(inst, model) or ""
    try:
        _orch.ROUTE_DEMAND.append((time.time(), str(job_type or "default")))
    except Exception:
        pass
    out: Dict[str, Any] = {"instance": iid, "model": mdl, "gate": "", "error": ""}
    if kind == "embed":
        # Never substitute an embedder: another model's vectors (often another
        # dimension) would land in stores indexed by the requested one.
        if not _route_core.serves(inst, model):
            out.update(ok=False, result=[],
                       error=f"{iid} does not serve embedding model {model!r}")
            return out
        vecs = await vllm_embed(kw.get("inputs") or [], model=mdl or None, instance_id=iid)
        # unit length, as Ollama's /api/embed returns them: the stored vectors
        # are, and vLLM's are not (norm ~20 for nomic) - the direction matches
        vecs = [_route_core.unit(v) for v in (vecs or [])]
        out.update(ok=bool(vecs), result=vecs, error="" if vecs else "empty embedding")
        return out
    gid = _route_core.gate_id(inst, iid)
    out["gate"] = gid
    async with _orch._ollama_slot(gid, timeout=slot_timeout) as act:
        # Stream internally so the gate heartbeat sees token liveness, exactly
        # as for an Ollama generation; forward tokens to the caller's callback.
        async def _tok(t: str):
            if isinstance(act, dict):
                act["t"] = time.monotonic()
            if stream_cb is not None:
                r = stream_cb(t)
                if asyncio.iscoroutine(r):
                    await r

        if kind == "generate":
            text = await vllm_generate(model=mdl or None, instance_id=iid,
                                       prefer_gpu=bool(inst.has_gpu), stream_cb=_tok, **kw)
            # vllm_generate reports a failure as "" (it logs + emits request_error)
            out.update(ok=bool(text), result=text,
                       error="" if text else "empty vLLM generation")
        elif kind == "chat":
            msg = await vllm_chat(model=mdl or None, instance_id=iid,
                                  prefer_gpu=bool(inst.has_gpu), stream_cb=_tok, **kw)
            err = (msg or {}).get("error") if isinstance(msg, dict) else "no reply"
            out.update(ok=not err, result=msg, error=str(err or ""))
        else:
            raise ValueError(f"routed_call: unknown kind {kind!r}")
    return out


def chat_target(rule: Optional[dict], *, model: Optional[str] = None,
                prefer_gpu: bool = False) -> Optional[dict]:
    """Where a routed CHAT goes when its rule pins vLLM (None = Ollama routing).

    {instance, gate, url, model}: `gate` is the semaphore/GPU-gate key the chat
    path takes (the Ollama node's id for a server on the same hardware).
    """
    rule = effective_rule(rule)
    pin = _route_core.pin_target(rule)
    if not pin:
        return None
    rule = rule or {}
    iid = _route_core.choose(VLLM_INSTANCES, pin, model=model,
                             prefer_gpu=bool(prefer_gpu or rule.get("prefer_gpu")),
                             deny_gpu=bool(rule.get("deny_gpu")))
    if not iid:
        log.info("vllm chat pin vllm:%s: no server up - Ollama routing", pin)
        return None
    inst = VLLM_INSTANCES[iid]
    try:
        _orch.ROUTE_DEMAND.append((time.time(), "chat"))
    except Exception:
        pass
    return {"instance": iid, "gate": _route_core.gate_id(inst, iid), "url": inst.url,
            "model": _route_core.served_model(inst, model) or (model or "")}


async def ollama_chat_once(client: "httpx.AsyncClient", target: dict,
                           ollama_body: dict) -> dict:
    """POST an Ollama-shaped chat body to the vLLM target; Ollama-shaped reply."""
    inst = VLLM_INSTANCES.get(target["instance"])
    body = _route_core.chat_body(dict(ollama_body, stream=False), target.get("model"))
    r = await client.post(f"{target['url']}/v1/chat/completions", json=body,
                          headers=inst.headers() if inst else None)
    r.raise_for_status()
    return _route_core.chat_reply(r.json())


class _OllamaShapedStream:
    """The bits of an httpx streaming response Vera's chat loop reads, with
    vLLM's SSE chat deltas rewritten as Ollama NDJSON lines (one done line)."""

    def __init__(self, resp):
        self._resp = resp
        self.status_code = resp.status_code
        self.headers = resp.headers

    async def aread(self):
        return await self._resp.aread()

    async def aiter_lines(self):
        done_sent = False
        async for line in self._resp.aiter_lines():
            ch = _route_core.sse_to_ollama_line(line)
            if ch is None:
                continue
            if ch.get("done"):
                if done_sent:
                    continue
                done_sent = True
            yield json.dumps(ch)
        if not done_sent:
            yield json.dumps({"message": {"role": "assistant", "content": ""}, "done": True})


@asynccontextmanager
async def ollama_chat_stream(client: "httpx.AsyncClient", target: dict, ollama_body: dict):
    """`async with` a streaming vLLM chat that reads like an Ollama /api/chat stream."""
    inst = VLLM_INSTANCES.get(target["instance"])
    body = _route_core.chat_body(dict(ollama_body, stream=True), target.get("model"))
    async with client.stream("POST", f"{target['url']}/v1/chat/completions", json=body,
                             headers=inst.headers() if inst else None) as resp:
        yield _OllamaShapedStream(resp)


# ══════════════════════════════════════════════════════════════════════════════
# Core generation helpers
# ══════════════════════════════════════════════════════════════════════════════

async def vllm_generate(
    prompt: str,
    model: Optional[str] = None,
    instance_id: Optional[str] = None,
    prefer_gpu: bool = True,
    max_tokens: int = 512,
    temperature: float = 0.7,
    top_p: float = 0.9,
    top_k: int = -1,
    repetition_penalty: float = 1.0,
    stop: Optional[List[str]] = None,
    logprobs: Optional[int] = None,
    # Guided decoding (structured output)
    guided_json: Optional[dict] = None,
    guided_regex: Optional[str] = None,
    guided_choice: Optional[List[str]] = None,
    guided_grammar: Optional[str] = None,
    # LoRA
    lora_name: Optional[str] = None,
    stream_cb: Optional[Callable] = None,
    # Extra passthrough
    extra: Optional[dict] = None,
    # Caller passthrough — lets llm.generate (and other intermediaries) log
    # the true upstream caller rather than vllm_generate itself.
    caller_override: Optional[dict] = None,
) -> str:
    """
    Call /v1/completions on the best available vLLM instance.
    Mirrors ollama_generate() API for easy drop-in substitution.
    """
    inst = pick_vllm_instance(prefer_gpu=prefer_gpu, instance_id=instance_id, model=model,
                               require_lora=lora_name)
    if inst is None:
        log.error("vllm_generate: no available instance")
        return ""

    mdl = model or VLLM_MODEL or (inst.models[0] if inst.models else "")
    if not mdl:
        log.error("vllm_generate: no model specified and none detected on instance %s", inst.id)
        return ""

    # ── Caller identification ─────────────────────────────────────────────────
    import traceback as _tb, os as _os
    if caller_override:
        _caller_file = caller_override.get("caller_file", "vllm_capabilities.py")
        _caller_func = caller_override.get("caller_func", "vllm_generate")
        _cap_name    = caller_override.get("cap_name", "vllm.generate")
    else:
        _caller_file = "vllm_capabilities.py"
        _caller_func = "vllm_generate"
        _cap_name    = "vllm.generate"
        try:
            _stack = _tb.extract_stack(limit=8)
            _this  = _os.path.basename(__file__)
            for _fr in reversed(_stack[:-1]):
                _fn = _os.path.basename(_fr.filename)
                if _fn != _this and not _fn.startswith("<"):
                    _caller_file = _fn
                    _caller_func = _fr.name
                    break
        except Exception:
            pass

    _req_id = str(uuid.uuid4())[:12]
    _t0     = time.time()
    _prompt_preview = (prompt or "")[:120].replace("\n", " ")

    log.info("vllm_req [%s] model=%s inst=%s caller=%s:%s prompt=%s",
             _req_id, mdl, inst.id, _caller_file, _caller_func, _prompt_preview)
    try:
        await emit_event({
            "type":           "ollama.request",  # reuse same event type for Workers panel
            "req_id":         _req_id,
            "model":          mdl,
            "instance_id":    inst.id,
            "instance_url":   inst.url,
            "caller_file":    _caller_file,
            "caller_func":    _caller_func,
            "caller_module":  _caller_file.replace(".py", ""),
            "cap_name":       _cap_name,
            "prompt_preview": _prompt_preview,
            "prompt_full":    (prompt or "")[:16000],
            "backend":        "vllm",
            "prefer_gpu":     prefer_gpu,
            "streaming":      stream_cb is not None,
        })
    except Exception:
        pass

    body: dict = {
        "model": mdl,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "stream": stream_cb is not None,
    }
    if top_k > 0:              body["top_k"] = top_k
    if repetition_penalty != 1.0: body["repetition_penalty"] = repetition_penalty
    if stop:                   body["stop"] = stop
    if logprobs:               body["logprobs"] = logprobs
    if guided_json:            body["guided_json"] = guided_json
    if guided_regex:           body["guided_regex"] = guided_regex
    if guided_choice:          body["guided_choice"] = guided_choice
    if guided_grammar:         body["guided_grammar"] = guided_grammar

    if lora_name and lora_name in inst.loaded_loras:
        body["lora_request"] = {
            "lora_name": lora_name,
            "lora_int_id": inst.loaded_loras[lora_name],
            "lora_local_path": lora_name,   # vLLM expects path on the server
        }
    if extra:
        body.update(extra)

    inst.in_use += 1
    try:
        async with httpx.AsyncClient(timeout=_req_timeout(), headers=inst.headers()) as c:
            if stream_cb:
                async with c.stream("POST", f"{inst.url}/v1/completions", json=body) as resp:
                    resp.raise_for_status()
                    buf: List[str] = []
                    async for line in resp.aiter_lines():
                        if not line or line == "data: [DONE]":
                            continue
                        if line.startswith("data: "):
                            try:
                                chunk = json.loads(line[6:])
                                tok = chunk["choices"][0].get("text", "")
                                if tok:
                                    buf.append(tok)
                                    await stream_cb(tok)
                            except Exception:
                                pass
                    _elapsed = round(time.time() - _t0, 2)
                    log.info("vllm_done [%s] %.2fs tokens=%d caller=%s:%s",
                             _req_id, _elapsed, len(buf), _caller_file, _caller_func)
                    try:
                        await emit_event({
                            "type": "ollama.request_done", "req_id": _req_id,
                            "model": mdl, "instance_id": inst.id,
                            "caller_file": _caller_file, "caller_func": _caller_func,
                            "elapsed_s": _elapsed, "token_count": len(buf), "backend": "vllm",
                        })
                    except Exception:
                        pass
                    return "".join(buf)
            else:
                r = await c.post(f"{inst.url}/v1/completions", json=body)
                r.raise_for_status()
                _text = r.json()["choices"][0]["text"]
                _elapsed = round(time.time() - _t0, 2)
                log.info("vllm_done [%s] %.2fs caller=%s:%s",
                         _req_id, _elapsed, _caller_file, _caller_func)
                try:
                    await emit_event({
                        "type": "ollama.request_done", "req_id": _req_id,
                        "model": mdl, "instance_id": inst.id,
                        "caller_file": _caller_file, "caller_func": _caller_func,
                        "elapsed_s": _elapsed, "backend": "vllm",
                    })
                except Exception:
                    pass
                return _text

    except Exception as exc:
        _elapsed = round(time.time() - _t0, 2)
        log.error("vllm_generate [%s] FAILED %.2fs inst=%s caller=%s:%s err=%s",
                  _req_id, _elapsed, inst.id, _caller_file, _caller_func, _err(exc))
        inst.errors += 1
        try:
            await emit_event({
                "type": "ollama.request_error", "req_id": _req_id,
                "model": mdl, "instance_id": inst.id,
                "caller_file": _caller_file, "caller_func": _caller_func,
                "elapsed_s": _elapsed, "error": str(exc)[:200], "backend": "vllm",
            })
        except Exception:
            pass
        # Failover to next best
        fallback = pick_vllm_instance(prefer_gpu=prefer_gpu, model=mdl)
        if fallback and fallback.id != inst.id:
            log.info("vllm_generate: failing over to %s", fallback.id)
            body["stream"] = False
            try:
                async with httpx.AsyncClient(timeout=_req_timeout(), headers=fallback.headers()) as c:
                    r = await c.post(f"{fallback.url}/v1/completions", json=body)
                    r.raise_for_status()
                    return r.json()["choices"][0]["text"]
            except Exception as exc2:
                log.error("vllm_generate fallback [%s]: %s", fallback.id, exc2)
        return ""
    finally:
        inst.in_use = max(0, inst.in_use - 1)


async def vllm_chat(
    messages: List[dict],
    model: Optional[str] = None,
    instance_id: Optional[str] = None,
    prefer_gpu: bool = True,
    max_tokens: Optional[int] = 512,
    temperature: float = 0.7,
    top_p: float = 0.9,
    stop: Optional[List[str]] = None,
    tools: Optional[List[dict]] = None,
    tool_choice: Optional[str] = None,
    # Guided decoding
    guided_json: Optional[dict] = None,
    guided_regex: Optional[str] = None,
    # LoRA
    lora_name: Optional[str] = None,
    stream_cb: Optional[Callable] = None,
    extra: Optional[dict] = None,
) -> dict:
    """
    Call /v1/chat/completions — returns the full choices[0].message dict.
    Supports tool_calls (function calling) and guided decoding.
    """
    inst = pick_vllm_instance(prefer_gpu=prefer_gpu, instance_id=instance_id, model=model,
                               require_lora=lora_name)
    if inst is None:
        return {"error": "no available vLLM instance"}

    mdl = model or VLLM_MODEL or (inst.models[0] if inst.models else "")
    if not mdl:
        return {"error": "no model specified"}

    body: dict = {
        "model": mdl,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "stream": stream_cb is not None,
    }
    if max_tokens:     body["max_tokens"] = max_tokens   # None = the rest of the window
    if stop:           body["stop"] = stop
    if tools:          body["tools"] = tools
    if tool_choice:    body["tool_choice"] = tool_choice
    if guided_json:    body["guided_json"] = guided_json
    if guided_regex:   body["guided_regex"] = guided_regex
    if lora_name and lora_name in inst.loaded_loras:
        body["lora_request"] = {
            "lora_name": lora_name,
            "lora_int_id": inst.loaded_loras[lora_name],
            "lora_local_path": lora_name,
        }
    if extra:
        body.update(extra)
    body = _route_core.apply_think_default(body, inst.think_default)

    inst.in_use += 1
    try:
        async with httpx.AsyncClient(timeout=_req_timeout(), headers=inst.headers()) as c:
            if stream_cb:
                async with c.stream("POST", f"{inst.url}/v1/chat/completions", json=body) as resp:
                    resp.raise_for_status()
                    buf: List[str] = []
                    async for line in resp.aiter_lines():
                        if not line or line == "data: [DONE]":
                            continue
                        if line.startswith("data: "):
                            try:
                                chunk = json.loads(line[6:])
                                delta = chunk["choices"][0]["delta"].get("content", "")
                                if delta:
                                    buf.append(delta)
                                    await stream_cb(delta)
                            except Exception:
                                pass
                    return {"role": "assistant", "content": "".join(buf)}
            else:
                r = await c.post(f"{inst.url}/v1/chat/completions", json=body)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]

    except Exception as exc:
        log.error("vllm_chat [%s]: %s", inst.id, _err(exc))
        inst.errors += 1
        return {"error": _err(exc)}
    finally:
        inst.in_use = max(0, inst.in_use - 1)


async def vllm_embed(
    inputs: List[str],
    model: Optional[str] = None,
    instance_id: Optional[str] = None,
) -> List[List[float]]:
    """Call /v1/embeddings and return list of embedding vectors."""
    inst = pick_vllm_instance(instance_id=instance_id, model=model)
    if inst is None:
        return []

    mdl = model or VLLM_MODEL or (inst.models[0] if inst.models else "")
    body = {"model": mdl, "input": inputs}
    inst.in_use += 1
    try:
        async with httpx.AsyncClient(timeout=60, headers=inst.headers()) as c:
            r = await c.post(f"{inst.url}/v1/embeddings", json=body)
            r.raise_for_status()
            data = r.json().get("data", [])
            return [d["embedding"] for d in sorted(data, key=lambda x: x["index"])]
    except Exception as exc:
        log.error("vllm_embed [%s]: %s", inst.id, exc)
        return []
    finally:
        inst.in_use = max(0, inst.in_use - 1)


# ══════════════════════════════════════════════════════════════════════════════
# Capabilities — instance management
# ══════════════════════════════════════════════════════════════════════════════

@capability(
    name="vllm.status",
    contract={"presentation": {"kind": "composite", "parts": [{"kind": "table", "form": "table", "map": {"rows": "instances"}, "draw": {"columns": ["name", "label", "status", "has_gpu", "latency_ms", "in_use", "errors", "tokens_per_second"]}, "title": "Instances", "empty": "No instances"}, {"kind": "kv", "map": {"fields": "global_config"}, "title": "Config"}]}},
    description="Get a health summary of all configured vLLM instances",
    http_method="GET",
    http_path="/vllm/status",
)
async def vllm_status() -> dict:
    instances = {}
    for iid, inst in VLLM_INSTANCES.items():
        instances[iid] = {
            "id": inst.id,
            "url": inst.url,
            "label": inst.label,
            "status": inst.status,
            "has_gpu": inst.has_gpu,
            "gate_as": inst.gate_as,
            "think_default": inst.think_default,
            "stands_in_for": list(inst.stands_in_for),
            "latency_ms": inst.latency_ms,
            "in_use": inst.in_use,
            "errors": inst.errors,
            "models": inst.models,
            "running_requests": inst.running_requests,
            "waiting_requests": inst.waiting_requests,
            "gpu_cache_usage": round(inst.gpu_cache_usage, 3),
            "cpu_cache_usage": round(inst.cpu_cache_usage, 3),
            "tokens_per_second": round(inst.tokens_per_second, 1),
            "loaded_loras": list(inst.loaded_loras.keys()),
            "pid": inst.pid,
        }
    return {
        "instances": instances,
        "total": len(VLLM_INSTANCES),
        "online": sum(1 for i in VLLM_INSTANCES.values() if i.status == "online"),
        "default_model": VLLM_MODEL,
        "global_config": {
            "quantization": VLLM_QUANTIZATION,
            "tensor_parallel": VLLM_TENSOR_PAR,
            "gpu_mem_util": VLLM_GPU_MEM_UTIL,
            "cpu_offload_gb": VLLM_CPU_OFFLOAD_GB,
            "spec_model": VLLM_SPEC_MODEL,
            "dtype": VLLM_DTYPE,
            "lora_enabled": VLLM_ENABLE_LORA,
        },
    }


@capability(
    name="vllm.instances.add",
    description="Register a vLLM server instance at runtime (persisted across "
                "restarts). Inputs: id (str!), url (str!), label, has_gpu (bool="
                "true — false for a CPU server), priority (int), api_key (str — "
                "not persisted; env VLLM_API_KEY applies after a restart), "
                "gate_as (str — the Ollama node id this server shares hardware "
                "with; its calls then take that node's GPU gate).",
    http_method="POST",
    http_path="/vllm/instances",
    schema={
        "properties": {
            "id":       {"type": "string"},
            "url":      {"type": "string"},
            "label":    {"type": "string"},
            "has_gpu":  {"type": "boolean"},
            "priority": {"type": "integer"},
            "api_key":  {"type": "string"},
            "gate_as":  {"type": "string"},
        },
        "required": ["id", "url"],
    },
)
async def vllm_instances_add(
    id: str, url: str, label: str = "", has_gpu: bool = True,
    priority: int = None, api_key: str = "", gate_as: str = "",
) -> dict:
    await _ensure_registry_loaded()
    global _REG_DOC
    async with _reg_lock():          # a health-cycle refresh must not land mid-change
        if id in VLLM_INSTANCES:
            return {"ok": False, "error": f"Instance '{id}' already exists. Remove it first."}
        inst = VLLMInstance(
            id=id, url=url.rstrip("/"), label=label or id,
            has_gpu=has_gpu,
            priority=priority if priority is not None else len(VLLM_INSTANCES),
            api_key=api_key or VLLM_API_KEY,
            gate_as=str(gate_as or ""),
        )
        VLLM_INSTANCES[id] = inst
        _REG_DOC = _reg_core.note_added(_REG_DOC, id, {
            "url": inst.url, "label": inst.label, "has_gpu": inst.has_gpu,
            "priority": inst.priority, "gate_as": inst.gate_as,
            "think_default": inst.think_default, "stands_in_for": list(inst.stands_in_for)})
        await _save_registry()
    await _ping_vllm(inst)
    await emit_event({"type": "vllm.instance.added", "id": id, "url": url,
                      "has_gpu": inst.has_gpu})
    return {"ok": True, "id": id, "status": inst.status, "models": inst.models}


@capability(
    name="vllm.instances.think",
    description="Set a vLLM server's thinking default for requests that do not choose: "
                "'default' (the model's own), 'on' or 'off'. A request that chooses "
                "(an agent's Think, llm.generate think=) still wins. Persisted with the "
                "registry. Inputs: instance_id (str!), think ('default'|'on'|'off').",
    http_method="POST",
    http_path="/vllm/instances/think",
    schema={"properties": {"instance_id": {"type": "string"},
                           "think": {"enum": ["default", "on", "off"]}},
            "required": ["instance_id", "think"]},
)
async def vllm_instances_think(instance_id: str, think: str = "default") -> dict:
    await _ensure_registry_loaded()
    global _REG_DOC
    async with _reg_lock():
        inst = VLLM_INSTANCES.get(instance_id)
        if inst is None:
            return {"ok": False, "error": f"Instance '{instance_id}' not found"}
        inst.think_default = _reg_core.think_default("" if think == "default" else think)
        _REG_DOC = _reg_core.note_added(_REG_DOC, instance_id, {
            "url": inst.url, "label": inst.label, "has_gpu": inst.has_gpu,
            "priority": inst.priority, "gate_as": inst.gate_as,
            "think_default": inst.think_default, "stands_in_for": list(inst.stands_in_for)})
        await _save_registry()
    await emit_event({"type": "vllm.instance.think", "id": instance_id,
                      "think_default": inst.think_default or "default"})
    return {"ok": True, "id": instance_id, "think_default": inst.think_default or "default"}


@capability(
    name="vllm.instances.stand_in",
    description="Set the model names a vLLM server stands in for: Vera routes requests "
                "for those names to it (sending the server's own model), e.g. when no "
                "server can run the requested model. Visible on the vLLM page; an empty "
                "list removes them. Persisted with the registry. Inputs: instance_id "
                "(str!), names (list[str] or comma string).",
    http_method="POST",
    http_path="/vllm/instances/stand_in",
    schema={"properties": {"instance_id": {"type": "string"},
                           "names": {"type": "array", "items": {"type": "string"}}},
            "required": ["instance_id"]},
)
async def vllm_instances_stand_in(instance_id: str, names=None) -> dict:
    await _ensure_registry_loaded()
    global _REG_DOC
    async with _reg_lock():
        inst = VLLM_INSTANCES.get(instance_id)
        if inst is None:
            return {"ok": False, "error": f"Instance '{instance_id}' not found"}
        inst.stands_in_for = [n for n in _reg_core.names(names) if n not in inst.models]
        _REG_DOC = _reg_core.note_added(_REG_DOC, instance_id, {
            "url": inst.url, "label": inst.label, "has_gpu": inst.has_gpu,
            "priority": inst.priority, "gate_as": inst.gate_as,
            "think_default": inst.think_default, "stands_in_for": list(inst.stands_in_for)})
        await _save_registry()
    await emit_event({"type": "vllm.instance.stand_in", "id": instance_id,
                      "stands_in_for": list(inst.stands_in_for)})
    return {"ok": True, "id": instance_id, "stands_in_for": list(inst.stands_in_for)}


@capability(
    name="vllm.instances.remove",
    description="Remove a vLLM instance from the registry",
    http_method="DELETE",
    http_path="/vllm/instances/{instance_id}",
    schema={"properties": {"instance_id": {"type": "string"}}, "required": ["instance_id"]},
)
async def vllm_instances_remove(instance_id: str) -> dict:
    await _ensure_registry_loaded()
    global _REG_DOC
    async with _reg_lock():
        if instance_id not in VLLM_INSTANCES:
            return {"ok": False, "error": f"Instance '{instance_id}' not found"}
        del VLLM_INSTANCES[instance_id]
        _REG_DOC = _reg_core.note_removed(_REG_DOC, instance_id, _env_ids())
        await _save_registry()
    await emit_event({"type": "vllm.instance.removed", "id": instance_id})
    return {"ok": True, "id": instance_id}


@capability(
    name="vllm.models",
    contract={"presentation": {"kind": "table", "form": "list", "map": {"rows": "all"}, "title": "Models served", "empty": "No vLLM instance is online"}},
    description="List all models served across all vLLM instances",
    http_method="GET",
    http_path="/vllm/models",
)
async def vllm_models() -> dict:
    result: Dict[str, List[str]] = {}
    for iid, inst in VLLM_INSTANCES.items():
        if inst.status in ("online", "degraded"):
            result[iid] = inst.models
    # deduplicated global list
    all_models = sorted({m for models in result.values() for m in models})
    return {"by_instance": result, "all": all_models}


# ══════════════════════════════════════════════════════════════════════════════
# Capabilities — generation
# ══════════════════════════════════════════════════════════════════════════════

@capability(
    name="vllm.generate",
    description="Raw text completion via vLLM /v1/completions with full parameter access",
    http_method="POST",
    http_path="/vllm/generate",
    schema={
        "properties": {
            "prompt":             {"type": "string"},
            "model":              {"type": "string"},
            "instance_id":        {"type": "string"},
            "max_tokens":         {"type": "integer", "default": 512},
            "temperature":        {"type": "number", "default": 0.7},
            "top_p":              {"type": "number", "default": 0.9},
            "top_k":              {"type": "integer", "default": -1},
            "repetition_penalty": {"type": "number", "default": 1.0},
            "stop":               {"type": "array"},
            "logprobs":           {"type": "integer"},
            "guided_json":        {"type": "object", "description": "JSON schema for structured output"},
            "guided_regex":       {"type": "string", "description": "Regex pattern for constrained output"},
            "guided_choice":      {"type": "array",  "description": "Constrain output to one of these strings"},
            "lora_name":          {"type": "string"},
            "prefer_gpu":         {"type": "boolean", "default": True},
        },
        "required": ["prompt"],
    },
)
async def cap_vllm_generate(
    prompt: str,
    model: str = None,
    instance_id: str = None,
    max_tokens: int = 512,
    temperature: float = 0.7,
    top_p: float = 0.9,
    top_k: int = -1,
    repetition_penalty: float = 1.0,
    stop: list = None,
    logprobs: int = None,
    guided_json: dict = None,
    guided_regex: str = None,
    guided_choice: list = None,
    lora_name: str = None,
    prefer_gpu: bool = True,
) -> dict:
    text = await vllm_generate(
        prompt=prompt, model=model, instance_id=instance_id, prefer_gpu=prefer_gpu,
        max_tokens=max_tokens, temperature=temperature, top_p=top_p, top_k=top_k,
        repetition_penalty=repetition_penalty, stop=stop, logprobs=logprobs,
        guided_json=guided_json, guided_regex=guided_regex, guided_choice=guided_choice,
        lora_name=lora_name,
    )
    return {"text": text, "model": model or VLLM_MODEL}


@capability(
    name="vllm.chat",
    description="Chat completion via vLLM /v1/chat/completions (OpenAI-compatible)",
    http_method="POST",
    http_path="/vllm/chat",
    schema={
        "properties": {
            "messages":     {"type": "array",  "description": "[{role, content}]"},
            "model":        {"type": "string"},
            "instance_id":  {"type": "string"},
            "max_tokens":   {"type": "integer", "default": 512},
            "temperature":  {"type": "number",  "default": 0.7},
            "top_p":        {"type": "number",  "default": 0.9},
            "stop":         {"type": "array"},
            "tools":        {"type": "array",  "description": "OpenAI-style function/tool definitions"},
            "tool_choice":  {"type": "string"},
            "guided_json":  {"type": "object"},
            "guided_regex": {"type": "string"},
            "lora_name":    {"type": "string"},
            "prefer_gpu":   {"type": "boolean", "default": True},
        },
        "required": ["messages"],
    },
)
async def cap_vllm_chat(
    messages: list,
    model: str = None,
    instance_id: str = None,
    max_tokens: int = 512,
    temperature: float = 0.7,
    top_p: float = 0.9,
    stop: list = None,
    tools: list = None,
    tool_choice: str = None,
    guided_json: dict = None,
    guided_regex: str = None,
    lora_name: str = None,
    prefer_gpu: bool = True,
) -> dict:
    msg = await vllm_chat(
        messages=messages, model=model, instance_id=instance_id, prefer_gpu=prefer_gpu,
        max_tokens=max_tokens, temperature=temperature, top_p=top_p, stop=stop,
        tools=tools, tool_choice=tool_choice, guided_json=guided_json, guided_regex=guided_regex,
        lora_name=lora_name,
    )
    return msg


@capability(
    name="vllm.embed",
    description="Generate embeddings via vLLM /v1/embeddings",
    http_method="POST",
    http_path="/vllm/embed",
    schema={
        "properties": {
            "inputs":       {"type": "array",  "description": "List of strings to embed"},
            "model":        {"type": "string"},
            "instance_id":  {"type": "string"},
        },
        "required": ["inputs"],
    },
)
async def cap_vllm_embed(inputs: list, model: str = None, instance_id: str = None) -> dict:
    vecs = await vllm_embed(inputs=inputs, model=model, instance_id=instance_id)
    return {"embeddings": vecs, "count": len(vecs), "dim": len(vecs[0]) if vecs else 0}


# ══════════════════════════════════════════════════════════════════════════════
# Capabilities — LoRA
# ══════════════════════════════════════════════════════════════════════════════

@capability(
    name="vllm.lora.load",
    description="Register a LoRA adapter on a vLLM instance (instance must have --enable-lora)",
    http_method="POST",
    http_path="/vllm/lora/load",
    schema={
        "properties": {
            "lora_name":      {"type": "string", "description": "Friendly name for the adapter"},
            "lora_path":      {"type": "string", "description": "Path on the vLLM server host"},
            "instance_id":    {"type": "string"},
        },
        "required": ["lora_name", "lora_path"],
    },
)
async def vllm_lora_load(lora_name: str, lora_path: str, instance_id: str = None) -> dict:
    global _LORA_ID_COUNTER
    inst = pick_vllm_instance(instance_id=instance_id)
    if not inst:
        return {"ok": False, "error": "No available instance"}

    lora_int_id = _LORA_ID_COUNTER
    _LORA_ID_COUNTER += 1
    inst.loaded_loras[lora_name] = lora_int_id

    await emit_event({"type": "vllm.lora.loaded", "instance": inst.id,
                      "lora_name": lora_name, "lora_int_id": lora_int_id})
    return {
        "ok": True,
        "lora_name": lora_name,
        "lora_int_id": lora_int_id,
        "instance": inst.id,
        "note": "Adapter registered. vLLM loads it on first use. Ensure --enable-lora and --max-loras are set on the server.",
    }


@capability(
    name="vllm.lora.list",
    description="List all registered LoRA adapters across instances",
    http_method="GET",
    http_path="/vllm/lora",
)
async def vllm_lora_list() -> dict:
    result = {}
    for iid, inst in VLLM_INSTANCES.items():
        result[iid] = [{"name": n, "int_id": v} for n, v in inst.loaded_loras.items()]
    return {"by_instance": result}


# ══════════════════════════════════════════════════════════════════════════════
# Capabilities — metrics
# ══════════════════════════════════════════════════════════════════════════════

@capability(
    name="vllm.metrics",
    description="Fetch and parse Prometheus metrics from a vLLM instance",
    http_method="GET",
    http_path="/vllm/metrics",
    schema={
        "properties": {
            "instance_id": {"type": "string"},
        },
    },
)
async def cap_vllm_metrics(instance_id: str = None) -> dict:
    inst = (VLLM_INSTANCES.get(instance_id) if instance_id
            else next((i for i in VLLM_INSTANCES.values() if i.status == "online"), None))
    if not inst:
        return {"ok": False, "error": "No instance available"}

    try:
        async with httpx.AsyncClient(timeout=5, headers=inst.headers()) as c:
            r = await c.get(f"{inst.url}/metrics")
            r.raise_for_status()
        raw = r.text

        # Parse all gauge/counter lines into a dict
        parsed: Dict[str, float] = {}
        for line in raw.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            m = re.match(r'^([\w:{}",=]+)\s+([\d.e+\-]+)', line)
            if m:
                parsed[m.group(1)] = float(m.group(2))

        return {"ok": True, "instance": inst.id, "raw_count": len(parsed), "metrics": parsed}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ══════════════════════════════════════════════════════════════════════════════
# Capabilities — server lifecycle (managed subprocess)
# ══════════════════════════════════════════════════════════════════════════════

_MANAGED_PROCS: Dict[str, subprocess.Popen] = {}

@capability(
    name="vllm.server.start",
    description="Launch a vLLM OpenAI-compatible server as a managed subprocess",
    http_method="POST",
    http_path="/vllm/server/start",
    schema={
        "properties": {
            "instance_id":          {"type": "string"},
            "model":                {"type": "string", "description": "HuggingFace model ID or local path"},
            "port":                 {"type": "integer", "default": 8001},
            "host":                 {"type": "string", "default": "0.0.0.0"},
            "tensor_parallel_size": {"type": "integer", "default": 1},
            "gpu_memory_utilization":{"type": "number", "default": 0.90},
            "cpu_offload_gb":       {"type": "number", "default": 0},
            "quantization":         {"type": "string", "description": "blank = from the model's config | awq | gptq | compressed-tensors | fp8 | bitsandbytes | gguf"},
            "dtype":                {"type": "string", "default": "auto"},
            "max_model_len":        {"type": "integer"},
            "spec_model":           {"type": "string", "description": "Draft model for speculative decoding"},
            "spec_num_speculative_tokens": {"type": "integer", "default": 5},
            "enable_lora":          {"type": "boolean", "default": False},
            "max_loras":            {"type": "integer", "default": 4},
            "enable_prefix_caching":{"type": "boolean", "default": True},
            "enable_chunked_prefill":{"type": "boolean", "default": True},
            "max_num_batched_tokens":{"type": "integer"},
            "api_key":              {"type": "string"},
            "served_model_name":    {"type": "string"},
            "extra_args":           {"type": "array", "description": "Additional CLI flags"},
        },
        "required": ["model"],
    },
)
async def vllm_server_start(
    model: str,
    instance_id: str = None,
    port: int = 8001,
    host: str = "0.0.0.0",
    tensor_parallel_size: int = None,
    gpu_memory_utilization: float = None,
    cpu_offload_gb: float = None,
    quantization: str = None,
    dtype: str = None,
    max_model_len: int = None,
    spec_model: str = None,
    spec_num_speculative_tokens: int = 5,
    enable_lora: bool = None,
    max_loras: int = None,
    enable_prefix_caching: bool = True,
    enable_chunked_prefill: bool = True,
    max_num_batched_tokens: int = None,
    api_key: str = None,
    served_model_name: str = None,
    extra_args: list = None,
) -> dict:
    iid = instance_id or f"vllm-managed-{port}"
    # The vllm CLI of this process's environment (VLLM_BIN overrides): the bare
    # "python" this used to run had no vllm on most hosts. Nodes are launched
    # through provisioning instead (nodes.backend.switch).
    _bin = (os.environ.get("VLLM_BIN") or shutil.which("vllm")
            or os.path.join(os.path.dirname(sys.executable), "vllm"))
    if not os.path.exists(_bin):
        return {"ok": False, "error": "no vllm CLI on this host (set VLLM_BIN) - to run vLLM "
                                      "on a node use Launch on a node (nodes.backend.switch)"}
    _key = api_key or VLLM_API_KEY
    _cpu_off = cpu_offload_gb if cpu_offload_gb is not None else VLLM_CPU_OFFLOAD_GB
    _lora = enable_lora if enable_lora is not None else VLLM_ENABLE_LORA
    cmd = _install_core.local_serve_argv(
        model, vllm_bin=_bin, port=port, host=host,
        tensor_parallel_size=tensor_parallel_size or VLLM_TENSOR_PAR,
        gpu_memory_utilization=gpu_memory_utilization or VLLM_GPU_MEM_UTIL,
        dtype=dtype or VLLM_DTYPE, cpu_offload_gb=_cpu_off or 0,
        quantization=quantization or VLLM_QUANTIZATION,
        max_model_len=max_model_len or VLLM_MAX_MODEL_LEN or 0,
        spec_model=spec_model or VLLM_SPEC_MODEL, spec_tokens=spec_num_speculative_tokens,
        enable_lora=bool(_lora), max_loras=max_loras or VLLM_MAX_LORAS,
        enable_prefix_caching=enable_prefix_caching,
        enable_chunked_prefill=enable_chunked_prefill,
        max_num_batched_tokens=max_num_batched_tokens or 0,
        api_key=_key or "", served_model_name=served_model_name or "",
        extra_args=extra_args)
    _shown = _install_core.redact_argv(cmd)

    log.info("vllm: launching server: %s", _shown)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        _MANAGED_PROCS[iid] = proc

        # Auto-register the instance
        url = f"http://127.0.0.1:{port}"
        inst = VLLMInstance(id=iid, url=url, label=f"Managed:{model}", has_gpu=True,
                            api_key=_key or "")
        inst.pid = proc.pid
        VLLM_INSTANCES[iid] = inst

        await emit_event({"type": "vllm.server.started", "id": iid, "pid": proc.pid, "cmd": _shown})
        return {
            "ok": True,
            "instance_id": iid,
            "pid": proc.pid,
            "url": url,
            "cmd": _shown,
            "note": "Server starting. Health checks will begin in ~15s.",
        }
    except FileNotFoundError:
        return {"ok": False, "error": "vllm not found. Install with: pip install vllm"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@capability(
    name="vllm.server.stop",
    description="Stop a managed vLLM server subprocess",
    http_method="POST",
    http_path="/vllm/server/stop",
    schema={"properties": {"instance_id": {"type": "string"}}, "required": ["instance_id"]},
)
async def vllm_server_stop(instance_id: str) -> dict:
    proc = _MANAGED_PROCS.get(instance_id)
    if not proc:
        return {"ok": False, "error": f"No managed process for '{instance_id}'"}

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    del _MANAGED_PROCS[instance_id]
    VLLM_INSTANCES.pop(instance_id, None)
    await emit_event({"type": "vllm.server.stopped", "id": instance_id})
    return {"ok": True, "instance_id": instance_id}


# ══════════════════════════════════════════════════════════════════════════════
# OpenAI-compatible passthrough proxy
# ══════════════════════════════════════════════════════════════════════════════

from fastapi import Request as _Req
from fastapi.responses import StreamingResponse as _SR, JSONResponse as _JR

@APP.api_route("/vllm/proxy/{instance_id}/{path:path}", methods=["GET", "POST", "DELETE"])
async def _vllm_proxy(instance_id: str, path: str, request: _Req):
    """
    Transparent reverse proxy to any vLLM instance.
    Clients can target http://vera:8000/vllm/proxy/gpu-0/v1/chat/completions
    and benefit from Vera's routing, auth, and observability.
    """
    inst = VLLM_INSTANCES.get(instance_id)
    if not inst:
        return _JR({"error": f"Instance '{instance_id}' not found"}, status_code=404)

    body = await request.body()
    headers = dict(inst.headers())
    headers.pop("Content-Type", None)
    # httpx sets no Content-Type for raw `content=`, and vLLM refuses a body
    # without application/json (every proxied POST failed with 400, 5 Oct 2026).
    if body:
        headers["Content-Type"] = request.headers.get("content-type") or "application/json"

    url = f"{inst.url}/{path}"
    if request.query_params:
        url += "?" + str(request.query_params)

    is_stream = False
    if body:
        try:
            parsed = json.loads(body)
            is_stream = parsed.get("stream", False)
        except Exception:
            pass

    inst.in_use += 1
    try:
        if is_stream:
            async def _gen():
                try:
                    async with httpx.AsyncClient(timeout=_req_timeout()) as c:
                        async with c.stream(request.method, url, content=body, headers=headers) as r:
                            async for chunk in r.aiter_bytes():
                                yield chunk
                finally:
                    inst.in_use = max(0, inst.in_use - 1)

            return _SR(_gen(), media_type="text/event-stream")
        else:
            async with httpx.AsyncClient(timeout=_req_timeout()) as c:
                r = await c.request(request.method, url, content=body, headers=headers)
            inst.in_use = max(0, inst.in_use - 1)
            return _JR(r.json(), status_code=r.status_code)
    except Exception as exc:
        inst.in_use = max(0, inst.in_use - 1)
        return _JR({"error": str(exc)}, status_code=502)


# ══════════════════════════════════════════════════════════════════════════════
# UI Panel
# ══════════════════════════════════════════════════════════════════════════════

import contextlib as _ctx
from pathlib import Path as _Path

_VLLM_PANEL_PATH = _Path(__file__).parent / "vllm_panel.html"


@APP.get("/vllm/panel", include_in_schema=False)
async def _vllm_panel_html():
    """Serve the vLLM panel as a standalone page for iframe embedding.

    The panel ships its own ``<script>`` blocks and relies on ``DOMContentLoaded``
    + relative ``fetch('/vllm/...')`` calls. Injecting that full document into the
    harness via ``innerHTML`` (the old ``register_ui(mode="tab")`` path) silently
    drops every script — the panel renders but nothing is clickable. Serving it
    standalone here, loaded in an iframe, runs the scripts in their own window and
    resolves fetches against this origin. The vLLM tab now lives inside the
    Workers & Ollama panel, which iframes this route.
    """
    from fastapi.responses import HTMLResponse as _HTMLResp
    if _VLLM_PANEL_PATH.exists():
        return _HTMLResp(_VLLM_PANEL_PATH.read_text(encoding="utf-8"))
    return _HTMLResp("<p style='color:#c96b6b'>vllm_panel.html not found</p>",
                     status_code=404)

# ── Start health loop ──────────────────────────────────────────────────────────
# A one-time startup (the loop runs forever) under a name node workers run too:
# it is also what loads the registry from Redis, so a worker that skipped it
# knew no vLLM server and sent every offloaded vLLM call to Ollama instead.
schedule(_health_loop, interval=999999, name="vllm_health")
log.info("vllm: module loaded — %d instance(s) configured", len(VLLM_INSTANCES))