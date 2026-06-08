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

        x_src = x[src]
        W = self.edge_proj[edge_type]
        messages = torch.einsum('ei,eio->eo', x_src, W)

        agg = torch.zeros(N, out_dim, device=x.device, dtype=messages.dtype)
        counts = torch.zeros(N, device=x.device, dtype=messages.dtype)
        agg.index_add_(0, dst, messages)
        counts.index_add_(0, dst, torch.ones_like(dst, dtype=messages.dtype))
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

    # ── Stage 1: GNN contrastive training ──────────────────────────────────

    def train_gnn_step(self, x, src, dst, edge_type, pos_pairs, neg_pairs):
        if not self.ready:
            return 0.0
        self.gnn.train()
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
        x_t = torch.tensor(x, dtype=torch.float32, device=self.device)
        s_t = torch.tensor(src, dtype=torch.long, device=self.device)
        d_t = torch.tensor(dst, dtype=torch.long, device=self.device)
        e_t = torch.tensor(edge_type, dtype=torch.long, device=self.device)
        return self.gnn(x_t, s_t, d_t, e_t).cpu().numpy()

    @torch.no_grad()
    def encode_isolated(self, embedding):
        if not self.ready:
            return None
        self.gnn.eval()
        x = torch.tensor(np.asarray(embedding, dtype="float32"),
                          device=self.device).unsqueeze(0)
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
    return (sys.modules.get("data_fabric") or
            sys.modules.get("data_fabric"))


