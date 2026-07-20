"""
commerce_stores.py — Multiple stores + the master view
======================================================

A **store** is an income **stream** of kind ``ecommerce`` (reusing the business
streams model — see [[commerce-business-tab]]), so each store shows up in the
business graph, dashboard and P&L automatically. Products, orders and listings
carry a ``store_id`` (added to those tables by their own modules), so one Vera
instance can run several shops that sell different things — "Retro Games UK",
"Anime Merch", … — each with its own inventory and books, plus a **master view**
that rolls everything up.

Caps (all ``business.store.*``):
  • ``business.store.list``   — stores + headline metrics.
  • ``business.store.upsert`` — create / rename / recolour a store.
  • ``business.store.delete`` — remove a store (its items become unassigned).
  • ``business.store.master`` — every store's revenue / profit / inventory /
    orders, an *Unassigned* bucket, and combined totals.

Reuses the profit engine in ``commerce_uk_tax`` for accurate per-store P&L.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

log = logging.getLogger("vera.commerce.stores")

try:
    from Vera.vera.capability_orchestration import (
        capability, emit_event, now_iso, enum_schema,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.commerce.stores").warning("commerce stores unavailable: %s", e)
    _CAP_AVAILABLE = False


def _f(v, default=0.0):
    try: return float(v)
    except (TypeError, ValueError): return default

def _i(v, default=0):
    try: return int(v)
    except (TypeError, ValueError): return default

async def _run(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

def _biz():  return sys.modules.get("business_capabilities")
def _core(): return sys.modules.get("commerce_capabilities")
def _tax():  return sys.modules.get("commerce_uk_tax")


# ─────────────────────────────────────────────────────────────────────────────
# Ensure sibling tables (products/orders/listings + store_id columns) exist
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_sync():
    for mod, fn in (("commerce_capabilities", "_ensure_schema_sync"),
                    ("commerce_listing", "_ensure_schema_sync"),
                    ("business_capabilities", "_ensure_schema_sync")):
        m = sys.modules.get(mod)
        if m and hasattr(m, fn):
            try:
                getattr(m, fn)()
            except Exception:
                pass

async def _ensure():
    await _run(_ensure_sync)


# ─────────────────────────────────────────────────────────────────────────────
# Store store (a store IS an ecommerce stream)
# ─────────────────────────────────────────────────────────────────────────────

def _db_list_stores_sync() -> List[dict]:
    biz = _biz()
    if not biz or not hasattr(biz, "_db_list"):
        return []
    # is_sim=0: simulated ecommerce streams (business sim scenarios) are NOT
    # stores — they must never surface in the live Stores view or master rollup.
    return biz._db_list("biz_streams", {"kind": "ecommerce", "is_sim": 0},
                        "name ASC", 500, None)

def _db_upsert_store_sync(fields: dict) -> dict:
    biz = _biz()
    fields = {**fields, "kind": "ecommerce"}
    return biz._db_upsert("biz_streams", fields)

def _db_get_store_sync(sid: str) -> Optional[dict]:
    biz = _biz()
    return biz._db_get("biz_streams", sid) if biz else None

def _db_delete_store_sync(sid: str) -> bool:
    biz = _biz()
    return biz._db_delete("biz_streams", sid) if biz else False


# ─────────────────────────────────────────────────────────────────────────────
# Per-store metrics (inventory via SQL, P&L via the profit engine)
# ─────────────────────────────────────────────────────────────────────────────

def _store_metrics_sync(store_id: str) -> dict:
    conn = _sqlite_conn()
    try:
        prod = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(price*qty_on_hand),0), "
            "COALESCE(SUM(cost*qty_on_hand),0) FROM commerce_products "
            "WHERE COALESCE(store_id,'')=?", (store_id,)).fetchone()
        try:
            live = conn.execute(
                "SELECT COUNT(*) FROM commerce_listings WHERE COALESCE(store_id,'')=? "
                "AND status='published'", (store_id,)).fetchone()[0]
        except Exception:
            live = 0
        open_orders = conn.execute(
            "SELECT COUNT(*) FROM commerce_orders WHERE COALESCE(store_id,'')=? "
            "AND status IN ('new','paid','packed')", (store_id,)).fetchone()[0]
    except Exception:
        prod, live, open_orders = (0, 0, 0), 0, 0
    finally:
        conn.close()
    m = {"product_count": _i(prod[0]), "inventory_retail": round(_f(prod[1]), 2),
         "inventory_cost": round(_f(prod[2]), 2), "listings_live": _i(live),
         "open_orders": _i(open_orders),
         "revenue_30d": 0.0, "net_30d": 0.0, "orders_30d": 0,
         "revenue_all": 0.0, "net_all": 0.0, "orders_all": 0}
    tax = _tax()
    if tax and hasattr(tax, "_iter_orders"):
        try:
            settings = tax._db_get_settings()
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            a30 = tax._aggregate(tax._iter_orders(cutoff, "", "", store_id), settings)
            aall = tax._aggregate(tax._iter_orders("", "", "", store_id), settings)
            m["revenue_30d"] = a30["totals"]["revenue"]; m["net_30d"] = a30["totals"]["net_profit"]
            m["orders_30d"] = a30["totals"]["orders"]
            m["revenue_all"] = aall["totals"]["revenue"]; m["net_all"] = aall["totals"]["net_profit"]
            m["orders_all"] = aall["totals"]["orders"]
        except Exception as e:
            log.debug("store metrics P&L failed: %s", e)
    return m


def _master_sync() -> dict:
    stores = _db_list_stores_sync()
    out_stores = []
    combined = {"product_count": 0, "inventory_retail": 0.0, "inventory_cost": 0.0,
                "listings_live": 0, "open_orders": 0, "revenue_30d": 0.0, "net_30d": 0.0,
                "orders_30d": 0, "revenue_all": 0.0, "net_all": 0.0, "orders_all": 0}
    for s in stores:
        met = _store_metrics_sync(s["id"])
        out_stores.append({"id": s["id"], "name": s.get("name"), "platform": s.get("platform"),
                           "color": s.get("color"), "icon": s.get("icon"),
                           "goal_monthly": _f(s.get("goal_monthly")),
                           "currency": s.get("currency") or "GBP", **met})
        for k in combined:
            combined[k] += met.get(k, 0)
    # unassigned bucket (items with no store_id)
    un = _store_metrics_sync("")
    if any(un.get(k) for k in ("product_count", "orders_all", "listings_live")):
        out_stores.append({"id": "", "name": "Unassigned", "platform": "", "color": "#6a6058",
                           "icon": "?", "goal_monthly": 0.0, "currency": "GBP", **un})
        for k in combined:
            combined[k] += un.get(k, 0)
    for k in ("inventory_retail", "inventory_cost", "revenue_30d", "net_30d",
              "revenue_all", "net_all"):
        combined[k] = round(combined[k], 2)
    combined["store_count"] = len(stores)
    return {"stores": out_stores, "totals": combined}


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    @capability(
        "business.store.list", http_method="GET", http_path="/business/store/list",
        http_tags=["commerce"], memory="off", silent=True,
        description="List the stores (e-commerce income streams) with headline metrics "
                    "(products, live listings, open orders, 30-day revenue/profit, "
                    "inventory value). Output: {stores:[{id,name,platform,revenue_30d,"
                    "net_30d,inventory_retail,product_count,listings_live}], count}.")
    async def cap_store_list(trace_id=None):
        await _ensure()
        stores = await _run(_db_list_stores_sync)
        out = []
        for s in stores:
            met = await _run(_store_metrics_sync, s["id"])
            out.append({"id": s["id"], "name": s.get("name"), "platform": s.get("platform"),
                        "color": s.get("color"), "icon": s.get("icon"),
                        "goal_monthly": _f(s.get("goal_monthly")),
                        "currency": s.get("currency") or "GBP",
                        "status": s.get("status"), "description": s.get("description"), **met})
        return {"stores": out, "count": len(out)}

    @capability(
        "business.store.upsert", http_method="POST", http_path="/business/store/upsert",
        http_tags=["commerce"],
        description="Create or update a store (an e-commerce income stream). "
                    "Input: id (omit to create), name (str!), platform (ebay|vinted|"
                    "multi), goal_monthly (float — monthly revenue target £), currency "
                    "(default GBP), color (hex), icon (emoji), description. "
                    "Output: {ok, store}.")
    async def cap_store_upsert(id: str = "", name: str = "", platform: str = "multi",
                               goal_monthly: float = 0.0, currency: str = "GBP",
                               color: str = "", icon: str = "🏬", description: str = "",
                               trace_id=None):
        await _ensure()
        if not (name or id):
            return {"error": "name required"}
        store = await _run(_db_upsert_store_sync, {
            "id": id or None, "name": name or None, "platform": platform,
            "goal_monthly": goal_monthly, "currency": currency or "GBP",
            "color": color or None, "icon": icon or None,
            "description": description or None, "status": "active"})
        await emit_event({"type": "commerce.progress", "stage": "store",
                          "message": f"store '{store.get('name')}' saved"})
        return {"ok": True, "store": store}

    @capability(
        "business.store.delete", http_method="POST", http_path="/business/store/delete",
        http_tags=["commerce"],
        description="Delete a store. Its products/orders/listings are kept but become "
                    "unassigned (store_id cleared logically). Input: id (str!). Output: {ok}.")
    async def cap_store_delete(id: str = "", trace_id=None):
        await _ensure()
        if not id:
            return {"error": "id required"}
        ok = await _run(_db_delete_store_sync, id)
        return {"ok": ok} if ok else {"error": "not found"}

    @capability(
        "business.store.get", http_method="GET", http_path="/business/store/get",
        http_tags=["commerce"], memory="off", silent=True,
        description="One store's record + live metrics (revenue/profit/inventory/orders). "
                    "Input: id (str! — empty = the Unassigned bucket). Output: {store}.")
    async def cap_store_get(id: str = "", trace_id=None):
        await _ensure()
        met = await _run(_store_metrics_sync, id)
        st = await _run(_db_get_store_sync, id) if id else None
        base = st or {"id": id, "name": "Unassigned", "currency": "GBP"}
        return {"store": {**base, **met}}

    @capability(
        "business.store.master", http_method="GET", http_path="/business/store/master",
        http_tags=["commerce"], memory="off", silent=True,
        description="Master view across every store: per-store revenue/profit/inventory/"
                    "orders (plus an Unassigned bucket) and combined totals. "
                    "Output: {stores:[...], totals:{revenue_30d,net_30d,inventory_retail,"
                    "product_count,store_count,...}}.")
    async def cap_store_master(trace_id=None):
        await _ensure()
        return await _run(_master_sync)

    log.info("commerce.stores: ready (store = ecommerce stream + master view)")
