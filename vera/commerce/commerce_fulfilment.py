"""
commerce_fulfilment.py — UK shipping, labels, Post-Office runs & pickups
========================================================================

The dispatch side of the reselling operation:

  • **Rate card** — Royal Mail (Large Letter → Special Delivery), Evri, Yodel and
    InPost services with weight-tiered prices, so the operator can quote a parcel
    and see the cheapest fit and how it compares to what the buyer was charged.
  • **Shipments** — one record per order to post: chosen service, parcel weight /
    size, actual postage cost (which feeds the profit engine via
    ``_db_order_postage``), tracking number and dispatch status.
  • **Thermal labels** — renders an address label and prints it to the cheap USB
    thermal printer through the existing ``print.label`` capability.
  • **Post-Office runs** — batches everything waiting to be posted into a single
    trip, drops a timed event on the Calendar and produces a manifest to carry.
  • **Pickups** — books a courier / Royal Mail collection window as a shipment
    batch + a Calendar event.

Storage: shared Data-Fabric SQLite db. All money is GBP. Prices are editable
defaults (carriers change them a few times a year), not a live carrier API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("vera.commerce.fulfilment")

try:
    from Vera.vera.capability_orchestration import (
        capability, emit_event, now_iso, enum_schema,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.commerce.fulfilment").warning("commerce fulfilment unavailable: %s", e)
    _CAP_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# UK carrier rate card (editable defaults, GBP; weight tiers = [max_grams, price])
# ─────────────────────────────────────────────────────────────────────────────

RATE_CARD: Dict[str, dict] = {
    "rm_ll_2nd":     {"carrier": "Royal Mail", "service": "Large Letter 2nd Class",
                      "format": "large_letter", "tracked": False, "signed": False, "comp": 20,
                      "tiers": [[100, 0.85], [250, 1.30], [500, 1.55], [750, 2.00]]},
    "rm_ll_1st":     {"carrier": "Royal Mail", "service": "Large Letter 1st Class",
                      "format": "large_letter", "tracked": False, "signed": False, "comp": 20,
                      "tiers": [[100, 1.55], [250, 1.95], [500, 2.30], [750, 2.80]]},
    "rm_sp_2nd":     {"carrier": "Royal Mail", "service": "Small Parcel 2nd Class",
                      "format": "small_parcel", "tracked": False, "signed": False, "comp": 20,
                      "tiers": [[1000, 2.99], [2000, 3.35]]},
    "rm_sp_1st":     {"carrier": "Royal Mail", "service": "Small Parcel 1st Class",
                      "format": "small_parcel", "tracked": False, "signed": False, "comp": 20,
                      "tiers": [[1000, 3.99], [2000, 4.45]]},
    "rm_mp_2nd":     {"carrier": "Royal Mail", "service": "Medium Parcel 2nd Class",
                      "format": "medium_parcel", "tracked": False, "signed": False, "comp": 20,
                      "tiers": [[2000, 5.65], [5000, 8.95], [10000, 14.55], [20000, 22.30]]},
    "rm_mp_1st":     {"carrier": "Royal Mail", "service": "Medium Parcel 1st Class",
                      "format": "medium_parcel", "tracked": False, "signed": False, "comp": 20,
                      "tiers": [[2000, 6.85], [5000, 10.20], [10000, 17.00], [20000, 25.00]]},
    "rm_tracked48":  {"carrier": "Royal Mail", "service": "Tracked 48",
                      "format": "parcel", "tracked": True, "signed": False, "comp": 100,
                      "tiers": [[1000, 3.29], [2000, 3.55], [5000, 5.60], [10000, 7.55], [20000, 10.55]]},
    "rm_tracked24":  {"carrier": "Royal Mail", "service": "Tracked 24",
                      "format": "parcel", "tracked": True, "signed": False, "comp": 100,
                      "tiers": [[1000, 4.45], [2000, 4.99], [5000, 6.95], [10000, 8.95], [20000, 12.50]]},
    "rm_special1pm": {"carrier": "Royal Mail", "service": "Special Delivery 1pm",
                      "format": "parcel", "tracked": True, "signed": True, "comp": 750,
                      "tiers": [[100, 8.15], [500, 9.05], [1000, 9.95], [2000, 12.00], [10000, 20.00]]},
    "evri_postable": {"carrier": "Evri", "service": "Postable (Letterbox)",
                      "format": "large_letter", "tracked": True, "signed": False, "comp": 20,
                      "tiers": [[100, 2.29], [250, 2.49], [1000, 2.99]]},
    "evri_parcel":   {"carrier": "Evri", "service": "Standard Parcel",
                      "format": "parcel", "tracked": True, "signed": False, "comp": 25,
                      "tiers": [[1000, 3.14], [2000, 3.45], [5000, 4.05], [10000, 5.15], [15000, 6.60]]},
    "yodel_store":   {"carrier": "Yodel", "service": "Store to Door",
                      "format": "parcel", "tracked": True, "signed": False, "comp": 50,
                      "tiers": [[2000, 3.20], [5000, 4.10], [10000, 5.30], [30000, 8.99]]},
    "inpost_locker": {"carrier": "InPost", "service": "Locker to Locker",
                      "format": "parcel", "tracked": True, "signed": False, "comp": 50,
                      "tiers": [[1000, 3.49], [2000, 3.99], [5000, 4.79], [15000, 6.99]]},
}

SHIP_STATUS = ["to_ship", "labelled", "posted", "collected", "delivered", "cancelled"]
PARCEL_FORMATS = ["large_letter", "small_parcel", "medium_parcel", "parcel"]

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

def _thermal():
    return sys.modules.get("thermal_printer_capabilities")

def _calendar():
    return sys.modules.get("calendar_capabilities")


# ─────────────────────────────────────────────────────────────────────────────
# Rate quoting
# ─────────────────────────────────────────────────────────────────────────────

def quote_rates(weight_g: int, fmt: str = "", tracked_only: bool = False,
                signed_only: bool = False) -> List[dict]:
    weight_g = max(1, _i(weight_g, 1))
    out = []
    for sid, s in RATE_CARD.items():
        if fmt and s["format"] != fmt:
            continue
        if tracked_only and not s["tracked"]:
            continue
        if signed_only and not s["signed"]:
            continue
        price = None
        for max_g, tier_price in s["tiers"]:
            if weight_g <= max_g:
                price = tier_price
                break
        if price is None:
            continue                        # parcel too heavy for this service
        out.append({"service_id": sid, "carrier": s["carrier"], "service": s["service"],
                    "format": s["format"], "tracked": s["tracked"], "signed": s["signed"],
                    "compensation": s["comp"], "price": price})
    out.sort(key=lambda r: r["price"])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Schema + shipment store
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_schema_sync():
    global _SCHEMA_READY
    conn = _sqlite_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS commerce_shipments (
                id            TEXT PRIMARY KEY,
                order_id      TEXT,
                carrier       TEXT,
                service_id    TEXT,
                service_label TEXT,
                tracked       INTEGER DEFAULT 0,
                signed        INTEGER DEFAULT 0,
                format        TEXT,
                weight_g      INTEGER DEFAULT 0,
                length_mm     INTEGER DEFAULT 0,
                width_mm      INTEGER DEFAULT 0,
                height_mm     INTEGER DEFAULT 0,
                postage_cost  REAL DEFAULT 0,
                tracking      TEXT,
                status        TEXT DEFAULT 'to_ship',
                label_printed INTEGER DEFAULT 0,
                pickup_id     TEXT,
                posted_at     TEXT,
                notes         TEXT,
                created_at    TEXT,
                updated_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_ship_order ON commerce_shipments(order_id);
            CREATE INDEX IF NOT EXISTS ix_ship_status ON commerce_shipments(status);

            CREATE TABLE IF NOT EXISTS commerce_pickups (
                id           TEXT PRIMARY KEY,
                carrier      TEXT,
                date         TEXT,
                window       TEXT,
                address      TEXT,
                shipment_ids TEXT,
                status       TEXT DEFAULT 'requested',
                cal_event_id TEXT,
                notes        TEXT,
                created_at   TEXT,
                updated_at   TEXT
            );
        """)
        conn.commit()
    finally:
        conn.close()
    _SCHEMA_READY = True

