"""
ml_onnx.py  —  ONNX export + ONNX Runtime serving for Vera ML Workshop modules
================================================================================
Turns a *trained* ML Workshop module (graph in `ml_workshop._MODULES` + weights
from `ml_training`) into a portable `.onnx` artifact served by ONNX Runtime,
exposed as first-class capabilities — so inference no longer needs the NumPy/
PyTorch training stack present.

This is the lead item (§3) of ONNX_TODO.md. It is **purely additive**:
  • New caps only — nothing in ml_workshop / ml_training is modified.
  • `onnx` / `onnxruntime` are *optional*. If absent, the module still loads and
    every cap returns a friendly `{"error": ...}` — Vera is unaffected.

Reference parity
────────────────
The export reproduces `ml_training._forward_with_weights(module, weights, X)`
exactly for the feed-forward subset of node types (dense / mlp / activation /
layer_norm / rms_norm / dropout / add / concat / output). Recurrent / attention /
conv / embedding nodes are detected and politely refused (so we never ship a
mis-exported model). `ml.onnx.verify` proves parity numerically.

Capabilities
────────────
  • ml.export.onnx      — export a trained module → validated `.onnx` artifact
  • ml.onnx.run         — run inference on an artifact via ONNX Runtime
  • ml.onnx.verify      — numeric parity check vs the NumPy reference forward
  • ml.onnx.list        — list exported artifacts
  • ml.onnx.delete      — delete an artifact (+ its auto-registered cap)
  • ml.onnx.model.<slug>— auto-registered per-model cap bound to one artifact

Artifacts live in `ML_ONNX_DIR` (default `<repo>/edge/models`), tying into §4
(edge ORT runtime).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("vera.ml_onnx")

# ── NumPy (required for build/verify; run only needs onnxruntime) ─────────────
try:
    import numpy as np
    HAS_NP = True
except ImportError:
    np = None
    HAS_NP = False

# ── ONNX (optional) ───────────────────────────────────────────────────────────
try:
    import onnx
    from onnx import TensorProto, helper, numpy_helper
    HAS_ONNX = True
except Exception as e:  # pragma: no cover - import guard
    onnx = None
    TensorProto = helper = numpy_helper = None
    HAS_ONNX = False
    log.info("onnx not available (ml.export.onnx disabled): %s", e)

# ── ONNX Runtime (optional) ───────────────────────────────────────────────────
try:
    import onnxruntime as ort
    HAS_ORT = True
except Exception as e:  # pragma: no cover - import guard
    ort = None
    HAS_ORT = False
    log.info("onnxruntime not available (ml.onnx.run disabled): %s", e)

# ── Vera capability framework (optional — mirrors ml_training) ────────────────
try:
    from Vera.vera.capability_orchestration import (
        CAPABILITY_REGISTRY, capability, emit_event, now_iso,
    )
    _CAP_AVAILABLE = True
except ImportError:
    _CAP_AVAILABLE = False
    CAPABILITY_REGISTRY = {}

    def now_iso() -> str:
        from datetime import datetime
        return datetime.utcnow().isoformat() + "Z"

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))   # .../<repo>
ONNX_DIR = os.getenv("ML_ONNX_DIR", os.path.join(_REPO, "edge", "models"))
OPSET = int(os.getenv("ML_ONNX_OPSET", "17"))

# Node types the exporter can faithfully reproduce. Anything else is refused.
SUPPORTED_NODES = {
    "input", "dense", "linear_probe", "mlp", "activation",
    "layer_norm", "rms_norm", "dropout", "add", "residual", "concat", "output",
}

# Execution-provider preference (GPU node → Windows DirectML → CPU).
_PROVIDER_PREFERENCE = [
    "CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider",
]

_SESSIONS: Dict[str, Tuple[float, Any]] = {}   # slug → (mtime, InferenceSession)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _ml_workshop():
    return sys.modules.get("ml_workshop")


def _ml_training():
    return sys.modules.get("ml_training")


def _safe(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]", "_", str(s))


async def _emit(ev: dict):
    if _CAP_AVAILABLE:
        try:
            await emit_event(ev)
        except Exception:
            pass


async def _load_module_and_weights(module_id: str):
    """Returns (module, weights, error). weights are np arrays. Mirrors the
    resolution order used by ml.train.predict / ml.train.evaluate."""
    mw = _ml_workshop()
    if not mw:
        return None, None, {"error": "ml_workshop not loaded"}
    module = getattr(mw, "_MODULES", {}).get(module_id)
    if not module:
        return None, None, {"error": f"module {module_id!r} not found"}

    weights = None
    mt = _ml_training()
    if mt:
        weights = getattr(mt, "_WEIGHTS", {}).get(module_id)
        if not weights and hasattr(mt, "_load_weights"):
            try:
                weights = await mt._load_weights(module_id)
            except Exception as e:
                log.debug("ml_onnx: _load_weights(%s): %s", module_id, e)
        if not weights and hasattr(mt, "_init_weights"):
            # Untrained module — fall back to fresh weights (same as predict).
            weights = mt._init_weights(module)
    return module, (weights or {}), None


def _unsupported_nodes(module: dict) -> List[str]:
    bad = [n.get("type", "") for n in module.get("nodes", [])
           if n.get("type", "") not in SUPPORTED_NODES]
    return sorted(set(bad))


def _topo_order(module: dict):
    """Replicates the topological sort used by the NumPy executors."""
    nodes = {n["id"]: n for n in module.get("nodes", [])}
    nonskip = [e for e in module.get("edges", []) if not e.get("skip")]
    adj = {nid: [] for nid in nodes}
    in_deg = {nid: 0 for nid in nodes}
    for e in nonskip:
        adj[e["from"]].append(e["to"])
        in_deg[e["to"]] = in_deg.get(e["to"], 0) + 1
    queue = [nid for nid, d in in_deg.items() if d == 0]
    order = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for nxt in adj[n]:
            in_deg[nxt] -= 1
            if in_deg[nxt] == 0:
                queue.append(nxt)
    return order, nodes, nonskip


def _infer_in_features(module: dict, weights: dict) -> int:
    for n in module.get("nodes", []):
        w = weights.get(n["id"], {})
        Wm = w.get("W")
        if Wm is not None and getattr(Wm, "ndim", 0) >= 2:
            return int(Wm.shape[0])
        if "W0" in w:
            return int(w["W0"].shape[0])
    for n in module.get("nodes", []):
        if n.get("type") == "input":
            sh = n.get("params", {}).get("shape")
            if sh:
                return int(sh[-1])
    return 8


# ─────────────────────────────────────────────────────────────────────────────
# ONNX GRAPH BUILDER  (parity with ml_training._forward_with_weights)
# ─────────────────────────────────────────────────────────────────────────────

class _Builder:
    """Accumulates ONNX nodes/initializers and emits activations as primitive
    ops so the result matches the NumPy reference bit-for-bit (within dtype)."""

    def __init__(self, onnx_dtype, np_dtype):
        self.onnx_dtype = onnx_dtype
        self.np_dtype = np_dtype
        self.nodes: list = []
        self.inits: list = []
        self._n = 0

    def uid(self, base: str) -> str:
        self._n += 1
        return f"{_safe(base)}__{self._n}"

    def const(self, value, base: str) -> str:
        name = self.uid(base)
        arr = np.asarray(value, dtype=self.np_dtype)
        self.inits.append(numpy_helper.from_array(arr, name))
        return name

    def init(self, arr, base: str) -> str:
        name = self.uid(base)
        self.inits.append(numpy_helper.from_array(np.asarray(arr, dtype=self.np_dtype), name))
        return name

    def node(self, op: str, ins: list, base: str, **attrs) -> str:
        out = self.uid(base)
        self.nodes.append(helper.make_node(op, ins, [out], **attrs))
        return out

    # ── activation, matching ml_training._ACTS exactly ──────────────────────
    def activation(self, act: str, x: str) -> str:
        act = act or "identity"
        if act in ("identity", "linear"):
            return self.node("Identity", [x], "id")
        if act == "relu":
            return self.node("Relu", [x], "relu")
        if act == "sigmoid":
            return self.node("Sigmoid", [x], "sig")
        if act == "tanh":
            return self.node("Tanh", [x], "tanh")
        if act == "softmax":
            # ref adds +1e-9 to the denominator; ONNX Softmax omits it (≈1e-9 diff)
            return self.node("Softmax", [x], "softmax", axis=-1)
        if act in ("swish", "silu"):
            s = self.node("Sigmoid", [x], "swish_sig")
            return self.node("Mul", [x, s], "swish")
        if act == "step":
            zero = self.const(0.0, "zero")
            ge = self.node("GreaterOrEqual", [x, zero], "step_ge")
            return self.node("Cast", [ge], "step", to=self.onnx_dtype)
        if act == "gelu":
            # 0.5*x*(1 + tanh(sqrt(2/pi)*(x + 0.044715*x^3)))
            c0 = self.const(0.044715, "gelu_c0")
            c1 = self.const(float(np.sqrt(2.0 / np.pi)), "gelu_c1")
            half = self.const(0.5, "gelu_half")
            one = self.const(1.0, "gelu_one")
            x2 = self.node("Mul", [x, x], "gelu_x2")
            x3 = self.node("Mul", [x2, x], "gelu_x3")
            inner = self.node("Add", [x, self.node("Mul", [c0, x3], "gelu_c0x3")], "gelu_inner")
            t = self.node("Tanh", [self.node("Mul", [c1, inner], "gelu_scaled")], "gelu_tanh")
            onep = self.node("Add", [one, t], "gelu_1pt")
            return self.node("Mul", [self.node("Mul", [half, x], "gelu_hx"), onep], "gelu")
        # Unknown activation → identity (matches _ACTS default)
        return self.node("Identity", [x], "id")


def _build_model(module: dict, weights: dict, dtype: str) -> Tuple[Any, dict]:
    """Build (and validate) an ONNX model reproducing the reference forward.
    Returns (ModelProto, meta)."""
    if dtype not in ("float32", "float64"):
        dtype = "float32"
    np_dtype = np.float32 if dtype == "float32" else np.float64
    onnx_dtype = TensorProto.FLOAT if dtype == "float32" else TensorProto.DOUBLE

    order, nodes, nonskip = _topo_order(module)
    all_edges = module.get("edges", [])
    b = _Builder(onnx_dtype, np_dtype)

    name_of: Dict[str, str] = {}
    onnx_output: Optional[str] = None
    used_types: set = set()

    def preds(nid: str, include_skip: bool) -> List[str]:
        edges = all_edges if include_skip else nonskip
        return [e["from"] for e in edges if e["to"] == nid and e["from"] in name_of]

    def gather(nid: str) -> str:
        """Single input tensor: graph input if no preds, else concat(axis=-1)."""
        ps = preds(nid, include_skip=False)
        if not ps:
            return "input"
        if len(ps) == 1:
            return name_of[ps[0]]
        return b.node("Concat", [name_of[p] for p in ps], "gconcat", axis=-1)

    for nid in order:
        node = nodes[nid]
        nt = node.get("type", "")
        p = node.get("params", {})
        w = weights.get(nid, {})
        used_types.add(nt)
        act = p.get("activation", p.get("fn", "identity"))

        if nt == "input":
            name_of[nid] = "input"
            continue

        x = gather(nid)

        if nt in ("dense", "linear_probe"):
            Wm = np.asarray(w["W"], dtype=np_dtype)
            bb = np.asarray(w.get("b", np.zeros(Wm.shape[1])), dtype=np_dtype)
            z = b.node("Gemm", [x, b.init(Wm, f"{nid}_W"), b.init(bb, f"{nid}_b")], f"{nid}_gemm")
            name_of[nid] = b.activation(act, z)
            onnx_output = name_of[nid]

        elif nt == "mlp":
            layers = p.get("layers", [64, 64])
            h = x
            for i in range(len(layers) - 1):
                Wi = np.asarray(w[f"W{i}"], dtype=np_dtype)
                bi = np.asarray(w.get(f"b{i}", np.zeros(layers[i + 1])), dtype=np_dtype)
                z = b.node("Gemm", [h, b.init(Wi, f"{nid}_W{i}"), b.init(bi, f"{nid}_b{i}")], f"{nid}_gemm{i}")
                h = b.activation(act, z)
            name_of[nid] = h
            onnx_output = h

        elif nt == "activation":
            name_of[nid] = b.activation(act, x)
            onnx_output = name_of[nid]

        elif nt in ("layer_norm", "rms_norm"):
            dim = w.get("gamma")
            gamma = np.asarray(w.get("gamma", np.ones(1)), dtype=np_dtype)
            beta = np.asarray(w.get("beta", np.zeros(1)), dtype=np_dtype)
            g = b.init(gamma, f"{nid}_gamma")
            bt = b.init(beta, f"{nid}_beta")
            if nt == "rms_norm":
                sq = b.node("Mul", [x, x], f"{nid}_sq")
                ms = b.node("ReduceMean", [sq], f"{nid}_ms", axes=[-1], keepdims=1)
                msb = b.node("Add", [ms, b.const(1e-8, f"{nid}_eps")], f"{nid}_msb")
                rms = b.node("Sqrt", [msb], f"{nid}_rms")
                xn = b.node("Div", [x, rms], f"{nid}_xn")
            else:
                mu = b.node("ReduceMean", [x], f"{nid}_mu", axes=[-1], keepdims=1)
                xc = b.node("Sub", [x, mu], f"{nid}_xc")
                sq = b.node("Mul", [xc, xc], f"{nid}_sq")
                var = b.node("ReduceMean", [sq], f"{nid}_var", axes=[-1], keepdims=1)
                std = b.node("Add", [b.node("Sqrt", [var], f"{nid}_std0"),
                                     b.const(1e-5, f"{nid}_eps")], f"{nid}_std")
                xn = b.node("Div", [xc, std], f"{nid}_xn")
            scaled = b.node("Mul", [xn, g], f"{nid}_scaled")
            name_of[nid] = b.node("Add", [scaled, bt], f"{nid}_out")
            onnx_output = name_of[nid]

        elif nt == "dropout":
            name_of[nid] = b.node("Identity", [x], f"{nid}_drop")
            onnx_output = name_of[nid]

        elif nt in ("add", "residual"):
            ps = preds(nid, include_skip=True)
            ins = [name_of[pp] for pp in ps] or [x]
            name_of[nid] = b.node("Sum", ins, f"{nid}_add") if len(ins) >= 2 \
                else b.node("Identity", ins, f"{nid}_add")
            onnx_output = name_of[nid]

        elif nt == "concat":
            ps = preds(nid, include_skip=True)
            ins = [name_of[pp] for pp in ps] or [x]
            axis = int(p.get("dim", -1))
            name_of[nid] = b.node("Concat", ins, f"{nid}_cat", axis=axis) if len(ins) >= 2 \
                else b.node("Identity", ins, f"{nid}_cat")
            onnx_output = name_of[nid]

        elif nt == "output":
            name_of[nid] = b.activation(p.get("activation", "identity"), x)
            onnx_output = name_of[nid]

        else:
            # Should never happen — caller checks _unsupported_nodes first.
            raise ValueError(f"unsupported node type for ONNX export: {nt!r}")

    if onnx_output is None:
        raise ValueError("module has no exportable output-producing node")

    inp = helper.make_tensor_value_info("input", onnx_dtype, ["batch", "features"])
    # Supported subset always yields a rank-2 (batch, features) output; a
    # symbolic shape keeps the checker happy without constraining batch size.
    out = helper.make_tensor_value_info(onnx_output, onnx_dtype, ["batch", "out"])
    graph = helper.make_graph(b.nodes, _safe(module.get("id", "vera_module")),
                              [inp], [out], b.inits)
    model = helper.make_model(
        graph, opset_imports=[helper.make_operatorsetid("", OPSET)],
        producer_name="vera-ml-onnx",
    )
    # Newer onnx stamps a higher IR version than older onnxruntime builds accept
    # (e.g. onnx 1.22 → IR 13, but ORT <1.18 maxes at IR 10). Pin a conservative
    # IR so artifacts load across the cluster's mixed ORT versions.
    model.ir_version = int(os.getenv("ML_ONNX_IR_VERSION", "10"))

    checker_warning = None
    try:
        onnx.checker.check_model(model)
    except Exception as e:
        checker_warning = str(e)
        log.warning("ml_onnx: checker warning for %s: %s", module.get("id"), e)

    meta = {
        "dtype": dtype,
        "opset": OPSET,
        "node_count": len(b.nodes),
        "node_types": sorted(used_types),
        "output_name": onnx_output,
        "checker_warning": checker_warning,
    }
    return model, meta


# ─────────────────────────────────────────────────────────────────────────────
# ARTIFACT I/O + SESSIONS
# ─────────────────────────────────────────────────────────────────────────────

def _paths(slug: str) -> Tuple[str, str]:
    return (os.path.join(ONNX_DIR, slug + ".onnx"),
            os.path.join(ONNX_DIR, slug + ".json"))


def _save_artifact(module_id: str, model, meta: dict) -> Tuple[str, str]:
    os.makedirs(ONNX_DIR, exist_ok=True)
    slug = _safe(module_id)
    onnx_path, meta_path = _paths(slug)
    onnx.save(model, onnx_path)
    meta = {**meta, "slug": slug, "module_id": module_id,
            "created": now_iso(), "onnx": os.path.basename(onnx_path)}
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return onnx_path, slug


def _get_session(slug: str):
    onnx_path, _ = _paths(slug)
    if not os.path.exists(onnx_path):
        return None
    mtime = os.path.getmtime(onnx_path)
    cached = _SESSIONS.get(slug)
    if cached and cached[0] == mtime:
        return cached[1]
    avail = set(ort.get_available_providers())
    provs = [p for p in _PROVIDER_PREFERENCE if p in avail] or None
    sess = ort.InferenceSession(onnx_path, providers=provs)
    _SESSIONS[slug] = (mtime, sess)
    return sess


def _run_session_array(slug: str, Xarr):
    sess = _get_session(slug)
    if sess is None:
        return None, None
    inp = sess.get_inputs()[0]
    np_in = np.float64 if "double" in inp.type else np.float32
    out = sess.run(None, {inp.name: np.asarray(Xarr, dtype=np_in)})
    return out[0], (sess.get_providers()[0] if sess.get_providers() else None)


async def _run_artifact(slug_or_id: str, X) -> dict:
    if not HAS_ORT:
        return {"error": "onnxruntime not installed", "hint": "pip install onnxruntime"}
    if not HAS_NP:
        return {"error": "NumPy required"}
    slug = _safe(slug_or_id)
    onnx_path, _ = _paths(slug)
    if not os.path.exists(onnx_path):
        return {"error": f"no ONNX artifact for {slug_or_id!r}; run ml.export.onnx first"}
    try:
        raw = json.loads(X) if isinstance(X, str) else X
        Xarr = np.asarray(raw, dtype=np.float32)
    except Exception as e:
        return {"error": f"X JSON parse: {e}"}
    if Xarr.ndim == 1:
        Xarr = Xarr[None, :]
    try:
        out, provider = _run_session_array(slug, Xarr)
    except Exception as e:
        return {"error": f"onnxruntime inference failed: {e}"}
    return {"ok": True, "artifact": slug, "provider": provider,
            "predictions": out.tolist(), "shape": list(out.shape)}


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-REGISTERED PER-MODEL CAPS
# ─────────────────────────────────────────────────────────────────────────────

def _make_runner(slug: str):
    async def _runner(X: str = "", trace_id=None):
        return await _run_artifact(slug, X)
    return _runner


def _register_model_cap(slug: str, module_id: str, live_mount: bool = False) -> Optional[str]:
    """Register `ml.onnx.model.<slug>` bound to one artifact. Import-time calls
    get HTTP routes from the normal lifespan mount pass; runtime calls pass
    live_mount=True to add the route immediately (best-effort)."""
    if not _CAP_AVAILABLE:
        return None
    cap_name = f"ml.onnx.model.{slug}"
    path = f"/ml/onnx/model/{slug}"
    if cap_name not in CAPABILITY_REGISTRY:
        capability(
            cap_name, http_method="POST", http_path=path,
            http_tags=["ml", "onnx", "model"], memory="on",
            description=(f"Run the exported ONNX model {module_id!r} via ONNX "
                         f"Runtime. Input: X (JSON list). Output: {{predictions}}."),
        )(_make_runner(slug))
    if live_mount:
        try:
            import Vera.vera.capability_orchestration as _orch
            app = getattr(_orch, "APP", None)
            mk = getattr(_orch, "_make_post_handler", None)
            if app and mk:
                app.add_api_route(path, mk(CAPABILITY_REGISTRY[cap_name], cap_name),
                                  methods=["POST"], tags=["ml", "onnx"],
                                  summary=cap_name)
        except Exception as e:
            log.debug("ml_onnx: live route mount for %s skipped: %s", cap_name, e)
    return cap_name


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    @capability(
        "ml.export.onnx",
        http_method="POST", http_path="/ml/export/onnx", http_tags=["ml", "onnx"],
        memory="on",
        description=(
            "Export a trained ML Workshop module to a portable ONNX artifact "
            "served via ONNX Runtime. Input: module_id (str!), dtype "
            "(float32|float64), register_cap (bool). Output: {ok, artifact, "
            "path, cap_name, unsupported}."
        ),
        schema={"properties": {"dtype": {"enum": ["float32", "float64"]}}},
    )
    async def cap_export_onnx(module_id: str = "", dtype: str = "float32",
                              register_cap: bool = True, trace_id=None):
        if not module_id:
            return {"error": "module_id is required"}
        if not HAS_NP:
            return {"error": "NumPy required"}
        if not HAS_ONNX:
            return {"error": "onnx not installed", "hint": "pip install onnx onnxruntime"}

        module, weights, err = await _load_module_and_weights(module_id)
        if err:
            return err

        unsupported = _unsupported_nodes(module)
        if unsupported:
            return {"error": "module contains node types the ONNX exporter does "
                             "not yet support",
                    "unsupported": unsupported,
                    "supported": sorted(SUPPORTED_NODES)}

        await _emit({"type": "ml.onnx.progress", "stage": "build",
                     "message": f"exporting {module_id} → ONNX"})
        try:
            model, meta = _build_model(module, weights, dtype)
            onnx_path, slug = _save_artifact(module_id, model, meta)
        except Exception as e:
            log.error("ml_onnx export failed for %s: %s", module_id, e)
            return {"error": f"onnx export failed: {e}"}

        cap_name = None
        if register_cap:
            cap_name = _register_model_cap(slug, module_id, live_mount=True)
            # stamp cap_name into the manifest so a restart re-registers it
            try:
                _, meta_path = _paths(slug)
                with open(meta_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                saved["cap_name"] = cap_name
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(saved, f, indent=2)
            except Exception:
                pass

        await _emit({"type": "ml.onnx.progress", "stage": "done",
                     "message": f"exported {os.path.basename(onnx_path)}",
                     "cap": cap_name})
        return {"ok": True, "module_id": module_id, "artifact": slug,
                "path": onnx_path, "dtype": meta["dtype"], "opset": meta["opset"],
                "node_count": meta["node_count"], "node_types": meta["node_types"],
                "checker_warning": meta["checker_warning"], "cap_name": cap_name}

    @capability(
        "ml.onnx.run",
        http_method="POST", http_path="/ml/onnx/run", http_tags=["ml", "onnx"],
        memory="on",
        description=("Run inference on an exported ONNX artifact via ONNX "
                     "Runtime. Input: artifact (str! module id/slug), X (JSON "
                     "list). Output: {ok, predictions, provider, shape}."),
    )
    async def cap_onnx_run(artifact: str = "", X: str = "", trace_id=None):
        if not artifact:
            return {"error": "artifact is required"}
        return await _run_artifact(artifact, X)

    @capability(
        "ml.onnx.verify",
        http_method="POST", http_path="/ml/onnx/verify", http_tags=["ml", "onnx"],
        memory="on",
        description=("Numerically verify an ONNX artifact reproduces the NumPy "
                     "reference forward. Input: module_id (str!), X (JSON list, "
                     "optional), n (int), tol (float). Output: {ok, max_abs_diff, "
                     "passed}."),
    )
    async def cap_onnx_verify(module_id: str = "", X: str = "", n: int = 4,
                              tol: float = 1e-3, trace_id=None):
        if not module_id:
            return {"error": "module_id is required"}
        if not HAS_NP:
            return {"error": "NumPy required"}
        if not HAS_ORT:
            return {"error": "onnxruntime not installed", "hint": "pip install onnxruntime"}
        module, weights, err = await _load_module_and_weights(module_id)
        if err:
            return err
        mt = _ml_training()
        if not mt or not hasattr(mt, "_forward_with_weights"):
            return {"error": "ml_training reference forward unavailable"}
        slug = _safe(module_id)
        onnx_path, _ = _paths(slug)
        if not os.path.exists(onnx_path):
            return {"error": "no ONNX artifact; run ml.export.onnx first"}

        try:
            if X:
                Xarr = np.asarray(json.loads(X), dtype=np.float32)
                if Xarr.ndim == 1:
                    Xarr = Xarr[None, :]
            else:
                feat = _infer_in_features(module, weights)
                Xarr = np.random.default_rng(0).standard_normal(
                    (max(1, int(n)), feat)).astype(np.float32)
        except Exception as e:
            return {"error": f"X JSON parse: {e}"}

        try:
            ref, _ = mt._forward_with_weights(module, weights, Xarr)
            ref = np.asarray(ref, dtype=np.float64)
            ortout, provider = _run_session_array(slug, Xarr)
            ortout = np.asarray(ortout, dtype=np.float64)
        except Exception as e:
            return {"error": f"verify run failed: {e}"}

        shapes_match = ref.shape == ortout.shape
        diff = float(np.max(np.abs(ref - ortout))) if shapes_match else None
        passed = bool(shapes_match and diff is not None and diff <= float(tol))
        return {"ok": True, "module_id": module_id, "passed": passed,
                "max_abs_diff": diff, "tol": float(tol), "provider": provider,
                "ref_shape": list(ref.shape), "onnx_shape": list(ortout.shape),
                "shapes_match": shapes_match}

    @capability(
        "ml.onnx.list",
        http_method="GET", http_path="/ml/onnx/list", http_tags=["ml", "onnx"],
        memory="off", silent=True,
        description="List exported ONNX artifacts. Output: {artifacts:[...], dir}.",
    )
    async def cap_onnx_list(trace_id=None):
        items = []
        if os.path.isdir(ONNX_DIR):
            for fn in sorted(os.listdir(ONNX_DIR)):
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(ONNX_DIR, fn), "r", encoding="utf-8") as f:
                        m = json.load(f)
                    onnx_path, _ = _paths(m.get("slug", os.path.splitext(fn)[0]))
                    items.append({
                        "slug": m.get("slug"), "module_id": m.get("module_id"),
                        "dtype": m.get("dtype"), "created": m.get("created"),
                        "node_count": m.get("node_count"), "cap_name": m.get("cap_name"),
                        "size_kb": round(os.path.getsize(onnx_path) / 1024, 1)
                        if os.path.exists(onnx_path) else None,
                    })
                except Exception:
                    pass
        return {"artifacts": items, "count": len(items), "dir": ONNX_DIR,
                "onnx": HAS_ONNX, "onnxruntime": HAS_ORT,
                "providers": list(ort.get_available_providers()) if HAS_ORT else []}

    @capability(
        "ml.onnx.delete",
        http_method="POST", http_path="/ml/onnx/delete", http_tags=["ml", "onnx"],
        memory="on",
        description="Delete an exported ONNX artifact and its cap. Input: artifact (str!).",
    )
    async def cap_onnx_delete(artifact: str = "", trace_id=None):
        if not artifact:
            return {"error": "artifact is required"}
        slug = _safe(artifact)
        onnx_path, meta_path = _paths(slug)
        removed = []
        for pth in (onnx_path, meta_path):
            if os.path.exists(pth):
                try:
                    os.remove(pth)
                    removed.append(os.path.basename(pth))
                except Exception as e:
                    return {"error": f"could not delete {pth}: {e}"}
        _SESSIONS.pop(slug, None)
        CAPABILITY_REGISTRY.pop(f"ml.onnx.model.{slug}", None)
        return {"ok": True, "artifact": slug, "removed": removed}

    # ── Re-register per-model caps for artifacts that already exist on disk ───
    # Runs at import (before the lifespan route-mount pass) so restored caps get
    # their HTTP routes mounted normally.
    def _rescan_and_register():
        if not os.path.isdir(ONNX_DIR):
            return 0
        n = 0
        for fn in os.listdir(ONNX_DIR):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(ONNX_DIR, fn), "r", encoding="utf-8") as f:
                    m = json.load(f)
                if m.get("cap_name"):
                    _register_model_cap(m.get("slug", os.path.splitext(fn)[0]),
                                        m.get("module_id", ""), live_mount=False)
                    n += 1
            except Exception:
                pass
        return n

    try:
        _restored = _rescan_and_register()
        log.info("ml_onnx ready — onnx=%s ort=%s dir=%s restored_model_caps=%d",
                 HAS_ONNX, HAS_ORT, ONNX_DIR, _restored)
    except Exception as e:
        log.debug("ml_onnx rescan skipped: %s", e)
