"""
markets_capabilities.py — Multi-source crypto OHLCV ingestion (CCXT)
====================================================================

Pulls full OHLCV history for selected crypto assets from any CCXT exchange
(Binance / Coinbase / Kraken / Bybit by default), stores every bar in the Data
Fabric, keeps a persistent *watchlist* and refreshes it on a configurable
schedule.

Design notes
------------
• CCXT is driven *synchronously inside `loop.run_in_executor`* so we don't block
  the event loop and stay version-agnostic (the unified `load_markets` /
  `fetch_ohlcv` API is stable across ccxt 1.x → 4.x).
• Every fetch runs as a **background job** that returns a `job_id` immediately and
  streams progress — this is what removes the old 30-second HTTP timeout.
• One dataset per asset+timeframe: ``mkt.{exchange}.{base}_{quote}.{tf}``.
  Each bar is a fabric_records row with a *deterministic* id
  ``f"{dataset_id}:{ts_ms}"`` so re-ingesting hits ``INSERT OR REPLACE`` and
  dedupes automatically (incremental updates never duplicate).
• Bars are written through the fabric single-writer queue (`_enqueue_write`),
  deliberately bypassing `ingest_dataset` so the per-ingest LLM
  entity-extraction / auto-tag never fires on numeric bar data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path as _Path
from typing import Dict, List, Optional

log = logging.getLogger("vera.markets")

try:
    from Vera.vera.capability_orchestration import (
        APP, capability, emit_event, now_iso, register_ui, schedule,
    )
    from Vera.vera.fabric.data_fabric import _enqueue_write, _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.markets").warning("markets caps unavailable: %s", e)
    _CAP_AVAILABLE = False

try:
    import ccxt
    HAS_CCXT = True
except Exception:                              # pragma: no cover
    HAS_CCXT = False

# ─────────────────────────────────────────────────────────────────────────────
# Constants & in-memory state
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_EXCHANGES  = ["binance", "coinbase", "kraken", "bybit"]
DEFAULT_TIMEFRAMES = ["1d", "1h"]
ALL_TIMEFRAMES     = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]

# Earliest point we walk back to for a "full" backfill. Crypto of interest
# postdates this; exchanges that only serve recent data (e.g. Kraken) simply
# return what they have.
FULL_HISTORY_START = "2013-01-01T00:00:00Z"

TF_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000,
}

_EXCHANGES:     Dict[str, object] = {}     # exchange_id -> ccxt instance
_MARKETS_CACHE: Dict[str, dict]   = {}     # exchange_id -> {symbol: market}
_MARKETS_TS:    Dict[str, float]  = {}     # exchange_id -> last load epoch
_MARKETS_TTL    = 3600                      # re-load market list hourly

_MKT_JOBS:     Dict[str, dict] = {}        # job_id -> job state
_MKT_INFLIGHT: set             = set()      # dataset_ids currently updating
_MAX_JOBS      = 60
_MAX_BARS_PER_TF = 3_000_000               # safety cap for a single backfill

_TABLE_READY = False


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ms_to_iso(ms: int) -> str:
    return datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")

def _iso_to_ms(s: str) -> Optional[int]:
    if not s:
        return None
    try:
        s2 = s.replace("Z", "")
        return int(datetime.fromisoformat(s2).replace(tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        return None

def _slug(symbol: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (symbol or "").lower()).strip("_") or "asset"

def _dataset_id(ex_id: str, symbol: str, tf: str) -> str:
    return f"mkt.{ex_id.lower()}.{_slug(symbol)}.{tf}"

def _as_list(v, default: List[str]) -> List[str]:
    if v is None or v == "":
        return list(default)
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        try:
            j = json.loads(v)
            if isinstance(j, list):
                return [str(x).strip() for x in j if str(x).strip()]
        except Exception:
            pass
        return [p.strip() for p in v.split(",") if p.strip()]
    return list(default)

def _get_exchange(ex_id: str):
    """Return a cached ccxt exchange instance (no network)."""
    ex_id = (ex_id or "binance").lower()
    if ex_id in _EXCHANGES:
        return _EXCHANGES[ex_id]
    if not HAS_CCXT:
        return None
    if not hasattr(ccxt, ex_id):
        raise ValueError(f"unknown exchange '{ex_id}'")
    inst = getattr(ccxt, ex_id)({"enableRateLimit": True, "timeout": 30000})
    _EXCHANGES[ex_id] = inst
    return inst


# ─────────────────────────────────────────────────────────────────────────────
# SQLite (watchlist table + dataset metadata) — fresh WAL connections in executor
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_table_sync():
    global _TABLE_READY
    conn = _sqlite_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mkt_watchlist (
                id                  TEXT PRIMARY KEY,
                exchange            TEXT,
                symbol              TEXT,
                timeframes          TEXT,
                auto_update         INTEGER DEFAULT 1,
                update_interval_min INTEGER DEFAULT 60,
                last_update         TEXT,
                last_status         TEXT,
                created_at          TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()
    _TABLE_READY = True

async def _ensure_table():
    if _TABLE_READY:
        return
    await asyncio.get_running_loop().run_in_executor(None, _ensure_table_sync)

def _ensure_dataset_sync(ds: str, ex_id: str, tf: str):
    conn = _sqlite_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO fabric_datasets (dataset_id, record_count, created_at, updated_at) "
            "VALUES (?,0,?,?)", (ds, now_iso(), now_iso()))
        for tag in ("crypto", "ohlcv", ex_id.lower(), tf):
            conn.execute(
                "INSERT OR REPLACE INTO fabric_dataset_tags (dataset_id, tag, source, created_at) "
                "VALUES (?,?,?,?)", (ds, tag, "markets", now_iso()))
        conn.commit()
    finally:
        conn.close()

def _last_bar_ms_sync(ds: str) -> Optional[int]:
    conn = _sqlite_conn()
    try:
        row = conn.execute(
            "SELECT MAX(created_at) FROM fabric_records WHERE dataset_id=?", (ds,)).fetchone()
        return _iso_to_ms(row[0]) if row and row[0] else None
    finally:
        conn.close()

def _reconcile_count_sync(ds: str) -> int:
    conn = _sqlite_conn()
    try:
        conn.execute(
            "UPDATE fabric_datasets SET "
            "record_count=(SELECT COUNT(*) FROM fabric_records WHERE dataset_id=?), updated_at=? "
            "WHERE dataset_id=?", (ds, now_iso(), ds))
        conn.commit()
        row = conn.execute(
            "SELECT record_count FROM fabric_datasets WHERE dataset_id=?", (ds,)).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        conn.close()

def _bar_counts_sync() -> Dict[str, int]:
    conn = _sqlite_conn()
    try:
        rows = conn.execute(
            "SELECT dataset_id, COUNT(*) FROM fabric_records "
            "WHERE dataset_id LIKE 'mkt.%' GROUP BY dataset_id").fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()

def _watchlist_rows_sync() -> List[dict]:
    conn = _sqlite_conn()
    try:
        # SELECT * so later-added columns (e.g. live_track from the data module)
        # flow through without another touch here.
        rows = conn.execute("SELECT * FROM mkt_watchlist ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def _watchlist_upsert_sync(row: dict):
    conn = _sqlite_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO mkt_watchlist "
            "(id, exchange, symbol, timeframes, auto_update, update_interval_min, "
            " last_update, last_status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (row["id"], row["exchange"], row["symbol"], json.dumps(row["timeframes"]),
             int(row.get("auto_update", 1)), int(row.get("update_interval_min", 60)),
             row.get("last_update"), row.get("last_status"),
             row.get("created_at") or now_iso()))
        conn.commit()
    finally:
        conn.close()

def _watchlist_get_sync(wid: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM mkt_watchlist WHERE id=?", (wid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()

def _watchlist_set_status_sync(wid: str, last_update: str, status: str):
    conn = _sqlite_conn()
    try:
        conn.execute("UPDATE mkt_watchlist SET last_update=?, last_status=? WHERE id=?",
                     (last_update, status, wid))
        conn.commit()
    finally:
        conn.close()

def _watchlist_delete_sync(wid: str):
    conn = _sqlite_conn()
    try:
        conn.execute("DELETE FROM mkt_watchlist WHERE id=?", (wid,))
        conn.commit()
    finally:
        conn.close()

def _delete_asset_data_sync(ex_id: str, symbol: str) -> int:
    """Delete every dataset (all timeframes) for an asset."""
    prefix = f"mkt.{ex_id.lower()}.{_slug(symbol)}."
    conn = _sqlite_conn()
    try:
        dss = [r[0] for r in conn.execute(
            "SELECT dataset_id FROM fabric_datasets WHERE dataset_id LIKE ?", (prefix + "%",)).fetchall()]
        # also catch datasets that exist only as records
        dss += [r[0] for r in conn.execute(
            "SELECT DISTINCT dataset_id FROM fabric_records WHERE dataset_id LIKE ?", (prefix + "%",)).fetchall()]
        dss = sorted(set(dss))
        for ds in dss:
            conn.execute("DELETE FROM fabric_records WHERE dataset_id=?", (ds,))
            conn.execute("DELETE FROM fabric_datasets WHERE dataset_id=?", (ds,))
            conn.execute("DELETE FROM fabric_dataset_tags WHERE dataset_id=?", (ds,))
        conn.commit()
        return len(dss)
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Market list (searchable symbols)
# ─────────────────────────────────────────────────────────────────────────────

def _load_markets_sync(ex_id: str) -> dict:
    ex = _get_exchange(ex_id)
    if ex is None:
        return {}
    return ex.load_markets() or {}

async def _markets(ex_id: str, force: bool = False) -> dict:
    ex_id = (ex_id or "binance").lower()
    now = time.time()
    if not force and ex_id in _MARKETS_CACHE and (now - _MARKETS_TS.get(ex_id, 0)) < _MARKETS_TTL:
        return _MARKETS_CACHE[ex_id]
    mkts = await asyncio.get_running_loop().run_in_executor(None, _load_markets_sync, ex_id)
    _MARKETS_CACHE[ex_id] = mkts
    _MARKETS_TS[ex_id] = now
    return mkts


# ─────────────────────────────────────────────────────────────────────────────
# The ingestion job — runs in the background, paginates full history
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_page_sync(ex_id: str, symbol: str, tf: str, since: Optional[int], limit: int):
    ex = _get_exchange(ex_id)
    return ex.fetch_ohlcv(symbol, timeframe=tf, since=since, limit=limit)

def _routed_ingestor(ex_id: str):
    """Non-CCXT providers (yahoo stocks/ETFs, custom price series) register
    ingestors in markets_data_capabilities.PROVIDER_INGESTORS so this module's
    job runner + auto-update scheduler serve every asset class."""
    md = sys.modules.get("markets_data_capabilities")
    return (getattr(md, "PROVIDER_INGESTORS", {}) or {}).get((ex_id or "").lower())

async def _ingest_timeframe(job: dict, ex_id: str, symbol: str, tf: str, full: bool) -> int:
    """Backfill or incrementally update one (asset, timeframe). Returns # new bars."""
    ds = _dataset_id(ex_id, symbol, tf)
    routed = _routed_ingestor(ex_id)
    if routed is not None:
        if ds in _MKT_INFLIGHT:
            job["errors"][tf] = "already updating"
            return 0
        _MKT_INFLIGHT.add(ds)
        try:
            return await routed(job, symbol, tf, full)
        finally:
            _MKT_INFLIGHT.discard(ds)
    if ds in _MKT_INFLIGHT:
        job["errors"][tf] = "already updating"
        return 0
    _MKT_INFLIGHT.add(ds)
    loop = asyncio.get_running_loop()
    tf_ms = TF_MS.get(tf, 86_400_000)
    new_total = 0
    try:
        await loop.run_in_executor(None, _ensure_dataset_sync, ds, ex_id, tf)
        last_ms = await loop.run_in_executor(None, _last_bar_ms_sync, ds)

        if full or last_ms is None:
            since = _iso_to_ms(FULL_HISTORY_START)
        else:
            since = last_ms + tf_ms          # incremental: only newer bars
        cursor_floor = last_ms or -1
        now_ms = int(time.time() * 1000)
        limit = 1000
        empty_streak = 0
        prev_last = None

        while True:
            if job.get("stop"):
                break
            try:
                page = await loop.run_in_executor(None, _fetch_page_sync, ex_id, symbol, tf, since, limit)
            except Exception as e:
                job["errors"][tf] = str(e)[:300]
                log.warning("fetch_ohlcv %s %s: %s", ex_id, symbol, e)
                break

            if not page:
                # Tolerate gaps / pre-listing empty windows when walking forward.
                if since is None or since >= now_ms:
                    break
                empty_streak += 1
                if empty_streak > 300:
                    break
                since += limit * tf_ms
                continue
            empty_streak = 0

            bars = [b for b in page if b and b[0] is not None and b[0] > cursor_floor]
            if bars:
                recs = []
                for b in bars:
                    ts_ms = int(b[0])
                    o, h, l, c = b[1], b[2], b[3], b[4]
                    vol = b[5] if len(b) > 5 else None
                    recs.append({
                        "id": f"{ds}:{ts_ms}",
                        "dataset_id": ds,
                        "text": f"{symbol} {tf} {_ms_to_iso(ts_ms)} O{o} H{h} L{l} C{c} V{vol}"[:300],
                        "data": {
                            "timestamp": ts_ms, "ts": _ms_to_iso(ts_ms),
                            "open": o, "high": h, "low": l, "close": c, "volume": vol,
                            "symbol": symbol, "exchange": ex_id, "timeframe": tf,
                        },
                        "source_id": f"ccxt:{ex_id}",
                        "tags": ["crypto", "ohlcv", ex_id, tf],
                        "created_at": _ms_to_iso(ts_ms),
                    })
                for r in recs:
                    await _enqueue_write({"kind": "insert_record", "rec": r}, wait=False)
                new_total += len(recs)
                cursor_floor = max(cursor_floor, recs[-1]["data"]["timestamp"])

            last_ts = page[-1][0]
            job["fetched"][tf] = new_total
            job["stage"] = f"{symbol} {tf}: {new_total} bars"
            await emit_event({"type": "markets.fetch", "job_id": job["job_id"],
                              "stage": "progress", "exchange": ex_id, "symbol": symbol,
                              "timeframe": tf, "fetched": new_total,
                              "last_ts": _ms_to_iso(int(last_ts))})

            if last_ts == prev_last:           # no forward progress — stop
                break
            prev_last = last_ts
            since = last_ts + tf_ms
            if new_total >= _MAX_BARS_PER_TF:
                break
            if last_ts >= now_ms - tf_ms:      # caught up to the latest bar
                break
            if len(page) < limit:              # last available page
                break

        # Flush the write queue (FIFO: awaiting one op guarantees all prior ran)
        await _enqueue_write({"kind": "upsert_dataset", "dataset_id": ds, "increment": 0}, wait=True)
        total = await loop.run_in_executor(None, _reconcile_count_sync, ds)
        job["totals"][tf] = total
        return new_total
    finally:
        _MKT_INFLIGHT.discard(ds)

