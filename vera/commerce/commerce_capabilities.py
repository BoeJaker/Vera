"""
commerce_capabilities.py — Business & e-commerce operations (core)
==================================================================

The **Business** tab. Gives a team of Vera agents (with one human overseeing
them) the tools to run a small e-commerce / service business:

  • Inventory   — products, stock levels, cost/price, reorder points, an audit
                  trail of every stock movement.
  • Orders      — manual + marketplace-sourced sales; fulfilling an order draws
                  down stock through the same audit trail.
  • Customers   — a light CRM.
  • Service     — work orders / bookings for the service side of the business.
  • Dashboard   — one aggregate call ("state of the business") for the human
                  glance and for an agent deciding what to do next.

Live pricing and marketplace (eBay, …) integration live in the sibling modules
``commerce_pricing_capabilities.py`` and ``commerce_platforms.py``. Everything
is a plain ``@capability`` so the agent loop can drive the shop end-to-end; the
panel (``commerce_panel.html``) is the human oversight surface.

Storage: the shared Data-Fabric SQLite db (``_sqlite_conn``), same as markets.
Fresh WAL connections are opened per operation inside ``run_in_executor`` so we
never block the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path as _Path
from typing import Dict, List, Optional

log = logging.getLogger("vera.commerce")

try:
    from Vera.vera.capability_orchestration import (
        APP, capability, emit_event, now_iso, register_ui, enum_schema,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.commerce").warning("commerce caps unavailable: %s", e)
    _CAP_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

ORDER_STATUSES   = ["new", "paid", "packed", "shipped", "delivered",
                    "cancelled", "refunded"]
SERVICE_STATUSES = ["new", "scheduled", "in_progress", "done", "cancelled"]
STOCK_REASONS    = ["restock", "sale", "return", "adjust", "damage", "sync"]

# Statuses that count as "open" (still need attention) for the dashboard.
_OPEN_ORDER = ("new", "paid", "packed")
_OPEN_SERVICE = ("new", "scheduled", "in_progress")

_SCHEMA_READY = False


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"

def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def _i(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

def _json_load(v, default):
    if v is None or v == "":
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default

async def _run(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

def _add_col(conn, table: str, col: str, decl: str):
    """Add a column to an existing table if missing (SQLite has no IF NOT EXISTS)."""
    try:
        have = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    except Exception:
        pass

def _ensure_schema_sync():
    global _SCHEMA_READY
    conn = _sqlite_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS commerce_products (
                id            TEXT PRIMARY KEY,
                sku           TEXT,
                name          TEXT,
                description   TEXT,
                category      TEXT,
                cost          REAL DEFAULT 0,
                price         REAL DEFAULT 0,
                currency      TEXT DEFAULT 'USD',
                qty_on_hand   INTEGER DEFAULT 0,
                reorder_point INTEGER DEFAULT 0,
                reorder_qty   INTEGER DEFAULT 0,
                location      TEXT,
                supplier      TEXT,
                upc           TEXT,
                store_id      TEXT,
                attributes    TEXT,
                created_at    TEXT,
                updated_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_commerce_products_sku ON commerce_products(sku);

            CREATE TABLE IF NOT EXISTS commerce_stock_moves (
                id          TEXT PRIMARY KEY,
                product_id  TEXT,
                delta       INTEGER,
                reason      TEXT,
                ref         TEXT,
                note        TEXT,
                ts          TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_commerce_moves_pid ON commerce_stock_moves(product_id);

            CREATE TABLE IF NOT EXISTS commerce_customers (
                id          TEXT PRIMARY KEY,
                name        TEXT,
                email       TEXT,
                phone       TEXT,
                address     TEXT,
                tags        TEXT,
                notes       TEXT,
                created_at  TEXT,
                updated_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS commerce_orders (
                id           TEXT PRIMARY KEY,
                source       TEXT DEFAULT 'manual',
                external_id  TEXT,
                customer_id  TEXT,
                store_id     TEXT,
                status       TEXT DEFAULT 'new',
                items        TEXT,
                subtotal     REAL DEFAULT 0,
                shipping     REAL DEFAULT 0,
                total        REAL DEFAULT 0,
                currency     TEXT DEFAULT 'USD',
                tracking     TEXT,
                notes        TEXT,
                placed_at    TEXT,
                updated_at   TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_commerce_orders_src ON commerce_orders(source, external_id);

            CREATE TABLE IF NOT EXISTS commerce_service_jobs (
                id           TEXT PRIMARY KEY,
                title        TEXT,
                customer_id  TEXT,
                status       TEXT DEFAULT 'new',
                assigned_to  TEXT,
                scheduled_at TEXT,
                line_items   TEXT,
                notes        TEXT,
                created_at   TEXT,
                updated_at   TEXT
            );
        """)
        # self-heal store scoping on pre-existing databases. MUST run before the
        # store_id indexes: on an old DB whose tables predate the column, an
        # index on store_id inside the executescript above would abort the whole
        # script with "no such column: store_id" and the heal would never run.
        _add_col(conn, "commerce_products", "store_id", "TEXT")
        _add_col(conn, "commerce_orders", "store_id", "TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_commerce_orders_store "
                     "ON commerce_orders(store_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_commerce_products_store "
                     "ON commerce_products(store_id)")
        conn.commit()
    finally:
        conn.close()
    _SCHEMA_READY = True

