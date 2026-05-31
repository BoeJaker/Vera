"""
fabric_discovery.py — Vera Data Fabric — Discovery & Sub-Source Detection
=========================================================================
A companion module that makes the data fabric's web/data discovery
*recursive and self-extending*. It layers three new capabilities on top of
the existing `fabric.web.acquire` crawler and `_detect_data_kind` /
`_ingest_data_payload` primitives in `fabric_web_acquisition.py`:

  1. INTERACTION-SURFACE DETECTION
     While crawling (or when re-scanning an existing dataset) it inspects
     every page for *other source types* it could consume — exactly the way
     a `Source` already has many types in the fabric. It detects:
        • RSS / Atom feeds          (<link rel=alternate>, /feed, .rss)
        • Sitemaps                  (sitemap.xml, robots.txt Sitemap:)
        • Git hosting               (github / gitlab / gitea repo links, *.git)
        • OpenAPI / Swagger specs   (openapi.json, swagger.json, /api-docs…)
        • GraphQL endpoints         (/graphql)
        • Generic JSON APIs         (/api/…, *.json)
        • Data files                (*.csv *.tsv *.jsonl *.ndjson)
        • DB connection hints       (postgres:// mysql:// mongodb:// …)
     Each detected surface is stored, scored against the active topic, and can
     be *promoted* into a real recurring `fabric.sources.add` source — so the
     fabric can then keep pulling it like any other source.

  2. CONCEPT → SUB-TABLE EXTRACTION
     It detects structured *concepts embedded inside a page* and pulls them in
     as sub-tables (sub-datasets) of the page's dataset, with inferred schema:
        • HTML tables               → e.g. a Pokédex, a stats table, a price list
        • API-endpoint tables/blocks→ method + path rows from API docs
        • CLI flag lists            → "flags for a command" (--flag / -f + help)
        • Definition lists (<dl>)   → term / definition rows
     Each becomes  <parent>.table.<slug>  and is linked HAS_SUBTABLE in the graph.

  3. RESUMABLE, DATASET-SEEDED CRAWLING
     A discovery crawl persists its frontier (queue + visited) so it can be
     continued later, and it can be *seeded from an existing dataset* — loading
     the URLs and link structure already scanned and continuing from there.

Capabilities
────────────
  fabric.discover.crawl        Resumable discovery crawl (surfaces + sub-tables)
  fabric.discover.continue     Resume a crawl from its saved frontier
  fabric.discover.from_dataset Seed a crawl from an existing dataset and continue
  fabric.discover.detect       One-shot: analyse a single URL (no crawl)
  fabric.surfaces.list         List detected interaction surfaces
  fabric.surfaces.promote      Promote a surface into a recurring fabric source
  fabric.surfaces.delete       Forget a detected surface
  fabric.subtables.list        List extracted sub-tables

The module is import-safe and degrades gracefully: BeautifulSoup is optional
(regex fallbacks are used), and all backend touches are wrapped defensively.

Loading
───────
Add this file alongside the other fabric modules. It is loaded the same way as
`fabric_web_acquisition.py`: either append its path to the `_module_files` list
in `capability_orchestration.py` (right after fabric_web_acquisition.py), or set

    VERA_MODULES="/path/to/fabric_discovery.py"

It must load AFTER data_fabric.py and fabric_web_acquisition.py (it imports both).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import httpx

from Vera.Orchestration.capability_orchestration import (
    capability, emit_event, now_iso, register_ui, CAPABILITY_REGISTRY,
)

log = logging.getLogger("vera.fabric_discovery")

# ── Tunables ──────────────────────────────────────────────────────────────
_CRAWL_DELAY      = float(os.getenv("FABRIC_DISCOVER_DELAY_S", os.getenv("FABRIC_CRAWL_DELAY_S", "2")))
_MAX_SUBTABLE_ROWS = int(os.getenv("FABRIC_SUBTABLE_MAX_ROWS", "500"))
_SPEC_FETCH_MAX    = int(os.getenv("FABRIC_SPEC_FETCH_BYTES", "2000000"))

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


# ── Lazy bridges into the sibling fabric modules ────────────────────────────
def _df():
    import Vera.Orchestration.fabric.data_fabric as df
    return df

def _wa():
    import Vera.Orchestration.fabric.fabric_web_acquisition as wa
    return wa

def _ingest():
    return _df().ingest_dataset

def _sqlite_conn():
    return _df()._sqlite_conn()

def _get_graph(name: str = "fabric"):
    try:
        graphs = _df().GRAPHS
        return graphs.get(name) or graphs.get("fabric")
    except Exception:
        return None

def _infer_schema(rows):
    try:
        return _df().infer_schema(rows)
    except Exception:
        return {}

def _source_types() -> Dict[str, Dict]:
    try:
        return _df().SOURCE_TYPES
    except Exception:
        return {}


def _soup(html: str):
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(html[:400000], "html.parser")
    except Exception:
        return None


# ── Entity extraction — reuse the original web-acquisition extractor ────────
def _wa_entity_fns():
    """Return (extract_entities, extract_relations, persist) from the original
    module, or (None, None, None) if unavailable."""
    try:
        wa = _wa()
        return (wa._extract_entities_from_text,
                wa._extract_relationships_from_entities,
                wa._persist_entity_graph)
    except Exception:
        return (None, None, None)


async def extract_entities_for_record(text: str, record_id: str, dataset_id: str,
                                      content_type: str = "web") -> int:
    """Extract people / orgs / dates / technologies / code symbols from one page
    and persist them (+ co-occurrence relations) into the shared entity graph
    tables, reusing the original engine. Returns the number of entities found."""
    extract, relate, persist = _wa_entity_fns()
    if not (extract and relate and persist) or not text:
        return 0
    try:
        ents = extract(text[:8000], content_type)
        if not ents:
            return 0
        rels = relate(ents, text[:8000])
        bag: Dict[str, Dict] = {}
        for e in ents:
            bag[e["normalised"]] = {**e, "mention_count": 1,
                                    "record_ids": {record_id}, "datasets": {dataset_id}}
        await persist(bag, rels, dataset_id)
        return len(bag)
    except Exception as e:
        log.debug("entity extract %s: %s", record_id, e)
        return 0


def _slug(text: str, fallback: str = "table") -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return (s[:40] or fallback)


def _abs(base: str, href: str) -> str:
    try:
        return urljoin(base, href).split("#")[0]
    except Exception:
        return href


# ═══════════════════════════════════════════════════════════════════════════
# PERSISTENCE  — surfaces, sub-tables, and resumable crawl frontiers
# ═══════════════════════════════════════════════════════════════════════════

_TABLES_READY = False

def _ensure_tables():
    global _TABLES_READY
    if _TABLES_READY:
        return
    try:
        conn = _sqlite_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS fabric_surfaces (
                id             TEXT PRIMARY KEY,
                crawl_id       TEXT,
                parent_dataset TEXT,
                from_url       TEXT,
                kind           TEXT,          -- rss|sitemap|github|gitlab|gitea|openapi|graphql|json_api|csv|json|jsonl|db
                source_type    TEXT,          -- maps to data_fabric SOURCE_TYPES (or '' if not directly addable)
                url            TEXT,
                label          TEXT,
                confidence     REAL DEFAULT 0,
                topic_score    REAL DEFAULT 0,
                why            TEXT,
                config         TEXT,          -- JSON config to hand to fabric.sources.add
                promoted       INTEGER DEFAULT 0,
                source_id      TEXT DEFAULT '',
                created_at     TEXT,
                updated_at     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_surf_parent ON fabric_surfaces(parent_dataset);
            CREATE INDEX IF NOT EXISTS idx_surf_kind   ON fabric_surfaces(kind);
            CREATE INDEX IF NOT EXISTS idx_surf_crawl  ON fabric_surfaces(crawl_id);

            CREATE TABLE IF NOT EXISTS fabric_subtables (
                id             TEXT PRIMARY KEY,
                crawl_id       TEXT,
                parent_dataset TEXT,
                sub_dataset    TEXT,
                from_url       TEXT,
                kind           TEXT,          -- table|api_endpoints|cli_flags|definitions
                title          TEXT,
                columns        TEXT,          -- JSON list
                schema         TEXT,          -- JSON
                row_count      INTEGER DEFAULT 0,
                created_at     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_subt_parent ON fabric_subtables(parent_dataset);
            CREATE INDEX IF NOT EXISTS idx_subt_crawl  ON fabric_subtables(crawl_id);

            CREATE TABLE IF NOT EXISTS fabric_discovery_frontier (
                id             TEXT PRIMARY KEY,
                dataset_id     TEXT,
                seed_url       TEXT,
                status         TEXT DEFAULT 'running',   -- running|paused|done|error
                config         TEXT,         -- JSON crawl config
                queue          TEXT,         -- JSON list of [url, depth, dry_streak]
                visited        TEXT,         -- JSON list of urls
                pages_fetched  INTEGER DEFAULT 0,
                surfaces_found INTEGER DEFAULT 0,
                subtables_found INTEGER DEFAULT 0,
                created_at     TEXT,
                updated_at     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_front_ds ON fabric_discovery_frontier(dataset_id);

            CREATE TABLE IF NOT EXISTS fabric_discovery_edges (
                id             TEXT PRIMARY KEY,   -- sha1(crawl:from:to:rel)
                crawl_id       TEXT,
                parent_dataset TEXT,
                from_id        TEXT,    -- node id (page url / dataset id)
                to_id          TEXT,    -- node id (page url / surface id / sub-dataset id)
                rel            TEXT,    -- HAS_PAGE|LINKS_TO|HAS_SURFACE|HAS_SUBTABLE|HAS_DATA_SUBSET
                to_label       TEXT,    -- Page|Dataset|Surface
                created_at     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_edge_crawl  ON fabric_discovery_edges(crawl_id);
            CREATE INDEX IF NOT EXISTS idx_edge_parent ON fabric_discovery_edges(parent_dataset);
        """)
        conn.commit()
        _TABLES_READY = True
    except Exception as e:
        log.debug("discovery tables: %s", e)


def _store_surface(s: Dict) -> bool:
    """Insert/refresh a surface. Idempotent on (kind,url) via deterministic id."""
    _ensure_tables()
    try:
        conn = _sqlite_conn()
        # Preserve promotion state if the surface already exists
        existing = conn.execute(
            "SELECT promoted, source_id FROM fabric_surfaces WHERE id=?", (s["id"],)
        ).fetchone()
        promoted  = existing["promoted"] if existing else 0
        source_id = existing["source_id"] if existing else ""
        conn.execute(
            "INSERT OR REPLACE INTO fabric_surfaces "
            "(id, crawl_id, parent_dataset, from_url, kind, source_type, url, label, "
            " confidence, topic_score, why, config, promoted, source_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (s["id"], s.get("crawl_id", ""), s.get("parent_dataset", ""),
             s.get("from_url", ""), s.get("kind", ""), s.get("source_type", ""),
             s.get("url", ""), s.get("label", ""),
             float(s.get("confidence", 0)), float(s.get("topic_score", 0)),
             s.get("why", ""), json.dumps(s.get("config", {})),
             promoted, source_id,
             (existing and now_iso()) or now_iso(), now_iso()),
        )
        conn.commit()
        return True
    except Exception as e:
        log.debug("store surface: %s", e)
        return False


def _store_subtable(t: Dict):
    _ensure_tables()
    try:
        conn = _sqlite_conn()
        conn.execute(
            "INSERT OR REPLACE INTO fabric_subtables "
            "(id, crawl_id, parent_dataset, sub_dataset, from_url, kind, title, "
            " columns, schema, row_count, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (t["id"], t.get("crawl_id", ""), t.get("parent_dataset", ""),
             t.get("sub_dataset", ""), t.get("from_url", ""), t.get("kind", ""),
             t.get("title", ""), json.dumps(t.get("columns", [])),
             json.dumps(t.get("schema", {})), int(t.get("row_count", 0)), now_iso()),
        )
        conn.commit()
    except Exception as e:
        log.debug("store subtable: %s", e)


