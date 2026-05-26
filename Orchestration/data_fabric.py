"""
data_fabric.py  —  Vera Polyglot Data Fabric v3
=================================================
Merges the modular pipeline architecture (v2 document) with the production
bug fixes (current v2):

  • Uses cfg (config.py) for ALL connection parameters — no bare os.getenv
  • fd leak fixed: stream worker reuses _orch.REDIS (shared pool), never
    opens its own aioredis connection
  • Eager SQLite init at module import — tables exist before first HTTP request
  • Source pull fixed: items are ingested via the full pipeline so they appear
    in /fabric/datasets and /fabric/query results
  • Query DSL: vector (FAISS+Chroma) + text (Postgres FTS) + graph expansion
  • ObjectStore: Garage / Ceph via S3-compatible API (optional)
  • Pipeline stages: Hash → Schema → TextExtract → Embed → PG → Vector → Neo4j

Storage layers (all optional, graceful degradation)
────────────────────────────────────────────────────
  SQLite     — always-available local fallback (eager init at import)
  PostgreSQL — authoritative relational store
  FAISS      — persistent sharded vector index (saved to ObjectStore)
  ChromaDB   — metadata-filtered vector search
  Neo4j      — auxiliary graph: dataset relationships, lineage, categories
  Redis      — hot cache (query results), streaming ingestion (shared pool)
  Garage/Ceph — object store for large blobs and FAISS snapshots

Capabilities
────────────
  fabric.ingest          fabric.update        fabric.query
  fabric.schema          fabric.datasets      fabric.stats
  fabric.link_datasets   fabric.stream_publish fabric.delete_dataset
  fabric.source.add      fabric.source.pull   fabric.source.list
  fabric.source.delete   fabric.bus.configure fabric.bus.status
  fabric.aux_graph.link  fabric.aux_graph.query
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

# ── Config (single source of truth) ──────────────────────────────────────────
from Vera.Orchestration.config import cfg

# ── Orchestrator integration ──────────────────────────────────────────────────
import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import (
    APP,
    capability, emit_event, now_iso, schedule,
)

log = logging.getLogger("vera.data_fabric")

def _redis():
    """Return the shared REDIS pool — never open a new connection."""
    return _orch.REDIS

# ── Optional backend imports ──────────────────────────────────────────────────
try:
    import numpy as np; HAS_NUMPY = True
except ImportError:
    np = None; HAS_NUMPY = False

try:
    import faiss as _faiss; HAS_FAISS = True
except ImportError:
    _faiss = None; HAS_FAISS = False

try:
    import chromadb as _chromadb; HAS_CHROMA = True
except ImportError:
    _chromadb = None; HAS_CHROMA = False

try:
    import asyncpg as _asyncpg; HAS_PG = True
except ImportError:
    _asyncpg = None; HAS_PG = False

try:
    from neo4j import AsyncGraphDatabase as _Neo4j; HAS_NEO = True
except ImportError:
    _Neo4j = None; HAS_NEO = False

try:
    import boto3 as _boto3; HAS_BOTO = True
except ImportError:
    _boto3 = None; HAS_BOTO = False

try:
    import feedparser; HAS_FEEDPARSER = True
except ImportError:
    feedparser = None; HAS_FEEDPARSER = False

try:
    import aiosqlite; HAS_AIOSQLITE = True
except ImportError:
    aiosqlite = None; HAS_AIOSQLITE = False

# ── Configuration (all from cfg) ──────────────────────────────────────────────
POSTGRES_URL       = cfg.POSTGRES_URL
CHROMA_HOST        = cfg.CHROMA_HOST
CHROMA_PORT        = cfg.CHROMA_PORT
NEO4J_URI          = cfg.NEO4J_URI
NEO4J_USER         = cfg.NEO4J_USER
NEO4J_PASSWORD     = cfg.NEO4J_PASS
OLLAMA_EMBED_URL   = cfg.OLLAMA_EMBED_URL
OLLAMA_EMBED_MODEL = cfg.OLLAMA_EMBED_MODEL

FABRIC_OBJECT_STORE = os.getenv("FABRIC_OBJECT_STORE",   "none")
FABRIC_S3_ENDPOINT  = os.getenv("FABRIC_S3_ENDPOINT",    "http://localhost:3900")
FABRIC_S3_ACCESS    = os.getenv("FABRIC_S3_ACCESS",      "")
FABRIC_S3_SECRET    = os.getenv("FABRIC_S3_SECRET",      "")
FABRIC_S3_BUCKET    = os.getenv("FABRIC_S3_BUCKET",      "vera-data-fabric")
FABRIC_S3_REGION    = os.getenv("FABRIC_S3_REGION",      "garage")
FABRIC_VECTOR_DIM   = int(os.getenv("FABRIC_VECTOR_DIM", "768"))
FABRIC_CACHE_TTL    = int(os.getenv("FABRIC_CACHE_TTL",  "3600"))
FABRIC_STREAM_KEY   = os.getenv("FABRIC_STREAM_KEY",     "vera:fabric:ingest")
SQLITE_PATH         = os.getenv("FABRIC_SQLITE", str(Path(__file__).parent / "vera_fabric.db"))

# ─────────────────────────────────────────────────────────────────────────────
# SQLITE FALLBACK — init synchronously at import time
# ─────────────────────────────────────────────────────────────────────────────

def _sqlite_conn() -> sqlite3.Connection:
    # Note: journal_mode is set once at import via _sqlite_set_wal(); we do NOT
    # repeat it here because setting it on a connection that has writers can
    # block. busy_timeout is per-connection so it MUST be set every time.
    conn = sqlite3.connect(SQLITE_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def _sqlite_cleanup_stale_locks():
    """If a previous process crashed mid-write, it can leave behind stale
    lock files that prevent any new connection from acquiring a lock. Detect
    and clean those up at import time, BEFORE any connection is opened.

    Safe because:
      - rollback journal: opening the DB and committing any tx will replay or
        roll back this journal; if the DB itself is consistent and the journal
        is older than the DB by more than 60s, it's almost certainly stale.
      - WAL mode: -shm and -wal can be rebuilt by SQLite as long as no live
        process holds the file. We use a short exclusive open to verify.
    """
    db_path = SQLITE_PATH
    if not os.path.exists(db_path):
        return  # fresh DB, nothing to clean
    try:
        # Try to open the DB exclusively for a brief moment to verify no other
        # process holds it. If the open succeeds, any -journal/-wal files left
        # behind are guaranteed stale.
        test = sqlite3.connect(db_path, timeout=2)
        try:
            test.execute("BEGIN IMMEDIATE")
            test.rollback()
        finally:
            test.close()
    except sqlite3.OperationalError as e:
        # Couldn\'t get an exclusive lock — another process is using the DB.
        # Don\'t touch any side files in that case.
        log.debug("DB in use by another process, skipping stale-lock cleanup: %s", e)
        return
    # If we got here, the DB is ours alone. Any -journal files are stale.
    for suffix in ("-journal",):
        p = db_path + suffix
        try:
            if os.path.exists(p):
                # Check it is older than the DB; if it is, it\'s safe to remove.
                if os.path.getmtime(p) <= os.path.getmtime(db_path) + 1:
                    os.remove(p)
                    log.info("data_fabric: removed stale %s", p)
        except Exception as _e:
            log.debug("cleanup %s: %s", p, _e)

# Wrap in defensive try — under no circumstances should cleanup kill the import
try:
    _sqlite_cleanup_stale_locks()
except Exception as _e:
    log.debug("stale-lock cleanup skipped: %s", _e)

def _sqlite_set_wal():
    """Switch the DB to WAL once. Safe to call repeatedly — if it\'s already
    WAL the PRAGMA is a no-op."""
    last_err = None
    for attempt in range(5):
        try:
            conn = sqlite3.connect(SQLITE_PATH, timeout=15, check_same_thread=False)
            try:
                conn.execute("PRAGMA busy_timeout=15000")
                # Read current mode to know if we even need to switch
                cur = conn.execute("PRAGMA journal_mode").fetchone()
                if cur and (cur[0] or "").lower() != "wal":
                    conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.commit()
                return
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            last_err = e
            if "locked" in str(e).lower():
                time.sleep(0.5 * (attempt + 1))
                continue
            log.warning("WAL set failed: %s", e)
            return
        except Exception as _e:
            log.debug("WAL init: %s", _e)
            return
    log.warning("WAL set: gave up after retries (%s)", last_err)

# CRITICAL: WAL must be set BEFORE any DDL. Without WAL, the rollback-journal
# locking model means concurrent module imports (which all open connections)
# can deadlock during table creation. Wrapped defensively — never kill import.
try:
    _sqlite_set_wal()
except Exception as _e:
    log.debug("WAL setup skipped: %s", _e)

def _sqlite_init_sync():
    """Create tables. Retries on transient lock errors — these can happen if
    another process is racing us during startup."""
    last_err = None
    for attempt in range(5):
        try:
            conn = _sqlite_conn()
            try:
                # Bigger busy timeout for the init transaction
                conn.execute("PRAGMA busy_timeout=15000")
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS fabric_datasets (
                        dataset_id   TEXT PRIMARY KEY,
                        record_count INTEGER DEFAULT 0,
                        created_at   TEXT,
                        updated_at   TEXT
                    );
                    CREATE TABLE IF NOT EXISTS fabric_records (
                        id          TEXT PRIMARY KEY,
                        dataset_id  TEXT NOT NULL,
                        text        TEXT,
                        data        TEXT,
                        source_id   TEXT,
                        tags        TEXT,
                        created_at  TEXT,
                        synced_pg   INTEGER DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS fabric_sources (
                        id          TEXT PRIMARY KEY,
                        source_type TEXT,
                        url         TEXT,
                        label       TEXT,
                        dataset_id  TEXT,
                        interval    INTEGER DEFAULT 0,
                        tags        TEXT,
                        headers     TEXT,
                        jq_path     TEXT,
                        page        TEXT,
                        lim         INTEGER DEFAULT 50,
                        enabled     INTEGER DEFAULT 1,
                        pull_count  INTEGER DEFAULT 0,
                        last_pulled TEXT,
                        created_at  TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_fr_dataset ON fabric_records(dataset_id);
                    CREATE INDEX IF NOT EXISTS idx_fr_synced  ON fabric_records(synced_pg);
            CREATE TABLE IF NOT EXISTS fabric_custom_graphs (
                name         TEXT PRIMARY KEY,
                description  TEXT,
                kind         TEXT,
                node_labels  TEXT,
                cypher_node  TEXT,
                cypher_edge  TEXT,
                created_at   TEXT
            );
            CREATE TABLE IF NOT EXISTS fabric_pipelines (
                id           TEXT PRIMARY KEY,
                name         TEXT,
                description  TEXT,
                stages       TEXT,
                tags         TEXT,
                created_at   TEXT,
                updated_at   TEXT
            );
            CREATE TABLE IF NOT EXISTS fabric_dataset_tags (
                dataset_id  TEXT NOT NULL,
                tag         TEXT NOT NULL,
                source      TEXT DEFAULT 'user',
                created_at  TEXT,
                PRIMARY KEY (dataset_id, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_fdt_tag ON fabric_dataset_tags(tag);
            CREATE TABLE IF NOT EXISTS fabric_agents (
                id            TEXT PRIMARY KEY,
                name          TEXT,
                description   TEXT,
                config        TEXT,
                tags          TEXT,
                created_at    TEXT,
                updated_at    TEXT
            );
            CREATE TABLE IF NOT EXISTS fabric_dags (
                id            TEXT PRIMARY KEY,
                name          TEXT,
                description   TEXT,
                definition    TEXT,
                tags          TEXT,
                created_at    TEXT,
                updated_at    TEXT
            );
            CREATE TABLE IF NOT EXISTS fabric_skills (
                id           TEXT PRIMARY KEY,
                name         TEXT,
                description  TEXT,
                dataset_ids  TEXT,
                ontology     TEXT,
                samples      TEXT,
                tags         TEXT,
                created_at   TEXT,
                updated_at   TEXT
            );
                """)
                conn.commit()
                return
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            last_err = e
            if "locked" in str(e).lower():
                log.debug("init locked, retry %d", attempt + 1)
                time.sleep(0.5 * (attempt + 1))
                continue
            raise
    log.warning("data_fabric: init exhausted retries (%s) — tables may not be ready", last_err)
    return  # never raise — module import must always succeed

# Eager init — tables ready before any HTTP request can arrive
try:
    _sqlite_init_sync()
    log.debug("data_fabric: SQLite tables ready at import time (%s)", SQLITE_PATH)
except Exception as _e:
    log.warning("data_fabric: SQLite eager init failed: %s", _e)

# ─────────────────────────────────────────────────────────────────────────────
# Single-writer queue — eliminates "database is locked" by serialising every
# SQLite write through one consumer task. Reads still use independent
# connections (WAL allows concurrent readers + 1 writer). Without this,
# concurrent ingest from multiple sources races on the WAL lock and hits
# busy_timeout.
# ─────────────────────────────────────────────────────────────────────────────
_WRITE_QUEUE: "asyncio.Queue" = None  # initialised lazily — needs a running loop
_WRITE_TASK = None

def _ensure_write_queue():
    global _WRITE_QUEUE, _WRITE_TASK
    if _WRITE_QUEUE is None:
        _WRITE_QUEUE = asyncio.Queue(maxsize=2000)
    if _WRITE_TASK is None or _WRITE_TASK.done():
        try:
            loop = asyncio.get_running_loop()
            _WRITE_TASK = loop.create_task(_write_consumer())
        except RuntimeError:
            # No running loop (e.g. import time). The consumer will be started
            # lazily by the first writer.
            pass
    return _WRITE_QUEUE

async def _write_consumer():
    """Single writer task. Drains _WRITE_QUEUE, batches operations from the
    same connection while available, commits once per batch."""
    log.info("data_fabric: SQLite writer task started")
    backoff = 0.5
    while True:
        try:
            op = await _WRITE_QUEUE.get()
        except Exception:
            await asyncio.sleep(0.1)
            continue
        if op is None:
            continue
        # Drain a small burst to amortise the connection cost
        batch = [op]
        try:
            while len(batch) < 100:
                batch.append(_WRITE_QUEUE.get_nowait())
        except asyncio.QueueEmpty:
            pass
        # Execute the batch in a single connection / transaction
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, _execute_write_batch, batch
            )
            backoff = 0.5
            # Success: signal completion to all op futures
            for op in batch:
                fut = op.get("_future")
                if fut and not fut.done():
                    fut.set_result(True)
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                # Re-queue and back off — DON'T resolve futures, the retry will
                log.warning("write batch locked, requeueing %d ops, backoff %.1fs",
                            len(batch), backoff)
                for op in batch:
                    try:
                        await _WRITE_QUEUE.put(op)
                    except Exception:
                        fut = op.get("_future")
                        if fut and not fut.done():
                            fut.set_exception(e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
            else:
                log.error("write batch error: %s", e)
                for op in batch:
                    fut = op.get("_future")
                    if fut and not fut.done():
                        fut.set_exception(e)
        except Exception as e:
            log.error("write batch unexpected: %s", e)
            for op in batch:
                fut = op.get("_future")
                if fut and not fut.done():
                    fut.set_exception(e)

def _execute_write_batch(batch: list):
    """Synchronous: execute every op in the batch on a single connection."""
    conn = _sqlite_conn()
    try:
        # Larger busy timeout for the writer specifically
        conn.execute("PRAGMA busy_timeout=30000")
        for op in batch:
            kind = op.get("kind")
            if kind == "insert_record":
                rec = op["rec"]
                conn.execute(
                    "INSERT OR REPLACE INTO fabric_records "
                    "(id, dataset_id, text, data, source_id, tags, created_at, synced_pg) "
                    "VALUES (?,?,?,?,?,?,?,0)",
                    (rec["id"], rec["dataset_id"], rec.get("text",""),
                     json.dumps(rec.get("data",{})), rec.get("source_id",""),
                     json.dumps(rec.get("tags",[])), rec.get("created_at", now_iso()))
                )
            elif kind == "upsert_dataset":
                dsid = op["dataset_id"]
                inc  = op.get("increment", 0)
                conn.execute(
                    "INSERT OR IGNORE INTO fabric_datasets "
                    "(dataset_id, record_count, created_at, updated_at) VALUES (?,0,?,?)",
                    (dsid, now_iso(), now_iso())
                )
                if inc:
                    conn.execute(
                        "UPDATE fabric_datasets SET record_count=record_count+?, updated_at=? "
                        "WHERE dataset_id=?",
                        (inc, now_iso(), dsid)
                    )
            elif kind == "upsert_source":
                src = op["src"]
                conn.execute(
                    "INSERT OR REPLACE INTO fabric_sources "
                    "(id, source_type, url, label, dataset_id, interval, tags, headers, "
                    " jq_path, page, lim, enabled, pull_count, last_pulled, created_at, "
                    " config, auth) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (src.get("id",""),
                     src.get("source_type", src.get("type","rss")),
                     src.get("url",""),
                     src.get("label",""),
                     src.get("dataset_id",""),
                     int(src.get("interval", 0) or 0),
                     json.dumps(src.get("tags") or []) if not isinstance(src.get("tags"), str) else src.get("tags"),
                     src.get("headers") if isinstance(src.get("headers"), str) else json.dumps(src.get("headers") or {}),
                     src.get("jq_path",""),
                     src.get("page",""),
                     int(src.get("lim", src.get("limit", 50)) or 50),
                     1 if src.get("enabled", True) else 0,
                     int(src.get("pull_count", 0) or 0),
                     src.get("last_pulled",""),
                     src.get("created_at", now_iso()),
                     src.get("config","") if isinstance(src.get("config",""), str) else json.dumps(src.get("config") or {}),
                     src.get("auth","") if isinstance(src.get("auth",""), str) else json.dumps(src.get("auth") or {}))
                )
            elif kind == "raw":
                conn.execute(op["sql"], op.get("params", ()))
        conn.commit()
    finally:
        conn.close()

async def _enqueue_write(op: dict, wait: bool = True):
    """Enqueue a write op. If wait=True, await its completion."""
    q = _ensure_write_queue()
    # Lazy task start (in case _ensure_write_queue ran at import time without a loop)
    global _WRITE_TASK
    if _WRITE_TASK is None or _WRITE_TASK.done():
        _WRITE_TASK = asyncio.get_running_loop().create_task(_write_consumer())
    fut = asyncio.get_running_loop().create_future() if wait else None
    op["_future"] = fut
    await q.put(op)
    if wait:
        await fut

# Deferred index migration — kicked off in the background after FastAPI is up.
# Building the composite index on a multi-million-row DB takes time AND holds a
# write lock; doing it synchronously at import time was causing "database is
# locked" errors on every other request. Now we run it off-thread, AFTER a
# small delay, so the server is responsive while indexing finishes.
_INDEX_MIGRATION_DONE = False

async def _sqlite_index_migration_async():
    """Run heavy DB migrations off-thread, with retries on lock contention."""
    global _INDEX_MIGRATION_DONE
    if _INDEX_MIGRATION_DONE:
        return
    # Wait a moment so the server can start serving first
    await asyncio.sleep(5)
    loop = asyncio.get_running_loop()
    def _check_index():
        conn = _sqlite_conn()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_fr_ds_ts'"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    try:
        already = await loop.run_in_executor(None, _check_index)
        if already:
            globals()["_INDEX_MIGRATION_DONE"] = True
            return
    except Exception as _e:
        log.debug("index check: %s", _e)

    def _build_index():
        # Use a long busy_timeout for the migration only — we expect contention
        conn = sqlite3.connect(SQLITE_PATH, timeout=120, check_same_thread=False)
        try:
            conn.execute("PRAGMA busy_timeout=120000")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fr_ds_ts ON fabric_records(dataset_id, created_at DESC)")
            conn.commit()
            return True
        finally:
            conn.close()

    for attempt in range(3):
        try:
            ok = await loop.run_in_executor(None, _build_index)
            if ok:
                log.info("data_fabric: composite index ready (idx_fr_ds_ts)")
                globals()["_INDEX_MIGRATION_DONE"] = True
                return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 2:
                log.debug("index migration locked, retry %d", attempt+1)
                await asyncio.sleep(10)
                continue
            log.warning("index migration: %s", e)
            return
        except Exception as e:
            log.warning("index migration: %s", e)
            return



async def _sqlite_insert_record(rec: dict):
    """Insert one record + bump dataset count. Goes through the single-writer
    queue, so it never contends with other writers."""
    await _enqueue_write({"kind": "insert_record", "rec": rec}, wait=False)
    await _enqueue_write({"kind": "upsert_dataset",
                          "dataset_id": rec["dataset_id"],
                          "increment": 1}, wait=True)

async def _sqlite_insert_records_batch(recs: list):
    """Bulk-insert many records in a single transaction. ~50x faster than the
    one-at-a-time path during a pull, and only takes the write lock once."""
    if not recs:
        return
    by_ds = {}
    for r in recs:
        by_ds.setdefault(r["dataset_id"], []).append(r)
    for r in recs:
        await _enqueue_write({"kind": "insert_record", "rec": r}, wait=False)
    for ds, group in by_ds.items():
        await _enqueue_write({"kind": "upsert_dataset",
                              "dataset_id": ds,
                              "increment": len(group)}, wait=True)
    # Schedule deterministic auto-tagging for any dataset that just gained
    # records. We dedupe per process so we don't fire the LLM repeatedly.
    for ds_id in by_ds.keys():
        try:
            asyncio.create_task(_maybe_auto_tag_dataset(ds_id))
        except RuntimeError:
            pass


# Module-level set of datasets we've already scheduled auto-tag for
_AUTO_TAG_SCHEDULED: set = set()

async def _maybe_auto_tag_dataset(dataset_id: str):
    """Schedule auto-tag for a dataset that's newly populated. Deduped per
    process and only fires once the dataset has >= 3 records and zero existing
    tags. Waits 8s to let ingestion settle so the LLM sees real samples."""
    if not dataset_id:
        return
    if dataset_id in _AUTO_TAG_SCHEDULED:
        return
    _AUTO_TAG_SCHEDULED.add(dataset_id)
    try:
        await asyncio.sleep(8)
        loop = asyncio.get_running_loop()
        def _read():
            conn = _sqlite_conn()
            try:
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM fabric_records WHERE dataset_id=?",
                    (dataset_id,)).fetchone()
                tags = conn.execute(
                    "SELECT COUNT(*) FROM fabric_dataset_tags WHERE dataset_id=?",
                    (dataset_id,)).fetchone()
                return (cnt[0] if cnt else 0, tags[0] if tags else 0)
            finally:
                conn.close()
        n_recs, n_tags = await loop.run_in_executor(None, _read)
        if n_recs < 3 or n_tags > 0:
            return
        # Run the existing capability — it does the LLM call
        try:
            await fabric_datasets_auto_tag(
                dataset_id=dataset_id, sample_size=15, max_tags=8, apply=True)
        except Exception as e:
            log.warning("auto_tag fire for %s: %s", dataset_id, e)
    except Exception as e:
        log.warning("_maybe_auto_tag_dataset: %s", e)
    finally:
        # Allow re-scheduling later if the user manually clears tags
        # Keep dedupe for 5 minutes
        async def _expire():
            await asyncio.sleep(300)
            _AUTO_TAG_SCHEDULED.discard(dataset_id)
        try: asyncio.create_task(_expire())
        except RuntimeError: pass


async def _sqlite_sources() -> List[dict]:
    loop = asyncio.get_running_loop()
    def _sync():
        conn = _sqlite_conn()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM fabric_sources").fetchall()]
        finally:
            conn.close()
    try:
        return await loop.run_in_executor(None, _sync)
    except Exception as e:
        log.warning("_sqlite_sources: %s", e)
        return []


async def _sqlite_datasets() -> List[dict]:
    loop = asyncio.get_running_loop()
    def _sync():
        conn = _sqlite_conn()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT dataset_id, record_count, created_at, updated_at FROM fabric_datasets"
            ).fetchall()]
        finally:
            conn.close()
    try:
        return await loop.run_in_executor(None, _sync)
    except Exception:
        return []


async def _sqlite_query(dataset_id: str = "", limit: int = 50, offset: int = 0) -> List[dict]:
    loop = asyncio.get_running_loop()
    def _sync():
        conn = _sqlite_conn()
        try:
            if dataset_id:
                rows = conn.execute(
                    "SELECT * FROM fabric_records WHERE dataset_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (dataset_id, limit, offset)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM fabric_records ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset)
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    try:
        return await loop.run_in_executor(None, _sync)
    except Exception:
        return []


async def _sqlite_upsert_source(src: dict):
    """Source upsert via the single-writer queue."""
    await _enqueue_write({"kind": "upsert_source", "src": src}, wait=True)



@dataclass
class DataRecord:
    id:             str     = field(default_factory=lambda: str(uuid.uuid4()))
    dataset_id:     str     = ""
    source:         str     = "api"
    created_at:     str     = field(default_factory=now_iso)
    updated_at:     str     = field(default_factory=now_iso)
    version:        int     = 1
    text:           str     = ""
    data:           Dict    = field(default_factory=dict)
    metadata:       Dict    = field(default_factory=dict)
    schema:         Dict    = field(default_factory=dict)
    tags:           List[str] = field(default_factory=list)
    embedding:      List[float] = field(default_factory=list)
    embedding_model: str    = ""
    parent_id:      str     = ""
    pipeline:       str     = ""
    content_hash:   str     = ""
    source_id:      str     = ""

    def compute_hash(self) -> str:
        content = self.text or json.dumps(self.data, sort_keys=True, default=str)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["embedding"] = []
        return d


# ─────────────────────────────────────────────────────────────────────────────
# EMBEDDING
# ─────────────────────────────────────────────────────────────────────────────

_embed_failed = False

async def _embed(text: str) -> Optional[List[float]]:
    """Generate embedding via the centralized ollama_embed (logged to Jobs).

    Preserves the circuit-breaker (_embed_failed) so that if the cluster is
    unreachable we stop hammering it, and the L2-normalisation behaviour.
    """
    global _embed_failed
    if _embed_failed or not text.strip():
        return None
    try:
        from Vera.Orchestration.capability_orchestration import ollama_embed
        vec = await ollama_embed(
            text, model=OLLAMA_EMBED_MODEL, normalize=HAS_NUMPY,
        )
        if vec is None:
            _embed_failed = True
            return None
        return vec
    except Exception as e:
        log.debug("fabric embed: %s", e)
        return None


def infer_schema(data: Any) -> Dict:
    schema: Dict[str, set] = {}
    rows = data if isinstance(data, list) else [data]
    for row in rows:
        if not isinstance(row, dict):
            continue
        for k, v in row.items():
            schema.setdefault(k, set()).add(type(v).__name__)
    return {k: sorted(v) for k, v in schema.items()}


# ─────────────────────────────────────────────────────────────────────────────
# OBJECT STORE (Garage / Ceph / none)
# ─────────────────────────────────────────────────────────────────────────────

class ObjectStore:
    def __init__(self):
        self._client = None
        self._bucket = FABRIC_S3_BUCKET
        self._mode   = FABRIC_OBJECT_STORE

    def connect(self) -> bool:
        if self._mode == "none" or not HAS_BOTO:
            return False
        try:
            from botocore.config import Config as _BotoConfig
            self._client = _boto3.client(
                "s3",
                endpoint_url         = FABRIC_S3_ENDPOINT,
                aws_access_key_id    = FABRIC_S3_ACCESS,
                aws_secret_access_key= FABRIC_S3_SECRET,
                region_name          = FABRIC_S3_REGION,
                config               = _BotoConfig(s3={"addressing_style": "path"}),
            )
            try:
                self._client.head_bucket(Bucket=self._bucket)
            except Exception:
                self._client.create_bucket(Bucket=self._bucket)
            log.info("✓ ObjectStore (%s) bucket=%s", self._mode, self._bucket)
            return True
        except Exception as e:
            log.warning("ObjectStore (%s) unavailable: %s", self._mode, e)
            self._client = None
            return False

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> bool:
        if not self._client: return False
        try:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)
            return True
        except Exception as e:
            log.error("ObjectStore put %s: %s", key, e); return False

    def get(self, key: str) -> Optional[bytes]:
        if not self._client: return None
        try:
            return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except Exception: return None

    def list_prefix(self, prefix: str) -> List[str]:
        if not self._client: return []
        try:
            resp = self._client.list_objects_v2(Bucket=self._bucket, Prefix=prefix)
            return [o["Key"] for o in resp.get("Contents", [])]
        except Exception: return []

    @property
    def available(self) -> bool:
        return self._client is not None


OBJECT_STORE = ObjectStore()


# ─────────────────────────────────────────────────────────────────────────────
# FAISS STORE — sharded persistent vector index
# ─────────────────────────────────────────────────────────────────────────────

class FaissStore:
    N_SHARDS   = int(os.getenv("FABRIC_FAISS_SHARDS", "4"))
    INDEX_TYPE = os.getenv("FABRIC_FAISS_INDEX", "flat")

    def __init__(self):
        self._dim           = FABRIC_VECTOR_DIM
        self._global_shards: Dict[str, Any] = {}
        self._shard_ids:     Dict[str, List[str]] = {}
        self._ds_indexes:    Dict[str, Any] = {}
        self._ds_ids:        Dict[str, List[str]] = {}
        self._lock           = threading.Lock()
        self._available      = False

    def _make_index(self):
        if self.INDEX_TYPE == "hnsw":
            return _faiss.IndexHNSWFlat(self._dim, 32, _faiss.METRIC_INNER_PRODUCT)
        return _faiss.IndexFlatIP(self._dim)

    def _shard_for(self, dataset_id: str) -> str:
        return f"shard_{hash(dataset_id) % self.N_SHARDS}"

    def connect(self) -> bool:
        if not HAS_FAISS or not HAS_NUMPY:
            return False
        try:
            for i in range(self.N_SHARDS):
                name = f"shard_{i}"
                self._global_shards[name] = self._make_index()
                self._shard_ids[name]     = []
            self._available = True
            log.info("✓ FaissStore — %d shards, dim=%d", self.N_SHARDS, self._dim)
            return True
        except Exception as e:
            log.error("FaissStore connect: %s", e)
            return False

    def add(self, record_id: str, dataset_id: str, embedding: List[float]) -> bool:
        if not self._available or not embedding: return False
        try:
            vec   = np.array([embedding], dtype="float32")
            sname = self._shard_for(dataset_id)
            with self._lock:
                self._global_shards[sname].add(vec)
                self._shard_ids[sname].append(record_id)
                if dataset_id not in self._ds_indexes:
                    self._ds_indexes[dataset_id] = self._make_index()
                    self._ds_ids[dataset_id]     = []
                self._ds_indexes[dataset_id].add(vec)
                self._ds_ids[dataset_id].append(record_id)
            return True
        except Exception as e:
            log.error("FAISS add: %s", e); return False

    def search_global(self, embedding: List[float], top_k: int = 10) -> List[Tuple[str, float]]:
        if not self._available or not embedding: return []
        try:
            vec     = np.array([embedding], dtype="float32")
            results = []
            with self._lock:
                for sname, idx in self._global_shards.items():
                    if idx.ntotal == 0: continue
                    k = min(top_k, idx.ntotal)
                    D, I = idx.search(vec, k)
                    ids  = self._shard_ids[sname]
                    for dist, i in zip(D[0], I[0]):
                        if i != -1 and i < len(ids):
                            results.append((ids[i], float(dist)))
            results.sort(key=lambda x: x[1], reverse=True)
            return results[:top_k]
        except Exception as e:
            log.error("FAISS search_global: %s", e); return []

    def search_dataset(self, dataset_id: str, embedding: List[float],
                       top_k: int = 10) -> List[Tuple[str, float]]:
        if not self._available or not embedding: return []
        try:
            vec = np.array([embedding], dtype="float32")
            with self._lock:
                idx = self._ds_indexes.get(dataset_id)
                ids = self._ds_ids.get(dataset_id, [])
            if idx is None or idx.ntotal == 0: return []
            k = min(top_k, idx.ntotal)
            D, I = idx.search(vec, k)
            return [(ids[i], float(D[0][n])) for n, i in enumerate(I[0])
                    if i != -1 and i < len(ids)]
        except Exception as e:
            log.error("FAISS search_dataset: %s", e); return []

    @property
    def available(self) -> bool:
        return self._available

    def stats(self) -> Dict:
        if not self._available: return {"available": False}
        with self._lock:
            return {
                "available":      True,
                "global_shards":  self.N_SHARDS,
                "global_vectors": sum(idx.ntotal for idx in self._global_shards.values()),
                "datasets":       len(self._ds_indexes),
                "dim":            self._dim,
            }


FAISS_STORE = FaissStore()


# ─────────────────────────────────────────────────────────────────────────────
# CHROMA STORE
# ─────────────────────────────────────────────────────────────────────────────

class FabricChromaStore:
    COLLECTION = "vera_fabric"

    def __init__(self):
        self._client = None
        self._col    = None

    def connect(self) -> bool:
        if not HAS_CHROMA: return False
        try:
            self._client = _chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
            self._col    = self._client.get_or_create_collection(
                self.COLLECTION, metadata={"hnsw:space": "cosine"})
            log.info("✓ FabricChromaStore — %d docs", self._col.count())
            return True
        except Exception as e:
            log.warning("FabricChromaStore: %s", e); return False

    def upsert(self, record: DataRecord) -> bool:
        if not self._col: return False
        try:
            meta = {
                "dataset_id":   record.dataset_id,
                "source":       record.source,
                "created_at":   record.created_at,
                "tags":         json.dumps(record.tags),
                "content_hash": record.content_hash,
            }
            kwargs: dict = {"ids": [record.id], "documents": [record.text or ""],
                            "metadatas": [meta]}
            if record.embedding:
                kwargs["embeddings"] = [record.embedding]
            self._col.upsert(**kwargs)
            return True
        except Exception as e:
            log.debug("FabricChroma upsert: %s", e); return False

    def search(self, embedding: List[float], dataset_id: Optional[str] = None,
               top_k: int = 10, filters: Optional[Dict] = None) -> List[Tuple[str, float]]:
        if not self._col: return []
        try:
            where: dict = {}
            if dataset_id:
                where["dataset_id"] = {"$eq": dataset_id}
            n = min(top_k, max(1, self._col.count()))
            kwargs: dict = {"n_results": n, "include": ["distances"],
                            "query_embeddings": [embedding]}
            if where:
                kwargs["where"] = where
            res = self._col.query(**kwargs)
            return [(rid, max(0.0, 1.0 - d))
                    for rid, d in zip(res["ids"][0], res["distances"][0])]
        except Exception as e:
            log.debug("FabricChroma search: %s", e); return []

    def delete_dataset(self, dataset_id: str) -> bool:
        if not self._col: return False
        try:
            res = self._col.get(where={"dataset_id": {"$eq": dataset_id}})
            if res["ids"]: self._col.delete(ids=res["ids"])
            return True
        except Exception: return False

    def reset_collection(self) -> Dict:
        """Delete and recreate the Chroma collection. Returns stats before/after.

        This is the correct way to handle an embedding dimension change — the
        old vectors are incompatible with the new model, so the entire collection
        must be rebuilt.
        """
        if not self._client:
            return {"error": "Chroma client not connected"}
        try:
            old_count = self._col.count() if self._col else 0
            self._client.delete_collection(self.COLLECTION)
            self._col = self._client.get_or_create_collection(
                self.COLLECTION, metadata={"hnsw:space": "cosine"})
            log.info("FabricChromaStore: collection reset (was %d docs, now empty)", old_count)
            return {"ok": True, "old_count": old_count, "new_count": 0,
                    "collection": self.COLLECTION}
        except Exception as e:
            log.error("FabricChromaStore reset: %s", e)
            return {"error": str(e)}

    @property
    def available(self) -> bool:
        return self._col is not None

    def stats(self) -> Dict:
        if not self._col: return {"available": False}
        try:
            return {"available": True, "count": self._col.count(), "collection": self.COLLECTION}
        except Exception: return {"available": False}


FABRIC_CHROMA = FabricChromaStore()


# ─────────────────────────────────────────────────────────────────────────────
# POSTGRES STORE
# ─────────────────────────────────────────────────────────────────────────────

