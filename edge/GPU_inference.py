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
from contextlib import asynccontextmanager
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
_sd_img2img_pipe     = None   # lazily built from _sd_pipe.components (shares weights)
_sd_cpu_pipe         = None   # lazily built CPU fallback (fp32) for GPU-OOM retries
_sd_cpu_img2img_pipe = None
_sd_device           = "cpu"  # device SD actually loaded on (set in _load_sd)
_tts_synthesizer     = None   # Coqui TTS instance
_kokoro_pipeline = None   # kokoro-onnx KPipeline instance
_redis_client    = None

_sd_loaded_loras: dict[str, float] = {}  # name → current weight (0 = unloaded)
_stt_queue  = queue.Queue(maxsize=64)
_sd_queue   = queue.Queue(maxsize=64)
_tts_queue  = queue.Queue(maxsize=64)

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
        # xformers is not available on V100 (pre-Ampere) — skip silently
        try:
            _sd_pipe.enable_xformers_memory_efficient_attention()
            log.info("xformers memory efficient attention enabled.")
        except Exception:
            log.info("xformers not available (expected on V100/older GPUs), using default attention.")

        # Attention slicing reduces peak VRAM usage with minimal speed cost
        _sd_pipe.enable_attention_slicing(slice_size="auto")

        # VAE tiling helps with larger images on limited VRAM (V100 here is 12GB)
        try:
            _sd_pipe.enable_vae_tiling()
            log.info("VAE tiling enabled.")
        except Exception:
            pass

        # V100 does not support TF32 — ensure correct precision
        torch.backends.cuda.matmul.allow_tf32 = False

    _sd_device = used
    log.info(f"Stable Diffusion ready ({'SDXL' if is_xl else 'SD1.x/2.x'}, {dtype}, {used}).")

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


def _decode_image_b64(b64: str):
    """Decode a base64 PNG/JPEG into an RGB PIL image."""
    from PIL import Image
    raw = base64.b64decode(b64)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def _chroma_key(image, hex_color: str = "#1bd12a", tol: int = 80):
    """Make pixels near `hex_color` transparent. Returns an RGBA PIL image.

    A simple colour-distance key — works well when the prompt produced a solid
    flat background of the key colour (we ask SD for exactly that). `tol` is the
    RGB euclidean-distance cutoff; larger removes more (incl. anti-aliased edge)."""
    from PIL import Image
    h = (hex_color or "#1bd12a").lstrip("#")
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except Exception:
        r, g, b = 0x1b, 0xd1, 0x2a
    img = image.convert("RGBA")
    arr = np.array(img).astype(np.int16)
    dist = np.sqrt((arr[..., 0] - r) ** 2 + (arr[..., 1] - g) ** 2 + (arr[..., 2] - b) ** 2)
    arr[..., 3] = np.where(dist < tol, 0, arr[..., 3])
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


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
    gen = None
    if payload.get("seed") is not None:
        gen = torch.Generator(device=device).manual_seed(int(payload["seed"]))
    common = dict(
        prompt=payload["prompt"],
        negative_prompt=payload.get("negative_prompt", ""),
        num_inference_steps=int(payload.get("steps",   SD_DEFAULT_STEPS)),
        guidance_scale=float(payload.get("guidance",   SD_DEFAULT_GUIDANCE)),
        generator=gen,
    )
    w = int(payload.get("width",  SD_DEFAULT_WIDTH))
    h = int(payload.get("height", SD_DEFAULT_HEIGHT))
    with torch.inference_mode():
        if init_b64:
            pipe = _get_img2img_pipe() if device == _sd_device else _get_cpu_img2img_pipe()
            init_image = _decode_image_b64(init_b64).resize((w, h))
            return pipe(image=init_image,
                        strength=float(payload.get("strength", 0.55)),
                        **common).images[0]
        pipe = _sd_pipe if device == _sd_device else _get_cpu_pipe()
        return pipe(width=w, height=h, **common).images[0]


def _is_cuda_oom(err: Exception) -> bool:
    oom_cls = getattr(torch.cuda, "OutOfMemoryError", ())
    return isinstance(err, oom_cls) or "out of memory" in str(err).lower()


def _sd_worker():
    log.info("SD worker started.")
    while True:
        job_id, payload, result_q = _sd_queue.get()
        try:
            # Apply LoRAs if requested (GPU pipe; shared by txt2img and img2img)
            raw_loras = payload.get("loras") or []
            lora_entries = [LoRAEntry(**l) if isinstance(l, dict) else l for l in raw_loras]
            _apply_loras(lora_entries)

            used = _sd_device
            try:
                image = _run_sd_generation(payload, _sd_device)
            except Exception as gen_err:
                # GPU out of memory → free VRAM and retry the SAME job on CPU so
                # the request still succeeds (just slower). Callers learn which
                # device served it from the "device" field below.
                if _sd_device == "cuda" and _is_cuda_oom(gen_err):
                    log.warning(f"SD CUDA OOM; retrying this job on CPU. ({gen_err})")
                    try: torch.cuda.empty_cache()
                    except Exception: pass
                    image = _run_sd_generation(payload, "cpu")
                    used = "cpu"
                else:
                    raise

            # Optional transparency: chroma-key the (solid-colour) background out.
            if payload.get("transparent"):
                try:
                    image = _chroma_key(image, payload.get("bg_color") or "#1bd12a")
                except Exception as e:
                    log.warning(f"chroma-key failed: {e}")

            buf = io.BytesIO()
            image.save(buf, format="PNG")
            out = {"status": "ok", "device": used,
                   "image_b64": base64.b64encode(buf.getvalue()).decode(), "format": "png"}
        except Exception as e:
            log.error(f"SD error: {e}\n{traceback.format_exc()}")
            out = {"status": "error", "error": str(e)}
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
        "controlnet":   False,
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