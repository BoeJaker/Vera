"""
commerce_ops.py — Consumables costing, suppliers & drop-shipping
================================================================

The "true cost & sourcing" layer that makes the P&L honest.

  • **Materials / consumables** — you buy packaging, mailing bags, bubble wrap,
    labels and postage in bulk; ``business.material.*`` records each bulk purchase
    (cost + quantity) and derives a **per-item** cost, optionally scoped to an item
    category. ``business.material.per_item`` returns the packaging cost to attribute
    to one sale — and the profit engine (``commerce_uk_tax._order_finance``) uses it
    automatically instead of the flat packaging default, so accounting is per-item.

  • **Suppliers & sourcing** — ``business.supplier.*`` keeps your suppliers
    (AliExpress, wholesalers). ``business.sourcing.*`` links a catalog product to a
    supplier offer: unit cost (in the supplier's currency, converted to GBP with a
    live keyless rate), per-item shipping, MOQ, and a **fulfilment mode**:
      – ``stock``    — you buy in and hold the units on-site in the UK;
      – ``dropship`` — the supplier ships straight from China per order.

  • **Restock & profit** — ``business.restock`` buys stock in (creates units at the
    landed cost) for stock-mode sourcing. ``business.dropship.profit`` computes a
    full, honest margin for a would-be listing — revenue − supplier cost − supplier
    shipping − marketplace fee − (your postage if you hold it) − packaging — and
    flags the UK import-VAT position (eBay collects VAT on <£135 consignments
    imported direct to the buyer) so the number, and the tax, are right.

Storage: shared Data-Fabric SQLite db. Money in GBP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

log = logging.getLogger("vera.commerce.ops")

try:
    from Vera.vera.capability_orchestration import (
        capability, emit_event, now_iso, enum_schema, CAPABILITY_REGISTRY,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.commerce.ops").warning("commerce ops unavailable: %s", e)
    _CAP_AVAILABLE = False

try:
    import httpx
    HAS_HTTPX = True
except Exception:                              # pragma: no cover
    HAS_HTTPX = False

MATERIAL_KINDS = ["packaging", "postage", "label", "filler", "other"]
SUPPLIER_KINDS = ["aliexpress", "wholesale", "retail", "other"]
FULFIL_MODES = ["stock", "dropship"]
UK_IMPORT_VAT_THRESHOLD = 135.0                # £ — eBay collects VAT below this
_SCHEMA_READY = False


def _f(v, d=0.0):
    try: return float(v)
    except (TypeError, ValueError): return d

def _i(v, d=0):
    try: return int(v)
    except (TypeError, ValueError): return d

def _new_id(p): return f"{p}_{uuid.uuid4().hex[:12]}"

async def _run(fn, *a):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *a)

def _core(): return sys.modules.get("commerce_capabilities")

def _cap_raw(name: str):
    reg = CAPABILITY_REGISTRY.get(name) if CAPABILITY_REGISTRY else None
    return reg.get("raw") if reg else None


# ─────────────────────────────────────────────────────────────────────────────
# FX — any currency → GBP (keyless, cached)
# ─────────────────────────────────────────────────────────────────────────────

_FX: Dict[str, dict] = {}

async def _to_gbp(amount: float, currency: str) -> float:
    amount = _f(amount)
    cur = (currency or "GBP").upper()
    if cur == "GBP" or not amount:
        return round(amount, 2)
    ent = _FX.get(cur)
    if not ent or time.time() - ent["at"] > 6 * 3600:
        rate = ent["rate"] if ent else None
        if HAS_HTTPX:
            try:
                async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
                    r = await c.get(f"https://api.frankfurter.app/latest?from={cur}&to=GBP")
                    rate = _f((r.json().get("rates") or {}).get("GBP")) or rate
            except Exception as e:
                log.debug("fx %s: %s", cur, e)
        if rate:
            _FX[cur] = {"rate": rate, "at": time.time()}
    rate = (_FX.get(cur) or {}).get("rate")
    return round(amount * rate, 2) if rate else round(amount, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_schema_sync():
    global _SCHEMA_READY
    conn = _sqlite_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS commerce_materials (
                id         TEXT PRIMARY KEY,
                name       TEXT,
                kind       TEXT DEFAULT 'packaging',
                category   TEXT,
                bulk_cost  REAL DEFAULT 0,
                bulk_qty   REAL DEFAULT 1,
                per_item   REAL DEFAULT 0,
                store_id   TEXT,
                notes      TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS commerce_suppliers (
                id         TEXT PRIMARY KEY,
                name       TEXT,
                kind       TEXT DEFAULT 'aliexpress',
                url        TEXT,
                currency   TEXT DEFAULT 'GBP',
                lead_days  INTEGER DEFAULT 0,
                store_id   TEXT,
                notes      TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS commerce_sourcing (
                id            TEXT PRIMARY KEY,
                product_id    TEXT,
                supplier_id   TEXT,
                supplier_url  TEXT,
                supplier_sku  TEXT,
                unit_cost     REAL DEFAULT 0,
                currency      TEXT DEFAULT 'GBP',
                unit_cost_gbp REAL DEFAULT 0,
                shipping_gbp  REAL DEFAULT 0,
                fulfil_mode   TEXT DEFAULT 'dropship',
                moq           INTEGER DEFAULT 1,
                notes         TEXT,
                store_id      TEXT,
                created_at    TEXT,
                updated_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_sourcing_prod ON commerce_sourcing(product_id);
        """)
        conn.commit()
    finally:
        conn.close()
    _SCHEMA_READY = True

