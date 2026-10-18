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
import os
import random
import sys
import time
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability, schedule

try:
    from Vera.vera.fabric import ingress_core as ic
    from Vera.vera.fabric import ingress_receipts_core as rc
    from Vera.vera.fabric import ingress_topic_core as tc
    from Vera.vera.fabric import ingress_search_core as sc
    from Vera.vera.fabric import ingress_crawl_core as cc
    from Vera.vera.fabric import ingress_novelty_core as nc
    from Vera.vera.fabric import ingress_writer as iw
    from Vera.vera.web import web_client as _wc
except ImportError:                       # loaded from its file under a stand-in orchestration (tests)
    from vera.fabric import ingress_core as ic
    from vera.fabric import ingress_receipts_core as rc
    from vera.fabric import ingress_topic_core as tc
    from vera.fabric import ingress_search_core as sc
    from vera.fabric import ingress_crawl_core as cc
    from vera.fabric import ingress_novelty_core as nc
    from vera.fabric import ingress_writer as iw
    from vera.web import web_client as _wc

log = logging.getLogger("vera.ingress")

SWITCH = "ingress.recording"
SWITCH_THROTTLE = "ingress.shared_throttle"
SWITCH_TTL_S = 15.0
HOST_INTERVAL_S = float(os.environ.get("VERA_WEB_DOMAIN_INTERVAL", "1.0") or 1.0)
HOST_JITTER_S = float(os.environ.get("VERA_WEB_DOMAIN_JITTER", "0.6") or 0.6)
THROTTLE_KEY = "vera:ingress:host:"
DEFAULT_BUDGET_GB = 20.0
#: estate hosts the configuration names on top of ingress_core's rules (Redis-free: an env/config list later)
INTERNAL_HOSTS: List[str] = []

_SW: Dict[str, Any] = {"at": 0.0, "mode": "off", "throttle": "off", "dedupe": "off", "defer": "off", "novelty": "off",
                       "task": None}


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


WRITER = iw.ReceiptWriter(_pool, novelty_getter=lambda: _SW["novelty"] == "on")


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
        modes = (eff or {}).get("modes") or {}
        _SW["modes"] = dict(modes)
        m = str(modes.get(SWITCH) or "off")
        _SW["mode"] = m if m in ("off", "meta", "on") else "off"
        _SW["throttle"] = "on" if str(modes.get(SWITCH_THROTTLE) or "off") == "on" else "off"
        _SW["dedupe"] = "on" if str(modes.get("ingress.dedupe") or "off") == "on" else "off"
        _SW["defer"] = "on" if str(modes.get("discovery.defer_enrichment") or "off") == "on" else "off"
        _SW["novelty"] = "on" if str(modes.get("ingress.novelty") or "off") == "on" else "off"
        _SW["read_files"] = "on" if str(modes.get("discovery.read_files") or "off") == "on" else "off"
    except Exception as ex:  # noqa: BLE001
        log.debug("ingress switch read: %s", ex)


async def _refresh_tick() -> None:
    """Keep the cached switches fresh in every process: a decision taken once (a crawl reads defer / read_files at its
    start) must not see a value from before the switch changed (18 Oct: a crawl started 20 s after
    discovery.defer_enrichment=on still ran inline - nothing had read the cache in between)."""
    if _has("minds.effective"):
        await _refresh()
        _SW["at"] = time.monotonic()


schedule(_refresh_tick, SWITCH_TTL_S, name="ingress.switches", skip_in_sandbox=False, singleton=False)


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


# ── what discovery asks the map (WI-4) ───────────────────────────────────────

def dedupe_on() -> bool:
    mode()
    return _SW["dedupe"] == "on"


def defer_on() -> bool:
    mode()
    return _SW["defer"] == "on"


def switch(feature_id: str, default: str = "off") -> str:
    """Any switch's mode from the cached global read (refreshed every SWITCH_TTL_S): for callers on a hot path that
    must not call minds.effective themselves."""
    mode()
    return str((_SW.get("modes") or {}).get(feature_id) or default)


def read_files_on() -> bool:
    mode()
    return _SW.get("read_files") == "on"


async def lookup_record(content_hash: str) -> Optional[Dict[str, Any]]:
    """The web_content row for a hash (record_id, first_key, seen), or None. One indexed read; never raises."""
    pool = _pool()
    if pool is None or not content_hash:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(rc.LOOKUP_RECORD, content_hash)
        return dict(row) if row else None
    except Exception as e:  # noqa: BLE001
        log.debug("ingress lookup: %s", e)
        return None


async def host_yields(hosts: List[str]) -> Dict[str, Optional[float]]:
    """{host: novel_yield or None} for a batch of hosts, one indexed read; {} when the map is not reachable."""
    pool = _pool()
    hs = sorted({ic.host_of("https://" + h) or h for h in (hosts or []) if h})[:500]
    if pool is None or not hs:
        return {}
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(rc.SELECT_HOST_YIELDS, hs)
        return {r[0]: (float(r[1]) if r[1] is not None else None) for r in rows}
    except Exception as e:  # noqa: BLE001
        log.debug("ingress host yields: %s", e)
        return {}


def note_record(content_hash: str, record_id: str, key: str = "") -> bool:
    """Tell the map which fabric record holds a content hash (queued; the writer upserts it)."""
    if mode() == "off" or not content_hash or not record_id:
        return False
    return WRITER.enqueue({"record": {"content_hash": content_hash, "record_id": record_id, "key": key}})


# ── the shared per-host slot (WI-3b) ─────────────────────────────────────────

