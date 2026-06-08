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
    APP, capability, emit_event, now_iso, register_ui, CAPABILITY_REGISTRY,
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


# ── Entity extraction ───────────────────────────────────────────────────────
def _wa_entity_fns():
    """Return (extract_entities, extract_relations, persist) from the original
    module, or (None, None, None) if unavailable. We reuse only `persist` for
    storage; extraction itself is our improved version below."""
    try:
        wa = _wa()
        return (getattr(wa, "_extract_entities_from_text", None),
                getattr(wa, "_extract_relationships_from_entities", None),
                wa._persist_entity_graph)
    except Exception:
        return (None, None, None)


# Stopwords / junk that the old regex extractor wrongly captured ("the","until"…)
_ENT_STOP = {
    "the", "a", "an", "and", "or", "but", "if", "then", "else", "for", "of", "to",
    "in", "on", "at", "by", "with", "from", "as", "is", "are", "was", "were", "be",
    "been", "being", "this", "that", "these", "those", "it", "its", "he", "she",
    "they", "them", "we", "you", "i", "me", "my", "your", "our", "their", "his",
    "her", "until", "untill", "while", "when", "where", "which", "who", "whom",
    "what", "how", "why", "not", "no", "yes", "can", "will", "would", "should",
    "could", "may", "might", "must", "do", "does", "did", "have", "has", "had",
    "about", "above", "after", "again", "all", "also", "any", "because", "before",
    "between", "both", "down", "during", "each", "few", "more", "most", "other",
    "over", "same", "some", "such", "than", "too", "very", "just", "into", "out",
    "up", "off", "only", "own", "so", "here", "there", "now", "new", "one", "two",
    "get", "got", "see", "use", "used", "using", "via", "per", "etc", "page",
    "home", "menu", "search", "login", "sign", "click", "read", "view", "next",
    "back", "top", "learn", "share", "follow", "subscribe", "contact", "privacy",
    "terms", "cookie", "cookies", "copyright", "rights", "reserved",
}

_RX_EMAIL  = re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b")
_RX_HANDLE = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{2,30})\b")
_RX_URL    = re.compile(r"https?://[^\s)>\"']{6,200}")
_RX_PERSON = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b")
_RX_ORG    = re.compile(r"\b([A-Z][A-Za-z0-9&.\-]+(?:\s+[A-Z][A-Za-z0-9&.\-]+)*\s+"
                        r"(?:Inc|Corp|Corporation|Company|Co|Ltd|LLC|GmbH|Foundation|"
                        r"Institute|University|Labs?|Group|Team|Project|Org|Organization))\b")
_RX_ORG_ACR = re.compile(r"\b([A-Z]{2,6})\b")
_RX_FUNC   = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]{2,40})\s*\(")
_RX_CLASS  = re.compile(r"\bclass\s+([A-Z][A-Za-z0-9_]{2,40})\b")
_RX_DOTTED = re.compile(r"\b([a-z_][a-zA-Z0-9_]+(?:\.[a-zA-Z_][a-zA-Z0-9_]+){1,4})\b")
_RX_VERSION= re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b")
_RX_YEAR   = re.compile(r"\b(19|20)\d{2}\b")
_RX_MONEY  = re.compile(r"[$€£]\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:million|billion|k|m|bn))?", re.I)

# Common technology / product names worth catching even lower-case
_TECH_HINTS = {"python", "javascript", "typescript", "rust", "golang", "java",
               "kubernetes", "docker", "redis", "postgres", "postgresql", "mysql",
               "mongodb", "neo4j", "react", "vue", "angular", "linux", "ollama",
               "tensorflow", "pytorch", "fastapi", "flask", "django", "sqlite",
               "chromadb", "faiss", "ceph", "garage", "nginx", "graphql"}


def _valid_proper(name: str) -> bool:
    parts = name.split()
    if not parts:
        return False
    if all(p.lower() in _ENT_STOP for p in parts):
        return False
    if parts[0].lower() in _ENT_STOP and len(parts) == 1:
        return False
    return True


def extract_entities2(text: str, content_type: str = "web") -> List[Dict]:
    """Unified entity extraction entry point.

    Delegates to the single canonical engine in fabric_web_acquisition
    (_extract_entities_from_text) so Discover+ shares one extractor with web
    acquisition and the entity-graph capability. Output shape is unchanged for
    downstream callers: a list of {name, type, normalised, mention_count,
    position} sorted by mention_count, capped to 80. position is preserved so
    _extract_relationships_from_entities can sentence-scope typed relations
    correctly — stripping it degrades all relations to RELATED_TO/CO_OCCURS.
    Falls back to the local legacy extractor only if unavailable.
    """
    if not text:
        return []
    try:
        fn = getattr(_wa(), "_extract_entities_from_text", None)
        if fn is not None:
            ents = fn(text, content_type) or []
            if ents:
                out = []
                for e in ents:
                    name = e.get("name")
                    if not name:
                        continue
                    ent = {
                        "name": name,
                        "type": e.get("type", "named_entity"),
                        "normalised": e.get("normalised") or name.lower(),
                        "mention_count": e.get("mention_count", 1),
                    }
                    # Preserve position — required by _extract_relationships_from_entities
                    # for sentence-scoped typed relation inference. Without it every
                    # pair falls back to the RELATED_TO weak-edge path.
                    if "position" in e:
                        ent["position"] = e["position"]
                    out.append(ent)
                if out:
                    out.sort(key=lambda e: e["mention_count"], reverse=True)
                    return out[:80]
    except Exception as e:
        log.debug("canonical extractor unavailable, using legacy: %s", e)
    return _extract_entities_legacy(text, content_type)


def _extract_entities_legacy(text: str, content_type: str = "web") -> List[Dict]:
    """Legacy local extractor (fallback). Types: person, account, email,
    organisation, technology, product, function, class, module, url, year,
    money. Aggressively filters stopwords/junk."""
    if not text:
        return []
    body = text[:12000]
    found: Dict[str, Dict] = {}

    def add(name, etype, weight=1):
        name = (name or "").strip().strip(".,:;\"'()[]")
        if not name or len(name) < 2 or len(name) > 60:
            return
        low = name.lower()
        if low in _ENT_STOP:
            return
        norm = f"{etype}:{low}"
        if norm in found:
            found[norm]["mention_count"] += weight
        else:
            found[norm] = {"name": name, "type": etype, "normalised": low,
                           "mention_count": weight}

    for m in _RX_EMAIL.findall(body):
        add(m, "email", 2)
    for m in _RX_HANDLE.findall(body):
        add("@" + m, "account", 2)
    for m in set(_RX_URL.findall(body)):
        try:
            from urllib.parse import urlparse as _up
            add(_up(m).netloc, "domain", 1)
        except Exception:
            pass
    for m in _RX_ORG.findall(body):
        if _valid_proper(m):
            add(m, "organisation", 2)
    for m in _RX_CLASS.findall(body):
        add(m, "class", 2)
    for m in _RX_FUNC.findall(body):
        if m.lower() not in _ENT_STOP and not m.isupper():
            add(m + "()", "function", 1)
    for m in _RX_DOTTED.findall(body):
        if m.count(".") >= 1 and not _RX_VERSION.match(m):
            add(m, "module", 1)
    # people: multi-word Capitalised, validated; strip leading stop tokens and
    # don't re-tag something already captured as an organisation
    org_norms = {e["normalised"] for e in found.values() if e["type"] == "organisation"}
    for m in _RX_PERSON.findall(body):
        toks = m.split()
        while toks and toks[0].lower() in _ENT_STOP:
            toks = toks[1:]
        if len(toks) < 2:
            continue
        if not all(t[:1].isupper() for t in toks):
            continue
        name = " ".join(toks)
        if name.lower() in org_norms:
            continue
        if _valid_proper(name):
            add(name, "person", 2)
    # acronym orgs (validated against stop + length)
    for m in _RX_ORG_ACR.findall(body):
        if m.lower() not in _ENT_STOP and len(m) >= 2:
            add(m, "organisation", 1)
    low_body = body.lower()
    for t in _TECH_HINTS:
        if re.search(r"\b" + re.escape(t) + r"\b", low_body):
            add(t, "technology", 2)
    for m in set(_RX_YEAR.findall(body)):
        add(m if isinstance(m, str) else "".join(m), "year", 1)
    for m in set(_RX_MONEY.findall(body)):
        add(m, "money", 1)

    # keep only entities with enough signal; cap to avoid runaway
    items = [e for e in found.values()
             if e["mention_count"] >= 1 and len(e["normalised"]) >= 2]
    # de-prioritise bare acronyms unless mentioned 2+ times
    items = [e for e in items
             if not (e["type"] == "organisation" and e["name"].isupper()
                     and e["mention_count"] < 2)]
    items.sort(key=lambda e: e["mention_count"], reverse=True)
    return items[:80]


def _cooccurrence_pairs(ents: List[Dict], max_pairs: int = 40) -> List[Dict]:
    """Same-page co-occurrence relations between the strongest entities, in the
    shape _persist_entity_graph expects (from_name/from_type/to_name/to_type)."""
    top = [e for e in ents if e["type"] in
           ("person", "organisation", "account", "email", "technology",
            "product", "class", "module", "domain", "location", "event")][:12]
    rels = []
    for i in range(len(top)):
        for j in range(i + 1, len(top)):
            rels.append({"from_name": top[i]["name"], "from_type": top[i]["type"],
                         "to_name": top[j]["name"], "to_type": top[j]["type"],
                         "rel": "MENTIONED_WITH", "distance": 0})
            if len(rels) >= max_pairs:
                return rels
    return rels


async def extract_entities_for_record(text: str, record_id: str, dataset_id: str,
                                      content_type: str = "web",
                                      llm_assist: bool = False) -> int:
    """Extract entities from one page and persist them, with their relations,
    into the shared entity-graph tables.

    Two tiers of relation quality:
      * llm_assist=True  — an LLM reads the text and returns the topic's key
        entities (correctly typed) and the structural relations between them.
        The LLM classification CORRECTS the spaCy/GLiNER types and its typed
        relations REPLACE bare co-occurrence.
      * llm_assist=False — spaCy/GLiNER entities are passed directly into
        _extract_relationships_from_entities which uses sentence-scoped cue
        matching (FOUNDED/LEADS/USES/LOCATED_IN/…). Falls back to
        MENTIONED_WITH co-occurrence only if the cue engine yields nothing,
        and attempts _upgrade_weak_edges on those weak pairs.
    """
    _e, _r, persist = _wa_entity_fns()
    if not persist or not text:
        return 0
    try:
        # Always run the canonical extractor (spaCy/GLiNER) first — it returns
        # entities WITH position data needed by the relation engine.
        canonical_ents = []
        if _e:
            try:
                canonical_ents = _e(text, content_type) or []
            except Exception as ex:
                log.debug("canonical ents %s: %s", record_id, ex)

        # extract_entities2 is used as a fallback / merge source
        ents = extract_entities2(text, content_type)
        rels = None

        if llm_assist:
            try:
                struct = await _llm_structure(text, content_type)
                lents = struct.get("entities") or []
                if lents:
                    # 1) correct spaCy/GLiNER entity types from the LLM's reading
                    ltype = {e["normalised"]: e["type"] for e in lents}
                    lsal = {e["normalised"]: e.get("salience") for e in lents}
                    for e in ents:
                        if e["normalised"] in ltype:
                            e["type"] = ltype[e["normalised"]]
                            sal = lsal.get(e["normalised"])
                            if sal is not None:
                                e.setdefault("props", {})["relevance"] = sal
                    # 2) add high-value entities spaCy/GLiNER missed
                    seen = {e["normalised"] for e in ents}
                    for e in lents:
                        if e["normalised"] in seen:
                            continue
                        ne = {"name": e["name"], "type": e["type"],
                              "normalised": e["normalised"],
                              "mention_count": e.get("mention_count", 2)}
                        if e.get("salience") is not None:
                            ne["props"] = {"relevance": e["salience"]}
                        ents.append(ne); seen.add(e["normalised"])
                    # 3) prefer the LLM's structural relations
                    if struct.get("relations"):
                        rels = struct["relations"]
            except Exception as ex:
                log.debug("llm structure %s: %s", record_id, ex)

        if not ents:
            return 0

        bag: Dict[str, Dict] = {}
        for e in ents:
            bag[e["normalised"]] = {**e, "mention_count": e.get("mention_count", 1),
                                    "record_ids": {record_id}, "datasets": {dataset_id}}

        if rels is None:
            # Non-LLM path: pass the canonical entities (with position data) into
            # _extract_relationships_from_entities so it can do sentence-scoped
            # typed inference. Using canonical_ents here (not ents) preserves the
            # position field that the cue engine requires.
            rels = []
            if _r and canonical_ents:
                try:
                    wr = _r(canonical_ents, text) or []
                    # Filter to entities we're actually persisting
                    rels = [r for r in wr
                            if str(r.get("from_name", "")).lower() in bag
                            and str(r.get("to_name", "")).lower() in bag]
                except Exception as ex:
                    log.debug("wa relations %s: %s", record_id, ex)
            if not rels:
                rels = _cooccurrence_pairs(ents)
                # Upgrade weak MENTIONED_WITH edges with a quick LLM pass using
                # the surrounding text as context.
                try:
                    wa = _wa()
                    _upg = getattr(wa, "_upgrade_weak_edges", None)
                    if _upg and rels:
                        ctx_snippet = text[:800]
                        for r in rels:
                            if not r.get("context"):
                                r["context"] = ctx_snippet
                        await _upg(rels)
                except Exception as ex:
                    log.debug("upgrade weak edges %s: %s", record_id, ex)

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


_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                    "utm_content", "fbclid", "gclid", "mc_cid", "mc_eid",
                    "ref", "ref_src", "ref_url", "spm", "igshid", "_ga", "yclid"}

