"""
gpu_inference_server.py  v1.3.0
================================
GPU inference server providing:
  - Whisper STT          POST /stt
  - Stable Diffusion     POST /imagine
  - TTS (sync)           POST /tts
  - TTS (streaming PCM)  POST /tts/stream
  - LLM+TTS fan-out      POST /chat/speak  +  GET /chat/text/{sid}
  - Voice list           GET  /tts/voices

TTS engines supported (select via TTS_ENGINE env var or per-request):
  • kokoro  — kokoro-onnx, high quality neural TTS, CPU/GPU, Apache 2.0 (DEFAULT)
              pip install kokoro-onnx
  • coqui   — original Coqui/TTS library (GPU accelerated)

Dependencies:
    fastapi uvicorn[standard] redis python-multipart httpx
    torch torchvision torchaudio openai-whisper
    diffusers transformers accelerate xformers safetensors
    TTS Pillow numpy soundfile
    kokoro-onnx  (for kokoro engine)
"""

import asyncio
import base64
import io
import json
import logging
import os
import queue
import re
import threading
import time
import traceback
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncGenerator, Optional

import httpx
import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

# ─────────────────────────── CONFIG ──────────────────────────────────────────

REDIS_URL           = os.getenv("REDIS_URL",            "redis://localhost:6379/0")
REDIS_RESULT_TTL    = int(os.getenv("REDIS_RESULT_TTL", "300"))

WHISPER_MODEL       = os.getenv("WHISPER_MODEL",        "base")
SD_MODEL_ID         = os.getenv("SD_MODEL_ID",          "runwayml/stable-diffusion-v1-5")
SD_DEVICE           = os.getenv("SD_DEVICE",            "cuda")
SD_LORA_DIR         = os.getenv("SD_LORA_DIR",           "")    # dir to scan for .safetensors/.pt LoRA files

# TTS engine: "kokoro" (default) or "coqui"
TTS_ENGINE          = os.getenv("TTS_ENGINE",           "kokoro")

# Coqui config
TTS_MODEL_NAME      = os.getenv("TTS_MODEL_NAME",       "tts_models/en/ljspeech/tacotron2-DDC")  # pragma: allowlist secret
TTS_VOCODER_NAME    = os.getenv("TTS_VOCODER_NAME",     "vocoder_models/en/ljspeech/hifigan_v2")
TTS_SPEAKER         = os.getenv("TTS_SPEAKER",          None)
TTS_LANGUAGE        = os.getenv("TTS_LANGUAGE",         None)

# Kokoro config
KOKORO_VOICE        = os.getenv("KOKORO_VOICE",         "af_heart")
KOKORO_LANG         = os.getenv("KOKORO_LANG",          "en-us")
KOKORO_SAMPLE_RATE  = 24000

OLLAMA_BASE_URL     = os.getenv("OLLAMA_BASE_URL",      "http://localhost:11434")

ENABLE_WHISPER      = os.getenv("ENABLE_WHISPER",       "1") == "1"
ENABLE_SD           = os.getenv("ENABLE_SD",            "1") == "1"
ENABLE_TTS          = os.getenv("ENABLE_TTS",           "1") == "1"
ENABLE_REDIS        = os.getenv("ENABLE_REDIS",         "1") == "1"

SERVER_HOST         = os.getenv("SERVER_HOST",          "0.0.0.0")
SERVER_PORT         = int(os.getenv("SERVER_PORT",      "8765"))

REDIS_STT_QUEUE     = "stt_requests"
REDIS_IMAGINE_QUEUE = "imagine_requests"
REDIS_TTS_QUEUE     = "tts_requests"
REDIS_RESULT_PREFIX = "result:"

SD_DEFAULT_STEPS    = int(os.getenv("SD_DEFAULT_STEPS",    "30"))
SD_DEFAULT_GUIDANCE = float(os.getenv("SD_DEFAULT_GUIDANCE","7.5"))
SD_DEFAULT_WIDTH    = int(os.getenv("SD_DEFAULT_WIDTH",    "512"))
SD_DEFAULT_HEIGHT   = int(os.getenv("SD_DEFAULT_HEIGHT",   "512"))

TTS_SAMPLE_RATE     = 22050  # updated after model load

# ─────────────────────────── LOGGING ─────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("gpu_server")

# ─────────────────────────── GLOBALS ─────────────────────────────────────────

_whisper_model       = None
_sd_pipe             = None
_sd_attn_backend     = "default"   # xformers | sdpa | sliced — chosen in _load_sd,
                                   # re-applied by _restore_attention after IP-Adapter/ControlNet
_sd_img2img_pipe     = None   # lazily built from _sd_pipe.components (shares weights)
_sd_cpu_pipe         = None   # lazily built CPU fallback (fp32) for GPU-OOM retries
_sd_cpu_img2img_pipe = None
_sd_device           = "cpu"  # device SD actually loaded on (set in _load_sd)

# ── Optional sprite-pipeline GPU tiers (rembg / ControlNet / IP-Adapter / ESRGAN) ──
# Auto-detected, lazily loaded + import-guarded: the server boots fine without any of
# their (large) models/deps, and /sd/capabilities reports what's actually importable
# so the Vera side degrades gracefully (chroma-key, img2img seed-lock, Lanczos).
# Each ENABLE_* defaults ON and acts as a kill-switch — set it to "0" to force a tier
# off even when its deps are present. Models/weights auto-download from HF on first use.
ENABLE_REMBG        = os.getenv("ENABLE_REMBG",      "1") != "0"
ENABLE_UPSCALE      = os.getenv("ENABLE_UPSCALE",    "1") != "0"
ENABLE_CONTROLNET   = os.getenv("ENABLE_CONTROLNET", "1") != "0"
ENABLE_IPADAPTER    = os.getenv("ENABLE_IPADAPTER",  "1") != "0"
CONTROLNET_MODEL_ID    = os.getenv("CONTROLNET_MODEL_ID",    "lllyasviel/sd-controlnet-openpose")
CONTROLNET_XL_MODEL_ID = os.getenv("CONTROLNET_XL_MODEL_ID", "thibaud/controlnet-openpose-sdxl-1.0")
IPADAPTER_REPO      = os.getenv("IPADAPTER_REPO",    "h94/IP-Adapter")
REMBG_MODEL         = os.getenv("REMBG_MODEL",       "u2net")
ESRGAN_MODEL        = os.getenv("ESRGAN_MODEL",      "RealESRGAN_x4plus")

_controlnet_pipe   = None   # lazily built (shares _sd_pipe weights + a ControlNet)
_openpose_detector = None   # controlnet_aux OpenposeDetector (False once if absent)
_ipadapter_pipe    = None   # components-shared pipe; adapter is load/unloaded per call
_rembg_sessions: dict = {}  # model name → rembg session
_esrgan_models:  dict = {}  # (model, scale) → RealESRGANer
_tts_synthesizer     = None   # Coqui TTS instance
_kokoro_pipeline = None   # kokoro-onnx KPipeline instance
_redis_client    = None

_sd_loaded_loras: dict[str, float] = {}  # name → current weight (0 = unloaded)
_stt_queue  = queue.Queue(maxsize=64)
_sd_queue   = queue.Queue(maxsize=64)
_tts_queue  = queue.Queue(maxsize=64)

# ── Live job progress (diffusion steps + post-processing phases) ─────────────
# Callers that pass a `job_id` with an image request can poll GET /progress/{id}
# while the request is in flight and receive {phase, step, total, preview_b64}.
# The preview is a cheap latent→RGB approximation (no VAE decode), refreshed
# every few steps, so the user can literally watch the image form.
_progress: dict[str, dict] = {}
_progress_lock = threading.Lock()
_PROGRESS_TTL = 600            # seconds before an entry may be pruned
_PREVIEW_EVERY = 3             # decode a preview every N diffusion steps

# Latent-channel → RGB approximation factors (the standard cheap trick — good
# enough to watch composition emerge without paying for a VAE decode per step).
_LATENT_RGB_SD15 = [[0.298, 0.207, 0.208], [0.187, 0.286, 0.173],
                    [-0.158, 0.189, 0.264], [-0.184, -0.271, -0.473]]
_LATENT_RGB_SDXL = [[0.3651, 0.4232, 0.4341], [-0.2533, -0.0042, 0.1068],
                    [0.1076, 0.1111, -0.0362], [-0.3165, -0.2492, -0.2188]]


def _progress_set(job_id: str, **kw):
    if not job_id:
        return
    with _progress_lock:
        ent = _progress.get(job_id) or {"job_id": job_id, "created": time.time()}
        ent.update(kw, ts=time.time())
        _progress[job_id] = ent
        # prune stale entries opportunistically
        if len(_progress) > 200:
            cutoff = time.time() - _PROGRESS_TTL
            for k in [k for k, v in _progress.items() if v.get("ts", 0) < cutoff]:
                _progress.pop(k, None)


def _progress_get(job_id: str) -> Optional[dict]:
    with _progress_lock:
        ent = _progress.get(job_id)
        return dict(ent) if ent else None


def _latents_to_preview_b64(latents, max_side: int = 256) -> Optional[str]:
    """Approximate RGB preview from diffusion latents (linear combination of the
    4 latent channels — no VAE). Returns a small base64 JPEG."""
    try:
        from PIL import Image as _PILImage
        lat = latents
        if lat.dim() == 4:
            lat = lat[0]
        factors = _LATENT_RGB_SDXL if "xl" in SD_MODEL_ID.lower() else _LATENT_RGB_SD15
        weights = torch.tensor(factors, dtype=lat.dtype, device=lat.device)  # (4,3)
        rgb = torch.einsum("chw,cr->rhw", lat[:4].float(), weights.float())
        rgb = (rgb - rgb.min()) / max(1e-5, float(rgb.max() - rgb.min()))
        arr = (rgb.clamp(0, 1) * 255).byte().permute(1, 2, 0).cpu().numpy()
        img = _PILImage.fromarray(arr, "RGB")
        img = img.resize((img.width * 8, img.height * 8), _PILImage.NEAREST)
        if max(img.size) > max_side:
            s = max_side / max(img.size)
            img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))),
                             _PILImage.BILINEAR)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=70)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        log.debug(f"latent preview failed: {e}")
        return None


def _step_callback_kwargs(pipe, job_id: str, total_steps: int) -> dict:
    """Pipeline kwargs wiring a per-step progress callback, matched to whichever
    callback API this diffusers build supports. No-op when job_id is empty."""
    if not job_id:
        return {}
    import inspect
    try:
        params = inspect.signature(pipe.__call__).parameters
    except (TypeError, ValueError):
        params = {}

    def _record(step: int, latents):
        upd = {"phase": "diffusion", "step": int(step) + 1, "total": int(total_steps)}
        if latents is not None and (step % _PREVIEW_EVERY == 0
                                    or step >= total_steps - 1):
            b64 = _latents_to_preview_b64(latents)
            if b64:
                upd["preview_b64"] = b64
        _progress_set(job_id, **upd)

    if "callback_on_step_end" in params:
        def _cb_new(_pipe, step, _timestep, cb_kwargs):
            _record(step, cb_kwargs.get("latents"))
            return cb_kwargs
        return {"callback_on_step_end": _cb_new,
                "callback_on_step_end_tensor_inputs": ["latents"]}
    if "callback" in params:
        def _cb_old(step, _timestep, latents):
            _record(step, latents)
        return {"callback": _cb_old, "callback_steps": 1}
    return {}

# ─────────────────────────── KOKORO VOICE CATALOGUE ──────────────────────────

KOKORO_VOICES = {
    "af_heart":    {"name": "Heart",    "lang": "en-us", "gender": "F"},
    "af_bella":    {"name": "Bella",    "lang": "en-us", "gender": "F"},
    "af_nicole":   {"name": "Nicole",   "lang": "en-us", "gender": "F"},
    "af_sarah":    {"name": "Sarah",    "lang": "en-us", "gender": "F"},
    "af_sky":      {"name": "Sky",      "lang": "en-us", "gender": "F"},
    "am_adam":     {"name": "Adam",     "lang": "en-us", "gender": "M"},
    "am_michael":  {"name": "Michael",  "lang": "en-us", "gender": "M"},
    "bf_emma":     {"name": "Emma",     "lang": "en-gb", "gender": "F"},
    "bf_isabella": {"name": "Isabella", "lang": "en-gb", "gender": "F"},
    "bm_george":   {"name": "George",   "lang": "en-gb", "gender": "M"},
    "bm_lewis":    {"name": "Lewis",    "lang": "en-gb", "gender": "M"},
}

# ─────────────────────────── MODEL LOADING ───────────────────────────────────

def _select_device() -> str:
    """Pick the compute device, honouring SD_DEVICE (default 'cuda').

    Falls back to CPU when CUDA is unavailable so the server still starts on a
    CPU-only box. Set SD_DEVICE=cpu to force CPU — e.g. if this card's PyTorch
    build lacks matching kernels (the old hard-coded reason: a V100/CC-7.0 with
    a torch wheel built only for CC>=7.5 hit a PTX JIT error; install a Volta
    build of torch to use the GPU).
    """
    want = (SD_DEVICE or "cuda").strip().lower()
    if want.startswith("cuda"):
        if torch.cuda.is_available():
            return "cuda"
        log.warning("SD_DEVICE=cuda but torch reports no CUDA — using CPU.")
    return "cpu"


def load_models():
    global _whisper_model, _sd_pipe, TTS_SAMPLE_RATE

    device = _select_device()
    log.info(f"Using device: {device}")

    if ENABLE_WHISPER:
        log.info(f"Loading Whisper ({WHISPER_MODEL})…")
        import whisper
        try:
            _whisper_model = whisper.load_model(WHISPER_MODEL, device=device)
        except Exception as e:
            if device == "cuda":
                log.warning(f"Whisper GPU load failed ({e}); falling back to CPU.")
                _whisper_model = whisper.load_model(WHISPER_MODEL, device="cpu")
            else:
                raise
        log.info("Whisper ready.")

    if ENABLE_SD:
        # SD is optional: a failure here (broken diffusers/xformers, missing
        # CUDA kernels, OOM, …) must NOT take down Whisper/TTS. Log and carry on
        # with SD disabled — /health and /sd/capabilities will report it off.
        try:
            _load_sd(device)
        except Exception as e:
            log.error(f"Stable Diffusion failed to load ({e}); continuing with SD disabled.")
            log.debug(traceback.format_exc())
            _sd_pipe = None

    if ENABLE_TTS:
        if TTS_ENGINE == "kokoro":
            _load_kokoro()
        else:
            _load_coqui(device)


