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
try:                                                    # 4 Oct: typed URLs, nav-only pages
    from Vera.vera.web import url_core as _url_core
except ImportError:                                     # pragma: no cover
    from . import url_core as _url_core
# Dual-spelled: Vera.vera.* resolves to the DEPLOYED checkout, which does not
# have a module until it lands there, so a NEW sibling must fall back to the
# plain package or this whole module fails to import.
try:
    from Vera.vera.web import search_engines as _engines
except ImportError:                                        # pragma: no cover
    from vera.web import search_engines as _engines

from Vera.vera.fabric.url_dataset_resolve import (
    auto_dataset_for_url as _auto_web_dataset)

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
    """Compatibility wrapper around the shared crawl-boundary policy."""
    return _wc.crawl_links(html, base_url, max_links=max_links)


# Single definition, shared with the researcher's engines. See search_engines
# for the two implementations this replaces and how they had drifted.
_decode_redirect = _engines.decode_redirect


# ─────────────────────────────────────────────────────────────────────────────
# ANTI-BOT DETECTION + READER FALLBACK — moved to the shared web_client layer.
# ─────────────────────────────────────────────────────────────────────────────

_detect_block = _wc.detect_block


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH ENGINES
# ─────────────────────────────────────────────────────────────────────────────

async def _search_searxng(query: str, limit: int, host: str = "") -> List[Dict[str, Any]]:
    """SearXNG, paged when `limit` needs more than one page.

    A limit that fits in one page sends exactly the request this sent before
    pagination existed - searxng_params omits `pageno` for page 1.
    """
    host = (host or DEFAULT_SEARXNG).rstrip("/")
    # Inside a container the configured host is written from the HOST's point of
    # view: "localhost" is this container, and a host-only name may not resolve
    # at all. Try the configured URL first, then the same port via the docker
    # gateway. See search_engines.host_candidates.
    for _cand in _engines.host_candidates(host) or [host]:
        _res = await _searxng_pages(_cand, query, limit)
        if _res:
            return _res
    return []


# What the last SearXNG answers looked like, for /health via web.engines.health. The record lives in search_engines
# so researcher_api's copy (the one that runs when it imports) reports to the same place.
def _note_searxng(body: Optional[Dict[str, Any]], error: str = "") -> None:
    _engines.note_searxng_answer(body, error=error)


@capability(
    "web.engines.health",
    contract={"presentation": {"kind": "kv", "form": "kv", "map": {"keys": ["state", "message", "reachable", "unresponsive", "empty_streak", "last_count", "ok_at", "checked_at"]}}},
    http_method="GET", http_path="/web/engines/health",
    http_tags=["web"], memory="off", silent=True,
    description="Is web search getting real results? What the last SearXNG answers looked like: state "
                "(ok|degraded|down|unknown), message, empty_streak, unresponsive engines with their reason, "
                "ok_at (last answer with results). No network call - it reports what searches saw. "
                "Shown under `search` in /health.",
)
async def cap_web_engines_health(trace_id=None) -> Dict[str, Any]:
    return _engines.searxng_status(_engines.SEARXNG_STATE)


async def _searxng_pages(host: str, query: str, limit: int) -> List[Dict[str, Any]]:
    """Walk SearXNG's pages for one host. Returns [] if it cannot be reached.

    The walk itself is search_engines.walk_pages, shared with the researcher's
    engine so there is ONE definition of when to stop paging.
    """
    try:
        # NOT the browser headers. SearXNG's bot detection answers a
        # browser-looking request with HTTP 200 and an EMPTY result list, so
        # this engine returned nothing on every call - measured, same host and
        # query, 0 results with BROWSER_HEADERS against 10 without. See
        # search_engines.SEARXNG_HEADERS. HEADERS stays right for _search_ddg,
        # which really is scraping an HTML page.
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT,
                                     headers=_engines.SEARXNG_HEADERS) as c:

            async def _fetch(pageno: int):
                try:
                    r = await _wc.request_with_policy(
                        lambda: c.get(f"{host}/search",
                                      params=_engines.searxng_params(query, 0, pageno)),
                        domain=urlparse(host).netloc,
                    )
                    r.raise_for_status()
                    body = r.json()
                except Exception as e:
                    log.debug("_search_searxng [%s] page %d: %s", query[:40], pageno, e)
                    if pageno == 1:
                        _note_searxng(None, error=str(e))
                    # None ends the walk but KEEPS earlier pages.
                    return None
                if pageno == 1:
                    _note_searxng(body)
                return [{
                    "url":     _decode_redirect(item.get("url", "")),
                    "title":   item.get("title", ""),
                    "snippet": item.get("content", "") or item.get("snippet", ""),
                    "engine":  "searxng",
                } for item in (body.get("results") or [])]

            return await _engines.walk_pages(_fetch, limit)
    except Exception as e:
        log.debug("_search_searxng [%s]: %s", query[:40], e)
        return []


