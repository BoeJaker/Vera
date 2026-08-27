"""Resolve which discovery dataset a page URL belongs to â€” without stalling the loop.

`discovery._dataset_for_url` answers this with a leading-wildcard LIKE over the
`data` JSON blob of every row in `fabric_records` (909,039 rows / 1.37 GB as of
2026-08-26). That is unindexable by construction, measured at **2.6 s per call**,
and it is a full scan whether or not it matches. It ran **synchronously on the
event loop**, so one `web.fetch` stalled every other request in the instance â€”
`perf.scan` attributed 4 stalls to that single line, worst 50.5 s.

This module keeps the query's RESULT identical and fixes only how it is run:

  * `scan_for_dataset(conn, url)` â€” the original query, unchanged, so the answer
    does not move.
  * `ResolveCache` â€” per-URL memo with TTL, including NEGATIVE results, because a
    miss costs exactly as much as a hit (full scan either way) and re-fetching the
    same URL previously paid it again every time.
  * `resolve_cached(conn, url, cache)` â€” the two composed.

Callers on an async path must run this in a thread (`asyncio.to_thread`); it is
deliberately synchronous so the pure logic stays testable without an event loop.

**Deliberately NOT done here: the fast indexed lookup.** `fabric_pages` has an
indexed `url` column and answers in 0.12 ms (~21,800Ã— faster), but it is NOT
equivalent â€” checked against real data, 18 of 25 URLs resolved to a DIFFERENT
dataset, because one URL is ingested into several datasets and the two queries
pick different ones (the scan has no ORDER BY, so its own choice is arbitrary).
Swapping it in would silently re-file pages. That is a semantics decision for a
human, not a refactor; see documentation/PLAN-agentic-loop-next-pass.md Phase F.
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urlparse
from typing import Any, Dict, Optional, Tuple

# The original query, verbatim in behaviour: match the url inside the JSON blob,
# then confirm by parsing (the LIKE alone can match a substring of a longer url).
_SQL = 'SELECT dataset_id, data FROM fabric_records WHERE data LIKE ? LIMIT 8'

DEFAULT_TTL_S = 900.0


def scan_for_dataset(conn: Any, url: str) -> str:
    """The legacy scan. Same SQL, same confirmation, same return value."""
    if not url:
        return ""
    rows = conn.execute(_SQL, ('%"url": "' + url + '"%',)).fetchall()
    for r in rows:
        try:
            raw = r["data"] if not isinstance(r, tuple) else r[1]
        except Exception:
            continue
        try:
            d = json.loads(raw) if raw else {}
        except Exception:
            continue
        if (d.get("url") or "").split("#")[0] == url:
            try:
                return r["dataset_id"] if not isinstance(r, tuple) else r[0]
            except Exception:
                return ""
    return ""


class ResolveCache:
    """URL â†’ dataset memo with TTL, caching misses too.

    A miss is the expensive case (nothing short-circuits the scan), and the
    research loop re-fetches the same URLs constantly, so not caching negatives
    would leave the worst path uncached.
    """

    def __init__(self, ttl_s: float = DEFAULT_TTL_S, max_entries: int = 4096):
        self.ttl_s = float(ttl_s)
        self.max_entries = int(max_entries)
        self._d: Dict[str, Tuple[float, str]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, url: str, now: Optional[float] = None) -> Optional[str]:
        now = time.monotonic() if now is None else now
        ent = self._d.get(url)
        if ent is None:
            self.misses += 1
            return None
        ts, val = ent
        if now - ts > self.ttl_s:
            self._d.pop(url, None)
            self.misses += 1
            return None
        self.hits += 1
        return val

    def put(self, url: str, dataset_id: str, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        if len(self._d) >= self.max_entries:
            # Cheap eviction: drop the oldest quarter rather than track an LRU.
            for k in sorted(self._d, key=lambda k: self._d[k][0])[: self.max_entries // 4]:
                self._d.pop(k, None)
        self._d[url] = (now, dataset_id or "")

    def clear(self) -> None:
        self._d.clear()

    def __len__(self) -> int:
        return len(self._d)


def resolve_cached(conn: Any, url: str, cache: Optional[ResolveCache] = None,
                   now: Optional[float] = None) -> str:
    """Cached `scan_for_dataset`. Identical answer, paid for at most once per TTL."""
    if not url:
        return ""
    if cache is None:
        return scan_for_dataset(conn, url)
    hit = cache.get(url, now=now)
    if hit is not None:
        return hit
    val = scan_for_dataset(conn, url)
    cache.put(url, val, now=now)
    return val

def auto_dataset_for_url(url: str) -> str:
    """The per-domain dataset a web page falls into: "web.<host>".

    Moved here verbatim from discovery._auto_ds so there is ONE definition that
    is importable without the app (discovery pulls in the whole fabric stack),
    which is what lets `web.fetch`'s dataset choice be tested at all.
    """
    host = urlparse(url if url.startswith(("http://", "https://"))
                    else "https://" + url).netloc
    return "web." + re.sub(r"[^a-z0-9]", "_", host.lower())[:30]
