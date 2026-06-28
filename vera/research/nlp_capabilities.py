"""
nlp_capabilities.py  —  ONNX-Runtime NLP utilities   (§5 of ONNX_TODO.md)
==========================================================================
Small NLP models — cross-encoder rerankers (and, later, classifiers) — served on
ONNX Runtime CPU via `fastembed`. No heavyweight serving process: ORT loads a
quantized cross-encoder and scores (query, document) pairs on CPU, keeping the
GPU free for the LLM cluster.

**Purely additive & optional.** `fastembed` is an optional dependency; if it
isn't installed every cap returns a friendly `{"error": ...}` and the rest of
Vera is unaffected. Nothing in the existing research/retrieval path is modified —
`nlp.rerank` is exposed for callers (and future retrieval wiring) to opt into.

Capabilities
────────────
  • nlp.rerank   — re-rank documents against a query with an ONNX cross-encoder
  • nlp.models   — report reranker availability + model name + providers
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from typing import Any, List, Optional

log = logging.getLogger("vera.nlp")

# ── Optional fastembed cross-encoder ──────────────────────────────────────────
try:
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    HAS_RERANK = True
except Exception as e:  # pragma: no cover - optional dep
    TextCrossEncoder = None
    HAS_RERANK = False
    log.info("fastembed cross-encoder unavailable (nlp.rerank disabled): %s", e)

# ── Optional ONNX text classification / NER (optimum + transformers) ──────────
try:
    from optimum.onnxruntime import (
        ORTModelForSequenceClassification, ORTModelForTokenClassification,
    )
    from transformers import AutoTokenizer
    from transformers import pipeline as _hf_pipeline
    HAS_ORT_CLASSIFY = True
except Exception as e:  # pragma: no cover - optional dep
    ORTModelForSequenceClassification = ORTModelForTokenClassification = None
    AutoTokenizer = _hf_pipeline = None
    HAS_ORT_CLASSIFY = False
    log.info("optimum/transformers unavailable (nlp.classify/nlp.ner disabled): %s", e)

# ── Capability framework (optional, mirrors sibling modules) ──────────────────
try:
    from Vera.vera.capability_orchestration import capability, emit_event, now_iso
    _CAP_AVAILABLE = True
except ImportError:
    _CAP_AVAILABLE = False

RERANK_MODEL    = os.getenv("VERA_RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2")
SENTIMENT_MODEL = os.getenv("VERA_SENTIMENT_MODEL",
                            "distilbert-base-uncased-finetuned-sst-2-english")
NER_MODEL       = os.getenv("VERA_NER_MODEL", "dslim/bert-base-NER")

_ENCODER = None
_SENTIMENT_PIPE = None
_NER_PIPE = None
_LOCK = threading.Lock()


def _get_sentiment_pipe():
    global _SENTIMENT_PIPE
    if _SENTIMENT_PIPE is None:
        if not HAS_ORT_CLASSIFY:
            raise RuntimeError("optimum/transformers not installed")
        with _LOCK:
            if _SENTIMENT_PIPE is None:
                log.info("nlp.classify: loading %s (ONNX Runtime)…", SENTIMENT_MODEL)
                m = ORTModelForSequenceClassification.from_pretrained(SENTIMENT_MODEL, export=True)
                t = AutoTokenizer.from_pretrained(SENTIMENT_MODEL)
                _SENTIMENT_PIPE = _hf_pipeline("text-classification", model=m,
                                               tokenizer=t, top_k=None)
    return _SENTIMENT_PIPE


def _get_ner_pipe():
    global _NER_PIPE
    if _NER_PIPE is None:
        if not HAS_ORT_CLASSIFY:
            raise RuntimeError("optimum/transformers not installed")
        with _LOCK:
            if _NER_PIPE is None:
                log.info("nlp.ner: loading %s (ONNX Runtime)…", NER_MODEL)
                m = ORTModelForTokenClassification.from_pretrained(NER_MODEL, export=True)
                t = AutoTokenizer.from_pretrained(NER_MODEL)
                _NER_PIPE = _hf_pipeline("token-classification", model=m, tokenizer=t,
                                         aggregation_strategy="simple")
    return _NER_PIPE


def _get_encoder():
    global _ENCODER
    if _ENCODER is None:
        if not HAS_RERANK:
            raise RuntimeError("fastembed cross-encoder not installed")
        with _LOCK:
            if _ENCODER is None:
                log.info("nlp.rerank: loading %s (ONNX Runtime CPU)…", RERANK_MODEL)
                _ENCODER = TextCrossEncoder(model_name=RERANK_MODEL)
    return _ENCODER


def _coerce_docs(documents: Any) -> List[str]:
    """Accept a JSON array string, a real list, or a newline-delimited string."""
    if documents is None:
        return []
    if isinstance(documents, list):
        return [str(d) for d in documents]
    if isinstance(documents, str):
        s = documents.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(d) for d in parsed]
        except Exception:
            pass
        return [line for line in s.splitlines() if line.strip()]
    return [str(documents)]


def _rerank_sync(query: str, docs: List[str]) -> List[float]:
    return [float(s) for s in _get_encoder().rerank(query, docs)]


if _CAP_AVAILABLE:

    @capability(
        "nlp.rerank",
        http_method="POST", http_path="/nlp/rerank", http_tags=["nlp", "onnx"],
        memory="on",
        description=(
            "Re-rank documents against a query with an ONNX cross-encoder (ORT "
            "CPU). Input: query (str!), documents (JSON array of strings or "
            "newline-delimited), top_k (int, 0=all). Output: {ranked:[{index, "
            "score, text}]}."
        ),
    )
    async def cap_nlp_rerank(query: str = "", documents: Any = None,
                             top_k: int = 0, trace_id=None):
        if not query:
            return {"error": "query is required"}
        if not HAS_RERANK:
            return {"error": "fastembed cross-encoder not installed",
                    "hint": "pip install fastembed"}
        docs = _coerce_docs(documents)
        if not docs:
            return {"error": "documents is required (JSON array of strings)"}
        try:
            scores = await asyncio.get_event_loop().run_in_executor(
                None, _rerank_sync, query, docs)
        except Exception as e:
            log.error("nlp.rerank failed: %s", e)
            return {"error": f"rerank failed: {e}"}

        ranked = sorted(
            ({"index": i, "score": s, "text": docs[i]} for i, s in enumerate(scores)),
            key=lambda r: r["score"], reverse=True,
        )
        if top_k and top_k > 0:
            ranked = ranked[:top_k]
        return {"ok": True, "query": query, "model": RERANK_MODEL,
                "count": len(docs), "ranked": ranked}

    @capability(
        "nlp.classify",
        http_method="POST", http_path="/nlp/classify", http_tags=["nlp", "onnx"],
        memory="on",
        description=("Sequence / sentiment classification with an ONNX model (ORT "
                     "CPU). Input: text (str!). Output: {top, labels:[{label, score}]}."),
    )
    async def cap_nlp_classify(text: str = "", trace_id=None):
        if not text:
            return {"error": "text is required"}
        if not HAS_ORT_CLASSIFY:
            return {"error": "optimum/transformers not installed",
                    "hint": "pip install optimum[onnxruntime] transformers"}
        try:
            res = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _get_sentiment_pipe()(text[:512]))
        except Exception as e:
            log.error("nlp.classify failed: %s", e)
            return {"error": f"classify failed: {e}"}
        flat = res[0] if (res and isinstance(res[0], list)) else res
        labels = [{"label": x.get("label"), "score": float(x.get("score", 0.0))}
                  for x in flat]
        labels.sort(key=lambda x: x["score"], reverse=True)
        return {"ok": True, "text": text, "model": SENTIMENT_MODEL,
                "top": labels[0]["label"] if labels else None, "labels": labels}

    @capability(
        "nlp.ner",
        http_method="POST", http_path="/nlp/ner", http_tags=["nlp", "onnx"],
        memory="on",
        description=("Named-entity recognition with an ONNX token-classifier (ORT "
                     "CPU). Input: text (str!). Output: {entities:[{entity, word, "
                     "score, start, end}]}."),
    )
    async def cap_nlp_ner(text: str = "", trace_id=None):
        if not text:
            return {"error": "text is required"}
        if not HAS_ORT_CLASSIFY:
            return {"error": "optimum/transformers not installed",
                    "hint": "pip install optimum[onnxruntime] transformers"}
        try:
            res = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _get_ner_pipe()(text[:1024]))
        except Exception as e:
            log.error("nlp.ner failed: %s", e)
            return {"error": f"ner failed: {e}"}
        entities = [{
            "entity": e.get("entity_group") or e.get("entity"),
            "word":   e.get("word"),
            "score":  float(e.get("score", 0.0)),
            "start":  int(e["start"]) if e.get("start") is not None else None,
            "end":    int(e["end"]) if e.get("end") is not None else None,
        } for e in res]
        return {"ok": True, "text": text, "model": NER_MODEL,
                "entities": entities, "count": len(entities)}

    @capability(
        "nlp.models",
        http_method="GET", http_path="/nlp/models", http_tags=["nlp", "onnx"],
        memory="off", silent=True,
        description="Report NLP model availability. Output: {rerank, classify, ner, providers}.",
    )
    async def cap_nlp_models(trace_id=None):
        providers = []
        try:
            import onnxruntime as _ort
            providers = list(_ort.get_available_providers())
        except Exception:
            pass
        return {"rerank_available":   HAS_RERANK,   "rerank_model":   RERANK_MODEL,
                "classify_available": HAS_ORT_CLASSIFY, "classify_model": SENTIMENT_MODEL,
                "ner_available":      HAS_ORT_CLASSIFY, "ner_model":      NER_MODEL,
                "providers": providers}

    log.info("nlp_capabilities ready — rerank=%s classify/ner=%s",
             HAS_RERANK, HAS_ORT_CLASSIFY)
