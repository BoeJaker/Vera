"""
curation_capabilities.py — Curated-dataset layer on the fabric backbone
=======================================================================

Phase 1 of the "reusable, consistent, quality-controlled datasets" work. This
does NOT introduce a new agent-facing namespace — the data fabric already has
three clean layers and we extend along them instead of muddying them with a
fourth:

  • fabric.*  — the data plane (store, vectors, pipelines, tags, graphs). The
                WRITE / identity / schema / quality mechanics belong here.
  • memory.*  — the agent-facing READ surface over the fabric (seek/browse/read).
                The typed field-level SELECT belongs here.
  • context.* — assembly (recall/assemble). The trust-ranked unification of
                datasets-above-memories lands here (a later phase).

So the caps this module registers live under fabric.* and memory.* — the
namespace a cap advertises is its home, regardless of which file defines it.

What Phase 1 adds
─────────────────
  fabric.upsert         keyed dedup / merge (gap-fill) / append (timeseries)
  fabric.schema.declare declared, VERSIONED schema for a dataset
  fabric.schema.get     declared schema (falls back to inferred)
  fabric.validate       quality / coverage / trust check vs the declared schema
  memory.select         field-level filter + sort over a dataset's rows

The store itself is unchanged: keyed upsert reuses the existing ingest pipeline
(embeddings + vectors + post-ingest) via a one-line `_id` hook in
ingest_dataset — a re-ingested business key REPLACES its row (INSERT OR REPLACE
on the PK) instead of appending a duplicate. DataRecord already carried the
content_hash / version / updated_at fields this needs; they were just unwired.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from typing import Any, Dict, List, Optional

from Vera.vera.capability_orchestration import (
    CAPABILITY_REGISTRY,
    capability,
    emit_event,
    now_iso,
)
from Vera.vera.fabric.curation_core import (
    FIELD_RE as _FIELD_RE,
    MAX_FILL_ATTEMPTS,
    backoff_cooldown,
    build_select_sql,
    compute_field_gaps,
    compute_key_gaps,
    gap_actionable,
    gap_id as _gap_id,
    join_rows,
    key_id as _key_id,
    match_score,
    merge_row,
    normalise_schema,
    rank_context_datasets,
    validate_rows,
)

log = logging.getLogger("vera.fabric_curation")


def _df():
    """The LIVE data_fabric module instance (loaded by the module loader)."""
    return (sys.modules.get("data_fabric")
            or sys.modules.get("Vera.vera.fabric.data_fabric"))


def _conn():
    df = _df()
    if not df:
        raise RuntimeError("data_fabric not loaded")
    return df._sqlite_conn()


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA STORE — declared, versioned schemas per dataset
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_schema_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fabric_dataset_schema (
            dataset_id  TEXT PRIMARY KEY,
            schema      TEXT DEFAULT '{}',
            key_fields  TEXT DEFAULT '[]',
            kind        TEXT DEFAULT 'table',
            trust       REAL DEFAULT 0.6,
            authority   TEXT DEFAULT 'curated',
            version     INTEGER DEFAULT 1,
            updated_at  TEXT
        )
    """)


def _load_schema_sync(dataset_id: str) -> Optional[Dict[str, Any]]:
    conn = _conn()
    try:
        _ensure_schema_table(conn)
        row = conn.execute(
            "SELECT schema, key_fields, kind, trust, authority, version, updated_at "
            "FROM fabric_dataset_schema WHERE dataset_id=?", (dataset_id,)).fetchone()
        if not row:
            return None
        return {
            "schema":     json.loads(row["schema"] or "{}"),
            "key":        json.loads(row["key_fields"] or "[]"),
            "kind":       row["kind"] or "table",
            "trust":      row["trust"] if row["trust"] is not None else 0.6,
            "authority":  row["authority"] or "curated",
            "version":    int(row["version"] or 1),
            "updated_at": row["updated_at"] or "",
        }
    finally:
        conn.close()


def _save_schema_sync(dataset_id: str, *, schema: Dict[str, Any], key: List[str],
                      kind: str, trust: float, authority: str) -> Dict[str, Any]:
    conn = _conn()
    try:
        _ensure_schema_table(conn)
        prev = conn.execute(
            "SELECT version FROM fabric_dataset_schema WHERE dataset_id=?",
            (dataset_id,)).fetchone()
        version = (int(prev["version"]) + 1) if prev else 1
        conn.execute(
            "INSERT OR REPLACE INTO fabric_dataset_schema "
            "(dataset_id, schema, key_fields, kind, trust, authority, version, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (dataset_id, json.dumps(schema), json.dumps(key), kind,
             float(trust), authority, version, now_iso()))
        conn.commit()
        return {"version": version}
    finally:
        conn.close()


async def get_dataset_schema(dataset_id: str) -> Optional[Dict[str, Any]]:
    """Public helper: the declared schema for a dataset, or None. Used by later
    phases (identify / gaps / context unification)."""
    return await asyncio.to_thread(_load_schema_sync, dataset_id)


# ─────────────────────────────────────────────────────────────────────────────
# KEYED UPSERT
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_existing_sync(ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not ids:
        return {}
    conn = _conn()
    try:
        out: Dict[str, Dict[str, Any]] = {}
        # Chunk the IN() to stay under SQLite's variable limit.
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            q = ("SELECT id, data FROM fabric_records WHERE id IN (%s)"
                 % ",".join("?" * len(chunk)))
            for r in conn.execute(q, chunk).fetchall():
                try:
                    out[r["id"]] = json.loads(r["data"] or "{}")
                except Exception:
                    out[r["id"]] = {}
        return out
    finally:
        conn.close()


def _count_sync(dataset_id: str) -> int:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM fabric_records WHERE dataset_id=?",
            (dataset_id,)).fetchone()
        return int(row["c"]) if row else 0
    finally:
        conn.close()


