"""
worldview_jepa.py  —  Vera WorldView (Graph-native, Concept-quantised, Dynamic)
================================================================================
A genuine world model for the fabric — not a pair-prediction toy.

Three coupled modules, trained end-to-end:

  1. GraphEncoder (GraphSAGE-style GNN)
     Encodes each FabricRecord using its neighborhood in the Loom + entity
     graph. A record's representation is a function of:
       • its own Ollama embedding (768-dim)
       • aggregated embeddings of its Loom-edge neighbors (per edge type)
       • aggregated embeddings of records sharing its entities

  2. ConceptBook (VQ-VAE codebook)
     Discretises the continuous GNN latent into K learned concepts.
     Each concept = a vector in latent space. Every record is assigned to
     its nearest concept via straight-through estimator (Oord et al. 2017).
     Concepts can be auto-labelled by an LLM from their member records.
     This gives the world model interpretable, named state.

  3. DynamicsTransformer (causal transformer over concept tokens)
     Trained on concept-token sequences extracted from:
       • Temporal walks: records in a dataset ordered by created_at
       • Graph walks:    biased random walks over the Loom graph
       • Entity walks:   sequences of records mentioning the same entity
     Learns p(c_{t+1} | c_1..c_t), enabling multi-step trajectory rollout
     and counterfactual conditioning.

  Together: a record is encoded with graph context, mapped to a concept,
  and the dynamics model can roll forward through concept space to predict
  what the world looks like next — across the whole fabric, not just one
  pair at a time.

Key design choices
──────────────────
  • Pure torch (no torch_geometric required) — message passing is implemented
    with index_add_, which works on CPU and CUDA without extra deps.
  • Three training stages can run independently or together. Stage 1 only
    needs Loom/entity edges. Stage 2 needs Stage 1. Stage 3 needs Stage 2.
  • EMA-updated VQ codebook (Razavi et al. 2019) — more stable than gradient
    updates and survives uneven concept usage.
  • Counterfactual rollout = swap one concept mid-sequence and re-decode.
  • Anomaly score = combination of (a) reconstruction distance in concept
    space and (b) low likelihood under the dynamics model.
  • Concepts get LLM-labelled lazily on first inspection.

Capabilities registered
───────────────────────
  worldview.train                   — full 3-stage training
  worldview.train_stage             — train one stage (gnn|codebook|dynamics)
  worldview.encode                  — encode text/record into latent + concept
  worldview.predict                 — single-step next-concept prediction
  worldview.rollout                 — multi-step trajectory through concepts
  worldview.counterfactual          — rollout with a concept swap
  worldview.query                   — nearest-neighbour search in latent space
  worldview.anomalies               — combined reconstruction + likelihood
  worldview.snapshot                — 2D projection for visualisation
  worldview.concepts                — list concepts with labels & populations
  worldview.concept_neighbors       — transition matrix slice for one concept
  worldview.concept_members         — records assigned to a concept
  worldview.explain_record          — show concept + neighbors + trajectory
  worldview.label_concepts          — LLM-label some or all concepts
  worldview.stats                   — full model + index + codebook stats
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import random
import sqlite3
import time
from collections import defaultdict, Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pathlib import Path 
_HERE = Path(__file__).parent


# CRITICAL: Set BLAS thread limits BEFORE numpy/torch are imported.
# OpenBLAS aborts (SIGABRT) when forked threads or asyncio's executor pool
# collide with BLAS's own threading. Setting these early prevents that.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("OMP_NUM_THREADS", "2")

log = logging.getLogger("vera.worldview_jepa")

# ── Orchestrator integration ──────────────────────────────────────────────────
import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import (
    capability, APP, emit_event, now_iso, schedule, ollama_generate, register_ui,
)
from Vera.Orchestration.config import cfg

# ── Optional heavy imports (graceful degradation) ─────────────────────────────
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    np = None
    HAS_NUMPY = False

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    HAS_TORCH = True
    # Prevent OpenBLAS/MKL thread contention with asyncio — a common
    # source of SIGABRT on home-server setups where the event loop and
    # BLAS both try to use multiple threads.
    torch.set_num_threads(2)
    if hasattr(torch, "set_num_interop_threads"):
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            pass  # already set
except ImportError:
    torch = nn = F = None
    HAS_TORCH = False

try:
    import faiss as _faiss
    HAS_FAISS = True
except ImportError:
    _faiss = None
    HAS_FAISS = False


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

EMBED_DIM        = int(os.getenv("WORLDVIEW_EMBED_DIM", "768"))
LATENT_DIM       = int(os.getenv("WORLDVIEW_LATENT_DIM", "256"))
HIDDEN_DIM       = int(os.getenv("WORLDVIEW_HIDDEN_DIM", "512"))
NUM_GNN_LAYERS   = int(os.getenv("WORLDVIEW_GNN_LAYERS", "2"))
NUM_CONCEPTS     = int(os.getenv("WORLDVIEW_NUM_CONCEPTS", "512"))
VQ_DECAY         = float(os.getenv("WORLDVIEW_VQ_DECAY", "0.99"))
VQ_COMMITMENT    = float(os.getenv("WORLDVIEW_VQ_COMMIT", "0.25"))
DYN_DIM          = int(os.getenv("WORLDVIEW_DYN_DIM", "192"))
DYN_HEADS        = int(os.getenv("WORLDVIEW_DYN_HEADS", "4"))
DYN_LAYERS       = int(os.getenv("WORLDVIEW_DYN_LAYERS", "3"))
DYN_CTX          = int(os.getenv("WORLDVIEW_DYN_CTX", "32"))
LR               = float(os.getenv("WORLDVIEW_LR", "3e-4"))
BATCH_SIZE       = int(os.getenv("WORLDVIEW_BATCH_SIZE", "128"))
MAX_NODES        = int(os.getenv("WORLDVIEW_MAX_NODES", "20000"))
MAX_WALKS        = int(os.getenv("WORLDVIEW_MAX_WALKS", "20000"))
WALK_LEN         = int(os.getenv("WORLDVIEW_WALK_LEN", "16"))
CHECKPOINT_DIR   = Path(os.getenv("WORLDVIEW_CHECKPOINT_DIR",
                                   str(Path.home() / ".vera" / "worldview")))
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_CKPT     = CHECKPOINT_DIR / "worldview_v2.pt"

LOOM_EDGE_TYPES = [
    "SIMILAR_TO", "SHARES_TOPIC", "DERIVED_FROM",
    "REFERENCES", "RELATED_TO", "CO_OCCURS",
    "TEMPORAL_NEXT", "ENTITY_LINK",
]
EDGE_TYPE_IDX = {t: i for i, t in enumerate(LOOM_EDGE_TYPES)}
NUM_EDGE_TYPES = len(LOOM_EDGE_TYPES)

TOKEN_BOS = 0
TOKEN_EOS = 1
NUM_SPECIAL_TOKENS = 2


# ─────────────────────────────────────────────────────────────────────────────
# GRAPH ENCODER  —  Multi-relational GraphSAGE
# ─────────────────────────────────────────────────────────────────────────────

class RelationalGNNLayer(nn.Module):
    """One multi-relational message-passing layer with mean aggregation."""
    def __init__(self, in_dim: int, out_dim: int, num_edge_types: int):
        super().__init__()
        self.edge_proj = nn.Parameter(torch.empty(num_edge_types, in_dim, out_dim))
        nn.init.xavier_uniform_(self.edge_proj)
        self.self_proj = nn.Linear(in_dim, out_dim)
        self.combine = nn.Sequential(
            nn.LayerNorm(out_dim * 2),
            nn.Linear(out_dim * 2, out_dim),
            nn.GELU(),
        )
        self.num_edge_types = num_edge_types

    def forward(self, x, src, dst, edge_type):
        N = x.size(0)
        out_dim = self.self_proj.out_features

        if src.numel() == 0:
            return self.combine(torch.cat(
                [self.self_proj(x), torch.zeros(N, out_dim, device=x.device)],
                dim=-1,
            ))

        # Process edges by type to avoid materialising a (E, in, out) tensor.
        # The old code did W = self.edge_proj[edge_type] which expanded the
        # (num_types, in, out) parameter to (E, in, out) — e.g. 14787×768×256
        # = 11.5 GB, causing OOM. Iterating over 8 types is negligible overhead.
        agg = torch.zeros(N, out_dim, device=x.device, dtype=x.dtype)
        counts = torch.zeros(N, device=x.device, dtype=x.dtype)

        for etype in range(self.num_edge_types):
            mask = (edge_type == etype)
            if not mask.any():
                continue
            e_src = src[mask]
            e_dst = dst[mask]
            # x_src @ W_type: (batch, in) @ (in, out) → (batch, out)
            msgs = x[e_src] @ self.edge_proj[etype]
            agg.index_add_(0, e_dst, msgs)
            counts.index_add_(0, e_dst, torch.ones(e_src.size(0),
                              device=x.device, dtype=x.dtype))

        counts = counts.clamp(min=1.0).unsqueeze(-1)
        agg = agg / counts

        return self.combine(torch.cat([self.self_proj(x), agg], dim=-1))


class GraphEncoder(nn.Module):
    """GNN encoder: input MLP → relational layers (residual) → output proj."""
    def __init__(self, embed_dim, hidden_dim, latent_dim, num_layers, num_edge_types):
        super().__init__()
        self.input_mlp = nn.Sequential(
            nn.Linear(embed_dim, hidden_dim),
            nn.LayerNorm(hidden_dim),
            nn.GELU(),
        )
        self.layers = nn.ModuleList([
            RelationalGNNLayer(hidden_dim, hidden_dim, num_edge_types)
            for _ in range(num_layers)
        ])
        self.output_proj = nn.Sequential(
            nn.Linear(hidden_dim, latent_dim),
            nn.LayerNorm(latent_dim),
        )

    def forward(self, x, src, dst, edge_type):
        h = self.input_mlp(x)
        for layer in self.layers:
            h = h + layer(h, src, dst, edge_type)
        return self.output_proj(h)


# ─────────────────────────────────────────────────────────────────────────────
# VQ CODEBOOK  —  Vector-Quantised concept book with EMA updates,
#   k-means++ init, periodic k-means reinit, entropy regularisation,
#   temperature-scaled assignment, and aggressive dead-code revival.
# ─────────────────────────────────────────────────────────────────────────────

class ConceptBook(nn.Module):
    """VQ-VAE codebook with strong anti-collapse guarantees.

    Key mechanisms that prevent the "only 2 out of 500 concepts" problem:
    1. k-means++ initialisation (good starting spread)
    2. Temperature-scaled soft assignment during training
    3. Aggressive dead-code revival EVERY step (not just at epoch end)
    4. Periodic full k-means re-initialisation of the codebook
    5. Strong entropy regularisation loss to force uniform usage
    6. EMA warmup schedule (fast early adaptation)
    """
    def __init__(self, num_concepts: int, dim: int, decay: float = 0.99,
                 commitment: float = 0.25):
        super().__init__()
        self.num_concepts = num_concepts
        self.dim = dim
        self.decay = decay
        self.commitment = commitment
        self._initial_decay = decay

        self.register_buffer("codebook", torch.randn(num_concepts, dim) * 0.1)
        self.register_buffer("ema_cluster_size", torch.zeros(num_concepts))
        self.register_buffer("ema_w", self.codebook.clone())
        self.register_buffer("initialised", torch.zeros(1))
        self.register_buffer("_total_forward", torch.zeros(1))
        # Track per-step assignment counts for balanced usage monitoring
        self.register_buffer("_step_assign_counts", torch.zeros(num_concepts))

    @torch.no_grad()
    def _init_codebook(self, z):
        """k-means++ initialisation for maximum initial spread."""
        n = z.size(0)
        if n < self.num_concepts:
            idx = torch.randint(0, n, (self.num_concepts,), device=z.device)
            self.codebook.copy_(z[idx])
        else:
            first = torch.randint(0, n, (1,), device=z.device)
            centroids = [z[first].squeeze(0)]
            for _ in range(self.num_concepts - 1):
                C = torch.stack(centroids, dim=0)
                d2 = torch.cdist(z, C).min(dim=1).values ** 2
                probs = d2 / (d2.sum() + 1e-8)
                next_idx = torch.multinomial(probs, 1).item()
                centroids.append(z[next_idx])
            self.codebook.copy_(torch.stack(centroids, dim=0))
        self.ema_w.copy_(self.codebook)
        self.ema_cluster_size.fill_(1.0)
        self._step_assign_counts.zero_()
        self.initialised.fill_(1.0)

    @torch.no_grad()
    def kmeans_reinit(self, z, n_iter: int = 10):
        """Full k-means re-initialisation from current latents.
        This is the nuclear anti-collapse option: run k-means on z and
        replace ALL codebook vectors with the resulting centroids.
        Guarantees every code sits near at least some data.
        """
        if z is None or z.size(0) < self.num_concepts:
            return 0
        device = z.device
        n, K = z.size(0), self.num_concepts

        # k-means++ init
        first = torch.randint(0, n, (1,), device=device)
        centroids = [z[first].squeeze(0)]
        for _ in range(K - 1):
            C = torch.stack(centroids, dim=0)
            d2 = torch.cdist(z, C).min(dim=1).values ** 2
            probs = d2 / (d2.sum() + 1e-8)
            next_idx = torch.multinomial(probs, 1).item()
            centroids.append(z[next_idx])
        centroids_t = torch.stack(centroids, dim=0)

        # k-means iterations
        for _ in range(n_iter):
            d = torch.cdist(z, centroids_t)
            assignments = d.argmin(dim=1)
            for k in range(K):
                mask = (assignments == k)
                if mask.any():
                    centroids_t[k] = z[mask].mean(dim=0)

        self.codebook.copy_(centroids_t)
        self.ema_w.copy_(centroids_t)
        # Reset EMA cluster sizes based on actual assignments
        d = torch.cdist(z, centroids_t)
        assignments = d.argmin(dim=1)
        for k in range(K):
            self.ema_cluster_size[k] = float((assignments == k).sum())
        self._step_assign_counts.zero_()
        return int((self.ema_cluster_size > 0).sum())

    def forward(self, z, jitter: float = 0.0, temperature: float = 0.0):
        """VQ forward pass with optional temperature-scaled soft assignment.

        temperature > 0: Gumbel-noise distance perturbation for exploration.
            Higher temperature → more uniform assignment → better codebook
            utilisation during early training. Anneal to 0 for hard assignment.
        jitter > 0: Gaussian noise on input z for symmetry breaking.
        """
        if self.initialised.item() < 0.5:
            self._init_codebook(z.detach())
        self._total_forward += 1

        z_in = z
        if jitter > 0 and self.training:
            z_in = z + torch.randn_like(z) * jitter

        d = (z_in.pow(2).sum(1, keepdim=True)
             + self.codebook.pow(2).sum(1).unsqueeze(0)
             - 2 * z_in @ self.codebook.t())

        # Temperature-scaled assignment (Gumbel noise for exploration)
        if temperature > 0 and self.training:
            gumbel = -torch.log(-torch.log(torch.rand_like(d) + 1e-10) + 1e-10)
            d_noisy = d - temperature * gumbel  # subtract noise from distances
            indices = d_noisy.argmin(dim=1)
        else:
            indices = d.argmin(dim=1)

        z_q = self.codebook[indices]

        if self.training:
            # Adaptive EMA decay — start low (0.85) for fast initial spread
            fwd_count = int(self._total_forward.item())
            warmup_steps = self.num_concepts * 3
            if fwd_count < warmup_steps:
                eff_decay = 0.85 + (self._initial_decay - 0.85) * (fwd_count / warmup_steps)
            else:
                eff_decay = self._initial_decay

            with torch.no_grad():
                one_hot = F.one_hot(indices, self.num_concepts).type(z.dtype)
                cluster_size = one_hot.sum(dim=0)
                self._step_assign_counts.add_(cluster_size)

                self.ema_cluster_size.mul_(eff_decay).add_(
                    cluster_size, alpha=1 - eff_decay)
                dw = one_hot.t() @ z.detach()
                self.ema_w.mul_(eff_decay).add_(dw, alpha=1 - eff_decay)
                n = self.ema_cluster_size.sum()
                smoothed = ((self.ema_cluster_size + 1e-5)
                            / (n + self.num_concepts * 1e-5) * n)
                self.codebook.copy_(self.ema_w / smoothed.unsqueeze(1))

        commitment_loss = F.mse_loss(z, z_q.detach())
        vq_loss = self.commitment * commitment_loss
        z_q_st = z + (z_q - z).detach()
        return z_q_st, indices, vq_loss

    def entropy_loss(self) -> torch.Tensor:
        """Negative entropy of code usage — higher = more collapsed."""
        sizes = self.ema_cluster_size
        total = sizes.sum() + 1e-8
        p = sizes / total
        p = p + 1e-8
        entropy = -(p * p.log()).sum()
        max_entropy = math.log(self.num_concepts)
        return (max_entropy - entropy) / max_entropy

    def perplexity(self) -> float:
        """Codebook perplexity — exp(entropy). Higher = more codes in use.
        Ideal: close to num_concepts. Bad: < 10."""
        sizes = self.ema_cluster_size
        total = sizes.sum() + 1e-8
        p = sizes / total + 1e-8
        entropy = -(p * p.log()).sum()
        return float(torch.exp(entropy).item())

    @torch.no_grad()
    def revive_dead_codes(self, z, threshold: float = 1e-3,
                          jitter: float = 0.02) -> int:
        """Re-seed dead codes from live encodings furthest from any live code.
        Also perturbs slightly underused codes for better spread."""
        if z is None or z.size(0) == 0:
            return 0
        sizes = self.ema_cluster_size
        dead = (sizes <= threshold).nonzero(as_tuple=False).flatten()
        if dead.numel() == 0:
            return 0
        live = (sizes > threshold)
        if live.any():
            d = torch.cdist(z, self.codebook[live]).min(dim=1).values
        else:
            d = torch.norm(z, dim=1)
        order = torch.argsort(d, descending=True)

        # Use more of z: cycle through if we have more dead codes than data
        n = dead.numel()
        if z.size(0) >= n:
            chosen = z[order[:n]]
        else:
            repeats = (n // z.size(0)) + 1
            expanded = z[order].repeat(repeats, 1)[:n]
            chosen = expanded + torch.randn_like(expanded) * jitter * 2
        if jitter > 0:
            chosen = chosen + torch.randn_like(chosen) * jitter
        dead = dead[:n]
        self.codebook[dead] = chosen
        self.ema_w[dead] = chosen
        # Give them a fair starting count — average of the live codes
        init_size = max(1.0, float(sizes[live].median()) * 0.3) if live.any() else 1.0
        self.ema_cluster_size[dead] = init_size
        return int(n)

    @torch.no_grad()
    def assign(self, z):
        d = (z.pow(2).sum(1, keepdim=True)
             + self.codebook.pow(2).sum(1).unsqueeze(0)
             - 2 * z @ self.codebook.t())
        return d.argmin(dim=1)

    @torch.no_grad()
    def lookup(self, indices):
        return self.codebook[indices]

    def usage_stats(self):
        with torch.no_grad():
            sizes = self.ema_cluster_size.detach().cpu().numpy()
            if sizes.sum() == 0:
                return {"active_concepts": 0,
                        "dead_concepts": self.num_concepts,
                        "entropy": 0.0,
                        "perplexity": 0.0,
                        "max_entropy": round(float(math.log(self.num_concepts)), 4)}
            p = sizes / sizes.sum()
            p_nz = p[p > 0]
            entropy = float(-(p_nz * np.log(p_nz + 1e-12)).sum())
            perplexity = float(np.exp(entropy))
            return {
                "active_concepts": int((sizes > 1e-3).sum()),
                "dead_concepts":   int((sizes <= 1e-3).sum()),
                "max_population":  float(sizes.max()),
                "min_population":  float(sizes.min()),
                "entropy":         round(entropy, 4),
                "perplexity":      round(perplexity, 1),
                "max_entropy":     round(float(math.log(self.num_concepts)), 4),
            }


# ─────────────────────────────────────────────────────────────────────────────
# DYNAMICS TRANSFORMER  —  Causal transformer over concept tokens
# ─────────────────────────────────────────────────────────────────────────────

class DynamicsTransformer(nn.Module):
    """Small GPT-style decoder operating on concept token sequences."""
    def __init__(self, num_concepts: int, dim: int = DYN_DIM,
                 heads: int = DYN_HEADS, layers: int = DYN_LAYERS,
                 max_ctx: int = DYN_CTX):
        super().__init__()
        self.vocab_size = num_concepts + NUM_SPECIAL_TOKENS
        self.max_ctx = max_ctx
        self.token_embed = nn.Embedding(self.vocab_size, dim)
        self.pos_embed   = nn.Embedding(max_ctx, dim)
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=dim, nhead=heads, dim_feedforward=dim * 4,
            dropout=0.1, activation="gelu", batch_first=True,
            norm_first=True,
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=layers)
        self.norm = nn.LayerNorm(dim)
        self.head = nn.Linear(dim, self.vocab_size, bias=False)
        self.head.weight = self.token_embed.weight  # tied

    def _causal_mask(self, T, device):
        return torch.triu(torch.ones(T, T, device=device), diagonal=1).bool()

    def forward(self, tokens):
        B, T = tokens.shape
        pos = torch.arange(T, device=tokens.device).unsqueeze(0).expand(B, T)
        h = self.token_embed(tokens) + self.pos_embed(pos)
        mask = self._causal_mask(T, tokens.device)
        try:
            h = self.transformer(h, mask=mask, is_causal=True)
        except TypeError:
            # older torch without is_causal kwarg
            h = self.transformer(h, mask=mask)
        h = self.norm(h)
        return self.head(h)

    @torch.no_grad()
    def rollout(self, prefix, steps, temperature=1.0, top_k=0):
        device = prefix.device
        seq = prefix.tolist()
        for _ in range(steps):
            window = seq[-self.max_ctx:]
            t = torch.tensor([window], device=device, dtype=torch.long)
            logits = self.forward(t)[0, -1]
            logits[TOKEN_BOS] = -1e9
            if temperature <= 0:
                nxt = int(logits.argmax().item())
            else:
                if top_k > 0:
                    v, _ = torch.topk(logits, min(top_k, logits.numel()))
                    logits = torch.where(logits < v[-1],
                                          torch.full_like(logits, -1e9), logits)
                probs = F.softmax(logits / max(temperature, 1e-6), dim=-1)
                nxt = int(torch.multinomial(probs, 1).item())
            if nxt == TOKEN_EOS:
                break
            seq.append(nxt)
        return torch.tensor(seq, device=device, dtype=torch.long)

    @torch.no_grad()
    def log_prob_of(self, tokens):
        if tokens.numel() < 2:
            return 0.0
        if tokens.dim() == 1:
            tokens = tokens.unsqueeze(0)
        logits = self.forward(tokens)
        log_probs = F.log_softmax(logits[:, :-1], dim=-1)
        targets = tokens[:, 1:].unsqueeze(-1)
        lp = log_probs.gather(-1, targets).squeeze(-1)
        return float(lp.mean().item())


# ─────────────────────────────────────────────────────────────────────────────
# JEPA PREDICTOR  —  predicts target latent from context latent
# ─────────────────────────────────────────────────────────────────────────────

class JEPAPredictor(nn.Module):
    """Narrow MLP that predicts target-encoder latents from context-encoder latents.
    This is the core JEPA component: no pixel/token reconstruction, only latent prediction."""
    def __init__(self, latent_dim: int, hidden_dim: int = 0):
        super().__init__()
        hd = hidden_dim or latent_dim * 2
        self.net = nn.Sequential(
            nn.Linear(latent_dim, hd),
            nn.LayerNorm(hd),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(hd, hd),
            nn.LayerNorm(hd),
            nn.GELU(),
            nn.Linear(hd, latent_dim),
        )
    def forward(self, z):
        return self.net(z)


def vicreg_loss(z_a, z_b, sim_weight=25.0, var_weight=25.0, cov_weight=1.0):
    """VICReg (Variance-Invariance-Covariance Regularization).
    Prevents representation collapse without requiring negative pairs.
    - Invariance: MSE between representations of linked nodes
    - Variance: keeps std of each dimension above a threshold
    - Covariance: decorrelates dimensions
    """
    # Invariance
    inv_loss = F.mse_loss(z_a, z_b)

    # Variance — std of each dim should be ≥ 1
    def _var(z):
        std = z.std(dim=0)
        return F.relu(1.0 - std).mean()
    var_loss = _var(z_a) + _var(z_b)

    # Covariance — off-diagonal of cov matrix should be small
    def _cov(z):
        n = z.size(0)
        z_c = z - z.mean(dim=0)
        cov_mat = (z_c.T @ z_c) / max(n - 1, 1)
        d = cov_mat.size(0)
        off_diag = cov_mat.pow(2).sum() - cov_mat.diagonal().pow(2).sum()
        return off_diag / max(d * (d - 1), 1)
    cov_loss = _cov(z_a) + _cov(z_b)

    return sim_weight * inv_loss + var_weight * var_loss + cov_weight * cov_loss


# ─────────────────────────────────────────────────────────────────────────────
# WORLDVIEW  —  the orchestrator that owns all three modules
# ─────────────────────────────────────────────────────────────────────────────

class WorldView:
    """Owns: GraphEncoder, ConceptBook, DynamicsTransformer."""
    def __init__(self, embed_dim=EMBED_DIM, latent_dim=LATENT_DIM,
                 hidden_dim=HIDDEN_DIM, num_concepts=NUM_CONCEPTS,
                 device=None):
        self.embed_dim    = embed_dim
        self.latent_dim   = latent_dim
        self.hidden_dim   = hidden_dim
        self.num_concepts = num_concepts
        self.device = device or (
            "cuda" if HAS_TORCH and torch.cuda.is_available() else "cpu"
        )

        self.ready = False
        self.train_steps = {"gnn": 0, "codebook": 0, "dynamics": 0}
        self.train_loss  = {"gnn": 0.0, "codebook": 0.0, "dynamics": 0.0}
        self.concept_labels: Dict[int, str] = {}
        self.record_concepts: Dict[str, int] = {}
        # Display metadata for each record, kept alongside concept assignment
        # so concept inspection / member listing works even after a restart
        # before the FAISS index is rebuilt.
        self.record_meta: Dict[str, Dict] = {}
        self.transition_counts: Dict[Tuple[int, int], int] = {}
        self.last_fabric_persist: str = ""
        self.last_fabric_load:    str = ""
        self._last_revived:       int = 0

        if not HAS_TORCH:
            log.warning("PyTorch not available — WorldView disabled")
            return

        self.gnn = GraphEncoder(
            embed_dim, hidden_dim, latent_dim,
            NUM_GNN_LAYERS, NUM_EDGE_TYPES
        ).to(self.device)
        self.codebook = ConceptBook(
            num_concepts, latent_dim,
            decay=VQ_DECAY, commitment=VQ_COMMITMENT
        ).to(self.device)
        self.dynamics = DynamicsTransformer(
            num_concepts, dim=DYN_DIM, heads=DYN_HEADS,
            layers=DYN_LAYERS, max_ctx=DYN_CTX,
        ).to(self.device)

        # JEPA target encoder — EMA copy of the GNN (never receives gradients)
        self.target_gnn = GraphEncoder(
            embed_dim, hidden_dim, latent_dim,
            NUM_GNN_LAYERS, NUM_EDGE_TYPES
        ).to(self.device)
        self.target_gnn.load_state_dict(self.gnn.state_dict())
        for p in self.target_gnn.parameters():
            p.requires_grad_(False)
        self._target_ema_decay = 0.996

        # JEPA predictor — predicts target-encoder latents from context-encoder
        self.predictor = JEPAPredictor(latent_dim, hidden_dim=latent_dim * 2).to(self.device)

        self.opt_gnn = torch.optim.AdamW(
            list(self.gnn.parameters()) + list(self.predictor.parameters()),
            lr=LR, weight_decay=1e-4)
        self.opt_dyn = torch.optim.AdamW(self.dynamics.parameters(),
                                          lr=LR, weight_decay=1e-4)
        self.ready = True
        log.info("WorldView v2 initialised — embed=%d latent=%d K=%d device=%s",
                 embed_dim, latent_dim, num_concepts, self.device)

    def reinitialize_for_embed_dim(self, new_embed_dim: int) -> bool:
        """Rebuild the GNN for a different input embedding dim.

        Keeps the ConceptBook and DynamicsTransformer if their dims are
        compatible (latent_dim and num_concepts are unchanged), so partial
        training is preserved. The GNN itself is reset.

        Called when the embeddings in Chroma turn out to have a different
        dimension than the configured default (e.g. 384 for MiniLM vs 768
        for nomic-embed-text).
        """
        if not HAS_TORCH:
            return False
        if new_embed_dim == self.embed_dim:
            return True  # nothing to do
        log.warning("WorldView: rebuilding GNN for embed_dim %d → %d "
                    "(GNN weights reset; codebook & dynamics preserved)",
                    self.embed_dim, new_embed_dim)
        self.embed_dim = new_embed_dim
        self.gnn = GraphEncoder(
            new_embed_dim, self.hidden_dim, self.latent_dim,
            NUM_GNN_LAYERS, NUM_EDGE_TYPES,
        ).to(self.device)
        # Rebuild JEPA target encoder and predictor
        self.target_gnn = GraphEncoder(
            new_embed_dim, self.hidden_dim, self.latent_dim,
            NUM_GNN_LAYERS, NUM_EDGE_TYPES,
        ).to(self.device)
        self.target_gnn.load_state_dict(self.gnn.state_dict())
        for p in self.target_gnn.parameters():
            p.requires_grad_(False)
        self.predictor = JEPAPredictor(
            self.latent_dim, hidden_dim=self.latent_dim * 2
        ).to(self.device)
        self.opt_gnn = torch.optim.AdamW(
            list(self.gnn.parameters()) + list(self.predictor.parameters()),
            lr=LR, weight_decay=1e-4)
        # GNN weights are now random — codebook & dynamics learned against
        # the OLD GNN are stale. Reset their training step counters so the
        # user is aware they need a fresh full training run.
        self.train_steps["gnn"] = 0
        self.train_loss["gnn"] = 0.0
        # Don't blow away the codebook/dynamics state — keep them in case
        # the user trains them again from the new GNN output. But mark them
        # invalid by clearing record_concepts (those came from old GNN).
        self.record_concepts = {}
        self.record_meta = {}
        self.transition_counts = {}
        return True

    # ── JEPA target encoder EMA update ───────────────────────────────────────

    @torch.no_grad()
    def _update_target_encoder(self):
        """EMA update: target_gnn ← decay * target_gnn + (1-decay) * gnn"""
        d = self._target_ema_decay
        for tp, sp in zip(self.target_gnn.parameters(), self.gnn.parameters()):
            tp.data.mul_(d).add_(sp.data, alpha=1.0 - d)

    # ── Stage 1: GNN contrastive + JEPA + VICReg training ─────────────────

    def train_gnn_step(self, x, src, dst, edge_type, pos_pairs, neg_pairs):
        if not self.ready:
            return 0.0
        self.gnn.train()
        self.predictor.train()
        self.target_gnn.eval()

        if isinstance(x, torch.Tensor):
            x_t = x.to(self.device)
            s_t = src.to(self.device)
            d_t = dst.to(self.device)
            e_t = edge_type.to(self.device)
        else:
            x_t = torch.tensor(x, dtype=torch.float32, device=self.device)
            s_t = torch.tensor(src, dtype=torch.long, device=self.device)
            d_t = torch.tensor(dst, dtype=torch.long, device=self.device)
            e_t = torch.tensor(edge_type, dtype=torch.long, device=self.device)

        # Context encoder (receives gradients)
        h = self.gnn(x_t, s_t, d_t, e_t)
        h_norm = F.normalize(h, dim=-1)

        # Target encoder (no gradients — EMA copy)
        with torch.no_grad():
            h_target = self.target_gnn(x_t, s_t, d_t, e_t)

        # ── Contrastive loss (margin-based) ───────────────────────
        pp = torch.tensor(pos_pairs, dtype=torch.long, device=self.device)
        np_ = torch.tensor(neg_pairs, dtype=torch.long, device=self.device)
        pos_sim = (h_norm[pp[:, 0]] * h_norm[pp[:, 1]]).sum(-1)
        neg_sim = (h_norm[np_[:, 0]] * h_norm[np_[:, 1]]).sum(-1)
        margin = _WV_CONFIG.get("contrastive_margin", 0.2)
        contrastive_loss = F.relu(margin - pos_sim + neg_sim).mean()

        # ── JEPA loss: predictor(context) should match target ─────
        # Use positive pairs: predict target[j] from context[i]
        pred_latent = self.predictor(h[pp[:, 0]])
        jepa_target = h_target[pp[:, 1]].detach()
        jepa_loss = F.smooth_l1_loss(pred_latent, jepa_target)

        # ── VICReg regularisation (prevents collapse) ─────────────
        sub_size = min(128, h.size(0))
        idx_a = torch.randperm(h.size(0), device=self.device)[:sub_size]
        idx_b = torch.randperm(h.size(0), device=self.device)[:sub_size]
        vic_loss = vicreg_loss(h[idx_a], h_target[idx_b].detach(),
                               sim_weight=10.0, var_weight=10.0, cov_weight=1.0)

        # ── Uniformity (backup anti-collapse) ─────────────────────
        sub = h_norm[:min(64, h.size(0))]
        uniformity = torch.tensor(0.0, device=self.device)
        if sub.size(0) > 1:
            uniformity_w = _WV_CONFIG.get("uniformity_weight", 0.1)
            uniformity = -torch.pdist(sub).pow(2).mul(-2).exp().mean().log() * uniformity_w

        # ── Combined loss ─────────────────────────────────────────
        jepa_w = _WV_CONFIG.get("jepa_weight", 1.0)
        vicreg_w = _WV_CONFIG.get("vicreg_weight", 0.5)
        total = contrastive_loss + jepa_w * jepa_loss + vicreg_w * vic_loss + uniformity

        self.opt_gnn.zero_grad()
        total.backward()
        torch.nn.utils.clip_grad_norm_(
            list(self.gnn.parameters()) + list(self.predictor.parameters()), 1.0)
        self.opt_gnn.step()

        # EMA update target encoder
        self._update_target_encoder()

        self.train_steps["gnn"] += 1
        lv = float(total.item())
        self.train_loss["gnn"] = 0.95 * self.train_loss["gnn"] + 0.05 * lv
        return lv

    # ── Stage 2: Codebook training with entropy regularization ──────────────

    def train_codebook_step(self, x, src, dst, edge_type):
        if not self.ready:
            return 0.0
        self.gnn.eval()
        self.codebook.train()
        with torch.no_grad():
            if isinstance(x, torch.Tensor):
                x_t = x.to(self.device)
                s_t = src.to(self.device)
                d_t = dst.to(self.device)
                e_t = edge_type.to(self.device)
            else:
                x_t = torch.tensor(x, dtype=torch.float32, device=self.device)
                s_t = torch.tensor(src, dtype=torch.long, device=self.device)
                d_t = torch.tensor(dst, dtype=torch.long, device=self.device)
                e_t = torch.tensor(edge_type, dtype=torch.long, device=self.device)
            h = self.gnn(x_t, s_t, d_t, e_t)

        cb_step = self.train_steps["codebook"]

        # Temperature annealing: start high for exploration, anneal to 0 (hard assignment)
        # This is critical for anti-collapse — prevents early winner-take-all
        temp_start = _WV_CONFIG.get("vq_temp_start", 2.0)
        temp_end   = _WV_CONFIG.get("vq_temp_end",   0.0)
        temp_anneal = _WV_CONFIG.get("vq_temp_anneal_steps", 40)
        if cb_step < temp_anneal:
            temperature = temp_start + (temp_end - temp_start) * (cb_step / temp_anneal)
        else:
            temperature = temp_end

        # Jitter during early codebook training
        jitter = _WV_CONFIG.get("vq_jitter", 0.05) if cb_step < 30 else 0.0

        _, _, vq_loss = self.codebook(h, jitter=jitter, temperature=temperature)

        # Entropy regularization — penalise collapsed codebook usage
        # Strong weight early (when collapse risk is highest), decay later
        entropy_base_w = _WV_CONFIG.get("entropy_weight", 5.0)
        if cb_step < 20:
            entropy_w = entropy_base_w * 3.0  # very strong early push for diversity
        elif cb_step < 50:
            entropy_w = entropy_base_w * 1.5
        else:
            entropy_w = entropy_base_w
        entropy_penalty = self.codebook.entropy_loss() * entropy_w

        total_loss = vq_loss + entropy_penalty

        # Dead code revival — run EVERY step for aggressive diversity
        try:
            if _WV_CONFIG.get("revive_dead", True):
                revive_jitter = _WV_CONFIG.get("revive_jitter", 0.03)
                self._last_revived = self.codebook.revive_dead_codes(
                    h, threshold=_WV_CONFIG.get("revive_threshold", 1e-3),
                    jitter=revive_jitter)
        except Exception:
            self._last_revived = 0

        # Periodic k-means reinit (nuclear option) — every N steps
        kmeans_every = _WV_CONFIG.get("kmeans_reinit_every", 0)
        if kmeans_every > 0 and cb_step > 0 and cb_step % kmeans_every == 0:
            try:
                n_active = self.codebook.kmeans_reinit(h)
                log.info("worldview: k-means reinit at step %d → %d active codes", cb_step, n_active)
            except Exception as e:
                log.debug("worldview: k-means reinit failed: %s", e)

        self.train_steps["codebook"] += 1
        lv = float(total_loss.item())
        self.train_loss["codebook"] = 0.95 * self.train_loss["codebook"] + 0.05 * lv
        return lv

    # ── Stage 3: Dynamics training ─────────────────────────────────────────

    def train_dynamics_step(self, token_seqs):
        if not self.ready or not token_seqs:
            return 0.0
        self.dynamics.train()
        T = min(max(len(s) for s in token_seqs), self.dynamics.max_ctx)
        B = len(token_seqs)
        tokens = torch.full((B, T), TOKEN_EOS, dtype=torch.long, device=self.device)
        for i, s in enumerate(token_seqs):
            s = s[:T]
            tokens[i, :len(s)] = torch.tensor(s, dtype=torch.long, device=self.device)

        logits = self.dynamics(tokens)
        loss = F.cross_entropy(
            logits[:, :-1].reshape(-1, logits.size(-1)),
            tokens[:, 1:].reshape(-1),
            ignore_index=TOKEN_EOS,
        )
        self.opt_dyn.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.dynamics.parameters(), 1.0)
        self.opt_dyn.step()
        self.train_steps["dynamics"] += 1
        lv = float(loss.item())
        self.train_loss["dynamics"] = 0.95 * self.train_loss["dynamics"] + 0.05 * lv
        return lv

    # ── Inference ──────────────────────────────────────────────────────────

    @torch.no_grad()
    def encode_subgraph(self, x, src, dst, edge_type):
        if not self.ready:
            return None
        self.gnn.eval()
        if isinstance(x, torch.Tensor):
            x_t = x.to(self.device)
            s_t = src.to(self.device)
            d_t = dst.to(self.device)
            e_t = edge_type.to(self.device)
        else:
            x_t = torch.tensor(x, dtype=torch.float32, device=self.device)
            s_t = torch.tensor(src, dtype=torch.long, device=self.device)
            d_t = torch.tensor(dst, dtype=torch.long, device=self.device)
            e_t = torch.tensor(edge_type, dtype=torch.long, device=self.device)
        return self.gnn(x_t, s_t, d_t, e_t).cpu().numpy()

    @torch.no_grad()
    def encode_isolated(self, embedding):
        if not self.ready:
            return None
        emb_arr = np.asarray(embedding, dtype="float32")
        if emb_arr.shape[-1] != self.embed_dim:
            log.warning("encode_isolated: embedding dim %d != model's %d",
                        emb_arr.shape[-1], self.embed_dim)
            return None
        self.gnn.eval()
        x = torch.tensor(emb_arr, device=self.device).unsqueeze(0)
        empty = torch.zeros(0, dtype=torch.long, device=self.device)
        h = self.gnn(x, empty, empty, empty)
        return h.cpu().numpy()[0]

    @torch.no_grad()
    def assign_concept(self, latent):
        if not self.ready:
            return -1
        z = torch.tensor(np.asarray(latent, dtype="float32"),
                          device=self.device)
        if z.dim() == 1:
            z = z.unsqueeze(0)
        return int(self.codebook.assign(z)[0].item())

    @torch.no_grad()
    def rollout(self, start_concept: int, steps: int = 8,
                temperature: float = 0.8, top_k: int = 20,
                pinned: Optional[List[Tuple[int, int]]] = None) -> List[int]:
        if not self.ready:
            return []
        prefix = [TOKEN_BOS, start_concept + NUM_SPECIAL_TOKENS]
        if pinned:
            for pos, c in pinned:
                while len(prefix) <= pos + 2:
                    prefix.append(c + NUM_SPECIAL_TOKENS)
                prefix[pos + 2] = c + NUM_SPECIAL_TOKENS
        t = torch.tensor(prefix, dtype=torch.long, device=self.device)
        seq = self.dynamics.rollout(t, steps,
                                     temperature=temperature, top_k=top_k)
        out = []
        for tok in seq.tolist():
            if tok < NUM_SPECIAL_TOKENS:
                continue
            out.append(tok - NUM_SPECIAL_TOKENS)
        return out

    @torch.no_grad()
    def sequence_log_prob(self, concept_seq: List[int]) -> float:
        if not self.ready or len(concept_seq) < 2:
            return 0.0
        toks = [TOKEN_BOS] + [c + NUM_SPECIAL_TOKENS for c in concept_seq]
        t = torch.tensor(toks, dtype=torch.long, device=self.device)
        return self.dynamics.log_prob_of(t)

    # ── Persistence ────────────────────────────────────────────────────────

    def _checkpoint_dict(self) -> Dict:
        ckpt = {
            "gnn":               self.gnn.state_dict(),
            "codebook":          self.codebook.state_dict(),
            "dynamics":          self.dynamics.state_dict(),
            "opt_gnn":           self.opt_gnn.state_dict(),
            "opt_dyn":           self.opt_dyn.state_dict(),
            "train_steps":       self.train_steps,
            "train_loss":        self.train_loss,
            "concept_labels":    self.concept_labels,
            "record_concepts":   self.record_concepts,
            "record_meta":       self.record_meta,
            "transition_counts": {f"{a},{b}": n
                                   for (a, b), n in self.transition_counts.items()},
            "config": {
                "embed_dim":    self.embed_dim,
                "latent_dim":   self.latent_dim,
                "hidden_dim":   self.hidden_dim,
                "num_concepts": self.num_concepts,
            },
        }
        # Save JEPA components if present
        if hasattr(self, "target_gnn"):
            ckpt["target_gnn"] = self.target_gnn.state_dict()
        if hasattr(self, "predictor"):
            ckpt["predictor"] = self.predictor.state_dict()
        return ckpt

    def serialize_bytes(self) -> Optional[bytes]:
        if not self.ready:
            return None
        try:
            buf = io.BytesIO()
            torch.save(self._checkpoint_dict(), buf)
            return buf.getvalue()
        except Exception as e:
            log.error("WorldView serialize_bytes failed: %s", e)
            return None

    def _restore_from_ckpt(self, ckpt: Dict) -> bool:
        cfg_ = ckpt.get("config", {})
        if cfg_.get("num_concepts", self.num_concepts) != self.num_concepts:
            log.warning("WorldView checkpoint has different K — skipping load")
            return False
        ckpt_embed = cfg_.get("embed_dim", self.embed_dim)
        if ckpt_embed != self.embed_dim:
            log.info("WorldView checkpoint embed_dim=%d differs from current %d — "
                     "rebuilding GNN to match checkpoint", ckpt_embed, self.embed_dim)
            self.reinitialize_for_embed_dim(ckpt_embed)
        self.gnn.load_state_dict(ckpt["gnn"])
        self.codebook.load_state_dict(ckpt["codebook"])
        self.dynamics.load_state_dict(ckpt["dynamics"])
        try:
            self.opt_gnn.load_state_dict(ckpt["opt_gnn"])
            self.opt_dyn.load_state_dict(ckpt["opt_dyn"])
        except Exception:
            pass
        self.train_steps     = ckpt.get("train_steps", self.train_steps)
        self.train_loss      = ckpt.get("train_loss",  self.train_loss)
        self.concept_labels  = ckpt.get("concept_labels", {})
        self.record_concepts = ckpt.get("record_concepts", {})
        self.record_meta     = ckpt.get("record_meta", {})
        tc = ckpt.get("transition_counts", {})
        self.transition_counts = {}
        for k, v in tc.items():
            try:
                if isinstance(k, str) and "," in k:
                    a, b = k.split(",", 1)
                    self.transition_counts[(int(a), int(b))] = v
                elif isinstance(k, (tuple, list)) and len(k) == 2:
                    self.transition_counts[(int(k[0]), int(k[1]))] = v
            except Exception:
                pass
        # Restore JEPA components if present
        if "target_gnn" in ckpt and hasattr(self, "target_gnn"):
            try: self.target_gnn.load_state_dict(ckpt["target_gnn"])
            except Exception: self.target_gnn.load_state_dict(self.gnn.state_dict())
        elif hasattr(self, "target_gnn"):
            self.target_gnn.load_state_dict(self.gnn.state_dict())
        if "predictor" in ckpt and hasattr(self, "predictor"):
            try: self.predictor.load_state_dict(ckpt["predictor"])
            except Exception: pass
        return True

    def load_bytes(self, blob: bytes) -> bool:
        if not HAS_TORCH or not blob:
            return False
        try:
            ckpt = torch.load(io.BytesIO(blob), map_location=self.device, weights_only=False)
            return self._restore_from_ckpt(ckpt)
        except Exception as e:
            log.error("WorldView load_bytes failed: %s", e)
            return False

    def save(self, path=None):
        if not self.ready:
            return
        path = path or DEFAULT_CKPT
        try:
            torch.save(self._checkpoint_dict(), str(path))
            log.info("WorldView saved → %s", path)
        except Exception as e:
            log.error("WorldView save failed: %s", e)

    def load(self, path=None):
        if not HAS_TORCH:
            return False
        path = path or DEFAULT_CKPT
        if not Path(path).exists():
            return False
        try:
            ckpt = torch.load(str(path), map_location=self.device, weights_only=False)
            ok = self._restore_from_ckpt(ckpt)
            if ok:
                log.info("WorldView loaded ← %s (gnn=%d cb=%d dyn=%d steps)",
                         path, self.train_steps["gnn"],
                         self.train_steps["codebook"], self.train_steps["dynamics"])
            return ok
        except Exception as e:
            log.error("WorldView load failed: %s", e)
            return False

    def stats(self) -> Dict:
        s = {
            "ready":         self.ready,
            "has_torch":     HAS_TORCH,
            "device":        str(self.device) if self.ready else "n/a",
            "embed_dim":     self.embed_dim,
            "latent_dim":    self.latent_dim,
            "num_concepts":  self.num_concepts,
            "train_steps":   self.train_steps,
            "train_loss":    {k: round(v, 6) for k, v in self.train_loss.items()},
            "records_assigned": len(self.record_concepts),
            "labelled_concepts": len(self.concept_labels),
            "transitions_observed": len(self.transition_counts),
            "last_fabric_persist": self.last_fabric_persist,
            "last_fabric_load":    self.last_fabric_load,
            "active_subview":      _WV_ACTIVE_SUBVIEW,
            "active_subview_datasets": _WV_ACTIVE_SUBVIEW_DATASETS,
        }
        if self.ready:
            s["codebook"] = self.codebook.usage_stats()
        return s


# ─────────────────────────────────────────────────────────────────────────────
# FAISS INDEX  —  latent vectors for nearest-neighbour queries
# ─────────────────────────────────────────────────────────────────────────────

class WorldViewIndex:
    """FAISS index over GNN-encoded latent vectors."""
    def __init__(self, dim=LATENT_DIM):
        self._dim = dim
        self._index = None
        self._ids: List[str] = []
        self._meta: Dict[str, Dict] = {}
        self._available = False

    def connect(self):
        if not HAS_FAISS or not HAS_NUMPY:
            return False
        try:
            self._index = _faiss.IndexFlatIP(self._dim)
            self._ids = []
            self._meta = {}
            self._available = True
            return True
        except Exception as e:
            log.error("WorldViewIndex: %s", e)
            return False

    def add_batch(self, ids, vectors, metas):
        if not self._available or not len(ids):
            return
        arr = np.asarray(vectors, dtype="float32")
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        arr = arr / norms
        self._index.add(arr)
        self._ids.extend(ids)
        for rid, m in zip(ids, metas):
            self._meta[rid] = m

    def search(self, vector, top_k=10):
        if not self._available or self._index.ntotal == 0:
            return []
        v = np.asarray(vector, dtype="float32").reshape(1, -1)
        n = np.linalg.norm(v)
        if n > 0:
            v = v / n
        k = min(top_k, self._index.ntotal)
        D, I = self._index.search(v, k)
        return [(self._ids[i], float(D[0][n_]))
                for n_, i in enumerate(I[0])
                if 0 <= i < len(self._ids)]

    def get_all_vectors(self):
        if not self._available or self._index.ntotal == 0:
            return [], None
        try:
            vecs = np.zeros((self._index.ntotal, self._dim), dtype="float32")
            self._index.reconstruct_n(0, self._index.ntotal, vecs)
        except Exception:
            try:
                vecs = _faiss.rev_swig_ptr(
                    self._index.get_xb(), self._index.ntotal * self._dim
                ).reshape(self._index.ntotal, self._dim).copy()
            except Exception:
                return [], None
        return self._ids[:self._index.ntotal], vecs

    @property
    def available(self):
        return self._available

    def stats(self):
        return {"available": self._available,
                "vectors":   self._index.ntotal if self._available else 0,
                "dim":       self._dim}


# ─────────────────────────────────────────────────────────────────────────────
# SINGLETONS
# ─────────────────────────────────────────────────────────────────────────────

MODEL = WorldView()
WV_INDEX = WorldViewIndex(dim=LATENT_DIM)
if HAS_FAISS and HAS_NUMPY:
    WV_INDEX.connect()
if HAS_TORCH:
    MODEL.load()


# ─────────────────────────────────────────────────────────────────────────────
# LOSS HISTORY  —  per-epoch loss for the training chart; survives restarts
# ─────────────────────────────────────────────────────────────────────────────

_WV_LOSS_HISTORY: Dict[str, List[Dict]] = {"gnn": [], "codebook": [], "dynamics": []}

def _loss_history_append(stage: str, epoch: int, loss: float, **extra):
    entry = {"epoch": epoch, "loss": round(loss, 6)}
    entry.update({k: v for k, v in extra.items() if v is not None})
    _WV_LOSS_HISTORY.setdefault(stage, []).append(entry)
    if len(_WV_LOSS_HISTORY[stage]) > 2000:
        _WV_LOSS_HISTORY[stage] = _WV_LOSS_HISTORY[stage][-2000:]

def _loss_history_reset():
    global _WV_LOSS_HISTORY
    _WV_LOSS_HISTORY = {"gnn": [], "codebook": [], "dynamics": []}

async def _persist_loss_history():
    fab = _get_fabric()
    if not fab: return
    try:
        blob = json.dumps(_WV_LOSS_HISTORY).encode()
        loop = asyncio.get_running_loop()
        def _w():
            conn = fab._sqlite_conn() if callable(getattr(fab, "_sqlite_conn", None)) else None
            if not conn: return
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS worldview_checkpoints("
                             "key TEXT PRIMARY KEY, blob BLOB, meta TEXT, updated_at TEXT)")
                conn.execute("INSERT INTO worldview_checkpoints(key,blob,meta,updated_at) "
                             "VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                             "blob=excluded.blob,meta=excluded.meta,updated_at=excluded.updated_at",
                             (WV_BLOB_KEY + "_loss", sqlite3.Binary(blob), "{}", now_iso()))
                conn.commit()
            finally:
                conn.close()
        await loop.run_in_executor(None, _w)
        log.debug("worldview: loss history persisted (%d gnn, %d cb, %d dyn epochs)",
                  len(_WV_LOSS_HISTORY.get("gnn",[])),
                  len(_WV_LOSS_HISTORY.get("codebook",[])),
                  len(_WV_LOSS_HISTORY.get("dynamics",[])))
    except Exception as e:
        log.debug("worldview: loss history persist failed: %s", e)

async def _load_loss_history():
    global _WV_LOSS_HISTORY
    fab = _get_fabric()
    if not fab: return
    try:
        loop = asyncio.get_running_loop()
        def _r():
            conn = fab._sqlite_conn() if callable(getattr(fab, "_sqlite_conn", None)) else None
            if not conn: return None
            try:
                cur = conn.execute("SELECT blob FROM worldview_checkpoints WHERE key=?",
                                   (WV_BLOB_KEY + "_loss",))
                row = cur.fetchone()
                return bytes(row[0]) if row and row[0] else None
            except sqlite3.OperationalError:
                return None
            finally:
                conn.close()
        blob = await loop.run_in_executor(None, _r)
        if blob:
            data = json.loads(blob.decode())
            if isinstance(data, dict):
                _WV_LOSS_HISTORY.update(data)
                log.info("worldview: loss history restored (%d gnn, %d cb, %d dyn epochs)",
                         len(_WV_LOSS_HISTORY.get("gnn",[])),
                         len(_WV_LOSS_HISTORY.get("codebook",[])),
                         len(_WV_LOSS_HISTORY.get("dynamics",[])))
    except Exception as e:
        log.debug("worldview: loss history load failed: %s", e)
# ─────────────────────────────────────────────────────────────────────────────

def _get_fabric():
    import sys
    return (sys.modules.get("Vera.Orchestration.fabric.data_fabric") or
            sys.modules.get("Vera.Orchestration.fabric.data_fabric"))


# ─────────────────────────────────────────────────────────────────────────────
# FABRIC PERSISTENCE  —  SQLite + Postgres checkpoint store/load
# ─────────────────────────────────────────────────────────────────────────────

WV_BLOB_KEY = os.getenv("WORLDVIEW_BLOB_KEY", "worldview_v2")

async def _fabric_store_checkpoint(blob: bytes, meta: Dict) -> Dict:
    fab = _get_fabric()
    out = {"sqlite": False, "postgres": False, "bytes": len(blob or b"")}
    if not fab or not blob:
        return out
    try:
        loop = asyncio.get_running_loop()
        def _w():
            conn = fab._sqlite_conn() if callable(getattr(fab, "_sqlite_conn", None)) else None
            if not conn:
                return False
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS worldview_checkpoints("
                    "key TEXT PRIMARY KEY, blob BLOB, meta TEXT, updated_at TEXT)")
                conn.execute(
                    "INSERT INTO worldview_checkpoints(key,blob,meta,updated_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                    "blob=excluded.blob, meta=excluded.meta, updated_at=excluded.updated_at",
                    (WV_BLOB_KEY, sqlite3.Binary(blob), json.dumps(meta), now_iso()))
                conn.commit()
                return True
            finally:
                conn.close()
        out["sqlite"] = bool(await loop.run_in_executor(None, _w))
    except Exception as e:
        log.warning("worldview: SQLite checkpoint store failed: %s", e)
    pg = getattr(fab, "FABRIC_PG", None)
    if pg is not None and getattr(pg, "available", False) and getattr(pg, "_pool", None):
        try:
            async with pg._pool.acquire() as conn:
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS worldview_checkpoints("
                    "key TEXT PRIMARY KEY, blob BYTEA, meta JSONB DEFAULT '{}',"
                    "updated_at TIMESTAMPTZ DEFAULT NOW())")
                await conn.execute(
                    "INSERT INTO worldview_checkpoints(key,blob,meta) VALUES($1,$2,$3) "
                    "ON CONFLICT(key) DO UPDATE SET blob=excluded.blob,"
                    "meta=excluded.meta, updated_at=NOW()",
                    WV_BLOB_KEY, blob, json.dumps(meta))
            out["postgres"] = True
        except Exception as e:
            log.debug("worldview: Postgres checkpoint store skipped: %s", e)
    return out


async def _fabric_load_checkpoint() -> Optional[bytes]:
    fab = _get_fabric()
    if not fab:
        return None
    pg = getattr(fab, "FABRIC_PG", None)
    if pg is not None and getattr(pg, "available", False) and getattr(pg, "_pool", None):
        try:
            async with pg._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT blob FROM worldview_checkpoints WHERE key=$1", WV_BLOB_KEY)
                if row and row["blob"]:
                    return bytes(row["blob"])
        except Exception as e:
            log.debug("worldview: Postgres checkpoint load skipped: %s", e)
    try:
        loop = asyncio.get_running_loop()
        def _r():
            conn = fab._sqlite_conn() if callable(getattr(fab, "_sqlite_conn", None)) else None
            if not conn:
                return None
            try:
                cur = conn.execute(
                    "SELECT blob FROM worldview_checkpoints WHERE key=?", (WV_BLOB_KEY,))
                r = cur.fetchone()
                return bytes(r[0]) if r and r[0] is not None else None
            except sqlite3.OperationalError:
                return None
            finally:
                conn.close()
        return await loop.run_in_executor(None, _r)
    except Exception as e:
        log.debug("worldview: SQLite checkpoint load failed: %s", e)
        return None


async def _persist_to_fabric() -> Dict:
    if not MODEL.ready:
        return {"ok": False, "error": "model not ready"}
    try:
        MODEL.save()
    except Exception:
        pass
    blob = MODEL.serialize_bytes()
    if not blob:
        return {"ok": False, "error": "serialize failed"}
    meta = {
        "train_steps": MODEL.train_steps, "num_concepts": MODEL.num_concepts,
        "embed_dim": MODEL.embed_dim, "records": len(MODEL.record_concepts),
        "saved_at": now_iso(),
    }
    res = await _fabric_store_checkpoint(blob, meta)
    if res.get("sqlite") or res.get("postgres"):
        MODEL.last_fabric_persist = now_iso()
        log.info("worldview: checkpoint persisted to fabric (%s)",
                 ", ".join(k for k in ("sqlite", "postgres") if res.get(k)))
    return {
        "ok": bool(res.get("sqlite") or res.get("postgres")),
        "stores": res, "bytes": res.get("bytes", 0),
    }


_wv_startup_done = False

async def _worldview_startup_load():
    global _wv_startup_done
    if _wv_startup_done:
        return
    if not HAS_TORCH or not MODEL.ready:
        _wv_startup_done = True
        return
    if MODEL.train_steps.get("gnn", 0) > 0:
        if not MODEL.last_fabric_load:
            MODEL.last_fabric_load = "local"
        # Still load loss history even if model came from local file
        await _load_loss_history()
        _wv_startup_done = True
        return
    try:
        blob = await _fabric_load_checkpoint()
    except Exception as e:
        log.debug("worldview startup load: %s", e)
        blob = None
    if blob and MODEL.load_bytes(blob):
        MODEL.last_fabric_load = now_iso()
        log.info("worldview: model restored from fabric checkpoint (gnn=%d steps, %d records)",
                 MODEL.train_steps.get("gnn", 0), len(MODEL.record_concepts))
    await _load_loss_history()
    _wv_startup_done = True


async def _fetch_records_with_embeddings(dataset_id: str = "",
                                          limit: int = MAX_NODES,
                                          node_ids: list = None):
    """Fetch records from Chroma for graph building.

    Returns (rids, embeddings_np, meta_dict) where:
      - rids:          list of record IDs in order
      - embeddings_np: numpy float32 array of shape (N, dim) — NO Python list conversion
      - meta_dict:     {rid: {dataset_id, text, created_at, tags}} — metadata only

    Keeping embeddings as a contiguous numpy array avoids the ~120 MB overhead
    of converting 5000+ × 768 numpy rows into Python lists of float objects.
    """
    fab = _get_fabric()
    if not fab:
        log.warning("worldview: data_fabric module not loaded")
        return [], None, {}

    # ── Primary path: Chroma ──────────────────────────────────────────────
    chroma = getattr(fab, "FABRIC_CHROMA", None)
    if chroma is not None and getattr(chroma, "available", False):
        try:
            loop = asyncio.get_running_loop()
            def _fetch_chroma():
                col = chroma._col
                if col is None:
                    return [], None, {}
                kwargs = {
                    "include": ["embeddings", "documents", "metadatas"],
                    "limit":   min(limit, 50000),
                }
                if node_ids:
                    # Train on an explicit set of record IDs (e.g. the exact
                    # node set of a saved vera-graph), not a whole dataset.
                    kwargs["ids"] = list(node_ids)[:50000]
                    kwargs.pop("limit", None)
                elif dataset_id:
                    kwargs["where"] = {"dataset_id": {"$eq": dataset_id}}
                res = col.get(**kwargs)
                ids   = res.get("ids")        if res.get("ids")        is not None else []
                embs  = res.get("embeddings") if res.get("embeddings") is not None else []
                docs  = res.get("documents")  if res.get("documents")  is not None else []
                metas = res.get("metadatas")  if res.get("metadatas")  is not None else []
                n_ids   = len(ids)
                n_embs  = len(embs)  if hasattr(embs, "__len__")  else 0
                n_docs  = len(docs)  if hasattr(docs, "__len__")  else 0
                n_metas = len(metas) if hasattr(metas, "__len__") else 0

                # Build rids list, meta dict, and collect valid embedding indices
                out_rids = []
                out_meta = {}
                valid_indices = []
                for i in range(n_ids):
                    if i >= n_embs:
                        continue
                    emb = embs[i]
                    if emb is None:
                        continue
                    # Quick length check without converting to list
                    try:
                        emb_len = len(emb) if hasattr(emb, "__len__") else 0
                    except Exception:
                        continue
                    if emb_len < 10:
                        continue
                    rid = ids[i]
                    meta = metas[i] if i < n_metas else {}
                    if meta is None:
                        meta = {}
                    text = (docs[i] if i < n_docs else "") or ""
                    tags = meta.get("tags", [])
                    if isinstance(tags, str):
                        try:
                            tags = json.loads(tags)
                        except Exception:
                            tags = []
                    out_rids.append(rid)
                    valid_indices.append(i)
                    out_meta[rid] = {
                        "dataset_id": meta.get("dataset_id", "") or "",
                        "text":       (text or "")[:200],
                        "created_at": meta.get("created_at", "") or "",
                        "tags":       tags or [],
                    }

                # Build embeddings numpy array directly from Chroma's array
                # without converting each row to a Python list first.
                if valid_indices and hasattr(embs, "__array__"):
                    # embs is already numpy — slice the valid rows
                    emb_np = np.array(embs, dtype="float32")
                    emb_np = emb_np[valid_indices]
                elif valid_indices:
                    # embs is a list of lists/arrays
                    emb_np = np.array([embs[i] for i in valid_indices], dtype="float32")
                else:
                    emb_np = None

                return out_rids, emb_np, out_meta

            rids, emb_np, meta = await loop.run_in_executor(None, _fetch_chroma)
            log.info("worldview: fetched %d records from Chroma "
                     "(dataset=%s, limit=%d)",
                     len(rids), dataset_id or "*", limit)
            if rids:
                return rids, emb_np, meta
        except Exception as e:
            log.warning("worldview: Chroma fetch failed: %s", e)

    return [], None, {}


async def _fetch_loom_edges(record_ids: set) -> List[Tuple[str, str, str]]:
    fab = _get_fabric()
    if not fab or not hasattr(fab, "FABRIC_NEO") or not fab.FABRIC_NEO.available:
        return []
    rel_list = ["SIMILAR_TO", "SHARES_TOPIC", "DERIVED_FROM",
                "REFERENCES", "RELATED_TO", "CO_OCCURS"]
    try:
        ids = list(record_ids)
        rows = await fab.FABRIC_NEO.query(
            "MATCH (a:FabricRecord)-[r]->(b:FabricRecord) "
            "WHERE a.id IN $ids AND b.id IN $ids "
            "AND type(r) IN $rels "
            "RETURN a.id AS src, b.id AS dst, type(r) AS rel",
            {"ids": ids, "rels": rel_list},
        )
        return [(r["src"], r["dst"], r["rel"]) for r in (rows or [])
                if "src" in r and "dst" in r]
    except Exception as e:
        log.debug("worldview loom fetch: %s", e)
        return []


async def _fetch_entity_links(record_ids: set,
                               max_entity_fanout: int = 30
                               ) -> List[Tuple[str, str, str]]:
    fab = _get_fabric()
    if not fab or not hasattr(fab, "FABRIC_NEO") or not fab.FABRIC_NEO.available:
        return []
    try:
        ids = list(record_ids)
        rows = await fab.FABRIC_NEO.query(
            "MATCH (e:Entity)-[:MENTIONED_IN]->(r:FabricRecord) "
            "WHERE r.id IN $ids "
            "RETURN e.id AS eid, r.id AS rid",
            {"ids": ids},
        )
        ent_map = defaultdict(list)
        for r in (rows or []):
            ent_map[r["eid"]].append(r["rid"])
        edges = []
        for eid, recs in ent_map.items():
            if len(recs) < 2 or len(recs) > max_entity_fanout:
                continue
            for i in range(len(recs)):
                for j in range(i + 1, len(recs)):
                    edges.append((recs[i], recs[j], "ENTITY_LINK"))
                    edges.append((recs[j], recs[i], "ENTITY_LINK"))
        return edges
    except Exception as e:
        log.debug("worldview entity fetch: %s", e)
        return []


def _build_temporal_edges(records: Dict[str, Dict]) -> List[Tuple[str, str, str]]:
    by_ds = defaultdict(list)
    for rid, r in records.items():
        by_ds[r["dataset_id"]].append((r.get("created_at", ""), rid))
    edges = []
    for ds, items in by_ds.items():
        items.sort()
        for i in range(len(items) - 1):
            edges.append((items[i][1], items[i + 1][1], "TEMPORAL_NEXT"))
    return edges


async def _diagnose_no_records(dataset_id: str = "") -> Dict:
    """Return a diagnostic dict explaining why the fabric appears empty."""
    diag = {
        "fabric_module_loaded":   False,
        "chroma_available":       False,
        "chroma_total_count":     0,
        "chroma_dataset_count":   None if not dataset_id else 0,
        "chroma_datasets_seen":   [],
        "chroma_sample_has_emb":  None,
        "chroma_sample_emb_dim":  None,
        "chroma_sample_meta":     None,
        "chroma_collection_name": "",
        "sqlite_total_count":     0,
        "sqlite_dataset_count":   None if not dataset_id else 0,
        "sqlite_datasets_seen":   [],
        "pg_available":           False,
        "dataset_id":             dataset_id or None,
        "hint":                   "",
    }
    fab = _get_fabric()
    if not fab:
        diag["hint"] = ("data_fabric module not loaded — confirm worldview_jepa "
                        "is imported AFTER data_fabric at startup.")
        return diag
    diag["fabric_module_loaded"] = True

    # ── Chroma deep inspection ──────────────────────────────────────────
    chroma = getattr(fab, "FABRIC_CHROMA", None)
    if chroma is not None and getattr(chroma, "available", False):
        diag["chroma_available"] = True
        diag["chroma_collection_name"] = getattr(chroma, "COLLECTION", "?")
        try:
            loop = asyncio.get_running_loop()
            def _inspect_chroma():
                col = chroma._col
                if col is None:
                    return {}
                d = {}
                d["total"] = col.count()

                # Sample fetch — confirms embeddings actually come back
                try:
                    sample = col.get(limit=3,
                                     include=["embeddings", "documents", "metadatas"])
                    s_embs = sample.get("embeddings")
                    s_metas = sample.get("metadatas") or []
                    if s_embs is not None and len(s_embs) > 0:
                        first_emb = s_embs[0]
                        if first_emb is not None:
                            try:
                                d["sample_has_emb"] = True
                                d["sample_emb_dim"] = len(first_emb)
                            except Exception:
                                d["sample_has_emb"] = False
                        else:
                            d["sample_has_emb"] = False
                    else:
                        d["sample_has_emb"] = False
                    d["sample_meta"] = s_metas[0] if s_metas else None
                except Exception as e:
                    d["sample_error"] = str(e)[:200]

                # Per-dataset count
                if dataset_id:
                    try:
                        r = col.get(where={"dataset_id": {"$eq": dataset_id}},
                                     include=[])
                        d["dataset_count"] = len(r.get("ids") or [])
                    except Exception:
                        d["dataset_count"] = 0

                # What dataset_ids EXIST in Chroma's metadata?
                # We pull a sample of metadatas across the collection and
                # count distinct dataset_id values.
                try:
                    # Larger sample → more accurate distinct list. Cap at 5k
                    # to keep response time reasonable.
                    big = col.get(limit=min(d["total"], 5000),
                                  include=["metadatas"])
                    counts: Dict[str, int] = {}
                    no_ds_count = 0
                    for m in (big.get("metadatas") or []):
                        if not m:
                            no_ds_count += 1
                            continue
                        ds = m.get("dataset_id") or ""
                        if ds:
                            counts[ds] = counts.get(ds, 0) + 1
                        else:
                            no_ds_count += 1
                    # Sort by count desc
                    d["datasets_seen"] = sorted(
                        [{"dataset_id": k, "count": v} for k, v in counts.items()],
                        key=lambda x: -x["count"],
                    )[:30]
                    d["no_dataset_id"] = no_ds_count
                except Exception as e:
                    d["dataset_scan_error"] = str(e)[:200]
                return d
            d = await loop.run_in_executor(None, _inspect_chroma)
            diag["chroma_total_count"]    = d.get("total", 0)
            diag["chroma_dataset_count"]  = d.get("dataset_count",
                                                  diag["chroma_dataset_count"])
            diag["chroma_datasets_seen"]  = d.get("datasets_seen", [])
            diag["chroma_no_dataset_id"]  = d.get("no_dataset_id", 0)
            diag["chroma_sample_has_emb"] = d.get("sample_has_emb")
            diag["chroma_sample_emb_dim"] = d.get("sample_emb_dim")
            diag["chroma_sample_meta"]    = d.get("sample_meta")
            if "sample_error" in d:
                diag["chroma_sample_error"] = d["sample_error"]
            if "dataset_scan_error" in d:
                diag["chroma_dataset_scan_error"] = d["dataset_scan_error"]
        except Exception as e:
            diag["chroma_error"] = str(e)[:200]

    # ── SQLite stats ─────────────────────────────────────────────────────
    try:
        loop = asyncio.get_running_loop()
        def _sqlite_count():
            conn = fab._sqlite_conn()
            if not conn:
                return 0, 0, []
            total = conn.execute("SELECT COUNT(*) FROM fabric_records").fetchone()[0]
            ds_n = 0
            if dataset_id:
                ds_n = conn.execute(
                    "SELECT COUNT(*) FROM fabric_records WHERE dataset_id = ?",
                    (dataset_id,)).fetchone()[0]
            datasets = conn.execute(
                "SELECT dataset_id, COUNT(*) as n FROM fabric_records "
                "GROUP BY dataset_id ORDER BY n DESC LIMIT 30"
            ).fetchall()
            return total, ds_n, [{"dataset_id": r[0], "count": r[1]} for r in datasets]
        total, ds_n, datasets = await loop.run_in_executor(None, _sqlite_count)
        diag["sqlite_total_count"] = total
        diag["sqlite_dataset_count"] = ds_n
        diag["sqlite_datasets_seen"] = datasets
    except Exception as e:
        diag["sqlite_error"] = str(e)[:200]

    # PG availability (records have no embeddings there but worth reporting)
    pg = getattr(fab, "FABRIC_PG", None)
    diag["pg_available"] = bool(pg and getattr(pg, "available", False))

    # ── Synthesise hint ──────────────────────────────────────────────────
    if not diag["chroma_available"]:
        diag["hint"] = ("Chroma is not connected — embeddings live there. "
                        "Check CHROMA_HOST/CHROMA_PORT and ensure the Chroma "
                        "server is reachable.")
    elif diag["chroma_total_count"] == 0:
        if diag["sqlite_total_count"] == 0:
            diag["hint"] = "Fabric is empty — ingest some data first."
        else:
            diag["hint"] = (f"SQLite has {diag['sqlite_total_count']} records but "
                            "Chroma is empty. The embedding pipeline stage may "
                            "have failed during ingest.")
    elif diag["chroma_sample_has_emb"] is False:
        diag["hint"] = ("Chroma has records but the sample fetch returned NO "
                        "embeddings — they were never written. Check whether "
                        "the ingest pipeline's embed stage was running when "
                        "these records were stored.")
    else:
        # Chroma has embeddings — compare what dataset_ids are in each store
        chroma_ds_set = {d["dataset_id"] for d in diag["chroma_datasets_seen"]}
        sqlite_ds_set = {d["dataset_id"] for d in diag["sqlite_datasets_seen"]}
        only_in_sqlite = sqlite_ds_set - chroma_ds_set
        if dataset_id and dataset_id not in chroma_ds_set:
            diag["hint"] = (f"Dataset '{dataset_id}' exists in SQLite but NOT in "
                            f"Chroma. Chroma has these datasets: "
                            f"{sorted(chroma_ds_set)[:10]}. "
                            f"You can either (a) re-ingest '{dataset_id}' so its "
                            "embed stage runs, or (b) train on a dataset Chroma "
                            "does have. Or train on ALL datasets (leave blank).")
        elif only_in_sqlite and not dataset_id:
            diag["hint"] = (f"Chroma has {diag['chroma_total_count']} embedded "
                            f"records across {len(chroma_ds_set)} datasets, but "
                            f"{len(only_in_sqlite)} other datasets exist only in "
                            f"SQLite with no embeddings: "
                            f"{sorted(only_in_sqlite)[:5]}. Train will use only "
                            "the Chroma-embedded subset.")
        else:
            diag["hint"] = ("Chroma has data and embeddings. If training still "
                            "reports 0 records, check server logs for the "
                            "actual `worldview Chroma fetch` line and look "
                            "for an exception.")

    return diag



async def _resolve_graph_node_ids_to_records(
    node_ids: List[str],
    max_depth: int = 3,
    limit: int = 50000,
) -> List[str]:
    """Resolve vera-graph node IDs to Chroma FabricRecord UUIDs.

    vera-graph sends whatever node IDs are in the graph — these may be
    Dataset, Entity, Session, Memory, FabricRecord, Category, Source nodes.
    Only FabricRecord IDs are directly addressable in Chroma.

    Resolution order:
      1. Plain UUID or FabricRecord:uuid  → used directly
      2. Dataset:id   → MATCH (d:Dataset)-[:CONTAINS]->(r:FabricRecord)
      3. Entity:id    → MATCH (e:Entity)-[:MENTIONED_IN]->(r:FabricRecord)
      4. Source:id    → MATCH (s:Source)-[:CONTAINS|HAS_RECORD]->(r:FabricRecord)
      5. Other nodes  → probe Neo4j for .dataset_id prop or label; re-resolve
      6. Deep walk up to max_depth hops from all node_ids → any reachable FabricRecord
      7. Fallback: pull dataset records from Chroma by metadata filter

    Returns a deduplicated list of FabricRecord IDs present in Chroma.
    """
    if not node_ids:
        return []

    fab    = _get_fabric()
    neo    = getattr(fab, "FABRIC_NEO",    None) if fab else None
    chroma = getattr(fab, "FABRIC_CHROMA", None) if fab else None

    collected:        set = set()
    datasets_to_pull: set = set()

    import re as _re
    _UUID_RE = _re.compile(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
        _re.IGNORECASE,
    )

    fabric_record_ids: List[str] = []
    dataset_ids:       List[str] = []
    entity_ids:        List[str] = []
    source_ids:        List[str] = []
    other_ids:         List[str] = []

    for nid in node_ids:
        if not nid:
            continue
        lo = nid.lower()
        if _UUID_RE.match(nid):
            fabric_record_ids.append(nid)
        elif lo.startswith('fabricrecord:'):
            fabric_record_ids.append(nid.split(':', 1)[1])
        elif lo.startswith('dataset:'):
            dataset_ids.append(nid.split(':', 1)[1])
        elif lo.startswith('entity:'):
            entity_ids.append(nid.split(':', 1)[1])
        elif lo.startswith('source:'):
            source_ids.append(nid.split(':', 1)[1])
        else:
            other_ids.append(nid)

    collected.update(fabric_record_ids)

    if neo and neo.available:
        # Dataset → FabricRecord
        if dataset_ids:
            try:
                rows = await neo.query(
                    "MATCH (d:Dataset)-[:CONTAINS]->(r:FabricRecord) "
                    "WHERE d.id IN $ids RETURN r.id AS rid LIMIT $lim",
                    {"ids": dataset_ids[:500], "lim": limit},
                )
                for row in (rows or []):
                    if row.get("rid"): collected.add(row["rid"])
            except Exception as e:
                log.debug("worldview resolve dataset→record: %s", e)

        # Entity → FabricRecord
        if entity_ids:
            try:
                rows = await neo.query(
                    "MATCH (e:Entity)-[:MENTIONED_IN]->(r:FabricRecord) "
                    "WHERE e.id IN $ids RETURN r.id AS rid LIMIT $lim",
                    {"ids": entity_ids[:500], "lim": limit},
                )
                for row in (rows or []):
                    if row.get("rid"): collected.add(row["rid"])
            except Exception as e:
                log.debug("worldview resolve entity→record: %s", e)

        # Source → FabricRecord
        if source_ids:
            try:
                rows = await neo.query(
                    "MATCH (s:Source)-[:CONTAINS|HAS_RECORD]->(r:FabricRecord) "
                    "WHERE s.id IN $ids RETURN r.id AS rid LIMIT $lim",
                    {"ids": source_ids[:500], "lim": limit},
                )
                for row in (rows or []):
                    if row.get("rid"): collected.add(row["rid"])
            except Exception as e:
                log.debug("worldview resolve source→record: %s", e)

        # Other IDs: probe labels + dataset_id prop
        if other_ids:
            try:
                rows = await neo.query(
                    "MATCH (n) WHERE n.id IN $ids "
                    "RETURN n.id AS nid, labels(n) AS lbls, "
                    "       n.dataset_id AS dsid, "
                    "       CASE WHEN 'FabricRecord' IN labels(n) THEN n.id ELSE null END AS rid",
                    {"ids": other_ids[:500]},
                )
                extra_datasets: List[str] = []
                for row in (rows or []):
                    if row.get("rid"):
                        collected.add(row["rid"])
                    elif row.get("dsid"):
                        datasets_to_pull.add(row["dsid"])
                    lbls = row.get("lbls") or []
                    if "Dataset" in lbls and row.get("nid"):
                        extra_datasets.append(row["nid"])
                if extra_datasets:
                    rows2 = await neo.query(
                        "MATCH (d:Dataset)-[:CONTAINS]->(r:FabricRecord) "
                        "WHERE d.id IN $ids RETURN r.id AS rid LIMIT $lim",
                        {"ids": extra_datasets[:500], "lim": limit},
                    )
                    for row in (rows2 or []):
                        if row.get("rid"): collected.add(row["rid"])
            except Exception as e:
                log.debug("worldview resolve other→record: %s", e)

        # Deep walk: up to max_depth hops from all input nodes
        # catches FabricRecords nested under any container type
        if max_depth >= 1:
            try:
                rows = await neo.query(
                    f"MATCH (start)-[*1..{min(max_depth, 4)}]-(r:FabricRecord) "
                    "WHERE start.id IN $ids "
                    "RETURN DISTINCT r.id AS rid LIMIT $lim",
                    {"ids": list(node_ids)[:200], "lim": limit},
                )
                before = len(collected)
                for row in (rows or []):
                    if row.get("rid"): collected.add(row["rid"])
                log.info("worldview deep walk: +%d records (depth=%d)",
                         len(collected) - before, max_depth)
            except Exception as e:
                log.debug("worldview deep traversal: %s", e)

    # Fallback: pull Chroma records by dataset_id metadata filter
    if datasets_to_pull and chroma and chroma.available:
        n_ds = max(1, len(datasets_to_pull))
        per_ds = min(limit // n_ds, 10000)
        for dsid in list(datasets_to_pull)[:20]:
            try:
                loop = asyncio.get_running_loop()
                def _pull(_dsid=dsid, _per=per_ds):
                    res = chroma._col.get(
                        where={"dataset_id": {"$eq": _dsid}},
                        include=["metadatas"],
                        limit=_per,
                    )
                    return res.get("ids") or []
                ids_from_ds = await loop.run_in_executor(None, _pull)
                collected.update(ids_from_ds)
            except Exception as e:
                log.debug("worldview pull dataset %s: %s", dsid, e)

    result = list(collected)[:limit]
    log.info("worldview: resolved %d graph node IDs → %d Chroma record IDs",
             len(node_ids), len(result))
    return result


async def build_graph(dataset_id: str = "", limit: int = MAX_NODES,
                      node_ids: list = None) -> Dict:
    """Build full training subgraph from resolved FabricRecord IDs.

    node_ids, when provided, should already be resolved to Chroma-addressable
    FabricRecord UUIDs via _resolve_graph_node_ids_to_records().
    Pass raw vera-graph node IDs through that function first.
    """
    rids, emb_np, records = await _fetch_records_with_embeddings(
        dataset_id, limit, node_ids=node_ids)
    if not rids or emb_np is None:
        return {"records": {}, "rids": [], "X": None}

    idx_of = {rid: i for i, rid in enumerate(rids)}
    # emb_np is already float32 numpy — just normalise in-place
    norms = np.linalg.norm(emb_np, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    emb_np /= norms

    edge_set = set()
    edges: List[Tuple[str, str, str]] = []

    for s, d, rel in await _fetch_loom_edges(set(rids)):
        if s in idx_of and d in idx_of:
            key = (s, d, rel)
            if key not in edge_set:
                edges.append(key); edge_set.add(key)

    for s, d, rel in await _fetch_entity_links(set(rids)):
        if s in idx_of and d in idx_of:
            key = (s, d, rel)
            if key not in edge_set:
                edges.append(key); edge_set.add(key)

    for s, d, rel in _build_temporal_edges(records):
        if s in idx_of and d in idx_of:
            key = (s, d, rel)
            if key not in edge_set:
                edges.append(key); edge_set.add(key)

    for rid in rids:
        edges.append((rid, rid, "RELATED_TO"))

    src = np.array([idx_of[e[0]] for e in edges], dtype="int64")
    dst = np.array([idx_of[e[1]] for e in edges], dtype="int64")
    edge_type = np.array([EDGE_TYPE_IDX.get(e[2], 0) for e in edges], dtype="int64")

    # Pre-convert to torch tensors so training steps don't re-create them
    # every epoch. emb_np is the only copy of the embeddings — no Python
    # list conversion was done, keeping peak memory ~120 MB lower.
    result = {
        "records": records, "idx_of": idx_of, "rids": rids,
        "X": emb_np, "src": src, "dst": dst, "edge_type": edge_type,
        "edges": edges,
    }
    if torch is not None:
        try:
            dev = "cpu"
            result["X_t"]  = torch.tensor(emb_np, dtype=torch.float32, device=dev)
            result["s_t"]  = torch.tensor(src, dtype=torch.long, device=dev)
            result["d_t"]  = torch.tensor(dst, dtype=torch.long, device=dev)
            result["e_t"]  = torch.tensor(edge_type, dtype=torch.long, device=dev)
            # Free numpy copies now that we have independent tensors
            del result["X"], result["src"], result["dst"], result["edge_type"]
            # Also free the original numpy arrays
            del emb_np, src, dst, edge_type
        except Exception as e:
            log.warning("worldview: torch pre-conversion failed, falling back to numpy: %s", e)

    log.info("WorldView graph: %d nodes, %d edges", len(rids), len(edges))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# CONTRASTIVE PAIR SAMPLING (Stage 1)
# ─────────────────────────────────────────────────────────────────────────────

def sample_pairs(graph: Dict, n_pairs: int = 512):
    idx_of = graph["idx_of"]
    edges = graph["edges"]
    N = len(graph["rids"])

    pos = []
    for s, d, rel in edges:
        if s == d:
            continue
        if rel in ("SIMILAR_TO", "SHARES_TOPIC", "DERIVED_FROM",
                   "REFERENCES", "RELATED_TO", "CO_OCCURS", "ENTITY_LINK"):
            pos.append((idx_of[s], idx_of[d]))
    if not pos:
        return [], []
    random.shuffle(pos)
    pos = pos[:n_pairs]

    pos_set = set((a, b) for a, b in pos) | set((b, a) for a, b in pos)
    neg = []
    attempts = 0
    while len(neg) < len(pos) and attempts < len(pos) * 10:
        a = random.randrange(N); b = random.randrange(N)
        if a != b and (a, b) not in pos_set:
            neg.append((a, b))
        attempts += 1
    while len(neg) < len(pos):
        neg.append((random.randrange(N), random.randrange(N)))

    return pos, neg


# ─────────────────────────────────────────────────────────────────────────────
# WALK GENERATION  (Stage 3 — dynamics training data)
# ─────────────────────────────────────────────────────────────────────────────

def generate_walks(graph: Dict, model: WorldView,
                    n_walks: int = MAX_WALKS, walk_len: int = WALK_LEN):
    rids = graph["rids"]
    idx_of = graph["idx_of"]
    N = len(rids)
    if N == 0 or not model.ready:
        return []

    by_src = defaultdict(list)
    for s, d, rel in graph["edges"]:
        if s == d:
            continue
        by_src[idx_of[s]].append((idx_of[d], rel))

    with torch.no_grad():
        x_t = graph.get("X_t")
        s_t = graph.get("s_t")
        d_t = graph.get("d_t")
        e_t = graph.get("e_t")
        if x_t is None:
            x_t = torch.tensor(graph["X"], device=model.device)
            s_t = torch.tensor(graph["src"], device=model.device)
            d_t = torch.tensor(graph["dst"], device=model.device)
            e_t = torch.tensor(graph["edge_type"], device=model.device)
        else:
            x_t = x_t.to(model.device)
            s_t = s_t.to(model.device)
            d_t = d_t.to(model.device)
            e_t = e_t.to(model.device)
        latents = model.gnn(x_t, s_t, d_t, e_t)
        concept_idx = model.codebook.assign(latents).cpu().numpy()

    model.record_concepts = {rids[i]: int(concept_idx[i]) for i in range(N)}
    # Also stash display metadata while we have the records in hand. This
    # makes concept inspection / labelling work even if training is
    # interrupted before the FAISS index gets rebuilt.
    model.record_meta = {}
    for i, rid in enumerate(rids):
        r = graph["records"].get(rid, {})
        c = int(concept_idx[i])
        model.record_meta[rid] = {
            "dataset_id":    r.get("dataset_id", ""),
            "text":          (r.get("text") or "")[:200],
            "created_at":    r.get("created_at", ""),
            "concept":       c,
            "concept_label": model.concept_labels.get(c, ""),
        }

    walks = []
    for _ in range(n_walks):
        start = random.randrange(N)
        seq = [int(concept_idx[start])]
        cur = start
        style = random.choice(["temporal", "random", "entity"])
        for _ in range(walk_len - 1):
            neighbors = by_src.get(cur, [])
            if not neighbors:
                break
            if style == "temporal":
                pool = [n for n in neighbors if n[1] == "TEMPORAL_NEXT"]
            elif style == "entity":
                pool = [n for n in neighbors if n[1] in ("ENTITY_LINK", "CO_OCCURS")]
            else:
                pool = neighbors
            if not pool:
                pool = neighbors
            nxt = random.choice(pool)[0]
            seq.append(int(concept_idx[nxt]))
            cur = nxt
        if len(seq) >= 2:
            walks.append([TOKEN_BOS] + [c + NUM_SPECIAL_TOKENS for c in seq])

    model.transition_counts.clear()
    for w in walks:
        body = [t - NUM_SPECIAL_TOKENS for t in w if t >= NUM_SPECIAL_TOKENS]
        for i in range(len(body) - 1):
            k = (body[i], body[i + 1])
            model.transition_counts[k] = model.transition_counts.get(k, 0) + 1

    log.info("WorldView walks: %d sequences", len(walks))
    return walks


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

async def _emit(stage, **kw):
    try:
        await emit_event({"type": "worldview.progress", "stage": stage, **kw})
    except Exception:
        pass


async def _wv_embed(text: str) -> Optional[List[float]]:
    """Embed text for WorldView — tries fabric._embed, falls back to _embed_direct
    using the orchestrator's configured model (not the env default which may differ)."""
    if not text or not text.strip():
        return None
    # Prefer the fabric's embedding — it uses the same model as training data
    fab = _get_fabric()
    if fab and hasattr(fab, "_embed"):
        try:
            emb = await fab._embed(text)
            if emb:
                return emb
        except Exception:
            pass
    # Fallback: direct Ollama — MUST use the same model the fabric uses
    try:
        import httpx
        # Get model from orchestrator (matches what was used to embed training data)
        model = getattr(_orch, "OLLAMA_EMBED_MODEL", None) or os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
        async with httpx.AsyncClient(timeout=30.0) as client:
            emb, reason, _ = await _embed_direct(text, model, client)
            if emb and len(emb) != MODEL.embed_dim:
                log.warning("_wv_embed: model %s produced %d-dim, need %d-dim. "
                            "Check OLLAMA_EMBED_MODEL matches Chroma fabric.",
                            model, len(emb), MODEL.embed_dim)
                return None  # don't return wrong-dim embedding
            return emb
    except Exception:
        return None


