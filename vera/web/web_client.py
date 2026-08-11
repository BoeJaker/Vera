"""
web_client.py — Shared hardened HTTP fetch layer for ALL of Vera's web access.
===============================================================================

Why this exists
───────────────
Every subsystem that touched the web (web_capabilities, researcher_api,
fabric_web_acquisition, …) grew its own fetch code with its own headers. The
web caps got hardened (realistic browser fingerprint, anti-bot interstitial
detection, reader-proxy fallback) but the research pipeline kept announcing
itself as "Vera-Research/1.0" — an instant block on Cloudflare / DataDome /
Reddit. Centralising the fetch here means the hardening is done once and can
never drift between subsystems again.

What a fetch does here
──────────────────────
  1. API switchover — if an API-provider hook is registered (web_api
     capabilities module) and a configured platform API matches the URL's
     domain (e.g. Reddit via its OAuth API), the page is fetched through the
     platform API instead of scraping. Cleanest, never blocked, compliant.
  2. Domain rewrite — hostile hosts are rewritten to friendlier equivalents
     (www.reddit.com → old.reddit.com by default; extensible via
     VERA_WEB_REWRITES and add_rewrite()).
  3. Per-domain throttle — a minimum interval + jitter between hits to the
     same domain, so crawls don't trip rate limiters (429s).
  4. Direct fetch — realistic desktop-Chrome fingerprint (UA + Sec-CH-UA
     client hints) over HTTP/2 when the `h2` package is available. A Chrome
     UA speaking HTTP/1.1 is itself a bot fingerprint Cloudflare scores.
  5. Block detection — Cloudflare / DataDome / PerimeterX / consent /
     CAPTCHA interstitials are recognised so challenge boilerplate is never
     mistaken for page content.
  6. Reader fallback — when blocked (or the page yielded almost no text),
     retry through a server-side-rendering reader proxy (default: Jina AI's
     r.jina.ai; VERA_WEB_READER="" disables, VERA_WEB_READER_KEY adds auth
     for the higher rate limit).

Usage
─────
    from Vera.vera.web import web_client as wc

    page = await wc.fetch_page(url)                  # one-shot
    async with wc.new_session() as client:           # crawl: shared TLS +
        for u in urls:                               # cookies across pages
            page = await wc.fetch_page(u, client=client)

    page = {url, final_url, domain, status, html, text, title, chars,
            blocked, block_reason, via ("direct"|"reader"|"api:<id>"),
            via_reader, via_api, elapsed_ms, error, reader_error}

`text` is always extracted plain text (already de-HTML'd — do NOT run it
through an HTML stripper again). `html` is the raw body when the page came
from a direct fetch, "" when it came via reader/API.

Env knobs
─────────
  VERA_WEB_UA               — override the browser UA string
  VERA_WEB_READER           — reader proxy prefix (default https://r.jina.ai/)
  VERA_WEB_READER_KEY       — optional reader API key (higher rate limit)
  VERA_WEB_READER_TIMEOUT   — floor timeout (s) for the reader fallback call
                              (default 25.0 — server-side rendering to clear
                              a JS challenge is reliably slower than a direct
                              fetch; too tight a timeout here silently
                              defeats the fallback on exactly the pages it
                              exists to rescue)
  VERA_WEB_REWRITES         — extra rewrites "host=target,host2=target2"
                              ("host=" with empty target removes a default)
  VERA_WEB_DOMAIN_INTERVAL  — min seconds between hits to one domain (def 1.0)
  VERA_WEB_DOMAIN_JITTER    — extra random 0..N seconds on top (default 0.6)
  VERA_WEB_MAX_PAGE_CHARS   — max chars of text per page (default 16000)
"""

from __future__ import annotations

import asyncio
import html as _htmllib
import logging
import os
import random
import re
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import httpx

log = logging.getLogger("vera.web_client")

# ─────────────────────────────────────────────────────────────────────────────
# HTTP/2 — Chrome always speaks h2; an h1-only client wearing a Chrome UA is a
# fingerprint mismatch anti-bot vendors score against you. httpx needs the
# optional `h2` package for this, so detect rather than require.
# ─────────────────────────────────────────────────────────────────────────────
try:
    import h2  # noqa: F401
    HTTP2 = True