@capability(
    "fabric.upsert", memory="off",
    http_method="POST", http_path="/fabric/upsert", http_tags=["fabric"],
    description="Ingest rows into a dataset WITH IDENTITY so re-ingesting the "
                "same business key does not duplicate. WHEN TO USE: whenever you "
                "collect data you may fetch again (a pokedex, a price series, a "
                "catalogue) — call this instead of fabric.ingest so the dataset "
                "stays consistent and reusable. "
                "Inputs: dataset_id (str!), rows (JSON array of objects), "
                "key (csv of field names forming the business key — e.g. 'id' or "
                "'symbol,date'; required for merge/replace, optional for append), "
                "mode ('merge'|'append'|'replace', default merge): "
                "merge = update-in-place and FILL missing fields (gap-fill); "
                "append = add rows, still de-duplicating exact key repeats; "
                "replace = overwrite the keyed row wholesale. "
                "source (str), tags (csv). "
                "Output: {dataset_id, upserted, new, updated, record_count, mode}.",
)
async def cap_fabric_upsert(dataset_id: str = "", rows: Any = None,
                            key: str = "", mode: str = "merge",
                            source: str = "upsert", tags: str = "",
                            trace_id=None) -> Dict[str, Any]:
    df = _df()
    if not df:
        return {"error": "data_fabric not loaded"}
    if not dataset_id:
        return {"error": "dataset_id required"}
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except Exception:
            return {"error": "rows must be valid JSON"}
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not rows:
        return {"error": "rows must be a non-empty JSON array of objects"}

    key_fields = [k.strip() for k in (key or "").split(",") if k.strip()]
    mode = (mode or "merge").strip().lower()
    if mode not in ("merge", "append", "replace"):
        mode = "merge"
    if mode in ("merge", "replace") and not key_fields:
        return {"error": f"mode={mode} needs key= (csv of field names)"}

    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]

    # Build the id-carrying rows. When keyed, compute a deterministic id so the
    # ingest INSERT OR REPLACE overwrites the row for that key.
    prepared: List[Dict[str, Any]] = []
    ids: List[str] = []
    for row in rows:
        if not isinstance(row, dict):
            row = {"value": row}
        r = dict(row)
        if key_fields:
            rid = _key_id(dataset_id, r, key_fields)
            r["_id"] = rid
            ids.append(rid)
        prepared.append(r)

    existing = await asyncio.to_thread(_fetch_existing_sync, ids) if ids else {}
    new_ct = sum(1 for i in ids if i not in existing) if ids else len(prepared)
    updated_ct = (len(ids) - new_ct) if ids else 0

    # merge → fill/overwrite existing with the incoming NON-EMPTY fields, so a
    # partial fetch tops up a fuller stored row rather than clobbering it.
    if mode == "merge" and existing:
        for r in prepared:
            rid = r.get("_id")
            prev = existing.get(rid)
            if prev:
                merged = merge_row(prev, r)
                merged["_id"] = rid
                r.clear()
                r.update(merged)

    res = await df.ingest_dataset(dataset_id, prepared, source=source,
                                  tags=tag_list)
    # Recompute record_count accurately (INSERT OR REPLACE means the ingest's
    # own +N increment over-counts replaced rows).
    real_count = await asyncio.to_thread(_count_sync, dataset_id)
    try:
        await df._enqueue_write({"kind": "raw",
            "sql": "UPDATE fabric_datasets SET record_count=?, updated_at=? WHERE dataset_id=?",
            "params": (real_count, now_iso(), dataset_id)}, wait=False)
    except Exception as e:
        log.debug("fabric.upsert count fix failed: %s", e)

    await emit_event({"type": "fabric.upserted", "dataset_id": dataset_id,
                      "mode": mode, "new": new_ct, "updated": updated_ct})
    return {"dataset_id": dataset_id, "upserted": res.get("ingested", 0),
            "new": new_ct, "updated": updated_ct, "record_count": real_count,
            "mode": mode, "errors": res.get("errors", 0)}


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA CAPS
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "fabric.schema.declare", memory="off",
    http_method="POST", http_path="/fabric/schema/declare", http_tags=["fabric"],
    description="Declare (and version) the schema for a dataset so agents and "
                "loops can operate on it reliably and quality can be checked. "
                "Inputs: dataset_id (str!), schema (object mapping field -> "
                "{type: string|number|integer|boolean|array|object, required: "
                "bool, description: str}), key (csv of the field(s) forming the "
                "business key), kind (table|timeseries|document|kv, default "
                "table), trust (float 0..1, default 0.6 — curated data is more "
                "authoritative than free-text memory), authority (str tag, "
                "default 'curated'). Bumps the stored version on each change. "
                "Output: {dataset_id, version, schema, key, kind, trust}.",
)
async def cap_fabric_schema_declare(dataset_id: str = "", schema: Any = None,
                                    key: str = "", kind: str = "table",
                                    trust: float = 0.6, authority: str = "curated",
                                    trace_id=None) -> Dict[str, Any]:
    if not dataset_id:
        return {"error": "dataset_id required"}
    if isinstance(schema, str):
        try:
            schema = json.loads(schema) if schema.strip() else {}
        except Exception:
            return {"error": "schema must be a valid JSON object"}
    if schema is None:
        schema = {}
    if not isinstance(schema, dict):
        return {"error": "schema must be an object mapping field -> spec"}
    key_fields = [k.strip() for k in (key or "").split(",") if k.strip()]
    try:
        norm = normalise_schema(schema, key_fields)
    except ValueError as e:
        return {"error": str(e)}
    try:
        trust = max(0.0, min(1.0, float(trust)))
    except Exception:
        trust = 0.6
    saved = await asyncio.to_thread(
        _save_schema_sync, dataset_id, schema=norm, key=key_fields,
        kind=(kind or "table"), trust=trust, authority=(authority or "curated"))
    await emit_event({"type": "fabric.schema.declared", "dataset_id": dataset_id,
                      "version": saved["version"]})
    return {"dataset_id": dataset_id, "version": saved["version"], "schema": norm,
            "key": key_fields, "kind": kind or "table", "trust": trust,
            "authority": authority or "curated"}