def _record_edge(crawl_id: str, parent_ds: str, from_id: str, to_id: str,
                 rel: str, to_label: str = "Page"):
    """Persist a discovery graph edge in SQLite so the map can be rebuilt for
    history even when Neo4j is unavailable. Idempotent on (crawl,from,to,rel)."""
    if not from_id or not to_id or from_id == to_id:
        return
    _ensure_tables()
    try:
        eid = "e_" + hashlib.sha1(f"{crawl_id}|{from_id}|{to_id}|{rel}".encode()).hexdigest()[:20]
        conn = _sqlite_conn()
        conn.execute(
            "INSERT OR IGNORE INTO fabric_discovery_edges "
            "(id, crawl_id, parent_dataset, from_id, to_id, rel, to_label, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (eid, crawl_id, parent_ds, from_id, to_id, rel, to_label, now_iso()),
        )
        conn.commit()
    except Exception as e:
        log.debug("record edge: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
# SURFACE DETECTION ENGINE
# ═══════════════════════════════════════════════════════════════════════════
#
# Given a fetched page (url, content-type, raw html, and the already-extracted
# structure dict from _extract_page_structure), return a list of candidate
# "interaction surfaces" — other source types the fabric could consume. Each is
# mapped onto the existing data_fabric SOURCE_TYPES taxonomy where one fits, so
# it can be promoted into a recurring source with one call.

# Hosts/owners on github that are NOT user repos
_GH_RESERVED = {
    "features", "about", "topics", "sponsors", "collections", "marketplace",
    "login", "join", "settings", "notifications", "explore", "orgs", "users",
    "pulls", "issues", "search", "new", "apps", "site", "contact", "pricing",
    "readme", "security", "customer-stories", "enterprise", "team", "nonprofit",
}

_DB_SCHEMES = {
    "postgres": "postgres", "postgresql": "postgres",
    "mysql": "mysql", "mariadb": "mysql",
    "mongodb": "mongodb", "mongodb+srv": "mongodb",
    "sqlite": "sqlite",
}

_RX_DB_URI    = re.compile(
    r"\b(postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|sqlite|redis|mssql|clickhouse)"
    r"://[^\s\"'<>)\]]+", re.I)
_RX_GITHUB    = re.compile(r"^https?://github\.com/([^/?#]+)/([^/?#]+)", re.I)
_RX_GITLAB    = re.compile(r"^https?://(?:[\w.-]*\.)?gitlab\.[\w.]+/([^?#]+)", re.I)
_RX_DOTGIT    = re.compile(r"^https?://[^\s\"'<>]+\.git$", re.I)
_OPENAPI_HINT = re.compile(r"(openapi\.(?:json|ya?ml)|swagger\.(?:json|ya?ml)|"
                           r"/v[0-9]+/api-docs|/api-docs|/swagger(?:-ui)?|/redoc)", re.I)


def _normalise_surface_url(u: str) -> str:
    return (u or "").split("#")[0].rstrip("/")


def _mk_surface(*, parent_dataset, from_url, kind, source_type, url,
                label, confidence, why, config) -> Dict:
    nu = _normalise_surface_url(url)
    sid = "surf_" + hashlib.sha1(f"{parent_dataset}|{kind}|{nu}".encode()).hexdigest()[:18]
    return {
        "id": sid, "parent_dataset": parent_dataset, "from_url": from_url,
        "kind": kind, "source_type": source_type, "url": nu, "label": label,
        "confidence": confidence, "why": why, "config": config or {},
        "topic_score": 0.0,
    }


def detect_surfaces(html: str, url: str, structure: Dict,
                    parent_dataset: str = "", headers: Dict = None) -> List[Dict]:
    """Detect interaction surfaces reachable from this page.

    Returns a de-duplicated list of surface dicts. Pure (no I/O)."""
    headers = headers or {}
    out: Dict[str, Dict] = {}

    def add(s: Dict):
        # Keep the highest-confidence detection for a given id
        cur = out.get(s["id"])
        if cur is None or s["confidence"] > cur["confidence"]:
            s["parent_dataset"] = parent_dataset
            s["from_url"] = url
            out[s["id"]] = s

    soup = _soup(html)
    links = structure.get("links", []) if structure else []
    full_text = (structure or {}).get("full_text", "") or ""

    # ── 1. RSS / Atom autodiscovery via <link rel=alternate> ────────────────
    if soup is not None:
        for ln in soup.find_all("link", rel=True):
            rels = ln.get("rel") or []
            typ = (ln.get("type") or "").lower()
            href = ln.get("href") or ""
            if not href:
                continue
            if "alternate" in [r.lower() for r in rels] and \
               ("rss" in typ or "atom" in typ or "xml" in typ and "feed" in (ln.get("title", "") or "").lower()):
                add(_mk_surface(
                    parent_dataset=parent_dataset, from_url=url, kind="rss",
                    source_type="rss", url=_abs(url, href),
                    label=ln.get("title") or "RSS / Atom feed",
                    confidence=0.95, why="<link rel=alternate> feed autodiscovery",
                    config={}))

    # ── 2. Link-derived surfaces (git, feeds, specs, apis, data files) ───────
    for ln in links:
        href = ln.get("url") or ""
        anchor = (ln.get("anchor") or "").strip()
        if not href:
            continue
        low = href.lower()

        # Git hosting
        m = _RX_GITHUB.match(href)
        if m:
            owner, repo = m.group(1), m.group(2)
            if owner.lower() not in _GH_RESERVED and repo and not repo.startswith("."):
                repo = repo[:-4] if repo.endswith(".git") else repo
                add(_mk_surface(
                    parent_dataset=parent_dataset, from_url=url, kind="github",
                    source_type="github", url=f"https://github.com/{owner}/{repo}",
                    label=f"GitHub: {owner}/{repo}", confidence=0.9,
                    why="GitHub repository link",
                    config={"base_url": "https://api.github.com", "owner": owner,
                            "repo": repo, "mode": "issues", "state": "open"}))
                continue
        m = _RX_GITLAB.match(href)
        if m:
            path = m.group(1).strip("/")
            # project path = first two segments (group/project) when present
            segs = [p for p in path.split("/") if p and p not in ("-",)]
            if segs and segs[0] not in ("explore", "users", "help", "dashboard"):
                host = urlparse(href).scheme + "://" + urlparse(href).netloc
                proj = "/".join(segs[:2]) if len(segs) >= 2 else segs[0]
                add(_mk_surface(
                    parent_dataset=parent_dataset, from_url=url, kind="gitlab",
                    source_type="gitlab", url=href, label=f"GitLab: {proj}",
                    confidence=0.7, why="GitLab project link",
                    config={"base_url": host, "project_id": proj,
                            "mode": "issues", "state": "opened"}))
                continue
        if _RX_DOTGIT.match(href):
            add(_mk_surface(
                parent_dataset=parent_dataset, from_url=url, kind="git",
                source_type="", url=href, label=f"git: {href.split('/')[-1]}",
                confidence=0.6, why="bare .git repository URL", config={}))
            continue

        # OpenAPI / Swagger / Redoc
        if _OPENAPI_HINT.search(low) or "openapi" in anchor.lower() or "swagger" in anchor.lower():
            add(_mk_surface(
                parent_dataset=parent_dataset, from_url=url, kind="openapi",
                source_type="api", url=_abs(url, href),
                label=anchor or "OpenAPI / Swagger spec", confidence=0.85,
                why="OpenAPI/Swagger specification link",
                config={"jq_path": "paths", "spec": True}))
            continue

        # GraphQL
        if low.rstrip("/").endswith("/graphql"):
            add(_mk_surface(
                parent_dataset=parent_dataset, from_url=url, kind="graphql",
                source_type="api", url=_abs(url, href), label="GraphQL endpoint",
                confidence=0.7, why="GraphQL endpoint link", config={}))
            continue

        # Feeds by path / extension / anchor
        if (low.endswith((".rss", ".atom")) or "/feed" in low or low.endswith("/rss") or
                anchor.lower() in ("rss", "atom", "feed", "subscribe (rss)")):
            add(_mk_surface(
                parent_dataset=parent_dataset, from_url=url, kind="rss",
                source_type="rss", url=_abs(url, href),
                label=anchor or "RSS feed", confidence=0.75,
                why="feed-like link path/anchor", config={}))
            continue

        # Sitemaps
        if low.endswith("sitemap.xml") or low.endswith("sitemap_index.xml") or "/sitemap" in low and low.endswith(".xml"):
            add(_mk_surface(
                parent_dataset=parent_dataset, from_url=url, kind="sitemap",
                source_type="", url=_abs(url, href), label="XML sitemap",
                confidence=0.8, why="sitemap link", config={}))
            continue

        # Data files
        if low.endswith((".csv", ".tsv")):
            add(_mk_surface(
                parent_dataset=parent_dataset, from_url=url, kind="csv",
                source_type="", url=_abs(url, href),
                label=anchor or "CSV data file", confidence=0.7,
                why="CSV/TSV data file link", config={}))
            continue
        if low.endswith((".jsonl", ".ndjson")):
            add(_mk_surface(
                parent_dataset=parent_dataset, from_url=url, kind="jsonl",
                source_type="", url=_abs(url, href),
                label=anchor or "JSONL data file", confidence=0.7,
                why="JSON-Lines data file link", config={}))
            continue
        if low.endswith(".json"):
            add(_mk_surface(
                parent_dataset=parent_dataset, from_url=url, kind="json_api",
                source_type="api", url=_abs(url, href),
                label=anchor or "JSON document", confidence=0.55,
                why="JSON document/endpoint link", config={"jq_path": ""}))
            continue

        # Generic /api/ endpoints
        if "/api/" in low or low.rstrip("/").endswith("/api"):
            add(_mk_surface(
                parent_dataset=parent_dataset, from_url=url, kind="json_api",
                source_type="api", url=_abs(url, href),
                label=anchor or "API endpoint", confidence=0.45,
                why="path contains /api/", config={"jq_path": ""}))
            continue

    # ── 3. Embedded swagger-ui / redoc on this very page ────────────────────
    if soup is not None:
        if soup.find(id=re.compile(r"swagger-ui|redoc", re.I)) or \
           soup.find(attrs={"class": re.compile(r"swagger-ui|redoc", re.I)}):
            # Try common spec locations relative to this page
            guess = _abs(url, "openapi.json")
            add(_mk_surface(
                parent_dataset=parent_dataset, from_url=url, kind="openapi",
                source_type="api", url=guess, label="Embedded API explorer",
                confidence=0.5, why="page embeds Swagger-UI / Redoc",
                config={"jq_path": "paths", "spec": True}))

    # ── 4. DB connection-string hints (low confidence, never auto-promoted) ──
    for m in _RX_DB_URI.finditer(full_text[:20000]):
        raw = m.group(0)
        scheme = m.group(1).lower()
        st = _DB_SCHEMES.get(scheme, "")
        if not st:
            continue  # redis/mssql/clickhouse have no direct source type
        try:
            p = urlparse(raw)
            cfg = {"host": p.hostname or "", "port": p.port or "",
                   "database": (p.path or "").lstrip("/")}
        except Exception:
            cfg = {}
        add(_mk_surface(
            parent_dataset=parent_dataset, from_url=url, kind="db",
            source_type=st, url=raw, label=f"{st} connection ({cfg.get('host','?')})",
            confidence=0.25, why="connection string found in page text (needs auth)",
            config=cfg))

    return list(out.values())


# ═══════════════════════════════════════════════════════════════════════════
# CONCEPT → SUB-TABLE EXTRACTION ENGINE
# ═══════════════════════════════════════════════════════════════════════════
#
# Detect structured concepts embedded in a page and turn them into row records
# suitable for ingestion into a sub-dataset. Returns a list of:
#   {kind, title, columns:[...], records:[{...}], confidence}
# where records are plain dicts (one per row). No I/O.

_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_RX_ENDPOINT  = re.compile(
    r"\b(" + "|".join(_HTTP_METHODS) + r")\s+(/[A-Za-z0-9_\-./{}:<>]+)")
_RX_FLAG      = re.compile(r"(?:^|[\s(,])(--[A-Za-z][A-Za-z0-9-]+|-[A-Za-z])(?=[\s=,)\]]|$)")
_API_HEADER_HINTS  = {"endpoint", "method", "path", "route", "verb", "resource", "operation"}
_FLAG_HEADER_HINTS = {"flag", "option", "argument", "arg", "switch", "parameter", "param"}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _classify_table(headers: List[str], rows: List[List[str]]) -> str:
    hset = {h.lower() for h in headers}
    if hset & _API_HEADER_HINTS:
        # endpoint table only if a column actually carries paths/methods
        joined = " ".join(c for r in rows[:8] for c in r).upper()
        if any(meth in joined for meth in _HTTP_METHODS) or "/" in joined:
            return "api_endpoints"
    if hset & _FLAG_HEADER_HINTS:
        joined = " ".join(c for r in rows[:8] for c in r)
        if "--" in joined or re.search(r"(?:^|\s)-[A-Za-z]\b", joined):
            return "cli_flags"
    return "table"


def _extract_html_tables(soup, url: str) -> List[Dict]:
    out: List[Dict] = []
    if soup is None:
        return out
    for tbl in soup.find_all("table"):
        # Skip nested tables — only treat the outermost as a sub-table
        if tbl.find_parent("table") is not None:
            continue
        headers: List[str] = []
        thead = tbl.find("thead")
        tbody = tbl.find("tbody")
        all_trs = tbl.find_all("tr")
        if thead:
            hr = thead.find("tr")
            if hr:
                headers = [_clean(c.get_text(" ")) for c in hr.find_all(["th", "td"])]
        # Candidate body rows: prefer <tbody>; else all rows NOT inside <thead>
        if tbody is not None:
            body_trs = tbody.find_all("tr")
        else:
            body_trs = [tr for tr in all_trs if tr.find_parent("thead") is None]
        # If no <thead>, promote the first body row to header if it's all <th>
        if not headers and body_trs:
            first = body_trs[0]
            if first.find_all("th") and not first.find_all("td"):
                headers = [_clean(c.get_text(" ")) for c in first.find_all("th")]
                body_trs = body_trs[1:]
        body_rows = []
        for tr in body_trs:
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            body_rows.append([_clean(c.get_text(" ")) for c in cells])
        # Need a real header and at least 2 data rows of >=2 cols
        body_rows = [r for r in body_rows if any(r)]
        if not headers or len(body_rows) < 2:
            continue
        ncol = max(len(headers), max((len(r) for r in body_rows), default=0))
        if ncol < 2:
            continue
        # Normalise / fabricate column names
        cols = []
        for i in range(ncol):
            h = headers[i] if i < len(headers) and headers[i] else f"col{i+1}"
            base = _slug(h, f"col{i+1}")
            c = base
            n = 2
            while c in cols:
                c = f"{base}_{n}"; n += 1
            cols.append(c)
        records = []
        for ri, r in enumerate(body_rows[:_MAX_SUBTABLE_ROWS]):
            rec = {cols[i]: r[i] for i in range(min(len(cols), len(r))) if r[i]}
            if not rec:
                continue
            rec["text"] = " | ".join(f"{cols[i]}={r[i]}"
                                     for i in range(min(len(cols), len(r))) if r[i])[:4000]
            rec["_row"] = ri
            records.append(rec)
        if len(records) < 2:
            continue
        # Title: caption, or nearest preceding heading
        title = ""
        cap = tbl.find("caption")
        if cap:
            title = _clean(cap.get_text(" "))
        if not title:
            prev = tbl.find_previous(re.compile(r"^h[1-6]$"))
            if prev:
                title = _clean(prev.get_text(" "))[:80]
        title = title or "table"
        kind = _classify_table([c for c in headers if c], [r for r in body_rows[:8]])
        out.append({"kind": kind, "title": title, "columns": cols,
                    "records": records, "confidence": 0.8})
    return out


def _extract_definition_lists(soup) -> List[Dict]:
    out: List[Dict] = []
    if soup is None:
        return out
    for dl in soup.find_all("dl"):
        terms = dl.find_all("dt")
        defs = dl.find_all("dd")
        if len(terms) < 2:
            continue
        records = []
        is_flags = False
        for i, dt in enumerate(terms[:_MAX_SUBTABLE_ROWS]):
            term = _clean(dt.get_text(" "))
            desc = _clean(defs[i].get_text(" ")) if i < len(defs) else ""
            if not term:
                continue
            if term.startswith("-"):
                is_flags = True
            records.append({"term": term, "definition": desc[:2000],
                            "text": f"{term}: {desc}"[:4000]})
        if len(records) < 2:
            continue
        if is_flags:
            for r in records:
                flags = _RX_FLAG.findall(r["term"])
                r["flag"] = next((f for f in flags if f.startswith("--")), flags[0] if flags else r["term"])
                r["description"] = r.pop("definition", "")
                r.pop("term", None)
            out.append({"kind": "cli_flags", "title": "command flags",
                        "columns": ["flag", "description"], "records": records,
                        "confidence": 0.75})
        else:
            out.append({"kind": "definitions", "title": "definitions",
                        "columns": ["term", "definition"], "records": records,
                        "confidence": 0.6})
    return out


def _extract_api_endpoints_from_text(structure: Dict) -> List[Dict]:
    """Pull `GET /v1/foo` style endpoints out of code blocks and body text."""
    seen: Set[Tuple[str, str]] = set()
    records = []
    sources = []
    for cb in (structure or {}).get("code_blocks", []) or []:
        sources.append(cb.get("text", ""))
    sources.append((structure or {}).get("full_text", "")[:20000])
    for src in sources:
        for m in _RX_ENDPOINT.finditer(src or ""):
            method, path = m.group(1).upper(), m.group(2)
            if len(path) < 2 or (method, path) in seen:
                continue
            seen.add((method, path))
            records.append({"method": method, "path": path,
                            "text": f"{method} {path}"})
            if len(records) >= _MAX_SUBTABLE_ROWS:
                break
    if len(records) < 2:
        return []
    return [{"kind": "api_endpoints", "title": "api endpoints",
             "columns": ["method", "path"], "records": records, "confidence": 0.65}]


def _extract_cli_flags_from_text(structure: Dict) -> List[Dict]:
    """Pull `--flag   description` style option lists from <pre>/usage blocks."""
    records = []
    seen: Set[str] = set()
    blocks = [cb.get("text", "") for cb in (structure or {}).get("code_blocks", []) or []]
    for block in blocks:
        for line in (block or "").splitlines():
            mm = _RX_FLAG.findall(line)
            longs = [f for f in mm if f.startswith("--")]
            if not longs:
                continue
            flag = longs[0]
            if flag in seen:
                continue
            seen.add(flag)
            # description = text after the flag cluster
            after = line
            for f in mm:
                after = after.replace(f, " ", 1)
            desc = _clean(re.sub(r"^[\s=,<>\[\]A-Z_]+", "", after))
            alias = next((f for f in mm if re.fullmatch(r"-[A-Za-z]", f)), "")
            records.append({"flag": flag, "alias": alias,
                            "description": desc[:500],
                            "text": f"{flag} {alias} {desc}".strip()})
            if len(records) >= _MAX_SUBTABLE_ROWS:
                break
    if len(records) < 2:
        return []
    return [{"kind": "cli_flags", "title": "command flags",
             "columns": ["flag", "alias", "description"], "records": records,
             "confidence": 0.6}]


def extract_subtables(html: str, url: str, structure: Dict) -> List[Dict]:
    """Detect structured concepts embedded in the page. Pure (no I/O).

    Returns list of {kind, title, columns, records, confidence}."""
    soup = _soup(html)
    found: List[Dict] = []
    try:
        found.extend(_extract_html_tables(soup, url))
    except Exception as e:
        log.debug("table extract %s: %s", url, e)
    try:
        found.extend(_extract_definition_lists(soup))
    except Exception as e:
        log.debug("dl extract %s: %s", url, e)
    # Only mine text/code for endpoints & flags if a table didn't already do it
    have_endpoints = any(f["kind"] == "api_endpoints" for f in found)
    have_flags     = any(f["kind"] == "cli_flags" for f in found)
    if not have_endpoints:
        try:
            found.extend(_extract_api_endpoints_from_text(structure))
        except Exception as e:
            log.debug("endpoint extract %s: %s", url, e)
    if not have_flags:
        try:
            found.extend(_extract_cli_flags_from_text(structure))
        except Exception as e:
            log.debug("flag extract %s: %s", url, e)
    return found


# ═══════════════════════════════════════════════════════════════════════════
# INGESTION + GRAPH WIRING
# ═══════════════════════════════════════════════════════════════════════════

async def _link_graph(parent_ds: str, child_id: str, rel: str,
                      child_label: str = "Dataset", child_props: Dict = None):
    try:
        graph = _get_graph()
        if not (graph and graph.available):
            return
        await graph.upsert_node("Dataset", parent_ds, {"id": parent_ds})
        await graph.upsert_node(child_label, child_id, {"id": child_id, **(child_props or {})})
        await graph.link("Dataset", parent_ds, child_label, child_id, rel=rel)
    except Exception as e:
        log.debug("graph link %s-%s: %s", parent_ds, child_id, e)


async def ingest_subtable(parent_ds: str, sub: Dict, from_url: str,
                          crawl_id: str = "", extra_tags: List[str] = None) -> Tuple[str, int]:
    """Ingest one detected sub-table into  <parent>.table.<slug>  and link it."""
    extra_tags = extra_tags or []
    records = sub.get("records", [])
    if len(records) < 2:
        return "", 0
    slug = _slug(sub.get("title") or sub.get("kind") or "table")
    sub_ds = f"{parent_ds}.table.{sub.get('kind', 'table')}.{slug}"[:120]
    tags = list({*extra_tags, "subtable", sub.get("kind", "table"), "data"})
    try:
        await _ingest()(sub_ds, records, source="fabric_discovery", tags=tags)
    except Exception as e:
        log.warning("ingest subtable %s: %s", sub_ds, e)
        return sub_ds, 0
    schema = _infer_schema(records)
    await _link_graph(parent_ds, sub_ds, "HAS_SUBTABLE",
                      child_props={"kind": sub.get("kind", "table"),
                                   "title": (sub.get("title") or "")[:80],
                                   "rows": len(records)})
    _store_subtable({
        "id": "subt_" + hashlib.sha1(f"{sub_ds}|{from_url}".encode()).hexdigest()[:18],
        "crawl_id": crawl_id, "parent_dataset": parent_ds, "sub_dataset": sub_ds,
        "from_url": from_url, "kind": sub.get("kind", "table"),
        "title": sub.get("title", ""), "columns": sub.get("columns", []),
        "schema": schema, "row_count": len(records),
    })
    await emit_event({"type": "fabric.discover.subtable", "parent_dataset": parent_ds,
                      "sub_dataset": sub_ds, "kind": sub.get("kind", "table"),
                      "title": (sub.get("title") or "")[:80], "rows": len(records),
                      "from_url": from_url, "crawl_id": crawl_id})
    return sub_ds, len(records)


# ── Topic scoring ───────────────────────────────────────────────────────────

def _topic_tokens(topic: str) -> List[str]:
    return [t for t in re.split(r"\s+", (topic or "").lower()) if len(t) >= 3]


def _score_topic(text: str, tokens: List[str]) -> float:
    if not tokens:
        return 0.0
    tl = (text or "").lower()
    hits = sum(1 for t in tokens if t in tl)
    return round(hits / len(tokens), 3)


# ═══════════════════════════════════════════════════════════════════════════
# SURFACE PROMOTION  — turn a detected surface into a real fabric source
# ═══════════════════════════════════════════════════════════════════════════

async def _sources_add(**kw):
    from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY
    cap = CAPABILITY_REGISTRY.get("fabric.sources.add")
    if not cap:
        return {"error": "fabric.sources.add capability unavailable"}
    fn = cap.get("raw") or cap.get("func")   # raw = deterministic, no dispatch/event overhead
    try:
        res = await fn(**kw, trace_id=None)
        return res if isinstance(res, dict) else {"result": res}
    except TypeError as e:
        # Retry without any kwargs the function doesn't accept
        try:
            import inspect
            sig = inspect.signature(fn)
            ok = {k: v for k, v in kw.items() if k in sig.parameters}
            res = await fn(**ok, trace_id=None)
            return res if isinstance(res, dict) else {"result": res}
        except Exception as e2:
            return {"error": f"sources.add failed: {e2}"}
    except Exception as e:
        return {"error": f"sources.add failed: {e}"}


async def promote_surface(surface_row: Dict, auto_pull: bool = False) -> Dict:
    """Register a detected surface as a recurring fabric source (or, for plain
    data files / sitemaps, ingest it once into a sub-dataset)."""
    kind   = surface_row.get("kind", "")
    st     = surface_row.get("source_type", "")
    url    = surface_row.get("url", "")
    parent = surface_row.get("parent_dataset", "") or "discovered"
    label  = surface_row.get("label", "") or url
    try:
        config = surface_row.get("config")
        if isinstance(config, str):
            config = json.loads(config or "{}")
    except Exception:
        config = {}

    # Data files & sitemaps: no recurring source type — ingest once via the
    # web-acquisition data payload helper into a sub-dataset.
    if kind in ("csv", "jsonl", "sitemap") or (kind == "json_api" and url.lower().endswith(".json") and not config):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                          headers=_HTTP_HEADERS) as c:
                r = await c.get(url)
                r.raise_for_status()
                body = r.text
            data_kind = {"csv": "csv", "jsonl": "jsonl", "sitemap": "sitemap"}.get(kind, "json")
            sub_ds, n = await _wa()._ingest_data_payload(
                url, body, data_kind, parent, urlparse(url).netloc,
                ["discovered", kind], _ingest())
            _mark_promoted(surface_row.get("id", ""), "(ingested)")
            return {"ok": True, "ingested": n, "dataset_id": sub_ds, "mode": "one_shot"}
        except Exception as e:
            return {"error": f"one-shot ingest failed: {e}"}

    if not st:
        return {"error": f"surface kind '{kind}' has no addable source type "
                         f"(informational only)"}

    # Build a source registration matching data_fabric SOURCE_TYPES
    ds_id = parent if kind not in ("github", "gitlab", "gitea") else \
        f"{parent}.repo.{_slug(config.get('repo') or config.get('project_id') or label)}"
    add_kw = {
        "url": url if st in ("rss", "api", "wiki", "scrape", "recon") else "",
        "source_type": st,
        "label": label[:60],
        "dataset_id": ds_id,
        "interval": 0,                       # manual by default — user can schedule
        "tags": "discovered," + kind,
        "config": json.dumps(config or {}),
        "enabled": kind != "db",             # db hints land disabled (need auth)
    }
    if st == "api" and config.get("jq_path") is not None:
        add_kw["jq_path"] = config.get("jq_path", "")

    res = await _sources_add(**add_kw)
    if not res or res.get("error"):
        return {"error": (res or {}).get("error", "fabric.sources.add unavailable")}
    src_id = res.get("id", "")
    _mark_promoted(surface_row.get("id", ""), src_id)

    pulled = None
    if auto_pull and add_kw["enabled"]:
        try:
            from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY
            pull = CAPABILITY_REGISTRY.get("fabric.sources.pull")
            if pull:
                pr = await pull["func"](source_id=src_id, trace_id=None)
                pulled = pr.get("ingested")
        except Exception as e:
            log.debug("auto-pull %s: %s", src_id, e)

    await emit_event({"type": "fabric.discover.promoted", "surface_id": surface_row.get("id", ""),
                      "source_id": src_id, "kind": kind, "dataset_id": ds_id,
                      "pulled": pulled})
    return {"ok": True, "source_id": src_id, "dataset_id": ds_id,
            "source_type": st, "enabled": add_kw["enabled"], "pulled": pulled}


