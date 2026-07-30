"""
vera_capabilities.py  –  Vera capability library  v3
=====================================================
All capabilities register themselves on import.
Each capability may declare a `ui` block that the harness
renders as a built-in panel widget.

Start standalone:
    python vera_capabilities.py        (runs on :8000)

Or import into your own app:
    from Vera.vera.config import cfg
from Vera.vera.capability_orchestration import APP  # noqa
    import vera_capabilities           # registers all caps
    import uvicorn; uvicorn.run(APP, ...)
"""

import asyncio, base64, hashlib, json, logging, math, os, re, sys, tempfile, textwrap, time, uuid
from datetime import datetime, timezone
from typing import Optional, Any
from urllib.parse import urlparse

import httpx

from Vera.vera.capability_orchestration import (
    APP,                          # noqa re-exported
    OLLAMA_INSTANCES, OLLAMA_MODEL,
    UI_PANELS, register_ui,       # panel registry lives in orchestrator
    capability, emit_event, emit_stream,
    enum_schema,                  # schema= helper: declare multiple-choice arg options
    media_base, media_slot,       # media-node router (STT / TTS / image-gen)
    now_iso, ollama_generate, pick_instance, schedule,
)

from Vera.vera.config import cfg
from Vera.vera.output_formats import apply_format, list_profiles
from Vera.vera.delivery import list_channels

log = logging.getLogger("vera.caps")


# Coerce timeout into an int, accepting formats like 10s, 60m, 1h, etc.
def parse_timeout(t: Any) -> int:
    if isinstance(t, (int, float)):
        return int(t)
    s = str(t).strip().lower()
    if not s:
        return _DEFAULT_TIMEOUT
    # Try to extract a number and unit
    import re
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([smhd])?$", s)
    if not m:
        return _DEFAULT_TIMEOUT  # fallback on unknown format
    num, unit = int(m.group(1)), m.group(2) or "s"
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return int(num * multipliers.get(unit, 1))


# ─────────────────────────────────────────────────────────────────────────────
#  ██  GPU INFERENCE SERVER  (192.168.0.250:8765)
#
#  Actual server endpoints (gpu_inference_server.py):
#    POST /stt                   — Whisper transcription (multipart file)
#    POST /tts                   — Kokoro/Coqui TTS → WAV b64
#    POST /tts/stream            — streaming PCM audio
#    POST /imagine               — Stable Diffusion txt2img
#    GET  /tts/voices            — voice catalogue
#    GET  /sd/loras              — LoRA file list
#    GET  /health                — GPU server health
#    POST /chat/speak            — Ollama LLM + TTS fan-out
#    GET  /chat/text/{sid}       — SSE text token stream
#    POST /duplex/start          — create duplex session
#    POST /duplex/query          — submit query (text or audio)
#    POST /duplex/interrupt/{id} — interrupt current response
#    GET  /duplex/audio/{id}     — persistent PCM audio stream
#    GET  /duplex/text/{id}      — persistent SSE text stream
#    DELETE /duplex/session/{id} — close session
# ─────────────────────────────────────────────────────────────────────────────

GPU_INFER_URL = cfg.GPU_INFER_URL


@capability(
    "gpu.health",
    http_method="GET", http_path="/gpu/health", http_tags=["gpu", "obs"],
    memory="off",
    description="Health status of the GPU inference server. "
                "Output: {status, whisper, stable_diffusion, tts, tts_engine, sample_rate, cuda, gpu}. CUDA).",
)
async def gpu_health(trace_id=None):
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{media_base('')}/health")
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"error": str(e), "status": "unreachable", "url": GPU_INFER_URL}


# ── STT ───────────────────────────────────────────────────────────────────────

@capability(
    "stt.transcribe",
    http_method="POST", http_path="/stt/transcribe", http_tags=["gpu", "stt"],
    memory="auto",
    description="Transcribe audio to text using Whisper on the GPU node. "
                "Input: audio_b64 (base64 WAV/WebM), language (optional, ISO code), task (transcribe|translate). "
                "Output: {text, language, duration_s}. "
                "Pass audio_b64 (base64 audio bytes), mime_type, optional language and translate flag.",
)
async def stt_transcribe(
    audio_b64: str,
    mime_type: str  = "audio/webm",
    language:  str  = "",
    translate: bool = False,
    trace_id=None,
):
    audio_bytes = base64.b64decode(audio_b64)
    files  = {"file": ("audio.webm", audio_bytes, mime_type)}
    data   = {}
    if language:  data["language"] = language
    if translate: data["task"]     = "translate"
    try:
        async with media_slot("stt") as _mn:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(f"{_mn['url']}/stt", files=files, data=data)
                r.raise_for_status()
                resp = r.json()
        return {
            "text":     resp.get("text", ""),
            "language": resp.get("language", ""),
        }
    except Exception as e:
        log.error("stt.transcribe: %s", e)
        return {"error": str(e), "text": ""}


# ── TTS ───────────────────────────────────────────────────────────────────────

@capability(
    "tts.synthesize",
    http_method="POST", http_path="/tts/synthesize", http_tags=["gpu", "tts"],
    memory="off",
    description="Synthesize text to speech on the GPU node. "
                "Input: text (str), voice (voice_id e.g. af_heart), speed (float 0.5-2.0), engine (kokoro|coqui). "
                "Output: {audio_b64, sample_rate, format}. "
                "Returns base64-encoded WAV audio and sample_rate.",
    schema=enum_schema(engine=["kokoro", "coqui"]),
)
async def tts_synthesize(
    text:     str,
    voice:    str   = "af_heart",
    speed:    float = 1.0,
    engine:   str   = "",    # "kokoro" | "coqui" | "" = server default
    language: str   = "",
    trace_id=None,
):
    body: dict = {"text": text, "voice": voice, "speed": speed}
    if engine:   body["engine"]   = engine
    if language: body["language"] = language
    try:
        async with media_slot("tts") as _mn:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(f"{_mn['url']}/tts", json=body)
                r.raise_for_status()
                data = r.json()
        return {
            "audio_b64":   data.get("audio_b64", ""),
            "mime_type":   "audio/wav",
            "voice":       voice,
            "sample_rate": data.get("sample_rate", 22050),
            "format":      data.get("format", "wav"),
        }
    except Exception as e:
        log.error("tts.synthesize: %s", e)
        return {"error": str(e), "audio_b64": ""}


@capability(
    "tts.voices",
    http_method="GET", http_path="/tts/voices", http_tags=["gpu", "tts"],
    memory="off",
    description="List available TTS voices from the GPU inference server. "
                "Output: {engine, voices: [{id, name, lang, gender}]}.",
)
async def list_tts_voices(trace_id=None):
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{media_base('tts')}/tts/voices")
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"error": str(e), "voices": [
            {"id": "af_heart",    "name": "Heart",    "lang": "en-us", "gender": "F"},
            {"id": "af_bella",    "name": "Bella",    "lang": "en-us", "gender": "F"},
            {"id": "af_sarah",    "name": "Sarah",    "lang": "en-us", "gender": "F"},
            {"id": "am_adam",     "name": "Adam",     "lang": "en-us", "gender": "M"},
            {"id": "am_michael",  "name": "Michael",  "lang": "en-us", "gender": "M"},
            {"id": "bf_emma",     "name": "Emma",     "lang": "en-gb", "gender": "F"},
            {"id": "bf_isabella", "name": "Isabella", "lang": "en-gb", "gender": "F"},
            {"id": "bm_george",   "name": "George",   "lang": "en-gb", "gender": "M"},
            {"id": "bm_lewis",    "name": "Lewis",    "lang": "en-gb", "gender": "M"},
        ]}


# ── Stable Diffusion ──────────────────────────────────────────────────────────

# job_id → media-node base URL: image jobs report progress on the node that
# runs them, so image.progress must poll the same node the job landed on.
# Bounded: pruned to the most recent ~200 jobs.
_MEDIA_JOB_BASE: dict = {}


def _track_media_job(job_id: str, base_url: str) -> None:
    if not job_id or not base_url:
        return
    _MEDIA_JOB_BASE[job_id] = base_url
    if len(_MEDIA_JOB_BASE) > 200:
        for k in list(_MEDIA_JOB_BASE)[:-200]:
            _MEDIA_JOB_BASE.pop(k, None)


def _archive_image(image_b64: str, **meta):
    """Fire-and-forget: persist a generated image into the data fabric via the
    images.store cap (if that module is loaded). Never raises into the caller."""
    if not image_b64:
        return
    try:
        import Vera.vera.capability_orchestration as _orch
        cap = _orch.CAPABILITY_REGISTRY.get("images.store")
        if cap:
            asyncio.ensure_future(cap["func"](image_b64=image_b64, **meta))
    except Exception as e:
        log.debug("image archive skipped: %s", e)


@capability(
    "image.generate",
    http_method="POST", http_path="/image/generate", http_tags=["gpu", "sd", "image"],
    memory="on",
    description="Generate an image with Stable Diffusion on the GPU node. "
                "Input: prompt (str), negative_prompt (str), steps (int 10-50), guidance (float), "
                "width/height (int, multiples of 64), seed (int|-1), loras (list of {name,weight}). "
                "Output: {image_b64, format}. "
                "Returns base64-encoded PNG. Use loras as comma-separated 'name:weight' pairs.",
)
async def image_generate(
    prompt:          str,
    negative_prompt: str   = "blurry, low quality, distorted",
    width:           int   = 512,
    height:          int   = 512,
    steps:           int   = 20,
    guidance:        float = 7.5,
    seed:            int   = -1,
    loras:           str   = "",   # e.g. "add_detail:0.8,skin_texture:0.6"
    transparent:     bool  = False,# chroma-key the background out to alpha
    bg_color:        str   = "",   # hex key colour; "" = default green
    chroma_tol:      int   = 80,   # chroma-key aggressiveness (higher removes more)
    store:           bool  = True, # archive the result to the data fabric
    job_id:          str   = "",   # optional: poll image.progress with this id while running
    scheduler:       str   = "",   # per-job sampler (dpmpp|euler|euler_a|unipc|ddim|lcm); "" = default
    trace_id=None,
):
    # Parse loras string → list of {name, weight} dicts
    lora_list = _parse_loras(loras)

    body = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "width": width, "height": height,
        "steps": steps, "guidance": guidance,
        "loras": lora_list,
        "transparent": transparent, "bg_color": bg_color, "chroma_tol": chroma_tol,
        "job_id": job_id, "scheduler": scheduler,
    }
    if seed >= 0: body["seed"] = seed

    try:
        async with media_slot("imagegen") as _mn:
            _track_media_job(job_id, _mn["url"])
            async with httpx.AsyncClient(timeout=300) as c:
                r = await c.post(f"{_mn['url']}/imagine", json=body)
                r.raise_for_status()
                data = r.json()
        img_b64 = data.get("image_b64", "")
        if store:
            _archive_image(img_b64, prompt=prompt, negative_prompt=negative_prompt,
                           seed=int(data.get("seed") or -1), device=data.get("device") or "",
                           steps=steps, guidance=guidance, width=width, height=height,
                           source="txt2img")
        return {
            "image_b64": img_b64,
            "mime_type": "image/png",
            "format":    data.get("format", "png"),
            "seed":      data.get("seed"),
            "device":    data.get("device"),   # 'cuda' | 'cpu' (OOM fallback)
            "steps":     steps,
            "width":     width,
            "height":    height,
        }
    except Exception as e:
        log.error("image.generate: %s", e)
        return {"error": str(e), "image_b64": ""}


@capability(
    "sd.loras",
    http_method="GET", http_path="/sd/loras", http_tags=["gpu", "sd"],
    memory="off",
    description="List available Stable Diffusion LoRA adapters on the GPU server. "
                "Output: {loras: [{name, filename, size_mb}], lora_dir}.",
)
async def list_loras(trace_id=None):
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{media_base('imagegen')}/sd/loras")
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"error": str(e), "loras": []}


CIVITAI_API = os.getenv("CIVITAI_API_BASE", "https://civitai.com/api/v1").rstrip("/")
HF_API      = os.getenv("HF_API_BASE", "https://huggingface.co/api").rstrip("/")


class _MarketUnavailable(Exception):
    """A marketplace endpoint could not be used (geo-block, network, auth)."""
    def __init__(self, msg: str, geo: bool = False):
        super().__init__(msg)
        self.geo = geo


def _market_client(timeout: float = 20):
    """httpx client for marketplace calls. Honours HTTPS_PROXY/HTTP_PROXY (httpx
    trust_env default) plus an explicit CIVITAI_PROXY override for geo-blocked
    hosts. `proxy=` is httpx>=0.26; older builds take `proxies=`."""
    kw = {"timeout": timeout, "follow_redirects": True,
          "headers": {"User-Agent": "vera-image-studio/1.0"}}
    proxy = os.getenv("CIVITAI_PROXY", "").strip()
    if proxy:
        try:
            return httpx.AsyncClient(proxy=proxy, **kw)
        except TypeError:
            return httpx.AsyncClient(proxies=proxy, **kw)
    return httpx.AsyncClient(**kw)


async def _market_get_json(url: str, params=None, headers=None, timeout: float = 20,
                           retries: int = 2, host_label: str = "marketplace"):
    """GET JSON with retries and explicit geo-block/HTML detection. Civitai
    geo-blocks some regions (e.g. UK) — those come back as 401/403/451 or a
    Cloudflare/consent HTML page instead of JSON."""
    last = None
    for attempt in range(max(1, retries)):
        try:
            async with _market_client(timeout) as c:
                r = await c.get(url, params=params, headers=headers or {})
            if r.status_code in (401, 403, 451):
                raise _MarketUnavailable(
                    f"{host_label} refused the request (HTTP {r.status_code} — likely "
                    f"geo-restricted from this server or an invalid/required token). "
                    f"Fixes: set CIVITAI_PROXY (or HTTPS_PROXY) to route around the "
                    f"block, set CIVITAI_API_BASE to a reachable mirror, set "
                    f"CIVITAI_TOKEN, or search provider=huggingface instead.",
                    geo=r.status_code in (403, 451))
            ctype = (r.headers.get("content-type") or "").lower()
            if "json" not in ctype:
                raise _MarketUnavailable(
                    f"{host_label} returned {ctype or 'no content-type'} instead of JSON "
                    f"(HTTP {r.status_code}) — usually a Cloudflare/geo-block interstitial. "
                    f"Set CIVITAI_PROXY / HTTPS_PROXY or use provider=huggingface.",
                    geo=True)
            r.raise_for_status()
            return r.json()
        except _MarketUnavailable:
            raise
        except Exception as e:
            last = e
            if attempt + 1 < retries:
                await asyncio.sleep(0.8 * (attempt + 1))
    raise _MarketUnavailable(f"{host_label} unreachable: {last}")