@capability(
    "fabric.schema.get", memory="off", silent=True,
    http_method="GET", http_path="/fabric/schema/get", http_tags=["fabric"],
    description="Get a dataset's DECLARED schema (with key/kind/trust/version). "
                "Falls back to an INFERRED schema (sampled from rows) when none "
                "has been declared, so agents always get something to work with. "
                "Input: dataset_id (str!). "
                "Output: {dataset_id, declared (bool), schema, key, kind, trust, "
                "version}.",
)
async def cap_fabric_schema_get(dataset_id: str = "", trace_id=None) -> Dict[str, Any]:
    if not dataset_id:
        return {"error": "dataset_id required"}
    declared = await get_dataset_schema(dataset_id)
    if declared:
        return {"dataset_id": dataset_id, "declared": True, **declared}
    # Fall back to the existing inferred schema.
    infer = CAPABILITY_REGISTRY.get("fabric.schema")
    inferred: Dict[str, Any] = {}
    if infer and infer.get("func"):
        try:
            r = await infer["func"](dataset_id=dataset_id)
            inferred = r.get("schema", {}) if isinstance(r, dict) else {}
        except Exception:
            inferred = {}
    # Shape it like a declared schema (type only, nothing required/keyed).
    shaped = {f: {"type": (t[0] if isinstance(t, list) and t else "string"),
                  "required": False} for f, t in inferred.items()}
    return {"dataset_id": dataset_id, "declared": False, "schema": shaped,
            "key": [], "kind": "table", "trust": 0.3, "version": 0}


# ─────────────────────────────────────────────────────────────────────────────
# memory.select — field-level filter + sort
# ─────────────────────────────────────────────────────────────────────────────

def _select_sync(dataset_id: str, where, sort, fields, limit, offset):
    conn = _conn()
    try:
        sql, params = build_select_sql(dataset_id, where, sort, limit, offset)
        rows = conn.execute(sql, params).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                data = json.loads(r["data"] or "{}")
            except Exception:
                data = {}
            if fields:
                data = {k: data.get(k) for k in fields}
            out.append({"id": r["id"], "created_at": r["created_at"], "data": data})
        return out
    finally:
        conn.close()


@capability(
    "memory.select", memory="off",
    http_method="POST", http_path="/memory/select", http_tags=["memory", "fabric"],
    description="Filter and sort a dataset's rows by FIELD VALUES — the typed "
                "read to complement memory.seek's semantic search. WHEN TO USE: "
                "you know the dataset and want specific rows (a symbol's price "
                "series in a date range; pokedex entries of a type; the newest N). "
                "Inputs: dataset_id (str!), where (list of {field, op, value}; op "
                "in eq/ne/gt/gte/lt/lte/contains/startswith/endswith/in/exists), "
                "sort (list of {field, dir:asc|desc}), fields (csv/list — project "
                "only these fields), limit (int default 100, max 2000), offset "
                "(int). Numeric comparisons work when the field is stored numeric. "
                "Output: {dataset_id, rows:[{id,created_at,data}], count}.",
)
async def cap_memory_select(dataset_id: str = "", where: Any = None,
                            sort: Any = None, fields: Any = None,
                            limit: int = 100, offset: int = 0,
                            trace_id=None) -> Dict[str, Any]:
    if not _df():
        return {"error": "data_fabric not loaded"}
    if not dataset_id:
        return {"error": "dataset_id required"}
    if isinstance(where, str):
        try: where = json.loads(where) if where.strip() else []
        except Exception: return {"error": "where must be valid JSON"}
    if isinstance(sort, str):
        try: sort = json.loads(sort) if sort.strip() else []
        except Exception: return {"error": "sort must be valid JSON"}
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.split(",") if f.strip()]
    where = where if isinstance(where, list) else ([] if where is None else [where])
    sort = sort if isinstance(sort, list) else ([] if sort is None else [sort])
    fields = [f for f in (fields or []) if _FIELD_RE.match(str(f))]
    try:
        limit = max(1, min(2000, int(limit)))
        offset = max(0, int(offset))
    except Exception:
        limit, offset = 100, 0
    try:
        rows = await asyncio.to_thread(_select_sync, dataset_id, where, sort,
                                       fields, limit, offset)
    except Exception as e:
        return {"error": f"select failed: {e}"}
    return {"dataset_id": dataset_id, "rows": rows, "count": len(rows)}


# ─────────────────────────────────────────────────────────────────────────────
# fabric.validate — quality / coverage / trust vs the declared schema
# ─────────────────────────────────────────────────────────────────────────────

def _validate_sync(dataset_id: str, sample_limit: int) -> Dict[str, Any]:
    conn = _conn()
    try:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM fabric_records WHERE dataset_id=?",
            (dataset_id,)).fetchone()["c"]
        rows = conn.execute(
            "SELECT id, data FROM fabric_records WHERE dataset_id=? LIMIT ?",
            (dataset_id, sample_limit)).fetchall()
    finally:
        conn.close()
    parsed = []
    for r in rows:
        try:
            parsed.append(json.loads(r["data"] or "{}"))
        except Exception:
            parsed.append({})
    return {"total": int(total), "sampled": len(parsed), "rows": parsed}


@capability(
    "fabric.validate", memory="off",
    http_method="POST", http_path="/fabric/validate", http_tags=["fabric"],
    description="Quality-check a dataset against its DECLARED schema so its data "
                "can be trusted. Reports required-field violations, type "
                "mismatches, per-field coverage (non-null fraction) and duplicate "
                "business keys, and a 0..1 quality score. "
                "Inputs: dataset_id (str!), sample_limit (int default 2000 — rows "
                "scanned). Output: {dataset_id, declared, total, sampled, "
                "quality_score, trust, coverage, missing_required, type_errors, "
                "duplicate_keys, ok}.",
)
async def cap_fabric_validate(dataset_id: str = "", sample_limit: int = 2000,
                              trace_id=None) -> Dict[str, Any]:
    if not _df():
        return {"error": "data_fabric not loaded"}
    if not dataset_id:
        return {"error": "dataset_id required"}
    schema_rec = await get_dataset_schema(dataset_id)
    try:
        sample_limit = max(1, min(20000, int(sample_limit)))
    except Exception:
        sample_limit = 2000
    scan = await asyncio.to_thread(_validate_sync, dataset_id, sample_limit)
    rows = scan["rows"]

    schema = (schema_rec or {}).get("schema", {})
    key_fields = (schema_rec or {}).get("key", [])
    trust = (schema_rec or {}).get("trust", 0.3)

    scored = validate_rows(rows, schema, key_fields, declared_trust=trust)
    result = {
        "dataset_id": dataset_id, "declared": bool(schema_rec),
        "total": scan["total"], "sampled": scored["sampled"],
        "quality_score": scored["quality_score"], "trust": scored["trust"],
        "coverage": scored["coverage"], "missing_required": scored["missing_required"],
        "type_errors": scored["type_errors"], "duplicate_keys": scored["duplicate_keys"],
        "ok": scored["ok"],
    }
    await emit_event({"type": "fabric.validated", "dataset_id": dataset_id,
                      "quality_score": scored["quality_score"], "ok": scored["ok"]})
    return result


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 2 — identify, gaps (+ noise/backoff suppression), fusion
# ═════════════════════════════════════════════════════════════════════════════