async def _ingest_job(job_id: str, ex_id: str, symbol: str, timeframes: List[str], full: bool):
    job = _MKT_JOBS[job_id]
    job["status"] = "running"
    try:
        for tf in timeframes:
            if job.get("stop"):
                break
            try:
                await _ingest_timeframe(job, ex_id, symbol, tf, full)
            except Exception as e:
                job["errors"][tf] = str(e)[:300]
                log.warning("ingest %s %s %s: %s", ex_id, symbol, tf, e)
        job["status"] = "error" if job["errors"] else "done"
    except Exception as e:
        job["status"] = "error"
        job["errors"]["_"] = str(e)[:300]
    finally:
        job["finished"] = now_iso()
        # Update watchlist status if this asset is watched
        wid = f"{ex_id}:{symbol}"
        try:
            row = await asyncio.get_running_loop().run_in_executor(None, _watchlist_get_sync, wid)
            if row:
                status = "ok" if job["status"] == "done" else ("; ".join(
                    f"{k}:{v}" for k, v in job["errors"].items())[:200] or "error")
                await asyncio.get_running_loop().run_in_executor(
                    None, _watchlist_set_status_sync, wid, now_iso(), status)
        except Exception:
            pass
        await emit_event({"type": "markets.fetch", "job_id": job_id, "stage": "done",
                          "exchange": ex_id, "symbol": symbol,
                          "status": job["status"], "fetched": job["fetched"]})

