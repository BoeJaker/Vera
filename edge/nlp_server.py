"""
nlp_server.py  —  text-level NLP on a compute node, so the Vera host never runs it
==================================================================================

Deployed to an ollama node via `nodes.provision` (component `nlp_server`). Vera
calls it over HTTP from `vera/research/nlp_capabilities.py`; the Vera host keeps
NO ML runtime of its own.

    python nlp_server.py serve --host 0.0.0.0 --port 8771

WHY A TEXT ENDPOINT AND NOT `onnx_runtime.py`
─────────────────────────────────────────────
`edge/onnx_runtime.py` already serves ONNX on nodes, but its `POST /run/{slug}`
is TENSOR-level: it takes an input tensor and returns output tensors. NER needs
tokenisation in front of the forward pass and label decoding (plus entity
aggregation) behind it, and that component's dependency list carries no
tokenizer.

Two ways to close that gap. Shipping token IDs from the host keeps the ONNX
server generic, but it still requires a tokenizer ON the Vera host and still
spends that 2-core VM's CPU on tokenising and decoding every call. Putting the
whole text→entities path on the node instead leaves the host with zero ML
dependencies, which is the actual goal here. That is what this file does; it is
a sibling of onnx_runtime.py, not a replacement for it.

⚠ PORT. The node agent (`edge/vera_node_agent.py`) already owns 8770 on every
node, and the `onnx_runtime` component declares 8770 too — a pre-existing
collision. This server defaults to 8771. Do not move it back.

⚠ THREADS. ONNX Runtime will otherwise take every core it can see. On gpu-250
that starves the ollama process sharing the container, which would defeat the
premise of putting NLP there (GPU busy, CPU free). `VERA_NLP_THREADS` caps it,
and the cap is applied to OpenMP BEFORE onnxruntime is imported, because the
OMP thread pool is sized at load time and a SessionOptions set afterwards
cannot shrink it.
"""

# ⚠ NO `from __future__ import annotations` in this file, deliberately.
# It stringifies annotations, and FastAPI resolves a stringified annotation
# against the MODULE globals — so a pydantic model declared inside build_app()
# becomes unresolvable and FastAPI silently falls back to treating the body
# parameter as a QUERY parameter. Every POST then 422s with
# {"loc": ["query", "req"], "msg": "Field required"} no matter what body is
# sent. Verified against fastapi 0.116.1 / pydantic 2.11.7.

import argparse
import os

# ── Thread cap — MUST be set before onnxruntime/torch import anything ────────
# Sized for a 12-core LXC container shared with an ollama runner. Four leaves
# the node able to keep inferring while NLP runs.
NLP_THREADS = int(os.getenv("VERA_NLP_THREADS", "4") or 4)
for _var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    # A CAP, not a default: setdefault() would let an environment that already
    # exported a larger value (or one inherited from a parent process) blow
    # straight through the limit this server exists to enforce. A smaller
    # existing value is a deliberate tightening and is left alone.
    try:
        _existing = int(os.environ.get(_var, "") or 0)
    except ValueError:
        _existing = 0
    os.environ[_var] = str(min(_existing, NLP_THREADS) if _existing > 0
                           else NLP_THREADS)

import json          # noqa: E402  (after the thread cap, deliberately)
import logging       # noqa: E402
import threading     # noqa: E402
import time          # noqa: E402
from typing import Any, Dict, List  # noqa: E402

try:
    # Shipped alongside this file by the `nlp_server` component so the host and
    # the node share ONE implementation of chunking and offset merging.
    from nlp_dispatch_core import chunk_text, merge_chunk_entities
    HAS_CORE = True
except Exception:  # pragma: no cover - deployment without the core file
    HAS_CORE = False

    def chunk_text(text, max_chars=1000, overlap=0):
        return [(0, text[:max_chars])]

    def merge_chunk_entities(pieces):
        return [e for _o, ents in pieces for e in (ents or [])]

log = logging.getLogger("vera.edge.nlp")

NER_MODEL = os.getenv("VERA_NER_MODEL",
                      "djagatiya/ner-roberta-base-ontonotesv5-englishv4")
SENTIMENT_MODEL = os.getenv("VERA_SENTIMENT_MODEL",
                            "distilbert-base-uncased-finetuned-sst-2-english")
RERANK_MODEL = os.getenv("VERA_RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2")

