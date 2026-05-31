"""
ml_workshop.py  —  Vera ML Workshop
=====================================
A live neural network construction and execution sandbox.

Architecture
────────────
Every ML "module" is a JSON-serialised graph of compute nodes:
  - Layers   : Dense, Conv2D, RNN, LSTM, Attention, Embedding, Norm, Dropout
  - Ops      : Add, Mul, Concat, Split, Reshape, Transpose, Softmax, etc.
  - Activations : ReLU, GELU, Swish, Sigmoid, Tanh, SiLU, custom
  - Perceptrons : single / multi-layer (the fundamental building block)
  - Ensembles  : MoE, Bagging, Stacking, Boosting metacompositions
  - Exotic     : Hopfield, Reservoir/ESN, CapsNet, Kolmogorov-Arnold

Modules are stored in Redis (hot) and optionally Postgres (cold).
They can be:
  - Built interactively via the panel
  - Generated from a natural language description via LLM
  - Assembled programmatically via caps
  - Called as caps: ml.run(<module_id>, inputs)
  - Composed into pipelines via DAG

Execution
─────────
Pure Python + NumPy (always available).  Optional PyTorch/JAX when
available — the executor auto-detects and chooses the best backend.

No training infrastructure in v1 — forward pass + introspection only.
Training hooks are scaffolded and ready for extension.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
import uuid
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("vera.ml_workshop")

# ── Optional numerical backends ──────────────────────────────────────────────
try:
    import numpy as np
    HAS_NP = True
except ImportError:
    np = None
    HAS_NP = False

try:
    import torch
    import torch.nn as tnn
    HAS_TORCH = True
except ImportError:
    torch = None
    tnn = None
    HAS_TORCH = False

# ── Vera imports ──────────────────────────────────────────────────────────────
try:
    from Vera.Orchestration.capability_orchestration import (
        APP, CAPABILITY_REGISTRY, capability, emit_event, now_iso,
        ollama_generate, schedule, register_ui,
    )
    from pathlib import Path as _Path
    _CAP_AVAILABLE = True
except ImportError:
    _CAP_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# MODULE STORE  (in-process dict; persisted to Redis)
# ─────────────────────────────────────────────────────────────────────────────

_MODULES: Dict[str, dict] = {}          # module_id → module_def
_EXEC_CACHE: Dict[str, Any] = {}        # module_id → compiled executor
REDIS_PREFIX = "vera:ml:"

# ─────────────────────────────────────────────────────────────────────────────
# LAYER CATALOGUE
# ─────────────────────────────────────────────────────────────────────────────

LAYER_CATALOGUE = {
    # ── Fundamental ──────────────────────────────────────────────────────────
    "perceptron": {
        "family": "fundamental",
        "desc": "Single perceptron: weighted sum + bias + activation",
        "params": {"inputs": 1, "activation": "step"},
        "icon": "⬟",
    },
    "dense": {
        "family": "fundamental",
        "desc": "Fully-connected linear layer  y = xW + b",
        "params": {"in_features": 64, "out_features": 64, "bias": True},
        "icon": "▦",
    },
    "mlp": {
        "family": "fundamental",
        "desc": "Multi-layer perceptron (stack of Dense + activation)",
        "params": {"layers": [64, 64], "activation": "relu", "dropout": 0.0},
        "icon": "⬡",
    },
    # ── Activations ──────────────────────────────────────────────────────────
    "activation": {
        "family": "activation",
        "desc": "Standalone activation function",
        "params": {"fn": "relu"},
        "icon": "ƒ",
    },
    # ── Convolutional ────────────────────────────────────────────────────────
    "conv1d": {
        "family": "conv",
        "desc": "1-D convolution over sequence",
        "params": {"in_channels": 1, "out_channels": 16, "kernel": 3, "stride": 1, "padding": 1},
        "icon": "≋",
    },
    "conv2d": {
        "family": "conv",
        "desc": "2-D spatial convolution",
        "params": {"in_channels": 3, "out_channels": 32, "kernel": 3, "stride": 1, "padding": 1},
        "icon": "⊞",
    },
    "depthwise_conv": {
        "family": "conv",
        "desc": "Depthwise-separable convolution (MobileNet style)",
        "params": {"channels": 32, "kernel": 3},
        "icon": "⊟",
    },
    # ── Pooling ──────────────────────────────────────────────────────────────
    "pool": {
        "family": "pooling",
        "desc": "Pooling (max / avg / global)",
        "params": {"mode": "max", "kernel": 2, "stride": 2},
        "icon": "⬇",
    },
    # ── Recurrent ────────────────────────────────────────────────────────────
    "rnn": {
        "family": "recurrent",
        "desc": "Vanilla recurrent unit  h_t = tanh(Wx_t + Uh_{t-1} + b)",
        "params": {"input_size": 64, "hidden_size": 128, "layers": 1, "bidirectional": False},
        "icon": "↻",
    },
    "lstm": {
        "family": "recurrent",
        "desc": "Long short-term memory cell",
        "params": {"input_size": 64, "hidden_size": 128, "layers": 1, "bidirectional": False},
        "icon": "⊛",
    },
    "gru": {
        "family": "recurrent",
        "desc": "Gated recurrent unit (lighter than LSTM)",
        "params": {"input_size": 64, "hidden_size": 128, "layers": 1},
        "icon": "⊕",
    },
    # ── Attention ────────────────────────────────────────────────────────────
    "attention": {
        "family": "attention",
        "desc": "Scaled dot-product attention",
        "params": {"dim": 64, "heads": 1},
        "icon": "◎",
    },
    "multi_head_attention": {
        "family": "attention",
        "desc": "Multi-head self/cross attention",
        "params": {"d_model": 128, "heads": 8, "dropout": 0.1},
        "icon": "⊚",
    },
    "transformer_block": {
        "family": "attention",
        "desc": "Full transformer block: MHA + FFN + LayerNorm + residuals",
        "params": {"d_model": 128, "heads": 8, "ffn_dim": 512, "dropout": 0.1},
        "icon": "⟁",
    },
    # ── Normalisation ────────────────────────────────────────────────────────
    "layer_norm": {
        "family": "norm",
        "desc": "Layer normalisation",
        "params": {"dim": 64, "eps": 1e-5},
        "icon": "≈",
    },
    "batch_norm": {
        "family": "norm",
        "desc": "Batch normalisation",
        "params": {"features": 64, "momentum": 0.1},
        "icon": "≡",
    },
    "rms_norm": {
        "family": "norm",
        "desc": "RMS Norm (used in LLaMA / Gemma)",
        "params": {"dim": 64, "eps": 1e-8},
        "icon": "⊜",
    },
    # ── Regularisation ───────────────────────────────────────────────────────
    "dropout": {
        "family": "regularisation",
        "desc": "Stochastic dropout",
        "params": {"p": 0.1},
        "icon": "⊘",
    },
    # ── Embedding ────────────────────────────────────────────────────────────
    "embedding": {
        "family": "embedding",
        "desc": "Lookup embedding table",
        "params": {"vocab_size": 1000, "dim": 64},
        "icon": "⊗",
    },
    "positional_encoding": {
        "family": "embedding",
        "desc": "Sinusoidal positional encoding (Transformer)",
        "params": {"dim": 64, "max_len": 512},
        "icon": "∿",
    },
    "rotary_embedding": {
        "family": "embedding",
        "desc": "Rotary position embedding (RoPE — LLaMA style)",
        "params": {"dim": 64, "max_len": 2048},
        "icon": "↺",
    },
    # ── Structural ops ───────────────────────────────────────────────────────
    "residual": {
        "family": "structural",
        "desc": "Residual (skip) connection wrapper",
        "params": {"dim": 64},
        "icon": "⥃",
    },
    "concat": {
        "family": "structural",
        "desc": "Concatenate multiple tensors along a dim",
        "params": {"dim": -1},
        "icon": "⟂",
    },
    "add": {
        "family": "structural",
        "desc": "Element-wise add (for residuals / branches)",
        "params": {},
        "icon": "+",
    },
    "reshape": {
        "family": "structural",
        "desc": "Reshape / flatten tensor",
        "params": {"shape": [-1]},
        "icon": "⬜",
    },
    "linear_probe": {
        "family": "structural",
        "desc": "Final classification / regression head",
        "params": {"in_features": 64, "out_features": 10, "task": "classification"},
        "icon": "▷",
    },
    # ── Exotic ───────────────────────────────────────────────────────────────
    "hopfield": {
        "family": "exotic",
        "desc": "Modern Hopfield / dense associative memory",
        "params": {"dim": 64, "beta": 1.0, "patterns": 128},
        "icon": "⌬",
    },
    "reservoir": {
        "family": "exotic",
        "desc": "Echo State Network reservoir (fixed random recurrence)",
        "params": {"input_dim": 32, "reservoir_dim": 256, "spectral_radius": 0.95, "sparsity": 0.1},
        "icon": "〜",
    },
    "capsule": {
        "family": "exotic",
        "desc": "Capsule layer with dynamic routing",
        "params": {"in_caps": 32, "out_caps": 10, "in_dim": 8, "out_dim": 16, "routing_iters": 3},
        "icon": "⬡",
    },
    "moe_router": {
        "family": "exotic",
        "desc": "Mixture-of-Experts sparse gating router",
        "params": {"num_experts": 8, "top_k": 2, "dim": 64},
        "icon": "⬨",
    },
    "kan_layer": {
        "family": "exotic",
        "desc": "Kolmogorov-Arnold Network layer (learnable splines on edges)",
        "params": {"in_features": 32, "out_features": 32, "grid": 5, "order": 3},
        "icon": "⌇",
    },
    "fourier_layer": {
        "family": "exotic",
        "desc": "Fourier Neural Operator layer",
        "params": {"in_channels": 32, "out_channels": 32, "modes": 16},
        "icon": "∿",
    },
    "state_space": {
        "family": "exotic",
        "desc": "Linear State Space Model (SSM / Mamba-style)",
        "params": {"d_model": 64, "d_state": 16, "d_conv": 4, "expand": 2},
        "icon": "⊞",
    },
    "neural_ode": {
        "family": "exotic",
        "desc": "Neural ODE: continuous-depth via ODE solver",
        "params": {"dim": 64, "solver": "euler", "steps": 10},
        "icon": "∂",
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE ARCHITECTURES
# ─────────────────────────────────────────────────────────────────────────────

TEMPLATES = {
    "perceptron": {
        "name": "Single Perceptron",
        "desc": "The most fundamental unit — weighted inputs, bias, activation",
        "nodes": [
            {"id": "inp", "type": "input", "params": {"shape": [4]}, "pos": [80, 200]},
            {"id": "p0", "type": "perceptron", "params": {"inputs": 4, "activation": "step"}, "pos": [280, 200]},
        ],
        "edges": [{"from": "inp", "to": "p0"}],
    },
    "mlp_classifier": {
        "name": "MLP Classifier",
        "desc": "Classic multi-layer perceptron for classification",
        "nodes": [
            {"id": "inp", "type": "input", "params": {"shape": [64]}, "pos": [80, 200]},
            {"id": "d0", "type": "dense", "params": {"in_features": 64, "out_features": 128}, "pos": [240, 200]},
            {"id": "a0", "type": "activation", "params": {"fn": "relu"}, "pos": [400, 200]},
            {"id": "d1", "type": "dense", "params": {"in_features": 128, "out_features": 64}, "pos": [560, 200]},
            {"id": "a1", "type": "activation", "params": {"fn": "relu"}, "pos": [720, 200]},
            {"id": "d2", "type": "dense", "params": {"in_features": 64, "out_features": 10}, "pos": [880, 200]},
            {"id": "out", "type": "output", "params": {"shape": [10], "activation": "softmax"}, "pos": [1040, 200]},
        ],
        "edges": [
            {"from": "inp", "to": "d0"}, {"from": "d0", "to": "a0"},
            {"from": "a0", "to": "d1"}, {"from": "d1", "to": "a1"},
            {"from": "a1", "to": "d2"}, {"from": "d2", "to": "out"},
        ],
    },
    "transformer_block": {
        "name": "Transformer Block",
        "desc": "Single transformer layer with MHA + FFN + residuals",
        "nodes": [
            {"id": "inp", "type": "input", "params": {"shape": [16, 128]}, "pos": [80, 240]},
            {"id": "ln1", "type": "layer_norm", "params": {"dim": 128}, "pos": [240, 240]},
            {"id": "mha", "type": "multi_head_attention", "params": {"d_model": 128, "heads": 8}, "pos": [420, 240]},
            {"id": "add1", "type": "add", "params": {}, "pos": [600, 240]},
            {"id": "ln2", "type": "layer_norm", "params": {"dim": 128}, "pos": [760, 240]},
            {"id": "ffn", "type": "mlp", "params": {"layers": [512, 128], "activation": "gelu"}, "pos": [920, 240]},
            {"id": "add2", "type": "add", "params": {}, "pos": [1080, 240]},
            {"id": "out", "type": "output", "params": {"shape": [16, 128]}, "pos": [1240, 240]},
        ],
        "edges": [
            {"from": "inp", "to": "ln1"}, {"from": "ln1", "to": "mha"},
            {"from": "mha", "to": "add1"}, {"from": "inp", "to": "add1", "skip": True},
            {"from": "add1", "to": "ln2"}, {"from": "ln2", "to": "ffn"},
            {"from": "ffn", "to": "add2"}, {"from": "add1", "to": "add2", "skip": True},
            {"from": "add2", "to": "out"},
        ],
    },
    "lstm_seq2seq": {
        "name": "LSTM Sequence Model",
        "desc": "Stacked LSTM for sequence processing",
        "nodes": [
            {"id": "inp", "type": "input", "params": {"shape": [32, 64]}, "pos": [80, 200]},
            {"id": "emb", "type": "embedding", "params": {"vocab_size": 10000, "dim": 64}, "pos": [240, 200]},
            {"id": "lstm0", "type": "lstm", "params": {"input_size": 64, "hidden_size": 256, "layers": 2}, "pos": [440, 200]},
            {"id": "dp", "type": "dropout", "params": {"p": 0.2}, "pos": [640, 200]},
            {"id": "head", "type": "linear_probe", "params": {"in_features": 256, "out_features": 10}, "pos": [820, 200]},
            {"id": "out", "type": "output", "params": {"shape": [10]}, "pos": [1000, 200]},
        ],
        "edges": [
            {"from": "inp", "to": "emb"}, {"from": "emb", "to": "lstm0"},
            {"from": "lstm0", "to": "dp"}, {"from": "dp", "to": "head"},
            {"from": "head", "to": "out"},
        ],
    },
    "resnet_block": {
        "name": "ResNet Block",
        "desc": "Residual convolutional block (skip connection)",
        "nodes": [
            {"id": "inp", "type": "input", "params": {"shape": [32, 32, 32]}, "pos": [80, 200]},
            {"id": "bn1", "type": "batch_norm", "params": {"features": 32}, "pos": [240, 200]},
            {"id": "a1", "type": "activation", "params": {"fn": "relu"}, "pos": [380, 200]},
            {"id": "c1", "type": "conv2d", "params": {"in_channels": 32, "out_channels": 32, "kernel": 3}, "pos": [520, 200]},
            {"id": "bn2", "type": "batch_norm", "params": {"features": 32}, "pos": [680, 200]},
            {"id": "a2", "type": "activation", "params": {"fn": "relu"}, "pos": [820, 200]},
            {"id": "c2", "type": "conv2d", "params": {"in_channels": 32, "out_channels": 32, "kernel": 3}, "pos": [960, 200]},
            {"id": "add", "type": "add", "params": {}, "pos": [1100, 200]},
            {"id": "out", "type": "output", "params": {"shape": [32, 32, 32]}, "pos": [1260, 200]},
        ],
        "edges": [
            {"from": "inp", "to": "bn1"}, {"from": "bn1", "to": "a1"},
            {"from": "a1", "to": "c1"}, {"from": "c1", "to": "bn2"},
            {"from": "bn2", "to": "a2"}, {"from": "a2", "to": "c2"},
            {"from": "c2", "to": "add"}, {"from": "inp", "to": "add", "skip": True},
            {"from": "add", "to": "out"},
        ],
    },
    "moe": {
        "name": "Mixture of Experts",
        "desc": "Sparse MoE layer with top-k routing",
        "nodes": [
            {"id": "inp", "type": "input", "params": {"shape": [64]}, "pos": [80, 200]},
            {"id": "router", "type": "moe_router", "params": {"num_experts": 4, "top_k": 2, "dim": 64}, "pos": [280, 200]},
            {"id": "e0", "type": "dense", "params": {"in_features": 64, "out_features": 64}, "pos": [480, 80]},
            {"id": "e1", "type": "dense", "params": {"in_features": 64, "out_features": 64}, "pos": [480, 180]},
            {"id": "e2", "type": "dense", "params": {"in_features": 64, "out_features": 64}, "pos": [480, 280]},
            {"id": "e3", "type": "dense", "params": {"in_features": 64, "out_features": 64}, "pos": [480, 380]},
            {"id": "merge", "type": "add", "params": {}, "pos": [680, 200]},
            {"id": "out", "type": "output", "params": {"shape": [64]}, "pos": [860, 200]},
        ],
        "edges": [
            {"from": "inp", "to": "router"},
            {"from": "router", "to": "e0"}, {"from": "router", "to": "e1"},
            {"from": "router", "to": "e2"}, {"from": "router", "to": "e3"},
            {"from": "e0", "to": "merge"}, {"from": "e1", "to": "merge"},
            {"from": "e2", "to": "merge"}, {"from": "e3", "to": "merge"},
            {"from": "merge", "to": "out"},
        ],
    },
    "esn": {
        "name": "Echo State Network",
        "desc": "Reservoir computing: fixed random recurrent dynamics",
        "nodes": [
            {"id": "inp", "type": "input", "params": {"shape": [16]}, "pos": [80, 200]},
            {"id": "res", "type": "reservoir", "params": {"input_dim": 16, "reservoir_dim": 256, "spectral_radius": 0.95}, "pos": [280, 200]},
            {"id": "read", "type": "linear_probe", "params": {"in_features": 256, "out_features": 8, "task": "regression"}, "pos": [520, 200]},
            {"id": "out", "type": "output", "params": {"shape": [8]}, "pos": [700, 200]},
        ],
        "edges": [
            {"from": "inp", "to": "res"}, {"from": "res", "to": "read"}, {"from": "read", "to": "out"},
        ],
    },
    "hopfield_memory": {
        "name": "Hopfield Associative Memory",
        "desc": "Modern Hopfield network for pattern retrieval",
        "nodes": [
            {"id": "query", "type": "input", "params": {"shape": [64], "label": "Query"}, "pos": [80, 160]},
            {"id": "patterns", "type": "input", "params": {"shape": [128, 64], "label": "Stored Patterns"}, "pos": [80, 320]},
            {"id": "hop", "type": "hopfield", "params": {"dim": 64, "beta": 1.0, "patterns": 128}, "pos": [340, 240]},
            {"id": "out", "type": "output", "params": {"shape": [64]}, "pos": [580, 240]},
        ],
        "edges": [
            {"from": "query", "to": "hop"}, {"from": "patterns", "to": "hop"}, {"from": "hop", "to": "out"},
        ],
    },
    "kan": {
        "name": "Kolmogorov-Arnold Network",
        "desc": "KAN: learnable spline activations on edges instead of nodes",
        "nodes": [
            {"id": "inp", "type": "input", "params": {"shape": [32]}, "pos": [80, 200]},
            {"id": "k0", "type": "kan_layer", "params": {"in_features": 32, "out_features": 32, "grid": 5}, "pos": [280, 200]},
            {"id": "k1", "type": "kan_layer", "params": {"in_features": 32, "out_features": 16, "grid": 5}, "pos": [480, 200]},
            {"id": "k2", "type": "kan_layer", "params": {"in_features": 16, "out_features": 1, "grid": 5}, "pos": [680, 200]},
            {"id": "out", "type": "output", "params": {"shape": [1]}, "pos": [860, 200]},
        ],
        "edges": [
            {"from": "inp", "to": "k0"}, {"from": "k0", "to": "k1"},
            {"from": "k1", "to": "k2"}, {"from": "k2", "to": "out"},
        ],
    },
    "ssm": {
        "name": "State Space Model (Mamba-style)",
        "desc": "Linear SSM selective scan — the foundation of Mamba",
        "nodes": [
            {"id": "inp", "type": "input", "params": {"shape": [32, 64]}, "pos": [80, 200]},
            {"id": "ssm0", "type": "state_space", "params": {"d_model": 64, "d_state": 16, "expand": 2}, "pos": [280, 200]},
            {"id": "ssm1", "type": "state_space", "params": {"d_model": 64, "d_state": 16, "expand": 2}, "pos": [500, 200]},
            {"id": "out", "type": "output", "params": {"shape": [32, 64]}, "pos": [720, 200]},
        ],
        "edges": [
            {"from": "inp", "to": "ssm0"}, {"from": "ssm0", "to": "ssm1"}, {"from": "ssm1", "to": "out"},
        ],
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# WORKED EXAMPLES
# Each entry is a fully annotated module that ships pre-loaded into the store.
# "story" is shown in the panel as a walkthrough; "takeaways" are bullet points.
# ─────────────────────────────────────────────────────────────────────────────

EXAMPLES: Dict[str, dict] = {

    # ── 01 ── The AND gate perceptron ────────────────────────────────────────
    "ex_and_gate": {
        "name": "Example: AND Gate Perceptron",
        "description": (
            "The simplest possible learnable computation. "
            "Two binary inputs, one perceptron, step activation. "
            "This is the exact circuit McCulloch & Pitts described in 1943."
        ),
        "story": (
            "A perceptron computes: output = step(w1*x1 + w2*x2 + b).\n"
            "For AND: we need output=1 only when BOTH inputs are 1.\n"
            "Weights [0.5, 0.5] and bias -0.75 achieve this exactly.\n\n"
            "Truth table:\n"
            "  [0,0] → step(-0.75)     = 0  ✓\n"
            "  [1,0] → step(0.5-0.75)  = 0  ✓\n"
            "  [0,1] → step(0.5-0.75)  = 0  ✓\n"
            "  [1,1] → step(1.0-0.75)  = 1  ✓\n\n"
            "XOR cannot be solved by a single perceptron — that requires an MLP.\n"
            "This limitation, discovered by Minsky & Papert in 1969, triggered\n"
            "the first AI winter and eventually motivated deep learning."
        ),
        "takeaways": [
            "A perceptron is a linear classifier: it draws a hyperplane in input space",
            "The step function makes it a hard binary classifier",
            "Single perceptrons cannot solve non-linearly-separable problems (XOR)",
            "Stack perceptrons into layers → MLP → universal approximation",
        ],
        "tags": ["example", "fundamental", "perceptron", "beginner"],
        "nodes": [
            {"id": "inp", "type": "input",      "params": {"shape": [2], "label": "x1, x2"},   "pos": [80, 200]},
            {"id": "p0",  "type": "perceptron", "params": {"inputs": 2, "activation": "step"}, "pos": [300, 200]},
            {"id": "out", "type": "output",     "params": {"shape": [1]},                       "pos": [500, 200]},
        ],
        "edges": [{"from": "inp", "to": "p0"}, {"from": "p0", "to": "out"}],
    },

    # ── 02 ── XOR solved by a 2-layer MLP ────────────────────────────────────
    "ex_xor_mlp": {
        "name": "Example: XOR via 2-Layer MLP",
        "description": (
            "XOR is the canonical example that proves a single perceptron is insufficient. "
            "A hidden layer with 2 neurons solves it. "
            "Demonstrates why depth matters."
        ),
        "story": (
            "XOR: output 1 iff inputs differ.\n"
            "  [0,0]→0, [0,1]→1, [1,0]→1, [1,1]→0\n\n"
            "No straight line can separate the two classes in 2D input space —\n"
            "the problem is not linearly separable.\n\n"
            "Solution: add a hidden layer. The first layer projects into a new\n"
            "feature space where the classes ARE separable. The second layer\n"
            "then draws the linear boundary there.\n\n"
            "Hidden layer (2 neurons, ReLU) learns:\n"
            "  h1 = ReLU(x1 + x2 - 0.5)   ← 'at least one is 1'\n"
            "  h2 = ReLU(x1 + x2 - 1.5)   ← 'both are 1'\n\n"
            "Output: sigmoid(h1 - h2 - 0.5) ≈ XOR\n\n"
            "This shows that 1 hidden layer can solve any boolean function.\n"
            "The Universal Approximation Theorem generalises this to continuous functions."
        ),
        "takeaways": [
            "Hidden layers enable non-linear decision boundaries",
            "2 hidden neurons suffice for XOR — depth is more efficient than width",
            "ReLU hidden + sigmoid output is the minimal XOR solver",
            "Universal Approximation: 1 hidden layer can approximate any function",
        ],
        "tags": ["example", "mlp", "xor", "fundamental", "beginner"],
        "nodes": [
            {"id": "inp",  "type": "input",      "params": {"shape": [2]},                                   "pos": [80,  200]},
            {"id": "h0",   "type": "dense",      "params": {"in_features": 2, "out_features": 4},            "pos": [280, 200]},
            {"id": "a0",   "type": "activation", "params": {"fn": "relu"},                                   "pos": [440, 200]},
            {"id": "h1",   "type": "dense",      "params": {"in_features": 4, "out_features": 1},            "pos": [600, 200]},
            {"id": "a1",   "type": "activation", "params": {"fn": "sigmoid"},                                "pos": [760, 200]},
            {"id": "out",  "type": "output",     "params": {"shape": [1]},                                   "pos": [920, 200]},
        ],
        "edges": [
            {"from": "inp", "to": "h0"}, {"from": "h0", "to": "a0"},
            {"from": "a0",  "to": "h1"}, {"from": "h1", "to": "a1"},
            {"from": "a1",  "to": "out"},
        ],
    },

    # ── 03 ── Autoencoder ────────────────────────────────────────────────────
    "ex_autoencoder": {
        "name": "Example: Bottleneck Autoencoder",
        "description": (
            "An encoder compresses 128-d input to a 16-d latent code; "
            "a decoder reconstructs the original. The bottleneck forces the "
            "network to learn compact representations — the foundation of VAEs, "
            "dimensionality reduction, and anomaly detection."
        ),
        "story": (
            "Architecture: 128 → 64 → 32 → 16 (bottleneck) → 32 → 64 → 128\n\n"
            "The encoder is a funnel: each layer compresses information.\n"
            "The bottleneck (16-d) is the learned representation — latent space.\n"
            "The decoder is the reverse funnel: it reconstructs from the code.\n\n"
            "Training objective: minimise reconstruction loss ||x - x̂||²\n\n"
            "Applications:\n"
            "  • Denoising: corrupt input → reconstruct clean version\n"
            "  • Anomaly detection: high reconstruction error = anomaly\n"
            "  • Compression: store only the latent code\n"
            "  • Pre-training: use encoder as feature extractor for downstream tasks\n\n"
            "Variational Autoencoder (VAE) adds a KL-divergence term so the latent\n"
            "space is smooth and generative (can sample novel examples)."
        ),
        "takeaways": [
            "Bottleneck architecture forces compact feature learning",
            "Encoder = compressor, decoder = reconstructor, latent = representation",
            "Reconstruction loss teaches the network what features matter most",
            "Basis for VAE, β-VAE, VQ-VAE, and modern image generation",
        ],
        "tags": ["example", "autoencoder", "representation", "intermediate"],
        "nodes": [
            {"id": "inp",  "type": "input",      "params": {"shape": [128]},                                   "pos": [80,  240]},
            {"id": "enc1", "type": "dense",      "params": {"in_features": 128, "out_features": 64},          "pos": [260, 240]},
            {"id": "ae1",  "type": "activation", "params": {"fn": "relu"},                                    "pos": [420, 240]},
            {"id": "enc2", "type": "dense",      "params": {"in_features": 64,  "out_features": 32},          "pos": [560, 240]},
            {"id": "ae2",  "type": "activation", "params": {"fn": "relu"},                                    "pos": [720, 240]},
            {"id": "enc3", "type": "dense",      "params": {"in_features": 32,  "out_features": 16},          "pos": [860, 240]},
            {"id": "lat",  "type": "layer_norm", "params": {"dim": 16},                                       "pos": [1020,240]},
            {"id": "dec1", "type": "dense",      "params": {"in_features": 16,  "out_features": 32},          "pos": [1180,240]},
            {"id": "ad1",  "type": "activation", "params": {"fn": "relu"},                                    "pos": [1340,240]},
            {"id": "dec2", "type": "dense",      "params": {"in_features": 32,  "out_features": 64},          "pos": [1500,240]},
            {"id": "ad2",  "type": "activation", "params": {"fn": "relu"},                                    "pos": [1660,240]},
            {"id": "dec3", "type": "dense",      "params": {"in_features": 64,  "out_features": 128},         "pos": [1820,240]},
            {"id": "out",  "type": "output",     "params": {"shape": [128], "activation": "sigmoid"},         "pos": [1980,240]},
        ],
        "edges": [
            {"from": "inp",  "to": "enc1"}, {"from": "enc1", "to": "ae1"},
            {"from": "ae1",  "to": "enc2"}, {"from": "enc2", "to": "ae2"},
            {"from": "ae2",  "to": "enc3"}, {"from": "enc3", "to": "lat"},
            {"from": "lat",  "to": "dec1"}, {"from": "dec1", "to": "ad1"},
            {"from": "ad1",  "to": "dec2"}, {"from": "dec2", "to": "ad2"},
            {"from": "ad2",  "to": "dec3"}, {"from": "dec3", "to": "out"},
        ],
    },

    # ── 04 ── Attention is all you need (self-attention head) ────────────────
    "ex_self_attention": {
        "name": "Example: Scaled Dot-Product Self-Attention",
        "description": (
            "The core operation of every modern transformer. "
            "A single attention head with Q/K/V projections, softmax scoring, "
            "and a residual connection. Shows exactly how tokens 'look at' each other."
        ),
        "story": (
            "Self-attention: each position queries all others, weights their values.\n\n"
            "Step by step:\n"
            "  1. Project input X into Q, K, V via learned weight matrices\n"
            "  2. Compute attention scores: A = softmax(QKᵀ / √d_k)\n"
            "  3. Weighted sum: output = A · V\n"
            "  4. Residual: output = X + Attention(X)\n\n"
            "Why divide by √d_k?\n"
            "  For large d_k, dot products grow large → softmax saturates →\n"
            "  gradients vanish. √d_k keeps the variance of QKᵀ ≈ 1.\n\n"
            "The residual connection:\n"
            "  Allows gradients to flow directly back through the skip path,\n"
            "  enabling training of very deep networks (100+ layers).\n\n"
            "Multi-head attention runs H independent heads in parallel,\n"
            "each learning different relationship types (syntax, coreference, etc.)."
        ),
        "takeaways": [
            "Q=query (what am I looking for?), K=key (what do I offer?), V=value (what do I say?)",
            "Attention score = compatibility between query and all keys",
            "Softmax turns scores into a probability distribution over positions",
            "Residual connection is essential for training depth beyond ~6 layers",
        ],
        "tags": ["example", "attention", "transformer", "intermediate"],
        "nodes": [
            {"id": "inp",  "type": "input",              "params": {"shape": [16, 64]},              "pos": [80,  240]},
            {"id": "ln",   "type": "layer_norm",         "params": {"dim": 64},                     "pos": [260, 240]},
            {"id": "mha",  "type": "multi_head_attention","params": {"d_model": 64, "heads": 4},    "pos": [460, 240]},
            {"id": "dp",   "type": "dropout",            "params": {"p": 0.1},                      "pos": [680, 240]},
            {"id": "add",  "type": "add",                "params": {},                              "pos": [860, 240]},
            {"id": "out",  "type": "output",             "params": {"shape": [16, 64]},             "pos": [1040,240]},
        ],
        "edges": [
            {"from": "inp", "to": "ln"},
            {"from": "ln",  "to": "mha"},
            {"from": "mha", "to": "dp"},
            {"from": "dp",  "to": "add"},
            {"from": "inp", "to": "add", "skip": True},
            {"from": "add", "to": "out"},
        ],
    },

    # ── 05 ── Depthwise-separable CNN (MobileNet building block) ─────────────
    "ex_depthwise_cnn": {
        "name": "Example: Depthwise-Separable Conv (MobileNet Block)",
        "description": (
            "Depthwise convolution filters each channel independently, "
            "then a 1×1 pointwise conv mixes channels. "
            "This achieves similar accuracy to standard Conv2d with ~8-9x fewer parameters."
        ),
        "story": (
            "Standard Conv2d with C_in channels, C_out filters, kernel k:\n"
            "  Parameters = C_in × C_out × k² + C_out\n\n"
            "Depthwise separable splits this into two cheaper ops:\n"
            "  1. Depthwise: C_in filters, each applied to ONE channel\n"
            "     Params = C_in × k² + C_in\n"
            "  2. Pointwise (1×1 conv): mixes channels without spatial filtering\n"
            "     Params = C_in × C_out + C_out\n\n"
            "For C_in=32, C_out=64, k=3:\n"
            "  Standard:   32×64×9 = 18,432 params\n"
            "  Separable:  32×9 + 32×64 = 288 + 2,048 = 2,336 params  (~8x cheaper)\n\n"
            "Used in: MobileNet, Xception, EfficientNet, and many mobile-first models.\n"
            "The accuracy drop vs standard convolution is typically < 1%."
        ),
        "takeaways": [
            "Spatial filtering and channel mixing are separable operations",
            "Depthwise-separable is ~8x cheaper than standard Conv2d",
            "Key innovation enabling neural nets on mobile/edge devices",
            "BN+ReLU between the two convolutions is standard practice",
        ],
        "tags": ["example", "conv", "cnn", "efficient", "intermediate"],
        "nodes": [
            {"id": "inp",  "type": "input",          "params": {"shape": [32, 32, 32]},                                     "pos": [80,  200]},
            {"id": "dw",   "type": "depthwise_conv", "params": {"channels": 32, "kernel": 3},                               "pos": [280, 200]},
            {"id": "bn1",  "type": "batch_norm",     "params": {"features": 32},                                            "pos": [460, 200]},
            {"id": "a1",   "type": "activation",     "params": {"fn": "relu"},                                              "pos": [620, 200]},
            {"id": "pw",   "type": "conv2d",         "params": {"in_channels": 32, "out_channels": 64, "kernel": 1},       "pos": [780, 200]},
            {"id": "bn2",  "type": "batch_norm",     "params": {"features": 64},                                            "pos": [960, 200]},
            {"id": "a2",   "type": "activation",     "params": {"fn": "relu"},                                              "pos": [1120,200]},
            {"id": "out",  "type": "output",         "params": {"shape": [32, 32, 64]},                                    "pos": [1300,200]},
        ],
        "edges": [
            {"from": "inp", "to": "dw"},  {"from": "dw",  "to": "bn1"},
            {"from": "bn1", "to": "a1"},  {"from": "a1",  "to": "pw"},
            {"from": "pw",  "to": "bn2"}, {"from": "bn2", "to": "a2"},
            {"from": "a2",  "to": "out"},
        ],
    },

    # ── 06 ── Dual-stream feature fusion (multi-modal) ───────────────────────
    "ex_dual_stream": {
        "name": "Example: Dual-Stream Feature Fusion",
        "description": (
            "Two independent encoder streams process different input modalities "
            "(e.g. image features + text features), then concatenate for joint classification. "
            "The fundamental pattern behind CLIP, VQA, and all multi-modal models."
        ),
        "story": (
            "Multi-modal learning: combine two different data types.\n\n"
            "Stream A (e.g. visual): 512-d image embedding → 256-d → 128-d\n"
            "Stream B (e.g. textual): 768-d text embedding → 256-d → 128-d\n\n"
            "Fusion: concatenate → [128, 128] = 256-d joint representation\n"
            "Head: 256 → 64 → num_classes\n\n"
            "Why separate streams?\n"
            "  Each modality has its own statistics and optimal transformation.\n"
            "  Forcing them through the same layers early loses modality-specific structure.\n\n"
            "Alternative fusion strategies:\n"
            "  • Add (element-wise): requires matching dims, implicit averaging\n"
            "  • Cross-attention: tokens in stream A attend to stream B (more expressive)\n"
            "  • Concat + project: shown here — simple and effective\n\n"
            "CLIP (Contrastive Language-Image Pretraining) trains two towers\n"
            "with a contrastive loss that aligns matching image/text pairs."
        ),
        "takeaways": [
            "Separate encoders respect each modality's statistical structure",
            "Concatenation fusion is simple but effective for most tasks",
            "Cross-attention fusion is more expressive but computationally heavier",
            "This pattern underlies CLIP, ALIGN, Flamingo, GPT-4V",
        ],
        "tags": ["example", "multimodal", "fusion", "intermediate"],
        "nodes": [
            {"id": "inpA", "type": "input",      "params": {"shape": [512], "label": "Visual"},     "pos": [80,  120]},
            {"id": "eA1",  "type": "dense",      "params": {"in_features": 512, "out_features": 256},"pos": [280, 120]},
            {"id": "aA1",  "type": "activation", "params": {"fn": "relu"},                          "pos": [460, 120]},
            {"id": "eA2",  "type": "dense",      "params": {"in_features": 256, "out_features": 128},"pos": [620, 120]},
            {"id": "inpB", "type": "input",      "params": {"shape": [768], "label": "Textual"},    "pos": [80,  360]},
            {"id": "eB1",  "type": "dense",      "params": {"in_features": 768, "out_features": 256},"pos": [280, 360]},
            {"id": "aB1",  "type": "activation", "params": {"fn": "relu"},                          "pos": [460, 360]},
            {"id": "eB2",  "type": "dense",      "params": {"in_features": 256, "out_features": 128},"pos": [620, 360]},
            {"id": "cat",  "type": "concat",     "params": {"dim": -1},                             "pos": [820, 240]},
            {"id": "ln",   "type": "layer_norm", "params": {"dim": 256},                            "pos": [1000,240]},
            {"id": "head", "type": "dense",      "params": {"in_features": 256, "out_features": 64},"pos": [1160,240]},
            {"id": "ah",   "type": "activation", "params": {"fn": "gelu"},                          "pos": [1320,240]},
            {"id": "cls",  "type": "linear_probe","params": {"in_features": 64, "out_features": 10,"task":"classification"},"pos": [1480,240]},
            {"id": "out",  "type": "output",     "params": {"shape": [10], "activation": "softmax"},"pos": [1660,240]},
        ],
        "edges": [
            {"from": "inpA","to": "eA1"}, {"from": "eA1", "to": "aA1"}, {"from": "aA1", "to": "eA2"},
            {"from": "inpB","to": "eB1"}, {"from": "eB1", "to": "aB1"}, {"from": "aB1", "to": "eB2"},
            {"from": "eA2", "to": "cat"}, {"from": "eB2", "to": "cat"},
            {"from": "cat", "to": "ln"},  {"from": "ln",  "to": "head"},
            {"from": "head","to": "ah"},  {"from": "ah",  "to": "cls"},
            {"from": "cls", "to": "out"},
        ],
    },

    # ── 07 ── Bidirectional GRU for sequence labelling ───────────────────────
    "ex_bigru_tagger": {
        "name": "Example: Bidirectional GRU Sequence Tagger",
        "description": (
            "A bidirectional GRU reads a sequence forwards and backwards, "
            "concatenates both hidden states, and applies a token-level classifier. "
            "Classic architecture for NER, POS tagging, and slot filling."
        ),
        "story": (
            "For sequence labelling, each token needs context from BOTH directions.\n\n"
            "Forward GRU at position t sees: tokens 1..t\n"
            "Backward GRU at position t sees: tokens t..T\n\n"
            "Concatenate → each position has full sequence context.\n\n"
            "Pipeline:\n"
            "  Input tokens (int ids)\n"
            "  → Embedding (vocab → dense vectors)\n"
            "  → Positional encoding (inject position information)\n"
            "  → Dropout (regularise)\n"
            "  → BiGRU (bidirectional=True doubles output dim: hidden*2)\n"
            "  → Layer norm (stabilise activations)\n"
            "  → Token classifier (project to num_labels per position)\n\n"
            "Why GRU over LSTM here?\n"
            "  GRU has fewer parameters (no cell state) → faster + less prone to overfitting\n"
            "  on smaller datasets. LSTM often wins on longer, harder sequences.\n\n"
            "Modern NLP has replaced BiGRUs with transformers, but BiGRUs are still\n"
            "preferred for low-resource or latency-constrained settings."
        ),
        "takeaways": [
            "Bidirectional = run forward + backward, concat hidden states",
            "Embedding + PE is the standard input pipeline for discrete token sequences",
            "GRU is lighter than LSTM — good default for medium-length sequences",
            "Token-level classification head applies the same Dense to every time step",
        ],
        "tags": ["example", "recurrent", "gru", "nlp", "sequence", "intermediate"],
        "nodes": [
            {"id": "inp",  "type": "input",              "params": {"shape": [32]},                                      "pos": [80,  200]},
            {"id": "emb",  "type": "embedding",          "params": {"vocab_size": 5000, "dim": 64},                     "pos": [260, 200]},
            {"id": "pe",   "type": "positional_encoding","params": {"dim": 64, "max_len": 128},                          "pos": [460, 200]},
            {"id": "dp0",  "type": "dropout",            "params": {"p": 0.15},                                          "pos": [640, 200]},
            {"id": "gru",  "type": "gru",                "params": {"input_size": 64, "hidden_size": 128, "layers": 2, "bidirectional": True}, "pos": [820, 200]},
            {"id": "ln",   "type": "layer_norm",         "params": {"dim": 256},                                         "pos": [1040,200]},
            {"id": "dp1",  "type": "dropout",            "params": {"p": 0.1},                                           "pos": [1220,200]},
            {"id": "cls",  "type": "linear_probe",       "params": {"in_features": 256, "out_features": 9, "task": "classification"}, "pos": [1400,200]},
            {"id": "out",  "type": "output",             "params": {"shape": [32, 9]},                                   "pos": [1580,200]},
        ],
        "edges": [
            {"from": "inp",  "to": "emb"},  {"from": "emb",  "to": "pe"},
            {"from": "pe",   "to": "dp0"},  {"from": "dp0",  "to": "gru"},
            {"from": "gru",  "to": "ln"},   {"from": "ln",   "to": "dp1"},
            {"from": "dp1",  "to": "cls"},  {"from": "cls",  "to": "out"},
        ],
    },

    # ── 08 ── Gated residual network (highway network) ───────────────────────
    "ex_highway": {
        "name": "Example: Highway / Gated Residual Network",
        "description": (
            "A highway network uses a learnable gate to blend transformed and "
            "identity-passed activations: y = T(x)·H(x) + (1-T(x))·x. "
            "Predecessor to ResNets; shows how gating enables very deep networks."
        ),
        "story": (
            "Depth is good — but vanilla stacking causes vanishing gradients.\n\n"
            "Highway networks (Srivastava et al., 2015) add a TRANSFORM GATE T(x)\n"
            "and a CARRY GATE (1-T(x)):\n\n"
            "  y = T(x) · H(x)    +    (1-T(x)) · x\n"
            "       ↑ transform        ↑ carry (skip)\n\n"
            "When T → 0, the layer is bypassed entirely (the signal 'carries through').\n"
            "When T → 1, the layer applies full transformation.\n"
            "The gate is learned, so the network decides per-layer per-sample.\n\n"
            "This is modelled here by:\n"
            "  • Two Dense layers: H (transform) and T (gate)\n"
            "  • T uses sigmoid activation → output ∈ (0,1)\n"
            "  • We approximate the gating with add + residual skip\n\n"
            "ResNets (He et al., 2016) simplified this to T=1 always (hard skip),\n"
            "which proved equally effective and is simpler to train."
        ),
        "takeaways": [
            "Learnable gates let each layer decide how much to transform vs pass through",
            "Highway networks enabled the first 100+ layer networks before ResNets",
            "ResNets are highway networks with the gate fixed to 1 (always transform + skip)",
            "Gating patterns appear throughout modern ML: GRU, LSTM, GLU, Mamba",
        ],
        "tags": ["example", "highway", "residual", "gating", "intermediate"],
        "nodes": [
            {"id": "inp",  "type": "input",      "params": {"shape": [128]},                               "pos": [80,  200]},
            {"id": "h",    "type": "dense",      "params": {"in_features": 128, "out_features": 128},     "pos": [280, 120]},
            {"id": "ah",   "type": "activation", "params": {"fn": "relu"},                                "pos": [460, 120]},
            {"id": "t",    "type": "dense",      "params": {"in_features": 128, "out_features": 128},     "pos": [280, 300]},
            {"id": "at",   "type": "activation", "params": {"fn": "sigmoid"},                             "pos": [460, 300]},
            {"id": "add",  "type": "add",        "params": {},                                            "pos": [680, 200]},
            {"id": "ln",   "type": "layer_norm", "params": {"dim": 128},                                  "pos": [860, 200]},
            {"id": "out",  "type": "output",     "params": {"shape": [128]},                              "pos": [1040,200]},
        ],
        "edges": [
            {"from": "inp", "to": "h"},   {"from": "h",   "to": "ah"},
            {"from": "inp", "to": "t"},   {"from": "t",   "to": "at"},
            {"from": "ah",  "to": "add"}, {"from": "at",  "to": "add"},
            {"from": "inp", "to": "add", "skip": True},
            {"from": "add", "to": "ln"},  {"from": "ln",  "to": "out"},
        ],
    },

    # ── 09 ── Mini LLM decoder block (GPT-style) ─────────────────────────────
    "ex_gpt_block": {
        "name": "Example: GPT-Style Decoder Block",
        "description": (
            "One layer of a GPT-style autoregressive language model. "
            "Pre-norm transformer block with causal self-attention, GELU FFN, "
            "and RMS normalisation — the exact recipe used in LLaMA / GPT-2."
        ),
        "story": (
            "GPT uses a decoder-only transformer: no cross-attention, causal masking.\n\n"
            "One block:\n"
            "  x → RMSNorm → CausalMHA → + → RMSNorm → FFN(SwiGLU) → + → x'\n\n"
            "Key differences from original transformer (Vaswani 2017):\n"
            "  • Pre-norm (apply norm BEFORE attention) vs post-norm\n"
            "    → more stable training for very deep models\n"
            "  • RMSNorm instead of LayerNorm (LLaMA)\n"
            "    → cheaper (no mean subtraction) with equivalent quality\n"
            "  • SwiGLU FFN: FFN(x) = (W1x ⊙ swish(W2x)) · W3\n"
            "    → gates the FFN, improving quality at same parameter count\n"
            "  • RoPE positional encoding (applied inside attention, not added to input)\n\n"
            "Stack 32 of these blocks with embedding + unembedding → GPT-2 (1.5B)\n"
            "Stack 80 blocks at d_model=8192 → LLaMA-65B\n\n"
            "The causal mask ensures position i cannot attend to position j > i,\n"
            "enabling autoregressive generation: predict one token at a time."
        ),
        "takeaways": [
            "Pre-norm (RMSNorm before attention) stabilises training of very deep models",
            "RoPE is applied inside attention — it rotates Q and K, not the input",
            "SwiGLU FFN outperforms vanilla ReLU FFN at the same parameter budget",
            "Causal masking makes the model predict left-to-right autoregressively",
        ],
        "tags": ["example", "gpt", "llm", "transformer", "attention", "advanced"],
        "nodes": [
            {"id": "inp",  "type": "input",              "params": {"shape": [16, 512]},                  "pos": [80,  240]},
            {"id": "rope", "type": "rotary_embedding",   "params": {"dim": 512, "max_len": 2048},         "pos": [260, 240]},
            {"id": "rn1",  "type": "rms_norm",           "params": {"dim": 512},                          "pos": [460, 240]},
            {"id": "mha",  "type": "multi_head_attention","params": {"d_model": 512, "heads": 8, "dropout": 0.0}, "pos": [660, 240]},
            {"id": "add1", "type": "add",                "params": {},                                    "pos": [860, 240]},
            {"id": "rn2",  "type": "rms_norm",           "params": {"dim": 512},                          "pos": [1040,240]},
            {"id": "ffn",  "type": "mlp",                "params": {"layers": [2048, 512], "activation": "swish"}, "pos": [1220,240]},
            {"id": "add2", "type": "add",                "params": {},                                    "pos": [1440,240]},
            {"id": "out",  "type": "output",             "params": {"shape": [16, 512]},                  "pos": [1620,240]},
        ],
        "edges": [
            {"from": "inp",  "to": "rope"},
            {"from": "rope", "to": "rn1"},
            {"from": "rn1",  "to": "mha"},
            {"from": "mha",  "to": "add1"},
            {"from": "inp",  "to": "add1", "skip": True},
            {"from": "add1", "to": "rn2"},
            {"from": "rn2",  "to": "ffn"},
            {"from": "ffn",  "to": "add2"},
            {"from": "add1", "to": "add2", "skip": True},
            {"from": "add2", "to": "out"},
        ],
    },

    # ── 10 ── Neural ODE: continuous depth ───────────────────────────────────
    "ex_neural_ode": {
        "name": "Example: Neural ODE (Continuous Depth)",
        "description": (
            "Instead of discrete layers, a Neural ODE defines the network as an "
            "ODE: dh/dt = f(h,t). 'Depth' becomes a continuous parameter solved "
            "by a numerical ODE integrator. Uses O(1) memory during training."
        ),
        "story": (
            "ResNet: h_{t+1} = h_t + f(h_t)  ← discrete time steps\n"
            "Neural ODE: dh/dt = f(h,t)       ← continuous time\n\n"
            "The ODE function f is a small neural network (here: Dense+GELU).\n"
            "Integration from t=0 to t=1 is performed by a numerical solver\n"
            "(Euler, RK4, Dormand-Prince etc.).\n\n"
            "Why is this interesting?\n"
            "  • Memory-efficient training: adjoint method uses O(1) memory\n"
            "    regardless of integration depth (vs O(L) for discrete ResNets)\n"
            "  • Adaptive computation: the solver takes more steps in complex regions\n"
            "  • Continuous normalising flows: Neural ODEs enable exact likelihood\n"
            "    computation via the instantaneous change-of-variables formula\n"
            "  • Natural fit for irregularly-sampled time series\n\n"
            "Trade-off: much slower to evaluate than discrete networks because\n"
            "the ODE solver makes many function evaluations per forward pass.\n\n"
            "Related: Latent ODEs for time series, FFJORD for generative models."
        ),
        "takeaways": [
            "Neural ODE = infinitely deep ResNet with shared weights at all depths",
            "Adjoint method gives O(1) memory for arbitrarily deep integration",
            "Adaptive step size means the model 'thinks harder' on complex inputs",
            "Foundation for continuous normalising flows (FFJORD) and Latent ODEs",
        ],
        "tags": ["example", "neural_ode", "ode", "exotic", "advanced"],
        "nodes": [
            {"id": "inp",   "type": "input",      "params": {"shape": [64]},                               "pos": [80,  200]},
            {"id": "enc",   "type": "dense",      "params": {"in_features": 64, "out_features": 64},      "pos": [260, 200]},
            {"id": "ae",    "type": "activation", "params": {"fn": "gelu"},                               "pos": [420, 200]},
            {"id": "ode",   "type": "neural_ode", "params": {"dim": 64, "solver": "rk4", "steps": 10},    "pos": [600, 200]},
            {"id": "ln",    "type": "layer_norm", "params": {"dim": 64},                                   "pos": [800, 200]},
            {"id": "head",  "type": "linear_probe","params": {"in_features": 64, "out_features": 10, "task": "classification"}, "pos": [980, 200]},
            {"id": "out",   "type": "output",     "params": {"shape": [10], "activation": "softmax"},     "pos": [1180,200]},
        ],
        "edges": [
            {"from": "inp",  "to": "enc"}, {"from": "enc",  "to": "ae"},
            {"from": "ae",   "to": "ode"}, {"from": "ode",  "to": "ln"},
            {"from": "ln",   "to": "head"},{"from": "head", "to": "out"},
            {"from": "inp",  "to": "ode", "skip": True},
        ],
    },

    # ── 11 ── Sparse MoE with per-expert normalisation (Switch Transformer) ──
    "ex_switch_moe": {
        "name": "Example: Switch Transformer MoE Layer",
        "description": (
            "Google's Switch Transformer routes each token to exactly ONE expert "
            "(top-1 routing), achieving the parameter count of a large model at "
            "the compute cost of a small one. Expert-choice routing is shown here."
        ),
        "story": (
            "Standard dense FFN: every token processed by the same weights.\n"
            "MoE FFN: each token routed to one (or k) specialised expert FFNs.\n\n"
            "Switch Transformer (Fedus et al., 2022) uses top-1 routing:\n"
            "  gate(x) = softmax(W_g · x)\n"
            "  expert = argmax(gate(x))\n"
            "  output = gate(x)[expert] · Expert_k(x)\n\n"
            "Why this works:\n"
            "  Model has N×(expert_params) total parameters but processes each\n"
            "  token through only 1×(expert_params) — compute stays constant.\n"
            "  Experts specialise on different kinds of inputs.\n\n"
            "Challenge: load balancing. Without an auxiliary loss, all tokens\n"
            "collapse onto 1-2 popular experts. Switch uses:\n"
            "  L_aux = α · Σ_i f_i · p_i  (encourage uniform routing)\n"
            "  where f_i = fraction of tokens to expert i\n"
            "        p_i = mean routing probability to expert i\n\n"
            "GShard, Mixtral, and GPT-4 all use variants of sparse MoE.\n"
            "Mixtral-8x7B has 8 experts with top-2 routing: 46.7B params,\n"
            "12.9B active per token — quality of a 70B model at 70B/2 cost."
        ),
        "takeaways": [
            "MoE decouples parameter count from compute: more params, same FLOPs",
            "Top-1 routing (Switch) is simpler and as good as top-k for k>1",
            "Load balancing auxiliary loss is critical to prevent expert collapse",
            "Mixtral-8x7B showed MoE is production-ready at large scale",
        ],
        "tags": ["example", "moe", "sparse", "transformer", "advanced"],
        "nodes": [
            {"id": "inp",   "type": "input",      "params": {"shape": [512]},                                    "pos": [80,  240]},
            {"id": "rn",    "type": "rms_norm",   "params": {"dim": 512},                                       "pos": [260, 240]},
            {"id": "router","type": "moe_router", "params": {"num_experts": 8, "top_k": 1, "dim": 512},        "pos": [460, 240]},
            {"id": "e0",    "type": "mlp",        "params": {"layers": [2048, 512], "activation": "gelu"},     "pos": [700, 60]},
            {"id": "e1",    "type": "mlp",        "params": {"layers": [2048, 512], "activation": "gelu"},     "pos": [700, 170]},
            {"id": "e2",    "type": "mlp",        "params": {"layers": [2048, 512], "activation": "gelu"},     "pos": [700, 280]},
            {"id": "e3",    "type": "mlp",        "params": {"layers": [2048, 512], "activation": "gelu"},     "pos": [700, 390]},
            {"id": "merge", "type": "add",        "params": {},                                                 "pos": [960, 240]},
            {"id": "add",   "type": "add",        "params": {},                                                 "pos": [1140,240]},
            {"id": "out",   "type": "output",     "params": {"shape": [512]},                                   "pos": [1320,240]},
        ],
        "edges": [
            {"from": "inp",    "to": "rn"},
            {"from": "rn",     "to": "router"},
            {"from": "router", "to": "e0"}, {"from": "router", "to": "e1"},
            {"from": "router", "to": "e2"}, {"from": "router", "to": "e3"},
            {"from": "e0",     "to": "merge"}, {"from": "e1",  "to": "merge"},
            {"from": "e2",     "to": "merge"}, {"from": "e3",  "to": "merge"},
            {"from": "merge",  "to": "add"},
            {"from": "inp",    "to": "add", "skip": True},
            {"from": "add",    "to": "out"},
        ],
    },

    # ── 12 ── 1D CNN for time-series classification ──────────────────────────
    "ex_tcn": {
        "name": "Example: Temporal CNN (TCN) for Time Series",
        "description": (
            "Stacked dilated 1D convolutions build up long-range temporal context "
            "with far fewer parameters than an LSTM. Each layer doubles the receptive "
            "field. Standard architecture for ECG classification, audio, and sensor data."
        ),
        "story": (
            "Recurrent networks process sequences step-by-step: inherently sequential.\n"
            "Temporal CNNs process all time steps in parallel using convolutions.\n\n"
            "Dilated convolution: skip d-1 positions between kernel elements.\n"
            "  Standard (dilation=1, k=3): receptive field = 3\n"
            "  Dilation=2: receptive field = 5\n"
            "  Dilation=4: receptive field = 9\n"
            "  Dilation=8: receptive field = 17\n"
            "  Stack 4 layers → receptive field = 1 + 2(k-1)(2^4 - 1) = 33 steps\n\n"
            "With k=3 and L dilation-doubling layers:\n"
            "  Receptive field = 1 + 2(k-1)(2^L - 1)\n\n"
            "TCN advantages over LSTM:\n"
            "  • Parallel computation → much faster training\n"
            "  • No vanishing gradients through time\n"
            "  • Flexible receptive field via dilation stacking\n\n"
            "Residual connections (WaveNet, TCN paper) prevent degradation\n"
            "and allow gradient flow across many dilated layers.\n\n"
            "Used in: WaveNet (audio synthesis), clinical ECG analysis,\n"
            "industrial sensor anomaly detection."
        ),
        "takeaways": [
            "Dilated convolutions expand receptive field exponentially with linear depth",
            "TCNs are parallelisable — all time steps computed simultaneously",
            "Residual connections every 2 layers prevent degradation at depth",
            "For long sequences (>1000 steps), TCN is typically faster than LSTM",
        ],
        "tags": ["example", "conv", "tcn", "timeseries", "1d", "intermediate"],
        "nodes": [
            {"id": "inp",  "type": "input",      "params": {"shape": [128, 1]},                                         "pos": [80,  200]},
            {"id": "c0",   "type": "conv1d",     "params": {"in_channels": 1,  "out_channels": 32, "kernel": 3},       "pos": [260, 200]},
            {"id": "bn0",  "type": "batch_norm", "params": {"features": 32},                                            "pos": [420, 200]},
            {"id": "a0",   "type": "activation", "params": {"fn": "relu"},                                              "pos": [560, 200]},
            {"id": "c1",   "type": "conv1d",     "params": {"in_channels": 32, "out_channels": 64, "kernel": 3},       "pos": [700, 200]},
            {"id": "bn1",  "type": "batch_norm", "params": {"features": 64},                                            "pos": [860, 200]},
            {"id": "a1",   "type": "activation", "params": {"fn": "relu"},                                              "pos": [1000,200]},
            {"id": "c2",   "type": "conv1d",     "params": {"in_channels": 64, "out_channels": 64, "kernel": 3},       "pos": [1140,200]},
            {"id": "bn2",  "type": "batch_norm", "params": {"features": 64},                                            "pos": [1300,200]},
            {"id": "a2",   "type": "activation", "params": {"fn": "relu"},                                              "pos": [1440,200]},
            {"id": "pool", "type": "pool",       "params": {"mode": "global"},                                          "pos": [1580,200]},
            {"id": "head", "type": "linear_probe","params": {"in_features": 64, "out_features": 5, "task": "classification"}, "pos": [1740,200]},
            {"id": "out",  "type": "output",     "params": {"shape": [5], "activation": "softmax"},                    "pos": [1920,200]},
        ],
        "edges": [
            {"from": "inp",  "to": "c0"},  {"from": "c0",  "to": "bn0"}, {"from": "bn0", "to": "a0"},
            {"from": "a0",   "to": "c1"},  {"from": "c1",  "to": "bn1"}, {"from": "bn1", "to": "a1"},
            {"from": "a1",   "to": "c2"},  {"from": "c2",  "to": "bn2"}, {"from": "bn2", "to": "a2"},
            {"from": "a2",   "to": "pool"},{"from": "pool", "to": "head"},{"from": "head", "to": "out"},
        ],
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# PARAMETER COUNT ESTIMATOR
# ─────────────────────────────────────────────────────────────────────────────

def _count_params(module: dict) -> int:
    total = 0
    for node in module.get("nodes", []):
        t = node.get("type", "")
        p = node.get("params", {})
        if t == "perceptron":
            total += p.get("inputs", 1) + 1
        elif t == "dense":
            i, o = p.get("in_features", 64), p.get("out_features", 64)
            total += i * o + (o if p.get("bias", True) else 0)
        elif t == "mlp":
            layers = p.get("layers", [64, 64])
            for a, b in zip(layers, layers[1:]):
                total += a * b + b
        elif t in ("rnn", "gru", "lstm"):
            i = p.get("input_size", 64)
            h = p.get("hidden_size", 128)
            mult = 3 if t == "gru" else (4 if t == "lstm" else 1)
            total += mult * (i * h + h * h + h)
        elif t == "multi_head_attention":
            d = p.get("d_model", 128)
            total += 4 * d * d
        elif t == "transformer_block":
            d = p.get("d_model", 128)
            ffn = p.get("ffn_dim", 512)
            total += 4 * d * d + 2 * d * ffn + 4 * d
        elif t == "conv2d":
            ic, oc, k = p.get("in_channels", 3), p.get("out_channels", 32), p.get("kernel", 3)
            total += ic * oc * k * k + oc
        elif t == "conv1d":
            ic, oc, k = p.get("in_channels", 1), p.get("out_channels", 16), p.get("kernel", 3)
            total += ic * oc * k + oc
        elif t == "embedding":
            total += p.get("vocab_size", 1000) * p.get("dim", 64)
        elif t == "kan_layer":
            i, o, g, k = p.get("in_features", 32), p.get("out_features", 32), p.get("grid", 5), p.get("order", 3)
            total += i * o * (g + k)
        elif t == "hopfield":
            d = p.get("dim", 64)
            total += d * d
        elif t == "reservoir":
            r = p.get("reservoir_dim", 256)
            total += p.get("input_dim", 32) * r   # input weights (fixed but counted)
        elif t in ("layer_norm", "batch_norm", "rms_norm"):
            total += p.get("dim", p.get("features", 64)) * 2
        elif t == "state_space":
            d, s, e = p.get("d_model", 64), p.get("d_state", 16), p.get("expand", 2)
            total += d * e * d + e * d * s * 2
    return total


# ─────────────────────────────────────────────────────────────────────────────
# NUMPY EXECUTOR
# ─────────────────────────────────────────────────────────────────────────────

def _relu(x): return np.maximum(0, x) if HAS_NP else x
def _gelu(x): return 0.5 * x * (1 + np.tanh(np.sqrt(2/np.pi) * (x + 0.044715 * x**3))) if HAS_NP else x
def _sigmoid(x): return 1 / (1 + np.exp(-x)) if HAS_NP else x
def _swish(x): return x * _sigmoid(x)
def _softmax(x):
    if not HAS_NP: return x
    e = np.exp(x - x.max(axis=-1, keepdims=True))
    return e / e.sum(axis=-1, keepdims=True)

ACTIVATIONS = {
    "relu": _relu, "gelu": _gelu, "sigmoid": _sigmoid,
    "tanh": np.tanh if HAS_NP else lambda x: x,
    "swish": _swish, "silu": _swish,
    "softmax": _softmax, "step": lambda x: (x >= 0).astype(float) if HAS_NP else x,
    "identity": lambda x: x, "linear": lambda x: x,
}


def _numpy_forward(module: dict, inputs: dict) -> dict:
    """Execute a forward pass using NumPy. Returns {node_id: output_array}."""
    if not HAS_NP:
        return {"error": "NumPy not available"}
    rng = np.random.default_rng(42)
    state: Dict[str, Any] = {}

    # Topological sort (simple — assumes no cycles)
    edges = module.get("edges", [])
    nodes = {n["id"]: n for n in module.get("nodes", [])}
    adj: Dict[str, List[str]] = {nid: [] for nid in nodes}
    in_deg: Dict[str, int] = {nid: 0 for nid in nodes}
    for e in edges:
        if e.get("skip"):
            continue
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

    for nid in order:
        node = nodes[nid]
        ntype = node.get("type", "")
        params = node.get("params", {})

        # Gather input tensors
        in_edges = [e for e in edges if e["to"] == nid and not e.get("skip")]
        if ntype == "input":
            label = params.get("label", nid)
            if label in inputs:
                x = np.array(inputs[label], dtype=float)
            elif nid in inputs:
                x = np.array(inputs[nid], dtype=float)
            else:
                shape = params.get("shape", [4])
                x = rng.standard_normal(shape)
            state[nid] = x
            continue

        # Stack / concatenate inputs from predecessor nodes
        in_tensors = [state[e["from"]] for e in in_edges if e["from"] in state]
        if not in_tensors:
            shape = params.get("shape", [64])
            x = rng.standard_normal(shape)
        elif len(in_tensors) == 1:
            x = in_tensors[0]
        else:
            try:
                x = np.concatenate(in_tensors, axis=-1)
            except Exception:
                x = in_tensors[0]

        act_fn = ACTIVATIONS.get(params.get("activation", "identity"), ACTIVATIONS["identity"])

        if ntype == "perceptron":
            nin = params.get("inputs", 1)
            W = rng.standard_normal(nin) * 0.1
            b = 0.0
            if x.shape[-1] != nin:
                x = x.flat[:nin] if x.size >= nin else np.pad(x.ravel(), (0, nin - x.size))
            y = act_fn(np.dot(x, W) + b)
            state[nid] = np.atleast_1d(y)

        elif ntype in ("dense", "linear_probe"):
            inf, outf = params.get("in_features", x.shape[-1]), params.get("out_features", 64)
            W = rng.standard_normal((inf, outf)) * np.sqrt(2.0 / inf)
            b = np.zeros(outf)
            # Reshape x to (..., inf)
            flat = x.reshape(-1, inf) if x.shape[-1] == inf else x.reshape(-1, x.shape[-1])
            if flat.shape[-1] != inf:
                W = rng.standard_normal((flat.shape[-1], outf)) * 0.1
            out = flat @ W + b
            out = act_fn(out)
            state[nid] = out.squeeze(0) if out.ndim > 1 and out.shape[0] == 1 else out

        elif ntype == "activation":
            state[nid] = act_fn(x)

        elif ntype == "mlp":
            layers = params.get("layers", [64, 64])
            h = x
            cur_dim = h.shape[-1] if h.ndim > 0 else 1
            for ldim in layers:
                W = rng.standard_normal((cur_dim, ldim)) * np.sqrt(2.0 / cur_dim)
                b = np.zeros(ldim)
                h = act_fn(h.reshape(-1, cur_dim) @ W + b)
                cur_dim = ldim
            state[nid] = h.squeeze(0) if h.ndim > 1 and h.shape[0] == 1 else h

        elif ntype == "layer_norm":
            dim = params.get("dim", x.shape[-1])
            mu = x.mean(axis=-1, keepdims=True)
            sigma = x.std(axis=-1, keepdims=True) + params.get("eps", 1e-5)
            state[nid] = (x - mu) / sigma

        elif ntype == "rms_norm":
            eps = params.get("eps", 1e-8)
            rms = np.sqrt((x**2).mean(axis=-1, keepdims=True) + eps)
            state[nid] = x / rms

        elif ntype == "batch_norm":
            mu, sigma = x.mean(axis=0), x.std(axis=0) + 1e-5
            state[nid] = (x - mu) / sigma

        elif ntype == "dropout":
            p = params.get("p", 0.1)
            mask = rng.random(x.shape) > p
            state[nid] = x * mask / (1 - p + 1e-8)

        elif ntype in ("add", "residual"):
            skip_ins = [state[e["from"]] for e in edges if e["to"] == nid and e["from"] in state]
            if len(skip_ins) >= 2:
                try:
                    result = sum(skip_ins)
                except Exception:
                    result = skip_ins[0]
                state[nid] = result
            else:
                state[nid] = x

        elif ntype == "concat":
            cat_ins = [state[e["from"]] for e in edges if e["to"] == nid and e["from"] in state]
            try:
                state[nid] = np.concatenate(cat_ins, axis=params.get("dim", -1))
            except Exception:
                state[nid] = x

        elif ntype == "reshape":
            shape = params.get("shape", [-1])
            try:
                state[nid] = x.reshape(shape)
            except Exception:
                state[nid] = x.ravel()

        elif ntype in ("rnn", "gru", "lstm"):
            hidden = params.get("hidden_size", 128)
            seq_len = x.shape[0] if x.ndim > 1 else 1
            h = np.zeros(hidden)
            for _ in range(min(seq_len, 32)):  # cap for speed
                xi = x[_] if x.ndim > 1 else x
                W_i = rng.standard_normal((xi.shape[-1], hidden)) * 0.01
                W_h = rng.standard_normal((hidden, hidden)) * 0.01
                h = np.tanh(xi.reshape(-1) @ W_i[:xi.reshape(-1).shape[0]] + h @ W_h)
            state[nid] = h

        elif ntype == "attention":
            dim = params.get("dim", x.shape[-1])
            if x.ndim == 1:
                x = x[np.newaxis, :]
            Wq = rng.standard_normal((x.shape[-1], dim)) * 0.1
            Wk = rng.standard_normal((x.shape[-1], dim)) * 0.1
            Wv = rng.standard_normal((x.shape[-1], dim)) * 0.1
            Q, K, V = x @ Wq, x @ Wk, x @ Wv
            scores = _softmax(Q @ K.T / np.sqrt(dim))
            state[nid] = scores @ V

        elif ntype == "multi_head_attention":
            d = params.get("d_model", x.shape[-1])
            h = params.get("heads", 8)
            d_h = d // h
            if x.ndim == 1:
                x = x[np.newaxis, :]
            out_parts = []
            for _ in range(min(h, 4)):  # cap heads for speed
                Wq = rng.standard_normal((x.shape[-1], d_h)) * 0.05
                Wk = rng.standard_normal((x.shape[-1], d_h)) * 0.05
                Wv = rng.standard_normal((x.shape[-1], d_h)) * 0.05
                Q, K, V = x @ Wq, x @ Wk, x @ Wv
                s = _softmax(Q @ K.T / np.sqrt(d_h))
                out_parts.append(s @ V)
            state[nid] = np.concatenate(out_parts, axis=-1)

        elif ntype == "transformer_block":
            # Simplified forward — use actual input dim to avoid shape mismatch
            if x.ndim == 1:
                x = x[np.newaxis, :]
            d = x.shape[-1]   # always use true input dim for weight init
            # LN + self-attention + residual
            norm = (x - x.mean(-1, keepdims=True)) / (x.std(-1, keepdims=True) + 1e-5)
            Wq = rng.standard_normal((d, d)) * 0.05
            attn = _softmax(norm @ Wq @ norm.T / np.sqrt(max(d, 1))) @ norm
            x = x + attn
            # LN + FFN + residual
            norm2 = (x - x.mean(-1, keepdims=True)) / (x.std(-1, keepdims=True) + 1e-5)
            ffn_dim = max(4, params.get("ffn_dim", d * 4))
            W1 = rng.standard_normal((d, ffn_dim)) * 0.05
            W2 = rng.standard_normal((ffn_dim, d)) * 0.05
            ffn = _gelu(norm2 @ W1) @ W2
            state[nid] = x + ffn

        elif ntype == "embedding":
            vocab = params.get("vocab_size", 1000)
            dim = params.get("dim", 64)
            E = rng.standard_normal((vocab, dim)) * 0.02
            idx = np.clip(x.astype(int).ravel(), 0, vocab - 1)
            state[nid] = E[idx]

        elif ntype == "positional_encoding":
            dim = params.get("dim", x.shape[-1])
            if x.ndim == 1:
                x = x[np.newaxis, :]
            seq = x.shape[0]
            pe = np.zeros((seq, dim))
            pos = np.arange(seq)[:, np.newaxis]
            div = np.exp(np.arange(0, dim, 2) * (-np.log(10000.0) / dim))
            pe[:, 0::2] = np.sin(pos * div)
            pe[:, 1::2] = np.cos(pos * div[:dim // 2])
            state[nid] = x + pe[:, :x.shape[-1]]

        elif ntype == "conv1d":
            oc = params.get("out_channels", 16)
            k  = params.get("kernel", 3)
            if x.ndim == 1:
                x = x[np.newaxis, :]          # (1, features) → treat features as seq
            # Treat x as (seq_len, channels); use actual channel count for weights
            ic = x.shape[-1]
            W = rng.standard_normal((oc, ic, k)) * 0.1
            pad = k // 2
            x_padded = np.pad(x, ((pad, pad), (0, 0)), mode='constant')
            out_len = x.shape[0]
            out = np.zeros((out_len, oc))
            for i in range(out_len):
                patch = x_padded[i:i+k, :ic].T   # (ic, k)
                out[i] = W.reshape(oc, -1) @ patch.ravel()
            state[nid] = out

        elif ntype == "conv2d":
            oc = params.get("out_channels", 32)
            out_shape = list(x.shape[:-1]) + [oc] if x.ndim > 1 else [oc]
            state[nid] = rng.standard_normal(out_shape) * 0.1

        elif ntype == "pool":
            mode = params.get("mode", "max")
            k = params.get("kernel", 2)
            if x.ndim < 2:
                state[nid] = x
                continue
            new_len = max(1, x.shape[0] // k)
            out = np.zeros((new_len,) + x.shape[1:])
            for i in range(new_len):
                chunk = x[i*k:min((i+1)*k, x.shape[0])]
                out[i] = chunk.max(0) if mode == "max" else chunk.mean(0)
            state[nid] = out

        elif ntype == "hopfield":
            beta = params.get("beta", 1.0)
            q = x.ravel()
            q_dim = q.shape[0]
            # Pattern dim matches query dim — params.dim is advisory only
            patterns = rng.standard_normal((params.get("patterns", 128), q_dim)) * 0.1
            scores = _softmax(beta * patterns @ q / np.sqrt(max(q_dim, 1)))
            state[nid] = patterns.T @ scores

        elif ntype == "reservoir":
            res_dim = params.get("reservoir_dim", 256)
            sr = params.get("spectral_radius", 0.95)
            W_in = rng.standard_normal((res_dim, x.ravel().shape[0])) * 0.1
            W_res = rng.standard_normal((res_dim, res_dim)) * 0.1
            # Normalise spectral radius
            try:
                ev_max = np.abs(np.linalg.eigvals(W_res)).max()
                W_res = W_res / ev_max * sr
            except Exception:
                pass
            h = np.zeros(res_dim)
            steps = min(x.shape[0] if x.ndim > 1 else 1, 20)
            for t in range(steps):
                xi = x[t] if x.ndim > 1 else x
                h = np.tanh(W_in @ xi.ravel() + W_res @ h)
            state[nid] = h

        elif ntype == "moe_router":
            k = params.get("top_k", 2)
            n = params.get("num_experts", 8)
            dim = params.get("dim", x.shape[-1])
            W_gate = rng.standard_normal((x.ravel().shape[0], n)) * 0.1
            logits = x.ravel() @ W_gate
            gate_w = _softmax(logits)
            top_k_idx = np.argsort(gate_w)[-k:]
            sparse = np.zeros(n)
            sparse[top_k_idx] = gate_w[top_k_idx]
            sparse /= sparse.sum() + 1e-8
            # Route: weighted sum of expert outputs (random here)
            expert_outs = rng.standard_normal((n, dim)) * 0.1
            state[nid] = (sparse[:, np.newaxis] * expert_outs).sum(0)

        elif ntype == "kan_layer":
            outf = params.get("out_features", 32)
            # Approximate KAN with learnable basis
            W = rng.standard_normal((x.ravel().shape[0], outf)) * 0.1
            state[nid] = np.tanh(x.ravel() @ W)

        elif ntype == "state_space":
            s_dim = params.get("d_state", 16)
            # Simplified SSM scan — use real input dim, not advisory d_model
            if x.ndim == 1:
                x = x[np.newaxis, :]
            d = x.shape[-1]                        # true input width
            A = rng.standard_normal((d, s_dim)) * 0.1
            B = rng.standard_normal((d, s_dim)) * 0.1
            h = np.zeros(s_dim)
            outs = []
            for t in range(x.shape[0]):
                h = np.tanh(x[t] @ A + h + x[t] @ B)
                outs.append(x[t] + h @ A.T)
            state[nid] = np.array(outs)

        elif ntype == "output":
            out_act = params.get("activation", "identity")
            act = ACTIVATIONS.get(out_act, ACTIVATIONS["identity"])
            state[nid] = act(x)

        else:
            state[nid] = x  # passthrough for unknown types

    return {nid: v.tolist() if hasattr(v, "tolist") else v for nid, v in state.items()}


# ─────────────────────────────────────────────────────────────────────────────
# SHAPE INFERENCE
# ─────────────────────────────────────────────────────────────────────────────

def _infer_shapes(module: dict) -> Dict[str, list]:
    """Walk the graph and infer output shape per node without executing."""
    shapes: Dict[str, list] = {}
    nodes = {n["id"]: n for n in module.get("nodes", [])}
    edges = [e for e in module.get("edges", []) if not e.get("skip")]
    adj: Dict[str, list] = {nid: [] for nid in nodes}
    in_deg = {nid: 0 for nid in nodes}
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

    for nid in order:
        node = nodes[nid]
        t = node.get("type", "")
        p = node.get("params", {})
        in_shapes = [shapes[e["from"]] for e in edges if e["to"] == nid and e["from"] in shapes]
        s = in_shapes[0] if in_shapes else p.get("shape", [64])

        if t == "input":
            shapes[nid] = p.get("shape", [4])
        elif t in ("dense", "linear_probe"):
            shapes[nid] = s[:-1] + [p.get("out_features", 64)] if len(s) > 1 else [p.get("out_features", 64)]
        elif t == "mlp":
            layers = p.get("layers", [64])
            shapes[nid] = s[:-1] + [layers[-1]] if len(s) > 1 else [layers[-1]]
        elif t in ("activation", "layer_norm", "batch_norm", "rms_norm", "dropout", "residual", "add"):
            shapes[nid] = s
        elif t == "embedding":
            shapes[nid] = s + [p.get("dim", 64)]
        elif t in ("rnn", "gru", "lstm"):
            shapes[nid] = [p.get("hidden_size", 128)]
        elif t == "multi_head_attention":
            shapes[nid] = s[:-1] + [p.get("d_model", s[-1])] if len(s) > 1 else [p.get("d_model", 128)]
        elif t == "transformer_block":
            shapes[nid] = s
        elif t == "conv1d":
            shapes[nid] = [max(1, (s[0] if s else 1)), p.get("out_channels", 16)]
        elif t == "conv2d":
            shapes[nid] = (s[:-1] if len(s) > 0 else []) + [p.get("out_channels", 32)]
        elif t == "pool":
            k = p.get("kernel", 2)
            shapes[nid] = [max(1, s[0] // k)] + s[1:] if s else [1]
        elif t == "reshape":
            shapes[nid] = p.get("shape", [-1])
        elif t == "concat":
            if in_shapes:
                total = sum(sh[-1] for sh in in_shapes)
                shapes[nid] = in_shapes[0][:-1] + [total]
            else:
                shapes[nid] = s
        elif t == "kan_layer":
            shapes[nid] = s[:-1] + [p.get("out_features", 32)] if len(s) > 1 else [p.get("out_features", 32)]
        elif t == "hopfield":
            shapes[nid] = [p.get("dim", 64)]
        elif t == "reservoir":
            shapes[nid] = [p.get("reservoir_dim", 256)]
        elif t == "moe_router":
            shapes[nid] = [p.get("dim", 64)]
        elif t == "state_space":
            shapes[nid] = s
        elif t == "output":
            shapes[nid] = p.get("shape", s)
        else:
            shapes[nid] = s

    return shapes


# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENCE  — Data Fabric is the single source of truth.
#
# Storage layout in the fabric:
#   dataset_id = "ml.modules"
#   Each record's `data` field holds the full module JSON.
#   record `id` is set to the module_id so upserts are idempotent.
#
# The in-process _MODULES dict is a write-through cache populated at startup
# and kept in sync on every save/delete.  Both ml_workshop and ml_training
# share the same fabric dataset so they always see the same modules.
# ─────────────────────────────────────────────────────────────────────────────

FABRIC_MODULES_DS  = "ml.modules"     # fabric dataset_id for module definitions
FABRIC_TRAINING_DS = "ml.training"    # fabric dataset_id for training jobs / results
FABRIC_DATASETS_DS = "ml.datasets"    # fabric dataset_id for ML dataset metadata


async def _fabric_ingest(dataset_id: str, record_id: str, data: dict,
                          text: str = "", tags: list = None):
    """Write one record into the Data Fabric, keyed by record_id.

    We pass the module dict as a flat item with a `text` field added so
    ingest_dataset can generate an embedding. The fabric will store it as:
        sqlite_record.data = JSON({...module fields..., text: ..., _record_id: id})
    which _fabric_query_all can reconstruct directly.
    """
    try:
        from Vera.Orchestration.fabric.data_fabric import ingest_dataset as _ingest
        # Merge record_id into the item so we can recover it on query
        item = dict(data)
        item["_record_id"] = record_id
        item["text"]       = text or record_id   # fabric uses 'text' for embedding
        await _ingest(
            dataset_id = dataset_id,
            data       = [item],
            source     = "ml_workshop",
            tags       = tags or [],
        )
        return True
    except Exception as e:
        log.debug("_fabric_ingest %s/%s: %s", dataset_id, record_id, e)
        return False


async def _fabric_delete(dataset_id: str, record_id: str):
    """Remove a single record from the fabric by dataset+record id."""
    try:
        from Vera.Orchestration.fabric.data_fabric import _enqueue_write
        await _enqueue_write({
            "kind":   "raw",
            "sql":    "DELETE FROM fabric_records WHERE dataset_id=? AND id=?",
            "params": (dataset_id, record_id),
        }, wait=True)
        # Also decrement the record count
        await _enqueue_write({
            "kind":   "raw",
            "sql":    (
                "UPDATE fabric_datasets SET record_count=MAX(0,record_count-1),"
                "updated_at=? WHERE dataset_id=?"
            ),
            "params": (now_iso(), dataset_id),
        }, wait=False)
    except Exception as e:
        log.debug("_fabric_delete %s/%s: %s", dataset_id, record_id, e)


async def _fabric_query_all(dataset_id: str) -> list:
    """Return all payload dicts from a fabric dataset.

    ingest_dataset wraps each item as:
        fabric_record.data = JSON({text, data: <original_item>, id: record_id})
    We unwrap and return the original item dicts.
    """
    try:
        from Vera.Orchestration.fabric.data_fabric import _sqlite_query
        rows = await _sqlite_query(dataset_id=dataset_id, limit=2000)
        result = []
        for r in rows:
            raw = r.get("data")
            if not raw:
                continue
            try:
                outer = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(outer, dict):
                    continue
                # ingest_dataset wraps as {text, data: payload, ...}
                # Try to get the actual payload
                payload = outer.get("data")
                if isinstance(payload, dict):
                    # Merge record_id from outer id field if missing
                    if "id" not in payload and outer.get("id"):
                        payload["_record_id"] = outer["id"]
                    result.append(payload)
                elif isinstance(payload, str):
                    try:
                        p = json.loads(payload)
                        if isinstance(p, dict):
                            result.append(p)
                    except Exception:
                        pass
                else:
                    # Flat record — outer IS the payload (legacy / direct ingest)
                    result.append(outer)
            except Exception:
                pass
        return result
    except Exception as e:
        log.debug("_fabric_query_all %s: %s", dataset_id, e)
        return []


async def _save_module(mid: str, module: dict):
    """Save module to in-process cache AND Data Fabric."""
    _MODULES[mid] = module
    # Primary: Data Fabric (SQLite-backed, shared across all processes/modules)
    await _fabric_ingest(
        dataset_id = FABRIC_MODULES_DS,
        record_id  = mid,
        data       = module,
        text       = f"{module.get('name',mid)} {' '.join(module.get('tags',[]))}",
        tags       = ["ml", "module"] + module.get("tags", []),
    )
    # Secondary: Redis hot-cache for sub-millisecond lookup by the training module
    try:
        import Vera.Orchestration.capability_orchestration as _orch
        if _orch.REDIS:
            await _orch.REDIS.set(REDIS_PREFIX + mid, json.dumps(module), ex=86400)
    except Exception as e:
        log.debug("ml_workshop redis save: %s", e)


async def _load_all_modules():
    """Populate _MODULES from Data Fabric (authoritative) then Redis hot-cache."""
    loaded = 0

    # 1. Load from Fabric (always available via SQLite)
    records = await _fabric_query_all(FABRIC_MODULES_DS)
    for data in records:
        mid = data.get("id") or data.get("_record_id") or data.get("module_id")
        if mid and isinstance(data, dict) and "nodes" in data:
            _MODULES[mid] = data
            loaded += 1

    # 2. Supplement with Redis (may have more-recent in-flight saves)
    try:
        import Vera.Orchestration.capability_orchestration as _orch
        if _orch.REDIS:
            keys = await _orch.REDIS.keys(REDIS_PREFIX + "*")
            for k in keys:
                raw = await _orch.REDIS.get(k)
                if raw:
                    try:
                        m   = json.loads(raw)
                        mid = k.decode().replace(REDIS_PREFIX, "") if isinstance(k, bytes) \
                              else k.replace(REDIS_PREFIX, "")
                        if mid and "nodes" in m:
                            _MODULES[mid] = m      # Redis wins (more recent)
                            loaded += 1
                    except Exception:
                        pass
    except Exception as e:
        log.debug("ml_workshop redis load: %s", e)

    log.info("ml_workshop: loaded %d modules (fabric+redis)", loaded)


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:
    _HERE = _Path(__file__).parent

    @APP.get("/ml/panel", include_in_schema=False)
    async def _ml_panel_route():
        from fastapi.responses import HTMLResponse
        p = _HERE / "ml_workshop_panel.html"
        if p.exists():
            return HTMLResponse(p.read_text(encoding="utf-8"))
        return HTMLResponse("<p style='color:red'>ml_workshop_panel.html not found</p>")

    try:
        register_ui(
            "ml-workshop",
            "ML Workshop",
            "⬡",
            """<div id="ml-workshop-mount" style="height:100%;display:flex;flex-direction:column;">
        <iframe src="/ml/panel"
                style="flex:1;border:none;width:100%;height:100%"
                allow="clipboard-read; clipboard-write">
        </iframe>
        </div>""",
            "",
            ui_caps=["ml.create", "ml.list", "ml.run", "ml.generate", "ml.inspect"],
            mode="tab",
            tab_order=65,
        )
    except Exception as _e:
        log.warning("ml_workshop register_ui: %s", _e)

    # ── Catalogue ──────────────────────────────────────────────────────────────

    @capability(
        "ml.catalogue",
        http_method="GET", http_path="/ml/catalogue", http_tags=["ml"],
        memory="off", silent=True,
        description="Return the full layer catalogue and architecture templates.",
    )
    async def ml_catalogue(trace_id=None):
        return {
            "layers": LAYER_CATALOGUE,
            "templates": {k: {"name": v["name"], "desc": v["desc"]} for k, v in TEMPLATES.items()},
            "activations": list(ACTIVATIONS.keys()),
            "families": sorted({v["family"] for v in LAYER_CATALOGUE.values()}),
            "backends": {"numpy": HAS_NP, "torch": HAS_TORCH},
        }

    # ── Create / save module ──────────────────────────────────────────────────

    @capability(
        "ml.create",
        http_method="POST", http_path="/ml/create", http_tags=["ml"],
        memory="off",
        description="Create or update a module definition. Pass nodes/edges JSON.",
    )
    async def ml_create(
        name: str,
        nodes: str = "[]",
        edges: str = "[]",
        description: str = "",
        tags: str = "",
        module_id: str = "",
        trace_id=None,
    ):
        mid = module_id or str(uuid.uuid4())[:8]
        try:
            node_list = json.loads(nodes) if isinstance(nodes, str) else nodes
            edge_list = json.loads(edges) if isinstance(edges, str) else edges
        except Exception as e:
            return {"error": f"JSON parse: {e}"}

        module = {
            "id": mid,
            "name": name,
            "description": description,
            "tags": [t.strip() for t in tags.split(",") if t.strip()],
            "nodes": node_list,
            "edges": edge_list,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "param_count": _count_params({"nodes": node_list}),
        }
        await _save_module(mid, module)
        shapes = _infer_shapes(module)
        module["shapes"] = shapes

        await emit_event({"type": "ml.module_created", "id": mid, "name": name,
                          "params": module["param_count"]})
        return {"ok": True, "id": mid, "param_count": module["param_count"],
                "shapes": shapes, "module": module}

    # ── Load template ──────────────────────────────────────────────────────────

    @capability(
        "ml.from_template",
        http_method="POST", http_path="/ml/from_template", http_tags=["ml"],
        memory="off",
        description="Instantiate a module from a named architecture template.",
    )
    async def ml_from_template(template: str, name: str = "", trace_id=None):
        t = TEMPLATES.get(template)
        if not t:
            return {"error": f"Unknown template: {template}. Options: {list(TEMPLATES.keys())}"}
        mid = str(uuid.uuid4())[:8]
        module = {
            "id": mid,
            "name": name or t["name"],
            "description": t["desc"],
            "tags": ["template", template],
            "nodes": deepcopy(t["nodes"]),
            "edges": deepcopy(t["edges"]),
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "param_count": _count_params({"nodes": t["nodes"]}),
        }
        module["shapes"] = _infer_shapes(module)
        await _save_module(mid, module)
        await emit_event({"type": "ml.module_created", "id": mid, "name": module["name"],
                          "template": template})
        return {"ok": True, "id": mid, "module": module}

    # ── List modules ──────────────────────────────────────────────────────────

    @capability(
        "ml.list",
        http_method="GET", http_path="/ml/list", http_tags=["ml"],
        memory="off", silent=True,
        description="List all saved ML modules (reads from Data Fabric).",
    )
    async def ml_list(refresh: bool = True, trace_id=None):
        # Always sync from Fabric so ml_training sees workshop modules immediately
        if refresh:
            await _load_all_modules()
        mods = []
        for mid, m in _MODULES.items():
            mods.append({
                "id": mid,
                "name": m.get("name", mid),
                "description": m.get("description", ""),
                "tags": m.get("tags", []),
                "param_count": m.get("param_count", 0),
                "nodes": len(m.get("nodes", [])),
                "edges": len(m.get("edges", [])),
                "updated_at": m.get("updated_at", ""),
            })
        mods.sort(key=lambda x: x["updated_at"], reverse=True)
        return {"modules": mods, "count": len(mods)}

    # ── Get single module ──────────────────────────────────────────────────────

    @capability(
        "ml.get",
        http_method="GET", http_path="/ml/get", http_tags=["ml"],
        memory="off", silent=True,
        description="Get a module definition by id.",
    )
    async def ml_get(id: str, trace_id=None):
        m = _MODULES.get(id)
        if not m:
            # Cache miss — try loading from fabric directly
            try:
                from Vera.Orchestration.fabric.data_fabric import _sqlite_query
                rows = await _sqlite_query(dataset_id=FABRIC_MODULES_DS, limit=2000)
                for r in rows:
                    raw = r.get("data")
                    if not raw:
                        continue
                    try:
                        outer = json.loads(raw) if isinstance(raw, str) else raw
                        d = outer.get("data", outer) if isinstance(outer, dict) else outer
                        if isinstance(d, str):
                            try: d = json.loads(d)
                            except: continue
                        rid = d.get("id") or d.get("_record_id") or outer.get("id","")
                        if rid == id and "nodes" in d:
                            m = d
                            _MODULES[id] = m   # warm the cache
                            break
                    except Exception:
                        pass
            except Exception as e:
                log.debug("ml_get fabric fallback: %s", e)
        if not m:
            return {"error": f"Module not found: {id}"}
        return {"module": m, "shapes": _infer_shapes(m)}

    # ── Delete ────────────────────────────────────────────────────────────────

    @capability(
        "ml.delete",
        http_method="POST", http_path="/ml/delete", http_tags=["ml"],
        memory="off",
        description="Delete a module by id from cache, Fabric, and Redis.",
    )
    async def ml_delete(id: str, trace_id=None):
        # Remove from in-process cache
        _MODULES.pop(id, None)
        # Remove from Data Fabric
        await _fabric_delete(FABRIC_MODULES_DS, id)
        # Remove from Redis
        try:
            import Vera.Orchestration.capability_orchestration as _orch
            if _orch.REDIS:
                await _orch.REDIS.delete(REDIS_PREFIX + id)
        except Exception:
            pass
        await emit_event({"type": "ml.module_deleted", "id": id})
        return {"ok": True, "id": id}

    # ── Run forward pass ──────────────────────────────────────────────────────

    @capability(
        "ml.run",
        http_method="POST", http_path="/ml/run", http_tags=["ml"],
        memory="on",
        description="Execute a forward pass through a module. "
                    "Pass inputs as JSON dict {node_id: array}. "
                    "Returns per-node outputs + summary stats.",
    )
    async def ml_run(
        id: str,
        inputs: str = "{}",
        summarise: bool = True,
        trace_id=None,
    ):
        m = _MODULES.get(id)
        if not m:
            return {"error": f"Module not found: {id}"}
        if not HAS_NP:
            return {"error": "NumPy not installed — cannot execute"}
        try:
            inp = json.loads(inputs) if isinstance(inputs, str) else inputs
        except Exception as e:
            return {"error": f"inputs JSON: {e}"}

        t0 = time.monotonic()
        result = _numpy_forward(m, inp)
        elapsed_ms = round((time.monotonic() - t0) * 1000, 2)

        if "error" in result:
            return result

        # Summarise outputs (shapes + norms)
        summary = {}
        for nid, val in result.items():
            if isinstance(val, list):
                try:
                    arr = np.array(val)
                    summary[nid] = {
                        "shape": list(arr.shape),
                        "dtype": str(arr.dtype),
                        "mean": float(arr.mean()),
                        "std": float(arr.std()),
                        "norm": float(np.linalg.norm(arr.ravel())),
                        "min": float(arr.min()),
                        "max": float(arr.max()),
                    }
                except Exception:
                    summary[nid] = {"raw": str(val)[:80]}
            else:
                summary[nid] = {"raw": str(val)[:80]}

        await emit_event({
            "type": "ml.run_complete", "id": id, "elapsed_ms": elapsed_ms,
            "nodes": len(result),
        })
        return {
            "id": id,
            "elapsed_ms": elapsed_ms,
            "summary": summary,
            "outputs": result if not summarise else {k: v for k, v in result.items()
                                                      if m.get("nodes") and
                                                      any(n["id"] == k and n["type"] == "output"
                                                          for n in m.get("nodes", []))},
        }

    # ── Inspect / introspect ──────────────────────────────────────────────────

    @capability(
        "ml.inspect",
        http_method="GET", http_path="/ml/inspect", http_tags=["ml"],
        memory="off", silent=True,
        description="Deep inspection: shapes, param counts, connectivity, cycle detection.",
    )
    async def ml_inspect(id: str, trace_id=None):
        m = _MODULES.get(id)
        if not m:
            return {"error": f"Not found: {id}"}
        nodes = {n["id"]: n for n in m.get("nodes", [])}
        edges = m.get("edges", [])
        shapes = _infer_shapes(m)

        node_info = []
        for n in m.get("nodes", []):
            nid = n["id"]
            in_e = [e for e in edges if e["to"] == nid]
            out_e = [e for e in edges if e["from"] == nid]
            pc = _count_params({"nodes": [n]})
            node_info.append({
                "id": nid,
                "type": n.get("type"),
                "family": LAYER_CATALOGUE.get(n.get("type", ""), {}).get("family", "unknown"),
                "params": n.get("params", {}),
                "param_count": pc,
                "in_shape": shapes.get(nid, []),
                "in_degree": len(in_e),
                "out_degree": len(out_e),
            })

        total_params = _count_params(m)
        has_cycle = len([n for n in m.get("nodes", [])]) != len(set(n["id"] for n in m.get("nodes", [])))

        return {
            "id": id,
            "name": m.get("name"),
            "total_params": total_params,
            "param_count_human": f"{total_params/1e6:.2f}M" if total_params >= 1e6 else f"{total_params/1e3:.1f}K" if total_params >= 1e3 else str(total_params),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "has_cycle": has_cycle,
            "nodes": node_info,
            "shapes": shapes,
            "backends": {"numpy": HAS_NP, "torch": HAS_TORCH},
        }

    # ── LLM generation ────────────────────────────────────────────────────────

    @capability(
        "ml.generate",
        http_method="POST", http_path="/ml/generate", http_tags=["ml"],
        memory="on",
        description="Ask the LLM to design a neural architecture from a natural language description.",
    )
    async def ml_generate(
        description: str,
        constraints: str = "",
        save: bool = True,
        trace_id=None,
    ):
        layer_summary = "\n".join(
            f"  {k}: {v['desc']} | family={v['family']} | default_params={json.dumps(v['params'])}"
            for k, v in LAYER_CATALOGUE.items()
        )
        template_names = ", ".join(TEMPLATES.keys())
        system = f"""You are an expert neural architecture designer for the Vera ML Workshop.