async def shared_wait(domain: str) -> Optional[float]:
    """web_client's THROTTLE_HOOK: the seconds to wait by the host's record in the coordination Redis, or None
    (switch off, or Redis unavailable) so the per-process throttle decides."""
    mode()                                        # keeps the cached switch fresh
    if _SW["throttle"] != "on" or not domain:
        return None
    try:
        r = _orch.COORD_REDIS or await _orch._ensure_coord_redis()
    except Exception:  # noqa: BLE001
        r = None
    if r is None:
        return None
    key = THROTTLE_KEY + str(domain).lower()
    try:
        stored = await r.get(key)
        wait, new_next = ic.next_slot(time.time(), float(stored or 0.0), HOST_INTERVAL_S,
                                      random.uniform(0.0, HOST_JITTER_S))
        await r.set(key, repr(new_next), ex=120)
    except Exception as e:  # noqa: BLE001
        log.debug("shared throttle %s: %s", domain, e)
        return None
    return wait


_wc.THROTTLE_HOOK = shared_wait


# ── robots.txt: stored per host, not obeyed (owner, 11 Oct) ──────────────────

ROBOTS_BATCH = 3
ROBOTS_UA = "Vera intelligence/1.0 (https://github.com/BoeJaker/Vera)"
_ROBOTS = {"running": False}


async def _robots_tick() -> None:
    """Fetch and store robots.txt for hosts the map has not seen one for (a few per minute, never on a request
    path). Informational: nothing reads it to refuse a fetch."""
    if _ROBOTS["running"] or mode() == "off":
        return
    pool = _pool()
    if pool is None:
        return
    _ROBOTS["running"] = True
    try:
        async with pool.acquire() as conn:
            hosts = [r[0] for r in await conn.fetch(
                "SELECT host FROM web_hosts WHERE robots_at IS NULL ORDER BY fetches DESC LIMIT $1", ROBOTS_BATCH)]
        for h in hosts:
            if ic.is_internal("https://" + h, INTERNAL_HOSTS):
                text, note = "", "internal"
            else:
                text, note = await _fetch_robots(h)
            summ = ic.robots_summary(text) if text else {}
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE web_hosts SET robots = $2, robots_at = NOW(), crawl_delay = $3, note = $4 WHERE host = $1",
                    h, text[:ic.ROBOTS_MAX_BYTES] if text else None, summ.get("crawl_delay"), note or None)
    except Exception as e:  # noqa: BLE001
        log.debug("robots tick: %s", e)
    finally:
        _ROBOTS["running"] = False


async def _fetch_robots(host: str):
    import httpx
    last = ""
    for scheme in ("https", "http"):
        try:
            async with httpx.AsyncClient(timeout=6.0, follow_redirects=True, headers={"User-Agent": ROBOTS_UA}) as c:
                r = await c.get(f"{scheme}://{host}/robots.txt")
        except Exception as e:  # noqa: BLE001
            last = f"robots {scheme}: {str(e)[:60]}"
            continue
        if r.status_code == 200 and "html" not in (r.headers.get("content-type") or "").lower():
            return r.text[:ic.ROBOTS_MAX_BYTES], f"robots {scheme} 200"
        if r.status_code in (404, 410):
            return "", f"robots {r.status_code}"
        last = f"robots {scheme} {r.status_code}"
    return "", last


schedule(_robots_tick, 60, name="ingress.robots", skip_in_sandbox=True, singleton=True)


# ── a client for callers that are not pages (WI-9): receipts + the shared slot by event hooks ──────────────

def hooked_client(*, caller: str, purpose: str = "feed", timeout: float = 30.0, headers: Optional[Dict[str, str]] = None,
                  verify: Any = True, follow_redirects: bool = True, **kw: Any):
    """An httpx.AsyncClient whose every response leaves a receipt in the web map and whose every request waits
    for the host's slot (web_client.throttle_domain). For the fabric's pullers, collectors and research gatherers:
    the transport changes, the caller's URL, headers, params and parsing do not."""
    import httpx

    async def _before(request: "httpx.Request") -> None:
        try:
            await _wc.throttle_domain(request.url.host or "")
        except Exception as e:  # noqa: BLE001
            log.debug("hooked client throttle: %s", e)
        request.extensions["ingress_t0"] = time.monotonic()

    async def _after(response: "httpx.Response") -> None:
        try:
            if mode() == "off":
                return
            await response.aread()
            ct = response.headers.get("content-type", "")
            body = response.text if (mode() == "on" and len(response.content) <= 2 * 1024 * 1024) else ""
            record_response(str(response.request.url), status=response.status_code, content_type=ct, body=body,
                            elapsed_ms=int((time.monotonic() - float(response.request.extensions.get("ingress_t0") or time.monotonic())) * 1000),
                            caller=caller, purpose=purpose, final_url=str(response.url), headers=dict(response.headers))
        except Exception as e:  # noqa: BLE001
            log.debug("hooked client receipt: %s", e)

    return httpx.AsyncClient(timeout=timeout, headers=headers, verify=verify, follow_redirects=follow_redirects,
                             event_hooks={"request": [_before], "response": [_after]}, **kw)


# ── capabilities ─────────────────────────────────────────────────────────────

@capability("fabric.ingress.fetch", http_method="POST", http_path="/fabric/ingress/fetch", http_tags=["fabric", "ingress"],
            memory="off",
            description="The one door for a public page: the hardened fetch (API switchover, rewrites, the shared "
                        "per-host slot, block detection, reader fallback) with its receipt in the web map. Input: url "
                        "(str!), purpose (str, default 'page'), caller (str), max_chars (int, default 16000), want "
                        "('text' default | 'html'). Output: fetch_page's result (text, title, status, final_url, "
                        "content_type, via, elapsed_ms, error) without html unless asked, plus key.")
