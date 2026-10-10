"""Held-out next-concept evaluation and the dynamics step cap for the JEPA worldview (four-minds FM-8.2).

The health verdict (worldview_health_core) cannot say "ok" without a held-out next-concept evaluation against a
baseline, and it calls the model "memorising" above 20 dynamics steps per observed transition. Before FM-8.2 the
training had neither: every walk trained the dynamics, nothing was held back, and the epochs ran whatever was asked
(40 steps per transition on 2026-10-10). This module gives the training:

- split_walks: a fixed share of the walks held back from training (every n-th walk - deterministic);
- step_cap: how many dynamics epochs keep the model under the steps-per-transition ceiling;
- baselines: unigram and bigram next-token log-loss / accuracy on the held-out walks, fitted on the training walks
  (add-one smoothing) - the bar FM-8.4 pre-registers (bigram included: a first-order walk hands it a strong case);
- verdict: the model beats the baseline only if its held-out log-loss is below BOTH, on enough tokens.

Walks are token lists as the dynamics model reads them: TOKEN_BOS, then concept + NUM_SPECIAL_TOKENS; a position's
target is the next token (the end-of-sequence token is padding and never a target).

Pure: no torch, no I/O.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple

HELD_OUT = 0.2
MAX_STEPS_PER_TRANSITION = 20.0       # worldview_health_core's ceiling
CAP_MARGIN = 0.9                      # stay under it, not on it
MIN_HELD_OUT_TOKENS = 200             # below this the comparison says nothing


def split_walks(walks: Sequence[Sequence[int]], held_out: float = HELD_OUT) -> Tuple[List, List]:
    """(train, test): every n-th walk is held back, n = round(1 / held_out). Nothing is held back from fewer than
    n walks."""
    walks = list(walks)
    if not walks or held_out <= 0:
        return walks, []
    n = max(2, int(round(1.0 / min(held_out, 0.5))))
    if len(walks) < n:
        return walks, []
    test = [w for i, w in enumerate(walks) if i % n == n - 1]
    train = [w for i, w in enumerate(walks) if i % n != n - 1]
    return train, test


def transitions(walks: Iterable[Sequence[int]], special: int = 2) -> Set[Tuple[int, int]]:
    """The distinct concept -> concept transitions in the walks (special tokens left out)."""
    out: Set[Tuple[int, int]] = set()
    for w in walks:
        body = [t - special for t in w if t >= special]
        out.update(zip(body, body[1:]))
    return out


def step_cap(n_transitions: int, steps_done: int, steps_per_epoch: int, requested: int,
             ratio: float = MAX_STEPS_PER_TRANSITION, margin: float = CAP_MARGIN) -> int:
    """Dynamics epochs to run: at most `requested`, and no more than keeps the model's total dynamics steps under
    margin x ratio x the distinct transitions it is trained on (0 when it is already there)."""
    if requested <= 0 or steps_per_epoch <= 0:
        return 0
    budget = margin * ratio * max(0, int(n_transitions)) - max(0, int(steps_done))
    return max(0, min(int(requested), int(budget // steps_per_epoch)))


def _targets(walk: Sequence[int], eos: int = 1) -> List[Tuple[int, int]]:
    """(previous, next) token pairs of a walk - every position whose next token is not padding."""
    return [(a, b) for a, b in zip(walk, walk[1:]) if b != eos]


def baselines(train: Sequence[Sequence[int]], test: Sequence[Sequence[int]], vocab: int,
              eos: int = 1) -> Dict[str, Any]:
    """Unigram and bigram next-token mean negative log-likelihood (nats) and top-1 accuracy on the held-out walks,
    fitted on the training walks with add-one smoothing over `vocab` tokens."""
    uni: Counter = Counter()
    bi: Dict[int, Counter] = {}
    for w in train:
        for a, b in _targets(w, eos):
            uni[b] += 1
            bi.setdefault(a, Counter())[b] += 1
    total = sum(uni.values())
    uni_top = uni.most_common(1)[0][0] if uni else None
    n = 0
    nll_u = nll_b = 0.0
    hit_u = hit_b = 0
    for w in test:
        for a, b in _targets(w, eos):
            n += 1
            nll_u -= math.log((uni[b] + 1) / (total + vocab))
            row = bi.get(a) or Counter()
            nll_b -= math.log((row[b] + 1) / (sum(row.values()) + vocab))
            hit_u += b == uni_top
            hit_b += bool(row) and b == row.most_common(1)[0][0]
    if not n:
        return {"n": 0}
    return {"n": n, "unigram_nll": round(nll_u / n, 4), "bigram_nll": round(nll_b / n, 4),
            "unigram_acc": round(hit_u / n, 4), "bigram_acc": round(hit_b / n, 4)}


def verdict(model_nll: float, model_acc: float, base: Dict[str, Any], *, train_walks: int = 0,
            test_walks: int = 0, min_tokens: int = MIN_HELD_OUT_TOKENS) -> Dict[str, Any]:
    """The held-out result the health verdict reads: beats_baseline only when the model's held-out log-loss is
    below the unigram's AND the bigram's, on at least `min_tokens` held-out tokens."""
    n = int(base.get("n") or 0)
    out = {"n_tokens": n, "train_walks": int(train_walks), "test_walks": int(test_walks),
           "model_nll": round(float(model_nll), 4), "model_acc": round(float(model_acc), 4),
           **{k: v for k, v in base.items() if k != "n"}}
    enough = n >= min_tokens
    out["beats_baseline"] = bool(enough and model_nll < base.get("unigram_nll", 0) and model_nll < base.get("bigram_nll", 0))
    if not enough:
        out["why"] = f"{n} held-out tokens (< {min_tokens})"
    elif not out["beats_baseline"]:
        out["why"] = ("does not beat the bigram baseline" if model_nll >= base.get("bigram_nll", 0)
                      else "does not beat the unigram baseline")
    return out