import time as _time


def _dataset_fields_sync(dataset_id: str) -> List[str]:
    """Declared schema fields for a dataset (cheap — no row sampling)."""
    rec = _load_schema_sync(dataset_id)
    return list((rec or {}).get("schema", {}).keys())


def _dataset_tags_sync(conn, dataset_id: str) -> List[str]:
    try:
        return [r["tag"] for r in conn.execute(
            "SELECT tag FROM fabric_dataset_tags WHERE dataset_id=?",
            (dataset_id,)).fetchall()]
    except Exception:
        return []


def _identify_candidates_sync(subject_tokens: List[str], want_tags: List[str],
                              limit: int) -> List[Dict[str, Any]]:
    """Prefilter datasets that plausibly match (id contains a subject token, or
    shares a wanted tag), then annotate with declared fields + tags. Kept cheap
    so identify never scans thousands of datasets."""
    conn = _conn()
    try:
        ids: Dict[str, int] = {}
        # id LIKE %token%
        for tok in subject_tokens[:6]:
            for r in conn.execute(
                "SELECT dataset_id, record_count FROM fabric_datasets "
                "WHERE dataset_id LIKE ? LIMIT ?", (f"%{tok}%", limit)).fetchall():
                ids[r["dataset_id"]] = r["record_count"] or 0
        # datasets carrying a wanted tag
        if want_tags:
            ph = ",".join("?" * len(want_tags))
            for r in conn.execute(
                f"SELECT DISTINCT dataset_id FROM fabric_dataset_tags "
                f"WHERE tag IN ({ph}) LIMIT ?",
                [*[t.lower() for t in want_tags], limit]).fetchall():
                ids.setdefault(r["dataset_id"], 0)
        # If nothing matched by token/tag, fall back to a small recent slice so
        # the caller still gets *some* candidates to consider.
        if not ids:
            for r in conn.execute(
                "SELECT dataset_id, record_count FROM fabric_datasets "
                "ORDER BY updated_at DESC LIMIT ?", (min(limit, 50),)).fetchall():
                ids[r["dataset_id"]] = r["record_count"] or 0
        out = []
        for did, rc in ids.items():
            out.append({"dataset_id": did, "record_count": rc,
                        "fields": _dataset_fields_sync(did),
                        "tags": _dataset_tags_sync(conn, did)})
        return out
    finally:
        conn.close()


@capability(
    "fabric.identify", memory="off",
    http_method="POST", http_path="/fabric/identify", http_tags=["fabric"],
    description="Recognise whether the fabric ALREADY has a dataset for what you "
                "are about to fetch, so you reuse it instead of re-collecting. "
                "WHEN TO USE: before any web/API fetch of reference data — 'do we "
                "already have the gen-1 pokedex?'. Matches on dataset-id/subject "
                "tokens, declared-schema field overlap and tag overlap. "
                "Inputs: subject (str — what you want, e.g. 'pokedex gen1'), "
                "expected_fields (csv/list — fields you need), tags (csv), "
                "top_k (int default 5), min_score (float default 0.3). "
                "Output: {matches:[{dataset_id, score, record_count, fields, "
                "tags}], best, recommendation: reuse|augment|not_found}.",
)
async def cap_fabric_identify(subject: str = "", expected_fields: Any = None,
                              tags: str = "", top_k: int = 5,
                              min_score: float = 0.3, trace_id=None) -> Dict[str, Any]:
    if not _df():
        return {"error": "data_fabric not loaded"}
    if isinstance(expected_fields, str):
        expected_fields = [f.strip() for f in expected_fields.split(",") if f.strip()]
    expected_fields = [f for f in (expected_fields or [])]
    want_tags = [t.strip() for t in (tags or "").split(",") if t.strip()]
    subject_tokens = re.findall(r"[a-z0-9]+", (subject or "").lower())
    if not subject_tokens and not want_tags and not expected_fields:
        return {"error": "provide at least one of subject / expected_fields / tags"}
    cands = await asyncio.to_thread(_identify_candidates_sync, subject_tokens,
                                    want_tags, 200)
    scored = []
    for c in cands:
        s = match_score(c, subject=subject, expected_fields=expected_fields,
                        want_tags=want_tags)
        if s >= min_score:
            scored.append({**c, "score": s})
    scored.sort(key=lambda x: x["score"], reverse=True)
    scored = scored[:max(1, min(50, int(top_k)))]
    best = scored[0] if scored else None
    if not best:
        rec = "not_found"
    elif expected_fields and set(expected_fields) - set(best.get("fields") or []):
        rec = "augment"   # matched a dataset but it lacks some fields → fill gaps
    else:
        rec = "reuse"
    return {"matches": scored, "best": best, "recommendation": rec,
            "subject": subject}


# ── Gap ledger ──────────────────────────────────────────────────────────────

def _ensure_gap_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fabric_gap_ledger (
            gap_id            TEXT PRIMARY KEY,
            dataset_id        TEXT,
            gap_type          TEXT,
            gap_ref           TEXT,
            status            TEXT DEFAULT 'open',
            attempts          INTEGER DEFAULT 0,
            last_attempt      TEXT,
            cooldown_until_ts REAL,
            reason            TEXT,
            updated_at        TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gap_ds "
                 "ON fabric_gap_ledger(dataset_id)")


