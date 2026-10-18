"""The web map's receipts (shared-planning web-ingress WI-3; PLAN §3.2). Pure.

Every fetch Vera makes leaves a receipt. The request path only enqueues the fetch result (microseconds); this
module turns that result into rows (hashing, links, reader route - the parts worth a thread), and holds the DDL
and the upserts the writer runs in one batch. No I/O, no Vera imports.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from Vera.vera.fabric import ingress_core as ic
except ImportError:  # pragma: no cover - worktree / app-free import
    from vera.fabric import ingress_core as ic

SCHEMA_VERSION = 1

#: Postgres DDL. Idempotent; the writer runs it once per process with a short lock_timeout.
DDL: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS web_hosts (
        host        TEXT PRIMARY KEY,
        first_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        fetches     BIGINT NOT NULL DEFAULT 0,
        errors      BIGINT NOT NULL DEFAULT 0,
        bytes       BIGINT NOT NULL DEFAULT 0,
        robots      TEXT,
        robots_at   TIMESTAMPTZ,
        crawl_delay REAL,
        novel_yield REAL,
        note        TEXT)""",
    """CREATE TABLE IF NOT EXISTS web_urls (
        url_key      TEXT PRIMARY KEY,
        host         TEXT NOT NULL,
        first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_status  INT,
        content_type TEXT,
        reader       TEXT,
        kind         TEXT,
        bytes        INT,
        etag         TEXT,
        last_modified TEXT,
        content_hash TEXT,
        simhash      BIGINT,
        fetches      BIGINT NOT NULL DEFAULT 0,
        changes      BIGINT NOT NULL DEFAULT 0,
        last_caller  TEXT,
        last_purpose TEXT,
        policy       TEXT,
        final_key    TEXT,
        title        TEXT)""",
    "CREATE INDEX IF NOT EXISTS web_urls_host ON web_urls(host)",
    "CREATE INDEX IF NOT EXISTS web_urls_hash ON web_urls(content_hash)",
    """CREATE TABLE IF NOT EXISTS web_fetches (
        id        BIGSERIAL PRIMARY KEY,
        ts        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        url_key   TEXT NOT NULL,
        host      TEXT NOT NULL,
        status    INT,
        ms        INT,
        bytes     INT,
        from_cache BOOLEAN NOT NULL DEFAULT FALSE,
        caller    TEXT,
        purpose   TEXT,
        policy    TEXT,
        error     TEXT)""",
    "CREATE INDEX IF NOT EXISTS web_fetches_ts ON web_fetches(ts)",
    "CREATE INDEX IF NOT EXISTS web_fetches_key ON web_fetches(url_key)",
    """CREATE TABLE IF NOT EXISTS web_links (
        src_key    TEXT NOT NULL,
        dst_key    TEXT NOT NULL,
        first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        text       TEXT,
        rel        TEXT,
        PRIMARY KEY (src_key, dst_key))""",
    "CREATE INDEX IF NOT EXISTS web_links_dst ON web_links(dst_key)",
    """CREATE TABLE IF NOT EXISTS web_content (
        content_hash TEXT PRIMARY KEY,
        first_key    TEXT NOT NULL,
        size         INT,
        reader       TEXT,
        simhash      BIGINT,
        b0 INT, b1 INT, b2 INT, b3 INT,
        first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        seen         BIGINT NOT NULL DEFAULT 1,
        record_id    TEXT)""",
    "CREATE INDEX IF NOT EXISTS web_content_b0 ON web_content(b0)",
    "CREATE INDEX IF NOT EXISTS web_content_b1 ON web_content(b1)",
    "CREATE INDEX IF NOT EXISTS web_content_b2 ON web_content(b2)",
    "CREATE INDEX IF NOT EXISTS web_content_b3 ON web_content(b3)",
    """CREATE TABLE IF NOT EXISTS web_queries (
        id          BIGSERIAL PRIMARY KEY,
        ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        query       TEXT NOT NULL,
        engine      TEXT,
        caller      TEXT,
        purpose     TEXT,
        results     INT,
        result_keys TEXT[])""",
    "CREATE INDEX IF NOT EXISTS web_queries_ts ON web_queries(ts)",
    # WI-5: novelty per URL (last fetch) and the host's novel yield (an EWMA, already a column)
    "ALTER TABLE web_urls ADD COLUMN IF NOT EXISTS novelty REAL",
    "ALTER TABLE web_urls ADD COLUMN IF NOT EXISTS novelty_kind TEXT",
    "ALTER TABLE web_urls ADD COLUMN IF NOT EXISTS canonical_hash TEXT",
    # WI-8: topic maps, keyed on the intel topic id
    """CREATE TABLE IF NOT EXISTS web_topic (
        topic_key   TEXT PRIMARY KEY,
        name        TEXT,
        intel_topic_id TEXT,
        dataset_ids TEXT[] NOT NULL DEFAULT '{}',
        kb_ids      TEXT[] NOT NULL DEFAULT '{}',
        worldview_concepts TEXT[] NOT NULL DEFAULT '{}',
        seeds       TEXT[] NOT NULL DEFAULT '{}',
        queries     TEXT[] NOT NULL DEFAULT '{}',
        entities    TEXT[] NOT NULL DEFAULT '{}',
        created     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_run    TIMESTAMPTZ,
        saturation  TEXT,
        novel_yield REAL)""",
    """CREATE TABLE IF NOT EXISTS web_topic_members (
        topic_key  TEXT NOT NULL,
        url_key    TEXT NOT NULL,
        role       TEXT NOT NULL DEFAULT 'page',
        kind       TEXT,
        relevance  REAL,
        novelty    REAL,
        first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        source     TEXT,
        PRIMARY KEY (topic_key, url_key))""",
    "CREATE INDEX IF NOT EXISTS web_topic_members_url ON web_topic_members(url_key)",
)