def _meta_for(rid: str) -> Dict:
    """Resolve display metadata for a record id.

    Falls back: FAISS index metadata → MODEL.record_meta → empty dict.
    This means concept inspection (members, snapshots, query results) keeps
    working even if FAISS is empty after a restart.
    """
    meta = WV_INDEX._meta.get(rid) if WV_INDEX.available else None
    if meta:
        return meta
    return MODEL.record_meta.get(rid, {})


def _dim_mismatch_error(emb) -> Optional[Dict]:
    """If the given embedding doesn't match MODEL.embed_dim, return an error dict.
    Otherwise return None.
    """
    if emb is None:
        return None
    try:
        n = len(emb)
    except Exception:
        return None
    if n != MODEL.embed_dim:
        return {
            "error": (f"Embedding dim mismatch: Ollama produced {n}-dim but "
                      f"WorldView was trained on {MODEL.embed_dim}-dim. "
                      "Your Chroma fabric and Ollama embed model are using "
                      "different embedding models."),
            "hint": ("Check OLLAMA_EMBED_MODEL — it must match the model that "
                     "populated Chroma. Common dims: nomic-embed-text=768, "
                     "all-minilm=384, mxbai-embed-large=1024."),
            "ollama_dim": n,
            "worldview_dim": MODEL.embed_dim,
        }
    return None