except ImportError:
    HTTP2 = False

# ─────────────────────────────────────────────────────────────────────────────
# Browser fingerprint
# ─────────────────────────────────────────────────────────────────────────────
USER_AGENT = os.getenv(
    "VERA_WEB_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)

BROWSER_HEADERS: Dict[str, str] = {
    "User-Agent":                USER_AGENT,
    "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                                 "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language":           "en-US,en;q=0.9",
    "Sec-CH-UA":                 '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
    "Sec-CH-UA-Mobile":          "?0",
    "Sec-CH-UA-Platform":        '"Windows"',
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "none",
    "Sec-Fetch-User":            "?1",
    "Upgrade-Insecure-Requests": "1",
}
HEADERS = BROWSER_HEADERS  # back-compat alias

READER_PROXY = os.getenv("VERA_WEB_READER", "https://r.jina.ai/").strip()
READER_KEY   = os.getenv("VERA_WEB_READER_KEY", "").strip()

MAX_PAGE_CHARS = int(os.getenv("VERA_WEB_MAX_PAGE_CHARS", "16000"))

DEFAULT_TIMEOUT = float(os.getenv("VERA_WEB_TIMEOUT", "8.0"))

# The reader proxy does server-side rendering (launches a real browser to
# clear a JS challenge) — that's reliably slower than a direct fetch, not
# just "a bit slower": a live probe against a Cloudflare-"Just a moment..."
# page (bulbapedia.bulbagarden.net, 2026-08-11) took 16.5s end to end. The
# old floor of max(timeout, 15.0) — inherited from the direct-fetch timeout,
# itself defaulting to 8s — was routinely too tight for exactly the pages
# this fallback exists to rescue, so the fallback's own httpx call hit ITS
# timeout, raised, and got swallowed by the `except Exception: log.debug`
# below (invisible at the orchestrator's default INFO log level) — the page
# came back reporting blocked=True, via_reader=False, with no visible error
# anywhere. Give it a floor generous enough to actually finish.
READER_TIMEOUT = float(os.getenv("VERA_WEB_READER_TIMEOUT", "25.0"))

# ─────────────────────────────────────────────────────────────────────────────
# Domain rewrites — hostile host → friendlier equivalent serving the same
# content. reddit's www/new UI hard-blocks datacenter IPs and non-JS clients;
# old.reddit.com is server-rendered and far softer.
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_REWRITES: Dict[str, str] = {
    "www.reddit.com": "old.reddit.com",
    "reddit.com":     "old.reddit.com",
    "np.reddit.com":  "old.reddit.com",
    "new.reddit.com": "old.reddit.com",
}

REWRITES: Dict[str, str] = dict(_DEFAULT_REWRITES)
for _pair in os.getenv("VERA_WEB_REWRITES", "").split(","):
    _pair = _pair.strip()
    if not _pair or "=" not in _pair:
        continue
    _h, _t = (s.strip().lower() for s in _pair.split("=", 1))
    if _h and not _t:
        REWRITES.pop(_h, None)      # "host=" removes a default
    elif _h:
        REWRITES[_h] = _t


def add_rewrite(host: str, target: str) -> None:
    """Runtime-extend the rewrite table (e.g. from an API-provider module)."""
    if host:
        REWRITES[host.lower()] = target.lower()


def remove_rewrite(host: str) -> None:
    REWRITES.pop((host or "").lower(), None)


def rewrite_url(url: str) -> str:
    """Apply the domain rewrite table. Returns the URL unchanged if no match."""
    try:
        p = urlparse(url)
        target = REWRITES.get(p.netloc.lower())
        if target:
            return urlunparse(p._replace(netloc=target))
    except Exception:
        pass
    return url


# ─────────────────────────────────────────────────────────────────────────────
# Per-domain throttle — min interval + jitter between hits to the same domain.
# First hit to a domain goes straight through; only repeats wait. asyncio is
# single-threaded so plain-dict setdefault is race-safe here.
# ─────────────────────────────────────────────────────────────────────────────
_MIN_INTERVAL = float(os.getenv("VERA_WEB_DOMAIN_INTERVAL", "1.0"))
_JITTER       = float(os.getenv("VERA_WEB_DOMAIN_JITTER", "0.6"))

_domain_next:  Dict[str, float] = {}
_domain_locks: Dict[str, asyncio.Lock] = {}


async def throttle_domain(domain: str) -> None:
    """Wait until this domain is allowed another request."""
    if not domain or _MIN_INTERVAL <= 0:
        return
    lock = _domain_locks.setdefault(domain, asyncio.Lock())
    async with lock:
        wait = _domain_next.get(domain, 0.0) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _domain_next[domain] = (time.monotonic() + _MIN_INTERVAL
                                + random.uniform(0, _JITTER))
    # Cheap unbounded-growth guard for long-lived processes.
    if len(_domain_next) > 4000:
        for d in list(_domain_next)[:2000]:
            _domain_next.pop(d, None)
            _domain_locks.pop(d, None)


# ─────────────────────────────────────────────────────────────────────────────
# Anti-bot / consent / CAPTCHA interstitial detection
# ─────────────────────────────────────────────────────────────────────────────
_BLOCK_SIGNATURES = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "cf_chl_opt",
    "cf-challenge",
    "challenge-platform",
    "enable javascript and cookies to continue",
    "please enable javascript",
    "please enable cookies",
    "please turn javascript on",
    "attention required",
    "verify you are a human",
    "verifying you are human",
    "are you a robot",
    "px-captcha",
    "captcha-delivery",
    "g-recaptcha",
    "h-captcha",
    "unusual traffic",
    "access to this page has been denied",
    "access denied",
    "request unsuccessful. incapsula",
    "ddos protection by",
    "generated by cloudflare",
    # reddit-specific walls
    "whoa there, pardner",
    "you've been blocked by network security",
    "our cdn was unable to reach our servers",
)


