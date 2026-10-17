"""The ingress gateway's live half (shared-planning web-ingress WI-3): the receipt hook on web_client, the writer
into the web map (Postgres, through the fabric's pool), the `ingress.recording` switch, and the `fabric.map.*`
read capabilities.

Record levels (switchboard `ingress.recording`): 'off' (default: today's behaviour, nothing written), 'meta'
(hosts, URLs and fetches - never a body), 'on' (plus content hashes and links for external pages, per the
owner's 11 Oct decision). The policy in ingress_core can only lower what the switch allows.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any, Dict, List, Optional

from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability

try:
    from Vera.vera.fabric import ingress_core as ic
    from Vera.vera.fabric import ingress_receipts_core as rc
    from Vera.vera.fabric import ingress_writer as iw
    from Vera.vera.web import web_client as _wc
except ImportError:                       # loaded from its file under a stand-in orchestration (tests)
    from vera.fabric import ingress_core as ic
    from vera.fabric import ingress_receipts_core as rc
    from vera.fabric import ingress_writer as iw
    from vera.web import web_client as _wc

log = logging.getLogger("vera.ingress")

SWITCH = "ingress.recording"
SWITCH_TTL_S = 15.0
DEFAULT_BUDGET_GB = 20.0
#: estate hosts the configuration names on top of ingress_core's rules (Redis-free: an env/config list later)
INTERNAL_HOSTS: List[str] = []

_SW: Dict[str, Any] = {"at": 0.0, "mode": "off", "task": None}


def _has(name: str) -> bool:
    return bool((CAPABILITY_REGISTRY.get(name) or {}).get("func"))


async def _call(name: str, /, **kw) -> Dict[str, Any]:
    fn = (CAPABILITY_REGISTRY.get(name) or {}).get("func")
    if not fn:
        return {}
    r = await fn(**kw)
    return r if isinstance(r, dict) else {}


def _data_fabric():
    """The one loaded data_fabric (never imported by its full name: a second copy runs the loader twice)."""
    return sys.modules.get("data_fabric") or sys.modules.get("Vera.vera.fabric.data_fabric")


def _pool():
    df = _data_fabric()
    pg = getattr(df, "FABRIC_PG", None) if df else None
    return getattr(pg, "_pool", None)


WRITER = iw.ReceiptWriter(_pool)


# ── the switch ───────────────────────────────────────────────────────────────

def mode() -> str:
    """The recording level now: a cached read of the global switch layer, refreshed in the background at most
    every SWITCH_TTL_S seconds; 'off' until the first read lands."""
    now = time.monotonic()
    if now - _SW["at"] >= SWITCH_TTL_S and (_SW["task"] is None or _SW["task"].done()) and _has("minds.effective"):
        try:
            _SW["task"] = asyncio.get_running_loop().create_task(_refresh())
            _SW["at"] = now
        except RuntimeError:
            pass
    return _SW["mode"]


async def _refresh() -> None:
    try:
        eff = await _call("minds.effective")
        m = str(((eff or {}).get("modes") or {}).get(SWITCH) or "off")
        _SW["mode"] = m if m in ("off", "meta", "on") else "off"
    except Exception as ex:  # noqa: BLE001
        log.debug("ingress switch read: %s", ex)


def _requested_for(m: str) -> str:
    return {"on": "full", "meta": "meta"}.get(m, "none")


# ── the hook: every fetch_page result ────────────────────────────────────────

def record_fetch_result(fp: Dict[str, Any], *, caller: str = "", purpose: str = "") -> bool:
    """Enqueue one fetch result as a receipt. Synchronous and cheap; the writer does the rest off the path."""
    m = mode()
    if m == "off":
        return False
    item = rc.enqueue_item(
        fp.get("url") or "", final_url=fp.get("final_url") or "", status=int(fp.get("status") or 0),
        content_type=fp.get("content_type") or "", text=fp.get("text") or "", html=fp.get("html") or "",
        elapsed_ms=int(fp.get("elapsed_ms") or 0), caller=caller or fp.get("caller") or "web_client",
        purpose=purpose or fp.get("purpose") or "page", error=fp.get("error") or fp.get("block_reason") or "",
        etag=fp.get("etag") or "", last_modified=fp.get("last_modified") or "", title=fp.get("title") or "",
        from_cache=bool(fp.get("from_cache")), requested=_requested_for(m), internal_hosts=INTERNAL_HOSTS)
    if item is None:
        return False
    return WRITER.enqueue(item)


def record_response(url: str, *, status: int, content_type: str, body: str, elapsed_ms: int, caller: str,
                    purpose: str = "page", final_url: str = "", error: str = "", headers: Optional[Dict[str, str]] = None) -> bool:
    """For callers that fetch without web_client (http.get today, the pullers later)."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    return record_fetch_result({"url": url, "final_url": final_url or url, "status": status, "content_type": content_type,
                                "html": body if "html" in (content_type or "") else "",
                                "text": "" if "html" in (content_type or "") else body, "elapsed_ms": elapsed_ms,
                                "error": error, "etag": h.get("etag", ""), "last_modified": h.get("last-modified", "")},
                               caller=caller, purpose=purpose)


