"""
markets_data_capabilities.py — Multi-asset data layer for the Markets workbench
================================================================================

Extends the CCXT crypto ingestion in `markets_capabilities.py` with:

• **Stocks / ETFs / indices / FX / futures** via Yahoo Finance's public chart
  endpoints (no API key, plain HTTPS) — provider id ``yahoo``.
• **Custom asset series** (video games, collectables, trading cards, anything
  without a feed) — provider id ``custom``; manual price points + CSV import.
• **Unified symbol lookup** across yahoo + ccxt + custom (`markets.lookup`).
• **Fast bar reads** (`markets.bars`) — one SQL query instead of paging
  /fabric/browse 200 records at a time.
• **Watchlist quotes** (`markets.quotes`) — last price / 24h change for every
  tracked asset from stored bars.
• **Provider-agnostic add** (`markets.asset.add`) — one call that upserts the
  watchlist row and starts the right backfill job for the provider.

Datasets follow the existing scheme ``mkt.{provider}.{slug}.{tf}`` and reuse
the same fabric record shape as the crypto ingester, so charts, backtests and
ML tools treat every asset class identically.

Cross-module access: the orchestrator loads capability modules under their
basename, so we reach the crypto module via ``sys.modules.get("markets_capabilities")``
(never a second package import — that would re-register its capabilities).
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
from typing import Dict, List, Optional

log = logging.getLogger("vera.markets.data")

try:
    from Vera.vera.capability_orchestration import (
        capability, emit_event, now_iso, schedule,
    )
    from Vera.vera.fabric.data_fabric import _enqueue_write, _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.markets.data").warning("markets data caps unavailable: %s", e)
    _CAP_AVAILABLE = False

try:
    import requests as _rq
    HAS_REQUESTS = True
except Exception:                              # pragma: no cover
    HAS_REQUESTS = False


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers (no orchestration deps — importable standalone for tests)
# ─────────────────────────────────────────────────────────────────────────────

TF_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000,
}

# tf -> (yahoo interval, max lookback window in days; None = full history).
# Always fetched via explicit period1/period2 — the range=max shortcut makes
# Yahoo silently degrade interval=1d to quarterly granularity.
YAHOO_TF = {
    "1m":  ("1m",  7),
    "5m":  ("5m",  59),
    "15m": ("15m", 59),
    "30m": ("30m", 59),
    "1h":  ("60m", 729),
    "1d":  ("1d",  None),
    "1w":  ("1wk", None),
}

YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
}

QUOTE_TYPE_CLASS = {
    "EQUITY": "stock", "ETF": "etf", "INDEX": "index",
    "CRYPTOCURRENCY": "crypto", "CURRENCY": "fx", "FUTURE": "future",
    "MUTUALFUND": "fund", "OPTION": "option",
}

# Sensible defaults per provider when the caller doesn't pick timeframes.
PROVIDER_DEFAULT_TFS = {
    "yahoo":  ["1d", "1h"],
    "custom": ["1d"],
}


def _slug(symbol: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (symbol or "").lower()).strip("_") or "asset"


def _dataset_id(provider: str, symbol: str, tf: str) -> str:
    return f"mkt.{(provider or 'binance').lower()}.{_slug(symbol)}.{tf}"


def _ms_to_iso(ms: int) -> str:
    return datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_to_ms(s: str) -> Optional[int]:
    if not s:
        return None
    try:
        s2 = str(s).replace("Z", "")
        return int(datetime.fromisoformat(s2).replace(tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        return None


def parse_symbol_key(key: str) -> (str, str):
    """'yahoo:AAPL' -> ('yahoo','AAPL'); bare symbols default to binance."""
    key = (key or "").strip()
    if ":" in key:
        p, s = key.split(":", 1)
        return (p.strip().lower() or "binance"), s.strip()
    return "binance", key


def parse_yahoo_chart(payload: dict) -> List[list]:
    """Yahoo v8 chart JSON -> [[ts_ms, o, h, l, c, v], ...] (nulls dropped)."""
    out: List[list] = []
    try:
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            return out
        r0 = result[0] or {}
        ts = r0.get("timestamp") or []
        quote = ((r0.get("indicators") or {}).get("quote") or [{}])[0] or {}
        opens, highs = quote.get("open") or [], quote.get("high") or []
        lows, closes = quote.get("low") or [], quote.get("close") or []
        vols = quote.get("volume") or []
        for i, t in enumerate(ts):
            try:
                c = closes[i] if i < len(closes) else None
                if t is None or c is None:
                    continue
                o = opens[i] if i < len(opens) and opens[i] is not None else c
                h = highs[i] if i < len(highs) and highs[i] is not None else max(o, c)
                l = lows[i]  if i < len(lows)  and lows[i]  is not None else min(o, c)
                v = vols[i]  if i < len(vols)  and vols[i]  is not None else 0
                out.append([int(t) * 1000, float(o), float(h), float(l), float(c), float(v)])
            except Exception:
                continue
    except Exception:
        pass
    return out


def find_gaps(ts_ms: List[int], tf: str, provider: str = "",
              max_gaps: int = 100) -> List[dict]:
    """Detect holes in a sorted bar-timestamp series. Stock (yahoo) series get
    weekend/holiday-tolerant thresholds so normal market closures don't flag."""
    tf_ms = TF_MS.get(tf, 86_400_000)
    day = 86_400_000
    if provider == "yahoo":
        thr = 15 * day if tf == "1w" else (6 * day if tf == "1d" else 4 * day)
    else:
        thr = int(3.5 * tf_ms)
    gaps = []
    for i in range(1, len(ts_ms)):
        d = ts_ms[i] - ts_ms[i - 1]
        if d > thr:
            gaps.append({
                "start": _ms_to_iso(ts_ms[i - 1] + tf_ms),
                "end": _ms_to_iso(ts_ms[i]),
                "start_ms": ts_ms[i - 1] + tf_ms, "end_ms": ts_ms[i],
                "bars_missing": max(1, int(d / tf_ms) - 1),
            })
            if len(gaps) >= max_gaps:
                break
    return gaps


def expected_bar_count(first_ms: int, last_ms: int, tf: str, provider: str = "") -> int:
    """Rough expected bar count over a span (stocks pro-rated for trading days)."""
    tf_ms = TF_MS.get(tf, 86_400_000)
    span = max(0, last_ms - first_ms)
    n = span / tf_ms + 1
    if provider == "yahoo":
        n *= 5.0 / 7.0                       # weekends
        if tf in ("1m", "5m", "15m", "30m", "1h"):
            n *= 6.5 / 24.0                  # regular session hours
    return max(1, int(n))


