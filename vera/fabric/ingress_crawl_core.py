"""The node crawl class's pure half (shared-planning web-ingress WI-12; PLAN §13.2).

The host splits a list of URLs into small per-host batches; each batch is one `ingress.fetch.batch` call, which the
orchestrator sends to an idle node worker of the `crawl` class or runs on the host when none is free. Batches are
interleaved across hosts so that batches running at the same time rarely share a host, and every fetch still waits for
the host's slot in the shared Redis bucket, so however many fetchers run, one host sees at most one request per
interval. Nodes fetch only: the host records the receipts and keeps every store.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Sequence

try:
    from Vera.vera.fabric import ingress_core as ic
except ImportError:                       # worktree / app-free import
    from vera.fabric import ingress_core as ic

PER_BATCH = 3
MAX_BATCH = 10
MAX_BODY_CHARS = 400_000
TEXTUAL = ("text/", "application/xhtml", "application/xml", "application/json", "application/rss",
           "application/atom", "application/ld+json", "+xml", "+json")


def plan_batches(urls: Iterable[str], per_batch: int = PER_BATCH) -> List[List[str]]:
    """Unique external http(s) URLs (by url_key) cut into batches of at most `per_batch`, each batch one host, the
    batches of different hosts interleaved (host A's first, host B's first, ..., host A's second ...)."""
    per = max(1, min(int(per_batch or PER_BATCH), MAX_BATCH))
    by_host: Dict[str, List[str]] = {}
    seen = set()
    for u in urls or ():
        u = str(u or "").strip()
        if not u.startswith(("http://", "https://")):
            continue
        k = ic.url_key(u)
        if not k or k in seen:
            continue
        seen.add(k)
        by_host.setdefault(ic.host_of(u), []).append(u)
    chunks = {h: [us[i:i + per] for i in range(0, len(us), per)] for h, us in by_host.items()}
    out: List[List[str]] = []
    while chunks:
        for h in list(chunks):
            out.append(chunks[h].pop(0))
            if not chunks[h]:
                del chunks[h]
    return out


def is_textual(content_type: str) -> bool:
    ct = str(content_type or "").lower()
    return any(t in ct for t in TEXTUAL)


def fetch_row(url: str, *, final_url: str = "", status: int = 0, content_type: str = "", body: str = "",
              elapsed_ms: int = 0, error: str = "", etag: str = "", last_modified: str = "",
              max_chars: int = MAX_BODY_CHARS) -> Dict[str, Any]:
    """One fetch as it travels back to the host - the shape web_client.fetch_page results have, so the host's
    receipt hook takes it unchanged. A body that is not text, or that decodes to binary, travels as ''."""
    ct = str(content_type or "")
    text = str(body or "") if is_textual(ct) else ""
    err = str(error or "")
    if text and ic.looks_binary(text):
        text, err = "", err or "binary body"
    text = text[:max(1, int(max_chars or MAX_BODY_CHARS))]
    html = "html" in ct.lower()
    return {"url": url, "final_url": final_url or url, "status": int(status or 0), "content_type": ct,
            "html": text if html else "", "text": "" if html else text, "elapsed_ms": int(elapsed_ms or 0),
            "error": err, "etag": etag or "", "last_modified": last_modified or "", "bytes": len(text)}


def summary(batches: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Where the batches ran and how the fetches went: {ran_on: {where: batches}, fetched, failed, retried}."""
    ran: Dict[str, int] = {}
    fetched = failed = retried = 0
    for b in batches or ():
        where = str(b.get("ran_on") or "?")
        ran[where] = ran.get(where, 0) + 1
        retried += int(b.get("attempts") or 1) > 1
        for r in b.get("results") or ():
            ok = 200 <= int(r.get("status") or 0) < 400 and not r.get("error")
            fetched += ok
            failed += not ok
    return {"ran_on": ran, "fetched": fetched, "failed": failed, "retried": retried}


def lost(urls: Iterable[str], batches: Sequence[Mapping[str, Any]]) -> List[str]:
    """Planned URLs with no result row at all - a batch that died and was not retried. Must stay empty."""
    got = {ic.url_key(str(r.get("url") or "")) for b in batches or () for r in b.get("results") or ()}
    return [u for u in (urls or ()) if ic.url_key(u) not in got]


def failed_row(url: str, error: str) -> Dict[str, Any]:
    return fetch_row(url, error=error or "batch failed")


def where(hostname: str, is_worker: bool) -> str:
    return ("node:" if is_worker else "host:") + (str(hostname or "") or "?")