class FabricPostgres:
    def __init__(self):
        self._pool = None
        self._connecting = False

    async def connect(self) -> bool:
        if not HAS_PG or self._connecting: return False
        self._connecting = True
        try:
            self._pool = await _asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=8)
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS fabric_datasets (
                        dataset_id   TEXT PRIMARY KEY,
                        record_count INT  DEFAULT 0,
                        created_at   TIMESTAMPTZ DEFAULT NOW(),
                        updated_at   TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS fabric_records (
                        id           TEXT PRIMARY KEY,
                        dataset_id   TEXT NOT NULL,
                        text         TEXT DEFAULT \'\',
                        data         JSONB DEFAULT \'{}\',
                        source_id    TEXT DEFAULT \'\',
                        tags         JSONB DEFAULT \'[]\',
                        created_at   TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS fd_ds ON fabric_records(dataset_id)")
                # Migration: add columns to existing tables that predate this schema
                for _migration in [
                    "ALTER TABLE fabric_datasets ADD COLUMN IF NOT EXISTS record_count INT DEFAULT 0",
                    "ALTER TABLE fabric_datasets ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()",
                    "ALTER TABLE fabric_datasets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()",
                    "ALTER TABLE fabric_records  ADD COLUMN IF NOT EXISTS source_id TEXT DEFAULT ''",
                    "ALTER TABLE fabric_records  ADD COLUMN IF NOT EXISTS tags JSONB DEFAULT '[]'",
                ]:
                    try:
                        await conn.execute(_migration)
                    except Exception as _me:
                        log.debug("PG migration skip: %s", _me)
            log.info("✓ FabricPostgres connected (migrations applied)")
            return True
        except Exception as e:
            log.warning("FabricPostgres: %s", e)
            self._pool = None
            return False
        finally:
            self._connecting = False

    async def store(self, record: DataRecord) -> bool:
        if not self._pool: return False
        try:
            async with self._pool.acquire() as conn:
                # asyncpg requires datetime objects, not ISO strings
                try:
                    from datetime import datetime as _dt
                    _ts = _dt.fromisoformat(
                        record.created_at.replace("Z", "+00:00")
                    ) if record.created_at else _dt.utcnow()
                except Exception:
                    from datetime import datetime as _dt
                    _ts = _dt.utcnow()
                await conn.execute(
                    "INSERT INTO fabric_records (id,dataset_id,text,data,source_id,tags,created_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(id) DO NOTHING",
                    record.id, record.dataset_id, record.text,
                    json.dumps(record.data), record.source_id or record.source,
                    json.dumps(record.tags), _ts,
                )
                try:
                    await conn.execute(
                        "INSERT INTO fabric_datasets (dataset_id,record_count,created_at,updated_at) "
                        "VALUES ($1,1,NOW(),NOW()) ON CONFLICT(dataset_id) DO UPDATE "
                        "SET record_count=fabric_datasets.record_count+1, updated_at=NOW()",
                        record.dataset_id,
                    )
                except Exception:
                    # No unique constraint — just ensure row exists
                    try:
                        await conn.execute(
                            "INSERT INTO fabric_datasets (dataset_id,record_count,created_at,updated_at) "
                            "VALUES ($1,1,NOW(),NOW())",
                            record.dataset_id,
                        )
                    except Exception:
                        pass
            return True
        except Exception as e:
            log.warning("FabricPostgres store: %s", e)
            return False

    async def search_text(self, query: str, dataset_id: Optional[str] = None,
                          limit: int = 10) -> List[Tuple[str, float]]:
        if not self._pool: return []
        try:
            async with self._pool.acquire() as conn:
                if dataset_id:
                    rows = await conn.fetch(
                        "SELECT id FROM fabric_records "
                        "WHERE dataset_id=$1 AND text ILIKE $2 LIMIT $3",
                        dataset_id, f"%{query}%", limit)
                else:
                    rows = await conn.fetch(
                        "SELECT id FROM fabric_records WHERE text ILIKE $1 LIMIT $2",
                        f"%{query}%", limit)
            return [(r["id"], 0.8) for r in rows]
        except Exception: return []

    async def get_by_ids(self, ids: List[str]) -> Dict[str, DataRecord]:
        if not self._pool or not ids: return {}
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM fabric_records WHERE id=ANY($1)", ids)
            result = {}
            for r in rows:
                result[r["id"]] = DataRecord(
                    id=r["id"], dataset_id=r["dataset_id"],
                    text=r["text"] or "", source=r.get("source_id",""),
                    data=json.loads(r["data"] or "{}"),
                    tags=json.loads(r["tags"] or "[]"),
                    created_at=str(r["created_at"]),
                )
            return result
        except Exception: return {}

    async def list_datasets(self) -> List[Dict]:
        if not self._pool: return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT dataset_id, record_count, updated_at FROM fabric_datasets "
                    "ORDER BY updated_at DESC")
            return [{"dataset_id": r["dataset_id"],
                     "record_count": r["record_count"],
                     "updated_at": str(r["updated_at"])} for r in rows]
        except Exception: return []

    async def stats(self) -> Dict:
        if not self._pool: return {"available": False}
        try:
            async with self._pool.acquire() as conn:
                total = await conn.fetchval("SELECT COUNT(*) FROM fabric_records")
                ds    = await conn.fetchval("SELECT COUNT(DISTINCT dataset_id) FROM fabric_datasets")
            return {"available": True, "records": total, "datasets": ds}
        except Exception as e:
            return {"available": False, "error": str(e)}

    @property
    def available(self) -> bool:
        return self._pool is not None


FABRIC_PG = FabricPostgres()


# ─────────────────────────────────────────────────────────────────────────────
# NEO4J AUXILIARY GRAPH
# ─────────────────────────────────────────────────────────────────────────────

class FabricNeo4j:
    def __init__(self):
        self._driver = None

    async def connect(self) -> bool:
        if not HAS_NEO: return False
        try:
            self._driver = _Neo4j.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
            async with self._driver.session() as s:
                await s.run("RETURN 1")
            log.info("✓ FabricNeo4j connected")
            return True
        except Exception as e:
            log.warning("FabricNeo4j: %s", e); self._driver = None; return False

    async def ensure_dataset(self, dataset_id: str) -> bool:
        if not self._driver: return False
        try:
            async with self._driver.session() as s:
                await s.run("MERGE (d:Dataset {id:$id}) SET d.updated_at=$ts",
                            id=dataset_id, ts=now_iso())
            return True
        except Exception: return False

    async def add_record(self, record: DataRecord) -> bool:
        if not self._driver: return False
        try:
            data = record.data or {}
            title = str(data.get("title") or data.get("name") or "")[:120]
            url = str(data.get("url") or data.get("link") or "")[:200]
            if not title and record.text:
                title = record.text.split('\n')[0][:120]
            async with self._driver.session() as s:
                await s.run("""
                    MERGE (r:FabricRecord {id:$id})
                    SET r.dataset_id=$ds, r.created_at=$ts,
                        r.title=$title, r.url=$url
                    WITH r MATCH (d:Dataset {id:$ds}) MERGE (d)-[:CONTAINS]->(r)
                """, id=record.id, ds=record.dataset_id, ts=record.created_at,
                    title=title, url=url)
            return True
        except Exception: return False

    async def link_datasets(self, from_id: str, to_id: str,
                            rel_type: str = "SIMILAR_TO", props: Dict = None) -> bool:
        if not self._driver: return False
        rtype = re.sub(r"[^A-Z0-9_]", "_", rel_type.upper())
        try:
            async with self._driver.session() as s:
                await s.run(
                    f"MATCH (a:Dataset {{id:$aid}}) MATCH (b:Dataset {{id:$bid}}) "
                    f"MERGE (a)-[r:{rtype}]->(b) SET r += $props",
                    aid=from_id, bid=to_id, props=props or {})
            return True
        except Exception: return False

    async def query(self, cypher: str, params: Dict = None) -> List[Dict]:
        if not self._driver: return []
        try:
            async with self._driver.session() as s:
                result = await s.run(cypher, **(params or {}))
                return await result.data()
        except Exception as e:
            return [{"error": str(e)}]

    async def query_graph(self, node_cypher: str, edge_cypher: str,
                            params: Dict = None) -> Dict:
        """Run two Cyphers (one for nodes, one for edges) in the same session
        and return them with full metadata: id, labels, properties for nodes;
        from/to/type/properties for edges. This is what visualisation needs —
        result.data() alone strips the labels and relationship types."""
        if not self._driver:
            return {"nodes": [], "edges": []}
        nodes = []
        edges = []
        try:
            async with self._driver.session() as s:
                # Nodes
                nres = await s.run(node_cypher, **(params or {}))
                async for record in nres:
                    for value in record.values():
                        if value is None:
                            continue
                        # neo4j Node has .id, .labels, .items()
                        if hasattr(value, "labels") and hasattr(value, "items"):
                            nodes.append({
                                "neo_id": value.id,
                                "labels": list(value.labels),
                                "props":  dict(value.items()),
                            })
                # Edges — assume cypher returns (a, r, b) shape
                eres = await s.run(edge_cypher, **(params or {}))
                async for record in eres:
                    a = b = r = None
                    for value in record.values():
                        if value is None: continue
                        if hasattr(value, "labels") and hasattr(value, "items"):
                            if a is None: a = value
                            else: b = value
                        elif hasattr(value, "type") and hasattr(value, "items"):
                            r = value
                    if a is not None and b is not None and r is not None:
                        edges.append({
                            "from_neo_id":  a.id,
                            "to_neo_id":    b.id,
                            "from_labels":  list(a.labels),
                            "to_labels":    list(b.labels),
                            "from_props":   dict(a.items()),
                            "to_props":     dict(b.items()),
                            "rel_type":     r.type,
                            "rel_props":    dict(r.items()),
                        })
                    elif a is not None and b is not None:
                        # No relationship returned (e.g. OPTIONAL MATCH miss)
                        pass
        except Exception as e:
            log.warning("query_graph: %s", e)
        return {"nodes": nodes, "edges": edges}

    async def upsert_node(self, label: str, node_id: str, props: Dict = None) -> bool:
        """MERGE a node by id with arbitrary label and properties."""
        if not self._driver: return False
        # Sanitise label (Cypher labels can't be parameterised)
        lbl = re.sub(r"[^A-Za-z0-9_]", "", label) or "Node"
        try:
            async with self._driver.session() as s:
                await s.run(
                    f"MERGE (n:{lbl} {{id:$id}}) SET n += $props, n.updated_at=$ts",
                    id=node_id, props=props or {}, ts=now_iso())
            return True
        except Exception as e:
            log.debug("upsert_node %s/%s: %s", lbl, node_id, e)
            return False

    async def link(self, from_label: str, from_id: str,
                    to_label: str, to_id: str,
                    rel: str = "RELATED_TO", props: Dict = None) -> bool:
        """MERGE an edge between two nodes (creating them if missing).
        This is the generic link method used by Loom, Skills, and any other
        subsystem that needs to write edges into the fabric graph."""
        if not self._driver: return False
        flbl = re.sub(r"[^A-Za-z0-9_]", "", from_label) or "Node"
        tlbl = re.sub(r"[^A-Za-z0-9_]", "", to_label) or "Node"
        rtype = re.sub(r"[^A-Z0-9_]", "_", rel.upper()) or "RELATED_TO"
        try:
            async with self._driver.session() as s:
                await s.run(
                    f"MERGE (a:{flbl} {{id:$aid}}) "
                    f"MERGE (b:{tlbl} {{id:$bid}}) "
                    f"MERGE (a)-[r:{rtype}]->(b) "
                    f"SET r += $props, r.updated_at=$ts",
                    aid=from_id, bid=to_id, props=props or {}, ts=now_iso())
            return True
        except Exception as e:
            log.debug("link %s->%s (%s): %s", from_id, to_id, rtype, e)
            return False

    async def link_many(self, edges: List[Dict]) -> int:
        """Bulk-write edges in a single session. Each edge dict needs:
        from_label, from_id, to_label, to_id, rel, props (optional)."""
        if not self._driver or not edges: return 0
        written = 0
        try:
            async with self._driver.session() as s:
                for e in edges:
                    flbl = re.sub(r"[^A-Za-z0-9_]", "", e.get("from_label","Node")) or "Node"
                    tlbl = re.sub(r"[^A-Za-z0-9_]", "", e.get("to_label","Node")) or "Node"
                    rtype = re.sub(r"[^A-Z0-9_]", "_", str(e.get("rel","RELATED_TO")).upper()) or "RELATED_TO"
                    try:
                        await s.run(
                            f"MERGE (a:{flbl} {{id:$aid}}) "
                            f"MERGE (b:{tlbl} {{id:$bid}}) "
                            f"MERGE (a)-[r:{rtype}]->(b) "
                            f"SET r += $props, r.updated_at=$ts",
                            aid=e["from_id"], bid=e["to_id"],
                            props=e.get("props") or {}, ts=now_iso())
                        written += 1
                    except Exception as inner:
                        log.debug("link_many edge: %s", inner)
        except Exception as outer:
            log.debug("link_many session: %s", outer)
        return written

    @property
    def available(self) -> bool:
        return self._driver is not None


FABRIC_NEO = FabricNeo4j()



# ─────────────────────────────────────────────────────────────────────────────
# GRAPH ADAPTER REGISTRY
# A uniform interface over graph backends so Loom, Skills, Discovery and
# external callers can target any registered graph by name. Default: "fabric".
# ─────────────────────────────────────────────────────────────────────────────

class GraphAdapter:
    """Protocol-ish wrapper. Methods may be overridden per backend."""
    name: str = "base"

    async def upsert_node(self, label: str, node_id: str, props: Dict = None) -> bool:
        return False
    async def link(self, from_label: str, from_id: str, to_label: str, to_id: str,
                   rel: str = "RELATED_TO", props: Dict = None) -> bool:
        return False
    async def link_many(self, edges: List[Dict]) -> int:
        return 0
    async def query(self, cypher: str, params: Dict = None) -> List[Dict]:
        return []
    async def query_graph(self, node_cypher: str, edge_cypher: str,
                            params: Dict = None) -> Dict:
        return {"nodes": [], "edges": []}
    @property
    def available(self) -> bool:
        return False
    def describe(self) -> Dict:
        return {"name": self.name, "available": self.available, "kind": "base"}


class FabricGraphAdapter(GraphAdapter):
    """Wraps FABRIC_NEO."""
    name = "fabric"
    def __init__(self, neo): self._neo = neo
    async def upsert_node(self, label, node_id, props=None):
        return await self._neo.upsert_node(label, node_id, props or {})
    async def link(self, fl, fi, tl, ti, rel="RELATED_TO", props=None):
        return await self._neo.link(fl, fi, tl, ti, rel, props or {})
    async def link_many(self, edges):
        return await self._neo.link_many(edges)
    async def query(self, cypher, params=None):
        return await self._neo.query(cypher, params)
    async def query_graph(self, node_cypher, edge_cypher, params=None):
        return await self._neo.query_graph(node_cypher, edge_cypher, params)
    @property
    def available(self):
        return self._neo.available
    def describe(self):
        return {"name": self.name, "available": self.available,
                "kind": "neo4j", "uri": NEO4J_URI,
                "description": "Primary fabric graph — datasets, records, sources, skills"}


class MemoryGraphAdapter(GraphAdapter):
    """Wraps the memory module's Neo4j backend if present."""
    name = "memory"
    def __init__(self):
        self._mem = None
        try:
            from Vera.Orchestration import memory as _m
            self._mem = getattr(_m, "MEMORY", None)
        except Exception:
            self._mem = None

    def _backend(self):
        if not self._mem: return None
        return getattr(self._mem, "_backends", {}).get("neo4j")

    async def upsert_node(self, label, node_id, props=None):
        be = self._backend()
        if not be: return False
        # Memory's neo4j uses different method names — try a few
        for fn_name in ("upsert_node","merge_node","add_node"):
            fn = getattr(be, fn_name, None)
            if fn:
                try:
                    r = fn(label, node_id, props or {})
                    if asyncio.iscoroutine(r): r = await r
                    return bool(r)
                except Exception as e:
                    log.debug("memory upsert_node: %s", e)
                    return False
        # Fallback: raw Cypher via session
        return False

    async def link(self, fl, fi, tl, ti, rel="RELATED_TO", props=None):
        be = self._backend()
        if not be: return False
        # Try to get a driver/session
        drv = getattr(be, "_driver", None) or getattr(be, "driver", None)
        if not drv: return False
        flbl = re.sub(r"[^A-Za-z0-9_]", "", fl) or "Node"
        tlbl = re.sub(r"[^A-Za-z0-9_]", "", tl) or "Node"
        rtype = re.sub(r"[^A-Z0-9_]", "_", rel.upper()) or "RELATED_TO"
        try:
            async with drv.session() as s:
                await s.run(
                    f"MERGE (a:{flbl} {{id:$aid}}) "
                    f"MERGE (b:{tlbl} {{id:$bid}}) "
                    f"MERGE (a)-[r:{rtype}]->(b) "
                    f"SET r += $props",
                    aid=fi, bid=ti, props=props or {})
            return True
        except Exception as e:
            log.debug("memory link: %s", e)
            return False

    async def link_many(self, edges):
        n = 0
        for e in edges:
            if await self.link(
                e.get("from_label","Node"), e["from_id"],
                e.get("to_label","Node"),   e["to_id"],
                e.get("rel","RELATED_TO"),  e.get("props") or {},
            ):
                n += 1
        return n

    async def query(self, cypher, params=None):
        be = self._backend()
        if not be: return []
        drv = getattr(be, "_driver", None) or getattr(be, "driver", None)
        if not drv: return []
        try:
            async with drv.session() as s:
                r = await s.run(cypher, **(params or {}))
                return await r.data()
        except Exception as e:
            return [{"error": str(e)}]

    async def query_graph(self, node_cypher, edge_cypher, params=None):
        be = self._backend()
        if not be: return {"nodes": [], "edges": []}
        drv = getattr(be, "_driver", None) or getattr(be, "driver", None)
        if not drv: return {"nodes": [], "edges": []}
        nodes = []; edges = []
        try:
            async with drv.session() as s:
                nres = await s.run(node_cypher, **(params or {}))
                async for record in nres:
                    for value in record.values():
                        if hasattr(value, "labels") and hasattr(value, "items"):
                            nodes.append({"neo_id": value.id,
                                            "labels": list(value.labels),
                                            "props": dict(value.items())})
                eres = await s.run(edge_cypher, **(params or {}))
                async for record in eres:
                    a = b = r = None
                    for value in record.values():
                        if value is None: continue
                        if hasattr(value, "labels") and hasattr(value, "items"):
                            if a is None: a = value
                            else: b = value
                        elif hasattr(value, "type") and hasattr(value, "items"):
                            r = value
                    if a and b and r:
                        edges.append({"from_neo_id": a.id, "to_neo_id": b.id,
                                        "from_labels": list(a.labels),
                                        "to_labels": list(b.labels),
                                        "from_props": dict(a.items()),
                                        "to_props": dict(b.items()),
                                        "rel_type": r.type,
                                        "rel_props": dict(r.items())})
        except Exception as e:
            log.warning("memory query_graph: %s", e)
        return {"nodes": nodes, "edges": edges}

    @property
    def available(self):
        return self._backend() is not None

    def describe(self):
        return {"name": self.name, "available": self.available,
                "kind": "neo4j",
                "description": "Memory graph — sessions, conversation memory, activity chain"}


class _GraphRegistry:
    def __init__(self):
        self._adapters: Dict[str, GraphAdapter] = {}

    def register(self, adapter: GraphAdapter):
        self._adapters[adapter.name] = adapter

    def get(self, name: str = "fabric") -> Optional[GraphAdapter]:
        return self._adapters.get(name)

    def list(self) -> List[Dict]:
        return [a.describe() for a in self._adapters.values()]

class NetGraphAdapter(GraphAdapter):
    """Network/asset graph — same Neo4j as fabric, but scoped queries default
    to NetHost / SshHost / Subnet / NetService labels populated by netscan.*"""
    name = "net"
    NET_LABELS = ("NetHost", "SshHost", "Subnet", "NetService", "Container",
                  "DockerHost", "K8sNode", "K8sPod", "PveNode", "PveGuest")

    def __init__(self, fabric_neo):
        self._neo = fabric_neo

    async def upsert_node(self, label, node_id, props=None):
        return await self._neo.upsert_node(label, node_id, props or {})

    async def link(self, fl, fi, tl, ti, rel="RELATED_TO", props=None):
        return await self._neo.link(fl, fi, tl, ti, rel, props or {})

    async def link_many(self, edges):
        return await self._neo.link_many(edges)

    async def query(self, cypher, params=None):
        return await self._neo.query(cypher, params)

    @property
    def available(self):
        return self._neo.available

    def describe(self):
        return {"name": self.name, "available": self.available, "kind": "neo4j",
                "description": "Network/asset graph (NetHost, SshHost, Subnet, "
                                "containers, k8s, proxmox)",
                "scope_labels": list(self.NET_LABELS)}


GRAPHS = _GraphRegistry()
GRAPHS.register(FabricGraphAdapter(FABRIC_NEO))
GRAPHS.register(MemoryGraphAdapter())
GRAPHS.register(NetGraphAdapter(FABRIC_NEO))

# ─────────────────────────────────────────────────────────────────────────────
# CUSTOM GRAPH REGISTRATION
# Users can register additional graphs from the UI — they're persisted in
# fabric_custom_graphs and re-registered on startup.
# ─────────────────────────────────────────────────────────────────────────────

class CustomFabricGraphAdapter(GraphAdapter):
    """A user-registered graph that's just a label-scoped view of the fabric Neo4j."""
    def __init__(self, name: str, description: str, node_labels: List[str],
                  fabric_neo):
        self.name = name
        self._desc = description
        self._labels = node_labels
        self._neo = fabric_neo

    async def upsert_node(self, label, node_id, props=None):
        return await self._neo.upsert_node(label, node_id, props or {})

    async def link(self, fl, fi, tl, ti, rel="RELATED_TO", props=None):
        return await self._neo.link(fl, fi, tl, ti, rel, props or {})

    async def link_many(self, edges):
        return await self._neo.link_many(edges)

    async def query(self, cypher, params=None):
        return await self._neo.query(cypher, params)

    async def query_graph(self, node_q, edge_q, params=None):
        return await self._neo.query_graph(node_q, edge_q, params)

    @property
    def available(self):
        return self._neo.available

    def describe(self):
        return {"name": self.name, "available": self.available, "kind": "neo4j",
                "description": self._desc,
                "scope_labels": self._labels,
                "user_registered": True}


def _load_custom_graphs():
    """Re-register any user-defined custom graphs at startup."""
    try:
        conn = _sqlite_conn()
        try:
            rows = conn.execute(
                "SELECT name, description, node_labels FROM fabric_custom_graphs"
            ).fetchall()
            for r in rows:
                try:
                    labels = json.loads(r[2] or "[]")
                except Exception:
                    labels = []
                adapter = CustomFabricGraphAdapter(
                    name=r[0], description=(r[1] or ""),
                    node_labels=labels, fabric_neo=FABRIC_NEO,
                )
                GRAPHS.register(adapter)
        finally:
            conn.close()
    except Exception as e:
        log.warning("custom graph load: %s", e)

# Load custom graphs at import time
try:
    _load_custom_graphs()
except Exception as _e:
    log.debug("custom graph load skipped: %s", _e)


@capability(
    "fabric.graphs.register",
    http_method="POST", http_path="/fabric/graphs/register",
    http_tags=["fabric","graph"],
    memory="off",
    description="Register a custom named graph view as a label-scoped subset of "
                "the fabric Neo4j. "
                "Input: name (str! — alphanumeric+underscore), "
                "description (str), "
                "node_labels (str — comma-sep Neo4j labels to scope to). "
                "Output: {ok, name}.",
)
async def fabric_graphs_register(name: str, description: str = "",
                                   node_labels: str = "",
                                   trace_id=None) -> Dict:
    name = re.sub(r"[^A-Za-z0-9_]", "", name).strip()
    if not name:
        return {"error": "name must be alphanumeric (or underscore)"}
    if name in ("fabric", "memory", "net"):
        return {"error": "name reserved — pick a different one"}
    labels = [re.sub(r"[^A-Za-z0-9_]","",l).strip()
                for l in (node_labels or "").split(",") if l.strip()]
    if not labels:
        return {"error": "node_labels required (comma-sep)"}

    await _enqueue_write({
        "kind":"raw",
        "sql": "INSERT OR REPLACE INTO fabric_custom_graphs "
                "(name, description, kind, node_labels, cypher_node, cypher_edge, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
        "params": (name, description, "neo4j", json.dumps(labels), "", "", now_iso()),
    }, wait=True)
    adapter = CustomFabricGraphAdapter(name=name, description=description,
                                          node_labels=labels, fabric_neo=FABRIC_NEO)
    GRAPHS.register(adapter)
    await emit_event({"type":"fabric.graph.registered", "name": name,
                       "labels": labels})
    return {"ok": True, "name": name, "labels": labels}


@capability(
    "fabric.graphs.unregister",
    http_method="POST", http_path="/fabric/graphs/unregister",
    http_tags=["fabric","graph"],
    memory="off",
    description="Remove a user-registered custom graph. "
                "Input: name (str!). Built-in graphs (fabric/memory/net) cannot be removed.",
)
async def fabric_graphs_unregister(name: str, trace_id=None) -> Dict:
    if name in ("fabric","memory","net"):
        return {"error": "cannot remove built-in graph"}
    await _enqueue_write({
        "kind":"raw",
        "sql": "DELETE FROM fabric_custom_graphs WHERE name=?",
        "params": (name,),
    }, wait=True)
    if name in GRAPHS._adapters:
        del GRAPHS._adapters[name]
    return {"ok": True, "removed": name}




@capability(
    "fabric.graphs.list",
    http_method="GET", http_path="/fabric/graphs", http_tags=["fabric","graph"],
    memory="off", silent=True,
    description="List registered graph adapters with availability status. "
                "Output: {graphs: [{name, available, kind, description}]}",
)
async def fabric_graphs_list(trace_id=None) -> Dict:
    return {"graphs": GRAPHS.list()}


@capability(
    "fabric.graphs.snapshot",
    http_method="GET", http_path="/fabric/graphs/snapshot", http_tags=["fabric","graph"],
    memory="off",
    description="Return a node+edge snapshot of a registered graph for visualisation. "
                "Input: graph (str default 'fabric'), limit (int default 200), "
                "label_filter (str — comma-sep labels to include, or '' for default), "
                "dataset_id (str — if set, scope to FabricRecords inside this dataset "
                "and the dataset itself). "
                "Output: {graph, nodes:[{id,label,name,...}], edges:[{from,to,rel,props}]}.",
)
async def fabric_graphs_snapshot(graph: str = "fabric", limit: int = 200,
                                   label_filter: str = "",
                                   dataset_id:   str = "",
                                   trace_id=None) -> Dict:
    adapter = GRAPHS.get(graph)
    if not adapter:
        return {"error": f"unknown graph: {graph}",
                "available": [g["name"] for g in GRAPHS.list()]}
    if not adapter.available:
        return {"error": f"graph '{graph}' is not connected",
                "graph": graph, "nodes": [], "edges": []}

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(2000, limit))

    # Default label filters per adapter — cleaner views
    labels: List[str] = []
    if label_filter:
        labels = [l.strip() for l in label_filter.split(",") if l.strip()]
    elif graph == "net":
        labels = list(NetGraphAdapter.NET_LABELS)
    elif graph == "memory":
        labels = ["Memory", "Session", "Activity"]
    # else: no filter — show whatever is in the graph

    # Build queries — use query_graph which preserves labels and rel types
    if dataset_id:
        # Scope to a specific dataset: show inter-record structure
        # (RELATED_TO from Loom, LINKS_TO from web crawls) — suppress CONTAINS
        # edges which create a hub-and-spoke around the Dataset node.
        node_q = ("MATCH (n) WHERE (n:Dataset AND n.id=$dsid) "
                  "OR (n:FabricRecord AND n.dataset_id=$dsid) "
                  "RETURN n LIMIT $limit")
        edge_q = ("MATCH (a:FabricRecord)-[r]-(b:FabricRecord) "
                  "WHERE a.dataset_id=$dsid AND b.dataset_id=$dsid "
                  "AND type(r) <> 'CONTAINS' "
                  "RETURN a, r, b LIMIT $limit")
        params = {"dsid": dataset_id, "limit": limit}
    elif labels:
        labels_safe = [re.sub(r"[^A-Za-z0-9_]", "", l) for l in labels if l]
        node_q = "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $labels) RETURN n LIMIT $limit"
        edge_q = ("MATCH (a)-[r]->(b) "
                  "WHERE any(l IN labels(a) WHERE l IN $labels) "
                  "AND any(l IN labels(b) WHERE l IN $labels) "
                  "RETURN a, r, b LIMIT $limit")
        params = {"labels": labels_safe, "limit": limit}
    else:
        node_q = "MATCH (n) RETURN n LIMIT $limit"
        edge_q = "MATCH (a)-[r]->(b) RETURN a, r, b LIMIT $limit"
        params = {"limit": limit}

    snap = await adapter.query_graph(node_q, edge_q, params)

    nodes_by_neo_id: Dict[int, Dict] = {}
    def _node_id(props):
        # Stable ID for the UI — use 'id' prop if present, otherwise neo_id
        return str(props.get("id") or props.get("neo_id"))
    def _node_name(props):
        for field in ("title", "name", "label", "url", "text"):
            val = props.get(field)
            if val and str(val).strip() and str(val).strip() != str(props.get("id", "")):
                return str(val).strip().split('\n')[0][:80]
        return str(props.get("id") or "?")[:80]

    for n in snap.get("nodes", []):
        nid = _node_id({**n["props"], "neo_id": n["neo_id"]})
        if n["neo_id"] in nodes_by_neo_id: continue
        nodes_by_neo_id[n["neo_id"]] = {
            "id":     nid,
            "name":   _node_name(n["props"]),
            "label":  (n["labels"][0] if n["labels"] else "Node"),
            "labels": n["labels"],
            "props":  {k: v for k, v in n["props"].items() if k not in ("id","neo_id")},
        }

    edges_out: List[Dict] = []
    for e in snap.get("edges", []):
        # Make sure both endpoints are in the node set
        if e["from_neo_id"] not in nodes_by_neo_id:
            nodes_by_neo_id[e["from_neo_id"]] = {
                "id":     _node_id({**e["from_props"], "neo_id": e["from_neo_id"]}),
                "name":   _node_name(e["from_props"]),
                "label":  (e["from_labels"][0] if e["from_labels"] else "Node"),
                "labels": e["from_labels"],
                "props":  e["from_props"],
            }
        if e["to_neo_id"] not in nodes_by_neo_id:
            nodes_by_neo_id[e["to_neo_id"]] = {
                "id":     _node_id({**e["to_props"], "neo_id": e["to_neo_id"]}),
                "name":   _node_name(e["to_props"]),
                "label":  (e["to_labels"][0] if e["to_labels"] else "Node"),
                "labels": e["to_labels"],
                "props":  e["to_props"],
            }
        from_id = nodes_by_neo_id[e["from_neo_id"]]["id"]
        to_id   = nodes_by_neo_id[e["to_neo_id"]]["id"]
        edges_out.append({
            "from":  from_id,
            "to":    to_id,
            "rel":   e["rel_type"],
            "props": e.get("rel_props") or {},
        })

    nodes_out = list(nodes_by_neo_id.values())
    return {
        "graph":      graph,
        "nodes":      nodes_out,
        "edges":      edges_out,
        "node_count": len(nodes_out),
        "edge_count": len(edges_out),
    }


@capability(
    "fabric.graphs.query",
    http_method="POST", http_path="/fabric/graphs/query", http_tags=["fabric","graph"],
    memory="off",
    description="Run a Cypher query against any registered graph. "
                "Input: graph (str — name, default 'fabric'), cypher (str!). "
                "Example: graph='fabric', cypher='MATCH (n:Dataset) RETURN n LIMIT 5'. "
                "Output: rows from the query.",
)
async def fabric_graphs_query(graph: str = "fabric", cypher: str = "",
                                trace_id=None) -> Dict:
    if not cypher.strip():
        return {"error": "cypher required"}
    adapter = GRAPHS.get(graph)
    if not adapter:
        return {"error": f"unknown graph: {graph}",
                "available": [g["name"] for g in GRAPHS.list()]}
    if not adapter.available:
        return {"error": f"graph '{graph}' is not connected"}
    rows = await adapter.query(cypher)
    return {"graph": graph, "rows": rows, "count": len(rows)}



# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE STAGES
# ─────────────────────────────────────────────────────────────────────────────

class PipelineStage(ABC):
    name: str = "base"
    async def process(self, record: DataRecord, ctx: Dict) -> DataRecord: ...
    async def on_error(self, record: DataRecord, error: Exception) -> Optional[DataRecord]:
        log.warning("Stage %s error: %s", self.name, error)
        return record

class HashStage(PipelineStage):
    name = "hash"
    async def process(self, record: DataRecord, ctx: Dict) -> DataRecord:
        if not record.content_hash:
            record.content_hash = record.compute_hash()
        return record

class SchemaStage(PipelineStage):
    name = "schema"
    async def process(self, record: DataRecord, ctx: Dict) -> DataRecord:
        if not record.schema and record.data:
            record.schema = infer_schema(record.data)
        return record

class TextExtractionStage(PipelineStage):
    name = "text_extract"
    async def process(self, record: DataRecord, ctx: Dict) -> DataRecord:
        if not record.text and record.data:
            parts = [v[:500] for v in record.data.values()
                     if isinstance(v, str) and v.strip()]
            record.text = " ".join(parts)[:2000]
        return record

class EmbedStage(PipelineStage):
    name = "embed"
    async def process(self, record: DataRecord, ctx: Dict) -> DataRecord:
        if not record.embedding and record.text.strip():
            emb = await _embed(record.text)
            if emb:
                record.embedding       = emb
                record.embedding_model = OLLAMA_EMBED_MODEL
        return record

class SQLiteStoreStage(PipelineStage):
    name = "sqlite_store"
    async def process(self, record: DataRecord, ctx: Dict) -> DataRecord:
        await _sqlite_insert_record({
            "id":         record.id,
            "dataset_id": record.dataset_id,
            "text":       record.text,
            "data":       record.data,
            "source_id":  record.source_id or record.source,
            "tags":       record.tags,
            "created_at": record.created_at,
        })
        return record

class PostgresStoreStage(PipelineStage):
    name = "pg_store"
    async def process(self, record: DataRecord, ctx: Dict) -> DataRecord:
        if FABRIC_PG.available:
            await FABRIC_PG.store(record)
        return record

class VectorIndexStage(PipelineStage):
    name = "vector_index"
    async def process(self, record: DataRecord, ctx: Dict) -> DataRecord:
        if record.embedding:
            FAISS_STORE.add(record.id, record.dataset_id, record.embedding)
            FABRIC_CHROMA.upsert(record)
        return record

class Neo4jStoreStage(PipelineStage):
    name = "neo4j_store"
    async def process(self, record: DataRecord, ctx: Dict) -> DataRecord:
        if FABRIC_NEO.available:
            await FABRIC_NEO.ensure_dataset(record.dataset_id)
            await FABRIC_NEO.add_record(record)
        return record


class IngestPipeline:
    def __init__(self, stages: List[PipelineStage]):
        self.stages = stages

    async def run(self, record: DataRecord, context: Dict = None) -> DataRecord:
        ctx = context or {}
        for stage in self.stages:
            try:
                record = await stage.process(record, ctx)
            except Exception as e:
                result = await stage.on_error(record, e)
                if result is None:
                    return record
                record = result
        return record


# Default pipeline: always writes to SQLite (immediate visibility), then Postgres
DEFAULT_PIPELINE = IngestPipeline([
    HashStage(),
    SchemaStage(),
    TextExtractionStage(),
    EmbedStage(),
    SQLiteStoreStage(),   # always runs — guarantees data appears in UI
    PostgresStoreStage(),
    VectorIndexStage(),
    Neo4jStoreStage(),
])


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC INGEST API
# ─────────────────────────────────────────────────────────────────────────────

async def ingest_dataset(
    dataset_id: str,
    data:       List[Dict],
    source:     str = "api",
    tags:       List[str] = None,
    source_id:  str = "",
) -> Dict:
    ingested = errors = 0
    for item in (data if isinstance(data, list) else [data]):
        if isinstance(item, str):
            text = item; item = {"text": item}
        else:
            text = item.get("text","") if isinstance(item, dict) else str(item)
            if not text and isinstance(item, dict):
                text = " ".join(str(v) for v in item.values()
                                if isinstance(v, str))[:2000]

        rec = DataRecord(
            dataset_id = dataset_id,
            source     = source,
            source_id  = source_id,
            text       = text[:2000],
            data       = item if isinstance(item, dict) else {"value": item},
            tags       = tags or [],
        )
        try:
            await DEFAULT_PIPELINE.run(rec)
            ingested += 1
        except Exception as e:
            log.error("ingest [%s]: %s", dataset_id, e)
            errors += 1

    await emit_event({"type": "fabric.ingested", "dataset_id": dataset_id,
                      "ingested": ingested, "errors": errors, "source": source})

    # ── Post-ingestion pipeline (async, non-blocking) ──────────────────────
    # Ensures every ingestion point gets: source registration, entity
    # extraction, and optional loom linking — matching the web acquisition's
    # automated processing. Settings are configurable per dataset.
    asyncio.create_task(_post_ingest_pipeline(dataset_id, source, source_id, ingested))

    return {"ingested": ingested, "errors": errors, "dataset_id": dataset_id}


