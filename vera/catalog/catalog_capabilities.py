"""
catalog_capabilities.py  —  Hugging Face Model Catalog + hardware-aware routing
================================================================================
Add to _module_files in capability_orchestration.py:
    os.path.join(_here, "catalog/catalog_capabilities.py"),

Gives Vera a model DISCOVERY + hardware-fit layer on top of the existing routing
engine.  Nothing here re-implements routing or install — every side-effecting
cap delegates to caps that already exist:

    ollama.pull / pxstore.models.pull      → install GGUF onto an Ollama node
    vllm.server.start                      → launch an HF repo on a vLLM node
    ollama.role_profiles.save / cap_routing.save → repoint a role/cap at a model
    exec.ssh.run                           → detect per-node VRAM/RAM/GPU

What this module adds:
  • Hugging Face browse:  catalog.search / catalog.model  (public HF API)
  • Hardware sizing:      _estimate_requirements(params_b, quant, ctx)
  • Node inventory:       catalog.nodes / node.detect / node.hw_set / node.ssh_set
  • Install:              catalog.install.plan / catalog.install
  • Swap / pin:           catalog.route.set_model / catalog.node.mark_quality
  • Auto-optimise:        catalog.optimize.suggest / apply + opt-in scheduled loop

Per-node hardware lives in Redis (vera:catalog:node_hw); the node→SSH-host map
(vera:catalog:node_ssh) points at the canonical exec.ssh.hosts.* store.
"""
from __future__ import annotations
import asyncio, json, logging, os, re, socket, sys, time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, now_iso, schedule, register_routing_profile,
)
import Vera.vera.capability_orchestration as _orch

log = logging.getLogger("vera.catalog")

HF_API = "https://huggingface.co/api"
HF_HOST = "https://huggingface.co"

# Redis keys
KEY_NODE_HW  = "vera:catalog:node_hw"    # {iid: {vram_gb,ram_gb,gpu_name,gpu_count,cpu_cores,class,source,detected_at}}
KEY_NODE_SSH = "vera:catalog:node_ssh"   # {iid: ssh_host_id}
KEY_AUTOOPT  = "vera:catalog:autoopt"    # {enabled_nodes:[], roles:[], interval_min, backend}
KEY_AUTOLOG  = "vera:catalog:autoopt:log"  # JSON list, newest-last, capped

# In-memory caches (hydrated lazily from Redis)
NODE_HW:  Dict[str, dict] = {}
NODE_SSH: Dict[str, str]  = {}
AUTOOPT:  dict = {"enabled_nodes": [], "roles": [], "interval_min": 1440, "backend": "ollama"}
_HYDRATED = {"v": False, "busy": False}


# ─────────────────────────────────────────────────────────────────────────────
# LAZY BACKEND ACCESS  (mirror of the _cap() pattern used elsewhere)
# ─────────────────────────────────────────────────────────────────────────────
def _cap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("func") if c else None


def _redis():
    return getattr(_orch, "REDIS", None)


async def _hydrate() -> None:
    # Redis comes up a beat after the HTTP server does, and the Downloads tab
    # starts polling immediately. Latching "hydrated" before that check — as this
    # did — meant one poll landing in that window permanently skipped the
    # restore, the orphan reap and the download auto-resume for the whole life of
    # the process. Only latch once there is actually a Redis to read from.
    if _HYDRATED["v"] or _HYDRATED["busy"]:
        return
    r = _redis()
    if not r:
        return                       # not ready yet — the next call retries
    _HYDRATED["busy"] = True
    try:
        await _hydrate_from(r)
        _HYDRATED["v"] = True
    finally:
        _HYDRATED["busy"] = False


async def _hydrate_from(r) -> None:
    for key, target, is_list in ((KEY_NODE_HW, NODE_HW, False),
                                 (KEY_NODE_SSH, NODE_SSH, False)):
        try:
            raw = await r.get(key)
            if raw:
                doc = json.loads(raw)
                if isinstance(doc, dict):
                    target.update(doc)
        except Exception as e:
            log.debug("hydrate %s: %s", key, e)
    try:
        raw = await r.get(KEY_AUTOOPT)
        if raw:
            doc = json.loads(raw)
            if isinstance(doc, dict):
                AUTOOPT.update(doc)
    except Exception as e:
        log.debug("hydrate autoopt: %s", e)
    # Downloads do NOT survive a Vera restart (the /api/pull stream is ours), so
    # retire the records a previous process left mid-flight, then put them back.
    await _reap_stale_pulls()
    # Off the request path: resuming re-runs the registry preflight per download,
    # and _hydrate() is awaited by the status poll the Downloads tab makes every
    # couple of seconds. Blocking it would stall the UI on the first call.
    _t = asyncio.create_task(_autoresume_pulls())
    _BG_TASKS.add(_t)
    _t.add_done_callback(_BG_TASKS.discard)


async def _persist(key: str, obj) -> None:
    r = _redis()
    if not r:
        return
    try:
        await r.set(key, json.dumps(obj))
    except Exception as e:
        log.warning("persist %s: %s", key, e)


def _hf_headers() -> dict:
    tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN") or ""
    return {"Authorization": f"Bearer {tok}"} if tok else {}


# ─────────────────────────────────────────────────────────────────────────────
# PARAMETER-COUNT + GGUF-QUANT PARSING
# ─────────────────────────────────────────────────────────────────────────────
_PARAM_RE = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*[bB](?![A-Za-z])")
_MOE_RE   = re.compile(r"(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*[bB]", re.I)
# GGUF quant token in a filename, e.g. model.Q4_K_M.gguf / model-IQ3_XS.gguf
_QUANT_RE = re.compile(r"[.\-]((?:IQ|Q|F|BF)\d[\w]*?|F16|BF16|F32)\.gguf$", re.I)
_SHARD_RE = re.compile(r"-\d{5}-of-\d{5}")


def _params_from_id(model_id: str) -> Optional[float]:
    """Best-effort billions-of-params from a repo id / filename."""
    m = _MOE_RE.search(model_id)
    if m:  # MoE like 8x7B — report total params (experts × size)
        try:
            return round(int(m.group(1)) * float(m.group(2)), 1)
        except Exception:
            pass
    hits = _PARAM_RE.findall(model_id)
    if hits:
        try:
            # take the largest plausible number (skip stray "1b"-style version tags)
            vals = [float(h) for h in hits if 0.1 <= float(h) <= 2000]
            if vals:
                return round(max(vals), 1)
        except Exception:
            pass
    return None


def _quant_of(filename: str) -> Optional[str]:
    m = _QUANT_RE.search(filename)
    if m:
        return m.group(1).upper()
    low = filename.lower()
    for tok in ("awq", "gptq", "fp8"):
        if tok in low:
            return tok.upper()
    return None


# ─────────────────────────────────────────────────────────────────────────────
# HARDWARE SIZING  —  clearly-labelled estimates, not guarantees
# ─────────────────────────────────────────────────────────────────────────────
# GB of on-device weights per BILLION parameters, by quantisation.
_QUANT_GB_PER_B = {
    "Q2_K": 0.42, "IQ2": 0.36, "Q3_K_S": 0.46, "Q3_K_M": 0.50, "Q3_K_L": 0.54,
    "Q3_K": 0.50, "IQ3": 0.44,
    "Q4_0": 0.56, "Q4_1": 0.60, "Q4_K_S": 0.57, "Q4_K_M": 0.63, "Q4_K": 0.63, "IQ4": 0.53,
    "Q5_0": 0.68, "Q5_1": 0.72, "Q5_K_S": 0.69, "Q5_K_M": 0.73, "Q5_K": 0.73,
    "Q6_K": 0.82, "Q8_0": 1.06,
    "F16": 2.0, "FP16": 2.0, "BF16": 2.0, "F32": 4.0, "FP32": 4.0,
    "AWQ": 0.55, "GPTQ": 0.55, "FP8": 1.0,
}
_DEFAULT_GB_PER_B = 0.65


def _gb_per_b(quant: Optional[str]) -> float:
    if not quant:
        return _DEFAULT_GB_PER_B
    q = quant.upper()
    if q in _QUANT_GB_PER_B:
        return _QUANT_GB_PER_B[q]
    # prefix match (Q4_K_M → Q4_K → Q4)
    for k in sorted(_QUANT_GB_PER_B, key=len, reverse=True):
        if q.startswith(k):
            return _QUANT_GB_PER_B[k]
    return _DEFAULT_GB_PER_B


def _estimate_requirements(params_b: Optional[float], quant: Optional[str],
                           ctx: int = 4096) -> dict:
    """VRAM/RAM estimate for a model of `params_b` billion params at `quant`.
    Returns GB figures + a note.  Everything is an ESTIMATE."""
    if not params_b or params_b <= 0:
        return {"known": False, "note": "parameter count unknown — estimate unavailable"}
    weights = params_b * _gb_per_b(quant)
    # KV-cache grows with context; rough fp16 approximation scaled by model size.
    kv = params_b * (max(ctx, 512) / 4096.0) * 0.12
    overhead = 0.8
    total = weights + kv + overhead
    return {
        "known": True,
        "params_b": round(params_b, 1),
        "quant": quant or "(unknown)",
        "ctx": ctx,
        "weights_gb": round(weights, 1),
        "kv_gb": round(kv, 1),
        "overhead_gb": overhead,
        "min_vram_gb": round(total, 1),      # to run fully on GPU
        "min_ram_gb": round(total + 1.0, 1), # CPU/offload path
        "note": "estimate (±20%)",
    }


# Rough tokens/sec by size band × device — refined by observed route stats below.
_TPS_BANDS = [
    (3,   {"gpu": 95, "cpu": 15}),
    (8,   {"gpu": 58, "cpu": 7}),
    (15,  {"gpu": 36, "cpu": 4}),
    (35,  {"gpu": 18, "cpu": 1.6}),
    (75,  {"gpu": 9,  "cpu": 0.7}),
    (1e9, {"gpu": 4,  "cpu": 0.3}),
]


def _estimate_throughput(params_b: Optional[float], has_gpu: bool) -> dict:
    if not params_b or params_b <= 0:
        return {"known": False}
    dev = "gpu" if has_gpu else "cpu"
    base = next(b[dev] for cutoff, b in _TPS_BANDS if params_b <= cutoff)
    # Refine with observed EMA tps for a same-band model, if we have any.
    observed = None
    try:
        stats = getattr(_orch, "_ROUTE_STATS", {}) or {}
        band = next(i for i, (c, _) in enumerate(_TPS_BANDS) if params_b <= c)
        samples = []
        for key, s in stats.items():
            mp = _params_from_id(str(key))
            if mp is None:
                continue
            if next(i for i, (c, _) in enumerate(_TPS_BANDS) if mp <= c) == band:
                tps = s.get("tps") if isinstance(s, dict) else None
                if tps:
                    samples.append(tps)
        if samples:
            observed = round(sum(samples) / len(samples), 1)
    except Exception:
        pass
    return {"known": True, "device": dev,
            "est_tps": observed or base,
            "source": "observed" if observed else "heuristic",
            "note": "tokens/sec estimate"}


def _best_gpu_hw() -> dict:
    best = {"vram_gb": 0.0, "ram_gb": 0.0, "gpu_name": ""}
    for iid, hw in NODE_HW.items():
        if (hw.get("vram_gb") or 0) > best["vram_gb"]:
            best = {"vram_gb": hw.get("vram_gb", 0.0), "ram_gb": hw.get("ram_gb", 0.0),
                    "gpu_name": hw.get("gpu_name", ""), "iid": iid}
    return best


def _best_cpu_ram() -> float:
    return max([hw.get("ram_gb") or 0.0 for hw in NODE_HW.values()] or [0.0])


def _fit(req: dict, node_hw: dict) -> dict:
    """Does `req` (from _estimate_requirements) fit `node_hw`?"""
    if not req.get("known"):
        return {"verdict": "unknown"}
    vram = node_hw.get("vram_gb") or 0.0
    ram  = node_hw.get("ram_gb") or 0.0
    if vram <= 0 and ram <= 0:
        # Nothing detected for this node — "unknown", never "too big". Claiming
        # a 1B model doesn't fit because we never probed the hardware is worse
        # than saying so.
        return {"verdict": "unknown", "reason": "hardware not detected"}
    gpu_ok = vram >= req["min_vram_gb"] and vram > 0
    cpu_ok = ram >= req["min_ram_gb"]
    if gpu_ok:
        return {"verdict": "gpu", "headroom_gb": round(vram - req["min_vram_gb"], 1)}
    if cpu_ok:
        return {"verdict": "cpu", "headroom_gb": round(ram - req["min_ram_gb"], 1)}
    return {"verdict": "no", "short_gb": round(req["min_vram_gb"] - vram, 1)}