def _normalize_url(url: str) -> str:
    """Canonical form so the same page isn't ingested/graphed twice:
    lowercase scheme+host, drop default ports, strip 'www.', drop fragment,
    remove tracking query params, and trim a trailing slash."""
    if not url:
        return url
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        s = urlsplit(url.strip())
        scheme = (s.scheme or "https").lower()
        host = (s.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        netloc = host
        if s.port and not ((scheme == "http" and s.port == 80) or
                           (scheme == "https" and s.port == 443)):
            netloc = f"{host}:{s.port}"
        q = [(k, v) for k, v in parse_qsl(s.query, keep_blank_values=False)
             if k.lower() not in _TRACKING_PARAMS]
        q.sort()
        path = s.path or "/"
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        return urlunsplit((scheme, netloc, path, urlencode(q), ""))
    except Exception:
        return url.split("#")[0]


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
            CREATE TABLE IF NOT EXISTS fabric_domain_authority (
                domain         TEXT,
                topic_key      TEXT,
                source_type    TEXT,
                base_authority REAL,
                pages          INTEGER DEFAULT 0,
                relevance_sum  REAL DEFAULT 0,
                unique_sum     REAL DEFAULT 0,
                score          REAL DEFAULT 0,
                updated_at     TEXT,
                PRIMARY KEY (domain, topic_key)
            );
            CREATE INDEX IF NOT EXISTS idx_domauth_topic ON fabric_domain_authority(topic_key);
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
    history even when Neo4j is unavailable. Idempotent on (crawl,from,to,rel).
    Also mirrors the edge into the main fabric graph (Neo4j) so discovery
    sessions show up in the data-fabric graph, not just the discovery panel."""
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
    # Mirror into the main fabric graph (best-effort, non-blocking)
    try:
        from_label = ("Dataset" if from_id == parent_ds or not from_id.startswith("http")
                      else "Page")
        asyncio.create_task(_graph_mirror(from_label, from_id, to_label, to_id, rel))
    except Exception:
        pass


async def _graph_mirror(from_label: str, from_id: str, to_label: str,
                        to_id: str, rel: str):
    """Write an edge into the shared fabric graph adapter (Neo4j) so the main
    data-fabric graph reflects discovery structure. A single link() MERGEs both
    nodes and the relationship. No-op if the graph is unavailable."""
    g = _get_graph()
    if not g or not getattr(g, "available", False):
        return
    try:
        await g.link(from_label, from_id, to_label, to_id, rel, {"via": "discovery"})
    except Exception as e:
        log.debug("graph mirror: %s", e)


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

    def _cell_data(cell, base_url: str) -> Dict:
        """Extract text and any hyperlinks from a table cell."""
        text = _clean(cell.get_text(" "))
        links = []
        for a in cell.find_all("a", href=True):
            href = a.get("href", "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            try:
                from urllib.parse import urljoin as _urljoin
                full = _urljoin(base_url, href).split("#")[0]
                if full.startswith(("http://", "https://")):
                    anchor = _clean(a.get_text(" ")) or full
                    links.append({"url": full, "anchor": anchor[:120]})
            except Exception:
                pass
        return {"text": text, "links": links}

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

        # Extract cell data — text + links per cell
        body_rows_raw = []   # list of list of {text, links}
        for tr in body_trs:
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            body_rows_raw.append([_cell_data(c, url) for c in cells])

        # Plain-text version for filtering / slug
        body_rows = [[c["text"] for c in row] for row in body_rows_raw]
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
        for ri, (r_text, r_raw) in enumerate(zip(body_rows[:_MAX_SUBTABLE_ROWS],
                                                   body_rows_raw[:_MAX_SUBTABLE_ROWS])):
            rec: Dict = {}
            cell_links: List[Dict] = []
            for i in range(min(len(cols), len(r_text))):
                if r_text[i]:
                    rec[cols[i]] = r_text[i]
                # Collect all links found in this row's cells
                if i < len(r_raw):
                    cell_links.extend(r_raw[i].get("links", []))
            if not rec:
                continue
            rec["text"] = " | ".join(f"{cols[i]}={r_text[i]}"
                                     for i in range(min(len(cols), len(r_text))) if r_text[i])[:4000]
            rec["_row"] = ri
            # Attach all hyperlinks found within this row's cells
            if cell_links:
                rec["_links"] = cell_links[:20]
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

_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "these", "those",
    "are", "was", "were", "has", "have", "had", "its", "their", "his", "her",
    "all", "any", "can", "will", "may", "about", "into", "over", "more", "most",
    "such", "than", "then", "they", "them", "out", "use", "used", "list",
    "data", "info", "page", "pages", "guide", "wiki", "full", "new", "how",
    "what", "when", "where", "who", "why", "your", "you", "our", "not",
}


def _topic_tokens(topic: str) -> List[str]:
    out = []
    for t in re.split(r"[^a-z0-9]+", (topic or "").lower()):
        if len(t) >= 3 and t not in _STOPWORDS:
            out.append(t)
    return list(dict.fromkeys(out))  # dedup, keep order


def _words(text: str) -> set:
    """Word set (length>=3) for word-boundary matching — avoids 'ash' matching
    'cash'/'washing' which inflated relevance."""
    return set(re.findall(r"[a-z0-9]{3,}", (text or "").lower()))


def _score_topic(text: str, tokens: List[str]) -> float:
    if not tokens:
        return 0.0
    w = _words(text)
    hits = sum(1 for t in tokens if t in w)
    return round(hits / len(tokens), 3)


def _relevance(full_text: str, title: str, url: str, tokens: List[str]) -> float:
    """Page relevance to the topic in [0,1]. Title and URL hits weigh more than
    body hits; with no topic, everything is neutral (0.5)."""
    if not tokens:
        return 0.5
    n = len(tokens)
    head_w = _words(title)
    body_w = _words(full_text)
    u = (url or "").lower()
    present = 0
    weighted = 0.0
    for t in tokens:
        if t in head_w:
            weighted += 1.0; present += 1
        elif t in u:
            weighted += 0.7; present += 1
        elif t in body_w:
            weighted += 0.4; present += 1
    coverage = present / n              # fraction of DISTINCT topic terms found
    avg = weighted / n
    # Breadth matters: a page that hits one term once should not score ~1.0.
    rel = 0.6 * coverage + 0.4 * avg
    if len((full_text or "")) < 200:    # near-empty pages can't be very relevant
        rel *= 0.5
    return round(max(0.0, min(1.0, rel)), 3)


def _topic_coverage(full_text: str, title: str, url: str, tokens: List[str]) -> float:
    """Fraction of DISTINCT topic terms present anywhere (drift signal)."""
    if not tokens:
        return 1.0
    hay = _words(full_text) | _words(title) | _words(url)
    return round(sum(1 for t in tokens if t in hay) / len(tokens), 3)


# ═══════════════════════════════════════════════════════════════════════════
# SOURCE TYPE · DOMAIN AUTHORITY · NODE SCORING
# ═══════════════════════════════════════════════════════════════════════════
_SOURCE_RULES = [
    ("wikipedia.org", "encyclopedia", 0.92), ("bulbapedia", "encyclopedia", 0.90),
    ("fandom.com", "encyclopedia", 0.82), ("wikia.", "encyclopedia", 0.80),
    (".gov", "official", 0.90), (".edu", "official", 0.85),
    ("docs.", "docs", 0.85), ("developer.", "docs", 0.82),
    ("serebii", "guide", 0.80), ("gamefaqs", "guide", 0.72), ("ign.com", "guide", 0.68),
    ("gamespot", "guide", 0.66), ("stackoverflow", "qa", 0.62),
    ("stackexchange", "qa", 0.60), ("quora", "qa", 0.45),
    ("reddit.com", "forum", 0.42), ("boards.", "forum", 0.40), ("forum", "forum", 0.40),
    ("discord", "social", 0.30), ("twitter.com", "social", 0.30), ("x.com", "social", 0.30),
    ("facebook", "social", 0.28), ("instagram", "social", 0.28), ("tiktok", "social", 0.25),
    ("medium.com", "blog", 0.42), ("blogspot", "blog", 0.38), ("wordpress", "blog", 0.38),
    ("substack", "blog", 0.40), ("amazon.", "store", 0.20), ("ebay.", "store", 0.20),
]
_WIKI_HINT = re.compile(r"(wiki|pedia|encycloped)", re.I)


def _classify_source(url: str, structure: Dict = None):
    """Non-LLM source-type + base authority from domain patterns / structure."""
    host = (urlparse(url or "").netloc or "").lower()
    for pat, st, auth in _SOURCE_RULES:
        if pat in host:
            return st, auth
    if _WIKI_HINT.search(host):
        return "encyclopedia", 0.80
    if structure and len(structure.get("tables", []) or []) >= 3:
        return "reference", 0.65
    return "unknown", 0.50


_AUTHORITATIVE_TYPES = {"encyclopedia", "reference", "official", "docs", "guide"}


def _quality_score(text: str, structure: Dict = None) -> float:
    """Non-LLM content-quality proxy [0,1]: rewards length + structure
    (headings/tables), penalises link-farm / near-empty pages."""
    t = text or ""
    n = len(t)
    if n < 150:
        return 0.1
    length_s = min(1.0, n / 6000.0)
    head = len((structure or {}).get("headings", []) or [])
    tabs = len((structure or {}).get("tables", []) or [])
    struct_s = min(1.0, head * 0.08 + tabs * 0.12)
    links = t.lower().count("http")
    density_pen = min(0.4, links / max(1.0, n / 400.0) * 0.1)
    return round(max(0.05, min(1.0, 0.55 * length_s + 0.45 * struct_s - density_pen)), 3)


def _novelty(text: str, seen_tokens: set) -> float:
    """Fraction of NEW content words vs what the crawl has already accumulated."""
    w = _words(text)
    if not w:
        return 0.0
    return round(len(w - seen_tokens) / len(w), 3)


def _usefulness(relevance: float, authority: float, quality: float,
                novelty: float) -> float:
    """Composite node usefulness — relevance gated by authority/quality/novelty."""
    return round(max(0.0, min(1.0,
        relevance * (0.5 + 0.5 * authority) * (0.5 + 0.5 * quality)
        * (0.45 + 0.55 * novelty))), 3)


def _topic_key(topic: str) -> str:
    return "|".join(_topic_tokens(topic)[:6]) or "*"


def _domain_auth_get(domain: str, topic_key: str):
    try:
        r = _sqlite_conn().execute(
            "SELECT source_type, base_authority, pages, relevance_sum, unique_sum, "
            "score FROM fabric_domain_authority WHERE domain=? AND topic_key=?",
            (domain, topic_key)).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def _domain_auth_update(domain, topic_key, source_type, base_auth, relevance, novelty):
    """Learned domain-relevance-vs-topic ('indexing'): accumulate per-domain
    stats so good sources (e.g. an encyclopedia for this topic) rise over time."""
    try:
        conn = _sqlite_conn()
        r = conn.execute("SELECT pages, relevance_sum, unique_sum FROM "
                         "fabric_domain_authority WHERE domain=? AND topic_key=?",
                         (domain, topic_key)).fetchone()
        if r:
            pages = (r["pages"] or 0) + 1
            rsum = (r["relevance_sum"] or 0.0) + relevance
            usum = (r["unique_sum"] or 0.0) + novelty
        else:
            pages, rsum, usum = 1, relevance, novelty
        avg_rel, avg_nov = rsum / pages, usum / pages
        score = round(0.4 * base_auth + 0.4 * avg_rel + 0.2 * avg_nov, 4)
        conn.execute(
            "INSERT OR REPLACE INTO fabric_domain_authority "
            "(domain, topic_key, source_type, base_authority, pages, relevance_sum, "
            "unique_sum, score, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (domain, topic_key, source_type, base_auth, pages, rsum, usum, score,
             now_iso()))
        conn.commit()
        return score, avg_rel, avg_nov, pages
    except Exception as e:
        log.debug("domain auth update %s: %s", domain, e)
        return base_auth, relevance, novelty, 1


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
             json.dumps(queue[:8000]), json.dumps(list(visited)[:100000]),
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


def _api_root_candidates(url: str) -> List[str]:
    """Walk up an API path to plausible index/root URLs (most-specific first)."""
    try:
        p = urlparse(url)
    except Exception:
        return []
    segs = [s for s in p.path.split("/") if s]
    out = []
    for i in range(len(segs), 0, -1):
        out.append(f"{p.scheme}://{p.netloc}/" + "/".join(segs[:i]) + "/")
    out.append(f"{p.scheme}://{p.netloc}/")
    seen, res = set(), []
    for u in out:
        if u not in seen:
            seen.add(u); res.append(u)
    return res[:6]


async def _maybe_api_enumeration(surface: Dict, parent_ds: str, crawl_id: str,
                                 extra_tags: List[str], budget: List[int]):
    """A discovered JSON API endpoint (e.g. .../api/v2/pokemon/ditto) is just ONE
    resource. Find the API's index/root (or an OpenAPI spec) and enumerate ALL of
    its endpoints into an api_endpoints sub-table, so we capture the whole API."""
    if surface.get("kind") not in ("json_api", "json") or budget[0] <= 0:
        return
    url = surface.get("url") or ""
    if not url.startswith(("http://", "https://")):
        return
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    endpoints: List[Dict] = []
    title = urlparse(url).netloc
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers=_HTTP_HEADERS) as c:
            # 1) richest source: an OpenAPI/Swagger spec near the host root
            for sp in ("/openapi.json", "/swagger.json", "/api/openapi.json",
                       "/v1/openapi.json", "/v2/openapi.json", "/api-docs"):
                if budget[0] <= 0:
                    break
                try:
                    r = await c.get(base + sp)
                except Exception:
                    continue
                if r.status_code < 400 and "json" in r.headers.get("content-type", "").lower():
                    try:
                        spec = r.json()
                    except Exception:
                        spec = None
                    if isinstance(spec, dict) and isinstance(spec.get("paths"), dict):
                        await _maybe_openapi_subtable(
                            {"kind": "openapi", "url": base + sp},
                            parent_ds, crawl_id, extra_tags, budget)
                        return
            # 2) resource-index roots (e.g. PokeAPI GET /api/v2/ -> {name: url, ...})
            for root in _api_root_candidates(url):
                if budget[0] <= 0:
                    break
                budget[0] -= 1
                try:
                    r = await c.get(root)
                except Exception:
                    continue
                if r.status_code >= 400 or "json" not in r.headers.get("content-type", "").lower():
                    continue
                try:
                    data = r.json()
                except Exception:
                    continue
                pairs = []
                if isinstance(data, dict):
                    pairs = [(k, v) for k, v in data.items()
                             if isinstance(v, str) and v.startswith("http")]
                    if not pairs and isinstance(data.get("results"), list):
                        for it in data["results"]:
                            if isinstance(it, dict) and isinstance(it.get("url"), str):
                                pairs.append((it.get("name") or it["url"], it["url"]))
                elif isinstance(data, list):
                    for it in data:
                        if isinstance(it, dict) and isinstance(it.get("url"), str):
                            pairs.append((it.get("name") or it["url"], it["url"]))
                for name, ep in pairs[:_MAX_SUBTABLE_ROWS]:
                    endpoints.append({"name": str(name)[:120], "url": ep, "method": "GET",
                                      "text": f"GET {ep} {name}".strip()})
                if endpoints:
                    title = root
                    break
    except Exception as e:
        log.debug("api enumerate %s: %s", url, e)
    if len(endpoints) >= 2:
        await ingest_subtable(parent_ds, {
            "kind": "api_endpoints",
            "title": f"API endpoints @ {title}"[:80],
            "columns": ["name", "url", "method"], "records": endpoints,
        }, url, crawl_id, extra_tags)


async def _build_global_entity_links(ds_id: str, min_shared: int = 2,
                                     max_links: int = 300) -> int:
    """Link entities that co-occur across MULTIPLE pages of the scan (not just
    within one source) as soft 'RELATED' relations — rendered as dotted edges.
    Returns the number of relations written."""
    persist = None
    try:
        persist = _wa()._persist_entity_graph
    except Exception:
        return 0
    conn = _sqlite_conn()
    try:
        rows = conn.execute(
            "SELECT entity_id, record_id, mention_count FROM fabric_entity_mentions m "
            "JOIN fabric_entities e ON e.id = m.entity_id "
            "WHERE m.dataset_id=? ORDER BY e.mention_count DESC LIMIT 20000", (ds_id,)).fetchall()
    except Exception:
        return 0
    # entity -> set(pages)
    ent_pages: Dict[str, set] = {}
    for r in rows:
        ent_pages.setdefault(r["entity_id"], set()).add(r["record_id"])
    # only consider entities seen on 2+ pages (meaningful), cap the strongest
    strong = sorted([(e, p) for e, p in ent_pages.items() if len(p) >= min_shared],
                    key=lambda x: len(x[1]), reverse=True)[:60]
    rels = []
    for i in range(len(strong)):
        for j in range(i + 1, len(strong)):
            shared = strong[i][1] & strong[j][1]
            if len(shared) >= min_shared:
                rels.append({"from": strong[i][0], "to": strong[j][0],
                             "rel": "RELATED", "props": {"shared_pages": len(shared)}})
            if len(rels) >= max_links:
                break
        if len(rels) >= max_links:
            break
    if not rels:
        return 0
    try:
        conn = _sqlite_conn()
        for r in rels:
            conn.execute(
                "INSERT OR REPLACE INTO fabric_entity_relations "
                "(from_id, to_id, rel, props, dataset_id, created_at) VALUES (?,?,?,?,?,?)",
                (r["from"], r["to"], r["rel"],
                 json.dumps(r.get("props", {})), ds_id, now_iso()))
        conn.commit()
    except Exception as e:
        log.debug("persist global rels: %s", e)
        return 0
    return len(rels)


# ── Crawl concurrency: parallelize across distinct topics/urls, queue within ──
# Crawls keyed by the same dataset (a topic or a url maps to one dataset) run one
# at a time; crawls for different topics/urls run concurrently. _run_crawl is the
# single chokepoint for every crawl path (topic / crawl / continue / from_dataset
# / expand) and is never called from within itself, so wrapping it is deadlock-free.
_CRAWL_LOCKS: Dict[str, "asyncio.Lock"] = {}
_CRAWL_LOCKS_GUARD: "Optional[asyncio.Lock]" = None
# Per-host fetch limiter: bound concurrent requests to the SAME host across ALL
# crawls (politeness), while different hosts fetch in parallel. Tunable via
# FABRIC_HOST_FETCH_CONCURRENCY.
_HOST_FETCH_SEMS: Dict[str, "asyncio.Semaphore"] = {}
_HOST_FETCH_GUARD: "Optional[asyncio.Lock]" = None
_HOST_FETCH_CONC = max(1, int(os.getenv("FABRIC_HOST_FETCH_CONCURRENCY", "2")))


async def _host_fetch_slot(host: str) -> "asyncio.Semaphore":
    global _HOST_FETCH_GUARD
    if _HOST_FETCH_GUARD is None:
        _HOST_FETCH_GUARD = asyncio.Lock()
    h = (host or "default").lower()
    async with _HOST_FETCH_GUARD:
        s = _HOST_FETCH_SEMS.get(h)
        if s is None:
            s = asyncio.Semaphore(_HOST_FETCH_CONC)
            _HOST_FETCH_SEMS[h] = s
        return s


async def _mirror_surface_to_fabric(surface: Dict, parent_ds: str):
    """Immediate per-surface fabric-graph write so crashes don't lose structure."""
    try:
        a=_df().GRAPHS.get("fabric")
        if not a or not getattr(a,"available",False): return
        sid=surface.get("id")
        if not sid: return
        await a.upsert_node("Dataset",parent_ds,{"id":parent_ds,"discovered":True})
        await a.upsert_node("Surface",sid,{"id":sid,
            "name":surface.get("label") or surface.get("url") or sid,
            "url":surface.get("url") or "","kind":surface.get("kind") or "",
            "source_type":surface.get("source_type") or "",
            "confidence":surface.get("confidence") or 0})
        await a.link("Dataset",parent_ds,"Surface",sid,rel="HAS_SURFACE")
    except Exception as e: log.debug("mirror surface: %s",e)


async def _mirror_subtable_to_fabric(sub_ds:str,parent_ds:str,title:str,kind:str):
    """Immediate per-subtable fabric-graph write."""
    try:
        a=_df().GRAPHS.get("fabric")
        if not a or not getattr(a,"available",False): return
        if not sub_ds: return
        await a.upsert_node("Dataset",parent_ds,{"id":parent_ds,"discovered":True})
        await a.upsert_node("Dataset",sub_ds,{"id":sub_ds,"name":title or sub_ds,"kind":kind or "table"})
        await a.link("Dataset",parent_ds,"Dataset",sub_ds,rel="HAS_DATA_SUBSET")
    except Exception as e: log.debug("mirror subtable: %s",e)


async def _mirror_crawl_to_fabric(crawl_id: str, ds_id: str, label: str = ""):
    """Mirror the discovered structure into the Neo4j *fabric* graph so a topic/
    url crawl shows up in the main data-fabric graph pane alongside ordinary
    datasets: the topic Dataset, its data sub-tables (HAS_DATA_SUBSET) and its
    detected surfaces (recorded as :Surface nodes via HAS_SURFACE — i.e. surfaces
    are stored in the fabric like tables). Best-effort and non-fatal."""
    try:
        adapter = _df().GRAPHS.get("fabric")
    except Exception:
        adapter = None
    if not adapter or not getattr(adapter, "available", False):
        return
    try:
        await adapter.upsert_node("Dataset", ds_id,
                                  {"id": ds_id, "name": label or ds_id,
                                   "kind": "topic", "discovered": True})
        conn = _sqlite_conn()
        # data sub-tables → HAS_DATA_SUBSET (the sub Dataset nodes already exist
        # from ingest; we add the structural edge from the topic dataset)
        for r in conn.execute(
                "SELECT DISTINCT sub_dataset, title, kind FROM fabric_subtables "
                "WHERE crawl_id=? OR parent_dataset=?", (crawl_id, ds_id)).fetchall():
            sub = r["sub_dataset"]
            if not sub:
                continue
            await adapter.upsert_node("Dataset", sub,
                                      {"id": sub, "name": r["title"] or sub,
                                       "kind": r["kind"] or "table"})
            await adapter.link("Dataset", ds_id, "Dataset", sub, rel="HAS_DATA_SUBSET")
        # detected surfaces → :Surface node + HAS_SURFACE
        for s in conn.execute(
                "SELECT id, url, label, kind, source_type, confidence, promoted, source_id "
                "FROM fabric_surfaces WHERE crawl_id=? OR parent_dataset=?",
                (crawl_id, ds_id)).fetchall():
            sid = s["id"]
            if not sid:
                continue
            await adapter.upsert_node("Surface", sid,
                                      {"id": sid, "name": s["label"] or s["url"] or sid,
                                       "url": s["url"] or "", "kind": s["kind"] or "",
                                       "source_type": s["source_type"] or "",
                                       "confidence": s["confidence"] or 0,
                                       "promoted": bool(s["promoted"]),
                                       "source_id": s["source_id"] or ""})
            await adapter.link("Dataset", ds_id, "Surface", sid, rel="HAS_SURFACE")
    except Exception as e:
        log.debug("fabric mirror %s: %s", ds_id, e)


async def _crawl_key_lock(key: str) -> "asyncio.Lock":
    global _CRAWL_LOCKS_GUARD
    if _CRAWL_LOCKS_GUARD is None:
        _CRAWL_LOCKS_GUARD = asyncio.Lock()
    k = (key or "").strip().lower() or "default"
    async with _CRAWL_LOCKS_GUARD:
        lk = _CRAWL_LOCKS.get(k)
        if lk is None:
            lk = asyncio.Lock()
            _CRAWL_LOCKS[k] = lk
        return lk


async def _run_crawl(crawl_id: str, ds_id: str, seed_url: str, config: Dict,
                     queue: List, visited: Set[str]) -> Dict:
    # Serialize per topic/url (== per dataset); different ones proceed in parallel.
    key = ds_id or (urlparse(seed_url).netloc if seed_url else "") or "default"
    lock = await _crawl_key_lock(key)
    if lock.locked():
        try:
            await emit_event({"type": "fabric.discover.progress",
                              "acquisition_id": crawl_id, "stage": "queued",
                              "engine": "discovery",
                              "message": f"queued behind another crawl for '{key}'"})
        except Exception:
            pass
    async with lock:
        return await _run_crawl_impl(crawl_id, ds_id, seed_url, config, queue, visited)


async def _run_crawl_impl(crawl_id: str, ds_id: str, seed_url: str, config: Dict,
                     queue: List, visited: Set[str]) -> Dict:
    wa = _wa()
    ingest_fn = _ingest()
    hostname = urlparse(seed_url).netloc if seed_url else ""

    no_limit    = bool(config.get("no_limit", False))
    max_pages   = (999999 if no_limit else max(1, min(2000, int(config.get("max_pages", 60)))))
    rep_dropoff = int(config.get("repetition_dropoff", 0))
    _md_raw     = int(config.get("max_depth", 4))
    depth_unlimited = bool(config.get("unlimited_depth", False)) or _md_raw <= 0
    max_depth   = 999999 if depth_unlimited else max(1, min(40, _md_raw))
    # exhaustive capture: 0 = UNLIMITED links / content per page
    max_links_pp   = max(0, int(config.get("max_links_per_page", 0)))   # enqueue cap
    page_link_cap  = max(0, int(config.get("page_link_cap", 0)))        # extractor cap
    page_text_cap  = max(0, int(config.get("page_text_cap", 0)))        # extractor text cap
    max_record_chars = max(0, int(config.get("max_record_chars", 0)))   # stored text
    same_domain = bool(config.get("same_domain", True))
    do_surfaces = bool(config.get("detect_surfaces", True))
    do_subtab   = bool(config.get("extract_subtables", True))
    auto_promote = bool(config.get("auto_promote", False))
    auto_pull    = bool(config.get("auto_pull", False))
    do_entities  = bool(config.get("extract_entities", True))
    entities_llm = bool(config.get("extract_entities_llm", True))
    llm_tagging  = bool(config.get("llm_tagging", False))
    min_relevance = float(config.get("min_relevance", 0.0) or 0.0)
    topic        = config.get("topic", "") or ""
    topic_dropoff = int(config.get("topic_dropoff", 3))
    extra_tags   = [t.strip() for t in (config.get("tags", "") or "").split(",") if t.strip()]
    tokens       = _topic_tokens(topic)

    neg = wa.NegativeFilter(
        negative_words=[w.strip() for w in (config.get("negative_words", "") or "").split(",") if w.strip()],
        negative_url_patterns=[p.strip() for p in (config.get("negative_urls", "") or "").split(",") if p.strip()],
    )

    # ── Required keyword filter ──────────────────────────────────────────────
    # If set, pages that do NOT contain this word (exact or fuzzy) are rejected.
    _req_kw       = (config.get("required_keyword", "") or "").strip().lower()
    _req_kw_mode  = (config.get("required_keyword_mode", "fuzzy") or "fuzzy").lower()

    def _req_keyword_ok(text: str, title: str, url_str: str) -> bool:
        """Return True if the required keyword passes (or no keyword is set)."""
        if not _req_kw:
            return True
        haystack = (text + " " + title + " " + url_str).lower()
        if _req_kw_mode == "exact":
            import re as _re
            return bool(_re.search(r'\b' + _re.escape(_req_kw) + r'\b', haystack))
        else:  # fuzzy: substring anywhere
            return _req_kw in haystack

    pages_fetched = 0
    rep_consecutive = 0
    rep_seen_tokens: set = set()
    # ── node-scoring / drift / whole-domain-swallow state ────────────────────
    _tkey = _topic_key(topic)
    seen_words: set = set()                 # running corpus vocabulary (novelty)
    domain_stats: Dict[str, Dict] = {}      # per-crawl per-domain richness
    domain_rich: Dict[str, bool] = {}       # domains currently being "swallowed"
    path_clusters: Dict[str, Dict] = {}      # path-prefix -> {count,rel,text_boost} for structured-area detection
    neg_tokens: set = set(w.lower() for w in
                          (config.get("negative_words", "") or "").split(",") if w.strip())
    drift_guard = bool(config.get("drift_guard", True)) and len(tokens) >= 2
    drift_min_cov = float(config.get("drift_min_coverage", 0.34) or 0.0)
    llm_drift = bool(config.get("llm_drift_gate", False)) and bool(topic)
    swallow_on = bool(config.get("swallow_domains", True))
    swallow_min_pages = max(2, int(config.get("swallow_min_pages", 3)))
    swallow_min_rel = float(config.get("swallow_min_relevance", 0.45) or 0.0)
    swallow_extra_depth = max(0, int(config.get("swallow_extra_depth", 3)))
    # rolling LLM topic description state
    roll_desc_on = bool(config.get("rolling_description", False))
    roll_topic = config.get("topic", "") or seed_url
    topic_desc = config.get("topic_description", "")
    roll_buf: list = []
    surfaces_found = 0
    subtables_found = 0
    entities_found = 0
    promoted = 0
    spec_budget = [int(config.get("max_spec_fetches", 5))]
    max_conc = max(1, min(16, int(config.get("max_concurrency", 4) or 4)))
    ent_workers = max(1, min(8, int(config.get("entity_workers", 3) or 3)))

    async def _emit(stage, **kw):
        try:
            await emit_event({"type": "fabric.discover.progress",
                              "acquisition_id": crawl_id, "dataset_id": ds_id,
                              "stage": stage, "engine": "discovery", **kw})
        except Exception:
            pass

    await _emit("starting", url=seed_url, max_pages=max_pages, max_depth=max_depth,
                resumed=bool(visited))

    # ── LLM "research brief": refine the GOAL up front (what kind of thing the
    #    topic is, the properties to capture, distinctive must-terms and sibling
    #    avoid-terms) and keep refining as results arrive — used to steer
    #    relevance and reject drift. ───────────────────────────────────────────
    brief_on = bool(config.get("topic_brief", False)) and bool(topic)
    brief_rounds = max(0, int(config.get("brief_rounds", 3)))
    brief_must: set = set()
    brief: Dict = {}
    if brief_on:
        try:
            brief = await _gen_brief(topic) or {}
            brief_must = set(w.lower() for t in brief.get("must_terms", [])
                             for w in _words(t))
            for t in brief.get("avoid_terms", []):
                neg_tokens.update(_words(t))
            config["topic_brief_data"] = brief
            await _emit("brief", topic=topic, brief=brief,
                        message=f"brief: {brief.get('type','?')} \u2014 must: "
                                + ", ".join(brief.get("must_terms", [])[:6])
                                + (" | avoid: " + ", ".join(brief.get("avoid_terms", [])[:6])
                                   if brief.get("avoid_terms") else ""))
        except Exception as e:
            log.debug("initial brief: %s", e)

    # ── Background processing queue: LLM tagging + entity extraction run in
    #    their OWN worker tasks so they never block fetching/crawling. The 2nd
    #    layer (entity graph) is therefore built in tandem with discovery. ────
    _NW = ent_workers
    # Priority 0 = high (LLM steer / brief / structural subtable work that
    # blocks 2nd/3rd order linking); Priority 1 = normal entity/tagging.
    proc_q: "asyncio.PriorityQueue" = asyncio.PriorityQueue(maxsize=400)
    _pq_seq = 0  # tiebreaker so equal-priority jobs stay FIFO

    def _pq_put_nowait(job, priority=1):
        nonlocal _pq_seq
        proc_q.put_nowait((priority, _pq_seq, job))
        _pq_seq += 1

    async def _pq_put(job, priority=1):
        nonlocal _pq_seq
        await proc_q.put((priority, _pq_seq, job))
        _pq_seq += 1

    async def _proc_worker():
        nonlocal entities_found
        while True:
            item = await proc_q.get()
            try:
                if item is None:
                    return
                _pri, _seq, job = item
                if job is None:
                    return
                jtext, jtitle, jrid, jds, jck, jurl, do_tag, do_ent, llm_ent = job
                if do_tag and topic:
                    try:
                        tg = await _llm_tag(jtext, jtitle, topic)
                        tags = tg.get("tags") or []
                        if tags or tg.get("relevance") is not None:
                            _patch_record_tags(jds, jrid, tags, tg.get("relevance"))
                        if tags:
                            await _emit("llm_action", action="tagged", url=jurl,
                                        tags=tags[:8],
                                        message="LLM tags: " + ", ".join(tags[:8]))
                    except Exception as e:
                        log.debug("bg tag %s: %s", jurl, e)
                if do_ent:
                    try:
                        if llm_ent:
                            await _emit("llm_action", action="entities", url=jurl,
                                        message=f"LLM entity extraction (bg): {jurl[:60]}")
                        n = await extract_entities_for_record(
                            jtext, jrid, jds, jck, llm_assist=llm_ent)
                        if n:
                            entities_found += n
                            await _emit("entity_found", url=jurl, count=n,
                                        dataset_id=jds,
                                        message=f"+{n} entities from {jurl[:60]}")
                    except Exception as e:
                        log.debug("bg entities %s: %s", jurl, e)
            finally:
                proc_q.task_done()

    _proc_workers = [asyncio.create_task(_proc_worker()) for _ in range(_NW)]
    # Dedicated semaphore for blocking LLM steer/drift-gate calls on the fetch
    # path — gives them up to 2 concurrent LLM slots independent of the entity
    # worker queue so they don't wait behind bulk extraction jobs.
    _steer_sem = asyncio.Semaphore(2)

    # ── In-tandem entity consolidator: periodically merges cross-type / alias
    #    duplicates (e.g. multi-type "Pikachu") WHILE the crawl runs, so the
    #    graph stays clean as data arrives rather than only at the end. ─────────
    _consol_stop = asyncio.Event()
    _consolidator = None

    async def _periodic_consolidate():
        interval = max(8, int(config.get("consolidate_interval", 20)))
        while not _consol_stop.is_set():
            try:
                await asyncio.wait_for(_consol_stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if _consol_stop.is_set():
                break
            try:
                res = await _call_cap("fabric.entity_graph.consolidate",
                                      dataset_id=ds_id, link=False)
                if res and res.get("merged"):
                    await _emit("consolidated", merged=res["merged"],
                                dataset_id=ds_id,
                                message=f"merged {res['merged']} duplicate entities "
                                        "(in tandem)")
            except Exception as e:
                log.debug("periodic consolidate: %s", e)

    if do_entities and config.get("consolidate_entities", True):
        _consolidator = asyncio.create_task(_periodic_consolidate())

    # ── Iterative brief refinement: a few rounds, building on results as they
    #    arrive (updates must/avoid terms used for relevance + drift). ─────────
    _brief_refiner = None
    if brief_on and brief_rounds > 0:
        async def _refine_brief():
            for _r in range(brief_rounds):
                try:
                    await asyncio.wait_for(_consol_stop.wait(), timeout=25)
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    recs = _synth_records(ds_id, limit=30)
                    if not recs:
                        continue
                    samples = "\n".join(("- " + (r["title"] or "")[:80] + ": "
                                         + (r["text"] or "")[:300]) for r in recs[:20])
                    nb = await _gen_brief(topic, samples=samples, prev=brief)
                    if nb:
                        brief.update(nb)
                        brief_must.update(w.lower() for t in nb.get("must_terms", [])
                                          for w in _words(t))
                        for t in nb.get("avoid_terms", []):
                            neg_tokens.update(_words(t))
                        config["topic_brief_data"] = brief
                        await _emit("brief_refined", round=_r + 1, brief=nb,
                                    message=f"brief refined (round {_r+1}) \u2014 must: "
                                            + ", ".join(nb.get("must_terms", [])[:6]))
                except Exception as e:
                    log.debug("brief refine: %s", e)
        _brief_refiner = asyncio.create_task(_refine_brief())

    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                  headers=_HTTP_HEADERS) as client:
        async def _process(page_url, depth, dry, parent_url):
            nonlocal pages_fetched, surfaces_found, subtables_found, promoted
            nonlocal rep_consecutive, roll_buf, _stop
            await _emit("page_fetching", url=page_url, depth=depth,
                        queued=len(queue), pages=pages_fetched,
                        message=f"fetch [{pages_fetched}/{max_pages}] d{depth} "
                                f"{page_url[:90]}")

            try:
                async with (await _host_fetch_slot(urlparse(page_url).netloc)):
                    await asyncio.sleep(_CRAWL_DELAY)
                    resp = await client.get(page_url)
                if resp.status_code >= 400:
                    return
                ct = resp.headers.get("content-type", "").lower()
            except Exception as e:
                log.debug("fetch %s: %s", page_url, e)
                return
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
                return
            if "text/html" not in ct and "text/plain" not in ct:
                # Non-HTML, non-data file (e.g. .pdf, .json with bad content-type,
                # .epub, etc.).  Try the parent directory — it may be an index page
                # or directory listing that contains links worth crawling.
                _NON_HTML_FILE_EXTS = {
                    ".pdf", ".epub", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
                    ".zip", ".gz", ".tar", ".rar", ".7z",
                    ".mp4", ".mp3", ".ogg", ".webm", ".avi",
                    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
                    ".wasm", ".bin", ".exe", ".dmg",
                }
                _url_ext = os.path.splitext(urlparse(page_url).path.lower())[1]
                if _url_ext in _NON_HTML_FILE_EXTS or "application/" in ct:
                    # Derive parent: strip the filename component
                    _parsed_pu = urlparse(page_url)
                    _parent_path = "/".join(_parsed_pu.path.rstrip("/").split("/")[:-1]) or "/"
                    if _parent_path and _parent_path != _parsed_pu.path:
                        _parent_url = _parsed_pu._replace(path=_parent_path, query="", fragment="").geturl()
                        # Only enqueue if we haven't already visited this parent
                        _pnorm = _normalize_url(_parent_url)
                        if _pnorm not in visited and depth > 0:
                            visited.add(_pnorm)
                            queue.append((_parent_url, max(0, depth - 1), False, page_url))
                            await _emit("parent_enqueued", url=_parent_url,
                                        child=page_url, ext=_url_ext,
                                        message=f"non-HTML file {_url_ext!r} -> queuing parent {_parent_url}")
                return


            structure = wa._extract_page_structure(
                raw, page_url, max_links=page_link_cap, max_text=page_text_cap)
            title = structure.get("title", "")
            full_text = structure.get("full_text", "")
            page_links = structure.get("links", [])
            await _emit("content_extracted", url=page_url,
                        chars=len(full_text), links=len(page_links),
                        title=(title or "")[:80],
                        message=f"extracted {len(full_text)} chars, "
                                f"{len(page_links)} links")

            if neg.content_dominated(full_text):
                await _emit("page_skipped", url=page_url, reason="boilerplate/ad-dominated")
                return

            # Topic gate (same dropoff semantics as fabric.web.acquire)
            if tokens:
                hay = (full_text + " " + title + " " + page_url).lower()
                has_topic = any(t in hay for t in tokens)
                next_dry = 0 if has_topic else dry + 1
                if next_dry > topic_dropoff:
                    return
            else:
                next_dry = 0

            # Relevance gate uses the fast HEURISTIC score only — LLM tagging
            # runs later in the background queue (never blocks the crawl).
            relevance = _relevance(full_text, title, page_url, tokens)
            llm_tags: List[str] = []
            _host = urlparse(page_url).netloc
            # brief must-terms boost: pages hitting distinctive terms rank higher
            if brief_must and (brief_must & (_words(full_text) | _words(title))):
                relevance = round(min(1.0, relevance + 0.15), 3)

            # ── Node scoring: relevance · authority · quality · novelty ──────
            src_type, base_auth = _classify_source(page_url, structure)
            novelty = _novelty(full_text, seen_words)
            quality = _quality_score(full_text, structure)
            learned = _domain_auth_get(_host, _tkey)
            authority = round(min(1.0, 0.6 * base_auth
                                  + 0.4 * (learned["score"] if learned else base_auth)), 3)
            usefulness = _usefulness(relevance, authority, quality, novelty)

            # ── Topic-drift guard (non-LLM): for multi-term topics require a
            #    minimum share of the DISTINCTIVE terms, and reject pages where
            #    learned negative/sibling terms dominate and coverage is weak. ──
            if depth > 0 and drift_guard:
                cov = _topic_coverage(full_text, title, page_url, tokens)
                neg_hit = bool(neg_tokens & (_words(full_text) | _words(title)))
                if cov < drift_min_cov or (neg_hit and cov < 0.5):
                    # optional LLM second-opinion only for borderline pages
                    keep = False
                    if llm_drift and 0.15 <= relevance <= 0.55:
                        try:
                            async with _steer_sem:
                                jd = _sjson(await _llm_generate(
                                    "TOPIC: " + topic + "\nPAGE TITLE: " + (title or "")[:160]
                                    + "\nEXCERPT: " + (full_text or "")[:700]
                                    + '\n\nIs this page specifically about the TOPIC, or a '
                                    "different/sibling subject (e.g. a different game/edition "
                                    'in the same series)? Return ONLY '
                                    '{"on_topic":true|false,"about":"short"}.',
                                    "You judge topical match strictly. JSON only.",
                                    timeout=30)) or {}
                            keep = bool(jd.get("on_topic"))
                            if not keep:
                                for w in _words(jd.get("about", "")):
                                    if w not in tokens:
                                        neg_tokens.add(w)   # learn sibling terms
                        except Exception:
                            keep = relevance >= 0.4
                    if not keep:
                        await _emit("page_skipped", url=page_url, relevance=relevance,
                                    reason="off_topic_drift",
                                    message=f"drift-skip ({cov:.0%} topic coverage): "
                                            + page_url[:70])
                        return

            # Drop clearly off-topic pages (seeds at depth 0 always kept)
            if depth > 0 and topic and min_relevance > 0 and relevance < min_relevance:
                await _emit("page_skipped", url=page_url, relevance=relevance, reason="low_relevance")
                return

            # ── Required keyword gate (hard filter, any depth including 0) ──
            if _req_kw and not _req_keyword_ok(full_text, title, page_url):
                await _emit("page_skipped", url=page_url, reason="required_keyword_missing",
                            message=f"no {'exact' if _req_kw_mode=='exact' else 'fuzzy'} match for '{_req_kw}'")
                return

            seen_words.update(_words(full_text))
            # learned domain authority for this topic (Google-indexing style)
            d_score, d_avg_rel, d_avg_nov, d_pages = _domain_auth_update(
                _host, _tkey, src_type, base_auth, relevance, novelty)
            # per-crawl richness + whole-domain "swallow" decision
            ds_ = domain_stats.setdefault(_host, {"pages": 0, "rel": 0.0, "nov": 0.0,
                                                  "rec_rel": 0.0, "rec_nov": 0.0})
            ds_["pages"] += 1; ds_["rel"] += relevance; ds_["nov"] += novelty
            ds_["rec_rel"] = 0.6 * ds_["rec_rel"] + 0.4 * relevance   # EMA window
            ds_["rec_nov"] = 0.6 * ds_["rec_nov"] + 0.4 * novelty
            if swallow_on:
                # Swallow condition: authoritative source type OR a domain that has
                # proved itself via the learned index with score ≥0.68. The learned
                # score is 0.4*base_auth + 0.4*avg_rel + 0.2*avg_nov — forums
                # (base 0.40) and social (base 0.25-0.30) cannot reach 0.68 even
                # with perfect relevance/novelty, so they are naturally excluded.
                # Only high-quality reference sites, specialised wikis, and
                # authoritative niche sources can qualify this way.
                _is_authoritative = (src_type in _AUTHORITATIVE_TYPES
                                     or (learned and learned.get("score", 0) >= 0.68))
                if _is_authoritative:
                    avg_rel = ds_["rel"] / ds_["pages"]
                    # Learned-only domains need slightly more evidence before swallowing
                    _min_pages = swallow_min_pages if src_type in _AUTHORITATIVE_TYPES else swallow_min_pages + 2
                    if (not domain_rich.get(_host) and ds_["pages"] >= _min_pages
                            and avg_rel >= swallow_min_rel
                            and ds_["nov"] / ds_["pages"] >= 0.15):
                        domain_rich[_host] = True
                        await _emit("domain_rich", url=page_url, domain=_host,
                                    source_type=src_type, authority=authority,
                                    message=f"rich source \u2014 swallowing {_host} "
                                            f"({src_type}, rel {avg_rel:.2f})")
                    elif domain_rich.get(_host) and (ds_["rec_rel"] < swallow_min_rel * 0.6
                                                     and ds_["rec_nov"] < 0.12):
                        domain_rich[_host] = False
                        await _emit("domain_dropoff", url=page_url, domain=_host,
                                    message=f"drop-off \u2014 stop swallowing {_host}")

            # ── Structured-area path-cluster detector ─────────────────────────
            # Track how many high-relevance pages we have seen under each 2-segment
            # URL path prefix (e.g. /wiki/, /guide/chapter/).  When >= 3 relevant
            # pages share a prefix AND their average relevance > 0.5, treat the
            # whole prefix as a "structured content area" and boost:
            #   • text cap removed (full text captured, not just first N chars)
            #   • all links under that prefix get +0.4 topic score boost
            # This ensures video-game guides, wiki sections, tutorial series etc.
            # are captured in full rather than sampled.
            _purl = urlparse(page_url)
            _path_parts = [p for p in _purl.path.split("/") if p]
            _pprefix = _purl.netloc + "/" + "/".join(_path_parts[:2])
            _pc = path_clusters.setdefault(_pprefix, {"count": 0, "rel": 0.0, "boosted": False})
            _pc["count"] += 1
            _pc["rel"] = (_pc["rel"] * (_pc["count"] - 1) + relevance) / _pc["count"]
            if (not _pc["boosted"] and _pc["count"] >= 3 and _pc["rel"] >= 0.50):
                _pc["boosted"] = True
                await _emit("structured_area_detected", url=page_url,
                            prefix=_pprefix, pages=_pc["count"],
                            avg_rel=round(_pc["rel"], 2),
                            message=f"structured area detected: {_pprefix} "
                                    f"({_pc['count']} pages, rel {_pc['rel']:.2f}) "
                                    f"\u2014 full-text capture enabled")
            _in_structured_area = _pc.get("boosted", False)
            # For pages in a boosted area: re-extract with no text cap for full content
            if page_text_cap and _in_structured_area:
                _full_struct = wa._extract_page_structure(
                    raw, page_url, max_links=page_link_cap, max_text=0)
                full_text = _full_struct.get("full_text", "") or full_text

            # Ingest the page itself — stable id on the NORMALISED url so the
            # same page upserts in place instead of spawning duplicate records.
            rid = "p_" + hashlib.sha256(f"{ds_id}:{page_url}".encode()).hexdigest()[:22]
            record = {
                "id": rid,
                "text": (full_text[:max_record_chars] if max_record_chars else full_text),
                "title": title,
                "url": page_url, "hostname": hostname, "depth": depth,
                "headings": structure.get("headings", [])[:20],
                "links": [{"url": ln.get("url",""), "anchor": ln.get("anchor","")[:80]}
                          for ln in (structure.get("links", []) or [])[:100]],
                "link_count": len(structure.get("links", []) or []),
                "word_count": structure.get("word_count", 0),
                "relevance": relevance, "authority": authority, "quality": quality,
                "novelty": novelty, "usefulness": usefulness, "source_type": src_type,
                "source": "fabric_discovery",
                "tags": extra_tags + ["web", "discovery"] + llm_tags,
            }
            try:
                await ingest_fn(ds_id, [record], source="fabric_discovery",
                                tags=extra_tags + ["web", "discovery"] + llm_tags)
                pages_fetched += 1
            except Exception as e:
                log.warning("ingest page %s: %s", page_url, e)
                return

            await _emit("page_added", url=page_url, title=(title or "")[:80],
                        record_id=rid, depth=depth, dataset_id=ds_id,
                        relevance=relevance, authority=authority, quality=quality,
                        novelty=novelty, usefulness=usefulness, source_type=src_type,
                        parent_url=parent_url)
            _record_edge(crawl_id, ds_id, (parent_url or ds_id), page_url,
                         ("LINKS_TO" if parent_url else "HAS_PAGE"), "Page")

            # ── Queue 2nd-order work (tagging + entity extraction) to the
            #    background workers — these are the LLM-heavy steps, kept OFF
            #    the fetch path so crawling never stalls on the model. ────────
            if do_entities or (llm_tagging and topic):
                ck = "code" if (structure.get("code_blocks") and
                                len(structure.get("code_blocks", [])) >= 2) else "web"
                try:
                    await _pq_put((full_text, title, rid, ds_id, ck, page_url,
                                      bool(llm_tagging and topic),
                                      bool(do_entities), bool(entities_llm)))
                except Exception as e:
                    log.debug("enqueue proc %s: %s", page_url, e)
            if rep_dropoff > 0 and full_text:
                pg=set(re.sub(r"[^a-z0-9]"," ",full_text[:4000].lower()).split())
                if pg:
                    new_frac=len(pg-rep_seen_tokens)/len(pg)
                    rep_seen_tokens.update(pg)
                    if new_frac<0.08:
                        rep_consecutive+=1
                        if rep_consecutive>=rep_dropoff:
                            await _emit("repetition_dropoff",pages=pages_fetched,
                                        consecutive=rep_consecutive,
                                        message=f"stopped: <8% new content"
                                        f" for {rep_dropoff} consecutive pages")
                            _stop = True
                            return
                    else: rep_consecutive=0
            if roll_desc_on and full_text:
                roll_buf.append(((title or "") + ": " + full_text[:400]).strip())
                if len(roll_buf) >= 5:
                    _snips = roll_buf
                    roll_buf = []

                    async def _roll_bg(snips):
                        nonlocal topic_desc
                        td = await _roll_topic_description(roll_topic, topic_desc, snips)
                        if td:
                            topic_desc = td
                            config["topic_description"] = td
                            await _emit("topic_description", description=td,
                                        pages=pages_fetched, dataset_id=ds_id)
                    asyncio.create_task(_roll_bg(_snips))
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
                        asyncio.create_task(_mirror_surface_to_fabric(s, ds_id))
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
                            await _maybe_api_enumeration(s, ds_id, crawl_id,
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
                        asyncio.create_task(_mirror_subtable_to_fabric(
                            _sd, ds_id, sub.get("title",""), sub.get("kind","table")))
                        await _emit("subtable_added", url=page_url, dataset_id=_sd,
                                    parent_dataset_id=ds_id, kind=sub.get("kind", "table"),
                                    title=sub.get("title", ""), rows=n)
                        # Extract entities from the TABLE content too (rows as
                        # text), attached to the parent dataset so they join the
                        # main entity graph — queued off the fetch path.
                        if do_entities:
                            try:
                                recs = sub.get("records", [])[:200]
                                tbl_txt = ((sub.get("title", "") or "") + "\n" + "\n".join(
                                    (" | ".join(f"{k}={v}" for k, v in r.items())
                                     if isinstance(r, dict) else " | ".join(map(str, r)))
                                    for r in recs))[:20000]
                                if tbl_txt.strip():
                                    trid = "subt:" + hashlib.sha1(
                                        _sd.encode()).hexdigest()[:16]
                                    # Priority 0: structural work for 2nd/3rd order
                                    # linking. Queue against the sub-dataset so its
                                    # own records get entities, AND against the parent
                                    # so entities join the main graph.
                                    await _pq_put((
                                        tbl_txt, sub.get("title", "") or _sd, trid,
                                        _sd, "table", page_url, False, True,
                                        bool(entities_llm)), priority=0)
                                    await _pq_put((
                                        tbl_txt, sub.get("title", "") or _sd, trid,
                                        ds_id, "table", page_url, False, True,
                                        bool(entities_llm)), priority=0)
                                    await _emit("table_entities_queued",
                                                url=page_url, dataset_id=_sd,
                                                message="queued entity extraction for "
                                                        "table " + (sub.get("title", "")
                                                                    or _sd)[:60])
                            except Exception as e:
                                log.debug("subtable entities %s: %s", _sd, e)

            # ── Enqueue children — topic-relevant links first, with a boost
            #    for links pointing at rich / high-authority sources so the
            #    crawler dives deeper into good domains ("swallow"). ───────────
            scored_links = []
            seen_norm = set()
            for ln in page_links:
                lu = _normalize_url(ln.get("url", ""))
                if not lu or lu in visited or lu in seen_norm:
                    continue
                seen_norm.add(lu)
                sc = _score_topic(f"{ln.get('anchor','')} {ln.get('context','')} {lu}", tokens)
                lhost = urlparse(lu).netloc
                # Git-aware path discovery: always dive into repos / repo links so
                # code & data attached to git pages/profiles is captured.
                if (_RX_GITHUB.match(lu) or _RX_DOTGIT.match(lu)
                        or "gitlab" in lhost or "bitbucket" in lhost
                        or lu.endswith(".git")):
                    sc += 0.6
                if domain_rich.get(lhost):
                    sc += 0.5                       # actively swallowing this domain
                else:
                    lrn = _domain_auth_get(lhost, _tkey)
                    if lrn and lrn.get("score", 0) >= 0.6:
                        sc += 0.25 * lrn["score"]   # known-good source for this topic
                scored_links.append((sc, lu, lhost))
            # Boost links under detected structured-area prefixes
            for i, (sc, lu, lhost) in enumerate(scored_links):
                _lu_parts = [p for p in urlparse(lu).path.split("/") if p]
                _lu_prefix = urlparse(lu).netloc + "/" + "/".join(_lu_parts[:2])
                if path_clusters.get(_lu_prefix, {}).get("boosted"):
                    scored_links[i] = (sc + 0.4, lu, lhost)
            scored_links.sort(key=lambda x: x[0], reverse=True)
            queued_norm = {_normalize_url(q[0]) for q in queue}
            added = 0
            _enq_cap = max_links_pp if max_links_pp else 10 ** 9
            for sc, lu, lhost in scored_links:
                if added >= _enq_cap or len(queue) > max_pages * 50:
                    break
                if lu in queued_norm:
                    continue
                # rich domains may be followed beyond the normal depth limit so
                # the whole relevant area gets captured (drop-off ends this).
                cdepth = depth + 1
                if domain_rich.get(lhost) and cdepth > max_depth:
                    cdepth = min(cdepth, max_depth + swallow_extra_depth)
                queue.append((lu, cdepth, next_dry, page_url))
                added += 1

            # If this page is a GitHub/GitLab PROFILE, enumerate its full repo
            # list (the profile only shows a handful) so every repo is found.
            _ghp = re.match(r"^https?://(github|gitlab)\.com/([^/?#]+)/?$", page_url)
            if _ghp and _ghp.group(2).lower() not in _GH_RESERVED:
                host_, user_ = _ghp.group(1), _ghp.group(2)
                tabs = ([f"https://github.com/{user_}?tab=repositories"]
                        if host_ == "github" else
                        [f"https://gitlab.com/users/{user_}/projects"])
                for eu in tabs:
                    eun = _normalize_url(eu)
                    if eun not in visited and eun not in queued_norm:
                        queue.append((eun, depth + 1, next_dry, page_url))
                        await _emit("git_paths", url=page_url, domain=host_,
                                    message=f"enumerating repositories of {user_}")


        # ── Concurrent multi-domain fetch pool ─────────────────────────────
        #  max_conc pages in flight at once; per-host politeness still applies
        #  via _host_fetch_slot, so DIFFERENT domains are scanned in parallel
        #  while the same host stays rate-limited.
        _stop = False
        pending = set()
        last_save = 0
        while not _stop and (queue or pending) and pages_fetched < max_pages:
            while (queue and len(pending) < max_conc and
                   (pages_fetched + len(pending)) < max_pages):
                item = queue.pop(0)
                page_url, depth, dry = item[0], item[1], item[2]
                parent_url = item[3] if len(item) > 3 else None
                page_url = _normalize_url(page_url)
                parent_url = _normalize_url(parent_url) if parent_url else None
                _eh = urlparse(page_url).netloc
                _dlimit = max_depth + (swallow_extra_depth if domain_rich.get(_eh) else 0)
                if page_url in visited or depth > _dlimit:
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
                _task = asyncio.create_task(
                    _process(page_url, depth, dry, parent_url))
                try:
                    _task._fdsc_host = urlparse(page_url).netloc
                except Exception:
                    _task._fdsc_host = ""
                pending.add(_task)
            if not pending:
                break
            if len(pending) > 1:
                _domains = sorted({getattr(t, "_fdsc_host", "") for t in pending} - {""})
                await _emit("scanning", in_flight=len(pending), queued=len(queue),
                            domains=_domains[:8],
                            message=f"scanning {len(pending)} pages in parallel"
                                    + (f" across {len(_domains)} domains"
                                       if len(_domains) > 1 else ""))
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED)
            for d in done:
                try:
                    d.result()
                except Exception as e:
                    log.debug("crawl worker: %s", e)
            if pages_fetched - last_save >= 5:
                last_save = pages_fetched
                _save_frontier(crawl_id, ds_id, seed_url, config, queue, visited,
                               "running", pages_fetched, surfaces_found, subtables_found)
                await _emit("progress", pages=pages_fetched, queued=len(queue),
                            surfaces=surfaces_found, subtables=subtables_found,
                            promoted=promoted, concurrency=max_conc)
        if pending:
            await asyncio.wait(pending)

    # Wait for the background 2nd-order queue to finish (it ran in tandem
    # throughout the crawl — this only awaits the small tail), then stop workers.
    try:
        await proc_q.join()
    except Exception:
        pass
    for _w in _proc_workers:
        try:
            _pq_put_nowait(None, priority=0)
        except Exception:
            pass
    for _w in _proc_workers:
        _w.cancel()
    # stop the in-tandem consolidator (a final pass runs below, before synthesis)
    if _consolidator:
        _consol_stop.set()
        try:
            await _consolidator
        except Exception:
            pass
    if _brief_refiner:
        _consol_stop.set()
        try:
            await _brief_refiner
        except Exception:
            pass

    status = "done" if not queue else "paused"
    if roll_desc_on and roll_buf:
        try:
            topic_desc = await _roll_topic_description(roll_topic, topic_desc, roll_buf)
            config["topic_description"] = topic_desc
            await _emit("topic_description", description=topic_desc,
                        pages=pages_fetched, dataset_id=ds_id, final=True)
        except Exception as e:
            log.debug("rolling desc flush: %s", e)
    _save_frontier(crawl_id, ds_id, seed_url, config, queue, visited,
                   status, pages_fetched, surfaces_found, subtables_found)
    # Cross-source "soft" entity links — relationships across the whole scan,
    # not just within a single page (rendered as dotted edges in the map).
    if do_entities:
        try:
            await _build_global_entity_links(ds_id)
        except Exception as e:
            log.debug("global entity links: %s", e)
    # Mirror the discovered structure into the main fabric graph (Neo4j) so the
    # topic dataset, its sub-tables and surfaces appear in the data-fabric pane.
    try:
        await _mirror_crawl_to_fabric(crawl_id, ds_id, config.get("topic", ""))
    except Exception as e:
        log.debug("fabric mirror call: %s", e)
    await _emit("done", pages=pages_fetched, surfaces=surfaces_found,
                subtables=subtables_found, entities=entities_found, promoted=promoted,
                queue_remaining=len(queue))

    # ── Finalize in the background (in tandem): a final consolidation pass
    #    (dedup + LLM relationship tie-together) so the 2nd-order graph is clean
    #    and linked, THEN auto-synthesize the 3rd-order model on top of it. ─────
    _want_synth = (status == "done" and config.get("auto_synthesize")
                   and config.get("topic"))
    _want_consol = (do_entities and config.get("consolidate_entities", True))
    if _want_synth or _want_consol:
        async def _finalize():
            if _want_consol:
                try:
                    await _emit("consolidating", dataset_id=ds_id,
                                message="final consolidation + relationship tie-together")
                    await emit_event({"type": "fabric.entity_graph.progress",
                                      "stage": "consolidating", "dataset_id": ds_id,
                                      "message": "final consolidation + relationship "
                                                 "tie-together"})
                    await _call_cap("fabric.entity_graph.consolidate",
                                    dataset_id=ds_id, link=True)
                except Exception as e:
                    log.warning("final consolidate %s: %s", ds_id, e)
            if _want_synth:
                try:
                    await _emit("synthesizing", dataset_id=ds_id,
                                topic=config.get("topic", ""),
                                message="auto-synthesizing 3rd-order model "
                                        "(on consolidated graph)\u2026")
                    await emit_event({"type": "fabric.synthesize.progress",
                                      "topic": config.get("topic", ""), "stage": "auto",
                                      "message": "auto-synthesis starting "
                                                 "(on consolidated graph)"})
                    r = await cap_synthesize_topic(
                        topic=config.get("topic", ""), dataset_id=ds_id,
                        allow_discovery=False,
                        neighbor_depth=int(config.get("synth_neighbor_depth", 1)),
                        infer_edges=bool(config.get("synth_infer_edges", True)),
                        full_source=bool(config.get("synth_full_source", True)),
                        max_entries=int(config.get("synth_max_entries", 150)),
                        focus=config.get("synth_focus", "") or "")
                    if isinstance(r, dict) and r.get("ok"):
                        await _emit("synthesized", dataset_id=ds_id,
                                    model_id=r.get("model_id", ""),
                                    entries=r.get("entry_count", 0),
                                    relations=len(r.get("relations", [])),
                                    inferred=r.get("inferred_relations", 0),
                                    message=f"3rd-order model ready: "
                                            f"{r.get('entry_count',0)} entries, "
                                            f"{len(r.get('relations',[]))} relations "
                                            f"({r.get('inferred_relations',0)} inferred) "
                                            f"\u2014 see the Models tab")
                except Exception as e:
                    log.warning("auto-synthesize %s: %s", ds_id, e)
                    await _emit("synthesize_error", dataset_id=ds_id,
                                message=f"auto-synthesis failed: {e}")
        asyncio.create_task(_finalize())

    return {
        "ok": True, "crawl_id": crawl_id, "dataset_id": ds_id, "status": status,
        "pages_fetched": pages_fetched, "surfaces_found": surfaces_found,
        "subtables_found": subtables_found, "entities_found": entities_found,
        "surfaces_promoted": promoted, "queue_remaining": len(queue),
        "topic_description": config.get("topic_description", ""),
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
    required_keyword: str = "", required_keyword_mode: str = "fuzzy",
    detect_surfaces: bool = True, extract_subtables: bool = True,
    extract_entities: bool = True, extract_entities_llm: bool = True,
    llm_tagging: bool = True, min_relevance: float = 0.0,
    auto_promote: bool = False, auto_pull: bool = False, tags: str = "",
    max_concurrency: int = 4, entity_workers: int = 3,
    auto_synthesize: bool = False, synth_neighbor_depth: int = 1,
    synth_infer_edges: bool = True, consolidate_entities: bool = True,
    drift_guard: bool = True, llm_drift_gate: bool = False,
    swallow_domains: bool = True, unlimited_depth: bool = False,
    topic_brief: bool = False, brief_rounds: int = 3,
    crawl_id: str = "",
    page_text_cap: int = 0, page_link_cap: int = 0, max_record_chars: int = 0,
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
        "required_keyword": required_keyword.strip(),
        "required_keyword_mode": required_keyword_mode or "fuzzy",
        "detect_surfaces": detect_surfaces, "extract_subtables": extract_subtables,
        "extract_entities": extract_entities, "extract_entities_llm": extract_entities_llm,
        "llm_tagging": llm_tagging, "min_relevance": min_relevance,
        "auto_promote": auto_promote, "auto_pull": auto_pull, "tags": tags,
        "max_concurrency": max_concurrency, "entity_workers": entity_workers,
        "auto_synthesize": auto_synthesize,
        "synth_neighbor_depth": synth_neighbor_depth,
        "synth_infer_edges": synth_infer_edges,
        "consolidate_entities": consolidate_entities,
        "drift_guard": drift_guard, "llm_drift_gate": llm_drift_gate,
        "swallow_domains": swallow_domains, "unlimited_depth": unlimited_depth,
        "topic_brief": topic_brief, "brief_rounds": brief_rounds,
        "page_text_cap": page_text_cap, "page_link_cap": page_link_cap,
        "max_record_chars": max_record_chars,
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
    auto_pull: bool = False, tags: str = "",
    page_text_cap: int = 0, page_link_cap: int = 0, max_record_chars: int = 0,
    trace_id=None,
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
            "SELECT data FROM fabric_records WHERE dataset_id=? LIMIT 50000", (ds_id,)
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
        "page_text_cap": page_text_cap, "page_link_cap": page_link_cap,
        "max_record_chars": max_record_chars,
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
    "fabric.surfaces.browse",
    http_method="POST", http_path="/fabric/surfaces/browse",
    http_tags=["fabric","discover"], memory="off",
    description="Browse the FULL content of a discovered surface with pagination. "
                "Input: surface_id or url, offset (default 0), page_size (default 100), "
                "follow_next (bool default True — follow REST pagination next links). "
                "Output: {ok, kind, url, columns, rows, count, offset, next_offset, "
                "has_more, total_seen}.",
)
async def cap_surfaces_browse(surface_id:str="", url:str="", offset:int=0,
                              page_size:int=100, follow_next:bool=True,
                              trace_id=None) -> Dict:
    _ensure_tables()
    kind=""
    if surface_id:
        try:
            row=_sqlite_conn().execute(
                "SELECT url,kind FROM fabric_surfaces WHERE id=?",(surface_id,)).fetchone()
        except Exception as e: return {"error":str(e)}
        if row: url=url or row["url"]; kind=row["kind"] or ""
    if not url or not url.startswith(("http://","https://")): return {"error":"no url"}
    page_size=max(1,min(1000,int(page_size or 100))); offset=max(0,int(offset or 0))
    ct=""; cols=[]; rows=[]; total_seen=0; has_more=False
    try:
        async with httpx.AsyncClient(timeout=30,follow_redirects=True,headers=_HTTP_HEADERS) as c:
            first=await c.get(url); ct=first.headers.get("content-type","").lower()
            body=first.text or ""
            if "json" in ct or kind in ("json","json_api"):
                try: data=first.json()
                except Exception: data=None
                raw=[]; nxt_link=None
                while True:
                    if isinstance(data,list): raw.extend(data); break
                    elif isinstance(data,dict):
                        chunk=data.get("results") or data.get("items") or data.get("data")
                        if isinstance(chunk,list):
                            raw.extend(chunk)
                            nxt=data.get("next")
                            if (follow_next and isinstance(nxt,str)
                                    and nxt.startswith("http") and len(raw)<offset+page_size):
                                try: data=(await c.get(nxt)).json(); nxt_link=nxt
                                except Exception: break
                            else: break
                        else:
                            raw=[{"key":k,"value":json.dumps(v)[:200]} for k,v in data.items()]
                            break
                    else: break
                total_seen=len(raw); page=raw[offset:offset+page_size]
                has_more=total_seen>offset+page_size or bool(nxt_link)
                for rec in page:
                    flat=({k:(v if isinstance(v,(str,int,float,bool)) else json.dumps(v)[:200])
                           for k,v in list(rec.items())[:20]}
                          if isinstance(rec,dict) else {"value":str(rec)[:300]})
                    for k in flat:
                        if k not in cols: cols.append(k)
                    rows.append(flat)
            elif "csv" in ct or kind=="csv" or url.lower().endswith(".csv"):
                lines=[ln for ln in body.splitlines() if ln.strip()]
                cols=[c.strip() for c in (lines[0].split(",") if lines else [])][:30]
                all_d=[]
                for ln in lines[1:]:
                    cells=ln.split(",")
                    all_d.append({cols[i] if i<len(cols) else f"c{i}":cells[i].strip()
                                  for i in range(min(len(cells),max(len(cols),1)))})
                total_seen=len(all_d); rows=all_d[offset:offset+page_size]
                has_more=total_seen>offset+page_size
            else:
                sz=page_size*300; st=offset*300
                chunk=_clean(re.sub(r"<[^>]+"," ",body))[st:st+sz]
                cols=["text"]; rows=[{"text":chunk}] if chunk else []
                total_seen=max(1,len(body)//300); has_more=(st+sz)<len(body)
    except Exception as e: return {"error":f"fetch: {e}","url":url}
    return {"ok":True,"url":url,"kind":kind or ct.split(";")[0],
            "columns":cols,"rows":rows,"count":len(rows),
            "offset":offset,"next_offset":offset+len(rows),
            "has_more":has_more,"total_seen":total_seen}


@capability(
    "fabric.surfaces.preview",
    http_method="POST", http_path="/fabric/surfaces/preview",
    http_tags=["fabric", "discover"], memory="off",
    description="Explore a discovered surface READ-ONLY without promoting/pulling "
                "it into the fabric. Fetches the surface and returns a small sample "
                "(rows/items/endpoints/text) plus inferred columns. "
                "Input: surface_id (str) or url (str), limit (int default 20). "
                "Output: {ok, kind, url, columns, rows, count, truncated}.",
)
async def cap_surfaces_preview(surface_id: str = "", url: str = "",
                               limit: int = 20, trace_id=None) -> Dict:
    _ensure_tables()
    kind = ""
    if surface_id:
        try:
            row = _sqlite_conn().execute(
                "SELECT url, kind FROM fabric_surfaces WHERE id=?", (surface_id,)).fetchone()
        except Exception as e:
            return {"error": str(e)}
        if row:
            url = url or row["url"]; kind = row["kind"] or ""
    if not url or not url.startswith(("http://", "https://")):
        return {"error": "no fetchable url for this surface"}
    limit = max(1, min(200, int(limit or 20)))
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers=_HTTP_HEADERS) as c:
            r = await c.get(url)
    except Exception as e:
        return {"error": f"fetch failed: {e}", "url": url}
    if r.status_code >= 400:
        return {"error": f"HTTP {r.status_code}", "url": url}
    ct = r.headers.get("content-type", "").lower()
    body = r.text or ""
    columns: List[str] = []
    rows: List[Dict] = []
    truncated = False

    def _rows_from_records(recs):
        cols, out = [], []
        for rec in recs[:limit]:
            if isinstance(rec, dict):
                flat = {k: (v if isinstance(v, (str, int, float, bool)) else json.dumps(v)[:120])
                        for k, v in list(rec.items())[:12]}
            else:
                flat = {"value": str(rec)[:200]}
            for k in flat:
                if k not in cols:
                    cols.append(k)
            out.append(flat)
        return cols, out

    if "json" in ct or kind in ("json", "json_api"):
        try:
            data = r.json()
        except Exception:
            data = None
        if isinstance(data, list):
            columns, rows = _rows_from_records(data)
            truncated = len(data) > limit
        elif isinstance(data, dict):
            # paginated list, resource index, or plain object
            if isinstance(data.get("results"), list):
                columns, rows = _rows_from_records(data["results"])
                truncated = len(data["results"]) > limit
            else:
                pairs = list(data.items())
                columns = ["key", "value"]
                for k, v in pairs[:limit]:
                    rows.append({"key": str(k),
                                 "value": (v if isinstance(v, (str, int, float, bool))
                                           else json.dumps(v))[:160]})
                truncated = len(pairs) > limit
    elif "csv" in ct or kind == "csv" or url.lower().endswith(".csv"):
        lines = [ln for ln in body.splitlines() if ln.strip()][:limit + 1]
        if lines:
            columns = [c.strip() for c in lines[0].split(",")][:20]
            for ln in lines[1:]:
                cells = ln.split(",")
                rows.append({columns[i] if i < len(columns) else f"col{i}":
                             cells[i].strip() for i in range(len(cells))})
            truncated = body.count("\n") > limit
    else:
        # html/xml/text — return a trimmed text snippet as a single "row"
        columns = ["text"]
        rows = [{"text": _clean(re.sub(r"<[^>]+>", " ", body))[:1200]}]
        truncated = len(body) > 1200

    return {"ok": True, "url": url, "kind": kind or ct.split(";")[0],
            "columns": columns, "rows": rows, "count": len(rows),
            "truncated": truncated}


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


@capability(
    "fabric.discover.description",
    http_method="GET", http_path="/fabric/discover/description",
    http_tags=["fabric", "discover"], memory="off", silent=True,
    description="Get the rolling LLM topic-description for a crawl. "
                "Input: crawl_id (str!). Output: {crawl_id, topic, description}.",
)
async def cap_discover_description(crawl_id: str, trace_id=None) -> Dict:
    _ensure_tables()
    try:
        row = _sqlite_conn().execute(
            "SELECT config FROM fabric_discovery_frontier WHERE id=?",
            (crawl_id,)).fetchone()
    except Exception as e:
        return {"error": str(e)}
    if not row:
        return {"error": "crawl not found", "crawl_id": crawl_id}
    try:
        cfg = json.loads(row["config"] or "{}")
    except Exception:
        cfg = {}
    return {"crawl_id": crawl_id, "topic": cfg.get("topic", ""),
            "description": cfg.get("topic_description", "")}


@capability(
    "fabric.discover.delete_scan",
    http_method="POST", http_path="/fabric/discover/delete_scan",
    http_tags=["fabric", "discover"], memory="off",
    description="Delete a discovery scan and its artifacts (frontier, surfaces, "
                "sub-tables, edges). Optionally also delete the underlying fabric "
                "dataset (all records + entity graph) it produced. "
                "Input: crawl_id (str!), delete_dataset (bool default False — also "
                "purge the dataset's records/vectors/graph). "
                "Output: {ok, crawl_id, dataset_id, deleted:{...}, dataset_deleted}.",
)
async def cap_discover_delete_scan(crawl_id: str, delete_dataset: bool = False,
                                   trace_id=None) -> Dict:
    if not crawl_id:
        return {"error": "crawl_id required"}
    _ensure_tables()
    conn = _sqlite_conn()
    try:
        row = conn.execute(
            "SELECT dataset_id FROM fabric_discovery_frontier WHERE id=?",
            (crawl_id,)).fetchone()
    except Exception as e:
        return {"error": str(e)}
    ds_id = (row["dataset_id"] if row else "") or ""
    deleted = {}
    for table, col in (("fabric_surfaces", "crawl_id"),
                       ("fabric_subtables", "crawl_id"),
                       ("fabric_discovery_edges", "crawl_id"),
                       ("fabric_discovery_frontier", "id")):
        try:
            cur = conn.execute(f"DELETE FROM {table} WHERE {col}=?", (crawl_id,))
            deleted[table] = cur.rowcount
        except Exception as e:
            log.debug("delete %s: %s", table, e)
            deleted[table] = -1
    conn.commit()

    # Always remove the discovery graph nodes for this crawl from Neo4j so the
    # map doesn't keep showing a deleted scan (surfaces / subtable child nodes).
    graph = _get_graph()
    if graph and getattr(graph, "available", False):
        for cy, p in (
            ("MATCH (s:Surface {crawl_id:$cid}) DETACH DELETE s", {"cid": crawl_id}),
            ("MATCH (s:Subtable {crawl_id:$cid}) DETACH DELETE s", {"cid": crawl_id}),
        ):
            try:
                await graph.query(cy, p)
            except Exception as e:
                log.debug("neo4j scan purge: %s", e)

    dataset_deleted = False
    if delete_dataset and ds_id:
        # 1) records + dataset row + vectors (SQLite / Chroma / Postgres)
        try:
            from Vera.Orchestration.fabric.data_fabric import fabric_dataset_delete
            await fabric_dataset_delete(dataset_id=ds_id)
            dataset_deleted = True
        except Exception as e:
            log.warning("delete dataset %s: %s", ds_id, e)
        # 2) entity graph for this dataset (Entity / MENTIONED_IN / HAS_ENTITY …)
        try:
            await _wa().cap_entity_graph_purge(dataset_id=ds_id)
        except Exception as e:
            log.debug("entity purge %s: %s", ds_id, e)
        # 3) Neo4j Dataset + FabricRecord nodes that survive fabric_dataset_delete
        if graph and getattr(graph, "available", False):
            for cy, p in (
                ("MATCH (r:FabricRecord {dataset_id:$ds}) DETACH DELETE r", {"ds": ds_id}),
                ("MATCH (r {dataset_id:$ds}) WHERE r:Record OR r:Page DETACH DELETE r", {"ds": ds_id}),
                ("MATCH (d:Dataset {id:$ds}) DETACH DELETE d", {"ds": ds_id}),
            ):
                try:
                    await graph.query(cy, p)
                except Exception as e:
                    log.debug("neo4j dataset purge: %s", e)

    await emit_event({"type": "fabric.discover.scan_deleted",
                      "crawl_id": crawl_id, "dataset_id": ds_id,
                      "dataset_deleted": dataset_deleted})
    return {"ok": True, "crawl_id": crawl_id, "dataset_id": ds_id,
            "deleted": deleted, "dataset_deleted": dataset_deleted}


@capability(
    "fabric.discover.clear_history",
    http_method="POST", http_path="/fabric/discover/clear_history",
    http_tags=["fabric", "discover"], memory="off",
    description="Bulk-delete discovery scans. Scope by dataset_id and/or status. "
                "Input: dataset_id (str — empty = all), status (str — running|paused|"
                "done|error, empty = any), delete_data (bool default False — also "
                "purge each scan's fabric dataset). "
                "Output: {ok, scans_deleted, datasets_deleted, crawl_ids}.",
)
async def cap_discover_clear_history(dataset_id: str = "", status: str = "",
                                     delete_data: bool = False,
                                     trace_id=None) -> Dict:
    _ensure_tables()
    where, params = ["1=1"], []
    if dataset_id:
        where.append("dataset_id=?"); params.append(dataset_id)
    if status:
        where.append("status=?"); params.append(status)
    try:
        rows = _sqlite_conn().execute(
            "SELECT id FROM fabric_discovery_frontier WHERE " + " AND ".join(where),
            params).fetchall()
    except Exception as e:
        return {"error": str(e)}
    crawl_ids = [r["id"] for r in rows]
    if not crawl_ids:
        return {"ok": True, "scans_deleted": 0, "datasets_deleted": 0,
                "crawl_ids": []}
    datasets_deleted = 0
    for cid in crawl_ids:
        res = await cap_discover_delete_scan(crawl_id=cid, delete_dataset=delete_data)
        if res.get("dataset_deleted"):
            datasets_deleted += 1
    await emit_event({"type": "fabric.discover.history_cleared",
                      "count": len(crawl_ids), "delete_data": bool(delete_data)})
    return {"ok": True, "scans_deleted": len(crawl_ids),
            "datasets_deleted": datasets_deleted, "crawl_ids": crawl_ids}


def _page_labels_for(ds_id: str, urls: Set[str]) -> Dict[str, Dict]:
    """Fetch title/word_count/text and metadata for page nodes from fabric_records."""
    labels: Dict[str, Dict] = {}
    if not urls:
        return labels
    try:
        rows = _sqlite_conn().execute(
            "SELECT data FROM fabric_records WHERE dataset_id=? ORDER BY json_extract(data, '$.relevance') DESC LIMIT 8000", (ds_id,)
        ).fetchall()
    except Exception:
        return labels
    for r in rows:
        try:
            d = json.loads(r["data"]) if r["data"] else {}
        except Exception:
            continue
        u = _normalize_url(d.get("url") or "")
        if u and u in urls and u not in labels:
            labels[u] = {
                "title":       d.get("title") or "",
                "depth":       d.get("depth", 0),
                "word_count":  d.get("word_count", 0),
                "record_id":   d.get("id", ""),
                "relevance":   d.get("relevance", 0.5),
                "source_type": d.get("source_type", ""),
                "tags":        d.get("tags", []),
                "headings":    d.get("headings", [])[:20],
                # Full text stored — browsers show it in the node content reader
                "text":        (d.get("text") or ""),
                # Outbound links stored in the record for the graph reader panel
                "links":       d.get("links", [])[:100],
                "link_count":  d.get("link_count", 0),
            }
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
                                            "relevance": (si.get("topic_score") if si.get("topic_score") is not None else 0.5),
                                            "promoted": si.get("promoted"),
                                            "can_promote": _can}}
                elif nid in sub_index:
                    bi = sub_index[nid]
                    nodes[nid] = {"id": nid, "label": (bi.get("title") or bi.get("kind") or "table")[:42],
                                  "type": "Subtable",
                                  "props": {"id": nid, "kind": bi.get("kind"),
                                            "rows": bi.get("row_count"), "subtable": True,
                                            "relevance": 0.8, "dataset_id": nid}}
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
            nodes[u]["props"].update({
                "title":       meta.get("title", ""),
                "dataset_id":  ds_id,
                "record_id":   meta.get("record_id", ""),
                "depth":       meta.get("depth", 0),
                "word_count":  meta.get("word_count", 0),
                "relevance":   meta.get("relevance", 0.5),
                "source_type": meta.get("source_type", ""),
                "tags":        meta.get("tags", []),
                "headings":    meta.get("headings", []),
                "text":        meta.get("text", ""),
                "links":       meta.get("links", []),
                "link_count":  meta.get("link_count", 0),
            })

    # Fallback: no edges recorded (older crawl) — attach known pages to the dataset
    if not edges and ds_id:
        try:
            rows = conn.execute(
                "SELECT data FROM fabric_records WHERE dataset_id=? ORDER BY json_extract(data, '$.relevance') DESC LIMIT ?",
                (ds_id, max_nodes)).fetchall()
            for r in rows:
                try:
                    d = json.loads(r["data"]) if r["data"] else {}
                except Exception:
                    continue
                u = _normalize_url(d.get("url") or "")
                if not u.startswith("http") or u in nodes:
                    continue
                nodes[u] = {"id": u, "label": (d.get("title") or _short(u))[:42],
                            "type": "Page",
                            "props": {"url": u, "title": d.get("title", ""),
                                      "dataset_id": ds_id, "record_id": d.get("id", ""),
                                      "relevance": d.get("relevance", 0.5),
                                      "word_count": d.get("word_count", 0),
                                      "source_type": d.get("source_type", ""),
                                      "tags": d.get("tags", []),
                                      "headings": d.get("headings", [])[:20],
                                      "text": (d.get("text") or "")}}
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
                "WHERE m.dataset_id = ? ORDER BY e.mention_count DESC LIMIT ?", (ds_id, max_nodes * 4)
            ).fetchall()
        except Exception as ex:
            erow = []
            log.debug("graph entities: %s", ex)
        ent_seen: Set[str] = set()
        for m in erow:
            eid, name, etype = m["id"], m["name"], m["type"]
            if eid not in nodes:
                # relevance from mention frequency (saturating)
                mc = m["mention_count"] or 1
                rel = round(min(1.0, 0.3 + 0.12 * mc), 3)
                nodes[eid] = {"id": eid, "label": (name or etype)[:42], "type": "Entity",
                              "props": {"id": eid, "name": name, "type": etype,
                                        "subtype": etype, "mention_count": mc,
                                        "relevance": rel}}
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

