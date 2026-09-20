"""
nlp_export_models.py  —  build the ONNX model set into the shared model store
=============================================================================

Run ONCE per model, on a box with internet and torch. Downloads each model from
the hub, exports it to ONNX, and writes a self-contained directory (weights +
tokenizer + config) that `edge/nlp_server.py` can load with NO torch, NO network
and NO write access.

    python nlp_export_models.py build --out /mnt/serve/nlp-build/out
    python nlp_export_models.py build --out ... --only ner,zeroshot
    python nlp_export_models.py verify --out /opt/nlp-models

WHY EXPORT AHEAD OF TIME INSTEAD OF `from_pretrained(..., export=True)`
──────────────────────────────────────────────────────────────────────
The ollama nodes share ONE model store, and it is bind-mounted **read-only**
into every node (`mp1: /tank_sdh/vera-store/models/ollama,...,ro=1`; the NLP set
is mounted the same way). `export=True` converts from the torch checkpoint at
load time and needs to WRITE — to the HF cache, and often to a temp dir beside
the model. On a read-only store that fails on the first request, after the
service has already reported healthy.

Exporting ahead of time also means the runtime needs neither torch (~2.5 GB) nor
outbound network on a production node, and a cold start is a file read rather
than an ONNX conversion.

Resumable: a model whose directory already contains a .onnx file is skipped, so
a re-run after a network failure only fetches what is missing.
"""

import argparse
import json
import os
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..", "vera", "research")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from nlp_dispatch_core import DEFAULT_MODELS, TASK_KIND, model_slug
except ImportError:  # running from the repo rather than a deploy dir
    from vera.research.nlp_dispatch_core import (  # type: ignore
        DEFAULT_MODELS, TASK_KIND, model_slug,
    )

#: optimum class per task kind. Kept here rather than in the core because the
#: core must stay importable without optimum installed.
_ORT_CLASS = {
    "token-classification":    "ORTModelForTokenClassification",
    "text-classification":     "ORTModelForSequenceClassification",
    "zero-shot-classification": "ORTModelForSequenceClassification",
    "question-answering":      "ORTModelForQuestionAnswering",
    "feature-extraction":      "ORTModelForFeatureExtraction",
}


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _already_built(path):
    return os.path.isdir(path) and any(
        f.endswith(".onnx") for f in os.listdir(path))


