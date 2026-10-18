"""What the worldview stream worker does with one fetched batch (four-minds FM-W.2, 18 Oct 2026). Pure: no I/O.

classify() splits a batch into the records to encode and the ones skipped, by reason, so worldview.stream.status says
why nothing was encoded; retry_ids() keeps the not-yet-embedded ones for a later pass (bounded, counted).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple

REASONS = ("already_indexed", "not_embedded", "dim_mismatch")
RETRY_MAX = 3
RETRY_CAP = 5000


def classify(ids: Sequence[str], embs: Sequence[Any], existing: Iterable[str], dim: int
             ) -> Tuple[List[int], Dict[str, List[str]]]:
    """(positions to encode, {reason: [ids]}) - a record is skipped when it is indexed already, has no embedding yet,
    or its embedding is not the model's size."""
    have = set(existing)
    take: List[int] = []
    skipped: Dict[str, List[str]] = {r: [] for r in REASONS}
    for i, rid in enumerate(ids):
        if rid in have:
            skipped["already_indexed"].append(rid)
            continue
        emb = embs[i] if i < len(embs) else None
        try:
            n = len(emb) if emb is not None else 0
        except Exception:
            n = 0
        if n < 10:
            skipped["not_embedded"].append(rid)
        elif dim and n != dim:
            skipped["dim_mismatch"].append(rid)
        else:
            take.append(i)
    return take, skipped


def retry_ids(pending: Dict[str, int], not_embedded: Iterable[str], requested: Iterable[str] = ()) -> Dict[str, int]:
    """The retry book after one pass: ids still without an embedding gain a try (dropped after RETRY_MAX); ids that
    were asked for and are no longer missing leave the book. Bounded at RETRY_CAP (oldest out)."""
    book = dict(pending)
    missing = list(not_embedded)
    for rid in requested:
        if rid not in missing:
            book.pop(rid, None)
    for rid in missing:
        book[rid] = book.get(rid, 0) + 1
        if book[rid] > RETRY_MAX:
            book.pop(rid, None)
    while len(book) > RETRY_CAP:
        book.pop(next(iter(book)))
    return book


def unindexed(ids: Sequence[str], existing: Iterable[str]) -> List[str]:
    """The ids not in the worldview yet, in order, without repeats (FM-W.3 catch-up)."""
    have = set(existing)
    out, seen = [], set()
    for rid in ids:
        if rid and rid not in have and rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def chunks(ids: Sequence[str], size: int) -> List[List[str]]:
    size = max(1, int(size or 1))
    return [list(ids[i:i + size]) for i in range(0, len(ids), size)]
