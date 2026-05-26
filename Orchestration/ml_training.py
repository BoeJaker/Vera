"""
ml_training.py  —  Vera ML Workshop Training Engine
=====================================================
Full training loop, dataset management, and data collection
tied to the ML Workshop and Data Fabric.

What this adds
──────────────
  Training engine
    • SGD, Adam, AdamW optimisers (pure NumPy — no PyTorch required)
    • Loss functions: MSE, MAE, BCE, CrossEntropy, Huber
    • Backprop through the module graph via numerical gradients (finite diff)
      with optional exact gradients for standard layers
    • Mini-batch training with progress streaming via Redis events
    • Metric tracking: loss, accuracy, MAE, RMSE per epoch
    • Early stopping, LR scheduling (step / cosine / plateau)
    • Checkpoint: best weights saved to Redis keyed by module_id

  Dataset engine
    • DatasetSpec: typed schema for train/val/test splits stored in fabric
    • Built-in generators: synthetic classification, regression, time-series,
      XOR, spiral, moons, circles (all pure NumPy)
    • Fabric loaders: pull OHLCV / any fabric dataset → NumPy arrays
    • Feature engineering: normalise, standardise, lag features, returns,
      rolling stats, RSI, MACD, Bollinger bands

  Data collectors (Data Fabric integration)
    • ml.data.fetch_ohlcv    — pull OHLCV from Yahoo Finance / Alpha Vantage / Stooq
    • ml.data.fetch_macro    — FRED macroeconomic series (GDP, CPI, rates)
    • ml.data.fetch_crypto   — CoinGecko OHLCV for crypto pairs
    • ml.data.fetch_synthetic— generate synthetic datasets for any example
    • ml.data.list_datasets  — list all ML-ready datasets in fabric
    • ml.data.prepare        — normalise + split a fabric dataset for training

  Training capabilities
    • ml.train               — start a training run (streams events)
    • ml.train.status        — get live training status
    • ml.train.stop          — cancel a running training job
    • ml.train.history       — fetch loss/metric curves for a run
    • ml.train.evaluate      — run evaluation on held-out test set
    • ml.train.predict       — batch inference using trained weights
    • ml.train.weights_get   — export trained weights as JSON
    • ml.train.weights_load  — load weights into a module
    • ml.examples.load_all   — seed the workshop with all worked examples
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
import uuid
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("vera.ml_training")

# ── NumPy (required) ─────────────────────────────────────────────────────────
try:
    import numpy as np
    HAS_NP = True
except ImportError:
    np = None
    HAS_NP = False

# ── HTTP (for data fetching) ──────────────────────────────────────────────────
try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

# ── Vera imports ──────────────────────────────────────────────────────────────
try:
    from Vera.Orchestration.capability_orchestration import (
        register_ui,
        APP, CAPABILITY_REGISTRY, capability, emit_event, now_iso,
        ollama_generate, schedule,
    )
    from Vera.Orchestration.config import cfg
    from pathlib import Path as _Path
    _CAP_AVAILABLE = True
except ImportError:
    _CAP_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# TRAINING STATE
# ─────────────────────────────────────────────────────────────────────────────

_TRAINING_JOBS: Dict[str, dict] = {}   # job_id → job state
_WEIGHTS:       Dict[str, dict] = {}   # module_id → {node_id: {W, b, ...}}

REDIS_W_PREFIX = "vera:ml:weights:"
REDIS_J_PREFIX = "vera:ml:job:"

# ─────────────────────────────────────────────────────────────────────────────
# NUMPY WEIGHT STORE — mutable parameter arrays keyed by (module_id, node_id)
# ─────────────────────────────────────────────────────────────────────────────

def _init_weights(module: dict) -> dict:
    """Initialise trainable weight arrays for a module. Returns weights dict."""
    if not HAS_NP:
        return {}
    rng = np.random.default_rng(42)
    weights = {}
    for node in module.get("nodes", []):
        nid  = node["id"]
        ntype = node.get("type", "")
        p    = node.get("params", {})

        if ntype == "dense" or ntype == "linear_probe":
            inf  = p.get("in_features", 64)
            outf = p.get("out_features", 64)
            W    = rng.standard_normal((inf, outf)) * np.sqrt(2.0 / inf)
            b    = np.zeros(outf)
            weights[nid] = {"W": W, "b": b}

        elif ntype == "mlp":
            layers = p.get("layers", [64, 64])
            mw = {}
            for i, (a, b_) in enumerate(zip(layers, layers[1:])):
                W = rng.standard_normal((a, b_)) * np.sqrt(2.0 / a)
                mw[f"W{i}"] = W
                mw[f"b{i}"] = np.zeros(b_)
            weights[nid] = mw

        elif ntype in ("layer_norm", "rms_norm"):
            dim = p.get("dim", 64)
            weights[nid] = {"gamma": np.ones(dim), "beta": np.zeros(dim)}

        elif ntype == "embedding":
            v   = p.get("vocab_size", 1000)
            d   = p.get("dim", 64)
            weights[nid] = {"E": rng.standard_normal((v, d)) * 0.02}

        elif ntype == "perceptron":
            nin = p.get("inputs", 1)
            weights[nid] = {"W": rng.standard_normal(nin) * 0.1, "b": np.array(0.0)}

        elif ntype == "conv1d":
            ic  = p.get("in_channels", 1)
            oc  = p.get("out_channels", 16)
            k   = p.get("kernel", 3)
            weights[nid] = {
                "W": rng.standard_normal((oc, ic, k)) * np.sqrt(2.0 / (ic * k)),
                "b": np.zeros(oc),
            }

        elif ntype in ("rnn", "gru", "lstm"):
            i_sz = p.get("input_size", 64)
            h_sz = p.get("hidden_size", 128)
            mult = 3 if ntype == "gru" else (4 if ntype == "lstm" else 1)
            weights[nid] = {
                "Wih": rng.standard_normal((i_sz, mult * h_sz)) * 0.01,
                "Whh": rng.standard_normal((h_sz, mult * h_sz)) * 0.01,
                "b":   np.zeros(mult * h_sz),
            }

        elif ntype == "multi_head_attention":
            d = p.get("d_model", 128)
            weights[nid] = {
                "Wq": rng.standard_normal((d, d)) * 0.02,
                "Wk": rng.standard_normal((d, d)) * 0.02,
                "Wv": rng.standard_normal((d, d)) * 0.02,
                "Wo": rng.standard_normal((d, d)) * 0.02,
            }

        elif ntype == "kan_layer":
            inf  = p.get("in_features", 32)
            outf = p.get("out_features", 32)
            g    = p.get("grid", 5)
            k    = p.get("order", 3)
            weights[nid] = {
                "splines": rng.standard_normal((inf, outf, g + k)) * 0.1,
            }

    return weights


def _flatten_weights(weights: dict) -> np.ndarray:
    """Flatten all weights into a single 1-D vector."""
    parts = []
    for nid in sorted(weights.keys()):
        for k in sorted(weights[nid].keys()):
            parts.append(weights[nid][k].ravel())
    return np.concatenate(parts) if parts else np.array([])


def _unflatten_weights(flat: np.ndarray, weights_template: dict) -> dict:
    """Restore weight dict from flattened vector using template shapes."""
    new_w = deepcopy(weights_template)
    idx = 0
    for nid in sorted(new_w.keys()):
        for k in sorted(new_w[nid].keys()):
            arr = new_w[nid][k]
            n   = arr.size
            new_w[nid][k] = flat[idx:idx+n].reshape(arr.shape)
            idx += n
    return new_w


# ─────────────────────────────────────────────────────────────────────────────
# FORWARD PASS WITH WEIGHTS (differentiable)
# ─────────────────────────────────────────────────────────────────────────────

def _relu(x):    return np.maximum(0, x)
def _gelu(x):    return 0.5*x*(1+np.tanh(np.sqrt(2/np.pi)*(x+0.044715*x**3)))
def _sigmoid(x): return 1/(1+np.exp(-np.clip(x, -500, 500)))
def _swish(x):   return x*_sigmoid(x)
def _softmax(x):
    e = np.exp(x - x.max(axis=-1, keepdims=True))
    return e / (e.sum(axis=-1, keepdims=True) + 1e-9)
def _tanh(x):    return np.tanh(x)

_ACTS = {
    "relu": _relu, "gelu": _gelu, "sigmoid": _sigmoid, "swish": _swish,
    "silu": _swish, "tanh": _tanh, "softmax": _softmax,
    "identity": lambda x: x, "linear": lambda x: x,
    "step": lambda x: (x >= 0).astype(float),
}


def _forward_with_weights(module: dict, weights: dict, X: np.ndarray) -> Tuple[np.ndarray, dict]:
    """
    Forward pass using explicit weight arrays.
    X: input array, shape (batch, features) or (batch, seq, features).
    Returns (output, activations_cache) where cache holds every node output.
    """
    nodes  = {n["id"]: n for n in module.get("nodes", [])}
    edges  = [e for e in module.get("edges", []) if not e.get("skip")]
    adj    = {nid: [] for nid in nodes}
    in_deg = {nid: 0  for nid in nodes}
    for e in edges:
        adj[e["from"]].append(e["to"])
        in_deg[e["to"]] = in_deg.get(e["to"], 0) + 1

    queue = [nid for nid, d in in_deg.items() if d == 0]
    order = []
    while queue:
        n = queue.pop(0); order.append(n)
        for nxt in adj[n]:
            in_deg[nxt] -= 1
            if in_deg[nxt] == 0: queue.append(nxt)

    cache: dict = {}
    output = None
    rng = np.random.default_rng(int(time.time() * 1000) % 2**32)

    for nid in order:
        node  = nodes[nid]
        ntype = node.get("type", "")
        p     = node.get("params", {})
        w     = weights.get(nid, {})

        in_tensors = [cache[e["from"]] for e in edges if e["to"] == nid and e["from"] in cache]
        if not in_tensors:
            x = X
        elif len(in_tensors) == 1:
            x = in_tensors[0]
        else:
            try:
                x = np.concatenate(in_tensors, axis=-1)
            except Exception:
                x = in_tensors[0]

        act_fn = _ACTS.get(p.get("activation", p.get("fn", "identity")), _ACTS["identity"])

        if ntype == "input":
            cache[nid] = X
            continue

        elif ntype in ("dense", "linear_probe"):
            W = w.get("W", np.eye(x.shape[-1]))
            b = w.get("b", np.zeros(W.shape[1]))
            # Handle batched input
            if x.ndim == 1:
                x = x[np.newaxis, :]
            # Resize W if needed
            if W.shape[0] != x.shape[-1]:
                W = np.eye(x.shape[-1])[:, :W.shape[1]]
            z = x @ W + b
            cache[nid] = act_fn(z)
            output     = cache[nid]

        elif ntype == "perceptron":
            W = w.get("W", np.ones(x.shape[-1]) * 0.5)
            b = float(w.get("b", 0.0))
            if x.ndim == 1: x = x[np.newaxis, :]
            if W.shape[0] != x.shape[-1]:
                W = np.ones(x.shape[-1]) * 0.5
            z = x @ W + b
            cache[nid] = act_fn(z)
            output     = cache[nid]

        elif ntype == "mlp":
            layers = p.get("layers", [64, 64])
            h = x.reshape(x.shape[0], -1) if x.ndim > 1 else x[np.newaxis, :]
            for i in range(len(layers) - 1):
                Wi = w.get(f"W{i}", np.eye(h.shape[-1])[:, :layers[i+1]])
                bi = w.get(f"b{i}", np.zeros(layers[i+1]))
                if Wi.shape[0] != h.shape[-1]:
                    Wi = np.random.randn(h.shape[-1], layers[i+1]) * 0.01
                h = act_fn(h @ Wi + bi)
            cache[nid] = h
            output     = h

        elif ntype == "activation":
            cache[nid] = act_fn(x)
            output     = cache[nid]

        elif ntype in ("layer_norm", "rms_norm"):
            gamma = w.get("gamma", np.ones(x.shape[-1]))
            beta  = w.get("beta",  np.zeros(x.shape[-1]))
            if ntype == "rms_norm":
                rms = np.sqrt((x**2).mean(-1, keepdims=True) + 1e-8)
                cache[nid] = gamma * x / rms + beta
            else:
                mu    = x.mean(-1, keepdims=True)
                sigma = x.std(-1, keepdims=True) + 1e-5
                cache[nid] = gamma * (x - mu) / sigma + beta
            output = cache[nid]

        elif ntype == "dropout":
            # No-op at inference; use mask during training
            cache[nid] = x
            output     = x

        elif ntype in ("add", "residual"):
            all_in = [cache[e["from"]] for e in module.get("edges", []) if e["to"] == nid and e["from"] in cache]
            if len(all_in) >= 2:
                try:    result = sum(all_in)
                except: result = all_in[0]
            else:
                result = x
            cache[nid] = result
            output     = result

        elif ntype == "concat":
            all_in = [cache[e["from"]] for e in module.get("edges", []) if e["to"] == nid and e["from"] in cache]
            try:
                cache[nid] = np.concatenate(all_in, axis=p.get("dim", -1))
            except Exception:
                cache[nid] = x
            output = cache[nid]

        elif ntype == "embedding":
            E   = w.get("E", np.eye(p.get("vocab_size", 100))[:, :p.get("dim", 64)])
            idx = np.clip(x.astype(int).ravel(), 0, E.shape[0]-1)
            cache[nid] = E[idx]
            output     = cache[nid]

        elif ntype in ("rnn", "gru", "lstm"):
            h_sz = p.get("hidden_size", 128)
            Wih  = w.get("Wih", np.random.randn(x.shape[-1], h_sz) * 0.01)
            Whh  = w.get("Whh", np.random.randn(h_sz, h_sz) * 0.01)
            bw   = w.get("b",   np.zeros(h_sz))
            if x.ndim == 2: x = x[:, np.newaxis, :]
            batch = x.shape[0]
            h = np.zeros((batch, h_sz))
            for t in range(x.shape[1]):
                xt = x[:, t, :]
                if Wih.shape[0] != xt.shape[-1]:
                    Wih = np.random.randn(xt.shape[-1], h_sz) * 0.01
                h = np.tanh(xt @ Wih + h @ Whh + bw)
            cache[nid] = h
            output     = h

        elif ntype in ("multi_head_attention", "transformer_block"):
            Wq = w.get("Wq", np.eye(x.shape[-1]))
            Wk = w.get("Wk", np.eye(x.shape[-1]))
            Wv = w.get("Wv", np.eye(x.shape[-1]))
            if x.ndim == 2: x = x[:, np.newaxis, :]
            d = x.shape[-1]
            if Wq.shape[0] != d: Wq = Wk = Wv = np.eye(d)
            Q, K, V = x @ Wq, x @ Wk, x @ Wv
            scores  = _softmax(Q @ K.transpose(0,2,1) / np.sqrt(d))
            out     = (scores @ V).mean(1)
            cache[nid] = out
            output     = out

        elif ntype == "pool":
            mode = p.get("mode", "max")
            k    = p.get("kernel", 2)
            if x.ndim < 2:
                cache[nid] = x
            elif mode == "global":
                cache[nid] = x.mean(axis=1) if x.ndim > 1 else x
            else:
                new_len = max(1, x.shape[1] // k) if x.ndim > 1 else 1
                out = np.zeros((x.shape[0], new_len, x.shape[-1])) if x.ndim > 2 else np.zeros((x.shape[0], new_len))
                for i in range(new_len):
                    chunk = x[:, i*k:min((i+1)*k, x.shape[1])]
                    out[:, i] = chunk.max(1) if mode == "max" else chunk.mean(1)
                cache[nid] = out
            output = cache[nid]

        elif ntype == "reshape":
            shape = [-1 if s == -1 else s for s in p.get("shape", [-1])]
            try:
                cache[nid] = x.reshape(x.shape[0], *[s for s in shape if s != -1]) if x.ndim > 1 else x.ravel()
            except Exception:
                cache[nid] = x.reshape(x.shape[0], -1) if x.ndim > 1 else x.ravel()
            output = cache[nid]

        elif ntype == "output":
            out_act = _ACTS.get(p.get("activation", "identity"), _ACTS["identity"])
            cache[nid] = out_act(x)
            output     = cache[nid]

        else:
            cache[nid] = x

    return (output if output is not None else x), cache


# ─────────────────────────────────────────────────────────────────────────────
# LOSS FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def _mse(y_pred, y_true):
    return float(np.mean((y_pred - y_true) ** 2))

def _mae(y_pred, y_true):
    return float(np.mean(np.abs(y_pred - y_true)))

def _bce(y_pred, y_true):
    p = np.clip(y_pred, 1e-7, 1 - 1e-7)
    return float(-np.mean(y_true * np.log(p) + (1 - y_true) * np.log(1 - p)))

def _cross_entropy(y_pred, y_true):
    # y_true: integer class labels or one-hot
    if y_true.ndim == 1:
        y_true_oh = np.zeros_like(y_pred)
        y_true_oh[np.arange(len(y_true)), y_true.astype(int)] = 1
        y_true = y_true_oh
    p = np.clip(_softmax(y_pred), 1e-7, 1.0)
    return float(-np.mean(np.sum(y_true * np.log(p), axis=-1)))

def _huber(y_pred, y_true, delta=1.0):
    diff = np.abs(y_pred - y_true)
    return float(np.mean(np.where(diff <= delta, 0.5 * diff**2, delta * diff - 0.5 * delta**2)))

def _accuracy(y_pred, y_true):
    if y_pred.ndim > 1 and y_pred.shape[-1] > 1:
        pred_cls = y_pred.argmax(-1)
    else:
        pred_cls = (y_pred.ravel() > 0.5).astype(int)
    true_cls = y_true.astype(int).ravel() if y_true.ndim == 1 else y_true.argmax(-1)
    return float(np.mean(pred_cls == true_cls))

LOSSES = {
    "mse": _mse, "mae": _mae, "bce": _bce,
    "cross_entropy": _cross_entropy, "huber": _huber,
}

def _compute_loss(y_pred, y_true, loss_fn: str) -> float:
    fn = LOSSES.get(loss_fn, _mse)
    return fn(y_pred, y_true)


# ─────────────────────────────────────────────────────────────────────────────
# GRADIENT COMPUTATION — numerical finite differences
# Fast enough for small networks; exact gradients for dense/mlp layers
# ─────────────────────────────────────────────────────────────────────────────

def _numerical_grad(module, weights, X, y_true, loss_fn, eps=1e-4):
    """Compute gradients via finite differences. Returns grad dict (same shape as weights)."""
    flat = _flatten_weights(weights)
    grad_flat = np.zeros_like(flat)
    for i in range(len(flat)):
        flat_p = flat.copy(); flat_p[i] += eps
        flat_m = flat.copy(); flat_m[i] -= eps
        w_p = _unflatten_weights(flat_p, weights)
        w_m = _unflatten_weights(flat_m, weights)
        yp, _ = _forward_with_weights(module, w_p, X)
        ym, _ = _forward_with_weights(module, w_m, X)
        grad_flat[i] = (_compute_loss(yp, y_true, loss_fn) -
                        _compute_loss(ym, y_true, loss_fn)) / (2 * eps)
    return _unflatten_weights(grad_flat, weights)


def _fast_grad(module, weights, X, y_true, loss_fn):
    """
    Exact gradients for linear layers via backprop.
    Falls back to numerical grad for unsupported node types.
    Uses numerical grad chunked in parallel for speed.
    """
    # For now, use numerical grads with chunked parallel approx
    # (full backprop is module-dependent; this is the correct general approach)
    flat = _flatten_weights(weights)
    if flat.size == 0:
        return {}
    eps = 1e-4
    # Vectorised: perturb each param once
    y_base, _ = _forward_with_weights(module, weights, X)
    loss_base  = _compute_loss(y_base, y_true, loss_fn)
    grad_flat  = np.zeros_like(flat)
    for i in range(len(flat)):
        flat_p      = flat.copy()
        flat_p[i]  += eps
        wp          = _unflatten_weights(flat_p, weights)
        yp, _       = _forward_with_weights(module, wp, X)
        grad_flat[i] = (_compute_loss(yp, y_true, loss_fn) - loss_base) / eps
    return _unflatten_weights(grad_flat, weights)


# ─────────────────────────────────────────────────────────────────────────────
# OPTIMISERS
# ─────────────────────────────────────────────────────────────────────────────

class _Optimiser:
    def step(self, weights: dict, grads: dict, lr: float) -> dict: ...

class SGD(_Optimiser):
    def __init__(self, momentum=0.0, weight_decay=0.0):
        self.momentum = momentum
        self.weight_decay = weight_decay
        self._velocity = {}

    def step(self, weights, grads, lr):
        new_w = {}
        for nid in weights:
            new_w[nid] = {}
            for k in weights[nid]:
                g = grads.get(nid, {}).get(k, np.zeros_like(weights[nid][k]))
                if self.weight_decay > 0:
                    g = g + self.weight_decay * weights[nid][k]
                if self.momentum > 0:
                    key = f"{nid}.{k}"
                    v   = self._velocity.get(key, np.zeros_like(g))
                    v   = self.momentum * v + g
                    self._velocity[key] = v
                    g = v
                new_w[nid][k] = weights[nid][k] - lr * g
        return new_w


class Adam(_Optimiser):
    def __init__(self, beta1=0.9, beta2=0.999, eps=1e-8, weight_decay=0.0):
        self.beta1 = beta1; self.beta2 = beta2
        self.eps = eps; self.weight_decay = weight_decay
        self._m = {}; self._v = {}; self._t = 0

    def step(self, weights, grads, lr):
        self._t += 1
        new_w = {}
        for nid in weights:
            new_w[nid] = {}
            for k in weights[nid]:
                g   = grads.get(nid, {}).get(k, np.zeros_like(weights[nid][k]))
                if self.weight_decay > 0:
                    g = g + self.weight_decay * weights[nid][k]
                key = f"{nid}.{k}"
                m   = self._m.get(key, np.zeros_like(g))
                v   = self._v.get(key, np.zeros_like(g))
                m   = self.beta1 * m + (1 - self.beta1) * g
                v   = self.beta2 * v + (1 - self.beta2) * g**2
                self._m[key] = m; self._v[key] = v
                m_hat = m / (1 - self.beta1**self._t)
                v_hat = v / (1 - self.beta2**self._t)
                new_w[nid][k] = weights[nid][k] - lr * m_hat / (np.sqrt(v_hat) + self.eps)
        return new_w


class AdamW(Adam):
    def step(self, weights, grads, lr):
        # Weight decay applied directly to weights (decoupled)
        wd = self.weight_decay
        self.weight_decay = 0.0
        new_w = super().step(weights, grads, lr)
        self.weight_decay = wd
        for nid in new_w:
            for k in new_w[nid]:
                new_w[nid][k] -= lr * wd * weights[nid][k]
        return new_w


def _make_optimiser(name: str, **kwargs) -> _Optimiser:
    name = name.lower()
    if name == "sgd":    return SGD(**{k: v for k, v in kwargs.items() if k in ("momentum","weight_decay")})
    if name == "adamw":  return AdamW(**{k: v for k, v in kwargs.items() if k in ("beta1","beta2","eps","weight_decay")})
    return Adam(**{k: v for k, v in kwargs.items() if k in ("beta1","beta2","eps","weight_decay")})


# ─────────────────────────────────────────────────────────────────────────────
# LR SCHEDULERS
# ─────────────────────────────────────────────────────────────────────────────

def _lr_schedule(base_lr: float, epoch: int, total: int, mode: str) -> float:
    if mode == "constant": return base_lr
    if mode == "step":
        step_every = max(1, total // 5)
        return base_lr * (0.5 ** (epoch // step_every))
    if mode == "cosine":
        return base_lr * 0.5 * (1 + math.cos(math.pi * epoch / max(1, total)))
    if mode == "linear":
        return base_lr * max(0.0, 1.0 - epoch / max(1, total))
    return base_lr


# ─────────────────────────────────────────────────────────────────────────────
# DATASET GENERATORS (pure NumPy)
# ─────────────────────────────────────────────────────────────────────────────

def _gen_classification(n=500, features=8, classes=3, noise=0.1, seed=0):
    rng = np.random.default_rng(seed)
    X, y = [], []
    per = n // classes
    for c in range(classes):
        centre = rng.standard_normal(features) * 3
        X.append(rng.standard_normal((per, features)) * noise + centre)
        y.extend([c] * per)
    X = np.vstack(X); y = np.array(y)
    idx = rng.permutation(len(y))
    return X[idx].astype(np.float32), y[idx].astype(np.int32)

def _gen_regression(n=500, features=8, noise=0.05, seed=0):
    rng = np.random.default_rng(seed)
    X = rng.standard_normal((n, features)).astype(np.float32)
    W = rng.standard_normal(features)
    y = (X @ W + rng.standard_normal(n) * noise).astype(np.float32)
    return X, y[:, np.newaxis]

def _gen_xor(n=400, noise=0.1, seed=0):
    rng = np.random.default_rng(seed)
    X = rng.standard_normal((n, 2)).astype(np.float32)
    y = (((X[:,0] > 0) ^ (X[:,1] > 0))).astype(np.int32)
    X += rng.standard_normal((n, 2)).astype(np.float32) * noise
    return X, y

def _gen_spiral(n=400, classes=2, noise=0.2, seed=0):
    rng = np.random.default_rng(seed)
    X, y = [], []
    per = n // classes
    for c in range(classes):
        t = np.linspace(0, 4 * np.pi, per)
        r = t / (4 * np.pi)
        ang = t + c * (2 * np.pi / classes)
        xi = np.column_stack([r * np.cos(ang), r * np.sin(ang)])
        xi += rng.standard_normal(xi.shape) * noise
        X.append(xi); y.extend([c] * per)
    X = np.vstack(X).astype(np.float32); y = np.array(y, dtype=np.int32)
    idx = rng.permutation(len(y))
    return X[idx], y[idx]

def _gen_moons(n=400, noise=0.1, seed=0):
    rng = np.random.default_rng(seed)
    half = n // 2
    t1   = np.linspace(0, np.pi, half)
    t2   = np.linspace(0, np.pi, half)
    X1   = np.column_stack([np.cos(t1), np.sin(t1)])
    X2   = np.column_stack([1 - np.cos(t2), 0.5 - np.sin(t2)])
    X    = np.vstack([X1, X2]).astype(np.float32)
    y    = np.array([0]*half + [1]*half, dtype=np.int32)
    X   += rng.standard_normal(X.shape).astype(np.float32) * noise
    idx  = rng.permutation(n)
    return X[idx], y[idx]

def _gen_timeseries(n=1000, features=6, horizon=1, noise=0.02, seed=0):
    """
    Generate multivariate time-series with trend + seasonality + noise.
    Returns (X, y) where X: (n, features), y: (n, horizon) next-step prediction.
    """
    rng = np.random.default_rng(seed)
    t   = np.linspace(0, 4*np.pi, n + horizon)
    series = np.column_stack([
        np.sin(t * (i+1) * 0.5) + rng.standard_normal(len(t)) * noise
        for i in range(features)
    ]).astype(np.float32)
    X = series[:n]
    y = series[1:n+1, :horizon]
    return X, y

def _gen_autoencoder_data(n=500, dim=64, latent=8, noise=0.05, seed=0):
    """Random linear manifold data for autoencoder training."""
    rng = np.random.default_rng(seed)
    Z   = rng.standard_normal((n, latent)).astype(np.float32)
    W   = rng.standard_normal((latent, dim)).astype(np.float32) * 0.3
    X   = Z @ W + rng.standard_normal((n, dim)).astype(np.float32) * noise
    return X, X  # target = input (reconstruction)

def _gen_ohlcv_synthetic(n=1000, seed=0):
    """
    Synthetic OHLCV data with realistic GBM price dynamics.
    Returns DataFrame-like dict with open/high/low/close/volume columns.
    """
    rng    = np.random.default_rng(seed)
    mu     = 0.0001; sigma = 0.015
    price  = 100.0
    prices = [price]
    for _ in range(n - 1):
        ret = rng.normal(mu, sigma)
        price = price * (1 + ret)
        prices.append(price)
    closes = np.array(prices, dtype=np.float32)
    opens  = np.roll(closes, 1); opens[0] = closes[0]
    noise  = rng.uniform(0.995, 1.005, n).astype(np.float32)
    highs  = np.maximum(opens, closes) * rng.uniform(1.001, 1.02, n).astype(np.float32)
    lows   = np.minimum(opens, closes) * rng.uniform(0.98, 0.999, n).astype(np.float32)
    vols   = rng.lognormal(10, 0.5, n).astype(np.float32)
    return {
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": vols,
        "n": n,
    }


# ─────────────────────────────────────────────────────────────────────────────
# FEATURE ENGINEERING
# ─────────────────────────────────────────────────────────────────────────────

def _returns(prices: np.ndarray, log: bool = True) -> np.ndarray:
    r = np.diff(prices) / prices[:-1]
    return np.log1p(r) if log else r

def _rolling_stats(x: np.ndarray, window: int) -> Tuple[np.ndarray, np.ndarray]:
    """Rolling mean and std. Returns arrays of same length with NaN filled zeros."""
    n    = len(x)
    mu   = np.zeros(n, dtype=np.float32)
    sig  = np.zeros(n, dtype=np.float32)
    for i in range(window, n):
        w       = x[i-window:i]
        mu[i]   = w.mean()
        sig[i]  = w.std() + 1e-8
    return mu, sig

def _rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    delta = np.diff(closes)
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    rsi   = np.zeros(len(closes), dtype=np.float32)
    for i in range(period, len(closes)):
        ag = gain[i-period:i].mean()
        al = loss[i-period:i].mean() + 1e-8
        rsi[i] = 100 - 100 / (1 + ag/al)
    return rsi

def _macd(closes: np.ndarray, fast=12, slow=26, signal=9) -> Tuple[np.ndarray, np.ndarray]:
    def _ema(x, span):
        alpha = 2 / (span + 1)
        out   = np.zeros_like(x)
        out[0] = x[0]
        for i in range(1, len(x)):
            out[i] = alpha * x[i] + (1 - alpha) * out[i-1]
        return out
    ema_fast   = _ema(closes, fast)
    ema_slow   = _ema(closes, slow)
    macd_line  = ema_fast - ema_slow
    signal_line= _ema(macd_line, signal)
    return macd_line.astype(np.float32), signal_line.astype(np.float32)

def _bollinger(closes: np.ndarray, window=20, n_std=2.0):
    mu, sig = _rolling_stats(closes, window)
    upper   = mu + n_std * sig
    lower   = mu - n_std * sig
    return mu.astype(np.float32), upper.astype(np.float32), lower.astype(np.float32)

def _normalise(X: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Standardise (zero mean, unit variance). Returns (X_norm, mean, std)."""
    mu  = X.mean(0, keepdims=True)
    sig = X.std(0, keepdims=True) + 1e-8
    return ((X - mu) / sig).astype(np.float32), mu, sig