async def _ensure_schema():
    if not _SCHEMA_READY:
        await _run(_ensure_schema_sync)

_SHIP_COLS = ("id", "order_id", "carrier", "service_id", "service_label", "tracked",
              "signed", "format", "weight_g", "length_mm", "width_mm", "height_mm",
              "postage_cost", "tracking", "status", "label_printed", "pickup_id",
              "posted_at", "notes", "created_at", "updated_at")

def _ship_out(row) -> dict:
    return dict(row) if row else None

def _db_upsert_shipment(fields: dict) -> dict:
    conn = _sqlite_conn()
    try:
        sid = fields.get("id") or ""
        existing = conn.execute("SELECT * FROM commerce_shipments WHERE id=?",
                                (sid,)).fetchone() if sid else None
        base = dict(existing) if existing else {}
        sid = base.get("id") or sid or _new_id("shp")
        merged = {**base, **{k: v for k, v in fields.items() if v is not None}}
        now = now_iso()
        row = {c: merged.get(c) for c in _SHIP_COLS}
        row["id"] = sid
        row["created_at"] = base.get("created_at") or now
        row["updated_at"] = now
        for c in ("tracked", "signed", "weight_g", "length_mm", "width_mm",
                  "height_mm", "label_printed"):
            row[c] = _i(row.get(c))
        row["postage_cost"] = _f(row.get("postage_cost"))
        row["status"] = row.get("status") or "to_ship"
        conn.execute(
            "INSERT OR REPLACE INTO commerce_shipments (%s) VALUES (%s)" %
            (",".join(_SHIP_COLS), ",".join("?" for _ in _SHIP_COLS)),
            tuple(row[c] for c in _SHIP_COLS))
        conn.commit()
        return dict(conn.execute("SELECT * FROM commerce_shipments WHERE id=?", (sid,)).fetchone())
    finally:
        conn.close()

