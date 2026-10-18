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
# How many inferences run at once. Each takes up to NLP_THREADS cores, so the default of one keeps the
# cap above; inference still runs OFF the event loop, so /health and request intake answer while a model
# works (RF-03: run synchronously inside async handlers, one long call blocked the whole server and the
# router read a busy node as a dead one). Raise it only on a node with cores to spare.
NLP_WORKERS = max(1, int(os.getenv("VERA_NLP_WORKERS", "1") or 1))
# The FAST lane: embed and langid run in their own one-worker pool, so a latency-critical call (the
# decision-space vector a chat turn waits for, ≈ 9 ms) never queues behind a rerank, NER or QA call.
# Measured 2026-10-09 with one shared worker, 4 embed + 1 rerank clients: embed p95 ≈ 115 ms against a
# 40 ms budget, all of it queueing. A fast call holds its threads for milliseconds, so the CPU cap barely
# moves. VERA_NLP_FAST_LANE=0 puts them back in the shared pool.
NLP_FAST_LANE = str(os.getenv("VERA_NLP_FAST_LANE", "1")).strip().lower() not in ("0", "false", "no", "off")
FAST_TASKS = ("embed", "langid")
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
# Imported here, at module load, never inside build_app(): serve() starts the preload thread (which
# imports onnxruntime/transformers, which import these) and then calls build_app(). An import inside
# build_app() raced the preload thread for the per-module import locks and deadlocked startup
# (cpu-246, 2026-10-09: the unit logged 'serving' but never bound 8771).
import asyncio      # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from typing import Any, Dict, List  # noqa: E402

try:
    # Shipped alongside this file by the `nlp_server` component so the node and
    # the Vera host share ONE registry and ONE chunking implementation.
    from nlp_dispatch_core import (
        DEFAULT_MODELS, TASK_KIND, GLINER_LABELS_DEFAULT, chunk_text,
        merge_chunk_entities, model_slug,
    )
    HAS_CORE = True
