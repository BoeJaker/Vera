"""The canvas curator's first questions, in shadow (four-minds FM-6.2 turn domain, FM-6.3 which surfaces).

Today surface.find picks a turn's domains from word lists (surface_core.DOMAINS[*].words, a hit count, the second
domain at >= 60% of the first) and ranks each domain's surfaces by fixed scores. The curator answers the same two
questions from the turn's decision-space vector (the Frame's MiniLM vector, FM-1.1): the cosine against a card per
domain (its label and words), then against a card per surface (what it shows, its capability). Cards are embedded
once per card text and cached.

In shadow nothing changes on the canvas: each turn.prepare records what the words chose and what the cards would
have chosen, and canvas.surface_rank.report reads how often they agree and where the cards found a domain the words
missed (the word lists' known gap: a turn about "how is my money doing" names no listed word).

Pure: no I/O.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SHADOW_KEY = "vera:canvas:surface_rank:shadow"
SHADOW_KEEP = 2000
CARDS_KEY = "vera:canvas:surface_cards:"
#: a domain needs at least this cosine to be the turn's domain (MiniLM: unrelated text sits near 0.1-0.2)
DOMAIN_TAU = 0.3
#: a second domain only when it is this close to the first
SECOND_GAP = 0.03
MAX_DOMAINS = 2


def domain_cards(domains: Iterable[Mapping[str, Any]]) -> Dict[str, str]:
    """One card per surface domain: what it is and the words that mean it."""
    return {str(d["id"]): f"{d.get('label') or d['id']}: {d.get('words') or ''}".strip() for d in domains}


def surface_card(s: Mapping[str, Any]) -> str:
    """One card per surface: what it shows and the capability or panel behind it, in words."""
    name = str(s.get("cap") or s.get("panel") or s.get("id") or "")
    words = name.replace(".", " ").replace("_", " ")
    return f"{str(s.get('about') or '').strip()[:200]} ({words})".strip()


def cards_hash(cards: Mapping[str, str]) -> str:
    return hashlib.sha256(json.dumps(sorted(cards.items())).encode("utf-8")).hexdigest()[:16]


def _unit(v: Sequence[float]) -> List[float]:
    n = math.sqrt(sum(float(x) * float(x) for x in v)) or 1.0
    return [float(x) / n for x in v]


def rank(vec: Optional[Sequence[float]], cards: Mapping[str, Sequence[float]]) -> List[Tuple[str, float]]:
    """(key, cosine) best first; empty without a vector."""
    if not vec:
        return []
    q = _unit(vec)
    out = [(k, round(sum(a * b for a, b in zip(q, _unit(cv))), 4)) for k, cv in cards.items()
           if cv and len(cv) == len(q)]
    return sorted(out, key=lambda kv: -kv[1])


def pick_domains(ranked: Sequence[Tuple[str, float]], tau: float = DOMAIN_TAU, gap: float = SECOND_GAP,
                 max_domains: int = MAX_DOMAINS) -> List[str]:
    """The turn's domains by the cards: the best above tau, and the next ones within `gap` of it."""
    if not ranked or ranked[0][1] < tau:
        return []
    top = ranked[0][1]
    return [k for k, s in ranked if s >= tau and s >= top - gap][:max_domains]


def pick_surfaces(ranked: Sequence[Tuple[str, float]], domain_of: Mapping[str, str], domains: Sequence[str],
                  k: int) -> List[str]:
    """The best k surfaces of the chosen domains by the cards."""
    allowed = set(domains)
    return [sid for sid, _ in ranked if domain_of.get(sid) in allowed][:max(0, int(k))]


def row(text_hash: str, words: Sequence[str], cards: Sequence[str], card_scores: Sequence[Tuple[str, float]],
        lexical: Sequence[str], by_cards: Sequence[str], ts: str = "") -> Dict[str, Any]:
    """One turn's comparison (no text: the hash ties it to the turn record)."""
    return {"ts": ts, "text_hash": text_hash, "words": list(words), "cards": list(cards),
            "card_scores": [[k, s] for k, s in list(card_scores)[:3]], "lexical": list(lexical),
            "by_cards": list(by_cards)}


def summarize(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """How often the cards agree with the words on the turn's top domain and on the surfaces, and how often each side
    found a domain where the other found none."""
    rows = list(rows)
    out: Dict[str, Any] = {"turns": len(rows)}
    both = [r for r in rows if r.get("words") and r.get("cards")]
    out["both_found"] = len(both)
    out["words_only"] = sum(1 for r in rows if r.get("words") and not r.get("cards"))
    out["cards_only"] = sum(1 for r in rows if r.get("cards") and not r.get("words"))
    out["neither"] = sum(1 for r in rows if not r.get("words") and not r.get("cards"))
    if both:
        out["top_domain_agree"] = round(sum(1 for r in both if r["words"][0] == r["cards"][0]) / len(both), 3)
    overlap = [len(set(r["lexical"]) & set(r["by_cards"])) / len(r["lexical"])
               for r in rows if r.get("lexical") and r.get("by_cards")]
    if overlap:
        out["surface_overlap"] = round(statistics.mean(overlap), 3)
    tops = [r["card_scores"][0][1] for r in rows if r.get("card_scores")]
    if tops:
        out["card_top_score_median"] = round(statistics.median(tops), 3)
    return out