async def _ensure_schema():
    if not _SCHEMA_READY:
        await _run(_ensure_schema_sync)


def _row(table: str, cols, fields: dict, id_prefix: str) -> dict:
    conn = _sqlite_conn()
    try:
        rid = fields.get("id") or ""
        existing = conn.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone() if rid else None
        base = dict(existing) if existing else {}
        rid = base.get("id") or rid or _new_id(id_prefix)
        merged = {**base, **{k: v for k, v in fields.items() if v is not None}}
        now = now_iso()
        row = {c: merged.get(c) for c in cols}
        row["id"] = rid
        row["created_at"] = base.get("created_at") or now
        row["updated_at"] = now
        conn.execute(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                     tuple(row[c] for c in cols))
        conn.commit()
        return dict(conn.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone())
    finally:
        conn.close()


# ── materials ────────────────────────────────────────────────────────────────

_MAT_COLS = ("id", "name", "kind", "category", "bulk_cost", "bulk_qty", "per_item",
             "store_id", "notes", "created_at", "updated_at")

def _db_upsert_material(fields: dict) -> dict:
    fields = dict(fields)
    fields["per_item"] = round(_f(fields.get("bulk_cost")) / max(_f(fields.get("bulk_qty"), 1), 1e-9), 4)
    return _row("commerce_materials", _MAT_COLS, fields, "mat")

def _db_list_materials(category: str = "", store_id: str = "") -> List[dict]:
    conn = _sqlite_conn()
    try:
        sql = "SELECT * FROM commerce_materials"; where, args = [], []
        if store_id: where.append("(store_id=? OR store_id IS NULL OR store_id='')"); args.append(store_id)
        if where: sql += " WHERE " + " AND ".join(where)
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY kind, name", args).fetchall()]
        if category:
            rows = [r for r in rows if not r.get("category") or r.get("category") == category]
        return rows
    finally:
        conn.close()

def _materials_per_item_sync(category: str = "", store_id: str = "") -> dict:
    """Total per-item consumable cost applicable to a sale, with a breakdown.
    Materials with no category apply to everything; category-specific ones apply
    only to their category."""
    rows = _db_list_materials(category, store_id)
    total = round(sum(_f(r.get("per_item")) for r in rows), 2)
    return {"per_item_total": total,
            "breakdown": [{"name": r["name"], "kind": r["kind"], "per_item": round(_f(r["per_item"]), 4),
                           "category": r.get("category") or "all"} for r in rows]}


# ── suppliers + sourcing ─────────────────────────────────────────────────────

_SUP_COLS = ("id", "name", "kind", "url", "currency", "lead_days", "store_id",
             "notes", "created_at", "updated_at")
_SRC_COLS = ("id", "product_id", "supplier_id", "supplier_url", "supplier_sku",
             "unit_cost", "currency", "unit_cost_gbp", "shipping_gbp", "fulfil_mode",
             "moq", "notes", "store_id", "created_at", "updated_at")

def _db_list_suppliers(store_id: str = "") -> List[dict]:
    conn = _sqlite_conn()
    try:
        rows = conn.execute("SELECT * FROM commerce_suppliers ORDER BY name").fetchall()
        out = [dict(r) for r in rows]
        return [r for r in out if not store_id or not r.get("store_id") or r.get("store_id") == store_id]
    finally:
        conn.close()

