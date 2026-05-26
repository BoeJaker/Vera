"""
vera_memory.py  —  Hybrid Memory System for Vera
=================================================
Architecture
------------
  Redis         →  hot bus / real-time event stream (ephemeral)
  PostgreSQL    →  immutable append-only archive (source of truth)
  ChromaDB      →  vector store (semantic similarity search)
  Neo4j         →  knowledge graph (relational / traversal queries)

All four work together as a "hybrid memory" layer:
  • Every memory record is written to Postgres first (immutable log)
  • Postgres then fans out to Chroma (vector) and Neo4j (graph) asynchronously
  • Redis events trigger automatic memory promotion via the promotion loop
  • Queries can span all stores and merge results

Pluggable Backend System
------------------------
Any class that inherits MemoryBackend and implements its interface
can be dropped in.  Three reference implementations are provided:
  ChromaBackend    — vector similarity search
  Neo4jBackend     — graph traversal, entity relationships
  PostgresBackend  — immutable relational archive, full-text search

Memory Record Schema
--------------------
Every record carries (see MemoryRecord dataclass for full docstring):
  Core identity   : id, session_id, trace_id, created_at, updated_at
  Classification  : record_type, source_type, category, tags, keywords
  Content         : text, summary, full_text, metadata
  Provenance      : human_text (bool), ai_output (bool), model, capability
  Spatial context : embedding (vector), graph_id, relations
  Status          : importance (0-1), archived, ttl_seconds

Promotion from Redis
--------------------
The MemoryPromoter listens to vera:events on Redis.
Any event with type prefixed "memory." or containing a "memory" key
is automatically promoted to the hybrid store.
LLM outputs are auto-promoted if they exceed a quality threshold.

Usage
-----
    import vera_memory       # side-effect: registers all @capability decorators

All capabilities are then available via MCP / REST:
    memory.store            — store a record
    memory.search           — semantic + keyword hybrid search
    memory.get              — fetch by id
    memory.relate           — create a graph relationship
    memory.traverse         — graph traversal from a node
    memory.promote          — manually promote a Redis event
    memory.stats            — store statistics
    memory.backends         — list active backends
    memory.recall           — context-aware recall (combines vector + graph)
    memory.forget           — soft-delete (archived=True, never actually deleted)
    memory.session_history  — all records for a session
    memory.similar          — top-N similar records to a query
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
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


def _to_dt(s) -> datetime:
    """Convert an ISO-8601 string to a timezone-aware datetime for asyncpg."""
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        # Handle trailing Z
        clean = str(s).replace('Z', '+00:00')
        dt = datetime.fromisoformat(clean)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)
from typing import Any, Dict, List, Optional, Tuple

import httpx

import Vera.Orchestration.capability_orchestration as _orch

from Vera.Orchestration.config import cfg
from Vera.Orchestration.capability_orchestration import (
    APP,            # noqa
    capability, emit_event, now_iso, ollama_generate, schedule,
)

log = logging.getLogger("vera.memory")

# Reference REDIS lazily through the orchestrator module so we always
# get the live connection object even if it was None at import time.
def _redis():
    return _orch.REDIS

# ── Optional backends ────────────────────────────────────────────────────────
try:
    import chromadb as _chromadb
    HAS_CHROMA = True
except ImportError:
    _chromadb = None; HAS_CHROMA = False

try:
    import asyncpg as _asyncpg
    HAS_PG = True
except ImportError:
    _asyncpg = None; HAS_PG = False

try:
    from neo4j import AsyncGraphDatabase as _Neo4j
    HAS_NEO = True
except ImportError:
    _Neo4j = None; HAS_NEO = False

# ── Config ────────────────────────────────────────────────────────────────────
POSTGRES_URL = cfg.POSTGRES_URL
CHROMA_HOST = cfg.CHROMA_HOST
CHROMA_PORT = cfg.CHROMA_PORT
CHROMA_COLLECTION   = os.getenv("CHROMA_COLLECTION",   "vera_memory")
NEO4J_URI = cfg.NEO4J_URI
NEO4J_USER = cfg.NEO4J_USER
NEO4J_PASSWORD = cfg.NEO4J_PASS
OLLAMA_EMBED_URL = cfg.OLLAMA_EMBED_URL
OLLAMA_EMBED_MODEL = cfg.OLLAMA_EMBED_MODEL
MEMORY_AUTO_EMBED   = os.getenv("MEMORY_AUTO_EMBED",   "1") == "1"
MEMORY_PROMO_STREAM = "vera:events"
MEMORY_EVENT_TYPES  = {"memory.store", "memory.promote", "llm.generate", "cap.ok"}

# ─────────────────────────────────────────────────────────────────────────────
# MEMORY RECORD  —  the universal schema
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MemoryRecord:
    """
    Universal memory record.  Every field that could be useful for retrieval,
    provenance, auditing, or graph navigation is included.

    Identity
    --------
    id          : globally unique UUID for this record
    session_id  : which conversation/session produced this
    trace_id    : which specific capability call produced this
    parent_id   : id of the record this was derived from (for versioning)

    Timestamps
    ----------
    created_at  : ISO-8601 UTC when first stored (immutable)
    updated_at  : ISO-8601 UTC of last update (archiving, tagging, etc.)
    ttl_seconds : if set, backends should expire after this many seconds

    Classification
    --------------
    record_type : "message" | "fact" | "event" | "summary" | "entity" |
                  "relationship" | "skill_output" | "observation" | "plan" |
                  "code" | "error" | "feedback" | "preference"
    source_type : "human" | "ai" | "tool" | "system" | "sensor" | "document"
    category    : free-form domain category e.g. "medical", "code", "personal"
    tags        : list of short string tags for filtering
    keywords    : extracted key terms for full-text / BM25 search
    importance  : float 0.0–1.0 (higher = more likely to be retrieved)
    archived    : soft-delete flag — records are NEVER physically deleted
    language    : ISO 639-1 language code e.g. "en", "fr"

    Content
    -------
    text        : the primary searchable text (short form if long)
    summary     : one-sentence summary generated by LLM or provided
    full_text   : complete original text (may be large)
    metadata    : arbitrary JSON dict for backend-specific extras

    Provenance
    ----------
    human_text  : True if the content was authored by a human
    ai_output   : True if the content was generated by an AI model
    model       : which model generated this (if ai_output)
    capability  : which Vera capability produced this
    source_url  : URL or file path this was extracted from

    Vector / Graph
    --------------
    embedding       : float list (produced by embedding model)
    embedding_model : which model produced the embedding
    graph_id        : Neo4j node ID if this record has a graph node
    relations       : list of {"type":str, "target_id":str, "properties":{}}
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    id:             str     = field(default_factory=lambda: str(uuid.uuid4()))
    session_id:     str     = ""
    trace_id:       str     = ""
    parent_id:      str     = ""

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at:     str     = field(default_factory=now_iso)
    updated_at:     str     = field(default_factory=now_iso)
    ttl_seconds:    int     = 0

    # ── Classification ────────────────────────────────────────────────────────
    record_type:    str     = "message"         # see docstring
    source_type:    str     = "human"           # human | ai | tool | system
    category:       str     = "general"
    tags:           List[str] = field(default_factory=list)
    keywords:       List[str] = field(default_factory=list)
    importance:     float   = 0.5
    archived:       bool    = False
    language:       str     = "en"

    # ── Content ───────────────────────────────────────────────────────────────
    text:           str     = ""
    summary:        str     = ""
    full_text:      str     = ""
    metadata:       Dict    = field(default_factory=dict)

    # ── Provenance ────────────────────────────────────────────────────────────
    human_text:     bool    = True
    ai_output:      bool    = False
    model:          str     = ""
    capability:     str     = ""
    source_url:     str     = ""

    # ── Vector / Graph ────────────────────────────────────────────────────────
    embedding:          List[float] = field(default_factory=list)
    embedding_model:    str         = ""
    graph_id:           str         = ""
    relations:          List[Dict]  = field(default_factory=list)

    # ── Content hash (for deduplication) ─────────────────────────────────────
    content_hash:   str     = ""

    def compute_hash(self) -> str:
        return hashlib.sha256(self.full_text.encode() or self.text.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["embedding"] = []  # don't serialise vectors to JSON (too large)
        return d

    def to_chroma_doc(self) -> Tuple[str, str, dict]:
        """Returns (id, document_text, metadata_dict) for ChromaDB."""
        meta = {
            "session_id":   self.session_id,
            "trace_id":     self.trace_id,
            "record_type":  self.record_type,
            "source_type":  self.source_type,
            "category":     self.category,
            "tags":         json.dumps(self.tags),
            "keywords":     json.dumps(self.keywords),
            "importance":   self.importance,
            "archived":     self.archived,
            "human_text":   self.human_text,
            "ai_output":    self.ai_output,
            "model":        self.model,
            "capability":   self.capability,
            "language":     self.language,
            "created_at":   self.created_at,
            "updated_at":   self.updated_at,
            "content_hash": self.content_hash,
            "summary":      self.summary[:500],
            "parent_id":    self.parent_id,
        }
        doc_text = self.text or self.summary or self.full_text[:500]
        return self.id, doc_text, meta


def _extract_keywords(text: str) -> List[str]:
    """Simple keyword extraction — stopword filter + frequency."""
    STOP = {
        "the","a","an","and","or","but","in","on","at","to","for","of","with",
        "is","are","was","were","be","been","being","have","has","had","do",
        "does","did","will","would","could","should","may","might","this",
        "that","these","those","i","you","he","she","it","we","they","my",
        "your","his","her","its","our","their","what","which","who","how",
    }
    words = re.findall(r'\b[a-zA-Z][a-zA-Z0-9_\-]{2,}\b', text.lower())
    freq: Dict[str,int] = {}
    for w in words:
        if w not in STOP:
            freq[w] = freq.get(w, 0) + 1
    return [w for w,_ in sorted(freq.items(), key=lambda x:-x[1])[:20]]


# ─────────────────────────────────────────────────────────────────────────────
# BACKEND ABSTRACT BASE
# ─────────────────────────────────────────────────────────────────────────────

class MemoryBackend(ABC):
    """
    Pluggable backend interface.  Implement all abstract methods to add a
    new storage technology.  Backends are registered via MEMORY_BACKENDS.

    The HybridMemoryStore calls all active backends for every write operation
    and fans out queries across all backends, merging results.
    """

    name: str = "base"

    @abstractmethod
    async def connect(self) -> bool:
        """Establish connection.  Return True on success."""

    @abstractmethod
    async def disconnect(self):
        """Close connections cleanly."""

    @abstractmethod
    async def store(self, record: MemoryRecord) -> bool:
        """Persist a record.  Return True on success."""

    @abstractmethod
    async def get(self, record_id: str) -> Optional[MemoryRecord]:
        """Retrieve a record by ID.  Return None if not found."""

    @abstractmethod
    async def search(
        self,
        query: str,
        limit: int = 10,
        filters: Optional[Dict] = None,
        embedding: Optional[List[float]] = None,
    ) -> List[Tuple[MemoryRecord, float]]:
        """
        Search for records matching query.
        Returns list of (record, relevance_score) sorted by score desc.
        filters: dict of field→value for exact-match pre-filtering.
        embedding: if provided, use vector similarity instead of / in addition to text.
        """

    @abstractmethod
    async def update(self, record_id: str, updates: Dict) -> bool:
        """Partial update of a record's fields."""

    @abstractmethod
    async def stats(self) -> Dict:
        """Return backend statistics (record count, index size, etc.)."""

    # Optional — backends can return None to indicate not supported
    async def relate(
        self,
        from_id: str,
        to_id: str,
        relation_type: str,
        properties: Optional[Dict] = None,
    ) -> bool:
        return False

    async def traverse(
        self,
        start_id: str,
        relation_types: Optional[List[str]] = None,
        depth: int = 2,
        limit: int = 20,
    ) -> List[Dict]:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# POSTGRES BACKEND  —  immutable append-only archive
# ─────────────────────────────────────────────────────────────────────────────

class PostgresBackend(MemoryBackend):
    """
    PostgreSQL as the source-of-truth immutable archive.
    Records are never deleted — archiving sets the archived flag.
    Full-text search via pg_trgm or tsvector.
    Relations stored in a separate edges table.
    """
    name = "postgres"

    def __init__(self):
        self._pool = None

    async def connect(self) -> bool:
        if not HAS_PG:
            log.warning("asyncpg not installed — PostgresBackend unavailable")
            return False
        try:
            self._pool = await _asyncpg.create_pool(POSTGRES_URL, min_size=2, max_size=10)
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS vera_memories (
                        id              TEXT PRIMARY KEY,
                        session_id      TEXT NOT NULL DEFAULT '',
                        trace_id        TEXT NOT NULL DEFAULT '',
                        parent_id       TEXT NOT NULL DEFAULT '',
                        created_at      TIMESTAMPTZ NOT NULL,
                        updated_at      TIMESTAMPTZ NOT NULL,
                        ttl_seconds     INT NOT NULL DEFAULT 0,
                        record_type     TEXT NOT NULL DEFAULT 'message',
                        source_type     TEXT NOT NULL DEFAULT 'human',
                        category        TEXT NOT NULL DEFAULT 'general',
                        tags            JSONB NOT NULL DEFAULT '[]',
                        keywords        JSONB NOT NULL DEFAULT '[]',
                        importance      FLOAT NOT NULL DEFAULT 0.5,
                        archived        BOOLEAN NOT NULL DEFAULT FALSE,
                        language        TEXT NOT NULL DEFAULT 'en',
                        text            TEXT NOT NULL DEFAULT '',
                        summary         TEXT NOT NULL DEFAULT '',
                        full_text       TEXT NOT NULL DEFAULT '',
                        metadata        JSONB NOT NULL DEFAULT '{}',
                        human_text      BOOLEAN NOT NULL DEFAULT TRUE,
                        ai_output       BOOLEAN NOT NULL DEFAULT FALSE,
                        model           TEXT NOT NULL DEFAULT '',
                        capability      TEXT NOT NULL DEFAULT '',
                        source_url      TEXT NOT NULL DEFAULT '',
                        embedding_model TEXT NOT NULL DEFAULT '',
                        graph_id        TEXT NOT NULL DEFAULT '',
                        content_hash    TEXT NOT NULL DEFAULT '',
                        search_vector   tsvector GENERATED ALWAYS AS (
                            setweight(to_tsvector('english', coalesce(text, '')), 'A') ||
                            setweight(to_tsvector('english', coalesce(summary, '')), 'B') ||
                            setweight(to_tsvector('english', coalesce(full_text, '')), 'C')
                        ) STORED
                    )
                """)
                # Create edge table with composite PK (new deployments)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS vera_memory_edges (
                        from_id      TEXT NOT NULL,
                        to_id        TEXT NOT NULL,
                        relation     TEXT NOT NULL,
                        properties   JSONB NOT NULL DEFAULT '{}',
                        session_id   TEXT NOT NULL DEFAULT '',
                        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (from_id, to_id, relation)
                    )
                """)
                # ── Migration for existing deployments ─────────────────────────────
                # Step 1: Add session_id column if missing
                try:
                    await conn.execute(
                        "ALTER TABLE vera_memory_edges "
                        "ADD COLUMN IF NOT EXISTS session_id TEXT NOT NULL DEFAULT ''"
                    )
                except Exception:
                    pass
                # Step 2: Remove duplicate rows before creating unique index
                # (duplicates prevent CREATE UNIQUE INDEX from succeeding)
                try:
                    await conn.execute("""
                        DELETE FROM vera_memory_edges a USING vera_memory_edges b
                        WHERE a.ctid > b.ctid
                          AND a.from_id = b.from_id
                          AND a.to_id   = b.to_id
                          AND a.relation = b.relation
                    """)
                except Exception:
                    pass
                # Step 3: Drop old UUID primary key and FK constraints if present
                try:
                    rows = await conn.fetch("""
                        SELECT constraint_name, constraint_type
                        FROM information_schema.table_constraints
                        WHERE table_name='vera_memory_edges'
                          AND constraint_type IN ('FOREIGN KEY','PRIMARY KEY')
                    """)
                    for row in rows:
                        # Don't drop the composite PK we want to keep
                        if 'pkey' in row['constraint_name'] or 'fkey' in row['constraint_name']:
                            try:
                                await conn.execute(
                                    f'ALTER TABLE vera_memory_edges '
                                    f'DROP CONSTRAINT IF EXISTS "{row["constraint_name"]}"'
                                )
                            except Exception:
                                pass
                except Exception:
                    pass
                # Step 4: Create unique index (allows ON CONFLICT col-spec to work)
                try:
                    await conn.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS vme_unique "
                        "ON vera_memory_edges(from_id, to_id, relation)"
                    )
                except Exception as e:
                    log.warning("vme_unique index creation failed (duplicates?): %s", e)
                await conn.execute("CREATE INDEX IF NOT EXISTS vm_session ON vera_memories(session_id)")
                await conn.execute("CREATE INDEX IF NOT EXISTS vm_type ON vera_memories(record_type)")
                await conn.execute("CREATE INDEX IF NOT EXISTS vm_category ON vera_memories(category)")
                await conn.execute("CREATE INDEX IF NOT EXISTS vm_created ON vera_memories(created_at DESC)")
                await conn.execute("CREATE INDEX IF NOT EXISTS vm_fts ON vera_memories USING gin(search_vector)")
                await conn.execute("CREATE INDEX IF NOT EXISTS vm_tags ON vera_memories USING gin(tags)")
                await conn.execute("CREATE INDEX IF NOT EXISTS vm_hash ON vera_memories(content_hash)")
                await conn.execute("CREATE INDEX IF NOT EXISTS vme_from ON vera_memory_edges(from_id)")
                await conn.execute("CREATE INDEX IF NOT EXISTS vme_to   ON vera_memory_edges(to_id)")
                await conn.execute("CREATE INDEX IF NOT EXISTS vme_sess ON vera_memory_edges(session_id)")
            log.info("✓ PostgresBackend connected")
            return True
        except Exception as e:
            log.error("PostgresBackend connect: %s", e)
            return False

    async def disconnect(self):
        if self._pool:
            await self._pool.close()

    async def store(self, record: MemoryRecord) -> bool:
        if not self._pool: return False
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO vera_memories (
                        id, session_id, trace_id, parent_id, created_at, updated_at,
                        ttl_seconds, record_type, source_type, category, tags, keywords,
                        importance, archived, language, text, summary, full_text, metadata,
                        human_text, ai_output, model, capability, source_url,
                        embedding_model, graph_id, content_hash
                    ) VALUES (
                        $1,$2,$3,$4,$5::TIMESTAMPTZ,$6::TIMESTAMPTZ,
                        $7,$8,$9,$10,$11::JSONB,$12::JSONB,
                        $13,$14,$15,$16,$17,$18,$19::JSONB,
                        $20,$21,$22,$23,$24,$25,$26,$27
                    ) ON CONFLICT (id) DO UPDATE SET
                        updated_at=EXCLUDED.updated_at,
                        importance=EXCLUDED.importance,
                        archived=EXCLUDED.archived,
                        summary=EXCLUDED.summary,
                        tags=EXCLUDED.tags,
                        keywords=EXCLUDED.keywords,
                        graph_id=EXCLUDED.graph_id,
                        metadata=EXCLUDED.metadata
                """,
                record.id, record.session_id, record.trace_id, record.parent_id,
                _to_dt(record.created_at), _to_dt(record.updated_at),
                record.ttl_seconds, record.record_type, record.source_type,
                record.category,
                json.dumps(record.tags), json.dumps(record.keywords),
                record.importance, record.archived, record.language,
                record.text, record.summary, record.full_text,
                json.dumps(record.metadata),
                record.human_text, record.ai_output,
                record.model, record.capability, record.source_url,
                record.embedding_model, record.graph_id, record.content_hash,
                )
            return True
        except Exception as e:
            log.error("PG store: %s", e)
            return False

    async def get(self, record_id: str) -> Optional[MemoryRecord]:
        if not self._pool: return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM vera_memories WHERE id=$1", record_id
                )
            if not row: return None
            return self._row_to_record(row)
        except Exception as e:
            log.error("PG get: %s", e); return None

    async def search(self, query: str, limit: int = 10, filters: Optional[Dict] = None,
                     embedding: Optional[List[float]] = None) -> List[Tuple[MemoryRecord, float]]:
        if not self._pool: return []
        try:
            conditions = ["archived = FALSE"]
            params: list = []
            i = 1
            if filters:
                for k, v in filters.items():
                    if k in ("session_id","record_type","source_type","category","language","model","capability"):
                        conditions.append(f"{k} = ${i}")
                        params.append(v); i += 1
                    elif k == "tags":
                        conditions.append(f"tags @> ${i}::JSONB")
                        params.append(json.dumps([v] if isinstance(v, str) else v)); i += 1
            where = " AND ".join(conditions)
            if query.strip():
                params.append(query)
                sql = f"""
                    SELECT *, ts_rank(search_vector, plainto_tsquery('english', ${i})) AS score
                    FROM vera_memories
                    WHERE {where}
                      AND search_vector @@ plainto_tsquery('english', ${i})
                    ORDER BY score DESC, importance DESC
                    LIMIT {limit}
                """
            else:
                sql = f"""
                    SELECT *, importance AS score FROM vera_memories
                    WHERE {where}
                    ORDER BY created_at DESC
                    LIMIT {limit}
                """
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, *params)
            return [(self._row_to_record(r), float(r.get("score", 0.5))) for r in rows]
        except Exception as e:
            log.error("PG search: %s", e); return []

    async def update(self, record_id: str, updates: Dict) -> bool:
        if not self._pool: return False
        allowed = {"summary","tags","keywords","importance","archived","graph_id","metadata","updated_at"}
        sets, params, i = [], [], 1
        for k, v in updates.items():
            if k not in allowed: continue
            sets.append(f"{k}=${i}")
            params.append(json.dumps(v) if isinstance(v, (list, dict)) else v)
            i += 1
        if not sets: return False
        params.append(record_id)
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE vera_memories SET {','.join(sets)} WHERE id=${i}", *params
                )
            return True
        except Exception as e:
            log.error("PG update: %s", e); return False

    async def relate(self, from_id: str, to_id: str, relation_type: str,
                     properties: Optional[Dict] = None) -> bool:
        if not self._pool: return False
        props = properties or {}
        sid   = props.get("session_id", "")
        # Try with ON CONFLICT col-spec first (works when unique index exists)
        for sql in [
            """INSERT INTO vera_memory_edges(from_id,to_id,relation,properties,session_id,created_at)
               VALUES($1,$2,$3,$4::JSONB,$5,NOW())
               ON CONFLICT (from_id,to_id,relation) DO UPDATE
                   SET properties=EXCLUDED.properties,
                       session_id=CASE WHEN EXCLUDED.session_id!='' THEN EXCLUDED.session_id
                                       ELSE vera_memory_edges.session_id END""",
            """INSERT INTO vera_memory_edges(from_id,to_id,relation,properties,session_id,created_at)
               VALUES($1,$2,$3,$4::JSONB,$5,NOW())
               ON CONFLICT DO NOTHING""",
        ]:
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(sql, from_id, to_id, relation_type,
                                       json.dumps(props), sid)
                return True
            except Exception as e:
                if "unique" in str(e).lower() or "constraint" in str(e).lower():
                    continue  # try next variant
                log.error("PG relate: %s", e)
                return False
        return False

    async def session_edges(self, session_id: str, limit: int = 200) -> List[Dict]:
        """Return all edges for nodes belonging to this session."""
        if not self._pool: return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT DISTINCT e.from_id, e.to_id, e.relation
                    FROM vera_memory_edges e
                    WHERE e.session_id = $1
                       OR e.from_id IN (SELECT id FROM vera_memories WHERE session_id=$1)
                       OR e.to_id   IN (SELECT id FROM vera_memories WHERE session_id=$1)
                    LIMIT $2
                """, session_id, limit)
            return [{"from_id":r["from_id"],"to_id":r["to_id"],"relation":r["relation"]} for r in rows]
        except Exception as e:
            log.error("PG session_edges: %s", e); return []
    async def traverse(self, start_id: str, relation_types: Optional[List[str]] = None,
                       depth: int = 2, limit: int = 20) -> List[Dict]:
        if not self._pool: return []
        try:
            cond = ""
            params: list = [start_id]
            if relation_types:
                cond = "AND e.relation = ANY($2)"
                params.append(relation_types)
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(f"""
                    SELECT e.to_id, e.relation, e.properties,
                           m.text, m.record_type, m.category
                    FROM vera_memory_edges e
                    LEFT JOIN vera_memories m ON m.id = e.to_id
                    WHERE e.from_id = $1 {cond}
                    LIMIT {limit}
                """, *params)
            return [dict(r) for r in rows]
        except Exception as e:
            log.error("PG traverse: %s", e); return []

    async def stats(self) -> Dict:
        if not self._pool: return {"connected": False}
        try:
            async with self._pool.acquire() as conn:
                total   = await conn.fetchval("SELECT COUNT(*) FROM vera_memories")
                active  = await conn.fetchval("SELECT COUNT(*) FROM vera_memories WHERE archived=FALSE")
                by_type = await conn.fetch("SELECT record_type, COUNT(*) n FROM vera_memories GROUP BY record_type")
                edges   = await conn.fetchval("SELECT COUNT(*) FROM vera_memory_edges")
            return {"connected":True,"total":total,"active":active,"edges":edges,
                    "by_type":{r["record_type"]:r["n"] for r in by_type}}
        except Exception as e:
            return {"connected":False,"error":str(e)}

    @staticmethod
    def _row_to_record(row) -> MemoryRecord:
        d = dict(row)
        d.pop("search_vector", None); d.pop("score", None)
        for jf in ("tags","keywords","metadata"):
            if isinstance(d.get(jf), str):
                try: d[jf] = json.loads(d[jf])
                except: d[jf] = [] if jf != "metadata" else {}
        # Convert asyncpg date types to ISO strings
        for tf in ("created_at","updated_at"):
            if hasattr(d.get(tf), "isoformat"):
                d[tf] = d[tf].isoformat() + "Z"
        # Fill fields MemoryRecord has but DB doesn't (embedding, relations, graph_id)
        d.setdefault("embedding", [])
        d.setdefault("relations", [])
        d.setdefault("graph_id",  "")
        return MemoryRecord(**{k: v for k, v in d.items() if k in MemoryRecord.__dataclass_fields__})