async def _roll_topic_description(topic: str, prev: str, snippets: list) -> str:
    """Maintain a concise, evolving LLM description of the topic as the crawl
    discovers more. Given the current description + new excerpts, returns an
    updated 2-4 sentence summary. Degrades to the previous description on
    failure."""
    joined = "\n".join((s or "")[:400] for s in snippets[:8])[:4000]
    sys = ("You maintain a concise, evolving description of a topic as a web "
           "crawl discovers more about it. Given the CURRENT description and NEW "
           "excerpts, return an updated 2-4 sentence description capturing what "
           "the topic is about and the most important findings so far. Be "
           "factual and concise. Return ONLY the description text, no preamble.")
    prompt = (f"TOPIC: {topic}\n\nCURRENT DESCRIPTION:\n{prev or '(none yet)'}"
              f"\n\nNEW EXCERPTS:\n{joined}\n\nUPDATED DESCRIPTION:")
    try:
        out = await _llm_generate(prompt, sys, timeout=30.0)
    except Exception as e:
        log.debug("roll desc: %s", e)
        return prev
    out = (out or "").strip()
    return out[:1200] if out else prev


async def _llm_generate(prompt: str, system: str = "", timeout: float = 30.0) -> str:
    """Call the local Ollama cluster via llm.generate. Returns '' on any failure
    so callers degrade gracefully to non-LLM behaviour."""
    cap = CAPABILITY_REGISTRY.get("llm.generate")
    if not cap:
        return ""
    fn = cap.get("func") or cap.get("raw")   # func = cluster routing/failover
    if not fn:
        return ""
    try:
        r = await asyncio.wait_for(
            fn(prompt=prompt, system=system, trace_id=None), timeout=timeout)
        if isinstance(r, dict):
            return (r.get("text") or r.get("response") or "").strip()
        return str(r or "").strip()
    except Exception as e:
        log.debug("llm.generate: %s", e)
        return ""


