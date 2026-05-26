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
import json
import logging
import math
import os
import random
import time
from collections import defaultdict, Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    capability, emit_event, now_iso, schedule, ollama_generate, register_ui,
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
# VQ CODEBOOK  —  Vector-Quantised concept book with EMA updates
# ─────────────────────────────────────────────────────────────────────────────

class ConceptBook(nn.Module):
    """VQ-VAE codebook with EMA updates and k-means++ initialisation."""
    def __init__(self, num_concepts: int, dim: int, decay: float = 0.99,
                 commitment: float = 0.25):
        super().__init__()
        self.num_concepts = num_concepts
        self.dim = dim
        self.decay = decay
        self.commitment = commitment

        self.register_buffer("codebook", torch.randn(num_concepts, dim) * 0.1)
        self.register_buffer("ema_cluster_size", torch.zeros(num_concepts))
        self.register_buffer("ema_w", self.codebook.clone())
        self.register_buffer("initialised", torch.zeros(1))

    @torch.no_grad()
    def _init_codebook(self, z):
        n = z.size(0)
        if n < self.num_concepts:
            idx = torch.randint(0, n, (self.num_concepts,), device=z.device)
            self.codebook.copy_(z[idx])
        else:
            # k-means++ init for spread centroids
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
        self.initialised.fill_(1.0)

    def forward(self, z):
        if self.initialised.item() < 0.5:
            self._init_codebook(z.detach())

        d = (z.pow(2).sum(1, keepdim=True)
             + self.codebook.pow(2).sum(1).unsqueeze(0)
             - 2 * z @ self.codebook.t())
        indices = d.argmin(dim=1)
        z_q = self.codebook[indices]

        if self.training:
            with torch.no_grad():
                one_hot = F.one_hot(indices, self.num_concepts).type(z.dtype)
                cluster_size = one_hot.sum(dim=0)
                self.ema_cluster_size.mul_(self.decay).add_(
                    cluster_size, alpha=1 - self.decay)
                dw = one_hot.t() @ z
                self.ema_w.mul_(self.decay).add_(dw, alpha=1 - self.decay)
                n = self.ema_cluster_size.sum()
                smoothed = ((self.ema_cluster_size + 1e-5)
                            / (n + self.num_concepts * 1e-5) * n)
                self.codebook.copy_(self.ema_w / smoothed.unsqueeze(1))

        commitment_loss = F.mse_loss(z, z_q.detach())
        vq_loss = self.commitment * commitment_loss
        z_q_st = z + (z_q - z).detach()
        return z_q_st, indices, vq_loss

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
                        "max_entropy": round(float(math.log(self.num_concepts)), 4)}
            p = sizes / sizes.sum()
            p_nz = p[p > 0]
            entropy = float(-(p_nz * np.log(p_nz + 1e-12)).sum())
            return {
                "active_concepts": int((sizes > 1e-3).sum()),
                "dead_concepts":   int((sizes <= 1e-3).sum()),
                "max_population":  float(sizes.max()),
                "min_population":  float(sizes.min()),
                "entropy":         round(entropy, 4),
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

        self.opt_gnn = torch.optim.AdamW(self.gnn.parameters(),
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
        self.opt_gnn = torch.optim.AdamW(self.gnn.parameters(),
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

    # ── Stage 1: GNN contrastive training ──────────────────────────────────

    def train_gnn_step(self, x, src, dst, edge_type, pos_pairs, neg_pairs):
        if not self.ready:
            return 0.0
        self.gnn.train()
        # Accept pre-built tensors or numpy arrays
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
        h = F.normalize(h, dim=-1)

        pp = torch.tensor(pos_pairs, dtype=torch.long, device=self.device)
        np_ = torch.tensor(neg_pairs, dtype=torch.long, device=self.device)
        pos_sim = (h[pp[:, 0]] * h[pp[:, 1]]).sum(-1)
        neg_sim = (h[np_[:, 0]] * h[np_[:, 1]]).sum(-1)

        margin = 0.2
        loss = F.relu(margin - pos_sim + neg_sim).mean()

        # Uniformity (prevent collapse): -log mean exp(-2 * dist^2)
        sub = h[:min(64, h.size(0))]
        if sub.size(0) > 1:
            uniformity = -torch.pdist(sub).pow(2).mul(-2).exp().mean().log()
            total = loss + 0.1 * uniformity
        else:
            total = loss

        self.opt_gnn.zero_grad()
        total.backward()
        torch.nn.utils.clip_grad_norm_(self.gnn.parameters(), 1.0)
        self.opt_gnn.step()
        self.train_steps["gnn"] += 1
        lv = float(total.item())
        self.train_loss["gnn"] = 0.95 * self.train_loss["gnn"] + 0.05 * lv
        return lv

    # ── Stage 2: Codebook training ─────────────────────────────────────────

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
        _, _, vq_loss = self.codebook(h)
        self.train_steps["codebook"] += 1
        lv = float(vq_loss.item())
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

    def save(self, path=None):
        if not self.ready:
            return
        path = path or DEFAULT_CKPT
        try:
            torch.save({
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
            }, str(path))
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
            ckpt = torch.load(str(path), map_location=self.device,
                              weights_only=False)
            cfg_ = ckpt.get("config", {})
            if cfg_.get("num_concepts", self.num_concepts) != self.num_concepts:
                log.warning("WorldView checkpoint has different K — skipping load")
                return False
            # If the saved checkpoint was trained on a different embedding
            # dim, rebuild the GNN to match before loading state_dicts.
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
            log.info("WorldView loaded ← %s (gnn=%d cb=%d dyn=%d steps)",
                     path, self.train_steps["gnn"],
                     self.train_steps["codebook"], self.train_steps["dynamics"])
            return True
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
# GRAPH BUILDER  —  fetch records + edges from the fabric
# ─────────────────────────────────────────────────────────────────────────────

def _get_fabric():
    import sys
    return (sys.modules.get("Vera.Orchestration.data_fabric") or
            sys.modules.get("data_fabric"))


async def _fetch_records_with_embeddings(dataset_id: str = "",
                                          limit: int = MAX_NODES):
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
                if dataset_id:
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


async def build_graph(dataset_id: str = "", limit: int = MAX_NODES) -> Dict:
    """Build full training subgraph."""
    rids, emb_np, records = await _fetch_records_with_embeddings(dataset_id, limit)
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
    if not MODEL.ready or not WV_INDEX.available:
        return 0
    WV_INDEX.connect()
    latents = MODEL.encode_subgraph(
        graph.get("X_t", graph.get("X")),
        graph.get("s_t", graph.get("src")),
        graph.get("d_t", graph.get("dst")),
        graph.get("e_t", graph.get("edge_type")),
    )
    if latents is None:
        return 0
    ids = graph["rids"]
    metas = []
    # Populate MODEL.record_meta in addition to the FAISS index metadata,
    # so that concept inspection (members, neighbors) works even after a
    # restart, before the FAISS index has been rebuilt.
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
    WV_INDEX.add_batch(ids, latents, metas)
    return len(ids)


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

    If instance_url is given, only that instance is tried. Otherwise picks
    the best online instance with the model loaded, and falls over to
    siblings on failure — matching the cluster pattern used by ollama_generate.

    Emits ollama.request / ollama.request_done / ollama.request_error events
    so every embed call appears in the Workers panel Jobs tab.

    Returns (vector_or_None, error_reason, instance_used).
    """
    import uuid as _uuid
    import time as _time

    if not text or not text.strip():
        return None, "no_text", ""

    req_id = str(_uuid.uuid4())[:12]
    t_start = _time.time()
    text_preview = (text or "")[:120].replace("\n", " ")

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
    for iid, url in instances_to_try:
        try:
            # /api/embed (newer) → /api/embeddings (older) fallback
            r = await client.post(
                f"{url}/api/embed",
                json={"model": ollama_model, "input": text[:4096]},
                timeout=60.0,
            )
            if r.status_code != 200:
                r = await client.post(
                    f"{url}/api/embeddings",
                    json={"model": ollama_model, "prompt": text[:4096]},
                    timeout=60.0,
                )
            if r.status_code != 200:
                last_err = f"http_{r.status_code}"
                continue  # try next instance
            data = r.json()
            emb = data.get("embeddings")
            if emb and isinstance(emb, list):
                vec = emb[0] if isinstance(emb[0], list) else emb
            else:
                vec = data.get("embedding")
            if not vec or len(vec) < 10:
                last_err = "no_vector"
                continue
            # Emit success event
            elapsed = round(_time.time() - t_start, 2)
            try:
                await emit_event({
                    "type": "ollama.request_done", "req_id": req_id,
                    "model": ollama_model, "instance_id": iid,
                    "caller_file": "worldview_jepa.py",
                    "caller_func": "_embed_direct",
                    "elapsed_s": elapsed, "dimensions": len(vec),
                })
            except Exception:
                pass
            return list(vec), "", iid
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


async def _embed_and_upsert_batch(records: List[Dict],
                                    max_concurrent: int = 32
                                    ) -> Tuple[int, int, Dict[str, int], Dict[str, int]]:
    """Embed records via the centralized ollama_embed (concurrent) and upsert into Chroma.

    Distributes work across ALL online Ollama instances in round-robin fashion
    so the full cluster is utilised, rather than piling onto whichever node
    pick_instance selects first.

    Returns (embedded_count, upserted_count, error_breakdown, instances_used).
    """
    from Vera.Orchestration.capability_orchestration import ollama_embed as _ollama_embed

    fab = _get_fabric()
    if not fab:
        return 0, 0, {"no_fabric_module": len(records)}, {}
    chroma = getattr(fab, "FABRIC_CHROMA", None)
    DataRecord = getattr(fab, "DataRecord", None)
    if not (chroma and DataRecord and getattr(chroma, "available", False)):
        return 0, 0, {"chroma_unavailable": len(records)}, {}

    # Reset the fabric's circuit-breaker so any other fabric code paths that
    # use _embed start working again too.
    try:
        fab._embed_failed = False
    except Exception:
        pass

    ollama_model = _orch.OLLAMA_EMBED_MODEL
    if not ollama_model:
        return 0, 0, {"ollama_model_missing": len(records)}, {}

    # Build list of online instances for round-robin distribution
    try:
        online_ids = [iid for iid, i in _orch.OLLAMA_INSTANCES.items()
                      if i.get("status") == "online"]
    except Exception:
        online_ids = []
    if not online_ids:
        return 0, 0, {"no_online_instances": len(records)}, {}

    errors: Dict[str, int] = defaultdict(int)
    instances_used: Dict[str, int] = defaultdict(int)
    sem = asyncio.Semaphore(max_concurrent)

    async def _embed_one(rec, assigned_instance):
        async with sem:
            # Pin to the assigned instance so load spreads evenly
            vec = await _ollama_embed(rec["text"], model=ollama_model,
                                       instance_id=assigned_instance, timeout=60)
            if vec is not None:
                instances_used[assigned_instance] = instances_used.get(assigned_instance, 0) + 1
                return rec, vec
            errors["embed_failed"] += 1
            return rec, None

    # Round-robin assign records to instances
    tasks = []
    for i, rec in enumerate(records):
        assigned = online_ids[i % len(online_ids)]
        tasks.append(_embed_one(rec, assigned))

    embedded = await asyncio.gather(*tasks)

    successful = [(r, e) for r, e in embedded if e is not None]
    if not successful:
        return 0, 0, dict(errors), dict(instances_used)

    # Upsert in a worker thread (Chroma client is sync)
    loop = asyncio.get_running_loop()
    def _upsert():
        n = 0
        upsert_errors = []
        for rec, emb in successful:
            try:
                dr = DataRecord(
                    id=rec["id"], dataset_id=rec["dataset_id"],
                    text=rec["text"], embedding=emb,
                    tags=rec.get("tags", []),
                    created_at=rec.get("created_at", ""),
                )
                ok = chroma.upsert(dr)
                if ok:
                    n += 1
                else:
                    upsert_errors.append("upsert_returned_false")
            except Exception as e:
                upsert_errors.append(f"upsert_exception:{type(e).__name__}")
        return n, upsert_errors
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
                "their text via Ollama, and upsert into Chroma. Useful when "
                "ingest's embed stage was disabled or Chroma was down. "
                "Input: dataset_id (str, optional — empty = all), "
                "limit (int, default 5000 — per call), "
                "batch_size (int, default 32 — concurrent embeds), "
                "dry_run (bool, default false), "
                "force (bool, default false — if true, resets Chroma on dimension mismatch). "
                "Output: {ok, missing_found, embedded, upserted, datasets_processed}.",
)
async def cap_worldview_reembed_missing(
    dataset_id: str = "",
    limit:      int = 5000,
    batch_size: int = 32,
    dry_run:    bool = False,
    force:      bool = False,
    trace_id=None,
) -> Dict:
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
    BATCH = batch_size
    total_embedded = 0
    total_upserted = 0
    total_errors: Dict[str, int] = defaultdict(int)
    total_instances: Dict[str, int] = defaultdict(int)
    for i in range(0, len(missing), BATCH):
        chunk = missing[i:i + BATCH]
        emb_n, ups_n, batch_errors, batch_instances = await _embed_and_upsert_batch(
            chunk, max_concurrent=batch_size,
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
    limit:            int = MAX_NODES,
    embed_missing:    bool = False,
    trace_id=None,
) -> Dict:
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

    await _emit("building_graph", message="Fetching records and edges...")

    graph = await build_graph(dataset_id, limit=limit)
    if not graph.get("rids"):
        diag = await _diagnose_no_records(dataset_id)
        return {"error": "No records with embeddings found",
                "diagnostic": diag,
                "hint": diag.get("hint", "Ingest data into the fabric first")}

    # Force GC after build_graph — the Chroma fetch runs in an executor
    # thread and can leave large temporary objects (response dicts, numpy
    # intermediaries) in gen-1/gen-2 that won't be collected until a gen-2
    # sweep, which might not happen before training allocates more memory.
    import gc
    gc.collect()

    N = len(graph["rids"])
    E = len(graph["edges"])
    # Get X reference — could be tensor or numpy depending on torch availability
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
                message=f"Graph: {N} nodes, {E} edges, embed_dim={actual_dim}",
                elapsed_s=round(time.time() - t0, 1))

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
        for epoch in range(gnn_epochs):
            try:
                pos, neg = sample_pairs(graph, n_pairs=min(1024, len(graph["edges"])))
                if not pos:
                    await _emit("gnn_epoch", epoch=epoch + 1, total=gnn_epochs,
                                 loss=0, message="No positive pairs — skipping")
                    break
                loss = MODEL.train_gnn_step(g_x, g_s, g_d, g_et, pos, neg)
                ep_rate = (epoch + 1) / max(time.time() - t_stage, 0.01)
                # Emit every epoch for UI progress
                await _emit("gnn_epoch", epoch=epoch + 1, total=gnn_epochs,
                             loss=round(loss, 6),
                             elapsed_s=round(time.time() - t0, 1),
                             epoch_rate=round(ep_rate, 2),
                             message=f"GNN epoch {epoch+1}/{gnn_epochs} loss={loss:.4f} ({ep_rate:.1f} ep/s)")
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
        for epoch in range(codebook_epochs):
            try:
                loss = MODEL.train_codebook_step(g_x, g_s, g_d, g_et)
                cb_stats = MODEL.codebook.usage_stats()
                ep_rate = (epoch + 1) / max(time.time() - t_stage, 0.01)
                await _emit("codebook_epoch", epoch=epoch + 1, total=codebook_epochs,
                             loss=round(loss, 6),
                             active=cb_stats["active_concepts"],
                             entropy=cb_stats["entropy"],
                             elapsed_s=round(time.time() - t0, 1),
                             epoch_rate=round(ep_rate, 2),
                             message=f"Codebook epoch {epoch+1}/{codebook_epochs} "
                                     f"loss={loss:.4f} concepts={cb_stats['active_concepts']} ({ep_rate:.1f} ep/s)")
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
        try:
            walks = generate_walks(graph, MODEL,
                                    n_walks=min(MAX_WALKS, N * 4), walk_len=WALK_LEN)
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
            for epoch in range(dynamics_epochs):
                try:
                    random.shuffle(walks)
                    losses = []
                    for i in range(0, len(walks), BATCH_SIZE):
                        batch = walks[i:i + BATCH_SIZE]
                        if len(batch) < 2:
                            continue
                        l = MODEL.train_dynamics_step(batch)
                        losses.append(l)
                    avg_loss = sum(losses) / len(losses) if losses else 0
                    ep_rate = (epoch + 1) / max(time.time() - t_stage, 0.01)
                    await _emit("dynamics_epoch", epoch=epoch + 1,
                                 total=dynamics_epochs,
                                 loss=round(avg_loss, 6),
                                 elapsed_s=round(time.time() - t0, 1),
                                 epoch_rate=round(ep_rate, 2),
                                 message=f"Dynamics epoch {epoch+1}/{dynamics_epochs} loss={avg_loss:.4f} ({ep_rate:.1f} ep/s)")
                    await asyncio.sleep(0)
                except Exception as e:
                    log.error("Dynamics epoch %d FAILED: %s", epoch + 1, e, exc_info=True)
                    await _emit("dynamics_error", epoch=epoch + 1, error=str(e)[:200],
                                 message=f"Dynamics epoch {epoch+1} failed: {e}")
                    break
        gc.collect()

    await _emit("indexing", message="Rebuilding FAISS index...")
    indexed = await _rebuild_index_from_graph(graph)

    MODEL.save()
    duration = round(time.time() - t0, 1)
    await _emit("done", message="Training complete",
                duration=duration, indexed=indexed,
                stages={k: MODEL.train_steps[k] for k in MODEL.train_steps})

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
        fab = _get_fabric()
        if fab:
            emb = await fab._embed(text)
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
        fab = _get_fabric()
        if fab:
            emb = await fab._embed(text)
            if emb:
                dim_err = _dim_mismatch_error(emb)
                if dim_err:
                    return dim_err
                latent = MODEL.encode_isolated(emb)
                if latent is not None:
                    start_c = MODEL.assign_concept(latent)
    if start_c < 0:
        return {"error": "No starting concept resolved"}

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
        fab = _get_fabric()
        if fab:
            emb = await fab._embed(text)
            if emb:
                dim_err = _dim_mismatch_error(emb)
                if dim_err:
                    return dim_err
                latent = MODEL.encode_isolated(emb)
                if latent is not None:
                    start_c = MODEL.assign_concept(latent)
    if start_c < 0:
        return {"error": "No starting concept resolved"}

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
    trace_id=None,
) -> Dict:
    if not MODEL.ready or not WV_INDEX.available:
        return {"error": "WorldView not ready"}
    fab = _get_fabric()
    if not (text and fab):
        return {"error": "Provide text"}
    emb = await fab._embed(text)
    if not emb:
        return {"error": "Embedding failed"}
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
        out.append({
            "id":            rid,
            "score":         round(score, 5),
            "dataset_id":    meta.get("dataset_id", ""),
            "text":          meta.get("text", ""),
            "concept":       meta.get("concept", -1),
            "concept_label": meta.get("concept_label", ""),
        })
    return {"results": out, "query": text[:200]}


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

    graph = await build_graph(dataset_id, limit=5000)
    if not graph.get("rids"):
        diag = await _diagnose_no_records(dataset_id)
        return {"error": "No records with embeddings found",
                "diagnostic": diag}

    actual_dim = graph.get("X_t", graph.get("X")).shape[1]
    if actual_dim != MODEL.embed_dim:
        return {"error": f"Embedding dim mismatch: data has {actual_dim} but "
                         f"model was trained on {MODEL.embed_dim}. "
                         "Run worldview.train to retrain on current data.",
                "actual_dim": actual_dim,
                "model_dim":  MODEL.embed_dim}

    latents = MODEL.encode_subgraph(
        graph.get("X_t", graph.get("X")),
        graph.get("s_t", graph.get("src")),
        graph.get("d_t", graph.get("dst")),
        graph.get("e_t", graph.get("edge_type")),
    )
    if latents is None:
        return {"error": "Encoding failed"}

    with torch.no_grad():
        z = torch.tensor(latents, device=MODEL.device)
        concepts = MODEL.codebook.assign(z)
        cb = MODEL.codebook.codebook[concepts]
        recon = (z - cb).pow(2).sum(-1).cpu().numpy()

    by_ds = defaultdict(list)
    rids = graph["rids"]
    for i, rid in enumerate(rids):
        r = graph["records"][rid]
        by_ds[r["dataset_id"]].append((r.get("created_at", ""), rid, int(concepts[i].item())))

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
    for i, rid in enumerate(rids):
        recon_score = float(recon[i]) / recon_max
        lp = record_logp.get(rid, 0.0)
        lp_score = max(0.0, -lp) / 8.0
        combined = 0.6 * recon_score + 0.4 * lp_score
        anomalies.append({
            "id":             rid,
            "dataset_id":     graph["records"][rid]["dataset_id"],
            "text":           graph["records"][rid]["text"][:200],
            "concept":        int(concepts[i].item()),
            "concept_label":  MODEL.concept_labels.get(int(concepts[i].item()), ""),
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
    if not WV_INDEX.available or not HAS_NUMPY:
        return {"error": "WorldView index not ready"}

    ids, vecs = WV_INDEX.get_all_vectors()
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
    if MODEL.ready and method == "pca" and Vt is not None:
        with torch.no_grad():
            cb = MODEL.codebook.codebook.cpu().numpy()
        cb_c = cb - mean_vec
        cb_proj = cb_c @ Vt[:2].T
        cb_n = (cb_proj - mins) / rng
        sizes = MODEL.codebook.ema_cluster_size.cpu().numpy()
        active = sizes > 1e-3
        for i in range(len(cb)):
            if not active[i]:
                continue
            concept_points.append({
                "idx":   i,
                "x":     round(float(cb_n[i, 0]), 5),
                "y":     round(float(cb_n[i, 1]), 5),
                "size":  float(sizes[i]),
                "label": MODEL.concept_labels.get(i, ""),
            })

    return {
        "points":   points,
        "concepts": concept_points,
        "method":   method,
        "count":    len(points),
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
    trace_id=None,
) -> Dict:
    if not MODEL.ready:
        return {"error": "WorldView not ready"}

    pop = Counter(MODEL.record_concepts.values())
    if concepts:
        target = [c for c in concepts if c in pop]
    else:
        target = [c for c, _ in pop.most_common() if c not in MODEL.concept_labels]
    target = target[:max_concepts]
    if not target:
        return {"labelled": [], "message": "Nothing to label"}

    await _emit("labelling", message=f"Labelling {len(target)} concepts...",
                 total=len(target))

    members_by = defaultdict(list)
    for rid, c in MODEL.record_concepts.items():
        if c in target and len(members_by[c]) < 8:
            meta = _meta_for(rid)
            t = meta.get("text", "")
            if t:
                members_by[c].append(t[:160])

    labelled = []
    for i, c in enumerate(target):
        samples = members_by.get(c, [])
        if not samples:
            continue
        prompt = (
            "Below are short text samples that have been clustered together "
            "by an unsupervised model. Suggest a SHORT (2-5 word) descriptive "
            "label for this cluster — what they have in common.\n\n"
            "Samples:\n"
            + "\n".join(f"- {s}" for s in samples)
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
            log.debug("label %d: %s", c, e)

    MODEL.save()
    await _emit("label_done", labelled=len(labelled))
    return {"labelled": labelled, "total_existing": len(MODEL.concept_labels)}


@capability(
    "worldview.stats",
    http_method="GET", http_path="/worldview/stats",
    http_tags=["worldview"], memory="off",
    description="Full WorldView stats. Output: {model, index}.",
)
async def cap_worldview_stats(trace_id=None) -> Dict:
    return {"model": MODEL.stats(), "index": WV_INDEX.stats()}


# ─────────────────────────────────────────────────────────────────────────────
# UI PANEL  —  iframe-mounted via the standard register_ui() pattern
# ─────────────────────────────────────────────────────────────────────────────

_WV_MOUNT_JS = r"""
(function mountWorldViewPanel() {
  var mount = document.getElementById('panel-wv');
  if (!mount || mount._wvMounted) return;
  mount._wvMounted = true;
  var frame = document.createElement('iframe');
  var backendBase = (document.getElementById('backendUrl') || {}).value || '';
  backendBase = backendBase.replace(/\/$/, '') || window._veraBase || 'http://llm.int:8999';
  frame.src = backendBase + '/ui/panels/file/worldview_panel.html';
  frame.style.cssText = 'width:100%;height:100%;border:none;display:block;background:#181614';
  frame.allow = 'clipboard-read; clipboard-write';
  mount.appendChild(frame);
  var urlInput = document.getElementById('backendUrl');
  if (urlInput) {
    urlInput.addEventListener('change', function() {
      try { frame.contentWindow.postMessage({type:'vera:base', url: urlInput.value.replace(/\/$/, '')}, '*'); } catch(_) {}
    });
  }
})();
"""

try:
    _orch.register_ui(
        "worldview",
        "WorldView",
        "",
        '<div id="panel-wv" style="height:100%;overflow:hidden;background:var(--bg0)"></div>',
        _WV_MOUNT_JS,
        ui_caps=[
            "worldview.train", "worldview.train_stage", "worldview.rebuild_index",
            "worldview.reembed_missing",
            "worldview.encode", "worldview.predict", "worldview.rollout",
            "worldview.counterfactual", "worldview.query", "worldview.anomalies",
            "worldview.snapshot", "worldview.concepts",
            "worldview.concept_neighbors", "worldview.concept_members",
            "worldview.explain_record", "worldview.label_concepts",
            "worldview.stats",
        ],
        mode="tab",
        tab_order=55,
    )
except Exception as _e:
    log.warning("worldview register_ui failed: %s", _e)

log.info("worldview_jepa.py v2 loaded — model=%s index=%s",
         "ready" if MODEL.ready else "disabled",
         "ready" if WV_INDEX.available else "disabled")