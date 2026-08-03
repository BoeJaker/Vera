"""
commerce_uk_tax.py — Profit engine + UK tax for the reselling operation
=======================================================================

Turns raw orders into money you can reason about, at every altitude:

  • **Per transaction** — for one order, split revenue into COGS (item cost),
    platform fees, postage cost, packaging and "other", and land on a net
    profit + margin. Costs are pulled automatically (product cost for COGS, a
    per-platform fee schedule, the booked shipment's postage) and can be
    overridden per order with ``commerce.profit.record``.
  • **Any range / whole business** — ``commerce.profit.report`` rolls those up
    over a date range, a UK tax year, or all-time, grouped by platform.
  • **Tax** — ``commerce.tax.summary`` computes, for a UK tax year (6 Apr →
    5 Apr), gross trading income, allowable expenses (or the £1,000 trading
    allowance if that's better), taxable profit, estimated Income Tax (with the
    personal-allowance taper), Class 4 NIC, and a VAT estimate if registered —
    plus a recommended amount to set aside.

Nothing here files anything with HMRC; it's a book-keeping + estimation surface
so the operator (and the agents) always know the real profit and the tax owed.
All money is GBP. Storage is the shared Data-Fabric SQLite db.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("vera.commerce.tax")

try:
    from Vera.vera.capability_orchestration import (
        capability, emit_event, now_iso, enum_schema,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.commerce.tax").warning("commerce tax unavailable: %s", e)
    _CAP_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# UK tax constants (thresholds frozen to 2027/28). Keyed by the tax-year start
# year (e.g. 2024 == 2024/25). ``_bands_for`` falls back to the latest known.
# ─────────────────────────────────────────────────────────────────────────────

UK_TAX_YEARS = {
    2023: {"personal_allowance": 12570, "basic_limit": 37700, "higher_limit": 125140,
           "basic_rate": 0.20, "higher_rate": 0.40, "additional_rate": 0.45,
           "pa_taper_start": 100000, "trading_allowance": 1000,
           "c4_lower": 12570, "c4_upper": 50270, "c4_main": 0.09, "c4_upper_rate": 0.02,
           "vat_threshold": 85000, "vat_rate": 0.20},
    2024: {"personal_allowance": 12570, "basic_limit": 37700, "higher_limit": 125140,
           "basic_rate": 0.20, "higher_rate": 0.40, "additional_rate": 0.45,
           "pa_taper_start": 100000, "trading_allowance": 1000,
           "c4_lower": 12570, "c4_upper": 50270, "c4_main": 0.06, "c4_upper_rate": 0.02,
           "vat_threshold": 90000, "vat_rate": 0.20},
    2025: {"personal_allowance": 12570, "basic_limit": 37700, "higher_limit": 125140,
           "basic_rate": 0.20, "higher_rate": 0.40, "additional_rate": 0.45,
           "pa_taper_start": 100000, "trading_allowance": 1000,
           "c4_lower": 12570, "c4_upper": 50270, "c4_main": 0.06, "c4_upper_rate": 0.02,
           "vat_threshold": 90000, "vat_rate": 0.20},
}

DEFAULT_SETTINGS = {
    "business_type": "sole_trader",       # sole_trader | ltd (ltd = informational only)
    "vat_registered": False,
    "vat_scheme": "standard",             # standard | flat_rate | margin
    "flat_rate_pct": 0.075,               # used only when vat_scheme == flat_rate
    "use_trading_allowance": "auto",      # auto | yes | no
    "other_taxable_income": 0.0,          # employment/other income in the tax year
    "accounting_basis": "cash",           # cash | accrual (informational)
    "packaging_default": 0.30,            # £ assumed per order if not recorded
    "set_aside_buffer_pct": 0.05,         # extra % on top of computed tax to reserve
    "platform_fees": {                    # final-value fee schedule per platform
        "ebay":   {"pct": 0.128, "fixed": 0.30},   # business FVF (editable by category)
        "vinted": {"pct": 0.0,   "fixed": 0.0},    # seller pays nothing on Vinted
        "manual": {"pct": 0.0,   "fixed": 0.0},
    },
}

_SETTINGS_ID = "settings"
_SCHEMA_READY = False


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def _i(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

def _new_id(prefix): return f"{prefix}_{uuid.uuid4().hex[:12]}"

async def _run(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

def _core():
    return sys.modules.get("commerce_capabilities")


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_schema_sync():
    global _SCHEMA_READY
    conn = _sqlite_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS commerce_order_costs (
                order_id       TEXT PRIMARY KEY,
                cogs           REAL,
                platform_fee   REAL,
                postage_cost   REAL,
                packaging_cost REAL,
                other_cost     REAL,
                fee_auto       INTEGER DEFAULT 1,
                cogs_auto      INTEGER DEFAULT 1,
                notes          TEXT,
                updated_at     TEXT
            );
            CREATE TABLE IF NOT EXISTS commerce_tax_settings (
                id      TEXT PRIMARY KEY,
                data    TEXT,
                updated_at TEXT
            );
        """)
        conn.commit()
    finally:
        conn.close()
    _SCHEMA_READY = True