def _mark_promoted(surface_id: str, source_id: str):
    if not surface_id:
        return
    try:
        conn = _sqlite_conn()
        conn.execute("UPDATE fabric_surfaces SET promoted=1, source_id=?, updated_at=? WHERE id=?",
                     (source_id, now_iso(), surface_id))
        conn.commit()
    except Exception as e:
        log.debug("mark promoted: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
# RESUMABLE CRAWL CORE
# ═══════════════════════════════════════════════════════════════════════════

def _save_frontier(crawl_id, ds_id, seed_url, config, queue, visited,
                   status, pages, surfaces, subtables):
    _ensure_tables()
    try:
        conn = _sqlite_conn()
        conn.execute(
            "INSERT OR REPLACE INTO fabric_discovery_frontier "
            "(id, dataset_id, seed_url, status, config, queue, visited, "
            " pages_fetched, surfaces_found, subtables_found, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,"
            "  COALESCE((SELECT created_at FROM fabric_discovery_frontier WHERE id=?), ?), ?)",
            (crawl_id, ds_id, seed_url, status, json.dumps(config),
             json.dumps(queue[:5000]), json.dumps(list(visited)[:10000]),
             pages, surfaces, subtables, crawl_id, now_iso(), now_iso()),
        )
        conn.commit()
    except Exception as e:
        log.debug("save frontier: %s", e)


def _load_frontier(crawl_id) -> Optional[Dict]:
    _ensure_tables()
    try:
        row = _sqlite_conn().execute(
            "SELECT * FROM fabric_discovery_frontier WHERE id=?", (crawl_id,)
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


async def _maybe_openapi_subtable(surface: Dict, parent_ds: str, crawl_id: str,
                                  extra_tags: List[str], budget: List[int]):
    """If we found an OpenAPI/Swagger spec and have budget, fetch it and turn
    its paths into an api_endpoints sub-table."""
    if surface.get("kind") != "openapi" or budget[0] <= 0:
        return
    budget[0] -= 1
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                      headers=_HTTP_HEADERS) as c:
            r = await c.get(surface["url"])
            if r.status_code >= 400:
                return
            spec = r.json()
    except Exception as e:
        log.debug("openapi fetch %s: %s", surface.get("url"), e)
        return
    paths = spec.get("paths") if isinstance(spec, dict) else None
    if not isinstance(paths, dict):
        return
    records = []
    for path, methods in list(paths.items())[:_MAX_SUBTABLE_ROWS]:
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.upper() not in _HTTP_METHODS:
                continue
            summary = ""
            if isinstance(op, dict):
                summary = op.get("summary") or op.get("operationId") or ""
            records.append({"method": method.upper(), "path": path,
                            "summary": _clean(summary)[:300],
                            "text": f"{method.upper()} {path} {summary}".strip()})
    if len(records) >= 2:
        await ingest_subtable(parent_ds, {
            "kind": "api_endpoints",
            "title": (spec.get("info", {}) or {}).get("title", "openapi") if isinstance(spec, dict) else "openapi",
            "columns": ["method", "path", "summary"], "records": records,
        }, surface["url"], crawl_id, extra_tags)


async def _run_crawl(crawl_id: str, ds_id: str, seed_url: str, config: Dict,
                     queue: List, visited: Set[str]) -> Dict:
    wa = _wa()
    ingest_fn = _ingest()
    hostname = urlparse(seed_url).netloc if seed_url else ""

    max_pages   = max(1, min(2000, int(config.get("max_pages", 60))))
    max_depth   = max(1, min(20, int(config.get("max_depth", 4))))
    same_domain = bool(config.get("same_domain", True))
    do_surfaces = bool(config.get("detect_surfaces", True))
    do_subtab   = bool(config.get("extract_subtables", True))
    auto_promote = bool(config.get("auto_promote", False))
    auto_pull    = bool(config.get("auto_pull", False))
    do_entities  = bool(config.get("extract_entities", True))
    topic        = config.get("topic", "") or ""
    topic_dropoff = int(config.get("topic_dropoff", 3))
    extra_tags   = [t.strip() for t in (config.get("tags", "") or "").split(",") if t.strip()]
    tokens       = _topic_tokens(topic)

    neg = wa.NegativeFilter(
        negative_words=[w.strip() for w in (config.get("negative_words", "") or "").split(",") if w.strip()],
        negative_url_patterns=[p.strip() for p in (config.get("negative_urls", "") or "").split(",") if p.strip()],
    )

    pages_fetched = 0
    surfaces_found = 0
    subtables_found = 0
    entities_found = 0
    promoted = 0
    spec_budget = [int(config.get("max_spec_fetches", 5))]

    async def _emit(stage, **kw):
        try:
            await emit_event({"type": "fabric.web.acquire.progress",
                              "acquisition_id": crawl_id, "dataset_id": ds_id,
                              "stage": stage, "engine": "discovery", **kw})
        except Exception:
            pass

    await _emit("starting", url=seed_url, max_pages=max_pages, max_depth=max_depth,
                resumed=bool(visited))

    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                  headers=_HTTP_HEADERS) as client:
        while queue and pages_fetched < max_pages:
            item = queue.pop(0)
            page_url, depth, dry = item[0], item[1], item[2]
            parent_url = item[3] if len(item) > 3 else None
            page_url = page_url.split("#")[0]
            if page_url in visited or depth > max_depth:
                continue
            if not neg.url_allowed(page_url):
                continue
            if same_domain and hostname:
                ph = urlparse(page_url).netloc
                if ph != hostname:
                    h, s = ph.split("."), hostname.split(".")
                    if not (len(h) >= 2 and len(s) >= 2 and h[-2:] == s[-2:]):
                        continue
            visited.add(page_url)

            try:
                await asyncio.sleep(_CRAWL_DELAY)
                resp = await client.get(page_url)
                if resp.status_code >= 400:
                    continue
                ct = resp.headers.get("content-type", "").lower()
            except Exception as e:
                log.debug("fetch %s: %s", page_url, e)
                continue
            raw = resp.text

            # Structured-data response → consume as a data sub-dataset + surface
            data_kind = wa._detect_data_kind(page_url, ct, raw)
            if data_kind:
                try:
                    sub_ds, n = await wa._ingest_data_payload(
                        page_url, raw, data_kind, ds_id, hostname,
                        extra_tags, ingest_fn)
                    pages_fetched += 1
                    if do_surfaces:
                        s = _mk_surface(parent_dataset=ds_id, from_url=page_url,
                                        kind=data_kind, source_type=("rss" if data_kind == "rss" else ""),
                                        url=page_url, label=f"{data_kind} feed/data",
                                        confidence=0.9, why="response was structured data",
                                        config={})
                        s["topic_score"] = _score_topic(page_url, tokens)
                        _store_surface(s); surfaces_found += 1
                    await _emit("data_detected", url=page_url, kind=data_kind,
                                dataset_id=sub_ds, ingested=n, parent_dataset_id=ds_id,
                                parent_url=parent_url)
                    # graph: parent → this data url → its sub-dataset
                    _record_edge(crawl_id, ds_id, (parent_url or ds_id), page_url,
                                 ("LINKS_TO" if parent_url else "HAS_PAGE"),
                                 "Page")
                    if sub_ds:
                        _record_edge(crawl_id, ds_id, page_url, sub_ds,
                                     "HAS_DATA_SUBSET", "Dataset")
                except Exception as e:
                    log.debug("data ingest %s: %s", page_url, e)
                continue
            if "text/html" not in ct and "text/plain" not in ct:
                continue

            structure = wa._extract_page_structure(raw, page_url)
            title = structure.get("title", "")
            full_text = structure.get("full_text", "")
            page_links = structure.get("links", [])

            if neg.content_dominated(full_text):
                continue

            # Topic gate (same dropoff semantics as fabric.web.acquire)
            if tokens:
                hay = (full_text + " " + title + " " + page_url).lower()
                has_topic = any(t in hay for t in tokens)
                next_dry = 0 if has_topic else dry + 1
                if next_dry > topic_dropoff:
                    continue
            else:
                next_dry = 0

            # Ingest the page itself into the main dataset
            chash = hashlib.sha256(full_text.encode()).hexdigest()[:16]
            rid = hashlib.sha256(f"{ds_id}:{page_url}:{chash}".encode()).hexdigest()[:24]
            record = {
                "id": rid, "text": full_text[:8000], "title": title,
                "url": page_url, "hostname": hostname, "depth": depth,
                "headings": structure.get("headings", [])[:20],
                "word_count": structure.get("word_count", 0),
                "source": "fabric_discovery",
                "tags": extra_tags + ["web", "discovery"],
            }
            try:
                await ingest_fn(ds_id, [record], source="fabric_discovery",
                                tags=extra_tags + ["web", "discovery"])
                pages_fetched += 1
            except Exception as e:
                log.warning("ingest page %s: %s", page_url, e)
                continue

            await _emit("page_added", url=page_url, title=(title or "")[:80],
                        record_id=rid, depth=depth, dataset_id=ds_id,
                        parent_url=parent_url)
            _record_edge(crawl_id, ds_id, (parent_url or ds_id), page_url,
                         ("LINKS_TO" if parent_url else "HAS_PAGE"), "Page")

            # ── Entity + relationship extraction (people, orgs, dates, tech,
            #    code symbols) — reuses the original engine, persisted to the
            #    shared entity-graph tables and surfaced in the discovery map ──
            if do_entities:
                try:
                    ck = "code" if (structure.get("code_blocks") and
                                    len(structure.get("code_blocks", [])) >= 2) else "web"
                    n_ent = await extract_entities_for_record(full_text, rid, ds_id, ck)
                    if n_ent:
                        entities_found += n_ent
                        await _emit("entity_found", url=page_url, count=n_ent, dataset_id=ds_id)
                except Exception as e:
                    log.debug("entities %s: %s", page_url, e)
            if do_surfaces:
                try:
                    surfaces = detect_surfaces(raw, page_url, structure, ds_id)
                except Exception as e:
                    log.debug("detect surfaces %s: %s", page_url, e); surfaces = []
                for s in surfaces:
                    s["crawl_id"] = crawl_id
                    s["topic_score"] = _score_topic(
                        f"{s.get('label','')} {s.get('url','')} {s.get('why','')}", tokens)
                    if _store_surface(s):
                        surfaces_found += 1
                        _record_edge(crawl_id, ds_id, page_url, s["id"],
                                     "HAS_SURFACE", "Surface")
                        await _emit("surface_detected", kind=s["kind"],
                                    surface_url=s["url"], label=s["label"],
                                    confidence=s["confidence"],
                                    topic_score=s["topic_score"], from_url=page_url)
                        await emit_event({"type": "fabric.discover.surface",
                                          "parent_dataset": ds_id, "crawl_id": crawl_id,
                                          **{k: s[k] for k in ("id", "kind", "source_type",
                                             "url", "label", "confidence", "topic_score")}})
                        # Opportunistically expand OpenAPI specs into endpoint tables
                        if do_subtab:
                            await _maybe_openapi_subtable(s, ds_id, crawl_id,
                                                          extra_tags, spec_budget)
                        # Auto-promote high-confidence, topic-relevant surfaces
                        if auto_promote and s["source_type"] and s["kind"] != "db" and \
                           s["confidence"] >= 0.6 and (not tokens or s["topic_score"] > 0):
                            try:
                                pr = await promote_surface(s, auto_pull=auto_pull)
                                if pr.get("ok"):
                                    promoted += 1
                            except Exception as e:
                                log.debug("auto-promote: %s", e)

            # ── Sub-table extraction ────────────────────────────────────────
            if do_subtab:
                try:
                    subs = extract_subtables(raw, page_url, structure)
                except Exception as e:
                    log.debug("extract subtables %s: %s", page_url, e); subs = []
                for sub in subs:
                    _sd, n = await ingest_subtable(ds_id, sub, page_url, crawl_id, extra_tags)
                    if n:
                        subtables_found += 1
                        _record_edge(crawl_id, ds_id, page_url, _sd,
                                     "HAS_SUBTABLE", "Dataset")
                        await _emit("subtable_added", url=page_url, dataset_id=_sd,
                                    parent_dataset_id=ds_id, kind=sub.get("kind", "table"),
                                    title=sub.get("title", ""), rows=n)

            # ── Enqueue children — topic-relevant links first ───────────────
            scored_links = []
            for ln in page_links:
                lu = ln.get("url", "").split("#")[0]
                if not lu or lu in visited:
                    continue
                sc = _score_topic(f"{ln.get('anchor','')} {ln.get('context','')} {lu}", tokens)
                scored_links.append((sc, lu))
            scored_links.sort(key=lambda x: x[0], reverse=True)
            added = 0
            for sc, lu in scored_links:
                if added >= 60 or len(queue) > max_pages * 5:
                    break
                queue.append((lu, depth + 1, next_dry, page_url))
                added += 1

            if pages_fetched % 3 == 1:
                _save_frontier(crawl_id, ds_id, seed_url, config, queue, visited,
                               "running", pages_fetched, surfaces_found, subtables_found)
                await _emit("progress", pages=pages_fetched, queued=len(queue),
                            surfaces=surfaces_found, subtables=subtables_found,
                            promoted=promoted, current=page_url[:120])

    status = "done" if not queue else "paused"
    _save_frontier(crawl_id, ds_id, seed_url, config, queue, visited,
                   status, pages_fetched, surfaces_found, subtables_found)
    await _emit("done", pages=pages_fetched, surfaces=surfaces_found,
                subtables=subtables_found, entities=entities_found, promoted=promoted,
                queue_remaining=len(queue))
    return {
        "ok": True, "crawl_id": crawl_id, "dataset_id": ds_id, "status": status,
        "pages_fetched": pages_fetched, "surfaces_found": surfaces_found,
        "subtables_found": subtables_found, "entities_found": entities_found,
        "surfaces_promoted": promoted, "queue_remaining": len(queue),
    }


