"""Rank fusion and relevance checks shared by Vera's retrieval paths (four-minds FM-7.1).

Reciprocal-rank fusion is scale-free: each backend contributes 1/(k + rank) per list,
so raw scores on incompatible scales (cosine, ts_rank, Lucene) never meet. What the
retrieval paths got wrong was not RRF itself but *which lists* were fused together:
memory.seek fused the fabric backends' lists, then appended session-memory hits
afterwards with a one-list score and no relevance floor, so a strong memory hit sat
far below a mediocre fabric hit and a weak one could never be filtered (RF-04).

Pure: no I/O, no fabric, no orchestrator.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Set

RRF_K = 60
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")

# Function words that say nothing about a query's subject. Kept small and literal.
STOPWORDS = frozenset("""
the and for are but not you all any can had her was one our out has have his how its may new now
old see two way who did get got let put say she too use with that this from they will would there
their what which when where why your about into than then them these those been being were does
doing just also only very more most some such over under again further here each both few other
own same should could shall might must upon what's i'm it's let's tell show give find make know
please want need like look looking search find about
""".split())


def rrf(lists: Sequence[Sequence[str]], weights: Optional[Sequence[float]] = None,
        k: int = RRF_K) -> Dict[str, float]:
    """Weighted reciprocal-rank fusion: score(id) = sum_i weight_i / (k + rank_i(id)).

    ``weights`` defaults to 1.0 per list. A list may repeat an id; only its best
    (first) rank counts.
    """
    out: Dict[str, float] = {}
    ws = list(weights) if weights is not None else [1.0] * len(lists)
    if len(ws) != len(lists):
        raise ValueError("weights must match lists")
    for lst, w in zip(lists, ws):
        seen: Set[str] = set()
        for rank, rid in enumerate(lst):
            if rid in seen:
                continue
            seen.add(rid)
            out[rid] = out.get(rid, 0.0) + float(w) / (k + rank)
    return out


def query_terms(text: str) -> Set[str]:
    """Content terms of a query: lowercase alphanumeric tokens of 3+ chars, minus stopwords."""
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if t not in STOPWORDS}


def lexical_pass(terms: Set[str], text: str, min_overlap: int = 1) -> bool:
    """True when ``text`` shares at least ``min_overlap`` content terms with the query.

    The same standard memory.seek already applies to fabric records ("a keyword hit
    passes the floor"), for candidates that come with no comparable semantic score.
    An empty term set (a query made only of stopwords) passes nothing.
    """
    if not terms:
        return False
    toks = set(_TOKEN_RE.findall((text or "").lower()[:4000]))
    return len(terms & toks) >= min_overlap


def list_weight(n_lists: int) -> float:
    """Weight that puts a single-backend list on the scale of ``n_lists`` fused lists.

    A record ranked first by every one of n backends scores n/k; a record ranked
    first by a single source given this weight scores the same, so a separately
    ranked source (session memory) competes on equal terms.
    """
    return float(max(1, int(n_lists)))