async def _ensure_schema():
    if not _SCHEMA_READY:
        await _run(_ensure_schema_sync)


# ─────────────────────────────────────────────────────────────────────────────
# Settings store
# ─────────────────────────────────────────────────────────────────────────────

def _db_get_settings() -> dict:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT data FROM commerce_tax_settings WHERE id=?",
                         (_SETTINGS_ID,)).fetchone()
    finally:
        conn.close()
    out = dict(DEFAULT_SETTINGS)
    if r and r[0]:
        try:
            saved = json.loads(r[0])
            # deep-ish merge for platform_fees
            fees = dict(out["platform_fees"]); fees.update(saved.get("platform_fees") or {})
            out.update(saved); out["platform_fees"] = fees
        except Exception:
            pass
    return out

def _db_set_settings(patch: dict) -> dict:
    cur = _db_get_settings()
    for k, v in (patch or {}).items():
        if v is None:
            continue
        if k == "platform_fees" and isinstance(v, dict):
            fees = dict(cur.get("platform_fees") or {}); fees.update(v)
            cur["platform_fees"] = fees
        else:
            cur[k] = v
    conn = _sqlite_conn()
    try:
        conn.execute("INSERT OR REPLACE INTO commerce_tax_settings (id,data,updated_at) "
                     "VALUES (?,?,?)", (_SETTINGS_ID, json.dumps(cur), now_iso()))
        conn.commit()
    finally:
        conn.close()
    return cur


# ─────────────────────────────────────────────────────────────────────────────
# Order costs store
# ─────────────────────────────────────────────────────────────────────────────

def _db_get_costs(order_id: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM commerce_order_costs WHERE order_id=?",
                         (order_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()

def _db_upsert_costs(order_id: str, patch: dict) -> dict:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM commerce_order_costs WHERE order_id=?",
                         (order_id,)).fetchone()
        base = dict(r) if r else {}
        merged = {**base, **{k: v for k, v in patch.items() if v is not None}}
        conn.execute(
            "INSERT OR REPLACE INTO commerce_order_costs "
            "(order_id,cogs,platform_fee,postage_cost,packaging_cost,other_cost,"
            " fee_auto,cogs_auto,notes,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (order_id, merged.get("cogs"), merged.get("platform_fee"),
             merged.get("postage_cost"), merged.get("packaging_cost"),
             merged.get("other_cost"),
             _i(merged.get("fee_auto", 1)), _i(merged.get("cogs_auto", 1)),
             merged.get("notes", ""), now_iso()))
        conn.commit()
        return dict(conn.execute("SELECT * FROM commerce_order_costs WHERE order_id=?",
                                 (order_id,)).fetchone())
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Finance model — the heart of the profit engine
# ─────────────────────────────────────────────────────────────────────────────

def _auto_cogs(order: dict) -> float:
    """Sum product.cost × qty for an order's line items (best effort)."""
    core = _core()
    if not core:
        return 0.0
    total = 0.0
    for it in (order.get("items") or []):
        ref = it.get("product_id") or it.get("sku")
        qty = _i(it.get("qty", 1)) or 1
        if not ref:
            continue
        try:
            p = core._db_get_product(ref)
        except Exception:
            p = None
        if p:
            total += _f(p.get("cost")) * qty
    return round(total, 2)

