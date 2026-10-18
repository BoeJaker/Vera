"""Novelty on receipts (shared-planning web-ingress WI-5; PLAN §3.4 steps 1-2). Pure.

What a fetch adds to what the map already holds:
  known  - the exact content hash is in web_content (only `seen` moves);             novelty 0
  near   - a simhash within NEAR_DUP_BITS of a known one (via the LSH bands);          novelty = bits / 64
  new    - nothing like it;                                                             novelty 1
Step 3 (embedding similarity against the fabric) is vera-62's shadow item and is not here.

The host's novel yield is an EWMA of the share of its fetches that were new - the frontier's expected-novelty
input (PLAN §3.4) and the saturation signal for topic maps.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from Vera.vera.fabric import ingress_core as ic
except ImportError:  # pragma: no cover
    from vera.fabric import ingress_core as ic

YIELD_ALPHA = 0.1


def score(content_hash: str, known_hashes: Iterable[str], candidates: Iterable[Tuple[str, int]],
          simhash: Optional[int], bits: int = ic.NEAR_DUP_BITS) -> Dict[str, Any]:
    """{novelty, kind, canonical}: `known_hashes` are hashes the map holds; `candidates` are (hash, simhash) rows
    that share an LSH band with this content; the nearest within `bits` makes it a near-duplicate."""
    if content_hash and content_hash in set(known_hashes or ()):
        return {"novelty": 0.0, "kind": "known", "canonical": content_hash}
    if simhash is not None:
        best: Optional[Tuple[int, str]] = None
        for h, sh in candidates or ():
            if h == content_hash or sh is None:
                continue
            d = ic.hamming(int(simhash), int(sh))
            if d <= bits and (best is None or d < best[0]):
                best = (d, str(h))
        if best is not None:
            return {"novelty": round(best[0] / 64.0, 3), "kind": "near", "canonical": best[1]}
    return {"novelty": 1.0, "kind": "new", "canonical": ""}


def band_params(bands_per_row: Iterable[Sequence[int]]) -> List[List[int]]:
    """The four band lists a batch queries web_content with (one list per band position)."""
    out: List[List[int]] = [[], [], [], []]
    for bands in bands_per_row or ():
        for i, b in enumerate(list(bands)[:4]):
            if b not in out[i]:
                out[i].append(int(b))
    return out


def host_yield(prev: Optional[float], novel: bool, alpha: float = YIELD_ALPHA) -> float:
    """The host's novel-yield EWMA after one fetch (a brand-new host starts at the first observation)."""
    x = 1.0 if novel else 0.0
    if prev is None:
        return x
    return round(float(prev) * (1.0 - alpha) + x * alpha, 4)


def summarise(scores: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    out = {"known": 0, "near": 0, "new": 0}
    for s in scores or ():
        k = str(s.get("kind") or "new")
        out[k] = out.get(k, 0) + 1
    return out