# ═══════════════════════════════════════════════════════════════════════════
# CAPABILITIES
# ═══════════════════════════════════════════════════════════════════════════

def _new_crawl_id(seed: str, ds: str) -> str:
    return "disc_" + hashlib.sha1(f"{seed}:{ds}:{now_iso()}".encode()).hexdigest()[:16]


def _auto_ds(url: str) -> str:
    host = urlparse(url if url.startswith(("http://", "https://")) else "https://" + url).netloc
    return f"web.{re.sub(r'[^a-z0-9]', '_', host.lower())[:30]}"


@capability(
    "fabric.discover.crawl",
    http_method="POST", http_path="/fabric/discover/crawl",
    http_tags=["fabric", "discover"],
    memory="on",
    description="Resumable discovery crawl. Fetches pages AND detects reachable "
                "interaction surfaces (RSS, git repos, OpenAPI/Swagger, GraphQL, "
                "JSON APIs, data files, DB hints) plus extracts embedded structured "
                "concepts (HTML tables/pokedexes, API-endpoint tables, CLI flag "
                "lists, definition lists) into sub-tables. Persists a frontier so it "
                "can be continued. "
                "Input: url (str!), dataset_id (str — auto from host), topic (str), "
                "topic_dropoff (int=3), max_pages (int=60), max_depth (int=4), "
                "same_domain (bool=True), negative_words (str), negative_urls (str), "
                "detect_surfaces (bool=True), extract_subtables (bool=True), "
                "auto_promote (bool=False — register detected sources), "
                "auto_pull (bool=False — pull promoted sources), tags (str). "
                "Output: {crawl_id, dataset_id, pages_fetched, surfaces_found, "
                "subtables_found, surfaces_promoted, status, queue_remaining}.",
)
async def cap_discover_crawl(
    url: str = "", dataset_id: str = "", topic: str = "", topic_dropoff: int = 3,
    max_pages: int = 60, max_depth: int = 4, same_domain: bool = True,
    negative_words: str = "", negative_urls: str = "",
    detect_surfaces: bool = True, extract_subtables: bool = True,
    extract_entities: bool = True,
    auto_promote: bool = False, auto_pull: bool = False, tags: str = "",
    crawl_id: str = "",
    trace_id=None,
) -> Dict:
    if not url or not url.strip():
        return {"error": "url required"}
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    _ensure_tables()
    ds_id = (re.sub(r"[^a-zA-Z0-9_.]", "_", dataset_id.strip())[:80]
             if dataset_id.strip() else _auto_ds(url))
    crawl_id = crawl_id.strip() or _new_crawl_id(url, ds_id)
    config = {
        "max_pages": max_pages, "max_depth": max_depth, "same_domain": same_domain,
        "topic": topic, "topic_dropoff": topic_dropoff,
        "negative_words": negative_words, "negative_urls": negative_urls,
        "detect_surfaces": detect_surfaces, "extract_subtables": extract_subtables,
        "extract_entities": extract_entities,
        "auto_promote": auto_promote, "auto_pull": auto_pull, "tags": tags,
    }
    return await _run_crawl(crawl_id, ds_id, url, config, [(url, 0, 0, None)], set())