async def cap_ingress_fetch(url: str = "", purpose: str = "page", caller: str = "", max_chars: int = 16000,
                            want: str = "text", trace_id=None):
    if not url:
        return {"error": "url required"}
    fp = await _wc.fetch_page(url, max_chars=max(500, min(int(max_chars or 16000), 400_000)),
                              caller=caller or "fabric.ingress.fetch", purpose=purpose or "page")
    out = dict(fp)
    if want != "html":
        out.pop("html", None)
    out["key"] = ic.url_key(fp.get("final_url") or url)
    return out


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


# ── topic maps (WI-8a) ────────────────────────────────────────────────────────

def _as_list(x: Any) -> List[str]:
    if isinstance(x, str):
        return [p.strip() for p in x.split(",") if p.strip()]
    return [str(p).strip() for p in (x or ()) if str(p).strip()]


@capability("fabric.map.topic.touch", http_method="POST", http_path="/fabric/map/topic/touch", http_tags=["fabric", "ingress"],
            memory="off", silent=True,
            description="Create or update a topic map and add members. Keyed on the intel topic id (the canonical "
                        "topic id). Input: topic_id (str!; or name), name, urls (list or comma string; members), role "
                        "(seed | source | page..., default 'source'), relevance (float), source (str: who added them), "
                        "seeds / queries / entities (lists). Output: {topic_key, members_added}.")
async def cap_map_topic_touch(topic_id: str = "", name: str = "", urls: Any = None, role: str = "source",
                              relevance: Any = None, source: str = "", seeds: Any = None, queries: Any = None,
                              entities: Any = None, trace_id=None):
    key = tc.topic_key(topic_id, name)
    if not key:
        return {"error": "topic_id or name required"}
    pool = _pool()
    if pool is None:
        return {"error": "the web map's store is not connected", "topic_key": key}
    role = role if role in tc.ROLES else "source"
    rel = None if relevance in (None, "") else float(relevance)
    members = []
    for u in _as_list(urls):
        k = ic.url_key(u if u.startswith("http") else "https://" + u)
        if k:
            members.append((key, k, role, None, rel, None, (source or "")[:60] or None))
    try:
        async with pool.acquire() as conn:
            if not WRITER._ddl_done:
                await WRITER._ddl(conn)
            await conn.execute(rc.UPSERT_TOPIC, key, (name or "")[:200] or None, (topic_id or "")[:120] or None,
                               _as_list(seeds)[:50], _as_list(queries)[:50], _as_list(entities)[:50])
            if members:
                await conn.executemany(rc.UPSERT_MEMBER, members)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:160], "topic_key": key}
    return {"topic_key": key, "members_added": len(members)}


@capability("fabric.map.topic.link", http_method="POST", http_path="/fabric/map/topic/link", http_tags=["fabric", "ingress"],
            memory="off", silent=True,
            description="Link an artefact to a topic map: a discovery dataset id, a KB id or a worldview concept. "
                        "Input: topic_id (str!), kind (dataset | kb | concept), ids (list or comma string). "
                        "Output: {topic_key, kind, linked}.")
async def cap_map_topic_link(topic_id: str = "", kind: str = "dataset", ids: Any = None, trace_id=None):
    key = tc.topic_key(topic_id)
    sql = rc.LINK_TOPIC.get(kind)
    if not key or not sql:
        return {"error": "topic_id and kind (dataset | kb | concept) required"}
    pool = _pool()
    if pool is None:
        return {"error": "the web map's store is not connected", "topic_key": key}
    vals = _as_list(ids)[:100]
    try:
        async with pool.acquire() as conn:
            if not WRITER._ddl_done:
                await WRITER._ddl(conn)
            await conn.execute(rc.UPSERT_TOPIC, key, None, topic_id[:120] or None, [], [], [])
            await conn.execute(sql, key, vals)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:160], "topic_key": key}
    return {"topic_key": key, "kind": kind, "linked": vals}


@capability("fabric.map.topic", http_method="GET", http_path="/fabric/map/topic", http_tags=["fabric", "ingress"],
            memory="off", silent=True,
            description="A topic's map: members by kind, role and host, the linked datasets / KBs / worldview "
                        "concepts, whether the topic is still yielding (saturation from the hosts' novel yield and "
                        "the recent members' novelty) and the kinds it has none of (gaps). Input: topic_id (str!), "
                        "limit (int, members read, default 500), members (bool: include the member rows). "
                        "Output: the summary (+ members).")
async def cap_map_topic(topic_id: str = "", limit: int = 500, members: bool = False, trace_id=None):
    key = tc.topic_key(topic_id)
    if not key:
        return {"error": "topic_id required"}
    pool = _pool()
    if pool is None:
        return {"error": "the web map's store is not connected", "topic_key": key}
    try:
        async with pool.acquire() as conn:
            if not WRITER._ddl_done:
                await WRITER._ddl(conn)
            t = await conn.fetchrow(rc.SELECT_TOPIC, key)
            if not t:
                return {"topic_key": key, "error": "no such topic map (fabric.map.topic.touch creates one)"}
            ms = [dict(r) for r in await conn.fetch(rc.SELECT_MEMBERS, key, max(1, min(int(limit or 500), 5000)))]
            hosts = sorted({ic.host_of(m["url_key"]) for m in ms if m.get("url_key")} - {""})
            yields = {r[0]: r[1] for r in await conn.fetch(rc.SELECT_HOST_YIELDS, hosts)} if hosts else {}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:160], "topic_key": key}
    out = tc.summary(_plain(t) or {}, ms, yields)
    if members:
        out["member_rows"] = [_plain(m) for m in ms]
    return out


