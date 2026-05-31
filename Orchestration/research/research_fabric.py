"""
research_fabric.py — Unified persistence + recall + activity for the research subsystem.
========================================================================================

This module replaces four separate files:
  • research_db.py                  — SQLite/PG persistence → now uses fabric
  • research_fabric.py (old)        — fabric ingest/recall helpers → kept + expanded
  • research_recall_capabilities.py — recall caps → folded in
  • research_activity_capabilities.py — activity tracking caps → folded in

All research data is persisted through the data fabric, which provides:
  • SQLite fallback (always available, zero config)
  • Vector embeddings via FAISS + Chroma (semantic search)
  • Graph links via Neo4j (relationship queries)
  • Full-text search across all records

The researcher_api.py imports this module and calls its functions directly
instead of maintaining its own database layer.

Dataset naming
──────────────
  research.jobs            — job submission records (query, mode, status, result)
  research.citations       — citations with full_text
  research.crawl_pages     — crawled pages
  research.searches        — search queries + result lists
  research.results         — final report/output text
  research.llm_calls       — LLM invocations
  research.notebooks       — notebook metadata
  research.notebook_cells  — notebook cells
  research.projects        — research projects
  research.project_rounds  — project rounds
  research.sessions        — research session records
  research.bookmarks       — user bookmarks
  research.generated_files — generated code/file outputs
  research.source_configs  — source configurations
  research.web_config      — web search config (singleton)
  research.instance_configs — Ollama instance configs
  research.iteration_targets — iteration targets
  research.notebook_pages  — notebook pages

Capabilities registered
───────────────────────
  research.recall.search       — semantic search across research datasets
  research.recall.job          — full job hydration
  research.recall.notebook     — notebook + cells
  research.recall.notebook.list — list notebooks
  research.recall.session      — session timeline
  research.recall.session.list — list sessions
  research.recall.project      — project + rounds
  research.recall.project.list — list projects
  research.recall.datasets     — list research datasets
  research.recall.crawled_pages — crawled pages by domain/query
  research.activity.search     — record a web search step
  research.activity.crawl      — record a page crawl
  research.activity.llm_call   — record an LLM invocation
  research.activity.citation   — record a citation discovery
  research.activity.notebook_save — record notebook save
  research.activity.cell_save  — record cell save
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("vera.research_fabric")


# ─────────────────────────────────────────────────────────────────────────────
# DATASET CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

DATASET_JOBS            = "research.jobs"
DATASET_SEARCHES        = "research.searches"
DATASET_CITATIONS       = "research.citations"
DATASET_CRAWL_PAGES     = "research.crawl_pages"
DATASET_RESULTS         = "research.results"
DATASET_LLM_CALLS       = "research.llm_calls"
DATASET_NOTEBOOKS       = "research.notebooks"
DATASET_NOTEBOOK_CELLS  = "research.notebook_cells"
DATASET_PROJECTS        = "research.projects"
DATASET_PROJECT_ROUNDS  = "research.project_rounds"
DATASET_SESSIONS        = "research.sessions"
DATASET_BOOKMARKS       = "research.bookmarks"
DATASET_GENERATED_FILES = "research.generated_files"
DATASET_SOURCE_CONFIGS  = "research.source_configs"
DATASET_WEB_CONFIG      = "research.web_config"
DATASET_INSTANCE_CONFIGS = "research.instance_configs"
DATASET_ITERATION_TARGETS = "research.iteration_targets"
DATASET_NOTEBOOK_PAGES  = "research.notebook_pages"
DATASET_PIPELINES       = "research.pipelines"
DATASET_PIPELINE_RUNS   = "research.pipeline_runs"

ALL_RESEARCH_DATASETS = [
    DATASET_JOBS, DATASET_SEARCHES, DATASET_CITATIONS,
    DATASET_CRAWL_PAGES, DATASET_RESULTS, DATASET_LLM_CALLS,
    DATASET_NOTEBOOKS, DATASET_NOTEBOOK_CELLS,
    DATASET_PROJECTS, DATASET_PROJECT_ROUNDS,
    DATASET_SESSIONS, DATASET_BOOKMARKS,
    DATASET_GENERATED_FILES, DATASET_SOURCE_CONFIGS,
    DATASET_INSTANCE_CONFIGS, DATASET_ITERATION_TARGETS,
    DATASET_NOTEBOOK_PAGES, DATASET_PIPELINES, DATASET_PIPELINE_RUNS,
]

# Datasets that should be included in semantic recall (skip config/infra)
RECALL_DATASETS = [
    DATASET_JOBS, DATASET_SEARCHES, DATASET_CITATIONS,
    DATASET_CRAWL_PAGES, DATASET_RESULTS, DATASET_LLM_CALLS,
    DATASET_NOTEBOOKS, DATASET_NOTEBOOK_CELLS,
    DATASET_PROJECTS, DATASET_SESSIONS,
]

TEXT_INDEX_LIMIT = 2000


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def _now_ts() -> float:
    return time.time()

def _ts_key(v: Any) -> float:
    """
    Coerce any timestamp value (float, int, ISO-8601 string, or None) into a
    sortable float. Records in fabric_records have created_at stored both as
    a float (in the data JSON) and an ISO string (in the column); the
    {**row, **data} merge means a given record can surface either type.
    Sorting a mixed list raises 'str < float' TypeError — this normalises it.
    """
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    # Pure numeric string
    try:
        return float(s)
    except ValueError:
        pass
    # ISO-8601 string
    try:
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2).timestamp()
    except Exception:
        return 0.0

def _int_key(v: Any) -> int:
    """Coerce any value to a sortable int (for sort_order / round_num)."""
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(float(str(v).strip()))
    except Exception:
        return 0

def _short_id() -> str:
    return uuid.uuid4().hex[:16]

def _j(v: Any) -> str:
    """JSON-serialise if not already a string."""
    return json.dumps(v) if not isinstance(v, str) else v

def _jload(v: Any) -> Any:
    """Deserialise JSON, tolerant of already-parsed values."""
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return v

def _fabric():
    """Resolve data_fabric module at runtime — avoids import cycles."""
    return (sys.modules.get("Vera.Orchestration.fabric.data_fabric") or
            sys.modules.get("Vera.Orchestration.fabric.data_fabric"))

def _registry():
    """Resolve CAPABILITY_REGISTRY at runtime."""
    co = (sys.modules.get("Vera.Orchestration.capability_orchestration") or
          sys.modules.get("capability_orchestration"))
    return getattr(co, "CAPABILITY_REGISTRY", {}) if co else {}

def _orch():
    """Resolve orchestrator module."""
    return (sys.modules.get("Vera.Orchestration.capability_orchestration") or
            sys.modules.get("capability_orchestration"))

def _emit_event(evt: dict):
    """Fire-and-forget event emission."""
    co = _orch()
    if co and hasattr(co, "emit_event"):
        asyncio.create_task(co.emit_event(evt))

def _memory_hooks():
    """Resolve memory_hooks for graph recording."""
    return (sys.modules.get("Vera.Orchestration.memory_hooks") or
            sys.modules.get("Vera.Orchestration.fabric.memory_hooks"))


# ─────────────────────────────────────────────────────────────────────────────
# RECORD SHAPING
# ─────────────────────────────────────────────────────────────────────────────

def shape_record(
    *,
    text:        str,
    full_text:   str = "",
    record_id:   str = "",
    job_id:      str = "",
    session_id:  str = "",
    project_id:  str = "",
    notebook_id: str = "",
    citation_id: str = "",
    url:         str = "",
    title:       str = "",
    domain:      str = "",
    extra:       Optional[Dict[str, Any]] = None,
    tags:        Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Produce a record dict in canonical research-fabric shape."""
    rid = record_id or _short_id()
    rec: Dict[str, Any] = {
        "id":         rid,
        "text":       (text or "")[:TEXT_INDEX_LIMIT],
        "full_text":  full_text or text,
        "created_at": _now_iso(),
        "tags":       list(tags or []),
    }
    if job_id:      rec["job_id"]      = job_id
    if session_id:  rec["session_id"]  = session_id
    if project_id:  rec["project_id"]  = project_id
    if notebook_id: rec["notebook_id"] = notebook_id
    if citation_id: rec["citation_id"] = citation_id
    if url:         rec["url"]         = url
    if title:       rec["title"]       = title
    if domain:      rec["domain"]      = domain
    if extra:
        for k, v in extra.items():
            if k not in rec:
                rec[k] = v
    return rec