def build_one(task, model_id, out_root):
    kind = TASK_KIND.get(task, "")
    target = os.path.join(out_root, model_slug(model_id))

    if kind == "fastembed":
        # fastembed ships ONNX rather than a torch checkpoint, so there is
        # nothing to export — but it still DOWNLOADS on first use, and the
        # server runs offline against a read-only store. So warm it INTO the
        # store now, where it will be mounted read-only with everything else.
        cache = os.path.join(out_root, "_fastembed")
        if os.path.isdir(cache) and os.listdir(cache):
            _log(f"HAVE  {task:11s} {model_id}  (fastembed cache)")
            return {"task": task, "model": model_id, "dir": "_fastembed",
                    "status": "ok", "kind": kind}
        _log(f"WARM  {task:11s} {model_id}  -> _fastembed")
        t0 = time.monotonic()
        try:
            os.makedirs(cache, exist_ok=True)
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            enc = TextCrossEncoder(model_name=model_id, cache_dir=cache)
            list(enc.rerank("warm", ["warm the session so the model is fetched"]))
        except Exception as e:
            _log(f"FAIL  {task:11s} {model_id}: {type(e).__name__}: {e}")
            return {"task": task, "model": model_id, "dir": "",
                    "status": "failed", "error": f"{type(e).__name__}: {e}"}
        _log(f"OK    {task:11s} {model_id}  cached in "
             f"{round(time.monotonic() - t0, 1)}s")
        return {"task": task, "model": model_id, "dir": "_fastembed",
                "status": "ok", "kind": kind}

    if _already_built(target):
        _log(f"HAVE  {task:11s} {model_id}")
        return {"task": task, "model": model_id,
                "dir": os.path.basename(target), "status": "ok", "kind": kind}

    cls_name = _ORT_CLASS.get(kind)
    if not cls_name:
        _log(f"SKIP  {task:11s} {model_id}  (no exporter for kind '{kind}')")
        return {"task": task, "model": model_id, "dir": "",
                "status": "unsupported", "kind": kind}

    _log(f"BUILD {task:11s} {model_id}  -> {os.path.basename(target)}")
    t0 = time.monotonic()
    try:
        import optimum.onnxruntime as ORT
        from transformers import AutoTokenizer
        cls = getattr(ORT, cls_name)
        model = cls.from_pretrained(model_id, export=True)
        tok = AutoTokenizer.from_pretrained(model_id)
        os.makedirs(target, exist_ok=True)
        model.save_pretrained(target)
        tok.save_pretrained(target)
    except Exception as e:
        _log(f"FAIL  {task:11s} {model_id}: {type(e).__name__}: {e}")
        traceback.print_exc()
        return {"task": task, "model": model_id, "dir": "",
                "status": "failed", "error": f"{type(e).__name__}: {e}"}

    dt = round(time.monotonic() - t0, 1)
    size_mb = round(sum(
        os.path.getsize(os.path.join(target, f))
        for f in os.listdir(target)
        if os.path.isfile(os.path.join(target, f))) / 1e6, 1)
    _log(f"OK    {task:11s} {model_id}  {size_mb}MB in {dt}s")
    return {"task": task, "model": model_id, "dir": os.path.basename(target),
            "status": "ok", "kind": kind, "size_mb": size_mb, "export_s": dt}


def cmd_build(args):
    out_root = os.path.abspath(args.out)
    os.makedirs(out_root, exist_ok=True)
    only = {t.strip() for t in (args.only or "").split(",") if t.strip()}

    results = []
    for task, model_id in DEFAULT_MODELS.items():
        if only and task not in only:
            continue
        results.append(build_one(task, model_id, out_root))

    manifest = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "models": results,
    }
    with open(os.path.join(out_root, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)

    ok = [r for r in results if r["status"] == "ok"]
    bad = [r for r in results if r["status"] == "failed"]
    _log(f"DONE  {len(ok)} built/present, {len(bad)} failed, "
         f"{len(results) - len(ok) - len(bad)} skipped")
    for r in bad:
        _log(f"  FAILED: {r['task']} {r['model']}: {r.get('error')}")
    return 1 if bad else 0


def cmd_verify(args):
    """Confirm every registry entry has a loadable directory in the store.

    Loads each model with onnxruntime only — no torch, no network — which is
    exactly what the server will do, so a pass here means the server will start.
    """
    out_root = os.path.abspath(args.out)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    rc = 0
    for task, model_id in DEFAULT_MODELS.items():
        kind = TASK_KIND.get(task, "")
        if kind == "fastembed":
            continue
        target = os.path.join(out_root, model_slug(model_id))
        if not _already_built(target):
            _log(f"MISSING {task:11s} {model_id}")
            rc = 1
            continue
        try:
            import optimum.onnxruntime as ORT
            from transformers import AutoTokenizer
            cls = getattr(ORT, _ORT_CLASS[kind])
            cls.from_pretrained(target)
            AutoTokenizer.from_pretrained(target)
            _log(f"LOADS   {task:11s} {model_id}")
        except Exception as e:
            _log(f"BROKEN  {task:11s} {model_id}: {type(e).__name__}: {e}")
            rc = 1
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build")
    b.add_argument("--out", required=True)
    b.add_argument("--only", default="")
    v = sub.add_parser("verify")
    v.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.cmd == "build":
        return cmd_build(args)
    if args.cmd == "verify":
        return cmd_verify(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
