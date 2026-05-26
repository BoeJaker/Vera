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
- SearXNG host comes from env `VERA_SEARXNG_URL` (default: http://llm.int:8888)
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
_orch = (sys.modules.get("Vera.Orchestration.capability_orchestration") or
         sys.modules.get("capability_orchestration"))
if not _orch:
    raise RuntimeError("web_capabilities: capability_orchestration module not loaded")

capability  = _orch.capability
emit_event  = _orch.emit_event

log = logging.getLogger("vera.web_capabilities")


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_SEARXNG = os.getenv("VERA_SEARXNG_URL", "http://llm.int:8888").rstrip("/")
DEFAULT_TIMEOUT = float(os.getenv("VERA_WEB_TIMEOUT", "8.0"))
USER_AGENT      = os.getenv("VERA_WEB_UA", "Vera-Research/1.0 (+https://vera.local)")
HEADERS         = {"User-Agent": USER_AGENT}

MAX_CRAWL_PAGES = 25       # hard ceiling per single crawl call
MAX_PAGE_CHARS  = 16000    # max chars to extract per page


def _research_fabric():
    return (sys.modules.get("Vera.Orchestration.research_fabric") or
            sys.modules.get("research_fabric"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sanitise_domain(domain: str) -> str:
    """Turn a domain into a safe dataset suffix."""
    return re.sub(r"[^a-z0-9_]+", "_", (domain or "unknown").lower()).strip("_") or "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# HTML → TEXT (mirrors researcher_api.html_to_text but standalone)
# ─────────────────────────────────────────────────────────────────────────────

def _html_to_text(html: str, preserve_structure: bool = True) -> str:
    """Strip HTML tags, decode entities, collapse whitespace."""
    if not html:
        return ""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<noscript[^>]*>.*?</noscript>", " ", text, flags=re.S | re.I)
    if preserve_structure:
        text = re.sub(r"</?(?:p|div|section|article|br|li|h[1-6])[^>]*>",
                      "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r" {3,}", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()[:MAX_PAGE_CHARS]


def _extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        return _html.unescape(m.group(1).strip())[:200]
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
    if m:
        return _html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()[:200]
    return ""


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


async def _dispatch_search(query: str, limit: int, engine: str = "auto",
                           searxng_host: str = "",
                           brave_api_key: str = "") -> Tuple[List[Dict[str, Any]], str]:
    """
    Try engines in order. 'auto' = searxng → brave → ddg.
    Returns (results, used_engine).
    """
    engine = (engine or "auto").lower()
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
    description="Search the web. Tries searxng → brave → ddg in order, or a "
                "specific engine if requested. "
                "Input: query (str!), limit (int default 8), "
                "engine (str: auto|searxng|brave|ddg), "
                "searxng_host (str — override default host), "
                "brave_api_key (str — override env var). "
                "Output: {results: [{url, title, snippet, engine}], "
                "engine_used, query, count}.",
)
async def cap_web_search(
    query:          str,
    limit:          int = 8,
    engine:         str = "auto",
    searxng_host:   str = "",
    brave_api_key:  str = "",
    trace_id=None,
) -> Dict[str, Any]:
    if not query.strip():
        return {"results": [], "engine_used": "none", "query": query,
                "count": 0, "error": "query required"}
    limit = max(1, min(50, int(limit)))
    t0 = time.monotonic()
    results, used = await _dispatch_search(query, limit, engine,
                                            searxng_host=searxng_host,
                                            brave_api_key=brave_api_key)
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    await emit_event({
        "type":         "web.search.done",
        "query":        query[:100],
        "engine_used":  used,
        "count":        len(results),
        "elapsed_ms":   elapsed_ms,
    })
    return {
        "query":       query,
        "engine_used": used,
        "results":     results,
        "count":       len(results),
        "elapsed_ms":  elapsed_ms,
    }


@capability(
    "web.fetch",
    http_method="POST", http_path="/web/fetch", http_tags=["web", "fetch"],
    memory="auto",
    description="Fetch one URL and extract clean text + title. "
                "Input: url (str!), timeout (float default 8.0), "
                "max_chars (int default 16000), "
                "ingest_to_fabric (bool default False — write page to "
                "web.crawl.<domain> dataset). "
                "Output: {url, title, text, domain, status_code, "
                "fetched_at, chars}.",
)
async def cap_web_fetch(
    url:               str,
    timeout:           float = DEFAULT_TIMEOUT,
    max_chars:         int   = MAX_PAGE_CHARS,
    ingest_to_fabric:  bool  = False,
    trace_id=None,
) -> Dict[str, Any]:
    if not url.strip():
        return {"error": "url required", "url": url}
    timeout = max(1.0, min(60.0, float(timeout)))
    max_chars = max(500, min(MAX_PAGE_CHARS, int(max_chars)))

    domain = urlparse(url).netloc
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=HEADERS,
                                      follow_redirects=True) as c:
            r = await c.get(url)
            html = r.text
            status = r.status_code
    except Exception as e:
        await emit_event({"type": "web.fetch.error", "url": url, "error": str(e)[:120]})
        return {"error": str(e), "url": url, "domain": domain}

    text = _html_to_text(html)[:max_chars]
    title = _extract_title(html)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    fetched_at = _now_iso()

    out = {
        "url":         url,
        "title":       title,
        "text":        text,
        "domain":      domain,
        "status_code": status,
        "fetched_at":  fetched_at,
        "chars":       len(text),
        "elapsed_ms":  elapsed_ms,
    }

    # Optional: ingest to fabric
    if ingest_to_fabric and text:
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

    visited: set = set()
    pages: List[Dict[str, Any]] = []
    rf = _research_fabric() if ingest_to_fabric else None
    fabric_records: List[Dict[str, Any]] = []

    async def _fetch_one(u: str, current_depth: int):
        if u in visited or len(pages) >= max_pages:
            return
        visited.add(u)
        try:
            async with httpx.AsyncClient(timeout=timeout, headers=HEADERS,
                                          follow_redirects=True) as c:
                r = await c.get(u)
                html = r.text
                status = r.status_code
        except Exception as e:
            await emit_event({"type": "web.crawl.error", "url": u, "error": str(e)[:120]})
            return

        text = _html_to_text(html)
        title = _extract_title(html)
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

    await _fetch_one(url, depth)

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
    description="Composite: search the web, then crawl the top-N results. "
                "Each crawled page is ingested to fabric. "
                "Input: query (str!), search_limit (int default 5), "
                "crawl_depth (int default 1), pages_per_result (int default 3), "
                "engine (str default auto), ingest_to_fabric (bool default True). "
                "Output: {query, search_results, crawl_summary: [{seed, page_count, "
                "total_chars}], fabric_dataset_count}.",
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

    # 1. Search
    search_res = await cap_web_search(query=query, limit=search_limit, engine=engine)
    results = search_res.get("results") or []

    # 2. Crawl each in parallel — but bounded so we don't melt the network
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


# ─────────────────────────────────────────────────────────────────────────────
# Module load message
# ─────────────────────────────────────────────────────────────────────────────
log.info("web_capabilities: loaded — caps registered: web.search, web.fetch, "
         "web.crawl, web.search_and_crawl")