async def _rebuild_index_from_graph(graph: Dict) -> int:
    """Encode records via GNN, assign concepts, build FAISS index.

    Concept assignment and record_meta are populated even when FAISS is
    unavailable — the concepts browser, anomalies, and counterfactual
    tabs all need them regardless of the similarity index.
    """
    if not MODEL.ready:
        log.warning("_rebuild_index: MODEL not ready")
        return 0
    loop = asyncio.get_running_loop()
    latents = await loop.run_in_executor(
        None, MODEL.encode_subgraph,
        graph.get("X_t", graph.get("X")),
        graph.get("s_t", graph.get("src")),
        graph.get("d_t", graph.get("dst")),
        graph.get("e_t", graph.get("edge_type")),
    )
    if latents is None:
        log.warning("_rebuild_index: encode_subgraph returned None")
        return 0
    ids = graph["rids"]
    log.info("_rebuild_index: encoded %d records → latents shape %s", len(ids), latents.shape)

    # Assign each record to a concept via the codebook
    if MODEL.train_steps.get("codebook", 0) > 0:
        try:
            z_t = torch.tensor(latents, dtype=torch.float32, device=MODEL.device)
            concept_ids = MODEL.codebook.assign(z_t).cpu().tolist()
            for i, rid in enumerate(ids):
                MODEL.record_concepts[rid] = concept_ids[i]
            log.info("_rebuild_index: assigned %d records to concepts", len(ids))
        except Exception as e:
            log.warning("_rebuild_index: concept assignment failed: %s", e)

    # Build metadata (needed for concept browser, anomalies, etc.)
    metas = []
    MODEL.record_meta = {}
    for rid in ids:
        r = graph["records"][rid]
        c = MODEL.record_concepts.get(rid, -1)
        m = {
            "dataset_id":    r["dataset_id"],
            "text":          (r.get("text") or "")[:200],
            "created_at":    r.get("created_at", ""),
            "concept":       c,
            "concept_label": MODEL.concept_labels.get(c, ""),
        }
        metas.append(m)
        MODEL.record_meta[rid] = m

    # FAISS indexing — only if available
    indexed = 0
    if WV_INDEX.available or WV_INDEX.connect():
        WV_INDEX.connect()  # reset for fresh rebuild
        WV_INDEX.add_batch(ids, latents, metas)
        indexed = len(ids)
        log.info("_rebuild_index: indexed %d records into FAISS", indexed)
    else:
        log.info("_rebuild_index: FAISS unavailable — skipping similarity index "
                  "(concepts and metadata still populated for %d records)", len(ids))

    # Cache the latent vectors for anomaly detection and snapshot without
    # needing to re-encode every time
    MODEL._cached_latents = latents
    MODEL._cached_rids = ids
    MODEL._cached_graph = graph

    return indexed


