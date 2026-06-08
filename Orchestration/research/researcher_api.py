"""
researcher_api.py  —  Vera Research Agent v4
─────────────────────────────────────────────
New in v4:
  • Full database persistence via db.py
    - SQLite by default (vera_research.db)
    - PostgreSQL via VERA_DB_URL env var
  • All jobs, citations, projects, rounds, sources, config saved on completion
  • /api/history now reads from DB (survives restarts)
  • /api/db/stats  — live DB statistics
  • /api/db/search — full-text search across all saved research
  • Sources & instance config loaded from DB on startup

Run:
    python researcher_api.py
    python -m Vera.ChatUI.researcher_api
    uvicorn Vera.ChatUI.researcher_api:app --host 0.0.0.0 --port 8765 --reload

DB config:
    VERA_DB_URL=postgresql+asyncpg://user:pass@host/vera  (optional — SQLite default)
    VERA_SQLITE_PATH=./vera_research.db                   (optional)
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import AsyncIterator, Optional
from urllib.parse import urlparse, urljoin

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Persistence through the data fabric (replaces research_db.py)
try:
    from Vera.Orchestration.research.research_fabric import DB
except ImportError:
    from research_fabric import DB

# Orchestrator primitives — available when loaded as a capability module.
# When running standalone (legacy), these are no-ops.
try:
    from Vera.Orchestration.capability_orchestration import (
        APP as _VERA_APP, capability, emit_event as _vera_emit,
        now_iso, register_ui, schedule, CAPABILITY_REGISTRY,
    )
    _VERA_MODE = True
except ImportError:
    try:
        from capability_orchestration import (
            APP as _VERA_APP, capability, emit_event as _vera_emit,
            now_iso, register_ui, schedule, CAPABILITY_REGISTRY,
        )
        _VERA_MODE = True
    except ImportError:
        _VERA_MODE = False
        _VERA_APP = None
        def capability(*a, **kw):
            def _d(fn): return fn
            return _d
        async def _vera_emit(e): pass
        def register_ui(*a, **kw): pass
        def schedule(*a, **kw): pass
        CAPABILITY_REGISTRY = {}

log = logging.getLogger("vera.researcher")
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

SCREENSHOT_DIR = Path("screenshots")
PROJECTS_DIR   = Path("projects")
SCREENSHOT_DIR.mkdir(exist_ok=True)
PROJECTS_DIR.mkdir(exist_ok=True)

# ══════════════════════════════════════════════════════════════════════════════
#  Enums
# ══════════════════════════════════════════════════════════════════════════════

class ModelTier(str, Enum):
    THINKER = "thinker"
    WRITER  = "writer"
    ANALYST = "analyst"
    AUTO    = "auto"

class AgentMode(str, Enum):
    SINGLE   = "single"
    PARALLEL = "parallel"
    DEEP     = "deep"

class SourceType(str, Enum):
    WEB_SEARCH  = "web_search"
    WEB_CRAWL   = "web_crawl"
    WEB_ARCHIVE = "web_archive"
    NEO4J       = "neo4j"
    CHROMA      = "chroma"
    GITHUB      = "github"
    NEWS        = "news"
    REDIS       = "redis"
    DATABASE    = "database"
    CUSTOM      = "custom"
    FABRIC      = "fabric"        # Vera data fabric (SQLite + FAISS + Chroma + PG)
    MEMORY      = "memory"        # Vera memory graph (session history, cap traces)

class JobStatus(str, Enum):
    QUEUED    = "queued"
    THINKING  = "thinking"
    SEARCHING = "searching"
    CRAWLING  = "crawling"
    ARCHITECTING = "architecting"
    CODING    = "coding"
    REVIEWING = "reviewing"
    WRITING   = "writing"
    VERIFYING = "verifying"
    CHAINING  = "chaining"    # waiting for next chain run
    DONE      = "done"
    ERROR     = "error"
    CANCELLED = "cancelled"

class OutputMode(str, Enum):
    REPORT    = "report"      # normal markdown report
    GUIDE     = "guide"       # multi-section long-form guide
    FILESTORE = "filestore"   # produces a full file-tree + content
    CODE      = "code"        # research → architect → implement → review → chain

# ══════════════════════════════════════════════════════════════════════════════
#  Dataclasses
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class OllamaInstance:
    name: str
    host: str
    port: int
    tier: ModelTier
    model: str
    ctx_size: int = 8192
    enabled: bool = True
    # Thinking-model support — set True for qwen3/deepseek-r1 etc.
    # When True: stream_ollama emits {"type":"thinking"} tokens separately
    # and never mixes them into the result buffer.
    enable_thinking: bool = False
    thinking_timeout: float = 0.0   # 0 = auto (8× base timeout)

    @property
    def base_url(self):  return f"http://{self.host}:{self.port}"
    @property
    def generate_url(self): return f"{self.base_url}/api/generate"
    @property
    def tags_url(self):  return f"{self.base_url}/api/tags"


@dataclass
class DataSource:
    id: str
    label: str
    type: SourceType
    enabled: bool
    config: dict = field(default_factory=dict)
    status: str = "unknown"


@dataclass
class WebSearchConfig:
    """Configurable web search behaviour."""
    engine:       str   = "searxng"    # searxng | brave | ddg
    result_count: int   = 8
    crawl_depth:  int   = 1            # 0=no crawl, 1=linked pages, 2=deep
    crawl_breadth:int   = 3            # pages per crawled link
    crawl_timeout:float = 8.0
    include_archive: bool = False
    safe_search:  int   = 0


@dataclass
class Citation:
    id: str
    url: str
    title: str
    snippet: str
    source_type: str
    screenshot_path: str = ""
    domain: str = ""
    full_text: str = ""          # populated by deep crawl
    fetched_at: float = field(default_factory=time.time)
    rank_score: float = 0.0      # 0-1 composite rank (relevance × authority × freshness)
    tags: list = field(default_factory=list)   # e.g. ["authoritative","structured","news","image"]
    image_urls: list = field(default_factory=list)  # images found on/for this page

    def __post_init__(self):
        if self.url and not self.domain:
            try: self.domain = urlparse(self.url).netloc
            except Exception: pass

    def to_dict(self, include_full_text: bool = False):
        d = asdict(self)
        d["screenshot_url"] = f"/screenshots/{self.screenshot_path}" if self.screenshot_path else ""
        if not include_full_text:
            d.pop("full_text", None)  # strip for WS to keep messages small
        # Ensure list fields serialise cleanly
        d.setdefault("tags", [])
        d.setdefault("image_urls", [])
        d.setdefault("rank_score", 0.0)
        return d

    def to_dict_full(self):
        """Full serialisation including crawled text — for DB storage."""
        return self.to_dict(include_full_text=True)


@dataclass
class ProjectRound:
    id: str
    job_id: str
    round_num: int
    query: str
    result: str
    citations: list[dict]
    created_at: float = field(default_factory=time.time)


@dataclass
class Project:
    id: str
    name: str
    description: str
    output_mode: OutputMode
    rounds: list[ProjectRound] = field(default_factory=list)
    context_summary: str = ""    # rolling summary updated after each round
    file_tree: dict = field(default_factory=dict)  # path → content for filestore
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "output_mode": self.output_mode, "round_count": len(self.rounds),
            "context_summary": self.context_summary[:400],
            "file_count": len(self.file_tree),
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


@dataclass
class AgentSlot:
    id: str
    tier: ModelTier
    job_id: Optional[str] = None
    status: str = "idle"
    model: str = ""
    tokens: int = 0
    started_at: Optional[float] = None


@dataclass
class ChainContext:
    """
    Passed between chained coding runs so each run knows exactly what
    has been built and what remains — without needing the full prior
    output in its context window.
    """
    chain_id:       str                       # shared across all runs in a chain
    run_number:     int                       # 1-based
    original_task:  str                       # the original user request, never changes
    architecture:   str  = ""                # full arch plan (set in run 1, carried forward)
    files_planned:  list[str] = field(default_factory=list)   # every file in the plan
    files_done:     list[str] = field(default_factory=list)   # files completed so far
    files_pending:  list[str] = field(default_factory=list)   # files still to write
    continuity_summary: str = ""             # thinker's rolling "state of play" summary
    accumulated_code: dict[str, str] = field(default_factory=dict)  # path → content so far
    research_context: str = ""               # source/research findings carried forward
    is_complete:    bool = False             # set true when files_pending is empty


@dataclass
class ResearchJob:
    id: str
    query: str
    mode: AgentMode
    output_mode: OutputMode
    sources: list[str]
    status: JobStatus
    created_at: float
    project_id: Optional[str] = None
    finished_at: Optional[float] = None
    result: Optional[str] = None
    error: Optional[str] = None
    steps: list[dict] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    token_count: int = 0
    file_tree: dict = field(default_factory=dict)   # path → content
    # Iterative context — passed from prior research run
    prior_context: str = ""        # text of previous result
    context_mode:  str = "fresh"   # "fresh" | "continue"
    # Chain fields (only set for CODE mode)
    chain_ctx: Optional[ChainContext] = None
    chain_continues: bool = False   # True when more runs needed
    # Pipeline stage overrides (set when run as part of a pipeline stage)
    pipeline_nlp_tools: Optional[list] = None     # forces specific NLP tools
    pipeline_writer_prompt: str = ""              # extra writer system instruction

# ══════════════════════════════════════════════════════════════════════════════
#  Defaults
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_INSTANCES: list[OllamaInstance] = [
    OllamaInstance("Thinker", "192.168.0.247", 11435, ModelTier.THINKER, "qwen3.5:9b", 131072),
    OllamaInstance("Writer",  "192.168.0.250", 11435, ModelTier.WRITER,  "qwen3.5:9b",  32768),
    OllamaInstance("Analyst", "192.168.0.246", 11436, ModelTier.ANALYST, "qwen3.5:9b",           32768, enabled=True),
]

DEFAULT_SOURCES: list[DataSource] = [
    DataSource("searxng",     "SearXNG",         SourceType.WEB_SEARCH,  True,  {"host":"http://llm.int:8888"}),
    DataSource("brave",       "Brave Search",    SourceType.WEB_SEARCH,  False, {"api_key":""}),
    DataSource("crawl4ai",    "Web Crawl",       SourceType.WEB_CRAWL,   True,  {}),
    DataSource("commoncrawl", "Common Crawl",    SourceType.WEB_ARCHIVE, False, {}),
    DataSource("wayback",     "Wayback Machine", SourceType.WEB_ARCHIVE, True,  {}),
    DataSource("neo4j",       "Neo4j Graph",     SourceType.NEO4J,       True,  {"uri":"bolt://llm.int:7687","user":"neo4j","password":""}),
    DataSource("chroma",      "ChromaDB",        SourceType.CHROMA,      True,  {"host":"llm.int","port":8000}),
    DataSource("github",      "GitHub",          SourceType.GITHUB,      False, {"token":""}),
    DataSource("hackernews",  "Hacker News",     SourceType.NEWS,        True,  {}),
    DataSource("arxiv",       "arXiv",           SourceType.NEWS,        True,  {}),
    DataSource("redis",       "Redis",           SourceType.REDIS,       False, {"host":"llm.int","port":6379,"password":"","db":0,"prefix":"vera:"}),
    DataSource("fabric",      "Data Fabric",     SourceType.FABRIC,      True,  {"top_k": 30}),
    DataSource("memory",      "Memory Graph",    SourceType.MEMORY,      True,  {"top_k": 20}),
]

# ══════════════════════════════════════════════════════════════════════════════
#  Global state
# ══════════════════════════════════════════════════════════════════════════════

instances:     list[OllamaInstance]       = list(DEFAULT_INSTANCES)
sources:       list[DataSource]           = list(DEFAULT_SOURCES)
web_cfg:       WebSearchConfig            = WebSearchConfig()
jobs:          dict[str, ResearchJob]     = {}
history:       list[ResearchJob]          = []
projects:      dict[str, Project]         = {}
ws_clients:    dict[str, list[WebSocket]] = {}
cancel_flags:  dict[str, bool]            = {}

agent_slots: list[AgentSlot] = [
    AgentSlot("slot-thinker", ModelTier.THINKER),
    AgentSlot("slot-writer",  ModelTier.WRITER),
    AgentSlot("slot-analyst", ModelTier.ANALYST),
]

# ══════════════════════════════════════════════════════════════════════════════
#  Screenshot
# ══════════════════════════════════════════════════════════════════════════════
# ── Playwright — import once at module level ──────────────────────────────────
# Never import inside _get_browser: concurrent coroutines calling it simultaneously
# exhaust Python's recursion limit during the first-time module import.
_async_playwright = None
_playwright_available = False

try:
    from playwright.async_api import async_playwright as _async_playwright
    _playwright_available = True
    log.info("playwright imported OK")
except Exception as e:
    log.warning("playwright import failed — screenshots will use image extraction / SVG: %s", e)

_pw_browser = None
_pw_lock = asyncio.Lock()
_screenshot_sem = asyncio.Semaphore(2)


async def _get_browser():
    global _pw_browser

    if not _playwright_available:
        raise RuntimeError("playwright not available (import failed at startup)")

    async with _pw_lock:
        if _pw_browser is not None:
            try:
                if not _pw_browser.is_connected():
                    raise RuntimeError("browser disconnected")
                return _pw_browser
            except Exception as e:
                log.warning("Browser dead, relaunching: %s", e)
                try:
                    await _pw_browser.close()
                except Exception:
                    pass
                _pw_browser = None

        try:
            log.info("Launching Playwright Chromium…")
            pw = await _async_playwright().start()
            _pw_browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            log.info("✓ Playwright browser ready (connected=%s)", _pw_browser.is_connected())
            return _pw_browser
        except Exception as e:
            log.error("Failed to launch Playwright browser: %s", e, exc_info=True)
            raise

async def capture_screenshot(url: str) -> str:
    """
    Capture a screenshot of *url*, trying three methods in order:
        1. Playwright headless Chromium
        2. OG/twitter meta image extraction
        3. SVG placeholder (last resort)

    Results are cached by URL hash. Stale SVG placeholders are deleted on
    each call so real screenshots can replace them on retry.
    Always returns a filename — never raises.
    """
    key = hashlib.md5(url.encode()).hexdigest()[:16]
    png_path = SCREENSHOT_DIR / f"{key}.png"
    svg_path = SCREENSHOT_DIR / f"{key}.svg"

    if png_path.exists():
        log.debug("Screenshot cache hit: %s → %s", url[:60], png_path.name)
        return png_path.name

    # Delete stale SVG so we always retry real capture
    if svg_path.exists():
        svg_path.unlink(missing_ok=True)
        log.debug("Removed stale SVG, retrying capture for: %s", url[:60])

    page_title = url[:70]

    # ── 1. Playwright (semaphore-limited to 2 concurrent pages) ──────────────
    async with _screenshot_sem:
        try:
            browser = await _get_browser()
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                extra_http_headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-GB,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br",
                    "DNT": "1",
                    "Upgrade-Insecure-Requests": "1",
                },
                java_script_enabled=True,
                ignore_https_errors=True,
            )
            page = await context.new_page()
            try:
                target_url = url if url.startswith(("http://", "https://")) else f"https://{url}"
                log.debug("Playwright navigating: %s", target_url[:80])
                await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass  # networkidle timeout is fine — page is rendered enough
                await page.screenshot(path=str(png_path), full_page=False, type="png")
                log.info("✓ Screenshot (playwright): %s → %s", url[:60], png_path.name)
                return png_path.name
            except Exception as e:
                log.warning("Playwright page error for %s: %s", url[:60], e)
                try:
                    page_title = (await page.title())[:70] or page_title
                except Exception:
                    pass
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
                try:
                    await context.close()
                except Exception:
                    pass
        except Exception as e:
            log.error("Playwright browser error for %s: %s", url[:60], e)
            # Only kill the shared browser for genuine browser-level failures
            global _pw_browser
            if not _pw_browser or not _pw_browser.is_connected():
                _pw_browser = None

    # ── 2. Meta image extraction ──────────────────────────────────────────────
    try:
        log.debug("Trying image extraction for: %s", url[:60])
        async with httpx.AsyncClient(
            timeout=10.0, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Vera/1.0)"},
        ) as c:
            r = await c.get(url)
            html = r.text[:80000]

        tm = re.search(r"<title[^>]*>([^<]{0,120})</title>", html, re.I)
        if tm:
            page_title = tm.group(1).strip()[:70]

        image_url = None
        for pat in [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\'>\s]+)["\']',
            r'<meta[^>]+content=["\']([^"\'>\s]+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\'>\s]+)["\']',
            r'<meta[^>]+content=["\']([^"\'>\s]+)["\'][^>]+name=["\']twitter:image["\']',
            r'<meta[^>]+name=["\']thumbnail["\'][^>]+content=["\']([^"\'>\s]+)["\']',
        ]:
            m = re.search(pat, html, re.I)
            if m:
                image_url = m.group(1).strip()
                log.debug("Found meta image: %s", image_url[:80])
                break

        if not image_url:
            for m in re.finditer(r'<img[^>]+src=["\']([^"\'>\s]+)["\'][^>]*>', html, re.I):
                src = m.group(1).strip()
                if src.startswith("data:"):
                    continue
                w = re.search(r'width=["\'](\d+)["\']', m.group(0), re.I)
                h = re.search(r'height=["\'](\d+)["\']', m.group(0), re.I)
                if w and int(w.group(1)) < 100:
                    continue
                if h and int(h.group(1)) < 100:
                    continue
                image_url = src
                log.debug("Found <img>: %s", image_url[:80])
                break

        if image_url:
            image_url = image_url.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            if image_url.startswith("//"):
                image_url = "https:" + image_url
            elif image_url.startswith("/"):
                p = urlparse(url)
                image_url = f"{p.scheme}://{p.netloc}{image_url}"
            elif not image_url.startswith(("http://", "https://")):
                image_url = urljoin(url, image_url)

        if image_url and image_url.startswith(("http://", "https://")):
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as c:
                img_resp = await c.get(image_url, headers={"User-Agent": "Mozilla/5.0"})
                ct = img_resp.headers.get("content-type", "").lower()
                if (any(ct.startswith(f"image/{t}") for t in
                        ("jpeg", "jpg", "png", "webp", "gif", "avif"))
                        and len(img_resp.content) > 5000):
                    png_path.write_bytes(img_resp.content)
                    log.info("✓ Screenshot (meta image): %s → %s", url[:60], png_path.name)
                    return png_path.name
                else:
                    log.debug("Meta image rejected: ct=%s size=%d", ct, len(img_resp.content))

    except Exception as e:
        log.warning("Image extraction failed for %s: %s", url[:60], e)

    # ── 3. SVG placeholder ────────────────────────────────────────────────────
    log.warning("All capture methods failed for %s — writing SVG placeholder", url[:60])
    domain = urlparse(url).netloc or url[:40]
    title_display = page_title[:60] + ("…" if len(page_title) > 60 else "")

    def ex(s: str) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    svg_path.write_text(f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="380">
  <rect width="640" height="380" fill="#e8e5df"/>
  <rect width="640" height="34" fill="#dddad3"/>
  <circle cx="16" cy="17" r="5" fill="#c03030" opacity=".55"/>
  <circle cx="29" cy="17" r="5" fill="#c47020" opacity=".55"/>
  <circle cx="42" cy="17" r="5" fill="#228060" opacity=".55"/>
  <rect x="55" y="9" width="530" height="16" rx="8" fill="#f0ede8"/>
  <text x="320" y="20" font-family="monospace" font-size="10" fill="#a8a69e" text-anchor="middle">{ex(domain)}</text>
  <rect x="40" y="54" width="560" height="260" rx="5" fill="#f0ede8"/>
  <text x="320" y="170" font-family="monospace" font-size="14" fill="#1a1a18" text-anchor="middle" font-weight="bold">{ex(title_display)}</text>
  <text x="320" y="195" font-family="monospace" font-size="10" fill="#a8a69e" text-anchor="middle">{ex(domain)}</text>
  <text x="320" y="350" font-family="monospace" font-size="9" fill="#c8c4bc" text-anchor="middle">{ex(url[:90])}</text>
</svg>""", encoding="utf-8")
    return svg_path.name


async def _safe_screenshot(url: str) -> str:
    """
    Wrapper used by gather_web_search. asyncio.gather with return_exceptions=True
    means any unhandled exception becomes an exception object in the results list,
    silently bypassing the isinstance(shot, str) check and leaving screenshot_path
    empty. This wrapper guarantees a string return and logs any unexpected failure.
    """
    try:
        return await capture_screenshot(url)
    except Exception as e:
        log.error("Unexpected screenshot error for %s: %s", url[:60], e, exc_info=True)
        return ""
    
# ══════════════════════════════════════════════════════════════════════════════
#  Deep Crawl
# ══════════════════════════════════════════════════════════════════════════════

def extract_links(html: str, base_url: str) -> list[str]:
    """Extract absolute href links from HTML."""
    links = re.findall(r'href=["\']([^"\']+)["\']', html)
    base = urlparse(base_url)
    out = []
    for l in links:
        if l.startswith("javascript:"): continue
        if l.startswith("#"): continue
        abs_url = urljoin(base_url, l)
        p = urlparse(abs_url)
        # stay on same domain
        if p.netloc == base.netloc and p.scheme in ("http","https"):
            out.append(abs_url)
    return list(dict.fromkeys(out))  # dedupe, preserve order


def _md_to_html(md: str) -> str:
    """
    Lightweight markdown to HTML converter for the document format endpoint.
    Handles headings, bold/italic, code fences, tables, lists, blockquotes.
    """
    lines  = md.replace("\r\n", "\n").split("\n")
    out    = []
    in_code  = False
    in_list  = False
    in_olist = False
    in_table = False

    def _inline(s: str) -> str:
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"__(.+?)__",     r"<strong>\1</strong>", s)
        s = re.sub(r"\*(.+?)\*",   r"<em>\1</em>", s)
        s = re.sub(r"`([^`]+)`",     r"<code>\1</code>", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
        s = re.sub(r"\[(\d+)\]",  r'<sup class="cref">[\1]</sup>', s)
        return s

    def _close_list():
        nonlocal in_list, in_olist
        if in_list:  out.append("</ul>");  in_list  = False
        if in_olist: out.append("</ol>"); in_olist = False

    def _close_table():
        nonlocal in_table
        if in_table: out.append("</tbody></table>"); in_table = False

    for line in lines:
        if line.startswith("```"):
            if in_code:
                out.append("</code></pre>"); in_code = False
            else:
                _close_list(); _close_table()
                lang = line[3:].strip()
                out.append(f'<pre><code class="language-{lang}">' if lang else "<pre><code>")
                in_code = True
            continue
        if in_code:
            out.append(line.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;"))
            continue
        if re.match(r"^(---+|\*\*\*+|___+)\s*$", line):
            _close_list(); _close_table(); out.append("<hr>"); continue
        hm = re.match(r"^(#{1,4})\s+(.*)", line)
        if hm:
            _close_list(); _close_table()
            lvl = len(hm.group(1))
            out.append(f"<h{lvl}>{_inline(hm.group(2))}</h{lvl}>"); continue
        if line.startswith("> "):
            _close_list(); _close_table()
            out.append(f"<blockquote>{_inline(line[2:])}</blockquote>"); continue
        if "|" in line and re.match(r"^\|", line.strip()):
            _close_list()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not in_table:
                out.append("<table><thead><tr>" +
                           "".join(f"<th>{_inline(c)}</th>" for c in cells) +
                           "</tr></thead><tbody>")
                in_table = True
            elif all(re.match(r"^:?-+:?$", c.replace(" ","")) for c in cells if c):
                pass
            else:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
            continue
        else:
            _close_table()
        ulm = re.match(r"^(\s*)[-*+]\s+(.*)", line)
        if ulm:
            if not in_list: out.append("<ul>"); in_list = True
            out.append(f"<li>{_inline(ulm.group(2))}</li>"); continue
        olm = re.match(r"^(\s*)\d+\.\s+(.*)", line)
        if olm:
            if not in_olist: out.append("<ol>"); in_olist = True
            out.append(f"<li>{_inline(olm.group(2))}</li>"); continue
        _close_list()
        if not line.strip(): continue
        out.append(f"<p>{_inline(line)}</p>")
    _close_list(); _close_table()
    if in_code: out.append("</code></pre>")
    return "\n".join(out)


def html_to_text(html: str, preserve_structure: bool = True) -> str:
    """HTML → plain text, preserving tables and lists as structured text."""
    text = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", html, flags=re.I)
    text = re.sub(r"<style[^>]*>[\s\S]*?</style>",   "", text,  flags=re.I)
    text = re.sub(r"<!--[\s\S]*?-->",                 "", text)
    if preserve_structure:
        # Tables → pipe-separated rows
        def _table(m: re.Match) -> str:
            rows = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", m.group(0), re.I)
            lines = []
            for row in rows:
                cells = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", row, re.I)
                cells = [re.sub(r"<[^>]+>", " ", c).strip() for c in cells]
                if any(cells):
                    lines.append(" | ".join(cells))
            return "\n".join(lines) + "\n"
        text = re.sub(r"<table[^>]*>[\s\S]*?</table>", _table, text, flags=re.I)
        # Lists → bullets
        text = re.sub(r"<li[^>]*>",  "• ", text, flags=re.I)
        text = re.sub(r"</li>",      "\n", text, flags=re.I)
        # Headings
        for h in ("h1","h2","h3","h4"):
            text = re.sub(rf"<{h}[^>]*>", "\n### ", text, flags=re.I)
            text = re.sub(rf"</{h}>",     "\n",      text, flags=re.I)
        # Block breaks
        text = re.sub(r"</?(?:p|div|section|article|br)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r" {3,}", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()[:16000]


async def deep_crawl_url(url: str, depth: int, breadth: int, timeout: float,
                         job_id: str = "", on_page: Optional[Callable] = None) -> str:
    """Fetch a URL and optionally crawl child links. Returns concatenated text.
    Calls on_page(url, text_chars) after each successful page fetch."""
    collected: list[str] = []
    visited: set[str] = set()

    async def fetch_one(u: str, remaining_depth: int):
        if u in visited or len(collected) > 20: return
        visited.add(u)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
                r = await c.get(u, headers={"User-Agent":"Vera-Research/1.0"})
                html = r.text
            text = html_to_text(html)
            if text:
                collected.append(f"[{u}]\n{text[:3000]}")
                if on_page:
                    try: await on_page(u, len(text))
                    except Exception: pass
                if job_id:
                    from urllib.parse import urlparse
                    domain = urlparse(u).netloc
                    await broadcast(job_id, {
                        "type": "crawl_progress",
                        "url": u,
                        "domain": domain,
                        "chars": len(text),
                        "depth": depth - remaining_depth,
                    })
            if remaining_depth > 0:
                child_links = extract_links(html, u)[:breadth]
                await asyncio.gather(*[fetch_one(cl, remaining_depth-1) for cl in child_links],
                                     return_exceptions=True)
        except Exception as e:
            if job_id:
                await broadcast(job_id, {"type":"crawl_error","url":u,"error":str(e)[:80]})
            log.debug("crawl %s: %s", u, e)

    await fetch_one(url, depth)
    return "\n\n---\n\n".join(collected)


# ══════════════════════════════════════════════════════════════════════════════
#  Redis source
# ══════════════════════════════════════════════════════════════════════════════

async def query_redis(query: str) -> list[Citation]:
    src = next((s for s in sources if s.id == "redis" and s.enabled), None)
    if not src: return []
    try:
        import redis.asyncio as aioredis  # type: ignore
        r = aioredis.Redis(
            host=src.config.get("host","llm.int"),
            port=int(src.config.get("port",6379)),
            password=src.config.get("password") or None,
            db=int(src.config.get("db",0)),
            decode_responses=True,
        )
        prefix = src.config.get("prefix","vera:")
        # Simple key scan
        keys = []
        async for k in r.scan_iter(f"{prefix}*", count=100):
            keys.append(k)
            if len(keys) >= 20: break
        # Filter by query words
        query_words = set(query.lower().split())
        citations = []
        for k in keys:
            val = await r.get(k)
            if not val: continue
            if any(w in val.lower() for w in query_words if len(w) > 3):
                citations.append(Citation(
                    id=str(uuid.uuid4())[:8],
                    url=f"redis://{k}",
                    title=k,
                    snippet=val[:300],
                    source_type="redis",
                ))
        await r.aclose()
        return citations
    except ImportError:
        log.debug("redis package not installed")
        return []
    except Exception as e:
        log.warning("Redis query failed: %s", e)
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  Web search & source gathering
# ══════════════════════════════════════════════════════════════════════════════

async def search_searxng(query: str, limit: int) -> list[dict]:
    src = next((s for s in sources if s.id=="searxng" and s.enabled), None)
    if not src:
        any_sx = next((s for s in sources if s.id=="searxng"), None)
        log.warning("search_searxng BLOCKED: found=%s enabled=%s sources_total=%d",
                    bool(any_sx), getattr(any_sx, 'enabled', 'N/A'), len(sources))
        return []
    cfg = src.config if isinstance(src.config, dict) else {}
    if isinstance(src.config, str):
        try: cfg = json.loads(src.config)
        except Exception: cfg = {}
    host = cfg.get("host", "http://llm.int:8888")
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{host}/search",
                params={"q":query,"format":"json","language":"en","safesearch":web_cfg.safe_search})
            results = r.json().get("results",[])[:limit]
            if not results:
                log.warning("search_searxng: 0 results from %s for %r (HTTP %s)", host, query[:50], r.status_code)
            else:
                log.info("search_searxng: %d results for %r", len(results), query[:50])
            return results
    except Exception as e:
        log.warning("search_searxng FAILED: %s (host=%s)", e, host); return []


async def search_brave(query: str, limit: int) -> list[dict]:
    src = next((s for s in sources if s.id=="brave" and s.enabled), None)
    if not src:
        log.debug("search_brave: not found or disabled")
        return []
    cfg = src.config if isinstance(src.config, dict) else {}
    if isinstance(src.config, str):
        try: cfg = json.loads(src.config)
        except Exception: cfg = {}
    api_key = cfg.get("api_key", "")
    if not api_key:
        log.debug("search_brave: no api_key configured")
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get("https://api.search.brave.com/res/v1/web/search",
                params={"q":query,"count":limit},
                headers={"Accept":"application/json","X-Subscription-Token":api_key})
            results = [{"url":w["url"],"title":w["title"],"content":w.get("description","")}
                    for w in r.json().get("web",{}).get("results",[])[:limit]]
            if not results:
                log.warning("search_brave: 0 results for %r (HTTP %s)", query[:50], r.status_code)
            return results
    except Exception as e:
        log.warning("search_brave FAILED: %s", e); return []


def _clean_search_url(url: str) -> str:
    """
    Decode tracker/redirect wrapper URLs back to the real destination URL.

    Handles:
      DuckDuckGo  https://duckduckgo.com/l/?uddg=<encoded>&rut=...
      SearXNG     may pass through similar redirects
      HTML entity decoded URLs  (& → &amp; etc.)
    """
    import html as _html
    from urllib.parse import urlparse, parse_qs, unquote
    url = _html.unescape(url)          # &amp; → &, &#x2F; → / etc.
    parsed = urlparse(url)
    # DuckDuckGo redirect  /l/ or /l.php
    if parsed.netloc in ("duckduckgo.com","www.duckduckgo.com") and parsed.path.startswith("/l"):
        qs = parse_qs(parsed.query)
        target = qs.get("uddg", qs.get("u", [""]))[0]
        if target:
            return unquote(target)
    # Google AMP/redirect
    if "/url?" in url:
        qs = parse_qs(parsed.query)
        target = qs.get("url", qs.get("q", [""]))[0]
        if target:
            return unquote(target)
    # Bing redirect
    if parsed.netloc.endswith("bing.com") and parsed.path.startswith("/ck/"):
        qs = parse_qs(parsed.query)
        target = qs.get("u", [""])[0]
        if target:
            return unquote(target.lstrip("a1"))
    return url


async def search_ddg(query: str, limit: int) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as c:
            r = await c.get("https://html.duckduckgo.com/html/",
                params={"q":query}, headers={"User-Agent":"Vera-Research/1.0"})
        links    = re.findall(r'class="result__a"[^>]+href="([^"]+)"[^>]*>([^<]+)<', r.text)
        snippets = re.findall(r'class="result__snippet"[^>]*>([^<]+)<', r.text)
        results = []
        for i, (raw_url, title) in enumerate(links[:limit]):
            clean = _clean_search_url(raw_url)
            # Skip DDG internal pages and empty URLs
            if not clean or "duckduckgo.com" in clean:
                continue
            results.append({
                "url":     clean,
                "title":   title.strip(),
                "content": snippets[i] if i < len(snippets) else ""
            })
        return results
    except Exception as e:
        log.debug("ddg: %s", e); return []


def _filter_arxiv(
    citations: "list[Citation]",
    directive: "Optional[ResearchDirective]" = None,
) -> "list[Citation]":
    """
    Remove arXiv citations that are irrelevant to the query type.
    Only keeps arXiv when directive confirms academic/technical intent
    OR the query itself contains academic signal terms.

    Also triggers a background log warning when arXiv citations are dropped
    so operators can tune the gate if needed.
    """
    # Determine whether academic sources are warranted
    is_academic_style = (
        directive and directive.valid and
        directive.output_style in ("deep_analysis", "report") and
        any(t in (directive.scope_focus or "").lower() for t in _ACADEMIC_QUERY_TERMS)
    )
    academic_source_priority = (
        directive and directive.valid and
        any("academic" in p.lower() or "paper" in p.lower() or "research" in p.lower()
            for p in (directive.source_priority or []))
    )
    allow = is_academic_style or academic_source_priority

    kept, dropped = [], []
    for c in citations:
        if "arxiv.org" in (c.url or "") or (c.source_type or "") == "arxiv":
            if allow:
                kept.append(c)
            else:
                dropped.append(c)
        else:
            kept.append(c)

    if dropped:
        log.info("_filter_arxiv: dropped %d arXiv citations (non-academic context)",
                 len(dropped))
    return kept


def _content_fingerprint(text: str) -> str:
    """A short fingerprint for deduplication — top-20 words sorted."""
    words = sorted(set(re.sub(r"[^a-z]", " ", text.lower()).split()) - _STOP_TERMS)[:20]
    return " ".join(words)

_STOP_TERMS = {
    "the","a","an","is","are","was","were","be","been","being","have","has",
    "had","do","does","did","will","would","could","should","may","might",
    "shall","can","of","in","on","at","to","for","with","by","from","as",
    "into","through","about","what","how","why","when","where","who","which",
    "that","this","these","those","and","or","but","not","no","also",
}


async def _decompose_query_for_search(
    query: str, fast: OllamaInstance, job_id: str
) -> list[str]:
    """
    Use the fast writer to decompose a query into 3-5 complementary search angles.
    Returns a list of search strings to run in parallel.
    Falls back to [query] on any failure so the pipeline never stalls.
    """
    prompt = (
        f"Research query: {query}\n\n"
        "Produce 4 distinct web search queries that together cover this topic from "
        "different angles (e.g. overview, technical detail, recent news, "
        "primary sources, criticism, data).\n"
        "Return ONLY a JSON array of strings. No explanation.\n"
        "Example: [\"query A\", \"query B\", \"query C\", \"query D\"]"
    )
    try:
        raw = await asyncio.wait_for(
            collect_ollama(fast, prompt,
                "You decompose research queries. Return ONLY a JSON array of strings.",
                job_id, timeout_secs=30),
            timeout=40,
        )
        start, end = raw.index("["), raw.rindex("]") + 1
        queries = json.loads(raw[start:end])
        # Validate: must be list of non-empty strings, max 5
        queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()][:5]
        if not queries:
            return [query]
        # Always include the original query
        if query not in queries:
            queries.insert(0, query)
        log.info("search decomposition: %d angles for %r", len(queries), query[:60])
        return queries[:5]
    except Exception as e:
        log.debug("query decomposition failed (%s) — using original", e)
        return [query]


async def gather_web_search(query: str, job_id: str) -> list[Citation]:
    """
    Multi-angle parallel web search with content deduplication.

    Improvements over v4:
    - Writer decomposes query into 3-5 complementary search angles
    - All angles searched in parallel
    - Results deduplicated by content fingerprint (not just URL)
    - Relevance filter tightened: at least 25% term overlap required
    - Each URL crawled once even if it appears in multiple search angles
    """
    limit = web_cfg.result_count
    engine = web_cfg.engine

    # --- Step 1: primary search fires immediately, extra angles in parallel --
    # The primary search on the raw query starts right now — no waiting for LLM.
    # Query decomposition runs concurrently; its results are merged in.
    fast = await get_instance(ModelTier.WRITER) or await get_instance(ModelTier.THINKER)

    async def _do_search(q: str) -> list[dict]:
        r: list[dict] = []
        if engine == "brave":             r = await search_brave(q, limit)
        if not r and engine in ("searxng","auto"): r = await search_searxng(q, limit)
        if not r:                          r = await search_ddg(q, limit)
        return r

    async def _decompose_and_search_angles(primary_results: list[dict]) -> list[list[dict]]:
        """
        Generate extra search angles and validate them against what the primary
        search actually found.  Any angle that introduces proper nouns or named
        entities not present in the primary results is dropped — this prevents
        hallucinated angles (e.g. "Boejaker role in The Raveonettes" when the
        primary results show no connection to that band).
        """
        if not fast: return []
        angles = await _decompose_query_for_search(query, fast, job_id)
        extra  = [a for a in angles if a.strip().lower() != query.strip().lower()][:4]
        if not extra: return []

        # Build a corpus of text from the primary results for validation
        primary_text = " ".join(
            (r.get("title","") + " " + r.get("content", r.get("snippet",""))).lower()
            for r in primary_results[:8]
        )

        # Extract proper nouns from an angle (capitalised words not in the query)
        query_words_lower = set(query.lower().split())
        def _angle_proper_nouns(angle: str) -> list[str]:
            return [
                w for w in angle.split()
                if w and w[0].isupper() and w.lower() not in query_words_lower
                and len(w) > 2 and w.lower() not in _STOP_TERMS
            ]

        # Filter: keep angle if ALL its proper nouns appear in primary results
        validated: list[str] = []
        skipped:   list[str] = []
        for angle in extra:
            nouns = _angle_proper_nouns(angle)
            if not nouns:
                validated.append(angle)   # no new proper nouns = safe
                continue
            # Check each proper noun appears in primary results
            all_found = all(noun.lower() in primary_text for noun in nouns)
            if all_found:
                validated.append(angle)
            else:
                missing = [n for n in nouns if n.lower() not in primary_text]
                log.debug("angle dropped — unverified terms %s: %r", missing, angle[:60])
                skipped.append(angle)

        # If too many dropped, ask writer to generate safer replacements
        if skipped and len(validated) < 2 and fast and primary_results:
            primary_titles = "; ".join(r.get("title","") for r in primary_results[:5])
            replacement_prompt = (
                f"Research query: {query}\n\n"
                f"Primary search found these results: {primary_titles}\n\n"
                "Based ONLY on what was actually found, produce 2 safe search angles "
                "that explore real aspects of this topic. Do not invent associations "
                "that are not evidenced by the search results.\n"
                "Return ONLY a JSON array of strings."
            )
            try:
                raw2 = await asyncio.wait_for(
                    collect_ollama(fast, replacement_prompt,
                        "You produce safe, evidence-based search queries. Return ONLY JSON.",
                        job_id, timeout_secs=20),
                    timeout=25
                )
                start2 = raw2.find("["); end2 = raw2.rfind("]") + 1
                if start2 >= 0 and end2 > start2:
                    replacements = json.loads(raw2[start2:end2])
                    validated.extend(r.strip() for r in replacements
                                     if isinstance(r,str) and r.strip()
                                     and r.strip().lower() != query.strip().lower())
            except Exception as e:
                log.debug("Angle replacement failed: %s", e)

        validated = validated[:3]
        if not validated: return []

        await broadcast(job_id, {"type":"step","t":time.time(),
            "label":"Extra angles",
            "detail":f"{len(validated)} validated angles"
                     + (f" ({len(skipped)} dropped)" if skipped else "")})

        return [r for r in await asyncio.gather(
            *[_do_search(a) for a in validated], return_exceptions=True
        ) if isinstance(r, list)]

    # Primary search fires first; angles wait for it so they can be validated
    # against the actual primary results (prevents hallucinated associations)
    primary_raw = await _do_search(query)
    primary_safe = primary_raw if isinstance(primary_raw, list) else []
    angle_lists = await _decompose_and_search_angles(primary_safe)

    # --- Step 2: merge all results, URL-deduplicate --------------------------
    seen_urls: set[str] = set()
    merged: list[dict] = []
    for item in (primary_raw if isinstance(primary_raw, list) else []):
        url = _clean_search_url(item.get("url",""))
        if url and url not in seen_urls: seen_urls.add(url); merged.append(item)
    for angle_res in (angle_lists if isinstance(angle_lists, list) else []):
        for item in angle_res:
            url = _clean_search_url(item.get("url",""))
            if url and url not in seen_urls: seen_urls.add(url); merged.append(item)

    results = merged

    citations: list[Citation] = []
    crawl_tasks, shot_tasks = [], []

    # --- Step 4: relevance filter + build citation list --------------------
    query_terms = set(re.sub(r"[^a-z0-9 ]", " ", query.lower()).split()) - _STOP_TERMS

    def _relevance(title: str, snippet: str) -> float:
        text = (title + " " + snippet).lower()
        if not query_terms: return 1.0
        return sum(1 for t in query_terms if t in text) / len(query_terms)

    content_fps: set[str] = set()   # content fingerprints for semantic dedup

    for item in results:
        url     = _clean_search_url(item.get("url", ""))
        title   = item.get("title", url)
        snippet = item.get("content", item.get("snippet", ""))[:400]
        if not url: continue

        # Adaptive relevance filter — strict when we have many results, lenient when few
        rel = _relevance(title, snippet)
        thresh = 0.20 if len(results) > 8 else 0.10
        if rel < thresh and len(citations) >= 6:
            log.debug("Skipping low-relevance result (%.0f%%): %s", rel*100, title[:60])
            continue

        # Content fingerprint dedup — catches mirror sites / near-duplicates
        fp = _content_fingerprint(title + " " + snippet)
        if fp in content_fps:
            log.debug("Skipping near-duplicate: %s", title[:60])
            continue
        content_fps.add(fp)

        cit = Citation(id=str(uuid.uuid4())[:8], url=url, title=title,
                       snippet=snippet, source_type="web")
        citations.append(cit)
        shot_tasks.append(_safe_screenshot(url))
        depth = max(1, web_cfg.crawl_depth)
        _jid = job_id if isinstance(job_id, str) else ""
        crawl_tasks.append(deep_crawl_url(url, depth,
                                           web_cfg.crawl_breadth, web_cfg.crawl_timeout,
                                           job_id=_jid))

    shots, crawls = await asyncio.gather(
        asyncio.gather(*shot_tasks, return_exceptions=True),
        asyncio.gather(*crawl_tasks, return_exceptions=True),
    )
    for cit, shot, crawled in zip(citations, shots, crawls):
        if isinstance(shot, str): cit.screenshot_path = shot
        if isinstance(crawled, str) and crawled: cit.full_text = crawled

    return citations


# Intents that justify arXiv — everything else is suppressed
_ACADEMIC_INTENTS = {
    "academic", "technical", "security", "code", "documentation",
}

# Query patterns that positively indicate an academic need
_ACADEMIC_QUERY_TERMS = {
    "paper","research","study","algorithm","model","neural","deep learning",
    "machine learning","transformer","llm","embedding","benchmark","dataset",
    "training","inference","architecture","method","approach","theorem",
    "proof","optimization","gradient","attention","diffusion","generative",
    "survey","review","analysis","empirical","experimental","ablation",
    "sota","state of the art","baseline","fine-tuning","pretraining",
    "quantum","protein","molecule","genome","climate model","physics",
    "cryptography","formal verification","compiler","type system",
}

# Terms that strongly indicate a query should NOT hit arXiv under any circumstances
_NON_ACADEMIC_TERMS = {
    "news","today","latest","this week","this month","breaking",
    "cve","vulnerability","breach","hack","exploit","attack","advisory",
    "price","stock","market","crypto","trading","earnings","revenue",
    "weather","sports","celebrity","announcement","release","update",
    "how to","tutorial","guide","setup","install","deploy","configure",
    "best","top","ranked","comparison","vs","versus","review","recommend",
    "recipe","pokemon","game","gaming","movie","music","book","tv",
    "company","startup","product","service","app","software","tool",
    "buy","sell","cost","price","cheap","free","discount",
}

async def gather_arxiv(
    query: str,
    limit: int = 6,
    intent: str = "",
) -> list[Citation]:
    """
    Fetch arXiv papers. Fires ONLY when the query is positively academic:
      - intent is in _ACADEMIC_INTENTS, OR
      - query contains ≥1 academic term AND no non-academic terms
    Falls back to empty list for any gaming/news/product/how-to query.
    """
    # Source must be explicitly enabled
    if not any(s.id=="arxiv" and s.enabled for s in sources):
        return []
    ql = query.lower()
    # Hard blocklist — non-academic signals always suppress
    non_ac_hits = sum(1 for t in _NON_ACADEMIC_TERMS if t in ql)
    if non_ac_hits >= 1:
        log.debug("gather_arxiv: suppressed (non_ac=%d) for %r", non_ac_hits, query[:50])
        return []
    # Positive whitelist — must have academic intent OR academic term
    has_academic_intent = intent in _ACADEMIC_INTENTS
    has_academic_term   = any(t in ql for t in _ACADEMIC_QUERY_TERMS)
    if not has_academic_intent and not has_academic_term:
        log.debug("gather_arxiv: suppressed (no academic signal) for %r", query[:50])
        return []
    log.debug("gather_arxiv: querying for %r", query[:60])
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get("https://export.arxiv.org/api/query",
                params={"search_query":f"all:{query}","max_results":limit,"sortBy":"relevance"})
        cits = []
        for entry in re.findall(r"<entry>(.*?)</entry>", r.text, re.S):
            tm = re.search(r"<title>(.*?)</title>", entry, re.S)
            sm = re.search(r"<summary>(.*?)</summary>", entry, re.S)
            im = re.search(r"<id>(.*?)</id>", entry)
            url = im.group(1).strip() if im else ""
            if url:
                cits.append(Citation(id=str(uuid.uuid4())[:8], url=url,
                    title=(tm.group(1).strip().replace("\n"," ") if tm else "arXiv"),
                    snippet=(sm.group(1).strip().replace("\n"," ")[:300] if sm else ""),
                    source_type="arxiv"))
        return cits
    except Exception as e:
        log.debug("arxiv: %s", e); return []


async def gather_hackernews(query: str, limit: int = 8) -> list[Citation]:
    # Active if explicitly named "hackernews" OR if any enabled NEWS source is active
    if not (any(s.id=="hackernews" and s.enabled for s in sources) or
            any(s.type==SourceType.NEWS and s.enabled for s in sources)):
        return []
    log.debug("gather_hackernews: querying for %r", query[:60])
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get("https://hn.algolia.com/api/v1/search",
                params={"query":query,"hitsPerPage":limit,"tags":"story"})
        return [Citation(id=str(uuid.uuid4())[:8],
            url=h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID','')}",
            title=h.get("title","HN Story"),
            snippet=f"Points:{h.get('points',0)} · Comments:{h.get('num_comments',0)}",
            source_type="hackernews") for h in r.json().get("hits",[])]
    except Exception as e:
        log.debug("hn: %s", e); return []




# ══════════════════════════════════════════════════════════════════════════════
#  Neo4j source
# ══════════════════════════════════════════════════════════════════════════════

def _neo4j_extract_cits(records, prop: str, uri: str) -> list:
    """Extract Citation objects from neo4j records regardless of driver type (sync or async)."""
    cits = []
    for record in records:
        try:
            # record.data() works on both sync and async neo4j records
            row = record.data() if hasattr(record, "data") else dict(record)
            # Find the first node-like object in the row values
            node: dict = {}
            for v in row.values():
                if v is None:
                    continue
                if hasattr(v, "keys") and hasattr(v, "get"):
                    try:
                        node = dict(v)
                        break
                    except Exception:
                        pass
                elif isinstance(v, dict):
                    node = v
                    break
            # Fall back: treat all scalar values as a flat dict
            if not node:
                node = {k: str(v) for k, v in row.items() if v is not None}
            text  = (node.get(prop) or node.get("text") or node.get("content")
                     or node.get("name") or str(node)[:400])
            title = (node.get("title") or node.get("name") or node.get("id")
                     or "Neo4j node")
            url   = (node.get("url") or node.get("uri")
                     or f"neo4j://{uri}/{str(node.get('id','?'))}")
            cits.append(Citation(
                id=str(uuid.uuid4())[:8], url=url, title=str(title)[:120],
                snippet=str(text)[:400], source_type="neo4j",
            ))
        except Exception as exc:
            log.debug("neo4j record parse error: %s", exc)
    return cits


def _neo4j_build_cypher(cypher: str, label: str, prop: str) -> tuple[str, str]:
    """Return (cypher_str, param_name) for the query to run."""
    if cypher:
        return cypher, "query"
    if label:
        return (
            f"MATCH (n:`{label}`) WHERE toLower(n.`{prop}`) CONTAINS toLower($q) RETURN n LIMIT 15",
            "q",
        )
    # Generic full-text scan — searches text/name/content/title properties
    return (
        "MATCH (n) WHERE "
        "toLower(coalesce(n.text,'')) CONTAINS toLower($q) OR "
        "toLower(coalesce(n.name,'')) CONTAINS toLower($q) OR "
        "toLower(coalesce(n.content,'')) CONTAINS toLower($q) OR "
        "toLower(coalesce(n.title,'')) CONTAINS toLower($q) "
        "RETURN n LIMIT 15",
        "q",
    )


async def _query_neo4j_via_vera_session(cypher_str: str, param_name: str,
                                        query: str, prop: str, uri: str) -> list[Citation]:
    """
    Try to run the Cypher against a live Vera session driver.
    Vera exposes its Neo4j driver at /api/sessions/active → vera.mem.graph._driver.
    Using the existing driver avoids opening a duplicate connection and works
    even when the bolt port is firewalled to local-only.
    Returns [] on any failure so the caller can fall back.
    """
    try:
        from Vera.ChatUI.api.session import sessions, get_or_create_vera  # type: ignore
        if not sessions:
            return []
        sid = sorted(sessions.keys(), reverse=True)[0]
        vera = get_or_create_vera(sid)
        drv  = vera.mem.graph._driver          # sync neo4j Driver
        with drv.session() as db_sess:
            result = db_sess.run(cypher_str, {param_name: query})
            records = list(result)
        cits = _neo4j_extract_cits(records, prop, uri)
        log.info("neo4j (via Vera session) returned %d results", len(cits))
        return cits
    except Exception as e:
        log.debug("neo4j via Vera session failed (%s), will try direct", e)
        return []


async def query_neo4j(query: str) -> list[Citation]:
    src = next((s for s in sources if s.id == "neo4j" and s.enabled), None)
    if not src: return []
    uri      = src.config.get("uri", "bolt://localhost:7687")
    user     = src.config.get("user", "neo4j")
    password = src.config.get("password", "")
    cypher   = src.config.get("cypher", "")
    label    = src.config.get("node_label", "")
    prop     = src.config.get("text_property", "text")

    cypher_str, param_name = _neo4j_build_cypher(cypher, label, prop)

    # ── Strategy 1: reuse the live Vera session driver (no extra connection) ──
    cits = await _query_neo4j_via_vera_session(cypher_str, param_name, query, prop, uri)
    if cits:
        return cits

    # ── Strategy 2: open a direct async connection ────────────────────────────
    try:
        from neo4j import AsyncGraphDatabase  # type: ignore
        drv = AsyncGraphDatabase.driver(uri, auth=(user, password))
        try:
            async with drv.session() as session:
                result = await session.run(cypher_str, {param_name: query})
                records = [r async for r in result]
            cits = _neo4j_extract_cits(records, prop, uri)
        finally:
            await drv.close()
        log.info("neo4j (direct async) returned %d results", len(cits))
        return cits
    except ImportError:
        log.warning("neo4j package not installed (pip install neo4j)")
        return []
    except Exception as e:
        log.warning("Neo4j query failed: %s", e)
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  ChromaDB source  (HTTP client OR local persistent directory)
# ══════════════════════════════════════════════════════════════════════════════

def _is_chroma_store(path: str) -> bool:
    """Return True if path looks like a ChromaDB PersistentClient store root."""
    import os as _os
    # Chroma v0.4+ stores: chroma.sqlite3 at root, or a 'data_level0.bin' / index files
    markers = ["chroma.sqlite3", "chroma-collections.parquet",
               "index", ".chroma"]
    return any(_os.path.exists(_os.path.join(path, m)) for m in markers)


def _find_chroma_stores(root: str) -> list[str]:
    """
    Given a root path, return all chroma store directories.
    If root itself is a store → [root].
    If root contains sub-directories that are stores → those sub-dirs.
    Recurses one level deep only (stores-of-stores not supported).
    """
    import os as _os
    if _is_chroma_store(root):
        return [root]
    # Scan immediate sub-directories
    stores = []
    try:
        for entry in sorted(_os.scandir(root), key=lambda e: e.name):
            if entry.is_dir() and _is_chroma_store(entry.path):
                stores.append(entry.path)
    except PermissionError:
        pass
    return stores or [root]   # fallback: try root even if no markers found


def _chroma_make_clients(src) -> list:
    """
    Return a list of (client, label) tuples to query.
    Supports:
      - HTTP server                          host + port
      - Single chroma store dir             directory = /path/to/store
      - Parent dir of multiple stores       directory = /path/to/parent   (auto-detected)
      - Comma-separated paths               directory = /path/a,/path/b
      - Glob patterns                       directory = /data/chroma*
    Falls back to Vera session chromadb client if nothing configured.
    """
    import chromadb  # type: ignore
    import glob as _glob, os as _os

    clients = []
    dir_val = src.config.get("directory", "").strip()
    host    = src.config.get("host", "localhost")
    port    = int(src.config.get("port", 8000))

    if dir_val:
        # Step 1: expand globs and comma-separated paths
        raw_paths = [p.strip() for p in dir_val.split(",") if p.strip()]
        candidate_roots: list[str] = []
        for p in raw_paths:
            globbed = _glob.glob(p)
            candidate_roots.extend(globbed if globbed else [p])

        # Step 2: for each root, find actual chroma stores (may be sub-dirs)
        all_store_paths: list[str] = []
        for root in candidate_roots:
            all_store_paths.extend(_find_chroma_stores(root))

        # Step 3: open a PersistentClient per store
        seen_paths: set[str] = set()
        for store_path in all_store_paths:
            real = _os.path.realpath(store_path)
            if real in seen_paths:
                continue
            seen_paths.add(real)
            try:
                client = chromadb.PersistentClient(path=store_path)
                label  = _os.path.basename(store_path.rstrip("/"))
                clients.append((client, f"local:{label}"))
                log.debug("chroma: PersistentClient at %s", store_path)
            except Exception as e:
                log.warning("chroma: cannot open store %s: %s", store_path, e)
    else:
        try:
            client = chromadb.HttpClient(host=host, port=port)
            clients.append((client, f"http:{host}:{port}"))
            log.debug("chroma: HttpClient at %s:%s", host, port)
        except Exception as e:
            log.warning("chroma: cannot create HttpClient %s:%s: %s", host, port, e)

    # Last resort: reuse the live Vera session chromadb instance
    if not clients:
        try:
            from Vera.ChatUI.api.session import sessions, get_or_create_vera  # type: ignore
            if sessions:
                sid  = sorted(sessions.keys(), reverse=True)[0]
                vera = get_or_create_vera(sid)
                client = vera.mem.vec
                clients.append((client, "vera-session"))
                log.debug("chroma: using Vera session vec client")
        except Exception as e:
            log.debug("chroma: Vera session vec fallback failed: %s", e)

    return clients


async def query_chroma(query: str) -> list[Citation]:
    src = next((s for s in sources if s.id == "chroma" and s.enabled), None)
    if not src: return []
    try:
        import chromadb  # type: ignore
        collection_filter = src.config.get("collection", "").strip()
        n_results = int(src.config.get("n_results", 8))
        max_cols  = int(src.config.get("max_collections", 10))

        clients = _chroma_make_clients(src)
        if not clients:
            log.warning("chroma: no usable client configured")
            return []

        cits = []
        for client, client_label in clients:
            try:
                if collection_filter:
                    col_names = [collection_filter]
                else:
                    all_cols  = client.list_collections()
                    col_names = [c.name for c in all_cols][:max_cols]

                log.debug("chroma(%s): searching %d collections for %r",
                          client_label, len(col_names), query[:60])

                for col_name in col_names:
                    try:
                        col   = client.get_collection(col_name)
                        count = col.count()
                        if count == 0:
                            log.debug("chroma: collection %s is empty, skipping", col_name)
                            continue
                        k = min(n_results, count)
                        results = col.query(query_texts=[query], n_results=k)
                        docs  = results.get("documents", [[]])[0]
                        metas = results.get("metadatas",  [[]])[0]
                        ids   = results.get("ids",         [[]])[0]
                        dists = results.get("distances",   [[]])[0]
                        for doc, meta, cid, dist in zip(docs, metas, ids, dists):
                            m     = meta or {}
                            url   = m.get("url") or m.get("source") or f"chroma://{client_label}/{col_name}/{cid}"
                            title = m.get("title") or m.get("name") or f"{col_name}/{cid[:40]}"
                            cits.append(Citation(
                                id=str(uuid.uuid4())[:8], url=url, title=str(title)[:120],
                                snippet=str(doc)[:400], source_type="chroma",
                            ))
                    except Exception as e:
                        log.debug("chroma(%s) collection %s: %s", client_label, col_name, e)

            except Exception as e:
                log.warning("chroma client %s failed: %s", client_label, e)

        log.info("chroma returned %d results total", len(cits))
        return cits
    except ImportError:
        log.warning("chromadb not installed (pip install chromadb)")
        return []
    except Exception as e:
        log.warning("Chroma query failed: %s", e)
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  GitHub source
# ══════════════════════════════════════════════════════════════════════════════

async def _github_crawl_readme(repo_url: str, token: str = "") -> str:
    """Fetch README content from a GitHub repo URL. Returns plain text."""
    try:
        # Convert HTML url → API url
        # https://github.com/owner/repo → https://api.github.com/repos/owner/repo/readme
        m = re.match(r"https://github\.com/([^/]+/[^/]+)", repo_url)
        if not m: return ""
        api_url = f"https://api.github.com/repos/{m.group(1)}/readme"
        headers = {"Accept": "application/vnd.github.raw+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(api_url, headers=headers)
            if r.status_code == 200:
                return r.text[:4000]
    except Exception as e:
        log.debug("README fetch %s: %s", repo_url, e)
    return ""


async def _github_web_fallback(query: str, limit: int = 6) -> list[dict]:
    """Fallback: web search restricted to github.com."""
    gh_query = f"site:github.com {query}"
    results = await search_searxng(gh_query, limit)
    if not results:
        results = await search_ddg(gh_query, limit)
    return results


async def gather_github(query: str, limit: int = 8) -> list[Citation]:
    """
    GitHub gathering — three strategies in order:
      1. GitHub Search API (repo + code search) — requires token
      2. Web search restricted to github.com — no token needed
      3. README/docs crawl for top repos found by either method
    """
    src = next((s for s in sources if s.id == "github" and s.enabled), None)
    if not src: return []
    token = src.config.get("token", "")
    if not token:
        log.info("GitHub: no token — using web-search fallback")
    orgs        = src.config.get("orgs", "")
    repos       = src.config.get("repos", "")
    search_code = src.config.get("search_code", False)
    cits: list[Citation] = []
    repo_urls: list[str] = []   # track for README crawl

    # ── Strategy 1: GitHub API (requires token) ───────────────────────────
    if token:
        api_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            async with httpx.AsyncClient(timeout=12.0, headers=api_headers) as c:
                scope = ""
                if repos:
                    scope = " ".join(f"repo:{r.strip()}" for r in repos.split(",") if r.strip())
                elif orgs:
                    scope = " ".join(f"org:{o.strip()}" for o in orgs.split(",") if o.strip())

                # Repository search
                r = await c.get("https://api.github.com/search/repositories",
                                params={"q": f"{query} {scope}".strip(),
                                        "per_page": limit, "sort": "stars"})
                if r.status_code == 200:
                    for item in r.json().get("items", [])[:limit]:
                        repo_urls.append(item["html_url"])
                        cits.append(Citation(
                            id=str(uuid.uuid4())[:8],
                            url=item["html_url"],
                            title=item.get("full_name", item["name"]),
                            snippet=(item.get("description") or "")[:300]
                                    + f"  ★{item.get('stargazers_count',0):,}",
                            source_type="github",
                        ))
                elif r.status_code == 401:
                    log.error("GitHub: 401 Unauthorized — check token; trying web fallback")
                    token = ""  # force web fallback below

                # Code search
                if r.status_code == 200 and (search_code or repos):
                    r2 = await c.get("https://api.github.com/search/code",
                                     params={"q": f"{query} {scope}".strip(),
                                             "per_page": limit // 2})
                    if r2.status_code == 200:
                        for item in r2.json().get("items", []):
                            url = item.get("html_url", "")
                            if url:
                                repo_url = "https://github.com/" + item.get(
                                    "repository", {}).get("full_name", "")
                                if repo_url not in repo_urls:
                                    repo_urls.append(repo_url)
                                cits.append(Citation(
                                    id=str(uuid.uuid4())[:8],
                                    url=url,
                                    title=(item.get("repository", {}).get("full_name", "")
                                           + "/" + item.get("name", "")),
                                    snippet=item.get("path", ""),
                                    source_type="github",
                                ))
        except Exception as e:
            log.warning("GitHub API failed: %s — trying web fallback", e)
            token = ""

    # ── Strategy 2: Web search fallback (no token or API failure) ─────────
    if not cits or not token:
        log.info("GitHub web-search fallback for: %r", query[:60])
        web_results = await _github_web_fallback(query, limit)
        for item in web_results:
            url = _clean_search_url(item.get("url", ""))
            if url and "github.com" in url:
                # Extract repo root from file URLs
                m = re.match(r"(https://github\.com/[^/]+/[^/]+)", url)
                repo_root = m.group(1) if m else url
                if repo_root not in repo_urls:
                    repo_urls.append(repo_root)
                cits.append(Citation(
                    id=str(uuid.uuid4())[:8],
                    url=url,
                    title=item.get("title", url),
                    snippet=item.get("content", item.get("snippet", ""))[:300],
                    source_type="github",
                ))

    # ── Strategy 3: README crawl for top repos ────────────────────────────
    # Fetch README for top-5 repos and populate full_text for better context
    readme_tasks = [_github_crawl_readme(u, token) for u in repo_urls[:5]]
    readmes = await asyncio.gather(*readme_tasks, return_exceptions=True)
    seen_cit_urls = {c.url for c in cits}
    for repo_url, readme in zip(repo_urls[:5], readmes):
        if isinstance(readme, str) and readme.strip():
            # Update the matching citation with full README text
            for c in cits:
                if repo_url in c.url and not c.full_text:
                    c.full_text = readme
                    c.tags = list(set(c.tags or []) | {"github", "has_readme"})
                    break
            else:
                # No existing citation for this repo — add one
                if repo_url not in seen_cit_urls:
                    cits.append(Citation(
                        id=str(uuid.uuid4())[:8],
                        url=repo_url,
                        title=repo_url.replace("https://github.com/", ""),
                        snippet=readme[:300],
                        source_type="github",
                        full_text=readme,
                        tags=["github", "has_readme"],
                    ))
                    seen_cit_urls.add(repo_url)

    log.info("github returned %d results (%d with README)", len(cits),
             sum(1 for c in cits if c.full_text))
    return cits


# ══════════════════════════════════════════════════════════════════════════════
#  Web archive sources (Wayback Machine + Common Crawl)
# ══════════════════════════════════════════════════════════════════════════════

async def gather_archive(query: str, active: set) -> list[Citation]:
    """Query Wayback Machine CDX API and/or Common Crawl index."""
    cits = []

    if "wayback" in active and any(s.id == "wayback" and s.enabled for s in sources):
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                # CDX API: find recent snapshots containing the query terms
                r = await c.get("http://web.archive.org/cdx/search/cdx",
                    params={
                        "q": query, "output": "json", "limit": 5,
                        "fl": "original,timestamp,statuscode,mimetype",
                        "filter": "statuscode:200", "collapse": "urlkey",
                    })
                if r.status_code == 200:
                    rows = r.json()[1:]  # skip header row
                    for row in rows[:5]:
                        orig_url, ts = row[0], row[1]
                        wb_url = f"https://web.archive.org/web/{ts}/{orig_url}"
                        cits.append(Citation(
                            id=str(uuid.uuid4())[:8],
                            url=wb_url,
                            title=f"[Archive] {orig_url[:80]}",
                            snippet=f"Archived {ts[:8]} from {orig_url[:100]}",
                            source_type="web_archive",
                        ))
        except Exception as e:
            log.debug("wayback: %s", e)

    if "commoncrawl" in active and any(s.id == "commoncrawl" and s.enabled for s in sources):
        try:
            # Common Crawl Index API
            async with httpx.AsyncClient(timeout=12.0) as c:
                r = await c.get("https://index.commoncrawl.org/CC-MAIN-2024-10-index",
                    params={"url": f"*{query.replace(' ','*')}*", "output": "json", "limit": 4})
                for line in r.text.strip().splitlines()[:4]:
                    try:
                        obj = json.loads(line)
                        url = obj.get("url", "")
                        cits.append(Citation(
                            id=str(uuid.uuid4())[:8],
                            url=url,
                            title=f"[CommonCrawl] {url[:80]}",
                            snippet=obj.get("filename", "")[:200],
                            source_type="web_archive",
                        ))
                    except Exception:
                        pass
        except Exception as e:
            log.debug("commoncrawl: %s", e)

    return cits


# ══════════════════════════════════════════════════════════════════════════════
#  Smart Gather — intent detection + structured data + doc crawl
# ══════════════════════════════════════════════════════════════════════════════

# ── Intent categories the fast model can identify ─────────────────────────
_INTENT_PROMPT = """\
Analyse this research query and return a JSON object with exactly these fields:
{
  "intent": one of: "general"|"structured_data"|"documentation"|"financial"|"osint"|"news_media"|"gaming"|"legal"|"academic"|"code"|"security"|"technical",
  "data_targets": ["authoritative domains or URLs to crawl directly"],
  "seed_urls":    ["specific deep URLs to fetch — e.g. NVD page, SEC filing, Wikipedia article"],
  "keywords":     ["4-8 precise search keywords"],
  "needs_images": true/false,
  "needs_tables": true/false,
  "osint_targets": ["entity names/domains to profile — for osint intent only"],
  "structured":   true/false,
  "source_queries": {
    "web":      "best web search query for this topic",
    "news":     "news-optimised query (recent angle, include dates if relevant)",
    "academic": "academic paper query — leave empty if not relevant",
    "github":   "GitHub repo/code search — leave empty if not code/security",
    "nvd":      "CVE/NVD keyword — leave empty if not security-related",
    "site_specific": ["optional site:domain.com query strings for authoritative sites"]
  }
}

Intent guide:
- general: broad informational
- structured_data: tables/databases (stats, specs, schedules, Pokédex)
- documentation: software library/API docs
- financial: company financials, SEC filings, earnings, stock data
- osint: company/person profiling, domain intel
- news_media: current events, breaking news
- gaming: game data, wikis, guides
- legal: legislation, case law, regulatory docs
- academic: research papers, citations
- code: programming implementation help
- security: CVEs, vulnerabilities, advisories, exploits, patches
- technical: engineering, systems, infrastructure

Return ONLY the JSON object. No explanation.
Query: {query}"""


async def _detect_intent(query: str, fast: OllamaInstance, job_id: str) -> dict:
    """Use the fast model to detect query intent and identify authoritative sources."""
    try:
        raw = await asyncio.wait_for(
            collect_ollama(fast, _INTENT_PROMPT.format(query=query),
                "You analyse research queries. Return only JSON.", job_id, timeout_secs=30),
            timeout=35
        )
        return json.loads(raw[raw.index("{"):raw.rindex("}")+1])
    except Exception as e:
        log.debug("Intent detection failed: %s", e)
        return {"intent": "general", "data_targets": [], "seed_urls": [],
                "keywords": query.split()[:6], "structured": False}


async def _fetch_structured_url(url: str, job_id: str, timeout: float = 15.0) -> Optional[Citation]:
    """
    Fetch a specific URL and return a Citation with full structured content.
    Preserves tables, infoboxes, lists for data-dense pages.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            r = await c.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; Vera-Research/2.0)",
                "Accept": "text/html,application/xhtml+xml"
            })
            if r.status_code >= 400:
                return None
            html = r.text
        text = html_to_text(html, preserve_structure=True)
        if not text or len(text) < 50:
            return None
        # Extract title from <title> tag
        title_m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
        title = title_m.group(1).strip() if title_m else url
        from urllib.parse import urlparse
        domain = urlparse(url).netloc
        await broadcast(job_id, {
            "type": "crawl_progress",
            "url": url, "domain": domain, "chars": len(text), "depth": 0
        })
        return Citation(
            id=str(uuid.uuid4())[:8],
            url=url, title=title, snippet=text[:400],
            source_type="structured", domain=domain,
            full_text=text
        )
    except Exception as e:
        log.debug("fetch_structured_url %s: %s", url, e)
        await broadcast(job_id, {"type": "crawl_error", "url": url, "error": str(e)[:60]})
        return None


async def _doc_site_crawl(base_url: str, query: str, job_id: str,
                           max_pages: int = 20, timeout: float = 12.0) -> list[Citation]:
    """
    Crawl a documentation site, score pages for relevance to the query,
    return the most relevant as Citations with full text.
    """
    await broadcast(job_id, {"type": "step", "t": time.time(),
                               "label": "Doc crawl", "detail": f"Crawling {base_url[:60]}…"})
    visited: set[str] = set()
    queue: list[str]  = [base_url]
    candidates: list[tuple[float, str, str, str]] = []  # (score, url, title, text)
    query_terms = set(re.sub(r"[^a-z0-9 ]", " ", query.lower()).split()) - {
        "the","a","an","is","are","was","be","of","in","to","for","with","how","what","why"
    }

    from urllib.parse import urlparse, urljoin
    base_domain = urlparse(base_url).netloc

    async def _fetch_and_score(url: str):
        if url in visited or len(visited) >= max_pages: return
        visited.add(url)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
                r = await c.get(url, headers={"User-Agent":"Vera-Research/2.0"})
                if r.status_code >= 400: return
                html = r.text
            text = html_to_text(html, preserve_structure=True)
            if not text: return
            title_m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
            title = title_m.group(1).strip() if title_m else url
            # Score relevance
            text_lower = text.lower()
            hits = sum(1 for t in query_terms if t in text_lower)
            score = hits / max(len(query_terms), 1)
            candidates.append((score, url, title, text))
            await broadcast(job_id, {
                "type": "crawl_progress",
                "url": url, "domain": base_domain,
                "chars": len(text), "depth": 0,
                "relevance": round(score, 2)
            })
            # Discover links to follow
            if len(visited) < max_pages:
                links = re.findall(r'href=["\']([^"\'#?]+)["\']', html)
                for l in links[:30]:
                    abs_url = urljoin(url, l)
                    p = urlparse(abs_url)
                    if (p.netloc == base_domain and p.scheme in ("http","https")
                            and abs_url not in visited):
                        queue.append(abs_url)
        except Exception as e:
            log.debug("doc_crawl %s: %s", url, e)

    # BFS with concurrency
    while queue and len(visited) < max_pages:
        batch = []
        while queue and len(batch) < 6:
            url = queue.pop(0)
            if url not in visited:
                batch.append(url)
        if batch:
            await asyncio.gather(*[_fetch_and_score(u) for u in batch])

    # Return top-scoring pages
    top = sorted(candidates, key=lambda x: -x[0])[:8]
    result = []
    for score, url, title, text in top:
        if score < 0.1 and len(result) > 2:
            continue  # skip very low relevance pages after we have 2 good ones
        result.append(Citation(
            id=str(uuid.uuid4())[:8], url=url, title=title,
            snippet=text[:400], source_type="documentation",
            domain=base_domain, full_text=text
        ))
    return result


# Well-known structured data sources by domain pattern
_STRUCTURED_SOURCES: dict[str, str] = {
    # Gaming — Pokemon
    "pokemon":      "https://bulbapedia.bulbagarden.net/wiki/",
    "pokémon":      "https://bulbapedia.bulbagarden.net/wiki/",
    "pokedex":      "https://bulbapedia.bulbagarden.net/wiki/List_of_Pok%C3%A9mon_by_National_Pok%C3%A9dex_number",
    "type chart":   "https://bulbapedia.bulbagarden.net/wiki/Type",
    "gen 1":        "https://bulbapedia.bulbagarden.net/wiki/Generation_I",
    # Finance / OSINT
    "sec filing":   "https://efts.sec.gov/LATEST/search-index?q={}&forms=10-K,10-Q",
    "annual report":"https://efts.sec.gov/LATEST/search-index?q={}&forms=10-K",
    "crunchbase":   "https://www.crunchbase.com/search/organizations",
    # News
    "bbc":          "https://www.bbc.co.uk/news",
    "reuters":      "https://www.reuters.com",
    "guardian":     "https://www.theguardian.com",
}

# Domain authority scores (higher = more trusted) — used in ranking
_DOMAIN_AUTHORITY: dict[str, float] = {
    # Encyclopedic / reference
    "en.wikipedia.org": 0.95, "bulbapedia.bulbagarden.net": 0.90,
    "scholar.google.com": 0.92, "arxiv.org": 0.72,  # only high for academic intents
    # Government / regulatory
    "sec.gov": 0.95, "gov.uk": 0.92, "irs.gov": 0.90,
    "legislation.gov.uk": 0.92, "eur-lex.europa.eu": 0.88,
    # News (established)
    "bbc.co.uk": 0.88, "bbc.com": 0.88, "reuters.com": 0.90,
    "theguardian.com": 0.85, "nytimes.com": 0.85, "ft.com": 0.87,
    "apnews.com": 0.88, "bloomberg.com": 0.87,
    # Tech docs
    "docs.python.org": 0.90, "developer.mozilla.org": 0.90,
    "pytorch.org": 0.88, "tensorflow.org": 0.88,
    "react.dev": 0.87, "fastapi.tiangolo.com": 0.85,
    # Finance data
    "finance.yahoo.com": 0.80, "marketwatch.com": 0.80,
    "crunchbase.com": 0.82, "pitchbook.com": 0.82,
    # Default for unknown domains
    "__default__": 0.40,
}

_DOC_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Python ecosystem
    (re.compile(r"\bpython\b.*\b(library|module|package|pip|import)\b", re.I),
     "https://docs.python.org/3/"),
    (re.compile(r"\bnumpy\b",      re.I), "https://numpy.org/doc/stable/"),
    (re.compile(r"\bpandas\b",     re.I), "https://pandas.pydata.org/docs/"),
    (re.compile(r"\bfastapi\b",    re.I), "https://fastapi.tiangolo.com/"),
    (re.compile(r"\btorch\b|\bpytorch\b", re.I), "https://pytorch.org/docs/stable/"),
    (re.compile(r"\btensorflow\b", re.I), "https://www.tensorflow.org/api_docs/"),
    (re.compile(r"\bscikit.?learn\b", re.I), "https://scikit-learn.org/stable/"),
    (re.compile(r"\bsqlalchemy\b", re.I), "https://docs.sqlalchemy.org/"),
    # JS/TS ecosystem
    (re.compile(r"\breact\b",      re.I), "https://react.dev/reference/"),
    (re.compile(r"\bnext\.?js\b",  re.I), "https://nextjs.org/docs/"),
    (re.compile(r"\bvue\.?js\b|\bvue\b.*\bframework\b", re.I), "https://vuejs.org/guide/"),
    (re.compile(r"\btailwind\b",   re.I), "https://tailwindcss.com/docs/"),
    (re.compile(r"\bsupabase\b",   re.I), "https://supabase.com/docs/"),
    # Systems
    (re.compile(r"\brust\b",       re.I), "https://doc.rust-lang.org/std/"),
    (re.compile(r"\bgo\b.*\b(lang|stdlib|package)\b", re.I), "https://pkg.go.dev/"),
    # Gaming wikis
    (re.compile(r"\bpokemon\b|\bpokémon\b", re.I), "https://bulbapedia.bulbagarden.net/wiki/"),
    (re.compile(r"\bminecraft\b",  re.I), "https://minecraft.wiki/"),
    (re.compile(r"\belden ring\b|\bsecrets of\b.*\bsouls\b", re.I), "https://eldenring.wiki.fextralife.com/"),
]



# ══════════════════════════════════════════════════════════════════════════════
#  Source ranking  — scores every citation on authority × relevance × freshness
# ══════════════════════════════════════════════════════════════════════════════

def _authority_score(domain: str) -> float:
    """Look up domain authority from the table; fall back to default."""
    score = _DOMAIN_AUTHORITY.get(domain)
    if score is not None:
        return score
    # Partial match (e.g. subdomain.bbc.co.uk → bbc.co.uk)
    for known, v in _DOMAIN_AUTHORITY.items():
        if known != "__default__" and known in domain:
            return v
    return _DOMAIN_AUTHORITY["__default__"]


def _freshness_score(cit: Citation) -> float:
    """Crude freshness: 1.0 for fetched < 1 hour ago, decays slowly."""
    age_hours = (time.time() - cit.fetched_at) / 3600
    return max(0.2, 1.0 - age_hours * 0.01)   # 0.01 per hour → 0.2 floor


def _tag_citation(cit: Citation, intent: str, query_terms: set[str]) -> list[str]:
    """Return a list of descriptive tags for a citation."""
    tags: list[str] = []
    # Source type tags
    if cit.source_type in ("structured", "documentation"):
        tags.append("structured")
    if cit.source_type == "documentation":
        tags.append("docs")
    # Authority
    auth = _authority_score(cit.domain)
    if auth >= 0.85:
        tags.append("authoritative")
    elif auth >= 0.65:
        tags.append("credible")
    # Content tags
    text = (cit.title + " " + cit.snippet).lower()
    if any(w in text for w in ("image","photo","picture","gallery","jpg","png","media")):
        tags.append("has_media")
    if any(w in text for w in ("table","stat","chart","data","list","comparison")):
        tags.append("has_data")
    if any(w in text for w in ("official","gov","authority","legislation","act")):
        tags.append("official")
    if intent in ("news_media",) or any(w in text for w in ("today","breaking","latest","news")):
        tags.append("news")
    # Relevance
    hits = sum(1 for t in query_terms if t in text)
    rel  = hits / max(len(query_terms), 1)
    if rel >= 0.7:
        tags.append("highly_relevant")
    elif rel >= 0.4:
        tags.append("relevant")
    if cit.image_urls:
        tags.append("images_found")
    return list(dict.fromkeys(tags))   # dedupe, preserve order


def _rank_citations(cits: list[Citation], query: str, intent: str) -> list[Citation]:
    """
    Compute a composite rank score for each citation and sort descending.
    rank = authority × relevance × freshness
    """
    query_terms = set(re.sub(r"[^a-z0-9 ]", " ", query.lower()).split()) - {
        "the","a","an","is","are","was","be","of","in","to","for","with","how","what","why"
    }
    for c in cits:
        text = (c.title + " " + c.snippet + " " + c.full_text[:200]).lower()
        hits = sum(1 for t in query_terms if t in text)
        rel  = hits / max(len(query_terms), 1)
        auth = _authority_score(c.domain)
        fresh= _freshness_score(c)
        c.rank_score = round(auth * 0.45 + rel * 0.45 + fresh * 0.10, 3)
        c.tags = _tag_citation(c, intent, query_terms)
    return sorted(cits, key=lambda c: -c.rank_score)


# ══════════════════════════════════════════════════════════════════════════════
#  Image extraction  — pulls images from crawled HTML, OG images, news images
# ══════════════════════════════════════════════════════════════════════════════

def _extract_images_from_html(html: str, base_url: str) -> list[str]:
    """Extract meaningful image URLs from a page (skip icons/logos < likely decorative)."""
    from urllib.parse import urljoin, urlparse
    imgs: list[str] = []
    # OG image first (most reliable for news)
    og = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if not og:
        og = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, re.I)
    if og:
        imgs.append(og.group(1))
    # Twitter card image
    tw = re.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if tw and tw.group(1) not in imgs:
        imgs.append(tw.group(1))
    # Regular img tags — filter by likely-meaningful size hints or alt text
    for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>', html, re.I):
        src = m.group(1)
        if any(skip in src.lower() for skip in ("icon","logo","sprite","blank","pixel","1x1","ad","banner","tracking")):
            continue
        if src.startswith("data:"): continue
        abs_url = urljoin(base_url, src)
        p = urlparse(abs_url)
        if p.scheme in ("http","https") and abs_url not in imgs:
            imgs.append(abs_url)
        if len(imgs) >= 6: break
    return imgs[:6]


# ══════════════════════════════════════════════════════════════════════════════
#  OSINT gather  — company/person profiling from multiple authoritative sources
# ══════════════════════════════════════════════════════════════════════════════

async def _osint_gather(
    entity: str, job: ResearchJob, fast: OllamaInstance
) -> list[Citation]:
    """
    Gather OSINT data for a company or person:
    - Companies House / SEC EDGAR (UK/US filings)
    - Crunchbase / LinkedIn (business profile)
    - WHOIS / domain intel
    - News sentiment
    Returns a list of Citations with structured full_text.
    """
    jid = job.id
    await broadcast(jid, {"type": "step", "t": time.time(),
                           "label": "OSINT", "detail": f"Profiling: {entity[:50]}"})

    # Parallel fetch of OSINT sources
    osint_urls = [
        f"https://find-and-update.company-information.service.gov.uk/search?q={entity.replace(' ','+')}",
        f"https://efts.sec.gov/LATEST/search-index?q={entity.replace(' ','+')}&forms=10-K,10-Q,S-1&dateRange=custom&startdt=2020-01-01",
        f"https://www.crunchbase.com/search/organizations/field/organizations/facet_ids/{entity.replace(' ','-').lower()}",
        f"https://www.google.com/search?q={entity.replace(' ','+')}+company+profile+linkedin+site:linkedin.com",
    ]
    search_queries = [
        f"{entity} company profile revenue employees founded",
        f"{entity} news recent funding acquisition IPO",
        f"{entity} CEO leadership team board directors",
        f'"{entity}" OSINT domain whois registrar',
    ]

    tasks = []
    for url in osint_urls:
        tasks.append(_fetch_structured_url(url, jid, timeout=12.0))
    for q in search_queries:
        tasks.append(gather_web_search(q, jid))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    cits: list[Citation] = []
    for res in results:
        if isinstance(res, Exception): continue
        if isinstance(res, Citation) and res:
            res.tags = ["osint", "structured"]
            cits.append(res)
        elif isinstance(res, list):
            for c in res[:3]:
                c.tags = list(set(c.tags or []) | {"osint"})
                cits.append(c)

    return cits


# ══════════════════════════════════════════════════════════════════════════════
#  Expansion planner  — thinker identifies thin sections, writer fills them
#  Used by the "Dive deeper" feature and by run_deep synthesis
# ══════════════════════════════════════════════════════════════════════════════

async def _plan_expansions(
    draft: str,
    original_query: str,
    thinker: OllamaInstance,
    job_id: str,
    max_expansions: int = 4,
) -> list[dict]:
    """
    Thinker reads the draft and returns a list of expansion targets:
    [{"section": "heading or excerpt", "expansion_query": "what to research", "priority": 1-5}]
    Higher priority = more important gap.
    """
    prompt = (
        f"Original query: {original_query}\n\n"
        f"Current draft (first 4000 chars):\n{draft[:4000]}\n\n"
        "Identify sections that are thin, vague, or missing key information. "
        "For each, provide a targeted research query that would improve it.\n"
        f"Return a JSON array of up to {max_expansions} objects:\n"
        '[{"section": "short excerpt or heading from the draft", '
        '"expansion_query": "specific query to research this further", '
        '"priority": 1-5}]\n'
        "Only return the JSON array. No other text."
    )
    try:
        raw = await asyncio.wait_for(
            collect_ollama(thinker, prompt,
                "You identify research gaps and expansion opportunities in drafts. "
                "Return only a JSON array.",
                job_id, timeout_secs=300),
            timeout=100
        )
        parsed = json.loads(raw[raw.index("["):raw.rindex("]")+1])
        expansions = [e for e in parsed if isinstance(e, dict) and "expansion_query" in e]
        return sorted(expansions, key=lambda e: -e.get("priority", 1))[:max_expansions]
    except Exception as e:
        log.debug("Expansion planning failed: %s", e)
        return []


async def _run_expansions(
    expansions: list[dict],
    job: ResearchJob,
    fast: OllamaInstance,
    existing_ctx: str,
) -> str:
    """
    Writer gathers data for each expansion target concurrently.
    Returns an addendum string to append to the synthesis context.
    """
    if not expansions or not fast:
        return ""

    async def _expand_one(exp: dict) -> str:
        q = exp.get("expansion_query", "")
        section = exp.get("section", "")
        if not q: return ""
        await broadcast(job.id, {"type": "step", "t": time.time(),
                                  "label": "Expanding", "detail": q[:60]})
        # Fast gather only — no slow model involved
        class _EJ:
            id = job.id
            sources = job.sources
            citations: list = []
        try:
            exp_fast = await get_instance(ModelTier.WRITER) or await get_instance(ModelTier.THINKER)
            cits, ctx = await smart_gather(q, _EJ(), fast=exp_fast)   # type: ignore
            # Merge new citations
            seen = {c.url for c in job.citations}
            for c in cits:
                if c.url not in seen:
                    job.citations.append(c); seen.add(c.url)
        except Exception:
            return ""
        if not ctx: return ""
        # Writer synthesises the expansion
        parts: list[str] = []
        async for tok in stream_ollama(
            fast,
            f"Section to expand: {section}\n\nExpansion query: {q}\n\n"
            f"Sources:\n{ctx[:3000]}\n\nExisting context:\n{existing_ctx[:1000]}\n\n"
            "Write 2-4 dense, well-cited paragraphs expanding this section. "
            "Cite sources as [N]. Stay tightly focused on the expansion query.",
            "You are a research writer producing focused section expansions. "
            "Be specific and cite every claim.",
            job.id, timeout_secs=WRITER_TIMEOUT
        ):
            parts.append(tok)
            if cancel_flags.get(job.id): break
        return f"\n\n### Expansion: {section[:60]}\n\n{''.join(parts)}"

    results = await asyncio.gather(*[_expand_one(e) for e in expansions],
                                   return_exceptions=True)
    addendum = ""
    for r in results:
        if isinstance(r, str): addendum += r
    return addendum




# ══════════════════════════════════════════════════════════════════════════════
#  AnalystEngine — host-local NLP pipeline
#  ─────────────────────────────────────────────────────────────────────────────
#  Runs ENTIRELY on the API host — no GPU, no LLM calls.
#  Produces a compact, structured AnalystReport that is fed to the thinker/writer
#  as enriched context, dramatically improving report quality by:
#    • Deduplicating near-identical claims across sources
#    • Identifying the highest-information-density sources
#    • Detecting contradictions and anomalies between sources
#    • Extracting key entities, facts, numbers
#    • Producing a compact knowledge summary (compactor role)
#    • Flagging gaps: things the query asked for that no source answered
#
#  The LLM analyst (when enabled) performs deeper structural analysis and
#  forwards its findings back through the same AnalystReport pipeline.
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AnalystReport:
    """Structured output from the AnalystEngine — fed to writer/thinker as context."""
    knowledge_bullets: list[str] = field(default_factory=list)
    top_sources: list[dict] = field(default_factory=list)
    contradictions: list[dict] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    entities: dict = field(default_factory=dict)
    anomalies: list[str] = field(default_factory=list)
    source_density: dict = field(default_factory=dict)
    # NEW: Timeline of dated events from sources
    timeline: list[dict] = field(default_factory=list)
    # NEW: Verbatim quotes and key statistics
    key_quotes: list[dict] = field(default_factory=list)
    # NEW: Keyword co-occurrence clusters
    term_clusters: list[list] = field(default_factory=list)
    # NEW: Per-source sentiment score (-1..+1)
    source_sentiment: dict = field(default_factory=dict)
    # NEW: LLM-produced report outline (feeds writer as synthesis plan)
    synthesis_plan: str = ""
    # NEW: LLM-scored knowledge bullets
    scored_bullets: list[dict] = field(default_factory=list)
    # NEW: Citations found by gap-fill targeted searches
    gap_fill_cits: list = field(default_factory=list)
    llm_notes: str = ""
    valid: bool = False
    elapsed: float = 0.0

    def to_context_string(self) -> str:
        """
        Compact, high-signal markdown fed to thinker/writer as synthesis context.
        Replaces raw source text — gives the LLM pre-processed signal, not noise.
        Includes: scored findings, timeline, quotes, entities, contradictions,
        anomalies, gaps, topic clusters, source ranking with sentiment.
        """
        if not self.valid:
            return ""
        # If we have no scored bullets AND no knowledge bullets, nothing to say
        if not self.scored_bullets and not self.knowledge_bullets:
            return ""
        parts = ["## Research Intelligence (Analyst Engine — host-local NLP)\n"]

        if self.synthesis_plan:
            parts.append("### Recommended Report Structure\n")
            parts.append(self.synthesis_plan[:2000])
            parts.append("")

        best = (self.scored_bullets or
                [{"text": b, "score": 3, "needs_verify": False}
                 for b in self.knowledge_bullets[:30]])
        if best:
            parts.append("### Key Findings (scored, deduplicated, density-ranked)\n")
            good  = [b for b in best if not b.get("needs_verify")]
            needv = [b for b in best if b.get("needs_verify")]
            parts.extend(f"- {b['text']}" for b in good[:25])
            if needv:
                parts.append("\n*Claims needing verification:*")
                parts.extend(f"- ⚠ {b['text']}" for b in needv[:5])
            parts.append("")

        if self.key_quotes:
            parts.append("### Key Quotes & Statistics\n")
            for q in self.key_quotes[:8]:
                parts.append(f"- \"{q['text'][:120]}\" — *{q['source'][:40]}*")
            parts.append("")

        if self.timeline:
            parts.append("### Chronological Events\n")
            for ev in self.timeline[:12]:
                parts.append(f"- **{ev['date']}** — {ev['event'][:100]} *(source: {ev['source'][:40]})*")
            parts.append("")

        if self.entities:
            parts.append("### Key Entities\n")
            for etype, vals in self.entities.items():
                if vals:
                    parts.append(f"- **{etype}**: {', '.join(str(v) for v in vals[:10])}")
            parts.append("")

        if self.contradictions:
            parts.append("### Contradictions Detected\n")
            for c in self.contradictions[:5]:
                src_a = c["sources"][0] if c["sources"] else "?"
                src_b = c["sources"][1] if len(c["sources"]) > 1 else "?"
                parts.append(f"- ⚠ *{src_a}*: `{c['claim_a'][:80]}`")
                parts.append(f"  ↔ *{src_b}*: `{c['claim_b'][:80]}`")
            parts.append("")

        if self.anomalies:
            parts.append("### Statistical Anomalies\n")
            parts.extend(f"- {a}" for a in self.anomalies[:5])
            parts.append("")

        if self.gaps:
            parts.append("### Research Gaps (unanswered aspects)\n")
            parts.extend(f"- {g}" for g in self.gaps[:6])
            parts.append("")

        if self.term_clusters:
            parts.append("### Topic Clusters Detected\n")
            for i, cl in enumerate(self.term_clusters[:4]):
                parts.append(f"- Cluster {i+1}: {', '.join(cl[:6])}")
            parts.append("")

        if self.top_sources:
            parts.append("### Highest-value Sources\n")
            for s in self.top_sources[:6]:
                sent_score = self.source_sentiment.get(s["url"], 0)
                sent = "+" if sent_score > 0.1 else "-" if sent_score < -0.1 else "o"
                parts.append(f"- [{sent}] **{s['title'][:60]}** "
                              f"({s['unique_facts']} facts, density {s['density']:.2f}) — {s['url']}")
            parts.append("")

        if self.gap_fill_cits:
            parts.append(f"### Gap-fill Sources ({len(self.gap_fill_cits)} additional)\n")
            for c in self.gap_fill_cits[:5]:
                parts.append(f"- **{c.title[:60]}** — {c.url}")
            parts.append("")

        if self.llm_notes:
            parts.append("### Analyst Notes\n")
            parts.append(self.llm_notes[:800])
            parts.append("")

        return "\n".join(parts)

    def to_report_section(self) -> str:
        """Rich markdown appendix appended to the final report."""
        if not self.valid:
            return ""
        # Guard: always produce *something* useful even if most fields are empty
        has_content = (
            self.knowledge_bullets or self.timeline or self.key_quotes
            or self.entities or self.contradictions or self.anomalies
            or self.gaps or self.top_sources or self.llm_notes
        )
        if not has_content:
            return ""
        parts = ["\n\n---\n\n## Research Analysis\n"]
        parts.append(
            f"*Host-local NLP · {self.elapsed:.1f}s · "
            f"{len(self.knowledge_bullets)} findings · "
            f"{len(self.top_sources)} sources ranked · "
            f"{len(self.contradictions)} contradictions*\n"
        )
        if self.timeline:
            parts.append("\n### Timeline\n")
            parts.append("| Date | Event | Source |")
            parts.append("|------|-------|--------|")
            for ev in self.timeline[:15]:
                d = ev["date"][:20]; e = ev["event"][:80]; s = ev["source"][:30]
                parts.append(f"| {d} | {e} | {s} |")
            parts.append("")
        if self.entities:
            for etype, vals in self.entities.items():
                if vals and len(vals) >= 2:
                    parts.append(f"\n### Extracted: {etype.title()}\n")
                    parts.append("| # | Value |")
                    parts.append("|---|-------|")
                    for i, v in enumerate(vals[:12], 1):
                        parts.append(f"| {i} | {str(v)[:60]} |")
                    parts.append("")
        if self.key_quotes:
            parts.append("\n### Key Quotes & Statistics\n")
            for q in self.key_quotes[:8]:
                parts.append(f"> {q['text'][:160]}\n> — *{q['source'][:50]}*\n")
        if self.contradictions:
            parts.append("\n### Source Contradictions\n")
            for c in self.contradictions[:5]:
                src_a = c["sources"][0] if c["sources"] else "?"
                src_b = c["sources"][1] if len(c["sources"]) > 1 else "?"
                parts.append(f"- **{src_a}**: {c['claim_a'][:100]}")
                parts.append(f"  vs **{src_b}**: {c['claim_b'][:100]}\n")
        if self.anomalies:
            parts.append("\n### Statistical Anomalies\n")
            parts.extend(f"- {a}" for a in self.anomalies[:5])
            parts.append("")
        if self.gaps:
            parts.append("\n### Research Gaps\n")
            parts.extend(f"- {g}" for g in self.gaps)
            parts.append("")
        if self.llm_notes:
            parts.append("\n### Analyst Assessment\n")
            parts.append(self.llm_notes[:1200])
            parts.append("")
        return "\n".join(parts)


class AnalystEngine:
    """
    Host-local NLP analysis pipeline.  Zero LLM calls (fast track).
    Optionally calls the analyst LLM for structural/argument analysis.
    """

    # Patterns for entity extraction
    _CVE_RE     = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)
    _CVSS_RE    = re.compile(r"CVSS\s*(?:Score)?[:\s]+([\d.]+)", re.I)
    _MONEY_RE   = re.compile(r"\$[\d,]+(?:\.\d+)?[MBKTmbt]?\b|(?:USD|GBP|EUR)\s*[\d,]+")
    _DATE_RE    = re.compile(
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}\b"
        r"|\b\d{4}-\d{2}-\d{2}\b"
        r"|\b(?:Q[1-4]\s+)?20\d{2}\b", re.I)
    _VERSION_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)*\b")
    _PERCENT_RE = re.compile(r"\b\d+(?:\.\d+)?\s*%")
    _NUMBER_RE  = re.compile(r"\b\d{2,}(?:[,.]\d+)*\b")
    _PROPER_RE  = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,3}\b")

    # Contradiction signal pairs — if two sources have opposing signals near the same term
    _CONTRA_PAIRS = [
        ({"increase","grew","up","rise","higher","more","positive","growth"},
         {"decrease","fell","down","decline","lower","less","negative","drop"}),
        ({"safe","secure","fixed","patched","resolved","mitigated"},
         {"unsafe","vulnerable","exposed","unpatched","critical","exploited"}),
        ({"supports","compatible","works","available","enabled"},
         {"unsupported","incompatible","broken","unavailable","disabled"}),
        ({"confirmed","proven","established","verified","certain"},
         {"unconfirmed","disputed","alleged","unclear","uncertain"}),
    ]

    def __init__(self, query: str, citations: list, job_id: str = ""):
        self.query    = query
        self.citations= citations
        self.job_id   = job_id
        self._query_terms = set(
            re.sub(r"[^a-z0-9 ]", " ", query.lower()).split()
        ) - _STOP_TERMS

    # ─── Public API ──────────────────────────────────────────────────────────

    async def run(
        self,
        analyst_inst: "Optional[OllamaInstance]" = None,
        slot_a: "Optional[AgentSlot]" = None,
        nlp_tools: "Optional[list]" = None,
    ) -> AnalystReport:
        """Run NLP pipeline with selected tools (None = all)."""
        _all_tools = nlp_tools is None
        def _active(tool: str) -> bool:
            return _all_tools or tool in (nlp_tools or [])
        log.info("AnalystEngine.run: tools=%s cits=%d",
                 "all" if _all_tools else nlp_tools, len(self.citations))
        t0 = time.time()
        report = AnalystReport()
        if not self.citations:
            log.warning("AnalystEngine: no citations — skipping")
            return report

        # Include titles in the text measure — title+snippet is enough for NLP phases
        total_text = sum(
            len(c.full_text or "") + len(c.snippet or "") + len(c.title or "")
            for c in self.citations
        )
        if total_text < 50:
            log.warning("AnalystEngine: citations too thin "
                        "(total_text=%d, cits=%d) — skipping",
                        total_text, len(self.citations))
            return report

        log.info("AnalystEngine: starting — %d citations, %d total chars",
                 len(self.citations), total_text)

        # --- Phase 1: text extraction + tokenisation (sync, instant) ---------
        src_tokens = {}       # url → list[str]
        src_text   = {}       # url → full plain text
        src_sents  = {}       # url → list[str]

        for c in self.citations:
            raw = (c.full_text or "") + " " + (c.snippet or "") + " " + (c.title or "")
            tokens = [w for w in re.sub(r"[^a-z0-9 ]"," ",raw.lower()).split()
                      if len(w) > 2 and w not in _STOP_TERMS]
            src_tokens[c.url] = tokens
            src_text[c.url]   = raw
            sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", raw) if len(s.strip()) > 20]
            src_sents[c.url]  = sents

        # --- Phase 2: per-source TF scoring → density ranking ----------------
        # TF across all sources combined (corpus frequency)
        corpus: list[str] = []
        for toks in src_tokens.values():
            corpus.extend(toks)
        corpus_freq = collections.Counter(corpus)
        total_corpus = max(len(corpus), 1)

        report.source_density = {}
        source_tfidf: dict[str, dict[str, float]] = {}  # url → {term: score}

        for c in self.citations:
            toks = src_tokens.get(c.url, [])
            if not toks: continue
            tf    = collections.Counter(toks)
            n_tok = max(len(toks), 1)
            scores: dict[str, float] = {}
            for term, cnt in tf.items():
                tf_score  = cnt / n_tok
                df        = corpus_freq.get(term, 1)
                idf       = math.log((total_corpus + 1) / (df + 1)) + 1
                scores[term] = tf_score * idf
            source_tfidf[c.url] = scores
            # Density = sum of top-20 TF-IDF scores (information richness proxy)
            top_scores = sorted(scores.values(), reverse=True)[:20]
            report.source_density[c.url] = round(sum(top_scores), 3)

        # Rank sources by density
        ranked_cits = sorted(self.citations,
                              key=lambda c: report.source_density.get(c.url, 0),
                              reverse=True)
        report.top_sources = []
        for c in ranked_cits[:8]:
            density = report.source_density.get(c.url, 0)
            # Unique facts ≈ number of sentences with query-term hits
            sents = src_sents.get(c.url, [])
            unique_facts = sum(
                1 for s in sents
                if any(t in s.lower() for t in self._query_terms)
            )
            report.top_sources.append({
                "title":        c.title[:80],
                "url":          c.url,
                "density":      density,
                "unique_facts": unique_facts,
                "domain":       c.domain,
            })

        # --- Phase 3: entity extraction ----------------------------------------
        all_text = " ".join(src_text.values())
        if _active("entities"):
            report.entities = {
                "CVE":      list(dict.fromkeys(self._CVE_RE.findall(all_text)))[:12],
                "CVSS":     list(dict.fromkeys(self._CVSS_RE.findall(all_text)))[:8],
                "versions": list(dict.fromkeys(self._VERSION_RE.findall(all_text)))[:10],
                "dates":    list(dict.fromkeys(self._DATE_RE.findall(all_text)))[:10],
                "monetary": list(dict.fromkeys(self._MONEY_RE.findall(all_text)))[:8],
                "percent":  list(dict.fromkeys(self._PERCENT_RE.findall(all_text)))[:8],
            }
            report.entities = {k: v for k, v in report.entities.items() if v}

        # --- Phase 4: contradiction detection ----------------------------------
        if _active("contradictions"):
            report.contradictions = []
            # For each source pair, look for opposing signals near shared query terms
            cit_list = self.citations[:12]
            for i in range(len(cit_list)):
                for j in range(i+1, len(cit_list)):
                    ca, cb = cit_list[i], cit_list[j]
                    ta = set(src_tokens.get(ca.url, []))
                    tb = set(src_tokens.get(cb.url, []))
                    shared = (ta & tb) & self._query_terms
                    if not shared: continue
                    for pos_set, neg_set in self._CONTRA_PAIRS:
                        a_pos = bool(ta & pos_set); a_neg = bool(ta & neg_set)
                        b_pos = bool(tb & pos_set); b_neg = bool(tb & neg_set)
                        if (a_pos and b_neg) or (a_neg and b_pos):
                            # Extract the most relevant sentence from each
                            def _best_sent(sents, signals):
                                for s in sents:
                                    sl = s.lower()
                                    if any(t in sl for t in signals) and any(t in sl for t in shared):
                                        return s[:120]
                                return (sents[0][:120] if sents else "")
                            sa_sents = src_sents.get(ca.url, [])
                            sb_sents = src_sents.get(cb.url, [])
                            report.contradictions.append({
                                "claim_a": _best_sent(sa_sents, pos_set if a_pos else neg_set),
                                "claim_b": _best_sent(sb_sents, pos_set if b_pos else neg_set),
                                "sources": [ca.domain, cb.domain],
                            })
                            break  # one contradiction per pair
                    if len(report.contradictions) >= 8:
                        break
                if len(report.contradictions) >= 8:
                    break

        # --- Phase 5: anomaly detection in numeric data -----------------------
        if _active("anomalies"):
            numbers_by_term: dict[str, list[float]] = collections.defaultdict(list)
            for url, sents in src_sents.items():
                for sent in sents:
                    nums = re.findall(r"\b(\d+(?:[,.]\d+)*)\b", sent)
                    for qt in self._query_terms:
                        if qt in sent.lower() and nums:
                            for n in nums[:3]:
                                try:
                                    numbers_by_term[qt].append(float(n.replace(",","")))
                                except ValueError:
                                    pass

            report.anomalies = []
            for term, vals in numbers_by_term.items():
                if len(vals) < 3: continue
                mn = sum(vals) / len(vals)
                if mn == 0: continue
                sd = math.sqrt(sum((v-mn)**2 for v in vals) / len(vals))
                for v in vals:
                    z = abs(v - mn) / (sd + 1e-9)
                    if z > 2.5:
                        report.anomalies.append(
                            f"Outlier value {v:,.0f} for '{term}' "
                            f"(mean={mn:,.0f}, σ={sd:,.0f}, z={z:.1f})"
                        )
                        break

        # --- Phase 6: knowledge compaction ------------------------------------
        # Collect sentences from high-density sources that hit query terms,
        # deduplicate by fingerprint, rank by TF-IDF, output top-N as bullets
        candidate_bullets: list[tuple[float, str]] = []
        seen_fps: set[str] = set()

        for c in ranked_cits[:10]:
            sents = src_sents.get(c.url, [])
            scores = source_tfidf.get(c.url, {})
            density = report.source_density.get(c.url, 0)
            for sent in sents:
                if len(sent) < 30 or len(sent) > 400: continue
                sl = sent.lower()
                query_hits = sum(1 for t in self._query_terms if t in sl)
                if query_hits == 0: continue
                # Score = query term density × source density × sentence length factor
                sent_words = sl.split()
                tfidf_sum  = sum(scores.get(w, 0) for w in sent_words)
                score      = (query_hits / max(len(self._query_terms),1)) * density + tfidf_sum
                fp         = _content_fingerprint(sent)
                if fp in seen_fps: continue
                seen_fps.add(fp)
                candidate_bullets.append((score, sent.strip()))

        candidate_bullets.sort(key=lambda x: -x[0])
        report.knowledge_bullets = [b for _, b in candidate_bullets[:35]]

        # --- Phase 7: gap detection -------------------------------------------
        report.gaps = []
        query_phrases = [
            p.strip() for p in re.split(r"[,;\s]+and\s+|\bor\b|\bvs\.?\b", self.query.lower())
            if len(p.strip()) > 5
        ]
        for phrase in query_phrases[:8]:
            phrase_words = set(phrase.split()) - _STOP_TERMS
            if not phrase_words: continue
            covered = any(
                all(w in " ".join(src_tokens.get(c.url,[]))
                    for w in phrase_words)
                for c in self.citations
            )
            if not covered:
                report.gaps.append(phrase)

        # --- Phase 9: timeline extraction -----------------------------------
        if _active("timeline"):
            _EV_DATE_RE = re.compile(
                r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
                r"[a-z]*\.?\s+\d{1,2},?\s+\d{4})"
                r"|(?:\d{4}-\d{2}-\d{2})"
                r"|(?:(?:Q[1-4]|H[12])\s+20\d{2})", re.I
            )
            timeline_events: list[dict] = []
            seen_tlfp: set[str] = set()
            for c in self.citations[:12]:
                for sent in src_sents.get(c.url, []):
                    dm = _EV_DATE_RE.search(sent)
                    if not dm: continue
                    if not any(t in sent.lower() for t in self._query_terms): continue
                    event_text = sent[:120].strip()
                    fp = _content_fingerprint(event_text)
                    if fp in seen_tlfp: continue
                    seen_tlfp.add(fp)
                    timeline_events.append({"date": dm.group(0)[:20],
                        "event": event_text, "source": c.domain or c.title[:30]})
                    if len(timeline_events) >= 20: break
            report.timeline = sorted(timeline_events, key=lambda e: e["date"])

        # --- Phase 10: key quotes / statistics ----------------------------
        if _active("key_quotes"):
            _QUOTE_RE = re.compile(
                r'"([^"]{20,180})"'  # double-quoted text
                r"|(\d+(?:\.\d+)?\s*%[^.]{0,60}\.)"  # percentage claim
                r"|(\$[\d,.]+[MBKTmbt]?[^.]{0,60}\.)", re.I
            )
            report.key_quotes = []
            seen_qfp: set[str] = set()
            for c in ranked_cits[:10]:
                sents = src_sents.get(c.url, [])
                scores_d = source_tfidf.get(c.url, {})
                for sent in sents:
                    m = _QUOTE_RE.search(sent)
                    if not m: continue
                    text = (m.group(1) or m.group(2) or m.group(3) or "").strip()
                    if len(text) < 15: continue
                    if not any(t in sent.lower() for t in self._query_terms): continue
                    fp = _content_fingerprint(text)
                    if fp in seen_qfp: continue
                    seen_qfp.add(fp)
                    rel = sum(scores_d.get(w, 0) for w in text.lower().split())
                    report.key_quotes.append({"text": text[:160],
                        "source": c.domain or c.title[:30], "relevance": round(rel, 3)})
                    if len(report.key_quotes) >= 15: break
            report.key_quotes.sort(key=lambda q: -q["relevance"])

        # --- Phase 11: keyword co-occurrence clusters ---------------------
        if _active("clusters"):
            top_terms_list = [
                t for t, _ in sorted(corpus_freq.items(), key=lambda x: -x[1])[:30]
                if t in self._query_terms or corpus_freq[t] > 3
            ]
            cooc: dict = collections.defaultdict(collections.Counter)
            for url, sents in src_sents.items():
                for sent in sents:
                    sl = sent.lower()
                    present = [t for t in top_terms_list if t in sl]
                    for a in present:
                        for b in present:
                            if a != b: cooc[a][b] += 1
            report.term_clusters = []
            assigned: set[str] = set()
            for term in top_terms_list:
                if term in assigned: continue
                cluster = [term]; assigned.add(term)
                for nbr, cnt in cooc[term].most_common(4):
                    if nbr not in assigned and cnt >= 2:
                        cluster.append(nbr); assigned.add(nbr)
                if len(cluster) >= 2: report.term_clusters.append(cluster)

        # --- Phase 12: source sentiment scoring --------------------------
        if _active("sentiment"):
            _POS_S = {"improve","effective","success","safe","secure","reliable","strong",
                      "advance","benefit","achieve","gain","increase","leading","best",
                      "solve","fix","patch","mitigate","resolve","protect"}
            _NEG_S = {"fail","risk","danger","threat","weak","flaw","bug","issue","breach",
                      "exploit","vulnerable","attack","error","problem","decline","loss",
                      "critical","severe","malicious","compromise","leak","expose"}
            report.source_sentiment = {}
            for c in self.citations:
                toks = src_tokens.get(c.url, [])
                if not toks: continue
                pos = sum(1 for t in toks if t in _POS_S)
                neg = sum(1 for t in toks if t in _NEG_S)
                report.source_sentiment[c.url] = round((pos - neg) / max(pos+neg, 1), 2)

        # --- Phase 8: structured LLM analysis ----------------------------
        if analyst_inst and len(report.knowledge_bullets) >= 4:
            try:
                if slot_a: slot_on(slot_a, analyst_inst, self.job_id, "analysing")
                await broadcast(self.job_id, {"type":"step","t":time.time(),
                    "label":"Analyst","detail":"Structured analysis…"})
                compact_input = (
                    f"Query: {self.query}\n\n"
                    f"Host-extracted findings ({len(report.knowledge_bullets)}):\n"
                    + "\n".join(f"{i+1}. {b}" for i,b in enumerate(report.knowledge_bullets[:25]))
                    + (f"\n\nEntities: "
                       + json.dumps({k:v[:5] for k,v in report.entities.items()}, ensure_ascii=False)
                       if report.entities else "")
                    + (f"\n\nContradictions ({len(report.contradictions)}):\n"
                       + "\n".join(f"- {c['claim_a'][:60]} vs {c['claim_b'][:60]}"
                                    for c in report.contradictions[:3])
                       if report.contradictions else "")
                    + (f"\n\nGaps: {', '.join(report.gaps)}" if report.gaps else "")
                    + (f"\n\nTimeline: {len(report.timeline)} events" if report.timeline else "")
                )
                llm_sys = (
                    "You are a critical research analyst. Return JSON with keys:\n"
                    "synthesis_plan: report outline (## headings + notes, ~200w)\n"
                    "scored_bullets: array of {text,score,needs_verify}\n"
                    "  score=0-5, needs_verify=true if questionable\n"
                    "analyst_notes: 2-3 sentence structural summary\n"
                    "Return ONLY valid JSON, no preamble."
                )
                llm_raw = await asyncio.wait_for(
                    collect_ollama(analyst_inst, compact_input, llm_sys,
                                   self.job_id, slot_a,
                                   timeout_secs=_effective_timeout(analyst_inst, 600)),
                    timeout=_effective_timeout(analyst_inst, 600)
                )
                if slot_a: slot_off(slot_a)
                try:
                    start = llm_raw.find("{"); end = llm_raw.rfind("}") + 1
                    parsed = json.loads(llm_raw[start:end]) if start >= 0 and end > start else {}
                    report.synthesis_plan = parsed.get("synthesis_plan","")[:3000]
                    raw_b = parsed.get("scored_bullets",[])
                    report.scored_bullets = [
                        {"text":b.get("text","")[:200],"score":int(b.get("score",3)),
                         "needs_verify":bool(b.get("needs_verify",False))}
                        for b in raw_b if isinstance(b,dict) and b.get("text")
                    ][:20]
                    report.llm_notes = parsed.get("analyst_notes","")[:600]
                except Exception as pe:
                    log.debug("Analyst JSON parse: %s", pe)
                    report.llm_notes = llm_raw[:600]
            except Exception as e:
                log.warning("Analyst LLM phase failed: %s", e)
                if slot_a: slot_off(slot_a)

        # Mark valid BEFORE gap-fill — if gap-fill times out we still return good results
        report.valid   = True
        report.elapsed = round(time.time() - t0, 2)
        log.info("AnalystEngine phases 1-12: %d bullets, %d contradictions, "
                 "%d gaps, %d timeline, %d quotes, %.2fs",
                 len(report.knowledge_bullets), len(report.contradictions),
                 len(report.gaps), len(report.timeline),
                 len(report.key_quotes), report.elapsed)

        # --- Phase 13: gap-fill targeted searches (best-effort, tight timeout) ---
        if report.gaps and self.job_id:
            try:
                # Cap at 2 gaps, each with a 10s timeout — must not block the pipeline
                gap_tasks = [
                    asyncio.wait_for(
                        gather_web_search(f"{self.query} {g}", self.job_id),
                        timeout=10.0
                    )
                    for g in report.gaps[:2]
                ]
                gap_results = await asyncio.gather(*gap_tasks, return_exceptions=True)
                new_cits: list = []
                existing_urls = {c.url for c in self.citations}
                for res in gap_results:
                    if isinstance(res, list):
                        for c in res:
                            if c.url not in existing_urls:
                                new_cits.append(c); existing_urls.add(c.url)
                if new_cits:
                    report.gap_fill_cits = new_cits[:6]
                    report.elapsed = round(time.time() - t0, 2)
                    await broadcast(self.job_id, {"type":"step","t":time.time(),
                        "label":"Gap fill",
                        "detail":f"+{len(new_cits)} for: {', '.join(report.gaps[:2])}"})
                    await broadcast(self.job_id, {"type":"citations",
                        "citations":[c.to_dict() for c in new_cits]})
            except Exception as e:
                log.warning("Gap-fill search failed (non-fatal): %s", e)

        return report


async def run_analyst_engine(
    query: str,
    citations: list,
    job_id: str,
    analyst_inst: "Optional[OllamaInstance]" = None,
    slot_a: "Optional[AgentSlot]" = None,
    nlp_tools: "Optional[list]" = None,
) -> AnalystReport:
    """Wrapper — runs selected NLP tools on citations."""
    engine = AnalystEngine(query, citations, job_id)
    return await engine.run(analyst_inst, slot_a, nlp_tools=nlp_tools)

async def _gather_nvd(keyword: str, job_id: str, limit: int = 12) -> list[Citation]:
    """Query the NVD (National Vulnerability Database) API v2.0 — free, no API key."""
    try:
        await broadcast(job_id, {"type":"step","t":time.time(),
            "label":"NVD/CVE","detail":f"Querying NVD: {keyword[:50]}"})
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get("https://services.nvd.nist.gov/rest/json/cves/2.0",
                params={"keywordSearch": keyword, "resultsPerPage": limit},
                headers={"User-Agent":"Vera-Research/2.0"})
        if r.status_code != 200:
            log.debug("NVD API %s: HTTP %s", keyword, r.status_code); return []
        cits = []
        for item in r.json().get("vulnerabilities", [])[:limit]:
            cve   = item.get("cve", {})
            cve_id= cve.get("id", "")
            desc  = next((d["value"] for d in cve.get("descriptions",[])
                          if d.get("lang")=="en"), "")
            # CVSS score (try v3.1 → v3.0 → v2)
            score = ""
            for key in ("cvssMetricV31","cvssMetricV30","cvssMetricV2"):
                m = cve.get("metrics",{}).get(key,[])
                if m: score = str(m[0].get("cvssData",{}).get("baseScore","")); break
            pub   = cve.get("published","")[:10]
            refs  = [r2.get("url","") for r2 in cve.get("references",[])[:4] if r2.get("url")]
            snippet = f"CVSS: {score} · Published: {pub}\n{desc[:300]}"
            if refs: snippet += f"\nRefs: {' | '.join(refs[:2])}"
            cits.append(Citation(
                id=str(uuid.uuid4())[:8],
                url=f"https://nvd.nist.gov/vuln/detail/{cve_id}",
                title=cve_id, snippet=snippet,
                source_type="structured", domain="nvd.nist.gov",
                full_text=desc + ("\n\nReferences:\n" + "\n".join(refs) if refs else ""),
                tags=["security","cve","structured","authoritative"],
            ))
        log.info("NVD returned %d CVEs for %r", len(cits), keyword)
        return cits
    except Exception as e:
        log.debug("NVD gather failed: %s", e); return []

async def smart_gather(
    query: str, job: ResearchJob,
    fast: Optional[OllamaInstance] = None,
    directive: "Optional[ResearchDirective]" = None,
) -> tuple[list[Citation], str]:
    """
    Intelligent source gathering:
    1. Detect query intent with fast model
    2. Identify authoritative/structured sources to crawl
    3. Run standard web search + structured crawl + doc crawl in parallel
    4. Assemble a rich context preserving tables and structured data
    """
    jid = job.id

    await broadcast(jid, {"type": "step", "t": time.time(),
                            "label": "Analysing", "detail": "Detecting query intent…"})

    # Get fast model for intent detection
    if not fast:
        fast = await get_instance(ModelTier.WRITER) or await get_instance(ModelTier.THINKER)

    # ── Step 1: Intent detection ──────────────────────────────────────────
    intent_data: dict = {}
    if fast:
        intent_data = await _detect_intent(query, fast, jid)
    else:
        intent_data = {"intent":"general","data_targets":[],"seed_urls":[],
                       "keywords":query.split()[:6],"structured":False}

    intent        = intent_data.get("intent", "general")
    data_targets  = intent_data.get("data_targets", [])
    seed_urls     = intent_data.get("seed_urls", [])
    structured    = intent_data.get("structured", False)

    await broadcast(jid, {"type": "intent",
                            "intent": intent,
                            "targets": data_targets[:4],
                            "structured": structured})
    await broadcast(jid, {"type": "step", "t": time.time(),
                            "label": "Intent", "detail": f"{intent} · {len(data_targets)} targets"})

    # ── Step 2: Add doc targets from pattern matching ─────────────────────
    doc_urls: list[str] = list(seed_urls)
    for pattern, doc_url in _DOC_PATTERNS:
        if pattern.search(query):
            if doc_url not in doc_urls:
                doc_urls.append(doc_url)
                await broadcast(jid, {"type": "step", "t": time.time(),
                                       "label": "Docs", "detail": f"Found: {doc_url[:50]}"})

    # ── Step 3: Run all gathering in parallel ─────────────────────────────
    tasks: list = []
    task_labels: list[str] = []

    # Extract custom per-source queries produced by the LLM
    sq          = intent_data.get("source_queries", {})
    web_q       = (sq.get("web")      or query).strip()
    news_q      = (sq.get("news")     or query).strip()
    nvd_term    = (sq.get("nvd")      or "").strip()
    site_qs     = [s.strip() for s in sq.get("site_specific", []) if s.strip()]

    if web_q != query:
        await broadcast(jid, {"type":"step","t":time.time(),
            "label":"Custom query","detail":f"Web: {web_q[:60]}"})

    # Always run standard web search (using custom web query if available)
    tasks.append(asyncio.create_task(gather_all_sources(web_q, job, intent=intent)))
    task_labels.append("web_search")

    # News-specific search angle (runs alongside web search)
    if news_q and news_q != web_q:
        tasks.append(asyncio.create_task(gather_web_search(news_q, jid)))
        task_labels.append("news_search")

    # Site-specific searches from intent
    for sq_item in site_qs[:2]:
        tasks.append(asyncio.create_task(gather_web_search(sq_item, jid)))
        task_labels.append(f"site:{sq_item[:30]}")

    # NVD/CVE search for security queries
    if nvd_term:
        tasks.append(asyncio.create_task(_gather_nvd(nvd_term, jid)))
        task_labels.append(f"nvd:{nvd_term[:30]}")

    # Structured/seed URLs — fetch each directly
    for url in seed_urls[:4]:
        tasks.append(asyncio.create_task(_fetch_structured_url(url, jid)))
        task_labels.append(f"structured:{url[:40]}")

    # Documentation crawl for each target
    for doc_url in doc_urls[:2]:
        tasks.append(asyncio.create_task(
            _doc_site_crawl(doc_url, query, jid, max_pages=15)))
        task_labels.append(f"docs:{doc_url[:40]}")

    # data_targets from intent detection — doc crawl
    for target in data_targets[:2]:
        # target could be a URL or a domain hint
        if not target.startswith("http"):
            target = "https://" + target
        tasks.append(asyncio.create_task(
            _doc_site_crawl(target, query, jid, max_pages=10)))
        task_labels.append(f"target:{target[:40]}")

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # ── Step 4: Assemble citations ────────────────────────────────────────
    # Determine if this intent justifies academic sources
    _is_academic_intent = intent in _ACADEMIC_INTENTS
    _has_academic_q = any(t in query.lower() for t in _ACADEMIC_QUERY_TERMS)
    _allow_arxiv = _is_academic_intent or _has_academic_q
    all_cits: list[Citation] = []
    seen_urls: set[str] = set()

    for label, res in zip(task_labels, results):
        if isinstance(res, Exception):
            log.warning("smart_gather task %s failed: %s", label, res)
            continue
        if label in ("web_search",) or label.startswith(("news_search","site:")):
            # gather_all_sources returns tuple; gather_web_search returns list
            web_cits = res[0] if isinstance(res, tuple) else res
            for c in (web_cits or []):
                if c.url not in seen_urls:
                    # Suppress stray arxiv results from web searches unless academic
                    if "arxiv.org" in (c.url or "") and not _allow_arxiv:
                        log.debug("smart_gather: suppressing stray arXiv URL %s", c.url[:60])
                        continue
                    all_cits.append(c); seen_urls.add(c.url)
        elif isinstance(res, list):   # doc crawl / NVD returns list
            for c in res:
                if c and c.url not in seen_urls:
                    all_cits.append(c); seen_urls.add(c.url)
        elif isinstance(res, Citation) and res:  # single URL fetch
            if res.url not in seen_urls:
                all_cits.append(res); seen_urls.add(res.url)

    # Extend job citations
    for c in all_cits:
        if c.url not in {x.url for x in job.citations}:
            job.citations.append(c)

    # ── Rank and tag all citations ────────────────────────────────────────
    all_cits = _rank_citations(all_cits, query, intent)

    # ── Extract images from crawled pages (for news/media intents) ────────
    if intent_data.get("needs_images") or intent in ("news_media", "general"):
        for c in all_cits[:8]:
            if c.full_text and not c.image_urls:
                try:
                    imgs = _extract_images_from_html(c.full_text, c.url)
                    if imgs:
                        c.image_urls = imgs
                        if "images_found" not in c.tags:
                            c.tags.append("images_found")
                except Exception:
                    pass

    # ── OSINT gather for company/person queries ───────────────────────────
    osint_targets = intent_data.get("osint_targets", [])
    if intent == "osint" and osint_targets and fast:
        for entity in osint_targets[:2]:
            try:
                osint_cits = await _osint_gather(entity, job, fast)
                for c in osint_cits:
                    if c.url not in seen_urls:
                        all_cits.append(c); seen_urls.add(c.url)
            except Exception as e:
                log.debug("OSINT for %s failed: %s", entity, e)
        all_cits = _rank_citations(all_cits, query, intent)  # re-rank with OSINT

    await broadcast(jid, {"type": "citations",
                            "citations": [c.to_dict() for c in all_cits]})
    await broadcast(jid, {"type": "step", "t": time.time(),
                            "label": "Sources",
                            "detail": f"{len(all_cits)} gathered · top: {all_cits[0].domain if all_cits else '—'}"})

    if not all_cits:
        return [], ""

    # ── Step 4b: Apply directive source_priority boost/demote ────────────
    # Boost sources that match directive source_priority,
    # demote arxiv.org for non-academic queries regardless of domain authority
    if directive and directive.valid and directive.source_priority:
        _prio_terms = [p.lower() for p in directive.source_priority]
        def _source_boost(c: Citation) -> float:
            domain = (c.domain or "").lower()
            stype  = (c.source_type or "").lower()
            if "arxiv.org" in domain and not _allow_arxiv:
                return -10.0  # push to bottom
            for pterm in _prio_terms:
                if pterm in domain or pterm in stype:
                    return 2.0
            return 0.0
        all_cits.sort(key=lambda c: _source_boost(c), reverse=True)
    elif not _allow_arxiv:
        # No directive, but still demote stray arxiv
        all_cits.sort(key=lambda c: (-10.0 if "arxiv.org" in (c.domain or "") else 0.0),
                      reverse=True)

    # ── Step 5: Build rich context string ────────────────────────────────
    # Sorted by rank: structured first, then by rank_score
    structured_cits = [c for c in all_cits if c.source_type in ("structured","documentation")]
    other_cits      = [c for c in all_cits if c.source_type not in ("structured","documentation")]

    ctx_parts: list[str] = ["## Research Sources\n"]
    char_budget = 18000

    for i, c in enumerate(structured_cits + other_cits, 1):
        if char_budget <= 0: break
        header = f"\n[{i}] **{c.title}** ({c.domain}) — *{c.source_type}*\n"
        ctx_parts.append(header)
        char_budget -= len(header)
        if c.full_text:
            chunk = c.full_text[:min(char_budget, 2000 if c.source_type=="documentation" else 800)]
            ctx_parts.append(chunk)
            char_budget -= len(chunk)
        elif c.snippet:
            chunk = f"    > {c.snippet[:280]}\n"
            ctx_parts.append(chunk)
            char_budget -= len(chunk)
        ctx_parts.append(f"    URL: {c.url}\n")
        if c.image_urls:
            ctx_parts.append(f"    Images: {' | '.join(c.image_urls[:3])}\n")
        if c.tags:
            ctx_parts.append(f"    Tags: {', '.join(c.tags)}\n")

    return all_cits, "\n".join(ctx_parts)


# ══════════════════════════════════════════════════════════════════════════════
#  FABRIC + MEMORY SOURCES
# ══════════════════════════════════════════════════════════════════════════════
# These query Vera's internal data stores — the data fabric (SQLite + FAISS +
# Chroma + PG fusion search) and the memory graph (session history, cap traces,
# conversation nodes). Results are converted to Citation objects so they
# integrate seamlessly with the existing ranking and deduplication pipeline.

async def gather_fabric(query: str, top_k: int = 30) -> list[Citation]:
    """Query the data fabric via fabric.query and convert results to Citations."""
    try:
        fabric = sys.modules.get("data_fabric")
        if not fabric or not hasattr(fabric, "execute_query"):
            log.debug("gather_fabric: data_fabric module not loaded")
            return []

        res = await fabric.execute_query({
            "text": query, "vector": query,
            "top_k": top_k, "include_data": True, "cache": False,
        })
        results = res.get("results", []) if isinstance(res, dict) else []
        if not results:
            return []

        cits: list[Citation] = []
        for r in results:
            data = r.get("data", {}) if isinstance(r.get("data"), dict) else {}
            text = r.get("text", "") or data.get("text", "") or data.get("content", "")
            title = (data.get("title", "") or data.get("name", "")
                     or data.get("cap_name", "") or r.get("dataset_id", ""))
            url = data.get("url", "") or data.get("source", "") or ""
            full_text = data.get("full_text", "") or data.get("result", "") or text

            if not text and not full_text:
                continue

            cits.append(Citation(
                id=str(uuid.uuid4())[:8],
                url=url or f"fabric://{r.get('dataset_id', 'unknown')}/{r.get('id', '')}",
                title=f"[Fabric] {title}" if title else f"[Fabric] {r.get('dataset_id', 'record')}",
                snippet=text[:400],
                full_text=full_text[:8000],
                source_type="fabric",
                domain=f"fabric:{r.get('dataset_id', '')}",
                fetched_at=time.time(),
            ))

        log.info("gather_fabric: %d results from %s backends",
                 len(cits), res.get("backends", []))
        return cits

    except Exception as e:
        log.warning("gather_fabric: %s", e)
        return []


async def gather_memory(query: str, top_k: int = 20) -> list[Citation]:
    """Query the memory graph via memory.search and convert results to Citations."""
    try:
        memory = sys.modules.get("memory")
        if not memory:
            log.debug("gather_memory: memory module not loaded")
            return []

        MEMORY = getattr(memory, "MEMORY", None)
        if not MEMORY or not hasattr(MEMORY, "search"):
            return []

        results = await MEMORY.search(query, limit=top_k)
        if not results:
            return []

        # Results are MemoryRecord objects or dicts
        cits: list[Citation] = []
        for r in results:
            if hasattr(r, "text"):
                text = r.text or ""
                full_text = getattr(r, "full_text", "") or text
                category = getattr(r, "category", "memory")
                session_id = getattr(r, "session_id", "")
                node_id = getattr(r, "id", "")
                tags = getattr(r, "tags", [])
                created = getattr(r, "created_at", "")
            elif isinstance(r, dict):
                text = r.get("text", "")
                full_text = r.get("full_text", "") or text
                category = r.get("category", "memory")
                session_id = r.get("session_id", "")
                node_id = r.get("id", "")
                tags = r.get("tags", [])
                created = r.get("created_at", "")
            else:
                continue

            if not text and not full_text:
                continue

            title_parts = [f"[Memory:{category}]"]
            if tags and isinstance(tags, list):
                title_parts.append(" ".join(tags[:3]))

            cits.append(Citation(
                id=str(uuid.uuid4())[:8],
                url=f"memory://{session_id}/{node_id}" if session_id else f"memory://{node_id}",
                title=" ".join(title_parts),
                snippet=text[:400],
                full_text=full_text[:8000],
                source_type="memory",
                domain="memory",
                fetched_at=time.time(),
            ))

        log.info("gather_memory: %d results", len(cits))
        return cits

    except Exception as e:
        log.warning("gather_memory: %s", e)
        return []


async def gather_all_sources(
    query: str,
    job: ResearchJob,
    intent: str = "",
) -> tuple[list[Citation], str]:
    # Normalise source IDs — strip stray JSON brackets/quotes that leak in
    # if sources arrived as an unparsed JSON array string.
    _raw_sources = job.sources if isinstance(job.sources, list) else []
    active = set()
    for s in _raw_sources:
        clean = str(s).strip().strip('[]"\' ')
        if clean:
            active.add(clean)
    task_map: dict[str, asyncio.Task] = {}

    log.info("gather_all_sources: job.sources=%s, active=%s", job.sources[:8], active)

    # Check both by explicit source ID (chips) AND by type (for user-added sources)
    def _src_active(ids: set, types: set = set()) -> bool:
        """True if any matching source ID is in active, or any enabled source of matching type."""
        if active & ids:
            return True
        if types:
            return any(s.type.value in types and s.enabled and s.id in active for s in sources)
        return False

    if _src_active({"searxng","brave","crawl4ai"}, {"web_search","web_crawl"}):
        task_map["web"] = asyncio.create_task(gather_web_search(query, job.id))
    if _src_active({"arxiv"}, set()):   # arXiv: only fire if explicitly enabled
        task_map["arxiv"] = asyncio.create_task(gather_arxiv(query, intent=intent))
    if _src_active({"hackernews"}, {"news"}):
        task_map["hn"] = asyncio.create_task(gather_hackernews(query))
    if _src_active({"redis"}, {"redis"}):
        task_map["redis"] = asyncio.create_task(query_redis(query))
    if _src_active({"neo4j"}, {"neo4j"}):
        task_map["neo4j"] = asyncio.create_task(query_neo4j(query))
    if _src_active({"chroma"}, {"chroma"}):
        task_map["chroma"] = asyncio.create_task(query_chroma(query))
    if _src_active({"github"}, {"github"}):
        task_map["github"] = asyncio.create_task(gather_github(query))
    if _src_active({"wayback","commoncrawl"}, {"web_archive"}):
        task_map["archive"] = asyncio.create_task(gather_archive(query, active))

    # Vera internal data stores
    if _src_active({"fabric"}, {"fabric"}):
        _fab_cfg = next((s.config for s in sources if s.id == "fabric"), {})
        _fab_k = int(_fab_cfg.get("top_k", 30)) if isinstance(_fab_cfg, dict) else 30
        task_map["fabric"] = asyncio.create_task(gather_fabric(query, top_k=_fab_k))
    if _src_active({"memory"}, {"memory"}):
        _mem_cfg = next((s.config for s in sources if s.id == "memory"), {})
        _mem_k = int(_mem_cfg.get("top_k", 20)) if isinstance(_mem_cfg, dict) else 20
        task_map["memory"] = asyncio.create_task(gather_memory(query, top_k=_mem_k))

    all_cits: list[Citation] = []
    if task_map:
        results = await asyncio.gather(*task_map.values(), return_exceptions=True)
        for name, res in zip(task_map.keys(), results):
            if isinstance(res, list): all_cits.extend(res)
            else: log.warning("Source %s failed: %s", name, res)

    # Deduplicate before extending job.citations
    _existing_urls = {c.url for c in job.citations}
    for c in all_cits:
        if c.url and c.url not in _existing_urls:
            job.citations.append(c)
            _existing_urls.add(c.url)

    if not all_cits:
        return all_cits, ""

    ctx_lines = ["## Retrieved Sources\n"]
    for i, c in enumerate(all_cits, 1):
        ctx_lines.append(f"[{i}] **{c.title}** ({c.domain})")
        if c.snippet: ctx_lines.append(f"    > {c.snippet[:280]}")
        if c.full_text: ctx_lines.append(f"    [crawled {len(c.full_text)} chars]\n    {c.full_text[:600]}")
        ctx_lines.append(f"    URL: {c.url}\n")

    return all_cits, "\n".join(ctx_lines)


# ══════════════════════════════════════════════════════════════════════════════
#  Ollama helpers
# ══════════════════════════════════════════════════════════════════════════════

async def get_instance(tier: ModelTier) -> Optional[OllamaInstance]:
    for inst in instances:
        if inst.tier == tier and inst.enabled: return inst
    for inst in instances:
        if inst.enabled: return inst
    return None


async def stream_ollama(
    inst: OllamaInstance,
    prompt: str,
    system: str = "",
    job_id: str = "",
    slot: Optional[AgentSlot] = None,
    timeout_secs: float = 300.0,
) -> AsyncIterator[str]:
    """
    Stream tokens from Ollama.

    Thinking-model behaviour (inst.enable_thinking=True):
      - Sets "think": true in the request payload.
      - Tokens arriving while Ollama is inside a <think> block are broadcast
        as {"type":"thinking"} and NOT yielded — they never enter the result
        buffer.  The UI renders them in a separate collapsible panel.
      - Tokens after </think> resume normal yielding.
      - Falls back gracefully: if the model doesn't emit think tags the
        tokens flow through normally.

    Thinking-model timeout:
      - timeout_secs is automatically scaled via _effective_timeout() so
        callers don't need to know whether thinking is enabled.
    """
    eff_timeout = _effective_timeout(inst, timeout_secs)
    payload = {
        "model":  inst.model,
        "prompt": prompt,
        "system": system,
        "stream": True,
        "think":  inst.enable_thinking,   # Ollama ≥0.7
        "options": {"num_ctx": inst.ctx_size},
    }
    to = httpx.Timeout(connect=5.0, read=eff_timeout, write=30.0, pool=5.0)

    # State for inline <think>...</think> detection when the model emits them
    # inside the "response" field (older Ollama versions that ignore "think":false)
    _thinking_inline = False     # True while inside an unclosed <think> block
    _think_buf: list[str] = []   # accumulates thinking text for broadcast

    async def _flush_think():
        if _think_buf and job_id:
            await broadcast(job_id, {
                "type": "thinking",
                "text": "".join(_think_buf),
                "tier": inst.tier,
            })
            _think_buf.clear()

    async with httpx.AsyncClient(timeout=to) as client:
        try:
            async with client.stream("POST", inst.generate_url, json=payload) as resp:
                resp.raise_for_status()
                async for raw in resp.aiter_lines():
                    if cancel_flags.get(job_id): break
                    if not raw: continue
                    try: chunk = json.loads(raw)
                    except json.JSONDecodeError: continue

                    # Newer Ollama: thinking comes through a dedicated field
                    think_tok = chunk.get("thinking", "")
                    if think_tok and job_id:
                        await broadcast(job_id, {
                            "type": "thinking",
                            "text": think_tok,
                            "tier": inst.tier,
                        })
                        if slot: slot.tokens += 1

                    tok = chunk.get("response", "")
                    if tok:
                        if slot: slot.tokens += 1

                        # Detect inline <think> tags (older Ollama / non-thinking models
                        # that still emit reasoning before the answer)
                        if inst.enable_thinking:
                            # Process tok char by char to split think vs answer
                            remaining = tok
                            while remaining:
                                if _thinking_inline:
                                    end = remaining.lower().find("</think>")
                                    if end == -1:
                                        _think_buf.append(remaining)
                                        remaining = ""
                                    else:
                                        _think_buf.append(remaining[:end])
                                        await _flush_think()
                                        _thinking_inline = False
                                        remaining = remaining[end + len("</think>"):]
                                else:
                                    start = remaining.lower().find("<think>")
                                    if start == -1:
                                        # Normal output — yield it
                                        if remaining:
                                            await broadcast(job_id, {
                                                "type": "token",
                                                "text": remaining,
                                                "tier": inst.tier,
                                            })
                                            yield remaining
                                        remaining = ""
                                    else:
                                        # Normal text before <think>
                                        before = remaining[:start]
                                        if before:
                                            await broadcast(job_id, {
                                                "type": "token",
                                                "text": before,
                                                "tier": inst.tier,
                                            })
                                            yield before
                                        _thinking_inline = True
                                        remaining = remaining[start + len("<think>"):]
                        else:
                            await broadcast(job_id, {"type":"token","text":tok,"tier":inst.tier})
                            yield tok

                    if chunk.get("done"):
                        await _flush_think()
                        break

        except httpx.ConnectError as e:
            err = f"\n\n⚠ Cannot reach {inst.name} at {inst.base_url}: {e}"
            await broadcast(job_id, {"type":"error","text":err}); yield err
        except (httpx.ReadTimeout, asyncio.TimeoutError):
            err = f"\n\n⚠ {inst.name} timed out after {eff_timeout:.0f}s"
            await broadcast(job_id, {"type":"error","text":err}); yield err
        except Exception as e:
            err = f"\n\n⚠ {inst.name}: {e}"
            await broadcast(job_id, {"type":"error","text":err}); yield err


async def collect_ollama(
    inst: OllamaInstance,
    prompt: str,
    system: str = "",
    job_id: str = "",
    slot: Optional[AgentSlot] = None,
    timeout_secs: float = 300.0,
) -> str:
    """
    Non-streaming helper for internal pipeline steps.
    Thinking tokens are routed to the UI via broadcast() inside stream_ollama
    and never included in the returned text, so callers always get clean output.
    """
    t0 = time.time()
    req_id = str(uuid.uuid4())[:12]

    # Emit ollama.request event so it shows in the Workers panel jobs queue
    if _VERA_MODE:
        try:
            await _vera_emit({
                "type":          "ollama.request",
                "req_id":        req_id,
                "model":         inst.model,
                "instance_id":   inst.name,
                "instance_url":  inst.base_url,
                "caller_file":   "researcher_api.py",
                "caller_func":   "collect_ollama",
                "caller_module": "researcher_api",
                "cap_name":      "",
                "prompt_preview": (prompt or "")[:120].replace("\n", " "),
                "json_mode":     False,
                "prefer_gpu":    False,
                "streaming":     True,
                "job_id":        job_id,
            })
        except Exception:
            pass

    parts: list[str] = []
    async for tok in stream_ollama(inst, prompt, system, job_id, slot, timeout_secs):
        parts.append(tok)
        if cancel_flags.get(job_id): break
    result = "".join(parts)
    # Belt-and-braces: strip any residual <think>...</think> blocks that
    # slipped through (e.g. model that ignores the think:false flag)
    result = re.sub(r"<think>[\s\S]*?</think>", "", result, flags=re.IGNORECASE).strip()

    # Emit ollama.request_done so the jobs queue shows completion
    elapsed = round(time.time() - t0, 2)
    if _VERA_MODE:
        try:
            await _vera_emit({
                "type":          "ollama.request_done",
                "req_id":        req_id,
                "model":         inst.model,
                "instance_id":   inst.name,
                "caller_file":   "researcher_api.py",
                "caller_func":   "collect_ollama",
                "elapsed_s":     elapsed,
                "token_count":   len(parts),
                "job_id":        job_id,
            })
        except Exception:
            pass

    return result


async def list_models(inst: OllamaInstance) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(inst.tags_url)
            return [m["name"] for m in r.json().get("models",[])]
    except Exception: return []


# ══════════════════════════════════════════════════════════════════════════════
#  Broadcast
# ══════════════════════════════════════════════════════════════════════════════

async def broadcast(job_id: str, payload: dict):
    # Vera event system — picked up by harness WS, cap tracking, dream sensors
    if _VERA_MODE:
        try:
            ev = {
                **payload,
                "job_id": job_id,
                "type": f"research.{payload.get('type', 'update')}",
            }
            # Normalize: ensure research.error events always carry an 'error' key
            # (the researcher emits {type:"error", text:"..."} but consumers
            # look for ev.error)
            if payload.get("type") == "error" and "error" not in ev:
                ev["error"] = payload.get("text") or payload.get("message") or "unknown error"
            await _vera_emit(ev)
        except Exception:
            pass
    # Direct WS to research panel iframe (needed for streaming tokens)
    msg = json.dumps(payload)
    for ws in list(ws_clients.get(job_id, [])):
        try: await ws.send_text(msg)
        except Exception:
            try: ws_clients[job_id].remove(ws)
            except ValueError: pass


# ══════════════════════════════════════════════════════════════════════════════
#  File-tree output
# ══════════════════════════════════════════════════════════════════════════════

def parse_file_tree(raw: str) -> dict[str, str]:
    """
    Parse LLM output that looks like:
        === FILE: path/to/file.ext ===
        <content>
        === END ===
    Returns {path: content}
    """
    files: dict[str, str] = {}
    pattern = re.compile(r"===\s*FILE:\s*([^\s=]+)\s*===\s*(.*?)===\s*END\s*===", re.S)
    for m in pattern.finditer(raw):
        path    = m.group(1).strip()
        content = m.group(2).strip()
        files[path] = content

    # Also handle markdown fenced blocks with filenames
    # ```python  # path/to/file.py
    md_pattern = re.compile(r"```[a-z]*\s*#\s*([^\n]+)\n(.*?)```", re.S)
    for m in md_pattern.finditer(raw):
        path = m.group(1).strip()
        content = m.group(2).strip()
        if "/" in path or "." in path:
            files[path] = content

    return files


async def materialise_file_tree(job: ResearchJob, project: Optional[Project] = None):
    """Write parsed file tree to disk under projects/<id>/files/"""
    tree = job.file_tree
    if not tree: return

    if project:
        base = PROJECTS_DIR / project.id / "files"
    else:
        base = PROJECTS_DIR / "standalone" / job.id / "files"
    base.mkdir(parents=True, exist_ok=True)

    # Also write source crawl content as _sources/ files if available
    if hasattr(job, "citations") and job.citations:
        for i, cit in enumerate(job.citations[:10]):
            if cit.full_text and len(cit.full_text) > 200:
                from urllib.parse import urlparse
                domain = urlparse(cit.url).netloc.replace(".", "_")
                src_path = base / "_sources" / f"{i+1}_{domain}.txt"
                src_path.parent.mkdir(parents=True, exist_ok=True)
                src_path.write_text(
                    f"Source: {cit.url}\nTitle: {cit.title}\n\n{cit.full_text}",
                    encoding="utf-8"
                )
                # Track in file_tree
                rel = str(src_path.relative_to(base))
                if rel not in tree:
                    tree[f"_sources/{i+1}_{domain}.txt"] = cit.full_text[:500]
                await broadcast(job.id, {"type":"file_created",
                                          "path":f"_sources/{i+1}_{domain}.txt"})

    for rel_path, content in tree.items():
        # Sanitise path
        safe = Path(rel_path)
        if safe.is_absolute(): safe = Path(*safe.parts[1:])
        target = base / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    await broadcast(job.id, {"type":"file_tree","files":list(tree.keys()),"base":str(base)})


# ══════════════════════════════════════════════════════════════════════════════
#  Project / Content-Base context
# ══════════════════════════════════════════════════════════════════════════════

async def update_project_context(project: Project, job: ResearchJob, thinker: Optional[OllamaInstance]):
    """After each round, update the rolling context summary and file tree."""
    project.rounds.append(ProjectRound(
        id=str(uuid.uuid4())[:8],
        job_id=job.id,
        round_num=len(project.rounds)+1,
        query=job.query,
        result=job.result or "",
        citations=[c.to_dict_full() for c in job.citations],
    ))
    if job.file_tree:
        project.file_tree.update(job.file_tree)

    # Summarise context with thinker if available
    if thinker and len(project.rounds) > 1:
        existing = project.context_summary
        new_content = (job.result or "")[:3000]
        summary_prompt = (
            f"Existing project context summary:\n{existing}\n\n"
            f"New round (query: {job.query}):\n{new_content}\n\n"
            "Update the summary to incorporate the new round. "
            "Keep it under 800 words. Focus on: what has been covered, "
            "key facts established, files created, open questions."
        )
        summary_sys = "You are a research project manager. Maintain a concise rolling summary."
        try:
            project.context_summary = await asyncio.wait_for(
                collect_ollama(thinker, summary_prompt, summary_sys, timeout_secs=SUMMARY_TIMEOUT),
                timeout=SUMMARY_TIMEOUT + 10
            )
        except asyncio.TimeoutError:
            project.context_summary = existing + f"\n\n[Round {len(project.rounds)}]: {job.query}"
    elif not project.context_summary:
        project.context_summary = (
            f"Project: {project.name}\n"
            f"Round 1 query: {job.query}\n"
            f"Summary: {(job.result or '')[:600]}"
        )

    project.updated_at = time.time()
    # Persist to disk
    proj_file = PROJECTS_DIR / project.id / "project.json"
    proj_file.parent.mkdir(exist_ok=True)
    proj_data = {
        **project.to_dict(),
        "context_summary": project.context_summary,
        "file_tree_keys": list(project.file_tree.keys()),
        "rounds": [{"id":r.id,"round_num":r.round_num,"query":r.query,
                    "created_at":r.created_at} for r in project.rounds],
    }
    proj_file.write_text(json.dumps(proj_data, indent=2), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
#  Pipeline helpers
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
#  Research Directive — thinker-produced before any search or writing starts.
#  Shapes output style, scope, writer system prompt, and sub-question strategy.
# ══════════════════════════════════════════════════════════════════════════════

_DIRECTIVE_PROMPT = """You are a research director. Analyse this query and decide the best research strategy.

Query: {query}
{context_hint}

Return a JSON object with EXACTLY these fields:

{{
  "output_style": one of:
    "report"        — structured analysis with ## sections, citations throughout
    "newspaper"     — journalistic, lead paragraph + sections, recent events focus
    "guide"         — step-by-step how-to, numbered instructions, code/examples
    "tables"        — data-heavy, markdown tables as primary structure
    "briefing"      — executive summary style, bullets, key facts up front
    "deep_analysis" — long-form, academic tone, multiple perspectives
    "comparison"    — side-by-side comparison of options/products/approaches,

  "scope_focus": 1-2 sentence description of EXACTLY what this research should cover,

  "scope_exclude": list of 2-4 strings — topics to NOT include even if tangentially related,

  "key_questions": list of 3-5 specific questions this research MUST answer,

  "table_topics": list of topics that should be presented as markdown tables (empty if none),

  "needs_recency": true if the query is time-sensitive (news, prices, versions, events),

  "depth": one of "shallow" | "standard" | "deep" — how thorough the research should be,

  "sub_questions": list of 3-5 focused sub-questions for parallel research
    (make these specific, non-overlapping, each covering a distinct angle),

  "writer_instructions": 2-3 sentences of specific instructions for the writer model —
    what tone, what to emphasise, what format elements to use, what to avoid,

  "source_priority": list of 2-4 source types to prioritise, e.g.
    ["official docs", "primary sources", "recent news", "academic papers",
     "wikis", "forums", "government data", "financial filings", "CVE databases"],
  "nlp_tools": list of 2-5 NLP tools to run (always include "entities" and "gaps"):
    "entities" "key_quotes" "contradictions" "timeline" "sentiment" "clusters" "anomalies" "gaps"
}}

Style guide:
- "apple inc" → report, deep_analysis: cover company broadly, financials, products, strategy
- "best pokemon teams" → tables, guide: team compositions as tables, mechanics guide
- "latest AI/ML news" → newspaper: journalistic, recent events, no historical background
- "CVE-2024-1234" → briefing, tables: CVSS table, affected versions, patch status
- "how to deploy kubernetes" → guide: numbered steps, code blocks, commands
- "nvidia vs amd gpu comparison" → comparison, tables: specs side-by-side
- "SEC filing analysis" → deep_analysis, tables: financial data, structured
- Short vague queries → standard depth, report style
- Queries with "latest/recent/today/this week" → needs_recency=true, newspaper or briefing

Return ONLY the JSON object. No explanation. No markdown fences.
"""


@dataclass
class ResearchDirective:
    """Produced by the thinker at job start — shapes all downstream prompts."""
    output_style:        str   = "report"
    scope_focus:         str   = ""
    scope_exclude:       list  = field(default_factory=list)
    key_questions:       list  = field(default_factory=list)
    table_topics:        list  = field(default_factory=list)
    needs_recency:       bool  = False
    depth:               str   = "standard"
    sub_questions:       list  = field(default_factory=list)
    writer_instructions: str   = ""
    source_priority:     list  = field(default_factory=list)
    # Derived: the writer system prompt built from this directive
    writer_sys:          str   = ""
    # NLP tools the analyst should run on this job (selected by thinker)
    nlp_tools:           list  = field(default_factory=list)
    # Whether the directive was successfully produced
    valid:               bool  = False

    def build_writer_sys(self) -> str:
        """Build a tailored writer system prompt from this directive."""
        style_instructions = {
            "report":       (
                "Write a well-structured research report with ## and ### section headers. "
                "Use citations [N] throughout. Include a ## Summary at the top."
            ),
            "newspaper":    (
                "Write in journalistic style. Open with a strong lead paragraph summarising "
                "the key development. Use short, punchy sections. Keep it current and factual. "
                "Prioritise recent events over background history."
            ),
            "guide":        (
                "Write a practical how-to guide. Use numbered steps, code blocks (```), "
                "and command examples. Be specific and actionable. No waffle."
            ),
            "tables":       (
                "Organise data into markdown tables wherever possible. "
                "Use | Col | Col | syntax. Only use prose for context and explanation."
            ),
            "briefing":     (
                "Write an executive briefing. Lead with a 3-5 bullet TL;DR, then key facts. "
                "Be concise — each section maximum 3 sentences. No padding."
            ),
            "deep_analysis":(
                "Write a comprehensive analytical report. Cover multiple perspectives and "
                "possible interpretations. Use ## sections, subsections, and cite all claims."
            ),
            "comparison":   (
                "Write a structured comparison. Use markdown tables for side-by-side specs. "
                "Include a Verdict or Recommendation section at the end."
            ),
        }
        base = style_instructions.get(self.output_style, style_instructions["report"])

        parts = [
            "You are a specialist research writer. " + base,
            "Always cite sources as [1], [2] etc.",
        ]

        if self.scope_focus:
            parts.append(f"Focus: {self.scope_focus}")

        if self.scope_exclude:
            parts.append(
                "Do NOT include: " + "; ".join(self.scope_exclude) + "."
            )

        if self.table_topics:
            parts.append(
                "Present the following as markdown tables: "
                + ", ".join(self.table_topics) + "."
            )

        if self.needs_recency:
            parts.append(
                "Prioritise the most recent information. "
                "Clearly note dates for all time-sensitive data."
            )

        if self.writer_instructions:
            parts.append(self.writer_instructions)

        return " ".join(parts)

    def extraction_prompt_suffix(self) -> str:
        """Appended to each sub-question extraction prompt in parallel mode."""
        parts = []
        if self.scope_focus:
            parts.append(f"Focus only on: {self.scope_focus}")
        if self.scope_exclude:
            parts.append("Exclude: " + "; ".join(self.scope_exclude[:2]))
        if self.output_style == "tables":
            parts.append("Format findings as bullet points or table rows.")
        if self.needs_recency:
            parts.append("Note the date of each finding.")
        return (" — " + " | ".join(parts)) if parts else ""

    def to_context_header(self) -> str:
        """Short header injected into writer prompts to reinforce directive."""
        lines = [f"## Research Directive\n"]
        lines.append(f"**Style**: {self.output_style}")
        if self.scope_focus:
            lines.append(f"**Focus**: {self.scope_focus}")
        if self.scope_exclude:
            lines.append(f"**Exclude**: {', '.join(self.scope_exclude[:3])}")
        if self.key_questions:
            lines.append("**Must answer**:")
            lines.extend(f"- {q}" for q in self.key_questions[:4])
        if self.table_topics:
            lines.append(f"**Tables required for**: {', '.join(self.table_topics[:3])}")
        return "\n".join(lines) + "\n"


async def build_research_directive(
    query: str,
    thinker: "Optional[OllamaInstance]",
    job_id: str,
    project: "Optional[Any]" = None,
    job: "Optional[ResearchJob]" = None,
) -> ResearchDirective:
    """
    Thinker analyses the query and returns a ResearchDirective.
    Falls back to a default directive if thinker is unavailable or times out.
    Runs as a fast collect — typically 5-15s on a CPU thinker.
    """
    d = ResearchDirective()

    if not thinker:
        # No thinker — infer basics from query text
        q_lower = query.lower()
        if any(w in q_lower for w in ("latest","news","today","this week","breaking")):
            d.output_style = "newspaper"; d.needs_recency = True
        elif any(w in q_lower for w in ("how to","tutorial","guide","setup","install","deploy")):
            d.output_style = "guide"
        elif any(w in q_lower for w in ("compare","vs","versus","best","top","ranked")):
            d.output_style = "comparison"
        elif any(w in q_lower for w in ("cve","vulnerability","exploit","cvss","patch")):
            d.output_style = "briefing"; d.needs_recency = True
        d.sub_questions = []
        d.writer_sys = d.build_writer_sys()
        d.valid = True
        return d

    context_hint = ""
    if project and getattr(project, "context_summary", ""):
        context_hint = f"Project context: {project.context_summary[:300]}"

    try:
        raw = await asyncio.wait_for(
            collect_ollama(
                thinker,
                _DIRECTIVE_PROMPT.format(query=query, context_hint=context_hint),
                "You are a research strategy director. Return only valid JSON.",
                job_id,
                timeout_secs=_effective_timeout(thinker, 600),
            ),
            timeout=_effective_timeout(thinker, 600),
        )
        start = raw.find("{"); end = raw.rfind("}") + 1
        if start < 0 or end <= start:
            raise ValueError("no JSON object in response")
        parsed = json.loads(raw[start:end])

        d.output_style        = parsed.get("output_style", "report")
        d.scope_focus         = str(parsed.get("scope_focus", ""))[:300]
        d.scope_exclude       = [str(x)[:80] for x in parsed.get("scope_exclude", [])[:4]]
        d.key_questions       = [str(x)[:150] for x in parsed.get("key_questions", [])[:5]]
        d.table_topics        = [str(x)[:80] for x in parsed.get("table_topics", [])[:4]]
        d.needs_recency       = bool(parsed.get("needs_recency", False))
        d.depth               = parsed.get("depth", "standard")
        d.sub_questions       = [str(x)[:200] for x in parsed.get("sub_questions", [])[:5]]
        d.writer_instructions = str(parsed.get("writer_instructions", ""))[:400]
        d.source_priority     = [str(x)[:60] for x in parsed.get("source_priority", [])[:4]]
        raw_nlp = [str(x) for x in parsed.get("nlp_tools", [])[:6]]
        d.nlp_tools = raw_nlp if raw_nlp else ["entities","gaps","key_quotes"]
        for _t in ("entities", "gaps"):
            if _t not in d.nlp_tools: d.nlp_tools.append(_t)
        d.valid               = True
        d.writer_sys          = d.build_writer_sys()

        log.info("ResearchDirective: style=%s depth=%s recency=%s questions=%d sub_qs=%d",
                 d.output_style, d.depth, d.needs_recency,
                 len(d.key_questions), len(d.sub_questions))

    except Exception as e:
        log.warning("build_research_directive failed (%s) — using defaults", e)
        d.writer_sys = d.build_writer_sys()
        d.valid = True   # always return a usable directive

    # ── Pipeline stage overrides ──────────────────────────────────────────
    # When this job is a pipeline stage, the stage can force specific NLP
    # tools and inject an extra writer instruction.
    if job is not None:
        if getattr(job, "pipeline_nlp_tools", None):
            d.nlp_tools = list(job.pipeline_nlp_tools)
            for _t in ("entities", "gaps"):
                if _t not in d.nlp_tools:
                    d.nlp_tools.append(_t)
        if getattr(job, "pipeline_writer_prompt", ""):
            d.writer_instructions = (
                (d.writer_instructions + " ") if d.writer_instructions else ""
            ) + job.pipeline_writer_prompt
            d.writer_sys = d.build_writer_sys()
        log.info("build_research_directive: applied pipeline overrides "
                 "(nlp_tools=%s, writer_prompt=%s)",
                 bool(getattr(job, "pipeline_nlp_tools", None)),
                 bool(getattr(job, "pipeline_writer_prompt", "")))

    return d

WRITE_SYS = (
    "You are a research writer. Produce clear, thorough, well-structured reports in Markdown. "
    "Use ## headers, bullet points, and **bold** for key terms. "
    "When sources are provided cite them inline as [1], [2] etc. "
    "End with a ## References section."
)

GUIDE_SYS = (
    "You are a technical writer producing comprehensive guides. "
    "Write in depth, using ## and ### headers, numbered steps, code blocks where relevant. "
    "This is one section of a larger guide — be thorough and complete for your assigned section. "
    "Cite sources inline as [1], [2] etc."
)

FILESTORE_SYS = (
    "You are a senior software engineer. Produce complete, working files. "
    "For EVERY file you create, wrap it exactly like this:\n\n"
    "=== FILE: path/to/filename.ext ===\n"
    "<complete file contents>\n"
    "=== END ===\n\n"
    "Include ALL files needed: configs, dockerfiles, scripts, READMEs, etc. "
    "Do not truncate or abbreviate. Every file must be production-ready."
)

# ── Coding mode system prompts ────────────────────────────────────────────────

CODE_ARCH_SYS = (
    "You are a software architect. Given a task and research context, produce a "
    "detailed architecture plan. Be precise and specific — no vague descriptions.\n\n"
    "Your output MUST be valid JSON matching this schema:\n"
    "{\n"
    '  "overview": "One paragraph describing what is being built",\n'
    '  "stack": ["tech1", "tech2"],\n'
    '  "files": [\n'
    '    {"path": "relative/path/file.ext", "purpose": "what this file does", '
    '"depends_on": ["other/file.ext"]}\n'
    "  ],\n"
    '  "interfaces": [\n'
    '    {"name": "InterfaceName", "description": "what it defines"}\n'
    "  ],\n"
    '  "implementation_order": ["file1.ext", "file2.ext"],\n'
    '  "notes": "Any important constraints or patterns to follow"\n'
    "}\n\n"
    "Respond with ONLY the JSON — no preamble, no markdown fences."
)

CODE_IMPL_SYS = (
    "You are a senior software engineer implementing specific files.\n\n"
    "Rules:\n"
    "- Write COMPLETE, working code — never use placeholders like '# TODO' or '...'\n"
    "- Every file must be immediately runnable/importable\n"
    "- Follow the architecture plan and interface definitions exactly\n"
    "- Be consistent with files already written\n"
    "- Wrap each file like this:\n\n"
    "=== FILE: path/to/file.ext ===\n"
    "<complete contents>\n"
    "=== END ===\n\n"
    "Write ONLY the file content — no explanations between files."
)

CODE_REVIEW_SYS = (
    "You are a code reviewer. Review the implemented files for:\n"
    "1. Correctness — will this actually run?\n"
    "2. Completeness — are all imports present, all functions implemented?\n"
    "3. Consistency — does it match the architecture and other files?\n"
    "4. Security — obvious vulnerabilities?\n\n"
    "Respond ONLY with valid JSON:\n"
    "{\n"
    '  "verdict": "pass" | "patch",\n'
    '  "issues": [\n'
    '    {"file": "path/to/file.ext", "line_hint": "approx location", '
    '"severity": "error"|"warning", "description": "what is wrong", '
    '"fix": "exact corrected code"}\n'
    "  ],\n"
    '  "summary": "brief overall assessment"\n'
    "}\n\n"
    "If verdict is 'pass', issues array must be empty.\n"
    "Respond with ONLY the JSON — no preamble."
)

CODE_CONTINUITY_SYS = (
    "You are a technical project manager. Summarise the current state of a "
    "coding project so a fresh context window can continue it coherently.\n\n"
    "Respond ONLY with valid JSON:\n"
    "{\n"
    '  "summary": "What has been built, key design decisions made",\n'
    '  "files_done": ["list of completed file paths"],\n'
    '  "files_pending": ["list of files still to implement"],\n'
    '  "key_interfaces": "Critical interfaces/types/contracts already defined",\n'
    '  "continuation_notes": "What the next run must know to continue correctly"\n'
    "}\n\n"
    "Respond with ONLY the JSON."
)


# ══════════════════════════════════════════════════════════════════════════════
#  Coding pipeline
# ══════════════════════════════════════════════════════════════════════════════

# How many tokens of written code to include in continuation context
CODE_CONTEXT_WINDOW  = 6000   # chars of recent code kept in prompt
# Max files per run before we emit a continuation signal
MAX_FILES_PER_RUN    = 8


def _recent_code_ctx(chain: ChainContext, chars: int = CODE_CONTEXT_WINDOW) -> str:
    """Return the tail of accumulated code for context injection."""
    if not chain.accumulated_code:
        return ""
    parts = []
    for path, content in list(chain.accumulated_code.items())[-6:]:
        parts.append(f"=== FILE: {path} ===\n{content[:800]}\n=== END ===")
    joined = "\n\n".join(parts)
    return joined[-chars:]


async def run_code_pipeline(job: ResearchJob, project: Optional[Project] = None) -> None:
    """
    Full coding pipeline.  Works for both first runs (chain_ctx is None)
    and continuation runs (chain_ctx already populated).

    Phases:
      1. Research  — gather sources (first run only, or if explicitly requested)
      2. Architect — thinker plans file tree + interfaces (first run only)
      3. Implement — writer generates files one at a time, analyst reviews each
      4. Continuity — thinker summarises state if files remain (emits chain signal)
    """
    thinker = await get_instance(ModelTier.THINKER)
    writer  = await get_instance(ModelTier.WRITER)
    analyst = await get_instance(ModelTier.ANALYST)

    if not writer and not thinker:
        job.status = JobStatus.ERROR
        job.error  = "No Ollama instance available"
        return

    use_writer = writer or thinker
    slot_t = slot_for(ModelTier.THINKER)
    slot_w = slot_for(ModelTier.WRITER)
    slot_a = slot_for(ModelTier.ANALYST)

    chain = job.chain_ctx

    # ── Phase 1: Research (first run only) ───────────────────────────────────
    # Code mode always does research — it searches for existing solutions,
    # relevant GitHub repos, docs, and examples BEFORE architecting.
    research_ctx = ""
    if chain is None or not chain.architecture:
        job.status = JobStatus.SEARCHING
        await step_emit(job, "Code research", "Finding existing solutions and references…")

        # 1a: Thinker decomposes the coding task into technical sub-questions
        use_thinker = thinker or writer
        decompose_prompt = (
            f"Coding task: {job.query}\n\n"
            "Produce 4-6 specific technical research questions that would help "
            "implement this correctly. Think about: existing libraries, "
            "similar implementations on GitHub, API documentation, "
            "common patterns and pitfalls, performance considerations.\n"
            "Return ONLY a JSON array of question strings."
        )
        tech_questions: list[str] = [job.query]
        try:
            raw_q = await asyncio.wait_for(
                collect_ollama(use_thinker, decompose_prompt,
                    "Return ONLY a JSON array of strings.", job.id,
                    timeout_secs=_effective_timeout(use_thinker, THINKER_PLAN_TIMEOUT)),
                timeout=_effective_timeout(use_thinker, THINKER_PLAN_TIMEOUT) + 10,
            )
            start = raw_q.find("["); end = raw_q.rfind("]") + 1
            if start >= 0 and end > start:
                parsed_q = json.loads(raw_q[start:end])
                tech_questions = [q for q in parsed_q if isinstance(q, str) and q.strip()]
                if not tech_questions:
                    tech_questions = [job.query]
            await step_emit(job, "Code research", f"{len(tech_questions)} technical questions identified")
        except Exception as e:
            log.debug("Tech question decompose failed: %s", e)

        # 1b: Always run GitHub search (force-enable for code mode)
        #     Even if the GitHub source chip is off, we attempt web-search fallback
        github_cits: list[Citation] = []
        try:
            # Temporarily enable GitHub for code mode if not already active
            gh_src = next((s for s in sources if s.id == "github" and s.enabled), None)
            if gh_src:
                github_cits = await gather_github(job.query, limit=10)
            else:
                # Web-search fallback: find relevant GitHub repos
                log.info("Code mode: GitHub source not enabled — using web fallback")
                gh_web = await _github_web_fallback(job.query, limit=8)
                for item in gh_web:
                    url = _clean_search_url(item.get("url", ""))
                    if url:
                        github_cits.append(Citation(
                            id=str(uuid.uuid4())[:8],
                            url=url,
                            title=item.get("title", url),
                            snippet=item.get("content", "")[:300],
                            source_type="github",
                        ))
            if github_cits:
                job.citations.extend(github_cits)
                await broadcast(job.id, {
                    "type": "citations",
                    "citations": [c.to_dict() for c in job.citations],
                })
                await step_emit(job, "GitHub", f"{len(github_cits)} repos/files found")
        except Exception as e:
            log.warning("Code mode GitHub search failed: %s", e)

        # 1c: Research each technical question in parallel (writer = fast)
        async def _research_one_question(q: str) -> str:
            class _FakeJob:
                id = job.id; sources = job.sources; citations: list = []
            try:
                fast_inst = await get_instance(ModelTier.WRITER) or use_thinker
                # Code mode: academic papers are relevant (code/technical intent)
                code_directive = ResearchDirective(
                    output_style="guide", source_priority=["official docs", "github"])
                _, ctx = await smart_gather(q, _FakeJob(), fast=fast_inst,  # type: ignore
                                            directive=code_directive)
                new_cits = _FakeJob.citations
                seen = {c.url for c in job.citations}
                for c in new_cits:
                    if c.url not in seen:
                        job.citations.append(c); seen.add(c.url)
                return ctx
            except Exception as e:
                log.debug("Tech question research failed: %s", e)
                return ""

        question_tasks = [_research_one_question(q) for q in tech_questions[:5]]
        question_ctxs = await asyncio.gather(*question_tasks, return_exceptions=True)

        # 1d: Build combined research context
        ctx_parts: list[str] = []
        if github_cits:
            gh_ctx = "\n".join(
                f"[GH] **{c.title}**\n{c.snippet[:200]}"
                + (f"\n{c.full_text[:600]}" if c.full_text else "")
                + f"\n{c.url}"
                for c in github_cits[:8]
            )
            ctx_parts.append(f"## GitHub References\n{gh_ctx}")

        for q, ctx in zip(tech_questions, question_ctxs):
            if isinstance(ctx, str) and ctx.strip():
                ctx_parts.append(f"## {q}\n{ctx[:2000]}")

        research_ctx = "\n\n".join(ctx_parts)
        if chain:
            chain.research_context = research_ctx

        if not research_ctx.strip():
            await step_emit(job, "Research", "No sources found — using model knowledge")
        else:
            await step_emit(job, "Research complete",
                f"{len(job.citations)} total sources · "
                f"{len(tech_questions)} questions answered")

        # Run analyst NLP on research citations before architecture phase
        # Gives thinker compact entity/fact view rather than raw text
        if job.citations:
            try:
                code_ar = await asyncio.wait_for(
                    run_analyst_engine(job.query, job.citations, job.id, None, None),
                    timeout=25.0
                )
                if code_ar.valid:
                    analyst_compact = code_ar.to_context_string()
                    if analyst_compact:
                        research_ctx = analyst_compact + "\n\n---\n\n" + research_ctx
                    await step_emit(job, "NLP Analysis",
                        f"{len(code_ar.knowledge_bullets)} key facts · "
                        f"{len(code_ar.entities)} entity types extracted")
            except Exception as e:
                log.debug("Code mode analyst NLP failed: %s", e)

    if chain:
        research_ctx = chain.research_context or research_ctx

    # ── Phase 2: Architecture (first run only) ───────────────────────────────
    arch_plan: dict = {}

    if chain is None or not chain.architecture:
        use_arch = thinker or writer
        slot_on(slot_t, use_arch, job.id, "thinking")
        job.status = JobStatus.ARCHITECTING
        await step_emit(job, "Architecting", f"{use_arch.name} designing system…")

        arch_prompt = (
            f"Task: {job.query}\n\n"
            f"Research context:\n{research_ctx[:4000]}\n\n"
            "Produce a complete architecture plan as specified. "
            "Think carefully about the full file structure needed."
        )

        arch_raw = await collect_ollama(
            use_arch, arch_prompt, CODE_ARCH_SYS, job.id, slot_t,
            timeout_secs=THINKER_PLAN_TIMEOUT
        )
        slot_off(slot_t)

        # Parse architecture JSON
        try:
            # Strip any accidental markdown fences
            clean = re.sub(r"^```[a-z]*\s*|\s*```$", "", arch_raw.strip(), flags=re.M)
            arch_plan = json.loads(clean)
        except Exception as e:
            log.warning("Architecture JSON parse failed: %s — attempting extraction", e)
            try:
                start = arch_raw.index("{")
                end   = arch_raw.rindex("}") + 1
                arch_plan = json.loads(arch_raw[start:end])
            except Exception:
                # Fallback: treat as free-form, extract files
                arch_plan = {
                    "overview": arch_raw[:500],
                    "files": [],
                    "implementation_order": [],
                    "notes": arch_raw,
                }

        # Extract ordered file list
        impl_order  = arch_plan.get("implementation_order", [])
        all_files   = [f["path"] for f in arch_plan.get("files", [])]
        # Merge: impl_order first, then any not listed
        ordered_files = impl_order + [f for f in all_files if f not in impl_order]

        if not ordered_files:
            # Fallback if arch returned nothing useful
            ordered_files = ["main.py", "README.md"]

        # Initialise chain context
        chain = ChainContext(
            chain_id      = str(uuid.uuid4())[:12],
            run_number    = 1,
            original_task = job.query,
            architecture  = json.dumps(arch_plan, indent=2),
            files_planned = ordered_files,
            files_done    = [],
            files_pending = list(ordered_files),
            research_context = research_ctx,
        )
        job.chain_ctx = chain

        # Broadcast architecture to frontend
        await broadcast(job.id, {
            "type":  "architecture",
            "plan":  arch_plan,
            "files": ordered_files,
            "chain_id": chain.chain_id,
        })
        await step_emit(job, "Architecture", f"{len(ordered_files)} files planned")

    else:
        # Continuation run — resume from where we left off
        await step_emit(job, "Continuing",
            f"Run {chain.run_number} · {len(chain.files_done)} done · {len(chain.files_pending)} remaining")

    if cancel_flags.get(job.id):
        return

    # ── Phase 3: Implement files ─────────────────────────────────────────────
    job.status = JobStatus.CODING
    files_this_run = 0
    all_output_parts: list[str] = []

    while chain.files_pending and files_this_run < MAX_FILES_PER_RUN:
        if cancel_flags.get(job.id):
            break

        target_file = chain.files_pending[0]
        await step_emit(job, f"Coding {files_this_run+1}", target_file)
        slot_on(slot_w, use_writer, job.id, "writing")

        # Build a focused context: arch plan + interfaces + recent code
        recent_ctx = _recent_code_ctx(chain)
        file_info  = next(
            (f for f in json.loads(chain.architecture).get("files",[])
             if f["path"] == target_file),
            {"path": target_file, "purpose": "", "depends_on": []}
        )
        # Include contents of files this one depends on
        deps_ctx = ""
        for dep in file_info.get("depends_on", [])[:3]:
            if dep in chain.accumulated_code:
                deps_ctx += f"\n--- {dep} (dependency) ---\n{chain.accumulated_code[dep][:600]}\n"

        impl_prompt = (
            f"Original task: {chain.original_task}\n\n"
            f"Architecture overview:\n{json.loads(chain.architecture).get('overview','')}\n"
            f"Architecture notes:\n{json.loads(chain.architecture).get('notes','')}\n\n"
            f"File to implement: {target_file}\n"
            f"Purpose: {file_info.get('purpose','')}\n"
            f"Depends on: {', '.join(file_info.get('depends_on',[]))}\n\n"
            f"Files already completed: {', '.join(chain.files_done) or 'none yet'}\n"
            f"Files still pending after this: {', '.join(chain.files_pending[1:MAX_FILES_PER_RUN])}\n\n"
            f"{deps_ctx}"
            f"Recent code written (for consistency):\n{recent_ctx}\n\n"
            f"Implement {target_file} completely and correctly."
            + (f"\n\nThinker notes for this file:\n{getattr(chain, '_next_file_hint', '')}"
               if getattr(chain, "_next_file_hint", "") else "")
        )

        impl_parts: list[str] = []
        async for tok in stream_ollama(
            use_writer, impl_prompt, CODE_IMPL_SYS, job.id, slot_w,
            timeout_secs=WRITER_TIMEOUT
        ):
            impl_parts.append(tok)
            if cancel_flags.get(job.id): break

        slot_off(slot_w)
        raw_impl = "".join(impl_parts)
        all_output_parts.append(raw_impl)

        # Parse and store file
        parsed = parse_file_tree(raw_impl)
        if not parsed:
            # LLM forgot the wrapper — store as-is under target path
            parsed = {target_file: raw_impl.strip()}

        for path, content in parsed.items():
            chain.accumulated_code[path] = content
            job.file_tree[path] = content
            await broadcast(job.id, {"type":"file_created","path":path})

        # ── Thinker pre-plans the NEXT file concurrently with analyst review ──
        # While writer has just finished streaming this file and analyst reviews,
        # the thinker (CPU) pre-computes context hints for the next file so it
        # starts with richer context instead of re-deriving dependencies itself.
        next_file_hint = ""
        next_file = chain.files_pending[1] if len(chain.files_pending) > 1 else ""
        next_hint_task = None
        if thinker and next_file and not cancel_flags.get(job.id):
            async def _prefetch_next_hint(nf: str) -> str:
                try:
                    nf_info = next(
                        (fi for fi in json.loads(chain.architecture).get("files",[])
                         if fi["path"] == nf),
                        {"path": nf, "purpose": "", "depends_on": []}
                    )
                    hint_prompt = (
                        f"Architecture:\n{json.loads(chain.architecture).get('overview','')}\n\n"
                        f"Next file to implement: {nf}\n"
                        f"Purpose: {nf_info.get('purpose','')}\n"
                        f"Files already completed: {', '.join(chain.files_done)}\n"
                        f"Key interfaces so far:\n{_recent_code_ctx(chain, 1500)}\n\n"
                        "List the 3 most important implementation requirements for this file."
                    )
                    return await asyncio.wait_for(
                        collect_ollama(thinker, hint_prompt,
                            "You are a senior developer. Give concise, specific requirements.",
                            job.id, slot_t,
                            timeout_secs=_effective_timeout(thinker, 600)),
                        timeout=_effective_timeout(thinker, 600)
                    )
                except Exception as _e:
                    log.debug("Thinker prefetch failed: %s", _e)
                    return ""
            next_hint_task = asyncio.create_task(_prefetch_next_hint(next_file))

        # ── Analyst review of this file ──────────────────────────────────
        if analyst and not cancel_flags.get(job.id):
            job.status = JobStatus.REVIEWING
            slot_on(slot_a, analyst, job.id, "verifying")
            await step_emit(job, "Reviewing", target_file)

            review_prompt = (
                f"Architecture:\n{json.loads(chain.architecture).get('overview','')}\n\n"
                f"File being reviewed: {target_file}\n\n"
                f"Implementation:\n{raw_impl[:5000]}\n\n"
                f"Other files already written (for consistency check):\n{recent_ctx[:2000]}"
            )

            review_raw = ""
            try:
                review_raw = await asyncio.wait_for(
                    collect_ollama(analyst, review_prompt, CODE_REVIEW_SYS,
                                   job.id, slot_a, timeout_secs=ANALYST_TIMEOUT),
                    timeout=ANALYST_TIMEOUT + 10
                )
            except asyncio.TimeoutError:
                await step_emit(job, "Review timeout", f"Skipping review for {target_file}")

            slot_off(slot_a)

            if review_raw:
                try:
                    clean_r = re.sub(r"^```[a-z]*\s*|\s*```$", "", review_raw.strip(), flags=re.M)
                    review  = json.loads(clean_r)
                except Exception:
                    try:
                        s = review_raw.index("{"); e2 = review_raw.rindex("}")+1
                        review = json.loads(review_raw[s:e2])
                    except Exception:
                        review = {"verdict":"pass","issues":[],"summary":"parse error"}

                verdict = review.get("verdict","pass")
                issues  = review.get("issues",[])
                summary = review.get("summary","")

                await broadcast(job.id, {
                    "type":    "review",
                    "file":    target_file,
                    "verdict": verdict,
                    "issues":  issues,
                    "summary": summary,
                })
                await step_emit(job, f"Review: {verdict}", summary[:60] if summary else target_file)

                # If issues found, ask writer to patch
                if verdict == "patch" and issues and not cancel_flags.get(job.id):
                    job.status = JobStatus.CODING
                    slot_on(slot_w, use_writer, job.id, "writing")
                    await step_emit(job, "Patching", f"{len(issues)} issues in {target_file}")

                    issues_text = "\n".join(
                        f"- [{i['severity']}] {i['description']}\n  Fix: {i['fix']}"
                        for i in issues[:5]
                    )
                    patch_prompt = (
                        f"File: {target_file}\n\n"
                        f"Current implementation:\n{raw_impl[:4000]}\n\n"
                        f"Issues to fix:\n{issues_text}\n\n"
                        f"Produce the corrected complete file."
                    )
                    patch_parts: list[str] = []
                    async for tok in stream_ollama(
                        use_writer, patch_prompt, CODE_IMPL_SYS, job.id, slot_w,
                        timeout_secs=WRITER_TIMEOUT
                    ):
                        patch_parts.append(tok)
                        if cancel_flags.get(job.id): break

                    slot_off(slot_w)
                    patched = "".join(patch_parts)
                    all_output_parts.append(patched)

                    patched_tree = parse_file_tree(patched)
                    if not patched_tree:
                        patched_tree = {target_file: patched.strip()}
                    for path, content in patched_tree.items():
                        chain.accumulated_code[path] = content
                        job.file_tree[path] = content

        # Collect thinker pre-fetch hint for next file
        if next_hint_task:
            try:
                next_file_hint = await asyncio.wait_for(next_hint_task, timeout=5)
            except Exception:
                next_file_hint = ""
        chain._next_file_hint = next_file_hint  # stash for next iteration

        # Mark file done
        chain.files_pending.remove(target_file)
        chain.files_done.append(target_file)
        files_this_run += 1
        job.status = JobStatus.CODING

        # Materialise to disk
        if project:
            await materialise_file_tree(job, project)
        else:
            await materialise_file_tree(job, None)

    # ── Phase 4: Continuity / completion ─────────────────────────────────────

    if chain.files_pending and not cancel_flags.get(job.id):
        # More files remain — generate a continuity summary and signal continuation
        job.status = JobStatus.CHAINING
        await step_emit(job, "Chain summary",
            f"{len(chain.files_done)} done · {len(chain.files_pending)} remain · summarising…")

        use_sum = thinker or writer
        slot_on(slot_t, use_sum, job.id, "thinking")

        sum_prompt = (
            f"Original task: {chain.original_task}\n\n"
            f"Architecture:\n{chain.architecture[:2000]}\n\n"
            f"Files completed this run: {', '.join(chain.files_done[-files_this_run:])}\n"
            f"All files done: {', '.join(chain.files_done)}\n"
            f"Files still pending: {', '.join(chain.files_pending)}\n\n"
            f"Key interfaces/types defined so far:\n{_recent_code_ctx(chain, 2000)}\n\n"
            "Produce a continuity summary so the next run can continue correctly."
        )

        sum_raw = await collect_ollama(
            use_sum, sum_prompt, CODE_CONTINUITY_SYS, job.id, slot_t,
            timeout_secs=SUMMARY_TIMEOUT
        )
        slot_off(slot_t)

        try:
            clean_s = re.sub(r"^```[a-z]*\s*|\s*```$", "", sum_raw.strip(), flags=re.M)
            sum_data = json.loads(clean_s)
        except Exception:
            sum_data = {"summary": sum_raw[:800], "continuation_notes": "Continue from where left off."}

        chain.continuity_summary = sum_data.get("summary","")
        chain.run_number += 1
        chain.is_complete = False
        job.chain_continues = True

        await broadcast(job.id, {
            "type":        "chain_continue",
            "chain_id":    chain.chain_id,
            "run_number":  chain.run_number,
            "files_done":  chain.files_done,
            "files_pending": chain.files_pending,
            "summary":     chain.continuity_summary,
            "continuation_notes": sum_data.get("continuation_notes",""),
        })
        await step_emit(job, "⛓ Continue", f"Run {chain.run_number} ready when you trigger it")

    else:
        # All done
        chain.is_complete = True
        job.chain_continues = False

        # Generate README summary
        readme = (
            f"# {chain.original_task}\n\n"
            f"## Overview\n\n{json.loads(chain.architecture).get('overview','')}\n\n"
            f"## Stack\n\n"
            + "\n".join(f"- {s}" for s in json.loads(chain.architecture).get("stack",[]))
            + f"\n\n## Files\n\n"
            + "\n".join(f"- `{p}`" for p in chain.files_done)
            + f"\n\n## Notes\n\n{json.loads(chain.architecture).get('notes','')}\n"
        )
        if "README.md" not in chain.accumulated_code:
            chain.accumulated_code["README.md"] = readme
            job.file_tree["README.md"] = readme
            await broadcast(job.id, {"type":"file_created","path":"README.md"})

        await step_emit(job, "Complete",
            f"All {len(chain.files_done)} files written · {len(chain.accumulated_code)} in tree")

    # Build result string (manifest + recent output)
    job.result = (
        f"# Code generation: {chain.original_task}\n\n"
        f"**Run {chain.run_number - (0 if chain.is_complete else 1)} of chain `{chain.chain_id}`**\n\n"
        f"## Files written this run\n\n"
        + "\n".join(f"- `{f}`" for f in chain.files_done[-files_this_run:])
        + f"\n\n## Progress\n\n{len(chain.files_done)}/{len(chain.files_planned)} files done\n\n"
        + ("✅ **Complete**" if chain.is_complete
           else f"⛓ **{len(chain.files_pending)} files remaining** — trigger another run to continue")
        + "\n\n---\n\n"
        + "\n\n".join(all_output_parts)[-8000:]  # last 8k chars of generated code
    )


async def step_emit(job: ResearchJob, label: str, detail: str = ""):
    s = {"t":time.time(),"label":label,"detail":detail}
    job.steps.append(s)
    await broadcast(job.id, {"type":"step",**s})


def slot_for(tier: ModelTier) -> Optional[AgentSlot]:
    return next((s for s in agent_slots if s.tier == tier), None)


def slot_on(slot: Optional[AgentSlot], inst: OllamaInstance, job_id: str, status: str):
    if not slot: return
    slot.job_id = job_id; slot.status = status
    slot.model  = inst.model; slot.started_at = time.time()


def slot_off(slot: Optional[AgentSlot]):
    if not slot: return
    slot.status = "idle"; slot.job_id = None


async def _search_phase(
    job: ResearchJob,
    directive: "Optional[ResearchDirective]" = None,
) -> tuple[list[Citation], str]:
    """Intelligent search phase — uses smart_gather for intent detection + structured data."""
    job.status = JobStatus.SEARCHING
    fast = await get_instance(ModelTier.WRITER) or await get_instance(ModelTier.THINKER)
    cits, ctx = await smart_gather(job.query, job, fast=fast, directive=directive)
    return cits, ctx


# ══════════════════════════════════════════════════════════════════════════════
#  Analyst with timeout (fixes stuck analyser)
# ══════════════════════════════════════════════════════════════════════════════

ANALYST_TIMEOUT      = 600.0   # analyst verification max seconds
THINKER_PLAN_TIMEOUT = 300.0   # planning/decompose/outline steps — allow extra for thinkers
THINKER_THINK_TIMEOUT= 1200.0   # deep reasoning/synthesis — extra budget for thinking chains
WRITER_TIMEOUT       = 6000.0   # writer extraction — slightly more headroom
SUMMARY_TIMEOUT      = 300.0   # rolling context summary

def _effective_timeout(inst: "OllamaInstance", base: float) -> float:
    """Scale timeout for instances that have thinking enabled."""
    if not inst.enable_thinking:
        return base
    if inst.thinking_timeout > 0:
        return inst.thinking_timeout
    return base * 6   # thinking chains can be 5-10× longer

async def run_analyst_phase(job: ResearchJob, draft: str,
                             analyst: OllamaInstance, slot_a: Optional[AgentSlot]) -> str:
    """Run analyst verification with a hard timeout so it can't block forever."""
    slot_on(slot_a, analyst, job.id, "verifying")
    job.status = JobStatus.VERIFYING
    await step_emit(job, "Verifying", f"{analyst.name} cross-checking (max {int(ANALYST_TIMEOUT)}s)…")

    verify_sys = (
        "You are a critical research analyst. Review the draft for accuracy, completeness, "
        "and logical consistency. Append a brief ## Verification note at the end. "
        "Do NOT rewrite the entire document — only append your notes."
    )
    verify_prompt = f"Query: {job.query}\n\nDraft:\n{draft[:6000]}"

    try:
        result = await asyncio.wait_for(
            collect_ollama(analyst, verify_prompt, verify_sys,
                           job.id, slot_a, timeout_secs=ANALYST_TIMEOUT),
            timeout=ANALYST_TIMEOUT + 10
        )
        slot_off(slot_a)
        return result or draft
    except asyncio.TimeoutError:
        slot_off(slot_a)
        await step_emit(job, "Verify timeout", f"Skipped after {int(ANALYST_TIMEOUT)}s")
        return draft + "\n\n---\n*Analyst verification timed out.*"


# ══════════════════════════════════════════════════════════════════════════════
#  Guide / multi-section output
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
#  Recursive Research Engine
#  ─────────────────────────
#  Instead of one search → one write, the engine:
#    1. Plans a tree of questions (breadth-first per level)
#    2. For each question: searches, reads full page text, identifies
#       further sub-questions the sources raise ("what I still don't know")
#    3. Recurses up to max_depth levels, gathering citations at every level
#    4. Builds a knowledge base across all levels
#    5. Synthesises into a final document
#
#  This turns it from a "summarise the top 8 search results" tool into
#  something that actually follows the research thread.
# ══════════════════════════════════════════════════════════════════════════════

MAX_RECURSIVE_DEPTH   = 3     # levels deep
MAX_QUESTIONS_PER_LEVEL = 4   # sub-questions per node
MAX_TOTAL_QUESTIONS   = 16    # circuit-breaker


@dataclass
class ResearchNode:
    question: str
    depth: int
    parent: Optional[str] = None
    citations: list[Citation] = field(default_factory=list)
    findings: str = ""
    sub_questions: list[str] = field(default_factory=list)


async def research_node(
    node: ResearchNode,
    job: ResearchJob,
    thinker: Optional[OllamaInstance],
    writer: OllamaInstance,
    all_citations: list[Citation],
    knowledge_base: list[str],
    directive: "Optional[ResearchDirective]" = None,
) -> None:
    """
    Research one question node — three instances working in parallel:
      • WRITER (GPU)  — search, score, extract facts from sources
      • ANALYST (CPU) — host-local NLP: entities, facts, anomalies, gaps
        runs concurrently with writer extraction; enriches node findings
      • THINKER (CPU) — sub-question planning with directive scope guard
        prevents drift into off-topic territory
    """
    if cancel_flags.get(job.id): return

    depth_label = "·" * node.depth
    await step_emit(job, f"{depth_label} Search", node.question[:70])

    # ── Gather sources (parallel: all source types at once) ───────────────
    fast_inst = await get_instance(ModelTier.WRITER) or await get_instance(ModelTier.THINKER)
    cits, ctx = await smart_gather(node.question, job, fast=fast_inst,
                                   directive=directive)
    # Filter arXiv immediately — don't let non-academic papers pollute node findings
    cits = _filter_arxiv(cits, directive)
    node.citations = cits
    all_citations.extend(cits)
    await broadcast(job.id, {"type":"citations","citations":[c.to_dict() for c in all_citations]})

    if not ctx and not cits:
        node.findings = f"No sources found for: {node.question}"
        return

    # ── Score and deduplicate citations (writer = fast model) ────────────
    # Batch-score all candidates in ONE prompt, then cross-source dedup.
    if len(cits) > 3 and writer:
        cit_list = cits[:16]   # score up to 16 candidates
        score_prompt = (
            f"Query: {node.question}\n\n"
            "For each source below, rate 0-5: 5=highly relevant unique insight, "
            "3=somewhat relevant, 1=tangential, 0=irrelevant or duplicate of another source.\n"
            "IMPORTANT: penalise sources that repeat the same information as higher-rated sources.\n"
            "Return ONLY a JSON array of integers in the same order. No other text.\n\n"
            + "\n".join(
                f"[{i}] {c.title} ({c.domain}): {c.snippet[:160]}"
                for i,c in enumerate(cit_list)
            )
        )
        try:
            raw_scores = await asyncio.wait_for(
                collect_ollama(writer, score_prompt,
                    "Return only a JSON array of integers.", job.id,
                    timeout_secs=_effective_timeout(writer, 60)),
                timeout=_effective_timeout(writer, 70),
            )
            start = raw_scores.find("["); end = raw_scores.rfind("]") + 1
            if start >= 0 and end > start:
                scores = json.loads(raw_scores[start:end])
                scored = sorted(zip(scores[:len(cit_list)], cit_list), key=lambda x: -x[0])
                # Keep score ≥ 2, always at least 4 results
                good = [c for s, c in scored if s >= 2]
                if len(good) >= 4:
                    # Post-scoring: penalise low-scoring arXiv on non-academic nodes
                    # and replace them with fresh web searches
                    _dir_is_academic = (
                        directive and directive.valid and
                        directive.output_style in ("deep_analysis", "report") and
                        any(t in node.question.lower() for t in _ACADEMIC_QUERY_TERMS)
                    )
                    arxiv_dropped = [
                        c for s, c in scored
                        if s <= 2 and c.source_type == "arxiv"
                    ]
                    if arxiv_dropped and not _dir_is_academic:
                        log.info("research_node: dropping %d low-scoring arXiv citations, "
                                 "firing replacement searches", len(arxiv_dropped))
                        # Fire replacement searches for each dropped arxiv result
                        replacement_tasks = [
                            asyncio.wait_for(
                                gather_web_search(
                                    f"{node.question} {c.title[:40]}", job.id),
                                timeout=8.0)
                            for c in arxiv_dropped[:2]
                        ]
                        try:
                            repl_results = await asyncio.gather(
                                *replacement_tasks, return_exceptions=True)
                            existing_urls = {c.url for c in good}
                            for repl in repl_results:
                                if isinstance(repl, list):
                                    for rc in repl[:2]:
                                        if rc.url not in existing_urls:
                                            good.append(rc)
                                            existing_urls.add(rc.url)
                                            all_citations.append(rc)
                        except Exception as _re:
                            log.debug("Replacement search failed: %s", _re)

                    # Content-fingerprint dedup post-scoring
                    fps: set[str] = set()
                    unique_good: list = []
                    for c in good:
                        fp = _content_fingerprint(c.title + " " + c.snippet)
                        if fp not in fps:
                            fps.add(fp)
                            unique_good.append(c)
                    cits = unique_good[:10]
                    ctx = "\n\n".join(
                        f"[{i+1}] **{c.title}** ({c.domain})\n"
                        + (f"    > {c.snippet[:280]}\n" if c.snippet else "")
                        + (f"    {c.full_text[:700]}\n" if c.full_text else "")
                        + f"    URL: {c.url}"
                        for i, c in enumerate(cits)
                    )
                    await step_emit(job, f"{depth_label} Curated",
                        f"{len(cits)}/{len(node.citations)} sources · "
                        f"{len(arxiv_dropped)} arXiv replaced")
        except Exception as e:
            log.debug("Source scoring failed: %s", e)

    # ── Extract findings (WRITER = fast model) ────────────────────────────
    await step_emit(job, f"{depth_label} Extract", f"Reading {len(cits)} sources…")
    extract_sys = (
        "You are a research analyst extracting precise knowledge from sources. "
        "Be specific and dense — facts, numbers, dates, names, mechanisms. "
        "Note contradictions and uncertainty. No introduction, no conclusion."
    )
    extract_prompt = (
        f"Research question: {node.question}\n\n"
        f"Sources:\n{ctx[:6000]}\n\n"
        + (f"Prior context:\n{chr(10).join(knowledge_base[-3:])[:1200]}\n\n"
           if knowledge_base else "") +
        "Extract ALL specific findings. Dense bullet points only."
    )
    findings_parts: list[str] = []
    async for tok in stream_ollama(writer, extract_prompt, extract_sys, job.id,
                                    timeout_secs=WRITER_TIMEOUT):
        findings_parts.append(tok)
        if cancel_flags.get(job.id): break
    node.findings = "".join(findings_parts)

    # ── Host-local analyst NLP (ANALYST instance, concurrent with knowledge_base update) ─
    # Runs fast NLP phases on this node's citations — no LLM needed.
    # Enriches node findings with entities, key quotes, anomalies, contradictions.
    analyst_inst = await get_instance(ModelTier.ANALYST)
    node_analyst_task = None
    if node.citations:
        node_analyst_task = asyncio.create_task(
            run_analyst_engine(node.question, node.citations, job.id,
                               None, None,  # host-local only
                               nlp_tools=(directive.nlp_tools
                                          if directive and directive.valid
                                          else ["entities","key_quotes","gaps"]))
        )

    # ── Sub-question planning (THINKER = smart model) ────────────────────────
    # Directive scope guard prevents drift — thinker knows what to stay focused on.
    if node.depth < MAX_RECURSIVE_DEPTH:
        plan_inst = thinker or writer
        sub_sys = "Identify precise follow-up research questions. Return ONLY a JSON array of strings."

        # Build scope constraint from directive
        scope_guard = ""
        if directive and directive.valid:
            if directive.scope_focus:
                scope_guard += f"\nFocus only on: {directive.scope_focus}"
            if directive.scope_exclude:
                scope_guard += f"\nDo NOT ask about: {chr(59).join(directive.scope_exclude[:3])}"
            if directive.key_questions:
                unanswered = [q for q in directive.key_questions
                              if not any(q.lower()[:20] in kb.lower() for kb in knowledge_base)]
                if unanswered:
                    scope_guard += (f"\nPriority: answer these if not yet covered: "
                                    + "; ".join(unanswered[:2]))

        sub_prompt = (
            f"Question investigated: {node.question}\n\n"
            f"Findings:\n{node.findings[:2000]}\n\n"
            f"List {MAX_QUESTIONS_PER_LEVEL} specific follow-up questions not yet answered. "
            "Questions must be directly related to the original research goal. "
            "Avoid tangential, philosophical, or off-topic questions."
            + scope_guard
            + "\nJSON array only."
        )
        try:
            raw = await asyncio.wait_for(
                collect_ollama(plan_inst, sub_prompt, sub_sys, job.id,
                               timeout_secs=THINKER_PLAN_TIMEOUT),
                timeout=THINKER_PLAN_TIMEOUT + 5
            )
            qs = json.loads(raw[raw.index("["):raw.rindex("]")+1])
            raw_qs = [str(q) for q in qs[:MAX_QUESTIONS_PER_LEVEL]]

            # Validate sub-questions against directive scope (same logic as angle validator)
            if directive and directive.valid and directive.scope_exclude:
                exclude_terms = set(" ".join(directive.scope_exclude).lower().split())
                raw_qs = [
                    q for q in raw_qs
                    if not any(t in q.lower() for t in exclude_terms if len(t) > 4)
                ]
            node.sub_questions = raw_qs
        except Exception:
            node.sub_questions = []

    # ── Collect analyst NLP results and enrich node findings ──────────────
    if node_analyst_task:
        try:
            ar: AnalystReport = await asyncio.wait_for(node_analyst_task, timeout=20.0)
            if ar.valid:
                # Append compact NLP enrichment to findings so downstream synthesis benefits
                enrichment_parts = []
                if ar.entities:
                    ent_lines = [f"{k}: {chr(44).join(str(v) for v in vals[:5])}"
                                 for k, vals in ar.entities.items() if vals]
                    if ent_lines:
                        enrichment_parts.append("Entities: " + " | ".join(ent_lines))
                if ar.key_quotes:
                    enrichment_parts.append(
                        "Key data: " + " | ".join(
                            q["text"][:80] for q in ar.key_quotes[:3]
                        )
                    )
                if ar.contradictions:
                    enrichment_parts.append(
                        f"Contradictions ({len(ar.contradictions)}): "
                        + ar.contradictions[0]["claim_a"][:60]
                        + " vs "
                        + ar.contradictions[0]["claim_b"][:60]
                    )
                if ar.anomalies:
                    enrichment_parts.append("Anomaly: " + ar.anomalies[0][:100])
                if enrichment_parts:
                    node.findings += ("\n\n**NLP Analysis:**\n"
                                     + "\n".join(f"- {p}" for p in enrichment_parts))
                log.debug("Node analyst: %d entities, %d quotes, %d contradictions",
                          len(ar.entities), len(ar.key_quotes), len(ar.contradictions))
        except Exception as e:
            log.debug("Node analyst NLP failed: %s", e)

    knowledge_base.append(
        f"[L{node.depth}] Q: {node.question}\n"
        f"Findings: {node.findings[:1200]}"
    )


async def recursive_research(
    job: ResearchJob,
    thinker: Optional[OllamaInstance],
    writer: OllamaInstance,
    project: Optional[Project],
    directive: "Optional[ResearchDirective]" = None,
) -> tuple[list[ResearchNode], list[Citation], str]:
    """
    Run the full recursive research tree.
    Returns (nodes, all_citations, accumulated_context).
    """
    proj_ctx = (f"\nProject context:\n{project.context_summary}" if project else "")
    all_nodes: list[ResearchNode] = []
    all_citations: list[Citation] = []
    knowledge_base: list[str] = []
    question_count = 0

    # Root node — BFS but run all nodes at the same depth concurrently
    root = ResearchNode(question=job.query, depth=0)
    current_level: list[ResearchNode] = [root]

    while current_level and question_count < MAX_TOTAL_QUESTIONS:
        if cancel_flags.get(job.id): break

        # Run all nodes at this depth level in parallel (writer handles all)
        batch = current_level[:MAX_TOTAL_QUESTIONS - question_count]
        await step_emit(job, f"Level {batch[0].depth}",
            f"Investigating {len(batch)} question(s) in parallel…")

        await asyncio.gather(*[
            research_node(node, job, thinker, writer, all_citations, knowledge_base,
                          directive=directive)
            for node in batch
        ])
        all_nodes.extend(batch)
        question_count += len(batch)

        # Collect sub-questions from this level → next level
        next_level: list[ResearchNode] = []
        for node in batch:
            for sq in node.sub_questions:
                if question_count + len(next_level) >= MAX_TOTAL_QUESTIONS: break
                next_level.append(ResearchNode(question=sq,
                                                depth=node.depth+1,
                                                parent=node.question))
        current_level = next_level

        await step_emit(job, "Progress",
            f"{question_count} questions, {len(all_citations)} sources found")

    # Build full context string for synthesis
    ctx_parts = [f"## Recursive Research Results ({len(all_nodes)} nodes)\n"]
    for node in all_nodes:
        indent = "  " * node.depth
        ctx_parts.append(f"\n{indent}### {'Root: ' if node.depth==0 else ''}{node.question}")
        if node.parent:
            ctx_parts.append(f"{indent}*(sub-question of: {node.parent})*")
        ctx_parts.append(f"{indent}{node.findings[:2000]}")
        if node.citations:
            ctx_parts.append(f"{indent}*Sources: {', '.join(c.domain for c in node.citations[:5])}*")

    full_ctx = "\n".join(ctx_parts)

    # Deduplicate citations
    seen_urls: set[str] = set()
    deduped: list[Citation] = []
    for c in all_citations:
        if c.url not in seen_urls:
            seen_urls.add(c.url)
            deduped.append(c)

    return all_nodes, deduped, full_ctx


async def run_guide_output(job: ResearchJob, ctx: str,
                            thinker: Optional[OllamaInstance],
                            writer: OllamaInstance,
                            project: Optional[Project]) -> str:
    """
    Deep recursive guide:
    1. Thinker builds ResearchDirective (scope + style)
    2. Recursive research tree with directive-scoped sub-questions
    3. Analyst NLP runs on each node concurrently
    4. Thinker plans section outline from ALL gathered knowledge
    5. Writer generates each section with directive-tailored system prompt
    """
    proj_ctx = (f"\n\nProject context:\n{project.context_summary}" if project else "")

    # Build directive first — guides the entire research tree
    await step_emit(job, "Directing", "Analysing query intent for guide…")
    directive = await build_research_directive(job.query, thinker, job.id, project, job=job)
    if directive.valid:
        await step_emit(job, "Directive",
            f"Style: {directive.output_style} · "
            f"{len(directive.key_questions)} key questions")

    # Run recursive research with directive-scoped node planning
    await step_emit(job, "Deep research", "Recursively investigating topic…")
    nodes, all_cits, full_ctx = await recursive_research(
        job, thinker, writer, project, directive=directive)

    # Update job citations — apply final arXiv filter with full directive context
    all_cits = _filter_arxiv(all_cits, directive)
    job.citations = all_cits
    await broadcast(job.id, {"type":"citations","citations":[c.to_dict() for c in all_cits]})

    # ── Analyst NLP on ALL citations, concurrent with section planning ──────
    # Host-local NLP phases (entities, quotes, contradictions, timeline) run on
    # the analyst instance while the thinker plans sections — both CPU-bound.
    analyst_inst_g = await get_instance(ModelTier.ANALYST)
    slot_ag = slot_for(ModelTier.ANALYST)
    guide_analyst_task = asyncio.create_task(
        run_analyst_engine(job.query, all_cits, job.id,
                           analyst_inst_g, slot_ag,
                           nlp_tools=directive.nlp_tools if directive.valid else None)
    )
    log.info("Guide: analyst NLP started on %d citations", len(all_cits))

    # Plan sections — thinker uses directive to know what sections are needed
    await step_emit(job, "Outline", "Planning sections from gathered knowledge…")
    use_inst = thinker or writer
    key_q_hint = ""
    if directive.valid and directive.key_questions:
        key_q_hint = ("\nEnsure sections cover these key questions: "
                      + "; ".join(directive.key_questions[:4]))
    outline_prompt = (
        f"Topic: {job.query}{proj_ctx}\n\n"
        + (f"Output style: {directive.output_style}. {directive.scope_focus}\n\n"
           if directive.valid and directive.scope_focus else "")
        + f"Knowledge gathered:\n{full_ctx[:6000]}\n\n"
        + "Based on everything researched, plan 6-10 section headings for a comprehensive guide. "
        + "Sections should reflect what was actually found, not generic placeholders. "
        + key_q_hint
        + "\nRespond ONLY with a JSON array of strings."
    )
    raw_outline = await collect_ollama(use_inst, outline_prompt,
        "You are a guide architect. Return only a JSON array of section titles.", job.id, timeout_secs=THINKER_PLAN_TIMEOUT)

    sections: list[str] = []
    try:
        sections = json.loads(raw_outline[raw_outline.index("["):raw_outline.rindex("]")+1])
        sections = [str(s) for s in sections[:10]]
    except Exception:
        # Fallback: use node questions as sections
        sections = [n.question for n in nodes[:8]]

    await step_emit(job, "Writing", f"{len(sections)} sections from {len(nodes)} research nodes")

    # Collect analyst results (started during outline planning)
    guide_ar = AnalystReport()
    try:
        guide_ar = await asyncio.wait_for(guide_analyst_task, timeout=60.0)
        if guide_ar.valid:
            await step_emit(job, "Analysis",
                f"{len(guide_ar.knowledge_bullets)} facts · "
                f"{len(guide_ar.entities)} entity types · "
                f"{guide_ar.elapsed:.1f}s")
            # Merge gap-fill citations
            seen_gu = {c.url for c in job.citations}
            for c in guide_ar.gap_fill_cits:
                if c.url not in seen_gu:
                    job.citations.append(c); seen_gu.add(c.url)
    except Exception as _eg:
        log.debug("Guide analyst: %s", _eg)

    # Analyst compact context to inject into each section
    guide_analyst_ctx = guide_ar.to_context_string() if guide_ar.valid else ""

    # Build citation reference list for LLM to cite inline
    cit_ref = "\n".join(f"[{i+1}] {c.title} — {c.url}" for i, c in enumerate(job.citations[:30]))

    all_parts: list[str] = [f"# {job.query}\n\n"]
    slot_w = slot_for(ModelTier.WRITER)
    slot_on(slot_w, writer, job.id, "writing")

    for i, section in enumerate(sections, 1):
        if cancel_flags.get(job.id): break
        await step_emit(job, f"§{i}/{len(sections)}", section[:60])

        # Find the most relevant research nodes for this section
        relevant_nodes = [n for n in nodes if
            any(w.lower() in n.question.lower() or w.lower() in n.findings.lower()
                for w in section.lower().split()[:5])][:4]
        node_ctx = "\n\n".join(
            f"From '{n.question}':\n{n.findings[:1500]}"
            for n in (relevant_nodes or nodes[:3])
        )

        sec_prompt = (
            f"Guide topic: {job.query}\n\n"
            f"Section to write: ## {section}\n\n"
            + (f"Research Intelligence (NLP-extracted facts/entities/contradictions):\n"
               f"{guide_analyst_ctx[:1800]}\n\n" if guide_analyst_ctx else "")
            + f"Directly relevant research findings:\n{node_ctx[:4000]}\n\n"
            + f"Full knowledge base summary:\n{full_ctx[:2000]}\n\n"
            + f"{proj_ctx}\n"
            + f"Previously written (for continuity):\n{''.join(all_parts)[-1200:]}\n\n"
            + f"Available citations:\n{cit_ref[:2000]}\n\n"
            + f"Write a thorough, specific section titled '## {section}'. "
            + "Use the actual research findings — specific facts, numbers, names, mechanisms. "
            + "Do NOT be vague or generic. Cite sources as [1], [2] etc. "
            + "Include code, commands, configs, or step-by-step instructions where relevant."
        )
        sec_parts: list[str] = []
        guide_sys = (directive.writer_sys
                     if directive.valid and directive.writer_sys
                     else GUIDE_SYS)
        # Thinker pre-plans next section concurrently (runs on CPU while writer uses GPU)
        next_sec = sections[i] if i < len(sections) else ""
        next_sec_plan_task = None
        if thinker and thinker is not writer and next_sec:
            _next_relevant = [n for n in nodes if
                any(w.lower() in n.question.lower()
                    for w in next_sec.lower().split()[:4])][:3]
            _next_ctx = "\n".join(f"- {n.findings[:300]}" for n in _next_relevant)
            next_sec_plan_task = asyncio.create_task(
                asyncio.wait_for(
                    collect_ollama(thinker,
                        f"Topic: {job.query}\nNext section: {next_sec}\n"
                        f"Relevant findings:\n{_next_ctx}\n"
                        "List 3 specific facts/examples this section must include.",
                        "Research planner. Be brief and specific.", job.id,
                        timeout_secs=_effective_timeout(thinker, 600)),
                    timeout=_effective_timeout(thinker, 600)
                )
            )
        async for tok in stream_ollama(writer, sec_prompt, guide_sys, job.id, slot_w):
            sec_parts.append(tok)
            if cancel_flags.get(job.id): break
        sec_text = "".join(sec_parts)
        # Collect thinker pre-plan (store for next iteration)
        _next_plan = ""
        if next_sec_plan_task:
            try: _next_plan = await asyncio.wait_for(next_sec_plan_task, timeout=3)
            except Exception: pass
        # Append plan hint to next iteration via a closure variable stored in all_parts metadata
        all_parts.append(sec_text + "\n\n")

    slot_off(slot_w)

    # Append references
    if all_cits:
        all_parts.append("\n\n## References\n\n")
        for i, c in enumerate(all_cits[:50], 1):
            all_parts.append(f"[{i}] [{c.title}]({c.url})  \n")

    return "".join(all_parts)


# ══════════════════════════════════════════════════════════════════════════════
#  Filestore output
# ══════════════════════════════════════════════════════════════════════════════

async def run_filestore_output(job: ResearchJob, ctx: str,
                                thinker: Optional[OllamaInstance],
                                writer: OllamaInstance,
                                project: Optional[Project]) -> str:
    """
    Produce a full file tree:
    1. Thinker plans the file/directory structure
    2. Writer generates each file's content
    3. Parse and materialise to disk
    """
    proj_ctx = (f"\n\nProject context:\n{project.context_summary}" if project else "")
    await step_emit(job, "Planning files", "Designing file structure…")

    plan_prompt = (
        f"Task: {job.query}\n\nSources:\n{ctx[:3000]}{proj_ctx}\n\n"
        "List ALL files that need to be created. "
        "Respond ONLY with a JSON array of file paths, e.g. "
        '["docker-compose.yml","app/main.py","app/config.py","README.md"]'
    )
    use_inst = thinker or writer
    raw_plan = await collect_ollama(use_inst, plan_prompt,
        "You are a software architect. Return only a JSON array of file paths.", job.id, timeout_secs=THINKER_PLAN_TIMEOUT)

    file_paths: list[str] = []
    try:
        file_paths = json.loads(raw_plan[raw_plan.index("["):raw_plan.rindex("]")+1])
        file_paths = [str(p) for p in file_paths if "." in p][:30]
    except Exception:
        file_paths = []

    if not file_paths:
        # Fallback: ask writer to produce everything in one shot
        await step_emit(job, "Generating", "Producing all files…")
        slot_w = slot_for(ModelTier.WRITER)
        slot_on(slot_w, writer, job.id, "writing")
        all_content: list[str] = []
        async for tok in stream_ollama(writer,
            f"Task: {job.query}\n\nSources:\n{ctx[:5000]}{proj_ctx}\n\nProduce ALL necessary files.",
            FILESTORE_SYS, job.id, slot_w):
            all_content.append(tok)
            if cancel_flags.get(job.id): break
        slot_off(slot_w)
        raw = "".join(all_content)
        job.file_tree = parse_file_tree(raw)
        await materialise_file_tree(job, project)
        return raw

    await step_emit(job, "Files planned", f"{len(file_paths)} files")
    slot_w = slot_for(ModelTier.WRITER)
    slot_on(slot_w, writer, job.id, "writing")

    all_output: list[str] = []
    completed_files: dict[str, str] = {}

    for i, fpath in enumerate(file_paths, 1):
        if cancel_flags.get(job.id): break
        await step_emit(job, f"File {i}/{len(file_paths)}", fpath)
        ext = Path(fpath).suffix
        file_prompt = (
            f"Task: {job.query}\n\nFile to create: {fpath}\n\n"
            f"Sources:\n{ctx[:3000]}{proj_ctx}\n\n"
            f"Already created files:\n{chr(10).join(completed_files.keys())}\n\n"
            f"Write the COMPLETE contents of {fpath}. "
            f"Wrap it like:\n=== FILE: {fpath} ===\n<contents>\n=== END ==="
        )
        file_parts: list[str] = []
        async for tok in stream_ollama(writer, file_prompt, FILESTORE_SYS, job.id, slot_w):
            file_parts.append(tok)
            if cancel_flags.get(job.id): break
        file_raw = "".join(file_parts)
        all_output.append(file_raw)
        # Parse this file immediately
        parsed = parse_file_tree(file_raw)
        if not parsed:
            # Wrap it ourselves if LLM forgot
            parsed = {fpath: file_raw.strip()}
        completed_files.update(parsed)
        await broadcast(job.id, {"type":"file_created","path":fpath})

    slot_off(slot_w)
    job.file_tree = completed_files
    await materialise_file_tree(job, project)

    # Generate a README summary
    summary = f"# {job.query}\n\n## Files Created\n\n"
    for p in completed_files:
        summary += f"- `{p}`\n"
    summary += f"\n## Sources\n\n"
    for i, c in enumerate(job.citations, 1):
        summary += f"[{i}] [{c.title}]({c.url})\n"
    return summary + "\n\n" + "\n\n".join(all_output)


# ══════════════════════════════════════════════════════════════════════════════
#  Main pipelines
# ══════════════════════════════════════════════════════════════════════════════

async def run_single(job: ResearchJob, project: Optional[Project] = None):
    """
    Single-agent mode.
    Report: one search round → write.
    Guide/Files: use full recursive engine.
    Code: use coding pipeline (all three agents).
    """
    writer  = await get_instance(ModelTier.WRITER) or await get_instance(ModelTier.THINKER)
    thinker = await get_instance(ModelTier.THINKER)
    if not writer:
        job.status = JobStatus.ERROR; job.error = "No Ollama instance available"; return

    slot_w = slot_for(ModelTier.WRITER)
    slot_on(slot_w, writer, job.id, "active")

    if job.output_mode == OutputMode.CODE:
        slot_off(slot_w)
        await run_code_pipeline(job, project)
        return

    if job.output_mode == OutputMode.GUIDE:
        job.status = JobStatus.SEARCHING
        job.result = await run_guide_output(job, "", thinker, writer, project)
        slot_off(slot_w)
        return

    if job.output_mode == OutputMode.FILESTORE:
        fstore_dir = await build_research_directive(job.query, thinker, job.id, project, job=job)
        _, ctx = await _search_phase(job, directive=fstore_dir)
        if job.citations:
            try:
                fs_ar = await asyncio.wait_for(
                    run_analyst_engine(job.query, job.citations, job.id,
                                       None, None,
                                       nlp_tools=fstore_dir.nlp_tools or None),
                    timeout=20.0)
                if fs_ar.valid:
                    ctx = fs_ar.to_context_string() + "\n\n---\n\n" + ctx
                    await step_emit(job, "NLP",
                        f"{len(fs_ar.knowledge_bullets)} facts extracted")
            except Exception as _fe: log.debug("Filestore analyst: %s", _fe)
        job.status = JobStatus.WRITING
        job.result = await run_filestore_output(job, ctx, thinker, writer, project)
        slot_off(slot_w)
        return

    # Standard report: thinker directs, search gathers, writer executes.
    # Thinker fires FIRST — it analyses the query intent and produces a
    # ResearchDirective that shapes output style, writer system prompt,
    # scope boundaries, and required content. Search runs concurrently.
    job.status = JobStatus.THINKING
    slot_t2 = slot_for(ModelTier.THINKER)
    if thinker:
        slot_on(slot_t2, thinker, job.id, "directing")
        await step_emit(job, "Directing",
                        f"{thinker.name} analysing query intent…")

    directive_task = asyncio.create_task(
        build_research_directive(job.query, thinker, job.id, project, job=job)
    )
    job.status = JobStatus.SEARCHING
    search_task = asyncio.create_task(_search_phase(job))

    # Both run concurrently — directive is fast (5-15s), search is slower
    directive, (_, ctx) = await asyncio.gather(
        directive_task, search_task, return_exceptions=True
    )
    if isinstance(directive, (Exception, type(None))):
        directive = ResearchDirective(); directive.writer_sys = directive.build_writer_sys()
    if isinstance(ctx, Exception): ctx = ""
    if thinker: slot_off(slot_t2)

    # Post-hoc arXiv filter — directive now known, strip any stray arXiv
    # citations that gathered before the directive was ready
    job.citations[:] = _filter_arxiv(job.citations, directive)

    if directive.valid:
        await broadcast(job.id, {
            "type": "directive",
            "style": directive.output_style,
            "focus": directive.scope_focus,
            "depth": directive.depth,
        })
        await step_emit(job, "Directive",
            f"Style: {directive.output_style} · "
            f"Depth: {directive.depth} · "
            f"{len(directive.key_questions)} key questions")

    proj_ctx = (f"\n\nProject context:\n{project.context_summary}" if project else "")
    iter_ctx = ""
    if job.prior_context and job.context_mode == "continue":
        iter_ctx = (
            f"\n\n## Prior Research (build on this, do not repeat)\n"
            f"{job.prior_context[:3000]}"
        )

    # Build directive context header for writer
    directive_hdr = directive.to_context_header() if directive.valid else ""
    cit_ref = "\n".join(f"[{i+1}] {c.title} — {c.url}" for i,c in enumerate(job.citations[:20]))

    # ── Analyst Engine: runs concurrently with search+outline, host-local ────
    # Host-local NLP phases finish in ~0.1-2s (no LLM needed for phases 1-7, 9-12).
    # LLM structural phase (8) is optional and uses the analyst instance.
    # We wait for it before writing so the writer gets compact, scored context
    # instead of raw source text — this produces significantly better reports.
    analyst_inst = await get_instance(ModelTier.ANALYST)
    analyst_slot = slot_for(ModelTier.ANALYST)
    analyst_task = asyncio.create_task(
        run_analyst_engine(job.query, job.citations, job.id,
                           analyst_inst, analyst_slot,
                           nlp_tools=directive.nlp_tools if directive.valid else None)
    )
    log.info("AnalystEngine task started for job %s (%d citations)",
             job.id, len(job.citations))

    # Await analyst — give it enough time for all host-local phases + optional LLM
    # No analyst model: 60s (covers all 12 NLP phases + gap-fill)
    # With analyst model: use _effective_timeout (scales with thinking mode)
    ar = AnalystReport()
    analyst_timeout = (
        _effective_timeout(analyst_inst, ANALYST_TIMEOUT)
        if analyst_inst else 600.0
    )
    try:
        ar = await asyncio.wait_for(analyst_task, timeout=analyst_timeout)
        if ar.valid:
            log.info("AnalystEngine run_single: %d bullets, %d contradictions, "
                     "%d gaps, %d timeline, %d quotes — %.2fs",
                     len(ar.knowledge_bullets), len(ar.contradictions),
                     len(ar.gaps), len(ar.timeline), len(ar.key_quotes), ar.elapsed)
            await step_emit(job, "Analysis",
                f"{len(ar.knowledge_bullets)} findings · "
                f"{len(ar.contradictions)} contradictions · "
                f"{len(ar.gaps)} gaps · {ar.elapsed:.1f}s")
            # Merge gap-fill citations found by analyst
            seen = {c.url for c in job.citations}
            for c in ar.gap_fill_cits:
                if c.url not in seen:
                    job.citations.append(c); seen.add(c.url)
        else:
            log.warning("AnalystEngine run_single: report.valid=False "
                        "(citations=%d, chars=%d)",
                        len(job.citations),
                        sum(len(c.full_text or "") + len(c.snippet or "")
                            + len(c.title or "") for c in job.citations))
    except asyncio.TimeoutError:
        log.warning("AnalystEngine run_single timed out after %.0fs — using partial",
                    analyst_timeout)
        if not analyst_task.done():
            analyst_task.cancel()
    except Exception as e:
        log.warning("AnalystEngine run_single exception: %s", e)

    # Build writer input — analyst compact context as primary, raw ctx as secondary
    analyst_ctx = ar.to_context_string() if ar.valid else ""
    # Rebuild cit_ref with any gap-fill additions
    cit_ref = "\n".join(f"[{i+1}] {c.title} — {c.url}"
                          for i, c in enumerate(job.citations[:25]))

    # Use the directive to construct a precisely targeted write prompt
    if analyst_ctx or directive_hdr:
        write_prompt = (
            f"Research query: {job.query}{proj_ctx}{iter_ctx}\n\n"
            + (directive_hdr + "\n" if directive_hdr else "")
            + (f"{analyst_ctx}\n\n" if analyst_ctx else "")
            + f"Full source context (for detail):\n{ctx[:4000]}\n\n"
            + f"Citations:\n{cit_ref}\n\n"
            + "Write a research output following the directive above. "
            + "Cite every claim as [1], [2] etc. "
            + "Reproduce tables using markdown table syntax. "
            + "Address the research gaps listed. "
            + "Resolve contradictions explicitly."
        )
    else:
        write_prompt = (
            f"Research query: {job.query}{proj_ctx}{iter_ctx}\n\n{ctx}\n\n"
            f"Citations:\n{cit_ref}\n\n"
            "Write a comprehensive research report. "
            "Cite sources as [1], [2] etc. Include specific facts, numbers, dates. "
            "Reproduce any tables from sources using markdown table syntax. "
            "Include image URLs as markdown images where sources provide them."
        )

    # Use the directive writer_sys — it is tailored to style + scope
    active_writer_sys = (directive.writer_sys if directive.valid and directive.writer_sys
                         else WRITE_SYS)
    job.status = JobStatus.WRITING
    await step_emit(job, "Writing", f"{writer.name} [{directive.output_style}]")
    parts: list[str] = []
    async for tok in stream_ollama(writer, write_prompt, active_writer_sys, job.id, slot_w):
        parts.append(tok)
        if cancel_flags.get(job.id): break
    job.result = "".join(parts)
    slot_off(slot_w)

    # Append analyst report section (tables, timeline, quotes, gaps)
    if ar.valid:
        job.result += ar.to_report_section()


async def run_deep(job: ResearchJob, project: Optional[Project] = None):
    """
    Deep mode: always uses the recursive research engine.
    Thinker does slow reasoning over the accumulated knowledge base,
    Writer produces the final output.
    """
    thinker = await get_instance(ModelTier.THINKER)
    writer  = await get_instance(ModelTier.WRITER)
    analyst = await get_instance(ModelTier.ANALYST)
    slot_t  = slot_for(ModelTier.THINKER)
    slot_a  = slot_for(ModelTier.ANALYST)

    if not (thinker or writer):
        await run_single(job, project); return

    use_writer = writer or thinker

    # FILE/CODE/GUIDE: delegate entirely to single-agent pipeline which handles them correctly
    if job.output_mode in (OutputMode.CODE, OutputMode.GUIDE, OutputMode.FILESTORE):
        await run_single(job, project)
        return

    # Deep report: directive → recursive research → thinker synthesises → writer drafts
    await step_emit(job, "Directing", "Thinker analysing query intent…")
    job.status = JobStatus.THINKING
    deep_directive = await build_research_directive(job.query, thinker, job.id, project, job=job)
    if deep_directive.valid:
        await step_emit(job, "Directive",
            f"Style: {deep_directive.output_style} · "
            f"Depth: {deep_directive.depth} · "
            f"{len(deep_directive.key_questions)} key questions")

    await step_emit(job, "Deep research", "Recursively investigating…")
    job.status = JobStatus.SEARCHING

    nodes, all_cits, full_ctx = await recursive_research(
        job, thinker, use_writer, project, directive=deep_directive)
    # Final arXiv filter on all accumulated citations
    all_cits = _filter_arxiv(all_cits, deep_directive)
    job.citations = all_cits
    await broadcast(job.id, {"type":"citations","citations":[c.to_dict() for c in all_cits]})

    if cancel_flags.get(job.id): return

    # Fire AnalystEngine on all gathered citations — runs while thinker synthesises
    analyst_deep = await get_instance(ModelTier.ANALYST)
    slot_ad      = slot_for(ModelTier.ANALYST)
    analyst_deep_task = asyncio.create_task(
        run_analyst_engine(job.query, all_cits, job.id, analyst_deep, slot_ad,
                           nlp_tools=deep_directive.nlp_tools if deep_directive.valid else None)
    )

    # Thinker synthesises the knowledge base
    thinking = full_ctx
    if thinker:
        slot_on(slot_t, thinker, job.id, "thinking")
        job.status = JobStatus.THINKING
        proj_ctx = (f"\n\nProject context:\n{project.context_summary}" if project else "")
        await step_emit(job, "Synthesising", f"{thinker.name} integrating {len(nodes)} research nodes…")
        think_sys = (
            "You are a senior researcher synthesising a deep investigation. "
            "Integrate all findings, resolve contradictions, identify the key insights, "
            "and create a clear writing plan for a comprehensive report."
        )
        iter_ctx_d = (f"\n\nPrior research (build on, don't repeat):\n{job.prior_context[:2000]}"
                      if job.prior_context and job.context_mode == "continue" else "")
        # Await analyst engine — should be done by now (host-local is fast)
        analyst_report_deep = AnalystReport()
        try:
            analyst_report_deep = await asyncio.wait_for(analyst_deep_task, timeout=30)
        except Exception as e:
            log.warning("AnalystEngine deep failed: %s", e)
        analyst_ctx = analyst_report_deep.to_context_string() if analyst_report_deep.valid else ""

        thinking = await collect_ollama(thinker,
            f"Topic: {job.query}{proj_ctx}{iter_ctx_d}\n\n"
            f"{full_ctx[:10000]}\n\n"
            + (f"Pre-analysis from AnalystEngine (use this to improve depth):\n{analyst_ctx[:3000]}\n\n" if analyst_ctx else "")
            + "Synthesise all findings and produce a structured writing plan.",
            think_sys, job.id, slot_t, timeout_secs=THINKER_THINK_TIMEOUT)
        slot_off(slot_t)

    if cancel_flags.get(job.id): return

    # Writer produces initial draft
    slot_w = slot_for(ModelTier.WRITER)
    slot_on(slot_w, use_writer, job.id, "writing")
    job.status = JobStatus.WRITING
    await step_emit(job, "Writing", f"{use_writer.name} producing draft…")
    cit_ref = "\n".join(f"[{i+1}] {c.title} — {c.url}" for i, c in enumerate(all_cits[:40]))
    proj_ctx = (f"\n\nProject context:\n{project.context_summary}" if project else "")
    draft_parts: list[str] = []
    async for tok in stream_ollama(use_writer,
        f"Topic: {job.query}{proj_ctx}\n\n"
        f"Research synthesis:\n{thinking[:8000]}\n\n"
        f"Full knowledge base:\n{full_ctx[:6000]}\n\n"
        f"Available citations:\n{cit_ref}\n\n"
        "Write a comprehensive, deeply detailed research report. "
        "Use ## headers, cite every claim as [N], include specific facts not vague generalities. "
        "Reproduce tables from sources using markdown table syntax. "
        "Include image URLs from sources as markdown images where relevant.",
        (deep_directive.writer_sys if deep_directive.valid and deep_directive.writer_sys
         else WRITE_SYS), job.id, slot_w):
        draft_parts.append(tok)
        if cancel_flags.get(job.id): break
    slot_off(slot_w)
    draft = "".join(draft_parts)

    # Thinker plans expansions on the draft; Writer gathers them concurrently
    if thinker and draft and not cancel_flags.get(job.id):
        await step_emit(job, "Expanding", "Finding thin sections to deepen…")
        expansions = await _plan_expansions(draft, job.query, thinker, job.id, max_expansions=3)
        if expansions:
            fast_w = use_writer
            addendum = await _run_expansions(expansions, job, fast_w, full_ctx[:2000])
            if addendum:
                draft += "\n\n## Expanded Sections\n" + addendum

    if cancel_flags.get(job.id): job.result = draft; return

    # Analyst verification with timeout
    if analyst:
        job.result = await run_analyst_phase(job, draft, analyst, slot_a)
    else:
        job.result = draft

    # Append analyst report section
    if analyst_report_deep.valid:
        job.result += analyst_report_deep.to_report_section()

    # Append references
    if all_cits and "## References" not in job.result:
        refs = "\n\n## References\n\n" + "\n".join(
            f"[{i+1}] [{c.title}]({c.url})  " for i, c in enumerate(all_cits[:50], 1)
        )
        job.result += refs


async def run_parallel(job: ResearchJob, project: Optional[Project] = None):
    """
    Parallel mode — optimised for speed:
      Phase 1 (fast): WRITER decomposes query and gathers sources for ALL sub-questions
                      concurrently (no slow model needed until synthesis)
      Phase 2 (slow): THINKER synthesises the gathered facts into a final report
      File/Code/Guide: routes to the appropriate single-agent pipeline
    """
    # File/code/guide modes don't benefit from parallel gathering — route directly
    if job.output_mode in (OutputMode.CODE, OutputMode.GUIDE, OutputMode.FILESTORE):
        await run_single(job, project); return

    thinker = await get_instance(ModelTier.THINKER)
    writer  = await get_instance(ModelTier.WRITER)
    slot_t  = slot_for(ModelTier.THINKER)
    fast    = writer or thinker   # writer is the fast model
    slow    = thinker or writer   # thinker is the smart/slow synthesiser

    if not fast:
        await run_single(job, project); return

    proj_ctx = (f"\n\nProject context:\n{project.context_summary}" if project else "")
    iter_ctx = (f"\n\nPrior research (build on, don't repeat):\n{job.prior_context[:2000]}"
                if job.prior_context and job.context_mode == "continue" else "")

    # ── Phase 1a: Thinker produces ResearchDirective ──────────────────────
    # The directive contains sub_questions, output style, writer instructions,
    # scope focus, and scope exclusions. If the thinker is the same as the
    # writer (single instance), directive still runs — just uses that instance.
    job.status = JobStatus.THINKING
    slot_td = slot_for(ModelTier.THINKER)
    if thinker:
        slot_on(slot_td, thinker, job.id, "directing")
        await step_emit(job, "Directing", f"{thinker.name} analysing query…")

    directive = await build_research_directive(job.query, slow, job.id, project, job=job)
    if thinker: slot_off(slot_td)

    if directive.valid:
        await broadcast(job.id, {
            "type": "directive",
            "style": directive.output_style,
            "focus": directive.scope_focus,
            "depth": directive.depth,
        })
        await step_emit(job, "Directive",
            f"Style: {directive.output_style} · "
            f"{len(directive.key_questions)} key questions · "
            f"Depth: {directive.depth}")

    # Sub-questions from directive; fall back to writer decomposition if absent
    sub_qs: list[str] = directive.sub_questions if len(directive.sub_questions) >= 2 else []
    if not sub_qs:
        # Directive had no sub_questions — ask writer to decompose
        raw = await collect_ollama(fast, job.query,
            "Break this research query into 3-5 focused, non-overlapping sub-questions "
            "that together cover the full topic. "
            "Return ONLY a JSON array of question strings. No other text.",
            job.id, timeout_secs=60)
        try:
            parsed = json.loads(raw[raw.index("["):raw.rindex("]")+1])
            sub_qs = [str(q) for q in parsed[:5] if q]
        except Exception:
            pass
    if not sub_qs:
        sub_qs = [job.query]

    await step_emit(job, "Tasks", f"{len(sub_qs)} sub-questions · {directive.output_style} style")
    job.status = JobStatus.SEARCHING

    # ── Phase 1b: ALL sub-questions gather sources concurrently ───────────
    # Analyst starts NOW alongside the gather — it processes citations as they
    # land (the citations list is a live reference). The analyst instance (CPU)
    # runs its NLP phases while the writer (GPU) does per-sub-question extraction.
    # All three instances are busy at once.
    analyst_inst2 = await get_instance(ModelTier.ANALYST)
    slot_a2       = slot_for(ModelTier.ANALYST)
    analyst_task  = asyncio.create_task(
        run_analyst_engine(job.query, job.citations, job.id,
                           analyst_inst2, slot_a2,
                           nlp_tools=directive.nlp_tools if directive.valid else None)
    )
    log.info("AnalystEngine task started for parallel job %s", job.id)

    async def gather_and_extract(q: str, idx: int) -> tuple[str, str]:
        """Returns (question, extracted_findings)."""
        await broadcast(job.id, {"type":"step","t":time.time(),
                                  "label":f"Search {idx+1}/{len(sub_qs)}","detail":q[:70]})
        class _SJ:
            id = job.id; sources = job.sources; citations: list = []
        try:
            fast_p = await get_instance(ModelTier.WRITER) or await get_instance(ModelTier.THINKER)
            cits, ctx = await smart_gather(q, _SJ(), fast=fast_p, directive=directive)
        except Exception:
            cits, ctx = [], ""
        # Filter arXiv before merging — directive is known at this point
        cits = _filter_arxiv(cits, directive)
        # Merge citations into the live job.citations list (analyst sees these)
        existing = {c.url for c in job.citations}
        new_cits = [c for c in cits if c.url not in existing]
        job.citations.extend(new_cits)
        if new_cits:
            await broadcast(job.id, {"type":"citations",
                                      "citations":[c.to_dict() for c in new_cits]})
        if not ctx:
            return q, f"No sources found for: {q}"
        # Writer (GPU) extracts key facts — directive shapes what to extract
        extract_suffix = directive.extraction_prompt_suffix()
        findings_parts: list[str] = []
        async for tok in stream_ollama(fast,
            f"Sub-question: {q}\n\nSources:\n{ctx[:4000]}",
            "Extract key facts and findings that answer this sub-question. "
            "Dense bullet points. Cite sources as [1],[2]. No intro or conclusion."
            + extract_suffix,
            job.id, timeout_secs=WRITER_TIMEOUT):
            findings_parts.append(tok)
            if cancel_flags.get(job.id): break
        return q, "".join(findings_parts)

    job.status = JobStatus.WRITING
    results = await asyncio.gather(*[gather_and_extract(q,i) for i,q in enumerate(sub_qs)])
    if cancel_flags.get(job.id):
        job.result = "\n\n".join(f"## {q}\n{f}" for q,f in results); return

    # ── Phase 2a: Collect analyst (was running during gather) ────────────────
    combined = "\n\n".join(f"### Sub-question: {q}\n{f}" for q, f in results)

    ar2 = AnalystReport()
    analyst_timeout2 = (
        _effective_timeout(analyst_inst2, ANALYST_TIMEOUT)
        if analyst_inst2 else 600.0
    )
    try:
        ar2 = await asyncio.wait_for(analyst_task, timeout=analyst_timeout2)
        if ar2.valid:
            log.info("AnalystEngine parallel: %d bullets, %d contradictions, "
                     "%d gaps, %d timeline — %.2fs",
                     len(ar2.knowledge_bullets), len(ar2.contradictions),
                     len(ar2.gaps), len(ar2.timeline), ar2.elapsed)
            await step_emit(job, "Analysis",
                f"{len(ar2.knowledge_bullets)} findings · "
                f"{len(ar2.contradictions)} contradictions · "
                f"{len(ar2.gaps)} gaps · {ar2.elapsed:.1f}s")
            seen = {c.url for c in job.citations}
            for c in ar2.gap_fill_cits:
                if c.url not in seen:
                    job.citations.append(c); seen.add(c.url)
        else:
            log.warning("AnalystEngine parallel: report.valid=False "
                        "(citations=%d, chars=%d)",
                        len(job.citations),
                        sum(len(c.full_text or "") + len(c.snippet or "")
                            + len(c.title or "") for c in job.citations))
    except asyncio.TimeoutError:
        log.warning("AnalystEngine parallel timed out after %.0fs", analyst_timeout2)
        if not analyst_task.done(): analyst_task.cancel()
    except Exception as e:
        log.warning("AnalystEngine parallel exception: %s", e)

    analyst_ctx = ar2.to_context_string() if ar2.valid else ""
    gap_fill_ctx = ""
    if ar2.gap_fill_cits:
        gap_fill_ctx = ("\n\n### Gap-fill Sources\n"
            + "\n".join(f"- {c.title[:60]}: {c.snippet[:120]}\n  URL: {c.url}"
                          for c in ar2.gap_fill_cits[:5]))

    cit_ref = "\n".join(f"[{i+1}] {c.title} — {c.url}"
                          for i, c in enumerate(job.citations[:40]))

    # ── Phase 2b: Thinker (CPU) produces synthesis plan ──────────────────────
    # Thinker reads the analyst compact context and produces a structured writing
    # plan. This runs as a fast collect (not streaming) so the GPU writer can
    # then stream the full report using both the plan and analyst context.
    synth_plan = ""
    if thinker and thinker is not slow:
        # thinker and slow are different instances — run plan concurrently
        pass  # plan runs in Phase 2c below sequentially (can't stream two at once)

    if slow is not writer:
        # Thinker (CPU) produces the plan; writer (GPU) will stream the report
        slot_on(slot_t, slow, job.id, "planning")
        await step_emit(job, "Planning", f"{slow.name} building report structure…")
        plan_input = (
            f"Query: {job.query}{proj_ctx}{iter_ctx}\n\n"
            + (f"{analyst_ctx}\n\n" if analyst_ctx else
               f"Sub-question findings:\n{combined[:3000]}\n\n")
            + "Produce a tight markdown report outline: ## section headings with "
            "1-2 line notes. No prose. Max 250 words."
        )
        try:
            synth_plan = await asyncio.wait_for(
                collect_ollama(slow, plan_input,
                    "You produce research report outlines. Return only markdown headings.",
                    job.id, slot_t,
                    timeout_secs=_effective_timeout(slow, THINKER_PLAN_TIMEOUT)),
                timeout=_effective_timeout(slow, THINKER_PLAN_TIMEOUT + 10)
            )
        except Exception as e:
            log.debug("Synthesis plan failed: %s", e)
        slot_off(slot_t)

    # ── Phase 2c: WRITER (GPU) streams the final report ──────────────────────
    # Writer gets: directive header (style+scope) + analyst compact context
    # + synthesis plan + raw findings. Uses directive-tailored writer_sys.
    directive_hdr2 = directive.to_context_header() if directive.valid else ""
    active_writer_sys2 = (directive.writer_sys if directive.valid and directive.writer_sys
                          else WRITE_SYS)
    if analyst_ctx or synth_plan or directive_hdr2:
        synth_prompt = (
            f"Query: {job.query}{proj_ctx}{iter_ctx}\n\n"
            + (directive_hdr2 + "\n" if directive_hdr2 else "")
            + (f"{analyst_ctx}\n\n" if analyst_ctx else "")
            + (f"Report structure to follow:\n{synth_plan}\n\n" if synth_plan else "")
            + f"Supporting findings (cite these):\n{combined[:4000]}{gap_fill_ctx}\n\n"
            + f"Citations:\n{cit_ref}\n\n"
            + "Write a research output following the directive above. "
            + "Cite every claim as [N]. "
            + "Reproduce tables using markdown table syntax. "
            + "Address every research gap. "
            + "Resolve contradictions explicitly."
        )
    else:
        synth_prompt = (
            f"Query: {job.query}{proj_ctx}{iter_ctx}\n\n"
            f"Gathered findings from {len(sub_qs)} sub-investigations:\n"
            f"{combined[:8000]}\n\n"
            f"Citations:\n{cit_ref}\n\n"
            "Synthesise ALL findings into a comprehensive, well-structured report. "
            "Use ## headers, cite every claim as [N], resolve any contradictions. "
            "Reproduce tables from sources using markdown table syntax."
        )

    # Use writer (GPU) for the streaming final output with tailored system prompt
    final_writer = writer or slow
    slot_w2 = slot_for(ModelTier.WRITER)
    slot_on(slot_w2, final_writer, job.id, "writing")
    await step_emit(job, "Writing",
        f"{final_writer.name} [{directive.output_style}] producing report…")
    job.status = JobStatus.WRITING
    synth_parts: list[str] = []
    async for tok in stream_ollama(final_writer, synth_prompt, active_writer_sys2, job.id, slot_w2):
        synth_parts.append(tok)
        if cancel_flags.get(job.id): break
    slot_off(slot_w2)
    job.result = "".join(synth_parts)

    if ar2.valid:
        job.result += ar2.to_report_section()


async def run_job(job: ResearchJob):
    project: Optional[Project] = None
    if job.project_id and job.project_id in projects:
        project = projects[job.project_id]

    try:
        cancel_flags[job.id] = False
        await broadcast(job.id, {"type":"status","status":job.status,"job_id":job.id})

        if   job.mode == AgentMode.DEEP:     await run_deep(job, project)
        elif job.mode == AgentMode.PARALLEL: await run_parallel(job, project)
        else:                                await run_single(job, project)

        job.status = JobStatus.CANCELLED if cancel_flags.get(job.id) else JobStatus.DONE

        # Update project context
        if project:
            thinker = await get_instance(ModelTier.THINKER)
            await update_project_context(project, job, thinker)

    except Exception as e:
        log.exception("Job %s failed", job.id)
        job.status = JobStatus.ERROR; job.error = str(e)
    finally:
        job.finished_at = time.time()
        job.token_count = sum(s.tokens for s in agent_slots)
        cancel_flags.pop(job.id, None)
        history.insert(0, job)
        if len(history) > 200: history.pop()
        chain_info: dict = {}
        if job.chain_ctx:
            chain_info = {
                "chain_id":       job.chain_ctx.chain_id,
                "run_number":     job.chain_ctx.run_number,
                "files_done":     job.chain_ctx.files_done,
                "files_pending":  job.chain_ctx.files_pending,
                "is_complete":    job.chain_ctx.is_complete,
                "chain_continues": job.chain_continues,
            }
        await broadcast(job.id, {
            "type":"done","status":job.status,"job_id":job.id,
            "result":job.result or "","error":job.error or "",
            "elapsed":round(job.finished_at-job.created_at,1),
            "tokens":job.token_count,
            "citations":[c.to_dict() for c in job.citations],
            "file_tree":list(job.file_tree.keys()),
            **chain_info,
        })
        jobs.pop(job.id, None)

    # ── Persist to database ───────────────────────────────────────────────────
    try:
        await DB.save_job(job)
        if project:
            await DB.save_project(project)
    except Exception as e:
        log.error("DB save failed for job %s: %s", job.id, e)

    # ── Save chain context for continuation ───────────────────────────────────
    if job.chain_ctx and not job.chain_ctx.is_complete:
        chain_store[job.chain_ctx.chain_id] = job.chain_ctx
        log.info("Chain %s saved: %d done, %d pending",
                 job.chain_ctx.chain_id,
                 len(job.chain_ctx.files_done),
                 len(job.chain_ctx.files_pending))


# ══════════════════════════════════════════════════════════════════════════════
#  Pydantic models
# ══════════════════════════════════════════════════════════════════════════════



class ResearchRequest(BaseModel):
    query:          str        = Field(..., min_length=1, max_length=8000)
    mode:           AgentMode  = AgentMode.SINGLE
    output_mode:    OutputMode = OutputMode.REPORT
    sources:        list[str]  = Field(default_factory=list)
    project_id:     Optional[str] = None
    context:        Optional[str] = None   # prior research text to include as context
    context_mode:   str = "fresh"          # "fresh" | "continue" — whether to include prior context

class ChainContinueRequest(BaseModel):
    """Trigger the next run of a chained coding job."""
    chain_id:   str                       # from chain_continue WS message
    job_id:     str                       # original job that produced the chain
    project_id: Optional[str] = None

class SourceTestRequest(BaseModel):
    source_id: str

class SourceUpdateRequest(BaseModel):
    sources: list[dict]

class InstanceUpdateRequest(BaseModel):
    instances: list[dict]

class WebSearchConfigRequest(BaseModel):
    engine:        Optional[str]   = None
    result_count:  Optional[int]   = None
    crawl_depth:   Optional[int]   = None
    crawl_breadth: Optional[int]   = None
    crawl_timeout: Optional[float] = None
    include_archive: Optional[bool]= None
    safe_search:   Optional[int]   = None

class ProjectCreateRequest(BaseModel):
    name:        str
    description: str = ""
    output_mode: OutputMode = OutputMode.REPORT


# ══════════════════════════════════════════════════════════════════════════════
#  Config file helpers  (vera_config.json — fallback when DB empty on restart)
# ══════════════════════════════════════════════════════════════════════════════

_CFG_FILE = Path("vera_config.json")


def _load_config_file() -> dict:
    if _CFG_FILE.exists():
        try:
            with open(_CFG_FILE) as f:
                data = json.load(f)
            log.info("Config fallback loaded from %s", _CFG_FILE)
            return data
        except Exception as e:
            log.warning("Cannot read %s: %s", _CFG_FILE, e)
    return {}


def _write_config_file() -> None:
    """Snapshot current instances/sources/web_cfg → vera_config.json."""
    try:
        data = {
            "instances": [
                {"name": i.name, "host": i.host, "port": i.port,
                 "tier": i.tier.value, "model": i.model,
                 "ctx_size": i.ctx_size, "enabled": i.enabled}
                for i in instances
            ],
            "sources": [
                {"id": s.id, "label": s.label, "type": s.type.value,
                 "enabled": s.enabled, "config": s.config, "status": s.status}
                for s in sources
            ],
            "web_cfg": {
                "engine": web_cfg.engine, "result_count": web_cfg.result_count,
                "crawl_depth": web_cfg.crawl_depth, "crawl_breadth": web_cfg.crawl_breadth,
                "crawl_timeout": web_cfg.crawl_timeout,
                "include_archive": web_cfg.include_archive, "safe_search": web_cfg.safe_search,
            },
        }
        with open(_CFG_FILE, "w") as f:
            json.dump(data, f, indent=2)
        log.debug("Config snapshot → %s", _CFG_FILE)
    except Exception as e:
        log.warning("Cannot write %s: %s", _CFG_FILE, e)


# ══════════════════════════════════════════════════════════════════════════════
#  App
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Database ──────────────────────────────────────────────────────────────
    await DB.init()
    log.info("Database ready")

    global sources, instances, web_cfg

    def _apply_file_cfg(fc: dict) -> None:
        global sources, instances, web_cfg
        if fc.get("sources"):
            loaded = []
            for row in fc["sources"]:
                try:
                    loaded.append(DataSource(
                        id=row["id"], label=row["label"], type=SourceType(row["type"]),
                        enabled=bool(row.get("enabled", True)),
                        config=row.get("config", {}), status=row.get("status", "unknown"),
                    ))
                except Exception as e:
                    log.warning("Config-file source skip %s: %s", row.get("id"), e)
            if loaded:
                sources = loaded
                log.info("Loaded %d sources from config file", len(sources))
        if fc.get("instances"):
            loaded = []
            for row in fc["instances"]:
                try:
                    loaded.append(OllamaInstance(
                        name=row["name"], host=row["host"], port=int(row["port"]),
                        tier=ModelTier(row["tier"]), model=row["model"],
                        ctx_size=int(row.get("ctx_size", 8192)),
                        enabled=bool(row.get("enabled", True)),
                    ))
                except Exception as e:
                    log.warning("Config-file instance skip %s: %s", row.get("name"), e)
            if loaded:
                instances = loaded
                log.info("Loaded %d instances from config file", len(instances))
        if fc.get("web_cfg"):
            wc = fc["web_cfg"]
            web_cfg.engine        = wc.get("engine", web_cfg.engine)
            web_cfg.result_count  = int(wc.get("result_count", web_cfg.result_count))
            web_cfg.crawl_depth   = int(wc.get("crawl_depth", web_cfg.crawl_depth))
            web_cfg.crawl_breadth = int(wc.get("crawl_breadth", web_cfg.crawl_breadth))
            web_cfg.crawl_timeout = float(wc.get("crawl_timeout", web_cfg.crawl_timeout))
            web_cfg.include_archive = bool(wc.get("include_archive", False))
            web_cfg.safe_search   = int(wc.get("safe_search", 0))
            log.info("Loaded web search config from config file")

    # ── Load persisted sources ────────────────────────────────────────────────
    saved_sources = await DB.load_sources()
    loaded_sources = []
    for row in saved_sources:
        try:
            loaded_sources.append(DataSource(
                id=row["id"], label=row["label"],
                type=SourceType(row["type"]),
                enabled=bool(row["enabled"]),
                config=row.get("config", {}),
                status=row.get("status", "unknown"),
            ))
        except Exception as e:
            log.warning("Skipping DB source %s (type=%r): %s", row.get("id"), row.get("type"), e)
    if loaded_sources:
        sources = loaded_sources
        log.info("Loaded %d sources from DB", len(sources))
    else:
        log.info("No valid sources in DB — trying %s", _CFG_FILE)
        _apply_file_cfg(_load_config_file())

    # ── Load persisted instances ──────────────────────────────────────────────
    saved_insts = await DB.load_instances()
    loaded_insts = []
    for row in saved_insts:
        try:
            loaded_insts.append(OllamaInstance(
                name=row["name"], host=row["host"], port=int(row["port"]),
                tier=ModelTier(row["tier"]), model=row["model"],
                ctx_size=int(row.get("ctx_size", 8192)),
                enabled=bool(row.get("enabled", True)),
            ))
        except Exception as e:
            log.warning("Skipping DB instance %s (tier=%r): %s", row.get("name"), row.get("tier"), e)
    if loaded_insts:
        instances = loaded_insts
        log.info("Loaded %d instances from DB", len(instances))
    else:
        log.info("No valid instances in DB — trying %s", _CFG_FILE)
        _apply_file_cfg(_load_config_file())

    # ── Load persisted web search config ──────────────────────────────────────
    saved_ws = await DB.load_web_search_config()
    if saved_ws:
        web_cfg.engine        = saved_ws.get("engine", web_cfg.engine)
        web_cfg.result_count  = int(saved_ws.get("result_count", web_cfg.result_count))
        web_cfg.crawl_depth   = int(saved_ws.get("crawl_depth", web_cfg.crawl_depth))
        web_cfg.crawl_breadth = int(saved_ws.get("crawl_breadth", web_cfg.crawl_breadth))
        web_cfg.crawl_timeout = float(saved_ws.get("crawl_timeout", web_cfg.crawl_timeout))
        web_cfg.include_archive = bool(saved_ws.get("include_archive", False))
        web_cfg.safe_search   = int(saved_ws.get("safe_search", 0))
        log.info("Loaded web search config from DB")
    else:
        _apply_file_cfg(_load_config_file())

    # Write a fresh snapshot so the file is always current
    _write_config_file()

    # ── Load persisted projects into memory ───────────────────────────────────
    saved_projects = await DB.load_projects()
    for row in saved_projects:
        if row["id"] not in projects:
            proj = Project(
                id=row["id"], name=row["name"],
                description=row.get("description", ""),
                output_mode=OutputMode.REPORT,
                context_summary=row.get("context_summary", ""),
                created_at=float(row.get("created_at", time.time())),
                updated_at=float(row.get("updated_at", time.time())),
            )
            projects[proj.id] = proj
    log.info("Loaded %d projects from DB", len(projects))

    # ── Load persisted bookmarks into memory ──────────────────────────────────
    saved_bmarks = await DB.load_bookmarks()
    for bm in saved_bmarks:
        bookmarks[bm["id"]] = bm
    log.info("Loaded %d bookmarks from DB", len(bookmarks))

    # ── Probe Ollama instances ────────────────────────────────────────────────
    log.info("Vera Researcher v4 starting on port 8765…")
    for inst in instances:
        mods = await list_models(inst)
        log.info("  %-10s %-28s %s", inst.name, inst.base_url,
                 f"{len(mods)} models" if mods else "UNREACHABLE")

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    # Close playwright browser if it was launched
    global _pw_browser
    if _pw_browser is not None:
        try:
            await _pw_browser.close()
            log.info("Playwright browser closed")
        except Exception:
            pass
        _pw_browser = None
    await DB.close()
    log.info("Vera Researcher shut down")


# In Vera mode, routes are registered on the orchestrator's APP.
# In standalone mode, create our own FastAPI app.
if _VERA_MODE:
    app = _VERA_APP
    # Orchestrator already has CORS + is already started — don't touch it.
    # Screenshots mount goes in the capability registration block below
    # (after routes are set up, before the server is fully live).
else:
    app = FastAPI(title="Vera Research Agent", version="3.0.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    try:
        app.mount("/screenshots", StaticFiles(directory=str(SCREENSHOT_DIR)), name="screenshots")
    except Exception:
        pass  # screenshots mount may fail if dir doesn't exist yet


# ══════════════════════════════════════════════════════════════════════════════
#  Routes — Research
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/research")
async def start_research(req: ResearchRequest, bg: BackgroundTasks):
    job = ResearchJob(
        id=str(uuid.uuid4()), query=req.query, mode=req.mode,
        output_mode=req.output_mode,
        sources=req.sources or [s.id for s in sources if s.enabled],
        status=JobStatus.QUEUED, created_at=time.time(),
        project_id=req.project_id,
        prior_context=req.context or "",
        context_mode=req.context_mode or "fresh",
    )
    jobs[job.id] = job
    # Save stub immediately so the job appears in Library even while running
    try:
        await DB.save_job(job)
    except Exception as e:
        log.warning("Early job save failed: %s", e)
    bg.add_task(run_job, job)
    return {"job_id":job.id,"status":job.status}


# In-memory chain store: chain_id → ChainContext
# (populated at end of each CODE run that needs continuation)
chain_store: dict[str, ChainContext] = {}


@app.post("/api/research/continue")
async def continue_chain(req: ChainContinueRequest, bg: BackgroundTasks):
    """
    Trigger the next run of a chained coding job.
    The frontend sends the chain_id received in the chain_continue WS message.
    """
    chain = chain_store.get(req.chain_id)
    if not chain:
        # Try to reconstruct from the original job's history
        orig = await DB.load_job_result(req.job_id)
        if not orig:
            raise HTTPException(404, f"Chain '{req.chain_id}' not found — original job may have been deleted")
        raise HTTPException(410, "Chain context expired from memory — restart the coding job")

    if chain.is_complete:
        return {"ok": False, "reason": "Chain is already complete"}

    if not chain.files_pending:
        chain.is_complete = True
        chain_store.pop(req.chain_id, None)
        return {"ok": False, "reason": "No files pending"}

    # Create a continuation job with the existing chain context
    cont_job = ResearchJob(
        id          = str(uuid.uuid4()),
        query       = chain.original_task,
        mode        = AgentMode.DEEP,      # always use all agents for coding
        output_mode = OutputMode.CODE,
        sources     = [],                  # no re-search needed
        status      = JobStatus.QUEUED,
        created_at  = time.time(),
        project_id  = req.project_id,
        chain_ctx   = chain,
    )
    jobs[cont_job.id] = cont_job
    bg.add_task(run_job, cont_job)
    return {
        "job_id":        cont_job.id,
        "chain_id":      chain.chain_id,
        "run_number":    chain.run_number,
        "files_pending": chain.files_pending,
        "files_done":    chain.files_done,
    }


@app.get("/api/research/chain/{chain_id}")
async def get_chain_status(chain_id: str):
    """Return current state of a chain."""
    chain = chain_store.get(chain_id)
    if not chain:
        raise HTTPException(404, "Chain not found")
    return {
        "chain_id":      chain.chain_id,
        "run_number":    chain.run_number,
        "original_task": chain.original_task,
        "files_planned": chain.files_planned,
        "files_done":    chain.files_done,
        "files_pending": chain.files_pending,
        "is_complete":   chain.is_complete,
        "summary":       chain.continuity_summary,
    }


class CrawlRequest(BaseModel):
    url:   str
    depth: int = 2


@app.post("/api/research/{job_id}/crawl")
async def trigger_crawl(job_id: str, req: CrawlRequest, bg: BackgroundTasks):
    """Tag a URL for deep crawl and stream results back into the job's WS channel."""
    async def _do(url: str, depth: int, jid: str):
        await broadcast(jid, {"type":"step","t":time.time(),
                               "label":"Deep crawl","detail":url[:80]})
        text = await deep_crawl_url(url, depth, web_cfg.crawl_breadth,
                                     web_cfg.crawl_timeout, job_id=jid)
        if text:
            # Create a citation for this crawl result and broadcast it
            from urllib.parse import urlparse
            dom = urlparse(url).netloc
            cit = Citation(id=str(uuid.uuid4())[:8], url=url,
                           title=f"Crawled: {dom}", snippet=text[:300],
                           source_type="crawl", full_text=text)
            await broadcast(jid, {"type":"citations","citations":[cit.to_dict_full()]})
            await broadcast(jid, {"type":"crawl_done","url":url,
                                   "chars":len(text),"title":cit.title})
            # Persist to DB if job exists
            job = jobs.get(jid)
            if job:
                job.citations.append(cit)
                try: await DB.save_job(job)
                except Exception: pass
        else:
            await broadcast(jid, {"type":"crawl_done","url":url,"chars":0,"title":url})
    bg.add_task(_do, req.url, req.depth, job_id)
    return {"ok": True, "message": f"Crawling {req.url} at depth {req.depth}"}



class ResearchChatRequest(BaseModel):
    message:       str
    context:       str = ""
    citations_ctx: str = ""
    mode:          AgentMode = AgentMode.SINGLE


@app.post("/api/research/chat")
async def research_chat(req: ResearchChatRequest):
    """Chat against the current research result — streams SSE tokens."""
    writer = await get_instance(ModelTier.WRITER) or await get_instance(ModelTier.THINKER)
    if not writer:
        raise HTTPException(503, "No model available")
    sys_p = (
        "You are a research assistant. The user has completed a research session "
        "and wants to ask follow-up questions or dive deeper. "
        "Use ONLY the provided research context to answer. "
        "If the answer is not in the context, say so and suggest a new search query. "
        "Be concise and cite sections of the research where relevant."
    )
    cit_block = ("Citations:\n" + req.citations_ctx[:2000]) if req.citations_ctx else ""
    prompt = (
        f"Research context:\n{req.context[:6000]}\n\n"
        f"{cit_block}\n\n"
        f"User question: {req.message}\n\n"
        "Answer using the research context above."
    )

    async def _stream():
        async for tok in stream_ollama(writer, prompt, sys_p, "chat", timeout_secs=WRITER_TIMEOUT):
            yield f"data: {json.dumps({'token': tok})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


# ─────────────────────────────────────────────────────────────────────────────
#  Document Format Agent — reformats a raw research section into a
#  professional, publication-quality document page.
#
#  Runs on the WRITER (fast model) so it completes while the user reads.
#  Uses the analyst instance if no writer is available.
#
#  The output is HTML (rendered markdown) ready to drop into #corpus-body.
# ─────────────────────────────────────────────────────────────────────────────

class FormatSectionRequest(BaseModel):
    query:    str
    raw_text: str
    cits:     list[dict] = []

DOC_FORMAT_SYS = (
    "You are a professional document editor. You receive a research section "
    "in raw markdown and reformat it into a clean, publication-quality document. "
    "\n\nRules:\n"
    "- Keep ALL factual content and citations — do not drop any information\n"
    "- Improve structure: add or promote headings (## and ###) to break up long sections\n"
    "- Open with a 1-2 sentence executive summary paragraph before the first heading\n"
    "- Convert bullet-point lists into flowing prose paragraphs where appropriate\n"
    "- Keep bullet lists for genuinely enumerable items (steps, features, specs)\n"
    "- Ensure tables use proper markdown table syntax with aligned columns\n"
    "- Wrap inline code in backticks, code blocks in triple-backtick fences with language\n"
    "- Preserve all citation references [1], [2] etc. exactly as-is\n"
    "- Do NOT add invented content, do NOT remove citations, do NOT summarise\n"
    "- End with a clean ## References section if citations are provided\n"
    "- Output clean markdown only — no preamble, no explanation"
)


@app.post("/api/research/format_section")
async def format_section(req: FormatSectionRequest):
    """
    Reformat a raw research section into publication-quality markdown.
    Uses the writer (fast model) so it completes in seconds.
    Returns {"html": "<rendered html>", "markdown": "<formatted md>"}.
    """
    writer = (await get_instance(ModelTier.WRITER)
              or await get_instance(ModelTier.ANALYST)
              or await get_instance(ModelTier.THINKER))
    if not writer:
        raise HTTPException(503, "No model available for document formatting")

    cit_block = ""
    if req.cits:
        cit_block = "\n\nCitations available:\n" + "\n".join(
            f"[{i+1}] {c.get('title','?')} — {c.get('url','')}"
            for i, c in enumerate(req.cits[:30])
        )

    prompt = (
        f"Research query: {req.query}\n\n"
        f"Raw section content:\n{req.raw_text[:8000]}"
        f"{cit_block}\n\n"
        "Reformat the above into a clean, professional document section."
    )

    try:
        formatted_md = await asyncio.wait_for(
            collect_ollama(writer, prompt, DOC_FORMAT_SYS,
                           timeout_secs=_effective_timeout(writer, WRITER_TIMEOUT)),
            timeout=_effective_timeout(writer, WRITER_TIMEOUT + 30)
        )
        # Render markdown → HTML for the client
        html = _md_to_html(formatted_md)
        return {"html": html, "markdown": formatted_md}
    except Exception as e:
        log.warning("format_section failed: %s", e)
        raise HTTPException(500, f"Format failed: {e}")




@app.post("/api/agent/stop")
async def stop_agent(payload: dict):
    jid = payload.get("job_id","")
    if jid in cancel_flags: cancel_flags[jid]=True; return {"ok":True}
    return {"ok":False,"reason":"job not found"}


@app.get("/api/agents/status")
async def agent_status():
    return {"slots":[{"id":s.id,"tier":s.tier,"status":s.status,"model":s.model,
        "tokens":s.tokens,"job_id":s.job_id,
        "elapsed":round(time.time()-s.started_at,1) if s.started_at and s.status!="idle" else None}
        for s in agent_slots]}


# ── History ──────────────────────────────────────────────────────────────────

@app.get("/api/history")
async def get_history(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    project_id: Optional[str] = None,
    search: Optional[str] = None,
):
    """
    Load research history from the database.
    Supports pagination, project filtering, and full-text search.
    In-flight jobs (not yet in DB) are prepended from in-memory state.
    """
    db_rows, _total = await DB.load_history(
        limit=limit, offset=offset,
        project_id=project_id, search=search,
    )

    # Prepend any currently running jobs not yet flushed to DB
    live = [
        {
            "id": j.id, "query": j.query, "mode": j.mode,
            "output_mode": j.output_mode, "status": j.status,
            "created_at": j.created_at, "finished_at": None,
            "token_count": 0, "citation_count": 0, "has_files": False,
            "error": None, "result_snippet": "Running…",
        }
        for j in jobs.values()
        if not any(r.get("id") == j.id for r in db_rows)
    ]

    return live + db_rows


@app.delete("/api/history/{job_id}")
async def delete_history(job_id: str):
    deleted = await DB.delete_job(job_id)
    # Also remove from in-memory list if present
    global history
    history = [j for j in history if j.id != job_id]
    return {"deleted": deleted}


@app.get("/api/history/{job_id}/result")
async def get_result(job_id: str):
    # Check in-memory first (job might still be running / just finished)
    mem_job = next((j for j in history if j.id == job_id), None)
    if mem_job:
        manifest = await DB.list_generated_files(job_id)
        return {
            "result":        mem_job.result,
            "steps":         mem_job.steps,
            "citations":     [c.to_dict() for c in mem_job.citations],
            "mode":          mem_job.mode,
            "output_mode":   mem_job.output_mode,
            "elapsed":       round((mem_job.finished_at or mem_job.created_at) - mem_job.created_at, 1),
            "tokens":        mem_job.token_count,
            "file_tree":     list(mem_job.file_tree.keys()),
            "file_manifest": manifest,
        }
    # Fall back to DB
    row = await DB.load_job_result(job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    return row


@app.get("/api/history/{job_id}/files")
async def list_job_files(job_id: str):
    """List generated file manifest (path + size, no content)."""
    manifest = await DB.list_generated_files(job_id)
    return {"job_id": job_id, "files": manifest}


@app.get("/api/history/{job_id}/files/{file_path:path}")
async def get_job_file(job_id: str, file_path: str):
    """Download a single generated file by path."""
    from fastapi.responses import Response as FR
    content = await DB.get_generated_file(job_id, file_path)
    if content is None:
        # Fall back to on-disk copy
        disk_path = PROJECTS_DIR / "standalone" / job_id / "files" / file_path
        if disk_path.exists():
            content = disk_path.read_text(encoding="utf-8", errors="replace")
        else:
            raise HTTPException(404, f"File '{file_path}' not found")
    ext = Path(file_path).suffix.lower()
    ct = {
        ".py":"text/x-python",".js":"text/javascript",".ts":"text/typescript",
        ".html":"text/html",".css":"text/css",".json":"application/json",
        ".yaml":"text/yaml",".yml":"text/yaml",".md":"text/markdown",
        ".sh":"text/x-sh",".toml":"text/plain",".env":"text/plain",
        ".txt":"text/plain",".xml":"text/xml",".sql":"text/x-sql",
        ".rs":"text/x-rust",".go":"text/x-go",".dockerfile":"text/plain",
    }.get(ext, "text/plain")
    return FR(content=content, media_type=ct,
              headers={"Content-Disposition": f'attachment; filename="{Path(file_path).name}"'})


@app.get("/api/history/{job_id}/files.zip")
async def download_job_files_zip(job_id: str):
    """Download all generated files for a job as a ZIP."""
    import io, zipfile
    from fastapi.responses import Response as FR
    files = await DB.load_generated_files(job_id)
    if not files:
        # Fall back to on-disk
        disk = PROJECTS_DIR / "standalone" / job_id / "files"
        if disk.exists():
            zip_path = PROJECTS_DIR / "standalone" / job_id / "files.zip"
            shutil.make_archive(str(zip_path.with_suffix("")), "zip", str(disk))
            return FileResponse(str(zip_path), media_type="application/zip",
                                filename=f"job_{job_id[:8]}_files.zip")
        raise HTTPException(404, "No generated files for this job")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    buf.seek(0)
    return FR(content=buf.read(), media_type="application/zip",
              headers={"Content-Disposition": f'attachment; filename="job_{job_id[:8]}_files.zip"'})




# ── Sources ───────────────────────────────────────────────────────────────────

@app.get("/api/sources")
async def get_sources():
    seen = set()
    deduped = []
    for s in sources:
        if s.id not in seen:
            seen.add(s.id)
            deduped.append(asdict(s))
    return deduped


@app.post("/api/sources/update")
async def update_sources(req: SourceUpdateRequest):
    """Full replace of sources list from UI."""
    global sources
    new: list[DataSource] = []
    _seen: set = set()
    for d in req.sources:
        try:
            sid = d["id"]
            if sid in _seen:
                continue
            _seen.add(sid)
            new.append(DataSource(
                id=sid, label=d["label"],
                type=SourceType(d["type"]), enabled=bool(d.get("enabled",True)),
                config=d.get("config",{}), status=d.get("status","unknown"),
            ))
        except Exception as e:
            raise HTTPException(400, f"Invalid source: {e}")
    sources = new
    await DB.save_sources(sources)
    _write_config_file()
    return {"ok":True,"count":len(sources)}


@app.post("/api/sources/add")
async def add_source(d: dict):
    """Add a single source and persist immediately."""
    try:
        src = DataSource(id=d["id"],label=d["label"],type=SourceType(d["type"]),
                         enabled=bool(d.get("enabled",True)),config=d.get("config",{}))
        # Remove any existing source with the same id first
        global sources
        sources = [s for s in sources if s.id != src.id]
        sources.append(src)
        await DB.save_sources(sources)
        _write_config_file()
        return {"ok":True}
    except Exception as e:
        raise HTTPException(400,str(e))


@app.delete("/api/sources/{source_id}")
async def delete_source(source_id:str):
    global sources
    before=len(sources); sources=[s for s in sources if s.id!=source_id]
    return {"deleted":before-len(sources)}


@app.post("/api/sources/test")
async def test_source(req: SourceTestRequest):
    src=next((s for s in sources if s.id==req.source_id),None)
    if not src: raise HTTPException(404,"Source not found")
    ok,detail=False,"Not implemented"

    # Robust config access — config may be a JSON string after a fabric round-trip
    cfg = src.config if isinstance(src.config, dict) else {}
    if isinstance(src.config, str):
        try: cfg = json.loads(src.config)
        except Exception: cfg = {}

    if src.id=="searxng":
        host=cfg.get("host","http://llm.int:8888")
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r=await c.get(f"{host.rstrip('/')}/search",
                    params={"q":"test","format":"json","language":"en"})
                data=r.json()
                count=len(data.get("results",[]))
                ok=r.status_code<400 and count>0
                detail=f"HTTP {r.status_code} · {count} results · host={host}"
        except Exception as e: detail=f"{type(e).__name__}: {e} (host={host})"

    elif src.id=="brave":
        try:
            key=cfg.get("api_key","")
            if not key: detail="No API key configured"; ok=False
            else:
                async with httpx.AsyncClient(timeout=8.0) as c:
                    r=await c.get("https://api.search.brave.com/res/v1/web/search",
                        params={"q":"test","count":1},
                        headers={"Accept":"application/json","X-Subscription-Token":key})
                    ok=r.status_code==200; detail=f"HTTP {r.status_code}"
        except Exception as e: detail=f"{type(e).__name__}: {e}"

    elif src.type==SourceType.NEO4J:
        uri=cfg.get("uri","bolt://localhost:7687")
        user=cfg.get("user","neo4j")
        password=cfg.get("password","")

        # ── Strategy A: try Vera session driver first (no new connection needed) ──
        vera_tried=False
        try:
            from Vera.ChatUI.api.session import sessions as _vsess, get_or_create_vera as _gv  # type:ignore
            if _vsess:
                sid=sorted(_vsess.keys(),reverse=True)[0]
                vera=_gv(sid)
                drv=vera.mem.graph._driver  # sync driver
                with drv.session() as db:
                    n=db.run("MATCH (n) RETURN count(n) AS n").single()["n"]
                    # Run a sample text search to prove it actually returns data
                    sample=list(db.run(
                        "MATCH (n) WHERE n.text IS NOT NULL OR n.name IS NOT NULL "
                        "RETURN coalesce(n.text,n.name,'') AS t LIMIT 3"
                    ))
                    sample_texts=[r["t"][:40] for r in sample if r["t"]]
                ok=True
                detail=(f"Connected via Vera session · {n} nodes"
                        +(f" · samples: {sample_texts}" if sample_texts else " · no text nodes found"))
                vera_tried=True
        except Exception as ve:
            log.debug("neo4j via Vera session: %s", ve)

        # ── Strategy B: direct async connection ──────────────────────────────────
        if not vera_tried:
            try:
                from neo4j import AsyncGraphDatabase  # type:ignore
                drv=AsyncGraphDatabase.driver(uri,auth=(user,password))
                await drv.verify_connectivity()
                async with drv.session() as s:
                    rec=await (await s.run("MATCH (n) RETURN count(n) AS n LIMIT 1")).single()
                    n=rec["n"] if rec else 0
                    sample_res=await s.run(
                        "MATCH (n) WHERE n.text IS NOT NULL OR n.name IS NOT NULL "
                        "RETURN coalesce(n.text,n.name,'') AS t LIMIT 3"
                    )
                    sample_texts=[r["t"][:40] async for r in sample_res if r["t"]]
                await drv.close()
                ok=True
                detail=(f"Direct connection · {n} nodes"
                        +(f" · samples: {sample_texts}" if sample_texts else " · no text nodes found"))
            except ImportError: detail="neo4j not installed (pip install neo4j)"
            except Exception as e: detail=str(e)

    elif src.type==SourceType.CHROMA:
        try:
            import chromadb, glob as _glob, os as _os  # type:ignore
            dir_val=cfg.get("directory","").strip()
            host=cfg.get("host","localhost")
            port=int(cfg.get("port",8000))

            clients_info = []  # list of (client, label)

            if dir_val:
                # Expand globs + comma-separated paths, then auto-detect sub-stores
                raw_paths=[p.strip() for p in dir_val.split(",") if p.strip()]
                candidate_roots=[]
                for p in raw_paths:
                    g=_glob.glob(p)
                    candidate_roots.extend(g if g else [p])
                store_paths=[]
                for root in candidate_roots:
                    store_paths.extend(_find_chroma_stores(root))
                seen_test=set()
                for path in store_paths:
                    real=_os.path.realpath(path)
                    if real in seen_test: continue
                    seen_test.add(real)
                    try:
                        c=chromadb.PersistentClient(path=path)
                        clients_info.append((c, _os.path.basename(path.rstrip("/"))))
                    except Exception as exc:
                        clients_info.append((None, f"{_os.path.basename(path)}: {exc}"))
            else:
                try:
                    c=chromadb.HttpClient(host=host,port=port)
                    c.heartbeat()
                    clients_info.append((c, f"http:{host}:{port}"))
                except Exception as exc:
                    clients_info.append((None, f"http:{host}:{port}: {exc}"))

            parts=[]
            all_ok=True
            for client, label in clients_info:
                if client is None:
                    parts.append(f"✗ {label}")
                    all_ok=False
                    continue
                try:
                    cols=client.list_collections()
                    total_docs=sum(col.count() for col in cols)
                    # Try a sample query on the first non-empty collection
                    sample_ok=""
                    for col in cols:
                        if col.count()>0:
                            try:
                                col.query(query_texts=["test"],n_results=1)
                                sample_ok=" ✓ query OK"
                            except Exception as qe:
                                sample_ok=f" ⚠ query failed: {qe}"
                            break
                    parts.append(f"✓ {label}: {len(cols)} col(s), {total_docs} docs{sample_ok}")
                except Exception as exc:
                    parts.append(f"✗ {label}: {exc}")
                    all_ok=False

            ok=all_ok and bool(clients_info)
            detail=" | ".join(parts) if parts else "No directories configured"
        except ImportError: detail="chromadb not installed (pip install chromadb)"
        except Exception as e: detail=str(e)

    elif src.type==SourceType.REDIS:
        try:
            import redis.asyncio as aioredis  # type:ignore
            r=aioredis.Redis(host=cfg.get("host","localhost"),port=int(cfg.get("port",6379)),
                password=cfg.get("password") or None,db=int(cfg.get("db",0)),decode_responses=True)
            await r.ping()
            prefix=cfg.get("prefix","vera:")
            count=await r.dbsize()
            await r.aclose(); ok=True; detail=f"PONG · {count} keys"
        except ImportError: detail="redis package not installed (pip install redis)"
        except Exception as e: detail=str(e)

    elif src.type==SourceType.GITHUB:
        token=cfg.get("token","")
        if not token: detail="No token configured — add a GitHub personal access token"; ok=False
        else:
            try:
                async with httpx.AsyncClient(timeout=8.0) as c:
                    r=await c.get("https://api.github.com/rate_limit",
                        headers={"Authorization":f"Bearer {token}",
                                 "Accept":"application/vnd.github+json",
                                 "X-GitHub-Api-Version":"2022-11-28"})
                    if r.status_code==200:
                        rl=r.json().get("resources",{}).get("search",{})
                        ok=True; detail=f"Token valid · search quota: {rl.get('remaining','?')}/{rl.get('limit','?')}"
                    elif r.status_code==401: detail="401 Unauthorized — token is invalid or expired"
                    else: detail=f"HTTP {r.status_code}"
            except Exception as e: detail=str(e)

    elif src.type==SourceType.WEB_ARCHIVE:
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r=await c.get("http://web.archive.org/cdx/search/cdx",
                    params={"url":"example.com","output":"json","limit":"1"})
                ok=r.status_code<400; detail=f"Wayback Machine reachable · HTTP {r.status_code}"
        except Exception as e: detail=str(e)

    elif src.type==SourceType.WEB_CRAWL:
        ok=True; detail="Web crawl is built-in — no external service required"

    elif src.type==SourceType.NEWS:
        ok=True; detail="News source uses public API — no auth required"

    elif src.type==SourceType.FABRIC:
        try:
            fab = sys.modules.get("data_fabric")
            if not fab:
                detail="data_fabric module not loaded in sys.modules"
            elif not hasattr(fab, "_sqlite_query"):
                detail="data_fabric loaded but missing _sqlite_query"
            else:
                rows = await fab._sqlite_query(dataset_id="research.jobs", limit=5)
                ok=True
                detail=(f"Fabric OK · {len(rows)} jobs in research.jobs · "
                        f"DB: {getattr(fab,'SQLITE_PATH','?')}")
        except Exception as e: detail=f"{type(e).__name__}: {e}"

    elif src.type==SourceType.MEMORY:
        try:
            mem = (sys.modules.get("memory")
                   or sys.modules.get("memory"))
            if not mem:
                mem_mods=[k for k in sys.modules if k.split('.')[-1]=='memory']
                detail=f"memory module not loaded (candidates: {mem_mods})"
            else:
                ok=True; detail=f"Memory module available ({mem.__name__})"
        except Exception as e: detail=f"{type(e).__name__}: {e}"

    src.status="ok" if ok else "error"
    # Persist status change
    await DB.save_sources(sources)
    _write_config_file()
    return {"ok":ok,"detail":detail}


# ── Web search config ─────────────────────────────────────────────────────────

@app.get("/api/websearch/config")
async def get_websearch_config():
    return asdict(web_cfg)


@app.post("/api/websearch/config")
async def set_websearch_config(req: WebSearchConfigRequest):
    if req.engine        is not None: web_cfg.engine        = req.engine
    if req.result_count  is not None: web_cfg.result_count  = max(1,min(req.result_count,20))
    if req.crawl_depth   is not None: web_cfg.crawl_depth   = max(0,min(req.crawl_depth,3))
    if req.crawl_breadth is not None: web_cfg.crawl_breadth = max(1,min(req.crawl_breadth,10))
    if req.crawl_timeout is not None: web_cfg.crawl_timeout = req.crawl_timeout
    if req.include_archive is not None: web_cfg.include_archive=req.include_archive
    if req.safe_search   is not None: web_cfg.safe_search   = req.safe_search
    await DB.save_web_search_config(web_cfg)
    _write_config_file()
    return asdict(web_cfg)


# ── Models ────────────────────────────────────────────────────────────────────

@app.get("/api/models")
async def get_models():
    return [{"instance":i.name,"tier":i.tier,"host":i.base_url,
             "current_model":i.model,"available":await list_models(i),"enabled":i.enabled}
            for i in instances]


@app.get("/api/config/instances")
async def get_instances_cfg():
    return [{
        "name":             i.name,
        "host":             i.host,
        "port":             i.port,
        "tier":             i.tier,
        "model":            i.model,
        "ctx_size":         i.ctx_size,
        "enabled":          i.enabled,
        "enable_thinking":  i.enable_thinking,
        "thinking_timeout": i.thinking_timeout,
    } for i in instances]


@app.post("/api/config/instances")
async def update_instances(req: InstanceUpdateRequest):
    global instances
    new = []
    for d in req.instances:
        try:
            new.append(OllamaInstance(
                name=d["name"], host=d["host"], port=int(d["port"]),
                tier=ModelTier(d["tier"]), model=d["model"],
                ctx_size=int(d.get("ctx_size", 8192)),
                enabled=bool(d.get("enabled", True)),
                enable_thinking=bool(d.get("enable_thinking", False)),
                thinking_timeout=float(d.get("thinking_timeout", 0.0)),
            ))
        except Exception as e:
            raise HTTPException(400, f"Invalid instance: {e}")
    instances = new
    await DB.save_instances(instances)
    _write_config_file()
    return {"ok": True, "count": len(instances)}


# ── Projects ──────────────────────────────────────────────────────────────────

@app.post("/api/projects")
async def create_project(req: ProjectCreateRequest):
    proj = Project(id=str(uuid.uuid4())[:12], name=req.name,
                   description=req.description, output_mode=req.output_mode)
    projects[proj.id] = proj
    await DB.save_project(proj)
    return proj.to_dict()


@app.get("/api/projects")
async def list_projects():
    # Merge in-memory with DB (DB is authoritative for persisted ones)
    db_rows = await DB.load_projects()
    db_ids = {r["id"] for r in db_rows}
    mem_only = [p.to_dict() for p in projects.values() if p.id not in db_ids]
    return mem_only + db_rows


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    db_row = await DB.load_project(project_id)
    if db_row:
        return db_row
    p = projects.get(project_id)
    if not p: raise HTTPException(404, "Project not found")
    return {**p.to_dict(), "rounds": [{"id":r.id,"round_num":r.round_num,"query":r.query,
        "created_at":r.created_at} for r in p.rounds], "context_summary":p.context_summary}


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    if project_id not in projects and not await DB.load_project(project_id):
        raise HTTPException(404, "Project not found")
    projects.pop(project_id, None)
    await DB.delete_project(project_id)
    proj_dir = PROJECTS_DIR / project_id
    if proj_dir.exists(): shutil.rmtree(proj_dir)
    return {"ok": True}


@app.get("/api/projects/{project_id}/download")
async def download_project(project_id: str):
    """Zip all generated files for a project — DB first, disk fallback."""
    import io, zipfile
    from fastapi.responses import Response as FR

    # Try DB first
    db_files = await DB.load_generated_files_for_project(project_id)
    proj_name = (projects.get(project_id) or type("P",(),{"name":project_id})()).name

    if db_files:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, content in db_files.items():
                zf.writestr(path, content)
        buf.seek(0)
        return FR(content=buf.read(), media_type="application/zip",
                  headers={"Content-Disposition":
                           f'attachment; filename="{proj_name.replace(" ","_")}_files.zip"'})

    # Disk fallback
    files_dir = PROJECTS_DIR / project_id / "files"
    if not files_dir.exists():
        raise HTTPException(404, "No generated files for this project")
    zip_path = PROJECTS_DIR / project_id / f"{project_id}.zip"
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", str(files_dir))
    return FileResponse(str(zip_path), media_type="application/zip",
                        filename=f"{proj_name.replace(' ','_')}.zip")


# ── Database ──────────────────────────────────────────────────────────────────

@app.get("/api/db/stats")
async def db_stats():
    return await DB.get_stats()


@app.get("/api/debug/history")
async def debug_history():
    """
    Diagnostic: traces why the Library doesn't show saved research.
    Hit /api/debug/history to see exactly where the load path breaks.
    """
    import sqlite3 as _sq
    diag = {}

    # 1. Resolve the fabric module and SQLite path
    fab = (sys.modules.get("data_fabric")
           or sys.modules.get("data_fabric"))
    diag["fabric_module"] = str(fab) if fab else None
    sqlite_path = getattr(fab, "SQLITE_PATH", None) if fab else None
    diag["sqlite_path"] = sqlite_path

    # 2. Direct SQL — count rows per dataset
    if sqlite_path:
        try:
            conn = _sq.connect(sqlite_path, timeout=10)
            try:
                # All distinct dataset_ids and their counts
                rows = conn.execute(
                    "SELECT dataset_id, COUNT(*) FROM fabric_records GROUP BY dataset_id"
                ).fetchall()
                diag["all_datasets"] = {r[0]: r[1] for r in rows}
                # Specifically research.jobs
                jrows = conn.execute(
                    "SELECT id, dataset_id, substr(data,1,200) FROM fabric_records "
                    "WHERE dataset_id='research.jobs' ORDER BY created_at DESC LIMIT 3"
                ).fetchall()
                diag["research_jobs_sample"] = [
                    {"id": r[0], "dataset_id": r[1], "data_preview": r[2]}
                    for r in jrows
                ]
            finally:
                conn.close()
        except Exception as e:
            diag["sql_error"] = str(e)

    # 3. Test DB.get_stats
    try:
        diag["DB.get_stats"] = await DB.get_stats()
    except Exception as e:
        diag["DB.get_stats_error"] = str(e)

    # 4. Test DB.load_history
    try:
        rows, total = await DB.load_history(limit=10, offset=0)
        diag["DB.load_history"] = {"total": total, "returned": len(rows),
                                    "first": rows[0] if rows else None}
    except Exception as e:
        diag["DB.load_history_error"] = str(e)

    # 5. Test the raw _query_by_filter
    try:
        rf = (sys.modules.get("Vera.Orchestration.research_fabric")
              or sys.modules.get("research_fabric"))
        if rf and hasattr(rf, "_query_by_filter"):
            qrows = await rf._query_by_filter("research.jobs", {}, limit=10)
            diag["_query_by_filter"] = {
                "count": len(qrows),
                "first_keys": list(qrows[0].keys()) if qrows else [],
                "first_id": qrows[0].get("id") if qrows else None,
                "first_job_id": qrows[0].get("job_id") if qrows else None,
            }
        # Also check notebooks
        if rf and hasattr(rf, "_query_by_filter"):
            nbrows = await rf._query_by_filter("research.notebooks", {}, limit=10)
            diag["notebooks_count"] = len(nbrows)
    except Exception as e:
        diag["_query_by_filter_error"] = str(e)

    return diag


@app.post("/api/debug/dedup")
async def debug_dedup():
    """
    One-time cleanup: physically remove stale duplicate rows from the
    research datasets in fabric_records, keeping only the newest row per
    logical record id. Fixes the duplicate notebooks/jobs that accumulated
    before the upsert fix was deployed.
    """
    import sqlite3 as _sq
    fab = (sys.modules.get("data_fabric")
           or sys.modules.get("data_fabric"))
    if not fab or not hasattr(fab, "SQLITE_PATH"):
        raise HTTPException(503, "data_fabric not available")

    datasets = ["research.jobs", "research.notebooks", "research.notebook_cells",
                "research.notebook_pages", "research.citations", "research.projects",
                "research.source_configs", "research.results", "research.bookmarks",
                "research.iteration_targets"]
    report = {}
    conn = _sq.connect(fab.SQLITE_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        for ds in datasets:
            rows = conn.execute(
                "SELECT id, data, created_at FROM fabric_records WHERE dataset_id=?",
                (ds,)
            ).fetchall()
            # Group row-uuids by logical id
            by_lid: dict = {}
            for row_uuid, data_json, col_created in rows:
                try:
                    d = json.loads(data_json or "{}")
                except Exception:
                    d = {}
                lid = str(d.get("id", "") or "")
                if not lid:
                    continue
                # newest-first sort key
                ts = d.get("updated_at") or d.get("created_at") or col_created or ""
                by_lid.setdefault(lid, []).append((row_uuid, ts))
            # For each logical id, keep newest, delete the rest
            deleted = 0
            for lid, entries in by_lid.items():
                if len(entries) <= 1:
                    continue
                def _k(e):
                    v = e[1]
                    if isinstance(v, (int, float)): return float(v)
                    try: return float(v)
                    except Exception:
                        try:
                            from datetime import datetime as _dt
                            return _dt.fromisoformat(str(v).replace("Z","+00:00")).timestamp()
                        except Exception:
                            return 0.0
                entries.sort(key=_k, reverse=True)
                for row_uuid, _ in entries[1:]:
                    conn.execute("DELETE FROM fabric_records WHERE id=?", (row_uuid,))
                    deleted += 1
            if deleted:
                report[ds] = {"before": len(rows), "deleted": deleted,
                              "after": len(rows) - deleted, "unique": len(by_lid)}
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "cleaned": report or "no duplicates found"}


@app.get("/api/db/search")
async def db_search(
    q:           str = Query(""),
    mode:        str = Query(""),
    output_mode: str = Query(""),
    limit:       int = Query(24, ge=1, le=200),
    offset:      int = Query(0,  ge=0),
):
    """
    Paginated full-text search across all saved research.
    PG: uses tsvector + ts_headline.  SQLite: LIKE fallback.
    Returns {items, total}.
    """
    rows, total = await DB.search(
        q=q, mode=mode, output_mode=output_mode,
        limit=limit, offset=offset,
    )
    return {"items": rows, "total": total}


@app.post("/api/db/export")
async def db_export(payload: dict):
    """Export DB as JSON. Body: {"limit": 500}"""
    limit = int(payload.get("limit", 500))
    return await DB.export_all(limit=limit)



# ── Bookmarks ─────────────────────────────────────────────────────────────────
# In-memory store (persisted via DB.save_bookmark / load_bookmarks)
bookmarks: dict[str, dict] = {}   # id → bookmark dict


@app.get("/api/bookmarks")
async def get_bookmarks():
    rows = await DB.load_bookmarks()
    return rows


@app.post("/api/bookmarks")
async def add_bookmark(payload: dict):
    """
    Bookmark a citation or a whole job result.
    Body: {type: "citation"|"job", job_id, title, url, snippet,
           screenshot_url, source_type, domain, tags:[]}
    """
    bm = {
        "id":             str(uuid.uuid4())[:12],
        "type":           payload.get("type", "citation"),
        "job_id":         payload.get("job_id", ""),
        "title":          payload.get("title", ""),
        "url":            payload.get("url", ""),
        "snippet":        payload.get("snippet", "")[:600],
        "screenshot_url": payload.get("screenshot_url", ""),
        "source_type":    payload.get("source_type", "web"),
        "domain":         payload.get("domain", ""),
        "tags":           payload.get("tags", []),
        "note":           payload.get("note", ""),
        "created_at":     time.time(),
    }
    bookmarks[bm["id"]] = bm
    await DB.save_bookmark(bm)
    return bm


@app.patch("/api/bookmarks/{bm_id}")
async def update_bookmark(bm_id: str, payload: dict):
    """Update note or tags on a bookmark."""
    bm = bookmarks.get(bm_id) or await DB.get_bookmark(bm_id)
    if not bm:
        raise HTTPException(404, "Bookmark not found")
    if "note" in payload: bm["note"] = payload["note"]
    if "tags" in payload: bm["tags"] = payload["tags"]
    bookmarks[bm_id] = bm
    await DB.save_bookmark(bm)
    return bm


@app.delete("/api/bookmarks/{bm_id}")
async def delete_bookmark(bm_id: str):
    bookmarks.pop(bm_id, None)
    await DB.delete_bookmark(bm_id)
    return {"ok": True}


# ── Project: add job / add bookmark ──────────────────────────────────────────

@app.post("/api/projects/{project_id}/add_job")
async def project_add_job(project_id: str, payload: dict):
    """Add an existing completed job to a project (without re-running)."""
    job_id = payload.get("job_id", "")
    proj = projects.get(project_id)
    if not proj:
        db_row = await DB.load_project(project_id)
        if not db_row:
            raise HTTPException(404, "Project not found")
        # Reconstruct minimal project in memory
        proj = Project(
            id=db_row["id"], name=db_row["name"],
            description=db_row.get("description",""),
            output_mode=OutputMode(db_row.get("output_mode","report")),
            context_summary=db_row.get("context_summary",""),
        )
        projects[project_id] = proj

    # Load the job result
    job_data = await DB.load_job_result(job_id)
    if not job_data:
        raise HTTPException(404, "Job not found")

    # Create a round for it
    from dataclasses import fields as dc_fields
    round_ = ProjectRound(
        id=str(uuid.uuid4())[:8],
        job_id=job_id,
        round_num=len(proj.rounds)+1,
        query=job_data.get("query",""),
        result=(job_data.get("result") or "")[:4000],
        citations=job_data.get("citations",[]),
    )
    proj.rounds.append(round_)
    proj.updated_at = time.time()
    # Update context summary
    if not proj.context_summary:
        proj.context_summary = f"Project: {proj.name}\nAdded: {job_data.get('query','')}"
    else:
        proj.context_summary += f"\n\nAdded job: {job_data.get('query','')}"
    await DB.save_project(proj)
    return {"ok": True, "round_num": round_.round_num}


@app.post("/api/projects/{project_id}/add_bookmark")
async def project_add_bookmark(project_id: str, payload: dict):
    """Tag a bookmark as belonging to a project."""
    bm_id = payload.get("bookmark_id","")
    bm = bookmarks.get(bm_id) or await DB.get_bookmark(bm_id)
    if not bm:
        raise HTTPException(404, "Bookmark not found")
    tags = bm.get("tags", [])
    tag = f"project:{project_id}"
    if tag not in tags:
        tags.append(tag)
        bm["tags"] = tags
        bookmarks[bm_id] = bm
        await DB.save_bookmark(bm)
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
#  Notebook API
# ══════════════════════════════════════════════════════════════════════════════

notebooks_cache:  dict[str, dict]          = {}
cell_ws_clients:  dict[str, list[WebSocket]]= {}


class NotebookCreateRequest(BaseModel):
    title:       str = "Untitled Notebook"
    description: str = ""
    project_id:  Optional[str] = None
    tags:        list[str] = Field(default_factory=list)

class CellCreateRequest(BaseModel):
    cell_type:  str           = "markdown"
    lang:       str           = "python"
    tag:        str           = "none"
    content:    str           = ""
    sort_order: int           = 0
    page_id:    Optional[str] = None
    title:      str           = ""

class CellUpdateRequest(BaseModel):
    content:    Optional[str]  = None
    cell_type:  Optional[str]  = None
    lang:       Optional[str]  = None
    tag:        Optional[str]  = None
    generated:  Optional[str]  = None
    sort_order: Optional[int]  = None
    page_id:    Optional[str]  = None
    title:      Optional[str]  = None
    citations:  Optional[list] = None
    parse_mode: Optional[str]  = None
    agent_mode: Optional[str]  = None
    thread:     Optional[list] = None

class CellChatRequest(BaseModel):
    message: str
    mode:    str = "chat"

class ReorderRequest(BaseModel):
    order: list[str]


@app.post("/api/notebooks")
async def create_notebook(req: NotebookCreateRequest):
    nb = {"id":str(uuid.uuid4())[:16],"title":req.title,"description":req.description,
          "project_id":req.project_id,"tags":req.tags,"cells":[],
          "created_at":time.time(),"updated_at":time.time()}
    await DB.save_notebook(nb); notebooks_cache[nb["id"]]=nb; return nb


@app.get("/api/notebooks")
async def list_notebooks(project_id: Optional[str] = None):
    return await DB.load_notebooks(project_id)


@app.get("/api/notebooks/{nb_id}")
async def get_notebook(nb_id: str):
    nb = await DB.load_notebook(nb_id)
    if not nb: raise HTTPException(404,"Notebook not found")
    nb["pages"] = await DB.load_pages(nb_id)
    return nb


@app.patch("/api/notebooks/{nb_id}")
async def update_notebook_meta(nb_id: str, payload: dict):
    nb = await DB.load_notebook(nb_id)
    if not nb: raise HTTPException(404,"Notebook not found")
    for k in ("title","description","tags"):
        if k in payload: nb[k] = payload[k]
    nb["updated_at"] = time.time()
    await DB.save_notebook(nb); return nb


@app.delete("/api/notebooks/{nb_id}")
async def delete_notebook_route(nb_id: str):
    await DB.delete_notebook(nb_id); notebooks_cache.pop(nb_id,None); return {"ok":True}


@app.post("/api/notebooks/{nb_id}/cells")
async def add_cell(nb_id: str, req: CellCreateRequest):
    cell = {"id":str(uuid.uuid4())[:16],"notebook_id":nb_id,
            "sort_order":req.sort_order,"cell_type":req.cell_type,"lang":req.lang,
            "tag":req.tag,"content":req.content,"generated":"","thread":[],
            "citations":[],"page_id":req.page_id or None,"title":req.title or "",
            "parse_mode":"whole","agent_mode":"single",
            "created_at":time.time(),"updated_at":time.time()}
    await DB.save_cell(cell); return cell


@app.patch("/api/notebooks/{nb_id}/cells/{cell_id}")
async def update_cell(nb_id: str, cell_id: str, req: CellUpdateRequest):
    cell = await DB.load_cell(cell_id)
    if not cell: raise HTTPException(404,"Cell not found")
    # Use model_dump(exclude_unset=True) so explicitly-sent null values are applied
    # (exclude_none would strip intentional nulls like page_id=null)
    for k,v in req.model_dump(exclude_unset=True).items():
        cell[k] = v
    cell["updated_at"] = time.time()
    await DB.save_cell(cell); return cell


@app.delete("/api/notebooks/{nb_id}/cells/{cell_id}")
async def delete_cell_route(nb_id: str, cell_id: str):
    await DB.delete_cell(cell_id); return {"ok":True}



@app.patch("/api/notebooks/{nb_id}/cells/{cell_id}/move")
async def move_cell_to_page(nb_id: str, cell_id: str, payload: dict):
    """Move a cell to a different page."""
    cell = await DB.load_cell(cell_id)
    if not cell: raise HTTPException(404, "Cell not found")
    cell["page_id"]    = payload.get("page_id")   # None = remove from any page
    cell["sort_order"] = payload.get("sort_order", cell.get("sort_order", 0))
    cell["updated_at"] = time.time()
    await DB.save_cell(cell)
    return cell


@app.post("/api/notebooks/{nb_id}/reorder")
async def reorder_cells(nb_id: str, req: ReorderRequest):
    for i,cid in enumerate(req.order):
        c = await DB.load_cell(cid)
        if c and c["notebook_id"]==nb_id:
            c["sort_order"]=i; c["updated_at"]=time.time()
            await DB.save_cell(c)
    return {"ok":True}


# ── Page endpoints ──────────────────────────────────────────────────────────

class PageCreateRequest(BaseModel):
    title:        str = "New Page"
    sort_order:   int = 0
    column_count: int = 1   # 1 = single column, 2 = two columns, 3 = three columns

class PageUpdateRequest(BaseModel):
    title:      Optional[str] = None
    sort_order: Optional[int] = None


@app.get("/api/notebooks/{nb_id}/pages")
async def list_pages(nb_id: str):
    return await DB.load_pages(nb_id)

@app.post("/api/notebooks/{nb_id}/pages")
async def create_page(nb_id: str, req: PageCreateRequest):
    nb = await DB.load_notebook(nb_id)
    if not nb: raise HTTPException(404,"Notebook not found")
    page = {"id":str(uuid.uuid4())[:16],"notebook_id":nb_id,
            "title":req.title,"sort_order":req.sort_order,
            "column_count":req.column_count,
            "created_at":time.time(),"updated_at":time.time()}
    await DB.save_page(page); return page

@app.patch("/api/notebooks/{nb_id}/pages/{page_id}")
async def update_page(nb_id: str, page_id: str, payload: dict):
    pages = await DB.load_pages(nb_id)
    page = next((p for p in pages if p["id"]==page_id), None)
    if not page: raise HTTPException(404,"Page not found")
    for k in ("title","sort_order","column_count"):
        if k in payload: page[k] = payload[k]
    page["updated_at"] = time.time()
    await DB.save_page(page); return page

@app.delete("/api/notebooks/{nb_id}/pages/{page_id}")
async def delete_page_ep(nb_id: str, page_id: str):
    await DB.delete_page(page_id); return {"ok":True}

@app.post("/api/notebooks/{nb_id}/cells/{cell_id}/name")
async def auto_name_cell(nb_id: str, cell_id: str):
    """Ask the Writer to suggest a short title for this cell."""
    cell = await DB.load_cell(cell_id)
    if not cell: raise HTTPException(404,"Cell not found")
    raw = (cell.get("content","") + "\n" + cell.get("generated","")).strip()
    if not raw: return {"title":""}
    if len(raw) <= 1200:
        excerpt = raw[:1200]
    else:
        mid = len(raw)//2
        excerpt = raw[:600] + "\n...\n" + raw[mid:mid+400]
    writer = await get_instance(ModelTier.WRITER) or await get_instance(ModelTier.THINKER)
    if not writer: return {"title":""}
    title = await collect_ollama(writer, excerpt,
        "Generate a very short descriptive title (3-7 words) for this notebook cell. "
        "Respond with ONLY the title, no quotes, no punctuation at end, no preamble.",
        cell_id, timeout_secs=300)
    title = title.strip().strip('"').strip("'").splitlines()[0][:80]
    cell["title"] = title; cell["updated_at"] = time.time()
    await DB.save_cell(cell)
    return {"title": title}


@app.post("/api/notebooks/from_job/{job_id}")
async def notebook_from_job(job_id: str, payload: dict):
    """Create a notebook pre-populated from a completed research job."""
    job_data = await DB.load_job_result(job_id)
    if not job_data: raise HTTPException(404,"Job not found")
    title = payload.get("title") or (job_data.get("query","Research")[:80])
    nb = {"id":str(uuid.uuid4())[:16],"title":title,
          "description":f"Compiled from job {job_id[:8]}",
          "project_id":payload.get("project_id"),"tags":["research"],
          "cells":[],"created_at":time.time(),"updated_at":time.time()}
    await DB.save_notebook(nb)

    t_now = time.time()
    cells: list[dict] = []
    pages: list[dict] = []

    def _mk_page(pg_title: str, order: int) -> dict:
        return {"id":str(uuid.uuid4())[:16],"notebook_id":nb["id"],
                "title":pg_title,"sort_order":order,
                "created_at":t_now,"updated_at":t_now}

    def _mk_cell(order:int, ctype:str, content:str, lang:str="python",
                 tag:str="none", title:str="", page_id:str=None) -> dict:
        return {"id":str(uuid.uuid4())[:16],"notebook_id":nb["id"],
                "sort_order":order,"cell_type":ctype,"lang":lang,"tag":tag,
                "content":content,"generated":"","thread":[],"citations":[],
                "title":title,"page_id":page_id,"parse_mode":"whole",
                "agent_mode":"single","created_at":t_now,"updated_at":t_now}

    # ── Overview page ────────────────────────────────────────────────────
    ov = _mk_page("Overview", 0)
    pages.append(ov)
    cells.append(_mk_cell(0,"markdown",f"# {title}",
                           title="Title", page_id=ov["id"]))
    if job_data.get("result"):
        cells.append(_mk_cell(1,"markdown",job_data["result"][:12000],
                               title="Research Result", page_id=ov["id"]))
    cits = job_data.get("citations",[])
    if cits:
        src = ("## Sources\n\n" +
               "\n".join(f"- [{c.get('title',c.get('url',''))}]({c.get('url','')})"
                         f"  \n  {c.get('snippet','')[:100]}" for c in cits[:20]))
        cells.append(_mk_cell(len(cells),"markdown",src,
                               title="Sources", page_id=ov["id"]))

    # ── One page per generated file ──────────────────────────────────────
    file_manifest = job_data.get("file_manifest",[])
    if not file_manifest:
        db_files = await DB.list_generated_files(job_id)
        file_manifest = db_files or []

    for i, f in enumerate(file_manifest[:20]):
        fpath = f.get("file_path",f) if isinstance(f,dict) else f
        content = await DB.get_generated_file(job_id, fpath)
        if not content: continue
        ext = Path(fpath).suffix.lstrip(".")
        fp = _mk_page(Path(fpath).name, i+1)
        pages.append(fp)
        cells.append(_mk_cell(i*2,"file",content[:16000],
                               lang=ext or "text",
                               title=Path(fpath).name, page_id=fp["id"]))

    # Save pages first (FK), then cells
    for p in pages: await DB.save_page(p)
    for c in cells: await DB.save_cell(c)

    # Auto-name cells with AI
    writer = await get_instance(ModelTier.WRITER) or await get_instance(ModelTier.THINKER)
    if writer:
        for c in cells:
            if c.get("title") and c["title"] not in ("", "Title", "Sources"): continue
            raw = (c.get("content","") + " " + c.get("generated","")).strip()
            if len(raw) > 80:
                try:
                    mid = len(raw)//2
                    excerpt = raw[:600] if len(raw)<=600 else raw[:400]+"\n...\n"+raw[mid:mid+200]
                    t = await collect_ollama(writer, excerpt,
                        "Generate a very short title (3-7 words) for this notebook cell. "
                        "ONLY the title, no quotes, no punctuation at end.",
                        c["id"], timeout_secs=20)
                    c["title"] = t.strip().strip('"').strip("'").splitlines()[0][:80]
                    c["updated_at"] = time.time()
                    await DB.save_cell(c)
                except Exception: pass

    nb["cells"] = cells
    nb["pages"] = pages
    return nb


def _build_nb_context(nb: dict, current_cell: dict) -> str:
    cells = sorted(nb.get("cells",[]), key=lambda c: int(c.get("sort_order",0) or 0))
    parts = [f"Notebook: {nb['title']}"]
    if nb.get("description"): parts.append(f"Description: {nb['description']}")
    for c in cells:
        if c["id"]==current_cell["id"]: break
        txt=(c.get("generated") or c.get("content",""))[:400]
        if txt.strip():
            m=f"[{c['cell_type'].upper()}{':'+c['lang'] if c['cell_type']=='code' else ''}]"
            parts.append(f"{m} {txt}")
    return "\n\n".join(parts)


async def _cell_broadcast(cell_id: str, payload: dict):
    msg=json.dumps(payload)
    for ws in list(cell_ws_clients.get(cell_id,[])):
        try: await ws.send_text(msg)
        except Exception:
            try: cell_ws_clients[cell_id].remove(ws)
            except ValueError: pass


async def _do_generate(nb: dict, cell: dict, nb_ctx: str) -> str:
    tag=cell.get("tag","none"); content=cell.get("content","").strip()
    cid=cell["id"]
    if not content:
        await _cell_broadcast(cid,{"type":"error","text":"Cell is empty"}); return ""
    if tag=="to_code":
        sys_p="You are an expert programmer. Convert the description to complete, working code. Add comments."
        prompt=f"{nb_ctx}\n\nConvert to {cell.get('lang','python')}:\n{content}"
    elif tag=="summarise":
        sys_p="You are a technical writer. Summarise concisely, preserve key facts."
        prompt=f"{nb_ctx}\n\nSummarise:\n{content}"
    elif tag=="research":
        return await _do_research(nb,cell,nb_ctx,content)
    else:
        if cell.get("cell_type")=="code":
            sys_p="You are an expert programmer. Write complete, working code with comments."
            prompt=f"{nb_ctx}\n\nWrite {cell.get('lang','python')} code for:\n{content}"
        else:
            sys_p=("You are a knowledgeable assistant. Flesh out the user's brief note into a thorough, "
                   "well-structured response. Use markdown headings and bullet points.")
            prompt=f"{nb_ctx}\n\nFlesh out this note into a complete section:\n\n{content}"
    writer=await get_instance(ModelTier.WRITER) or await get_instance(ModelTier.THINKER)
    if not writer: await _cell_broadcast(cid,{"type":"error","text":"No model available"}); return ""
    parts=[]
    async for tok in stream_ollama(writer,prompt,sys_p,f"cell:{cid}",timeout_secs=WRITER_TIMEOUT):
        parts.append(tok); await _cell_broadcast(cid,{"type":"token","text":tok})
    generated="".join(parts)
    cell["generated"]=generated
    t=cell.get("thread",[]); t.append({"role":"assistant","content":generated,"action":"generate","t":time.time()})
    cell["thread"]=t; cell["updated_at"]=time.time()
    await DB.save_cell(cell)
    await _cell_broadcast(cid,{"type":"done","generated":generated,"cell_id":cid})
    return generated


async def _do_research(nb: dict, cell: dict, nb_ctx: str, query: str) -> str:
    cid=cell["id"]
    await _cell_broadcast(cid,{"type":"status","text":"Researching…"})
    ctx_str=""; cits: list[Citation] = []
    try:
        # Use gather_all_sources so all configured sources (web, neo4j, chroma, etc.) are queried
        class _FJ:
            id="nb"
            sources=[]
            citations: list = []
        fake_job=_FJ()
        cits, ctx_str = await gather_all_sources(query, fake_job)  # type: ignore
        if not cits:
            # fallback to web search only
            cits = await gather_web_search(query, fake_job)
            ctx_str = "\n\n".join(
                f"[{i+1}] {c.title}\n{c.snippet[:300]}" for i,c in enumerate(cits[:8])
            )
        # Broadcast citations so the frontend Sources tab and research sidebar populate
        await _cell_broadcast(cid,{
            "type": "citations",
            "citations": [c.to_dict() for c in cits[:20]]
        })
    except Exception as e:
        log.debug("nb research gather: %s", e)
    writer=await get_instance(ModelTier.WRITER) or await get_instance(ModelTier.THINKER)
    if not writer: await _cell_broadcast(cid,{"type":"error","text":"No model"}); return ""
    sys_p=("You are a research assistant writing for a notebook. Use the provided sources. "
           "Cite inline as [1],[2] etc. Use ## headings and bullet points for structure.")
    prompt=f"{nb_ctx}\n\nQuery: {query}\n\nSources:\n{ctx_str[:4000]}\n\nWrite a comprehensive, well-cited notebook section."
    parts=[]
    async for tok in stream_ollama(writer,prompt,sys_p,f"cell:{cid}",timeout_secs=WRITER_TIMEOUT):
        parts.append(tok); await _cell_broadcast(cid,{"type":"token","text":tok})
    generated="".join(parts)
    cell["generated"]=generated
    # Store citations on the cell for persistence
    cell["citations"]=[c.to_dict() for c in cits[:20]]
    t=cell.get("thread",[]); t.append({"role":"assistant","content":generated,"action":"research","t":time.time()})
    cell["thread"]=t; cell["updated_at"]=time.time()
    await DB.save_cell(cell)
    await _cell_broadcast(cid,{"type":"done","generated":generated,"cell_id":cid,"citations":[c.to_dict() for c in cits[:20]]})
    return generated


async def _do_chat(nb: dict, cell: dict, nb_ctx: str, message: str) -> str:
    cid=cell["id"]
    writer=await get_instance(ModelTier.WRITER) or await get_instance(ModelTier.THINKER)
    if not writer: await _cell_broadcast(cid,{"type":"error","text":"No model"}); return ""
    thread=cell.get("thread",[]); hist="\n".join(
        f"{'User' if m['role']=='user' else 'Assistant'}: {m['content'][:400]}" for m in thread[-6:])
    sys_p="You are a helpful notebook assistant. Be concise. You can suggest edits, explain, write code, answer questions."
    prompt=f"{nb_ctx}\n\nCell content:\n{(cell.get('generated') or cell.get('content',''))[:2000]}\n\nConversation:\n{hist}\n\nUser: {message}\nAssistant:"
    parts=[]
    async for tok in stream_ollama(writer,prompt,sys_p,f"cell:{cid}",timeout_secs=WRITER_TIMEOUT):
        parts.append(tok); await _cell_broadcast(cid,{"type":"token","text":tok})
    resp="".join(parts)
    thread.append({"role":"user","content":message,"t":time.time()})
    thread.append({"role":"assistant","content":resp,"action":"chat","t":time.time()})
    cell["thread"]=thread; cell["updated_at"]=time.time()
    await DB.save_cell(cell)
    await _cell_broadcast(cid,{"type":"done","response":resp,"cell_id":cid,"thread":thread})
    return resp


@app.websocket("/ws/notebook/{nb_id}/cell/{cell_id}")
async def cell_stream_ws(ws: WebSocket, nb_id: str, cell_id: str):
    await ws.accept()
    cell_ws_clients.setdefault(cell_id,[]).append(ws)
    try:
        while True:
            raw=await ws.receive_text()
            try: req=json.loads(raw)
            except Exception: continue
            action=req.get("action","generate"); message=req.get("message","")
            nb=await DB.load_notebook(nb_id); cell=await DB.load_cell(cell_id)
            if not nb or not cell:
                await _cell_broadcast(cell_id,{"type":"error","text":"Not found"}); continue
            nb_ctx=_build_nb_context(nb,cell)
            if action=="generate": await _do_generate(nb,cell,nb_ctx)
            elif action=="research": await _do_research(nb,cell,nb_ctx,message or cell.get("content",""))
            elif action=="chat": await _do_chat(nb,cell,nb_ctx,message)
    except WebSocketDisconnect: pass
    finally:
        try: cell_ws_clients.get(cell_id,[]).remove(ws)
        except ValueError: pass


@app.post("/api/notebooks/{nb_id}/cells/{cell_id}/chat")
async def cell_chat_rest(nb_id: str, cell_id: str, req: CellChatRequest):
    nb=await DB.load_notebook(nb_id); cell=await DB.load_cell(cell_id)
    if not nb or not cell: raise HTTPException(404,"Not found")
    nb_ctx=_build_nb_context(nb,cell)
    if req.mode=="research": resp=await _do_research(nb,cell,nb_ctx,req.message)
    else: resp=await _do_chat(nb,cell,nb_ctx,req.message)
    cell_upd=await DB.load_cell(cell_id)
    return {"response":resp,"thread":cell_upd.get("thread",[]) if cell_upd else []}



# ══════════════════════════════════════════════════════════════════════════════
#  Continuous Iteration Engine
#  ────────────────────────────
#  Runs research in the background, building a traversal map of what has been
#  covered. Uses the WRITER (fast model) so it doesn't block interactive work.
#  Supports all modes (single/parallel/deep) and all output modes.
# ══════════════════════════════════════════════════════════════════════════════

# In-memory registry of running iteration tasks
_iter_tasks:  dict[str, asyncio.Task]  = {}   # it_id → asyncio.Task
_iter_stop:   dict[str, bool]          = {}   # it_id → stop_requested


@dataclass
class TraversalMap:
    """Tracks everything the iteration engine has covered for one target."""
    covered_queries:   list[str]       = field(default_factory=list)
    covered_urls:      set[str]        = field(default_factory=set)
    covered_topics:    list[str]       = field(default_factory=list)
    knowledge_summary: str             = ""
    iteration_count:   int             = 0
    last_run_at:       float           = 0.0
    results_digest:    list[str]       = field(default_factory=list)  # short digest of each result

    def to_dict(self) -> dict:
        return {
            "covered_queries":   self.covered_queries,
            "covered_urls":      list(self.covered_urls),
            "covered_topics":    self.covered_topics,
            "knowledge_summary": self.knowledge_summary,
            "iteration_count":   self.iteration_count,
            "last_run_at":       self.last_run_at,
            "results_digest":    self.results_digest,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TraversalMap":
        tm = cls()
        tm.covered_queries  = d.get("covered_queries", [])
        tm.covered_urls     = set(d.get("covered_urls", []))
        tm.covered_topics   = d.get("covered_topics", [])
        tm.knowledge_summary= d.get("knowledge_summary", "")
        tm.iteration_count  = d.get("iteration_count", 0)
        tm.last_run_at      = d.get("last_run_at", 0.0)
        tm.results_digest   = d.get("results_digest", [])
        return tm


async def _iter_next_query(
    seed: str,
    tm: TraversalMap,
    fast: OllamaInstance,
    job_id: str,
    existing_content: str = "",   # pre-loaded content from project/notebook
) -> str:
    """Ask the fast model what to research next given the traversal map and existing content."""
    # First iteration — still use seed but also look at existing content
    if not tm.covered_queries and not existing_content:
        return seed

    covered_summary = "\n".join(f"- {q}" for q in tm.covered_queries[-12:])
    topics_summary  = ", ".join(tm.covered_topics[-8:]) if tm.covered_topics else "none yet"

    # Build a digest of recent results for context
    recent_digest = "\n".join(tm.results_digest[-5:]) if tm.results_digest else ""

    prompt = (
        f"Research topic: {seed}\n\n"
        + (f"Existing content to build on:\n{existing_content[:1500]}\n\n"
           if existing_content else "")
        + (f"Knowledge summary so far ({tm.iteration_count} iterations):\n"
           + f"{tm.knowledge_summary[:800]}\n\n"
           if tm.knowledge_summary else "")
        + (f"Recent findings:\n{recent_digest[:600]}\n\n" if recent_digest else "")
        + (f"Already investigated:\n{covered_summary}\n\n" if covered_summary else "")
        + (f"Topics covered: {topics_summary}\n\n" if topics_summary != "none yet" else "")
        + "Generate ONE specific, focused research query that:\n"
        + "1. Explores an aspect NOT yet covered\n"
        + "2. Builds naturally on what is already known\n"
        + "3. Goes deeper or broader in a useful direction\n"
        + "Return ONLY the query text — no explanation, no quotes, no numbering."
    )
    raw = await collect_ollama(fast, prompt,
        "You generate precise research queries. Return only the query text, nothing else.",
        job_id, timeout_secs=300)
    q = raw.strip().strip('"').strip("'").splitlines()[0].strip()
    return q if len(q) > 5 else seed


async def _iter_update_traversal(
    tm: TraversalMap,
    query: str,
    result: str,
    citations: list[Citation],
    fast: OllamaInstance,
    job_id: str,
) -> None:
    """Update the traversal map after a completed iteration."""
    tm.covered_queries.append(query)
    tm.covered_urls.update(c.url for c in citations)
    tm.iteration_count += 1
    tm.last_run_at = time.time()

    # Extract new topics from this result
    topic_prompt = (
        f"Result summary (first 1000 chars):\n{result[:1000]}\n\n"
        "List the 3-5 main topics/concepts covered. Return a JSON array of short strings."
    )
    try:
        raw = await asyncio.wait_for(
            collect_ollama(fast, topic_prompt,
                "Return only a JSON array of strings.", job_id, timeout_secs=30),
            timeout=35
        )
        new_topics = json.loads(raw[raw.index("["):raw.rindex("]")+1])
        tm.covered_topics.extend(str(t) for t in new_topics[:5])
        tm.covered_topics = list(dict.fromkeys(tm.covered_topics))[-30:]  # dedup, keep 30
    except Exception:
        pass

    # Always store a short digest of this result
    digest = result[:400].replace("\n", " ").strip()
    if digest:
        tm.results_digest.append(f"[Iter {tm.iteration_count}] Q: {query[:80]}\n{digest}")
        tm.results_digest = tm.results_digest[-20:]  # keep last 20

    # Update rolling knowledge summary every iteration (not every 3)
    summ_prompt = (
        f"Topic: {tm.covered_queries[0] if tm.covered_queries else 'unknown'}\n\n"
        + (f"Existing summary:\n{tm.knowledge_summary[:600]}\n\n" if tm.knowledge_summary else "")
        + f"New research (iteration {tm.iteration_count}):\n"
        + f"Query: {query}\n"
        + f"Findings: {result[:1200]}\n\n"
        "Update the running knowledge summary. Keep under 500 words. "
        "Include: what is known, key facts discovered, what still needs investigation."
    )
    try:
        new_summary = await asyncio.wait_for(
            collect_ollama(fast, summ_prompt,
                "You maintain rolling research summaries. Be concise and dense with facts.",
                job_id, timeout_secs=300),
            timeout=100
        )
        if new_summary and new_summary.strip():
            tm.knowledge_summary = new_summary.strip()
    except Exception as e:
        log.debug("Summary update failed: %s", e)
        # Fallback: append raw digest to summary
        if not tm.knowledge_summary:
            tm.knowledge_summary = f"[Iter {tm.iteration_count}] {query}: {result[:300]}"
        else:
            tm.knowledge_summary += f"\n\n[Iter {tm.iteration_count}] {query}: {result[:200]}"


async def _run_iteration_loop(it_id: str) -> None:
    """
    The main iteration background loop.
    Runs until stopped or max iterations reached.
    Uses only the WRITER (fast model) to avoid blocking interactive work.
    """
    log.info("Iteration loop started: %s", it_id)

    while True:
        if _iter_stop.get(it_id):
            log.info("Iteration %s: stop requested", it_id)
            break

        # Reload target from DB on each iteration (config may have changed)
        it = await DB.load_iteration_target(it_id)
        if not it or it["status"] not in ("running",):
            log.info("Iteration %s: status=%s, stopping loop", it_id, it.get("status") if it else "gone")
            break

        tm = TraversalMap.from_dict(it.get("traversal_map") or {})
        interval = it.get("interval_secs", 300)
        seed     = it.get("seed_query", "")
        mode     = AgentMode(it.get("mode", "single"))
        om       = OutputMode(it.get("output_mode", "report"))

        # Get fast model (WRITER preferred; THINKER fallback)
        fast = await get_instance(ModelTier.WRITER) or await get_instance(ModelTier.THINKER)
        if not fast:
            log.warning("Iteration %s: no model available, sleeping 60s", it_id)
            await asyncio.sleep(60)
            continue

        # Load existing content from target to give context for next query
        existing_content = ""
        try:
            t_type = it.get("target_type","")
            t_id   = it.get("target_id","")
            if t_type == "project" and t_id in projects:
                proj = projects[t_id]
                # Last 2 project rounds + context summary
                rounds_text = "\n\n".join(
                    f"Round {r.round_num}: {r.query}\n{r.result[:400]}"
                    for r in proj.rounds[-3:]
                ) if proj.rounds else ""
                existing_content = (
                    (f"Project: {proj.name}\n{proj.context_summary[:600]}\n\n"
                     if proj.context_summary else "")
                    + rounds_text
                )
            elif t_type == "notebook" and t_id:
                nb = await DB.load_notebook(t_id)
                if nb:
                    cells = sorted(nb.get("cells",[]), key=lambda c: int(c.get("sort_order",0) or 0))
                    # Last 4 cells with content
                    cell_texts = [
                        (c.get("title","") + ":\n" + (c.get("generated") or c.get("content",""))[:300])
                        for c in cells[-4:]
                        if (c.get("generated") or c.get("content","")).strip()
                    ]
                    existing_content = "\n\n".join(cell_texts)
            elif t_type == "job":
                # Last 3 history jobs matching seed
                matching = [j for j in history[:20]
                            if seed.lower()[:20] in j.query.lower() and j.result]
                existing_content = "\n\n".join(
                    f"Q: {j.query}\n{(j.result or '')[:400]}" for j in matching[:3]
                )
        except Exception as e:
            log.debug("Iter %s: could not load existing content: %s", it_id, e)

        # Generate next query
        try:
            next_q = await _iter_next_query(seed, tm, fast, it_id,
                                             existing_content=existing_content)
        except Exception as e:
            log.warning("Iteration %s: query generation failed: %s", it_id, e)
            await asyncio.sleep(30)
            continue

        log.info("Iteration %s (iter #%d): %r", it_id, tm.iteration_count+1, next_q[:80])

        # Broadcast to anyone watching the iteration channel
        await broadcast(it_id, {
            "type": "iter_start",
            "it_id": it_id,
            "iteration": tm.iteration_count + 1,
            "query": next_q,
            "t": time.time(),
        })

        # Build rich prior context = knowledge summary + recent results + existing content
        full_prior = ""
        if tm.knowledge_summary:
            full_prior += f"Knowledge so far:\n{tm.knowledge_summary[:1500]}\n\n"
        if existing_content:
            full_prior += f"Existing content:\n{existing_content[:1500]}\n\n"
        if tm.results_digest:
            full_prior += "Recent iterations:\n" + "\n".join(tm.results_digest[-3:])

        # Create and run the research job
        job = ResearchJob(
            id=str(uuid.uuid4()),
            query=next_q,
            mode=mode,
            output_mode=om,
            sources=[s.id for s in sources if s.enabled],
            status=JobStatus.QUEUED,
            created_at=time.time(),
            project_id=it.get("target_id") if it.get("target_type") == "project" else None,
            prior_context=full_prior[:4000],
            context_mode="continue",
        )
        jobs[job.id] = job
        try:
            await DB.save_job(job)
        except Exception:
            pass

        # Broadcast the job id so the UI can subscribe to it
        await broadcast(it_id, {
            "type": "iter_job",
            "it_id": it_id,
            "job_id": job.id,
            "query": next_q,
            "iteration": tm.iteration_count + 1,
        })

        project_obj: Optional[Project] = None
        if it.get("target_type") == "project" and it.get("target_id") in projects:
            project_obj = projects[it["target_id"]]

        try:
            # Run the actual research (single/parallel/deep all work here)
            await run_job_body(job, project_obj)
        except Exception as e:
            log.warning("Iteration %s: job %s failed: %s", it_id, job.id, e)
            job.status = JobStatus.ERROR
            job.error  = str(e)

        # Persist job
        try:
            await DB.save_job(job)
        except Exception:
            pass

        # Update traversal map
        if job.result:
            await _iter_update_traversal(tm, next_q, job.result, job.citations, fast, it_id)

        # Attach result to target
        target_type = it.get("target_type")
        target_id   = it.get("target_id", "")
        if target_type == "project" and project_obj:
            await update_project_context(project_obj, job, fast)
            await DB.save_project(project_obj)
        elif target_type == "notebook":
            # Append result as a new cell in the notebook
            try:
                nb = await DB.load_notebook(target_id)
                if nb:
                    existing_cells = nb.get("cells") or []
                    new_cell = {
                        "id": str(uuid.uuid4())[:16],
                        "notebook_id": target_id,
                        "sort_order": len(existing_cells),
                        "cell_type": "markdown",
                        "lang": "python",
                        "tag": "none",
                        "content": f"## Iteration {tm.iteration_count}: {next_q}\n\n{job.result or ''}",
                        "generated": "",
                        "thread": [],
                        "citations": [c.to_dict() for c in job.citations[:10]],
                        "title": next_q[:60],
                        "page_id": None,
                        "parse_mode": "whole",
                        "agent_mode": "single",
                        "created_at": time.time(),
                        "updated_at": time.time(),
                    }
                    await DB.save_cell(new_cell)
            except Exception as e:
                log.warning("Iteration %s: notebook append failed: %s", it_id, e)

        # Persist updated traversal map
        it["traversal_map"] = tm.to_dict()
        it["updated_at"] = time.time()
        await DB.save_iteration_target(it)

        # Broadcast completion
        await broadcast(it_id, {
            "type": "iter_done",
            "it_id": it_id,
            "iteration": tm.iteration_count,
            "job_id": job.id,
            "query": next_q,
            "topics": tm.covered_topics[-5:],
            "knowledge_summary": tm.knowledge_summary[:400],
            "t": time.time(),
        })

        # Sleep before next iteration (interruptible every second)
        log.info("Iteration %s (iter #%d done): sleeping %ds before next run",
                  it_id, tm.iteration_count, interval)
        for _ in range(max(interval, 10)):  # minimum 10s even if interval=0
            if _iter_stop.get(it_id):
                break
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                log.info("Iteration %s: task cancelled during sleep", it_id)
                return

    # Loop exited — update status
    _iter_tasks.pop(it_id, None)
    _iter_stop.pop(it_id, None)
    it = await DB.load_iteration_target(it_id)
    if it and it.get("status") == "running":
        it["status"]     = "paused"
        it["updated_at"] = time.time()
        await DB.save_iteration_target(it)
    log.info("Iteration loop ended: %s", it_id)


async def run_job_body(job: ResearchJob, project: Optional[Project]) -> None:
    """Run a research job without the DB-save wrapper (used by iteration engine).
    Does NOT raise — errors are stored in job.error so the loop can continue."""
    cancel_flags[job.id] = False
    try:
        if   job.mode == AgentMode.DEEP:     await run_deep(job, project)
        elif job.mode == AgentMode.PARALLEL: await run_parallel(job, project)
        else:                                await run_single(job, project)
        job.status = JobStatus.DONE if not cancel_flags.get(job.id) else JobStatus.CANCELLED
    except Exception as e:
        log.warning("run_job_body job %s failed: %s", job.id, e)
        job.status = JobStatus.ERROR
        job.error  = str(e)
        # Don't re-raise — iteration loop should continue
    finally:
        job.finished_at = time.time()
        cancel_flags.pop(job.id, None)
        history.insert(0, job)
        if len(history) > 200: history.pop()


# ══════════════════════════════════════════════════════════════════════════════
#  Pipeline Engine
#  ───────────────
#  A pipeline is an ordered list of stages. Each stage is one of:
#    - "research"   : runs a full research job (single/parallel/deep)
#    - "transform"  : LLM rewrites/condenses/expands the running context
#    - "synthesis"  : LLM produces a final combined output from all prior stages
#  Each stage carries: model tier, sources, nlp tools, output mode, and a
#  custom prompt + query template. {topic} = the pipeline input; {prev} = the
#  previous stage's output; {all} = concatenation of every prior stage output.
# ══════════════════════════════════════════════════════════════════════════════

# In-memory registry of active pipeline runs
_pipeline_runs:  dict[str, dict]  = {}   # run_id → run state
_pipeline_stop:  dict[str, bool]  = {}   # run_id → stop flag


def _apply_stage_template(template: str, topic: str, prev: str, all_ctx: str) -> str:
    """Substitute {topic}, {prev}, {all} placeholders in a stage query/prompt."""
    if not template:
        return topic
    return (template
            .replace("{topic}", topic or "")
            .replace("{prev}", (prev or "")[:6000])
            .replace("{all}", (all_ctx or "")[:10000]))


async def _run_pipeline_stage(
    stage: dict, stage_idx: int, run_id: str,
    topic: str, prev_output: str, all_outputs: list[str],
) -> dict:
    """
    Execute a single pipeline stage. Returns a dict with the stage result.
    Stage schema:
      { name, kind, mode, output_mode, sources[], nlp_tools[],
        model_tier, query_template, prompt }
    """
    kind         = stage.get("kind", "research")
    name         = stage.get("name", f"Stage {stage_idx+1}")
    all_ctx      = "\n\n---\n\n".join(all_outputs)
    query        = _apply_stage_template(
        stage.get("query_template", "{topic}"), topic, prev_output, all_ctx)
    custom_prompt = stage.get("prompt", "")

    await broadcast(run_id, {"type": "pl_stage_start", "stage": stage_idx,
                             "name": name, "kind": kind, "t": time.time()})

    # ── Transform / synthesis stages — single LLM call, no search ──────────
    if kind in ("transform", "synthesis"):
        tier_map = {"thinker": ModelTier.THINKER, "writer": ModelTier.WRITER,
                    "analyst": ModelTier.ANALYST, "auto": ModelTier.WRITER}
        tier = tier_map.get(stage.get("model_tier", "writer"), ModelTier.WRITER)
        inst = await get_instance(tier) or await get_instance(ModelTier.WRITER) \
               or await get_instance(ModelTier.THINKER)
        if not inst:
            return {"name": name, "kind": kind, "output": "",
                    "error": "no model available", "status": "error"}

        if kind == "synthesis":
            sys_p = (custom_prompt or
                     "You are a research synthesiser. Combine all prior stage "
                     "outputs into one coherent, well-structured final report. "
                     "Use ## headers, preserve citations, resolve contradictions.")
            user_p = (f"Pipeline topic: {topic}\n\n"
                      f"All stage outputs:\n{all_ctx[:14000]}\n\n"
                      "Produce the final combined output.")
        else:  # transform
            sys_p = (custom_prompt or
                     "You are a research editor. Transform the provided text as "
                     "instructed, preserving all facts and citations.")
            user_p = (f"Topic: {topic}\n\nInput text:\n{prev_output[:10000]}\n\n"
                      "Apply the transformation.")
        parts: list[str] = []
        try:
            async for tok in stream_ollama(inst, user_p, sys_p, run_id,
                                           timeout_secs=WRITER_TIMEOUT):
                parts.append(tok)
                if _pipeline_stop.get(run_id): break
        except Exception as e:
            return {"name": name, "kind": kind, "output": "",
                    "error": str(e), "status": "error"}
        out = "".join(parts)
        await broadcast(run_id, {"type": "pl_stage_done", "stage": stage_idx,
                                 "name": name, "chars": len(out)})
        return {"name": name, "kind": kind, "output": out,
                "job_id": "", "status": "done", "citations": 0}

    # ── Research stage — runs a full research job ──────────────────────────
    mode_map = {"single": AgentMode.SINGLE, "parallel": AgentMode.PARALLEL,
                "deep": AgentMode.DEEP}
    om_map   = {"report": OutputMode.REPORT, "guide": OutputMode.GUIDE,
                "filestore": OutputMode.FILESTORE, "code": OutputMode.CODE}
    mode = mode_map.get(stage.get("mode", "single"), AgentMode.SINGLE)
    om   = om_map.get(stage.get("output_mode", "report"), OutputMode.REPORT)

    stage_sources = stage.get("sources", [])
    if not stage_sources:
        stage_sources = [s.id for s in sources if s.enabled]

    # Prior stage output becomes the job's prior_context
    prior = prev_output if stage_idx > 0 else ""

    job = ResearchJob(
        id=str(uuid.uuid4()), query=query,
        mode=mode, output_mode=om,
        sources=stage_sources, status=JobStatus.QUEUED,
        created_at=time.time(),
        prior_context=prior[:5000],
        context_mode="continue" if prior else "fresh",
        pipeline_nlp_tools=stage.get("nlp_tools") or None,
        pipeline_writer_prompt=custom_prompt or "",
    )
    jobs[job.id] = job

    try:
        await DB.save_job(job)
    except Exception:
        pass

    await run_job_body(job, None)

    try:
        await DB.save_job(job)
    except Exception:
        pass

    await broadcast(run_id, {"type": "pl_stage_done", "stage": stage_idx,
                             "name": name, "job_id": job.id,
                             "chars": len(job.result or ""),
                             "citations": len(job.citations)})

    return {"name": name, "kind": kind, "output": job.result or "",
            "job_id": job.id, "status": job.status,
            "citations": len(job.citations)}


async def _run_pipeline(run_id: str) -> None:
    """Execute every stage of a pipeline run sequentially."""
    run = _pipeline_runs.get(run_id)
    if not run:
        return
    run["status"] = "running"
    run["updated_at"] = time.time()
    await broadcast(run_id, {"type": "pl_start", "run_id": run_id,
                             "stages": len(run["stages"])})

    topic        = run["topic"]
    stage_results: list[dict] = []
    all_outputs:  list[str]   = []
    prev_output  = ""
    job_ids:      list[str]   = []

    try:
        for idx, stage in enumerate(run["stages"]):
            if _pipeline_stop.get(run_id):
                run["status"] = "cancelled"
                break
            result = await _run_pipeline_stage(
                stage, idx, run_id, topic, prev_output, all_outputs)
            stage_results.append(result)
            run["stage_results"] = stage_results
            if result.get("job_id"):
                job_ids.append(result["job_id"])
            out = result.get("output", "")
            if out:
                prev_output = out
                all_outputs.append(f"## {result['name']}\n\n{out}")
            # Persist progress
            run["updated_at"] = time.time()
            try:
                await DB.save_pipeline_run({
                    "id": run_id, "pipeline_id": run["pipeline_id"],
                    "pipeline_name": run["pipeline_name"],
                    "status": run["status"], "stages": stage_results,
                    "final_result": prev_output, "job_ids": job_ids,
                    "error": run.get("error", ""),
                    "created_at": run["created_at"], "updated_at": run["updated_at"],
                })
            except Exception:
                pass
            if result.get("status") == "error":
                run["error"] = result.get("error", "stage failed")

        if run["status"] not in ("cancelled",):
            run["status"] = "done"
        run["final_result"] = prev_output
        run["job_ids"] = job_ids

    except Exception as e:
        log.warning("pipeline run %s failed: %s", run_id, e)
        run["status"] = "error"
        run["error"]  = str(e)
    finally:
        run["updated_at"] = time.time()
        try:
            await DB.save_pipeline_run({
                "id": run_id, "pipeline_id": run["pipeline_id"],
                "pipeline_name": run["pipeline_name"],
                "status": run["status"], "stages": stage_results,
                "final_result": run.get("final_result", ""), "job_ids": job_ids,
                "error": run.get("error", ""),
                "created_at": run["created_at"], "updated_at": run["updated_at"],
            })
        except Exception:
            pass
        await broadcast(run_id, {"type": "pl_done", "run_id": run_id,
                                 "status": run["status"],
                                 "final_result": run.get("final_result", ""),
                                 "error": run.get("error", "")})
        _pipeline_stop.pop(run_id, None)


# ── Pipeline API models ──────────────────────────────────────────────────────

class PipelineStage(BaseModel):
    name:           str  = "Stage"
    kind:           str  = "research"   # research | transform | synthesis
    mode:           str  = "single"     # single | parallel | deep
    output_mode:    str  = "report"     # report | guide | filestore | code
    model_tier:     str  = "auto"       # thinker | writer | analyst | auto
    sources:        list[str] = Field(default_factory=list)
    nlp_tools:      list[str] = Field(default_factory=list)
    query_template: str  = "{topic}"
    prompt:         str  = ""

class PipelineSaveRequest(BaseModel):
    id:          Optional[str] = None
    name:        str = "Untitled Pipeline"
    description: str = ""
    stages:      list[PipelineStage] = Field(default_factory=list)
    tags:        list[str] = Field(default_factory=list)
    project_id:  Optional[str] = None

class PipelineRunRequest(BaseModel):
    pipeline_id: str
    topic:       str = Field(..., min_length=1, max_length=4000)


@app.get("/api/pipelines")
async def list_pipelines(project_id: Optional[str] = None):
    return await DB.load_pipelines(project_id)


@app.get("/api/pipelines/{pl_id}")
async def get_pipeline(pl_id: str):
    pl = await DB.load_pipeline(pl_id)
    if not pl:
        raise HTTPException(404, "Pipeline not found")
    return pl


@app.post("/api/pipelines")
async def save_pipeline(req: PipelineSaveRequest):
    pl_id = req.id or str(uuid.uuid4())[:16]
    existing = await DB.load_pipeline(pl_id) if req.id else None
    pl = {
        "id":          pl_id,
        "name":        req.name,
        "description": req.description,
        "stages":      [s.model_dump() for s in req.stages],
        "tags":        req.tags,
        "project_id":  req.project_id or "",
        "created_at":  existing["created_at"] if existing else time.time(),
        "updated_at":  time.time(),
    }
    await DB.save_pipeline(pl)
    return pl


@app.delete("/api/pipelines/{pl_id}")
async def delete_pipeline_ep(pl_id: str):
    await DB.delete_pipeline(pl_id)
    return {"ok": True}


@app.post("/api/pipelines/run")
async def run_pipeline_ep(req: PipelineRunRequest, bg: BackgroundTasks):
    pl = await DB.load_pipeline(req.pipeline_id)
    if not pl:
        raise HTTPException(404, "Pipeline not found")
    if not pl.get("stages"):
        raise HTTPException(400, "Pipeline has no stages")
    run_id = str(uuid.uuid4())
    run = {
        "id":            run_id,
        "pipeline_id":   pl["id"],
        "pipeline_name": pl["name"],
        "topic":         req.topic,
        "stages":        pl["stages"],
        "stage_results": [],
        "status":        "queued",
        "final_result":  "",
        "job_ids":       [],
        "error":         "",
        "created_at":    time.time(),
        "updated_at":    time.time(),
    }
    _pipeline_runs[run_id] = run
    _pipeline_stop[run_id] = False
    bg.add_task(_run_pipeline, run_id)
    return {"run_id": run_id, "status": "queued",
            "pipeline_name": pl["name"], "stages": len(pl["stages"])}


@app.post("/api/pipelines/run/{run_id}/stop")
async def stop_pipeline_run(run_id: str):
    if run_id in _pipeline_runs:
        _pipeline_stop[run_id] = True
        return {"ok": True}
    return {"ok": False, "reason": "run not found"}


@app.get("/api/pipelines/runs")
async def list_pipeline_runs(pipeline_id: Optional[str] = None):
    # Merge in-memory live runs with persisted ones
    db_runs = await DB.load_pipeline_runs(pipeline_id, limit=50)
    db_ids  = {r["id"] for r in db_runs}
    live = [
        {"id": r["id"], "pipeline_id": r["pipeline_id"],
         "pipeline_name": r["pipeline_name"], "status": r["status"],
         "stages": r.get("stage_results", []),
         "final_result": r.get("final_result", ""),
         "job_ids": r.get("job_ids", []), "error": r.get("error", ""),
         "created_at": r["created_at"], "updated_at": r["updated_at"]}
        for rid, r in _pipeline_runs.items()
        if rid not in db_ids and (not pipeline_id or r["pipeline_id"] == pipeline_id)
    ]
    return live + db_runs


@app.get("/api/pipelines/runs/{run_id}")
async def get_pipeline_run(run_id: str):
    if run_id in _pipeline_runs:
        r = _pipeline_runs[run_id]
        return {"id": r["id"], "pipeline_id": r["pipeline_id"],
                "pipeline_name": r["pipeline_name"], "status": r["status"],
                "stages": r.get("stage_results", []),
                "final_result": r.get("final_result", ""),
                "job_ids": r.get("job_ids", []), "error": r.get("error", ""),
                "created_at": r["created_at"], "updated_at": r["updated_at"]}
    run = await DB.load_pipeline_run(run_id)
    if not run:
        raise HTTPException(404, "Pipeline run not found")
    return run


@app.delete("/api/pipelines/runs/{run_id}")
async def delete_pipeline_run_ep(run_id: str):
    _pipeline_runs.pop(run_id, None)
    await DB.delete_pipeline_run(run_id)
    return {"ok": True}


@app.websocket("/ws/pipeline/{run_id}")
async def pipeline_ws(ws: WebSocket, run_id: str):
    await ws.accept()
    ws_clients.setdefault(run_id, []).append(ws)
    try:
        while True:
            await ws.receive_text()
    except Exception:
        pass
    finally:
        try: ws_clients.get(run_id, []).remove(ws)
        except ValueError: pass


# ── Iteration API endpoints ────────────────────────────────────────────────

class IterCreateRequest(BaseModel):
    target_type:   str        = "project"   # project | job | notebook
    target_id:     str
    seed_query:    str
    mode:          AgentMode  = AgentMode.SINGLE
    output_mode:   OutputMode = OutputMode.REPORT
    interval_secs: int        = 300         # seconds between iterations
    autostart:     bool       = True


@app.post("/api/iterate")
async def create_iteration(req: IterCreateRequest):
    """Create (and optionally start) a continuous iteration target."""
    it = {
        "id":            str(uuid.uuid4())[:16],
        "target_type":   req.target_type,
        "target_id":     req.target_id,
        "status":        "running" if req.autostart else "paused",
        "mode":          req.mode.value,
        "output_mode":   req.output_mode.value,
        "interval_secs": req.interval_secs,
        "seed_query":    req.seed_query,
        "traversal_map": {},
        "created_at":    time.time(),
        "updated_at":    time.time(),
    }
    await DB.save_iteration_target(it)
    if req.autostart:
        task = asyncio.create_task(_run_iteration_loop(it["id"]))
        _iter_tasks[it["id"]] = task
    return it


@app.get("/api/iterate")
async def list_iterations(status: Optional[str] = None):
    return await DB.load_iteration_targets(status)


@app.get("/api/iterate/{it_id}")
async def get_iteration(it_id: str):
    it = await DB.load_iteration_target(it_id)
    if not it: raise HTTPException(404, "Iteration not found")
    it["running"] = it_id in _iter_tasks
    return it


@app.post("/api/iterate/{it_id}/start")
async def start_iteration(it_id: str):
    it = await DB.load_iteration_target(it_id)
    if not it: raise HTTPException(404, "Iteration not found")
    if it_id in _iter_tasks and not _iter_tasks[it_id].done():
        return {"ok": True, "status": "already_running"}
    it["status"]     = "running"
    it["updated_at"] = time.time()
    await DB.save_iteration_target(it)
    _iter_stop.pop(it_id, None)
    task = asyncio.create_task(_run_iteration_loop(it_id))
    _iter_tasks[it_id] = task
    return {"ok": True, "status": "running"}


@app.post("/api/iterate/{it_id}/pause")
async def pause_iteration(it_id: str):
    it = await DB.load_iteration_target(it_id)
    if not it: raise HTTPException(404, "Iteration not found")
    _iter_stop[it_id] = True
    it["status"]     = "paused"
    it["updated_at"] = time.time()
    await DB.save_iteration_target(it)
    return {"ok": True, "status": "paused"}


@app.post("/api/iterate/{it_id}/stop")
async def stop_iteration(it_id: str):
    it = await DB.load_iteration_target(it_id)
    if not it: raise HTTPException(404, "Iteration not found")
    _iter_stop[it_id] = True
    task = _iter_tasks.get(it_id)
    if task and not task.done(): task.cancel()
    it["status"]     = "stopped"
    it["updated_at"] = time.time()
    await DB.save_iteration_target(it)
    await DB.delete_iteration_target(it_id)
    _iter_tasks.pop(it_id, None)
    _iter_stop.pop(it_id, None)
    return {"ok": True, "status": "stopped"}


@app.patch("/api/iterate/{it_id}")
async def update_iteration(it_id: str, payload: dict):
    it = await DB.load_iteration_target(it_id)
    if not it: raise HTTPException(404, "Iteration not found")
    for k in ("interval_secs", "mode", "output_mode", "seed_query"):
        if k in payload: it[k] = payload[k]
    it["updated_at"] = time.time()
    await DB.save_iteration_target(it)
    return it


@app.get("/api/iterate/{it_id}/map")
async def get_traversal_map(it_id: str):
    it = await DB.load_iteration_target(it_id)
    if not it: raise HTTPException(404, "Iteration not found")
    return it.get("traversal_map", {})


# ── Resume any running iterations on startup ──────────────────────────────
async def _resume_iterations() -> None:
    """Called from lifespan startup — resume any targets that were running."""
    try:
        running = await DB.load_iteration_targets(status="running")
        for it in running:
            if it["id"] not in _iter_tasks:
                task = asyncio.create_task(_run_iteration_loop(it["id"]))
                _iter_tasks[it["id"]] = task
                log.info("Resumed iteration: %s (seed: %s)", it["id"], it["seed_query"][:60])
    except Exception as e:
        log.warning("Could not resume iterations: %s", e)




@app.websocket("/ws/iterate/{it_id}")
async def iterate_ws(ws: WebSocket, it_id: str):
    """Subscribe to iteration progress for a target."""
    await ws.accept()
    ws_clients.setdefault(it_id, []).append(ws)
    try:
        while True: await ws.receive_text()
    except Exception: pass
    finally:
        try: ws_clients.get(it_id,[]).remove(ws)
        except ValueError: pass

# ══════════════════════════════════════════════════════════════════════════════
#  New /api/expand endpoint  — dive deeper into any part of a result
# ══════════════════════════════════════════════════════════════════════════════

class ExpandRequest(BaseModel):
    job_id:    Optional[str] = None   # context job (existing result)
    text:      str                    # selected passage to expand
    query:     Optional[str] = None   # override query (if not from job)
    mode:      str = "single"         # "single" | "deep"


@app.post("/api/expand")
async def expand_section(req: ExpandRequest, bg: BackgroundTasks):
    """
    Dive deeper into a selected passage of an existing result.
    Thinker plans what to look up; Writer gathers in parallel; result streamed.
    """
    thinker = await get_instance(ModelTier.THINKER)
    writer  = await get_instance(ModelTier.WRITER) or thinker
    if not writer:
        raise HTTPException(503, "No model available")

    base_query = req.query or req.text[:200]

    # Create a new job for the expansion
    job = ResearchJob(
        id=str(uuid.uuid4()),
        query=f"Expand: {base_query[:120]}",
        mode=AgentMode(req.mode) if req.mode in ("parallel","deep") else AgentMode.SINGLE,
        output_mode=OutputMode.REPORT,
        sources=[s.id for s in sources if s.enabled],
        status=JobStatus.QUEUED,
        created_at=time.time(),
        prior_context=req.text,
        context_mode="continue",
    )
    jobs[job.id] = job
    await DB.save_job(job)

    async def _run():
        cancel_flags[job.id] = False
        try:
            # Step 1: Thinker plans expansion targets from the selected text
            expansions = []
            if thinker:
                expansions = await _plan_expansions(
                    req.text, base_query, thinker, job.id, max_expansions=3)
            if not expansions:
                expansions = [{"section": req.text[:80], "expansion_query": base_query}]

            # Step 2: Writer gathers each expansion target concurrently
            addendum = await _run_expansions(expansions, job, writer, req.text)

            # Step 3: Thinker synthesises into a focused expansion report
            if thinker and addendum:
                slot_t = slot_for(ModelTier.THINKER)
                slot_on(slot_t, thinker, job.id, "synthesising")
                cit_ref = "\n".join(f"[{i+1}] {c.title} — {c.url}"
                                     for i, c in enumerate(job.citations[:20]))
                parts: list[str] = []
                async for tok in stream_ollama(thinker,
                    f"Selected text to expand:\n{req.text[:1000]}\n\n"
                    f"Gathered expansion material:\n{addendum[:6000]}\n\n"
                    f"Citations:\n{cit_ref}\n\n"
                    "Write a comprehensive, well-cited expansion of the selected text. "
                    "Include ALL relevant data, tables, images found. Cite as [N].",
                    WRITE_SYS, job.id, slot_t):
                    parts.append(tok)
                    if cancel_flags.get(job.id): break
                slot_off(slot_t)
                job.result = "".join(parts)
            else:
                job.result = addendum.strip() or "No additional information found."

            job.status = JobStatus.DONE
        except Exception as e:
            job.status = JobStatus.ERROR
            job.error = str(e)
            log.warning("expand_section failed: %s", e)
        finally:
            job.finished_at = time.time()
            try: await DB.save_job(job)
            except Exception: pass
            history.insert(0, job)
            if len(history) > 200: history.pop()
            await broadcast(job.id, {"type": "done", "result": job.result or "",
                                      "status": job.status, "job_id": job.id})

    bg.add_task(_run)
    return {"job_id": job.id, "status": "queued"}




@app.websocket("/ws/stream/{job_id}")
async def ws_stream(ws: WebSocket, job_id: str):
    await ws.accept()
    ws_clients.setdefault(job_id, []).append(ws)
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect: pass
    finally:
        try: ws_clients.get(job_id,[]).remove(ws)
        except ValueError: pass

@app.get("/api/debug/screenshot")
async def debug_screenshot(url: str = Query("https://example.com")):
    """Test screenshot capture with full diagnostics."""
    import sys, importlib
    result = {
        "url": url,
        "playwright_importable": False,
        "playwright_version": None,
        "browser_launched": False,
        "browser_error": None,
        "screenshot_attempted": False,
        "screenshot_result": None,
        "screenshot_error": None,
        "fallback_used": None,
        "file_written": False,
        "file_path": None,
    }

    # Check playwright import
    try:
        import playwright
        result["playwright_importable"] = True
        result["playwright_version"] = getattr(playwright, "__version__", "unknown")
    except ImportError as e:
        result["browser_error"] = f"playwright not installed: {e}"
        return result

    # Check browser
    try:
        browser = await _get_browser()
        result["browser_launched"] = browser is not None and browser.is_connected()
    except Exception as e:
        result["browser_error"] = str(e)

    # Attempt full screenshot
    try:
        result["screenshot_attempted"] = True
        path = await capture_screenshot(url)
        result["screenshot_result"] = path
        full = SCREENSHOT_DIR / path
        result["file_written"] = full.exists()
        result["file_path"] = str(full)
        result["fallback_used"] = path.endswith(".svg")
    except Exception as e:
        result["screenshot_error"] = str(e)

    return result
# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
#  Vera Capability Registration
# ═══════════════════════════════════════════════════════════════════════════════
# When loaded as a Vera module, register all research functions as capabilities.
# The @app routes above still work (they bind to APP in Vera mode, or the
# standalone app otherwise), so the HTTP API is backwards-compatible.
# The capabilities below add MCP, DAG, and cap-tracking on top.

if _VERA_MODE:
    try:
        import research_fabric as _rf
    except ImportError:
        from Vera.Orchestration import research_fabric as _rf

    # ── Startup init (replaces the standalone lifespan in Vera mode) ──────────
    # Runs as a background task so it doesn't block module loading.
    async def _research_startup():
        """Initialise research subsystem when running inside the orchestrator."""
        global sources, instances, web_cfg, projects, bookmarks
        try:
            await DB.init()
        except Exception as e:
            log.warning("research_startup DB.init: %s", e)

        # Load persisted sources
        try:
            saved_sources = await DB.load_sources()
            if saved_sources:
                loaded = []
                for row in saved_sources:
                    try:
                        # Ensure config is a dict (may come back as JSON string from fabric)
                        cfg = row.get("config", {})
                        if isinstance(cfg, str):
                            try: cfg = json.loads(cfg)
                            except Exception: cfg = {}
                        if not isinstance(cfg, dict):
                            cfg = {}
                        # Ensure enabled is a real bool (not string "True"/"False")
                        en = row.get("enabled", True)
                        if isinstance(en, str):
                            en = en.lower() not in ("false", "0", "no", "")
                        loaded.append(DataSource(
                            id=row["id"], label=row["label"],
                            type=SourceType(row["type"]),
                            enabled=bool(en),
                            config=cfg,
                            status=row.get("status", "unknown"),
                        ))
                    except Exception as se:
                        log.debug("research_startup skip source %s: %s", row.get("id","?"), se)
                if loaded:
                    sources = loaded
                    log.info("Research: loaded %d sources from fabric", len(sources))
            else:
                # No saved sources — try vera_config.json before falling back to defaults
                try:
                    fc = _load_config_file()
                    if fc.get("sources"):
                        loaded_fc = []
                        for row in fc["sources"]:
                            try:
                                cfg = row.get("config", {})
                                if isinstance(cfg, str):
                                    try: cfg = json.loads(cfg)
                                    except Exception: cfg = {}
                                loaded_fc.append(DataSource(
                                    id=row["id"], label=row["label"],
                                    type=SourceType(row["type"]),
                                    enabled=bool(row.get("enabled", True)),
                                    config=cfg,
                                    status=row.get("status", "unknown"),
                                ))
                            except Exception:
                                pass
                        if loaded_fc:
                            sources = loaded_fc
                            log.info("Research: loaded %d sources from config file", len(sources))
                except Exception:
                    pass
                if sources is DEFAULT_SOURCES or len(sources) == len(DEFAULT_SOURCES):
                    log.info("Research: using %d default sources", len(sources))
        except Exception as e:
            log.warning("research_startup sources: %s", e)

        # Ensure new default sources (fabric, memory) are always present
        # even if loaded from an older save that didn't have them
        existing_ids = {s.id for s in sources}
        for default_src in DEFAULT_SOURCES:
            if default_src.id not in existing_ids:
                sources.append(default_src)
                log.info("Research: added missing default source: %s", default_src.id)

        # Deduplicate sources by id — fabric round-trips can create dupes
        _seen_sids: set = set()
        _deduped: list = []
        for s in sources:
            if s.id not in _seen_sids:
                _seen_sids.add(s.id)
                _deduped.append(s)
        if len(_deduped) != len(sources):
            log.warning("Research: deduped sources %d → %d", len(sources), len(_deduped))
            sources = _deduped
            try: await DB.save_sources(sources)
            except Exception: pass

        # Log active sources so we can debug search issues
        active = [(s.id, s.enabled, type(s.config).__name__) for s in sources]
        log.info("Research: sources: %s", active)

        # Load persisted instances
        try:
            saved_insts = await DB.load_instances()
            if saved_insts:
                loaded_insts = []
                for row in saved_insts:
                    try:
                        loaded_insts.append(OllamaInstance(
                            name=row["name"], host=row["host"], port=int(row["port"]),
                            tier=ModelTier(row["tier"]), model=row["model"],
                            ctx_size=int(row.get("ctx_size", 8192)),
                            enabled=bool(row.get("enabled", True)),
                        ))
                    except Exception:
                        pass
                if loaded_insts:
                    instances = loaded_insts
                    log.info("Research: loaded %d instances from fabric", len(instances))
        except Exception as e:
            log.debug("research_startup instances: %s", e)

        # Load web search config
        try:
            saved_ws = await DB.load_web_search_config()
            if saved_ws:
                web_cfg.engine        = saved_ws.get("engine", web_cfg.engine)
                web_cfg.result_count  = int(saved_ws.get("result_count", web_cfg.result_count))
                web_cfg.crawl_depth   = int(saved_ws.get("crawl_depth", web_cfg.crawl_depth))
                web_cfg.crawl_breadth = int(saved_ws.get("crawl_breadth", web_cfg.crawl_breadth))
                web_cfg.crawl_timeout = float(saved_ws.get("crawl_timeout", web_cfg.crawl_timeout))
                web_cfg.include_archive = bool(saved_ws.get("include_archive", False))
                web_cfg.safe_search   = int(saved_ws.get("safe_search", 0))
        except Exception as e:
            log.debug("research_startup web_cfg: %s", e)

        # Load projects
        try:
            saved_projects = await DB.load_projects()
            for row in saved_projects:
                if row["id"] not in projects:
                    proj = Project(
                        id=row["id"], name=row["name"],
                        description=row.get("description", ""),
                        output_mode=OutputMode.REPORT,
                        context_summary=row.get("context_summary", ""),
                        created_at=float(row.get("created_at", time.time())),
                        updated_at=float(row.get("updated_at", time.time())),
                    )
                    projects[proj.id] = proj
            log.info("Research: loaded %d projects", len(projects))
        except Exception as e:
            log.debug("research_startup projects: %s", e)

        # Load bookmarks
        try:
            saved_bmarks = await DB.load_bookmarks()
            for bm in saved_bmarks:
                bookmarks[bm["id"]] = bm
        except Exception as e:
            log.debug("research_startup bookmarks: %s", e)

        # Mount screenshots directory on the orchestrator app
        try:
            from fastapi.staticfiles import StaticFiles
            _VERA_APP.mount("/screenshots", StaticFiles(directory=str(SCREENSHOT_DIR)), name="screenshots")
        except Exception:
            pass

        log.info("Research subsystem ready (Vera mode, %d sources, %d instances)",
                 len(sources), len(instances))

    # Schedule startup — runs as soon as the event loop ticks after module load
    try:
        asyncio.get_event_loop().create_task(_research_startup())
    except RuntimeError:
        # No running loop yet — will be called when orchestrator starts
        # via the module's presence in the lifespan load sequence
        pass

    # ── Pipeline ──────────────────────────────────────────────────────────────

    @capability(
        "research.run",
        http_method="POST", http_path="/research/run",
        http_tags=["research", "pipeline"], memory="on",
        description="Submit a research job. Returns {job_id, status}.",
        schema={"properties": {
            "query":        {"type": "string", "description": "Research query"},
            "mode":         {"type": "string", "enum": ["single","parallel","deep"], "default": "single"},
            "output_mode":  {"type": "string", "enum": ["report","guide","code","filestore"], "default": "report"},
            "sources":      {"type": "string", "default": "", "description": "Comma-sep source IDs or empty for defaults"},
            "project_id":   {"type": "string", "default": ""},
            "context":      {"type": "string", "default": ""},
            "context_mode": {"type": "string", "default": "fresh"},
        }},
    )
    async def cap_research_run(
        query: str, mode: str = "single", output_mode: str = "report",
        sources: str = "", project_id: str = "", context: str = "",
        context_mode: str = "fresh", trace_id=None,
    ):
        # Parse sources — the frontend sends a JSON array string like
        # '["searxng","brave"]'; older callers may send "searxng,brave".
        # Empty → use all enabled sources.
        src_list: list[str] = []
        if sources:
            s = sources.strip()
            if s.startswith("["):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        src_list = [str(x).strip() for x in parsed if str(x).strip()]
                except Exception:
                    log.warning("cap_research_run: could not parse sources JSON: %r", s[:120])
            if not src_list:
                src_list = [x.strip().strip('[]"\' ') for x in s.split(",")]
                src_list = [x for x in src_list if x]
        if not src_list:
            src_list = [src.id for src in globals()["sources"] if src.enabled]
        log.info("cap_research_run: query=%r sources=%s", query[:60], src_list)
        job = ResearchJob(
            id=str(uuid.uuid4()), query=query,
            mode=AgentMode(mode), output_mode=OutputMode(output_mode),
            sources=src_list, status=JobStatus.QUEUED,
            created_at=time.time(), project_id=project_id or None,
            prior_context=context or "", context_mode=context_mode or "fresh",
        )
        jobs[job.id] = job
        try: await DB.save_job(job)
        except Exception: pass
        asyncio.create_task(run_job(job))
        return {"job_id": job.id, "status": "queued"}

    # ── Convenience aliases (backwards compat with old research_capabilities.py) ──
    # These wrap research.run with preset mode/output_mode combinations.

    _ALIAS_SCHEMA = {"properties": {
        "query":        {"type": "string", "description": "Research query"},
        "project_id":   {"type": "string", "default": ""},
        "context":      {"type": "string", "default": ""},
        "context_mode": {"type": "string", "default": "fresh"},
    }}

    @capability("research.report", http_method="POST", http_path="/research/report",
                http_tags=["research", "pipeline"], memory="on",
                description="Report pipeline (single mode, report output).",
                schema=_ALIAS_SCHEMA)
    async def cap_report(query: str, project_id: str = "", context: str = "",
                         context_mode: str = "fresh", trace_id=None):
        return await cap_research_run(query=query, mode="single", output_mode="report",
                                      project_id=project_id, context=context,
                                      context_mode=context_mode, trace_id=trace_id)

    @capability("research.parallel", http_method="POST", http_path="/research/parallel",
                http_tags=["research", "pipeline"], memory="on",
                description="Parallel pipeline (parallel mode, report output).",
                schema=_ALIAS_SCHEMA)
    async def cap_parallel(query: str, project_id: str = "", context: str = "",
                           context_mode: str = "fresh", trace_id=None):
        return await cap_research_run(query=query, mode="parallel", output_mode="report",
                                      project_id=project_id, context=context,
                                      context_mode=context_mode, trace_id=trace_id)

    @capability("research.deep", http_method="POST", http_path="/research/deep",
                http_tags=["research", "pipeline"], memory="on",
                description="Deep pipeline (deep mode, report output).",
                schema=_ALIAS_SCHEMA)
    async def cap_deep(query: str, project_id: str = "", context: str = "",
                       context_mode: str = "fresh", trace_id=None):
        return await cap_research_run(query=query, mode="deep", output_mode="report",
                                      project_id=project_id, context=context,
                                      context_mode=context_mode, trace_id=trace_id)

    @capability("research.code", http_method="POST", http_path="/research/code",
                http_tags=["research", "pipeline"], memory="on",
                description="Code pipeline (deep mode, code output).",
                schema=_ALIAS_SCHEMA)
    async def cap_code(query: str, project_id: str = "", context: str = "",
                       context_mode: str = "fresh", trace_id=None):
        return await cap_research_run(query=query, mode="deep", output_mode="code",
                                      project_id=project_id, context=context,
                                      context_mode=context_mode, trace_id=trace_id)

    @capability("research.guide", http_method="POST", http_path="/research/guide",
                http_tags=["research", "pipeline"], memory="on",
                description="Guide pipeline (single mode, guide output).",
                schema=_ALIAS_SCHEMA)
    async def cap_guide(query: str, project_id: str = "", context: str = "",
                        context_mode: str = "fresh", trace_id=None):
        return await cap_research_run(query=query, mode="single", output_mode="guide",
                                      project_id=project_id, context=context,
                                      context_mode=context_mode, trace_id=trace_id)

    @capability("research.filestore", http_method="POST", http_path="/research/filestore",
                http_tags=["research", "pipeline"], memory="on",
                description="Filestore pipeline (deep mode, filestore output).",
                schema=_ALIAS_SCHEMA)
    async def cap_filestore(query: str, project_id: str = "", context: str = "",
                            context_mode: str = "fresh", trace_id=None):
        return await cap_research_run(query=query, mode="deep", output_mode="filestore",
                                      project_id=project_id, context=context,
                                      context_mode=context_mode, trace_id=trace_id)

    @capability(
        "research.chain.continue",
        http_method="POST", http_path="/research/chain/continue",
        http_tags=["research", "pipeline"], memory="on",
        description="Continue a chained coding job.",
        schema={"properties": {
            "chain_id":   {"type": "string"},
            "job_id":     {"type": "string", "default": ""},
            "project_id": {"type": "string", "default": ""},
        }},
    )
    async def cap_chain_continue(chain_id: str, job_id: str = "", project_id: str = "", trace_id=None):
        chain = chain_store.get(chain_id)
        if not chain:
            return {"ok": False, "error": f"Chain '{chain_id}' not found or expired"}
        if chain.is_complete:
            return {"ok": False, "reason": "Chain already complete"}
        if not chain.files_pending:
            chain.is_complete = True; chain_store.pop(chain_id, None)
            return {"ok": False, "reason": "No files pending"}
        cont_job = ResearchJob(
            id=str(uuid.uuid4()), query=chain.original_task,
            mode=AgentMode.DEEP, output_mode=OutputMode.CODE,
            sources=[], status=JobStatus.QUEUED,
            created_at=time.time(), project_id=project_id or None,
            chain_ctx=chain,
        )
        jobs[cont_job.id] = cont_job
        asyncio.create_task(run_job(cont_job))
        return {"job_id": cont_job.id, "chain_id": chain.chain_id,
                "run_number": chain.run_number, "files_pending": chain.files_pending,
                "files_done": chain.files_done}

    @capability(
        "research.chain.status",
        http_method="GET", http_path="/research/chain/status",
        http_tags=["research"], memory="off",
        description="Get chain status.",
        schema={"properties": {"chain_id": {"type": "string"}}},
    )
    async def cap_chain_status(chain_id: str, trace_id=None):
        chain = chain_store.get(chain_id)
        if not chain:
            return {"ok": False, "error": "Chain not found"}
        return {"chain_id": chain.chain_id, "run_number": chain.run_number,
                "original_task": chain.original_task, "files_planned": chain.files_planned,
                "files_done": chain.files_done, "files_pending": chain.files_pending,
                "is_complete": chain.is_complete, "summary": chain.continuity_summary}

    @capability(
        "research.crawl_additional",
        http_method="POST", http_path="/research/crawl_additional",
        http_tags=["research", "pipeline"], memory="off",
        description="Deep crawl a URL and append results to an existing job.",
        schema={"properties": {
            "job_id": {"type": "string"},
            "url":    {"type": "string"},
            "depth":  {"type": "integer", "default": 2},
        }},
    )
    async def cap_crawl_additional(job_id: str, url: str, depth: int = 2, trace_id=None):
        async def _do():
            await broadcast(job_id, {"type":"step","t":time.time(),"label":"Deep crawl","detail":url[:80]})
            text = await deep_crawl_url(url, depth, web_cfg.crawl_breadth, web_cfg.crawl_timeout, job_id=job_id)
            if text:
                from urllib.parse import urlparse
                dom = urlparse(url).netloc
                cit = Citation(id=str(uuid.uuid4())[:8], url=url, title=f"Crawled: {dom}",
                               snippet=text[:300], source_type="crawl", full_text=text)
                await broadcast(job_id, {"type":"citations","citations":[cit.to_dict_full()]})
                await broadcast(job_id, {"type":"crawl_done","url":url,"chars":len(text),"title":cit.title})
                job = jobs.get(job_id)
                if job:
                    job.citations.append(cit)
                    try: await DB.save_job(job)
                    except Exception: pass
            else:
                await broadcast(job_id, {"type":"crawl_done","url":url,"chars":0,"title":url})
        asyncio.create_task(_do())
        return {"ok": True, "message": f"Crawling {url} at depth {depth}"}

    @capability(
        "research.format_section",
        http_method="POST", http_path="/research/format_section",
        http_tags=["research"], memory="off",
        description="Reformat raw research section into publication-quality markdown.",
        schema={"properties": {
            "query":    {"type": "string"},
            "raw_text": {"type": "string"},
            "cits":     {"type": "string", "default": "[]", "description": "JSON array of citation dicts"},
        }},
    )
    async def cap_format_section(query: str, raw_text: str, cits: str = "[]", trace_id=None):
        writer = (await get_instance(ModelTier.WRITER)
                  or await get_instance(ModelTier.ANALYST)
                  or await get_instance(ModelTier.THINKER))
        if not writer:
            return {"error": "No model available for document formatting"}
        cit_list = json.loads(cits) if isinstance(cits, str) else cits
        cit_block = ""
        if cit_list:
            cit_block = "\n\nCitations available:\n" + "\n".join(
                f"[{i+1}] {c.get('title','?')} -- {c.get('url','')}" for i, c in enumerate(cit_list[:30]))
        prompt = f"Research query: {query}\n\nRaw section content:\n{raw_text[:8000]}{cit_block}\n\nReformat the above into a clean, professional document section."
        try:
            formatted_md = await asyncio.wait_for(
                collect_ollama(writer, prompt, DOC_FORMAT_SYS,
                               timeout_secs=_effective_timeout(writer, WRITER_TIMEOUT)),
                timeout=_effective_timeout(writer, WRITER_TIMEOUT + 30))
            html = _md_to_html(formatted_md)
            return {"html": html, "markdown": formatted_md}
        except Exception as e:
            return {"error": f"Format failed: {e}"}

    @capability(
        "research.expand",
        http_method="POST", http_path="/research/expand",
        http_tags=["research", "pipeline"], memory="on",
        description="Dive deeper into a selected passage of an existing result.",
        schema={"properties": {
            "text":    {"type": "string", "description": "Selected passage to expand"},
            "job_id":  {"type": "string", "default": ""},
            "query":   {"type": "string", "default": ""},
            "mode":    {"type": "string", "default": "single"},
        }},
    )
    async def cap_expand(text: str, job_id: str = "", query: str = "", mode: str = "single", trace_id=None):
        thinker = await get_instance(ModelTier.THINKER)
        writer  = await get_instance(ModelTier.WRITER) or thinker
        if not writer:
            return {"error": "No model available"}
        # Delegate to the existing expand logic via its @app route handler
        from pydantic import BaseModel as _BM
        class _ER(_BM):
            job_id: str = job_id; text: str = text; query: str = query; mode: str = mode
        from fastapi import BackgroundTasks
        bg = BackgroundTasks()
        result = await expand_section(_ER(job_id=job_id, text=text, query=query, mode=mode), bg)
        await bg()  # run background tasks
        return result

    # ── Agent control ─────────────────────────────────────────────────────────

    @capability(
        "research.agent.stop",
        http_method="POST", http_path="/research/agent/stop",
        http_tags=["research"], memory="off",
        description="Cancel a running research job.",
        schema={"properties": {"job_id": {"type": "string"}}},
    )
    async def cap_agent_stop(job_id: str = "", trace_id=None):
        if job_id and job_id in cancel_flags:
            cancel_flags[job_id] = True
            return {"ok": True, "job_id": job_id}
        return {"ok": False, "error": "Job not found or not running"}

    @capability(
        "research.agents.status",
        http_method="GET", http_path="/research/agents/status",
        http_tags=["research"], memory="off", silent=True,
        description="Get real-time status of research agent slots.",
    )
    async def cap_agents_status(trace_id=None):
        return {"slots": [
            {"id": s.id, "tier": s.tier.value if hasattr(s.tier, "value") else str(s.tier),
             "status": s.status, "model": s.model or "", "tokens": s.tokens,
             "job_id": s.job_id or "",
             "elapsed": int(time.time() - s.started_at) if s.started_at and s.status != "idle" else 0}
            for s in agent_slots
        ]}

    # ── History ────────────────────────────────────────────────────────────────

    @capability(
        "research.history",
        http_method="GET", http_path="/research/history",
        http_tags=["research"], memory="off", silent=True,
        description="List research job history.",
        schema={"properties": {
            "limit":       {"type": "integer", "default": 50},
            "offset":      {"type": "integer", "default": 0},
            "project_id":  {"type": "string", "default": ""},
            "search":      {"type": "string", "default": ""},
        }},
    )
    async def cap_history(limit: int = 50, offset: int = 0, project_id: str = "",
                          search: str = "", trace_id=None):
        db_rows, total = await DB.load_history(
            limit=int(limit), offset=int(offset),
            project_id=project_id or None, search=search or None)
        live = [{"id": j.id, "query": j.query, "mode": j.mode, "output_mode": j.output_mode,
                 "status": j.status, "created_at": j.created_at, "finished_at": None,
                 "token_count": 0, "citation_count": 0, "has_files": False,
                 "error": None, "result_snippet": "Running..."}
                for j in jobs.values() if not any(r.get("id") == j.id for r in db_rows)]
        return {"jobs": live + db_rows, "total": total + len(live)}

    @capability(
        "research.history.delete",
        http_method="DELETE", http_path="/research/history/delete",
        http_tags=["research"], memory="off",
        description="Delete a research job from history.",
        schema={"properties": {"job_id": {"type": "string"}}},
    )
    async def cap_history_delete(job_id: str, trace_id=None):
        deleted = await DB.delete_job(job_id)
        global history
        history = [j for j in history if j.id != job_id]
        return {"deleted": deleted}

    @capability(
        "research.job.status",
        http_method="GET", http_path="/research/job/status",
        http_tags=["research"], memory="off",
        description="Get the current status of a research job. Used by the agent "
                    "loop's long-running-await poller to know when a job finishes. "
                    "Input: job_id (str). "
                    "Output: {job_id, status, query, mode, output_mode, error?, "
                    "result?, citations_count?, elapsed?}.",
        schema={"properties": {"job_id": {"type": "string"}}},
    )
    async def cap_job_status(job_id: str, trace_id=None):
        # 1. Check in-memory running/recent jobs first
        job = jobs.get(job_id)
        # 2. Check completed history list
        if not job:
            job = next((j for j in history if j.id == job_id), None)
        # 3. Try persistent DB
        if not job:
            try:
                job = await DB.load_job(job_id)
            except Exception as e:
                log.warning("cap_job_status: DB.load_job(%s) failed: %s",
                            job_id, e)
                job = None
        if not job:
            return {"job_id": job_id, "status": "not_found",
                    "error": f"Job {job_id} not found"}
        status_val = job.status.value if hasattr(job.status, "value") else str(job.status)
        out = {
            "job_id":      job.id,
            "status":      status_val,
            "query":       getattr(job, "query", ""),
            "mode":        str(getattr(job, "mode", "")),
            "output_mode": str(getattr(job, "output_mode", "")),
        }
        # Terminal states: include result/error
        if status_val in ("done", "completed", "finished", "cancelled"):
            out["result"] = getattr(job, "result", "") or ""
            out["citations_count"] = len(getattr(job, "citations", []))
            out["elapsed"] = round(
                (getattr(job, "finished_at", None) or job.created_at) - job.created_at, 1
            )
            out["finished_at"] = getattr(job, "finished_at", None)
        if status_val in ("error", "failed"):
            out["error"] = getattr(job, "error", "") or "unknown error"
            out["result"] = getattr(job, "result", "") or ""
        return out

    @capability(
        "research.job.result",
        http_method="GET", http_path="/research/job/result",
        http_tags=["research"], memory="off",
        description="Get full result for a research job.",
        schema={"properties": {"job_id": {"type": "string"}}},
    )
    async def cap_job_result(job_id: str, trace_id=None):
        mem_job = next((j for j in history if j.id == job_id), None)
        if not mem_job:
            mem_job = jobs.get(job_id)
        if mem_job:
            manifest = await DB.list_generated_files(job_id)
            return {"result": mem_job.result, "steps": mem_job.steps,
                    "citations": [c.to_dict() for c in mem_job.citations],
                    "mode": mem_job.mode, "output_mode": mem_job.output_mode,
                    "elapsed": round((mem_job.finished_at or mem_job.created_at) - mem_job.created_at, 1),
                    "tokens": mem_job.token_count,
                    "file_tree": list(mem_job.file_tree.keys()),
                    "file_manifest": manifest}
        row = await DB.load_job_result(job_id)
        if not row:
            return {"error": "Job not found"}
        return row

    @capability(
        "research.job.files",
        http_method="GET", http_path="/research/job/files",
        http_tags=["research"], memory="off",
        description="List generated file manifest for a job.",
        schema={"properties": {"job_id": {"type": "string"}}},
    )
    async def cap_job_files(job_id: str, trace_id=None):
        manifest = await DB.list_generated_files(job_id)
        return {"job_id": job_id, "files": manifest}

    @capability(
        "research.job.file",
        http_method="GET", http_path="/research/job/file",
        http_tags=["research"], memory="off",
        description="Get content of a single generated file.",
        schema={"properties": {
            "job_id":    {"type": "string"},
            "file_path": {"type": "string"},
        }},
    )
    async def cap_job_file(job_id: str, file_path: str, trace_id=None):
        content = await DB.get_generated_file(job_id, file_path)
        if content is None:
            disk_path = PROJECTS_DIR / "standalone" / job_id / "files" / file_path
            if disk_path.exists():
                content = disk_path.read_text(encoding="utf-8", errors="replace")
            else:
                return {"error": f"File '{file_path}' not found"}
        return {"file_path": file_path, "content": content, "size": len(content)}

    # ── Sources ───────────────────────────────────────────────────────────────

    @capability(
        "research.sources",
        http_method="GET", http_path="/research/sources",
        http_tags=["research"], memory="off", silent=True,
        description="List configured research data sources.",
    )
    async def cap_sources(trace_id=None):
        return [{"id": s.id, "label": s.label, "type": s.type.value,
                 "enabled": s.enabled, "config": s.config, "status": s.status}
                for s in globals()["sources"]]

    @capability(
        "research.sources.update",
        http_method="POST", http_path="/research/sources/update",
        http_tags=["research"], memory="off",
        description="Full replace of sources list.",
        schema={"properties": {"sources": {"type": "string", "description": "JSON array of source dicts"}}},
    )
    async def cap_sources_update(sources_json: str = "[]", trace_id=None):
        global sources
        raw = json.loads(sources_json) if isinstance(sources_json, str) else sources_json
        new = []
        for d in raw:
            new.append(DataSource(
                id=d["id"], label=d["label"], type=SourceType(d["type"]),
                enabled=bool(d.get("enabled", True)), config=d.get("config", {}),
                status=d.get("status", "unknown")))
        sources = new
        await DB.save_sources(sources)
        _write_config_file()
        return {"ok": True, "count": len(sources)}

    @capability(
        "research.sources.add",
        http_method="POST", http_path="/research/sources/add",
        http_tags=["research"], memory="off",
        description="Add a single source.",
        schema={"properties": {
            "id": {"type": "string"}, "label": {"type": "string"},
            "type": {"type": "string"}, "enabled": {"type": "boolean", "default": True},
            "config": {"type": "string", "default": "{}", "description": "JSON config dict"},
        }},
    )
    async def cap_sources_add(id: str, label: str, type: str, enabled: bool = True,
                              config: str = "{}", trace_id=None):
        global sources
        cfg = json.loads(config) if isinstance(config, str) else config
        src = DataSource(id=id, label=label, type=SourceType(type), enabled=enabled, config=cfg)
        sources = [s for s in sources if s.id != src.id]
        sources.append(src)
        await DB.save_sources(sources)
        _write_config_file()
        return {"ok": True}

    @capability(
        "research.sources.delete",
        http_method="DELETE", http_path="/research/sources/delete",
        http_tags=["research"], memory="off",
        description="Delete a source by ID.",
        schema={"properties": {"source_id": {"type": "string"}}},
    )
    async def cap_sources_delete(source_id: str, trace_id=None):
        global sources
        before = len(sources)
        sources = [s for s in sources if s.id != source_id]
        return {"deleted": before - len(sources)}

    @capability(
        "research.sources.test",
        http_method="POST", http_path="/research/sources/test",
        http_tags=["research"], memory="off",
        description="Test connectivity of a source.",
        schema={"properties": {"source_id": {"type": "string"}}},
    )
    async def cap_sources_test(source_id: str, trace_id=None):
        # Delegate to the existing @app route handler
        from pydantic import BaseModel as _BM
        class _STR(_BM):
            source_id: str = source_id
        return await test_source(_STR(source_id=source_id))

    # ── Web search config ─────────────────────────────────────────────────────

    @capability(
        "research.websearch.config.get",
        http_method="GET", http_path="/research/websearch/config",
        http_tags=["research"], memory="off", silent=True,
        description="Get web search configuration.",
    )
    async def cap_websearch_config_get(trace_id=None):
        return asdict(web_cfg)

    @capability(
        "research.websearch.config.set",
        http_method="POST", http_path="/research/websearch/config/set",
        http_tags=["research"], memory="off",
        description="Update web search configuration.",
        schema={"properties": {
            "engine":          {"type": "string", "default": ""},
            "result_count":    {"type": "integer", "default": 0},
            "crawl_depth":     {"type": "integer", "default": -1},
            "crawl_breadth":   {"type": "integer", "default": -1},
            "crawl_timeout":   {"type": "number", "default": -1},
            "include_archive": {"type": "string", "default": ""},
            "safe_search":     {"type": "integer", "default": -1},
        }},
    )
    async def cap_websearch_config_set(engine: str = "", result_count: int = 0,
                                       crawl_depth: int = -1, crawl_breadth: int = -1,
                                       crawl_timeout: float = -1, include_archive: str = "",
                                       safe_search: int = -1, trace_id=None):
        if engine:          web_cfg.engine = engine
        if result_count > 0: web_cfg.result_count = max(1, min(result_count, 20))
        if crawl_depth >= 0: web_cfg.crawl_depth = max(0, min(crawl_depth, 3))
        if crawl_breadth > 0: web_cfg.crawl_breadth = max(1, min(crawl_breadth, 10))
        if crawl_timeout > 0: web_cfg.crawl_timeout = crawl_timeout
        if include_archive: web_cfg.include_archive = include_archive.lower() in ("true", "1", "yes")
        if safe_search >= 0: web_cfg.safe_search = safe_search
        await DB.save_web_search_config(web_cfg)
        _write_config_file()
        return asdict(web_cfg)

    # ── Models / Instances ────────────────────────────────────────────────────

    @capability(
        "research.models",
        http_method="GET", http_path="/research/models",
        http_tags=["research"], memory="off", silent=True,
        description="List Ollama model instances and their available models.",
    )
    async def cap_models(trace_id=None):
        return [{"instance": i.name, "tier": i.tier, "host": i.base_url,
                 "current_model": i.model, "available": await list_models(i), "enabled": i.enabled}
                for i in instances]

    @capability(
        "research.config.instances.get",
        http_method="GET", http_path="/research/config/instances",
        http_tags=["research"], memory="off", silent=True,
        description="Get Ollama instance configurations.",
    )
    async def cap_instances_get(trace_id=None):
        return [{"name": i.name, "host": i.host, "port": i.port, "tier": i.tier,
                 "model": i.model, "ctx_size": i.ctx_size, "enabled": i.enabled,
                 "enable_thinking": i.enable_thinking, "thinking_timeout": i.thinking_timeout}
                for i in instances]

    @capability(
        "research.config.instances.set",
        http_method="POST", http_path="/research/config/instances/set",
        http_tags=["research"], memory="off",
        description="Update Ollama instance configurations.",
        schema={"properties": {"instances": {"type": "string", "description": "JSON array of instance dicts"}}},
    )
    async def cap_instances_set(instances_json: str = "[]", trace_id=None):
        global instances
        raw = json.loads(instances_json) if isinstance(instances_json, str) else instances_json
        new = []
        for d in raw:
            new.append(OllamaInstance(
                name=d["name"], host=d["host"], port=int(d["port"]),
                tier=ModelTier(d["tier"]), model=d["model"],
                ctx_size=int(d.get("ctx_size", 8192)), enabled=bool(d.get("enabled", True)),
                enable_thinking=bool(d.get("enable_thinking", False)),
                thinking_timeout=float(d.get("thinking_timeout", 0.0))))
        instances = new
        await DB.save_instances(instances)
        _write_config_file()
        return {"ok": True, "count": len(instances)}

    # ── Projects ──────────────────────────────────────────────────────────────

    @capability(
        "research.projects.create",
        http_method="POST", http_path="/research/projects/create",
        http_tags=["research"], memory="on",
        description="Create a new research project.",
        schema={"properties": {
            "name":        {"type": "string"},
            "description": {"type": "string", "default": ""},
            "output_mode": {"type": "string", "default": "report"},
        }},
    )
    async def cap_projects_create(name: str, description: str = "", output_mode: str = "report",
                                  trace_id=None):
        proj = Project(id=str(uuid.uuid4())[:12], name=name, description=description,
                       output_mode=OutputMode(output_mode))
        projects[proj.id] = proj
        await DB.save_project(proj)
        return proj.to_dict()

    @capability(
        "research.projects",
        http_method="GET", http_path="/research/projects",
        http_tags=["research"], memory="off", silent=True,
        description="List all research projects.",
    )
    async def cap_projects(trace_id=None):
        db_rows = await DB.load_projects()
        db_ids = {r["id"] for r in db_rows}
        mem_only = [p.to_dict() for p in projects.values() if p.id not in db_ids]
        return mem_only + db_rows

    @capability(
        "research.projects.get",
        http_method="GET", http_path="/research/projects/get",
        http_tags=["research"], memory="off",
        description="Get a single project with rounds.",
        schema={"properties": {"project_id": {"type": "string"}}},
    )
    async def cap_projects_get(project_id: str, trace_id=None):
        db_row = await DB.load_project(project_id)
        if db_row:
            return db_row
        p = projects.get(project_id)
        if not p:
            return {"error": "Project not found"}
        return {**p.to_dict(), "rounds": [{"id": r.id, "round_num": r.round_num,
                "query": r.query, "created_at": r.created_at} for r in p.rounds],
                "context_summary": p.context_summary}

    @capability(
        "research.projects.delete",
        http_method="DELETE", http_path="/research/projects/delete",
        http_tags=["research"], memory="off",
        description="Delete a project.",
        schema={"properties": {"project_id": {"type": "string"}}},
    )
    async def cap_projects_delete(project_id: str, trace_id=None):
        projects.pop(project_id, None)
        await DB.delete_project(project_id)
        proj_dir = PROJECTS_DIR / project_id
        if proj_dir.exists():
            import shutil; shutil.rmtree(proj_dir)
        return {"ok": True}

    @capability(
        "research.projects.add_job",
        http_method="POST", http_path="/research/projects/add_job",
        http_tags=["research"], memory="off",
        description="Add an existing completed job to a project.",
        schema={"properties": {
            "project_id": {"type": "string"},
            "job_id":     {"type": "string"},
        }},
    )
    async def cap_projects_add_job(project_id: str, job_id: str, trace_id=None):
        # Delegate to the existing route handler
        return await project_add_job(project_id, {"job_id": job_id})

    # ── DB / Stats / Export ───────────────────────────────────────────────────

    @capability(
        "research.db.stats",
        http_method="GET", http_path="/research/db/stats",
        http_tags=["research"], memory="off", silent=True,
        description="Database statistics.",
    )
    async def cap_db_stats(trace_id=None):
        return await DB.get_stats()

    @capability(
        "research.db.search",
        http_method="GET", http_path="/research/db/search",
        http_tags=["research"], memory="off",
        description="Full-text search across saved research.",
        schema={"properties": {
            "q":     {"type": "string"},
            "limit": {"type": "integer", "default": 24},
        }},
    )
    async def cap_db_search(q: str = "", limit: int = 24, trace_id=None):
        rows, total = await DB.search(q=q, limit=int(limit))
        return {"results": rows, "total": total}

    @capability(
        "research.db.export",
        http_method="POST", http_path="/research/db/export",
        http_tags=["research"], memory="off",
        description="Export all research data.",
    )
    async def cap_db_export(trace_id=None):
        return await DB.export_all()

    # ── Bookmarks ─────────────────────────────────────────────────────────────

    @capability(
        "research.bookmarks",
        http_method="GET", http_path="/research/bookmarks",
        http_tags=["research"], memory="off", silent=True,
        description="List all bookmarks.",
    )
    async def cap_bookmarks(trace_id=None):
        return await DB.load_bookmarks()

    @capability(
        "research.bookmarks.add",
        http_method="POST", http_path="/research/bookmarks/add",
        http_tags=["research"], memory="off",
        description="Add a bookmark.",
        schema={"properties": {
            "title": {"type": "string"}, "url": {"type": "string", "default": ""},
            "snippet": {"type": "string", "default": ""}, "job_id": {"type": "string", "default": ""},
            "type": {"type": "string", "default": "citation"},
        }},
    )
    async def cap_bookmarks_add(title: str, url: str = "", snippet: str = "", job_id: str = "",
                                type: str = "citation", trace_id=None):
        bm = {"id": str(uuid.uuid4())[:12], "type": type, "job_id": job_id,
              "title": title, "url": url, "snippet": snippet[:600],
              "screenshot_url": "", "source_type": "web", "domain": "",
              "tags": [], "note": "", "created_at": time.time()}
        bookmarks[bm["id"]] = bm
        await DB.save_bookmark(bm)
        return bm

    @capability(
        "research.bookmarks.update",
        http_method="PATCH", http_path="/research/bookmarks/update",
        http_tags=["research"], memory="off",
        description="Update a bookmark's note or tags.",
        schema={"properties": {
            "bm_id": {"type": "string"},
            "note":  {"type": "string", "default": ""},
            "tags":  {"type": "string", "default": "", "description": "JSON array of tags"},
        }},
    )
    async def cap_bookmarks_update(bm_id: str, note: str = "", tags: str = "", trace_id=None):
        bm = bookmarks.get(bm_id) or await DB.get_bookmark(bm_id)
        if not bm:
            return {"error": "Bookmark not found"}
        if note: bm["note"] = note
        if tags: bm["tags"] = json.loads(tags) if isinstance(tags, str) else tags
        bookmarks[bm_id] = bm
        await DB.save_bookmark(bm)
        return bm

    @capability(
        "research.bookmarks.delete",
        http_method="DELETE", http_path="/research/bookmarks/delete",
        http_tags=["research"], memory="off",
        description="Delete a bookmark.",
        schema={"properties": {"bm_id": {"type": "string"}}},
    )
    async def cap_bookmarks_delete(bm_id: str, trace_id=None):
        bookmarks.pop(bm_id, None)
        await DB.delete_bookmark(bm_id)
        return {"ok": True}

    # ── Notebooks ─────────────────────────────────────────────────────────────

    @capability(
        "research.notebook.create",
        http_method="POST", http_path="/research/notebook/create",
        http_tags=["research", "notebook"], memory="on",
        description="Create a new notebook.",
        schema={"properties": {
            "title":       {"type": "string", "default": "Untitled Notebook"},
            "description": {"type": "string", "default": ""},
            "project_id":  {"type": "string", "default": ""},
        }},
    )
    async def cap_notebook_create(title: str = "Untitled Notebook", description: str = "",
                                  project_id: str = "", trace_id=None):
        nb = {"id": str(uuid.uuid4())[:12], "title": title, "description": description,
              "project_id": project_id or None, "tags": [],
              "created_at": time.time(), "updated_at": time.time()}
        await DB.save_notebook(nb)
        return nb

    @capability(
        "research.notebook.list",
        http_method="GET", http_path="/research/notebook/list",
        http_tags=["research", "notebook"], memory="off", silent=True,
        description="List notebooks.",
        schema={"properties": {"project_id": {"type": "string", "default": ""}}},
    )
    async def cap_notebook_list(project_id: str = "", trace_id=None):
        return await DB.load_notebooks(project_id=project_id or None)

    @capability(
        "research.notebook.get",
        http_method="GET", http_path="/research/notebook/get",
        http_tags=["research", "notebook"], memory="off",
        description="Get a notebook with all cells and pages.",
        schema={"properties": {"nb_id": {"type": "string"}}},
    )
    async def cap_notebook_get(nb_id: str, trace_id=None):
        nb = await DB.load_notebook(nb_id)
        if not nb:
            return {"error": "Notebook not found"}
        return nb

    @capability(
        "research.notebook.update",
        http_method="PATCH", http_path="/research/notebook/update",
        http_tags=["research", "notebook"], memory="off",
        description="Update notebook metadata.",
        schema={"properties": {
            "nb_id": {"type": "string"},
            "title": {"type": "string", "default": ""},
            "description": {"type": "string", "default": ""},
        }},
    )
    async def cap_notebook_update(nb_id: str, title: str = "", description: str = "", trace_id=None):
        nb = await DB.load_notebook(nb_id)
        if not nb:
            return {"error": "Notebook not found"}
        if title: nb["title"] = title
        if description: nb["description"] = description
        nb["updated_at"] = time.time()
        await DB.save_notebook(nb)
        return nb

    @capability(
        "research.notebook.delete",
        http_method="DELETE", http_path="/research/notebook/delete",
        http_tags=["research", "notebook"], memory="off",
        description="Delete a notebook and all its cells.",
        schema={"properties": {"nb_id": {"type": "string"}}},
    )
    async def cap_notebook_delete(nb_id: str, trace_id=None):
        await DB.delete_notebook(nb_id)
        return {"ok": True}

    @capability(
        "research.notebook.cell.add",
        http_method="POST", http_path="/research/notebook/cell/add",
        http_tags=["research", "notebook"], memory="off",
        description="Add a cell to a notebook.",
        schema={"properties": {
            "nb_id":     {"type": "string"},
            "cell_type": {"type": "string", "default": "markdown"},
            "content":   {"type": "string", "default": ""},
            "lang":      {"type": "string", "default": "python"},
            "page_id":   {"type": "string", "default": ""},
        }},
    )
    async def cap_notebook_cell_add(nb_id: str, cell_type: str = "markdown", content: str = "",
                                    lang: str = "python", page_id: str = "", trace_id=None):
        cell = {"id": str(uuid.uuid4())[:12], "notebook_id": nb_id,
                "sort_order": 999, "cell_type": cell_type, "lang": lang,
                "tag": "none", "content": content, "generated": "",
                "thread": [], "page_id": page_id or None, "title": "",
                "citations": [], "parse_mode": "whole", "agent_mode": "single",
                "created_at": time.time(), "updated_at": time.time()}
        await DB.save_cell(cell)
        return cell

    @capability(
        "research.notebook.cell.update",
        http_method="PATCH", http_path="/research/notebook/cell/update",
        http_tags=["research", "notebook"], memory="off",
        description="Update a cell.",
        schema={"properties": {
            "nb_id":   {"type": "string"},
            "cell_id": {"type": "string"},
            "content": {"type": "string", "default": ""},
        }},
    )
    async def cap_notebook_cell_update(nb_id: str, cell_id: str, content: str = "", trace_id=None, **kw):
        cell = await DB.load_cell(cell_id)
        if not cell:
            return {"error": "Cell not found"}
        if content: cell["content"] = content
        for k in ("generated", "tag", "cell_type", "lang", "sort_order", "page_id",
                  "title", "parse_mode", "agent_mode"):
            if k in kw: cell[k] = kw[k]
        cell["updated_at"] = time.time()
        await DB.save_cell(cell)
        return cell

    @capability(
        "research.notebook.cell.delete",
        http_method="DELETE", http_path="/research/notebook/cell/delete",
        http_tags=["research", "notebook"], memory="off",
        description="Delete a cell.",
        schema={"properties": {"cell_id": {"type": "string"}}},
    )
    async def cap_notebook_cell_delete(cell_id: str, trace_id=None, **kw):
        await DB.delete_cell(cell_id)
        return {"ok": True}

    @capability(
        "research.notebook.from_job",
        http_method="POST", http_path="/research/notebook/from_job",
        http_tags=["research", "notebook"], memory="on",
        description="Create a notebook from a completed research job.",
        schema={"properties": {
            "job_id":     {"type": "string"},
            "project_id": {"type": "string", "default": ""},
        }},
    )
    async def cap_notebook_from_job(job_id: str, project_id: str = "", trace_id=None):
        # Delegate to existing handler
        return await notebook_from_job(job_id, {"project_id": project_id})

    # ── Iteration ─────────────────────────────────────────────────────────────

    @capability(
        "research.iterate.create",
        http_method="POST", http_path="/research/iterate/create",
        http_tags=["research", "iteration"], memory="on",
        description="Create a continuous iteration target.",
        schema={"properties": {
            "target_type":   {"type": "string", "default": "project"},
            "target_id":     {"type": "string"},
            "seed_query":    {"type": "string"},
            "mode":          {"type": "string", "default": "single"},
            "output_mode":   {"type": "string", "default": "report"},
            "interval_secs": {"type": "integer", "default": 300},
            "autostart":     {"type": "boolean", "default": True},
        }},
    )
    async def cap_iterate_create(target_id: str, seed_query: str,
                                 target_type: str = "project", mode: str = "single",
                                 output_mode: str = "report", interval_secs: int = 300,
                                 autostart: bool = True, trace_id=None):
        it = {"id": str(uuid.uuid4())[:16], "target_type": target_type,
              "target_id": target_id, "status": "running" if autostart else "paused",
              "mode": mode, "output_mode": output_mode,
              "interval_secs": interval_secs, "seed_query": seed_query,
              "traversal_map": {}, "created_at": time.time(), "updated_at": time.time()}
        await DB.save_iteration_target(it)
        if autostart:
            task = asyncio.create_task(_run_iteration_loop(it["id"]))
            _iter_tasks[it["id"]] = task
        return it

    @capability(
        "research.iterate.list",
        http_method="GET", http_path="/research/iterate/list",
        http_tags=["research", "iteration"], memory="off", silent=True,
        description="List iteration targets.",
        schema={"properties": {"status": {"type": "string", "default": ""}}},
    )
    async def cap_iterate_list(status: str = "", trace_id=None):
        return await DB.load_iteration_targets(status or None)

    @capability(
        "research.iterate.get",
        http_method="GET", http_path="/research/iterate/get",
        http_tags=["research", "iteration"], memory="off",
        description="Get iteration target details.",
        schema={"properties": {"it_id": {"type": "string"}}},
    )
    async def cap_iterate_get(it_id: str, trace_id=None):
        it = await DB.load_iteration_target(it_id)
        if not it:
            return {"error": "Iteration not found"}
        it["running"] = it_id in _iter_tasks
        return it

    @capability(
        "research.iterate.start",
        http_method="POST", http_path="/research/iterate/start",
        http_tags=["research", "iteration"], memory="off",
        description="Start/resume an iteration target.",
        schema={"properties": {"it_id": {"type": "string"}}},
    )
    async def cap_iterate_start(it_id: str, trace_id=None):
        it = await DB.load_iteration_target(it_id)
        if not it:
            return {"error": "Iteration not found"}
        if it_id in _iter_tasks and not _iter_tasks[it_id].done():
            return {"ok": True, "status": "already_running"}
        it["status"] = "running"; it["updated_at"] = time.time()
        await DB.save_iteration_target(it)
        _iter_stop.pop(it_id, None)
        _iter_tasks[it_id] = asyncio.create_task(_run_iteration_loop(it_id))
        return {"ok": True, "status": "running"}

    @capability(
        "research.iterate.pause",
        http_method="POST", http_path="/research/iterate/pause",
        http_tags=["research", "iteration"], memory="off",
        description="Pause an iteration target.",
        schema={"properties": {"it_id": {"type": "string"}}},
    )
    async def cap_iterate_pause(it_id: str, trace_id=None):
        it = await DB.load_iteration_target(it_id)
        if not it:
            return {"error": "Iteration not found"}
        _iter_stop[it_id] = True
        it["status"] = "paused"; it["updated_at"] = time.time()
        await DB.save_iteration_target(it)
        return {"ok": True, "status": "paused"}

    @capability(
        "research.iterate.stop",
        http_method="POST", http_path="/research/iterate/stop",
        http_tags=["research", "iteration"], memory="off",
        description="Stop and delete an iteration target.",
        schema={"properties": {"it_id": {"type": "string"}}},
    )
    async def cap_iterate_stop(it_id: str, trace_id=None):
        it = await DB.load_iteration_target(it_id)
        if not it:
            return {"error": "Iteration not found"}
        _iter_stop[it_id] = True
        task = _iter_tasks.get(it_id)
        if task and not task.done(): task.cancel()
        await DB.delete_iteration_target(it_id)
        _iter_tasks.pop(it_id, None); _iter_stop.pop(it_id, None)
        return {"ok": True, "status": "stopped"}

    @capability(
        "research.iterate.update",
        http_method="PATCH", http_path="/research/iterate/update",
        http_tags=["research", "iteration"], memory="off",
        description="Update iteration settings.",
        schema={"properties": {
            "it_id": {"type": "string"},
            "interval_secs": {"type": "integer", "default": 0},
            "seed_query": {"type": "string", "default": ""},
        }},
    )
    async def cap_iterate_update(it_id: str, interval_secs: int = 0, seed_query: str = "",
                                 mode: str = "", output_mode: str = "", trace_id=None):
        it = await DB.load_iteration_target(it_id)
        if not it:
            return {"error": "Iteration not found"}
        if interval_secs > 0: it["interval_secs"] = interval_secs
        if seed_query: it["seed_query"] = seed_query
        if mode: it["mode"] = mode
        if output_mode: it["output_mode"] = output_mode
        it["updated_at"] = time.time()
        await DB.save_iteration_target(it)
        return it

    @capability(
        "research.iterate.map",
        http_method="GET", http_path="/research/iterate/map",
        http_tags=["research", "iteration"], memory="off",
        description="Get traversal map for an iteration.",
        schema={"properties": {"it_id": {"type": "string"}}},
    )
    async def cap_iterate_map(it_id: str, trace_id=None):
        it = await DB.load_iteration_target(it_id)
        if not it:
            return {"error": "Iteration not found"}
        return it.get("traversal_map", {})

    # ── Recall (replaces research_recall_capabilities.py) ─────────────────────

    @capability(
        "research.recall.search",
        http_method="GET", http_path="/research/recall/search",
        http_tags=["research", "recall"], memory="off",
        description="Semantic search across all research datasets.",
        schema={"properties": {
            "query":      {"type": "string"},
            "dataset_id": {"type": "string", "default": ""},
            "top_k":      {"type": "integer", "default": 20},
        }},
    )
    async def cap_recall_search(query: str, dataset_id: str = "", top_k: int = 20, trace_id=None):
        return await _rf.recall_research(query, dataset_id=dataset_id or None, top_k=int(top_k))

    @capability(
        "research.recall.job",
        http_method="GET", http_path="/research/recall/job",
        http_tags=["research", "recall"], memory="off",
        description="Full job hydration from fabric.",
        schema={"properties": {"job_id": {"type": "string"}}},
    )
    async def cap_recall_job(job_id: str, trace_id=None):
        return await _rf.get_research_job(job_id)

    @capability(
        "research.recall.notebook",
        http_method="GET", http_path="/research/recall/notebook",
        http_tags=["research", "recall"], memory="off",
        description="Recall a notebook from fabric.",
        schema={"properties": {"notebook_id": {"type": "string"}}},
    )
    async def cap_recall_notebook(notebook_id: str, trace_id=None):
        out = await _rf.get_research_notebook(notebook_id)
        if not out.get("notebook"):
            nb = await DB.load_notebook(notebook_id)
            if nb:
                out["notebook"] = nb; out["cells"] = nb.get("cells", []); out["cell_count"] = len(out["cells"])
        return out

    @capability(
        "research.recall.session",
        http_method="GET", http_path="/research/recall/session",
        http_tags=["research", "recall"], memory="off",
        description="Recall a research session timeline.",
        schema={"properties": {"session_id": {"type": "string"}}},
    )
    async def cap_recall_session(session_id: str, trace_id=None):
        return await _rf.get_research_session(session_id)

    @capability(
        "research.recall.datasets",
        http_method="GET", http_path="/research/recall/datasets",
        http_tags=["research", "recall"], memory="off", silent=True,
        description="List all research datasets in the fabric.",
    )
    async def cap_recall_datasets(trace_id=None):
        return await _rf.list_research_datasets()

    # ── Health ────────────────────────────────────────────────────────────────

    @capability(
        "research.health",
        http_method="GET", http_path="/research/health",
        http_tags=["research"], memory="off", silent=True,
        description="Health check for the research subsystem.",
    )
    async def cap_health(trace_id=None):
        active = len([j for j in jobs.values()
                      if j.status not in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED)])
        return {"status": "ok", "mode": "integrated", "jobs_active": active,
                "instances": len(instances), "sources": len(globals()["sources"])}

    # ── UI Panel Registration ─────────────────────────────────────────────────

    _IFRAME_STYLE = ("flex:1;width:100%;border:none;height:100%;"
                     "background:var(--bg,#f0ede8)")

    @_VERA_APP.get("/research/panel", include_in_schema=False)
    async def _serve_research_panel():
        from fastapi.responses import HTMLResponse
        p = Path(__file__).parent / "research_panel.html"
        return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                            else "<p style='color:red'>research_panel.html not found</p>")

    @_VERA_APP.get("/notebook/panel", include_in_schema=False)
    async def _serve_notebook_panel():
        from fastapi.responses import HTMLResponse
        p = Path(__file__).parent / "notebook_panel.html"
        return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                            else "<p style='color:red'>notebook_panel.html not found</p>")

    @_VERA_APP.get("/nlp/panel", include_in_schema=False)
    async def _serve_nlp_panel():
        from fastapi.responses import HTMLResponse
        p = Path(__file__).parent / "nlp_panel.html"
        return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                            else "<p style='color:red'>nlp_panel.html not found</p>")

    _all_cap_names = [k for k in CAPABILITY_REGISTRY if k.startswith("research.")]

    register_ui(
        "research-panel", "Research", "",
        f'<div style="height:100%;display:flex;flex-direction:column;">'
        f'<iframe src="/research/panel" style="{_IFRAME_STYLE}" '
        f'allow="clipboard-read; clipboard-write"></iframe></div>',
        "", ui_caps=_all_cap_names, mode="tab", tab_order=55,
    )
    register_ui(
        "notebook-panel", "Notebook", "",
        f'<div style="height:100%;display:flex;flex-direction:column;">'
        f'<iframe src="/notebook/panel" style="{_IFRAME_STYLE}" '
        f'allow="clipboard-read; clipboard-write"></iframe></div>',
        "", ui_caps=[c for c in _all_cap_names if "notebook" in c],
        mode="tab", tab_order=56,
    )
    register_ui(
        "nlp-panel", "NLP", "",
        f'<div style="height:100%;display:flex;flex-direction:column;">'
        f'<iframe src="/nlp/panel" style="{_IFRAME_STYLE}" '
        f'allow="clipboard-read; clipboard-write"></iframe></div>',
        "", ui_caps=[], mode="injectable", tab_order=57,
    )

    log.info("researcher_api: %d research.* capabilities registered (Vera mode)",
             len(_all_cap_names))


# ── Standalone entry point (legacy — kept for backwards compat) ───────────────

if not _VERA_MODE and __name__ == "__main__":
    import uvicorn
    module_name = __spec__.name if __spec__ is not None else "researcher_api"
    reload_dir  = os.path.dirname(os.path.abspath(__file__))
    uvicorn.run(f"{module_name}:app", host="0.0.0.0", port=8765,
                reload=False, reload_dirs=[reload_dir], log_level="info")