# ─────────────────────────────────────────────────────────────────────────────
# CHROMA BACKEND  —  vector store
# ─────────────────────────────────────────────────────────────────────────────

class ChromaBackend(MemoryBackend):
    """
    ChromaDB for semantic vector search.
    Embeddings are generated via Ollama (nomic-embed-text or similar).
    Uses ChromaDB's native cosine similarity.
    """
    name = "chroma"

    def __init__(self):
        self._client     = None
        self._collection = None

    async def connect(self) -> bool:
        if not HAS_CHROMA:
            log.warning("chromadb not installed — ChromaBackend unavailable")
            return False
        try:
            self._client = _chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
            self._collection = self._client.get_or_create_collection(
                name=CHROMA_COLLECTION,
                metadata={"hnsw:space": "cosine"},
            )
            count = self._collection.count()
            log.info("✓ ChromaBackend connected — %d documents", count)
            return True
        except Exception as e:
            log.error("ChromaBackend connect: %s", e)
            return False

    async def disconnect(self):
        pass  # ChromaDB HTTP client is stateless

    async def store(self, record: MemoryRecord) -> bool:
        if not self._collection: return False
        try:
            rid, doc, meta = record.to_chroma_doc()
            if record.embedding:
                self._collection.upsert(
                    ids=[rid], documents=[doc], metadatas=[meta],
                    embeddings=[record.embedding],
                )
            else:
                self._collection.upsert(ids=[rid], documents=[doc], metadatas=[meta])
            return True
        except Exception as e:
            log.error("Chroma store: %s", e); return False

    async def get(self, record_id: str) -> Optional[MemoryRecord]:
        if not self._collection: return None
        try:
            res = self._collection.get(ids=[record_id], include=["documents","metadatas"])
            if not res["ids"]: return None
            return self._chroma_to_record(
                res["ids"][0], res["documents"][0], res["metadatas"][0]
            )
        except Exception as e:
            log.error("Chroma get: %s", e); return None

    async def search(self, query: str, limit: int = 10, filters: Optional[Dict] = None,
                     embedding: Optional[List[float]] = None) -> List[Tuple[MemoryRecord, float]]:
        if not self._collection: return []
        try:
            # Build individual filter clauses
            clauses: list = [{"archived": {"$eq": False}}]
            if filters:
                for k, v in (filters or {}).items():
                    if k in ("session_id","record_type","source_type","category","language"):
                        clauses.append({k: {"$eq": v}})

            # Chroma requires $and when there are multiple conditions;
            # a single-element list must be unwrapped to avoid the
            # "Expected where to have exactly one operator" error.
            if len(clauses) == 0:
                where_arg = None
            elif len(clauses) == 1:
                where_arg = clauses[0]
            else:
                where_arg = {"$and": clauses}

            kwargs: dict = {
                "n_results": min(int(limit), self._collection.count() or 1),
                "where":     where_arg,
                "include":   ["documents","metadatas","distances"],
            }
            if embedding:
                kwargs["query_embeddings"] = [embedding]
            else:
                kwargs["query_texts"] = [query] if query.strip() else [""]
            res = self._collection.query(**{k:v for k,v in kwargs.items() if v is not None})
            results = []
            for rid, doc, meta, dist in zip(
                res["ids"][0], res["documents"][0],
                res["metadatas"][0], res["distances"][0],
            ):
                score = max(0.0, 1.0 - dist)  # cosine distance → similarity
                results.append((self._chroma_to_record(rid, doc, meta), score))
            return results
        except Exception as e:
            log.error("Chroma search: %s", e); return []

    async def update(self, record_id: str, updates: Dict) -> bool:
        if not self._collection: return False
        try:
            current = await self.get(record_id)
            if not current: return False
            for k, v in updates.items():
                if hasattr(current, k): setattr(current, k, v)
            return await self.store(current)
        except Exception as e:
            log.error("Chroma update: %s", e); return False

    async def stats(self) -> Dict:
        if not self._collection:
            return {"connected": False}
        try:
            return {"connected": True, "collection": CHROMA_COLLECTION,
                    "count": self._collection.count()}
        except Exception as e:
            return {"connected": False, "error": str(e)}

    @staticmethod
    def _chroma_to_record(rid: str, doc: str, meta: dict) -> MemoryRecord:
        for jf in ("tags","keywords","relations"):
            if isinstance(meta.get(jf), str):
                try: meta[jf] = json.loads(meta[jf])
                except: meta[jf] = []
        return MemoryRecord(
            id=rid, text=doc,
            session_id=meta.get("session_id",""),
            trace_id=meta.get("trace_id",""),
            record_type=meta.get("record_type","message"),
            source_type=meta.get("source_type","human"),
            category=meta.get("category","general"),
            tags=meta.get("tags",[]),
            keywords=meta.get("keywords",[]),
            importance=meta.get("importance",0.5),
            archived=meta.get("archived",False),
            human_text=meta.get("human_text",True),
            ai_output=meta.get("ai_output",False),
            model=meta.get("model",""),
            capability=meta.get("capability",""),
            language=meta.get("language","en"),
            created_at=meta.get("created_at",now_iso()),
            updated_at=meta.get("updated_at",now_iso()),
            summary=meta.get("summary",""),
            content_hash=meta.get("content_hash",""),
            parent_id=meta.get("parent_id",""),
        )


