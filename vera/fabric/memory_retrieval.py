"""
memory_retrieval.py — Canonical agent-facing retrieval for the data fabric.

Why this module exists
──────────────────────
LLM agents struggled with the raw fabric caps: fabric.query returns big JSON
blobs that get flat-truncated by the loop preview budget, results cluster into
near-duplicates (many records from one noisy dataset), fabric.datasets returns
thousands of rows, and there was no way to read a full record by id at all.

This module provides ONE retrieval door designed for models:

  memory.seek     — hybrid search (FAISS + Chroma + PG full-text fused with
                    reciprocal-rank fusion), near-duplicate collapse, MMR-style
                    diversity selection, recency boost, and a self-sizing text
                    rendering that fits the caller's context budget (max_chars)
                    instead of being cut mid-JSON by the loop.
  memory.read     — full verbatim record by id (fabric first, then the session
                    memory store), paged via offset/max_chars.
  memory.map      — namespace-level dataset browser (one level at a time),
                    replacing "list all datasets".
  memory.tooling  — switch between 'canonical' (hide the overlapping fabric /
                    memory read caps from agent discovery) and 'full'
                    (legacy behaviour, nothing hidden).

The gate is SOFT: gated caps are only hidden from discovery surfaces
(CAP_INDEX.relevance_search, agent-loop toolkit/catalog builders). They stay
registered, callable, and available to panels, pipelines, DAGs and explicit
allowed_caps lists. Consumers pull the live gate set via
gated_discovery_caps(); when this module isn't loaded they fall back to an
empty set, i.e. full tooling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from Vera.vera.capability_orchestration import (
    CAPABILITY_REGISTRY,
    capability,
)

log = logging.getLogger("vera.memory_retrieval")


def _df():
    """The LIVE data_fabric module instance (loaded by the module loader)."""
    return (sys.modules.get("data_fabric")
            or sys.modules.get("Vera.vera.fabric.data_fabric"))


def _mem():
    """The LIVE session-memory module instance, if loaded."""
    return (sys.modules.get("memory")
            or sys.modules.get("Vera.vera.fabric.memory"))


# ─────────────────────────────────────────────────────────────────────────────
# TOOLING MODE (canonical | full) — persisted in the fabric SQLite
# ─────────────────────────────────────────────────────────────────────────────

_MODE_KEY = "memory.tooling_mode"
_MODES = ("canonical", "full")
_MODE_TTL = 5.0          # seconds the mode read is cached (toolkit builds are hot)
_mode_cache: Dict[str, Any] = {"value": "", "at": 0.0}

# Read/search doors that overlap memory.seek/read/map and confuse cap triage.
# Hidden from DISCOVERY only while mode == canonical — never blocked at call time.
GATED_DISCOVERY_CAPS: Set[str] = {
    "fabric.query", "fabric.datasets",
    "context.recall", "context.recall_fabric",
    "memory.search", "memory.recall", "memory.similar",
    "memory.session_history", "memory.find_similar_questions",
    "memory.traverse", "memory.get",
}


def _kv_conn():
    conn = _df()._sqlite_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS fabric_kv ("
                 "key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    return conn


def tooling_mode() -> str:
    now = time.monotonic()
    if _mode_cache["value"] and now - _mode_cache["at"] < _MODE_TTL:
        return _mode_cache["value"]
    mode = ""
    try:
        conn = _kv_conn()
        try:
            row = conn.execute("SELECT value FROM fabric_kv WHERE key=?",
                               (_MODE_KEY,)).fetchone()
            mode = (row[0] if row else "") or ""
        finally:
            conn.close()
    except Exception as e:
        log.debug("tooling_mode read: %s", e)
    if mode not in _MODES:
        mode = os.getenv("VERA_MEMORY_TOOLING", "canonical").strip().lower()
        if mode not in _MODES:
            mode = "canonical"
    _mode_cache.update(value=mode, at=now)
    return mode


def _set_tooling_mode(mode: str) -> None:
    conn = _kv_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO fabric_kv (key, value, updated_at) VALUES (?,?,?)",
            (_MODE_KEY, mode, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()
    _mode_cache.update(value=mode, at=time.monotonic())


def gated_discovery_caps() -> Set[str]:
    """The caps discovery surfaces should hide right now (empty in full mode)."""
    try:
        return set(GATED_DISCOVERY_CAPS) if tooling_mode() == "canonical" else set()
    except Exception:
        return set()


# ─────────────────────────────────────────────────────────────────────────────
# SEEK PIPELINE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_RRF_K = 60               # reciprocal-rank fusion constant (scale-free fusion)
_CANDIDATE_POOL = 120     # candidates pulled before dedup/diversity
_SEEK_MIN_SCORE = float(os.getenv("FABRIC_SEEK_MIN_SCORE", "0.28"))  # cosine relevance floor
_DUP_JACCARD = 0.65       # token-set similarity above which records collapse
_MMR_PENALTY = 0.35       # diversity penalty weight during final selection
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


def _tok(text: str) -> Set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()[:800]))


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter) if inter else 0.0


def _dt(created_at: str) -> Optional[datetime]:
    if not created_at:
        return None
    s = str(created_at).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _parse_since(since: str) -> Optional[datetime]:
    """Accepts '7d' / '12h' / '30m' or an ISO date."""
    s = (since or "").strip().lower()
    if not s:
        return None
    m = re.fullmatch(r"(\d+)\s*([mhdw])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        mult = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
        return datetime.now(timezone.utc) - timedelta(seconds=n * mult)
    return _dt(s)


def _age_str(created_at: str) -> str:
    d = _dt(created_at)
    if not d:
        return "?"
    secs = max(0, (datetime.now(timezone.utc) - d).total_seconds())
    if secs < 3600:        return f"{int(secs // 60)}m"
    if secs < 86400:       return f"{int(secs // 3600)}h"
    if secs < 86400 * 60:  return f"{int(secs // 86400)}d"
    return f"{int(secs // (86400 * 30))}mo"


def _recency_boost(created_at: str) -> float:
    d = _dt(created_at)
    if not d:
        return 1.0
    age_days = max(0.0, (datetime.now(timezone.utc) - d).total_seconds() / 86400)
    return 1.0 + 0.25 * math.exp(-age_days / 45.0)


def _norm_rec(rid: str, rec: Any, score: float) -> Optional[Dict]:
    """Normalise a PG DataRecord or a SQLite row dict into one candidate shape."""
    if rec is None:
        return None
    if isinstance(rec, dict):
        try:
            tags = json.loads(rec.get("tags") or "[]")
        except Exception:
            tags = []
        return {"id": rid, "dataset_id": rec.get("dataset_id", "") or "",
                "text": rec.get("text", "") or "",
                "created_at": rec.get("created_at", "") or "",
                "tags": tags, "source": rec.get("source_id", "") or "",
                "score": score, "origin": "fabric"}
    return {"id": rid, "dataset_id": getattr(rec, "dataset_id", "") or "",
            "text": getattr(rec, "text", "") or "",
            "created_at": getattr(rec, "created_at", "") or "",
            "tags": getattr(rec, "tags", []) or [],
            "source": getattr(rec, "source", "") or "",
            "score": score, "origin": "fabric"}


async def _sqlite_rows_by_ids(df, ids: List[str]) -> Dict[str, dict]:
    if not ids:
        return {}
    def _fetch():
        conn = df._sqlite_conn()
        try:
            qmarks = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT * FROM fabric_records WHERE id IN ({qmarks})", ids
            ).fetchall()
            return {r["id"]: dict(r) for r in rows}
        finally:
            conn.close()
    try:
        return await asyncio.get_running_loop().run_in_executor(None, _fetch)
    except Exception as e:
        log.debug("seek sqlite fetch: %s", e)
        return {}


async def _gather_ranked_lists(df, query: str, scope: str, pool: int):
    """One ordered id list per backend; fused later with RRF (scale-free, so the
    incompatible raw score scales of FAISS/Chroma/PG never touch each other).

    Also returns the raw signals needed for a relevance FLOOR: the best cosine
    similarity per record (from faiss/chroma) and the set of records that were
    genuine keyword hits. Without this, seek fused purely by RANK, so the nearest
    vectors always rank #1 even when they are far away — surfacing unrelated rows
    as authoritative context. Returns (ranked, vec_sims, text_ids)."""
    ranked: List[List[str]] = []
    vec_sims: Dict[str, float] = {}
    text_ids: set = set()
    # An exact dataset scope goes to the backends; a prefix ('caps.' / 'caps')
    # is applied post-fetch against the global lists.
    scoped_ds = scope if (scope and not scope.endswith(".")) else ""

    def _note_vec(res):
        for rid, sim in res:
            vec_sims[rid] = max(vec_sims.get(rid, 0.0), float(sim))

    embedding = None
    try:
        embedding = await asyncio.wait_for(df._embed(query), timeout=10)
    except Exception as e:
        log.debug("seek embed: %s", e)

    if embedding:
        try:
            if getattr(df.FAISS_STORE, "available", False):
                res = (df.FAISS_STORE.search_dataset(scoped_ds, embedding, pool)
                       if scoped_ds else df.FAISS_STORE.search_global(embedding, pool))
                if res:
                    ranked.append([rid for rid, _ in res]); _note_vec(res)
        except Exception as e:
            log.debug("seek faiss: %s", e)
        try:
            # sync chroma HTTP client — keep it off the event loop
            res = await asyncio.to_thread(
                df.FABRIC_CHROMA.search, embedding,
                dataset_id=scoped_ds or None, top_k=pool)
            if res:
                ranked.append([rid for rid, _ in res]); _note_vec(res)
        except Exception as e:
            log.debug("seek chroma: %s", e)
        # A scoped call can also miss (scope was actually a prefix) — widen with
        # a global chroma pass so prefix scopes still get semantic candidates.
        if scoped_ds:
            try:
                res = await asyncio.to_thread(
                    df.FABRIC_CHROMA.search, embedding,
                    dataset_id=None, top_k=pool)
                if res:
                    ranked.append([rid for rid, _ in res]); _note_vec(res)
            except Exception:
                pass

    if getattr(df.FABRIC_PG, "available", False):
        try:
            res = await asyncio.wait_for(
                df.FABRIC_PG.search_text(query, scoped_ds or None, limit=pool),
                timeout=6)
            if res:
                ranked.append([rid for rid, _ in res])
                text_ids.update(rid for rid, _ in res)
        except Exception as e:
            log.debug("seek pg_text: %s", e)

    return ranked, vec_sims, text_ids


async def _memory_candidates(query: str, limit: int) -> List[Dict]:
    """Optional session-memory merge so seek is genuinely canonical."""
    mem = _mem()
    if mem is None or not hasattr(mem, "MEMORY"):
        return []
    try:
        res = await asyncio.wait_for(mem.MEMORY.search(query, limit=limit), timeout=8)
    except Exception as e:
        log.debug("seek memory merge: %s", e)
        return []
    out = []
    for rank, item in enumerate(res or []):
        rec = (item or {}).get("record") or {}
        rid = rec.get("id")
        if not rid or not rec.get("text"):
            continue
        out.append({"id": rid,
                    "dataset_id": f"memory.{rec.get('category') or rec.get('record_type') or 'node'}",
                    "text": rec.get("text", ""),
                    "created_at": rec.get("created_at", ""),
                    "tags": rec.get("tags", []) or [],
                    "source": "memory",
                    "score": 1.0 / (_RRF_K + rank),
                    "origin": "memory"})
    return out


def _render_seek(query: str, selected: List[Dict], candidates: int,
                 collapsed: int, max_chars: int) -> str:
    k = len(selected)
    if not k:
        return (f'MEMORY SEEK "{query}" — no matches.\n'
                f"→ Try broader wording, drop scope/since, or memory.map() to see "
                f"what datasets exist.")
    # Spend the budget on snippets: fixed overhead per entry + header/footer.
    snip = max(160, (max_chars - 260 - 100 * k) // k)
    dsets: Dict[str, int] = {}
    for c in selected:
        dsets[c["dataset_id"]] = dsets.get(c["dataset_id"], 0) + 1
    ds_summary = ", ".join(f"{d}×{n}" if n > 1 else d
                           for d, n in sorted(dsets.items(), key=lambda x: -x[1])[:6])
    lines = [f'MEMORY SEEK "{query}" — {k} of {candidates} candidates · '
             f"{collapsed} near-duplicates collapsed · datasets: {ds_summary}"]
    for i, c in enumerate(selected, 1):
        text = re.sub(r"\s+", " ", (c["text"] or "").strip())
        if len(text) > snip:
            cut = text[:snip].rsplit(" ", 1)[0]
            text = (cut or text[:snip]) + " …"
        dup = f" (+{c['collapsed']} similar)" if c.get("collapsed") else ""
        lines.append(f"[{i}] id={c['id']} · {c['dataset_id']} · "
                     f"{_age_str(c['created_at'])} · {c['score']:.2f}{dup}\n    {text}")
    lines.append('→ memory.read(record_id="<id>") for a full record · '
                 'memory.seek(scope="<dataset or prefix>") to narrow · '
                 "memory.map() to browse datasets")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "memory.seek",
    http_method="POST", http_path="/memory/seek", http_tags=["memory", "fabric"],
    memory="off",
    description=(
        "THE canonical way to search Vera's stored knowledge (data fabric + session "
        "memory). Hybrid keyword+semantic search across all storage backends with "
        "near-duplicate collapse and diversity selection, rendered as a compact text "
        "block sized to max_chars. WHEN TO USE: any time you need stored data, past "
        "results, ingested documents, news, research or telemetry — start here before "
        "any other fabric/memory cap. "
        "Params: query (str!), scope (str — dataset id or dot-prefix, e.g. 'cve_nvd' "
        "or 'caps'), since (str — '7d', '12h' or ISO date), k (int 8 — records "
        "returned), max_chars (int 4000 — size the text block to your context "
        "budget), include_memory (bool true — merge session-memory hits). "
        "Output: {text, results:[{n,id,dataset_id,score,created_at,collapsed}], "
        "count, candidates, collapsed, datasets}. Read the `text` field. Expand any "
        "hit with memory.read(record_id=<id>); browse namespaces with memory.map()."
    ),
)
async def cap_memory_seek(query: str, scope: str = "", since: str = "",
                          k: int = 8, max_chars: int = 4000,
                          include_memory: bool = True, trace_id=None) -> Dict:
    query = str(query or "").strip()
    if not query:
        return {"error": "query required"}
    df = _df()
    if df is None:
        return {"error": "data fabric not loaded"}

    try:    k = max(1, min(25, int(k)))
    except Exception: k = 8
    try:    max_chars = max(800, min(32000, int(max_chars)))
    except Exception: max_chars = 4000
    if isinstance(include_memory, str):
        include_memory = include_memory.lower() in ("1", "true", "yes")
    scope = (scope or "").strip()
    scope_prefix = scope.rstrip(".") if scope else ""
    since_dt = _parse_since(since)

    pool = _CANDIDATE_POOL
    ranked, vec_sims, text_ids = await _gather_ranked_lists(df, query, scope, pool)

    # RRF fusion — rank-based, so backend score scales never mix.
    fused: Dict[str, float] = {}
    for lst in ranked:
        for rank, rid in enumerate(lst):
            fused[rid] = fused.get(rid, 0.0) + 1.0 / (_RRF_K + rank)

    # Relevance floor: a record survives only if it is a decent SEMANTIC match
    # (cosine ≥ floor) or a genuine keyword hit. Without this, seek returned the
    # nearest 120 vectors no matter how far — the root of "unrelated context".
    # When the query produced vector signal but nothing clears the floor we keep
    # the single best (flagged weak) so a scoped lookup never hard-fails, rather
    # than dumping the whole far-away pool.
    weak_ctx = False
    if fused and vec_sims:
        passing = {rid for rid in fused
                   if vec_sims.get(rid, 0.0) >= _SEEK_MIN_SCORE or rid in text_ids}
        if passing:
            fused = {rid: s for rid, s in fused.items() if rid in passing}
        else:
            weak_ctx = True
            best = max(fused, key=lambda r: vec_sims.get(r, 0.0))
            fused = {best: fused[best]}

    cands: List[Dict] = []
    if fused:
        ordered = sorted(fused, key=fused.get, reverse=True)[:pool]
        recmap: Dict[str, Any] = {}
        if getattr(df.FABRIC_PG, "available", False):
            try:
                recmap = await asyncio.wait_for(
                    df.FABRIC_PG.get_by_ids(ordered), timeout=6) or {}
            except Exception as e:
                log.debug("seek pg get_by_ids: %s", e)
        missing = [r for r in ordered if r not in recmap]
        if missing:
            recmap.update(await _sqlite_rows_by_ids(df, missing))
        for rid in ordered:
            c = _norm_rec(rid, recmap.get(rid), fused[rid])
            if c and c["text"]:
                cands.append(c)
    else:
        # Every backend came back empty/down — degrade through the legacy path
        # (its 300-char summaries are still better than nothing).
        try:
            res = await df.execute_query({"text": query, "vector": query,
                                          "dataset_id": scope_prefix or None,
                                          "top_k": min(pool, k * 4)})
            for r in (res or {}).get("results", []):
                cands.append({"id": r.get("id"), "dataset_id": r.get("dataset_id", ""),
                              "text": r.get("text", "") or "",
                              "created_at": r.get("created_at", ""),
                              "tags": r.get("tags", []) or [],
                              "source": r.get("source", ""),
                              "score": float(r.get("score", 0) or 0),
                              "origin": "fabric"})
        except Exception as e:
            log.debug("seek legacy fallback: %s", e)

    if include_memory and not scope:
        cands.extend(await _memory_candidates(query, limit=max(4, k // 2)))

    # Scope prefix / since filters
    if scope_prefix:
        cands = [c for c in cands
                 if c["dataset_id"] == scope_prefix
                 or c["dataset_id"].startswith(scope_prefix + ".")
                 or c["dataset_id"] == scope]
    if since_dt:
        cands = [c for c in cands if (_dt(c["created_at"]) or since_dt) >= since_dt]

    if not cands:
        return {"text": _render_seek(query, [], 0, 0, max_chars),
                "results": [], "count": 0, "candidates": 0,
                "collapsed": 0, "datasets": []}

    # Normalise fused scores to ~0..1 for display, apply mild recency boost.
    top = max(c["score"] for c in cands) or 1.0
    for c in cands:
        c["score"] = (c["score"] / top) * _recency_boost(c["created_at"])
        c["_tok"] = _tok(c["text"])
    cands.sort(key=lambda c: -c["score"])

    # Near-duplicate collapse — keep the best of each cluster, count the rest.
    kept: List[Dict] = []
    for c in cands:
        dup_of = next((s for s in kept
                       if _jaccard(c["_tok"], s["_tok"]) >= _DUP_JACCARD), None)
        if dup_of is not None:
            dup_of["collapsed"] += 1
        else:
            c["collapsed"] = 0
            kept.append(c)
    collapsed_total = sum(s["collapsed"] for s in kept)

    # Greedy MMR-style pick: relevance minus similarity to already-picked minus
    # a small same-dataset crowding penalty — "best picture", not "best cluster".
    selected: List[Dict] = []
    remaining = list(kept)
    while remaining and len(selected) < k:
        best, best_val = None, -1e9
        for c in remaining:
            sim = max((_jaccard(c["_tok"], s["_tok"]) for s in selected), default=0.0)
            same_ds = sum(1 for s in selected if s["dataset_id"] == c["dataset_id"])
            val = c["score"] - _MMR_PENALTY * sim - 0.05 * same_ds
            if val > best_val:
                best, best_val = c, val
        selected.append(best)
        remaining.remove(best)

    text = _render_seek(query, selected, len(cands), collapsed_total, max_chars)
    results = [{"n": i, "id": c["id"], "dataset_id": c["dataset_id"],
                "score": round(c["score"], 3), "created_at": c["created_at"],
                "collapsed": c.get("collapsed", 0)}
               for i, c in enumerate(selected, 1)]
    out = {"text": text, "results": results, "count": len(selected),
           "candidates": len(cands), "collapsed": collapsed_total,
           "weak": bool(weak_ctx),
           "datasets": sorted({c["dataset_id"] for c in selected})}
    if weak_ctx:
        out["note"] = ("No strongly-relevant stored knowledge — the closest matches are "
                       "below the relevance floor. Treat this as little/no data on the topic "
                       "rather than authoritative context.")
    return out


@capability(
    "memory.read",
    http_method="POST", http_path="/memory/read", http_tags=["memory", "fabric"],
    memory="off",
    description=(
        "Read ONE full record verbatim by id (ids come from memory.seek / "
        "fabric.query results). Checks the fabric first, then the session-memory "
        "store. Params: record_id (str!), offset (int 0 — character offset for "
        "paging), max_chars (int 6000), include_data (bool false — include the "
        "record's structured data payload). "
        "Output: {text, record_id, dataset_id, total_chars, offset, next_offset, "
        "created_at, tags, source}. next_offset is null when the record is fully "
        "read; otherwise call again with offset=next_offset."
    ),
)
async def cap_memory_read(record_id: str, offset: int = 0, max_chars: int = 6000,
                          include_data: bool = False, trace_id=None) -> Dict:
    record_id = str(record_id or "").strip()
    if not record_id:
        return {"error": "record_id required"}
    df = _df()
    if df is None:
        return {"error": "data fabric not loaded"}
    try:    offset = max(0, int(offset))
    except Exception: offset = 0
    try:    max_chars = max(500, min(32000, int(max_chars)))
    except Exception: max_chars = 6000
    if isinstance(include_data, str):
        include_data = include_data.lower() in ("1", "true", "yes")

    rec: Optional[Dict] = None
    if getattr(df.FABRIC_PG, "available", False):
        try:
            got = await asyncio.wait_for(df.FABRIC_PG.get_by_ids([record_id]), timeout=6)
            raw = (got or {}).get(record_id)
            rec = _norm_rec(record_id, raw, 0.0)
            if rec is not None and include_data:
                rec["data"] = getattr(raw, "data", None)
        except Exception as e:
            log.debug("read pg: %s", e)
    if rec is None:
        rows = await _sqlite_rows_by_ids(df, [record_id])
        if rows.get(record_id):
            rec = _norm_rec(record_id, rows[record_id], 0.0)
            if include_data:
                try:
                    rec["data"] = json.loads(rows[record_id].get("data") or "{}")
                except Exception:
                    pass
    if rec is None:
        # Fall through to the session-memory store — seek can surface its nodes.
        mem = _mem()
        if mem is not None and hasattr(mem, "MEMORY"):
            try:
                node = await mem.MEMORY.get(record_id)
                if node:
                    nd = node.to_dict() if hasattr(node, "to_dict") else dict(node)
                    rec = {"id": record_id,
                           "dataset_id": f"memory.{nd.get('category') or 'node'}",
                           "text": nd.get("text", "") or "",
                           "created_at": nd.get("created_at", "") or "",
                           "tags": nd.get("tags", []) or [],
                           "source": "memory", "score": 0.0, "origin": "memory"}
            except Exception as e:
                log.debug("read memory store: %s", e)
    if rec is None:
        return {"error": f"record not found: {record_id}",
                "hint": "ids come from memory.seek results (results[].id)"}

    full = rec["text"] or ""
    total = len(full)
    chunk = full[offset:offset + max_chars]
    next_offset = offset + len(chunk)
    out = {"text": chunk, "record_id": record_id,
           "dataset_id": rec["dataset_id"], "total_chars": total,
           "offset": offset,
           "next_offset": next_offset if next_offset < total else None,
           "created_at": rec["created_at"], "tags": rec["tags"],
           "source": rec["source"]}
    if include_data and rec.get("data") is not None:
        payload = json.dumps(rec["data"], default=str)
        out["data"] = rec["data"] if len(payload) <= 6000 else payload[:6000]
    return out


@capability(
    "memory.map",
    http_method="GET", http_path="/memory/map", http_tags=["memory", "fabric"],
    memory="off", silent=True,
    description=(
        "Browse the fabric's dataset namespaces ONE LEVEL at a time — there are "
        "thousands of datasets, never try to list them all. prefix='' shows the "
        "top-level namespaces with aggregate counts; prefix='caps' shows caps.*; "
        "and so on, one dot-segment per call. Params: prefix (str), max_entries "
        "(int 40). Output: {text, entries:[{name, datasets, records, expandable}], "
        "count}. Search inside a namespace with memory.seek(scope=<name>)."
    ),
)
async def cap_memory_map(prefix: str = "", max_entries: int = 40,
                         trace_id=None) -> Dict:
    fab_ds = CAPABILITY_REGISTRY.get("fabric.datasets", {}).get("func")
    if not fab_ds:
        return {"error": "fabric.datasets not available"}
    try:    max_entries = max(5, min(200, int(max_entries)))
    except Exception: max_entries = 40
    prefix = (prefix or "").strip().rstrip(".")

    entries: List[Dict] = []
    if prefix:
        res = await fab_ds(parent=prefix)
        for d in (res or {}).get("datasets", []):
            entries.append({"name": d.get("dataset_id", "?"),
                            "datasets": 1,
                            "records": d.get("record_count", 0) or 0,
                            "expandable": bool(d.get("has_children"))})
    else:
        # Group the flat dataset list by first dot-segment ourselves — the raw
        # list is thousands of rows and must never reach an LLM.
        res = await fab_ds()
        groups: Dict[str, Dict] = {}
        for d in (res or {}).get("datasets", []):
            did = d.get("dataset_id", "") or ""
            seg = did.split(".", 1)[0] or did
            g = groups.setdefault(seg, {"name": seg, "datasets": 0,
                                        "records": 0, "expandable": False})
            g["datasets"] += 1
            g["records"] += d.get("record_count", 0) or 0
            if "." in did:
                g["expandable"] = True
        entries = list(groups.values())

    entries.sort(key=lambda e: -e["records"])
    total = len(entries)
    shown = entries[:max_entries]

    where = f'prefix="{prefix}"' if prefix else "top level"
    lines = [f"FABRIC MAP ({where} — {total} entries, "
             f"{sum(e['records'] for e in entries):,} records)"]
    for e in shown:
        expand = (f'  → memory.map(prefix="{e["name"]}")' if e["expandable"] else "")
        ds_part = f"{e['datasets']} ds · " if e["datasets"] > 1 else ""
        lines.append(f"  {e['name']:<42} {ds_part}{e['records']:,} rec{expand}")
    if total > len(shown):
        lines.append(f"  … and {total - len(shown)} more (raise max_entries or narrow prefix)")
    lines.append('→ memory.seek(query="…", scope="<name>") to search inside a namespace')

    return {"text": "\n".join(lines), "entries": shown, "count": total,
            "prefix": prefix}


@capability(
    "memory.tooling",
    http_method="POST", http_path="/memory/tooling", http_tags=["memory", "fabric"],
    memory="off",
    schema={"properties": {"mode": {
        "enum": ["", "canonical", "full"],
        "description": "Empty = report current mode; canonical/full = switch."}}},
    description=(
        "Get or set the agent-facing retrieval tooling mode. mode='' reports the "
        "current mode. 'canonical' hides the overlapping fabric/memory read caps "
        "from agent discovery so agents converge on memory.seek/read/map; 'full' "
        "restores every cap to discovery (legacy behaviour). The gate is soft — "
        "gated caps always stay callable when invoked explicitly, and panels/"
        "pipelines are unaffected. Persists across restarts."
    ),
)
async def cap_memory_tooling(mode: str = "", trace_id=None) -> Dict:
    current = tooling_mode()
    gated = sorted(GATED_DISCOVERY_CAPS)
    if not mode:
        return {"mode": current,
                "gated_caps": gated if current == "canonical" else [],
                "gate_set": gated}
    mode = str(mode).strip().lower()
    if mode not in _MODES:
        return {"error": f"mode must be one of {list(_MODES)} (or '' to read)"}
    try:
        _set_tooling_mode(mode)
    except Exception as e:
        return {"error": f"failed to persist mode: {e}"}
    return {"ok": True, "mode": mode, "previous": current,
            "gated_caps": gated if mode == "canonical" else []}