MAP_TABLES = ("web_hosts", "web_urls", "web_fetches", "web_links", "web_content", "web_queries", "web_topic",
              "web_topic_members")

#: Upserts, in the order the writer runs them. $n placeholders (asyncpg).
UPSERT_HOST = """INSERT INTO web_hosts (host, fetches, errors, bytes) VALUES ($1, 1, $2, $3)
    ON CONFLICT (host) DO UPDATE SET last_seen = NOW(), fetches = web_hosts.fetches + 1,
    errors = web_hosts.errors + EXCLUDED.errors, bytes = web_hosts.bytes + EXCLUDED.bytes"""
UPSERT_URL = """INSERT INTO web_urls (url_key, host, last_status, content_type, reader, kind, bytes, etag, last_modified,
        content_hash, simhash, fetches, changes, last_caller, last_purpose, policy, final_key, title)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 1, 0, $12, $13, $14, $15, $16)
    ON CONFLICT (url_key) DO UPDATE SET last_seen = NOW(), last_status = EXCLUDED.last_status,
    content_type = COALESCE(EXCLUDED.content_type, web_urls.content_type),
    reader = COALESCE(EXCLUDED.reader, web_urls.reader), kind = COALESCE(EXCLUDED.kind, web_urls.kind),
    bytes = COALESCE(EXCLUDED.bytes, web_urls.bytes), etag = COALESCE(EXCLUDED.etag, web_urls.etag),
    last_modified = COALESCE(EXCLUDED.last_modified, web_urls.last_modified),
    changes = web_urls.changes + CASE WHEN EXCLUDED.content_hash IS NOT NULL AND web_urls.content_hash IS NOT NULL
                                       AND EXCLUDED.content_hash <> web_urls.content_hash THEN 1 ELSE 0 END,
    content_hash = COALESCE(EXCLUDED.content_hash, web_urls.content_hash),
    simhash = COALESCE(EXCLUDED.simhash, web_urls.simhash), fetches = web_urls.fetches + 1,
    last_caller = EXCLUDED.last_caller, last_purpose = EXCLUDED.last_purpose, policy = EXCLUDED.policy,
    final_key = COALESCE(EXCLUDED.final_key, web_urls.final_key), title = COALESCE(EXCLUDED.title, web_urls.title)"""
INSERT_FETCH = """INSERT INTO web_fetches (url_key, host, status, ms, bytes, from_cache, caller, purpose, policy, error)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)"""
UPSERT_LINK = """INSERT INTO web_links (src_key, dst_key, text, rel) VALUES ($1, $2, $3, $4)
    ON CONFLICT (src_key, dst_key) DO UPDATE SET last_seen = NOW(), text = COALESCE(EXCLUDED.text, web_links.text)"""
UPSERT_CONTENT = """INSERT INTO web_content (content_hash, first_key, size, reader, simhash, b0, b1, b2, b3)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    ON CONFLICT (content_hash) DO UPDATE SET last_seen = NOW(), seen = web_content.seen + 1"""
INSERT_QUERY = """INSERT INTO web_queries (query, engine, caller, purpose, results, result_keys)
    VALUES ($1, $2, $3, $4, $5, $6)"""
#: a page ingest tells the map which fabric record holds a content hash (the first one wins)
NOTE_RECORD = """INSERT INTO web_content (content_hash, first_key, record_id) VALUES ($1, $2, $3)
    ON CONFLICT (content_hash) DO UPDATE SET record_id = COALESCE(web_content.record_id, EXCLUDED.record_id),
    last_seen = NOW()"""
