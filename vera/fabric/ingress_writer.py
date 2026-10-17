"""The receipt writer (web-ingress WI-3): a bounded in-memory queue the request path drops fetch results on, and
one background task that turns them into rows (in a thread) and writes them to Postgres in batches.

The contract the plan sets (PLAN §0.2, §6): enqueue costs the caller microseconds and never blocks; when the queue
is full the receipt is dropped and counted, never waited for; a store that is down costs nothing but a counter.
No Vera imports: the pool comes from a getter the capabilities module hands in, so this is testable with a fake.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

try:
    from Vera.vera.fabric import ingress_receipts_core as rc
except ImportError:  # pragma: no cover
    from vera.fabric import ingress_receipts_core as rc

log = logging.getLogger("vera.ingress")

PoolGetter = Callable[[], Any]
ModeGetter = Callable[[], str]

QUEUE_MAX = 5000
BATCH_MAX = 500
FLUSH_EVERY_S = 0.25
DDL_LOCK_TIMEOUT = "3s"


class ReceiptWriter:
    def __init__(self, pool_getter: PoolGetter, *, queue_max: int = QUEUE_MAX, batch_max: int = BATCH_MAX,
                 flush_every_s: float = FLUSH_EVERY_S, rows_in_thread: bool = True) -> None:
        self._pool_getter = pool_getter
        self._q: Optional[asyncio.Queue] = None
        self._queue_max, self._batch_max, self._every = int(queue_max), int(batch_max), float(flush_every_s)
        self._task: Optional[asyncio.Task] = None
        self._ddl_done = False
        self._rows_in_thread = rows_in_thread
        self.stats: Dict[str, Any] = {"enqueued": 0, "dropped_full": 0, "dropped_no_store": 0, "flushed": 0,
                                      "batches": 0, "errors": 0, "last_flush_ms": 0, "last_error": "",
                                      "queries": 0, "links": 0, "content": 0}

    # ── the request path ─────────────────────────────────────────────────────
    def enqueue(self, item: Dict[str, Any]) -> bool:
        """Put one fetch result (or a {'query': ...} row) on the queue. Never blocks, never raises."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:                     # no running loop: nothing could drain it
            self.stats["dropped_no_store"] += 1
            return False
        q = self._queue()
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            self.stats["dropped_full"] += 1
            return False
        self.stats["enqueued"] += 1
        self._ensure_task(loop)
        return True

    def _queue(self) -> asyncio.Queue:
        if self._q is None:
            self._q = asyncio.Queue(maxsize=self._queue_max)
        return self._q

    def _ensure_task(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        if self._task is None or self._task.done():
            self._task = (loop or asyncio.get_running_loop()).create_task(self._run())

    @property
    def depth(self) -> int:
        return self._q.qsize() if self._q is not None else 0

    # ── the background task ──────────────────────────────────────────────────
    async def _run(self) -> None:
        try:
            while True:
                batch = await self._take_batch()
                if batch:
                    await self.flush(batch)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the writer must outlive any one bad batch
            self.stats["errors"] += 1
            self.stats["last_error"] = str(e)[:200]
            log.warning("ingress writer: %s", e)

    async def _take_batch(self) -> List[Dict[str, Any]]:
        q = self._queue()
        first = await q.get()
        batch = [first]
        deadline = time.monotonic() + self._every
        while len(batch) < self._batch_max:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(q.get(), timeout=left))
            except asyncio.TimeoutError:
                break
        return batch

    async def flush(self, batch: List[Dict[str, Any]]) -> Dict[str, int]:
        """Write one batch. Rows are computed in a thread (hashes, links); the writes are one transaction."""
        pool = self._pool_getter()
        if pool is None:
            self.stats["dropped_no_store"] += len(batch)
            return {"dropped": len(batch)}
        t0 = time.monotonic()
        fetches = [b for b in batch if "query" not in b and "record" not in b]
        queries = [b for b in batch if "query" in b]
        notes = [(b["record"]["content_hash"], b["record"].get("key") or "", b["record"]["record_id"])
                 for b in batch if "record" in b and b["record"].get("content_hash") and b["record"].get("record_id")]
        if self._rows_in_thread:
            rows = await asyncio.to_thread(lambda: [rc.rows_for(b) for b in fetches])
        else:
            rows = [rc.rows_for(b) for b in fetches]
        hosts = [r["host"] for r in rows]
        urls = [r["url"] for r in rows]
        fetched = [r["fetch"] for r in rows]
        content = [r["content"] for r in rows if r["content"]]
        links = [ln for r in rows for ln in r["links"]]
        qrows = [rc.query_row(b["query"], b.get("engine", ""), b.get("caller", ""), b.get("purpose", ""),
                              b.get("result_urls") or []) for b in queries]
        try:
            async with pool.acquire() as conn:
                if not self._ddl_done:
                    await self._ddl(conn)
                async with conn.transaction():
                    if hosts:
                        await conn.executemany(rc.UPSERT_HOST, hosts)
                        await conn.executemany(rc.UPSERT_URL, urls)
                        await conn.executemany(rc.INSERT_FETCH, fetched)
                    if content:
                        await conn.executemany(rc.UPSERT_CONTENT, content)
                    if links:
                        await conn.executemany(rc.UPSERT_LINK, links)
                    if qrows:
                        await conn.executemany(rc.INSERT_QUERY, qrows)
                    if notes:
                        await conn.executemany(rc.NOTE_RECORD, notes)
        except Exception as e:  # noqa: BLE001
            self.stats["errors"] += 1
            self.stats["last_error"] = str(e)[:200]
            log.warning("ingress flush of %d: %s", len(batch), e)
            return {"error": 1}
        self.stats["flushed"] += len(fetched)
        self.stats["queries"] += len(qrows)
        self.stats["links"] += len(links)
        self.stats["content"] += len(content)
        self.stats["batches"] += 1
        self.stats["last_flush_ms"] = int((time.monotonic() - t0) * 1000)
        return {"fetches": len(fetched), "links": len(links), "content": len(content), "queries": len(qrows)}

    async def _ddl(self, conn: Any) -> None:
        try:
            await conn.execute(f"SET lock_timeout = '{DDL_LOCK_TIMEOUT}'")
        except Exception:  # noqa: BLE001
            pass
        for stmt in rc.DDL:
            await conn.execute(stmt)
        self._ddl_done = True

    async def drain(self, timeout_s: float = 5.0) -> None:
        """Flush whatever is queued now (tests, shutdown)."""
        q = self._queue()
        t0 = time.monotonic()
        while not q.empty() and time.monotonic() - t0 < timeout_s:
            batch = []
            while not q.empty() and len(batch) < self._batch_max:
                batch.append(q.get_nowait())
            await self.flush(batch)

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
