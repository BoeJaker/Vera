"""
commerce_market.py — Market OSINT, watchlists & buy-opportunity alerts
======================================================================

Watches the eBay + Vinted markets so the operator both **prices right** and
**buys cheap**.

  • ``commerce.market.search`` — one query across eBay (Browse API) and Vinted
    (public catalogue), merged, with per-platform + combined price stats.
  • ``commerce.watch.*`` — saved searches ("PS2 slim boxed", "Zelda amiibo",
    "Funko Pop grail") each with a target buy price / condition.
  • ``commerce.watch.scan`` — runs the watches, flags every live listing at or
    below the target (or well below the market median) as a **buy opportunity**
    and stores an alert; also scans owned, listed inventory and flags anything
    mispriced vs the current market for **repricing**.
  • ``commerce.market.reprice`` — for one product, compares your price to the
    live market and suggests (or applies) a competitive price.

Anonymous where possible (Vinted needs no login; eBay comps use a connected
eBay app token when present). Storage: shared Data-Fabric SQLite db. GBP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("vera.commerce.market")

try:
    from Vera.vera.capability_orchestration import (
        capability, emit_event, now_iso, enum_schema,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.commerce.market").warning("commerce market unavailable: %s", e)
    _CAP_AVAILABLE = False

try:
    import httpx
    HAS_HTTPX = True
except Exception:                              # pragma: no cover
    HAS_HTTPX = False

WATCH_PLATFORMS = ["any", "ebay", "vinted"]
ALERT_KINDS = ["deal", "reprice"]
# a listing this far below the market median counts as a deal even with no target
DEFAULT_DEAL_DISCOUNT = 0.25
_SCHEMA_READY = False


def _f(v, default=0.0):
    try: return float(v)
    except (TypeError, ValueError): return default

def _i(v, default=0):
    try: return int(v)
    except (TypeError, ValueError): return default

def _new_id(prefix): return f"{prefix}_{uuid.uuid4().hex[:12]}"

async def _run(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

def _core():      return sys.modules.get("commerce_capabilities")
def _platforms(): return sys.modules.get("commerce_platforms")
def _vinted():    return sys.modules.get("commerce_vinted")
def _pricing():   return sys.modules.get("commerce_pricing_capabilities")


def _summarise(prices: List[float]) -> Optional[dict]:
    prices = sorted(p for p in prices if p and p > 0)
    if not prices:
        return None
    return {"low": round(prices[0], 2), "median": round(statistics.median(prices), 2),
            "high": round(prices[-1], 2), "avg": round(sum(prices) / len(prices), 2),
            "sample_size": len(prices)}


# ─────────────────────────────────────────────────────────────────────────────
# Source: eBay Browse (live items, GB marketplace)
# ─────────────────────────────────────────────────────────────────────────────

async def _ebay_items(query: str, limit: int = 50) -> dict:
    if not HAS_HTTPX:
        return {"items": [], "summary": None}
    plat = _platforms()
    if not plat:
        return {"items": [], "summary": None}
    acct = await _run(plat._db_first_account, "ebay", True)
    if not acct:
        return {"items": [], "summary": None, "note": "no eBay account connected"}
    token = await plat.ebay_app_token(acct["id"])
    if not token:
        return {"items": [], "summary": None, "note": "no eBay app token"}
    base = plat.EBAY_ENV.get(acct.get("env") or "production",
                             plat.EBAY_ENV["production"])["api"]
    mkt = plat._ebay_marketplace(acct) if hasattr(plat, "_ebay_marketplace") else "EBAY_GB"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{base}/buy/browse/v1/item_summary/search",
                            headers={"Authorization": f"Bearer {token}",
                                     "X-EBAY-C-MARKETPLACE-ID": mkt},
                            params={"q": query, "limit": min(int(limit), 100)})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.debug("ebay market search failed: %s", e)
        return {"items": [], "summary": None, "note": f"ebay error: {e}"}
    items, prices = [], []
    for it in data.get("itemSummaries", []):
        pr = it.get("price") or {}
        val = _f(pr.get("value"), None) if pr.get("value") is not None else None
        if val:
            prices.append(val)
        items.append({
            "platform": "ebay", "external_id": it.get("itemId", ""),
            "title": it.get("title", ""), "price": val,
            "currency": pr.get("currency", "GBP"),
            "condition": it.get("condition", ""),
            "url": it.get("itemWebUrl", ""),
            "photo": ((it.get("image") or {}).get("imageUrl", "")),
            "seller": ((it.get("seller") or {}).get("username", "")),
        })
    return {"items": items, "summary": _summarise(prices)}


async def _vinted_items(query: str, limit: int = 48) -> dict:
    v = _vinted()
    if not v or not hasattr(v, "vinted_search"):
        return {"items": [], "summary": None, "note": "vinted module not loaded"}
    res = await v.vinted_search(query, per_page=int(limit))
    if res.get("error"):
        return {"items": [], "summary": None, "note": res["error"]}
    items = []
    for it in res.get("items", []):
        items.append({"platform": "vinted", "external_id": it.get("external_id", ""),
                      "title": it.get("title", ""), "price": it.get("price"),
                      "currency": it.get("currency", "GBP"),
                      "condition": it.get("condition", ""), "url": it.get("url", ""),
                      "photo": it.get("photo", ""), "seller": it.get("seller", "")})
    return {"items": items, "summary": res.get("summary")}


# ─────────────────────────────────────────────────────────────────────────────
# Schema — watches + alerts
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_schema_sync():
    global _SCHEMA_READY
    conn = _sqlite_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS commerce_watches (
                id           TEXT PRIMARY KEY,
                name         TEXT,
                query        TEXT,
                platform     TEXT DEFAULT 'any',
                max_price    REAL,
                condition    TEXT,
                category     TEXT,
                deal_discount REAL,
                active       INTEGER DEFAULT 1,
                notify       INTEGER DEFAULT 1,
                last_scan    TEXT,
                last_hits    INTEGER DEFAULT 0,
                notes        TEXT,
                created_at   TEXT,
                updated_at   TEXT
            );
            CREATE TABLE IF NOT EXISTS commerce_market_alerts (
                id          TEXT PRIMARY KEY,
                watch_id    TEXT,
                kind        TEXT,
                platform    TEXT,
                external_id TEXT,
                title       TEXT,
                price       REAL,
                currency    TEXT DEFAULT 'GBP',
                market_median REAL,
                reason      TEXT,
                url         TEXT,
                photo       TEXT,
                seen        INTEGER DEFAULT 0,
                ts          TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_alerts_watch ON commerce_market_alerts(watch_id);
            CREATE INDEX IF NOT EXISTS ix_alerts_ts ON commerce_market_alerts(ts);
        """)
        conn.commit()
    finally:
        conn.close()
    _SCHEMA_READY = True