LOOKUP_RECORD = "SELECT content_hash, first_key, record_id, seen FROM web_content WHERE content_hash = $1"
# WI-5: what the map already holds, for a batch
LOOKUP_HASHES = "SELECT content_hash FROM web_content WHERE content_hash = ANY($1)"
LOOKUP_BANDS = ("SELECT content_hash, simhash FROM web_content WHERE b0 = ANY($1) OR b1 = ANY($2) OR b2 = ANY($3) "
                "OR b3 = ANY($4) LIMIT 4000")
UPDATE_URL_NOVELTY = "UPDATE web_urls SET novelty = $2, novelty_kind = $3, canonical_hash = $4 WHERE url_key = $1"
UPDATE_HOST_YIELD = ("UPDATE web_hosts SET novel_yield = CASE WHEN novel_yield IS NULL THEN $2 "
                     "ELSE novel_yield * 0.9 + $2 * 0.1 END WHERE host = $1")

MAX_LINKS_PER_PAGE = 500
MAX_TITLE = 300


def _signed64(v: int) -> int:
    """A 64-bit simhash as the signed BIGINT Postgres stores."""
    v = int(v) & ((1 << 64) - 1)
    return v - (1 << 64) if v >= (1 << 63) else v


def unsigned64(v: Optional[int]) -> Optional[int]:
    return None if v is None else int(v) & ((1 << 64) - 1)


