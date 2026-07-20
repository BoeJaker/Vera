"""
commerce_pricing_capabilities.py — Live price data for inventory
================================================================

"Fetch real price data" for a product. Two sources, tried in order:

  1. **eBay Browse API** — when an eBay account is connected (an app token is
     available from ``commerce_platforms``), search active listings and take the
     low / median / high of comparable prices. Accurate + ToS-clean.
  2. **Keyless web fallback** — when no eBay app credentials exist, reuse the
     in-process ``web.search`` capability and extract price figures from the
     result snippets. Zero setup, best-effort.

Every lookup is stored as a snapshot (``commerce_price_snapshots``) so agents
can chart price history and ``commerce.price.suggest`` can recommend a
competitive sale price that still clears a target margin over cost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import statistics
import sys
import uuid
from typing import Dict, List, Optional

log = logging.getLogger("vera.commerce.pricing")

try:
    from Vera.vera.capability_orchestration import (
        capability, emit_event, now_iso, CAPABILITY_REGISTRY,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.commerce.pricing").warning("commerce pricing unavailable: %s", e)
    _CAP_AVAILABLE = False

try:
    import httpx
    HAS_HTTPX = True
except Exception:                              # pragma: no cover
    HAS_HTTPX = False

_PRICE_RE = re.compile(r"\$\s?([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)")
_SCHEMA_READY = False


async def _run(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

def _core():
    return sys.modules.get("commerce_capabilities")

def _platforms():
    return sys.modules.get("commerce_platforms")


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot store
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_schema_sync():
    global _SCHEMA_READY
    conn = _sqlite_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS commerce_price_snapshots (
                id          TEXT PRIMARY KEY,
                product_id  TEXT,
                sku         TEXT,
                source      TEXT,
                query       TEXT,
                low         REAL,
                median      REAL,
                high        REAL,
                currency    TEXT DEFAULT 'USD',
                sample_size INTEGER,
                raw         TEXT,
                ts          TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_commerce_price_pid "
                     "ON commerce_price_snapshots(product_id)")
        conn.commit()
    finally:
        conn.close()
    _SCHEMA_READY = True

async def _ensure_schema():
    if not _SCHEMA_READY:
        await _run(_ensure_schema_sync)

def _db_save_snapshot(row: dict) -> dict:
    conn = _sqlite_conn()
    try:
        sid = _new_id("px")
        conn.execute(
            "INSERT INTO commerce_price_snapshots "
            "(id,product_id,sku,source,query,low,median,high,currency,sample_size,raw,ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, row.get("product_id", ""), row.get("sku", ""), row.get("source", ""),
             row.get("query", ""), row.get("low"), row.get("median"), row.get("high"),
             row.get("currency", "USD"), row.get("sample_size", 0),
             json.dumps(row.get("raw") or [])[:20000], now_iso()))
        conn.commit()
        row["id"] = sid
        return row
    finally:
        conn.close()

def _db_snapshots(product_id: str = "", limit: int = 50) -> List[dict]:
    conn = _sqlite_conn()
    try:
        if product_id:
            rows = conn.execute(
                "SELECT id,product_id,sku,source,query,low,median,high,currency,"
                "sample_size,ts FROM commerce_price_snapshots WHERE product_id=? "
                "ORDER BY ts DESC LIMIT ?", (product_id, int(limit))).fetchall()
        else:
            rows = conn.execute(
                "SELECT id,product_id,sku,source,query,low,median,high,currency,"
                "sample_size,ts FROM commerce_price_snapshots ORDER BY ts DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ─────────────────────────────────────────────────────────────────────────────
# Price sources
# ─────────────────────────────────────────────────────────────────────────────

def _summarise(prices: List[float]) -> Optional[dict]:
    prices = [p for p in prices if p and p > 0]
    if not prices:
        return None
    prices.sort()
    return {"low": round(prices[0], 2),
            "median": round(statistics.median(prices), 2),
            "high": round(prices[-1], 2),
            "sample_size": len(prices)}

async def _ebay_browse(query: str, limit: int = 50) -> Optional[dict]:
    """Live comps from the eBay Browse API. None if no eBay account / token."""
    if not HAS_HTTPX:
        return None
    plat = _platforms()
    if not plat:
        return None
    acct = await _run(plat._db_first_account, "ebay", True)
    if not acct:
        return None
    token = await plat.ebay_app_token(acct["id"])
    if not token:
        return None
    base = plat.EBAY_ENV.get(acct.get("env") or "production", plat.EBAY_ENV["production"])["api"]
    mkt = plat._ebay_marketplace(acct) if hasattr(plat, "_ebay_marketplace") else "EBAY_US"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.get(
                f"{base}/buy/browse/v1/item_summary/search",
                headers={"Authorization": f"Bearer {token}",
                         "X-EBAY-C-MARKETPLACE-ID": mkt},
                params={"q": query, "limit": min(int(limit), 100)})
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        log.warning("ebay browse failed: %s", e)
        return None
    prices, currency = [], "USD"
    for it in data.get("itemSummaries", []):
        pr = it.get("price") or {}
        try:
            prices.append(float(pr.get("value")))
            currency = pr.get("currency", currency)
        except (TypeError, ValueError):
            continue
    summ = _summarise(prices)
    if not summ:
        return None
    summ.update({"source": "ebay", "currency": currency, "raw": prices[:50]})
    return summ

async def _web_search(query: str, limit: int = 8) -> List[dict]:
    """Call the web.search capability in-process (raw, to avoid event noise)."""
    reg = CAPABILITY_REGISTRY.get("web.search") if CAPABILITY_REGISTRY else None
    raw = reg.get("raw") if reg else None
    if not raw:
        wc = sys.modules.get("web_capabilities")
        raw = getattr(wc, "cap_web_search", None) if wc else None
    if not raw:
        return []
    try:
        res = await raw(query=query, limit=int(limit), discover="off", trace_id=None)
    except Exception as e:
        log.warning("web.search fallback failed: %s", e)
        return []
    return (res or {}).get("results", []) if isinstance(res, dict) else []

async def _web_fallback(query: str) -> Optional[dict]:
    """Best-effort comps from web-search snippets (no API keys)."""
    results = await _web_search(f"{query} price for sale", limit=10)
    prices = []
    for r in results:
        text = f"{r.get('title','')} {r.get('snippet','')}"
        for m in _PRICE_RE.findall(text):
            try:
                prices.append(float(m.replace(",", "")))
            except ValueError:
                continue
    # Drop implausible outliers (junk like "$1" or a phone number turned price).
    prices = [p for p in prices if 0.5 <= p <= 100000]
    summ = _summarise(prices)
    if not summ:
        return None
    summ.update({"source": "web", "currency": "USD", "raw": prices[:50]})
    return summ


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    @capability(
        "business.price.lookup", http_method="POST", http_path="/business/price/lookup",
        http_tags=["commerce"],
        description="Fetch real market price comps for a product and store a snapshot. "
                    "Source: eBay Browse API when an eBay account is connected, else a "
                    "keyless web-search fallback. Input: product_id (str — local product; "
                    "its name/upc seeds the query), query (str — override search terms), "
                    "upc (str). Output: {ok, source, low, median, high, sample_size, currency} "
                    "or {error}.")
    async def cap_price_lookup(product_id: str = "", query: str = "", upc: str = "",
                               trace_id=None):
        await _ensure_schema()
        core = _core()
        prod = None
        if product_id and core:
            prod = await _run(core._db_get_product, product_id)
        q = query or (prod or {}).get("name") or upc or (prod or {}).get("upc") or ""
        q = q.strip()
        if not q:
            return {"error": "need query, upc, or a product_id with a name"}
        await emit_event({"type": "commerce.progress", "stage": "price.lookup",
                          "message": f"pricing '{q}'"})
        summ = await _ebay_browse(q)
        if not summ:
            summ = await _web_fallback(q)
        if not summ:
            return {"error": f"no price data found for '{q}' "
                             "(connect an eBay account for higher-fidelity comps)"}
        snap = await _run(_db_save_snapshot, {
            "product_id": (prod or {}).get("id", "") or product_id,
            "sku": (prod or {}).get("sku", ""), "query": q, **summ})
        return {"ok": True, "source": summ["source"], "low": summ["low"],
                "median": summ["median"], "high": summ["high"],
                "sample_size": summ["sample_size"], "currency": summ["currency"],
                "snapshot_id": snap["id"]}

    @capability(
        "business.price.snapshots", http_method="GET",
        http_path="/business/price/snapshots", http_tags=["commerce"],
        memory="off", silent=True,
        description="Price-snapshot history. Input: product_id (str — omit for all), "
                    "limit (int default 50). Output: {snapshots:[...], count}.")
    async def cap_price_snapshots(product_id: str = "", limit: int = 50, trace_id=None):
        await _ensure_schema()
        snaps = await _run(_db_snapshots, product_id, int(limit))
        return {"snapshots": snaps, "count": len(snaps)}

    @capability(
        "business.price.suggest", http_method="POST", http_path="/business/price/suggest",
        http_tags=["commerce"],
        description="Suggest a competitive sale price: benchmarks against live comps "
                    "(runs a fresh lookup if needed) but never below cost·(1+margin). "
                    "Input: product_id (str! — supplies cost), margin (float default 0.30 "
                    "= 30% min markup), undercut (float default 0.0 = shave this fraction off "
                    "the market median to win the buy box). Output: {ok, suggested_price, "
                    "market_median, floor, cost, basis} or {error}.")
    async def cap_price_suggest(product_id: str = "", margin: float = 0.30,
                                undercut: float = 0.0, trace_id=None):
        await _ensure_schema()
        core = _core()
        if not core:
            return {"error": "commerce core module not loaded"}
        prod = await _run(core._db_get_product, product_id)
        if not prod:
            return {"error": "product not found"}
        # Use the most recent snapshot; else run a lookup now.
        snaps = await _run(_db_snapshots, prod["id"], 1)
        median = snaps[0]["median"] if snaps else None
        source = snaps[0]["source"] if snaps else None
        if median is None:
            look = await cap_price_lookup(product_id=prod["id"], trace_id=None)
            if look.get("error"):
                return {"error": f"no comps to base a price on: {look['error']}"}
            median = look["median"]; source = look["source"]
        cost = float(prod.get("cost") or 0)
        floor = round(cost * (1.0 + float(margin)), 2) if cost else 0.0
        target = float(median) * (1.0 - max(0.0, float(undercut)))
        suggested = round(max(target, floor), 2)
        basis = ("floor:cost+margin" if floor and suggested <= floor + 0.001
                 else f"market:{source}")
        return {"ok": True, "suggested_price": suggested, "market_median": round(float(median), 2),
                "floor": floor, "cost": round(cost, 2), "basis": basis}

    log.info("business.pricing: ready (eBay Browse + web fallback)")