def _fit_target(req: dict, fits: str) -> dict:
    """`fits` = a node id, 'cluster' (best node), or 'any' (no filter)."""
    if fits in ("", "any"):
        return {"verdict": "any"}
    if fits == "cluster":
        g = _best_gpu_hw()
        node_hw = {"vram_gb": g["vram_gb"], "ram_gb": max(g["ram_gb"], _best_cpu_ram())}
        return _fit(req, node_hw)
    return _fit(req, NODE_HW.get(fits, {}))


# ─────────────────────────────────────────────────────────────────────────────
# HUGGING FACE BROWSE
# ─────────────────────────────────────────────────────────────────────────────
@capability("catalog.search", memory="off", silent=True,
            http_method="GET", http_path="/catalog/search", http_tags=["catalog"],
            description="Search Hugging Face models. Query: search (str — include the "
                        "publisher for quant repos, e.g. 'bartowski Qwen3.6'; HF ranks by "
                        "relevance, so a bare model name buries them), filter (str — HF "
                        "tag, default 'gguf' = pullable; do NOT use 'text-generation', "
                        "which hides multimodal repos tagged image-text-to-text and the "
                        "many quant repos with no pipeline tag), sort "
                        "(downloads|likes|lastModified|trendingScore, default downloads), "
                        "limit (int, default 60, max 100 — HF's ceiling), fits "
                        "(node_id|cluster|any — annotate "
                        "each result with a hardware-fit verdict), quant (str — quant to "
                        "assume for the fit estimate, default Q4_K_M). Returns normalised "
                        "rows with an estimated parameter count + fit badge.")
async def cap_catalog_search(search: str = "", filter: str = "gguf",
                             sort: str = "downloads", limit: int = 60,
                             fits: str = "any", quant: str = "Q4_K_M",
                             author: str = "", trace_id=None):
    await _hydrate()
    rows = await _hf_models(search=search, filt=filter, sort=sort, limit=limit,
                            author=author, fits=fits, quant=quant)
    if isinstance(rows, dict):
        return rows
    return {"results": rows, "count": len(rows), "fits": fits, "quant": quant}