def _auto_postage(order_id: str) -> Optional[float]:
    """Actual postage cost booked in the fulfilment module, if any."""
    ful = sys.modules.get("commerce_fulfilment")
    if ful and hasattr(ful, "_db_order_postage"):
        try:
            return ful._db_order_postage(order_id)
        except Exception:
            return None
    return None

def _materials_packaging(order: dict, settings: dict) -> float:
    """Per-item packaging cost from the consumables ledger (commerce_ops) × units
    in the order, so packaging accounting is per-item. Falls back to the flat
    packaging_default when no materials are recorded."""
    ops = sys.modules.get("commerce_ops")
    if ops and hasattr(ops, "_materials_per_item_sync"):
        try:
            per = _f(ops._materials_per_item_sync("", order.get("store_id") or "")
                     .get("per_item_total"))
            if per:
                qty = sum(_i(it.get("qty", 1)) for it in (order.get("items") or [])) or 1
                return round(per * qty, 2)
        except Exception:
            pass
    return _f(settings.get("packaging_default"), 0.30)


def _order_finance(order: dict, settings: dict) -> dict:
    """Full profit breakdown for one order. Recorded costs win over auto ones."""
    oid = order.get("id", "")
    total = _f(order.get("total"))
    shipping_charged = _f(order.get("shipping"))
    source = (order.get("source") or "manual").lower()
    costs = _db_get_costs(oid) or {}
    fees_sched = (settings.get("platform_fees") or {}).get(source) \
        or (settings.get("platform_fees") or {}).get("manual") or {"pct": 0.0, "fixed": 0.0}

    cogs = costs.get("cogs")
    if cogs is None:
        cogs = _auto_cogs(order)
    fee = costs.get("platform_fee")
    if fee is None:
        fee = round(_f(fees_sched.get("pct")) * total + _f(fees_sched.get("fixed")), 2)
    postage = costs.get("postage_cost")
    if postage is None:
        postage = _auto_postage(oid)
    if postage is None:
        postage = 0.0
    packaging = costs.get("packaging_cost")
    if packaging is None:
        packaging = _materials_packaging(order, settings)
    other = _f(costs.get("other_cost"))

    cost_total = round(_f(cogs) + _f(fee) + _f(postage) + _f(packaging) + other, 2)
    net = round(total - cost_total, 2)
    margin = round(net / total * 100, 1) if total else None
    return {
        "order_id": oid, "source": source, "status": order.get("status"),
        "placed_at": order.get("placed_at"),
        "revenue": round(total, 2), "shipping_charged": round(shipping_charged, 2),
        "cogs": round(_f(cogs), 2), "platform_fee": round(_f(fee), 2),
        "postage_cost": round(_f(postage), 2), "packaging_cost": round(_f(packaging), 2),
        "other_cost": round(other, 2), "cost_total": cost_total,
        "net_profit": net, "margin_pct": margin,
        "recorded": bool(costs), "currency": order.get("currency") or "GBP",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tax-year helpers
# ─────────────────────────────────────────────────────────────────────────────

def _current_tax_year_start(now: datetime = None) -> int:
    now = now or datetime.now(timezone.utc)
    # UK tax year starts 6 April. Before 6 Apr -> previous year.
    return now.year if (now.month, now.day) >= (4, 6) else now.year - 1

def _bands_for(year_start: int) -> dict:
    if year_start in UK_TAX_YEARS:
        return UK_TAX_YEARS[year_start]
    return UK_TAX_YEARS[max(UK_TAX_YEARS)]

def _tax_year_bounds(year_start: int) -> tuple:
    """ISO bounds [6 Apr year_start, 6 Apr year_start+1)."""
    start = datetime(year_start, 4, 6, tzinfo=timezone.utc).isoformat()
    end = datetime(year_start + 1, 4, 6, tzinfo=timezone.utc).isoformat()
    return start, end

def _income_tax(taxable_income: float, b: dict) -> dict:
    """UK income tax on non-savings income, with personal-allowance taper."""
    ti = max(0.0, taxable_income)
    pa = b["personal_allowance"]
    taper_start = b["pa_taper_start"]
    if ti > taper_start:
        pa = max(0.0, pa - (ti - taper_start) / 2.0)
    taxable = max(0.0, ti - pa)
    basic_band = b["basic_limit"]
    tax = 0.0
    # basic
    basic_amt = min(taxable, basic_band)
    tax += basic_amt * b["basic_rate"]
    remaining = taxable - basic_amt
    # higher (between basic band top and additional threshold)
    higher_cap = max(0.0, (b["higher_limit"] - pa) - basic_band)
    higher_amt = min(max(0.0, remaining), higher_cap)
    tax += higher_amt * b["higher_rate"]
    remaining -= higher_amt
    # additional
    if remaining > 0:
        tax += remaining * b["additional_rate"]
    return {"personal_allowance_used": round(pa, 2), "taxable": round(taxable, 2),
            "income_tax": round(tax, 2)}

def _class4_nic(profit: float, b: dict) -> float:
    p = max(0.0, profit)
    if p <= b["c4_lower"]:
        return 0.0
    main = (min(p, b["c4_upper"]) - b["c4_lower"]) * b["c4_main"]
    upper = max(0.0, p - b["c4_upper"]) * b["c4_upper_rate"]
    return round(main + upper, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation across orders
# ─────────────────────────────────────────────────────────────────────────────

def _iter_orders(start_iso: str = "", end_iso: str = "", source: str = "",
                 store_id: str = "") -> List[dict]:
    core = _core()
    if not core:
        return []
    conn = _sqlite_conn()
    try:
        sql = ("SELECT * FROM commerce_orders WHERE status NOT IN "
               "('cancelled','refunded')")
        args: List[Any] = []
        if start_iso:
            sql += " AND COALESCE(placed_at, updated_at) >= ?"; args.append(start_iso)
        if end_iso:
            sql += " AND COALESCE(placed_at, updated_at) < ?"; args.append(end_iso)
        if source:
            sql += " AND source = ?"; args.append(source)
        if store_id:
            sql += " AND store_id = ?"; args.append(store_id)
        sql += " ORDER BY COALESCE(placed_at, updated_at) DESC"
        try:
            rows = conn.execute(sql, args).fetchall()
        except Exception:
            rows = []                      # commerce_orders not created yet
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["items"] = json.loads(d.get("items") or "[]")
        except Exception:
            d["items"] = []
        out.append(d)
    return out

def _aggregate(orders: List[dict], settings: dict) -> dict:
    totals = {"orders": 0, "units": 0, "revenue": 0.0, "cogs": 0.0, "platform_fee": 0.0,
              "postage_cost": 0.0, "packaging_cost": 0.0, "other_cost": 0.0,
              "cost_total": 0.0, "net_profit": 0.0}
    by_platform: Dict[str, dict] = {}
    lines = []
    for o in orders:
        fin = _order_finance(o, settings)
        lines.append(fin)
        totals["orders"] += 1
        totals["units"] += sum(_i(it.get("qty", 1)) or 1 for it in (o.get("items") or []))
        for k in ("revenue", "cogs", "platform_fee", "postage_cost", "packaging_cost",
                  "other_cost", "cost_total", "net_profit"):
            totals[k] += fin[k]
        p = fin["source"]
        bp = by_platform.setdefault(p, {"orders": 0, "revenue": 0.0, "net_profit": 0.0,
                                        "cost_total": 0.0})
        bp["orders"] += 1; bp["revenue"] += fin["revenue"]
        bp["net_profit"] += fin["net_profit"]; bp["cost_total"] += fin["cost_total"]
    for k in totals:
        if isinstance(totals[k], float):
            totals[k] = round(totals[k], 2)
    for p in by_platform.values():
        for k in ("revenue", "net_profit", "cost_total"):
            p[k] = round(p[k], 2)
    totals["margin_pct"] = (round(totals["net_profit"] / totals["revenue"] * 100, 1)
                            if totals["revenue"] else None)
    return {"totals": totals, "by_platform": by_platform, "orders": lines}


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    @capability(
        "business.tax.settings", http_method="GET",
        http_path="/business/tax/settings", http_tags=["commerce"],
        memory="off", silent=True,
        description="Read the tax / profit settings (VAT registration + scheme, "
                    "trading-allowance policy, other income, per-platform fee "
                    "schedule, packaging default, set-aside buffer). Output: {settings}.")
    async def cap_tax_settings(trace_id=None):
        await _ensure_schema()
        return {"settings": await _run(_db_get_settings)}

    @capability(
        "business.tax.settings.set", http_method="POST",
        http_path="/business/tax/settings/set", http_tags=["commerce"],
        schema=enum_schema(vat_scheme=["standard", "flat_rate", "margin"],
                           use_trading_allowance=["auto", "yes", "no"],
                           business_type=["sole_trader", "ltd"]),
        description="Update tax / profit settings. Input (all optional): "
                    "vat_registered (bool), vat_scheme (standard|flat_rate|margin), "
                    "flat_rate_pct (float), use_trading_allowance (auto|yes|no), "
                    "other_taxable_income (float — employment/other income this year), "
                    "business_type (sole_trader|ltd), packaging_default (float £), "
                    "set_aside_buffer_pct (float), platform_fees (dict "
                    "{platform:{pct,fixed}}). Output: {ok, settings}.")
    async def cap_tax_settings_set(
        vat_registered: bool = None, vat_scheme: str = None, flat_rate_pct: float = None,
        use_trading_allowance: str = None, other_taxable_income: float = None,
        business_type: str = None, packaging_default: float = None,
        set_aside_buffer_pct: float = None, platform_fees: Dict = None, trace_id=None):
        await _ensure_schema()
        patch = {"vat_registered": vat_registered, "vat_scheme": vat_scheme,
                 "flat_rate_pct": flat_rate_pct,
                 "use_trading_allowance": use_trading_allowance,
                 "other_taxable_income": other_taxable_income,
                 "business_type": business_type, "packaging_default": packaging_default,
                 "set_aside_buffer_pct": set_aside_buffer_pct,
                 "platform_fees": platform_fees}
        s = await _run(_db_set_settings, patch)
        return {"ok": True, "settings": s}

    @capability(
        "business.profit.record", http_method="POST",
        http_path="/business/profit/record", http_tags=["commerce"],
        description="Attach / override the cost components of one order so its profit "
                    "is exact. Any omitted field keeps its auto value (COGS from product "
                    "cost, fee from the schedule, postage from the booked shipment). "
                    "Input: order_id (str!), cogs (float), platform_fee (float), "
                    "postage_cost (float), packaging_cost (float), other_cost (float), "
                    "notes (str). Output: {ok, finance}.")
    async def cap_profit_record(
        order_id: str = "", cogs: float = None, platform_fee: float = None,
        postage_cost: float = None, packaging_cost: float = None,
        other_cost: float = None, notes: str = "", trace_id=None):
        await _ensure_schema()
        if not order_id:
            return {"error": "order_id required"}
        core = _core()
        order = await _run(core._db_get_order, order_id) if core else None
        if not order:
            return {"error": "order not found"}
        await _run(_db_upsert_costs, order_id, {
            "cogs": cogs, "platform_fee": platform_fee, "postage_cost": postage_cost,
            "packaging_cost": packaging_cost, "other_cost": other_cost,
            "fee_auto": 0 if platform_fee is not None else 1,
            "cogs_auto": 0 if cogs is not None else 1, "notes": notes or None})
        settings = await _run(_db_get_settings)
        fin = await _run(_order_finance, order, settings)
        await emit_event({"type": "commerce.progress", "stage": "profit.record",
                          "message": f"order {order_id}: net £{fin['net_profit']:.2f}"})
        return {"ok": True, "finance": fin}

    @capability(
        "business.profit.order", http_method="GET",
        http_path="/business/profit/order", http_tags=["commerce"],
        memory="off", silent=True,
        description="Full profit breakdown for one order: revenue vs COGS, platform "
                    "fee, postage cost, packaging, other → net profit + margin. "
                    "Input: order_id (str!). Output: {finance}.")
    async def cap_profit_order(order_id: str = "", trace_id=None):
        await _ensure_schema()
        if not order_id:
            return {"error": "order_id required"}
        core = _core()
        order = await _run(core._db_get_order, order_id) if core else None
        if not order:
            return {"error": "order not found"}
        settings = await _run(_db_get_settings)
        return {"finance": await _run(_order_finance, order, settings)}

    @capability(
        "business.profit.report", http_method="GET",
        http_path="/business/profit/report", http_tags=["commerce"],
        memory="off", silent=True,
        description="Profit & loss across a range. Input: period (all|tax_year|month|"
                    "range), tax_year (int — start year, e.g. 2025 = 2025/26; default "
                    "current), month (YYYY-MM), start (ISO), end (ISO), source "
                    "(ebay|vinted|manual — filter platform), store_id (str — scope to "
                    "one store), detail (bool — include per-order lines). Output: "
                    "{totals:{revenue,cogs,platform_fee,postage_cost,packaging_cost,"
                    "net_profit,margin_pct,units,orders}, by_platform, orders?}.")
    async def cap_profit_report(
        period: str = "all", tax_year: int = 0, month: str = "", start: str = "",
        end: str = "", source: str = "", store_id: str = "", detail: bool = False,
        trace_id=None):
        await _ensure_schema()
        s_iso = e_iso = ""
        if period == "tax_year" or (tax_year and period not in ("month", "range")):
            ys = int(tax_year) if tax_year else _current_tax_year_start()
            s_iso, e_iso = _tax_year_bounds(ys)
        elif period == "month" or month:
            try:
                y, m = month.split("-"); y, m = int(y), int(m)
                s_iso = datetime(y, m, 1, tzinfo=timezone.utc).isoformat()
                e_iso = (datetime(y + (m == 12), (m % 12) + 1, 1,
                                  tzinfo=timezone.utc).isoformat())
            except Exception:
                return {"error": "month must be YYYY-MM"}
        elif period == "range":
            s_iso, e_iso = start, end
        orders = await _run(_iter_orders, s_iso, e_iso, source, store_id)
        settings = await _run(_db_get_settings)
        agg = await _run(_aggregate, orders, settings)
        out = {"period": period, "start": s_iso, "end": e_iso, "store_id": store_id,
               "totals": agg["totals"], "by_platform": agg["by_platform"]}
        if detail:
            out["orders"] = agg["orders"]
        return out

    @capability(
        "business.tax.summary", http_method="GET",
        http_path="/business/tax/summary", http_tags=["commerce"],
        memory="off", silent=True,
        description="UK tax estimate for a tax year (6 Apr → 5 Apr): gross trading "
                    "income, allowable expenses vs the £1,000 trading allowance, "
                    "taxable profit, Income Tax (with personal-allowance taper), Class 4 "
                    "NIC, VAT estimate if registered, and a recommended amount to set "
                    "aside. NOT tax advice — an estimate. Input: tax_year (int — start "
                    "year, default current). Output: {tax_year_label, income, expenses, "
                    "taxable_profit, income_tax, class4_nic, vat, total_liability, "
                    "set_aside, notes, vat_threshold_watch}. store_id (str) scopes to "
                    "one store; omit for the whole business.")
    async def cap_tax_summary(tax_year: int = 0, store_id: str = "", trace_id=None):
        await _ensure_schema()
        ys = int(tax_year) if tax_year else _current_tax_year_start()
        b = _bands_for(ys)
        s_iso, e_iso = _tax_year_bounds(ys)
        settings = await _run(_db_get_settings)
        orders = await _run(_iter_orders, s_iso, e_iso, "", store_id)
        agg = await _run(_aggregate, orders, settings)
        t = agg["totals"]
        gross_income = t["revenue"]
        expenses = round(t["cogs"] + t["platform_fee"] + t["postage_cost"]
                         + t["packaging_cost"] + t["other_cost"], 2)
        profit_after_expenses = round(gross_income - expenses, 2)

        # Trading allowance decision (auto = whichever leaves less taxable profit).
        ta = b["trading_allowance"]
        pref = settings.get("use_trading_allowance", "auto")
        profit_with_allowance = round(max(0.0, gross_income - ta), 2)
        if pref == "yes":
            use_ta, taxable_profit = True, profit_with_allowance
        elif pref == "no":
            use_ta, taxable_profit = False, profit_after_expenses
        else:
            use_ta = profit_with_allowance < profit_after_expenses
            taxable_profit = min(profit_with_allowance, profit_after_expenses)
        taxable_profit = max(0.0, taxable_profit)

        other_income = _f(settings.get("other_taxable_income"))
        total_income = taxable_profit + other_income
        it = _income_tax(total_income, b)
        # Income tax attributable to the trading profit (marginal, above other income).
        it_other = _income_tax(other_income, b)["income_tax"]
        trade_income_tax = round(max(0.0, it["income_tax"] - it_other), 2)
        c4 = _class4_nic(taxable_profit, b)

        vat = {"registered": bool(settings.get("vat_registered")), "scheme": settings.get("vat_scheme"),
               "due_estimate": 0.0}
        if settings.get("vat_registered"):
            scheme = settings.get("vat_scheme")
            if scheme == "flat_rate":
                vat["due_estimate"] = round(gross_income * _f(settings.get("flat_rate_pct"), 0.075), 2)
                vat["note"] = "flat-rate: % of VAT-inclusive turnover"
            elif scheme == "margin":
                margin_total = max(0.0, gross_income - t["cogs"])
                vat["due_estimate"] = round(margin_total * b["vat_rate"] / (1 + b["vat_rate"]), 2)
                vat["note"] = "margin scheme: VAT on the margin only (typical for used goods)"
            else:
                vat["due_estimate"] = round(gross_income * b["vat_rate"] / (1 + b["vat_rate"]), 2)
                vat["note"] = "standard: output VAT on sales (input VAT on purchases not modelled)"

        liability = round(trade_income_tax + c4 + vat["due_estimate"], 2)
        buffer_pct = _f(settings.get("set_aside_buffer_pct"), 0.05)
        set_aside = round(liability * (1 + buffer_pct), 2)

        notes = []
        if gross_income <= ta:
            notes.append(f"Trading income £{gross_income:,.2f} is within the £{ta:,.0f} "
                         "trading allowance — likely no tax due and no need to register "
                         "for Self Assessment on this alone.")
        if use_ta:
            notes.append(f"Using the £{ta:,.0f} trading allowance beats deducting "
                         f"£{expenses:,.2f} of expenses this year.")
        else:
            notes.append(f"Deducting £{expenses:,.2f} of actual expenses beats the "
                         f"£{ta:,.0f} trading allowance.")
        vat_watch = {"threshold": b["vat_threshold"], "turnover": gross_income,
                     "headroom": round(b["vat_threshold"] - gross_income, 2),
                     "over": gross_income > b["vat_threshold"]}
        if vat_watch["over"] and not settings.get("vat_registered"):
            notes.append(f"Turnover £{gross_income:,.0f} is over the £{b['vat_threshold']:,.0f} "
                         "VAT registration threshold — you likely must register for VAT.")

        return {
            "tax_year_label": f"{ys}/{str(ys + 1)[-2:]}",
            "start": s_iso, "end": e_iso,
            "income": {"gross_trading": gross_income, "other": other_income,
                       "total": round(total_income, 2)},
            "expenses": {"total": expenses, "cogs": t["cogs"], "platform_fee": t["platform_fee"],
                         "postage_cost": t["postage_cost"], "packaging_cost": t["packaging_cost"],
                         "other": t["other_cost"]},
            "net_profit_after_expenses": profit_after_expenses,
            "used_trading_allowance": use_ta,
            "taxable_profit": round(taxable_profit, 2),
            "income_tax": {"on_trade": trade_income_tax, "total_all_income": it["income_tax"],
                           "personal_allowance_used": it["personal_allowance_used"]},
            "class4_nic": c4,
            "vat": vat,
            "total_liability": liability,
            "set_aside": set_aside,
            "vat_threshold_watch": vat_watch,
            "orders": t["orders"], "units": t["units"],
            "notes": notes,
            "disclaimer": "Estimate only — not tax advice. Verify with HMRC / an accountant.",
        }

    log.info("business.tax: ready (profit engine + UK tax %s)",
             f"{_current_tax_year_start()}/{str(_current_tax_year_start()+1)[-2:]}")