async def _ensure_schema():
    if not _SCHEMA_READY:
        await _run(_ensure_schema_sync)

_W_COLS = ("id", "name", "query", "platform", "max_price", "condition", "category",
           "deal_discount", "active", "notify", "last_scan", "last_hits", "notes",
           "created_at", "updated_at")

def _db_upsert_watch(fields: dict) -> dict:
    conn = _sqlite_conn()
    try:
        wid = fields.get("id") or ""
        existing = conn.execute("SELECT * FROM commerce_watches WHERE id=?",
                                (wid,)).fetchone() if wid else None
        base = dict(existing) if existing else {}
        wid = base.get("id") or wid or _new_id("watch")
        merged = {**base, **{k: v for k, v in fields.items() if v is not None}}
        now = now_iso()
        row = {c: merged.get(c) for c in _W_COLS}
        row["id"] = wid
        row["created_at"] = base.get("created_at") or now
        row["updated_at"] = now
        row["active"] = _i(merged.get("active", 1))
        row["notify"] = _i(merged.get("notify", 1))
        row["last_hits"] = _i(merged.get("last_hits", 0))
        conn.execute("INSERT OR REPLACE INTO commerce_watches (%s) VALUES (%s)" %
                     (",".join(_W_COLS), ",".join("?" for _ in _W_COLS)),
                     tuple(row[c] for c in _W_COLS))
        conn.commit()
        return dict(conn.execute("SELECT * FROM commerce_watches WHERE id=?", (wid,)).fetchone())
    finally:
        conn.close()

def _db_list_watches(active_only: bool = False) -> List[dict]:
    conn = _sqlite_conn()
    try:
        sql = "SELECT * FROM commerce_watches"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY updated_at DESC"
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()