async def _rebuild_index_from_fabric(dataset_id: str = "",
                                      limit: int = MAX_NODES) -> int:
    """Rebuild the FAISS index without re-training.

    Fetches the current set of records from Chroma, runs them through the
    existing GNN, and repopulates WV_INDEX + MODEL.record_meta.

    Used when:
      • Training was interrupted between codebook fitting and index building.
      • Process restarted and FAISS state was lost (it's in-memory only).
      • New records were ingested after training and need to be indexed.
    """
    if not MODEL.ready:
        return 0
    if MODEL.train_steps.get("gnn", 0) == 0:
        log.warning("worldview: cannot rebuild index — GNN was never trained")
        return 0
    graph = await build_graph(dataset_id, limit=limit)
    if not graph.get("rids"):
        return 0
    _x_ref = graph.get("X_t", graph.get("X"))
    actual_dim = _x_ref.shape[1]
    if actual_dim != MODEL.embed_dim:
        log.warning("worldview rebuild: dim mismatch %d vs %d — cannot rebuild "
                    "without retraining", actual_dim, MODEL.embed_dim)
        return 0
    return await _rebuild_index_from_graph(graph)


# ─────────────────────────────────────────────────────────────────────────────
# RE-EMBED  —  back-fill Chroma for records that exist in SQLite but were
# never embedded (because the embed stage was off or Chroma was down at
# ingest time). Also usable as a "cleanup" maintenance op.
# ─────────────────────────────────────────────────────────────────────────────

async def _chroma_collection_dim() -> Optional[int]:
    """Return the embedding dimension of the existing Chroma collection,
    sampled from one record. None if Chroma is empty or unreachable.
    """
    fab = _get_fabric()
    if not fab:
        return None
    chroma = getattr(fab, "FABRIC_CHROMA", None)
    if chroma is None or not getattr(chroma, "available", False):
        return None
    try:
        loop = asyncio.get_running_loop()
        def _probe():
            col = chroma._col
            if col is None:
                return None
            r = col.get(limit=1, include=["embeddings"])
            embs = r.get("embeddings")
            if embs is None:
                return None
            try:
                if len(embs) == 0:
                    return None
                first = embs[0]
                if first is None:
                    return None
                return len(first)
            except Exception:
                return None
        return await loop.run_in_executor(None, _probe)
    except Exception as e:
        log.debug("chroma dim probe: %s", e)
        return None


async def _chroma_existing_ids(dataset_id: str = "") -> set:
    """Return set of record IDs already present in Chroma."""
    fab = _get_fabric()
    if not fab:
        return set()
    chroma = getattr(fab, "FABRIC_CHROMA", None)
    if chroma is None or not getattr(chroma, "available", False):
        return set()
    try:
        loop = asyncio.get_running_loop()
        def _scan():
            col = chroma._col
            if col is None:
                return set()
            kwargs = {"include": []}
            if dataset_id:
                kwargs["where"] = {"dataset_id": {"$eq": dataset_id}}
            r = col.get(**kwargs)
            return set(r.get("ids") or [])
        return await loop.run_in_executor(None, _scan)
    except Exception as e:
        log.debug("chroma id scan: %s", e)
        return set()


async def _sqlite_records_for_dataset(dataset_id: str = "",
                                       limit: int = 50000,
                                       exclude_ids: Optional[set] = None
                                       ) -> List[Dict]:
    """Pull records from SQLite (id, dataset_id, text, created_at, tags).
    Optionally exclude a set of IDs (e.g. ones already in Chroma).
    """
    fab = _get_fabric()
    if not fab:
        return []
    try:
        loop = asyncio.get_running_loop()
        def _scan():
            conn = fab._sqlite_conn()
            if not conn:
                return []
            q = ("SELECT id, dataset_id, text, created_at, tags "
                 "FROM fabric_records")
            args = []
            if dataset_id:
                q += " WHERE dataset_id = ?"
                args.append(dataset_id)
            q += " LIMIT ?"
            args.append(limit)
            out = []
            for row in conn.execute(q, args).fetchall():
                if exclude_ids and row[0] in exclude_ids:
                    continue
                if not row[2]:  # no text → can't embed
                    continue
                tags = row[4]
                if isinstance(tags, str):
                    try:
                        tags = json.loads(tags)
                    except Exception:
                        tags = []
                out.append({
                    "id":         row[0],
                    "dataset_id": row[1],
                    "text":       row[2],
                    "created_at": row[3] or "",
                    "tags":       tags or [],
                })
            return out
        return await loop.run_in_executor(None, _scan)
    except Exception as e:
        log.warning("worldview sqlite scan: %s", e)
        return []


async def _embed_direct(text: str, ollama_model: str,
                         client: "httpx.AsyncClient",
                         instance_url: Optional[str] = None
                         ) -> Tuple[Optional[List[float]], str, str]:
    """Embed a single text via Ollama directly (no fabric circuit-breaker).

    Long texts are split into chunks and each chunk is embedded separately,
    then the vectors are mean-pooled so NO DATA IS LOST (no truncation). This
    handles models with limited context windows gracefully.

    Returns (vector_or_None, error_reason, instance_used).
    """
    import uuid as _uuid
    import time as _time

    if not text or not text.strip():
        return None, "no_text", ""

    req_id = str(_uuid.uuid4())[:12]
    t_start = _time.time()
    text_preview = (text or "")[:120].replace("\n", " ")

    # ── Chunk long texts ──────────────────────────────────────────────────
    # Most embedding models have a context window of 512–8192 tokens.
    # We chunk at ~1800 chars (~450 tokens) which is safe for any common model.
    # Chunks split on paragraph/sentence boundaries to keep semantic coherence.
    CHUNK_LIMIT = 1800

    def _chunk_text(t: str) -> List[str]:
        t = t.strip()
        if len(t) <= CHUNK_LIMIT:
            return [t]
        chunks = []
        while t:
            if len(t) <= CHUNK_LIMIT:
                chunks.append(t)
                break
            # Try to split on paragraph, then sentence, then space
            cut = CHUNK_LIMIT
            for sep in ["\n\n", "\n", ". ", "! ", "? ", "; ", ", ", " "]:
                idx = t.rfind(sep, 0, CHUNK_LIMIT)
                if idx > CHUNK_LIMIT // 3:   # don't cut too early
                    cut = idx + len(sep)
                    break
            chunks.append(t[:cut].strip())
            t = t[cut:].strip()
        return [c for c in chunks if c]

    chunks = _chunk_text(text)

    # Build the list of instances to try
    instances_to_try: List[Tuple[str, str]] = []  # [(id, url), ...]
    if instance_url:
        instances_to_try = [("explicit", instance_url)]
    else:
        try:
            online = [(iid, i) for iid, i in _orch.OLLAMA_INSTANCES.items()
                      if i.get("status") == "online"]
        except Exception:
            online = []
        if not online:
            return None, "no_online_instances", ""

        # Prefer instances that have the model loaded, then by load + priority
        with_model = [(iid, i) for iid, i in online
                      if ollama_model in (i.get("models") or [])]
        without_model = [(iid, i) for iid, i in online
                         if ollama_model not in (i.get("models") or [])]

        def _key(pair):
            _, inst = pair
            return (inst.get("in_use", 0), inst.get("priority", 99))

        with_model.sort(key=_key)
        without_model.sort(key=_key)
        # Try ones with the model first, then any other online instance as a
        # last resort (sometimes /api/tags is stale or model auto-pulls)
        instances_to_try = [(iid, i["url"]) for iid, i in with_model + without_model]

    # Emit start event
    first_iid = instances_to_try[0][0] if instances_to_try else ""
    first_url = instances_to_try[0][1] if instances_to_try else ""
    try:
        await emit_event({
            "type":         "ollama.request",
            "req_id":       req_id,
            "model":        ollama_model,
            "instance_id":  first_iid,
            "instance_url": first_url,
            "caller_file":  "worldview_jepa.py",
            "caller_func":  "_embed_direct",
            "caller_module": "worldview_jepa",
            "cap_name":     "worldview.reembed",
            "prompt_preview": f"[embed] {text_preview}",
            "json_mode":    False,
            "prefer_gpu":   False,
            "streaming":    False,
        })
    except Exception:
        pass

    last_err = "no_attempts"
    used_instance = ""
    for iid, url in instances_to_try:
        try:
            # Embed each chunk separately, then mean-pool the vectors so
            # long documents get a faithful representation — no truncation.
            chunk_vecs = []
            for chunk in chunks:
                r = await client.post(
                    f"{url}/api/embed",
                    json={"model": ollama_model, "input": chunk},
                    timeout=60.0,
                )
                if r.status_code != 200:
                    r = await client.post(
                        f"{url}/api/embeddings",
                        json={"model": ollama_model, "prompt": chunk},
                        timeout=60.0,
                    )
                if r.status_code != 200:
                    last_err = f"http_{r.status_code}"
                    chunk_vecs = []
                    break
                data = r.json()
                emb = data.get("embeddings")
                if emb and isinstance(emb, list):
                    vec = emb[0] if isinstance(emb[0], list) else emb
                else:
                    vec = data.get("embedding")
                if not vec or len(vec) < 10:
                    last_err = "no_vector"
                    chunk_vecs = []
                    break
                chunk_vecs.append(vec)

            if not chunk_vecs:
                continue  # try next instance

            # Mean-pool: average across all chunk embeddings
            dim = len(chunk_vecs[0])
            if len(chunk_vecs) == 1:
                final_vec = chunk_vecs[0]
            else:
                final_vec = [0.0] * dim
                for cv in chunk_vecs:
                    for d in range(dim):
                        final_vec[d] += cv[d]
                for d in range(dim):
                    final_vec[d] /= len(chunk_vecs)

            used_instance = iid
            # Emit success event
            elapsed = round(_time.time() - t_start, 2)
            try:
                await emit_event({
                    "type": "ollama.request_done", "req_id": req_id,
                    "model": ollama_model, "instance_id": iid,
                    "caller_file": "worldview_jepa.py",
                    "caller_func": "_embed_direct",
                    "elapsed_s": elapsed, "dimensions": len(final_vec),
                    "chunks": len(chunk_vecs),
                })
            except Exception:
                pass
            return list(final_vec), "", iid
        except asyncio.TimeoutError:
            last_err = "timeout"
            continue
        except Exception as exc:
            last_err = f"exception:{type(exc).__name__}"
            continue

    # Emit failure event
    elapsed = round(_time.time() - t_start, 2)
    try:
        await emit_event({
            "type": "ollama.request_error", "req_id": req_id,
            "model": ollama_model, "instance_id": first_iid,
            "caller_file": "worldview_jepa.py",
            "caller_func": "_embed_direct",
            "elapsed_s": elapsed, "error": last_err,
        })
    except Exception:
        pass
    return None, last_err, ""