async def _fetch_records_with_embeddings(dataset_id: str = "",
                                          limit: int = MAX_NODES) -> Dict[str, Dict]:
    """Build {record_id: {dataset_id, embedding, text, created_at, tags}} from the fabric.

    Embeddings live in Chroma — NOT in Postgres or SQLite, which only store text/metadata.
    Strategy:
      1. Chroma is the authoritative source. Pull (id, embedding, metadata, document) directly.
      2. Optionally hydrate text/created_at/tags from SQLite if Chroma's metadata is sparse.
      3. As a last resort (e.g. Chroma missing), fall back to fabric.browse + embed-on-the-fly.
    """
    fab = _get_fabric()
    if not fab:
        log.warning("worldview: data_fabric module not loaded")
        return {}

    records: Dict[str, Dict] = {}

    # ── Primary path: Chroma ──────────────────────────────────────────────
    chroma = getattr(fab, "FABRIC_CHROMA", None)
    if chroma is not None and getattr(chroma, "available", False):
        try:
            loop = asyncio.get_running_loop()
            def _fetch_chroma():
                col = chroma._col
                if col is None:
                    return {}
                kwargs = {
                    "include": ["embeddings", "documents", "metadatas"],
                    "limit":   min(limit, 50000),
                }
                if dataset_id:
                    kwargs["where"] = {"dataset_id": {"$eq": dataset_id}}
                res = col.get(**kwargs)
                out = {}
                # CRITICAL: don't use `x or []` on Chroma returns — embeddings
                # is a numpy ndarray, which has ambiguous truthiness.
                ids   = res.get("ids")        if res.get("ids")        is not None else []
                embs  = res.get("embeddings") if res.get("embeddings") is not None else []
                docs  = res.get("documents")  if res.get("documents")  is not None else []
                metas = res.get("metadatas")  if res.get("metadatas")  is not None else []
                # Normalise lengths (np arrays support len() but iterate as rows)
                n_ids   = len(ids)
                n_embs  = len(embs)  if hasattr(embs, "__len__")  else 0
                n_docs  = len(docs)  if hasattr(docs, "__len__")  else 0
                n_metas = len(metas) if hasattr(metas, "__len__") else 0
                for i in range(n_ids):
                    rid = ids[i]
                    if i >= n_embs:
                        continue
                    emb = embs[i]
                    # emb might be a 1-D numpy array, list, or None
                    if emb is None:
                        continue
                    try:
                        if hasattr(emb, "tolist"):
                            emb_list = emb.tolist()
                        else:
                            emb_list = list(emb)
                    except Exception:
                        continue
                    if not isinstance(emb_list, list) or len(emb_list) < 10:
                        continue
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
                    out[rid] = {
                        "dataset_id": meta.get("dataset_id", "") or "",
                        "embedding":  emb_list,
                        "text":       (text or "")[:200],
                        "created_at": meta.get("created_at", "") or "",
                        "tags":       tags or [],
                    }
                return out
            records = await loop.run_in_executor(None, _fetch_chroma)
            log.info("worldview: fetched %d records from Chroma "
                     "(dataset=%s, limit=%d)",
                     len(records), dataset_id or "*", limit)
        except Exception as e:
            log.warning("worldview Chroma fetch failed: %s", e)

    # ── Hydrate sparse metadata from SQLite if needed ────────────────────
    if records:
        ids_to_hydrate = [rid for rid, r in records.items()
                          if not r.get("text") or not r.get("created_at")]
        if ids_to_hydrate:
            try:
                loop = asyncio.get_running_loop()
                def _hydrate():
                    conn = fab._sqlite_conn()
                    if not conn:
                        return {}
                    placeholders = ",".join("?" * len(ids_to_hydrate))
                    q = (f"SELECT id, dataset_id, text, created_at, tags "
                         f"FROM fabric_records WHERE id IN ({placeholders})")
                    out = {}
                    for row in conn.execute(q, ids_to_hydrate).fetchall():
                        tags = row[4]
                        if isinstance(tags, str):
                            try:
                                tags = json.loads(tags)
                            except Exception:
                                tags = []
                        out[row[0]] = {
                            "dataset_id": row[1],
                            "text":       (row[2] or "")[:200],
                            "created_at": row[3] or "",
                            "tags":       tags or [],
                        }
                    return out
                hydrated = await loop.run_in_executor(None, _hydrate)
                for rid, extra in hydrated.items():
                    rec = records.get(rid)
                    if not rec:
                        continue
                    if extra.get("dataset_id") and not rec.get("dataset_id"):
                        rec["dataset_id"] = extra["dataset_id"]
                    if extra.get("text") and not rec.get("text"):
                        rec["text"] = extra["text"]
                    if extra.get("created_at") and not rec.get("created_at"):
                        rec["created_at"] = extra["created_at"]
                    if extra.get("tags") and not rec.get("tags"):
                        rec["tags"] = extra["tags"]
            except Exception as e:
                log.debug("worldview SQLite hydrate: %s", e)

    # ── Fallback: fabric.browse + embed on the fly ───────────────────────
    # Only used if Chroma is unavailable or empty. This re-embeds text, which
    # is expensive — limited to one dataset to keep it tractable.
    if not records and dataset_id:
        log.info("worldview: Chroma empty, falling back to browse+embed for %s",
                 dataset_id)
        try:
            browse_cap = _orch.CAPABILITY_REGISTRY.get("fabric.browse")
            if browse_cap:
                res = await browse_cap["func"](
                    dataset_id=dataset_id, limit=min(limit, 2000),
                    offset=0, search="", lite=True,
                )
                _embed = fab._embed
                for rec in res.get("records", []):
                    text = rec.get("text", "")
                    if not text:
                        continue
                    emb = await _embed(text)
                    if emb and len(emb) > 10:
                        tags = rec.get("tags", [])
                        if isinstance(tags, str):
                            try:
                                tags = json.loads(tags)
                            except Exception:
                                tags = []
                        records[rec["id"]] = {
                            "dataset_id": dataset_id,
                            "embedding":  emb,
                            "text":       text[:200],
                            "created_at": rec.get("created_at", ""),
                            "tags":       tags or [],
                        }
                log.info("worldview: fallback path produced %d records", len(records))
        except Exception as e:
            log.warning("worldview browse+embed fallback: %s", e)

    # ── Trim to limit ─────────────────────────────────────────────────────
    if len(records) > limit:
        keep = list(records.keys())[:limit]
        records = {k: records[k] for k in keep}

    return records


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
        "fabric_module_loaded": False,
        "chroma_available":     False,
        "chroma_total_count":   0,
        "chroma_dataset_count": None if not dataset_id else 0,
        "sqlite_total_count":   0,
        "sqlite_dataset_count": None if not dataset_id else 0,
        "pg_available":         False,
        "dataset_id":           dataset_id or None,
        "datasets_seen":        [],
        "hint":                 "",
    }
    fab = _get_fabric()
    if not fab:
        diag["hint"] = ("data_fabric module not loaded — confirm worldview_jepa "
                        "is imported AFTER data_fabric at startup.")
        return diag
    diag["fabric_module_loaded"] = True

    # Chroma stats
    chroma = getattr(fab, "FABRIC_CHROMA", None)
    if chroma is not None and getattr(chroma, "available", False):
        diag["chroma_available"] = True
        try:
            loop = asyncio.get_running_loop()
            def _count():
                col = chroma._col
                if col is None:
                    return 0, 0
                total = col.count()
                ds_n = 0
                if dataset_id:
                    try:
                        r = col.get(where={"dataset_id": {"$eq": dataset_id}},
                                    include=[], limit=1)
                        # Chroma's get with limit=1 returns up to 1 id;
                        # use a larger limit to estimate
                        r2 = col.get(where={"dataset_id": {"$eq": dataset_id}},
                                     include=[])
                        ds_n = len(r2.get("ids") or [])
                    except Exception:
                        ds_n = 0
                return total, ds_n
            total, ds_n = await loop.run_in_executor(None, _count)
            diag["chroma_total_count"] = total
            diag["chroma_dataset_count"] = ds_n
        except Exception as e:
            diag["chroma_error"] = str(e)[:200]

    # SQLite stats
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
            datasets = [r[0] for r in conn.execute(
                "SELECT DISTINCT dataset_id FROM fabric_records LIMIT 50"
            ).fetchall()]
            return total, ds_n, datasets
        total, ds_n, datasets = await loop.run_in_executor(None, _sqlite_count)
        diag["sqlite_total_count"] = total
        diag["sqlite_dataset_count"] = ds_n
        diag["datasets_seen"] = datasets
    except Exception as e:
        diag["sqlite_error"] = str(e)[:200]

    # PG availability (records have no embeddings there but worth reporting)
    pg = getattr(fab, "FABRIC_PG", None)
    diag["pg_available"] = bool(pg and getattr(pg, "available", False))

    # Generate a useful hint based on what we found
    if not diag["chroma_available"]:
        diag["hint"] = ("Chroma is not connected — embeddings live there. "
                        "Check CHROMA_HOST/CHROMA_PORT and ensure the Chroma "
                        "server is reachable. Fabric records may exist in "
                        "SQLite/Postgres but worldview needs the vectors.")
    elif diag["chroma_total_count"] == 0:
        if diag["sqlite_total_count"] == 0:
            diag["hint"] = ("Fabric is empty — ingest some data first via "
                            "the fabric panel (sources / web acquisition).")
        else:
            diag["hint"] = (f"SQLite has {diag['sqlite_total_count']} records but "
                            "Chroma is empty. The embedding pipeline stage may "
                            "have failed during ingest. Re-run ingest with "
                            "Chroma online, or use the browse+embed fallback by "
                            "specifying a dataset_id (slow).")
    elif dataset_id and diag["chroma_dataset_count"] == 0:
        diag["hint"] = (f"Dataset '{dataset_id}' has no embeddings in Chroma. "
                        f"Datasets seen in SQLite: {diag['datasets_seen'][:10]}. "
                        "Check the dataset name spelling or pick one from the list.")
    else:
        diag["hint"] = ("Chroma has data but worldview's fetch returned nothing. "
                        "This may be a metadata-filter mismatch. Check server "
                        "logs for 'worldview Chroma fetch failed'.")

    return diag