# ── the targeted index's query and its gaps (WI-14a) ──────────────────────────

def record_gap(question: str, *, caller: str = "info.seek") -> bool:
    """A question the page index had nothing on - the demand that seeds a targeted crawl (PLAN §13.3)."""
    return record_query(question, engine="index", caller=caller, result_urls=[], purpose="gap")


@capability("fabric.map.search", http_method="POST", http_path="/fabric/map/search", http_tags=["fabric", "ingress"],
            memory="off", silent=True,
            description="What Vera has already read on something: the page index's full-text hits and the fabric's "
                        "vector hits fused (reciprocal rank fusion), reranked on an NLP node, then ranked with the web "
                        "map - members of a named topic first, recently checked pages and pages that were new when "
                        "found a little higher - each with since when Vera has known it, when it was last checked, "
                        "how often it was fetched and changed. Input: query (str!), topic_id (str: an intel topic id), "
                        "limit (int, default 10), vector (bool, default true), rerank (bool, default true). Output: {ok, results:[{url, title, excerpt, score, known_since, "
                        "last_checked, fetches, changes, novelty, kind, topic_member}], index_ms}.")
async def cap_map_search(query: str = "", topic_id: str = "", limit: int = 10, vector: bool = True,
                         rerank: bool = True, trace_id=None):
    q = " ".join(str(query or "").split())
    if not q:
        return {"error": "query required"}
    lim = max(1, min(int(limit or 10), 50))
    t0 = time.monotonic()

    async def _text():
        return await _call("discovery.recall_index.search", query=q, limit=lim * 3) \
            if _has("discovery.recall_index.search") else {}
    text_task = asyncio.ensure_future(_text())
    vdiag: Dict[str, Any] = {}
    vec_pages = await _vector_pages(q, lim * 3, vdiag) if vector else []
    idx = await text_task
    index_ms = int((time.monotonic() - t0) * 1000)
    text_pages = list((idx or {}).get("pages") or [])
    pages = sc.fuse({"text": text_pages, "vector": vec_pages}, ic.url_key)
    reranked = False
    if rerank and len(pages) > 1 and _has("nlp.rerank"):
        head = pages[:20]
        docs = [(str(p.get("title") or "") + ". " + " ".join(str(p.get("full_text") or "").split())[:900]) for p in head]
        try:
            rr = await asyncio.wait_for(_call("nlp.rerank", query=q, documents=docs, top_k=len(docs)), RERANK_TIMEOUT_S)
            if rr.get("ranked"):
                pages = sc.apply_rerank(head, rr["ranked"]) + pages[20:]
                reranked = True
        except Exception as e:  # noqa: BLE001
            log.debug("map search rerank: %s", e)
    keys = {str(p.get("url") or ""): ic.url_key(str(p.get("url") or "")) for p in pages}
    rows: Dict[str, Dict[str, Any]] = {}
    members: List[str] = []
    pool = _pool()
    if pool is not None and keys:
        try:
            async with pool.acquire() as conn:
                for r in await conn.fetch("SELECT url_key, first_seen, last_seen, fetches, changes, novelty, kind "
                                          "FROM web_urls WHERE url_key = ANY($1)", list(set(keys.values()))):
                    rows[r["url_key"]] = dict(r)
                if topic_id:
                    members = [r[0] for r in await conn.fetch(
                        "SELECT url_key FROM web_topic_members WHERE topic_key = $1 AND url_key = ANY($2)",
                        tc.topic_key(topic_id), list(set(keys.values())))]
        except Exception as e:  # noqa: BLE001
            log.debug("map search join: %s", e)
    results = sc.rank(pages, rows, members, keys=keys, limit=lim)
    legs = {p.get("url"): p.get("legs") for p in pages}
    for r in results:
        r["legs"] = legs.get(r["url"]) or []
    return {"ok": bool((idx or {}).get("ok", bool(pages))), "query": q, "results": results, "index_ms": index_ms,
            "legs": {"text": len(text_pages), "vector": len(vec_pages), "reranked": reranked, "vector_diag": vdiag},
            "ms": int((time.monotonic() - t0) * 1000)}


RERANK_TIMEOUT_S = 6.0
VECTOR_TIMEOUT_S = 8.0


def _records_by_id(ids: List[str]) -> List[Any]:
    df = _data_fabric()
    if df is None or not ids:
        return []
    conn = df._sqlite_conn()
    marks = ",".join("?" * len(ids))
    return conn.execute(f"SELECT id, dataset_id, text, data FROM fabric_records WHERE id IN ({marks})", ids).fetchall()


