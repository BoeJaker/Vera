"""
browser_capabilities.py  —  Vera Web Browser Capabilities
===========================================================
Autonomous web navigation and page interaction via Playwright.

Capabilities exposed
─────────────────────
  browser.screenshot   — Capture a full-page PNG screenshot of a URL
  browser.content      — Extract text, links, and metadata from a page
  browser.click        — Click an element by CSS selector and return screenshot
  browser.type         — Type into an input field and return screenshot
  browser.scroll       — Scroll the page and return screenshot
  browser.select       — Select an option from a <select> element
  browser.navigate     — Multi-step autonomous navigation session
  browser.search       — Search the web via a search engine and return results
  browser.extract      — Extract structured data from a page via LLM
  browser.monitor      — Watch a URL for changes (polls at interval)
  browser.pdf          — Convert a URL to PDF (base64)
  browser.health       — Check Playwright / browser availability

Installation
────────────
  pip install playwright
  playwright install chromium

Configuration (env vars)
────────────────────────
  BROWSER_HEADLESS      — "1" (default) or "0" for visible browser
  BROWSER_TIMEOUT_MS    — page load timeout ms (default 30000)
  BROWSER_VIEWPORT_W    — viewport width px (default 1280)
  BROWSER_VIEWPORT_H    — viewport height px (default 900)
  BROWSER_USER_AGENT    — custom UA string (default: realistic Chrome UA)
  BROWSER_MAX_SESSIONS  — max concurrent browser sessions (default 3)
  BROWSER_SCREENSHOT_Q  — screenshot JPEG quality 1-100 (default 85)

Usage from DAG
──────────────
  dag = [
    ["browser.search",  "results",  {"query": "Vera AI framework"}],
    ["browser.content", "page",     {"url": "{{results.urls[0]}}"}],
    ["llm.summarize",   "summary",  {"text": "{{page.text}}"}],
  ]
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse, urlencode, quote_plus

from Vera.Orchestration.capability_orchestration import (
    APP, capability, emit_event, now_iso, ollama_generate,
)

log = logging.getLogger("vera.browser")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

HEADLESS       = os.getenv("BROWSER_HEADLESS",    "1") == "1"
TIMEOUT_MS     = int(os.getenv("BROWSER_TIMEOUT_MS",  "30000"))
VIEWPORT_W     = int(os.getenv("BROWSER_VIEWPORT_W",   "1280"))
VIEWPORT_H     = int(os.getenv("BROWSER_VIEWPORT_H",    "900"))
MAX_SESSIONS   = int(os.getenv("BROWSER_MAX_SESSIONS",    "3"))
SCREENSHOT_Q   = int(os.getenv("BROWSER_SCREENSHOT_Q",   "85"))
USER_AGENT     = os.getenv(
    "BROWSER_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
)

# ─────────────────────────────────────────────────────────────────────────────
# PLAYWRIGHT LIFECYCLE
# Playwright is loaded lazily on first use so the module imports cleanly
# even when playwright is not installed (other caps still work).
# ─────────────────────────────────────────────────────────────────────────────

_playwright_instance = None
_browser_instance    = None
_session_semaphore   = None   # created on first use (needs running event loop)
_pw_lock             = asyncio.Lock()


def _get_semaphore() -> asyncio.Semaphore:
    """Return the session semaphore, creating it lazily on the first call.

    asyncio.Semaphore() requires a running event loop, so it cannot be created
    at module-import time.  This helper is called at the start of every cap
    so the semaphore is always ready by the time it is needed.
    """
    global _session_semaphore
    if _session_semaphore is None:
        _session_semaphore = asyncio.Semaphore(MAX_SESSIONS)
    return _session_semaphore


async def _get_browser():
    """Return a shared persistent Chromium browser instance (lazy init)."""
    global _playwright_instance, _browser_instance

    async with _pw_lock:
        if _browser_instance is not None and _browser_instance.is_connected():
            return _browser_instance

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright not installed — run: pip install playwright && playwright install chromium"
            )

        _playwright_instance = await async_playwright().start()
        _browser_instance    = await _playwright_instance.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
            ],
        )
        log.info("browser: Chromium launched (headless=%s)", HEADLESS)
        return _browser_instance


async def _new_page(browser):
    """Create a new browser context + page with standard settings."""
    ctx  = await browser.new_context(
        viewport      = {"width": VIEWPORT_W, "height": VIEWPORT_H},
        user_agent    = USER_AGENT,
        locale        = "en-GB",
        timezone_id   = "Europe/London",
        java_script_enabled=True,
    )
    # Block known ad/tracker domains to speed up page loads
    await ctx.route(
        re.compile(
            r"(googlesyndication|doubleclick|facebook\.net"
            r"|analytics\.google|hotjar|intercom|mixpanel"
            r"|sentry\.io|cdn\.branch\.io)"
        ),
        lambda route: route.abort(),
    )
    page = await ctx.new_page()
    page.set_default_timeout(TIMEOUT_MS)
    page.set_default_navigation_timeout(TIMEOUT_MS)
    return ctx, page


async def _screenshot_b64(page, full_page: bool = True, quality: int = SCREENSHOT_Q) -> str:
    """Return a base64-encoded JPEG screenshot of the current page."""
    raw = await page.screenshot(
        full_page=full_page,
        type="jpeg",
        quality=quality,
    )
    return base64.b64encode(raw).decode()


def _clean_text(html_or_text: str, max_chars: int = 40_000) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", html_or_text, flags=re.I)
    text = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", text,        flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+",  " ", text)
    text = re.sub(r"\n{3,}",  "\n\n", text)
    return text.strip()[:max_chars]


def _safe_url(url: str) -> str:
    """Ensure url has a scheme."""
    if not url.startswith(("http://", "https://")):
        return "https://" + url
    return url


def _err(msg: str, **kw) -> dict:
    return {"ok": False, "error": msg, **kw}


# ─────────────────────────────────────────────────────────────────────────────
# ██  CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────


@capability(
    "browser.health",
    http_method="GET", http_path="/browser/health", http_tags=["browser"],
    memory="off", silent=True,
    description="Check Playwright and Chromium availability. "
                "Output: {ok, playwright, chromium, headless, viewport, timeout_ms}.",
)
async def browser_health(trace_id=None):
    try:
        from playwright.async_api import async_playwright  # noqa
        playwright_ok = True
    except ImportError:
        playwright_ok = False

    chromium_ok = False
    if playwright_ok:
        try:
            browser = await _get_browser()
            chromium_ok = browser.is_connected()
        except Exception:
            pass

    return {
        "ok":         playwright_ok and chromium_ok,
        "playwright": playwright_ok,
        "chromium":   chromium_ok,
        "headless":   HEADLESS,
        "viewport":   f"{VIEWPORT_W}×{VIEWPORT_H}",
        "timeout_ms": TIMEOUT_MS,
        "max_sessions": MAX_SESSIONS,
    }


@capability(
    "browser.screenshot",
    http_method="POST", http_path="/browser/screenshot", http_tags=["browser", "web"],
    memory="on",
    description="Capture a full-page screenshot of a URL using Playwright. "
                "Input: url (str!), full_page (bool default True), wait_for (str — CSS selector to wait for), "
                "wait_ms (int — extra ms to wait after load). "
                "Output: {image_b64, url, title, ok, load_ms}. "
                "image_b64 is a JPEG encoded as base64.",
)
async def browser_screenshot(
    url:       str,
    full_page: bool = True,
    wait_for:  str  = "",
    wait_ms:   int  = 0,
    trace_id=None,
) -> dict:
    url = _safe_url(url)
    t0  = time.monotonic()
    async with _get_semaphore():
        browser = await _get_browser()
        ctx, page = await _new_page(browser)
        try:
            await page.goto(url, wait_until="domcontentloaded")
            if wait_for:
                try:
                    await page.wait_for_selector(wait_for, timeout=10_000)
                except Exception:
                    pass
            if wait_ms > 0:
                await asyncio.sleep(min(wait_ms / 1000, 10))
            title    = await page.title()
            final_url = page.url
            img_b64  = await _screenshot_b64(page, full_page=full_page)
            load_ms  = round((time.monotonic() - t0) * 1000)
            await emit_event({
                "type":    "browser.screenshot",
                "url":     final_url,
                "title":   title,
                "load_ms": load_ms,
            })
            return {
                "ok":        True,
                "image_b64": img_b64,
                "url":       final_url,
                "title":     title,
                "load_ms":   load_ms,
            }
        except Exception as e:
            log.warning("browser.screenshot [%s]: %s", url, e)
            return _err(str(e), url=url)
        finally:
            await ctx.close()


@capability(
    "browser.content",
    http_method="POST", http_path="/browser/content", http_tags=["browser", "web"],
    memory="on",
    description="Extract text content, links and metadata from a URL using Playwright. "
                "Renders JavaScript before extraction — works on SPAs. "
                "Input: url (str!), include_links (bool default True), max_chars (int default 20000). "
                "Output: {text, title, url, links:[{href,text}], meta, ok, load_ms}.",
)
async def browser_content(
    url:           str,
    include_links: bool = True,
    max_chars:     int  = 20_000,
    trace_id=None,
) -> dict:
    url = _safe_url(url)
    t0  = time.monotonic()
    async with _get_semaphore():
        browser = await _get_browser()
        ctx, page = await _new_page(browser)
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(0.8)   # let lazy JS render
            title     = await page.title()
            final_url = page.url

            # Extract visible text
            body_html = await page.inner_html("body")
            text      = _clean_text(body_html, max_chars)

            # Extract links
            links: list[dict] = []
            if include_links:
                raw_links = await page.eval_on_selector_all(
                    "a[href]",
                    """els => els.map(el => ({
                        href: el.href,
                        text: (el.innerText||el.textContent||'').trim().slice(0,120)
                    })).filter(l => l.href.startsWith('http'))""",
                )
                # Dedup by href, keep max 60
                seen: set[str] = set()
                for lnk in raw_links:
                    if lnk["href"] not in seen and len(links) < 60:
                        seen.add(lnk["href"])
                        links.append(lnk)

            # Extract meta tags (description, og:*)
            meta: dict = await page.evaluate("""() => {
                const r = {};
                document.querySelectorAll('meta[name],meta[property]').forEach(m => {
                    const k = m.name || m.getAttribute('property') || '';
                    const v = m.content || '';
                    if (k && v) r[k] = v;
                });
                return r;
            }""")

            load_ms = round((time.monotonic() - t0) * 1000)
            return {
                "ok":       True,
                "text":     text,
                "title":    title,
                "url":      final_url,
                "links":    links,
                "meta":     {k: v for k, v in (meta or {}).items() if len(k) < 40},
                "load_ms":  load_ms,
                "char_count": len(text),
            }
        except Exception as e:
            log.warning("browser.content [%s]: %s", url, e)
            return _err(str(e), url=url, text="", links=[], meta={})
        finally:
            await ctx.close()


@capability(
    "browser.click",
    http_method="POST", http_path="/browser/click", http_tags=["browser", "web"],
    memory="on",
    description="Navigate to a URL, click an element by CSS selector, and return a screenshot. "
                "Input: url (str!), selector (str! — CSS selector to click), "
                "wait_for (str — selector to wait for after click), wait_ms (int). "
                "Output: {image_b64, url, title, clicked, ok}.",
)
async def browser_click(
    url:      str,
    selector: str,
    wait_for: str = "",
    wait_ms:  int = 500,
    trace_id=None,
) -> dict:
    url = _safe_url(url)
    async with _get_semaphore():
        browser = await _get_browser()
        ctx, page = await _new_page(browser)
        try:
            await page.goto(url, wait_until="domcontentloaded")
            # Locate and click target element
            el = await page.query_selector(selector)
            if not el:
                return _err(f"Selector not found: {selector}", url=url, clicked=False)
            await el.scroll_into_view_if_needed()
            await el.click()
            # Wait for navigation or selector
            if wait_for:
                try:
                    await page.wait_for_selector(wait_for, timeout=10_000)
                except Exception:
                    pass
            elif wait_ms > 0:
                await asyncio.sleep(min(wait_ms / 1000, 5))
            title    = await page.title()
            final_url = page.url
            img_b64  = await _screenshot_b64(page)
            return {
                "ok":        True,
                "image_b64": img_b64,
                "url":       final_url,
                "title":     title,
                "clicked":   selector,
            }
        except Exception as e:
            log.warning("browser.click [%s] %s: %s", url, selector, e)
            return _err(str(e), url=url, clicked=False)
        finally:
            await ctx.close()


@capability(
    "browser.type",
    http_method="POST", http_path="/browser/type", http_tags=["browser", "web"],
    memory="on",
    description="Navigate to a URL, type text into a field, optionally submit, and return a screenshot. "
                "Input: url (str!), selector (str! — CSS selector of input), "
                "text (str! — text to type), submit (bool — press Enter after typing, default False), "
                "clear_first (bool — clear existing value first, default True). "
                "Output: {image_b64, url, title, ok}.",
)
async def browser_type(
    url:         str,
    selector:    str,
    text:        str,
    submit:      bool = False,
    clear_first: bool = True,
    trace_id=None,
) -> dict:
    url = _safe_url(url)
    async with _get_semaphore():
        browser = await _get_browser()
        ctx, page = await _new_page(browser)
        try:
            await page.goto(url, wait_until="domcontentloaded")
            el = await page.query_selector(selector)
            if not el:
                return _err(f"Selector not found: {selector}", url=url)
            await el.scroll_into_view_if_needed()
            await el.click()
            if clear_first:
                await el.select_text()
                await page.keyboard.press("Delete")
            await el.type(text, delay=35)  # human-like typing speed
            if submit:
                await page.keyboard.press("Enter")
                await asyncio.sleep(1.5)
            title    = await page.title()
            final_url = page.url
            img_b64  = await _screenshot_b64(page)
            return {
                "ok":        True,
                "image_b64": img_b64,
                "url":       final_url,
                "title":     title,
                "typed":     text,
                "submitted": submit,
            }
        except Exception as e:
            log.warning("browser.type [%s] %s: %s", url, selector, e)
            return _err(str(e), url=url)
        finally:
            await ctx.close()


@capability(
    "browser.scroll",
    http_method="POST", http_path="/browser/scroll", http_tags=["browser", "web"],
    memory="off",
    description="Navigate to a URL, scroll to a position or element, and return a screenshot. "
                "Input: url (str!), direction (down|up|top|bottom, default down), "
                "amount (int — pixels to scroll, default 600), selector (str — scroll to element). "
                "Output: {image_b64, url, title, ok}.",
)
async def browser_scroll(
    url:       str,
    direction: str = "down",
    amount:    int = 600,
    selector:  str = "",
    trace_id=None,
) -> dict:
    url = _safe_url(url)
    async with _get_semaphore():
        browser = await _get_browser()
        ctx, page = await _new_page(browser)
        try:
            await page.goto(url, wait_until="domcontentloaded")
            if selector:
                el = await page.query_selector(selector)
                if el:
                    await el.scroll_into_view_if_needed()
            elif direction == "top":
                await page.evaluate("window.scrollTo(0,0)")
            elif direction == "bottom":
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            elif direction == "up":
                await page.evaluate(f"window.scrollBy(0, -{amount})")
            else:  # down
                await page.evaluate(f"window.scrollBy(0, {amount})")
            await asyncio.sleep(0.3)
            title    = await page.title()
            img_b64  = await _screenshot_b64(page, full_page=False)
            return {"ok": True, "image_b64": img_b64, "url": page.url, "title": title}
        except Exception as e:
            return _err(str(e), url=url)
        finally:
            await ctx.close()


@capability(
    "browser.select",
    http_method="POST", http_path="/browser/select", http_tags=["browser", "web"],
    memory="on",
    description="Select an option from a <select> element on a page. "
                "Input: url (str!), selector (str! — CSS selector of <select>), "
                "value (str — option value to select), label (str — option text to select). "
                "Output: {image_b64, url, selected, ok}.",
)
async def browser_select(
    url:      str,
    selector: str,
    value:    str = "",
    label:    str = "",
    trace_id=None,
) -> dict:
    url = _safe_url(url)
    async with _get_semaphore():
        browser = await _get_browser()
        ctx, page = await _new_page(browser)
        try:
            await page.goto(url, wait_until="domcontentloaded")
            if value:
                selected = await page.select_option(selector, value=value)
            elif label:
                selected = await page.select_option(selector, label=label)
            else:
                return _err("Provide value or label", url=url)
            await asyncio.sleep(0.4)
            img_b64 = await _screenshot_b64(page)
            return {
                "ok":        True,
                "image_b64": img_b64,
                "url":       page.url,
                "title":     await page.title(),
                "selected":  selected,
            }
        except Exception as e:
            return _err(str(e), url=url)
        finally:
            await ctx.close()


@capability(
    "browser.pdf",
    http_method="POST", http_path="/browser/pdf", http_tags=["browser", "web"],
    memory="on",
    description="Convert a URL to PDF using Playwright (Chromium print-to-PDF). "
                "Input: url (str!), landscape (bool default False), print_background (bool default True). "
                "Output: {pdf_b64, url, title, ok, size_kb}. "
                "pdf_b64 is the PDF file as base64.",
)
async def browser_pdf(
    url:              str,
    landscape:        bool = False,
    print_background: bool = True,
    trace_id=None,
) -> dict:
    url = _safe_url(url)
    async with _get_semaphore():
        browser = await _get_browser()
        ctx, page = await _new_page(browser)
        try:
            await page.goto(url, wait_until="networkidle")
            title  = await page.title()
            raw    = await page.pdf(
                landscape=landscape,
                print_background=print_background,
                format="A4",
            )
            pdf_b64 = base64.b64encode(raw).decode()
            return {
                "ok":      True,
                "pdf_b64": pdf_b64,
                "url":     page.url,
                "title":   title,
                "size_kb": round(len(raw) / 1024, 1),
            }
        except Exception as e:
            return _err(str(e), url=url)
        finally:
            await ctx.close()


@capability(
    "browser.search",
    http_method="POST", http_path="/browser/search", http_tags=["browser", "web", "search"],
    memory="on",
    description="Search the web using DuckDuckGo (no API key required) and return structured results. "
                "Input: query (str!), max_results (int default 8), "
                "include_screenshot (bool default False — screenshot of results page). "
                "Output: {results:[{title,url,snippet}], query, ok, screenshot_b64?}.",
)
async def browser_search(
    query:              str,
    max_results:        int  = 8,
    include_screenshot: bool = False,
    trace_id=None,
) -> dict:
    search_url = f"https://duckduckgo.com/?q={quote_plus(query)}&ia=web"
    async with _get_semaphore():
        browser = await _get_browser()
        ctx, page = await _new_page(browser)
        try:
            await page.goto(search_url, wait_until="domcontentloaded")
            # Wait for result elements
            try:
                await page.wait_for_selector("[data-result='result']", timeout=10_000)
            except Exception:
                try:
                    await page.wait_for_selector("article", timeout=5_000)
                except Exception:
                    pass

            results: list[dict] = await page.evaluate(f"""() => {{
                const items = [];
                // DuckDuckGo result selectors (multiple layouts)
                const selectors = [
                    '[data-result="result"]',
                    'article[data-testid]',
                    '.result',
                ];
                let els = [];
                for (const sel of selectors) {{
                    els = [...document.querySelectorAll(sel)];
                    if (els.length > 0) break;
                }}
                for (const el of els.slice(0, {max_results})) {{
                    const a     = el.querySelector('a[href]');
                    const href  = a ? a.href : '';
                    const title = a ? (a.innerText||a.textContent||'').trim() : '';
                    const snipEl = el.querySelector('[class*="snippet"], [class*="result__snippet"], span');
                    const snippet = snipEl ? (snipEl.innerText||snipEl.textContent||'').trim().slice(0,300) : '';
                    if (href && href.startsWith('http') && !href.includes('duckduckgo.com'))
                        items.push({{ title, url: href, snippet }});
                }}
                return items;
            }}""")

            out: dict = {"ok": True, "query": query, "results": results}
            if include_screenshot:
                out["screenshot_b64"] = await _screenshot_b64(page, full_page=False)

            await emit_event({
                "type":    "browser.search",
                "query":   query,
                "results": len(results),
            })
            return out

        except Exception as e:
            log.warning("browser.search [%s]: %s", query, e)
            return _err(str(e), query=query, results=[])
        finally:
            await ctx.close()


@capability(
    "browser.extract",
    http_method="POST", http_path="/browser/extract", http_tags=["browser", "web", "llm"],
    memory="on",
    description="Load a URL, extract visible text, then use an LLM to extract structured data per a schema. "
                "Input: url (str!), schema (str! — JSON schema or description of what to extract, e.g. "
                "'{ name, price, description }'), "
                "prefer_gpu (bool default True). "
                "Output: {data (extracted fields), raw_text_chars, url, title, ok}.",
)
async def browser_extract(
    url:        str,
    schema:     str,
    prefer_gpu: bool = True,
    trace_id=None,
) -> dict:
    # Step 1: fetch page content
    content_result = await browser_content(url=url, include_links=False, max_chars=15_000)
    if not content_result.get("ok"):
        return _err(content_result.get("error", "content fetch failed"), url=url)

    text  = content_result["text"]
    title = content_result["title"]

    # Step 2: ask LLM to extract data
    system = (
        "You are a precise web data extractor. "
        "Extract ONLY the fields specified in the schema from the provided page text. "
        "Return ONLY a valid JSON object with the extracted fields. "
        "If a field is not present, use null. "
        "Do not add commentary or explanation."
    )
    prompt = (
        f"Extract the following schema from this page:\n\nSchema: {schema}\n\n"
        f"Page title: {title}\n\nPage text:\n{text[:12_000]}"
    )

    raw = await ollama_generate(prompt, system=system, json_mode=True, prefer_gpu=prefer_gpu)
    try:
        data = json.loads(raw)
    except Exception:
        # Try to extract JSON from LLM output
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = {"raw_llm_output": raw, "parse_error": True}
        else:
            data = {"raw_llm_output": raw, "parse_error": True}

    return {
        "ok":             True,
        "data":           data,
        "url":            url,
        "title":          title,
        "raw_text_chars": len(text),
    }


@capability(
    "browser.navigate",
    http_method="POST", http_path="/browser/navigate", http_tags=["browser", "web"],
    memory="on",
    description="Autonomous multi-step browser session driven by natural language instructions. "
                "The LLM plans each step (goto, click, type, scroll, extract) and executes it, "
                "returning a screenshot and state after each action. "
                "Input: goal (str! — what to accomplish, e.g. 'find the price of RTX 4090 on scan.co.uk'), "
                "start_url (str — starting URL, default 'https://www.google.com'), "
                "max_steps (int default 8), prefer_gpu (bool default True). "
                "Output: {ok, steps:[{action,screenshot_b64,url,note}], "
                "final_url, result (extracted final answer), total_steps}.",
)
async def browser_navigate(
    goal:       str,
    start_url:  str  = "https://www.google.com",
    max_steps:  int  = 8,
    prefer_gpu: bool = True,
    trace_id=None,
) -> dict:
    start_url  = _safe_url(start_url)
    max_steps  = min(max(1, max_steps), 15)   # hard cap
    steps_done: list[dict] = []
    current_url = start_url

    SYSTEM = (
        "You are a browser automation agent. At each step you are given: "
        "the goal, the current URL, the current page title, and a list of "
        "visible links and interactive elements. You must choose ONE action "
        "and respond ONLY with a JSON object in this exact format:\n"
        '{"action":"goto|click|type|scroll|extract|done",'
        '"selector":"CSS selector if click/type/scroll",'
        '"text":"text to type if action=type",'
        '"url":"URL if action=goto",'
        '"direction":"down|up|top|bottom if action=scroll",'
        '"note":"brief explanation of this step"}\n\n'
        "Actions:\n"
        "  goto     — navigate to a URL (provide url field)\n"
        "  click    — click an element (provide selector field)\n"
        "  type     — type into a field then press Enter (provide selector + text)\n"
        "  scroll   — scroll the page (provide direction)\n"
        "  extract  — you have enough info, extract the answer (use done instead)\n"
        "  done     — goal achieved, provide final answer in note field\n\n"
        "IMPORTANT: Use selectors that are robust (prefer id, name, role attributes). "
        "Keep steps minimal. If the goal is a question, answer it in the done note."
    )

    async with _get_semaphore():
        browser  = await _get_browser()
        ctx, page = await _new_page(browser)

        try:
            await page.goto(start_url, wait_until="domcontentloaded")
            current_url = page.url

            for step_i in range(max_steps):
                # Gather page state for LLM
                title = await page.title()
                # Extract a compact representation: links + inputs
                page_summary: str = await page.evaluate("""() => {
                    const parts = [];
                    // Inputs
                    document.querySelectorAll('input,textarea,select').forEach((el,i) => {
                        const t = el.type || el.tagName.toLowerCase();
                        const n = el.name || el.id || el.placeholder || '';
                        if (n) parts.push(`INPUT[${t}] name="${n}" id="${el.id||''}" placeholder="${el.placeholder||''}"`);
                    });
                    // Buttons
                    document.querySelectorAll('button,[role=button],input[type=submit]').forEach(el => {
                        const t = (el.innerText||el.value||el.textContent||'').trim().slice(0,60);
                        if (t) parts.push(`BUTTON "${t}" selector="${el.id?'#'+el.id:el.className.split(' ')[0]||'button'}"`);
                    });
                    // Top links
                    const links = [...document.querySelectorAll('a[href]')]
                        .filter(a => a.href.startsWith('http') && (a.innerText||'').trim().length > 1)
                        .slice(0, 20)
                        .map(a => `LINK "${(a.innerText||'').trim().slice(0,60)}" href="${a.href}"`);
                    parts.push(...links);
                    // Visible text snippet
                    const body = document.body;
                    const visible = (body.innerText||'').trim().slice(0, 1500);
                    return JSON.stringify({elements: parts.slice(0,30), text_snippet: visible});
                }""")

                try:
                    ps = json.loads(page_summary)
                except Exception:
                    ps = {"elements": [], "text_snippet": ""}

                prompt = (
                    f"Goal: {goal}\n"
                    f"Step: {step_i + 1}/{max_steps}\n"
                    f"Current URL: {current_url}\n"
                    f"Page title: {title}\n\n"
                    f"Page elements:\n" + "\n".join(ps.get("elements", [])) + "\n\n"
                    f"Page text (snippet):\n{ps.get('text_snippet','')[:800]}\n\n"
                    "Respond with the next action JSON."
                )

                raw = await ollama_generate(
                    prompt, system=SYSTEM, json_mode=True, prefer_gpu=prefer_gpu
                )

                # Parse LLM action
                action_obj: dict = {}
                try:
                    action_obj = json.loads(raw)
                except Exception:
                    m = re.search(r"\{[\s\S]*?\}", raw)
                    if m:
                        try:
                            action_obj = json.loads(m.group(0))
                        except Exception:
                            pass

                action   = action_obj.get("action", "done")
                selector = action_obj.get("selector", "")
                text_val = action_obj.get("text", "")
                goto_url = action_obj.get("url", "")
                note     = action_obj.get("note", "")
                direction= action_obj.get("direction", "down")

                await emit_event({
                    "type":   "browser.navigate.step",
                    "step":   step_i + 1,
                    "action": action,
                    "url":    current_url,
                    "note":   note,
                })

                step_record: dict = {
                    "step":   step_i + 1,
                    "action": action,
                    "url":    current_url,
                    "note":   note,
                }

                if action == "done":
                    img_b64 = await _screenshot_b64(page, full_page=False)
                    step_record["screenshot_b64"] = img_b64
                    steps_done.append(step_record)
                    break

                elif action == "goto" and goto_url:
                    try:
                        await page.goto(_safe_url(goto_url), wait_until="domcontentloaded")
                        current_url = page.url
                    except Exception as e:
                        step_record["error"] = str(e)

                elif action == "click" and selector:
                    try:
                        el = await page.query_selector(selector)
                        if el:
                            await el.scroll_into_view_if_needed()
                            await el.click()
                            await asyncio.sleep(1.2)
                            current_url = page.url
                        else:
                            step_record["error"] = f"selector not found: {selector}"
                    except Exception as e:
                        step_record["error"] = str(e)

                elif action == "type" and selector:
                    try:
                        el = await page.query_selector(selector)
                        if el:
                            await el.click()
                            await el.select_text()
                            await page.keyboard.press("Delete")
                            await el.type(text_val, delay=30)
                            await page.keyboard.press("Enter")
                            await asyncio.sleep(1.5)
                            current_url = page.url
                        else:
                            step_record["error"] = f"selector not found: {selector}"
                    except Exception as e:
                        step_record["error"] = str(e)

                elif action == "scroll":
                    try:
                        if direction == "top":
                            await page.evaluate("window.scrollTo(0,0)")
                        elif direction == "bottom":
                            await page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
                        elif direction == "up":
                            await page.evaluate("window.scrollBy(0,-600)")
                        else:
                            await page.evaluate("window.scrollBy(0,600)")
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        step_record["error"] = str(e)

                # Take screenshot of current state (viewport only for speed)
                try:
                    img_b64 = await _screenshot_b64(page, full_page=False)
                    step_record["screenshot_b64"] = img_b64
                except Exception:
                    pass

                step_record["url"] = page.url
                steps_done.append(step_record)

            # Build final answer from last "done" note or extract page text
            final_note = next(
                (s["note"] for s in reversed(steps_done) if s.get("action") == "done"),
                "",
            )
            if not final_note:
                # Extract page text as fallback answer
                body_html = await page.inner_html("body")
                final_note = _clean_text(body_html, 3000)

            return {
                "ok":          True,
                "goal":        goal,
                "steps":       [{k: v for k, v in s.items() if k != "screenshot_b64"} for s in steps_done],
                "screenshots": [s.get("screenshot_b64", "") for s in steps_done],
                "final_url":   page.url,
                "result":      final_note,
                "total_steps": len(steps_done),
            }

        except Exception as e:
            log.warning("browser.navigate [%s]: %s", goal, e)
            return _err(str(e), goal=goal, steps=steps_done, result="")
        finally:
            await ctx.close()


@capability(
    "browser.monitor",
    http_method="POST", http_path="/browser/monitor", http_tags=["browser", "web"],
    memory="on",
    description="Monitor a URL for changes by comparing page content hashes. "
                "Takes an initial snapshot and a comparison snapshot after interval_ms. "
                "Input: url (str!), interval_ms (int default 5000 — ms between checks), "
                "selector (str — CSS selector to watch, default whole body), "
                "checks (int default 1 — number of additional checks). "
                "Output: {changed, hash_before, hash_after, diff_chars, screenshot_b64, ok}.",
)
async def browser_monitor(
    url:         str,
    interval_ms: int = 5_000,
    selector:    str = "body",
    checks:      int = 1,
    trace_id=None,
) -> dict:
    url = _safe_url(url)
    interval_ms = min(max(500, interval_ms), 60_000)
    checks      = min(max(1, checks), 10)

    async with _get_semaphore():
        browser = await _get_browser()
        ctx, page = await _new_page(browser)
        try:
            await page.goto(url, wait_until="domcontentloaded")

            async def _get_hash():
                try:
                    el = await page.query_selector(selector)
                    txt = await el.inner_text() if el else await page.inner_text("body")
                    return hashlib.md5(txt.encode()).hexdigest(), txt
                except Exception:
                    return "", ""

            hash_before, text_before = await _get_hash()
            hash_after = hash_before
            text_after = text_before

            for _ in range(checks):
                await asyncio.sleep(interval_ms / 1000)
                await page.reload(wait_until="domcontentloaded")
                hash_after, text_after = await _get_hash()
                if hash_after != hash_before:
                    break

            changed  = hash_after != hash_before
            img_b64  = await _screenshot_b64(page, full_page=False)
            diff_chars = abs(len(text_after) - len(text_before))

            await emit_event({
                "type":    "browser.monitor",
                "url":     url,
                "changed": changed,
            })

            return {
                "ok":            True,
                "changed":       changed,
                "url":           url,
                "hash_before":   hash_before,
                "hash_after":    hash_after,
                "diff_chars":    diff_chars,
                "screenshot_b64": img_b64,
            }
        except Exception as e:
            return _err(str(e), url=url, changed=False)
        finally:
            await ctx.close()