def parse_price_csv(text: str) -> List[list]:
    """Parse 'date,price[,note]' CSV lines -> [[ts_ms, price, note], ...]."""
    rows: List[list] = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln or ln.lower().startswith(("date", "#", "//")):
            continue
        parts = [p.strip() for p in re.split(r"[,;\t]", ln)]
        if len(parts) < 2:
            continue
        ms = _iso_to_ms(parts[0]) or _iso_to_ms(parts[0] + "T00:00:00")
        if ms is None:
            # try epoch seconds / ms
            try:
                n = float(parts[0])
                ms = int(n * 1000) if n < 1e12 else int(n)
            except Exception:
                continue
        try:
            price = float(re.sub(r"[^0-9.eE+-]", "", parts[1]))
        except Exception:
            continue
        rows.append([ms, price, parts[2] if len(parts) > 2 else ""])
    rows.sort(key=lambda r: r[0])
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Yahoo HTTP (sync, run in executor)
# ─────────────────────────────────────────────────────────────────────────────

def _yahoo_get_sync(url: str, params: dict) -> dict:
    if not HAS_REQUESTS:
        raise RuntimeError("requests not installed")
    r = _rq.get(url, params=params, headers=YAHOO_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def _yahoo_search_sync(query: str, count: int = 12) -> List[dict]:
    j = _yahoo_get_sync("https://query1.finance.yahoo.com/v1/finance/search",
                        {"q": query, "quotesCount": count, "newsCount": 0, "listsCount": 0})
    out = []
    for q in (j.get("quotes") or []):
        sym = q.get("symbol")
        if not sym:
            continue
        out.append({
            "provider": "yahoo", "symbol": sym,
            "name": q.get("longname") or q.get("shortname") or sym,
            "asset_class": QUOTE_TYPE_CLASS.get(str(q.get("quoteType", "")).upper(), "stock"),
            "exchange_disp": q.get("exchDisp") or q.get("exchange") or "",
        })
    return out


def _yahoo_chart_sync(symbol: str, tf: str, since_ms: Optional[int] = None) -> List[list]:
    interval, window_days = YAHOO_TF.get(tf, ("1d", None))
    now_s = int(time.time()) + 60
    if since_ms:
        start_s = max(0, int(since_ms // 1000))
    else:
        start_s = 0 if window_days is None else now_s - window_days * 86400
    if window_days is not None:
        # intraday endpoints reject period1 older than their window
        start_s = max(start_s, now_s - window_days * 86400)
    if start_s >= now_s:
        # incremental callers advance since by one whole timeframe (+7d on 1w),
        # which can overshoot "now" — yahoo 400s when period1 > period2
        return []
    params = {"interval": interval, "events": "div,splits",
              "period1": start_s, "period2": now_s}
    j = _yahoo_get_sync(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}", params)
    return parse_yahoo_chart(j)


def _yahoo_meta_sync(symbol: str) -> dict:
    """Instrument metadata from the chart endpoint (incl. firstTradeDate)."""
    j = _yahoo_get_sync(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        {"interval": "1d", "range": "5d"})
    meta = ((j.get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
    ftd = meta.get("firstTradeDate")
    return {
        "inception_ms": int(ftd) * 1000 if ftd else None,
        "inception": _ms_to_iso(int(ftd) * 1000) if ftd else None,
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName") or "",
        "currency": meta.get("currency") or "",
        "instrument_type": meta.get("instrumentType") or "",
        "name": meta.get("longName") or meta.get("shortName") or "",
    }


def _yahoo_chart_range_sync(symbol: str, tf: str, start_ms: int, end_ms: int) -> List[list]:
    interval, window_days = YAHOO_TF.get(tf, ("1d", None))
    now_s = int(time.time())
    start_s, end_s = int(start_ms // 1000), int(end_ms // 1000) + 60
    if window_days is not None:
        floor_s = now_s - window_days * 86400
        if end_s < floor_s:
            return []                        # range predates yahoo's intraday window
        start_s = max(start_s, floor_s)
    if start_s >= end_s:
        return []                            # yahoo 400s when period1 > period2
    j = _yahoo_get_sync(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        {"interval": interval, "period1": start_s, "period2": end_s,
         "events": "div,splits"})
    return parse_yahoo_chart(j)


# ─────────────────────────────────────────────────────────────────────────────
# Everything below needs the orchestrator
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    def _mc():
        """The crypto/base markets module (loaded before us by the orchestrator)."""
        return sys.modules.get("markets_capabilities")

    # ── SQLite: custom-assets table ───────────────────────────────────────────

    _CUSTOM_READY = False

    def _ensure_custom_table_sync():
        conn = _sqlite_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mkt_custom_assets (
                    id          TEXT PRIMARY KEY,
                    name        TEXT,
                    asset_class TEXT,
                    currency    TEXT,
                    notes       TEXT,
                    created_at  TEXT
                )
            """)
            conn.commit()
        finally:
            conn.close()

    async def _ensure_custom_table():
        global _CUSTOM_READY
        if _CUSTOM_READY:
            return
        await asyncio.get_running_loop().run_in_executor(None, _ensure_custom_table_sync)
        _CUSTOM_READY = True

    def _custom_rows_sync(query: str = "") -> List[dict]:
        conn = _sqlite_conn()
        try:
            if query:
                rows = conn.execute(
                    "SELECT * FROM mkt_custom_assets WHERE name LIKE ? OR id LIKE ? "
                    "ORDER BY created_at", (f"%{query}%", f"%{query}%")).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM mkt_custom_assets ORDER BY created_at").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _ensure_dataset_sync(ds: str, provider: str, tf: str, extra_tags: List[str]):
        conn = _sqlite_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO fabric_datasets (dataset_id, record_count, created_at, updated_at) "
                "VALUES (?,0,?,?)", (ds, now_iso(), now_iso()))
            for tag in set(["ohlcv", provider.lower(), tf] + list(extra_tags or [])):
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

    async def _write_bars(ds: str, provider: str, symbol: str, tf: str,
                          bars: List[list], floor_ms: int = -1) -> int:
        """Write [[ts_ms,o,h,l,c,v],…] rows as fabric records (deterministic ids)."""
        n = 0
        for b in bars:
            ts_ms = int(b[0])
            if ts_ms <= floor_ms:
                continue
            o, h, l, c = b[1], b[2], b[3], b[4]
            v = b[5] if len(b) > 5 else None
            await _enqueue_write({"kind": "insert_record", "rec": {
                "id": f"{ds}:{ts_ms}",
                "dataset_id": ds,
                "text": f"{symbol} {tf} {_ms_to_iso(ts_ms)} O{o} H{h} L{l} C{c} V{v}"[:300],
                "data": {
                    "timestamp": ts_ms, "ts": _ms_to_iso(ts_ms),
                    "open": o, "high": h, "low": l, "close": c, "volume": v,
                    "symbol": symbol, "exchange": provider, "timeframe": tf,
                },
                "source_id": f"{provider}:{symbol}",
                "tags": ["ohlcv", provider, tf],
                "created_at": _ms_to_iso(ts_ms),
            }}, wait=False)
            n += 1
        if n:
            await _enqueue_write({"kind": "upsert_dataset", "dataset_id": ds, "increment": 0}, wait=True)
        return n

    # ── Yahoo ingestion (called by markets_capabilities' job runner too) ──────

    async def _yahoo_ingest_timeframe(job: dict, symbol: str, tf: str, full: bool) -> int:
        """Backfill/refresh one (yahoo symbol, timeframe). Same contract as the
        crypto `_ingest_timeframe`: fills job['fetched'/'totals'/'errors']."""
        if tf not in YAHOO_TF:
            job["errors"][tf] = f"timeframe {tf} unsupported by yahoo"
            return 0
        ds = _dataset_id("yahoo", symbol, tf)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _ensure_dataset_sync, ds, "yahoo", tf, ["stocks"])
        last_ms = await loop.run_in_executor(None, _last_bar_ms_sync, ds)
        since = None if (full or last_ms is None) else last_ms + TF_MS.get(tf, 86_400_000)
        try:
            bars = await loop.run_in_executor(None, _yahoo_chart_sync, symbol, tf, since)
        except Exception as e:
            job["errors"][tf] = str(e)[:300]
            log.warning("yahoo chart %s %s: %s", symbol, tf, e)
            return 0
        n = await _write_bars(ds, "yahoo", symbol, tf, bars, floor_ms=(last_ms or -1))
        job["fetched"][tf] = n
        job["stage"] = f"{symbol} {tf}: {n} bars"
        total = await loop.run_in_executor(None, _reconcile_count_sync, ds)
        job["totals"][tf] = total
        await emit_event({"type": "markets.fetch", "job_id": job.get("job_id"),
                          "stage": "progress", "exchange": "yahoo", "symbol": symbol,
                          "timeframe": tf, "fetched": n})
        return n

    async def _custom_ingest_timeframe(job: dict, symbol: str, tf: str, full: bool) -> int:
        """Custom series have no upstream feed — nothing to refresh."""
        job["fetched"][tf] = 0
        return 0

    # Exported router used by markets_capabilities._ingest_timeframe so the
    # shared job runner + auto-update scheduler work for every provider.
    PROVIDER_INGESTORS = {
        "yahoo":  _yahoo_ingest_timeframe,
        "custom": _custom_ingest_timeframe,
    }

    # ── Unified lookup ────────────────────────────────────────────────────────

    @capability(
        "markets.lookup", http_method="GET", http_path="/markets/lookup",
        http_tags=["markets"], memory="off", silent=True,
        description="Unified asset search across stocks/ETFs/indices/FX (yahoo), "
                    "crypto (ccxt exchange) and custom tracked assets (collectables, "
                    "video games, trading cards). Input: query (str!), "
                    "exchange (str — ccxt exchange for crypto results, default binance), "
                    "limit (int=12). Output: {results:[{provider,symbol,name,asset_class,"
                    "exchange_disp,tracked,key}]}.",
    )
    async def cap_markets_lookup(query: str = "", exchange: str = "binance",
                                 limit: int = 12, trace_id=None) -> dict:
        q = (query or "").strip()
        if not q:
            return {"error": "query required"}
        limit = max(1, min(40, int(limit)))
        loop = asyncio.get_running_loop()
        results: List[dict] = []

        # tracked set for flagging
        tracked = set()
        mc = _mc()
        try:
            if mc:
                rows = await loop.run_in_executor(None, mc._watchlist_rows_sync)
                tracked = {f"{r['exchange']}:{r['symbol']}" for r in rows}
        except Exception:
            pass

        async def _yahoo_part():
            try:
                return await loop.run_in_executor(None, _yahoo_search_sync, q, limit)
            except Exception as e:
                log.debug("yahoo search: %s", e)
                return []

        async def _ccxt_part():
            try:
                if not (mc and getattr(mc, "HAS_CCXT", False)):
                    return []
                mkts = await mc._markets(exchange)
                ql = q.lower()
                items = []
                for sym, m in mkts.items():
                    if not isinstance(m, dict):
                        continue
                    base = str(m.get("base", "")).lower()
                    if ql in sym.lower() or ql in base:
                        items.append({
                            "provider": exchange.lower(), "symbol": sym,
                            "name": f"{m.get('base','')}/{m.get('quote','')}",
                            "asset_class": "crypto",
                            "exchange_disp": exchange.capitalize(),
                        })
                items.sort(key=lambda it: (0 if it["symbol"].lower().startswith(ql) else 1,
                                           len(it["symbol"])))
                return items[:limit]
            except Exception as e:
                log.debug("ccxt lookup: %s", e)
                return []

        async def _custom_part():
            try:
                await _ensure_custom_table()
                rows = await loop.run_in_executor(None, _custom_rows_sync, q)
                return [{
                    "provider": "custom", "symbol": r["name"],
                    "name": r["name"], "asset_class": r.get("asset_class") or "collectable",
                    "exchange_disp": "Custom",
                } for r in rows[:limit]]
            except Exception:
                return []

        parts = await asyncio.gather(_yahoo_part(), _ccxt_part(), _custom_part())
        for grp in (parts[2], parts[1], parts[0]):     # custom, crypto, yahoo order
            for it in grp:
                it["key"] = f"{it['provider']}:{it['symbol']}"
                it["tracked"] = it["key"] in tracked
                results.append(it)
        return {"query": q, "count": len(results), "results": results[: limit * 3]}

    # ── Provider-agnostic add / fetch ─────────────────────────────────────────

    @capability(
        "markets.asset.add", http_method="POST", http_path="/markets/asset/add",
        http_tags=["markets"], memory="on",
        description="Track any asset (crypto via ccxt exchange, stock/ETF/index/FX via "
                    "provider 'yahoo', collectables via provider 'custom') — upserts the "
                    "watchlist row and starts the right backfill job. "
                    "Input: provider (str — 'binance'|'coinbase'|…|'yahoo'|'custom'), "
                    "symbol (str! — 'BTC/USDT', 'AAPL', or custom asset name), "
                    "timeframes (list/csv — default per provider), auto_update (bool=True), "
                    "update_interval_min (int=60), backfill (bool=True). "
                    "Output: {ok, key, job_id}.",
    )
    async def cap_markets_asset_add(provider: str = "binance", symbol: str = "",
                                    timeframes=None, auto_update: bool = True,
                                    update_interval_min: int = 60,
                                    backfill: bool = True, trace_id=None) -> dict:
        mc = _mc()
        if not mc:
            return {"error": "markets base module unavailable"}
        if not symbol:
            return {"error": "symbol required"}
        prov = (provider or "binance").lower()
        tfs = mc._as_list(timeframes, PROVIDER_DEFAULT_TFS.get(prov, mc.DEFAULT_TIMEFRAMES))
        if prov == "yahoo":
            tfs = [tf for tf in tfs if tf in YAHOO_TF] or ["1d"]
        if prov == "custom":
            tfs, auto_update, backfill = ["1d"], False, False

        await mc._ensure_table()
        wid = f"{prov}:{symbol}"
        row = {"id": wid, "exchange": prov, "symbol": symbol, "timeframes": tfs,
               "auto_update": 1 if auto_update else 0,
               "update_interval_min": max(1, int(update_interval_min)),
               "last_update": None, "last_status": "queued" if backfill else "added",
               "created_at": now_iso()}
        await asyncio.get_running_loop().run_in_executor(None, mc._watchlist_upsert_sync, row)

        job_id = None
        can_fetch = (prov == "yahoo" and HAS_REQUESTS) or \
                    (prov not in ("yahoo", "custom") and getattr(mc, "HAS_CCXT", False))
        if backfill and can_fetch:
            job = mc._new_job(prov, symbol, tfs, True)
            job_id = job["job_id"]
            asyncio.create_task(mc._ingest_job(job_id, prov, symbol, tfs, True))
        await emit_event({"type": "markets.watchlist", "stage": "added",
                          "exchange": prov, "symbol": symbol})
        return {"ok": True, "key": wid, "id": wid, "job_id": job_id, "timeframes": tfs}

    # ── Fast bar reads ────────────────────────────────────────────────────────

    def _bars_sync(ds: str, limit: int, start_ms: Optional[int], end_ms: Optional[int]) -> List[dict]:
        conn = _sqlite_conn()
        try:
            sql = "SELECT data, created_at FROM fabric_records WHERE dataset_id=?"
            args: list = [ds]
            if start_ms is not None:
                sql += " AND created_at>=?"; args.append(_ms_to_iso(start_ms))
            if end_ms is not None:
                sql += " AND created_at<=?"; args.append(_ms_to_iso(end_ms))
            sql += " ORDER BY created_at DESC LIMIT ?"; args.append(int(limit))
            rows = conn.execute(sql, args).fetchall()
            out = []
            for r in rows:
                try:
                    d = json.loads(r[0]) if isinstance(r[0], str) else (r[0] or {})
                    if isinstance(d, dict) and d.get("close") is not None:
                        out.append(d)
                except Exception:
                    continue
            out.reverse()
            return out
        finally:
            conn.close()

    async def get_bars(dataset_id: str, limit: int = 5000,
                       start_ms: Optional[int] = None, end_ms: Optional[int] = None) -> List[dict]:
        """Shared server-side bar loader (analysis/lab modules import this)."""
        limit = max(1, min(200_000, int(limit)))
        return await asyncio.get_running_loop().run_in_executor(
            None, _bars_sync, dataset_id, limit, start_ms, end_ms)

    @capability(
        "markets.bars", http_method="GET", http_path="/markets/bars",
        http_tags=["markets"], memory="off", silent=True,
        description="Read stored OHLCV bars for a dataset in one call (columnar arrays, "
                    "oldest→newest). Input: dataset_id (str! e.g. 'mkt.binance.btc_usdt.1d'), "
                    "limit (int=5000, max 50000), start (ISO str), end (ISO str). "
                    "Output: {dataset_id, count, t:[unix_sec], o, h, l, c, v, symbol, timeframe}.",
    )
    async def cap_markets_bars(dataset_id: str = "", limit: int = 5000,
                               start: str = "", end: str = "", trace_id=None) -> dict:
        if not dataset_id:
            return {"error": "dataset_id required"}
        limit = max(1, min(50_000, int(limit)))
        bars = await get_bars(dataset_id, limit, _iso_to_ms(start), _iso_to_ms(end))
        t, o, h, l, c, v = [], [], [], [], [], []
        for d in bars:
            ts = d.get("timestamp")
            if ts is None:
                ts = _iso_to_ms(d.get("ts") or "")
            if ts is None:
                continue
            t.append(int(ts // 1000) if ts > 1e12 else int(ts))
            o.append(d.get("open")); h.append(d.get("high"))
            l.append(d.get("low"));  c.append(d.get("close"))
            v.append(d.get("volume") or 0)
        meta = bars[-1] if bars else {}
        return {"dataset_id": dataset_id, "count": len(t),
                "t": t, "o": o, "h": h, "l": l, "c": c, "v": v,
                "symbol": meta.get("symbol", ""), "exchange": meta.get("exchange", ""),
                "timeframe": meta.get("timeframe", "")}

    # ── Quotes for the whole watchlist ────────────────────────────────────────

    def _quote_from_bars_sync(provider: str, symbol: str, tfs: List[str]) -> dict:
        """Last price + 24h change from stored bars (no network)."""
        conn = _sqlite_conn()
        try:
            # last price: freshest bar across the asset's timeframes
            last_px, last_ts = None, None
            for tf in sorted(tfs, key=lambda x: TF_MS.get(x, 1 << 60)):
                ds = _dataset_id(provider, symbol, tf)
                r = conn.execute(
                    "SELECT data FROM fabric_records WHERE dataset_id=? "
                    "ORDER BY created_at DESC LIMIT 1", (ds,)).fetchone()
                if not r:
                    continue
                try:
                    d = json.loads(r[0]) if isinstance(r[0], str) else (r[0] or {})
                    ts = d.get("timestamp") or 0
                    if last_ts is None or ts > last_ts:
                        last_ts, last_px = ts, d.get("close")
                except Exception:
                    continue
            # daily change: last two 1d closes (fall back to first tf)
            ref_tf = "1d" if "1d" in tfs else (tfs[0] if tfs else "1d")
            ds = _dataset_id(provider, symbol, ref_tf)
            rows = conn.execute(
                "SELECT data FROM fabric_records WHERE dataset_id=? "
                "ORDER BY created_at DESC LIMIT 2", (ds,)).fetchall()
            closes = []
            for r in rows:
                try:
                    d = json.loads(r[0]) if isinstance(r[0], str) else (r[0] or {})
                    if d.get("close") is not None:
                        closes.append(float(d["close"]))
                except Exception:
                    continue
            chg = None
            if len(closes) == 2 and closes[1]:
                base = closes[1]
                px = last_px if last_px is not None else closes[0]
                chg = (float(px) - base) / base * 100.0
            return {"last": last_px, "ts": last_ts, "change_pct": chg}
        finally:
            conn.close()

    async def latest_price(provider: str, symbol: str, tfs: List[str] = None) -> Optional[float]:
        """Exported for the portfolio valuer."""
        q = await asyncio.get_running_loop().run_in_executor(
            None, _quote_from_bars_sync, provider, symbol, tfs or ["1d", "1h"])
        return q.get("last")

    @capability(
        "markets.quotes", http_method="GET", http_path="/markets/quotes",
        http_tags=["markets"], memory="off", silent=True,
        description="Last price + day change for every watchlist asset (from stored bars; "
                    "no network). Input: none. Output: {quotes:[{key,exchange,symbol,last,"
                    "change_pct,ts}]}.",
    )
    async def cap_markets_quotes(trace_id=None) -> dict:
        mc = _mc()
        if not mc:
            return {"error": "markets base module unavailable"}
        await mc._ensure_table()
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(None, mc._watchlist_rows_sync)
        out = []
        for r in rows:
            tfs = mc._as_list(r.get("timeframes"), ["1d"])
            q = await loop.run_in_executor(
                None, _quote_from_bars_sync, r["exchange"], r["symbol"], tfs)
            last, ts = q.get("last"), q.get("ts")
            if r.get("live_track"):
                try:
                    tick = await loop.run_in_executor(None, _latest_tick_sync, r["id"])
                    if tick and (ts is None or tick[0] > ts):
                        ts, last = tick[0], tick[1]
                except Exception:
                    pass
            out.append({"key": r["id"], "exchange": r["exchange"], "symbol": r["symbol"],
                        "last": last, "change_pct": q.get("change_pct"),
                        "ts": ts, "live": bool(r.get("live_track"))})
        return {"quotes": out, "count": len(out)}

    # ── Custom asset series (collectables / video games / trading cards) ─────

    @capability(
        "markets.custom.create", http_method="POST", http_path="/markets/custom/create",
        http_tags=["markets"], memory="on",
        description="Create a custom tracked asset with a manual price series — for "
                    "collectables, video games, trading cards, watches, anything without "
                    "a market feed. Input: name (str!), asset_class (str — collectable|"
                    "video_game|trading_card|other, default collectable), currency (str=USD), "
                    "notes (str), initial_price (float — optional first price point). "
                    "Output: {ok, key, dataset_id}.",
    )
    async def cap_markets_custom_create(name: str = "", asset_class: str = "collectable",
                                        currency: str = "USD", notes: str = "",
                                        initial_price: float = 0.0, trace_id=None) -> dict:
        if not name.strip():
            return {"error": "name required"}
        name = name.strip()
        await _ensure_custom_table()
        loop = asyncio.get_running_loop()
        aid = _slug(name)

        def _ins():
            conn = _sqlite_conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO mkt_custom_assets "
                    "(id, name, asset_class, currency, notes, created_at) VALUES (?,?,?,?,?,?)",
                    (aid, name, asset_class or "collectable", currency or "USD",
                     notes or "", now_iso()))
                conn.commit()
            finally:
                conn.close()
        await loop.run_in_executor(None, _ins)

        ds = _dataset_id("custom", name, "1d")
        await loop.run_in_executor(None, _ensure_dataset_sync, ds, "custom", "1d",
                                   [asset_class or "collectable"])
        # watchlist row so it shows up everywhere (no auto-update — manual series)
        r = await cap_markets_asset_add(provider="custom", symbol=name, trace_id=trace_id)
        if r.get("error"):
            return r
        if initial_price and float(initial_price) > 0:
            await cap_markets_custom_add_price(asset=name, price=float(initial_price),
                                               trace_id=trace_id)
        return {"ok": True, "key": f"custom:{name}", "id": aid, "dataset_id": ds}

    @capability(
        "markets.custom.add_price", http_method="POST", http_path="/markets/custom/add_price",
        http_tags=["markets"], memory="on",
        description="Record a price point for a custom asset (sale seen, appraisal, listing). "
                    "Input: asset (str! — name used at create), price (float!), "
                    "ts (ISO str — default now), note (str). Output: {ok, dataset_id, ts}.",
    )
    async def cap_markets_custom_add_price(asset: str = "", price: float = 0.0,
                                           ts: str = "", note: str = "", trace_id=None) -> dict:
        if not asset.strip():
            return {"error": "asset required"}
        try:
            px = float(price)
        except Exception:
            return {"error": "price must be a number"}
        if px <= 0:
            return {"error": "price must be > 0"}
        ms = _iso_to_ms(ts) if ts else int(time.time() * 1000)
        if ms is None:
            return {"error": f"could not parse ts '{ts}'"}
        ds = _dataset_id("custom", asset.strip(), "1d")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _ensure_dataset_sync, ds, "custom", "1d", ["collectable"])
        last_ms = await loop.run_in_executor(None, _last_bar_ms_sync, ds)
        n = await _write_bars(ds, "custom", asset.strip(), "1d",
                              [[ms, px, px, px, px, 0]], floor_ms=-1)
        await emit_event({"type": "markets.fetch", "stage": "progress",
                          "exchange": "custom", "symbol": asset, "timeframe": "1d",
                          "fetched": n})
        return {"ok": True, "dataset_id": ds, "ts": _ms_to_iso(ms), "price": px,
                "was_backfill": bool(last_ms and ms <= last_ms)}

    @capability(
        "markets.custom.import_csv", http_method="POST", http_path="/markets/custom/import_csv",
        http_tags=["markets"], memory="on",
        description="Bulk-import a price history for a custom asset from CSV text "
                    "('date,price' per line — ISO dates or epoch). "
                    "Input: asset (str!), csv (str!). Output: {ok, imported, dataset_id}.",
    )
    async def cap_markets_custom_import_csv(asset: str = "", csv: str = "", trace_id=None) -> dict:
        if not asset.strip():
            return {"error": "asset required"}
        rows = parse_price_csv(csv)
        if not rows:
            return {"error": "no parsable 'date,price' rows in csv"}
        ds = _dataset_id("custom", asset.strip(), "1d")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _ensure_dataset_sync, ds, "custom", "1d", ["collectable"])
        bars = [[r[0], r[1], r[1], r[1], r[1], 0] for r in rows]
        n = await _write_bars(ds, "custom", asset.strip(), "1d", bars, floor_ms=-1)
        total = await loop.run_in_executor(None, _reconcile_count_sync, ds)
        await emit_event({"type": "markets.fetch", "stage": "done",
                          "exchange": "custom", "symbol": asset, "fetched": {"1d": n}})
        return {"ok": True, "imported": n, "total": total, "dataset_id": ds}

    @capability(
        "markets.custom.list", http_method="GET", http_path="/markets/custom/list",
        http_tags=["markets"], memory="off", silent=True,
        description="List custom tracked assets (collectables etc.) with latest price. "
                    "Output: {assets:[{id,name,asset_class,currency,notes,last,points}]}.",
    )
    async def cap_markets_custom_list(trace_id=None) -> dict:
        await _ensure_custom_table()
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(None, _custom_rows_sync, "")
        out = []
        for r in rows:
            q = await loop.run_in_executor(
                None, _quote_from_bars_sync, "custom", r["name"], ["1d"])
            ds = _dataset_id("custom", r["name"], "1d")
            def _cnt(ds=ds):
                conn = _sqlite_conn()
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM fabric_records WHERE dataset_id=?", (ds,)).fetchone()
                    return int(row[0]) if row else 0
                finally:
                    conn.close()
            cnt = await loop.run_in_executor(None, _cnt)
            out.append({**{k: r.get(k) for k in ("id", "name", "asset_class", "currency", "notes")},
                        "key": f"custom:{r['name']}", "last": q.get("last"),
                        "change_pct": q.get("change_pct"), "points": cnt})
        return {"assets": out, "count": len(out)}

    @capability(
        "markets.custom.delete", http_method="POST", http_path="/markets/custom/delete",
        http_tags=["markets"], memory="on",
        description="Delete a custom asset: its registry row, watchlist entry and stored "
                    "price series. Input: asset (str!). Output: {ok, datasets_deleted}.",
    )
    async def cap_markets_custom_delete(asset: str = "", trace_id=None) -> dict:
        if not asset.strip():
            return {"error": "asset required"}
        asset = asset.strip()
        mc = _mc()
        await _ensure_custom_table()
        loop = asyncio.get_running_loop()

        def _del():
            conn = _sqlite_conn()
            try:
                conn.execute("DELETE FROM mkt_custom_assets WHERE id=? OR name=?",
                             (_slug(asset), asset))
                conn.commit()
            finally:
                conn.close()
        await loop.run_in_executor(None, _del)
        deleted = 0
        if mc:
            await loop.run_in_executor(None, mc._watchlist_delete_sync, f"custom:{asset}")
            deleted = await loop.run_in_executor(None, mc._delete_asset_data_sync, "custom", asset)
        return {"ok": True, "datasets_deleted": deleted}

    # ── Live tick recording (full retention) ─────────────────────────────────

    _LIVE_READY = False
    _LIVE_INTERVAL_S = 20

    def _ensure_live_sync():
        conn = _sqlite_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mkt_live_ticks (
                    key   TEXT NOT NULL,
                    ts    INTEGER NOT NULL,
                    price REAL,
                    PRIMARY KEY (key, ts)
                )""")
            try:
                conn.execute("ALTER TABLE mkt_watchlist ADD COLUMN live_track INTEGER DEFAULT 0")
            except Exception:
                pass                          # column already exists
            conn.commit()
        finally:
            conn.close()

    async def _ensure_live():
        global _LIVE_READY
        if _LIVE_READY:
            return
        await asyncio.get_running_loop().run_in_executor(None, _ensure_live_sync)
        _LIVE_READY = True

    def _ccxt_last_sync(ex_id: str, symbol: str) -> Optional[float]:
        mc = _mc()
        if not (mc and getattr(mc, "HAS_CCXT", False)):
            return None
        ex = mc._get_exchange(ex_id)
        t = ex.fetch_ticker(symbol)
        return t.get("last") or t.get("close")

    def _yahoo_last_sync(symbol: str) -> Optional[float]:
        bars = _yahoo_chart_sync(symbol, "1m",
                                 since_ms=int((time.time() - 1800) * 1000))
        return bars[-1][4] if bars else None

    def _tick_insert_sync(rows: List[tuple]):
        conn = _sqlite_conn()
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO mkt_live_ticks (key, ts, price) VALUES (?,?,?)", rows)
            conn.commit()
        finally:
            conn.close()

    def _latest_tick_sync(key: str) -> Optional[tuple]:
        conn = _sqlite_conn()
        try:
            r = conn.execute("SELECT ts, price FROM mkt_live_ticks WHERE key=? "
                             "ORDER BY ts DESC LIMIT 1", (key,)).fetchone()
            return (r[0], r[1]) if r else None
        finally:
            conn.close()

    async def _live_tick():
        """Record a price point for every live-tracked asset. History is kept
        forever (mkt_live_ticks) — this is the 'record prices live' store."""
        mc = _mc()
        if not mc:
            return
        try:
            await _ensure_live()
            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(None, mc._watchlist_rows_sync)
            live = [r for r in rows if r.get("live_track")]
            if not live:
                return
            now_ms = int(time.time() * 1000)
            out, inserts = [], []
            for r in live:
                prov, sym = r["exchange"], r["symbol"]
                px = None
                try:
                    if prov == "yahoo":
                        px = await loop.run_in_executor(None, _yahoo_last_sync, sym)
                    elif prov != "custom":
                        px = await loop.run_in_executor(None, _ccxt_last_sync, prov, sym)
                except Exception as e:
                    log.debug("live tick %s: %s", r["id"], e)
                if px is not None:
                    inserts.append((r["id"], now_ms, float(px)))
                    out.append({"key": r["id"], "price": float(px), "ts": now_ms})
            if inserts:
                await loop.run_in_executor(None, _tick_insert_sync, inserts)
                await emit_event({"type": "markets.tick", "ticks": out})
        except Exception as e:
            log.debug("live tick cycle: %s", e)

    schedule(_live_tick, _LIVE_INTERVAL_S, "mkt_live_tick")

    @capability(
        "markets.live.set", http_method="POST", http_path="/markets/live/set",
        http_tags=["markets"], memory="on",
        description="Enable/disable live price recording for a tracked asset — a price "
                    "point is stored every ~20s and the FULL tick history is retained. "
                    "Input: symbol_key (str! — 'provider:symbol'), enabled (bool=True). "
                    "Output: {ok, key, enabled}.",
    )
    async def cap_live_set(symbol_key: str = "", enabled: bool = True, trace_id=None) -> dict:
        mc = _mc()
        if not symbol_key or not mc:
            return {"error": "symbol_key required"}
        await mc._ensure_table()
        await _ensure_live()

        def _upd():
            conn = _sqlite_conn()
            try:
                cur = conn.execute("UPDATE mkt_watchlist SET live_track=? WHERE id=?",
                                   (1 if enabled else 0, symbol_key))
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()
        n = await asyncio.get_running_loop().run_in_executor(None, _upd)
        if not n:
            return {"error": f"'{symbol_key}' is not in the watchlist — track it first"}
        return {"ok": True, "key": symbol_key, "enabled": bool(enabled),
                "interval_s": _LIVE_INTERVAL_S}

    @capability(
        "markets.live.ticks", http_method="GET", http_path="/markets/live/ticks",
        http_tags=["markets"], memory="off", silent=True,
        description="Read recorded live ticks for an asset (oldest→newest). "
                    "Input: symbol_key (str!), limit (int=1000), start (ISO str). "
                    "Output: {key, t:[unix_sec], price:[...], count}.",
    )
    async def cap_live_ticks(symbol_key: str = "", limit: int = 1000,
                             start: str = "", trace_id=None) -> dict:
        if not symbol_key:
            return {"error": "symbol_key required"}
        await _ensure_live()
        start_ms = _iso_to_ms(start)

        def _rows():
            conn = _sqlite_conn()
            try:
                if start_ms:
                    rows = conn.execute(
                        "SELECT ts, price FROM mkt_live_ticks WHERE key=? AND ts>=? "
                        "ORDER BY ts DESC LIMIT ?",
                        (symbol_key, start_ms, max(1, min(50_000, int(limit))))).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT ts, price FROM mkt_live_ticks WHERE key=? "
                        "ORDER BY ts DESC LIMIT ?",
                        (symbol_key, max(1, min(50_000, int(limit))))).fetchall()
                return [(r[0], r[1]) for r in rows]
            finally:
                conn.close()
        rows = await asyncio.get_running_loop().run_in_executor(None, _rows)
        rows.reverse()
        return {"key": symbol_key, "count": len(rows),
                "t": [int(r[0] // 1000) for r in rows], "price": [r[1] for r in rows]}

    # ── History audit & repair ────────────────────────────────────────────────

    def _dataset_span_sync(ds: str):
        conn = _sqlite_conn()
        try:
            r = conn.execute(
                "SELECT MIN(created_at), MAX(created_at), COUNT(*) FROM fabric_records "
                "WHERE dataset_id=?", (ds,)).fetchone()
            return (_iso_to_ms(r[0]), _iso_to_ms(r[1]), int(r[2] or 0)) if r else (None, None, 0)
        finally:
            conn.close()

    def _dataset_ts_sync(ds: str, cap: int = 250_000) -> List[int]:
        conn = _sqlite_conn()
        try:
            rows = conn.execute(
                "SELECT created_at FROM fabric_records WHERE dataset_id=? "
                "ORDER BY created_at LIMIT ?", (ds, cap)).fetchall()
            out = []
            for r in rows:
                ms = _iso_to_ms(r[0])
                if ms is not None:
                    out.append(ms)
            return out
        finally:
            conn.close()

    async def _asset_meta(prov: str, symbol: str) -> dict:
        """Inception date + instrument info per provider."""
        loop = asyncio.get_running_loop()
        if prov == "yahoo":
            try:
                return await loop.run_in_executor(None, _yahoo_meta_sync, symbol)
            except Exception as e:
                return {"error": f"meta fetch failed: {e}"}
        if prov == "custom":
            first, last, n = await loop.run_in_executor(
                None, _dataset_span_sync, _dataset_id("custom", symbol, "1d"))
            return {"inception_ms": first, "inception": _ms_to_iso(first) if first else None,
                    "exchange": "custom", "instrument_type": "custom", "name": symbol}
        mc = _mc()
        if not (mc and getattr(mc, "HAS_CCXT", False)):
            return {"error": "ccxt unavailable"}

        def _probe():
            ex = mc._get_exchange(prov)
            page = ex.fetch_ohlcv(symbol, timeframe="1d",
                                  since=1230768000000, limit=10)  # 2009-01-01
            return page[0][0] if page else None
        try:
            first_ms = await loop.run_in_executor(None, _probe)
        except Exception as e:
            return {"error": f"inception probe failed: {e}"}
        return {"inception_ms": first_ms,
                "inception": _ms_to_iso(first_ms) if first_ms else None,
                "exchange": prov, "instrument_type": "crypto", "name": symbol}

    @capability(
        "markets.history.audit", http_method="GET", http_path="/markets/history/audit",
        http_tags=["markets"], memory="off", silent=True,
        description="Audit an asset's stored history: instrument metadata (incl. the date "
                    "it started trading), per-timeframe stored range, bar counts vs "
                    "expected, completeness %, missing-data gaps and how much pre-history "
                    "is absent. Input: symbol_key (str! — 'provider:symbol'). "
                    "Output: {key, meta:{inception,exchange,...}, datasets:[{tf,first,last,"
                    "bars,expected,completeness_pct,gaps,gap_count,inception_gap_days}]}.",
    )
    async def cap_history_audit(symbol_key: str = "", trace_id=None) -> dict:
        mc = _mc()
        if not symbol_key or not mc:
            return {"error": "symbol_key required"}
        prov, sym = parse_symbol_key(symbol_key)
        loop = asyncio.get_running_loop()
        meta = await _asset_meta(prov, sym)
        row = await loop.run_in_executor(None, mc._watchlist_get_sync, symbol_key)
        tfs = mc._as_list(row.get("timeframes"), ["1d"]) if row else ["1d"]
        out = []
        for tf in tfs:
            ds = _dataset_id(prov, sym, tf)
            first, last, n = await loop.run_in_executor(None, _dataset_span_sync, ds)
            if not n:
                out.append({"tf": tf, "dataset_id": ds, "bars": 0})
                continue
            ts = await loop.run_in_executor(None, _dataset_ts_sync, ds)
            gaps = find_gaps(ts, tf, prov)
            exp = expected_bar_count(first, last, tf, prov)
            inception_gap_days = None
            inc = meta.get("inception_ms")
            if inc and first and first - inc > TF_MS.get(tf, 86_400_000):
                inception_gap_days = round((first - inc) / 86_400_000, 1)
            out.append({
                "tf": tf, "dataset_id": ds,
                "first": _ms_to_iso(first), "last": _ms_to_iso(last),
                "bars": n, "expected": exp,
                "completeness_pct": round(min(100.0, n / exp * 100.0), 2),
                "gap_count": len(gaps), "gaps": gaps[:20],
                "missing_bars_est": sum(g["bars_missing"] for g in gaps),
                "inception_gap_days": inception_gap_days,
            })
        return {"key": symbol_key, "provider": prov, "symbol": sym,
                "meta": meta, "datasets": out}

    def _ccxt_fetch_range_sync_page(prov, symbol, tf, since, limit=1000):
        mc = _mc()
        ex = mc._get_exchange(prov)
        return ex.fetch_ohlcv(symbol, timeframe=tf, since=since, limit=limit)

    async def _fetch_range(prov: str, symbol: str, tf: str,
                           start_ms: int, end_ms: int, job: dict) -> int:
        """Fill one [start,end] range from the provider into the dataset."""
        ds = _dataset_id(prov, symbol, tf)
        loop = asyncio.get_running_loop()
        tf_ms = TF_MS.get(tf, 86_400_000)
        wrote = 0
        if prov == "yahoo":
            try:
                bars = await loop.run_in_executor(
                    None, _yahoo_chart_range_sync, symbol, tf, start_ms, end_ms)
            except Exception as e:
                job["errors"][tf] = str(e)[:200]
                return 0
            bars = [b for b in bars if start_ms <= b[0] <= end_ms]
            return await _write_bars(ds, prov, symbol, tf, bars, floor_ms=-1)
        # ccxt pagination
        since = start_ms
        guard = 0
        while since < end_ms and guard < 500:
            guard += 1
            if job.get("stop"):
                break
            try:
                page = await loop.run_in_executor(
                    None, _ccxt_fetch_range_sync_page, prov, symbol, tf, since)
            except Exception as e:
                job["errors"][tf] = str(e)[:200]
                break
            if not page:
                since += 1000 * tf_ms
                continue
            bars = [b for b in page if b and b[0] is not None and b[0] <= end_ms]
            wrote += await _write_bars(ds, prov, symbol, tf, bars, floor_ms=-1)
            last_ts = page[-1][0]
            nxt = last_ts + tf_ms
            if nxt <= since:
                break
            since = nxt
        return wrote

    @capability(
        "markets.history.repair", http_method="POST", http_path="/markets/history/repair",
        http_tags=["markets"], memory="on",
        description="Repair an asset's stored history in the background: backfill from "
                    "the instrument's inception date (true full history) and re-fetch "
                    "every detected gap. Runs the audit first; progress streams as "
                    "markets.fetch events. Input: symbol_key (str!), timeframes "
                    "(list/csv — default all tracked). Output: {ok, job_id, ranges}.",
    )
    async def cap_history_repair(symbol_key: str = "", timeframes=None, trace_id=None) -> dict:
        mc = _mc()
        if not symbol_key or not mc:
            return {"error": "symbol_key required"}
        prov, sym = parse_symbol_key(symbol_key)
        if prov == "custom":
            return {"error": "custom series have no upstream feed to repair from"}
        audit = await cap_history_audit(symbol_key=symbol_key, trace_id=trace_id)
        if audit.get("error"):
            return audit
        want = mc._as_list(timeframes, [d["tf"] for d in audit["datasets"]])
        plan = []  # (tf, start_ms, end_ms, label)
        inc = (audit.get("meta") or {}).get("inception_ms")
        for d in audit["datasets"]:
            if d["tf"] not in want:
                continue
            tf_ms = TF_MS.get(d["tf"], 86_400_000)
            first = _iso_to_ms(d.get("first") or "")
            if not d.get("bars"):
                start = inc or _iso_to_ms(mc.FULL_HISTORY_START)
                plan.append((d["tf"], start, int(time.time() * 1000), "full"))
                continue
            if inc and first and first - inc > tf_ms * 2:
                plan.append((d["tf"], inc, first, "pre-history"))
            for g in d.get("gaps") or []:
                plan.append((d["tf"], g["start_ms"], g["end_ms"], "gap"))
        if not plan:
            return {"ok": True, "job_id": None, "ranges": 0,
                    "note": "history is already complete"}
        job = mc._new_job(prov, sym, sorted({p[0] for p in plan}), False)
        job["stage"] = f"repair: {len(plan)} ranges"

        async def _run():
            job["status"] = "running"
            try:
                for tf, s_ms, e_ms, label in plan:
                    if job.get("stop"):
                        break
                    n = await _fetch_range(prov, sym, tf, s_ms, e_ms, job)
                    job["fetched"][tf] = job["fetched"].get(tf, 0) + n
                    job["stage"] = f"{label} {tf}: +{n}"
                    await emit_event({"type": "markets.fetch", "job_id": job["job_id"],
                                      "stage": "progress", "exchange": prov, "symbol": sym,
                                      "timeframe": tf, "fetched": job["fetched"].get(tf, 0)})
                for tf in {p[0] for p in plan}:
                    await asyncio.get_running_loop().run_in_executor(
                        None, _reconcile_count_sync, _dataset_id(prov, sym, tf))
                job["status"] = "error" if job["errors"] else "done"
            except Exception as e:
                job["status"] = "error"
                job["errors"]["_"] = str(e)[:300]
            finally:
                job["finished"] = now_iso()
                await emit_event({"type": "markets.fetch", "job_id": job["job_id"],
                                  "stage": "done", "exchange": prov, "symbol": sym,
                                  "status": job["status"], "fetched": job["fetched"]})
        asyncio.create_task(_run())
        return {"ok": True, "job_id": job["job_id"], "ranges": len(plan),
                "plan": [{"tf": p[0], "from": _ms_to_iso(p[1]),
                          "to": _ms_to_iso(p[2]), "why": p[3]} for p in plan[:30]]}

    log.info("markets data capabilities loaded (requests=%s)", HAS_REQUESTS)