def _new_job(ex_id: str, symbol: str, timeframes: List[str], full: bool) -> dict:
    job_id = uuid.uuid4().hex[:8]
    job = {
        "job_id": job_id, "exchange": ex_id, "symbol": symbol,
        "timeframes": timeframes, "full": full, "status": "queued",
        "stage": "queued", "fetched": {}, "totals": {}, "errors": {},
        "stop": False, "started": now_iso(), "finished": None,
    }
    _MKT_JOBS[job_id] = job
    # prune old finished jobs
    if len(_MKT_JOBS) > _MAX_JOBS:
        done = [j for j in _MKT_JOBS.values() if j["status"] in ("done", "error")]
        done.sort(key=lambda j: j.get("finished") or "")
        for j in done[: len(_MKT_JOBS) - _MAX_JOBS]:
            _MKT_JOBS.pop(j["job_id"], None)
    return job


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    @capability(
        "markets.exchanges", http_method="GET", http_path="/markets/exchanges",
        http_tags=["markets"], memory="off", silent=True,
        description="List the default crypto exchanges and CCXT availability. "
                    "Output: {ccxt, exchanges:[{id,name,has_ohlcv}], timeframes}.",
    )
    async def cap_markets_exchanges(trace_id=None) -> dict:
        out = []
        for ex_id in DEFAULT_EXCHANGES:
            entry = {"id": ex_id, "name": ex_id.capitalize(), "has_ohlcv": True}
            if HAS_CCXT:
                try:
                    ex = _get_exchange(ex_id)
                    entry["has_ohlcv"] = bool(getattr(ex, "has", {}).get("fetchOHLCV", True))
                except Exception:
                    pass
            out.append(entry)
        return {"ccxt": HAS_CCXT, "exchanges": out,
                "timeframes": ALL_TIMEFRAMES, "default_timeframes": DEFAULT_TIMEFRAMES}

    @capability(
        "markets.timeframes", http_method="GET", http_path="/markets/timeframes",
        http_tags=["markets"], memory="off", silent=True,
        description="List timeframes an exchange supports. Input: exchange (str). "
                    "Output: {exchange, timeframes:[...]}.",
    )
    async def cap_markets_timeframes(exchange: str = "binance", trace_id=None) -> dict:
        if not HAS_CCXT:
            return {"error": "ccxt not installed", "timeframes": ALL_TIMEFRAMES}
        try:
            ex = _get_exchange(exchange)
            tfs = list((getattr(ex, "timeframes", None) or {}).keys()) or ALL_TIMEFRAMES
        except Exception as e:
            return {"error": str(e), "timeframes": ALL_TIMEFRAMES}
        return {"exchange": exchange.lower(), "timeframes": tfs}

    @capability(
        "markets.symbols", http_method="GET", http_path="/markets/symbols",
        http_tags=["markets"], memory="off", silent=True,
        description="Searchable symbol list for an exchange (loads & caches its market list). "
                    "Input: exchange (str), query (str — substring filter), limit (int=100). "
                    "Output: {exchange, count, symbols:[{symbol,base,quote,active}]}.",
    )
    async def cap_markets_symbols(exchange: str = "binance", query: str = "",
                                  limit: int = 100, trace_id=None) -> dict:
        if not HAS_CCXT:
            return {"error": "ccxt not installed"}
        try:
            mkts = await _markets(exchange)
        except Exception as e:
            return {"error": f"load_markets failed: {e}", "exchange": exchange.lower()}
        q = (query or "").strip().lower()
        items = []
        for sym, m in mkts.items():
            if not isinstance(m, dict):
                continue
            base = str(m.get("base", "")).lower()
            quote = str(m.get("quote", "")).lower()
            if q and (q not in sym.lower() and q not in base and q not in quote):
                continue
            items.append({"symbol": sym, "base": m.get("base", ""),
                          "quote": m.get("quote", ""),
                          "active": m.get("active", True) is not False})

        def _rank(it):
            b = str(it["base"]).lower()
            return (0 if b == q else (1 if b.startswith(q) else 2), it["symbol"])
        items.sort(key=_rank)
        return {"exchange": exchange.lower(), "count": len(items),
                "symbols": items[: max(1, min(500, limit))]}

    @capability(
        "markets.fetch", http_method="POST", http_path="/markets/fetch",
        http_tags=["markets"], memory="on",
        description="Start a background OHLCV backfill/refresh job for one asset. "
                    "Returns immediately (no timeout). "
                    "Input: exchange (str), symbol (str! e.g. 'BTC/USDT'), "
                    "timeframes (list/csv, default ['1d','1h']), full (bool=True — full history vs incremental). "
                    "Output: {ok, job_id, datasets:[...]}.",
    )
    async def cap_markets_fetch(exchange: str = "binance", symbol: str = "",
                                timeframes=None, full: bool = True, trace_id=None) -> dict:
        ex_id = (exchange or "binance").lower()
        if not HAS_CCXT and _routed_ingestor(ex_id) is None:
            return {"error": "ccxt not installed — run: pip install ccxt"}
        if not symbol:
            return {"error": "symbol required (e.g. 'BTC/USDT')"}
        tfs = _as_list(timeframes, DEFAULT_TIMEFRAMES)
        job = _new_job(ex_id, symbol, tfs, bool(full))
        asyncio.create_task(_ingest_job(job["job_id"], ex_id, symbol, tfs, bool(full)))
        await emit_event({"type": "markets.fetch", "job_id": job["job_id"], "stage": "started",
                          "exchange": ex_id, "symbol": symbol, "timeframes": tfs})
        return {"ok": True, "job_id": job["job_id"], "exchange": ex_id, "symbol": symbol,
                "datasets": [_dataset_id(ex_id, symbol, tf) for tf in tfs]}

    @capability(
        "markets.jobs", http_method="GET", http_path="/markets/jobs",
        http_tags=["markets"], memory="off", silent=True,
        description="Status of background fetch jobs. Input: job_id (str optional). "
                    "Output: {jobs:[...]} or {job:{...}}.",
    )
    async def cap_markets_jobs(job_id: str = "", trace_id=None) -> dict:
        if job_id:
            j = _MKT_JOBS.get(job_id)
            return {"job": j} if j else {"error": "no such job", "job_id": job_id}
        jobs = sorted(_MKT_JOBS.values(), key=lambda j: j.get("started") or "", reverse=True)
        return {"jobs": jobs, "inflight": sorted(_MKT_INFLIGHT)}

    @capability(
        "markets.watchlist.list", http_method="GET", http_path="/markets/watchlist",
        http_tags=["markets"], memory="off", silent=True,
        description="List watched assets with config, stored bar counts and last update. "
                    "Output: {watchlist:[{id,exchange,symbol,timeframes,auto_update,"
                    "update_interval_min,last_update,last_status,counts}]}.",
    )
    async def cap_markets_watchlist_list(trace_id=None) -> dict:
        await _ensure_table()
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(None, _watchlist_rows_sync)
        counts = await loop.run_in_executor(None, _bar_counts_sync)
        out = []
        for r in rows:
            tfs = _as_list(r.get("timeframes"), [])
            out.append({
                "id": r["id"], "exchange": r["exchange"], "symbol": r["symbol"],
                "timeframes": tfs,
                "auto_update": bool(r.get("auto_update", 1)),
                "update_interval_min": r.get("update_interval_min", 60),
                "last_update": r.get("last_update"), "last_status": r.get("last_status"),
                "live_track": bool(r.get("live_track", 0)),
                "counts": {tf: counts.get(_dataset_id(r["exchange"], r["symbol"], tf), 0) for tf in tfs},
            })
        return {"watchlist": out, "count": len(out)}

    @capability(
        "markets.watchlist.add", http_method="POST", http_path="/markets/watchlist/add",
        http_tags=["markets"], memory="on",
        description="Add an asset to the watchlist and (optionally) start a full backfill. "
                    "Input: exchange (str), symbol (str!), timeframes (list/csv, default ['1d','1h']), "
                    "auto_update (bool=True), update_interval_min (int=60), backfill (bool=True). "
                    "Output: {ok, id, job_id}.",
    )
    async def cap_markets_watchlist_add(exchange: str = "binance", symbol: str = "",
                                        timeframes=None, auto_update: bool = True,
                                        update_interval_min: int = 60,
                                        backfill: bool = True, trace_id=None) -> dict:
        if not symbol:
            return {"error": "symbol required"}
        await _ensure_table()
        ex_id = (exchange or "binance").lower()
        tfs = _as_list(timeframes, DEFAULT_TIMEFRAMES)
        wid = f"{ex_id}:{symbol}"
        row = {"id": wid, "exchange": ex_id, "symbol": symbol, "timeframes": tfs,
               "auto_update": 1 if auto_update else 0,
               "update_interval_min": max(1, int(update_interval_min)),
               "last_update": None, "last_status": "queued", "created_at": now_iso()}
        await asyncio.get_running_loop().run_in_executor(None, _watchlist_upsert_sync, row)
        job_id = None
        if backfill and HAS_CCXT:
            job = _new_job(ex_id, symbol, tfs, True)
            job_id = job["job_id"]
            asyncio.create_task(_ingest_job(job_id, ex_id, symbol, tfs, True))
        await emit_event({"type": "markets.watchlist", "stage": "added",
                          "exchange": ex_id, "symbol": symbol})
        return {"ok": True, "id": wid, "job_id": job_id,
                "ccxt": HAS_CCXT, "timeframes": tfs}

    @capability(
        "markets.watchlist.config", http_method="POST", http_path="/markets/watchlist/config",
        http_tags=["markets"], memory="on",
        description="Update a watched asset's config. Input: exchange (str), symbol (str!), "
                    "auto_update (bool), update_interval_min (int), timeframes (list/csv). "
                    "Output: {ok, id}.",
    )
    async def cap_markets_watchlist_config(exchange: str = "binance", symbol: str = "",
                                           auto_update: bool = True,
                                           update_interval_min: int = 60,
                                           timeframes=None, trace_id=None) -> dict:
        if not symbol:
            return {"error": "symbol required"}
        await _ensure_table()
        ex_id = (exchange or "binance").lower()
        wid = f"{ex_id}:{symbol}"
        loop = asyncio.get_running_loop()
        existing = await loop.run_in_executor(None, _watchlist_get_sync, wid)
        if not existing:
            return {"error": "not in watchlist", "id": wid}
        tfs = _as_list(timeframes, _as_list(existing.get("timeframes"), DEFAULT_TIMEFRAMES))
        row = {"id": wid, "exchange": ex_id, "symbol": symbol, "timeframes": tfs,
               "auto_update": 1 if auto_update else 0,
               "update_interval_min": max(1, int(update_interval_min)),
               "last_update": existing.get("last_update"),
               "last_status": existing.get("last_status"),
               "created_at": existing.get("created_at") or now_iso()}
        await loop.run_in_executor(None, _watchlist_upsert_sync, row)
        return {"ok": True, "id": wid, "timeframes": tfs}

    @capability(
        "markets.watchlist.remove", http_method="POST", http_path="/markets/watchlist/remove",
        http_tags=["markets"], memory="on",
        description="Remove an asset from the watchlist. Input: exchange (str), symbol (str!), "
                    "delete_data (bool=False — also delete stored OHLCV datasets). "
                    "Output: {ok, id, datasets_deleted}.",
    )
    async def cap_markets_watchlist_remove(exchange: str = "binance", symbol: str = "",
                                           delete_data: bool = False, trace_id=None) -> dict:
        if not symbol:
            return {"error": "symbol required"}
        await _ensure_table()
        ex_id = (exchange or "binance").lower()
        wid = f"{ex_id}:{symbol}"
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _watchlist_delete_sync, wid)
        deleted = 0
        if delete_data:
            deleted = await loop.run_in_executor(None, _delete_asset_data_sync, ex_id, symbol)
        return {"ok": True, "id": wid, "datasets_deleted": deleted}

    @capability(
        "markets.update_now", http_method="POST", http_path="/markets/update_now",
        http_tags=["markets"], memory="on",
        description="Trigger an incremental refresh now. Input: exchange (str optional), "
                    "symbol (str optional — both blank = refresh whole watchlist). "
                    "Output: {ok, job_ids:[...]}.",
    )
    async def cap_markets_update_now(exchange: str = "", symbol: str = "", trace_id=None) -> dict:
        if not HAS_CCXT and not sys.modules.get("markets_data_capabilities"):
            return {"error": "ccxt not installed"}
        await _ensure_table()
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(None, _watchlist_rows_sync)
        if symbol:
            ex_id = (exchange or "binance").lower()
            rows = [r for r in rows if r["id"] == f"{ex_id}:{symbol}"]
        elif exchange:
            rows = [r for r in rows if r["exchange"] == exchange.lower()]
        job_ids = []
        for r in rows:
            tfs = _as_list(r.get("timeframes"), DEFAULT_TIMEFRAMES)
            job = _new_job(r["exchange"], r["symbol"], tfs, False)
            job_ids.append(job["job_id"])
            asyncio.create_task(_ingest_job(job["job_id"], r["exchange"], r["symbol"], tfs, False))
        return {"ok": True, "job_ids": job_ids, "assets": len(job_ids)}

    # ── Panel route + registration ────────────────────────────────────────────

    _HERE = _Path(__file__).parent

    @APP.get("/markets/panel", include_in_schema=False)
    async def _markets_panel_route():
        from fastapi.responses import HTMLResponse
        p = _HERE / "markets_panel.html"
        if p.exists():
            return HTMLResponse(p.read_text(encoding="utf-8"))
        return HTMLResponse("<p style='color:red'>markets_panel.html not found</p>")

    # ONE Markets panel: the Quant Studio is the whole tab (single left-rail
    # navigation — no sub-tab bar). The classic panel is no longer navigable
    # on its own; it stays served at /markets/panel solely so the studio can
    # open its node-graph pipeline builder in an overlay (?pipe=1).
    register_ui(
        "markets", "Markets", "⟁",
        """<div id="markets-mount" style="height:100%;display:flex;flex-direction:column;">
            <iframe src="/markets/studio/panel"
                    style="flex:1;border:none;width:100%;height:100%"
                    allow="clipboard-read; clipboard-write"></iframe>
        </div>""",
        "",
        ui_caps=["markets.exchanges", "markets.symbols", "markets.timeframes",
                 "markets.fetch", "markets.jobs", "markets.watchlist.list",
                 "markets.watchlist.add", "markets.watchlist.config",
                 "markets.watchlist.remove", "markets.update_now",
                 # data layer (markets_data_capabilities.py)
                 "markets.lookup", "markets.asset.add", "markets.bars", "markets.quotes",
                 "markets.custom.create", "markets.custom.add_price",
                 "markets.custom.import_csv", "markets.custom.list", "markets.custom.delete",
                 "markets.live.set", "markets.live.ticks",
                 "markets.history.audit", "markets.history.repair",
                 # analysis layer (markets_analysis_capabilities.py)
                 "markets.indicators", "markets.indicator_config.get",
                 "markets.indicator_config.set",
                 "markets.indicator.custom.save", "markets.indicator.custom.list",
                 "markets.indicator.custom.delete", "markets.indicator.custom.test",
                 "markets.annotate.add",
                 "markets.annotate.list", "markets.annotate.update", "markets.annotate.remove",
                 "markets.sentiment.analyze", "markets.sentiment.map",
                 "markets.sentiment.refresh", "markets.sentiment.history",
                 # lab layer (markets_lab_capabilities.py)
                 "markets.ml.create", "markets.ml.list", "markets.ml.update",
                 "markets.ml.train", "markets.ml.predict", "markets.ml.delete",
                 "markets.strategy.save", "markets.strategy.list", "markets.strategy.delete",
                 "markets.strategy.accept", "markets.strategy.archive",
                 "markets.monitor.status", "markets.alerts.list", "markets.alerts.ack",
                 "markets.backtest.run", "markets.backtest.list", "markets.backtest.get",
                 "markets.backtest.engines", "markets.backtest.signals",
                 "markets.backtest.sweep", "markets.backtest.sweep_status",
                 "markets.portfolio.tx_add", "markets.portfolio.tx_list",
                 "markets.portfolio.tx_remove", "markets.portfolio.positions",
                 "markets.portfolio.history",
                 # studio layer (markets_studio_capabilities.py — merged sub-tab)
                 "markets.strategy.library", "markets.strategy.from_template",
                 "markets.analysis.trendfit", "markets.analysis.pivots",
                 "markets.backtest.analyze", "markets.backtest.autotune",
                 "markets.backtest.autotune_status", "markets.backtest.batch",
                 "markets.backtest.batch_status", "markets.ml.walkforward",
                 "markets.ml.series",
                 "markets.infographic.save", "markets.infographic.list",
                 "markets.infographic.delete",
                 "markets.project.asset", "markets.project.portfolio",
                 "markets.portfolio.optimize", "markets.rotation.scan",
                 "markets.dynamics.fetch", "markets.dynamics.snapshot",
                 "markets.wsb.scan", "markets.news.feed",
                 "markets.sentiment.to_series", "markets.sim.templates",
                 "markets.strategy.versions", "markets.strategy.revert",
                 "markets.trader.status", "markets.trader.config.set",
                 "markets.trader.tick",
                 "markets.evolve.start", "markets.evolve.stop",
                 "markets.evolve.status", "markets.evolve.history",
                 "markets.evolve.config.set", "markets.evolve.tick",
                 "markets.baseline.list", "markets.baseline.ensure",
                 "markets.overview", "markets.events.detect", "markets.events.apply",
                 "markets.macro.catalog", "markets.macro.fetch",
                 "markets.sim.create", "markets.sim.list", "markets.sim.order",
                 "markets.sim.equity", "markets.sim.reset", "markets.sim.delete",
                 "markets.layout.save", "markets.layout.list", "markets.layout.delete"],
        mode="tab", tab_order=67,
    )

    # ── Auto-update scheduler ──────────────────────────────────────────────────

    async def _mkt_autoupdate_tick():
        """Every minute: refresh any watched asset whose interval has elapsed."""
        if not HAS_CCXT and not sys.modules.get("markets_data_capabilities"):
            return
        try:
            await _ensure_table()
            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(None, _watchlist_rows_sync)
            now_ms = time.time() * 1000
            for r in rows:
                if not r.get("auto_update", 1):
                    continue
                interval_ms = max(1, int(r.get("update_interval_min", 60))) * 60_000
                last_ms = _iso_to_ms(r.get("last_update")) if r.get("last_update") else None
                if last_ms is not None and (now_ms - last_ms) < interval_ms:
                    continue
                tfs = _as_list(r.get("timeframes"), DEFAULT_TIMEFRAMES)
                # Skip if any of this asset's datasets are already updating
                if any(_dataset_id(r["exchange"], r["symbol"], tf) in _MKT_INFLIGHT for tf in tfs):
                    continue
                # Optimistically stamp last_update so we don't re-trigger next tick
                await loop.run_in_executor(None, _watchlist_set_status_sync,
                                           r["id"], now_iso(), "updating")
                job = _new_job(r["exchange"], r["symbol"], tfs, False)
                asyncio.create_task(_ingest_job(job["job_id"], r["exchange"], r["symbol"], tfs, False))
        except Exception as e:
            log.warning("autoupdate tick: %s", e)

    schedule(_mkt_autoupdate_tick, 60, "mkt_autoupdate")

    log.info("markets capabilities loaded (ccxt=%s)", HAS_CCXT)