def _load_sd(device: str):
    """
    Load Stable Diffusion pipeline with automatic model/device selection.

    Model selection via SD_MODEL_ID env var. Recommended modern models:
      • SG161222/Realistic_Vision_V6.0_B1_noVAE  (photorealism, fast)
      • stabilityai/stable-diffusion-2-1          (quality, 768px native)
      • runwayml/stable-diffusion-v1-5            (legacy, 512px, very fast)
      • stabilityai/stable-diffusion-xl-base-1.0  (SDXL, needs 10GB+ VRAM)

    On V100 / older CUDA GPUs: use float16, disable xformers (not supported),
    enable attention slicing and VAE tiling for memory efficiency.
    """
    global _sd_pipe, _sd_device

    is_xl = "xl" in SD_MODEL_ID.lower()
    if is_xl:
        from diffusers import StableDiffusionXLPipeline as SDPipeline
    else:
        from diffusers import StableDiffusionPipeline as SDPipeline

    def _try_load(dev: str):
        """Load the pipeline on `dev`. Retries without the fp16 variant since
        not every checkpoint publishes one."""
        dtype = torch.float16 if dev == "cuda" else torch.float32
        kwargs = dict(torch_dtype=dtype, safety_checker=None, use_safetensors=True)
        if dev == "cuda":
            kwargs["variant"] = "fp16"
        try:
            pipe = SDPipeline.from_pretrained(SD_MODEL_ID, **kwargs).to(dev)
        except Exception as e:
            if kwargs.pop("variant", None) is not None:
                log.warning(f"SD fp16-variant load failed ({e}); retrying without variant.")
                pipe = SDPipeline.from_pretrained(SD_MODEL_ID, **kwargs).to(dev)
            else:
                raise
        return pipe, dtype

    log.info(f"Loading Stable Diffusion ({SD_MODEL_ID}) on {device}...")
    try:
        _sd_pipe, dtype = _try_load(device)
        used = device
    except Exception as e:
        # Any GPU failure (missing kernels, OOM, PTX JIT, …) degrades to CPU so
        # the server still comes up — same outcome as the old forced-CPU path.
        if device == "cuda":
            log.error(f"SD load on GPU failed ({e}); falling back to CPU.")
            _sd_pipe, dtype = _try_load("cpu")
            used = "cpu"
        else:
            raise

    if used == "cuda":
        # Attention backend, best-first for this GPU:
        #   xformers (Ampere+) → PyTorch SDPA (works great on V100/Volta, torch≥2.0)
        #   → sliced attention (last-resort low-VRAM fallback).
        # SDPA is memory-efficient enough for SD1.x/2.x on a 16GB+ V100, so we AVOID
        # attention slicing there — slicing is a real speed hit. Force the old
        # low-VRAM (sliced) path with SD_LOW_VRAM=1 (e.g. SDXL on a small card).
        global _sd_attn_backend
        low_vram = os.getenv("SD_LOW_VRAM", "0") == "1"
        _sd_attn_backend = "default"
        if not low_vram:
            try:
                _sd_pipe.enable_xformers_memory_efficient_attention()
                _sd_attn_backend = "xformers"
                log.info("SD attention: xformers.")
            except Exception:
                try:
                    from diffusers.models.attention_processor import AttnProcessor2_0
                    _sd_pipe.unet.set_attn_processor(AttnProcessor2_0())
                    _sd_attn_backend = "sdpa"
                    log.info("SD attention: PyTorch SDPA (xformers unavailable — expected on V100).")
                except Exception as e:
                    log.info(f"SD attention: SDPA unavailable ({e}) — falling back to slicing.")
        if _sd_attn_backend == "default" or low_vram:
            _sd_pipe.enable_attention_slicing(slice_size="auto")
            _sd_attn_backend = "sliced"
            log.info("SD attention: sliced (low-VRAM).")

        # channels-last speeds up the conv-heavy UNet/VAE on Volta (best-effort).
        for comp in ("unet", "vae"):
            try:
                getattr(_sd_pipe, comp).to(memory_format=torch.channels_last)
            except Exception:
                pass

        # VAE tiling + slicing keep peak VRAM down when decoding big / batched frames.
        for m in ("enable_vae_tiling", "enable_vae_slicing"):
            try:
                getattr(_sd_pipe, m)()
            except Exception:
                pass

        # Optional sampler override (DPM++ 2M Karras is a strong pixel-art default;
        # 'lcm' enables 4–8 step generation when paired with an LCM-LoRA).
        _apply_scheduler(_sd_pipe, os.getenv("SD_SCHEDULER", ""))

        # V100 does not support TF32 — ensure correct precision
        torch.backends.cuda.matmul.allow_tf32 = False

    _sd_device = used
    log.info(f"Stable Diffusion ready ({'SDXL' if is_xl else 'SD1.x/2.x'}, {dtype}, {used}, "
             f"attn={_sd_attn_backend}).")


def _restore_attention():
    """Re-apply the baseline attention backend chosen in _load_sd. IP-Adapter and
    ControlNet reset the shared UNet's attn processors around a call; this puts the
    fast baseline back so /imagine txt2img isn't left on a slower path."""
    if _sd_pipe is None:
        return
    try:
        if _sd_attn_backend == "xformers":
            _sd_pipe.unet.set_default_attn_processor()
            _sd_pipe.enable_xformers_memory_efficient_attention()
        elif _sd_attn_backend == "sdpa":
            from diffusers.models.attention_processor import AttnProcessor2_0
            _sd_pipe.unet.set_attn_processor(AttnProcessor2_0())
        elif _sd_attn_backend == "sliced":
            _sd_pipe.unet.set_default_attn_processor()
            if _sd_device == "cuda":
                _sd_pipe.enable_attention_slicing(slice_size="auto")
        else:
            _sd_pipe.unet.set_default_attn_processor()
    except Exception as e:
        log.debug(f"[SD] restore attention ({_sd_attn_backend}): {e}")


SD_SCHEDULERS = ["dpmpp", "euler", "euler_a", "unipc", "ddim", "lcm"]


def _apply_scheduler(pipe, name: str):
    """Optionally swap the sampler. '' keeps the model default. Options: dpmpp
    (DPM++ 2M Karras — strong general/pixel-art default), euler, euler_a, unipc,
    ddim, lcm (few-step; pair with an LCM-LoRA + guidance≈1-2). Best-effort —
    bad names keep the default."""
    name = (name or "").strip().lower()
    if not name:
        return
    try:
        cfg = pipe.scheduler.config
        if name in ("dpmpp", "dpmpp_2m", "dpm++"):
            from diffusers import DPMSolverMultistepScheduler
            pipe.scheduler = DPMSolverMultistepScheduler.from_config(cfg, use_karras_sigmas=True)
        elif name == "euler":
            from diffusers import EulerDiscreteScheduler
            pipe.scheduler = EulerDiscreteScheduler.from_config(cfg)
        elif name in ("euler_a", "euler_ancestral"):
            from diffusers import EulerAncestralDiscreteScheduler
            pipe.scheduler = EulerAncestralDiscreteScheduler.from_config(cfg)
        elif name == "unipc":
            from diffusers import UniPCMultistepScheduler
            pipe.scheduler = UniPCMultistepScheduler.from_config(cfg)
        elif name == "ddim":
            from diffusers import DDIMScheduler
            pipe.scheduler = DDIMScheduler.from_config(cfg)
        elif name == "lcm":
            from diffusers import LCMScheduler
            pipe.scheduler = LCMScheduler.from_config(cfg)
        else:
            log.info(f"SD_SCHEDULER '{name}' unknown; keeping default.")
            return
        log.info(f"SD scheduler set to {name}.")
    except Exception as e:
        log.warning(f"SD_SCHEDULER '{name}' failed ({e}); keeping default.")


@contextmanager
def _scheduler_override(pipe, name: str):
    """Swap the sampler for ONE job (payload 'scheduler'), restoring the pipe's
    original scheduler afterwards so concurrent callers keep their default.
    Cheap: schedulers are small config objects, nothing is downloaded."""
    name = (name or "").strip().lower()
    if not name:
        yield
        return
    orig = pipe.scheduler
    try:
        _apply_scheduler(pipe, name)
        yield
    finally:
        pipe.scheduler = orig

def _load_kokoro():
    global _kokoro_pipeline, TTS_SAMPLE_RATE
    log.info("Loading Kokoro TTS...")
    try:
        from kokoro_onnx import Kokoro
        import urllib.request, os

        model_dir   = os.getenv("KOKORO_MODEL_DIR", os.path.dirname(os.path.abspath(__file__)))
        model_path  = os.path.join(model_dir, "kokoro-v1.0.onnx")
        voices_path = os.path.join(model_dir, "voices-v1.0.bin")

        BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v0.2.1"
        FILES = {
            model_path:  f"{BASE}/kokoro-v1.0.onnx",
            voices_path: f"{BASE}/voices-v1.0.bin",
        }

        for dest, url in FILES.items():
            if not os.path.exists(dest):
                log.info(f"Downloading {os.path.basename(dest)} from {url} ...")
                urllib.request.urlretrieve(url, dest)
                log.info(f"Downloaded {os.path.basename(dest)} ({os.path.getsize(dest)//1024//1024} MB)")

        _kokoro_pipeline = Kokoro(model_path, voices_path)
        TTS_SAMPLE_RATE  = KOKORO_SAMPLE_RATE

        try:
            voices = _kokoro_pipeline.get_voices()
            log.info(f"Kokoro ready. {len(voices)} voices: {list(voices)[:6]}")
        except Exception:
            log.info(f"Kokoro ready. SR={TTS_SAMPLE_RATE}")

    except Exception as e:
        log.error(f"Kokoro failed ({e}), falling back to Coqui on CPU...")
        _load_coqui("cpu")



def _load_coqui(device: str):
    global _tts_synthesizer, TTS_SAMPLE_RATE
    log.info(f"Loading Coqui TTS ({TTS_MODEL_NAME}) on {device}...")
    from TTS.api import TTS as CoquiTTS
    try:
        _tts_synthesizer = CoquiTTS(
            model_name=TTS_MODEL_NAME,
            # vocoder_name=TTS_VOCODER_NAME if TTS_VOCODER_NAME else None,
            gpu=(device == "cuda"),
        )
    except RuntimeError as e:
        if device == "cuda" and ("cuDNN" in str(e) or "cuda" in str(e).lower()):
            log.warning(f"Coqui CUDA failed ({e}), retrying on CPU...")
            _tts_synthesizer = CoquiTTS(
                model_name=TTS_MODEL_NAME,
                # vocoder_name=TTS_VOCODER_NAME if TTS_VOCODER_NAME else None,
                gpu=False,
            )
        else:
            raise
    if hasattr(_tts_synthesizer, "synthesizer") and hasattr(
        _tts_synthesizer.synthesizer, "output_sample_rate"
    ):
        TTS_SAMPLE_RATE = _tts_synthesizer.synthesizer.output_sample_rate
    log.info(f"Coqui TTS ready. Sample rate: {TTS_SAMPLE_RATE} Hz")

# ─────────────────────────── REDIS ───────────────────────────────────────────

def get_redis():
    global _redis_client
    if _redis_client is None:
        import redis as redis_lib
        _redis_client = redis_lib.from_url(REDIS_URL, decode_responses=False)
    return _redis_client


def redis_set_result(job_id: str, payload: dict):
    get_redis().setex(
        f"{REDIS_RESULT_PREFIX}{job_id}", REDIS_RESULT_TTL, json.dumps(payload)
    )


def redis_get_result(job_id: str) -> Optional[dict]:
    raw = get_redis().get(f"{REDIS_RESULT_PREFIX}{job_id}")
    return json.loads(raw) if raw else None

# ─────────────────────────── TTS SYNTHESIS ───────────────────────────────────

def _tts_synthesize(text: str, opts: dict) -> tuple[np.ndarray, int]:
    """Returns (float32_audio_array, sample_rate)."""
    text   = _clean_for_tts(text)
    if not text:
        # Return a short silence rather than erroring on empty cleaned text
        silence = np.zeros(int(TTS_SAMPLE_RATE * 0.1), dtype=np.float32)
        return silence, TTS_SAMPLE_RATE
    engine = opts.get("engine") or TTS_ENGINE

    if engine == "kokoro" and _kokoro_pipeline is not None:
        return _synthesize_kokoro(text, opts)
    elif _tts_synthesizer is not None:
        return _synthesize_coqui(text, opts)
    elif _kokoro_pipeline is not None:
        return _synthesize_kokoro(text, opts)
    else:
        raise RuntimeError("No TTS engine loaded.")


def _synthesize_kokoro(text: str, opts: dict) -> tuple[np.ndarray, int]:
    voice = opts.get("voice") or opts.get("speaker") or KOKORO_VOICE
    speed = float(opts.get("speed") or 1.0)
    speed = max(0.5, min(2.0, speed))

    kk = _kokoro_pipeline

    # v0.3.x: create(text, voice, speed, lang) is synchronous, returns (samples, sample_rate)
    if hasattr(kk, "create"):
        result = kk.create(text, voice=voice, speed=speed, lang=KOKORO_LANG)
        # Returns (audio_array, sample_rate) tuple
        if isinstance(result, tuple):
            audio, sr = result
        else:
            audio, sr = result, KOKORO_SAMPLE_RATE
        if audio is None or len(audio) == 0:
            raise RuntimeError(f"Kokoro.create() produced no audio for: {text!r}")
        return np.array(audio, dtype=np.float32), int(sr)

    # v0.4+ callable: kk(text, voice, speed) -> generator of (gs, ps, audio)
    if callable(kk):
        samples = []
        for _, _, audio in kk(text, voice=voice, speed=speed):
            if audio is not None and len(audio) > 0:
                samples.append(np.array(audio, dtype=np.float32))
        if not samples:
            raise RuntimeError(f"Kokoro generator produced no audio for: {text!r}")
        return np.concatenate(samples), KOKORO_SAMPLE_RATE

    raise RuntimeError(
        f"Cannot determine Kokoro synthesis API. "
        f"Available: {[m for m in dir(kk) if not m.startswith('_')]}"
    )


def _synthesize_coqui(text: str, opts: dict) -> tuple[np.ndarray, int]:
    speaker  = opts.get("speaker",  TTS_SPEAKER)
    language = opts.get("language", TTS_LANGUAGE)
    kwargs   = {}
    if speaker:  kwargs["speaker"]  = speaker
    if language: kwargs["language"] = language
    wav = _tts_synthesizer.tts(text=text, **kwargs)
    if not isinstance(wav, np.ndarray):
        wav = np.array(wav, dtype=np.float32)
    return wav, TTS_SAMPLE_RATE


def _wav_to_pcm_s16le(wav: np.ndarray) -> bytes:
    return (np.clip(wav, -1.0, 1.0) * 32767).astype(np.int16).tobytes()

def _expand_roman_numerals(text: str) -> str:
    """Replace Roman numerals with spoken English, but only in clear numeric contexts."""
    _spoken = {
        'I':'one','II':'two','III':'three','IV':'four','V':'five',
        'VI':'six','VII':'seven','VIII':'eight','IX':'nine','X':'ten',
        'XI':'eleven','XII':'twelve','XIII':'thirteen','XIV':'fourteen',
        'XV':'fifteen','XVI':'sixteen','XVII':'seventeen','XVIII':'eighteen',
        'XIX':'nineteen','XX':'twenty','XXX':'thirty','XL':'forty',
        'L':'fifty','LX':'sixty','LXX':'seventy','LXXX':'eighty',
        'XC':'ninety','C':'one hundred',
    }
    # Only expand Roman numerals that follow a qualifying context word
    # (Generation, Chapter, Part, Phase, etc.) to avoid mangling normal text
    context = r'(?:Generation|Chapter|Part|Phase|Stage|Version|Vol|Type|Class|Level|Tier|Mark|Model|Series|Mk|Episode|Book|Act|Scene|Round|Season|Step|Figure|Table|Appendix|Section)'
    pattern = re.compile(
        r'(' + context + r')\s+([IVXLCDM]+)\b',
        re.IGNORECASE
    )
    def _replace(m):
        prefix = m.group(1)
        numeral = m.group(2).upper()
        spoken = _spoken.get(numeral)
        if spoken:
            return f'{prefix} {spoken}'
        return m.group(0)
    return pattern.sub(_replace, text)