#: Per-chunk character budget. The old in-process cap was a hard `text[:1024]`
#: truncation; this is a chunk size, not a limit on the document.
CHUNK_CHARS = int(os.getenv("VERA_NLP_CHUNK_CHARS", "1000") or 1000)
#: Re-read this much of the previous chunk so an entity on a boundary is seen
#: whole by at least one chunk.
CHUNK_OVERLAP = int(os.getenv("VERA_NLP_CHUNK_OVERLAP", "100") or 100)

_NER_PIPE = None
_SENTIMENT_PIPE = None
_ENCODER = None
_LOCK = threading.Lock()


def _session_options():
    """ONNX Runtime session options with the thread cap applied.

    Belt and braces with the OpenMP env vars above: the env vars bound the
    OpenMP pool at import time, this bounds ORT's own intra-op pool.
    """
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = NLP_THREADS
    so.inter_op_num_threads = 1
    return so


def _get_ner_pipe():
    global _NER_PIPE
    if _NER_PIPE is None:
        with _LOCK:
            if _NER_PIPE is None:
                from optimum.onnxruntime import ORTModelForTokenClassification
                from transformers import AutoTokenizer, pipeline
                log.info("nlp_server: loading NER %s (threads=%d)",
                         NER_MODEL, NLP_THREADS)
                m = ORTModelForTokenClassification.from_pretrained(
                    NER_MODEL, export=True, session_options=_session_options())
                t = AutoTokenizer.from_pretrained(NER_MODEL)
                _NER_PIPE = pipeline("token-classification", model=m,
                                     tokenizer=t, aggregation_strategy="simple")
    return _NER_PIPE


def _get_sentiment_pipe():
    global _SENTIMENT_PIPE
    if _SENTIMENT_PIPE is None:
        with _LOCK:
            if _SENTIMENT_PIPE is None:
                from optimum.onnxruntime import ORTModelForSequenceClassification
                from transformers import AutoTokenizer, pipeline
                log.info("nlp_server: loading classifier %s", SENTIMENT_MODEL)
                m = ORTModelForSequenceClassification.from_pretrained(
                    SENTIMENT_MODEL, export=True,
                    session_options=_session_options())
                t = AutoTokenizer.from_pretrained(SENTIMENT_MODEL)
                _SENTIMENT_PIPE = pipeline("text-classification", model=m,
                                           tokenizer=t, top_k=None)
    return _SENTIMENT_PIPE


def _get_encoder():
    global _ENCODER
    if _ENCODER is None:
        with _LOCK:
            if _ENCODER is None:
                from fastembed.rerank.cross_encoder import TextCrossEncoder
                log.info("nlp_server: loading reranker %s", RERANK_MODEL)
                _ENCODER = TextCrossEncoder(model_name=RERANK_MODEL)
    return _ENCODER


def _providers() -> List[str]:
    try:
        import onnxruntime as ort
        return list(ort.get_available_providers())
    except Exception:
        return []


