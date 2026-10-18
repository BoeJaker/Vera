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
from dataclasses import dataclass, field, fields, asdict
from datetime import datetime, timezone
from pathlib import Path

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
from typing import Any, Dict, List, Mapping, Optional, Tuple

import httpx

import Vera.vera.capability_orchestration as _orch

from Vera.vera.config import cfg
from Vera.vera.capability_orchestration import (
    APP,            # noqa
    register_ui,
    capability, emit_event, now_iso, ollama_generate, schedule,
)
# Dev-sandbox write guard — memory backends are prod's SHARED stores; suppress
# writes when running as a dev sandbox (strict no-op in prod). See sandbox_guard.
from Vera.vera.sandbox_guard import write_blocked as _sbx_write_blocked   # noqa: E402
try:                                                    # RF-77
    from Vera.vera.fabric import chroma_bound_core as _chroma_bound
except ImportError:                                     # pragma: no cover
    from . import chroma_bound_core as _chroma_bound
try:                                                    # one hit per repeated text (recall_core)
    from Vera.vera.fabric import recall_core as _recall_core
except ImportError:                                     # pragma: no cover
    from . import recall_core as _recall_core
try:                                                    # one 0..1 hybrid score (RF-05)
    from Vera.vera.fabric import memory_score_core as _memory_score_core
except ImportError:                                     # pragma: no cover
    from . import memory_score_core as _memory_score_core
try:                                                    # long questions: content terms only (FM-7)
    from Vera.vera.fabric import fusion_core as _fusion_core
except ImportError:                                     # pragma: no cover
    from . import fusion_core as _fusion_core
try:                                                    # noisy records rank after the rest (memory_noise_core)
    from Vera.vera.fabric import memory_noise_core as _noise_core
except ImportError:                                     # pragma: no cover
    from . import memory_noise_core as _noise_core
try:                                                    # default-off public-ingress revision canary
    from Vera.vera.fabric import memory_dual_write_runtime as _revision_canary
except ImportError:                                     # pragma: no cover
    from . import memory_dual_write_runtime as _revision_canary
try:                                                    # default-off manual split-write repair
    from Vera.vera.fabric import memory_dual_write_repair_runtime as _revision_repair
except ImportError:                                     # pragma: no cover
    from . import memory_dual_write_repair_runtime as _revision_repair
try:                                                    # deterministic rollback rehearsal
    from Vera.vera.fabric import memory_dual_write_rollback as _revision_rollback
except ImportError:                                     # pragma: no cover
    from . import memory_dual_write_rollback as _revision_rollback
try:                                                    # the time a question asks about (CU-M.9)
    from Vera.vera.fabric import recall_scope_core as _scope_core
except ImportError:                                     # pragma: no cover
    from . import recall_scope_core as _scope_core


