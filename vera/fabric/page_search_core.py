"""Discovery's page search index in Postgres (four-minds FM-7.1 / Q5; owner, 2026-10-11: Postgres, not SQLite FTS5).

discover_recall searched the fabric's SQLite `fabric_pages` with `lower(full_text) LIKE '%term%'` per term (a full
scan per term, ~0.25 s each on 7k pages) and returned the NEWEST pages that matched ANY term, not the best. This index
ranks them: a Postgres table with a generated, weighted tsvector (title A, text B) and a GIN index - the same pattern
as memory search (`vera_memories.search_vector`) - reachable from every process and node.

SQLite stays the store of record for pages and their entities. The index is DERIVED: a sync job copies pages whose
SQLite rowid is past a watermark. Every page write is `INSERT OR REPLACE`, which gives a replaced row a new, higher
rowid, so the watermark also catches updates; nothing deletes pages. Search falls back to the LIKE path whenever the
index is unavailable.

Pure: SQL text and the query builder; no I/O.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

TABLE = "fabric_pages_search"
STATE = "fabric_pages_search_state"
WATERMARK_KEY = "sqlite_rowid"
#: text indexed per page (to_tsvector caps a tsvector at 1 MB; recall shows the first part anyway)
TEXT_CAP = 100_000
#: query terms used (as discover_recall's LIKE path: words of 3+ characters)
MAX_TERMS = 8
MIN_TERM = 3

DDL = [
    f"""CREATE TABLE IF NOT EXISTS {TABLE} (
        id          TEXT PRIMARY KEY,
        url         TEXT,
        title       TEXT,
        dataset_id  TEXT,
        word_count  INTEGER DEFAULT 0,
        full_text   TEXT,
        src_rowid   BIGINT,
        synced_at   TIMESTAMPTZ DEFAULT now(),
        search_vector tsvector GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(full_text, '')), 'B')
        ) STORED
    )""",
    f"CREATE INDEX IF NOT EXISTS {TABLE}_fts ON {TABLE} USING gin(search_vector)",
    f"CREATE INDEX IF NOT EXISTS {TABLE}_url ON {TABLE}(url)",
    f"CREATE INDEX IF NOT EXISTS {TABLE}_ds ON {TABLE}(dataset_id)",
    f"CREATE TABLE IF NOT EXISTS {STATE} (k TEXT PRIMARY KEY, v BIGINT NOT NULL, updated_at TIMESTAMPTZ DEFAULT now())",
]

UPSERT = (f"INSERT INTO {TABLE} (id, url, title, dataset_id, word_count, full_text, src_rowid, synced_at) "
          f"VALUES ($1, $2, $3, $4, $5, $6, $7, now()) "
          f"ON CONFLICT (id) DO UPDATE SET url = EXCLUDED.url, title = EXCLUDED.title, "
          f"dataset_id = EXCLUDED.dataset_id, word_count = EXCLUDED.word_count, full_text = EXCLUDED.full_text, "
          f"src_rowid = EXCLUDED.src_rowid, synced_at = now()")

GET_WATERMARK = f"SELECT v FROM {STATE} WHERE k = $1"
SET_WATERMARK = (f"INSERT INTO {STATE} (k, v, updated_at) VALUES ($1, $2, now()) "
                 f"ON CONFLICT (k) DO UPDATE SET v = GREATEST({STATE}.v, EXCLUDED.v), updated_at = now()")

#: the next pages past the watermark, oldest first (SQLite)
SQLITE_BATCH = ("SELECT rowid, id, url, title, dataset_id, word_count, full_text FROM fabric_pages "
                "WHERE rowid > ? ORDER BY rowid LIMIT ?")
SQLITE_MAX_ROWID = "SELECT coalesce(max(rowid), 0), count(*) FROM fabric_pages"

_WORD = re.compile(r"[a-z0-9]+")


def terms(query: str, max_terms: int = MAX_TERMS) -> List[str]:
    """The query's searchable words: lower case, letters and digits only, 3+ characters, first seen first."""
    out: List[str] = []
    for w in _WORD.findall(str(query or "").lower()):
        if len(w) >= MIN_TERM and w not in out:
            out.append(w)
        if len(out) >= max_terms:
            break
    return out


def or_tsquery(query: str) -> str:
    """A to_tsquery string matching ANY of the words (the LIKE path's semantics), ranked by ts_rank_cd, so a page
    with more of them comes first. '' when the query has no searchable word. Words are [a-z0-9]+ only, so no
    tsquery operator can be injected."""
    return " | ".join(terms(query))


def search_sql(with_dataset: bool) -> str:
    """$1 = the or_tsquery string, $2 = limit, $3 = dataset_id (when with_dataset)."""
    ds = " AND dataset_id = $3" if with_dataset else ""
    return (f"SELECT url, title, full_text, dataset_id, word_count, ts_rank_cd(search_vector, q) AS rank "
            f"FROM {TABLE}, to_tsquery('english', $1) q WHERE search_vector @@ q{ds} "
            f"ORDER BY rank DESC, src_rowid DESC LIMIT $2")


def clean(text: Any, cap: int = 0) -> str:
    """Text Postgres can store: NUL bytes removed (crawled pages carry them; one stopped the first sync on 11 Oct
    2026 - 'invalid byte sequence for encoding UTF8: 0x00' - and the whole batch with it), optionally capped."""
    t = str(text or "").replace("\x00", "")
    return t[:cap] if cap else t


def to_rows(sqlite_rows: Iterable[Sequence[Any]]) -> Tuple[List[Tuple], int]:
    """SQLite (rowid, id, url, title, dataset_id, word_count, full_text) rows -> UPSERT argument tuples, and the
    highest rowid seen (the next watermark). Rows without an id are skipped; text is NUL-free and capped at
    TEXT_CAP."""
    out: List[Tuple] = []
    top = 0
    for r in sqlite_rows:
        rowid, pid, url, title, ds, wc, text = (list(r) + [None] * 7)[:7]
        top = max(top, int(rowid or 0))
        if not pid:
            continue
        out.append((clean(pid), clean(url), clean(title, 1000), clean(ds), int(wc or 0),
                    clean(text, TEXT_CAP), int(rowid or 0)))
    return out, top


def page_dict(row: Any) -> Dict[str, Any]:
    """A search row as discover_recall's page shape (url, title, full_text, tags, dataset_id, word_count), plus rank."""
    get = row.get if hasattr(row, "get") else (lambda k, d=None: d)
    return {"url": get("url") or "", "title": get("title") or "", "full_text": get("full_text") or "",
            "tags": "", "dataset_id": get("dataset_id") or "", "word_count": int(get("word_count") or 0),
            "rank": round(float(get("rank") or 0.0), 4)}


def lag(sqlite_max_rowid: int, watermark: Optional[int]) -> int:
    """How many rowids the index is behind the page store (0 when caught up)."""
    return max(0, int(sqlite_max_rowid or 0) - int(watermark or 0))