# ─────────────────────────────────────────────────────────────────────────────
# NEO4J BACKEND  —  knowledge graph
# ─────────────────────────────────────────────────────────────────────────────

class Neo4jBackend(MemoryBackend):
    """
    Neo4j for graph-based memory storage and traversal.
    Each MemoryRecord becomes a :Memory node.
    Relations become graph edges with full properties.
    Enables: entity linking, concept graphs, conversation threads,
             semantic networks, temporal chains.
    """
    name = "neo4j"

    def __init__(self):
        self._driver = None

    async def connect(self) -> bool:
        if not HAS_NEO:
            log.warning("neo4j driver not installed — Neo4jBackend unavailable")
            return False
        try:
            self._driver = _Neo4j.driver(
                NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)
            )
            async with self._driver.session() as s:
                await s.run("RETURN 1")
                # Constraints and indexes
                # Apply constraints + indexes — try Neo4j 5.x syntax first, fall back to 4.x
                _neo4j_stmts_v5 = [
                    "CREATE CONSTRAINT vera_mem_id IF NOT EXISTS FOR (m:Memory) REQUIRE m.id IS UNIQUE",
                    "CREATE INDEX vera_mem_session IF NOT EXISTS FOR (m:Memory) ON (m.session_id)",
                    "CREATE INDEX vera_mem_type    IF NOT EXISTS FOR (m:Memory) ON (m.record_type)",
                    "CREATE INDEX vera_mem_cat     IF NOT EXISTS FOR (m:Memory) ON (m.category)",
                    "CREATE INDEX vera_mem_created IF NOT EXISTS FOR (m:Memory) ON (m.created_at)",
                    "CREATE FULLTEXT INDEX vera_mem_text IF NOT EXISTS FOR (m:Memory) ON EACH [m.text, m.summary, m.full_text]",
                ]
                _neo4j_stmts_v4 = [
                    "CREATE CONSTRAINT ON (m:Memory) ASSERT m.id IS UNIQUE",
                    "CREATE INDEX ON :Memory(session_id)",
                    "CREATE INDEX ON :Memory(record_type)",
                    "CREATE INDEX ON :Memory(category)",
                    "CREATE INDEX ON :Memory(created_at)",
                ]
                # Try v5 syntax; if the first stmt fails with syntax error, fall back to v4
                try:
                    for stmt in _neo4j_stmts_v5:
                        try: await s.run(stmt)
                        except Exception: pass
                except Exception:
                    for stmt in _neo4j_stmts_v4:
                        try: await s.run(stmt)
                        except Exception: pass
                # Session node constraint
                try:
                    await s.run("CREATE CONSTRAINT vera_sess_id IF NOT EXISTS FOR (s:Session) REQUIRE s.session_id IS UNIQUE")
                except Exception:
                    try: await s.run("CREATE CONSTRAINT ON (s:Session) ASSERT s.session_id IS UNIQUE")
                    except Exception: pass
            log.info("✓ Neo4jBackend connected")
            asyncio.ensure_future(emit_event({
                "type": "backend.connected", "backend": "neo4j", "uri": NEO4J_URI}))
            return True
        except Exception as e:
            log.error("Neo4jBackend connect FAILED: %s", e)
            asyncio.ensure_future(emit_event({
                "type": "backend.error", "backend": "neo4j",
                "error": str(e), "uri": NEO4J_URI}))
            return False

    async def disconnect(self):
        if self._driver: await self._driver.close()

    async def store(self, record: MemoryRecord) -> bool:
        if not self._driver: return False
        try:
            props = {
                "id":           record.id,
                "session_id":   record.session_id,
                "trace_id":     record.trace_id,
                "parent_id":    record.parent_id,
                "created_at":   record.created_at,
                "updated_at":   record.updated_at,
                "record_type":  record.record_type,
                "source_type":  record.source_type,
                "category":     record.category,
                "tags":         record.tags,
                "keywords":     record.keywords,
                "importance":   record.importance,
                "archived":     record.archived,
                "language":     record.language,
                "text":         record.text[:2000],  # Neo4j string limit
                "summary":      record.summary,
                "human_text":   record.human_text,
                "ai_output":    record.ai_output,
                "model":        record.model,
                "capability":   record.capability,
                "content_hash": record.content_hash,
                "graph_id":     record.id,  # use record id as graph node identity
            }
            async with self._driver.session() as s:
                result = await s.run(
                    "MERGE (m:Memory {id:$id}) SET m += $props RETURN id(m) AS nid",
                    id=record.id, props=props,
                )
                rec = await result.single()
                graph_node_id = str(rec["nid"]) if rec else ""

                # Create parent relationship if parent_id set
                if record.parent_id:
                    await s.run("""
                        MATCH (child:Memory {id:$cid}), (parent:Memory {id:$pid})
                        MERGE (child)-[:DERIVED_FROM]->(parent)
                    """, cid=record.id, pid=record.parent_id)

                # Create session chain — link to previous record in session
                if record.session_id:
                    await s.run("""
                        MATCH (curr:Memory {id:$cid})
                        MATCH (prev:Memory {session_id:$sid})
                        WHERE prev.id <> $cid AND prev.created_at < $ts
                        WITH prev ORDER BY prev.created_at DESC LIMIT 1
                        MERGE (prev)-[:NEXT_IN_SESSION]->(curr)
                    """, cid=record.id, sid=record.session_id, ts=record.created_at)

                # Create explicit relations from record.relations
                for rel in record.relations:
                    target = rel.get("target_id","")
                    rtype  = re.sub(r'[^A-Z0-9_]', '_', rel.get("type","RELATED").upper())
                    if target:
                        await s.run(f"""
                            MATCH (src:Memory {{id:$sid}})
                            MERGE (tgt:Memory {{id:$tid}})
                            MERGE (src)-[r:{rtype}]->(tgt)
                            SET r += $props
                        """, sid=record.id, tid=target, props=rel.get("properties",{}))

                # Maintain :Session node and CONTAINS edge
                if record.session_id:
                    await s.run("""
                        MERGE (sess:Session {session_id: $sid})
                        ON CREATE SET sess.created_at = $ts, sess.agent_name = $agent
                        WITH sess
                        MATCH (m:Memory {id: $mid})
                        MERGE (sess)-[:CONTAINS]->(m)
                    """, sid=record.session_id, ts=record.created_at,
                         agent=next((t for t in record.tags if t not in
                                     ("session","start","agent_turn","human","ai",
                                      "cap_call","cap_output","message","event")), ""),
                         mid=record.id)

            return True
        except Exception as e:
            log.error("Neo4j store: %s", e)
            asyncio.ensure_future(emit_event({
                "type": "backend.error", "backend": "neo4j",
                "op": "store", "error": str(e)[:200]}))
            return False

    async def get(self, record_id: str) -> Optional[MemoryRecord]:
        if not self._driver: return None
        try:
            async with self._driver.session() as s:
                result = await s.run("MATCH (m:Memory {id:$id}) RETURN m", id=record_id)
                rec = await result.single()
            if not rec: return None
            return self._node_to_record(dict(rec["m"]))
        except Exception as e:
            log.error("Neo4j get: %s", e); return None

    async def search(self, query: str, limit: int = 10, filters: Optional[Dict] = None,
                     embedding: Optional[List[float]] = None) -> List[Tuple[MemoryRecord, float]]:
        if not self._driver: return []
        try:
            where_parts = ["m.archived = false"]
            params: dict = {"limit": limit}
            for k, v in (filters or {}).items():
                if k in ("session_id","record_type","source_type","category"):
                    where_parts.append(f"m.{k} = ${k}")
                    params[k] = v
            where = " AND ".join(where_parts)

            if query.strip():
                params["query"] = query
                cypher = f"""
                    CALL db.index.fulltext.queryNodes('vera_mem_text', $query)
                    YIELD node AS m, score
                    WHERE {where}
                    RETURN m, score ORDER BY score DESC LIMIT $limit
                """
            else:
                cypher = f"""
                    MATCH (m:Memory) WHERE {where}
                    RETURN m, m.importance AS score
                    ORDER BY m.created_at DESC LIMIT $limit
                """
            async with self._driver.session() as s:
                # NOTE: do NOT pass params as **kwargs — neo4j AsyncSession.run()
                # has 'query' as its own positional/keyword arg, so unpacking a
                # params dict that contains 'query' raises 'multiple values for
                # argument query'. Pass the dict as the second positional arg.
                result = await s.run(cypher, params)
                rows = await result.data()
            return [(self._node_to_record(dict(r["m"])), float(r.get("score",0.5))) for r in rows]
        except Exception as e:
            log.error("Neo4j search: %s", e); return []

    async def update(self, record_id: str, updates: Dict) -> bool:
        if not self._driver: return False
        safe = {k:v for k,v in updates.items()
                if k not in ("id","created_at") and isinstance(v,(str,int,float,bool,list))}
        if not safe: return False
        try:
            async with self._driver.session() as s:
                await s.run("MATCH (m:Memory {id:$id}) SET m += $props", id=record_id, props=safe)
            return True
        except Exception as e:
            log.error("Neo4j update: %s", e); return False

    async def relate(self, from_id: str, to_id: str, relation_type: str,
                     properties: Optional[Dict] = None) -> bool:
        if not self._driver: return False
        rtype = re.sub(r'[^A-Z0-9_]', '_', relation_type.upper())
        try:
            async with self._driver.session() as s:
                # Only create edge if both nodes exist — prevents ghost stub nodes
                await s.run(f"""
                    MATCH (src:Memory {{id:$fid}})
                    MATCH (tgt:Memory {{id:$tid}})
                    MERGE (src)-[r:{rtype}]->(tgt)
                    SET r += $props, r.created_at = $ts
                """, fid=from_id, tid=to_id, props=properties or {}, ts=now_iso())
            return True
        except Exception as e:
            log.error("Neo4j relate: %s", e); return False

    async def session_edges(self, session_id: str, limit: int = 200) -> List[Dict]:
        """Return all edges between Memory nodes in this session from Neo4j."""
        if not self._driver: return []
        limit = int(limit)
        try:
            async with self._driver.session() as s:
                result = await s.run(f"""
                    MATCH (m:Memory {{session_id:$sid}})-[r]->(n:Memory)
                    RETURN m.id AS from_id, n.id AS to_id, type(r) AS rel_type
                    LIMIT {limit}
                """, sid=session_id)
                rows = await result.data()
            return [{"from_id": r["from_id"], "to_id": r["to_id"],
                     "relation": r["rel_type"]} for r in rows]
        except Exception as e:
            log.error("Neo4j session_edges: %s", e); return []

    async def get_session_nodes(self, session_id: str, limit: int = 200) -> List[Dict]:
        """Fetch all Memory nodes for a session directly from Neo4j."""
        if not self._driver: return []
        limit = int(limit)  # ensure integer
        try:
            async with self._driver.session() as s:
                # Use property-map syntax and hardcoded LIMIT (parameterized LIMIT
                # is not supported in all Neo4j versions)
                result = await s.run(f"""
                    MATCH (m:Memory {{session_id: $sid}})
                    RETURN m.id AS id, m.record_type AS record_type,
                           m.source_type AS source_type, m.category AS category,
                           m.text AS text, m.summary AS summary,
                           m.capability AS capability, m.importance AS importance,
                           m.created_at AS created_at, m.tags AS tags,
                           m.session_id AS session_id, m.model AS model
                    ORDER BY m.created_at ASC
                    LIMIT {limit}
                """, sid=session_id)
                rows = await result.data()
            log.info("Neo4j get_session_nodes sid=%s -> %d nodes", session_id[:12], len(rows))
            return rows
        except Exception as e:
            log.error("Neo4j get_session_nodes: %s", e)
            return []

    async def get_all_nodes(self, limit: int = 300) -> List[Dict]:
        """Fetch all Memory nodes from Neo4j regardless of session."""
        if not self._driver: return []
        limit = int(limit)
        try:
            async with self._driver.session() as s:
                result = await s.run(f"""
                    MATCH (m:Memory)
                    RETURN m.id AS id, m.record_type AS record_type,
                           m.source_type AS source_type, m.category AS category,
                           m.text AS text, m.summary AS summary,
                           m.capability AS capability, m.importance AS importance,
                           m.created_at AS created_at, m.tags AS tags,
                           m.session_id AS session_id, m.model AS model
                    ORDER BY m.created_at DESC
                    LIMIT {limit}
                """)
                rows = await result.data()
            log.info("Neo4j get_all_nodes -> %d nodes", len(rows))
            return rows
        except Exception as e:
            log.error("Neo4j get_all_nodes: %s", e); return []

    async def get_all_edges(self, limit: int = 1000) -> List[Dict]:
        """Fetch all edges from Neo4j regardless of session."""
        if not self._driver: return []
        limit = int(limit)
        try:
            async with self._driver.session() as s:
                result = await s.run(f"""
                    MATCH (m:Memory)-[r]->(n:Memory)
                    RETURN m.id AS from_id, n.id AS to_id, type(r) AS rel_type
                    LIMIT {limit}
                """)
                rows = await result.data()
            return [{"from_id": r["from_id"], "to_id": r["to_id"],
                     "relation": r["rel_type"]} for r in rows]
        except Exception as e:
            log.error("Neo4j get_all_edges: %s", e); return []
            return rows
        except Exception as e:
            log.error("Neo4j get_session_nodes: %s", e); return []

    async def traverse(self, start_id: str, relation_types: Optional[List[str]] = None,
                       depth: int = 2, limit: int = 20) -> List[Dict]:
        if not self._driver: return []
        rel_filter = ""
        if relation_types:
            rtypes = "|".join(re.sub(r'[^A-Z0-9_]','_',r.upper()) for r in relation_types)
            rel_filter = f":{rtypes}"
        try:
            async with self._driver.session() as s:
                # Use a named path so we can extract the last relationship via
                # relationships(p) — avoids the Type mismatch when calling
                # type(last(collect(r))) where r is already a list for
                # variable-length (*1..depth) patterns.
                result = await s.run(f"""
                    MATCH p=(start:Memory {{id:$id}})-[{rel_filter}*1..{depth}]->(related:Memory)
                    WITH related, relationships(p) AS rels
                    RETURN DISTINCT related, type(last(rels)) AS rel_type
                    LIMIT $limit
                """, id=start_id, limit=limit)
                rows = await result.data()
            return [{"node": dict(r["related"]), "relation": r["rel_type"]} for r in rows]
        except Exception as e:
            log.error("Neo4j traverse: %s", e); return []

    async def stats(self) -> Dict:
        if not self._driver: return {"connected": False}
        try:
            async with self._driver.session() as s:
                n     = (await (await s.run("MATCH (m:Memory) RETURN count(m) AS c")).single())["c"]
                rels  = (await (await s.run("MATCH ()-[r]->() RETURN count(r) AS c")).single())["c"]
                sess  = (await (await s.run("MATCH (s:Session) RETURN count(s) AS c")).single())["c"]
            return {"connected":True,"nodes":n,"relationships":rels,"sessions":sess,
                    "uri":NEO4J_URI}
        except Exception as e:
            log.error("Neo4j stats: %s", e)
            return {"connected":False,"error":str(e),"uri":NEO4J_URI}

    @staticmethod
    def _node_to_record(props: dict) -> MemoryRecord:
        return MemoryRecord(**{k:v for k,v in props.items()
                               if k in MemoryRecord.__dataclass_fields__})


