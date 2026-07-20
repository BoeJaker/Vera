"""
web_capabilities.py — Web search, fetch, and crawl as @capabilities.
====================================================================

Background
──────────
Up until now, all the "go look something up on the web" logic lived inside
researcher_api.py — buried in private helpers like `gather_web_search`,
`deep_crawl_url`, `search_searxng`. Anything that wanted that capability had
to either:
  (a) submit a full research job (heavy: spins up the whole pipeline), or
  (b) re-implement the logic locally (duplication, drift)

Dream cycles, agentic chats, IDE chats, and the upcoming research-recall
flows ALL want simple "search the web", "fetch this URL", "crawl this site"
operations as first-class registry citizens. This module pulls that out.

What's here
───────────
  web.search            — one query → result list (searxng → brave → ddg)
  web.fetch             — one URL  → cleaned text + metadata
  web.crawl             — one seed URL → recursive walk, optional fabric ingest
  web.search_and_crawl  — composite: search → take top N → fetch each → ingest

Routing
───────
- All three search engines share a single dispatcher with a clean fallback
  chain. The user can force an engine via `engine="searxng|brave|ddg|auto"`.
- SearXNG host comes from env `VERA_SEARXNG_URL` (default: http://<BACKEND_HOST>:8888)
- Brave key from env `BRAVE_API_KEY`
- HTTP timeouts are aggressive (8s default) — these caps are meant to be
  responsive, not exhaustive.

Fabric integration
──────────────────
Crawl operations write each fetched page to the fabric:
  • dataset = `web.crawl.<sanitised-domain>`
  • record  = {url, title, full_text, domain, fetched_at, parent_url}
This means subsequent recall queries (research.recall.search, fabric.query)
can find any page that's ever been crawled, semantically.

Activity tracking
─────────────────
Every cap call is observable through @capability's normal mechanisms (cap.call
event, optional memory record via memory="auto"). For composite operations
(crawl, search_and_crawl) we additionally emit progress events:
  • web.search.done   — search finished, N results
  • web.fetch.done    — fetch finished, M chars
  • web.crawl.page    — one page in the walk just landed
  • web.crawl.done    — walk finished
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs, unquote, urljoin

import httpx

# Resolve the orchestrator at import time
_orch = (sys.modules.get("Vera.vera.capability_orchestration") or
         sys.modules.get("capability_orchestration"))
if not _orch:
    raise RuntimeError("web_capabilities: capability_orchestration module not loaded")

capability  = _orch.capability
emit_event  = _orch.emit_event

from Vera.vera.config import cfg

# Shared hardened fetch layer — browser fingerprint, HTTP/2, per-domain
# throttle, domain rewrites (reddit→old.reddit), block detection, reader-proxy
# fallback and the platform-API switchover all live there now, shared with the
# research pipeline and fabric web acquisition.
from Vera.vera.web import web_client as _wc

log = logging.getLogger("vera.web_capabilities")


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_SEARXNG = os.getenv("VERA_SEARXNG_URL", f"http://{cfg.BACKEND_HOST}:8888").rstrip("/")
DEFAULT_TIMEOUT = float(os.getenv("VERA_WEB_TIMEOUT", "8.0"))

# Browser fingerprint / reader proxy / page-size limits are owned by the shared
# web_client layer; these aliases keep existing call sites working.
USER_AGENT      = _wc.USER_AGENT
BROWSER_HEADERS = _wc.BROWSER_HEADERS
HEADERS         = BROWSER_HEADERS      # back-compat alias
READER_PROXY    = _wc.READER_PROXY
MAX_PAGE_CHARS  = _wc.MAX_PAGE_CHARS   # max chars to extract per page

MAX_CRAWL_PAGES = 25       # hard ceiling per single crawl call

# Discovery-first mode for web.search:
#   background — recall + snippet-entity update + background full-page fetch
#   snippets   — recall + snippet-entity update only (no page fetch)
#   off        — legacy behaviour (no recall, no discovery ingest)
DEFAULT_DISCOVER_MODE = os.getenv("VERA_WEB_DISCOVER_MODE", "background").lower()

# Background tasks are kept referenced so the event loop doesn't GC them mid-flight.
_BG_TASKS: set = set()


def _spawn(coro):
    """Fire-and-forget a coroutine, holding a reference until it completes."""
    try:
        t = asyncio.create_task(coro)
    except RuntimeError:
        # No running loop (shouldn't happen inside an async cap) — run best-effort.
        return None
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return t


def _research_fabric():
    return (sys.modules.get("research_fabric") or
            sys.modules.get("research_fabric"))


def _researcher_api():
    """
    The research subsystem registers itself as sys.modules['researcher_api'].
    Its search_searxng / search_brave / search_ddg read the *persisted* source
    list + web_cfg (SearXNG host, Brave key, default engine, safe_search) — the
    same config the operator tunes in the research UI. That's precisely why
    research search is reliable where this module's env-only path is not, so we
    route through it when it's loaded.
    """
    return sys.modules.get("researcher_api")


def _discovery():
    """Resolve the fabric discovery module (registered as 'discovery')."""
    return (sys.modules.get("Vera.vera.fabric.discovery") or
            sys.modules.get("discovery"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sanitise_domain(domain: str) -> str:
    """Turn a domain into a safe dataset suffix."""
    return re.sub(r"[^a-z0-9_]+", "_", (domain or "unknown").lower()).strip("_") or "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# HTML → TEXT — moved to the shared web_client layer (identical behaviour).
# ─────────────────────────────────────────────────────────────────────────────

_html_to_text  = _wc.html_to_text
_extract_title = _wc.extract_title


def _extract_links(html: str, base_url: str, max_links: int = 30) -> List[str]:
    """Pull <a href> URLs, normalise to absolute, dedupe by hostname-aware key."""
    out: List[str] = []
    seen: set = set()
    base_host = urlparse(base_url).netloc
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I):
        href = m.group(1).strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        try:
            absu = urljoin(base_url, href)
            p = urlparse(absu)
            if p.scheme not in ("http", "https"):
                continue
            # Skip cross-domain by default (the crawler caller controls this
            # via crawl_breadth on the SAME domain only). External jumps would
            # explode the search space.
            if p.netloc != base_host:
                continue
            key = (p.scheme, p.netloc, p.path)
            if key in seen:
                continue
            seen.add(key)
            out.append(absu)
            if len(out) >= max_links:
                break
        except Exception:
            continue
    return out


def _decode_redirect(url: str) -> str:
    """Unwrap DDG/Google redirect URLs to the actual destination."""
    url = _html.unescape(url or "")
    parsed = urlparse(url)
    if parsed.netloc in ("duckduckgo.com", "www.duckduckgo.com") and parsed.path.startswith("/l"):
        qs = parse_qs(parsed.query)
        target = qs.get("uddg", qs.get("u", [""]))[0]
        if target:
            return unquote(target)
    if "/url?" in url and "google." in parsed.netloc:
        qs = parse_qs(parsed.query)
        target = qs.get("q", qs.get("url", [""]))[0]
        if target:
            return unquote(target)
    return url


# ─────────────────────────────────────────────────────────────────────────────
# ANTI-BOT DETECTION + READER FALLBACK — moved to the shared web_client layer.
# ─────────────────────────────────────────────────────────────────────────────

_detect_block = _wc.detect_block


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH ENGINES
# ─────────────────────────────────────────────────────────────────────────────

async def _search_searxng(query: str, limit: int, host: str = "") -> List[Dict[str, Any]]:
    host = (host or DEFAULT_SEARXNG).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=HEADERS) as c:
            r = await c.get(f"{host}/search", params={
                "q": query, "format": "json", "language": "en", "safesearch": 0,
            })
            r.raise_for_status()
            data = r.json()
            out: List[Dict[str, Any]] = []
            for item in (data.get("results") or [])[:limit]:
                out.append({
                    "url":     _decode_redirect(item.get("url", "")),
                    "title":   item.get("title", ""),
                    "snippet": item.get("content", "") or item.get("snippet", ""),
                    "engine":  "searxng",
                })
            return out
    except Exception as e:
        log.debug("_search_searxng [%s]: %s", query[:40], e)
        return []


async def _search_brave(query: str, limit: int, api_key: str = "") -> List[Dict[str, Any]]:
    api_key = api_key or os.getenv("BRAVE_API_KEY", "")
    if not api_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as c:
            r = await c.get("https://api.search.brave.com/res/v1/web/search",
                            params={"q": query, "count": limit},
                            headers={"Accept": "application/json",
                                     "X-Subscription-Token": api_key})
            r.raise_for_status()
            data = r.json()
            out: List[Dict[str, Any]] = []
            for item in (data.get("web", {}).get("results", []) or [])[:limit]:
                out.append({
                    "url":     item.get("url", ""),
                    "title":   item.get("title", ""),
                    "snippet": item.get("description", ""),
                    "engine":  "brave",
                })
            return out
    except Exception as e:
        log.debug("_search_brave [%s]: %s", query[:40], e)
        return []


async def _search_ddg(query: str, limit: int) -> List[Dict[str, Any]]:
    """DuckDuckGo HTML lite endpoint — no API key required."""
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=HEADERS,
                                      follow_redirects=True) as c:
            r = await c.get("https://html.duckduckgo.com/html/",
                            params={"q": query})
            r.raise_for_status()
            html = r.text
        out: List[Dict[str, Any]] = []
        # Match results blocks. DDG's HTML structure is fragile but stable
        # within a release — we tolerate failure and fall back to searxng.
        for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            html, re.S | re.I,
        ):
            if len(out) >= limit:
                break
            url = _decode_redirect(m.group(1))
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            out.append({
                "url":     url,
                "title":   _html.unescape(title),
                "snippet": "",
                "engine":  "ddg",
            })
        return out
    except Exception as e:
        log.debug("_search_ddg [%s]: %s", query[:40], e)
        return []


async def _dispatch_via_research(query: str, limit: int, engine: str
                                 ) -> Tuple[List[Dict[str, Any]], str]:
    """
    Route through the research subsystem's config-aware engines and normalise to
    web.search's result shape. Mirrors researcher_api.gather_web_search._do_search
    exactly (brave→searxng→ddg fallback), seeded by the operator-configured
    default engine when the caller said "auto". Returns ([], "none") when the
    module isn't loaded or every engine came back empty.
    """
    ra = _researcher_api()
    if not ra:
        return [], "none"

    clean = getattr(ra, "_clean_search_url", None)

    def _norm(items, eng: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for it in (items or [])[:limit]:
            if not isinstance(it, dict):
                continue
            u = it.get("url", "")
            if clean:
                try:
                    u = clean(u)
                except Exception:
                    pass
            if not u:
                continue
            out.append({
                "url":     u,
                "title":   it.get("title", ""),
                "snippet": it.get("content") or it.get("snippet", ""),
                "engine":  eng,
            })
        return out

    # "auto" defers to the operator's configured default engine (web_cfg.engine).
    if engine == "auto":
        cfg_engine = getattr(getattr(ra, "web_cfg", None), "engine", "searxng") or "searxng"
    else:
        cfg_engine = engine

    r: List[Dict[str, Any]] = []
    eng = "none"
    try:
        if cfg_engine == "brave" and hasattr(ra, "search_brave"):
            r = await ra.search_brave(query, limit); eng = "brave"
        if not r and cfg_engine in ("searxng", "auto") and hasattr(ra, "search_searxng"):
            r = await ra.search_searxng(query, limit); eng = "searxng"
        if not r and hasattr(ra, "search_ddg"):
            r = await ra.search_ddg(query, limit); eng = "ddg"
    except Exception as e:
        log.debug("_dispatch_via_research [%s]: %s", query[:40], e)
        return [], "none"

    n = _norm(r, eng)
    return (n, eng) if n else ([], "none")


async def _dispatch_search(query: str, limit: int, engine: str = "auto",
                           searxng_host: str = "",
                           brave_api_key: str = "") -> Tuple[List[Dict[str, Any]], str]:
    """
    Try engines in order. 'auto' = searxng → brave → ddg.
    Returns (results, used_engine).

    Preferred path: reuse the research subsystem's config-aware engines (they see
    the SearXNG host / Brave key the operator actually configured). We only skip
    straight to the local env-configured engines when the caller passes explicit
    host/key overrides — and the local engines still act as a final fallback.
    """
    engine = (engine or "auto").lower()

    if not searxng_host and not brave_api_key:
        res, used = await _dispatch_via_research(query, limit, engine)
        if res:
            return res, used

    order: List[str]
    if engine == "auto":
        order = ["searxng", "brave", "ddg"]
    elif engine in ("searxng", "brave", "ddg"):
        order = [engine]
    else:
        order = ["searxng", "brave", "ddg"]

    for eng in order:
        if eng == "searxng":
            res = await _search_searxng(query, limit, host=searxng_host)
        elif eng == "brave":
            res = await _search_brave(query, limit, api_key=brave_api_key)
        else:
            res = await _search_ddg(query, limit)
        if res:
            return res, eng
    return [], "none"


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "web.search",
    http_method="POST", http_path="/web/search", http_tags=["web", "search"],
    memory="auto",
    description="Search the web and return ranked results with titles, URLs and snippets. "
                "WHEN TO USE: quick web lookups, 'search for X', 'find pages about Y', fetching live information "
                "without a full research pipeline — returns immediately (not LONG-RUNNING). "
                "Discovery-first: in parallel with the live search it recalls what the fabric already "
                "knows (previously-crawled pages + extracted entities) and returns it under `recalled`, "
                "then refreshes the discovery entity graph from the new results. "
                "For deeper research with synthesis and citations use research.run or research.report. "
                "Reliability: routes through the research subsystem's config-aware engines (the same "
                "SearXNG host / Brave key / default engine the operator configured), falling back to "
                "env-configured engines. Tries searxng → brave → ddg in order unless engine is specified. "
                "Platform APIs: when a platform API provider is configured (web.api.*) and the query "
                "targets that platform (site:reddit.com …, a platform keyword, or platform='reddit'), "
                "the platform's native search API is used first and its results lead the list. "
                "Input: query (str!), limit (int default 8), engine (str: auto|searxng|brave|ddg), "
                "platform (str — force a platform API provider by id, e.g. 'reddit'), "
                "searxng_host (str — override host), brave_api_key (str — override env var), "
                "dataset_id (str — discovery dataset to update, default 'web.search'), "
                "discover (str: background|snippets|off — how much discovery processing; "
                "background also fetches+reconciles top pages in the background). "
                "Output: {results: [{url, title, snippet, engine}], engine_used, query, count, elapsed_ms, "
                "recalled: {pages, entities}, discovery_dataset}.",
)
async def cap_web_search(
    query:          str,
    limit:          int = 8,
    engine:         str = "auto",
    platform:       str = "",
    searxng_host:   str = "",
    brave_api_key:  str = "",
    dataset_id:     str = "",
    discover:       str = "",
    trace_id=None,
) -> Dict[str, Any]:
    if not query.strip():
        return {"results": [], "engine_used": "none", "query": query,
                "count": 0, "error": "query required"}
    limit = max(1, min(50, int(limit)))
    mode = (discover or DEFAULT_DISCOVER_MODE).lower()
    disc = _discovery()
    ds_used = dataset_id.strip() or "web.search"
    t0 = time.monotonic()

    # Recall already-processed discovery data IN PARALLEL with the live search.
    recall_task = None
    if disc and mode != "off":
        recall_task = _spawn(disc.discover_recall(query=query, limit=limit))

    # Platform-API switchover: if the web_api module is loaded and a configured
    # provider matches the query (site:<domain>, platform keyword, or explicit
    # platform=), search through the platform's own API — its results lead and
    # the general engines fill the remainder.
    api_results: List[Dict[str, Any]] = []
    api_engine = ""
    engine_query = query
    wapi = sys.modules.get("web_api_capabilities")
    if wapi and hasattr(wapi, "search_for_query"):
        try:
            api_results, api_engine, cleaned = await wapi.search_for_query(
                query, limit, platform=platform)
            if api_results and cleaned:
                engine_query = cleaned
        except Exception as e:
            log.debug("web.search platform api [%s]: %s", query[:40], e)

    if len(api_results) >= limit:
        results, used = api_results[:limit], api_engine
    else:
        results, used = await _dispatch_search(engine_query, limit, engine,
                                               searxng_host=searxng_host,
                                               brave_api_key=brave_api_key)
        if api_results:
            seen = {r.get("url", "") for r in api_results}
            results = api_results + [r for r in results
                                     if r.get("url", "") not in seen]
            results = results[:limit]
            used = f"{api_engine}+{used}" if used != "none" else api_engine
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    recalled = {"pages": [], "entities": []}
    if recall_task is not None:
        try:
            r = await recall_task
            if isinstance(r, dict):
                recalled = {"pages": r.get("pages", []), "entities": r.get("entities", [])}
        except Exception as e:
            log.debug("web.search recall: %s", e)

    # Refresh the discovery entity graph from the fresh results. Snippet-level
    # ingest is ADDITIVE (reconcile=False) so a sparse snippet never prunes what
    # a real full-page crawl found. When mode=='background', the top results are
    # additionally fetched in full and reconciled — off the response path.
    if disc and mode in ("background", "snippets") and results:
        async def _snippet_update():
            for r in results:
                u = r.get("url")
                if not u:
                    continue
                snip = ((r.get("title", "") or "") + "\n\n" +
                        (r.get("snippet", "") or "")).strip()
                if not snip:
                    continue
                try:
                    await disc.discover_ingest_page(
                        u, dataset_id=ds_used, text=snip, title=r.get("title", ""),
                        extract_entities=True, reconcile=False, tags=["web_search"])
                except Exception as e:
                    log.debug("web.search snippet ingest %s: %s", u, e)
        _spawn(_snippet_update())

        if mode == "background":
            top_urls = [r.get("url") for r in results[:5] if r.get("url")]
            async def _bg_fetch():
                for u in top_urls:
                    try:
                        await disc.discover_ingest_page(
                            u, dataset_id=ds_used, fetch=True, extract_entities=True,
                            reconcile=True, full_fetch=True, tags=["web_search"])
                    except Exception as e:
                        log.debug("web.search bg fetch %s: %s", u, e)
            _spawn(_bg_fetch())

    await emit_event({
        "type":         "web.search.done",
        "query":        query[:100],
        "engine_used":  used,
        "count":        len(results),
        "elapsed_ms":   elapsed_ms,
        "recalled":     len(recalled["pages"]),
    })
    return {
        "query":       query,
        "engine_used": used,
        "results":     results,
        "count":       len(results),
        "elapsed_ms":  elapsed_ms,
        "recalled":    recalled,
        "recalled_count": len(recalled["pages"]),
        "discovery_dataset": (ds_used if (disc and mode != "off") else ""),
        "discover_mode": mode,
    }


@capability(
    "web.fetch",
    http_method="POST", http_path="/web/fetch", http_tags=["web", "fetch"],
    memory="auto",
    description="Fetch one URL and extract clean text + title. "
                "WHEN TO USE: you have a specific URL (from web.search results, user-provided, or known source) "
                "and need to read its full text content. Returns immediately (not LONG-RUNNING). "
                "Anti-blocking pipeline: if a platform API is configured for the URL's domain (web.api.* — "
                "e.g. Reddit via its OAuth API) the content comes from that API instead of scraping (via_api); "
                "otherwise hostile domains are rewritten (reddit→old.reddit), a realistic browser fingerprint "
                "is sent over HTTP/2, anti-bot / cookie-consent / CAPTCHA interstitials are detected, and "
                "(unless VERA_WEB_READER is disabled) blocked pages retry through a reader proxy that renders "
                "the page server-side. If still blocked, returns blocked=true + block_reason "
                "instead of ingesting the challenge page. "
                "Discovery-first: recalls any prior processing of this URL (under `recalled`) in parallel "
                "with the fetch, and — when ingest_to_fabric is set — persists the page into the fabric "
                "discovery store and refreshes its entities (extracting new ones and disconnecting/pruning "
                "entities no longer present on the page). "
                "Input: url (str!), timeout (float default 8.0), max_chars (int default 16000), "
                "ingest_to_fabric (bool default True), dataset_id (str — discovery dataset). "
                "Output: {url, title, text, domain, status_code, fetched_at, chars, elapsed_ms, "
                "blocked, block_reason, via_reader, recalled, entities, fabric_dataset}.",
)
async def cap_web_fetch(
    url:               str,
    timeout:           float = DEFAULT_TIMEOUT,
    max_chars:         int   = MAX_PAGE_CHARS,
    ingest_to_fabric:  bool  = True,
    dataset_id:        str   = "",
    trace_id=None,
) -> Dict[str, Any]:
    if not url.strip():
        return {"error": "url required", "url": url}
    timeout = max(1.0, min(60.0, float(timeout)))
    max_chars = max(500, min(MAX_PAGE_CHARS, int(max_chars)))

    domain = urlparse(url).netloc
    disc = _discovery()

    # Recall prior processing of this URL in parallel with the live fetch.
    recall_task = _spawn(disc.discover_recall(urls=[url])) if disc else None

    t0 = time.monotonic()
    # Shared hardened fetch: platform-API switchover → domain rewrite →
    # throttled browser-fingerprint fetch → block detection → reader fallback.
    fp = await _wc.fetch_page(url, timeout=timeout, max_chars=max_chars)
    if fp.get("error"):
        await emit_event({"type": "web.fetch.error", "url": url,
                          "error": fp["error"][:120]})
        return {"error": fp["error"], "url": url, "domain": domain}

    html         = fp.get("html", "")
    text         = fp.get("text", "")
    title        = fp.get("title", "")
    status       = fp.get("status", 0)
    block_reason = fp.get("block_reason", "")
    via_reader   = bool(fp.get("via_reader"))
    via_api      = fp.get("via_api", "")

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    fetched_at = _now_iso()

    out = {
        "url":          url,
        "title":        title,
        "text":         text,
        "domain":       domain,
        "status_code":  status,
        "fetched_at":   fetched_at,
        "chars":        len(text),
        "elapsed_ms":   elapsed_ms,
        "blocked":      bool(block_reason),
        "block_reason": block_reason,
        "via_reader":   via_reader,
        "via_api":      via_api,
    }

    recalled = {"pages": [], "entities": []}
    if recall_task is not None:
        try:
            rr = await recall_task
            if isinstance(rr, dict):
                recalled = {"pages": rr.get("pages", []), "entities": rr.get("entities", [])}
        except Exception as e:
            log.debug("web.fetch recall: %s", e)
    out["recalled"] = recalled

    # Ingest into the fabric discovery store (full page → entities + reconcile).
    # Never ingest a page we still believe is a challenge/consent interstitial —
    # it would poison the discovery graph with boilerplate entities.
    if ingest_to_fabric and text and not block_reason:
        if disc:
            try:
                ing = await disc.discover_ingest_page(
                    url, dataset_id=dataset_id, html=html, text=text, title=title,
                    extract_entities=True, reconcile=True, full_fetch=True,
                    tags=["web_fetch", domain])
                if isinstance(ing, dict) and not ing.get("error"):
                    out["fabric_dataset"] = ing.get("dataset_id", "")
                    out["entities"]       = ing.get("entities", 0)
                    out["record_id"]      = ing.get("record_id", "")
            except Exception as e:
                log.debug("web.fetch discovery ingest: %s", e)
        else:
            # Legacy fallback: research_fabric ingest (no discovery module loaded)
            rf = _research_fabric()
            if rf:
                ds = f"web.crawl.{_sanitise_domain(domain)}"
                try:
                    rec = rf.shape_record(
                        text       = (title + "\n\n" + text)[:rf.TEXT_INDEX_LIMIT],
                        full_text  = text,
                        url        = url,
                        title      = title,
                        domain     = domain,
                        extra      = {"status_code": status, "fetched_at": fetched_at},
                        tags       = ["web_fetch", domain],
                    )
                    await rf.ingest_research_record(ds, rec, source="web.fetch",
                                                     tags=["web_fetch", domain])
                    out["fabric_dataset"] = ds
                except Exception as e:
                    log.debug("web.fetch fabric ingest: %s", e)

    await emit_event({
        "type":         "web.fetch.done",
        "url":          url,
        "domain":       domain,
        "chars":        len(text),
        "status_code":  status,
        "elapsed_ms":   elapsed_ms,
        "blocked":      bool(block_reason),
        "via_reader":   via_reader,
    })
    return out


@capability(
    "web.crawl",
    http_method="POST", http_path="/web/crawl", http_tags=["web", "crawl"],
    memory="on",
    description="Recursive crawl from a seed URL. Same-domain only. "
                "Input: url (str!), depth (int 0-3 default 1), "
                "breadth (int 1-10 default 3 — links per level), "
                "max_pages (int default 10), timeout (float default 8.0), "
                "ingest_to_fabric (bool default True). "
                "Output: {seed, pages: [{url, title, chars, ...}], "
                "page_count, total_chars, fabric_dataset}.",
)
async def cap_web_crawl(
    url:              str,
    depth:            int   = 1,
    breadth:          int   = 3,
    max_pages:        int   = 10,
    timeout:          float = DEFAULT_TIMEOUT,
    ingest_to_fabric: bool  = True,
    trace_id=None,
) -> Dict[str, Any]:
    if not url.strip():
        return {"error": "url required", "pages": []}
    depth     = max(0, min(3, int(depth)))
    breadth   = max(1, min(10, int(breadth)))
    max_pages = max(1, min(MAX_CRAWL_PAGES, int(max_pages)))
    timeout   = max(1.0, min(60.0, float(timeout)))
    seed_domain = urlparse(url).netloc
    fabric_dataset = f"web.crawl.{_sanitise_domain(seed_domain)}" if ingest_to_fabric else ""

    # Discovery-first: delegate to the discovery crawl engine so crawled pages
    # land in the shared discovery graph with entities extracted AND reconciled
    # (entities no longer present on re-crawled pages are unlinked/pruned). Falls
    # back to the inline crawl below if the discovery module isn't available.
    disc = _discovery()
    if disc and ingest_to_fabric:
        try:
            dr = await disc.cap_discover_crawl(
                url=url, dataset_id="", max_pages=max_pages, max_depth=depth,
                same_domain=True, extract_entities=True, extract_entities_llm=False,
                reconcile_entities=True, detect_surfaces=False, extract_subtables=False,
                llm_tagging=False, auto_synthesize=False, consolidate_entities=False,
                tags="web_crawl")
            if isinstance(dr, dict) and not dr.get("error"):
                ds = dr.get("dataset_id", "")
                d_pages = await disc.discover_dataset_pages(ds, limit=max_pages)
                total_chars = sum(p.get("chars", 0) for p in d_pages)
                await emit_event({"type": "web.crawl.done", "seed": url,
                                  "page_count": dr.get("pages_fetched", len(d_pages)),
                                  "total_chars": total_chars})
                return {
                    "seed":           url,
                    "pages":          d_pages,
                    "page_count":     dr.get("pages_fetched", len(d_pages)),
                    "total_chars":    total_chars,
                    "fabric_dataset": ds,
                    "ingested":       dr.get("pages_fetched", len(d_pages)),
                    "entities_found": dr.get("entities_found", 0),
                    "crawl_id":       dr.get("crawl_id", ""),
                    "via":            "discovery",
                }
        except Exception as e:
            log.debug("web.crawl discovery path: %s", e)

    visited: set = set()
    pages: List[Dict[str, Any]] = []
    rf = _research_fabric() if ingest_to_fabric else None
    fabric_records: List[Dict[str, Any]] = []

    # One shared session for the whole walk — one TLS handshake, and cookies
    # (e.g. consent cookies set by page 1) persist across pages like a browser.
    session = _wc.new_session(timeout)

    async def _fetch_one(u: str, current_depth: int):
        if u in visited or len(pages) >= max_pages:
            return
        visited.add(u)
        fp = await _wc.fetch_page(u, timeout=timeout, client=session)
        if fp.get("error"):
            await emit_event({"type": "web.crawl.error", "url": u,
                              "error": fp["error"][:120]})
            return

        # Skip anti-bot / consent interstitials — don't record or ingest them,
        # and don't harvest their (challenge-page) links for recursion.
        if fp.get("blocked"):
            await emit_event({"type": "web.crawl.error", "url": u,
                              "error": f"blocked: {fp.get('block_reason', '')}"[:120]})
            return

        html   = fp.get("html", "")
        status = fp.get("status", 0)
        text   = fp.get("text", "")
        title  = fp.get("title", "")
        page = {
            "url":         u,
            "title":       title,
            "domain":      urlparse(u).netloc,
            "chars":       len(text),
            "status_code": status,
            "depth":       depth - current_depth,
            "fetched_at":  _now_iso(),
        }
        # We deliberately don't include `text` in the page dict in the response
        # to keep the response light. Full text goes to fabric.
        pages.append(page)
        await emit_event({"type": "web.crawl.page", **page})

        if rf and text:
            try:
                fabric_records.append(rf.shape_record(
                    text       = (title + "\n\n" + text)[:rf.TEXT_INDEX_LIMIT],
                    full_text  = text,
                    url        = u,
                    title      = title,
                    domain     = page["domain"],
                    extra      = {"status_code": status,
                                  "fetched_at":  page["fetched_at"],
                                  "parent_url":  url if u != url else "",
                                  "crawl_depth": page["depth"]},
                    tags       = ["web_crawl", page["domain"]],
                ))
            except Exception as e:
                log.debug("web.crawl shape_record: %s", e)

        # Recurse
        if current_depth > 0 and len(pages) < max_pages:
            child_links = _extract_links(html, u, max_links=breadth)
            await asyncio.gather(
                *[_fetch_one(cl, current_depth - 1) for cl in child_links],
                return_exceptions=True,
            )

    try:
        await _fetch_one(url, depth)
    finally:
        await session.aclose()

    # Bulk ingest at the end — single fabric call rather than N calls
    if rf and fabric_records:
        try:
            await rf.ingest_research_records(
                fabric_dataset, fabric_records,
                source="web.crawl",
                tags=["web_crawl", seed_domain],
            )
        except Exception as e:
            log.debug("web.crawl fabric ingest: %s", e)

    total_chars = sum(p.get("chars", 0) for p in pages)
    await emit_event({
        "type":         "web.crawl.done",
        "seed":         url,
        "page_count":   len(pages),
        "total_chars":  total_chars,
    })

    return {
        "seed":           url,
        "pages":          pages,
        "page_count":     len(pages),
        "total_chars":    total_chars,
        "fabric_dataset": fabric_dataset,
        "ingested":       len(fabric_records) if rf else 0,
    }


@capability(
    "web.search_and_crawl",
    http_method="POST", http_path="/web/search_and_crawl",
    http_tags=["web", "search", "crawl"],
    memory="on",
    description="Composite: search the web, then crawl the top-N results through the "
                "fabric discovery engine — each crawled page is stored and its entities "
                "extracted + reconciled into the shared discovery graph. "
                "Input: query (str!), search_limit (int default 5), "
                "crawl_depth (int default 1), pages_per_result (int default 3), "
                "engine (str default auto), ingest_to_fabric (bool default True). "
                "Output: {query, search_results, crawl_summary: [{seed, page_count, "
                "total_chars}], fabric_datasets, fabric_dataset_count, entities_found}.",
)
async def cap_web_search_and_crawl(
    query:             str,
    search_limit:      int   = 5,
    crawl_depth:       int   = 1,
    pages_per_result:  int   = 3,
    engine:            str   = "auto",
    ingest_to_fabric:  bool  = True,
    trace_id=None,
) -> Dict[str, Any]:
    if not query.strip():
        return {"error": "query required"}

    # 1. Search. discover="off" — we crawl the results below, so there's no need
    #    to also snippet-process them here (the crawl does the full processing).
    search_res = await cap_web_search(query=query, limit=search_limit,
                                      engine=engine, discover="off")
    results = search_res.get("results") or []
    urls = [r.get("url") for r in results if r.get("url")]

    # 2a. Discovery-first: crawl ALL result URLs in ONE discovery run so pages +
    #     entities (reconciled) land in the shared graph. Falls back to per-URL
    #     web.crawl below if the discovery module isn't available.
    disc = _discovery()
    if disc and ingest_to_fabric and urls:
        try:
            cr = await disc.discover_crawl_urls(
                urls, dataset_id="web.search_and_crawl", topic=query,
                max_depth=int(crawl_depth), breadth=int(pages_per_result),
                max_pages=len(urls) * (int(pages_per_result) + 1),
                same_domain=False, reconcile=True)
            if isinstance(cr, dict) and not cr.get("error"):
                ds = cr.get("dataset_id", "")
                put = cr.get("per_url_text", {}) or {}
                summary = [{"seed": u, "page_count": 1 if put.get(u) else 0,
                            "total_chars": len(put.get(u, ""))} for u in urls]
                return {
                    "query":                query,
                    "engine_used":          search_res.get("engine_used"),
                    "search_count":         len(results),
                    "search_results":       results,
                    "crawl_summary":        summary,
                    "total_pages":          cr.get("pages_fetched", len(put)),
                    "total_chars":          sum(len(t) for t in put.values()),
                    "fabric_datasets":      [ds] if ds else [],
                    "fabric_dataset_count": 1 if ds else 0,
                    "entities_found":       cr.get("entities_found", 0),
                    "crawl_id":             cr.get("crawl_id", ""),
                    "via":                  "discovery",
                }
        except Exception as e:
            log.debug("web.search_and_crawl discovery path: %s", e)

    # 2b. Legacy fallback: crawl each in parallel — but bounded so we don't melt the network
    crawl_tasks = []
    for r in results:
        u = r.get("url")
        if not u:
            continue
        crawl_tasks.append(cap_web_crawl(
            url=u, depth=int(crawl_depth), breadth=int(pages_per_result),
            max_pages=int(pages_per_result) + 1,
            ingest_to_fabric=bool(ingest_to_fabric),
        ))
    crawl_results = await asyncio.gather(*crawl_tasks, return_exceptions=True)

    summary: List[Dict[str, Any]] = []
    fabric_datasets: set = set()
    total_pages = total_chars = 0
    for cr in crawl_results:
        if isinstance(cr, Exception):
            continue
        if not isinstance(cr, dict):
            continue
        summary.append({
            "seed":        cr.get("seed"),
            "page_count":  cr.get("page_count", 0),
            "total_chars": cr.get("total_chars", 0),
        })
        total_pages += cr.get("page_count", 0)
        total_chars += cr.get("total_chars", 0)
        if cr.get("fabric_dataset"):
            fabric_datasets.add(cr["fabric_dataset"])

    return {
        "query":                 query,
        "engine_used":           search_res.get("engine_used"),
        "search_count":          len(results),
        "search_results":        results,
        "crawl_summary":         summary,
        "total_pages":           total_pages,
        "total_chars":           total_chars,
        "fabric_datasets":       sorted(fabric_datasets),
        "fabric_dataset_count":  len(fabric_datasets),
    }


@capability(
    "web.research",
    http_method="POST", http_path="/web/research",
    http_tags=["web", "search", "research"],
    memory="on",
    description="Unified fast web research — search the web BROADLY and PULL the useful "
                "content from the top results in ONE call. WHEN TO USE: the default way to "
                "'look something up', 'research X', 'find and read about Y' — it is broader + "
                "faster than chaining web.search then web.fetch by hand, and far lighter than "
                "research.run (no multi-stage synthesis pipeline). Returns immediately (not "
                "LONG-RUNNING). Runs ONE web search, then fetches the top-N result pages in "
                "PARALLEL (realistic browser fingerprint + reader-proxy fallback, same as "
                "web.fetch) and returns their cleaned text INLINE so you can read, quote and "
                "cite them straight away — no second round-trip. Extra un-fetched result links "
                "come back under `more_links` for optional follow-up. This is a fast read, NOT a "
                "crawl: nothing is written to the fabric. For synthesis + citations use "
                "research.run / research.report; for one known URL use web.fetch. "
                "Input: query (str!), results (int default 5, max 10 — pages to fetch & read), "
                "max_chars (int default 4000 — text kept per page), engine (str: "
                "auto|searxng|brave|ddg), fetch_timeout (float default 8.0). "
                "Output: {query, engine_used, count, sources: [{url, title, snippet, text, "
                "chars, blocked, via_reader}], more_links: [{url, title, snippet}], elapsed_ms}.",
)
async def cap_web_research(
    query:         str,
    results:       int   = 5,
    max_chars:     int   = 4000,
    engine:        str   = "auto",
    fetch_timeout: float = DEFAULT_TIMEOUT,
    trace_id=None,
) -> Dict[str, Any]:
    if not query.strip():
        return {"query": query, "sources": [], "count": 0, "error": "query required"}
    results   = max(1, min(10, int(results)))
    max_chars = max(200, min(MAX_PAGE_CHARS, int(max_chars)))
    t0 = time.monotonic()

    # 1. Broad search. discover="off" — we do our own content pull below and don't
    #    want to also kick off the background snippet/crawl processing here. Ask for
    #    more hits than we fetch so `more_links` has real follow-up candidates.
    sr = await cap_web_search(query=query, limit=max(results * 2, results),
                              engine=engine, discover="off")
    hits = [h for h in (sr.get("results") or []) if h.get("url")]
    top  = hits[:results]

    # 2. Pull the useful content from the top results IN PARALLEL. ingest_to_fabric
    #    stays False — this is a fast read, not a crawl-and-store.
    fetches = await asyncio.gather(
        *[cap_web_fetch(url=h["url"], timeout=fetch_timeout,
                        max_chars=max_chars, ingest_to_fabric=False) for h in top],
        return_exceptions=True,
    )

    sources: List[Dict[str, Any]] = []
    for h, f in zip(top, fetches):
        src = {
            "url":     h.get("url"),
            "title":   h.get("title", ""),
            "snippet": h.get("snippet", ""),
            "engine":  h.get("engine", ""),
        }
        if isinstance(f, dict) and not f.get("error"):
            src["title"]      = f.get("title") or src["title"]
            src["text"]       = f.get("text", "")
            src["chars"]      = f.get("chars", len(f.get("text", "")))
            src["blocked"]    = bool(f.get("blocked"))
            src["via_reader"] = bool(f.get("via_reader"))
        else:
            err = str(f) if isinstance(f, Exception) else (f or {}).get("error", "")
            src.update({"text": "", "chars": 0, "blocked": True, "error": err[:160]})
        sources.append(src)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    await emit_event({
        "type":       "web.research.done",
        "query":      query[:100],
        "sources":    len(sources),
        "engine_used": sr.get("engine_used", ""),
        "elapsed_ms": elapsed_ms,
    })
    return {
        "query":       query,
        "engine_used": sr.get("engine_used", ""),
        "count":       len(sources),
        "sources":     sources,
        "more_links":  [{"url": h.get("url"), "title": h.get("title", ""),
                         "snippet": h.get("snippet", "")} for h in hits[results:results + 6]],
        "elapsed_ms":  elapsed_ms,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Module load message
# ─────────────────────────────────────────────────────────────────────────────
log.info("web_capabilities: loaded — caps registered: web.search, web.fetch, "
         "web.crawl, web.search_and_crawl, web.research")