def detect_block(html: str, status_code: int) -> str:
    """
    Return a short reason string if the response looks like an anti-bot /
    consent / CAPTCHA interstitial rather than the real page, else "".
    """
    head = (html or "")[:6000].lower()
    for sig in _BLOCK_SIGNATURES:
        if sig in head:
            return sig
    # A 401/403/429/503 on a tiny body is almost always a challenge page even
    # when the vendor didn't leave one of the fingerprints above.
    if status_code in (401, 403, 429, 503) and len(html or "") < 30000:
        return f"blocked (HTTP {status_code})"
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# HTML → text
# ─────────────────────────────────────────────────────────────────────────────
_MAIN_CONTENT_RE = [
    # Wikipedia's actual article container — checked first since it's the
    # domain observed live pushing real content past a naive full-page strip.
    re.compile(r'<div[^>]+id=["\']mw-content-text["\'][^>]*>(.*)', re.S | re.I),
    re.compile(r'<div[^>]+id=["\']bodyContent["\'][^>]*>(.*)', re.S | re.I),
    # Generic semantic containers most other sites use.
    re.compile(r"<main\b[^>]*>(.*?)</main>", re.S | re.I),
    re.compile(r"<article\b[^>]*>(.*?)</article>", re.S | re.I),
    re.compile(r'<div[^>]+role=["\']main["\'][^>]*>(.*)', re.S | re.I),
]
# A match shorter than this is more likely a wrong/near-empty container than
# the real article body — trust it only when there's substance behind it.
_MIN_MAIN_CONTENT_CHARS = 400


def _extract_main_html(html: str) -> str:
    """Best-effort isolate the page's real content container BEFORE stripping
    tags. Observed live: a naive whole-document strip put Wikipedia's nav
    menu ("Jump to content / Main menu / Main page / Contents / Current
    events / Random article / ...") at the very FRONT of the extracted text,
    ahead of the actual article — a caller reading only the head (or a
    truncated preview) never reaches the real content and wrongly concludes
    the fetch failed, then escalates to a much more expensive and fragile
    tool (browser automation with guessed CSS selectors, or a live search
    engine hit) for data a plain fetch already had.

    Purely additive: falls back to the FULL, unmodified document when no
    recognized container matches or the match is implausibly short — never
    removes anything a narrower heuristic might have gotten wrong, only
    reorders what the existing full-strip already does once a container is
    confidently found."""
    for pat in _MAIN_CONTENT_RE:
        m = pat.search(html)
        if m and len(m.group(1)) >= _MIN_MAIN_CONTENT_CHARS:
            return m.group(1)
    return html