def _load_ledger_sync(dataset_id: str) -> Dict[str, Dict[str, Any]]:
    conn = _conn()
    try:
        _ensure_gap_table(conn)
        out = {}
        for r in conn.execute(
            "SELECT * FROM fabric_gap_ledger WHERE dataset_id=?",
            (dataset_id,)).fetchall():
            out[r["gap_id"]] = dict(r)
        return out
    finally:
        conn.close()


def _present_keys_sync(dataset_id: str, key_field: str, limit: int) -> set:
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT json_extract(data, '$.{key_field}') AS k "
            "FROM fabric_records WHERE dataset_id=? LIMIT ?",
            (dataset_id, limit)).fetchall()
        return {str(r["k"]) for r in rows if r["k"] is not None}
    finally:
        conn.close()


@capability(
    "fabric.gaps", memory="off",
    http_method="POST", http_path="/fabric/gaps", http_tags=["fabric"],
    description="Report what a dataset is MISSING vs an expectation — missing "
                "fields (low coverage) and missing keys — and, crucially, which "
                "gaps are worth acting on NOW. Gaps marked noise/unfillable or "
                "still in fetch-backoff are returned as SUPPRESSED, not "
                "actionable, so noisy/un-fillable gaps don't re-trigger fetches "
                "every cycle. "
                "Inputs: dataset_id (str!), expected_fields (csv/list — defaults "
                "to the declared required fields), key_field (str — defaults to "
                "the declared key), expected_keys (list — keys that SHOULD exist), "
                "key_range (object {min,max} — integer key range that should "
                "exist), coverage_threshold (float default 0.9), scan_limit (int "
                "default 20000). Output: {dataset_id, actionable:[...], "
                "suppressed:[{gap, reason}], counts:{...}}.",
)
async def cap_fabric_gaps(dataset_id: str = "", expected_fields: Any = None,
                          key_field: str = "", expected_keys: Any = None,
                          key_range: Any = None, coverage_threshold: float = 0.9,
                          scan_limit: int = 20000, trace_id=None) -> Dict[str, Any]:
    if not _df():
        return {"error": "data_fabric not loaded"}
    if not dataset_id:
        return {"error": "dataset_id required"}
    schema_rec = await get_dataset_schema(dataset_id)
    schema = (schema_rec or {}).get("schema", {})
    if isinstance(expected_fields, str):
        expected_fields = [f.strip() for f in expected_fields.split(",") if f.strip()]
    if not expected_fields:
        # Default expectation: the declared REQUIRED fields.
        expected_fields = [f for f, spec in schema.items() if spec.get("required")]
    if not key_field:
        kf = (schema_rec or {}).get("key", [])
        key_field = kf[0] if kf else ""
    if isinstance(expected_keys, str):
        try: expected_keys = json.loads(expected_keys)
        except Exception: expected_keys = None
    if isinstance(key_range, str):
        try: key_range = json.loads(key_range)
        except Exception: key_range = None
    try:
        scan_limit = max(1, min(200000, int(scan_limit)))
    except Exception:
        scan_limit = 20000

    scan = await asyncio.to_thread(_validate_sync, dataset_id, scan_limit)
    rows = scan["rows"]
    gaps: List[Dict[str, Any]] = []
    if expected_fields:
        gaps += compute_field_gaps(rows, expected_fields, float(coverage_threshold))
    if key_field and (expected_keys or key_range):
        present = await asyncio.to_thread(_present_keys_sync, dataset_id,
                                          key_field, scan_limit)
        gaps += compute_key_gaps(present, expected_keys=expected_keys,
                                 key_range=key_range if isinstance(key_range, dict) else None)

    ledger = await asyncio.to_thread(_load_ledger_sync, dataset_id)
    now = _time.time()
    actionable, suppressed = [], []
    for g in gaps:
        gid = _gap_id(dataset_id, g)
        lrow = ledger.get(gid)
        ok, reason = gap_actionable(lrow, now=now)
        entry = {**g, "gap_id": gid,
                 "attempts": int((lrow or {}).get("attempts", 0)),
                 "status": (lrow or {}).get("status", "open")}
        if ok:
            actionable.append(entry)
        else:
            suppressed.append({"gap": entry, "reason": reason})
    return {"dataset_id": dataset_id, "actionable": actionable,
            "suppressed": suppressed,
            "counts": {"total": len(gaps), "actionable": len(actionable),
                       "suppressed": len(suppressed)},
            "key_field": key_field}


def _gap_ref_from_input(g: Any) -> Optional[Dict[str, str]]:
    if isinstance(g, dict) and g.get("type") and g.get("ref") is not None:
        return {"type": str(g["type"]), "ref": str(g["ref"])}
    return None


