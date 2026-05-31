"""
data_fabric_collectors.py  —  Vera Prebaked Data Collectors
=============================================================
Specialised collection capabilities for well-known data sources, with
versioning, incremental ingestion, and documentation monitoring.

Capabilities
─────────────
  collector.catalog         — list all prebaked source definitions
  collector.add_prebaked    — register a prebaked source into the fabric
  collector.discover        — AI-powered dataset discovery via browser
  collector.ingest_cve      — incremental CVE database ingestion (NVD)
  collector.ingest_arxiv    — ArXiv paper ingestion by category/query
  collector.ingest_hn       — Hacker News top/new/best stories
  collector.ingest_wiki     — Wikipedia article + revision tracking
  collector.ingest_docs     — Documentation site crawler with change detection
  collector.monitor_docs    — Check monitored doc sites for updates
  collector.version_list    — List versioned snapshots for a dataset
  collector.version_diff    — Diff two versions of a dataset record

Prebaked Sources (configurable via env)
─────────────────────────────────────────
  CVE / NVD       — NIST NVD CVE feed (incremental, resumable)
  ArXiv           — cs.AI, cs.LG, cs.CR, stat.ML, physics, bio, etc.
  Hacker News     — top / new / best / ask / show (Algolia API)
  Wikipedia       — any article with revision history
  CISA KEV        — Known Exploited Vulnerabilities catalog
  OpenStreetMap   — Nominatim geocoding / OverpassAPI
  News (RSS)      — BBC, Guardian, Reuters, AP, NYT, Al Jazeera
  Weather         — Open-Meteo (no API key) + Met Office (configurable)
  GitHub          — repo events / releases / issues (rate-limited)
  PyPI            — new package releases feed
  OpenAlex        — scholarly literature (academic papers)

Rate Limits
───────────
  All sources respect robots.txt spirit. Configurable via env:
    COLLECTOR_CVE_DELAY_S    = 6      (NVD requires ≥6s between requests)
    COLLECTOR_ARXIV_DELAY_S  = 3
    COLLECTOR_HN_DELAY_S     = 1
    COLLECTOR_DEFAULT_DELAY_S= 2
    COLLECTOR_WIKI_DELAY_S   = 1      (Wikipedia polite policy)
    COLLECTOR_DOCS_DELAY_S   = 3
    COLLECTOR_GITHUB_DELAY_S = 10     (unauthenticated: 60 req/hr)

Incremental ingestion
──────────────────────
  CVE, ArXiv, and CISA KEV track a "cursor" in SQLite so large datasets
  (250k+ CVEs) can be ingested across many sessions without re-fetching.
  Each session picks up where the last left off.

Documentation monitoring
─────────────────────────
  collector.ingest_docs crawls a documentation site, stores each page as a
  versioned record (dataset = docs:<hostname>), and tags with content hash.
  collector.monitor_docs re-crawls and stores a NEW version record when
  the hash changes, keeping the full history.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse, quote_plus

import httpx

from Vera.Orchestration.capability_orchestration import (
    capability, emit_event, now_iso,
)

log = logging.getLogger("vera.collectors")

# ── Rate-limit delays (seconds between requests) ───────────────────────────
_DELAY = {
    "cve":     float(os.getenv("COLLECTOR_CVE_DELAY_S",     "6")),
    "arxiv":   float(os.getenv("COLLECTOR_ARXIV_DELAY_S",   "3")),
    "hn":      float(os.getenv("COLLECTOR_HN_DELAY_S",      "1")),
    "wiki":    float(os.getenv("COLLECTOR_WIKI_DELAY_S",    "1")),
    "docs":    float(os.getenv("COLLECTOR_DOCS_DELAY_S",    "3")),
    "github":  float(os.getenv("COLLECTOR_GITHUB_DELAY_S", "10")),
    "default": float(os.getenv("COLLECTOR_DEFAULT_DELAY_S", "2")),
}

# ── Lazy import of fabric ingest function ─────────────────────────────────
def _fabric_ingest():
    from Vera.Orchestration.fabric.data_fabric import ingest_dataset
    return ingest_dataset

def _fabric_sqlite():
    from Vera.Orchestration.fabric.data_fabric import _sqlite_conn
    return _sqlite_conn

# ── HTTP client (shared, with user-agent) ─────────────────────────────────
_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.5",
    "Accept":     "application/json, application/xml, text/html",
}

async def _get(url: str, params: dict = None, headers: dict = None,
               delay_key: str = "default", timeout: int = 30) -> Optional[httpx.Response]:
    """Rate-limited GET with shared headers."""
    await asyncio.sleep(_DELAY.get(delay_key, _DELAY["default"]))
    h = {**_HTTP_HEADERS, **(headers or {})}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        try:
            r = await c.get(url, params=params, headers=h)
            r.raise_for_status()
            return r
        except Exception as e:
            log.warning("collector GET %s: %s", url, e)
            return None


# ── Cursor helpers (persisted per collector in fabric SQLite) ──────────────
_CURSOR_TABLE_CREATED = False

def _ensure_cursor_table():
    global _CURSOR_TABLE_CREATED
    if _CURSOR_TABLE_CREATED:
        return
    try:
        conn = _fabric_sqlite()()
        conn.execute("""CREATE TABLE IF NOT EXISTS collector_cursors
                        (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)""")
        conn.commit()
        _CURSOR_TABLE_CREATED = True
    except Exception as e:
        log.debug("cursor table: %s", e)

def _get_cursor(key: str) -> Optional[str]:
    _ensure_cursor_table()
    try:
        conn = _fabric_sqlite()()
        row = conn.execute("SELECT value FROM collector_cursors WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None

def _set_cursor(key: str, value: str):
    _ensure_cursor_table()
    try:
        conn = _fabric_sqlite()()
        conn.execute("""INSERT OR REPLACE INTO collector_cursors(key,value,updated_at)
                        VALUES(?,?,?)""", (key, value, now_iso()))
        conn.commit()
    except Exception as e:
        log.debug("set_cursor: %s", e)


# ── Tag sanitiser ──────────────────────────────────────────────────────────
def _clean_tags(raw) -> List[str]:
    """
    Robustly extract clean tag strings from heavily escaped JSON artifacts
    that can appear in RSS/API feeds (e.g. repeated JSON-encoding of strings).
    """
    if not raw:
        return []

    # If it's already a list of clean strings, use it
    if isinstance(raw, list):
        tags = []
        for t in raw:
            s = str(t).strip()
            # Un-escape until stable
            prev = None
            while prev != s:
                prev = s
                try:
                    candidate = json.loads(s)
                    if isinstance(candidate, str):
                        s = candidate.strip()
                    else:
                        break
                except Exception:
                    break
            # Keep only printable, reasonably short tags
            s = re.sub(r'[^\x20-\x7E]', '', s).strip()
            if s and len(s) < 80 and not s.startswith('"'):
                tags.append(s)
        return tags[:20]

    # String: try JSON decode first, then comma-split
    s = str(raw)
    # Repeatedly unwrap JSON string encoding
    for _ in range(10):
        stripped = s.strip()
        if (stripped.startswith('"') and stripped.endswith('"')) or \
           (stripped.startswith('[') and stripped.endswith(']')):
            try:
                decoded = json.loads(stripped)
                if isinstance(decoded, list):
                    return _clean_tags(decoded)
                if isinstance(decoded, str):
                    s = decoded
                    continue
            except Exception:
                pass
        break

    # Comma-split fallback
    parts = re.split(r'[,;|]+', s)
    result = []
    for p in parts:
        p = re.sub(r'[^\x20-\x7E]', '', p).strip().strip('"').strip("'")
        if p and len(p) < 80:
            result.append(p)
    return result[:20]


# ═══════════════════════════════════════════════════════════════════════════
# PREBAKED SOURCE CATALOG
# ═══════════════════════════════════════════════════════════════════════════

PREBAKED_SOURCES: Dict[str, dict] = {
    # ── Security ────────────────────────────────────────────────────────
    "nvd_cve_recent": {
        "label":       "NVD CVE (recent 30 days)",
        "category":    "security",
        "description": "NIST National Vulnerability Database — recently published CVEs",
        "capability":  "collector.ingest_cve",
        "params":      {"mode": "recent", "batch_size": 100},
        "tags":        ["security", "cve", "nvd", "vulnerability"],
        "rate_note":   "6s between requests (NVD policy)",
        "url":         "https://services.nvd.nist.gov/rest/json/cves/2.0",
    },
    "nvd_cve_full": {
        "label":       "NVD CVE (full database, incremental)",
        "category":    "security",
        "description": "Full NVD CVE database — ingests incrementally across sessions",
        "capability":  "collector.ingest_cve",
        "params":      {"mode": "full", "batch_size": 100},
        "tags":        ["security", "cve", "nvd", "vulnerability"],
        "rate_note":   "6s between requests — takes many sessions for full 250k+",
        "url":         "https://services.nvd.nist.gov/rest/json/cves/2.0",
    },
    "cisa_kev": {
        "label":       "CISA Known Exploited Vulnerabilities",
        "category":    "security",
        "description": "CISA KEV catalog — actively exploited CVEs",
        "capability":  None,  # uses direct API
        "url":         "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
        "source_type": "api",
        "jq_path":     "vulnerabilities",
        "tags":        ["security", "cve", "cisa", "kev", "exploited"],
        "rate_note":   "single file, update daily",
    },
    # ── Research & Academia ─────────────────────────────────────────────
    "arxiv_ai": {
        "label":       "ArXiv cs.AI + cs.LG",
        "category":    "research",
        "description": "ArXiv artificial intelligence and machine learning papers",
        "capability":  "collector.ingest_arxiv",
        "params":      {"categories": "cs.AI cs.LG", "max_results": 200},
        "tags":        ["arxiv", "research", "ai", "ml", "papers"],
        "url":         "http://export.arxiv.org/api/query",
    },
    "arxiv_security": {
        "label":       "ArXiv cs.CR (Cryptography & Security)",
        "category":    "research",
        "description": "ArXiv cryptography and security papers",
        "capability":  "collector.ingest_arxiv",
        "params":      {"categories": "cs.CR", "max_results": 100},
        "tags":        ["arxiv", "research", "security", "cryptography"],
        "url":         "http://export.arxiv.org/api/query",
    },
    "arxiv_systems": {
        "label":       "ArXiv cs.DC + cs.OS (Distributed & OS)",
        "category":    "research",
        "description": "ArXiv distributed computing and operating systems papers",
        "capability":  "collector.ingest_arxiv",
        "params":      {"categories": "cs.DC cs.OS cs.AR", "max_results": 100},
        "tags":        ["arxiv", "research", "distributed", "systems"],
        "url":         "http://export.arxiv.org/api/query",
    },
    # ── Tech News ───────────────────────────────────────────────────────
    "hacker_news_top": {
        "label":       "Hacker News — Top Stories",
        "category":    "news",
        "description": "Y Combinator Hacker News top stories via Algolia API",
        "capability":  "collector.ingest_hn",
        "params":      {"feed": "top", "limit": 60},
        "tags":        ["hackernews", "tech", "news"],
        "url":         "https://hn.algolia.com/api/v1/search",
    },
    "hacker_news_new": {
        "label":       "Hacker News — New",
        "category":    "news",
        "description": "Hacker News newest submissions",
        "capability":  "collector.ingest_hn",
        "params":      {"feed": "new", "limit": 60},
        "tags":        ["hackernews", "tech", "news", "new"],
        "url":         "https://hn.algolia.com/api/v1/search_by_date",
    },
    "hacker_news_ask": {
        "label":       "Hacker News — Ask HN",
        "category":    "news",
        "description": "Hacker News Ask HN threads",
        "capability":  "collector.ingest_hn",
        "params":      {"feed": "ask", "limit": 30},
        "tags":        ["hackernews", "tech", "ask"],
        "url":         "https://hn.algolia.com/api/v1/search",
    },
    # ── General News (RSS) ───────────────────────────────────────────────
    "bbc_tech": {
        "label":       "BBC Technology News",
        "category":    "news",
        "url":         "http://feeds.bbci.co.uk/news/technology/rss.xml",
        "source_type": "rss",
        "tags":        ["bbc", "tech", "news"],
        "rate_note":   "2s delay, update every 30min",
    },
    "guardian_tech": {
        "label":       "The Guardian — Technology",
        "category":    "news",
        "url":         "https://www.theguardian.com/technology/rss",
        "source_type": "rss",
        "tags":        ["guardian", "tech", "news"],
    },
    "reuters_tech": {
        "label":       "Reuters Technology",
        "category":    "news",
        "url":         "https://feeds.reuters.com/reuters/technologyNews",
        "source_type": "rss",
        "tags":        ["reuters", "tech", "news"],
    },
    "ars_technica": {
        "label":       "Ars Technica",
        "category":    "news",
        "url":         "https://feeds.arstechnica.com/arstechnica/index",
        "source_type": "rss",
        "tags":        ["ars", "tech", "news"],
    },
    "lobsters": {
        "label":       "Lobste.rs (tech link aggregator)",
        "category":    "news",
        "url":         "https://lobste.rs/rss",
        "source_type": "rss",
        "tags":        ["lobsters", "tech", "links"],
    },
    # ── Weather ─────────────────────────────────────────────────────────
    "open_meteo_uk": {
        "label":       "Open-Meteo — UK (London)",
        "category":    "weather",
        "description": "Free Open-Meteo weather API — no key required",
        "url":         "https://api.open-meteo.com/v1/forecast?latitude=51.5&longitude=-0.1&current_weather=true&hourly=temperature_2m,precipitation,windspeed_10m",
        "source_type": "api",
        "tags":        ["weather", "uk", "london", "open-meteo"],
        "rate_note":   "max 10k/day free, update every 15min",
    },
    # ── Software & Dev ───────────────────────────────────────────────────
    "pypi_releases": {
        "label":       "PyPI New Releases",
        "category":    "software",
        "url":         "https://pypi.org/rss/updates.xml",
        "source_type": "rss",
        "tags":        ["pypi", "python", "releases"],
    },
    "github_trending": {
        "label":       "GitHub Trending (Unofficial RSS)",
        "category":    "software",
        "url":         "https://mshibanami.github.io/GitHubTrendingRSS/daily/all.xml",
        "source_type": "rss",
        "tags":        ["github", "trending", "software"],
    },
    # ── Science ─────────────────────────────────────────────────────────
    "nasa_apod": {
        "label":       "NASA Astronomy Picture of the Day",
        "category":    "science",
        "url":         "https://www.nasa.gov/feeds/iotd-feed",
        "source_type": "rss",
        "tags":        ["nasa", "astronomy", "science"],
    },
    "nature_news": {
        "label":       "Nature News & Comment",
        "category":    "science",
        "url":         "https://www.nature.com/nature.rss",
        "source_type": "rss",
        "tags":        ["nature", "science", "research"],
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITY: CATALOG
# ═══════════════════════════════════════════════════════════════════════════

@capability(
    "collector.catalog",
    http_method="GET", http_path="/collector/catalog", http_tags=["collector"],
    memory="off", silent=True,
    description="List all prebaked data source definitions. "
                "Output: {sources: [{id, label, category, description, tags, url, rate_note}]}.",
)
async def cap_collector_catalog(trace_id=None) -> Dict:
    return {
        "sources": [
            {
                "id":          k,
                "label":       v.get("label", k),
                "category":    v.get("category", "general"),
                "description": v.get("description", ""),
                "tags":        v.get("tags", []),
                "url":         v.get("url", ""),
                "rate_note":   v.get("rate_note", ""),
                "capability":  v.get("capability"),
                "source_type": v.get("source_type", "rss"),
                "params":      v.get("params", {}),
            }
            for k, v in PREBAKED_SOURCES.items()
        ],
        "count": len(PREBAKED_SOURCES),
    }


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITY: ADD PREBAKED
# ═══════════════════════════════════════════════════════════════════════════

@capability(
    "collector.add_prebaked",
    http_method="POST", http_path="/collector/add_prebaked", http_tags=["collector"],
    memory="off",
    description="Register a prebaked source into the data fabric and optionally pull. "
                "Input: source_id (str! — from collector.catalog), "
                "dataset_id (str — override dataset name), "
                "auto_pull (bool default True). "
                "Output: {ok, source_id, dataset_id, ingested}.",
)
async def cap_collector_add_prebaked(
    source_id:  str,
    dataset_id: str  = "",
    auto_pull:  bool = True,
    trace_id=None,
) -> Dict:
    defn = PREBAKED_SOURCES.get(source_id)
    if not defn:
        return {"ok": False, "error": f"Unknown source_id: {source_id}"}

    ds_id = dataset_id or f"prebaked_{source_id}"
    ingested = 0

    # For sources with a dedicated capability, call that
    cap_name = defn.get("capability")
    if auto_pull and cap_name:
        params = {**defn.get("params", {}), "dataset_id": ds_id}
        # Dynamic dispatch
        from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY
        cap_fn = CAPABILITY_REGISTRY.get(cap_name, {}).get("func")
        if cap_fn:
            try:
                result = await cap_fn(**params)
                ingested = result.get("ingested", 0)
            except Exception as e:
                log.warning("add_prebaked %s: %s", cap_name, e)
    elif auto_pull and defn.get("source_type"):
        # Use fabric source pull
        from Vera.Orchestration.fabric.data_fabric import _pull_source, _sqlite_upsert_source
        src = {
            "id":          source_id,
            "url":         defn["url"],
            "source_type": defn.get("source_type", "rss"),
            "dataset_id":  ds_id,
            "label":       defn.get("label", source_id),
            "tags":        ",".join(defn.get("tags", [])),
            "limit":       defn.get("params", {}).get("limit", 50),
            "interval":    0,
            "enabled":     True,
            "jq_path":     defn.get("jq_path", ""),
        }
        await _sqlite_upsert_source(src)
        ingested = await _pull_source(src)

    return {
        "ok":        True,
        "source_id": source_id,
        "dataset_id": ds_id,
        "label":     defn.get("label"),
        "ingested":  ingested,
    }


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITY: CVE INGESTION (incremental, resumable)
# ═══════════════════════════════════════════════════════════════════════════

@capability(
    "collector.ingest_cve",
    http_method="POST", http_path="/collector/ingest/cve", http_tags=["collector", "security"],
    memory="off",
    description="Ingest CVE records from NIST NVD API 2.0. "
                "Supports incremental ingestion — resumes from last cursor across sessions. "
                "Input: dataset_id (str default 'cve_nvd'), "
                "mode (recent|full, default recent), "
                "batch_size (int 1-2000, default 100), "
                "max_batches (int default 5 — cap per call to avoid timeouts). "
                "Output: {ingested, total_in_batch, cursor, mode, dataset_id}.",
)
async def cap_collector_ingest_cve(
    dataset_id:  str = "cve_nvd",
    mode:        str = "recent",
    batch_size:  int = 100,
    max_batches: int = 5,
    trace_id=None,
) -> Dict:
    batch_size  = max(1, min(2000, batch_size))
    max_batches = max(1, min(50,   max_batches))
    ingest_fn   = _fabric_ingest()
    cursor_key  = f"cve_{mode}_start"

    base_url = "https://services.nvd.nist.gov/rest/json/cves/2.0"
    total_ingested = 0
    start_index    = int(_get_cursor(cursor_key) or "0") if mode == "full" else 0
    batches_done   = 0

    while batches_done < max_batches:
        params: dict = {"resultsPerPage": batch_size, "startIndex": start_index}
        if mode == "recent":
            # Last 30 days
            from datetime import timedelta
            now  = datetime.now(timezone.utc)
            params["pubStartDate"] = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000")
            params["pubEndDate"]   = now.strftime("%Y-%m-%dT%H:%M:%S.000")

        resp = await _get(base_url, params=params, delay_key="cve", timeout=60)
        if not resp:
            break

        try:
            data       = resp.json()
        except Exception:
            break

        vulns      = data.get("vulnerabilities", [])
        total_avail= data.get("totalResults", 0)

        if not vulns:
            # Full cursor exhausted
            if mode == "full":
                _set_cursor(cursor_key, "0")   # reset for next round
            break

        records = []
        for item in vulns:
            cve = item.get("cve", {})
            cve_id = cve.get("id", "")
            descs  = cve.get("descriptions", [])
            desc   = next((d["value"] for d in descs if d.get("lang") == "en"), "")
            metrics= cve.get("metrics", {})
            # Try CVSS v3.1, v3.0, v2
            score = None
            severity = ""
            for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
                m = metrics.get(key, [])
                if m:
                    d = m[0].get("cvssData", {})
                    score    = d.get("baseScore")
                    severity = d.get("baseSeverity", m[0].get("baseSeverity", ""))
                    break

            refs = [r.get("url", "") for r in cve.get("references", [])[:5]]
            tags = _clean_tags(
                [severity.lower()] if severity else []
            ) + _clean_tags(
                [w.get("type","") for w in cve.get("weaknesses", [])]
            )

            records.append({
                "id":          cve_id,
                "text":        f"{cve_id}: {desc[:500]}",
                "title":       cve_id,
                "description": desc,
                "published":   cve.get("published", ""),
                "modified":    cve.get("lastModified", ""),
                "cvss_score":  score,
                "severity":    severity,
                "references":  refs,
                "tags":        list(set(t for t in tags if t)),
                "source":      "nvd",
                "url":         f"https://nvd.nist.gov/vuln/detail/{cve_id}",
            })

        result = await ingest_fn(dataset_id, records, source="nvd_cve", tags=["cve", "nvd"])
        total_ingested += result.get("ingested", 0)
        batches_done   += 1

        start_index += len(vulns)
        if mode == "full":
            _set_cursor(cursor_key, str(start_index))
        if start_index >= total_avail or len(vulns) < batch_size:
            if mode == "full":
                _set_cursor(cursor_key, "0")
            break

    await emit_event({
        "type":     "collector.cve.done",
        "ingested": total_ingested,
        "mode":     mode,
        "dataset":  dataset_id,
    })
    return {
        "ok":        True,
        "ingested":  total_ingested,
        "mode":      mode,
        "cursor":    _get_cursor(cursor_key),
        "dataset_id": dataset_id,
    }


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITY: ARXIV INGESTION
# ═══════════════════════════════════════════════════════════════════════════

@capability(
    "collector.ingest_arxiv",
    http_method="POST", http_path="/collector/ingest/arxiv", http_tags=["collector", "research"],
    memory="off",
    description="Ingest papers from ArXiv by category and/or search query. "
                "Input: dataset_id (str default 'arxiv'), "
                "categories (str — space-sep, e.g. 'cs.AI cs.LG'), "
                "query (str — keyword search), "
                "max_results (int default 100, max 500). "
                "Output: {ingested, dataset_id}.",
)
async def cap_collector_ingest_arxiv(
    dataset_id:  str = "arxiv",
    categories:  str = "cs.AI",
    query:       str = "",
    max_results: int = 100,
    trace_id=None,
) -> Dict:
    max_results = max(1, min(500, max_results))
    ingest_fn   = _fabric_ingest()

    cats   = categories.strip().split()
    q_parts = [f"cat:{c}" for c in cats if c]
    if query:
        q_parts.append(f"all:{quote_plus(query)}")
    search_q = " AND ".join(q_parts) if q_parts else "all:artificial intelligence"

    params = {
        "search_query": search_q,
        "start":        0,
        "max_results":  max_results,
        "sortBy":       "submittedDate",
        "sortOrder":    "descending",
    }

    resp = await _get("http://export.arxiv.org/api/query", params=params,
                      delay_key="arxiv", timeout=60)
    if not resp:
        return {"ok": False, "error": "ArXiv request failed"}

    # Parse Atom XML
    NS = {"atom": "http://www.w3.org/2005/Atom",
          "arxiv": "http://arxiv.org/schemas/atom"}
    try:
        root = ET.fromstring(resp.text)
    except Exception as e:
        return {"ok": False, "error": f"XML parse: {e}"}

    records = []
    for entry in root.findall("atom:entry", NS):
        def t(tag): 
            el = entry.find(f"atom:{tag}", NS)
            return el.text.strip() if el is not None and el.text else ""
        
        arxiv_id = t("id").split("/")[-1]
        title    = re.sub(r"\s+", " ", t("title"))
        summary  = re.sub(r"\s+", " ", t("summary"))
        published= t("published")
        updated  = t("updated")
        authors  = [a.find("atom:name", NS).text for a in entry.findall("atom:author", NS)
                    if a.find("atom:name", NS) is not None]
        cats_raw = [c.get("term","") for c in entry.findall("atom:category", NS)]

        records.append({
            "id":        arxiv_id,
            "text":      f"{title}\n\n{summary[:800]}",
            "title":     title,
            "abstract":  summary,
            "authors":   authors[:8],
            "published": published,
            "updated":   updated,
            "categories":cats_raw,
            "url":       f"https://arxiv.org/abs/{arxiv_id}",
            "pdf_url":   f"https://arxiv.org/pdf/{arxiv_id}",
            "tags":      _clean_tags(cats_raw[:5]),
            "source":    "arxiv",
        })

    result = await ingest_fn(dataset_id, records, source="arxiv",
                             tags=["arxiv", "research"] + _clean_tags(cats[:3]))
    return {"ok": True, "ingested": result.get("ingested", 0), "dataset_id": dataset_id}


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITY: HACKER NEWS
# ═══════════════════════════════════════════════════════════════════════════

@capability(
    "collector.ingest_hn",
    http_method="POST", http_path="/collector/ingest/hn", http_tags=["collector", "news"],
    memory="off",
    description="Ingest Hacker News stories via Algolia API. "
                "Input: dataset_id (str default 'hacker_news'), "
                "feed (top|new|best|ask|show, default top), "
                "limit (int default 60, max 200). "
                "Output: {ingested, dataset_id}.",
)
async def cap_collector_ingest_hn(
    dataset_id: str = "hacker_news",
    feed:       str = "top",
    limit:      int = 60,
    trace_id=None,
) -> Dict:
    limit     = max(1, min(200, limit))
    ingest_fn = _fabric_ingest()

    # Algolia HN API — no rate limit issues, friendly API
    endpoint_map = {
        "top":  "https://hn.algolia.com/api/v1/search",
        "new":  "https://hn.algolia.com/api/v1/search_by_date",
        "best": "https://hn.algolia.com/api/v1/search",
        "ask":  "https://hn.algolia.com/api/v1/search",
        "show": "https://hn.algolia.com/api/v1/search",
    }
    tags_map = {
        "top":  "story",
        "new":  "story",
        "best": "story",
        "ask":  "ask_hn",
        "show": "show_hn",
    }
    url   = endpoint_map.get(feed, endpoint_map["top"])
    htag  = tags_map.get(feed, "story")
    params = {"tags": htag, "hitsPerPage": limit}

    resp = await _get(url, params=params, delay_key="hn", timeout=30)
    if not resp:
        return {"ok": False, "error": "HN API request failed"}

    try:
        data = resp.json()
    except Exception:
        return {"ok": False, "error": "HN JSON parse error"}

    hits    = data.get("hits", [])
    records = []
    for h in hits:
        hn_url   = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID','')}"
        raw_tags = h.get("_tags", [])
        # _tags is a list of strings, e.g. ["story", "author_...", "story_12345"]
        clean    = _clean_tags([t for t in raw_tags
                                if not t.startswith(("author_", "story_", "comment_"))])
        records.append({
            "id":          str(h.get("objectID", "")),
            "text":        f"{h.get('title','')}\n{h.get('story_text','')[:400]}".strip(),
            "title":       h.get("title", ""),
            "author":      h.get("author", ""),
            "points":      h.get("points", 0),
            "num_comments":h.get("num_comments", 0),
            "url":         hn_url,
            "hn_url":      f"https://news.ycombinator.com/item?id={h.get('objectID','')}",
            "created_at":  h.get("created_at", ""),
            "tags":        clean + [feed],
            "source":      "hackernews",
            "feed":        feed,
        })

    result = await ingest_fn(dataset_id, records, source="hackernews",
                             tags=["hackernews", "tech", feed])
    return {"ok": True, "ingested": result.get("ingested", 0), "dataset_id": dataset_id}


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITY: DOCUMENTATION SITE CRAWLER + VERSION TRACKING
# ═══════════════════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────────────────────────
# SITE PROFILER & ADAPTIVE COLLECTION
# Sniffs a URL on first visit and decides the best extraction strategy.
# ─────────────────────────────────────────────────────────────────────────────

async def _profile_site(url: str, html_hint: str = "") -> Dict[str, Any]:
    """Analyse a URL to figure out the best ingestion strategy.
    Returns a dict with:
      - kind: one of mediawiki, sitemap, rss_hub, openapi, json_ld, spa, plain_html
      - hints: list of follow-up sources (RSS feeds, sitemaps, APIs)
      - features: misc detected metadata
    Cheap — does at most 3 HTTP probes."""
    from urllib.parse import urlparse, urljoin
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        BeautifulSoup = None

    profile = {
        "url":      url,
        "kind":     "plain_html",
        "hints":    [],
        "features": {},
    }
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as c:
        # 1. Fetch the start page if no hint supplied
        try:
            if not html_hint:
                resp = await c.get(url)
                if resp.status_code >= 400:
                    profile["features"]["start_status"] = resp.status_code
                    return profile
                html_hint = resp.text
                profile["features"]["start_status"] = resp.status_code
        except Exception as e:
            profile["features"]["start_error"] = str(e)
            return profile

        # 2. Detect MediaWiki — look for the generator meta or wiki paths.
        is_wiki = False
        if "mediawiki" in (html_hint or "").lower()[:8000]:
            is_wiki = True
        if BeautifulSoup:
            try:
                soup_lite = BeautifulSoup(html_hint[:30000], "html.parser")
                gen = soup_lite.find("meta", attrs={"name": "generator"})
                if gen and "mediawiki" in (gen.get("content","") or "").lower():
                    is_wiki = True
                # Many wikis have a /wiki/Special:Search link
                if soup_lite.find("a", href=re.compile(r"Special:Search|api\.php")):
                    is_wiki = True
            except Exception:
                pass
        # Probe /api.php
        if not is_wiki:
            try:
                api_probe = await c.get(urljoin(base, "/api.php?action=query&meta=siteinfo&format=json"),
                                          timeout=5)
                if (api_probe.status_code == 200 and
                    "application/json" in api_probe.headers.get("content-type","")):
                    j = api_probe.json()
                    if "query" in j and "general" in j["query"]:
                        is_wiki = True
                        profile["features"]["wiki_sitename"] = j["query"]["general"].get("sitename")
            except Exception:
                pass

        if is_wiki:
            profile["kind"] = "mediawiki"
            profile["hints"].append({
                "type": "wiki_api",
                "url":  urljoin(base, "/api.php"),
                "label": "MediaWiki API endpoint",
            })

        # 3. Find linked RSS / Atom feeds (every site type can have these)
        if BeautifulSoup:
            try:
                soup_lite = soup_lite if "soup_lite" in dir() else BeautifulSoup(html_hint[:30000], "html.parser")
                for link in soup_lite.find_all("link", rel="alternate"):
                    t = (link.get("type") or "").lower()
                    if t in ("application/rss+xml", "application/atom+xml"):
                        feed_url = urljoin(url, link.get("href",""))
                        if feed_url.startswith("http"):
                            profile["hints"].append({
                                "type": "rss",
                                "url":  feed_url,
                                "label": link.get("title","") or "RSS/Atom feed",
                            })
            except Exception:
                pass

        # 4. Look for sitemap
        sitemap_urls = []
        try:
            # robots.txt → Sitemap: line
            rob = await c.get(urljoin(base, "/robots.txt"), timeout=5)
            if rob.status_code == 200:
                for line in rob.text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        sm = line.split(":", 1)[1].strip()
                        if sm.startswith("http"):
                            sitemap_urls.append(sm)
        except Exception:
            pass
        # /sitemap.xml direct
        try:
            sm_resp = await c.head(urljoin(base, "/sitemap.xml"), timeout=5)
            if sm_resp.status_code == 200:
                sitemap_urls.append(urljoin(base, "/sitemap.xml"))
        except Exception:
            pass
        for sm in sitemap_urls[:3]:
            profile["hints"].append({"type":"sitemap","url":sm,"label":"XML sitemap"})

        # 5. OpenAPI / Swagger — common paths
        for ap in ("/openapi.json","/swagger.json","/api-docs","/api/docs","/v1/openapi.json"):
            try:
                pr = await c.head(urljoin(base, ap), timeout=4)
                if pr.status_code == 200:
                    profile["hints"].append({
                        "type":"openapi","url":urljoin(base,ap),"label":"OpenAPI/Swagger spec",
                    })
                    profile["kind"] = "openapi"
                    break
            except Exception:
                pass

        # 6. JSON-LD structured data on page
        if BeautifulSoup:
            try:
                soup_lite = soup_lite if "soup_lite" in dir() else BeautifulSoup(html_hint[:30000], "html.parser")
                ld = soup_lite.find("script", type="application/ld+json")
                if ld and ld.string:
                    profile["features"]["has_json_ld"] = True
            except Exception:
                pass

        # 7. SPA detection — small body text relative to script tags
        if BeautifulSoup:
            try:
                soup_lite = soup_lite if "soup_lite" in dir() else BeautifulSoup(html_hint[:30000], "html.parser")
                body = soup_lite.body
                if body:
                    body_text = body.get_text(strip=True)
                    script_count = len(body.find_all("script"))
                    profile["features"]["body_text_len"] = len(body_text)
                    profile["features"]["script_count"] = script_count
                    if len(body_text) < 500 and script_count > 5:
                        # Likely an SPA — set kind only if not something stronger
                        if profile["kind"] == "plain_html":
                            profile["kind"] = "spa"
            except Exception:
                pass

        # If we have RSS hints and the page isn't otherwise specialised, mark as rss_hub
        rss_count = sum(1 for h in profile["hints"] if h["type"]=="rss")
        if rss_count >= 1 and profile["kind"] == "plain_html":
            profile["kind"] = "rss_hub"
        profile["features"]["rss_count"] = rss_count

    return profile


@capability(
    "collector.site_profile",
    http_method="POST", http_path="/collector/site_profile",
    http_tags=["collector","web"],
    memory="off",
    description="Profile a website to determine the best ingestion strategy. "
                "Detects MediaWiki, RSS feeds, sitemaps, OpenAPI specs, SPAs. "
                "Input: url (str!). "
                "Output: {kind, hints:[{type,url,label}], features}.",
)
async def collector_site_profile(url: str, trace_id=None) -> Dict:
    if not url.strip():
        return {"error":"url required"}
    return await _profile_site(url.strip())


# ─────────────────────────────────────────────────────────────────────────────
# MEDIAWIKI INGESTION — for sites where _profile_site says kind=mediawiki
# ─────────────────────────────────────────────────────────────────────────────

async def _ingest_mediawiki(start_url: str, dataset_id: str,
                              max_pages: int = 200, topic: str = "",
                              tags: str = "") -> Dict:
    """Use the MediaWiki API to enumerate pages and pull clean content.
    Much more reliable than HTML scraping for wikis."""
    from urllib.parse import urlparse, urljoin
    parsed = urlparse(start_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    api_url = urljoin(base, "/api.php")

    extra_tags = _clean_tags(tags)
    ingest_fn = _fabric_ingest()
    if not ingest_fn:
        return {"error": "fabric ingest unavailable"}

    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36", "Accept": "application/json,*/*"}
    pages_ingested = 0
    pages_seen = 0
    errors: list = []
    page_titles: list = []

    async def _emit(stage, **extra):
        try:
            await emit_event({"type":"collector.docs.progress",
                                "dataset_id":dataset_id,"stage":stage, **extra})
        except Exception: pass

    await _emit("starting", url=start_url, max_pages=max_pages, mode="mediawiki")

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as c:
        # Step 1: Discover pages — if topic given, search; else enumerate
        if topic:
            await _emit("page_visited", message=f"Searching MediaWiki for: {topic}")
            try:
                params = {"action":"query","list":"search","srsearch":topic,
                          "srlimit": min(50, max_pages),
                          "format":"json", "srnamespace":"0"}
                sr = await c.get(api_url, params=params)
                sr.raise_for_status()
                results = sr.json().get("query", {}).get("search", [])
                page_titles = [hit["title"] for hit in results]
                await _emit("found", count=len(page_titles))
            except Exception as e:
                errors.append(f"search: {e}")
                await _emit("warning", message=f"Search failed: {e}")
        if not page_titles:
            # Fallback: allpages enumeration
            try:
                cont_token = None
                while len(page_titles) < max_pages:
                    params = {"action":"query","list":"allpages",
                              "aplimit": min(500, max_pages - len(page_titles)),
                              "format":"json","apnamespace":"0"}
                    if cont_token:
                        params["apcontinue"] = cont_token
                    pr = await c.get(api_url, params=params)
                    pr.raise_for_status()
                    j = pr.json()
                    for p in j.get("query",{}).get("allpages",[]):
                        page_titles.append(p["title"])
                        if len(page_titles) >= max_pages: break
                    cont_token = (j.get("continue",{}) or {}).get("apcontinue")
                    if not cont_token: break
                await _emit("found", count=len(page_titles),
                              message=f"Enumerated {len(page_titles)} pages")
            except Exception as e:
                errors.append(f"allpages: {e}")
                await _emit("warning", message=f"Page enumeration failed: {e}")

        # Step 2: Pull each page via parse API (clean text, no chrome)
        topic_lc = (topic or "").strip().lower()
        topic_tokens = [t for t in re.split(r"\s+", topic_lc) if len(t) >= 3]
        for title in page_titles[:max_pages]:
            pages_seen += 1
            try:
                params = {"action":"parse","page":title,"format":"json",
                          "prop":"text|sections|categories"}
                pr = await c.get(api_url, params=params)
                if pr.status_code != 200:
                    errors.append(f"{title}: HTTP {pr.status_code}")
                    continue
                j = pr.json()
                if "error" in j:
                    errors.append(f"{title}: {j['error'].get('info','?')}")
                    continue
                page_data = j.get("parse",{}) or {}
                html = (page_data.get("text",{}) or {}).get("*","")
                if not html:
                    continue
                # Extract just text — strip HTML for clean storage
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "html.parser")
                # Remove script/style and edit-section markers
                for t in soup(["script","style","sup"]): t.decompose()
                # Remove edit-section ([edit] links)
                for span in soup.find_all("span", class_="mw-editsection"): span.decompose()
                text = " ".join(soup.get_text(separator=" ").split())[:20000]
                if not text:
                    continue
                # Topic filter — same forgiving token match as scrape path
                if topic_lc:
                    title_lc = title.lower()
                    text_lc = text.lower()
                    if topic_tokens:
                        if not any(t in text_lc or t in title_lc for t in topic_tokens):
                            continue
                    elif topic_lc not in text_lc and topic_lc not in title_lc:
                        continue
                page_url = urljoin(base, "/wiki/" + title.replace(" ","_"))
                cats = [c.get("*","") for c in page_data.get("categories",[])][:10]
                rec_id = "wiki_" + hashlib.sha1((dataset_id + ":" + title).encode()).hexdigest()[:16]
                rec = {
                    "id":         rec_id,
                    "dataset_id": dataset_id,
                    "text":       f"# {title}\n\n{text}",
                    "data": {
                        "title":       title,
                        "url":         page_url,
                        "page_id":     page_data.get("pageid"),
                        "categories":  cats,
                        "_source":     "mediawiki",
                        "_ingested_at": now_iso(),
                    },
                    "tags": (extra_tags + ["wiki", "mediawiki"]),
                    "source_id":  "wiki_" + dataset_id,
                    "created_at": now_iso(),
                }
                # Send through ingest_fn
                try:
                    if asyncio.iscoroutinefunction(ingest_fn):
                        await ingest_fn([rec])
                    else:
                        ingest_fn([rec])
                    pages_ingested += 1
                    if pages_ingested % 5 == 0 or pages_ingested == 1:
                        await _emit("page_ingested", title=title,
                                      total=pages_ingested, url=page_url)
                except Exception as e:
                    errors.append(f"ingest {title}: {e}")
            except Exception as e:
                errors.append(f"{title}: {e}")
                continue

    await _emit("done", ingested=pages_ingested, total_pages=pages_seen,
                  errors=len(errors), mode="mediawiki")
    return {
        "ok":        True,
        "ingested":  pages_ingested,
        "pages_seen":pages_seen,
        "errors":    errors[:10],
        "mode":      "mediawiki",
        "dataset_id":dataset_id,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SITEMAP-DRIVEN COLLECTION
# ─────────────────────────────────────────────────────────────────────────────

async def _enumerate_sitemap(sitemap_url: str, max_urls: int = 500) -> List[str]:
    """Read a sitemap.xml (recursing into sitemapindex if present) and return
    a flat list of <loc> URLs. Cap with max_urls."""
    from xml.etree import ElementTree as ET
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"}
    out: List[str] = []
    queue = [sitemap_url]
    visited = set()
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as c:
        while queue and len(out) < max_urls:
            sm = queue.pop(0)
            if sm in visited: continue
            visited.add(sm)
            try:
                r = await c.get(sm)
                if r.status_code != 200: continue
                # Strip namespace for parsing
                root = ET.fromstring(re.sub(r'\sxmlns="[^"]+"', "", r.text, count=1).encode())
                # Sitemap index
                for sub in root.findall(".//sitemap/loc"):
                    if sub.text: queue.append(sub.text.strip())
                # Direct URLs
                for u in root.findall(".//url/loc"):
                    if u.text and len(out) < max_urls:
                        out.append(u.text.strip())
            except Exception:
                continue
    return out




async def _auto_stitch_hook(dataset_id: str):
    """Fire fabric.loom.run on a dataset after collector ingestion completes.
    Mirrors the behaviour of the auto_stitch tag in _pull_source."""
    try:
        from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY
        loom_fn = (CAPABILITY_REGISTRY.get("fabric.loom.run") or {}).get("fn")
        if not loom_fn:
            return
        await asyncio.sleep(2)  # give ingestion time to settle to disk + chroma
        res = await loom_fn(
            dataset_a=dataset_id,
            dataset_b="",
            mode="hybrid",
            min_score=0.45,
            max_matches=200,
            persist=True,
        )
        try:
            await emit_event({"type":"fabric.loom.auto",
                                "dataset_id": dataset_id,
                                "trigger": "collector_ingest",
                                "matches": res.get("total", 0),
                                "persisted": res.get("persisted", 0)})
        except Exception: pass
    except Exception as e:
        log.warning("collector auto-stitch %s: %s", dataset_id, e)




@capability(
    "collector.url_inspect",
    http_method="POST", http_path="/collector/url_inspect",
    http_tags=["collector","web"],
    memory="off",
    description="Inspect a URL: returns the site profile, a preview screenshot "
                "(if browser cap available), and metadata. Useful before "
                "starting a crawl. "
                "Input: url (str!), screenshot (bool default True). "
                "Output: {profile, screenshot_url?, title, description, url}.",
)
async def collector_url_inspect(url: str, screenshot: bool = True,
                                  trace_id=None) -> Dict:
    if not url.strip():
        return {"error":"url required"}
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    profile = await _profile_site(url)
    out = {
        "url":       url,
        "profile":   profile,
        "title":     "",
        "description": "",
    }
    # Pull title/description from the start page
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                       headers={"User-Agent":"Vera-Fabric/1.0"}) as c:
            r = await c.get(url)
            if r.status_code == 200:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(r.text[:30000], "html.parser")
                if soup.title and soup.title.string:
                    out["title"] = soup.title.string.strip()
                desc = soup.find("meta", attrs={"name":"description"}) or \
                       soup.find("meta", attrs={"property":"og:description"})
                if desc:
                    out["description"] = (desc.get("content","") or "")[:500]
    except Exception as e:
        out["meta_error"] = str(e)

    # Optional preview screenshot
    if screenshot:
        try:
            from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY
            shot_fn = (CAPABILITY_REGISTRY.get("browser.screenshot") or {}).get("fn")
            if shot_fn:
                sres = await shot_fn(url=url, full_page=False)
                if sres and isinstance(sres, dict):
                    if sres.get("path"):
                        out["screenshot_path"] = sres["path"]
                    if sres.get("data_url"):
                        out["screenshot_data_url"] = sres["data_url"]
        except Exception as e:
            out["screenshot_error"] = str(e)

    return out

@capability(
    "collector.ingest_docs",
    http_method="POST", http_path="/collector/ingest/docs", http_tags=["collector", "docs"],
    memory="off",
    description="Crawl a documentation/wiki site BFS-style with depth and topic filtering. "
                "Stores each page as a versioned record; re-running tracks changes. "
                "Input: url (str! — start URL), dataset_id (str), "
                "max_pages (int default 50, max 2000), "
                "max_depth (int default 5 — link hops from start), "
                "same_domain (bool default True), "
                "tags (str — comma-sep), "
                "topic (str — keyword filter; pages without it are penalised), "
                "topic_dropoff (int default 2 — stop following a branch after this many "
                "consecutive pages without the topic). "
                "Output: {ingested, updated, unchanged, dataset_id, deepest_level}.",
)
async def cap_collector_ingest_docs(
    url:           str,
    dataset_id:    str  = "",
    max_pages:     int  = 50,
    max_depth:     int  = 5,
    same_domain:   bool = True,
    tags:          str  = "",
    topic:         str  = "",
    topic_dropoff: int  = 2,
    trace_id=None,
) -> Dict:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed    = urlparse(url)
    hostname  = parsed.netloc
    ds_id     = dataset_id or f"docs_{hostname.replace('.','_').replace('-','_')}"
    max_pages = max(1, min(2000, max_pages))
    extra_tags= _clean_tags(tags)
    ingest_fn = _fabric_ingest()

    # ── PROFILE THE SITE ─────────────────────────────────────────────────
    # Sniff what kind of site this is. If it's MediaWiki, the wiki API path
    # gives us much cleaner content than HTML scraping. Sitemaps let us
    # enumerate exhaustively. RSS hints become sub-sources.
    site_profile: Dict[str, Any] = {"kind": "plain_html", "hints": [], "features": {}}
    try:
        site_profile = await _profile_site(url)
    except Exception as e:
        log.warning("doc crawl: profile failed for %s: %s", url, e)
    site_kind = site_profile.get("kind", "plain_html")

    # Emit profile event so the UI can reflect what was detected
    try:
        await emit_event({"type":"collector.docs.progress",
                            "dataset_id": ds_id, "stage": "profile",
                            "site_kind": site_kind,
                            "hints": site_profile.get("hints", []),
                            "features": site_profile.get("features", {})})
    except Exception: pass

    # Register RSS feeds found in profile as sub-sources (if any)
    rss_hints = [h for h in site_profile.get("hints", []) if h.get("type")=="rss"]
    if rss_hints:
        try:
            from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY
            add_fn = (CAPABILITY_REGISTRY.get("fabric.sources.add") or {}).get("fn")
            if add_fn:
                for hint in rss_hints[:5]:
                    try:
                        sub_id = "rss_sub_" + hashlib.sha1(hint["url"].encode()).hexdigest()[:14]
                        await add_fn(
                            url=hint["url"],
                            source_type="rss",
                            label=f"{hint.get('label','RSS')} (from {hostname})",
                            dataset_id=ds_id,
                            interval=86400,
                            tags=(tags or "") + (",sub_source,rss" if tags else "sub_source,rss"),
                            id=sub_id,
                        )
                    except Exception as e:
                        log.warning("doc crawl: RSS sub-source register failed: %s", e)
        except ImportError:
            pass

    # Dispatch to specialised path
    if site_kind == "mediawiki":
        result = await _ingest_mediawiki(url, ds_id, max_pages=max_pages,
                                            topic=topic, tags=tags)
        # Register the wiki itself as a fabric source for re-pulls
        try:
            from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY
            add_fn = (CAPABILITY_REGISTRY.get("fabric.sources.add") or {}).get("fn")
            if add_fn:
                await add_fn(
                    url=url, source_type="docs",
                    label=f"Wiki: {site_profile.get('features',{}).get('wiki_sitename') or hostname}",
                    dataset_id=ds_id, interval=86400,
                    tags=tags or "wiki",
                    config=json.dumps({
                        "start_url": url, "max_pages": max_pages,
                        "topic": topic, "site_kind": "mediawiki",
                    }),
                    id="docs_" + ds_id,
                )
                result["source_id"] = "docs_" + ds_id
        except Exception:
            pass
        # Auto-stitch hook
        if result.get("ingested",0) > 0:
            asyncio.create_task(_auto_stitch_hook(ds_id))
        return result

    if site_kind == "sitemap":
        # Use the first sitemap hint to enumerate URLs, then fall through to BFS
        # but seeded with the sitemap URLs (in addition to the start URL).
        sitemap_hint = next((h for h in site_profile["hints"] if h["type"]=="sitemap"), None)
        if sitemap_hint:
            try:
                seed_urls = await _enumerate_sitemap(sitemap_hint["url"], max_urls=max_pages)
                # Inject these as the initial queue
                site_profile["features"]["sitemap_seeds"] = len(seed_urls)
                try:
                    await emit_event({"type":"collector.docs.progress",
                                        "dataset_id": ds_id, "stage": "sitemap_loaded",
                                        "count": len(seed_urls)})
                except Exception: pass
                # Continue into the BFS path with the URLs pre-seeded — the existing
                # logic will discover them as the queue.
                # Note: we do this by extending the start queue below.
                _sitemap_seeds = seed_urls
            except Exception as e:
                log.warning("sitemap enumeration failed: %s", e)
                _sitemap_seeds = []
        else:
            _sitemap_seeds = []
    else:
        _sitemap_seeds = []

    # Load seen URLs+hashes from cursor
    seen_key  = f"docs_seen_{ds_id}"
    seen_raw  = _get_cursor(seen_key) or "{}"
    try:
        seen: Dict[str, str] = json.loads(seen_raw)  # url -> content_hash
    except Exception:
        seen = {}

    # BFS crawl with depth + topic dropoff tracking
    # Each queue item is (url, depth, dry_streak)
    # dry_streak = number of consecutive pages on this branch without the topic
    queue: list = [(url, 0, 0)]
    # Sitemap-seeded URLs: queue them at depth 1
    for su in _sitemap_seeds[:max_pages * 2]:
        if su != url:
            queue.append((su, 1, 0))
    visited   = set(seen.keys())
    ingested  = 0
    updated   = 0
    unchanged = 0
    skipped_topic = 0
    deepest   = 0
    topic_lc  = (topic or "").strip().lower()
    new_seen  = dict(seen)

    try:
        from bs4 import BeautifulSoup
        _have_bs4 = True
    except ImportError:
        _have_bs4 = False

    pages_seen = 0
    async def _doc_emit(stage: str, **extra):
        try:
            await emit_event({"type": "collector.docs.progress",
                              "dataset_id": ds_id, "stage": stage, **extra})
        except Exception:
            pass
    await _doc_emit("starting", url=url, max_pages=max_pages)

    while queue and (ingested + updated) < max_pages:
        page_url, depth, dry_streak = queue.pop(0)
        if page_url in visited:
            continue
        if depth > max_depth:
            continue
        visited.add(page_url)
        deepest = max(deepest, depth)
        pages_seen += 1
        if pages_seen % 5 == 1:
            await _doc_emit("progress", visited=len(visited), queued=len(queue),
                            depth=depth, deepest=deepest,
                            ingested=ingested, updated=updated, unchanged=unchanged,
                            skipped_topic=skipped_topic,
                            current=page_url[:120])

        resp = await _get(page_url, delay_key="docs", timeout=30)
        if not resp:
            continue

        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            continue

        raw_html = resp.text

        # Extract text
        if _have_bs4:
            from bs4 import BeautifulSoup
            soup  = BeautifulSoup(raw_html, "html.parser")
            title = soup.title.string.strip() if soup.title else hostname
            # Extract LINKS first — before stripping nav (we want nav links!)
            links = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                # Skip anchors, JavaScript, mailto, tel
                if not href or href.startswith(("#","javascript:","mailto:","tel:","data:")):
                    continue
                full = urljoin(page_url, href)
                if not full.startswith(("http://", "https://")):
                    continue
                # Strip the fragment so /wiki/Page#section-1 doesn't multiply
                full = full.split("#", 1)[0]
                # Same-domain check is more permissive: accept sub.example.com
                # when same_domain=True and start was on example.com
                if same_domain:
                    href_host = urlparse(full).netloc
                    # Match if same host OR if both share the registrable domain (last 2 labels)
                    if href_host != hostname:
                        h_parts = href_host.split(".")
                        s_parts = hostname.split(".")
                        if len(h_parts) >= 2 and len(s_parts) >= 2:
                            if h_parts[-2:] != s_parts[-2:]:
                                continue
                        else:
                            continue
                links.append(full)
            # Now extract TEXT — try to find main content first; otherwise full body
            main_el = (soup.find("main") or
                       soup.find("article") or
                       soup.find(id=re.compile(r"^(content|main|mw-content-text|article)", re.I)) or
                       soup.find(class_=re.compile(r"(content|article|post|entry|page-body)", re.I)))
            text_root = main_el if main_el else soup.body or soup
            for tag in text_root(["script", "style", "noscript"]):
                tag.decompose()
            text = " ".join(text_root.get_text(separator=" ").split())[:20000]
            # If text is suspiciously thin (likely SPA or JS-rendered), retry
            # via the browser capability which executes JS before extracting.
            if len(text) < 200:
                try:
                    from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY
                    browser_fn = (CAPABILITY_REGISTRY.get("browser.content") or {}).get("fn")
                    if browser_fn:
                        bres = await browser_fn(url=page_url, include_links=True, max_chars=20000)
                        if bres and bres.get("ok"):
                            text = bres.get("text") or text
                            title = bres.get("title") or title
                            # Merge browser-extracted links with the BeautifulSoup ones
                            for bl in (bres.get("links") or []):
                                if isinstance(bl, dict) and bl.get("href"):
                                    if bl["href"] not in links and bl["href"].startswith("http"):
                                        links.append(bl["href"])
                except Exception as _be:
                    log.debug("browser fallback skipped: %s", _be)
        else:
            title = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.I | re.S)
            title = title.group(1).strip() if title else hostname
            text  = re.sub(r"<[^>]+>", " ", raw_html)
            text  = re.sub(r"\s+", " ", text).strip()[:20000]
            links = re.findall(r'href=["\']([^"\']+)["\']', raw_html)
            links = [urljoin(page_url, l) for l in links
                     if l.startswith(("http", "/")) and
                     (not same_domain or urlparse(urljoin(page_url, l)).netloc == hostname)]

        content_hash = hashlib.sha256(text.encode()).hexdigest()[:16]

        # Topic match logic: split topic into tokens; ANY token matching means
        # the page is "on topic". Falls through to dropoff only after several
        # consecutive misses.
        text_lc = text.lower()
        title_lc = (title or "").lower()
        url_lc = page_url.lower()
        if topic_lc:
            tokens = [t for t in re.split(r"\s+", topic_lc) if len(t) >= 3]
            if tokens:
                has_topic = any(t in text_lc or t in title_lc or t in url_lc
                                for t in tokens)
            else:
                has_topic = topic_lc in text_lc or topic_lc in title_lc or topic_lc in url_lc
            if has_topic:
                next_dry = 0
            else:
                next_dry = dry_streak + 1
            # If we've gone topic_dropoff pages without the topic, abandon this branch
            if next_dry > topic_dropoff:
                skipped_topic += 1
                continue
        else:
            has_topic = True
            next_dry = 0

        # Version check
        prev_hash = new_seen.get(page_url)
        if prev_hash == content_hash:
            unchanged += 1
            # Still queue links so we can traverse through unchanged pages
            seen_in_page = set()
            added = 0
            for link in links:
                if added >= 60: break
                if link in visited or link in seen_in_page: continue
                seen_in_page.add(link)
                if len(queue) > max_pages * 5: break
                queue.append((link, depth + 1, next_dry))
                added += 1
            continue

        # Store as versioned record
        version_ts = now_iso()
        record = {
            "id":           hashlib.sha256(f"{page_url}:{version_ts}".encode()).hexdigest()[:24],
            "text":         text[:8000],
            "title":        title,
            "url":          page_url,
            "hostname":     hostname,
            "content_hash": content_hash,
            "version":      version_ts,
            "previous_hash":prev_hash or "",
            "is_update":    prev_hash is not None,
            "tags":         _clean_tags(["docs", hostname.split(".")[0]] + extra_tags),
            "source":       "docs_crawler",
        }

        res = await ingest_fn(ds_id, [record], source="docs_crawler",
                              tags=["docs", hostname.split(".")[0]])
        if res.get("ingested", 0):
            if prev_hash:
                updated += 1
            else:
                ingested += 1
            new_seen[page_url] = content_hash

        # Emit a progress event per ingested page so the UI shows life
        if prev_hash is None and res.get("ingested"):
            await _doc_emit("page_ingested", url=page_url, title=title[:80],
                            total=ingested + updated)
        # Queue child links — follow more links so wiki/doc sites with hundreds
        # of links per page actually progress. Cap the queue so we don't blow up.
        # Also dedupe within this page's link list to skip duplicate hrefs.
        seen_in_page = set()
        added = 0
        for link in links:
            if added >= 60: break  # follow up to 60 unique links per page
            if link in visited or link in seen_in_page: continue
            seen_in_page.add(link)
            if len(queue) > max_pages * 5: break
            queue.append((link, depth + 1, next_dry))
            added += 1

    # Persist updated seen map
    _set_cursor(seen_key, json.dumps(new_seen))

    # Register or update a fabric source so this crawl shows in the Sources list
    # and can be re-pulled on a schedule (24h default). The source uses type=docs
    # which routes back through this same capability via _pull_docs.
    src_id = None
    try:
        from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY
        add_fn = (CAPABILITY_REGISTRY.get("fabric.sources.add") or {}).get("fn")
        if add_fn:
            src_id = "docs_" + ds_id
            try:
                add_res = await add_fn(
                    url=url,
                    source_type="docs",
                    label=f"Docs: {hostname}",
                    dataset_id=ds_id,
                    interval=86400,  # 24h default
                    tags=tags or "docs",
                    config=json.dumps({
                        "start_url":     url,
                        "max_pages":     max_pages,
                        "max_depth":     max_depth,
                        "topic":         topic,
                        "topic_dropoff": topic_dropoff,
                        "same_domain":   same_domain,
                    }),
                    auth="{}",
                    id=src_id,
                )
                if isinstance(add_res, dict):
                    src_id = add_res.get("id", src_id)
            except Exception as e:
                log.warning("doc crawl: source register failed: %s", e)
    except ImportError:
        pass

    # Auto-stitch the new records
    if ingested > 0 or updated > 0:
        asyncio.create_task(_auto_stitch_hook(ds_id))

    await _doc_emit("done", ingested=ingested, updated=updated,
                    unchanged=unchanged, total_pages=len(visited),
                    source_registered=bool(src_id))
    return {
        "ok":             True,
        "ingested":       ingested,
        "updated":        updated,
        "unchanged":      unchanged,
        "skipped_topic":  skipped_topic,
        "deepest_level":  deepest,
        "dataset_id":     ds_id,
        "pages_seen":     len(new_seen),
        "source_id":      src_id,
    }


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITY: MONITOR DOCS (detect changes)
# ═══════════════════════════════════════════════════════════════════════════

@capability(
    "collector.monitor_docs",
    http_method="POST", http_path="/collector/monitor/docs", http_tags=["collector", "docs"],
    memory="off",
    description="Re-check a previously crawled documentation site for updated pages. "
                "Stores new version records only for changed pages. "
                "Input: dataset_id (str!) — must have been ingested with collector.ingest_docs. "
                "max_pages (int default 30). "
                "Output: {checked, changed, dataset_id}.",
)
async def cap_collector_monitor_docs(
    dataset_id: str,
    max_pages:  int = 30,
    trace_id=None,
) -> Dict:
    # Re-run ingest_docs on the same dataset — cursor tracks changes
    # We need the start URL — stored in cursor
    key     = f"docs_start_{dataset_id}"
    start_url = _get_cursor(key)
    if not start_url:
        return {"ok": False, "error": "No start URL found. Run collector.ingest_docs first."}

    result = await cap_collector_ingest_docs(
        url=start_url, dataset_id=dataset_id,
        max_pages=max_pages, same_domain=True,
    )
    result["checked"] = result.pop("ingested", 0) + result.pop("unchanged", 0)
    result["changed"] = result.pop("updated", 0)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITY: VERSION LIST
# ═══════════════════════════════════════════════════════════════════════════

@capability(
    "collector.version_list",
    http_method="GET", http_path="/collector/versions", http_tags=["collector", "docs"],
    memory="off",
    description="List versioned snapshots for a URL within a dataset. "
                "Input: dataset_id (str!), url (str!), limit (int default 20). "
                "Output: {versions:[{id, version, content_hash, is_update}]}.",
)
async def cap_collector_version_list(
    dataset_id: str,
    url:        str,
    limit:      int = 20,
    trace_id=None,
) -> Dict:
    from Vera.Orchestration.fabric.data_fabric import _sqlite_conn
    loop = asyncio.get_running_loop()
    def _q():
        conn = _sqlite_conn()
        rows = conn.execute(
            """SELECT id, data, created_at FROM fabric_records
               WHERE dataset_id=? ORDER BY created_at DESC LIMIT ?""",
            (dataset_id, limit * 5)
        ).fetchall()
        versions = []
        for row in rows:
            try:
                d = json.loads(row[1] or "{}")
            except Exception:
                d = {}
            if d.get("url") == url:
                versions.append({
                    "id":           row[0],
                    "version":      d.get("version", row[2]),
                    "content_hash": d.get("content_hash", ""),
                    "is_update":    d.get("is_update", False),
                    "created_at":   row[2],
                })
            if len(versions) >= limit:
                break
        return versions

    versions = await loop.run_in_executor(None, _q)
    return {"ok": True, "dataset_id": dataset_id, "url": url, "versions": versions}


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITY: DISCOVER (AI-powered)
# ═══════════════════════════════════════════════════════════════════════════

@capability(
    "collector.discover",
    http_method="POST", http_path="/collector/discover", http_tags=["collector"],
    memory="on",
    description="Discover and ingest data sources for a topic. "
                "Streams progress events. "
                "Input: topic (str!), max_sources (int default 5), "
                "content_type (rss|api|web|scrape|recon|all default all): "
                " rss = feeds only, scrape = HTML extraction, "
                " recon = Playwright API discovery (slower, best for SPAs), "
                " all = RSS first then scrape fallback. "
                "Output: {sources_found, ingested_total, sources:[{url,label,ingested,type}]}.",
)
async def cap_collector_discover(
    topic:        str,
    max_sources:  int = 5,
    content_type: str = "all",
    trace_id=None,
) -> Dict:
    """Streamed discovery: searches for sources on the topic, then for each one
    tries RSS first (cheapest, most likely to be a real feed), then falls back
    to HTML scrape if not. Emits collector.discover.progress events along the
    way so the UI can show what's happening."""
    max_sources = max(1, min(15, max_sources))
    topic_clean = re.sub(r"[^a-zA-Z0-9 _-]", "", topic).strip()[:60]

    async def _emit(stage: str, **extra):
        try:
            await emit_event({"type": "collector.discover.progress",
                              "topic": topic_clean, "stage": stage, **extra})
        except Exception:
            pass

    await _emit("starting", message=f"Searching for '{topic_clean}'…")

    # Build query — bias towards feeds for "rss", towards APIs for "api", etc.
    if content_type == "rss":
        query = f"{topic_clean} RSS feed OR atom"
    elif content_type == "api":
        query = f"{topic_clean} JSON API endpoint"
    elif content_type == "web":
        query = f"{topic_clean} blog OR articles"
    else:
        query = f"{topic_clean} feed OR blog OR API OR dataset"

    # Use browser search if available, else fall back to a generic web search
    results = []
    try:
        from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY
        browser_search = (CAPABILITY_REGISTRY.get("browser.search") or {}).get("fn")
        if browser_search:
            sr = await browser_search(query=query, max_results=max_sources * 3)
            results = sr.get("results", []) or []
    except Exception as e:
        await _emit("warning", message=f"browser.search unavailable: {e}")

    if not results:
        await _emit("warning",
                    message="browser.search returned nothing — falling back to direct feed probe")
        # Heuristic fallback: probe a curated list of common feed/blog patterns
        # against the topic (e.g. medium.com/topic/<slug>, dev.to/t/<slug>).
        slug = re.sub(r"[^a-z0-9]+", "-", topic_clean.lower()).strip("-") or "topic"
        candidates = [
            f"https://medium.com/feed/tag/{slug}",
            f"https://dev.to/feed/tag/{slug}",
            f"https://hnrss.org/newest?q={topic_clean.replace(' ','+')}",
            f"https://www.reddit.com/r/{slug}/.rss",
            f"https://news.google.com/rss/search?q={topic_clean.replace(' ','+')}",
        ]
        results = [{"url": u, "title": u} for u in candidates]
        await _emit("found", message=f"Probing {len(results)} fallback feeds",
                    count=len(results))

    await _emit("found", message=f"Found {len(results)} candidates",
                count=len(results))

    # Import pull functions from the main fabric module
    try:
        from Vera.Orchestration.fabric.data_fabric import _pull_rss, _pull_scrape, _sqlite_insert_records_batch, now_iso
    except Exception as e:
        return {"ok": False, "error": f"data_fabric imports: {e}",
                "sources_found": 0, "ingested_total": 0, "sources": []}

    sources_done = []
    ingested_total = 0
    safe_topic = re.sub(r"[^a-z0-9_]", "_", topic_clean.lower().replace(" ","_"))[:30] or "topic"

    for idx, r in enumerate(results[:max_sources]):
        url = r.get("url") or ""
        title = r.get("title") or url
        if not url or not url.startswith(("http://","https://")):
            continue

        await _emit("probing", message=f"[{idx+1}/{max_sources}] {title[:60]}", url=url)

        ds_id = f"discovered_{safe_topic}"
        items: list = []
        chosen_type = "web"

        # Probe: HEAD/GET the URL, check if it looks like a feed
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                          headers=_HTTP_HEADERS) as c:
                probe = await c.get(url)
                ct = (probe.headers.get("content-type") or "").lower()
                head = (probe.text[:600] if probe.text else "").lower()
                looks_like_feed = (
                    "xml" in ct or "rss" in ct or "atom" in ct or
                    "<rss" in head or "<feed" in head or
                    "<?xml" in head and ("<channel" in head or "<entry" in head)
                )
        except Exception as e:
            await _emit("error", url=url, message=f"probe failed: {e}")
            sources_done.append({"url": url, "label": title, "ingested": 0,
                                 "type": "error", "error": str(e)[:120]})
            continue

        # Dispatch by content_type:
        #   rss      → only RSS pulls
        #   scrape   → only HTML scrape (forces _pull_scrape regardless of feed detection)
        #   recon    → uses Playwright to discover the underlying API
        #   web/all  → try RSS (if feed), then scrape
        if content_type == "recon":
            try:
                from Vera.Orchestration.fabric.data_fabric import _pull_recon
                src = {"url": url, "lim": 30, "limit": 30, "id": "discover_" + safe_topic,
                       "headers": "{}", "page": ""}
                items = await _pull_recon(src)
                chosen_type = "recon"
            except Exception as e:
                await _emit("warning", url=url, message=f"recon failed: {e}")
                items = []
        elif content_type == "scrape":
            try:
                src = {"url": url, "lim": 30, "limit": 30}
                items = await _pull_scrape(src)
                chosen_type = "scrape"
            except Exception as e:
                await _emit("warning", url=url, message=f"scrape failed: {e}")
                items = []
        else:
            # Try RSS first if it smells like a feed
            if looks_like_feed and content_type in ("all","rss"):
                try:
                    src = {"url": url, "lim": 30, "limit": 30}
                    items = await _pull_rss(src)
                    chosen_type = "rss"
                except Exception as e:
                    await _emit("warning", url=url, message=f"rss failed: {e}")
                    items = []
            # Fall back to scrape
            if not items and content_type in ("all","web","scrape"):
                try:
                    src = {"url": url, "lim": 20, "limit": 20}
                    items = await _pull_scrape(src)
                    chosen_type = "scrape"
                except Exception as e:
                    await _emit("warning", url=url, message=f"scrape failed: {e}")
                    items = []

        if not items:
            sources_done.append({"url": url, "label": title, "ingested": 0,
                                 "type": "empty"})
            continue

        # Build records and insert via the fabric writer queue
        records = []
        for it in items:
            text = (it.get("text") or "").strip()
            if not text:
                continue
            rid = hashlib.sha256((ds_id + text[:200]).encode()).hexdigest()[:32]
            records.append({
                "id":         rid,
                "dataset_id": ds_id,
                "text":       text[:6000],
                "data":       {**(it.get("data") or {}), "_source_url": url, "_topic": topic_clean},
                "source_id":  "discover_" + safe_topic,
                "tags":       [topic_clean, chosen_type, "discovered"],
                "created_at": now_iso(),
            })
        try:
            if records:
                await _sqlite_insert_records_batch(records)
        except Exception as e:
            await _emit("error", url=url, message=f"insert failed: {e}")

        ingested_total += len(records)
        # Register the discovered URL as a fabric source so it shows in the
        # Sources panel and can be re-pulled on a 24h schedule. Use a stable
        # ID so re-runs update rather than duplicate.
        registered_src_id = ""
        try:
            from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY as _CR
            add_fn = (_CR.get("fabric.sources.add") or {}).get("fn")
            if add_fn:
                stable_id = "disc_" + hashlib.sha1((url + "|" + ds_id).encode()).hexdigest()[:14]
                src_type_for_pull = chosen_type if chosen_type in ("rss","scrape","recon") else "scrape"
                add_res = await add_fn(
                    url=url, source_type=src_type_for_pull,
                    label=(title or url)[:80],
                    dataset_id=ds_id, interval=86400,
                    tags=topic_clean + ",discovered",
                    id=stable_id,
                )
                if isinstance(add_res, dict):
                    registered_src_id = add_res.get("id", stable_id)
        except Exception as e:
            log.warning("discover: source register failed: %s", e)

        sources_done.append({
            "url": url, "label": title, "ingested": len(records),
            "type": chosen_type, "dataset_id": ds_id,
            "source_id": registered_src_id,
        })
        await _emit("ingested", url=url, label=title, count=len(records),
                    type=chosen_type, total_so_far=ingested_total)

    # Fire auto-stitch on the discovered dataset
    if ingested_total > 0:
        ds_for_stitch = f"discovered_{safe_topic}"
        asyncio.create_task(_auto_stitch_hook(ds_for_stitch))

    await _emit("done", message=f"Discovered {len(sources_done)} sources, "
                                 f"{ingested_total} records",
                 ingested_total=ingested_total)

    return {
        "ok": True, "topic": topic_clean,
        "sources_found":  len([s for s in sources_done if s["ingested"] > 0]),
        "ingested_total": ingested_total,
        "sources":        sources_done,
        "dataset_id":     f"discovered_{safe_topic}",
    }