# ─────────────────────────────────────────────────────────────────────────────
# EMBEDDING  —  Ollama text embeddings
# ─────────────────────────────────────────────────────────────────────────────

_EMBED_FAILED = False   # set True after first 404 on both endpoints to stop spamming

async def embed_text(text: str) -> Optional[List[float]]:
    """Generate a text embedding via the centralized ollama_embed (logged to Jobs).

    Preserves the circuit-breaker and MEMORY_AUTO_EMBED gate.
    """
    global _EMBED_FAILED
    if _EMBED_FAILED or not MEMORY_AUTO_EMBED or not text.strip():
        return None
    try:
        from Vera.Orchestration.capability_orchestration import ollama_embed
        vec = await ollama_embed(text, model=OLLAMA_EMBED_MODEL)
        if vec is None:
            _EMBED_FAILED = True
            log.warning(
                "Embedding unavailable — model '%s' may not be pulled. "
                "Run: ollama pull %s  OR set MEMORY_AUTO_EMBED=0 to disable.",
                OLLAMA_EMBED_MODEL, OLLAMA_EMBED_MODEL
            )
        return vec
    except Exception as e:
        log.debug("embed_text: %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# HYBRID MEMORY STORE  —  orchestrates all backends
# ─────────────────────────────────────────────────────────────────────────────

class HybridMemoryStore:
    """
    Orchestrates multiple MemoryBackend instances.

    Write path:  embed → all backends in parallel
    Read path:   query all backends → merge → deduplicate → re-rank
    """

    def __init__(self):
        self._backends: Dict[str, MemoryBackend] = {}
        self._ready = False

    def register(self, backend: MemoryBackend):
        self._backends[backend.name] = backend
        log.info("Memory backend registered: %s", backend.name)

    async def startup(self):
        for name, backend in list(self._backends.items()):
            ok = await backend.connect()
            if not ok:
                log.warning("Backend %s failed to connect — disabled", name)
                del self._backends[name]
        self._ready = bool(self._backends)
        active = list(self._backends.keys())
        log.info("HybridMemoryStore ready with backends: %s", active)
        await emit_event({"type": "backend.status",
                          "active": active,
                          "missing": [b for b in ["postgres","chroma","neo4j"] if b not in active]})

    async def shutdown(self):
        for b in self._backends.values():
            await b.disconnect()

    @property
    def backends(self) -> List[str]:
        return list(self._backends.keys())

    async def store(self, record: MemoryRecord) -> Dict:
        """Store a record across all backends. Returns per-backend success map."""
        # Fill in derived fields
        if not record.content_hash:
            record.content_hash = record.compute_hash()
        if not record.keywords:
            record.keywords = _extract_keywords(record.full_text or record.text)
        if not record.text and record.full_text:
            record.text = record.full_text[:500]

        # Generate embedding (shared across backends)
        if not record.embedding and MEMORY_AUTO_EMBED:
            emb = await embed_text(record.text or record.summary)
            if emb:
                record.embedding       = emb
                record.embedding_model = OLLAMA_EMBED_MODEL

        # Fan out to all backends concurrently
        tasks = {name: b.store(record) for name, b in self._backends.items()}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        outcome = {}
        for name, res in zip(tasks.keys(), results):
            outcome[name] = res if isinstance(res, bool) else False

        await emit_event({
            "type":       "memory.stored",
            "id":         record.id,
            "session_id": record.session_id,
            "record_type":record.record_type,
            "backends":   outcome,
            # Include lightweight record for live graph injection in the UI
            "record": {
                "id":          record.id,
                "session_id":  record.session_id,
                "record_type": record.record_type,
                "source_type": record.source_type,
                "category":    record.category,
                "capability":  record.capability,
                "text":        (record.text or record.summary or "")[:200],
                "importance":  record.importance,
                "tags":        (record.tags or [])[:6],
                "created_at":  record.created_at,
                "model":       record.model or "",
                "relations":   [],
            },
        })
        return outcome

    async def search(
        self,
        query:   str,
        limit:   int             = 10,
        filters: Optional[Dict]  = None,
        backends: Optional[List[str]] = None,
    ) -> List[Dict]:
        """
        Hybrid search across all (or specified) backends.
        Generates an embedding for the query and passes it to vector backends.
        Results are merged, deduplicated by id, and re-ranked by a combined score.
        """
        embedding = await embed_text(query)
        active = {k: v for k, v in self._backends.items()
                  if backends is None or k in backends}
        tasks = {
            name: b.search(query, limit=limit*2, filters=filters, embedding=embedding)
            for name, b in active.items()
        }
        all_results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        # Merge and deduplicate — higher score wins when same id appears in multiple backends
        merged: Dict[str, Tuple[MemoryRecord, float]] = {}
        for name, results in zip(tasks.keys(), all_results):
            if isinstance(results, Exception): continue
            for rec, score in results:
                if rec.id not in merged or merged[rec.id][1] < score:
                    merged[rec.id] = (rec, score)

        # Re-rank: importance × backend_score
        ranked = sorted(
            merged.values(),
            key=lambda x: x[1] * (0.5 + 0.5 * x[0].importance),
            reverse=True,
        )[:int(limit)]
        return [{"record": r.to_dict(), "score": round(s, 4)} for r, s in ranked]

    async def get(self, record_id: str) -> Optional[MemoryRecord]:
        for b in self._backends.values():
            rec = await b.get(record_id)
            if rec: return rec
        return None

    async def update(self, record_id: str, updates: Dict) -> Dict:
        updates["updated_at"] = datetime.now(timezone.utc)
        tasks = {n: b.update(record_id, updates) for n,b in self._backends.items()}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        return {n: r if isinstance(r, bool) else False for n, r in zip(tasks, results)}

    async def relate(self, from_id: str, to_id: str, relation_type: str,
                     properties: Optional[Dict] = None) -> Dict:
        neo = self._backends.get("neo4j")
        if neo:
            ok = await neo.relate(from_id, to_id, relation_type, properties)
            log.info("EDGE %s-[%s]->%s neo4j=%s", from_id[:8], relation_type, to_id[:8], ok)
            return {"neo4j": ok}
        msg = f"EDGE DROPPED — neo4j not in backends={list(self._backends.keys())}"
        log.error(msg)
        asyncio.ensure_future(emit_event({"type": "backend.error", "backend": "neo4j",
                                          "op": "relate", "error": msg}))
        return {}
    async def session_edges(self, session_id: str, limit: int = 200) -> List[Dict]:
        neo = self._backends.get("neo4j")
        if not neo:
            log.error("SESSION_EDGES — neo4j not in backends=%s", list(self._backends.keys()))
            return []
        try:
            edges = await neo.session_edges(session_id, limit=limit)
            log.info("SESSION_EDGES sid=%s → %d edges", session_id[:12], len(edges))
            return edges
        except Exception as e:
            log.error("SESSION_EDGES neo4j error: %s", e)
            return []
    async def get_session_nodes(self, session_id: str, limit: int = 200) -> List[Dict]:
        """Get all nodes for a session from Neo4j."""
        neo = self._backends.get("neo4j")
        if neo and hasattr(neo, "get_session_nodes"):
            nodes = await neo.get_session_nodes(session_id, limit=limit)
            return nodes
        log.error("get_session_nodes: neo4j not in backends=%s", list(self._backends.keys()))
        return []

    async def get_all_nodes(self, limit: int = 300) -> List[Dict]:
        neo = self._backends.get("neo4j")
        if neo and hasattr(neo, "get_all_nodes"):
            return await neo.get_all_nodes(limit=limit)
        return []

    async def get_all_edges(self, limit: int = 1000) -> List[Dict]:
        neo = self._backends.get("neo4j")
        if neo and hasattr(neo, "get_all_edges"):
            return await neo.get_all_edges(limit=limit)
        return []

    async def traverse(self, start_id: str, relation_types: Optional[List[str]] = None,
                       depth: int = 2, limit: int = 20) -> List[Dict]:
        # Prefer Neo4j for graph traversal, fallback to Postgres
        for name in ("neo4j", "postgres"):
            if name in self._backends:
                return await self._backends[name].traverse(start_id, relation_types, depth, limit)
        return []

    async def stats(self) -> Dict:
        tasks = {n: b.stats() for n, b in self._backends.items()}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        return {n: r if isinstance(r, dict) else {"error": str(r)}
                for n, r in zip(tasks, results)}




@capability(
    "memory.edge_diag", memory="off",
    http_method="GET", http_path="/memory/edges/diag", http_tags=["memory"],
    description="Diagnostic: count edges in Postgres and Neo4j for a session.",
)
async def memory_edge_diag(session_id: str = "", trace_id=None):
    result = {}
    pg_backend = MEMORY._backends.get("postgres")
    if pg_backend and pg_backend._pool:
        try:
            async with pg_backend._pool.acquire() as conn:
                total = await conn.fetchval("SELECT COUNT(*) FROM vera_memory_edges")
                sess  = await conn.fetchval(
                    "SELECT COUNT(*) FROM vera_memory_edges WHERE session_id=$1", session_id
                ) if session_id else 0
                by_rel = await conn.fetch(
                    "SELECT relation, COUNT(*) n FROM vera_memory_edges GROUP BY relation ORDER BY n DESC LIMIT 10"
                )
                # Check if unique index exists
                idx = await conn.fetchval(
                    "SELECT indexname FROM pg_indexes WHERE tablename='vera_memory_edges' AND indexname='vme_unique'"
                )
                result["postgres"] = {
                    "total_edges": total,
                    "session_edges": sess,
                    "by_relation": {r["relation"]: r["n"] for r in by_rel},
                    "unique_index": idx or "MISSING",
                }
        except Exception as e:
            result["postgres"] = {"error": str(e)}
    neo_backend = MEMORY._backends.get("neo4j")
    if neo_backend and neo_backend._driver:
        try:
            async with neo_backend._driver.session() as s:
                rels = (await (await s.run("MATCH ()-[r]->() RETURN count(r) AS c")).single())["c"]
                result["neo4j"] = {"total_relationships": rels}
        except Exception as e:
            result["neo4j"] = {"error": str(e)}
    return result

# ── Global singleton ─────────────────────────────────────────────────────────
MEMORY = HybridMemoryStore()

# Register default backends based on availability
MEMORY.register(PostgresBackend())
MEMORY.register(ChromaBackend())
MEMORY.register(Neo4jBackend())


# ─────────────────────────────────────────────────────────────────────────────
# REDIS PROMOTER  —  auto-promote events to persistent memory
# ─────────────────────────────────────────────────────────────────────────────

async def _memory_promoter():
    """
    Listen to vera:events on Redis and auto-promote qualifying events
    to the persistent hybrid memory store.

    Promotes:
      - Events with type "memory.*"
      - LLM generation results (llm.generate, cap.ok for llm.*)
      - Explicit memory.store / memory.promote events
    """
    if not _redis():
        log.debug("Memory promoter: Redis not available")
        return

    last_id = "$"
    log.info("Memory promoter started")

    while True:
        try:
            results = await _redis().xread(
                {MEMORY_PROMO_STREAM: last_id}, count=50, block=5000
            )
        except Exception as e:
            log.debug("Memory promoter xread: %s", e)
            await asyncio.sleep(2)
            continue

        if not results:
            continue

        for stream, messages in results:
            for msg_id, data in messages:
                last_id = msg_id
                try:
                    raw = data.get(b"data", b"{}")
                    event = json.loads(raw)
                    await _maybe_promote(event)
                except Exception as e:
                    log.debug("Memory promoter event error: %s", e)


async def _maybe_promote(event: dict):
    """Decide whether to promote a Redis event to memory and do it."""
    etype = event.get("type", "")

    # Explicit memory events
    if etype == "memory.store":
        rec = _event_to_record(event)
        await MEMORY.store(rec)
        return

    if etype == "memory.promote":
        # Fetch from Redis key if a reference was given
        ref = event.get("ref")
        if ref and _redis():
            raw = await _redis().get(ref)
            if raw:
                try:
                    event = json.loads(raw)
                except Exception:
                    pass
        rec = _event_to_record(event)
        await MEMORY.store(rec)
        return

    # Auto-promote LLM outputs
    if etype == "cap.ok" and event.get("name","").startswith("llm."):
        result_data = event.get("result") or event.get("content") or {}
        text = ""
        if isinstance(result_data, dict):
            text = result_data.get("text","") or result_data.get("summary","")
        elif isinstance(result_data, str):
            text = result_data
        if len(text) > 50:   # only promote substantive outputs
            rec = MemoryRecord(
                session_id  = event.get("session_id",""),
                trace_id    = event.get("trace_id",""),
                record_type = "message",
                source_type = "ai",
                category    = "llm_output",
                tags        = ["auto_promoted","llm"],
                text        = text[:500],
                full_text   = text,
                human_text  = False,
                ai_output   = True,
                model       = event.get("model",""),
                capability  = event.get("name",""),
                importance  = 0.6,
            )
            await MEMORY.store(rec)


def _event_to_record(event: dict) -> MemoryRecord:
    """Convert a generic Redis event dict to a MemoryRecord."""
    text = (event.get("text") or event.get("content") or
            event.get("message") or event.get("data") or "")
    if isinstance(text, dict): text = json.dumps(text)
    return MemoryRecord(
        id          = event.get("id") or str(uuid.uuid4()),
        session_id  = event.get("session_id",""),
        trace_id    = event.get("trace_id",""),
        record_type = event.get("record_type","event"),
        source_type = event.get("source_type","system"),
        category    = event.get("category","general"),
        tags        = event.get("tags",[]),
        keywords    = event.get("keywords",[]),
        importance  = float(event.get("importance",0.5)),
        text        = str(text)[:500],
        full_text   = str(text),
        summary     = event.get("summary",""),
        human_text  = event.get("human_text", event.get("source_type") == "human"),
        ai_output   = event.get("ai_output", event.get("source_type") == "ai"),
        model       = event.get("model",""),
        capability  = event.get("capability",""),
        metadata    = {k: v for k, v in event.items()
                      if k not in ("text","content","id","session_id","type")
                      and isinstance(v, (str,int,float,bool))},
    )


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "memory.store", memory="off",
    http_method="POST", http_path="/memory/store", http_tags=["memory"],
    description="Store a memory record across all active backends (Postgres + Chroma + Neo4j).",
)
async def memory_store(
    text:           str,
    session_id:     str   = "",
    trace_id:       str   = "",
    record_type:    str   = "message",
    source_type:    str   = "human",
    category:       str   = "general",
    tags:           str   = "",       # comma-separated
    summary:        str   = "",
    full_text:      str   = "",
    human_text:     bool  = True,
    ai_output:      bool  = False,
    model:          str   = "",
    capability_src: str   = "",
    importance:     float = 0.5,
    parent_id:      str   = "",
    trace_id_param=None,
):
    rec = MemoryRecord(
        session_id  = session_id,
        trace_id    = trace_id or trace_id_param or "",
        record_type = record_type,
        source_type = source_type,
        category    = category,
        tags        = [t.strip() for t in tags.split(",") if t.strip()],
        text        = text,
        summary     = summary,
        full_text   = full_text or text,
        human_text  = human_text,
        ai_output   = ai_output,
        model       = model,
        capability  = capability_src,
        importance  = importance,
        parent_id   = parent_id,
    )
    result = await MEMORY.store(rec)
    return {"id": rec.id, "backends": result, "keywords": rec.keywords,
            "content_hash": rec.content_hash}