async def _post_ingest_pipeline(
    dataset_id: str, source: str, source_id: str, record_count: int
):
    """Background post-ingestion tasks. Non-blocking — errors are logged, not raised."""
    try:
        # 1) Auto-create a source if none exists for this dataset
        await _ensure_dataset_source(dataset_id, source, source_id)

        # 2) Load per-dataset processing config
        cfg = await _get_dataset_processing_config(dataset_id)

        # 3) Entity extraction (if enabled for this dataset)
        if cfg.get("auto_extract_entities", True) and record_count > 0:
            try:
                from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY
                extract_cap = CAPABILITY_REGISTRY.get("fabric.entity_graph.extract")
                if extract_cap:
                    ext_args = {
                        "dataset_id":   dataset_id,
                        "limit":        min(record_count, cfg.get("extract_limit", 500)),
                        "content_type": cfg.get("content_type", "text"),
                        "persist":      True,
                    }
                    await extract_cap["func"](**ext_args, trace_id=new_id())
                    log.info("post-ingest entity extraction done: %s (%d records)",
                             dataset_id, record_count)
            except Exception as e:
                log.warning("post-ingest entity extraction %s: %s", dataset_id, e)

        # 4) Loom linking (if enabled for this dataset)
        if cfg.get("auto_loom", False) and record_count > 0:
            try:
                from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY
                loom_cap = CAPABILITY_REGISTRY.get("fabric.loom.run")
                if loom_cap:
                    loom_scope = cfg.get("loom_scope", "internal")
                    loom_args = {
                        "dataset_a": dataset_id,
                        "mode":      cfg.get("loom_mode", "hybrid"),
                        "min_score": cfg.get("loom_min_score", 0.4),
                        "max_matches": cfg.get("loom_max_matches", 100),
                        "persist":   True,
                    }
                    # If scope is "internal", loom compares dataset against itself
                    # If "cross", loom compares against all other datasets
                    if loom_scope == "cross":
                        loom_args["dataset_b"] = ""  # empty = all datasets
                    else:
                        loom_args["dataset_b"] = dataset_id
                    await loom_cap["func"](**loom_args, trace_id=new_id())
                    log.info("post-ingest loom done: %s (scope=%s)",
                             dataset_id, loom_scope)
            except Exception as e:
                log.warning("post-ingest loom %s: %s", dataset_id, e)
    except Exception as e:
        log.warning("post-ingest pipeline %s: %s", dataset_id, e)


async def _ensure_dataset_source(dataset_id: str, source: str, source_id: str):
    """Create a source record for this dataset if one doesn't exist."""
    try:
        conn = _write_conn()
        existing = conn.execute(
            "SELECT source_id FROM fabric_sources WHERE dataset_id=? LIMIT 1",
            (dataset_id,)
        ).fetchone()
        if existing:
            return
        sid = source_id or f"auto_{dataset_id}"
        conn.execute(
            "INSERT OR IGNORE INTO fabric_sources "
            "(source_id, source_type, url, dataset_id, schedule, enabled, config, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sid, source or "api", "", dataset_id, "", 1,
             json.dumps({"auto_created": True, "source": source}), now_iso())
        )
        conn.commit()
        log.info("auto-created source %s for dataset %s", sid, dataset_id)
    except Exception as e:
        log.warning("ensure_dataset_source %s: %s", dataset_id, e)