# ═══════════════════════════════════════════════════════════════════════════
# STEALTH MULTI-METHOD SCRAPER
# ═══════════════════════════════════════════════════════════════════════════
"""
Stealth scraper for sites with anti-bot protection (Serebii, Bulbapedia,
Cloudflare-gated wikis, etc.).  Tries a cascade of strategies from cheapest
to most expensive, records which strategy worked per domain, and persists
the winner in SQLite so future pulls skip the failing methods.

Strategy cascade (in order):
  1. direct_plain      — plain httpx, no cookies, minimal headers
  2. direct_chrome     — httpx with a realistic full Chrome UA + Accept headers
  3. direct_accept     — Chrome UA + full Accept-Language + Sec-Fetch-* headers
  4. curl_impersonate  — curl_cffi (impersonates Chrome TLS fingerprint), if installed
  5. playwright_basic  — Playwright chromium, wait for DOMContentLoaded
  6. playwright_stealth— Playwright + playwright-stealth patch (evades navigator.webdriver)

Backoff: per-domain configurable delay between requests (default 3 s).
         Doubles on 429/503, resets on success, capped at 120 s.
Domain config is stored in SQLite under key "stealth_cfg:<domain>".
"""

import random

_STEALTH_UA_POOL = [
    # Chrome 124 on Windows 11
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 124 on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Firefox 125 on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Firefox 125 on Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

_STEALTH_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,image/apng,*/*;q=0.8,"
    "application/signed-exchange;v=b3;q=0.7"
)
_STEALTH_ACCEPT_FF = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,*/*;q=0.8"
)
_STEALTH_LANG  = "en-GB,en-US;q=0.9,en;q=0.8"