@capability(
    "fabric.discover.continue",
    http_method="POST", http_path="/fabric/discover/continue",
    http_tags=["fabric", "discover"],
    memory="off",
    description="Resume a discovery crawl from its saved frontier (queue + visited). "
                "Input: crawl_id (str!) OR dataset_id (str — most recent crawl), "
                "additional_pages (int=60 — extends the page budget). "
                "Output: same as fabric.discover.crawl.",
)
async def cap_discover_continue(
    crawl_id: str = "", dataset_id: str = "", additional_pages: int = 60,
    trace_id=None,
) -> Dict:
    _ensure_tables()
    fr = None
    if crawl_id:
        fr = _load_frontier(crawl_id)
    elif dataset_id:
        try:
            row = _sqlite_conn().execute(
                "SELECT * FROM fabric_discovery_frontier WHERE dataset_id=? "
                "ORDER BY updated_at DESC LIMIT 1", (dataset_id,)).fetchone()
            fr = dict(row) if row else None
        except Exception:
            fr = None
    if not fr:
        return {"error": "No saved crawl found. Provide a valid crawl_id or dataset_id."}
    try:
        config  = json.loads(fr.get("config") or "{}")
        queue   = [tuple(x) for x in json.loads(fr.get("queue") or "[]")]
        visited = set(json.loads(fr.get("visited") or "[]"))
    except Exception as e:
        return {"error": f"corrupt frontier state: {e}"}
    if not queue:
        return {"ok": True, "status": "done", "note": "frontier already exhausted",
                "crawl_id": fr["id"], "dataset_id": fr["dataset_id"],
                "pages_fetched": fr.get("pages_fetched", 0)}
    # Extend the page budget so the resumed run actually fetches more
    config["max_pages"] = int(config.get("max_pages", 60)) + max(0, additional_pages)
    return await _run_crawl(fr["id"], fr["dataset_id"], fr.get("seed_url", ""),
                            config, queue, visited)


@capability(
    "fabric.discover.from_dataset",
    http_method="POST", http_path="/fabric/discover/from_dataset",
    http_tags=["fabric", "discover"],
    memory="on",
    description="Seed a discovery crawl from an EXISTING dataset's already-scanned "
                "structure and continue crawling. Loads the URLs already ingested "
                "(marking them visited) and the outbound links recorded in the graph "
                "(LINKS_TO) as the new frontier, so the crawl extends what's been "
                "scanned rather than starting over. "
                "Input: dataset_id (str!), topic (str), max_pages (int=60), "
                "max_depth (int=6), same_domain (bool=True), detect_surfaces (bool=True), "
                "extract_subtables (bool=True), auto_promote (bool=False), "
                "auto_pull (bool=False), tags (str). "
                "Output: {crawl_id, dataset_id, seeded_urls, known_pages, ...}.",
)
async def cap_discover_from_dataset(
    dataset_id: str = "", topic: str = "", max_pages: int = 60, max_depth: int = 6,
    same_domain: bool = True, detect_surfaces: bool = True,
    extract_subtables: bool = True, auto_promote: bool = False,
    auto_pull: bool = False, tags: str = "", trace_id=None,
) -> Dict:
    if not dataset_id.strip():
        return {"error": "dataset_id required"}
    ds_id = dataset_id.strip()
    _ensure_tables()

    # 1) Existing page URLs → visited set (+ a seed host)
    visited: Set[str] = set()
    seed_host = ""
    try:
        rows = _sqlite_conn().execute(
            "SELECT data FROM fabric_records WHERE dataset_id=? LIMIT 5000", (ds_id,)
        ).fetchall()
        for r in rows:
            try:
                d = json.loads(r["data"]) if r["data"] else {}
            except Exception:
                d = {}
            u = (d.get("url") or "").split("#")[0]
            if u.startswith(("http://", "https://")):
                visited.add(u)
                if not seed_host:
                    seed_host = urlparse(u).netloc
    except Exception as e:
        log.debug("from_dataset read records: %s", e)
    known_pages = len(visited)

    # 2) Outbound link targets from the graph that we haven't visited yet
    frontier_urls: List[str] = []
    graph = _get_graph()
    if graph and graph.available:
        try:
            res = await graph.query(
                "MATCH (:Dataset {id:$ds})-[:CONTAINS]->(:FabricRecord)-[:LINKS_TO]->(t:FabricRecord) "
                "WHERE t.url IS NOT NULL RETURN DISTINCT t.url AS url LIMIT 4000",
                {"ds": ds_id})
            for row in (res or []):
                u = (row.get("url") or "").split("#")[0]
                if u.startswith(("http://", "https://")) and u not in visited:
                    frontier_urls.append(u)
        except Exception as e:
            log.debug("from_dataset graph query: %s", e)

    # 3) Fallback: if the graph yielded nothing, re-harvest links from a sample
    #    of known pages so we still have somewhere to continue.
    if not frontier_urls and visited:
        sample = list(visited)[:8]
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                      headers=_HTTP_HEADERS) as c:
            for u in sample:
                try:
                    r = await c.get(u)
                    if r.status_code >= 400:
                        continue
                    st = _wa()._extract_page_structure(r.text, u)
                    for ln in st.get("links", []):
                        lu = ln.get("url", "").split("#")[0]
                        if lu.startswith(("http://", "https://")) and lu not in visited:
                            frontier_urls.append(lu)
                except Exception:
                    continue

    frontier_urls = list(dict.fromkeys(frontier_urls))  # dedupe, keep order
    if not frontier_urls:
        return {"error": "Could not derive a frontier from this dataset "
                         "(no stored URLs or outbound links found).",
                "known_pages": known_pages}

    seed_url = ("https://" + seed_host) if seed_host else frontier_urls[0]
    crawl_id = _new_crawl_id(seed_url, ds_id)
    queue = [(u, 1, 0, None) for u in frontier_urls]
    config = {
        "max_pages": max_pages, "max_depth": max_depth, "same_domain": same_domain,
        "topic": topic, "topic_dropoff": 3, "detect_surfaces": detect_surfaces,
        "extract_subtables": extract_subtables, "auto_promote": auto_promote,
        "auto_pull": auto_pull, "tags": tags or "rescan",
    }
    result = await _run_crawl(crawl_id, ds_id, seed_url, config, queue, set(visited))
    result["seeded_urls"] = len(frontier_urls)
    result["known_pages"] = known_pages
    return result


@capability(
    "fabric.discover.detect",
    http_method="POST", http_path="/fabric/discover/detect",
    http_tags=["fabric", "discover"],
    memory="off",
    description="One-shot analysis of a single page (no crawl): fetch it and "
                "return the interaction surfaces and structured sub-tables found, "
                "optionally ingesting the sub-tables. "
                "Input: url (str!), dataset_id (str), ingest_subtables (bool=False), "
                "store_surfaces (bool=True). "
                "Output: {surfaces:[...], subtables:[...]}.",
)
async def cap_discover_detect(
    url: str = "", dataset_id: str = "", ingest_subtables: bool = False,
    store_surfaces: bool = True, trace_id=None,
) -> Dict:
    if not url.strip():
        return {"error": "url required"}
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    _ensure_tables()
    ds_id = dataset_id.strip() or _auto_ds(url)
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                      headers=_HTTP_HEADERS) as c:
            r = await c.get(url)
            r.raise_for_status()
            raw = r.text
    except Exception as e:
        return {"error": f"fetch failed: {e}"}
    structure = _wa()._extract_page_structure(raw, url)
    surfaces = detect_surfaces(raw, url, structure, ds_id)
    if store_surfaces:
        for s in surfaces:
            _store_surface(s)
    subs = extract_subtables(raw, url, structure)
    ingested = []
    if ingest_subtables:
        for sub in subs:
            sd, n = await ingest_subtable(ds_id, sub, url, "", [])
            if n:
                ingested.append({"sub_dataset": sd, "rows": n, "kind": sub["kind"]})
    return {
        "ok": True, "dataset_id": ds_id, "url": url,
        "surfaces": [{k: s[k] for k in ("kind", "source_type", "url", "label",
                      "confidence", "why")} for s in surfaces],
        "subtables": [{"kind": s["kind"], "title": s["title"],
                       "columns": s["columns"], "rows": len(s["records"])}
                      for s in subs],
        "ingested": ingested,
    }


@capability(
    "fabric.surfaces.list",
    http_method="GET", http_path="/fabric/surfaces", http_tags=["fabric", "discover"],
    memory="off", silent=True,
    description="List detected interaction surfaces. Input: parent_dataset (str), "
                "kind (str), promoted (str: 'all'|'yes'|'no' = all), "
                "min_confidence (float=0), limit (int=200). "
                "Output: {surfaces:[...], count}.",
)
async def cap_surfaces_list(
    parent_dataset: str = "", kind: str = "", promoted: str = "all",
    min_confidence: float = 0.0, limit: int = 200, trace_id=None,
) -> Dict:
    _ensure_tables()
    where, params = ["confidence >= ?"], [float(min_confidence)]
    if parent_dataset:
        where.append("parent_dataset = ?"); params.append(parent_dataset)
    if kind:
        where.append("kind = ?"); params.append(kind)
    if promoted == "yes":
        where.append("promoted = 1")
    elif promoted == "no":
        where.append("promoted = 0")
    sql = ("SELECT * FROM fabric_surfaces WHERE " + " AND ".join(where) +
           " ORDER BY topic_score DESC, confidence DESC LIMIT ?")
    params.append(min(int(limit), 1000))
    try:
        rows = _sqlite_conn().execute(sql, params).fetchall()
    except Exception as e:
        return {"error": str(e), "surfaces": []}
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["config"] = json.loads(d.get("config") or "{}")
        except Exception:
            d["config"] = {}
        out.append(d)
    return {"surfaces": out, "count": len(out)}


@capability(
    "fabric.surfaces.promote",
    http_method="POST", http_path="/fabric/surfaces/promote",
    http_tags=["fabric", "discover"], memory="off",
    description="Promote a detected surface into a recurring fabric source (or, for "
                "data files / sitemaps, ingest it once). "
                "Input: surface_id (str!), auto_pull (bool=False — pull immediately). "
                "Output: {ok, source_id, dataset_id, source_type, pulled}.",
)
async def cap_surfaces_promote(
    surface_id: str = "", auto_pull: bool = False, trace_id=None,
) -> Dict:
    if not surface_id:
        return {"error": "surface_id required"}
    _ensure_tables()
    row = _sqlite_conn().execute(
        "SELECT * FROM fabric_surfaces WHERE id=?", (surface_id,)).fetchone()
    if not row:
        return {"error": f"surface not found: {surface_id}"}
    return await promote_surface(dict(row), auto_pull=auto_pull)