async def _embed_and_upsert_batch(
    records:        List[Dict],
    gpu_batch:      int = 32,   # concurrent slots per GPU instance
    cpu_batch:      int = 8,    # concurrent slots per CPU instance
    max_concurrent: int = 0,    # legacy — ignored
) -> Tuple[int, int, Dict[str, int], Dict[str, int]]:
    """Embed records using the fabric's _embed path with per-instance semaphores.

    Uses data_fabric._embed() — the same function used during normal ingest —
    so WorldView embedding is fully integrated with the fabric's embedding
    system, respecting the same model config, normalisation, and logging.

    Differences from direct ollama_embed():
      • Per-instance semaphores (gpu_batch / cpu_batch) limit concurrency
        independently per node rather than globally.
      • Retry logic: each record gets max_retries attempts with exponential
        backoff (0.5s, 1s, 2s…). Transient failures (busy instance, timeout)
        are retried; the global circuit-breaker is NOT tripped for retryable
        errors so the rest of the batch continues.
      • When all instances appear offline, waits up to 30s for a health
        re-check rather than failing the whole batch immediately.
      • Tasks are created lazily via a queue so _pick() runs at actual
        execution time (not at task-creation time), giving accurate load
        distribution.

    Returns (embedded_count, upserted_count, error_breakdown, instances_used).
    """
    fab = _get_fabric()
    if not fab:
        return 0, 0, {"no_fabric_module": len(records)}, {}
    chroma     = getattr(fab, "FABRIC_CHROMA", None)
    DataRecord = getattr(fab, "DataRecord", None)
    if not (chroma and DataRecord and getattr(chroma, "available", False)):
        return 0, 0, {"chroma_unavailable": len(records)}, {}

    # Reset fabric circuit-breaker — previous failures should not block this run
    try:
        fab._embed_failed = False
    except Exception:
        pass

    ollama_model = _orch.OLLAMA_EMBED_MODEL
    if not ollama_model:
        return 0, 0, {"ollama_model_missing": len(records)}, {}

    # ── Wait for at least one online instance ─────────────────────────────────
    async def _wait_for_instances(timeout: float = 30.0) -> Dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                online = {iid: inst for iid, inst in _orch.OLLAMA_INSTANCES.items()
                          if inst.get("status") == "online"}
            except Exception:
                online = {}
            if online:
                return online
            # Trigger a health re-check and wait briefly
            log.info("worldview embed: no online instances — re-checking health…")
            try:
                await asyncio.gather(
                    *[_orch._ping_instance(iid, inst)
                      for iid, inst in _orch.OLLAMA_INSTANCES.items()],
                    return_exceptions=True,
                )
            except Exception:
                pass
            await asyncio.sleep(2.0)
        return {}

    online = await _wait_for_instances(30.0)
    if not online:
        return 0, 0, {"no_online_instances": len(records)}, {}

    # ── Per-instance semaphores ───────────────────────────────────────────────
    sem_slots: Dict[str, tuple] = {}
    for iid, inst in online.items():
        cap = gpu_batch if inst.get("has_gpu") else cpu_batch
        sem_slots[iid] = (asyncio.Semaphore(cap), cap)

    errors:         Dict[str, int] = defaultdict(int)
    instances_used: Dict[str, int] = defaultdict(int)
    in_flight:      Dict[str, int] = {iid: 0 for iid in online}

    def _pick_instance() -> Optional[str]:
        """Pick the online instance with the most free slots right now."""
        best_iid, best_free = None, -1
        for iid in list(online.keys()):
            # Re-check status at dispatch time — an instance may have gone offline
            if _orch.OLLAMA_INSTANCES.get(iid, {}).get("status") != "online":
                continue
            _, cap = sem_slots[iid]
            free = cap - in_flight.get(iid, 0)
            if free > best_free:
                best_free, best_iid = free, iid
        return best_iid

    async def _embed_with_retry(text: str) -> Optional[List[float]]:
        """Embed one text via fabric._embed, retrying indefinitely with capped backoff.

        Returns None only if the text itself is invalid (handled upstream).
        Pauses and retries on any transient failure — never drops the record.
        The global _embed_failed circuit-breaker is suppressed throughout so
        a single failure doesn't block subsequent records.
        """
        attempt = 0
        while True:
            try:
                fab._embed_failed = False
            except Exception:
                pass
            try:
                vec = await fab._embed(text)
                if vec is not None:
                    return vec
            except Exception as e:
                log.debug("worldview _embed attempt %d: %s", attempt + 1, e)

            try:
                fab._embed_failed = False
            except Exception:
                pass

            # Exponential backoff: 0.5s, 1s, 2s, 4s … capped at 30s
            wait = min(0.5 * (2 ** attempt), 30.0)
            attempt += 1
            log.debug("worldview embed retry %d in %.1fs", attempt, wait)
            await asyncio.sleep(wait)

    async def _embed_one(rec: Dict) -> Tuple[Dict, Optional[List[float]]]:
        text = rec.get("text", "")
        if not text or not text.strip():
            errors["empty_text"] += 1
            return rec, None

        # Only blocks when ALL instances are offline (status != "online").
        # Normal backpressure — instances online but slots full — is handled
        # by the semaphore below; this loop is never entered in that case.
        wait = 2.0
        while True:
            iid = _pick_instance()
            if iid is not None:
                break
            log.info("worldview embed: all instances offline — pausing %.0fs then re-pinging", wait)
            await asyncio.sleep(wait)
            try:
                fab._embed_failed = False
            except Exception:
                pass
            await asyncio.gather(
                *[_orch._ping_instance(iid2, inst)
                  for iid2, inst in _orch.OLLAMA_INSTANCES.items()],
                return_exceptions=True,
            )
            wait = min(wait * 2, 60.0)

        sem, _ = sem_slots[iid]
        async with sem:
            in_flight[iid] = in_flight.get(iid, 0) + 1
            try:
                vec = await _embed_with_retry(text)
                instances_used[iid] = instances_used.get(iid, 0) + 1
                return rec, list(vec)
            except Exception as e:
                errors["embed_exception"] += 1
                log.debug("_embed_one %s unhandled: %s", iid, e)
                return rec, None
            finally:
                in_flight[iid] = max(0, in_flight.get(iid, 1) - 1)
                try:
                    fab._embed_failed = False
                except Exception:
                    pass

    embedded_results = await asyncio.gather(*[_embed_one(rec) for rec in records])

    successful = [(r, e) for r, e in embedded_results if e is not None]
    if not successful:
        return 0, 0, dict(errors), dict(instances_used)

    # Upsert in a worker thread (Chroma client is sync)
    loop = asyncio.get_running_loop()
    def _upsert():
        n = 0
        upsert_errs = []
        for rec, emb in successful:
            try:
                dr = DataRecord(
                    id=rec["id"], dataset_id=rec["dataset_id"],
                    text=rec["text"], embedding=emb,
                    tags=rec.get("tags", []),
                    created_at=rec.get("created_at", ""),
                )
                if chroma.upsert(dr):
                    n += 1
                else:
                    upsert_errs.append("upsert_returned_false")
            except Exception as e:
                upsert_errs.append(f"upsert_exception:{type(e).__name__}")
        return n, upsert_errs
    upserted, upsert_errs = await loop.run_in_executor(None, _upsert)
    for u in upsert_errs:
        errors[u] += 1

    return len(successful), upserted, dict(errors), dict(instances_used)


@capability(
    "worldview.reembed_missing",
    http_method="POST", http_path="/worldview/reembed_missing",
    http_tags=["worldview", "fabric", "maintenance"],
    memory="off", streams=["worldview.progress"],
    description="Find records that exist in SQLite but not in Chroma, embed "
                "their text via Ollama, and upsert into Chroma. "
                "Input: dataset_id (str, optional — empty = all), "
                "limit (int, default 5000 — per call), "
                "gpu_batch_size (int, default 32 — concurrent slots for GPU instances), "
                "cpu_batch_size (int, default 8  — concurrent slots per CPU instance), "
                "dry_run (bool, default false), "
                "force (bool, default false — resets Chroma on dimension mismatch). "
                "Output: {ok, missing_found, embedded, upserted, datasets_processed}.",
)
async def cap_worldview_reembed_missing(
    dataset_id:     str  = "",
    limit:          int  = 5000,
    gpu_batch_size: int  = 32,
    cpu_batch_size: int  = 8,
    batch_size:     int  = 0,    # legacy alias — if set, overrides cpu_batch_size
    dry_run:        bool = False,
    force:          bool = False,
    trace_id=None,
) -> Dict:
    # Legacy single batch_size field maps to cpu_batch_size
    if batch_size > 0:
        cpu_batch_size = batch_size

    fab = _get_fabric()
    if not fab:
        return {"error": "data_fabric module not loaded"}
    chroma = getattr(fab, "FABRIC_CHROMA", None)
    if chroma is None or not getattr(chroma, "available", False):
        return {"error": "Chroma is not available — embeddings cannot be stored"}

    # Ollama cluster health probe. Uses direct httpx so we bypass the
    # fabric's circuit-breaker (which may be tripped from an earlier
    # transient failure). Tries the cluster the way ollama_generate does.
    ollama_model = _orch.OLLAMA_EMBED_MODEL
    if not ollama_model:
        return {"error": "OLLAMA_EMBED_MODEL not configured"}

    # Reset the global circuit-breaker so other fabric callers start working.
    try:
        fab._embed_failed = False
    except Exception:
        pass

    from Vera.Orchestration.capability_orchestration import ollama_embed as _ollama_embed

    # Build per-instance status snapshot for the diagnostic
    try:
        cluster_snapshot = {
            iid: {
                "url":    i.get("url"),
                "status": i.get("status"),
                "models_loaded": len(i.get("models") or []),
                "in_use": i.get("in_use", 0),
                "latency_ms": i.get("latency_ms"),
            }
            for iid, i in _orch.OLLAMA_INSTANCES.items()
        }
    except Exception:
        cluster_snapshot = {}

    online_any = [iid for iid, s in cluster_snapshot.items()
                   if s.get("status") == "online"]

    if not online_any:
        return {
            "error": "No online Ollama instances in the cluster.",
            "cluster": cluster_snapshot,
            "hint": "Check `ollama.instances` capability or the Workers panel "
                    "to see why every instance is offline.",
        }

    # Probe: use centralized ollama_embed which handles routing, fallback,
    # and doesn't require the model to appear in /api/tags (embed models
    # are loaded on-demand by Ollama, so they often aren't listed there).
    probe_dim = None
    probe_err = ""
    try:
        probe_vec = await _ollama_embed("dim probe", model=ollama_model, timeout=30)
        if probe_vec is not None:
            probe_dim = len(probe_vec)
        else:
            probe_err = "ollama_embed returned None"
    except Exception as e:
        probe_err = f"probe_exception:{type(e).__name__}:{str(e)[:100]}"

    if probe_dim is None:
        return {
            "error": ("Ollama embed probe failed across the cluster — cannot "
                      "proceed with re-embed."),
            "probe_error":  probe_err,
            "model":        ollama_model,
            "cluster":      cluster_snapshot,
            "hint": (f"Online instances: {online_any}. "
                     "Probe error: " + str(probe_err) + ". "
                     "Possible causes: model name typo (check OLLAMA_EMBED_MODEL), "
                     f"model not pulled (run: ollama pull {ollama_model}), "
                     "or all instances returned errors."),
        }

    await _emit("probe_ok",
                message=f"Ollama embed probe OK, dim={probe_dim}",
                dim=probe_dim)

    existing_dim = await _chroma_collection_dim()
    if existing_dim is not None and probe_dim != existing_dim:
        if not force:
            return {
                "error": (f"Cannot re-embed: Chroma collection has "
                          f"{existing_dim}-dim vectors but current Ollama embed "
                          f"model produces {probe_dim}-dim. These cannot coexist."),
                "chroma_dim":  existing_dim,
                "ollama_dim":  probe_dim,
                "hint": ("Pass force=true to reset the Chroma collection and "
                         "re-embed everything with the current model. Or revert "
                         "OLLAMA_EMBED_MODEL to whatever produced the existing vectors."),
            }
        # force=True: wipe and recreate Chroma collection
        await _emit("chroma_reset",
                    message=f"Dimension mismatch ({existing_dim} vs {probe_dim}) — "
                            "resetting Chroma collection (force=true)...",
                    old_dim=existing_dim, new_dim=probe_dim)
        reset_result = chroma.reset_collection()
        if not reset_result.get("ok"):
            return {"error": "Failed to reset Chroma collection",
                    "detail": reset_result}
        log.info("worldview reembed: Chroma reset (was %d-dim, now empty for %d-dim)",
                 existing_dim, probe_dim)

    await _emit("reembed_scan",
                message="Scanning for records missing from Chroma...",
                dataset_id=dataset_id)

    # Find missing IDs
    chroma_ids = await _chroma_existing_ids(dataset_id)
    sqlite_records = await _sqlite_records_for_dataset(
        dataset_id, limit=limit + len(chroma_ids), exclude_ids=chroma_ids,
    )
    missing = sqlite_records[:limit]

    await _emit("reembed_found", missing=len(missing),
                already_in_chroma=len(chroma_ids),
                message=f"Found {len(missing)} records to embed")

    if not missing:
        return {"ok": True, "missing_found": 0,
                "embedded": 0, "upserted": 0,
                "message": "No missing records to embed"}

    if dry_run:
        # Show breakdown by dataset
        by_ds = Counter(r["dataset_id"] for r in missing)
        return {
            "ok": True, "dry_run": True,
            "missing_found": len(missing),
            "breakdown": [{"dataset_id": k, "count": v}
                           for k, v in by_ds.most_common(20)],
        }

    # Embed and upsert in batches with progress
    # Outer batch = total records dispatched per iteration; sized so the whole
    # cluster stays saturated: gpu_batch + N_cpu * cpu_batch records in-flight.
    n_cpu = sum(1 for inst in _orch.OLLAMA_INSTANCES.values()
                if inst.get("status") == "online" and not inst.get("has_gpu"))
    n_gpu = sum(1 for inst in _orch.OLLAMA_INSTANCES.values()
                if inst.get("status") == "online" and inst.get("has_gpu"))
    BATCH = max(gpu_batch_size * max(n_gpu, 1) + cpu_batch_size * max(n_cpu, 1), 16)
    total_embedded = 0
    total_upserted = 0
    total_errors: Dict[str, int] = defaultdict(int)
    total_instances: Dict[str, int] = defaultdict(int)
    for i in range(0, len(missing), BATCH):
        chunk = missing[i:i + BATCH]
        emb_n, ups_n, batch_errors, batch_instances = await _embed_and_upsert_batch(
            chunk, gpu_batch=gpu_batch_size, cpu_batch=cpu_batch_size,
        )
        total_embedded += emb_n
        total_upserted += ups_n
        for k, v in (batch_errors or {}).items():
            total_errors[k] += v
        for k, v in (batch_instances or {}).items():
            total_instances[k] += v
        # Build a short error summary for the progress event
        err_summary = ""
        if total_errors:
            top = sorted(total_errors.items(), key=lambda x: -x[1])[:3]
            err_summary = " · errors: " + ", ".join(f"{k}={v}" for k, v in top)
        inst_summary = ""
        if total_instances:
            inst_summary = " · via: " + ", ".join(
                f"{k}={v}" for k, v in sorted(total_instances.items(), key=lambda x: -x[1]))
        await _emit("reembed_progress",
                    processed=i + len(chunk),
                    total=len(missing),
                    embedded=total_embedded,
                    upserted=total_upserted,
                    errors=dict(total_errors),
                    instances=dict(total_instances),
                    message=f"Embedded {total_embedded}/{i + len(chunk)}{inst_summary}{err_summary}")

    datasets_processed = sorted(set(r["dataset_id"] for r in missing[:total_upserted]))

    await _emit("reembed_done",
                missing=len(missing),
                embedded=total_embedded,
                upserted=total_upserted,
                errors=dict(total_errors),
                instances=dict(total_instances))

    result = {
        "ok":            True,
        "missing_found": len(missing),
        "embedded":      total_embedded,
        "upserted":      total_upserted,
        "errors":        dict(total_errors),
        "instances_used": dict(total_instances),
        "datasets_processed": datasets_processed,
    }
    if total_upserted == 0:
        # Pick the most common error and explain it
        if total_errors:
            top_err = max(total_errors.items(), key=lambda x: x[1])[0]
            hints = {
                "no_text":        "Records had no text content to embed.",
                "no_vector":      "Ollama returned a response but no vector — model may be wrong.",
                "timeout":        "Ollama timed out — check it's reachable and the embed model is loaded.",
                "http_404":       "Ollama returned 404 — the embed model isn't loaded. Run `ollama pull <model>` first.",
                "http_500":       "Ollama returned 500 — check Ollama server logs.",
                "http_503":       "Ollama returned 503 — server overloaded or model not ready.",
            }
            # Match prefix matches too (exception:TypeError etc.)
            hint = hints.get(top_err, "")
            if not hint:
                for k, v in hints.items():
                    if top_err.startswith(k):
                        hint = v
                        break
            if not hint:
                hint = f"Most common error: '{top_err}'. Check server logs."
            result["note"] = "No records embedded. " + hint
        else:
            result["note"] = "No records embedded (no errors reported either — investigate)."
    else:
        result["note"] = "Run worldview.train now to use the newly-embedded data."

    return result


@capability(
    "worldview.train",
    http_method="POST", http_path="/worldview/train",
    http_tags=["worldview", "jepa", "ml"],
    memory="off", streams=["worldview.progress"],
    description="Run full 3-stage WorldView training: GNN → codebook → dynamics. "
                "Input: dataset_id (str, optional), gnn_epochs (int, default 20), "
                "codebook_epochs (int, default 8), dynamics_epochs (int, default 15), "
                "limit (int, default 20000), "
                "embed_missing (bool, default false — back-fill Chroma from "
                "SQLite for records missing embeddings BEFORE training; slow "
                "but ensures all datasets are usable). "
                "Output: {ok, stages, nodes, edges, indexed, duration_s}.",
)
async def cap_worldview_train(
    dataset_id:       str = "",
    gnn_epochs:       int = 20,
    codebook_epochs:  int = 8,
    dynamics_epochs:  int = 15,
    limit:            int = 0,
    embed_missing:    bool = False,
    node_ids:         list = None,
    trace_id=None,
) -> Dict:
    if limit <= 0:
        limit = _WV_CONFIG.get("max_nodes", MAX_NODES)
    if not MODEL.ready:
        return {"error": "PyTorch not available", "hint": "pip install torch"}

    t0 = time.time()

    # Optional pre-step: back-fill missing embeddings from SQLite
    if embed_missing:
        await _emit("embed_missing",
                    message="Back-filling Chroma for records missing embeddings...")
        rb = await cap_worldview_reembed_missing(
            dataset_id=dataset_id, limit=limit,
        )
        if rb.get("error"):
            return {"error": "embed_missing pre-step failed: " + rb["error"],
                    "reembed_result": rb}
        await _emit("embed_missing_done",
                    embedded=rb.get("embedded", 0),
                    upserted=rb.get("upserted", 0),
                    message=f"Back-filled {rb.get('upserted', 0)} records")

    _scope_desc = (f"{len(node_ids)} graph nodes" if node_ids
                   else (dataset_id or "all datasets"))
    await _emit("building_graph",
                message=f"Fetching records and edges from {_scope_desc} (limit={limit})...")
    _loss_history_reset()

    # ── Resolve graph node IDs → Chroma FabricRecord IDs ─────────────────────
    # The vera-graph panel sends vera-graph node IDs (Dataset, Entity, Session,
    # FabricRecord etc.).  Only FabricRecord UUIDs are addressable in Chroma.
    # _resolve_graph_node_ids_to_records crawls Neo4j to collect the full set
    # of FabricRecord IDs reachable from the supplied node set.
    resolved_node_ids = node_ids
    if node_ids:
        await _emit("resolving_graph",
                    message=f"Resolving {len(node_ids)} graph nodes → FabricRecord IDs...")
        resolved_node_ids = await _resolve_graph_node_ids_to_records(
            node_ids, max_depth=3, limit=limit,
        )
        if not resolved_node_ids:
            # Fallback: if resolution yielded nothing, train on the full
            # dataset_id scope (or all data) rather than failing immediately.
            log.warning(
                "worldview: node_id resolution yielded 0 records — "
                "falling back to dataset_id=%r scope", dataset_id,
            )
            await _emit("resolve_fallback",
                        message=f"Could not resolve graph nodes to records — "
                                f"falling back to dataset scope: {dataset_id or 'all'}",
                        original_count=len(node_ids))
            resolved_node_ids = None   # will use dataset_id / all-data path
        else:
            await _emit("resolve_done",
                        message=f"Resolved {len(node_ids)} graph nodes → "
                                f"{len(resolved_node_ids)} FabricRecord IDs",
                        original=len(node_ids), resolved=len(resolved_node_ids))

    graph = await build_graph(dataset_id, limit=limit, node_ids=resolved_node_ids)
    if not graph.get("rids"):
        diag = await _diagnose_no_records(dataset_id)
        return {"error": "No records with embeddings found",
                "diagnostic": diag,
                "hint": diag.get("hint", "Ingest data into the fabric first")}

    import gc
    gc.collect()

    N = len(graph["rids"])
    E = len(graph["edges"])
    X_ref = graph.get("X_t", graph.get("X"))
    actual_dim = X_ref.shape[1]
    if actual_dim != MODEL.embed_dim:
        await _emit("dim_mismatch",
                    message=f"Embedding dim is {actual_dim}, model was {MODEL.embed_dim} — rebuilding GNN",
                    actual=actual_dim, expected=MODEL.embed_dim)
        log.warning("WorldView training: actual embedding dim %d != model's %d, reinitialising",
                    actual_dim, MODEL.embed_dim)
        MODEL.reinitialize_for_embed_dim(actual_dim)
    await _emit("graph_ready", nodes=N, edges=E,
                message=f"Graph built: {N} nodes, {E} edges, embed_dim={actual_dim}",
                elapsed_s=round(time.time() - t0, 1))

    # Emit training plan so the UI knows what's coming
    await _emit("train_plan",
                message=f"Training plan: GNN {gnn_epochs}ep → Codebook {codebook_epochs}ep "
                        f"(K={MODEL.num_concepts}, entropy_w={_WV_CONFIG.get('entropy_weight', 5.0)}, "
                        f"temp={_WV_CONFIG.get('vq_temp_start', 2.0)}→{_WV_CONFIG.get('vq_temp_end', 0.0)}) "
                        f"→ Dynamics {dynamics_epochs}ep",
                gnn_epochs=gnn_epochs, codebook_epochs=codebook_epochs,
                dynamics_epochs=dynamics_epochs, nodes=N, edges=E)

    # Resolve tensor references (pre-built in build_graph, or fallback to numpy)
    g_x  = graph.get("X_t",  graph.get("X"))
    g_s  = graph.get("s_t",  graph.get("src"))
    g_d  = graph.get("d_t",  graph.get("dst"))
    g_et = graph.get("e_t",  graph.get("edge_type"))

    import gc

    # Stage 1 — GNN contrastive training
    if gnn_epochs > 0:
        await _emit("stage_gnn", message=f"Training graph encoder ({gnn_epochs} epochs, {N} nodes, {E} edges)...",
                     elapsed_s=round(time.time() - t0, 1))
        gc.collect()
        t_stage = time.time()
        margin = _WV_CONFIG.get("contrastive_margin", 0.2)
        uniformity_w = _WV_CONFIG.get("uniformity_weight", 0.1)
        loop = asyncio.get_running_loop()
        for epoch in range(gnn_epochs):
            try:
                pos, neg = sample_pairs(graph, n_pairs=min(1024, len(graph["edges"])))
                if not pos:
                    await _emit("gnn_epoch", epoch=epoch + 1, total=gnn_epochs,
                                 loss=0, message="No positive pairs — skipping")
                    break
                # Run CPU-bound training step in executor to avoid blocking the event loop
                loss = await loop.run_in_executor(
                    None, MODEL.train_gnn_step, g_x, g_s, g_d, g_et, pos, neg)
                ep_rate = (epoch + 1) / max(time.time() - t_stage, 0.01)
                # Emit every epoch for UI progress
                await _emit("gnn_epoch", epoch=epoch + 1, total=gnn_epochs,
                             loss=round(loss, 6),
                             elapsed_s=round(time.time() - t0, 1),
                             epoch_rate=round(ep_rate, 2),
                             message=f"GNN epoch {epoch+1}/{gnn_epochs} loss={loss:.4f} ({ep_rate:.1f} ep/s)")
                _loss_history_append("gnn", epoch + 1, loss)
                # Yield to event loop so SSE events actually flush
                await asyncio.sleep(0)
            except Exception as e:
                log.error("GNN epoch %d FAILED: %s", epoch + 1, e, exc_info=True)
                await _emit("gnn_error", epoch=epoch + 1, error=str(e)[:200],
                             message=f"GNN epoch {epoch+1} failed: {e}")
                break
        gc.collect()

    # Stage 2 — Codebook fitting
    if codebook_epochs > 0:
        await _emit("stage_codebook", message=f"Fitting concept codebook ({codebook_epochs} epochs)...",
                     elapsed_s=round(time.time() - t0, 1))
        gc.collect()
        t_stage = time.time()
        loop = asyncio.get_running_loop()
        for epoch in range(codebook_epochs):
            try:
                loss = await loop.run_in_executor(
                    None, MODEL.train_codebook_step, g_x, g_s, g_d, g_et)
                cb_stats = MODEL.codebook.usage_stats()
                ep_rate = (epoch + 1) / max(time.time() - t_stage, 0.01)
                await _emit("codebook_epoch", epoch=epoch + 1, total=codebook_epochs,
                             loss=round(loss, 6),
                             active=cb_stats["active_concepts"],
                             entropy=cb_stats["entropy"],
                             perplexity=cb_stats.get("perplexity", 0),
                             revived=getattr(MODEL, "_last_revived", 0),
                             elapsed_s=round(time.time() - t0, 1),
                             epoch_rate=round(ep_rate, 2),
                             message=f"Codebook epoch {epoch+1}/{codebook_epochs} "
                                     f"loss={loss:.4f} active={cb_stats['active_concepts']} "
                                     f"perplexity={cb_stats.get('perplexity',0):.0f} ({ep_rate:.1f} ep/s)")
                _loss_history_append("codebook", epoch + 1, loss,
                                     active=cb_stats["active_concepts"],
                                     perplexity=round(cb_stats.get("perplexity", 0), 1),
                                     entropy=round(float(cb_stats["entropy"]), 4))
                await asyncio.sleep(0)
            except Exception as e:
                log.error("Codebook epoch %d FAILED: %s", epoch + 1, e, exc_info=True)
                await _emit("codebook_error", epoch=epoch + 1, error=str(e)[:200],
                             message=f"Codebook epoch {epoch+1} failed: {e}")
                break
        gc.collect()

    # Stage 3 — Dynamics (walks + transformer)
    if dynamics_epochs > 0:
        await _emit("stage_dynamics", message="Generating concept walks...",
                     elapsed_s=round(time.time() - t0, 1))
        gc.collect()
        loop = asyncio.get_running_loop()
        try:
            walks = await loop.run_in_executor(
                None, generate_walks, graph, MODEL,
                min(_WV_CONFIG.get("max_walks", MAX_WALKS), N * 4),
                _WV_CONFIG.get("walk_len", WALK_LEN))
        except Exception as e:
            log.error("Walk generation FAILED: %s", e, exc_info=True)
            await _emit("dynamics_error", error=str(e)[:200],
                         message=f"Walk generation failed: {e}")
            walks = []
        if walks:
            await _emit("dynamics_walks", walks=len(walks),
                         elapsed_s=round(time.time() - t0, 1),
                         message=f"{len(walks)} walks generated — training dynamics...")
            t_stage = time.time()
            dyn_batch = _WV_CONFIG.get("batch_size", BATCH_SIZE)

            def _run_dynamics_epoch(walk_data, batch_sz):
                """Run one full dynamics epoch in a thread — CPU-bound."""
                random.shuffle(walk_data)
                losses = []
                for i in range(0, len(walk_data), batch_sz):
                    batch = walk_data[i:i + batch_sz]
                    if len(batch) < 2:
                        continue
                    l = MODEL.train_dynamics_step(batch)
                    losses.append(l)
                return sum(losses) / len(losses) if losses else 0.0

            for epoch in range(dynamics_epochs):
                try:
                    avg_loss = await loop.run_in_executor(
                        None, _run_dynamics_epoch, walks, dyn_batch)
                    ep_rate = (epoch + 1) / max(time.time() - t_stage, 0.01)
                    await _emit("dynamics_epoch", epoch=epoch + 1,
                                 total=dynamics_epochs,
                                 loss=round(avg_loss, 6),
                                 elapsed_s=round(time.time() - t0, 1),
                                 epoch_rate=round(ep_rate, 2),
                                 batches=len(walks) // dyn_batch,
                                 message=f"Dynamics epoch {epoch+1}/{dynamics_epochs} loss={avg_loss:.4f} ({ep_rate:.1f} ep/s)")
                    _loss_history_append("dynamics", epoch + 1, avg_loss)
                    await asyncio.sleep(0)
                except Exception as e:
                    log.error("Dynamics epoch %d FAILED: %s", epoch + 1, e, exc_info=True)
                    await _emit("dynamics_error", epoch=epoch + 1, error=str(e)[:200],
                                 message=f"Dynamics epoch {epoch+1} failed: {e}")
                    break
        gc.collect()

    await _emit("indexing", message="Rebuilding FAISS index...")
    log.info("worldview: starting index rebuild (graph has %d records)", N)
    try:
        indexed = await _rebuild_index_from_graph(graph)
    except Exception as e:
        log.error("worldview: index rebuild FAILED: %s", e, exc_info=True)
        await _emit("indexing_error", error=str(e)[:200],
                     message=f"Index rebuild failed: {e}")
        indexed = 0
    if indexed == 0:
        diag_detail = (f"WV_INDEX.available={WV_INDEX.available}, "
                       f"HAS_FAISS={HAS_FAISS}, HAS_NUMPY={HAS_NUMPY}, "
                       f"MODEL.ready={MODEL.ready}, graph_rids={len(graph.get('rids', []))}")
        log.warning("worldview: 0 records indexed — %s", diag_detail)
        await _emit("indexing_error",
                     message=f"0 records indexed — {diag_detail}",
                     faiss_available=HAS_FAISS, numpy_available=HAS_NUMPY,
                     index_available=WV_INDEX.available, model_ready=MODEL.ready,
                     graph_rids=len(graph.get("rids", [])))
        # Attempt a diagnostic encode to understand why
        try:
            test_latents = MODEL.encode_subgraph(
                graph.get("X_t", graph.get("X")),
                graph.get("s_t", graph.get("src")),
                graph.get("d_t", graph.get("dst")),
                graph.get("e_t", graph.get("edge_type")),
            )
            encode_result = ("None" if test_latents is None else
                             f"shape={test_latents.shape}")
            log.warning("worldview: diagnostic encode_subgraph returned %s",
                         encode_result)
            await _emit("indexing_error",
                         message=f"Diagnostic encode returned: {encode_result}")
        except Exception as de:
            log.warning("worldview: diagnostic encode_subgraph raised: %s", de)
            await _emit("indexing_error",
                         message=f"Diagnostic encode raised: {de}")

    # Also build transition counts for dynamics if codebook was trained
    if indexed > 0 and MODEL.train_steps.get("codebook", 0) > 0:
        try:
            concepts = [MODEL.record_concepts.get(rid, -1) for rid in graph["rids"]]
            concepts = [c for c in concepts if c >= 0]
            for i in range(len(concepts) - 1):
                k = (concepts[i], concepts[i + 1])
                MODEL.transition_counts[k] = MODEL.transition_counts.get(k, 0) + 1
            log.info("worldview: built %d transition pairs from %d assigned concepts",
                      len(MODEL.transition_counts), len(concepts))
        except Exception as e:
            log.warning("worldview: transition count build failed: %s", e)

    MODEL.save()
    await _persist_to_fabric()
    await _persist_loss_history()
    duration = round(time.time() - t0, 1)
    await _emit("done", message="Training complete",
                duration=duration, indexed=indexed,
                stages={k: MODEL.train_steps[k] for k in MODEL.train_steps})

    # Auto-label concepts after training (if enabled and LLM available)
    auto_labelled = 0
    if _WV_CONFIG.get("auto_label", True) and indexed > 0:
        try:
            await _emit("auto_label", message="Auto-labelling top concepts...")
            label_k = _WV_CONFIG.get("auto_label_k", 50)
            label_res = await cap_worldview_label_concepts(max_concepts=label_k)
            auto_labelled = len(label_res.get("labelled", []))
            if auto_labelled > 0:
                await _emit("auto_label_done",
                             message=f"Auto-labelled {auto_labelled} concepts",
                             labelled=auto_labelled)
                MODEL.save()
                await _persist_to_fabric()
        except Exception as e:
            log.debug("worldview: auto-label after training failed: %s", e)

    # Auto-start streaming if not already running
    if not _STREAM_ENABLED and indexed > 0:
        try:
            await cap_worldview_stream_start()
            await _emit("stream_autostart", message="Streaming auto-started after training")
        except Exception as e:
            log.debug("worldview: auto-start streaming failed: %s", e)

    return {
        "ok": True,
        "stages": {
            "gnn":       {"steps": MODEL.train_steps["gnn"],
                          "loss":  round(MODEL.train_loss["gnn"], 6)},
            "codebook":  {"steps": MODEL.train_steps["codebook"],
                          "loss":  round(MODEL.train_loss["codebook"], 6),
                          **MODEL.codebook.usage_stats()},
            "dynamics":  {"steps": MODEL.train_steps["dynamics"],
                          "loss":  round(MODEL.train_loss["dynamics"], 6)},
        },
        "nodes":     N,
        "edges":     E,
        "indexed":   indexed,
        "duration_s": duration,
    }