def _clean_for_tts(text: str) -> str:
    """
    Strip markdown, emojis, and other content that should not be spoken aloud.
    Expands Roman numerals in context (e.g. "Generation IV" -> "Generation four").
    """
    # ── Fenced code blocks ───────────────────────────────────────────────────
    text = re.sub(r'```[\w]*\s*\n[\s\S]*?```', ' code block omitted. ', text)
    text = re.sub(r'~~~[\s\S]*?~~~', ' code block omitted. ', text)

    # ── Inline code → keep content, drop backticks ───────────────────────────
    text = re.sub(r'`([^`\n]+)`', r'\1', text)
    text = text.replace('`', '')

    # ── Images & links ───────────────────────────────────────────────────────
    text = re.sub(r'!\[([^\]]*)\]\([^)]*\)', '', text)
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)

    # ── Headers ──────────────────────────────────────────────────────────────
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)

    # ── Bold / italic — longest marker first to avoid orphaned asterisks ─────
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\*\*(.+?)\*\*',     r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\*(.+?)\*',         r'\1', text, flags=re.DOTALL)
    text = re.sub(r'___(.+?)___',       r'\1', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__',         r'\1', text, flags=re.DOTALL)
    text = re.sub(r'_(.+?)_',           r'\1', text, flags=re.DOTALL)
    text = re.sub(r'~~(.+?)~~',         r'\1', text, flags=re.DOTALL)

    # ── Nuke any remaining stray asterisks/underscores ───────────────────────
    text = re.sub(r'\*+', ' ', text)
    text = re.sub(r'(?<!\w)_+(?!\w)', ' ', text)

    # ── Blockquotes & horizontal rules ───────────────────────────────────────
    text = re.sub(r'^>+\s?', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)

    # ── HTML tags ────────────────────────────────────────────────────────────
    text = re.sub(r'<[^>]+>', '', text)

    # ── List markers ─────────────────────────────────────────────────────────
    text = re.sub(r'^\s*[-+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+[.):\-]\s+', '', text, flags=re.MULTILINE)

    # ── Tables ───────────────────────────────────────────────────────────────
    text = re.sub(r'^\|.*\|\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[|:\s\-]+$', '', text, flags=re.MULTILINE)

    # ── Escaped markdown characters → literal ────────────────────────────────
    text = re.sub(r'\\([^\\])', r'\1', text)
    text = re.sub(r'[|\\]', ' ', text)

    # ── Emojis ───────────────────────────────────────────────────────────────
    text = re.sub(
        u'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF'
        u'\U0001F1E0-\U0001F1FF\U00002700-\U000027BF\U0001F900-\U0001F9FF'
        u'\U00002600-\U000026FF\U00002B00-\U00002BFF\U0001FA00-\U0001FA6F'
        u'\U0001FA70-\U0001FAFF\uFE00-\uFE0F\u200d\u200b\u20e3]',
        '', text
    )

    # ── Roman numerals in context ────────────────────────────────────────────
    text = _expand_roman_numerals(text)

    # ── Normalise whitespace ─────────────────────────────────────────────────
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)

    return text.strip()

def _stt_worker():
    log.info("STT worker started.")
    while True:
        job_id, payload, result_q = _stt_queue.get()
        try:
            audio_np = _bytes_to_float_array(payload["audio_bytes"])
            result   = _whisper_model.transcribe(
                audio_np,
                language=payload.get("language"),
                task=payload.get("task", "transcribe"),
                fp16=torch.cuda.is_available(),
            )
            out = {"status": "ok", "text": result["text"].strip(), "language": result.get("language")}
        except Exception as e:
            log.error(f"STT error: {e}\n{traceback.format_exc()}")
            out = {"status": "error", "error": str(e)}
        if result_q: result_q.put(out)
        else: redis_set_result(job_id, out)


def _bytes_to_float_array(audio_bytes: bytes) -> np.ndarray:
    """
    Decode audio bytes to a float32 mono 16kHz numpy array for Whisper.

    Browsers send audio/webm (Opus) from MediaRecorder, which soundfile cannot
    decode. Strategy:
      1. Try soundfile directly (works for wav, flac, ogg/vorbis, aiff)
      2. Fall back to ffmpeg via subprocess (handles webm, mp4, mp3, any format)
      3. Fall back to pydub if ffmpeg is not on PATH
    """
    import subprocess, shutil

    # ── Attempt 1: soundfile (fast, handles most non-browser formats) ─────────
    try:
        buf = io.BytesIO(audio_bytes)
        data, sr = sf.read(buf, dtype="float32", always_2d=False)
        if data.ndim == 2:
            data = data.mean(axis=1)
        if sr != 16000:
            import torchaudio
            tensor = torch.from_numpy(data).unsqueeze(0)
            data = torchaudio.transforms.Resample(sr, 16000)(tensor).squeeze(0).numpy()
        return data
    except Exception:
        pass  # fall through to ffmpeg

    # ── Attempt 2: ffmpeg (handles webm/opus, mp4, mp3, and everything else) ──
    if shutil.which("ffmpeg"):
        try:
            proc = subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-i", "pipe:0",          # read from stdin
                    "-f", "f32le",           # output raw float32 little-endian
                    "-ar", "16000",          # resample to 16kHz
                    "-ac", "1",              # mono
                    "pipe:1",                # write to stdout
                ],
                input=audio_bytes,
                capture_output=True,
                timeout=30,
            )
            if proc.returncode == 0 and proc.stdout:
                data = np.frombuffer(proc.stdout, dtype=np.float32).copy()
                log.info(f"STT: decoded {len(audio_bytes)} bytes via ffmpeg → {len(data)} samples")
                return data
            else:
                log.warning(f"ffmpeg decode failed: {proc.stderr.decode()[:200]}")
        except Exception as e:
            log.warning(f"ffmpeg error: {e}")

    # ── Attempt 3: pydub (slower, needs pydub + ffmpeg or libav) ─────────────
    try:
        from pydub import AudioSegment
        seg  = AudioSegment.from_file(io.BytesIO(audio_bytes))
        seg  = seg.set_frame_rate(16000).set_channels(1).set_sample_width(2)
        pcm  = np.frombuffer(seg.raw_data, dtype=np.int16).astype(np.float32) / 32768.0
        log.info(f"STT: decoded via pydub → {len(pcm)} samples")
        return pcm
    except Exception as e:
        log.warning(f"pydub error: {e}")

    raise RuntimeError(
        "Cannot decode audio: soundfile, ffmpeg, and pydub all failed. "
        "Install ffmpeg: apt-get install ffmpeg"
    )


def _get_img2img_pipe():
    """Build (once) an img2img pipeline that SHARES weights with _sd_pipe.

    Reuses the already-loaded components dict, so there is no extra model
    download and no additional VRAM/RAM beyond the scheduler/pipeline wrapper.
    Lazily constructed on first img2img request so the txt2img-only path is
    completely unaffected for anyone who never calls /img2img.
    """
    global _sd_img2img_pipe
    if _sd_img2img_pipe is not None:
        return _sd_img2img_pipe
    if _sd_pipe is None:
        raise RuntimeError("Stable Diffusion base pipeline not loaded.")
    is_xl = "xl" in SD_MODEL_ID.lower()
    if is_xl:
        from diffusers import StableDiffusionXLImg2ImgPipeline as I2I
    else:
        from diffusers import StableDiffusionImg2ImgPipeline as I2I
    _sd_img2img_pipe = I2I(**_sd_pipe.components)
    log.info("Stable Diffusion img2img pipeline ready (shared components).")
    return _sd_img2img_pipe


def _hex_rgb(hex_color: str, default=(0x1b, 0xd1, 0x2a)):
    h = (hex_color or "").lstrip("#")
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except Exception:
        return default


def _decode_image_b64(b64: str, bg=None):
    """Decode a base64 PNG/JPEG into an RGB PIL image. Transparent pixels are
    flattened onto `bg` (RGB tuple or hex str; default white) — PIL's bare
    .convert('RGB') flattens onto BLACK, which poisons img2img/IP-Adapter when
    the init/reference is a bg-removed RGBA cutout (dark halos + a background
    no chroma key can remove)."""
    from PIL import Image
    raw = base64.b64decode(b64)
    img = Image.open(io.BytesIO(raw))
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        if isinstance(bg, str):
            bg = _hex_rgb(bg, default=(255, 255, 255))
        base = Image.new("RGBA", img.size, tuple(bg or (255, 255, 255)) + (255,))
        img = Image.alpha_composite(base, img)
    return img.convert("RGB")


def _payload_key_rgb(payload: dict):
    """The chroma-key colour a transparent-generation payload will be keyed on."""
    return _hex_rgb(payload.get("bg_color") or "#1bd12a")


# Named key colours CLIP can actually paint. Hex codes mean nothing to SD's text
# encoder — the prompt must say "green screen", not "#1bd12a".
_KEY_COLOR_NAMES = [
    ((0x1b, 0xd1, 0x2a), "bright green"), ((0x00, 0xff, 0x00), "bright green"),
    ((0xff, 0x00, 0xff), "bright magenta"), ((0x00, 0x00, 0xff), "bright blue"),
    ((0x00, 0xff, 0xff), "bright cyan"), ((0xff, 0xff, 0x00), "bright yellow"),
    ((0xff, 0xff, 0xff), "pure white"), ((0x00, 0x00, 0x00), "pure black"),
    ((0x80, 0x80, 0x80), "flat gray"), ((0xff, 0x80, 0x00), "bright orange"),
]


def _bg_color_words(hex_color: str) -> str:
    r, g, b = _hex_rgb(hex_color)
    best, bd = "bright green", 10 ** 9
    for (cr, cg, cb), name in _KEY_COLOR_NAMES:
        d = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if d < bd:
            best, bd = name, d
    return best


def _augment_transparent_prompt(payload: dict) -> dict:
    """When transparent output is requested, actually ASK the model for the flat
    key-colour backdrop the chroma key removes afterwards. Callers historically
    said only 'plain background', so the key colour was rarely in the image and
    'transparent' generations came back fully opaque — the root cause of sprite
    sheets shipping with a baked-in background."""
    if not payload.get("transparent") or payload.get("_bgp_done"):
        return payload
    words = _bg_color_words(payload.get("bg_color") or "#1bd12a")
    p = (payload.get("prompt") or "").rstrip().rstrip(",")
    payload["prompt"] = (f"{p}, isolated on a solid flat {words} chroma key background, "
                         f"no scenery")
    neg = (payload.get("negative_prompt") or "").rstrip().rstrip(",")
    # Keep the key colour OFF the character: without these, "flat colors /
    # limited palette"-style prompts happily reuse the dominant backdrop
    # colour for clothing, glow and rim light — which the key then can't
    # remove without eating the character.
    add = (f"detailed background, scenery, gradient background, vignette, drop shadow, "
           f"{words} clothing, {words} glow, {words} tint, {words} lighting, "
           f"{words} reflections on the character")
    payload["negative_prompt"] = f"{neg}, {add}" if neg else add
    payload["_bgp_done"] = True
    return payload


def _chroma_key(image, hex_color: str = "#1bd12a", tol: int = 80):
    """Robust background key → RGBA.

    Diffusion backdrops are never perfectly flat, so a fixed hex at a fixed
    distance either leaves background chunks or eats the character. v2:
      1. estimate the ACTUAL backdrop colour from the border ring (median) and
         key on that when the border is uniform or close to the requested key;
      2. clear only key-coloured pixels CONNECTED to the border (flood fill),
         so same-coloured details inside the character survive;
      3. despill — pull the key tint out of edge pixels so no green fringe.
    """
    from PIL import Image
    req = np.array(_hex_rgb(hex_color), dtype=np.int16)
    img = image.convert("RGBA")
    arr = np.array(img).astype(np.int16)
    H, W = arr.shape[:2]
    tol = max(8, int(tol))

    # 1. Estimate the real backdrop from a border ring.
    bw = max(2, min(H, W) // 100)
    ring = np.concatenate([arr[:bw].reshape(-1, 4), arr[-bw:].reshape(-1, 4),
                           arr[:, :bw].reshape(-1, 4), arr[:, -bw:].reshape(-1, 4)])
    med = np.median(ring[:, :3], axis=0).astype(np.int16)
    ring_d = np.sqrt(((ring[:, :3] - med) ** 2).sum(axis=1))
    uniform = float((ring_d < tol).mean())
    req_d = float(np.sqrt(((med - req) ** 2).sum()))
    key = med if (req_d < 2.5 * tol or uniform >= 0.55) else req

    dist = np.sqrt(((arr[..., :3] - key) ** 2).sum(axis=-1))
    mask = dist < tol

    # 2. Keep only the border-connected region of the mask.
    reach = None
    try:
        from scipy import ndimage
        lbl, n = ndimage.label(mask)
        if n:
            border_labels = np.unique(np.concatenate([
                lbl[0], lbl[-1], lbl[:, 0], lbl[:, -1]]))
            border_labels = border_labels[border_labels != 0]
            reach = np.isin(lbl, border_labels) if border_labels.size else np.zeros_like(mask)
    except Exception:
        pass
    if reach is None:                       # numpy-only iterative flood fill
        reach = np.zeros_like(mask)
        reach[0] |= mask[0]; reach[-1] |= mask[-1]
        reach[:, 0] |= mask[:, 0]; reach[:, -1] |= mask[:, -1]
        for _ in range(H + W):
            p = np.pad(reach, 1)
            grown = (p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]) & mask
            grown |= reach
            if (grown == reach).all():
                break
            reach = grown
    if not reach.any() and uniform >= 0.85:
        # Border is uniformly key-coloured but nothing matched within tol —
        # widen once rather than returning a fully opaque "transparent" image.
        mask = dist < tol * 1.8
        reach = mask.copy()

    # 2b. Enclosed backdrop pockets (between the legs, under the arms, inside
    # an elbow crook) are NOT border-connected, so the flood fill above leaves
    # them baked in as solid key-colour blobs — the "bright green where there
    # was none" bug. It is worst on img2img/edit passes, whose RGBA init is
    # flattened ONTO the key colour, guaranteeing exactly-key pockets. Clear
    # any unreached pixel that sits WELL inside the key tolerance: a genuine
    # costume detail that close to the exact chroma key is vanishingly rare,
    # while enclosed backdrop matches it almost perfectly.
    pocket = mask & ~reach & (dist < tol * 0.65)
    if pocket.any():
        reach = reach | pocket
    arr[..., 3] = np.where(reach, 0, arr[..., 3])

    # 3. Despill the 2px fringe next to cleared background.
    dom = int(np.argmax(key))
    others = [i for i in (0, 1, 2) if i != dom]
    if key[dom] > max(key[others[0]], key[others[1]]) + 30:
        p = np.pad(reach, 2)
        near = np.zeros_like(reach)
        for dy in (-2, -1, 0, 1, 2):
            for dx in (-2, -1, 0, 1, 2):
                near |= p[2 + dy: 2 + dy + H, 2 + dx: 2 + dx + W]
        fringe = near & (arr[..., 3] > 0)
        ch = arr[..., dom]
        cap = np.maximum(arr[..., others[0]], arr[..., others[1]]) + 8
        arr[..., dom] = np.where(fringe & (ch > cap), cap, ch)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")


def _encode_image(pil_img, fmt: str = "PNG") -> str:
    buf = io.BytesIO()
    pil_img.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode()


# ── Face detection (for face-region expression editing) ───────────────────────
_FACE_CASCADE = None


def _get_face_cascade():
    """OpenCV Haar frontal-face cascade if cv2 is installed, else None."""
    global _FACE_CASCADE
    if _FACE_CASCADE is not None:
        return _FACE_CASCADE or None
    try:
        import cv2
        casc = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        _FACE_CASCADE = False if casc.empty() else casc
    except Exception as e:
        log.info(f"face cascade unavailable ({e}); using heuristic face region")
        _FACE_CASCADE = False
    return _FACE_CASCADE or None


def _detect_face_box(pil_img):
    """Return (x, y, w, h, detected, method). Uses OpenCV Haar when available;
    otherwise a centered upper-portion heuristic (our character images are
    deliberately centered head-and-shoulders portraits)."""
    W, H = pil_img.size
    casc = _get_face_cascade()
    if casc is not None:
        try:
            import cv2
            gray = cv2.cvtColor(np.array(pil_img.convert("RGB")), cv2.COLOR_RGB2GRAY)
            faces = casc.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                          minSize=(int(W * 0.10), int(H * 0.10)))
            if len(faces):
                x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                return int(x), int(y), int(w), int(h), True, "cascade"
            return (W - int(W * 0.5)) // 2, int(H * 0.10), int(W * 0.5), int(H * 0.5), False, "cascade"
        except Exception as e:
            log.debug(f"face detect: {e}")
    # Heuristic centered upper box
    w, h = int(W * 0.5), int(H * 0.5)
    return (W - w) // 2, int(H * 0.10), w, h, False, "heuristic"