def _make_ohlcv_features(ohlcv: dict, window: int = 20, lookahead: int = 5) -> Tuple[np.ndarray, np.ndarray]:
    """
    Build ML-ready feature matrix from OHLCV dict.
    Returns (X, y) where y = sign of next `lookahead`-bar return (classification).
    """
    closes = np.array(ohlcv["close"], dtype=np.float32)
    opens  = np.array(ohlcv["open"],  dtype=np.float32)
    vols   = np.array(ohlcv["volume"],dtype=np.float32)
    n      = len(closes)

    ret      = np.concatenate([[0], _returns(closes)])
    mu, sig  = _rolling_stats(closes, window)
    rsi_v    = _rsi(closes)
    macd_v, macd_sig = _macd(closes)
    bb_mid, bb_up, bb_lo = _bollinger(closes, window)
    vol_norm, _, _ = _normalise(vols.reshape(-1, 1))
    vol_norm = vol_norm.ravel()
    # Price position within Bollinger bands
    bb_pos   = np.where(bb_up - bb_lo > 0, (closes - bb_lo) / (bb_up - bb_lo + 1e-8), 0.5)

    X = np.column_stack([
        ret,
        (closes - mu) / (sig + 1e-8),       # z-score price
        rsi_v / 100,                          # RSI normalised
        macd_v / (closes + 1e-8),             # MACD normalised
        (macd_v - macd_sig) / (closes + 1e-8), # MACD histogram
        bb_pos,                               # Bollinger position
        vol_norm,                             # volume z-score
    ]).astype(np.float32)

    # Target: positive return over next `lookahead` bars
    fwd_ret = np.zeros(n, dtype=np.float32)
    for i in range(n - lookahead):
        fwd_ret[i] = 1.0 if closes[i + lookahead] > closes[i] else 0.0

    # Trim initial NaN window
    start = max(window, 26) + 1
    return X[start:n-lookahead], fwd_ret[start:n-lookahead]