async def _hf_models(search: str = "", filt: str = "", sort: str = "downloads",
                     limit: int = 30, author: str = "", fits: str = "any",
                     quant: str = "Q4_K_M"):
    """One normalised HF /api/models query. Returns rows, or an error dict."""
    params = {
        "limit": max(1, min(int(limit or 30), 100)),
        "sort": sort or "downloads",
        "direction": "-1",
        "full": "true",
    }
    if search:
        params["search"] = search
    if filt:
        params["filter"] = filt
    if author:
        params["author"] = author
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.get(f"{HF_API}/models", params=params, headers=_hf_headers())
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return {"error": f"HF query failed: {e}", "results": []}

    rows = []
    for m in data if isinstance(data, list) else []:
        mid = m.get("id") or m.get("modelId") or ""
        if not mid:
            continue
        params_b = None
        st = m.get("safetensors") or {}
        if isinstance(st, dict) and st.get("total"):
            try:
                params_b = round(float(st["total"]) / 1e9, 1)
            except Exception:
                params_b = None
        if params_b is None:
            params_b = _params_from_id(mid)
        req = _estimate_requirements(params_b, quant)
        tags = [t for t in (m.get("tags") or []) if isinstance(t, str)]
        rows.append({
            "id": mid,
            "author": mid.split("/")[0] if "/" in mid else "",
            "downloads": m.get("downloads", 0),
            "likes": m.get("likes", 0),
            "lastModified": m.get("lastModified", ""),
            "pipeline_tag": m.get("pipeline_tag", ""),
            "gated": m.get("gated", False),
            "tags": tags[:12],
            "gguf": "gguf" in [t.lower() for t in tags],
            "est_params_b": params_b,
            "requirement": req,
            "fit": _fit_target(req, fits),
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# BROWSE  —  curated entry points, so you can look around without knowing what
# to type. Families/publishers are search shortcuts, not a hard-coded catalogue:
# every tile just parameterises the same HF query, so new releases show up on
# their own.
# ─────────────────────────────────────────────────────────────────────────────
FAMILIES: List[dict] = [
    {"key": "qwen",      "label": "Qwen",        "search": "Qwen",
     "blurb": "Alibaba · strong all-round + coder variants, 0.5B→235B"},
    {"key": "llama",     "label": "Llama",       "search": "Llama",
     "blurb": "Meta · the default general-purpose family"},
    {"key": "mistral",   "label": "Mistral",     "search": "Mistral",
     "blurb": "Mistral AI · efficient dense + MoE models"},
    {"key": "gemma",     "label": "Gemma",       "search": "gemma",
     "blurb": "Google · small, punchy, good on modest GPUs"},
    {"key": "phi",       "label": "Phi",         "search": "phi",
     "blurb": "Microsoft · tiny models that punch above their weight"},
    {"key": "deepseek",  "label": "DeepSeek",    "search": "DeepSeek",
     "blurb": "Reasoning + coding specialists, incl. distills"},
    {"key": "glm",       "label": "GLM",         "search": "GLM",
     "blurb": "Zhipu · bilingual general + agentic models"},
    {"key": "command-r", "label": "Command-R",   "search": "command-r",
     "blurb": "Cohere · RAG and tool-use oriented"},
    {"key": "granite",   "label": "Granite",     "search": "granite",
     "blurb": "IBM · enterprise/code models, permissive licences"},
    {"key": "nemotron",  "label": "Nemotron",    "search": "Nemotron",
     "blurb": "NVIDIA · tuned for instruction following + reward"},
    {"key": "gpt-oss",   "label": "gpt-oss",     "search": "gpt-oss",
     "blurb": "OpenAI open-weight models + community quants"},
    {"key": "smol",      "label": "SmolLM",      "search": "SmolLM",
     "blurb": "HF · very small models for CPU nodes"},
    {"key": "coder",     "label": "Code models", "search": "coder",
     "blurb": "Code-specialised checkpoints across vendors"},
    {"key": "embed",     "label": "Embeddings",  "search": "embed",
     "task": "feature-extraction", "gguf": False,
     "blurb": "Embedding models for Fabric / memory / RAG"},
    {"key": "vision",    "label": "Vision · VLM", "search": "VL",
     "blurb": "Multimodal checkpoints (image understanding)"},
]

# Repos that publish reliable GGUF conversions — browsing by publisher is the
# fastest route to something Ollama can actually pull.
PUBLISHERS: List[dict] = [
    {"key": "bartowski", "label": "bartowski", "blurb": "Broad, fast GGUF quants of new releases"},
    {"key": "unsloth", "label": "unsloth", "blurb": "GGUF + dynamic quants, often day-one"},
    {"key": "lmstudio-community", "label": "lmstudio-community", "blurb": "Curated, well-tested quants"},
    {"key": "mradermacher", "label": "mradermacher", "blurb": "Huge coverage incl. imatrix quants"},
    {"key": "TheBloke", "label": "TheBloke", "blurb": "The classic archive (older models)"},
    {"key": "Qwen", "label": "Qwen (official)", "blurb": "First-party Qwen repos"},
    {"key": "google", "label": "google", "blurb": "First-party Gemma repos"},
    {"key": "meta-llama", "label": "meta-llama", "blurb": "First-party Llama repos (gated)"},
    {"key": "mistralai", "label": "mistralai", "blurb": "First-party Mistral repos"},
    {"key": "deepseek-ai", "label": "deepseek-ai", "blurb": "First-party DeepSeek repos"},
]

SIZE_BUCKETS: List[dict] = [
    {"key": "tiny",   "label": "≤ 4B",    "min_b": 0,   "max_b": 4,
     "blurb": "CPU-friendly · fast utility work"},
    {"key": "small",  "label": "5 – 9B",  "min_b": 4.1, "max_b": 9,
     "blurb": "The 8GB-VRAM sweet spot"},
    {"key": "medium", "label": "10 – 20B", "min_b": 9.1, "max_b": 20,
     "blurb": "Needs ~12–16GB VRAM at Q4"},
    {"key": "large",  "label": "21 – 40B", "min_b": 20.1, "max_b": 40,
     "blurb": "24GB+ GPUs, or slow-HQ CPU nodes"},
    {"key": "xl",     "label": "40B +",   "min_b": 40.1, "max_b": 10000,
     "blurb": "Multi-GPU, or patient offloaded inference"},
]

# Ollama's own library — plain `ollama pull <name>` refs. These need no HF repo
# resolution at all, which is the shortest path from browsing to a running model.
# Names + tags verified against ollama.com/library; cloud/MLX-only tags omitted.
OLLAMA_LIBRARY: List[dict] = [
    {"name": "qwen3.5", "tags": ["0.8b", "2b", "4b", "9b", "27b", "35b"],
     "blurb": "Qwen 3.5 — the current all-round default", "group": "general"},
    {"name": "qwen3", "tags": ["0.6b", "1.7b", "4b", "8b", "14b", "32b"],
     "blurb": "Qwen 3 general / thinking", "group": "general"},
    {"name": "gemma3", "tags": ["270m", "1b", "4b", "12b", "27b"],
     "blurb": "Google Gemma 3 — multimodal, strong per GB", "group": "general"},
    {"name": "llama3.3", "tags": ["70b"], "blurb": "Meta Llama 3.3 (big)", "group": "general"},
    {"name": "llama3.2", "tags": ["1b", "3b"], "blurb": "Small Llama — fine on CPU", "group": "general"},
    {"name": "mistral", "tags": ["7b"], "blurb": "Mistral 7B instruct", "group": "general"},
    {"name": "mistral-nemo", "tags": ["12b"], "blurb": "12B with long context", "group": "general"},
    {"name": "phi4", "tags": ["14b"], "blurb": "Microsoft Phi-4", "group": "general"},
    {"name": "phi4-mini", "tags": ["3.8b"], "blurb": "Phi-4 mini — tiny + capable", "group": "general"},
    {"name": "granite4", "tags": ["350m", "1b", "3b"], "blurb": "IBM Granite 4 — small, permissive", "group": "general"},
    {"name": "gpt-oss", "tags": ["20b", "120b"], "blurb": "OpenAI open-weight models", "group": "general"},
    {"name": "deepseek-r1", "tags": ["1.5b", "7b", "8b", "14b", "32b", "70b"],
     "blurb": "Reasoning distills across every size", "group": "reasoning"},
    {"name": "magistral", "tags": ["24b"], "blurb": "Mistral's reasoning model", "group": "reasoning"},
    {"name": "qwen3-coder", "tags": ["30b"], "blurb": "Agentic coding", "group": "code"},
    {"name": "qwen2.5-coder", "tags": ["1.5b", "7b", "14b", "32b"], "blurb": "Code completion + fixes", "group": "code"},
    {"name": "devstral", "tags": ["24b"], "blurb": "Codebase-agent tuned", "group": "code"},
    {"name": "codestral", "tags": ["22b"], "blurb": "Mistral's code model", "group": "code"},
    {"name": "nomic-embed-text", "tags": ["latest"], "blurb": "Vera's default embedding model", "group": "embed"},
    {"name": "embeddinggemma", "tags": ["300m"], "blurb": "Small, fast embeddings", "group": "embed"},
    {"name": "qwen3-embedding", "tags": ["0.6b", "4b", "8b"], "blurb": "Higher-quality embeddings", "group": "embed"},
    {"name": "qwen2.5vl", "tags": ["3b", "7b", "32b"], "blurb": "Vision-language", "group": "vision"},
    {"name": "llava", "tags": ["7b", "13b"], "blurb": "Classic vision + language", "group": "vision"},
    {"name": "minicpm-v", "tags": ["8b"], "blurb": "Compact multimodal", "group": "vision"},
]


@capability("catalog.browse.index", memory="off", silent=True,
            http_method="GET", http_path="/catalog/browse/index", http_tags=["catalog"],
            description="The browse entry points: model families, GGUF publishers, size "
                        "buckets (annotated with what the cluster can actually run) and "
                        "the Ollama library shortlist. Use with catalog.browse to list "
                        "models without knowing a search term.")
async def cap_catalog_browse_index(trace_id=None):
    await _hydrate()
    best = _best_gpu_hw()
    ram = _best_cpu_ram()
    buckets = []
    for b in SIZE_BUCKETS:
        mid = (b["min_b"] + min(b["max_b"], 80)) / 2 or 1
        req = _estimate_requirements(mid, "Q4_K_M")
        buckets.append({**b, "cluster_fit": _fit(req, {"vram_gb": best["vram_gb"],
                                                       "ram_gb": max(best["ram_gb"], ram)})})
    return {"families": FAMILIES, "publishers": PUBLISHERS, "sizes": buckets,
            "ollama_library": OLLAMA_LIBRARY,
            "cluster": {"best_gpu": best, "best_cpu_ram_gb": ram}}


@capability("catalog.browse", memory="off", silent=True,
            http_method="GET", http_path="/catalog/browse", http_tags=["catalog"],
            description="Browse Hugging Face models without a search term. Query: mode "
                        "(trending|popular|recent|family|publisher|size), family (str — a "
                        "key from catalog.browse.index), publisher (str — HF author), "
                        "min_b/max_b (float — parameter-count window for mode=size), gguf "
                        "(bool, default true — only Ollama-pullable repos), task (str — HF "
                        "pipeline tag when gguf is false), limit (int), fits "
                        "(node_id|cluster|any), quant (str). Returns the same annotated "
                        "rows as catalog.search.")
async def cap_catalog_browse(mode: str = "trending", family: str = "", publisher: str = "",
                             min_b: float = 0, max_b: float = 0, gguf: bool = True,
                             task: str = "text-generation", limit: int = 40,
                             fits: str = "any", quant: str = "Q4_K_M", trace_id=None):
    await _hydrate()
    fam = next((f for f in FAMILIES if f["key"] == family), None) if family else None
    if fam and fam.get("gguf") is False:
        gguf = False
        task = fam.get("task", task)
    filt = "gguf" if gguf else (task or "")
    sort = {"trending": "trendingScore", "popular": "downloads",
            "recent": "lastModified"}.get(mode, "downloads")
    search, author = "", ""
    fetch = int(limit or 40)
    if mode == "family":
        if not fam:
            return {"error": f"unknown family: {family}", "results": []}
        search = fam["search"]
    elif mode == "publisher":
        if not publisher:
            return {"error": "publisher required for mode=publisher", "results": []}
        author = publisher
    elif mode == "size":
        # Over-fetch, then window by estimated parameter count.
        fetch = 100
    rows = await _hf_models(search=search, filt=filt, sort=sort, limit=fetch,
                            author=author, fits=fits, quant=quant)
    if isinstance(rows, dict):
        return rows
    if mode == "size":
        lo, hi = float(min_b or 0), float(max_b or 1e9)
        rows = [r for r in rows if r.get("est_params_b") is not None
                and lo <= r["est_params_b"] <= hi][:int(limit or 40)]
    return {"results": rows, "count": len(rows), "mode": mode,
            "family": family, "publisher": publisher, "fits": fits, "quant": quant}


@capability("catalog.model", memory="off", silent=True,
            http_method="GET", http_path="/catalog/model", http_tags=["catalog"],
            description="Hugging Face model detail + FILE TREE with sizes. Query: id "
                        "(str! — 'author/repo'), fits (node_id|cluster|any). Returns the "
                        "file tree, detected GGUF quant variants (each with size + est "
                        "VRAM/RAM + fit + throughput), any full-precision safetensors "
                        "size, context length, and the model card summary.")
async def cap_catalog_model(id: str = "", fits: str = "any", trace_id=None):
    await _hydrate()
    mid = (id or "").strip()
    if not mid:
        return {"error": "id required"}
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.get(f"{HF_API}/models/{mid}", params={"blobs": "true"},
                            headers=_hf_headers())
            r.raise_for_status()
            info = r.json()
    except Exception as e:
        return {"error": f"HF model fetch failed: {e}"}

    siblings = info.get("siblings") or []
    tree = []
    for s in siblings:
        tree.append({"path": s.get("rfilename", ""), "size": s.get("size")})

    # parameter count
    params_b = None
    st = info.get("safetensors") or {}
    if isinstance(st, dict) and st.get("total"):
        try:
            params_b = round(float(st["total"]) / 1e9, 1)
        except Exception:
            params_b = None
    if params_b is None:
        params_b = _params_from_id(mid)

    # context length from config (best-effort, short timeout — many gguf repos lack it)
    ctx = 4096
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            cr = await c.get(f"{HF_HOST}/{mid}/raw/main/config.json", headers=_hf_headers())
            if cr.status_code == 200:
                cfg = cr.json()
                ctx = int(cfg.get("max_position_embeddings")
                          or cfg.get("n_positions") or ctx) or ctx
    except Exception:
        pass

    # GGUF quant variants: group sibling gguf files by quant token
    variants: Dict[str, dict] = {}
    for s in siblings:
        fn = s.get("rfilename", "")
        if not fn.lower().endswith(".gguf"):
            continue
        q = _quant_of(fn) or "UNKNOWN"
        v = variants.setdefault(q, {"quant": q, "size": 0, "files": 0, "sharded": False})
        v["size"] += (s.get("size") or 0)
        v["files"] += 1
        if _SHARD_RE.search(fn):
            v["sharded"] = True
    variant_rows = []
    for q, v in sorted(variants.items(), key=lambda kv: kv[1]["size"]):
        req = _estimate_requirements(params_b, q, ctx)
        variant_rows.append({
            **v,
            "size_gb": round((v["size"] or 0) / 1e9, 2),
            "requirement": req,
            "fit": _fit_target(req, fits),
            "throughput_gpu": _estimate_throughput(params_b, True),
            "throughput_cpu": _estimate_throughput(params_b, False),
            "ollama_ref": f"hf.co/{mid}:{q}" if q != "UNKNOWN" else f"hf.co/{mid}",
        })

    # full-precision (safetensors) footprint — the vLLM path
    saf_bytes = sum((s.get("size") or 0) for s in siblings
                    if s.get("rfilename", "").endswith(".safetensors"))
    full_req = _estimate_requirements(params_b, "F16", ctx) if params_b else {"known": False}
    safetensors = {
        "size_gb": round(saf_bytes / 1e9, 2),
        "requirement": full_req,
        "fit": _fit_target(full_req, fits),
        "vllm_capable": bool(saf_bytes),
    }
    card = info.get("cardData") or {}
    card_summary = card.get("summary", "") if isinstance(card, dict) else ""

    return {
        "id": mid,
        "author": mid.split("/")[0] if "/" in mid else "",
        "pipeline_tag": info.get("pipeline_tag", ""),
        "gated": info.get("gated", False),
        "downloads": info.get("downloads", 0),
        "likes": info.get("likes", 0),
        "lastModified": info.get("lastModified", ""),
        "tags": [t for t in (info.get("tags") or []) if isinstance(t, str)],
        "est_params_b": params_b,
        "context_length": ctx,
        "tree": sorted(tree, key=lambda x: x["path"]),
        "gguf_variants": variant_rows,
        "safetensors": safetensors,
        "card_summary": card_summary,
    }


# ─────────────────────────────────────────────────────────────────────────────
# NODE HARDWARE INVENTORY  (auto-detect over SSH + manual override)
# ─────────────────────────────────────────────────────────────────────────────
def _vllm_instances() -> dict:
    mod = sys.modules.get("vllm_capabilities")
    return getattr(mod, "VLLM_INSTANCES", {}) if mod else {}


def _all_nodes() -> Dict[str, dict]:
    """Unified {iid: {backend,label,url,has_gpu,status,models[]}} across Ollama+vLLM."""
    out: Dict[str, dict] = {}
    for iid, i in getattr(_orch, "OLLAMA_INSTANCES", {}).items():
        out[iid] = {"backend": "ollama", "label": i.get("label", iid),
                    "url": i.get("url", ""), "has_gpu": i.get("has_gpu", False),
                    "enabled": i.get("enabled", True), "status": i.get("status", ""),
                    "priority": i.get("priority", 0), "models": i.get("models", []),
                    "num_ctx": i.get("num_ctx", 4096)}
    for iid, inst in _vllm_instances().items():
        out[iid] = {"backend": "vllm",
                    "label": getattr(inst, "label", iid),
                    "url": getattr(inst, "url", ""),
                    "has_gpu": getattr(inst, "has_gpu", True),
                    "enabled": getattr(inst, "enabled", True),
                    "status": getattr(inst, "status", ""),
                    "models": getattr(inst, "models", []) or [],
                    "num_ctx": getattr(inst, "num_ctx", 4096)}
    return out


def _node_view(iid: str, base: dict) -> dict:
    hw = NODE_HW.get(iid, {})
    return {
        "id": iid, **base,
        "ssh_host": NODE_SSH.get(iid, ""),
        "vram_gb": hw.get("vram_gb"), "ram_gb": hw.get("ram_gb"),
        "gpu_name": hw.get("gpu_name", ""), "gpu_count": hw.get("gpu_count"),
        "cpu_cores": hw.get("cpu_cores"),
        "hw_source": hw.get("source", ""), "detected_at": hw.get("detected_at", ""),
        "class": hw.get("class", ""),        # "slow-hq" etc.
        "pinned_model": hw.get("pinned_model", ""),
    }


async def _node_view_full(iid: str, base: dict) -> dict:
    """_node_view plus disk figures and an SSH-host suggestion for unmapped nodes."""
    v = _node_view(iid, base)
    v.update(await _node_disk(iid))
    v["ssh_host_suggest"] = await _ssh_host_suggest(iid)
    return v


@capability("catalog.nodes", memory="off", silent=True,
            http_method="GET", http_path="/catalog/nodes", http_tags=["catalog"],
            description="Unified per-node view for the catalog: Ollama + vLLM nodes with "
                        "their detected/overridden hardware (VRAM/RAM/GPU), free DISK "
                        "(root fs + the filesystem holding the Ollama model store), SSH "
                        "mapping, installed models, and any 'slow-hq' class + pinned model.")
async def cap_catalog_nodes(trace_id=None):
    await _hydrate()
    nodes = [await _node_view_full(iid, base) for iid, base in _all_nodes().items()]
    nodes.sort(key=lambda n: (n["backend"], -(n.get("vram_gb") or 0), n["id"]))
    return {"nodes": nodes,
            "cluster": {"best_gpu": _best_gpu_hw(), "best_cpu_ram_gb": _best_cpu_ram()}}


# Root filesystem + the filesystem that actually holds the Ollama blobs. Emitted
# as pipe-delimited lines so a partial/garbled response still parses cleanly.
_DISK_SCRIPT = r"""
df -P -B1G / 2>/dev/null | awk 'NR==2{print "DISK|"$2"|"$4}'
for p in "$OLLAMA_MODELS" /usr/share/ollama/.ollama/models /var/lib/ollama/.ollama/models \
         "$HOME/.ollama/models" /root/.ollama/models /mnt/models/ollama; do
  [ -n "$p" ] && [ -d "$p" ] || continue
  echo "MPATH|$p"
  df -P -B1G "$p" 2>/dev/null | awk 'NR==2{print "MDISK|"$2"|"$4}'
  du -s -B1G "$p" 2>/dev/null | awk '{print "MUSED|"$1}'
  break
done
"""


def _parse_disk(txt: str) -> dict:
    """Parse _DISK_SCRIPT output into disk_* fields (all GB, all best-effort)."""
    out: Dict[str, Any] = {}
    for ln in (txt or "").splitlines():
        k, _, v = ln.strip().partition("|")
        try:
            if k == "DISK":
                tot, _, free = v.partition("|")
                out["disk_total_gb"] = round(float(re.sub(r"[^\d.]", "", tot) or 0), 1)
                out["disk_free_gb"] = round(float(re.sub(r"[^\d.]", "", free) or 0), 1)
            elif k == "MPATH":
                out["models_path"] = v.strip()
            elif k == "MDISK":
                tot, _, free = v.partition("|")
                out["models_disk_total_gb"] = round(float(re.sub(r"[^\d.]", "", tot) or 0), 1)
                out["models_disk_free_gb"] = round(float(re.sub(r"[^\d.]", "", free) or 0), 1)
            elif k == "MUSED":
                out["models_dir_gb"] = round(float(re.sub(r"[^\d.]", "", v) or 0), 1)
        except Exception:
            continue
    return out


# nodes.list is a pure registry merge (no SSH), but it is not free — cache it
# briefly so per-node disk lookups during a render don't rebuild it N times.
_NODES_CACHE = {"at": 0.0, "rows": []}


async def _estate_nodes() -> List[dict]:
    if time.time() - _NODES_CACHE["at"] < 30 and _NODES_CACHE["rows"]:
        return _NODES_CACHE["rows"]
    fn = _cap("nodes.list")
    rows = []
    if fn:
        try:
            rows = (await fn() or {}).get("nodes", []) or []
        except Exception as e:
            log.debug("estate nodes: %s", e)
    _NODES_CACHE.update(at=time.time(), rows=rows)
    return rows


async def _estate_lookup(iid: str) -> dict:
    """The unified-estate node row that owns this Ollama/vLLM instance id."""
    for n in await _estate_nodes():
        for rt in (n.get("ollama") or []) + (n.get("vllm") or []):
            if rt.get("id") == iid:
                return n
    return {}


async def _node_disk(iid: str) -> dict:
    """Disk figures for a node: catalog's own SSH probe first, else the estate's
    cached detection facts (nodes.detect). Returns {} when nothing is known."""
    hw = NODE_HW.get(iid, {})
    keys = ("disk_total_gb", "disk_free_gb", "models_path",
            "models_disk_total_gb", "models_disk_free_gb", "models_dir_gb")
    own = {k: hw[k] for k in keys if hw.get(k) is not None}
    if own.get("disk_free_gb") is not None or own.get("models_disk_free_gb") is not None:
        return {**own, "disk_source": hw.get("source", "auto")}
    node = await _estate_lookup(iid)
    f = node.get("facts") or {}
    fallback = {k: f[k] for k in ("disk_total_gb", "disk_free_gb") if f.get(k) is not None}
    if fallback:
        return {**fallback, "disk_source": "nodes.detect"}
    return {}


async def _ssh_host_suggest(iid: str) -> str:
    """A plausible SSH host for an unmapped node, from the unified estate."""
    if NODE_SSH.get(iid):
        return ""
    return (await _estate_lookup(iid)).get("ssh_host_id", "") or ""


async def _detect_hw(host_id: str) -> dict:
    """Probe VRAM/RAM/GPU/cores/disk on an SSH host. Best-effort; missing tools tolerated."""
    ssh = _cap("exec.ssh.run")
    if not ssh:
        return {"error": "exec.ssh.run unavailable"}
    out = {}
    # GPU
    try:
        r = await ssh(command="nvidia-smi --query-gpu=name,memory.total "
                              "--format=csv,noheader,nounits", host_id=host_id, timeout=20)
        txt = (r or {}).get("stdout", "") if isinstance(r, dict) else ""
        lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
        if lines:
            names, total_mib = [], 0.0
            for ln in lines:
                parts = [p.strip() for p in ln.split(",")]
                if parts:
                    names.append(parts[0])
                if len(parts) > 1:
                    try:
                        total_mib += float(re.sub(r"[^\d.]", "", parts[1]) or 0)
                    except Exception:
                        pass
            out["gpu_name"] = names[0] if names else ""
            out["gpu_count"] = len(lines)
            out["vram_gb"] = round(total_mib / 1024.0, 1)
    except Exception as e:
        log.debug("detect gpu %s: %s", host_id, e)
    # RAM
    try:
        r = await ssh(command="free -m | awk '/^Mem:/{print $2}'", host_id=host_id, timeout=15)
        txt = (r or {}).get("stdout", "").strip() if isinstance(r, dict) else ""
        if txt:
            out["ram_gb"] = round(float(re.sub(r"[^\d.]", "", txt) or 0) / 1024.0, 1)
    except Exception as e:
        log.debug("detect ram %s: %s", host_id, e)
    # CPU cores
    try:
        r = await ssh(command="nproc", host_id=host_id, timeout=15)
        txt = (r or {}).get("stdout", "").strip() if isinstance(r, dict) else ""
        if txt:
            out["cpu_cores"] = int(re.sub(r"[^\d]", "", txt) or 0)
    except Exception as e:
        log.debug("detect nproc %s: %s", host_id, e)
    # Disk — root filesystem AND the filesystem holding the Ollama model store
    # (often a separate mount; that is the one a pull actually fills up).
    try:
        r = await ssh(command=_DISK_SCRIPT, host_id=host_id, timeout=25)
        out.update(_parse_disk((r or {}).get("stdout", "") if isinstance(r, dict) else ""))
    except Exception as e:
        log.debug("detect disk %s: %s", host_id, e)
    return out


@capability("catalog.node.detect", memory="off",
            http_method="POST", http_path="/catalog/node/detect", http_tags=["catalog"],
            description="Auto-detect one node's hardware over SSH (nvidia-smi / free / "
                        "nproc) and cache it. Inputs: instance_id (str!). The node must be "
                        "mapped to an SSH host first (catalog.node.ssh_set).")
async def cap_catalog_node_detect(instance_id: str, trace_id=None):
    await _hydrate()
    host = NODE_SSH.get(instance_id)
    if not host:
        return {"error": f"node {instance_id} has no SSH host mapping — set one first"}
    hw = await _detect_hw(host)
    if hw.get("error"):
        return hw
    if not hw:
        return {"error": "detection returned nothing (tools missing or host unreachable)"}
    prev = NODE_HW.get(instance_id, {})
    prev.update(hw, source="auto", detected_at=now_iso())
    NODE_HW[instance_id] = prev
    await _persist(KEY_NODE_HW, NODE_HW)
    await emit_event({"type": "catalog.node.detected", "instance_id": instance_id, "hw": hw})
    return {"ok": True, "instance_id": instance_id, "hw": prev}


@capability("catalog.nodes.detect_all", memory="off",
            http_method="POST", http_path="/catalog/nodes/detect_all", http_tags=["catalog"],
            description="Detect hardware for every SSH-mapped node in one pass.")
async def cap_catalog_nodes_detect_all(trace_id=None):
    await _hydrate()
    results = {}
    for iid in list(NODE_SSH.keys()):
        results[iid] = await cap_catalog_node_detect(instance_id=iid)
    return {"ok": True, "results": results}


@capability("catalog.node.hw_set", memory="off",
            http_method="POST", http_path="/catalog/node/hw_set", http_tags=["catalog"],
            description="Manually set/override a node's hardware. Inputs: instance_id (str!), "
                        "vram_gb, ram_gb, gpu_name, gpu_count, cpu_cores (any subset). "
                        "Marks source=manual.")
async def cap_catalog_node_hw_set(instance_id: str, vram_gb: float = None,
                                  ram_gb: float = None, gpu_name: str = None,
                                  gpu_count: int = None, cpu_cores: int = None,
                                  trace_id=None):
    await _hydrate()
    hw = NODE_HW.get(instance_id, {})
    for k, v in (("vram_gb", vram_gb), ("ram_gb", ram_gb), ("gpu_name", gpu_name),
                 ("gpu_count", gpu_count), ("cpu_cores", cpu_cores)):
        if v is not None and v != "":
            hw[k] = v
    hw["source"] = "manual"
    hw["detected_at"] = now_iso()
    NODE_HW[instance_id] = hw
    await _persist(KEY_NODE_HW, NODE_HW)
    return {"ok": True, "instance_id": instance_id, "hw": hw}


@capability("catalog.node.ssh_set", memory="off",
            http_method="POST", http_path="/catalog/node/ssh_set", http_tags=["catalog"],
            description="Map a routing node (Ollama/vLLM instance id) to a host in the "
                        "canonical exec.ssh.hosts store, so its hardware can be auto-detected. "
                        "Inputs: instance_id (str!), host_id (str — empty clears).")
async def cap_catalog_node_ssh_set(instance_id: str, host_id: str = "", trace_id=None):
    await _hydrate()
    if host_id:
        NODE_SSH[instance_id] = host_id
    else:
        NODE_SSH.pop(instance_id, None)
    await _persist(KEY_NODE_SSH, NODE_SSH)
    return {"ok": True, "instance_id": instance_id, "host_id": host_id}


# ─────────────────────────────────────────────────────────────────────────────
# INSTALLED MODELS  —  what is ALREADY on each node, and what it costs on disk
# ─────────────────────────────────────────────────────────────────────────────
def _ssl():
    return getattr(_orch, "_SSL_CTX", None) or True


def _params_from_size_str(s: str) -> Optional[float]:
    """Ollama reports parameter_size like '7.6B' / '32.8B' / '1.5B'."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*([BbMm])", str(s or ""))
    if not m:
        return None
    val = float(m.group(1))
    return round(val / 1000.0, 2) if m.group(2).lower() == "m" else round(val, 1)


async def _ollama_inventory(iid: str, url: str) -> dict:
    """/api/tags + /api/ps for one Ollama node — installed models with their REAL
    quant + parameter size (no estimating needed), and which are resident now."""
    out: Dict[str, Any] = {"models": [], "running": [], "count": 0, "size_gb": 0.0}
    try:
        async with httpx.AsyncClient(timeout=8, verify=_ssl()) as c:
            r = await c.get(f"{url}/api/tags")
            r.raise_for_status()
            tags = (r.json() or {}).get("models", []) or []
    except Exception as e:
        return {**out, "error": str(e)}
    resident: Dict[str, dict] = {}
    try:
        async with httpx.AsyncClient(timeout=5, verify=_ssl()) as c:
            r = await c.get(f"{url}/api/ps")
            if r.status_code == 200:
                for m in (r.json() or {}).get("models", []) or []:
                    resident[m.get("name", "")] = {
                        "vram_gb": round((m.get("size_vram") or 0) / 1e9, 2),
                        "expires_at": m.get("expires_at", "")}
    except Exception:
        pass
    total = 0.0
    for m in tags:
        name = m.get("name") or m.get("model") or ""
        det = m.get("details") or {}
        size_gb = round((m.get("size") or 0) / 1e9, 2)
        total += size_gb
        quant = det.get("quantization_level") or ""
        params_b = _params_from_size_str(det.get("parameter_size", "")) or \
            _params_from_id(name)
        res = resident.get(name)
        out["models"].append({
            "name": name,
            "size_gb": size_gb,
            "quant": quant,
            "family": det.get("family", ""),
            "format": det.get("format", ""),
            "params_b": params_b,
            "modified_at": m.get("modified_at", ""),
            "digest": (m.get("digest") or "")[:12],
            "loaded": bool(res),
            "vram_gb": (res or {}).get("vram_gb"),
            "expires_at": (res or {}).get("expires_at", ""),
            "requirement": _estimate_requirements(params_b, quant),
            "source": "hf" if name.startswith("hf.co/") else "library",
        })
    out["models"].sort(key=lambda x: (not x["loaded"], -(x["size_gb"] or 0)))
    out["running"] = [n for n in resident]
    out["count"] = len(out["models"])
    out["size_gb"] = round(total, 2)
    return out


@capability("catalog.installed", memory="off", silent=True,
            http_method="GET", http_path="/catalog/installed", http_tags=["catalog"],
            description="Models ALREADY installed on each node, with their real quant, "
                        "parameter size, on-disk size, whether they are resident in VRAM "
                        "right now, plus the node's free disk space and how much of it the "
                        "model store occupies. Query: instance_id (str — one node, default "
                        "all). This is the 'what do I already have' view for the catalog.")
async def cap_catalog_installed(instance_id: str = "", trace_id=None):
    await _hydrate()
    allnodes = _all_nodes()
    targets = ({instance_id: allnodes[instance_id]} if instance_id and instance_id in allnodes
               else allnodes)

    async def _one(iid: str, base: dict) -> dict:
        node = await _node_view_full(iid, base)
        if base.get("backend") == "ollama" and base.get("url"):
            inv = await _ollama_inventory(iid, base["url"])
        else:
            # vLLM serves exactly what it was started with — no local tag store.
            inv = {"models": [{"name": m, "size_gb": None, "quant": "", "family": "",
                               "params_b": _params_from_id(str(m)), "loaded": True,
                               "source": "vllm"} for m in (base.get("models") or [])],
                   "running": list(base.get("models") or []),
                   "count": len(base.get("models") or []), "size_gb": 0.0}
        free = node.get("models_disk_free_gb")
        if free is None:
            free = node.get("disk_free_gb")
        total = node.get("models_disk_total_gb")
        if total is None:
            total = node.get("disk_total_gb")
        return {
            "id": iid, "backend": base.get("backend", ""),
            "label": base.get("label", iid), "url": base.get("url", ""),
            "status": base.get("status", ""), "has_gpu": base.get("has_gpu", False),
            "enabled": base.get("enabled", True),
            "vram_gb": node.get("vram_gb"), "ram_gb": node.get("ram_gb"),
            "gpu_name": node.get("gpu_name", ""),
            "disk_free_gb": free, "disk_total_gb": total,
            "disk_source": node.get("disk_source", ""),
            "models_path": node.get("models_path", ""),
            "models_dir_gb": node.get("models_dir_gb"),
            "ssh_host": node.get("ssh_host", ""),
            "ssh_host_suggest": node.get("ssh_host_suggest", ""),
            **inv,
        }

    # Concurrently — one unreachable node must not stall the whole view.
    done = await asyncio.gather(*[_one(iid, base) for iid, base in targets.items()],
                                return_exceptions=True)
    rows = []
    for (iid, base), res in zip(targets.items(), done):
        if isinstance(res, BaseException):
            rows.append({"id": iid, "backend": base.get("backend", ""),
                         "label": base.get("label", iid), "url": base.get("url", ""),
                         "status": base.get("status", ""), "models": [], "running": [],
                         "count": 0, "size_gb": 0.0, "error": str(res)[:200]})
        else:
            rows.append(res)
    rows.sort(key=lambda r: (r["backend"], r["id"]))
    return {"nodes": rows,
            "total_models": sum(r.get("count") or 0 for r in rows),
            "total_size_gb": round(sum(r.get("size_gb") or 0 for r in rows), 2)}


@capability("catalog.model.delete", memory="off",
            http_method="POST", http_path="/catalog/model/delete", http_tags=["catalog"],
            description="Delete an installed model from an Ollama node, freeing its disk. "
                        "Inputs: instance_id (str!), model (str! — the exact installed tag, "
                        "e.g. 'qwen2.5:7b' or 'hf.co/author/repo:Q4_K_M').")
async def cap_catalog_model_delete(instance_id: str, model: str, trace_id=None):
    base = _all_nodes().get(instance_id)
    if not base:
        return {"error": f"unknown node: {instance_id}"}
    if base.get("backend") != "ollama":
        return {"error": "only Ollama nodes have a deletable local model store"}
    url = base.get("url", "")
    try:
        async with httpx.AsyncClient(timeout=60, verify=_ssl()) as c:
            r = await c.request("DELETE", f"{url}/api/delete", json={"name": model})
            if r.status_code >= 400:
                return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"error": str(e)}
    await emit_event({"type": "catalog.model.deleted",
                      "instance_id": instance_id, "model": model})
    return {"ok": True, "instance_id": instance_id, "model": model}


# ─────────────────────────────────────────────────────────────────────────────
# LIVE DOWNLOADS  —  streaming /api/pull with real progress, speed and ETA
# ─────────────────────────────────────────────────────────────────────────────
# Ollama's /api/pull emits NDJSON: one object per state change, and for each
# blob a running {digest,total,completed}. We aggregate those into a single
# progress record per pull so the UI can show a live bar instead of a spinner
# that sits there for twenty minutes.
KEY_PULLS = "vera:catalog:pulls"        # Redis hash: pull_id -> record JSON
PULLS: Dict[str, dict] = {}             # this process's pulls (authoritative for them)
_PULL_TASKS: Dict[str, asyncio.Task] = {}
_PULL_SEQ = {"n": 0}
_ACTIVE_STATES = ("starting", "downloading", "verifying")
# A running pull writes its record at least once a second. One that still claims
# to be active but has not been touched for this long belongs to a Vera process
# that is gone — and since the /api/pull stream is ours, the transfer died with
# it. Generous enough to cover a slow manifest fetch before the first byte.
PULL_STALE_S = 120.0
# Which Vera opened a given pull stream. Stable across restarts of this host, so
# on the way back up we can tell our own dead downloads (resume them) apart from
# another cluster member's live ones (leave alone) without waiting on a timeout.
PULL_OWNER = os.getenv("VERA_NODE_NAME") or socket.gethostname()
# When this process began. A record last written before we existed cannot be
# ours, which is how records from *older* Vera builds — written before `owner`
# was recorded — are still recognised as dead the moment we come back.
_PROC_START = time.time()


def _pull_id(instance_id: str) -> str:
    _PULL_SEQ["n"] += 1
    return f"{instance_id}-{int(time.time())}-{_PULL_SEQ['n']}"


def _iso_age(ts: str) -> float:
    """Seconds since an ISO-8601 stamp; +inf when it cannot be read."""
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - t).total_seconds())
    except Exception:
        return float("inf")


def _pull_is_stale(rec: dict) -> bool:
    """True for a record that still advertises itself as running but whose owner
    is gone. Only ever true for the Redis mirror — local records in PULLS are
    driven by a live task."""
    if rec.get("state") not in _ACTIVE_STATES:
        return False
    # Our own record with no live task behind it. A pull only exists inside the
    # process that opened its stream, so if we are that Vera and we have no such
    # task, it died with the previous process — true the instant we come back.
    #
    # The age check below CANNOT see this case: at restart every record was
    # written a second earlier (the worker persists once a second right up to
    # the moment it is killed), so they all look alive. Auto-resume runs at
    # startup, found nothing, and never ran again — the heuristic raced the one
    # event it exists to catch.
    if rec.get("owner") == PULL_OWNER and rec.get("id") not in PULLS:
        return True
    # Someone else's record (another Vera in the cluster): all we can do is
    # notice it has stopped being written to.
    return _iso_age(rec.get("updated_at") or rec.get("started_at") or "") > PULL_STALE_S


def _predates_this_process(rec: dict) -> bool:
    """Was this record last written before we existed?

    Only meaningful during the startup pass, and only for records from builds
    that did not stamp an owner — without it, every download in flight at the
    moment this fix ships would be stranded one final time. It is deliberately
    NOT part of _pull_is_stale: seconds into startup almost everything predates
    us, which would condemn another cluster member's perfectly live pull."""
    return (rec.get("state") in _ACTIVE_STATES and not rec.get("owner")
            and _iso_age(rec.get("updated_at") or rec.get("started_at") or "")
            > (time.time() - _PROC_START))


def _pull_mark_interrupted(rec: dict) -> dict:
    """Turn an orphaned 'downloading' record into an honest terminal one. Ollama
    keeps the partial blob, so the bytes already on the node are not lost —
    re-pulling the same ref resumes from there (what the UI's Resume does)."""
    rec["state"] = "interrupted"
    rec["ok"] = False
    rec["error"] = rec.get("error") or (
        "Vera restarted while this download was in flight — the partial download "
        "is still on the node, resume to continue from where it stopped")
    rec["speed_bps"] = 0
    rec["eta_s"] = None
    rec["finished_at"] = rec.get("finished_at") or now_iso()
    return rec


async def _pull_all_redis() -> List[Tuple[str, dict]]:
    """Every mirrored record in the cluster, decoded. Unparseable rows are skipped."""
    r = _redis()
    if not r:
        return []
    out: List[Tuple[str, dict]] = []
    try:
        raw = await r.hgetall(KEY_PULLS)
        for k, v in (raw or {}).items():
            k = k.decode() if isinstance(k, bytes) else k
            v = v.decode() if isinstance(v, bytes) else v
            try:
                out.append((k, json.loads(v)))
            except Exception:
                continue
    except Exception as e:
        log.debug("pull scan: %s", e)
    return out


async def _pull_store(pull_id: str, rec: dict) -> None:
    r = _redis()
    if not r:
        return
    try:
        await r.hset(KEY_PULLS, pull_id, json.dumps(rec))
    except Exception as e:
        log.debug("pull store %s: %s", pull_id, e)


# Interrupted downloads are picked back up when Vera returns. Set
# VERA_PULL_AUTORESUME=0 to leave them sitting for a manual Resume instead.
PULL_AUTORESUME = (os.getenv("VERA_PULL_AUTORESUME", "1").strip().lower()
                   not in ("0", "false", "no", "off"))
# Don't silently restart multi-GB transfers that were abandoned days ago, and
# don't stampede the uplink if a lot were in flight at once.
PULL_RESUME_MAX_AGE_S = 86400.0
PULL_RESUME_MAX = 16


# Transfers die for two very different reasons. A dropped connection is bad luck
# and retrying is exactly right; a rejected manifest is a verdict and retrying
# reproduces it forever. Ollama reports both as "failed", so read the message.
_TRANSIENT_RE = re.compile(
    r"max retries exceeded|EOF|stream error|connection reset|connection refused|"
    r"broken pipe|timeout|timed out|temporary failure|no route to host|"
    r"unexpected EOF|i/o error|HTTP 5\d\d|digest mismatch", re.I)
_PERMANENT_RE = re.compile(
    r"file does not exist|not a valid quantization scheme|unauthorized|"
    r"no space left|invalid digest|not published|published incompletely|"
    r"unknown node|no such model", re.I)
PULL_MAX_RETRIES = 5


def _pull_failure_is_transient(rec: dict) -> bool:
    err = str(rec.get("error") or "")
    if _PERMANENT_RE.search(err):
        return False
    return bool(_TRANSIENT_RE.search(err))


def _pull_is_resumable(rec: dict) -> bool:
    """What may come back on its own.

    'interrupted' always: the process holding the stream went away, which is no
    reflection on the download. 'failed' only when the message describes a
    transport problem rather than a verdict — a dropped connection nine tenths
    of the way through a 20 GB pull should not need a human to notice it — and
    only up to PULL_MAX_RETRIES, so a model that genuinely cannot be fetched
    stops instead of looping. 'cancelled' never: that was a decision."""
    state = rec.get("state")
    if state == "interrupted":
        return True
    if state == "failed" and _pull_failure_is_transient(rec):
        return int(rec.get("resume_count") or 0) < PULL_MAX_RETRIES
    return False


async def _resume_pull(pid: str, rec: dict) -> dict:
    """Restart one interrupted download and retire the record it came from.

    Ollama still holds the partial blob, so this continues from where it stopped
    rather than re-fetching. Returns the new record, or {"error": ...}."""
    started = await _pull_start(rec.get("model", ""), rec.get("instance_id", ""),
                                hf_id=rec.get("hf_id", ""))
    if started.get("error"):
        return started
    # Carry the attempt count across, so a model that keeps dropping eventually
    # stops retrying instead of cycling forever.
    started["resume_count"] = int(rec.get("resume_count") or 0) + 1
    if started.get("id") != pid:
        # Superseded by a fresh record; drop the old one so the list does not
        # accumulate a rung per restart.
        await _pull_forget(pid)
    return started


async def _resume_pulls(pull_id: str = "", auto: bool = False) -> dict:
    """Resume one interrupted download, or every one of them when pull_id is ''."""
    rows = [(pid, rec) for pid, rec in await _pull_all_redis()
            if _pull_is_resumable(rec) and (not pull_id or pid == pull_id)]
    if auto:
        rows = [(pid, rec) for pid, rec in rows
                if _iso_age(rec.get("finished_at") or rec.get("updated_at") or "")
                <= PULL_RESUME_MAX_AGE_S]
    rows.sort(key=lambda kv: kv[1].get("started_at") or "")
    resumed, failed = [], []
    for pid, rec in rows[:PULL_RESUME_MAX]:
        res = await _resume_pull(pid, rec)
        if res.get("error"):
            failed.append({"model": rec.get("model"), "instance_id": rec.get("instance_id"),
                           "error": str(res["error"])[:200]})
        else:
            resumed.append({"pull_id": res["id"], "model": rec.get("model"),
                            "instance_id": rec.get("instance_id"),
                            "from_pct": rec.get("pct", 0)})
    return {"ok": True, "resumed": resumed, "failed": failed,
            "skipped": max(0, len(rows) - PULL_RESUME_MAX)}


_BG_TASKS: set = set()          # strong refs; a bare create_task can be GC'd
PULL_SWEEP_S = 180.0            # how often to pick dropped transfers back up


async def _pull_sweep() -> None:
    """Periodically put back anything that stopped on its own.

    Startup recovery only covers restarts. A connection dropped at 3am mid-pull
    would otherwise sit there failed until someone looked at the tab — after
    hours of transfer. Cheap: a Redis scan that no-ops unless something is
    actually waiting."""
    if not PULL_AUTORESUME:
        return
    try:
        if not any(_pull_is_resumable(rec) for _, rec in await _pull_all_redis()):
            return
        res = await _resume_pulls(auto=True)
    except Exception as e:
        log.debug("pull sweep: %s", e)
        return
    for r in res["resumed"]:
        log.info("catalog: picked %s back up on %s from %.1f%%",
                 r["model"], r["instance_id"], r["from_pct"] or 0.0)


async def _autoresume_pulls() -> None:
    """Startup hook: put back whatever the previous process was downloading.

    A pull is a stream owned by the Vera process, so every restart kills all of
    them. Without this the operator has to notice and press Resume on each row,
    which is exactly the manual step a restart should not create."""
    if not PULL_AUTORESUME:
        return
    try:
        res = await _resume_pulls(auto=True)
    except Exception as e:
        log.warning("catalog: auto-resume failed: %s", e)
        return
    for r in res["resumed"]:
        log.info("catalog: resumed %s on %s from %.1f%%",
                 r["model"], r["instance_id"], r["from_pct"] or 0.0)
    for f in res["failed"]:
        log.warning("catalog: could not resume %s on %s: %s",
                    f["model"], f["instance_id"], f["error"])
    if res["resumed"] or res["failed"]:
        await emit_event({"type": "catalog.pull.autoresume",
                          "resumed": len(res["resumed"]), "failed": len(res["failed"])})


async def _pull_forget(pull_id: str) -> None:
    PULLS.pop(pull_id, None)
    r = _redis()
    if not r:
        return
    try:
        await r.hdel(KEY_PULLS, pull_id)
    except Exception as e:
        log.debug("pull forget %s: %s", pull_id, e)


async def _reap_stale_pulls() -> int:
    """One-shot at startup: rewrite every orphaned record as 'interrupted'.

    Without this the Downloads list keeps showing a phantom live transfer — frozen
    percentage, but a speed and ETA that read as current — which cannot be
    cancelled (no local task) and cannot be dismissed (the UI only offers Dismiss
    on finished rows). Records that are merely stale-looking because they belong
    to a *live* pull on another Vera instance are left alone: those are still
    being written to, so they never cross PULL_STALE_S."""
    n = 0
    for pid, rec in await _pull_all_redis():
        if not (_pull_is_stale(rec) or _predates_this_process(rec)):
            continue
        await _pull_store(pid, _pull_mark_interrupted(rec))
        n += 1
    if n:
        log.info("catalog: retired %d model download(s) orphaned by a restart", n)
    return n


# ── registry preflight ───────────────────────────────────────────────────────
# Ollama pulls a model's big layers first and its tiny config blob LAST. When a
# registry serves the manifest but 404s one of the objects in it, the download
# runs all the way to 100%, fails on the final fetch, and never writes a
# manifest — so nothing appears in the model list, nothing can be resumed, and
# the bytes sit orphaned in the blob store. Worse, Ollama surfaces that 404 as
# Go's bare os.ErrNotExist, whose message is the uninformative "file does not
# exist". Two HTTP round-trips up front turn 39 GB and an hour into an
# immediate, legible refusal.
_HF_REF_RE = re.compile(r"^(?:hf\.co|huggingface\.co)/([^/]+/[^:/]+)(?::(.+))?$", re.I)


def _hf_ref(model: str) -> Optional[Tuple[str, str]]:
    """(repo, tag) for an hf.co pull ref, else None."""
    m = _HF_REF_RE.match((model or "").strip())
    return (m.group(1), m.group(2) or "latest") if m else None


# A GGUF filename ends in the quant it holds; that suffix is the tag HF serves.
_QUANT_TAG_RE = re.compile(
    r"^(?:I?Q\d[A-Z0-9_]*|BF16|F16|F32|MXFP4[A-Z0-9_]*|TQ\d[A-Z0-9_]*)$", re.I)
_HF_SHARD_RE = re.compile(r"-\d{5}-of-\d{5}$")


async def _hf_available_quants(repo: str) -> List[str]:
    """The quant tags a Hugging Face repo actually publishes.

    HF's Ollama endpoint only serves a tag when a matching GGUF file exists, and
    rejects everything else with a flat 400 — "The specified tag is not a valid
    quantization scheme" — that names no alternatives. The catalog's quant picker
    offers a generic list, so picking one this particular repo never published
    looks identical to picking a good one, and every retry fails the same way."""
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                     verify=_ssl()) as c:
            r = await c.get(f"{HF_HOST}/api/models/{repo}")
            if r.status_code >= 400:
                return []
            files = [s.get("rfilename", "") for s in (r.json().get("siblings") or [])]
    except Exception as e:
        log.debug("hf quants %s: %s", repo, e)
        return []
    cands: List[str] = []
    for f in files:
        if not f.lower().endswith(".gguf"):
            continue
        stem = _HF_SHARD_RE.sub("", os.path.basename(f)[:-5])
        tag = stem.rsplit("-", 1)[-1]        # trailing token; 'UD-' prefixes drop out
        if _QUANT_TAG_RE.match(tag) and tag not in cands:
            cands.append(tag)
    # A file existing is NOT enough: HF matches the tag against its own list of
    # quantization schemes, so a publisher's non-standard naming (bartowski's
    # Q4_K_L, say) is present in the repo and still rejected. Listing those back
    # would send the operator round the same loop. Ask which ones it will serve.
    sem = asyncio.Semaphore(8)

    async def _ok(tag: str) -> Optional[str]:
        async with sem:
            try:
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True,
                                             verify=_ssl()) as c:
                    r = await c.get(f"{HF_HOST}/v2/{repo}/manifests/{tag}",
                                    headers={"Accept": "application/vnd.docker."
                                                       "distribution.manifest.v2+json"})
                return tag if r.status_code == 200 else None
            except Exception:
                return None

    served = await asyncio.gather(*(_ok(t) for t in cands[:40]))
    return sorted(t for t in served if t)