# ─────────────────────────────────────────────────────────────────────────────
# LOW-LEVEL FABRIC I/O
# ─────────────────────────────────────────────────────────────────────────────

def _direct_sqlite_write(dataset_id: str, records: List[Dict],
                          source: str = "research", tags: Optional[List[str]] = None) -> int:
    """
    Guaranteed synchronous write to fabric_records via direct SQL.
    Used as a durability backstop — the async ingest_dataset write queue
    can silently drop records if its consumer task isn't running in the
    current event loop. This bypasses the queue entirely.

    Upsert semantics: if a record carries a logical 'id', any existing
    fabric_records row whose data JSON has the same id is deleted first,
    so re-saving a job/notebook/cell replaces it instead of duplicating.
    Returns number of rows written.
    """
    fab = _fabric()
    if not fab or not hasattr(fab, "SQLITE_PATH"):
        return 0
    import sqlite3
    written = 0
    try:
        conn = sqlite3.connect(fab.SQLITE_PATH, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            # Pre-load existing rows in this dataset once, to find dupes by logical id
            existing = conn.execute(
                "SELECT id, data FROM fabric_records WHERE dataset_id=?",
                (dataset_id,)
            ).fetchall()
            id_to_rowids: Dict[str, list] = {}
            for row_uuid, data_json in existing:
                try:
                    d = json.loads(data_json or "{}")
                except Exception:
                    continue
                lid = str(d.get("id", ""))
                if lid:
                    id_to_rowids.setdefault(lid, []).append(row_uuid)

            for rec in records:
                logical_id = str(rec.get("id", ""))
                # Delete any existing rows with the same logical id (upsert)
                if logical_id and logical_id in id_to_rowids:
                    for old_rowid in id_to_rowids[logical_id]:
                        conn.execute("DELETE FROM fabric_records WHERE id=?", (old_rowid,))

                row_id = _short_id()
                text = rec.get("text", "") or ""
                if not text:
                    text = " ".join(str(v) for v in rec.values()
                                    if isinstance(v, str))[:2000]
                conn.execute(
                    "INSERT OR REPLACE INTO fabric_records "
                    "(id, dataset_id, text, data, source_id, tags, created_at, synced_pg) "
                    "VALUES (?,?,?,?,?,?,?,0)",
                    (row_id, dataset_id, text[:2000], json.dumps(rec),
                     source, json.dumps(tags or []), _now_iso())
                )
                written += 1
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("_direct_sqlite_write [%s]: %s", dataset_id, e)
    return written


async def _ingest(dataset_id: str, records: List[Dict], source: str = "research",
                  tags: Optional[List[str]] = None) -> Dict:
    """Write one or more records to a fabric dataset.

    Uses a direct synchronous SQL insert for guaranteed durability and
    immediate read-back. The async fabric pipeline (ingest_dataset) is NOT
    used here because its write queue can silently drop records if its
    consumer task isn't running in the current event loop — which is exactly
    why research history/notebooks were not persisting.

    The direct write puts rows into the same fabric_records table the fabric
    UI reads, so records remain visible there too.
    """
    fab = _fabric()
    if not fab:
        log.warning("_ingest [%s]: _fabric() returned None — record NOT saved", dataset_id)
        return {"ok": False, "reason": "fabric unavailable", "ingested": 0}

    direct_written = await asyncio.get_event_loop().run_in_executor(
        None, _direct_sqlite_write, dataset_id, records, source, tags
    )
    if direct_written == 0:
        log.warning("_ingest [%s]: 0 rows written for %d records",
                    dataset_id, len(records))
    else:
        log.debug("_ingest [%s]: wrote %d rows", dataset_id, direct_written)
    return {"ok": direct_written > 0, "ingested": direct_written}


async def _ingest_one(dataset_id: str, record: Dict, source: str = "research",
                      tags: Optional[List[str]] = None) -> Dict:
    """Convenience: ingest a single record."""
    return await _ingest(dataset_id, [record], source, tags)


async def _query_by_filter(
    dataset_id: str,
    filters:    Dict[str, Any],
    limit:      int = 50,
) -> List[Dict]:
    """Pull records by exact field match on the data JSON column.

    Deduplicates by logical record id (data['id']) — keeps only the newest
    row per id. Stale rows accumulate when older code wrote without upsert;
    this guarantees the UI never shows duplicate jobs/notebooks/cells.
    """
    fab = _fabric()
    if not fab:
        log.warning("_query_by_filter [%s]: _fabric() returned None — "
                    "data_fabric not in sys.modules", dataset_id)
        return []
    if not hasattr(fab, "_sqlite_query"):
        log.warning("_query_by_filter [%s]: fabric module has no _sqlite_query", dataset_id)
        return []
    try:
        rows = await fab._sqlite_query(dataset_id=dataset_id, limit=max(limit, 2000))
        # First pass: parse + filter
        matched: List[Dict] = []
        for row in rows:
            try:
                data = json.loads(row.get("data") or "{}")
            except Exception:
                data = {}
            if all(str(data.get(k)) == str(v) for k, v in filters.items()):
                matched.append({**row, "_data": data, **data})
        # Second pass: dedup by logical id, keep newest (by created_at/updated_at)
        by_id: Dict[str, Dict] = {}
        no_id: List[Dict] = []
        for r in matched:
            lid = str(r.get("id", "") or "")
            if not lid:
                no_id.append(r)
                continue
            prev = by_id.get(lid)
            if prev is None:
                by_id[lid] = r
            else:
                # keep whichever has the newer timestamp
                r_ts    = max(_ts_key(r.get("updated_at")), _ts_key(r.get("created_at")))
                prev_ts = max(_ts_key(prev.get("updated_at")), _ts_key(prev.get("created_at")))
                if r_ts >= prev_ts:
                    by_id[lid] = r
        out = list(by_id.values()) + no_id
        return out[:limit]
    except Exception as e:
        log.warning("_query_by_filter [%s] FAILED: %s", dataset_id, e)
        return []


async def _query_by_id(dataset_id: str, record_id: str) -> Optional[Dict]:
    """Fetch a single record by its id."""
    fab = _fabric()
    if not fab or not hasattr(fab, "_sqlite_query"):
        return None
    try:
        rows = await fab._sqlite_query(dataset_id=dataset_id, limit=1000)
        for row in rows:
            if row.get("id") == record_id:
                data = _jload(row.get("data") or "{}")
                return {**row, "_data": data, **data}
        return None
    except Exception:
        return None


async def _delete_by_filter(dataset_id: str, filters: Dict[str, Any]) -> int:
    """Delete records matching a filter. Returns count deleted."""
    fab = _fabric()
    if not fab:
        return 0
    try:
        rows = await fab._sqlite_query(dataset_id=dataset_id, limit=10000)
        to_delete = []
        for row in rows:
            try:
                data = json.loads(row.get("data") or "{}")
            except Exception:
                data = {}
            if all(str(data.get(k)) == str(v) for k, v in filters.items()):
                to_delete.append(row["id"])

        if not to_delete:
            return 0

        import sqlite3
        conn = sqlite3.connect(fab.SQLITE_PATH, timeout=30, check_same_thread=False)
        try:
            placeholders = ",".join("?" for _ in to_delete)
            conn.execute(
                f"DELETE FROM fabric_records WHERE id IN ({placeholders})",
                to_delete
            )
            conn.commit()
            return len(to_delete)
        finally:
            conn.close()
    except Exception as e:
        log.warning("_delete_by_filter [%s]: %s", dataset_id, e)
        return 0


async def _count_dataset(dataset_id: str) -> int:
    """Count records in a dataset."""
    fab = _fabric()
    if not fab:
        return 0
    try:
        import sqlite3
        conn = sqlite3.connect(fab.SQLITE_PATH, timeout=10, check_same_thread=False)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM fabric_records WHERE dataset_id=?",
                (dataset_id,)
            ).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()
    except Exception:
        return 0


# ═══════════════════════════════════════════════════════════════════════════════
# DB-COMPATIBLE API — drop-in replacement for research_db.DB
# ═══════════════════════════════════════════════════════════════════════════════
#
# researcher_api.py calls DB.save_job(), DB.load_history(), etc.
# This class provides the same interface but persists through the fabric.
# ═══════════════════════════════════════════════════════════════════════════════