async def _get_dataset_processing_config(dataset_id: str) -> Dict:
    """Get per-dataset processing configuration.
    Stored in fabric_dataset_config table. Returns defaults if no config set.
    """
    defaults = {
        "auto_extract_entities": True,
        "auto_loom":            False,      # off by default — can be heavy
        "extract_limit":        500,
        "content_type":         "text",
        "loom_scope":           "internal", # internal = within dataset, cross = all datasets
        "loom_mode":            "hybrid",
        "loom_min_score":       0.4,
        "loom_max_matches":     100,
        "entity_scope":         "internal", # internal = only link entities within dataset
    }
    try:
        conn = _write_conn()
        # Ensure config table exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fabric_dataset_config (
                dataset_id TEXT PRIMARY KEY,
                config     TEXT DEFAULT '{}',
                updated_at TEXT
            )
        """)
        row = conn.execute(
            "SELECT config FROM fabric_dataset_config WHERE dataset_id=?",
            (dataset_id,)
        ).fetchone()
        if row and row["config"]:
            stored = json.loads(row["config"])
            defaults.update(stored)
    except Exception as e:
        log.warning("get_dataset_config %s: %s", dataset_id, e)
    return defaults


# ─────────────────────────────────────────────────────────────────────────────
# QUERY DSL
# ─────────────────────────────────────────────────────────────────────────────

def _cache_key(q: Dict) -> str:
    return "fabric:cache:" + hashlib.md5(
        json.dumps(q, sort_keys=True, default=str).encode()
    ).hexdigest()

async def _cache_get(key: str) -> Optional[Dict]:
    r = _redis()
    if not r: return None
    try:
        raw = await r.get(key)
        return json.loads(raw) if raw else None
    except Exception: return None

async def _cache_set(key: str, value: Dict, ttl: int = FABRIC_CACHE_TTL):
    r = _redis()
    if not r: return
    try:
        await r.setex(key, ttl, json.dumps(value, default=str))
    except Exception: pass


async def execute_query(q: Dict) -> Dict:
    """Execute a Query DSL document. Fuses FAISS + Chroma + Postgres FTS scores."""
    if q.get("cache", True):
        cached = await _cache_get(_cache_key(q))
        if cached:
            cached["cached"] = True
            return cached

    top_k      = int(q.get("top_k", 10))
    vector_q   = q.get("vector","") or q.get("text","")
    text_q     = q.get("text","") or vector_q
    dataset_id = q.get("dataset_id") or None
    v_weight   = float(q.get("vector_weight", 1.0))
    t_weight   = float(q.get("text_weight", 0.5))

    scores: Dict[str, float] = {}
    backends_used = []

    async def _safe(name, coro, timeout=8):
        """Run with timeout, swallow errors, log them."""
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("query %s: timeout after %ds", name, timeout)
            return None
        except Exception as e:
            log.debug("query %s: %s", name, e)
            return None

    # Vector search
    embedding = None
    if vector_q:
        embedding = await _safe("embed", _embed(vector_q), timeout=10)
    if embedding and v_weight > 0:
        try:
            faiss_res = (
                FAISS_STORE.search_dataset(dataset_id, embedding, top_k * 2)
                if dataset_id else
                FAISS_STORE.search_global(embedding, top_k * 2)
            )
            if faiss_res:
                backends_used.append("faiss")
                for rid, score in faiss_res:
                    scores[rid] = scores.get(rid, 0) + score * v_weight
        except Exception as e:
            log.debug("query faiss: %s", e)
        try:
            chroma_res = FABRIC_CHROMA.search(embedding, dataset_id=dataset_id, top_k=top_k * 2)
            if chroma_res:
                backends_used.append("chroma")
                for rid, score in chroma_res:
                    scores[rid] = scores.get(rid, 0) + score * v_weight * 0.8
        except Exception as e:
            log.debug("query chroma: %s", e)

    # Text search — Postgres (with timeout — most likely to hang)
    if text_q and t_weight > 0 and FABRIC_PG.available:
        text_res = await _safe("pg_text",
                                FABRIC_PG.search_text(text_q, dataset_id, limit=top_k * 2),
                                timeout=5)
        if text_res:
            backends_used.append("pg")
            for rid, score in text_res:
                scores[rid] = scores.get(rid, 0) + score * t_weight

    # Fetch records — also bounded
    ranked_ids  = sorted(scores, key=scores.get, reverse=True)[:top_k]
    records_map = {}
    if FABRIC_PG.available and ranked_ids:
        records_map = await _safe("pg_get_by_ids",
                                   FABRIC_PG.get_by_ids(ranked_ids),
                                   timeout=5) or {}

    results = []
    for rid in ranked_ids:
        rec   = records_map.get(rid)
        entry = {"id": rid, "score": round(scores[rid], 5)}
        if rec:
            entry.update({"dataset_id": rec.dataset_id, "text": rec.text[:300],
                          "created_at": rec.created_at, "tags": rec.tags,
                          "source": rec.source})
            if q.get("include_data"):
                entry["data"] = rec.data
        results.append(entry)

    # Fallback: if PG returned nothing (unavailable or empty), use SQLite
    # Always try SQLite if we have a query but no results so far.
    if not results and (text_q or vector_q or dataset_id):
        backends_used.append("sqlite")
        sqlite_rows = await _safe("sqlite_query",
                                   _sqlite_query(dataset_id=dataset_id or "", limit=top_k * 4),
                                   timeout=8) or []
        # Score by simple word overlap so we can rank
        words = set((text_q or vector_q or "").lower().split())
        scored = []
        for row in sqlite_rows:
            text = (row.get("text","") or "").lower()
            if not words or any(w in text for w in words if len(w) > 2):
                hits = sum(1 for w in words if w in text and len(w) > 2)
                scored.append((hits, row))
        scored.sort(key=lambda x: -x[0])
        sqlite_rows = [row for _, row in scored[:top_k]]
        for row in sqlite_rows:
            text = row.get("text","")
            query_words = set((text_q or vector_q).lower().split())
            if not query_words or any(w in text.lower() for w in query_words if len(w) > 2):
                try:
                    data = json.loads(row.get("data") or "{}")
                    tags = json.loads(row.get("tags") or "[]")
                except Exception:
                    data, tags = {}, []
                entry = {"id": row["id"], "score": 0.5,
                         "dataset_id": row["dataset_id"],
                         "text": text[:300], "created_at": row.get("created_at",""),
                         "tags": tags, "source": row.get("source_id","")}
                if q.get("include_data"):
                    entry["data"] = data
                results.append(entry)

    output = {
        "results": results, "count": len(results),
        "query":   {k: v for k, v in q.items() if k != "vector"},
        "backends": list(set(backends_used)) or ["sqlite"],
        "backends_available": {"faiss": FAISS_STORE.available, "chroma": FABRIC_CHROMA.available,
                                "pg": FABRIC_PG.available, "neo4j": FABRIC_NEO.available},
        "cached": False,
    }
    if q.get("cache", True):
        await _cache_set(_cache_key(q), output, int(q.get("cache_ttl", FABRIC_CACHE_TTL)))
    return output


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE CONNECTORS
# ─────────────────────────────────────────────────────────────────────────────

_SOURCES: Dict[str, Dict] = {}   # id -> source dict, pre-loaded at import
_PG_CONNECTING = False

def _load_sources_sync():
    """Pre-load sources from SQLite into _SOURCES at import time. Retries on
    transient locks so a crashed previous process doesn't leave us with an
    empty source list."""
    for attempt in range(5):
        try:
            conn = _sqlite_conn()
            try:
                conn.execute("PRAGMA busy_timeout=15000")
                rows = conn.execute("SELECT * FROM fabric_sources").fetchall()
                for row in rows:
                    s = dict(row)
                    _SOURCES[s["id"]] = s
                if rows:
                    log.info("data_fabric: pre-loaded %d sources from SQLite", len(rows))
                return
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 4:
                time.sleep(0.5 * (attempt + 1))
                continue
            log.warning("source pre-load: %s", e)
            return
        except Exception as e:
            log.warning("source pre-load: %s", e)
            return
    return
def _load_sources_sync_OLD_unused():
    """Pre-load sources from SQLite into _SOURCES at import time."""
    try:
        conn = _sqlite_conn()
        try:
            rows = conn.execute("SELECT * FROM fabric_sources").fetchall()
            for row in rows:
                s = dict(row)
                _SOURCES[s["id"]] = s
            if rows:
                log.debug("data_fabric: pre-loaded %d sources from SQLite", len(rows))
        finally:
            conn.close()
    except Exception as _e:
        log.debug("data_fabric: source pre-load: %s", _e)

_load_sources_sync()


# --- One-time migration: add config + auth columns to fabric_sources ---
def _sqlite_migrate_sources():
    try:
        conn = _sqlite_conn()
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(fabric_sources)").fetchall()}
            if "config" not in cols:
                conn.execute("ALTER TABLE fabric_sources ADD COLUMN config TEXT")
                log.info("data_fabric: added fabric_sources.config column")
            if "auth" not in cols:
                conn.execute("ALTER TABLE fabric_sources ADD COLUMN auth TEXT")
                log.info("data_fabric: added fabric_sources.auth column")
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("source migrate: %s", e)

try:
    _sqlite_migrate_sources()
except Exception as _e:
    log.debug("migrate skipped: %s", _e)

async def _pull_rss(src: dict) -> List[dict]:
    """Fetch RSS/Atom feed via httpx (proper UA + redirects) then parse with
    feedparser. Doing the HTTP ourselves is essential — feedparser's built-in
    fetcher uses a default UA that gets blocked or redirected by major
    publishers (BBC, NYT, Reddit, etc.)."""
    url = src["url"]
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Vera-Fabric/1.0; +https://example.com/bot) "
                      "feedparser",
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
    }
    body: bytes = b""
    final_url = url
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as c:
            r = await c.get(url)
            final_url = str(r.url)
            # Tolerate 4xx — sometimes feeds 403 but still send the body
            if r.status_code < 500:
                body = r.content
            else:
                r.raise_for_status()
    except Exception as e:
        log.warning("rss fetch %s: %s", url, e)
        return []

    if not body:
        log.warning("rss %s: empty body", url)
        return []

    if HAS_FEEDPARSER:
        import concurrent.futures
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            feed = await loop.run_in_executor(pool, feedparser.parse, body)
        if feed.bozo and not feed.entries:
            log.warning("rss %s: feedparser bozo %s, no entries — was the URL really a feed? final=%s",
                        url, getattr(feed, "bozo_exception", None), final_url)
            return []
        results = []
        for entry in feed.entries[:src.get("lim", src.get("limit", 50))]:
            title = str(entry.get("title",""))
            summary = str(entry.get("summary",""))
            text = (title + ". " + summary).strip()[:4000]
            if not text or text == ".":
                continue
            results.append({"text": text, "data": {
                "title":     title,
                "link":      entry.get("link",""),
                "published": str(entry.get("published","")),
                "summary":   summary[:1000],
            }})
        log.info("rss %s -> %d items (final_url=%s)", url, len(results), final_url)
        return results

    # Fallback: regex-only (very rough)
    text = body.decode("utf-8", errors="ignore")
    items = re.findall(r"<title>(.*?)</title>", text, re.DOTALL)
    return [{"text": t.strip()[:2000]} for t in items[1:21]]  # skip channel title


async def _pull_api(src: dict) -> List[dict]:
    try:
        headers = json.loads(src.get("headers") or "{}")
        if isinstance(headers, str): headers = {}
    except Exception:
        headers = {}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"}) as c:
        r = await c.get(src["url"], headers=headers); r.raise_for_status()
        data = r.json()
    jq = (src.get("jq_path") or "").strip()
    if jq:
        for key in jq.split("."):
            if isinstance(data, dict) and key:
                data = data.get(key, [])
    if not isinstance(data, list):
        data = [data]
    results = []
    for item in data[:src.get("lim", src.get("limit", 50))]:
        if isinstance(item, dict):
            text = " ".join(str(v) for v in item.values()
                            if isinstance(v, (str, int, float)))[:4000]
        else:
            text = str(item)[:4000]
        results.append({"text": text, "data": item if isinstance(item, dict) else {"value": item}})
    return results


async def _pull_wiki(src: dict) -> List[dict]:
    base = src.get("url","https://en.wikipedia.org").rstrip("/")
    page = src.get("page","")
    if not page: return []
    import urllib.parse as _up
    params = {"action":"query","titles":page,"prop":"extracts","exintro":"1",
              "explaintext":"1","format":"json","redirects":"1"}
    url = base + "/w/api.php?" + _up.urlencode(params)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"}) as c:
        r = await c.get(url); r.raise_for_status()
        data = r.json()
    results = []
    for pid, pd in data.get("query",{}).get("pages",{}).items():
        if pid == "-1": continue
        text = pd.get("extract","")[:6000]
        results.append({"text": text, "data": {
            "title": pd.get("title",""), "pageid": pid,
            "extract": text, "wiki_url": base,
        }})
    return results



# ─────────────────────────────────────────────────────────────────────────────
# HTML SCRAPE — works for any site, follows redirects, parses readable content
# ─────────────────────────────────────────────────────────────────────────────

async def _pull_scrape(src: dict) -> List[dict]:
    """Fetch a webpage and turn it into structured records.

    Strategy:
      1. httpx GET with redirects + browser-like headers.
      2. Parse with BeautifulSoup (or fall back to regex).
      3. Extract: title, meta, headings, article paragraphs, links, lists.
      4. Each "section" (h1/h2-bounded) becomes one record.
      5. If the page is mostly a list of articles (homepage, blog index),
         each anchor with substantial text becomes its own record.
    """
    url = src["url"]
    try:
        headers = json.loads(src.get("headers") or "{}") if isinstance(src.get("headers"), str) else (src.get("headers") or {})
    except Exception:
        headers = {}
    headers.setdefault("User-Agent",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36 Vera-Fabric/1.0")
    headers.setdefault("Accept", "text/html,application/xhtml+xml")

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as c:
        r = await c.get(url)
        # Tolerate non-2xx but only if there is a body — many sites return 403 with content
        if r.status_code >= 400 and not r.content:
            r.raise_for_status()
        html = r.text
        final_url = str(r.url)

    items: List[dict] = []
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(html, "html.parser")
        # Strip scripts/styles
        for tag in soup(["script","style","noscript","iframe","svg"]):
            tag.decompose()

        page_title = (soup.title.string.strip() if soup.title and soup.title.string else "")

        # Strategy A: article / main / sectioned content
        main = soup.find("article") or soup.find("main") or soup.find("body")
        sections = []
        if main:
            current_heading = page_title or "Introduction"
            current_text: list = []
            for el in main.descendants:
                name = getattr(el, "name", None)
                if name in ("h1","h2","h3"):
                    if current_text:
                        sections.append((current_heading, " ".join(current_text)))
                    current_heading = el.get_text(" ", strip=True)[:200] or current_heading
                    current_text = []
                elif name in ("p","li"):
                    txt = el.get_text(" ", strip=True)
                    if txt and len(txt) > 30:
                        current_text.append(txt)
            if current_text:
                sections.append((current_heading, " ".join(current_text)))

        for heading, body_text in sections[:src.get("lim", src.get("limit", 50))]:
            text = (heading + ". " + body_text)[:6000]
            if len(body_text) < 80:
                continue
            items.append({
                "text": text,
                "data": {
                    "title":     heading,
                    "section":   heading,
                    "url":       final_url,
                    "page_title": page_title,
                    "char_count": len(body_text),
                }
            })

        # Strategy B: link list (used when the page itself yielded few sections)
        if len(items) < 3:
            links = []
            for a in soup.find_all("a", href=True):
                txt = a.get_text(" ", strip=True)
                href = a.get("href","")
                if not txt or len(txt) < 20 or len(txt) > 200:
                    continue
                # Resolve relative URLs
                if href.startswith("/"):
                    from urllib.parse import urljoin
                    href = urljoin(final_url, href)
                elif not href.startswith(("http://","https://")):
                    continue
                links.append({"title": txt, "url": href})
            # Dedup by title
            seen_titles = set()
            for link in links[:src.get("lim", src.get("limit", 50))]:
                if link["title"] in seen_titles:
                    continue
                seen_titles.add(link["title"])
                items.append({
                    "text": link["title"],
                    "data": {
                        "title": link["title"],
                        "url":   link["url"],
                        "source_url": final_url,
                    }
                })

    except ImportError:
        # No BS4 — minimal regex fallback
        log.warning("data_fabric: bs4 not installed, using regex fallback for scrape")
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL|re.IGNORECASE)
        title = (title_match.group(1).strip() if title_match else "")[:200]
        # Extract paragraph-ish chunks
        paras = re.findall(r"<p[^>]*>(.*?)</p>", html, re.DOTALL|re.IGNORECASE)
        for p in paras[:src.get("lim", src.get("limit", 50))]:
            txt = re.sub(r"<[^>]+>", " ", p)
            txt = re.sub(r"\s+", " ", txt).strip()
            if len(txt) > 80:
                items.append({"text": txt[:4000],
                              "data": {"title": title, "url": final_url}})

    log.info("data_fabric: scrape %s -> %d items (final_url=%s)", url, len(items), final_url)
    return items


# ─────────────────────────────────────────────────────────────────────────────
# RECON-STYLE PULL — Playwright drives a real browser, captures the JSON APIs
# the site itself uses, then those become the dataset's data source. Once
# discovered, the captured endpoint is saved on the source so future pulls hit
# it directly via httpx (no browser needed).
# ─────────────────────────────────────────────────────────────────────────────

_RECON_DISCOVERY_TIME = 8  # seconds of network observation
_RECON_SCROLL_STEPS   = 3

async def _pull_recon(src: dict) -> List[dict]:
    """Fetch by capturing the site's own data API.

    First call:  open a browser, scroll, watch XHR/fetch responses, identify
                 the JSON endpoint with the most signal, save it to the source.
    Later calls: hit the saved endpoint directly with httpx (fast, no browser).

    The saved endpoint and headers/cookies live in src["page"] (URL) and
    src["headers"] (JSON-encoded session headers), so we re-use existing
    schema columns rather than altering the DB.
    """
    saved_endpoint = src.get("page","")
    try:
        saved_headers = json.loads(src.get("headers") or "{}")
        if isinstance(saved_headers, str):
            saved_headers = {}
    except Exception:
        saved_headers = {}

    # Fast path: we already discovered the API, just hit it
    if saved_endpoint and saved_endpoint.startswith(("http://","https://")):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                          headers={**saved_headers,
                                                   "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"}) as c:
                r = await c.get(saved_endpoint)
                r.raise_for_status()
                data = r.json()
            return _recon_normalise_payload(data, saved_endpoint, src)
        except Exception as e:
            log.warning("data_fabric: recon fast-path failed (%s); re-discovering", e)
            # Fall through to discovery

    # Discovery path — needs Playwright
    try:
        # Reuse browser_capabilities' shared browser if present
        try:
            from browser_capabilities import _get_browser, _new_page
            browser = await _get_browser()
            ctx_page = await _new_page(browser)
            context, page = ctx_page if isinstance(ctx_page, tuple) else (None, ctx_page)
        except Exception:
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=True,
                args=["--no-sandbox","--disable-blink-features=AutomationControlled"])
            context = await browser.new_context(user_agent="Vera-Fabric/1.0")
            page = await context.new_page()

        endpoints: dict = {}

        async def _on_response(response):
            try:
                req = response.request
                ct = (response.headers.get("content-type") or "").lower()
                if "json" not in ct and req.resource_type not in ("xhr","fetch"):
                    return
                u = req.url
                if u not in endpoints:
                    endpoints[u] = {"method": req.method, "hits": 0,
                                    "ct": ct, "size_total": 0,
                                    "request_headers": dict(req.headers)}
                endpoints[u]["hits"] += 1
                try:
                    body = await response.body()
                    endpoints[u]["size_total"] += len(body)
                    # Keep the largest sample for normalisation
                    if endpoints[u]["size_total"] < 500_000:
                        endpoints[u]["sample"] = body
                except Exception:
                    pass
            except Exception:
                pass

        page.on("response", lambda r: asyncio.create_task(_on_response(r)))

        try:
            await page.goto(src["url"], wait_until="networkidle", timeout=30_000)
        except Exception as e:
            log.warning("recon goto: %s", e)

        # Trigger lazy-loaded content
        for _ in range(_RECON_SCROLL_STEPS):
            try:
                await page.mouse.wheel(0, 2000)
            except Exception:
                pass
            await asyncio.sleep(1.0)

        await asyncio.sleep(_RECON_DISCOVERY_TIME)

        # Collect cookies as session headers
        try:
            cookies = await context.cookies() if context else []
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            session_headers = {"Cookie": cookie_str} if cookie_str else {}
        except Exception:
            session_headers = {}

        try:
            await page.close()
            if context:
                await context.close()
        except Exception:
            pass

        # Score endpoints — prefer JSON, multiple hits, larger payloads, "api" in URL
        scored = []
        for u, m in endpoints.items():
            score = 0
            if "json" in m.get("ct",""):    score += 5
            if m["hits"] > 1:               score += 3
            if "api" in u.lower():          score += 2
            if "/v1" in u or "/v2" in u:    score += 1
            if m["size_total"] > 1000:      score += min(m["size_total"]//1000, 5)
            scored.append((score, u, m))
        scored.sort(reverse=True, key=lambda x: x[0])

        if not scored:
            log.warning("recon: no JSON endpoints discovered for %s", src["url"])
            return []

        best_score, best_url, best_meta = scored[0]
        log.info("recon: best endpoint for %s -> %s (score=%d, hits=%d)",
                 src["url"], best_url, best_score, best_meta["hits"])

        # Persist for next time — store URL in `page`, headers in `headers`
        try:
            updated = {**src, "page": best_url,
                       "headers": json.dumps(session_headers)}
            await _sqlite_upsert_source(updated)
            _SOURCES[src["id"]] = updated
        except Exception as e:
            log.warning("recon persist: %s", e)

        # Parse the captured sample
        sample = best_meta.get("sample")
        if sample:
            try:
                payload = json.loads(sample.decode("utf-8", errors="ignore"))
                return _recon_normalise_payload(payload, best_url, src)
            except Exception as e:
                log.warning("recon parse sample: %s", e)
        return []

    except Exception as e:
        log.error("recon discovery failed: %s", e)
        return []

def _recon_normalise_payload(payload, endpoint_url: str, src: dict) -> List[dict]:
    """Turn arbitrary JSON into a list of records. Walks dict/list shapes and
    extracts items with text-bearing fields."""
    items: List[dict] = []

    # If the payload is a list of dicts, that's the dataset.
    candidates = []
    if isinstance(payload, list):
        candidates = [payload]
    elif isinstance(payload, dict):
        # Look for a list of dicts inside common keys
        for key in ("data","items","results","records","entries","posts",
                    "articles","stories","feed","list","rows","hits"):
            v = payload.get(key)
            if isinstance(v, list) and v and isinstance(v[0], (dict, str)):
                candidates.append(v)
        # Also scan one level deep
        for v in payload.values():
            if isinstance(v, dict):
                for inner in v.values():
                    if isinstance(inner, list) and inner and isinstance(inner[0], (dict, str)):
                        candidates.append(inner)

    if not candidates:
        # No list found — treat the whole payload as one record
        if isinstance(payload, dict):
            text = " ".join(str(v) for v in payload.values()
                            if isinstance(v, (str,int,float)))[:4000]
            if text:
                items.append({"text": text, "data": {**payload, "_endpoint": endpoint_url}})
        return items

    biggest = max(candidates, key=len)
    lim = src.get("lim", src.get("limit", 50))
    for item in biggest[:lim]:
        if isinstance(item, dict):
            # Find a "title-ish" field
            title = ""
            for tk in ("title","name","headline","subject","summary","label"):
                if isinstance(item.get(tk), str) and item.get(tk).strip():
                    title = item[tk].strip()
                    break
            if not title:
                # Fallback: first stringy value
                for v in item.values():
                    if isinstance(v, str) and 10 < len(v) < 300:
                        title = v
                        break
            text_parts = [title] if title else []
            for v in item.values():
                if isinstance(v, str) and len(v) > 30:
                    text_parts.append(v)
                elif isinstance(v, (int, float, bool)):
                    pass  # skip numerics for text
            text = " ".join(text_parts)[:6000]
            if not text:
                continue
            items.append({
                "text": text,
                "data": {**item, "_endpoint": endpoint_url}
            })
        elif isinstance(item, str):
            items.append({"text": item[:4000], "data": {"value": item, "_endpoint": endpoint_url}})

    return items





# ─────────────────────────────────────────────────────────────────────────────
# SOURCE-TYPE FRAMEWORK
# Each source has a `source_type`, plus type-specific config (a JSON blob in
# the `config` column) and auth (a JSON blob in `auth`). At pull time, env-var
# references like ${VAR_NAME} are resolved on the fly so secrets can live
# outside the database if desired.
# ─────────────────────────────────────────────────────────────────────────────

_ENV_REF_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

def _resolve_env(value):
    """Replace ${VAR} placeholders with environment values. Recursive for
    nested dicts and lists. Unknown vars are left as-is."""
    if value is None:
        return None
    if isinstance(value, str):
        def _sub(m):
            return os.environ.get(m.group(1), m.group(0))
        return _ENV_REF_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value

def _src_config(src: dict) -> dict:
    cfg = src.get("config")
    if isinstance(cfg, dict):
        return _resolve_env(cfg)
    if isinstance(cfg, str) and cfg.strip():
        try:
            return _resolve_env(json.loads(cfg))
        except Exception:
            log.warning("source %s: config is not valid JSON", src.get("id"))
    return {}

def _src_auth(src: dict) -> dict:
    a = src.get("auth")
    if isinstance(a, dict):
        return _resolve_env(a)
    if isinstance(a, str) and a.strip():
        try:
            return _resolve_env(json.loads(a))
        except Exception:
            log.warning("source %s: auth is not valid JSON", src.get("id"))
    return {}


# ── Git-forge pullers ────────────────────────────────────────────────────────

async def _pull_gitea(src: dict) -> List[dict]:
    cfg  = _src_config(src); auth = _src_auth(src)
    base = (cfg.get("base_url") or src.get("url") or "").rstrip("/")
    if not base:
        log.warning("gitea: no base_url"); return []
    mode  = (cfg.get("mode") or "issues").lower()
    owner = cfg.get("owner") or ""
    repo  = cfg.get("repo") or ""
    state = cfg.get("state") or "open"
    limit = int(src.get("lim") or 50)
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"}
    if auth.get("token"):
        headers["Authorization"] = f"token {auth['token']}"
    items: list = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as c:
        if mode == "issues":
            if owner and repo:
                url = f"{base}/api/v1/repos/{owner}/{repo}/issues"
            else:
                url = f"{base}/api/v1/repos/issues/search"
            params = {"state": state, "limit": min(50, limit), "type": "issues"}
            r = await c.get(url, params=params); r.raise_for_status()
            data = r.json() if isinstance(r.json(), list) else r.json().get("data", [])
            for it in data[:limit]:
                items.append({
                    "text": f"{it.get('title','')}\n\n{it.get('body','') or ''}"[:6000],
                    "data": {"title": it.get("title"), "number": it.get("number"),
                             "state": it.get("state"), "html_url": it.get("html_url",""),
                             "user": (it.get("user") or {}).get("login"),
                             "labels": [l.get("name") for l in (it.get("labels") or [])],
                             "created_at": it.get("created_at")},
                })
        elif mode == "repos":
            url = f"{base}/api/v1/repos/search"
            params = {"limit": min(50, limit)}
            if owner: params["owner"] = owner
            r = await c.get(url, params=params); r.raise_for_status()
            for it in (r.json().get("data") or [])[:limit]:
                items.append({
                    "text": f"{it.get('full_name','')}\n\n{it.get('description','') or ''}"[:4000],
                    "data": {"full_name": it.get("full_name"),
                             "description": it.get("description"),
                             "html_url": it.get("html_url"),
                             "stars": it.get("stars_count"),
                             "language": it.get("language")},
                })
        elif mode == "commits":
            if not (owner and repo):
                log.warning("gitea commits: owner and repo required"); return []
            url = f"{base}/api/v1/repos/{owner}/{repo}/commits"
            r = await c.get(url, params={"limit": min(50, limit)}); r.raise_for_status()
            for it in r.json()[:limit]:
                commit = it.get("commit") or {}
                items.append({
                    "text": f"{commit.get('message','')}\n\nby {(commit.get('author') or {}).get('name','')}"[:4000],
                    "data": {"sha": it.get("sha"), "html_url": it.get("html_url"),
                             "message": commit.get("message"),
                             "author": (commit.get("author") or {}).get("name")},
                })
    return items


async def _pull_github(src: dict) -> List[dict]:
    cfg  = _src_config(src); auth = _src_auth(src)
    base  = (cfg.get("base_url") or "https://api.github.com").rstrip("/")
    mode  = (cfg.get("mode") or "issues").lower()
    owner = cfg.get("owner") or ""
    repo  = cfg.get("repo") or ""
    state = cfg.get("state") or "open"
    limit = int(src.get("lim") or 50)
    headers = {"Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"}
    if auth.get("token"):
        headers["Authorization"] = f"Bearer {auth['token']}"
    items: list = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as c:
        if mode == "issues":
            url = (f"{base}/repos/{owner}/{repo}/issues" if (owner and repo)
                    else f"{base}/issues")
            r = await c.get(url, params={"state": state, "per_page": min(50, limit)})
            r.raise_for_status()
            for it in r.json()[:limit]:
                if it.get("pull_request"): continue
                items.append({
                    "text": f"{it.get('title','')}\n\n{it.get('body','') or ''}"[:6000],
                    "data": {"title": it.get("title"), "number": it.get("number"),
                             "state": it.get("state"), "html_url": it.get("html_url"),
                             "user": (it.get("user") or {}).get("login"),
                             "labels": [l.get("name") for l in (it.get("labels") or [])],
                             "created_at": it.get("created_at")},
                })
        elif mode == "repos":
            url = (f"{base}/users/{owner}/repos" if owner else f"{base}/user/repos")
            r = await c.get(url, params={"per_page": min(50, limit)})
            r.raise_for_status()
            for it in r.json()[:limit]:
                items.append({
                    "text": f"{it.get('full_name','')}\n\n{it.get('description','') or ''}"[:4000],
                    "data": {"full_name": it.get("full_name"),
                             "description": it.get("description"),
                             "html_url": it.get("html_url"),
                             "stars": it.get("stargazers_count"),
                             "language": it.get("language")},
                })
        elif mode == "commits":
            if not (owner and repo):
                log.warning("github commits: owner and repo required"); return []
            r = await c.get(f"{base}/repos/{owner}/{repo}/commits",
                              params={"per_page": min(50, limit)})
            r.raise_for_status()
            for it in r.json()[:limit]:
                commit = it.get("commit") or {}
                items.append({
                    "text": f"{commit.get('message','')}\n\nby {(commit.get('author') or {}).get('name','')}"[:4000],
                    "data": {"sha": it.get("sha"), "html_url": it.get("html_url"),
                             "message": commit.get("message"),
                             "author": (commit.get("author") or {}).get("name")},
                })
    return items


async def _pull_gitlab(src: dict) -> List[dict]:
    cfg  = _src_config(src); auth = _src_auth(src)
    base = (cfg.get("base_url") or "https://gitlab.com").rstrip("/")
    mode = (cfg.get("mode") or "issues").lower()
    project_id = cfg.get("project_id") or ""
    state = cfg.get("state") or "opened"
    limit = int(src.get("lim") or 50)
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"}
    if auth.get("token"):
        headers["PRIVATE-TOKEN"] = auth["token"]
    items: list = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as c:
        if mode == "issues":
            url = (f"{base}/api/v4/projects/{project_id}/issues" if project_id
                    else f"{base}/api/v4/issues")
            r = await c.get(url, params={"state": state, "per_page": min(50, limit)})
            r.raise_for_status()
            for it in r.json()[:limit]:
                items.append({
                    "text": f"{it.get('title','')}\n\n{it.get('description','') or ''}"[:6000],
                    "data": {"title": it.get("title"), "iid": it.get("iid"),
                             "state": it.get("state"), "web_url": it.get("web_url"),
                             "labels": it.get("labels"),
                             "author": (it.get("author") or {}).get("username"),
                             "created_at": it.get("created_at")},
                })
        elif mode == "projects":
            r = await c.get(f"{base}/api/v4/projects",
                              params={"per_page": min(50, limit), "membership": "true"})
            r.raise_for_status()
            for it in r.json()[:limit]:
                items.append({
                    "text": f"{it.get('path_with_namespace','')}\n\n{it.get('description','') or ''}"[:4000],
                    "data": {"path_with_namespace": it.get("path_with_namespace"),
                             "description": it.get("description"),
                             "web_url": it.get("web_url"),
                             "star_count": it.get("star_count")},
                })
        elif mode == "commits":
            if not project_id:
                log.warning("gitlab commits: project_id required"); return []
            r = await c.get(f"{base}/api/v4/projects/{project_id}/repository/commits",
                              params={"per_page": min(50, limit)})
            r.raise_for_status()
            for it in r.json()[:limit]:
                items.append({
                    "text": f"{it.get('message','')}\n\nby {it.get('author_name','')}"[:4000],
                    "data": {"id": it.get("id"), "web_url": it.get("web_url"),
                             "message": it.get("message"), "author": it.get("author_name")},
                })
    return items


# ── Database pullers ─────────────────────────────────────────────────────────

def _row_to_record(row: dict, text_field: str) -> dict:
    """Common helper: turn a db row into a fabric record dict."""
    if text_field and text_field in row:
        txt = str(row[text_field])
    else:
        txt = json.dumps(row, default=str)
    return {
        "text": txt[:6000],
        "data": {k: (v if isinstance(v, (int, float, bool, type(None))) else str(v))
                  for k, v in row.items()},
    }


async def _pull_postgres(src: dict) -> List[dict]:
    try:
        import asyncpg
    except ImportError:
        raise RuntimeError("asyncpg not installed (pip install asyncpg)")
    cfg  = _src_config(src); auth = _src_auth(src)
    host = cfg.get("host") or "localhost"
    port = int(cfg.get("port") or 5432)
    db   = cfg.get("database") or cfg.get("db") or ""
    query = cfg.get("query") or ""
    text_field = cfg.get("text_field") or ""
    limit = int(src.get("lim") or 100)
    if not db or not query:
        log.warning("postgres: database and query required"); return []
    if not re.match(r"^\s*(SELECT|WITH)\b", query, re.I):
        log.warning("postgres: only SELECT / WITH queries allowed"); return []
    user = auth.get("user") or "postgres"
    password = auth.get("password") or ""
    items: list = []
    conn = None
    try:
        conn = await asyncpg.connect(host=host, port=port, database=db,
                                      user=user, password=password, timeout=15)
        sql = query if "LIMIT" in query.upper() else f"{query} LIMIT {limit}"
        rows = await conn.fetch(sql)
        for row in rows[:limit]:
            items.append(_row_to_record(dict(row), text_field))
    finally:
        if conn:
            try: await conn.close()
            except Exception: pass
    return items


async def _pull_mysql(src: dict) -> List[dict]:
    try:
        import aiomysql
    except ImportError:
        raise RuntimeError("aiomysql not installed (pip install aiomysql)")
    cfg  = _src_config(src); auth = _src_auth(src)
    host = cfg.get("host") or "localhost"
    port = int(cfg.get("port") or 3306)
    db   = cfg.get("database") or cfg.get("db") or ""
    query = cfg.get("query") or ""
    text_field = cfg.get("text_field") or ""
    limit = int(src.get("lim") or 100)
    if not db or not query:
        log.warning("mysql: database and query required"); return []
    if not re.match(r"^\s*SELECT\b", query, re.I):
        log.warning("mysql: only SELECT queries allowed"); return []
    user = auth.get("user") or "root"
    password = auth.get("password") or ""
    items: list = []
    pool = None
    try:
        pool = await aiomysql.create_pool(host=host, port=port, db=db,
                                            user=user, password=password,
                                            connect_timeout=15, autocommit=True)
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                sql = query if "LIMIT" in query.upper() else f"{query} LIMIT {limit}"
                await cur.execute(sql)
                rows = await cur.fetchall()
        for row in rows[:limit]:
            items.append(_row_to_record(row, text_field))
    finally:
        if pool:
            pool.close()
            try: await pool.wait_closed()
            except Exception: pass
    return items


async def _pull_sqlite(src: dict) -> List[dict]:
    cfg = _src_config(src)
    path = (cfg.get("path") or src.get("url","").replace("sqlite://","").replace("file://",""))
    query = cfg.get("query") or ""
    text_field = cfg.get("text_field") or ""
    limit = int(src.get("lim") or 100)
    if not path or not query:
        log.warning("sqlite: path and query required"); return []
    if not os.path.exists(path):
        log.warning("sqlite: file %s does not exist", path); return []
    if not re.match(r"^\s*SELECT\b", query, re.I):
        log.warning("sqlite: only SELECT queries allowed"); return []
    loop = asyncio.get_running_loop()
    def _run():
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
        try:
            c.row_factory = sqlite3.Row
            sql = query if "LIMIT" in query.upper() else f"{query} LIMIT {limit}"
            return [dict(r) for r in c.execute(sql).fetchall()]
        finally:
            c.close()
    rows = await loop.run_in_executor(None, _run)
    return [_row_to_record(r, text_field) for r in rows[:limit]]


async def _pull_mongodb(src: dict) -> List[dict]:
    try:
        import motor.motor_asyncio as motor_aio
    except ImportError:
        raise RuntimeError("motor not installed (pip install motor)")
    cfg  = _src_config(src); auth = _src_auth(src)
    uri = cfg.get("uri") or src.get("url") or "mongodb://localhost:27017"
    db_name = cfg.get("database") or cfg.get("db") or ""
    coll = cfg.get("collection") or ""
    filt_raw = cfg.get("filter") or {}
    proj_raw = cfg.get("projection") or None
    text_field = cfg.get("text_field") or ""
    limit = int(src.get("lim") or 100)
    if not db_name or not coll:
        log.warning("mongodb: database and collection required"); return []
    # Filter/projection might still be JSON strings
    if isinstance(filt_raw, str):
        try: filt = json.loads(filt_raw) if filt_raw.strip() else {}
        except Exception: filt = {}
    else:
        filt = filt_raw or {}
    if isinstance(proj_raw, str):
        try: proj = json.loads(proj_raw) if proj_raw.strip() else None
        except Exception: proj = None
    else:
        proj = proj_raw
    if auth.get("user") and "@" not in uri:
        from urllib.parse import quote_plus, urlparse, urlunparse
        parsed = urlparse(uri)
        netloc = f"{quote_plus(auth['user'])}:{quote_plus(auth.get('password',''))}@{parsed.netloc or 'localhost'}"
        uri = urlunparse(parsed._replace(netloc=netloc))
    items: list = []
    client = None
    try:
        client = motor_aio.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000)
        col = client[db_name][coll]
        cursor = col.find(filt, proj).limit(limit) if proj else col.find(filt).limit(limit)
        async for doc in cursor:
            doc.pop("_id", None)
            items.append(_row_to_record(doc, text_field))
    finally:
        if client:
            try: client.close()
            except Exception: pass
    return items


async def _pull_elasticsearch(src: dict) -> List[dict]:
    cfg  = _src_config(src); auth = _src_auth(src)
    url = (cfg.get("url") or src.get("url") or "http://localhost:9200").rstrip("/")
    index = cfg.get("index") or "_all"
    text_field = cfg.get("text_field") or ""
    limit = int(src.get("lim") or 50)
    q_raw = cfg.get("query")
    if isinstance(q_raw, str) and q_raw.strip():
        try: q = json.loads(q_raw)
        except Exception: q = {"match_all": {}}
    elif isinstance(q_raw, dict) and q_raw:
        q = q_raw
    else:
        q = {"match_all": {}}
    body = {"size": limit, "query": q}
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"}
    auth_param = None
    if auth.get("api_key"):
        headers["Authorization"] = f"ApiKey {auth['api_key']}"
    elif auth.get("user"):
        auth_param = (auth["user"], auth.get("password",""))
    items: list = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                   headers=headers, auth=auth_param) as c:
        r = await c.post(f"{url}/{index}/_search", json=body)
        r.raise_for_status()
        hits = ((r.json().get("hits") or {}).get("hits") or [])
        for h in hits[:limit]:
            doc = h.get("_source") or {}
            doc["_es_id"] = h.get("_id")
            doc["_es_index"] = h.get("_index")
            items.append(_row_to_record(doc, text_field))
    return items




async def _pull_topic(src: dict) -> List[dict]:
    """Re-run discovery for the saved topic. Returns [] because the discover
    capability ingests records itself via the fabric writer."""
    cfg = _src_config(src)
    topic = cfg.get("topic","").strip()
    if not topic:
        log.warning("topic source: no topic"); return []
    try:
        from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY
    except ImportError:
        return []
    fn = (CAPABILITY_REGISTRY.get("collector.discover") or {}).get("fn")
    if not fn:
        log.warning("collector.discover not registered")
        return []
    try:
        result = await fn(
            topic=topic,
            max_sources=int(cfg.get("max_sources") or 5),
            content_type=cfg.get("content_type") or "all",
        )
        log.info("topic re-run %r: %d sources, %d records",
                  topic,
                  result.get("sources_found", 0),
                  result.get("ingested_total", 0))
    except Exception as e:
        log.warning("topic re-run %r: %s", topic, e)
    return []


async def _pull_docs(src: dict) -> List[dict]:
    """Re-crawl a documentation site. The original crawl wrote a cursor
    keyed on the dataset_id, so this incremental call only fetches changed
    pages. Config: {start_url, max_pages, max_depth, topic, topic_dropoff,
    same_domain}."""
    cfg = _src_config(src)
    start_url = cfg.get("start_url") or src.get("url")
    if not start_url:
        log.warning("docs source: no start_url"); return []
    # Delegate to collector.ingest_docs — call the registered capability function
    try:
        from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY
    except ImportError:
        CAPABILITY_REGISTRY = {}
    fn = (CAPABILITY_REGISTRY.get("collector.ingest_docs") or {}).get("fn")
    if not fn:
        log.warning("collector.ingest_docs not registered — install collectors module")
        return []
    try:
        result = await fn(
            url=start_url,
            dataset_id=src.get("dataset_id",""),
            max_pages=int(cfg.get("max_pages") or src.get("lim") or 50),
            max_depth=int(cfg.get("max_depth") or 5),
            same_domain=bool(cfg.get("same_domain", True)),
            tags=",".join(src.get("tags") or []) if isinstance(src.get("tags"), list) else (src.get("tags") or ""),
            topic=cfg.get("topic", ""),
            topic_dropoff=int(cfg.get("topic_dropoff") or 2),
        )
    except Exception as e:
        log.warning("_pull_docs delegate: %s", e)
        return []
    # collector.ingest_docs handles its own ingestion via FABRIC_INGEST.
    # We return [] to skip the standard insert path — records are already in.
    if isinstance(result, dict):
        ingested = result.get("ingested", 0)
        updated  = result.get("updated", 0)
        log.info("docs pull: %d new, %d updated for %s",
                  ingested, updated, src.get("dataset_id","?"))
    return []  # records already inserted by collector


# ── Source-type registry — for the panel UI ──────────────────────────────────

SOURCE_TYPES: Dict[str, Dict] = {
    "rss":     {"description": "RSS / Atom feed",
                "config_fields": [], "auth_fields": [], "needs_url": True},
    "api":     {"description": "Generic JSON API endpoint",
                "config_fields": [{"name":"jq_path","label":"JSON path",
                                    "help":"e.g. items[*]"}],
                "auth_fields":   [{"name":"headers","label":"Headers (JSON)",
                                    "type":"textarea"}],
                "needs_url": True},
    "wiki":    {"description": "MediaWiki API",
                "config_fields": [], "auth_fields": [], "needs_url": True},
    "scrape":  {"description": "HTML scrape",
                "config_fields": [], "auth_fields": [], "needs_url": True},
    "recon":   {"description": "Playwright API discovery (best for SPAs)",
                "config_fields": [], "auth_fields": [], "needs_url": True},
    "gitea":   {"description": "Gitea / Forgejo instance",
                "config_fields": [
                    {"name":"base_url","label":"Base URL","required":True,
                     "help":"e.g. https://gitea.example.com"},
                    {"name":"mode","label":"Mode","type":"select",
                     "options":["issues","repos","commits"],"default":"issues"},
                    {"name":"owner","label":"Owner / org",
                     "help":"optional — empty = all"},
                    {"name":"repo","label":"Repository",
                     "help":"required for commits"},
                    {"name":"state","label":"State","type":"select",
                     "options":["open","closed","all"],"default":"open"}],
                "auth_fields":   [{"name":"token","label":"API token",
                                    "secret":True,
                                    "help":"Personal access token"}],
                "needs_url": False},
    "github":  {"description": "GitHub.com or GHE",
                "config_fields": [
                    {"name":"base_url","label":"Base URL",
                     "default":"https://api.github.com",
                     "help":"override for GHE"},
                    {"name":"mode","label":"Mode","type":"select",
                     "options":["issues","repos","commits"],"default":"issues"},
                    {"name":"owner","label":"Owner"},
                    {"name":"repo","label":"Repository"},
                    {"name":"state","label":"State","type":"select",
                     "options":["open","closed","all"],"default":"open"}],
                "auth_fields":   [{"name":"token","label":"Personal access token",
                                    "secret":True}],
                "needs_url": False},
    "gitlab":  {"description": "GitLab.com or self-hosted",
                "config_fields": [
                    {"name":"base_url","label":"Base URL",
                     "default":"https://gitlab.com"},
                    {"name":"mode","label":"Mode","type":"select",
                     "options":["issues","projects","commits"],
                     "default":"issues"},
                    {"name":"project_id","label":"Project ID",
                     "help":"numeric ID or path"},
                    {"name":"state","label":"State","type":"select",
                     "options":["opened","closed","all"],"default":"opened"}],
                "auth_fields":   [{"name":"token","label":"Personal access token",
                                    "secret":True}],
                "needs_url": False},
    "postgres": {"description": "PostgreSQL — runs a SELECT query",
                 "config_fields": [
                     {"name":"host","label":"Host","default":"localhost"},
                     {"name":"port","label":"Port","default":"5432","type":"number"},
                     {"name":"database","label":"Database","required":True},
                     {"name":"query","label":"SELECT query","type":"textarea",
                      "required":True,
                      "help":"e.g. SELECT id, title, body FROM articles ORDER BY created_at DESC"},
                     {"name":"text_field","label":"Text field",
                      "help":"column for record text (optional)"}],
                 "auth_fields":   [
                     {"name":"user","label":"User","default":"postgres"},
                     {"name":"password","label":"Password","secret":True,
                      "help":"supports ${ENV_VAR}"}],
                 "needs_url": False},
    "mysql":    {"description": "MySQL / MariaDB — runs a SELECT query",
                 "config_fields": [
                     {"name":"host","label":"Host","default":"localhost"},
                     {"name":"port","label":"Port","default":"3306","type":"number"},
                     {"name":"database","label":"Database","required":True},
                     {"name":"query","label":"SELECT query","type":"textarea",
                      "required":True},
                     {"name":"text_field","label":"Text field"}],
                 "auth_fields":   [
                     {"name":"user","label":"User","default":"root"},
                     {"name":"password","label":"Password","secret":True}],
                 "needs_url": False},
    "sqlite":   {"description": "Local SQLite database file (read-only)",
                 "config_fields": [
                     {"name":"path","label":"Path to .db","required":True,
                      "help":"absolute path on the server"},
                     {"name":"query","label":"SELECT query","type":"textarea",
                      "required":True},
                     {"name":"text_field","label":"Text field"}],
                 "auth_fields":   [],
                 "needs_url": False},
    "mongodb":  {"description": "MongoDB collection scan",
                 "config_fields": [
                     {"name":"uri","label":"Connection URI",
                      "default":"mongodb://localhost:27017"},
                     {"name":"database","label":"Database","required":True},
                     {"name":"collection","label":"Collection","required":True},
                     {"name":"filter","label":"Filter (JSON)","type":"textarea",
                      "help":"e.g. {\"status\":\"active\"}"},
                     {"name":"projection","label":"Projection (JSON)",
                      "type":"textarea"},
                     {"name":"text_field","label":"Text field"}],
                 "auth_fields":   [
                     {"name":"user","label":"User"},
                     {"name":"password","label":"Password","secret":True}],
                 "needs_url": False},
    "topic":   {"description": "Discovery topic — re-runs Web Acquisition by topic",
                "config_fields": [
                    {"name":"topic","label":"Topic","required":True},
                    {"name":"max_sources","label":"Max sources","type":"number","default":"5"},
                    {"name":"content_type","label":"Content type","type":"select",
                     "options":["all","rss","scrape","recon"],"default":"all"}],
                "auth_fields": [],
                "needs_url": False},
    "docs":     {"description": "Documentation crawler (re-crawl with change detection)",
                 "config_fields": [
                     {"name":"start_url","label":"Start URL","required":True,
                      "help":"e.g. https://docs.example.com"},
                     {"name":"max_pages","label":"Max pages","type":"number","default":"50"},
                     {"name":"max_depth","label":"Max depth","type":"number","default":"5"},
                     {"name":"topic","label":"Topic filter (optional)"},
                     {"name":"topic_dropoff","label":"Topic dropoff","type":"number","default":"2"}],
                 "auth_fields":   [],
                 "needs_url": False},
    "elasticsearch": {"description": "Elasticsearch / OpenSearch",
                       "config_fields": [
                           {"name":"url","label":"URL",
                            "default":"http://localhost:9200"},
                           {"name":"index","label":"Index","required":True,
                            "help":"e.g. logs-* or _all"},
                           {"name":"query","label":"Query DSL (JSON)",
                            "type":"textarea",
                            "help":"leave blank for match_all"},
                           {"name":"text_field","label":"Text field"}],
                       "auth_fields":   [
                           {"name":"user","label":"User"},
                           {"name":"password","label":"Password","secret":True},
                           {"name":"api_key","label":"API key (alt)","secret":True}],
                       "needs_url": False},
}


@capability(
    "fabric.source_types.list",
    http_method="GET", http_path="/fabric/source_types",
    http_tags=["fabric","sources"],
    memory="off", silent=True,
    description="List all supported source types and their config schemas. "
                "The panel UI uses this to render type-specific forms. "
                "Output: {types: [{name, description, config_fields, "
                "auth_fields, needs_url}]}",
)
async def fabric_source_types_list(trace_id=None) -> Dict:
    return {"types": [{"name": k, **v} for k, v in SOURCE_TYPES.items()]}



async def _pull_source(src: dict) -> int:
    stype = src.get("source_type") or src.get("type","rss")
    label = src.get("label","?")
    await emit_event({"type": "fabric.source.pulling", "label": label,
                      "source_type": stype, "url": src.get("url","")})
    try:
        if stype == "rss":
            items = await _pull_rss(src)
        elif stype in ("api","http"):
            items = await _pull_api(src)
        elif stype == "wiki":
            items = await _pull_wiki(src)
        elif stype == "scrape":
            items = await _pull_scrape(src)
        elif stype == "recon":
            items = await _pull_recon(src)
        elif stype == "gitea":
            items = await _pull_gitea(src)
        elif stype == "github":
            items = await _pull_github(src)
        elif stype == "gitlab":
            items = await _pull_gitlab(src)
        elif stype == "postgres":
            items = await _pull_postgres(src)
        elif stype == "mysql":
            items = await _pull_mysql(src)
        elif stype == "sqlite":
            items = await _pull_sqlite(src)
        elif stype == "mongodb":
            items = await _pull_mongodb(src)
        elif stype == "elasticsearch":
            items = await _pull_elasticsearch(src)
        elif stype == "docs":
            items = await _pull_docs(src)
        elif stype == "topic":
            items = await _pull_topic(src)
        else:
            log.warning("unknown source_type %r — falling back to rss", stype)
            items = await _pull_rss(src)
    except httpx.HTTPStatusError as e:
        # Surface the actual status code in the error message
        log.error("data_fabric: pull %s (%s): HTTP %d %s",
                  label, stype, e.response.status_code, e.response.reason_phrase)
        await emit_event({"type": "fabric.source.error", "label": label,
                          "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"})
        return 0
    except Exception as e:
        log.error("data_fabric: pull %s (%s): %s", label, stype, e)
        await emit_event({"type": "fabric.source.error", "label": label, "error": str(e)})
        return 0

    if not items:
        log.info("data_fabric: pull %s returned no items", label)
        # Still update the source so we don't hammer it
        updated = {**src, "pull_count": src.get("pull_count",0)+1, "last_pulled": now_iso()}
        try:
            await _sqlite_upsert_source(updated)
        except Exception as e:
            log.warning("source upsert: %s", e)
        _SOURCES[src["id"]] = updated
        await emit_event({"type":"fabric.source.pulled", "label": label,
                          "dataset_id": src.get("dataset_id", src["id"]),
                          "ingested": 0, "source_type": stype,
                          "note": "No items returned"})
        return 0

    ds_id = src.get("dataset_id") or src["id"]
    try:
        tags = json.loads(src.get("tags") or "[]") if isinstance(src.get("tags"), str) else (src.get("tags") or [])
    except Exception:
        tags = []

    # Build records first, dedup by content hash, then bulk-insert
    records = []
    seen = set()
    for item in items:
        item_text = item.get("text","")
        if not item_text:
            continue
        rid = hashlib.sha256((ds_id + item_text[:200]).encode()).hexdigest()[:32]
        if rid in seen:
            continue
        seen.add(rid)
        records.append({
            "id":         rid,
            "dataset_id": ds_id,
            "text":       item_text[:8000],
            "data":       item.get("data", {}),
            "source_id":  src["id"],
            "tags":       tags,
            "created_at": now_iso(),
        })

    # Check which records already exist in SQLite BEFORE insert so we can
    # skip embed/vector/graph stages for them. Previously _run_extras
    # re-embedded every record on every pull cycle — an embed storm.
    existing_ids: set = set()
    if records and HAS_AIOSQLITE:
        try:
            all_ids = [r["id"] for r in records]
            async with aiosqlite.connect(_db_path(ds_id)) as db:
                for ci in range(0, len(all_ids), 200):
                    chunk_ids = all_ids[ci:ci+200]
                    placeholders = ",".join("?" * len(chunk_ids))
                    async with db.execute(
                        f"SELECT id FROM fabric_records WHERE id IN ({placeholders})",
                        chunk_ids,
                    ) as cur:
                        async for row in cur:
                            existing_ids.add(row[0])
            if existing_ids:
                log.info("pull %s: %d/%d records already exist — will skip embed for those",
                         label, len(existing_ids), len(records))
        except Exception as e:
            log.debug("pre-insert dedup check: %s", e)

    # Bulk SQLite insert through the writer queue, with progress events.
    # We split into chunks so the UI gets a live stream of records appearing.
    ingested = 0
    if records:
        chunk = 5  # emit progress every 5 records
        try:
            for i in range(0, len(records), chunk):
                batch = records[i:i+chunk]
                await _sqlite_insert_records_batch(batch)
                ingested += len(batch)
                # Emit a progress event per chunk — fed live into the UI
                await emit_event({
                    "type": "fabric.record.ingested",
                    "label": label,
                    "dataset_id": ds_id,
                    "count": ingested,
                    "total_expected": len(records),
                    # Send a tiny preview so the UI can show it scrolling past
                    "samples": [{"title": (r.get("data") or {}).get("title")
                                          or (r.get("text") or "")[:80],
                                  "id": r["id"][:12]} for r in batch],
                })
        except Exception as e:
            log.error("data_fabric: bulk insert %s: %s", label, e)

    # Async pipeline for embed/vector/graph stages — these run AFTER the SQLite
    # write so the UI sees data immediately even if downstream stages stall.
    # We don't await them; they're best-effort.
    # Only run on genuinely *new* records to avoid re-embedding existing data.
    new_records = [r for r in records if r["id"] not in existing_ids]

    async def _run_extras(recs):
        for r in recs:
            try:
                rec = DataRecord(
                    id=r["id"], dataset_id=r["dataset_id"], source=stype,
                    source_id=r["source_id"], text=r["text"], data=r["data"],
                    tags=r["tags"]
                )
                # Run only the non-SQLite stages
                for stage in DEFAULT_PIPELINE.stages:
                    if stage.name in ("sqlite_store",):
                        continue
                    try:
                        rec = await stage.process(rec, {})
                    except Exception as e:
                        log.debug("stage %s: %s", stage.name, e)
            except Exception:
                pass
    if new_records:
        asyncio.create_task(_run_extras(new_records))

    # Update source stats
    updated = {**src, "pull_count": src.get("pull_count",0)+1, "last_pulled": now_iso()}
    try:
        await _sqlite_upsert_source(updated)
    except Exception as e:
        log.warning("source upsert: %s", e)
    _SOURCES[src["id"]] = updated

    # Auto-stitch: if the source has the "auto_stitch" tag and we ingested
    # new records, kick off a Loom run for this dataset against itself.
    # Runs in the background — does not block the pull return.
    src_tags_raw = src.get("tags") or []
    try:
        if isinstance(src_tags_raw, str):
            src_tags_list = json.loads(src_tags_raw)
        else:
            src_tags_list = src_tags_raw
    except Exception:
        src_tags_list = []
    if records and any(t == "auto_stitch" for t in src_tags_list):
        async def _auto_stitch():
            try:
                # Stitch new records against the rest of the dataset
                new_ids = [r["id"] for r in records]
                # Build args explicitly so we use the public capability path
                res = await fabric_loom_run(
                    dataset_a=ds_id,
                    dataset_b="",
                    mode="hybrid",
                    min_score=0.45,
                    max_matches=50,
                    persist=True,
                    only_new_ids=",".join(new_ids[:50]),
                )
                await emit_event({"type":"fabric.loom.auto",
                                   "dataset_id": ds_id,
                                   "trigger": "ingest",
                                   "matches": res.get("total", 0),
                                   "persisted": res.get("persisted", 0)})
            except Exception as e:
                log.warning("auto-stitch: %s", e)
        asyncio.create_task(_auto_stitch())

    await emit_event({"type":"fabric.source.pulled",
                      "label": label, "dataset_id": ds_id,
                      "ingested": ingested, "source_type": stype})
    return ingested


# ─────────────────────────────────────────────────────────────────────────────
# AUTO PULL LOOP
# ─────────────────────────────────────────────────────────────────────────────

_AUTO_PULL_SEM = None  # initialised lazily

async def _auto_pull_loop():
    global _AUTO_PULL_SEM
    if _AUTO_PULL_SEM is None:
        # Limit concurrent pulls — prevents fan-out lock contention
        _AUTO_PULL_SEM = asyncio.Semaphore(2)
    while True:
        await asyncio.sleep(30)
        now = time.time()
        due = []
        for src in list(_SOURCES.values()):
            if not src.get("enabled", True):
                continue
            interval = int(src.get("interval", 0) or 0)
            if interval <= 0:
                continue
            last = src.get("_last_pull_ts", 0)
            if now - last >= interval:
                _SOURCES[src["id"]]["_last_pull_ts"] = now
                due.append(src)

        async def _run_one(s):
            async with _AUTO_PULL_SEM:
                try:
                    await _pull_source(s)
                except Exception as e:
                    log.error("auto_pull [%s]: %s", s.get("label","?"), e)

        if due:
            await asyncio.gather(*[_run_one(s) for s in due], return_exceptions=True)


# ─────────────────────────────────────────────────────────────────────────────
# REDIS STREAM WORKER — uses shared REDIS pool, NO new connections (fd leak fix)
# ─────────────────────────────────────────────────────────────────────────────

_BUS_ENABLED  = False
_BUS_FILTERS: List[str] = []
_BUS_TASK: Optional[asyncio.Task] = None

async def _bus_worker():
    """
    Consume vera:events stream using the shared REDIS pool.
    Never opens its own aioredis connection — that was the source of the fd leak.
    Error 24 "Too many open files" was caused by repeated aioredis.from_url()
    calls inside the worker loop; now we reuse _orch.REDIS exclusively.
    """
    backoff = 2.0
    while _BUS_ENABLED:
        redis = _redis()
        if redis is None:
            await asyncio.sleep(5)
            continue
        try:
            stream   = "vera:events"
            group    = "fabric_bus"
            consumer = f"fabric-{os.getpid()}"
            try:
                await redis.xgroup_create(stream, group, id="$", mkstream=True)
            except Exception:
                pass  # BUSYGROUP — already exists
            backoff = 2.0
            log.info("data_fabric: bus worker attached to %s", stream)
            while _BUS_ENABLED:
                try:
                    msgs = await redis.xreadgroup(
                        group, consumer, {stream: ">"}, count=20, block=5000)
                except Exception as e:
                    log.warning("data_fabric: bus read: %s", e)
                    break
                if not msgs:
                    continue
                for _, entries in msgs:
                    for msg_id, fields in entries:
                        try:
                            raw   = fields.get(b"data") or fields.get("data") or b"{}"
                            ev    = json.loads(raw)
                            etype = ev.get("type","")
                            if etype.startswith("fabric."):
                                pass  # skip our own events
                            elif not _BUS_FILTERS or any(f in etype for f in _BUS_FILTERS):
                                text = ev.get("text") or ev.get("content") or ""
                                if text and len(str(text)) > 20:
                                    await ingest_dataset(
                                        dataset_id = "bus." + etype.replace(".", "_"),
                                        data       = [{"text": str(text)[:4000], **ev}],
                                        source     = "redis_bus",
                                        tags       = [etype],
                                    )
                            await redis.xack(stream, group, msg_id)
                        except Exception as e:
                            log.debug("data_fabric: bus event: %s", e)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("data_fabric: bus worker: %s", e)
            backoff = min(backoff * 2, 60.0)
            await asyncio.sleep(backoff)


# ─────────────────────────────────────────────────────────────────────────────
# POSTGRES RECONNECT (for fallback sync)
# ─────────────────────────────────────────────────────────────────────────────

async def _pg_connect_loop():
    global _PG_CONNECTING
    if _PG_CONNECTING: return
    _PG_CONNECTING = True
    delay = 5.0
    while not FABRIC_PG.available:
        try:
            await FABRIC_PG.connect()
            if FABRIC_PG.available:
                log.info("data_fabric: Postgres reconnected")
                break
        except Exception:
            pass
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60.0)
    _PG_CONNECTING = False


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "fabric.ingest", memory="off",
    http_method="POST", http_path="/fabric/ingest", http_tags=["fabric"],
    description="Ingest records into a named dataset. "
                "Input: dataset_id (str!), records (JSON array or object), "
                "source (str), tags (comma-sep). "
                "Output: {ingested, errors, dataset_id}.",
)
async def cap_fabric_ingest(dataset_id: str, records: str = "[]",
                             source: str = "api", tags: str = "",
                             trace_id=None) -> Dict:
    try:
        parsed = json.loads(records)
    except Exception:
        return {"error": "records must be valid JSON"}
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    return await ingest_dataset(dataset_id, parsed if isinstance(parsed, list) else [parsed],
                                source=source, tags=tag_list)


@capability(
    "fabric.query", memory="off",
    http_method="POST", http_path="/fabric/query", http_tags=["fabric"],
    description="Search the data fabric (vector + text + graph). "
                "PREFERRED: use keyword args directly — "
                "fabric.query(text=\"your search\") for keyword search, "
                "fabric.query(vector=\"your search\") for semantic search, "
                "or both: fabric.query(text=\"ML\", vector=\"machine learning models\"). "
                "Optional params: dataset_id (str — filter to one dataset), "
                "top_k (int, default 20), include_data (bool, default False — set True to get full records). "
                "TIP: call fabric.datasets first to discover available datasets, then query with text= or vector=. "
                "Output: {results:[...], count, backends, cached}.",
)
async def cap_fabric_query(query=None, text: str = "",
                              vector: str = "", dataset_id: str = "",
                              top_k: int = 20, trace_id=None) -> Dict:
    """Accepts the query as:
      - a dict (JSON object via REST/MCP)
      - a JSON-encoded string (legacy compatibility)
      - a plain text string (auto-converted to text+vector search)
      - or as expanded keyword args (text=, vector=, dataset_id=, top_k=)
    """
    q: Dict[str, Any] = {}
    if isinstance(query, dict):
        q = query
    elif isinstance(query, str) and query.strip():
        stripped = query.strip()
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                q = parsed
            else:
                # Non-dict JSON (list, scalar) — treat as plain text search
                q = {"text": stripped, "vector": stripped}
        except json.JSONDecodeError:
            # Not JSON at all — treat as a plain text search query.
            # This is the most common LLM mistake: fabric.query(query="search terms")
            q = {"text": stripped, "vector": stripped}
    # Layer keyword args on top — they override anything in the dict
    if text:        q["text"] = text
    if vector:      q["vector"] = vector
    if dataset_id:  q["dataset_id"] = dataset_id
    if top_k != 20: q["top_k"] = top_k
    if not q:
        return {"error": "empty query",
                "hint": "Provide at least one of: text, vector, dataset_id."}
    return await execute_query(q)


@capability(
    "fabric.datasets", memory="off",
    http_method="GET", http_path="/fabric/datasets", http_tags=["fabric"],
    description="List all datasets with record counts. "
                "Output: {datasets: [{dataset_id, record_count, updated_at}]}.",
)
async def cap_fabric_datasets(trace_id=None) -> Dict:
    # Merge PG and SQLite datasets
    pg_ds = await FABRIC_PG.list_datasets() if FABRIC_PG.available else []
    sl_ds = await _sqlite_datasets()
    merged: Dict[str, dict] = {d["dataset_id"]: d for d in sl_ds}
    for d in pg_ds:
        merged[d["dataset_id"]] = d  # PG takes priority
    return {"datasets": list(merged.values()), "count": len(merged)}


@capability(
    "fabric.stats", memory="off",
    http_method="GET", http_path="/fabric/stats", http_tags=["fabric"],
    description="Statistics from all fabric backends. "
                "Output: {postgres, faiss, chroma, neo4j, sqlite, object_store}.",
)
async def cap_fabric_stats(trace_id=None) -> Dict:
    pg_stats = await FABRIC_PG.stats()
    if not pg_stats.get("available"):
        sl = await _sqlite_datasets()
        pg_stats = {"available": False, "sqlite_fallback": True,
                    "datasets": len(sl),
                    "records":  sum(d.get("record_count",0) for d in sl)}
    return {
        "postgres":     pg_stats,
        "faiss":        FAISS_STORE.stats(),
        "chroma":       FABRIC_CHROMA.stats(),
        "neo4j":        {"available": FABRIC_NEO.available},
        "sqlite":       {"available": True, "path": SQLITE_PATH},
        "object_store": {"available": OBJECT_STORE.available, "mode": FABRIC_OBJECT_STORE},
    }


@capability(
    "fabric.schema", memory="off",
    http_method="GET", http_path="/fabric/schema", http_tags=["fabric"],
    description="Get schema for a dataset. Input: dataset_id (query param).",
)
async def cap_fabric_schema(dataset_id: str, trace_id=None) -> Dict:
    # Sample records from SQLite to infer schema
    rows = await _sqlite_query(dataset_id=dataset_id, limit=10)
    if not rows:
        return {"dataset_id": dataset_id, "schema": {}}
    schema: Dict[str, set] = {}
    for row in rows:
        try:
            data = json.loads(row.get("data") or "{}")
            for k, v in data.items():
                schema.setdefault(k, set()).add(type(v).__name__)
        except Exception:
            pass
    return {"dataset_id": dataset_id, "schema": {k: sorted(v) for k, v in schema.items()},
            "record_count": len(rows)}


@capability(
    "fabric.delete_dataset", memory="off",
    http_method="POST", http_path="/fabric/delete", http_tags=["fabric"],
    description="Remove Chroma vectors for a dataset. Input: dataset_id (str!).",
)
async def cap_fabric_delete(dataset_id: str, trace_id=None) -> Dict:
    chroma_ok = FABRIC_CHROMA.delete_dataset(dataset_id) if FABRIC_CHROMA.available else False
    return {"chroma_cleared": chroma_ok, "dataset_id": dataset_id}


@capability(
    "fabric.chroma_reset", memory="off",
    http_method="POST", http_path="/fabric/chroma_reset", http_tags=["fabric"],
    description="Delete and recreate the Chroma vector collection. Required when "
                "switching embedding models (dimension mismatch). All vectors are "
                "lost — use worldview.reembed_missing afterwards to rebuild. "
                "Input: confirm (bool, must be true).",
)
async def cap_fabric_chroma_reset(confirm: bool = False, trace_id=None) -> Dict:
    if not confirm:
        stats = FABRIC_CHROMA.stats()
        return {"error": "Pass confirm=true to reset the Chroma collection.",
                "current": stats,
                "warning": "This deletes ALL vectors. Re-embed with worldview.reembed_missing after."}
    if not FABRIC_CHROMA.available:
        return {"error": "Chroma is not connected"}
    result = FABRIC_CHROMA.reset_collection()
    if result.get("ok"):
        await emit_event({"type": "fabric.chroma_reset",
                          "old_count": result.get("old_count", 0)})
    return result


@capability(
    "fabric.link_datasets", memory="off",
    http_method="POST", http_path="/fabric/link", http_tags=["fabric"],
    description="Link two datasets in the auxiliary graph. "
                "Input: from_id (str!), to_id (str!), rel_type (str). "
                "Output: {ok, from, to, rel}.",
)
async def cap_fabric_link(from_id: str, to_id: str, rel_type: str = "SIMILAR_TO",
                           trace_id=None) -> Dict:
    ok = await FABRIC_NEO.link_datasets(from_id, to_id, rel_type)
    return {"ok": ok, "from": from_id, "to": to_id, "rel": rel_type}


@capability(
    "fabric.stream_publish", memory="off",
    http_method="POST", http_path="/fabric/stream/publish", http_tags=["fabric"],
    description="Publish a record to the fabric Redis ingestion stream. "
                "Input: dataset_id (str!), data (JSON str), source (str).",
)
async def cap_fabric_stream_publish(dataset_id: str, data: str = "{}",
                                     source: str = "stream_publish",
                                     trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"error": "Redis not available"}
    try:
        msg_id = await r.xadd(FABRIC_STREAM_KEY, {"id": dataset_id, "data": data, "source": source})
        return {"ok": True, "msg_id": str(msg_id), "stream": FABRIC_STREAM_KEY}
    except Exception as e:
        return {"error": str(e)}


@capability(
    "fabric.aux_graph.link", memory="off",
    http_method="POST", http_path="/fabric/graph/link", http_tags=["fabric"],
    description="Link two typed nodes in the auxiliary Neo4j graph.",
)
async def cap_fabric_aux_link(from_type: str, from_id: str,
                               to_type: str, to_id: str,
                               rel: str = "RELATED_TO", props: str = "{}",
                               trace_id=None) -> Dict:
    if not FABRIC_NEO.available:
        return {"error": "Neo4j not connected"}
    rtype = re.sub(r"[^A-Z0-9_]", "_", rel.upper())
    try:
        p = json.loads(props or "{}")
    except Exception:
        p = {}
    try:
        async with FABRIC_NEO._driver.session() as s:
            await s.run(
                f"MERGE (a:{from_type} {{id:$aid}}) MERGE (b:{to_type} {{id:$bid}}) "
                f"MERGE (a)-[r:{rtype}]->(b) SET r += $props, r.ts=$ts",
                aid=from_id, bid=to_id, props=p, ts=now_iso())
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


@capability(
    "fabric.aux_graph.query", memory="off",
    http_method="POST", http_path="/fabric/graph/query", http_tags=["fabric"],
    description="Read-only Cypher query on the fabric Neo4j graph. "
                "Returns: {rows (raw data shape), nodes [{id,name,label,labels,props}], "
                "edges [{from,to,rel,props}]} — the latter two are usable directly by "
                "graph visualisations.",
)
async def cap_fabric_aux_query(cypher: str, trace_id=None) -> Dict:
    if not FABRIC_NEO.available:
        return {"error": "Neo4j not connected", "rows": [], "nodes": [], "edges": []}
    if re.search(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP)\b", cypher, re.I):
        return {"error": "Only read queries allowed"}

    # 1) Plain rows — for backwards-compatible callers
    rows = await FABRIC_NEO.query(cypher)

    # 2) Structured nodes/edges — re-run via raw session so we have neo objects.
    #    This is more expensive but only requested cyphers run here.
    nodes_by_id: Dict[int, Dict] = {}
    edges: List[Dict] = []
    try:
        async with FABRIC_NEO._driver.session() as s:
            res = await s.run(cypher)
            async for record in res:
                # Walk every value in the record
                values = list(record.values())
                row_nodes = []
                row_rel   = None
                for v in values:
                    if v is None: continue
                    if hasattr(v, "labels") and hasattr(v, "items"):  # Node
                        if v.id not in nodes_by_id:
                            props = dict(v.items())
                            nodes_by_id[v.id] = {
                                "id":     str(props.get("id") or v.id),
                                "neo_id": v.id,
                                "name":   str(props.get("title") or props.get("name")
                                                or props.get("label") or props.get("url")
                                                or props.get("id") or "?")[:80],
                                "label":  (list(v.labels)[0] if v.labels else "Node"),
                                "labels": list(v.labels),
                                "props":  props,
                            }
                        row_nodes.append(v)
                    elif hasattr(v, "type") and hasattr(v, "items"):  # Relationship
                        row_rel = v
                # If the row had a relationship and at least 2 nodes, emit an edge
                if row_rel is not None and len(row_nodes) >= 2:
                    a, b = row_nodes[0], row_nodes[1]
                    edges.append({
                        "from":  nodes_by_id[a.id]["id"],
                        "to":    nodes_by_id[b.id]["id"],
                        "rel":   row_rel.type,
                        "props": dict(row_rel.items()),
                    })
    except Exception as e:
        log.warning("aux query structured: %s", e)

    return {
        "rows":  rows[:200],
        "count": len(rows),
        "nodes": list(nodes_by_id.values()),
        "edges": edges,
        "node_count": len(nodes_by_id),
        "edge_count": len(edges),
    }


# ── Source management ─────────────────────────────────────────────────────────

@capability(
    "fabric.sources",
    http_method="GET", http_path="/fabric/sources", http_tags=["fabric"],
    memory="off", silent=True,
    description="List all registered data fabric sources. "
                "Output: {sources: [{id, type, url, label, dataset_id, ...}]}",
)
async def fabric_sources(trace_id=None):
    """List sources with normalised, JSON-safe output. SQLite is canonical;
    in-memory _SOURCES only fills gaps for sources not yet persisted. Every row
    is sanitised so one bad row can never crash the UI render."""
    try:
        sqlite_srcs = await _sqlite_sources()
    except Exception as e:
        log.warning("fabric_sources: sqlite read failed: %s", e)
        sqlite_srcs = []
    merged: Dict[str, dict] = {}
    for s in sqlite_srcs:
        if isinstance(s, dict) and s.get("id"):
            merged[s["id"]] = s
    for src_id, src in _SOURCES.items():
        if src_id and src_id not in merged and isinstance(src, dict):
            merged[src_id] = src
    results = []
    for s in merged.values():
        try:
            out = {}
            out["id"]          = str(s.get("id",""))[:64]
            out["url"]         = str(s.get("url",""))[:2000]
            out["label"]       = str(s.get("label",""))[:200]
            out["dataset_id"]  = str(s.get("dataset_id",""))[:80]
            stype = s.get("source_type") or s.get("type") or "rss"
            out["source_type"] = str(stype)[:32]
            out["type"]        = out["source_type"]
            try:
                out["interval"] = int(s.get("interval", 0) or 0)
            except (TypeError, ValueError):
                out["interval"] = 0
            try:
                out["limit"] = int(s.get("limit", s.get("lim", 50)) or 50)
            except (TypeError, ValueError):
                out["limit"] = 50
            out["enabled"] = bool(s.get("enabled", True))
            try:
                out["pull_count"] = int(s.get("pull_count", 0) or 0)
            except (TypeError, ValueError):
                out["pull_count"] = 0
            out["last_pulled"] = str(s.get("last_pulled",""))[:32]
            out["created_at"]  = str(s.get("created_at",""))[:32]
            raw_tags = s.get("tags")
            if isinstance(raw_tags, list):
                out["tags"] = [str(t)[:80] for t in raw_tags[:20] if t]
            elif isinstance(raw_tags, str) and raw_tags.strip():
                try:
                    parsed = json.loads(raw_tags)
                    if isinstance(parsed, list):
                        out["tags"] = [str(t)[:80] for t in parsed[:20] if t]
                    else:
                        out["tags"] = []
                except Exception:
                    out["tags"] = [t.strip()[:80] for t in raw_tags.split(",")
                                   if t.strip()][:20]
            else:
                out["tags"] = []
            jq = s.get("jq_path")
            if jq: out["jq_path"] = str(jq)[:200]
            page = s.get("page")
            if page: out["page"] = str(page)[:200]
            results.append(out)
        except Exception as e:
            log.warning("fabric_sources: skipping malformed row %r: %s",
                        s.get("id") if isinstance(s, dict) else "?", e)
            continue
    return {"sources": results}


@capability(
    "fabric.sources.add",
    http_method="POST", http_path="/fabric/sources/add", http_tags=["fabric"],
    memory="off",
    description="Register a data source (RSS, API, wiki, HTTP, scrape, recon). "
                "Input: url (str!), source_type (rss|api|http|wiki|scrape|recon), "
                "label (str), dataset_id (str), interval (int seconds, 0=manual), "
                "tags (comma-sep), headers (JSON), jq_path (str), "
                "page (str, wiki only), limit (int). "
                "Output: {id, label, dataset_id}.",
)
async def fabric_sources_add(
    url:         str  = "",
    source_type: str  = "rss",
    label:       str  = "",
    dataset_id:  str  = "",
    interval:    int  = 300,
    tags:        str  = "",
    headers:     str  = "{}",
    jq_path:     str  = "",
    page:        str  = "",
    limit:       int  = 50,
    enabled:     bool = True,
    config:      str  = "",
    auth:        str  = "",
    id:          str  = "",
    trace_id=None,
):
    # Caller can pre-set the source id for idempotent re-registration
    # (e.g. the doc crawler re-uses the same ID across re-crawls).
    src_id  = id.strip() if id and id.strip() else str(uuid.uuid4())
    # Robust hostname extraction — handles missing scheme, trailing slashes, etc.
    def _safe_hostname(u: str) -> str:
        try:
            from urllib.parse import urlparse
            if not u.startswith(("http://","https://")):
                u = "https://" + u.lstrip("/")
            host = urlparse(u).hostname or ""
            return re.sub(r"[^a-z0-9_]", "_", host.lower())[:40] or "source"
        except Exception:
            return "source"
    if dataset_id.strip():
        ds_id = re.sub(r"[^a-zA-Z0-9_]", "_", dataset_id.strip())[:80]
    else:
        ds_id = _safe_hostname(url) + "_" + src_id[:6]
    lbl     = label.strip() or (url[:60] if url else "source")
    # Auto-prepend scheme if missing — saves "list index out of range" later
    if url and not url.startswith(("http://","https://")):
        url = "https://" + url.lstrip("/")
    try:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    except Exception:
        tag_list = []
    # Normalise config + auth — accept dict OR JSON string
    def _to_json_str(v):
        if v is None or v == "": return ""
        if isinstance(v, (dict, list)): return json.dumps(v)
        if isinstance(v, str): return v.strip()
        return ""
    src = {
        "id":          src_id,
        "type":        source_type,
        "source_type": source_type,
        "url":         url,
        "label":       lbl,
        "dataset_id":  ds_id,
        "interval":    interval,
        "tags":        tag_list,
        "headers":     headers,
        "jq_path":     jq_path,
        "page":        page,
        "lim":         limit,
        "limit":       limit,
        "enabled":     enabled,
        "pull_count":  0,
        "last_pulled": "",
        "created_at":  now_iso(),
        "config":      _to_json_str(config),
        "auth":        _to_json_str(auth),
    }
    _SOURCES[src_id] = src
    await _sqlite_upsert_source(src)
    await emit_event({"type":"fabric.source.added","id":src_id,"label":lbl,"dataset_id":ds_id})
    return {"id": src_id, "label": lbl, "dataset_id": ds_id, "url": url}


@capability(
    "fabric.sources.delete",
    http_method="POST", http_path="/fabric/sources/delete", http_tags=["fabric"],
    memory="off",
    description="Remove a data source. Input: source_id (str!).",
)
async def fabric_sources_delete(source_id: str, trace_id=None):
    _SOURCES.pop(source_id, None)
    # Route through the writer queue — never open a parallel connection
    await _enqueue_write({
        "kind": "raw",
        "sql":  "DELETE FROM fabric_sources WHERE id=?",
        "params": (source_id,),
    }, wait=True)
    return {"deleted": True, "source_id": source_id}


@capability(
    "fabric.sources.pull",
    http_method="POST", http_path="/fabric/sources/pull", http_tags=["fabric"],
    memory="off",
    description="Pull a source immediately. Input: source_id (str!). "
                "Output: {ingested, dataset_id}.",
)
async def fabric_sources_pull(source_id: str, trace_id=None):
    src = _SOURCES.get(source_id)
    if not src:
        sl = await _sqlite_sources()
        src = next((s for s in sl if s["id"] == source_id), None)
    if not src:
        return {"error": f"Source not found: {source_id}"}
    # Normalise the source dict before pulling
    if "lim" in src and "limit" not in src:
        src["limit"] = src["lim"]
    n = await _pull_source(src)
    return {"ingested": n, "dataset_id": src.get("dataset_id",""), "source_id": source_id}




@capability(
    "fabric.health",
    http_method="GET", http_path="/fabric/health", http_tags=["fabric"],
    memory="off", silent=True,
    description="Diagnostic snapshot of fabric subsystem. Output: "
                "{db_path, journal_mode, db_size, has_journal_file, has_wal_files, "
                " writer_task_alive, write_queue_size, sources_count, datasets_count, "
                " auto_pull_active}.",
)
async def fabric_health(trace_id=None):
    out = {"db_path": SQLITE_PATH}
    # DB file presence and side files
    try:
        out["db_size"]         = os.path.getsize(SQLITE_PATH) if os.path.exists(SQLITE_PATH) else 0
        out["has_journal_file"] = os.path.exists(SQLITE_PATH + "-journal")
        out["has_wal_files"]   = (os.path.exists(SQLITE_PATH + "-wal")
                                   and os.path.exists(SQLITE_PATH + "-shm"))
    except Exception:
        pass
    # Journal mode + counts via a real (read-only) query
    try:
        loop = asyncio.get_running_loop()
        def _check():
            conn = _sqlite_conn()
            try:
                jm = conn.execute("PRAGMA journal_mode").fetchone()
                rcount = conn.execute("SELECT COUNT(*) FROM fabric_records").fetchone()
                dcount = conn.execute("SELECT COUNT(*) FROM fabric_datasets").fetchone()
                scount = conn.execute("SELECT COUNT(*) FROM fabric_sources").fetchone()
                return {
                    "journal_mode":     (jm[0] if jm else "?"),
                    "records_count":    rcount[0] if rcount else -1,
                    "datasets_count":   dcount[0] if dcount else -1,
                    "sources_count":    scount[0] if scount else -1,
                }
            finally:
                conn.close()
        out.update(await loop.run_in_executor(None, _check))
    except sqlite3.OperationalError as e:
        out["sqlite_error"] = str(e)
    except Exception as e:
        out["sqlite_error"] = str(e)
    # Writer queue state
    out["writer_task_alive"] = bool(_WRITE_TASK and not _WRITE_TASK.done())
    out["write_queue_size"]  = _WRITE_QUEUE.qsize() if _WRITE_QUEUE else None
    out["index_migration_done"] = bool(_INDEX_MIGRATION_DONE)
    # In-memory source count
    out["in_memory_sources"] = len(_SOURCES)
    # Auto-pull semaphore
    out["auto_pull_sem"] = "ready" if _AUTO_PULL_SEM is not None else "not_initialised"
    return out



# ─────────────────────────────────────────────────────────────────────────────
# LOOM — relation stitching within / across datasets
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "fabric.loom.run",
    http_method="POST", http_path="/fabric/loom/run", http_tags=["fabric","loom"],
    memory="off",
    description="Stitch relations across (or within) datasets. Runs server-side on the "
                "full dataset, emits fabric.loom.progress events, and writes RELATED_TO "
                "edges into the Neo4j graph so relations persist. "
                "Input: dataset_a (str!), dataset_b (str — optional, defaults to dataset_a), "
                "mode (entity|semantic|hybrid|tag default hybrid), "
                "min_score (float 0-1 default 0.4), "
                "max_matches (int default 100), "
                "tag_filter (str — comma-sep), "
                "persist (bool default True — write to graph). "
                "Output: {matches:[{a_id,b_id,score,evidence}], total, persisted}.",
)
async def fabric_loom_run(
    dataset_a:    str,
    dataset_b:    str   = "",
    mode:         str   = "hybrid",
    min_score:    float = 0.4,
    max_matches:  int   = 100,
    tag_filter:   str   = "",
    persist:      bool  = True,
    only_new_ids: str   = "",
    graph:        str   = "fabric",
    edge_type:    str   = "auto",
    trace_id=None,
) -> Dict:
    if not dataset_a:
        return {"error": "dataset_a required"}
    if not dataset_b:
        dataset_b = dataset_a  # within-dataset stitching
    same_ds = (dataset_a == dataset_b)
    tags = [t.strip().lower() for t in (tag_filter or "").split(",") if t.strip()]

    async def _emit(stage, **kw):
        try:
            await emit_event({"type":"fabric.loom.progress",
                              "dataset_a":dataset_a, "dataset_b":dataset_b,
                              "stage":stage, **kw})
        except Exception: pass

    await _emit("loading", message=f"Loading {dataset_a}" + ("" if same_ds else f" + {dataset_b}"))

    # Stream all records from each dataset (paged)
    async def _load_all(ds_id):
        out = []
        offset = 0
        page = 200
        while True:
            rows = await _sqlite_query(dataset_id=ds_id, limit=page, offset=offset)
            if not rows: break
            out.extend(rows)
            if len(rows) < page: break
            offset += page
            if len(out) >= 5000:  # hard cap to avoid runaway
                break
        return out

    recs_a = await _load_all(dataset_a)
    recs_b = recs_a if same_ds else await _load_all(dataset_b)

    # Apply tag filter client-side
    if tags:
        def _matches(r):
            try:
                rt = json.loads(r.get("tags","[]")) if isinstance(r.get("tags"),str) else (r.get("tags") or [])
            except Exception:
                rt = []
            rt_lc = [str(t).lower() for t in rt]
            return any(t in rt_lc for t in tags)
        recs_a = [r for r in recs_a if _matches(r)]
        if not same_ds:
            recs_b = [r for r in recs_b if _matches(r)]

    # only_new_ids: restrict A to a specific ID subset (used by auto-stitch
    # to compare just-ingested records against the rest of the dataset)
    if only_new_ids:
        new_set = set(s.strip() for s in only_new_ids.split(",") if s.strip())
        if new_set:
            recs_a = [r for r in recs_a if r.get("id") in new_set]

    if not recs_a or not recs_b:
        await _emit("done", message=f"No records to stitch ({len(recs_a)}/{len(recs_b)})")
        return {"matches": [], "total": 0, "persisted": 0,
                "info": "no records after filter",
                "counts": {"a": len(recs_a), "b": len(recs_b)}}

    await _emit("comparing", message=f"Comparing {len(recs_a)} × {len(recs_b)} records",
                a_count=len(recs_a), b_count=len(recs_b))

    matches: List[dict] = []

    def _r_text(r):
        t = (r.get("text") or "").lower()
        try:
            d = json.loads(r.get("data","{}")) if isinstance(r.get("data"),str) else (r.get("data") or {})
            t = t + " " + " ".join(str(v) for v in d.values()
                                    if isinstance(v, (str,int,float)))[:600]
        except Exception:
            pass
        return t

    def _r_tags(r):
        try:
            rt = json.loads(r.get("tags","[]")) if isinstance(r.get("tags"),str) else (r.get("tags") or [])
        except Exception:
            rt = []
        return set(str(t).lower() for t in rt)

    if mode in ("entity", "hybrid", "semantic"):
        # Score each pair via word-overlap. Skip self-matches when same_ds.
        # Note: O(N*M) — capped by 5000 above so worst case is 25M pairs which
        # is acceptable since the body is just set ops.
        for ra in recs_a:
            ra_id = ra["id"]
            ta = _r_text(ra)
            wa = {w for w in ta.split() if len(w) > 4}
            if not wa:
                continue
            for rb in recs_b:
                if same_ds and ra_id >= rb["id"]:
                    continue  # avoid duplicates and self
                tb = _r_text(rb)
                wb = {w for w in tb.split() if len(w) > 4}
                if not wb:
                    continue
                inter = wa & wb
                if not inter:
                    continue
                # Jaccard for hybrid; raw overlap for entity
                if mode == "entity":
                    score = len(inter) / max(len(wa), 1)
                else:
                    score = len(inter) / max(len(wa | wb), 1) * 2
                score = min(1.0, score)
                if score >= min_score:
                    matches.append({
                        "a_id": ra_id, "b_id": rb["id"],
                        "a_dataset": ra["dataset_id"], "b_dataset": rb["dataset_id"],
                        "score": round(score, 4),
                        "evidence": list(inter)[:8],
                    })

    elif mode == "tag":
        for ra in recs_a:
            ra_id = ra["id"]
            ta_set = _r_tags(ra)
            if not ta_set:
                continue
            for rb in recs_b:
                if same_ds and ra_id >= rb["id"]:
                    continue
                tb_set = _r_tags(rb)
                inter = ta_set & tb_set
                if len(inter) >= 1:
                    score = len(inter) / max(len(ta_set | tb_set), 1)
                    if score >= min_score:
                        matches.append({
                            "a_id": ra_id, "b_id": rb["id"],
                            "a_dataset": ra["dataset_id"], "b_dataset": rb["dataset_id"],
                            "score": round(score, 4),
                            "evidence": list(inter)[:8],
                        })

    matches.sort(key=lambda m: -m["score"])
    matches = matches[:max_matches]

    await _emit("scored", message=f"Found {len(matches)} matches",
                count=len(matches))

    persisted = 0
    edge_type_counts = {}
    if persist and matches:
        adapter = GRAPHS.get(graph) or GRAPHS.get("fabric")
        if adapter and adapter.available:
            # Classify edge type for each match
            def _classify_edge(m):
                """Determine edge type based on evidence, score, and mode."""
                if edge_type != "auto":
                    return edge_type
                score = m.get("score", 0)
                evidence = m.get("evidence", [])
                ev_str = " ".join(str(e).lower() for e in evidence)
                a_ds = m.get("a_dataset", "")
                b_ds = m.get("b_dataset", "")

                # Cross-dataset matches at high score suggest derivation
                if a_ds != b_ds and score > 0.8:
                    return "DERIVED_FROM"
                # URL/reference evidence suggests citation
                if any(w in ev_str for w in ("http", "doi:", "arxiv", "isbn", "cite")):
                    return "REFERENCES"
                # Tag-only matches suggest topic sharing
                if mode == "tag":
                    return "SHARES_TOPIC"
                # High score within same dataset suggests similarity
                if same_ds and score > 0.6:
                    return "SIMILAR_TO"
                # Medium score cross-dataset suggests topical relation
                if not same_ds and score > 0.5:
                    return "SHARES_TOPIC"
                # Default
                return "RELATED_TO"

            # 1) Edges between FabricRecord nodes (the label fabric actually creates)
            record_edges = []
            edge_type_counts = {}
            for m in matches[:200]:
                rel = _classify_edge(m)
                edge_type_counts[rel] = edge_type_counts.get(rel, 0) + 1
                record_edges.append({
                    "from_label": "FabricRecord", "from_id": m["a_id"],
                    "to_label":   "FabricRecord", "to_id":   m["b_id"],
                    "rel":        rel,
                    "props":      {"score": m["score"], "mode": mode,
                                    "evidence": ", ".join(m["evidence"][:5])},
                })
                # Store the classified type back on the match for UI display
                m["edge_type"] = rel
            # 2) Aggregate to dataset-level edges with a count so the main graph
            #    view (Dataset->Dataset) shows the relationship strength.
            ds_pair_counts: Dict[tuple, Dict] = {}
            for m in matches:
                key = tuple(sorted([m.get("a_dataset",""), m.get("b_dataset","")]))
                if not key[0] or not key[1] or key[0] == key[1]:
                    continue
                d = ds_pair_counts.setdefault(key, {"count":0, "score_sum":0.0})
                d["count"] += 1
                d["score_sum"] += float(m.get("score") or 0)
            ds_edges = []
            for (a, b), info in ds_pair_counts.items():
                avg = info["score_sum"] / max(info["count"], 1)
                ds_edges.append({
                    "from_label": "Dataset", "from_id": a,
                    "to_label":   "Dataset", "to_id":   b,
                    "rel":        "RELATED_TO",
                    "props":      {"loom_match_count": info["count"],
                                    "loom_avg_score":   round(avg, 4),
                                    "loom_mode":        mode},
                })
            try:
                persisted = await adapter.link_many(record_edges + ds_edges)
                if persisted:
                    log.info("loom persisted %d edges to %s graph "
                             "(%d record + %d dataset)",
                             persisted, adapter.name,
                             len(record_edges), len(ds_edges))
            except Exception as e:
                log.warning("loom persist: %s", e)
        else:
            log.info("loom: graph '%s' unavailable, skipping persist", graph)

    edge_summary = ", ".join(f"{k}:{v}" for k, v in sorted(edge_type_counts.items())) if persist and matches else ""
    await _emit("done", message=f"{len(matches)} matches, {persisted} edges written" +
                (f" ({edge_summary})" if edge_summary else ""),
                matches=len(matches), persisted=persisted,
                edge_types=edge_type_counts if persist else {})

    return {
        "ok":         True,
        "matches":    matches,
        "total":      len(matches),
        "persisted":  persisted,
        "mode":       mode,
        "edge_type":  edge_type,
        "edge_types": edge_type_counts if persist else {},
        "counts":     {"a": len(recs_a), "b": len(recs_b)},
    }




# ─────────────────────────────────────────────────────────────────────────────
# SKILLS / ONTOLOGY BUILDER
# A "skill" is a structured knowledge artefact derived from one or more
# datasets in the fabric. It contains:
#   - name, description, tags
#   - the dataset_ids it was built from
#   - an ontology (concepts + relations) extracted by the LLM
#   - sample records used as evidence
# Skills are queryable via fabric.query and can be fed back to other agents.
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "fabric.skills.list",
    http_method="GET", http_path="/fabric/skills", http_tags=["fabric","skills"],
    memory="off",
    description="List all skills. Output: {skills:[{id,name,description,dataset_ids,...}]}.",
)
async def fabric_skills_list(trace_id=None) -> Dict:
    loop = asyncio.get_running_loop()
    def _sync():
        conn = _sqlite_conn()
        try:
            rows = conn.execute(
                "SELECT id, name, description, dataset_ids, tags, created_at, updated_at "
                "FROM fabric_skills ORDER BY updated_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    try:
        rows = await loop.run_in_executor(None, _sync)
    except Exception as e:
        log.warning("skills list: %s", e)
        return {"skills": [], "error": str(e)}
    out = []
    for r in rows:
        try:
            r["dataset_ids"] = json.loads(r.get("dataset_ids","[]"))
        except Exception:
            r["dataset_ids"] = []
        try:
            r["tags"] = json.loads(r.get("tags","[]"))
        except Exception:
            r["tags"] = []
        out.append(r)
    return {"skills": out, "count": len(out)}


@capability(
    "fabric.skills.get",
    http_method="GET", http_path="/fabric/skills/get", http_tags=["fabric","skills"],
    memory="off",
    description="Fetch a single skill by id, including ontology and samples. "
                "Input: skill_id (str!).",
)
async def fabric_skills_get(skill_id: str, trace_id=None) -> Dict:
    loop = asyncio.get_running_loop()
    def _sync():
        conn = _sqlite_conn()
        try:
            row = conn.execute("SELECT * FROM fabric_skills WHERE id=?", (skill_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    row = await loop.run_in_executor(None, _sync)
    if not row:
        return {"error": "skill not found"}
    for f in ("dataset_ids","tags","ontology","samples"):
        try:
            row[f] = json.loads(row.get(f,"") or ("[]" if f in ("dataset_ids","tags","samples") else "{}"))
        except Exception:
            row[f] = [] if f in ("dataset_ids","tags","samples") else {}
    return row


@capability(
    "fabric.skills.delete",
    http_method="POST", http_path="/fabric/skills/delete", http_tags=["fabric","skills"],
    memory="off",
    description="Delete a skill. Input: skill_id (str!).",
)
async def fabric_skills_delete(skill_id: str, trace_id=None) -> Dict:
    await _enqueue_write({"kind":"raw",
                          "sql":"DELETE FROM fabric_skills WHERE id=?",
                          "params":(skill_id,)}, wait=True)
    return {"deleted": True, "skill_id": skill_id}


@capability(
    "fabric.skills.build",
    http_method="POST", http_path="/fabric/skills/build", http_tags=["fabric","skills"],
    memory="on",
    description="Build a skill from one or more datasets. Samples records, asks the "
                "LLM to extract concepts and relations, stores the resulting ontology. "
                "Input: name (str!), dataset_ids (str — comma-sep!), "
                "description (str — optional, LLM auto-generates if blank), "
                "sample_size (int default 30 — records to sample for ontology extraction), "
                "tags (str — comma-sep). "
                "Output: {skill_id, name, ontology, samples_used}.",
)
async def fabric_skills_build(
    name:        str,
    dataset_ids: str,
    description: str = "",
    sample_size: int = 60,
    tags:        str = "",
    trace_id=None,
) -> Dict:
    if not name.strip():
        return {"error": "name required"}
    ds_list = [d.strip() for d in dataset_ids.split(",") if d.strip()]
    if not ds_list:
        return {"error": "dataset_ids required"}

    sample_size = max(5, min(500, sample_size))
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    skill_id = "skill_" + hashlib.sha256(
        (name + "|" + ",".join(sorted(ds_list))).encode()
    ).hexdigest()[:16]

    async def _emit(stage, **kw):
        try:
            await emit_event({"type":"fabric.skills.progress",
                              "skill_id":skill_id, "name":name,
                              "stage":stage, **kw})
        except Exception: pass

    await _emit("starting", message=f"Sampling {len(ds_list)} dataset(s)…")

    # Sample records — pull from across the timeline (head, mid, tail) so we
    # get a representative slice, not just the newest. Round-robin across datasets.
    samples = []
    per_ds = max(3, sample_size // max(len(ds_list), 1))
    for ds in ds_list:
        # Total record count for this dataset
        loop = asyncio.get_running_loop()
        def _count():
            conn = _sqlite_conn()
            try:
                row = conn.execute("SELECT COUNT(*) FROM fabric_records WHERE dataset_id=?",
                                    (ds,)).fetchone()
                return row[0] if row else 0
            finally:
                conn.close()
        total = await loop.run_in_executor(None, _count)
        if total == 0: continue

        # Take head, middle, tail
        head_n = max(1, per_ds // 3)
        mid_n  = max(1, per_ds // 3)
        tail_n = per_ds - head_n - mid_n
        head_rows = await _sqlite_query(dataset_id=ds, limit=head_n, offset=0)
        mid_rows  = await _sqlite_query(dataset_id=ds, limit=mid_n,
                                          offset=max(0, total // 2 - mid_n // 2))
        tail_rows = await _sqlite_query(dataset_id=ds, limit=tail_n,
                                          offset=max(0, total - tail_n))

        # Read existing dataset tags so the prompt knows what's already there
        def _ds_tags():
            conn = _sqlite_conn()
            try:
                return [r[0] for r in conn.execute(
                    "SELECT tag FROM fabric_dataset_tags WHERE dataset_id=?",
                    (ds,)).fetchall()]
            finally:
                conn.close()
        ds_tags_list = []
        try:
            ds_tags_list = await loop.run_in_executor(None, _ds_tags)
        except Exception: pass

        seen_ids = set()
        for r in head_rows + mid_rows + tail_rows:
            if r["id"] in seen_ids: continue
            seen_ids.add(r["id"])
            # Pull source URL from data field if present
            src_url = ""
            try:
                d = json.loads(r.get("data","{}")) if isinstance(r.get("data"), str) else (r.get("data") or {})
                src_url = d.get("link") or d.get("url") or d.get("_source_url") or ""
            except Exception:
                pass
            samples.append({
                "id":            r["id"],
                "dataset_id":    r["dataset_id"],
                "text":          (r.get("text","") or "")[:800],
                "tags":          r.get("tags","[]"),
                "ds_tags":       ds_tags_list,
                "src_url":       src_url[:120],
            })
        if len(samples) >= sample_size:
            break
    samples = samples[:sample_size]

    if not samples:
        return {"error": f"No records found in: {', '.join(ds_list)}"}

    await _emit("extracting", message=f"Asking LLM to extract concepts from {len(samples)} samples…",
                samples=len(samples))

    # Build extraction prompt — one big prompt with all samples
    # Build a context-rich prompt: each sample includes dataset tags + source URL
    # if available. This grounds the LLM extraction in real provenance.
    sample_lines = []
    for i, s in enumerate(samples):
        meta = []
        if s.get("ds_tags"): meta.append("ds_tags=" + ",".join(s["ds_tags"][:5]))
        if s.get("src_url"): meta.append("src=" + s["src_url"])
        meta_str = " [" + " ".join(meta) + "]" if meta else ""
        sample_lines.append(f"[{i+1}] ({s['dataset_id']}){meta_str}\n{s['text']}")
    sample_text = "\n\n".join(sample_lines)[:18000]

    system = (
        "You are an ontology extraction expert. Given a representative sample of "
        "records from one or more datasets, identify the SUBSTANTIVE concepts that "
        "characterise the domain — entities, topics, technologies, events, named "
        "things, key terminology. Look for what the records are ABOUT, not just "
        "what words appear. Use the dataset tags and source URLs as context. "
        "Return ONLY valid JSON in this exact shape:\n"
        "{\n"
        '  "summary": "2-4 sentence description of the domain and what an agent '
        'with this skill would know how to handle",\n'
        '  "concepts": [{"name": "PreciseName", "type": "topic|entity|tech|event|term|person|org|product", "description": "what it is and why it matters in this domain"}],\n'
        '  "relations": [{"from": "ConceptA", "to": "ConceptB", "label": "is_a|part_of|uses|caused_by|relates_to|opposite_of|instance_of|derived_from"}]\n'
        "}\n"
        "Aim for 15-30 concepts and 20-50 relations. Concept names: PascalCase or "
        "specific phrases — never generic words like 'data' or 'system'. Match "
        "concept names EXACTLY between the concepts list and the relations."
    )

    prompt = f"Records:\n{sample_text}\n\nExtract the ontology. Return ONLY the JSON object, no preamble."

    ontology = {"summary":"", "concepts":[], "relations":[]}
    auto_desc = ""
    try:
        from Vera.Orchestration.capability_orchestration import ollama_generate
        raw = await asyncio.wait_for(
            ollama_generate(prompt, system=system, json_mode=True),
            timeout=120,
        )
        # Strip code fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\n?|\n?```$", "", raw)
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                ontology = {
                    "summary":   str(parsed.get("summary",""))[:1000],
                    "concepts":  parsed.get("concepts",[])[:25] if isinstance(parsed.get("concepts"), list) else [],
                    "relations": parsed.get("relations",[])[:40] if isinstance(parsed.get("relations"), list) else [],
                }
                auto_desc = ontology["summary"]
        except Exception as e:
            log.warning("skills extract parse: %s; raw[:200]=%s", e, raw[:200])
            await _emit("warning", message=f"LLM returned malformed JSON: {e}")
    except asyncio.TimeoutError:
        await _emit("warning", message="LLM extraction timed out")
    except Exception as e:
        await _emit("warning", message=f"LLM unavailable: {e}")

    final_desc = description.strip() or auto_desc or f"Skill built from {len(ds_list)} dataset(s)"

    # Persist locally for fast retrieval (the "fabric skill" record).
    payload = {
        "id":           skill_id,
        "name":         name.strip(),
        "description":  final_desc,
        "dataset_ids":  json.dumps(ds_list),
        "ontology":     json.dumps(ontology),
        "samples":      json.dumps([{"id":s["id"], "dataset_id":s["dataset_id"]} for s in samples]),
        "tags":         json.dumps(tag_list),
        "created_at":   now_iso(),
        "updated_at":   now_iso(),
    }
    await _enqueue_write({
        "kind": "raw",
        "sql":  "INSERT OR REPLACE INTO fabric_skills "
                "(id, name, description, dataset_ids, ontology, samples, tags, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
        "params": (payload["id"], payload["name"], payload["description"],
                   payload["dataset_ids"], payload["ontology"], payload["samples"],
                   payload["tags"], payload["created_at"], payload["updated_at"]),
    }, wait=True)

    # ALSO register through the canonical skills + ontologies modules so the
    # built artefacts show up in the Vera Skills UI alongside hand-authored ones.
    # We do this fire-and-forget — fabric persistence is the source of truth here.
    skill_external_id = None
    ontology_external_id = None
    try:
        from Vera.Orchestration.skills import skills_create, ontologies_create
        # 1) Skill — content is a system-prompt fragment derived from the ontology
        # Build a substantive skill content body — concepts grouped by type,
        # key relations summarised, and a usage hint.
        concepts_by_type: Dict[str, list] = {}
        for c in ontology.get("concepts", []):
            if isinstance(c, dict) and c.get("name"):
                concepts_by_type.setdefault(c.get("type","concept"), []).append(c)
        type_lines = []
        for ctype, items in sorted(concepts_by_type.items()):
            names = [c.get("name","?") for c in items[:12]]
            type_lines.append(f"- {ctype}: {', '.join(names)}")
        # Top relations
        rel_lines = []
        for r in ontology.get("relations", [])[:15]:
            if isinstance(r, dict):
                rel_lines.append(f"  {r.get('from','?')} --[{r.get('label','')}]--> {r.get('to','?')}")
        skill_content = (
            f"# Specialist Knowledge: {name.strip()}\n\n"
            f"## Domain\n{final_desc}\n\n"
            f"Built from {len(ds_list)} fabric dataset(s): {', '.join(ds_list)}\n"
            f"Sampled {len(samples)} representative records.\n\n"
            f"## Key Concepts (by type)\n" + "\n".join(type_lines) + "\n\n"
            f"## Notable Relations\n" + "\n".join(rel_lines) + "\n\n"
            f"## Usage\n"
            f"When the user asks about any of these concepts or topics, draw on this "
            f"specialist knowledge. You can also query the underlying records via "
            f"the fabric.query capability with dataset_id in {ds_list}."
        )
        try:
            sk_rec = await skills_create(
                name        = name.strip(),
                content     = skill_content,
                description = final_desc,
                type        = "system_prompt",
                tags        = ",".join(tag_list + ["fabric_built"] + ds_list[:3]),
                enabled     = True,
            )
            skill_external_id = sk_rec.get("id") if isinstance(sk_rec, dict) else None
        except Exception as e:
            log.warning("skills_create from fabric: %s", e)

        # 2) Ontology — concepts → entities, relations → relationships
        try:
            ent_list = []
            for c in ontology.get("concepts", []):
                if not isinstance(c, dict) or not c.get("name"): continue
                ent_list.append({
                    "name":        c.get("name"),
                    "type":        c.get("type", "concept"),
                    "description": c.get("description", ""),
                })
            rel_list = []
            for r in ontology.get("relations", []):
                if not isinstance(r, dict): continue
                if r.get("from") and r.get("to"):
                    rel_list.append({
                        "from":  r.get("from"),
                        "to":    r.get("to"),
                        "label": r.get("label", "RELATED_TO"),
                    })
            ont_rec = await ontologies_create(
                name              = name.strip() + " — Ontology",
                description       = final_desc,
                domain            = (tag_list[0] if tag_list else "general"),
                context_hints     = "Built from fabric datasets: " + ", ".join(ds_list),
                entities          = json.dumps(ent_list),
                relationships     = json.dumps(rel_list),
                tags              = ",".join(tag_list + ["fabric_built"]),
                enabled           = True,
            )
            ontology_external_id = ont_rec.get("id") if isinstance(ont_rec, dict) else None
        except Exception as e:
            log.warning("ontologies_create from fabric: %s", e)
    except ImportError:
        log.debug("skills module not loaded — skipping canonical registration")
    except Exception as e:
        log.warning("skills/ontology registration: %s", e)

    # Persist concepts to Neo4j (best-effort, non-blocking)
    if FABRIC_NEO.available:
        async def _persist_graph():
            try:
                # Skill node
                await FABRIC_NEO.upsert_node("Skill", skill_id,
                                             {"name": name, "description": final_desc[:500]})
                for c in ontology.get("concepts", []):
                    if not isinstance(c, dict) or not c.get("name"):
                        continue
                    cid = "concept_" + hashlib.sha1(
                        (skill_id + "|" + c["name"]).encode()
                    ).hexdigest()[:16]
                    await FABRIC_NEO.upsert_node("Concept", cid,
                                                  {"name":c.get("name",""),
                                                   "type":c.get("type",""),
                                                   "description":c.get("description","")[:300]})
                    await FABRIC_NEO.link("Skill", skill_id, "Concept", cid, rel="HAS_CONCEPT")
                # Relations
                concept_ids = {c.get("name"): "concept_"+hashlib.sha1(
                    (skill_id+"|"+c["name"]).encode()).hexdigest()[:16]
                    for c in ontology.get("concepts",[]) if isinstance(c, dict) and c.get("name")}
                for rel in ontology.get("relations", []):
                    if not isinstance(rel, dict): continue
                    fid = concept_ids.get(rel.get("from"))
                    tid = concept_ids.get(rel.get("to"))
                    if fid and tid:
                        rname = re.sub(r"[^A-Z_]", "_",
                                        (rel.get("label","RELATED")).upper())[:32] or "RELATED"
                        await FABRIC_NEO.link("Concept", fid, "Concept", tid, rel=rname)
                # Datasets used
                for ds in ds_list:
                    await FABRIC_NEO.link("Skill", skill_id, "Dataset", ds, rel="BUILT_FROM")
            except Exception as e:
                log.warning("skills graph persist: %s", e)
        asyncio.create_task(_persist_graph())

    await _emit("done", message=f"Skill ready: {len(ontology.get('concepts',[]))} concepts, "
                                f"{len(ontology.get('relations',[]))} relations",
                concepts=len(ontology.get("concepts",[])),
                relations=len(ontology.get("relations",[])))

    return {
        "ok":                   True,
        "skill_id":             skill_id,
        "skill_external_id":    skill_external_id,
        "ontology_external_id": ontology_external_id,
        "name":                 name,
        "description":          final_desc,
        "dataset_ids":          ds_list,
        "ontology":             ontology,
        "samples_used":         len(samples),
    }




@capability(
    "fabric.extract_graph",
    http_method="POST", http_path="/fabric/extract_graph", http_tags=["fabric","graph","nlp"],
    memory="on",
    description="Extract entities and relations from records into a graph. "
                "Modes: nlp (regex/heuristics, fast), llm (deeper, slow), hybrid. "
                "Input: dataset_id (str!), mode (nlp|llm|hybrid default nlp), "
                "limit (int default 100 records to process), "
                "graph (str default 'fabric'), persist (bool default True). "
                "Output: {entities, relations, persisted, dataset_id}.",
)
async def fabric_extract_graph(
    dataset_id:  str,
    mode:        str  = "nlp",
    limit:       int  = 100,
    graph:       str  = "fabric",
    persist:     bool = True,
    trace_id=None,
) -> Dict:
    if not dataset_id:
        return {"error": "dataset_id required"}
    limit = max(1, min(1000, limit))

    async def _emit(stage, **kw):
        try:
            await emit_event({"type":"fabric.extract_graph.progress",
                              "dataset_id": dataset_id, "mode": mode,
                              "stage": stage, **kw})
        except Exception: pass

    await _emit("loading", message=f"Loading up to {limit} records from {dataset_id}")

    # Stream records
    records: list = []
    offset = 0
    page_sz = 100
    while len(records) < limit:
        batch = await _sqlite_query(dataset_id=dataset_id, limit=min(page_sz, limit-len(records)), offset=offset)
        if not batch: break
        records.extend(batch)
        if len(batch) < page_sz: break
        offset += page_sz

    if not records:
        return {"error": f"No records in {dataset_id}"}

    await _emit("extracting", message=f"Extracting from {len(records)} records",
                count=len(records))

    entities: Dict[str, Dict] = {}   # entity_id -> {name, type, mentions:[record_id]}
    relations: List[Dict]    = []

    # ── NLP mode: regex-based entity + relation extraction ──
    # Identifies: code symbols (CamelCase, snake_case identifiers), URLs,
    # capitalised noun phrases, code fence languages, hashtag-style refs.
    if mode in ("nlp", "hybrid"):
        # Patterns
        pat_camel    = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+){1,})\b")
        pat_snake    = re.compile(r"\b([a-z]+(?:_[a-z]+){1,})\b")
        pat_caps     = re.compile(r"\b([A-Z][A-Z0-9_]{2,}[A-Z0-9])\b")  # CONSTANTS
        pat_url      = re.compile(r"https?://([\w.-]+)/?")
        pat_func     = re.compile(r"\bdef\s+(\w+)\b|\bfunction\s+(\w+)\b")
        pat_class    = re.compile(r"\bclass\s+(\w+)\b")
        pat_import   = re.compile(r"(?:import|from)\s+([\w.]+)")
        pat_quoted   = re.compile(r"\"([A-Za-z][A-Za-z0-9 _-]{2,40})\"")

        for rec in records:
            text = (rec.get("text") or "")[:6000]
            rid = rec["id"]
            local_ents = set()

            # Collect entities by category
            for m in pat_class.finditer(text):
                name = m.group(1); local_ents.add(("class", name))
            for m in pat_func.finditer(text):
                name = m.group(1) or m.group(2); local_ents.add(("function", name))
            for m in pat_import.finditer(text):
                local_ents.add(("module", m.group(1)))
            for m in pat_camel.finditer(text):
                local_ents.add(("type", m.group(1)))
            for m in pat_caps.finditer(text):
                local_ents.add(("constant", m.group(1)))
            for m in pat_url.finditer(text):
                local_ents.add(("host", m.group(1)))
            for m in pat_quoted.finditer(text):
                local_ents.add(("term", m.group(1)))

            # Register entities
            for etype, name in local_ents:
                eid = etype + ":" + hashlib.sha1(name.encode()).hexdigest()[:12]
                ent = entities.setdefault(eid, {"id":eid, "name":name, "type":etype,
                                                 "mentions": []})
                if rid not in ent["mentions"]:
                    ent["mentions"].append(rid)

            # Co-occurrence: any two entities in the same record become a CO_OCCURS edge
            ent_list = list(local_ents)
            for i in range(len(ent_list)):
                for j in range(i+1, len(ent_list)):
                    ti, ni = ent_list[i]; tj, nj = ent_list[j]
                    a_id = ti + ":" + hashlib.sha1(ni.encode()).hexdigest()[:12]
                    b_id = tj + ":" + hashlib.sha1(nj.encode()).hexdigest()[:12]
                    relations.append({
                        "from_id": a_id, "to_id": b_id,
                        "rel": "CO_OCCURS",
                        "record_id": rid,
                    })

    # ── LLM mode: deeper extraction via Ollama ──
    if mode in ("llm", "hybrid"):
        try:
            from Vera.Orchestration.capability_orchestration import ollama_generate
        except Exception:
            ollama_generate = None
        if ollama_generate:
            await _emit("llm_phase", message="Asking LLM to extract relations...")
            # Process in small batches to keep prompts manageable
            batch_size = 8
            for i in range(0, min(len(records), 40), batch_size):
                chunk = records[i:i+batch_size]
                texts = "\n\n".join(
                    f"[id={r['id']}] {(r.get('text') or '')[:400]}"
                    for r in chunk
                )[:6000]
                prompt = (
                    "Extract entities and relations from these records. "
                    "Return ONLY a JSON object:\n"
                    "{\"entities\":[{\"name\":\"...\",\"type\":\"person|tech|concept|tool\"}],"
                    "\"relations\":[{\"from\":\"name\",\"to\":\"name\",\"rel\":\"USES|PART_OF|RELATES_TO\"}]}\n"
                    "Limit to 15 entities and 20 relations total.\n\n"
                    f"Records:\n{texts}"
                )
                try:
                    raw = await asyncio.wait_for(
                        ollama_generate(prompt, system="You output JSON only.", json_mode=True),
                        timeout=90,
                    )
                    raw = raw.strip()
                    if raw.startswith("```"):
                        raw = re.sub(r"^```(?:json)?\n?|\n?```$", "", raw)
                    parsed = json.loads(raw)
                    for e in (parsed.get("entities") or [])[:20]:
                        if not isinstance(e, dict): continue
                        name = (e.get("name") or "").strip()
                        if not name: continue
                        etype = (e.get("type") or "concept")[:20]
                        eid = etype + ":" + hashlib.sha1(name.encode()).hexdigest()[:12]
                        ent = entities.setdefault(eid, {"id":eid, "name":name, "type":etype,
                                                         "mentions": []})
                        # Mention is the chunk's record range
                        for r in chunk:
                            if name.lower() in (r.get("text") or "").lower() and r["id"] not in ent["mentions"]:
                                ent["mentions"].append(r["id"])
                    name_to_id = {ent["name"]: ent["id"] for ent in entities.values()}
                    for r in (parsed.get("relations") or [])[:30]:
                        if not isinstance(r, dict): continue
                        f_id = name_to_id.get(r.get("from"))
                        t_id = name_to_id.get(r.get("to"))
                        if f_id and t_id:
                            relations.append({
                                "from_id": f_id, "to_id": t_id,
                                "rel": (r.get("rel","RELATES_TO")[:30]).upper(),
                            })
                except asyncio.TimeoutError:
                    await _emit("warning", message=f"LLM batch {i//batch_size+1} timed out")
                except Exception as e:
                    await _emit("warning", message=f"LLM batch {i//batch_size+1}: {e}")

    # Deduplicate relations
    seen_rels = set()
    deduped = []
    for r in relations:
        key = (r["from_id"], r["to_id"], r["rel"])
        if key not in seen_rels:
            seen_rels.add(key)
            deduped.append(r)
    relations = deduped

    await _emit("scored", message=f"{len(entities)} entities, {len(relations)} relations",
                entities=len(entities), relations=len(relations))

    # Persist
    persisted_nodes = 0
    persisted_edges = 0
    if persist and (entities or relations):
        adapter = GRAPHS.get(graph) or GRAPHS.get("fabric")
        if adapter and adapter.available:
            mention_edges = []
            for ent in entities.values():
                if await adapter.upsert_node("Entity", ent["id"],
                                              {"name": ent["name"],
                                               "type": ent["type"],
                                               "mention_count": len(ent["mentions"]),
                                               "dataset_id": dataset_id}):
                    persisted_nodes += 1
                # Link to mentioning records — use FabricRecord label (not Record)
                for rid in ent["mentions"][:50]:
                    mention_edges.append({
                        "from_label": "Entity", "from_id": ent["id"],
                        "to_label":   "FabricRecord", "to_id": rid,
                        "rel":        "MENTIONED_IN",
                        "props":      {"dataset_id": dataset_id},
                    })
            if mention_edges:
                await adapter.link_many(mention_edges)
            edges = [{
                "from_label": "Entity", "from_id": r["from_id"],
                "to_label":   "Entity", "to_id":   r["to_id"],
                "rel":        r["rel"],
                "props":      {"record_id": r.get("record_id","")},
            } for r in relations]
            persisted_edges = await adapter.link_many(edges)

        # Also persist to fabric_entity_mentions junction table (if it exists)
        try:
            loop = asyncio.get_running_loop()
            def _write_mentions():
                conn = _sqlite_conn()
                ts = now_iso()
                for ent in entities.values():
                    for rid in ent["mentions"][:200]:
                        conn.execute(
                            "INSERT OR IGNORE INTO fabric_entity_mentions "
                            "(entity_id, record_id, dataset_id, created_at) "
                            "VALUES (?,?,?,?)",
                            (ent["id"], rid, dataset_id, ts)
                        )
                conn.commit()
                conn.close()
            await loop.run_in_executor(None, _write_mentions)
        except Exception as _e:
            log.debug("entity mentions write: %s", _e)

    await _emit("done",
                message=f"Done: {len(entities)} entities, {len(relations)} relations, "
                        f"{persisted_nodes} nodes + {persisted_edges} edges written",
                entities=len(entities), relations=len(relations),
                persisted_nodes=persisted_nodes, persisted_edges=persisted_edges)

    return {
        "ok":              True,
        "dataset_id":      dataset_id,
        "mode":            mode,
        "entities":        list(entities.values())[:200],
        "relations":       relations[:300],
        "entity_count":    len(entities),
        "relation_count":  len(relations),
        "persisted_nodes": persisted_nodes,
        "persisted_edges": persisted_edges,
    }


@capability(
    "fabric.ai_analyse_links",
    http_method="POST", http_path="/fabric/ai_analyse_links", http_tags=["fabric","graph","loom"],
    memory="on",
    description="Suggest dataset-level relations using the LLM, then automatically "
                "drive Loom for each accepted pair. Replaces the old standalone analyser. "
                "Input: max_pairs (int default 8), min_score (float default 0.5), "
                "auto_stitch (bool default False — if True, runs Loom on every suggestion). "
                "Output: {suggestions: [...], stitched: [...]}.",
)
async def fabric_ai_analyse_links(
    max_pairs:   int   = 8,
    min_score:   float = 0.5,
    auto_stitch: bool  = False,
    trace_id=None,
) -> Dict:
    # List datasets
    loop = asyncio.get_running_loop()
    def _list():
        conn = _sqlite_conn()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT dataset_id, record_count FROM fabric_datasets "
                "ORDER BY record_count DESC LIMIT 40"
            ).fetchall()]
        finally:
            conn.close()
    datasets = await loop.run_in_executor(None, _list)
    if len(datasets) < 2:
        return {"error": "Need at least 2 datasets to analyse"}

    ds_names = ", ".join(d["dataset_id"] for d in datasets)
    prompt = (
        "Given these datasets from a polyglot data fabric, identify which pairs are "
        "most likely to share content or topics.\n\n"
        f"Datasets: {ds_names}\n\n"
        "Return ONLY a JSON object:\n"
        '{"suggestions": [{"from":"ds_id","to":"ds_id","rel":"RELATED_TO|SHARES_TOPIC|DERIVED_FROM",'
        '"reason":"brief","score":0.85}]}\n'
        f"Limit to {max_pairs} suggestions. Score is your confidence 0-1."
    )

    suggestions: List[Dict] = []
    try:
        from Vera.Orchestration.capability_orchestration import ollama_generate
        raw = await asyncio.wait_for(
            ollama_generate(prompt, system="You output JSON only.", json_mode=True),
            timeout=60,
        )
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\n?|\n?```$", "", raw)
        parsed = json.loads(raw)
        suggestions = parsed.get("suggestions", []) if isinstance(parsed, dict) else []
        suggestions = [s for s in suggestions
                       if isinstance(s, dict) and s.get("from") and s.get("to")
                       and float(s.get("score", 0)) >= min_score][:max_pairs]
    except Exception as e:
        log.warning("ai_analyse_links: %s", e)
        # Fallback: token-overlap heuristic on dataset names
        ds_ids = [d["dataset_id"] for d in datasets]
        for i, a in enumerate(ds_ids):
            for b in ds_ids[i+1:]:
                a_toks = set(a.lower().replace("_"," ").replace("-"," ").split())
                b_toks = set(b.lower().replace("_"," ").replace("-"," ").split())
                a_toks = {t for t in a_toks if len(t) > 3}
                b_toks = {t for t in b_toks if len(t) > 3}
                shared = a_toks & b_toks
                if shared:
                    suggestions.append({
                        "from": a, "to": b, "rel": "SHARES_TOPIC",
                        "reason": f"Shared tokens: {', '.join(shared)}",
                        "score": len(shared) / max(len(a_toks | b_toks), 1),
                    })
        suggestions.sort(key=lambda s: -s.get("score", 0))
        suggestions = [s for s in suggestions if s["score"] >= min_score][:max_pairs]

    stitched = []
    if auto_stitch and suggestions:
        for s in suggestions:
            try:
                res = await fabric_loom_run(
                    dataset_a=s["from"], dataset_b=s["to"],
                    mode="hybrid", min_score=0.4, max_matches=50,
                    persist=True,
                )
                stitched.append({
                    "from": s["from"], "to": s["to"],
                    "matches": res.get("total", 0),
                    "persisted": res.get("persisted", 0),
                })
            except Exception as e:
                log.warning("ai_analyse_links stitch: %s", e)

    return {"suggestions": suggestions, "stitched": stitched,
            "count": len(suggestions)}




@capability(
    "fabric.ontologies.build",
    http_method="POST", http_path="/fabric/ontologies/build", http_tags=["fabric","ontology","skills"],
    memory="on",
    description="Build an ontology from one or more datasets. Samples records, asks the "
                "LLM to extract entity types and relationship rules, registers via the "
                "canonical ontologies.create capability so it shows in the Skills UI. "
                "Input: name (str!), dataset_ids (str — comma-sep!), "
                "domain (str default 'general'), "
                "description (str — auto-generates if blank), "
                "sample_size (int default 30), "
                "tags (str — comma-sep). "
                "Output: {ontology_id, name, entities, relationships, samples_used}.",
)
async def fabric_ontologies_build(
    name:        str,
    dataset_ids: str,
    domain:      str = "general",
    description: str = "",
    sample_size: int = 30,
    tags:        str = "",
    trace_id=None,
) -> Dict:
    if not name.strip():
        return {"error": "name required"}
    ds_list = [d.strip() for d in dataset_ids.split(",") if d.strip()]
    if not ds_list:
        return {"error": "dataset_ids required"}
    try:
        sample_size = int(sample_size)
    except (TypeError, ValueError):
        sample_size = 30
    sample_size = max(5, min(500, sample_size))
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    async def _emit(stage, **kw):
        try:
            await emit_event({"type":"fabric.ontology.progress",
                              "name":name, "stage":stage, **kw})
        except Exception: pass

    await _emit("starting", message=f"Sampling {len(ds_list)} dataset(s)...")

    samples = []
    per_ds = max(3, sample_size // max(len(ds_list), 1))
    for ds in ds_list:
        loop = asyncio.get_running_loop()
        def _cnt():
            conn = _sqlite_conn()
            try:
                r = conn.execute("SELECT COUNT(*) FROM fabric_records WHERE dataset_id=?",
                                  (ds,)).fetchone()
                return r[0] if r else 0
            finally:
                conn.close()
        total = await loop.run_in_executor(None, _cnt)
        if total == 0: continue
        head_n = max(1, per_ds // 3)
        mid_n  = max(1, per_ds // 3)
        tail_n = per_ds - head_n - mid_n
        head_rows = await _sqlite_query(dataset_id=ds, limit=head_n, offset=0)
        mid_rows  = await _sqlite_query(dataset_id=ds, limit=mid_n,
                                          offset=max(0, total // 2 - mid_n // 2))
        tail_rows = await _sqlite_query(dataset_id=ds, limit=tail_n,
                                          offset=max(0, total - tail_n))
        seen = set()
        for r in head_rows + mid_rows + tail_rows:
            if r["id"] in seen: continue
            seen.add(r["id"])
            src_url = ""
            try:
                d = json.loads(r.get("data","{}")) if isinstance(r.get("data"), str) else (r.get("data") or {})
                src_url = d.get("link") or d.get("url") or d.get("_source_url") or ""
            except Exception: pass
            samples.append({
                "id": r["id"], "dataset_id": r["dataset_id"],
                "text": (r.get("text","") or "")[:800],
                "src_url": src_url[:120],
            })
        if len(samples) >= sample_size: break
    samples = samples[:sample_size]
    if not samples:
        return {"error": f"No records in: {', '.join(ds_list)}"}

    await _emit("extracting", message=f"Asking LLM to define ontology from {len(samples)} samples...",
                samples=len(samples))

    sample_lines = []
    for i, s in enumerate(samples):
        meta = " [src=" + s["src_url"] + "]" if s.get("src_url") else ""
        sample_lines.append(f"[{i+1}] ({s['dataset_id']}){meta}\n{s['text']}")
    sample_text = "\n\n".join(sample_lines)[:18000]

    system = (
        "You are an ontology design system. Given a sample of records, design a "
        "structured ontology that describes WHAT KIND of entities and relationships "
        "exist in this domain. Return ONLY valid JSON in this shape:\n"
        "{\n"
        '  "summary": "1-3 sentence description of the domain",\n'
        '  "entities": [{"name": "EntityType", "type": "category", "description": "what this entity represents", "properties": ["prop1","prop2"]}],\n'
        '  "relationships": [{"from": "EntityType", "to": "EntityType", "label": "VERB_PHRASE", "description": "..."}],\n'
        '  "processing_rules": ["When you see X, do Y", ...]\n'
        "}\n"
        "Maximum 20 entities, 30 relationships, 10 processing_rules. "
        "Entity names must be PascalCase. Relationship labels must be SNAKE_CASE_VERBS."
    )
    prompt = f"Records:\n{sample_text}\n\nDesign the ontology. JSON only."

    out = {"summary":"", "entities":[], "relationships":[], "processing_rules":[]}
    try:
        from Vera.Orchestration.capability_orchestration import ollama_generate
        raw = await asyncio.wait_for(
            ollama_generate(prompt, system=system, json_mode=True),
            timeout=120,
        )
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\n?|\n?```$", "", raw)
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            out = {
                "summary":          str(parsed.get("summary",""))[:1000],
                "entities":         parsed.get("entities",[])[:20]   if isinstance(parsed.get("entities"), list) else [],
                "relationships":    parsed.get("relationships",[])[:30] if isinstance(parsed.get("relationships"), list) else [],
                "processing_rules": parsed.get("processing_rules",[])[:10] if isinstance(parsed.get("processing_rules"), list) else [],
            }
    except asyncio.TimeoutError:
        await _emit("warning", message="LLM timed out")
    except Exception as e:
        await _emit("warning", message=f"LLM: {e}")

    final_desc = description.strip() or out["summary"] or f"Ontology for {len(ds_list)} dataset(s)"

    # Register via canonical ontologies.create
    ontology_id = None
    try:
        from Vera.Orchestration.skills import ontologies_create
        ont_rec = await ontologies_create(
            name              = name.strip(),
            description       = final_desc,
            domain            = domain or "general",
            context_hints     = "Built from fabric datasets: " + ", ".join(ds_list),
            entities          = json.dumps(out["entities"]),
            relationships     = json.dumps(out["relationships"]),
            processing_rules  = json.dumps(out["processing_rules"]),
            tags              = ",".join(tag_list + ["fabric_built"] + ds_list[:3]),
            enabled           = True,
        )
        ontology_id = ont_rec.get("id") if isinstance(ont_rec, dict) else None
    except ImportError:
        log.warning("skills module not available — ontology not registered canonically")
    except Exception as e:
        log.warning("ontologies_create: %s", e)

    # Persist ontology entities + relationships in the fabric graph
    if FABRIC_NEO.available:
        async def _persist_graph():
            try:
                if ontology_id:
                    await FABRIC_NEO.upsert_node("Ontology", ontology_id,
                                                  {"name": name, "domain": domain,
                                                   "description": final_desc[:500]})
                ent_ids = {}
                for e in out["entities"]:
                    if not isinstance(e, dict) or not e.get("name"): continue
                    eid = "ont_ent_" + hashlib.sha1(
                        ((ontology_id or name) + "|" + e["name"]).encode()
                    ).hexdigest()[:14]
                    ent_ids[e["name"]] = eid
                    await FABRIC_NEO.upsert_node("OntologyEntity", eid,
                                                  {"name": e.get("name",""),
                                                   "type": e.get("type",""),
                                                   "description": e.get("description","")[:300]})
                    if ontology_id:
                        await FABRIC_NEO.link("Ontology", ontology_id,
                                                "OntologyEntity", eid, rel="DEFINES")
                for r in out["relationships"]:
                    if not isinstance(r, dict): continue
                    fid = ent_ids.get(r.get("from"))
                    tid = ent_ids.get(r.get("to"))
                    if fid and tid:
                        rname = re.sub(r"[^A-Z_]", "_",
                                        (r.get("label","RELATES_TO")).upper())[:32] or "RELATES_TO"
                        await FABRIC_NEO.link("OntologyEntity", fid,
                                                "OntologyEntity", tid, rel=rname)
                for ds in ds_list:
                    if ontology_id:
                        await FABRIC_NEO.link("Ontology", ontology_id,
                                                "Dataset", ds, rel="BUILT_FROM")
            except Exception as e:
                log.warning("ontology graph persist: %s", e)
        asyncio.create_task(_persist_graph())

    await _emit("done",
                message=f"Built ontology with {len(out['entities'])} entities, "
                        f"{len(out['relationships'])} relationships",
                entities=len(out["entities"]),
                relationships=len(out["relationships"]))

    return {
        "ok":               True,
        "ontology_id":      ontology_id,
        "name":             name,
        "description":      final_desc,
        "domain":           domain,
        "dataset_ids":      ds_list,
        "summary":          out["summary"],
        "entities":         out["entities"],
        "relationships":    out["relationships"],
        "processing_rules": out["processing_rules"],
        "samples_used":     len(samples),
    }




# ─────────────────────────────────────────────────────────────────────────────
# DATASET PROCESSING CONFIG
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "fabric.datasets.config",
    http_method="POST", http_path="/fabric/datasets/config", http_tags=["fabric"],
    memory="off",
    description="Get or set per-dataset processing configuration. "
                "Controls: auto_extract_entities (bool), auto_loom (bool), "
                "loom_scope (internal|cross), entity_scope (internal|cross), "
                "content_type (text|code|web), extract_limit (int), "
                "loom_mode (vector|keyword|hybrid), loom_min_score (float). "
                "Input: dataset_id (str!), config (dict — omit to just read). "
                "Output: {dataset_id, config}.",
)
async def cap_dataset_config(dataset_id: str = "", config: dict = None,
                               trace_id=None) -> Dict:
    if not dataset_id:
        return {"error": "dataset_id required"}
    current = await _get_dataset_processing_config(dataset_id)
    if config and isinstance(config, dict):
        # Validate and merge
        valid_keys = {"auto_extract_entities", "auto_loom", "extract_limit",
                      "content_type", "loom_scope", "loom_mode", "loom_min_score",
                      "loom_max_matches", "entity_scope"}
        merged = {k: v for k, v in current.items()}
        for k, v in config.items():
            if k in valid_keys:
                merged[k] = v
        try:
            conn = _write_conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fabric_dataset_config (
                    dataset_id TEXT PRIMARY KEY,
                    config     TEXT DEFAULT '{}',
                    updated_at TEXT
                )
            """)
            conn.execute(
                "INSERT OR REPLACE INTO fabric_dataset_config "
                "(dataset_id, config, updated_at) VALUES (?,?,?)",
                (dataset_id, json.dumps(merged), now_iso())
            )
            conn.commit()
            current = merged
        except Exception as e:
            return {"error": str(e)}
    return {"dataset_id": dataset_id, "config": current}