def record_query(query: str, *, engine: str, caller: str, result_urls: List[str], purpose: str = "search") -> bool:
    """A search Vera sent (owner, 11 Oct: record them). Any level above 'off'."""
    if mode() == "off" or not query:
        return False
    return WRITER.enqueue({"query": query, "engine": engine, "caller": caller, "purpose": purpose,
                           "result_urls": list(result_urls or [])[:50]})


if record_fetch_result not in _wc.FETCH_HOOKS:
    _wc.FETCH_HOOKS.append(record_fetch_result)


# ── capabilities ─────────────────────────────────────────────────────────────

@capability("fabric.ingress.status", http_method="GET", http_path="/fabric/ingress/status", http_tags=["fabric", "ingress"],
            memory="off", silent=True,
            description="The ingress gateway's state: the recording level (switch ingress.recording: off | meta | on), "
                        "the receipt queue depth, rows flushed and dropped, the last error, and the web map's size "
                        "against its budget. Input: none. Output: {mode, queue_depth, stats, budget}.")
async def cap_ingress_status(trace_id=None):
    out = {"mode": mode(), "queue_depth": WRITER.depth, "stats": dict(WRITER.stats), "budget": None,
           "store": bool(_pool())}
    pool = _pool()
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(rc.SIZE_SQL, list(rc.MAP_TABLES))
            out["budget"] = rc.budget_state({r[0]: r[1] for r in rows}, DEFAULT_BUDGET_GB)
        except Exception as e:  # noqa: BLE001
            out["budget"] = {"error": str(e)[:160]}
    return out


@capability("fabric.map.url", http_method="GET", http_path="/fabric/map/url", http_tags=["fabric", "ingress"],
            memory="off", silent=True,
            description="What the web map knows about one URL: its row (first/last seen, status, reader, kind, "
                        "content hash, how often fetched and changed, who last asked) and the last fetches. "
                        "Input: url (str!), fetches (int, default 10). Output: {key, url, fetches, links_out, links_in}.")
async def cap_map_url(url: str = "", fetches: int = 10, trace_id=None):
    key = ic.url_key(url)
    if not key:
        return {"error": "url required"}
    pool = _pool()
    if pool is None:
        return {"key": key, "url": None, "error": "the web map's store is not connected"}
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM web_urls WHERE url_key = $1", key)
            fs = await conn.fetch("SELECT ts, status, ms, bytes, caller, purpose, error FROM web_fetches "
                                  "WHERE url_key = $1 ORDER BY ts DESC LIMIT $2", key, max(1, min(int(fetches or 10), 100)))
            lo = await conn.fetchval("SELECT count(*) FROM web_links WHERE src_key = $1", key)
            li = await conn.fetchval("SELECT count(*) FROM web_links WHERE dst_key = $1", key)
    except Exception as e:  # noqa: BLE001
        return {"key": key, "error": str(e)[:160]}
    return {"key": key, "url": _plain(row), "fetches": [_plain(f) for f in fs], "links_out": lo, "links_in": li}