@capability(
    "memory.search", memory="off",
    http_method="POST", http_path="/memory/search", http_tags=["memory"],
    description="Hybrid semantic + keyword search across all memory backends.",
)
async def memory_search(
    query:          str,
    limit:          int   = 10,
    session_id:     str   = "",
    record_type:    str   = "",
    category:       str   = "",
    tags:           str   = "",
    backends:       str   = "",   # comma-separated subset e.g. "chroma,neo4j"
    trace_id=None,
):
    # Defensive coerce — query params arrive as strings via HTTP
    try:
        limit = int(limit)
    except Exception:
        limit = 10
    filters: dict = {}
    if session_id:  filters["session_id"]  = session_id
    if record_type: filters["record_type"] = record_type
    if category:    filters["category"]    = category
    if tags:        filters["tags"]        = [t.strip() for t in tags.split(",") if t.strip()]
    backend_list = [b.strip() for b in backends.split(",") if b.strip()] or None
    results = await MEMORY.search(query, limit=limit, filters=filters or None,
                                   backends=backend_list)
    return {"results": results, "count": len(results), "query": query}


@capability(
    "memory.get", memory="off",
    http_method="GET", http_path="/memory/get", http_tags=["memory"],
    description="Retrieve a specific memory record by id.",
)
async def memory_get(id: str, trace_id=None):
    rec = await MEMORY.get(id)
    if not rec:
        return {"error": f"Record not found: {id}"}
    return rec.to_dict()