async def _ensure_schema():
    if not _SCHEMA_READY:
        await _run(_ensure_schema_sync)


# ─────────────────────────────────────────────────────────────────────────────
# Product store (sync — always called inside _run / executor)
# ─────────────────────────────────────────────────────────────────────────────

_PRODUCT_COLS = ("id", "sku", "name", "description", "category", "cost", "price",
                 "currency", "qty_on_hand", "reorder_point", "reorder_qty",
                 "location", "supplier", "upc", "store_id", "attributes", "created_at",
                 "updated_at")

def _prod_out(row) -> dict:
    d = dict(row)
    d["attributes"] = _json_load(d.get("attributes"), {})
    return d

def _db_list_products(q: str = "", category: str = "", limit: int = 500,
                      store_id: str = "") -> List[dict]:
    conn = _sqlite_conn()
    try:
        sql = "SELECT * FROM commerce_products"
        where, args = [], []
        if q:
            where.append("(sku LIKE ? OR name LIKE ? OR upc LIKE ?)")
            like = f"%{q}%"; args += [like, like, like]
        if category:
            where.append("category = ?"); args.append(category)
        if store_id:
            where.append("store_id = ?"); args.append(store_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY name LIMIT ?"; args.append(int(limit))
        return [_prod_out(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()

def _db_get_product(pid: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM commerce_products WHERE id=? OR sku=?",
                         (pid, pid)).fetchone()
        return _prod_out(r) if r else None
    finally:
        conn.close()

def _db_upsert_product(fields: dict) -> dict:
    conn = _sqlite_conn()
    try:
        pid = fields.get("id") or ""
        existing = None
        if pid:
            existing = conn.execute("SELECT * FROM commerce_products WHERE id=?",
                                    (pid,)).fetchone()
        if not existing and fields.get("sku"):
            existing = conn.execute("SELECT * FROM commerce_products WHERE sku=?",
                                    (fields["sku"],)).fetchone()
        base = _prod_out(existing) if existing else {}
        pid = base.get("id") or pid or _new_id("prod")
        merged = {**base, **{k: v for k, v in fields.items() if v is not None}}
        now = now_iso()
        row = {
            "id": pid,
            "sku": merged.get("sku", ""),
            "name": merged.get("name", ""),
            "description": merged.get("description", ""),
            "category": merged.get("category", ""),
            "cost": _f(merged.get("cost", 0)),
            "price": _f(merged.get("price", 0)),
            "currency": merged.get("currency", "USD") or "USD",
            "qty_on_hand": _i(merged.get("qty_on_hand", 0)),
            "reorder_point": _i(merged.get("reorder_point", 0)),
            "reorder_qty": _i(merged.get("reorder_qty", 0)),
            "location": merged.get("location", ""),
            "supplier": merged.get("supplier", ""),
            "upc": merged.get("upc", ""),
            "store_id": merged.get("store_id", "") or "",
            "attributes": json.dumps(merged.get("attributes") or {}),
            "created_at": base.get("created_at") or now,
            "updated_at": now,
        }
        conn.execute(
            "INSERT OR REPLACE INTO commerce_products "
            "(id,sku,name,description,category,cost,price,currency,qty_on_hand,"
            " reorder_point,reorder_qty,location,supplier,upc,store_id,attributes,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(row[c] for c in _PRODUCT_COLS))
        conn.commit()
        row["attributes"] = merged.get("attributes") or {}
        return row
    finally:
        conn.close()

def _db_delete_product(pid: str) -> bool:
    conn = _sqlite_conn()
    try:
        cur = conn.execute("DELETE FROM commerce_products WHERE id=? OR sku=?",
                           (pid, pid))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()

def _db_adjust_stock(pid: str, delta: int, reason: str, ref: str = "",
                     note: str = "") -> Optional[dict]:
    """Atomically write a stock move and update qty_on_hand. Returns the
    product row (updated) or None if the product is missing."""
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM commerce_products WHERE id=? OR sku=?",
                         (pid, pid)).fetchone()
        if not r:
            return None
        prod = dict(r)
        new_qty = _i(prod.get("qty_on_hand", 0)) + int(delta)
        now = now_iso()
        conn.execute(
            "INSERT INTO commerce_stock_moves (id,product_id,delta,reason,ref,note,ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (_new_id("mv"), prod["id"], int(delta), reason, ref, note, now))
        conn.execute("UPDATE commerce_products SET qty_on_hand=?, updated_at=? WHERE id=?",
                     (new_qty, now, prod["id"]))
        conn.commit()
        prod["qty_on_hand"] = new_qty
        return _prod_out(prod)
    finally:
        conn.close()

def _db_stock_moves(pid: str = "", limit: int = 200) -> List[dict]:
    conn = _sqlite_conn()
    try:
        if pid:
            rows = conn.execute(
                "SELECT * FROM commerce_stock_moves WHERE product_id=? ORDER BY ts DESC LIMIT ?",
                (pid, int(limit))).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM commerce_stock_moves ORDER BY ts DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def _db_low_stock() -> List[dict]:
    conn = _sqlite_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM commerce_products WHERE qty_on_hand <= reorder_point "
            "AND (reorder_point > 0 OR qty_on_hand <= 0) ORDER BY qty_on_hand").fetchall()
        return [_prod_out(r) for r in rows]
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Customer store
# ─────────────────────────────────────────────────────────────────────────────

def _cust_out(row) -> dict:
    d = dict(row)
    d["address"] = _json_load(d.get("address"), {})
    d["tags"] = _json_load(d.get("tags"), [])
    return d

def _db_list_customers(q: str = "", limit: int = 500) -> List[dict]:
    conn = _sqlite_conn()
    try:
        if q:
            like = f"%{q}%"
            rows = conn.execute(
                "SELECT * FROM commerce_customers WHERE name LIKE ? OR email LIKE ? "
                "ORDER BY name LIMIT ?", (like, like, int(limit))).fetchall()
        else:
            rows = conn.execute("SELECT * FROM commerce_customers ORDER BY name LIMIT ?",
                                (int(limit),)).fetchall()
        return [_cust_out(r) for r in rows]
    finally:
        conn.close()

def _db_get_customer(cid: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM commerce_customers WHERE id=?", (cid,)).fetchone()
        return _cust_out(r) if r else None
    finally:
        conn.close()

def _db_upsert_customer(fields: dict) -> dict:
    conn = _sqlite_conn()
    try:
        cid = fields.get("id") or ""
        existing = conn.execute("SELECT * FROM commerce_customers WHERE id=?",
                                (cid,)).fetchone() if cid else None
        base = _cust_out(existing) if existing else {}
        cid = base.get("id") or cid or _new_id("cust")
        merged = {**base, **{k: v for k, v in fields.items() if v is not None}}
        now = now_iso()
        conn.execute(
            "INSERT OR REPLACE INTO commerce_customers "
            "(id,name,email,phone,address,tags,notes,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, merged.get("name", ""), merged.get("email", ""),
             merged.get("phone", ""), json.dumps(merged.get("address") or {}),
             json.dumps(merged.get("tags") or []), merged.get("notes", ""),
             base.get("created_at") or now, now))
        conn.commit()
        return _db_get_customer(cid) or {"id": cid}
    finally:
        conn.close()

def _db_delete_customer(cid: str) -> bool:
    conn = _sqlite_conn()
    try:
        cur = conn.execute("DELETE FROM commerce_customers WHERE id=?", (cid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Order store
# ─────────────────────────────────────────────────────────────────────────────

def _order_out(row) -> dict:
    d = dict(row)
    d["items"] = _json_load(d.get("items"), [])
    return d

def _db_list_orders(status: str = "", source: str = "", limit: int = 200,
                    store_id: str = "") -> List[dict]:
    conn = _sqlite_conn()
    try:
        sql = "SELECT * FROM commerce_orders"; where, args = [], []
        if status:
            where.append("status=?"); args.append(status)
        if source:
            where.append("source=?"); args.append(source)
        if store_id:
            where.append("store_id=?"); args.append(store_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(placed_at, updated_at) DESC LIMIT ?"; args.append(int(limit))
        return [_order_out(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()

def _db_get_order(oid: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM commerce_orders WHERE id=?", (oid,)).fetchone()
        return _order_out(r) if r else None
    finally:
        conn.close()

def _db_find_order_by_external(source: str, external_id: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM commerce_orders WHERE source=? AND external_id=?",
                         (source, external_id)).fetchone()
        return _order_out(r) if r else None
    finally:
        conn.close()

def _db_upsert_order(fields: dict) -> dict:
    conn = _sqlite_conn()
    try:
        oid = fields.get("id") or ""
        existing = conn.execute("SELECT * FROM commerce_orders WHERE id=?",
                                (oid,)).fetchone() if oid else None
        if not existing and fields.get("source") and fields.get("external_id"):
            existing = conn.execute(
                "SELECT * FROM commerce_orders WHERE source=? AND external_id=?",
                (fields["source"], fields["external_id"])).fetchone()
        base = _order_out(existing) if existing else {}
        oid = base.get("id") or oid or _new_id("ord")
        merged = {**base, **{k: v for k, v in fields.items() if v is not None}}
        items = merged.get("items") or []
        subtotal = merged.get("subtotal")
        if subtotal is None:
            subtotal = sum(_f(it.get("unit_price")) * _i(it.get("qty", 1)) for it in items)
        shipping = _f(merged.get("shipping", 0))
        total = merged.get("total")
        if total is None:
            total = _f(subtotal) + shipping
        now = now_iso()
        conn.execute(
            "INSERT OR REPLACE INTO commerce_orders "
            "(id,source,external_id,customer_id,store_id,status,items,subtotal,shipping,total,"
            " currency,tracking,notes,placed_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, merged.get("source", "manual"), merged.get("external_id", ""),
             merged.get("customer_id", ""), merged.get("store_id", "") or "",
             merged.get("status", "new"),
             json.dumps(items), _f(subtotal), shipping, _f(total),
             merged.get("currency", "USD") or "USD", merged.get("tracking", ""),
             merged.get("notes", ""), merged.get("placed_at") or base.get("placed_at") or now, now))
        conn.commit()
        return _db_get_order(oid) or {"id": oid}
    finally:
        conn.close()

def _db_set_order_status(oid: str, status: str, tracking: str = "") -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT id FROM commerce_orders WHERE id=?", (oid,)).fetchone()
        if not r:
            return None
        if tracking:
            conn.execute("UPDATE commerce_orders SET status=?, tracking=?, updated_at=? WHERE id=?",
                         (status, tracking, now_iso(), oid))
        else:
            conn.execute("UPDATE commerce_orders SET status=?, updated_at=? WHERE id=?",
                         (status, now_iso(), oid))
        conn.commit()
        return _db_get_order(oid)
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Service-job store
# ─────────────────────────────────────────────────────────────────────────────

def _svc_out(row) -> dict:
    d = dict(row)
    d["line_items"] = _json_load(d.get("line_items"), [])
    return d

def _db_list_service(status: str = "", limit: int = 200) -> List[dict]:
    conn = _sqlite_conn()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM commerce_service_jobs WHERE status=? "
                "ORDER BY COALESCE(scheduled_at, created_at) DESC LIMIT ?",
                (status, int(limit))).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM commerce_service_jobs "
                "ORDER BY COALESCE(scheduled_at, created_at) DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [_svc_out(r) for r in rows]
    finally:
        conn.close()

def _db_get_service(sid: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM commerce_service_jobs WHERE id=?", (sid,)).fetchone()
        return _svc_out(r) if r else None
    finally:
        conn.close()

def _db_upsert_service(fields: dict) -> dict:
    conn = _sqlite_conn()
    try:
        sid = fields.get("id") or ""
        existing = conn.execute("SELECT * FROM commerce_service_jobs WHERE id=?",
                                (sid,)).fetchone() if sid else None
        base = _svc_out(existing) if existing else {}
        sid = base.get("id") or sid or _new_id("svc")
        merged = {**base, **{k: v for k, v in fields.items() if v is not None}}
        now = now_iso()
        conn.execute(
            "INSERT OR REPLACE INTO commerce_service_jobs "
            "(id,title,customer_id,status,assigned_to,scheduled_at,line_items,notes,"
            " created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, merged.get("title", ""), merged.get("customer_id", ""),
             merged.get("status", "new"), merged.get("assigned_to", ""),
             merged.get("scheduled_at", ""), json.dumps(merged.get("line_items") or []),
             merged.get("notes", ""), base.get("created_at") or now, now))
        conn.commit()
        return _db_get_service(sid) or {"id": sid}
    finally:
        conn.close()

def _db_set_service_status(sid: str, status: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT id FROM commerce_service_jobs WHERE id=?", (sid,)).fetchone()
        if not r:
            return None
        conn.execute("UPDATE commerce_service_jobs SET status=?, updated_at=? WHERE id=?",
                     (status, now_iso(), sid))
        conn.commit()
        return _db_get_service(sid)
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard aggregate
# ─────────────────────────────────────────────────────────────────────────────

def _db_dashboard() -> dict:
    conn = _sqlite_conn()
    try:
        prod = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(cost*qty_on_hand),0) invval, "
            "COALESCE(SUM(price*qty_on_hand),0) retval FROM commerce_products").fetchone()
        low = conn.execute(
            "SELECT COUNT(*) FROM commerce_products "
            "WHERE qty_on_hand <= reorder_point AND (reorder_point > 0 OR qty_on_hand <= 0)").fetchone()
        open_orders = conn.execute(
            "SELECT COUNT(*) FROM commerce_orders WHERE status IN (?,?,?)",
            _OPEN_ORDER).fetchone()
        cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat() + "Z"
        rev = conn.execute(
            "SELECT COALESCE(SUM(total),0) FROM commerce_orders "
            "WHERE placed_at >= ? AND status NOT IN ('cancelled','refunded')",
            (cutoff,)).fetchone()
        open_svc = conn.execute(
            "SELECT COUNT(*) FROM commerce_service_jobs WHERE status IN (?,?,?)",
            _OPEN_SERVICE).fetchone()
        cust = conn.execute("SELECT COUNT(*) FROM commerce_customers").fetchone()
        return {
            "products": _i(prod[0]),
            "inventory_cost_value": round(_f(prod[1]), 2),
            "inventory_retail_value": round(_f(prod[2]), 2),
            "low_stock": _i(low[0]),
            "open_orders": _i(open_orders[0]),
            "revenue_30d": round(_f(rev[0]), 2),
            "open_service_jobs": _i(open_svc[0]),
            "customers": _i(cust[0]),
        }
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    # ── Inventory ──────────────────────────────────────────────────────────────

    @capability(
        "business.product.list", http_method="GET", http_path="/business/product/list",
        http_tags=["commerce"], memory="off", silent=True,
        description="List inventory products. Input: q (str — match sku/name/upc), "
                    "category (str), store_id (str — scope to one store), limit (int "
                    "default 500). Output: {products:[...], count}.")
    async def cap_product_list(q: str = "", category: str = "", limit: int = 500,
                               store_id: str = "", trace_id=None):
        await _ensure_schema()
        items = await _run(_db_list_products, q, category, int(limit), store_id)
        return {"products": items, "count": len(items)}

    @capability(
        "business.product.get", http_method="GET", http_path="/business/product/get",
        http_tags=["commerce"], memory="off", silent=True,
        description="Get one product by id or SKU. Input: id (str!). Output: {product} or {error}.")
    async def cap_product_get(id: str = "", trace_id=None):
        await _ensure_schema()
        if not id:
            return {"error": "id (or sku) required"}
        p = await _run(_db_get_product, id)
        return {"product": p} if p else {"error": "not found"}

    @capability(
        "business.product.upsert", http_method="POST", http_path="/business/product/upsert",
        http_tags=["commerce"],
        description="Create or update a product. Matches on id, else on sku. "
                    "Input: id (str — omit to create), sku, name, description, category, "
                    "cost (float), price (float), currency, qty_on_hand (int), reorder_point (int), "
                    "reorder_qty (int), location, supplier, upc, store_id (str — which "
                    "store this belongs to), attributes (dict). Output: {ok, product}.")
    async def cap_product_upsert(
        id: str = "", sku: str = "", name: str = "", description: str = "",
        category: str = "", cost: float = 0.0, price: float = 0.0, currency: str = "USD",
        qty_on_hand: int = 0, reorder_point: int = 0, reorder_qty: int = 0,
        location: str = "", supplier: str = "", upc: str = "", store_id: str = "",
        attributes: Dict = None, trace_id=None):
        await _ensure_schema()
        if not (sku or name or id):
            return {"error": "sku or name required"}
        fields = {"id": id or None, "sku": sku or None, "name": name or None,
                  "description": description or None, "category": category or None,
                  "cost": cost, "price": price, "currency": currency,
                  "qty_on_hand": qty_on_hand, "reorder_point": reorder_point,
                  "reorder_qty": reorder_qty, "location": location or None,
                  "supplier": supplier or None, "upc": upc or None,
                  "store_id": store_id or None, "attributes": attributes}
        p = await _run(_db_upsert_product, fields)
        await emit_event({"type": "commerce.progress", "stage": "product",
                          "message": f"saved product {p.get('sku') or p.get('name')}"})
        return {"ok": True, "product": p}

    @capability(
        "business.product.delete", http_method="POST", http_path="/business/product/delete",
        http_tags=["commerce"],
        description="Delete a product by id or sku. Input: id (str!). Output: {ok} or {error}.")
    async def cap_product_delete(id: str = "", trace_id=None):
        await _ensure_schema()
        if not id:
            return {"error": "id required"}
        ok = await _run(_db_delete_product, id)
        return {"ok": ok} if ok else {"error": "not found"}

    @capability(
        "business.stock.adjust", http_method="POST", http_path="/business/stock/adjust",
        http_tags=["commerce"],
        schema=enum_schema(reason=STOCK_REASONS),
        description="Adjust a product's stock by a signed delta and record an audit move. "
                    "Input: id (str! — product id or sku), delta (int! — +restock/-sale), "
                    "reason (str), ref (str — e.g. order id), note (str). "
                    "Output: {ok, product} with the new qty_on_hand.")
    async def cap_stock_adjust(id: str = "", delta: int = 0, reason: str = "adjust",
                               ref: str = "", note: str = "", trace_id=None):
        await _ensure_schema()
        if not id:
            return {"error": "id required"}
        if int(delta) == 0:
            return {"error": "delta must be non-zero"}
        p = await _run(_db_adjust_stock, id, int(delta), reason or "adjust", ref, note)
        if not p:
            return {"error": "product not found"}
        await emit_event({"type": "commerce.progress", "stage": "stock",
                          "message": f"{p.get('sku') or p.get('name')} {int(delta):+d} → {p['qty_on_hand']}"})
        return {"ok": True, "product": p}

    @capability(
        "business.stock.moves", http_method="GET", http_path="/business/stock/moves",
        http_tags=["commerce"], memory="off", silent=True,
        description="Stock-movement audit trail. Input: product_id (str — omit for all), "
                    "limit (int default 200). Output: {moves:[...], count}.")
    async def cap_stock_moves(product_id: str = "", limit: int = 200, trace_id=None):
        await _ensure_schema()
        moves = await _run(_db_stock_moves, product_id, int(limit))
        return {"moves": moves, "count": len(moves)}

    @capability(
        "business.product.low_stock", http_method="GET",
        http_path="/business/product/low_stock", http_tags=["commerce"],
        memory="off", silent=True,
        description="Products at or below their reorder point (need restocking). "
                    "Output: {products:[...], count}.")
    async def cap_low_stock(trace_id=None):
        await _ensure_schema()
        items = await _run(_db_low_stock)
        return {"products": items, "count": len(items)}

    # ── Orders ─────────────────────────────────────────────────────────────────

    @capability(
        "business.order.list", http_method="GET", http_path="/business/order/list",
        http_tags=["commerce"], memory="off", silent=True,
        schema=enum_schema(status=ORDER_STATUSES),
        description="List orders. Input: status (str), source (str: manual|ebay|vinted), "
                    "store_id (str — scope to one store), limit (int default 200). "
                    "Output: {orders:[...], count}.")
    async def cap_order_list(status: str = "", source: str = "", limit: int = 200,
                             store_id: str = "", trace_id=None):
        await _ensure_schema()
        items = await _run(_db_list_orders, status, source, int(limit), store_id)
        return {"orders": items, "count": len(items)}

    @capability(
        "business.order.get", http_method="GET", http_path="/business/order/get",
        http_tags=["commerce"], memory="off", silent=True,
        description="Get one order by id. Input: id (str!). Output: {order} or {error}.")
    async def cap_order_get(id: str = "", trace_id=None):
        await _ensure_schema()
        if not id:
            return {"error": "id required"}
        o = await _run(_db_get_order, id)
        return {"order": o} if o else {"error": "not found"}

    @capability(
        "business.order.upsert", http_method="POST", http_path="/business/order/upsert",
        http_tags=["commerce"],
        schema=enum_schema(status=ORDER_STATUSES),
        description="Create or update an order. When creating with deduct_stock=true, each "
                    "line item draws down inventory (a 'sale' stock move per item). "
                    "Input: id (str — omit to create), source, external_id, customer_id, "
                    "status, items (list of {product_id|sku, qty, unit_price, name}), "
                    "shipping (float), currency, tracking, notes, store_id (str — which "
                    "store this sale belongs to), deduct_stock (bool default true on create). "
                    "Output: {ok, order}.")
    async def cap_order_upsert(
        id: str = "", source: str = "manual", external_id: str = "", customer_id: str = "",
        status: str = "new", items: List = None, shipping: float = 0.0, currency: str = "USD",
        tracking: str = "", notes: str = "", store_id: str = "", deduct_stock: bool = True,
        trace_id=None):
        await _ensure_schema()
        items = items or []
        is_new = not (await _run(_db_get_order, id)) if id else True
        fields = {"id": id or None, "source": source, "external_id": external_id or None,
                  "customer_id": customer_id or None, "status": status, "items": items,
                  "shipping": shipping, "currency": currency, "tracking": tracking or None,
                  "notes": notes or None, "store_id": store_id or None}
        order = await _run(_db_upsert_order, fields)
        # Draw down stock for freshly created orders only (avoid double-decrement on edits).
        if is_new and deduct_stock:
            for it in items:
                ref = it.get("product_id") or it.get("sku")
                qty = _i(it.get("qty", 1))
                if ref and qty:
                    await _run(_db_adjust_stock, ref, -abs(qty), "sale", order["id"],
                               f"order {order['id']}")
            order = await _run(_db_get_order, order["id"])
        await emit_event({"type": "commerce.progress", "stage": "order",
                          "message": f"order {order['id']} {status} (${order.get('total')})"})
        return {"ok": True, "order": order}

    @capability(
        "business.order.set_status", http_method="POST", http_path="/business/order/set_status",
        http_tags=["commerce"],
        schema=enum_schema(status=ORDER_STATUSES),
        description="Set an order's status (and optional tracking number). "
                    "Input: id (str!), status (str!), tracking (str). Output: {ok, order}.")
    async def cap_order_set_status(id: str = "", status: str = "", tracking: str = "", trace_id=None):
        await _ensure_schema()
        if not (id and status):
            return {"error": "id and status required"}
        o = await _run(_db_set_order_status, id, status, tracking)
        return {"ok": True, "order": o} if o else {"error": "not found"}

    # ── Customers ──────────────────────────────────────────────────────────────

    @capability(
        "business.customer.list", http_method="GET", http_path="/business/customer/list",
        http_tags=["commerce"], memory="off", silent=True,
        description="List customers. Input: q (str — name/email match), limit (int). "
                    "Output: {customers:[...], count}.")
    async def cap_customer_list(q: str = "", limit: int = 500, trace_id=None):
        await _ensure_schema()
        items = await _run(_db_list_customers, q, int(limit))
        return {"customers": items, "count": len(items)}

    @capability(
        "business.customer.get", http_method="GET", http_path="/business/customer/get",
        http_tags=["commerce"], memory="off", silent=True,
        description="Get one customer by id. Input: id (str!). Output: {customer} or {error}.")
    async def cap_customer_get(id: str = "", trace_id=None):
        await _ensure_schema()
        if not id:
            return {"error": "id required"}
        c = await _run(_db_get_customer, id)
        return {"customer": c} if c else {"error": "not found"}

    @capability(
        "business.customer.upsert", http_method="POST", http_path="/business/customer/upsert",
        http_tags=["commerce"],
        description="Create or update a customer. Input: id (str — omit to create), name, "
                    "email, phone, address (dict), tags (list), notes. Output: {ok, customer}.")
    async def cap_customer_upsert(
        id: str = "", name: str = "", email: str = "", phone: str = "",
        address: Dict = None, tags: List = None, notes: str = "", trace_id=None):
        await _ensure_schema()
        if not (name or email or id):
            return {"error": "name or email required"}
        fields = {"id": id or None, "name": name or None, "email": email or None,
                  "phone": phone or None, "address": address, "tags": tags,
                  "notes": notes or None}
        c = await _run(_db_upsert_customer, fields)
        return {"ok": True, "customer": c}

    @capability(
        "business.customer.delete", http_method="POST", http_path="/business/customer/delete",
        http_tags=["commerce"],
        description="Delete a customer by id. Input: id (str!). Output: {ok} or {error}.")
    async def cap_customer_delete(id: str = "", trace_id=None):
        await _ensure_schema()
        if not id:
            return {"error": "id required"}
        ok = await _run(_db_delete_customer, id)
        return {"ok": ok} if ok else {"error": "not found"}

    # ── Service jobs ───────────────────────────────────────────────────────────

    @capability(
        "business.service.list", http_method="GET", http_path="/business/service/list",
        http_tags=["commerce"], memory="off", silent=True,
        schema=enum_schema(status=SERVICE_STATUSES),
        description="List service jobs / work orders. Input: status (str), limit (int). "
                    "Output: {jobs:[...], count}.")
    async def cap_service_list(status: str = "", limit: int = 200, trace_id=None):
        await _ensure_schema()
        items = await _run(_db_list_service, status, int(limit))
        return {"jobs": items, "count": len(items)}

    @capability(
        "business.service.get", http_method="GET", http_path="/business/service/get",
        http_tags=["commerce"], memory="off", silent=True,
        description="Get one service job by id. Input: id (str!). Output: {job} or {error}.")
    async def cap_service_get(id: str = "", trace_id=None):
        await _ensure_schema()
        if not id:
            return {"error": "id required"}
        j = await _run(_db_get_service, id)
        return {"job": j} if j else {"error": "not found"}

    @capability(
        "business.service.upsert", http_method="POST", http_path="/business/service/upsert",
        http_tags=["commerce"],
        schema=enum_schema(status=SERVICE_STATUSES),
        description="Create or update a service job / work order. Input: id (str — omit to "
                    "create), title, customer_id, status, assigned_to (agent or person), "
                    "scheduled_at (ISO), line_items (list), notes. Output: {ok, job}.")
    async def cap_service_upsert(
        id: str = "", title: str = "", customer_id: str = "", status: str = "new",
        assigned_to: str = "", scheduled_at: str = "", line_items: List = None,
        notes: str = "", trace_id=None):
        await _ensure_schema()
        if not (title or id):
            return {"error": "title required"}
        fields = {"id": id or None, "title": title or None, "customer_id": customer_id or None,
                  "status": status, "assigned_to": assigned_to or None,
                  "scheduled_at": scheduled_at or None, "line_items": line_items,
                  "notes": notes or None}
        j = await _run(_db_upsert_service, fields)
        return {"ok": True, "job": j}

    @capability(
        "business.service.set_status", http_method="POST",
        http_path="/business/service/set_status", http_tags=["commerce"],
        schema=enum_schema(status=SERVICE_STATUSES),
        description="Set a service job's status. Input: id (str!), status (str!). "
                    "Output: {ok, job}.")
    async def cap_service_set_status(id: str = "", status: str = "", trace_id=None):
        await _ensure_schema()
        if not (id and status):
            return {"error": "id and status required"}
        j = await _run(_db_set_service_status, id, status)
        return {"ok": True, "job": j} if j else {"error": "not found"}

    # ── Dashboard ──────────────────────────────────────────────────────────────

    @capability(
        "business.store.dashboard", http_method="GET", http_path="/business/store/dashboard",
        http_tags=["commerce"], memory="off", silent=True,
        description="Store snapshot (optionally one store via store_id): inventory value, "
                    "low-stock count, open orders, "
                    "30-day revenue, open service jobs, customers, connected platforms. "
                    "Output: {...counts, platforms:[...]}.")
    async def cap_dashboard(trace_id=None):
        await _ensure_schema()
        d = await _run(_db_dashboard)
        # Fold in connected platforms if the platforms module is loaded.
        import sys
        plat = sys.modules.get("commerce_platforms")
        if plat and hasattr(plat, "_db_list_accounts"):
            try:
                accts = await _run(plat._db_list_accounts)
                d["platforms"] = [{"id": a["id"], "connector": a["connector"],
                                   "label": a.get("label"), "status": a.get("status")}
                                  for a in accts]
            except Exception:
                d["platforms"] = []
        else:
            d["platforms"] = []
        return d

    # ── Panel ──────────────────────────────────────────────────────────────────

    _HERE = _Path(__file__).parent

    @APP.get("/commerce/panel", include_in_schema=False)
    async def _commerce_panel_route():
        from fastapi.responses import HTMLResponse
        p = _HERE / "commerce_panel.html"
        if p.exists():
            return HTMLResponse(p.read_text(encoding="utf-8"))
        return HTMLResponse("<p style='color:red'>commerce_panel.html not found</p>")

    # NOTE: the standalone "Shop" tab was merged into the Business panel as a
    # sub-tab (see business_capabilities.py register_ui). The /commerce/panel
    # route above is still served — the Business panel iframes it — but we no
    # longer register a separate top-level tab here, so it doesn't appear twice.
    # (The commerce ui_caps now travel with the Business tab.)

    log.info("commerce: core ready (inventory / orders / customers / service / dashboard) — UI folded into Business tab")