@capability(
    "worldview.train_stage",
    http_method="POST", http_path="/worldview/train_stage",
    http_tags=["worldview"], memory="off", streams=["worldview.progress"],
    description="Train one specific stage. Input: stage (gnn|codebook|dynamics), "
                "epochs (int), dataset_id (str).",
)
async def cap_worldview_train_stage(
    stage:      str = "gnn",
    epochs:     int = 5,
    dataset_id: str = "",
    trace_id=None,
) -> Dict:
    if stage not in ("gnn", "codebook", "dynamics"):
        return {"error": "stage must be one of: gnn, codebook, dynamics"}
    args = {"dataset_id": dataset_id, "gnn_epochs": 0,
            "codebook_epochs": 0, "dynamics_epochs": 0}
    args[stage + "_epochs"] = epochs
    return await cap_worldview_train(**args)


@capability(
    "worldview.rebuild_index",
    http_method="POST", http_path="/worldview/rebuild_index",
    http_tags=["worldview"], memory="off", streams=["worldview.progress"],
    description="Rebuild the FAISS latent index without retraining. "
                "Use when training was interrupted, after a restart (FAISS is "
                "in-memory only), or after new records have been ingested. "
                "Input: dataset_id (str, optional), limit (int). "
                "Output: {ok, indexed, duration_s}.",
)
async def cap_worldview_rebuild_index(
    dataset_id: str = "",
    limit:      int = MAX_NODES,
    trace_id=None,
) -> Dict:
    if not MODEL.ready:
        return {"error": "WorldView not ready"}
    if MODEL.train_steps.get("gnn", 0) == 0:
        return {"error": "GNN has not been trained — run worldview.train first"}
    t0 = time.time()
    await _emit("rebuilding_index",
                message="Re-encoding all records into the latent index...")
    indexed = await _rebuild_index_from_fabric(dataset_id, limit=limit)
    duration = round(time.time() - t0, 1)
    if indexed == 0:
        diag = await _diagnose_no_records(dataset_id)
        await _emit("rebuild_done", indexed=0, duration=duration,
                    message="No records to index")
        return {"error": "Rebuilt index is empty",
                "diagnostic": diag,
                "duration_s": duration}
    # Save model so updated record_meta survives a restart
    MODEL.save()
    await _persist_to_fabric()
    await _emit("rebuild_done", indexed=indexed, duration=duration,
                message=f"Indexed {indexed} records")
    return {"ok": True, "indexed": indexed, "duration_s": duration}


@capability(
    "worldview.encode",
    http_method="POST", http_path="/worldview/encode",
    http_tags=["worldview"], memory="off",
    description="Encode text or record into latent + concept. "
                "Input: text (str) OR record_id (str) OR embedding (list).",
)
async def cap_worldview_encode(
    text:      str = "",
    record_id: str = "",
    embedding: List[float] = None,
    trace_id=None,
) -> Dict:
    if not MODEL.ready:
        return {"error": "WorldView not ready"}

    if record_id and record_id in MODEL.record_concepts:
        c = MODEL.record_concepts[record_id]
        return {"record_id": record_id, "concept": c,
                "concept_label": MODEL.concept_labels.get(c, "")}

    emb = embedding
    if not emb and text:
        emb = await _wv_embed(text)
    if not emb and record_id:
        fab = _get_fabric()
        if fab and hasattr(fab, "FABRIC_PG") and fab.FABRIC_PG.available:
            rmap = await fab.FABRIC_PG.get_by_ids([record_id])
            rec = (rmap or {}).get(record_id)
            if rec and rec.embedding:
                emb = rec.embedding
    if not emb:
        return {"error": "Provide text, record_id, or embedding"}

    dim_err = _dim_mismatch_error(emb)
    if dim_err:
        return dim_err

    latent = MODEL.encode_isolated(emb)
    if latent is None:
        return {"error": "Encoding failed (unknown reason — check server logs)"}
    c = MODEL.assign_concept(latent)
    return {
        "latent":        latent.tolist(),
        "concept":       c,
        "concept_label": MODEL.concept_labels.get(c, ""),
        "latent_dim":    MODEL.latent_dim,
    }


@capability(
    "worldview.predict",
    http_method="POST", http_path="/worldview/predict",
    http_tags=["worldview"], memory="off",
    description="Predict the next concept after a record/concept/text. "
                "Output: {next_concepts: [{concept, prob, label}]}.",
)
async def cap_worldview_predict(
    record_id: str = "",
    concept:   int = -1,
    text:      str = "",
    top_k:     int = 8,
    trace_id=None,
) -> Dict:
    if not MODEL.ready:
        return {"error": "WorldView not ready"}

    start_c = concept
    if start_c < 0 and record_id:
        start_c = MODEL.record_concepts.get(record_id, -1)
    if start_c < 0 and text:
        emb = await _wv_embed(text)
        if emb:
            dim_err = _dim_mismatch_error(emb)
            if dim_err:
                return dim_err
            latent = MODEL.encode_isolated(emb)
            if latent is not None:
                start_c = MODEL.assign_concept(latent)
    if start_c < 0:
        return {"error": "No starting concept resolved — provide text, a valid record_id, or a concept index"}

    with torch.no_grad():
        prefix = [TOKEN_BOS, start_c + NUM_SPECIAL_TOKENS]
        t = torch.tensor([prefix], dtype=torch.long, device=MODEL.device)
        logits = MODEL.dynamics(t)[0, -1]
        logits[TOKEN_BOS] = -1e9
        logits[TOKEN_EOS] = -1e9
        probs = F.softmax(logits, dim=-1)
        top_p, top_i = torch.topk(probs, min(top_k, probs.numel()))
        next_concepts = []
        for p, i in zip(top_p.tolist(), top_i.tolist()):
            if i < NUM_SPECIAL_TOKENS:
                continue
            c = i - NUM_SPECIAL_TOKENS
            next_concepts.append({
                "concept": c, "prob": round(p, 6),
                "label":   MODEL.concept_labels.get(c, ""),
            })

    return {"start_concept": start_c,
            "start_concept_label": MODEL.concept_labels.get(start_c, ""),
            "next_concepts": next_concepts}


@capability(
    "worldview.rollout",
    http_method="POST", http_path="/worldview/rollout",
    http_tags=["worldview"], memory="off",
    description="Multi-step trajectory through concept space. "
                "Input: record_id|concept|text, steps (default 8), "
                "temperature (default 0.8), top_k (default 20). "
                "Output: {trajectory: [{step, concept, label, members}]}.",
)
async def cap_worldview_rollout(
    record_id:    str   = "",
    concept:      int   = -1,
    text:         str   = "",
    steps:        int   = 8,
    temperature:  float = 0.8,
    top_k:        int   = 20,
    trace_id=None,
) -> Dict:
    if not MODEL.ready:
        return {"error": "WorldView not ready"}

    start_c = concept
    if start_c < 0 and record_id:
        start_c = MODEL.record_concepts.get(record_id, -1)
    if start_c < 0 and text:
        emb = await _wv_embed(text)
        if emb:
            dim_err = _dim_mismatch_error(emb)
            if dim_err:
                return dim_err
            latent = MODEL.encode_isolated(emb)
            if latent is not None:
                start_c = MODEL.assign_concept(latent)
    if start_c < 0:
        return {"error": "No starting concept resolved — provide text, a valid record_id, or a concept index"}

    trajectory = MODEL.rollout(start_c, steps=steps,
                                temperature=temperature, top_k=top_k)
    members_by_concept = defaultdict(list)
    for rid, c in MODEL.record_concepts.items():
        if len(members_by_concept[c]) < 3:
            members_by_concept[c].append(rid)

    out = []
    for i, c in enumerate(trajectory):
        out.append({
            "step":    i,
            "concept": c,
            "label":   MODEL.concept_labels.get(c, ""),
            "members": members_by_concept.get(c, [])[:3],
        })
    return {
        "start_concept": start_c,
        "start_concept_label": MODEL.concept_labels.get(start_c, ""),
        "trajectory": out,
        "temperature": temperature,
    }


@capability(
    "worldview.counterfactual",
    http_method="POST", http_path="/worldview/counterfactual",
    http_tags=["worldview"], memory="off",
    description="Counterfactual: rollout with a concept swap mid-trajectory. "
                "Input: start_concept (int!), swap_at (int), swap_to (int!), steps. "
                "Output: {baseline, counterfactual, divergence_step}.",
)
async def cap_worldview_counterfactual(
    start_concept: int = -1,
    swap_at:       int = 1,
    swap_to:       int = -1,
    steps:         int = 8,
    temperature:   float = 0.6,
    trace_id=None,
) -> Dict:
    if not MODEL.ready:
        return {"error": "WorldView not ready"}
    if start_concept < 0 or swap_to < 0:
        return {"error": "start_concept and swap_to required"}

    baseline = MODEL.rollout(start_concept, steps=steps, temperature=temperature)
    counterfactual = MODEL.rollout(
        start_concept, steps=steps, temperature=temperature,
        pinned=[(swap_at, swap_to)],
    )

    members_by_concept = defaultdict(list)
    for rid, c in MODEL.record_concepts.items():
        if len(members_by_concept[c]) < 2:
            members_by_concept[c].append(rid)

    def _fmt(traj):
        return [{
            "step":    i, "concept": c,
            "label":   MODEL.concept_labels.get(c, ""),
            "members": members_by_concept.get(c, [])[:2],
        } for i, c in enumerate(traj)]

    divergence_step = len(baseline)
    for i, (a, b) in enumerate(zip(baseline, counterfactual)):
        if a != b:
            divergence_step = i
            break

    return {
        "start_concept": start_concept,
        "swap_at":       swap_at,
        "swap_to":       swap_to,
        "baseline":       _fmt(baseline),
        "counterfactual": _fmt(counterfactual),
        "divergence_step": divergence_step,
    }


@capability(
    "worldview.query",
    http_method="POST", http_path="/worldview/query",
    http_tags=["worldview"], memory="off",
    description="Nearest-neighbour query in latent space. "
                "Input: text (str), top_k (int).",
)
async def cap_worldview_query(
    text:  str = "",
    top_k: int = 10,
    dataset_id: str = "",
    trace_id=None,
) -> Dict:
    if not MODEL.ready or not WV_INDEX.available:
        return {"error": "WorldView not ready — train the model first, then rebuild the index"}
    if not text:
        return {"error": "Provide a text query"}

    # Embed using the same model as training data
    emb = await _wv_embed(text)
    if not emb:
        return {"error": "Embedding failed. Check that the embedding service is running and "
                         "OLLAMA_EMBED_MODEL matches the model used to populate Chroma."}

    dim_err = _dim_mismatch_error(emb)
    if dim_err:
        return dim_err
    latent = MODEL.encode_isolated(emb)
    if latent is None:
        return {"error": "Encoding failed (unknown reason — check server logs)"}
    results = WV_INDEX.search(latent, top_k=top_k)
    out = []
    for rid, score in results:
        meta = _meta_for(rid)
        if dataset_id and meta.get("dataset_id", "") != dataset_id:
            continue
        out.append({
            "id":            rid,
            "score":         round(score, 5),
            "dataset_id":    meta.get("dataset_id", ""),
            "text":          meta.get("text", ""),
            "concept":       meta.get("concept", -1),
            "concept_label": meta.get("concept_label", ""),
        })
    c = MODEL.assign_concept(latent)
    return {
        "results": out, "query": text[:200],
        "query_concept": c,
        "query_concept_label": MODEL.concept_labels.get(c, ""),
    }


@capability(
    "worldview.anomalies",
    http_method="POST", http_path="/worldview/anomalies",
    http_tags=["worldview"], memory="off",
    description="Anomaly score = (a) distance from assigned concept + "
                "(b) low likelihood under dynamics model. "
                "Input: dataset_id (str), top_k (int).",
)
async def cap_worldview_anomalies(
    dataset_id: str = "",
    top_k:      int = 20,
    trace_id=None,
) -> Dict:
    if not MODEL.ready:
        return {"error": "WorldView not ready"}

    # Use cached graph/latents from last training if available
    graph = getattr(MODEL, "_cached_graph", None)
    latents = getattr(MODEL, "_cached_latents", None)
    rids = getattr(MODEL, "_cached_rids", None)

    if graph is None or latents is None:
        # Fall back to building from scratch
        graph = await build_graph(dataset_id, limit=_WV_CONFIG.get("max_nodes", MAX_NODES))
        if not graph.get("rids"):
            diag = await _diagnose_no_records(dataset_id)
            return {"error": "No records with embeddings found",
                    "diagnostic": diag}
        actual_dim = graph.get("X_t", graph.get("X")).shape[1]
        if actual_dim != MODEL.embed_dim:
            return {"error": f"Embedding dim mismatch: data has {actual_dim} but "
                             f"model was trained on {MODEL.embed_dim}.",
                    "actual_dim": actual_dim, "model_dim": MODEL.embed_dim}
        loop = asyncio.get_running_loop()
        latents = await loop.run_in_executor(
            None, MODEL.encode_subgraph,
            graph.get("X_t", graph.get("X")),
            graph.get("s_t", graph.get("src")),
            graph.get("d_t", graph.get("dst")),
            graph.get("e_t", graph.get("edge_type")),
        )
        if latents is None:
            return {"error": "Encoding failed"}
        rids = graph["rids"]

    # Filter by dataset if specified
    if dataset_id:
        indices = [i for i, rid in enumerate(rids)
                   if graph["records"].get(rid, {}).get("dataset_id") == dataset_id]
        if not indices:
            return {"error": f"No records for dataset '{dataset_id}' in cached graph"}
    else:
        indices = list(range(len(rids)))

    with torch.no_grad():
        z = torch.tensor(latents[indices], device=MODEL.device)
        concepts = MODEL.codebook.assign(z)
        cb = MODEL.codebook.codebook[concepts]
        recon = (z - cb).pow(2).sum(-1).cpu().numpy()

    # Build temporal chains per dataset for dynamics scoring
    by_ds = defaultdict(list)
    for ii, idx in enumerate(indices):
        rid = rids[idx]
        r = graph["records"].get(rid, {})
        by_ds[r.get("dataset_id", "")].append(
            (r.get("created_at", ""), rid, int(concepts[ii].item()), ii))

    record_logp = {}
    for ds, items in by_ds.items():
        items.sort()
        for i in range(1, len(items)):
            prev_c = items[i - 1][2]
            this_c = items[i][2]
            lp = MODEL.sequence_log_prob([prev_c, this_c])
            record_logp[items[i][1]] = lp

    recon_max = max(float(recon.max()), 1e-6)
    anomalies = []
    for ii, idx in enumerate(indices):
        rid = rids[idx]
        r = graph["records"].get(rid, {})
        recon_score = float(recon[ii]) / recon_max
        lp = record_logp.get(rid, 0.0)
        lp_score = max(0.0, -lp) / 8.0
        combined = 0.6 * recon_score + 0.4 * lp_score
        anomalies.append({
            "id":             rid,
            "dataset_id":     r.get("dataset_id", ""),
            "text":           r.get("text", "")[:200],
            "concept":        int(concepts[ii].item()),
            "concept_label":  MODEL.concept_labels.get(int(concepts[ii].item()), ""),
            "recon_distance": round(recon_score, 5),
            "log_prob":       round(lp, 4) if rid in record_logp else None,
            "anomaly_score":  round(combined, 5),
        })

    anomalies.sort(key=lambda a: -a["anomaly_score"])
    return {"anomalies": anomalies[:top_k], "total": len(anomalies)}


@capability(
    "worldview.snapshot",
    http_method="GET", http_path="/worldview/snapshot",
    http_tags=["worldview"], memory="off",
    description="2D projection for visualisation. "
                "Output: {points, concepts, method, count}.",
)
async def cap_worldview_snapshot(
    method: str = "pca",
    limit:  int = 500,
    trace_id=None,
) -> Dict:
    if not HAS_NUMPY:
        return {"error": "NumPy not available"}

    ids, vecs = None, None

    # Try FAISS index first
    if WV_INDEX.available:
        ids, vecs = WV_INDEX.get_all_vectors()

    # Fall back to cached latents from last training
    if (ids is None or not len(ids)) and hasattr(MODEL, "_cached_latents"):
        latents = MODEL._cached_latents
        cached_rids = MODEL._cached_rids
        if latents is not None and cached_rids:
            ids = list(cached_rids)
            vecs = np.asarray(latents, dtype="float32")

    if not ids or vecs is None or len(ids) == 0:
        return {"error": "No vectors — train first"}

    if len(ids) > limit:
        sel = np.random.choice(len(ids), limit, replace=False)
        ids = [ids[i] for i in sel]
        vecs = vecs[sel]

    proj = None
    Vt = None
    mean_vec = vecs.mean(axis=0)

    if method == "umap":
        try:
            import umap
            reducer = umap.UMAP(n_components=2,
                                 n_neighbors=min(15, len(ids) - 1),
                                 metric="cosine", random_state=42)
            proj = reducer.fit_transform(vecs)
        except ImportError:
            method = "pca"

    if proj is None or method == "pca":
        vc = vecs - mean_vec
        U, S, Vt = np.linalg.svd(vc, full_matrices=False)
        proj = U[:, :2] * S[:2]
        method = "pca"

    mins = proj.min(axis=0); maxs = proj.max(axis=0)
    rng = (maxs - mins); rng[rng == 0] = 1.0
    proj_n = (proj - mins) / rng

    points = []
    for i, rid in enumerate(ids):
        meta = _meta_for(rid)
        points.append({
            "id":            rid,
            "x":             round(float(proj_n[i, 0]), 5),
            "y":             round(float(proj_n[i, 1]), 5),
            "dataset_id":    meta.get("dataset_id", ""),
            "text":          meta.get("text", "")[:80],
            "concept":       meta.get("concept", -1),
            "concept_label": meta.get("concept_label", ""),
        })

    concept_points = []
    if MODEL.ready:
        from collections import Counter as _Counter, defaultdict as _dd
        pop_from_records = _Counter(MODEL.record_concepts.values())

        # Compute concept centroids from the actual projected positions of their members.
        # This guarantees concepts sit at the center of their cluster on the map,
        # regardless of how the codebook vectors drifted during training.
        concept_sums = _dd(lambda: np.zeros(2, dtype="float64"))
        concept_counts = _dd(int)
        for i, rid in enumerate(ids):
            c = MODEL.record_concepts.get(rid, -1)
            if c >= 0:
                concept_sums[c] += proj_n[i]
                concept_counts[c] += 1

        for c_idx in range(MODEL.num_concepts):
            pop = pop_from_records.get(c_idx, 0)
            if pop == 0:
                continue
            if concept_counts[c_idx] > 0:
                cx = float(concept_sums[c_idx][0] / concept_counts[c_idx])
                cy = float(concept_sums[c_idx][1] / concept_counts[c_idx])
            else:
                # Concept has assigned records but none in this sample — use codebook projection
                if method == "pca" and Vt is not None:
                    with torch.no_grad():
                        cb_vec = MODEL.codebook.codebook[c_idx].cpu().numpy()
                    cb_c = cb_vec - mean_vec
                    cb_proj_v = cb_c @ Vt[:2].T
                    cx = float((cb_proj_v[0] - mins[0]) / rng[0])
                    cy = float((cb_proj_v[1] - mins[1]) / rng[1])
                else:
                    continue
            concept_points.append({
                "idx":      c_idx,
                "x":        round(cx, 5),
                "y":        round(cy, 5),
                "size":     pop,
                "label":    MODEL.concept_labels.get(c_idx, ""),
            })

    return {
        "points":   points,
        "concepts": concept_points,
        "method":   method,
        "count":    len(points),
        "active_concepts": len(concept_points),
    }


@capability(
    "worldview.concepts",
    http_method="GET", http_path="/worldview/concepts",
    http_tags=["worldview"], memory="off",
    description="List concepts with populations and labels.",
)
async def cap_worldview_concepts(
    min_population: int = 1,
    trace_id=None,
) -> Dict:
    if not MODEL.ready:
        return {"error": "WorldView not ready"}

    pop = Counter(MODEL.record_concepts.values())
    member_samples = defaultdict(list)
    for rid, c in MODEL.record_concepts.items():
        if len(member_samples[c]) < 5:
            sample = _meta_for(rid)
            member_samples[c].append({
                "id":   rid,
                "text": sample.get("text", "")[:80],
                "dataset_id": sample.get("dataset_id", ""),
            })

    concepts = []
    for c in sorted(pop.keys(), key=lambda x: -pop[x]):
        if pop[c] < min_population:
            continue
        concepts.append({
            "idx":       c,
            "population": pop[c],
            "label":     MODEL.concept_labels.get(c, ""),
            "members_sample": member_samples.get(c, []),
        })

    return {"concepts": concepts, "total": len(concepts)}


@capability(
    "worldview.concept_neighbors",
    http_method="GET", http_path="/worldview/concept_neighbors",
    http_tags=["worldview"], memory="off",
    description="Transition probabilities from a concept (observed + predicted).",
)
async def cap_worldview_concept_neighbors(
    concept: int = -1,
    top_k:   int = 8,
    trace_id=None,
) -> Dict:
    if not MODEL.ready or concept < 0:
        return {"error": "concept required"}

    outgoing = [(b, n) for (a, b), n in MODEL.transition_counts.items()
                if a == concept]
    total = sum(n for _, n in outgoing)
    outgoing.sort(key=lambda x: -x[1])
    observed = []
    for b, n in outgoing[:top_k]:
        observed.append({
            "to":    b,
            "count": n,
            "prob":  round(n / max(total, 1), 5),
            "label": MODEL.concept_labels.get(b, ""),
        })

    predicted = []
    with torch.no_grad():
        prefix = torch.tensor([[TOKEN_BOS, concept + NUM_SPECIAL_TOKENS]],
                               dtype=torch.long, device=MODEL.device)
        logits = MODEL.dynamics(prefix)[0, -1]
        logits[TOKEN_BOS] = -1e9; logits[TOKEN_EOS] = -1e9
        probs = F.softmax(logits, dim=-1)
        top_p, top_i = torch.topk(probs, min(top_k, probs.numel()))
        for p, i in zip(top_p.tolist(), top_i.tolist()):
            if i < NUM_SPECIAL_TOKENS:
                continue
            c = i - NUM_SPECIAL_TOKENS
            predicted.append({"to": c, "prob": round(p, 5),
                              "label": MODEL.concept_labels.get(c, "")})

    return {
        "concept":  concept,
        "label":    MODEL.concept_labels.get(concept, ""),
        "observed": observed,
        "predicted": predicted,
    }