def html_to_text(html: str, preserve_structure: bool = True,
                 max_chars: int = MAX_PAGE_CHARS) -> str:
    """Strip HTML tags, decode entities, collapse whitespace."""
    if not html:
        return ""
    html = _extract_main_html(html)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<noscript[^>]*>.*?</noscript>", " ", text, flags=re.S | re.I)
    if preserve_structure:
        text = re.sub(r"</?(?:p|div|section|article|br|li|h[1-6])[^>]*>",
                      "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _htmllib.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\t", " ").replace(" ", " ")
    text = re.sub(r"[​‌‍﻿]", "", text)
    text = re.sub(r"[ ]{2,}", " ", text)
    text = re.sub(r"[ ]*\n[ ]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_chars]


def extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        return _htmllib.unescape(m.group(1).strip())[:200]
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
    if m:
        return _htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()[:200]
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# API-provider hook — the web_api capabilities module registers an async
# fn(url) -> Optional[{text, title, provider, url?}] here. Returning None means
# "no configured API covers this URL; scrape normally".
# ─────────────────────────────────────────────────────────────────────────────
_API_HOOK: Optional[Callable[[str], Awaitable[Optional[Dict[str, Any]]]]] = None


def set_api_hook(fn: Optional[Callable[[str], Awaitable[Optional[Dict[str, Any]]]]]) -> None:
    global _API_HOOK
    _API_HOOK = fn


# ─────────────────────────────────────────────────────────────────────────────
# Sessions
# ─────────────────────────────────────────────────────────────────────────────
def new_session(timeout: float = DEFAULT_TIMEOUT,
                headers: Optional[Dict[str, str]] = None) -> httpx.AsyncClient:
    """
    A browser-fingerprinted client for reuse across a crawl: one TLS handshake,
    cookies persist between pages (consent cookies set on page 1 are presented
    on page 2 — exactly what a real browser does).
    """
    return httpx.AsyncClient(timeout=timeout,
                             headers=headers or BROWSER_HEADERS,
                             follow_redirects=True,
                             http2=HTTP2)


async def _fetch_via_reader(url: str, timeout: float) -> Tuple[str, int, str]:
    """
    Fetch through the reader proxy (server-side render → clean text).
    Returns (text, status_code, title). Text is plain/markdown, NOT HTML.
    """
    if not READER_PROXY:
        return "", 0, ""
    proxied = READER_PROXY.rstrip("/") + "/" + url
    # NEITHER BROWSER_HEADERS NOR the shared USER_AGENT. This call's target
    # is the reader PROXY (e.g. r.jina.ai), not the page being rescued — and
    # USER_AGENT itself is a Chrome-browser CLAIM (module docstring: "Browser
    # fingerprint"). Live-verified against r.jina.ai (2026-08-11): sending
    # either the full BROWSER_HEADERS set OR just USER_AGENT alone got an
    # instant 403 "Just a moment..." from Cloudflare in front of THE READER
    # ITSELF, while a plain, honestly-non-browser UA succeeded immediately.
    # httpx's TLS handshake doesn't match real Chrome's, so CLAIMING Chrome
    # via headers is what trips the mismatch — there's no page here to
    # impersonate a browser navigating to, so don't claim to be one.
    hdrs = {"User-Agent": "Vera/1.0 (+reader-fallback)", "Accept": "*/*"}
    if READER_KEY:
        hdrs["Authorization"] = f"Bearer {READER_KEY}"
    await throttle_domain(urlparse(READER_PROXY).netloc)
    async with httpx.AsyncClient(timeout=max(timeout, READER_TIMEOUT), headers=hdrs,
                                 follow_redirects=True) as c:
        r = await c.get(proxied)
    text = (r.text or "").strip()
    title = ""
    m = re.search(r"^Title:\s*(.+)$", text, re.M)
    if m:
        title = m.group(1).strip()[:200]
    return text, r.status_code, title


# ─────────────────────────────────────────────────────────────────────────────
# The one fetch
# ─────────────────────────────────────────────────────────────────────────────
async def fetch_page(url: str, *,
                     timeout: float = DEFAULT_TIMEOUT,
                     max_chars: int = MAX_PAGE_CHARS,
                     client: Optional[httpx.AsyncClient] = None,
                     use_api: bool = True,
                     use_reader: bool = True,
                     use_rewrites: bool = True,
                     throttle: bool = True) -> Dict[str, Any]:
    """
    Hardened single-page fetch. See module docstring for the pipeline.
    Never raises — transport failures come back as {"error": ...}.
    """
    url = (url or "").strip()
    out: Dict[str, Any] = {
        "url": url, "final_url": url, "domain": urlparse(url).netloc,
        "status": 0, "html": "", "text": "", "title": "", "chars": 0,
        "blocked": False, "block_reason": "", "via": "direct",
        "via_reader": False, "via_api": "", "elapsed_ms": 0, "error": "",
        "reader_error": "",
    }
    if not url:
        out["error"] = "url required"
        return out
    t0 = time.monotonic()

    def _done() -> Dict[str, Any]:
        out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        out["chars"] = len(out["text"])
        return out

    # 1. Platform API switchover (Reddit OAuth etc.) — cleanest path, try first.
    if use_api and _API_HOOK:
        try:
            api = await _API_HOOK(url)
            if api and api.get("text"):
                pid = str(api.get("provider", "api"))
                out.update({
                    "text":      str(api["text"])[:max_chars],
                    "title":     str(api.get("title", ""))[:200],
                    "final_url": api.get("url", url),
                    "status":    200,
                    "via":       f"api:{pid}",
                    "via_api":   pid,
                })
                return _done()
        except Exception as e:
            log.debug("api hook %s: %s", url[:80], e)

    # 2. Domain rewrite.
    fetch_url = rewrite_url(url) if use_rewrites else url
    fetch_domain = urlparse(fetch_url).netloc

    # 3. Throttle repeats against the same domain.
    if throttle:
        await throttle_domain(fetch_domain)

    # 4. Direct fetch with the browser fingerprint.
    html_body, status, transport_err = "", 0, ""
    own_client = client is None
    c = client or new_session(timeout)
    try:
        r = await c.get(fetch_url)
        html_body, status = r.text, r.status_code
        out["final_url"] = str(r.url)
    except Exception as e:
        transport_err = str(e)
    finally:
        if own_client:
            await c.aclose()

    out["status"] = status
    if html_body:
        out["html"] = html_body
        out["text"] = html_to_text(html_body, max_chars=max_chars)
        out["title"] = extract_title(html_body)

    # 5. Block detection.
    reason = detect_block(html_body, status) if not transport_err else ""
    out["blocked"] = bool(reason)
    out["block_reason"] = reason

    # 6. Reader fallback when blocked, transport-failed, or nearly empty.
    if use_reader and READER_PROXY and (reason or transport_err or len(out["text"]) < 200):
        try:
            rtext, rstatus, rtitle = await _fetch_via_reader(url, timeout)
            if rtext and len(rtext) > len(out["text"]) and not detect_block(rtext, rstatus):
                out.update({
                    "text":         rtext[:max_chars],
                    "title":        out["title"] or rtitle,
                    "html":         "",   # reader output is already plain text
                    "status":       rstatus or status,
                    "blocked":      False,
                    "block_reason": "",
                    "via":          "reader",
                    "via_reader":   True,
                })
                transport_err = ""
            elif rtext:
                # Reader responded but its own output looked blocked/short —
                # worth knowing, distinct from a hard failure below.
                out["reader_error"] = f"reader returned no better than direct fetch (status={rstatus})"
        except Exception as e:
            # This used to be silent at log.debug (invisible at the
            # orchestrator's default INFO level) — the fallback existing
            # specifically to rescue a blocked page failing without a trace
            # is exactly what made the too-tight READER_TIMEOUT above go
            # undiagnosed. Log it where it'll actually be seen, and hand the
            # reason back to the caller instead of a bare blocked=True.
            out["reader_error"] = f"{type(e).__name__}: {e}"
            log.warning("web.fetch reader fallback failed for %s: %s", url[:80], e)

    if transport_err and not out["text"]:
        out["error"] = transport_err
    return _done()