@capability(
    "fabric.surfaces.delete",
    http_method="POST", http_path="/fabric/surfaces/delete",
    http_tags=["fabric", "discover"], memory="off",
    description="Forget a detected surface. Input: surface_id (str!).",
)
async def cap_surfaces_delete(surface_id: str = "", trace_id=None) -> Dict:
    if not surface_id:
        return {"error": "surface_id required"}
    _ensure_tables()
    try:
        conn = _sqlite_conn()
        conn.execute("DELETE FROM fabric_surfaces WHERE id=?", (surface_id,))
        conn.commit()
    except Exception as e:
        return {"error": str(e)}
    return {"deleted": True, "surface_id": surface_id}


@capability(
    "fabric.subtables.list",
    http_method="GET", http_path="/fabric/subtables", http_tags=["fabric", "discover"],
    memory="off", silent=True,
    description="List extracted sub-tables (embedded structured concepts pulled into "
                "sub-datasets). Input: parent_dataset (str), kind (str), limit (int=200). "
                "Output: {subtables:[...], count}.",
)
async def cap_subtables_list(
    parent_dataset: str = "", kind: str = "", limit: int = 200, trace_id=None,
) -> Dict:
    _ensure_tables()
    where, params = ["1=1"], []
    if parent_dataset:
        where.append("parent_dataset = ?"); params.append(parent_dataset)
    if kind:
        where.append("kind = ?"); params.append(kind)
    sql = ("SELECT * FROM fabric_subtables WHERE " + " AND ".join(where) +
           " ORDER BY created_at DESC LIMIT ?")
    params.append(min(int(limit), 1000))
    try:
        rows = _sqlite_conn().execute(sql, params).fetchall()
    except Exception as e:
        return {"error": str(e), "subtables": []}
    out = []
    for r in rows:
        d = dict(r)
        for f in ("columns", "schema"):
            try:
                d[f] = json.loads(d.get(f) or ("[]" if f == "columns" else "{}"))
            except Exception:
                d[f] = [] if f == "columns" else {}
        out.append(d)
    return {"subtables": out, "count": len(out)}


log.info("fabric_discovery: discovery & sub-source detection loaded "
         "(caps: discover.crawl/continue/from_dataset/detect, surfaces.*, subtables.list)")


# ═══════════════════════════════════════════════════════════════════════════
# HISTORY + GRAPH RECONSTRUCTION + TOPIC DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════

@capability(
    "fabric.discover.history",
    http_method="GET", http_path="/fabric/discover/history",
    http_tags=["fabric", "discover"], memory="off", silent=True,
    description="List discovery crawls (resumable frontiers) with progress counts. "
                "Input: dataset_id (str — filter), status (str — running|paused|done), "
                "limit (int=50). "
                "Output: {crawls:[{crawl_id, dataset_id, seed_url, status, topic, "
                "pages_fetched, queued, surfaces_found, subtables_found, "
                "created_at, updated_at}], count}.",
)
async def cap_discover_history(
    dataset_id: str = "", status: str = "", limit: int = 50, trace_id=None,
) -> Dict:
    _ensure_tables()
    where, params = ["1=1"], []
    if dataset_id:
        where.append("dataset_id = ?"); params.append(dataset_id)
    if status:
        where.append("status = ?"); params.append(status)
    sql = ("SELECT id, dataset_id, seed_url, status, config, queue, "
           "pages_fetched, surfaces_found, subtables_found, created_at, updated_at "
           "FROM fabric_discovery_frontier WHERE " + " AND ".join(where) +
           " ORDER BY updated_at DESC LIMIT ?")
    params.append(min(int(limit), 500))
    try:
        rows = _sqlite_conn().execute(sql, params).fetchall()
    except Exception as e:
        return {"error": str(e), "crawls": []}
    out = []
    for r in rows:
        d = dict(r)
        try:
            cfg = json.loads(d.pop("config", None) or "{}")
        except Exception:
            cfg = {}
        try:
            queued = len(json.loads(d.pop("queue", None) or "[]"))
        except Exception:
            queued = 0
        out.append({
            "crawl_id": d["id"], "dataset_id": d["dataset_id"],
            "seed_url": d.get("seed_url", ""), "status": d.get("status", ""),
            "topic": cfg.get("topic", ""), "queued": queued,
            "pages_fetched": d.get("pages_fetched", 0),
            "surfaces_found": d.get("surfaces_found", 0),
            "subtables_found": d.get("subtables_found", 0),
            "created_at": d.get("created_at", ""), "updated_at": d.get("updated_at", ""),
        })
    return {"crawls": out, "count": len(out)}


def _page_labels_for(ds_id: str, urls: Set[str]) -> Dict[str, Dict]:
    """Fetch title/word_count for a set of page urls from fabric_records."""
    labels: Dict[str, Dict] = {}
    if not urls:
        return labels
    try:
        rows = _sqlite_conn().execute(
            "SELECT data FROM fabric_records WHERE dataset_id=? LIMIT 8000", (ds_id,)
        ).fetchall()
    except Exception:
        return labels
    for r in rows:
        try:
            d = json.loads(r["data"]) if r["data"] else {}
        except Exception:
            continue
        u = (d.get("url") or "").split("#")[0]
        if u and u in urls and u not in labels:
            labels[u] = {"title": d.get("title") or "", "depth": d.get("depth", 0),
                         "word_count": d.get("word_count", 0), "record_id": d.get("id", "")}
    return labels


@capability(
    "fabric.discover.graph",
    http_method="GET", http_path="/fabric/discover/graph",
    http_tags=["fabric", "discover"], memory="off", silent=True,
    description="Reconstruct the discovery map for a crawl or dataset as a node/edge "
                "graph (pages, detected surfaces, extracted sub-tables, data subsets). "
                "Suitable for direct hand-off to the VeraGraph renderer. "
                "Input: crawl_id (str) OR dataset_id (str), max_nodes (int=400). "
                "Output: {nodes:[{id,label,type,props}], edges:[{from,to,rel}], "
                "dataset_id, crawl_id, stats}.",
)
async def cap_discover_graph(
    crawl_id: str = "", dataset_id: str = "", max_nodes: int = 400,
    include_entities: bool = True, trace_id=None,
) -> Dict:
    _ensure_tables()
    conn = _sqlite_conn()
    # Resolve crawl/dataset
    ds_id = dataset_id.strip()
    cid = crawl_id.strip()
    if cid and not ds_id:
        fr = _load_frontier(cid)
        if fr:
            ds_id = fr.get("dataset_id", "")
    if not ds_id and not cid:
        return {"error": "crawl_id or dataset_id required", "nodes": [], "edges": []}

    edge_where, eparams = [], []
    if cid:
        edge_where.append("crawl_id = ?"); eparams.append(cid)
    if ds_id:
        edge_where.append("parent_dataset = ?"); eparams.append(ds_id)
    ewsql = " OR ".join(edge_where) if edge_where else "1=1"
    try:
        erows = conn.execute(
            "SELECT from_id, to_id, rel, to_label FROM fabric_discovery_edges "
            "WHERE " + ewsql + " LIMIT ?", eparams + [max_nodes * 6]
        ).fetchall()
    except Exception as e:
        erows = []
        log.debug("graph edges: %s", e)

    nodes: Dict[str, Dict] = {}
    edges: List[Dict] = []
    page_urls: Set[str] = set()

    def _short(u: str) -> str:
        try:
            p = urlparse(u)
            tail = (p.path or "/").rstrip("/").split("/")[-1] or p.netloc
            return (tail or u)[:42]
        except Exception:
            return (u or "")[:42]

    # Root dataset node
    if ds_id:
        nodes[ds_id] = {"id": ds_id, "label": ds_id.split(".")[-1] or ds_id,
                        "type": "Dataset", "props": {"id": ds_id, "root": True}}

    # Surfaces & sub-tables we may reference
    surf_index: Dict[str, Dict] = {}
    try:
        srows = conn.execute(
            "SELECT id, kind, source_type, url, label, confidence, topic_score, promoted "
            "FROM fabric_surfaces WHERE parent_dataset=? LIMIT ?", (ds_id, max_nodes)
        ).fetchall()
        for s in srows:
            surf_index[s["id"]] = dict(s)
    except Exception:
        pass
    sub_index: Dict[str, Dict] = {}
    try:
        brows = conn.execute(
            "SELECT sub_dataset, kind, title, row_count FROM fabric_subtables "
            "WHERE parent_dataset=? LIMIT ?", (ds_id, max_nodes)
        ).fetchall()
        for b in brows:
            sub_index[b["sub_dataset"]] = dict(b)
    except Exception:
        pass

    for e in erows:
        f, t, rel, tl = e["from_id"], e["to_id"], e["rel"], (e["to_label"] or "Page")
        if f and f.startswith("http"):
            page_urls.add(f)
        if t and t.startswith("http"):
            page_urls.add(t)
        # Ensure endpoint nodes exist (filled with proper labels below)
        for nid, lbl in ((f, None), (t, None)):
            if nid and nid not in nodes:
                if nid in surf_index:
                    si = surf_index[nid]
                    _can = bool(si.get("source_type")) and not si.get("promoted") and si.get("kind") != "db"
                    nodes[nid] = {"id": nid, "label": (si.get("label") or si.get("kind") or "surface")[:42],
                                  "type": "Surface",
                                  "props": {"id": nid, "kind": si.get("kind"), "url": si.get("url"),
                                            "label": si.get("label"),
                                            "source_type": si.get("source_type"),
                                            "confidence": si.get("confidence"),
                                            "topic_score": si.get("topic_score"),
                                            "promoted": si.get("promoted"),
                                            "can_promote": _can}}
                elif nid in sub_index:
                    bi = sub_index[nid]
                    nodes[nid] = {"id": nid, "label": (bi.get("title") or bi.get("kind") or "table")[:42],
                                  "type": "Subtable",
                                  "props": {"id": nid, "kind": bi.get("kind"),
                                            "rows": bi.get("row_count"), "subtable": True,
                                            "dataset_id": nid}}
                elif nid == ds_id:
                    pass  # already created
                elif nid.startswith("http"):
                    nodes[nid] = {"id": nid, "label": _short(nid), "type": "Page",
                                  "props": {"url": nid}}
                else:
                    nodes[nid] = {"id": nid, "label": nid.split(".")[-1][:42],
                                  "type": "Dataset", "props": {"id": nid}}
        edges.append({"from": f, "to": t, "rel": rel})

    # Enrich page nodes with titles/word counts from fabric_records
    labels = _page_labels_for(ds_id, page_urls)
    for u, meta in labels.items():
        if u in nodes:
            nodes[u]["label"] = (meta.get("title") or _short(u))[:42]
            nodes[u]["props"].update({"title": meta.get("title", ""),
                                      "dataset_id": ds_id,
                                      "record_id": meta.get("record_id", ""),
                                      "depth": meta.get("depth", 0),
                                      "word_count": meta.get("word_count", 0)})

    # Fallback: no edges recorded (older crawl) — attach known pages to the dataset
    if not edges and ds_id:
        try:
            rows = conn.execute(
                "SELECT data FROM fabric_records WHERE dataset_id=? LIMIT ?",
                (ds_id, max_nodes)).fetchall()
            for r in rows:
                try:
                    d = json.loads(r["data"]) if r["data"] else {}
                except Exception:
                    continue
                u = (d.get("url") or "").split("#")[0]
                if not u.startswith("http") or u in nodes:
                    continue
                nodes[u] = {"id": u, "label": (d.get("title") or _short(u))[:42],
                            "type": "Page",
                            "props": {"url": u, "title": d.get("title", ""),
                                      "dataset_id": ds_id, "record_id": d.get("id", "")}}
                edges.append({"from": ds_id, "to": u, "rel": "HAS_PAGE"})
        except Exception as e:
            log.debug("graph fallback: %s", e)

    # ── Entities (people, orgs, dates, technologies, code symbols) ──────────
    if include_entities and ds_id:
        # record_id → page url, so MENTIONS can attach to the page node
        rec2url: Dict[str, str] = {}
        for u, meta in (labels or {}).items():
            rid = meta.get("record_id")
            if rid:
                rec2url[rid] = u
        try:
            erow = conn.execute(
                "SELECT m.record_id, e.id, e.name, e.type, e.mention_count "
                "FROM fabric_entity_mentions m JOIN fabric_entities e ON e.id = m.entity_id "
                "WHERE m.dataset_id = ? LIMIT ?", (ds_id, max_nodes * 4)
            ).fetchall()
        except Exception as ex:
            erow = []
            log.debug("graph entities: %s", ex)
        ent_seen: Set[str] = set()
        for m in erow:
            eid, name, etype = m["id"], m["name"], m["type"]
            if eid not in nodes:
                nodes[eid] = {"id": eid, "label": (name or etype)[:42], "type": "Entity",
                              "props": {"id": eid, "name": name, "type": etype,
                                        "subtype": etype, "mention_count": m["mention_count"]}}
            ent_seen.add(eid)
            purl = rec2url.get(m["record_id"])
            if purl and purl in nodes:
                edges.append({"from": purl, "to": eid, "rel": "MENTIONS"})
            elif ds_id in nodes:
                edges.append({"from": ds_id, "to": eid, "rel": "MENTIONS"})
        # entity ↔ entity relations
        if ent_seen:
            try:
                rrow = conn.execute(
                    "SELECT from_id, to_id, rel FROM fabric_entity_relations "
                    "WHERE dataset_id = ? LIMIT ?", (ds_id, max_nodes * 4)
                ).fetchall()
            except Exception:
                rrow = []
            for r in rrow:
                if r["from_id"] in ent_seen and r["to_id"] in ent_seen:
                    edges.append({"from": r["from_id"], "to": r["to_id"], "rel": r["rel"]})

    # Cap node count (keep dataset + highest-degree)
    node_list = list(nodes.values())
    if len(node_list) > max_nodes:
        deg: Dict[str, int] = {}
        for e in edges:
            deg[e["from"]] = deg.get(e["from"], 0) + 1
            deg[e["to"]] = deg.get(e["to"], 0) + 1
        node_list.sort(key=lambda n: (n["props"].get("root", False), deg.get(n["id"], 0)),
                       reverse=True)
        node_list = node_list[:max_nodes]
        keep = {n["id"] for n in node_list}
        edges = [e for e in edges if e["from"] in keep and e["to"] in keep]

    stats = {
        "pages": sum(1 for n in node_list if n["type"] == "Page"),
        "surfaces": sum(1 for n in node_list if n["type"] == "Surface"),
        "subdatasets": sum(1 for n in node_list if n["type"] == "Subtable"),
        "entities": sum(1 for n in node_list if n["type"] == "Entity"),
        "edges": len(edges),
    }
    return {"nodes": node_list, "edges": edges, "dataset_id": ds_id,
            "crawl_id": cid, "stats": stats}