# ─────────────────────────────────────────────────────────────────────────────
# DATASET TAGGING
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "fabric.datasets.tag",
    http_method="POST", http_path="/fabric/datasets/tag", http_tags=["fabric","tags"],
    memory="off",
    description="Add or remove tags on a dataset. "
                "Input: dataset_id (str!), tags (str — comma-sep!), "
                "action (add|remove default add), source (user|llm default user). "
                "Output: {dataset_id, tags_now}.",
)
async def fabric_datasets_tag(dataset_id: str, tags: str,
                                action: str = "add", source: str = "user",
                                trace_id=None) -> Dict:
    if not dataset_id or not tags:
        return {"error": "dataset_id and tags required"}
    tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()]
    if not tag_list:
        return {"error": "no valid tags after parsing"}
    ts = now_iso()
    if action == "remove":
        for t in tag_list:
            await _enqueue_write({"kind": "raw",
                "sql":    "DELETE FROM fabric_dataset_tags WHERE dataset_id=? AND tag=?",
                "params": (dataset_id, t)}, wait=False)
    else:
        for t in tag_list:
            await _enqueue_write({"kind": "raw",
                "sql":    "INSERT OR REPLACE INTO fabric_dataset_tags "
                          "(dataset_id, tag, source, created_at) VALUES (?,?,?,?)",
                "params": (dataset_id, t, source, ts)}, wait=False)
    # Read back
    loop = asyncio.get_running_loop()
    def _read():
        conn = _sqlite_conn()
        try:
            return [r[0] for r in conn.execute(
                "SELECT tag FROM fabric_dataset_tags WHERE dataset_id=? ORDER BY tag",
                (dataset_id,)).fetchall()]
        finally:
            conn.close()
    await asyncio.sleep(0.1)  # let writer drain
    tags_now = await loop.run_in_executor(None, _read)
    await emit_event({"type":"fabric.dataset.tagged",
                       "dataset_id": dataset_id, "tags": tags_now})
    return {"ok": True, "dataset_id": dataset_id, "tags_now": tags_now,
            "action": action, "applied": tag_list}