def enqueue_item(url: str, *, final_url: str = "", status: int = 0, content_type: str = "", text: str = "",
                 html: str = "", elapsed_ms: int = 0, caller: str = "", purpose: str = "", error: str = "",
                 etag: str = "", last_modified: str = "", title: str = "", from_cache: bool = False,
                 nbytes: Optional[int] = None, has_auth: bool = False, requested: str = "",
                 internal_hosts: Sequence[str] = (), ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """What the request path puts on the queue: the fetch result and its policy, nothing computed. None when
    the policy is 'none' (nothing is recorded, not even the host). Cheap by construction."""
    policy = ic.record_policy(url, purpose=purpose, has_auth=has_auth, requested=requested,
                              internal_hosts=internal_hosts)
    if policy == "none":
        return None
    body = html or text or ""
    return {"url": url, "final_url": final_url or url, "status": int(status or 0), "content_type": content_type or "",
            "text": text if policy == "full" else "", "html": html if policy == "full" else "",
            "elapsed_ms": int(elapsed_ms or 0), "caller": caller[:60], "purpose": purpose[:40],
            "error": (error or "")[:200], "etag": (etag or "")[:200], "last_modified": (last_modified or "")[:80],
            "title": (title or "")[:MAX_TITLE], "from_cache": bool(from_cache),
            "bytes": int(nbytes if nbytes is not None else len(body.encode("utf-8", "replace")) if body else 0),
            "policy": policy, "ts": float(ts if ts is not None else time.time())}


def rows_for(item: Dict[str, Any]) -> Dict[str, Any]:
    """The rows one queued item becomes (the part worth a thread: hashing, simhash, links). Returns the parameter
    tuples per statement; a 'meta' receipt has no content or links."""
    key = ic.url_key(item["url"])
    final_key = ic.url_key(item.get("final_url") or item["url"])
    host = ic.host_of(item.get("final_url") or item["url"]) or ic.host_of(item["url"])
    status = int(item.get("status") or 0)
    body_html, body_text = item.get("html") or "", item.get("text") or ""
    sniff = (body_html or body_text)[:4096].encode("utf-8", "replace")
    reader = ic.reader_for(item.get("content_type") or "", item.get("final_url") or item["url"], sniff) \
        if (status or item.get("content_type")) else ""
    kind = ic.kind_of(reader) if reader else ""
    full = item.get("policy") == "full" and bool(body_text or body_html) and 200 <= status < 300
    content_hash = simhash = None
    content_row = None
    links: List[Tuple[str, str, str, str]] = []
    if full:
        text_for_hash = body_text or body_html
        content_hash = ic.content_hash(text_for_hash)
        sh = ic.simhash64(text_for_hash)
        simhash = _signed64(sh)
        b = ic.lsh_bands(sh)
        content_row = (content_hash, key, int(item.get("bytes") or 0), reader, simhash, b[0], b[1], b[2], b[3])
        if body_html and reader == "html":
            for ln in ic.extract_links(body_html, item.get("final_url") or item["url"], limit=MAX_LINKS_PER_PAGE):
                if ln["key"] != key:
                    links.append((key, ln["key"], (ln.get("text") or "")[:120] or None, (ln.get("rel") or "")[:40] or None))
    err = 1 if (item.get("error") or status >= 400 or status == 0) else 0
    return {
        "host": (host, err, int(item.get("bytes") or 0)),
        "url": (key, host, status or None, (item.get("content_type") or "")[:120] or None, reader or None,
                kind or None, int(item.get("bytes") or 0) or None, item.get("etag") or None,
                item.get("last_modified") or None, content_hash, simhash, item.get("caller") or None,
                item.get("purpose") or None, item.get("policy"), final_key if final_key != key else None,
                item.get("title") or None),
        "fetch": (key, host, status or None, int(item.get("elapsed_ms") or 0), int(item.get("bytes") or 0),
                  bool(item.get("from_cache")), item.get("caller") or None, item.get("purpose") or None,
                  item.get("policy"), item.get("error") or None),
        "content": content_row,
        "links": links,
        "key": key, "content_hash": content_hash,
    }


def query_row(query: str, engine: str, caller: str, purpose: str, result_urls: Sequence[str]) -> Tuple[Any, ...]:
    keys = []
    for u in result_urls or ():
        k = ic.url_key(str(u))
        if k and k not in keys:
            keys.append(k)
    return ((query or "")[:500], (engine or "")[:40] or None, (caller or "")[:60] or None,
            (purpose or "")[:40] or None, len(keys), keys[:50])


# ── size budget (PLAN §12.7: a byte budget, 20 GB to start, alarm at 80 %) ────

SIZE_SQL = ("SELECT relname, pg_total_relation_size(relid) FROM pg_stat_user_tables WHERE relname = ANY($1)")


def budget_state(sizes: Dict[str, int], budget_gb: float) -> Dict[str, Any]:
    total = sum(int(v or 0) for v in sizes.values())
    budget = float(budget_gb or 0) * (1 << 30)
    share = (total / budget) if budget > 0 else 0.0
    return {"bytes": total, "budget_gb": budget_gb, "share": round(share, 3),
            "state": "over" if share >= 1.0 else ("warn" if share >= 0.8 else "ok"),
            "tables": {k: int(v or 0) for k, v in sizes.items()}}

# ── topic maps (WI-8) ─────────────────────────────────────────────────────────
UPSERT_TOPIC = """INSERT INTO web_topic (topic_key, name, intel_topic_id, seeds, queries, entities)
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (topic_key) DO UPDATE SET name = COALESCE(EXCLUDED.name, web_topic.name),
    intel_topic_id = COALESCE(EXCLUDED.intel_topic_id, web_topic.intel_topic_id),
    seeds = (SELECT ARRAY(SELECT DISTINCT x FROM unnest(web_topic.seeds || EXCLUDED.seeds) AS x)),
    queries = (SELECT ARRAY(SELECT DISTINCT x FROM unnest(web_topic.queries || EXCLUDED.queries) AS x)),
    entities = (SELECT ARRAY(SELECT DISTINCT x FROM unnest(web_topic.entities || EXCLUDED.entities) AS x)),
    updated = NOW()"""
LINK_TOPIC = {
    "dataset": "UPDATE web_topic SET dataset_ids = (SELECT ARRAY(SELECT DISTINCT x FROM unnest(dataset_ids || $2::text[]) AS x)), updated = NOW() WHERE topic_key = $1",
    "kb": "UPDATE web_topic SET kb_ids = (SELECT ARRAY(SELECT DISTINCT x FROM unnest(kb_ids || $2::text[]) AS x)), updated = NOW() WHERE topic_key = $1",
    "concept": "UPDATE web_topic SET worldview_concepts = (SELECT ARRAY(SELECT DISTINCT x FROM unnest(worldview_concepts || $2::text[]) AS x)), updated = NOW() WHERE topic_key = $1",
}
UPSERT_MEMBER = """INSERT INTO web_topic_members (topic_key, url_key, role, kind, relevance, novelty, source)
    VALUES ($1, $2, $3, $4, $5, $6, $7)
    ON CONFLICT (topic_key, url_key) DO UPDATE SET last_seen = NOW(),
    role = CASE WHEN EXCLUDED.role IN ('seed', 'authority', 'hub') THEN EXCLUDED.role ELSE web_topic_members.role END,
    kind = COALESCE(EXCLUDED.kind, web_topic_members.kind),
    relevance = COALESCE(EXCLUDED.relevance, web_topic_members.relevance),
    novelty = COALESCE(EXCLUDED.novelty, web_topic_members.novelty)"""
SELECT_TOPIC = "SELECT * FROM web_topic WHERE topic_key = $1"
SELECT_MEMBERS = ("SELECT m.url_key, m.role, COALESCE(m.kind, u.kind) AS kind, m.relevance, COALESCE(m.novelty, u.novelty) AS novelty, "
                  "m.last_seen, (m.last_seen > NOW() - INTERVAL '7 days') AS recent FROM web_topic_members m "
                  "LEFT JOIN web_urls u ON u.url_key = m.url_key WHERE m.topic_key = $1 ORDER BY m.last_seen DESC LIMIT $2")
SELECT_HOST_YIELDS = "SELECT host, novel_yield FROM web_hosts WHERE host = ANY($1)"
