"""
nlp_server.py  —  text-level NLP on a compute node, so the Vera host never runs it
==================================================================================

Deployed to an ollama node via `nodes.provision` (component `nlp_server`). Vera
calls it over HTTP from `vera/research/nlp_capabilities.py`; the Vera host keeps
NO ML runtime of its own.

    python nlp_server.py serve --host 0.0.0.0 --port 8771

WHY A TEXT ENDPOINT AND NOT `onnx_runtime.py`
─────────────────────────────────────────────
`edge/onnx_runtime.py` serves ONNX on nodes, but its `POST /run/{slug}` is
TENSOR-level: tensors in, tensors out. NER needs tokenisation in front of the
forward pass and label decoding (plus entity aggregation) behind it, and that
component's dependency list carries no tokenizer.

Shipping token IDs from the host would keep that server generic, but it still
requires a tokenizer ON the Vera host and still spends that 2-core VM's CPU
tokenising and decoding every call. Putting the whole text→result path on the
node leaves the host with zero ML dependencies, which is the actual goal.

THE MODEL STORE IS READ-ONLY
────────────────────────────
The ollama nodes share one ZFS model store, bind-mounted read-only into each
node. So models are **pre-exported** into it by `edge/nlp_export_models.py` and
loaded from disk here: no conversion, no network and no writes at request time.
`from_pretrained(id, export=True)` would convert from the torch checkpoint on
first request and needs to write — it fails on a read-only store, and it fails
*after* the service has already reported healthy. If a model is missing from the
store this server says so rather than silently reaching for the hub.

This does NOT make the runtime torch-free: `optimum[onnxruntime]` supplies the
ORTModelFor* loader classes and pulls torch as a hard dependency (measured — it
fetched a 554MB torch wheel). Pre-exporting buys the absence of CONVERSION and
of network at request time, which is what a read-only store actually requires;
it does not buy a smaller install.

⚠ PORT. The node agent (`edge/vera_node_agent.py`) owns 8770 on every node, and
the `onnx_runtime` component now takes 8772. This server is 8771.

⚠ THREADS. ONNX Runtime will otherwise take every core it can see. On gpu-250
that starves the ollama runner sharing the container, which would defeat the
premise of putting NLP there (GPU busy, CPU free). `VERA_NLP_THREADS` caps it,
applied to OpenMP BEFORE onnxruntime is imported, because the OMP pool is sized
at load time and a SessionOptions set afterwards cannot shrink it.

⚠ NO `from __future__ import annotations` in this file. It stringifies
annotations, and FastAPI resolves a stringified annotation against the MODULE
globals — so a pydantic model declared inside build_app() becomes unresolvable
and FastAPI silently binds the body parameter as a QUERY parameter, 422-ing
every POST. Verified against fastapi 0.116.1 / pydantic 2.11.7.
"""

import argparse
import os

# ── Thread cap — MUST be set before onnxruntime/torch import anything ────────
# Sized for a 12-core LXC container shared with an ollama runner.
NLP_THREADS = int(os.getenv("VERA_NLP_THREADS", "4") or 4)
for _var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    # A CAP, not a default: setdefault() would let an environment that already
    # exported a larger value blow straight through the limit this server
    # exists to enforce. A smaller existing value is a deliberate tightening.
    try:
        _existing = int(os.environ.get(_var, "") or 0)
    except ValueError:
        _existing = 0
    os.environ[_var] = str(min(_existing, NLP_THREADS) if _existing > 0
                           else NLP_THREADS)

# The store is read-only and complete; never let a miss become a silent download.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import json          # noqa: E402  (after the thread cap, deliberately)
import logging       # noqa: E402
import threading     # noqa: E402
import time          # noqa: E402
from typing import Any, Dict, List  # noqa: E402

try:
    # Shipped alongside this file by the `nlp_server` component so the node and
    # the Vera host share ONE registry and ONE chunking implementation.
    from nlp_dispatch_core import (
        DEFAULT_MODELS, TASK_KIND, chunk_text, merge_chunk_entities, model_slug,
    )
    HAS_CORE = True
except Exception:  # pragma: no cover - deployment without the core file
    HAS_CORE = False
    DEFAULT_MODELS, TASK_KIND = {}, {}

    def model_slug(m):
        return str(m).replace("/", "__")

    def chunk_text(text, max_chars=1000, overlap=0):
        return [(0, text[:max_chars])]

    def merge_chunk_entities(pieces):
        return [e for _o, ents in pieces for e in (ents or [])]

