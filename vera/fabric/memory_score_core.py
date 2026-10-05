"""One 0..1 relevance score for the hybrid memory store (RF-05).

The store asks up to three backends and each answers on its own scale: Chroma a cosine
similarity (0..1, live 0.40-0.58), Postgres ts_rank (0..1), Neo4j a Lucene score
(unbounded, live 1.4-7.5). The merge used to keep the largest raw number, so any
keyword hit from Neo4j outranked every vector hit and every consumer threshold built
for 0..1 (related-QA 0.4, the chat panel's 0.55, chat-memory decay) passed it.

Each backend's score is mapped onto 0..1 first, then the backends that found the same
record are combined with a noisy-OR, so agreement raises a score and nothing leaves
0..1. Plain rank fusion was the first design; it was dropped because Chroma returns a
near-flat cosine for unrelated queries, and RRF would have ranked that noise level with
the best keyword hit.

Pure: no I/O.
"""
from __future__ import annotations

from typing import Dict, Iterable, Mapping, Tuple

# Lucene score at which a Neo4j hit maps to 0.5 (s / (s + LUCENE_HALF)).
# Live: 1.45 -> 0.42, 2.0 -> 0.5, 4.3 -> 0.68, 7.5 -> 0.79.
LUCENE_HALF = 2.0
UNBOUNDED = frozenset({"neo4j"})


def normalise(backend: str, score) -> float:
    """A backend's raw score on 0..1."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return 0.0
    if s != s or s <= 0:
        return 0.0
    if backend in UNBOUNDED:
        return s / (s + LUCENE_HALF)
    return min(1.0, s)


def combine(per_backend: Mapping[str, Iterable[Tuple[str, float]]]) -> Dict[str, float]:
    """{record id: 0..1 score} from each backend's (id, raw score) hits.

    noisy-OR across backends: 1 - prod(1 - s_b). A backend that lists an id twice
    counts its best hit once.
    """
    best: Dict[str, Dict[str, float]] = {}
    for name, hits in per_backend.items():
        for rid, raw in hits:
            s = normalise(name, raw)
            per = best.setdefault(rid, {})
            if s > per.get(name, -1.0):
                per[name] = s
    out: Dict[str, float] = {}
    for rid, per in best.items():
        miss = 1.0
        for s in per.values():
            miss *= 1.0 - s
        out[rid] = 1.0 - miss
    return out
