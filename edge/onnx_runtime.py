"""
onnx_runtime.py  —  Edge ONNX Runtime model server   (§4 of ONNX_TODO.md)
==========================================================================
Serves the `.onnx` artifacts produced by Vera's `ml.export.onnx`
(`vera/machine learning/ml_onnx.py`), which land in `edge/models/`.

ONNX Runtime is the edge runtime: a small binary, CPU-capable, int8-quantizable,
and execution-provider aware. This process picks the best provider available on
the node it runs on:

    CUDAExecutionProvider  →  DmlExecutionProvider  →  CPUExecutionProvider

so the same artifact accelerates on the CUDA GPU node, on the Windows host via
DirectML, or runs lean on the CPU nodes — no code change.

This is intentionally decoupled from the Vera orchestrator: it only needs
`onnxruntime` (and `onnx` for quantization). It shares the `edge/models/`
directory with the server-side exporter, so the server exports and any edge node
serves.

HTTP (optional — needs fastapi/uvicorn, already present for GPU_inference.py):
  GET  /health             — providers, loaded models
  GET  /models             — artifacts discovered in MODELS_DIR
  POST /run/{slug}         — body {"X": [[...]]}  → {predictions, provider}
  POST /quantize/{slug}    — produce an int8 dynamically-quantized copy

CLI:
  python onnx_runtime.py serve   [--host 0.0.0.0 --port 8770]
  python onnx_runtime.py list
  python onnx_runtime.py run      <slug> --x "[[...]]"
  python onnx_runtime.py quantize <slug>
  python onnx_runtime.py bench    <slug> [--n 1000]
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    import onnxruntime as ort
    HAS_ORT = True
except Exception as e:  # pragma: no cover
    ort = None
    HAS_ORT = False
    print(f"[onnx_runtime] onnxruntime unavailable: {e}")

# ── Config ────────────────────────────────────────────────────────────────────

_HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.getenv("ML_ONNX_DIR", os.path.join(_HERE, "models"))

_PROVIDER_PREFERENCE = [
    "CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider",
]

_SESSIONS: Dict[str, Tuple[float, Any]] = {}   # slug → (mtime, InferenceSession)


# ── Provider / session management ─────────────────────────────────────────────

def select_providers() -> List[str]:
    """Highest-preference execution providers available on this node."""
    if not HAS_ORT:
        return []
    avail = set(ort.get_available_providers())
    return [p for p in _PROVIDER_PREFERENCE if p in avail] or ["CPUExecutionProvider"]


def _model_path(slug: str) -> str:
    return os.path.join(MODELS_DIR, slug + ".onnx")


def load_session(slug: str):
    """Cached InferenceSession for `slug`, rebuilt if the file changed."""
    if not HAS_ORT:
        raise RuntimeError("onnxruntime not installed")
    path = _model_path(slug)
    if not os.path.exists(path):
        raise FileNotFoundError(f"no ONNX artifact for {slug!r} in {MODELS_DIR}")
    mtime = os.path.getmtime(path)
    cached = _SESSIONS.get(slug)
    if cached and cached[0] == mtime:
        return cached[1]
    sess = ort.InferenceSession(path, providers=select_providers())
    _SESSIONS[slug] = (mtime, sess)
    return sess


def run_model(slug: str, X) -> Dict[str, Any]:
    """Run inference. X is a list/array; returns {predictions, provider, shape}."""
    sess = load_session(slug)
    inp = sess.get_inputs()[0]
    np_in = np.float64 if "double" in inp.type else np.float32
    arr = np.asarray(X, dtype=np_in)
    if arr.ndim == 1:
        arr = arr[None, :]
    out = sess.run(None, {inp.name: arr})[0]
    return {
        "slug": slug,
        "provider": sess.get_providers()[0] if sess.get_providers() else None,
        "predictions": np.asarray(out).tolist(),
        "shape": list(np.asarray(out).shape),
    }


# ── Registry ──────────────────────────────────────────────────────────────────

def list_models() -> List[dict]:
    items: List[dict] = []
    if not os.path.isdir(MODELS_DIR):
        return items
    for fn in sorted(os.listdir(MODELS_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(MODELS_DIR, fn), "r", encoding="utf-8") as f:
                m = json.load(f)
            slug = m.get("slug", os.path.splitext(fn)[0])
            path = _model_path(slug)
            items.append({
                "slug": slug,
                "module_id": m.get("module_id"),
                "dtype": m.get("dtype"),
                "opset": m.get("opset"),
                "created": m.get("created"),
                "size_kb": round(os.path.getsize(path) / 1024, 1) if os.path.exists(path) else None,
                "quantized": os.path.exists(_model_path(slug + ".int8")),
            })
        except Exception:
            pass
    return items


# ── int8 dynamic quantization ─────────────────────────────────────────────────

def quantize_model(slug: str) -> Dict[str, Any]:
    """Produce a dynamically int8-quantized copy `<slug>.int8.onnx`.

    Dynamic quantization needs no calibration data — weights become int8, which
    typically shrinks the model ~4x and speeds up CPU inference, at a small
    accuracy cost. Good fit for the CPU/edge nodes.
    """
    try:
        from onnxruntime.quantization import QuantType, quantize_dynamic
    except Exception as e:
        return {"error": f"onnxruntime.quantization unavailable: {e}"}
    src = _model_path(slug)
    if not os.path.exists(src):
        return {"error": f"no ONNX artifact for {slug!r}"}
    dst = _model_path(slug + ".int8")
    try:
        quantize_dynamic(src, dst, weight_type=QuantType.QInt8)
    except Exception as e:
        return {"error": f"quantize failed: {e}"}
    _SESSIONS.pop(slug + ".int8", None)
    return {
        "ok": True, "slug": slug, "int8_slug": slug + ".int8",
        "src_kb": round(os.path.getsize(src) / 1024, 1),
        "int8_kb": round(os.path.getsize(dst) / 1024, 1),
    }


def benchmark(slug: str, n: int = 1000) -> Dict[str, Any]:
    sess = load_session(slug)
    inp = sess.get_inputs()[0]
    # Build a dummy batch using the declared feature dim if static, else 8.
    feat = 8
    try:
        dims = inp.shape
        if len(dims) >= 2 and isinstance(dims[-1], int):
            feat = dims[-1]
    except Exception:
        pass
    np_in = np.float64 if "double" in inp.type else np.float32
    X = np.random.default_rng(0).standard_normal((1, feat)).astype(np_in)
    feed = {inp.name: X}
    sess.run(None, feed)  # warmup
    t0 = time.perf_counter()
    for _ in range(n):
        sess.run(None, feed)
    dt = time.perf_counter() - t0
    return {"slug": slug, "provider": sess.get_providers()[0], "n": n,
            "total_s": round(dt, 4), "per_call_ms": round(dt / n * 1000, 4)}


# ── Optional HTTP server ──────────────────────────────────────────────────────

def build_app():
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel

    app = FastAPI(title="Vera Edge ONNX Runtime", version="1.0.0")

    class RunReq(BaseModel):
        X: list

    @app.get("/health")
    async def health():
        return {"status": "ok", "onnxruntime": HAS_ORT,
                "providers": list(ort.get_available_providers()) if HAS_ORT else [],
                "selected": select_providers(), "models_dir": MODELS_DIR,
                "models": len(list_models())}

    @app.get("/models")
    async def models():
        return {"models": list_models(), "dir": MODELS_DIR}

    @app.post("/run/{slug}")
    async def run(slug: str, req: RunReq):
        try:
            return run_model(slug, req.X)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/quantize/{slug}")
    async def quantize(slug: str):
        res = quantize_model(slug)
        if res.get("error"):
            raise HTTPException(status_code=400, detail=res["error"])
        return res

    return app


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main():
    ap = argparse.ArgumentParser(description="Vera edge ONNX Runtime server/CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the HTTP server")
    s.add_argument("--host", default=os.getenv("ONNX_HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.getenv("ONNX_PORT", "8770")))

    sub.add_parser("list", help="list artifacts")

    r = sub.add_parser("run", help="run inference")
    r.add_argument("slug")
    r.add_argument("--x", required=True, help="JSON input, e.g. '[[1,2,3,4]]'")

    q = sub.add_parser("quantize", help="int8-quantize an artifact")
    q.add_argument("slug")

    b = sub.add_parser("bench", help="benchmark an artifact")
    b.add_argument("slug")
    b.add_argument("--n", type=int, default=1000)

    args = ap.parse_args()

    if args.cmd == "serve":
        import uvicorn
        print(f"[onnx_runtime] providers={select_providers()} dir={MODELS_DIR}")
        uvicorn.run(build_app(), host=args.host, port=args.port)
    elif args.cmd == "list":
        print(json.dumps(list_models(), indent=2))
    elif args.cmd == "run":
        print(json.dumps(run_model(args.slug, json.loads(args.x)), indent=2))
    elif args.cmd == "quantize":
        print(json.dumps(quantize_model(args.slug), indent=2))
    elif args.cmd == "bench":
        print(json.dumps(benchmark(args.slug, args.n), indent=2))


if __name__ == "__main__":
    _main()