class DB:
    """
    Drop-in replacement for research_db.DB.

    Every method has the same signature and return shape as the original,
    but persists through the data fabric instead of a separate SQLite/PG DB.

    Call DB.init() once at startup (safe to call repeatedly).
    """

    _ready = False

    @staticmethod
    async def init():
        DB._ready = True
        log.info("research_fabric.DB: ready (backed by data fabric)")

    @staticmethod
    async def close():
        pass  # fabric manages its own connections

    # ── research jobs ─────────────────────────────────────────────────────────

    @staticmethod
    async def save_job(job) -> None:
        """Upsert a ResearchJob and its citations into the fabric."""
        rec = {
            "id":          job.id,
            "text":        f"[{job.mode}/{job.output_mode}] {job.query}"[:TEXT_INDEX_LIMIT],
            "full_text":   (job.result or job.query or "")[:50000],
            "query":       job.query,
            "mode":        str(job.mode.value) if hasattr(job.mode, "value") else str(job.mode),
            "output_mode": str(job.output_mode.value) if hasattr(job.output_mode, "value") else str(job.output_mode),
            "status":      str(job.status.value) if hasattr(job.status, "value") else str(job.status),
            "result":      job.result or "",
            "error":       job.error or "",
            "sources":     _j(job.sources) if not isinstance(job.sources, str) else job.sources,
            "steps":       _j(job.steps) if not isinstance(job.steps, str) else job.steps,
            "file_tree":   _j({k: "" for k in job.file_tree.keys()}) if job.file_tree else "{}",
            "project_id":  job.project_id or "",
            "token_count": job.token_count,
            "created_at":  job.created_at,
            "finished_at": job.finished_at,
            "job_id":      job.id,
        }
        await _ingest_one(DATASET_JOBS, rec, tags=["research", "job"])

        # Save citations
        if job.citations:
            cit_recs = []
            for c in job.citations:
                cit_recs.append({
                    "id":              c.id,
                    "text":            f"{c.title or c.url}: {(c.snippet or '')[:200]}"[:TEXT_INDEX_LIMIT],
                    "full_text":       c.full_text or c.snippet or "",
                    "job_id":          job.id,
                    "url":             c.url or "",
                    "title":           c.title or "",
                    "snippet":         c.snippet or "",
                    "source_type":     c.source_type or "web",
                    "screenshot_path": c.screenshot_path or "",
                    "domain":          c.domain or "",
                    "fetched_at":      c.fetched_at or _now_ts(),
                    "citation_id":     c.id,
                })
            await _ingest(DATASET_CITATIONS, cit_recs, tags=["research", "citation"])

        # Save generated files
        if job.file_tree:
            await DB.save_generated_files(
                job_id=job.id,
                project_id=getattr(job, "project_id", None),
                file_tree=job.file_tree,
            )

        # Also save the result text as a separate record for recall
        if job.result and str(getattr(job.status, "value", job.status)) == "done":
            await _ingest_one(DATASET_RESULTS, {
                "id":        f"result:{job.id}",
                "text":      job.result[:TEXT_INDEX_LIMIT],
                "full_text": job.result,
                "job_id":    job.id,
                "query":     job.query,
                "mode":      str(job.mode.value) if hasattr(job.mode, "value") else str(job.mode),
            }, tags=["research", "result"])

    # ── generated files ───────────────────────────────────────────────────────

    @staticmethod
    async def save_generated_files(job_id: str, project_id: Optional[str],
                                   file_tree: dict) -> None:
        if not file_tree:
            return
        # Delete old versions for this job
        await _delete_by_filter(DATASET_GENERATED_FILES, {"job_id": job_id})
        recs = []
        for path, content in file_tree.items():
            recs.append({
                "id":         f"{job_id}:{path}",
                "text":       f"File: {path} ({len(content)} bytes)"[:TEXT_INDEX_LIMIT],
                "full_text":  content[:100000],
                "job_id":     job_id,
                "project_id": project_id or "",
                "file_path":  path,
                "content":    content,
                "size_bytes": len(content.encode("utf-8")) if content else 0,
                "created_at": _now_ts(),
            })
        if recs:
            await _ingest(DATASET_GENERATED_FILES, recs, tags=["research", "file"])

    @staticmethod
    async def load_generated_files(job_id: str) -> dict:
        rows = await _query_by_filter(DATASET_GENERATED_FILES, {"job_id": job_id}, limit=500)
        return {r.get("file_path", ""): r.get("content", "") for r in rows if r.get("file_path")}

    @staticmethod
    async def load_generated_files_for_project(project_id: str) -> dict:
        rows = await _query_by_filter(DATASET_GENERATED_FILES, {"project_id": project_id}, limit=500)
        seen, out = set(), {}
        for r in sorted(rows, key=lambda x: _ts_key(x.get("created_at")), reverse=True):
            fp = r.get("file_path", "")
            if fp and fp not in seen:
                seen.add(fp)
                out[fp] = r.get("content", "")
        return out

    @staticmethod
    async def get_generated_file(job_id: str, file_path: str) -> Optional[str]:
        row = await _query_by_id(DATASET_GENERATED_FILES, f"{job_id}:{file_path}")
        return row.get("content") if row else None

    @staticmethod
    async def list_generated_files(job_id: str) -> list:
        rows = await _query_by_filter(DATASET_GENERATED_FILES, {"job_id": job_id}, limit=500)
        return [{"id": r.get("id", ""), "job_id": job_id,
                 "project_id": r.get("project_id", ""),
                 "file_path": r.get("file_path", ""),
                 "size_bytes": r.get("size_bytes", 0),
                 "created_at": r.get("created_at", "")} for r in rows]

    # ── history / search ──────────────────────────────────────────────────────

    @staticmethod
    async def load_history(
        limit: int = 50, offset: int = 0,
        project_id: Optional[str] = None,
        mode: Optional[str] = None,
        output_mode: Optional[str] = None,
        search: Optional[str] = None,
    ) -> tuple:
        """Load job summaries with filtering. Returns (rows, total_count)."""
        # Pull all jobs and filter in memory (fabric doesn't have SQL WHERE)
        all_rows = await _query_by_filter(DATASET_JOBS, {}, limit=2000)
        log.info("load_history: _query_by_filter('research.jobs') returned %d raw rows",
                 len(all_rows))

        # Apply filters
        filtered = []
        for r in all_rows:
            if project_id and r.get("project_id") != project_id:
                continue
            if mode and r.get("mode") != mode:
                continue
            if output_mode and r.get("output_mode") != output_mode:
                continue
            if search:
                haystack = (r.get("query", "") + " " + r.get("result", "")[:2000]).lower()
                if search.lower() not in haystack:
                    continue
            filtered.append(r)

        # Sort by created_at descending
        filtered.sort(key=lambda x: _ts_key(x.get("created_at")), reverse=True)
        total = len(filtered)
        page = filtered[offset:offset + limit]

        # Shape output to match research_db format
        rows = []
        for r in page:
            cit_count = await _count_by_parent(DATASET_CITATIONS, "job_id", r.get("job_id", r.get("id", "")))
            rows.append({
                "id":             r.get("job_id", r.get("id", "")),
                "query":          r.get("query", ""),
                "mode":           r.get("mode", "single"),
                "output_mode":    r.get("output_mode", "report"),
                "status":         r.get("status", "unknown"),
                "error":          r.get("error", ""),
                "project_id":     r.get("project_id", ""),
                "token_count":    _int_key(r.get("token_count", 0)),
                "created_at":     _ts_key(r.get("created_at")),
                "finished_at":    (_ts_key(r.get("finished_at"))
                                   if r.get("finished_at") not in (None, "", 0)
                                   else None),
                "result_snippet": (r.get("result", "") or "")[:200],
                "citation_count": cit_count,
                "has_files":      bool(r.get("file_tree") and r["file_tree"] != "{}"),
            })
        return rows, total

    @staticmethod
    async def load_job_result(job_id: str) -> Optional[dict]:
        """Load full result + citations + file manifest for one job."""
        rows = await _query_by_filter(DATASET_JOBS, {"job_id": job_id}, limit=5)
        if not rows:
            # Fallback: try by id directly
            rows = await _query_by_filter(DATASET_JOBS, {"id": job_id}, limit=5)
        if not rows:
            return None
        r = rows[0]

        cits = await _query_by_filter(DATASET_CITATIONS, {"job_id": job_id}, limit=500)
        for c in cits:
            c["screenshot_url"] = f"/screenshots/{c['screenshot_path']}" if c.get("screenshot_path") else ""

        file_manifest = await DB.list_generated_files(job_id)

        return {
            "id":            job_id,
            "query":         r.get("query", ""),
            "mode":          r.get("mode", "single"),
            "output_mode":   r.get("output_mode", "report"),
            "status":        r.get("status", "unknown"),
            "result":        r.get("result", ""),
            "error":         r.get("error", ""),
            "steps":         _jload(r.get("steps", "[]")),
            "citations":     cits,
            "file_tree":     list(_jload(r.get("file_tree", "{}")).keys()) if isinstance(_jload(r.get("file_tree", "{}")), dict) else [],
            "file_manifest": file_manifest,
            "sources":       _jload(r.get("sources", "[]")),
            "project_id":    r.get("project_id", ""),
            "token_count":   r.get("token_count", 0),
            "created_at":    r.get("created_at", 0),
            "finished_at":   r.get("finished_at"),
        }

    @staticmethod
    async def load_job(job_id: str):
        """Load a single job as an attribute-accessible object.

        cap_job_status (research.job.status) does attribute access on the
        returned value — job.id, job.status, getattr(job,'query',''),
        job.created_at, getattr(job,'finished_at',None) — and performs
        arithmetic (finished_at - created_at). A plain dict would raise
        AttributeError on job.status, which was being swallowed by a bare
        except and surfacing as a bogus 'not_found'. So this returns a
        types.SimpleNamespace, with timestamps coerced to floats.

        Returns None if the job is not present in the fabric.
        """
        row = await DB.load_job_result(job_id)
        if not row:
            return None
        import types
        return types.SimpleNamespace(
            id          = row.get("id", job_id),
            query       = row.get("query", ""),
            mode        = row.get("mode", "single"),
            output_mode = row.get("output_mode", "report"),
            status      = row.get("status", "unknown"),
            result      = row.get("result", "") or "",
            error       = row.get("error", "") or "",
            citations   = row.get("citations", []) or [],
            sources     = row.get("sources", []) or [],
            project_id  = row.get("project_id", ""),
            token_count = _int_key(row.get("token_count", 0)),
            created_at  = _ts_key(row.get("created_at")),
            finished_at = (_ts_key(row.get("finished_at"))
                           if row.get("finished_at") not in (None, "", 0)
                           else None),
        )

    @staticmethod
    async def delete_job(job_id: str) -> int:
        count = await _delete_by_filter(DATASET_JOBS, {"job_id": job_id})
        await _delete_by_filter(DATASET_CITATIONS, {"job_id": job_id})
        await _delete_by_filter(DATASET_GENERATED_FILES, {"job_id": job_id})
        await _delete_by_filter(DATASET_RESULTS, {"job_id": job_id})
        return count

    @staticmethod
    async def search(q: str = "", mode: str = "", output_mode: str = "",
                     limit: int = 24, offset: int = 0) -> tuple:
        return await DB.load_history(
            limit=limit, offset=offset,
            mode=mode or None, output_mode=output_mode or None,
            search=q or None,
        )

    # ── projects ──────────────────────────────────────────────────────────────

    @staticmethod
    async def save_project(project) -> None:
        ft = {k: v[:200] for k, v in project.file_tree.items()} if hasattr(project, "file_tree") and project.file_tree else {}
        rec = {
            "id":              project.id,
            "text":            f"Project: {project.name}"[:TEXT_INDEX_LIMIT],
            "full_text":       f"{project.name}\n{project.description or ''}\n{project.context_summary or ''}",
            "project_id":      project.id,
            "name":            project.name,
            "description":     project.description or "",
            "output_mode":     str(project.output_mode.value) if hasattr(project.output_mode, "value") else str(project.output_mode),
            "context_summary": project.context_summary or "",
            "file_tree":       _j(ft),
            "created_at":      project.created_at,
            "updated_at":      project.updated_at,
        }
        await _ingest_one(DATASET_PROJECTS, rec, tags=["research", "project"])

        # Save rounds
        if hasattr(project, "rounds") and project.rounds:
            for r in project.rounds:
                round_rec = {
                    "id":         r.id,
                    "text":       f"Round {r.round_num}: {r.query}"[:TEXT_INDEX_LIMIT],
                    "full_text":  (r.result or r.query)[:4000],
                    "project_id": project.id,
                    "job_id":     r.job_id or "",
                    "round_num":  r.round_num,
                    "query":      r.query,
                    "result":     (r.result or "")[:4000],
                    "citations":  _j(r.citations) if not isinstance(r.citations, str) else r.citations,
                    "created_at": r.created_at,
                }
                await _ingest_one(DATASET_PROJECT_ROUNDS, round_rec, tags=["research", "round"])

    @staticmethod
    async def load_projects() -> list:
        rows = await _query_by_filter(DATASET_PROJECTS, {}, limit=500)
        out = []
        for r in rows:
            pid = r.get("project_id", r.get("id", ""))
            round_count = len(await _query_by_filter(DATASET_PROJECT_ROUNDS, {"project_id": pid}, limit=500))
            ft = _jload(r.get("file_tree", "{}"))
            out.append({
                "id":              pid,
                "name":            r.get("name", ""),
                "description":     r.get("description", ""),
                "output_mode":     r.get("output_mode", "report"),
                "context_summary": r.get("context_summary", ""),
                "file_count":      len(ft) if isinstance(ft, dict) else 0,
                "file_tree":       None,
                "round_count":     round_count,
                "created_at":      r.get("created_at", 0),
                "updated_at":      r.get("updated_at", 0),
                "enabled":         True,
            })
        out.sort(key=lambda x: _ts_key(x.get("updated_at")), reverse=True)
        return out

    @staticmethod
    async def load_project(project_id: str) -> Optional[dict]:
        rows = await _query_by_filter(DATASET_PROJECTS, {"project_id": project_id}, limit=5)
        if not rows:
            return None
        r = rows[0]
        rounds = await _query_by_filter(DATASET_PROJECT_ROUNDS, {"project_id": project_id}, limit=500)
        rounds.sort(key=lambda x: _int_key(x.get("round_num")))
        ft = _jload(r.get("file_tree", "{}"))
        return {
            "id":              project_id,
            "name":            r.get("name", ""),
            "description":     r.get("description", ""),
            "output_mode":     r.get("output_mode", "report"),
            "context_summary": r.get("context_summary", ""),
            "file_tree":       ft,
            "file_count":      len(ft) if isinstance(ft, dict) else 0,
            "rounds":          [{"id": rd.get("id"), "round_num": rd.get("round_num"),
                                 "query": rd.get("query"), "job_id": rd.get("job_id"),
                                 "created_at": rd.get("created_at")} for rd in rounds],
            "created_at":      r.get("created_at", 0),
            "updated_at":      r.get("updated_at", 0),
        }

    @staticmethod
    async def delete_project(project_id: str) -> int:
        count = await _delete_by_filter(DATASET_PROJECTS, {"project_id": project_id})
        await _delete_by_filter(DATASET_PROJECT_ROUNDS, {"project_id": project_id})
        return count

    # ── source configs ────────────────────────────────────────────────────────

    @staticmethod
    async def save_sources(sources: list) -> None:
        await _delete_by_filter(DATASET_SOURCE_CONFIGS, {})
        recs = []
        for i, s in enumerate(sources):
            recs.append({
                "id":         s.id if hasattr(s, "id") else s.get("id", _short_id()),
                "text":       f"Source: {s.label if hasattr(s, 'label') else s.get('label', '')}"[:TEXT_INDEX_LIMIT],
                "label":      s.label if hasattr(s, "label") else s.get("label", ""),
                "type":       (s.type.value if hasattr(s.type, "value") else str(s.type)) if hasattr(s, "type") else s.get("type", ""),
                "enabled":    bool(s.enabled if hasattr(s, "enabled") else s.get("enabled", True)),
                "config":     _j(s.config if hasattr(s, "config") else s.get("config", {})),
                "status":     s.status if hasattr(s, "status") else s.get("status", "unknown"),
                "sort_order": i,
            })
        if recs:
            await _ingest(DATASET_SOURCE_CONFIGS, recs, tags=["research", "config"])

    @staticmethod
    async def load_sources() -> list:
        rows = await _query_by_filter(DATASET_SOURCE_CONFIGS, {}, limit=200)
        rows.sort(key=lambda x: _int_key(x.get("sort_order")))
        out = []
        for r in rows:
            t = r.get("type", "")
            if t and "." in t:
                t = t.split(".")[-1].lower()
            # Robust enabled parsing — handles bool, string, int
            en = r.get("enabled", True)
            if isinstance(en, str):
                en = en.lower() not in ("false", "0", "no", "")
            # Robust config parsing — may be string, dict, or nested JSON
            cfg = _jload(r.get("config", "{}"))
            if isinstance(cfg, str):
                try: cfg = json.loads(cfg)
                except Exception: cfg = {}
            if not isinstance(cfg, dict):
                cfg = {}
            out.append({
                "id":         r.get("id", ""),
                "label":      r.get("label", ""),
                "type":       t,
                "enabled":    bool(en),
                "config":     cfg,
                "status":     r.get("status", "unknown"),
                "sort_order": r.get("sort_order", 0),
            })
        return out

    # ── web search config ─────────────────────────────────────────────────────

    @staticmethod
    async def save_web_search_config(cfg) -> None:
        rec = {
            "id":              "singleton",
            "text":            "Web search configuration",
            "engine":          cfg.engine if hasattr(cfg, "engine") else cfg.get("engine", "searxng"),
            "result_count":    cfg.result_count if hasattr(cfg, "result_count") else cfg.get("result_count", 8),
            "crawl_depth":     cfg.crawl_depth if hasattr(cfg, "crawl_depth") else cfg.get("crawl_depth", 1),
            "crawl_breadth":   cfg.crawl_breadth if hasattr(cfg, "crawl_breadth") else cfg.get("crawl_breadth", 3),
            "crawl_timeout":   cfg.crawl_timeout if hasattr(cfg, "crawl_timeout") else cfg.get("crawl_timeout", 8.0),
            "include_archive": bool(cfg.include_archive if hasattr(cfg, "include_archive") else cfg.get("include_archive", False)),
            "safe_search":     cfg.safe_search if hasattr(cfg, "safe_search") else cfg.get("safe_search", 0),
        }
        await _ingest_one(DATASET_WEB_CONFIG, rec, tags=["research", "config"])

    @staticmethod
    async def load_web_search_config() -> Optional[dict]:
        row = await _query_by_id(DATASET_WEB_CONFIG, "singleton")
        return row

    # ── instance configs ──────────────────────────────────────────────────────

    @staticmethod
    async def save_instances(instances: list) -> None:
        await _delete_by_filter(DATASET_INSTANCE_CONFIGS, {})
        recs = []
        for idx, i in enumerate(instances):
            recs.append({
                "id":         i.name if hasattr(i, "name") else i.get("name", ""),
                "text":       f"Instance: {i.name if hasattr(i, 'name') else i.get('name', '')}"[:TEXT_INDEX_LIMIT],
                "name":       i.name if hasattr(i, "name") else i.get("name", ""),
                "host":       i.host if hasattr(i, "host") else i.get("host", ""),
                "port":       i.port if hasattr(i, "port") else i.get("port", 11434),
                "tier":       (i.tier.value if hasattr(i.tier, "value") else str(i.tier)) if hasattr(i, "tier") else i.get("tier", "auto"),
                "model":      i.model if hasattr(i, "model") else i.get("model", ""),
                "ctx_size":   i.ctx_size if hasattr(i, "ctx_size") else i.get("ctx_size", 8192),
                "enabled":    bool(i.enabled if hasattr(i, "enabled") else i.get("enabled", True)),
                "sort_order": idx,
            })
        if recs:
            await _ingest(DATASET_INSTANCE_CONFIGS, recs, tags=["research", "config"])

    @staticmethod
    async def load_instances() -> list:
        rows = await _query_by_filter(DATASET_INSTANCE_CONFIGS, {}, limit=100)
        rows.sort(key=lambda x: _int_key(x.get("sort_order")))
        for r in rows:
            r["enabled"] = bool(r.get("enabled", True))
            t = r.get("tier", "")
            if t and "." in t:
                r["tier"] = t.split(".")[-1].lower()
        return rows

    # ── bookmarks ─────────────────────────────────────────────────────────────

    @staticmethod
    async def save_bookmark(bm: dict) -> None:
        rec = {
            "id":             bm["id"],
            "text":           f"Bookmark: {bm.get('title', bm.get('url', ''))}"[:TEXT_INDEX_LIMIT],
            "full_text":      bm.get("snippet", ""),
            "type":           bm.get("type", "citation"),
            "job_id":         bm.get("job_id", ""),
            "title":          bm.get("title", ""),
            "url":            bm.get("url", ""),
            "snippet":        bm.get("snippet", ""),
            "screenshot_url": bm.get("screenshot_url", ""),
            "source_type":    bm.get("source_type", "web"),
            "domain":         bm.get("domain", ""),
            "tags":           _j(bm.get("tags", [])),
            "note":           bm.get("note", ""),
            "created_at":     bm.get("created_at", _now_ts()),
        }
        await _ingest_one(DATASET_BOOKMARKS, rec, tags=["research", "bookmark"])

    @staticmethod
    async def load_bookmarks(limit: int = 500) -> list:
        rows = await _query_by_filter(DATASET_BOOKMARKS, {}, limit=limit)
        rows.sort(key=lambda x: _ts_key(x.get("created_at")), reverse=True)
        for r in rows:
            r["tags"] = _jload(r.get("tags", "[]"))
        return rows

    @staticmethod
    async def get_bookmark(bm_id: str) -> Optional[dict]:
        row = await _query_by_id(DATASET_BOOKMARKS, bm_id)
        if row:
            row["tags"] = _jload(row.get("tags", "[]"))
        return row

    @staticmethod
    async def delete_bookmark(bm_id: str) -> None:
        await _delete_by_filter(DATASET_BOOKMARKS, {"id": bm_id})

    # ── notebooks ─────────────────────────────────────────────────────────────

    @staticmethod
    async def save_notebook(nb: dict) -> None:
        rec = {
            "id":          nb["id"],
            "text":        f"Notebook: {nb.get('title', 'Untitled')}"[:TEXT_INDEX_LIMIT],
            "full_text":   f"{nb.get('title', '')}\n{nb.get('description', '')}",
            "notebook_id": nb["id"],
            "title":       nb.get("title", "Untitled Notebook"),
            "description": nb.get("description", ""),
            "project_id":  nb.get("project_id") or "",
            "tags":        _j(nb.get("tags", [])),
            "created_at":  nb.get("created_at", _now_ts()),
            "updated_at":  nb.get("updated_at", _now_ts()),
        }
        await _ingest_one(DATASET_NOTEBOOKS, rec, tags=["research", "notebook"])

    @staticmethod
    async def load_notebooks(project_id: Optional[str] = None) -> list:
        if project_id:
            rows = await _query_by_filter(DATASET_NOTEBOOKS, {"project_id": project_id}, limit=500)
        else:
            rows = await _query_by_filter(DATASET_NOTEBOOKS, {}, limit=500)
        rows.sort(key=lambda x: _ts_key(x.get("updated_at")), reverse=True)
        for r in rows:
            r["tags"] = _jload(r.get("tags", "[]"))
        return rows

    @staticmethod
    async def load_notebook(nb_id: str) -> Optional[dict]:
        nbs = await _query_by_filter(DATASET_NOTEBOOKS, {"notebook_id": nb_id}, limit=5)
        if not nbs:
            return None
        nb = nbs[0]
        nb["tags"] = _jload(nb.get("tags", "[]"))
        cells = await _query_by_filter(DATASET_NOTEBOOK_CELLS, {"notebook_id": nb_id}, limit=500)
        cells.sort(key=lambda x: _int_key(x.get("sort_order")))
        for c in cells:
            c["thread"]    = _jload(c.get("thread", "[]"))
            c["citations"] = _jload(c.get("citations", "[]"))
        nb["cells"] = cells
        pages = await _query_by_filter(DATASET_NOTEBOOK_PAGES, {"notebook_id": nb_id}, limit=100)
        pages.sort(key=lambda x: _int_key(x.get("sort_order")))
        nb["pages"] = pages
        return nb

    @staticmethod
    async def delete_notebook(nb_id: str) -> None:
        await _delete_by_filter(DATASET_NOTEBOOKS, {"notebook_id": nb_id})
        await _delete_by_filter(DATASET_NOTEBOOK_CELLS, {"notebook_id": nb_id})
        await _delete_by_filter(DATASET_NOTEBOOK_PAGES, {"notebook_id": nb_id})

    @staticmethod
    async def save_cell(cell: dict) -> None:
        rec = {
            "id":          cell["id"],
            "text":        (cell.get("content", "") or cell.get("generated", ""))[:TEXT_INDEX_LIMIT],
            "full_text":   f"CONTENT:\n{cell.get('content', '')}\nGENERATED:\n{cell.get('generated', '')}",
            "notebook_id": cell["notebook_id"],
            "sort_order":  cell.get("sort_order", 0),
            "cell_type":   cell.get("cell_type", "markdown"),
            "lang":        cell.get("lang", "python"),
            "tag":         cell.get("tag", "none"),
            "content":     cell.get("content", ""),
            "generated":   cell.get("generated", ""),
            "thread":      _j(cell.get("thread", [])),
            "page_id":     cell.get("page_id") or "",
            "title":       cell.get("title", ""),
            "citations":   _j(cell.get("citations", [])),
            "parse_mode":  cell.get("parse_mode", "whole"),
            "agent_mode":  cell.get("agent_mode", "single"),
            "created_at":  cell.get("created_at", _now_ts()),
            "updated_at":  cell.get("updated_at", _now_ts()),
        }
        await _ingest_one(DATASET_NOTEBOOK_CELLS, rec, tags=["research", "notebook", "cell"])
        # Touch parent notebook updated_at
        nbs = await _query_by_filter(DATASET_NOTEBOOKS, {"notebook_id": cell["notebook_id"]}, limit=1)
        if nbs:
            nbs[0]["updated_at"] = _now_ts()
            await _ingest_one(DATASET_NOTEBOOKS, nbs[0], tags=["research", "notebook"])

    @staticmethod
    async def save_cells_bulk(cells: list) -> None:
        for cell in cells:
            await DB.save_cell(cell)

    @staticmethod
    async def delete_cell(cell_id: str) -> None:
        await _delete_by_filter(DATASET_NOTEBOOK_CELLS, {"id": cell_id})

    @staticmethod
    async def load_cell(cell_id: str) -> Optional[dict]:
        row = await _query_by_id(DATASET_NOTEBOOK_CELLS, cell_id)
        if row:
            row["thread"]    = _jload(row.get("thread", "[]"))
            row["citations"] = _jload(row.get("citations", "[]"))
        return row

    # ── pages ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def save_page(page: dict) -> None:
        rec = {
            "id":           page["id"],
            "text":         f"Page: {page.get('title', 'Page')}",
            "notebook_id":  page["notebook_id"],
            "title":        page.get("title", "Page"),
            "sort_order":   page.get("sort_order", 0),
            "column_count": page.get("column_count", 1),
            "created_at":   page.get("created_at", _now_ts()),
            "updated_at":   page.get("updated_at", _now_ts()),
        }
        await _ingest_one(DATASET_NOTEBOOK_PAGES, rec, tags=["research", "page"])

    @staticmethod
    async def load_pages(notebook_id: str) -> list:
        rows = await _query_by_filter(DATASET_NOTEBOOK_PAGES, {"notebook_id": notebook_id}, limit=100)
        rows.sort(key=lambda x: _int_key(x.get("sort_order")))
        return rows

    @staticmethod
    async def delete_page(page_id: str) -> None:
        await _delete_by_filter(DATASET_NOTEBOOK_PAGES, {"id": page_id})
        # Unlink cells from this page
        cells = await _query_by_filter(DATASET_NOTEBOOK_CELLS, {"page_id": page_id}, limit=500)
        for c in cells:
            c["page_id"] = ""
            await _ingest_one(DATASET_NOTEBOOK_CELLS, c, tags=["research", "notebook", "cell"])

    # ── iteration targets ─────────────────────────────────────────────────────

    @staticmethod
    async def save_iteration_target(it: dict) -> None:
        rec = {
            "id":            it["id"],
            "text":          f"Iteration: {it.get('target_type', '')} {it.get('seed_query', '')[:60]}",
            "target_type":   it.get("target_type", "project"),
            "target_id":     it.get("target_id", ""),
            "status":        it.get("status", "paused"),
            "mode":          it.get("mode", "single"),
            "output_mode":   it.get("output_mode", "report"),
            "interval_secs": it.get("interval_secs", 300),
            "seed_query":    it.get("seed_query", ""),
            "traversal_map": _j(it.get("traversal_map", {})),
            "created_at":    it.get("created_at", _now_ts()),
            "updated_at":    it.get("updated_at", _now_ts()),
        }
        await _ingest_one(DATASET_ITERATION_TARGETS, rec, tags=["research", "iteration"])

    @staticmethod
    async def load_iteration_targets(status: Optional[str] = None) -> list:
        if status:
            rows = await _query_by_filter(DATASET_ITERATION_TARGETS, {"status": status}, limit=200)
        else:
            rows = await _query_by_filter(DATASET_ITERATION_TARGETS, {}, limit=200)
        rows.sort(key=lambda x: _ts_key(x.get("updated_at")), reverse=True)
        for r in rows:
            r["traversal_map"] = _jload(r.get("traversal_map", "{}"))
        return rows

    @staticmethod
    async def load_iteration_target(it_id: str) -> Optional[dict]:
        row = await _query_by_id(DATASET_ITERATION_TARGETS, it_id)
        if row:
            row["traversal_map"] = _jload(row.get("traversal_map", "{}"))
        return row

    @staticmethod
    async def delete_iteration_target(it_id: str) -> None:
        await _delete_by_filter(DATASET_ITERATION_TARGETS, {"id": it_id})

    # ── pipelines ─────────────────────────────────────────────────────────────

    @staticmethod
    async def save_pipeline(pl: dict) -> None:
        rec = {
            "id":          pl["id"],
            "text":        f"Pipeline: {pl.get('name', 'Untitled')}"[:TEXT_INDEX_LIMIT],
            "name":        pl.get("name", "Untitled Pipeline"),
            "description": pl.get("description", ""),
            "stages":      _j(pl.get("stages", [])),
            "tags":        _j(pl.get("tags", [])),
            "project_id":  pl.get("project_id") or "",
            "created_at":  pl.get("created_at", _now_ts()),
            "updated_at":  pl.get("updated_at", _now_ts()),
        }
        await _ingest_one(DATASET_PIPELINES, rec, tags=["research", "pipeline"])

    @staticmethod
    async def load_pipelines(project_id: Optional[str] = None) -> list:
        rows = await _query_by_filter(DATASET_PIPELINES, {}, limit=500)
        rows.sort(key=lambda x: _ts_key(x.get("updated_at")), reverse=True)
        out = []
        for r in rows:
            if project_id and r.get("project_id") != project_id:
                continue
            out.append({
                "id":          r.get("id", ""),
                "name":        r.get("name", "Untitled"),
                "description": r.get("description", ""),
                "stages":      _jload(r.get("stages", "[]")),
                "tags":        _jload(r.get("tags", "[]")),
                "project_id":  r.get("project_id", ""),
                "created_at":  _ts_key(r.get("created_at")),
                "updated_at":  _ts_key(r.get("updated_at")),
            })
        return out

    @staticmethod
    async def load_pipeline(pl_id: str) -> Optional[dict]:
        rows = await _query_by_filter(DATASET_PIPELINES, {"id": pl_id}, limit=2)
        if not rows:
            return None
        r = rows[0]
        return {
            "id":          r.get("id", ""),
            "name":        r.get("name", "Untitled"),
            "description": r.get("description", ""),
            "stages":      _jload(r.get("stages", "[]")),
            "tags":        _jload(r.get("tags", "[]")),
            "project_id":  r.get("project_id", ""),
            "created_at":  _ts_key(r.get("created_at")),
            "updated_at":  _ts_key(r.get("updated_at")),
        }

    @staticmethod
    async def delete_pipeline(pl_id: str) -> None:
        await _delete_by_filter(DATASET_PIPELINES, {"id": pl_id})

    @staticmethod
    async def save_pipeline_run(run: dict) -> None:
        rec = {
            "id":          run["id"],
            "text":        f"Pipeline run: {run.get('pipeline_name', '')}"[:TEXT_INDEX_LIMIT],
            "pipeline_id": run.get("pipeline_id", ""),
            "pipeline_name": run.get("pipeline_name", ""),
            "status":      run.get("status", "queued"),
            "stages":      _j(run.get("stages", [])),
            "final_result": run.get("final_result", "")[:60000],
            "job_ids":     _j(run.get("job_ids", [])),
            "error":       run.get("error", ""),
            "created_at":  run.get("created_at", _now_ts()),
            "updated_at":  run.get("updated_at", _now_ts()),
        }
        await _ingest_one(DATASET_PIPELINE_RUNS, rec, tags=["research", "pipeline_run"])

    @staticmethod
    async def load_pipeline_runs(pipeline_id: Optional[str] = None,
                                 limit: int = 50) -> list:
        rows = await _query_by_filter(DATASET_PIPELINE_RUNS, {}, limit=500)
        rows.sort(key=lambda x: _ts_key(x.get("created_at")), reverse=True)
        out = []
        for r in rows:
            if pipeline_id and r.get("pipeline_id") != pipeline_id:
                continue
            out.append({
                "id":            r.get("id", ""),
                "pipeline_id":   r.get("pipeline_id", ""),
                "pipeline_name": r.get("pipeline_name", ""),
                "status":        r.get("status", "unknown"),
                "stages":        _jload(r.get("stages", "[]")),
                "final_result":  r.get("final_result", ""),
                "job_ids":       _jload(r.get("job_ids", "[]")),
                "error":         r.get("error", ""),
                "created_at":    _ts_key(r.get("created_at")),
                "updated_at":    _ts_key(r.get("updated_at")),
            })
            if len(out) >= limit:
                break
        return out

    @staticmethod
    async def load_pipeline_run(run_id: str) -> Optional[dict]:
        rows = await _query_by_filter(DATASET_PIPELINE_RUNS, {"id": run_id}, limit=2)
        if not rows:
            return None
        r = rows[0]
        return {
            "id":            r.get("id", ""),
            "pipeline_id":   r.get("pipeline_id", ""),
            "pipeline_name": r.get("pipeline_name", ""),
            "status":        r.get("status", "unknown"),
            "stages":        _jload(r.get("stages", "[]")),
            "final_result":  r.get("final_result", ""),
            "job_ids":       _jload(r.get("job_ids", "[]")),
            "error":         r.get("error", ""),
            "created_at":    _ts_key(r.get("created_at")),
            "updated_at":    _ts_key(r.get("updated_at")),
        }

    @staticmethod
    async def delete_pipeline_run(run_id: str) -> None:
        await _delete_by_filter(DATASET_PIPELINE_RUNS, {"id": run_id})

    # ── stats ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def get_stats() -> dict:
        projs_n = await _count_dataset(DATASET_PROJECTS)
        cits_n  = await _count_dataset(DATASET_CITATIONS)
        # Use deduped query for jobs — raw COUNT(*) includes stale duplicate rows
        all_jobs = await _query_by_filter(DATASET_JOBS, {}, limit=5000)
        jobs_n   = len(all_jobs)
        total_tokens = sum(_int_key(r.get("token_count", 0)) for r in all_jobs)
        last = max(all_jobs, key=lambda x: _ts_key(x.get("created_at"))) if all_jobs else {}

        fab = _fabric()
        db_size = 0
        if fab and hasattr(fab, "SQLITE_PATH"):
            import os
            try:
                db_size = os.path.getsize(fab.SQLITE_PATH)
            except Exception:
                pass

        return {
            "total_jobs":      jobs_n,
            "total_projects":  projs_n,
            "total_citations": cits_n,
            "total_tokens":    total_tokens,
            "last_query":      last.get("query", ""),
            "last_at":         _ts_key(last.get("created_at")),
            "db_backend":      "fabric",
            "db_size_bytes":   db_size,
        }

    # ── export ────────────────────────────────────────────────────────────────

    @staticmethod
    async def export_all(limit: int = 500) -> dict:
        rows, _ = await DB.load_history(limit=limit)
        projs   = await DB.load_projects()
        srcs    = await DB.load_sources()
        ws_cfg  = await DB.load_web_search_config()
        bmarks  = await DB.load_bookmarks()
        return {
            "jobs":              rows,
            "projects":          projs,
            "sources":           srcs,
            "bookmarks":         bmarks,
            "web_search_config": ws_cfg,
            "exported_at":       _now_ts(),
            "db_backend":        "fabric",
        }


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPER
# ─────────────────────────────────────────────────────────────────────────────

