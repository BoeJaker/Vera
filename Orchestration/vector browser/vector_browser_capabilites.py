"""
vector_browser_capabilities.py — Chroma / FAISS vector audit & browser
=======================================================================
Capabilities for inspecting, auditing, and browsing the vector stores
that back the Vera Data Fabric.  Registered as fabric.vectors.* and
served via a dedicated panel at /fabric/vectors/panel.

Capabilities
────────────
  fabric.vectors.overview      — combined stats for both stores + dim alignment
  fabric.vectors.chroma.browse — paginated Chroma document listing w/ embeddings
  fabric.vectors.chroma.get    — single-record detail from Chroma
  fabric.vectors.chroma.datasets — per-dataset vector counts from Chroma
  fabric.vectors.faiss.shards  — FAISS shard/dataset index stats
  fabric.vectors.faiss.sample  — sample vectors from a FAISS shard or dataset
  fabric.vectors.audit         — full dimension-alignment audit across both stores
  fabric.vectors.compare       — compare a record's vectors across Chroma vs FAISS
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import (
    APP, capability, emit_event, now_iso,
)
from Vera.Orchestration.fabric.data_fabric import (
    FABRIC_CHROMA, FAISS_STORE, FABRIC_VECTOR_DIM,
    OLLAMA_EMBED_MODEL, HAS_NUMPY, HAS_FAISS,
)

try:
    import numpy as np
except ImportError:
    np = None

log = logging.getLogger("vera.vector_browser")


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _safe_embedding_stats(emb: List[float]) -> Dict:
    """Compute basic stats about an embedding vector."""
    if not emb:
        return {"dim": 0}
    dim = len(emb)
    out: Dict[str, Any] = {"dim": dim}
    if np is not None:
        arr = np.array(emb, dtype="float32")
        out["norm"]   = round(float(np.linalg.norm(arr)), 6)
        out["mean"]   = round(float(arr.mean()), 6)
        out["std"]    = round(float(arr.std()), 6)
        out["min"]    = round(float(arr.min()), 6)
        out["max"]    = round(float(arr.max()), 6)
        out["zeros"]  = int(np.count_nonzero(arr == 0))
        out["nans"]   = int(np.isnan(arr).sum())
    return out


def _chroma_col():
    """Return the Chroma collection handle or None."""
    return getattr(FABRIC_CHROMA, "_col", None)


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "fabric.vectors.overview",
    http_method="GET", http_path="/fabric/vectors/overview", http_tags=["fabric", "vectors"],
    memory="off", silent=True,
    description="Combined vector store overview: Chroma + FAISS stats, configured "
                "embedding dimension, model name, and alignment status.",
)
async def cap_vectors_overview(trace_id=None) -> Dict:
    chroma_stats = FABRIC_CHROMA.stats()
    faiss_stats  = FAISS_STORE.stats()

    # Try to detect actual Chroma dimension from a sample
    chroma_dim = None
    col = _chroma_col()
    if col:
        try:
            sample = col.peek(limit=1)
            if sample and sample.get("embeddings") and sample["embeddings"][0]:
                chroma_dim = len(sample["embeddings"][0])
        except Exception:
            pass

    faiss_dim = faiss_stats.get("dim") if faiss_stats.get("available") else None

    configured_dim = FABRIC_VECTOR_DIM
    dims_match = True
    mismatches = []
    if chroma_dim is not None and chroma_dim != configured_dim:
        dims_match = False
        mismatches.append(f"Chroma has {chroma_dim}-dim vectors, config says {configured_dim}")
    if faiss_dim is not None and faiss_dim != configured_dim:
        dims_match = False
        mismatches.append(f"FAISS has {faiss_dim}-dim indexes, config says {configured_dim}")
    if chroma_dim and faiss_dim and chroma_dim != faiss_dim:
        dims_match = False
        mismatches.append(f"Chroma ({chroma_dim}) and FAISS ({faiss_dim}) dimensions differ")

    return {
        "chroma":         chroma_stats,
        "faiss":          faiss_stats,
        "configured_dim": configured_dim,
        "chroma_dim":     chroma_dim,
        "faiss_dim":      faiss_dim,
        "embed_model":    OLLAMA_EMBED_MODEL,
        "dims_aligned":   dims_match,
        "mismatches":     mismatches,
    }


@capability(
    "fabric.vectors.chroma.browse",
    http_method="POST", http_path="/fabric/vectors/chroma/browse",
    http_tags=["fabric", "vectors"],
    memory="off", silent=True,
    description="Browse Chroma vectors with pagination. Returns ids, documents, "
                "metadata, and embedding stats (norm, dim, mean, std). "
                "Input: offset (int), limit (int 1-100), dataset_id (str, optional), "
                "include_embeddings (bool, default false — returns stats only). "
                "Output: {records, total, has_more}.",
)
async def cap_vectors_chroma_browse(
    offset:            int  = 0,
    limit:             int  = 50,
    dataset_id:        str  = "",
    include_embeddings: bool = False,
    trace_id=None,
) -> Dict:
    col = _chroma_col()
    if not col:
        return {"error": "Chroma not connected", "records": [], "total": 0}

    limit = max(1, min(100, limit))

    try:
        total = col.count()
    except Exception:
        total = 0

    try:
        where = None
        if dataset_id:
            where = {"dataset_id": {"$eq": dataset_id}}

        includes = ["metadatas", "documents", "embeddings"]
        kwargs: dict = {"include": includes}

        # Chroma's .get() supports offset/limit
        if where:
            kwargs["where"] = where
        kwargs["limit"]  = limit
        kwargs["offset"] = offset

        result = col.get(**kwargs)

        records = []
        ids        = result.get("ids") or []
        docs       = result.get("documents") or []
        metas      = result.get("metadatas") or []
        embeddings = result.get("embeddings") or []

        for i, rid in enumerate(ids):
            rec: Dict[str, Any] = {"id": rid}
            rec["document"] = (docs[i][:500] if docs[i] else "") if i < len(docs) else ""
            rec["metadata"] = metas[i] if i < len(metas) else {}
            emb = embeddings[i] if i < len(embeddings) else None
            rec["embedding_stats"] = _safe_embedding_stats(emb) if emb else {"dim": 0}
            if include_embeddings and emb:
                # Send first/last 8 values as a preview, not the full vector
                rec["embedding_preview"] = {
                    "first_8": [round(v, 6) for v in emb[:8]],
                    "last_8":  [round(v, 6) for v in emb[-8:]],
                }
            records.append(rec)

        return {
            "records":  records,
            "total":    total,
            "offset":   offset,
            "limit":    limit,
            "has_more": (offset + limit) < total,
        }
    except Exception as e:
        log.warning("chroma browse: %s", e)
        return {"error": str(e), "records": [], "total": 0}


@capability(
    "fabric.vectors.chroma.get",
    http_method="GET", http_path="/fabric/vectors/chroma/get",
    http_tags=["fabric", "vectors"],
    memory="off", silent=True,
    description="Get full detail for a single Chroma record by id. "
                "Input: record_id (str!). "
                "Output: {id, document, metadata, embedding_stats, embedding_preview}.",
)
async def cap_vectors_chroma_get(record_id: str, trace_id=None) -> Dict:
    col = _chroma_col()
    if not col:
        return {"error": "Chroma not connected"}

    try:
        result = col.get(ids=[record_id], include=["metadatas", "documents", "embeddings"])
        if not result.get("ids"):
            return {"error": f"Record {record_id} not found in Chroma"}

        emb  = result["embeddings"][0] if result.get("embeddings") else None
        doc  = result["documents"][0] if result.get("documents") else ""
        meta = result["metadatas"][0] if result.get("metadatas") else {}

        out: Dict[str, Any] = {
            "id":              record_id,
            "document":        doc[:2000] if doc else "",
            "metadata":        meta,
            "embedding_stats": _safe_embedding_stats(emb) if emb else {"dim": 0},
        }
        if emb:
            out["embedding_preview"] = {
                "first_16": [round(v, 6) for v in emb[:16]],
                "last_16":  [round(v, 6) for v in emb[-16:]],
                "dim":       len(emb),
            }
        return out
    except Exception as e:
        return {"error": str(e)}


@capability(
    "fabric.vectors.chroma.datasets",
    http_method="GET", http_path="/fabric/vectors/chroma/datasets",
    http_tags=["fabric", "vectors"],
    memory="off", silent=True,
    description="List datasets present in Chroma with per-dataset vector counts "
                "and sample dimension. Output: {datasets: [{dataset_id, count, sample_dim}]}.",
)
async def cap_vectors_chroma_datasets(trace_id=None) -> Dict:
    col = _chroma_col()
    if not col:
        return {"error": "Chroma not connected", "datasets": []}

    try:
        # Get all unique dataset_ids from metadata
        # Chroma doesn't have a native "distinct" — we page through metadata
        all_meta = col.get(include=["metadatas"], limit=10000)
        metas = all_meta.get("metadatas") or []
        ids   = all_meta.get("ids") or []

        ds_counts: Dict[str, int] = {}
        ds_sample_ids: Dict[str, str] = {}
        for i, meta in enumerate(metas):
            ds = meta.get("dataset_id", "__none__") if meta else "__none__"
            ds_counts[ds] = ds_counts.get(ds, 0) + 1
            if ds not in ds_sample_ids and i < len(ids):
                ds_sample_ids[ds] = ids[i]

        # Sample one embedding per dataset to check dimension
        datasets = []
        for ds, count in sorted(ds_counts.items(), key=lambda x: -x[1]):
            entry: Dict[str, Any] = {"dataset_id": ds, "count": count}
            sample_id = ds_sample_ids.get(ds)
            if sample_id:
                try:
                    s = col.get(ids=[sample_id], include=["embeddings"])
                    if s.get("embeddings") and s["embeddings"][0]:
                        entry["sample_dim"] = len(s["embeddings"][0])
                except Exception:
                    pass
            datasets.append(entry)

        return {
            "datasets":    datasets,
            "total_count": col.count(),
        }
    except Exception as e:
        return {"error": str(e), "datasets": []}


@capability(
    "fabric.vectors.faiss.shards",
    http_method="GET", http_path="/fabric/vectors/faiss/shards",
    http_tags=["fabric", "vectors"],
    memory="off", silent=True,
    description="FAISS shard and per-dataset index statistics. "
                "Output: {global_shards: [{name, vectors, dim}], "
                "dataset_indexes: [{dataset_id, vectors, dim}], dim, total}.",
)
async def cap_vectors_faiss_shards(trace_id=None) -> Dict:
    if not FAISS_STORE.available:
        return {"error": "FAISS not available", "global_shards": [], "dataset_indexes": []}

    shards = []
    with FAISS_STORE._lock:
        for name, idx in FAISS_STORE._global_shards.items():
            shards.append({
                "name":    name,
                "vectors": idx.ntotal,
                "ids":     len(FAISS_STORE._shard_ids.get(name, [])),
                "dim":     idx.d if hasattr(idx, 'd') else FAISS_STORE._dim,
            })

        ds_indexes = []
        for ds_id, idx in FAISS_STORE._ds_indexes.items():
            ds_indexes.append({
                "dataset_id": ds_id,
                "vectors":    idx.ntotal,
                "ids":        len(FAISS_STORE._ds_ids.get(ds_id, [])),
                "dim":        idx.d if hasattr(idx, 'd') else FAISS_STORE._dim,
            })

    return {
        "global_shards":   shards,
        "dataset_indexes": sorted(ds_indexes, key=lambda x: -x["vectors"]),
        "dim":             FAISS_STORE._dim,
        "total":           sum(s["vectors"] for s in shards),
        "index_type":      FAISS_STORE.INDEX_TYPE,
        "n_shards":        FAISS_STORE.N_SHARDS,
    }


@capability(
    "fabric.vectors.faiss.sample",
    http_method="POST", http_path="/fabric/vectors/faiss/sample",
    http_tags=["fabric", "vectors"],
    memory="off", silent=True,
    description="Sample vector IDs and stats from a FAISS shard or dataset index. "
                "Input: shard_name (str, e.g. 'shard_0') OR dataset_id (str), "
                "limit (int 1-50 default 10). "
                "Output: {samples: [{id, norm, mean, std}]}.",
)
async def cap_vectors_faiss_sample(
    shard_name: str = "",
    dataset_id: str = "",
    limit:      int = 10,
    trace_id=None,
) -> Dict:
    if not FAISS_STORE.available or np is None:
        return {"error": "FAISS or numpy not available", "samples": []}

    limit = max(1, min(50, limit))

    with FAISS_STORE._lock:
        if dataset_id:
            idx = FAISS_STORE._ds_indexes.get(dataset_id)
            ids = FAISS_STORE._ds_ids.get(dataset_id, [])
            label = f"dataset:{dataset_id}"
        elif shard_name:
            idx = FAISS_STORE._global_shards.get(shard_name)
            ids = FAISS_STORE._shard_ids.get(shard_name, [])
            label = f"shard:{shard_name}"
        else:
            return {"error": "Provide shard_name or dataset_id"}

    if idx is None:
        return {"error": f"Index not found: {label}", "samples": []}

    n = min(limit, idx.ntotal, len(ids))
    if n == 0:
        return {"samples": [], "label": label, "total": 0}

    samples = []
    try:
        for i in range(n):
            vec = np.zeros((1, idx.d), dtype="float32")
            try:
                idx.reconstruct(i, vec[0])
            except Exception:
                continue
            norm  = float(np.linalg.norm(vec))
            mean  = float(vec.mean())
            std   = float(vec.std())
            samples.append({
                "id":   ids[i] if i < len(ids) else f"?_{i}",
                "norm": round(norm, 6),
                "mean": round(mean, 6),
                "std":  round(std, 6),
                "dim":  int(idx.d),
            })
    except Exception as e:
        log.warning("faiss sample: %s", e)

    return {"samples": samples, "label": label, "total": idx.ntotal}


@capability(
    "fabric.vectors.audit",
    http_method="GET", http_path="/fabric/vectors/audit", http_tags=["fabric", "vectors"],
    memory="off",
    description="Full vector dimension audit across Chroma and FAISS. "
                "Checks every dataset for consistent embedding sizes. "
                "Output: {aligned, configured_dim, chroma_audit, faiss_audit, issues}.",
)
async def cap_vectors_audit(trace_id=None) -> Dict:
    issues = []
    configured_dim = FABRIC_VECTOR_DIM

    # ── Chroma audit ──
    chroma_audit: Dict[str, Any] = {"available": FABRIC_CHROMA.available}
    col = _chroma_col()
    if col:
        try:
            total = col.count()
            chroma_audit["total"] = total

            # Sample up to 500 records for dimension check
            sample_size = min(500, total)
            if sample_size > 0:
                result = col.get(
                    include=["embeddings", "metadatas"],
                    limit=sample_size
                )
                embeddings = result.get("embeddings") or []
                metas      = result.get("metadatas") or []

                dim_counts: Dict[int, int] = {}
                null_count = 0
                nan_count  = 0
                per_ds_dims: Dict[str, Dict[int, int]] = {}

                for i, emb in enumerate(embeddings):
                    if emb is None or len(emb) == 0:
                        null_count += 1
                        continue
                    d = len(emb)
                    dim_counts[d] = dim_counts.get(d, 0) + 1

                    ds = (metas[i] or {}).get("dataset_id", "__none__") if i < len(metas) else "__none__"
                    per_ds_dims.setdefault(ds, {})
                    per_ds_dims[ds][d] = per_ds_dims[ds].get(d, 0) + 1

                    if np is not None:
                        arr = np.array(emb, dtype="float32")
                        if np.isnan(arr).any():
                            nan_count += 1

                chroma_audit["sampled"]     = sample_size
                chroma_audit["dim_counts"]  = dim_counts
                chroma_audit["null_vectors"] = null_count
                chroma_audit["nan_vectors"]  = nan_count
                chroma_audit["per_dataset_dims"] = {
                    ds: dims for ds, dims in sorted(per_ds_dims.items())
                }

                if len(dim_counts) > 1:
                    issues.append({
                        "level":   "error",
                        "store":   "chroma",
                        "message": f"Mixed dimensions in Chroma: {dim_counts}",
                    })
                for d in dim_counts:
                    if d != configured_dim:
                        issues.append({
                            "level":   "warning",
                            "store":   "chroma",
                            "message": f"Chroma has {dim_counts[d]} vectors with dim={d}, "
                                       f"expected {configured_dim}",
                        })
                if null_count > 0:
                    issues.append({
                        "level":   "warning",
                        "store":   "chroma",
                        "message": f"{null_count} records have null/empty embeddings",
                    })
                if nan_count > 0:
                    issues.append({
                        "level":   "error",
                        "store":   "chroma",
                        "message": f"{nan_count} embeddings contain NaN values",
                    })
        except Exception as e:
            chroma_audit["error"] = str(e)

    # ── FAISS audit ──
    faiss_audit: Dict[str, Any] = {"available": FAISS_STORE.available}
    if FAISS_STORE.available:
        with FAISS_STORE._lock:
            shard_dims = {}
            for name, idx in FAISS_STORE._global_shards.items():
                d = idx.d if hasattr(idx, 'd') else FAISS_STORE._dim
                shard_dims[name] = {"dim": d, "vectors": idx.ntotal}

            ds_dims = {}
            for ds_id, idx in FAISS_STORE._ds_indexes.items():
                d = idx.d if hasattr(idx, 'd') else FAISS_STORE._dim
                ds_dims[ds_id] = {"dim": d, "vectors": idx.ntotal}

        faiss_audit["shard_dims"]   = shard_dims
        faiss_audit["dataset_dims"] = ds_dims
        faiss_audit["configured"]   = FAISS_STORE._dim

        unique_dims = set(s["dim"] for s in shard_dims.values())
        unique_dims.update(s["dim"] for s in ds_dims.values())
        if len(unique_dims) > 1:
            issues.append({
                "level":   "error",
                "store":   "faiss",
                "message": f"Mixed dimensions across FAISS indexes: {unique_dims}",
            })
        for d in unique_dims:
            if d != configured_dim:
                issues.append({
                    "level":   "warning",
                    "store":   "faiss",
                    "message": f"FAISS index dimension {d} differs from configured {configured_dim}",
                })

    # ── Cross-store check ──
    chroma_dims = set(chroma_audit.get("dim_counts", {}).keys())
    faiss_dims  = set()
    if FAISS_STORE.available:
        with FAISS_STORE._lock:
            faiss_dims = {
                (idx.d if hasattr(idx, 'd') else FAISS_STORE._dim)
                for idx in FAISS_STORE._global_shards.values()
            }
    all_dims = chroma_dims | faiss_dims
    if len(all_dims) > 1:
        issues.append({
            "level":   "error",
            "store":   "cross",
            "message": f"Chroma and FAISS have different dimensions: {all_dims}",
        })

    return {
        "aligned":        len(issues) == 0,
        "configured_dim": configured_dim,
        "embed_model":    OLLAMA_EMBED_MODEL,
        "chroma_audit":   chroma_audit,
        "faiss_audit":    faiss_audit,
        "issues":         issues,
    }


@capability(
    "fabric.vectors.compare",
    http_method="POST", http_path="/fabric/vectors/compare",
    http_tags=["fabric", "vectors"],
    memory="off", silent=True,
    description="Compare a record's vector representation across Chroma and FAISS. "
                "Input: record_id (str!), dataset_id (str — needed for FAISS lookup). "
                "Output: {chroma, faiss, match_status}.",
)
async def cap_vectors_compare(
    record_id:  str,
    dataset_id: str = "",
    trace_id=None,
) -> Dict:
    result: Dict[str, Any] = {"record_id": record_id}

    # Chroma lookup
    col = _chroma_col()
    chroma_emb = None
    if col:
        try:
            cr = col.get(ids=[record_id], include=["embeddings", "metadatas", "documents"])
            if cr.get("ids"):
                chroma_emb = cr["embeddings"][0] if cr.get("embeddings") else None
                result["chroma"] = {
                    "found":    True,
                    "document": (cr["documents"][0] or "")[:300] if cr.get("documents") else "",
                    "metadata": cr["metadatas"][0] if cr.get("metadatas") else {},
                    "stats":    _safe_embedding_stats(chroma_emb),
                }
                if not dataset_id:
                    dataset_id = (cr["metadatas"][0] or {}).get("dataset_id", "") if cr.get("metadatas") else ""
            else:
                result["chroma"] = {"found": False}
        except Exception as e:
            result["chroma"] = {"found": False, "error": str(e)}
    else:
        result["chroma"] = {"available": False}

    # FAISS lookup — check if the record ID exists in any shard/dataset index
    faiss_found = False
    faiss_stats: Dict[str, Any] = {}
    if FAISS_STORE.available:
        with FAISS_STORE._lock:
            # Check dataset index
            if dataset_id and dataset_id in FAISS_STORE._ds_ids:
                ids = FAISS_STORE._ds_ids[dataset_id]
                if record_id in ids:
                    faiss_found = True
                    idx_pos = ids.index(record_id)
                    faiss_stats["index"] = "dataset"
                    faiss_stats["dataset_id"] = dataset_id
                    faiss_stats["position"] = idx_pos
            # Check global shards
            if not faiss_found:
                for sname, sids in FAISS_STORE._shard_ids.items():
                    if record_id in sids:
                        faiss_found = True
                        idx_pos = sids.index(record_id)
                        faiss_stats["index"] = "global"
                        faiss_stats["shard"] = sname
                        faiss_stats["position"] = idx_pos
                        break

        result["faiss"] = {"found": faiss_found, **faiss_stats}
    else:
        result["faiss"] = {"available": False}

    # Match status
    if chroma_emb and faiss_found:
        result["match_status"] = "both_stores"
    elif chroma_emb and not faiss_found:
        result["match_status"] = "chroma_only"
    elif not chroma_emb and faiss_found:
        result["match_status"] = "faiss_only"
    else:
        result["match_status"] = "neither"

    return result


# ─────────────────────────────────────────────────────────────────────────────
# PANEL ROUTE
# ─────────────────────────────────────────────────────────────────────────────

_PANEL_PATH = Path(__file__).parent / "vector_browser_panel.html"

from starlette.responses import HTMLResponse as _HTMLResponse

@APP.get("/fabric/vectors/panel", include_in_schema=False)
async def _vectors_panel():
    try:
        html = _PANEL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = (
            "<!DOCTYPE html><html><body style='background:#181614;color:#c96b6b;"
            "font-family:monospace;padding:40px'>"
            "<h2>vector_browser_panel.html not found</h2>"
            f"<p>Expected: {_PANEL_PATH}</p>"
            "</body></html>"
        )
    return _HTMLResponse(html)


# ─────────────────────────────────────────────────────────────────────────────
# UI REGISTRATION
# ─────────────────────────────────────────────────────────────────────────────

from Vera.Orchestration.capability_orchestration import register_ui as _reg_ui

_reg_ui(
    "vector-browser-panel",
    "Vector Browser",
    "\u25C8",
    """<div style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/fabric/vectors/panel"
          style="flex:1;border:none;width:100%;height:100%;background:#181614"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=[
        "fabric.vectors.overview", "fabric.vectors.chroma.browse",
        "fabric.vectors.chroma.get", "fabric.vectors.chroma.datasets",
        "fabric.vectors.faiss.shards", "fabric.vectors.faiss.sample",
        "fabric.vectors.audit", "fabric.vectors.compare",
    ],
    mode="inject",   # hosted as a sub-section in the Data Fabric panel
    tab_order=36,
)

log.info("vector browser panel registered at /fabric/vectors/panel")