@capability(
    "worldview.concept_members",
    http_method="GET", http_path="/worldview/concept_members",
    http_tags=["worldview"], memory="off",
    description="List records assigned to a concept.",
)
async def cap_worldview_concept_members(
    concept: int = -1,
    limit:   int = 50,
    trace_id=None,
) -> Dict:
    if not MODEL.ready or concept < 0:
        return {"error": "concept required"}
    members = []
    for rid, c in MODEL.record_concepts.items():
        if c != concept:
            continue
        meta = _meta_for(rid)
        members.append({
            "id":         rid,
            "text":       meta.get("text", ""),
            "dataset_id": meta.get("dataset_id", ""),
        })
        if len(members) >= limit:
            break
    return {"concept": concept,
            "label":    MODEL.concept_labels.get(concept, ""),
            "members":  members, "total": len(members)}


@capability(
    "worldview.concept_detail",
    http_method="GET", http_path="/worldview/concept_detail",
    http_tags=["worldview"], memory="off",
    description="Full detail for a concept: label, members, transitions, predictions, stats.",
)
async def cap_worldview_concept_detail(
    concept: int = -1,
    member_limit: int = 20,
    trace_id=None,
) -> Dict:
    if not MODEL.ready or concept < 0:
        return {"error": "concept required"}
    # Population
    pop = sum(1 for c in MODEL.record_concepts.values() if c == concept)
    # Members
    members = []
    for rid, c in MODEL.record_concepts.items():
        if c != concept:
            continue
        meta = _meta_for(rid)
        members.append({"id": rid, "text": meta.get("text", "")[:120],
                        "dataset_id": meta.get("dataset_id", "")})
        if len(members) >= member_limit:
            break
    # Dataset breakdown
    ds_counts = Counter()
    for rid, c in MODEL.record_concepts.items():
        if c == concept:
            meta = _meta_for(rid)
            ds_counts[meta.get("dataset_id", "unknown")] += 1
    # Transitions
    observed = []
    total_out = 0
    for (a, b), n in MODEL.transition_counts.items():
        if a == concept:
            observed.append({"to": b, "count": n, "label": MODEL.concept_labels.get(b, "")})
            total_out += n
    observed.sort(key=lambda x: -x["count"])
    for o in observed:
        o["prob"] = round(o["count"] / max(total_out, 1), 4)
    # Predicted next
    predicted = []
    try:
        with torch.no_grad():
            prefix = torch.tensor([[TOKEN_BOS, concept + NUM_SPECIAL_TOKENS]],
                                   dtype=torch.long, device=MODEL.device)
            logits = MODEL.dynamics(prefix)[0, -1]
            logits[TOKEN_BOS] = -1e9; logits[TOKEN_EOS] = -1e9
            probs = F.softmax(logits, dim=-1)
            top_p, top_i = torch.topk(probs, min(8, probs.numel()))
            for p, i in zip(top_p.tolist(), top_i.tolist()):
                if i < NUM_SPECIAL_TOKENS:
                    continue
                c = i - NUM_SPECIAL_TOKENS
                predicted.append({"to": c, "prob": round(p, 5),
                                  "label": MODEL.concept_labels.get(c, "")})
    except Exception:
        pass
    return {
        "concept":     concept,
        "label":       MODEL.concept_labels.get(concept, ""),
        "population":  pop,
        "members":     members,
        "datasets":    [{"id": k, "count": v} for k, v in ds_counts.most_common(20)],
        "observed_transitions": observed[:15],
        "predicted_next":       predicted,
    }


@capability(
    "worldview.explain_record",
    http_method="GET", http_path="/worldview/explain_record",
    http_tags=["worldview"], memory="off",
    description="Explain a record's position in the WorldView. "
                "Output: {concept, neighbors, predicted_next}.",
)
async def cap_worldview_explain_record(
    record_id: str = "",
    trace_id=None,
) -> Dict:
    if not MODEL.ready or not record_id:
        return {"error": "record_id required"}
    c = MODEL.record_concepts.get(record_id, -1)
    if c < 0:
        return {"error": "Record not in WorldView — retrain to include it"}

    neighbors = []
    for rid, cc in MODEL.record_concepts.items():
        if cc == c and rid != record_id:
            meta = _meta_for(rid)
            neighbors.append({
                "id":         rid,
                "text":       meta.get("text", "")[:120],
                "dataset_id": meta.get("dataset_id", ""),
            })
            if len(neighbors) >= 5:
                break

    with torch.no_grad():
        prefix = torch.tensor([[TOKEN_BOS, c + NUM_SPECIAL_TOKENS]],
                               dtype=torch.long, device=MODEL.device)
        logits = MODEL.dynamics(prefix)[0, -1]
        logits[TOKEN_BOS] = -1e9; logits[TOKEN_EOS] = -1e9
        probs = F.softmax(logits, dim=-1)
        top_p, top_i = torch.topk(probs, 5)
        predicted = []
        for p, i in zip(top_p, top_i):
            iv = int(i.item())
            if iv < NUM_SPECIAL_TOKENS:
                continue
            cc = iv - NUM_SPECIAL_TOKENS
            predicted.append({"concept": cc,
                              "prob": round(float(p.item()), 5),
                              "label": MODEL.concept_labels.get(cc, "")})

    return {
        "record_id":           record_id,
        "concept":             c,
        "concept_label":       MODEL.concept_labels.get(c, ""),
        "same_concept_neighbors": neighbors,
        "predicted_next":      predicted,
    }


@capability(
    "worldview.label_concepts",
    http_method="POST", http_path="/worldview/label_concepts",
    http_tags=["worldview", "llm"], memory="off",
    streams=["worldview.progress"],
    description="LLM-label concepts based on their member records. "
                "Input: concepts (list[int], optional), max_concepts (int).",
)
async def cap_worldview_label_concepts(
    concepts:     List[int] = None,
    max_concepts: int = 20,
    batch_size:   int = 5,
    trace_id=None,
) -> Dict:
    if not MODEL.ready:
        return {"error": "WorldView not ready"}

    pop = Counter(MODEL.record_concepts.values())
    if concepts:
        target = [c for c in concepts if c in pop]
    else:
        # Label unlabelled concepts first, then update existing labels
        target = [c for c, _ in pop.most_common() if c not in MODEL.concept_labels]
    target = target[:max_concepts]
    if not target:
        return {"labelled": [], "message": "Nothing to label"}

    await _emit("labelling", message=f"Labelling {len(target)} concepts (batch_size={batch_size})...",
                 total=len(target))

    members_by = defaultdict(list)
    for rid, c in MODEL.record_concepts.items():
        if c in target and len(members_by[c]) < 10:
            meta = _meta_for(rid)
            t = meta.get("text", "")
            if t:
                members_by[c].append(t[:200])

    labelled = []
    errors = 0
    for i, c in enumerate(target):
        samples = members_by.get(c, [])
        if not samples:
            continue
        prompt = (
            "Below are short text samples that have been clustered together "
            "by an unsupervised model. Suggest a SHORT (2-5 word) descriptive "
            "label for this cluster — what they have in common.\n\n"
            "Samples:\n"
            + "\n".join(f"- {s}" for s in samples[:8])
            + "\n\nReturn ONLY a JSON object: {\"label\": \"...\"}"
        )
        try:
            raw = await ollama_generate(prompt, system="You output JSON only.",
                                         json_mode=True)
            obj = json.loads(raw or "{}")
            lab = (obj.get("label") or "").strip()[:50]
            if lab:
                MODEL.concept_labels[c] = lab
                labelled.append({"idx": c, "label": lab})
                await _emit("labelled", concept=c, label=lab,
                             progress=i + 1, total=len(target))
        except Exception as e:
            errors += 1
            log.debug("label %d: %s", c, e)
            if errors > 3:
                log.warning("label_concepts: %d consecutive errors, stopping", errors)
                await _emit("label_error",
                             message=f"Stopped after {errors} errors at concept {i+1}/{len(target)}")
                break

        # Pause between batches to avoid overwhelming the LLM server
        if (i + 1) % batch_size == 0 and i < len(target) - 1:
            await asyncio.sleep(0.5)

    MODEL.save()
    await _persist_to_fabric()
    await _emit("label_done", labelled=len(labelled))
    return {"labelled": labelled, "total_existing": len(MODEL.concept_labels),
            "errors": errors}


# ─────────────────────────────────────────────────────────────────────────────
# AGENT / AGENTIC CAPS  —  higher-level queries for use by agents, DAGs, etc.
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "worldview.landscape",
    http_method="GET", http_path="/worldview/landscape",
    http_tags=["worldview", "agent"], memory="off",
    description="High-level summary of the WorldView concept landscape for "
                "agentic consumption.  Returns top-N concepts by population "
                "with labels, member counts, sample texts, and predicted "
                "transition partners.  Useful for agents that need to "
                "understand 'what does the data look like' before drilling in. "
                "Input: top_k (int, default 25), dataset_id (str, optional). "
                "Output: {concepts, total_records, total_concepts, datasets}.",
)
async def cap_worldview_landscape(
    top_k:      int = 25,
    dataset_id: str = "",
    trace_id=None,
) -> Dict:
    if not MODEL.ready:
        return {"error": "WorldView not ready — train first"}

    pop = Counter(MODEL.record_concepts.values())
    if not pop:
        return {"error": "No concept assignments — train the WorldView first"}

    # Filter by dataset if specified
    if dataset_id:
        filtered = Counter()
        for rid, c in MODEL.record_concepts.items():
            m = MODEL.record_meta.get(rid, {})
            if m.get("dataset_id") == dataset_id:
                filtered[c] += 1
        pop = filtered

    # Gather datasets seen
    datasets_seen = Counter()
    for rid, c in MODEL.record_concepts.items():
        m = MODEL.record_meta.get(rid, {})
        datasets_seen[m.get("dataset_id", "unknown")] += 1

    concepts = []
    for c in sorted(pop.keys(), key=lambda x: -pop[x])[:top_k]:
        # Sample members
        samples = []
        for rid, rc in MODEL.record_concepts.items():
            if rc == c and len(samples) < 3:
                m = MODEL.record_meta.get(rid, {})
                samples.append(m.get("text", "")[:120])
        # Predicted next
        predicted_next = []
        with torch.no_grad():
            try:
                seq = [TOKEN_BOS, c + NUM_SPECIAL_TOKENS]
                inp = torch.tensor([seq], device=MODEL.device)
                logits = MODEL.dynamics(inp)
                probs = torch.softmax(logits[0, -1, NUM_SPECIAL_TOKENS:MODEL.num_concepts + NUM_SPECIAL_TOKENS], dim=0)
                top_vals, top_idx = probs.topk(min(3, MODEL.num_concepts))
                for v, i in zip(top_vals, top_idx):
                    predicted_next.append({
                        "concept": int(i.item()),
                        "label": MODEL.concept_labels.get(int(i.item()), ""),
                        "prob": round(float(v.item()), 4),
                    })
            except Exception:
                pass

        concepts.append({
            "idx":        c,
            "label":      MODEL.concept_labels.get(c, ""),
            "population": pop[c],
            "samples":    samples,
            "predicted_next": predicted_next,
        })

    return {
        "concepts":       concepts,
        "total_records":  sum(pop.values()),
        "total_concepts": len(pop),
        "datasets":       [{"id": d, "count": n} for d, n in
                           datasets_seen.most_common(50)],
    }


@capability(
    "worldview.detect_drift",
    http_method="POST", http_path="/worldview/detect_drift",
    http_tags=["worldview", "agent"], memory="off",
    description="Detect concept drift in a dataset — compare which concepts "
                "are overrepresented relative to the global distribution. "
                "Useful for monitoring whether a dataset's content has shifted. "
                "Input: dataset_id (str, required). "
                "Output: {drifted_concepts, dataset_distribution, global_distribution}.",
)
async def cap_worldview_detect_drift(
    dataset_id: str = "",
    trace_id=None,
) -> Dict:
    if not MODEL.ready or not MODEL.record_concepts:
        return {"error": "WorldView not ready or no concept assignments"}
    if not dataset_id:
        return {"error": "dataset_id required"}

    global_pop = Counter(MODEL.record_concepts.values())
    global_total = sum(global_pop.values())
    ds_pop = Counter()
    ds_total = 0
    for rid, c in MODEL.record_concepts.items():
        m = MODEL.record_meta.get(rid, {})
        if m.get("dataset_id") == dataset_id:
            ds_pop[c] += 1
            ds_total += 1

    if ds_total == 0:
        return {"error": f"No records for dataset '{dataset_id}'"}

    # Find concepts where dataset's distribution differs significantly from global
    drifted = []
    for c in ds_pop:
        ds_frac = ds_pop[c] / ds_total
        gl_frac = global_pop.get(c, 0) / max(global_total, 1)
        if gl_frac > 0:
            ratio = ds_frac / gl_frac
        else:
            ratio = float("inf")
        if ratio > 2.0 or ratio < 0.3:
            drifted.append({
                "concept":      c,
                "label":        MODEL.concept_labels.get(c, ""),
                "dataset_frac": round(ds_frac, 4),
                "global_frac":  round(gl_frac, 4),
                "ratio":        round(ratio, 2) if ratio != float("inf") else "inf",
                "direction":    "over" if ratio > 1.0 else "under",
            })
    drifted.sort(key=lambda d: -(d["ratio"] if isinstance(d["ratio"], (int, float)) else 999))

    return {
        "dataset_id":     dataset_id,
        "dataset_records": ds_total,
        "global_records":  global_total,
        "drifted_concepts": drifted,
    }


@capability(
    "worldview.summarise",
    http_method="POST", http_path="/worldview/summarise",
    http_tags=["worldview", "agent"], memory="off",
    description="Structured summary of worldview state for agents/DAGs. "
                "Returns model health, top concepts, recent anomalies, "
                "and streaming worker status in a single call. "
                "Input: dataset_id (str, optional). "
                "Output: comprehensive dict.",
)
async def cap_worldview_summarise(
    dataset_id: str = "",
    trace_id=None,
) -> Dict:
    summary = {
        "model_ready":    MODEL.ready,
        "device":         MODEL.device if MODEL.ready else "n/a",
        "train_steps":    dict(MODEL.train_steps) if MODEL.ready else {},
        "train_loss":     {k: round(v, 5) for k, v in MODEL.train_loss.items()} if MODEL.ready else {},
        "total_records":  len(MODEL.record_concepts),
        "total_concepts": len(set(MODEL.record_concepts.values())),
        "labelled":       len(MODEL.concept_labels),
        "faiss_available": HAS_FAISS,
        "index_vectors":  WV_INDEX.stats().get("vectors", 0) if WV_INDEX.available else 0,
        "streaming":      _STREAM_ENABLED,
        "stream_stats":   {k: v for k, v in _STREAM_STATS.items()
                           if k not in ("pending_concepts",)},
    }

    # Top concepts
    pop = Counter(MODEL.record_concepts.values())
    if dataset_id:
        pop = Counter(c for rid, c in MODEL.record_concepts.items()
                      if MODEL.record_meta.get(rid, {}).get("dataset_id") == dataset_id)
    top = []
    for c in sorted(pop.keys(), key=lambda x: -pop[x])[:10]:
        top.append({
            "idx": c,
            "label": MODEL.concept_labels.get(c, ""),
            "count": pop[c],
        })
    summary["top_concepts"] = top

    # Datasets breakdown
    ds_counts = Counter()
    for rid in MODEL.record_concepts:
        m = MODEL.record_meta.get(rid, {})
        ds_counts[m.get("dataset_id", "?")] += 1
    summary["datasets"] = [{"id": d, "count": n}
                            for d, n in ds_counts.most_common(20)]

    return summary


@capability(
    "worldview.loss_history",
    http_method="GET", http_path="/worldview/loss_history",
    http_tags=["worldview"], memory="off",
    description="Return the persisted per-epoch loss history for all three training stages.",
)
async def cap_worldview_loss_history(trace_id=None) -> Dict:
    return {
        "gnn":      _WV_LOSS_HISTORY.get("gnn", []),
        "codebook": _WV_LOSS_HISTORY.get("codebook", []),
        "dynamics": _WV_LOSS_HISTORY.get("dynamics", []),
        "total_epochs": sum(len(v) for v in _WV_LOSS_HISTORY.values()),
    }


@capability(
    "worldview.stats",
    http_method="GET", http_path="/worldview/stats",
    http_tags=["worldview"], memory="off",
    description="Full WorldView stats. Output: {model, index}.",
)
async def cap_worldview_stats(trace_id=None) -> Dict:
    return {
        "model": MODEL.stats(),
        "index": WV_INDEX.stats(),
        "embed_model": _orch.OLLAMA_EMBED_MODEL,
        "has_faiss": HAS_FAISS,
        "has_numpy": HAS_NUMPY,
        "has_torch": HAS_TORCH,
    }


# ─────────────────────────────────────────────────────────────────────────────
# UI PANEL  —  iframe-mounted via the standard register_ui() pattern
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# MODEL CONFIG  —  runtime-adjustable hyperparameters for next training run
# ─────────────────────────────────────────────────────────────────────────────

# Runtime overrides — start from module-level defaults
_WV_CONFIG: Dict[str, Any] = {
    "latent_dim":      LATENT_DIM,
    "hidden_dim":      HIDDEN_DIM,
    "num_gnn_layers":  NUM_GNN_LAYERS,
    "num_concepts":    NUM_CONCEPTS,
    "vq_decay":        VQ_DECAY,
    "vq_commitment":   VQ_COMMITMENT,
    "dyn_dim":         DYN_DIM,
    "dyn_heads":       DYN_HEADS,
    "dyn_layers":      DYN_LAYERS,
    "dyn_ctx":         DYN_CTX,
    "lr":              LR,
    "batch_size":      BATCH_SIZE,
    "max_nodes":       MAX_NODES,
    "max_walks":       MAX_WALKS,
    "walk_len":        WALK_LEN,
    "contrastive_margin": 0.2,
    "uniformity_weight":  0.1,
    "revive_dead":        True,
    "revive_threshold":   1e-3,
    "revive_jitter":      0.03,
    "entropy_weight":     5.0,         # strong: higher = more concept diversity
    "vq_jitter":          0.05,
    "vq_temp_start":      2.0,         # Gumbel temperature at codebook epoch 0
    "vq_temp_end":        0.0,         # anneal to hard assignment
    "vq_temp_anneal_steps": 40,        # anneal over this many codebook steps
    "kmeans_reinit_every":  0,         # periodic k-means reinit (0=off, e.g. 25)
    "jepa_weight":        1.0,
    "vicreg_weight":      0.5,
    "target_ema_decay":   0.996,
    "auto_label":         True,        # auto-label concepts after training
    "auto_label_k":       50,          # max concepts to auto-label
}


@capability(
    "worldview.config",
    http_method="GET", http_path="/worldview/config",
    http_tags=["worldview"], memory="off",
    description="Return current WorldView hyperparameters (runtime-adjustable). "
                "Output: dict of all tunable parameters with current values.",
)
async def cap_worldview_config(trace_id=None) -> Dict:
    return {**_WV_CONFIG, "embed_dim": MODEL.embed_dim if MODEL.ready else EMBED_DIM}


@capability(
    "worldview.config_set",
    http_method="POST", http_path="/worldview/config",
    http_tags=["worldview"], memory="off",
    description="Update WorldView hyperparameters for the next training run. "
                "Input: any subset of config keys. Changes that affect model "
                "architecture (latent_dim, num_concepts, etc.) require a fresh "
                "training run; optimizer params (lr, batch_size) take effect "
                "immediately on the next epoch. "
                "Output: {updated, config}.",
)
async def cap_worldview_config_set(
    latent_dim:         int   = None,
    hidden_dim:         int   = None,
    num_gnn_layers:     int   = None,
    num_concepts:       int   = None,
    vq_decay:           float = None,
    vq_commitment:      float = None,
    dyn_dim:            int   = None,
    dyn_heads:          int   = None,
    dyn_layers:         int   = None,
    dyn_ctx:            int   = None,
    lr:                 float = None,
    batch_size:         int   = None,
    max_nodes:          int   = None,
    max_walks:          int   = None,
    walk_len:           int   = None,
    contrastive_margin: float = None,
    uniformity_weight:  float = None,
    revive_dead:        bool  = None,
    revive_threshold:   float = None,
    revive_jitter:      float = None,
    entropy_weight:     float = None,
    vq_jitter:          float = None,
    vq_temp_start:      float = None,
    vq_temp_end:        float = None,
    vq_temp_anneal_steps: int = None,
    kmeans_reinit_every:  int = None,
    jepa_weight:        float = None,
    vicreg_weight:      float = None,
    target_ema_decay:   float = None,
    auto_label:         bool  = None,
    auto_label_k:       int   = None,
    trace_id=None,
) -> Dict:
    updated = []
    params = {
        "latent_dim": latent_dim, "hidden_dim": hidden_dim,
        "num_gnn_layers": num_gnn_layers, "num_concepts": num_concepts,
        "vq_decay": vq_decay, "vq_commitment": vq_commitment,
        "dyn_dim": dyn_dim, "dyn_heads": dyn_heads,
        "dyn_layers": dyn_layers, "dyn_ctx": dyn_ctx,
        "lr": lr, "batch_size": batch_size,
        "max_nodes": max_nodes, "max_walks": max_walks, "walk_len": walk_len,
        "contrastive_margin": contrastive_margin,
        "uniformity_weight": uniformity_weight,
        "revive_dead": revive_dead, "revive_threshold": revive_threshold,
        "revive_jitter": revive_jitter, "entropy_weight": entropy_weight,
        "vq_jitter": vq_jitter,
        "vq_temp_start": vq_temp_start, "vq_temp_end": vq_temp_end,
        "vq_temp_anneal_steps": vq_temp_anneal_steps,
        "kmeans_reinit_every": kmeans_reinit_every,
        "jepa_weight": jepa_weight,
        "vicreg_weight": vicreg_weight, "target_ema_decay": target_ema_decay,
        "auto_label": auto_label, "auto_label_k": auto_label_k,
    }
    for key, val in params.items():
        if val is not None and key in _WV_CONFIG:
            old = _WV_CONFIG[key]
            _WV_CONFIG[key] = type(old)(val)
            updated.append({"key": key, "old": old, "new": _WV_CONFIG[key]})

    # Apply immediately where possible (lr changes the optimizer directly)
    if lr is not None and MODEL.ready:
        for pg in MODEL.opt_gnn.param_groups:
            pg["lr"] = _WV_CONFIG["lr"]
        for pg in MODEL.opt_dyn.param_groups:
            pg["lr"] = _WV_CONFIG["lr"]

    await _emit("config_updated", updated=updated,
                message=f"Updated {len(updated)} config params")
    return {"updated": updated, "config": {**_WV_CONFIG}}


# ─────────────────────────────────────────────────────────────────────────────
# STREAMING WORKER  —  incremental WorldView updates from Redis events
# ─────────────────────────────────────────────────────────────────────────────
# Subscribes to vera:events, watches for fabric.ingested / fabric.pipeline.stage
# events. When new records land in Chroma, the worker:
#   1. Fetches the new records' embeddings
#   2. Encodes them through the GNN (isolated, no graph context)
#   3. Assigns them to concepts via the codebook
#   4. Adds them to the FAISS index
#   5. Periodically fine-tunes the dynamics model on walks that include
#      the newly-assigned concepts
#
# This gives the WorldView a live, continuously-updating picture of the
# fabric without requiring full retraining.
# ─────────────────────────────────────────────────────────────────────────────

_STREAM_ENABLED = False
_STREAM_TASK: Optional[asyncio.Task] = None
_STREAM_STATS: Dict[str, Any] = {
    "started_at":        "",
    "events_seen":       0,
    "records_encoded":   0,
    "records_indexed":   0,
    "dynamics_updates":  0,
    "errors":            0,
    "last_event_at":     "",
    "last_error":        "",
    "pending_concepts":  [],      # concepts touched since last dynamics update
    "dynamics_interval": 60,      # seconds between dynamics fine-tune passes
    "event_filters":     ["fabric.ingested", "fabric.upserted",
                          "fabric.pipeline.stage", "fabric.dataset.created"],
    "batch_size":        64,      # max records to process per ingestion event
}


