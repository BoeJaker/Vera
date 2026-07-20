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
import asyncio, json, logging, os, re, sys, time
from typing import Any, Dict, List, Optional

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
_HYDRATED = {"v": False}


# ─────────────────────────────────────────────────────────────────────────────
# LAZY BACKEND ACCESS  (mirror of the _cap() pattern used elsewhere)
# ─────────────────────────────────────────────────────────────────────────────
def _cap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("func") if c else None


def _redis():
    return getattr(_orch, "REDIS", None)


async def _hydrate() -> None:
    if _HYDRATED["v"]:
        return
    _HYDRATED["v"] = True
    r = _redis()
    if not r:
        return
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
            description="Search Hugging Face models. Query: search (str), filter "
                        "(str — HF tag, e.g. 'gguf' or 'text-generation'), sort "
                        "(downloads|likes|lastModified|trendingScore, default downloads), "
                        "limit (int, default 30), fits (node_id|cluster|any — annotate "
                        "each result with a hardware-fit verdict), quant (str — quant to "
                        "assume for the fit estimate, default Q4_K_M). Returns normalised "
                        "rows with an estimated parameter count + fit badge.")
async def cap_catalog_search(search: str = "", filter: str = "text-generation",
                             sort: str = "downloads", limit: int = 30,
                             fits: str = "any", quant: str = "Q4_K_M",
                             trace_id=None):
    await _hydrate()
    params = {
        "limit": max(1, min(int(limit or 30), 100)),
        "sort": sort or "downloads",
        "direction": "-1",
        "full": "true",
    }
    if search:
        params["search"] = search
    if filter:
        params["filter"] = filter
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.get(f"{HF_API}/models", params=params, headers=_hf_headers())
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return {"error": f"HF search failed: {e}", "results": []}

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
        rows.append({
            "id": mid,
            "author": mid.split("/")[0] if "/" in mid else "",
            "downloads": m.get("downloads", 0),
            "likes": m.get("likes", 0),
            "lastModified": m.get("lastModified", ""),
            "pipeline_tag": m.get("pipeline_tag", ""),
            "gated": m.get("gated", False),
            "tags": [t for t in (m.get("tags") or []) if isinstance(t, str)][:12],
            "est_params_b": params_b,
            "requirement": req,
            "fit": _fit_target(req, fits),
        })
    return {"results": rows, "count": len(rows), "fits": fits, "quant": quant}


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


@capability("catalog.nodes", memory="off", silent=True,
            http_method="GET", http_path="/catalog/nodes", http_tags=["catalog"],
            description="Unified per-node view for the catalog: Ollama + vLLM nodes with "
                        "their detected/overridden hardware (VRAM/RAM/GPU), SSH mapping, "
                        "installed models, and any 'slow-hq' class + pinned model.")
async def cap_catalog_nodes(trace_id=None):
    await _hydrate()
    nodes = [_node_view(iid, base) for iid, base in _all_nodes().items()]
    nodes.sort(key=lambda n: (n["backend"], -(n.get("vram_gb") or 0), n["id"]))
    return {"nodes": nodes,
            "cluster": {"best_gpu": _best_gpu_hw(), "best_cpu_ram_gb": _best_cpu_ram()}}


async def _detect_hw(host_id: str) -> dict:
    """Probe VRAM/RAM/GPU/cores on an SSH host. Best-effort; missing tools tolerated."""
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
                        "model store). Delegates to ollama.pull / pxstore.models.pull / "
                        "vllm.server.start.")
async def cap_catalog_install(id: str, instance_id: str, backend: str = "ollama",
                              quant: str = "", via: str = "direct", trace_id=None):
    plan = await cap_catalog_install_plan(id=id, instance_id=instance_id,
                                          backend=backend, quant=quant)
    if not plan.get("ok"):
        return plan
    await emit_event({"type": "catalog.install.start", "id": id,
                      "instance_id": instance_id, "backend": backend, "plan": plan})
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

log.info("catalog: model catalog capabilities loaded")