async def _count_by_parent(dataset_id: str, key: str, value: str) -> int:
    """Count records in a dataset where data[key] == value."""
    rows = await _query_by_filter(dataset_id, {key: value}, limit=10000)
    return len(rows)


# ═══════════════════════════════════════════════════════════════════════════════
# SEMANTIC RECALL
# ═══════════════════════════════════════════════════════════════════════════════

async def recall_research(
    query:        str,
    dataset_id:   Optional[str] = None,
    top_k:        int = 20,
    include_data: bool = True,
) -> Dict[str, Any]:
    """Fusion search across research datasets. Single dataset if specified."""
    fab = _fabric()
    if not fab or not hasattr(fab, "execute_query"):
        return {"ok": False, "reason": "fabric unavailable", "results": []}

    if dataset_id:
        q = {"vector": query, "text": query, "dataset_id": dataset_id,
             "top_k": top_k, "include_data": include_data, "cache": False}
        try:
            res = await fab.execute_query(q)
            return {"ok": True, "results": res.get("results", []),
                    "count": res.get("count", 0), "dataset_id": dataset_id,
                    "backends": res.get("backends", [])}
        except Exception as e:
            return {"ok": False, "reason": str(e), "results": []}

    # Multi-dataset fusion
    merged: List[Dict] = []
    seen_ids: set = set()
    backends_used: set = set()
    per_ds = max(3, top_k // max(1, len(RECALL_DATASETS)) + 2)

    for ds in RECALL_DATASETS:
        try:
            res = await fab.execute_query({
                "vector": query, "text": query, "dataset_id": ds,
                "top_k": per_ds, "include_data": include_data, "cache": False,
            })
            for r in (res.get("results") or []):
                rid = r.get("id") or r.get("record_id") or ""
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
                r["_dataset_id"] = ds
                merged.append(r)
            for b in (res.get("backends") or []):
                backends_used.add(b)
        except Exception as e:
            log.debug("recall_research [%s]: %s", ds, e)
            continue

    def _score(r):
        return r.get("score", r.get("rank", 0.0)) or 0.0
    merged.sort(key=_score, reverse=True)
    merged = merged[:top_k]

    return {"ok": True, "results": merged, "count": len(merged),
            "datasets": RECALL_DATASETS, "backends": sorted(backends_used)}


async def recall_by_filter(
    dataset_id: str, filters: Dict[str, Any], limit: int = 50,
) -> Dict[str, Any]:
    """Pull records by exact filter. Wraps _query_by_filter with ok/results shape."""
    results = await _query_by_filter(dataset_id, filters, limit)
    return {"ok": True, "results": results, "count": len(results),
            "dataset_id": dataset_id, "filters": filters}


# ═══════════════════════════════════════════════════════════════════════════════
# JOB / NOTEBOOK / SESSION HYDRATION
# ═══════════════════════════════════════════════════════════════════════════════

async def get_research_job(job_id: str) -> Dict[str, Any]:
    """Reconstruct a full research job from fabric records."""
    if not job_id:
        return {"ok": False, "reason": "job_id required"}
    out: Dict[str, Any] = {"ok": True, "job_id": job_id}
    job_rows = await _query_by_filter(DATASET_JOBS, {"job_id": job_id}, limit=5)
    out["job"] = job_rows[0] if job_rows else {}
    res_rows = await _query_by_filter(DATASET_RESULTS, {"job_id": job_id}, limit=5)
    out["result"] = res_rows[0] if res_rows else {}
    out["citations"] = await _query_by_filter(DATASET_CITATIONS, {"job_id": job_id}, limit=200)
    out["crawl_pages"] = await _query_by_filter(DATASET_CRAWL_PAGES, {"job_id": job_id}, limit=200)
    out["searches"] = await _query_by_filter(DATASET_SEARCHES, {"job_id": job_id}, limit=50)
    out["llm_calls"] = await _query_by_filter(DATASET_LLM_CALLS, {"job_id": job_id}, limit=200)
    out["counts"] = {k: len(out[k]) for k in ("citations", "crawl_pages", "searches", "llm_calls")}
    return out

async def get_research_notebook(notebook_id: str) -> Dict[str, Any]:
    """Pull notebook + all cells from fabric."""
    if not notebook_id:
        return {"ok": False, "reason": "notebook_id required"}
    nbs = await _query_by_filter(DATASET_NOTEBOOKS, {"notebook_id": notebook_id}, limit=5)
    nb = nbs[0] if nbs else {}
    cells = await _query_by_filter(DATASET_NOTEBOOK_CELLS, {"notebook_id": notebook_id}, limit=500)
    cells.sort(key=lambda c: (_int_key(c.get("sort_order")), _ts_key(c.get("created_at"))))
    return {"ok": True, "notebook_id": notebook_id, "notebook": nb,
            "cells": cells, "cell_count": len(cells)}

async def get_research_session(session_id: str) -> Dict[str, Any]:
    """Pull session + all jobs in this session."""
    if not session_id:
        return {"ok": False, "reason": "session_id required"}
    sess_rows = await _query_by_filter(DATASET_SESSIONS, {"session_id": session_id}, limit=5)
    sess = sess_rows[0] if sess_rows else {}
    jobs = await _query_by_filter(DATASET_JOBS, {"session_id": session_id}, limit=100)
    jobs.sort(key=lambda j: _ts_key(j.get("created_at")))
    return {"ok": True, "session_id": session_id, "session": sess,
            "jobs": jobs, "job_count": len(jobs)}


# ═══════════════════════════════════════════════════════════════════════════════
# DATASET LISTING
# ═══════════════════════════════════════════════════════════════════════════════

async def list_research_datasets() -> Dict[str, Any]:
    """List research/web datasets in the fabric."""
    cap_reg = _registry()
    cap = cap_reg.get("fabric.datasets")
    if not cap:
        return {"ok": False, "reason": "fabric.datasets cap unavailable", "datasets": []}
    try:
        res = await cap["func"]()
        all_ds = (res.get("datasets") or []) if isinstance(res, dict) else []
        keep = [d for d in all_ds
                if (d.get("dataset_id") or "").startswith(("research.", "web."))]
        return {"ok": True, "datasets": keep, "count": len(keep)}
    except Exception as e:
        return {"ok": False, "reason": str(e), "datasets": []}


# ═══════════════════════════════════════════════════════════════════════════════
# INGEST HELPERS (for researcher_api pipeline events)
# ═══════════════════════════════════════════════════════════════════════════════

async def ingest_research_record(
    dataset_id: str, item: Dict, source: str = "research",
    tags: Optional[List[str]] = None,
) -> Dict:
    """Convenience: ingest a single shaped record."""
    return await _ingest_one(dataset_id, item, source, tags)

async def ingest_research_records(
    dataset_id: str, items: List[Dict], source: str = "research",
    tags: Optional[List[str]] = None,
) -> Dict:
    """Batch ingest multiple shaped records."""
    return await _ingest(dataset_id, items, source, tags)

async def ingest_search(
    job_id: str, query: str, engine: str, results: list,
    session_id: str = "",
) -> Dict:
    """Record a search event in the fabric."""
    rec = shape_record(
        text=f"Search [{engine}]: {query}",
        full_text=json.dumps(results[:20], default=str)[:30000],
        job_id=job_id, session_id=session_id,
        extra={"engine": engine, "query": query,
               "result_count": len(results)},
        tags=["research", "search", engine],
    )
    return await _ingest_one(DATASET_SEARCHES, rec, tags=["research", "search"])

async def ingest_crawl(
    url: str, title: str, full_text: str, domain: str = "",
    job_id: str = "", session_id: str = "", parent_url: str = "",
) -> Dict:
    """Record a crawled page."""
    rec = shape_record(
        text=f"{title}\n{full_text[:1000]}"[:TEXT_INDEX_LIMIT],
        full_text=full_text[:50000],
        url=url, title=title, domain=domain,
        job_id=job_id, session_id=session_id,
        extra={"parent_url": parent_url},
        tags=["research", "crawl", domain] if domain else ["research", "crawl"],
    )
    return await _ingest_one(DATASET_CRAWL_PAGES, rec, tags=["research", "crawl"])

async def ingest_llm_call(
    job_id: str, tier: str, model: str, instance: str,
    prompt_chars: int, response_chars: int, elapsed_ms: int,
    session_id: str = "",
) -> Dict:
    """Record an LLM call."""
    rec = shape_record(
        text=f"LLM [{tier}/{model}]: {prompt_chars} -> {response_chars} chars in {elapsed_ms}ms",
        job_id=job_id, session_id=session_id,
        extra={"tier": tier, "model": model, "instance": instance,
               "prompt_chars": prompt_chars, "response_chars": response_chars,
               "elapsed_ms": elapsed_ms},
        tags=["research", "llm", tier],
    )
    return await _ingest_one(DATASET_LLM_CALLS, rec, tags=["research", "llm"])

async def ingest_citation(
    job_id: str, url: str, title: str, snippet: str,
    full_text: str = "", source_type: str = "web",
    session_id: str = "",
) -> Dict:
    """Record a citation discovery (used by bridge/activity tracking)."""
    rec = shape_record(
        text=f"{title}: {snippet[:200]}"[:TEXT_INDEX_LIMIT],
        full_text=full_text or snippet,
        url=url, title=title, job_id=job_id, session_id=session_id,
        extra={"source_type": source_type, "snippet": snippet},
        tags=["research", "citation", source_type],
    )
    return await _ingest_one(DATASET_CITATIONS, rec, tags=["research", "citation"])


# ═══════════════════════════════════════════════════════════════════════════════
# ACTIVITY RECORDING (replaces research_activity_capabilities.py inline calls)
# ═══════════════════════════════════════════════════════════════════════════════

async def record_activity(
    *,
    session_id:      str,
    category:        str,
    text:            str,
    full_text:       str = "",
    tags:            Optional[List[str]] = None,
    importance:      float = 0.5,
    capability_name: str = "",
    fabric_dataset:  str = "",
    fabric_data:     Optional[Dict] = None,
    job_id:          str = "",
    parent_node_id:  str = "",
) -> str:
    """
    Record a research activity event on the memory graph + fabric.
    Returns the node_id of the created memory node.
    """
    node_id = uuid.uuid4().hex[:16]

    # 1. Memory graph
    hooks = _memory_hooks()
    if hooks and hasattr(hooks, "_record"):
        try:
            await hooks._record(
                text=text,
                category=category,
                tags=tags or [],
                importance=importance,
                session_id=session_id,
                node_id=node_id,
                source_type="tool",
                record_type="event",
                capability=capability_name,
            )
        except Exception as e:
            log.debug("record_activity graph: %s", e)

    # 2. Parent link
    if parent_node_id and hooks and hasattr(hooks, "_link"):
        try:
            await hooks._link(parent_node_id, node_id, "FOLLOWS_ACTIVITY",
                             session_id=session_id)
        except Exception as e:
            log.debug("record_activity link: %s", e)

    # 3. Fabric
    if fabric_dataset:
        try:
            rec = shape_record(
                text=text, full_text=full_text or text,
                record_id=node_id, job_id=job_id,
                session_id=session_id,
                extra=fabric_data, tags=tags,
            )
            await _ingest_one(fabric_dataset, rec, source="research_activity", tags=tags)
        except Exception as e:
            log.debug("record_activity fabric [%s]: %s", fabric_dataset, e)

    # 4. Event
    _emit_event({
        "type":       f"{category}.recorded",
        "node_id":    node_id,
        "session_id": session_id,
        "job_id":     job_id,
        "category":   category,
        "text":       text[:200],
    })
    return node_id