async def _stream_encode_new_records(dataset_id: str, limit: int = 64,
                                      record_ids: List[str] = None) -> int:
    """Fetch recently-ingested records from Chroma and add them to the index.

    If record_ids is provided, fetches those specific records.
    Otherwise fetches the latest records and filters to un-indexed ones.
    """
    if not MODEL.ready:
        return 0
    if MODEL.train_steps.get("gnn", 0) == 0:
        return 0

    fab = _get_fabric()
    if not fab:
        return 0
    chroma = getattr(fab, "FABRIC_CHROMA", None)
    if not chroma or not getattr(chroma, "available", False):
        return 0

    loop = asyncio.get_running_loop()

    def _fetch():
        col = chroma._col
        if col is None:
            return [], [], [], []
        try:
            if record_ids:
                # Fetch specific records by ID
                batch_ids = [r for r in record_ids[:limit]
                             if r not in MODEL.record_concepts]
                if not batch_ids:
                    return [], [], [], []
                res = col.get(ids=batch_ids,
                              include=["embeddings", "documents", "metadatas"])
            else:
                # Fetch records from this dataset, Chroma returns in insertion order
                kwargs = {
                    "include": ["embeddings", "documents", "metadatas"],
                    "limit": min(limit * 2, 1000),  # over-fetch to find new ones
                }
                if dataset_id:
                    kwargs["where"] = {"dataset_id": {"$eq": dataset_id}}
                res = col.get(**kwargs)
            return (res.get("ids") or [], res.get("embeddings") or [],
                    res.get("documents") or [], res.get("metadatas") or [])
        except Exception as e:
            log.debug("stream fetch: %s", e)
            return [], [], [], []

    try:
        ids, embs, docs, metas = await loop.run_in_executor(None, _fetch)
    except Exception as e:
        log.debug("stream encode fetch: %s", e)
        return 0

    if not ids:
        return 0

    # Filter to records NOT already indexed
    existing = set(MODEL.record_concepts.keys())
    new_indices = []
    for i, rid in enumerate(ids):
        if rid not in existing and i < len(embs) and embs[i] is not None:
            try:
                if len(embs[i]) >= 10:
                    new_indices.append(i)
            except Exception:
                pass

    if not new_indices:
        return 0

    # Encode each new record through the GNN (isolated — no graph context)
    encoded = 0
    new_concepts = []
    for idx in new_indices:
        rid = ids[idx]
        try:
            emb = embs[idx]
            if len(emb) != MODEL.embed_dim:
                continue
            latent = MODEL.encode_isolated(emb)
            if latent is None:
                continue
            concept = MODEL.assign_concept(latent)
            MODEL.record_concepts[rid] = concept
            meta = (metas[idx] if idx < len(metas) else {}) or {}
            text = (docs[idx] if idx < len(docs) else "") or ""
            m = {
                "dataset_id":    meta.get("dataset_id", dataset_id),
                "text":          text[:200],
                "created_at":    meta.get("created_at", ""),
                "concept":       concept,
                "concept_label": MODEL.concept_labels.get(concept, ""),
            }
            MODEL.record_meta[rid] = m
            if WV_INDEX.available:
                WV_INDEX.add_batch([rid], [latent], [m])
            new_concepts.append(concept)
            encoded += 1
        except Exception as e:
            log.debug("stream encode record %s: %s", rid, e)
            continue

    if new_concepts:
        # Track which concepts were touched for dynamics fine-tuning
        _STREAM_STATS["pending_concepts"].extend(new_concepts)

    return encoded


async def _stream_dynamics_update() -> float:
    """Fine-tune the dynamics transformer on walks involving recently-touched concepts.

    Returns the average loss, or 0 if nothing to train on.
    """
    if not MODEL.ready or MODEL.train_steps.get("codebook", 0) == 0:
        return 0.0

    pending = _STREAM_STATS["pending_concepts"]
    if not pending:
        return 0.0

    # Build walks from the known record_concepts
    # We generate short walks biased towards recently-touched concepts
    touched_set = set(pending)
    all_concepts = list(MODEL.record_concepts.values())
    if len(all_concepts) < 4:
        return 0.0

    walks = []
    for _ in range(min(500, len(pending) * 10)):
        # Start from a touched concept, walk via transition counts
        start = random.choice(pending)
        seq = [start]
        cur = start
        for _ in range(_WV_CONFIG["walk_len"] - 1):
            # Find transitions from cur
            candidates = [(b, n) for (a, b), n in MODEL.transition_counts.items()
                          if a == cur and n > 0]
            if not candidates:
                # Fall back to random concept
                cur = random.choice(all_concepts)
            else:
                # Weighted sample
                total_w = sum(n for _, n in candidates)
                r = random.random() * total_w
                cumulative = 0
                for b, n in candidates:
                    cumulative += n
                    if r <= cumulative:
                        cur = b
                        break
            seq.append(cur)
        if len(seq) >= 2:
            walks.append([TOKEN_BOS] + [c + NUM_SPECIAL_TOKENS for c in seq])

    if not walks:
        return 0.0

    # Train a few mini-batches
    bs = _WV_CONFIG["batch_size"]
    random.shuffle(walks)
    losses = []
    for i in range(0, min(len(walks), bs * 3), bs):
        batch = walks[i:i + bs]
        if len(batch) < 2:
            continue
        l = MODEL.train_dynamics_step(batch)
        losses.append(l)

    # Update transition counts with new walks
    for w in walks:
        body = [t - NUM_SPECIAL_TOKENS for t in w if t >= NUM_SPECIAL_TOKENS]
        for i in range(len(body) - 1):
            k = (body[i], body[i + 1])
            MODEL.transition_counts[k] = MODEL.transition_counts.get(k, 0) + 1

    # Clear pending
    _STREAM_STATS["pending_concepts"] = []
    avg = sum(losses) / len(losses) if losses else 0.0
    _STREAM_STATS["dynamics_updates"] += 1
    return avg


async def _wv_stream_worker():
    """Background worker: subscribe to vera:events, incrementally update WorldView."""
    backoff = 2.0
    last_dynamics_update = time.time()

    while _STREAM_ENABLED:
        redis = _orch.REDIS
        if redis is None:
            await asyncio.sleep(5)
            continue
        try:
            stream = "vera:events"
            group = "worldview_stream"
            consumer = f"worldview-{os.getpid()}"
            try:
                await redis.xgroup_create(stream, group, id="$", mkstream=True)
            except Exception:
                pass  # BUSYGROUP — already exists
            backoff = 2.0
            log.info("worldview stream worker attached to %s", stream)
            await _emit("stream_attached", message="Streaming worker connected to Redis")

            while _STREAM_ENABLED:
                try:
                    msgs = await redis.xreadgroup(
                        group, consumer, {stream: ">"}, count=20, block=5000)
                except Exception as e:
                    log.warning("worldview stream read: %s", e)
                    break
                if not msgs:
                    # Check if dynamics update is due
                    now = time.time()
                    interval = _STREAM_STATS.get("dynamics_interval", 60)
                    if (now - last_dynamics_update) >= interval and _STREAM_STATS["pending_concepts"]:
                        try:
                            avg_loss = await _stream_dynamics_update()
                            last_dynamics_update = now
                            if avg_loss > 0:
                                await _emit("stream_dynamics",
                                            loss=round(avg_loss, 6),
                                            updates=_STREAM_STATS["dynamics_updates"],
                                            message=f"Dynamics fine-tune loss={avg_loss:.4f}")
                                MODEL.save()
                        except Exception as e:
                            log.debug("stream dynamics update: %s", e)
                    continue

                for _, entries in msgs:
                    for msg_id, fields in entries:
                        try:
                            raw = fields.get(b"data") or fields.get("data") or b"{}"
                            ev = json.loads(raw)
                            etype = ev.get("type", "")
                            _STREAM_STATS["events_seen"] += 1
                            _STREAM_STATS["last_event_at"] = ev.get("ts", "")

                            filters = _STREAM_STATS.get("event_filters",
                                                         ["fabric.ingested"])
                            if any(etype == f or etype.startswith(f + ".")
                                   for f in filters):
                                dataset_id = ev.get("dataset_id", "")
                                # Accept event even without explicit count —
                                # many fabric events don't carry one
                                record_ids = ev.get("record_ids") or ev.get("ids") or []
                                batch_limit = _STREAM_STATS.get("batch_size", 64)
                                n = await _stream_encode_new_records(
                                    dataset_id, limit=batch_limit,
                                    record_ids=record_ids)
                                _STREAM_STATS["records_encoded"] += n
                                _STREAM_STATS["records_indexed"] += n
                                if n > 0:
                                    await _emit(
                                        "stream_encoded",
                                        dataset_id=dataset_id,
                                        encoded=n,
                                        total_encoded=_STREAM_STATS["records_encoded"],
                                        message=f"Encoded {n} new records from {dataset_id or 'unknown'}")

                            await redis.xack(stream, group, msg_id)
                        except Exception as e:
                            _STREAM_STATS["errors"] += 1
                            _STREAM_STATS["last_error"] = str(e)[:200]
                            log.debug("worldview stream event: %s", e)

                # Periodic dynamics update
                now = time.time()
                interval = _STREAM_STATS.get("dynamics_interval", 60)
                if (now - last_dynamics_update) >= interval and _STREAM_STATS["pending_concepts"]:
                    try:
                        avg_loss = await _stream_dynamics_update()
                        last_dynamics_update = now
                        if avg_loss > 0:
                            await _emit("stream_dynamics",
                                        loss=round(avg_loss, 6),
                                        updates=_STREAM_STATS["dynamics_updates"],
                                        message=f"Dynamics fine-tune loss={avg_loss:.4f}")
                            MODEL.save()
                    except Exception as e:
                        log.debug("stream dynamics update: %s", e)

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("worldview stream worker: %s", e)
            _STREAM_STATS["errors"] += 1
            _STREAM_STATS["last_error"] = str(e)[:200]
            backoff = min(backoff * 2, 60.0)
            await asyncio.sleep(backoff)

    log.info("worldview stream worker stopped")
    await _emit("stream_stopped", message="Streaming worker stopped")


@capability(
    "worldview.stream.start",
    http_method="POST", http_path="/worldview/stream/start",
    http_tags=["worldview", "stream"], memory="off",
    description="Start the WorldView streaming worker. Subscribes to Redis "
                "events and incrementally encodes new fabric records, assigns "
                "them to concepts, and fine-tunes the dynamics model. "
                "Input: event_filters (list[str], default ['fabric.ingested']), "
                "dynamics_interval (int, seconds between dynamics updates, default 60), "
                "batch_size (int, max records per event, default 64). "
                "Output: {running, stats}.",
)
async def cap_worldview_stream_start(
    event_filters:     List[str] = None,
    dynamics_interval: int = 60,
    batch_size:        int = 64,
    trace_id=None,
) -> Dict:
    global _STREAM_ENABLED, _STREAM_TASK
    if not MODEL.ready:
        return {"error": "WorldView not ready — train first"}
    if MODEL.train_steps.get("gnn", 0) == 0:
        return {"error": "GNN has not been trained — run worldview.train first, "
                         "then start streaming for incremental updates"}

    if event_filters:
        _STREAM_STATS["event_filters"] = event_filters
    _STREAM_STATS["dynamics_interval"] = max(10, dynamics_interval)
    _STREAM_STATS["batch_size"] = max(1, min(500, batch_size))

    if _STREAM_ENABLED and _STREAM_TASK and not _STREAM_TASK.done():
        return {"running": True, "note": "already running", "stats": _STREAM_STATS}

    _STREAM_ENABLED = True
    _STREAM_STATS["started_at"] = now_iso()
    _STREAM_STATS["events_seen"] = 0
    _STREAM_STATS["records_encoded"] = 0
    _STREAM_STATS["records_indexed"] = 0
    _STREAM_STATS["dynamics_updates"] = 0
    _STREAM_STATS["errors"] = 0
    _STREAM_STATS["last_error"] = ""
    _STREAM_STATS["pending_concepts"] = []
    _STREAM_TASK = asyncio.create_task(_wv_stream_worker())
    return {"running": True, "stats": _STREAM_STATS}


@capability(
    "worldview.stream.stop",
    http_method="POST", http_path="/worldview/stream/stop",
    http_tags=["worldview", "stream"], memory="off",
    description="Stop the WorldView streaming worker.",
)
async def cap_worldview_stream_stop(trace_id=None) -> Dict:
    global _STREAM_ENABLED, _STREAM_TASK
    was_running = _STREAM_ENABLED
    _STREAM_ENABLED = False
    if _STREAM_TASK and not _STREAM_TASK.done():
        _STREAM_TASK.cancel()
    _STREAM_TASK = None
    # Save model state on stop
    if MODEL.ready:
        MODEL.save()
    return {"stopped": True, "was_running": was_running, "stats": _STREAM_STATS}


@capability(
    "worldview.stream.status",
    http_method="GET", http_path="/worldview/stream/status",
    http_tags=["worldview", "stream"], memory="off",
    description="Status of the WorldView streaming worker.",
)
async def cap_worldview_stream_status(trace_id=None) -> Dict:
    return {
        "enabled":    _STREAM_ENABLED,
        "task_alive": _STREAM_TASK is not None and not _STREAM_TASK.done(),
        **_STREAM_STATS,
    }

@APP.get("/ui/panels/worldview-panel", include_in_schema=False)
async def _worldview_panel():
    """WorldView panel HTML — served into the fabric panel iframe."""
    from fastapi.responses import HTMLResponse
    p = _HERE / "worldview_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>worldview_panel.html not found</p>")


# ─────────────────────────────────────────────────────────────────────────────
# FABRIC PERSISTENCE CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "worldview.persist",
    http_method="POST", http_path="/worldview/persist",
    http_tags=["worldview", "fabric"], memory="off",
    description="Force-persist the model checkpoint into the fabric (SQLite+Postgres).",
)
async def cap_worldview_persist(trace_id=None) -> Dict:
    if not MODEL.ready:
        return {"error": "WorldView not ready"}
    if MODEL.train_steps.get("gnn", 0) == 0:
        return {"error": "Nothing to persist — not trained"}
    return await _persist_to_fabric()


@capability(
    "worldview.load_from_fabric",
    http_method="POST", http_path="/worldview/load_from_fabric",
    http_tags=["worldview", "fabric"], memory="off",
    description="Restore the model checkpoint from the fabric. Input: rebuild_index (bool).",
)
async def cap_worldview_load_from_fabric(rebuild_index: bool = True, trace_id=None) -> Dict:
    if not HAS_TORCH or not MODEL.ready:
        return {"error": "WorldView not ready"}
    blob = await _fabric_load_checkpoint()
    if not blob:
        return {"ok": False, "restored": False, "error": "No checkpoint in fabric"}
    if not MODEL.load_bytes(blob):
        return {"ok": False, "restored": False, "error": "Load failed (K/dim mismatch?)"}
    MODEL.last_fabric_load = now_iso()
    global _wv_startup_done
    _wv_startup_done = True
    indexed = 0
    if rebuild_index and MODEL.train_steps.get("gnn", 0) > 0:
        try:
            indexed = await _rebuild_index_from_fabric("", limit=_WV_CONFIG.get("max_nodes", MAX_NODES))
        except Exception as e:
            log.warning("load_from_fabric index rebuild: %s", e)
    return {
        "ok": True, "restored": True,
        "train_steps": MODEL.train_steps,
        "records": len(MODEL.record_concepts),
        "indexed": indexed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SUB-WORLDVIEWS  —  named checkpoints scoped to specific dataset(s)
# Stored in fabric SQLite. Activating one swaps the global MODEL state.
# ─────────────────────────────────────────────────────────────────────────────

_WV_ACTIVE_SUBVIEW: str = ""   # "" = global (default)
_WV_ACTIVE_SUBVIEW_DATASETS: List[str] = []   # cached for the active sub

async def _get_subview_datasets(name: str) -> List[str]:
    """Return dataset IDs for a named sub-worldview, or [] for global."""
    if not name:
        return []
    fab = _get_fabric()
    if not fab:
        return []
    try:
        loop = asyncio.get_running_loop()
        def _r():
            conn = fab._sqlite_conn() if callable(getattr(fab, "_sqlite_conn", None)) else None
            if not conn: return []
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS worldview_subviews("
                             "name TEXT PRIMARY KEY, datasets TEXT, meta TEXT, updated_at TEXT)")
                cur = conn.execute("SELECT datasets FROM worldview_subviews WHERE name=?", (name,))
                row = cur.fetchone()
                return json.loads(row[0]) if row and row[0] else []
            finally:
                conn.close()
        return await loop.run_in_executor(None, _r)
    except Exception:
        return []

@capability(
    "worldview.subview.list",
    http_method="GET", http_path="/worldview/subviews",
    http_tags=["worldview", "subview"], memory="off",
    description="List all saved sub-worldviews.",
)
async def cap_subview_list(trace_id=None) -> Dict:
    fab = _get_fabric()
    if not fab:
        return {"subviews": [], "active": _WV_ACTIVE_SUBVIEW}
    try:
        loop = asyncio.get_running_loop()
        def _r():
            conn = fab._sqlite_conn() if callable(getattr(fab, "_sqlite_conn", None)) else None
            if not conn: return []
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS worldview_subviews("
                             "name TEXT PRIMARY KEY, datasets TEXT, meta TEXT, updated_at TEXT)")
                cur = conn.execute("SELECT name, datasets, meta, updated_at FROM worldview_subviews ORDER BY name")
                return [{"name": r[0], "datasets": json.loads(r[1] or "[]"),
                         "meta": json.loads(r[2] or "{}"), "updated_at": r[3]} for r in cur.fetchall()]
            finally:
                conn.close()
        subs = await loop.run_in_executor(None, _r)
        return {"subviews": subs, "active": _WV_ACTIVE_SUBVIEW}
    except Exception as e:
        return {"subviews": [], "active": _WV_ACTIVE_SUBVIEW, "error": str(e)}


@capability(
    "worldview.subview.create",
    http_method="POST", http_path="/worldview/subviews/create",
    http_tags=["worldview", "subview"], memory="off",
    description="Create a named sub-worldview. "
                "Input: name (str), datasets (list[str], optional), "
                "node_ids (list[str], optional — train on exactly these record "
                "IDs, e.g. the node set of a saved vera-graph), "
                "graph (str, optional — name of the paired saved graph). "
                "Provide datasets OR node_ids.",
)
async def cap_subview_create(name: str = "", datasets: List[str] = None,
                             node_ids: List[str] = None, graph: str = "",
                             trace_id=None) -> Dict:
    if not name:
        return {"error": "Provide a name"}
    if not datasets and not node_ids:
        return {"error": "Provide datasets or node_ids"}
    datasets = datasets or []
    fab = _get_fabric()
    if not fab:
        return {"error": "Fabric not available"}
    try:
        meta = {"created_at": now_iso(), "datasets": datasets}
        if node_ids:
            meta["node_ids"] = list(node_ids)[:50000]
            meta["scope"] = "graph_nodes"
        if graph:
            meta["graph"] = graph          # paired saved-graph name
        loop = asyncio.get_running_loop()
        def _w():
            conn = fab._sqlite_conn() if callable(getattr(fab, "_sqlite_conn", None)) else None
            if not conn: return False
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS worldview_subviews("
                             "name TEXT PRIMARY KEY, datasets TEXT, meta TEXT, updated_at TEXT)")
                conn.execute("INSERT INTO worldview_subviews(name,datasets,meta,updated_at) "
                             "VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                             "datasets=excluded.datasets,meta=excluded.meta,updated_at=excluded.updated_at",
                             (name, json.dumps(datasets), json.dumps(meta), now_iso()))
                conn.commit(); return True
            finally:
                conn.close()
        ok = await loop.run_in_executor(None, _w)
        return {"ok": ok, "name": name, "datasets": datasets,
                "node_ids": len(meta.get("node_ids", [])), "graph": graph or None}
    except Exception as e:
        return {"error": str(e)}


@capability(
    "worldview.subview.activate",
    http_method="POST", http_path="/worldview/subviews/activate",
    http_tags=["worldview", "subview"], memory="off",
    description="Activate a sub-worldview (loads its checkpoint). Pass name='' for global.",
)
async def cap_subview_activate(name: str = "", trace_id=None) -> Dict:
    global _WV_ACTIVE_SUBVIEW, _WV_ACTIVE_SUBVIEW_DATASETS
    if not name or name == "global":
        # Switch to global — reload the default checkpoint
        prev = _WV_ACTIVE_SUBVIEW
        _WV_ACTIVE_SUBVIEW = ""
        _WV_ACTIVE_SUBVIEW_DATASETS = []
        # Try fabric checkpoint first (most complete state)
        blob = await _fabric_load_checkpoint()
        if blob and MODEL.load_bytes(blob):
            MODEL.last_fabric_load = now_iso()
            log.info("worldview: switched to global (fabric checkpoint)")
            return {"ok": True, "active": "", "previous": prev,
                    "message": "Switched to global WorldView",
                    "train_steps": MODEL.train_steps,
                    "records": len(MODEL.record_concepts)}
        # Fall back to local file
        if MODEL.load():
            log.info("worldview: switched to global (local file)")
            return {"ok": True, "active": "", "previous": prev,
                    "message": "Switched to global (local file)",
                    "train_steps": MODEL.train_steps,
                    "records": len(MODEL.record_concepts)}
        return {"ok": True, "active": "", "previous": prev,
                "message": "Switched to global (no checkpoint — model reset)"}

    # Load sub-worldview checkpoint
    fab = _get_fabric()
    if not fab:
        return {"error": "Fabric not available"}
    # First verify the sub exists
    try:
        loop = asyncio.get_running_loop()
        def _check():
            conn = fab._sqlite_conn() if callable(getattr(fab, "_sqlite_conn", None)) else None
            if not conn: return None
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS worldview_subviews("
                             "name TEXT PRIMARY KEY, datasets TEXT, meta TEXT, updated_at TEXT)")
                cur = conn.execute("SELECT datasets FROM worldview_subviews WHERE name=?", (name,))
                row = cur.fetchone()
                return json.loads(row[0]) if row else None
            finally:
                conn.close()
        datasets = await loop.run_in_executor(None, _check)
    except Exception:
        datasets = None

    key = f"worldview_sub_{name}"
    try:
        loop = asyncio.get_running_loop()
        def _r():
            conn = fab._sqlite_conn() if callable(getattr(fab, "_sqlite_conn", None)) else None
            if not conn: return None
            try:
                cur = conn.execute("SELECT blob FROM worldview_checkpoints WHERE key=?", (key,))
                r = cur.fetchone()
                return bytes(r[0]) if r and r[0] else None
            except Exception:
                return None
            finally:
                conn.close()
        blob = await loop.run_in_executor(None, _r)
    except Exception:
        blob = None
    if blob and MODEL.load_bytes(blob):
        _WV_ACTIVE_SUBVIEW = name
        _WV_ACTIVE_SUBVIEW_DATASETS = datasets or []
        MODEL.last_fabric_load = now_iso()
        return {"ok": True, "active": name, "datasets": datasets,
                "train_steps": MODEL.train_steps,
                "records": len(MODEL.record_concepts)}
    elif not blob:
        _WV_ACTIVE_SUBVIEW = name
        _WV_ACTIVE_SUBVIEW_DATASETS = datasets or []
        return {"ok": True, "active": name, "datasets": datasets,
                "message": "Sub-worldview activated (no checkpoint yet — train it)"}
    return {"error": "Failed to load checkpoint"}


@capability(
    "worldview.subview.save",
    http_method="POST", http_path="/worldview/subviews/save",
    http_tags=["worldview", "subview"], memory="off",
    description="Save the current model state as the active sub-worldview's checkpoint.",
)
async def cap_subview_save(trace_id=None) -> Dict:
    if not _WV_ACTIVE_SUBVIEW:
        return {"error": "No sub-worldview active (use global persist instead)"}
    if not MODEL.ready:
        return {"error": "Model not ready"}
    blob = MODEL.serialize_bytes()
    if not blob:
        return {"error": "Serialize failed"}
    key = f"worldview_sub_{_WV_ACTIVE_SUBVIEW}"
    meta = {"name": _WV_ACTIVE_SUBVIEW, "train_steps": MODEL.train_steps,
            "records": len(MODEL.record_concepts), "saved_at": now_iso()}
    fab = _get_fabric()
    if not fab:
        return {"error": "Fabric not available"}
    try:
        loop = asyncio.get_running_loop()
        def _w():
            conn = fab._sqlite_conn() if callable(getattr(fab, "_sqlite_conn", None)) else None
            if not conn: return False
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS worldview_checkpoints("
                             "key TEXT PRIMARY KEY, blob BLOB, meta TEXT, updated_at TEXT)")
                conn.execute("INSERT INTO worldview_checkpoints(key,blob,meta,updated_at) "
                             "VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                             "blob=excluded.blob,meta=excluded.meta,updated_at=excluded.updated_at",
                             (key, sqlite3.Binary(blob), json.dumps(meta), now_iso()))
                conn.commit(); return True
            finally:
                conn.close()
        ok = await loop.run_in_executor(None, _w)
        return {"ok": ok, "name": _WV_ACTIVE_SUBVIEW, "bytes": len(blob)}
    except Exception as e:
        return {"error": str(e)}


@capability(
    "worldview.subview.delete",
    http_method="POST", http_path="/worldview/subviews/delete",
    http_tags=["worldview", "subview"], memory="off",
    description="Delete a sub-worldview (name + checkpoint). Input: name (str).",
)
async def cap_subview_delete(name: str = "", trace_id=None) -> Dict:
    global _WV_ACTIVE_SUBVIEW
    if not name:
        return {"error": "Provide a name"}
    fab = _get_fabric()
    if not fab:
        return {"error": "Fabric not available"}
    try:
        loop = asyncio.get_running_loop()
        def _w():
            conn = fab._sqlite_conn() if callable(getattr(fab, "_sqlite_conn", None)) else None
            if not conn: return False
            try:
                conn.execute("DELETE FROM worldview_subviews WHERE name=?", (name,))
                conn.execute("DELETE FROM worldview_checkpoints WHERE key=?",
                             (f"worldview_sub_{name}",))
                conn.commit(); return True
            finally:
                conn.close()
        ok = await loop.run_in_executor(None, _w)
        if _WV_ACTIVE_SUBVIEW == name:
            _WV_ACTIVE_SUBVIEW = ""
        return {"ok": ok, "deleted": name}
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# UI REGISTRATION  —  injected as sub-section inside the Data Fabric panel
# (not a standalone top-level tab — the fabric panel hosts it via iframe)
# ─────────────────────────────────────────────────────────────────────────────

try:
    _orch.register_ui(
        "worldview",
        "WorldView",
        "◈",
        "",   # no standalone HTML needed — fabric_panel.html hosts the iframe
        "",
        ui_caps=[
            "worldview.train", "worldview.train_stage", "worldview.rebuild_index",
            "worldview.reembed_missing",
            "worldview.encode", "worldview.predict", "worldview.rollout",
            "worldview.counterfactual", "worldview.query", "worldview.anomalies",
            "worldview.snapshot", "worldview.concepts",
            "worldview.concept_neighbors", "worldview.concept_members",
            "worldview.concept_detail",
            "worldview.explain_record", "worldview.label_concepts",
            "worldview.stats",
            "worldview.config", "worldview.config_set",
            "worldview.stream.start", "worldview.stream.stop",
            "worldview.stream.status",
            "worldview.landscape", "worldview.detect_drift",
            "worldview.summarise",
            "worldview.persist", "worldview.load_from_fabric",
            "worldview.loss_history",
            "worldview.subview.list", "worldview.subview.create",
            "worldview.subview.activate", "worldview.subview.save",
            "worldview.subview.delete",
        ],
        mode="inject",   # fabric_panel.html hosts the WorldView iframe directly
        tab_order=55,
    )
except Exception as _e:
    log.warning("worldview register_ui failed: %s", _e)

# Schedule startup restore from fabric (runs once, 900 ms after module load)
try:
    schedule(_worldview_startup_load, 900, name="worldview_startup_load")
except Exception as _e:
    log.debug("worldview: could not schedule fabric startup load: %s", _e)

log.info("worldview_jepa.py v2 loaded — model=%s index=%s",
         "ready" if MODEL.ready else "disabled",
         "ready" if WV_INDEX.available else "disabled")