def _expand_box(x, y, w, h, W, H, pad=0.45):
    px, py = int(w * pad), int(h * pad)
    nx, ny = max(0, x - px), max(0, y - py)
    return nx, ny, min(W - nx, w + 2 * px), min(H - ny, h + 2 * py)


def _get_cpu_pipe():
    """Lazily build a CPU (fp32) copy of the SD pipeline, used as an OOM
    fallback when the GPU runs out of memory mid-generation. Kept resident
    after first use (~4GB RAM for SD1.x) so later fallbacks are fast."""
    global _sd_cpu_pipe
    if _sd_cpu_pipe is not None:
        return _sd_cpu_pipe
    is_xl = "xl" in SD_MODEL_ID.lower()
    if is_xl:
        from diffusers import StableDiffusionXLPipeline as P
    else:
        from diffusers import StableDiffusionPipeline as P
    log.info("Building CPU fallback SD pipeline (fp32)…")
    _sd_cpu_pipe = P.from_pretrained(
        SD_MODEL_ID, torch_dtype=torch.float32,
        safety_checker=None, use_safetensors=True,
    ).to("cpu")
    return _sd_cpu_pipe


def _get_cpu_img2img_pipe():
    global _sd_cpu_img2img_pipe
    if _sd_cpu_img2img_pipe is not None:
        return _sd_cpu_img2img_pipe
    base = _get_cpu_pipe()
    is_xl = "xl" in SD_MODEL_ID.lower()
    if is_xl:
        from diffusers import StableDiffusionXLImg2ImgPipeline as I2I
    else:
        from diffusers import StableDiffusionImg2ImgPipeline as I2I
    _sd_cpu_img2img_pipe = I2I(**base.components)
    return _sd_cpu_img2img_pipe


def _run_sd_generation(payload: dict, device: str):
    """Run one SD generation on `device` ('cuda'|'cpu'); returns a PIL image.
    Picks the matching txt2img/img2img pipeline for that device."""
    init_b64 = payload.get("init_image_b64")
    job_id = payload.get("job_id") or ""
    steps = int(payload.get("steps", SD_DEFAULT_STEPS))
    gen = None
    if payload.get("seed") is not None:
        gen = torch.Generator(device=device).manual_seed(int(payload["seed"]))
    is_xl = "xl" in SD_MODEL_ID.lower()
    # Long-prompt embeds (compel) only on the primary device — the CPU OOM
    # fallback keeps plain prompts to avoid cross-device embedding mismatches.
    text_kw = (_text_kwargs(_sd_pipe, payload["prompt"], payload.get("negative_prompt", ""), is_xl)
               if device == _sd_device else
               {"prompt": payload["prompt"], "negative_prompt": payload.get("negative_prompt", "")})
    common = dict(
        num_inference_steps=steps,
        guidance_scale=float(payload.get("guidance",   SD_DEFAULT_GUIDANCE)),
        generator=gen, **text_kw,
    )
    w = int(payload.get("width",  SD_DEFAULT_WIDTH))
    h = int(payload.get("height", SD_DEFAULT_HEIGHT))
    _progress_set(job_id, phase="diffusion", step=0, total=steps, device=device)
    sched = payload.get("scheduler") or ""
    with torch.inference_mode():
        if init_b64:
            pipe = _get_img2img_pipe() if device == _sd_device else _get_cpu_img2img_pipe()
            # Flatten RGBA inits onto the chroma key colour (transparent runs) or
            # white — never black (see _decode_image_b64).
            init_bg = _payload_key_rgb(payload) if payload.get("transparent") else None
            init_image = _decode_image_b64(init_b64, bg=init_bg).resize((w, h))
            with _scheduler_override(pipe, sched):
                return pipe(image=init_image,
                            strength=float(payload.get("strength", 0.55)),
                            **_step_callback_kwargs(pipe, job_id, steps),
                            **common).images[0]
        pipe = _sd_pipe if device == _sd_device else _get_cpu_pipe()
        with _scheduler_override(pipe, sched):
            return pipe(width=w, height=h,
                        **_step_callback_kwargs(pipe, job_id, steps),
                        **common).images[0]


def _is_cuda_oom(err: Exception) -> bool:
    oom_cls = getattr(torch.cuda, "OutOfMemoryError", ())
    return isinstance(err, oom_cls) or "out of memory" in str(err).lower()


# Reclaim VRAM from a co-located Ollama when SD OOMs. Set SD_EVICT_OLLAMA_ON_OOM
# to 0/false/no/off to disable entirely — image generation then never disturbs
# LLM residency and an OOM goes straight to CPU.
_SD_EVICT_OLLAMA = os.getenv("SD_EVICT_OLLAMA_ON_OOM", "1").strip().lower() \
    not in ("0", "false", "no", "off", "")
# keep_alive to restore on a model that was busy when we tried to evict it, so a
# mid-generation model isn't left marked to unload the instant its gen ends.
_SD_OLLAMA_RESTORE_KA = os.getenv("SD_OLLAMA_RESTORE_KEEP_ALIVE", "10m").strip() or "10m"


def _ollama_ps() -> tuple[Optional[list], int]:
    """(resident model names, total size_vram bytes) from Ollama /api/ps, or
    (None, 0) if unreachable. Sync — runs on the SD worker thread, not the loop.
    Note: /api/ps has no 'actively generating' field, so live-vs-idle can only
    be told apart by whether an unload request actually frees the VRAM."""
    base = OLLAMA_BASE_URL.rstrip("/")
    try:
        with httpx.Client(timeout=5) as c:
            models = c.get(f"{base}/api/ps").json().get("models", []) or []
    except Exception:
        return None, 0
    names = [m.get("name") or m.get("model") for m in models]
    vram = sum(int(m.get("size_vram") or 0) for m in models)
    return [n for n in names if n], vram


def _rearm_ollama_keep_alive(models: list) -> None:
    """Restore normal keep_alive on models that were busy when we tried to evict
    them (their generation held the runner, so keep_alive:0 never freed them).
    Without this, a model that was mid-generation when SD OOM'd would unload the
    moment it finished instead of staying warm.

    Fire-and-forget on a daemon thread with a short timeout: a keep_alive-only
    request just updates the existing runner's expiry (no generation slot
    needed), so it lands fast. The short timeout also means it won't still be
    waiting when the gen ends — so it can't arrive after an unload and cold-
    reload a model. If it can't apply, the model self-heals on its next call."""
    if not models:
        return
    base = OLLAMA_BASE_URL.rstrip("/")
    ka = _SD_OLLAMA_RESTORE_KA
    def _do():
        try:
            with httpx.Client(timeout=6) as c:
                for m in models:
                    try:
                        c.post(f"{base}/api/generate", json={"model": m, "keep_alive": ka})
                    except Exception:
                        pass
        except Exception:
            pass
    threading.Thread(target=_do, name="sd-ollama-rearm", daemon=True).start()
    log.info(f"SD OOM: re-arming keep_alive={ka} on still-resident model(s) {models}")


def _evict_ollama_models() -> bool:
    """Reclaim VRAM from the co-located Ollama so an OOM'd SD job can retry on
    CUDA instead of a minutes-long CPU render — then VERIFY the VRAM was freed
    before the caller retries.

    keep_alive:0 does NOT interrupt an in-flight generation: Ollama defers the
    actual unload until the model's active-request count hits zero, so a running
    LLM call completes normally and only then unloads. The flip side is that if
    a generation IS live, the VRAM stays held and a CUDA retry would just OOM
    again — so we re-poll /api/ps and only return True once resident VRAM has
    actually dropped (an idle model unloads within ~1s). A busy model leaves us
    returning False → the caller goes straight to CPU for this one image, no
    wasted retry, and the model unloads (then reloads with its own keep_alive on
    the next real call) once its current generation finishes.

    Disabled entirely by SD_EVICT_OLLAMA_ON_OOM=0."""
    if not _SD_EVICT_OLLAMA:
        log.info("SD OOM: Ollama eviction disabled (SD_EVICT_OLLAMA_ON_OOM=0) — using CPU")
        return False
    names, vram_before = _ollama_ps()
    if names is None:
        log.info("SD OOM: no local Ollama reachable to reclaim VRAM")
        return False
    if not names:
        log.info("SD OOM: Ollama holds no resident model — nothing to reclaim")
        return False
    base = OLLAMA_BASE_URL.rstrip("/")
    log.warning(f"SD OOM: requesting unload of resident Ollama model(s) {names} "
                f"({vram_before >> 20}MB VRAM) — will not interrupt any in-flight generation")
    try:
        with httpx.Client(timeout=30) as c:
            for m in names:
                try:
                    c.post(f"{base}/api/generate", json={"model": m, "keep_alive": 0})
                except Exception as e:
                    log.warning(f"ollama unload request for {m} failed: {e}")
    except Exception as e:
        log.info(f"ollama eviction failed: {e}")
        return False
    # Verify the unload actually freed VRAM. Idle model → gone within ~1s;
    # busy model → still resident (its generation holds the runner).
    reclaimed = False
    last_still: list = list(names)            # assume all held until shown otherwise
    vram_now = vram_before
    for _ in range(6):                        # up to ~3s
        time.sleep(0.5)
        still, vram_now = _ollama_ps()
        if still is None:                     # ollama went away — assume freed
            last_still = []
            reclaimed = True
            break
        last_still = still
        if not still or vram_now < vram_before:
            reclaimed = True
            break
    # Any model we told to unload but that is STILL resident was busy (its
    # generation held the runner) — restore its keep_alive so it stays warm
    # instead of unloading the instant its current generation ends.
    survivors = [m for m in names if m in last_still]
    if survivors:
        _rearm_ollama_keep_alive(survivors)
    try: torch.cuda.empty_cache()
    except Exception: pass
    if reclaimed:
        log.warning(f"SD OOM: reclaimed VRAM ({vram_before >> 20}MB → "
                    f"{vram_now >> 20}MB) — retrying on CUDA")
        return True
    log.warning("SD OOM: Ollama VRAM still held (a generation is likely in "
                "flight) — using CPU for this image; its keep_alive restored")
    return False


# ── Sprite-pipeline GPU ops (run on the serial _sd_queue via an `op` field) ───
# Each handler takes the job payload and returns a worker `out` dict
# ({status, image_b64, device, format} or {status:"error", error}). They share
# the single SD worker so all GPU work stays serialised (no VRAM contention).

def _dep_available(modname: str) -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec(modname) is not None
    except Exception:
        return False


def _controlnet_available() -> bool:
    """ControlNet is usable if SD is loaded and diffusers exposes ControlNetModel.
    The model itself auto-downloads from HF on first /controlnet/pose call."""
    if not (ENABLE_CONTROLNET and _sd_pipe is not None and _dep_available("diffusers")):
        return False
    try:
        from diffusers import ControlNetModel  # noqa: F401
        return True
    except Exception:
        return False


def _openpose_available() -> bool:
    return bool(ENABLE_CONTROLNET and _dep_available("controlnet_aux"))


def _ipadapter_available() -> bool:
    """IP-Adapter is usable if SD is loaded and this diffusers build has
    Pipeline.load_ip_adapter (>=0.22). Weights auto-download from IPADAPTER_REPO."""
    if not (ENABLE_IPADAPTER and _sd_pipe is not None):
        return False
    try:
        from diffusers import StableDiffusionPipeline
        return hasattr(StableDiffusionPipeline, "load_ip_adapter")
    except Exception:
        return False


# ── Long prompts (compel) ─────────────────────────────────────────────────────
# SD's CLIP text encoder only reads 77 tokens. `compel` builds long-prompt
# embeddings (with attention weighting) that lift that limit. Auto-detected and
# import-guarded — without it, prompts simply truncate at 77 tokens (a warning).
ENABLE_LONG_PROMPTS = os.getenv("ENABLE_LONG_PROMPTS", "1") != "0"
_compel_procs: dict = {}   # id(pipe) → Compel processor


def _long_prompts_available() -> bool:
    return bool(ENABLE_LONG_PROMPTS and _sd_pipe is not None and _dep_available("compel"))


def _needs_long_prompt(prompt: str) -> bool:
    # Cheap heuristic (~0.75 words/token): only engage compel past CLIP's window
    # so short prompts keep their exact existing behaviour/seed reproducibility.
    return len(prompt.split()) > 60 or len(prompt) > 300


def _get_compel(pipe, is_xl: bool):
    key = id(pipe)
    if key in _compel_procs:
        return _compel_procs[key]
    from compel import Compel, ReturnedEmbeddingsType
    if is_xl:
        proc = Compel(tokenizer=[pipe.tokenizer, pipe.tokenizer_2],
                      text_encoder=[pipe.text_encoder, pipe.text_encoder_2],
                      returned_embeddings_type=ReturnedEmbeddingsType.PENULTIMATE_HIDDEN_STATES_NON_NORMALIZED,
                      requires_pooled=[False, True], truncate_long_prompts=False)
    else:
        proc = Compel(tokenizer=pipe.tokenizer, text_encoder=pipe.text_encoder,
                      truncate_long_prompts=False)
    _compel_procs[key] = proc
    return proc


def _text_kwargs(pipe, prompt: str, negative: str, is_xl: bool) -> dict:
    """Prompt kwargs for a pipe call. Builds compel long-prompt embeds when
    available and the prompt is long; otherwise plain strings. Any failure falls
    back to plain strings so the generation path is never broken."""
    negative = negative or ""
    if _long_prompts_available() and _needs_long_prompt(prompt):
        try:
            proc = _get_compel(pipe, is_xl)
            if is_xl:
                cond, pooled = proc(prompt)
                neg, neg_pooled = proc(negative)
                cond, neg = proc.pad_conditioning_tensors_to_same_length([cond, neg])
                return {"prompt_embeds": cond, "pooled_prompt_embeds": pooled,
                        "negative_prompt_embeds": neg, "negative_pooled_prompt_embeds": neg_pooled}
            cond = proc(prompt)
            neg = proc(negative)
            cond, neg = proc.pad_conditioning_tensors_to_same_length([cond, neg])
            return {"prompt_embeds": cond, "negative_prompt_embeds": neg}
        except Exception as e:
            log.warning(f"[SD] compel long-prompt failed ({e}); using plain prompt.")
    return {"prompt": prompt, "negative_prompt": negative}


def _get_rembg_session(model: str):
    model = model or REMBG_MODEL
    if model not in _rembg_sessions:
        from rembg import new_session
        _rembg_sessions[model] = new_session(model)
    return _rembg_sessions[model]


def _op_rembg(payload: dict) -> dict:
    from rembg import remove
    from PIL import Image
    img = _decode_image_b64(payload["image_b64"])
    # Aggressiveness knobs: alpha matting refines hair/soft edges; the thresholds
    # and erode size trade edge tightness vs. how much faint fringe is kept.
    kw = {}
    if payload.get("alpha_matting"):
        kw.update(alpha_matting=True,
                  alpha_matting_foreground_threshold=int(payload.get("fg_threshold", 240)),
                  alpha_matting_background_threshold=int(payload.get("bg_threshold", 10)),
                  alpha_matting_erode_size=int(payload.get("erode", 10)))
    if payload.get("post_process"):
        kw["post_process_mask"] = True
    out = remove(img, session=_get_rembg_session(payload.get("model") or REMBG_MODEL), **kw)
    if not isinstance(out, Image.Image):
        out = Image.open(io.BytesIO(out))
    return {"status": "ok", "device": "cpu", "format": "png",
            "image_b64": _encode_image(out.convert("RGBA"))}