@capability(
    "memory.relate", memory="off",
    http_method="POST", http_path="/memory/relate", http_tags=["memory"],
    description="Create a typed relationship between two memory records in the graph.",
)
async def memory_relate(
    from_id:       str,
    to_id:         str,
    relation_type: str   = "RELATED_TO",
    properties:    str   = "{}",
    trace_id=None,
):
    try:    props = json.loads(properties)
    except: props = {}
    result = await MEMORY.relate(from_id, to_id, relation_type, props)
    return {"from_id": from_id, "to_id": to_id,
            "relation_type": relation_type, "backends": result}


@capability(
    "memory.traverse", memory="off",
    http_method="POST", http_path="/memory/traverse", http_tags=["memory"],
    description="Graph traversal from a memory node. Returns connected records up to depth hops.",
)
async def memory_traverse(
    start_id:       str,
    relation_types: str   = "",   # comma-separated, empty=all
    depth:          int   = 2,
    limit:          int   = 20,
    trace_id=None,
):
    rels = [r.strip() for r in relation_types.split(",") if r.strip()] or None
    results = await MEMORY.traverse(start_id, rels, depth, limit)
    return {"start_id": start_id, "depth": depth, "results": results,
            "count": len(results)}


@capability(
    "memory.stats", memory="off", silent=True,
    http_method="GET", http_path="/memory/stats", http_tags=["memory"],
    description="Statistics from all active memory backends (record counts, index sizes).",
)
async def memory_stats(trace_id=None):
    s = await MEMORY.stats()
    # NOTE: Do NOT emit_event here — it creates a feedback loop:
    # HTML loadMemoryStats() → HTTP /memory/stats → cap.ok event → WS →
    # handleLiveEvent → _scheduleStatsFetch → loadMemoryStats() again.
    # The dashboard now uses the returned data directly via _applyMemoryStats().
    return {"backends": s, "active_backends": MEMORY.backends}


