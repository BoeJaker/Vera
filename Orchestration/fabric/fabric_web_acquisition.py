"""
data_fabric_web_acquisition.py — Enhanced Web Acquisition & Entity Graph
=========================================================================
Extends the data fabric with:

  1. Deep web acquisition: fetches full page content (not just links), extracts
     page structure (headings, sections, code blocks, lists), and supports
     negative word / URL exclusion filters plus multi-stage guided crawls.

  2. Second-order entity graph: extracts named entities (people, orgs, dates,
     places, technologies, code symbols) from ingested text, normalises them,
     and upserts into a separate "entity" graph layer linked back to first-order
     nodes (FabricRecords). Entities that appear across multiple records are
     automatically cross-linked.

  3. Enhanced loom extraction hooks for web pages (link structure, anchor text,
     heading hierarchy), code (definitions, imports, call sites), and free text
     (key entities, dates, relationships).

Capabilities
─────────────
  fabric.web.acquire          — Multi-stage web acquisition with content fetch
  fabric.web.continue         — Resume/continue a previous acquisition
  fabric.entity_graph.extract — Extract second-order entity graph from dataset
  fabric.entity_graph.query   — Query the entity graph (by entity, type, or dataset)
  fabric.entity_graph.merge   — Merge duplicate entities across datasets
  fabric.web.acquire_status   — Get status of running/completed acquisitions

Integration
───────────
  - Registers a source for each acquisition so re-pulls work
  - Emits progress events for real-time UI updates
  - Entity graph stored in Neo4j under :Entity label with MENTIONED_IN edges
    to :FabricRecord and CO_OCCURS / RELATES_TO edges between entities
  - Page structure stored as structured metadata on records
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

from Vera.Orchestration.capability_orchestration import (
    capability, emit_event, now_iso,
)

log = logging.getLogger("vera.web_acquisition")

# ── Rate limiting ─────────────────────────────────────────────────────────
_CRAWL_DELAY = float(os.getenv("FABRIC_CRAWL_DELAY_S", "2"))

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# ── Lazy imports ──────────────────────────────────────────────────────────
def _fabric_ingest():
    from Vera.Orchestration.fabric.data_fabric import ingest_dataset
    return ingest_dataset

def _sqlite_conn():
    from Vera.Orchestration.fabric.data_fabric import _sqlite_conn as sc
    return sc()

def _get_graph(name="fabric"):
    from Vera.Orchestration.fabric.data_fabric import GRAPHS
    return GRAPHS.get(name) or GRAPHS.get("fabric")


# ── Acquisition state store (SQLite) ─────────────────────────────────────
_ACQ_TABLE_READY = False

def _ensure_acq_table():
    global _ACQ_TABLE_READY
    if _ACQ_TABLE_READY:
        return
    try:
        conn = _sqlite_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS fabric_acquisitions (
                id           TEXT PRIMARY KEY,
                dataset_id   TEXT,
                source_url   TEXT,
                status       TEXT DEFAULT 'running',
                config       TEXT,
                state        TEXT,
                pages_fetched INTEGER DEFAULT 0,
                entities_found INTEGER DEFAULT 0,
                created_at   TEXT,
                updated_at   TEXT
            );
            CREATE TABLE IF NOT EXISTS fabric_entities (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                type         TEXT NOT NULL,
                normalised   TEXT,
                mention_count INTEGER DEFAULT 0,
                datasets     TEXT,
                first_seen   TEXT,
                last_seen    TEXT,
                props        TEXT
            );
            CREATE TABLE IF NOT EXISTS fabric_entity_mentions (
                entity_id    TEXT NOT NULL,
                record_id    TEXT NOT NULL,
                dataset_id   TEXT NOT NULL,
                created_at   TEXT,
                PRIMARY KEY (entity_id, record_id)
            );
            CREATE INDEX IF NOT EXISTS idx_fe_name ON fabric_entities(normalised);
            CREATE INDEX IF NOT EXISTS idx_fe_type ON fabric_entities(type);
            CREATE INDEX IF NOT EXISTS idx_fem_entity ON fabric_entity_mentions(entity_id);
            CREATE INDEX IF NOT EXISTS idx_fem_record ON fabric_entity_mentions(record_id);
            CREATE INDEX IF NOT EXISTS idx_fem_dataset ON fabric_entity_mentions(dataset_id);
            CREATE TABLE IF NOT EXISTS fabric_entity_relations (
                from_id      TEXT NOT NULL,
                to_id        TEXT NOT NULL,
                rel          TEXT NOT NULL,
                props        TEXT,
                dataset_id   TEXT,
                created_at   TEXT,
                PRIMARY KEY (from_id, to_id, rel)
            );
            CREATE INDEX IF NOT EXISTS idx_fer_from ON fabric_entity_relations(from_id);
            CREATE INDEX IF NOT EXISTS idx_fer_to ON fabric_entity_relations(to_id);
            CREATE INDEX IF NOT EXISTS idx_fer_ds ON fabric_entity_relations(dataset_id);
        """)
        conn.commit()
        _ACQ_TABLE_READY = True
    except Exception as e:
        log.debug("acq table: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
# NEGATIVE FILTER ENGINE
# ═══════════════════════════════════════════════════════════════════════════

class NegativeFilter:
    """Filter URLs and content based on negative words/patterns."""

    def __init__(self, negative_words: List[str] = None,
                 negative_url_patterns: List[str] = None):
        self.words = [w.strip().lower() for w in (negative_words or []) if w.strip()]
        self.url_patterns = [p.strip().lower() for p in (negative_url_patterns or []) if p.strip()]

    def url_allowed(self, url: str) -> bool:
        url_lc = url.lower()
        for pat in self.url_patterns:
            if pat in url_lc:
                return False
        # Check negative words in URL path
        path = urlparse(url).path.lower()
        for w in self.words:
            if w in path:
                return False
        return True

    def content_dominated(self, text: str) -> bool:
        """Return True if content is dominated by negative-word topics."""
        if not self.words:
            return False
        text_lc = text.lower()
        neg_hits = sum(1 for w in self.words if w in text_lc)
        return neg_hits >= len(self.words) * 0.6 and neg_hits >= 3


# ═══════════════════════════════════════════════════════════════════════════
# DATA FORMAT DETECTION (CSV / JSON / RSS / Sitemap)
# ═══════════════════════════════════════════════════════════════════════════
#
# When a crawl encounters a structured data feed instead of an HTML page,
# the polite thing to do is consume it as data — into its own dataset linked
# to the parent — rather than throwing it away. This function identifies
# common feed shapes from the URL, content-type header, and the first few
# bytes of the body.

def _detect_data_kind(url: str, content_type: str, body: str) -> Optional[str]:
    """Return one of 'csv', 'json', 'rss', 'sitemap', 'jsonl' or None."""
    if not body:
        return None
    url_lc = url.lower().rstrip("/")
    ct_lc = (content_type or "").lower()
    head = body[:2048].lstrip()

    # JSON / JSONL
    if "application/json" in ct_lc or url_lc.endswith(".json"):
        if head.startswith(("{", "[")):
            return "json"
    if url_lc.endswith(".jsonl") or url_lc.endswith(".ndjson"):
        # Each line is a JSON object — minimal sanity check
        first_line = head.split("\n", 1)[0].strip()
        if first_line.startswith("{") and first_line.rstrip().endswith("}"):
            return "jsonl"

    # CSV
    if "text/csv" in ct_lc or url_lc.endswith(".csv") or url_lc.endswith(".tsv"):
        # Quick sniff: comma-or-tab separated, multiple lines
        first_lines = head.split("\n", 5)[:5]
        if len(first_lines) >= 2:
            sep = "\t" if (url_lc.endswith(".tsv") or "\t" in first_lines[0]) else ","
            cols0 = first_lines[0].count(sep)
            cols1 = first_lines[1].count(sep) if len(first_lines) > 1 else 0
            if cols0 >= 1 and abs(cols0 - cols1) <= 1:
                return "csv"

    # RSS / Atom
    if "application/rss" in ct_lc or "application/atom" in ct_lc:
        return "rss"
    if "<rss" in head[:1024].lower() or "<feed" in head[:1024].lower():
        return "rss"

    # XML Sitemap
    if "<urlset" in head[:1024].lower() or "<sitemapindex" in head[:1024].lower():
        return "sitemap"

    return None


async def _ingest_data_payload(
    url: str, body: str, kind: str, parent_ds: str,
    hostname: str, extra_tags: List[str], ingest_fn
) -> Tuple[str, int]:
    """Ingest a detected data payload into a sub-dataset of `parent_ds`."""
    # Sub-dataset name: <parent>.data.<kind>
    sub_ds = f"{parent_ds}.data.{kind}"
    records: List[Dict] = []

    if kind == "json":
        try:
            data = json.loads(body)
        except Exception:
            data = None
        if isinstance(data, list):
            for i, item in enumerate(data[:500]):
                rec_id = hashlib.sha256(f"{url}:{i}:{kind}".encode()).hexdigest()[:24]
                records.append({
                    "id":     rec_id,
                    "text":   json.dumps(item) if isinstance(item, dict) else str(item),
                    "title":  (str(item.get("title") or item.get("name") or "")[:80]
                               if isinstance(item, dict) else f"item_{i}"),
                    "url":    url,
                    "source": "web_acquisition_data",
                    "tags":   extra_tags + [kind, "data"],
                })
        elif isinstance(data, dict):
            # Single object — ingest as one record with metadata fields
            rec_id = hashlib.sha256(f"{url}:{kind}".encode()).hexdigest()[:24]
            records.append({
                "id":     rec_id,
                "text":   json.dumps(data)[:8000],
                "title":  str(data.get("title") or data.get("name") or url)[:80],
                "url":    url,
                "source": "web_acquisition_data",
                "tags":   extra_tags + [kind, "data"],
            })

    elif kind == "jsonl":
        for i, line in enumerate(body.splitlines()[:500]):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            rec_id = hashlib.sha256(f"{url}:{i}:{kind}".encode()).hexdigest()[:24]
            records.append({
                "id":     rec_id,
                "text":   line[:8000],
                "title":  (str(item.get("title") or item.get("name") or "")[:80]
                           if isinstance(item, dict) else f"line_{i}"),
                "url":    url,
                "source": "web_acquisition_data",
                "tags":   extra_tags + [kind, "data"],
            })

    elif kind == "csv":
        try:
            import csv as _csv, io as _io
            sep = "\t" if url.lower().endswith(".tsv") else ","
            reader = _csv.DictReader(_io.StringIO(body), delimiter=sep)
            for i, row in enumerate(reader):
                if i >= 500:
                    break
                rec_id = hashlib.sha256(f"{url}:{i}:csv".encode()).hexdigest()[:24]
                # Build a readable "text" from the row
                text = " | ".join(f"{k}={v}" for k, v in row.items() if v)[:8000]
                records.append({
                    "id":     rec_id,
                    "text":   text,
                    "title":  (str(row.get("title") or row.get("name") or row.get("id") or f"row_{i}"))[:80],
                    "url":    url,
                    "source": "web_acquisition_data",
                    "tags":   extra_tags + ["csv", "data"],
                    "row":    {k: v for k, v in row.items() if v}
                })
        except Exception as e:
            log.debug("csv parse %s: %s", url, e)

    elif kind == "rss":
        # Use feedparser-style minimal regex extraction
        item_pat = re.compile(r"<(?:item|entry)[^>]*>(.*?)</(?:item|entry)>", re.I | re.S)
        title_pat = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
        link_pat = re.compile(r"<link[^>]*?(?:href=[\"']([^\"']+)[\"']|>(.*?)</link>)", re.I | re.S)
        desc_pat = re.compile(r"<(?:description|summary|content)[^>]*>(.*?)</(?:description|summary|content)>", re.I | re.S)
        for i, m in enumerate(item_pat.finditer(body)):
            if i >= 200:
                break
            inner = m.group(1)
            title_m = title_pat.search(inner)
            link_m = link_pat.search(inner)
            desc_m = desc_pat.search(inner)
            title = (title_m.group(1) if title_m else "").strip()
            link = ""
            if link_m:
                link = (link_m.group(1) or link_m.group(2) or "").strip()
            desc = re.sub(r"<[^>]+>", " ", (desc_m.group(1) if desc_m else "")).strip()[:2000]
            rec_id = hashlib.sha256(f"{url}:{i}:rss".encode()).hexdigest()[:24]
            records.append({
                "id":     rec_id,
                "text":   desc or title,
                "title":  title[:80],
                "url":    link or url,
                "source": "web_acquisition_data",
                "tags":   extra_tags + ["rss", "feed"],
            })

    elif kind == "sitemap":
        loc_pat = re.compile(r"<loc[^>]*>(.*?)</loc>", re.I | re.S)
        for i, m in enumerate(loc_pat.finditer(body)):
            if i >= 500:
                break
            loc = m.group(1).strip()
            if not loc:
                continue
            rec_id = hashlib.sha256(f"{url}:{i}:sitemap".encode()).hexdigest()[:24]
            records.append({
                "id":     rec_id,
                "text":   loc,
                "title":  loc[:80],
                "url":    loc,
                "source": "web_acquisition_data",
                "tags":   extra_tags + ["sitemap", "url"],
            })

    if not records:
        return sub_ds, 0
    try:
        await ingest_fn(sub_ds, records, source="web_acquisition_data",
                        tags=extra_tags + [kind, "data"])
    except Exception as e:
        log.warning("ingest %s into %s: %s", kind, sub_ds, e)
        return sub_ds, 0

    # Link parent dataset → sub_dataset in the graph
    try:
        graph = _get_graph()
        if graph and graph.available:
            await graph.upsert_node("Dataset", parent_ds, {"id": parent_ds})
            await graph.upsert_node("Dataset", sub_ds, {"id": sub_ds, "kind": kind})
            await graph.link("Dataset", parent_ds, "Dataset", sub_ds, rel="HAS_DATA_SUBSET")
    except Exception:
        pass

    return sub_ds, len(records)


# ═══════════════════════════════════════════════════════════════════════════
# PAGE CONTENT EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════

_BOILERPLATE_RE = re.compile(
    r"(?:^|[\s_-])(?:ad|ads|advert|advertis|sponsor|promo|banner|cookie|consent|"
    r"newsletter|subscrib|signup|sign-up|sidebar|widget|related|recirc|recommend|"
    r"share|social|comment|disqus|nav|menu|breadcrumb|pagination|pager|footer|"
    r"header|masthead|popup|modal|overlay|paywall|toolbar|skip|hidden|"
    r"cta|donate|cookiebar|gdpr)", re.I)
_ROLE_BOILERPLATE_RE = re.compile(
    r"(navigation|banner|complementary|contentinfo|search|dialog|alert)", re.I)


def _normalise_ws(s: str) -> str:
    return " ".join((s or "").split())


def _densest_block(soup):
    """Pick the container holding the most paragraph text — a cheap readability
    heuristic for pages without a <main>/<article>."""
    best, best_len = None, 0
    for el in soup.find_all(["article", "main", "section", "div"]):
        ps = el.find_all("p", recursive=True)
        if len(ps) < 2:
            continue
        L = sum(len(p.get_text(strip=True)) for p in ps)
        if L > best_len:
            best_len, best = L, el
    return best


def _strip_boilerplate(soup):
    """Remove scripts, chrome and ad/nav/cookie/comment boilerplate in place."""
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header",
                     "aside", "form", "button", "svg", "iframe", "template"]):
        tag.decompose()
    for attr in ("class", "id"):
        for el in soup.find_all(attrs={attr: _BOILERPLATE_RE}):
            el.decompose()
    for el in soup.find_all(attrs={"role": _ROLE_BOILERPLATE_RE}):
        el.decompose()
    for el in soup.find_all(attrs={"aria-hidden": "true"}):
        el.decompose()