async def _vector_pages(q: str, n: int, diag: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """The vector leg: the query embedded the way records are, the fabric's Chroma collection searched with NO where
    filter (a filter scans the whole collection), the hits that are web pages, in similarity order. [] on any
    failure or after VECTOR_TIMEOUT_S - the text leg still answers."""
    d = diag if diag is not None else {}
    df = _data_fabric()
    store = getattr(df, "FABRIC_CHROMA", None) if df else None
    if store is None or not hasattr(df, "_embed"):
        d["error"] = "no fabric vector store in this process"
        return []
    t0 = time.monotonic()
    try:
        async def _go():
            emb = await df._embed(q)
            d["embed_ms"] = int((time.monotonic() - t0) * 1000)
            if not emb:
                d["error"] = "no embedding"
                return []
            d["dim"] = len(emb)
            hits = await asyncio.get_running_loop().run_in_executor(None, lambda: store.search(emb, top_k=n))
            d["hits"] = len(hits or [])
            d["search_ms"] = int((time.monotonic() - t0) * 1000) - d["embed_ms"]
            score = {rid: s for rid, s in hits or []}
            rows = await asyncio.to_thread(_records_by_id, list(score))
            d["records"] = len(rows)
            pages = [sc.record_page(r[0], r[1], r[2], r[3], score.get(r[0], 0.0)) for r in rows]
            return sorted([p for p in pages if p], key=lambda p: p["rank"], reverse=True)
        return await asyncio.wait_for(_go(), VECTOR_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        d["error"] = (type(e).__name__ + ": " + str(e))[:160]
        log.debug("map search vector leg: %s", e)
        return []


@capability("fabric.map.gaps", http_method="GET", http_path="/fabric/map/gaps", http_tags=["fabric", "ingress"],
            memory="off", silent=True,
            description="Questions the page index could not answer (info.seek found nothing Vera had read before), "
                        "grouped, most asked first - the demand a targeted crawl should go and fill. Input: days "
                        "(int, default 7), limit (int, default 50). Output: {gaps:[{query, times, last, callers}]}.")
async def cap_map_gaps(days: int = 7, limit: int = 50, trace_id=None):
    pool = _pool()
    if pool is None:
        return {"error": "the web map's store is not connected", "gaps": []}
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(GAPS_SQL, max(1, min(int(days or 7), 90)), max(1, min(int(limit or 50), 500)))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:160], "gaps": []}
    return {"gaps": sc.gap_rows([dict(r) for r in rows])}


GAPS_SQL = ("SELECT g.query, count(*) AS n, max(g.ts) AS last, array_agg(DISTINCT g.caller) AS callers, "
            "(SELECT max(f.ts) FROM web_queries f WHERE f.purpose = 'gap_fill' AND f.query = g.query) AS filled "
            "FROM web_queries g WHERE g.purpose = 'gap' AND g.ts > NOW() - make_interval(days => $1) "
            "GROUP BY g.query ORDER BY n DESC, last DESC LIMIT $2")
GAP_DATASET = "web.gaps"


def _discovery_mod():
    return sys.modules.get("Vera.vera.fabric.discovery") or sys.modules.get("discovery")


@capability("fabric.map.gaps.fill", http_method="POST", http_path="/fabric/map/gaps/fill",
            http_tags=["fabric", "ingress"], memory="off", silent=True,
            description="Go and read what Vera was asked about and had nothing on: for each open gap (fabric.map.gaps, "
                        "most asked first) one web search, then a shallow crawl of its top results into the page index "
                        "(dataset web.gaps; entities by the NLP tier, no LLM), and the gap is marked filled. Stops at "
                        "`minutes`. Meant as the target of a scheduled Loop Lab window. Input: minutes (int, default "
                        "45), max_gaps (int, default 10), days (int, default 7), search_limit (int, default 4), "
                        "pages_per_result (int, default 2). Output: {ok, filled:[{query, urls, pages}], skipped, "
                        "minutes, reason}.")
async def cap_map_gaps_fill(minutes: int = 45, max_gaps: int = 10, days: int = 7, search_limit: int = 4,
                            pages_per_result: int = 2, trace_id=None):
    disc = _discovery_mod()
    if disc is None or not hasattr(disc, "discover_crawl_urls"):
        return {"ok": False, "error": "the discovery module is not loaded"}
    g = await cap_map_gaps(days=days, limit=200)
    if g.get("error"):
        return {"ok": False, "error": g["error"]}
    plan = sc.fill_plan(g.get("gaps") or [], max_gaps=max_gaps)
    budget = max(5, min(int(minutes or 45), 240)) * 60
    t0 = time.monotonic()
    filled: List[Dict[str, Any]] = []
    reason = "all open gaps crawled" if plan else "no open gaps"
    for q in plan:
        if time.monotonic() - t0 >= budget:
            reason = "time is up"
            break
        s = await _call("web.search", query=q, limit=max(1, min(int(search_limit or 4), 10)), discover="off")
        urls = [r.get("url") for r in (s or {}).get("results") or [] if r.get("url")]
        pages = 0
        if urls:
            try:
                ppr = max(1, min(int(pages_per_result or 2), 5))
                cr = await disc.discover_crawl_urls(urls, dataset_id=GAP_DATASET, topic=q, max_depth=1, breadth=ppr,
                                                    max_pages=len(urls) * (ppr + 1), same_domain=False,
                                                    reconcile=False, tags="web,discovery,gap")
                pages = int((cr or {}).get("pages_fetched") or 0)
            except Exception as e:  # noqa: BLE001
                log.warning("gap fill crawl [%s]: %s", q[:60], e)
        record_query(q, engine="crawl", caller="fabric.map.gaps.fill", result_urls=urls, purpose="gap_fill")
        filled.append({"query": q, "urls": len(urls), "pages": pages})
    return {"ok": True, "filled": filled, "skipped": max(0, len(plan) - len(filled)),
            "minutes": round((time.monotonic() - t0) / 60, 1), "reason": reason}


# ── the node crawl class (WI-12a, PLAN §13.2) ─────────────────────────────────

_LOCAL_NEXT: Dict[str, float] = {}