async def _civitai_search(query: str, limit: int, sort: str, base_model: str,
                          nsfw: bool) -> list:
    params = {"types": "LORA", "limit": min(max(1, int(limit)), 50),
              "sort": sort, "nsfw": str(bool(nsfw)).lower()}
    if query:
        params["query"] = query
    if base_model:
        params["baseModels"] = base_model
    headers = {}
    tok = os.getenv("CIVITAI_TOKEN", "")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    data = await _market_get_json(f"{CIVITAI_API}/models", params=params,
                                  headers=headers, host_label="Civitai")
    out = []
    for m in (data.get("items") or []):
        vers = m.get("modelVersions") or []
        if not vers:
            continue
        v = vers[0]
        files = v.get("files") or []
        f = next((x for x in files if x.get("type") == "Model"), files[0] if files else None)
        if not f:
            continue
        thumb = next((i.get("url") for i in (v.get("images") or []) if i.get("url")), "")
        stats = m.get("stats") or {}
        out.append({
            "name": m.get("name", ""), "id": m.get("id"), "version_id": v.get("id"),
            "base_model": v.get("baseModel", ""), "thumb": thumb,
            "download_url": f.get("downloadUrl") or v.get("downloadUrl", ""),
            "filename": f.get("name", ""),
            "size_mb": round((f.get("sizeKB") or 0) / 1024, 1),
            "downloads": stats.get("downloadCount", 0), "rating": stats.get("rating", 0),
            "nsfw": m.get("nsfw", False), "tags": (m.get("tags") or [])[:6],
            "provider": "civitai",
        })
    return out


_HF_BASE_HINTS = (("xl", "SDXL 1.0"), ("flux", "Flux.1 D"), ("pony", "Pony"),
                  ("v1-5", "SD 1.5"), ("v1.5", "SD 1.5"), ("sd15", "SD 1.5"),
                  ("stable-diffusion-3", "SD 3"), ("stable-diffusion-2", "SD 2.1"))


async def _hf_search(query: str, limit: int, sort: str, base_model: str) -> list:
    """LoRA search on the HuggingFace Hub — not geo-restricted, no key needed.
    Used as the automatic fallback when Civitai is blocked."""
    hf_sort = {"Most Downloaded": "downloads", "Highest Rated": "likes",
               "Newest": "lastModified"}.get(sort, "downloads")
    params = {"filter": "lora", "sort": hf_sort, "direction": "-1",
              "limit": min(max(1, int(limit)), 50), "full": "true"}
    if query:
        params["search"] = query
    headers = {}
    tok = os.getenv("HF_TOKEN", "")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    data = await _market_get_json(f"{HF_API}/models", params=params,
                                  headers=headers, host_label="HuggingFace")
    out = []
    for m in (data if isinstance(data, list) else []):
        mid = m.get("modelId") or m.get("id") or ""
        sibs = [s.get("rfilename") or "" for s in (m.get("siblings") or [])]
        weights = [f for f in sibs if f.lower().endswith((".safetensors", ".pt", ".bin"))
                   and "text_encoder" not in f.lower()]
        if not mid or not weights:
            continue
        # Prefer an explicitly lora-named weight file, else the first one.
        wfile = next((f for f in weights if "lora" in f.lower()), weights[0])
        preview = next((f for f in sibs
                        if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))), "")
        tags = m.get("tags") or []
        base = ""
        base_tag = next((t for t in tags if t.startswith("base_model:")), "")
        for hint, label in _HF_BASE_HINTS:
            if hint in base_tag.lower():
                base = label
                break
        nice_tags = [t for t in tags
                     if ":" not in t and t not in ("lora", "diffusers", "safetensors")][:6]
        out.append({
            "name": mid.split("/")[-1], "id": mid, "version_id": "",
            "base_model": base, "thumb":
                f"https://huggingface.co/{mid}/resolve/main/{preview}" if preview else "",
            "download_url": f"https://huggingface.co/{mid}/resolve/main/{wfile}",
            "filename": wfile.split("/")[-1], "size_mb": 0,
            "downloads": m.get("downloads", 0), "rating": m.get("likes", 0),
            "nsfw": False, "tags": nice_tags, "provider": "huggingface",
        })
    # HF can't filter by SD base model server-side — apply a lenient local filter,
    # but never filter down to nothing.
    if base_model:
        bl = base_model.lower().replace(" ", "")
        kept = [o for o in out if not o["base_model"]
                or bl.startswith(o["base_model"].lower().replace(" ", "")[:4])
                or o["base_model"].lower().replace(" ", "")[:4] in bl]
        if kept:
            out = kept
    return out


# ── LoRA blob store (Garage/S3 via the data-fabric ObjectStore) ──────────────
# LoRAs installed on a GPU node live only on that node's disk. Storing them in the
# shared object store makes them durable and installable onto ANY GPU node, and
# gives us a searchable "my store" provider. Layout in the default bucket:
#   loras/<filename>       the .safetensors weights
#   loras/<stem>.json      a metadata sidecar (name/base_model/tags/thumb/source/…)
_LORA_PREFIX = "loras/"


def _object_store():
    """The data-fabric ObjectStore, or None when the blob store isn't configured."""
    fab = sys.modules.get("data_fabric")
    store = getattr(fab, "OBJECT_STORE", None) if fab else None
    if store is None or getattr(fab, "FABRIC_OBJECT_STORE", "none") == "none":
        return None
    return store


async def _obj_call(fn, *args):
    """Run a blocking boto3/ObjectStore call off the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args))


def _lora_filename(url: str, filename: str = "") -> str:
    name = (filename or "").strip() or os.path.basename(urlparse(url).path) or "lora.safetensors"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if not name.lower().endswith((".safetensors", ".pt", ".bin", ".ckpt")):
        name += ".safetensors"
    return name


async def _lora_store_from_url(store, url: str, filename: str, meta: dict, token: str = "") -> dict:
    """Stream-download a LoRA and put it (plus a JSON metadata sidecar) into the
    object store. Returns {key, filename, size_mb, stored} or {error}."""
    fname = _lora_filename(url, filename)
    stem = fname.rsplit(".", 1)[0]
    key = _LORA_PREFIX + fname
    headers = {"User-Agent": "vera-image-studio/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif "civitai.com" in url and os.getenv("CIVITAI_TOKEN"):
        headers["Authorization"] = f"Bearer {os.getenv('CIVITAI_TOKEN')}"
    elif "huggingface.co" in url and os.getenv("HF_TOKEN"):
        headers["Authorization"] = f"Bearer {os.getenv('HF_TOKEN')}"
    tmp = os.path.join(tempfile.gettempdir(), f"vera_lora_{uuid.uuid4().hex}_{fname}")
    size = 0
    try:
        async with httpx.AsyncClient(timeout=900, follow_redirects=True) as c:
            async with c.stream("GET", url, headers=headers) as r:
                if r.status_code >= 400:
                    return {"error": f"download failed (HTTP {r.status_code}) — needs a token / gated repo?"}
                with open(tmp, "wb") as fh:
                    async for chunk in r.aiter_bytes(1 << 20):
                        fh.write(chunk); size += len(chunk)
        if size < 1024:
            return {"error": "download too small — the URL likely returned an error page, not a model"}
        size_mb = round(size / (1024 * 1024), 1)
        if not await _obj_call(store.upload_file, key, tmp, "application/octet-stream"):
            return {"error": "object-store upload failed (check FABRIC_S3_* and the bucket)"}
        sidecar = {"name": meta.get("name") or stem, "filename": fname, "key": key,
                   "base_model": meta.get("base_model", ""), "tags": meta.get("tags", []),
                   "thumb": meta.get("thumb", ""), "nsfw": bool(meta.get("nsfw", False)),
                   "source": meta.get("source", ""), "source_url": url,
                   "size_mb": size_mb, "stored_at": now_iso()}
        await _obj_call(store.put, _LORA_PREFIX + stem + ".json",
                        json.dumps(sidecar).encode("utf-8"), "application/json")
        return {"key": key, "filename": fname, "size_mb": size_mb, "stored": True,
                "name": sidecar["name"]}
    except Exception as e:
        return {"error": f"store failed: {e}"}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


async def _blob_search(store, query: str, base_model: str, nsfw: bool, limit: int) -> list:
    """Search the object store's LoRA registry by reading the .json sidecars."""
    keys = await _obj_call(store.list_prefix, _LORA_PREFIX)
    q = (query or "").lower().strip()
    bl = (base_model or "").lower().replace(" ", "")
    out = []
    for k in keys:
        if not k.endswith(".json"):
            continue
        raw = await _obj_call(store.get, k)
        if not raw:
            continue
        try:
            m = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        if not nsfw and m.get("nsfw"):
            continue
        hay = " ".join([m.get("name", ""), m.get("filename", ""),
                        " ".join(m.get("tags", []) or []), m.get("base_model", "")]).lower()
        if q and q not in hay:
            continue
        if bl and m.get("base_model") and bl[:4] not in m["base_model"].lower().replace(" ", ""):
            continue
        out.append({
            "name": m.get("name", ""), "id": m.get("key", ""), "version_id": "",
            "base_model": m.get("base_model", ""), "thumb": m.get("thumb", ""),
            "download_url": "", "blob_key": m.get("key", ""),
            "filename": m.get("filename", ""), "size_mb": m.get("size_mb", 0),
            "downloads": 0, "rating": 0, "nsfw": bool(m.get("nsfw", False)),
            "tags": (m.get("tags") or [])[:6], "provider": "blob",
        })
        if len(out) >= max(1, min(int(limit), 100)):
            break
    return out


@capability(
    "sd.lora_search",
    http_method="GET", http_path="/sd/lora_search", http_tags=["gpu", "sd", "lora"],
    memory="off",
    description="Search LoRA sources. Inputs: query (str), limit (int<=50), sort "
                "(Most Downloaded|Highest Rated|Newest), base_model ('SD 1.5', 'SDXL 1.0', "
                "… optional filter), nsfw (bool), provider (auto|civitai|huggingface|blob). "
                "'blob' searches YOUR object-store LoRA library (durable, cross-node). 'auto' "
                "surfaces blob matches first, then tries Civitai and falls back to HuggingFace "
                "when Civitai is geo-blocked/unreachable (it is blocked in some regions, e.g. "
                "the UK). Output: {results:[{name, id, base_model, thumb, download_url, "
                "blob_key, filename, size_mb, downloads, rating, nsfw, tags, provider}], count, "
                "provider_used, warnings}. Env: CIVITAI_TOKEN, CIVITAI_PROXY/HTTPS_PROXY, "
                "CIVITAI_API_BASE, HF_TOKEN, FABRIC_OBJECT_STORE/FABRIC_S3_*.",
    schema=enum_schema(sort=["Most Downloaded", "Highest Rated", "Newest"],
                       provider=["auto", "civitai", "huggingface", "blob"]),
)
async def sd_lora_search(query: str = "", limit: int = 24, sort: str = "Most Downloaded",
                         base_model: str = "", nsfw: bool = False, provider: str = "auto",
                         trace_id=None):
    provider = (provider or "auto").lower()
    warnings: list = []
    results: list = []
    used = ""
    store = _object_store()
    # blob store — your own durable LoRAs, searched first in 'auto'.
    if provider == "blob":
        if not store:
            return {"error": "object store not configured (set FABRIC_OBJECT_STORE=garage + "
                             "FABRIC_S3_*)", "results": [], "count": 0}
        try:
            results = await _blob_search(store, query, base_model, nsfw, limit)
        except Exception as e:
            return {"error": f"blob store search failed: {e}", "results": [], "count": 0}
        return {"results": results, "count": len(results), "provider_used": "blob",
                "warnings": warnings}
    blob_results: list = []
    if provider == "auto" and store:
        try:
            blob_results = await _blob_search(store, query, base_model, nsfw, limit)
            if blob_results:
                warnings.append(f"{len(blob_results)} from your blob store.")
        except Exception:
            pass
    if provider in ("auto", "civitai"):
        try:
            results = await _civitai_search(query, limit, sort, base_model, nsfw)
            used = "civitai"
            if not results and provider == "auto":
                warnings.append("Civitai returned no matches — also searching HuggingFace.")
        except _MarketUnavailable as e:
            if provider == "civitai":
                return {"error": f"Civitai search failed: {e}", "results": [],
                        "count": 0, "geo_blocked": e.geo, "warnings": warnings}
            warnings.append(f"Civitai unavailable → falling back to HuggingFace. ({e})")
        except Exception as e:
            if provider == "civitai":
                return {"error": f"Civitai search failed: {e}", "results": [], "count": 0,
                        "warnings": warnings}
            warnings.append(f"Civitai search failed → falling back to HuggingFace. ({e})")
    if not results and provider in ("auto", "huggingface"):
        try:
            results = await _hf_search(query, limit, sort, base_model)
            used = "huggingface"
        except Exception as e:
            msg = f"HuggingFace search failed: {e}"
            if warnings:  # both providers down — surface everything
                msg = " | ".join(warnings + [msg])
            if not blob_results:
                return {"error": msg, "results": [], "count": 0, "warnings": warnings}
            warnings.append(msg)
    results = blob_results + results
    return {"results": results, "count": len(results),
            "provider_used": used or ("blob" if blob_results else ""), "warnings": warnings}


@capability(
    "sd.lora_install",
    http_method="POST", http_path="/sd/lora_install", http_tags=["gpu", "sd", "lora"],
    memory="off",
    description="Download a LoRA into the GPU node's SD_LORA_DIR so it can be used in "
                "generation. Inputs: url (the .safetensors download URL — e.g. a Civitai "
                "download_url), blob_key (install FROM your object store instead of a URL — "
                "presigned and handed to the GPU node), filename (optional), token (optional "
                "bearer; Civitai downloads may need one, else CIVITAI_TOKEN on the GPU host), "
                "store_blob (bool — also save a durable copy to the object store). Output: "
                "{name, filename, size_mb, blob?} or {error}. Large files can take a while.",
)
async def sd_lora_install(url: str = "", filename: str = "", token: str = "",
                          store_blob: bool = False, blob_key: str = "", trace_id=None):
    store = _object_store()
    src_url = url
    if blob_key:
        if not store:
            return {"error": "object store not configured — cannot install from blob"}
        src_url = await _obj_call(store.presign, blob_key, "get", "", 3600)
        if not src_url:
            return {"error": f"could not presign blob '{blob_key}'"}
        if not filename:
            filename = os.path.basename(blob_key)
    if not src_url:
        return {"error": "url or blob_key required"}
    try:
        async with httpx.AsyncClient(timeout=900) as c:
            r = await c.post(f"{media_base('imagegen')}/sd/loras/download",
                             json={"url": src_url, "filename": filename, "token": token})
            r.raise_for_status()
            res = r.json()
    except Exception as e:
        return {"error": str(e)}
    if isinstance(res, dict) and res.get("error"):
        return res
    # Opt-in: mirror the just-installed LoRA into the durable object store too.
    if store_blob and url and store:
        res["blob"] = await _lora_store_from_url(
            store, url, filename or (res or {}).get("filename", ""),
            {"name": (res or {}).get("name") or filename, "source": "install"}, token)
    return res