_STRATEGY_TABLE_CREATED = False

def _ensure_stealth_table():
    global _STRATEGY_TABLE_CREATED
    if _STRATEGY_TABLE_CREATED:
        return
    try:
        conn = _fabric_sqlite()()
        conn.execute("""CREATE TABLE IF NOT EXISTS stealth_domain_cfg
                        (domain TEXT PRIMARY KEY,
                         strategy TEXT,
                         delay_s  REAL DEFAULT 3.0,
                         fail_count INTEGER DEFAULT 0,
                         updated_at TEXT)""")
        conn.commit()
        _STRATEGY_TABLE_CREATED = True
    except Exception as e:
        log.debug("stealth table: %s", e)

def _stealth_load(domain: str) -> dict:
    _ensure_stealth_table()
    try:
        conn = _fabric_sqlite()()
        row = conn.execute(
            "SELECT strategy,delay_s,fail_count FROM stealth_domain_cfg WHERE domain=?",
            (domain,)
        ).fetchone()
        if row:
            return {"strategy": row[0], "delay_s": float(row[1] or 3.0), "fail_count": int(row[2] or 0)}
    except Exception:
        pass
    return {"strategy": None, "delay_s": 3.0, "fail_count": 0}

def _stealth_save(domain: str, strategy: str, delay_s: float, fail_count: int = 0):
    _ensure_stealth_table()
    try:
        conn = _fabric_sqlite()()
        conn.execute("""INSERT OR REPLACE INTO stealth_domain_cfg
                        (domain,strategy,delay_s,fail_count,updated_at) VALUES(?,?,?,?,?)""",
                     (domain, strategy, delay_s, fail_count, now_iso()))
        conn.commit()
    except Exception as e:
        log.debug("stealth_save: %s", e)