def _parse_lines(text: str, limit: int) -> List[str]:
    out = []
    for ln in (text or "").splitlines():
        ln = re.sub(r"^[\s\-\*\d\.\)\]]+", "", ln).strip().strip('"').strip("'")
        if 2 < len(ln) < 120:
            out.append(ln)
        if len(out) >= limit:
            break
    return out


async def _llm_expand_queries(topic: str, n: int) -> List[str]:
    """Ask the LLM for high-signal search queries / subtopics for the topic."""
    if n <= 0:
        return []
    sys = ("You generate concise web-search queries. Output ONLY the queries, "
           "one per line, no numbering, no commentary.")
    prompt = (f"Topic: {topic}\n\nGive {n} diverse, specific web-search queries that "
              f"together cover the most relevant facets, key entities, and "
              f"authoritative sources for this topic. One query per line.")
    txt = await _llm_generate(prompt, sys, timeout=25.0)
    qs = _parse_lines(txt, n)
    return qs


async def _llm_tag(text: str, title: str, topic: str) -> Dict:
    """Ask the LLM to rate relevance (0-1) and tag a page. Returns
    {relevance: float|None, tags: [str]}. Degrades to {} on failure."""
    snippet = (title + "\n\n" + (text or "")[:1500]).strip()
    if not snippet:
        return {}
    sys = ('You are a precise content classifier. Respond ONLY with compact JSON: '
           '{"relevance": <0.0-1.0>, "tags": ["..."]}. relevance = how on-topic the '
           'page is for the given topic.')
    prompt = f"Topic: {topic}\n\nPage:\n{snippet}\n\nJSON:"
    txt = await _llm_generate(prompt, sys, timeout=25.0)
    if not txt:
        return {}
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(0))
    except Exception:
        return {}
    out = {}
    if isinstance(d.get("relevance"), (int, float)):
        out["relevance"] = max(0.0, min(1.0, float(d["relevance"])))
    if isinstance(d.get("tags"), list):
        out["tags"] = [str(t)[:40] for t in d["tags"][:8] if str(t).strip()]
    return out