def _as_dt(v: Any) -> datetime:
    """A window bound as an aware datetime (a datetime, or an ISO string with Z or an offset)."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


try:                                                    # one canonical record per repeated text (CU-M.5)
    from Vera.vera.fabric import memory_canonical_core as _canon_core
except ImportError:                                     # pragma: no cover
    from . import memory_canonical_core as _canon_core
# "0" ranks noisy records with the rest again.
_RECALL_NOISE_LAST = __import__("os").getenv("VERA_MEMORY_NOISE_LAST", "1") != "0"
try:                                                    # retrieval timing envelopes (FM-7.0)
    from Vera.vera.fabric import retrieval_timing_core as _rtc
except ImportError:                                     # pragma: no cover
    from . import retrieval_timing_core as _rtc
# What recall leaves out unless asked (owner, 2026-10-04): tool calls and imported coding-agent transcript lines -
# kept, still in the activity graph, findable with include_activity or by naming the record type; not embedded.
# "" restores the old behaviour (everything recalled and embedded).
_RECALL_EXCLUDE_TYPES, _RECALL_EXCLUDE_TRANSCRIPTS = _recall_core.parse_exclusions(
    __import__("os").getenv("VERA_MEMORY_RECALL_EXCLUDE", "cap_call,dag_step,transcript"))

log = logging.getLogger("vera.memory")

_SBX_BLOCK_LOGGED = set()


def _sbx_note(where: str) -> None:
    if where not in _SBX_BLOCK_LOGGED:
        _SBX_BLOCK_LOGGED.add(where)
        log.info("dev-sandbox write guard: suppressed %s write to shared prod "
                 "store (set VERA_SANDBOX_WRITE_GUARD=0 to allow)", where)

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
# Match data_fabric._embed's normalize flag EXACTLY. ollama_embed's de-dup cache
# is keyed by (model | normalize | provider | text-hash); memory and the fabric
# embed the SAME chat text, so a MISMATCHED normalize flag yields different keys
# and the shared cache never collapses them — the text is embedded TWICE (2x load
# on the serialised CPU embed nodes, which then slows the query-embed that recall
# blocks on). data_fabric uses normalize=HAS_NUMPY; mirror it so the keys match
# and the second store's embed is a cache hit. Memory's Chroma cosine space is
# normalise-agnostic, so this does not change retrieval results.
try:
    import numpy as _np_probe     # noqa: F401  (presence probe only)
    _EMBED_NORMALIZE = True
except Exception:
    _EMBED_NORMALIZE = False
MEMORY_PROMO_STREAM = "vera:events"
MEMORY_EVENT_TYPES  = {"memory.store", "memory.promote", "llm.generate", "cap.ok"}

# Version the Chroma collection by embed model. Different models emit different
# vector dimensions (e.g. all-MiniLM=384 vs nomic-embed-text=768); reusing one
# collection across models triggers Chroma's "expecting embedding with dimension
# of N, got M" error. Suffixing the model keeps each dimension in its own
# collection — switching models just creates a fresh one (old data untouched).
def _versioned_collection(base: str, model: str) -> str:
    tag = re.sub(r"[^A-Za-z0-9._-]", "_", (model or "default")).strip("._-") or "default"
    return f"{base}__{tag}"[:63]   # Chroma collection names cap at 63 chars

CHROMA_COLLECTION = _versioned_collection(CHROMA_COLLECTION, OLLAMA_EMBED_MODEL)

# Lucene query-parser special characters. The Neo4j fulltext index
# (db.index.fulltext.queryNodes) parses its query as Lucene syntax, so raw text
# containing these — e.g. the parentheses in "openclaw_status() got an
# unexpected keyword argument" — raises a ParseException. Escape them so any
# free-text query is treated literally.
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')

def _lucene_escape(q: str) -> str:
    return _LUCENE_SPECIAL.sub(r"\\\1", q or "")

# HybridMemoryStore.search() truncates any incoming query to this many
# characters before embedding it or handing it to a backend — see the
# comment at that call site for why. Generous for a real search query's
# intent, nowhere near Neo4j's 1024-Lucene-clause cap even in the worst
# case (every character its own word).
# Candidates each backend returns per hit asked for: the merge collapses repeated texts,
# so `limit` distinct hits need more than `limit` raw ones (context-and-ui E11).
_CANDIDATES_PER_HIT = 3
# Neo4j fulltext candidates scored per hit asked for (see Neo4jBackend.search).
_NEO_FULLTEXT_CANDIDATES_PER_HIT = 20
_NEO_FULLTEXT_MIN_CANDIDATES = 500

# The last 500 timing envelopes per retrieval path in this process (FM-7.0), read by the
# retrieval.timing capability. Timeouts, errors and very slow calls also go out as events.
RETRIEVAL_TIMING = _rtc.Window(500)

# Chroma vector search without server-side filters. Any `where` makes Chroma build an
# allowed-id set over the whole collection first: live 2026-10-05 on 212k vectors, 10-12 ms
# unfiltered vs 2.4-3.7 s with only archived=false - a cost that grows with the collection
# whatever it holds. The default exclusions (archived, the kinds recall leaves out,
# transcript lines) are applied here, after an over-fetch; only when too few survive does the
# search fall back to the filtered query. A caller's own filters (session_id, record_type, ...)
# still go to the server.
_CHROMA_OVERFETCH = int(os.getenv("VERA_CHROMA_OVERFETCH", "3") or 3)
CHROMA_PATHS: Dict[str, int] = {"unfiltered": 0, "fallback": 0, "filtered": 0}
# What the nightly vector guard found and dropped last time (see _vector_guard_job).
VECTOR_GUARD: Dict[str, Any] = {}


def _record_timing(env) -> None:
    d = env.to_dict()
    RETRIEVAL_TIMING.add(d)
    if env.worth_an_event():
        try:
            asyncio.get_running_loop().create_task(emit_event(d))
        except Exception:
            pass
_HYBRID_SEARCH_QUERY_MAX_CHARS = 600

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
        # Postgres and the hybrid update path legitimately hydrate timestamps
        # as datetime objects. Chroma metadata accepts scalar JSON values only;
        # normalize both timestamps here so store and metadata-only update use
        # the same safe representation.
        def _chroma_timestamp(value: Any) -> str:
            return value.isoformat() if hasattr(value, "isoformat") else str(value)

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
            "created_at":   _chroma_timestamp(self.created_at),
            "updated_at":   _chroma_timestamp(self.updated_at),
            "content_hash": self.content_hash,
            "summary":      self.summary[:500],
            "parent_id":    self.parent_id,
        }
        # Truncate, never skip: an oversized doc would trip chroma's request-
        # size limit (413) and silently lose the vector. full_text stays whole
        # in Postgres; embeddings are computed from ≤4096 chars regardless.
        doc_text = (self.text or self.summary or self.full_text[:500])[:4000]
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

_PG_STARTUP_LOCK_TIMEOUT = os.environ.get("VERA_PG_STARTUP_LOCK_TIMEOUT", "3s")


async def _pg_try(conn, sql: str) -> bool:
    """One best-effort start-up statement: a lock timeout or any other failure skips it (it runs next start-up)."""
    try:
        await conn.execute(sql)
        return True
    except Exception as e:
        log.debug("PG start-up statement skipped (%s): %s", sql[:60], e)
        return False


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
                # Start-up schema work never waits on a lock. 2026-10-05: prod restarted during the nightly pg_dump,
                # whose share locks made this connect's no-op ALTER TABLE wait, and every read of vera_memory_edges
                # queued behind that ALTER - memory came up 8 min late with nothing in recall until then. A migration
                # that cannot get its lock in time is skipped (each is best-effort) and runs at the next start-up.
                await conn.execute(f"SET lock_timeout = '{_PG_STARTUP_LOCK_TIMEOUT}'")
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
                await _pg_try(conn, "CREATE INDEX IF NOT EXISTS vm_session ON vera_memories(session_id)")
                await _pg_try(conn, "CREATE INDEX IF NOT EXISTS vm_type ON vera_memories(record_type)")
                await _pg_try(conn, "CREATE INDEX IF NOT EXISTS vm_category ON vera_memories(category)")
                await _pg_try(conn, "CREATE INDEX IF NOT EXISTS vm_created ON vera_memories(created_at DESC)")
                await _pg_try(conn, "CREATE INDEX IF NOT EXISTS vm_fts ON vera_memories USING gin(search_vector)")
                await _pg_try(conn, "CREATE INDEX IF NOT EXISTS vm_tags ON vera_memories USING gin(tags)")
                await _pg_try(conn, "CREATE INDEX IF NOT EXISTS vm_hash ON vera_memories(content_hash)")
                # the canonical record of a text, found as each memory is written (memory_canonical_core)
                await _pg_try(conn, "CREATE INDEX IF NOT EXISTS vm_text_key ON vera_memories ((metadata->>'text_key')) "
                                    "WHERE metadata ? 'text_key'")
                await _pg_try(conn, "CREATE INDEX IF NOT EXISTS vme_from ON vera_memory_edges(from_id)")
                await _pg_try(conn, "CREATE INDEX IF NOT EXISTS vme_to   ON vera_memory_edges(to_id)")
                await _pg_try(conn, "CREATE INDEX IF NOT EXISTS vme_sess ON vera_memory_edges(session_id)")
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
            # faded_only: the soft-archived records the fading job set aside (metadata.faded_at) - searched only to let a
            # strong match surface again (owner, 2026-10-05); everything else reads live records only
            conditions = (["archived = TRUE", "metadata ? 'faded_at'"] if (filters or {}).get("faded_only")
                          else ["archived = FALSE"])
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
                    elif k == "exclude_record_types" and v:
                        conditions.append(f"record_type <> ALL(${i}::text[])")
                        params.append([str(x) for x in v]); i += 1
                    elif k == "exclude_transcripts" and v:
                        # imported coding-agent transcript lines (recall_core.is_transcript); LIKE treats '[' literally
                        conditions.append("NOT (text LIKE '[Claude Code%' OR text LIKE '[Codex%')")
                    elif k in ("since", "until") and v:
                        # the time the question asks about (recall_scope_core)
                        conditions.append(f"created_at {'>=' if k == 'since' else '<'} ${i}")
                        params.append(_as_dt(v)); i += 1
            where = " AND ".join(conditions)
            if query.strip():
                params.append(query)
                sql = f"""
                    SELECT *, ts_rank(search_vector, plainto_tsquery('english', ${i})) AS score
                    FROM vera_memories
                    WHERE {where}
                      AND search_vector @@ plainto_tsquery('english', ${i})
                      AND NOT (metadata ? 'canonical_id')
                    ORDER BY (metadata ? 'noise'), score DESC, importance DESC
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
        self._skipped_no_embedding = 0
        self._count_cache: Tuple[float, int] = (0.0, 0)

    def _cached_count(self) -> int:
        """count() with a 30s TTL — search() needed it on every call purely to
        clamp n_results, which cost an extra HTTP roundtrip per query."""
        ts, n = self._count_cache
        now = time.monotonic()
        if now - ts > 30.0:
            n = self._collection.count()
            self._count_cache = (now, n)
        return n

    async def connect(self) -> bool:
        if not HAS_CHROMA:
            log.warning("chromadb not installed — ChromaBackend unavailable")
            return False
        try:
            def _connect_sync():
                # a failed attempt must not leave its client's connection open (RF-82)
                with _chroma_bound.discard_on_failure():
                    client = _chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
                    _chroma_bound.bound(client)      # finite timeout - RF-77
                    coll = client.get_or_create_collection(
                        name=CHROMA_COLLECTION,
                        metadata={"hnsw:space": "cosine"},
                    )
                    return client, coll, coll.count()
            self._client, self._collection, count = \
                await _chroma_bound.run(_connect_sync)
            log.info("✓ ChromaBackend connected — %d documents", count)
            return True
        except Exception as e:
            log.error("ChromaBackend connect: %s", e)
            return False

    async def disconnect(self):
        pass  # ChromaDB HTTP client is stateless

    async def store(self, record: MemoryRecord) -> bool:
        if not self._collection: return False
        if not record.embedding:
            # NEVER upsert without an explicit vector: Chroma falls back to its
            # built-in default embedder (MiniLM, 384-dim), which either raises
            # "expecting embedding with dimension of N, got M" against this
            # model-versioned collection or quietly poisons it with vectors from
            # a different space. The record is safe in Postgres — re-encode it
            # later with memory.backfill_vectors.
            self._skipped_no_embedding += 1
            n = self._skipped_no_embedding
            if n == 1 or n % 100 == 0:
                log.warning("Chroma store: %d record(s) skipped (no embedding — "
                            "embedder offline?). Run memory.backfill_vectors once "
                            "the embedder is back.", n)
            return False
        try:
            rid, doc, meta = record.to_chroma_doc()
            # chromadb HttpClient is synchronous — keep its network round-trips
            # off the event loop, on Chroma's OWN pool (same for every call site in
            # this class): a slow Chroma once parked all 16 default-pool threads (RF-77).
            await _chroma_bound.run(
                self._collection.upsert,
                ids=[rid], documents=[doc], metadatas=[meta],
                embeddings=[record.embedding],
            )
            return True
        except Exception as e:
            log.error("Chroma store: %s", e); return False

    async def get(self, record_id: str) -> Optional[MemoryRecord]:
        if not self._collection: return None
        try:
            res = await _chroma_bound.run(
                self._collection.get,
                ids=[record_id], include=["documents","metadatas"])
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
            where_doc = None
            explicit = False            # a caller's own filter: only the server can apply it
            drop_types: set = set()
            if filters:
                for k, v in (filters or {}).items():
                    if k in ("session_id","record_type","source_type","category","language"):
                        clauses.append({k: {"$eq": v}})
                        explicit = True
                    elif k == "exclude_record_types" and v:
                        clauses.append({"record_type": {"$nin": [str(x) for x in v]}})
                        drop_types = {str(x) for x in v}
                    elif k == "exclude_transcripts" and v:
                        where_doc = {"$and": [{"$not_contains": "[Claude Code"}, {"$not_contains": "[Codex"}]}

            # Chroma requires $and when there are multiple conditions;
            # a single-element list must be unwrapped to avoid the
            # "Expected where to have exactly one operator" error.
            if len(clauses) == 0:
                where_arg = None
            elif len(clauses) == 1:
                where_arg = clauses[0]
            else:
                where_arg = {"$and": clauses}

            # the time the question asks about: Chroma cannot range over its string dates, so hits are kept here
            win = ((filters or {}).get("since"), (filters or {}).get("until"))
            if win[0] or win[1]:
                lo = _as_dt(win[0]).isoformat() if win[0] else ""
                hi = _as_dt(win[1]).isoformat() if win[1] else "~"
                inner_filters = {k: v for k, v in (filters or {}).items() if k not in ("since", "until")}
                hits = await self.search(query, limit=limit * 3, filters=inner_filters, embedding=embedding)
                return [(r, s) for r, s in hits if lo <= str(r.created_at or "") < hi][:limit]
            if not embedding:
                # No query vector (embedder offline / circuit-breaker cooling).
                # query_texts would embed with Chroma's default 384-dim function
                # and dim-mismatch against the model-versioned collection — skip
                # vector search; Postgres full-text + Neo4j still serve the query.
                log.debug("Chroma search skipped — no query embedding available")
                return []
            total = await _chroma_bound.run(self._cached_count) or 1
            want = min(int(limit), total)
            if not explicit:
                fast = await self._search_unfiltered(embedding, want, total, drop_types, where_doc is not None)
                if fast is not None:
                    CHROMA_PATHS["unfiltered"] += 1
                    return fast
                CHROMA_PATHS["fallback"] += 1
            else:
                CHROMA_PATHS["filtered"] += 1
            kwargs: dict = {
                "n_results": want,
                "where":     where_arg,
                "where_document": where_doc,
                "include":   ["documents","metadatas","distances"],
                "query_embeddings": [embedding],
            }
            try:
                res = await _chroma_bound.run(
                    lambda: self._collection.query(
                        **{k: v for k, v in kwargs.items() if v is not None}))
            except Exception as e:
                if where_doc is None:
                    raise
                # a server that rejects the document filter: query without it, leave transcripts out below
                log.debug("Chroma where_document refused (%s) - filtering transcripts after the query", e)
                kwargs["where_document"] = None
                res = await _chroma_bound.run(
                    lambda: self._collection.query(
                        **{k: v for k, v in kwargs.items() if v is not None}))
            results = []
            for rid, doc, meta, dist in zip(
                res["ids"][0], res["documents"][0],
                res["metadatas"][0], res["distances"][0],
            ):
                if where_doc is not None and _recall_core.is_transcript(doc):
                    continue
                score = max(0.0, 1.0 - dist)  # cosine distance → similarity
                results.append((self._chroma_to_record(rid, doc, meta), score))
            return results
        except Exception as e:
            log.error("Chroma search: %s", e); return []

    async def _search_unfiltered(self, embedding, want: int, total: int, drop_types: set,
                                 drop_transcripts: bool) -> Optional[List[Tuple[MemoryRecord, float]]]:
        """Nearest `want` hits with the default exclusions applied here, not by the server.
        None when fewer than `want` survive an over-fetch that did not reach the whole
        collection - the caller then runs the filtered query."""
        n = min(total, max(want * max(1, _CHROMA_OVERFETCH), want + 10))
        res = await _chroma_bound.run(
            lambda: self._collection.query(query_embeddings=[embedding], n_results=n,
                                           include=["documents", "metadatas", "distances"]))
        ids = res["ids"][0]
        out: List[Tuple[MemoryRecord, float]] = []
        for rid, doc, meta, dist in zip(ids, res["documents"][0], res["metadatas"][0], res["distances"][0]):
            meta = meta or {}
            if meta.get("archived") in (True, 1, "true", "True"):
                continue
            if drop_types and str(meta.get("record_type", "")) in drop_types:
                continue
            if drop_transcripts and _recall_core.is_transcript(doc):
                continue
            out.append((self._chroma_to_record(rid, doc, meta), max(0.0, 1.0 - dist)))
            if len(out) >= want:
                return out
        # every vector was read (a small collection, or fewer came back): nothing more to find
        return out if (n >= total or len(ids) < n) else None

    async def update(self, record_id: str, updates: Dict) -> bool:
        if not self._collection: return False
        try:
            current = await self.get(record_id)
            if not current: return False
            for k, v in updates.items():
                if hasattr(current, k): setattr(current, k, v)
            rid, _doc, meta = current.to_chroma_doc()
            # Metadata-only update: passing documents (or routing through
            # store(), which now requires an embedding) would re-embed via the
            # collection's default 384-dim function. update() with metadatas
            # alone preserves the stored vector and document.
            await _chroma_bound.run(
                self._collection.update, ids=[rid], metadatas=[meta])
            return True
        except Exception as e:
            log.error("Chroma update: %s", e); return False

    async def stats(self) -> Dict:
        if not self._collection:
            return {"connected": False}
        try:
            return {"connected": True, "collection": CHROMA_COLLECTION,
                    "count": await _chroma_bound.run(self._collection.count)}
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
            # Drop the half-open driver (it was assigned before RETURN 1) so the
            # backend reports a clean disconnected state and a later reconnect
            # attempt starts fresh.
            if self._driver is not None:
                try: await self._driver.close()
                except Exception: pass
                self._driver = None
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
                "noise":        bool((record.metadata or {}).get("noise")),   # ranks last (memory_noise_core)
                # an occurrence of another record's text; Neo4j drops a null property, so a canonical has none
                "canonical_id": (record.metadata or {}).get("canonical_id") or None,
            }
            async with self._driver.session() as s:
                await s.run(
                    "MERGE (m:Memory {id:$id}) SET m += $props",
                    id=record.id, props=props,
                )

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
                elif k == "exclude_record_types" and v:
                    where_parts.append("NOT coalesce(m.record_type, '') IN $exclude_record_types")
                    params["exclude_record_types"] = [str(x) for x in v]
                elif k == "exclude_transcripts" and v:
                    where_parts.append("NOT (coalesce(m.text, '') STARTS WITH '[Claude Code' "
                                       "OR coalesce(m.text, '') STARTS WITH '[Codex')")
                elif k in ("since", "until") and v:
                    # ISO strings compare in time order (recall_scope_core)
                    where_parts.append(f"coalesce(m.created_at, '') {'>=' if k == 'since' else '<'} ${k}")
                    params[k] = _as_dt(v).isoformat()
            where = " AND ".join(where_parts)

            if query.strip():
                # a long question keeps only its content terms (fusion_core.lexical_query)
                params["query"] = _lucene_escape(_fusion_core.lexical_query(query))
                # A candidate limit for Lucene: without it every matching node is scored before the
                # WHERE and LIMIT below (live 2026-10-05: a 36-word chat message, 377-932 ms; with
                # {limit: 300}, 35-81 ms and the identical top results). Generous, because the WHERE
                # drops excluded kinds after it.
                params["cand"] = max(_NEO_FULLTEXT_MIN_CANDIDATES, int(limit) * _NEO_FULLTEXT_CANDIDATES_PER_HIT)
                cypher = f"""
                    CALL db.index.fulltext.queryNodes('vera_mem_text', $query, {{limit: $cand}})
                    YIELD node AS m, score
                    WHERE {where} AND m.canonical_id IS NULL
                    RETURN m, score ORDER BY coalesce(m.noise, false), score DESC LIMIT $limit
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

_EMBED_FAILED_AT  = 0.0     # monotonic ts of the last hard failure (0 = healthy)
_EMBED_RETRY_SECS = 300.0   # circuit-breaker cooldown before probing the cluster again
# Wait budget for ONE embed. A search or store must not sit behind a busy
# embed node: past this the caller carries on without a vector — the search
# answers from Postgres + Neo4j, the store skips the Chroma write (backfill
# later with memory.backfill_vectors). The embed itself is NOT cancelled
# (query_embed_core.bounded_embed_result): the node finishes it and the vector
# lands in ollama_embed's cache for the next identical text. Live on prod
# 2026-09-13: the chat page's memory chip waited 10-135 s per embed behind
# Vera's own fabric-ingest traffic, which is what made chat "slow with memory
# on".
_EMBED_WAIT_S = float(os.getenv("VERA_EMBED_WAIT_S", "5") or 5)
# After a timeout, skip embeds outright for this long rather than making
# every caller pay the full wait again while the node is still backed up.
_EMBED_SLOW_COOLDOWN_S = float(os.getenv("VERA_EMBED_SLOW_COOLDOWN_S", "30") or 30)
_EMBED_SLOW_UNTIL = 0.0     # monotonic deadline of the current slow-cooldown (0 = none)

# A degraded backing store must not hold every memory caller indefinitely.
# This applies after the separately bounded embedding phase.  Individual
# backends still own their internal retries; the hybrid layer owns the latency
# budget and isolates a provider that fails to return.
_BACKEND_OP_TIMEOUT_S = max(
    0.1, float(os.getenv("VERA_MEMORY_BACKEND_TIMEOUT_S", "10") or 10))

async def embed_text_outcome(text: str) -> Tuple[Optional[List[float]], str]:
    """Generate a text embedding via the centralized ollama_embed (logged to Jobs).

    Circuit-breaker with COOLDOWN: after a hard failure we stop hammering the
    cluster, but only for _EMBED_RETRY_SECS. A permanently-latched breaker meant
    one bad window (embed node rebooting, boot race before the cluster monitor
    marks nodes online) silently disabled vectors for the whole process lifetime
    — records piled up in Postgres with no Chroma vector.
    """
    global _EMBED_FAILED_AT, _EMBED_SLOW_UNTIL
    if not MEMORY_AUTO_EMBED:
        return None, "skipped:off"
    if not text.strip():
        # an empty query is a listing (newest records), not a search: nothing to embed. Labelled apart from
        # "off" so retrieval.timing does not read listings as the vector leg being disabled (FM-0.4, 2026-10-09)
        return None, "skipped:empty"
    if _EMBED_FAILED_AT and (time.monotonic() - _EMBED_FAILED_AT) < _EMBED_RETRY_SECS:
        return None, "skipped:breaker"
    if _EMBED_SLOW_UNTIL and time.monotonic() < _EMBED_SLOW_UNTIL:
        return None, "skipped:cooldown"
    try:
        from Vera.vera.capability_orchestration import ollama_embed
        from Vera.vera.dag.query_embed_core import bounded_embed_result
        vec, timed_out = await bounded_embed_result(
            lambda: ollama_embed(text, model=OLLAMA_EMBED_MODEL, normalize=_EMBED_NORMALIZE),
            _EMBED_WAIT_S)
        if timed_out:
            first = not _EMBED_SLOW_UNTIL or time.monotonic() >= _EMBED_SLOW_UNTIL
            _EMBED_SLOW_UNTIL = time.monotonic() + _EMBED_SLOW_COOLDOWN_S
            if first:
                log.warning(
                    "embed_text: no vector within %.1fs (embed node backed up) — "
                    "carrying on without embeddings for %ds; searches use "
                    "Postgres + Neo4j, stores skip Chroma (memory.backfill_vectors "
                    "fills them in later)", _EMBED_WAIT_S, int(_EMBED_SLOW_COOLDOWN_S))
            return None, "timeout"
        _EMBED_SLOW_UNTIL = 0.0
        if vec is None:
            first = not _EMBED_FAILED_AT
            _EMBED_FAILED_AT = time.monotonic()
            if first:
                log.warning(
                    "Embedding unavailable — model '%s' may not be pulled or no "
                    "Ollama node is online. Retrying every %ds. Run: ollama pull %s "
                    "OR set MEMORY_AUTO_EMBED=0 to disable. Backfill skipped "
                    "vectors later with memory.backfill_vectors.",
                    OLLAMA_EMBED_MODEL, int(_EMBED_RETRY_SECS), OLLAMA_EMBED_MODEL
                )
        else:
            _EMBED_FAILED_AT = 0.0
        return vec, ("ok" if vec is not None else "unavailable")
    except Exception as e:
        _EMBED_FAILED_AT = _EMBED_FAILED_AT or time.monotonic()
        log.debug("embed_text: %s", e)
        return None, "error"


async def embed_text(text: str) -> Optional[List[float]]:
    """The query/record vector, or None. embed_text_outcome also says why there is none
    (skipped:off|empty|breaker|cooldown, timeout, unavailable, error) - the timing envelopes
    need that to tell "no vector" apart from "nothing found"."""
    vec, _ = await embed_text_outcome(text)
    return vec


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
        # Backends that fail their FIRST connect are kept registered (not
        # deleted) and retried in the background — a slow-starting service like
        # Neo4j (community can take 20–40s) loses the boot race otherwise and
        # would stay "disconnected" for the whole process life every reboot.
        self._disconnected: List[str] = []
        for name, backend in list(self._backends.items()):
            try:
                ok = await backend.connect()
            except Exception as e:
                ok = False
                log.warning("Backend %s connect raised: %s", name, e)
            if not ok:
                log.warning("Backend %s failed initial connect — will retry in background", name)
                self._disconnected.append(name)
        active = [n for n in self._backends if n not in self._disconnected]
        self._ready = bool(active)
        log.info("HybridMemoryStore ready with backends: %s%s", active,
                 (" (retrying: %s)" % self._disconnected) if self._disconnected else "")
        await self._emit_backend_status()
        if self._disconnected:
            asyncio.ensure_future(self._reconnect_loop())

    async def _emit_backend_status(self):
        active = [n for n in self._backends if n not in getattr(self, "_disconnected", [])]
        await emit_event({"type": "backend.status",
                          "active": active,
                          "missing": [b for b in ["postgres","chroma","neo4j"] if b not in active]})

    async def _reconnect_loop(self):
        """Retry backends that failed their initial connect until each succeeds.
        Bounded backoff (10s → cap 5min) so a genuinely-absent service doesn't spam."""
        delay = 10
        while getattr(self, "_disconnected", None):
            await asyncio.sleep(delay)
            for name in list(self._disconnected):
                backend = self._backends.get(name)
                if backend is None:
                    self._disconnected.remove(name)
                    continue
                try:
                    ok = await backend.connect()
                except Exception as e:
                    ok = False
                    log.debug("reconnect %s failed: %s", name, e)
                if ok:
                    self._disconnected.remove(name)
                    self._ready = True
                    log.info("✓ Backend %s reconnected (was slow to start)", name)
                    await self._emit_backend_status()
            delay = min(delay * 2, 300)

    async def shutdown(self):
        for b in self._backends.values():
            await b.disconnect()

    @property
    def backends(self) -> List[str]:
        return list(self._backends.keys())

    async def repair_get(self, backend_name: str, record_id: str) -> Optional[MemoryRecord]:
        """Read one exact backend for content-verified reconciliation."""
        backend = self._backends.get(str(backend_name or ""))
        if backend is None:
            raise ValueError("unknown memory backend")
        return await asyncio.wait_for(
            backend.get(str(record_id or "")), _BACKEND_OP_TIMEOUT_S)

    async def repair_store(self, backend_name: str, record: MemoryRecord) -> bool:
        """Rebuild one exact backend without invoking the normal fan-out path."""
        backend_name = str(backend_name or "")
        backend = self._backends.get(backend_name)
        if backend is None:
            raise ValueError("unknown memory backend")
        if _sbx_write_blocked():
            _sbx_note("memory")
            return False
        expected_hash = record.compute_hash()
        if record.content_hash and record.content_hash != expected_hash:
            raise ValueError("memory repair content hash mismatch")
        record.content_hash = expected_hash
        if not record.keywords:
            record.keywords = _extract_keywords(record.full_text or record.text)
        if not record.text and record.full_text:
            record.text = record.full_text[:500]
        if backend_name == "chroma" and not record.embedding:
            if not MEMORY_AUTO_EMBED:
                return False
            embedding = await embed_text(record.text or record.summary)
            if not embedding:
                return False
            record.embedding = embedding
            record.embedding_model = OLLAMA_EMBED_MODEL
        result = await asyncio.wait_for(
            backend.store(record), _BACKEND_OP_TIMEOUT_S)
        return result is True

    async def store(self, record: MemoryRecord) -> Dict:
        """Store a record across all backends. Returns per-backend success map."""
        # Dev-sandbox write guard: the memory backends (Postgres/Chroma/Neo4j)
        # are prod's SHARED stores, so a dev loop storing memories would pollute
        # prod. Suppress the fan-out entirely (strict no-op in prod). The record
        # still lives in the sandbox's isolated Redis/SQLite fabric elsewhere.
        if _sbx_write_blocked():
            _sbx_note("memory")
            # A protected no-op is not a successful persistence receipt. False
            # keeps callers from reporting data as stored when no backend saw
            # it; the write guard remains fail-closed and side-effect free.
            return {name: False for name in self._backends}
        # Fill in derived fields
        if not record.content_hash:
            record.content_hash = record.compute_hash()
        if not record.keywords:
            record.keywords = _extract_keywords(record.full_text or record.text)
        if not record.text and record.full_text:
            record.text = record.full_text[:500]

        # Generate embedding (shared across backends) - but not for the kinds recall leaves out (tool calls, imported
        # transcript lines): they are never searched by meaning, and every such embed queued on the shared embed nodes
        # ahead of chat's own query embeds, which then timed out (2026-10-04, EVIDENCE E1/E12). They go to Postgres and
        # Neo4j as before; Chroma is not asked, so no "embedder offline" warning is raised for a skip by design.
        worthy = _recall_core.embed_worthy(record.record_type, record.text or record.full_text,
                                           _RECALL_EXCLUDE_TYPES, _RECALL_EXCLUDE_TRANSCRIPTS)
        # Noise (status lines, prompt scaffolding, injected context, raw tool JSON) is kept and marked, so every
        # backend ranks it after real memories; it is not embedded - nothing should find it by meaning.
        why = _noise_core.noise_reasons(record)
        if why:
            record.metadata = dict(record.metadata or {}, noise=why)
            worthy = False
        # The same text as a live record of the same type: kept, as an occurrence of that canonical record, which is
        # the one recall returns (memory_canonical_core). Not embedded - the canonical's vector already stands for it.
        canon = ""
        if _canon_core.eligible(record):
            key = _canon_core.text_key(record.text or record.full_text)
            if key:
                record.metadata = dict(record.metadata or {}, text_key=key)
                canon = await self._canonical_for(key, record.record_type, record.id)
                if canon:
                    record.metadata["canonical_id"] = canon
                    worthy = False
        if worthy and not record.embedding and MEMORY_AUTO_EMBED:
            emb = await embed_text(record.text or record.summary)
            if emb:
                record.embedding       = emb
                record.embedding_model = OLLAMA_EMBED_MODEL

        # Fan out to all backends concurrently
        tasks = {
            name: asyncio.wait_for(b.store(record), _BACKEND_OP_TIMEOUT_S)
            for name, b in self._backends.items()
            if worthy or name != "chroma"
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        outcome = {}
        for name, res in zip(tasks.keys(), results):
            if isinstance(res, asyncio.CancelledError):
                raise res
            if isinstance(res, TimeoutError):
                log.warning("Memory backend %s store timed out after %.1fs",
                            name, _BACKEND_OP_TIMEOUT_S)
            outcome[name] = res if isinstance(res, bool) else False
        if canon:
            try:                            # the occurrence stays reachable: a path from it to the canonical
                await asyncio.wait_for(self.relate(record.id, canon, "OCCURRENCE_OF",
                                                   {"session_id": record.session_id}), _BACKEND_OP_TIMEOUT_S)
            except Exception as e:
                log.debug("occurrence link %s -> %s: %s", record.id, canon, e)

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

    async def _canonical_for(self, key: str, record_type: str, record_id: str, timeout: float = 0.3) -> str:
        """The live canonical record with this text key and type, or "" (none, or Postgres slow - the record is then
        stored as its own canonical and a later memory.canonical.mark run folds it in). Never raises."""
        pool = getattr(self._backends.get("postgres"), "_pool", None)
        if pool is None:
            return ""
        try:
            async def _q():
                async with pool.acquire() as conn:
                    return await conn.fetchval(
                        "SELECT id FROM vera_memories WHERE metadata->>'text_key' = $1 AND record_type = $2 "
                        "AND NOT archived AND NOT (metadata ? 'canonical_id') AND id <> $3 "
                        "ORDER BY created_at, id LIMIT 1", key, record_type or "", record_id or "")
            return str(await asyncio.wait_for(_q(), timeout) or "")
        except Exception:
            return ""

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
        # Cap query length HERE, at the single entry point every backend and
        # the embedder both go through — callers occasionally pass something
        # far bigger than a search query (a chat/loop caller's full turn
        # text, which for an agentic-loop specialist step can run to tens of
        # thousands of characters). Two live-confirmed costs scale with that:
        # (1) embed_text() below tokenizes + embeds the WHOLE string before
        # any backend even starts — the dominant cost for a long query, and
        # (2) Neo4jBackend.search()'s fulltext index treats each whitespace
        # -separated word as its own Lucene OR-clause; past the 1024-clause
        # cap it doesn't just get slow, it throws outright ("TooManyClauses:
        # maxClauseCount is set to 1024" — live-caught 2026-08-11, a
        # memory-enabled chat pre-request delay of 7+s per the orchestrator's
        # own "slow to appear in the queue" warning, on runs that reported
        # minutes of pause before the LLM even started). A search QUERY
        # doesn't need to be more than a couple hundred characters to convey
        # its intent — cap it once here rather than trust every caller (and
        # every future one) to do it themselves.
        query = (query or "")[:_HYBRID_SEARCH_QUERY_MAX_CHARS]
        filters = _recall_core.default_recall_filters(filters, _RECALL_EXCLUDE_TYPES, _RECALL_EXCLUDE_TRANSCRIPTS)
        active = {k: v for k, v in self._backends.items()
                  if backends is None or k in backends}
        # Only Chroma's vector search actually reads `embedding` — Postgres
        # (plainto_tsquery) and Neo4j (fulltext index) both accept and ignore
        # it. embed_text() is the dominant cost here (3.5-8.5s on this
        # instance's shared embed nodes) and was being paid unconditionally
        # even for callers who explicitly restrict `backends` to exclude
        # chroma — pure waste. Skip it when nothing active needs it.
        env = _rtc.Envelope("hybrid.search")
        embedding = None
        if "chroma" in active:
            embedding, embed_outcome = await embed_text_outcome(query)
            env.stage("embed", env.mark(), embed_outcome)
        tasks = {
            name: _rtc.timed(asyncio.wait_for(
                b.search(query, limit=limit * _CANDIDATES_PER_HIT, filters=filters, embedding=embedding),
                _BACKEND_OP_TIMEOUT_S,
            ))
            for name, b in active.items()
        }
        all_results = []
        for name, (result, ms) in zip(tasks.keys(), await asyncio.gather(*tasks.values())):
            if isinstance(result, TimeoutError):
                log.warning("Memory backend %s search timed out after %.1fs",
                            name, _BACKEND_OP_TIMEOUT_S)
            if name == "chroma" and embedding is None:
                env.stage(name, ms, "skipped:no-vector")
            else:
                env.stage(name, ms, _rtc.leg_outcome(result),
                          None if isinstance(result, BaseException) else len(result))
            all_results.append(result)
        env.mark()
        out = self._merge_and_rank(tasks.keys(), all_results, limit, query)
        env.stage("merge", env.mark(), "ok" if out else "empty", len(out))
        _record_timing(env)
        return out

    def _merge_and_rank(self, names, all_results, limit: int, query: str = "") -> List[Dict]:
        """Merge backend hits by record id and rank them.

        Two or more backends on a real query: each score goes onto 0..1 and backends that
        found the same record combine (memory_score_core), so the score every consumer
        thresholds is one scale (RF-05). One backend, or an empty query (importance
        listing): the backend's own score, as before."""
        names = list(names)
        merged: Dict[str, Tuple[MemoryRecord, float]] = {}
        lists: Dict[str, List[Tuple[str, float]]] = {}
        for name, results in zip(names, all_results):
            if isinstance(results, Exception): continue
            lists[name] = [(rec.id, score) for rec, score in results]
            for rec, score in results:
                if rec.id not in merged or merged[rec.id][1] < score:
                    merged[rec.id] = (rec, score)
        if len(names) > 1 and (query or "").strip():
            fused = _memory_score_core.combine(lists)
            merged = {rid: (rec, fused.get(rid, 0.0)) for rid, (rec, _s) in merged.items()}
        return self._rank_pairs(merged.values(), limit)

    @staticmethod
    def _rank_pairs(pairs, limit: int) -> List[Dict]:
        """Best first, noise after everything else (memory_noise_core: status lines, prompt scaffolding, injected
        context, raw tool JSON - kept, findable, never ahead of a real memory), then one hit per repeated text
        (recall_core.collapse_repeats), then the limit - so `limit` hits are `limit` different texts, each saying how
        many copies it stood for, and a noisy hit says why it ranked last."""
        rows = []
        for r, s in pairs:
            why = _noise_core.noise_reasons(r) if _RECALL_NOISE_LAST else []
            rows.append((bool(why), -(s * (0.5 + 0.5 * r.importance)), r, s, why))
        rows.sort(key=lambda x: (x[0], x[1]))
        hits = []
        for _n, _k, r, s, why in rows:
            hit = {"record": r.to_dict(), "score": round(s, 4)}
            if why:
                hit["noise"] = why
            hits.append(hit)
        return _recall_core.collapse_repeats(hits, limit=int(limit))

    _FADED_SEEN: Dict[str, float] = {"any": 0.0, "t": 0.0}

    async def _any_faded(self) -> bool:
        """Has the fading job set anything aside? Asked of Postgres at most every 10 minutes, so a store with nothing
        faded never pays for the extra query."""
        now = time.monotonic()
        if now - self._FADED_SEEN["t"] < 600:
            return bool(self._FADED_SEEN["any"])
        pg = self._backends.get("postgres")
        pool = getattr(pg, "_pool", None)
        any_ = False
        if pool is not None:
            try:
                async with pool.acquire() as conn:
                    any_ = bool(await conn.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM vera_memories WHERE archived AND metadata ? 'faded_at')"))
            except Exception:
                any_ = False
        self._FADED_SEEN.update(any=1.0 if any_ else 0.0, t=now)
        return any_

    async def _with_resurfaced(self, query: str, limit: int, filters: Optional[Dict], names: List[str],
                               results: List[Any], timeout: float = 0.5) -> List[Any]:
        """Faded records that match at least as strongly as the best live Postgres hit join that backend's hits,
        marked resurfaced (recall_core.resurface). Never slower than `timeout`; never raises."""
        try:
            if not (query or "").strip() or "postgres" not in names or not await self._any_faded():
                return results
            idx = names.index("postgres")
            live = results[idx] if isinstance(results[idx], list) else []
            pg = self._backends["postgres"]
            faded = await asyncio.wait_for(
                pg.search(query, limit=max(3, limit), filters=dict(filters or {}, faded_only=True)), timeout)
            keep = _recall_core.resurface(live, faded)
            for rec, _sc in keep:
                try:
                    rec.metadata = dict(rec.metadata or {}, resurfaced=True)
                except Exception:
                    pass
            if keep:
                results = list(results)
                results[idx] = list(live) + list(keep)
        except Exception as e:
            log.debug("resurface: %s", e)
        return results

    async def _time_tier(self, query: str, limit: int, filters: Optional[Dict], timeout: float = 1.0):
        """(query, filters, tier) to search with. A question that names a time ("yesterday", "last week", "in
        March") is searched there with the phrase taken out of the text query - the text search wants every word, so
        "yesterday" itself found nothing - then in a window four times as wide, then over all time (recall_scope_core;
        CU-M.9): a tier stands when it finds at least min(limit, 3). A caller's own window is left alone."""
        f = dict(filters or {})
        if f.get("since") or f.get("until") or f.get("_unscoped"):
            return query, filters, None
        sc = _scope_core.time_scope(query)
        if not sc:
            return query, filters, None
        q = _scope_core.strip(query, sc["phrase"])
        need = max(1, min(int(limit or 1), 3))
        for tier in (sc, _scope_core.widen(sc)):
            tf = dict(f, since=tier["since"], until=tier["until"])
            try:
                hits = await self.search_fast(q, limit=need, filters=dict(tf, _unscoped=True), timeout=timeout)
            except Exception:
                hits = []
            if len(hits) >= need:
                return q, tf, tier["kind"]
        return q, f, "all time"

    async def search_fast(
        self,
        query:   str,
        limit:   int             = 10,
        filters: Optional[Dict]  = None,
        timeout: float           = 2.5,
    ) -> List[Dict]:
        """The fast tier alone: Postgres + Neo4j, no embedding, bounded by `timeout`.

        For callers that only look (a live preview of what a turn would recall): unlike search_fast_and_slow it
        starts no embed and leaves no background task behind, so calling it on every keystroke costs nothing on
        the shared embed nodes."""
        query = (query or "")[:_HYBRID_SEARCH_QUERY_MAX_CHARS]
        query, filters, _tier = await self._time_tier(query, limit, filters, min(timeout, 1.0))
        filters = _recall_core.default_recall_filters(filters, _RECALL_EXCLUDE_TYPES, _RECALL_EXCLUDE_TRANSCRIPTS)
        names = [n for n in ("postgres", "neo4j") if n in self._backends]
        if not names:
            return []
        env = _rtc.Envelope("hybrid.fast_only")
        tasks = {n: _rtc.timed(self._backends[n].search(query, limit=limit * _CANDIDATES_PER_HIT,
                                                        filters=filters, embedding=None))
                 for n in names}
        results = self._fast_legs(env, list(tasks.keys()), await self._gather_fast(tasks, timeout), timeout)
        results = await self._with_resurfaced(query, limit, filters, list(tasks.keys()), results)
        env.mark()
        out = self._merge_and_rank(tasks.keys(), results, limit, query)
        env.stage("merge", env.mark(), "ok" if out else "empty", len(out))
        _record_timing(env)
        return out

    @staticmethod
    async def _gather_fast(tasks, timeout: float):
        """The timed fast-tier legs under one bound: each leg that finished as its (result, ms), a leg still running
        at the bound cancelled and standing as a TimeoutError - one slow backend no longer throws away the other's
        hits (recall_core.gather_within; a deep memory search on prod got 0 hits from two backends that each answer
        in 0.3-0.5 s alone)."""
        return await _recall_core.gather_within(tasks, timeout)

    @staticmethod
    def _fast_legs(env, names, timed_results, timeout: float):
        """Record each fast leg on `env`; return the bare results for the merge."""
        if timed_results is None:
            for n in names:
                env.stage(n, timeout * 1000.0, "timeout")
            return [TimeoutError("fast tier timed out")] * len(names)
        out = []
        for n, entry in zip(names, timed_results):
            if isinstance(entry, BaseException):            # the leg did not finish inside the bound
                env.stage(n, timeout * 1000.0, _rtc.leg_outcome(entry))
                out.append(entry if isinstance(entry, Exception) else TimeoutError(str(entry)))
                continue
            result, ms = entry
            env.stage(n, ms, _rtc.leg_outcome(result),
                      None if isinstance(result, BaseException) else len(result))
            out.append(result)
        return out

    async def search_fast_and_slow(
        self,
        query:   str,
        limit:   int             = 10,
        filters: Optional[Dict]  = None,
        fast_timeout: float      = 2.5,
    ) -> Tuple[List[Dict], "asyncio.Task"]:
        """
        Two-tier search for latency-sensitive callers (chat's memory_inject).

        embed_text() is the dominant cost of a normal search() on this
        instance — live-measured 3.5-8.5s PER CALL on the shared CPU embed
        nodes regardless of query size, because of a near-continuous stream
        of unrelated background embed traffic (fabric ingestion, sandbox
        session syncs) that saturates them. Postgres (plainto_tsquery) and
        Neo4j (fulltext index) searches need NO embedding at all, but the
        plain search() above still makes them wait for one anyway since it
        fetches the embedding before starting ANY backend.

        Returns (fast_results, slow_task):
          fast_results — Postgres + Neo4j hits only, gathered directly and
            capped at fast_timeout (typically well under 1s once index-
            warm). Available immediately for the CURRENT turn.
          slow_task — a background asyncio.Task, already running, that
            awaits the embedding (started in parallel with the fast tier,
            not after it) and folds in a Chroma vector search once ready,
            reusing the fast tier's Postgres/Neo4j hits rather than
            re-querying them. Resolves to the full merged+reranked result
            set. Callers should hand this to a delivery mechanism for the
            NEXT turn (see memory_hooks.get_agent_memory_context_v2) rather
            than await it inline — awaiting it defeats the entire point.
        """
        query = (query or "")[:_HYBRID_SEARCH_QUERY_MAX_CHARS]
        query, filters, _tier = await self._time_tier(query, limit, filters, min(fast_timeout, 1.0))
        filters = _recall_core.default_recall_filters(filters, _RECALL_EXCLUDE_TYPES, _RECALL_EXCLUDE_TRANSCRIPTS)

        # Start the embed NOW, in parallel with the fast tier below — not
        # after it finishes. It's the long pole; every second it's not
        # already running is a second added to the slow tier's total time.
        slow_env = _rtc.Envelope("hybrid.slow")       # starts with the embed
        embed_task = asyncio.create_task(embed_text_outcome(query))

        fast_env = _rtc.Envelope("hybrid.fast")
        fast_names = [n for n in ("postgres", "neo4j") if n in self._backends]
        fast_tasks = {n: _rtc.timed(self._backends[n].search(query, limit=limit * _CANDIDATES_PER_HIT,
                                                             filters=filters, embedding=None))
                      for n in fast_names}
        if fast_tasks:
            fast_all = self._fast_legs(fast_env, fast_names, await self._gather_fast(fast_tasks, fast_timeout),
                                       fast_timeout)
            fast_all = await self._with_resurfaced(query, limit, filters, fast_names, fast_all)
        else:
            fast_all = []
        fast_env.mark()
        fast_out = self._merge_and_rank(fast_tasks.keys(), fast_all, limit, query)
        fast_env.stage("merge", fast_env.mark(), "ok" if fast_out else "empty", len(fast_out))
        _record_timing(fast_env)
        fast_names_run = list(fast_tasks.keys())

        async def _slow() -> List[Dict]:
            try:
                embedding, embed_outcome = await embed_task
            except Exception:
                embedding, embed_outcome = None, "error"
            slow_env.stage("embed", slow_env.mark(), embed_outcome)
            chroma = self._backends.get("chroma")
            chroma_results: List[Tuple[MemoryRecord, float]] = []
            if chroma is None:
                slow_env.stage("chroma", 0.0, "skipped:no-backend")
            elif not embedding:
                slow_env.stage("chroma", 0.0, "skipped:no-vector")
            else:
                result, ms = await _rtc.timed(chroma.search(
                    query, limit=limit * _CANDIDATES_PER_HIT, filters=filters, embedding=embedding))
                slow_env.stage("chroma", ms, _rtc.leg_outcome(result),
                               None if isinstance(result, BaseException) else len(result))
                chroma_results = [] if isinstance(result, BaseException) else result
            # The fast tier's hits are reused, not re-queried; scored on the same scale.
            slow_env.mark()
            out = self._merge_and_rank(fast_names_run + ["chroma"], list(fast_all) + [chroma_results],
                                       limit, query)
            slow_env.stage("merge", slow_env.mark(), "ok" if out else "empty", len(out))
            _record_timing(slow_env)
            return out

        slow_task = asyncio.create_task(_slow())
        return fast_out, slow_task

    async def get(self, record_id: str) -> Optional[MemoryRecord]:
        for b in self._backends.values():
            rec = await b.get(record_id)
            if rec: return rec
        return None

    async def update(self, record_id: str, updates: Dict) -> Dict:
        if _sbx_write_blocked():
            _sbx_note("memory-update")
            return {name: False for name in self._backends}
        updates["updated_at"] = datetime.now(timezone.utc)
        tasks = {
            n: asyncio.wait_for(b.update(record_id, updates), _BACKEND_OP_TIMEOUT_S)
            for n, b in self._backends.items()
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, result in zip(tasks.keys(), results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, TimeoutError):
                log.warning("Memory backend %s update timed out after %.1fs",
                            name, _BACKEND_OP_TIMEOUT_S)
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
                {MEMORY_PROMO_STREAM: last_id}, count=50, block=_orch.STREAM_BLOCK_MS
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
    description="Persist a text record to long-term memory (Postgres + Chroma vector index + Neo4j graph). "
                "WHEN TO USE: save important facts, research results, or context you want to recall in this or future sessions. "
                "Input: text (str!), session_id (str), category (str — label for filtering, e.g. 'research','finding'), "
                "tags (str — comma-sep), importance (float 0.0-1.0, default 0.5), parent_id (str — link to parent record). "
                "Output: {id, keywords, backends, content_hash}. "
                "Use memory.recall or memory.search to retrieve stored records.",
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
    # tags is documented as a comma-separated string, but internal/LLM callers
    # sometimes pass a list — coerce so we don't blow up on .split().
    if isinstance(tags, (list, tuple)):
        tags = ",".join(str(t) for t in tags if t)
    rec = MemoryRecord(
        session_id  = session_id,
        trace_id    = trace_id or trace_id_param or "",
        record_type = record_type,
        source_type = source_type,
        category    = category,
        tags        = [t.strip() for t in str(tags).split(",") if t.strip()],
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
    async def _native_store():
        return await MEMORY.store(rec)

    actor = str(_orch.CALLER_KIND.get("") or "user").strip().lower()
    actor = {"mcp": "claude_code"}.get(actor, actor)
    result = await _revision_canary.store_public_memory_with_revision_canary(
        rec.to_dict(), native_write=_native_store, actor=actor,
        observed_at=now_iso())
    return {"id": rec.id, "backends": result, "keywords": rec.keywords,
            "content_hash": rec.content_hash}


@capability(
    "memory.dual_write.reconcile", memory="off",
    http_method="POST", http_path="/memory/dual-write/reconcile",
    http_tags=["memory"],
    description="Manually reconcile one previously journalled split Memory write. "
                "Disabled unless VERA_MEMORY_RECONCILIATION_ENABLED=1. Repairs only "
                "the receipt's exact unresolved revision or Postgres/Chroma/Neo4j "
                "target, verifies canonical and native content identities, and never "
                "moves read authority. Input: operation_id, attempt_id, timeout_s "
                "(0..300). Output: classified target outcomes and convergence.",
)
async def memory_dual_write_reconcile(
    operation_id: str, attempt_id: str, timeout_s: float = 30.0,
    trace_id=None,
):
    async def load_native(backend_name: str, native_id: str):
        record = await MEMORY.repair_get(backend_name, native_id)
        return record.to_dict() if record is not None else None

    async def write_native(backend_name: str, snapshot: Mapping[str, Any]):
        allowed = {item.name for item in fields(MemoryRecord)}
        record = MemoryRecord(**{
            key: value for key, value in dict(snapshot).items() if key in allowed
        })
        return await MEMORY.repair_store(backend_name, record)

    actor = str(_orch.CALLER_KIND.get("") or "user").strip().lower()
    actor = {"mcp": "claude_code"}.get(actor, actor)
    return await _revision_repair.run_manual_memory_reconciliation(
        operation_id=operation_id, attempt_id=attempt_id, actor=actor,
        native_loader=load_native, native_writer=write_native,
        timeout_s=timeout_s)


@capability(
    "memory.dual_write.readiness", memory="off",
    http_method="GET", http_path="/memory/dual-write/readiness",
    http_tags=["memory"],
    description="Assess bounded, payload-free Memory dual-write evidence for a "
                "future activation review. Reads receipt and reconciliation "
                "metadata plus the latest rollback-rehearsal receipt; performs "
                "no repair and never grants or moves authority. Input: limit "
                "(1..1000), min_receipts (1..1000), max_evidence_age_seconds "
                "(60..31536000). Output: metrics, fail-closed reasons and "
                "ready_for_activation_review; authority_switch_allowed is always false.",
)
async def memory_dual_write_readiness(
    limit: int = 100, min_receipts: int = 25,
    max_evidence_age_seconds: int = 86_400, trace_id=None,
):
    try:
        from Vera.vera.provenance import get_provenance
    except ImportError:                              # pragma: no cover
        from ..provenance import get_provenance
    source_commit = str(get_provenance().get("git_sha") or "")
    return await asyncio.to_thread(
        _revision_repair.read_memory_authority_readiness,
        limit=limit, min_receipts=min_receipts,
        max_evidence_age_seconds=max_evidence_age_seconds,
        expected_source_commit=source_commit)


@capability(
    "memory.dual_write.rollback_rehearse", memory="off",
    http_method="POST", http_path="/memory/dual-write/rollback-rehearse",
    http_tags=["memory"],
    description="Run a deterministic rollback rehearsal against the real disabled "
                "dual-write and reconciliation gates using local sentinels only, "
                "then persist a payload-free receipt bound to the running source "
                "commit. It invokes no Memory backend, cannot enable either gate "
                "and never moves authority. Input: rehearsal_id (stable opaque "
                "operation ID). Output: created and exact rehearsal receipt.",
)
async def memory_dual_write_rollback_rehearse(
    rehearsal_id: str, trace_id=None,
):
    try:
        from Vera.vera.provenance import get_provenance
    except ImportError:                              # pragma: no cover
        from ..provenance import get_provenance
    source_commit = str(get_provenance().get("git_sha") or "")
    if not source_commit:
        raise RuntimeError("running source identity is unavailable")
    return await _revision_rollback.run_memory_rollback_rehearsal(
        rehearsal_id=rehearsal_id, source_commit=source_commit)


@capability(
    "memory.search", memory="off",
    http_method="POST", http_path="/memory/search", http_tags=["memory"],
    description="Search long-term memory using hybrid semantic + keyword matching. "
                "WHEN TO USE: recall past research results, stored facts, previous tool outputs for a topic. "
                "Input: query (str!), limit (int, default 10), session_id (str — filter to one session), "
                "category (str — filter by category label), tags (str — comma-sep filter), "
                "backends (str — comma-sep subset: postgres,chroma,neo4j; empty = all), "
                "include_activity (bool, default false — capability-call records and imported coding-agent "
                "transcript lines are left out unless this is true or record_type names one; use it to ask "
                "what was run). "
                "Output: {results: [{record: {id, text, category, tags, created_at, ...}, score, repeats}], count, "
                "query}. For richer context with graph expansion use memory.recall instead.",
)
async def memory_search(
    query:          str,
    limit:          int   = 10,
    session_id:     str   = "",
    record_type:    str   = "",
    category:       str   = "",
    tags:           str   = "",
    backends:       str   = "",   # comma-separated subset e.g. "chroma,neo4j"
    include_activity: bool = False,
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
    if str(include_activity).lower() in ("1", "true", "yes", "on"):   # HTTP may send it as a string
        filters["include_activity"] = True
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


async def _vector_guard_job():
    """Once a night (03:00-03:59 local, at most every 20 h): drop vectors of the kinds recall
    leaves out that reached the collection anyway (an exclusion list changed, a writer that
    skipped embed_worthy), capped at 20k per night, and keep what was found for
    retrieval.timing. No model work - Chroma reads and deletes only."""
    import time as _t
    now = _t.time()
    if _t.localtime(now).tm_hour != 3 or now - VECTOR_GUARD.get("at", 0) < 20 * 3600:
        return
    VECTOR_GUARD["at"] = now
    try:
        r = await memory_vectors_drop_excluded(confirm=True, limit=20000)
    except Exception as e:
        r = {"error": str(e)[:200]}
    VECTOR_GUARD.update({"ts": now_iso(), "found": r.get("found"), "dropped": r.get("dropped"),
                         "collection_after": r.get("collection_after"), "error": r.get("error")})
    if r.get("found"):
        log.warning("vector guard: %s excluded-kind vectors had reached the collection; dropped %s",
                    r.get("found"), r.get("dropped"))


schedule(_vector_guard_job, interval=3600, name="memory_vector_guard", singleton=True, skip_in_sandbox=True)


def _pool_of(client) -> Dict[str, Any]:
    """Connection counts of one chromadb HttpClient's httpx pool."""
    pool = client._server._session._transport._pool
    conns = list(pool.connections)
    return {"connections": len(conns), "idle": sum(1 for c in conns if c.is_idle()),
            "closed": sum(1 for c in conns if c.is_closed()),
            "max_connections": pool._max_connections, "max_keepalive": pool._max_keepalive_connections}


def chroma_pool_stats(deep: bool = False) -> Dict[str, Any]:
    """Where this process's connections to Chroma live (RF-82). The two known clients' pools,
    and with deep=True every httpx client in the heap holding a connection to Chroma's port,
    so a client built somewhere unexpected shows up."""
    import sys
    out: Dict[str, Any] = {"clients": {}}
    known = {"memory": getattr(MEMORY._backends.get("chroma"), "_client", None)}
    df = sys.modules.get("Vera.vera.fabric.data_fabric") or sys.modules.get("data_fabric")
    known["fabric"] = getattr(getattr(df, "FABRIC_CHROMA", None), "_client", None)
    for name, cl in known.items():
        if cl is None:
            out["clients"][name] = None
            continue
        try:
            out["clients"][name] = dict(_pool_of(cl), id=id(cl))
        except Exception as e:
            out["clients"][name] = {"error": str(e)[:160]}
    if deep:
        import gc
        import httpx
        port = int(CHROMA_PORT)
        found = []
        for o in gc.get_objects():
            if not isinstance(o, httpx.Client):
                continue
            try:
                conns = list(o._transport._pool.connections)
            except Exception:
                continue
            to_chroma = [c for c in conns if getattr(getattr(c, "_origin", None), "port", None) == port]
            if to_chroma:
                found.append({"id": id(o), "connections": len(to_chroma),
                              "idle": sum(1 for c in to_chroma if c.is_idle()),
                              "user_agent": str(o.headers.get("user-agent", ""))[:60],
                              "held_by": _holders(o)})
        out["httpx_clients_to_chroma"] = sorted(found, key=lambda x: -x["connections"])[:50]
        out["httpx_client_count"] = len(found)
        out["connections_total"] = sum(f["connections"] for f in found)
        # who holds them, summed: the leaking creator is the holder whose count grows
        by: Dict[str, int] = {}
        for f in found:
            k = " <- ".join(f["held_by"]) or "?"
            by[k] = by.get(k, 0) + 1
        out["by_holder"] = dict(sorted(by.items(), key=lambda kv: -kv[1]))
        try:
            from chromadb.api.shared_system_client import SharedSystemClient
            out["chromadb_systems"] = len(getattr(SharedSystemClient, "_identifier_to_system", {}) or {})
        except Exception as e:
            out["chromadb_systems"] = f"? {e}"[:80]
    return out


def _holders(obj, depth: int = 3) -> List[str]:
    """Type (and module) of what holds `obj`, `depth` levels up - skipping frames, lists and
    the gc scan's own containers - so the creator of a leaked client can be read off."""
    import gc
    import types as _types
    chain: List[str] = []
    cur = obj
    for _ in range(depth):
        nxt = None
        for r in gc.get_referrers(cur):
            if isinstance(r, (_types.FrameType, list)) or r is chain:
                continue
            if isinstance(r, dict):
                owners = [x for x in gc.get_referrers(r)
                          if not isinstance(x, (_types.FrameType, list, dict)) and getattr(x, "__dict__", None) is r]
                if owners:
                    nxt = owners[0]
                    break
                if r.get("__name__"):                  # a module's globals
                    chain.append("module " + str(r.get("__name__")))
                    return chain
                continue
            nxt = r
            break
        if nxt is None:
            break
        chain.append(f"{type(nxt).__module__}.{type(nxt).__name__}")
        cur = nxt
    return chain


@capability(
    "memory.chroma.pool", memory="off", silent=True,
    http_method="GET", http_path="/memory/chroma/pool", http_tags=["memory"],
    description="Where this process's connections to Chroma live (RF-82 diagnosis): connections, idle and "
                "closed counts and the pool limits of the memory and data-fabric Chroma clients. "
                "deep=true also walks the heap for every httpx client holding a connection to Chroma's "
                "port (slower, one-off). Read-only.",
)
async def memory_chroma_pool(deep: bool = False, trace_id=None):
    if isinstance(deep, str):
        deep = deep.strip().lower() in ("1", "true", "yes")
    return await asyncio.to_thread(chroma_pool_stats, bool(deep))


@capability(
    "retrieval.timing", memory="off", silent=True,
    http_method="GET", http_path="/retrieval/timing", http_tags=["memory"],
    description="Retrieval timing envelopes (four-minds FM-7.0): p50/p95 ms per stage and how each stage "
                "ended (ok, empty, timeout, error, skipped:<why>) over the last 500 calls per path in "
                "this process. Tells 'the embed timed out, so the vector leg never ran' apart from "
                "'the vector leg found nothing'. Paths: hybrid.search, hybrid.fast, hybrid.slow, "
                "hybrid.fast_only. Input: path (str, optional - one path). Emptied by a restart.",
)
async def retrieval_timing(path: str = "", trace_id=None):
    # No emit_event here, for the same reason as memory.stats.
    return {"window": RETRIEVAL_TIMING.size, "paths": RETRIEVAL_TIMING.summary(path),
            "chroma_paths": dict(CHROMA_PATHS), "vector_guard": dict(VECTOR_GUARD)}


@capability(
    "memory.backends", memory="off",
    contract={"presentation": {"kind": "table", "form": "table", "map": {"rows": "backends"}, "draw": {"columns": ["name", "connected", "total", "active", "edges"]}}},
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
    description="Smart recall: semantic search + automatic graph-neighbour expansion for rich context. "
                "WHEN TO USE: the best general-purpose memory retrieval — use this before answering questions "
                "that might benefit from past session context or stored knowledge. Returns pre-formatted context string "
                "ready to inject into a prompt. Better than memory.search for general recall because it also pulls in related records. "
                "Input: query (str!), session_id (str), limit (int, default 10), depth (int default 2 — graph hops). "
                "Output: {results: [{record, score, neighbours}], context (pre-formatted text), count}.",
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


@APP.get("/galaxy/panel", include_in_schema=False)
async def _nlp_panel():
    from fastapi.responses import HTMLResponse
    p = _HERE / "memory_map.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>memory_map.html not found</p>")

_HERE          = Path(__file__).parent
_IFRAME_STYLE = (
    "flex:1;border:none;width:100%;height:100%;background:var(--bg,#f0ede8)"
)

register_ui(
    "memory-galaxy-panel",
    "Galaxy",
    "✺",
    f"""<div style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/galaxy/panel" style="{_IFRAME_STYLE}"
          allow="clipboard-read; clipboard-write"></iframe>
</div>""",
    "",
    # ui_caps=["nlp.modules", "nlp.run", "nlp.health", "nlp.llm"],
    mode="tab",
    tab_order=58,
    specialist_agent="fabric-librarian",
    specialist_loop_profile="fabric-discovery",
)

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


@capability(
    "memory.reindex_embeddings",
    http_method="POST", http_path="/memory/reindex_embeddings",
    http_tags=["memory", "embed"], memory="off",
    description=(
        "Re-embed stored memory vectors (Chroma) with the CURRENT embedding "
        "provider — run after switching VERA_EMBED_PROVIDER so existing vectors "
        "match new ones. DRY-RUN by default. Input: confirm (bool — must be true "
        "to write), limit (int, 0=all), batch (int). Output: {dry_run, total, "
        "reembedded, provider, dim}."
    ),
)
async def memory_reindex_embeddings(confirm: bool = False, limit: int = 0,
                                    batch: int = 128, trace_id=None):
    cb = MEMORY._backends.get("chroma")
    coll = getattr(cb, "_collection", None) if cb else None
    if coll is None:
        return {"error": "chroma backend not connected"}

    import Vera.vera.capability_orchestration as _o
    provider = getattr(_o, "EMBED_PROVIDER", "ollama")

    # Probe the live embedder (also confirms MEMORY_AUTO_EMBED is on).
    probe = await embed_text("vector space probe")
    if probe is None:
        return {"error": "embedder unavailable (embed_text returned None) — "
                         "check the provider/model and MEMORY_AUTO_EMBED"}
    dim = len(probe)

    try:
        total = coll.count()
    except Exception as e:
        return {"error": f"chroma count failed: {e}"}
    target = total if (not limit or limit <= 0) else min(int(limit), total)

    if not confirm:
        return {"ok": True, "dry_run": True, "provider": provider, "dim": dim,
                "total": total, "would_reembed": target,
                "note": "Re-run with confirm=true to write new embeddings in place "
                        "(documents/metadata preserved)."}

    await emit_event({"type": "memory.reindex", "stage": "start",
                      "total": target, "provider": provider, "dim": dim})
    done = errors = offset = 0
    while offset < target:
        n = min(batch, target - offset)
        try:
            res = coll.get(include=["documents", "metadatas"], limit=n, offset=offset)
        except Exception as e:
            return {"error": f"chroma get failed at offset {offset}: {e}",
                    "reembedded": done}
        ids = res.get("ids") or []
        if not ids:
            break
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        for i, rid in enumerate(ids):
            doc = docs[i] if i < len(docs) else ""
            if not (doc and doc.strip()):
                continue
            vec = await embed_text(doc)
            if not vec:
                errors += 1
                continue
            try:
                coll.upsert(ids=[rid], documents=[doc],
                            metadatas=[metas[i] if i < len(metas) else {}],
                            embeddings=[vec])
                done += 1
            except Exception as e:
                errors += 1
                log.debug("reindex upsert %s: %s", rid, e)
        offset += len(ids)
        await emit_event({"type": "memory.reindex", "stage": "progress",
                          "done": done, "of": target})

    await emit_event({"type": "memory.reindex", "stage": "done",
                      "reembedded": done, "errors": errors})
    return {"ok": True, "dry_run": False, "provider": provider, "dim": dim,
            "total": total, "reembedded": done, "errors": errors}


@capability(
    "memory.backfill_vectors",
    http_method="POST", http_path="/memory/backfill_vectors",
    http_tags=["memory", "embed"], memory="off",
    description=(
        "Re-encode memory records that exist in Postgres (the source of truth) "
        "but have NO vector in the CURRENT Chroma collection — use after a "
        "Chroma reset, an embed-model switch (collections are versioned per "
        "model), or an embedder outage that skipped vectors. Embeds with the "
        "current provider and upserts explicit vectors; also stamps "
        "embedding_model on the Postgres rows. DRY-RUN by default. Inputs: "
        "confirm (bool — must be true to write), limit (int, 0=all missing), "
        "batch (int=64), include_archived (bool=false). Output: {dry_run, "
        "pg_total, chroma_count, missing, backfilled, errors, provider, dim, "
        "collection}."
    ),
)
async def memory_backfill_vectors(confirm: bool = False, limit: int = 0,
                                  batch: int = 64, include_archived: bool = False,
                                  trace_id=None):
    cb   = MEMORY._backends.get("chroma")
    coll = getattr(cb, "_collection", None) if cb else None
    pg   = MEMORY._backends.get("postgres")
    pool = getattr(pg, "_pool", None) if pg else None
    if coll is None:
        return {"error": "chroma backend not connected"}
    if pool is None:
        return {"error": "postgres backend not connected"}

    import Vera.vera.capability_orchestration as _o
    provider = getattr(_o, "EMBED_PROVIDER", "ollama")
    probe = await embed_text("vector space probe")
    if probe is None:
        return {"error": "embedder unavailable (embed_text returned None) — "
                         "check the provider/model and MEMORY_AUTO_EMBED"}
    dim = len(probe)

    cond = "TRUE" if include_archived else "archived=false"
    # the kinds recall leaves out are never embedded (VERA_MEMORY_RECALL_EXCLUDE) - so they are not "missing" a vector
    # either; the names come from parse_exclusions, which admits only [a-z0-9_] identifiers
    if _RECALL_EXCLUDE_TYPES:
        cond += " AND record_type NOT IN (" + ",".join("'" + t + "'" for t in _RECALL_EXCLUDE_TYPES) + ")"
    if _RECALL_EXCLUDE_TRANSCRIPTS:
        cond += " AND NOT (text LIKE '[Claude Code%' OR text LIKE '[Codex%')"
    async with pool.acquire() as conn:
        pg_total = await conn.fetchval(f"SELECT COUNT(*) FROM vera_memories WHERE {cond}")
    try:
        chroma_count = coll.count()
    except Exception as e:
        return {"error": f"chroma count failed: {e}"}

    # Keyset-scan Postgres ids and diff against the collection in chunks.
    missing: List[str] = []
    last_id = ""
    while True:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT id FROM vera_memories WHERE {cond} AND id > $1 "
                f"ORDER BY id LIMIT 500", last_id)
        if not rows:
            break
        ids = [r["id"] for r in rows]
        last_id = ids[-1]
        try:
            got = set((coll.get(ids=ids) or {}).get("ids") or [])
        except Exception as e:
            return {"error": f"chroma get failed: {e}", "checked_up_to": last_id}
        missing.extend(i for i in ids if i not in got)

    target = missing if (not limit or limit <= 0) else missing[:int(limit)]
    base = {"provider": provider, "dim": dim, "collection": CHROMA_COLLECTION,
            "pg_total": pg_total, "chroma_count": chroma_count,
            "missing": len(missing)}
    if not confirm:
        return {"ok": True, "dry_run": True, **base,
                "would_backfill": len(target),
                "note": "Re-run with confirm=true to embed + upsert the missing "
                        "records into the current collection."}

    await emit_event({"type": "memory.backfill", "stage": "start",
                      "missing": len(target), **{k: base[k] for k in
                                                 ("provider", "dim", "collection")}})
    done = errors = 0
    for i in range(0, len(target), max(1, int(batch))):
        chunk = target[i:i + max(1, int(batch))]
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM vera_memories WHERE id = ANY($1::text[])", chunk)
        stamped: List[str] = []
        for row in rows:
            rec = pg._row_to_record(row)
            rid, doc, meta = rec.to_chroma_doc()
            if not (doc and doc.strip()):
                continue
            vec = await embed_text(doc)
            if not vec:
                errors += 1
                continue
            try:
                coll.upsert(ids=[rid], documents=[doc], metadatas=[meta],
                            embeddings=[vec])
                stamped.append(rid)
                done += 1
            except Exception as e:
                errors += 1
                log.debug("backfill upsert %s: %s", rid, e)
        if stamped:
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE vera_memories SET embedding_model=$2 "
                        "WHERE id = ANY($1::text[])", stamped, OLLAMA_EMBED_MODEL)
            except Exception as e:
                log.debug("backfill pg stamp: %s", e)
        await emit_event({"type": "memory.backfill", "stage": "progress",
                          "done": done, "of": len(target), "errors": errors})

    await emit_event({"type": "memory.backfill", "stage": "done",
                      "backfilled": done, "errors": errors})
    return {"ok": True, "dry_run": False, **base,
            "backfilled": done, "errors": errors}


@capability(
    "memory.vectors.drop_excluded",
    http_method="POST", http_path="/memory/vectors/drop_excluded",
    http_tags=["memory", "embed"], memory="off",
    description=(
        "Remove the vectors of the record kinds recall leaves out (VERA_MEMORY_RECALL_EXCLUDE: capability calls, "
        "workflow steps, imported coding-agent transcript lines) from the memory vector collection. Only the "
        "derived vectors go: the records stay in Postgres and Neo4j, in the activity graph, and are found with "
        "memory.search include_activity=true. DRY-RUN by default. Inputs: confirm (bool - must be true to delete), "
        "limit (int, 0 = all), batch (int=1000). Output: {dry_run, kinds, found, dropped, collection_before, "
        "collection_after}."
    ),
)
async def memory_vectors_drop_excluded(confirm: bool = False, limit: int = 0, batch: int = 1000,
                                       trace_id=None):
    cb = MEMORY._backends.get("chroma")
    coll = getattr(cb, "_collection", None) if cb else None
    if coll is None:
        return {"error": "chroma backend not connected"}
    if not (_RECALL_EXCLUDE_TYPES or _RECALL_EXCLUDE_TRANSCRIPTS):
        return {"ok": True, "dry_run": not confirm, "kinds": [], "found": 0, "dropped": 0,
                "note": "VERA_MEMORY_RECALL_EXCLUDE is empty - nothing is excluded"}
    batch = max(100, min(int(batch or 1000), 5000))
    cap = int(limit or 0)
    kinds = list(_RECALL_EXCLUDE_TYPES) + (["transcript"] if _RECALL_EXCLUDE_TRANSCRIPTS else [])
    # (where, where_document) pairs: by record type, then by the transcript prefix
    selectors = []
    if _RECALL_EXCLUDE_TYPES:
        selectors.append(({"record_type": {"$in": list(_RECALL_EXCLUDE_TYPES)}}, None))
    if _RECALL_EXCLUDE_TRANSCRIPTS:
        for pfx in ("[Claude Code", "[Codex"):
            selectors.append((None, {"$contains": pfx}))
    try:
        before = await _chroma_bound.run(coll.count)
    except Exception as e:
        return {"error": f"chroma count failed: {e}"}

    async def _page(where, wdoc, offset):
        kw = {"include": [], "limit": batch, "offset": offset}
        if where: kw["where"] = where
        if wdoc: kw["where_document"] = wdoc
        res = await _chroma_bound.run(lambda: coll.get(**kw))
        ids = (res or {}).get("ids") or []
        if wdoc:   # a "contains" match anywhere; only the lines that START with the prefix are transcripts
            got = await _chroma_bound.run(lambda: coll.get(ids=ids, include=["documents"])) if ids else {}
            docs = dict(zip(got.get("ids") or [], got.get("documents") or []))
            ids = [i for i in ids if _recall_core.is_transcript(docs.get(i, ""))]
        return ids, len((res or {}).get("ids") or [])

    found = dropped = 0
    for where, wdoc in selectors:
        offset = 0
        while True:
            if cap and found >= cap:
                break
            try:
                ids, scanned = await _page(where, wdoc, offset)
            except Exception as e:
                return {"error": f"chroma get failed: {e}", "found": found, "dropped": dropped}
            if not scanned:
                break
            if cap:
                ids = ids[:max(0, cap - found)]
            found += len(ids)
            if confirm and ids:
                try:
                    await _chroma_bound.run(lambda: coll.delete(ids=ids))
                    dropped += len(ids)
                except Exception as e:
                    return {"error": f"chroma delete failed: {e}", "found": found, "dropped": dropped}
                # deleted rows leave the result set, so the next page starts where this one did - except the rows
                # this page scanned and kept (non-prefix "contains" matches), which are skipped by offset
                offset += scanned - len(ids)
            else:
                offset += scanned
    after = before
    try:
        after = await _chroma_bound.run(coll.count)
    except Exception:
        pass
    if confirm:
        try:
            cb._count_cache = (0.0, 0)   # the collection shrank; the next count reads it fresh (shape: (ts, n))
        except Exception:
            pass
        await emit_event({"type": "memory.vectors.dropped", "kinds": kinds, "dropped": dropped})
    return {"ok": True, "dry_run": not confirm, "kinds": kinds, "found": found, "dropped": dropped,
            "collection_before": before, "collection_after": after,
            "note": "" if confirm else "Re-run with confirm=true to delete these vectors (records are kept)."}


async def _startup():
    await MEMORY.startup()
    # Start the Redis promoter background task - on the host only. It reads
    # the promotion stream with plain XREAD (no consumer group), so every
    # process running it promotes every event: a node worker would store each
    # memory a second time.
    if not getattr(_orch, "_IS_WORKER", False):
        asyncio.create_task(_memory_promoter())
    log.info("vera_memory ready — backends: %s", MEMORY.backends)

# Schedule startup to run once after the event loop is running
schedule(_startup, interval=999999, name="memory_startup")
# Do NOT add create_task(_startup()) here — schedule() handles it once.
# Running both causes double Neo4j connect + duplicate backend logs.