def _get_upscaler(model: str, scale: int):
    key = (model or ESRGAN_MODEL, int(scale))
    if key in _esrgan_models:
        return _esrgan_models[key]
    from realesrgan import RealESRGANer
    from basicsr.archs.rrdbnet_arch import RRDBNet
    name = (model or ESRGAN_MODEL)
    # x4plus_anime uses a 6-block net; the default x4plus uses 23.
    nb = 6 if "anime" in name.lower() else 23
    net = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=nb,
                  num_grow_ch=32, scale=4)
    weights = os.getenv("ESRGAN_WEIGHTS", "") or f"{name}.pth"
    up = RealESRGANer(scale=4, model_path=weights, model=net,
                      half=(_sd_device == "cuda"),
                      device=("cuda" if torch.cuda.is_available() else "cpu"))
    _esrgan_models[key] = up
    return up


def _op_upscale(payload: dict) -> dict:
    from PIL import Image
    img = _decode_image_b64(payload["image_b64"])
    scale = int(payload.get("scale", 4))
    up = _get_upscaler(payload.get("model"), scale)
    out, _ = up.enhance(np.array(img), outscale=scale)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    return {"status": "ok", "device": dev, "format": "png",
            "image_b64": _encode_image(Image.fromarray(out))}


def _get_openpose_detector():
    global _openpose_detector
    if _openpose_detector is not None:
        return _openpose_detector or None
    try:
        from controlnet_aux import OpenposeDetector
        _openpose_detector = OpenposeDetector.from_pretrained("lllyasviel/Annotators")
    except Exception as e:
        log.error(f"[SD] OpenPose detector unavailable: {e}")
        _openpose_detector = False
    return _openpose_detector or None


def _get_controlnet_pipe():
    """ControlNet pipe sharing _sd_pipe's weights + a lazily-loaded ControlNet."""
    global _controlnet_pipe
    if _controlnet_pipe is not None:
        return _controlnet_pipe
    if _sd_pipe is None:
        raise RuntimeError("Stable Diffusion base pipeline not loaded.")
    from diffusers import ControlNetModel
    is_xl = "xl" in SD_MODEL_ID.lower()
    cn_id = CONTROLNET_XL_MODEL_ID if is_xl else CONTROLNET_MODEL_ID
    dtype = torch.float16 if _sd_device == "cuda" else torch.float32
    controlnet = ControlNetModel.from_pretrained(cn_id, torch_dtype=dtype)
    if is_xl:
        from diffusers import StableDiffusionXLControlNetPipeline as CNP
    else:
        from diffusers import StableDiffusionControlNetPipeline as CNP
    pipe = CNP(controlnet=controlnet, **_sd_pipe.components).to(_sd_device)
    _controlnet_pipe = pipe
    log.info("[SD] ControlNet OpenPose pipeline ready (%s).", cn_id)
    return pipe


def _op_controlnet(payload: dict) -> dict:
    from PIL import Image
    job_id = payload.get("job_id") or ""
    ref_b64 = payload.get("ref_image_b64") or ""
    ref_bg = _payload_key_rgb(payload) if payload.get("transparent") else None
    if payload.get("control_image_b64"):
        # Pose maps are skeletons on black — flatten any alpha onto black.
        control = _decode_image_b64(payload["control_image_b64"], bg=(0, 0, 0))
    elif ref_b64:
        det = _get_openpose_detector()
        if det is None:
            return {"status": "error", "error": "OpenPose detector not installed"}
        control = det(_decode_image_b64(ref_b64))
    else:
        return {"status": "error", "error": "control_image_b64 or ref_image_b64 required"}
    _apply_loras([LoRAEntry(**l) if isinstance(l, dict) else l
                  for l in (payload.get("loras") or [])])
    _augment_transparent_prompt(payload)
    pipe = _get_controlnet_pipe()
    is_xl = "xl" in SD_MODEL_ID.lower()
    w = int(payload.get("width", SD_DEFAULT_WIDTH))
    h = int(payload.get("height", SD_DEFAULT_HEIGHT))
    steps = int(payload.get("steps", SD_DEFAULT_STEPS))
    control = control.convert("RGB").resize((w, h))
    gen = (torch.Generator(device=_sd_device).manual_seed(int(payload["seed"]))
           if payload.get("seed") is not None else None)

    # Pose + identity in ONE pass: when a control image AND a reference are both
    # given and IP-Adapter is installed, load the adapter around this call so
    # ControlNet dictates the limbs while IP-Adapter keeps the character's look.
    # (The CN pipe shares the base UNet, so the same load/unload dance applies.)
    use_ip = bool(payload.get("control_image_b64") and ref_b64 and _ipadapter_available())
    extra = {}
    if use_ip:
        try:
            pipe.unet.set_default_attn_processor()
            if is_xl:
                pipe.load_ip_adapter(IPADAPTER_REPO, subfolder="sdxl_models",
                                     weight_name="ip-adapter_sdxl.bin")
            else:
                pipe.load_ip_adapter(IPADAPTER_REPO, subfolder="models",
                                     weight_name="ip-adapter_sd15.bin")
            pipe.set_ip_adapter_scale(float(payload.get("ip_scale", 0.55)))
            extra["ip_adapter_image"] = _decode_image_b64(ref_b64, bg=ref_bg).convert("RGB")
        except Exception as e:
            log.warning(f"[SD] ControlNet+IP-Adapter combine failed ({e}); pose-only.")
            use_ip = False
            extra = {}
    _progress_set(job_id, phase="diffusion", step=0, total=steps, device=_sd_device)
    try:
        with torch.inference_mode(), _scheduler_override(pipe, payload.get("scheduler") or ""):
            image = pipe(
                image=control,
                num_inference_steps=steps,
                guidance_scale=float(payload.get("guidance", SD_DEFAULT_GUIDANCE)),
                controlnet_conditioning_scale=float(payload.get("strength", 1.0)),
                width=w, height=h, generator=gen,
                **_step_callback_kwargs(pipe, job_id, steps),
                **extra,
                **_text_kwargs(pipe, payload["prompt"], payload.get("negative_prompt", ""), is_xl)
            ).images[0]
    finally:
        if use_ip:
            try: pipe.unload_ip_adapter()
            except Exception: pass
            _restore_attention()   # put the fast baseline (SDPA/xformers/sliced) back
    if payload.get("transparent"):
        _progress_set(job_id, phase="chroma-key")
        try: image = _chroma_key(image, payload.get("bg_color") or "#1bd12a",
                                 int(payload.get("chroma_tol", 80)))
        except Exception as e: log.warning(f"chroma-key failed: {e}")
    return {"status": "ok", "device": _sd_device, "format": "png",
            "tier": ("controlnet+ipadapter" if use_ip else "controlnet"),
            "image_b64": _encode_image(image)}


def _get_ipadapter_pipe():
    """Components-shared pipe used for IP-Adapter. The adapter is loaded and then
    UNLOADED around every call (see _op_ipadapter) so the shared UNet — and hence
    the base /imagine txt2img pipe — is never left mutated."""
    global _ipadapter_pipe
    if _ipadapter_pipe is not None:
        return _ipadapter_pipe
    if _sd_pipe is None:
        raise RuntimeError("Stable Diffusion base pipeline not loaded.")
    is_xl = "xl" in SD_MODEL_ID.lower()
    if is_xl:
        from diffusers import StableDiffusionXLPipeline as P
    else:
        from diffusers import StableDiffusionPipeline as P
    _ipadapter_pipe = P(**_sd_pipe.components).to(_sd_device)
    return _ipadapter_pipe


def _op_ipadapter(payload: dict) -> dict:
    from PIL import Image
    ref_bg = _payload_key_rgb(payload) if payload.get("transparent") else None
    ref = _decode_image_b64(payload["ref_image_b64"], bg=ref_bg).convert("RGB")
    _apply_loras([LoRAEntry(**l) if isinstance(l, dict) else l
                  for l in (payload.get("loras") or [])])
    _augment_transparent_prompt(payload)
    pipe = _get_ipadapter_pipe()
    is_xl = "xl" in SD_MODEL_ID.lower()
    # IP-Adapter's loader instantiates the UNet's CURRENT attn-processor class with
    # no args. If attention slicing is enabled (as _load_sd does for VRAM), that
    # class is SlicedAttnProcessor, whose __init__ requires `slice_size` → TypeError.
    # Reset to the default processor before loading, then restore slicing afterwards.
    try:
        pipe.unet.set_default_attn_processor()
    except Exception as e:
        log.debug(f"[SD] reset attn processor before IP-Adapter: {e}")
    if is_xl:
        pipe.load_ip_adapter(IPADAPTER_REPO, subfolder="sdxl_models",
                             weight_name="ip-adapter_sdxl.bin")
    else:
        pipe.load_ip_adapter(IPADAPTER_REPO, subfolder="models",
                             weight_name="ip-adapter_sd15.bin")
    try:
        pipe.set_ip_adapter_scale(float(payload.get("scale", 0.6)))
        w = int(payload.get("width", SD_DEFAULT_WIDTH))
        h = int(payload.get("height", SD_DEFAULT_HEIGHT))
        steps = int(payload.get("steps", SD_DEFAULT_STEPS))
        job_id = payload.get("job_id") or ""
        gen = (torch.Generator(device=_sd_device).manual_seed(int(payload["seed"]))
               if payload.get("seed") is not None else None)
        _progress_set(job_id, phase="diffusion", step=0, total=steps, device=_sd_device)
        with torch.inference_mode(), _scheduler_override(pipe, payload.get("scheduler") or ""):
            image = pipe(
                ip_adapter_image=ref,
                num_inference_steps=steps,
                guidance_scale=float(payload.get("guidance", SD_DEFAULT_GUIDANCE)),
                width=w, height=h, generator=gen,
                **_step_callback_kwargs(pipe, job_id, steps),
                **_text_kwargs(pipe, payload["prompt"], payload.get("negative_prompt", ""), is_xl)
            ).images[0]
    finally:
        # Always restore the shared UNet so /imagine txt2img is unaffected: drop the
        # IP-Adapter processors and re-enable the memory-saving attention slicing.
        try: pipe.unload_ip_adapter()
        except Exception: pass
        _restore_attention()   # put the fast baseline (SDPA/xformers/sliced) back
    if payload.get("transparent"):
        try: image = _chroma_key(image, payload.get("bg_color") or "#1bd12a",
                                 int(payload.get("chroma_tol", 80)))
        except Exception as e: log.warning(f"chroma-key failed: {e}")
    return {"status": "ok", "device": _sd_device, "format": "png",
            "image_b64": _encode_image(image)}


_SD_OPS = {
    "rembg":      _op_rembg,
    "upscale":    _op_upscale,
    "controlnet": _op_controlnet,
    "ipadapter":  _op_ipadapter,
}


def _sd_worker():
    log.info("SD worker started.")
    while True:
        job_id, payload, result_q = _sd_queue.get()
        prog_id = payload.get("job_id") or ""
        try:
            # Sprite-pipeline ops (rembg/upscale/controlnet/ipadapter) run through
            # their own handlers but on THIS serial worker so GPU use stays ordered.
            op = payload.get("op")
            if op in _SD_OPS:
                _progress_set(prog_id, phase=op, step=0, total=0)
                out = _SD_OPS[op](payload)
                _progress_set(prog_id,
                              phase=("done" if out.get("status") == "ok" else "error"),
                              error=out.get("error", ""))
                if result_q: result_q.put(out)
                else: redis_set_result(job_id, out)
                continue

            # Apply LoRAs if requested (GPU pipe; shared by txt2img and img2img)
            raw_loras = payload.get("loras") or []
            lora_entries = [LoRAEntry(**l) if isinstance(l, dict) else l for l in raw_loras]
            _apply_loras(lora_entries)

            # Transparent output → make the prompt request the key-colour backdrop.
            _augment_transparent_prompt(payload)

            used = _sd_device
            try:
                image = _run_sd_generation(payload, _sd_device)
            except Exception as gen_err:
                # GPU out of memory → reclaim VRAM (evict the resident Ollama
                # LLM, which gpu_first routing keeps loaded ~permanently) and
                # retry the SAME job on CUDA; only then fall back to CPU so the
                # request still succeeds (just slower). Callers learn which
                # device served it from the "device" field below.
                if _sd_device == "cuda" and _is_cuda_oom(gen_err):
                    log.warning(f"SD CUDA OOM; reclaiming VRAM and retrying. ({gen_err})")
                    try: torch.cuda.empty_cache()
                    except Exception: pass
                    image = None
                    if _evict_ollama_models():
                        try:
                            image = _run_sd_generation(payload, "cuda")
                        except Exception as retry_err:
                            if not _is_cuda_oom(retry_err):
                                raise
                            log.warning(f"still OOM after eviction; using CPU. ({retry_err})")
                    if image is None:
                        image = _run_sd_generation(payload, "cpu")
                        used = "cpu"
                else:
                    raise

            # Optional transparency: chroma-key the (solid-colour) background out.
            # `chroma_tol` controls aggressiveness (higher removes more, incl. edges).
            if payload.get("transparent"):
                _progress_set(prog_id, phase="chroma-key")
                try:
                    image = _chroma_key(image, payload.get("bg_color") or "#1bd12a",
                                        int(payload.get("chroma_tol", 80)))
                except Exception as e:
                    log.warning(f"chroma-key failed: {e}")

            buf = io.BytesIO()
            image.save(buf, format="PNG")
            out = {"status": "ok", "device": used,
                   "image_b64": base64.b64encode(buf.getvalue()).decode(), "format": "png"}
            _progress_set(prog_id, phase="done")
        except Exception as e:
            log.error(f"SD error: {e}\n{traceback.format_exc()}")
            out = {"status": "error", "error": str(e)}
            _progress_set(prog_id, phase="error", error=str(e))
        if result_q: result_q.put(out)
        else: redis_set_result(job_id, out)


def _tts_worker():
    log.info("TTS worker started.")
    while True:
        job_id, payload, result_q = _tts_queue.get()
        try:
            wav, sr = _tts_synthesize(payload["text"], payload)
            buf = io.BytesIO()
            sf.write(buf, wav, sr, format="WAV", subtype="PCM_16")
            out = {"status": "ok", "audio_b64": base64.b64encode(buf.getvalue()).decode(),
                   "format": "wav", "sample_rate": sr}
        except Exception as e:
            log.error(f"TTS error: {e}\n{traceback.format_exc()}")
            out = {"status": "error", "error": str(e)}
        if result_q: result_q.put(out)
        else: redis_set_result(job_id, out)


def _redis_listener():
    if not ENABLE_REDIS:
        return
    log.info("Redis listener started.")
    r = get_redis()
    queues = (
        ([REDIS_STT_QUEUE]     if ENABLE_WHISPER else []) +
        ([REDIS_IMAGINE_QUEUE] if ENABLE_SD      else []) +
        ([REDIS_TTS_QUEUE]     if ENABLE_TTS     else [])
    )
    while True:
        try:
            item = r.blpop(queues, timeout=2)
            if item is None:
                continue
            queue_name, raw = item
            queue_name = queue_name.decode() if isinstance(queue_name, bytes) else queue_name
            msg    = json.loads(raw)
            job_id = msg.get("job_id", str(uuid.uuid4()))
            if queue_name == REDIS_STT_QUEUE:
                msg["audio_bytes"] = base64.b64decode(msg.get("audio_b64", ""))
                _stt_queue.put((job_id, msg, None))
            elif queue_name == REDIS_IMAGINE_QUEUE:
                _sd_queue.put((job_id, msg, None))
            elif queue_name == REDIS_TTS_QUEUE:
                _tts_queue.put((job_id, msg, None))
        except Exception as e:
            log.error(f"Redis listener error: {e}")
            time.sleep(1)

# ─────────────────────────── SENTENCE HELPERS ────────────────────────────────

# Split at sentence-ending punctuation followed by whitespace.
# The negative lookbehind avoids splitting on "Mr." "Dr." etc.
_SENT_END = re.compile(r'(?<![A-Z][a-z])(?<![A-Z])(?<=[.!?])\s+')