async def _hf_bad_tag_msg(model: str) -> str:
    ref = _hf_ref(model)
    if not ref:
        return ""
    repo, tag = ref
    quants = await _hf_available_quants(repo)
    if not quants:
        return (f"hugging face does not serve the quant '{tag}' for {repo}. "
                f"Use 'latest', or pick the model from the Ollama library instead.")
    return (f"'{tag}' is not published for {repo} — that repo offers: "
            f"{', '.join(quants)} (or 'latest').")


async def _hf_check_objects(model: str) -> dict:
    """Verify every object an hf.co pull needs is actually served.

    Returns {"checked": bool, "missing": [{digest, kind, size}], "note": str}.
    Callers gate on ``missing`` — it is only ever populated by a definitive 404 on
    a specific object. A ref this cannot inspect (an Ollama library model, an
    unreachable registry) yields checked=False and no missing objects, so the
    download proceeds: a network blip must never block a legitimate pull.

    An unknown repo or quant is deliberately NOT our problem — Hugging Face
    answers those with 400/401 on the manifest itself, and Ollama fails on them
    within a second at "pulling manifest" without moving any data. This guard
    exists for the one case that is expensive: a manifest that resolves fine
    while an object inside it does not."""
    ref = _hf_ref(model)
    if not ref:
        return {"checked": False, "missing": [], "note": "not a hugging-face ref"}
    repo, tag = ref
    base = f"{HF_HOST}/v2/{repo}"
    timeout = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=20.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     verify=_ssl()) as c:
            mr = await c.get(f"{base}/manifests/{tag}",
                             headers={"Accept": "application/vnd.docker.distribution."
                                                "manifest.v2+json"})
            if mr.status_code >= 400:
                # A rejected quant tag is worth catching here: Ollama's own error
                # for it lists no alternatives, so the operator retries other
                # quants blind. Everything else is left to Ollama, which fails on
                # it in about a second without moving data.
                if "quantization scheme" in (mr.text or ""):
                    return {"checked": True, "missing": [], "bad_tag": True,
                            "note": f"manifest HTTP {mr.status_code}: rejected quant tag"}
                return {"checked": False, "missing": [],
                        "note": f"manifest HTTP {mr.status_code} — leaving it to Ollama"}
            man = mr.json()
            objs = [{"digest": l.get("digest", ""),
                     "kind": str(l.get("mediaType", "")).rsplit(".", 1)[-1] or "layer",
                     "size": int(l.get("size") or 0)}
                    for l in (man.get("layers") or [])]
            cfg = man.get("config") or {}
            if cfg.get("digest"):
                objs.append({"digest": cfg["digest"], "kind": "config",
                             "size": int(cfg.get("size") or 0)})

            async def _head(o: dict):
                try:
                    r = await c.head(f"{base}/blobs/{o['digest']}")
                    return o, r.status_code
                except Exception:
                    return o, None          # inconclusive, not missing

            results = await asyncio.gather(*(_head(o) for o in objs))
    except Exception as e:
        return {"checked": False, "missing": [], "note": f"preflight unavailable: {e}"}
    missing = [o for o, code in results if code == 404]
    # An object we could not reach is not an object we know to be absent.
    partial = any(code is None for _, code in results)
    return {"checked": not partial, "missing": missing,
            "note": "some objects could not be checked" if partial else ""}


