"""memory.seek's keyword leg on a Postgres full-text index (four-minds FM-7.1, 11 Oct 2026).

The leg was `text ILIKE '%w%' OR ...` over `fabric_records` (636k rows, only a primary key): a sequential scan that
stops at the first 4 x limit rows matching ANY word in table order, ranked in Python afterwards. Measured on prod
11 Oct: p50 322 ms, p95 2.2 s per seek (the tail of every seek), and the rows it returns are the first found, not the
best - a record holding every query word can be missed entirely.

The index is an expression GIN index (no new column, so no table rewrite and no write lock: built CONCURRENTLY in
43 s, 62 MB). A search runs in two tiers: the records holding ALL the words, ranked (3-9 ms on prod), then, when
they are fewer than asked for, records holding ANY word from a capped candidate set, ranked (80-320 ms). All-word
records always come first.

Pure: SQL text, the query builder and the merge; no I/O.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, List, Sequence, Tuple

INDEX = "fabric_records_fts"
#: must match the index expression exactly, or Postgres will not use the index
VECTOR = "to_tsvector('english', coalesce(text, ''))"
#: candidates the any-word tier ranks (it recomputes each one's vector to rank it)
ANY_CAP = 1000
MAX_TERMS = 12
MIN_TERM = 3

CREATE = f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON fabric_records USING gin ({VECTOR})"
#: an interrupted CONCURRENTLY build leaves an INVALID index that IF NOT EXISTS would keep forever
STATE = "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass($1)"
DROP = f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}"

#: local hours the nightly database dump runs (backbone §2: no heavy DDL beside it)
DUMP_WINDOW = (1, 2, 3)

_WORD = re.compile(r"[a-z0-9]+")


def in_dump_window(hour: int) -> bool:
    return int(hour) in DUMP_WINDOW


def terms(query: str, max_terms: int = MAX_TERMS) -> List[str]:
    """The query's searchable words: lower case, letters and digits only, 3+ characters, first seen first. Only
    [a-z0-9] reaches a tsquery, so no tsquery operator can be injected."""
    out: List[str] = []
    for w in _WORD.findall(str(query or "").lower()):
        if len(w) >= MIN_TERM and w not in out:
            out.append(w)
        if len(out) >= max_terms:
            break
    return out


def tsquery(words: Sequence[str], op: str) -> str:
    """'a & b' (all) or 'a | b' (any); '' for no words."""
    return f" {op} ".join(words)


def search_sql(tier: str, with_dataset: bool) -> str:
    """$1 = the tsquery string, $2 = limit, $3 = dataset_id (when with_dataset). tier 'all' ranks every match (an
    all-word match is selective); tier 'any' ranks the first ANY_CAP matches."""
    ds = " AND dataset_id = $3" if with_dataset else ""
    if tier == "all":
        return (f"SELECT id, ts_rank_cd({VECTOR}, q) AS r FROM fabric_records, to_tsquery('english', $1) q "
                f"WHERE {VECTOR} @@ q{ds} ORDER BY r DESC LIMIT $2")
    return (f"SELECT id, ts_rank_cd({VECTOR}, q) AS r FROM ("
            f"SELECT id, text FROM fabric_records, to_tsquery('english', $1) q0 WHERE {VECTOR} @@ q0{ds} "
            f"LIMIT {ANY_CAP}) c, to_tsquery('english', $1) q ORDER BY r DESC LIMIT $2")


def _norm(rows: Iterable[Tuple[str, float]]) -> List[Tuple[str, float]]:
    rows = [(str(i), float(r or 0.0)) for i, r in rows]
    top = max((r for _, r in rows), default=0.0) or 1.0
    return [(i, r / top) for i, r in rows]


def merge(all_rows: Iterable[Tuple[str, Any]], any_rows: Iterable[Tuple[str, Any]],
          limit: int) -> List[Tuple[str, float]]:
    """All-word records first (scores 0.5-1.0), then any-word records not already listed (below 0.45), each tier in
    its own rank order; at most `limit`."""
    out = [(i, 0.5 + 0.5 * s) for i, s in _norm(all_rows)]
    seen = {i for i, _ in out}
    out += [(i, 0.45 * s) for i, s in _norm(any_rows) if i not in seen]
    return out[:max(0, int(limit))]