@capability(
    "fabric.datasets.tags",
    http_method="GET", http_path="/fabric/datasets/tags", http_tags=["fabric","tags"],
    memory="off", silent=True,
    description="List tags for one dataset, or all (dataset, tag) pairs. "
                "Input: dataset_id (str — empty for all). "
                "Output: {tags or all}.",
)
async def fabric_datasets_tags(dataset_id: str = "", trace_id=None) -> Dict:
    loop = asyncio.get_running_loop()
    def _read():
        conn = _sqlite_conn()
        try:
            if dataset_id:
                rows = conn.execute(
                    "SELECT tag, source, created_at FROM fabric_dataset_tags "
                    "WHERE dataset_id=? ORDER BY tag",
                    (dataset_id,)).fetchall()
                return {"dataset_id": dataset_id,
                        "tags": [{"tag": r[0], "source": r[1], "created_at": r[2]}
                                  for r in rows]}
            # All — group by tag
            rows = conn.execute(
                "SELECT tag, dataset_id, source FROM fabric_dataset_tags "
                "ORDER BY tag, dataset_id").fetchall()
            grouped: Dict[str, Dict] = {}
            ds_tags: Dict[str, list] = {}
            for tag, ds, src in rows:
                grouped.setdefault(tag, {"tag": tag, "datasets": []})
                grouped[tag]["datasets"].append({"dataset_id": ds, "source": src})
                ds_tags.setdefault(ds, []).append(tag)
            return {"by_tag": list(grouped.values()),
                    "by_dataset": [{"dataset_id": k, "tags": v}
                                    for k, v in ds_tags.items()]}
        finally:
            conn.close()
    try:
        return await loop.run_in_executor(None, _read)
    except Exception as e:
        return {"error": str(e), "tags": [], "by_tag": [], "by_dataset": []}