def _hf_broken_msg(model: str, chk: dict) -> str:
    bits = ", ".join(f"{o['kind']} {o['digest'][7:19]} ({o['size']}B)"
                     for o in chk.get("missing", []))
    repo = (_hf_ref(model) or (model, ""))[0]
    return (f"this quant is published incompletely on hugging face — the registry "
            f"404s on: {bits}. Ollama fetches that object last, so the download "
            f"would reach 100% and then fail with 'file does not exist'. Try a "
            f"different quant of {repo}.")


async def _pull_persist(rec: dict, force: bool = False) -> None:
    """Mirror progress into Redis so any Vera instance in the cluster can report
    it (the pull itself only runs on the node that accepted the request)."""
    now = time.time()
    if not force and now - rec.get("_persisted", 0) < 1.0:
        return
    rec["_persisted"] = now
    r = _redis()
    if not r:
        return
    try:
        await r.hset(KEY_PULLS, rec["id"],
                     json.dumps({k: v for k, v in rec.items() if not k.startswith("_")}))
        await r.expire(KEY_PULLS, 86400)
    except Exception as e:
        log.debug("pull persist: %s", e)


def _pull_public(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if not k.startswith("_")}


async def _explain_pull_error(model: str, err: str) -> str:
    """Ollama reports a registry 404 as Go's bare os.ErrNotExist — "file does not
    exist" — naming neither the object nor the cause. Ask the registry which one
    it is actually refusing to serve and say so."""
    e = (err or "").strip()
    if "quantization scheme" in e.lower():
        return (await _hf_bad_tag_msg(model) or e)[:400]
    if e.lower().rstrip(".") != "file does not exist":
        return e[:400]
    chk = await _hf_check_objects(model)
    if chk.get("missing"):
        return _hf_broken_msg(model, chk)[:400]
    return (f"{e} — Ollama could not fetch one of this model's objects from the "
            f"registry (a 404 is reported with this wording)")[:400]