_LLM_ENT_TYPES = {"person", "organisation", "org", "account", "identity", "email",
                  "technology", "product", "location", "place", "event", "concept",
                  "function", "class", "module", "domain"}

async def _llm_entities(text: str, content_type: str = "web") -> List[Dict]:
    """LLM-assisted entity extraction for higher recall/precision on
    people, accounts/identities, orgs, products, places. Returns the same shape
    as extract_entities2. Degrades to [] on failure."""
    snippet = (text or "")[:2000].strip()
    if not snippet:
        return []
    sys = ('Extract named entities. Respond ONLY with JSON array of '
           '{"name":"...","type":"person|account|organisation|product|location|'
           'event|technology|function|class"}. Be precise: NO stopwords, no generic '
           'words, only real named entities, identities, accounts, or code symbols.')
    txt = await _llm_generate(f"Text:\n{snippet}\n\nJSON:", sys, timeout=30.0)
    m = re.search(r"\[.*\]", txt, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for e in arr if isinstance(arr, list) else []:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name", "")).strip()
        etype = str(e.get("type", "")).strip().lower()
        if etype == "org":
            etype = "organisation"
        if etype == "identity":
            etype = "account"
        if etype == "place":
            etype = "location"
        if (2 <= len(name) <= 60 and etype in _LLM_ENT_TYPES
                and name.lower() not in _ENT_STOP):
            out.append({"name": name, "type": etype, "normalised": name.lower(),
                        "mention_count": 2})
    return out[:40]


# Map the richer vocabulary the LLM may use onto the entity subtypes the graph
# already knows how to group/colour, so relations stay connectable and nothing
# falls through to a bare grey node.
_STRUCT_TYPE_MAP = {
    "org": "organisation", "organization": "organisation", "company": "organisation",
    "institution": "organisation", "agency": "organisation", "team": "organisation",
    "group": "organisation", "consortium": "organisation",
    "place": "location", "city": "location", "country": "location",
    "region": "location", "venue": "location",
    "identity": "account", "handle": "account", "username": "account", "alias": "account",
    "software": "technology", "library": "technology", "framework": "technology",
    "tool": "technology", "platform": "technology", "language": "technology",
    "programming language": "technology", "system": "technology",
    "protocol": "concept", "standard": "concept", "specification": "concept",
    "spec": "concept", "format": "concept", "method": "concept", "algorithm": "concept",
    "methodology": "concept", "paradigm": "concept", "metric": "concept",
    "theory": "concept", "field": "concept", "discipline": "concept", "model": "concept",
    "technique": "concept", "approach": "concept", "law": "concept",
    "work": "named_entity", "publication": "named_entity", "paper": "named_entity",
    "book": "named_entity", "article": "named_entity", "dataset": "named_entity",
    "title": "role", "position": "role", "job": "role",
    "service": "product", "device": "product", "app": "product",
    "application": "product", "hardware": "product",
    "datetime": "date", "timestamp": "time",
}
_STRUCT_OK = {"person", "organisation", "product", "technology", "concept",
              "location", "event", "date", "year", "time", "role", "account",
              "email", "domain", "named_entity", "function", "class", "module"}


def _norm_struct_type(t: str) -> str:
    t = (t or "").strip().lower()
    t = _STRUCT_TYPE_MAP.get(t, t)
    return t if t in _STRUCT_OK else "named_entity"


async def _llm_structure(text: str, content_type: str = "web") -> Dict:
    """Read a body of text and return the STRUCTURE of its topic: the key
    entities (correctly typed) and the relations between them. This is what
    turns "entity found here" into a mapped-out picture of the subject.

    Returns {"entities":[{name,type,normalised,mention_count,salience}],
             "relations":[{from_name,from_type,to_name,to_type,rel,distance}]}.
    Relation endpoints are guaranteed to reference returned entities. Degrades
    to empty lists on any failure so callers fall back to the regex engine.
    """
    snippet = (text or "")[:5000].strip()
    if not snippet:
        return {"entities": [], "relations": []}
    sys = (
        "You are an information-extraction engine. Read the text and capture the "
        "STRUCTURE of its topic: the key entities and how they relate to one "
        "another. Respond with ONLY a JSON object of the form "
        '{"entities":[{"name":str,"type":str,"salience":number}],'
        '"relations":[{"from":str,"to":str,"rel":str}]}. '
        "Allowed entity types: person, organisation, product, technology, "
        "concept, standard, protocol, location, event, date, role, work. "
        "RULES: (1) Use 'person' ONLY for an individual human being — products, "
        "standards, protocols, software, libraries, languages, companies and "
        "concepts are NOT persons. (2) Prefer fewer, high-value entities: the "
        "ones the topic is actually about, not every capitalised phrase. "
        "(3) salience is 0..1, how central the entity is to the topic. "
        "(4) Every 'from' and 'to' MUST be a name present in your entities list. "
        "(5) Use SPECIFIC UPPER_SNAKE relation verbs that describe the actual "
        "relationship: CREATED_BY, DEVELOPED_BY, PART_OF, USES, BASED_ON, "
        "SUCCEEDS, REPLACES, COMPETES_WITH, WORKS_FOR, FOUNDED, MEMBER_OF, "
        "LOCATED_IN, OCCURRED_IN, PUBLISHED_BY, INFLUENCED, OWNS, LEADS, "
        "REPORTS_TO, ACQUIRED_BY, ALLIED_WITH, RIVAL_OF, IMPLEMENTS, EXTENDS, "
        "DEFEATS, PRODUCED_BY, APPEARED_IN, MARRIED_TO, PARENT_OF, CHILD_OF. "
        "(6) NEVER use RELATED_TO or CO_OCCURS — if you cannot find a specific "
        "verb, omit the relation entirely. Quality over quantity."
    )
    txt = await _llm_generate(f"Text:\n{snippet}\n\nJSON:", sys, timeout=45.0)
    if not txt:
        return {"entities": [], "relations": []}
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return {"entities": [], "relations": []}
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return {"entities": [], "relations": []}
    if not isinstance(obj, dict):
        return {"entities": [], "relations": []}

    ents: List[Dict] = []
    seen = set()
    for e in (obj.get("entities") or []):
        if not isinstance(e, dict):
            continue
        name = str(e.get("name", "")).strip().strip(".,:;\"'()[]{}")
        if not (2 <= len(name) <= 80):
            continue
        nrm = name.lower()
        if nrm in seen or nrm in _ENT_STOP:
            continue
        seen.add(nrm)
        try:
            sal = float(e.get("salience", 0.6))
        except Exception:
            sal = 0.6
        sal = max(0.0, min(1.0, sal))
        ents.append({"name": name, "type": _norm_struct_type(e.get("type")),
                     "normalised": nrm, "mention_count": 2, "salience": sal})
    by_norm = {e["normalised"]: e for e in ents}

    rels: List[Dict] = []
    rseen = set()
    for r in (obj.get("relations") or []):
        if not isinstance(r, dict):
            continue
        fn = str(r.get("from", "")).strip().lower()
        tn = str(r.get("to", "")).strip().lower()
        a = by_norm.get(fn)
        b = by_norm.get(tn)
        if not a or not b or a is b:
            continue
        rel = re.sub(r"[^A-Z0-9_]", "_",
                     str(r.get("rel", "MENTIONED_WITH")).strip().upper().replace(" ", "_"))
        rel = rel.strip("_")[:32] or "MENTIONED_WITH"
        # demote generic fallback verbs — LLM was told not to use them; if it
        # did anyway it means the relationship is unspecified, mark it weak
        if rel in ("RELATED_TO", "RELATED", "CO_OCCURS", "ASSOCIATED_WITH",
                   "CONNECTED_TO", "LINKED_TO"):
            rel = "MENTIONED_WITH"
        key = (a["normalised"], b["normalised"], rel)
        if key in rseen:
            continue
        rseen.add(key)
        rels.append({"from_name": a["name"], "from_type": a["type"],
                     "to_name": b["name"], "to_type": b["type"],
                     "rel": rel, "distance": 0})
    return {"entities": ents[:60], "relations": rels[:80]}


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
        import Vera.Orchestration.research.researcher_api as ra
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


_SITE_GROUPS = {
    "reddit":  "site:reddit.com",
    "x":       "(site:x.com OR site:twitter.com OR site:nitter.net)",
    "youtube": "site:youtube.com",
    "news":    ("(site:reuters.com OR site:apnews.com OR site:bbc.com OR "
                "site:theguardian.com OR site:nytimes.com OR site:bloomberg.com "
                "OR site:arstechnica.com OR site:techcrunch.com)"),
    "github":  "(site:github.com OR site:gitlab.com)",
    "stackoverflow": "(site:stackoverflow.com OR site:stackexchange.com)",
    "hackernews":    "site:news.ycombinator.com",
    "blogs":   "(site:medium.com OR site:substack.com OR site:dev.to)",
    "wikipedia":     "site:wikipedia.org",
    "academic": ("(site:arxiv.org OR site:scholar.google.com OR "
                 "site:semanticscholar.org OR site:researchgate.net OR "
                 "site:ssrn.com)"),
    "forums":  "(site:quora.com OR site:discourse.org)",
    "mastodon": "(site:mastodon.social OR site:fosstodon.org OR site:hachyderm.io)",
    "linkedin": "site:linkedin.com",
    "tiktok":  "site:tiktok.com",
    "podcasts": "(site:podcasts.apple.com OR site:open.spotify.com)",
    "docs":    "(inurl:docs OR inurl:documentation)",
}
# Every group except the very noisy ones — used by the "all" sentinel.
_SITE_GROUPS_ALL = [k for k in _SITE_GROUPS if k not in ("linkedin", "tiktok")]


def _site_query_angles(topic: str, groups: str) -> List[str]:
    """Site-targeted search queries (reddit / x / youtube / news) so a topic
    also pulls discussion, video and press coverage, not just documentation."""
    out, seen = [], set()
    raw = (groups or "").lower().replace(" ", "").split(",")
    expanded = []
    for g in raw:
        if g.strip() in ("all", "*"):
            expanded.extend(_SITE_GROUPS_ALL)
        elif g.strip():
            expanded.append(g.strip())
    for g in expanded:
        flt = _SITE_GROUPS.get(g)
        if flt and g not in seen:
            seen.add(g)
            out.append(f"{topic} {flt}")
    return out


def _vera_source_seeds(topic: str, n: int) -> List[Dict]:
    """Seed from URLs Vera already holds whose content matches the topic — reuse
    what the system already knows instead of only searching the open web."""
    try:
        kw = "%" + (topic or "").strip().lower() + "%"
        rows = _sqlite_conn().execute(
            "SELECT data FROM fabric_records "
            "WHERE lower(text) LIKE ? AND data LIKE '%http%' LIMIT 400",
            (kw,)).fetchall()
    except Exception as e:
        log.debug("vera source seeds: %s", e)
        return []
    seen, out = set(), []
    for r in rows:
        try:
            d = json.loads(r["data"]) if r["data"] else {}
        except Exception:
            continue
        u = _normalize_url(str(d.get("url") or ""))
        if u.startswith("http") and u not in seen:
            seen.add(u)
            label = (d.get("title") or u.split("//")[-1].split("/")[0])
            out.append({"url": u, "label": label[:60], "type": "vera"})
        if len(out) >= n:
            break
    return out


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
    sites: str = "reddit,x,youtube,news", include_vera_sources: bool = True,
    expansion_rounds: int = 2, searches_per_round: int = 4,
    results_per_search: int = 8, dataset_id: str = "",
    max_pages: int = 120, max_depth: int = 4, topic_dropoff: int = 3,
    same_domain: bool = False, detect_surfaces: bool = True,
    extract_subtables: bool = True, extract_entities: bool = True,
    negative_words: str = "", negative_urls: str = "",
    required_keyword: str = "", required_keyword_mode: str = "fuzzy",
    auto_promote: bool = False, auto_pull: bool = False,
    llm_search: bool = True, llm_tagging: bool = True, min_relevance: float = 0.06,
    extract_entities_llm: bool = True,
    no_limit: bool = False, repetition_dropoff: int = 5,
    rolling_description: bool = True,
    max_concurrency: int = 4, entity_workers: int = 3,
    auto_synthesize: bool = False, synth_neighbor_depth: int = 1,
    synth_infer_edges: bool = True, consolidate_entities: bool = True,
    drift_guard: bool = True, llm_drift_gate: bool = False,
    swallow_domains: bool = True, unlimited_depth: bool = False,
    topic_brief: bool = False, brief_rounds: int = 3,
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
            await emit_event({"type": "fabric.discover.progress",
                              "acquisition_id": crawl_id, "stage": stage,
                              "engine": "discovery", **kw})
        except Exception:
            pass

    ds_id = (re.sub(r"[^a-zA-Z0-9_.]", "_", dataset_id.strip())[:80]
             if dataset_id.strip()
             else "topic." + re.sub(r"[^a-z0-9]+", "_", topic.lower()).strip("_")[:40])

    # ── 1) Seed: explicit URLs win, else multi-angle web search + feed probe ─
    explicit = [_normalize_url(u) for u in seed_urls.split(",")
                if u.strip().startswith(("http://", "https://"))]
    queries: List[str] = []
    discovered: List[Dict] = []
    if explicit:
        seeds = explicit
    else:
        queries = _topic_query_angles(topic, search_angles)
        # LLM-enhanced query expansion (subtopics / key entities / sources)
        if llm_search:
            try:
                extra = await _llm_expand_queries(topic, max(3, search_angles))
                for q in extra:
                    if q.lower() not in {x.lower() for x in queries}:
                        queries.append(q)
            except Exception as e:
                log.debug("llm query expand: %s", e)
        await _emit("seeding", message=f"searching {len(queries)} angles for '{topic}' across sites: {sites}",
                    queries=queries, sites=sites)
        discovered = await _search_many(queries, results_per_search)
        # plus feed/source discovery via the existing collector
        try:
            discovered += await _find_seed_urls(topic, max_sources, content_type)
        except Exception as e:
            log.debug("feed seed: %s", e)
        # site-targeted angles (reddit / x / youtube / news) — discussion, video, press
        site_qs = _site_query_angles(topic, sites)
        if site_qs:
            queries += site_qs
            try:
                discovered += await _search_many(site_qs, results_per_search)
            except Exception as e:
                log.debug("site search: %s", e)
        # reuse URLs Vera already knows about for this topic
        if include_vera_sources:
            try:
                discovered += _vera_source_seeds(topic, max_sources)
            except Exception as e:
                log.debug("vera seeds: %s", e)
        seen, seeds = set(), []
        for d in discovered:
            u = _normalize_url(d["url"])
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
        "extract_entities_llm": extract_entities_llm,
        "no_limit": no_limit, "repetition_dropoff": repetition_dropoff,
        "rolling_description": rolling_description,
        "max_concurrency": max_concurrency, "entity_workers": entity_workers,
        "auto_synthesize": auto_synthesize,
        "synth_neighbor_depth": synth_neighbor_depth,
        "synth_infer_edges": synth_infer_edges,
        "consolidate_entities": consolidate_entities,
        "drift_guard": drift_guard, "llm_drift_gate": llm_drift_gate,
        "swallow_domains": swallow_domains, "unlimited_depth": unlimited_depth,
        "topic_brief": topic_brief, "brief_rounds": brief_rounds,
        "llm_tagging": llm_tagging, "min_relevance": min_relevance,
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


@capability(
    "fabric.discover.map_topic",
    http_method="POST", http_path="/fabric/discover/map_topic",
    http_tags=["fabric", "discover"], memory="on",
    description="Map an ENTIRE topic comprehensively in one call. Seeds from a "
                "wide multi-angle web search PLUS targeted searches across many "
                "site types (reddit, X, youtube, news, github, stackoverflow, "
                "hackernews, blogs, wikipedia, academic, forums, podcasts...), then "
                "deep-crawls with relevance + repetition drop-off, extracts the "
                "entity graph (NER + optional LLM), and keeps a rolling LLM "
                "description of the topic. "
                "Input: topic (str!), depth (quick|standard|deep|exhaustive default "
                "standard), dataset_id (str), sites (str — override; default 'all'), "
                "use_llm_entities (bool default True), llm_steering (bool default "
                "False), focus (str — bias extraction). "
                "Output: same shape as discover.topic plus the topic_description.",
)
async def cap_discover_map_topic(
    topic: str = "", depth: str = "standard", dataset_id: str = "",
    sites: str = "all", use_llm_entities: bool = True,
    llm_steering: bool = False, focus: str = "", crawl_id: str = "",
    max_concurrency: int = 6, entity_workers: int = 3,
    auto_synthesize: bool = False, synth_neighbor_depth: int = 1,
    synth_infer_edges: bool = True, consolidate_entities: bool = True,
    drift_guard: bool = True, llm_drift_gate: bool = False,
    swallow_domains: bool = True, unlimited_depth: bool = False,
    topic_brief: bool = False, brief_rounds: int = 3,
    trace_id=None,
) -> Dict:
    if not topic or not topic.strip():
        return {"error": "topic required"}
    presets = {
        "quick": dict(max_pages=40, max_depth=3, search_angles=4,
                      expansion_rounds=1, searches_per_round=3,
                      results_per_search=6, repetition_dropoff=4, no_limit=False),
        "standard": dict(max_pages=150, max_depth=4, search_angles=6,
                         expansion_rounds=2, searches_per_round=4,
                         results_per_search=8, repetition_dropoff=5, no_limit=False),
        "deep": dict(max_pages=400, max_depth=5, search_angles=8,
                     expansion_rounds=3, searches_per_round=5,
                     results_per_search=10, repetition_dropoff=6, no_limit=False),
        "exhaustive": dict(max_pages=999999, max_depth=6, search_angles=10,
                           expansion_rounds=4, searches_per_round=6,
                           results_per_search=12, repetition_dropoff=8,
                           no_limit=True),
    }
    cfg = presets.get((depth or "standard").lower(), presets["standard"])
    try:
        await emit_event({"type": "fabric.discover.progress", "stage": "map_start",
                          "engine": "discovery",
                          "message": f"Mapping topic '{topic.strip()}' "
                                     f"(depth={depth}, sites={sites})"})
    except Exception:
        pass
    return await cap_discover_topic(
        topic=topic, dataset_id=dataset_id, sites=sites,
        content_type="all", same_domain=False,
        detect_surfaces=True, extract_subtables=True,
        extract_entities=True, extract_entities_llm=bool(use_llm_entities),
        llm_search=True, llm_tagging=True, rolling_description=True,
        loom=True, loom_cross=True, crawl_id=crawl_id,
        max_concurrency=max_concurrency, entity_workers=entity_workers,
        auto_synthesize=auto_synthesize, synth_neighbor_depth=synth_neighbor_depth,
        synth_infer_edges=synth_infer_edges, consolidate_entities=consolidate_entities,
        drift_guard=drift_guard, llm_drift_gate=llm_drift_gate,
        swallow_domains=swallow_domains, unlimited_depth=unlimited_depth,
        topic_brief=topic_brief, brief_rounds=brief_rounds,
        **cfg, trace_id=trace_id,
    )



# ═══════════════════════════════════════════════════════════════════════════
# 3rd-ORDER SYNTHESIS — an LLM builds a coherent, distilled picture of a topic
# on top of the records (1st order) and the entity graph (2nd order), and may
# trigger additional discovery to fill gaps.
# ═══════════════════════════════════════════════════════════════════════════

async def _gen_brief(topic: str, samples: str = "", prev: Dict = None) -> Dict:
    """LLM 'research brief' that guides a focused crawl. Iteratively refined as
    results arrive. Returns {type, summary, properties[], must_terms[],
    avoid_terms[]} — must_terms boost relevance, avoid_terms flag sibling drift."""
    sys = ("You build a concise research brief that guides a focused web crawl, "
           "and you sharply distinguish the target from SIMILAR-but-different "
           "subjects. Output strict JSON only.")
    pr = "TOPIC: " + topic + "\n"
    if prev:
        pr += ("CURRENT BRIEF: " + json.dumps(prev)[:1500]
               + "\nRefine it using what the crawl has found so far.\n")
    if samples:
        pr += "SAMPLES FROM RESULTS SO FAR:\n" + samples[:4000] + "\n"
    pr += ('\nReturn ONLY {"type":"what kind of thing the topic is",'
           '"summary":"1-2 sentence brief","properties":["key attributes worth '
           'capturing"],"must_terms":["distinctive terms that strongly indicate a '
           'page IS about this exact topic"],"avoid_terms":["terms that indicate a '
           'DIFFERENT but easily-confused subject (e.g. a sibling game/edition) to '
           'steer away from"]}.')
    d = _sjson(await _llm_generate(pr, sys, timeout=90)) or {}
    if not isinstance(d, dict):
        return {}
    for k in ("properties", "must_terms", "avoid_terms"):
        v = d.get(k)
        d[k] = [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []
    d["type"] = str(d.get("type", ""))[:80]
    d["summary"] = str(d.get("summary", ""))[:400]
    return d


def _sjson(raw: str):
    """Best-effort JSON extraction from an LLM response (handles code fences,
    leading prose, trailing commas)."""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s).strip()
    for op, cl in (("{", "}"), ("[", "]")):
        i = s.find(op)
        if i < 0:
            continue
        depth = 0
        for j in range(i, len(s)):
            if s[j] == op:
                depth += 1
            elif s[j] == cl:
                depth -= 1
                if depth == 0:
                    frag = s[i:j + 1]
                    try:
                        return json.loads(frag)
                    except Exception:
                        try:
                            return json.loads(re.sub(r",\s*([}\]])", r"\1", frag))
                        except Exception:
                            break
        break
    try:
        return json.loads(s)
    except Exception:
        return None


async def _call_cap(name: str, **kw):
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap:
        return None
    fn = cap.get("raw") or cap.get("func")
    if not fn:
        return None
    kw.setdefault("trace_id", None)
    try:
        return await fn(**kw)
    except Exception as e:
        log.debug("call %s: %s", name, e)
        return None


def _nrm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _patch_record_tags(ds_id: str, rid: str, add_tags, relevance=None):
    """Patch a record's tags / relevance in place (no re-embed) — used by the
    background tagger so LLM tags persist without blocking the crawl."""
    try:
        conn = _sqlite_conn()
        row = conn.execute(
            "SELECT data, tags FROM fabric_records WHERE id=? AND dataset_id=?",
            (rid, ds_id)).fetchone()
        if not row:
            return
        try:
            d = json.loads(row["data"]) if row["data"] else {}
        except Exception:
            d = {}
        try:
            tags = set(json.loads(row["tags"]) if row["tags"] else [])
        except Exception:
            tags = set(d.get("tags", []) or [])
        if add_tags:
            tags.update(add_tags)
            d["tags"] = sorted(tags)
        if relevance is not None:
            try:
                d["relevance"] = round(
                    (float(d.get("relevance", 0)) + float(relevance)) / 2.0, 3)
            except Exception:
                pass
        conn.execute("UPDATE fabric_records SET data=?, tags=? WHERE id=? AND dataset_id=?",
                     (json.dumps(d), json.dumps(sorted(tags)), rid, ds_id))
        conn.commit()
    except Exception as e:
        log.debug("patch record tags %s: %s", rid, e)


def _synth_records(ds_id: str, limit: int = 400):
    """Load record snippets (title/url/text) for grounding."""
    out = []
    try:
        rows = _sqlite_conn().execute(
            "SELECT data FROM fabric_records WHERE dataset_id=? LIMIT ?",
            (ds_id, limit)).fetchall()
    except Exception:
        return out
    for r in rows:
        try:
            d = json.loads(r["data"]) if r["data"] else {}
        except Exception:
            continue
        out.append({"id": d.get("id", ""), "title": d.get("title", ""),
                    "url": d.get("url", ""), "text": (d.get("text") or "")})
    return out


@capability(
    "fabric.discover.subtopic",
    http_method="POST", http_path="/fabric/discover/subtopic",
    http_tags=["fabric", "discover"], memory="on",
    description="Launch a focused discovery crawl about a SINGLE entity/sub-topic "
                "(e.g. an entity extracted from a previous run) and link it back to "
                "the parent dataset in the graph. "
                "Input: entity (str!), parent_dataset_id (str — link source), depth "
                "(quick|standard|deep|exhaustive), dataset_id (str — target; default "
                "derived), sites (str default 'all'), focus (str — extra angle), "
                "crawl_id (str — passthrough for polling). Output: discover.map_topic result.",
)
async def cap_discover_subtopic(
    entity: str = "", parent_dataset_id: str = "", depth: str = "standard",
    dataset_id: str = "", sites: str = "all", focus: str = "",
    crawl_id: str = "", trace_id=None,
) -> Dict:
    if not entity or not entity.strip():
        return {"error": "entity required"}
    entity = entity.strip()
    topic = entity if not focus else f"{entity} {focus}"
    res = await cap_discover_map_topic(
        topic=topic, depth=depth, dataset_id=dataset_id, sites=sites,
        use_llm_entities=True, llm_steering=False, focus=focus,
        crawl_id=crawl_id, trace_id=trace_id)
    # Link the new dataset back to the parent in the graph.
    new_ds = (res or {}).get("dataset_id", "")
    if parent_dataset_id and new_ds:
        g = _get_graph()
        if g and getattr(g, "available", False):
            try:
                await g.upsert_node("Dataset", parent_dataset_id, {"id": parent_dataset_id})
                await g.upsert_node("Dataset", new_ds,
                                    {"id": new_ds, "subtopic_of": parent_dataset_id,
                                     "entity": entity})
                await g.query(
                    "MATCH (p:Dataset {id:$p}),(c:Dataset {id:$c}) "
                    "MERGE (p)-[:SUBTOPIC {entity:$e}]->(c)",
                    {"p": parent_dataset_id, "c": new_ds, "e": entity})
            except Exception as e:
                log.debug("subtopic link: %s", e)
    if isinstance(res, dict):
        res["subtopic_of"] = parent_dataset_id
        res["entity"] = entity
    return res


def _resolve_topic_datasets(topic: str, dataset_id: str) -> List[str]:
    if dataset_id:
        return [dataset_id]
    out = []
    try:
        rows = _sqlite_conn().execute(
            "SELECT dataset_id, config FROM fabric_discovery_frontier "
            "ORDER BY rowid DESC LIMIT 60").fetchall()
        tnorm = _nrm(topic)
        for r in rows:
            ds = r["dataset_id"] or ""
            cfg = r["config"] or ""
            if ds and (tnorm in _nrm(ds) or tnorm in _nrm(cfg)):
                if ds not in out:
                    out.append(ds)
    except Exception:
        pass
    return out


@capability(
    "fabric.synthesize.topic",
    http_method="POST", http_path="/fabric/synthesize/topic",
    http_tags=["fabric", "synthesize", "graph"], memory="on",
    streams=["fabric.synthesize.progress"],
    description="Build a 3rd-ORDER, coherent picture of a topic: an LLM plans the "
                "structure the topic needs, checks coverage against the existing "
                "records + entity graph, OPTIONALLY triggers additional discovery to "
                "fill gaps, then distils multiple sources into one structured model "
                "(entries with attributes/facts/relations) persisted as a Concept "
                "layer. E.g. given 'Sinnoh Pokedex' it works out it needs every "
                "species entry, finds/maps the gaps, and distils them into one "
                "representation. "
                "Input: topic (str!), dataset_id (str — source; auto-resolved from "
                "prior crawls if omitted), allow_discovery (bool default True), "
                "discovery_depth (quick|standard|deep default standard), "
                "max_discovery_rounds (int default 2), max_entries (int default 150), "
                "sites (str default 'all'), focus (str), persist (bool default True). "
                "Output: {ok, model_id, topic, summary, entries, entry_count, "
                "relations, gaps, discovery_runs}.",
)
async def cap_synthesize_topic(
    topic: str = "", dataset_id: str = "", allow_discovery: bool = True,
    discovery_depth: str = "standard", max_discovery_rounds: int = 2,
    max_entries: int = 150, sites: str = "all", focus: str = "",
    neighbor_depth: int = 1, full_source: bool = True, infer_edges: bool = True,
    max_source_chars: int = 4000,
    persist: bool = True, trace_id=None,
) -> Dict:
    if not topic or not topic.strip():
        return {"error": "topic required"}
    topic = topic.strip()

    async def _emit(stage, **kw):
        try:
            await emit_event({"type": "fabric.synthesize.progress",
                              "topic": topic, "stage": stage, **kw})
        except Exception:
            pass

    datasets = _resolve_topic_datasets(topic, dataset_id)
    ds_primary = datasets[0] if datasets else (
        re.sub(r"[^a-zA-Z0-9_.]", "_", topic.lower())[:60] or "topic")
    await _emit("start", message=f"Synthesising '{topic}'",
                datasets=datasets, primary=ds_primary)

    async def _load_graph():
        ents, rels = [], []
        for ds in (datasets or [ds_primary]):
            q = await _call_cap("fabric.entity_graph.query", search="",
                                dataset_id=ds, limit=600) or {}
            ents.extend(q.get("entities", []) or [])
            rels.extend(q.get("relationships", q.get("relations", [])) or [])
        # de-dup entities by normalised name
        seen, uent = set(), []
        for e in ents:
            k = _nrm(e.get("name", ""))
            if k and k not in seen:
                seen.add(k); uent.append(e)
        return uent, rels

    entities, relations = await _load_graph()
    records = []
    for ds in (datasets or [ds_primary]):
        records.extend(_synth_records(ds, 300))
    await _emit("loaded", entities=len(entities), relations=len(relations),
                records=len(records),
                message=f"{len(entities)} entities, {len(records)} records in scope")

    # ── PLAN: how should a coherent picture of this topic be structured? ──────
    roster = "; ".join(f'{e.get("name","")} [{e.get("type","")}]'
                       for e in entities[:80])
    titles = " | ".join((r["title"] or r["url"])[:60] for r in records[:40])
    plan_sys = ("You are a research architect. Given a topic, plan the structure a "
                "COHERENT, complete picture of it requires. Output strict JSON only.")
    plan_prompt = (
        f"TOPIC: {topic}\n"
        + (f"FOCUS: {focus}\n" if focus else "")
        + f"KNOWN ENTITIES: {roster or '(none yet)'}\n"
        + f"SOURCE TITLES: {titles or '(none yet)'}\n\n"
        "Plan the structure. Return ONLY:\n"
        '{"entry_type":"what each entry/item represents (singular noun)",'
        '"structure":"1-2 sentences on what a complete picture contains",'
        '"expected_entries":["the specific items the picture should include, if '
        'enumerable (e.g. each species/member/chapter) — else empty"],'
        '"aspects":["the fields/sub-facts each entry needs"],'
        '"discovery_queries":["targeted web searches that would fill this in"]}')
    await _emit("planning", message="LLM planning topic structure")
    plan = _sjson(await _llm_generate(plan_prompt, plan_sys, timeout=90)) or {}
    entry_type = str(plan.get("entry_type") or "entry")
    expected = [str(x).strip() for x in (plan.get("expected_entries") or []) if str(x).strip()]
    aspects = [str(x).strip() for x in (plan.get("aspects") or []) if str(x).strip()]
    dqueries = [str(x).strip() for x in (plan.get("discovery_queries") or []) if str(x).strip()]
    await _emit("planned", entry_type=entry_type, expected=len(expected),
                aspects=aspects[:12],
                message=f"plan: {entry_type}; {len(expected)} expected entries; "
                        f"aspects: {', '.join(aspects[:8])}")

    # ── COVERAGE + GAP-FILLING DISCOVERY ──────────────────────────────────────
    def _coverage(targets):
        have = {_nrm(e.get("name", "")) for e in entities}
        hay = " ".join((r["title"] + " " + r["text"][:600]) for r in records).lower()
        present, missing = [], []
        for tname in targets:
            k = _nrm(tname)
            if k and (k in have or _nrm(tname) in _nrm(hay)):
                present.append(tname)
            else:
                missing.append(tname)
        return present, missing

    discovery_runs = []
    gaps = []
    if expected:
        _, gaps = _coverage(expected)
    rounds = max(0, int(max_discovery_rounds or 0))
    if allow_discovery and rounds and (gaps or dqueries):
        for rnd in range(rounds):
            queries = list(dqueries)
            queries += [f"{topic} {g}" for g in gaps[:6]]
            queries = queries[:5]
            if not queries:
                break
            await _emit("discovering", round=rnd + 1, queries=queries,
                        message=f"round {rnd+1}: filling gaps via "
                                f"{len(queries)} searches")
            for q in queries:
                cid = _new_crawl_id(q, ds_primary)
                r = await cap_discover_map_topic(
                    topic=q, depth=discovery_depth, dataset_id=ds_primary,
                    sites=sites, use_llm_entities=True, focus=focus,
                    crawl_id=cid, trace_id=None)
                discovery_runs.append({"query": q,
                                       "dataset_id": (r or {}).get("dataset_id", ds_primary),
                                       "pages": (r or {}).get("pages_fetched", 0)})
            if ds_primary not in datasets:
                datasets.append(ds_primary)
            entities, relations = await _load_graph()
            records = []
            for ds in (datasets or [ds_primary]):
                records.extend(_synth_records(ds, 300))
            dqueries = []
            if expected:
                _, gaps = _coverage(expected)
                await _emit("coverage", round=rnd + 1,
                            covered=len(expected) - len(gaps), total=len(expected),
                            message=f"coverage {len(expected)-len(gaps)}/{len(expected)}")
                if not gaps:
                    break

    # ── ENTITY PROFILES: build per-entity LLM picture from source context ────
    # Run this before synthesis so _entity_ctx() has descriptions, aliases,
    # attributes and facts — these feed directly into the per-entry distillation
    # prompt and are the backbone of the 3rd-order model.
    _prof_fn = None
    try:
        _wa_mod = _wa()
        _prof_fn = getattr(_wa_mod, "_llm_profile", None)
    except Exception:
        pass

    if _prof_fn and entities and records:
        await _emit("profiling", message=f"building entity profiles for top entities")
        _rec_by_id = {r["id"]: r for r in records if r.get("id")}
        # Sort by mention_count; profile the top salient entities
        _top_ents = sorted(entities, key=lambda e: -e.get("mention_count", 1))[:40]
        _profiled = 0
        for _e in _top_ents:
            _name = _e.get("name", "")
            if not _name or _e.get("description"):  # skip if already profiled
                continue
            _ctxs = []
            for _rid in (list(_e.get("record_ids", [])) if isinstance(_e.get("record_ids"), (set, list)) else [])[:8]:
                _rec = _rec_by_id.get(_rid)
                if not _rec or not _rec.get("text"):
                    continue
                _pos = _rec["text"].lower().find(_name.lower())
                if _pos < 0:
                    _pos = 0
                _ctxs.append(_rec["text"][max(0, _pos - 200):_pos + 500])
            if not _ctxs:
                # fall back to name-search across records
                for _rec in records[:30]:
                    if _name.lower() in (_rec.get("text") or "").lower():
                        _ctxs.append(_rec["text"][:600])
                        if len(_ctxs) >= 4:
                            break
            if not _ctxs:
                continue
            try:
                _prof = await _prof_fn(_name, _e.get("type", ""), _ctxs, focus)
            except Exception as _pe:
                log.debug("profile %s: %s", _name, _pe)
                continue
            if not _prof:
                continue
            if _prof.get("type") and _e.get("type") in ("named_entity", "concept", ""):
                _e["type"] = _prof["type"]
            if _prof.get("description"):
                _e["description"] = _prof["description"]
            if _prof.get("aliases"):
                _al = set(_e.get("aliases", [])); _al.update(_prof["aliases"])
                _al.discard(_name); _e["aliases"] = sorted(_al)[:20]
            if _prof.get("attributes"):
                _e.setdefault("attributes", {}).update(_prof["attributes"])
            if _prof.get("facts"):
                _fs = list(_e.get("facts", []))
                for _f in _prof["facts"]:
                    if _f not in _fs:
                        _fs.append(_f)
                _e["facts"] = _fs[:20]
            _profiled += 1
        await _emit("profiled", count=_profiled,
                    message=f"profiled {_profiled} entities — descriptions ready for synthesis")

    # ── SYNTHESIS: distil sources into structured entries (batched) ───────────
    targets = expected[:max_entries] if expected else [
        e.get("name", "") for e in sorted(
            entities, key=lambda e: -e.get("mention_count", 1))[:max_entries]]
    targets = [t for t in targets if t]
    entries = []
    syn_sys = ("You distil multiple source excerpts and entity profiles into a single "
               "clean, factual record per item. Use ONLY supplied facts. Never invent. "
               "Use SPECIFIC relation verbs (FOUNDED, PART_OF, USES, LEADS, DEFEATS, "
               "APPEARED_IN, CREATED_BY, etc) — never RELATED_TO or CO_OCCURS. "
               "Output strict JSON only.")
    # Index records by id + name-search blobs; entities by normalised name;
    # build an adjacency map of the entity graph for neighbourhood expansion.
    rec_by_id = {r["id"]: r for r in records if r.get("id")}
    rec_blobs = [(r.get("id", ""), (r["title"] + " " + r["text"])) for r in records]
    ent_by_name = {_nrm(e.get("name", "")): e for e in entities}
    adj: Dict[str, list] = {}
    for r in relations:
        fn = r.get("from_name", "") or r.get("from", "")
        tn = r.get("to_name", "") or r.get("to", "")
        if not fn or not tn:
            continue
        rel = r.get("rel", "RELATED_TO")
        why = r.get("context", "") or r.get("why", "")
        adj.setdefault(_nrm(fn), []).append((tn, rel, why, "out"))
        adj.setdefault(_nrm(tn), []).append((fn, rel, why, "in"))

    def _neighbours(nk, depth):
        seen, frontier, out = {nk}, [nk], []
        for _ in range(max(1, depth)):
            nxt = []
            for cur in frontier:
                for (other, _rel, _why, _d) in adj.get(cur, []):
                    ok = _nrm(other)
                    if ok in seen:
                        continue
                    seen.add(ok); nxt.append(ok)
                    ne = ent_by_name.get(ok)
                    if ne:
                        out.append(ne)
            frontier = nxt
            if not frontier:
                break
        return out

    def _entity_ctx(name):
        e = ent_by_name.get(_nrm(name)) or {}
        props = e.get("props", {}) if isinstance(e.get("props"), dict) else {}
        desc = props.get("description", "") or e.get("description", "")
        aliases = props.get("aliases", e.get("aliases", []) or [])
        attrs = props.get("attributes", {}) or {}
        facts = props.get("facts", []) or []
        parts = [f"### {name}  [type: {e.get('type','?')}]"]
        if desc:
            parts.append(desc[:400])
        if aliases:
            parts.append("aliases: " + ", ".join([str(a) for a in aliases][:6]))
        if attrs:
            parts.append("attributes: " + "; ".join(
                f"{k}={v}" for k, v in list(attrs.items())[:12]))
        if facts:
            parts.append("known facts: " + " | ".join(str(f) for f in facts[:8]))
        rels = adj.get(_nrm(name), [])
        if rels:
            parts.append("direct relations:")
            for (other, rel, why, d) in rels[:25]:
                arrow = (f"{name} -{rel}-> {other}" if d == "out"
                         else f"{other} -{rel}-> {name}")
                parts.append("  - " + arrow + (f"  ({why})" if why else ""))
        if neighbor_depth >= 1:
            neigh = _neighbours(_nrm(name), neighbor_depth)
            if neigh:
                parts.append(f"connected entities (≤{neighbor_depth} hops):")
                for ne in neigh[:25]:
                    np = ne.get("props", {}) if isinstance(ne.get("props"), dict) else {}
                    nd = np.get("description", "") or ne.get("description", "")
                    parts.append(f"  - {ne.get('name','')} [{ne.get('type','')}]"
                                 + (f": {nd[:120]}" if nd else ""))
        # full content of the records this entity was extracted from
        srcs, budget = [], max(600, int(max_source_chars))
        for rid in (e.get("record_ids", []) or [])[:8]:
            rec = rec_by_id.get(rid)
            if not rec or not rec.get("text"):
                continue
            take = rec["text"][:budget] if full_source else rec["text"][:700]
            srcs.append(f"[source: {rec.get('title') or rec.get('url') or rid}]\n{take}")
            budget -= len(take)
            if budget <= 0:
                break
        if not srcs:  # fall back to name search
            for rid, blob in rec_blobs:
                if name.lower() in blob.lower() or (_nrm(name) and _nrm(name) in _nrm(blob)):
                    srcs.append(blob[:1400])
                    if len(srcs) >= 3:
                        break
        if srcs:
            parts.append("SOURCE CONTENT:\n" + "\n---\n".join(srcs))
        return "\n".join(parts)

    BATCH = 6 if full_source else 10
    for bi in range(0, len(targets), BATCH):
        batch = targets[bi:bi + BATCH]
        ctx_parts = [_entity_ctx(name) for name in batch]
        syn_prompt = (
            f"TOPIC: {topic}\nENTRY TYPE: {entry_type}\n"
            f"FIELDS WANTED: {', '.join(aspects[:12]) or 'key facts'}\n"
            + (f"FOCUS: {focus}\n" if focus else "")
            + "Distil each item below into one factual entry. Return ONLY:\n"
            '{"entries":[{"name":"","type":"","description":"2-3 sentence factual portrait of this entity grounded in the sources","attributes":{},'
            '"facts":["short grounded facts"],'
            '"relations":[{"to":"other item/entity","rel":"UPPER_SNAKE","why":""}]}]}'
            "\nInclude only items you have evidence for.\n\n"
            + "\n\n".join(ctx_parts))
        await _emit("synthesising",
                    done=len(entries), total=len(targets),
                    message=f"distilling entries {bi+1}-{min(bi+BATCH,len(targets))}"
                            f" of {len(targets)}")
        data = _sjson(await _llm_generate(syn_prompt, syn_sys, timeout=150)) or {}
        for it in (data.get("entries") or []):
            if not isinstance(it, dict) or not it.get("name"):
                continue
            entries.append({
                "name": str(it["name"])[:120],
                "type": str(it.get("type") or entry_type)[:60],
                "description": str(it.get("description") or "")[:600],
                "attributes": it.get("attributes") if isinstance(it.get("attributes"), dict) else {},
                "facts": [str(f)[:240] for f in (it.get("facts") or [])][:15],
                "relations": [r for r in (it.get("relations") or [])
                              if isinstance(r, dict) and r.get("to")][:12],
            })
        if len(entries) >= max_entries:
            break

    # ── OVERVIEW SUMMARY ──────────────────────────────────────────────────────
    names = ", ".join(e["name"] for e in entries[:60])
    summ = await _llm_generate(
        f"TOPIC: {topic}\nENTRY TYPE: {entry_type}\nITEMS: {names}\n\n"
        "Write a concise, coherent 4-6 sentence overview of this topic as a whole, "
        "grounded in the items above.",
        "You write factual, coherent topic overviews.", timeout=90) or ""
    await _emit("summarised", entries=len(entries))

    # ── Relations: per-entry (direct) + an LLM pass for complex/distant edges ──
    model_id = "topic_model:" + hashlib.sha1(
        (topic + "|" + ds_primary).encode()).hexdigest()[:14]
    flat_relations = []
    for e in entries:
        for r in e.get("relations", []):
            if not r.get("to"):
                continue
            _rel = re.sub(r"[^A-Z_]", "", str(r.get("rel") or "")
                          .upper().replace(" ", "_").replace("-", "_"))
            if not _rel or _rel in ("RELATED_TO", "RELATED", "CO_OCCURS",
                                    "ASSOCIATED_WITH", "MENTIONED_WITH"):
                continue
            flat_relations.append({"from": e["name"], "to": r["to"],
                                   "rel": _rel,
                                   "why": r.get("why", ""), "kind": "direct"})
    inferred = 0
    if infer_edges and len(entries) >= 2:
        await _emit("inferring_edges",
                    message="LLM mapping complex / distant relations")
        roster = "\n".join(
            f'- {e["name"]} [{e["type"]}]'
            + (": " + "; ".join(e["facts"][:3]) if e.get("facts") else "")
            for e in entries[:80])
        existing = "; ".join(f'{r["from"]}->{r["to"]}' for r in flat_relations[:120])
        isys = ("You map NON-OBVIOUS, complex or distant relationships between the "
                "listed concepts — causal, hierarchical, transitive, thematic or "
                "part-of links that are NOT already listed. Use SPECIFIC UPPER_SNAKE "
                "verbs only (CAUSES, ENABLES, PRECEDES, PART_OF, SUPERSEDES, "
                "RIVAL_OF, ALLIED_WITH, CHILD_OF, MEMBER_OF, INFLUENCED, etc). "
                "NEVER use RELATED_TO or CO_OCCURS. Output strict JSON only.")
        iprompt = ("CONCEPTS:\n" + roster
                   + (f"\n\nALREADY LINKED: {existing}" if existing else "")
                   + (f"\nFOCUS: {focus}" if focus else "")
                   + '\n\nReturn ONLY {"relations":[{"from":"concept","to":"concept",'
                   '"rel":"UPPER_SNAKE","why":"brief justification"}]}\n'
                   "Only well-justified, non-trivial links between listed concepts.")
        idata = _sjson(await _llm_generate(iprompt, isys, timeout=180)) or {}
        names_l = {e["name"].lower(): e["name"] for e in entries}
        seen_pairs = {(_nrm(r["from"]), _nrm(r["to"]), r["rel"]) for r in flat_relations}
        for r in (idata.get("relations") or [])[:200]:
            if not isinstance(r, dict):
                continue
            fa = names_l.get(str(r.get("from", "")).strip().lower())
            tb = names_l.get(str(r.get("to", "")).strip().lower())
            if not fa or not tb or fa == tb:
                continue
            rel = re.sub(r"[^A-Z_]", "", str(r.get("rel") or "")
                         .upper().replace(" ", "_").replace("-", "_"))
            # Drop any edge where the LLM couldn't find a specific verb
            if not rel or rel in ("RELATED_TO", "RELATED", "CO_OCCURS",
                                  "ASSOCIATED_WITH", "CONNECTED_TO", "MENTIONED_WITH"):
                continue
            key = (_nrm(fa), _nrm(tb), rel)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            flat_relations.append({"from": fa, "to": tb, "rel": rel,
                                   "why": str(r.get("why", ""))[:200],
                                   "kind": "inferred"})
            inferred += 1
        await _emit("inferred_edges", count=inferred)

    if persist and entries:
        ts = now_iso()
        try:
            conn = _sqlite_conn()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS fabric_topic_models "
                "(id TEXT PRIMARY KEY, topic TEXT, dataset_id TEXT, entry_type TEXT, "
                "summary TEXT, payload TEXT, entry_count INTEGER, created_at TEXT)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS fabric_topic_entries "
                "(model_id TEXT, name TEXT, type TEXT, props TEXT, created_at TEXT)")
            conn.execute(
                "INSERT OR REPLACE INTO fabric_topic_models "
                "(id, topic, dataset_id, entry_type, summary, payload, entry_count, "
                "created_at) VALUES (?,?,?,?,?,?,?,?)",
                (model_id, topic, ds_primary, entry_type, summ,
                 json.dumps({"aspects": aspects, "datasets": datasets,
                             "discovery_runs": discovery_runs,
                             "relations": flat_relations[:500]}),
                 len(entries), ts))
            conn.execute("DELETE FROM fabric_topic_entries WHERE model_id=?", (model_id,))
            for e in entries:
                conn.execute(
                    "INSERT INTO fabric_topic_entries "
                    "(model_id, name, type, props, created_at) VALUES (?,?,?,?,?)",
                    (model_id, e["name"], e["type"],
                     json.dumps({"attributes": e.get("attributes") or {},
                                 "facts": e.get("facts") or [],
                                 "description": e.get("description") or "",
                                 "aliases": e.get("aliases") or []}),
                     ts))
            conn.commit()
        except Exception as e:
            log.warning("topic model sqlite: %s", e)

        g = _get_graph()
        if g and getattr(g, "available", False):
            try:
                await g.upsert_node("TopicModel", model_id,
                                    {"id": model_id, "topic": topic,
                                     "entry_type": entry_type, "summary": summ[:1000],
                                     "dataset_id": ds_primary})
                for e in entries:
                    cid = "concept:" + hashlib.sha1(
                        (model_id + "|" + e["name"]).encode()).hexdigest()[:14]
                    # Pull the entity profile built during the profiling pass
                    _ent = ent_by_name.get(_nrm(e["name"])) or {}
                    _props = _ent.get("props", {}) if isinstance(_ent.get("props"), dict) else {}
                    await g.upsert_node("Concept", cid, {
                        "id": cid, "name": e["name"], "type": e["type"],
                        "model_id": model_id,
                        "facts": e["facts"][:12],
                        "attributes": json.dumps(e.get("attributes") or {}),
                        "description": (e.get("description") or
                                        _props.get("description") or
                                        _ent.get("description") or ""),
                        "aliases": (_ent.get("aliases") or
                                    _props.get("aliases") or [])[:10],
                        "topic": topic})
                    await g.query(
                        "MATCH (m:TopicModel {id:$m}),(c:Concept {id:$c}) "
                        "MERGE (m)-[:HAS_ENTRY]->(c)", {"m": model_id, "c": cid})
                    # link concept back to the 2nd-order entity when names match
                    await g.query(
                        "MATCH (c:Concept {id:$c}),(en:Entity) "
                        "WHERE toLower(en.name)=toLower($n) "
                        "MERGE (c)-[:DISTILLED_FROM]->(en)",
                        {"c": cid, "n": e["name"]})
                # inter-concept relations
                for r in flat_relations[:500]:
                    await g.query(
                        "MATCH (a:Concept {model_id:$m}),(b:Concept {model_id:$m}) "
                        "WHERE toLower(a.name)=toLower($fa) AND toLower(b.name)=toLower($tb) "
                        "MERGE (a)-[x:CONCEPT_REL {rel:$rel}]->(b) "
                        "SET x.why=$why, x.kind=$kind",
                        {"m": model_id, "fa": r["from"], "tb": r["to"],
                         "rel": r["rel"], "why": r.get("why", ""),
                         "kind": r.get("kind", "direct")})
            except Exception as e:
                log.debug("topic model neo4j: %s", e)

    await _emit("done", model_id=model_id, entries=len(entries),
                relations=len(flat_relations), inferred=inferred,
                discovery_runs=len(discovery_runs),
                message=f"3rd-order model: {len(entries)} entries, "
                        f"{len(flat_relations)} relations ({inferred} inferred)")
    return {"ok": True, "model_id": model_id, "topic": topic,
            "entry_type": entry_type, "summary": summ,
            "entries": entries, "entry_count": len(entries),
            "relations": flat_relations, "inferred_relations": inferred,
            "datasets": datasets, "gaps": gaps, "discovery_runs": discovery_runs}