async def _host_slot(host: str) -> None:
    """Wait for this host's turn in the shared bucket (the same record shared_wait uses), whatever the switch: node
    fetchers exist only because the rate is shared. Without Redis, a per-process record."""
    if not host:
        return
    wait = None
    try:
        r = _orch.COORD_REDIS or await _orch._ensure_coord_redis()
    except Exception:  # noqa: BLE001
        r = None
    if r is not None:
        try:
            key = THROTTLE_KEY + host
            stored = await r.get(key)
            wait, new_next = ic.next_slot(time.time(), float(stored or 0.0), HOST_INTERVAL_S,
                                          random.uniform(0.0, HOST_JITTER_S))
            await r.set(key, repr(new_next), ex=120)
        except Exception as e:  # noqa: BLE001
            log.debug("crawl slot %s: %s", host, e)
            wait = None
    if wait is None:
        wait, _LOCAL_NEXT[host] = ic.next_slot(time.time(), _LOCAL_NEXT.get(host, 0.0), HOST_INTERVAL_S,
                                               random.uniform(0.0, HOST_JITTER_S))
    if wait and wait > 0:
        await asyncio.sleep(min(wait, 30.0))


@capability("ingress.fetch.batch", http_method="POST", http_path="/ingress/fetch/batch", http_tags=["fabric", "ingress"],
            memory="off", silent=True,
            description="Fetch a few web pages, one after another, each after its host's turn in the shared per-host "
                        "rate record - the work unit of the crawl worker class, run on an idle crawl node or on the "
                        "host. Fetches only: it records and stores nothing (the caller does). Internal estate hosts "
                        "are refused. Input: urls (list of str!, at most 10), max_chars (int, default 400000). Output: "
                        "{ok, results:[{url, final_url, status, content_type, html, text, elapsed_ms, error, etag, "
                        "last_modified, bytes}], ran_on}.")