async def _pull_worker(rec: dict, url: str) -> None:
    """Stream one /api/pull and keep `rec` up to date until it terminates."""
    layers: Dict[str, dict] = rec["layers"]
    last_emit = 0.0
    # Long downloads: no read timeout, but don't hang forever on a dead connect.
    timeout = httpx.Timeout(connect=20.0, read=None, write=None, pool=None)
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=_ssl()) as c:
            async with c.stream("POST", f"{url}/api/pull",
                                json={"name": rec["model"], "stream": True}) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")[:300]
                    raise RuntimeError(f"HTTP {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    line = (line or "").strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except Exception:
                        continue
                    if msg.get("error"):
                        raise RuntimeError(str(msg["error"]))
                    status = str(msg.get("status") or "")
                    rec["status"] = status
                    digest = msg.get("digest")
                    if digest and msg.get("total"):
                        lay = layers.setdefault(digest, {"total": 0, "completed": 0})
                        lay["total"] = int(msg.get("total") or lay["total"])
                        lay["completed"] = int(msg.get("completed") or 0)
                        rec["state"] = "downloading"
                    elif status.startswith("verifying") or status.startswith("writing"):
                        rec["state"] = "verifying"
                    total = sum(l["total"] for l in layers.values())
                    done = sum(min(l["completed"], l["total"]) for l in layers.values())
                    rec["total_bytes"] = total
                    rec["completed_bytes"] = done
                    rec["pct"] = round(done / total * 100, 1) if total else (
                        100.0 if rec["state"] == "verifying" else 0.0)
                    rec["layers_done"] = sum(
                        1 for l in layers.values() if l["total"] and l["completed"] >= l["total"])
                    rec["layers_total"] = len(layers)
                    now = time.time()
                    dt = now - rec["_rate_at"]
                    if dt >= 2.0:
                        rec["speed_bps"] = max(0, int((done - rec["_rate_bytes"]) / dt))
                        rec["_rate_at"], rec["_rate_bytes"] = now, done
                        rec["eta_s"] = (int((total - done) / rec["speed_bps"])
                                        if rec["speed_bps"] > 0 and total > done else None)
                    rec["updated_at"] = now_iso()
                    if now - last_emit >= 1.0:
                        last_emit = now
                        await _pull_persist(rec)
                        await emit_event({"type": "catalog.pull.progress",
                                          **{k: rec[k] for k in
                                             ("id", "model", "instance_id", "state",
                                              "status", "pct", "speed_bps", "eta_s",
                                              "total_bytes", "completed_bytes")}})
                    if status == "success":
                        rec["state"] = "done"
        if rec["state"] != "done":
            rec["state"] = "done"          # stream ended cleanly without an explicit success
        rec["ok"] = True
        rec["pct"] = 100.0
    except asyncio.CancelledError:
        # Two very different things arrive here: the operator pressing Cancel,
        # and the event loop tearing down tasks as Vera shuts down. Only the
        # first is a decision to stop wanting this model — a shutdown must look
        # like any other interruption so it gets picked back up on restart.
        if rec.pop("_operator_cancel", False):
            rec["state"] = "cancelled"
            rec["ok"] = False
            rec["error"] = "cancelled by operator"
        else:
            _pull_mark_interrupted(rec)
            rec["error"] = ("Vera shut down while this download was in flight — "
                            "it resumes automatically when Vera comes back")
        raise
    except Exception as e:
        rec["state"] = "failed"
        rec["ok"] = False
        rec["error"] = await _explain_pull_error(rec["model"], str(e))
    finally:
        rec["finished_at"] = now_iso()
        rec["updated_at"] = rec["finished_at"]
        rec["eta_s"] = None
        rec["speed_bps"] = 0
        _PULL_TASKS.pop(rec["id"], None)
        # Cancellation lands here too, and awaiting inside a cancelling task can
        # be interrupted — run the final write as an independent task so the
        # record never gets stuck showing "downloading".
        try:
            await asyncio.shield(asyncio.ensure_future(_pull_finalise(rec)))
        except BaseException:
            pass


async def _pull_finalise(rec: dict) -> None:
    await _pull_persist(rec, force=True)
    await emit_event({"type": "catalog.pull.done", "id": rec["id"],
                      "model": rec["model"], "instance_id": rec["instance_id"],
                      "ok": bool(rec.get("ok")), "state": rec["state"],
                      "error": rec.get("error", "")})


async def _pull_start(model: str, instance_id: str, hf_id: str = "") -> dict:
    """Kick off a streamed pull in the background; returns the progress record."""
    base = _all_nodes().get(instance_id)
    if not base:
        return {"error": f"unknown node: {instance_id}"}
    if base.get("backend") != "ollama":
        return {"error": "streamed pulls are an Ollama-node operation"}
    url = base.get("url", "")
    if not url:
        return {"error": f"node {instance_id} has no URL"}
    for rec in PULLS.values():
        if (rec["instance_id"] == instance_id and rec["model"] == model
                and rec["state"] in _ACTIVE_STATES):
            return rec                     # already downloading here — join it
    # The same ref may be in flight on another Vera instance; a second /api/pull
    # would fight it over the same partial blob. Orphans from a restart look
    # identical in Redis, so retire those instead of refusing to start.
    for pid, other in await _pull_all_redis():
        if (pid not in PULLS and other.get("instance_id") == instance_id
                and other.get("model") == model
                and other.get("state") in _ACTIVE_STATES):
            if _pull_is_stale(other):
                await _pull_store(pid, _pull_mark_interrupted(other))
            else:
                return other               # live elsewhere — point at its record
    chk = await _hf_check_objects(model)
    if chk.get("bad_tag"):
        return {"error": await _hf_bad_tag_msg(model), "preflight": chk}
    if chk.get("missing"):
        return {"error": _hf_broken_msg(model, chk), "preflight": chk}
    rec = {
        "id": _pull_id(instance_id), "model": model, "hf_id": hf_id,
        "owner": PULL_OWNER,
        "instance_id": instance_id, "node_label": base.get("label", instance_id),
        "state": "starting", "status": "queued", "ok": None, "error": "",
        "total_bytes": 0, "completed_bytes": 0, "pct": 0.0,
        "speed_bps": 0, "eta_s": None, "layers": {},
        "layers_done": 0, "layers_total": 0,
        "started_at": now_iso(), "updated_at": now_iso(), "finished_at": "",
        "_rate_at": time.time(), "_rate_bytes": 0, "_persisted": 0.0,
    }
    PULLS[rec["id"]] = rec
    _prune_pulls()
    await _pull_persist(rec, force=True)
    await emit_event({"type": "catalog.pull.start", "id": rec["id"],
                      "model": model, "instance_id": instance_id})
    _PULL_TASKS[rec["id"]] = asyncio.create_task(_pull_worker(rec, url))
    return rec


def _prune_pulls() -> None:
    """Keep the in-memory log bounded: drop finished pulls beyond the newest 40."""
    finished = sorted((r for r in PULLS.values() if r["state"] not in _ACTIVE_STATES),
                      key=lambda r: r.get("finished_at") or r.get("started_at") or "")
    for rec in finished[:-40]:
        PULLS.pop(rec["id"], None)


@capability("catalog.pull.start", memory="off",
            http_method="POST", http_path="/catalog/pull/start", http_tags=["catalog"],
            description="Start a model download on an Ollama node and return IMMEDIATELY "
                        "with a pull id — progress is streamed in the background and read "
                        "back via catalog.pull.status. Inputs: model (str! — an Ollama "
                        "library name like 'qwen2.5:7b' or an 'hf.co/author/repo:QUANT' "
                        "ref), instance_id (str!). Re-starting a pull that is already "
                        "running joins the existing one; re-starting one that was "
                        "interrupted resumes it from the partial data on the node. "
                        "hf.co refs are preflighted — a quant whose registry objects "
                        "are incomplete is refused up front rather than after "
                        "downloading the whole model.")
async def cap_catalog_pull_start(model: str, instance_id: str, trace_id=None):
    await _hydrate()
    if not (model or "").strip():
        return {"error": "model required"}
    rec = await _pull_start(model.strip(), instance_id)
    if rec.get("error"):
        return rec
    return {"ok": True, "pull_id": rec["id"], "pull": _pull_public(rec)}


@capability("catalog.pull.status", memory="off", silent=True,
            http_method="GET", http_path="/catalog/pull/status", http_tags=["catalog"],
            description="Live status of model downloads: percent complete, bytes, speed, "
                        "ETA, per-layer counts and the last Ollama status line. Query: "
                        "pull_id (str — one pull), instance_id (str — filter), active_only "
                        "(bool). Includes pulls started by other Vera instances in the "
                        "cluster (mirrored through Redis). A download whose Vera process "
                        "died mid-transfer reports state 'interrupted', not 'downloading' "
                        "— restart it with catalog.pull.start to resume.")
async def cap_catalog_pull_status(pull_id: str = "", instance_id: str = "",
                                  active_only: bool = False, trace_id=None):
    await _hydrate()          # first call after a restart also reaps the orphans
    merged: Dict[str, dict] = {}
    for pid, rec in await _pull_all_redis():
        # Report an orphan as interrupted rather than as a live transfer. The
        # startup reaper handles our own leftovers; this also covers another
        # instance dying mid-session, without waiting for it to come back.
        merged[pid] = _pull_mark_interrupted(rec) if _pull_is_stale(rec) else rec
    for pid, rec in PULLS.items():        # local records win — they are live
        merged[pid] = _pull_public(rec)
    rows = list(merged.values())
    if pull_id:
        rows = [x for x in rows if x.get("id") == pull_id]
    if instance_id:
        rows = [x for x in rows if x.get("instance_id") == instance_id]
    if active_only:
        rows = [x for x in rows if x.get("state") in _ACTIVE_STATES]
    rows.sort(key=lambda x: x.get("started_at") or "", reverse=True)
    active = [x for x in rows if x.get("state") in _ACTIVE_STATES]
    return {"pulls": rows[:60], "active": len(active),
            "active_ids": [x["id"] for x in active]}


@capability("catalog.pull.cancel", memory="off",
            http_method="POST", http_path="/catalog/pull/cancel", http_tags=["catalog"],
            description="Cancel an in-flight model download. Inputs: pull_id (str!). Only "
                        "the Vera instance that started the pull can cancel it; from "
                        "another instance this reports where it is running. A download "
                        "orphaned by a restart is retired as 'interrupted'.")
async def cap_catalog_pull_cancel(pull_id: str, trace_id=None):
    task = _PULL_TASKS.get(pull_id)
    if not task:
        if pull_id in PULLS:
            return {"ok": True, "note": "already finished", "pull": _pull_public(PULLS[pull_id])}
        # No local task and no local record: either another live Vera instance owns
        # it, or it is an orphan from a restart. Retire the orphan here so the row
        # is not stuck being un-cancellable until the Redis TTL expires.
        for pid, rec in await _pull_all_redis():
            if pid != pull_id:
                continue
            if _pull_is_stale(rec):
                await _pull_store(pid, _pull_mark_interrupted(rec))
                return {"ok": True, "pull_id": pull_id, "state": "interrupted",
                        "note": "download was orphaned by a Vera restart", "pull": rec}
            break
        return {"error": "pull not running on this Vera instance — cancel it from the "
                         "instance that started it"}
    rec = PULLS.get(pull_id)
    if rec is not None:
        rec["_operator_cancel"] = True   # tells the worker this is a real stop,
                                         # not a shutdown — see _pull_worker
    task.cancel()
    return {"ok": True, "pull_id": pull_id, "state": "cancelling"}


@capability("catalog.pull.resume", memory="off",
            http_method="POST", http_path="/catalog/pull/resume", http_tags=["catalog"],
            description="Resume downloads that a Vera restart interrupted, continuing "
                        "from the partial data already on the node rather than starting "
                        "over. Inputs: pull_id (str — one download; empty resumes ALL "
                        "interrupted ones). Operator-cancelled and failed downloads are "
                        "left alone. This also runs automatically at startup.")
async def cap_catalog_pull_resume(pull_id: str = "", trace_id=None):
    await _hydrate()
    res = await _resume_pulls(pull_id=pull_id)
    if pull_id and not res["resumed"] and not res["failed"]:
        return {"error": "nothing to resume — that download is not in the "
                         "'interrupted' state"}
    return res


@capability("catalog.pull.clear", memory="off",
            http_method="POST", http_path="/catalog/pull/clear", http_tags=["catalog"],
            description="Clear finished download records from the downloads list. "
                        "Inputs: pull_id (str — one record; empty clears all finished, "
                        "including any orphaned by a restart, but never a live pull).")
async def cap_catalog_pull_clear(pull_id: str = "", trace_id=None):
    ids = ([pull_id] if pull_id
           else [pid for pid, rec in PULLS.items() if rec["state"] not in _ACTIVE_STATES])
    cleared = 0
    for pid in ids:
        rec = PULLS.get(pid)
        if rec and rec["state"] in _ACTIVE_STATES:
            continue
        PULLS.pop(pid, None)
        cleared += 1
    r = _redis()
    if r:
        try:
            if pull_id:
                await r.hdel(KEY_PULLS, pull_id)
            else:
                # Keep only what is genuinely live (here or on another instance);
                # everything else goes — finished, orphaned by a restart, or
                # unparseable junk.
                keep = {pid for pid, rec in await _pull_all_redis()
                        if rec.get("state") in _ACTIVE_STATES and not _pull_is_stale(rec)}
                allk = [k.decode() if isinstance(k, bytes) else k
                        for k in (await r.hkeys(KEY_PULLS) or [])]
                done = [k for k in allk if k not in keep]
                if done:
                    await r.hdel(KEY_PULLS, *done)
        except Exception as e:
            log.debug("pull clear redis: %s", e)
    return {"ok": True, "cleared": cleared}


# ─────────────────────────────────────────────────────────────────────────────
# INSTALL  (delegates to ollama.pull / pxstore.models.pull / vllm.server.start)
# ─────────────────────────────────────────────────────────────────────────────
def _pick_gguf_variant(variants: List[dict], instance_id: str,
                       want_quant: str = "") -> Optional[dict]:
    """Best GGUF variant for a node: honour an explicit quant, else pick the
    highest-quality one that fits (falling back to the smallest)."""
    usable = [v for v in variants if v.get("quant") not in (None, "UNKNOWN")]
    if not usable:
        return None
    if want_quant:
        for v in usable:
            if v["quant"].upper() == want_quant.upper():
                return v
    fitting = [v for v in usable if v.get("fit", {}).get("verdict") in ("gpu", "cpu")]
    pool = fitting or usable
    # highest quality ≈ largest size that qualifies
    return max(pool, key=lambda v: v.get("size", 0))


@capability("catalog.install.plan", memory="off", silent=True,
            http_method="POST", http_path="/catalog/install/plan", http_tags=["catalog"],
            description="Dry-run an install: resolve the concrete model ref + hardware "
                        "verdict without side effects. Inputs: id (str! HF repo), backend "
                        "(ollama|vllm), instance_id (str!), quant (str — optional GGUF "
                        "quant override).")
async def cap_catalog_install_plan(id: str, instance_id: str, backend: str = "ollama",
                                   quant: str = "", trace_id=None):
    await _hydrate()
    detail = await cap_catalog_model(id=id, fits=instance_id)
    if detail.get("error"):
        return detail
    node = _node_view(instance_id, _all_nodes().get(instance_id, {}))
    if backend == "ollama":
        v = _pick_gguf_variant(detail.get("gguf_variants", []), instance_id, quant)
        if not v:
            return {"error": "no GGUF variant found in this repo for the Ollama path — "
                             "use the vLLM backend or a *-GGUF repo", "detail_id": id}
        return {"ok": True, "backend": "ollama", "instance_id": instance_id,
                "ref": v["ollama_ref"], "quant": v["quant"], "size_gb": v["size_gb"],
                "requirement": v["requirement"], "fit": v["fit"],
                "throughput": v["throughput_gpu"] if node.get("has_gpu") else v["throughput_cpu"]}
    elif backend == "vllm":
        tags = [t.lower() for t in detail.get("tags", [])]
        vq = quant or next((q for q in ("awq", "gptq", "fp8") if q in tags), "")
        vram = node.get("vram_gb") or 0
        tp = node.get("gpu_count") or 1
        args = {"model": id, "instance_id": instance_id,
                "tensor_parallel_size": tp,
                "max_model_len": min(detail.get("context_length", 4096), 8192)}
        if vq:
            args["quantization"] = vq
        return {"ok": True, "backend": "vllm", "instance_id": instance_id,
                "vllm_args": args, "requirement": detail.get("safetensors", {}).get("requirement"),
                "fit": detail.get("safetensors", {}).get("fit"),
                "vram_gb": vram}
    return {"error": f"unknown backend: {backend}"}


@capability("catalog.install", memory="off",
            http_method="POST", http_path="/catalog/install", http_tags=["catalog"],
            description="Install a model onto a node. Inputs: id (str! HF repo), backend "
                        "(ollama|vllm), instance_id (str!), quant (str — optional), via "
                        "(direct|store — Ollama only; 'store' pulls once to the shared "
                        "model store), wait (bool, default true — set false to return a "
                        "pull_id immediately and track live progress via "
                        "catalog.pull.status). Delegates to ollama.pull / "
                        "pxstore.models.pull / vllm.server.start.")
async def cap_catalog_install(id: str, instance_id: str, backend: str = "ollama",
                              quant: str = "", via: str = "direct", wait: bool = True,
                              trace_id=None):
    plan = await cap_catalog_install_plan(id=id, instance_id=instance_id,
                                          backend=backend, quant=quant)
    if not plan.get("ok"):
        return plan
    await emit_event({"type": "catalog.install.start", "id": id,
                      "instance_id": instance_id, "backend": backend, "plan": plan})
    if backend == "ollama" and not wait and via != "store":
        rec = await _pull_start(plan["ref"], instance_id, hf_id=id)
        if rec.get("error"):
            return rec
        return {"ok": True, "backend": "ollama", "ref": plan["ref"],
                "pull_id": rec["id"], "streaming": True, "plan": plan}
    if backend == "ollama":
        ref = plan["ref"]
        if via == "store" and _cap("pxstore.models.pull"):
            fn = _cap("pxstore.models.pull")
            res = await fn(model=ref, via="store")
        else:
            fn = _cap("ollama.pull")
            if not fn:
                return {"error": "ollama.pull unavailable"}
            res = await fn(model=ref, instance_id=instance_id)
        ok = not (isinstance(res, dict) and res.get("error"))
        await emit_event({"type": "catalog.install.done", "id": id, "ref": ref,
                          "instance_id": instance_id, "ok": ok})
        return {"ok": ok, "backend": "ollama", "ref": ref, "result": res}
    elif backend == "vllm":
        fn = _cap("vllm.server.start")
        if not fn:
            return {"error": "vllm.server.start unavailable"}
        res = await fn(**plan["vllm_args"])
        ok = bool(isinstance(res, dict) and res.get("ok"))
        await emit_event({"type": "catalog.install.done", "id": id,
                          "instance_id": instance_id, "backend": "vllm", "ok": ok})
        return {"ok": ok, "backend": "vllm", "result": res}
    return {"error": f"unknown backend: {backend}"}


# ─────────────────────────────────────────────────────────────────────────────
# SWAP / PIN / "SLOW HIGH-QUALITY"  (delegate to the routing caps)
# ─────────────────────────────────────────────────────────────────────────────
@capability("catalog.route.set_model", memory="off",
            http_method="POST", http_path="/catalog/route/set_model", http_tags=["catalog"],
            description="Repoint a routing target at a model (the easy model-swap). "
                        "Inputs: model (str!), scope (role|cap, default cap), instance_id "
                        "(str — optional pin), and either pattern (str — for scope=cap, e.g. "
                        "'chat.*') or profile+role (for scope=role). prefer_gpu/deny_gpu "
                        "(bool). Wraps ollama.cap_routing.save / ollama.role_profiles.save.")
async def cap_catalog_route_set_model(model: str, scope: str = "cap", pattern: str = "",
                                      profile: str = "", role: str = "default",
                                      instance_id: str = "", prefer_gpu: bool = False,
                                      deny_gpu: bool = False, trace_id=None):
    if not (model or "").strip():
        return {"error": "model required"}
    if scope == "role":
        if not profile:
            return {"error": "profile required for scope=role"}
        fn = _cap("ollama.role_profiles.save")
        if not fn:
            return {"error": "ollama.role_profiles.save unavailable"}
        rule = {"model": model, "prefer_gpu": prefer_gpu, "deny_gpu": deny_gpu}
        if instance_id:
            rule["pin"] = instance_id
        return await fn(profile=profile, roles={role: rule})
    # scope == cap
    if not pattern:
        return {"error": "pattern required for scope=cap"}
    fn = _cap("ollama.cap_routing.save")
    if not fn:
        return {"error": "ollama.cap_routing.save unavailable"}
    return await fn(pattern=pattern, model=model, pin=instance_id,
                    prefer_gpu=prefer_gpu, deny_gpu=deny_gpu)


@capability("catalog.node.mark_quality", memory="off",
            http_method="POST", http_path="/catalog/node/mark_quality", http_tags=["catalog"],
            description="Mark a node as a 'slow high-quality' node and pin a large model to "
                        "it: sets the node's class badge + writes a routing pin under the "
                        "'quality' role profile so loops/agents can request the high-quality "
                        "route. Inputs: instance_id (str!), model (str!), role (str, "
                        "default 'default'). Set model='' to UNMARK.")
async def cap_catalog_node_mark_quality(instance_id: str, model: str = "",
                                        role: str = "default", trace_id=None):
    await _hydrate()
    hw = NODE_HW.get(instance_id, {})
    if not model:
        hw.pop("class", None)
        hw.pop("pinned_model", None)
        NODE_HW[instance_id] = hw
        await _persist(KEY_NODE_HW, NODE_HW)
        df = _cap("ollama.role_profiles.delete")
        if df:
            await df(profile="quality", role=role)
        await emit_event({"type": "catalog.node.quality", "instance_id": instance_id,
                          "marked": False})
        return {"ok": True, "instance_id": instance_id, "marked": False}
    hw["class"] = "slow-hq"
    hw["pinned_model"] = model
    NODE_HW[instance_id] = hw
    await _persist(KEY_NODE_HW, NODE_HW)
    fn = _cap("ollama.role_profiles.save")
    res = None
    if fn:
        res = await fn(profile="quality", label="Slow · High-Quality",
                       roles={role: {"model": model, "pin": instance_id, "prefer_gpu": True}})
    await emit_event({"type": "catalog.node.quality", "instance_id": instance_id,
                      "marked": True, "model": model})
    return {"ok": True, "instance_id": instance_id, "marked": True,
            "model": model, "profile": "quality", "role": role, "route": res}


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-OPTIMISE  (manual suggest/apply + opt-in scheduled loop)
# ─────────────────────────────────────────────────────────────────────────────
async def _suggest_for_node(iid: str, node: dict, backend: str = "ollama") -> Optional[dict]:
    """Latest fitting model for a node vs. what it currently runs."""
    hw = NODE_HW.get(iid, {})
    if not (hw.get("vram_gb") or hw.get("ram_gb")):
        return None  # unknown hardware — skip
    res = await cap_catalog_search(search="", filter="gguf", sort="lastModified",
                                   limit=40, fits=iid)
    fitting = [r for r in res.get("results", [])
               if r.get("fit", {}).get("verdict") in ("gpu", "cpu")
               and r.get("est_params_b")]
    if not fitting:
        return None
    # prefer the biggest fitting model (highest quality), newest as tie-break
    best = max(fitting, key=lambda r: (r["est_params_b"], r.get("lastModified", "")))
    current = (node.get("models") or [None])[0]
    if current and best["id"].split("/")[-1].lower() in str(current).lower():
        return None  # already on (a build of) this model
    return {"instance_id": iid, "backend": backend,
            "current": current, "suggested": best["id"],
            "est_params_b": best["est_params_b"],
            "requirement": best.get("requirement"), "fit": best.get("fit"),
            "reason": f"latest model that fits {iid} "
                      f"(~{best['est_params_b']}B, {best['fit'].get('verdict','?').upper()})"}


@capability("catalog.optimize.suggest", memory="off",
            http_method="POST", http_path="/catalog/optimize/suggest", http_tags=["catalog"],
            description="Recommend the best latest model that fits each node (current → "
                        "suggested diff), no side effects. Inputs: scope (all|node), "
                        "instance_id (str — for scope=node), backend (ollama|vllm).")
async def cap_catalog_optimize_suggest(scope: str = "all", instance_id: str = "",
                                       backend: str = "ollama", trace_id=None):
    await _hydrate()
    nodes = _all_nodes()
    targets = ([instance_id] if scope == "node" and instance_id
               else [iid for iid, n in nodes.items() if n.get("enabled", True)])
    suggestions = []
    for iid in targets:
        n = nodes.get(iid)
        if not n:
            continue
        s = await _suggest_for_node(iid, n, backend)
        if s:
            suggestions.append(s)
    return {"suggestions": suggestions, "count": len(suggestions)}


@capability("catalog.optimize.apply", memory="off",
            http_method="POST", http_path="/catalog/optimize/apply", http_tags=["catalog"],
            description="Apply optimiser selections: install the suggested model on each "
                        "node and (optionally) repoint roles at it. Inputs: selections "
                        "(list of {instance_id, model, backend, roles?[], profile?, "
                        "pin?bool}).")
async def cap_catalog_optimize_apply(selections: Optional[List[dict]] = None, trace_id=None):
    await _hydrate()
    selections = selections or []
    results = []
    for sel in selections:
        iid = sel.get("instance_id")
        model = sel.get("model")
        backend = sel.get("backend", "ollama")
        if not (iid and model):
            results.append({"error": "instance_id and model required", "sel": sel})
            continue
        inst = await cap_catalog_install(id=model, instance_id=iid, backend=backend)
        routed = []
        if inst.get("ok") and sel.get("roles"):
            ref = inst.get("ref") or model
            for role in sel["roles"]:
                routed.append(await cap_catalog_route_set_model(
                    model=ref, scope="role",
                    profile=sel.get("profile", "default"), role=role,
                    instance_id=iid if sel.get("pin") else ""))
        results.append({"instance_id": iid, "model": model, "install": inst, "routed": routed})
    return {"ok": True, "results": results}


@capability("catalog.autoopt.get", memory="off", silent=True,
            http_method="GET", http_path="/catalog/autoopt", http_tags=["catalog"],
            description="Get auto-optimise config + recent run log.")
async def cap_catalog_autoopt_get(trace_id=None):
    await _hydrate()
    log_entries = []
    r = _redis()
    if r:
        try:
            raw = await r.get(KEY_AUTOLOG)
            if raw:
                log_entries = json.loads(raw)[-30:]
        except Exception:
            pass
    return {"config": AUTOOPT, "log": log_entries}


@capability("catalog.autoopt.set", memory="off",
            http_method="POST", http_path="/catalog/autoopt/set", http_tags=["catalog"],
            description="Configure opt-in auto-optimise. Inputs: enabled_nodes (list — "
                        "instance ids that auto-apply), roles (list — roles to repoint), "
                        "interval_min (int, default 1440), backend (ollama|vllm).")
async def cap_catalog_autoopt_set(enabled_nodes: Optional[List[str]] = None,
                                  roles: Optional[List[str]] = None,
                                  interval_min: int = None, backend: str = "",
                                  trace_id=None):
    await _hydrate()
    if enabled_nodes is not None:
        AUTOOPT["enabled_nodes"] = [str(x) for x in enabled_nodes]
    if roles is not None:
        AUTOOPT["roles"] = [str(x) for x in roles]
    if interval_min:
        AUTOOPT["interval_min"] = int(interval_min)
    if backend:
        AUTOOPT["backend"] = backend
    await _persist(KEY_AUTOOPT, AUTOOPT)
    return {"ok": True, "config": AUTOOPT}


_AUTOOPT_STATE = {"last": 0.0}


async def _autoopt_tick():
    """Scheduled: if auto-optimise is enabled for any node and the interval has
    elapsed, run suggest→apply for those nodes and append to the log."""
    try:
        await _hydrate()
        nodes = AUTOOPT.get("enabled_nodes") or []
        if not nodes:
            return
        interval_s = max(60, int(AUTOOPT.get("interval_min", 1440)) * 60)
        if time.time() - _AUTOOPT_STATE["last"] < interval_s:
            return
        _AUTOOPT_STATE["last"] = time.time()
        applied = []
        allnodes = _all_nodes()
        for iid in nodes:
            n = allnodes.get(iid)
            if not n:
                continue
            s = await _suggest_for_node(iid, n, AUTOOPT.get("backend", "ollama"))
            if not s:
                continue
            sel = {"instance_id": iid, "model": s["suggested"],
                   "backend": AUTOOPT.get("backend", "ollama"),
                   "roles": AUTOOPT.get("roles") or [], "profile": "default", "pin": True}
            res = await cap_catalog_optimize_apply(selections=[sel])
            applied.append({"instance_id": iid, "model": s["suggested"],
                            "ok": bool((res.get("results") or [{}])[0].get("install", {}).get("ok"))})
        entry = {"ts": now_iso(), "applied": applied}
        r = _redis()
        if r and applied:
            try:
                raw = await r.get(KEY_AUTOLOG)
                lst = json.loads(raw) if raw else []
                lst.append(entry)
                await r.set(KEY_AUTOLOG, json.dumps(lst[-60:]))
            except Exception:
                pass
        if applied:
            await emit_event({"type": "catalog.autoopt.run", "applied": applied})
    except Exception as e:
        log.warning("autoopt tick: %s", e)


# Declare the 'quality' routing profile so it always shows in the Model Routing
# page; mark_quality fills in a USER override that wins.
try:
    register_routing_profile("quality", label="Slow · High-Quality", owner="catalog",
                             roles={"default": {}})
except Exception as e:
    log.debug("register quality profile: %s", e)

# Opt-in auto-optimise heartbeat (checks its own interval internally).
try:
    schedule(_autoopt_tick, 300.0, name="catalog_autoopt")
except Exception as e:
    log.debug("schedule autoopt: %s", e)

# Pick dropped downloads back up without waiting for a restart or an operator.
try:
    schedule(_pull_sweep, PULL_SWEEP_S, name="catalog_pull_sweep")
except Exception as e:
    log.debug("schedule pull sweep: %s", e)

log.info("catalog: model catalog capabilities loaded")