@capability(
    "sd.lora_store",
    http_method="POST", http_path="/sd/lora_store", http_tags=["gpu", "sd", "lora"],
    memory="off",
    description="Save a LoRA into Vera's object store (Garage/S3 blob store) for durable, "
                "cross-node storage independent of any single GPU host — Vera streams the file "
                "in and writes a searchable metadata sidecar. Later install it onto any GPU "
                "node with sd.lora_install(blob_key=…), or find it via sd.lora_search "
                "(provider=blob). Inputs: url (str! .safetensors URL), filename, name, "
                "base_model, tags (csv), thumb, nsfw (bool), source, token (optional bearer). "
                "Output: {key, filename, size_mb, stored} or {error}.",
)
async def sd_lora_store(url: str = "", filename: str = "", name: str = "", base_model: str = "",
                        tags: str = "", thumb: str = "", nsfw: bool = False, source: str = "",
                        token: str = "", trace_id=None):
    if not url:
        return {"error": "url required"}
    store = _object_store()
    if not store:
        return {"error": "object store not configured (set FABRIC_OBJECT_STORE=garage + "
                         "FABRIC_S3_ENDPOINT/ACCESS/SECRET)"}
    meta = {"name": name or filename, "base_model": base_model,
            "tags": [t.strip() for t in tags.split(",") if t.strip()],
            "thumb": thumb, "nsfw": bool(nsfw), "source": source or "url"}
    return await _lora_store_from_url(store, url, filename, meta, token)


@capability(
    "sd.lora_store_delete",
    http_method="POST", http_path="/sd/lora_store_delete", http_tags=["gpu", "sd", "lora"],
    memory="off",
    description="Remove a LoRA (weights + metadata sidecar) from the object store. Input: "
                "key (the blob_key, e.g. 'loras/foo.safetensors'). Output: {removed:[keys]}.",
)
async def sd_lora_store_delete(key: str = "", trace_id=None):
    store = _object_store()
    if not store:
        return {"error": "object store not configured"}
    if not key:
        return {"error": "key required"}
    base = key[len(_LORA_PREFIX):] if key.startswith(_LORA_PREFIX) else key
    stem = base.rsplit(".", 1)[0]
    removed = []
    for k in (key if key.startswith(_LORA_PREFIX) else _LORA_PREFIX + key,
              _LORA_PREFIX + stem + ".json"):
        if await _obj_call(store.delete, k):
            removed.append(k)
    return {"removed": removed}


@capability(
    "sd.lora_delete",
    http_method="POST", http_path="/sd/lora_delete", http_tags=["gpu", "sd", "lora"],
    memory="off",
    description="Delete an installed LoRA from the GPU node's SD_LORA_DIR. Input: name "
                "(filename stem). Output: {removed:[filenames]} or {error}.",
)
async def sd_lora_delete(name: str, trace_id=None):
    if not name:
        return {"error": "name required"}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{media_base('imagegen')}/sd/loras/delete", json={"name": name})
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"error": str(e)}


@capability(
    "image.img2img",
    http_method="POST", http_path="/image/img2img", http_tags=["gpu", "sd", "image"],
    memory="on",
    description="Derive a new image from an init image with Stable Diffusion (img2img) "
                "on the GPU node. Input: prompt (str), init_image_b64 (base64 PNG/JPEG), "
                "strength (float 0-1, lower = closer to init), negative_prompt, steps, "
                "guidance, width/height, seed, loras ('name:weight' csv). "
                "Output: {image_b64, mime_type, format}. Use to keep a character "
                "consistent across expression frames. Falls back to image.generate "
                "(txt2img) if the server lacks img2img.",
)
async def image_img2img(
    prompt:          str,
    init_image_b64:  str,
    strength:        float = 0.55,
    negative_prompt: str   = "blurry, low quality, distorted",
    width:           int   = 512,
    height:          int   = 512,
    steps:           int   = 20,
    guidance:        float = 7.5,
    seed:            int   = -1,
    loras:           str   = "",
    transparent:     bool  = False,# chroma-key the background out to alpha
    bg_color:        str   = "",   # hex key colour; "" = default green
    chroma_tol:      int   = 80,   # chroma-key aggressiveness (higher removes more)
    store:           bool  = True, # archive the result to the data fabric
    job_id:          str   = "",   # optional: poll image.progress with this id while running
    scheduler:       str   = "",   # per-job sampler (dpmpp|euler|euler_a|unipc|ddim|lcm); "" = default
    trace_id=None,
):
    lora_list = _parse_loras(loras)

    body = {
        "prompt": prompt,
        "init_image_b64": init_image_b64,
        "strength": strength,
        "negative_prompt": negative_prompt,
        "width": width, "height": height,
        "steps": steps, "guidance": guidance,
        "loras": lora_list,
        "transparent": transparent, "bg_color": bg_color, "chroma_tol": chroma_tol,
        "job_id": job_id, "scheduler": scheduler,
    }
    if seed >= 0:
        body["seed"] = seed

    try:
        async with media_slot("imagegen") as _mn:
            _track_media_job(job_id, _mn["url"])
            async with httpx.AsyncClient(timeout=300) as c:
                r = await c.post(f"{_mn['url']}/img2img", json=body)
                r.raise_for_status()
                data = r.json()
        img_b64 = data.get("image_b64", "")
        if store:
            _archive_image(img_b64, prompt=prompt, negative_prompt=negative_prompt,
                           seed=int(data.get("seed") or -1), device=data.get("device") or "",
                           steps=steps, guidance=guidance, width=width, height=height,
                           source="img2img")
        return {
            "image_b64": img_b64,
            "mime_type": "image/png",
            "format":    data.get("format", "png"),
            "seed":      data.get("seed"),
            "device":    data.get("device"),   # 'cuda' | 'cpu' (OOM fallback)
            "strength":  strength,
        }
    except Exception as e:
        log.error("image.img2img: %s", e)
        return {"error": str(e), "image_b64": ""}


@capability(
    "image.expression",
    http_method="POST", http_path="/image/expression", http_tags=["gpu", "sd", "image"],
    memory="off",
    description="Change ONLY the face of a character image to a new expression, keeping "
                "the background/body/framing identical (face-region img2img composited "
                "back via face detection). Inputs: base_image_b64 (str!), prompt "
                "(identity+expression), negative_prompt, strength (0-1), steps, guidance, "
                "seed, face_box ('x,y,w,h' or omit to auto-detect), pad. Output: "
                "{image_b64, face_detected, face_method, face_box, device}.",
)
async def image_expression(
    base_image_b64:  str,
    prompt:          str,
    negative_prompt: str   = "blurry, low quality, distorted",
    strength:        float = 0.5,
    steps:           int   = 20,
    guidance:        float = 7.5,
    seed:            int   = -1,
    face_box:        str   = "",   # "x,y,w,h" or empty = auto-detect
    pad:             float = 0.45,
    trace_id=None,
):
    body = {
        "base_image_b64": base_image_b64, "prompt": prompt,
        "negative_prompt": negative_prompt, "strength": strength,
        "steps": steps, "guidance": guidance, "pad": pad,
    }
    if seed >= 0:
        body["seed"] = seed
    if face_box:
        try:
            body["face_box"] = [int(v) for v in face_box.split(",")][:4]
        except Exception:
            pass
    try:
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(f"{media_base('imagegen')}/expression", json=body)
            r.raise_for_status()
            data = r.json()
        return {
            "image_b64":     data.get("image_b64", ""),
            "mime_type":     "image/png",
            "face_detected": data.get("face_detected"),
            "face_method":   data.get("face_method"),
            "face_box":      data.get("face_box"),
            "device":        data.get("device"),
            "format":        "png",
        }
    except Exception as e:
        log.error("image.expression: %s", e)
        return {"error": str(e), "image_b64": ""}


@capability(
    "image.sd_capabilities",
    http_method="GET", http_path="/image/sd_capabilities", http_tags=["gpu", "sd"],
    memory="off",
    description="Report which Stable Diffusion generation tiers the GPU server can "
                "serve right now (txt2img / img2img / controlnet / talking_head) so "
                "callers can pick the best-available path and degrade gracefully. "
                "Output: {txt2img, img2img, controlnet, talking_head, model, device}.",
)
async def image_sd_capabilities(trace_id=None):
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{media_base('imagegen')}/sd/capabilities")
            r.raise_for_status()
            return r.json()
    except Exception as e:
        # Server too old to expose /sd/capabilities → assume txt2img only.
        return {
            "txt2img": True, "img2img": False, "controlnet": False,
            "talking_head": False, "error": str(e),
        }


@capability(
    "image.progress",
    http_method="GET", http_path="/image/progress", http_tags=["gpu", "sd", "image"],
    memory="off", silent=True,
    description="Live progress of an in-flight GPU image job that was started with a "
                "job_id (image.generate / image.img2img / image.pose / image.ipadapter, "
                "and the spritegen.generate_* caps). Input: job_id (str!). Output: "
                "{phase: queue|diffusion|chroma-key|rembg|upscale|done|error, step, total, "
                "preview_b64?} — preview_b64 is a small approximate render of the "
                "diffusion state, refreshed every few steps, so a UI can show the image "
                "forming. Returns {phase:'unknown'} until the GPU node has seen the job.",
)
async def image_progress(job_id: str = "", trace_id=None):
    if not job_id:
        return {"error": "job_id required", "phase": "unknown"}
    try:
        # Poll the node the job actually started on (see _MEDIA_JOB_BASE).
        base = _MEDIA_JOB_BASE.get(job_id) or media_base("imagegen")
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(f"{base}/progress/{job_id}")
            if r.status_code == 404:
                return {"phase": "unknown", "job_id": job_id}
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"error": str(e), "phase": "unknown", "job_id": job_id}