except Exception:  # pragma: no cover - deployment without the core file
    HAS_CORE = False
    DEFAULT_MODELS, TASK_KIND, GLINER_LABELS_DEFAULT = {}, {}, []

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
    model that is in fact present and loadable. gliner is a torch checkpoint
    (gliner_config.json beside the weights, no .onnx); spacy is a pip package,
    present when it imports.
    """
    kind = TASK_KIND.get(task)
    if kind == "fastembed":
        d = fastembed_dir()
        try:
            return os.path.isdir(d) and bool(os.listdir(d))
        except OSError:
            return False
    if kind == "gliner":
        p = model_path_for(task)
        return bool(p) and os.path.isfile(os.path.join(p, "gliner_config.json"))
    if kind == "spacy":
        import importlib.util
        return importlib.util.find_spec(model_id_for(task).replace("-", "_")) is not None
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


def get_gliner():
    """The GLiNER model from the store — a torch checkpoint saved by the exporter, never the hub."""
    if "gliner" not in _RAW:
        with _LOCK:
            if "gliner" not in _RAW:
                if not model_present("gliner"):
                    raise RuntimeError(
                        f"gliner is not in the store: expected {model_path_for('gliner')}. "
                        f"Build it with nlp_export_models.py build --only gliner and re-mount the store.")
                from gliner import GLiNER
                try:
                    import torch
                    torch.set_num_threads(NLP_THREADS)
                except Exception:
                    pass
                log.info("nlp_server: loading gliner from %s", model_path_for("gliner"))
                _RAW["gliner"] = GLiNER.from_pretrained(model_path_for("gliner"), local_files_only=True)
    return _RAW["gliner"]


def load_task(task: str) -> Dict[str, Any]:
    """Load one task's model now: {ok, s} or {ok: False, error}."""
    t0 = time.monotonic()
    try:
        kind = TASK_KIND.get(task)
        if task == "rerank":
            get_encoder()
        elif task == "embed":
            get_raw(task)
        elif kind == "gliner":
            get_gliner()
        elif kind == "spacy":
            get_spacy()
        else:
            get_pipe(task)
        return {"ok": True, "s": round(time.monotonic() - t0, 1)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


#: The startup preload (VERA_NLP_PRELOAD: "all" - every task whose model is in
#: the store - a comma list, or "" for lazy loading). The models are small
#: (the lot is a few GB) and the first call to a cold one waited 10-15 s
#: (GLiNER, 2026-09-30), so every node keeps them all loaded.
PRELOAD: Dict[str, Any] = {"state": "idle", "loaded": {}, "failed": {}}


def preload() -> None:
    want = os.getenv("VERA_NLP_PRELOAD", "").strip()
    if not want:
        PRELOAD["state"] = "off"
        return
    tasks = (list(DEFAULT_MODELS) if want == "all"
             else [t.strip() for t in want.split(",") if t.strip()])
    PRELOAD["state"] = "loading"
    for t in tasks:
        if not model_present(t):
            continue                      # not in the store: nothing to warm
        r = load_task(t)
        (PRELOAD["loaded"] if r.get("ok") else PRELOAD["failed"])[t] = r.get("s", r.get("error"))
    PRELOAD["state"] = "done"
    log.info("nlp_server: preloaded %s%s", sorted(PRELOAD["loaded"]),
             f", failed {PRELOAD['failed']}" if PRELOAD["failed"] else "")


def get_spacy():
    """The spaCy pipeline named by the registry — a pip package the component installed."""
    if "spacy" not in _RAW:
        with _LOCK:
            if "spacy" not in _RAW:
                if not model_present("spacy"):
                    raise RuntimeError(
                        f"spaCy pipeline '{model_id_for('spacy')}' is not installed on this node "
                        f"(the component's pip steps put it in the venv).")
                import spacy
                log.info("nlp_server: loading spacy %s", model_id_for("spacy"))
                _RAW["spacy"] = spacy.load(model_id_for("spacy"), disable=["lemmatizer"])
    return _RAW["spacy"]


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

def run_ner(text, task="ner", max_chars=0, overlap=-1, labels=None, threshold=0.4):
    """Entities over the WHOLE text, not its first paragraph. Three engines behind one shape:
    the ORT token classifiers (`ner`, `ner_multi`), GLiNER (`gliner` — zero-shot over `labels`,
    the fabric's set when none are given, at `threshold`) and spaCy (`spacy`)."""
    kind = TASK_KIND.get(task, "")
    max_chars = int(max_chars or (1400 if kind == "gliner" else CHUNK_CHARS))
    overlap = CHUNK_OVERLAP if overlap is None or overlap < 0 else int(overlap)
    # A caller-supplied max_chars smaller than the overlap would make chunk_text
    # raise, turning a tuning choice into a 500. Clamp instead.
    if overlap >= max_chars:
        overlap = max(0, max_chars // 10)
    if kind == "gliner":
        model = get_gliner()
        labels = [str(l) for l in (labels or [])] or list(GLINER_LABELS_DEFAULT)
        def _run(chunk):
            return [{"entity": e.get("label"), "word": e.get("text"), "score": float(e.get("score", 0.0)),
                     "start": int(e["start"]), "end": int(e["end"])}
                    for e in model.predict_entities(chunk, labels, threshold=float(threshold or 0.4))]
    elif kind == "spacy":
        nlp = get_spacy()
        _drop = {"CARDINAL", "ORDINAL", "PERCENT", "QUANTITY"}
        def _run(chunk):
            return [{"entity": e.label_, "word": e.text, "score": 0.85, "start": int(e.start_char), "end": int(e.end_char)}
                    for e in nlp(chunk).ents if e.label_ not in _drop]
    else:
        pipe = get_pipe(task)
        def _run(chunk):
            return [{
                "entity": e.get("entity_group") or e.get("entity"),
                "word": e.get("word"),
                "score": float(e.get("score", 0.0)),
                "start": int(e["start"]) if e.get("start") is not None else None,
                "end": int(e["end"]) if e.get("end") is not None else None,
            } for e in pipe(chunk)]
    chunks = chunk_text(text, max_chars=max_chars, overlap=overlap)
    pieces = [(offset, _run(chunk)) for offset, chunk in chunks]
    entities = merge_chunk_entities(pieces)
    return {"ok": True, "task": task, "model": model_id_for(task),
            "entities": entities, "count": len(entities),
            "chunks": len(chunks), "chars": len(text or ""),
            **({"labels": labels, "threshold": float(threshold or 0.4)} if kind == "gliner" else {})}


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


NLI_MAX_PAIRS = 64
NLI_BATCH = 8


def run_nli(pairs):
    """Premise -> hypothesis inference for each pair: {entailment, neutral, contradiction}
    probabilities, in order. Scored raw (the text-classification pipeline takes one text), with
    the pipeline's own model and tokenizer, so the preload's copy is the only one in memory."""
    import torch
    pipe = get_pipe("nli")
    model, tok = pipe.model, pipe.tokenizer
    names = {int(i): str(lab).lower() for i, lab in model.config.id2label.items()}
    out = []
    for k in range(0, len(pairs), NLI_BATCH):
        chunk = pairs[k:k + NLI_BATCH]
        enc = tok([p["premise"] for p in chunk], [p["hypothesis"] for p in chunk],
                  truncation="only_first", max_length=320, padding=True, return_tensors="pt")
        with torch.no_grad():
            probs = torch.softmax(model(**enc).logits, dim=-1).tolist()
        out += [{names.get(i, str(i)): round(float(v), 4) for i, v in enumerate(row)} for row in probs]
    return {"ok": True, "model": model_id_for("nli"), "results": out}


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

# ── /decide: System 1 decisions on the node (four-minds FM-9.1) ─────────────
# The worker nodes are the compute pool: a decision that needs real compute
# runs here, not in the Vera host process. v1 is nearest-neighbour search
# (the kNN judge, the tool shortlist): the host pushes an index once
# (POST /decide/index, kept in memory as a normalised float32 matrix) and
# asks for the k nearest rows (POST /decide). /health advertises the methods
# and the indexes held, and a missing or stale index answers "index_missing"
# so the host pushes it and asks again.
DECIDE_SCHEMA = "vera.decide/v1"
DECIDE_METHODS = ("knn",)
DECIDE_MAX_INDEXES = int(os.getenv("VERA_DECIDE_MAX_INDEXES", "8") or 8)
DECIDE_MAX_ROWS = int(os.getenv("VERA_DECIDE_MAX_ROWS", "50000") or 50000)
_DECIDE: Dict[str, Dict[str, Any]] = {}
_DECIDE_LOCK = threading.Lock()


def decide_index_put(index_id, version, rows):
    """Keep an index: rows [{id, vec, run, payload}]. The newest index wins its id; the oldest index is
    dropped past DECIDE_MAX_INDEXES."""
    import numpy as np
    index_id, version = str(index_id or "").strip(), str(version or "").strip()
    if not index_id or not version:
        return {"error": "index_id and version are required"}
    rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("vec")]
    if not rows:
        return {"error": "rows is required (each with a vec)"}
    if len(rows) > DECIDE_MAX_ROWS:
        return {"error": f"too many rows ({len(rows)} > {DECIDE_MAX_ROWS})"}
    dim = len(rows[0]["vec"])
    rows = [r for r in rows if len(r["vec"]) == dim]
    mat = np.asarray([r["vec"] for r in rows], dtype=np.float32)
    mat /= np.clip(np.linalg.norm(mat, axis=1, keepdims=True), 1e-12, None)
    entry = {"version": version, "mat": mat, "dim": dim,
             "ids": [str(r.get("id", i)) for i, r in enumerate(rows)],
             "runs": np.asarray([str(r.get("run") or "") for r in rows], dtype=object),
             "payloads": [r.get("payload") for r in rows], "at": time.time()}
    with _DECIDE_LOCK:
        _DECIDE[index_id] = entry
        while len(_DECIDE) > DECIDE_MAX_INDEXES:
            del _DECIDE[min(_DECIDE, key=lambda k: _DECIDE[k]["at"])]
    return {"ok": True, "index_id": index_id, "version": version, "rows": len(rows), "dim": dim}


def decide_inventory():
    with _DECIDE_LOCK:
        return {k: {"version": v["version"], "rows": int(v["mat"].shape[0]), "dim": v["dim"]}
                for k, v in _DECIDE.items()}


def decide_knn(index_id, version, vector, k=15, exclude_run=""):
    """The k nearest rows to `vector` (cosine), best first; rows of `exclude_run` never count."""
    import numpy as np
    with _DECIDE_LOCK:
        e = _DECIDE.get(str(index_id or ""))
    if e is None or (version and e["version"] != str(version)):
        return {"error": "index_missing", "index_id": index_id, "have": e["version"] if e else None}
    q = np.asarray(vector or [], dtype=np.float32)
    if q.shape != (e["dim"],):
        return {"error": f"vector has {q.shape[0] if q.ndim else 0} dims, the index {e['dim']}"}
    t0 = time.perf_counter()
    q /= max(float(np.linalg.norm(q)), 1e-12)
    sims = e["mat"] @ q
    eligible = int(sims.shape[0])
    if exclude_run:
        mask = e["runs"] == str(exclude_run)
        sims = np.where(mask, -np.inf, sims)
        eligible -= int(mask.sum())
    k = max(0, min(int(k or 15), eligible))
    idx = np.argpartition(-sims, k - 1)[:k] if k else np.asarray([], dtype=int)
    idx = idx[np.argsort(-sims[idx])] if k else idx
    out = [{"id": e["ids"][i], "score": round(float(sims[i]), 5), "run": str(e["runs"][i]),
            "payload": e["payloads"][i]} for i in idx]
    return {"ok": True, "neighbours": out, "eligible": eligible, "rows": int(e["mat"].shape[0]),
            "ms": round((time.perf_counter() - t0) * 1000, 3)}


def build_app():
    from fastapi import FastAPI
    from pydantic import BaseModel

    app = FastAPI(title="Vera edge NLP")
    # Every call reported to the Estate Activity pane (edge/activity_record.py):
    # a pure-ASGI tee, bytes untouched. Absent module = old behaviour.
    try:
        import activity_record
        activity_record.install(app, "nlp", ("/ner", "/classify", "/zeroshot", "/qa", "/nli",
                                             "/langid", "/embed", "/rerank", "/decide",
                                             "/decide/index"),
                                port=int(os.getenv("VERA_NLP_PORT", "8771") or 8771))
    except Exception as e:                    # never let reporting stop the server
        log.warning("activity reporting off: %s", e)

    class NerReq(BaseModel):
        text: str = ""
        task: str = "ner"
        max_chars: int = 0
        overlap: int = -1
        labels: List[str] = []        # gliner: what to look for (the fabric's set when empty)
        threshold: float = 0.4        # gliner: the score below which a span is not an entity

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

    class NliReq(BaseModel):
        pairs: List[Dict[str, str]] = []

    class TextReq(BaseModel):
        text: str = ""

    class EmbedReq(BaseModel):
        texts: List[str] = []

    class RerankReq(BaseModel):
        query: str = ""
        documents: List[str] = []
        top_k: int = 0

    class DecideIndexReq(BaseModel):
        index_id: str = ""
        version: str = ""
        rows: List[Dict[str, Any]] = []

    class DecideReq(BaseModel):
        method: str = "knn"
        index_id: str = ""
        version: str = ""
        text: str = ""
        vector: List[float] = []
        k: int = 15
        exclude_run: str = ""

    def _guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            log.error("%s failed: %s", getattr(fn, "__name__", "op"), e)
            return {"error": f"{getattr(fn, '__name__', 'op')} failed: "
                             f"{type(e).__name__}: {e}"}

    _infer = ThreadPoolExecutor(max_workers=NLP_WORKERS, thread_name_prefix="nlp-infer")
    _fast = (ThreadPoolExecutor(max_workers=1, thread_name_prefix="nlp-fast") if NLP_FAST_LANE else _infer)
    _load = {"inflight": 0, "served": 0, "fast_inflight": 0}

    async def _run(fn, *a, fast: bool = False):
        """One model call in a pool, never on the event loop: the fast lane for embed/langid, the shared
        pool (NLP_WORKERS at a time) for everything else."""
        key = "fast_inflight" if fast else "inflight"
        _load[key] += 1
        try:
            return await asyncio.get_running_loop().run_in_executor(_fast if fast else _infer,
                                                                    lambda: _guard(fn, *a))
        finally:
            _load[key] -= 1
            _load["served"] += 1

    @app.get("/health")
    async def health():
        # Deliberately does NOT load a model: health must answer while a cold
        # model is still loading, or the router reads a loading node as a dead
        # one and routes away from it for minutes.
        return {"ok": True, "service": "vera-nlp", "threads": NLP_THREADS,
                "workers": NLP_WORKERS, "inflight": _load["inflight"], "served": _load["served"],
                "fast_lane": list(FAST_TASKS) if NLP_FAST_LANE else [], "fast_inflight": _load["fast_inflight"],
                "providers": _providers(), "model_root": MODEL_ROOT,
                "core": HAS_CORE, "component": component_record(),
                "chunk": {"chars": CHUNK_CHARS, "overlap": CHUNK_OVERLAP},
                "preload": {"state": PRELOAD["state"], "loaded": sorted(PRELOAD["loaded"]),
                            "failed": PRELOAD["failed"]},
                "decide": {"schema": DECIDE_SCHEMA, "methods": list(DECIDE_METHODS),
                           "indexes": decide_inventory()},
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
        out = await _run(run_ner, req.text, req.task, req.max_chars, req.overlap, req.labels, req.threshold)
        if isinstance(out, dict):
            out["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
        return out

    @app.post("/classify")
    async def classify(req: ClassifyReq):
        if not req.text:
            return {"error": "text is required"}
        return await _run(run_classify, req.text, req.task)

    @app.post("/zeroshot")
    async def zeroshot(req: ZeroShotReq):
        if not req.text:
            return {"error": "text is required"}
        if not req.labels:
            return {"error": "labels is required (a list of candidate labels)"}
        return await _run(run_zeroshot, req.text, req.labels, req.multi_label)

    @app.post("/nli")
    async def nli(req: NliReq):
        pairs = [{"premise": str(p.get("premise") or "")[:4000],
                  "hypothesis": str(p.get("hypothesis") or "")[:1000]}
                 for p in (req.pairs or [])[:NLI_MAX_PAIRS] if isinstance(p, dict)]
        pairs = [p for p in pairs if p["premise"].strip() and p["hypothesis"].strip()]
        if not pairs:
            return {"error": "pairs is required: [{premise, hypothesis}, ...]"}
        return await _run(run_nli, pairs)

    @app.post("/qa")
    async def qa(req: QaReq):
        if not req.question:
            return {"error": "question is required"}
        if not req.context:
            return {"error": "context is required"}
        return await _run(run_qa, req.question, req.context)

    @app.post("/langid")
    async def langid(req: TextReq):
        if not req.text:
            return {"error": "text is required"}
        return await _run(run_langid, req.text, fast=True)

    @app.post("/embed")
    async def embed(req: EmbedReq):
        if not req.texts:
            return {"error": "texts is required (a list of strings)"}
        return await _run(run_embed, req.texts, fast=True)

    @app.post("/rerank")
    async def rerank(req: RerankReq):
        if not req.query:
            return {"error": "query is required"}
        if not req.documents:
            return {"error": "documents is required"}
        return await _run(run_rerank, req.query, list(req.documents), req.top_k)

    @app.post("/decide/index")
    async def decide_index(req: DecideIndexReq):
        return await _run(decide_index_put, req.index_id, req.version, list(req.rows))

    @app.post("/decide")
    async def decide(req: DecideReq):
        if req.method not in DECIDE_METHODS:
            return {"error": f"method must be one of {list(DECIDE_METHODS)}"}
        vector, embed_model = list(req.vector), None
        if not vector:
            if not req.text.strip():
                return {"error": "text or vector is required"}
            emb = await _run(run_embed, [req.text], fast=True)
            if not isinstance(emb, dict) or emb.get("error") or not emb.get("embeddings"):
                return {"error": "embed failed: " + str((emb or {}).get("error"))[:200]}
            vector, embed_model = emb["embeddings"][0], emb.get("model")
        out = await _run(decide_knn, req.index_id, req.version, vector, req.k, req.exclude_run)
        if isinstance(out, dict) and embed_model:
            out["embed_model"] = embed_model
        return out

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
        out = {t: load_task(t) for t in tasks}
        print(json.dumps({"threads": NLP_THREADS, "warmed": out}, indent=2))
        return 0 if all(v.get("ok") for v in out.values()) else 1

    import uvicorn
    log.info("vera-nlp serving on %s:%d (threads=%d, models=%s)",
             args.host, args.port, NLP_THREADS, MODEL_ROOT)
    # build the app BEFORE the preload thread starts, so none of its imports race the thread's (a deadlock
    # on the per-module import locks left the unit 'serving' but never bound, 2026-10-09)
    app = build_app()
    threading.Thread(target=preload, name="nlp-preload", daemon=True).start()
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