def _extract_page_structure(html: str, url: str, max_links: int = 0,
                            max_text: int = 0, max_html: int = 3_000_000) -> Dict:
    """Extract structured content from HTML: headings, sections, links, tables,
    code blocks, lists, metadata. Returns a rich record.

    Caps are configurable; 0 means UNLIMITED (exhaustive capture). Defaults are
    unlimited so crawling grabs all content / links / paths from a page."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return _extract_page_structure_regex(html, url)

    soup = BeautifulSoup((html[:max_html] if max_html else html), "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    desc = ""
    meta_desc = (soup.find("meta", attrs={"name": "description"}) or
                 soup.find("meta", attrs={"property": "og:description"}))
    if meta_desc:
        desc = (meta_desc.get("content", "") or "")[:500]

    headings = []
    for tag in soup.find_all(re.compile(r"^h[1-6]$")):
        text = tag.get_text(strip=True)
        if text and len(text) < 200:
            headings.append({"level": int(tag.name[1]), "text": text[:120]})

    # Extract ALL links with anchor text + context (no cap unless max_links set)
    links = []
    seen_hrefs = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        full = urljoin(url, href).split("#")[0]
        if not full.startswith(("http://", "https://")) or full in seen_hrefs:
            continue
        seen_hrefs.add(full)
        anchor = a.get_text(strip=True)[:120]
        parent = a.find_parent(["p", "li", "td", "div", "section"])
        context = parent.get_text(strip=True)[:200] if parent else ""
        links.append({"url": full, "anchor": anchor, "context": context,
                      "rel": (a.get("rel") or [""])[0] if a.get("rel") else ""})
        if max_links and len(links) >= max_links:
            break

    code_blocks = []
    for code in soup.find_all(["code", "pre"]):
        text = code.get_text(strip=True)
        if len(text) > 20:
            lang = ""
            for c in code.get("class", []):
                if c.startswith(("language-", "lang-", "highlight-")):
                    lang = c.split("-", 1)[1]
                    break
            code_blocks.append({"language": lang, "text": text[:2000]})

    # Tables (count + light sample) — used for source-type / quality signals
    tables = []
    for tb in soup.find_all("table"):
        rows = tb.find_all("tr")
        if len(rows) >= 2:
            tables.append({"rows": len(rows)})

    _strip_boilerplate(soup)
    main_el = (soup.find("main") or soup.find("article")
               or soup.find(attrs={"role": "main"})
               or soup.find(id=re.compile(
                   r"^(content|main|mw-content-text|article|post|story)", re.I))
               or soup.find(class_=re.compile(
                   r"(article-body|post-content|entry-content|story-body|"
                   r"articlebody|content__article|content|article|post|entry)", re.I)))
    if not main_el:
        main_el = _densest_block(soup)
    text_root = main_el if main_el else (soup.body or soup)
    full_text = _normalise_ws(text_root.get_text(separator=" "))
    if max_text:
        full_text = full_text[:max_text]

    lists = []
    for ol_ul in soup.find_all(["ul", "ol"]):
        items = [li.get_text(strip=True)[:200]
                 for li in ol_ul.find_all("li", recursive=False)
                 if li.get_text(strip=True)]
        if len(items) >= 2:
            lists.append({"type": ol_ul.name, "items": items[:60]})

    return {
        "title": title,
        "description": desc,
        "headings": headings[:120],
        "links": (links[:max_links] if max_links else links),
        "code_blocks": code_blocks[:60],
        "lists": lists[:60],
        "tables": tables,
        "full_text": full_text,
        "word_count": len(full_text.split()),
    }


def _extract_page_structure_regex(html: str, url: str) -> Dict:
    """Fallback when BeautifulSoup is unavailable."""
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()

    text = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()[:20000]

    links = []
    for m in re.finditer(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        href, anchor = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()[:80]
        full = urljoin(url, href).split("#")[0]
        if full.startswith(("http://", "https://")):
            links.append({"url": full, "anchor": anchor, "context": ""})

    headings = []
    for m in re.finditer(r"<h([1-6])[^>]*>(.*?)</h\1>", html, re.I | re.S):
        txt = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if txt:
            headings.append({"level": int(m.group(1)), "text": txt[:120]})

    return {
        "title": title,
        "description": "",
        "headings": headings[:50],
        "links": links[:200],
        "code_blocks": [],
        "lists": [],
        "full_text": text,
        "word_count": len(text.split()),
    }


# ═══════════════════════════════════════════════════════════════════════════
# ENTITY EXTRACTION ENGINE
# ═══════════════════════════════════════════════════════════════════════════

# Regex patterns for NLP entity extraction
_PAT_DATE = re.compile(
    r"\b(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|"
    r"Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}|\b(?:January|February|March|April|May|June|"
    r"July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})\b",
    re.I
)
_PAT_YEAR = re.compile(r"\b(1[89]\d{2}|20[0-3]\d)\b")

# Titles that precede a person's name. Used both to type people and, in the
# relationship pass, to detect "X, <title> of Y" role patterns.
_TITLE_WORDS = (
    "CEO|CTO|CFO|COO|CIO|CMO|CISO|President|Vice President|VP|SVP|EVP|"
    "Managing Director|Director|Chairman|Chairwoman|Chairperson|Chair|"
    "Founder|Co-Founder|Cofounder|Co-founder|Owner|Partner|"
    "Professor|Prof|Dr|Mr|Mrs|Ms|Mx|Sir|Dame|Lord|Lady|Rev|"
    "Senator|Governor|Mayor|Minister|Secretary|Ambassador|Representative|"
    "General|Colonel|Major|Captain|Lieutenant|Sergeant|Admiral|"
    "Head|Chief|Lead|Principal|Engineer|Scientist|Researcher|Analyst|Manager"
)
_PAT_PERSON_TITLE = re.compile(
    r"\b(?:" + _TITLE_WORDS + r")\.?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z'\u2019.-]+){0,3})\b"
)

# Organisation suffixes — a capitalised run ending in one of these is an org.
_ORG_SUFFIX_WORDS = {
    "inc", "incorporated", "corp", "corporation", "ltd", "limited", "llc", "llp",
    "plc", "gmbh", "ag", "sa", "nv", "bv", "co", "company", "foundation",
    "institute", "institution", "university", "college", "academy", "school",
    "association", "group", "holdings", "partners", "labs", "laboratory",
    "laboratories", "technologies", "technology", "systems", "solutions",
    "software", "ventures", "capital", "bank", "trust", "society", "committee",
    "agency", "authority", "department", "ministry", "bureau", "consortium",
    "alliance", "network", "council", "federation", "union", "organisation",
    "organization", "studios", "studio", "media", "press", "industries",
    "enterprises", "international", "global", "worldwide", "services",
}
_PAT_ORG = re.compile(
    r"\b([A-Z][A-Za-z0-9'\u2019-]*(?:\s+(?:&|and)?\s*[A-Z][A-Za-z0-9'\u2019-]*){0,3})\s+"
    r"(?:Inc|Incorporated|Corp|Corporation|Ltd|Limited|LLC|LLP|PLC|GmbH|AG|SA|"
    r"Co|Company|Foundation|Institute|University|College|Academy|Association|"
    r"Group|Holdings|Partners|Labs|Laboratories|Technologies|Systems|Solutions|"
    r"Ventures|Capital|Bank|Trust|Consortium|Alliance|Federation|Studios|"
    r"Industries|Enterprises|Services)\b\.?"
)

# Technologies — extend the known set; case-sensitive where it matters.
_PAT_TECH = re.compile(
    r"\b(Python|JavaScript|TypeScript|Rust|Golang|Java|C\+\+|C#|Ruby|Swift|"
    r"Kotlin|Scala|Elixir|Haskell|Perl|PHP|R|Julia|Dart|Lua|"
    r"React|Vue|Angular|Svelte|Next\.js|Nuxt|Django|Flask|FastAPI|Rails|"
    r"Spring|Express|Node\.js|Deno|Bun|"
    r"Docker|Kubernetes|Podman|Terraform|Ansible|Helm|Nomad|"
    r"PostgreSQL|MySQL|MariaDB|SQLite|MongoDB|Redis|Cassandra|Neo4j|"
    r"Elasticsearch|ClickHouse|DuckDB|Kafka|RabbitMQ|"
    r"TensorFlow|PyTorch|JAX|Keras|scikit-learn|Pandas|NumPy|CUDA|"
    r"GPT-\d(?:\.\d)?|GPT|Claude|Llama|Mistral|Gemini|BERT|Transformer|"
    r"LLM|RAG|API|REST|GraphQL|gRPC|WebSocket|OAuth|JWT|SAML|"
    r"FAISS|Chroma|Ollama|ONNX|WASM|WebAssembly|LangChain|Hugging Face)\b"
)
_PAT_CAMEL = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z0-9]+){1,})\b")
_PAT_FUNC_DEF = re.compile(r"\bdef\s+(\w+)\b|\bfunction\s+(\w+)\b")
_PAT_CLASS_DEF = re.compile(r"\bclass\s+(\w+)\b")
_PAT_IMPORT = re.compile(r"(?:import|from)\s+([\w.]+)")
_PAT_URL_DOMAIN = re.compile(r"https?://([\w.-]+)/?")
_PAT_ACRONYM = re.compile(r"\b([A-Z]{2,6})\b")
_PAT_MONEY = re.compile(
    r"(\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:million|billion|trillion|bn|m|k))?|"
    r"\b\d[\d,]*(?:\.\d+)?\s?(?:million|billion|trillion)?\s?"
    r"(?:dollars|euros|pounds|USD|EUR|GBP))\b", re.I
)
# A capitalised run: Titlecase words joined by spaces or small connectors.
_PAT_CAPS_PHRASE = re.compile(
    r"\b([A-Z][A-Za-z0-9'\u2019-]+"
    r"(?:\s+(?:of|the|and|for|de|von|van)\s+[A-Z][A-Za-z0-9'\u2019-]+"
    r"|\s+[A-Z][A-Za-z0-9'\u2019-]+){1,4})\b"
)
# Single proper noun (one capitalised word) — gated hard in the extractor.
_PAT_PROPER1 = re.compile(r"\b([A-Z][a-z]{2,}(?:[A-Z][a-z]+)?)\b")

# Conservative location gazetteer + suffix words for typing places.
_LOC_SUFFIX_WORDS = {
    "city", "county", "province", "state", "republic", "kingdom", "island",
    "islands", "bay", "river", "lake", "mountain", "mountains", "valley",
    "peninsula", "desert", "ocean", "sea", "gulf", "strait", "district",
    "region", "territory", "country", "nation", "town", "village", "borough",
    "tower", "building", "bridge", "hall", "house", "centre", "center", "palace",
    "castle", "cathedral", "church", "temple", "mosque", "stadium", "arena",
    "airport", "station", "terminal", "port", "harbour", "harbor", "square",
    "park", "gardens", "street", "road", "avenue", "lane", "plaza", "court",
    "campus", "university", "college", "school", "hospital", "library",
    "museum", "gallery", "market", "mall", "estate", "manor", "abbey", "pier",
}
_COUNTRIES = {
    "united states", "usa", "america", "united kingdom", "uk", "britain",
    "england", "scotland", "wales", "ireland", "france", "germany", "spain",
    "italy", "portugal", "netherlands", "belgium", "switzerland", "austria",
    "sweden", "norway", "denmark", "finland", "poland", "russia", "ukraine",
    "china", "japan", "korea", "south korea", "north korea", "india", "pakistan",
    "bangladesh", "indonesia", "vietnam", "thailand", "singapore", "malaysia",
    "philippines", "australia", "new zealand", "canada", "mexico", "brazil",
    "argentina", "chile", "colombia", "peru", "egypt", "nigeria", "kenya",
    "south africa", "morocco", "israel", "saudi arabia", "turkey", "iran",
    "iraq", "greece", "czech republic", "hungary", "romania", "taiwan",
}
# Common words that should never become standalone single-word entities.
_ENTITY_STOPWORDS = {
    "the", "this", "that", "these", "those", "some", "many", "much", "more",
    "most", "such", "what", "when", "where", "which", "while", "who", "whom",
    "whose", "why", "how", "here", "there", "then", "than", "thus", "hence",
    "also", "however", "therefore", "moreover", "meanwhile", "nevertheless",
    "although", "because", "since", "unless", "until", "whereas", "whether",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "january", "february", "march", "april", "june", "july",
    "august", "september", "october", "november", "december",
    "introduction", "conclusion", "summary", "overview", "abstract", "contents",
    "note", "notes", "example", "examples", "figure", "table", "section",
    "chapter", "page", "home", "about", "contact", "search", "menu", "login",
    "register", "subscribe", "share", "tweet", "email", "click", "read", "more",
}
# Acronyms to ignore (too generic to be useful entities).
_ACRONYM_STOP = {
    "THE", "AND", "FOR", "BUT", "NOT", "ALL", "ANY", "CAN", "HAS", "HAD", "WAS",
    "ARE", "YOU", "OUR", "OUT", "NEW", "NOW", "WHO", "WHY", "HOW", "ITS", "HIS",
    "HER", "ONE", "TWO", "USA", "UK", "US",
    # role/title acronyms — captured as relation cues, not standalone entities
    "CEO", "CTO", "CFO", "COO", "CIO", "CMO", "CISO", "VP", "SVP", "EVP",
}
_TECH_ACRONYMS = {"API", "SDK", "CLI", "GUI", "URL", "URI", "HTTP", "HTTPS",
                  "JSON", "XML", "YAML", "SQL", "CSS", "HTML", "CPU", "GPU",
                  "RAM", "SSD", "ML", "AI", "NLP", "LLM", "RAG", "OS", "VM",
                  "CDN", "DNS", "TLS", "SSL", "JWT", "RPC", "ORM"}


# Descriptor prefixes ("the city of X", "republic of X", "mount X") and the
# generic geo/structure type words that should not, on their own, distinguish
# two names referring to the same place ("Cherry Grove City" == "Cherry Grove").
_DESCRIPTOR_PREFIX = re.compile(
    r"^(?:the\s+)?(?:city|town|township|village|municipality|borough|county|"
    r"province|state|republic|kingdom|district|region|prefecture|port|lake|"
    r"mount|mt|river|gulf|bay|isle|island|cape|fort|university|college|"
    r"institute|department|ministry|office|bank|company|corporation)\s+of\s+",
    re.I)
_STRIP_TYPEWORDS = {
    "city", "town", "township", "village", "municipality", "borough", "county",
    "province", "district", "region", "area", "metro", "metropolitan",
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "plc", "gmbh", "sa", "ag",
}


def _normalise_entity(name: str) -> str:
    """Normalise an entity name for deduplication. Strips articles, possessives,
    surrounding punctuation, leading descriptors ("the city of ..."), and
    redundant leading/trailing type words ("... City", "Acme Inc") so that
    surface variants of the same entity collapse to one key. Spaces are
    preserved (token-aware checks rely on them); use _canonical_key() for the
    space-insensitive identity key."""
    n = (name or "").strip()
    n = re.sub(r"\s+", " ", n)
    n = n.strip("\"'\u2018\u2019()[]{}")
    n = re.sub(r"['\u2019]s\b", "", n)                    # possessive
    n = n.lower().strip()
    n = re.sub(r"^(?:the|a|an)\s+", "", n)                # leading article
    n = _DESCRIPTOR_PREFIX.sub("", n)                     # "the city of x" -> "x"
    n = n.rstrip(".,;:!?").strip()
    toks = n.split()
    while len(toks) > 1 and toks[-1].strip(".") in _STRIP_TYPEWORDS:
        toks.pop()
    while len(toks) > 1 and toks[0] in _STRIP_TYPEWORDS:
        toks.pop(0)
    return " ".join(toks).strip() or n


def _canonical_key(name: str) -> str:
    """Space/punctuation-insensitive identity key used to generate the entity id,
    so "Cherrygrove", "Cherry Grove" and "the City of Cherrygrove" all collapse
    to a single node. Falls back to the spaced normal form when the alnum key
    would be too short to be safe."""
    norm = _normalise_entity(name)
    key = re.sub(r"[^a-z0-9]+", "", norm)
    return key if len(key) >= 3 else norm


def _looks_org(name: str) -> bool:
    toks = re.sub(r"[.,]", "", (name or "").lower()).split()
    return bool(toks) and toks[-1] in _ORG_SUFFIX_WORDS


def _looks_location(name: str) -> bool:
    low = _normalise_entity(name)
    if low in _COUNTRIES:
        return True
    toks = [w for w in low.split() if w not in ("of", "the", "and")]
    return any(tok in _LOC_SUFFIX_WORDS for tok in toks)


def _split_sentences(text: str):
    """Segment text into (start, end, sentence) spans, preserving offsets so
    entity positions can be mapped back to the sentence that contains them."""
    spans = []
    start = 0
    for m in re.finditer(r"[.!?]+(?:\s+|$)|\n{2,}", text):
        end = m.end()
        if text[start:end].strip():
            spans.append((start, end, text[start:end]))
        start = end
    if start < len(text) and text[start:].strip():
        spans.append((start, len(text), text[start:]))
    return spans


# Salient entity types — used to gate weak (cue-less) relations so the graph
# isn't flooded with meaningless CO_OCCURS edges between vague phrases.
_SALIENT_TYPES = {"person", "organisation", "location", "technology",
                  "product", "event", "work"}

# Ordered relation cues. First match in the between-span wins. The boolean is
# "reverse" — when True the relation direction is flipped (B rel A).
_REL_CUE_SPECS = [
    # passive forms first — "X founded by Y" means Y FOUNDED X (reverse)
    (r"co-?founded by|founded by|established by", "FOUNDED", True),
    (r"acquired by|bought by|purchased by", "ACQUIRED", True),
    (r"developed by|built by|designed by|made by|created by", "DEVELOPED", True),
    (r"written by|authored by", "AUTHORED", True),
    (r"owned by", "OWNS", True),
    (r"led by|headed by|run by|managed by", "LEADS", True),
    (r"published by|released by", "RELEASED", True),
    (r"co-?founded|founded|establish(?:ed)?|set up", "FOUNDED", False),
    (r"acquired|bought|purchased|took over", "ACQUIRED", False),
    (r"merged with", "MERGED_WITH", False),
    (r"invested in|funded|backed|financed", "INVESTED_IN", False),
    (r"subsidiary of|division of|unit of|owned by", "PART_OF", False),
    (r"owns|parent (?:company )?of", "OWNS", False),
    (r"part of|belongs to|within|component of|member of", "PART_OF", False),
    (r"(?:ceo|cto|cfo|coo|president|director|head|vp|chair(?:man|woman)?)\s+of|"
     r"leads|heads|runs|manages|oversees|chairs|in charge of", "LEADS", False),
    (r"works for|works at|employed by|joined|hired by", "WORKS_FOR", False),
    (r"reports to", "REPORTS_TO", False),
    (r"appointed|named|serves as|appointed as|elected", "HAS_ROLE", False),
    (r"based in|headquartered in|located in|situated in", "LOCATED_IN", False),
    (r"born in", "BORN_IN", False),
    (r"died in|passed away in", "DIED_IN", False),
    (r"released|launched|unveiled|announced|shipped|introduced|published",
     "RELEASED", False),
    (r"developed|built|designed|engineered|invented", "DEVELOPED", False),
    (r"uses|built with|powered by|based on|leverages|runs on|written in|"
     r"implemented in", "USES", False),
    (r"partnered with|collaborated with|teamed up with|allied with",
     "PARTNERED_WITH", False),
    (r"competes with|rival of|competitor of", "COMPETES_WITH", False),
    (r"wrote|authored|co-authored", "AUTHORED", False),
    (r"regulated by|governed by", "REGULATED_BY", False),
    (r"succeeded|replaced|took over from", "SUCCEEDED", False),
    (r"acquired by", "ACQUIRED", True),
]
_REL_CUES = [(re.compile(r"\b(?:" + pat + r")\b", re.I), rel, rev)
             for pat, rel, rev in _REL_CUE_SPECS]


# ═══════════════════════════════════════════════════════════════════════════
# PLUGGABLE NER BACKEND  — statistical/zero-shot model when available.
# Far better person/org/place typing and compound-name recall ("Seacourt Tower
# of Oxford") than capitalisation heuristics. Priority: GLiNER (zero-shot, 2024
# SOTA) > spaCy > heuristic fallback. Enable on the host:
#   pip install gliner                                  (FABRIC_GLINER_MODEL)
#   pip install spacy && python -m spacy download en_core_web_sm   (or _trf)
# Control: FABRIC_NER_BACKEND=auto|gliner|spacy|heuristic
# ═══════════════════════════════════════════════════════════════════════════

_SPACY_TYPE = {
    "PERSON": "person", "ORG": "organisation", "GPE": "location",
    "LOC": "location", "FAC": "location", "PRODUCT": "product",
    "EVENT": "event", "WORK_OF_ART": "work", "LAW": "concept",
    "LANGUAGE": "concept", "NORP": "concept", "DATE": "date",
    "TIME": "date", "MONEY": "money",
}
# A SMALL set of aliases that fold obvious synonyms onto a shared type; every
# OTHER label GLiNER (or the LLM) emits is kept VERBATIM (slugified) so the type
# vocabulary is open/flexible rather than prescriptive.
_TYPE_ALIASES = {
    "organization": "organisation", "company": "organisation",
    "corporation": "organisation", "place": "location", "building": "location",
    "facility": "location", "landmark": "location", "geographic location": "location",
    "geopolitical entity": "location", "creative work": "work",
    "work of art": "work", "datetime": "date", "time": "date",
    "monetary value": "money", "currency": "money", "human": "person",
    "people": "person", "tool": "technology", "software": "technology",
}


def _slug_type(label: str) -> str:
    """Slugify an arbitrary entity-type label into a clean, open-vocabulary type
    (e.g. 'Video Game Character' -> 'video_game_character')."""
    s = re.sub(r"[^a-z0-9]+", "_", (label or "").strip().lower()).strip("_")
    return s or "named_entity"


def _map_gliner_type(label: str) -> str:
    low = (label or "").strip().lower()
    if low in _TYPE_ALIASES:
        return _TYPE_ALIASES[low]
    if low in ("person", "organisation", "location", "product", "technology",
               "event", "work", "date", "money", "concept", "role"):
        return low
    return _slug_type(label)  # keep ANY other label as its own type


# A broad DEFAULT label menu spanning many domains. Fully overridable with
# FABRIC_GLINER_LABELS (comma-separated) — GLiNER is zero-shot, so any labels
# work. Kept wide so the graph is not boxed into a handful of categories.
_GLINER_LABELS_DEFAULT = [
    "person", "organization", "company", "government agency", "location",
    "city", "country", "building", "landmark", "geographic feature",
    "product", "technology", "software", "programming language", "device",
    "vehicle", "creative work", "book", "film", "game", "song", "character",
    "event", "date", "money", "law", "field of study", "scientific concept",
    "biological species", "chemical", "medical condition", "job title",
    "nationality", "language", "award", "currency", "unit",
]


def _gliner_labels() -> List[str]:
    env = os.getenv("FABRIC_GLINER_LABELS", "").strip()
    if env:
        labs = [x.strip() for x in env.split(",") if x.strip()]
        if labs:
            return labs
    return _GLINER_LABELS_DEFAULT


def _gliner_threshold() -> float:
    try:
        return float(os.getenv("FABRIC_GLINER_THRESHOLD", "0.4"))
    except Exception:
        return 0.4

_NER_STATE = {"init": False, "kind": "heuristic", "obj": None}


def _ner_backend() -> Dict:
    """Lazily detect and cache the best available NER backend."""
    if _NER_STATE["init"]:
        return _NER_STATE
    _NER_STATE["init"] = True
    pref = os.getenv("FABRIC_NER_BACKEND", "auto").lower()

    if pref in ("auto", "gliner"):
        try:
            from gliner import GLiNER  # type: ignore
            model = os.getenv("FABRIC_GLINER_MODEL", "urchade/gliner_medium-v2.1")
            _NER_STATE.update(kind="gliner", obj=GLiNER.from_pretrained(model))
            log.info("entity NER backend: GLiNER (%s)", model)
            return _NER_STATE
        except Exception as e:
            log.debug("GLiNER unavailable: %s", e)

    if pref in ("auto", "spacy"):
        try:
            import spacy  # type: ignore
            model = os.getenv("FABRIC_NER_MODEL", "en_core_web_sm")
            nlp = spacy.load(model, disable=["lemmatizer", "tagger", "parser",
                                             "attribute_ruler"])
            _NER_STATE.update(kind="spacy", obj=nlp)
            log.info("entity NER backend: spaCy (%s)", model)
            return _NER_STATE
        except Exception as e:
            log.debug("spaCy unavailable: %s", e)

    _NER_STATE.update(kind="heuristic")
    log.info("entity NER backend: heuristic (install spaCy or GLiNER for better NER)")
    return _NER_STATE


def _resolve_overlaps(entities: List[Dict]) -> List[Dict]:
    """Keep the strongest (confidence, then length) entity per character region;
    drop anything whose span overlaps an already-accepted one."""
    ordered = sorted(entities,
                     key=lambda e: (-e.get("confidence", 0.5), -len(e["name"])))
    accepted: List[Dict] = []
    taken: List[tuple] = []
    for e in ordered:
        s = e.get("position", 0)
        en = s + len(e["name"])
        if any(not (en <= ts or s >= te) for ts, te in taken):
            continue
        accepted.append(e)
        taken.append((s, en))
    accepted.sort(key=lambda e: e.get("position", 0))
    return accepted


def _supplement_entities(text: str, content_type: str,
                         exclude: Optional[set] = None) -> List[Dict]:
    """High-precision patterns a statistical model tends to miss: the curated
    technology list, code symbols, domains and tech acronyms."""
    seen = set(exclude or set())
    out: List[Dict] = []

    def _add(name, etype, pos, conf):
        norm = _normalise_entity(name)
        if not norm or len(norm) < 2 or norm in seen:
            return
        if " " not in norm and norm in _ENTITY_STOPWORDS:
            return
        seen.add(norm)
        out.append({"name": name.strip().strip(".,;:"), "type": etype,
                    "normalised": norm, "position": pos,
                    "context": "", "confidence": conf, "description": ""})

    for m in _PAT_TECH.finditer(text):
        _add(m.group(1), "technology", m.start(), 0.8)
    if content_type in ("code", "web"):
        for m in _PAT_CLASS_DEF.finditer(text):
            _add(m.group(1), "class", m.start(), 0.8)
        for m in _PAT_FUNC_DEF.finditer(text):
            nm = m.group(1) or m.group(2)
            if nm:
                _add(nm, "function", m.start(), 0.75)
        for m in _PAT_IMPORT.finditer(text):
            _add(m.group(1), "module", m.start(), 0.7)
        for m in _PAT_CAMEL.finditer(text):
            _add(m.group(1), "type_name", m.start(), 0.55)
    for m in _PAT_URL_DOMAIN.finditer(text):
        d = m.group(1)
        if d and len(d) > 4 and "." in d:
            _add(d, "domain", m.start(), 0.6)
    for m in _PAT_ACRONYM.finditer(text):
        if m.group(1) in _TECH_ACRONYMS:
            _add(m.group(1), "technology", m.start(), 0.6)
    return out


def _model_entities(text: str, content_type: str) -> List[Dict]:
    """Run the active statistical NER backend over the text."""
    st = _ner_backend()
    kind = st["kind"]
    out: List[Dict] = []
    seen = set()

    def _add(name, etype, pos, conf):
        name = (name or "").strip().strip(".,;:")
        norm = _normalise_entity(name)
        if not norm or len(norm) < 2 or len(norm) > 120 or norm in seen:
            return
        if " " not in norm and norm in _ENTITY_STOPWORDS:
            return
        seen.add(norm)
        out.append({"name": name, "type": etype, "normalised": norm,
                    "position": pos,
                    "context": text[max(0, pos - 30):pos + len(name) + 30][:200],
                    "confidence": round(conf, 2), "description": ""})

    try:
        if kind == "spacy":
            doc = st["obj"](text[:100000])
            _spacy_drop = {"CARDINAL", "ORDINAL", "PERCENT", "QUANTITY"}
            for e in doc.ents:
                if e.label_ in _spacy_drop:
                    continue
                ty = _SPACY_TYPE.get(e.label_) or _slug_type(e.label_)
                _add(e.text, ty, e.start_char, 0.85)
        elif kind == "gliner":
            g = st["obj"]
            step = 1400
            for cs in range(0, min(len(text), 24000), step):
                seg = text[cs:cs + step]
                try:
                    preds = g.predict_entities(seg, _gliner_labels(), threshold=_gliner_threshold())
                except Exception as ie:
                    log.debug("gliner predict: %s", ie)
                    continue
                for e in preds:
                    ty = _map_gliner_type(str(e.get("label", "")))
                    _add(e.get("text", ""), ty, cs + int(e.get("start", 0)),
                         float(e.get("score", 0.8)))
    except Exception as ex:
        log.debug("model NER (%s): %s", kind, ex)
    return out


def _extract_entities_from_text(text: str, content_type: str = "text") -> List[Dict]:
    """Extract typed entities. Uses the statistical NER backend (spaCy/GLiNER)
    when available — far better person/org/place typing and compound-name
    recall — supplemented by high-precision tech/code patterns. Falls back to
    the heuristic engine when no model is installed."""
    if not text or not text.strip():
        return []
    st = _ner_backend()
    if st["kind"] in ("spacy", "gliner"):
        ents = _model_entities(text, content_type)
        ents += _supplement_entities(text, content_type,
                                     exclude={e["normalised"] for e in ents})
        return _resolve_overlaps(ents)
    return _heuristic_entities(text, content_type)


def _heuristic_entities(text: str, content_type: str = "text") -> List[Dict]:
    """Extract typed entities from text.

    Returns a list of {name, type, normalised, position, context, confidence,
    description}. The engine layers specific patterns (dates, titled people,
    org-suffix names, technologies, code symbols, acronyms, money) over a
    general capitalised-phrase detector, retyping generic phrases into
    person/organisation/location where signals allow, and filters out
    sentence-initial stopwords and other common false positives.
    """
    entities: List[Dict] = []
    seen = set()
    # sentence-start offsets, to suppress capitalised stopwords at the start of
    # a sentence (e.g. "However", "These") being mistaken for entities.
    sent_starts = {s for s, _e, _t in _split_sentences(text)}

    def _add(name, etype, pos=0, ctx="", conf=0.5):
        norm = _normalise_entity(name)
        if not norm or len(norm) < 2 or len(norm) > 120:
            return
        if norm in seen:
            return
        # single-word stopword guard
        if " " not in norm and norm in _ENTITY_STOPWORDS:
            return
        seen.add(norm)
        entities.append({
            "name": name.strip().strip(".,;:"),
            "type": etype, "normalised": norm,
            "position": pos, "context": ctx[:200],
            "confidence": round(conf, 2), "description": "",
        })

    # Dates / years
    for m in _PAT_DATE.finditer(text):
        _add(m.group(1), "date", m.start(), conf=0.9)
    for m in _PAT_YEAR.finditer(text):
        ctx = text[max(0, m.start()-40):m.end()+40].lower()
        if any(w in ctx for w in ("founded", "created", "established", "released",
                                  "launched", "born", "died", "started",
                                  "invented", "acquired", "merged", "since")):
            _add(m.group(1), "year", m.start(), ctx, conf=0.7)

    # People with explicit titles (high confidence)
    for m in _PAT_PERSON_TITLE.finditer(text):
        _add(m.group(1), "person", m.start(1),
             text[max(0, m.start()-40):m.end()+40], conf=0.85)

    # Organisations by suffix (high confidence)
    for m in _PAT_ORG.finditer(text):
        _add(m.group(0), "organisation", m.start(), conf=0.85)

    # Technologies
    for m in _PAT_TECH.finditer(text):
        _add(m.group(1), "technology", m.start(), conf=0.8)

    # Money / financial amounts
    for m in _PAT_MONEY.finditer(text):
        _add(m.group(1), "money", m.start(), conf=0.7)

    # Code-specific entities
    if content_type in ("code", "web"):
        for m in _PAT_CLASS_DEF.finditer(text):
            _add(m.group(1), "class", m.start(), conf=0.8)
        for m in _PAT_FUNC_DEF.finditer(text):
            name = m.group(1) or m.group(2)
            if name:
                _add(name, "function", m.start(), conf=0.75)
        for m in _PAT_IMPORT.finditer(text):
            _add(m.group(1), "module", m.start(), conf=0.7)
        for m in _PAT_CAMEL.finditer(text):
            _add(m.group(1), "type_name", m.start(), conf=0.6)

    # Domains from URLs
    for m in _PAT_URL_DOMAIN.finditer(text):
        domain = m.group(1)
        if domain and len(domain) > 4 and "." in domain:
            _add(domain, "domain", m.start(), conf=0.6)

    # Acronyms (known tech ones typed as technology; others as acronym)
    for m in _PAT_ACRONYM.finditer(text):
        ac = m.group(1)
        if ac in _ACRONYM_STOP:
            continue
        if ac in _TECH_ACRONYMS:
            _add(ac, "technology", m.start(), conf=0.6)
        else:
            _add(ac, "acronym", m.start(), conf=0.45)

    # Capitalised phrases — general proper-noun detector with retyping.
    for m in _PAT_CAPS_PHRASE.finditer(text):
        phrase = m.group(1).strip()
        if len(phrase) < 3:
            continue
        # Drop sentence-initial single common words masquerading as entities.
        first_word = phrase.split()[0]
        if (m.start() in sent_starts and " " not in phrase
                and first_word.lower() in _ENTITY_STOPWORDS):
            continue
        if re.match(r"^(The|This|That|These|Those|Some|What|When|Where|How|Why|"
                    r"There|Here|It|We|They|You|If|But|And|Or|So|Then)\b", phrase):
            # allow if it's a longer multiword proper phrase after the lead word
            rest = phrase.split(None, 1)
            if len(rest) < 2:
                continue
            phrase = rest[1]
        ctx = text[max(0, m.start()-30):m.end()+30]
        if _looks_org(phrase):
            _add(phrase, "organisation", m.start(), ctx, conf=0.7)
        elif _looks_location(phrase):
            _add(phrase, "location", m.start(), ctx, conf=0.65)
        else:
            # Without a statistical NER model or an explicit title we do NOT
            # guess "person" from capitalisation alone (the main cause of people
            # being over-represented). Default to the neutral "named_entity";
            # spaCy/GLiNER or the LLM layer assign person/org/location properly.
            _add(phrase, "named_entity", m.start(), ctx, conf=0.45)

    # Single proper nouns (one capitalised word) — only when NOT sentence-initial
    # and not a stopword. Lower confidence; retyped to location where known.
    for m in _PAT_PROPER1.finditer(text):
        word = m.group(1)
        low = word.lower()
        if m.start() in sent_starts:
            continue
        if low in _ENTITY_STOPWORDS or len(low) < 3:
            continue
        # skip if it's clearly the lead-in of an already-captured phrase
        if _normalise_entity(word) in seen:
            continue
        ctx = text[max(0, m.start()-25):m.end()+25]
        if _looks_location(word):
            _add(word, "location", m.start(), ctx, conf=0.5)
        else:
            _add(word, "named_entity", m.start(), ctx, conf=0.4)

    return _resolve_overlaps(entities)


def _extract_relationships_from_entities(entities: List[Dict], text: str) -> List[Dict]:
    """Infer typed relationships between entities.

    Sentence-scoped: only entities co-occurring in the same sentence are
    candidates. For each candidate pair the span between them is scanned for a
    relation cue (founded, acquired, leads, based in, uses, ...). A cue yields a
    typed, directed, high-confidence relation. Without a cue, a weak RELATED_TO
    edge is emitted ONLY between two salient entities that are close together —
    everything else is dropped, which keeps the graph meaningful instead of a
    hairball of CO_OCCURS edges.
    """
    if not entities:
        return []
    sents = _split_sentences(text)
    # Map each entity to the sentence index containing its position.
    ents_by_sent: Dict[int, List[Dict]] = {}
    for ent in entities:
        pos = ent.get("position", 0)
        for si, (s, e, _seg) in enumerate(sents):
            if s <= pos < e:
                ents_by_sent.setdefault(si, []).append(ent)
                break

    best: Dict[tuple, Dict] = {}

    def _consider(a, b, rel, score, cue, ctx):
        if a["normalised"] == b["normalised"]:
            return
        key = (a["normalised"], b["normalised"])
        rkey = (b["normalised"], a["normalised"])
        # keep the single best-scoring relation per unordered pair
        prev = best.get(key) or best.get(rkey)
        if prev and prev["score"] >= score:
            return
        best.pop(rkey, None)
        best[key] = {
            "from_name": a["name"], "from_type": a["type"],
            "to_name": b["name"], "to_type": b["type"],
            "rel": rel, "score": round(score, 3),
            "distance": abs(a.get("position", 0) - b.get("position", 0)),
            "cue": cue, "context": ctx[:160],
        }

    for si, ents in ents_by_sent.items():
        if len(ents) < 2:
            continue
        ents = sorted(ents, key=lambda x: x.get("position", 0))
        # Relate only ADJACENT entities (consecutive by position). Reaching
        # across an intervening entity is what produced spurious long-range
        # links (e.g. a verb belonging to a different pair). Adjacency keeps the
        # cue text small and attributable.
        for i in range(len(ents) - 1):
            a, b = ents[i], ents[i + 1]
            a_end = a.get("position", 0) + len(a["name"])
            b_start = b.get("position", 0)
            between = text[a_end:b_start]
            if len(between) > 160:
                continue  # too far apart to be reliably related
            matched = None
            # locative override: "... in/at/near <Place>" — covers proper-noun
            # places not in the gazetteer (typed named_entity) as well.
            if (b["type"] in ("location", "named_entity")
                    and re.search(r"\b(?:in|at|near|from)\s*$", between, re.I)):
                matched = ("LOCATED_IN", False)
            if matched is None:
                for rx, rel, rev in _REL_CUES:
                    if rx.search(between):
                        matched = (rel, rev)
                        break
            if matched:
                rel, rev = matched
                frm, to = (b, a) if rev else (a, b)
                _consider(frm, to, rel, 0.85, rel, between)
            elif a["type"] in ("date", "year") or b["type"] in ("date", "year"):
                other = b if a["type"] in ("date", "year") else a
                if other["type"] in _SALIENT_TYPES:
                    _consider(a, b, "DATED", 0.4, "", between)
            elif (a["type"] in _SALIENT_TYPES and b["type"] in _SALIENT_TYPES
                  and len(between) < 60):
                _consider(a, b, "RELATED_TO", 0.3, "", between)

    rels = sorted(best.values(), key=lambda r: -r["score"])
    return rels[:200]


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITY: DEEP WEB ACQUISITION
# ═══════════════════════════════════════════════════════════════════════════

@capability(
    "fabric.web.acquire",
    http_method="POST", http_path="/fabric/web/acquire",
    http_tags=["fabric", "web", "acquisition"],
    memory="on",
    description="Multi-stage web acquisition with full content fetching, structural "
                "extraction, negative word filtering, and entity graph building. "
                "Creates both a source and a dataset. Pages are fetched for their full "
                "content — not just link discovery. Supports guided crawling with LLM "
                "or manual stage checkpoints. "
                "Input: url (str!), dataset_id (str — auto from hostname), "
                "max_pages (int default 50), max_depth (int default 4), "
                "negative_words (str — comma-sep words to avoid, e.g. 'forum,login,signup'), "
                "negative_urls (str — comma-sep URL fragments to skip, e.g. '/user/,/login'), "
                "topic (str — focus keyword), topic_dropoff (int default 3), "
                "extract_entities (bool default True — run entity extraction), "
                "fetch_content (bool default True — fetch full page text), "
                "same_domain (bool default True), "
                "tags (str — comma-sep). "
                "Output: {acquisition_id, dataset_id, pages_fetched, entities_found, "
                "source_id, links_discovered, structure}.",
)
async def cap_web_acquire(
    url:              str,
    dataset_id:       str  = "",
    max_pages:        int  = 50,
    max_depth:        int  = 4,
    negative_words:   str  = "",
    negative_urls:    str  = "",
    topic:            str  = "",
    topic_dropoff:    int  = 3,
    extract_entities: bool = True,
    fetch_content:    bool = True,
    same_domain:      bool = True,
    tags:             str  = "",
    trace_id=None,
) -> Dict:
    if not url or not url.strip():
        return {"error": "url required"}
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    _ensure_acq_table()
    max_pages = max(1, min(2000, max_pages))
    max_depth = max(1, min(20, max_depth))

    parsed = urlparse(url)
    hostname = parsed.netloc
    ds_id = dataset_id or f"web.{re.sub(r'[^a-z0-9]', '_', hostname.lower())[:30]}"
    acq_id = f"acq_{hashlib.sha1(f'{url}:{ds_id}:{now_iso()}'.encode()).hexdigest()[:16]}"

    neg_filter = NegativeFilter(
        negative_words=[w.strip() for w in (negative_words or "").split(",") if w.strip()],
        negative_url_patterns=[p.strip() for p in (negative_urls or "").split(",") if p.strip()],
    )
    extra_tags = [t.strip() for t in (tags or "").split(",") if t.strip()]
    topic_lc = (topic or "").strip().lower()
    ingest_fn = _fabric_ingest()

    async def _emit(stage, **kw):
        try:
            await emit_event({"type": "fabric.web.acquire.progress",
                              "acquisition_id": acq_id, "dataset_id": ds_id,
                              "stage": stage, **kw})
        except Exception:
            pass

    # Save acquisition state
    try:
        conn = _sqlite_conn()
        conn.execute(
            "INSERT OR REPLACE INTO fabric_acquisitions "
            "(id, dataset_id, source_url, status, config, state, pages_fetched, "
            "entities_found, created_at, updated_at) VALUES (?,?,?,?,?,?,0,0,?,?)",
            (acq_id, ds_id, url, "running",
             json.dumps({
                 "max_pages": max_pages, "max_depth": max_depth,
                 "negative_words": negative_words, "negative_urls": negative_urls,
                 "topic": topic, "topic_dropoff": topic_dropoff,
                 "same_domain": same_domain, "tags": tags,
             }),
             "{}", now_iso(), now_iso())
        )
        conn.commit()
    except Exception as e:
        log.debug("acq save: %s", e)

    await _emit("starting", url=url, max_pages=max_pages, max_depth=max_depth)

    # BFS crawl with content fetching
    queue: list = [(url, 0, 0)]  # (url, depth, dry_streak)
    visited: Set[str] = set()
    pages_fetched = 0
    pages_skipped = 0
    all_entities: Dict[str, Dict] = {}  # normalised_name -> entity
    all_relations: List[Dict] = []
    all_links: List[Dict] = []
    page_map: Dict[str, Dict] = {}  # url -> {title, depth, links_out, links_in}

    try:
        from bs4 import BeautifulSoup
        _have_bs4 = True
    except ImportError:
        _have_bs4 = False

    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True, headers=_HTTP_HEADERS
    ) as client:
        while queue and pages_fetched < max_pages:
            page_url, depth, dry_streak = queue.pop(0)

            # Normalise
            page_url = page_url.split("#")[0]
            if page_url in visited:
                continue
            if depth > max_depth:
                continue

            # Negative URL filter
            if not neg_filter.url_allowed(page_url):
                pages_skipped += 1
                continue

            # Same domain check
            if same_domain:
                page_host = urlparse(page_url).netloc
                if page_host != hostname:
                    h_parts = page_host.split(".")
                    s_parts = hostname.split(".")
                    if len(h_parts) >= 2 and len(s_parts) >= 2:
                        if h_parts[-2:] != s_parts[-2:]:
                            continue
                    else:
                        continue

            visited.add(page_url)

            # Fetch the page
            try:
                await asyncio.sleep(_CRAWL_DELAY)
                resp = await client.get(page_url)
                if resp.status_code >= 400:
                    continue
                ct = resp.headers.get("content-type", "").lower()
            except Exception as e:
                log.debug("fetch %s: %s", page_url, e)
                continue

            raw_text = resp.text

            # ── Data format detection ────────────────────────────────────
            # If the response looks like structured data (CSV / JSON / RSS /
            # XML sitemap), consume it into a dedicated dataset rather than
            # treating it as an HTML page. This means crawls of mixed sites
            # naturally pick up data feeds without the user pre-configuring them.
            data_kind = _detect_data_kind(page_url, ct, raw_text)
            if data_kind:
                try:
                    sub_ds_id, ingested_count = await _ingest_data_payload(
                        page_url, raw_text, data_kind, ds_id, hostname,
                        extra_tags, ingest_fn
                    )
                    pages_fetched += 1
                    await _emit("data_detected",
                                url=page_url,
                                kind=data_kind,
                                dataset_id=sub_ds_id,
                                ingested=ingested_count,
                                parent_dataset_id=ds_id)
                    # Keep crawling for HTML — data subsets are side effects;
                    # don't try to extract links from a CSV/JSON.
                    continue
                except Exception as e:
                    log.debug("data ingest %s (%s): %s", page_url, data_kind, e)
                    # Fall through: treat as text/html below
            elif "text/html" not in ct and "text/plain" not in ct:
                # Not HTML and not detected as structured data → skip
                continue

            raw_html = raw_text

            # Extract page structure
            if fetch_content:
                structure = _extract_page_structure(raw_html, page_url)
            else:
                # Minimal extraction: just links and title
                structure = _extract_page_structure_regex(raw_html, page_url)

            title = structure.get("title", "")
            full_text = structure.get("full_text", "")
            page_links = structure.get("links", [])

            # Negative content filter
            if neg_filter.content_dominated(full_text):
                pages_skipped += 1
                continue

            # Topic match
            text_lc = full_text.lower()
            title_lc = title.lower()
            url_lc = page_url.lower()
            if topic_lc:
                tokens = [t for t in re.split(r"\s+", topic_lc) if len(t) >= 3]
                has_topic = any(
                    t in text_lc or t in title_lc or t in url_lc
                    for t in (tokens or [topic_lc])
                )
                if has_topic:
                    next_dry = 0
                else:
                    next_dry = dry_streak + 1
                if next_dry > topic_dropoff:
                    pages_skipped += 1
                    continue
            else:
                next_dry = 0

            # Build record
            content_hash = hashlib.sha256(full_text.encode()).hexdigest()[:16]
            record_id = hashlib.sha256(
                f"{ds_id}:{page_url}:{content_hash}".encode()
            ).hexdigest()[:24]

            record = {
                "id": record_id,
                "text": full_text[:8000],
                "title": title,
                "url": page_url,
                "hostname": hostname,
                "depth": depth,
                "content_hash": content_hash,
                "headings": structure.get("headings", [])[:20],
                "link_count": len(page_links),
                "code_block_count": len(structure.get("code_blocks", [])),
                "word_count": structure.get("word_count", 0),
                "tags": extra_tags + ["web", hostname.split(".")[0]],
                "source": "web_acquisition",
            }

            # Add code blocks as structured metadata
            if structure.get("code_blocks"):
                record["code_blocks"] = structure["code_blocks"][:10]

            # Ingest the record
            try:
                result = await ingest_fn(
                    ds_id, [record],
                    source="web_acquisition",
                    tags=extra_tags + ["web"]
                )
                pages_fetched += 1
            except Exception as e:
                log.warning("ingest %s: %s", page_url, e)
                continue

            # Track page structure
            page_map[page_url] = {
                "title": title, "depth": depth,
                "links_out": [l["url"] for l in page_links[:50]],
                "record_id": record_id,
            }

            # Find parent URL (page that linked here) for live-graph edges
            parent_url = None
            for src_url, info in page_map.items():
                if src_url == page_url:
                    continue
                if page_url in info.get("links_out", []):
                    parent_url = src_url
                    break

            # Emit per-page event so the live crawl graph can grow
            await _emit("page_added",
                        url=page_url,
                        title=(title or "")[:80],
                        record_id=record_id,
                        parent_url=parent_url,
                        depth=depth,
                        dataset_id=ds_id)

            # Track all discovered links
            for link in page_links:
                all_links.append({
                    "from_url": page_url,
                    "to_url":   link["url"],
                    "anchor":   link.get("anchor", ""),
                })

            # Entity extraction
            if extract_entities and full_text:
                content_type = "code" if structure.get("code_blocks") else "text"
                entities = _extract_entities_from_text(full_text, content_type)
                relations = _extract_relationships_from_entities(entities, full_text)

                for ent in entities:
                    norm = ent["normalised"]
                    is_new = norm not in all_entities
                    if norm in all_entities:
                        all_entities[norm]["mention_count"] += 1
                        all_entities[norm]["record_ids"].add(record_id)
                        all_entities[norm]["datasets"].add(ds_id)
                    else:
                        all_entities[norm] = {
                            **ent,
                            "mention_count": 1,
                            "record_ids": {record_id},
                            "datasets": {ds_id},
                        }
                    # Emit per-new-entity event for live graph
                    if is_new:
                        await _emit("entity_found",
                                    entity_name=ent["name"][:80],
                                    entity_type=ent["type"],
                                    from_url=page_url,
                                    record_id=record_id)

                all_relations.extend(relations)

            # Queue child links
            added = 0
            for link in page_links:
                if added >= 60:
                    break
                link_url = link["url"].split("#")[0]
                if link_url in visited:
                    continue
                if len(queue) > max_pages * 5:
                    break
                queue.append((link_url, depth + 1, next_dry))
                added += 1

            # Progress event
            if pages_fetched % 3 == 1 or pages_fetched == 1:
                await _emit("progress",
                            pages=pages_fetched, queued=len(queue),
                            depth=depth, skipped=pages_skipped,
                            entities=len(all_entities),
                            current=page_url[:120])

    # Persist entities to entity graph
    entities_persisted = 0
    if extract_entities and all_entities:
        entities_persisted = await _persist_entity_graph(
            all_entities, all_relations, ds_id
        )

    # Persist link structure as graph edges
    links_persisted = 0
    graph = _get_graph()
    if graph and graph.available and page_map:
        try:
            # First, upsert FabricRecord nodes with properties so they have
            # display names in the graph view (link_many creates bare nodes
            # with only an id prop via MERGE).
            for page_url, info in page_map.items():
                rid = info.get("record_id")
                if rid:
                    await graph.upsert_node("FabricRecord", rid, {
                        "id":         rid,
                        "title":      (info.get("title") or "")[:120],
                        "url":        page_url[:500],
                        "dataset_id": ds_id,
                        "depth":      info.get("depth", 0),
                    })
            # Ensure the Dataset node itself exists and has a CONTAINS edge
            # to each record so the expand-dataset flow works
            await graph.upsert_node("Dataset", ds_id, {"id": ds_id})
            contains_edges = []
            for page_url, info in page_map.items():
                rid = info.get("record_id")
                if rid:
                    contains_edges.append({
                        "from_label": "Dataset", "from_id": ds_id,
                        "to_label": "FabricRecord", "to_id": rid,
                        "rel": "CONTAINS",
                        "props": {},
                    })
            if contains_edges:
                await graph.link_many(contains_edges)

            # Now create the LINKS_TO edges between records
            edges = []
            for link in all_links[:500]:
                from_rec = page_map.get(link["from_url"], {}).get("record_id")
                to_rec = page_map.get(link["to_url"], {}).get("record_id")
                if from_rec and to_rec and from_rec != to_rec:
                    edges.append({
                        "from_label": "FabricRecord", "from_id": from_rec,
                        "to_label": "FabricRecord", "to_id": to_rec,
                        "rel": "LINKS_TO",
                        "props": {"anchor": link.get("anchor", "")[:60]},
                    })
            if edges:
                links_persisted = await graph.link_many(edges)
        except Exception as e:
            log.warning("link graph persist: %s", e)

    # Register as a fabric source
    source_id = f"webacq_{hashlib.sha1(url.encode()).hexdigest()[:14]}"
    source_registered = False
    try:
        from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY
        add_fn = (CAPABILITY_REGISTRY.get("fabric.sources.add") or {}).get("fn")
        if add_fn:
            await add_fn(
                url=url, source_type="docs",
                label=f"Web: {hostname}",
                dataset_id=ds_id, interval=86400,
                tags=tags or "web",
                config=json.dumps({
                    "start_url": url, "max_pages": max_pages,
                    "max_depth": max_depth, "topic": topic,
                    "negative_words": negative_words,
                    "negative_urls": negative_urls,
                    "same_domain": same_domain,
                }),
                id=source_id,
            )
            source_registered = True
        else:
            log.warning("source register: fabric.sources.add capability not found, using direct SQLite")
    except Exception as e:
        log.warning("source register via capability: %s", e)

    # Fallback: write directly to fabric_sources if capability wasn't available
    if not source_registered:
        try:
            src_conn = _sqlite_conn()
            src_conn.execute(
                "INSERT OR REPLACE INTO fabric_sources "
                "(id, source_type, url, label, dataset_id, interval, tags, "
                "enabled, pull_count, last_pulled, created_at, config) "
                "VALUES (?,?,?,?,?,?,?,1,1,?,?,?)",
                (source_id, "docs", url, f"Web: {hostname}",
                 ds_id, 86400, tags or "web", now_iso(), now_iso(),
                 json.dumps({
                     "start_url": url, "max_pages": max_pages,
                     "max_depth": max_depth, "topic": topic,
                     "negative_words": negative_words,
                     "negative_urls": negative_urls,
                     "same_domain": same_domain,
                 }))
            )
            src_conn.commit()
            source_registered = True
            log.info("source registered via direct SQLite: %s", source_id)
            # Emit the event so the UI picks it up
            await emit_event({
                "type": "fabric.source.added",
                "id": source_id,
                "label": f"Web: {hostname}",
                "dataset_id": ds_id,
            })
        except Exception as e2:
            log.warning("source register direct fallback: %s", e2)

    # Update acquisition status
    try:
        conn = _sqlite_conn()
        conn.execute(
            "UPDATE fabric_acquisitions SET status='done', pages_fetched=?, "
            "entities_found=?, updated_at=?, state=? WHERE id=?",
            (pages_fetched, len(all_entities), now_iso(),
             json.dumps({
                 "visited": list(visited)[:500],
                 "queue_remaining": len(queue),
             }),
             acq_id)
        )
        conn.commit()
    except Exception:
        pass

    await _emit("done",
                pages=pages_fetched, entities=len(all_entities),
                relations=len(all_relations), links=len(all_links),
                skipped=pages_skipped)

    return {
        "ok": True,
        "acquisition_id": acq_id,
        "dataset_id": ds_id,
        "source_id": source_id,
        "pages_fetched": pages_fetched,
        "pages_skipped": pages_skipped,
        "links_discovered": len(all_links),
        "entities_found": len(all_entities),
        "relations_found": len(all_relations),
        "entities_persisted": entities_persisted,
        "links_persisted": links_persisted,
        "structure": {
            "pages": len(page_map),
            "max_depth_reached": max(
                (p.get("depth", 0) for p in page_map.values()), default=0
            ),
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITY: CONTINUE ACQUISITION
# ═══════════════════════════════════════════════════════════════════════════

@capability(
    "fabric.web.continue",
    http_method="POST", http_path="/fabric/web/continue",
    http_tags=["fabric", "web", "acquisition"],
    memory="off",
    description="Continue a previous web acquisition from where it left off. "
                "Re-uses the same config (negative words, topic, etc.) and dataset. "
                "Input: acquisition_id (str!) OR source_id (str!) OR dataset_id (str!), "
                "additional_pages (int default 50). "
                "Output: same as fabric.web.acquire.",
)
async def cap_web_continue(
    acquisition_id: str = "",
    source_id:      str = "",
    dataset_id:     str = "",
    additional_pages: int = 50,
    trace_id=None,
) -> Dict:
    _ensure_acq_table()

    # Find the acquisition record
    conn = _sqlite_conn()
    row = None
    if acquisition_id:
        row = conn.execute(
            "SELECT * FROM fabric_acquisitions WHERE id=?", (acquisition_id,)
        ).fetchone()
    elif source_id or dataset_id:
        lookup = source_id or dataset_id
        row = conn.execute(
            "SELECT * FROM fabric_acquisitions WHERE dataset_id=? OR source_url LIKE ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (lookup, f"%{lookup}%")
        ).fetchone()

    if not row:
        return {"error": "Acquisition not found. Provide acquisition_id, source_id, or dataset_id."}

    acq = dict(row)
    config = json.loads(acq.get("config", "{}"))
    state = json.loads(acq.get("state", "{}"))

    # Re-run with same config but starting from visited set
    return await cap_web_acquire(
        url=acq["source_url"],
        dataset_id=acq["dataset_id"],
        max_pages=additional_pages,
        max_depth=config.get("max_depth", 4),
        negative_words=config.get("negative_words", ""),
        negative_urls=config.get("negative_urls", ""),
        topic=config.get("topic", ""),
        topic_dropoff=config.get("topic_dropoff", 3),
        extract_entities=True,
        fetch_content=True,
        same_domain=config.get("same_domain", True),
        tags=config.get("tags", ""),
    )


# ═══════════════════════════════════════════════════════════════════════════
# ENTITY GRAPH PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════

async def _persist_entity_graph(
    entities: Dict[str, Dict],
    relations: List[Dict],
    dataset_id: str,
) -> int:
    """Persist extracted entities and relations into both SQLite and Neo4j.

    Key change: entity→record links are stored per-record in the
    fabric_entity_mentions junction table so we can answer "which records
    mention this entity?" rather than only "which datasets?".
    """
    persisted = 0
    ts = now_iso()

    # SQLite persistence — entity rows + per-record mention rows
    try:
        conn = _sqlite_conn()
        for norm, ent in entities.items():
            ckey = _canonical_key(ent.get("name") or norm)
            eid = f"{ent['type']}:{hashlib.sha1(ckey.encode()).hexdigest()[:12]}"
            record_ids = list(ent.get("record_ids", set()))
            ds_list = list(ent.get("datasets", {dataset_id}))

            # Upsert the entity master row — merge datasets on conflict
            # First check if entity exists so we can merge dataset lists
            existing_row = conn.execute(
                "SELECT datasets FROM fabric_entities WHERE id = ?", (eid,)
            ).fetchone()
            if existing_row:
                try:
                    existing_ds = set(json.loads(existing_row["datasets"] or "[]"))
                except Exception:
                    existing_ds = set()
                merged_ds = list(existing_ds | set(ds_list))
            else:
                merged_ds = ds_list

            conn.execute(
                "INSERT INTO fabric_entities "
                "(id, name, type, normalised, mention_count, datasets, "
                "first_seen, last_seen, props) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "mention_count = mention_count + excluded.mention_count, "
                "datasets = ?, "
                "last_seen = excluded.last_seen, "
                "props = excluded.props",
                (eid, ent["name"], ent["type"], ent.get("normalised", norm),
                 ent.get("mention_count", 1),
                 json.dumps(ds_list),
                 ts, ts,
                 json.dumps({
                     "record_ids": record_ids[:50],
                     "description": ent.get("description", ""),
                     "aliases": sorted(ent.get("aliases", []))[:25],
                     "attributes": ent.get("attributes", {}),
                     "facts": ent.get("facts", [])[:20],
                 }),
                 json.dumps(merged_ds))
            )

            # Write per-record mention rows
            for rid in record_ids[:200]:
                conn.execute(
                    "INSERT OR IGNORE INTO fabric_entity_mentions "
                    "(entity_id, record_id, dataset_id, created_at) "
                    "VALUES (?,?,?,?)",
                    (eid, rid, dataset_id, ts)
                )

            persisted += 1

        # Persist entity-entity relations to SQLite
        for rel in relations[:300]:
            from_ck = _canonical_key(rel["from_name"])
            to_ck = _canonical_key(rel["to_name"])
            from_eid = f"{rel['from_type']}:{hashlib.sha1(from_ck.encode()).hexdigest()[:12]}"
            to_eid = f"{rel['to_type']}:{hashlib.sha1(to_ck.encode()).hexdigest()[:12]}"
            conn.execute(
                "INSERT OR REPLACE INTO fabric_entity_relations "
                "(from_id, to_id, rel, props, dataset_id, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (from_eid, to_eid, rel["rel"],
                 json.dumps({"distance": rel.get("distance", 0),
                             "from_name": rel["from_name"],
                             "to_name": rel["to_name"],
                             "context": rel.get("context", ""),
                             "score": rel.get("score", 0)}),
                 dataset_id, ts)
            )

        conn.commit()
    except Exception as e:
        log.warning("entity sqlite: %s", e)

    # Neo4j persistence
    graph = _get_graph()
    if graph and graph.available:
        try:
            # Make sure the Dataset node exists for HAS_ENTITY linking
            await graph.upsert_node("Dataset", dataset_id, {"id": dataset_id})

            dataset_entity_edges = []
            mention_edges = []

            # Upsert entity nodes
            for norm, ent in entities.items():
                ckey = _canonical_key(ent.get("name") or norm)
                eid = f"{ent['type']}:{hashlib.sha1(ckey.encode()).hexdigest()[:12]}"
                await graph.upsert_node("Entity", eid, {
                    "name": ent["name"],
                    "type": ent["type"],
                    "normalised": norm,
                    "canonical": ckey,
                    "mention_count": ent.get("mention_count", 1),
                    "dataset_id": dataset_id,
                    "description": ent.get("description", ""),
                    "aliases": sorted(ent.get("aliases", []))[:25],
                    "facts": ent.get("facts", [])[:20],
                })

                # Link to mentioning records — Entity→FabricRecord per actual record
                for rid in list(ent.get("record_ids", set()))[:50]:
                    mention_edges.append({
                        "from_label": "Entity", "from_id": eid,
                        "to_label":   "FabricRecord", "to_id": rid,
                        "rel":        "MENTIONED_IN",
                        "props":      {"dataset_id": dataset_id},
                    })

                # Link from every dataset this entity appears in
                for ds in ent.get("datasets", {dataset_id}):
                    if ds:
                        dataset_entity_edges.append({
                            "from_label": "Dataset", "from_id": ds,
                            "to_label":   "Entity",  "to_id":   eid,
                            "rel":        "HAS_ENTITY",
                            "props":      {"mention_count": ent.get("mention_count", 1)},
                        })

            if mention_edges:
                await graph.link_many(mention_edges)
            if dataset_entity_edges:
                await graph.link_many(dataset_entity_edges)

            # Create entity-entity relationship edges
            edges = []
            for rel in relations[:300]:
                from_ck = _canonical_key(rel["from_name"])
                to_ck = _canonical_key(rel["to_name"])
                from_eid = f"{rel['from_type']}:{hashlib.sha1(from_ck.encode()).hexdigest()[:12]}"
                to_eid = f"{rel['to_type']}:{hashlib.sha1(to_ck.encode()).hexdigest()[:12]}"
                edges.append({
                    "from_label": "Entity", "from_id": from_eid,
                    "to_label": "Entity", "to_id": to_eid,
                    "rel": rel["rel"],
                    "props": {"distance": rel.get("distance", 0),
                              "context": rel.get("context", ""),
                              "score": rel.get("score", 0)},
                })
            if edges:
                await graph.link_many(edges)
        except Exception as e:
            log.warning("entity neo4j: %s", e)

    return persisted


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITY: ENTITY GRAPH QUERY
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# LLM-ASSISTED EXTRACTION + AGENTIC STEERING  (optional augmentation)
# When enabled, an LLM extracts typed triples (with entity descriptions and
# relation context) which are MERGED with the heuristic output. "Agentic
# steering" then runs one or more critique/refine passes over the merged graph
# — merging duplicate entities, correcting types, adding missed high-value
# relations and pruning spurious ones — guided by an optional `focus` directive.
# ═══════════════════════════════════════════════════════════════════════════

_ENTITY_LLM_SYS = (
    "You are a precise knowledge-graph extraction engine. Read the TEXT and "
    "identify ONLY entities explicitly named or clearly implied, and ONLY "
    "relationships directly stated or unambiguously implied. Use the most "
    "specific UPPER_SNAKE relation verb (FOUNDED, LEADS, WORKS_FOR, REPORTS_TO, "
    "ACQUIRED, SUBSIDIARY_OF, PART_OF, OWNS, LOCATED_IN, HEADQUARTERED_IN, "
    "BORN_IN, RELEASED, DEVELOPED, USES, BUILT_WITH, AUTHORED, PUBLISHED_BY, "
    "PARTNERED_WITH, INVESTED_IN, COMPETES_WITH, REGULATED_BY, MEMBER_OF, "
    "HAS_ROLE) - never default to CO_OCCURS or RELATED_TO. Give each entity a "
    "concise one-sentence description grounded in the text, and each relation a "
    "short context phrase from the text. Never invent facts. Output strict JSON "
    "only - no prose, no markdown fences."
)

_STEER_SYS = (
    "You are a knowledge-graph editor. You are given a draft graph (entities and "
    "relations) extracted from a document. Improve its QUALITY: merge duplicate "
    "or alias entities, correct wrong entity types, add high-value relations that "
    "are clearly implied but missing, and remove spurious or vague relations. Be "
    "conservative - act only on clear improvements. Output strict JSON only."
)


def _llm_json(raw: str):
    """Best-effort parse of an LLM JSON response."""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s).strip()
    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b != -1 and b > a:
        s = s[a:b + 1]
    s = re.sub(r",\s*([}\]])", r"\1", s)  # tolerate trailing commas
    try:
        return json.loads(s)
    except Exception:
        return None


async def _llm_call(prompt: str, system: str, timeout: int = 90) -> str:
    """Call the local LLM. Prefers ollama_generate (json mode); falls back to
    the llm.generate capability. Returns '' on any failure."""
    try:
        from Vera.Orchestration.capability_orchestration import ollama_generate
        out = await asyncio.wait_for(
            ollama_generate(prompt, system=system, json_mode=True), timeout=timeout)
        if out:
            return out
    except Exception as e:
        log.debug("ollama_generate unavailable: %s", e)
    try:
        from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY
        fn = (CAPABILITY_REGISTRY.get("llm.generate") or {}).get("func")
        if fn:
            r = await asyncio.wait_for(
                fn(prompt=prompt, system=system, trace_id=None), timeout=timeout)
            if isinstance(r, dict):
                return r.get("text") or r.get("response") or ""
            return str(r or "")
    except Exception as e:
        log.debug("llm.generate fallback failed: %s", e)
    return ""


def _map_llm_type(t: str) -> str:
    t = (t or "").strip().lower()
    return {
        "org": "organisation", "organization": "organisation",
        "company": "organisation", "people": "person", "person": "person",
        "tech": "technology", "technology": "technology", "tool": "technology",
        "software": "technology", "product": "product", "place": "location",
        "location": "location", "geo": "location", "event": "event",
        "date": "date", "time": "date", "concept": "concept",
        "work": "work", "role": "role",
    }.get(t, t or "named_entity")


def _dedup_relations(relations: List[Dict]) -> List[Dict]:
    """Keep the best-scoring relation per (from, to, rel)."""
    best: Dict[tuple, Dict] = {}
    for r in relations:
        f = _normalise_entity(r.get("from_name", ""))
        to = _normalise_entity(r.get("to_name", ""))
        if not f or not to:
            continue
        key = (f, to, r.get("rel"))
        if key not in best or r.get("score", 0) > best[key].get("score", 0):
            best[key] = r
    return sorted(best.values(), key=lambda r: -r.get("score", 0))


async def _llm_extract(text: str, focus: str = "") -> Tuple[List[Dict], List[Dict]]:
    """LLM typed-triple extraction. Returns (entities, relations) in the SAME
    shape the heuristic engine produces, so they merge uniformly."""
    text = (text or "")[:3500]
    if not text.strip():
        return [], []
    focus_line = (f"\nFOCUS: prioritise {focus.strip()}.\n" if focus.strip() else "")
    prompt = (
        "Extract entities and their relationships from the TEXT below."
        + focus_line +
        '\nReturn ONLY this JSON (no other text):\n'
        '{"entities":[{"name":"exact name","type":"person|organisation|'
        'technology|product|location|event|concept|date|work|role",'
        '"description":"one sentence from the text"}],'
        '"relations":[{"from":"entity name","to":"entity name",'
        '"rel":"UPPER_SNAKE_VERB","context":"short phrase from text"}]}\n'
        'Every name in relations must appear in entities.\n\n'
        'TEXT:\n"""' + text + '"""'
    )
    raw = await _llm_call(prompt, _ENTITY_LLM_SYS)
    data = _llm_json(raw)
    if not isinstance(data, dict):
        return [], []
    ents: List[Dict] = []
    name_type: Dict[str, tuple] = {}
    for e in (data.get("entities") or [])[:60]:
        if not isinstance(e, dict):
            continue
        nm = str(e.get("name") or "").strip()
        if not nm or len(nm) > 90:
            continue
        ty = _map_llm_type(e.get("type"))
        if ty == "named_entity" and _looks_org(nm):
            ty = "organisation"
        norm = _normalise_entity(nm)
        if not norm:
            continue
        pos = text.find(nm)
        ents.append({"name": nm, "type": ty, "normalised": norm,
                     "position": pos if pos >= 0 else 0, "context": "",
                     "confidence": 0.8,
                     "description": str(e.get("description") or "").strip()[:300],
                     "source": "llm"})
        name_type[nm.lower()] = (nm, ty)
    rels: List[Dict] = []
    for r in (data.get("relations") or [])[:120]:
        if not isinstance(r, dict):
            continue
        a = str(r.get("from") or "").strip()
        b = str(r.get("to") or "").strip()
        if not a or not b or a.lower() == b.lower():
            continue
        rel = re.sub(r"[^A-Z_]", "", str(r.get("rel") or "RELATED_TO")
                     .upper().replace(" ", "_").replace("-", "_")) or "RELATED_TO"
        an, at = name_type.get(a.lower(), (a, "named_entity"))
        bn, bt = name_type.get(b.lower(), (b, "named_entity"))
        rels.append({"from_name": an, "from_type": at, "to_name": bn,
                     "to_type": bt, "rel": rel, "score": 0.8, "distance": 0,
                     "cue": "llm",
                     "context": str(r.get("context") or "").strip()[:160]})
    return ents, rels


def _graph_summary(entities: Dict[str, Dict], relations: List[Dict],
                   max_e: int = 60, max_r: int = 80) -> str:
    """Compact textual rendering of the current graph for a steering prompt."""
    es = sorted(entities.values(), key=lambda e: -e.get("mention_count", 1))[:max_e]
    elines = [f"- {e['name']} [{e['type']}]"
              + (f": {e['description']}" if e.get("description") else "")
              for e in es]
    rlines = [f"- {r['from_name']} --{r['rel']}--> {r['to_name']}"
              for r in relations[:max_r]]
    return ("ENTITIES:\n" + "\n".join(elines)
            + "\n\nRELATIONS:\n" + "\n".join(rlines))


async def _llm_steer(entities: Dict[str, Dict], relations: List[Dict],
                     focus: str = "", records: List[Dict] = None) -> Dict:
    """One agentic refine pass over the aggregate graph, grounded in source
    excerpts. Returns a corrections dict {merge, retype, rename, add_relations,
    drop_relations, set_description, add_attributes}."""
    if not entities:
        return {}
    focus_line = (f"\nFOCUS DIRECTIVE: {focus.strip()}. Bias every decision "
                  f"toward this focus; drop anything irrelevant to it.\n"
                  if focus.strip() else "")
    snips = ""
    if records:
        parts = []
        for r in records[:8]:
            txt = (r.get("text") or "")
            if txt:
                parts.append(((r.get("title") or "") + ": " + txt[:280]).strip())
        if parts:
            snips = ("\n\nSOURCE EXCERPTS (ground every correction in these — do "
                     "not invent):\n- " + "\n- ".join(parts))
    prompt = (
        "Here is a draft knowledge graph extracted from sources." + focus_line +
        "\n\n" + _graph_summary(entities, relations) + snips +
        "\n\nImprove it. Priorities: (1) merge genuine duplicates/aliases; "
        "(2) fix wrong types; (3) REPLACE vague RELATED_TO/CO_OCCURS with a "
        "specific directed relationship when the excerpts justify one; "
        "(4) add well-supported missing relationships; (5) drop spurious ones; "
        "(6) give each important entity a one-line factual description and key "
        "attributes from the excerpts.\n"
        "Return ONLY this JSON:\n"
        '{"merge":[{"from":"duplicate name","into":"canonical name"}],'
        '"retype":[{"entity":"name","type":"correct type"}],'
        '"rename":[{"entity":"name","name":"canonical name"}],'
        '"add_relations":[{"from":"name","to":"name","rel":"UPPER_SNAKE",'
        '"context":"why"}],'
        '"drop_relations":[{"from":"name","to":"name"}],'
        '"set_description":[{"entity":"name","description":"1-2 factual sentences"}],'
        '"add_attributes":[{"entity":"name","attributes":{"key":"value"}}]}\n'
        "Use names exactly as they appear above. Empty arrays are fine."
    )
    raw = await _llm_call(prompt, _STEER_SYS, timeout=120)
    data = _llm_json(raw)
    return data if isinstance(data, dict) else {}


def _apply_steer(entities: Dict[str, Dict], relations: List[Dict],
                 corrections: Dict, dataset_id: str):
    """Apply steering corrections to the aggregate collections.
    Returns (entities, relations, stats)."""
    stats = {"merged": 0, "retyped": 0, "renamed": 0, "added": 0, "dropped": 0,
             "described": 0, "attributed": 0}
    by_name = {e["name"].lower(): norm for norm, e in entities.items()}
    by_name.update({norm: norm for norm in entities})

    def _resolve(name):
        if not name:
            return None
        n = str(name).lower()
        if n in by_name:
            return by_name[n]
        nn = _normalise_entity(name)
        return nn if nn in entities else None

    for m in (corrections.get("merge") or [])[:40]:
        if not isinstance(m, dict):
            continue
        src = _resolve(m.get("from")); dst = _resolve(m.get("into"))
        if not src or not dst or src == dst:
            continue
        se, de = entities.get(src), entities.get(dst)
        if not se or not de:
            continue
        de["mention_count"] = de.get("mention_count", 1) + se.get("mention_count", 1)
        de.setdefault("record_ids", set()).update(se.get("record_ids", set()))
        de.setdefault("datasets", set()).update(se.get("datasets", set()))
        if not de.get("description") and se.get("description"):
            de["description"] = se["description"]
        for r in relations:
            if _normalise_entity(r["from_name"]) == src:
                r["from_name"] = de["name"]; r["from_type"] = de["type"]
            if _normalise_entity(r["to_name"]) == src:
                r["to_name"] = de["name"]; r["to_type"] = de["type"]
        entities.pop(src, None)
        by_name[str(m.get("from") or "").lower()] = dst
        stats["merged"] += 1

    for rt in (corrections.get("retype") or [])[:60]:
        if not isinstance(rt, dict):
            continue
        tgt = _resolve(rt.get("entity")); nt = _map_llm_type(rt.get("type"))
        if tgt and nt and entities.get(tgt):
            entities[tgt]["type"] = nt
            stats["retyped"] += 1

    for rn in (corrections.get("rename") or [])[:40]:
        if not isinstance(rn, dict):
            continue
        tgt = _resolve(rn.get("entity")); newname = str(rn.get("name") or "").strip()
        if tgt and newname and entities.get(tgt):
            entities[tgt]["name"] = newname[:120]
            stats["renamed"] += 1

    drop_keys = set()
    for d in (corrections.get("drop_relations") or [])[:80]:
        if not isinstance(d, dict):
            continue
        f = _normalise_entity(d.get("from") or ""); tt = _normalise_entity(d.get("to") or "")
        if f and tt:
            drop_keys.add((f, tt)); drop_keys.add((tt, f))
    if drop_keys:
        before = len(relations)
        relations[:] = [r for r in relations
                        if (_normalise_entity(r["from_name"]),
                            _normalise_entity(r["to_name"])) not in drop_keys]
        stats["dropped"] = before - len(relations)

    for a in (corrections.get("add_relations") or [])[:80]:
        if not isinstance(a, dict):
            continue
        fsrc = _resolve(a.get("from")); tsrc = _resolve(a.get("to"))
        if not fsrc or not tsrc or fsrc == tsrc:
            continue
        fe, te = entities.get(fsrc), entities.get(tsrc)
        if not fe or not te:
            continue
        rel = re.sub(r"[^A-Z_]", "", str(a.get("rel") or "RELATED_TO")
                     .upper().replace(" ", "_").replace("-", "_")) or "RELATED_TO"
        relations.append({"from_name": fe["name"], "from_type": fe["type"],
                          "to_name": te["name"], "to_type": te["type"],
                          "rel": rel, "score": 0.75, "distance": 0,
                          "cue": "llm_steer",
                          "context": str(a.get("context") or "").strip()[:160]})
        stats["added"] += 1

    for sd in (corrections.get("set_description") or [])[:80]:
        if not isinstance(sd, dict):
            continue
        tgt = _resolve(sd.get("entity"))
        desc = str(sd.get("description") or "").strip()
        if tgt and desc and entities.get(tgt):
            entities[tgt]["description"] = desc[:600]
            stats["described"] += 1

    for aa in (corrections.get("add_attributes") or [])[:80]:
        if not isinstance(aa, dict):
            continue
        tgt = _resolve(aa.get("entity"))
        attrs = aa.get("attributes")
        if tgt and isinstance(attrs, dict) and entities.get(tgt):
            entities[tgt].setdefault("attributes", {}).update(
                {str(k)[:40]: str(v)[:160] for k, v in attrs.items()})
            stats["attributed"] += 1

    return entities, relations, stats

# ═══════════════════════════════════════════════════════════════════════════
# ALIAS RESOLUTION + ENTITY PROFILES + SECONDARY RELATIONSHIP INFERENCE
# ═══════════════════════════════════════════════════════════════════════════

_LOCATION_TYPES = {"location", "city", "country", "place", "building",
                   "landmark", "geographic_feature", "geographic_location",
                   "gpe", "facility", "region", "state", "county"}


_EXCLUSIVE_FAMILIES = {"person", "location", "organisation", "event",
                       "date", "money"}


def _type_family(t: str) -> str:
    """Group near-equivalent types so alias clustering only merges compatible
    entities (all place-like types -> 'location'; everything else by itself)."""
    t = (t or "").lower()
    if t in _LOCATION_TYPES or t.endswith("_location") or t.endswith("_city"):
        return "location"
    if t in ("organisation", "organization", "company", "corporation",
             "government_agency"):
        return "organisation"
    return t or "named_entity"


def _resolve_aliases(entities: Dict[str, Dict], relations: List[Dict]):
    """Cluster surface variants of the same entity (by canonical key + type
    family), merge their mentions/records/descriptions, collect aliases, pick a
    canonical display name, and repoint + dedup relations. Returns
    (entities_by_canonical_key, relations)."""
    from collections import Counter
    _GENERIC = ("named_entity", "concept", "acronym", "")
    # 1) group by canonical key
    by_ck: Dict[str, List[Dict]] = {}
    for norm, e in entities.items():
        by_ck.setdefault(_canonical_key(e.get("name") or norm), []).append(e)
    # 2) within each key, merge across types EXCEPT where two mutually-exclusive
    #    real-world families collide. Only person/location/organisation/event/
    #    date/money are "exclusive" (a person and a place sharing a name must
    #    stay apart). Everything else — character, pokemon, product, work,
    #    technology, generic named_entity … — is freely merged onto one node so
    #    "Pikachu" [character] / [pokemon] / [named_entity] become ONE entity.
    clusters: Dict[str, List[Dict]] = {}
    for ck, group in by_ck.items():
        excl: Dict[str, List[Dict]] = {}
        rest: List[Dict] = []
        for e in group:
            t_ = e.get("type", "")
            fam = _type_family(t_)
            if t_ not in _GENERIC and fam in _EXCLUSIVE_FAMILIES:
                excl.setdefault(fam, []).append(e)
            else:
                rest.append(e)
        if len(excl) <= 1:
            fam = next(iter(excl)) if excl else "open"
            members = (excl[fam] if excl else []) + rest
            clusters[ck + "|" + fam] = members
        else:
            biggest = max(excl, key=lambda f: sum(m.get("mention_count", 1)
                                                  for m in excl[f]))
            for f, mem in excl.items():
                clusters[ck + "|" + f] = mem + (rest if f == biggest else [])

    new_entities: Dict[str, Dict] = {}
    rename: Dict[str, str] = {}
    for _ckfam, members in clusters.items():
        if not members:
            continue
        members.sort(key=lambda m: (-m.get("mention_count", 1),
                                    -len(m.get("name", ""))))
        head = members[0]
        aliases: set = set()
        rids: set = set()
        dss: set = set()
        facts: list = []
        attrs: dict = {}
        desc = ""
        mention = 0
        conf = 0.0
        type_counts: Counter = Counter()
        for m in members:
            mention += m.get("mention_count", 1)
            rids |= set(m.get("record_ids", set()))
            dss |= set(m.get("datasets", set()))
            conf = max(conf, m.get("confidence", 0.5))
            if m.get("name"):
                aliases.add(m["name"])
            for a in (m.get("aliases") or []):
                aliases.add(a)
            if not desc and m.get("description"):
                desc = m["description"]
            for f in (m.get("facts") or []):
                if f not in facts:
                    facts.append(f)
            attrs.update(m.get("attributes") or {})
            mt = m.get("type", "")
            if mt and mt not in ("named_entity", "concept", "acronym"):
                type_counts[mt] += m.get("mention_count", 1)
        best_type = (type_counts.most_common(1)[0][0]
                     if type_counts else head.get("type", "named_entity"))
        disp = head["name"]
        aliases.discard(disp)
        ckey = _canonical_key(disp)
        ekey = _normalise_entity(disp) + "|" + _type_family(best_type)
        new_entities[ekey] = {
            "name": disp, "type": best_type,
            "normalised": _normalise_entity(disp), "canonical": ckey,
            "mention_count": mention, "record_ids": rids, "datasets": dss,
            "confidence": conf, "description": desc,
            "aliases": sorted(aliases)[:30], "facts": facts[:30],
            "attributes": attrs,
        }
        for m in members:
            rename[(m.get("name") or "").lower()] = disp
            rename[_normalise_entity(m.get("name") or "")] = disp

    # ── Coreference: fold a SHORT name (e.g. "Ash" [character]) into a unique
    #    longer entity that contains it (e.g. "Ash Ketchum" [named_entity]),
    #    across compatible type families. Guarded against ambiguity. ──────────
    _PERSON_FAM = {"person", "character"}

    def _fam_compat(fa, fb):
        if fa == fb:
            return True
        if "named_entity" in (fa, fb):
            return True
        return fa in _PERSON_FAM and fb in _PERSON_FAM

    items = [e for e in new_entities.values()]
    # process shortest names first so they fold into longer ones
    for short in sorted(items, key=lambda e: len(e["normalised"].split())):
        if short.get("_merged"):
            continue
        st = short["normalised"].split()
        if not st or len(st) > 2 or len(st[0]) < 3:
            continue
        sfam = _type_family(short["type"])
        cands = []
        for lng in items:
            if lng is short or lng.get("_merged"):
                continue
            lt = lng["normalised"].split()
            if len(lt) <= len(st):
                continue
            if (st == lt[:len(st)] or set(st).issubset(set(lt))) and \
                    _fam_compat(sfam, _type_family(lng["type"])):
                cands.append(lng)
        if len(cands) != 1:
            continue  # ambiguous (or none) — do not merge
        lng = cands[0]
        lng["mention_count"] = lng.get("mention_count", 1) + short.get("mention_count", 1)
        lng.setdefault("record_ids", set()).update(short.get("record_ids", set()))
        lng.setdefault("datasets", set()).update(short.get("datasets", set()))
        al = set(lng.get("aliases", []))
        al.add(short["name"]); al.update(short.get("aliases", []))
        al.discard(lng["name"])
        lng["aliases"] = sorted(al)[:30]
        if not lng.get("description") and short.get("description"):
            lng["description"] = short["description"]
        # prefer the more specific type (character/person beats named_entity)
        if lng["type"] in ("named_entity", "concept", "acronym") and \
                short["type"] not in ("named_entity", "concept", "acronym"):
            lng["type"] = short["type"]
        fl = lng.setdefault("facts", [])
        for f in short.get("facts", []):
            if f not in fl:
                fl.append(f)
        lng.setdefault("attributes", {}).update(short.get("attributes", {}))
        short["_merged"] = True
        rename[(short.get("name") or "").lower()] = lng["name"]
        rename[short["normalised"]] = lng["name"]
    # drop merged entries
    new_entities = {k: v for k, v in new_entities.items() if not v.get("_merged")}

    def _disp(name):
        return (rename.get((name or "").lower())
                or rename.get(_normalise_entity(name or "")) or name)

    ck_index: Dict[str, Dict] = {}
    for e in new_entities.values():
        ck_index[e["canonical"]] = e
    for r in relations:
        r["from_name"] = _disp(r.get("from_name", ""))
        r["to_name"] = _disp(r.get("to_name", ""))
        fe = ck_index.get(_canonical_key(r["from_name"]))
        te = ck_index.get(_canonical_key(r["to_name"]))
        if fe:
            r["from_type"] = fe["type"]
        if te:
            r["to_type"] = te["type"]
    relations = [r for r in relations
                 if _canonical_key(r["from_name"]) != _canonical_key(r["to_name"])]
    return new_entities, _dedup_relations(relations)


async def _llm_profile(name: str, etype: str, contexts: List[str],
                       focus: str = "") -> Dict:
    """Build a rich factual profile of one entity from excerpts that mention it."""
    ctx = "\n---\n".join((c or "")[:500] for c in contexts[:6])[:3200]
    if not ctx.strip():
        return {}
    sys = ("You build a concise, factual profile of a single entity using ONLY "
           "the supplied excerpts. Never invent facts. Output strict JSON only.")
    prompt = (
        f'ENTITY: "{name}" (current type: {etype})\n'
        + (f"FOCUS: {focus}\n" if focus else "")
        + 'From the EXCERPTS, return ONLY this JSON:\n'
        '{"type":"most precise type, free-form lowercase",'
        '"description":"2-3 sentence factual summary",'
        '"aliases":["other names or spellings used for this entity"],'
        '"attributes":{"key":"value"},'
        '"facts":["short standalone factual statements"]}\n'
        "Include only facts grounded in the excerpts.\n\n"
        f"EXCERPTS:\n{ctx}"
    )
    data = _llm_json(await _llm_call(prompt, sys, timeout=90))
    if not isinstance(data, dict):
        return {}
    out: Dict = {}
    if data.get("type"):
        out["type"] = _map_gliner_type(str(data["type"]))
    if data.get("description"):
        out["description"] = str(data["description"])[:600]
    al = data.get("aliases")
    if isinstance(al, list):
        out["aliases"] = [str(a)[:80] for a in al if a][:20]
    at = data.get("attributes")
    if isinstance(at, dict):
        out["attributes"] = {str(k)[:40]: str(v)[:160] for k, v in at.items()}
    fa = data.get("facts")
    if isinstance(fa, list):
        out["facts"] = [str(f)[:220] for f in fa if f][:20]
    return out


async def _llm_relate_entities(entities: Dict[str, Dict],
                               existing: List[Dict], focus: str = "") -> List[Dict]:
    """Secondary pass: infer relationships ACROSS the whole entity roster (not
    just adjacent mentions), grounded in the entity types + profiles."""
    ents = sorted(entities.values(), key=lambda e: -e.get("mention_count", 1))[:60]
    if len(ents) < 2:
        return []
    roster = "\n".join(
        f'- {e["name"]} [{e["type"]}]'
        + (f': {e["description"]}' if e.get("description") else "")
        for e in ents)
    have = {(_canonical_key(r.get("from_name", "")),
             _canonical_key(r.get("to_name", ""))) for r in existing}
    sys = ("You map relationships BETWEEN the listed entities. Assert only "
           "well-supported relationships. Use specific UPPER_SNAKE verbs "
           "(LEADS, FOUNDED, LOCATED_IN, PART_OF, OWNS, WORKS_FOR, CREATED, "
           "USES, ALLIED_WITH, RIVAL_OF, MEMBER_OF, ...). Output strict JSON only.")
    prompt = ("ENTITIES:\n" + roster
              + (f"\n\nFOCUS: {focus}" if focus else "")
              + '\n\nReturn ONLY: {"relations":[{"from":"name","to":"name",'
              '"rel":"UPPER_SNAKE","context":"brief justification"}]}\n'
              "Use entity names exactly as listed. Do not relate an entity to itself.")
    data = _llm_json(await _llm_call(prompt, sys, timeout=120))
    name_idx = {e["name"].lower(): e for e in ents}
    out: List[Dict] = []
    for r in (data or {}).get("relations", [])[:150]:
        if not isinstance(r, dict):
            continue
        a = str(r.get("from") or "").strip()
        b = str(r.get("to") or "").strip()
        if not a or not b or a.lower() == b.lower():
            continue
        ea = name_idx.get(a.lower()); eb = name_idx.get(b.lower())
        if not ea or not eb:
            continue
        if (_canonical_key(a), _canonical_key(b)) in have:
            continue
        rel = re.sub(r"[^A-Z_]", "", str(r.get("rel") or "RELATED_TO")
                     .upper().replace(" ", "_").replace("-", "_")) or "RELATED_TO"
        out.append({"from_name": ea["name"], "from_type": ea["type"],
                    "to_name": eb["name"], "to_type": eb["type"], "rel": rel,
                    "score": 0.7, "distance": 0, "cue": "llm_relate",
                    "context": str(r.get("context") or "")[:160]})
    return out


async def _llm_describe_relations(relations: List[Dict], focus: str = "") -> int:
    """Upgrade weak/generic edges (RELATED_TO, CO_OCCURS, ASSOCIATED_WITH) into
    SPECIFIC, explained relationships using each edge's saved context. Mutates
    relations in place; returns the number upgraded."""
    weak = [r for r in relations
            if r.get("rel") in ("RELATED_TO", "RELATED", "CO_OCCURS",
                                "ASSOCIATED_WITH")]
    if not weak:
        return 0
    changed = 0
    for i in range(0, len(weak), 25):
        batch = weak[i:i + 25]
        lines = [
            f'{j}. "{r.get("from_name","")}" -> "{r.get("to_name","")}"'
            f' | context: {(r.get("context") or "")[:160]}'
            for j, r in enumerate(batch)]
        sys = ("You assign a SPECIFIC directed relationship type and a one-line "
               "reason for each entity pair, grounded in the given context. "
               "Output strict JSON only.")
        prompt = (
            "For each numbered pair, give the precise relationship FROM the first "
            "entity TO the second as an UPPER_SNAKE verb (e.g. FOUNDED, LOCATED_IN, "
            "WORKS_FOR, PART_OF, OWNS, CREATED, USES, MEMBER_OF, ALLIED_WITH, "
            "PARENT_OF, PRODUCES, SUCCEEDED_BY, DEFEATED). Only keep RELATED_TO if "
            "the context shows nothing more specific. "
            + (f"FOCUS: {focus}. " if focus else "")
            + 'Return ONLY {"items":[{"i":0,"rel":"UPPER_SNAKE","why":"short reason"}]}'
            + "\n\n" + "\n".join(lines))
        data = _llm_json(await _llm_call(prompt, sys, timeout=90))
        for it in (data or {}).get("items", []):
            if not isinstance(it, dict):
                continue
            try:
                idx = int(it.get("i"))
            except Exception:
                continue
            if not (0 <= idx < len(batch)):
                continue
            rel = re.sub(r"[^A-Z_]", "", str(it.get("rel") or "")
                         .upper().replace(" ", "_").replace("-", "_"))
            why = str(it.get("why") or "").strip()
            if rel and rel != "RELATED_TO":
                batch[idx]["rel"] = rel
                batch[idx]["cue"] = "llm_describe"
                changed += 1
            if why:
                batch[idx]["context"] = why[:200]
    return changed




@capability(
    "fabric.entity_graph.extract",
    http_method="POST", http_path="/fabric/entity_graph/extract",
    http_tags=["fabric", "graph", "entity"],
    memory="on",
    description="Extract a second-order entity graph from a dataset's records. "
                "The heuristic engine identifies people, organisations, locations, "
                "dates, technologies, products and code symbols and infers typed, "
                "directed relations (FOUNDED, LEADS, LOCATED_IN, USES, ...) from the "
                "verb/preposition cues between adjacent mentions. "
                "Optional LLM augmentation (use_llm) extracts typed triples with "
                "entity descriptions + relation context and merges them in. "
                "Optional agentic steering (llm_steering) runs critique/refine passes "
                "that merge duplicate entities, fix types, add missed relations and "
                "prune spurious ones, guided by an optional `focus` directive. "
                "Alias resolution (alias_resolution, default ON) structurally collapses "
                "surface variants ('the City of Cherrygrove' / 'Cherry Grove City' / "
                "'Cherrygrove') into one node with aliases. Entity types are OPEN "
                "vocabulary (whatever the NER/LLM emits). llm_profiles builds a rich "
                "per-entity profile (description, aliases, attributes, facts); "
                "llm_relationships runs a secondary whole-roster pass inferring "
                "relationships between extracted entities. "
                "Input: dataset_id (str!), limit (int default 100), "
                "content_type (text|code|web default text), persist (bool default True), "
                "use_llm (bool default False), llm_steering (bool default False), "
                "steering_rounds (int 1-3 default 1), focus (str — domain/goal to bias "
                "extraction & steering), llm_max_records (int default 40 — cap on per-record "
                "LLM calls). "
                "Output: {entities, relations, persisted, dataset_id, steering}.",
)
async def cap_entity_graph_extract(
    dataset_id:      str,
    limit:           int  = 100,
    content_type:    str  = "text",
    persist:         bool = True,
    use_llm:         bool = False,
    llm_steering:    bool = False,
    steering_rounds: int  = 1,
    focus:           str  = "",
    llm_max_records: int  = 40,
    alias_resolution: bool = True,
    llm_profiles:    bool = False,
    llm_relationships: bool = False,
    describe_relations: bool = True,
    profile_top:     int  = 25,
    trace_id=None,
) -> Dict:
    if not dataset_id:
        return {"error": "dataset_id required"}
    limit = max(1, min(1000, limit))

    async def _emit(stage, **kw):
        try:
            await emit_event({"type": "fabric.entity_graph.progress",
                              "dataset_id": dataset_id, "stage": stage, **kw})
        except Exception:
            pass

    await _emit("loading", message=f"Loading up to {limit} records from {dataset_id}")

    # Load records
    from Vera.Orchestration.fabric.data_fabric import _sqlite_query
    records = []
    offset = 0
    page_sz = 100
    while len(records) < limit:
        batch = await _sqlite_query(dataset_id=dataset_id, limit=min(page_sz, limit - len(records)), offset=offset)
        if not batch:
            break
        records.extend(batch)
        if len(batch) < page_sz:
            break
        offset += page_sz

    if not records:
        return {"error": f"No records in {dataset_id}"}

    await _emit("extracting", count=len(records), use_llm=bool(use_llm))

    all_entities: Dict[str, Dict] = {}
    all_relations: List[Dict] = []

    def _absorb(entities, relations, rid):
        """Merge one record's extraction into the aggregate, preferring more
        specific types and the first non-empty description per entity."""
        _generic = ("named_entity", "concept", "acronym")
        for ent in entities:
            norm = ent["normalised"]
            if norm in all_entities:
                cur = all_entities[norm]
                cur["mention_count"] += 1
                cur["record_ids"].add(rid)
                if cur["type"] in _generic and ent["type"] not in _generic:
                    cur["type"] = ent["type"]
                if not cur.get("description") and ent.get("description"):
                    cur["description"] = ent["description"]
                cur["confidence"] = max(cur.get("confidence", 0.5),
                                        ent.get("confidence", 0.5))
            else:
                all_entities[norm] = {
                    **ent,
                    "mention_count": 1,
                    "record_ids": {rid},
                    "datasets": {dataset_id},
                    "description": ent.get("description", ""),
                }
        all_relations.extend(relations)

    llm_used = 0
    for rec in records:
        text = (rec.get("text") or "")[:8000]
        rid = rec["id"]

        # heuristic engine — always runs, on every record
        h_ents = _extract_entities_from_text(text, content_type)
        h_rels = _extract_relationships_from_entities(h_ents, text)
        _absorb(h_ents, h_rels, rid)

        # optional LLM augmentation — capped to bound cost/latency
        if use_llm and llm_used < max(1, llm_max_records):
            try:
                l_ents, l_rels = await _llm_extract(text, focus)
                if l_ents or l_rels:
                    _absorb(l_ents, l_rels, rid)
                    llm_used += 1
            except Exception as e:
                log.debug("llm extract %s: %s", rid, e)

    all_relations = _dedup_relations(all_relations)

    # ── Alias resolution: collapse surface variants of the same entity ─────
    if alias_resolution and all_entities:
        before = len(all_entities)
        all_entities, all_relations = _resolve_aliases(all_entities, all_relations)
        await _emit("aliased", before=before, after=len(all_entities),
                    merged=before - len(all_entities))

    await _emit("scored", entities=len(all_entities),
                relations=len(all_relations), llm_records=llm_used)

    # ── Agentic steering: critique/refine the aggregate graph ──────────────
    steering = []
    if use_llm and llm_steering and all_entities:
        rounds = max(1, min(3, int(steering_rounds or 1)))
        for rnd in range(rounds):
            await _emit("steering", round=rnd + 1, message="LLM refining graph")
            corr = await _llm_steer(all_entities, all_relations, focus, records)
            if not corr:
                break
            all_entities, all_relations, st = _apply_steer(
                all_entities, all_relations, corr, dataset_id)
            steering.append(st)
            await _emit("steered", round=rnd + 1, **st)
            if not any(st.values()):
                break  # converged — nothing more to change
        all_relations = _dedup_relations(all_relations)

    # ── LLM entity profiles: rich per-entity description/attributes/facts ──
    profiled = 0
    if llm_profiles and all_entities:
        await _emit("profiling", message="building entity profiles")
        rid_text = {r["id"]: (r.get("text") or "") for r in records}
        top = sorted(all_entities.values(), key=lambda e: -e.get("mention_count", 1))
        for e in top[:max(1, min(80, int(profile_top or 25)))]:
            ctxs = []
            for rid in list(e.get("record_ids", []))[:8]:
                txt = rid_text.get(rid, "")
                if not txt:
                    continue
                pos = txt.lower().find(e["name"].lower())
                if pos < 0:
                    for al in e.get("aliases", []):
                        pos = txt.lower().find(al.lower())
                        if pos >= 0:
                            break
                if pos < 0:
                    pos = 0
                ctxs.append(txt[max(0, pos - 200):pos + 400])
            if not ctxs:
                continue
            try:
                prof = await _llm_profile(e["name"], e["type"], ctxs, focus)
            except Exception as ex:
                log.debug("profile %s: %s", e.get("name"), ex)
                continue
            if not prof:
                continue
            if prof.get("type") and e.get("type") in ("named_entity", "concept", ""):
                e["type"] = prof["type"]
            if prof.get("description"):
                e["description"] = prof["description"]
            if prof.get("aliases"):
                al = set(e.get("aliases", [])); al.update(prof["aliases"])
                al.discard(e["name"]); e["aliases"] = sorted(al)[:30]
            if prof.get("attributes"):
                e.setdefault("attributes", {}).update(prof["attributes"])
            if prof.get("facts"):
                fs = list(e.get("facts", []))
                for f in prof["facts"]:
                    if f not in fs:
                        fs.append(f)
                e["facts"] = fs[:30]
            profiled += 1
        await _emit("profiled", count=profiled)

    # ── Secondary inter-entity relationship inference (whole-roster) ───────
    secondary_added = 0
    if llm_relationships and len(all_entities) >= 2:
        await _emit("relating", message="inferring inter-entity relationships")
        try:
            secondary = await _llm_relate_entities(all_entities, all_relations, focus)
        except Exception as ex:
            secondary = []
            log.debug("relate: %s", ex)
        if secondary:
            all_relations.extend(secondary)
            all_relations = _dedup_relations(all_relations)
            secondary_added = len(secondary)
        await _emit("related", added=secondary_added)

    # ── Describe weak RELATED_TO/CO_OCCURS edges with a specific type + reason ─
    described = 0
    if use_llm and describe_relations and all_relations:
        await _emit("describing", message="describing weak relationships")
        try:
            described = await _llm_describe_relations(all_relations, focus)
        except Exception as ex:
            log.debug("describe relations: %s", ex)
        if described:
            all_relations = _dedup_relations(all_relations)
        await _emit("described", count=described)

    persisted = 0
    if persist and all_entities:
        persisted = await _persist_entity_graph(
            all_entities, all_relations, dataset_id
        )

    # Convert sets to lists for JSON serialisation
    entity_list = []
    for norm, ent in all_entities.items():
        entity_list.append({
            "name": ent["name"],
            "type": ent["type"],
            "normalised": ent.get("normalised", norm),
            "canonical": ent.get("canonical", ""),
            "description": ent.get("description", ""),
            "aliases": ent.get("aliases", []),
            "attributes": ent.get("attributes", {}),
            "facts": ent.get("facts", []),
            "confidence": ent.get("confidence", 0.5),
            "mention_count": ent["mention_count"],
            "record_ids": list(ent["record_ids"])[:20],
        })
    entity_list.sort(key=lambda e: -e["mention_count"])

    await _emit("done", entities=len(entity_list),
                relations=len(all_relations), persisted=persisted)

    return {
        "ok": True,
        "dataset_id": dataset_id,
        "entities": entity_list[:200],
        "relations": all_relations[:300],
        "entity_count": len(entity_list),
        "relation_count": len(all_relations),
        "persisted": persisted,
        "llm": {"used": bool(use_llm), "records": llm_used,
                "steering": steering, "profiled": profiled,
                "secondary_relations": secondary_added,
                "relations_described": described},
        "alias_resolution": bool(alias_resolution),
    }


def _ner_available() -> Dict:
    """Cheap importability probe (does NOT load models)."""
    avail = {"spacy": False, "gliner": False}
    try:
        import spacy  # type: ignore  # noqa
        avail["spacy"] = True
    except Exception:
        pass
    try:
        import gliner  # type: ignore  # noqa
        avail["gliner"] = True
    except Exception:
        pass
    return avail


@capability(
    "fabric.entity_graph.ner",
    http_method="POST", http_path="/fabric/entity_graph/ner",
    http_tags=["fabric", "graph", "entity", "nlp"],
    memory="off",
    description="Inspect and control the entity NER/NLP backend, and self-test it. "
                "GET/empty: report the ACTIVE backend (gliner|spacy|heuristic), which "
                "libraries are importable, the configured model names, and run a "
                "self-test extraction on a sample so you can SEE what it produces. "
                "Set backend (auto|gliner|spacy|heuristic) to switch at runtime "
                "(re-detects on next use). Optionally pass spacy_model / gliner_model "
                "to change the model, and sample to test your own text. "
                "NER is independent of and composes with the LLM augmentation in "
                "fabric.entity_graph.extract (use_llm / llm_steering). "
                "Output: {active_backend, configured, available, models, self_test}.",
)
async def cap_entity_graph_ner(
    backend: str = "", spacy_model: str = "", gliner_model: str = "",
    sample: str = "", trace_id=None,
) -> Dict:
    changed = False
    if spacy_model.strip():
        os.environ["FABRIC_NER_MODEL"] = spacy_model.strip(); changed = True
    if gliner_model.strip():
        os.environ["FABRIC_GLINER_MODEL"] = gliner_model.strip(); changed = True
    if backend.strip():
        b = backend.strip().lower()
        if b not in ("auto", "gliner", "spacy", "heuristic"):
            return {"error": "backend must be auto|gliner|spacy|heuristic"}
        os.environ["FABRIC_NER_BACKEND"] = b
        changed = True
    if changed:
        # force re-detection on next use
        _NER_STATE.update(init=False, kind="heuristic", obj=None)

    st = _ner_backend()  # triggers (re)detection + model load
    avail = _ner_available()
    test_text = (sample.strip() or
                 "Tim Cook, the CEO of Apple Inc, met Angela Merkel in Berlin to "
                 "discuss the Seacourt Tower of Oxford, a project built with Python "
                 "and funded by SoftBank in 2023.")
    try:
        ents = _extract_entities_from_text(test_text, "text")
    except Exception as e:
        ents = []
        log.debug("ner self-test: %s", e)
    # type histogram so the over/under-representation is obvious at a glance
    hist: Dict[str, int] = {}
    for e in ents:
        hist[e["type"]] = hist.get(e["type"], 0) + 1
    return {
        "active_backend": st["kind"],
        "configured": os.getenv("FABRIC_NER_BACKEND", "auto"),
        "available": avail,
        "models": {
            "spacy": os.getenv("FABRIC_NER_MODEL", "en_core_web_sm"),
            "gliner": os.getenv("FABRIC_GLINER_MODEL", "urchade/gliner_medium-v2.1"),
        },
        "gliner_labels": _gliner_labels(),
        "gliner_threshold": _gliner_threshold(),
        "self_test": {
            "text": test_text,
            "type_histogram": hist,
            "entities": [{"name": e["name"], "type": e["type"],
                          "confidence": e.get("confidence")} for e in ents],
        },
        "note": ("If active_backend is 'heuristic', install a model on the host: "
                 "`pip install gliner` (zero-shot, best) or "
                 "`pip install spacy && python -m spacy download en_core_web_sm`, "
                 "then set backend=auto. NER composes with LLM augmentation."),
    }



@capability(
    "fabric.entity_graph.consolidate",
    http_method="POST", http_path="/fabric/entity_graph/consolidate",
    http_tags=["fabric", "graph", "entity"], memory="off",
    streams=["fabric.entity_graph.progress"],
    description="In-tandem consolidation of a dataset's entity graph: surgically "
                "MERGES cross-type / alias duplicate nodes (e.g. 'Pikachu' as "
                "character + pokemon + named_entity -> one node) without a full "
                "rebuild, so it is safe to run repeatedly mid-crawl. Optionally "
                "(link=True) runs an LLM pass that ties related entities together "
                "with typed relations. "
                "Input: dataset_id (str!), link (bool), trace_id. "
                "Output: {ok, merged, clusters, linked}.",
)
async def cap_entity_graph_consolidate(dataset_id: str = "", link: bool = False,
                                       trace_id=None) -> Dict:
    if not dataset_id:
        return {"error": "dataset_id required"}
    from collections import Counter
    _GEN = {"named_entity", "concept", "acronym", ""}
    conn = _sqlite_conn()
    try:
        mids = {m[0] for m in conn.execute(
            "SELECT DISTINCT entity_id FROM fabric_entity_mentions WHERE dataset_id=?",
            (dataset_id,)).fetchall()}
    except Exception:
        mids = set()
    try:
        rows = conn.execute(
            "SELECT id,name,type,normalised,mention_count,datasets,props "
            "FROM fabric_entities").fetchall()
    except Exception as e:
        return {"error": str(e)}
    relevant = []
    for r in rows:
        r = dict(r)
        try:
            dss = set(json.loads(r.get("datasets") or "[]"))
        except Exception:
            dss = set()
        if dataset_id in dss or r["id"] in mids:
            r["_dss"] = dss
            relevant.append(r)

    # group by canonical key, then split only on exclusive-family conflict
    by_ck: Dict[str, list] = {}
    for e in relevant:
        by_ck.setdefault(_canonical_key(e["name"] or e.get("normalised") or ""),
                         []).append(e)
    clusters = []
    for ck, group in by_ck.items():
        if len(group) < 2:
            continue
        excl: Dict[str, list] = {}
        rest = []
        for e in group:
            fam = _type_family(e["type"])
            if e["type"] not in _GEN and fam in _EXCLUSIVE_FAMILIES:
                excl.setdefault(fam, []).append(e)
            else:
                rest.append(e)
        if len(excl) <= 1:
            clusters.append((list(excl.values())[0] if excl else []) + rest)
        else:
            biggest = max(excl, key=lambda f: sum(m["mention_count"] or 0
                                                  for m in excl[f]))
            for f, mem in excl.items():
                clusters.append(mem + (rest if f == biggest else []))

    graph = _get_graph()
    merged = 0
    ts = now_iso()
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        cluster.sort(key=lambda e: (-(e["mention_count"] or 0),
                                    e["type"] in _GEN, -len(e["name"] or "")))
        survivor, losers = cluster[0], cluster[1:]
        tc: Counter = Counter()
        for e in cluster:
            if e["type"] not in _GEN:
                tc[e["type"]] += (e["mention_count"] or 1)
        best_type = tc.most_common(1)[0][0] if tc else survivor["type"]
        try:
            sp = json.loads(survivor.get("props") or "{}")
        except Exception:
            sp = {}
        aliases = set(sp.get("aliases", []))
        facts = list(sp.get("facts", []))
        attrs = dict(sp.get("attributes", {}))
        total = survivor["mention_count"] or 0
        sdss = set(survivor.get("_dss") or set())
        for L in losers:
            try:
                lp = json.loads(L.get("props") or "{}")
            except Exception:
                lp = {}
            aliases.add(L["name"]); aliases.update(lp.get("aliases", []))
            for f in lp.get("facts", []):
                if f not in facts:
                    facts.append(f)
            attrs.update(lp.get("attributes", {}))
            total += (L["mention_count"] or 0)
            sdss |= set(L.get("_dss") or set())
            sid, lid = survivor["id"], L["id"]
            try:
                conn.execute("UPDATE OR IGNORE fabric_entity_mentions "
                             "SET entity_id=? WHERE entity_id=?", (sid, lid))
                conn.execute("DELETE FROM fabric_entity_mentions WHERE entity_id=?", (lid,))
                conn.execute("UPDATE OR IGNORE fabric_entity_relations "
                             "SET from_id=? WHERE from_id=?", (sid, lid))
                conn.execute("UPDATE OR IGNORE fabric_entity_relations "
                             "SET to_id=? WHERE to_id=?", (sid, lid))
                conn.execute("DELETE FROM fabric_entity_relations "
                             "WHERE from_id=? OR to_id=?", (lid, lid))
                conn.execute("DELETE FROM fabric_entities WHERE id=?", (lid,))
            except Exception as e:
                log.debug("consolidate merge %s->%s: %s", lid, sid, e)
            if graph and getattr(graph, "available", False):
                try:
                    await graph.query("MATCH (l:Entity {id:$l}) DETACH DELETE l",
                                      {"l": lid})
                except Exception:
                    pass
            merged += 1
        aliases.discard(survivor["name"])
        sp["aliases"] = sorted(aliases)[:40]
        sp["facts"] = facts[:40]
        sp["attributes"] = attrs
        try:
            conn.execute(
                "UPDATE fabric_entities SET type=?, mention_count=?, datasets=?, "
                "props=?, last_seen=? WHERE id=?",
                (best_type, total, json.dumps(sorted(sdss)), json.dumps(sp), ts,
                 survivor["id"]))
        except Exception as e:
            log.debug("consolidate survivor update: %s", e)
        if graph and getattr(graph, "available", False):
            try:
                await graph.upsert_node("Entity", survivor["id"], {
                    "name": survivor["name"], "type": best_type,
                    "mention_count": total})
            except Exception:
                pass
    try:
        conn.execute("DELETE FROM fabric_entity_relations WHERE from_id=to_id")
        conn.commit()
    except Exception:
        pass

    # Optional: tie related entities together with an LLM relation pass.
    linked = 0
    if link:
        try:
            q = await cap_entity_graph_query(search="", dataset_id=dataset_id, limit=120)
            ents = {e["name"]: {"name": e["name"], "type": e.get("type", ""),
                                "mention_count": e.get("mention_count", 1),
                                "description": (e.get("props") or {}).get("description", "")}
                    for e in (q.get("entities") or [])}
            existing = q.get("relationships", q.get("relations", [])) or []
            new_rels = await _llm_relate_entities(ents, existing, focus="")
            if new_rels:
                name2 = {}
                for e in conn.execute(
                        "SELECT id,name,type FROM fabric_entities").fetchall():
                    name2[(e["name"] or "").lower()] = (e["id"], e["type"])
                for r in new_rels:
                    fa = name2.get(r["from_name"].lower())
                    tb = name2.get(r["to_name"].lower())
                    if not fa or not tb or fa[0] == tb[0]:
                        continue
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO fabric_entity_relations "
                            "(from_id,to_id,rel,props,dataset_id,created_at) "
                            "VALUES (?,?,?,?,?,?)",
                            (fa[0], tb[0], r["rel"],
                             json.dumps({"from_name": r["from_name"],
                                         "to_name": r["to_name"],
                                         "context": r.get("context", ""),
                                         "cue": "consolidate_link"}),
                             dataset_id, ts))
                        linked += 1
                    except Exception:
                        pass
                conn.commit()
        except Exception as e:
            log.debug("consolidate link: %s", e)

    try:
        await emit_event({"type": "fabric.entity_graph.progress",
                          "stage": "consolidated", "dataset_id": dataset_id,
                          "merged": merged, "linked": linked,
                          "message": f"consolidated {merged} duplicate entities"
                                     + (f", +{linked} relations" if linked else "")})
    except Exception:
        pass
    return {"ok": True, "dataset_id": dataset_id, "merged": merged,
            "clusters": len([c for c in clusters if len(c) > 1]), "linked": linked}


@capability(
    "fabric.entity_graph.dedup",
    http_method="POST", http_path="/fabric/entity_graph/dedup",
    http_tags=["fabric", "graph", "entity"],
    memory="on",
    description="Rebuild a dataset's entity graph cleanly with the current "
                "normalization + alias resolution (fixes pre-existing duplicate "
                "entities like 'Cherrygrove' / 'Cherry Grove City'). Purges the "
                "dataset's existing entity graph then re-extracts it. "
                "Input: dataset_id (str!), limit (int default 500), use_llm (bool), "
                "llm_profiles (bool), llm_relationships (bool), focus (str). "
                "Output: extract result (entities/relations/persisted).",
)
async def cap_entity_graph_dedup(
    dataset_id: str, limit: int = 500, use_llm: bool = False,
    llm_profiles: bool = False, llm_relationships: bool = False,
    focus: str = "", trace_id=None,
) -> Dict:
    if not dataset_id:
        return {"error": "dataset_id required"}
    try:
        await cap_entity_graph_purge(dataset_id=dataset_id)
    except Exception as e:
        log.warning("dedup purge %s: %s", dataset_id, e)
    return await cap_entity_graph_extract(
        dataset_id=dataset_id, limit=limit, persist=True,
        alias_resolution=True, use_llm=use_llm, llm_profiles=llm_profiles,
        llm_relationships=llm_relationships, focus=focus,
    )


@capability(
    "fabric.entity_graph.profile",
    http_method="POST", http_path="/fabric/entity_graph/profile",
    http_tags=["fabric", "graph", "entity", "nlp"],
    memory="on",
    description="Build (and persist) a rich LLM profile for ONE entity from its "
                "mentions in a dataset: precise type, description, aliases, "
                "attributes and facts. "
                "Input: dataset_id (str!), name (str! — entity name), limit (int "
                "default 300 records to scan), focus (str), persist (bool default "
                "True). Output: {name, profile, updated}.",
)
async def cap_entity_graph_profile(
    dataset_id: str, name: str, limit: int = 300, focus: str = "",
    persist: bool = True, trace_id=None,
) -> Dict:
    if not dataset_id or not name:
        return {"error": "dataset_id and name required"}
    from Vera.Orchestration.fabric.data_fabric import _sqlite_query
    records = []
    offset = 0
    while len(records) < limit:
        batch = await _sqlite_query(dataset_id=dataset_id,
                                    limit=min(100, limit - len(records)), offset=offset)
        if not batch:
            break
        records.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    nl = name.lower()
    ctxs = []
    for r in records:
        txt = r.get("text") or ""
        pos = txt.lower().find(nl)
        if pos >= 0:
            ctxs.append(txt[max(0, pos - 200):pos + 400])
        if len(ctxs) >= 8:
            break
    if not ctxs:
        return {"error": f"no mentions of '{name}' found in {dataset_id}",
                "name": name}
    prof = await _llm_profile(name, "", ctxs, focus)
    if not prof:
        return {"name": name, "profile": {}, "updated": False,
                "note": "LLM unavailable or no profile produced"}

    updated = False
    if persist:
        try:
            conn = _sqlite_conn()
            ck = _canonical_key(name)
            # match by canonical key on normalised/name
            for row in conn.execute(
                    "SELECT id, name, type, normalised, props FROM fabric_entities"
            ).fetchall():
                if _canonical_key(row["name"] or row["normalised"] or "") != ck:
                    continue
                try:
                    props = json.loads(row["props"] or "{}")
                except Exception:
                    props = {}
                if prof.get("description"):
                    props["description"] = prof["description"]
                if prof.get("aliases"):
                    al = set(props.get("aliases", [])); al.update(prof["aliases"])
                    props["aliases"] = sorted(al)[:30]
                if prof.get("attributes"):
                    props.setdefault("attributes", {}).update(prof["attributes"])
                if prof.get("facts"):
                    fs = list(props.get("facts", []))
                    for f in prof["facts"]:
                        if f not in fs:
                            fs.append(f)
                    props["facts"] = fs[:30]
                new_type = prof.get("type") if row["type"] in (
                    "named_entity", "concept", "") else row["type"]
                conn.execute("UPDATE fabric_entities SET props=?, type=? WHERE id=?",
                             (json.dumps(props), new_type or row["type"], row["id"]))
                updated = True
            conn.commit()
        except Exception as e:
            log.warning("profile persist %s: %s", name, e)
        graph = _get_graph()
        if graph and graph.available and prof:
            try:
                eid = f"{prof.get('type') or 'named_entity'}:{hashlib.sha1(_canonical_key(name).encode()).hexdigest()[:12]}"
                await graph.upsert_node("Entity", eid, {
                    "name": name, "description": prof.get("description", ""),
                    "aliases": prof.get("aliases", [])[:25],
                    "facts": prof.get("facts", [])[:20]})
            except Exception as e:
                log.debug("profile neo4j %s: %s", name, e)

    return {"name": name, "profile": prof, "updated": updated,
            "contexts_used": len(ctxs)}


@capability(
    "fabric.entity_graph.query",
    http_method="POST", http_path="/fabric/entity_graph/query",
    http_tags=["fabric", "graph", "entity"],
    memory="off",
    description="Query the second-order entity graph. Search by entity name, type, "
                "or dataset. Returns entities and their relationships. "
                "Input: search (str — name/keyword), type (str — filter by entity type), "
                "dataset_id (str — filter by dataset), limit (int default 50). "
                "Output: {entities, relationships, count}.",
)
async def cap_entity_graph_query(
    search:     str = "",
    type:       str = "",
    dataset_id: str = "",
    limit:      int = 50,
    trace_id=None,
) -> Dict:
    _ensure_acq_table()
    limit = max(1, min(500, limit))

    conn = _sqlite_conn()

    # Build query — if filtering by dataset, join via mentions table
    if dataset_id:
        conditions = ["m.dataset_id = ?"]
        params: list = [dataset_id]
        if search:
            conditions.append("(e.normalised LIKE ? OR e.name LIKE ?)")
            params.extend([f"%{search.lower()}%", f"%{search}%"])
        if type:
            conditions.append("e.type = ?")
            params.append(type)
        where = " AND ".join(conditions)
        params.append(limit)
        rows = conn.execute(
            f"SELECT DISTINCT e.* FROM fabric_entities e "
            f"JOIN fabric_entity_mentions m ON m.entity_id = e.id "
            f"WHERE {where} "
            f"ORDER BY e.mention_count DESC LIMIT ?",
            tuple(params)
        ).fetchall()
    else:
        conditions = []
        params = []
        if search:
            conditions.append("(normalised LIKE ? OR name LIKE ?)")
            params.extend([f"%{search.lower()}%", f"%{search}%"])
        if type:
            conditions.append("type = ?")
            params.append(type)
        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM fabric_entities WHERE {where} "
            f"ORDER BY mention_count DESC LIMIT ?",
            tuple(params)
        ).fetchall()

    entities = []
    for row in rows:
        r = dict(row)
        try:
            r["datasets"] = json.loads(r.get("datasets", "[]"))
        except Exception:
            r["datasets"] = []
        try:
            r["props"] = json.loads(r.get("props", "{}"))
        except Exception:
            r["props"] = {}
        # Fetch per-record mentions from junction table
        mention_rows = conn.execute(
            "SELECT record_id, dataset_id FROM fabric_entity_mentions "
            "WHERE entity_id = ? ORDER BY created_at DESC LIMIT 50",
            (r["id"],)
        ).fetchall()
        r["record_ids"] = [mr["record_id"] for mr in mention_rows]
        r["mention_datasets"] = list({mr["dataset_id"] for mr in mention_rows})
        entities.append(r)

    # Get relationships from Neo4j if available
    relationships = []
    graph = _get_graph()
    if graph and graph.available and entities:
        try:
            entity_ids = [e["id"] for e in entities[:20]]
            # Query for relationships involving these entities — use named aliases
            async with graph._driver.session() as session:
                result = await session.run(
                    "MATCH (a:Entity)-[r]->(b:Entity) "
                    "WHERE a.id IN $ids OR b.id IN $ids "
                    "RETURN a.id AS aid, a.name AS aname, "
                    "       type(r) AS rel, b.id AS bid, b.name AS bname "
                    "LIMIT 200",
                    ids=entity_ids
                )
                async for record in result:
                    relationships.append({
                        "from_id":   record["aid"],
                        "from_name": record["aname"],
                        "rel":       record["rel"],
                        "to_id":     record["bid"],
                        "to_name":   record["bname"],
                    })
        except Exception as e:
            log.debug("entity rel query: %s", e)

    # SQLite fallback for relationships
    if not relationships and entities:
        try:
            entity_ids = [e["id"] for e in entities[:50]]
            ph = ",".join("?" for _ in entity_ids)
            rel_rows = conn.execute(
                f"SELECT r.from_id, r.to_id, r.rel, r.props "
                f"FROM fabric_entity_relations r "
                f"WHERE r.from_id IN ({ph}) OR r.to_id IN ({ph}) "
                f"LIMIT 200",
                tuple(entity_ids) + tuple(entity_ids)
            ).fetchall()
            # Build a name lookup from entities
            eid_name = {e["id"]: e.get("name", e["id"]) for e in entities}
            for rr in rel_rows:
                rr = dict(rr)
                try:
                    rprops = json.loads(rr.get("props", "{}") or "{}")
                except Exception:
                    rprops = {}
                relationships.append({
                    "from_id":   rr["from_id"],
                    "from_name": rprops.get("from_name") or eid_name.get(rr["from_id"], rr["from_id"]),
                    "rel":       rr["rel"],
                    "to_id":     rr["to_id"],
                    "to_name":   rprops.get("to_name") or eid_name.get(rr["to_id"], rr["to_id"]),
                })
        except Exception as e:
            log.debug("entity rel sqlite query: %s", e)

    return {
        "ok": True,
        "entities": entities,
        "relationships": relationships,
        "count": len(entities),
    }


@capability(
    "fabric.entity_graph.record_entities",
    http_method="GET", http_path="/fabric/entity_graph/record_entities",
    http_tags=["fabric", "graph", "entity"],
    memory="off", silent=True,
    description="Get entities mentioned in a specific record. "
                "Input: record_id (str!), limit (int default 60). "
                "Output: {nodes, edges, node_count, edge_count}.",
)
async def cap_entity_graph_record_entities(
    record_id: str = "",
    limit:     int = 60,
    trace_id=None,
) -> Dict:
    if not record_id:
        return {"error": "record_id required", "nodes": [], "edges": []}
    _ensure_acq_table()
    limit = max(1, min(200, int(limit)))
    conn = _sqlite_conn()

    mention_rows = conn.execute(
        "SELECT m.entity_id, m.dataset_id FROM fabric_entity_mentions m "
        "WHERE m.record_id = ? LIMIT ?",
        (record_id, limit)
    ).fetchall()
    if not mention_rows:
        return {"nodes": [], "edges": [], "node_count": 0, "edge_count": 0}

    entity_ids = list(set(mr["entity_id"] for mr in mention_rows))
    placeholders = ",".join("?" for _ in entity_ids)
    entity_rows = conn.execute(
        f"SELECT id, name, type, normalised, mention_count, datasets, props "
        f"FROM fabric_entities WHERE id IN ({placeholders})",
        tuple(entity_ids)
    ).fetchall()

    nodes = []
    for row in entity_rows:
        r = dict(row)
        try:
            ds = json.loads(r.get("datasets", "[]"))
        except Exception:
            ds = []
        nodes.append({
            "id": r["id"], "name": r["name"], "type": "Entity",
            "labels": ["Entity"],
            "props": {"name": r["name"], "type": r["type"],
                      "mention_count": r.get("mention_count", 0), "datasets": ds},
        })

    edges = [{"from": eid, "to": record_id, "rel": "MENTIONED_IN", "props": {}}
             for eid in entity_ids]

    # Entity-entity relationship edges — try Neo4j first, fall back to SQLite
    ee_edges_found = False
    graph = _get_graph()
    if graph and graph.available and len(entity_ids) > 1:
        try:
            async with graph._driver.session() as session:
                result = await session.run(
                    "MATCH (a:Entity)-[r]->(b:Entity) "
                    "WHERE a.id IN $ids AND b.id IN $ids "
                    "RETURN a.id AS aid, type(r) AS rel, b.id AS bid LIMIT 200",
                    ids=entity_ids)
                async for record in result:
                    edges.append({"from": record["aid"], "to": record["bid"],
                                  "rel": record["rel"], "props": {}})
                    ee_edges_found = True
        except Exception as e:
            log.warning("record_entities neo4j edges: %s", e)

    # SQLite fallback for entity-entity relationships
    if not ee_edges_found and len(entity_ids) > 1:
        try:
            ph = ",".join("?" for _ in entity_ids)
            rel_rows = conn.execute(
                f"SELECT from_id, to_id, rel, props FROM fabric_entity_relations "
                f"WHERE from_id IN ({ph}) AND to_id IN ({ph}) LIMIT 200",
                tuple(entity_ids) + tuple(entity_ids)
            ).fetchall()
            for rr in rel_rows:
                rr = dict(rr)
                try:
                    rprops = json.loads(rr.get("props", "{}") or "{}")
                except Exception:
                    rprops = {}
                edges.append({"from": rr["from_id"], "to": rr["to_id"],
                              "rel": rr["rel"], "props": rprops})
        except Exception as e:
            log.warning("record_entities sqlite rels: %s", e)

    return {"nodes": nodes, "edges": edges,
            "node_count": len(nodes), "edge_count": len(edges)}


@capability(
    "fabric.entity_graph.snapshot",
    http_method="GET", http_path="/fabric/entity_graph/snapshot",
    http_tags=["fabric", "graph", "entity"],
    memory="off",
    description="Get a snapshot of the entity graph for visualisation. "
                "Returns nodes (entities) and edges (relationships) suitable "
                "for graph rendering. Optionally includes first-order Dataset "
                "and FabricRecord nodes connected via HAS_ENTITY/MENTIONED_IN. "
                "Input: dataset_id (str — optional filter), "
                "entity_type (str — optional filter), "
                "limit (int default 200), "
                "include_datasets (bool default False), "
                "include_records (bool default False). "
                "Output: {nodes, edges, node_count, edge_count}.",
)
async def cap_entity_graph_snapshot(
    dataset_id:        str  = "",
    entity_type:       str  = "",
    limit:             int  = 200,
    include_datasets:  bool = False,
    include_records:   bool = False,
    trace_id=None,
) -> Dict:
    _ensure_acq_table()

    # Coerce string forms (query-string params arrive as strings)
    if isinstance(include_datasets, str):
        include_datasets = include_datasets.lower() in ("1", "true", "yes", "on")
    if isinstance(include_records, str):
        include_records = include_records.lower() in ("1", "true", "yes", "on")

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(500, limit))

    conn = _sqlite_conn()

    # If filtering by dataset, use the mentions junction table
    if dataset_id:
        conditions = ["m.dataset_id = ?"]
        params: List[Any] = [dataset_id]
        if entity_type:
            conditions.append("e.type = ?")
            params.append(entity_type)
        where = " AND ".join(conditions)
        params.append(limit)
        rows = conn.execute(
            f"SELECT DISTINCT e.id, e.name, e.type, e.normalised, "
            f"e.mention_count, e.datasets, e.props "
            f"FROM fabric_entities e "
            f"JOIN fabric_entity_mentions m ON m.entity_id = e.id "
            f"WHERE {where} "
            f"ORDER BY e.mention_count DESC LIMIT ?",
            tuple(params)
        ).fetchall()
    else:
        conditions = []
        params = []
        if entity_type:
            conditions.append("type = ?")
            params.append(entity_type)
        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)
        rows = conn.execute(
            f"SELECT id, name, type, normalised, mention_count, datasets, props "
            f"FROM fabric_entities WHERE {where} "
            f"ORDER BY mention_count DESC LIMIT ?",
            tuple(params)
        ).fetchall()

    nodes = []
    record_ids_to_include: set = set()
    dataset_ids_to_include: set = set()

    for row in rows:
        r = dict(row)
        try:
            ds = json.loads(r.get("datasets", "[]"))
        except Exception:
            ds = []

        # Fetch per-record mentions from junction table
        mention_rows = conn.execute(
            "SELECT record_id, dataset_id FROM fabric_entity_mentions "
            "WHERE entity_id = ? LIMIT 30",
            (r["id"],)
        ).fetchall()
        ent_record_ids = [mr["record_id"] for mr in mention_rows]

        nodes.append({
            "id":     r["id"],
            "name":   r["name"],
            "type":   r["type"],
            "labels": ["Entity"],
            "props": {
                "name":          r["name"],
                "type":          r["type"],
                "mention_count": r.get("mention_count", 0),
                "datasets":      ds,
                "record_ids":    ent_record_ids[:10],
            },
        })
        if include_datasets:
            for d in ds:
                if d:
                    dataset_ids_to_include.add(d)
        if include_records:
            for rid in ent_record_ids[:10]:
                if rid:
                    record_ids_to_include.add(rid)

    edges = []
    graph = _get_graph()
    if graph and graph.available and nodes:
        try:
            entity_ids = [n["id"] for n in nodes[:200]]
            async with graph._driver.session() as session:
                # Entity-Entity edges — use named aliases (record["a.id"] doesn't
                # work in modern neo4j drivers; alias to a clean name).
                result = await session.run(
                    "MATCH (a:Entity)-[r]->(b:Entity) "
                    "WHERE a.id IN $ids AND b.id IN $ids "
                    "RETURN a.id AS aid, type(r) AS rel, b.id AS bid, "
                    "       properties(r) AS rprops "
                    "LIMIT 500",
                    ids=entity_ids
                )
                async for record in result:
                    edges.append({
                        "from":  record["aid"],
                        "to":    record["bid"],
                        "rel":   record["rel"],
                        "props": dict(record["rprops"]) if record["rprops"] else {},
                    })

                # Optional Dataset overlay
                if include_datasets and dataset_ids_to_include:
                    ds_list = list(dataset_ids_to_include)[:200]
                    ds_result = await session.run(
                        "MATCH (d:Dataset) WHERE d.id IN $dsids "
                        "RETURN d.id AS did, properties(d) AS dprops",
                        dsids=ds_list
                    )
                    seen_ds: set = set()
                    async for record in ds_result:
                        did = record["did"]
                        if did in seen_ds:
                            continue
                        seen_ds.add(did)
                        nodes.append({
                            "id":     did,
                            "name":   did,
                            "type":   "Dataset",
                            "labels": ["Dataset"],
                            "props":  dict(record["dprops"]) if record["dprops"] else {"id": did},
                        })
                    he_result = await session.run(
                        "MATCH (d:Dataset)-[r:HAS_ENTITY]->(e:Entity) "
                        "WHERE d.id IN $dsids AND e.id IN $eids "
                        "RETURN d.id AS did, e.id AS eid, properties(r) AS rprops "
                        "LIMIT 1000",
                        dsids=ds_list, eids=entity_ids
                    )
                    async for record in he_result:
                        edges.append({
                            "from":  record["did"],
                            "to":    record["eid"],
                            "rel":   "HAS_ENTITY",
                            "props": dict(record["rprops"]) if record["rprops"] else {},
                        })

                # Optional FabricRecord overlay
                if include_records and record_ids_to_include:
                    rec_list = list(record_ids_to_include)[:300]
                    rec_result = await session.run(
                        "MATCH (r:FabricRecord) WHERE r.id IN $rids "
                        "RETURN r.id AS rid, properties(r) AS rprops "
                        "LIMIT 500",
                        rids=rec_list
                    )
                    seen_rec: set = set()
                    async for record in rec_result:
                        rid = record["rid"]
                        if rid in seen_rec:
                            continue
                        seen_rec.add(rid)
                        rprops = dict(record["rprops"]) if record["rprops"] else {}
                        nodes.append({
                            "id":     rid,
                            "name":   (rprops.get("title") or rprops.get("url") or rid)[:60],
                            "type":   "FabricRecord",
                            "labels": ["FabricRecord"],
                            "props":  rprops,
                        })
                    mi_result = await session.run(
                        "MATCH (e:Entity)-[r:MENTIONED_IN]->(rec:FabricRecord) "
                        "WHERE e.id IN $eids AND rec.id IN $rids "
                        "RETURN e.id AS eid, rec.id AS rid "
                        "LIMIT 1000",
                        eids=entity_ids, rids=rec_list
                    )
                    async for record in mi_result:
                        edges.append({
                            "from":  record["eid"],
                            "to":    record["rid"],
                            "rel":   "MENTIONED_IN",
                            "props": {},
                        })
        except Exception as e:
            log.warning("entity snapshot edges: %s", e)

    # SQLite fallback: if no entity-entity edges from Neo4j, use the local table
    if nodes:
        entity_ids_for_rels = [n["id"] for n in nodes if n.get("type") == "Entity" or
                               (n.get("labels") and "Entity" in n["labels"])]
        ee_count = sum(1 for e in edges if e.get("rel") not in ("MENTIONED_IN", "HAS_ENTITY"))
        if ee_count == 0 and len(entity_ids_for_rels) > 1:
            try:
                ph = ",".join("?" for _ in entity_ids_for_rels)
                rel_rows = conn.execute(
                    f"SELECT from_id, to_id, rel, props FROM fabric_entity_relations "
                    f"WHERE from_id IN ({ph}) AND to_id IN ({ph}) LIMIT 500",
                    tuple(entity_ids_for_rels) + tuple(entity_ids_for_rels)
                ).fetchall()
                for rr in rel_rows:
                    rr = dict(rr)
                    try:
                        rprops = json.loads(rr.get("props", "{}") or "{}")
                    except Exception:
                        rprops = {}
                    edges.append({"from": rr["from_id"], "to": rr["to_id"],
                                  "rel": rr["rel"], "props": rprops})
            except Exception as e:
                log.warning("entity snapshot sqlite rels: %s", e)

    return {
        "nodes":      nodes,
        "edges":      edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITY: ATTACH ENTITIES TO DATASETS (HAS_ENTITY edges)
# ═══════════════════════════════════════════════════════════════════════════

@capability(
    "fabric.entity_graph.attach_to_datasets",
    http_method="POST", http_path="/fabric/entity_graph/attach_to_datasets",
    http_tags=["fabric", "graph", "entity"],
    memory="off",
    description="Create HAS_ENTITY edges from each Dataset to every Entity that "
                "was extracted from records in that dataset. This makes the "
                "second-order entities visible in the main fabric graph view. "
                "Idempotent — safe to re-run. "
                "Output: {ok, edges_created, datasets_linked, entities_processed}.",
)
async def cap_entity_attach_to_datasets(trace_id=None) -> Dict:
    _ensure_acq_table()

    graph = _get_graph()
    if not graph or not graph.available:
        return {"error": "Neo4j graph not available", "ok": False}

    conn = _sqlite_conn()
    rows = conn.execute(
        "SELECT id, datasets FROM fabric_entities ORDER BY mention_count DESC LIMIT 5000"
    ).fetchall()

    edges = []
    seen_datasets: set = set()
    entities_processed = 0
    for row in rows:
        r = dict(row)
        try:
            ds_list = json.loads(r.get("datasets", "[]"))
        except Exception:
            ds_list = []
        if not ds_list:
            continue
        entities_processed += 1
        for ds in ds_list:
            if not ds:
                continue
            seen_datasets.add(ds)
            edges.append({
                "from_label": "Dataset", "from_id": ds,
                "to_label":   "Entity",  "to_id":   r["id"],
                "rel":        "HAS_ENTITY",
                "props":      {},
            })

    # Make sure Dataset nodes exist
    for ds in seen_datasets:
        try:
            await graph.upsert_node("Dataset", ds, {"id": ds})
        except Exception:
            pass

    edges_created = 0
    if edges:
        try:
            edges_created = await graph.link_many(edges)
        except Exception as e:
            log.warning("attach_to_datasets: %s", e)
            return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "edges_created": edges_created,
        "datasets_linked": len(seen_datasets),
        "entities_processed": entities_processed,
    }


@capability(
    "fabric.web.acquire_status",
    http_method="GET", http_path="/fabric/web/acquire_status",
    http_tags=["fabric", "web"],
    memory="off",
    description="List recent web acquisitions with their status. "
                "Input: limit (int default 20). "
                "Output: {acquisitions: [...]}.",
)
async def cap_web_acquire_status(
    limit: int = 20,
    trace_id=None,
) -> Dict:
    _ensure_acq_table()
    conn = _sqlite_conn()
    rows = conn.execute(
        "SELECT id, dataset_id, source_url, status, pages_fetched, "
        "entities_found, created_at, updated_at FROM fabric_acquisitions "
        "ORDER BY updated_at DESC LIMIT ?",
        (min(limit, 100),)
    ).fetchall()
    return {
        "acquisitions": [dict(r) for r in rows],
        "count": len(rows),
    }


@capability(
    "fabric.entity_graph.types",
    http_method="GET", http_path="/fabric/entity_graph/types",
    http_tags=["fabric", "graph", "entity"],
    memory="off",
    description="List all entity types and their counts. "
                "Output: {types: [{type, count}]}.",
)
async def cap_entity_graph_types(trace_id=None) -> Dict:
    _ensure_acq_table()
    conn = _sqlite_conn()
    rows = conn.execute(
        "SELECT type, COUNT(*) as cnt FROM fabric_entities "
        "GROUP BY type ORDER BY cnt DESC"
    ).fetchall()
    return {
        "types": [{"type": r[0], "count": r[1]} for r in rows],
    }


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITY: PURGE ENTITY GRAPH (wipe stale / outdated data)
# ═══════════════════════════════════════════════════════════════════════════

@capability(
    "fabric.entity_graph.purge",
    http_method="POST", http_path="/fabric/entity_graph/purge",
    http_tags=["fabric", "graph", "entity"],
    memory="off",
    description="Purge entity graph data. Removes entities, mentions, and Neo4j "
                "Entity/MENTIONED_IN/HAS_ENTITY/CO_OCCURS edges. "
                "Input: dataset_id (str — optional, purge only this dataset; "
                "omit to purge ALL entity data), "
                "entity_types (str — comma-separated types to purge, optional). "
                "Output: {ok, entities_deleted, mentions_deleted, neo4j_purged}.",
)
async def cap_entity_graph_purge(
    dataset_id:   str = "",
    entity_types: str = "",
    trace_id=None,
) -> Dict:
    _ensure_acq_table()
    conn = _sqlite_conn()

    entities_deleted = 0
    mentions_deleted = 0

    type_list = [t.strip() for t in entity_types.split(",") if t.strip()] if entity_types else []

    if dataset_id and not type_list:
        # Purge entities only linked to this dataset
        # First find entity IDs that are ONLY in this dataset
        mention_del = conn.execute(
            "DELETE FROM fabric_entity_mentions WHERE dataset_id = ?",
            (dataset_id,)
        )
        mentions_deleted = mention_del.rowcount

        # Delete entities that now have zero mentions
        ent_del = conn.execute(
            "DELETE FROM fabric_entities WHERE id NOT IN "
            "(SELECT DISTINCT entity_id FROM fabric_entity_mentions)"
        )
        entities_deleted = ent_del.rowcount

        # Update remaining entities' datasets field
        remaining = conn.execute(
            "SELECT DISTINCT entity_id FROM fabric_entity_mentions"
        ).fetchall()
        for row in remaining:
            eid = row[0]
            ds_rows = conn.execute(
                "SELECT DISTINCT dataset_id FROM fabric_entity_mentions WHERE entity_id = ?",
                (eid,)
            ).fetchall()
            conn.execute(
                "UPDATE fabric_entities SET datasets = ?, "
                "mention_count = (SELECT COUNT(*) FROM fabric_entity_mentions WHERE entity_id = ?) "
                "WHERE id = ?",
                (json.dumps([r[0] for r in ds_rows]), eid, eid)
            )

    elif type_list:
        # Purge specific entity types
        placeholders = ",".join("?" * len(type_list))
        if dataset_id:
            # Type + dataset scoped
            eids = conn.execute(
                f"SELECT e.id FROM fabric_entities e "
                f"JOIN fabric_entity_mentions m ON m.entity_id = e.id "
                f"WHERE e.type IN ({placeholders}) AND m.dataset_id = ?",
                (*type_list, dataset_id)
            ).fetchall()
            eid_list = [r[0] for r in eids]
        else:
            eids = conn.execute(
                f"SELECT id FROM fabric_entities WHERE type IN ({placeholders})",
                tuple(type_list)
            ).fetchall()
            eid_list = [r[0] for r in eids]

        if eid_list:
            eid_ph = ",".join("?" * len(eid_list))
            m_del = conn.execute(
                f"DELETE FROM fabric_entity_mentions WHERE entity_id IN ({eid_ph})",
                tuple(eid_list)
            )
            mentions_deleted = m_del.rowcount
            e_del = conn.execute(
                f"DELETE FROM fabric_entities WHERE id IN ({eid_ph})",
                tuple(eid_list)
            )
            entities_deleted = e_del.rowcount
    else:
        # Full purge — everything
        e_del = conn.execute("DELETE FROM fabric_entities")
        entities_deleted = e_del.rowcount
        m_del = conn.execute("DELETE FROM fabric_entity_mentions")
        mentions_deleted = m_del.rowcount

    conn.commit()

    # Neo4j purge
    neo4j_purged = False
    graph = _get_graph()
    if graph and graph.available:
        try:
            async with graph._driver.session() as session:
                if dataset_id and not type_list:
                    await session.run(
                        "MATCH (e:Entity)-[r:MENTIONED_IN]->(rec:FabricRecord) "
                        "WHERE r.dataset_id = $dsid DELETE r",
                        dsid=dataset_id
                    )
                    await session.run(
                        "MATCH (d:Dataset {id: $dsid})-[r:HAS_ENTITY]->(e:Entity) DELETE r",
                        dsid=dataset_id
                    )
                    # Remove orphaned Entity nodes
                    await session.run(
                        "MATCH (e:Entity) WHERE NOT (e)--() DELETE e"
                    )
                elif not dataset_id and not type_list:
                    # Full purge
                    await session.run(
                        "MATCH (e:Entity) DETACH DELETE e"
                    )
                neo4j_purged = True
        except Exception as e:
            log.warning("entity purge neo4j: %s", e)

    return {
        "ok": True,
        "entities_deleted": entities_deleted,
        "mentions_deleted": mentions_deleted,
        "neo4j_purged": neo4j_purged,
    }


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITY: ENTITY RECORDS — which records mention a specific entity
# ═══════════════════════════════════════════════════════════════════════════

@capability(
    "fabric.entity_graph.records",
    http_method="POST", http_path="/fabric/entity_graph/records",
    http_tags=["fabric", "graph", "entity"],
    memory="off",
    description="Get the fabric records that mention a specific entity. "
                "Input: entity_id (str!), limit (int default 20). "
                "Output: {records: [{id, dataset_id, text, title, url, ...}], count}.",
)
async def cap_entity_graph_records(
    entity_id: str,
    limit:     int = 20,
    trace_id=None,
) -> Dict:
    if not entity_id:
        return {"error": "entity_id required"}
    _ensure_acq_table()
    limit = max(1, min(200, limit))

    conn = _sqlite_conn()

    # Get record IDs from mentions table
    mention_rows = conn.execute(
        "SELECT record_id, dataset_id FROM fabric_entity_mentions "
        "WHERE entity_id = ? ORDER BY created_at DESC LIMIT ?",
        (entity_id, limit)
    ).fetchall()

    if not mention_rows:
        return {"records": [], "count": 0, "entity_id": entity_id}

    # Fetch the actual record content from fabric_records
    records = []
    for mr in mention_rows:
        rid = mr["record_id"]
        dsid = mr["dataset_id"]
        rec_row = conn.execute(
            "SELECT id, dataset_id, text, data, tags, created_at "
            "FROM fabric_records WHERE id = ?",
            (rid,)
        ).fetchone()
        if rec_row:
            r = dict(rec_row)
            # Parse data JSON for title/url
            try:
                data = json.loads(r.get("data") or "{}")
            except Exception:
                data = {}
            records.append({
                "id":         r["id"],
                "dataset_id": r["dataset_id"],
                "text":       (r.get("text") or "")[:500],
                "title":      data.get("title", "")[:120],
                "url":        data.get("url", ""),
                "tags":       r.get("tags", ""),
                "created_at": r.get("created_at", ""),
            })
        else:
            # Record not found in SQLite — still report the link
            records.append({
                "id":         rid,
                "dataset_id": dsid,
                "text":       "",
                "title":      "(record not in local store)",
                "url":        "",
                "tags":       "",
                "created_at": "",
            })

    return {
        "ok": True,
        "entity_id": entity_id,
        "records": records,
        "count": len(records),
    }


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITY: BULK LOAD ENTITIES — import second-order entities + edges
# ═══════════════════════════════════════════════════════════════════════════

@capability(
    "fabric.entity_graph.bulk_load",
    http_method="POST", http_path="/fabric/entity_graph/bulk_load",
    http_tags=["fabric", "graph", "entity"],
    memory="off",
    description="Bulk load entities and relationships into the entity graph. "
                "Use this to import pre-computed entities, merge external NER output, "
                "or restore from a backup. "
                "Input: entities (list of {name, type, record_ids:[str], datasets:[str]}), "
                "relationships (list of {from_name, from_type, to_name, to_type, rel}), "
                "dataset_id (str — default dataset for entities without explicit datasets), "
                "purge_first (bool default False — wipe existing entity data for dataset_id before loading). "
                "Output: {ok, entities_loaded, relationships_loaded, persisted}.",
)
async def cap_entity_graph_bulk_load(
    entities:       List[Dict] = None,
    relationships:  List[Dict] = None,
    dataset_id:     str = "",
    purge_first:    bool = False,
    trace_id=None,
) -> Dict:
    if not entities:
        return {"error": "entities list required"}

    _ensure_acq_table()

    # Optionally purge existing data first
    if purge_first and dataset_id:
        await cap_entity_graph_purge(dataset_id=dataset_id)

    # Normalise and build entity dict
    all_entities: Dict[str, Dict] = {}
    for ent in (entities or [])[:5000]:
        if not isinstance(ent, dict):
            continue
        name = (ent.get("name") or "").strip()
        if not name:
            continue
        etype = (ent.get("type") or "entity")[:30]
        norm = _normalise_entity(name)
        if not norm:
            continue

        record_ids = set()
        for rid in (ent.get("record_ids") or []):
            if rid:
                record_ids.add(str(rid))

        datasets = set()
        for ds in (ent.get("datasets") or []):
            if ds:
                datasets.add(str(ds))
        if dataset_id and not datasets:
            datasets.add(dataset_id)

        if norm in all_entities:
            all_entities[norm]["mention_count"] += ent.get("mention_count", max(1, len(record_ids)))
            all_entities[norm]["record_ids"].update(record_ids)
            all_entities[norm]["datasets"].update(datasets)
        else:
            all_entities[norm] = {
                "name": name,
                "type": etype,
                "normalised": norm,
                "mention_count": ent.get("mention_count", max(1, len(record_ids))),
                "record_ids": record_ids,
                "datasets": datasets,
            }

    # Build relations
    all_relations = []
    for rel in (relationships or [])[:2000]:
        if not isinstance(rel, dict):
            continue
        from_name = (rel.get("from_name") or "").strip()
        to_name = (rel.get("to_name") or "").strip()
        if not from_name or not to_name:
            continue
        all_relations.append({
            "from_name": from_name,
            "from_type": (rel.get("from_type") or "entity")[:30],
            "to_name": to_name,
            "to_type": (rel.get("to_type") or "entity")[:30],
            "rel": (rel.get("rel") or "RELATED_TO")[:30].upper(),
            "distance": rel.get("distance", 0),
        })

    persisted = 0
    if all_entities:
        target_ds = dataset_id or "bulk_import"
        persisted = await _persist_entity_graph(
            all_entities, all_relations, target_ds
        )

    return {
        "ok": True,
        "entities_loaded": len(all_entities),
        "relationships_loaded": len(all_relations),
        "persisted": persisted,
    }




# ---------------------------------------------------------------------------
# NER model installation (pip + spaCy download)
# ---------------------------------------------------------------------------
@capability(
    "fabric.entity_graph.ner_install",
    http_method="POST", http_path="/fabric/entity_graph/ner_install",
    http_tags=["fabric", "entity", "nlp"], memory="off",
    streams=["fabric.ner.install.progress"],
    description=(
        "Install or update NER/NLP model packages at runtime. "
        "Runs pip install for gliner or spacy, optionally also runs "
        "'python -m spacy download <model>' for spaCy language models. "
        "Returns live stdout/stderr via stream events. "
        "Input: package ('gliner' | 'spacy' | 'spacy_model' | custom pip spec), "
        "model_name (str, spaCy model to download e.g. en_core_web_trf), "
        "force_reinstall (bool=False). "
        "Output: {ok, returncode, stdout, stderr, package, model_name}."
    ),
)
async def cap_ner_install_model(
    package: str = "",
    model_name: str = "",
    force_reinstall: bool = False,
    trace_id=None,
) -> Dict:
    pkg = (package or "").strip()
    model = (model_name or "").strip()
    if not pkg and not model:
        return {"error": "package or model_name required"}

    async def _emit(stage: str, **kw):
        try:
            await emit_event({"type": "fabric.ner.install.progress",
                              "stage": stage, **kw})
        except Exception:
            pass

    results = []

    # --- pip install ---
    if pkg:
        # Map shorthand names to pip specs
        pip_spec = {
            "gliner": "gliner",
            "spacy": "spacy[transformers]",
        }.get(pkg.lower(), pkg)

        cmd = [sys.executable, "-m", "pip", "install", pip_spec]
        if force_reinstall:
            cmd.append("--force-reinstall")
        cmd.append("--break-system-packages")

        await _emit("pip_start", message=f"pip install {pip_spec}",
                    cmd=" ".join(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout_lines = []
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                stdout_lines.append(text)
                await _emit("pip_output", line=text)
            await proc.wait()
            rc = proc.returncode
            await _emit("pip_done", returncode=rc,
                        message=f"pip exit {rc}")
            results.append({"step": "pip", "spec": pip_spec,
                            "returncode": rc, "stdout": "\n".join(stdout_lines[-40:])})
            if rc != 0:
                return {"ok": False, "returncode": rc,
                        "package": pkg, "model_name": model,
                        "stdout": "\n".join(stdout_lines[-40:]),
                        "error": f"pip install failed (exit {rc})"}
        except Exception as e:
            return {"ok": False, "error": f"pip install error: {e}",
                    "package": pkg, "model_name": model}

    # --- spaCy model download ---
    if model:
        cmd2 = [sys.executable, "-m", "spacy", "download", model]
        await _emit("spacy_download_start", message=f"spacy download {model}",
                    cmd=" ".join(cmd2))
        try:
            proc2 = await asyncio.create_subprocess_exec(
                *cmd2,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout2 = []
            async for line in proc2.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                stdout2.append(text)
                await _emit("spacy_output", line=text)
            await proc2.wait()
            rc2 = proc2.returncode
            await _emit("spacy_download_done", returncode=rc2,
                        message=f"spacy download exit {rc2}")
            results.append({"step": "spacy_download", "model": model,
                            "returncode": rc2, "stdout": "\n".join(stdout2[-40:])})
            if rc2 != 0:
                return {"ok": False, "returncode": rc2,
                        "package": pkg, "model_name": model,
                        "stdout": "\n".join(stdout2[-40:]),
                        "error": f"spacy download failed (exit {rc2})"}
        except Exception as e:
            return {"ok": False, "error": f"spacy download error: {e}",
                    "package": pkg, "model_name": model}

    # Force NER state to re-detect on next use
    _NER_STATE.update(init=False, kind="heuristic", obj=None)

    return {
        "ok": True,
        "package": pkg,
        "model_name": model,
        "steps": results,
        "note": "NER backend will re-detect on next use. "
                "Call fabric.entity_graph.ner to confirm active backend.",
    }


# ---------------------------------------------------------------------------
# NER GLiNER label and threshold runtime control
# ---------------------------------------------------------------------------
@capability(
    "fabric.entity_graph.ner_labels",
    http_method="POST", http_path="/fabric/entity_graph/ner_labels",
    http_tags=["fabric", "entity", "nlp"], memory="off",
    description=(
        "Get or set the GLiNER entity label set and confidence threshold at runtime. "
        "GET (no args): return current labels and threshold. "
        "POST with labels (comma-separated str) and/or threshold (float): update them. "
        "Labels control what entity types GLiNER extracts; threshold filters low-confidence spans. "
        "Changes take effect immediately for the next extraction. "
        "Input: labels (str, comma-separated, e.g. 'person,organisation,location,technology'), "
        "threshold (float, 0.1-0.95). "
        "Output: {labels, threshold, changed}."
    ),
)
async def cap_ner_set_labels(
    labels: str = "",
    threshold: float = -1.0,
    trace_id=None,
) -> Dict:
    changed = False
    if labels.strip():
        os.environ["FABRIC_GLINER_LABELS"] = labels.strip()
        changed = True
    if threshold >= 0.0:
        os.environ["FABRIC_GLINER_THRESHOLD"] = str(min(0.99, max(0.01, threshold)))
        changed = True
    return {
        "ok": True,
        "labels": _gliner_labels(),
        "threshold": _gliner_threshold(),
        "changed": changed,
        "note": "Changes apply to the next GLiNER extraction call.",
    }

log.info("data_fabric_web_acquisition: capabilities registered")