def _chrome_headers(ua: str = None) -> dict:
    ua = ua or random.choice(_STEALTH_UA_POOL[:3])  # Chrome UA
    is_ff = "Firefox" in ua
    return {
        "User-Agent":                ua,
        "Accept":                    _STEALTH_ACCEPT_FF if is_ff else _STEALTH_ACCEPT,
        "Accept-Language":           _STEALTH_LANG,
        "Accept-Encoding":           "gzip, deflate, br",
        "Cache-Control":             "max-age=0",
        "Sec-Ch-Ua":                 '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"' if not is_ff else "",
        "Sec-Ch-Ua-Mobile":          "?0",
        "Sec-Ch-Ua-Platform":        '"Windows"',
        "Sec-Fetch-Dest":            "document",
        "Sec-Fetch-Mode":            "navigate",
        "Sec-Fetch-Site":            "none",
        "Sec-Fetch-User":            "?1",
        "Upgrade-Insecure-Requests": "1",
        "DNT":                       "1",
        "Connection":                "keep-alive",
    }

async def _stealth_try_direct(url: str, strategy: str, timeout: float = 30.0) -> tuple:
    """Returns (html_text, status_code) or raises."""
    ua = random.choice(_STEALTH_UA_POOL)
    if strategy == "direct_plain":
        headers = {"User-Agent": ua, "Accept": "text/html,*/*"}
    elif strategy == "direct_chrome":
        headers = _chrome_headers(random.choice(_STEALTH_UA_POOL[:3]))
    else:  # direct_accept
        headers = _chrome_headers(ua)

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
        http2=True,
    ) as c:
        r = await c.get(url)
        return r.text, r.status_code