async def _search_brave(query: str, limit: int, api_key: str = "") -> List[Dict[str, Any]]:
    api_key = api_key or os.getenv("BRAVE_API_KEY", "")
    if not api_key:
        return []
    pages: List[List[Dict[str, Any]]] = []
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as c:
            for pageno in _engines.page_plan(limit):
                params: Dict[str, Any] = {"q": query, "count": limit}
                # Brave pages by result OFFSET. Omitted on page 1, so the first
                # request is unchanged from before pagination.
                off = _engines.brave_offset(pageno)
                if off:
                    params["offset"] = off
                r = await _wc.request_with_policy(
                    lambda: c.get("https://api.search.brave.com/res/v1/web/search",
                                  params=params,
                                  headers={"Accept": "application/json",
                                           "X-Subscription-Token": api_key}),
                    domain="api.search.brave.com",
                )
                r.raise_for_status()
                items = (r.json().get("web", {}).get("results", []) or [])
                pages.append([{
                    "url":     item.get("url", ""),
                    "title":   item.get("title", ""),
                    "snippet": item.get("description", ""),
                    "engine":  "brave",
                } for item in items])
                if not items or _engines.enough(sum(len(p) for p in pages), limit):
                    break
    except Exception as e:
        log.debug("_search_brave [%s]: %s", query[:40], e)
        return _engines.merge_pages(pages, limit)
    return _engines.merge_pages(pages, limit)