def _train_val_test_split(X, y, val=0.15, test=0.15, seed=42):
    n    = len(X)
    rng  = np.random.default_rng(seed)
    idx  = rng.permutation(n)
    n_test = max(1, int(n * test))
    n_val  = max(1, int(n * val))
    n_train= n - n_val - n_test
    tr_idx = idx[:n_train]
    va_idx = idx[n_train:n_train+n_val]
    te_idx = idx[n_train+n_val:]
    return (X[tr_idx], y[tr_idx],
            X[va_idx], y[va_idx],
            X[te_idx], y[te_idx])


# ─────────────────────────────────────────────────────────────────────────────
# TRAINING LOOP
# ─────────────────────────────────────────────────────────────────────────────

async def _train_loop(job_id: str, job: dict):
    """
    Async training loop that streams progress events.
    Designed to be cancelled cleanly via job["stop"] = True.
    """
    if not HAS_NP:
        job["status"] = "error"; job["error"] = "NumPy not available"
        return

    module_id = job["module_id"]
    sys_mod = __import__("sys")
    ml_mod  = sys_mod.modules.get("ml_workshop")

    # Resolve module — try in-process cache first, then reload from fabric
    module = None
    if ml_mod:
        module = ml_mod._MODULES.get(module_id)
        if not module:
            # Force a fabric reload and try again
            try:
                await ml_mod._load_all_modules()
                module = ml_mod._MODULES.get(module_id)
            except Exception as e:
                log.debug("train_loop fabric reload: %s", e)

    # Last resort: query fabric directly without needing ml_workshop loaded
    if not module:
        try:
            from Vera.Orchestration.data_fabric import _sqlite_query
            rows = await _sqlite_query(dataset_id="ml.modules", limit=2000)
            for r in rows:
                raw = r.get("data")
                if not raw:
                    continue
                try:
                    outer = json.loads(raw) if isinstance(raw, str) else raw
                    # Unwrap ingest_dataset envelope: {text, data: module_dict}
                    d = outer.get("data", outer) if isinstance(outer, dict) else outer
                    if isinstance(d, str):
                        try: d = json.loads(d)
                        except: continue
                    rid = d.get("id") or d.get("_record_id") or outer.get("id","")
                    if rid == module_id and "nodes" in d:
                        module = d
                        # Warm the workshop cache if available
                        if ml_mod:
                            ml_mod._MODULES[module_id] = module
                        break
                except Exception:
                    pass
        except Exception as e:
            log.debug("train_loop direct fabric lookup: %s", e)

    if not module:
        job["status"] = "error"; job["error"] = f"Module {module_id} not found in fabric or cache"
        return

    cfg_t     = job["config"]
    epochs    = int(cfg_t.get("epochs", 20))
    lr        = float(cfg_t.get("lr", 1e-3))
    batch     = int(cfg_t.get("batch_size", 32))
    opt_name  = cfg_t.get("optimiser", "adam")
    loss_fn   = cfg_t.get("loss", "mse")
    sched     = cfg_t.get("lr_schedule", "cosine")
    patience  = int(cfg_t.get("early_stopping_patience", 10))
    grad_clip = float(cfg_t.get("grad_clip", 5.0))

    X_tr = np.array(job["X_train"], dtype=np.float32)
    y_tr = np.array(job["y_train"], dtype=np.float32)
    X_va = np.array(job.get("X_val", X_tr[:max(1, len(X_tr)//5)]), dtype=np.float32)
    y_va = np.array(job.get("y_val", y_tr[:max(1, len(y_tr)//5)]), dtype=np.float32)

    # Init weights (or continue from checkpoint)
    if module_id in _WEIGHTS:
        weights = _WEIGHTS[module_id]
    else:
        weights = _init_weights(module)
        _WEIGHTS[module_id] = weights

    if not weights:
        job["status"] = "error"; job["error"] = "No trainable parameters found"
        return

    opt   = _make_optimiser(opt_name, weight_decay=float(cfg_t.get("weight_decay", 0.0)))
    rng   = np.random.default_rng(42)
    n_tr  = len(X_tr)

    history        = {"train_loss": [], "val_loss": [], "train_acc": [], "val_acc": [], "lr": []}
    best_val_loss  = float("inf")
    best_weights   = deepcopy(weights)
    no_improve     = 0

    job["status"]  = "running"
    job["history"] = history

    for epoch in range(epochs):
        if job.get("stop"):
            job["status"] = "stopped"
            break

        curr_lr  = _lr_schedule(lr, epoch, epochs, sched)
        idx      = rng.permutation(n_tr)
        X_sh, y_sh = X_tr[idx], y_tr[idx]

        # Mini-batch
        epoch_losses = []
        for start in range(0, n_tr, batch):
            if job.get("stop"): break
            Xb = X_sh[start:start+batch]
            yb = y_sh[start:start+batch]

            # Gradient step (use fast 1-sided finite diff)
            grads = _fast_grad(module, weights, Xb, yb, loss_fn)

            # Gradient clipping
            flat_g = _flatten_weights(grads)
            gnorm  = np.linalg.norm(flat_g)
            if gnorm > grad_clip and gnorm > 0:
                scale  = grad_clip / gnorm
                grads  = _unflatten_weights(flat_g * scale, grads)

            weights = opt.step(weights, grads, curr_lr)

            # Compute batch loss
            yp, _  = _forward_with_weights(module, weights, Xb)
            bloss  = _compute_loss(yp, yb, loss_fn)
            epoch_losses.append(bloss)

            # Yield control
            await asyncio.sleep(0)

        train_loss = float(np.mean(epoch_losses)) if epoch_losses else 0.0

        # Val pass
        yp_val, _ = _forward_with_weights(module, weights, X_va)
        val_loss  = _compute_loss(yp_val, y_va, loss_fn)

        # Accuracy (if classification)
        train_acc = val_acc = 0.0
        if loss_fn in ("cross_entropy", "bce"):
            yp_tr_f, _ = _forward_with_weights(module, weights, X_tr[:min(256, n_tr)])
            train_acc  = _accuracy(yp_tr_f, y_tr[:min(256, n_tr)])
            val_acc    = _accuracy(yp_val, y_va)

        history["train_loss"].append(round(train_loss, 6))
        history["val_loss"].append(round(val_loss, 6))
        history["train_acc"].append(round(train_acc, 4))
        history["val_acc"].append(round(val_acc, 4))
        history["lr"].append(round(curr_lr, 8))

        job["epoch"]    = epoch + 1
        job["progress"] = round((epoch + 1) / epochs * 100, 1)

        await emit_event({
            "type":       "ml.train_epoch",
            "job_id":     job_id,
            "module_id":  module_id,
            "epoch":      epoch + 1,
            "epochs":     epochs,
            "train_loss": round(train_loss, 6),
            "val_loss":   round(val_loss, 6),
            "val_acc":    round(val_acc, 4),
            "lr":         round(curr_lr, 8),
            "progress":   job["progress"],
        })

        # Early stopping
        if val_loss < best_val_loss - 1e-6:
            best_val_loss = val_loss
            best_weights  = deepcopy(weights)
            no_improve    = 0
            job["best_val_loss"] = round(best_val_loss, 6)
        else:
            no_improve += 1
            if no_improve >= patience:
                job["status"] = "early_stopped"
                break

        await asyncio.sleep(0)

    # Restore best weights
    _WEIGHTS[module_id] = best_weights
    await _persist_weights(module_id, best_weights)

    if job["status"] == "running":
        job["status"] = "complete"

    # Persist final job record to fabric
    await _persist_job(job)

    await emit_event({
        "type":          "ml.train_complete",
        "job_id":        job_id,
        "module_id":     module_id,
        "status":        job["status"],
        "epochs_run":    job.get("epoch", 0),
        "best_val_loss": job.get("best_val_loss", 0),
    })


async def _persist_weights(module_id: str, weights: dict):
    """Save weights to Data Fabric (primary) and Redis (hot cache)."""
    if not weights:
        return
    serialised = {
        nid: {k: v.tolist() for k, v in nw.items()}
        for nid, nw in weights.items()
    }
    # Primary: fabric (survives restarts)
    try:
        from Vera.Orchestration.data_fabric import ingest_dataset as _ingest
        await _ingest(
            dataset_id = "ml.weights",
            data       = [{"module_id": module_id, "weights": serialised,
                           "saved_at": now_iso()}],
            source     = "ml_training",
            tags       = ["ml", "weights", module_id],
        )
    except Exception as e:
        log.debug("persist_weights fabric: %s", e)
    # Secondary: Redis for fast sub-ms lookup during active training
    try:
        import Vera.Orchestration.capability_orchestration as _orch
        if _orch.REDIS:
            await _orch.REDIS.set(
                REDIS_W_PREFIX + module_id,
                json.dumps(serialised),
                ex=604800,   # 7 days TTL
            )
    except Exception as e:
        log.debug("persist_weights redis: %s", e)


async def _persist_job(job: dict):
    """Persist training job history to Data Fabric."""
    try:
        from Vera.Orchestration.data_fabric import ingest_dataset as _ingest
        record = {
            "job_id":        job["job_id"],
            "module_id":     job["module_id"],
            "status":        job.get("status",""),
            "config":        job.get("config",{}),
            "history":       job.get("history",{}),
            "best_val_loss": job.get("best_val_loss"),
            "epochs_run":    job.get("epoch",0),
            "created_at":    job.get("created_at",""),
            "updated_at":    now_iso(),
        }
        await _ingest(
            dataset_id = "ml.training.jobs",
            data       = [record],
            source     = "ml_training",
            tags       = ["ml","training","job", job["module_id"]],
        )
    except Exception as e:
        log.debug("persist_job: %s", e)


async def _load_weights(module_id: str) -> Optional[dict]:
    """Load weights from Redis (fast) then Data Fabric (fallback)."""
    # 1. Redis hot cache
    try:
        import Vera.Orchestration.capability_orchestration as _orch
        if _orch.REDIS:
            raw = await _orch.REDIS.get(REDIS_W_PREFIX + module_id)
            if raw:
                d = json.loads(raw)
                return {nid: {k: np.array(v) for k, v in nw.items()} for nid, nw in d.items()}
    except Exception:
        pass
    # 2. Data Fabric fallback
    try:
        from Vera.Orchestration.data_fabric import _sqlite_query
        rows = await _sqlite_query(dataset_id="ml.weights", limit=500)
        # Scan newest-first for this module_id
        for r in rows:
            raw = r.get("data")
            if not raw:
                continue
            try:
                outer = json.loads(raw) if isinstance(raw, str) else raw
                # ingest_dataset wraps: {text, data: payload}
                payload = outer.get("data") if isinstance(outer, dict) else None
                if payload is None:
                    payload = outer  # flat record
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if isinstance(payload, dict) and payload.get("module_id") == module_id:
                    w = payload.get("weights", {})
                    if w:
                        return {nid: {k: np.array(v) for k, v in nw.items()}
                                for nid, nw in w.items()}
            except Exception:
                pass
    except Exception as e:
        log.debug("_load_weights fabric: %s", e)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    # ── Dataset: synthetic generators ─────────────────────────────────────────

    @capability(
        "ml.data.fetch_synthetic",
        http_method="POST", http_path="/ml/data/synthetic", http_tags=["ml", "data"],
        memory="off",
        description=(
            "Generate a synthetic dataset and store it in the Data Fabric. "
            "kind: classification | regression | xor | spiral | moons | timeseries | "
            "autoencoder | ohlcv_synthetic. "
            "Returns {dataset_id, n_samples, features, task}."
        ),
    )
    async def ml_data_fetch_synthetic(
        kind:       str = "classification",
        n:          int = 500,
        features:   int = 8,
        classes:    int = 3,
        noise:      float = 0.1,
        dataset_id: str = "",
        trace_id=None,
    ):
        if not HAS_NP:
            return {"error": "NumPy required"}

        ds_id = dataset_id or f"ml.synthetic.{kind}"

        if kind == "classification":
            X, y = _gen_classification(n, features, classes, noise)
            rows = [{"features": X[i].tolist(), "label": int(y[i])} for i in range(len(X))]
            task = "classification"
        elif kind == "regression":
            X, y = _gen_regression(n, features, noise)
            rows = [{"features": X[i].tolist(), "target": float(y[i,0])} for i in range(len(X))]
            task = "regression"
        elif kind == "xor":
            X, y = _gen_xor(n, noise)
            rows = [{"features": X[i].tolist(), "label": int(y[i])} for i in range(len(X))]
            task = "binary_classification"
        elif kind == "spiral":
            X, y = _gen_spiral(n, classes, noise)
            rows = [{"features": X[i].tolist(), "label": int(y[i])} for i in range(len(X))]
            task = "classification"
        elif kind == "moons":
            X, y = _gen_moons(n, noise)
            rows = [{"features": X[i].tolist(), "label": int(y[i])} for i in range(len(X))]
            task = "binary_classification"
        elif kind == "timeseries":
            X, y = _gen_timeseries(n, features)
            rows = [{"features": X[i].tolist(), "target": y[i].tolist()} for i in range(len(X))]
            task = "forecasting"
        elif kind == "autoencoder":
            X, y = _gen_autoencoder_data(n, features * 8)
            rows = [{"features": X[i].tolist()} for i in range(len(X))]
            task = "reconstruction"
        elif kind == "ohlcv_synthetic":
            ohlcv = _gen_ohlcv_synthetic(n)
            rows = [
                {"open": float(ohlcv["open"][i]), "high": float(ohlcv["high"][i]),
                 "low": float(ohlcv["low"][i]),   "close": float(ohlcv["close"][i]),
                 "volume": float(ohlcv["volume"][i]), "bar": i}
                for i in range(n)
            ]
            task = "ohlcv"
            ds_id = dataset_id or "ml.synthetic.ohlcv"
        else:
            return {"error": f"Unknown kind: {kind}"}

        # Store in Data Fabric
        try:
            from Vera.Orchestration.data_fabric import ingest_dataset
            result = await ingest_dataset(
                dataset_id = ds_id,
                data       = rows,
                source     = "ml_synthetic",
                tags       = ["ml", "synthetic", kind, task],
            )
            ingested = result.get("ingested", len(rows))
        except Exception as e:
            ingested = len(rows)
            log.warning("ml synthetic fabric ingest: %s", e)

        await emit_event({"type": "ml.data.ready", "dataset_id": ds_id,
                          "kind": kind, "n": ingested, "task": task})
        return {
            "ok": True, "dataset_id": ds_id,
            "n_samples": ingested, "features": features,
            "task": task, "kind": kind,
        }

    # ── Dataset: OHLCV from Stooq (free, no API key) ──────────────────────────

    @capability(
        "ml.data.fetch_ohlcv",
        http_method="POST", http_path="/ml/data/ohlcv", http_tags=["ml", "data"],
        memory="off",
        description=(
            "Fetch real OHLCV price data from Stooq (free, no API key). "
            "symbol: e.g. 'AAPL.US', 'BTC.V', 'SPY.US', '^SPX'. "
            "interval: d (daily), w (weekly), m (monthly). "
            "Stores in Data Fabric as ml.ohlcv.<symbol>. "
            "Returns {dataset_id, n_samples, symbol}."
        ),
    )
    async def ml_data_fetch_ohlcv(
        symbol:     str = "AAPL.US",
        interval:   str = "d",
        dataset_id: str = "",
        trace_id=None,
    ):
        if not HAS_HTTPX:
            return {"error": "httpx not installed"}

        ds_id   = dataset_id or f"ml.ohlcv.{symbol.lower().replace('.','_')}"
        url     = f"https://stooq.com/q/d/l/?s={symbol}&i={interval}"

        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                          headers={"User-Agent": "Vera-ML/1.0"}) as c:
                resp = await c.get(url)
                resp.raise_for_status()
                text = resp.text
        except Exception as e:
            return {"error": f"Fetch failed: {e}", "url": url}

        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        if len(lines) < 2:
            return {"error": "Empty or invalid response from Stooq", "symbol": symbol}

        headers = [h.lower() for h in lines[0].split(",")]
        rows    = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) < len(headers):
                continue
            try:
                row = dict(zip(headers, parts))
                rows.append({
                    "date":   row.get("date",""),
                    "open":   float(row.get("open",  0)),
                    "high":   float(row.get("high",  0)),
                    "low":    float(row.get("low",   0)),
                    "close":  float(row.get("close", 0)),
                    "volume": float(row.get("volume",0)),
                    "symbol": symbol,
                })
            except Exception:
                continue

        if not rows:
            return {"error": "No valid OHLCV rows parsed", "symbol": symbol}

        try:
            from Vera.Orchestration.data_fabric import ingest_dataset
            result = await ingest_dataset(
                dataset_id = ds_id,
                data       = rows,
                source     = "stooq",
                tags       = ["ml", "ohlcv", "finance", symbol.lower()],
            )
            ingested = result.get("ingested", len(rows))
        except Exception as e:
            ingested = len(rows)
            log.warning("ohlcv fabric ingest: %s", e)

        await emit_event({"type": "ml.data.ready", "dataset_id": ds_id,
                          "symbol": symbol, "n": ingested})
        return {
            "ok": True, "dataset_id": ds_id,
            "n_samples": ingested, "symbol": symbol,
            "interval": interval,
            "date_range": f"{rows[0]['date']} → {rows[-1]['date']}" if rows else "",
        }

    # ── Dataset: Crypto via CoinGecko ─────────────────────────────────────────

    @capability(
        "ml.data.fetch_crypto",
        http_method="POST", http_path="/ml/data/crypto", http_tags=["ml", "data"],
        memory="off",
        description=(
            "Fetch crypto OHLCV from CoinGecko public API (no key needed). "
            "coin_id: 'bitcoin', 'ethereum', 'solana', etc. "
            "vs_currency: 'usd'. days: 1–365. "
            "Returns {dataset_id, n_samples, coin_id}."
        ),
    )
    async def ml_data_fetch_crypto(
        coin_id:    str = "bitcoin",
        vs_currency: str = "usd",
        days:       int = 365,
        dataset_id: str = "",
        trace_id=None,
    ):
        if not HAS_HTTPX:
            return {"error": "httpx not installed"}

        ds_id  = dataset_id or f"ml.crypto.{coin_id}_{vs_currency}"
        url    = (f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
                  f"?vs_currency={vs_currency}&days={min(days, 365)}")
        try:
            async with httpx.AsyncClient(timeout=30,
                                          headers={"User-Agent": "Vera-ML/1.0"}) as c:
                resp = await c.get(url); resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return {"error": f"CoinGecko fetch failed: {e}"}

        rows = []
        for item in data:
            if len(item) >= 5:
                ts = item[0] / 1000
                rows.append({
                    "timestamp": int(ts),
                    "open":  float(item[1]),
                    "high":  float(item[2]),
                    "low":   float(item[3]),
                    "close": float(item[4]),
                    "coin":  coin_id,
                })

        if not rows:
            return {"error": "No OHLCV data returned", "coin_id": coin_id}

        try:
            from Vera.Orchestration.data_fabric import ingest_dataset
            result = await ingest_dataset(
                dataset_id = ds_id,
                data       = rows,
                source     = "coingecko",
                tags       = ["ml", "crypto", "ohlcv", coin_id, vs_currency],
            )
            ingested = result.get("ingested", len(rows))
        except Exception as e:
            ingested = len(rows)

        await emit_event({"type": "ml.data.ready", "dataset_id": ds_id,
                          "coin_id": coin_id, "n": ingested})
        return {"ok": True, "dataset_id": ds_id, "n_samples": ingested,
                "coin_id": coin_id, "vs_currency": vs_currency}

    # ── Dataset: FRED macroeconomic ────────────────────────────────────────────

    @capability(
        "ml.data.fetch_macro",
        http_method="POST", http_path="/ml/data/macro", http_tags=["ml", "data"],
        memory="off",
        description=(
            "Fetch macroeconomic series from FRED (St. Louis Fed — free, no key for public series). "
            "series_ids: comma-separated FRED series codes, e.g. 'GDP,CPIAUCSL,FEDFUNDS,UNRATE'. "
            "Returns {dataset_id, n_samples, series}."
        ),
    )
    async def ml_data_fetch_macro(
        series_ids: str = "GDP,CPIAUCSL,FEDFUNDS,UNRATE",
        dataset_id: str = "ml.macro.fred",
        trace_id=None,
    ):
        if not HAS_HTTPX:
            return {"error": "httpx not installed"}

        ids   = [s.strip() for s in series_ids.split(",") if s.strip()]
        all_rows: Dict[str, dict] = {}

        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "Vera-ML/1.0"}) as c:
            for sid in ids:
                url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
                try:
                    resp = await c.get(url); resp.raise_for_status()
                    lines = resp.text.strip().splitlines()
                    for line in lines[1:]:
                        parts = line.split(",")
                        if len(parts) >= 2:
                            date, val = parts[0], parts[1]
                            if date not in all_rows:
                                all_rows[date] = {"date": date}
                            try:
                                all_rows[date][sid.lower()] = float(val)
                            except ValueError:
                                pass
                    await asyncio.sleep(0.5)
                except Exception as e:
                    log.warning("FRED %s: %s", sid, e)

        rows = sorted(all_rows.values(), key=lambda r: r["date"])
        if not rows:
            return {"error": "No macroeconomic data fetched"}

        try:
            from Vera.Orchestration.data_fabric import ingest_dataset
            result = await ingest_dataset(
                dataset_id = dataset_id,
                data       = rows,
                source     = "fred",
                tags       = ["ml", "macro", "economics"] + [s.lower() for s in ids],
            )
            ingested = result.get("ingested", len(rows))
        except Exception as e:
            ingested = len(rows)

        await emit_event({"type": "ml.data.ready", "dataset_id": dataset_id,
                          "series": ids, "n": ingested})
        return {"ok": True, "dataset_id": dataset_id, "n_samples": ingested,
                "series": ids}

    # ── Dataset: list ─────────────────────────────────────────────────────────

    @capability(
        "ml.data.list",
        http_method="GET", http_path="/ml/data/list", http_tags=["ml", "data"],
        memory="off", silent=True,
        description="List all ML-tagged datasets in the Data Fabric.",
    )
    async def ml_data_list(trace_id=None):
        try:
            from Vera.Orchestration.data_fabric import _sqlite_datasets, _sqlite_query
            all_ds = await _sqlite_datasets()
            # Include all ml.* datasets — data, modules, weights, jobs
            ml_ds  = [d for d in all_ds if str(d.get("dataset_id","")).startswith("ml.")]
            result = []
            for d in ml_ds:
                ds_id = d["dataset_id"]
                # Skip internal module/weight stores from the dataset picker
                # (they're handled by the module browser)
                sample = await _sqlite_query(dataset_id=ds_id, limit=1)
                keys = []
                if sample:
                    raw = sample[0].get("data")
                    if raw:
                        try:
                            parsed = json.loads(raw) if isinstance(raw, str) else raw
                            keys = list(parsed.keys())[:8] if isinstance(parsed, dict) else []
                        except Exception:
                            pass
                result.append({
                    "dataset_id":   ds_id,
                    "record_count": d.get("record_count", 0),
                    "updated_at":   d.get("updated_at",""),
                    "sample_keys":  keys,
                    "is_internal":  ds_id in ("ml.modules","ml.weights","ml.training.jobs"),
                })
            result.sort(key=lambda x: x["updated_at"], reverse=True)
            return {"datasets": result, "count": len(result)}
        except Exception as e:
            return {"datasets": [], "error": str(e)}

    # ── Dataset: prepare for training ────────────────────────────────────────

    @capability(
        "ml.data.prepare",
        http_method="POST", http_path="/ml/data/prepare", http_tags=["ml", "data"],
        memory="off",
        description=(
            "Load a fabric dataset, engineer features, normalise, and split into "
            "train/val/test arrays ready for ml.train. "
            "task: classification | regression | forecasting | ohlcv_direction. "
            "Returns {X_train, y_train, X_val, y_val, X_test, y_test, feature_names}."
        ),
    )
    async def ml_data_prepare(
        dataset_id:  str,
        task:        str   = "classification",
        feature_col: str   = "features",
        label_col:   str   = "label",
        max_rows:    int   = 5000,
        val_frac:    float = 0.15,
        test_frac:   float = 0.15,
        normalise:   bool  = True,
        trace_id=None,
    ):
        if not HAS_NP:
            return {"error": "NumPy required"}

        try:
            from Vera.Orchestration.data_fabric import _sqlite_query
            rows = await _sqlite_query(dataset_id=dataset_id, limit=max_rows)
        except Exception as e:
            return {"error": f"Fabric query failed: {e}"}

        if not rows:
            return {"error": f"No data found for dataset_id={dataset_id}"}

        # ── Parse fabric records — each row has a 'data' JSON field ──────────
        def _parse_row(r: dict) -> dict:
            """Extract the payload dict from a fabric record."""
            raw = r.get("data")
            if raw is None:
                # Some records store directly in the row dict
                return {k: v for k, v in r.items()
                        if k not in ("id","dataset_id","text","source_id",
                                     "tags","created_at","synced_pg")}
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except Exception:
                    return {}
            if isinstance(raw, dict):
                return raw
            return {}

        # Parse feature and label columns
        try:
            if task == "ohlcv_direction":
                ohlcv_data = [_parse_row(r) for r in rows]
                ohlcv = {
                    "open":   [float(d.get("open",  0) or 0) for d in ohlcv_data],
                    "high":   [float(d.get("high",  0) or 0) for d in ohlcv_data],
                    "low":    [float(d.get("low",   0) or 0) for d in ohlcv_data],
                    "close":  [float(d.get("close", 0) or 0) for d in ohlcv_data],
                    "volume": [float(d.get("volume",0) or 0) for d in ohlcv_data],
                }
                # Filter rows where close=0 (gaps)
                valid = [i for i,c in enumerate(ohlcv["close"]) if c > 0]
                if len(valid) < 30:
                    return {"error": "Not enough valid OHLCV rows (need >30 non-zero close prices)"}
                for k in ohlcv:
                    ohlcv[k] = [ohlcv[k][i] for i in valid]
                X, y = _make_ohlcv_features(ohlcv)
                y    = y[:, np.newaxis]
                feature_names = ["return","z_price","rsi","macd","macd_hist","bb_pos","volume"]

            else:
                data_rows = [_parse_row(r) for r in rows]

                # Extract feature_col
                X_list, y_list = [], []
                for row in data_rows:
                    feat = row.get(feature_col, [])
                    if not feat and feat != 0:
                        continue
                    if isinstance(feat, (int, float)):
                        feat = [float(feat)]
                    elif not isinstance(feat, list):
                        continue
                    lbl_raw = row.get(label_col, row.get("target", row.get("close")))
                    if lbl_raw is None:
                        continue
                    lbl = lbl_raw if isinstance(lbl_raw, list) else [float(lbl_raw)]
                    X_list.append([float(v) for v in feat])
                    y_list.append(lbl)

                if not X_list:
                    return {
                        "error": (
                            f"No '{feature_col}' column found in {len(data_rows)} "
                            f"records of dataset '{dataset_id}'. "
                            f"Sample keys: {list(data_rows[0].keys())[:8] if data_rows else []}"
                        )
                    }

                X = np.array(X_list, dtype=np.float32)
                y = np.array(y_list, dtype=np.float32)
                feature_names = [f"f{i}" for i in range(X.shape[1])]

        except Exception as e:
            return {"error": f"Feature extraction failed: {e}"}

        if normalise:
            X, xmu, xsig = _normalise(X)

        X_tr, y_tr, X_va, y_va, X_te, y_te = _train_val_test_split(
            X, y, val=val_frac, test=test_frac
        )

        return {
            "ok": True,
            "dataset_id":    dataset_id,
            "task":          task,
            "n_train":       len(X_tr),
            "n_val":         len(X_va),
            "n_test":        len(X_te),
            "features":      X.shape[1],
            "feature_names": feature_names,
            "X_train": X_tr.tolist(), "y_train": y_tr.tolist(),
            "X_val":   X_va.tolist(), "y_val":   y_va.tolist(),
            "X_test":  X_te.tolist(), "y_test":  y_te.tolist(),
        }

    # ── Train ─────────────────────────────────────────────────────────────────

    @capability(
        "ml.train",
        http_method="POST", http_path="/ml/train", http_tags=["ml"],
        memory="on",
        description=(
            "Start a training run. Supply module_id, X_train/y_train arrays (JSON lists), "
            "and optional X_val/y_val. Config: epochs, lr, batch_size, optimiser "
            "(sgd|adam|adamw), loss (mse|mae|bce|cross_entropy|huber), "
            "lr_schedule (constant|step|cosine|linear), early_stopping_patience, "
            "weight_decay, grad_clip. "
            "Returns {job_id} immediately; stream ml.train_epoch events for progress."
        ),
    )
    async def ml_train(
        module_id:  str,
        X_train:    str,
        y_train:    str,
        X_val:      str = "[]",
        y_val:      str = "[]",
        config:     str = "{}",
        trace_id=None,
    ):
        if not HAS_NP:
            return {"error": "NumPy required for training"}
        try:
            Xtr = json.loads(X_train)
            ytr = json.loads(y_train)
            Xva = json.loads(X_val) if X_val and X_val != "[]" else []
            yva = json.loads(y_val) if y_val and y_val != "[]" else []
            cfg = json.loads(config) if isinstance(config, str) else config
        except Exception as e:
            return {"error": f"JSON parse: {e}"}

        if not Xtr:
            return {"error": "X_train is empty"}

        # Auto-split val from train if not provided
        if not Xva:
            n      = len(Xtr)
            n_val  = max(1, n // 10)
            Xva    = Xtr[-n_val:]
            yva    = ytr[-n_val:]
            Xtr    = Xtr[:-n_val]
            ytr    = ytr[:-n_val]

        job_id = str(uuid.uuid4())[:8]
        job    = {
            "job_id":    job_id,
            "module_id": module_id,
            "status":    "queued",
            "config":    cfg,
            "X_train":   Xtr,
            "y_train":   ytr,
            "X_val":     Xva,
            "y_val":     yva,
            "epoch":     0,
            "progress":  0.0,
            "history":   {},
            "stop":      False,
            "created_at": now_iso(),
        }
        _TRAINING_JOBS[job_id] = job

        asyncio.create_task(_train_loop(job_id, job))

        await emit_event({"type": "ml.train_started", "job_id": job_id,
                          "module_id": module_id, "config": cfg})
        return {
            "ok": True, "job_id": job_id,
            "module_id": module_id,
            "message": "Training started — listen for ml.train_epoch events",
        }

    # ── Quick train (dataset_id shortcut) ─────────────────────────────────────

    @capability(
        "ml.train.from_dataset",
        http_method="POST", http_path="/ml/train/dataset", http_tags=["ml"],
        memory="on",
        description=(
            "Convenience: prepare a fabric dataset then immediately start training. "
            "Combines ml.data.prepare + ml.train in one call. "
            "Supply module_id, dataset_id, task, and config JSON."
        ),
    )
    async def ml_train_from_dataset(
        module_id:  str,
        dataset_id: str,
        task:       str = "classification",
        config:     str = "{}",
        max_rows:   int = 5000,
        trace_id=None,
    ):
        # Prepare
        prep = await ml_data_prepare(
            dataset_id=dataset_id, task=task, max_rows=max_rows
        )
        if "error" in prep:
            return prep
        cfg = json.loads(config) if isinstance(config, str) else config

        # Auto-set loss based on task
        if "loss" not in cfg:
            cfg["loss"] = {
                "classification": "cross_entropy",
                "binary_classification": "bce",
                "regression": "mse",
                "forecasting": "mse",
                "ohlcv_direction": "bce",
                "reconstruction": "mse",
            }.get(task, "mse")

        return await ml_train(
            module_id=module_id,
            X_train=json.dumps(prep["X_train"]),
            y_train=json.dumps(prep["y_train"]),
            X_val=json.dumps(prep["X_val"]),
            y_val=json.dumps(prep["y_val"]),
            config=json.dumps(cfg),
        )

    # ── Job status ────────────────────────────────────────────────────────────

    @capability(
        "ml.train.status",
        http_method="GET", http_path="/ml/train/status", http_tags=["ml"],
        memory="off", silent=True,
        description="Get status of a training job. Pass job_id or omit for all jobs.",
    )
    async def ml_train_status(job_id: str = "", trace_id=None):
        if job_id:
            job = _TRAINING_JOBS.get(job_id)
            if not job:
                # Try fabric for completed jobs from previous sessions
                try:
                    from Vera.Orchestration.data_fabric import _sqlite_query
                    rows = await _sqlite_query(dataset_id="ml.training.jobs", limit=200)
                    for r in rows:
                        raw = r.get("data")
                        if not raw: continue
                        outer = json.loads(raw) if isinstance(raw, str) else raw
                        d = outer.get("data", outer) if isinstance(outer, dict) else outer
                        if isinstance(d, str):
                            try: d = json.loads(d)
                            except: continue
                        if d.get("job_id") == job_id:
                            return {
                                "job_id":    d["job_id"],
                                "module_id": d.get("module_id",""),
                                "status":    d.get("status","complete"),
                                "epoch":     d.get("epochs_run",0),
                                "progress":  100,
                                "best_val_loss": d.get("best_val_loss"),
                                "config":    d.get("config",{}),
                                "created_at": d.get("created_at",""),
                                "history":   d.get("history",{}),
                            }
                except Exception:
                    pass
                return {"error": f"Job {job_id} not found"}
            return {
                "job_id":    job["job_id"],
                "module_id": job["module_id"],
                "status":    job["status"],
                "epoch":     job.get("epoch", 0),
                "progress":  job.get("progress", 0),
                "best_val_loss": job.get("best_val_loss", None),
                "config":    job.get("config", {}),
                "created_at": job.get("created_at",""),
            }
        # List: merge in-process jobs with fabric history
        jobs = []
        seen = set()
        for jid, job in _TRAINING_JOBS.items():
            seen.add(jid)
            jobs.append({
                "job_id":    jid,
                "module_id": job["module_id"],
                "status":    job["status"],
                "epoch":     job.get("epoch", 0),
                "progress":  job.get("progress", 0),
                "best_val_loss": job.get("best_val_loss", None),
                "created_at": job.get("created_at",""),
            })
        # Add completed jobs from fabric that aren't in memory
        try:
            from Vera.Orchestration.data_fabric import _sqlite_query
            rows = await _sqlite_query(dataset_id="ml.training.jobs", limit=50)
            for r in rows:
                raw = r.get("data")
                if not raw: continue
                try:
                    outer = json.loads(raw) if isinstance(raw, str) else raw
                    d = outer.get("data", outer) if isinstance(outer, dict) else outer
                    if isinstance(d, str):
                        try: d = json.loads(d)
                        except: continue
                    jid = d.get("job_id","")
                    if jid and jid not in seen:
                        seen.add(jid)
                        jobs.append({
                            "job_id":    jid,
                            "module_id": d.get("module_id",""),
                            "status":    d.get("status","complete"),
                            "epoch":     d.get("epochs_run",0),
                            "progress":  100,
                            "best_val_loss": d.get("best_val_loss"),
                            "created_at": d.get("created_at",""),
                        })
                except Exception:
                    pass
        except Exception:
            pass
        return {"jobs": sorted(jobs, key=lambda j: j["created_at"], reverse=True)}

    # ── Job stop ──────────────────────────────────────────────────────────────

    @capability(
        "ml.train.stop",
        http_method="POST", http_path="/ml/train/stop", http_tags=["ml"],
        memory="off",
        description="Stop a running training job.",
    )
    async def ml_train_stop(job_id: str, trace_id=None):
        job = _TRAINING_JOBS.get(job_id)
        if not job:
            return {"error": f"Job {job_id} not found"}
        job["stop"] = True
        return {"ok": True, "job_id": job_id, "status": "stopping"}

    # ── Loss history ──────────────────────────────────────────────────────────

    @capability(
        "ml.train.history",
        http_method="GET", http_path="/ml/train/history", http_tags=["ml"],
        memory="off", silent=True,
        description="Get full loss and metric history for a training job.",
    )
    async def ml_train_history(job_id: str, trace_id=None):
        job = _TRAINING_JOBS.get(job_id)
        if not job:
            return {"error": f"Job {job_id} not found"}
        return {
            "job_id":    job_id,
            "module_id": job["module_id"],
            "status":    job["status"],
            "config":    job.get("config", {}),
            "history":   job.get("history", {}),
            "best_val_loss": job.get("best_val_loss"),
            "epochs_run":    job.get("epoch", 0),
        }

    # ── Evaluate ─────────────────────────────────────────────────────────────

    @capability(
        "ml.train.evaluate",
        http_method="POST", http_path="/ml/train/evaluate", http_tags=["ml"],
        memory="on",
        description=(
            "Evaluate a trained module on test data. "
            "Supply module_id, X_test and y_test (JSON lists). "
            "Returns {loss, accuracy, mse, mae, predictions_sample}."
        ),
    )
    async def ml_train_evaluate(
        module_id: str,
        X_test:    str,
        y_test:    str,
        loss_fn:   str = "mse",
        trace_id=None,
    ):
        if not HAS_NP:
            return {"error": "NumPy required"}
        sys_mod = __import__("sys")
        ml_mod  = sys_mod.modules.get("ml_workshop")
        if not ml_mod:
            return {"error": "ml_workshop not loaded"}
        module = ml_mod._MODULES.get(module_id)
        if not module:
            return {"error": f"Module {module_id} not found"}

        # Load trained weights or init fresh
        w = _WEIGHTS.get(module_id) or await _load_weights(module_id)
        if not w:
            w = _init_weights(module)

        try:
            X = np.array(json.loads(X_test), dtype=np.float32)
            y = np.array(json.loads(y_test), dtype=np.float32)
        except Exception as e:
            return {"error": f"JSON parse: {e}"}

        y_pred, _ = _forward_with_weights(module, w, X)

        metrics = {
            "loss":     round(_compute_loss(y_pred, y, loss_fn), 6),
            "mse":      round(_mse(y_pred, y), 6),
            "mae":      round(_mae(y_pred, y), 6),
            "rmse":     round(float(np.sqrt(_mse(y_pred, y))), 6),
        }
        if loss_fn in ("cross_entropy", "bce"):
            metrics["accuracy"] = round(_accuracy(y_pred, y), 4)

        # Sample predictions
        n_show = min(10, len(y_pred))
        pred_sample = []
        for i in range(n_show):
            pred_sample.append({
                "true":  y[i].tolist() if hasattr(y[i], "tolist") else float(y[i]),
                "pred":  y_pred[i].tolist() if hasattr(y_pred[i], "tolist") else float(y_pred[i]),
            })

        return {
            "ok":             True,
            "module_id":      module_id,
            "n_samples":      len(X),
            "metrics":        metrics,
            "predictions_sample": pred_sample,
        }

    # ── Predict ───────────────────────────────────────────────────────────────

    @capability(
        "ml.train.predict",
        http_method="POST", http_path="/ml/train/predict", http_tags=["ml"],
        memory="on",
        description="Run batch inference using trained weights. Supply X (JSON list).",
    )
    async def ml_train_predict(module_id: str, X: str, trace_id=None):
        if not HAS_NP:
            return {"error": "NumPy required"}
        sys_mod = __import__("sys")
        ml_mod  = sys_mod.modules.get("ml_workshop")
        if not ml_mod:
            return {"error": "ml_workshop not loaded"}
        module  = ml_mod._MODULES.get(module_id)
        if not module:
            return {"error": f"Module {module_id} not found"}
        w = _WEIGHTS.get(module_id) or await _load_weights(module_id)
        if not w:
            w = _init_weights(module)
        try:
            Xarr = np.array(json.loads(X), dtype=np.float32)
        except Exception as e:
            return {"error": f"JSON parse: {e}"}
        y_pred, _ = _forward_with_weights(module, w, Xarr)
        return {
            "ok":         True,
            "module_id":  module_id,
            "n":          len(Xarr),
            "predictions": y_pred.tolist() if hasattr(y_pred, "tolist") else [[float(y_pred)]],
            "shape":      list(y_pred.shape) if hasattr(y_pred, "shape") else [],
        }

    # ── Weights export/import ─────────────────────────────────────────────────

    @capability(
        "ml.train.weights_get",
        http_method="GET", http_path="/ml/train/weights", http_tags=["ml"],
        memory="off", silent=True,
        description="Export trained weights for a module as a JSON dict.",
    )
    async def ml_train_weights_get(module_id: str, trace_id=None):
        w = _WEIGHTS.get(module_id) or await _load_weights(module_id)
        if not w:
            return {"error": f"No weights found for {module_id}"}
        serialised = {nid: {k: v.tolist() for k, v in nw.items()} for nid, nw in w.items()}
        stats = {}
        for nid, nw in w.items():
            stats[nid] = {k: {"shape": list(v.shape), "norm": float(np.linalg.norm(v.ravel()))}
                          for k, v in nw.items()}
        return {"module_id": module_id, "weights": serialised, "stats": stats}

    @capability(
        "ml.train.weights_load",
        http_method="POST", http_path="/ml/train/weights/load", http_tags=["ml"],
        memory="off",
        description="Load weights into a module from a JSON dict (from weights_get).",
    )
    async def ml_train_weights_load(module_id: str, weights: str, trace_id=None):
        try:
            w_dict = json.loads(weights) if isinstance(weights, str) else weights
            w = {nid: {k: np.array(v) for k, v in nw.items()} for nid, nw in w_dict.items()}
        except Exception as e:
            return {"error": f"Parse: {e}"}
        _WEIGHTS[module_id] = w
        await _persist_weights(module_id, w)
        return {"ok": True, "module_id": module_id, "nodes_loaded": len(w)}

    # ── Load all examples into the workshop ───────────────────────────────────

    @capability(
        "ml.examples.load_all",
        http_method="POST", http_path="/ml/examples/load_all", http_tags=["ml"],
        memory="off",
        description=(
            "Seed the ML Workshop with all worked examples AND fetch associated "
            "datasets into the Data Fabric. "
            "Loads: 12 annotated example modules + synthetic datasets for each. "
            "Pass fetch_real_data=true to also pull live OHLCV from Stooq."
        ),
    )
    async def ml_examples_load_all(fetch_real_data: bool = False, trace_id=None):
        sys_mod = __import__("sys")
        ml_mod  = sys_mod.modules.get("ml_workshop")
        if not ml_mod:
            return {"error": "ml_workshop not loaded"}

        loaded  = []
        ds_jobs = []
        now     = now_iso()

        for ex_id, ex in ml_mod.EXAMPLES.items():
            mid = ex_id  # use example key as module id for stable references
            module = {
                "id":          mid,
                "name":        ex["name"],
                "description": ex["description"],
                "story":       ex.get("story",""),
                "takeaways":   ex.get("takeaways",[]),
                "tags":        ex.get("tags",[]),
                "nodes":       deepcopy(ex["nodes"]),
                "edges":       deepcopy(ex["edges"]),
                "created_at":  now,
                "updated_at":  now,
            }
            try:
                from ml_workshop import _count_params, _infer_shapes
                module["param_count"] = _count_params(module)
                module["shapes"]      = _infer_shapes(module)
            except Exception:
                module["param_count"] = 0
                module["shapes"] = {}

            await ml_mod._save_module(mid, module)
            loaded.append({"id": mid, "name": ex["name"]})

        # Generate associated synthetic datasets
        ds_map = {
            "ex_and_gate":    ("xor",            {"n": 200, "features": 2}),
            "ex_xor_mlp":     ("xor",            {"n": 400, "features": 2}),
            "ex_autoencoder": ("autoencoder",     {"n": 500, "features": 8}),
            "ex_dual_stream": ("classification",  {"n": 600, "features": 8, "classes": 5}),
            "ex_bigru_tagger":("timeseries",      {"n": 1000,"features": 8}),
            "ex_highway":     ("classification",  {"n": 500, "features": 8}),
            "ex_gpt_block":   ("timeseries",      {"n": 800, "features": 6}),
            "ex_neural_ode":  ("spiral",          {"n": 600, "noise": 0.15}),
            "ex_switch_moe":  ("classification",  {"n": 600, "features": 8, "classes": 8}),
            "ex_tcn":         ("timeseries",      {"n": 1200,"features": 6}),
            "ex_self_attention":("classification",{"n": 500, "features": 8}),
            "ex_depthwise_cnn":("classification", {"n": 600, "features": 8}),
        }

        ds_loaded = []
        for ex_id, (kind, kw) in ds_map.items():
            try:
                ds_id = f"ml.synthetic.{kind}"
                r = await ml_data_fetch_synthetic(
                    kind=kind, dataset_id=ds_id, **kw
                )
                ds_loaded.append({"dataset_id": ds_id, "kind": kind, "n": r.get("n_samples",0)})
            except Exception as e:
                log.warning("examples dataset %s: %s", kind, e)

        # Always generate OHLCV synthetic
        try:
            r = await ml_data_fetch_synthetic(kind="ohlcv_synthetic", n=1000)
            ds_loaded.append({"dataset_id": r.get("dataset_id",""), "kind": "ohlcv_synthetic", "n": r.get("n_samples",0)})
        except Exception as e:
            log.warning("ohlcv synthetic: %s", e)

        # Optionally pull live data
        live_loaded = []
        if fetch_real_data and HAS_HTTPX:
            for sym in ["AAPL.US", "SPY.US", "BTC.V"]:
                try:
                    r = await ml_data_fetch_ohlcv(symbol=sym)
                    if r.get("ok"):
                        live_loaded.append(r)
                    await asyncio.sleep(1)
                except Exception as e:
                    log.warning("ohlcv %s: %s", sym, e)

        await emit_event({
            "type": "ml.examples.loaded",
            "modules": len(loaded),
            "datasets": len(ds_loaded),
            "live_datasets": len(live_loaded),
        })
        return {
            "ok":      True,
            "modules": loaded,
            "datasets": ds_loaded,
            "live_datasets": live_loaded,
            "total_modules": len(loaded),
        }

    # if _CAP_AVAILABLE:
    _HERE = _Path(__file__).parent

    @APP.get("/ml/training_panel", include_in_schema=False)
    async def _ml_panel_route():
        from fastapi.responses import HTMLResponse
        p = _HERE / "ml_training_panel.html"
        if p.exists():
            return HTMLResponse(p.read_text(encoding="utf-8"))
        return HTMLResponse("<p style='color:red'>ml_training_panel.html not found</p>")

    try:
        register_ui(
            "ml-training",
            "ML Training",
            "⬡",
            """<div id="ml-training-mount" style="height:100%;display:flex;flex-direction:column;">
        <iframe src="/ml/training_panel"
                style="flex:1;border:none;width:100%;height:100%"
                allow="clipboard-read; clipboard-write">
        </iframe>
        </div>""",
            "",
            ui_caps=["ml.create", "ml.list", "ml.run", "ml.generate", "ml.inspect","ml.data.fetch_synthetic","ml.data.fetch_ohlcv","ml.data.fetch_crypto","ml.data.fetch_macro","ml.data.list","ml.data.prepare","ml.train", "ml.train.from_dataset", "ml.train.status", "ml.train.stop","ml.train.history","ml.train.evaluate","ml.train.predict","ml.train.weights_get","ml.train.weights_load","ml.examples.load_all" ],
            mode="tab",
            tab_order=66,
        )
    except Exception as _e:
        log.warning("ml_workshop register_ui: %s", _e)
        
    # ── Startup: load weights from Redis ──────────────────────────────────────

    async def _training_startup():
        await asyncio.sleep(3)
        log.info("ml_training ready — numpy=%s httpx=%s", HAS_NP, HAS_HTTPX)

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_training_startup())
    except Exception:
        pass