def _upsert_ledger_sync(dataset_id: str, gid: str, gtype: str, gref: str,
                        *, status: str, attempts: int, cooldown_ts: Optional[float],
                        reason: str) -> Dict[str, Any]:
    conn = _conn()
    try:
        _ensure_gap_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO fabric_gap_ledger "
            "(gap_id, dataset_id, gap_type, gap_ref, status, attempts, "
            " last_attempt, cooldown_until_ts, reason, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (gid, dataset_id, gtype, gref, status, attempts, now_iso(),
             cooldown_ts, reason, now_iso()))
        conn.commit()
        row = conn.execute("SELECT * FROM fabric_gap_ledger WHERE gap_id=?",
                           (gid,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


@capability(
    "fabric.gaps.attempt", memory="off",
    http_method="POST", http_path="/fabric/gaps/attempt", http_tags=["fabric"],
    description="Record that a fetch was ATTEMPTED for one or more gaps. On "
                "'failed' the gap goes into an exponential backoff (retried "
                "ever-less-often) and, after too many failures, is auto-marked "
                "'unfillable' so it stops re-triggering fetches. On 'filled' it "
                "is cleared. Call this whenever you try to fill a gap. "
                "Inputs: dataset_id (str!), gaps (list of {type,ref}), outcome "
                "('failed'|'filled', default failed), reason (str). "
                "Output: {updated:[ledger rows]}.",
)
async def cap_fabric_gaps_attempt(dataset_id: str = "", gaps: Any = None,
                                  outcome: str = "failed", reason: str = "",
                                  trace_id=None) -> Dict[str, Any]:
    if not dataset_id:
        return {"error": "dataset_id required"}
    if isinstance(gaps, str):
        try: gaps = json.loads(gaps)
        except Exception: return {"error": "gaps must be valid JSON"}
    if isinstance(gaps, dict):
        gaps = [gaps]
    if not isinstance(gaps, list) or not gaps:
        return {"error": "gaps must be a non-empty list of {type,ref}"}
    outcome = (outcome or "failed").lower()
    ledger = await asyncio.to_thread(_load_ledger_sync, dataset_id)
    updated = []
    for g in gaps:
        ref = _gap_ref_from_input(g)
        if not ref:
            continue
        gid = _gap_id(dataset_id, ref)
        prev = ledger.get(gid, {})
        attempts = int(prev.get("attempts", 0)) + 1
        if outcome == "filled":
            status, cooldown = "filled", None
        else:
            status = "unfillable" if attempts >= MAX_FILL_ATTEMPTS else "open"
            cooldown = _time.time() + backoff_cooldown(attempts)
        row = await asyncio.to_thread(
            _upsert_ledger_sync, dataset_id, gid, ref["type"], ref["ref"],
            status=status, attempts=attempts, cooldown_ts=cooldown,
            reason=reason or outcome)
        updated.append(row)
    await emit_event({"type": "fabric.gaps.attempted", "dataset_id": dataset_id,
                      "outcome": outcome, "count": len(updated)})
    return {"updated": updated}


@capability(
    "fabric.gaps.resolve", memory="off",
    http_method="POST", http_path="/fabric/gaps/resolve", http_tags=["fabric"],
    description="Manually set a gap's status so it stops (or resumes) triggering "
                "fetches. Use 'noise' for a gap that is spurious and 'unfillable' "
                "for data that genuinely cannot be obtained — both suppress it "
                "permanently until reset with 'open'. "
                "Inputs: dataset_id (str!), gap ({type,ref}), status "
                "('noise'|'unfillable'|'open'|'filled'), reason (str). "
                "Output: the updated ledger row.",
)
async def cap_fabric_gaps_resolve(dataset_id: str = "", gap: Any = None,
                                  status: str = "noise", reason: str = "",
                                  trace_id=None) -> Dict[str, Any]:
    if not dataset_id:
        return {"error": "dataset_id required"}
    ref = _gap_ref_from_input(gap)
    if not ref:
        return {"error": "gap must be {type, ref}"}
    status = (status or "noise").lower()
    if status not in ("noise", "unfillable", "open", "filled"):
        return {"error": "status must be noise|unfillable|open|filled"}
    gid = _gap_id(dataset_id, ref)
    ledger = await asyncio.to_thread(_load_ledger_sync, dataset_id)
    attempts = int(ledger.get(gid, {}).get("attempts", 0))
    # Reopening clears the backoff; suppressing leaves no cooldown (permanent).
    row = await asyncio.to_thread(
        _upsert_ledger_sync, dataset_id, gid, ref["type"], ref["ref"],
        status=status, attempts=(0 if status == "open" else attempts),
        cooldown_ts=None, reason=reason or f"manual:{status}")
    await emit_event({"type": "fabric.gaps.resolved", "dataset_id": dataset_id,
                      "status": status, "gap_id": gid})
    return row


# ── Fusion ──────────────────────────────────────────────────────────────────

def _ensure_fusion_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fabric_fusion_recipes (
            into_id     TEXT PRIMARY KEY,
            left_id     TEXT,
            right_id    TEXT,
            on_fields   TEXT,
            how         TEXT,
            ttl_secs    INTEGER,
            created_at  TEXT,
            expires_at  TEXT,
            last_refresh TEXT,
            row_count   INTEGER DEFAULT 0
        )
    """)


def _all_rows_sync(dataset_id: str, limit: int) -> List[Dict[str, Any]]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT data FROM fabric_records WHERE dataset_id=? LIMIT ?",
            (dataset_id, limit)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["data"] or "{}"))
        except Exception:
            pass
    return out


def _save_recipe_sync(rec: Dict[str, Any]) -> None:
    conn = _conn()
    try:
        _ensure_fusion_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO fabric_fusion_recipes "
            "(into_id, left_id, right_id, on_fields, how, ttl_secs, created_at, "
            " expires_at, last_refresh, row_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rec["into_id"], rec["left_id"], rec["right_id"],
             json.dumps(rec["on_fields"]), rec["how"], rec["ttl_secs"],
             rec.get("created_at") or now_iso(), rec.get("expires_at"),
             now_iso(), rec.get("row_count", 0)))
        conn.commit()
    finally:
        conn.close()


def _load_recipe_sync(into_id: str) -> Optional[Dict[str, Any]]:
    conn = _conn()
    try:
        _ensure_fusion_table(conn)
        row = conn.execute("SELECT * FROM fabric_fusion_recipes WHERE into_id=?",
                           (into_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def _do_fuse(left_id: str, right_id: str, on: List[str], how: str,
                   into_id: str, ttl_secs: int, limit: int) -> Dict[str, Any]:
    df = _df()
    left_rows = await asyncio.to_thread(_all_rows_sync, left_id, limit)
    right_rows = await asyncio.to_thread(_all_rows_sync, right_id, limit)
    fused = join_rows(left_rows, right_rows, on, how)
    # Materialise the fused rows into the ephemeral dataset, keyed on the join
    # fields so a refresh replaces cleanly.
    upsert = CAPABILITY_REGISTRY.get("fabric.upsert")
    if upsert and upsert.get("func") and fused:
        await upsert["func"](dataset_id=into_id, rows=json.dumps(fused),
                             key=",".join(on), mode="replace",
                             source="fuse", tags="ephemeral,fused")
    # Record the fusion as link records in the aux graph (reproducible lineage).
    link = CAPABILITY_REGISTRY.get("fabric.link_datasets")
    if link and link.get("func"):
        for src in (left_id, right_id):
            try:
                await link["func"](from_id=src, to_id=into_id, rel_type="FUSED_INTO")
            except Exception:
                pass
    expires = (now_iso() if ttl_secs <= 0
               else _iso_in(ttl_secs))
    await asyncio.to_thread(_save_recipe_sync, {
        "into_id": into_id, "left_id": left_id, "right_id": right_id,
        "on_fields": on, "how": how, "ttl_secs": ttl_secs,
        "expires_at": (None if ttl_secs <= 0 else expires),
        "row_count": len(fused)})
    return {"into": into_id, "rows": len(fused), "how": how, "on": on,
            "left": left_id, "right": right_id, "ttl_secs": ttl_secs,
            "expires_at": (None if ttl_secs <= 0 else expires)}


def _iso_in(secs: int) -> str:
    import datetime as _dt
    return (_dt.datetime.now(_dt.timezone.utc)
            + _dt.timedelta(seconds=int(secs))).isoformat().replace("+00:00", "Z")


@capability(
    "fabric.fuse", memory="off",
    http_method="POST", http_path="/fabric/fuse", http_tags=["fabric"],
    description="Row-level JOIN two datasets on shared key field(s) into a new "
                "ephemeral fused dataset, and store the recipe so it can be "
                "refreshed/reproduced. LEFT wins on field conflicts. "
                "Inputs: left (str!), right (str!), on (csv of join field(s)!), "
                "how (inner|left|outer, default inner), into (str — target "
                "dataset id; default 'fused.<hash>'), ttl_secs (int default "
                "86400; 0 = no expiry), limit (int default 50000 rows/side). "
                "Output: {into, rows, how, on, expires_at, recipe_saved}.",
)
async def cap_fabric_fuse(left: str = "", right: str = "", on: str = "",
                          how: str = "inner", into: str = "",
                          ttl_secs: int = 86400, limit: int = 50000,
                          trace_id=None) -> Dict[str, Any]:
    if not _df():
        return {"error": "data_fabric not loaded"}
    on_fields = [f.strip() for f in (on or "").split(",") if f.strip()]
    if not left or not right or not on_fields:
        return {"error": "left, right and on (csv of join fields) are required"}
    for f in on_fields:
        if not _FIELD_RE.match(f):
            return {"error": f"invalid join field: {f!r}"}
    how = (how or "inner").lower()
    if how not in ("inner", "left", "outer"):
        how = "inner"
    if not into:
        h = _gap_id(f"{left}|{right}|{how}", {"type": "fuse", "ref": ",".join(on_fields)})
        into = f"fused.{h[:12]}"
    try:
        ttl_secs = max(0, int(ttl_secs))
        limit = max(1, min(500000, int(limit)))
    except Exception:
        ttl_secs, limit = 86400, 50000
    res = await _do_fuse(left, right, on_fields, how, into, ttl_secs, limit)
    await emit_event({"type": "fabric.fused", "into": into, "rows": res["rows"]})
    return {**res, "recipe_saved": True}


@capability(
    "fabric.fuse.refresh", memory="off",
    http_method="POST", http_path="/fabric/fuse/refresh", http_tags=["fabric"],
    description="Re-run a stored fusion recipe so the fused dataset reflects the "
                "current source rows. Input: into (str! — the fused dataset id). "
                "Output: {into, rows, refreshed} or {error}.",
)
async def cap_fabric_fuse_refresh(into: str = "", trace_id=None) -> Dict[str, Any]:
    if not into:
        return {"error": "into required"}
    rec = await asyncio.to_thread(_load_recipe_sync, into)
    if not rec:
        return {"error": f"no fusion recipe for {into}"}
    try:
        on_fields = json.loads(rec.get("on_fields") or "[]")
    except Exception:
        on_fields = []
    res = await _do_fuse(rec["left_id"], rec["right_id"], on_fields,
                         rec.get("how", "inner"), into,
                         int(rec.get("ttl_secs", 86400) or 0), 50000)
    await emit_event({"type": "fabric.fuse.refreshed", "into": into,
                      "rows": res["rows"]})
    return {**res, "refreshed": True}


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 3 — context.for_agent: unified, trust-ranked surfacing of curated
# datasets ABOVE agent-scoped memories. This lives in the context.* namespace
# (assembly layer) — the precedence "datasets outrank memories" is an assembly
# concern, so it belongs here even though the file is fabric-side.
# ═════════════════════════════════════════════════════════════════════════════

def _resolve_dataset_tags(agent: str, profile: str, extra_tags: List[str]) -> List[str]:
    """The dataset tags that scope data to a specialist: explicit tags + an
    agent tag + the agent/profile's family tags (from loop_profiles)."""
    tags = {t.strip().lower() for t in extra_tags if t.strip()}
    if agent:
        tags.add(f"agent:{agent}".lower())
    lp = (sys.modules.get("loop_profiles")
          or sys.modules.get("Vera.vera.dag.loop_profiles"))
    prof = None
    if lp:
        try:
            if profile:
                prof = lp.get_profile(profile)
            if not prof and agent:
                for p in getattr(lp, "LOOP_PROFILES", []):
                    if p.get("agent") == agent:
                        prof = p
                        break
        except Exception:
            prof = None
    if prof:
        tags.add(f"profile:{prof.get('id')}".lower())
        for fam in (prof.get("family") or []):
            tags.add(f"family:{fam}".lower())
    return sorted(tags)


def _datasets_by_tags_sync(tags: List[str], limit: int) -> Dict[str, int]:
    """dataset_id -> number of the requested tags it carries (tag-match strength)."""
    if not tags:
        return {}
    conn = _conn()
    try:
        ph = ",".join("?" * len(tags))
        rows = conn.execute(
            f"SELECT dataset_id, COUNT(*) AS m FROM fabric_dataset_tags "
            f"WHERE tag IN ({ph}) GROUP BY dataset_id LIMIT ?",
            [*tags, limit]).fetchall()
        return {r["dataset_id"]: int(r["m"]) for r in rows}
    except Exception:
        return {}
    finally:
        conn.close()


async def _dataset_slice(dataset_id: str, query: str, n: int) -> List[Dict[str, Any]]:
    """A small, relevant slice of a dataset: semantic hits when there's a query,
    else the newest rows. All via async caps — nothing blocks the loop."""
    if query:
        fq = CAPABILITY_REGISTRY.get("fabric.query")
        if fq and fq.get("func"):
            try:
                r = await fq["func"](text=query, vector=query, dataset_id=dataset_id,
                                     top_k=n, include_data=True)
                hits = (r or {}).get("results", []) or []
                out = [h.get("data") or {"text": h.get("text", "")} for h in hits]
                if out:
                    return out[:n]
            except Exception as e:
                log.debug("context slice query [%s]: %s", dataset_id, e)
    sel = CAPABILITY_REGISTRY.get("memory.select")
    if sel and sel.get("func"):
        try:
            r = await sel["func"](dataset_id=dataset_id, limit=n)
            return [row.get("data", {}) for row in (r or {}).get("rows", [])][:n]
        except Exception as e:
            log.debug("context slice select [%s]: %s", dataset_id, e)
    return []


@capability(
    "context.for_agent", memory="off",
    http_method="POST", http_path="/context/for_agent", http_tags=["context"],
    description="Unified context for a specialist agent/loop: the RELEVANT curated "
                "datasets (declared schema + a small data slice) ranked by TRUST "
                "ABOVE the agent's scoped memories. Curated datasets are treated as "
                "more authoritative than free-text memory. Datasets are found by "
                "the agent/profile's family tags AND by query relevance "
                "(fabric.identify). Use this to ground a specialist before it acts "
                "so it reuses consistent datasets instead of re-collecting. "
                "Inputs: query (str), agent (str — agent name), profile (str — loop "
                "profile id), session_id (str — for agent memories), dataset_tags "
                "(csv — extra tag scope), top_datasets (int default 5), slice_rows "
                "(int default 5), include_memories (bool default True). "
                "Output: {datasets:[{dataset_id, trust, record_count, schema, "
                "slice, tag_match, relevance}], memories, prompt}.",
)
async def cap_context_for_agent(query: str = "", agent: str = "", profile: str = "",
                                session_id: str = "", dataset_tags: str = "",
                                top_datasets: int = 5, slice_rows: int = 5,
                                include_memories: bool = True,
                                trace_id=None) -> Dict[str, Any]:
    if not _df():
        return {"error": "data_fabric not loaded"}
    extra = [t for t in (dataset_tags or "").split(",") if t.strip()]
    tags = _resolve_dataset_tags(agent, profile, extra)
    try:
        top_datasets = max(1, min(20, int(top_datasets)))
        slice_rows = max(0, min(20, int(slice_rows)))
    except Exception:
        top_datasets, slice_rows = 5, 5

    # 1) Tagged datasets (family/agent/profile scope) → tag-match strength.
    tag_hits = await asyncio.to_thread(_datasets_by_tags_sync, tags, 100) if tags else {}

    # 2) Query-relevant datasets via identify (cheap, dataset-level).
    rel_scores: Dict[str, float] = {}
    if query:
        idc = CAPABILITY_REGISTRY.get("fabric.identify")
        if idc and idc.get("func"):
            try:
                r = await idc["func"](subject=query, top_k=top_datasets * 2, min_score=0.2)
                for m in (r or {}).get("matches", []):
                    rel_scores[m["dataset_id"]] = float(m.get("score", 0))
            except Exception as e:
                log.debug("context identify: %s", e)

    # 3) Build candidates with trust from the declared schema.
    cand_ids = set(tag_hits) | set(rel_scores)
    cands: List[Dict[str, Any]] = []
    max_tag = max(tag_hits.values()) if tag_hits else 0
    for did in cand_ids:
        srec = await get_dataset_schema(did)
        trust = (srec or {}).get("trust", 0.3)
        cands.append({
            "dataset_id": did,
            "trust": trust,
            "schema": (srec or {}).get("schema", {}),
            "tag_match": (tag_hits.get(did, 0) / max_tag) if max_tag else 0.0,
            "relevance": rel_scores.get(did, 0.0),
        })
    ranked = rank_context_datasets(cands, top=top_datasets)

    # 4) Attach a small slice to each ranked dataset.
    for d in ranked:
        d["slice"] = await _dataset_slice(d["dataset_id"], query, slice_rows)

    # 5) Agent-scoped memories (below datasets in authority).
    memories = ""
    if include_memories and (agent or session_id):
        mc = CAPABILITY_REGISTRY.get("memory.agent_context")
        if mc and mc.get("func"):
            try:
                r = await mc["func"](session_id=session_id or "", query=query or "",
                                     agent_name=agent or "", limit=5)
                memories = (r or {}).get("context", "") or ""
            except Exception as e:
                log.debug("context agent memories: %s", e)

    prompt = _assemble_context_prompt(ranked, memories, subject=query)
    return {"datasets": ranked, "memories": memories, "prompt": prompt,
            "tags": tags}


def _assemble_context_prompt(datasets: List[Dict[str, Any]], memories: str,
                             *, subject: str = "") -> str:
    lines: List[str] = []
    if datasets:
        lines.append("AUTHORITATIVE DATASETS (curated — prefer these over memory; "
                     "query/extend them with memory.select / fabric.upsert):")
        for d in datasets:
            fields = ", ".join(list((d.get("schema") or {}).keys())[:20])
            head = f"• {d['dataset_id']}  (trust {round(float(d.get('trust',0)),2)})"
            if fields:
                head += f"  fields: {fields}"
            lines.append(head)
            for row in (d.get("slice") or [])[:3]:
                try:
                    lines.append("    " + json.dumps(row, default=str)[:200])
                except Exception:
                    pass
    if memories:
        lines.append("")
        lines.append("RELATED MEMORIES (lower authority than the datasets above):")
        lines.append(memories.strip()[:1500])
    return "\n".join(lines).strip()


log.info("fabric_curation: upsert / schema.declare+get / validate / select "
         "+ identify / gaps(+attempt+resolve) / fuse(+refresh) / "
         "context.for_agent registered")