log = logging.getLogger("vera.edge.nlp")

#: Where the pre-exported ONNX model set lives (the shared store, mounted ro).
MODEL_ROOT = os.getenv("VERA_NLP_MODEL_DIR", "/opt/nlp-models")

#: Per-chunk character budget. The old in-process cap was a hard `text[:1024]`
#: truncation; this is a chunk size, not a limit on the document.
CHUNK_CHARS = int(os.getenv("VERA_NLP_CHUNK_CHARS", "1000") or 1000)
CHUNK_OVERLAP = int(os.getenv("VERA_NLP_CHUNK_OVERLAP", "100") or 100)

_ORT_CLASS = {
    "token-classification":     "ORTModelForTokenClassification",
    "text-classification":      "ORTModelForSequenceClassification",
    "zero-shot-classification": "ORTModelForSequenceClassification",
    "question-answering":       "ORTModelForQuestionAnswering",
    "feature-extraction":       "ORTModelForFeatureExtraction",
}
_PIPELINE_TASK = {
    "token-classification":     "token-classification",
    "text-classification":      "text-classification",
    "zero-shot-classification": "zero-shot-classification",
    "question-answering":       "question-answering",
}

_COMPONENT = None


def component_record() -> Dict[str, Any]:
    """This server's deployed version, from the `nlp_server.version.json` the
    deploy wrote beside it, with each recorded file re-hashed so a hand edit on
    the node shows as `intact: false` rather than passing for the version.
    Read once: a redeploy restarts the service."""
    global _COMPONENT
    if _COMPONENT is None:
        import hashlib
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            with open(os.path.join(here, "nlp_server.version.json"), encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            rec = {}
        if not isinstance(rec, dict) or not rec.get("version"):
            _COMPONENT = {"version": "", "intact": None,
                          "note": "deployed before component versions"}
        else:
            changed = []
            for name, digest in (rec.get("files") or {}).items():
                if name.startswith("<"):        # the deps spec, not a file
                    continue
                try:
                    with open(os.path.join(here, name), "rb") as fh:
                        ok = hashlib.sha256(fh.read()).hexdigest() == digest
                except OSError:
                    ok = False
                if not ok:
                    changed.append(name)
            _COMPONENT = {"version": rec["version"], "intact": not changed,
                          "changed": changed, "files": rec.get("files") or {},
                          "deps_installed": rec.get("deps_installed")}
    return _COMPONENT


_PIPES: Dict[str, Any] = {}
_RAW: Dict[str, Any] = {}          # task -> (model, tokenizer) for embeddings
_ENCODER = None
_LOCK = threading.Lock()
_MANIFEST = None


def _manifest():
    """Read the export manifest once; never hash model files on health paths."""
    global _MANIFEST
    if _MANIFEST is None:
        try:
            with open(os.path.join(MODEL_ROOT, "manifest.json"), encoding="utf-8") as fh:
                value = json.load(fh)
            _MANIFEST = value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            _MANIFEST = {}
    return _MANIFEST


def task_inventory(task: str) -> Dict[str, Any]:
    row = {"model": model_id_for(task), "kind": TASK_KIND.get(task),
           "present": model_present(task),
           "loaded": (task in _PIPES or task in _RAW or
                      (task == "rerank" and _ENCODER is not None))}
    package = (_manifest().get("model_packages") or {}).get(task)
    if isinstance(package, dict):
        row["model_package"] = package
    else:
        row["inventory_status"] = "missing_content_verified_manifest"
    return row


def model_id_for(task: str) -> str:
    """Model id for a task. `VERA_NLP_MODEL_<TASK>` overrides the registry."""
    return os.getenv("VERA_NLP_MODEL_" + task.upper(),
                     DEFAULT_MODELS.get(task, ""))


def model_path_for(task: str) -> str:
    if TASK_KIND.get(task) == "fastembed":
        return fastembed_dir()          # its own cache layout, not a slug dir
    mid = model_id_for(task)
    return os.path.join(MODEL_ROOT, model_slug(mid)) if mid else ""


def model_present(task: str) -> bool:
    """Is this task's model actually in the store?

    fastembed is the exception: it keeps its own cache layout under `_fastembed`
    rather than a slug directory with a .onnx beside the config, so asking the
    slug path would always answer "no" and the inventory would under-report a
    model that is in fact present and loadable.
    """
    if TASK_KIND.get(task) == "fastembed":
        d = fastembed_dir()
        try:
            return os.path.isdir(d) and bool(os.listdir(d))
        except OSError:
            return False
    p = model_path_for(task)
    try:
        return bool(p) and os.path.isdir(p) and any(
            f.endswith(".onnx") for f in os.listdir(p))
    except OSError:
        return False


def _session_options():
    """ORT session options with the thread cap applied.

    Belt and braces with the OpenMP env vars above: those bound the OpenMP pool
    at import time, this bounds ORT's own intra-op pool.
    """
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = NLP_THREADS
    so.inter_op_num_threads = 1
    return so


def _load(task: str):
    """Load (model, tokenizer) for a task from the store. Never from the hub."""
    kind = TASK_KIND.get(task, "")
    cls_name = _ORT_CLASS.get(kind)
    if not cls_name:
        raise RuntimeError(f"no loader for task '{task}' (kind '{kind}')")
    path = model_path_for(task)
    if not model_present(task):
        raise RuntimeError(
            f"model for '{task}' is not in the store: expected {path}. "
            f"Build it with nlp_export_models.py and re-mount the store.")
    import optimum.onnxruntime as ORT
    from transformers import AutoTokenizer
    cls = getattr(ORT, cls_name)
    log.info("nlp_server: loading %s from %s (threads=%d)",
             task, path, NLP_THREADS)
    model = cls.from_pretrained(path, session_options=_session_options())
    tok = AutoTokenizer.from_pretrained(path)
    return model, tok


def get_pipe(task: str):
    """A transformers pipeline for `task`, built once and cached."""
    if task not in _PIPES:
        with _LOCK:
            if task not in _PIPES:
                from transformers import pipeline
                kind = TASK_KIND.get(task, "")
                ptask = _PIPELINE_TASK.get(kind)
                if not ptask:
                    raise RuntimeError(f"task '{task}' has no pipeline")
                model, tok = _load(task)
                kw = {}
                if kind == "token-classification":
                    kw["aggregation_strategy"] = "simple"
                if kind == "text-classification":
                    kw["top_k"] = None
                _PIPES[task] = pipeline(ptask, model=model, tokenizer=tok, **kw)
    return _PIPES[task]


def get_raw(task: str):
    if task not in _RAW:
        with _LOCK:
            if task not in _RAW:
                _RAW[task] = _load(task)
    return _RAW[task]


def fastembed_dir() -> str:
    """Where fastembed's own ONNX cache lives — inside the shared store.

    fastembed ships ONNX rather than a torch checkpoint, so it is not part of
    the optimum export; but it still DOWNLOADS on first use, and this server
    runs offline against a read-only store. The cache is therefore populated
    once by the exporter and mounted read-only with everything else.
    """
    return os.getenv("VERA_NLP_FASTEMBED_DIR",
                     os.path.join(MODEL_ROOT, "_fastembed"))


def get_encoder():
    """fastembed reranker, read from the pre-populated cache in the store."""
    global _ENCODER
    if _ENCODER is None:
        with _LOCK:
            if _ENCODER is None:
                from fastembed.rerank.cross_encoder import TextCrossEncoder
                _ENCODER = TextCrossEncoder(model_name=model_id_for("rerank"),
                                            cache_dir=fastembed_dir())
    return _ENCODER


def _providers() -> List[str]:
    try:
        import onnxruntime as ort
        return list(ort.get_available_providers())
    except Exception:
        return []


# ── Operations ───────────────────────────────────────────────────────────────

def run_ner(text, task="ner", max_chars=0, overlap=-1):
    """Entities over the WHOLE text, not its first paragraph."""
    max_chars = int(max_chars or CHUNK_CHARS)
    overlap = CHUNK_OVERLAP if overlap is None or overlap < 0 else int(overlap)
    # A caller-supplied max_chars smaller than the overlap would make chunk_text
    # raise, turning a tuning choice into a 500. Clamp instead.
    if overlap >= max_chars:
        overlap = max(0, max_chars // 10)
    pipe = get_pipe(task)
    chunks = chunk_text(text, max_chars=max_chars, overlap=overlap)
    pieces = []
    for offset, chunk in chunks:
        ents = [{
            "entity": e.get("entity_group") or e.get("entity"),
            "word": e.get("word"),
            "score": float(e.get("score", 0.0)),
            "start": int(e["start"]) if e.get("start") is not None else None,
            "end": int(e["end"]) if e.get("end") is not None else None,
        } for e in pipe(chunk)]
        pieces.append((offset, ents))
    entities = merge_chunk_entities(pieces)
    return {"ok": True, "task": task, "model": model_id_for(task),
            "entities": entities, "count": len(entities),
            "chunks": len(chunks), "chars": len(text or "")}


def run_classify(text, task="classify"):
    res = get_pipe(task)(text[:512])
    flat = res[0] if (res and isinstance(res[0], list)) else res
    labels = [{"label": x.get("label"), "score": float(x.get("score", 0.0))}
              for x in flat]
    labels.sort(key=lambda x: x["score"], reverse=True)
    return {"ok": True, "task": task, "model": model_id_for(task),
            "top": labels[0]["label"] if labels else None, "labels": labels}


def run_zeroshot(text, labels, multi_label=False):
    """Classify against labels supplied at call time — no training, no LLM."""
    out = get_pipe("zeroshot")(text, candidate_labels=list(labels),
                               multi_label=bool(multi_label))
    scored = [{"label": l, "score": float(s)}
              for l, s in zip(out["labels"], out["scores"])]
    return {"ok": True, "model": model_id_for("zeroshot"),
            "top": scored[0]["label"] if scored else None,
            "labels": scored, "multi_label": bool(multi_label)}


def run_qa(question, context):
    out = get_pipe("qa")(question=question, context=context)
    return {"ok": True, "model": model_id_for("qa"),
            "answer": out.get("answer"), "score": float(out.get("score", 0.0)),
            "start": out.get("start"), "end": out.get("end")}


def run_langid(text):
    res = get_pipe("langid")(text[:512])
    flat = res[0] if (res and isinstance(res[0], list)) else res
    langs = sorted(({"lang": x.get("label"), "score": float(x.get("score", 0.0))}
                    for x in flat), key=lambda x: x["score"], reverse=True)
    return {"ok": True, "model": model_id_for("langid"),
            "lang": langs[0]["lang"] if langs else None, "langs": langs[:5]}


def run_embed(texts):
    """Mean-pooled, L2-normalised sentence embeddings.

    The feature-extraction pipeline returns per-TOKEN vectors; a sentence vector
    needs masked mean pooling, so this calls the model directly rather than
    through a pipeline.
    """
    import numpy as np
    model, tok = get_raw("embed")
    enc = tok(list(texts), padding=True, truncation=True, max_length=512,
              return_tensors="np")
    out = model(**{k: v for k, v in enc.items()})
    hidden = out[0] if isinstance(out, (tuple, list)) else (
        getattr(out, "last_hidden_state", None))
    if hidden is None:
        hidden = list(out.values())[0]
    hidden = np.asarray(hidden)
    mask = np.asarray(enc["attention_mask"])[..., None].astype(hidden.dtype)
    summed = (hidden * mask).sum(axis=1)
    counts = np.clip(mask.sum(axis=1), 1e-9, None)
    vecs = summed / counts
    norms = np.clip(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-12, None)
    vecs = vecs / norms
    return {"ok": True, "model": model_id_for("embed"),
            "dim": int(vecs.shape[1]), "count": int(vecs.shape[0]),
            "embeddings": vecs.astype(float).tolist()}


def run_rerank(query, documents, top_k=0):
    scores = [float(s) for s in get_encoder().rerank(query, documents)]
    ranked = sorted(({"index": i, "score": s, "text": documents[i]}
                     for i, s in enumerate(scores)),
                    key=lambda r: r["score"], reverse=True)
    if top_k and top_k > 0:
        ranked = ranked[:top_k]
    return {"ok": True, "model": model_id_for("rerank"),
            "count": len(documents), "ranked": ranked}


# ── HTTP ─────────────────────────────────────────────────────────────────────

def build_app():
    from fastapi import FastAPI
    from pydantic import BaseModel

    app = FastAPI(title="Vera edge NLP")

    class NerReq(BaseModel):
        text: str = ""
        task: str = "ner"
        max_chars: int = 0
        overlap: int = -1

    class ClassifyReq(BaseModel):
        text: str = ""
        task: str = "classify"

    class ZeroShotReq(BaseModel):
        text: str = ""
        labels: List[str] = []
        multi_label: bool = False

    class QaReq(BaseModel):
        question: str = ""
        context: str = ""

    class TextReq(BaseModel):
        text: str = ""

    class EmbedReq(BaseModel):
        texts: List[str] = []

    class RerankReq(BaseModel):
        query: str = ""
        documents: List[str] = []
        top_k: int = 0

    def _guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            log.error("%s failed: %s", getattr(fn, "__name__", "op"), e)
            return {"error": f"{getattr(fn, '__name__', 'op')} failed: "
                             f"{type(e).__name__}: {e}"}

    @app.get("/health")
    async def health():
        # Deliberately does NOT load a model: health must answer while a cold
        # model is still loading, or the router reads a loading node as a dead
        # one and routes away from it for minutes.
        return {"ok": True, "service": "vera-nlp", "threads": NLP_THREADS,
                "providers": _providers(), "model_root": MODEL_ROOT,
                "core": HAS_CORE, "component": component_record(),
                "chunk": {"chars": CHUNK_CHARS, "overlap": CHUNK_OVERLAP},
                "tasks": {t: task_inventory(t) for t in DEFAULT_MODELS}}

    @app.get("/models")
    async def models():
        return {"model_root": MODEL_ROOT,
                "schema": "vera.nlp-node-models/v2",
                "component": component_record(),
                "models": {t: task_inventory(t) for t in DEFAULT_MODELS},
                "providers": _providers()}

    @app.post("/ner")
    async def ner(req: NerReq):
        if not req.text:
            return {"error": "text is required"}
        t0 = time.monotonic()
        out = _guard(run_ner, req.text, req.task, req.max_chars, req.overlap)
        if isinstance(out, dict):
            out["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
        return out

    @app.post("/classify")
    async def classify(req: ClassifyReq):
        if not req.text:
            return {"error": "text is required"}
        return _guard(run_classify, req.text, req.task)

    @app.post("/zeroshot")
    async def zeroshot(req: ZeroShotReq):
        if not req.text:
            return {"error": "text is required"}
        if not req.labels:
            return {"error": "labels is required (a list of candidate labels)"}
        return _guard(run_zeroshot, req.text, req.labels, req.multi_label)

    @app.post("/qa")
    async def qa(req: QaReq):
        if not req.question:
            return {"error": "question is required"}
        if not req.context:
            return {"error": "context is required"}
        return _guard(run_qa, req.question, req.context)

    @app.post("/langid")
    async def langid(req: TextReq):
        if not req.text:
            return {"error": "text is required"}
        return _guard(run_langid, req.text)

    @app.post("/embed")
    async def embed(req: EmbedReq):
        if not req.texts:
            return {"error": "texts is required (a list of strings)"}
        return _guard(run_embed, req.texts)

    @app.post("/rerank")
    async def rerank(req: RerankReq):
        if not req.query:
            return {"error": "query is required"}
        if not req.documents:
            return {"error": "documents is required"}
        return _guard(run_rerank, req.query, list(req.documents), req.top_k)

    return app


def _main():
    p = argparse.ArgumentParser(description="Vera edge NLP server")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    # 8771, NOT 8770 — the node agent owns 8770 on every node.
    s.add_argument("--port", type=int,
                   default=int(os.getenv("VERA_NLP_PORT", "8771") or 8771))

    w = sub.add_parser("warm", help="load models now and report timings")
    w.add_argument("--tasks", default="ner",
                   help="comma-separated task names, or 'all'")

    sub.add_parser("check", help="report which models the store actually has")

    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.cmd == "check":
        rows = {t: {"model": model_id_for(t), "kind": TASK_KIND.get(t),
                    "present": model_present(t), "path": model_path_for(t)}
                for t in DEFAULT_MODELS}
        print(json.dumps({"model_root": MODEL_ROOT, "tasks": rows}, indent=2))
        missing = [t for t, r in rows.items()
                   if not r["present"] and TASK_KIND.get(t) != "fastembed"]
        return 1 if missing else 0

    if args.cmd == "warm":
        tasks = (list(DEFAULT_MODELS) if args.tasks == "all"
                 else [t.strip() for t in args.tasks.split(",") if t.strip()])
        out = {}
        for t in tasks:
            t0 = time.monotonic()
            try:
                if t == "rerank":
                    get_encoder()
                elif t == "embed":
                    get_raw(t)
                else:
                    get_pipe(t)
                out[t] = {"ok": True, "s": round(time.monotonic() - t0, 1)}
            except Exception as e:
                out[t] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        print(json.dumps({"threads": NLP_THREADS, "warmed": out}, indent=2))
        return 0 if all(v.get("ok") for v in out.values()) else 1

    import uvicorn
    log.info("vera-nlp serving on %s:%d (threads=%d, models=%s)",
             args.host, args.port, NLP_THREADS, MODEL_ROOT)
    uvicorn.run(build_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
