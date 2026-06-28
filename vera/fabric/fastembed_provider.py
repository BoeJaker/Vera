"""
fastembed_provider.py  —  optional fastembed (ONNX Runtime) embedding backend
==============================================================================
§1 of ONNX_TODO.md. Serves `nomic-embed-text` (and other supported models) on
ONNX Runtime CPU via the `fastembed` library — faster batched throughput and
lower memory than routing every embed through Ollama, and it removes Ollama from
the embedding hot path the data fabric / memory system hammer.

**Opt-in and fully back-compat.** It is only consulted when
`cfg.EMBED_PROVIDER == "fastembed"`; `ollama_embed()` falls through to the Ollama
path on any failure here. `fastembed` is an *optional* dependency — if it isn't
installed, `available()` is False and nothing changes.

⚠️  Vector-space caveat: fastembed's `nomic-ai/nomic-embed-text-v1.5` is 768-dim
like Ollama's `nomic-embed-text`, but the float values differ. Switching backends
on an already-populated vector store requires a re-index. That's why this is
off by default.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import List, Optional

log = logging.getLogger("vera.fastembed")

try:
    from fastembed import TextEmbedding
    HAS_FASTEMBED = True
except Exception as e:  # pragma: no cover - optional dep
    TextEmbedding = None
    HAS_FASTEMBED = False
    log.info("fastembed not installed (EMBED_PROVIDER=fastembed will fall back to Ollama): %s", e)

_MODEL_NAME = os.getenv("VERA_FASTEMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")
_MODEL = None
_LOCK = threading.Lock()


def available() -> bool:
    return HAS_FASTEMBED


def model_name() -> str:
    return _MODEL_NAME


def _get_model():
    """Lazily construct the embedding model (downloads weights on first use)."""
    global _MODEL
    if _MODEL is None:
        if not HAS_FASTEMBED:
            raise RuntimeError("fastembed not installed")
        with _LOCK:
            if _MODEL is None:
                log.info("fastembed: loading %s (ONNX Runtime CPU)…", _MODEL_NAME)
                _MODEL = TextEmbedding(model_name=_MODEL_NAME)
    return _MODEL


def embed(text: str) -> Optional[List[float]]:
    """Embed a single string. Synchronous + CPU-bound — call via a thread
    executor from async code. Returns None on any failure (caller falls back)."""
    if not HAS_FASTEMBED or not text:
        return None
    try:
        vecs = list(_get_model().embed([text]))
        if not vecs:
            return None
        return vecs[0].tolist()
    except Exception as e:
        log.warning("fastembed embed failed: %s", e)
        return None


def embed_batch(texts: List[str]) -> Optional[List[List[float]]]:
    """Embed many strings at once (fastembed's strength). None on failure."""
    if not HAS_FASTEMBED or not texts:
        return None
    try:
        return [v.tolist() for v in _get_model().embed(texts)]
    except Exception as e:
        log.warning("fastembed embed_batch failed: %s", e)
        return None
