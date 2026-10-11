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


# A query of this many words or more is a chat message, not a search: see lexical_query.
LONG_QUERY_WORDS = 8


def lexical_query(text: str, min_words: int = LONG_QUERY_WORDS) -> str:
    """The text to send a keyword index for ``text``.

    Short queries go as they are. A long one (a chat message) keeps only its content
    terms, in order and once each: an index that keeps stopwords (Neo4j's
    ``standard-no-stop-words``) ORs every word, so filler-heavy messages ("you", "is",
    "working") outrank the record that names the subject. Live 2026-10-06, 14 long
    questions: Neo4j leg hit@5 0.64 -> 0.79; hybrid MRR 0.50 -> 0.69; short queries
    unchanged. A query of stopwords only goes as it is.
    """
    text = text or ""
    if len(text.split()) < int(min_words):
        return text
    terms = [t for t in dict.fromkeys(_TOKEN_RE.findall(text.lower())) if t not in STOPWORDS]
    return " ".join(terms) if terms else text


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


#: how many fused candidates the cross-encoder re-reads (FM-7.3: one node call, 1 query x N documents)
RERANK_TOP = 30


def rerank_lift(rerank_scores: Sequence[float], top: float) -> List[float]:
    """New scores for a reranked head, in the head's own order: the cross-encoder decides the order inside the head,
    and every head item lands above every candidate outside it (whose scores are <= top). The best gets 2 * top, the
    rest step down evenly, the last stays above top."""
    n = len(rerank_scores)
    if not n:
        return []
    order = sorted(range(n), key=lambda i: -float(rerank_scores[i]))
    out = [0.0] * n
    base = float(top) if top and top > 0 else 1.0
    for pos, i in enumerate(order):
        out[i] = base * (2.0 - pos / n)
    return out