async def _stealth_try_curl(url: str, timeout: float = 30.0) -> tuple:
    """Use curl_cffi to impersonate Chrome TLS fingerprint."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        raise RuntimeError("curl_cffi not installed")
    async with AsyncSession(impersonate="chrome124") as s:
        r = await s.get(url, timeout=timeout)
        return r.text, r.status_code

async def _stealth_try_playwright(url: str, stealth: bool = False, timeout: float = 30.0) -> tuple:
    """Use Playwright to fetch page after JS execution."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("playwright not installed")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage", "--disable-gpu",
                  "--window-size=1366,768"]
        )
        ctx = await browser.new_context(
            user_agent=random.choice(_STEALTH_UA_POOL[:3]),
            locale="en-GB",
            timezone_id="Europe/London",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={
                "Accept-Language": _STEALTH_LANG,
                "DNT": "1",
            },
        )
        if stealth:
            try:
                from playwright_stealth import stealth_async
                page = await ctx.new_page()
                await stealth_async(page)
            except ImportError:
                page = await ctx.new_page()
                # Manual stealth: remove webdriver property
                await page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    window.chrome = {runtime: {}};
                    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                    Object.defineProperty(navigator, 'languages', {get: () => ['en-GB','en-US','en']});
                """)
        else:
            page = await ctx.new_page()

        try:
            response = await page.goto(url, timeout=int(timeout * 1000),
                                        wait_until="domcontentloaded")
            # Wait a bit for JS rendering
            await page.wait_for_timeout(random.randint(1500, 3500))
            html = await page.content()
            status = response.status if response else 200
        finally:
            await browser.close()

    return html, status


_STRATEGY_ORDER = [
    "direct_plain",
    "direct_chrome",
    "direct_accept",
    "curl_impersonate",
    "playwright_basic",
    "playwright_stealth",
]


async def _stealth_fetch(url: str, domain: str = None,
                          emit_fn=None, timeout: float = 30.0) -> dict:
    """
    Fetch a URL using the stealth cascade.
    Returns {html, status, strategy, domain, url, is_blocked}.
    Persists working strategy + delay for domain.
    """
    parsed  = urlparse(url)
    domain  = domain or parsed.netloc
    cfg     = _stealth_load(domain)
    backoff = max(cfg["delay_s"], 1.0)

    async def _emit(msg, **kw):
        if emit_fn:
            try:
                await emit_fn({"type": "stealth.progress", "url": url,
                               "domain": domain, "msg": msg, **kw})
            except Exception:
                pass

    # If we have a known-good strategy, try it first
    strategies = _STRATEGY_ORDER.copy()
    if cfg["strategy"] and cfg["strategy"] in strategies:
        # Promote known-good to front
        strategies.remove(cfg["strategy"])
        strategies.insert(0, cfg["strategy"])

    await asyncio.sleep(backoff)

    for strategy in strategies:
        await _emit(f"trying strategy={strategy}")
        try:
            if strategy in ("direct_plain", "direct_chrome", "direct_accept"):
                html, status = await _stealth_try_direct(url, strategy, timeout)
            elif strategy == "curl_impersonate":
                html, status = await _stealth_try_curl(url, timeout)
            elif strategy == "playwright_basic":
                html, status = await _stealth_try_playwright(url, stealth=False, timeout=timeout + 15)
            elif strategy == "playwright_stealth":
                html, status = await _stealth_try_playwright(url, stealth=True, timeout=timeout + 20)
            else:
                continue

            # Detect soft blocks (Cloudflare challenge, etc.)
            is_blocked = (
                status in (403, 429, 503) or
                "cf-chl" in (html or "")[:3000].lower() or
                "just a moment" in (html or "")[:2000].lower() or
                "checking your browser" in (html or "")[:2000].lower() or
                "enable javascript" in (html or "")[:2000].lower() and len(html or "") < 5000
            )

            if is_blocked:
                await _emit(f"blocked status={status} → trying next strategy")
                # Backoff before next attempt
                backoff = min(backoff * 2, 120.0)
                await asyncio.sleep(min(backoff, 10.0))
                _stealth_save(domain, cfg.get("strategy") or strategy,
                              delay_s=min(backoff, 60.0),
                              fail_count=cfg["fail_count"] + 1)
                continue

            # Success — save winning strategy and reset delay
            new_delay = max(3.0, backoff * 0.8)  # slowly reduce on success
            _stealth_save(domain, strategy, delay_s=new_delay, fail_count=0)
            await _emit(f"success strategy={strategy} status={status}")
            return {
                "html":       html or "",
                "status":     status,
                "strategy":   strategy,
                "domain":     domain,
                "url":        url,
                "is_blocked": False,
            }

        except RuntimeError as e:
            await _emit(f"strategy={strategy} unavailable: {str(e)[:60]}")
            continue
        except Exception as e:
            await _emit(f"strategy={strategy} error: {str(e)[:120]}")
            backoff = min(backoff * 1.5, 60.0)
            await asyncio.sleep(min(backoff, 8.0))
            continue

    # All strategies failed
    _stealth_save(domain, None, delay_s=min(backoff, 120.0),
                  fail_count=cfg["fail_count"] + len(strategies))
    return {
        "html":       "",
        "status":     0,
        "strategy":   "failed",
        "domain":     domain,
        "url":        url,
        "is_blocked": True,
    }


@capability(
    "collector.stealth_fetch",
    http_method="POST", http_path="/collector/stealth/fetch",
    http_tags=["collector", "stealth", "web"],
    memory="off",
    description="Fetch a URL using stealth cascade: tries direct httpx, curl_cffi, "
                "and Playwright (basic + stealth) in order. Saves the working strategy "
                "per domain in SQLite so future pulls are faster. Handles Cloudflare, "
                "per-request backoff, and soft-block detection. "
                "Input: url (str!), timeout (float default 30). "
                "Output: {html, status, strategy, domain, url, is_blocked}.",
)
async def cap_collector_stealth_fetch(
    url:     str,
    timeout: float = 30.0,
    trace_id=None,
) -> Dict:
    if not url.strip():
        return {"error": "url required"}
    return await _stealth_fetch(url.strip(), timeout=timeout, emit_fn=emit_event)


@capability(
    "collector.stealth_crawl",
    http_method="POST", http_path="/collector/stealth/crawl",
    http_tags=["collector", "stealth", "web"],
    memory="off",
    description="Stealthy recursive crawler for protected sites (Serebii, Bulbapedia, etc.). "
                "Uses per-domain strategy cache (httpx → curl_cffi → Playwright). "
                "Backs off on rate-limits; per-domain delay is adaptive. "
                "Input: url (str!), dataset_id (str), max_pages (int default 50), "
                "max_depth (int default 3), delay_s (float default 3.0 — per request delay), "
                "same_domain (bool default True), topic (str — filter irrelevant pages), "
                "tags (str — comma-sep). "
                "Output: {ingested, updated, pages_seen, dataset_id, strategy_used}.",
)
async def cap_collector_stealth_crawl(
    url:         str,
    dataset_id:  str  = "",
    max_pages:   int  = 50,
    max_depth:   int  = 3,
    delay_s:     float = 3.0,
    same_domain: bool = True,
    topic:       str  = "",
    tags:        str  = "",
    trace_id=None,
) -> Dict:
    if not url.strip():
        return {"error": "url required"}

    from urllib.parse import urlparse, urljoin
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        BeautifulSoup = None

    parsed   = urlparse(url.strip())
    domain   = parsed.netloc
    hostname = domain.lstrip("www.")
    ds_id    = dataset_id or f"stealth.{re.sub(r'[^a-z0-9]','_', hostname.lower())}"
    extra_tags = _clean_tags(tags)

    ingest_fn = _fabric_ingest()
    if not ingest_fn:
        return {"error": "fabric ingest unavailable"}

    # Initialise domain delay (use saved or param)
    cfg = _stealth_load(domain)
    effective_delay = max(delay_s, cfg.get("delay_s", delay_s))
    _stealth_save(domain, cfg.get("strategy"), delay_s=effective_delay)

    seen_key    = f"stealth_seen_{ds_id}"
    seen_raw    = _get_cursor(seen_key) or "{}"
    try:    seen = json.loads(seen_raw)
    except: seen = {}

    queue    : list = [(url.strip(), 0)]
    visited  : set  = set(seen.keys())
    new_seen : dict = dict(seen)
    ingested = 0
    updated  = 0
    strategies_used: set = set()

    async def _doc_emit(stage, **kw):
        try:
            await emit_event({"type":"collector.stealth.progress",
                               "dataset_id":ds_id,"stage":stage,
                               "domain":domain, **kw})
        except Exception: pass

    await _doc_emit("starting", url=url, max_pages=max_pages)

    topic_kw = [w.lower() for w in (topic or "").split() if len(w) > 2] if topic else []

    while queue and len(visited) < max_pages:
        page_url, depth = queue.pop(0)
        if page_url in visited:
            continue
        visited.add(page_url)

        await _doc_emit("page_fetching", url=page_url, depth=depth,
                        seen=len(visited), total=max_pages)

        fetch_result = await _stealth_fetch(
            page_url, domain=domain, emit_fn=emit_event, timeout=45.0
        )
        html   = fetch_result.get("html") or ""
        status = fetch_result.get("status", 0)
        strat  = fetch_result.get("strategy", "")
        if strat and strat != "failed":
            strategies_used.add(strat)

        if fetch_result.get("is_blocked") or not html:
            await _doc_emit("blocked", url=page_url, strategy=strat)
            continue

        # Extract text
        title = ""
        text  = ""
        if BeautifulSoup:
            try:
                soup = BeautifulSoup(html, "html.parser")
                # Remove nav/footer/script/style noise
                for tag in soup(["script","style","nav","footer","header",
                                  "aside","noscript","iframe"]):
                    tag.decompose()
                title = soup.title.get_text(strip=True) if soup.title else ""
                text  = soup.get_text(separator="\n", strip=True)
                # Collapse excess whitespace
                text  = re.sub(r'\n{3,}', '\n\n', text)[:16000]
            except Exception as e:
                text = re.sub(r'<[^>]+>', ' ', html)[:8000]
        else:
            text = re.sub(r'<[^>]+>', ' ', html)[:8000]

        if not text.strip():
            continue

        # Topic filter
        if topic_kw:
            text_lower = text.lower()
            hits = sum(1 for kw in topic_kw if kw in text_lower)
            if hits < max(1, len(topic_kw) // 3):
                await _doc_emit("skip_topic", url=page_url)
                continue

        content_hash = hashlib.sha256(text[:4000].encode()).hexdigest()[:16]
        old_hash     = new_seen.get(page_url)
        is_new       = old_hash is None
        is_updated   = not is_new and old_hash != content_hash
        new_seen[page_url] = content_hash

        if is_new or is_updated:
            record = {
                "id":           hashlib.sha256((ds_id + page_url).encode()).hexdigest()[:32],
                "text":         (title + "\n\n" + text)[:8000],
                "title":        title,
                "url":          page_url,
                "domain":       domain,
                "depth":        depth,
                "content_hash": content_hash,
                "is_update":    is_updated,
                "version":      now_iso(),
                "strategy":     strat,
                "tags":         extra_tags + ["stealth_crawl", hostname.replace(".","_")],
                "source":       "stealth_crawl",
            }
            try:
                res = await ingest_fn(ds_id, [record], source="stealth_crawl",
                                       tags=["stealth", "crawl", hostname])
                if is_new:
                    ingested += 1
                else:
                    updated  += 1
            except Exception as e:
                await _doc_emit("ingest_error", url=page_url, error=str(e)[:120])

        # Enqueue child links
        if depth < max_depth and BeautifulSoup:
            try:
                soup = BeautifulSoup(html, "html.parser")
                added = 0
                for a in soup.find_all("a", href=True):
                    if added >= 50:
                        break
                    href = urljoin(page_url, a["href"].split("#")[0])
                    if not href.startswith(("http://","https://")):
                        continue
                    if same_domain and urlparse(href).netloc != domain:
                        continue
                    if href not in visited and (href, depth+1) not in queue:
                        queue.append((href, depth + 1))
                        added += 1
            except Exception:
                pass

        # Adaptive delay between pages — use saved domain config
        cfg = _stealth_load(domain)
        page_delay = max(cfg.get("delay_s", delay_s), delay_s)
        # Add small jitter so timing looks human
        jitter = random.uniform(0.5, page_delay * 0.4)
        await asyncio.sleep(page_delay + jitter)

    # Persist seen map
    _set_cursor(seen_key, json.dumps(new_seen))

    # Register as a source for future scheduled re-crawls
    try:
        from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY as _CR
        add_fn = (_CR.get("fabric.sources.add") or {}).get("fn")
        if add_fn:
            await add_fn(
                url=url, source_type="stealth",
                label=f"Stealth: {hostname}",
                dataset_id=ds_id, interval=86400,
                tags=tags or "stealth,crawl",
                config=json.dumps({"max_pages": max_pages, "max_depth": max_depth,
                                    "delay_s": delay_s, "topic": topic}),
                id=f"stealth_{re.sub(r'[^a-z0-9]','_',hostname.lower())[:20]}",
            )
    except Exception as e:
        log.debug("stealth_crawl: source register: %s", e)

    if ingested + updated > 0:
        asyncio.create_task(_auto_stitch_hook(ds_id))

    await _doc_emit("done", ingested=ingested, updated=updated,
                    pages_seen=len(visited),
                    strategies=list(strategies_used))
    return {
        "ok":           True,
        "ingested":     ingested,
        "updated":      updated,
        "pages_seen":   len(visited),
        "dataset_id":   ds_id,
        "domain":       domain,
        "strategy_used": list(strategies_used),
    }


@capability(
    "collector.stealth_domain_config",
    http_method="POST", http_path="/collector/stealth/domain_config",
    http_tags=["collector", "stealth"],
    memory="off",
    description="Get or set the stealth scraper config for a domain. "
                "Input: domain (str!), strategy (str optional — force a strategy), "
                "delay_s (float optional — override per-request delay), "
                "reset (bool — clear saved strategy and revert to auto-cascade). "
                "Output: {domain, strategy, delay_s, fail_count, updated_at}.",
)
async def cap_collector_stealth_domain_config(
    domain:   str,
    strategy: str   = "",
    delay_s:  float = 0.0,
    reset:    bool  = False,
    trace_id=None,
) -> Dict:
    if not domain.strip():
        return {"error": "domain required"}
    domain = domain.strip().lstrip("https://").lstrip("http://").split("/")[0]

    if reset:
        _stealth_save(domain, None, delay_s=3.0, fail_count=0)
        return {"ok": True, "domain": domain, "reset": True}

    cfg = _stealth_load(domain)
    if strategy or delay_s:
        new_strat  = strategy if strategy in _STRATEGY_ORDER else cfg.get("strategy")
        new_delay  = delay_s if delay_s > 0 else cfg.get("delay_s", 3.0)
        _stealth_save(domain, new_strat, delay_s=new_delay, fail_count=cfg.get("fail_count", 0))
        cfg = _stealth_load(domain)

    return {
        "domain":     domain,
        "strategy":   cfg.get("strategy"),
        "delay_s":    cfg.get("delay_s", 3.0),
        "fail_count": cfg.get("fail_count", 0),
        "strategies_available": _STRATEGY_ORDER,
    }


@capability(
    "collector.stealth_list_domains",
    http_method="GET", http_path="/collector/stealth/domains",
    http_tags=["collector", "stealth"],
    memory="off",
    description="List all domains with saved stealth scraper configurations. "
                "Output: {domains: [{domain, strategy, delay_s, fail_count, updated_at}]}.",
)
async def cap_collector_stealth_list_domains(trace_id=None) -> Dict:
    _ensure_stealth_table()
    loop = asyncio.get_running_loop()
    def _q():
        conn = _fabric_sqlite()()
        rows = conn.execute(
            "SELECT domain,strategy,delay_s,fail_count,updated_at FROM stealth_domain_cfg ORDER BY updated_at DESC"
        ).fetchall()
        return [{"domain": r[0], "strategy": r[1], "delay_s": r[2],
                 "fail_count": r[3], "updated_at": r[4]} for r in rows]
    domains = await loop.run_in_executor(None, _q)
    return {"domains": domains, "count": len(domains)}


# ═══════════════════════════════════════════════════════════════════════════
# IOT / SERIAL / ESP32 SOURCES  +  TIME-SERIES DATASET CLASS
# ═══════════════════════════════════════════════════════════════════════════
"""
Adds IoT/sensor data as first-class fabric sources:
  collector.iot.serial_read  — read from USB/serial port (ESP32, Arduino, etc.)
  collector.iot.mqtt_sub     — subscribe to MQTT topic and ingest payloads
  collector.iot.websocket    — consume a WebSocket endpoint for sensor streams
  collector.iot.http_poll    — poll an HTTP sensor endpoint at interval
  collector.timeseries.ingest— low-level: ingest a list of {ts, value, ...} rows
  collector.timeseries.query — query a time-series dataset by time range + field

