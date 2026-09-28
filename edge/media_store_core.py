"""media_store_core.py - where the media server finds its models.

Every node mounts the shared specialist-model store READ-ONLY at
/opt/vera-store/models (VERA_MODEL_STORE). A model the store holds is loaded
from there, so every node serves the same bytes and nothing is downloaded per
node. A model the store lacks falls back to what the server always did (a
Hugging Face id / its own cache) - on a read-only store the fallback must never
be "download into the store", so resolution only ever answers "this complete
local copy" or "your old identifier".

Store layout (written by the builder CT and by the one-off cache import):
    sd/<owner>__<name>/            diffusers snapshot dirs (SD, ControlNet, IP-Adapter)
    whisper/<name>.pt              openai-whisper checkpoints
    tts/kokoro-v1.0/               kokoro-v1.0.onnx + voices-v1.0.bin
    tts/coqui/tts/<tts_models--..> Coqui's own layout (TTS_HOME=tts/coqui)
    rembg/<model>.onnx             rembg sessions (U2NET_HOME=rembg)

Pure: paths in, paths out. Shipped beside GPU_inference.py; tested in
tests/test_media_store_core.py.
"""
import os
from typing import Dict, Optional

DEFAULT_STORE = "/opt/vera-store/models"

KOKORO_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")


def store_root(env: Optional[Dict[str, str]] = None) -> str:
    e = os.environ if env is None else env
    return e.get("VERA_MODEL_STORE", DEFAULT_STORE) or DEFAULT_STORE


def slug(model_id: str) -> str:
    return str(model_id).replace("/", "__")


def _complete(path: str) -> bool:
    """A diffusers/transformers directory is usable when it has its index or
    config. A copy still being written (<dir>.part) never matches."""
    return any(os.path.isfile(os.path.join(path, f))
               for f in ("model_index.json", "config.json")) or \
        os.path.isdir(os.path.join(path, "models"))          # IP-Adapter's layout


def hf_source(model_id: str, family: str = "sd", root: str = "") -> str:
    """The store copy of a Hugging Face repo if complete, else the repo id.
    An absolute path passed in is taken as-is (an operator's explicit choice)."""
    if not model_id or os.path.isabs(model_id):
        return model_id
    p = os.path.join(root or store_root(), family, slug(model_id))
    return p if os.path.isdir(p) and _complete(p) else model_id


def whisper_root(name: str, root: str = "") -> Optional[str]:
    """download_root for whisper.load_model when the store has <name>.pt."""
    d = os.path.join(root or store_root(), "whisper")
    return d if os.path.isfile(os.path.join(d, f"{name}.pt")) else None


def kokoro_dir(root: str = "") -> Optional[str]:
    d = os.path.join(root or store_root(), "tts", "kokoro-v1.0")
    return d if all(os.path.isfile(os.path.join(d, f)) for f in KOKORO_FILES) else None


def coqui_home(model_name: str, root: str = "") -> Optional[str]:
    """TTS_HOME for Coqui when the store holds this model in Coqui's layout
    (tts_models/en/ljspeech/tacotron2-DDC -> tts/tts_models--en--ljspeech--tacotron2-DDC)."""
    home = os.path.join(root or store_root(), "tts", "coqui")
    d = os.path.join(home, "tts", str(model_name).replace("/", "--"))
    return home if os.path.isdir(d) else None


def rembg_home(model: str, root: str = "") -> Optional[str]:
    d = os.path.join(root or store_root(), "rembg")
    return d if os.path.isfile(os.path.join(d, f"{model}.onnx")) else None


def resolve_all(cfg: Dict[str, str], root: str = "") -> Dict[str, object]:
    """Where each model the server may load comes from: {what: path | None}.
    None means "not in the store - the server's own cache / hub id"."""
    r = root or store_root()
    sd = hf_source(cfg.get("sd", ""), "sd", r)
    cn = hf_source(cfg.get("controlnet", ""), "sd", r)
    ip = hf_source(cfg.get("ipadapter", ""), "sd", r)
    return {
        "store": r if os.path.isdir(r) else None,
        "sd": sd if sd != cfg.get("sd", "") else None,
        "controlnet": cn if cn != cfg.get("controlnet", "") else None,
        "ipadapter": ip if ip != cfg.get("ipadapter", "") else None,
        "whisper": whisper_root(cfg.get("whisper", ""), r),
        "kokoro": kokoro_dir(r),
        "coqui": coqui_home(cfg.get("coqui", ""), r),
        "rembg": rembg_home(cfg.get("rembg", ""), r),
    }