def _extract_sentences(buf: str) -> tuple[list[str], str]:
    """Split buffer at sentence boundaries. Returns (complete_sentences, remainder)."""
    parts = _SENT_END.split(buf)
    if len(parts) <= 1:
        return [], buf
    return [s.strip() for s in parts[:-1] if s.strip()], parts[-1]


def _split_sentences(text: str) -> list[str]:
    parts = _SENT_END.split(text.strip())
    return [p.strip() for p in parts if p.strip()]

# ─────────────────────────── STARTUP ─────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_models()
    if ENABLE_WHISPER: threading.Thread(target=_stt_worker, daemon=True).start()
    if ENABLE_SD:      threading.Thread(target=_sd_worker,  daemon=True).start()
    if ENABLE_TTS:     threading.Thread(target=_tts_worker, daemon=True).start()
    if ENABLE_REDIS:
        try:
            get_redis().ping()
            threading.Thread(target=_redis_listener, daemon=True).start()
            log.info("Redis listener running.")
        except Exception as e:
            log.warning(f"Redis unavailable: {e}")
    yield


app = FastAPI(title="GPU Inference Server", version="1.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────── HELPERS ─────────────────────────────────────────

def _sync_dispatch(q: queue.Queue, job_id: str, payload: dict, timeout: int = 120) -> dict:
    result_q: queue.Queue = queue.Queue()
    if isinstance(payload, dict) and payload.get("job_id"):
        _progress_set(payload["job_id"], phase="queue", step=0)
    try:
        q.put_nowait((job_id, payload, result_q))
    except queue.Full:
        raise HTTPException(status_code=503, detail="Server busy.")
    try:
        return result_q.get(timeout=timeout)
    except queue.Empty:
        raise HTTPException(status_code=504, detail="Inference timed out.")


def _active_engine() -> str:
    return "kokoro" if _kokoro_pipeline else ("coqui" if _tts_synthesizer else "none")


def _sr_for_engine(engine: Optional[str]) -> int:
    e = engine or _active_engine()
    return KOKORO_SAMPLE_RATE if e == "kokoro" else TTS_SAMPLE_RATE


async def _stream_tts_sentences(sentences: list[str], opts: dict) -> AsyncGenerator[bytes, None]:
    loop = asyncio.get_event_loop()
    for sentence in sentences:
        try:
            wav, _ = await loop.run_in_executor(None, _tts_synthesize, sentence, opts)
            yield _wav_to_pcm_s16le(wav)
        except Exception as e:
            log.error(f"TTS stream error: {e}")

# ─────────────────────────── ROUTES ──────────────────────────────────────────

@app.get("/health")
async def health():
    engine = _active_engine()
    return {
        "status":           "ok",
        "whisper":          _whisper_model    is not None,
        "stable_diffusion": _sd_pipe          is not None,
        "sd_device":        _sd_device,
        "tts":              engine            != "none",
        "tts_engine":       engine,
        "sample_rate":      TTS_SAMPLE_RATE,
        "cuda":             torch.cuda.is_available(),
        "gpu":              torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
    }


@app.get("/tts/voices")
async def list_voices():
    engine = _active_engine()
    if engine == "kokoro":
        # Try to get live voice list from the loaded model first,
        # fall back to the static catalogue if not available
        live_voices = []
        try:
            if _kokoro_pipeline and hasattr(_kokoro_pipeline, "get_voices"):
                raw = _kokoro_pipeline.get_voices()
                # get_voices() may return a list of strings or a dict
                if isinstance(raw, dict):
                    for vid, meta in raw.items():
                        live_voices.append({
                            "id": vid,
                            "name": meta.get("name", vid) if isinstance(meta, dict) else vid,
                            "lang": meta.get("language", "en-us") if isinstance(meta, dict) else "en-us",
                            "gender": meta.get("gender", "?") if isinstance(meta, dict) else "?",
                        })
                else:
                    for vid in raw:
                        # Map to catalogue entry if known, else use raw id
                        meta = KOKORO_VOICES.get(vid, {})
                        live_voices.append({
                            "id":     vid,
                            "name":   meta.get("name", vid),
                            "lang":   meta.get("lang", "en-us"),
                            "gender": meta.get("gender", "?"),
                        })
        except Exception as e:
            log.warning(f"Could not get live Kokoro voices: {e}")

        voices = live_voices if live_voices else [
            {"id": vid, "name": v["name"], "lang": v["lang"], "gender": v["gender"]}
            for vid, v in KOKORO_VOICES.items()
        ]
    else:
        voices = [{"id": "default", "name": "Default", "lang": "en", "gender": "?"}]
        if _tts_synthesizer and hasattr(_tts_synthesizer, "speakers"):
            voices = [
                {"id": s, "name": s, "lang": "en", "gender": "?"}
                for s in (_tts_synthesizer.speakers or [])
            ]
    return {"engine": engine, "voices": voices}


# ── STT ──────────────────────────────────────────────────────────────────────

@app.post("/stt")
async def stt(
    file: UploadFile = File(...),
    language: Optional[str] = Form(None),
    task: str = Form("transcribe"),
):
    if not ENABLE_WHISPER or _whisper_model is None:
        raise HTTPException(status_code=503, detail="Whisper not loaded.")
    audio_bytes = await file.read()
    result = _sync_dispatch(_stt_queue, str(uuid.uuid4()), {
        "audio_bytes": audio_bytes, "language": language, "task": task,
    })
    if result["status"] == "error":
        raise HTTPException(status_code=500, detail=result["error"])
    return result


# ── Image generation ──────────────────────────────────────────────────────────

class LoRAEntry(BaseModel):
    name: str           # filename stem, e.g. "add_detail"
    weight: float = 1.0 # typically 0.5–1.5

class ImagineRequest(BaseModel):
    prompt: str
    negative_prompt: str = ""
    steps: int           = SD_DEFAULT_STEPS
    guidance: float      = SD_DEFAULT_GUIDANCE
    width: int           = SD_DEFAULT_WIDTH
    height: int          = SD_DEFAULT_HEIGHT
    seed: Optional[int]  = None
    loras: list[LoRAEntry] = []   # LoRAs to apply for this generation
    transparent: bool    = False  # chroma-key the background out to alpha
    bg_color: str        = ""     # hex key colour; "" = default bright green
    chroma_tol: int      = 80     # chroma-key aggressiveness (higher removes more)
    job_id: str          = ""     # optional: poll GET /progress/{job_id} for live steps
    scheduler: str       = ""     # per-job sampler override (see SD_SCHEDULERS); "" = default


def _scan_lora_dir() -> list[dict]:
    """Scan SD_LORA_DIR for .safetensors and .pt LoRA files."""
    lora_dir = SD_LORA_DIR.strip()
    if not lora_dir or not os.path.isdir(lora_dir):
        return []
    files = []
    for fname in sorted(os.listdir(lora_dir)):
        if fname.lower().endswith(('.safetensors', '.pt', '.bin')):
            stem = os.path.splitext(fname)[0]
            path = os.path.join(lora_dir, fname)
            size_mb = os.path.getsize(path) / 1024 / 1024
            files.append({
                "name":     stem,
                "filename": fname,
                "path":     path,
                "size_mb":  round(size_mb, 1),
            })
    return files


def _apply_loras(loras: list[LoRAEntry]):
    """
    Load and fuse LoRA weights into the pipeline for a generation.
    Unloads any previously loaded LoRAs first.
    diffusers >= 0.21 supports load_lora_weights() + set_adapters().
    """
    global _sd_loaded_loras

    if not loras:
        # Unload all if none requested
        if _sd_loaded_loras:
            try:
                _sd_pipe.unload_lora_weights()
                log.info("[SD] LoRAs unloaded")
            except Exception as e:
                log.warning(f"[SD] unload_lora_weights: {e}")
            _sd_loaded_loras = {}
        return

    lora_dir = SD_LORA_DIR.strip()
    adapter_names  = []
    adapter_weights = []

    for entry in loras:
        path = os.path.join(lora_dir, entry.name + ".safetensors")
        if not os.path.exists(path):
            path = os.path.join(lora_dir, entry.name + ".pt")
        if not os.path.exists(path):
            log.warning(f"[SD] LoRA not found: {entry.name}")
            continue
        try:
            _sd_pipe.load_lora_weights(lora_dir, weight_name=os.path.basename(path),
                                        adapter_name=entry.name)
            adapter_names.append(entry.name)
            adapter_weights.append(entry.weight)
            log.info(f"[SD] loaded LoRA {entry.name!r} @ {entry.weight}")
        except Exception as e:
            log.error(f"[SD] failed to load LoRA {entry.name!r}: {e}")

    if adapter_names:
        try:
            _sd_pipe.set_adapters(adapter_names, adapter_weights=adapter_weights)
        except Exception as e:
            log.error(f"[SD] set_adapters failed: {e}")

    _sd_loaded_loras = {n: w for n, w in zip(adapter_names, adapter_weights)}


@app.get("/sd/loras")
async def list_loras():
    """List available LoRA files from SD_LORA_DIR."""
    return {"loras": _scan_lora_dir(), "lora_dir": SD_LORA_DIR}


def _safe_lora_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", (name or "").strip())


def _lora_dest(filename: str) -> str:
    """Resolve a sanitised destination path inside SD_LORA_DIR (traversal-guarded)."""
    lora_dir = SD_LORA_DIR.strip()
    if not lora_dir:
        raise HTTPException(status_code=503, detail="SD_LORA_DIR is not configured on this server.")
    os.makedirs(lora_dir, exist_ok=True)
    fn = _safe_lora_name(filename) or "lora.safetensors"
    if not fn.lower().endswith((".safetensors", ".pt", ".bin")):
        fn += ".safetensors"
    dest = os.path.abspath(os.path.join(lora_dir, fn))
    if os.path.commonpath([dest, os.path.abspath(lora_dir)]) != os.path.abspath(lora_dir):
        raise HTTPException(status_code=400, detail="invalid filename")
    return dest


class LoraDownloadRequest(BaseModel):
    url: str
    filename: str = ""       # default: derived from the URL
    token: str = ""          # optional bearer (e.g. Civitai); else CIVITAI_TOKEN env


@app.post("/sd/loras/download")
async def download_lora(req: LoraDownloadRequest):
    """Download a LoRA from a URL (Civitai / HuggingFace / direct) into SD_LORA_DIR
    so it can be used by /imagine. Streams to disk; cleans up a partial file on
    failure. Civitai often needs a token — pass `token` or set CIVITAI_TOKEN."""
    from urllib.parse import urlparse, unquote
    fn = req.filename or os.path.basename(unquote(urlparse(req.url).path))
    dest = _lora_dest(fn)
    host = (urlparse(req.url).hostname or "").lower()
    # Scope tokens to their host: a Civitai bearer sent to HuggingFace makes HF
    # return 401 for files that would download fine anonymously (and vice versa).
    headers = {}
    url = req.url
    if "civitai" in host:
        token = req.token or os.getenv("CIVITAI_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
            if "token=" not in url:  # Civitai's download endpoint also accepts ?token=
                url += ("&" if "?" in url else "?") + "token=" + token
    elif "huggingface" in host or "hf.co" in host:
        token = req.token or os.getenv("HF_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif req.token:
        headers["Authorization"] = f"Bearer {req.token}"
    size = 0
    try:
        async with httpx.AsyncClient(timeout=900, follow_redirects=True) as c:
            async with c.stream("GET", url, headers=headers) as r:
                r.raise_for_status()
                ctype = (r.headers.get("content-type") or "").lower()
                if "text/html" in ctype:
                    raise RuntimeError(
                        f"{host} sent an HTML page instead of a file — geo-block, "
                        f"login wall or consent page. Set HTTPS_PROXY on this node "
                        f"or supply a token.")
                with open(dest, "wb") as f:
                    async for chunk in r.aiter_bytes(1 << 20):
                        f.write(chunk)
                        size += len(chunk)
    except Exception as e:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except Exception:
            pass
        hint = ""
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (401, 403, 451):
            hint = (" — Civitai downloads usually need an API token (CIVITAI_TOKEN), "
                    "and some regions are geo-blocked (set HTTPS_PROXY)."
                    if "civitai" in host else
                    " — gated repo? Set HF_TOKEN or accept the license on the hub.")
        raise HTTPException(status_code=502, detail=f"download failed: {e}{hint}")
    return {"status": "ok", "name": os.path.splitext(os.path.basename(dest))[0],
            "filename": os.path.basename(dest), "size_mb": round(size / 1024 / 1024, 1)}


class LoraDeleteRequest(BaseModel):
    name: str                # LoRA name (filename stem) to remove


@app.post("/sd/loras/delete")
async def delete_lora(req: LoraDeleteRequest):
    """Remove a LoRA file (any of .safetensors/.pt/.bin) from SD_LORA_DIR."""
    lora_dir = SD_LORA_DIR.strip()
    if not lora_dir:
        raise HTTPException(status_code=503, detail="SD_LORA_DIR is not configured.")
    removed = []
    for ext in (".safetensors", ".pt", ".bin"):
        try:
            p = _lora_dest(_safe_lora_name(req.name) + ext)
        except HTTPException:
            continue
        if os.path.isfile(p):
            try:
                os.remove(p)
                removed.append(os.path.basename(p))
            except Exception as e:
                log.warning(f"lora delete: {e}")
    return {"status": "ok", "removed": removed}


@app.post("/imagine")
async def imagine(req: ImagineRequest):
    if not ENABLE_SD or _sd_pipe is None:
        raise HTTPException(status_code=503, detail="Stable Diffusion not loaded.")
    result = _sync_dispatch(_sd_queue, str(uuid.uuid4()), req.model_dump(), timeout=300)
    if result["status"] == "error":
        raise HTTPException(status_code=500, detail=result["error"])
    return result


class Img2ImgRequest(BaseModel):
    prompt: str
    init_image_b64: str                # base64 PNG/JPEG to derive from
    strength: float      = 0.55        # 0=keep init, 1=ignore init
    negative_prompt: str = ""
    steps: int           = SD_DEFAULT_STEPS
    guidance: float      = SD_DEFAULT_GUIDANCE
    width: int           = SD_DEFAULT_WIDTH
    height: int          = SD_DEFAULT_HEIGHT
    seed: Optional[int]  = None
    loras: list[LoRAEntry] = []
    transparent: bool    = False  # chroma-key the background out to alpha
    bg_color: str        = ""     # hex key colour; "" = default bright green
    chroma_tol: int      = 80     # chroma-key aggressiveness (higher removes more)
    job_id: str          = ""     # optional: poll GET /progress/{job_id} for live steps
    scheduler: str       = ""     # per-job sampler override (see SD_SCHEDULERS); "" = default


@app.post("/img2img")
async def img2img(req: Img2ImgRequest):
    """Derive a new image from an init image (same model/weights as /imagine).

    Used for consistent character expression frames: generate one base portrait,
    then re-roll each expression from it at low strength so identity is preserved.
    Runs on the same single SD worker queue as /imagine, so generations stay
    serialised and the txt2img path is unaffected.
    """
    if not ENABLE_SD or _sd_pipe is None:
        raise HTTPException(status_code=503, detail="Stable Diffusion not loaded.")
    result = _sync_dispatch(_sd_queue, str(uuid.uuid4()), req.model_dump(), timeout=300)
    if result["status"] == "error":
        raise HTTPException(status_code=500, detail=result["error"])
    return result


class ExpressionRequest(BaseModel):
    base_image_b64: str               # the character image to edit
    prompt: str                       # identity + target expression
    negative_prompt: str = ""
    strength: float      = 0.5        # img2img strength on the face crop
    steps: int           = SD_DEFAULT_STEPS
    guidance: float      = SD_DEFAULT_GUIDANCE
    seed: Optional[int]  = None
    face_box: Optional[list] = None   # [x,y,w,h] override (else auto-detect)
    pad: float           = 0.45       # context padding around the face


@app.post("/expression")
async def expression(req: ExpressionRequest):
    """Change ONLY the face of a character image to a new expression, keeping the
    background, body and framing IDENTICAL.

    Detects the face (or uses face_box), runs img2img on a padded crop of just
    that region, then composites the result back with a feathered elliptical
    mask. This is what makes per-state emotions read as the *same* character in
    the *same* scene, instead of a brand-new image each time. Returns
    {image_b64, face_detected, face_method, face_box, device}.
    """
    if not ENABLE_SD or _sd_pipe is None:
        raise HTTPException(status_code=503, detail="Stable Diffusion not loaded.")
    from PIL import Image, ImageDraw, ImageFilter
    raw = base64.b64decode(req.base_image_b64)
    base = Image.open(io.BytesIO(raw))
    base = base.convert("RGBA") if (base.mode in ("RGBA", "LA")
                                    or "transparency" in base.info) else base.convert("RGB")
    W, H = base.size
    if req.face_box and len(req.face_box) == 4:
        fx, fy, fw, fh = [int(v) for v in req.face_box]
        detected, method = True, "given"
    else:
        fx, fy, fw, fh, detected, method = _detect_face_box(base)
    ex, ey, ew, eh = _expand_box(fx, fy, fw, fh, W, H, req.pad)

    crop = base.crop((ex, ey, ex + ew, ey + eh)).convert("RGB")
    gw, gh = max(384, ew - ew % 8), max(384, eh - eh % 8)
    payload = {
        "prompt": req.prompt, "negative_prompt": req.negative_prompt,
        "init_image_b64": _encode_image(crop.resize((gw, gh))),
        "strength": req.strength, "steps": req.steps, "guidance": req.guidance,
        "width": gw, "height": gh,
    }
    if req.seed is not None:
        payload["seed"] = req.seed
    result = _sync_dispatch(_sd_queue, str(uuid.uuid4()), payload, timeout=300)
    if result["status"] == "error":
        raise HTTPException(status_code=500, detail=result["error"])

    newface = _decode_image_b64(result["image_b64"]).resize((ew, eh))
    mask = Image.new("L", (ew, eh), 0)
    inset = int(min(ew, eh) * 0.10)
    ImageDraw.Draw(mask).ellipse([inset, inset, ew - inset, eh - inset], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(max(4, min(ew, eh) // 12)))
    out = base.copy()   # keep base's mode (RGBA preserved for transparent sprites)
    out.paste(newface, (ex, ey), mask)
    return {"status": "ok", "format": "png", "device": result.get("device"),
            "face_detected": detected, "face_method": method,
            "face_box": [fx, fy, fw, fh], "image_b64": _encode_image(out)}


# ── Sprite-pipeline endpoints (background removal / pose / identity / upscale) ──
# Each dispatches an `op` job onto the serial _sd_queue. They 503 cleanly when the
# tier isn't enabled/installed so the Vera side falls back to chroma-key / img2img
# / Lanczos.

class RembgRequest(BaseModel):
    image_b64: str
    model: str = ""                    # u2net | u2netp | isnet-general-use | …
    alpha_matting: bool = False        # refine soft/hair edges (slower)
    fg_threshold: int = 240            # alpha-matting foreground threshold
    bg_threshold: int = 10             # alpha-matting background threshold
    erode: int = 10                    # alpha-matting erode size
    post_process: bool = False         # clean the mask (removes speckle/halo)
    job_id: str = ""                   # optional live-progress key


@app.post("/rembg")
async def rembg_route(req: RembgRequest):
    if not (ENABLE_REMBG and _dep_available("rembg")):
        raise HTTPException(status_code=503, detail="Background removal (rembg) not available.")
    payload = req.model_dump()
    payload["op"] = "rembg"
    result = _sync_dispatch(_sd_queue, str(uuid.uuid4()), payload, timeout=180)
    if result["status"] == "error":
        raise HTTPException(status_code=500, detail=result["error"])
    return result


class UpscaleRequest(BaseModel):
    image_b64: str
    scale: int = 4
    model: str = ""
    job_id: str = ""                   # optional live-progress key


@app.post("/upscale")
async def upscale_route(req: UpscaleRequest):
    if not (ENABLE_UPSCALE and _dep_available("realesrgan") and _dep_available("basicsr")):
        raise HTTPException(status_code=503, detail="ESRGAN upscaler not available.")
    result = _sync_dispatch(_sd_queue, str(uuid.uuid4()),
                            {"op": "upscale", "image_b64": req.image_b64,
                             "scale": req.scale, "model": req.model}, timeout=300)
    if result["status"] == "error":
        raise HTTPException(status_code=500, detail=result["error"])
    return result


class ControlNetRequest(BaseModel):
    prompt: str
    control_image_b64: str = ""        # a pose/skeleton PNG …
    ref_image_b64: str     = ""        # … or a reference to derive the skeleton from.
                                       # With BOTH given (and IP-Adapter installed) the
                                       # ref also locks identity during the posed render.
    negative_prompt: str   = ""
    strength: float        = 1.0       # controlnet_conditioning_scale
    ip_scale: float        = 0.55      # IP-Adapter identity strength when combined
    steps: int             = SD_DEFAULT_STEPS
    guidance: float        = SD_DEFAULT_GUIDANCE
    width: int             = SD_DEFAULT_WIDTH
    height: int            = SD_DEFAULT_HEIGHT
    seed: Optional[int]    = None
    loras: list[LoRAEntry] = []
    transparent: bool      = False
    bg_color: str          = ""
    chroma_tol: int        = 80
    job_id: str            = ""        # optional live-progress key
    scheduler: str         = ""        # per-job sampler override; "" = default


@app.post("/controlnet/pose")
async def controlnet_pose_route(req: ControlNetRequest):
    if not _controlnet_available():
        raise HTTPException(status_code=503, detail="ControlNet OpenPose not available.")
    if not req.control_image_b64 and req.ref_image_b64 and not _openpose_available():
        raise HTTPException(status_code=503,
                            detail="Deriving a pose from a reference needs controlnet_aux "
                                   "(pip install controlnet_aux); or pass control_image_b64.")
    payload = req.model_dump()
    payload["op"] = "controlnet"
    result = _sync_dispatch(_sd_queue, str(uuid.uuid4()), payload, timeout=300)
    if result["status"] == "error":
        raise HTTPException(status_code=500, detail=result["error"])
    return result


class IPAdapterRequest(BaseModel):
    prompt: str
    ref_image_b64: str                 # identity reference
    scale: float           = 0.6
    control_image_b64: str = ""
    negative_prompt: str   = ""
    steps: int             = SD_DEFAULT_STEPS
    guidance: float        = SD_DEFAULT_GUIDANCE
    width: int             = SD_DEFAULT_WIDTH
    height: int            = SD_DEFAULT_HEIGHT
    seed: Optional[int]    = None
    loras: list[LoRAEntry] = []
    transparent: bool      = False
    bg_color: str          = ""
    chroma_tol: int        = 80
    job_id: str            = ""        # optional live-progress key
    scheduler: str         = ""        # per-job sampler override; "" = default


@app.post("/ipadapter")
async def ipadapter_route(req: IPAdapterRequest):
    if not _ipadapter_available():
        raise HTTPException(status_code=503, detail="IP-Adapter not available.")
    payload = req.model_dump()
    payload["op"] = "ipadapter"
    result = _sync_dispatch(_sd_queue, str(uuid.uuid4()), payload, timeout=300)
    if result["status"] == "error":
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/sd/capabilities")
async def sd_capabilities():
    """Report which image-generation tiers this server can serve right now.

    Lets callers pick the best-available generation path and degrade
    gracefully. img2img is available whenever the base SD pipeline is loaded
    (it is built on demand from the same components). ControlNet / talking-head
    are not provided by this server build.
    """
    sd_ready = bool(ENABLE_SD and _sd_pipe is not None)
    return {
        "txt2img":      sd_ready,
        "img2img":      sd_ready,
        # Sprite-pipeline tiers (auto-detected by importability; see ENABLE_* flags).
        "controlnet":   _controlnet_available(),
        "openpose":     _openpose_available(),   # pose can be derived from a reference
        "ipadapter":    _ipadapter_available(),
        "rembg":        bool(ENABLE_REMBG and _dep_available("rembg")),
        "upscale":      bool(ENABLE_UPSCALE and _dep_available("realesrgan")
                            and _dep_available("basicsr")),
        "long_prompts": _long_prompts_available(),   # compel lifts the 77-token cap
        "schedulers":   SD_SCHEDULERS,               # per-job sampler overrides
        "talking_head": False,
        "model":        SD_MODEL_ID,
        "device":       _sd_device,
        "loras":        len(_scan_lora_dir()),
    }


# ── Thumbnails (SD image + PIL text overlay) ──────────────────────────────────
# Text is drawn with PIL (NOT Stable Diffusion, which can't spell). SD generates
# at an SD-friendly size for the target aspect; we upscale to the exact target
# then overlay a title/subtitle with an outline for legibility.

THUMB_PRESETS = {
    "youtube":    (1280, 720),
    "youtube_hd": (1920, 1080),
    "shorts":     (1080, 1920),
    "square":     (1080, 1080),
    "twitter":    (1200, 675),
    "og":         (1200, 630),
}

_FONT_CANDIDATES = [
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]
try:  # matplotlib bundles DejaVu — a reliable fallback in ML envs
    import matplotlib as _mpl
    _FONT_CANDIDATES.append(os.path.join(os.path.dirname(_mpl.__file__),
                                          "mpl-data", "fonts", "ttf", "DejaVuSans-Bold.ttf"))
except Exception:
    pass


def _load_font(size: int):
    from PIL import ImageFont
    for cand in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(cand, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap_text(draw, text, font, max_w):
    lines, cur = [], ""
    for word in text.split():
        test = (cur + " " + word).strip()
        if draw.textlength(test, font=font) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _fit_font(draw, text, max_w, max_h, start, min_size=14):
    size = max(min_size, int(start))
    while size >= min_size:
        font = _load_font(size)
        lines = _wrap_text(draw, text, font, max_w)
        if len(lines) * size * 1.2 <= max_h and \
           all(draw.textlength(ln, font=font) <= max_w for ln in lines):
            return font, lines, size
        size -= 4
    font = _load_font(min_size)
    return font, _wrap_text(draw, text, font, max_w), min_size


def _sd_gen_size(w, h, gen_long=640):
    """An SD-friendly generation size (multiple of 8) matching the target aspect."""
    if w >= h:
        gw, gh = gen_long, max(384, round(gen_long * h / w))
    else:
        gh, gw = gen_long, max(384, round(gen_long * w / h))
    return gw - gw % 8, gh - gh % 8


def _overlay_thumbnail(img, W, H, title="", subtitle="", position="bottom",
                       text_color="#ffffff", stroke_color="#000000"):
    from PIL import Image, ImageDraw
    base = img.convert("RGB").resize((W, H), Image.LANCZOS)
    draw = ImageDraw.Draw(base)
    margin = int(W * 0.05)
    max_w = W - 2 * margin
    blocks = []   # (lines, font, line_height, size)
    if title:
        f, lines, sz = _fit_font(draw, title, max_w, H * 0.45, H * 0.20)
        blocks.append((lines, f, int(sz * 1.18), sz))
    if subtitle:
        f, lines, sz = _fit_font(draw, subtitle, max_w, H * 0.22, H * 0.09)
        blocks.append((lines, f, int(sz * 1.18), sz))
    total_h = sum(len(l) * lh for (l, _f, lh, _s) in blocks) + (10 if len(blocks) > 1 else 0)
    if position == "top":
        y = margin
    elif position == "center":
        y = max(margin, (H - total_h) // 2)
    else:
        y = H - total_h - margin
    for (lines, font, lh, sz) in blocks:
        stroke = max(2, sz // 14)
        for ln in lines:
            tw = draw.textlength(ln, font=font)
            draw.text(((W - tw) // 2, y), ln, font=font, fill=text_color,
                      stroke_width=stroke, stroke_fill=stroke_color)
            y += lh
        y += 10
    return base


class ThumbnailRequest(BaseModel):
    prompt: str
    negative_prompt: str = ""
    preset: str          = "youtube"     # see THUMB_PRESETS; ignored if width&height set
    width: int           = 0
    height: int          = 0
    steps: int           = SD_DEFAULT_STEPS
    guidance: float      = SD_DEFAULT_GUIDANCE
    seed: Optional[int]  = None
    loras: list[LoRAEntry] = []
    title: str           = ""
    subtitle: str        = ""
    position: str        = "bottom"       # top | center | bottom
    text_color: str      = "#ffffff"
    stroke_color: str    = "#000000"


@app.get("/thumbnail/presets")
async def thumbnail_presets():
    return {"presets": [{"id": k, "width": w, "height": h}
                        for k, (w, h) in THUMB_PRESETS.items()]}


@app.post("/thumbnail")
async def thumbnail(req: ThumbnailRequest):
    if not ENABLE_SD or _sd_pipe is None:
        raise HTTPException(status_code=503, detail="Stable Diffusion not loaded.")
    W, H = (req.width, req.height) if (req.width and req.height) \
        else THUMB_PRESETS.get(req.preset, (1280, 720))
    gw, gh = _sd_gen_size(W, H)
    payload = {
        "prompt": req.prompt, "negative_prompt": req.negative_prompt,
        "steps": req.steps, "guidance": req.guidance, "width": gw, "height": gh,
        "loras": [l.model_dump() if hasattr(l, "model_dump") else l for l in req.loras],
    }
    if req.seed is not None:
        payload["seed"] = req.seed
    result = _sync_dispatch(_sd_queue, str(uuid.uuid4()), payload, timeout=300)
    if result["status"] == "error":
        raise HTTPException(status_code=500, detail=result["error"])

    img = _decode_image_b64(result["image_b64"])
    try:
        final = _overlay_thumbnail(img, W, H, req.title, req.subtitle,
                                   req.position, req.text_color, req.stroke_color)
    except Exception as e:
        log.warning(f"thumbnail overlay failed: {e}")
        final = img.convert("RGB").resize((W, H))
    buf = io.BytesIO()
    final.save(buf, format="PNG")
    return {"status": "ok", "format": "png", "width": W, "height": H,
            "device": result.get("device"),
            "image_b64": base64.b64encode(buf.getvalue()).decode()}


# ── TTS ───────────────────────────────────────────────────────────────────────

class TTSRequest(BaseModel):
    text: str
    engine:   Optional[str]   = None   # "kokoro" | "coqui" | None → server default
    voice:    Optional[str]   = None   # kokoro voice id
    speaker:  Optional[str]   = None   # coqui speaker name
    language: Optional[str]   = None
    speed:    Optional[float] = 1.0


@app.post("/tts")
async def tts(req: TTSRequest):
    if not ENABLE_TTS or _active_engine() == "none":
        raise HTTPException(status_code=503, detail="TTS not loaded.")
    result = _sync_dispatch(_tts_queue, str(uuid.uuid4()), req.model_dump())
    if result["status"] == "error":
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.post("/tts/stream")
async def tts_stream(req: TTSRequest):
    if not ENABLE_TTS or _active_engine() == "none":
        raise HTTPException(status_code=503, detail="TTS not loaded.")
    sentences = _split_sentences(req.text)
    if not sentences:
        raise HTTPException(status_code=400, detail="Empty text.")
    opts = req.model_dump()
    sr   = _sr_for_engine(req.engine)
    return StreamingResponse(
        _stream_tts_sentences(sentences, opts),
        media_type="audio/pcm",
        headers={"X-Sample-Rate": str(sr), "X-Channels": "1", "X-Bit-Depth": "16"},
    )


# ── Chat + Speak (single Ollama call → fan-out) ───────────────────────────────

_chat_sessions: dict[str, dict] = {}


class ChatSpeakRequest(BaseModel):
    model:    str            = "llama3.2"
    prompt:   str
    engine:   Optional[str]   = None
    voice:    Optional[str]   = None
    speaker:  Optional[str]   = None
    language: Optional[str]   = None
    speed:    Optional[float] = 1.0
    session_id: Optional[str] = None


@app.post("/chat/speak")
async def chat_speak(req: ChatSpeakRequest):
    """
    Calls Ollama once. Fans every token to:
      • text SSE queue   → GET /chat/text/{session_id}
      • TTS pipeline     → PCM audio returned on this response

    Tokens are base64-encoded in the SSE stream to avoid all space/newline
    stripping issues with the SSE protocol.
    """
    if not ENABLE_TTS or _active_engine() == "none":
        raise HTTPException(status_code=503, detail="TTS not loaded.")

    ollama_base = OLLAMA_BASE_URL.rstrip("/")
    tts_opts    = {k: getattr(req, k) for k in ("engine", "voice", "speaker", "language", "speed")}
    session_id  = req.session_id or str(uuid.uuid4())
    sr          = _sr_for_engine(req.engine)

    text_q     = asyncio.Queue(maxsize=4096)
    done_event = asyncio.Event()
    _chat_sessions[session_id] = {"text_q": text_q, "done": done_event}

    log.info(f"[chat/speak] sid={session_id} model={req.model!r} engine={req.engine or _active_engine()} ollama={ollama_base!r}")

    async def generate() -> AsyncGenerator[bytes, None]:
        text_buf   = ""
        loop       = asyncio.get_event_loop()
        chunks_out = 0
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=10.0),
                follow_redirects=True,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{ollama_base}/api/chat",
                    json={
                        "model":    req.model,
                        "messages": [{"role": "user", "content": req.prompt}],
                        "stream":   True,
                    },
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        log.error(f"[chat/speak] Ollama {resp.status_code}: {body.decode()[:500]}")
                        return

                    log.info("[chat/speak] Ollama stream open…")
                    async for raw_line in resp.aiter_lines():
                        if not raw_line.strip():
                            continue
                        try:
                            obj = json.loads(raw_line)
                        except json.JSONDecodeError:
                            continue

                        token     = obj.get("message", {}).get("content", "")
                        text_buf += token

                        # Fan ALL tokens including whitespace-only to text queue
                        await text_q.put(("token", token))

                        sentences, text_buf = _extract_sentences(text_buf)
                        for sentence in sentences:
                            if not sentence:
                                continue
                            log.info(f"[chat/speak] TTS → {sentence!r}")
                            try:
                                wav, _ = await loop.run_in_executor(
                                    None, _tts_synthesize, sentence, tts_opts
                                )
                                yield _wav_to_pcm_s16le(wav)
                                chunks_out += 1
                            except Exception as tts_err:
                                log.error(f"[chat/speak] TTS error: {tts_err}\n{traceback.format_exc()}")

                        if obj.get("done"):
                            log.info("[chat/speak] Ollama done")
                            break

            if text_buf.strip():
                log.info(f"[chat/speak] flush: {text_buf.strip()!r}")
                try:
                    wav, _ = await loop.run_in_executor(
                        None, _tts_synthesize, text_buf.strip(), tts_opts
                    )
                    yield _wav_to_pcm_s16le(wav)
                    chunks_out += 1
                except Exception as e:
                    log.error(f"[chat/speak] flush error: {e}")

            log.info(f"[chat/speak] complete — {chunks_out} PCM chunks")

        except httpx.ConnectError as e:
            log.error(f"[chat/speak] cannot connect to Ollama: {e}")
        except httpx.HTTPError as e:
            log.error(f"[chat/speak] HTTP error: {e}")
        except Exception as e:
            log.error(f"[chat/speak] error: {e}\n{traceback.format_exc()}")
        finally:
            await text_q.put(("done", None))
            done_event.set()
            async def _cleanup():
                await asyncio.sleep(30)
                _chat_sessions.pop(session_id, None)
            asyncio.create_task(_cleanup())

    return StreamingResponse(
        generate(),
        media_type="audio/pcm",
        headers={
            "X-Sample-Rate":     str(sr),
            "X-Channels":        "1",
            "X-Bit-Depth":       "16",
            "X-Session-Id":      session_id,
            "Transfer-Encoding": "chunked",
        },
    )


@app.get("/chat/text/{session_id}")
async def chat_text(session_id: str):
    """
    SSE text token stream for a /chat/speak session.
    Tokens are base64-encoded to prevent any SSE whitespace/newline corruption.

    Events:
      data: <base64(token_utf8)>   — each LLM token
      data: [DONE]                 — stream finished
    """
    # Wait briefly in case /chat/speak hasn't registered the session yet
    for _ in range(40):
        if session_id in _chat_sessions:
            break
        await asyncio.sleep(0.05)

    session = _chat_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id!r} not found.")

    text_q = session["text_q"]

    async def event_stream():
        while True:
            kind, data = await text_q.get()
            if kind == "done":
                yield "data: [DONE]\n\n"
                break
            if kind == "token":
                # Base64-encode so spaces, newlines, colons are all safe
                b64 = base64.b64encode(data.encode("utf-8")).decode("ascii")
                yield f"data: {b64}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Live job progress polling ─────────────────────────────────────────────────

@app.get("/progress/{job_id}")
async def get_progress(job_id: str):
    """Live progress for an in-flight image job that was submitted with a
    `job_id`: {phase, step, total, preview_b64?, device?}. `phase` walks
    queue→diffusion→(chroma-key|rembg|upscale…)→done|error. `preview_b64` is a
    small approximate render of the diffusion state — show it to the user so
    they can watch the image form. 404 until the job has been seen."""
    ent = _progress_get(job_id)
    if ent is None:
        return JSONResponse(status_code=404,
                            content={"status": "unknown", "job_id": job_id})
    ent.setdefault("phase", "queue")
    ent["status"] = "ok"
    return ent


# ── Result polling ────────────────────────────────────────────────────────────

@app.get("/result/{job_id}")
async def get_result(job_id: str):
    if not ENABLE_REDIS:
        raise HTTPException(status_code=404, detail="Redis not enabled.")
    result = redis_get_result(job_id)
    if result is None:
        return JSONResponse(status_code=202, content={"status": "pending", "job_id": job_id})
    return result



# ── Duplex voice session (interrupt-safe) ────────────────────────────────────
#
# POST /duplex/start          — start a session, returns session_id
# POST /duplex/query          — submit text or audio query, returns session_id
# POST /duplex/interrupt/{id} — interrupt current speech immediately
# GET  /duplex/audio/{id}     — PCM audio stream for this session
# GET  /duplex/text/{id}      — SSE text token stream for this session
# DELETE /duplex/session/{id} — clean up session
#
# The frontend holds one persistent audio stream and one SSE text stream per
# session. Queries are submitted separately and interrupt any in-progress
# speech. The audio stream stays open across queries; new PCM chunks arrive
# as each new query is answered.
# ─────────────────────────────────────────────────────────────────────────────

_duplex_sessions: dict[str, dict] = {}


class DuplexQueryRequest(BaseModel):
    session_id: str
    text: Optional[str]       = None   # text query (mutually exclusive with audio_b64)
    audio_b64: Optional[str]  = None   # base64 WAV/WebM for STT
    model: str                = "llama3.2"
    engine: Optional[str]     = None
    voice: Optional[str]      = None
    speed: Optional[float]    = 1.0


def _make_duplex_session(session_id: str) -> dict:
    return {
        "id":          session_id,
        "audio_q":     asyncio.Queue(maxsize=512),   # PCM bytes or sentinel
        "text_q":      asyncio.Queue(maxsize=4096),  # (kind, data) tokens
        "interrupt":   asyncio.Event(),              # set to abort current query
        "lock":        asyncio.Lock(),               # serialise queries
        "active":      True,
    }


@app.post("/duplex/start")
async def duplex_start():
    """Create a new duplex session. Returns session_id."""
    sid = str(uuid.uuid4())
    _duplex_sessions[sid] = _make_duplex_session(sid)
    log.info(f"[duplex] session {sid} created")
    return {"session_id": sid}


@app.post("/duplex/query")
async def duplex_query(req: DuplexQueryRequest):
    """
    Submit a query (text or audio) to the duplex session.
    Interrupts any in-progress response immediately.
    Audio queries are transcribed via Whisper before being sent to Ollama.
    """
    session = _duplex_sessions.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    # Resolve text — transcribe audio if needed
    query_text = req.text
    if not query_text and req.audio_b64:
        if not ENABLE_WHISPER or _whisper_model is None:
            raise HTTPException(status_code=503, detail="Whisper not loaded.")
        audio_bytes = base64.b64decode(req.audio_b64)
        loop = asyncio.get_event_loop()
        def _transcribe():
            audio_np = _bytes_to_float_array(audio_bytes)
            result   = _whisper_model.transcribe(audio_np, fp16=False)
            return result["text"].strip()
        query_text = await loop.run_in_executor(None, _transcribe)
        log.info(f"[duplex] STT result: {query_text!r}")

    if not query_text:
        raise HTTPException(status_code=400, detail="No text or audio provided.")

    # Signal interrupt to stop any ongoing response
    session["interrupt"].set()

    # Small yield to let the ongoing generator notice the interrupt
    await asyncio.sleep(0.05)
    session["interrupt"].clear()

    # Fan transcribed text to SSE text stream as a user turn marker
    await session["text_q"].put(("user", query_text))

    # Fire the response in the background so this request returns immediately
    asyncio.create_task(_duplex_respond(session, query_text, req))

    return {"session_id": req.session_id, "query": query_text, "status": "accepted"}


async def _duplex_respond(session: dict, query_text: str, req: DuplexQueryRequest):
    """Background task: Ollama → TTS → audio_q, with interrupt support."""
    ollama_base = OLLAMA_BASE_URL.rstrip("/")
    tts_opts    = {"engine": req.engine, "voice": req.voice, "speed": req.speed}
    interrupt   = session["interrupt"]
    audio_q     = session["audio_q"]
    text_q      = session["text_q"]
    loop        = asyncio.get_event_loop()

    async with session["lock"]:
        text_buf   = ""
        chunks_out = 0

        # Notify text stream that assistant is starting
        await text_q.put(("assistant_start", None))

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=10.0),
                follow_redirects=True,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{ollama_base}/api/chat",
                    json={
                        "model":    req.model,
                        "messages": [{"role": "user", "content": query_text}],
                        "stream":   True,
                    },
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        log.error(f"[duplex] Ollama error {resp.status_code}: {body.decode()[:300]}")
                        await text_q.put(("error", f"Ollama error {resp.status_code}"))
                        return

                    async for raw_line in resp.aiter_lines():
                        # Check for interrupt on every token
                        if interrupt.is_set():
                            log.info(f"[duplex] interrupted after {chunks_out} chunks")
                            # Drain audio queue to stop playback
                            while not audio_q.empty():
                                try: audio_q.get_nowait()
                                except: pass
                            await audio_q.put(("interrupt", None))
                            await text_q.put(("interrupt", None))
                            return

                        if not raw_line.strip():
                            continue
                        try:
                            obj = json.loads(raw_line)
                        except json.JSONDecodeError:
                            continue

                        token     = obj.get("message", {}).get("content", "")
                        text_buf += token
                        await text_q.put(("token", token))

                        sentences, text_buf = _extract_sentences(text_buf)
                        for sentence in sentences:
                            if interrupt.is_set():
                                break
                            if not sentence:
                                continue
                            try:
                                wav, sr = await loop.run_in_executor(
                                    None, _tts_synthesize, sentence, tts_opts
                                )
                                if not interrupt.is_set():
                                    await audio_q.put(("pcm", _wav_to_pcm_s16le(wav)))
                                    chunks_out += 1
                            except Exception as e:
                                log.error(f"[duplex] TTS error: {e}")

                        if obj.get("done"):
                            break

            # Flush remainder
            remainder = text_buf.strip()
            if remainder and not interrupt.is_set():
                try:
                    wav, sr = await loop.run_in_executor(
                        None, _tts_synthesize, remainder, tts_opts
                    )
                    if not interrupt.is_set():
                        await audio_q.put(("pcm", _wav_to_pcm_s16le(wav)))
                        chunks_out += 1
                except Exception as e:
                    log.error(f"[duplex] flush TTS error: {e}")

        except httpx.ConnectError as e:
            log.error(f"[duplex] cannot connect to Ollama: {e}")
            await text_q.put(("error", str(e)))
        except Exception as e:
            log.error(f"[duplex] error: {e}\n{traceback.format_exc()}")
            await text_q.put(("error", str(e)))
        finally:
            await text_q.put(("assistant_end", None))
            log.info(f"[duplex] response done — {chunks_out} chunks")


@app.post("/duplex/interrupt/{session_id}")
async def duplex_interrupt(session_id: str):
    """Immediately interrupt the current response for this session."""
    session = _duplex_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    session["interrupt"].set()
    # Drain the audio queue so the client stops hearing old audio
    q = session["audio_q"]
    while not q.empty():
        try: q.get_nowait()
        except: pass
    await q.put(("interrupt", None))
    log.info(f"[duplex] manual interrupt for {session_id}")
    return {"status": "interrupted"}


@app.get("/duplex/audio/{session_id}")
async def duplex_audio(session_id: str):
    """
    Persistent PCM audio stream for a duplex session.
    Stays open across multiple queries. Clients should reconnect if it drops.
    Sends raw s16le PCM. Interrupt markers cause a brief silence then resume.
    """
    session = _duplex_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    audio_q = session["audio_q"]
    sr      = _sr_for_engine(None)

    async def stream():
        while session.get("active"):
            try:
                kind, data = await asyncio.wait_for(audio_q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send keepalive silence (50ms) so the connection stays open
                silence = bytes(int(sr * 0.05) * 2)
                yield silence
                continue

            if kind == "pcm":
                yield data
            elif kind == "interrupt":
                # Short silence on interrupt to cleanly stop current audio
                yield bytes(int(sr * 0.1) * 2)
            # Other sentinels (session end etc.) — just continue

    return StreamingResponse(
        stream(),
        media_type="audio/pcm",
        headers={
            "X-Sample-Rate":     str(sr),
            "X-Channels":        "1",
            "X-Bit-Depth":       "16",
            "Transfer-Encoding": "chunked",
        },
    )


@app.get("/duplex/text/{session_id}")
async def duplex_text(session_id: str):
    """
    Persistent SSE text stream for a duplex session.
    Events: user, assistant_start, token (b64), assistant_end, interrupt, error.
    """
    session = _duplex_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    text_q = session["text_q"]

    async def event_stream():
        while session.get("active"):
            try:
                kind, data = await asyncio.wait_for(text_q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue

            if kind == "token":
                b64 = base64.b64encode((data or "").encode()).decode()
                yield f"event: token\ndata: {b64}\n\n"
            elif kind == "user":
                b64 = base64.b64encode((data or "").encode()).decode()
                yield f"event: user\ndata: {b64}\n\n"
            elif kind == "assistant_start":
                yield f"event: assistant_start\ndata: 1\n\n"
            elif kind == "assistant_end":
                yield f"event: assistant_end\ndata: 1\n\n"
            elif kind == "interrupt":
                yield f"event: interrupt\ndata: 1\n\n"
            elif kind == "error":
                b64 = base64.b64encode((data or "").encode()).decode()
                yield f"event: error\ndata: {b64}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.delete("/duplex/session/{session_id}")
async def duplex_end(session_id: str):
    """Clean up a duplex session."""
    session = _duplex_sessions.pop(session_id, None)
    if session:
        session["active"] = False
        session["interrupt"].set()
    return {"status": "closed"}


# ─────────────────────────── ENTRYPOINT ──────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "GPU_inference:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        workers=1,
        log_level="info",
    )