@capability(
    "fabric.domains.authority",
    http_method="GET", http_path="/fabric/domains/authority",
    http_tags=["fabric", "discovery"], memory="off",
    description="Learned domain-relevance-vs-topic table (Google-indexing style): "
                "which domains have proven to be good, authoritative sources for a "
                "topic. Input: topic (str, optional — blank = all), limit (int). "
                "Output: {topic_key, domains:[{domain, source_type, base_authority, "
                "score, pages, avg_relevance, avg_novelty}]}.",
)
async def cap_domains_authority(topic: str = "", limit: int = 50, trace_id=None) -> Dict:
    tk = _topic_key(topic) if topic else None
    try:
        if tk:
            rows = _sqlite_conn().execute(
                "SELECT domain, source_type, base_authority, pages, relevance_sum, "
                "unique_sum, score FROM fabric_domain_authority WHERE topic_key=? "
                "ORDER BY score DESC LIMIT ?", (tk, int(limit))).fetchall()
        else:
            rows = _sqlite_conn().execute(
                "SELECT domain, source_type, base_authority, pages, relevance_sum, "
                "unique_sum, score FROM fabric_domain_authority "
                "ORDER BY score DESC LIMIT ?", (int(limit),)).fetchall()
    except Exception as e:
        return {"error": str(e), "domains": []}
    out = []
    for r in rows:
        p = max(1, r["pages"] or 1)
        out.append({"domain": r["domain"], "source_type": r["source_type"],
                    "base_authority": round(r["base_authority"] or 0, 3),
                    "score": round(r["score"] or 0, 3), "pages": r["pages"],
                    "avg_relevance": round((r["relevance_sum"] or 0) / p, 3),
                    "avg_novelty": round((r["unique_sum"] or 0) / p, 3)})
    return {"topic_key": tk or "*", "domains": out}