@capability("fabric.map.host", http_method="GET", http_path="/fabric/map/host", http_tags=["fabric", "ingress"],
            memory="off", silent=True,
            description="What the web map knows about one host: its row (fetches, errors, bytes, robots), how many "
                        "URLs and distinct contents it has, and its most fetched URLs. Input: host (str!), limit "
                        "(int, default 20). Output: {host, urls, contents, top}.")
async def cap_map_host(host: str = "", limit: int = 20, trace_id=None):
    h = ic.host_of("https://" + str(host or "").replace("https://", "").replace("http://", ""))
    if not h:
        return {"error": "host required"}
    pool = _pool()
    if pool is None:
        return {"host": h, "error": "the web map's store is not connected"}
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM web_hosts WHERE host = $1", h)
            n = await conn.fetchval("SELECT count(*) FROM web_urls WHERE host = $1", h)
            c = await conn.fetchval("SELECT count(DISTINCT content_hash) FROM web_urls WHERE host = $1 AND content_hash IS NOT NULL", h)
            top = await conn.fetch("SELECT url_key, fetches, changes, last_status, kind, last_seen FROM web_urls "
                                   "WHERE host = $1 ORDER BY fetches DESC, last_seen DESC LIMIT $2", h, max(1, min(int(limit or 20), 200)))
    except Exception as e:  # noqa: BLE001
        return {"host": h, "error": str(e)[:160]}
    return {"host": _plain(row) or {"host": h}, "urls": n, "contents": c, "top": [_plain(t) for t in top]}


@capability("fabric.map.stats", http_method="GET", http_path="/fabric/map/stats", http_tags=["fabric", "ingress"],
            memory="off", silent=True,
            description="The web map in numbers: hosts, URLs, fetches, links, distinct contents, queries; fetches "
                        "and new URLs in the last 24 h; the busiest callers; size against the budget. Input: none.")
async def cap_map_stats(trace_id=None):
    pool = _pool()
    if pool is None:
        return {"error": "the web map's store is not connected", "mode": mode()}
    out: Dict[str, Any] = {"mode": mode()}
    try:
        async with pool.acquire() as conn:
            for t in rc.MAP_TABLES:
                out[t] = await conn.fetchval(f"SELECT count(*) FROM {t}")
            out["fetches_24h"] = await conn.fetchval("SELECT count(*) FROM web_fetches WHERE ts > NOW() - INTERVAL '24 hours'")
            out["new_urls_24h"] = await conn.fetchval("SELECT count(*) FROM web_urls WHERE first_seen > NOW() - INTERVAL '24 hours'")
            out["callers_24h"] = [_plain(r) for r in await conn.fetch(
                "SELECT caller, count(*) AS n FROM web_fetches WHERE ts > NOW() - INTERVAL '24 hours' "
                "GROUP BY caller ORDER BY n DESC LIMIT 12")]
            out["repeat_share_24h"] = await conn.fetchval(
                "SELECT CASE WHEN count(*) = 0 THEN 0 ELSE round(1.0 - count(DISTINCT url_key)::numeric / count(*), 3) END "
                "FROM web_fetches WHERE ts > NOW() - INTERVAL '24 hours'")
            rows = await conn.fetch(rc.SIZE_SQL, list(rc.MAP_TABLES))
            out["budget"] = rc.budget_state({r[0]: r[1] for r in rows}, DEFAULT_BUDGET_GB)
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:160]
    out["writer"] = dict(WRITER.stats, queue_depth=WRITER.depth)
    return out


def _plain(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    d = dict(row)
    for k, v in list(d.items()):
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif k == "simhash" and v is not None:
            d[k] = rc.unsigned64(v)
    return d