def _parse_loras(loras: str):
    out = []
    for part in (loras or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, _, weight = part.partition(":")
            out.append({"name": name.strip(), "weight": float(weight.strip() or 1.0)})
        else:
            out.append({"name": part, "weight": 1.0})
    return out


@capability(
    "image.rembg",
    http_method="POST", http_path="/image/rembg", http_tags=["gpu", "sd", "image"],
    memory="off",
    description="Remove an image's background to transparent RGBA via rembg/SAM2 on the GPU "
                "node (u2net by default). Input: image_b64 (base64 PNG/JPEG), model "
                "(u2net|u2netp|isnet-general-use|...), alpha_matting (bool, refine soft/hair "
                "edges), fg_threshold/bg_threshold/erode (alpha-matting tuning), post_process "
                "(bool, clean the mask). Output: {image_b64, mime_type, device}. Returns {error} "
                "if the server lacks rembg — callers should chroma-key instead.",
)
async def image_rembg(image_b64: str, model: str = "u2net", alpha_matting: bool = False,
                      fg_threshold: int = 240, bg_threshold: int = 10, erode: int = 10,
                      post_process: bool = False, trace_id=None):
    try:
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(f"{media_base('imagegen')}/rembg",
                             json={"image_b64": image_b64, "model": model,
                                   "alpha_matting": alpha_matting, "fg_threshold": fg_threshold,
                                   "bg_threshold": bg_threshold, "erode": erode,
                                   "post_process": post_process})
            r.raise_for_status()
            data = r.json()
        return {"image_b64": data.get("image_b64", ""), "mime_type": "image/png",
                "device": data.get("device")}
    except Exception as e:
        log.error("image.rembg: %s", e)
        return {"error": str(e), "image_b64": ""}


@capability(
    "image.pose",
    http_method="POST", http_path="/image/pose", http_tags=["gpu", "sd", "image"],
    memory="on",
    description="Generate an image conditioned on a pose via ControlNet OpenPose on the GPU "
                "node. Inputs: prompt, control_image_b64 (a pose/skeleton PNG) OR ref_image_b64 "
                "(derive the OpenPose skeleton from it), ref_image_b64 (also used for IP-Adapter "
                "identity when given), negative_prompt, strength, steps, guidance, seed, "
                "width/height, loras ('name:weight' csv), transparent, bg_color. Output: "
                "{image_b64, device}. Returns {error} if ControlNet is not installed.",
)
async def image_pose(
    prompt:          str,
    control_image_b64: str = "",
    ref_image_b64:   str = "",
    negative_prompt: str   = "blurry, low quality, distorted",
    strength:        float = 1.0,
    steps:           int   = 24,
    guidance:        float = 7.5,
    seed:            int   = -1,
    width:           int   = 768,
    height:          int   = 768,
    loras:           str   = "",
    transparent:     bool  = False,
    bg_color:        str   = "",
    chroma_tol:      int   = 80,
    store:           bool  = False,
    job_id:          str   = "",
    scheduler:       str   = "",   # per-job sampler (dpmpp|euler|euler_a|unipc|ddim|lcm); "" = default
    trace_id=None,
):
    body = {
        "prompt": prompt, "control_image_b64": control_image_b64,
        "ref_image_b64": ref_image_b64, "negative_prompt": negative_prompt,
        "strength": strength, "width": width, "height": height,
        "steps": steps, "guidance": guidance, "loras": _parse_loras(loras),
        "transparent": transparent, "bg_color": bg_color, "chroma_tol": chroma_tol,
        "job_id": job_id, "scheduler": scheduler,
    }
    if seed >= 0:
        body["seed"] = seed
    try:
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(f"{media_base('imagegen')}/controlnet/pose", json=body)
            r.raise_for_status()
            data = r.json()
        img_b64 = data.get("image_b64", "")
        if store:
            _archive_image(img_b64, prompt=prompt, negative_prompt=negative_prompt,
                           seed=int(data.get("seed") or -1), device=data.get("device") or "",
                           steps=steps, guidance=guidance, width=width, height=height,
                           source="controlnet")
        return {"image_b64": img_b64, "mime_type": "image/png", "device": data.get("device")}
    except Exception as e:
        log.error("image.pose: %s", e)
        return {"error": str(e), "image_b64": ""}


@capability(
    "image.ipadapter",
    http_method="POST", http_path="/image/ipadapter", http_tags=["gpu", "sd", "image"],
    memory="on",
    description="Generate an identity-locked image from a reference via IP-Adapter on the GPU "
                "node (keeps face/clothing/proportions consistent across new poses). Inputs: "
                "prompt, ref_image_b64 (str!), scale (0-1 identity strength), negative_prompt, "
                "steps, guidance, seed, width/height, loras, transparent, bg_color, "
                "control_image_b64 (optional, combine with ControlNet pose). Output: "
                "{image_b64, device}. Returns {error} if IP-Adapter is not installed.",
)
async def image_ipadapter(
    prompt:          str,
    ref_image_b64:   str,
    scale:           float = 0.6,
    control_image_b64: str = "",
    negative_prompt: str   = "blurry, low quality, distorted",
    steps:           int   = 24,
    guidance:        float = 7.5,
    seed:            int   = -1,
    width:           int   = 768,
    height:          int   = 768,
    loras:           str   = "",
    transparent:     bool  = False,
    bg_color:        str   = "",
    chroma_tol:      int   = 80,
    store:           bool  = False,
    job_id:          str   = "",
    scheduler:       str   = "",   # per-job sampler (dpmpp|euler|euler_a|unipc|ddim|lcm); "" = default
    trace_id=None,
):
    body = {
        "prompt": prompt, "ref_image_b64": ref_image_b64, "scale": scale,
        "control_image_b64": control_image_b64, "negative_prompt": negative_prompt,
        "width": width, "height": height, "steps": steps, "guidance": guidance,
        "loras": _parse_loras(loras), "transparent": transparent, "bg_color": bg_color,
        "chroma_tol": chroma_tol, "job_id": job_id, "scheduler": scheduler,
    }
    if seed >= 0:
        body["seed"] = seed
    try:
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(f"{media_base('imagegen')}/ipadapter", json=body)
            r.raise_for_status()
            data = r.json()
        img_b64 = data.get("image_b64", "")
        if store:
            _archive_image(img_b64, prompt=prompt, negative_prompt=negative_prompt,
                           seed=int(data.get("seed") or -1), device=data.get("device") or "",
                           steps=steps, guidance=guidance, width=width, height=height,
                           source="ipadapter")
        return {"image_b64": img_b64, "mime_type": "image/png", "device": data.get("device")}
    except Exception as e:
        log.error("image.ipadapter: %s", e)
        return {"error": str(e), "image_b64": ""}


@capability(
    "image.upscale",
    http_method="POST", http_path="/image/upscale", http_tags=["gpu", "sd", "image"],
    memory="off",
    description="Upscale an image with Real-ESRGAN on the GPU node. Inputs: image_b64 (str!), "
                "scale (2|4), model (RealESRGAN_x4plus|RealESRGAN_x4plus_anime_6B|...). Output: "
                "{image_b64, device}. Returns {error} if ESRGAN is not installed — callers "
                "should fall back to a Lanczos resize.",
    schema=enum_schema(scale=[2, 4]),
)
async def image_upscale(image_b64: str, scale: int = 4, model: str = "", trace_id=None):
    try:
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(f"{media_base('imagegen')}/upscale",
                             json={"image_b64": image_b64, "scale": scale, "model": model})
            r.raise_for_status()
            data = r.json()
        return {"image_b64": data.get("image_b64", ""), "mime_type": "image/png",
                "device": data.get("device")}
    except Exception as e:
        log.error("image.upscale: %s", e)
        return {"error": str(e), "image_b64": ""}


@capability(
    "image.thumbnail",
    http_method="POST", http_path="/image/thumbnail", http_tags=["gpu", "sd", "image"],
    memory="on",
    description="Generate a video-thumbnail-sized image with a text title/subtitle "
                "overlaid (text is drawn with PIL on the GPU node, NOT by SD which can't "
                "spell). Inputs: prompt, negative_prompt, preset "
                "(youtube|youtube_hd|shorts|square|twitter|og) or width/height, steps, "
                "guidance, seed, loras, title, subtitle, position (top|center|bottom), "
                "text_color, stroke_color. Output: {image_b64, width, height, device}. "
                "Archived to the data fabric (source=thumbnail).",
    schema=enum_schema(
        preset=["youtube", "youtube_hd", "shorts", "square", "twitter", "og"],
        position=["top", "center", "bottom"],
    ),
)
async def image_thumbnail(
    prompt:          str,
    negative_prompt: str   = "blurry, low quality, distorted",
    preset:          str   = "youtube",
    width:           int   = 0,
    height:          int   = 0,
    steps:           int   = 24,
    guidance:        float = 7.5,
    seed:            int   = -1,
    loras:           str   = "",
    title:           str   = "",
    subtitle:        str   = "",
    position:        str   = "bottom",
    text_color:      str   = "#ffffff",
    stroke_color:    str   = "#000000",
    store:           bool  = True,
    trace_id=None,
):
    lora_list = []
    for part in (loras or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, _, weight = part.partition(":")
            lora_list.append({"name": name.strip(), "weight": float(weight.strip() or 1.0)})
        else:
            lora_list.append({"name": part, "weight": 1.0})

    body = {
        "prompt": prompt, "negative_prompt": negative_prompt,
        "preset": preset, "width": width, "height": height,
        "steps": steps, "guidance": guidance, "loras": lora_list,
        "title": title, "subtitle": subtitle, "position": position,
        "text_color": text_color, "stroke_color": stroke_color,
    }
    if seed >= 0:
        body["seed"] = seed
    try:
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(f"{media_base('imagegen')}/thumbnail", json=body)
            r.raise_for_status()
            data = r.json()
        img_b64 = data.get("image_b64", "")
        if store and img_b64:
            _archive_image(img_b64, prompt=(title + " — " + prompt).strip(" —"),
                           negative_prompt=negative_prompt, seed=seed,
                           device=data.get("device") or "",
                           width=data.get("width") or width,
                           height=data.get("height") or height, source="thumbnail")
        return {"image_b64": img_b64, "mime_type": "image/png",
                "width": data.get("width"), "height": data.get("height"),
                "device": data.get("device"), "format": "png"}
    except Exception as e:
        log.error("image.thumbnail: %s", e)
        return {"error": str(e), "image_b64": ""}


# ── Chat + Speak (LLM → TTS fan-out) ─────────────────────────────────────────

@capability(
    "gpu.chat_speak",
    http_method="POST", http_path="/gpu/chat/speak", http_tags=["gpu", "tts", "llm"],
    memory="on",
    description="Send a prompt to Ollama on the GPU server; response streams as PCM audio + SSE text. "
                "Input: prompt (str), model (str), voice (voice_id), speed (float), engine (str), session_id (str). "
                "Output: {url, text_url, body, note}. "
                "Returns session_id for GET /chat/text/{session_id}.",
)
async def gpu_chat_speak(
    prompt:     str,
    model:      str   = "llama3.2",
    voice:      str   = "af_heart",
    speed:      float = 1.0,
    engine:     str   = "",
    session_id: str   = "",
    trace_id=None,
):
    body: dict = {"prompt": prompt, "model": model, "voice": voice, "speed": speed}
    if engine:     body["engine"]     = engine
    if session_id: body["session_id"] = session_id
    try:
        # This endpoint returns streaming PCM — we just start it and return the session_id
        # The caller should separately connect to /chat/text/{session_id} for text tokens
        # and stream audio from /chat/speak directly.
        # Resolve the media node ONCE — the returned URLs are session-sticky and
        # must all point at the same server.
        base = media_base("tts")
        async with httpx.AsyncClient(timeout=10) as c:
            # HEAD request to validate connectivity first
            h = await c.head(f"{base}/health")
        return {
            "url":        f"{base}/chat/speak",
            "text_url":   f"{base}/chat/text/{{session_id}}",
            "body":       body,
            "note":       "POST body to url for audio stream; GET text_url for SSE tokens",
        }
    except Exception as e:
        return {"error": str(e)}


# ── Duplex voice session ──────────────────────────────────────────────────────

@capability(
    "gpu.duplex_start",
    http_method="POST", http_path="/gpu/duplex/start", http_tags=["gpu", "tts", "voice"],
    memory="off",
    description="Create a persistent duplex voice session on the GPU server. "
                "Output: {session_id, audio_url, text_url, query_url}. "
                "Returns session_id for use with gpu.duplex_query and stream endpoints.",
)
async def gpu_duplex_start(trace_id=None):
    try:
        # Resolve ONCE and remember the node per session — duplex sessions are
        # stateful on the server, so every follow-up call must hit the same one.
        base = media_base("tts")
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{base}/duplex/start")
            r.raise_for_status()
            data = r.json()
        sid = data.get("session_id", "")
        if sid:
            _DUPLEX_SESSION_BASE[sid] = base
        return {
            "session_id": sid,
            "audio_url":  f"{base}/duplex/audio/{sid}",
            "text_url":   f"{base}/duplex/text/{sid}",
            "query_url":  f"{base}/duplex/query",
        }
    except Exception as e:
        return {"error": str(e)}


# Duplex sessions are stateful on the media server: remember which node each
# session was started on so query/interrupt route to the same one.
_DUPLEX_SESSION_BASE: dict = {}


def _duplex_base(session_id: str) -> str:
    return _DUPLEX_SESSION_BASE.get(session_id) or media_base("tts")


@capability(
    "gpu.duplex_query",
    http_method="POST", http_path="/gpu/duplex/query", http_tags=["gpu", "tts", "voice"],
    memory="on",
    description="Submit text or audio to a live duplex voice session. "
                "Input: session_id (str), text (str), audio_b64 (base64 WebM — triggers Whisper STT), "
                "model (str), voice (voice_id), speed (float). "
                "Output: {session_id, query, status}. "
                "Interrupts any in-progress response. audio_b64 triggers Whisper STT first.",
)
async def gpu_duplex_query(
    session_id: str,
    text:       str   = "",
    audio_b64:  str   = "",
    model:      str   = "llama3.2",
    voice:      str   = "af_heart",
    speed:      float = 1.0,
    trace_id=None,
):
    body: dict = {"session_id": session_id, "model": model, "voice": voice, "speed": speed}
    if text:      body["text"]      = text
    if audio_b64: body["audio_b64"] = audio_b64
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{_duplex_base(session_id)}/duplex/query", json=body)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"error": str(e)}


@capability(
    "gpu.duplex_interrupt",
    http_method="POST", http_path="/gpu/duplex/interrupt", http_tags=["gpu", "tts", "voice"],
    memory="off",
    description="Immediately interrupt the current TTS response in a duplex session. "
                "Input: session_id (str). Output: {status}.",
)
async def gpu_duplex_interrupt(session_id: str, trace_id=None):
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(f"{_duplex_base(session_id)}/duplex/interrupt/{session_id}")
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"error": str(e)}





register_ui(
    "whisper-stt",
    "Speech → Text",
    "mic",
    """
<div style="display:flex;flex-direction:column;gap:12px">
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
    <button id="wRecBtn" class="btn primary" onclick="whisperToggle(,
    ui_caps=['stt.transcribe'])">⏺ Record</button>
    <button class="btn" onclick="whisperUpload()">📂 Upload File</button>
    <input type="file" id="wFile" accept="audio/*,video/*" style="display:none" onchange="whisperFromFile(this)">
    <select id="wLang" style="width:120px">
      <option value="">Auto-detect</option>
      <option value="en">English</option>
      <option value="fr">French</option>
      <option value="de">German</option>
      <option value="es">Spanish</option>
      <option value="ja">Japanese</option>
      <option value="zh">Chinese</option>
    </select>
    <label style="display:flex;align-items:center;gap:5px;font-size:11px;color:var(--dim2)">
      <input type="checkbox" id="wTranslate"> Translate to EN
    </label>
  </div>
  <div id="wViz" style="height:40px;background:var(--bg0);border:1px solid var(--border);border-radius:4px;display:flex;align-items:center;justify-content:center">
    <span style="color:var(--dim);font-size:11px;font-family:var(--mono)">idle</span>
  </div>
  <div id="wStatus" class="status-bar"></div>
  <div style="font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px">Transcript</div>
  <textarea id="wResult" style="min-height:120px;font-size:12px" placeholder="Transcript will appear here…" readonly></textarea>
  <div style="display:flex;gap:8px">
    <button class="btn sm" onclick="navigator.clipboard.writeText(document.getElementById('wResult').value)">Copy</button>
    <button class="btn sm" onclick="document.getElementById('wResult').value=''">Clear</button>
    <button class="btn sm teal" onclick="whisperSendToLLM()">→ Send to LLM</button>
  </div>
</div>
""",
    """
(function(){
  let recorder=null, chunks=[], stream=null, recording=false;
  const vizEl = ()=>document.getElementById('wViz');
  const statEl= ()=>document.getElementById('wStatus');

  window.whisperToggle = async function() {
    if (!recording) {
      try {
        stream = await navigator.mediaDevices.getUserMedia({audio:true});
        recorder = new MediaRecorder(stream);
        chunks = [];
        recorder.ondataavailable = e=>{ if(e.data.size>0) chunks.push(e.data); };
        recorder.onstop = whisperProcess;
        recorder.start(200);
        recording = true;
        document.getElementById('wRecBtn').textContent='⏹ Stop';
        document.getElementById('wRecBtn').classList.add('danger');
        vizEl().innerHTML='<div style="display:flex;gap:3px;align-items:center" id="wBars">'
          +Array(16).fill(0).map((_,i)=>`<div style="width:4px;height:20px;background:var(--err);border-radius:2px;animation:wbar .6s ${i*.04}s infinite alternate"></div>`).join('')
          +'</div>';
        if(!document.getElementById('wBarStyle')){
          const s=document.createElement('style');
          s.id='wBarStyle';
          s.textContent='@keyframes wbar{0%{height:4px}100%{height:32px}}';
          document.head.appendChild(s);
        }
        statEl().textContent='Recording…';
        statEl().className='status-bar warn';
      } catch(e){ statEl().textContent='Mic error: '+e.message; statEl().className='status-bar err'; }
    } else {
      recorder.stop();
      stream.getTracks().forEach(t=>t.stop());
      recording=false;
      document.getElementById('wRecBtn').textContent='⏺ Record';
      document.getElementById('wRecBtn').classList.remove('danger');
      vizEl().innerHTML='<span style="color:var(--dim);font-size:11px;font-family:var(--mono)">processing…</span>';
      statEl().textContent='Processing…';
      statEl().className='status-bar';
    }
  };

  window.whisperUpload = function(){ document.getElementById('wFile').click(); };

  window.whisperFromFile = async function(inp) {
    const file = inp.files[0]; if(!file) return;
    statEl().textContent='Reading file…'; statEl().className='status-bar';
    const ab = await file.arrayBuffer();
    const b64 = btoa(String.fromCharCode(...new Uint8Array(ab)));
    await whisperSubmit(b64, file.type||'audio/webm');
  };

  async function whisperProcess() {
    const blob = new Blob(chunks, {type:'audio/webm'});
    const ab   = await blob.arrayBuffer();
    const b64  = btoa(String.fromCharCode(...new Uint8Array(ab)));
    await whisperSubmit(b64, 'audio/webm');
  }

  async function whisperSubmit(b64, mime) {
    statEl().textContent='Transcribing…'; statEl().className='status-bar';
    const lang = document.getElementById('wLang').value;
    const trans= document.getElementById('wTranslate').checked;
    try {
      const res = await callCapRaw('stt.transcribe', {
        audio_b64: b64, mime_type: mime,
        language: lang, translate: trans
      });
      const text = res?.text||res?.content?.text||'';
      document.getElementById('wResult').value = text;
      statEl().textContent = `✓ Done${res?.language?' ['+res.language+']':''}`+(res?.duration?` · ${res.duration.toFixed(1)}s`:'');
      statEl().className='status-bar ok';
    } catch(e){
      statEl().textContent='Error: '+e.message; statEl().className='status-bar err';
    }
    vizEl().innerHTML='<span style="color:var(--dim);font-size:11px;font-family:var(--mono)">idle</span>';
  }

  window.whisperSendToLLM = function() {
    const text = document.getElementById('wResult').value;
    if(!text) return;
    const el = document.getElementById('llmPrompt');
    if(el){ el.value=text; switchTab('dashboard'); }
  };
})();
""",
    ui_caps=['stt.transcribe'],
    mode="inject",
    tab_order=10,
)


register_ui(
    "stable-diffusion",
    "Image Gen",
    "art",
    """
<div style="display:flex;flex-direction:column;gap:12px">
  <div class="g2">
    <div style="display:flex;flex-direction:column;gap:9px">
      <div>
        <div style="font-size:10px;color:var(--dim,
    ui_caps=['image.generate', 'sd.loras']);text-transform:uppercase;letter-spacing:.8px;margin-bottom:5px">Prompt</div>
        <textarea id="sdPrompt" style="height:80px;font-size:12px" placeholder="A cinematic photo of…"></textarea>
      </div>
      <div>
        <div style="font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:5px">Negative Prompt</div>
        <textarea id="sdNeg" style="height:50px;font-size:11px">blurry, low quality, distorted, watermark</textarea>
      </div>
      <div class="g2">
        <div class="row"><label>W</label><input id="sdW" type="number" value="512" step="64" min="256" max="1024" style="flex:1"></div>
        <div class="row"><label>H</label><input id="sdH" type="number" value="512" step="64" min="256" max="1024" style="flex:1"></div>
        <div class="row"><label>Steps</label><input id="sdSteps" type="number" value="20" min="5" max="50" style="flex:1"></div>
        <div class="row"><label>CFG</label><input id="sdCfg" type="number" value="7.5" step="0.5" min="1" max="20" style="flex:1"></div>
      </div>
      <div class="row"><label>Seed</label><input id="sdSeed" value="-1" style="flex:1"><button class="btn sm" onclick="document.getElementById('sdSeed').value=Math.floor(Math.random()*99999999)">🎲</button></div>
      <div class="row"><label>LoRAs</label><input id="sdLoras" placeholder="name:0.8,other:0.6" style="flex:1"><button class="btn sm" onclick="sdLoadLoras()">↻</button></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn primary" onclick="sdGenerate()">🎨 Generate</button>
        <button class="btn sm" onclick="sdDownload()">⬇</button>
      </div>
    </div>
    <div>
      <div id="sdImgWrap" style="background:var(--bg0);border:1px solid var(--border);border-radius:6px;min-height:260px;display:flex;align-items:center;justify-content:center;overflow:hidden">
        <span style="color:var(--dim);font-size:11px">Image will appear here</span>
      </div>
      <div id="sdStatus" class="status-bar" style="margin-top:7px"></div>
      <div id="sdSeedOut" style="font-family:var(--mono);font-size:10px;color:var(--dim2);margin-top:4px"></div>
    </div>
  </div>
</div>
""",
    r"""
(function(){
  let lastB64='';

  window.sdGenerate = async function() {
    const prompt = document.getElementById('sdPrompt').value.trim();
    if (!prompt) return;
    sdSt('⟳ Generating…','');
    sdImg('');
    const res = await callCapRaw('image.generate',{
      prompt, negative_prompt: document.getElementById('sdNeg').value,
      width:  +document.getElementById('sdW').value,
      height: +document.getElementById('sdH').value,
      steps:  +document.getElementById('sdSteps').value,
      guidance: +document.getElementById('sdCfg').value,
      seed:   +document.getElementById('sdSeed').value,
      loras:  document.getElementById('sdLoras').value,
    });
    if (res.error) { sdSt('✗ '+res.error,'err'); return; }
    lastB64 = res.image_b64;
    sdImg(lastB64);
    sdSt('✓ Done · seed:'+res.seed,'ok');
    document.getElementById('sdSeedOut').textContent = 'seed: '+res.seed;
  };

  window.sdDownload = function() {
    if (!lastB64) return;
    const a=document.createElement('a'); a.download='vera_sd.png';
    a.href='data:image/png;base64,'+lastB64; a.click();
  };

  window.sdLoadLoras = async function() {
    try {
      const r=await fetch(window._veraBase+'/sd/loras');
      const d=await r.json();
      // Show available loras as hint text
      const inp=document.getElementById('sdLoras');
      const names=(d.loras||[]).map(l=>l.name||l).join(', ');
      inp.placeholder = names ? 'Available: '+names.slice(0,40) : 'name:weight,...';
    } catch(e){ console.warn('loras',e); }
  };

  function sdSt(msg,cls){ const el=document.getElementById('sdStatus'); el.textContent=msg; el.className='status-bar'+(cls?' '+cls:''); }
  function sdImg(b64){ const w=document.getElementById('sdImgWrap'); w.innerHTML=b64 ? `<img src="data:image/png;base64,${b64}" style="max-width:100%;max-height:380px;object-fit:contain">` : '<span style="color:var(--dim);font-size:11px">Image will appear here</span>'; }
})();
""",
    ui_caps=['image.generate', 'sd.loras'],
    mode="inject",
    tab_order=20,
)


register_ui(
    "kokoro-tts",
    "Text to Speech",
    "",
    """
<div style="display:flex;flex-direction:column;gap:12px">
  <textarea id="ttsText" style="min-height:100px;font-size:12px" placeholder="Enter text to synthesize…"></textarea>
  <div class="g2">
    <div>
      <div class="row"><label>Voice</label>
        <select id="ttsVoice" style="flex:1">
          <option value="af_heart">af_heart (F warm)</option>
          <option value="af_bella">af_bella (F soft)</option>
          <option value="af_sarah">af_sarah (F clear)</option>
          <option value="am_adam">am_adam (M deep)</option>
          <option value="am_michael">am_michael (M natural)</option>
          <option value="bf_emma">bf_emma (F British)</option>
          <option value="bf_isabella">bf_isabella (F British warm)</option>
          <option value="bm_george">bm_george (M British)</option>
          <option value="bm_lewis">bm_lewis (M British deep)</option>
        </select>
      </div>
      <div class="row">
        <label>Speed</label>
        <input type="range" id="ttsSpeed" min="0.5" max="2" step="0.05" value="1" style="flex:1;padding:0" oninput="document.getElementById('ttsSpeedVal').textContent=this.value">
        <span id="ttsSpeedVal" style="min-width:28px;text-align:right;font-family:var(--mono);font-size:11px;color:var(--acc)">1</span>
      </div>
    </div>
    <div>
      <div style="display:flex;gap:8px;margin-top:4px;flex-wrap:wrap">
        <button class="btn primary" onclick="ttsSynthesize()">🔊 Synthesize</button>
        <button class="btn sm" onclick="ttsLoadVoices()">↻ Voices</button>
        <button class="btn sm" onclick="ttsDownload()">⬇ WAV</button>
      </div>
      <div id="ttsStatus" class="status-bar" style="margin-top:8px"></div>
    </div>
  </div>
  <div id="ttsPlayerWrap" style="display:none">
    <audio id="ttsPlayer" controls style="width:100%;margin-top:4px"></audio>
  </div>
</div>
""",
    """
(function(){
  let lastAudioB64='', lastMime='audio/wav';

  window.ttsSynthesize = async function() {
    const text  = document.getElementById('ttsText').value.trim();
    if (!text) return;
    const voice = document.getElementById('ttsVoice').value;
    const speed = parseFloat(document.getElementById('ttsSpeed').value);
    const st    = document.getElementById('ttsStatus');
    st.textContent='Synthesizing…'; st.className='status-bar';
    document.getElementById('ttsPlayerWrap').style.display='none';
    try {
      const res = await callCapRaw('tts.synthesize',{text, voice, speed});
      if (res.error) throw new Error(res.error);
      if (!res.audio_b64) throw new Error('Server returned no audio data');
      lastAudioB64 = res.audio_b64;
      lastMime     = res.mime_type || 'audio/wav';
      const bytes  = Uint8Array.from(atob(lastAudioB64), c=>c.charCodeAt(0));
      const blob   = new Blob([bytes],{type:lastMime});
      const url    = URL.createObjectURL(blob);
      const player = document.getElementById('ttsPlayer');
      player.src   = url;
      document.getElementById('ttsPlayerWrap').style.display='block';
      player.play();
      st.textContent=`✓ Done · voice:${voice}`;
      st.className='status-bar ok';
    } catch(e){
      st.textContent='Error: '+e.message; st.className='status-bar err';
    }
  };

  window.ttsDownload = function() {
    if (!lastAudioB64) return;
    const bytes = Uint8Array.from(atob(lastAudioB64), c=>c.charCodeAt(0));
    const blob  = new Blob([bytes],{type:lastMime});
    const a     = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'vera_tts.wav';
    a.click();
  };

  window.ttsLoadVoices = async function() {
    try {
      const res  = await fetch(window._veraBase+'/tts/voices');
      const data = await res.json();
      const sel  = document.getElementById('ttsVoice');
      if (data.voices && data.voices.length) {
        sel.innerHTML = data.voices.map(v=>`<option value="${v}">${v}</option>`).join('');
      }
    } catch(e){ console.warn('voice list fetch failed', e); }
  };
})();
""",
    ui_caps=['tts.synthesize'],
    mode="inject",
    tab_order=30,
)

# ─────────────────────────────────────────────────────────────────────────────
#  ██  LLM GROUP
# ─────────────────────────────────────────────────────────────────────────────

async def _llm_files_context(files, session_id: str = "",
                             *, max_per_file: int = 20000, max_files: int = 8) -> str:
    """Read the given file(s)/portions and format them as a grounding CONTEXT block
    to prepend to a generation prompt. `files` accepts a path, a 'path:start-end'
    portion, a newline/comma list of those, a JSON list, or dicts {path,start,end}.
    Reads are SANDBOX-AWARE via the ide.fs.read cap (so a './output_*.txt' from a
    loop step resolves in the session workspace). Best-effort — skips what it can't
    read; returns '' when nothing usable."""
    specs = files
    if isinstance(specs, str):
        s = specs.strip()
        if s[:1] in ("[", "{"):
            try:
                specs = json.loads(s)
            except Exception:
                specs = [s]
        elif s:
            specs = ([p.strip() for p in re.split(r"[\n,]+", s) if p.strip()]
                     if ("\n" in s or "," in s) else [s])
        else:
            specs = []
    if isinstance(specs, dict):
        specs = [specs]
    if not isinstance(specs, list) or not specs:
        return ""
    try:
        from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY as _REG
    except Exception:
        return ""
    _rd = _REG.get("ide.fs.read")
    _rdfn = (_rd.get("raw") if _rd else None)
    if not _rdfn:
        return ""
    blocks: list = []
    for spec in specs[:max_files]:
        path, start, end = "", 0, 0
        if isinstance(spec, dict):
            path = str(spec.get("path") or spec.get("file") or "").strip()
            start = int(spec.get("start") or 0); end = int(spec.get("end") or 0)
        else:
            path = str(spec).strip()
            m = re.match(r"^(.*?):(\d+)-(\d+)$", path)
            if m:
                path, start, end = m.group(1), int(m.group(2)), int(m.group(3))
        if not path:
            continue
        try:
            r = await _rdfn(path=path, session_id=session_id, max_bytes=max_per_file * 2)
        except Exception:
            continue
        if not isinstance(r, dict) or r.get("error"):
            continue
        content = r.get("content") or ""
        loc = ""
        if start or end:
            lines = content.splitlines()
            content = "\n".join(lines[max(0, start - 1):(end or len(lines))])
            loc = f" (lines {start or 1}-{end or len(lines)})"
        blocks.append(f"===== FILE: {path}{loc} =====\n{content[:max_per_file]}")
    if not blocks:
        return ""
    return ("CONTEXT FILES (grounding for this task — use their content; do not echo them "
            "verbatim unless asked):\n\n" + "\n\n".join(blocks))


async def _llm_save_output(save_as: str, text: str, session_id: str = "") -> dict:
    """Persist a generation to `save_as` in the session's working area — INSIDE the
    sandbox container when the session is sandboxed, else the host artifact dir —
    and return {path, path_rel, bytes} (or {save_error} on failure).

    This is what makes a written deliverable a FILE without a second cap call: the
    caller asked for text AND a file, so llm.generate produces both. Best-effort:
    a failed write never fails the generation (the text is still returned)."""
    rel = str(save_as or "").strip().strip("/\\")
    if not rel or not (text or "").strip():
        return {}
    try:
        from Vera.vera.execution.exec_capabilities import write_artifact_file as _waf
        full = await _waf(relpath=rel, content=text, session_id=session_id or "")
        return {"path": full,
                "path_rel": os.path.basename(str(full)) or rel,
                "bytes": len((text or "").encode("utf-8", "ignore"))}
    except Exception as e:
        log.debug("llm output save failed for %s: %s", rel, e)
        return {"save_error": str(e)[:200]}


@capability(
    "llm.generate",
    http_method="POST", http_path="/llm/generate", http_tags=["llm", "generate"],
    # mode="distributed" caused stuck-pending jobs when the result-loop
    # was on the same process that dispatched — run locally instead.
    # The underlying ollama_generate / vllm_generate already handle their
    # own concurrency queuing via per-instance semaphores.
    streams=["tokens"],
    memory="on",
    description="Generate free-form text using the local LLM cluster (Ollama or vLLM). "
                "WHEN TO USE: writing prose, answering questions, reasoning, drafting content, summarising/"
                "synthesising results you PROVIDE, writing code/scripts, or structuring YOUR OWN reasoning "
                "as JSON. NOT for real-world DATA: do not use it to produce or edit a dataset / factual "
                "records / a populated JSON or CSV (e.g. real entities with real stats) — it fabricates; "
                "fetch or compute that with a script (exec.python.run) instead. "
                "This is the most general-purpose AUTHORING tool — if no specialised tool fits, use this. "
                "HOW TO USE: provide prompt= with the task and optional system= for persona/constraints. "
                "To ground the generation on FILES, pass files= — a path, a 'path:start-end' portion, "
                "or a list (e.g. files=['./output_web_fetch_2.txt','./notes.md:1-40']); their content is "
                "read (sandbox-aware) and prepended as context, so you don't paste large text inline. "
                "Params: prompt (str!), files (str|list — file paths/portions to include as context), "
                "system (str — optional persona/instructions), model (str — leave blank for default), "
                "prefer_gpu (bool default True — route to GPU instance), backend (ollama|vllm|auto), "
                "output_format (str — optional shared format profile, e.g. markdown|report|audio|json|"
                "docs|quick|short|long; see llm.formats — appends a format directive to the system prompt), "
                "job_type (str — optional cluster routing hint: embedding|chat|dream|summarize|vision|code; "
                "routes to instances per the active routing profile), "
                "save_as (str — optional FILENAME, e.g. 'report.md': the generated text is written to that "
                "file in your working directory/sandbox and its path returned, so you do NOT need a "
                "separate ide.fs.write/exec write step to persist a document). "
                "Output: {text (the generated response), model, instance, backend, has_gpu, tokens, "
                "path + path_rel (when save_as was given)}. "
                "Streams token-by-token via the tokens stream for live output.",
)
async def llm_generate(
    prompt:        str,
    model:         str   = None,
    system:        str   = "",
    instance_id:   str   = None,
    prefer_gpu:    bool  = False,
    backend:       str   = "auto",   # "ollama" | "vllm" | "auto"
    caller:        str   = "",       # true caller label for logging (e.g. "dream_research")
    output_format: str   = "",       # shared output-format profile (vera.output_formats)
    job_type:      str   = "",       # cluster routing hint (embedding|chat|dream|code|...)
    profile:       str   = "",       # routing profile (e.g. "loop") — with role, picks model+sampling
    role:          str   = "",       # routing role within the profile (e.g. "coder", "writer")
    files=None,                      # path | 'path:start-end' | list — read + prepended as context
    save_as:       str   = "",       # filename — write the generated text there (sandbox-aware)
    session_id:    str   = "",       # for sandbox-aware file reads/writes
    trace_id=None,
    stream_cb=None,   # optional extra per-call callback (e.g. the agentic loop's
                       # live tool-output stream) — internal plumbing, deliberately
                       # untyped/unannotated like trace_id so it never enters the
                       # generated JSON schema and a model can never pass it.
):
    from Vera.vera.capability_orchestration import (
        CAPABILITY_REGISTRY as _REG,
        _ollama_caller_info,
        effective_num_ctx,
    )

    # Optional FILE CONTEXT: read the given file(s)/portions and prepend them so the
    # caller can ground the generation on source files (a script to refactor, fetched
    # data, notes) without pasting them inline — the loop equivalent of podcast
    # sources. Sandbox-aware; best-effort.
    if files:
        try:
            _fctx = await _llm_files_context(files, session_id)
            if _fctx:
                prompt = _fctx + "\n\n---\n\n" + (prompt or "")
        except Exception as _fe:
            log.debug("llm.generate file-context read failed: %s", _fe)

    # Standardised output-format layer: prepend/append the profile's directive
    # to the system prompt so every backend produces the requested shape.
    if output_format:
        system = apply_format(system, output_format)

    # ── Output budget ────────────────────────────────────────────────────────
    # A bare llm.generate used to pass NO options, so it inherited the model's
    # small default context window — long structured outputs (big JSON/code
    # files, full documents) were silently truncated mid-stream. Grant a
    # generous but BOUNDED window: the model's detected max, capped to
    # VERA_LLM_GEN_CTX (default 16384) and the cluster-wide OLLAMA_MAX_AUTO_CTX,
    # and let the response use it (num_predict rides the same ceiling; the model
    # still stops early at a natural EOS for short answers).
    try:
        _want_ctx = int(os.getenv("VERA_LLM_GEN_CTX", "16384") or 16384)
    except Exception:
        _want_ctx = 16384

    # Build a caller_override so the real upstream caller is logged rather
    # than llm_generate itself appearing as the caller in every log entry.
    _caller_info = _ollama_caller_info()
    if caller:
        _caller_info["caller_func"] = caller
        _caller_info["cap_name"]    = caller

    tokens_collected: list = []
    async def _tok(t: str):
        tokens_collected.append(t)
        await emit_stream("tokens", trace_id, {"token": t}, "llm.generate")
        if stream_cb is not None:
            try:
                await stream_cb(t)
            except Exception:
                pass

    # ── Backend routing ────────────────────────────────────────────────────────
    # Prefer vLLM when: backend="vllm" OR (backend="auto" AND vLLM has online instances)
    _use_vllm = False
    if backend in ("vllm", "auto"):
        try:
            from Vera.vera.vllm_capabilities import VLLM_INSTANCES as _VI, vllm_generate as _vg
            _online_vllm = [i for i in _VI.values() if i.status == "online"]
            if _online_vllm and (backend == "vllm" or prefer_gpu):
                _use_vllm = True
        except ImportError:
            pass

    if _use_vllm:
        # Route through vllm.generate cap so its own event pipeline fires
        _cap = _REG.get("vllm.generate")
        if _cap:
            kw = dict(prompt=prompt, model=model, prefer_gpu=prefer_gpu,
                      instance_id=instance_id or None, max_tokens=_want_ctx,
                      _caller_label=caller or _caller_info["caller_func"])
            if system:
                # vLLM /v1/completions doesn't have a system field — prepend it
                kw["prompt"] = f"{system}\n\n{prompt}"
            try:
                result = await _cap["raw"](**{k:v for k,v in kw.items()
                                              if k in _cap["raw"].__code__.co_varnames},
                                          trace_id=trace_id)
                text = result.get("text", "") if isinstance(result, dict) else str(result)
                used_model = result.get("model", model or "") if isinstance(result, dict) else (model or "")
                used_inst  = result.get("instance_id", "") if isinstance(result, dict) else ""
                _fr = str(result.get("finish_reason", "")) if isinstance(result, dict) else ""
                return {"text": text, "model": used_model, "instance": used_inst,
                        "backend": "vllm", "has_gpu": True, "tokens": len(tokens_collected),
                        "truncated": _fr == "length",
                        **(await _llm_save_output(save_as, text, session_id))}
            except Exception as _e:
                log.warning("llm.generate vllm route failed (%s), falling back to ollama", _e)

    # ── Ollama path ─────────────────────────────────────────────────────────────
    # Call ollama_generate with the true caller so logs/events show the real source.
    try:
        _ctx = await effective_num_ctx(model, instance_id or None, prefer_gpu, manual=_want_ctx)
        _gen_opts = {"num_ctx": _ctx, "num_predict": _ctx}
    except Exception:
        _gen_opts = {"num_predict": _want_ctx}
    _meta: dict = {}
    text = await ollama_generate(
        prompt, system=system, model=model,
        instance_id=instance_id or None,
        prefer_gpu=prefer_gpu, stream_cb=_tok,
        caller_override=_caller_info,
        job_type=job_type or None,
        profile=profile or None, role=role or None,
        options=_gen_opts, meta_out=_meta,
    )
    chosen = pick_instance(prefer_gpu=prefer_gpu, instance_id=instance_id or None,
                           model=model, job_type=job_type or None)
    inst   = OLLAMA_INSTANCES.get(chosen or "", {})
    return {"text": text, "model": model or OLLAMA_MODEL,
            "instance": chosen, "instance_url": inst.get("url"),
            "backend": "ollama",
            "has_gpu": inst.get("has_gpu", False), "tokens": len(tokens_collected),
            "truncated": bool(_meta.get("truncated")),
            **(await _llm_save_output(save_as, text, session_id))}


@capability("llm.formats",
    http_method="GET", http_path="/llm/formats", http_tags=["llm"],
    memory="off", silent=True,
    description="List the shared output-format profiles that llm.generate (and other "
                "callers) accept via output_format=. Each: {id, label, kind "
                "(length|structure|deliverable), target_file_format}. Use these ids to "
                "standardise how answers are shaped, and pair with render.export to "
                "produce a file in the matching format.",
)
async def llm_formats(trace_id=None):
    profiles = list_profiles()
    return {"formats": profiles, "count": len(profiles)}


@capability("delivery.channels",
    http_method="GET", http_path="/delivery/channels", http_tags=["llm", "delivery"],
    memory="off", silent=True,
    description="List the shared delivery channels output can be routed to (the "
                "routing twin of llm.formats). Each: {id, label, cap, default_format, "
                "needs_target, target_field, target_label, fixed_target, source "
                "(builtin|skill)}. The dream deliver stage uses these as its "
                "deliver_to set; new channels can be added as `delivery_channel` "
                "skills and appear here automatically.",
)
async def delivery_channels(trace_id=None):
    channels = list_channels()
    return {"channels": channels, "count": len(channels)}


@capability("llm.summarize",
    http_method="POST", http_path="/llm/summarize", http_tags=["llm", "text"],
    memory="on",
    description="Summarise long text into a short form using the local LLM. "
                "WHEN TO USE: condense research results, large documents, or tool output before presenting to the user "
                "or passing to another tool. "
                "Input: text (str!), max_words (int default 150), style (concise|bullet|executive), "
                "instance_id (str), prefer_gpu (bool). "
                "Output: {summary, style, src_chars}.")
async def llm_summarize(
    text: str, max_words: int = 150, style: str = "concise",
    instance_id: str = None, prefer_gpu: bool = None, trace_id=None,
):
    if not text or not str(text).strip():
        return {"error": "text is required", "summary": "", "style": style, "src_chars": 0}
    text = str(text)
    if prefer_gpu is None: prefer_gpu = len(text) > 1000
    styles = {"concise":"Write a concise summary.","bullet":"Bullet-point summary (max 7).","executive":"One-paragraph executive summary."}
    system = f"You are a summarisation assistant. {styles.get(style,styles['concise'])} Target ≤{max_words} words. Reply with only the summary."
    # job_type="summarize" routes via the active Ollama profile (light/CPU by
    # default). Coerce a possible dict/None return into text so callers never
    # hit "'dict'/'NoneType' object has no attribute 'strip'".
    out = await ollama_generate(f"Summarise:\n\n{text}", system=system, instance_id=instance_id or None, prefer_gpu=prefer_gpu,
                                job_type="summarize",
                                caller_override={"caller_file":"capabilities.py","caller_func":"llm_summarize","cap_name":"llm.summarize"})
    if isinstance(out, dict):
        out = out.get("text") or out.get("response") or ""
    out = (out or "")
    if not isinstance(out, str):
        out = str(out)
    return {"summary": out.strip(), "style": style, "src_chars": len(text)}


@capability("llm.analyze",
    http_method="POST", http_path="/llm/analyze", http_tags=["llm", "analysis"],
    memory="on",
    description="Analyse text for sentiment, topics, entities and readability. "
                "Input: text (str!), aspects (str, default sentiment,topics,entities,readability), model (str), instance_id (str), prefer_gpu (bool), system (str). "
                "Output: {analysis object as JSON}.")
async def llm_analyze(
    text: str, aspects: str = "sentiment,topics,entities,readability",
    instance_id: str = None, prefer_gpu: bool = True, trace_id=None,
):
    system = ('You are an expert text analyst. Return ONLY valid JSON: '
              '{"sentiment":"positive|negative|neutral|mixed","sentiment_score":0.0,'
              '"topics":["..."],"entities":[{"text":"...","type":"person|org|place|date"}],'
              '"readability":"simple|intermediate|advanced","key_phrases":["..."]}')
    raw = await ollama_generate(text, system=system, json_mode=True, instance_id=instance_id or None, prefer_gpu=prefer_gpu,
                                caller_override={"caller_file":"capabilities.py","caller_func":"llm_analyze","cap_name":"llm.analyze"})
    try: result = json.loads(raw)
    except: result = {"raw": raw, "parse_error": True}
    result.update(char_count=len(text), word_count=len(text.split()))
    return result


@capability("llm.code_review",
    http_method="POST", http_path="/llm/code_review", http_tags=["llm", "code"],
    memory="on",
    description="Review code for bugs, security issues, style and performance. "
                "Input: code (str!), language (str), focus (str, default all), severity (str, default all), model (str), prefer_gpu (bool). "
                "Output: {issues:[{severity,category,line,message,fix}], summary}.")
async def llm_code_review(
    code: str, language: str = "python", focus: str = "bugs,security,style,performance",
    instance_id: str = None, prefer_gpu: bool = True, trace_id=None,
):
    system = (f'You are a senior {language} engineer. Focus on: {focus}. '
              'Return JSON: {"issues":[{"severity":"critical|high|medium|low","category":"...","message":"...","suggestion":"..."}],"overall_score":0-10,"summary":"..."}')
    raw = await ollama_generate(f"Review this {language} code:\n\n```{language}\n{code}\n```",
                                system=system, json_mode=True, instance_id=instance_id or None, prefer_gpu=prefer_gpu)
    try: return json.loads(raw)
    except: return {"issues": [], "summary": raw, "parse_error": True}


@capability("llm.translate",
    http_method="POST", http_path="/llm/translate", http_tags=["llm", "text"],
    memory="on",
    description="Translate text to a target language. "
                "Input: text (str!), target_lang (str!, e.g. 'French'), source_lang (str), model (str), instance_id (str), prefer_gpu (bool). "
                "Output: {translated, source_lang, target_lang, original_len}.")
async def llm_translate(
    text: str, target_lang: str = "English", source_lang: str = "auto",
    instance_id: str = None, prefer_gpu: bool = False, trace_id=None,
):
    src = f" from {source_lang}" if source_lang != "auto" else ""
    system = f"Translate the text{src} to {target_lang}. Reply with only the translated text."
    out = await ollama_generate(text, system=system, instance_id=instance_id or None, prefer_gpu=prefer_gpu,
                                 caller_override={"caller_file":"capabilities.py","caller_func":"llm_translate","cap_name":"llm.translate"})
    return {"translated": out.strip(), "source_lang": source_lang, "target_lang": target_lang}


@capability("llm.classify",
    http_method="POST", http_path="/llm/classify", http_tags=["llm", "analysis"],
    memory="auto",
    description="Classify text into one or more of a provided category list. "
                "Input: text (str!), categories (str!, comma-separated), multi_label (bool), model (str), instance_id (str), prefer_gpu (bool), system (str). "
                "Output: {label, confidence, labels: [{category, confidence}]}.")
async def llm_classify(
    text: str, categories: str = "positive,negative,neutral", multi_label: bool = False,
    instance_id: str = None, prefer_gpu: bool = False, trace_id=None,
):
    cats = [c.strip() for c in categories.split(",")]
    system = f'Classify into {"one or more" if multi_label else "exactly one"} of {cats}. Return ONLY JSON: {{"label":"..."}} or {{"labels":[...]}}'
    raw = await ollama_generate(text, system=system, json_mode=True, instance_id=instance_id or None, prefer_gpu=prefer_gpu,
                                caller_override={"caller_file":"capabilities.py","caller_func":"llm_classify","cap_name":"llm.classify"})
    try: return {**json.loads(raw), "categories": cats}
    except: return {"raw": raw, "parse_error": True}


@capability("llm.explain",
    http_method="POST", http_path="/llm/explain", http_tags=["llm", "text"],
    memory="on",
    description="Explain a concept, error message, or code snippet in plain language. "
                "Input: topic (str!), level (beginner|intermediate|expert), model (str), instance_id (str), prefer_gpu (bool). "
                "Output: {explanation, level, topic}.")
async def llm_explain(
    content: str, level: str = "intermediate", format: str = "prose",
    instance_id: str = None, prefer_gpu: bool = False, trace_id=None,
):
    fmts = {"prose":"Clear prose.","bullet":"Bullet points.","eli5":"Explain simply to a beginner."}
    system = f"You are a patient expert teacher. Target: {level}. {fmts.get(format,'Clear prose.')} Be accurate."
    out = await ollama_generate(f"Explain:\n\n{content}", system=system, instance_id=instance_id or None, prefer_gpu=prefer_gpu,
                                caller_override={"caller_file":"capabilities.py","caller_func":"llm_explain","cap_name":"llm.explain"})
    return {"explanation": out.strip(), "level": level, "format": format}


@capability("llm.brainstorm",
    http_method="POST", http_path="/llm/brainstorm", http_tags=["llm", "creative"],
    memory="on",
    description="Brainstorm ideas on a topic. "
                "Input: topic (str!), count (int, default 5), style (diverse|practical|creative|contrarian), model (str), prefer_gpu (bool). "
                "Output: {ideas: [str], topic, style}.")
async def llm_brainstorm(
    topic: str, count: int = 8, style: str = "diverse",
    instance_id: str = None, prefer_gpu: bool = False, trace_id=None,
):
    styles = {"diverse":"Diverse ideas across angles.","practical":"Practical actionable ideas.",
              "creative":"Imaginative unconventional ideas.","contrarian":"Challenge assumptions."}
    system = (f"Brainstorming assistant. {styles.get(style,styles['diverse'])} "
              'Return ONLY JSON: {"ideas":[{"title":"...","description":"...","rationale":"..."}]}')
    raw = await ollama_generate(f"Brainstorm {count} ideas for: {topic}", system=system,
                                json_mode=True, instance_id=instance_id or None, prefer_gpu=prefer_gpu)
    try: return {**json.loads(raw), "topic": topic, "style": style}
    except: return {"ideas": [], "raw": raw, "parse_error": True}


@capability("llm.rewrite",
    http_method="POST", http_path="/llm/rewrite", http_tags=["llm", "text"],
    memory="on",
    description="Rewrite text in a specified tone. "
                "Input: text (str!), tone (professional|casual|formal|friendly|concise|assertive), model (str), prefer_gpu (bool). "
                "Output: {rewritten, tone, original_len, new_len}.")
async def llm_rewrite(
    text: str, tone: str = "professional", target_len: str = "same",
    instance_id: str = None, prefer_gpu: bool = False, trace_id=None,
):
    system = f"Rewrite the text to be {tone} in tone. Target length: {target_len}. Reply with only the rewritten text."
    out = await ollama_generate(text, system=system, instance_id=instance_id or None, prefer_gpu=prefer_gpu)
    return {"rewritten": out.strip(), "tone": tone, "src_words": len(text.split()), "out_words": len(out.split())}


@capability("llm.qa",
    http_method="POST", http_path="/llm/qa", http_tags=["llm", "search"],
    memory="on",
    description="Answer a question using provided context text (RAG-style). "
                "Input: question (str!), context (str!), model (str), prefer_gpu (bool). "
                "Output: {answer, confidence, question}.")
async def llm_qa(
    question: str, context: str,
    instance_id: str = None, prefer_gpu: bool = False, trace_id=None,
):
    system = ('Answer only from context. If not found, say so. '
              'Return JSON: {"answer":"...","confidence":"high|medium|low","quote":"..."}')
    raw = await ollama_generate(f"Context:\n{context}\n\nQuestion: {question}",
                                system=system, json_mode=True, instance_id=instance_id or None, prefer_gpu=prefer_gpu)
    try: return {**json.loads(raw), "question": question}
    except: return {"answer": raw, "question": question, "parse_error": True}


@capability("llm.plan",
    http_method="POST", http_path="/llm/plan", http_tags=["llm", "dag"],
    memory="on",
    description="Produce a Vera DAG execution plan for a natural-language goal. "
                "Uses the dag-planner agent via the CapabilityIndex. "
                "Input: goal (str!), available_caps (list, optional — limits which caps are considered). "
                "Output: {dag, initial_state, rationale, warnings}.")
async def llm_plan_cap(goal: str, prefer_gpu: bool = True, trace_id=None):
    from Vera.vera.capability_orchestration import plan_dag
    return await plan_dag(goal)


# ─────────────────────────────────────────────────────────────────────────────
#  ██  TEXT
# ─────────────────────────────────────────────────────────────────────────────

@capability("text.stats",
    http_method="POST", http_path="/text/stats", http_tags=["text"],
    memory="off",
    description="Count chars, words, sentences and paragraphs in text. "
                "Input: text (str!). Output: {chars, words, sentences, paragraphs, avg_word_len}.")
async def text_stats(text: str, trace_id=None):
    words = text.split(); sents = re.split(r'(?<=[.!?])\s+', text.strip()); paras = [p for p in text.split("\n\n") if p.strip()]
    return {"chars":len(text),"words":len(words),"sentences":len([s for s in sents if s]),"paragraphs":len(paras),
            "avg_word_len":round(sum(len(w) for w in words)/max(len(words),1),2)}

@capability("text.find_replace",
    http_method="POST", http_path="/text/find_replace", http_tags=["text"],
    memory="off",
    description="Find and replace within text, with optional regex support. "
                "Input: text (str!), find (str!), replace (str), use_regex (bool). "
                "Output: {result, replacements_made}.")
async def text_find_replace(text: str, find: str, replace: str = "", regex: bool = False, trace_id=None):
    try:
        if regex: new,n = re.sub(find,replace,text), len(re.findall(find,text))
        else: n,new = text.count(find),text.replace(find,replace)
        return {"result":new,"replacements":n}
    except re.error as e: return {"error":str(e),"result":text}

@capability("text.extract_urls",
    http_method="POST", http_path="/text/extract_urls", http_tags=["text"],
    memory="off",
    description="Extract all URLs from text. "
                "Input: text (str!). Output: {urls: [str], count}.")
async def text_extract_urls(text: str, trace_id=None):
    urls = re.findall(r'https?://[^\s<>"\'{}|\\^`\[\]]+', text)
    return {"urls":[{"url":u,"domain":urlparse(u).netloc} for u in urls],"count":len(urls)}

@capability("text.hash",
    http_method="POST", http_path="/text/hash", http_tags=["text"],
    memory="off",
    description="Hash text using a cryptographic algorithm. "
                "Input: text (str!), algorithm (md5|sha1|sha256|sha512, default sha256). "
                "Output: {hash, algorithm, input_len}.")
async def text_hash(text: str, algorithm: str = "sha256", trace_id=None):
    algos={"md5":hashlib.md5,"sha1":hashlib.sha1,"sha256":hashlib.sha256,"sha512":hashlib.sha512}
    fn=algos.get(algorithm)
    if not fn: return {"error":f"Unknown: {algorithm}","supported":list(algos)}
    return {"hash":fn(text.encode()).hexdigest(),"algorithm":algorithm}

@capability("text.split_chunks",
    http_method="POST", http_path="/text/split_chunks", http_tags=["text"],
    memory="off",
    description="Split text into overlapping chunks for embedding/pipeline use. "
                "Input: text (str!), chunk_size (int, default 500 chars), overlap (int, default 50). "
                "Output: {chunks: [str], count, chunk_size, overlap}.")
async def text_split_chunks(text: str, chunk_size: int = 800, overlap: int = 100, trace_id=None):
    chunks,i=[],0
    while i<len(text):
        c=text[i:i+chunk_size]; chunks.append({"index":len(chunks),"text":c,"start":i,"end":i+len(c)}); i+=chunk_size-overlap
    return {"chunks":chunks,"count":len(chunks)}


# ─────────────────────────────────────────────────────────────────────────────
#  ██  DATA / MATH / HTTP / SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

@capability("data.json_validate",
    http_method="POST", http_path="/data/json_validate", http_tags=["data"],
    memory="off",
    description="Validate a JSON string and report parse errors. "
                "Input: json_str (str!). Output: {valid, error, line, column}.")
async def data_json_validate(json_str: str, trace_id=None):
    try:
        p=json.loads(json_str)
        return {"valid":True,"type":type(p).__name__,"length":len(p) if isinstance(p,(dict,list)) else None}
    except json.JSONDecodeError as e: return {"valid":False,"error":str(e),"line":e.lineno}

@capability("data.json_flatten",
    http_method="POST", http_path="/data/json_flatten", http_tags=["data"],
    memory="off",
    description="Flatten a nested JSON object to dot-notation keys. "
                "Input: json_str (str!), separator (str, default '.'). "
                "Output: {flattened: {key: value, ...}, key_count}.")
async def data_json_flatten(json_str: str, separator: str = ".", trace_id=None):
    try: obj=json.loads(json_str)
    except json.JSONDecodeError as e: return {"error":str(e)}
    def flat(o,p=""):
        r={}
        if isinstance(o,dict):
            for k,v in o.items(): r.update(flat(v,f"{p}{separator}{k}" if p else k))
        elif isinstance(o,list):
            for i,v in enumerate(o): r.update(flat(v,f"{p}{separator}{i}" if p else str(i)))
        else: r[p]=o
        return r
    f=flat(obj); return {"flat":f,"keys":len(f)}

@capability("math.compute",
    http_method="POST", http_path="/math/compute", http_tags=["math"],
    memory="off",
    description="Safely evaluate a math expression using Python math functions. "
                "Input: expression (str!, e.g. 'sqrt(16) + sin(pi/2)'). "
                "Output: {result, expression}.")
async def math_compute(expression: str, trace_id=None):
    safe={k:getattr(math,k) for k in dir(math) if not k.startswith("_")}
    stripped=re.sub(r'[a-zA-Z_]+','',expression)
    if any(c not in set("0123456789+-*/()., eE%") for c in stripped):
        return {"error":"Disallowed characters","expression":expression}
    try: r=eval(expression,{"__builtins__":{}},safe); return {"expression":expression,"result":r,"type":type(r).__name__}
    except Exception as e: return {"error":str(e),"expression":expression}

@capability("math.stats",
    http_method="POST", http_path="/math/stats", http_tags=["math"],
    memory="off",
    description="Descriptive statistics for a list of numbers. "
                "Input: numbers (str!, comma-separated, e.g. '1,2,3,4,5'). "
                "Output: {mean, median, mode, std, variance, min, max, count, sum}.")
async def math_stats(numbers: str, trace_id=None):
    try: vals=[float(x.strip()) for x in numbers.split(",") if x.strip()]
    except ValueError as e: return {"error":str(e)}
    if not vals: return {"error":"No numbers"}
    n=len(vals); m=sum(vals)/n; srt=sorted(vals); mid=n//2
    med=(srt[mid-1]+srt[mid])/2 if n%2==0 else srt[mid]
    var=sum((x-m)**2 for x in vals)/n
    return {"count":n,"mean":round(m,6),"median":round(med,6),"min":min(vals),"max":max(vals),"std_dev":round(math.sqrt(var),6)}

@capability("http.get",
    http_method="POST", http_path="/http/get", http_tags=["http", "web"],
    memory="auto",
    description="HTTP GET request to an external URL — returns raw response body. "
                "WHEN TO USE: call REST APIs, check endpoint health, download raw content from a known URL. "
                "For human-readable web pages prefer web.fetch which strips HTML to clean text. "
                "Input: url (str!), timeout (int sec default 15). "
                "Output: {ok, status, body (up to 64KB), content_type, latency_ms, url}.")
async def http_get(url: str, timeout: int = 15, trace_id=None):
    timeout = parse_timeout(timeout)
    try:
        async with httpx.AsyncClient(timeout=timeout,follow_redirects=True) as c:
            t0=time.monotonic(); r=await c.get(url); ms=round((time.monotonic()-t0)*1000)
        return {"url":str(r.url),"status":r.status_code,"ok":r.is_success,"latency_ms":ms,
                "content_type":r.headers.get("content-type",""),"body":r.text[:65536]}
    except Exception as e: return {"url":url,"error":str(e),"ok":False}

@capability("http.post",
    http_method="POST", http_path="/http/post", http_tags=["http", "web"],
    memory="auto",
    description="HTTP POST request with a JSON payload to an external URL. "
                "WHEN TO USE: call REST APIs that require POST, submit data to webhooks, trigger external services. "
                "Input: url (str!), payload (str — JSON-encoded dict, default '{}'), timeout (int sec default 15). "
                "Output: {ok, status, body (up to 32KB), content_type, url}.")
async def http_post(url: str, payload: str = "{}", timeout: int = 15, trace_id=None):
    timeout = parse_timeout(timeout)
    try:
        async with httpx.AsyncClient(timeout=timeout,follow_redirects=True) as c:
            r=await c.post(url,json=json.loads(payload))
        return {"url":str(r.url),"status":r.status_code,"ok":r.is_success,"body":r.text[:32768]}
    except Exception as e: return {"url":url,"error":str(e),"ok":False}

@capability("system.timestamp",
    http_method="GET", http_path="/system/timestamp", http_tags=["system", "util"],
    memory="off",
    description="Return current timestamps in multiple formats. "
                "Output: {iso, unix, unix_ms, date, time, timezone}.")
async def system_timestamp(trace_id=None):
    now=datetime.now(timezone.utc)
    return {"utc":now.isoformat(),"unix":int(now.timestamp()),"date":now.strftime("%Y-%m-%d"),
            "time":now.strftime("%H:%M:%S"),"day":now.strftime("%A")}

@capability("system.ping",
    http_method="POST", http_path="/system/ping", http_tags=["system", "network"],
    memory="off",
    description="HTTP-ping a host and return reachability and latency. "
                "Input: host (str!, hostname or URL), timeout (int, default 5 seconds). "
                "Output: {reachable, latency_ms, status, host}.")
async def system_ping(host: str, timeout: int = 5, trace_id=None):
    timeout = parse_timeout(timeout)
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            t0=time.monotonic(); r=await c.get(f"http://{host}"); ms=round((time.monotonic()-t0)*1000)
        return {"host":host,"reachable":True,"latency_ms":ms,"status":r.status_code}
    except Exception as e: return {"host":host,"reachable":False,"error":str(e)}


# ─────────────────────────────────────────────────────────────────────────────
#  ██  OLLAMA MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

@capability("ollama.list_models",
    http_method="GET", http_path="/ollama/models", http_tags=["ollama"],
    memory="off",
    description="List models available on one or all Ollama cluster nodes. "
                "Input: instance_id (str, optional — leave empty for all nodes). "
                "Output: {models: {instance_id: [model_name]}}.")
async def ollama_list_models(instance_id: str = None, trace_id=None):
    targets={instance_id:OLLAMA_INSTANCES[instance_id]} if instance_id and instance_id in OLLAMA_INSTANCES else OLLAMA_INSTANCES
    result={}
    async def _f(iid,inst):
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r=await c.get(f"{inst['url']}/api/tags"); r.raise_for_status()
                models=r.json().get("models",[])
                result[iid]={"models":[{"name":m["name"],"size_gb":round(m.get("size",0)/1e9,2)} for m in models],
                              "count":len(models),"status":"online","url":inst["url"],"has_gpu":inst["has_gpu"]}
        except Exception as e: result[iid]={"error":str(e),"status":"offline"}
    await asyncio.gather(*[_f(iid,inst) for iid,inst in targets.items()])
    return result

@capability("ollama.instances",
    http_method="GET", http_path="/ollama/cluster", http_tags=["ollama"],
    memory="off",
    description="Live status of all Ollama cluster nodes. Output: {instance_id: {url,status,models,in_use,latency_ms,has_gpu}}.")
async def ollama_instances_status(trace_id=None):
    return {iid:{"url":i["url"],"label":i["label"],"has_gpu":i["has_gpu"],"status":i["status"],
                 "latency_ms":i["latency_ms"],"models":i["models"],"in_use":i["in_use"],
                 "errors":i["errors"],"last_check":i["last_check"]}
            for iid,i in OLLAMA_INSTANCES.items()}

@capability("ollama.generate_raw",
    http_method="POST", http_path="/ollama/generate_raw", http_tags=["ollama", "llm"],
    memory="auto",
    description="Direct Ollama generation with full parameter control. "
                "Input: prompt (str!), model (str), system (str), instance_id (str), prefer_gpu (bool), "
                "temperature (float), top_p (float), top_k (int), repeat_penalty (float). "
                "Output: {text, model, instance, tokens}.")
async def ollama_generate_raw(
    prompt: str, model: str = None, system: str = "",
    temperature: float = 0.7, top_p: float = 0.9, num_predict: int = 512,
    stop: str = "", instance_id: str = None, prefer_gpu: bool = False, trace_id=None,
):
    import time as _time
    from Vera.vera.capability_orchestration import (
        _ollama_log_append, _ollama_caller_info,
    )

    chosen=pick_instance(prefer_gpu=prefer_gpu,instance_id=instance_id or None,model=model)
    if not chosen: return {"error":"No available instance"}
    inst=OLLAMA_INSTANCES[chosen]; use_mdl=model or OLLAMA_MODEL
    opts={"temperature":temperature,"top_p":top_p,"num_predict":num_predict}
    if stop: opts["stop"]=[s.strip() for s in stop.split(",")]
    payload={"model":use_mdl,"prompt":prompt,"stream":False,"options":opts}
    if system: payload["system"]=system

    # ── Log the request ──────────────────────────────────────────────────────
    _req_id = str(uuid.uuid4())[:12]
    _t0 = _time.time()
    _prompt_preview = (prompt or "")[:120].replace("\n", " ")
    _prompt_full = (prompt or "")[:16000]
    log.info("ollama_req [%s] model=%s inst=%s caller=capabilities:ollama_generate_raw prompt=%s",
             _req_id, use_mdl, chosen, _prompt_preview)
    try:
        await emit_event({
            "type": "ollama.request", "req_id": _req_id,
            "model": use_mdl, "instance_id": chosen,
            "instance_url": inst.get("url", ""),
            "caller_file": "capabilities.py", "caller_func": "ollama_generate_raw",
            "caller_module": "capabilities", "cap_name": "ollama.generate_raw",
            "prompt_preview": _prompt_preview, "prompt_full": _prompt_full, "json_mode": False,
            "prefer_gpu": prefer_gpu, "streaming": False,
        })
    except Exception:
        pass

    inst["in_use"]+=1
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r=await c.post(f"{inst['url']}/api/generate",json=payload); r.raise_for_status()
            d=r.json()
        _elapsed = round(_time.time() - _t0, 2)
        log.info("ollama_done [%s] %.2fs caller=capabilities:ollama_generate_raw", _req_id, _elapsed)
        _ollama_log_append({
            "req_id": _req_id, "model": use_mdl, "instance": chosen,
            "caller_file": "capabilities.py", "caller_func": "ollama_generate_raw",
            "prompt_preview": _prompt_preview, "ts": now_iso(),
            "status": "done", "elapsed_s": _elapsed,
            "eval_count": d.get("eval_count", 0),
        })
        try:
            await emit_event({
                "type": "ollama.request_done", "req_id": _req_id,
                "model": use_mdl, "instance_id": chosen,
                "caller_file": "capabilities.py", "caller_func": "ollama_generate_raw",
                "elapsed_s": _elapsed, "eval_count": d.get("eval_count", 0),
            })
        except Exception:
            pass
        return {"text":d.get("response",""),"model":use_mdl,"instance":chosen,"has_gpu":inst.get("has_gpu",False),
                "eval_count":d.get("eval_count"),"total_duration":d.get("total_duration")}
    except Exception as e:
        from Vera.vera.capability_orchestration import _err_text
        _elapsed = round(_time.time() - _t0, 2)
        _err = _err_text(e)
        log.error("ollama_generate_raw [%s] FAILED after %.2fs inst=%s err=%s",
                  _req_id, _elapsed, chosen, _err)
        _ollama_log_append({
            "req_id": _req_id, "model": use_mdl, "instance": chosen,
            "caller_file": "capabilities.py", "caller_func": "ollama_generate_raw",
            "prompt_preview": _prompt_preview, "ts": now_iso(),
            "status": "error", "elapsed_s": _elapsed, "error": _err,
        })
        try:
            await emit_event({
                "type": "ollama.request_error", "req_id": _req_id,
                "model": use_mdl, "instance_id": chosen,
                "caller_file": "capabilities.py", "caller_func": "ollama_generate_raw",
                "elapsed_s": _elapsed, "error": _err,
                "error_type": type(e).__name__,
            })
        except Exception:
            pass
        return {"error": _err, "instance": chosen}
    finally: inst["in_use"]=max(0,inst["in_use"]-1)


# ─────────────────────────────────────────────────────────────────────────────
#  ██  PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

@capability("pipeline.analyze_and_report",
    http_method="POST", http_path="/pipeline/analyze_report", http_tags=["pipeline", "llm"],
    memory="on", streams=["pipeline.progress"],
            description="Full pipeline: stats + analyze + classify + summarize → report.")
async def pipeline_analyze_and_report(
    text: str, categories: str = "technical,business,general",
    prefer_gpu: bool = True, trace_id=None,
):
    await emit_stream("pipeline.progress", trace_id, {"stage":"start"}, "pipeline.analyze_and_report")
    results = await asyncio.gather(
        text_stats(text=text,trace_id=trace_id),
        llm_analyze(text=text,prefer_gpu=prefer_gpu,trace_id=trace_id),
        llm_classify(text=text,categories=categories,prefer_gpu=prefer_gpu,trace_id=trace_id),
        llm_summarize(text=text,max_words=80,style="concise",prefer_gpu=prefer_gpu,trace_id=trace_id),
        return_exceptions=True,
    )
    await emit_stream("pipeline.progress", trace_id, {"stage":"done"}, "pipeline.analyze_and_report")
    keys=["stats","analysis","classify","summary"]
    return {k:(r if not isinstance(r,Exception) else {"error":str(r)}) for k,r in zip(keys,results)}


# ─────────────────────────────────────────────────────────────────────────────
#  ██  LLM STREAMING ENDPOINT
#  POST /llm/stream  — raw Ollama token SSE, no agent wrapper
#  Body: {prompt, system?, model?, instance_id?, prefer_gpu?}
#  Yields: text/event-stream  data: {"type":"token","text":"..."}
#           ...                data: {"type":"done","text":"<full>"}
# ─────────────────────────────────────────────────────────────────────────────

from fastapi import Request as _Request
from fastapi.responses import StreamingResponse as _StreamingResponse

@APP.post("/llm/stream")
async def llm_stream_endpoint(request: _Request):
    """
    SSE streaming endpoint for raw LLM text generation.
    Yields one SSE event per token from Ollama /api/generate.
    No agent system, no memory — pure token stream.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    prompt      = body.get("prompt", "")
    system      = body.get("system", "")
    model       = body.get("model") or OLLAMA_MODEL
    instance_id = body.get("instance_id") or None
    prefer_gpu  = bool(body.get("prefer_gpu", False))

    chosen = pick_instance(prefer_gpu=prefer_gpu, instance_id=instance_id, model=model) or "cpu-246"
    inst   = OLLAMA_INSTANCES.get(chosen, {})
    url    = inst.get("url", "http://192.168.0.246:11435")

    ollama_body: dict = {"model": model, "prompt": prompt, "stream": True}
    if system: ollama_body["system"] = system

    async def _generate():
        import time as _time
        from Vera.vera.capability_orchestration import (
            emit_event as _emit_event, _ollama_log_append, now_iso as _now_iso,
        )
        _req_id = str(uuid.uuid4())[:12]
        _t0 = _time.monotonic()
        _prompt_preview = (prompt or "")[:120].replace("\n", " ")
        _prompt_full = (prompt or "")[:16000]
        log.info("ollama_req [%s] model=%s inst=%s caller=capabilities:llm_stream prompt=%s",
                 _req_id, model, chosen, _prompt_preview)
        try:
            await _emit_event({
                "type": "ollama.request", "req_id": _req_id,
                "model": model, "instance_id": chosen, "instance_url": url,
                "caller_file": "capabilities.py", "caller_func": "llm_stream_endpoint",
                "caller_module": "capabilities", "cap_name": "llm.stream",
                "prompt_preview": _prompt_preview, "prompt_full": _prompt_full, "json_mode": False,
                "prefer_gpu": prefer_gpu, "streaming": True,
            })
        except Exception:
            pass

        yield b": ping\n\n"
        full = []
        _error_text = ""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as c:
                async with c.stream("POST", f"{url}/api/generate", json=ollama_body) as resp:
                    if resp.status_code != 200:
                        err = await resp.aread()
                        _error_text = (f"HTTP {resp.status_code} from {chosen}"
                                       + (": " + err.decode()[:200] if err else ""))
                        yield f"data: {json.dumps({'type':'error','text':_error_text})}\n\n".encode()
                        return
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            token = json.loads(line).get("response", "")
                        except Exception:
                            continue
                        if token:
                            full.append(token)
                            yield f"data: {json.dumps({'type':'token','text':token})}\n\n".encode()
        except Exception as e:
            from Vera.vera.capability_orchestration import _err_text
            _error_text = _err_text(e)
            yield f"data: {json.dumps({'type':'error','text':_error_text})}\n\n".encode()
            return
        finally:
            _elapsed = round(_time.monotonic() - _t0, 2)
            if _error_text:
                log.error("ollama_generate [%s] FAILED after %.2fs caller=capabilities:llm_stream err=%s",
                          _req_id, _elapsed, _error_text[:120])
                _ollama_log_append({
                    "req_id": _req_id, "model": model, "instance": chosen,
                    "caller_file": "capabilities.py", "caller_func": "llm_stream_endpoint",
                    "prompt_preview": _prompt_preview, "ts": _now_iso(),
                    "status": "error", "elapsed_s": _elapsed, "error": _error_text[:200],
                })
                try:
                    await _emit_event({
                        "type": "ollama.request_error", "req_id": _req_id,
                        "model": model, "instance_id": chosen,
                        "caller_file": "capabilities.py", "caller_func": "llm_stream_endpoint",
                        "elapsed_s": _elapsed, "error": _error_text[:200],
                    })
                except Exception:
                    pass
            else:
                log.info("ollama_done [%s] %.2fs tokens=%d caller=capabilities:llm_stream",
                         _req_id, _elapsed, len(full))
                _ollama_log_append({
                    "req_id": _req_id, "model": model, "instance": chosen,
                    "caller_file": "capabilities.py", "caller_func": "llm_stream_endpoint",
                    "prompt_preview": _prompt_preview, "ts": _now_iso(),
                    "status": "done", "elapsed_s": _elapsed, "tokens": len(full),
                })
                try:
                    await _emit_event({
                        "type": "ollama.request_done", "req_id": _req_id,
                        "model": model, "instance_id": chosen,
                        "caller_file": "capabilities.py", "caller_func": "llm_stream_endpoint",
                        "elapsed_s": _elapsed, "token_count": len(full),
                    })
                except Exception:
                    pass
        yield f"data: {json.dumps({'type':'done','text':''.join(full)})}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return _StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ─────────────────────────────────────────────────────────────────────────────
#  ██  SCHEDULED
# ─────────────────────────────────────────────────────────────────────────────

async def _model_sync():
    for iid,inst in OLLAMA_INSTANCES.items():
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r=await c.get(f"{inst['url']}/api/tags"); r.raise_for_status()
                inst["models"]=[m["name"] for m in r.json().get("models",[])]
        except: pass
    await emit_event({"type":"caps.model_sync"})

schedule(_model_sync, interval=3600, name="model_sync")

# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("vera_capabilities:APP", host="0.0.0.0", port=8000, reload=False)