def _db_get_watch(wid: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM commerce_watches WHERE id=?", (wid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()

def _db_delete_watch(wid: str) -> bool:
    conn = _sqlite_conn()
    try:
        cur = conn.execute("DELETE FROM commerce_watches WHERE id=?", (wid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()

def _db_alert_exists(watch_id: str, platform: str, external_id: str) -> bool:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT 1 FROM commerce_market_alerts WHERE watch_id=? AND "
                         "platform=? AND external_id=? LIMIT 1",
                         (watch_id, platform, external_id)).fetchone()
        return bool(r)
    finally:
        conn.close()

def _db_add_alert(a: dict) -> dict:
    conn = _sqlite_conn()
    try:
        aid = _new_id("al")
        conn.execute(
            "INSERT INTO commerce_market_alerts (id,watch_id,kind,platform,external_id,"
            "title,price,currency,market_median,reason,url,photo,seen,ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, a.get("watch_id", ""), a.get("kind", "deal"), a.get("platform", ""),
             a.get("external_id", ""), a.get("title", ""), _f(a.get("price")),
             a.get("currency", "GBP"), a.get("market_median"), a.get("reason", ""),
             a.get("url", ""), a.get("photo", ""), 0, now_iso()))
        conn.commit()
        a["id"] = aid
        return a
    finally:
        conn.close()

def _db_list_alerts(kind: str = "", unseen_only: bool = False, limit: int = 100) -> List[dict]:
    conn = _sqlite_conn()
    try:
        sql = "SELECT * FROM commerce_market_alerts"; where, args = [], []
        if kind:        where.append("kind=?"); args.append(kind)
        if unseen_only: where.append("seen=0")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"; args.append(int(limit))
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()

def _db_mark_alerts_seen(ids: List[str]) -> int:
    if not ids:
        return 0
    conn = _sqlite_conn()
    try:
        q = ",".join("?" for _ in ids)
        cur = conn.execute(f"UPDATE commerce_market_alerts SET seen=1 WHERE id IN ({q})", ids)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Core scan logic
# ─────────────────────────────────────────────────────────────────────────────

async def _market_for(query: str, platform: str, limit: int) -> dict:
    """Return merged items + per-source summaries for a query."""
    sources = {}
    all_items = []
    if platform in ("any", "ebay"):
        eb = await _ebay_items(query, limit)
        sources["ebay"] = eb.get("summary")
        all_items += eb.get("items", [])
    if platform in ("any", "vinted"):
        vi = await _vinted_items(query, limit)
        sources["vinted"] = vi.get("summary")
        all_items += vi.get("items", [])
    combined = _summarise([_f(i.get("price")) for i in all_items if i.get("price")])
    return {"items": all_items, "by_platform": sources, "combined": combined}


async def _scan_watch(w: dict) -> dict:
    query = w.get("query") or w.get("name") or ""
    if not query:
        return {"watch_id": w["id"], "hits": 0, "alerts": []}
    platform = w.get("platform") or "any"
    market = await _market_for(query, platform, 60)
    median = (market["combined"] or {}).get("median")
    max_price = _f(w.get("max_price"), None) if w.get("max_price") not in (None, "") else None
    discount = _f(w.get("deal_discount"), DEFAULT_DEAL_DISCOUNT) or DEFAULT_DEAL_DISCOUNT
    deal_ceiling = None
    if median:
        deal_ceiling = round(median * (1 - discount), 2)
    alerts = []
    for it in sorted(market["items"], key=lambda x: (_f(x.get("price")) or 1e9)):
        price = _f(it.get("price"), None)
        if not price or price <= 0:
            continue
        is_deal = False
        reason = ""
        if max_price is not None and price <= max_price:
            is_deal = True
            reason = f"£{price:.2f} ≤ your £{max_price:.2f} target"
        elif deal_ceiling is not None and price <= deal_ceiling:
            is_deal = True
            reason = f"£{price:.2f} is {round((1 - price / median) * 100)}% below the £{median:.2f} median"
        if not is_deal:
            continue
        if _db_alert_exists(w["id"], it["platform"], it.get("external_id", "")):
            continue
        alert = _db_add_alert({
            "watch_id": w["id"], "kind": "deal", "platform": it["platform"],
            "external_id": it.get("external_id", ""), "title": it.get("title", ""),
            "price": price, "currency": it.get("currency", "GBP"),
            "market_median": median, "reason": reason, "url": it.get("url", ""),
            "photo": it.get("photo", "")})
        alerts.append(alert)
    _db_upsert_watch({"id": w["id"], "last_scan": now_iso(), "last_hits": len(alerts)})
    return {"watch_id": w["id"], "name": w.get("name"), "query": query,
            "median": median, "deal_ceiling": deal_ceiling, "hits": len(alerts),
            "alerts": alerts}


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    @capability(
        "business.market.search", http_method="POST",
        http_path="/business/market/search", http_tags=["commerce"],
        schema=enum_schema(platform=WATCH_PLATFORMS),
        description="Search the live market across eBay + Vinted for one query, merged, "
                    "with per-platform and combined price stats (low/median/high/avg). "
                    "For pricing decisions and deal-hunting. Input: query (str!), platform "
                    "(any|ebay|vinted), limit (int default 50). Output: {items:[{platform,"
                    "title,price,currency,condition,url,photo}], by_platform:{ebay,vinted}, "
                    "combined:{low,median,high,avg,sample_size}}.")
    async def cap_market_search(query: str = "", platform: str = "any", limit: int = 50,
                                trace_id=None):
        if not query.strip():
            return {"error": "query required"}
        await emit_event({"type": "commerce.progress", "stage": "market.search",
                          "message": f"market '{query}' ({platform})"})
        m = await _market_for(query, platform if platform in WATCH_PLATFORMS else "any",
                              int(limit))
        return {"query": query, "items": m["items"], "count": len(m["items"]),
                "by_platform": m["by_platform"], "combined": m["combined"]}

    @capability(
        "business.watch.upsert", http_method="POST",
        http_path="/business/watch/upsert", http_tags=["commerce"],
        schema=enum_schema(platform=WATCH_PLATFORMS),
        description="Create / update a market watch (a saved search for buy "
                    "opportunities). Input: id (omit to create), name (str!), query "
                    "(str — search terms; defaults to name), platform (any|ebay|vinted), "
                    "max_price (float — flag listings at/below this), deal_discount "
                    "(float 0-1 — else flag this far below median, default 0.25), "
                    "condition, category, active (bool), notify (bool), notes. "
                    "Output: {ok, watch}.")
    async def cap_watch_upsert(
        id: str = "", name: str = "", query: str = "", platform: str = "any",
        max_price: float = None, deal_discount: float = None, condition: str = "",
        category: str = "", active: bool = True, notify: bool = True, notes: str = "",
        trace_id=None):
        await _ensure_schema()
        if not (name or id):
            return {"error": "name required"}
        w = await _run(_db_upsert_watch, {
            "id": id or None, "name": name or None, "query": query or name or None,
            "platform": platform, "max_price": max_price, "deal_discount": deal_discount,
            "condition": condition or None, "category": category or None,
            "active": 1 if active else 0, "notify": 1 if notify else 0,
            "notes": notes or None})
        return {"ok": True, "watch": w}

    @capability(
        "business.watch.list", http_method="GET",
        http_path="/business/watch/list", http_tags=["commerce"],
        memory="off", silent=True,
        description="List market watches. Input: active_only (bool). "
                    "Output: {watches:[...], count}.")
    async def cap_watch_list(active_only: bool = False, trace_id=None):
        await _ensure_schema()
        rows = await _run(_db_list_watches, bool(active_only))
        return {"watches": rows, "count": len(rows)}

    @capability(
        "business.watch.delete", http_method="POST",
        http_path="/business/watch/delete", http_tags=["commerce"],
        description="Delete a market watch. Input: id (str!). Output: {ok}.")
    async def cap_watch_delete(id: str = "", trace_id=None):
        await _ensure_schema()
        if not id:
            return {"error": "id required"}
        ok = await _run(_db_delete_watch, id)
        return {"ok": ok} if ok else {"error": "not found"}

    @capability(
        "business.watch.scan", http_method="POST",
        http_path="/business/watch/scan", http_tags=["commerce"],
        description="Run market watches now and record buy-opportunity alerts (new "
                    "listings at/below target or well below the median). Runs all active "
                    "watches, or one by id. Also emits an event per new deal so the "
                    "operator is notified. Schedulable on a loop/cron. Input: id (str — "
                    "one watch; else all active). Output: {ok, scanned, new_alerts, "
                    "results:[{watch_id,name,hits}]}.")
    async def cap_watch_scan(id: str = "", trace_id=None):
        await _ensure_schema()
        watches = ([await _run(_db_get_watch, id)] if id
                   else await _run(_db_list_watches, True))
        watches = [w for w in watches if w]
        if not watches:
            return {"ok": True, "scanned": 0, "new_alerts": 0, "results": []}
        results, total = [], 0
        for w in watches:
            try:
                r = await _scan_watch(w)
            except Exception as e:
                log.warning("watch scan failed (%s): %s", w.get("name"), e)
                r = {"watch_id": w["id"], "name": w.get("name"), "hits": 0,
                     "error": str(e), "alerts": []}
            total += r.get("hits", 0)
            if r.get("hits") and w.get("notify", 1):
                for al in r["alerts"][:5]:
                    await emit_event({"type": "commerce.alert", "stage": "deal",
                                      "message": f"DEAL {w.get('name')}: {al['title'][:48]} "
                                                 f"£{_f(al['price']):.2f} — {al['reason']}",
                                      "url": al.get("url", "")})
            results.append({"watch_id": r["watch_id"], "name": r.get("name"),
                            "median": r.get("median"), "hits": r.get("hits", 0)})
        return {"ok": True, "scanned": len(watches), "new_alerts": total, "results": results}

    @capability(
        "business.market.alerts", http_method="GET",
        http_path="/business/market/alerts", http_tags=["commerce"],
        memory="off", silent=True,
        schema=enum_schema(kind=ALERT_KINDS),
        description="List recent market alerts (buy opportunities + repricing flags). "
                    "Input: kind (deal|reprice), unseen_only (bool), limit (int). "
                    "Output: {alerts:[{title,price,url,reason,platform,kind,seen}], count}.")
    async def cap_market_alerts(kind: str = "", unseen_only: bool = False, limit: int = 100,
                                trace_id=None):
        await _ensure_schema()
        rows = await _run(_db_list_alerts, kind, bool(unseen_only), int(limit))
        return {"alerts": rows, "count": len(rows)}

    @capability(
        "business.market.alert.seen", http_method="POST",
        http_path="/business/market/alert/seen", http_tags=["commerce"],
        description="Mark alert(s) as seen. Input: ids (list of alert ids) or all (bool "
                    "— mark every unseen). Output: {ok, updated}.")
    async def cap_market_alert_seen(ids: List = None, all: bool = False, trace_id=None):
        await _ensure_schema()
        if all and not ids:
            unseen = await _run(_db_list_alerts, "", True, 1000)
            ids = [a["id"] for a in unseen]
        n = await _run(_db_mark_alerts_seen, ids or [])
        return {"ok": True, "updated": n}

    @capability(
        "business.market.reprice", http_method="POST",
        http_path="/business/market/reprice", http_tags=["commerce"],
        description="Check one product's price against the live market and suggest a "
                    "competitive price (never below cost+margin). Optionally apply it and "
                    "flag a reprice alert. Input: product_id (str!), margin (float default "
                    "0.30), undercut (float default 0.03), apply (bool — write the new "
                    "price). Output: {ok, current_price, market_median, suggested_price, "
                    "applied}.")
    async def cap_market_reprice(product_id: str = "", margin: float = 0.30,
                                 undercut: float = 0.03, apply: bool = False, trace_id=None):
        await _ensure_schema()
        core = _core(); pricing = _pricing()
        if not core:
            return {"error": "commerce core not loaded"}
        prod = await _run(core._db_get_product, product_id)
        if not prod:
            return {"error": "product not found"}
        suggested = None; median = None; basis = ""
        if pricing and hasattr(pricing, "cap_price_suggest"):
            sug = await pricing.cap_price_suggest(product_id=prod["id"], margin=margin,
                                                  undercut=undercut, trace_id=None)
            if sug.get("ok"):
                suggested = sug["suggested_price"]; median = sug.get("market_median")
                basis = sug.get("basis", "")
        if suggested is None:
            m = await _market_for(prod.get("name") or prod.get("upc") or "", "any", 40)
            median = (m["combined"] or {}).get("median")
            if median:
                cost = _f(prod.get("cost"))
                floor = round(cost * (1 + margin), 2) if cost else 0
                suggested = round(max(median * (1 - undercut), floor), 2)
        if suggested is None:
            return {"error": "no market data to reprice against"}
        current = _f(prod.get("price"))
        applied = False
        if apply and suggested and abs(suggested - current) >= 0.01:
            await _run(core._db_upsert_product, {"id": prod["id"], "price": suggested})
            applied = True
        # flag a reprice alert when materially off
        if median and current and abs(current - suggested) / max(current, 1) >= 0.10:
            reason = ("overpriced vs market" if current > suggested else "underpriced vs market")
            await _run(_db_add_alert, {
                "watch_id": "", "kind": "reprice", "platform": "own",
                "external_id": prod["id"], "title": prod.get("name", ""),
                "price": current, "currency": prod.get("currency", "GBP"),
                "market_median": median,
                "reason": f"{reason}: yours £{current:.2f} vs suggested £{suggested:.2f}",
                "url": "", "photo": ""})
        return {"ok": True, "current_price": round(current, 2), "market_median": median,
                "suggested_price": suggested, "basis": basis, "applied": applied}

    log.info("business.market: ready (eBay + Vinted OSINT, watches, alerts)")