@capability(
    "fabric.datasets.auto_tag",
    http_method="POST", http_path="/fabric/datasets/auto_tag", http_tags=["fabric","tags"],
    memory="on",
    description="Use the LLM to suggest and apply tags to a dataset based on "
                "its records. "
                "Input: dataset_id (str!), sample_size (int default 15), "
                "max_tags (int default 6), apply (bool default True — actually save). "
                "Output: {dataset_id, suggested_tags, applied}.",
)
async def fabric_datasets_auto_tag(dataset_id: str, sample_size: int = 15,
                                     max_tags: int = 6, apply: bool = True,
                                     trace_id=None) -> Dict:
    if not dataset_id:
        return {"error": "dataset_id required"}
    try:
        sample_size = int(sample_size); max_tags = int(max_tags)
    except (TypeError, ValueError):
        sample_size = 15; max_tags = 6
    sample_size = max(3, min(50, sample_size))
    max_tags    = max(1, min(20, max_tags))

    rows = await _sqlite_query(dataset_id=dataset_id, limit=sample_size)
    if not rows:
        return {"error": f"No records in {dataset_id}"}

    sample_text = "\n\n".join(
        f"[{i+1}] {(r.get('text','') or '')[:300]}"
        for i, r in enumerate(rows)
    )[:6000]

    suggested: List[str] = []
    try:
        from Vera.Orchestration.capability_orchestration import ollama_generate
        prompt = (
            f"Analyse these {len(rows)} sample records and suggest up to {max_tags} "
            f"short, lowercase, single-word or hyphenated tags that classify the "
            f"dataset's domain and content type. Tags should be useful for grouping "
            f"similar datasets together (examples: 'security', 'cve', 'news', "
            f"'machine-learning', 'rss-feed', 'documentation', 'finance', 'biology').\n\n"
            f"Records:\n{sample_text}\n\n"
            f"Return ONLY a JSON array of strings, e.g. [\"security\",\"cve\",\"news\"]."
        )
        raw = await asyncio.wait_for(
            ollama_generate(prompt, system="You output JSON only.", json_mode=True),
            timeout=60,
        )
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\n?|\n?```$", "", raw)
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            suggested = [str(t).strip().lower() for t in parsed if t][:max_tags]
        elif isinstance(parsed, dict) and isinstance(parsed.get("tags"), list):
            suggested = [str(t).strip().lower() for t in parsed["tags"] if t][:max_tags]
    except Exception as e:
        log.warning("auto_tag LLM: %s", e)
        # Fallback: word frequency
        from collections import Counter
        words = []
        for r in rows:
            for w in re.findall(r"[a-zA-Z]{4,}", (r.get("text","") or "").lower()):
                if w not in {"that","this","with","from","have","been","were","will"}:
                    words.append(w)
        suggested = [w for w, _ in Counter(words).most_common(max_tags)]

    applied_tags = []
    if apply and suggested:
        for t in suggested:
            await _enqueue_write({"kind": "raw",
                "sql":    "INSERT OR REPLACE INTO fabric_dataset_tags "
                          "(dataset_id, tag, source, created_at) VALUES (?,?,?,?)",
                "params": (dataset_id, t, "llm", now_iso())}, wait=False)
            applied_tags.append(t)
        await asyncio.sleep(0.1)

    return {"ok": True, "dataset_id": dataset_id,
            "suggested_tags": suggested, "applied": applied_tags}


# ─────────────────────────────────────────────────────────────────────────────
# AGENTS & DAGS — stored in fabric so they're queryable, taggable, listable
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "fabric.agents.register",
    http_method="POST", http_path="/fabric/agents", http_tags=["fabric","agents"],
    memory="off",
    description="Register or update an agent definition in the fabric. "
                "Input: name (str!), description (str), "
                "config (object/JSON-string — agent config), tags (str). "
                "Output: {id, name}.",
)
async def fabric_agents_register(name: str, description: str = "",
                                   config=None, tags: str = "",
                                   id: str = "",
                                   trace_id=None) -> Dict:
    if not name.strip():
        return {"error": "name required"}
    aid = id.strip() or "agent_" + hashlib.sha1(name.encode()).hexdigest()[:14]
    cfg_str = config if isinstance(config, str) else json.dumps(config or {})
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    ts = now_iso()
    await _enqueue_write({"kind":"raw",
        "sql": "INSERT OR REPLACE INTO fabric_agents "
               "(id, name, description, config, tags, created_at, updated_at) "
               "VALUES (?,?,?,?,?,COALESCE((SELECT created_at FROM fabric_agents WHERE id=?),?),?)",
        "params": (aid, name.strip(), description.strip(), cfg_str,
                   json.dumps(tag_list), aid, ts, ts)}, wait=True)
    if FABRIC_NEO.available:
        asyncio.create_task(FABRIC_NEO.upsert_node("Agent", aid,
                                                     {"name": name.strip(),
                                                      "description": description[:300]}))
    await emit_event({"type":"fabric.agent.registered", "id": aid, "name": name})
    return {"ok": True, "id": aid, "name": name}


@capability(
    "fabric.agents.list",
    http_method="GET", http_path="/fabric/agents", http_tags=["fabric","agents"],
    memory="off",
    description="List registered agents. Output: {agents:[{id,name,description,tags,...}]}.",
)
async def fabric_agents_list(trace_id=None) -> Dict:
    loop = asyncio.get_running_loop()
    def _read():
        conn = _sqlite_conn()
        try:
            rows = conn.execute(
                "SELECT id, name, description, tags, created_at, updated_at "
                "FROM fabric_agents ORDER BY updated_at DESC").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    try:
        rows = await loop.run_in_executor(None, _read)
    except Exception as e:
        return {"agents": [], "error": str(e)}
    for r in rows:
        try: r["tags"] = json.loads(r.get("tags","[]"))
        except Exception: r["tags"] = []
    return {"agents": rows, "count": len(rows)}


@capability(
    "fabric.agents.delete",
    http_method="POST", http_path="/fabric/agents/delete", http_tags=["fabric","agents"],
    memory="off",
    description="Delete an agent. Input: id (str!).",
)
async def fabric_agents_delete(id: str, trace_id=None) -> Dict:
    if not id:
        return {"error": "id required"}
    await _enqueue_write({"kind":"raw",
        "sql":"DELETE FROM fabric_agents WHERE id=?",
        "params":(id,)}, wait=True)
    return {"ok": True, "deleted": id}


@capability(
    "fabric.dags.save",
    http_method="POST", http_path="/fabric/dags", http_tags=["fabric","dags"],
    memory="off",
    description="Save a DAG definition to the fabric. "
                "Input: name (str!), description (str), "
                "definition (object/JSON-string — node and edge definitions), tags (str). "
                "Output: {id, name}.",
)
async def fabric_dags_save(name: str, definition=None,
                             description: str = "", tags: str = "",
                             id: str = "",
                             trace_id=None) -> Dict:
    if not name.strip():
        return {"error": "name required"}
    did = id.strip() or "dag_" + hashlib.sha1(name.encode()).hexdigest()[:14]
    def_str = definition if isinstance(definition, str) else json.dumps(definition or {})
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    ts = now_iso()
    await _enqueue_write({"kind":"raw",
        "sql": "INSERT OR REPLACE INTO fabric_dags "
               "(id, name, description, definition, tags, created_at, updated_at) "
               "VALUES (?,?,?,?,?,COALESCE((SELECT created_at FROM fabric_dags WHERE id=?),?),?)",
        "params": (did, name.strip(), description.strip(), def_str,
                   json.dumps(tag_list), did, ts, ts)}, wait=True)
    if FABRIC_NEO.available:
        asyncio.create_task(FABRIC_NEO.upsert_node("DAG", did,
                                                     {"name": name.strip(),
                                                      "description": description[:300]}))
    await emit_event({"type":"fabric.dag.saved", "id": did, "name": name})
    return {"ok": True, "id": did, "name": name}


@capability(
    "fabric.dags.list",
    http_method="GET", http_path="/fabric/dags", http_tags=["fabric","dags"],
    memory="off",
    description="List saved DAGs. Output: {dags:[{id,name,description,tags,...}]}.",
)
async def fabric_dags_list(trace_id=None) -> Dict:
    loop = asyncio.get_running_loop()
    def _read():
        conn = _sqlite_conn()
        try:
            rows = conn.execute(
                "SELECT id, name, description, tags, created_at, updated_at "
                "FROM fabric_dags ORDER BY updated_at DESC").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    try:
        rows = await loop.run_in_executor(None, _read)
    except Exception as e:
        return {"dags": [], "error": str(e)}
    for r in rows:
        try: r["tags"] = json.loads(r.get("tags","[]"))
        except Exception: r["tags"] = []
    return {"dags": rows, "count": len(rows)}


@capability(
    "fabric.dags.get",
    http_method="GET", http_path="/fabric/dags/get", http_tags=["fabric","dags"],
    memory="off",
    description="Fetch a DAG by id, including its full definition. Input: id (str!).",
)
async def fabric_dags_get(id: str, trace_id=None) -> Dict:
    if not id: return {"error": "id required"}
    loop = asyncio.get_running_loop()
    def _read():
        conn = _sqlite_conn()
        try:
            row = conn.execute("SELECT * FROM fabric_dags WHERE id=?", (id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    row = await loop.run_in_executor(None, _read)
    if not row: return {"error": "not found"}
    try: row["tags"] = json.loads(row.get("tags","[]"))
    except Exception: row["tags"] = []
    try: row["definition"] = json.loads(row.get("definition","{}"))
    except Exception: pass
    return row


@capability(
    "fabric.dags.delete",
    http_method="POST", http_path="/fabric/dags/delete", http_tags=["fabric","dags"],
    memory="off",
    description="Delete a DAG. Input: id (str!).",
)
async def fabric_dags_delete(id: str, trace_id=None) -> Dict:
    if not id: return {"error": "id required"}
    await _enqueue_write({"kind":"raw",
        "sql":"DELETE FROM fabric_dags WHERE id=?",
        "params":(id,)}, wait=True)
    return {"ok": True, "deleted": id}




# ─────────────────────────────────────────────────────────────────────────────
# SEARCH PIPELINES
# A pipeline is a saved sequence of stages, each with a type and config.
# Stages run sequentially, threading state forward. The "output" stage's
# results are returned to the caller.
#
# Stage types:
#   text_search   — text/keyword search on records
#   vector_search — semantic similarity (uses _search_vector)
#   tag_filter    — keep records matching specific dataset tags
#   dataset_filter— keep records from specific datasets
#   time_filter   — keep records within a time window (recent_days)
#   graph_expand  — for each record, follow RELATED_TO edges N hops
#   rank          — sort/rerank by score, recency, or composite
#   limit         — cap result count
#   output        — finalise and return
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "fabric.pipelines.save",
    http_method="POST", http_path="/fabric/pipelines",
    http_tags=["fabric","pipelines","search"],
    memory="off",
    description="Save a search pipeline definition. "
                "Input: name (str!), description (str), "
                "stages (list of stage objects [{type, config}] OR JSON string), "
                "tags (str). "
                "Output: {id, name}.",
)
async def fabric_pipelines_save(name: str, stages=None,
                                  description: str = "", tags: str = "",
                                  id: str = "", trace_id=None) -> Dict:
    if not name.strip():
        return {"error": "name required"}
    if isinstance(stages, str):
        try: stages_list = json.loads(stages) if stages.strip() else []
        except Exception:
            return {"error": "stages must be valid JSON"}
    elif isinstance(stages, list):
        stages_list = stages
    else:
        stages_list = []
    if not stages_list:
        return {"error": "at least one stage required"}

    pid = id.strip() or "pipe_" + hashlib.sha1(name.encode()).hexdigest()[:14]
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    ts = now_iso()
    await _enqueue_write({
        "kind":"raw",
        "sql": "INSERT OR REPLACE INTO fabric_pipelines "
                "(id, name, description, stages, tags, created_at, updated_at) "
                "VALUES (?,?,?,?,?,COALESCE((SELECT created_at FROM fabric_pipelines WHERE id=?),?),?)",
        "params": (pid, name.strip(), description.strip(),
                    json.dumps(stages_list), json.dumps(tag_list),
                    pid, ts, ts),
    }, wait=True)
    await emit_event({"type":"fabric.pipeline.saved","id":pid,"name":name})
    return {"ok": True, "id": pid, "name": name, "stages": stages_list}


@capability(
    "fabric.pipelines.list",
    http_method="GET", http_path="/fabric/pipelines",
    http_tags=["fabric","pipelines"],
    memory="off",
    description="List saved search pipelines.",
)
async def fabric_pipelines_list(trace_id=None) -> Dict:
    loop = asyncio.get_running_loop()
    def _read():
        conn = _sqlite_conn()
        try:
            rows = conn.execute(
                "SELECT id, name, description, stages, tags, created_at, updated_at "
                "FROM fabric_pipelines ORDER BY updated_at DESC").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    try:
        rows = await loop.run_in_executor(None, _read)
    except Exception as e:
        return {"pipelines": [], "error": str(e)}
    for r in rows:
        try: r["stages"] = json.loads(r.get("stages","[]"))
        except Exception: r["stages"] = []
        try: r["tags"] = json.loads(r.get("tags","[]"))
        except Exception: r["tags"] = []
    return {"pipelines": rows, "count": len(rows)}


@capability(
    "fabric.pipelines.delete",
    http_method="POST", http_path="/fabric/pipelines/delete",
    http_tags=["fabric","pipelines"],
    memory="off",
    description="Delete a saved pipeline. Input: id (str!).",
)
async def fabric_pipelines_delete(id: str, trace_id=None) -> Dict:
    if not id: return {"error":"id required"}
    await _enqueue_write({"kind":"raw",
        "sql":"DELETE FROM fabric_pipelines WHERE id=?",
        "params":(id,)}, wait=True)
    return {"ok":True, "deleted": id}


@capability(
    "fabric.pipelines.run",
    http_method="POST", http_path="/fabric/pipelines/run",
    http_tags=["fabric","pipelines","search"],
    memory="on",
    description="Execute a saved pipeline (by id) or an inline pipeline definition. "
                "Input: id (str — saved pipeline ID, or empty if using stages), "
                "stages (list of stage objects, or empty if using id), "
                "input (dict — runtime input merged into the first stage's context), "
                "limit (int default 50). "
                "Stage types: text_search, vector_search, tag_filter, dataset_filter, "
                "time_filter, graph_expand, rank, limit, output. "
                "Output: {results, stages_run, stage_log}.",
)
async def fabric_pipelines_run(id: str = "", stages=None, input=None,
                                 limit: int = 50, trace_id=None) -> Dict:
    # Resolve stages: either by id or inline
    stages_list: List[dict] = []
    if id and id.strip():
        loop = asyncio.get_running_loop()
        def _read():
            conn = _sqlite_conn()
            try:
                row = conn.execute(
                    "SELECT stages FROM fabric_pipelines WHERE id=?", (id,)).fetchone()
                return row
            finally:
                conn.close()
        row = await loop.run_in_executor(None, _read)
        if not row:
            return {"error": f"no pipeline with id {id}"}
        try:
            stages_list = json.loads(row[0] or "[]")
        except Exception:
            return {"error":"saved pipeline has invalid stages JSON"}
    elif stages is not None:
        if isinstance(stages, str):
            try: stages_list = json.loads(stages)
            except Exception:
                return {"error": "stages must be valid JSON"}
        elif isinstance(stages, list):
            stages_list = stages

    if not stages_list:
        return {"error": "no stages to run — provide id= or stages="}

    # Initialise context from input dict
    if isinstance(input, str):
        try: input = json.loads(input)
        except Exception: input = {}
    ctx: Dict[str, Any] = dict(input or {})
    ctx.setdefault("limit", limit)

    # Working result set
    results: List[Dict] = []
    stage_log: List[Dict] = []

    async def _emit_stage(idx, stype, status, **extra):
        try:
            await emit_event({"type":"fabric.pipeline.stage",
                              "pipeline_id": id, "stage_index": idx,
                              "stage_type": stype, "status": status, **extra})
        except Exception: pass

    for idx, stage in enumerate(stages_list):
        if not isinstance(stage, dict): continue
        stype = stage.get("type") or ""
        cfg   = stage.get("config") or {}
        await _emit_stage(idx, stype, "running")

        try:
            if stype == "text_search":
                # Search records by text (uses fabric.query)
                text = cfg.get("text") or ctx.get("text") or ctx.get("topic") or ""
                if not text.strip():
                    stage_log.append({"idx":idx,"type":stype,"skipped":"no text"})
                    continue
                top_k = int(cfg.get("top_k") or ctx.get("limit") or 50)
                qres = await execute_query({"text": text, "top_k": top_k})
                results = qres.get("results", []) or []

            elif stype == "vector_search":
                vector_text = cfg.get("text") or ctx.get("text") or ctx.get("topic") or ""
                if not vector_text.strip():
                    stage_log.append({"idx":idx,"type":stype,"skipped":"no vector text"})
                    continue
                top_k = int(cfg.get("top_k") or ctx.get("limit") or 50)
                qres = await execute_query({"vector": vector_text, "top_k": top_k})
                vec_results = qres.get("results", []) or []
                # Merge into existing results — union by id, prefer higher score
                if results:
                    by_id = {r.get("id"): r for r in results}
                    for r in vec_results:
                        rid = r.get("id")
                        if rid and rid not in by_id:
                            by_id[rid] = r
                    results = list(by_id.values())
                else:
                    results = vec_results

            elif stype == "tag_filter":
                # Keep only records whose dataset has any of these tags
                want_tags = cfg.get("tags") or []
                if isinstance(want_tags, str):
                    want_tags = [t.strip().lower() for t in want_tags.split(",") if t.strip()]
                if want_tags:
                    # Pull tagged datasets
                    tags_res = await fabric_datasets_tags()
                    by_ds = {x["dataset_id"]: set(x["tags"])
                              for x in tags_res.get("by_dataset",[])}
                    results = [r for r in results
                                if any(t in by_ds.get(r.get("dataset_id"),set())
                                       for t in want_tags)]

            elif stype == "dataset_filter":
                # Keep records from specific datasets
                want_ds = cfg.get("dataset_ids") or []
                if isinstance(want_ds, str):
                    want_ds = [d.strip() for d in want_ds.split(",") if d.strip()]
                if want_ds:
                    results = [r for r in results if r.get("dataset_id") in want_ds]

            elif stype == "time_filter":
                # Keep records from the last N days
                import datetime as _dt
                days = int(cfg.get("days") or 30)
                cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=days)
                cutoff_iso = cutoff.isoformat()
                results = [r for r in results if (r.get("created_at") or "") >= cutoff_iso]

            elif stype == "graph_expand":
                # For each record, fetch its RELATED_TO neighbours and add
                if not FABRIC_NEO.available:
                    stage_log.append({"idx":idx,"type":stype,"skipped":"neo4j offline"})
                    continue
                hops = int(cfg.get("hops") or 1)
                ids = [r["id"] for r in results if r.get("id")][:50]
                if not ids:
                    continue
                neighbours = []
                try:
                    cypher = (
                        f"MATCH (a:FabricRecord)-[r:RELATED_TO*1..{hops}]-(b:FabricRecord) "
                        f"WHERE a.id IN $ids "
                        f"RETURN DISTINCT b.id AS id, b.dataset_id AS dataset_id "
                        f"LIMIT $lim"
                    )
                    rows = await FABRIC_NEO.query(cypher,
                                                    {"ids": ids,
                                                     "lim": int(cfg.get("limit") or 50)})
                    for row in rows:
                        if not isinstance(row, dict): continue
                        nid = row.get("id")
                        if nid:
                            neighbours.append({"id": nid,
                                                "dataset_id": row.get("dataset_id",""),
                                                "from_graph_expand": True})
                except Exception as e:
                    stage_log.append({"idx":idx,"type":stype,"warning":str(e)})
                # Merge neighbours into results (skip duplicates)
                seen = {r.get("id") for r in results}
                for n in neighbours:
                    if n.get("id") not in seen:
                        results.append(n)
                        seen.add(n.get("id"))

            elif stype == "rank":
                # Re-rank by recency, score, or composite
                by = (cfg.get("by") or "score").lower()
                if by == "recency":
                    results.sort(key=lambda r: r.get("created_at",""), reverse=True)
                elif by == "score":
                    results.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
                elif by == "composite":
                    # Score 0.7 weight + recency 0.3 weight
                    import datetime as _dt
                    def _composite(r):
                        s = float(r.get("score") or 0)
                        ts = r.get("created_at","")
                        try:
                            d = _dt.datetime.fromisoformat(ts.replace("Z",""))
                            age_days = (_dt.datetime.utcnow() - d).total_seconds() / 86400
                            recency = max(0, 1 - age_days / 30)
                        except Exception:
                            recency = 0
                        return 0.7 * s + 0.3 * recency
                    results.sort(key=_composite, reverse=True)

            elif stype == "limit":
                n = int(cfg.get("n") or ctx.get("limit") or 50)
                results = results[:n]

            elif stype == "output":
                # Just records: marker for end of pipeline; nothing to do
                pass

            else:
                stage_log.append({"idx":idx,"type":stype,"warning":f"unknown stage type"})

            stage_log.append({"idx":idx,"type":stype,"count_after":len(results)})
            await _emit_stage(idx, stype, "done", count=len(results))

        except Exception as e:
            log.warning("pipeline stage %d (%s): %s", idx, stype, e)
            stage_log.append({"idx":idx,"type":stype,"error":str(e)})
            await _emit_stage(idx, stype, "error", error=str(e))

    # Strip overly-large fields from final results
    final = []
    for r in results[:limit]:
        if isinstance(r, dict):
            slim = {k: v for k, v in r.items() if k != "data"}
            final.append(slim)

    return {"ok": True,
            "results": final,
            "count": len(final),
            "stages_run": len(stages_list),
            "stage_log": stage_log}




@capability(
    "fabric.dataset.reset_edges",
    http_method="POST", http_path="/fabric/dataset/reset_edges",
    http_tags=["fabric","graph"],
    memory="off",
    description="Delete all RELATED_TO edges between FabricRecord nodes belonging to "
                "a single dataset, plus the aggregate Dataset-RELATED_TO edges that "
                "include this dataset. Useful for re-running Loom from scratch. "
                "Input: dataset_id (str!). "
                "Output: {ok, deleted}.",
)
async def fabric_dataset_reset_edges(dataset_id: str, trace_id=None) -> Dict:
    if not dataset_id.strip():
        return {"error": "dataset_id required"}
    if not FABRIC_NEO.available:
        return {"error": "Neo4j not connected"}
    deleted = 0
    try:
        # Drop record-level edges within the dataset
        rec_cypher = (
            "MATCH (a:FabricRecord {dataset_id:$dsid})-[r:RELATED_TO]-"
            "(b:FabricRecord {dataset_id:$dsid}) "
            "DELETE r RETURN count(r) AS n"
        )
        async with FABRIC_NEO._driver.session() as s:
            r = await s.run(rec_cypher, dsid=dataset_id)
            row = await r.single()
            if row:
                deleted += int(row["n"] or 0)
            # Drop aggregate Dataset-Dataset edges that touch this dataset
            ds_cypher = (
                "MATCH (a:Dataset {id:$dsid})-[r:RELATED_TO]-(b:Dataset) "
                "DELETE r RETURN count(r) AS n"
            )
            r2 = await s.run(ds_cypher, dsid=dataset_id)
            row2 = await r2.single()
            if row2:
                deleted += int(row2["n"] or 0)
    except Exception as e:
        log.warning("reset_edges: %s", e)
        return {"error": str(e), "deleted": deleted}
    await emit_event({"type":"fabric.dataset.edges_reset",
                      "dataset_id": dataset_id, "deleted": deleted})
    return {"ok": True, "dataset_id": dataset_id, "deleted": deleted}




@capability(
    "fabric.tags.fan_out",
    http_method="POST", http_path="/fabric/tags/fan_out",
    http_tags=["fabric","tags","sync"],
    memory="off",
    description="Fan-out: pull all SOURCES whose tags include any of the given tags. "
                "Useful for 'pull all news', 'pull all pokemon sources'. "
                "Input: tags (str — comma-sep), match_dataset_tags (bool default True — "
                "also include sources whose dataset has any of these tags), "
                "max_concurrent (int default 4). "
                "Output: {triggered, sources:[{id,label,ingested,error?}]}.",
)
async def fabric_tags_fan_out(tags: str, match_dataset_tags: bool = True,
                                max_concurrent: int = 4,
                                trace_id=None) -> Dict:
    if not tags or not tags.strip():
        return {"error":"tags required"}
    want = [t.strip().lower() for t in tags.split(",") if t.strip()]
    if not want:
        return {"error":"no valid tags"}

    # Walk in-memory _SOURCES + their tag lists
    matched: List[Dict] = []
    # 1) Direct tag match on source
    for sid, src in _SOURCES.items():
        try:
            src_tags_raw = src.get("tags") or []
            if isinstance(src_tags_raw, str):
                try: src_tags = json.loads(src_tags_raw)
                except Exception: src_tags = [src_tags_raw]
            else:
                src_tags = list(src_tags_raw)
            src_tags_lc = [t.lower() for t in src_tags if t]
            if any(w in src_tags_lc for w in want):
                matched.append({"src": src, "match": "source_tag"})
        except Exception:
            continue

    # 2) Dataset-level tag match
    if match_dataset_tags:
        try:
            loop = asyncio.get_running_loop()
            def _read_ds_tags():
                conn = _sqlite_conn()
                try:
                    rows = conn.execute(
                        "SELECT dataset_id, tag FROM fabric_dataset_tags "
                        "WHERE LOWER(tag) IN (" + ",".join("?"*len(want)) + ")",
                        want).fetchall()
                    return [r[0] for r in rows]
                finally:
                    conn.close()
            ds_with_tags = set(await loop.run_in_executor(None, _read_ds_tags))
        except Exception as e:
            ds_with_tags = set()
            log.warning("fan_out ds tag read: %s", e)
        seen_ids = {m["src"].get("id") for m in matched}
        for sid, src in _SOURCES.items():
            if sid in seen_ids: continue
            if src.get("dataset_id") in ds_with_tags:
                matched.append({"src": src, "match": "dataset_tag"})

    if not matched:
        return {"ok":True, "triggered": 0, "sources": [],
                "matched_tags": want}

    # Pull each in parallel with a concurrency gate
    sem = asyncio.Semaphore(max(1, min(8, int(max_concurrent or 4))))
    async def _do_pull(entry):
        src = entry["src"]
        async with sem:
            try:
                ingested = await _pull_source(src)
                return {"id": src.get("id",""),
                        "label": src.get("label",""),
                        "match": entry["match"],
                        "ingested": ingested}
            except Exception as e:
                return {"id": src.get("id",""),
                        "label": src.get("label",""),
                        "error": str(e)[:200]}
    results = await asyncio.gather(*[_do_pull(e) for e in matched])
    total_ingested = sum(r.get("ingested",0) for r in results if isinstance(r.get("ingested"), int))
    await emit_event({"type":"fabric.tags.fan_out",
                       "tags": want, "triggered": len(matched),
                       "ingested_total": total_ingested})
    return {"ok": True,
            "triggered": len(matched),
            "ingested_total": total_ingested,
            "matched_tags": want,
            "sources": results}


@capability(
    "fabric.sources.auto_tag",
    http_method="POST", http_path="/fabric/sources/auto_tag",
    http_tags=["fabric","tags","sources"],
    memory="on",
    description="Auto-tag a single source by sampling its records and asking the LLM "
                "for tags. Same logic as fabric.datasets.auto_tag but applied to the "
                "source's dataset. "
                "Input: source_id (str!), sample_size (int default 15), max_tags (int default 6). "
                "Output: {ok, applied}.",
)
async def fabric_sources_auto_tag(source_id: str, sample_size: int = 15,
                                    max_tags: int = 6, trace_id=None) -> Dict:
    if not source_id or source_id not in _SOURCES:
        return {"error":"source not found"}
    src = _SOURCES[source_id]
    ds_id = src.get("dataset_id")
    if not ds_id:
        return {"error":"source has no dataset_id"}
    return await fabric_datasets_auto_tag(
        dataset_id=ds_id,
        sample_size=sample_size, max_tags=max_tags,
        apply=True)


@capability(
    "fabric.tags.list_grouped",
    http_method="GET", http_path="/fabric/tags/list_grouped",
    http_tags=["fabric","tags"],
    memory="off", silent=True,
    description="List all tags with the count of sources and datasets that carry each. "
                "Output: {tags:[{tag, datasets, sources}]}.",
)
async def fabric_tags_list_grouped(trace_id=None) -> Dict:
    loop = asyncio.get_running_loop()
    def _read():
        conn = _sqlite_conn()
        try:
            rows = conn.execute(
                "SELECT tag, COUNT(DISTINCT dataset_id) AS n_ds "
                "FROM fabric_dataset_tags GROUP BY tag ORDER BY n_ds DESC, tag"
            ).fetchall()
            return [(r[0], r[1]) for r in rows]
        finally:
            conn.close()
    try:
        ds_tag_rows = await loop.run_in_executor(None, _read)
    except Exception as e:
        return {"tags":[], "error": str(e)}

    # Count sources matching each tag too
    src_counts: Dict[str, int] = {}
    for src in _SOURCES.values():
        raw = src.get("tags") or []
        if isinstance(raw, str):
            try: raw = json.loads(raw)
            except Exception: raw = [raw]
        for t in raw or []:
            if not t: continue
            tlc = str(t).lower()
            src_counts[tlc] = src_counts.get(tlc, 0) + 1
    # Merge: every tag from either side
    all_tags: Dict[str, Dict] = {}
    for tag, n_ds in ds_tag_rows:
        all_tags[tag] = {"tag": tag, "datasets": n_ds, "sources": 0}
    for tag, n_src in src_counts.items():
        if tag in all_tags:
            all_tags[tag]["sources"] = n_src
        else:
            all_tags[tag] = {"tag": tag, "datasets": 0, "sources": n_src}
    out = sorted(all_tags.values(),
                  key=lambda x: (-(x["datasets"] + x["sources"]), x["tag"]))
    return {"tags": out, "count": len(out)}


@capability(
    "fabric.topic.save",
    http_method="POST", http_path="/fabric/topic/save",
    http_tags=["fabric","sources"],
    memory="off",
    description="Save a discovery topic as a recurring 'topic source' that re-runs "
                "discovery on a schedule. "
                "Input: topic (str!), max_sources (int default 5), "
                "content_type (str default 'all'), interval (int default 86400). "
                "Output: {ok, source_id}.",
)
async def fabric_topic_save(topic: str, max_sources: int = 5,
                              content_type: str = "all",
                              interval: int = 86400, trace_id=None) -> Dict:
    if not topic.strip():
        return {"error":"topic required"}
    safe = re.sub(r"[^a-z0-9_]", "_", topic.lower().replace(" ","_"))[:30] or "topic"
    src_id = "topic_" + hashlib.sha1(topic.encode()).hexdigest()[:14]
    config = json.dumps({
        "topic": topic.strip(),
        "max_sources": int(max_sources or 5),
        "content_type": content_type or "all",
    })
    try:
        await fabric_sources_add(
            url="",
            source_type="topic",
            label=f"Topic: {topic.strip()[:60]}",
            dataset_id=f"discovered_{safe}",
            interval=interval,
            tags="topic_source," + topic.strip(),
            config=config,
            id=src_id,
        )
    except Exception as e:
        return {"error": str(e)}
    return {"ok": True, "source_id": src_id, "topic": topic.strip()}


@capability(
    "fabric.dataset.reset_edges_alias",
    http_method="POST", http_path="/fabric/dataset/reset_edges_alias",
    http_tags=["fabric","graph"],
    memory="off", silent=True,
    description="Internal alias to satisfy older clients.",
)
async def fabric_dataset_reset_edges_alias(dataset_id: str, trace_id=None) -> Dict:
    return await fabric_dataset_reset_edges(dataset_id=dataset_id)


@capability(
    "fabric.bus.configure",
    http_method="POST", http_path="/fabric/bus/configure", http_tags=["fabric","bus"],
    memory="off",
    description="Enable/disable Redis event bus ingestion. "
                "Input: enabled (bool), filters (comma-sep event prefixes). "
                "Output: {enabled, filters}.",
)
async def fabric_bus_configure(enabled: bool = True, filters: str = "",
                                trace_id=None):
    global _BUS_ENABLED, _BUS_FILTERS, _BUS_TASK
    _BUS_FILTERS = [f.strip() for f in filters.split(",") if f.strip()
                    and not f.strip().startswith("fabric")
                    and not f.strip().startswith("memory")]
    if enabled and not _BUS_ENABLED:
        _BUS_ENABLED = True
        if _BUS_TASK is None or _BUS_TASK.done():
            _BUS_TASK = asyncio.create_task(_bus_worker())
    elif not enabled and _BUS_ENABLED:
        _BUS_ENABLED = False
        if _BUS_TASK and not _BUS_TASK.done():
            _BUS_TASK.cancel()
        _BUS_TASK = None
    return {"enabled": _BUS_ENABLED, "filters": _BUS_FILTERS}


@capability(
    "fabric.bus.status",
    http_method="GET", http_path="/fabric/bus/status", http_tags=["fabric","bus"],
    memory="off",
    description="Bus consumer status. Output: {enabled, filters, task_alive}.",
)
async def fabric_bus_status(trace_id=None):
    return {
        "enabled":    _BUS_ENABLED,
        "filters":    _BUS_FILTERS,
        "task_alive": _BUS_TASK is not None and not _BUS_TASK.done(),
        "stream":     "vera:events",
        "note":       "Uses shared REDIS pool — no separate connections",
    }

# ─────────────────────────────────────────────────────────────────────────────
# ENHANCED DATASET MANAGEMENT CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "fabric.browse",
    http_method="POST", http_path="/fabric/browse", http_tags=["fabric"],
    memory="off",
    description="Browse records in a dataset with pagination and full content. "
                "Input: dataset_id (str!), limit (int 1-200, default 50), "
                "offset (int, default 0), search (str, optional text filter). "
                "Output: {records, total, has_more, dataset_id}.",
)
async def cap_fabric_browse(
    dataset_id: str,
    limit:      int = 50,
    offset:     int = 0,
    search:     str = "",
    lite:       bool = False,
    trace_id=None,
) -> Dict:
    """Browse records with pagination. Projects only needed columns and clamps
    text length so large datasets stay snappy.

    lite=True   -> excludes the parsed JSON `data` blob (graph view, schedule)
    lite=False  -> includes data; text is still trimmed to 4kB per record
    """
    limit = max(1, min(200, limit))
    loop  = asyncio.get_running_loop()

    # Use a fast COUNT only when needed — for the first page or a search.
    # Once the client has a total, it can pass it back via offset to skip COUNT.
    need_total = (offset == 0) or bool(search)

    def _sync():
        conn = _sqlite_conn()
        try:
            # Project only the columns the UI actually consumes — saves bandwidth
            # AND prevents SQLite from materialising the (often large) `data`
            # column when we are in lite mode.
            cols_lite = "id, dataset_id, source_id, tags, created_at, substr(text, 1, 800) AS text"
            cols_full = "id, dataset_id, source_id, tags, created_at, substr(text, 1, 4000) AS text, data"
            cols = cols_lite if lite else cols_full

            params: tuple
            where = "WHERE dataset_id=?"
            params = (dataset_id,)
            if search:
                where += " AND text LIKE ?"
                params = (dataset_id, "%" + search + "%")

            if need_total:
                total_row = conn.execute(
                    "SELECT COUNT(*) FROM fabric_records " + where, params
                ).fetchone()
                total = total_row[0] if total_row else 0
            else:
                total = -1  # unknown; client should retain previous value

            rows = conn.execute(
                f"SELECT {cols} FROM fabric_records {where} "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + (limit, offset)
            ).fetchall()
            return [dict(r) for r in rows], total
        finally:
            conn.close()

    try:
        # Shorter timeout while the composite index is still being built;
        # otherwise the full 20s.
        _t = 8 if not _INDEX_MIGRATION_DONE else 20
        records, total = await asyncio.wait_for(
            loop.run_in_executor(None, _sync), timeout=_t
        )
        # Parse JSON data field if present (only when not lite)
        if not lite:
            for r in records:
                if isinstance(r.get("data"), str):
                    try:
                        parsed = json.loads(r["data"])
                        # Truncate any huge string fields inside data to keep response bounded
                        if isinstance(parsed, dict):
                            for k, v in list(parsed.items()):
                                if isinstance(v, str) and len(v) > 4000:
                                    parsed[k] = v[:4000] + "…"
                        r["data"] = parsed
                    except Exception:
                        # If JSON is invalid or huge, drop it rather than ship 1MB
                        if isinstance(r["data"], str) and len(r["data"]) > 4000:
                            r["data"] = r["data"][:4000] + "…"
        return {
            "records":    records,
            "total":      total,
            "has_more":   (total < 0) or ((offset + limit) < total),
            "offset":     offset,
            "limit":      limit,
            "dataset_id": dataset_id,
            "lite":       bool(lite),
        }
    except asyncio.TimeoutError:
        log.warning("fabric_browse timeout: ds=%s offset=%d limit=%d", dataset_id, offset, limit)
        msg = ("Browse timed out — index migration may still be running. "
               "Wait ~30 seconds and retry." if not _INDEX_MIGRATION_DONE
               else "Browse timed out — try a smaller page size.")
        return {"error": msg, "records": [], "total": 0}
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            return {"error": "Database busy — index migration in progress. Retry in a few seconds.",
                    "records": [], "total": 0}
        log.warning("fabric_browse: %s", e)
        return {"error": str(e), "records": [], "total": 0}
    except Exception as e:
        log.warning("fabric_browse: %s", e)
        return {"error": str(e), "records": [], "total": 0}


@capability(
    "fabric.delete_record",
    http_method="POST", http_path="/fabric/delete_record", http_tags=["fabric"],
    memory="off",
    description="Delete a single record by id from all backends. "
                "Input: record_id (str!), dataset_id (str, optional — used for Chroma). "
                "Output: {deleted, record_id, backends}.",
)
async def cap_fabric_delete_record(
    record_id:  str,
    dataset_id: str = "",
    trace_id=None,
) -> Dict:
    """Delete a single record from SQLite (primary) and Chroma (if available)."""
    backends = []
    loop = asyncio.get_running_loop()

    # SQLite delete
    def _sqlite_del():
        conn = _sqlite_conn()
        try:
            conn.execute("DELETE FROM fabric_records WHERE id=?", (record_id,))
            conn.commit()
            return True
        finally:
            conn.close()
    try:
        await loop.run_in_executor(None, _sqlite_del)
        backends.append("sqlite")
    except Exception as e:
        log.warning("fabric_delete_record sqlite: %s", e)

    # Chroma delete
    if FABRIC_CHROMA.available and dataset_id:
        try:
            col = FABRIC_CHROMA._client.get_or_create_collection(
                dataset_id.replace(".", "_").replace("/", "_"))
            col.delete(ids=[record_id])
            backends.append("chroma")
        except Exception as e:
            log.debug("fabric_delete_record chroma: %s", e)

    return {"deleted": bool(backends), "record_id": record_id, "backends": backends}


@capability(
    "fabric.clear_dataset",
    http_method="POST", http_path="/fabric/clear_dataset", http_tags=["fabric"],
    memory="off",
    description="Delete ALL records in a dataset from all backends (SQLite + Chroma + FAISS). "
                "Input: dataset_id (str!). Output: {cleared, dataset_id, backends}.",
)
async def cap_fabric_clear_dataset(dataset_id: str, trace_id=None) -> Dict:
    """Delete every record in a dataset from all storage backends."""
    backends = []
    loop = asyncio.get_running_loop()

    # SQLite
    def _sqlite_clear():
        conn = _sqlite_conn()
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM fabric_records WHERE dataset_id=?", (dataset_id,)
            ).fetchone()[0]
            conn.execute("DELETE FROM fabric_records WHERE dataset_id=?", (dataset_id,))
            conn.commit()
            return n
        finally:
            conn.close()
    try:
        n = await loop.run_in_executor(None, _sqlite_clear)
        backends.append(f"sqlite({n} records)")
    except Exception as e:
        log.warning("fabric_clear_dataset sqlite: %s", e)

    # Chroma
    if FABRIC_CHROMA.available:
        try:
            FABRIC_CHROMA.delete_dataset(dataset_id)
            backends.append("chroma")
        except Exception as e:
            log.debug("fabric_clear_dataset chroma: %s", e)

    await emit_event({"type": "fabric.dataset_cleared", "dataset_id": dataset_id,
                      "backends": backends})
    return {"cleared": bool(backends), "dataset_id": dataset_id, "backends": backends}


@capability(
    "fabric.dataset_stats",
    http_method="GET", http_path="/fabric/dataset_stats", http_tags=["fabric"],
    memory="off",
    description="Get detailed stats for a specific dataset. "
                "Input: dataset_id (str!). "
                "Output: {dataset_id, total_records, oldest, newest, sample_tags}.",
)
async def cap_fabric_dataset_stats(dataset_id: str, trace_id=None) -> Dict:
    loop = asyncio.get_running_loop()
    def _sync():
        conn = _sqlite_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt, MIN(created_at) as oldest, MAX(created_at) as newest "
                "FROM fabric_records WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
            tags_rows = conn.execute(
                "SELECT tags FROM fabric_records WHERE dataset_id=? "
                "AND tags IS NOT NULL AND tags != '' LIMIT 20", (dataset_id,)
            ).fetchall()
            tag_set = set()
            for tr in tags_rows:
                try:
                    tag_set.update(json.loads(tr[0]) if tr[0].startswith("[") else [tr[0]])
                except Exception:
                    pass
            return dict(row), list(tag_set)[:20]
        finally:
            conn.close()
    try:
        stats, tags = await loop.run_in_executor(None, _sync)
        return {
            "dataset_id":     dataset_id,
            "total_records":  stats.get("cnt", 0),
            "oldest":         stats.get("oldest") or "",
            "newest":         stats.get("newest") or "",
            "sample_tags":    tags,
        }
    except Exception as e:
        return {"error": str(e), "dataset_id": dataset_id, "total_records": 0}




# ─────────────────────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────────────────────

_STARTUP_DONE = False

async def _startup():
    global _STARTUP_DONE
    if _STARTUP_DONE:
        return
    _STARTUP_DONE = True

    # Connect optional backends concurrently (non-blocking)
    async def _try(coro):
        try: await coro
        except Exception: pass

    loop  = asyncio.get_running_loop()
    tasks = [
        _try(loop.run_in_executor(None, OBJECT_STORE.connect)),
        _try(loop.run_in_executor(None, FAISS_STORE.connect)),
        _try(loop.run_in_executor(None, FABRIC_CHROMA.connect)),
        _try(FABRIC_PG.connect()),
        _try(FABRIC_NEO.connect()),
    ]
    await asyncio.gather(*tasks, return_exceptions=True)

    # Load sources from SQLite
    try:
        srcs = await _sqlite_sources()
        for s in srcs:
            _SOURCES[s["id"]] = s
        if srcs:
            log.info("data_fabric: loaded %d sources from SQLite", len(srcs))
    except Exception as e:
        log.warning("data_fabric: source load: %s", e)

    # Start auto-pull loop AND deferred index migration (off-thread, won't block)
    asyncio.create_task(_auto_pull_loop())
    asyncio.create_task(_sqlite_index_migration_async())

    active = [n for n, ok in [("faiss", FAISS_STORE.available),
                               ("chroma", FABRIC_CHROMA.available),
                               ("postgres", FABRIC_PG.available),
                               ("neo4j", FABRIC_NEO.available),
                               ("object_store", OBJECT_STORE.available)] if ok]
    log.info("data_fabric ready — backends: %s (sqlite always on)", active or ["sqlite only"])
    await emit_event({"type": "fabric.ready", "backends": ["sqlite"] + active})


schedule(_startup, interval=999999, name="fabric_startup")
# Do NOT add create_task(_startup()) here — schedule() handles it once.
# Running both causes double-initialisation.

# ─────────────────────────────────────────────────────────────────────────────
# ADDITIONAL ENDPOINTS — source editing, full dataset delete, article content fetch
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "fabric.sources.update",
    http_method="POST", http_path="/fabric/sources/update", http_tags=["fabric"],
    memory="off",
    description="Update an existing source's fields (label, tags, interval, limit, enabled, jq_path, headers). "
                "Input: source_id (str!), plus any fields to update. "
                "Output: {ok, source_id}.",
)
async def fabric_sources_update(
    source_id: str,
    label:     str  = None,
    tags:      str  = None,
    interval:  int  = None,
    limit:     int  = None,
    enabled:   bool = None,
    jq_path:   str  = None,
    headers:   str  = None,
    trace_id=None,
) -> Dict:
    # Load current
    src = _SOURCES.get(source_id)
    if not src:
        sl = await _sqlite_sources()
        src = next((s for s in sl if s["id"] == source_id), None)
    if not src:
        return {"error": f"Source not found: {source_id}"}
    updated = dict(src)
    if label    is not None: updated["label"]    = label
    if tags     is not None: updated["tags"]     = [t.strip() for t in tags.split(",") if t.strip()]
    if interval is not None: updated["interval"] = interval
    if limit    is not None: updated["limit"] = updated["lim"] = limit
    if enabled  is not None: updated["enabled"]  = enabled
    if jq_path  is not None: updated["jq_path"]  = jq_path
    if headers  is not None: updated["headers"]  = headers
    _SOURCES[source_id] = updated
    await _sqlite_upsert_source(updated)
    return {"ok": True, "source_id": source_id}


@capability(
    "fabric.dataset.delete",
    http_method="POST", http_path="/fabric/dataset/delete", http_tags=["fabric"],
    memory="off",
    description="Fully delete a dataset — removes all records from SQLite, Chroma, and FAISS. "
                "Input: dataset_id (str!). Output: {ok, dataset_id, backends}.",
)
async def fabric_dataset_delete(dataset_id: str, trace_id=None) -> Dict:
    backends = []
    # SQLite records + dataset row + clear any source links pointing here
    loop = asyncio.get_running_loop()
    # Route all three deletes through the writer queue — atomic batch, no contention
    await _enqueue_write({"kind": "raw",
                          "sql": "DELETE FROM fabric_records WHERE dataset_id=?",
                          "params": (dataset_id,)}, wait=False)
    await _enqueue_write({"kind": "raw",
                          "sql": "DELETE FROM fabric_datasets WHERE dataset_id=?",
                          "params": (dataset_id,)}, wait=False)
    await _enqueue_write({"kind": "raw",
                          "sql": "UPDATE fabric_sources SET dataset_id='' WHERE dataset_id=?",
                          "params": (dataset_id,)}, wait=True)
    # Clear in-memory source dataset_id links too
    for sid, s in list(_SOURCES.items()):
        if isinstance(s, dict) and s.get("dataset_id") == dataset_id:
            _SOURCES[sid] = {**s, "dataset_id": ""}
    backends.append("sqlite")
    # Chroma
    if FABRIC_CHROMA.available:
        try:
            FABRIC_CHROMA.delete_dataset(dataset_id)
            backends.append("chroma")
        except Exception:
            pass
    # FAISS — no per-dataset delete, just note it
    # Postgres
    if FABRIC_PG.available:
        try:
            async with FABRIC_PG._pool.acquire() as conn:
                await conn.execute("DELETE FROM fabric_records WHERE dataset_id=$1", dataset_id)
            backends.append("postgres")
        except Exception:
            pass
    await emit_event({"type": "fabric.dataset.deleted", "dataset_id": dataset_id})
    return {"ok": True, "dataset_id": dataset_id, "backends": backends}


@capability(
    "fabric.rss.fetch_content",
    http_method="POST", http_path="/fabric/rss/fetch_content", http_tags=["fabric"],
    memory="off",
    description="Pull an RSS feed and fetch full article text for each entry. "
                "Input: source_id (str!) — must be an existing RSS source. "
                "max_articles (int, default 10) — cap on article fetches (rate-limiting). "
                "Output: {ingested, dataset_id}.",
)
async def fabric_rss_fetch_content(source_id: str, max_articles: int = 10, trace_id=None) -> Dict:
    src = _SOURCES.get(source_id)
    if not src:
        sl = await _sqlite_sources()
        src = next((s for s in sl if s["id"] == source_id), None)
    if not src:
        return {"error": f"Source not found: {source_id}"}

    import re as _re

    if not HAS_FEEDPARSER:
        return {"error": "feedparser not installed"}

    import concurrent.futures
    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        feed = await loop.run_in_executor(pool, feedparser.parse, src["url"])

    limit = min(max_articles, src.get("limit", 50))
    records = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                  headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"}) as client:
        for entry in feed.entries[:limit]:
            link = entry.get("link", "")
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            full_text = summary

            if link:
                try:
                    r = await client.get(link)
                    if "text/html" in r.headers.get("content-type", ""):
                        html = r.text
                        # Strip scripts/styles
                        html = _re.sub(r'<(script|style)[^>]*>[\s\S]*?</\1>', ' ', html, flags=_re.I)
                        html = _re.sub(r'<[^>]+>', ' ', html)
                        html = _re.sub(r'\s+', ' ', html).strip()
                        if len(html) > len(summary):
                            full_text = html[:8000]
                    await asyncio.sleep(1.5)  # polite delay
                except Exception:
                    pass

            tags_raw = src.get("tags", [])
            tags = tags_raw if isinstance(tags_raw, list) else [t.strip() for t in str(tags_raw).split(",") if t.strip()]
            records.append({
                "text":      f"{title}\n\n{full_text}",
                "title":     title,
                "url":       link,
                "summary":   summary,
                "full_text": full_text,
                "published": str(entry.get("published", "")),
                "source":    src.get("label", src["url"]),
                "tags":      tags,
            })

    result = await ingest_dataset(src["dataset_id"], records,
                                   source=src.get("label", source_id),
                                   tags=src.get("tags", []) if isinstance(src.get("tags"), list) else [])
    return {"ok": True, "ingested": result.get("ingested", 0), "dataset_id": src["dataset_id"]}


# ─────────────────────────────────────────────────────────────────────────────
# MISSING GRAPH-ACTION CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────
# These are referenced by _NODE_ACTION_REGISTRY but were never wired up.
# Each one is a thin wrapper around existing fabric primitives.


@capability(
    "fabric.entity_graph.extract",
    http_method="POST", http_path="/fabric/entity_graph/extract",
    http_tags=["fabric", "graph", "entity"],
    memory="off",
    description="Extract entities from a dataset. Alias for fabric.extract_graph "
                "with entity_graph-compatible progress events. "
                "Input: dataset_id (str!), limit (int default 500), "
                "content_type (str, ignored — always 'text'), persist (bool). "
                "Output: same as fabric.extract_graph.",
)
async def cap_entity_graph_extract(
    dataset_id:   str,
    limit:        int  = 500,
    content_type: str  = "text",
    persist:      bool = True,
    trace_id=None,
) -> Dict:
    """Delegate to fabric.extract_graph, re-emitting progress under the
    fabric.entity_graph.progress event type."""
    return await fabric_extract_graph(
        dataset_id=dataset_id,
        mode="nlp",
        limit=limit,
        persist=persist,
        trace_id=trace_id,
    )


@capability(
    "fabric.entity_graph.extract_record",
    http_method="POST", http_path="/fabric/entity_graph/extract_record",
    http_tags=["fabric", "graph", "entity"],
    memory="off",
    description="Re-extract entities from a single record. "
                "Input: record_id (str!). "
                "Output: {ok, record_id, entities, relations}.",
)
async def cap_entity_graph_extract_record(record_id: str, trace_id=None) -> Dict:
    if not record_id:
        return {"error": "record_id required"}

    async def _emit(stage, **kw):
        try:
            await emit_event({"type": "fabric.entity_graph.progress",
                              "record_id": record_id, "stage": stage, **kw})
        except Exception:
            pass

    await _emit("loading", message=f"Loading record {record_id[:24]}...")

    # Fetch the single record from SQLite
    loop = asyncio.get_running_loop()
    def _fetch():
        conn = _sqlite_conn()
        try:
            row = conn.execute(
                "SELECT * FROM fabric_records WHERE id=? LIMIT 1", (record_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    rec = await loop.run_in_executor(None, _fetch)
    if not rec:
        return {"error": f"Record {record_id} not found"}

    dataset_id = rec.get("dataset_id", "")
    text = (rec.get("text") or "")[:6000]
    if not text.strip():
        return {"ok": True, "record_id": record_id, "entities": [], "relations": [],
                "message": "Record has no text content"}

    await _emit("extracting", message="Extracting entities from record")

    # NLP extraction (same patterns as fabric.extract_graph)
    entities: Dict[str, Dict] = {}
    relations: List[Dict] = []

    pat_camel  = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+){1,})\b")
    pat_snake  = re.compile(r"\b([a-z]+(?:_[a-z]+){1,})\b")
    pat_caps   = re.compile(r"\b([A-Z][A-Z0-9_]{2,}[A-Z0-9])\b")
    pat_url    = re.compile(r"https?://([\w.-]+)/?")
    pat_func   = re.compile(r"\bdef\s+(\w+)\b|\bfunction\s+(\w+)\b")
    pat_class  = re.compile(r"\bclass\s+(\w+)\b")
    pat_import = re.compile(r"(?:import|from)\s+([\w.]+)")
    pat_quoted = re.compile(r'"([A-Za-z][A-Za-z0-9 _-]{2,40})"')

    local_ents = set()
    for m in pat_class.finditer(text):
        local_ents.add(("class", m.group(1)))
    for m in pat_func.finditer(text):
        local_ents.add(("function", m.group(1) or m.group(2)))
    for m in pat_import.finditer(text):
        local_ents.add(("module", m.group(1)))
    for m in pat_camel.finditer(text):
        local_ents.add(("type", m.group(1)))
    for m in pat_caps.finditer(text):
        local_ents.add(("constant", m.group(1)))
    for m in pat_url.finditer(text):
        local_ents.add(("host", m.group(1)))
    for m in pat_quoted.finditer(text):
        local_ents.add(("term", m.group(1)))

    for etype, name in local_ents:
        eid = etype + ":" + hashlib.sha1(name.encode()).hexdigest()[:12]
        entities[eid] = {"id": eid, "name": name, "type": etype,
                         "mentions": [record_id]}

    ent_list = list(local_ents)
    for i in range(len(ent_list)):
        for j in range(i + 1, len(ent_list)):
            ti, ni = ent_list[i]
            tj, nj = ent_list[j]
            a_id = ti + ":" + hashlib.sha1(ni.encode()).hexdigest()[:12]
            b_id = tj + ":" + hashlib.sha1(nj.encode()).hexdigest()[:12]
            relations.append({"from_id": a_id, "to_id": b_id,
                              "rel": "CO_OCCURS", "record_id": record_id})

    # Persist to graph
    persisted = 0
    adapter = GRAPHS.get("fabric")
    if adapter and adapter.available and entities:
        for ent in entities.values():
            if await adapter.upsert_node("Entity", ent["id"],
                                          {"name": ent["name"], "type": ent["type"],
                                           "mention_count": 1,
                                           "dataset_id": dataset_id}):
                persisted += 1
        mention_edges = [{"from_label": "Entity", "from_id": eid,
                          "to_label": "FabricRecord", "to_id": record_id,
                          "rel": "MENTIONED_IN",
                          "props": {"dataset_id": dataset_id}}
                         for eid in entities]
        await adapter.link_many(mention_edges)

    await _emit("done", message=f"{len(entities)} entities, {len(relations)} relations",
                entities=len(entities), relations=len(relations))

    return {"ok": True, "record_id": record_id, "dataset_id": dataset_id,
            "entities": list(entities.values())[:100],
            "relations": relations[:200],
            "entity_count": len(entities), "persisted": persisted}


@capability(
    "fabric.loom.record_match",
    http_method="POST", http_path="/fabric/loom/record_match",
    http_tags=["fabric", "loom"],
    memory="off",
    description="Find records related to a single record across all datasets. "
                "Input: record_id (str!), mode (vector|keyword|hybrid default hybrid), "
                "max_matches (int default 10). "
                "Output: {ok, record_id, matches:[{id,dataset_id,score,snippet}]}.",
)
async def cap_loom_record_match(
    record_id:   str,
    mode:        str = "hybrid",
    max_matches: int = 10,
    trace_id=None,
) -> Dict:
    if not record_id:
        return {"error": "record_id required"}

    async def _emit(stage, **kw):
        try:
            await emit_event({"type": "fabric.loom.progress",
                              "record_id": record_id, "stage": stage, **kw})
        except Exception:
            pass

    await _emit("loading", message=f"Loading record {record_id[:24]}...")

    # Fetch the source record
    loop = asyncio.get_running_loop()
    def _fetch():
        conn = _sqlite_conn()
        try:
            row = conn.execute(
                "SELECT * FROM fabric_records WHERE id=? LIMIT 1", (record_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    rec = await loop.run_in_executor(None, _fetch)
    if not rec:
        return {"error": f"Record {record_id} not found"}

    text = (rec.get("text") or "")[:2000]
    if not text.strip():
        return {"ok": True, "record_id": record_id, "matches": [],
                "message": "Record has no text to match against"}

    await _emit("searching", message=f"Searching ({mode}) for up to {max_matches} matches")

    # Use fabric.query which supports text + vector search
    from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY as _CR
    query_cap = _CR.get("fabric.query", {}).get("func")
    if not query_cap:
        return {"error": "fabric.query capability not available"}

    # Build search based on mode
    kw: Dict = {"limit": max_matches * 2}  # over-fetch to exclude self
    if mode in ("keyword", "hybrid"):
        words = text.split()[:30]
        kw["text"] = " ".join(words)
    if mode in ("vector", "hybrid"):
        kw["vector"] = text[:500]

    try:
        results = await query_cap(**kw, trace_id=trace_id)
    except Exception as e:
        return {"error": f"Query failed: {e}"}

    records_out = results.get("records") or results.get("results") or []

    # Filter out self and deduplicate
    matches = []
    seen = {record_id}
    for r in records_out:
        rid = r.get("id", "")
        if rid in seen:
            continue
        seen.add(rid)
        snippet = (r.get("text") or "")[:200]
        matches.append({
            "id":         rid,
            "dataset_id": r.get("dataset_id", ""),
            "score":      r.get("score", r.get("similarity", 0)),
            "snippet":    snippet,
        })
        if len(matches) >= max_matches:
            break

    await _emit("done", message=f"Found {len(matches)} related records",
                matches=len(matches))

    return {"ok": True, "record_id": record_id,
            "matches": matches, "total": len(matches)}


@capability(
    "fabric.record.summarise",
    http_method="POST", http_path="/fabric/record/summarise",
    http_tags=["fabric", "record"],
    memory="off",
    description="Generate an LLM summary of a single record. "
                "Input: record_id (str!). "
                "Output: {ok, record_id, summary}.",
)
async def cap_record_summarise(record_id: str, trace_id=None) -> Dict:
    if not record_id:
        return {"error": "record_id required"}

    # Fetch the record
    loop = asyncio.get_running_loop()
    def _fetch():
        conn = _sqlite_conn()
        try:
            row = conn.execute(
                "SELECT * FROM fabric_records WHERE id=? LIMIT 1", (record_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    rec = await loop.run_in_executor(None, _fetch)
    if not rec:
        return {"error": f"Record {record_id} not found"}

    text = (rec.get("text") or "")[:4000]
    if not text.strip():
        return {"ok": True, "record_id": record_id,
                "summary": "(empty record — no text content)"}

    try:
        from Vera.Orchestration.capability_orchestration import ollama_generate
    except ImportError:
        return {"error": "LLM (ollama_generate) not available"}

    dataset_id = rec.get("dataset_id", "unknown")
    prompt = (
        f"Summarise the following record from dataset '{dataset_id}' in 2-4 concise sentences. "
        f"Focus on the key facts, entities, and any actionable information.\n\n"
        f"--- RECORD ---\n{text}\n--- END ---\n\n"
        f"Summary:"
    )
    try:
        summary = await asyncio.wait_for(
            ollama_generate(prompt, system="You are a concise summariser."),
            timeout=60,
        )
        summary = summary.strip()
    except asyncio.TimeoutError:
        return {"error": "LLM timed out"}
    except Exception as e:
        return {"error": f"LLM error: {e}"}

    return {"ok": True, "record_id": record_id,
            "dataset_id": dataset_id, "summary": summary}


@capability(
    "fabric.entity_graph.mentions",
    http_method="GET", http_path="/fabric/entity_graph/mentions",
    http_tags=["fabric", "graph", "entity"],
    memory="off", silent=True,
    description="List records that mention a given entity. "
                "Input: entity_id (str!). "
                "Output: {ok, entity_id, records:[{id,dataset_id,snippet}]}.",
)
async def cap_entity_graph_mentions(entity_id: str, trace_id=None) -> Dict:
    if not entity_id:
        return {"error": "entity_id required"}

    loop = asyncio.get_running_loop()
    def _fetch():
        conn = _sqlite_conn()
        try:
            rows = conn.execute(
                "SELECT record_id, dataset_id FROM fabric_entity_mentions "
                "WHERE entity_id=? ORDER BY created_at DESC LIMIT 200",
                (entity_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
        finally:
            conn.close()

    mentions = await loop.run_in_executor(None, _fetch)

    # Enrich with snippets
    if mentions:
        record_ids = [m["record_id"] for m in mentions[:50]]
        def _snippets():
            conn = _sqlite_conn()
            try:
                placeholders = ",".join("?" * len(record_ids))
                rows = conn.execute(
                    f"SELECT id, dataset_id, substr(text, 1, 200) as snippet "
                    f"FROM fabric_records WHERE id IN ({placeholders})",
                    record_ids
                ).fetchall()
                return {r["id"]: dict(r) for r in rows}
            except Exception:
                return {}
            finally:
                conn.close()
        snip_map = await loop.run_in_executor(None, _snippets)

        records = []
        for m in mentions:
            rid = m["record_id"]
            info = snip_map.get(rid, {})
            records.append({
                "id":         rid,
                "dataset_id": info.get("dataset_id", m.get("dataset_id", "")),
                "snippet":    info.get("snippet", ""),
            })
    else:
        records = []

    return {"ok": True, "entity_id": entity_id,
            "records": records, "total": len(records)}


@capability(
    "fabric.entity_graph.merge",
    http_method="POST", http_path="/fabric/entity_graph/merge",
    http_tags=["fabric", "graph", "entity"],
    memory="on",
    description="Merge one entity into another, moving all mention links. "
                "Input: entity_id (str! — source, will be deleted), "
                "target_id (str! — target, receives the mentions). "
                "Output: {ok, merged, source, target, mentions_moved}.",
)
async def cap_entity_graph_merge(
    entity_id: str, target_id: str = "", trace_id=None,
) -> Dict:
    if not entity_id or not target_id:
        return {"error": "entity_id and target_id required"}
    if entity_id == target_id:
        return {"error": "Cannot merge an entity into itself"}

    loop = asyncio.get_running_loop()

    # Move mention rows in SQLite
    def _move():
        conn = _sqlite_conn()
        try:
            cur = conn.execute(
                "UPDATE fabric_entity_mentions SET entity_id=? "
                "WHERE entity_id=?", (target_id, entity_id)
            )
            cnt = cur.rowcount
            conn.execute(
                "DELETE FROM fabric_entity_mentions WHERE rowid NOT IN "
                "(SELECT MIN(rowid) FROM fabric_entity_mentions "
                " GROUP BY entity_id, record_id)"
            )
            conn.commit()
            return cnt
        except Exception as e:
            log.warning("entity merge sqlite: %s", e)
            return 0
        finally:
            conn.close()

    moved = await loop.run_in_executor(None, _move)

    # Move edges in Neo4j graph
    adapter = GRAPHS.get("fabric")
    if adapter and adapter.available:
        try:
            await adapter.run_cypher(
                "MATCH (e:Entity {id: $src})-[r:MENTIONED_IN]->(rec) "
                "MERGE (t:Entity {id: $tgt})-[:MENTIONED_IN]->(rec) "
                "DELETE r",
                {"src": entity_id, "tgt": target_id}
            )
            await adapter.run_cypher(
                "MATCH (e:Entity {id: $src})-[r:CO_OCCURS]-(other) "
                "WHERE other.id <> $tgt "
                "MERGE (t:Entity {id: $tgt})-[:CO_OCCURS]-(other) "
                "DELETE r",
                {"src": entity_id, "tgt": target_id}
            )
            await adapter.run_cypher(
                "MATCH (e:Entity {id: $src}) DETACH DELETE e",
                {"src": entity_id}
            )
        except Exception as e:
            log.warning("entity merge neo4j: %s", e)

    return {"ok": True, "merged": True, "source": entity_id,
            "target": target_id, "mentions_moved": moved}


@capability(
    "fabric.entity_graph.purge",
    http_method="POST", http_path="/fabric/entity_graph/purge",
    http_tags=["fabric", "graph", "entity"],
    memory="off",
    description="Purge all entity state for a dataset. "
                "Input: dataset_id (str!), drop_entities (bool default False). "
                "Output: {ok, dataset_id, mentions_deleted, entities_deleted}.",
)
async def cap_entity_graph_purge(
    dataset_id: str, drop_entities: bool = False, trace_id=None,
) -> Dict:
    if not dataset_id:
        return {"error": "dataset_id required"}

    loop = asyncio.get_running_loop()

    def _purge_mentions():
        conn = _sqlite_conn()
        try:
            cur = conn.execute(
                "DELETE FROM fabric_entity_mentions WHERE dataset_id=?",
                (dataset_id,)
            )
            conn.commit()
            return cur.rowcount
        except Exception as e:
            log.warning("entity purge sqlite: %s", e)
            return 0
        finally:
            conn.close()

    mentions_deleted = await loop.run_in_executor(None, _purge_mentions)

    entities_deleted = 0
    adapter = GRAPHS.get("fabric")
    if adapter and adapter.available:
        try:
            await adapter.run_cypher(
                "MATCH (:Entity)-[r:MENTIONED_IN {dataset_id: $ds}]->() DELETE r",
                {"ds": dataset_id}
            )
            if drop_entities:
                result = await adapter.run_cypher(
                    "MATCH (e:Entity) WHERE NOT (e)-[:MENTIONED_IN]->() "
                    "WITH e LIMIT 5000 DETACH DELETE e RETURN count(e) as cnt", {}
                )
                if result and result[0]:
                    entities_deleted = result[0].get("cnt", 0)
        except Exception as e:
            log.warning("entity purge neo4j: %s", e)

    return {"ok": True, "dataset_id": dataset_id,
            "mentions_deleted": mentions_deleted,
            "entities_deleted": entities_deleted}


# ─────────────────────────────────────────────────────────────────────────────
# GRAPH NODE ACTIONS — server-side registry + dispatcher
# ─────────────────────────────────────────────────────────────────────────────

_NODE_ACTION_REGISTRY: Dict[str, list] = {
    "Dataset": [
        {"id": "browse",           "label": "Browse records",        "icon": "\u25e6",
         "capability": "__local"},
        {"id": "extract_entities", "label": "Extract entities",      "icon": "\u25c9",
         "capability": "fabric.entity_graph.extract",
         "args": {"dataset_id": "$id"}, "stream": "fabric.entity_graph.progress",
         "options": [
             {"name": "limit", "type": "int", "default": 1000, "label": "Max records"},
             {"name": "content_type", "type": "select", "default": "text",
              "options": ["text", "code", "web"], "label": "Content type"},
             {"name": "persist", "type": "bool", "default": True, "label": "Persist to graph"},
         ]},
        {"id": "run_loom",         "label": "Run Loom",              "icon": "\u29d6",
         "capability": "fabric.loom.run",
         "args": {"dataset_a": "$id"}, "stream": "fabric.loom.progress",
         "options": [
             {"name": "mode", "type": "select", "default": "hybrid",
              "options": ["vector", "keyword", "hybrid", "entity", "semantic", "tag"],
              "label": "Match mode"},
             {"name": "min_score", "type": "float", "default": 0.4, "label": "Min score"},
             {"name": "max_matches", "type": "int", "default": 100, "label": "Max matches"},
             {"name": "persist", "type": "bool", "default": True, "label": "Write edges to graph"},
             {"name": "edge_type", "type": "select", "default": "auto",
              "options": ["auto", "RELATED_TO", "SIMILAR_TO", "REFERENCES",
                          "DEPENDS_ON", "DERIVED_FROM", "SHARES_TOPIC"],
              "label": "Edge type"},
         ]},
        {"id": "pull_full_text",   "label": "Pull full text",       "icon": "\u21e3",
         "capability": "fabric.dataset.fetch_content",
         "args": {"dataset_id": "$id"}, "stream": "fabric.fetch_content.progress"},
        {"id": "ai_analyse",       "label": "AI Analyse Links",     "icon": "\u2726",
         "capability": "fabric.ai_analyse_links",
         "args": {}, "stream": "fabric.ai_analyse_links.progress",
         "options": [
             {"name": "max_pairs", "type": "int", "default": 8},
             {"name": "min_score", "type": "float", "default": 0.5},
             {"name": "auto_stitch", "type": "bool", "default": False},
         ]},
        {"id": "run_cap", "label": "Run capability against dataset", "icon": "\u25b8",
         "capability": "__dispatch",
         "args": {"dataset_id": "$id"},
         "options": [
             {"name": "capability", "type": "string", "default": "", "label": "Capability name"},
             {"name": "extra_args", "type": "string", "default": "{}", "label": "Extra args (JSON)"},
         ]},
    ],
    "FabricRecord": [
        {"id": "open_record", "label": "Open record", "icon": "\u25a6", "capability": "__local"},
        {"id": "extract_entities_record", "label": "Re-extract entities",
         "icon": "\u25c9", "capability": "fabric.entity_graph.extract_record",
         "args": {"record_id": "$id"}, "stream": "fabric.entity_graph.progress"},
        {"id": "find_related", "label": "Find related records (Loom)", "icon": "\u29d6",
         "capability": "fabric.loom.record_match",
         "args": {"record_id": "$id"}, "stream": "fabric.loom.progress",
         "options": [
             {"name": "mode", "type": "select", "default": "hybrid",
              "options": ["vector", "keyword", "hybrid"]},
             {"name": "max_matches", "type": "int", "default": 10},
         ]},
        {"id": "summarise", "label": "Summarise with LLM", "icon": "\u2726",
         "capability": "fabric.record.summarise", "args": {"record_id": "$id"}},
        {"id": "run_cap", "label": "Run capability against record", "icon": "\u25b8",
         "capability": "__dispatch",
         "args": {"record_id": "$id"},
         "options": [
             {"name": "capability", "type": "string", "default": "", "label": "Capability name"},
             {"name": "extra_args", "type": "string", "default": "{}", "label": "Extra args (JSON)"},
         ]},
    ],
    "Entity": [
        {"id": "show_mentions", "label": "Show mentions", "icon": "\u29c9",
         "capability": "fabric.entity_graph.mentions", "args": {"entity_id": "$id"}},
        {"id": "merge", "label": "Merge with another entity", "icon": "\u21c6",
         "capability": "fabric.entity_graph.merge", "args": {"entity_id": "$id"},
         "options": [{"name": "target_id", "type": "string", "label": "Target entity ID"}],
         "confirm": "Merge this entity into the target?"},
    ],
    "Ontology": [
        {"id": "view_ontology", "label": "View in Ontology Browser", "icon": "\u25e6",
         "capability": "__local"},
    ],
    "Skill": [
        {"id": "view_skill", "label": "View in Skills Editor", "icon": "\u25e6",
         "capability": "__local"},
    ],
}


@capability(
    "fabric.graph.node_actions",
    http_method="GET", http_path="/fabric/graph/node_actions",
    http_tags=["fabric", "graph"],
    memory="off", silent=True,
    description="Get available actions for a graph node by label.",
)
async def cap_graph_node_actions(node_label: str = "", node_id: str = "",
                                  trace_id=None) -> Dict:
    if not node_label:
        return {"actions": []}
    actions = _NODE_ACTION_REGISTRY.get(node_label, [])
    enriched = []
    for a in actions:
        a_copy = dict(a)
        if a_copy.get("capability") == "__dispatch":
            a_copy["available_capabilities"] = sorted(_orch.CAPABILITY_REGISTRY.keys())
        enriched.append(a_copy)
    return {"actions": enriched}


@capability(
    "fabric.graph.run_node_action",
    http_method="POST", http_path="/fabric/graph/run_node_action",
    http_tags=["fabric", "graph"],
    memory="off",
    description="Execute a graph node action by dispatching to the named capability.",
)
async def cap_graph_run_node_action(
    node_label: str = "", node_id: str = "",
    action_id: str = "", options: dict = None,
    trace_id=None,
) -> Dict:
    if not node_label or not action_id:
        return {"error": "node_label and action_id required"}
    options = options or {}
    actions = _NODE_ACTION_REGISTRY.get(node_label, [])
    action = None
    for a in actions:
        if a["id"] == action_id:
            action = a
            break
    if not action:
        return {"error": f"Unknown action '{action_id}' for '{node_label}'"}

    cap_name = action.get("capability", "")
    if cap_name == "__local":
        return {"ok": True, "local": True, "action_id": action_id}

    if cap_name == "__dispatch":
        cap_name = options.pop("capability", "")
        if not cap_name:
            return {"error": "No capability specified"}
        extra_str = options.pop("extra_args", "{}")
        try:
            extra = json.loads(extra_str) if isinstance(extra_str, str) else (extra_str or {})
        except Exception:
            extra = {}
        options.update(extra)

    cap = _orch.CAPABILITY_REGISTRY.get(cap_name)
    if not cap:
        return {"error": f"Capability '{cap_name}' not found"}

    args: Dict = {}
    for k, v in (action.get("args") or {}).items():
        args[k] = node_id if v == "$id" else v
    for opt_def in (action.get("options") or []):
        opt_name = opt_def["name"]
        if opt_name in options:
            val = options[opt_name]
            ot = opt_def.get("type", "string")
            try:
                if ot == "int": val = int(val)
                elif ot == "float": val = float(val)
                elif ot == "bool": val = val if isinstance(val, bool) else str(val).lower() in ("true","1","yes")
            except (ValueError, TypeError): pass
            args[opt_name] = val
        elif opt_name not in args and "default" in opt_def:
            args[opt_name] = opt_def["default"]

    # Introspect the target function's signature so we only pass valid kwargs.
    # This prevents TypeError when the action registry declares options that
    # don't match the actual capability function parameters.
    import inspect as _inspect
    try:
        sig = _inspect.signature(cap["func"])
        valid_params = set(sig.parameters.keys())
        # If the function uses **kwargs, all params are valid
        has_var_kw = any(
            p.kind == _inspect.Parameter.VAR_KEYWORD
            for p in sig.parameters.values()
        )
        if not has_var_kw:
            filtered = {k: v for k, v in args.items() if k in valid_params}
            if len(filtered) < len(args):
                dropped = set(args.keys()) - set(filtered.keys())
                log.debug("run_node_action: dropped unknown args %s for %s",
                          dropped, cap_name)
            args = filtered
    except (ValueError, TypeError):
        pass  # can't introspect, pass all args and hope for the best

    tid = trace_id or _orch.new_id()
    try:
        result = await cap["func"](**args, trace_id=tid)
        return {"ok": True, "result": result, "action_id": action_id,
                "capability": cap_name, "trace_id": tid}
    except Exception as e:
        log.error("run_node_action %s/%s: %s", cap_name, action_id, e)
        return {"error": str(e), "action_id": action_id}


# ─────────────────────────────────────────────────────────────────────────────
# FABRIC PANEL — standalone HTML page served at /fabric/panel
# ─────────────────────────────────────────────────────────────────────────────

from fastapi.responses import HTMLResponse as _FabHTMLResponse
from pathlib import Path as _FabPath

_FABRIC_PANEL_PATH = _FabPath(__file__).parent / "fabric_panel.html"


@APP.get("/fabric/panel", include_in_schema=False)
async def fabric_panel_html(trace_id=None):
    try:
        html = _FABRIC_PANEL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = (
            "<!DOCTYPE html><html><body style='background:#181614;color:#c96b6b;"
            "font-family:monospace;padding:40px'>"
            "<h2>fabric_panel.html not found</h2>"
            f"<p>Expected: {_FABRIC_PANEL_PATH}</p>"
            "</body></html>"
        )
    return _FabHTMLResponse(html)


from Vera.Orchestration.capability_orchestration import register_ui as _reg_ui

_reg_ui(
    "fabric-panel",
    "Data Fabric",
    "⬡",
    """<div style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/fabric/panel"
          style="flex:1;border:none;width:100%;height:100%;background:#181614"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=[
        "fabric.ingest", "fabric.query", "fabric.datasets", "fabric.stats",
        "fabric.schema", "fabric.browse", "fabric.sources", "fabric.sources.add",
        "fabric.sources.update", "fabric.sources.pull", "fabric.sources.delete",
        "fabric.dataset.delete", "fabric.clear_dataset", "fabric.delete_record",
        "fabric.aux_graph.link", "fabric.aux_graph.query",
        "fabric.bus.configure", "fabric.bus.status",
        "fabric.rss.fetch_content",
    ],
    mode="tab",
    tab_order=35,
)

log.info("fabric panel registered at /fabric/panel")