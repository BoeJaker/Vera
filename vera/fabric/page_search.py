"""Discovery's page search index in Postgres: sync from the fabric's SQLite pages, search for discover_recall
(four-minds FM-7.1 / Q5; see page_search_core for the design and why it is Postgres).

- sync_once(): the pages past the rowid watermark, upserted in batches; a job runs it every 2 minutes (prod and dev
  alike - the index is derived and cheap: 7k pages / 12 MB on 11 Oct 2026).
- search(): ranked pages for a query, or None when the index cannot answer (no pool, not built yet, an error) - the
  caller then keeps its LIKE path.
- discovery.recall_index.status / .sync: rows, watermark, lag; a manual sync.

The switch `discovery.recall_index` (off | pg, default off) decides whether discover_recall uses search().
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, schedule

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.fabric import page_search_core as PS
except Exception:                        # worktree / app-free import
    from vera.fabric import page_search_core as PS   # type: ignore

log = logging.getLogger("vera.fabric.page_search")


def _df():
    """The LIVE data_fabric module (the one the loader connected), never a fresh import of a second copy."""
    import sys
    m = sys.modules.get("data_fabric") or sys.modules.get("Vera.vera.fabric.data_fabric")
    if m is None:
        raise RuntimeError("data_fabric is not loaded yet")
    return m
BATCH = 500
MAX_PER_RUN = 20_000
_SCHEMA_READY = False
_LOCK = asyncio.Lock()


def _pool():
    return getattr(_orch, "PG_POOL", None)


async def _ensure_schema(pool) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    async with pool.acquire() as conn:
        for stmt in PS.DDL:
            await conn.execute(stmt)
    _SCHEMA_READY = True


def _sqlite_batch(after: int, n: int) -> List[Any]:
    df = _df()
    conn = df._sqlite_conn()
    return conn.execute(PS.SQLITE_BATCH, (int(after), int(n))).fetchall()


def _sqlite_extent() -> List[int]:
    df = _df()
    conn = df._sqlite_conn()
    row = conn.execute(PS.SQLITE_MAX_ROWID).fetchone()
    return [int(row[0] or 0), int(row[1] or 0)]


async def sync_once(max_rows: int = MAX_PER_RUN) -> Dict[str, Any]:
    """Copy the pages past the watermark into the index (bounded per run). Serialised: one sync at a time."""
    pool = _pool()
    if pool is None:
        return {"ok": False, "error": "postgres unavailable"}
    if _LOCK.locked():
        return {"ok": True, "skipped": "a sync is already running"}
    async with _LOCK:
        await _ensure_schema(pool)
        async with pool.acquire() as conn:
            wm = int(await conn.fetchval(PS.GET_WATERMARK, PS.WATERMARK_KEY) or 0)
        start = wm
        # re-read the watermark row itself: a REPLACE of the newest page deletes it and re-inserts it at the same
        # rowid (SQLite gives max(rowid)+1, and the max was the row it just deleted), so `rowid > wm` would miss it
        after = max(0, wm - 1)
        copied = 0
        while copied < max_rows:
            n = min(BATCH, max_rows - copied)
            rows = await asyncio.to_thread(_sqlite_batch, after, n)
            if not rows:
                break
            args, top = PS.to_rows(rows)
            async with pool.acquire() as conn:
                async with conn.transaction():
                    if args:
                        await conn.executemany(PS.UPSERT, args)
                    await conn.execute(PS.SET_WATERMARK, PS.WATERMARK_KEY, top)
            copied += len(rows)
            after = wm = max(wm, top)
            if len(rows) < n:
                break
        return {"ok": True, "copied": copied, "new": PS.lag(wm, start), "watermark": wm}


async def search(query: str, dataset_id: str = "", limit: int = 20) -> Optional[List[Dict[str, Any]]]:
    """Ranked pages for a query, or None when the index cannot answer (the caller keeps its LIKE path)."""
    q = PS.or_tsquery(query)
    pool = _pool()
    if not q or pool is None:
        return None
    try:
        await _ensure_schema(pool)
        async with pool.acquire() as conn:
            if not await conn.fetchval(PS.GET_WATERMARK, PS.WATERMARK_KEY):
                return None                   # never synced: the index would answer "nothing"
            if dataset_id:
                rows = await conn.fetch(PS.search_sql(True), q, int(limit), dataset_id)
            else:
                rows = await conn.fetch(PS.search_sql(False), q, int(limit))
        return [PS.page_dict(r) for r in rows]
    except Exception as e:
        log.debug("page search unavailable: %s", e)
        return None


async def _sync_job() -> None:
    try:
        if getattr(_orch, "_IS_WORKER", False):
            return
        res = await sync_once()
        if res.get("new"):
            log.info("page search index: %d new page rowids synced (watermark %s)", res["new"], res.get("watermark"))
    except Exception as e:
        log.warning("page search sync: %s", e)


schedule(_sync_job, interval=120, name="discovery_page_search_sync", singleton=True)


try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.minds import contract_core as _MC  # type: ignore


@capability(
    "discovery.recall_index.status", http_method="GET", http_path="/discovery/recall_index/status",
    contract=_MC.contract("discovery.recall_index.status", effects=["read"], idempotency="idempotent"),
    http_tags=["fabric", "discovery"], memory="off", silent=True,
    description="The discovery page search index (Postgres, four-minds FM-7.1): {ok, rows, watermark, "
                "sqlite_max_rowid, sqlite_pages, lag} - lag is how many page rowids the index is behind.",
)
async def cap_recall_index_status(trace_id=None) -> Dict[str, Any]:
    pool = _pool()
    if pool is None:
        return {"ok": False, "error": "postgres unavailable"}
    await _ensure_schema(pool)
    async with pool.acquire() as conn:
        rows = await conn.fetchval(f"SELECT count(*) FROM {PS.TABLE}")
        wm = await conn.fetchval(PS.GET_WATERMARK, PS.WATERMARK_KEY) or 0
    top, n = await asyncio.to_thread(_sqlite_extent)
    return {"ok": True, "rows": int(rows or 0), "watermark": int(wm), "sqlite_max_rowid": top, "sqlite_pages": n,
            "lag": PS.lag(top, wm)}


@capability(
    "discovery.recall_index.sync", http_method="POST", http_path="/discovery/recall_index/sync",
    contract=_MC.contract("discovery.recall_index.sync", effects=["read", "write"], idempotency="idempotent"),
    http_tags=["fabric", "discovery"], memory="off",
    description="Copy discovery pages past the watermark into the Postgres search index now (the job does it every "
                "2 minutes). Input: max_rows (int=20000). Output: {ok, copied, watermark}.",
)
async def cap_recall_index_sync(max_rows: int = MAX_PER_RUN, trace_id=None) -> Dict[str, Any]:
    return await sync_once(int(max_rows or MAX_PER_RUN))


@capability(
    "discovery.recall_index.search", http_method="POST", http_path="/discovery/recall_index/search",
    contract=_MC.contract("discovery.recall_index.search", effects=["read"], idempotency="idempotent"),
    http_tags=["fabric", "discovery"], memory="off", silent=True,
    description="Ranked discovery pages for a query from the Postgres page index (any word matches, more matches "
                "rank higher; title weighs above text). Inputs: query (str!), dataset_id (str), limit (int=20). "
                "Output: {ok, pages:[{url, title, full_text, dataset_id, word_count, rank}]} | {ok:false} when the "
                "index cannot answer (discover_recall then keeps its LIKE path).",
)
async def cap_recall_index_search(query: str = "", dataset_id: str = "", limit: int = 20,
                                  trace_id=None) -> Dict[str, Any]:
    pages = await search(query, dataset_id=dataset_id, limit=max(1, min(int(limit or 20), 200)))
    return {"ok": pages is not None, "pages": pages or []}