async def build_graph(dataset_id: str = "", limit: int = MAX_NODES) -> Dict:
    """Build full training subgraph."""
    records = await _fetch_records_with_embeddings(dataset_id, limit)
    if not records:
        return {"records": {}, "rids": [], "X": None}

    rids = list(records.keys())
    idx_of = {rid: i for i, rid in enumerate(rids)}
    X = np.array([records[r]["embedding"] for r in rids], dtype="float32")
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    X = X / norms

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

    log.info("WorldView graph: %d nodes, %d edges", len(rids), len(edges))
    return {
        "records": records, "idx_of": idx_of, "rids": rids,
        "X": X, "src": src, "dst": dst, "edge_type": edge_type,
        "edges": edges,
    }


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
        x_t = torch.tensor(graph["X"], device=model.device)
        s_t = torch.tensor(graph["src"], device=model.device)
        d_t = torch.tensor(graph["dst"], device=model.device)
        e_t = torch.tensor(graph["edge_type"], device=model.device)
        latents = model.gnn(x_t, s_t, d_t, e_t)
        concept_idx = model.codebook.assign(latents).cpu().numpy()

    model.record_concepts = {rids[i]: int(concept_idx[i]) for i in range(N)}

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


async def _rebuild_index_from_graph(graph: Dict) -> int:
    if not MODEL.ready or not WV_INDEX.available:
        return 0
    WV_INDEX.connect()
    latents = MODEL.encode_subgraph(
        graph["X"], graph["src"], graph["dst"], graph["edge_type"],
    )
    if latents is None:
        return 0
    ids = graph["rids"]
    metas = []
    for rid in ids:
        r = graph["records"][rid]
        c = MODEL.record_concepts.get(rid, -1)
        metas.append({
            "dataset_id": r["dataset_id"],
            "text":       r["text"][:120],
            "concept":    c,
            "concept_label": MODEL.concept_labels.get(c, ""),
        })
    WV_INDEX.add_batch(ids, latents, metas)
    return len(ids)


