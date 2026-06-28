"""
embed_provider_capabilities.py  —  embedding backend introspection + migration guard
=====================================================================================
§1 of ONNX_TODO.md. Embeddings can be served by Ollama (default) or by the
opt-in fastembed (ONNX Runtime CPU) backend — switched with
`cfg.EMBED_PROVIDER` / `VERA_EMBED_PROVIDER`. Switching changes the **vector
space**: the two backends produce different vectors for the same text, so an
already-indexed store must be re-embedded before flipping the default.

These caps help make that decision safely — read-only, additive, no behaviour
change to the embedding path itself.

  • embed.provider.info   — active backend + fastembed availability
  • embed.provider.check  — compare Ollama vs fastembed embeddings of one text
                            (dimension match, cosine, re-index guidance)
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("vera.embed_provider")

try:
    import Vera.vera.capability_orchestration as _O
    from Vera.vera.capability_orchestration import capability
    _CAP_AVAILABLE = True
except ImportError:
    _CAP_AVAILABLE = False


def _fastembed():
    try:
        import Vera.vera.fabric.fastembed_provider as fe
        return fe
    except Exception:
        return None


def _cosine(a, b) -> Optional[float]:
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = (sum(x * x for x in a)) ** 0.5
    nb = (sum(y * y for y in b)) ** 0.5
    if not na or not nb:
        return None
    return dot / (na * nb)


if _CAP_AVAILABLE:

    @capability(
        "embed.provider.info",
        http_method="GET", http_path="/embed/provider/info", http_tags=["embed"],
        memory="off", silent=True,
        description="Report the active embedding backend + fastembed availability. "
                    "Output: {provider, ollama_model, fastembed_available, fastembed_model}.",
    )
    async def cap_embed_provider_info(trace_id=None):
        fe = _fastembed()
        return {
            "provider":            getattr(_O, "EMBED_PROVIDER", "ollama"),
            "ollama_model":        getattr(_O, "OLLAMA_EMBED_MODEL", ""),
            "ollama_url":          getattr(_O, "OLLAMA_EMBED_URL", ""),
            "fastembed_available": bool(fe and fe.available()),
            "fastembed_model":     fe.model_name() if (fe and fe.available()) else None,
        }

    @capability(
        "embed.provider.check",
        http_method="POST", http_path="/embed/provider/check", http_tags=["embed"],
        memory="on",
        description=(
            "Migration guard: compare the current (Ollama) embedding against the "
            "fastembed (ONNX Runtime) candidate before switching EMBED_PROVIDER. "
            "Input: text (str). Output: {ollama_dim, fastembed_dim, dims_match, "
            "cosine_same_text, reindex_required, recommendation}."
        ),
    )
    async def cap_embed_provider_check(
        text: str = "The quick brown fox jumps over the lazy dog.", trace_id=None
    ):
        fe = _fastembed()
        if not (fe and fe.available()):
            return {"error": "fastembed not installed", "hint": "pip install fastembed",
                    "fastembed_available": False}

        # Force each backend explicitly via the per-call provider override so the
        # comparison is valid regardless of the current global default.
        try:
            ovec = await _O.ollama_embed(text, provider="ollama")
        except Exception as e:
            ovec = None
            log.debug("embed.provider.check: ollama embed failed: %s", e)
        try:
            fvec = await _O.ollama_embed(text, provider="fastembed")
        except Exception as e:
            return {"error": f"fastembed embed failed: {e}"}

        odim = len(ovec) if ovec else None
        fdim = len(fvec) if fvec else None
        dims_match = (odim is not None and odim == fdim)
        sim = _cosine(ovec, fvec) if dims_match else None

        if odim is None:
            compatible = None
            rec = ("Could not reach the Ollama embedder to compare. fastembed produces "
                   f"{fdim}-dim vectors — confirm this matches your indexed store before switching.")
        elif not dims_match:
            compatible = False
            rec = (f"INCOMPATIBLE: Ollama={odim}d vs fastembed={fdim}d. Switching requires a "
                   "FULL RE-INDEX of every stored vector.")
        else:
            compatible = True
            rec = (f"Same dimensionality ({odim}d) but a DIFFERENT vector space "
                   f"(cosine≈{sim:.3f} between the two embeddings of the same text). Existing "
                   "Ollama vectors are NOT comparable to fastembed ones — re-index before flipping "
                   "the default, or only switch on an empty/fresh store.")

        return {"ok": True, "text": text,
                "ollama_model":        getattr(_O, "OLLAMA_EMBED_MODEL", ""),
                "ollama_dim":          odim,
                "fastembed_model":     fe.model_name(),
                "fastembed_dim":       fdim,
                "dims_match":          dims_match,
                "cosine_same_text":    sim,
                "dimension_compatible": compatible,
                "reindex_required":    True,
                "recommendation":      rec}

    @capability(
        "embed.provider.benchmark",
        http_method="POST", http_path="/embed/provider/benchmark", http_tags=["embed"],
        memory="on",
        description=("Benchmark embedding throughput: Ollama vs fastembed (ONNX "
                     "Runtime CPU). Input: n (int), text (str). Output: "
                     "{results:{ollama, fastembed:{per_embed_ms, embeds_per_s, dim}}}."),
    )
    async def cap_embed_provider_benchmark(
        n: int = 20, text: str = "The quick brown fox jumps over the lazy dog.",
        trace_id=None,
    ):
        import time
        n = max(1, min(int(n), 500))
        fe = _fastembed()
        out = {}
        for prov in ("ollama", "fastembed"):
            if prov == "fastembed" and not (fe and fe.available()):
                out[prov] = {"available": False}
                continue
            try:                       # warmup (fastembed loads its model lazily)
                await _O.ollama_embed(text, provider=prov)
            except Exception:
                pass
            t0 = time.perf_counter()
            ok = 0
            dim = None
            for _ in range(n):
                try:
                    v = await _O.ollama_embed(text, provider=prov)
                except Exception:
                    v = None
                if v:
                    ok += 1
                    dim = len(v)
            dt = time.perf_counter() - t0
            out[prov] = {
                "available": True, "n": n, "ok": ok, "dim": dim,
                "total_s": round(dt, 3),
                "per_embed_ms": round(dt / n * 1000, 2) if n else None,
                "embeds_per_s": round(ok / dt, 1) if dt > 0 else None,
            }
        return {"ok": True, "text_len": len(text), "results": out}

    log.info("embed_provider_capabilities ready")