@capability(
    "fabric.synthesize.list",
    http_method="GET", http_path="/fabric/synthesize/list",
    http_tags=["fabric", "synthesize"], memory="off",
    description="List persisted 3rd-order topic models. "
                "Output: {models:[{id, topic, dataset_id, entry_type, entry_count, "
                "created_at}]}.",
)
async def cap_synthesize_list(limit: int = 100, trace_id=None) -> Dict:
    try:
        rows = _sqlite_conn().execute(
            "SELECT id, topic, dataset_id, entry_type, entry_count, created_at "
            "FROM fabric_topic_models ORDER BY created_at DESC LIMIT ?",
            (int(limit),)).fetchall()
    except Exception:
        return {"models": []}
    return {"models": [{"id": r["id"], "topic": r["topic"],
                        "dataset_id": r["dataset_id"], "entry_type": r["entry_type"],
                        "entry_count": r["entry_count"], "created_at": r["created_at"]}
                       for r in rows]}


@capability(
    "fabric.synthesize.get",
    http_method="POST", http_path="/fabric/synthesize/get",
    http_tags=["fabric", "synthesize"], memory="off",
    description="Fetch one 3rd-order topic model with its entries and relations. "
                "Input: model_id (str!). Output: {model, entries, relations}.",
)
async def cap_synthesize_get(model_id: str = "", trace_id=None) -> Dict:
    if not model_id:
        return {"error": "model_id required"}
    conn = _sqlite_conn()
    try:
        m = conn.execute(
            "SELECT id, topic, dataset_id, entry_type, summary, payload, "
            "entry_count, created_at FROM fabric_topic_models WHERE id=?",
            (model_id,)).fetchone()
    except Exception as e:
        return {"error": str(e)}
    if not m:
        return {"error": "model not found"}
    try:
        payload = json.loads(m["payload"] or "{}")
    except Exception:
        payload = {}
    entries = []
    try:
        for r in conn.execute(
                "SELECT name, type, props FROM fabric_topic_entries "
                "WHERE model_id=? ORDER BY rowid", (model_id,)).fetchall():
            try:
                props = json.loads(r["props"] or "{}")
            except Exception:
                props = {}
            entries.append({"name": r["name"], "type": r["type"],
                            "attributes": props.get("attributes", {}),
                            "facts": props.get("facts", [])})
    except Exception:
        pass
    return {"model": {"id": m["id"], "topic": m["topic"],
                      "dataset_id": m["dataset_id"], "entry_type": m["entry_type"],
                      "summary": m["summary"], "entry_count": m["entry_count"],
                      "created_at": m["created_at"],
                      "aspects": payload.get("aspects", []),
                      "datasets": payload.get("datasets", []),
                      "discovery_runs": payload.get("discovery_runs", [])},
            "entries": entries,
            "relations": payload.get("relations", [])}


@capability(
    "fabric.synthesize.delete",
    http_method="POST", http_path="/fabric/synthesize/delete",
    http_tags=["fabric", "synthesize"], memory="off",
    description="Delete a 3rd-order topic model (SQLite rows + Neo4j Concept "
                "layer). Input: model_id (str!). Output: {ok}.",
)
async def cap_synthesize_delete(model_id: str = "", trace_id=None) -> Dict:
    if not model_id:
        return {"error": "model_id required"}
    try:
        conn = _sqlite_conn()
        conn.execute("DELETE FROM fabric_topic_models WHERE id=?", (model_id,))
        conn.execute("DELETE FROM fabric_topic_entries WHERE model_id=?", (model_id,))
        conn.commit()
    except Exception as e:
        return {"error": str(e)}
    g = _get_graph()
    if g and getattr(g, "available", False):
        for cy in ("MATCH (c:Concept {model_id:$m}) DETACH DELETE c",
                   "MATCH (t:TopicModel {id:$m}) DETACH DELETE t"):
            try:
                await g.query(cy, {"m": model_id})
            except Exception:
                pass
    return {"ok": True, "model_id": model_id}


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
    mode: str = "auto", max_links: int = 25, trace_id=None,
) -> Dict:
    _ensure_tables()
    nl = (node_label or "").lower()
    if not node_id:
        return {"error": "node_id required"}
    max_links = max(1, min(120, int(max_links)))

    # ── Surface: crawl crawlable surfaces, else promote + pull data ─────────
    if nl == "surface":
        row = _sqlite_conn().execute(
            "SELECT * FROM fabric_surfaces WHERE id=?", (node_id,)).fetchone()
        if not row:
            return {"error": f"surface not found: {node_id}"}
        row = dict(row)
        parent = row.get("parent_dataset", "") or "discovered"
        kind = (row.get("kind") or "").lower()
        surl = row.get("url") or ""
        crawlable = kind in ("docs", "wiki", "sitemap", "github", "gitlab", "gitea",
                             "graphql", "openapi", "json_api") or kind.startswith("web")
        do_crawl = (mode == "crawl") or (mode == "auto" and crawlable and surl.startswith("http"))
        if do_crawl and surl.startswith("http"):
            # crawl the surface as a fresh seed into the parent dataset
            sub_cid = _new_crawl_id(surl, parent)
            cfg = {"max_pages": max_links + 1, "max_depth": 2, "same_domain": False,
                   "topic": "", "topic_dropoff": 99, "detect_surfaces": True,
                   "extract_subtables": True, "extract_entities": True,
                   "auto_promote": False, "tags": "expand,surface"}
            try:
                await _run_crawl(sub_cid, parent, _normalize_url(surl), cfg,
                                 [(_normalize_url(surl), 0, 0, None)], set())
            except Exception as e:
                return {"error": f"surface crawl failed: {e}"}
            _record_edge(row.get("crawl_id", ""), parent, node_id,
                         _normalize_url(surl), "LINKS_TO", "Page")
            g = await cap_discover_graph(crawl_id=sub_cid, dataset_id=parent,
                                         include_entities=True)
            _mark_promoted(node_id, "(crawled)")
            return {"ok": True, "nodes": g.get("nodes", []), "edges": g.get("edges", []),
                    "added": len(g.get("nodes", [])),
                    "note": f"Crawled {kind} surface ({g.get('stats', {}).get('pages', 0)} pages)."}
        # else: register as a source + pull, fold ingested data in
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
                                    "relevance": 0.8, "source_id": res.get("source_id", "")}})
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

    # ── Page: synthesize 3rd-order model for its containing dataset ────────
    if nl == "page" and mode == "synthesize":
        url = node_id.split("#")[0]
        ds = (dataset_id or "").strip() or _dataset_for_url(url) or _auto_ds(url)
        if not ds:
            return {"error": "cannot determine dataset for this page"}
        synth_cap = CAPABILITY_REGISTRY.get("fabric.synthesize.topic")
        if not synth_cap:
            return {"error": "fabric.synthesize.topic not registered"}
        fn = synth_cap.get("func") or synth_cap.get("raw")
        if not fn:
            return {"error": "fabric.synthesize.topic has no callable"}
        try:
            r = await fn(dataset_id=ds, max_records=max_links or 50, trace_id=trace_id)
            return {"ok": True, "nodes": [], "edges": [],
                    "added": 0,
                    "topic": r.get("topic", ""),
                    "entries": r.get("entries", 0),
                    "note": f"3rd-order model built for {ds} ({r.get('entries', 0)} entries)."}
        except Exception as e:
            return {"error": f"synthesis failed: {e}"}

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


@capability(
    "fabric.discover.auto",
    http_method="POST", http_path="/fabric/discover/auto",
    http_tags=["fabric", "discover"], memory="on",
    description="Automatically keep mining the BEST detected surfaces of a "
                "dataset for useful, non-duplicate data. Each round ranks "
                "un-pulled surfaces by confidence × topic relevance, then crawls "
                "crawlable ones and pulls data ones, skipping anything already "
                "ingested (normalised-URL dedupe). "
                "Input: dataset_id (str!), rounds (int=3), per_round (int=4), "
                "min_confidence (float=0.5), max_pages_each (int=20). "
                "Output: {dataset_id, pulled, surfaces_done, rounds_run, details}.",
)
async def cap_discover_auto(
    dataset_id: str = "", rounds: int = 3, per_round: int = 4,
    min_confidence: float = 0.5, max_pages_each: int = 20, trace_id=None,
) -> Dict:
    if not dataset_id.strip():
        return {"error": "dataset_id required"}
    ds_id = dataset_id.strip()
    _ensure_tables()
    conn = _sqlite_conn()
    done, details = 0, []
    rounds_run = 0

    async def _emit(stage, **kw):
        try:
            await emit_event({"type": "fabric.discover.progress",
                              "engine": "discovery", "stage": stage,
                              "dataset_id": ds_id, **kw})
        except Exception:
            pass

    for rnd in range(max(1, rounds)):
        try:
            rows = conn.execute(
                "SELECT * FROM fabric_surfaces WHERE parent_dataset=? AND promoted=0 "
                "AND confidence >= ? ORDER BY (confidence + COALESCE(topic_score,0)) DESC "
                "LIMIT ?", (ds_id, min_confidence, per_round * 3)).fetchall()
        except Exception as e:
            return {"error": f"surface query: {e}", "pulled": done}
        if not rows:
            break
        rounds_run += 1
        picked = 0
        await _emit("auto_round", round=rnd + 1, candidates=len(rows))
        for r in rows:
            if picked >= per_round:
                break
            row = dict(r)
            try:
                res = await cap_discover_expand(
                    node_label="Surface", node_id=row["id"],
                    mode="auto", max_links=max_pages_each)
            except Exception as e:
                res = {"error": str(e)}
            picked += 1; done += 1
            details.append({"surface": row.get("label") or row.get("url"),
                            "kind": row.get("kind"),
                            "added": res.get("added", 0),
                            "error": res.get("error")})
            await _emit("auto_pulled", surface=row.get("label") or row.get("url"),
                        added=res.get("added", 0))
        # refresh connection (new surfaces may have appeared)
        conn = _sqlite_conn()

    # final cross-source linking + loom stitch within dataset
    try:
        await _build_global_entity_links(ds_id)
    except Exception:
        pass
    await _emit("auto_done", pulled=done, rounds=rounds_run)
    return {"ok": True, "dataset_id": ds_id, "pulled": done,
            "surfaces_done": done, "rounds_run": rounds_run, "details": details[:50]}


# ---------------------------------------------------------------------------
# Scrape a single page and return its full text + metadata
# ---------------------------------------------------------------------------
@capability(
    "fabric.discover.scrape_page",
    http_method="POST", http_path="/fabric/discover/scrape_page",
    http_tags=["fabric", "discover"], memory="off",
    description=(
        "Fetch a single URL and extract its full text, headings, links, and metadata "
        "without ingesting it into the fabric. Also stores/updates the Page record "
        "in the current dataset so the content is visible in the graph. "
        "Input: url (str!), dataset_id (str optional), max_links (int=100). "
        "Output: {ok, url, title, full_text, word_count, headings, links, tags, stored}."
    ),
)
async def cap_discover_scrape_page(
    url: str = "", dataset_id: str = "", max_links: int = 100, trace_id=None,
) -> Dict:
    url = (url or "").strip()
    if not url or not url.startswith("http"):
        return {"error": "url required (must start with http)"}
    import httpx as _httpx
    wa = _wa()
    try:
        async with _httpx.AsyncClient(timeout=20, follow_redirects=True,
                                       headers=_HTTP_HEADERS) as client:
            resp = await client.get(url)
        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code}", "url": url}
        ct = resp.headers.get("content-type", "").lower()
        raw = resp.text
    except Exception as e:
        return {"error": f"fetch: {e}", "url": url}

    structure = wa._extract_page_structure(raw, url, max_links=max_links, max_text=0)
    full_text = structure.get("full_text", "")
    title = structure.get("title", "") or url.split("/")[-1]
    headings = structure.get("headings", [])
    links = structure.get("links", [])
    tags = structure.get("tags", [])
    word_count = len(full_text.split())

    # Update/insert the Page record in the dataset so it's visible in the graph
    stored = False
    ds_id = (dataset_id or "").strip()
    if not ds_id:
        ds_id = _dataset_for_url(url) or ""
    if ds_id:
        try:
            _ensure_tables()
            conn = _sqlite_conn()
            import hashlib as _hashlib
            page_id = "page:" + _hashlib.sha1(url.encode()).hexdigest()[:16]
            tags_str = ",".join(tags[:20]) if tags else ""
            headings_json = json.dumps(headings[:40])
            conn.execute(
                "INSERT OR REPLACE INTO fabric_pages "
                "(id, dataset_id, crawl_id, url, title, full_text, tags, headings, "
                " word_count, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))",
                (page_id, ds_id, "", url, title, full_text, tags_str,
                 headings_json, word_count)
            )
            conn.commit()
            stored = True
        except Exception as e:
            log.debug("scrape_page store: %s", e)

    return {
        "ok": True,
        "url": url,
        "title": title,
        "full_text": full_text,
        "word_count": word_count,
        "headings": headings[:40],
        "links": links[:max_links],
        "tags": tags[:20],
        "stored": stored,
        "dataset_id": ds_id,
    }