def _db_get_shipment(sid: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM commerce_shipments WHERE id=?", (sid,)).fetchone()
        return _ship_out(r)
    finally:
        conn.close()

def _db_list_shipments(status: str = "", order_id: str = "", limit: int = 300) -> List[dict]:
    conn = _sqlite_conn()
    try:
        sql = "SELECT * FROM commerce_shipments"; where, args = [], []
        if status:   where.append("status=?"); args.append(status)
        if order_id: where.append("order_id=?"); args.append(order_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"; args.append(int(limit))
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()

def _db_order_postage(order_id: str) -> Optional[float]:
    """Total postage cost booked for an order — consumed by the tax/profit engine."""
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT COALESCE(SUM(postage_cost),0), COUNT(*) "
                         "FROM commerce_shipments WHERE order_id=? AND status!='cancelled'",
                         (order_id,)).fetchone()
        return _f(r[0]) if r and r[1] else None
    finally:
        conn.close()

def _db_upsert_pickup(fields: dict) -> dict:
    conn = _sqlite_conn()
    try:
        pid = fields.get("id") or ""
        existing = conn.execute("SELECT * FROM commerce_pickups WHERE id=?",
                                (pid,)).fetchone() if pid else None
        base = dict(existing) if existing else {}
        pid = base.get("id") or pid or _new_id("pk")
        merged = {**base, **{k: v for k, v in fields.items() if v is not None}}
        now = now_iso()
        conn.execute(
            "INSERT OR REPLACE INTO commerce_pickups "
            "(id,carrier,date,window,address,shipment_ids,status,cal_event_id,notes,"
            " created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, merged.get("carrier", ""), merged.get("date", ""),
             merged.get("window", ""),
             json.dumps(merged.get("address") or []) if not isinstance(merged.get("address"), str) else merged.get("address"),
             json.dumps(merged.get("shipment_ids") or []) if not isinstance(merged.get("shipment_ids"), str) else merged.get("shipment_ids"),
             merged.get("status", "requested"), merged.get("cal_event_id", ""),
             merged.get("notes", ""), base.get("created_at") or now, now))
        conn.commit()
        r = conn.execute("SELECT * FROM commerce_pickups WHERE id=?", (pid,)).fetchone()
        d = dict(r)
        for k in ("address", "shipment_ids"):
            try: d[k] = json.loads(d.get(k) or "[]")
            except Exception: pass
        return d
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Address formatting for labels
# ─────────────────────────────────────────────────────────────────────────────

