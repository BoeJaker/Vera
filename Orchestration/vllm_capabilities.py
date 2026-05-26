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
  vllm.status           — cluster health summary
  vllm.instances.list   — list all configured instances
  vllm.instances.add    — add a new instance at runtime
  vllm.instances.remove — remove an instance
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
import subprocess
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import httpx

import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import (
    APP,
    capability,
    emit_event,
    now_iso,
    register_ui,
    schedule,
)
from Vera.Orchestration.config import cfg

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
        )
        VLLM_INSTANCES[iid] = inst
        log.info("vllm: registered instance %s → %s", iid, inst.url)

_init_instances()


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


async def _health_loop(interval: float = 15.0) -> None:
    while True:
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
        async with httpx.AsyncClient(timeout=180, headers=inst.headers()) as c:
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
                    return "".join(buf)
            else:
                r = await c.post(f"{inst.url}/v1/completions", json=body)
                r.raise_for_status()
                return r.json()["choices"][0]["text"]

    except Exception as exc:
        log.error("vllm_generate [%s]: %s", inst.id, exc)
        inst.errors += 1
        # Failover to next best
        fallback = pick_vllm_instance(prefer_gpu=prefer_gpu, model=mdl)
        if fallback and fallback.id != inst.id:
            log.info("vllm_generate: failing over to %s", fallback.id)
            body["stream"] = False
            try:
                async with httpx.AsyncClient(timeout=180, headers=fallback.headers()) as c:
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
    max_tokens: int = 512,
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
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "stream": stream_cb is not None,
    }
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

    inst.in_use += 1
    try:
        async with httpx.AsyncClient(timeout=180, headers=inst.headers()) as c:
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
        log.error("vllm_chat [%s]: %s", inst.id, exc)
        inst.errors += 1
        return {"error": str(exc)}
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
    description="Register a new vLLM server instance at runtime",
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
        },
        "required": ["id", "url"],
    },
)
async def vllm_instances_add(
    id: str, url: str, label: str = "", has_gpu: bool = True,
    priority: int = None, api_key: str = "",
) -> dict:
    if id in VLLM_INSTANCES:
        return {"ok": False, "error": f"Instance '{id}' already exists. Remove it first."}
    inst = VLLMInstance(
        id=id, url=url.rstrip("/"), label=label or id,
        has_gpu=has_gpu,
        priority=priority if priority is not None else len(VLLM_INSTANCES),
        api_key=api_key or VLLM_API_KEY,
    )
    VLLM_INSTANCES[id] = inst
    await _ping_vllm(inst)
    await emit_event({"type": "vllm.instance.added", "id": id, "url": url})
    return {"ok": True, "id": id, "status": inst.status, "models": inst.models}


@capability(
    name="vllm.instances.remove",
    description="Remove a vLLM instance from the registry",
    http_method="DELETE",
    http_path="/vllm/instances/{instance_id}",
    schema={"properties": {"instance_id": {"type": "string"}}, "required": ["instance_id"]},
)
async def vllm_instances_remove(instance_id: str) -> dict:
    if instance_id not in VLLM_INSTANCES:
        return {"ok": False, "error": f"Instance '{instance_id}' not found"}
    del VLLM_INSTANCES[instance_id]
    await emit_event({"type": "vllm.instance.removed", "id": instance_id})
    return {"ok": True, "id": instance_id}


@capability(
    name="vllm.models",
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
            "quantization":         {"type": "string", "description": "gptq | awq | fp8 | gguf | turbomind"},
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

    cmd = [
        "python", "-m", "vllm.entrypoints.openai.api_server",
        "--model", model,
        "--host", host,
        "--port", str(port),
        "--tensor-parallel-size", str(tensor_parallel_size or VLLM_TENSOR_PAR),
        "--gpu-memory-utilization", str(gpu_memory_utilization or VLLM_GPU_MEM_UTIL),
        "--dtype", dtype or VLLM_DTYPE,
    ]

    _cpu_off = cpu_offload_gb if cpu_offload_gb is not None else VLLM_CPU_OFFLOAD_GB
    if _cpu_off > 0:
        cmd += ["--cpu-offload-gb", str(_cpu_off)]

    _quant = quantization or VLLM_QUANTIZATION
    if _quant:
        cmd += ["--quantization", _quant]

    _mml = max_model_len or VLLM_MAX_MODEL_LEN
    if _mml:
        cmd += ["--max-model-len", str(_mml)]

    _spec = spec_model or VLLM_SPEC_MODEL
    if _spec:
        cmd += [
            "--speculative-model", _spec,
            "--num-speculative-tokens", str(spec_num_speculative_tokens),
        ]

    _lora = enable_lora if enable_lora is not None else VLLM_ENABLE_LORA
    if _lora:
        cmd += ["--enable-lora", "--max-loras", str(max_loras or VLLM_MAX_LORAS)]

    if enable_prefix_caching:
        cmd += ["--enable-prefix-caching"]

    if enable_chunked_prefill:
        cmd += ["--enable-chunked-prefill"]

    if max_num_batched_tokens:
        cmd += ["--max-num-batched-tokens", str(max_num_batched_tokens)]

    _key = api_key or VLLM_API_KEY
    if _key:
        cmd += ["--api-key", _key]

    if served_model_name:
        cmd += ["--served-model-name", served_model_name]

    if extra_args:
        cmd.extend(str(a) for a in extra_args)

    log.info("vllm: launching server: %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        _MANAGED_PROCS[iid] = proc

        # Auto-register the instance
        url = f"http://127.0.0.1:{port}"
        inst = VLLMInstance(id=iid, url=url, label=f"Managed:{model}", has_gpu=True,
                            api_key=_key or "")
        inst.pid = proc.pid
        VLLM_INSTANCES[iid] = inst

        await emit_event({"type": "vllm.server.started", "id": iid, "pid": proc.pid, "cmd": " ".join(cmd)})
        return {
            "ok": True,
            "instance_id": iid,
            "pid": proc.pid,
            "url": url,
            "cmd": " ".join(cmd),
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
    headers.pop("Content-Type", None)   # let httpx set it

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
                    async with httpx.AsyncClient(timeout=180) as c:
                        async with c.stream(request.method, url, content=body, headers=headers) as r:
                            async for chunk in r.aiter_bytes():
                                yield chunk
                finally:
                    inst.in_use = max(0, inst.in_use - 1)

            return _SR(_gen(), media_type="text/event-stream")
        else:
            async with httpx.AsyncClient(timeout=180) as c:
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

try:
    _panel_path = __file__.replace("vllm_capabilities.py", "vllm_panel.html")
    with open(_panel_path, encoding="utf-8") as _fh:
        _PANEL_HTML = _fh.read()
except Exception as _e:
    _PANEL_HTML = f"<p style='color:var(--err)'>vllm_panel.html not found: {_e}</p>"

register_ui(
    panel_id="vllm",
    label="vLLM",
    icon="",
    html=_PANEL_HTML,
    mode="tab",
    tab_order=45,
    ui_caps=[
        "vllm.status", "vllm.models", "vllm.generate", "vllm.chat", "vllm.embed",
        "vllm.instances.add", "vllm.instances.remove",
        "vllm.lora.load", "vllm.lora.list",
        "vllm.metrics", "vllm.server.start", "vllm.server.stop",
    ],
)

# ── Start health loop ──────────────────────────────────────────────────────────
schedule(_health_loop, interval=0)
log.info("vllm: module loaded — %d instance(s) configured", len(VLLM_INSTANCES))