# ── Register graph node actions so the surface/page/sub-table nodes are
#    interactive in the VeraGraph drawer (Add as source / Pull & expand / …) ──
def _register_node_actions():
    try:
        from Vera.Orchestration.fabric.data_fabric import _NODE_ACTION_REGISTRY
    except Exception as e:
        log.debug("node-action registry unavailable: %s", e)
        return
    _NODE_ACTION_REGISTRY["Surface"] = [
        {"id": "browse_full", "label": "Browse full content", "icon": "\u2630",
         "capability": "fabric.surfaces.browse", "args": {"surface_id": "$id"},
         "options": [{"name": "offset", "type": "int", "default": 0, "label": "Offset"},
                     {"name": "page_size", "type": "int", "default": 100, "label": "Rows/page"},
                     {"name": "follow_next", "type": "bool", "default": True,
                      "label": "Follow pagination"}],
         "context": "Page through the entire surface without ingesting it."},
        {"id": "preview", "label": "Preview (no pull)", "icon": "\u25c9",
         "capability": "fabric.surfaces.preview", "args": {"surface_id": "$id"},
         "options": [{"name": "limit", "type": "int", "default": 20, "label": "Rows"}],
         "context": "Fetch and sample this surface read-only \u2014 explore it "
                    "without promoting/pulling it into the fabric."},
        {"id": "add_source", "label": "Add as data source", "icon": "\u2295",
         "capability": "fabric.surfaces.promote", "args": {"surface_id": "$id"},
         "options": [{"name": "auto_pull", "type": "bool", "default": True,
                      "label": "Pull immediately"}]},
        {"id": "pull_expand", "label": "Pull & expand into graph", "icon": "\u2935",
         "capability": "fabric.discover.expand",
         "args": {"node_id": "$id", "node_label": "Surface", "mode": "pull"}},
        {"id": "crawl_surface", "label": "Crawl this surface", "icon": "\u2398",
         "capability": "fabric.discover.expand",
         "args": {"node_id": "$id", "node_label": "Surface", "mode": "crawl"},
         "options": [{"name": "max_links", "type": "int", "default": 25,
                      "label": "Max pages"}]},
        {"id": "forget", "label": "Forget surface", "icon": "\u2715",
         "capability": "fabric.surfaces.delete", "args": {"surface_id": "$id"},
         "confirm": "Forget this surface?"},
    ]
    _NODE_ACTION_REGISTRY["Page"] = [
        {"id": "view_content", "label": "View full content", "icon": "\u2261",
         "capability": "__local",
         "context": "Show this page's scraped text in the content drawer."},
        {"id": "scrape_content", "label": "Scrape / refresh content", "icon": "\u21ba",
         "capability": "fabric.discover.scrape_page",
         "args": {"url": "$id"},
         "options": [{"name": "max_links", "type": "int", "default": 100,
                      "label": "Max links to capture"}],
         "context": "Fetch this page now and store its full text, headings and links."},
        {"id": "expand_links", "label": "Crawl this page\u2019s links", "icon": "\u2935",
         "capability": "fabric.discover.expand",
         "args": {"node_id": "$id", "node_label": "Page"},
         "options": [{"name": "max_links", "type": "int", "default": 25,
                      "label": "Max pages to crawl"}]},
        {"id": "extract_entities", "label": "Extract entities", "icon": "\u2b21",
         "capability": "fabric.discover.expand",
         "args": {"node_id": "$id", "node_label": "Page"}},
        {"id": "synthesize", "label": "3rd-order synthesis", "icon": "\u25c8",
         "capability": "fabric.discover.expand",
         "args": {"node_id": "$id", "node_label": "Page", "mode": "synthesize"},
         "options": [
             {"name": "max_records", "type": "int", "default": 50, "label": "Max records"},
         ],
         "context": "Build a 3rd-order topic model from the dataset containing this page.",
         "stream": "fabric.synthesize.progress"},
    ]
    _NODE_ACTION_REGISTRY["Subtable"] = [
        {"id": "browse", "label": "Browse records", "icon": "\u25e6",
         "capability": "__local",
         "context": "Open records in the browser modal."},
        {"id": "extract_entities", "label": "Extract entities", "icon": "\u2b21",
         "capability": "fabric.discover.expand",
         "args": {"node_id": "$id", "node_label": "Subtable"}},
        {"id": "synthesize", "label": "3rd-order synthesis", "icon": "\u25c8",
         "capability": "fabric.synthesize.topic",
         "args": {"dataset_id": "$id"},
         "options": [
             {"name": "max_records", "type": "int", "default": 50, "label": "Max records"},
         ],
         "context": "Build a 3rd-order topic model from this sub-table."},
    ]
    _NODE_ACTION_REGISTRY["Dataset"] = [
        {"id": "auto_mine", "label": "Auto-mine best surfaces", "icon": "\u21bb",
         "capability": "fabric.discover.auto", "args": {"dataset_id": "$id"},
         "options": [{"name": "rounds", "type": "int", "default": 3, "label": "Rounds"},
                     {"name": "per_round", "type": "int", "default": 4, "label": "Per round"}]},
        {"id": "extract_entities", "label": "Extract entities", "icon": "\u2b21",
         "capability": "fabric.discover.expand",
         "args": {"node_id": "$id", "node_label": "Dataset"}},
        {"id": "extract_full", "label": "Extract entities + relations", "icon": "\u2b21",
         "capability": "fabric.entity_graph.extract", "args": {"dataset_id": "$id"},
         "options": [
             {"name": "use_llm", "type": "bool", "default": True,
              "label": "LLM typed relations (reads relations from the text)"},
             {"name": "limit", "type": "int", "default": 200, "label": "Max records"},
             {"name": "content_type", "type": "select", "default": "text",
              "options": ["text", "code", "web"], "label": "Content"},
         ],
         "context": "Builds the second-order entity graph \u2014 typed entities + "
                    "explicit relations, normalised/deduped across datasets. LLM "
                    "mode infers named relations from the text instead of relying "
                    "on co-occurrence."},
        {"id": "loom_stitch", "label": "Loom: stitch related records", "icon": "\u2682",
         "capability": "fabric.loom.run", "args": {"dataset_a": "$id"},
         "options": [
             {"name": "dataset_b", "type": "string", "default": "",
              "label": "Against (blank = this dataset, * = all datasets)"},
             {"name": "mode", "type": "select", "default": "hybrid",
              "options": ["hybrid", "vector", "graph"], "label": "Mode"},
             {"name": "min_score", "type": "float", "default": 0.45, "label": "Min score"},
             {"name": "max_matches", "type": "int", "default": 100, "label": "Max matches"},
         ],
         "context": "Stitch RELATED_TO links between records by similarity \u2014 within "
                    "this dataset, or use * to relate it against ALL other datasets "
                    "(cross-dataset structure)."},
        {"id": "synthesize", "label": "3rd-order synthesis", "icon": "\u25c8",
         "capability": "fabric.synthesize.topic",
         "args": {"dataset_id": "$id"},
         "options": [
             {"name": "max_records", "type": "int", "default": 100, "label": "Max records"},
             {"name": "neighbor_depth", "type": "int", "default": 1, "label": "Neighbor depth"},
             {"name": "use_llm", "type": "bool", "default": True, "label": "LLM synthesis"},
         ],
         "context": "Build a 3rd-order topic model: distilled, cross-linked concept "
                    "entries synthesised from all records in this dataset.",
         "stream": "fabric.synthesize.progress"},
        {"id": "browse", "label": "Browse records", "icon": "\u25e6",
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


# ---------------------------------------------------------------------------
# Query the discovery graph with an LLM
# ---------------------------------------------------------------------------
@capability(
    "fabric.discover.query",
    http_method="POST", http_path="/fabric/discover/query",
    http_tags=["fabric", "discover"], memory="on",
    description=(
        "Ask the LLM a question about a crawl/dataset. Gathers page titles, summaries, "
        "and entity data from the discovery graph, builds a context window, and answers "
        "using the local LLM cluster. Good for: what did you find, summarise the topic, "
        "what entities appeared most. "
        "Input: question (str!), dataset_id (str), crawl_id (str), "
        "max_context_pages (int=40). "
        "Output: {answer, context_pages_used, dataset_id, question}."
    ),
)
async def cap_discover_query(
    question: str = "",
    dataset_id: str = "",
    crawl_id: str = "",
    max_context_pages: int = 40,
    trace_id=None,
) -> Dict:
    if not question.strip():
        return {"error": "question required"}
    _ensure_tables()
    conn = _sqlite_conn()

    ds_id = dataset_id.strip()
    cid   = crawl_id.strip()
    if cid and not ds_id:
        fr = _load_frontier(cid)
        if fr:
            ds_id = fr.get("dataset_id", "")

    page_rows: List[Dict] = []
    if ds_id:
        try:
            rows = conn.execute(
                "SELECT url, title, full_text, tags, word_count FROM fabric_pages "
                "WHERE dataset_id=? ORDER BY rowid DESC LIMIT ?",
                (ds_id, max_context_pages * 2)
            ).fetchall()
            page_rows = [dict(r) for r in rows]
        except Exception:
            pass
    if not page_rows and cid:
        try:
            rows = conn.execute(
                "SELECT url, title, full_text, tags, word_count FROM fabric_pages "
                "WHERE crawl_id=? ORDER BY rowid DESC LIMIT ?",
                (cid, max_context_pages * 2)
            ).fetchall()
            page_rows = [dict(r) for r in rows]
        except Exception:
            pass

    surf_summary = ""
    if ds_id:
        try:
            srows = conn.execute(
                "SELECT kind, COUNT(*) as n FROM fabric_surfaces "
                "WHERE parent_dataset=? GROUP BY kind", (ds_id,)
            ).fetchall()
            if srows:
                surf_summary = "Detected surfaces: " + ", ".join(
                    f"{r['n']} {r['kind']}" for r in srows)
        except Exception:
            pass

    if not page_rows:
        return {"error": "no pages found for this dataset/crawl -- try crawling first",
                "dataset_id": ds_id, "question": question}

    context_parts: List[str] = []
    for p in page_rows[:max_context_pages]:
        title   = (p.get("title") or "").strip()
        url     = (p.get("url") or "").strip()
        text    = (p.get("full_text") or "").strip()
        snippet = text[:600] if text else ""
        tags    = p.get("tags") or ""
        if isinstance(tags, str) and tags:
            snippet += " [tags: " + tags[:80] + "]"
        header = title or url
        context_parts.append("### " + header + "\nURL: " + url + "\n" + snippet)

    context_block = "\n\n".join(context_parts)
    if surf_summary:
        context_block = surf_summary + "\n\n" + context_block

    system = (
        "You are an expert research assistant. You have been given a collection of "
        "crawled web pages and their content extracted by a discovery engine. "
        "Answer the user's question accurately and concisely based ONLY on the "
        "provided context. If the answer is not in the context, say so clearly. "
        "Be specific, cite page titles or URLs where relevant."
    )
    prompt = (
        "Context from discovery crawl (dataset: " + (ds_id or cid) + "):\n\n"
        + context_block
        + "\n\n---\nQuestion: " + question + "\n\nAnswer:"
    )

    answer = await _llm_generate(prompt, system=system, timeout=60.0)
    if not answer:
        answer = "(LLM unavailable -- ensure an Ollama node is reachable)"

    return {
        "ok": True,
        "answer": answer,
        "context_pages_used": len(page_rows[:max_context_pages]),
        "dataset_id": ds_id,
        "crawl_id": cid,
        "question": question,
    }


# ---------------------------------------------------------------------------
# Compile a structured document from discovery pages
# ---------------------------------------------------------------------------
@capability(
    "fabric.discover.compile",
    http_method="POST", http_path="/fabric/discover/compile",
    http_tags=["fabric", "discover", "synthesize"], memory="on",
    streams=["fabric.discover.compile.progress"],
    description=(
        "Compile a coherent multi-section document from crawled pages about a topic. "
        "The LLM clusters pages into subtopics then writes each section from relevant "
        "page content. Output is a Markdown document stored in the dataset and returned. "
        "Input: dataset_id (str!), topic (str, optional), max_pages (int=60), "
        "max_sections (int=8), style (str='report'|'wiki'|'guide'). "
        "Output: {document, title, sections, dataset_id, pages_used, stored_id}."
    ),
)
async def cap_discover_compile(
    dataset_id: str = "",
    topic: str = "",
    max_pages: int = 60,
    max_sections: int = 8,
    style: str = "report",
    trace_id=None,
) -> Dict:
    if not dataset_id.strip():
        return {"error": "dataset_id required"}
    ds_id = dataset_id.strip()
    _ensure_tables()
    conn = _sqlite_conn()

    async def _emit(stage: str, **kw):
        try:
            await emit_event({"type": "fabric.discover.compile.progress",
                              "stage": stage, "dataset_id": ds_id, **kw})
        except Exception:
            pass

    await _emit("start", message="Gathering pages...")

    try:
        rows = conn.execute(
            "SELECT url, title, full_text, tags, headings, word_count "
            "FROM fabric_pages WHERE dataset_id=? ORDER BY rowid LIMIT ?",
            (ds_id, max_pages)
        ).fetchall()
        pages = [dict(r) for r in rows]
    except Exception as e:
        return {"error": f"page load: {e}", "dataset_id": ds_id}

    if not pages:
        return {"error": "no pages in dataset -- crawl first", "dataset_id": ds_id}

    await _emit("pages_loaded", count=len(pages), message=f"Loaded {len(pages)} pages")

    if not topic:
        try:
            r = conn.execute(
                "SELECT label FROM fabric_crawl_history WHERE dataset_id=? LIMIT 1",
                (ds_id,)
            ).fetchone()
            topic = (r["label"] if r else "") or ds_id.replace("_", " ")
        except Exception:
            topic = ds_id.replace("_", " ")

    # Step 1: cluster page titles into sections
    titles_block = "\n".join(
        str(i + 1) + ". " + (p.get("title") or p.get("url") or "")[:100]
        for i, p in enumerate(pages[:max_pages])
    )
    _json_tmpl = '{"title":"...","sections":[{"heading":"...","page_indices":[1,2,3],"summary":"..."}]}'
    cluster_system = (
        "You are a document architect. Given a list of web page titles from a crawl, "
        "group them into " + str(min(max_sections, 8)) + " logical sections for a "
        + style + " document about: " + topic + ". "
        "Output ONLY valid JSON with this structure (no markdown fences, no commentary):\n"
        + _json_tmpl + "\n"
        "page_indices are 1-based. Each page should appear in exactly one section. "
        "Group thematically. Use clear, descriptive section headings."
    )
    cluster_prompt = "Pages to cluster:\n" + titles_block

    await _emit("clustering", message="Clustering pages into sections...")
    cluster_raw = await _llm_generate(cluster_prompt, system=cluster_system, timeout=45.0)

    sections_plan: List[Dict] = []
    doc_title = topic
    try:
        clean = re.sub(r"```(?:json)?", "", cluster_raw or "").strip().rstrip("`").strip()
        plan = json.loads(clean)
        doc_title = plan.get("title") or topic
        sections_plan = plan.get("sections") or []
    except Exception:
        chunk = max(1, len(pages) // max(1, max_sections))
        sections_plan = [
            {"heading": f"Part {i+1}",
             "page_indices": list(range(i * chunk + 1, min((i + 1) * chunk + 1, len(pages) + 1))),
             "summary": ""}
            for i in range(max_sections)
            if i * chunk < len(pages)
        ]

    await _emit("sections_planned", count=len(sections_plan),
                message=f"Planned {len(sections_plan)} sections")

    style_note = {
        "wiki":  "encyclopedic, neutral, factual Wikipedia-style prose",
        "guide": "practical step-by-step guide prose with tips and examples",
    }.get(style, "well-structured analytical report prose")

    doc_sections: List[str] = []
    for sec_idx, sec in enumerate(sections_plan[:max_sections]):
        heading   = sec.get("heading") or f"Section {sec_idx + 1}"
        indices   = [i - 1 for i in (sec.get("page_indices") or []) if 1 <= i <= len(pages)]
        sec_pages = [pages[i] for i in indices] if indices else pages[sec_idx::max_sections][:5]

        await _emit("writing_section", section=heading,
                    idx=sec_idx + 1, total=len(sections_plan),
                    message=f"Writing {sec_idx+1}/{len(sections_plan)}: {heading}")

        src_parts = []
        for sp in sec_pages[:8]:
            t   = (sp.get("title") or "").strip()
            txt = (sp.get("full_text") or "").strip()
            src_parts.append("[" + t + "]\n" + txt[:800])
        source_block = "\n\n---\n".join(src_parts)

        sec_system = (
            "You write " + style_note + ". "
            "Using ONLY the provided source content about '" + topic + "', "
            "write the section '" + heading + "'. "
            "Be informative and cohesive. Do not invent information not in the sources. "
            "Output the section body only (no heading, no markdown title). "
            "Aim for 150-400 words."
        )
        sec_prompt = "Source content:\n" + source_block + "\n\nWrite the '" + heading + "' section:"
        sec_body = await _llm_generate(sec_prompt, system=sec_system, timeout=60.0)
        if not sec_body:
            sec_body = "(Section content unavailable -- LLM did not respond)"

        doc_sections.append("## " + heading + "\n\n" + sec_body.strip())

    await _emit("assembling", message="Assembling document...")

    intro_system = (
        "You write a short, engaging introduction paragraph for a " + style + " document. "
        "Output only the intro paragraph, 2-4 sentences."
    )
    intro_prompt = (
        "Write a brief introduction for a " + style + " document titled '" + doc_title + "' "
        "covering " + str(len(doc_sections)) + " sections: "
        + ", ".join(s.get("heading", "") for s in sections_plan[:max_sections])
    )
    intro = await _llm_generate(intro_prompt, system=intro_system, timeout=30.0)

    document = "# " + doc_title + "\n\n"
    if intro:
        document += intro.strip() + "\n\n"
    document += "\n\n".join(doc_sections)
    document += "\n\n---\n*Compiled from " + str(len(pages)) + " pages crawled from dataset `" + ds_id + "`.*\n"

    stored_id = ""
    try:
        import uuid as _uuid
        stored_id = str(_uuid.uuid4())
        conn.execute(
            "INSERT OR REPLACE INTO fabric_records "
            "(id, dataset_id, url, kind, title, data, created_at) "
            "VALUES (?,?,?,?,?,?,datetime('now'))",
            (stored_id, ds_id, "compiled:" + ds_id, "document", doc_title, document)
        )
        conn.commit()
    except Exception as e:
        log.debug("compile store: %s", e)

    await _emit("done", message="Document compiled",
                title=doc_title, pages_used=len(pages), sections=len(doc_sections))

    return {
        "ok": True,
        "title": doc_title,
        "document": document,
        "sections": [s.get("heading", "") for s in sections_plan[:max_sections]],
        "pages_used": len(pages),
        "dataset_id": ds_id,
        "stored_id": stored_id,
        "style": style,
    }


# NOTE: The Discover+ UI is now served exclusively through the vera_graph
# sidebar panel (vera_graph_panel_discover.js) registered via vera_graph_panels.py.
# The old standalone HTML tab (mode="tab") has been removed to avoid duplication.
# The /ui/panels/discover-panel endpoint below still serves the standalone iframe
# page for backwards compatibility with any direct links.
log.info("fabric_discovery: Discover+ backend capabilities registered (UI via vera_graph sidebar)")

# ---------------------------------------------------------------------------
# Standalone page served into the fabric panel iframe
# ---------------------------------------------------------------------------
from fastapi.responses import HTMLResponse as _DiscHTMLResponse

@APP.get("/ui/panels/discover-panel", include_in_schema=False)
async def _discover_standalone_panel():
    """Discover+ as a full standalone page for the fabric panel iframe."""
    html = _PANEL_HTML or "<p style='color:red'>fabric_discovery_panel.html not found</p>"
    js   = _PANEL_JS  or ""
    _CSS_VARS = (
        ":root{--bg0:#181614;--bg1:#1f1d1a;--bg2:#272421;--bg3:#302c29;"
        "--border:#3a3530;--border2:#4a4540;--acc:#5a9e8f;--acc2:#8fb87a;"
        "--acc3:#c9955a;--acc4:#9e8fa0;--ok:#6db87a;--err:#c96b6b;--warn:#c9a35a;"
        "--text:#ddd5c8;--fg:#ddd5c8;--dim:#6a6058;--dim2:#8a7e70;"
        "--mono:'JetBrains Mono','Fira Mono',monospace;"
        "--sans:'Inter','IBM Plex Sans',system-ui,sans-serif;"
        "--radius:5px;color-scheme:dark}"
        "*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}"
        "html,body{height:100%;background:var(--bg0);color:var(--text);"
        "font-family:var(--sans);font-size:13px;overflow:hidden}"
    )
    _HELPERS = """
var API = '';
window.API = API;
function esc(s) {
    return String(s || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}
async function api(path, method, body, timeout) {
    try {
        var o = { method: method || (body ? 'POST' : 'GET'),
                  headers: { 'Content-Type': 'application/json' } };
        if (body) o.body = JSON.stringify(body);
        var r = await fetch(API + path, o);
        return await r.json();
    } catch(e) { return null; }
}
"""
    page = (
        "<!DOCTYPE html><html lang='en'><head>"
        "<meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1.0'>"
        "<title>Discover+</title>"
        f"<style>{_CSS_VARS}"
        "html,body,#vg-host{width:100%;height:100%;margin:0;padding:0;overflow:hidden}"
        "#vg-host{display:flex;flex-direction:row}"
        "</style>"
        "</head><body>"
        "<div id='vg-host'></div>"
        f"<script>{_HELPERS}</script>"
        "<script src='/ui/vera-graph.js'></script>"
        "<script src='/ui/vera-graph-panel-loom.js'></script>"
        "<script src='/ui/vera-graph-panel-worldview.js'></script>"
        "<script src='/ui/vera-graph-panel-discover.js'></script>"
        "<script>"
        "window.addEventListener('DOMContentLoaded',function(){"
        "  if(!window.veraUI||!window.veraUI.Graph)return;"
        "  var host=document.getElementById('vg-host');"
        "  veraUI.Graph.create(host,{"
        "    height:'fill',"
        "    showSearch:true,showLegend:false,showLayerToggle:false,"
        "    filtersOnly:true,showRelevance:true,"
        "    actionsEnabled:true,apiBase:'',"
        "    autoOpenTerminal:false,"
        "    bottomDrawerHeight:180,"
        "    defaultPanel:'discover'"
        "  });"
        "});"
        "</script>"
        "</body></html>"
    )
    return _DiscHTMLResponse(page)