async def cap_ingress_fetch_batch(urls: Any = None, max_chars: int = cc.MAX_BODY_CHARS, trace_id=None):
    import socket
    import httpx
    us = [str(u) for u in (urls if isinstance(urls, list) else [urls] if urls else [])][:cc.MAX_BATCH]
    headers = dict(getattr(_wc, "BROWSER_HEADERS", {}) or {})
    headers["Accept-Encoding"] = ic.accept_encoding()
    out: List[Dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as c:
        for u in us:
            if not u.startswith(("http://", "https://")) or ic.is_internal(u, INTERNAL_HOSTS):
                out.append(cc.failed_row(u, "refused: not an external http(s) URL"))
                continue
            await _host_slot(ic.host_of(u))
            t0 = time.monotonic()
            try:
                resp = await c.get(u)
                out.append(cc.fetch_row(u, final_url=str(resp.url), status=resp.status_code,
                                        content_type=resp.headers.get("content-type", ""), body=resp.text,
                                        elapsed_ms=int((time.monotonic() - t0) * 1000),
                                        etag=resp.headers.get("etag", ""),
                                        last_modified=resp.headers.get("last-modified", ""), max_chars=max_chars))
            except Exception as e:  # noqa: BLE001
                out.append(cc.fetch_row(u, elapsed_ms=int((time.monotonic() - t0) * 1000), error=str(e)[:200]))
    return {"ok": True, "results": out, "ran_on": cc.where(socket.gethostname(), bool(getattr(_orch, "_IS_WORKER", False)))}


@capability("fabric.crawl.fetch_many", http_method="POST", http_path="/fabric/crawl/fetch_many",
            http_tags=["fabric", "ingress"], memory="off", silent=True,
            description="Fetch many web pages through the crawl worker class: the URLs are cut into small per-host "
                        "batches, interleaved across hosts, and up to `parallel` batches run at once - each on an idle "
                        "crawl node, or on the host when none is free. Every host keeps one shared rate however many "
                        "fetchers run. A batch that fails or times out is retried once, so no URL is dropped. Receipts "
                        "are written here, on the host. Input: urls (list of str!), parallel (int, default 4), "
                        "per_batch (int, default 3), caller (str), purpose (str, default page), include_bodies (bool, "
                        "default false). Output: {ok, results, ran_on, fetched, failed, retried, lost, ms}.")
async def cap_crawl_fetch_many(urls: Any = None, parallel: int = 4, per_batch: int = cc.PER_BATCH,
                               caller: str = "fabric.crawl", purpose: str = "page", include_bodies: bool = False,
                               trace_id=None):
    us = [str(u) for u in (urls if isinstance(urls, list) else [urls] if urls else [])]
    batches = cc.plan_batches(us, per_batch)
    if not batches:
        return {"ok": False, "error": "no external http(s) URL given", "results": []}
    sem = asyncio.Semaphore(max(1, min(int(parallel or 4), 16)))
    t0 = time.monotonic()

    async def _one(b: List[str]) -> Dict[str, Any]:
        async with sem:
            r: Dict[str, Any] = {}
            attempts = 0
            for attempts in (1, 2):
                try:
                    r = await _call("ingress.fetch.batch", urls=b, _timeout=30 + 25 * len(b))
                except Exception as e:  # noqa: BLE001
                    r = {"error": str(e)[:200]}
                if isinstance(r.get("results"), list) and len(r["results"]) == len(b):
                    break
            rows = r.get("results") if isinstance(r.get("results"), list) and len(r["results"]) == len(b) \
                else [cc.failed_row(u, str(r.get("error") or "batch failed twice")) for u in b]
            for row in rows:
                record_fetch_result(row, caller=caller, purpose=purpose)
            return {"results": rows, "ran_on": r.get("ran_on") or "failed", "attempts": attempts}

    done = await asyncio.gather(*(_one(b) for b in batches))
    rows = [row for b in done for row in b["results"]]
    if not include_bodies:
        rows = [{k: v for k, v in row.items() if k not in ("html", "text")} for row in rows]
    return dict(ok=True, results=rows, lost=cc.lost([u for b in batches for u in b], done),
                ms=int((time.monotonic() - t0) * 1000), batches=len(batches), **cc.summary(done))


# ── the targeted index's daily metrics (WI-14c, PLAN §13.3) ───────────────────

METRIC_SQL = {
    "purposes": ("SELECT purpose, count(*) FROM web_queries WHERE ts > NOW() - make_interval(days => $1) "
                 "AND purpose IN ('index_hit', 'gap') GROUP BY purpose"),
    "sample": ("SELECT DISTINCT ON (query) query, result_keys FROM web_queries WHERE purpose = 'search' AND results > 0 "
               "AND ts > NOW() - make_interval(days => $1) ORDER BY query, ts DESC LIMIT $2"),
    "fresh": ("SELECT m.topic_key, max(u.last_seen) AS newest FROM web_topic_members m JOIN web_urls u USING (url_key) "
              "GROUP BY m.topic_key"),
    "crawl": ("SELECT count(*) AS fetched, count(*) FILTER (WHERE u.novelty_kind = 'new') AS new, "
              "count(DISTINCT date_trunc('hour', f.ts)) AS hours FROM web_fetches f LEFT JOIN web_urls u USING (url_key) "
              "WHERE f.ts > NOW() - make_interval(days => $1) AND f.caller LIKE 'fabric.discover%'"),
    "fetches": "SELECT count(*) FROM web_fetches WHERE ts > NOW() - make_interval(days => $1)",
    "semantic": ("SELECT count(*) AS n, count(*) FILTER (WHERE semantic_novelty <= 0.05) AS known, "
                 "avg(semantic_novelty) AS mean FROM web_urls WHERE semantic_at > NOW() - make_interval(days => $1)"),
    "store": ("INSERT INTO web_metrics_daily (day, metrics) VALUES (CURRENT_DATE, $1::jsonb) "
              "ON CONFLICT (day) DO UPDATE SET metrics = EXCLUDED.metrics, ts = NOW()"),
}


@capability("fabric.map.metrics", http_method="POST", http_path="/fabric/map/metrics", http_tags=["fabric", "ingress"],
            memory="off", silent=True,
            description="How well Vera's own index answers, measured over the last `days`: the index answer rate "
                        "(questions info.seek answered from pages Vera had already read, against those it had nothing "
                        "on), the live overlap (for a sample of recent searches, how much of the engine's top 10 the "
                        "index's top 10 also holds), the freshness lag per topic, the novel pages per crawl hour and "
                        "the pages fetched per question. store=true keeps today's row (the nightly run); history=N "
                        "returns the last N stored days. Input: days (int, default 1), sample (int, default 20), "
                        "store (bool), history (int). Output: {metrics, history?}.")
async def cap_map_metrics(days: int = 1, sample: int = 20, store: bool = False, history: int = 0, trace_id=None):
    import json as _json
    pool = _pool()
    if pool is None:
        return {"error": "the web map's store is not connected"}
    d = max(1, min(int(days or 1), 30))
    try:
        async with pool.acquire() as conn:
            purposes = {r[0]: int(r[1]) for r in await conn.fetch(METRIC_SQL["purposes"], d)}
            samples = [dict(r) for r in await conn.fetch(METRIC_SQL["sample"], d, max(0, min(int(sample or 20), 100)))]
            fresh = [dict(r) for r in await conn.fetch(METRIC_SQL["fresh"])]
            crawl = dict(await conn.fetchrow(METRIC_SQL["crawl"], d) or {})
            fetches = int(await conn.fetchval(METRIC_SQL["fetches"], d) or 0)
            sem = dict(await conn.fetchrow(METRIC_SQL["semantic"], d) or {})
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}
    overlaps = []
    for srow in samples:
        r = await cap_map_search(query=srow["query"], limit=sc.OVERLAP_K)
        overlaps.append(sc.overlap_at([x.get("key") for x in r.get("results") or []], list(srow["result_keys"] or [])))
    m = sc.daily_metrics(purposes, overlaps, fresh, crawl, fetches)
    m["days"] = d
    try:
        m["retention"] = (await cap_map_retention(days=90)).get("retention")
    except Exception as e:  # noqa: BLE001
        m["retention"] = {"error": str(e)[:120]}
    m["semantic_shadow"] = {"pages": int(sem.get("n") or 0), "known": int(sem.get("known") or 0),
                            "mean_novelty": round(float(sem["mean"]), 3) if sem.get("mean") is not None else None}
    out: Dict[str, Any] = {"metrics": m}
    try:
        async with pool.acquire() as conn:
            if store:
                await conn.execute(METRIC_SQL["store"], _json.dumps(m))
            if history:
                out["history"] = [{"day": str(r[0]), "metrics": _json.loads(r[1]) if isinstance(r[1], str) else r[1]}
                                  for r in await conn.fetch("SELECT day, metrics FROM web_metrics_daily ORDER BY day DESC "
                                                            "LIMIT $1", max(1, min(int(history), 90)))]
    except Exception as e:  # noqa: BLE001
        out["store_error"] = str(e)[:160]
    return out


# ── semantic novelty in shadow (WI-5b, PLAN §3.4 step 3) ─────────────────────