def _address_lines(addr: Any, name: str = "") -> List[str]:
    lines = []
    if name:
        lines.append(name)
    if isinstance(addr, dict):
        if isinstance(addr.get("lines"), list):
            lines += [str(x).strip() for x in addr["lines"] if str(x).strip()]
        for k in ("name", "line1", "line2", "street", "address", "city", "town",
                  "county", "postcode", "postal_code", "zip", "country"):
            v = addr.get(k)
            if v and str(v) not in lines:
                for part in str(v).splitlines():
                    if part.strip() and part.strip() not in lines:
                        lines.append(part.strip())
    elif isinstance(addr, list):
        lines += [str(x) for x in addr if x]
    elif isinstance(addr, str) and addr.strip():
        lines += [l.strip() for l in addr.splitlines() if l.strip()]
    return lines[:8]


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    @capability(
        "business.ship.carriers", http_method="GET",
        http_path="/business/ship/carriers", http_tags=["commerce"],
        memory="off", silent=True,
        description="The UK carrier rate card (Royal Mail / Evri / Yodel / InPost) with "
                    "weight-tiered prices. Output: {services:[{service_id,carrier,service,"
                    "format,tracked,signed,compensation,tiers}]}.")
    async def cap_ship_carriers(trace_id=None):
        return {"services": [{"service_id": k, **v} for k, v in RATE_CARD.items()],
                "formats": PARCEL_FORMATS}

    @capability(
        "business.ship.rates", http_method="GET",
        http_path="/business/ship/rates", http_tags=["commerce"],
        memory="off", silent=True,
        schema=enum_schema(format=PARCEL_FORMATS),
        description="Quote shipping options for a parcel, cheapest first. Compares to "
                    "what the buyer paid if order_id is given. Input: weight_g (int!), "
                    "format (large_letter|small_parcel|medium_parcel|parcel — filter), "
                    "tracked (bool — tracked only), signed (bool — signed only), "
                    "order_id (str — compare vs shipping charged). "
                    "Output: {rates:[{service_id,carrier,service,price,tracked}], "
                    "cheapest, shipping_charged?}.")
    async def cap_ship_rates(weight_g: int = 0, format: str = "", tracked: bool = False,
                             signed: bool = False, order_id: str = "", trace_id=None):
        if _i(weight_g) <= 0:
            return {"error": "weight_g (grams) required"}
        rates = quote_rates(int(weight_g), format, tracked, signed)
        out = {"rates": rates, "cheapest": rates[0] if rates else None,
               "weight_g": int(weight_g)}
        if order_id:
            core = _core()
            o = await _run(core._db_get_order, order_id) if core else None
            if o:
                out["shipping_charged"] = _f(o.get("shipping"))
                if rates:
                    out["postage_margin"] = round(_f(o.get("shipping")) - rates[0]["price"], 2)
        return out

    @capability(
        "business.ship.book", http_method="POST",
        http_path="/business/ship/book", http_tags=["commerce"],
        schema=enum_schema(service_id=list(RATE_CARD.keys()), status=SHIP_STATUS,
                           format=PARCEL_FORMATS),
        description="Create / update a shipment for an order: pick a service (or pass a "
                    "postage_cost directly), record parcel weight/size + tracking. The "
                    "booked postage cost flows into profit automatically. Optionally set "
                    "the order to 'shipped'. Input: order_id (str!), service_id (str — "
                    "from the rate card), weight_g (int), format, postage_cost (float — "
                    "overrides the service tier price), tracking (str), status "
                    "(to_ship|labelled|posted|…), set_order_shipped (bool), id (str — "
                    "update a shipment), notes. Output: {ok, shipment}.")
    async def cap_ship_book(
        order_id: str = "", service_id: str = "", weight_g: int = 0, format: str = "",
        postage_cost: float = None, tracking: str = "", status: str = "to_ship",
        set_order_shipped: bool = False, id: str = "", notes: str = "", trace_id=None):
        await _ensure_schema()
        if not (order_id or id):
            return {"error": "order_id required"}
        svc = RATE_CARD.get(service_id) if service_id else None
        cost = postage_cost
        if cost is None and svc and _i(weight_g) > 0:
            for max_g, tier_price in svc["tiers"]:
                if int(weight_g) <= max_g:
                    cost = tier_price
                    break
        fields = {
            "id": id or None, "order_id": order_id or None,
            "carrier": (svc or {}).get("carrier"), "service_id": service_id or None,
            "service_label": (svc or {}).get("service"),
            "tracked": 1 if (svc or {}).get("tracked") else 0,
            "signed": 1 if (svc or {}).get("signed") else 0,
            "format": format or (svc or {}).get("format"),
            "weight_g": weight_g, "postage_cost": cost, "tracking": tracking or None,
            "status": status, "notes": notes or None}
        shp = await _run(_db_upsert_shipment, fields)
        # Push tracking + status to the order if requested.
        core = _core()
        if set_order_shipped and core and order_id:
            try:
                await _run(core._db_set_order_status, order_id, "shipped", tracking or "")
            except Exception:
                pass
        await emit_event({"type": "commerce.progress", "stage": "ship.book",
                          "message": f"shipment {shp['id']} {shp.get('service_label') or ''} "
                                     f"£{_f(shp.get('postage_cost')):.2f}"})
        return {"ok": True, "shipment": shp}

    @capability(
        "business.ship.list", http_method="GET",
        http_path="/business/ship/list", http_tags=["commerce"],
        memory="off", silent=True,
        schema=enum_schema(status=SHIP_STATUS),
        description="List shipments. Input: status (to_ship|labelled|posted|…), order_id, "
                    "limit. Output: {shipments:[...], count, to_post, total_postage}.")
    async def cap_ship_list(status: str = "", order_id: str = "", limit: int = 300, trace_id=None):
        await _ensure_schema()
        rows = await _run(_db_list_shipments, status, order_id, int(limit))
        to_post = sum(1 for r in rows if r.get("status") in ("to_ship", "labelled"))
        return {"shipments": rows, "count": len(rows), "to_post": to_post,
                "total_postage": round(sum(_f(r.get("postage_cost")) for r in rows), 2)}

    @capability(
        "business.ship.mark", http_method="POST",
        http_path="/business/ship/mark", http_tags=["commerce"],
        schema=enum_schema(status=SHIP_STATUS),
        description="Update a shipment's dispatch status (and optionally the order's). "
                    "Input: id (str!), status (posted|collected|delivered|…), tracking "
                    "(str), sync_order (bool — mirror shipped/delivered onto the order). "
                    "Output: {ok, shipment}.")
    async def cap_ship_mark(id: str = "", status: str = "", tracking: str = "",
                            sync_order: bool = True, trace_id=None):
        await _ensure_schema()
        if not (id and status):
            return {"error": "id and status required"}
        patch = {"id": id, "status": status}
        if tracking:
            patch["tracking"] = tracking
        if status in ("posted", "collected"):
            patch["posted_at"] = now_iso()
        shp = await _run(_db_upsert_shipment, patch)
        core = _core()
        if sync_order and core and shp.get("order_id"):
            omap = {"posted": "shipped", "collected": "shipped", "delivered": "delivered"}
            if status in omap:
                try:
                    await _run(core._db_set_order_status, shp["order_id"], omap[status],
                               shp.get("tracking") or "")
                except Exception:
                    pass
        return {"ok": True, "shipment": shp}

    @capability(
        "business.ship.label", http_method="POST",
        http_path="/business/ship/label", http_tags=["commerce"],
        description="Print an address / shipping label on the USB thermal printer. "
                    "Pulls the delivery address from the order's customer when order_id "
                    "is given, or accepts explicit lines. Input: order_id (str — resolve "
                    "buyer address), shipment_id (str — mark label printed + use its "
                    "tracking), to (list of address lines — overrides), from_ (list — "
                    "return address), ref (str — order/tracking), barcode (str), "
                    "printer_id (str). Output: {ok, transport, bytes} or {error}.")
    async def cap_ship_label(
        order_id: str = "", shipment_id: str = "", to: List = None, from_: List = None,
        ref: str = "", barcode: str = "", printer_id: str = "", trace_id=None):
        await _ensure_schema()
        core = _core()
        to_lines = list(to) if to else []
        tracking = barcode
        if shipment_id:
            shp = await _run(_db_get_shipment, shipment_id)
            if shp:
                order_id = order_id or shp.get("order_id")
                tracking = tracking or shp.get("tracking") or ""
                ref = ref or shp.get("order_id") or shipment_id
        if not to_lines and order_id and core:
            o = await _run(core._db_get_order, order_id)
            if o and o.get("customer_id"):
                cust = await _run(core._db_get_customer, o["customer_id"])
                if cust:
                    to_lines = _address_lines(cust.get("address"), cust.get("name"))
            ref = ref or order_id
        if not to_lines:
            return {"error": "no delivery address — pass 'to' lines or an order with a "
                            "customer address"}
        thermal = _thermal()
        if not thermal or not hasattr(thermal, "cap_print_label"):
            return {"error": "thermal printer module not loaded"}
        res = await thermal.cap_print_label(
            printer_id=printer_id, to=to_lines, from_=from_ or [], ref=ref,
            barcode=tracking or barcode, note="", trace_id=None)
        if res.get("ok") and shipment_id:
            await _run(_db_upsert_shipment, {"id": shipment_id, "label_printed": 1,
                                             "status": "labelled"})
        return res

    @capability(
        "business.ship.postrun", http_method="POST",
        http_path="/business/ship/postrun", http_tags=["commerce"],
        description="Plan a Post-Office / drop-off run: gathers every shipment waiting "
                    "to be posted (status to_ship/labelled), schedules a timed trip on "
                    "the Calendar and returns a manifest to carry. Input: when (ISO start "
                    "— omit for a to-do), duration_min (int default 30), location (str — "
                    "the Post Office / drop shop), carrier (str — filter, optional). "
                    "Output: {ok, manifest:[...], count, total_postage, cal_event_id?}.")
    async def cap_ship_postrun(when: str = "", duration_min: int = 30, location: str = "",
                               carrier: str = "", trace_id=None):
        await _ensure_schema()
        rows = await _run(_db_list_shipments, "", "", 500)
        pending = [r for r in rows if r.get("status") in ("to_ship", "labelled")
                   and (not carrier or (r.get("carrier") or "").lower() == carrier.lower())]
        total = round(sum(_f(r.get("postage_cost")) for r in pending), 2)
        manifest = [{"shipment_id": r["id"], "order_id": r.get("order_id"),
                     "carrier": r.get("carrier"), "service": r.get("service_label"),
                     "tracking": r.get("tracking"), "postage_cost": _f(r.get("postage_cost")),
                     "labelled": bool(r.get("label_printed"))} for r in pending]
        result = {"ok": True, "manifest": manifest, "count": len(manifest),
                  "total_postage": total, "location": location}
        title = f"Post Office run — {len(manifest)} parcel(s)" + (f" @ {location}" if location else "")
        cal = _calendar()
        if cal and hasattr(cal, "cap_event_upsert"):
            try:
                desc = ("Drop off:\n" + "\n".join(
                    f"• {m['carrier'] or ''} {m['service'] or ''} — order {m['order_id']}"
                    f" ({'labelled' if m['labelled'] else 'NEEDS LABEL'})" for m in manifest)
                    + f"\nTotal postage: £{total:.2f}")
                if when:
                    try:
                        st = datetime.fromisoformat(when.replace("Z", "+00:00"))
                        end = (st + timedelta(minutes=int(duration_min or 30))).isoformat()
                    except Exception:
                        end = ""
                    ev = await cal.cap_event_upsert(
                        title=title, start=when, end=end, description=desc,
                        tags=["business", "shipping", "postrun"], color="#5a9e8f")
                    if ev.get("ok"):
                        result["cal_event_id"] = ev["event"]["id"]
                elif hasattr(cal, "cap_todo_upsert"):
                    td = await cal.cap_todo_upsert(title=title, due="", priority=1, notes=desc)
                    if td.get("ok"):
                        result["cal_todo_id"] = td["todo"]["id"]
            except Exception as e:
                result["cal_error"] = str(e)
        await emit_event({"type": "commerce.progress", "stage": "ship.postrun",
                          "message": f"post run: {len(manifest)} parcel(s), £{total:.2f}"})
        return result

    @capability(
        "business.ship.pickup", http_method="POST",
        http_path="/business/ship/pickup", http_tags=["commerce"],
        description="Arrange a courier / Royal Mail collection: records a pickup booking "
                    "for the given shipments and drops a Calendar event for the window. "
                    "(Booking the collection with the carrier's own account is a separate "
                    "step — this schedules and tracks it.) Input: carrier (str!), date "
                    "(YYYY-MM-DD or ISO!), window (str — e.g. '09:00-17:00'), address "
                    "(list of lines — collection address), shipment_ids (list — default "
                    "all to_ship/labelled), notes. Output: {ok, pickup, cal_event_id?}.")
    async def cap_ship_pickup(carrier: str = "", date: str = "", window: str = "",
                              address: List = None, shipment_ids: List = None,
                              notes: str = "", trace_id=None):
        await _ensure_schema()
        if not (carrier and date):
            return {"error": "carrier and date required"}
        if not shipment_ids:
            rows = await _run(_db_list_shipments, "", "", 500)
            shipment_ids = [r["id"] for r in rows if r.get("status") in ("to_ship", "labelled")
                            and (r.get("carrier") or "").lower() == carrier.lower()]
        pickup = await _run(_db_upsert_pickup, {
            "carrier": carrier, "date": date, "window": window or "09:00-17:00",
            "address": address or [], "shipment_ids": shipment_ids or [],
            "status": "requested", "notes": notes or None})
        # tag shipments with the pickup id
        for sid in (shipment_ids or []):
            await _run(_db_upsert_shipment, {"id": sid, "pickup_id": pickup["id"]})
        result = {"ok": True, "pickup": pickup}
        cal = _calendar()
        if cal and hasattr(cal, "cap_event_upsert"):
            try:
                start = date if "T" in date else f"{date}T09:00:00"
                try:
                    st = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    end = (st + timedelta(hours=8)).isoformat()
                except Exception:
                    end = ""
                ev = await cal.cap_event_upsert(
                    title=f"{carrier} collection — {len(shipment_ids or [])} parcel(s)",
                    start=start, end=end,
                    description=f"Collection window {window or '09:00-17:00'}. "
                                f"{len(shipment_ids or [])} parcel(s).\n{notes or ''}",
                    tags=["business", "shipping", "pickup"], color="#8fb87a")
                if ev.get("ok"):
                    await _run(_db_upsert_pickup, {"id": pickup["id"],
                                                   "cal_event_id": ev["event"]["id"]})
                    result["cal_event_id"] = ev["event"]["id"]
            except Exception as e:
                result["cal_error"] = str(e)
        await emit_event({"type": "commerce.progress", "stage": "ship.pickup",
                          "message": f"{carrier} pickup booked for {date}"})
        return result

    log.info("business.fulfilment: ready (%d carrier services)", len(RATE_CARD))