async def _search_ddg(query: str, limit: int) -> List[Dict[str, Any]]:
    """DuckDuckGo HTML lite endpoint — no API key required."""
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=HEADERS,
                                      follow_redirects=True) as c:
            r = await _wc.request_with_policy(
                lambda: c.get("https://html.duckduckgo.com/html/",
                              params={"q": query}),
                domain="html.duckduckgo.com",
            )
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

    # ONE order, shared with the local path below. These two used to disagree -
    # research tried brave -> searxng -> ddg while the local fallback tried
    # searxng -> brave -> ddg - so the same cap preferred a different engine
    # depending on whether researcher_api happened to import. See search_engines.
    _fn = {"searxng": "search_searxng", "brave": "search_brave", "ddg": "search_ddg"}
    searchers = {
        name: (lambda fn=fn: fn(query, limit))
        for name, attr in _fn.items()
        if (fn := getattr(ra, attr, None)) is not None
    }
    r, eng = await _engines.dispatch_search(engine, cfg_engine, searchers)

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

    return await _engines.dispatch_search(
        engine,
        "searxng",
        {
            "searxng": lambda: _search_searxng(query, limit, host=searxng_host),
            "brave": lambda: _search_brave(query, limit, api_key=brave_api_key),
            "ddg": lambda: _search_ddg(query, limit),
        },
    )


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

    # Platform-API switchover through web_client's shared provider boundary.
    # A configured provider leads; the general engines fill the remainder.
    api_results: List[Dict[str, Any]] = []
    api_engine = ""
    engine_query = query
    api_results, api_engine, cleaned = await _wc.search_via_api(
        query, limit, platform=platform)
    if api_results and cleaned:
        engine_query = cleaned

    if len(api_results) >= limit:
        results, used = api_results[:limit], api_engine
    else:
        results, used = await _dispatch_search(engine_query, limit, engine,
                                               searxng_host=searxng_host,
                                               brave_api_key=brave_api_key)
        if api_results:
            results = _engines.merge_pages((api_results, results), limit)
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

    try:    # the web map keeps the searches Vera sends (owner, 11 Oct; web-ingress WI-3b) - never on the hot path
        _ing = sys.modules.get("ingress_capabilities") or sys.modules.get("Vera.vera.fabric.ingress_capabilities")
        if _ing is not None:
            _ing.record_query(query, engine=str(used or ""), caller="web.search",
                              result_urls=[str(r.get("url") or "") for r in results if r.get("url")])
    except Exception as _e:  # noqa: BLE001
        log.debug("web.search receipt: %s", _e)
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
                "THAT INGEST RUNS IN THE BACKGROUND: the page is returned as soon as it is fetched "
                "(~0.15s) rather than after enrichment (12-126s measured), so `ingest` comes back "
                "'scheduled' and `entities`/`record_id` are NOT in the result. `fabric_dataset` IS "
                "returned immediately because it is deterministic (web.<host>). Pass await_ingest=true "
                "ONLY if you must query the fabric for this page in the same breath — it restores the "
                "old blocking behaviour and its full cost. "
                "Input: url (str!), timeout (float default 8.0), max_chars (int default 16000), "
                "ingest_to_fabric (bool default True), await_ingest (bool default False), "
                "dataset_id (str — discovery dataset). "
                "Output: {url, title, text, domain, status_code, fetched_at, chars, elapsed_ms, "
                "blocked, block_reason, via_reader, reader_error (non-empty when the reader "
                "fallback was tried and did NOT rescue a blocked page — e.g. it timed out), "
                "recalled, fabric_dataset, ingest ('scheduled'|'done'|'skipped')}.",
)
async def cap_web_fetch(
    url:               str,
    timeout:           float = DEFAULT_TIMEOUT,
    max_chars:         int   = MAX_PAGE_CHARS,
    ingest_to_fabric:  bool  = True,
    await_ingest:      bool  = False,
    dataset_id:        str   = "",
    trace_id=None,
) -> Dict[str, Any]:
    if not url.strip():
        return {"error": "url required", "url": url}
    url = _url_core.normalize_url(url)
    timeout = max(1.0, min(60.0, float(timeout)))
    max_chars = max(500, min(MAX_PAGE_CHARS, int(max_chars)))

    domain = urlparse(url).netloc
    disc = _discovery()

    # Recall prior processing of this URL in parallel with the live fetch.
    recall_task = _spawn(disc.discover_recall(urls=[url])) if disc else None

    t0 = time.monotonic()
    # Shared hardened fetch: platform-API switchover → domain rewrite →
    # throttled browser-fingerprint fetch → block detection → reader fallback.
    fp = await _wc.fetch_page(url, timeout=timeout, max_chars=max_chars, caller="web.fetch", purpose="page")
    if fp.get("error"):
        await emit_event({"type": "web.fetch.error", "url": url,
                          "error": fp["error"][:120]})
        return {"error": fp["error"], "url": url, "domain": domain}

    html         = fp.get("html", "")
    text         = fp.get("text", "")
    title        = fp.get("title", "")
    status       = fp.get("status", 0)
    block_reason = fp.get("block_reason", "")
    # A 404 / 410 came back `ok` with the not-found page's text, so an agent
    # loop that invented a URL "fetched" it successfully and cited it (census
    # run58 research-report: a typo'd redis.io/news URL fetched twice). Not
    # found is not content: say so, the way a transport failure is said.
    if status in (404, 410):
        await emit_event({"type": "web.fetch.error", "url": url, "error": f"HTTP {status}"})
        return {"ok": False, "error": f"HTTP {status}: no page at this URL - check the address "
                                      "(web.search finds the right one) rather than retrying it",
                "url": url, "domain": domain, "status_code": status, "title": title,
                "text": "", "chars": 0}
    via_reader   = bool(fp.get("via_reader"))
    via_api      = fp.get("via_api", "")
    reader_error = fp.get("reader_error", "")

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    fetched_at = _now_iso()

    out = {
        # first, so a loop's preview of the result shows it
        **({"warning": "this page's text is only navigation/menu items (likely rendered by "
                       "JavaScript) - it is NOT the page's content; use another URL or web.research"}
           if fp.get("nav_only") else {}),
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
        "reader_error": reader_error,
        "nav_only":     bool(fp.get("nav_only")),
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
    #
    # RUNS IN THE BACKGROUND. Measured on prod 2026-08-27: the fetch itself is
    # ~0.15s while awaiting the ingest made the caller wait 12-126s (embedding +
    # a GLiNER/spaCy NER pass over the page), and a single research run spent
    # over three minutes of its life inside web.fetch. Nothing in the returned
    # result depended on the ingest finishing: no caller reads `entities` or
    # `record_id`, and `fabric_dataset` is now DETERMINISTIC (web.<host>), so it
    # is reported immediately without waiting for anything.
    #
    # Pass await_ingest=True if you must query the fabric for this page in the
    # same breath — that restores the old blocking behaviour for that caller only.
    async def _ingest_page():
        """The enrichment nobody is blocked on. Errors are logged, never raised
        into the caller, because by now the page has already been returned."""
        if disc:
            try:
                # File the page under its own web dataset unless the caller named
                # one. Leaving dataset_id empty made discover_ingest_page resolve
                # it by scanning every JSON blob in fabric_records (2.6s, and a
                # full scan whether it matched or not - the 2026-08-26 event-loop
                # stall). Measured on 20 real web_fetch pages, that scan added
                # nothing 12/20 times, and the other 8 were arguably WRONG: it
                # filed one-off fetches into curated corpora (agent_rag.azure_expert,
                # agent_rag.gatherer, research.citations), quietly mutating agents'
                # knowledge bases as a side effect of someone fetching a page.
                # web.search already passes an explicit dataset; web.fetch was the
                # only path that did not.
                ing = await disc.discover_ingest_page(
                    url, dataset_id=ds_for_ingest, html=html, text=text, title=title,
                    extract_entities=True, reconcile=True, full_fetch=True,
                    tags=["web_fetch", domain])
                ents = ing.get("entities", 0) if isinstance(ing, dict) else 0
                err = ing.get("error", "") if isinstance(ing, dict) else "no result"
            except Exception as e:
                ents, err = 0, str(e)[:200]
                log.debug("web.fetch discovery ingest: %s", e)
        else:
            # Legacy fallback: research_fabric ingest (no discovery module loaded)
            ents, err = 0, ""
            rf = _research_fabric()
            if rf:
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
                    await rf.ingest_research_record(ds_for_ingest, rec, source="web.fetch",
                                                     tags=["web_fetch", domain])
                except Exception as e:
                    err = str(e)[:200]
                    log.debug("web.fetch fabric ingest: %s", e)
        # Say so either way — a background failure that logged nothing would be
        # invisible, which is how "the fabric is missing pages" becomes unfindable.
        await emit_event({"type": "web.fetch.ingested", "url": url, "domain": domain,
                          "dataset": ds_for_ingest, "entities": ents,
                          "error": (err or "")[:200]})

    # the reader version, kept as the page is read (canvas-enrichment slice 2): the html already fetched is read as an
    # article in the background and kept in the reader cache, so reading it again - the model, the canvas - is instant
    if html and not block_reason and status < 400:
        _rmod = sys.modules.get("Vera.vera.web.browser_capabilities") or sys.modules.get("browser_capabilities")
        _rr = getattr(_rmod, "reader_read", None) if _rmod else None
        if _rr:
            _spawn(_rr(url, html=html, mode="fetch"))

    ds_for_ingest = ""
    if ingest_to_fabric and text and not block_reason:
        ds_for_ingest = ((dataset_id or "").strip()
                         or (_auto_web_dataset(url) if disc
                             else "web.crawl." + _sanitise_domain(domain)))
        out["fabric_dataset"] = ds_for_ingest
        if await_ingest:
            await _ingest_page()
            out["ingest"] = "done"
        else:
            _spawn(_ingest_page())
            out["ingest"] = "scheduled"
    else:
        out["ingest"] = "skipped"

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
    description="Recursive crawl from a seed URL. Same-domain only. WHEN TO USE: you know "
                "the SITE (not just a search query) and need to follow a few of its own links "
                "— e.g. 'check nature.com's AI section for recent articles'. Lighter than "
                "fabric.discover.crawl (no surface/subtable/entity detection, no resumable "
                "frontier) — for a quick, disposable read pass ingest_to_fabric=false; only "
                "set it true when you actually want this persisted into the data fabric as a "
                "reusable dataset, not for a one-off lookup inside a loop step. "
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
    url = _wc.canonical_crawl_url(url)
    if not url:
        return {"error": "valid http(s) url required", "pages": []}
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
    content_seen: set = set()
    pages: List[Dict[str, Any]] = []
    rf = _research_fabric() if ingest_to_fabric else None
    fabric_records: List[Dict[str, Any]] = []

    # One shared session for the whole walk — one TLS handshake, and cookies
    # (e.g. consent cookies set by page 1) persist across pages like a browser.
    session = _wc.new_session(timeout)

    async def _fetch_one(u: str, current_depth: int):
        u = _wc.canonical_crawl_url(u)
        if (not u or _wc.crawl_scope(u) != _wc.crawl_scope(url)
                or u in visited or len(visited) >= max_pages):
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
        content_key = _wc.crawl_content_fingerprint(text)
        if content_key and content_key in content_seen:
            await emit_event({"type": "web.crawl.duplicate", "url": u})
            return
        if content_key:
            content_seen.add(content_key)
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
    # what the chat's tool picker matches words against (info-seeking plan SK-0.4): the default for current facts
    tags=["web", "search", "research", "latest", "news", "updates", "current", "facts", "lookup"],
    memory="on",
    description="Look something up on the web and READ the top pages in ONE call (seconds). "
                "WHEN TO USE: the default for any question about the outside world - a fact, "
                "'what is X', 'find out about Y', 'the latest news / updates on Z', anything current "
                "or that may have changed since training. Answer from the returned page text and cite "
                "it with dates (`published` when the page gives one); if the pages do not answer, "
                "search again with a sharper query rather than guessing. Not LONG-RUNNING. Runs ONE "
                "web search, then reads the top-N result pages in PARALLEL through the reader (article "
                "text, cached for later; plain fetch as the fallback) and returns their text INLINE. "
                "Extra un-read result links come back under `more_links`. It does not crawl or write "
                "datasets to the fabric. For a long written report the user will read later use "
                "research.run (a background job, minutes); for one known URL use web.fetch. "
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

    # HOW IT STANDS, AS IT GOES (WSU-41): the same info.seek.step events info.seek sends - the stage, what
    # it has checked, the pages in hand (read or not yet) - keyed by the query, which is the question the chat's live
    # item for this call holds. Best effort: a lost event changes nothing.
    checked: List[str] = []
    have: List[Dict[str, Any]] = []
    async def step(stage: str) -> None:
        try:
            await emit_event({"type": "info.seek.step", "question": query[:300], "need": "lookup", "subject": query[:120],
                              "trace_id": str(trace_id or ""),   # the call's own key (DA-04a: lookups matched by trace)
                              "stage": stage, "checked": checked[-8:], "elapsed_ms": int((time.monotonic() - t0) * 1000),
                              "items": [{"title": str(h.get("title") or "")[:160], "url": h.get("url") or "",
                                         "site": h.get("site") or h.get("domain") or "", "published": h.get("published") or "",
                                         "read": bool(h.get("read"))} for h in have[:12]]})
        except Exception:
            pass
    await step("searching the web")

    # 1. Broad search. discover="off" — we do our own content pull below and don't
    #    want to also kick off the background snippet/crawl processing here. Ask for
    #    more hits than we fetch so `more_links` has real follow-up candidates.
    sr = await cap_web_search(query=query, limit=max(results * 2, results),
                              engine=engine, discover="off")
    hits = [h for h in (sr.get("results") or []) if h.get("url")]
    top  = hits[:results]
    checked.append(f"search: {len(hits)} results" + (f" ({sr.get('engine_used')})" if sr.get("engine_used") else ""))
    have.extend({"title": h.get("title") or "", "url": h.get("url"), "domain": h.get("domain") or ""} for h in top)
    await step(f"reading the top {len(top)} pages" if top else "no results")

    # 2. Pull the useful content from the top results IN PARALLEL. ingest_to_fabric
    #    stays False — this is a fast read, not a crawl-and-store.
    #    THE ARTICLE, NOT THE PAGE (canvas-enrichment slice 2 - owner, 2026-10-03: "reader mode should be cached as the
    #    site is crawled - ideally this is the output the LLM should see to cut out rubbish from its input"): each page
    #    is read through reader.page - the reader cache, else the fetched page read as an article and kept - so the
    #    model reads the article (and the canvas's reader mode opens it at once later); a page reader.page cannot read
    #    falls back to the plain fetch, as before.
    reader = (_orch.CAPABILITY_REGISTRY.get("reader.page") or {}).get("func")

    async def _read(h):
        if reader:
            try:
                r = await asyncio.wait_for(reader(url=h["url"], max_chars=max_chars, mode="fetch"), fetch_timeout + 8.0)
                if isinstance(r, dict) and r.get("ok") and str(r.get("markdown") or "").strip():
                    md = str(r["markdown"])
                    return {"title": r.get("title") or "", "text": md, "chars": len(md), "blocked": False,
                            "via_reader": r.get("via") == "reader-proxy", "reader": True, "cached": bool(r.get("cached")),
                            "byline": r.get("byline") or "", "published": r.get("published") or ""}
            except Exception as e:                               # the plain fetch below is the fallback
                log.debug("web.research reader [%s]: %s", h.get("url"), e)
        return await cap_web_fetch(url=h["url"], timeout=fetch_timeout, max_chars=max_chars, ingest_to_fabric=False)

    async def _read_told(i, h):
        f = await _read(h)
        try:                                                     # this page is in hand: the item shows it read
            ok = isinstance(f, dict) and not f.get("error")
            have[i]["read"] = ok and bool(f.get("text"))
            if ok and f.get("published"):
                have[i]["published"] = f["published"]
            n = sum(1 for x in have if x.get("read"))
            await step(f"read {n} of {len(top)} pages")
        except Exception:
            pass
        return f

    fetches = await asyncio.gather(*[_read_told(i, h) for i, h in enumerate(top)], return_exceptions=True)

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
            if f.get("reader"):
                src["reader"], src["cached"] = True, bool(f.get("cached"))
                for k in ("byline", "published"):
                    if f.get(k):
                        src[k] = f[k]
        else:
            err = str(f) if isinstance(f, Exception) else (f or {}).get("error", "")
            src.update({"text": "", "chars": 0, "blocked": True, "error": err[:160]})
        sources.append(src)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    checked.append(f"read: {sum(1 for x in sources if x.get('chars'))} of {len(sources)} pages")
    await step("done")
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
# info.seek — the one front door for finding information (info-seeking plan SK-3)
# ─────────────────────────────────────────────────────────────────────────────
try:
    from Vera.vera.web import info_seek_core as _seek
except Exception:                                   # worktree / app-free import
    from vera.web import info_seek_core as _seek


async def _seek_call(name: str, timeout: float, **kw) -> Dict[str, Any]:
    """One sibling cap, bounded; a missing cap, an error or a timeout is {} with the reason under _error."""
    fn = (_orch.CAPABILITY_REGISTRY.get(name) or {}).get("func")
    if not fn:
        return {"_error": f"{name} not loaded"}
    try:
        r = await asyncio.wait_for(fn(**kw), timeout)
        return r if isinstance(r, dict) else {"result": r}
    except asyncio.TimeoutError:
        return {"_error": f"{name} timed out after {timeout:.0f} s"}
    except Exception as e:
        return {"_error": f"{name}: {str(e)[:120]}"}


# Searches one info.seek sends to SearXNG start this far apart (they still overlap). Four at once hit every upstream
# engine four times in the same instant, and brave / duckduckgo answered this IP with "too many requests" and CAPTCHAs
# (2026-10-10). Env VERA_SEEK_SPACING_S overrides; 0 sends them together.
SEEK_SPACING_S = max(0.0, min(5.0, float(os.getenv("VERA_SEEK_SPACING_S", "0.5") or 0.5)))


async def _seek_searches(queries: List[str], *, first_slot: int = 0) -> List[Dict[str, Any]]:
    """web.search for each query, the i-th started (first_slot + i) * SEEK_SPACING_S after the call; results in
    query order."""
    async def one(i: int, q: str) -> Dict[str, Any]:
        delay = (first_slot + i) * SEEK_SPACING_S
        if delay:
            await asyncio.sleep(delay)
        return await _seek_call("web.search", 25, query=q, limit=8, discover="off")
    return list(await asyncio.gather(*[one(i, q) for i, q in enumerate(queries)]))


_LAST_SEEK_KEY = "vera:info:last_seek:"
_LAST_SEEK_TTL = 90 * 86400


async def _last_seek(subject: str, set_to: str = "") -> str:
    """When a 'latest' question about `subject` was last answered (Redis, 90 days), or "" - and record a new time
    when `set_to` is given. Never raises: without Redis, latest just uses its default window."""
    r = getattr(_orch, "REDIS", None)
    key = _LAST_SEEK_KEY + _seek.subject_key(subject)
    if r is None or key == _LAST_SEEK_KEY:
        return ""
    try:
        if set_to:
            await r.set(key, set_to, ex=_LAST_SEEK_TTL)
            return set_to
        v = await r.get(key)
        return v.decode() if isinstance(v, (bytes, bytearray)) else str(v or "")
    except Exception as e:
        log.debug("info.seek last-seek %s: %s", key, e)
        return ""


def _report_seller(title: str, snippet: str) -> bool:
    """Market-research sellers ("... market size, share, CAGR 2026-2033") are not news; intel's rule, reused."""
    try:
        try:
            from Vera.vera.intel import intel_ops_core as _iops
        except Exception:
            from vera.intel import intel_ops_core as _iops
        return bool(_iops.report_seller_hint(title or "", snippet or ""))
    except Exception:
        return False


def _searxng_host() -> str:
    """The SearXNG the operator configured (the research subsystem's source), else the env/default one."""
    ra = _researcher_api()
    try:
        src = next((s for s in (getattr(ra, "sources", None) or []) if getattr(s, "id", "") == "searxng"), None)
        conf = src.config if src is not None else {}
        if isinstance(conf, str):
            conf = json.loads(conf or "{}")
        host = (conf or {}).get("host") if isinstance(conf, dict) else ""
        if host:
            return str(host).rstrip("/")
    except Exception:
        pass
    return DEFAULT_SEARXNG


async def _searxng_news(query: str, limit: int = 10, time_range: str = "month") -> List[Dict[str, Any]]:
    """SearXNG's NEWS category (bing/google/yahoo/qwant news...) inside a time range: current headlines with the
    age each engine states ("14 hours ago | Source"). The general search behind web.search has no notion of time,
    so 'latest' questions got evergreen explainers ranked first. Empty when SearXNG cannot be reached."""
    host = _searxng_host()
    params = _engines.searxng_params(query, 0, 1, categories="news", time_range=time_range)
    for cand in _engines.host_candidates(host) or [host]:
        try:
            async with httpx.AsyncClient(timeout=12.0, headers=_engines.SEARXNG_HEADERS) as c:
                r = await c.get(f"{cand}/search", params=params)
                r.raise_for_status()
                body = r.json()
        except Exception as e:
            log.debug("searxng news [%s] %s: %s", query[:40], cand, e)
            continue
        out = []
        for it in body.get("results") or []:
            if not it.get("url"):
                continue
            pub = it.get("publishedDate")
            out.append({"url": _decode_redirect(it.get("url", "")), "title": it.get("title", ""),
                        "snippet": it.get("content", "") or "", "engine": it.get("engine", "searxng-news"),
                        "published": "" if pub in (None, "None") else str(pub),
                        "age_text": str(it.get("metadata") or "")})
        return out[:limit]
    return []


async def _feed_hits(subject: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Recent items from the feeds Vera follows (the trade press, BBC... added as fabric sources through intel) whose
    title or summary names every content word of the subject - so a subject nobody tracks still gets what those feeds
    carried (plan SK-3.5). Bounded: at most 12 feeds, 40 items each, 6 s per call."""
    try:
        try:
            from Vera.vera.intel import intel_sources_core as _isrc
        except Exception:
            from vera.intel import intel_sources_core as _isrc
    except Exception:
        return []
    # plural words match their stem ("prices" -> "price": "UK house price growth halves" names "house prices")
    words = [(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w)
             for w in (x.lower() for x in _seek.keywords_of(subject).split()) if len(w) > 2]
    if not words:
        return []
    src = await _seek_call("fabric.sources", 6)
    feeds = [s for s in (src.get("sources") or []) if str(s.get("id") or "").startswith("intel-src-")
             and s.get("dataset_id")][:12]

    async def one(s: Dict[str, Any]) -> List[Dict[str, Any]]:
        r = await _seek_call("fabric.browse", 6, dataset_id=s["dataset_id"], limit=40)
        out = []
        for rec in r.get("records") or []:
            hit = _isrc.record_hit(rec, kind="feed")
            if not hit or not hit.get("url"):
                continue
            text = f"{hit.get('title') or ''} {hit.get('snippet') or ''}".lower()
            if all(w in text for w in words):
                out.append(dict(hit, kind="web", engine="feed: " + str(s.get("label") or s.get("name") or s["id"])))
        return out
    got = await asyncio.gather(*[one(s) for s in feeds], return_exceptions=True)
    return [h for g in got if isinstance(g, list) for h in g][:limit]


async def _seek_read(items: List[Dict[str, Any]], n: int, chars: int) -> int:
    """Read up to `n` items through the reader (article text, cached), in order, in parallel. A page that cannot be
    read - blocked by an anti-bot challenge, no article - is marked and the next result is read in its place (a
    second batch), so a blocked site costs one slot, not the answer. A page's own published date fills one the
    search did not give. Returns how many yielded text."""
    def candidates():
        return [it for it in items if not it.get("text") and not it.get("unreadable") and it.get("url")
                and it.get("kind") != "intel"]

    async def one(it):
        r = await _seek_call("reader.page", 25, url=it["url"], max_chars=max(chars * 3, 4000), mode="fetch")
        md = str(r.get("markdown") or "") if r.get("ok") else ""
        if not md.strip():
            it["unreadable"] = str(r.get("error") or r.get("_error") or "no article")[:80]
            return 0
        it["text"] = _seek.excerpt(md, chars)
        it["title"] = r.get("title") or it.get("title")
        if r.get("published") and not it.get("published"):
            it["published"] = r["published"]
        if not it.get("published"):            # the page's own "Updated on ..." line (SK-3.3)
            d = _seek.labelled_date(md)
            if d:
                it["published"] = d.strftime("%Y-%m-%d")
        return 1
    got = 0
    for _ in range(2):                       # the first batch, then one refill for what failed
        todo = candidates()[:max(0, n - got)]
        if not todo:
            break
        res = await asyncio.gather(*[one(it) for it in todo], return_exceptions=True)
        got += sum(x for x in res if isinstance(x, int))
        if got >= n:
            break
    return got


async def _seek_rerank(subject: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Order by relevance to the SUBJECT (short query, the reranker's strength); the search order when the NLP tier
    is unavailable."""
    if len(items) < 3:
        return items
    docs = [f"{it.get('title') or ''}. {it.get('snippet') or ''}"[:500] for it in items]
    rr = await _seek_call("nlp.rerank", 15, query=subject, documents=docs, top_k=len(docs))
    ranked = rr.get("ranked") or []
    if not ranked:
        return items
    order = []
    for x in ranked:
        try:
            i = int(x.get("index"))
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(items) and i not in order:
            order.append(i)
            items[i]["relevance"] = round(float(x.get("score") or 0.0), 2)
    order += [i for i in range(len(items)) if i not in order]
    # far off-topic results (cross-encoder logits well below zero) go; never all of them
    kept = [items[i] for i in order if items[i].get("relevance", 0.0) > -6.0]
    return kept or [items[i] for i in order]


@capability(
    "info.seek",
    http_method="POST", http_path="/info/seek",
    http_tags=["web", "info"], memory="on",
    tags=["info", "search", "latest", "news", "updates", "facts", "lookup", "research", "current", "web"],
    description="FIND INFORMATION to answer a question - the front door. It decides what the question needs and "
                "runs the right lookups together, returning numbered, DATED evidence to cite. "
                "WHEN TO USE: any question about the outside world - 'what's the latest on X', 'updates on X', "
                "'what is X', 'explain/compare X', a price or a fact that may have changed since training. "
                "need=latest (auto for latest/news/updates): checks what Vera already tracks on the subject "
                "(intel), searches dated news plus up to three web searches, drops report-sellers, orders newest "
                "first and reads the newest pages (~10-40 s). need=fact: the subject's keywords and the question, "
                "ranked by whether they answer it, the best pages read, a second round when they do not. "
                "need=overview: three angles, one page a site, more pages read (~20-60 s). need=deep: the "
                "overview now plus research.run started in the background for a full report (job_id returned). "
                "Input: question (str!), need (auto|fact|latest|overview|deep, default auto), since (ISO date: "
                "what counts as new for latest; default 14 days), topic (the subject when the question is long), "
                "pages (int, pages to read, default 5, max 8), excerpt_chars (int, per page, default 900), "
                "start_report (bool, default true: need=deep starts research.run; loops pass false). "
                "Output: {need, subject, since, answerable, items:[{n, title, url, site, published, age_days, "
                "kind (web|intel), read, text}], checked, gaps, guide, tracked?, job_id?, elapsed_ms}. Cite items "
                "as [n] with their dates; report the gaps instead of guessing.",
)
async def cap_info_seek(question: str = "", need: str = "auto", since: str = "", topic: str = "",
                        pages: int = 5, excerpt_chars: int = 900, start_report: bool = True,
                        trace_id=None) -> Dict[str, Any]:
    question = " ".join(str(question or "").split())
    if not question:
        return {"error": "question required"}
    t0 = time.monotonic()
    now = datetime.now(timezone.utc)
    cls = _seek.classify_need(question)
    need = (need or "auto").strip().lower()
    if need not in _seek.NEEDS:
        need = cls["need"]
    subject = (topic or "").strip() or _seek.subject_of(question)
    pages = max(1, min(8, int(pages or 5)))
    chars = max(300, min(3000, int(excerpt_chars or 900)))
    checked: List[str] = [f"need: {need} ({cls['reason'] if need == cls['need'] else 'asked for'})"]
    gaps: List[str] = []
    extra: Dict[str, Any] = {}
    items: List[Dict[str, Any]] = []

    # HOW THE LOOKUP STANDS, AS IT GOES (owner, 2026-10-11: "the research family of caps has rich output while its
    # running that need to be communicated"): each stage is an info.seek.step event - what it is doing, what it has
    # checked so far, the pages it has in hand - keyed by the question, so the chat's item for this call can show the
    # lookup working rather than a card that appears only at the end. Best effort: a lost event changes nothing.
    async def step(stage: str, have: Optional[List[Dict[str, Any]]] = None) -> None:
        try:
            await emit_event({"type": "info.seek.step", "question": question[:300], "need": need, "subject": subject[:120],
                              "trace_id": str(trace_id or ""),   # the call's own key (DA-04a), as web.research's steps
                              "stage": stage, "checked": checked[-8:], "elapsed_ms": int((time.monotonic() - t0) * 1000),
                              "items": [{"title": str(h.get("title") or "")[:160], "url": h.get("url") or "",
                                         "site": h.get("site") or h.get("domain") or "", "published": h.get("published") or "",
                                         "read": bool(h.get("text"))} for h in (have or [])[:12]]})
        except Exception:
            pass
    await step("planning: need " + need + " · " + subject[:80])

    # what Vera already tracks on the subject - milliseconds, so always asked
    intel_task = asyncio.create_task(_seek_call("intel.lookup", 8, query=subject, since=since))

    if need == "latest":
        if not since:
            sf = _seek.since_for(await _last_seek(subject), now)
            since = sf["since"]
            checked.append("new means: " + sf["why"])
        await _last_seek(subject, now.isoformat(timespec="seconds"))
        queries = _seek.latest_queries(subject, question, now)
        span = "week" if (now - (_seek.parse_date(since) or now)).days <= 8 else "month"
        news_task = asyncio.create_task(_searxng_news(subject, 12, span))
        await step(f"searching: the news of the past {span} and {len(queries)} web searches")
        feed_task = asyncio.create_task(_feed_hits(subject))
        res = await _seek_searches(queries, first_slot=1)        # the news search has slot 0
        try:
            news = await asyncio.wait_for(news_task, 20)
        except Exception:
            news = []
        try:
            feeds = await asyncio.wait_for(feed_task, 15)
        except Exception:
            feeds = []
        # news first: it is what is current; then the feeds Vera follows; the general results add depth
        hits = _seek.merge_hits([news, feeds] + [r.get("results") or [] for r in res])
        engines = sorted({r.get("engine_used") for r in res if r.get("engine_used")})
        checked.append(f"news: {len(news)} headlines from the past {span}")
        if feeds:
            checked.append(f"feeds Vera follows: {len(feeds)} items naming '{subject}'")
        checked.append(f"web: {len(queries)} searches ({', '.join(engines) or 'no engine answered'}), {len(hits)} results")
        _seek.fill_dates(hits, now)
        dropped = [h for h in hits if _report_seller(h.get("title"), h.get("snippet"))]
        hits = [h for h in hits if h not in dropped]
        if dropped:
            checked.append(f"dropped {len(dropped)} market-report sellers")
        await step(f"ranking {len(hits)} results")
        hits = await _seek_rerank(subject, hits)
        items = [dict(h, kind="web") for h in _seek.per_site(hits, 2)][:12]
        # newest first BEFORE reading: the pages read are the new ones, not the evergreen explainers relevance
        # ranks high (prod, 10 Oct: every read page was an undated explainer while the dated news went unread)
        items = _seek.order_latest(items, since, now)
        await step(f"reading the {min(pages, len(items))} newest pages", items)
        got = await _seek_read(items, pages, chars)
        checked.append(f"read {got} of the top pages")
        items = _seek.order_latest(items, since, now)          # a page read may have given its own date
        await step("read the pages; checking what Vera tracks", items)
    else:
        # fact / overview / deep: the subject's keywords first, then other angles; ranked against the QUESTION (the
        # cross-encoder judges "does this page answer it"), at most two pages a site, the best pages read
        queries = (_seek.fact_queries(subject, question) if need == "fact"
                   else _seek.overview_queries(subject, question, now))
        await step(f"searching: {len(queries)} web searches")
        res = await _seek_searches(queries)
        hits = _seek.merge_hits([r.get("results") or [] for r in res])
        engines = sorted({r.get("engine_used") for r in res if r.get("engine_used")})
        checked.append(f"web: {len(queries)} searches ({', '.join(engines) or 'no engine answered'}), {len(hits)} results")
        _seek.fill_dates(hits, now)
        await step(f"ranking {len(hits)} results against the question")
        hits = await _seek_rerank(question, hits)
        cap = 2 if need == "fact" else 1          # an overview wants many sites, a fact may take two pages of one
        items = [dict(h, kind="web") for h in _seek.per_site(hits, cap)][:10]
        n_read = pages if need == "fact" else min(8, pages + 2)
        await step(f"reading the {min(n_read, len(items))} best pages", items)
        got = await _seek_read(items, n_read, chars)
        checked.append(f"read {got} of the top pages")
        await step("read the pages", items)
        if need == "fact" and got < 2 and len(queries) < 3:
            # depth over speed: the pages did not carry an answer - one sharper round before giving up
            q3 = f"{subject} {now.year}"
            r3 = await _seek_call("web.search", 25, query=q3, limit=8, discover="off")
            more = _seek.merge_hits([items, r3.get("results") or []])[len(items):]
            _seek.fill_dates(more, now)
            more = [dict(h, kind="web") for h in (await _seek_rerank(question, more))][:5]
            got2 = await _seek_read(more, 3, chars)
            items = (items + more)[:10]
            checked.append(f"second round '{q3}': {len(more)} results, read {got2}")
        # what was read leads: an unread result is only its snippet
        items = [it for it in items if it.get("text")] + [it for it in items if not it.get("text")]
        if need == "deep" and not start_report:
            checked.append("deep: the overview only - start_report=false (the caller runs no background jobs)")
        elif need == "deep":
            job = await _seek_call("research.run", 20, query=question, mode="parallel", output_mode="report")
            if job.get("job_id"):
                extra["job_id"] = job["job_id"]
                checked.append("research.run started in the background for the full report (job_id)")
            else:
                gaps.append("the background report could not start: " + str(job.get("_error") or job.get("error")))

    intel = await intel_task
    if intel.get("tracked"):
        tp = intel.get("topic") or {}
        snap = intel.get("snapshot") or {}
        extra["tracked"] = {"topic_id": tp.get("id"), "name": tp.get("name"),
                            "assessment": snap.get("headline") or "", "assessment_ts": snap.get("ts") or "",
                            "assessment_age_days": snap.get("age_days")}
        isigs = [{"title": s.get("title"), "url": s.get("url"), "published": s.get("published") or s.get("seen"),
                  "snippet": s.get("summary") or "", "kind": "intel"} for s in (intel.get("signals") or [])
                 if s.get("url")]
        known = {_seek.canon_url(it.get("url") or "") for it in items}
        isigs = [s for s in isigs if _seek.canon_url(s["url"]) not in known][:4]
        if need == "latest":
            items = _seek.order_latest(items + isigs, since, now)
        else:
            items = items + isigs
        checked.append(f"intel: tracked as '{tp.get('name')}' ({len(isigs)} signals since {intel.get('since', '')[:10]})")
    elif intel.get("_error"):
        checked.append("intel: " + intel["_error"])
    else:
        checked.append("intel: not tracked" + (f" (near: {', '.join(m['name'] for m in intel.get('matches') or [])})"
                                               if intel.get("matches") else ""))
        if need == "latest":
            extra["offer_tracking"] = f"Vera does not follow '{subject}'; offer to track it (intel.topic.save name=...)"

    if not any(it.get("kind") == "web" for it in items):
        gaps.append("the web searches returned nothing usable - see /health search")
    pack = _seek.build_pack(need, question, subject, items[:10], since=since if need == "latest" else "",
                            checked=checked, gaps=gaps, now=now,
                            elapsed_ms=int((time.monotonic() - t0) * 1000), extra=extra)
    await emit_event({"type": "info.seek.done", "need": need, "subject": subject[:80], "items": len(pack["items"]),
                      "elapsed_ms": pack["elapsed_ms"]})
    return pack


# ─────────────────────────────────────────────────────────────────────────────
# Module load message
# ─────────────────────────────────────────────────────────────────────────────
log.info("web_capabilities: loaded — caps registered: web.search, web.fetch, "
         "web.crawl, web.search_and_crawl, web.research")