_SEM_SEM: Optional[asyncio.Semaphore] = None
SEMANTIC_TOP_K = 4


def _semantic_ids(ds_id: str, rid: str) -> List[str]:
    """The fabric_records ids (Chroma's ids) of a crawl record, whose own id is inside its data."""
    df = _data_fabric()
    if df is None:
        return []
    rows = df._sqlite_conn().execute("SELECT id FROM fabric_records WHERE dataset_id = ? AND json_extract(data, '$.id') = ?",
                                     (ds_id, rid)).fetchall()
    return [r[0] for r in rows]


async def semantic_note(url: str, ds_id: str, rid: str) -> Optional[float]:
    """With `ingress.semantic_novelty` = shadow: read the page's own vector back from Chroma (no second embedding),
    search its nearest records with no where filter, and record 1 - max cosine to anything else on web_urls. Acts
    on nothing. Never raises; at most two run at once."""
    global _SEM_SEM
    mode()
    if str((_SW.get("modes") or {}).get("ingress.semantic_novelty") or "off") != "shadow":
        return None
    df = _data_fabric()
    store = getattr(df, "FABRIC_CHROMA", None) if df else None
    pool = _pool()
    if store is None or pool is None or not url:
        return None
    if _SEM_SEM is None:
        _SEM_SEM = asyncio.Semaphore(2)
    try:
        async with _SEM_SEM:
            ids = await asyncio.to_thread(_semantic_ids, ds_id, rid)
            if not ids or not store._ready():
                return None
            loop = asyncio.get_running_loop()
            got = await loop.run_in_executor(None, lambda: store._col.get(ids=ids[:1], include=["embeddings"]))
            embs = (got or {}).get("embeddings")
            if embs is None or len(embs) == 0:
                return None
            emb = [float(x) for x in embs[0]]
            hits = await loop.run_in_executor(None, lambda: store.search(emb, top_k=SEMANTIC_TOP_K + len(ids)))
            nov, twin = nc.semantic(hits, ids)
            if nov is None:
                return None
            async with pool.acquire() as conn:
                await conn.execute(rc.UPDATE_URL_SEMANTIC, ic.url_key(url), nov, twin)
            return nov
    except Exception as e:  # noqa: BLE001
        log.debug("semantic novelty %s: %s", url, e)
        return None


# ── the retention experiment (PLAN §14.2) ────────────────────────────────────

RETENTION_SQL = {
    "revisits": ("WITH f AS (SELECT ts, status, content_hash, lag(ts) OVER w AS pts, lag(content_hash) OVER w AS phash "
                 "FROM web_fetches WHERE ts > NOW() - make_interval(days => $1) WINDOW w AS (PARTITION BY url_key ORDER BY ts)) "
                 "SELECT CASE WHEN pts IS NULL THEN 'first' ELSE " + sc.AGE_SQL.format(d="ts - pts") + " END AS bucket, "
                 "count(*) AS n, count(*) FILTER (WHERE pts IS NOT NULL AND (status = 304 OR (content_hash IS NOT NULL "
                 "AND content_hash = phash))) AS unchanged, count(*) FILTER (WHERE pts IS NOT NULL AND (status = 304 OR "
                 "(content_hash IS NOT NULL AND phash IS NOT NULL))) AS comparable FROM f GROUP BY 1"),
    "recall": ("SELECT " + sc.AGE_SQL.format(d="q.ts - u.first_seen") + " AS bucket, count(*) AS n FROM web_queries q "
               "CROSS JOIN LATERAL unnest(q.result_keys) AS k(url_key) JOIN web_urls u ON u.url_key = k.url_key "
               "WHERE q.purpose = 'index_hit' AND q.ts > NOW() - make_interval(days => $1) GROUP BY 1"),
    "span": "SELECT EXTRACT(EPOCH FROM (NOW() - min(ts))) / 86400 FROM web_fetches",
    "counts": "SELECT (SELECT count(*) FROM web_fetches) AS f, (SELECT count(*) FROM web_links) AS l",
}


@capability("fabric.map.retention", http_method="POST", http_path="/fabric/map/retention", http_tags=["fabric", "ingress"],
            memory="off", silent=True,
            description="Is keeping a longer web history worth it (the retention experiment): over the last `days` of "
                        "receipts, by the age of the previous fetch of the same URL (<1d, 1-7d, 7-30d, 30-60d, 60-90d, "
                        ">90d) - how many fetches were revisits and how many of those came back unchanged; by the age "
                        "of the pages index answers cited - how much recall comes from old pages; and what a day of "
                        "history costs in bytes. Input: days (int, default 90). Output: {retention}.")
async def cap_map_retention(days: int = 90, trace_id=None):
    pool = _pool()
    if pool is None:
        return {"error": "the web map's store is not connected"}
    d = max(1, min(int(days or 90), 365))
    try:
        async with pool.acquire() as conn:
            rev = [dict(r) for r in await conn.fetch(RETENTION_SQL["revisits"], d)]
            rec = [dict(r) for r in await conn.fetch(RETENTION_SQL["recall"], d)]
            span = float(await conn.fetchval(RETENTION_SQL["span"]) or 0.0)
            cnt = await conn.fetchrow(RETENTION_SQL["counts"])
            sizes = {r[0]: int(r[1]) for r in await conn.fetch(rc.SIZE_SQL, list(rc.MAP_TABLES))}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}
    return {"retention": sc.retention_curves(rev, rec, sizes, {"web_fetches": int(cnt[0] or 0),
                                                                 "web_links": int(cnt[1] or 0)}, span)}
