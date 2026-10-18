"""Pure row and query shapes for discovery's stores (shared-planning web-ingress WI-1 = DEEP-07 D07-1).

The silent-correctness set in discovery.py was SQL that named columns the tables do not have, a queue entry of
the wrong arity, a Neo4j purge keyed on a property that is never written, and deletes issued on connections that
were never committed. Each shape now lives here, where a temp SQLite with the real schema can prove it, and
discovery.py only calls it. No I/O.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# ── the crawl frontier (B2) ───────────────────────────────────────────────────

QUEUE_ARITY = 6     # (priority, seq, url, depth, dry_streak, parent_url)


def queue_item(item: Any, next_seq: Callable[[], int]) -> Tuple[float, int, str, int, int, Optional[str]]:
    """One frontier entry in the crawl's shape, whatever a caller handed in: the full 6-tuple, the legacy
    (url, depth, dry_streak[, parent]) a surface expand or a saved frontier passes, or a bare URL. A heap of mixed
    shapes compared strings with floats and raised in the main loop (fabric.discover.expand, every time)."""
    if isinstance(item, (list, tuple)):
        t = tuple(item)
        if len(t) == QUEUE_ARITY:
            pr, seq, url, depth, dry, parent = t
            return (float(pr), int(seq), str(url), int(depth or 0), int(dry or 0), parent or None)
        if len(t) == 5:
            pr, seq, url, depth, dry = t
            return (float(pr), int(seq), str(url), int(depth or 0), int(dry or 0), None)
        if 3 <= len(t) <= 4:
            url, depth, dry = t[0], t[1], t[2]
            parent = t[3] if len(t) == 4 else None
            return (-1.0, int(next_seq()), str(url), int(depth or 0), int(dry or 0), parent or None)
        if len(t) == 2:
            return (-1.0, int(next_seq()), str(t[0]), int(t[1] or 0), 0, None)
        if len(t) == 1:
            return (-1.0, int(next_seq()), str(t[0]), 0, 0, None)
        raise ValueError(f"queue entry of arity {len(t)}: {t!r}")
    return (-1.0, int(next_seq()), str(item), 0, 0, None)


def normalise_queue(queue: Iterable[Any], next_seq: Callable[[], int]) -> List[Tuple[float, int, str, int, int, Optional[str]]]:
    return [queue_item(q, next_seq) for q in (queue or [])]


# ── fabric_pages (B1, B18) ────────────────────────────────────────────────────

def page_id(url: str) -> str:
    return "page:" + hashlib.sha1(str(url or "").encode()).hexdigest()[:16]


FABRIC_PAGE_UPSERT = (
    "INSERT INTO fabric_pages (id, dataset_id, crawl_id, url, title, full_text, tags, headings, word_count, created_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,datetime('now')) "
    "ON CONFLICT(id) DO UPDATE SET dataset_id=excluded.dataset_id, crawl_id=excluded.crawl_id, url=excluded.url, "
    "title=excluded.title, full_text=excluded.full_text, tags=excluded.tags, headings=excluded.headings, "
    "word_count=excluded.word_count, created_at=excluded.created_at "
    "WHERE excluded.crawl_id != '' OR length(excluded.full_text) >= length(coalesce(fabric_pages.full_text, ''))"
)


def fabric_page_row(ds_id: str, crawl_id: str, url: str, title: str, full_text: str, tags: Sequence[str],
                    headings: Sequence[Any], word_count: Any) -> Tuple[str, Tuple[Any, ...]]:
    """The one way a page reaches fabric_pages (crawls never wrote it: B1). A crawl (crawl_id set) always writes the
    page as it is now, even when it got shorter; a write from no crawl (a search snippet) only replaces a shorter
    text, so it can no longer overwrite a full page (B18; review 18 Oct: a page that shrank was stuck)."""
    return FABRIC_PAGE_UPSERT, (
        page_id(url), ds_id or "", crawl_id or "", url or "", (title or "")[:500], full_text or "",
        ",".join(str(t) for t in list(tags or [])[:20]), json.dumps(list(headings or [])[:40]),
        int(word_count or 0),
    )


# ── fabric_records as it is (B3, B4) ──────────────────────────────────────────

RECENT_RECORDS_SQL = "SELECT text, data FROM fabric_records WHERE dataset_id=? ORDER BY ROWID DESC LIMIT ?"


def record_summaries(rows: Iterable[Any], limit: int = 25) -> List[str]:
    """'title: first words' per record from the columns fabric_records has (text, data JSON); the steering
    prompt used to SELECT title, description, which do not exist, and saw nothing (B3)."""
    out: List[str] = []
    for r in rows or []:
        try:
            text = r["text"] if not isinstance(r, tuple) else r[0]
            raw = r["data"] if not isinstance(r, tuple) else r[1]
        except (KeyError, IndexError, TypeError):
            continue
        try:
            d = json.loads(raw) if isinstance(raw, str) and raw else (raw if isinstance(raw, dict) else {})
        except ValueError:
            d = {}
        title = str(d.get("title") or "").strip()
        body = str(d.get("description") or d.get("summary") or text or "").strip()
        if not title and not body:
            continue
        out.append((f"{title}: {body[:120]}" if title else body[:140]).strip())
        if len(out) >= limit:
            break
    return out


COMPILED_RECORD_INSERT = ("INSERT OR REPLACE INTO fabric_records (id, dataset_id, text, data, source_id, tags, "
                          "created_at) VALUES (?,?,?,?,?,?,datetime('now'))")


def compiled_record_row(ds_id: str, title: str, document: str, pages_used: int = 0) -> Tuple[str, Tuple[Any, ...]]:
    """The compiled document as a fabric record in the real columns; the old INSERT named url, kind and title as
    columns, failed at debug level and returned a stored_id for nothing (B4). The id is stable per dataset so a
    recompile replaces the last one."""
    rid = "compiled:" + hashlib.sha1(str(ds_id or "").encode()).hexdigest()[:20]
    data = {"url": "compiled:" + str(ds_id or ""), "kind": "document", "title": title or "", "pages_used": int(pages_used or 0),
            "source": "fabric_discovery"}
    return COMPILED_RECORD_INSERT, (rid, ds_id or "", document or "", json.dumps(data), "fabric_discovery",
                                    "compiled,document")


TOPIC_FROM_FRONTIER_SQL = ("SELECT config FROM fabric_discovery_frontier WHERE dataset_id=? "
                           "ORDER BY updated_at DESC LIMIT 1")


def topic_from_frontier(config_json: Any, ds_id: str = "") -> str:
    """The topic a dataset was crawled for, from its latest frontier's config (fabric_crawl_history, which
    compile read, exists nowhere: B4)."""
    try:
        cfg = json.loads(config_json) if isinstance(config_json, str) and config_json else (config_json or {})
    except ValueError:
        cfg = {}
    t = str((cfg or {}).get("topic") or "").strip()
    return t or str(ds_id or "").replace("topic.", "").replace("_", " ").strip()


# ── the Neo4j purge of a scan (B5) ────────────────────────────────────────────

SCAN_SURFACE_IDS_SQL = "SELECT id FROM fabric_surfaces WHERE crawl_id=?"
SCAN_SUBTABLE_IDS_SQL = "SELECT sub_dataset FROM fabric_subtables WHERE crawl_id=?"


def purge_cypher(surface_ids: Iterable[str], subtable_dataset_ids: Iterable[str]) -> List[Tuple[str, Dict[str, Any]]]:
    """The graph nodes a deleted scan leaves behind, by the ids the SQLite rows hold. Surface nodes carry no
    crawl_id and sub-tables are Dataset nodes, so the old MATCH on {crawl_id} / :Subtable deleted nothing (B5)."""
    out: List[Tuple[str, Dict[str, Any]]] = []
    s = [str(x) for x in surface_ids if x]
    d = [str(x) for x in subtable_dataset_ids if x]
    if s:
        out.append(("MATCH (s:Surface) WHERE s.id IN $ids DETACH DELETE s", {"ids": s}))
    if d:
        out.append(("MATCH (d:Dataset) WHERE d.id IN $ids DETACH DELETE d", {"ids": d}))
    return out


# ── overwrite (DATA-01) ───────────────────────────────────────────────────────

OVERWRITE_DELETES: Tuple[str, ...] = (
    "DELETE FROM fabric_records WHERE dataset_id=?",
    "DELETE FROM fabric_pages WHERE dataset_id=?",
    "DELETE FROM fabric_discovery_edges WHERE parent_dataset=?",
    "DELETE FROM fabric_surfaces WHERE parent_dataset=?",
    "DELETE FROM fabric_entities WHERE id IN (SELECT entity_id FROM fabric_entity_mentions WHERE dataset_id=?)",
    "DELETE FROM fabric_entity_mentions WHERE dataset_id=?",
    "DELETE FROM fabric_discovery_frontier WHERE dataset_id=?",
)


def overwrite_dataset(conn: Any, ds_id: str) -> Dict[str, int]:
    """Clear a dataset's discovery rows on ONE connection and commit it. The old code opened a fresh connection
    per statement and committed a seventh, so nothing was deleted (DATA-01). The entity delete runs before the
    mentions delete it depends on. Returns rows deleted per table (-1 = the table is absent)."""
    out: Dict[str, int] = {}
    for sql in OVERWRITE_DELETES:
        table = sql.split(" FROM ", 1)[1].split(" ", 1)[0]
        try:
            out[table] = conn.execute(sql, (ds_id,)).rowcount
        except Exception:  # noqa: BLE001 - a missing optional table is not a reason to keep the rest
            out[table] = -1
    conn.commit()
    return out


# ── acquire resume (B11) ──────────────────────────────────────────────────────

def continue_kwargs(acq: Dict[str, Any], additional_pages: int) -> Dict[str, Any]:
    """The kwargs fabric.web.continue hands fabric.web.acquire: the saved config AND the saved visited set, so
    the next run goes on from where the last one stopped instead of re-fetching the same pages (B11)."""
    try:
        config = json.loads(acq.get("config") or "{}") or {}
    except ValueError:
        config = {}
    try:
        state = json.loads(acq.get("state") or "{}") or {}
    except ValueError:
        state = {}
    return {
        "url": acq.get("source_url") or "", "dataset_id": acq.get("dataset_id") or "",
        "max_pages": max(1, int(additional_pages or 50)), "max_depth": int(config.get("max_depth", 4) or 4),
        "negative_words": config.get("negative_words", ""), "negative_urls": config.get("negative_urls", ""),
        "topic": config.get("topic", ""), "topic_dropoff": int(config.get("topic_dropoff", 3) or 3),
        "extract_entities": True, "fetch_content": True, "same_domain": bool(config.get("same_domain", True)),
        "tags": config.get("tags", ""), "resume_visited": [str(u) for u in (state.get("visited") or [])],
    }