def _db_list_sourcing(product_id: str = "") -> List[dict]:
    conn = _sqlite_conn()
    try:
        sql = "SELECT * FROM commerce_sourcing"; args = []
        if product_id:
            sql += " WHERE product_id=?"; args.append(product_id)
        return [dict(r) for r in conn.execute(sql + " ORDER BY updated_at DESC", args).fetchall()]
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    @capability(
        "business.material.upsert", http_method="POST",
        http_path="/business/material/upsert", http_tags=["commerce"],
        schema=enum_schema(kind=MATERIAL_KINDS),
        description="Record a bulk purchase of a packing/postage consumable; the "
                    "per-item cost is derived (bulk_cost / bulk_qty) for accurate "
                    "per-sale accounting. Input: id (blank=new), name (str!), kind "
                    "(packaging|postage|label|filler|other), category (item category "
                    "this applies to; blank = all items), bulk_cost (float £), bulk_qty "
                    "(float — units in the bulk buy), store_id, notes. "
                    "Output: {ok, material}.")
    async def cap_material_upsert(id: str = "", name: str = "", kind: str = "packaging",
                                  category: str = "", bulk_cost: float = 0.0,
                                  bulk_qty: float = 1.0, store_id: str = "",
                                  notes: str = "", trace_id=None):
        await _ensure_schema()
        if not (name or id):
            return {"error": "name required"}
        m = await _run(_db_upsert_material, {"id": id or None, "name": name or None,
            "kind": kind, "category": category or None, "bulk_cost": bulk_cost,
            "bulk_qty": bulk_qty, "store_id": store_id or None, "notes": notes or None})
        return {"ok": True, "material": m}

    @capability(
        "business.material.list", http_method="GET",
        http_path="/business/material/list", http_tags=["commerce"],
        memory="off", silent=True,
        description="List consumables + their per-item costs. Input: category (filter "
                    "to those applying to this category + the universal ones), store_id. "
                    "Output: {materials:[...], per_item_total}.")
    async def cap_material_list(category: str = "", store_id: str = "", trace_id=None):
        await _ensure_schema()
        rows = await _run(_db_list_materials, category, store_id)
        agg = await _run(_materials_per_item_sync, category, store_id)
        return {"materials": rows, "per_item_total": agg["per_item_total"]}

    @capability(
        "business.material.delete", http_method="POST",
        http_path="/business/material/delete", http_tags=["commerce"],
        description="Delete a consumable. Input: id (str!). Output: {ok}.")
    async def cap_material_delete(id: str = "", trace_id=None):
        await _ensure_schema()
        def _d():
            conn = _sqlite_conn()
            try: conn.execute("DELETE FROM commerce_materials WHERE id=?", (id,)); conn.commit()
            finally: conn.close()
        await _run(_d)
        return {"ok": True}

    @capability(
        "business.material.per_item", http_method="GET",
        http_path="/business/material/per_item", http_tags=["commerce"],
        memory="off", silent=True,
        description="The total per-item consumable (packaging/postage-materials) cost "
                    "to attribute to one sale of a given category. Input: category, "
                    "store_id. Output: {per_item_total, breakdown:[...]}.")
    async def cap_material_per_item(category: str = "", store_id: str = "", trace_id=None):
        await _ensure_schema()
        return await _run(_materials_per_item_sync, category, store_id)

    @capability(
        "business.supplier.upsert", http_method="POST",
        http_path="/business/supplier/upsert", http_tags=["commerce"],
        schema=enum_schema(kind=SUPPLIER_KINDS),
        description="Create/update a supplier (AliExpress, wholesaler…). Input: id, "
                    "name (str!), kind (aliexpress|wholesale|retail|other), url, "
                    "currency (e.g. CNY|USD|GBP), lead_days (int), store_id, notes. "
                    "Output: {ok, supplier}.")
    async def cap_supplier_upsert(id: str = "", name: str = "", kind: str = "aliexpress",
                                  url: str = "", currency: str = "GBP", lead_days: int = 0,
                                  store_id: str = "", notes: str = "", trace_id=None):
        await _ensure_schema()
        if not (name or id):
            return {"error": "name required"}
        s = await _run(_row, "commerce_suppliers", _SUP_COLS, {"id": id or None,
            "name": name or None, "kind": kind, "url": url or None,
            "currency": (currency or "GBP").upper(), "lead_days": lead_days,
            "store_id": store_id or None, "notes": notes or None}, "sup")
        return {"ok": True, "supplier": s}

    @capability(
        "business.supplier.list", http_method="GET",
        http_path="/business/supplier/list", http_tags=["commerce"],
        memory="off", silent=True,
        description="List suppliers. Input: store_id. Output: {suppliers:[...]}.")
    async def cap_supplier_list(store_id: str = "", trace_id=None):
        await _ensure_schema()
        return {"suppliers": await _run(_db_list_suppliers, store_id)}

    @capability(
        "business.supplier.delete", http_method="POST",
        http_path="/business/supplier/delete", http_tags=["commerce"],
        description="Delete a supplier. Input: id (str!). Output: {ok}.")
    async def cap_supplier_delete(id: str = "", trace_id=None):
        await _ensure_schema()
        def _d():
            conn = _sqlite_conn()
            try: conn.execute("DELETE FROM commerce_suppliers WHERE id=?", (id,)); conn.commit()
            finally: conn.close()
        await _run(_d)
        return {"ok": True}

    @capability(
        "business.sourcing.upsert", http_method="POST",
        http_path="/business/sourcing/upsert", http_tags=["commerce"],
        schema=enum_schema(fulfil_mode=FULFIL_MODES),
        description="Link a catalog product to a supplier offer (its source of stock). "
                    "The unit cost is converted to GBP with a live rate. Input: id, "
                    "product_id (str!), supplier_id, supplier_url, supplier_sku, "
                    "unit_cost (float — in the supplier's currency), currency (CNY|USD|"
                    "GBP), shipping_gbp (per-item shipping £), fulfil_mode (stock=hold "
                    "on-site | dropship=ship from source per order), moq (int), "
                    "store_id, notes. Also sets the product's cost to the landed unit "
                    "cost so COGS is right. Output: {ok, sourcing}.")
    async def cap_sourcing_upsert(id: str = "", product_id: str = "", supplier_id: str = "",
                                  supplier_url: str = "", supplier_sku: str = "",
                                  unit_cost: float = 0.0, currency: str = "GBP",
                                  shipping_gbp: float = 0.0, fulfil_mode: str = "dropship",
                                  moq: int = 1, store_id: str = "", notes: str = "",
                                  trace_id=None):
        await _ensure_schema()
        if not product_id:
            return {"error": "product_id required"}
        cur = (currency or "GBP").upper()
        cost_gbp = await _to_gbp(unit_cost, cur)
        src = await _run(_row, "commerce_sourcing", _SRC_COLS, {"id": id or None,
            "product_id": product_id, "supplier_id": supplier_id or None,
            "supplier_url": supplier_url or None, "supplier_sku": supplier_sku or None,
            "unit_cost": unit_cost, "currency": cur, "unit_cost_gbp": cost_gbp,
            "shipping_gbp": shipping_gbp, "fulfil_mode": fulfil_mode, "moq": moq,
            "store_id": store_id or None, "notes": notes or None}, "src")
        # keep the product cost == landed cost (supplier cost + per-item shipping)
        core = _core()
        if core:
            await _run(core._db_upsert_product, {"id": product_id,
                       "cost": round(cost_gbp + _f(shipping_gbp), 2)})
        return {"ok": True, "sourcing": src}

    @capability(
        "business.sourcing.list", http_method="GET",
        http_path="/business/sourcing/list", http_tags=["commerce"],
        memory="off", silent=True,
        description="Supplier offers for a product (or all). Input: product_id. "
                    "Output: {sourcing:[...]}.")
    async def cap_sourcing_list(product_id: str = "", trace_id=None):
        await _ensure_schema()
        return {"sourcing": await _run(_db_list_sourcing, product_id)}

    @capability(
        "business.restock", http_method="POST",
        http_path="/business/restock", http_tags=["commerce"],
        description="Buy stock in against a supplier offer. For 'stock' fulfilment it "
                    "creates qty units at the landed cost (so they enter inventory ready "
                    "to sell); for 'dropship' it records the intent only (no physical "
                    "stock — the listing fulfils from source per order). Input: "
                    "sourcing_id (str!) OR product_id, qty (int default = MOQ), "
                    "condition (default new), store_id. Output: {ok, mode, created, "
                    "landed_cost}.")
    async def cap_restock(sourcing_id: str = "", product_id: str = "", qty: int = 0,
                          condition: str = "new", store_id: str = "", trace_id=None):
        await _ensure_schema()
        srcs = await _run(_db_list_sourcing, product_id)
        src = None
        if sourcing_id:
            src = next((s for s in await _run(_db_list_sourcing, "") if s["id"] == sourcing_id), None)
        src = src or (srcs[0] if srcs else None)
        if not src:
            return {"error": "no sourcing record — add one with business.sourcing.upsert"}
        landed = round(_f(src.get("unit_cost_gbp")) + _f(src.get("shipping_gbp")), 2)
        n = _i(qty) or _i(src.get("moq"), 1) or 1
        if src.get("fulfil_mode") == "dropship":
            await emit_event({"type": "commerce.progress", "stage": "restock",
                              "message": f"dropship intent ×{n} @ £{landed}"})
            return {"ok": True, "mode": "dropship", "created": 0, "landed_cost": landed,
                    "note": "Drop-ship — no physical stock created; the listing fulfils from source."}
        intake = _cap_raw("business.unit.intake")
        made = 0
        if intake:
            for _ in range(n):
                r = await intake(product_id=src["product_id"], condition=condition,
                                 completeness="sealed", cost=landed, currency="GBP",
                                 store_id=store_id or src.get("store_id") or "",
                                 lookup=False, trace_id=None)
                if r.get("ok"): made += 1
        await emit_event({"type": "commerce.progress", "stage": "restock",
                          "message": f"restocked {made} unit(s) @ £{landed}"})
        return {"ok": True, "mode": "stock", "created": made, "landed_cost": landed}

    @capability(
        "business.dropship.profit", http_method="POST",
        http_path="/business/dropship/profit", http_tags=["commerce"],
        schema=enum_schema(platform=["ebay", "vinted"], fulfil_mode=FULFIL_MODES),
        description="Honest margin for a drop-ship / sourced listing at a given sell "
                    "price: revenue − supplier cost − supplier shipping − marketplace "
                    "fee − (your postage if you hold stock) − packaging materials, with "
                    "the UK import-VAT position flagged (eBay collects VAT on <£135 "
                    "consignments shipped direct to the buyer). Input: product_id OR "
                    "sourcing_id, sell_price (float £!), platform (ebay|vinted), "
                    "your_postage (float £ — if you post it yourself), category (for "
                    "packaging), store_id. Output: {ok, breakdown, net_profit, "
                    "margin_pct, vat_note}.")
    async def cap_dropship_profit(product_id: str = "", sourcing_id: str = "",
                                  sell_price: float = 0.0, platform: str = "ebay",
                                  your_postage: float = 0.0, category: str = "",
                                  store_id: str = "", trace_id=None):
        await _ensure_schema()
        all_src = await _run(_db_list_sourcing, "")
        src = next((s for s in all_src if s["id"] == sourcing_id), None) if sourcing_id else None
        if not src and product_id:
            src = next((s for s in all_src if s["product_id"] == product_id), None)
        if not src:
            return {"error": "no sourcing record for this product"}
        price = _f(sell_price)
        if not price:
            return {"error": "sell_price required"}
        # marketplace fee from tax settings' schedule (fallback 12.8% + £0.30)
        fee_pct, fee_fixed = 0.128, 0.30
        tax = sys.modules.get("commerce_uk_tax")
        if tax and hasattr(tax, "_db_get_settings"):
            try:
                sched = ((await _run(tax._db_get_settings)).get("platform_fees") or {}).get(platform) or {}
                fee_pct = _f(sched.get("pct"), fee_pct); fee_fixed = _f(sched.get("fixed"), fee_fixed)
            except Exception:
                pass
        fee = round(fee_pct * price + fee_fixed, 2)
        supplier_cost = _f(src.get("unit_cost_gbp"))
        supplier_ship = _f(src.get("shipping_gbp"))
        stock_mode = src.get("fulfil_mode") == "stock"
        postage = round(_f(your_postage), 2) if stock_mode else 0.0
        pack = (await _run(_materials_per_item_sync, category, store_id))["per_item_total"] if stock_mode else 0.0
        costs = round(supplier_cost + supplier_ship + fee + postage + pack, 2)
        net = round(price - costs, 2)
        vat_note = ("Direct-from-source & ≤ £135: the marketplace (eBay) collects UK "
                    "import VAT at checkout — you neither charge nor reclaim it."
                    if (not stock_mode and price <= UK_IMPORT_VAT_THRESHOLD)
                    else "Held on-site: standard UK VAT rules apply if you are VAT-registered."
                    if stock_mode else
                    "Direct-from-source & > £135: import VAT/duty may fall to the buyer or you — check the consignment value rules.")
        return {"ok": True, "net_profit": net,
                "margin_pct": round(net / price * 100, 1) if price else None,
                "breakdown": {"revenue": round(price, 2), "supplier_cost": supplier_cost,
                              "supplier_shipping": supplier_ship, "marketplace_fee": fee,
                              "your_postage": postage, "packaging": pack, "cost_total": costs},
                "fulfil_mode": src.get("fulfil_mode"), "vat_note": vat_note}

    log.info("business.ops: ready (materials costing + suppliers + drop-ship)")