@capability(
    "memory.backends", memory="off",
    http_method="GET", http_path="/memory/backends", http_tags=["memory"],
    description="List active memory backends and their connection status.",
)
async def memory_backends(trace_id=None):
    stats = await MEMORY.stats()
    return {
        "backends": [
            {"name": n, "connected": stats.get(n,{}).get("connected",False),
             **{k:v for k,v in stats.get(n,{}).items() if k!="connected"}}
            for n in MEMORY.backends
        ]
    }


@capability(
    "memory.recall", memory="off",
    http_method="POST", http_path="/memory/recall", http_tags=["memory"],
    description="Context-aware recall: semantic search + graph expansion for a query. "
                "Returns semantically similar memories enriched with their graph neighbours.",
)
async def memory_recall(
    query:       str,
    session_id:  str   = "",
    limit:       int   = 8,
    graph_depth: int   = 1,
    trace_id=None,
):
    # Step 1: vector search
    filters = {"session_id": session_id} if session_id else None
    results = await MEMORY.search(query, limit=limit, filters=filters)

    # Step 2: for each result, pull one hop of graph neighbours
    enriched = []
    for item in results:
        rec_id    = item["record"]["id"]
        neighbours = await MEMORY.traverse(rec_id, depth=graph_depth, limit=5)
        enriched.append({
            **item,
            "neighbours": [
                {"id":   n["node"]["id"] if "node" in n else n.get("to_id",""),
                 "text":  n["node"].get("text","")[:200] if "node" in n else "",
                 "type":  n.get("relation",""),
                 "cat":   n["node"].get("category","") if "node" in n else ""}
                for n in neighbours
            ],
        })

    # Step 3: build a context string for the calling LLM
    context_parts = []
    for item in enriched[:limit]:
        r = item["record"]
        context_parts.append(
            f"[{r.get('record_type','?')} | {r.get('created_at','')[:10]} | "
            f"score:{item['score']:.2f}]\n{r.get('text','')[:300]}"
        )

    return {
        "results":  enriched,
        "count":    len(enriched),
        "context":  "\n\n---\n\n".join(context_parts),
    }