def run_ner(text: str, max_chars: int = 0, overlap: int = -1) -> Dict[str, Any]:
    """Entities over the WHOLE text, not its first paragraph."""
    max_chars = int(max_chars or CHUNK_CHARS)
    overlap = CHUNK_OVERLAP if overlap is None or overlap < 0 else int(overlap)
    # A caller-supplied max_chars smaller than the configured overlap would make
    # chunk_text raise, turning a tuning choice into a 500. Clamp instead.
    if overlap >= max_chars:
        overlap = max(0, max_chars // 10)
    pipe = _get_ner_pipe()
    chunks = chunk_text(text, max_chars=max_chars, overlap=overlap)
    pieces = []
    for offset, chunk in chunks:
        raw = pipe(chunk)
        ents = [{
            "entity": e.get("entity_group") or e.get("entity"),
            "word": e.get("word"),
            "score": float(e.get("score", 0.0)),
            "start": int(e["start"]) if e.get("start") is not None else None,
            "end": int(e["end"]) if e.get("end") is not None else None,
        } for e in raw]
        pieces.append((offset, ents))
    entities = merge_chunk_entities(pieces)
    return {"ok": True, "model": NER_MODEL, "entities": entities,
            "count": len(entities), "chunks": len(chunks),
            "chars": len(text or "")}


def run_classify(text: str) -> Dict[str, Any]:
    res = _get_sentiment_pipe()(text[:512])
    flat = res[0] if (res and isinstance(res[0], list)) else res
    labels = [{"label": x.get("label"), "score": float(x.get("score", 0.0))}
              for x in flat]
    labels.sort(key=lambda x: x["score"], reverse=True)
    return {"ok": True, "model": SENTIMENT_MODEL,
            "top": labels[0]["label"] if labels else None, "labels": labels}


def run_rerank(query: str, documents: List[str], top_k: int = 0) -> Dict[str, Any]:
    scores = [float(s) for s in _get_encoder().rerank(query, documents)]
    ranked = sorted(({"index": i, "score": s, "text": documents[i]}
                     for i, s in enumerate(scores)),
                    key=lambda r: r["score"], reverse=True)
    if top_k and top_k > 0:
        ranked = ranked[:top_k]
    return {"ok": True, "model": RERANK_MODEL, "count": len(documents),
            "ranked": ranked}


def build_app():
    from fastapi import FastAPI
    from pydantic import BaseModel

    app = FastAPI(title="Vera edge NLP")

    class NerReq(BaseModel):
        text: str = ""
        max_chars: int = 0
        overlap: int = -1

    class ClassifyReq(BaseModel):
        text: str = ""

    class RerankReq(BaseModel):
        query: str = ""
        documents: List[str] = []
        top_k: int = 0

    @app.get("/health")
    async def health():
        # Deliberately does NOT load a model: health must answer while a cold
        # model is still exporting, or the router will read a loading node as a
        # dead one and route every call away from it for minutes.
        return {"ok": True, "service": "vera-nlp",
                "threads": NLP_THREADS, "providers": _providers(),
                "models": {"ner": NER_MODEL, "classify": SENTIMENT_MODEL,
                           "rerank": RERANK_MODEL},
                "loaded": {"ner": _NER_PIPE is not None,
                           "classify": _SENTIMENT_PIPE is not None,
                           "rerank": _ENCODER is not None},
                "chunk": {"chars": CHUNK_CHARS, "overlap": CHUNK_OVERLAP},
                "core": HAS_CORE}

    @app.get("/models")
    async def models():
        return {"ner": NER_MODEL, "classify": SENTIMENT_MODEL,
                "rerank": RERANK_MODEL, "providers": _providers()}

    @app.post("/ner")
    async def ner(req: NerReq):
        if not req.text:
            return {"error": "text is required"}
        t0 = time.monotonic()
        try:
            out = run_ner(req.text, req.max_chars, req.overlap)
        except Exception as e:
            log.error("ner failed: %s", e)
            return {"error": f"ner failed: {type(e).__name__}: {e}"}
        out["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
        return out

    @app.post("/classify")
    async def classify(req: ClassifyReq):
        if not req.text:
            return {"error": "text is required"}
        try:
            return run_classify(req.text)
        except Exception as e:
            log.error("classify failed: %s", e)
            return {"error": f"classify failed: {type(e).__name__}: {e}"}

    @app.post("/rerank")
    async def rerank(req: RerankReq):
        if not req.query:
            return {"error": "query is required"}
        if not req.documents:
            return {"error": "documents is required"}
        try:
            return run_rerank(req.query, list(req.documents), req.top_k)
        except Exception as e:
            log.error("rerank failed: %s", e)
            return {"error": f"rerank failed: {type(e).__name__}: {e}"}

    return app


def _main():
    p = argparse.ArgumentParser(description="Vera edge NLP server")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    # 8771, NOT 8770 — the node agent owns 8770 on every node.
    s.add_argument("--port", type=int,
                   default=int(os.getenv("VERA_NLP_PORT", "8771") or 8771))

    w = sub.add_parser("warm", help="load the models now and report timings")
    w.add_argument("--what", default="ner")

    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.cmd == "warm":
        t0 = time.monotonic()
        if args.what in ("ner", "all"):
            _get_ner_pipe()
        if args.what in ("classify", "all"):
            _get_sentiment_pipe()
        if args.what in ("rerank", "all"):
            _get_encoder()
        print(json.dumps({"ok": True, "warmed": args.what,
                          "elapsed_s": round(time.monotonic() - t0, 1),
                          "threads": NLP_THREADS}))
        return

    import uvicorn
    log.info("vera-nlp serving on %s:%d (threads=%d)",
             args.host, args.port, NLP_THREADS)
    uvicorn.run(build_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    _main()