Time-series datasets use dataset_id prefix "ts." and store records with:
  {ts: ISO8601, fields: {field: value, ...}, tags: [...], source: str}
These are recognised by the fabric UI and charted rather than graph-expanded.
"""

_HAS_SERIAL   = None
_HAS_AIOMQTT  = None
_HAS_AIOHTTP  = None

def _check_serial():
    global _HAS_SERIAL
    if _HAS_SERIAL is None:
        try:
            import serial
            _HAS_SERIAL = True
        except ImportError:
            _HAS_SERIAL = False
    return _HAS_SERIAL

def _check_aiomqtt():
    global _HAS_AIOMQTT
    if _HAS_AIOMQTT is None:
        try:
            import aiomqtt
            _HAS_AIOMQTT = True
        except ImportError:
            _HAS_AIOMQTT = False
    return _HAS_AIOMQTT


async def _ts_ingest(dataset_id: str, rows: list, source: str = "iot",
                      tags: list = None, merge_threshold_s: float = 0.0) -> dict:
    """
    Ingest time-series rows into fabric.
    Each row should be {ts: ISO or epoch, value: float, field: str, ...}.
    Normalises timestamp to ISO8601 UTC. Deduplicates by (dataset_id, ts, field).
    """
    ingest_fn = _fabric_ingest()
    if not ingest_fn:
        return {"ok": False, "error": "fabric ingest unavailable"}
    records = []
    for row in rows:
        # Normalise timestamp
        ts_raw = row.get("ts") or row.get("timestamp") or row.get("time") or ""
        if not ts_raw:
            ts_raw = now_iso()
        elif isinstance(ts_raw, (int, float)):
            from datetime import datetime, timezone
            ts_raw = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc).isoformat().replace("+00:00","Z")

        field  = row.get("field") or row.get("name") or "value"
        value  = row.get("value")
        extra  = {k: v for k, v in row.items() if k not in ("ts","timestamp","time","field","name","value")}

        rec_id = hashlib.sha256(f"{dataset_id}:{ts_raw}:{field}".encode()).hexdigest()[:32]
        text   = f"{field}={value} at {ts_raw}"
        records.append({
            "id":          rec_id,
            "text":        text,
            "ts":          ts_raw,
            "field":       field,
            "value":       value,
            "source":      source,
            "tags":        (tags or []) + ["timeseries", source],
            "_ts_dataset": True,  # marker for UI
            **extra,
        })
    if not records:
        return {"ok": True, "ingested": 0}
    res = await ingest_fn(dataset_id, records, source=source, tags=(tags or []) + ["timeseries"])
    return {"ok": True, "ingested": res.get("ingested", len(records)), "dataset_id": dataset_id}


@capability(
    "collector.timeseries.ingest",
    http_method="POST", http_path="/collector/ts/ingest",
    http_tags=["collector", "iot", "timeseries"],
    memory="off",
    description="Ingest time-series rows into a fabric dataset (no graph, charted in UI). "
                "Input: dataset_id (str!), rows (list of {ts,field,value,...}), "
                "source (str default 'manual'), tags (str). "
                "Output: {ingested, dataset_id}.",
)
async def cap_ts_ingest(
    dataset_id: str,
    rows=None,
    source: str = "manual",
    tags:   str = "",
    trace_id=None,
) -> Dict:
    if not dataset_id:
        return {"error": "dataset_id required"}
    if isinstance(rows, str):
        try: rows = json.loads(rows)
        except: return {"error": "rows must be a JSON list"}
    rows = rows or []
    tag_list = _clean_tags(tags)
    # Ensure dataset_id is prefixed ts.
    if not dataset_id.startswith("ts."):
        dataset_id = "ts." + dataset_id
    return await _ts_ingest(dataset_id, rows, source=source, tags=tag_list)


@capability(
    "collector.timeseries.query",
    http_method="POST", http_path="/collector/ts/query",
    http_tags=["collector", "iot", "timeseries"],
    memory="off",
    description="Query a time-series dataset by time range and/or field name. "
                "Input: dataset_id (str!), field (str optional), "
                "from_ts (str ISO optional), to_ts (str ISO optional), "
                "limit (int default 500). "
                "Output: {rows: [{ts,field,value,...}], count, dataset_id}.",
)
async def cap_ts_query(
    dataset_id: str,
    field:      str = "",
    from_ts:    str = "",
    to_ts:      str = "",
    limit:      int = 500,
    trace_id=None,
) -> Dict:
    if not dataset_id:
        return {"error": "dataset_id required"}
    if not dataset_id.startswith("ts."):
        dataset_id = "ts." + dataset_id
    loop = asyncio.get_running_loop()
    def _q():
        from Vera.Orchestration.fabric.data_fabric import _sqlite_conn
        conn = _sqlite_conn()
        sql  = """SELECT data, created_at FROM fabric_records WHERE dataset_id=?"""
        params: list = [dataset_id]
        rows_raw = conn.execute(sql + " ORDER BY created_at DESC LIMIT ?",
                                 params + [limit * 5]).fetchall()
        rows_out = []
        for r in rows_raw:
            try:
                d = json.loads(r[0] or "{}")
            except:
                d = {}
            ts  = d.get("ts") or r[1] or ""
            fld = d.get("field") or ""
            if field and fld != field:
                continue
            if from_ts and ts < from_ts:
                continue
            if to_ts   and ts > to_ts:
                continue
            rows_out.append({
                "ts":    ts,
                "field": fld,
                "value": d.get("value"),
                "source":d.get("source",""),
                **{k: v for k, v in d.items() if k not in ("ts","field","value","source","text","tags","id")},
            })
            if len(rows_out) >= limit:
                break
        return rows_out
    rows_out = await loop.run_in_executor(None, _q)
    rows_out.sort(key=lambda r: r.get("ts",""))
    return {"ok": True, "dataset_id": dataset_id, "rows": rows_out, "count": len(rows_out)}


@capability(
    "collector.iot.serial_read",
    http_method="POST", http_path="/collector/iot/serial",
    http_tags=["collector", "iot", "serial"],
    memory="off",
    description="Read data from a USB/serial device (ESP32, Arduino, sensors). "
                "Reads lines for `duration_s` seconds and parses each line as "
                "JSON or 'field=value' or CSV (col headers auto-detected). "
                "Input: port (str e.g. '/dev/ttyUSB0' or 'COM3'), "
                "baud (int default 115200), "
                "duration_s (float default 10.0 — how long to read), "
                "dataset_id (str — defaults to ts.serial.<port_slug>), "
                "tags (str). "
                "Output: {ingested, rows_parsed, dataset_id, port}.",
)
async def cap_iot_serial_read(
    port:        str,
    baud:        int   = 115200,
    duration_s:  float = 10.0,
    dataset_id:  str   = "",
    tags:        str   = "",
    trace_id=None,
) -> Dict:
    if not _check_serial():
        return {"error": "pyserial not installed. Run: pip install pyserial"}
    if not port:
        return {"error": "port required (e.g. /dev/ttyUSB0 or COM3)"}

    import serial as _serial
    port_slug  = re.sub(r"[^a-z0-9]", "_", port.lower()).strip("_") or "serial"
    ds_id      = dataset_id or f"ts.serial.{port_slug}"
    if not ds_id.startswith("ts."):
        ds_id = "ts." + ds_id
    tag_list   = _clean_tags(tags) + ["serial", "iot", port_slug]
    rows       = []
    rows_parsed = 0
    csv_headers: list = []

    await emit_event({"type": "collector.iot.serial.start",
                       "port": port, "baud": baud, "duration_s": duration_s})

    def _read_sync():
        nonlocal rows, rows_parsed, csv_headers
        deadline = time.time() + duration_s
        try:
            ser = _serial.Serial(port, baudrate=baud, timeout=1.0)
        except Exception as e:
            return {"error": f"Cannot open {port}: {e}"}
        ts_now = now_iso()
        while time.time() < deadline:
            try:
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                ts_now = now_iso()
                # Try JSON first
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if isinstance(v, (int, float)):
                                rows.append({"ts": ts_now, "field": k, "value": float(v)})
                        rows_parsed += 1
                        continue
                except Exception:
                    pass
                # Try key=value
                if "=" in line and "," not in line:
                    parts = line.split("=", 1)
                    try:
                        rows.append({"ts": ts_now, "field": parts[0].strip(),
                                     "value": float(parts[1].strip())})
                        rows_parsed += 1
                        continue
                    except ValueError:
                        pass
                # Try CSV — first line may be headers
                parts = line.split(",")
                if not csv_headers and all(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', p.strip()) for p in parts):
                    csv_headers = [p.strip() for p in parts]
                    continue
                if csv_headers and len(parts) == len(csv_headers):
                    try:
                        for h, v in zip(csv_headers, parts):
                            rows.append({"ts": ts_now, "field": h, "value": float(v.strip())})
                        rows_parsed += 1
                        continue
                    except ValueError:
                        pass
                # Raw text fallback
                rows.append({"ts": ts_now, "field": "raw", "value": None, "text": line})
                rows_parsed += 1
            except Exception:
                pass
        ser.close()
        return None

    loop = asyncio.get_running_loop()
    err  = await loop.run_in_executor(None, _read_sync)
    if err:
        return err

    if rows:
        res = await _ts_ingest(ds_id, rows, source="serial", tags=tag_list)
        ingested = res.get("ingested", 0)
    else:
        ingested = 0

    await emit_event({"type": "collector.iot.serial.done", "port": port,
                       "rows_parsed": rows_parsed, "ingested": ingested})
    return {
        "ok":          True,
        "port":        port,
        "baud":        baud,
        "rows_parsed": rows_parsed,
        "ingested":    ingested,
        "dataset_id":  ds_id,
    }


@capability(
    "collector.iot.mqtt_sub",
    http_method="POST", http_path="/collector/iot/mqtt",
    http_tags=["collector", "iot", "mqtt"],
    memory="off",
    description="Subscribe to an MQTT topic and ingest payloads as time-series records. "
                "Requires aiomqtt: pip install aiomqtt. "
                "Input: host (str!), port (int default 1883), topic (str default '#'), "
                "duration_s (float default 30.0), dataset_id (str), "
                "username (str), password (str), tags (str). "
                "Output: {ingested, messages, dataset_id}.",
)
async def cap_iot_mqtt_sub(
    host:        str,
    port:        int   = 1883,
    topic:       str   = "#",
    duration_s:  float = 30.0,
    dataset_id:  str   = "",
    username:    str   = "",
    password:    str   = "",
    tags:        str   = "",
    trace_id=None,
) -> Dict:
    if not _check_aiomqtt():
        return {"error": "aiomqtt not installed. Run: pip install aiomqtt"}
    import aiomqtt
    host_slug = re.sub(r"[^a-z0-9]","_", host.lower())[:20]
    ds_id     = dataset_id or f"ts.mqtt.{host_slug}"
    if not ds_id.startswith("ts."):
        ds_id = "ts." + ds_id
    tag_list  = _clean_tags(tags) + ["mqtt","iot", host_slug]
    rows      = []
    messages  = 0
    kwargs    = {}
    if username: kwargs["username"] = username
    if password: kwargs["password"] = password

    try:
        async with aiomqtt.Client(host, port=port, **kwargs) as client:
            await client.subscribe(topic)
            deadline = asyncio.get_event_loop().time() + duration_s
            async for msg in client.messages:
                if asyncio.get_event_loop().time() > deadline:
                    break
                payload = msg.payload.decode("utf-8", errors="replace")
                ts_now  = now_iso()
                field   = str(msg.topic).replace("/",".")
                try:
                    obj = json.loads(payload)
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if isinstance(v, (int, float)):
                                rows.append({"ts": ts_now, "field": f"{field}.{k}", "value": float(v)})
                    elif isinstance(obj, (int, float)):
                        rows.append({"ts": ts_now, "field": field, "value": float(obj)})
                except Exception:
                    try:
                        rows.append({"ts": ts_now, "field": field, "value": float(payload.strip())})
                    except Exception:
                        rows.append({"ts": ts_now, "field": field, "value": None, "text": payload[:200]})
                messages += 1
    except Exception as e:
        return {"ok": False, "error": str(e), "dataset_id": ds_id}

    ingested = 0
    if rows:
        res = await _ts_ingest(ds_id, rows, source="mqtt", tags=tag_list)
        ingested = res.get("ingested", 0)

    return {"ok": True, "host": host, "topic": topic,
            "messages": messages, "ingested": ingested, "dataset_id": ds_id}


@capability(
    "collector.iot.http_poll",
    http_method="POST", http_path="/collector/iot/http_poll",
    http_tags=["collector", "iot"],
    memory="off",
    description="Poll an HTTP sensor/IoT endpoint repeatedly and ingest readings. "
                "Parses JSON responses; supports nested field extraction via dot path. "
                "Input: url (str!), interval_s (float default 5.0), "
                "count (int default 10 — number of polls), "
                "field_path (str e.g. 'data.temperature'), "
                "field_name (str default 'value'), "
                "dataset_id (str), tags (str). "
                "Output: {ingested, polls, dataset_id}.",
)
async def cap_iot_http_poll(
    url:        str,
    interval_s: float = 5.0,
    count:      int   = 10,
    field_path: str   = "",
    field_name: str   = "value",
    dataset_id: str   = "",
    tags:       str   = "",
    trace_id=None,
) -> Dict:
    if not url:
        return {"error": "url required"}
    parsed    = urlparse(url)
    url_slug  = re.sub(r"[^a-z0-9]","_", parsed.netloc.lower())[:20]
    ds_id     = dataset_id or f"ts.http.{url_slug}"
    if not ds_id.startswith("ts."):
        ds_id = "ts." + ds_id
    tag_list  = _clean_tags(tags) + ["http_poll","iot"]
    rows: list = []
    polls = 0

    def _extract(obj, path: str):
        """Dot-path extraction, e.g. 'data.sensors.0.temp'."""
        parts = path.split(".")
        cur = obj
        for p in parts:
            if isinstance(cur, dict):
                cur = cur.get(p)
            elif isinstance(cur, list):
                try: cur = cur[int(p)]
                except: return None
            else:
                return None
        return cur

    for i in range(max(1, min(count, 1000))):
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                                          headers=_chrome_headers()) as c:
                r = await c.get(url)
                ts_now = now_iso()
                try:
                    obj = r.json()
                except Exception:
                    obj = None
                if obj is None:
                    continue
                if field_path:
                    val = _extract(obj, field_path)
                    if val is not None:
                        rows.append({"ts": ts_now, "field": field_name,
                                     "value": float(val) if isinstance(val, (int, float)) else val})
                elif isinstance(obj, dict):
                    for k, v in obj.items():
                        if isinstance(v, (int, float)):
                            rows.append({"ts": ts_now, "field": k, "value": float(v)})
                elif isinstance(obj, (int, float)):
                    rows.append({"ts": ts_now, "field": field_name, "value": float(obj)})
                polls += 1
        except Exception as e:
            await emit_event({"type": "collector.iot.http_poll.error", "error": str(e)[:120]})

        if i < count - 1:
            await asyncio.sleep(interval_s)

    ingested = 0
    if rows:
        res = await _ts_ingest(ds_id, rows, source="http_poll", tags=tag_list)
        ingested = res.get("ingested", 0)

    return {"ok": True, "url": url, "polls": polls, "ingested": ingested, "dataset_id": ds_id}


@capability(
    "collector.iot.list_ports",
    http_method="GET", http_path="/collector/iot/ports",
    http_tags=["collector", "iot", "serial"],
    memory="off",
    description="List available USB/serial ports on the system. "
                "Useful for discovering ESP32/Arduino connections. "
                "Output: {ports: [{device, description, hwid}]}.",
)
async def cap_iot_list_ports(trace_id=None) -> Dict:
    if not _check_serial():
        return {"error": "pyserial not installed", "ports": []}
    import serial.tools.list_ports as _lp
    loop = asyncio.get_running_loop()
    def _q():
        return [{"device": p.device, "description": p.description, "hwid": p.hwid}
                for p in _lp.comports()]
    ports = await loop.run_in_executor(None, _q)
    return {"ports": ports, "count": len(ports)}