# ═══════════════════════════════════════════════════════════════════════════
# SEARCH (reusing the research engine) · CONCEPT EXPANSION · LOOM
# ═══════════════════════════════════════════════════════════════════════════

async def _web_search(query: str, limit: int = 8) -> List[Dict]:
    """Search the web, reusing whatever the host system provides. Tries, in
    order: a registered web.search/browser.search capability, then the research
    engine's brave→searxng→ddg functions. Returns [{url,title,content}]."""
    # 1) a registered search capability
    for cap_name in ("web.search", "browser.search", "research.search"):
        cap = CAPABILITY_REGISTRY.get(cap_name)
        if not cap:
            continue
        fn = cap.get("raw") or cap.get("func") or cap.get("fn")
        if not fn:
            continue
        for kwargs in ({"query": query, "max_results": limit, "trace_id": None},
                       {"query": query, "limit": limit, "trace_id": None},
                       {"query": query, "max_results": limit},
                       {"query": query}):
            try:
                r = await fn(**kwargs)
            except TypeError:
                continue
            except Exception as e:
                log.debug("%s: %s", cap_name, e); break
            rows = (r.get("results") or r.get("citations") or r.get("hits") or []) if isinstance(r, dict) else (r or [])
            out = []
            for x in rows:
                if isinstance(x, dict):
                    u = x.get("url") or x.get("link") or ""
                    if u.startswith(("http://", "https://")):
                        out.append({"url": u, "title": x.get("title") or u,
                                    "content": x.get("content") or x.get("snippet") or ""})
            if out:
                return out[:limit]
    # 2) the research engine's raw search functions (brave → searxng → ddg)
    try:
        import Vera.Orchestration.researcher.researcher_api as ra
    except Exception:
        return []
    for name in ("search_brave", "search_searxng", "search_ddg"):
        fn = getattr(ra, name, None)
        if not fn:
            continue
        try:
            rows = await fn(query, limit)
        except Exception as e:
            log.debug("%s: %s", name, e); rows = []
        out = []
        for x in (rows or []):
            u = (x.get("url") if isinstance(x, dict) else "") or ""
            if u.startswith(("http://", "https://")):
                out.append({"url": u, "title": x.get("title") or u,
                            "content": x.get("content") or x.get("snippet") or ""})
        if out:
            return out[:limit]
    return []


_QUERY_TEMPLATES = [
    "{t}", "{t} overview", "{t} documentation", "{t} guide tutorial",
    "{t} reference", "{t} api", "{t} dataset OR list", "what is {t}",
    "{t} examples", "{t} wiki",
]


def _topic_query_angles(topic: str, n: int) -> List[str]:
    """Build several complementary search queries from one topic, so discovery
    sees many facets instead of a single result page."""
    angles, seen = [], set()
    for tmpl in _QUERY_TEMPLATES:
        q = tmpl.format(t=topic).strip()
        k = q.lower()
        if k not in seen:
            seen.add(k); angles.append(q)
        if len(angles) >= max(1, n):
            break
    return angles


async def _search_many(queries: List[str], per: int) -> List[Dict]:
    """Run several searches concurrently, clean + dedupe URLs, keep order."""
    results = await asyncio.gather(*[_web_search(q, per) for q in queries],
                                   return_exceptions=True)
    seen, merged = set(), []
    for r in results:
        if isinstance(r, Exception) or not r:
            continue
        for row in r:
            u = row["url"].split("#")[0]
            if u in seen:
                continue
            seen.add(u); merged.append({"url": u, "label": row.get("title") or u,
                                        "type": "web", "content": row.get("content", "")})
    return merged


_CONCEPT_TYPES = {"person", "organisation", "technology", "named_entity",
                  "domain", "type_name", "class", "module"}


def _top_concepts(ds_id: str, limit: int, exclude: Set[str]) -> List[str]:
    """Top entities discovered in a dataset, as fresh search terms (people,
    orgs, technologies, named entities) — drives concept-triggered searches."""
    try:
        rows = _sqlite_conn().execute(
            "SELECT name, type, mention_count FROM fabric_entities "
            "WHERE datasets LIKE ? ORDER BY mention_count DESC LIMIT 200",
            ('%"' + ds_id + '"%',)).fetchall()
    except Exception as e:
        log.debug("top concepts: %s", e); return []
    out = []
    for r in rows:
        if r["type"] not in _CONCEPT_TYPES:
            continue
        name = (r["name"] or "").strip()
        key = name.lower()
        if len(name) < 3 or key in exclude:
            continue
        exclude.add(key); out.append(name)
        if len(out) >= limit:
            break
    return out


async def _run_loom(ds_id: str, cross: bool, max_datasets: int,
                    min_score: float) -> Dict:
    """Stitch RELATED_TO links within the dataset and, if cross=True, against
    other datasets — tying topic results to records elsewhere in the fabric."""
    cap = CAPABILITY_REGISTRY.get("fabric.loom.run")
    if not cap:
        return {"ok": False, "reason": "loom unavailable"}
    fn = cap.get("raw") or cap.get("func")
    internal = 0
    cross_pairs = 0
    try:
        r = await fn(dataset_a=ds_id, dataset_b=ds_id, mode="hybrid",
                     min_score=min_score, max_matches=200, persist=True, trace_id=None)
        internal = (r or {}).get("total", 0)
    except Exception as e:
        log.debug("loom internal: %s", e)
    if cross:
        try:
            others = [d.get("dataset_id") for d in await _df()._sqlite_datasets()
                      if d.get("dataset_id") and d.get("dataset_id") != ds_id]
        except Exception:
            others = []
        # Prefer other discovery/topic datasets, then the rest, capped.
        others.sort(key=lambda d: (not (d.startswith("topic.") or d.startswith("web.")
                                        or d.startswith("discovered_")), d))
        for other in others[:max(0, max_datasets)]:
            try:
                r = await fn(dataset_a=ds_id, dataset_b=other, mode="hybrid",
                             min_score=min_score, max_matches=80, persist=True, trace_id=None)
                cross_pairs += (r or {}).get("total", 0)
            except Exception as e:
                log.debug("loom cross %s: %s", other, e)
    return {"ok": True, "internal_links": internal, "cross_links": cross_pairs}