@capability(
    "worldview.train",
    http_method="POST", http_path="/worldview/train",
    http_tags=["worldview", "jepa", "ml"],
    memory="off", streams=["worldview.progress"],
    description="Run full 3-stage WorldView training: GNN → codebook → dynamics. "
                "Input: dataset_id (str, optional), gnn_epochs (int, default 20), "
                "codebook_epochs (int, default 8), dynamics_epochs (int, default 15), "
                "limit (int, default 20000). "
                "Output: {ok, stages, nodes, edges, indexed, duration_s}.",
)
async def cap_worldview_train(
    dataset_id:       str = "",
    gnn_epochs:       int = 20,
    codebook_epochs:  int = 8,
    dynamics_epochs:  int = 15,
    limit:            int = MAX_NODES,
    trace_id=None,
) -> Dict:
    if not MODEL.ready:
        return {"error": "PyTorch not available", "hint": "pip install torch"}

    t0 = time.time()
    await _emit("building_graph", message="Fetching records and edges...")

    graph = await build_graph(dataset_id, limit=limit)
    if not graph.get("rids"):
        diag = await _diagnose_no_records(dataset_id)
        return {"error": "No records with embeddings found",
                "diagnostic": diag,
                "hint": diag.get("hint", "Ingest data into the fabric first")}

    N = len(graph["rids"])
    E = len(graph["edges"])
    await _emit("graph_ready", nodes=N, edges=E,
                message=f"Graph: {N} nodes, {E} edges")

    # Stage 1
    if gnn_epochs > 0:
        await _emit("stage_gnn", message="Training graph encoder...")
        for epoch in range(gnn_epochs):
            pos, neg = sample_pairs(graph, n_pairs=min(1024, len(graph["edges"])))
            if not pos:
                break
            loss = MODEL.train_gnn_step(
                graph["X"], graph["src"], graph["dst"], graph["edge_type"],
                pos, neg,
            )
            if (epoch + 1) % max(1, gnn_epochs // 10) == 0 or epoch == gnn_epochs - 1:
                await _emit("gnn_epoch", epoch=epoch + 1, total=gnn_epochs,
                             loss=round(loss, 6))

    # Stage 2
    if codebook_epochs > 0:
        await _emit("stage_codebook", message="Fitting concept codebook...")
        for epoch in range(codebook_epochs):
            loss = MODEL.train_codebook_step(
                graph["X"], graph["src"], graph["dst"], graph["edge_type"],
            )
            if (epoch + 1) % max(1, codebook_epochs // 4) == 0 or epoch == codebook_epochs - 1:
                cb_stats = MODEL.codebook.usage_stats()
                await _emit("codebook_epoch", epoch=epoch + 1, total=codebook_epochs,
                             loss=round(loss, 6),
                             active=cb_stats["active_concepts"],
                             entropy=cb_stats["entropy"])

    # Stage 3
    if dynamics_epochs > 0:
        await _emit("stage_dynamics", message="Generating walks...")
        walks = generate_walks(graph, MODEL,
                                n_walks=min(MAX_WALKS, N * 4), walk_len=WALK_LEN)
        if walks:
            await _emit("dynamics_walks", walks=len(walks),
                         message=f"{len(walks)} walks generated")
            for epoch in range(dynamics_epochs):
                random.shuffle(walks)
                losses = []
                for i in range(0, len(walks), BATCH_SIZE):
                    batch = walks[i:i + BATCH_SIZE]
                    if len(batch) < 2:
                        continue
                    l = MODEL.train_dynamics_step(batch)
                    losses.append(l)
                if losses and ((epoch + 1) % max(1, dynamics_epochs // 8) == 0
                               or epoch == dynamics_epochs - 1):
                    await _emit("dynamics_epoch", epoch=epoch + 1,
                                 total=dynamics_epochs,
                                 loss=round(sum(losses) / len(losses), 6))

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

    latent = MODEL.encode_isolated(emb)
    if latent is None:
        return {"error": "Encoding failed"}
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
                latent = MODEL.encode_isolated(emb)
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
                latent = MODEL.encode_isolated(emb)
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
    latent = MODEL.encode_isolated(emb)
    results = WV_INDEX.search(latent, top_k=top_k)
    out = []
    for rid, score in results:
        meta = WV_INDEX._meta.get(rid, {})
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

    latents = MODEL.encode_subgraph(
        graph["X"], graph["src"], graph["dst"], graph["edge_type"],
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
        meta = WV_INDEX._meta.get(rid, {})
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
            sample = WV_INDEX._meta.get(rid, {})
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
        meta = WV_INDEX._meta.get(rid, {})
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
            meta = WV_INDEX._meta.get(rid, {})
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
            meta = WV_INDEX._meta.get(rid, {})
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
            "worldview.train", "worldview.train_stage", "worldview.encode",
            "worldview.predict", "worldview.rollout", "worldview.counterfactual",
            "worldview.query", "worldview.anomalies", "worldview.snapshot",
            "worldview.concepts", "worldview.concept_neighbors",
            "worldview.concept_members", "worldview.explain_record",
            "worldview.label_concepts", "worldview.stats",
        ],
        mode="tab",
        tab_order=55,
    )
except Exception as _e:
    log.warning("worldview register_ui failed: %s", _e)

log.info("worldview_jepa.py v2 loaded — model=%s index=%s",
         "ready" if MODEL.ready else "disabled",
         "ready" if WV_INDEX.available else "disabled")