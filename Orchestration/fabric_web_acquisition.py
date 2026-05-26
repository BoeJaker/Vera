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
    from Vera.Orchestration.data_fabric import ingest_dataset
    return ingest_dataset

def _sqlite_conn():
    from Vera.Orchestration.data_fabric import _sqlite_conn as sc
    return sc()

def _get_graph(name="fabric"):
    from Vera.Orchestration.data_fabric import GRAPHS
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

def _extract_page_structure(html: str, url: str) -> Dict:
    """Extract structured content from HTML: headings, sections, links,
    code blocks, lists, metadata. Returns a rich record."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        # Fallback: regex-based extraction
        return _extract_page_structure_regex(html, url)

    soup = BeautifulSoup(html[:200000], "html.parser")

    # Title
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    # Meta description
    desc = ""
    meta_desc = (soup.find("meta", attrs={"name": "description"}) or
                 soup.find("meta", attrs={"property": "og:description"}))
    if meta_desc:
        desc = (meta_desc.get("content", "") or "")[:500]

    # Headings hierarchy
    headings = []
    for tag in soup.find_all(re.compile(r"^h[1-6]$")):
        text = tag.get_text(strip=True)
        if text and len(text) < 200:
            headings.append({
                "level": int(tag.name[1]),
                "text": text[:120],
            })

    # Extract links with anchor text and context
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
        anchor = a.get_text(strip=True)[:80]
        # Get surrounding context (parent paragraph text)
        parent = a.find_parent(["p", "li", "td", "div", "section"])
        context = ""
        if parent:
            context = parent.get_text(strip=True)[:200]
        links.append({
            "url": full,
            "anchor": anchor,
            "context": context,
        })

    # Code blocks
    code_blocks = []
    for code in soup.find_all(["code", "pre"]):
        text = code.get_text(strip=True)
        if len(text) > 20:
            lang = ""
            classes = code.get("class", [])
            for c in classes:
                if c.startswith(("language-", "lang-", "highlight-")):
                    lang = c.split("-", 1)[1]
                    break
            code_blocks.append({
                "language": lang,
                "text": text[:2000],
            })

    # Main content text
    main_el = (soup.find("main") or soup.find("article") or
               soup.find(id=re.compile(r"^(content|main|mw-content-text)", re.I)) or
               soup.find(class_=re.compile(r"(content|article|post|entry)", re.I)))
    text_root = main_el if main_el else soup.body or soup
    for tag in text_root(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    full_text = " ".join(text_root.get_text(separator=" ").split())[:20000]

    # Lists (ordered and unordered)
    lists = []
    for ol_ul in soup.find_all(["ul", "ol"]):
        items = []
        for li in ol_ul.find_all("li", recursive=False):
            t = li.get_text(strip=True)[:200]
            if t:
                items.append(t)
        if items and len(items) >= 2:
            lists.append({
                "type": ol_ul.name,
                "items": items[:30],
            })

    return {
        "title": title,
        "description": desc,
        "headings": headings[:50],
        "links": links[:200],
        "code_blocks": code_blocks[:20],
        "lists": lists[:20],
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
_PAT_PERSON_TITLE = re.compile(
    r"\b(CEO|CTO|CFO|COO|President|Director|Chairman|Chairwoman|VP|Professor|"
    r"Dr\.|Mr\.|Mrs\.|Ms\.)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b"
)
_PAT_ORG = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,4})\s+(?:Inc|Corp|Ltd|LLC|GmbH|Co|"
    r"Foundation|Institute|University|Association|Group|Holdings)\b"
)
_PAT_TECH = re.compile(
    r"\b(Python|JavaScript|TypeScript|Rust|Go|Java|C\+\+|Ruby|Swift|Kotlin|"
    r"React|Vue|Angular|Django|Flask|FastAPI|Node\.js|Docker|Kubernetes|"
    r"PostgreSQL|MySQL|MongoDB|Redis|Neo4j|TensorFlow|PyTorch|CUDA|"
    r"GPT-\d|BERT|Transformer|LLM|API|REST|GraphQL|WebSocket|OAuth|"
    r"FAISS|Chroma|Ollama|ONNX|WASM|gRPC)\b"
)
_PAT_CAMEL = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+){1,})\b")
_PAT_SNAKE = re.compile(r"\b([a-z]+(?:_[a-z]+){1,})\b")
_PAT_FUNC_DEF = re.compile(r"\bdef\s+(\w+)\b|\bfunction\s+(\w+)\b")
_PAT_CLASS_DEF = re.compile(r"\bclass\s+(\w+)\b")
_PAT_IMPORT = re.compile(r"(?:import|from)\s+([\w.]+)")
_PAT_URL_DOMAIN = re.compile(r"https?://([\w.-]+)/?")
_PAT_CAPS_PHRASE = re.compile(
    r"\b([A-Z][a-z]+(?:\s+(?:of|the|and|for|in|on|at|to|by|with)\s+)?"
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b"
)


def _normalise_entity(name: str) -> str:
    """Normalise an entity name for deduplication."""
    n = name.strip()
    n = re.sub(r"\s+", " ", n)
    n = n.lower()
    # Remove trailing punctuation
    n = n.rstrip(".,;:!?")
    return n


def _extract_entities_from_text(text: str, content_type: str = "text") -> List[Dict]:
    """Extract entities from text using regex patterns.
    Returns list of {name, type, position, context}."""
    entities = []
    seen = set()

    def _add(name, etype, pos=0, ctx=""):
        norm = _normalise_entity(name)
        if norm and len(norm) >= 2 and len(norm) < 120 and norm not in seen:
            seen.add(norm)
            entities.append({
                "name": name.strip(),
                "type": etype,
                "normalised": norm,
                "position": pos,
                "context": ctx[:200],
            })

    # Dates
    for m in _PAT_DATE.finditer(text):
        _add(m.group(1), "date", m.start())

    # People with titles
    for m in _PAT_PERSON_TITLE.finditer(text):
        _add(m.group(0), "person", m.start(),
             text[max(0, m.start()-40):m.end()+40])

    # Organisations
    for m in _PAT_ORG.finditer(text):
        _add(m.group(0), "organisation", m.start())

    # Technologies
    for m in _PAT_TECH.finditer(text):
        _add(m.group(1), "technology", m.start())

    # Code-specific entities
    if content_type in ("code", "web"):
        for m in _PAT_CLASS_DEF.finditer(text):
            _add(m.group(1), "class", m.start())
        for m in _PAT_FUNC_DEF.finditer(text):
            name = m.group(1) or m.group(2)
            if name:
                _add(name, "function", m.start())
        for m in _PAT_IMPORT.finditer(text):
            _add(m.group(1), "module", m.start())
        for m in _PAT_CAMEL.finditer(text):
            _add(m.group(1), "type_name", m.start())

    # Domain names from URLs
    for m in _PAT_URL_DOMAIN.finditer(text):
        domain = m.group(1)
        if domain and len(domain) > 4 and "." in domain:
            _add(domain, "domain", m.start())

    # Capitalised phrases (likely proper nouns / named entities)
    for m in _PAT_CAPS_PHRASE.finditer(text):
        phrase = m.group(1)
        # Filter out common false positives
        if len(phrase) >= 4 and not re.match(r"^(The|This|That|These|Those|Some|What|When|Where|How|Why)\b", phrase):
            _add(phrase, "named_entity", m.start(),
                 text[max(0, m.start()-30):m.end()+30])

    # Years of significance
    for m in _PAT_YEAR.finditer(text):
        yr = m.group(1)
        # Only include if near a meaningful word
        ctx = text[max(0, m.start()-40):m.end()+40].lower()
        if any(w in ctx for w in ["founded", "created", "established", "released",
                                   "launched", "born", "died", "started", "invented"]):
            _add(yr, "year", m.start(), ctx)

    return entities


def _extract_relationships_from_entities(entities: List[Dict], text: str) -> List[Dict]:
    """Infer relationships between co-occurring entities."""
    relations = []

    # Position-based co-occurrence: entities within 200 chars of each other
    for i, a in enumerate(entities):
        for j, b in enumerate(entities):
            if i >= j:
                continue
            dist = abs(a["position"] - b["position"])
            if dist < 200:
                # Determine relationship type from context
                between = text[min(a["position"], b["position"]):
                              max(a["position"], b["position"]) + len(b["name"])]
                rel = "CO_OCCURS"
                between_lc = between.lower()
                if any(w in between_lc for w in ["founded", "created", "started"]):
                    rel = "FOUNDED"
                elif any(w in between_lc for w in ["ceo of", "president of", "director of",
                                                     "leads", "manages"]):
                    rel = "LEADS"
                elif any(w in between_lc for w in ["uses", "built with", "powered by",
                                                     "based on", "leverages"]):
                    rel = "USES"
                elif any(w in between_lc for w in ["part of", "belongs to", "within",
                                                     "inside", "component of"]):
                    rel = "PART_OF"
                elif any(w in between_lc for w in ["in ", "at ", "located"]):
                    if a["type"] in ("person", "organisation") and b["type"] == "named_entity":
                        rel = "LOCATED_AT"
                elif a["type"] == "date" or b["type"] == "date" or a["type"] == "year" or b["type"] == "year":
                    rel = "DATED"

                relations.append({
                    "from_name": a["name"],
                    "from_type": a["type"],
                    "to_name": b["name"],
                    "to_type": b["type"],
                    "rel": rel,
                    "distance": dist,
                })

    return relations[:200]


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
            eid = f"{ent['type']}:{hashlib.sha1(norm.encode()).hexdigest()[:12]}"
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
                (eid, ent["name"], ent["type"], norm,
                 ent.get("mention_count", 1),
                 json.dumps(ds_list),
                 ts, ts,
                 json.dumps({
                     "record_ids": record_ids[:50],
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
            from_norm = _normalise_entity(rel["from_name"])
            to_norm = _normalise_entity(rel["to_name"])
            from_eid = f"{rel['from_type']}:{hashlib.sha1(from_norm.encode()).hexdigest()[:12]}"
            to_eid = f"{rel['to_type']}:{hashlib.sha1(to_norm.encode()).hexdigest()[:12]}"
            conn.execute(
                "INSERT OR REPLACE INTO fabric_entity_relations "
                "(from_id, to_id, rel, props, dataset_id, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (from_eid, to_eid, rel["rel"],
                 json.dumps({"distance": rel.get("distance", 0),
                             "from_name": rel["from_name"],
                             "to_name": rel["to_name"]}),
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
                eid = f"{ent['type']}:{hashlib.sha1(norm.encode()).hexdigest()[:12]}"
                await graph.upsert_node("Entity", eid, {
                    "name": ent["name"],
                    "type": ent["type"],
                    "normalised": norm,
                    "mention_count": ent.get("mention_count", 1),
                    "dataset_id": dataset_id,
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
                from_norm = _normalise_entity(rel["from_name"])
                to_norm = _normalise_entity(rel["to_name"])
                from_eid = f"{rel['from_type']}:{hashlib.sha1(from_norm.encode()).hexdigest()[:12]}"
                to_eid = f"{rel['to_type']}:{hashlib.sha1(to_norm.encode()).hexdigest()[:12]}"
                edges.append({
                    "from_label": "Entity", "from_id": from_eid,
                    "to_label": "Entity", "to_id": to_eid,
                    "rel": rel["rel"],
                    "props": {"distance": rel.get("distance", 0)},
                })
            if edges:
                await graph.link_many(edges)
        except Exception as e:
            log.warning("entity neo4j: %s", e)

    return persisted


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITY: ENTITY GRAPH QUERY
# ═══════════════════════════════════════════════════════════════════════════

@capability(
    "fabric.entity_graph.extract",
    http_method="POST", http_path="/fabric/entity_graph/extract",
    http_tags=["fabric", "graph", "entity"],
    memory="on",
    description="Extract a second-order entity graph from a dataset's records. "
                "Identifies people, organisations, dates, technologies, code symbols, "
                "and their relationships. Entities are normalised and cross-linked "
                "across records. "
                "Input: dataset_id (str!), limit (int default 100), "
                "content_type (text|code|web default text), "
                "persist (bool default True). "
                "Output: {entities, relations, persisted, dataset_id}.",
)
async def cap_entity_graph_extract(
    dataset_id:   str,
    limit:        int  = 100,
    content_type: str  = "text",
    persist:      bool = True,
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
    from Vera.Orchestration.data_fabric import _sqlite_query
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

    await _emit("extracting", count=len(records))

    all_entities: Dict[str, Dict] = {}
    all_relations: List[Dict] = []

    for rec in records:
        text = (rec.get("text") or "")[:8000]
        rid = rec["id"]

        entities = _extract_entities_from_text(text, content_type)
        relations = _extract_relationships_from_entities(entities, text)

        for ent in entities:
            norm = ent["normalised"]
            if norm in all_entities:
                all_entities[norm]["mention_count"] += 1
                all_entities[norm]["record_ids"].add(rid)
            else:
                all_entities[norm] = {
                    **ent,
                    "mention_count": 1,
                    "record_ids": {rid},
                    "datasets": {dataset_id},
                }

        all_relations.extend(relations)

    await _emit("scored",
                entities=len(all_entities), relations=len(all_relations))

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
            "normalised": norm,
            "mention_count": ent["mention_count"],
            "record_ids": list(ent["record_ids"])[:20],
        })
    entity_list.sort(key=lambda e: -e["mention_count"])

    await _emit("done",
                entities=len(entity_list), relations=len(all_relations),
                persisted=persisted)

    return {
        "ok": True,
        "dataset_id": dataset_id,
        "entities": entity_list[:200],
        "relations": all_relations[:300],
        "entity_count": len(entity_list),
        "relation_count": len(all_relations),
        "persisted": persisted,
    }


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


log.info("data_fabric_web_acquisition: capabilities registered")