async def _find_seed_urls(topic: str, max_sources: int, content_type: str) -> List[Dict]:
    """Use the existing collector.discover capability to turn a topic into seed
    sources. When content_type='all', query several content types and merge for
    broader coverage. Returns a list of {url, label, type}. [] if unavailable."""
    cap = CAPABILITY_REGISTRY.get("collector.discover")
    if not cap:
        return []
    fn = cap.get("raw") or cap.get("func")
    if not fn:
        return []

    async def _one(ct: str, n: int) -> List[Dict]:
        try:
            res = await fn(topic=topic, max_sources=n, content_type=ct, trace_id=None)
        except TypeError:
            res = await fn(topic=topic, max_sources=n, content_type=ct)
        except Exception as e:
            log.debug("collector.discover(%s): %s", ct, e)
            return []
        rows = []
        for s in (res or {}).get("sources", []) or []:
            u = (s.get("url") or "").strip()
            if u.startswith(("http://", "https://")):
                rows.append({"url": u, "label": s.get("label") or u, "type": s.get("type") or ct})
        return rows

    out: List[Dict] = []
    if content_type and content_type != "all":
        out = await _one(content_type, max_sources)
    else:
        # Spread the budget across types so a topic yields feeds + sites + apis,
        # not just the first few search hits of one kind.
        per = max(3, max_sources // 2)
        for ct in ("web", "rss", "api"):
            out.extend(await _one(ct, per))
            if len({r["url"] for r in out}) >= max_sources * 2:
                break

    # Dedupe by url, keep first label/type, cap generously (the crawl budget
    # is what really bounds the work; we want broad seeding here).
    seen, merged = set(), []
    for r in out:
        if r["url"] in seen:
            continue
        seen.add(r["url"]); merged.append(r)
    return merged[:max(max_sources, 12)]


@capability(
    "fabric.discover.topic",
    http_method="POST", http_path="/fabric/discover/topic",
    http_tags=["fabric", "discover"], memory="on",
    description="Deep topic-driven discovery. Runs MULTIPLE web searches across "
                "several query angles (reusing the host web.search / research "
                "engine) plus feed discovery, seeds a resumable crawl on all of "
                "them, then runs CONCEPT-EXPANSION rounds: the strongest entities "
                "found (people, orgs, technologies, named entities) trigger fresh "
                "searches whose results are crawled too. Every page is "
                "entity-extracted (people/orgs/dates/tech/code symbols) with "
                "relationships, surfaces are detected, embedded tables pulled in as "
                "sub-tables, and finally the Loom stitches the results to each "
                "other and to records in OTHER datasets (cross-dataset RELATED_TO). "
                "Resumable via fabric.discover.continue. "
                "Input: topic (str!), seed_urls (str — comma-sep, optional), "
                "max_sources (int=10), content_type (rss|api|web|scrape|all), "
                "search_angles (int=6 — how many query variants), "
                "expansion_rounds (int=2 — concept-triggered search rounds), "
                "searches_per_round (int=4), results_per_search (int=8), "
                "dataset_id (str), max_pages (int=120), max_depth (int=4), "
                "topic_dropoff (int=3), same_domain (bool=False), "
                "detect_surfaces/extract_subtables/extract_entities (bool=True), "
                "negative_words/negative_urls (str), auto_promote (bool=True), "
                "loom (bool=True), loom_cross (bool=True), loom_max_datasets "
                "(int=8), loom_min_score (float=0.45), tags (str), crawl_id (str). "
                "Output: {crawl_id, dataset_id, topic, seed_urls, queries, "
                "pages_fetched, surfaces_found, subtables_found, entities_found, "
                "expansion_rounds_run, loom, status, queue_remaining}.",
)
async def cap_discover_topic(
    topic: str = "", seed_urls: str = "", max_sources: int = 10,
    content_type: str = "all", search_angles: int = 6,
    expansion_rounds: int = 2, searches_per_round: int = 4,
    results_per_search: int = 8, dataset_id: str = "",
    max_pages: int = 120, max_depth: int = 4, topic_dropoff: int = 3,
    same_domain: bool = False, detect_surfaces: bool = True,
    extract_subtables: bool = True, extract_entities: bool = True,
    negative_words: str = "", negative_urls: str = "",
    auto_promote: bool = True, auto_pull: bool = False,
    loom: bool = True, loom_cross: bool = True, loom_max_datasets: int = 8,
    loom_min_score: float = 0.45,
    tags: str = "", crawl_id: str = "",
    trace_id=None,
) -> Dict:
    if not topic or not topic.strip():
        return {"error": "topic required"}
    topic = topic.strip()
    _ensure_tables()

    async def _emit(stage, **kw):
        try:
            await emit_event({"type": "fabric.web.acquire.progress",
                              "acquisition_id": crawl_id, "stage": stage,
                              "engine": "discovery", **kw})
        except Exception:
            pass

    ds_id = (re.sub(r"[^a-zA-Z0-9_.]", "_", dataset_id.strip())[:80]
             if dataset_id.strip()
             else "topic." + re.sub(r"[^a-z0-9]+", "_", topic.lower()).strip("_")[:40])

    # ── 1) Seed: explicit URLs win, else multi-angle web search + feed probe ─
    explicit = [u.strip().split("#")[0] for u in seed_urls.split(",")
                if u.strip().startswith(("http://", "https://"))]
    queries: List[str] = []
    discovered: List[Dict] = []
    if explicit:
        seeds = explicit
    else:
        queries = _topic_query_angles(topic, search_angles)
        await _emit("seeding", message=f"searching {len(queries)} angles for '{topic}'",
                    queries=queries)
        discovered = await _search_many(queries, results_per_search)
        # plus feed/source discovery via the existing collector
        try:
            discovered += await _find_seed_urls(topic, max_sources, content_type)
        except Exception as e:
            log.debug("feed seed: %s", e)
        seen, seeds = set(), []
        for d in discovered:
            u = d["url"].split("#")[0]
            if u not in seen:
                seen.add(u); seeds.append(u)
    if not seeds:
        return {"error": "No seed URLs found. Provide seed_urls, or enable a "
                         "web.search / browser.search capability or the research "
                         "engine.", "topic": topic, "queries": queries}
    seeds = list(dict.fromkeys(seeds))

    cid = crawl_id.strip() or _new_crawl_id(seeds[0], ds_id)
    config = {
        "max_pages": max_pages, "max_depth": max_depth, "same_domain": same_domain,
        "topic": topic, "topic_dropoff": topic_dropoff,
        "negative_words": negative_words, "negative_urls": negative_urls,
        "detect_surfaces": detect_surfaces, "extract_subtables": extract_subtables,
        "extract_entities": extract_entities,
        "auto_promote": auto_promote, "auto_pull": auto_pull,
        "tags": (tags + ",topic" if tags else "topic"),
    }

    # ── 2) Initial crawl segment ────────────────────────────────────────────
    await _emit("seeded", message=f"{len(seeds)} seeds from {len(queries)} queries",
                seeds=len(seeds))
    queue = [(u, 0, 0, None) for u in seeds]
    result = await _run_crawl(cid, ds_id, seeds[0], config, queue, set())

    # ── 3) Concept-expansion rounds: top entities → new searches → crawl ────
    searched_terms: Set[str] = set(q.lower() for q in queries)
    rounds_run = 0
    for rnd in range(max(0, expansion_rounds)):
        concepts = _top_concepts(ds_id, searches_per_round, searched_terms)
        if not concepts:
            break
        cqueries = [f"{topic} {c}" if c.lower() not in topic.lower() else c for c in concepts]
        await _emit("expanding", round=rnd + 1, concepts=concepts, queries=cqueries)
        found = await _search_many(cqueries, results_per_search)
        if not found:
            continue
        # Load the (paused/done) frontier and append the new seeds, then resume
        fr = _load_frontier(cid)
        if not fr:
            break
        try:
            q2 = [tuple(x) for x in json.loads(fr.get("queue") or "[]")]
            vis = set(json.loads(fr.get("visited") or "[]"))
        except Exception:
            q2, vis = [], set()
        new_seeds = [d["url"] for d in found if d["url"] not in vis]
        for u in new_seeds:
            q2.append((u, 0, 0, None))
        if not new_seeds:
            continue
        cfg2 = dict(config)
        cfg2["max_pages"] = max_pages + (rnd + 1) * max(20, results_per_search * searches_per_round)
        seg = await _run_crawl(cid, ds_id, seeds[0], cfg2, q2, vis)
        result = seg
        rounds_run += 1

    # ── 4) Loom — stitch within the dataset and across other datasets ───────
    loom_res = {}
    if loom:
        await _emit("loom", message="stitching relations (internal + cross-dataset)")
        loom_res = await _run_loom(ds_id, loom_cross, loom_max_datasets, loom_min_score)

    result["topic"] = topic
    result["seed_urls"] = seeds
    result["queries"] = queries
    result["expansion_rounds_run"] = rounds_run
    if discovered:
        result["seed_sources"] = discovered[:40]
    if loom_res:
        result["loom"] = loom_res
    return result


log.info("fabric_discovery: history/graph/topic caps loaded "
         "(discover.history, discover.graph, discover.topic)")


# ═══════════════════════════════════════════════════════════════════════════
# INTERACTIVE GRAPH EXPANSION
# ═══════════════════════════════════════════════════════════════════════════

def _dataset_for_url(url: str) -> str:
    """Find which discovery dataset a page URL belongs to (via fabric_records)."""
    try:
        rows = _sqlite_conn().execute(
            'SELECT dataset_id, data FROM fabric_records WHERE data LIKE ? LIMIT 8',
            ('%"url": "' + url + '"%',)).fetchall()
        for r in rows:
            try:
                d = json.loads(r["data"]) if r["data"] else {}
            except Exception:
                continue
            if (d.get("url") or "").split("#")[0] == url:
                return r["dataset_id"]
    except Exception as e:
        log.debug("dataset_for_url: %s", e)
    return ""


@capability(
    "fabric.discover.expand",
    http_method="POST", http_path="/fabric/discover/expand",
    http_tags=["fabric", "discover", "graph"], memory="off",
    description="Interactively grow the discovery graph from a node. "
                "Surface → register + pull it and fold the ingested data in; "
                "Page → crawl one level of its links; Subtable/Dataset → extract "
                "entities. Returns a {nodes, edges} delta to merge into the map. "
                "Input: node_label (Surface|Page|Subtable|Dataset), node_id (str!), "
                "dataset_id (str — for Page, optional), max_links (int=25). "
                "Output: {ok, nodes, edges, added, note}.",
)
async def cap_discover_expand(
    node_label: str = "", node_id: str = "", dataset_id: str = "",
    max_links: int = 25, trace_id=None,
) -> Dict:
    _ensure_tables()
    nl = (node_label or "").lower()
    if not node_id:
        return {"error": "node_id required"}
    max_links = max(1, min(120, int(max_links)))

    # ── Surface: promote + pull, then surface the ingested data as nodes ────
    if nl == "surface":
        row = _sqlite_conn().execute(
            "SELECT * FROM fabric_surfaces WHERE id=?", (node_id,)).fetchone()
        if not row:
            return {"error": f"surface not found: {node_id}"}
        row = dict(row)
        parent = row.get("parent_dataset", "") or "discovered"
        res = await promote_surface(row, auto_pull=True)
        if res.get("error"):
            return {"error": res["error"]}
        sub = res.get("dataset_id", "")
        nodes, edges = [], []
        if sub:
            _record_edge(row.get("crawl_id", ""), parent, node_id, sub,
                         "HAS_DATA_SUBSET", "Dataset")
            nodes.append({"id": sub, "label": sub.split(".")[-1][:42], "type": "Subtable",
                          "props": {"id": sub, "dataset_id": sub, "promoted": True,
                                    "source_id": res.get("source_id", "")}})
            edges.append({"from": node_id, "to": sub, "rel": "HAS_DATA_SUBSET"})
            # sample a few freshly ingested records as page nodes
            try:
                recs = _sqlite_conn().execute(
                    "SELECT data FROM fabric_records WHERE dataset_id=? "
                    "ORDER BY created_at DESC LIMIT ?", (sub, max_links)).fetchall()
                for r in recs:
                    try:
                        d = json.loads(r["data"]) if r["data"] else {}
                    except Exception:
                        continue
                    rid = d.get("id") or d.get("url") or ""
                    if not rid:
                        continue
                    nodes.append({"id": rid, "label": (d.get("title") or d.get("url") or rid)[:42],
                                  "type": "Page",
                                  "props": {"url": d.get("url", ""), "title": d.get("title", ""),
                                            "dataset_id": sub, "record_id": d.get("id", "")}})
                    edges.append({"from": sub, "to": rid, "rel": "HAS_PAGE"})
            except Exception as e:
                log.debug("expand surface recs: %s", e)
        return {"ok": True, "nodes": nodes, "edges": edges, "added": len(nodes),
                "note": f"Promoted {row.get('kind','')} → {res.get('source_id','source')}; "
                        f"ingested {res.get('pulled') or res.get('ingested') or 0} records."}

    # ── Page: crawl one level of its outbound links into the same dataset ───
    if nl == "page":
        url = node_id.split("#")[0]
        ds = (dataset_id or "").strip() or _dataset_for_url(url) or _auto_ds(url)
        sub_cid = _new_crawl_id(url, ds)
        config = {"max_pages": max_links + 1, "max_depth": 2, "same_domain": False,
                  "topic": "", "topic_dropoff": 99,
                  "detect_surfaces": True, "extract_subtables": True,
                  "extract_entities": True, "auto_promote": False, "tags": "expand"}
        try:
            await _run_crawl(sub_cid, ds, url, config, [(url, 0, 0, None)], set())
        except Exception as e:
            return {"error": f"expand crawl failed: {e}"}
        g = await cap_discover_graph(crawl_id=sub_cid, dataset_id=ds, include_entities=True)
        return {"ok": True, "nodes": g.get("nodes", []), "edges": g.get("edges", []),
                "added": len(g.get("nodes", [])),
                "note": f"Expanded {url} into {ds} ({g.get('stats', {}).get('pages', 0)} pages)."}

    # ── Subtable / Dataset: extract entities and return the entity delta ────
    if nl in ("subtable", "dataset"):
        ds = node_id
        cap = CAPABILITY_REGISTRY.get("fabric.entity_graph.extract")
        if cap:
            fn = cap.get("raw") or cap.get("func")
            try:
                await fn(dataset_id=ds, content_type="web", persist=True, trace_id=None)
            except Exception as e:
                log.debug("expand entity extract: %s", e)
        g = await cap_discover_graph(dataset_id=ds, include_entities=True)
        nodes = [n for n in g.get("nodes", []) if n["type"] in ("Entity", "Dataset", "Subtable")]
        keep = {n["id"] for n in nodes}
        edges = [e for e in g.get("edges", []) if e["from"] in keep and e["to"] in keep]
        return {"ok": True, "nodes": nodes, "edges": edges, "added": len(nodes),
                "note": f"Extracted {g.get('stats', {}).get('entities', 0)} entities from {ds}."}

    return {"error": f"don't know how to expand a '{node_label}' node"}


# ── Register graph node actions so the surface/page/sub-table nodes are
#    interactive in the VeraGraph drawer (Add as source / Pull & expand / …) ──
def _register_node_actions():
    try:
        from Vera.Orchestration.fabric.data_fabric import _NODE_ACTION_REGISTRY
    except Exception as e:
        log.debug("node-action registry unavailable: %s", e)
        return
    _NODE_ACTION_REGISTRY["Surface"] = [
        {"id": "add_source", "label": "Add as data source", "icon": "\u2295",
         "capability": "fabric.surfaces.promote", "args": {"surface_id": "$id"},
         "options": [{"name": "auto_pull", "type": "bool", "default": True,
                      "label": "Pull immediately"}]},
        {"id": "pull_expand", "label": "Pull & expand into graph", "icon": "\u2935",
         "capability": "fabric.discover.expand",
         "args": {"node_id": "$id", "node_label": "Surface"}},
        {"id": "forget", "label": "Forget surface", "icon": "\u2715",
         "capability": "fabric.surfaces.delete", "args": {"surface_id": "$id"},
         "confirm": "Forget this surface?"},
    ]
    _NODE_ACTION_REGISTRY["Page"] = [
        {"id": "expand_links", "label": "Crawl this page's links", "icon": "\u2935",
         "capability": "fabric.discover.expand",
         "args": {"node_id": "$id", "node_label": "Page"},
         "options": [{"name": "max_links", "type": "int", "default": 25,
                      "label": "Max links"}]},
    ]
    _NODE_ACTION_REGISTRY["Subtable"] = [
        {"id": "extract_entities", "label": "Extract entities", "icon": "\u2b21",
         "capability": "fabric.discover.expand",
         "args": {"node_id": "$id", "node_label": "Subtable"}},
        {"id": "browse", "label": "Browse sub-table", "icon": "\u25e6",
         "capability": "__local"},
    ]
    log.info("fabric_discovery: registered graph node actions (Surface/Page/Subtable)")

_register_node_actions()


# ═══════════════════════════════════════════════════════════════════════════
# UI PANEL — "Discover+" tab
# ═══════════════════════════════════════════════════════════════════════════
# The markup lives in fabric_discovery_panel.html and the logic in
# fabric_discovery_panel.js, sibling to this module (same pattern as the other
# *_panel.html / *_element.js pairs). We read them at import and inject via
# register_ui(mode="tab"). If the files are missing, we register a small notice
# so the tab still loads cleanly.

def _read_sibling(name: str) -> str:
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception as e:
        log.warning("fabric_discovery: could not read %s: %s", name, e)
        return ""


_PANEL_HTML = _read_sibling("fabric_discovery_panel.html")
_PANEL_JS   = _read_sibling("fabric_discovery_panel.js")

if not _PANEL_HTML:
    _PANEL_HTML = ('<div id="fdsc-root" style="padding:20px;color:var(--dim,#8a8278);'
                   'font-size:12px">Discover+ panel markup not found '
                   '(fabric_discovery_panel.html). The backend capabilities '
                   '(fabric.discover.*) are still available via the API.</div>')

try:
    register_ui(
        "fabric-discover",
        "Discover+",
        "\u2295",  # ⊕ monochrome glyph
        _PANEL_HTML,
        _PANEL_JS,
        ui_caps=[
            "fabric.discover.topic", "fabric.discover.crawl",
            "fabric.discover.continue", "fabric.discover.from_dataset",
            "fabric.discover.history", "fabric.discover.graph",
            "fabric.discover.expand",
            "fabric.surfaces.list", "fabric.surfaces.promote",
            "fabric.subtables.list",
        ],
        mode="tab",
        tab_order=30,
    )
    log.info("fabric_discovery: Discover+ UI panel registered (mode=tab)")
except Exception as _ui_e:
    log.warning("fabric_discovery: register_ui failed: %s", _ui_e)