@capability(
    "memory.forget", memory="off",
    http_method="POST", http_path="/memory/forget", http_tags=["memory"],
    description="Soft-delete a memory record (sets archived=True). Records are never physically deleted.",
)
async def memory_forget(id: str, trace_id=None):
    result = await MEMORY.update(id, {"archived": True})
    await emit_event({"type": "memory.forgotten", "id": id})
    return {"id": id, "backends": result}


@capability(
    "memory.session_history", memory="off",
    http_method="GET", http_path="/memory/session", http_tags=["memory"],
    description="Retrieve all memory records for a session, ordered by time.",
)
async def memory_session_history(
    session_id:  str,
    limit:       int   = 50,
    record_type: str   = "",
    trace_id=None,
):
    filters: dict = {"session_id": session_id}
    if record_type: filters["record_type"] = record_type
    results = await MEMORY.search("", limit=int(limit), filters=filters)
    # Sort by created_at — results are [{record:{...}, score:n}] dicts
    def _ts(item):
        rec = item.get("record", {}) if isinstance(item, dict) else {}
        return rec.get("created_at", "")
    results.sort(key=_ts, reverse=False)
    records = [r["record"] for r in results if isinstance(r, dict) and "record" in r]
    return {"session_id": session_id, "records": records, "count": len(records)}


@capability(
    "memory.similar", memory="off",
    http_method="POST", http_path="/memory/similar", http_tags=["memory"],
    description="Find the top-N most semantically similar records to a given text or record id.",
)
async def memory_similar(
    query:        str   = "",
    record_id:    str   = "",
    limit:        int   = 10,
    trace_id=None,
):
    if record_id and not query:
        rec = await MEMORY.get(record_id)
        if rec:
            query = rec.text or rec.summary
    if not query:
        return {"error": "Provide query text or a record_id", "results": []}
    results = await MEMORY.search(query, limit=limit, backends=["chroma"])
    # Exclude the source record itself
    results = [r for r in results if r["record"]["id"] != record_id]
    return {"results": results[:limit], "count": len(results)}


@capability(
    "memory.promote", memory="off",
    http_method="POST", http_path="/memory/promote", http_tags=["memory"],
    description="Manually promote a Redis event or raw dict payload to persistent memory.",
)
async def memory_promote_cap(
    event_json:  str   = "{}",
    source_type: str   = "system",
    category:    str   = "general",
    importance:  float = 0.5,
    trace_id=None,
):
    try:    event = json.loads(event_json)
    except: event = {"text": event_json}
    event.setdefault("source_type", source_type)
    event.setdefault("category",    category)
    event.setdefault("importance",  importance)
    rec = _event_to_record(event)
    result = await MEMORY.store(rec)
    return {"id": rec.id, "backends": result}


@capability(
    "memory.auto_summarise", memory="off",
    http_method="POST", http_path="/memory/summarise", http_tags=["memory"],
    description="Ask the LLM to generate a summary for a stored record and update it.",
)
async def memory_auto_summarise(id: str, trace_id=None):
    rec = await MEMORY.get(id)
    if not rec:
        return {"error": f"Record not found: {id}"}
    text_to_summarise = rec.full_text or rec.text
    if not text_to_summarise:
        return {"error": "Record has no text to summarise"}
    summary = await ollama_generate(
        text_to_summarise[:3000],
        system="Summarise in one concise sentence. Return only the summary.",
        prefer_gpu=True,
    )
    summary = summary.strip()
    await MEMORY.update(id, {"summary": summary})
    return {"id": id, "summary": summary}


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "memory.neo4j_diag", memory="off",
    http_method="GET", http_path="/memory/neo4j/diag", http_tags=["memory"],
    description="Diagnose Neo4j connectivity, node and edge counts.",
)
async def memory_neo4j_diag(session_id: str = "", trace_id=None):
    neo = MEMORY._backends.get("neo4j")
    if not neo:
        return {"error": "neo4j not in backends", "backends": list(MEMORY._backends.keys())}
    if not neo._driver:
        return {"error": "neo4j driver is None — connect() failed"}
    try:
        async with neo._driver.session() as s:
            n_nodes = (await (await s.run("MATCH (m:Memory) RETURN count(m) AS c")).single())["c"]
            n_rels  = (await (await s.run("MATCH ()-[r]->() RETURN count(r) AS c")).single())["c"]
            sess_nodes, sess_edges = [], []
            if session_id:
                r1 = await s.run("MATCH (m:Memory {session_id:$sid}) RETURN m.id AS id, m.record_type AS t LIMIT 10", sid=session_id)
                sess_nodes = await r1.data()
                r2 = await s.run("""
                    MATCH (m:Memory)-[r]->(n:Memory)
                    WHERE m.session_id=$sid OR n.session_id=$sid
                    RETURN m.id AS from_id, n.id AS to_id, type(r) AS rel LIMIT 20
                """, sid=session_id)
                sess_edges = await r2.data()
        return {"connected": True, "nodes": n_nodes, "rels": n_rels,
                "session_nodes": sess_nodes, "session_edges": sess_edges,
                "backends": list(MEMORY._backends.keys())}
    except Exception as e:
        return {"error": str(e), "connected": False}


async def _startup():
    await MEMORY.startup()
    # Start the Redis promoter background task
    asyncio.create_task(_memory_promoter())
    log.info("vera_memory ready — backends: %s", MEMORY.backends)

# Schedule startup to run once after the event loop is running
schedule(_startup, interval=999999, name="memory_startup")
# Do NOT add create_task(_startup()) here — schedule() handles it once.
# Running both causes double Neo4j connect + duplicate backend logs.