Design a neural network based on the user's description.

AVAILABLE LAYER TYPES (use ONLY these exact type names):
{layer_summary}

Also available: "input" (params: shape, label) and "output" (params: shape, activation)

RULES:
1. Every graph MUST start with one or more "input" nodes and end with one or more "output" nodes.
2. Node ids must be short alphanumeric strings: inp, d0, d1, a0, ln1, out, etc.
3. Edges connect nodes: {{"from": "id1", "to": "id2"}}. Add "skip": true for residual bypasses.
4. params must match the layer's schema shown above.
5. Keep graphs to 4-15 nodes unless complexity demands more.
6. Use "family" knowledge: recurrent layers for sequences, attention for transformers, conv for spatial.

Respond ONLY with a JSON object in this exact format (no markdown):
{{
  "name": "Architecture Name",
  "description": "Brief description",
  "nodes": [
    {{"id": "inp", "type": "input", "params": {{"shape": [64]}}, "pos": [80, 200]}},
    ...
  ],
  "edges": [
    {{"from": "inp", "to": "d0"}},
    ...
  ]
}}"""

        user_msg = f"Design: {description}"
        if constraints:
            user_msg += f"\nConstraints: {constraints}"

        raw = await ollama_generate(user_msg, system=system, prefer_gpu=True, json_mode=True)
        if not raw:
            return {"error": "LLM returned empty response"}

        # Extract JSON
        design = None
        for attempt in [raw, re.search(r'\{[\s\S]*\}', raw or "")]:
            txt = attempt if isinstance(attempt, str) else (attempt.group() if attempt else "")
            try:
                d = json.loads(txt.strip())
                if isinstance(d, dict) and d.get("nodes"):
                    design = d
                    break
            except Exception:
                pass

        if not design:
            return {"error": "Could not parse architecture from LLM", "raw": raw[:500]}

        if save:
            mid = str(uuid.uuid4())[:8]
            # Ensure positions exist
            for i, node in enumerate(design.get("nodes", [])):
                if "pos" not in node:
                    node["pos"] = [80 + i * 180, 200]
            module = {
                "id": mid,
                "name": design.get("name", "Generated Architecture"),
                "description": design.get("description", description),
                "tags": ["generated", "llm"],
                "nodes": design.get("nodes", []),
                "edges": design.get("edges", []),
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "param_count": _count_params({"nodes": design.get("nodes", [])}),
            }
            module["shapes"] = _infer_shapes(module)
            await _save_module(mid, module)
            await emit_event({"type": "ml.module_generated", "id": mid,
                              "name": module["name"], "description": description})
            return {"ok": True, "id": mid, "module": module}

        return {"ok": True, "design": design}

    # ── Explain ───────────────────────────────────────────────────────────────

    @capability(
        "ml.explain",
        http_method="POST", http_path="/ml/explain", http_tags=["ml"],
        memory="on",
        description="Ask the LLM to explain what a module does and why its architecture makes sense.",
    )
    async def ml_explain(id: str, detail: str = "medium", trace_id=None):
        m = _MODULES.get(id)
        if not m:
            return {"error": f"Not found: {id}"}
        summary = {
            "name": m.get("name"),
            "description": m.get("description"),
            "total_params": _count_params(m),
            "nodes": [{"id": n["id"], "type": n["type"], "params": n.get("params", {})}
                      for n in m.get("nodes", [])],
            "edges": m.get("edges", []),
        }
        system = ("You are an ML educator. Explain neural network architectures clearly. "
                  "Cover: purpose, data flow, key design decisions, strengths and limitations. "
                  "Be " + ("brief (2-3 sentences)" if detail == "brief" else
                            "thorough and educational (full explanation)") + ".")
        text = await ollama_generate(
            f"Explain this neural network:\n{json.dumps(summary, indent=2)}",
            system=system, prefer_gpu=True,
        )
        return {"id": id, "name": m.get("name"), "explanation": text}

    # ── Suggest modifications ──────────────────────────────────────────────────

    @capability(
        "ml.suggest",
        http_method="POST", http_path="/ml/suggest", http_tags=["ml"],
        memory="on",
        description="Get LLM suggestions for improving or extending a module.",
    )
    async def ml_suggest(id: str, goal: str = "", trace_id=None):
        m = _MODULES.get(id)
        if not m:
            return {"error": f"Not found: {id}"}
        summary = {
            "name": m.get("name"),
            "nodes": [{"id": n["id"], "type": n["type"], "params": n.get("params", {})}
                      for n in m.get("nodes", [])],
        }
        system = ("You are an ML architecture expert. Suggest concrete, actionable improvements "
                  "to a neural network. Return a JSON list of suggestions, each with: "
                  "title, description, node_changes (what to add/modify/remove). "
                  "Return ONLY valid JSON, no markdown.")
        user_msg = f"Suggest improvements to:\n{json.dumps(summary, indent=2)}"
        if goal:
            user_msg += f"\nGoal: {goal}"
        raw = await ollama_generate(user_msg, system=system, prefer_gpu=True, json_mode=True)
        try:
            suggestions = json.loads(raw or "[]")
            if isinstance(suggestions, dict):
                suggestions = suggestions.get("suggestions", [suggestions])
        except Exception:
            suggestions = [{"title": "LLM Suggestion", "description": raw[:500]}]
        return {"id": id, "suggestions": suggestions}

    # ── Compare modules ───────────────────────────────────────────────────────

    @capability(
        "ml.compare",
        http_method="POST", http_path="/ml/compare", http_tags=["ml"],
        memory="on",
        description="Compare two modules: param counts, depth, layer families, shapes.",
    )
    async def ml_compare(id_a: str, id_b: str, trace_id=None):
        a, b = _MODULES.get(id_a), _MODULES.get(id_b)
        if not a:
            return {"error": f"Module {id_a} not found"}
        if not b:
            return {"error": f"Module {id_b} not found"}

        def _families(m):
            return {LAYER_CATALOGUE.get(n.get("type", ""), {}).get("family", "unknown")
                    for n in m.get("nodes", []) if n.get("type") not in ("input", "output")}

        return {
            "a": {"id": id_a, "name": a.get("name"), "params": _count_params(a),
                  "nodes": len(a.get("nodes", [])), "families": sorted(_families(a))},
            "b": {"id": id_b, "name": b.get("name"), "params": _count_params(b),
                  "nodes": len(b.get("nodes", [])), "families": sorted(_families(b))},
            "param_ratio": round(_count_params(b) / max(_count_params(a), 1), 3),
        }

    # ── Startup ───────────────────────────────────────────────────────────────

    async def _ml_startup():
        await asyncio.sleep(2)
        await _load_all_modules()
        log.info("ml_workshop ready — %d modules, numpy=%s torch=%s",
                 len(_MODULES), HAS_NP, HAS_TORCH)

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_ml_startup())
    except Exception:
        pass