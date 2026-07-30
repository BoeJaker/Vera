"""
commerce_intake.py — Per-unit intake, vision item-ID, quality grading & the Scan Station
========================================================================================

The "get physical stock in fast and accurately" surface. Where ``commerce_products``
is the *catalog* (one row per title — "Mario Kart 8, Switch"), this module adds
``commerce_units``: one row per **physical copy** you actually hold, each with its
own condition, grade, photos, AI-written description and asking price. That is what
lets a used-games shop carry four copies of the same game in four different
conditions and list each on its own merits.

  • **Catalog resolve** — ``business.catalog.resolve`` finds-or-creates the catalog
    product for a barcode / title so units always hang off a title record. The
    catalog product's ``qty_on_hand`` is kept in sync with the number of in-stock
    units, so the existing Dashboard / Inventory / Tax views stay correct.
  • **Unit intake** — ``business.unit.intake`` turns one scan (or one vision ID)
    into a stocked unit: barcode lookup → resolve catalog → create the unit →
    persist its photos → (optionally) grade + describe it with the vision model →
    (optionally) suggest a price. ``business.unit.{list,get,upsert,delete}`` round
    it out.
  • **Vision item-ID** — ``business.vision.identify_item`` names a game from a photo
    when there is no barcode to scan (loose cartridges, discs) — title / platform /
    region candidates with confidence, and it reads a barcode out of the photo if
    one is visible.
  • **Quality grading** — ``business.vision.assess_unit`` looks at a unit's photos
    *with* its catalog details in context and returns a condition, a 0–10 grade, a
    flaw list and a ready-to-list per-unit description.
  • **One-unit listing** — ``business.unit.draft_listing`` drafts a listing from a
    unit (its price / condition / photos / description) via the existing
    ``business.listing.draft`` and links it back.

Storage: shared Data-Fabric SQLite db. Money is GBP by default. The Scan Station
UI is served at ``/business/intake/panel``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import uuid
from pathlib import Path as _Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("vera.commerce.intake")

try:
    from Vera.vera.capability_orchestration import (
        APP, capability, emit_event, now_iso, enum_schema, CAPABILITY_REGISTRY,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.commerce.intake").warning("commerce intake unavailable: %s", e)
    _CAP_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Vocab (games-first, but generic — new categories just add options)
# ─────────────────────────────────────────────────────────────────────────────

CONDITIONS   = ["new", "like_new", "very_good", "good", "acceptable", "for_parts"]
COMPLETENESS = ["sealed", "cib", "boxed_no_manual", "cart_only", "disc_only",
                "loose", "digital", "n_a"]
UNIT_STATUS  = ["in_stock", "listed", "sold", "reserved", "scrapped"]
CATEGORIES   = ["video_game", "console", "accessory", "controller", "amiibo",
                "trading_card", "collectible", "merch", "other"]


def _f(v, default=0.0):
    try: return float(v)
    except (TypeError, ValueError): return default

def _i(v, default=0):
    try: return int(v)
    except (TypeError, ValueError): return default

def _new_id(prefix): return f"{prefix}_{uuid.uuid4().hex[:12]}"

async def _run(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

def _core():     return sys.modules.get("commerce_capabilities")
def _listing():  return sys.modules.get("commerce_listing")
def _pricing():  return sys.modules.get("commerce_pricing_capabilities")
def _media():    return sys.modules.get("media_capabilities")
def _images():   return sys.modules.get("image_fabric")

def _cap_raw(name: str):
    reg = CAPABILITY_REGISTRY.get(name) if CAPABILITY_REGISTRY else None
    return reg.get("raw") if reg else None


# ─────────────────────────────────────────────────────────────────────────────
# Schema — units
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMA_READY = False

_U_COLS = ("id", "product_id", "store_id", "sku", "upc", "title", "category",
           "condition", "grade", "completeness", "cost", "price", "currency",
           "photos", "description", "quality", "attributes", "status",
           "listing_id", "location", "created_at", "updated_at")
_U_JSON = {"photos", "quality", "attributes"}


def _ensure_schema_sync():
    global _SCHEMA_READY
    conn = _sqlite_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS commerce_units (
                id           TEXT PRIMARY KEY,
                product_id   TEXT,
                store_id     TEXT,
                sku          TEXT,
                upc          TEXT,
                title        TEXT,
                category     TEXT,
                condition    TEXT,
                grade        REAL DEFAULT 0,
                completeness TEXT,
                cost         REAL DEFAULT 0,
                price        REAL DEFAULT 0,
                currency     TEXT DEFAULT 'GBP',
                photos       TEXT,
                description  TEXT,
                quality      TEXT,
                attributes   TEXT,
                status       TEXT DEFAULT 'in_stock',
                listing_id   TEXT,
                location     TEXT,
                created_at   TEXT,
                updated_at   TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_units_product ON commerce_units(product_id);
            CREATE INDEX IF NOT EXISTS ix_units_status  ON commerce_units(status);
            CREATE INDEX IF NOT EXISTS ix_units_store   ON commerce_units(store_id);
            CREATE INDEX IF NOT EXISTS ix_units_upc     ON commerce_units(upc);
        """)
        conn.commit()
    finally:
        conn.close()
    _SCHEMA_READY = True

async def _ensure_schema():
    if not _SCHEMA_READY:
        await _run(_ensure_schema_sync)


def _unit_out(row) -> Optional[dict]:
    if not row:
        return None
    d = dict(row)
    for c in _U_JSON:
        default = [] if c == "photos" else {}
        try:
            d[c] = json.loads(d.get(c) or ("[]" if c == "photos" else "{}"))
        except Exception:
            d[c] = default
    return d


def _db_upsert_unit(fields: dict) -> dict:
    conn = _sqlite_conn()
    try:
        uid = fields.get("id") or ""
        existing = conn.execute("SELECT * FROM commerce_units WHERE id=?",
                                (uid,)).fetchone() if uid else None
        base = _unit_out(existing) if existing else {}
        uid = base.get("id") or uid or _new_id("unit")
        merged = {**base, **{k: v for k, v in fields.items() if v is not None}}
        now = now_iso()
        row = {}
        for c in _U_COLS:
            if c == "id": row[c] = uid
            elif c == "created_at": row[c] = base.get("created_at") or now
            elif c == "updated_at": row[c] = now
            elif c in _U_JSON:
                dv = [] if c == "photos" else {}
                row[c] = json.dumps(merged.get(c) if merged.get(c) is not None else dv)
            elif c in ("grade", "cost", "price"): row[c] = _f(merged.get(c))
            else: row[c] = merged.get(c, "") if merged.get(c) is not None else ""
        conn.execute("INSERT OR REPLACE INTO commerce_units (%s) VALUES (%s)" %
                     (",".join(_U_COLS), ",".join("?" for _ in _U_COLS)),
                     tuple(row[c] for c in _U_COLS))
        conn.commit()
        return _unit_out(conn.execute("SELECT * FROM commerce_units WHERE id=?",
                                      (uid,)).fetchone())
    finally:
        conn.close()

def _db_get_unit(uid: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        return _unit_out(conn.execute("SELECT * FROM commerce_units WHERE id=?",
                                      (uid,)).fetchone())
    finally:
        conn.close()

def _db_list_units(product_id: str = "", status: str = "", store_id: str = "",
                   q: str = "", limit: int = 300) -> List[dict]:
    conn = _sqlite_conn()
    try:
        sql = "SELECT * FROM commerce_units"; where, args = [], []
        if product_id: where.append("product_id=?"); args.append(product_id)
        if status:     where.append("status=?"); args.append(status)
        if store_id:   where.append("store_id=?"); args.append(store_id)
        if q:
            where.append("(title LIKE ? OR sku LIKE ? OR upc LIKE ?)")
            like = f"%{q}%"; args += [like, like, like]
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"; args.append(int(limit))
        return [_unit_out(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()

def _db_delete_unit(uid: str) -> Optional[str]:
    """Delete a unit; return its product_id so the caller can resync qty."""
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT product_id FROM commerce_units WHERE id=?",
                         (uid,)).fetchone()
        pid = (dict(r).get("product_id") if r else "") or ""
        conn.execute("DELETE FROM commerce_units WHERE id=?", (uid,))
        conn.commit()
        return pid
    finally:
        conn.close()

def _db_count_in_stock(product_id: str) -> int:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT COUNT(*) n FROM commerce_units "
                         "WHERE product_id=? AND status IN ('in_stock','listed','reserved')",
                         (product_id,)).fetchone()
        return _i(dict(r).get("n") if r else 0)
    finally:
        conn.close()


def _sync_product_qty_sync(product_id: str):
    """Keep the catalog product's qty_on_hand == number of held units. Cheap and
    idempotent; runs after every unit create / status change / delete so the
    existing Dashboard, Inventory and Tax views reflect physical reality."""
    core = _core()
    if not core or not product_id:
        return
    n = _db_count_in_stock(product_id)
    conn = _sqlite_conn()
    try:
        conn.execute("UPDATE commerce_products SET qty_on_hand=?, updated_at=? WHERE id=?",
                     (n, now_iso(), product_id))
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Photo persistence — accept a URL/path (keep) or base64/data-URI (store to disk)
# ─────────────────────────────────────────────────────────────────────────────

async def _persist_photo(src: str) -> str:
    if not src:
        return ""
    s = src.strip()
    if s.startswith("http://") or s.startswith("https://") or s.startswith("/"):
        return s                                    # already a servable URL
    b64 = s.split(",", 1)[-1] if s.startswith("data:") else s
    imgs = _images()
    if imgs and hasattr(imgs, "images_store"):
        try:
            res = await imgs.images_store(image_b64=b64, prompt="commerce unit photo",
                                          source="img2img", trace_id=None)
            if res.get("url"):
                return res["url"]
        except Exception as e:
            log.debug("photo store failed: %s", e)
    # last resort: keep the data URI inline (heavier, but never loses the photo)
    return src

async def _persist_photos(srcs: List[str]) -> List[str]:
    out = []
    for s in (srcs or []):
        u = await _persist_photo(s)
        if u:
            out.append(u)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Vision prompts
# ─────────────────────────────────────────────────────────────────────────────

_IDENTIFY_PROMPT = (
    "You are identifying a second-hand item for resale (mostly retro & modern "
    "VIDEO GAMES — cartridges, discs, boxes — but could be a console, controller "
    "or accessory). Look closely at any printed title, logo, platform mark, region "
    "code and cover art. Reply with ONLY a JSON object: {\"candidates\": [ "
    "{\"title\": str, \"platform\": str, \"region\": str, \"category\": one of "
    "video_game|console|accessory|controller|amiibo|trading_card|collectible|merch|"
    "other, \"confidence\": 0.0-1.0} ] ordered best-first (up to 3), "
    "\"barcode\": the digits if a barcode is clearly readable else \"\", "
    "\"notes\": short string}. No prose outside the JSON.")

def _assess_prompt(context: dict) -> str:
    ctx = ""
    if context:
        bits = [f"{k}: {context[k]}" for k in ("title", "platform", "category",
                "completeness", "region") if context.get(k)]
        if bits:
            ctx = " Known details about this exact item — trust these over guessing: " \
                  + "; ".join(bits) + "."
    return (
        "You are a UK reseller grading a second-hand item (video games / consoles / "
        "accessories) for eBay & Vinted." + ctx + " Look at the photo(s) and judge the "
        "PHYSICAL condition of THIS copy — scratches, scuffs, label wear, cracks, "
        "yellowing, missing manual/box, disc scratches. Reply with ONLY a JSON object: "
        "{\"condition\": one of new|like_new|very_good|good|acceptable|for_parts, "
        "\"grade\": 0.0-10.0 (10 = mint), \"completeness\": one of "
        "sealed|cib|boxed_no_manual|cart_only|disc_only|loose|n_a, "
        "\"flaws\": [short strings of any visible defects], "
        "\"title\": a concise eBay-style title (<=80 chars, include platform/edition), "
        "\"description\": 2-3 honest sentences a buyer wants, noting the wear you see, "
        "\"keywords\": [5-8 search terms]}. No prose outside the JSON.")

def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    blob = m.group(0)
    try:
        return json.loads(blob)
    except Exception:
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", blob.replace("'", '"')))
        except Exception:
            return None

async def _vision_json(image_b64: str, image_url: str, prompt: str, model: str):
    media = _media()
    if not media or not hasattr(media, "cap_vision_describe"):
        return None, {"error": "vision model not loaded — pull a VL model "
                               "(e.g. `ollama pull qwen2.5vl`)"}
    res = await media.cap_vision_describe(image_b64=image_b64, image_url=image_url,
                                          prompt=prompt, model=model, trace_id=None)
    if res.get("error"):
        return None, res
    return _extract_json(res.get("text", "")) or {}, res


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    @capability(
        "business.catalog.resolve", http_method="POST",
        http_path="/business/catalog/resolve", http_tags=["commerce"],
        description="Find-or-create the CATALOG product (the title record) that a "
                    "physical unit belongs to. Matches on UPC first, then exact name "
                    "within the store, else creates a new catalog product (qty 0 — "
                    "units drive the count). Optionally runs a barcode lookup to fill "
                    "the title/brand/images. Input: upc (str), title (str), category, "
                    "brand, platform, images (list), lookup (bool default true), "
                    "store_id, force_new (bool). Output: {ok, product, matched(bool), "
                    "lookup}.")
    async def cap_catalog_resolve(
        upc: str = "", title: str = "", category: str = "", brand: str = "",
        platform: str = "", images: List = None, lookup: bool = True,
        store_id: str = "", force_new: bool = False, trace_id=None):
        await _ensure_schema()
        core = _core()
        if not core:
            return {"error": "commerce core module not loaded"}
        upc = re.sub(r"[^0-9Xx]", "", upc or "").strip()
        look = None
        if upc and lookup:
            lst = _listing()
            if lst and hasattr(lst, "_barcode_lookup"):
                try:
                    look = await lst._barcode_lookup(upc)
                except Exception as e:
                    log.debug("barcode lookup: %s", e)
        want_title = title or (look or {}).get("title") or (f"Item {upc}" if upc else "")
        # match existing catalog product
        existing = None
        if not force_new:
            try:
                pool = await _run(core._db_list_products, upc or want_title, "", 25, store_id)
            except Exception:
                pool = []
            if upc:
                existing = next((p for p in pool if str(p.get("upc") or "") == upc), None)
            if not existing and want_title:
                wl = want_title.strip().lower()
                existing = next((p for p in pool
                                 if (p.get("name") or "").strip().lower() == wl), None)
        if existing:
            return {"ok": True, "product": existing, "matched": True, "lookup": look}
        attrs = {}
        if look:
            attrs = {"brand": look.get("brand", ""), "source": look.get("source", ""),
                     "images": look.get("images") or []}
        if platform: attrs["platform"] = platform
        if brand:    attrs["brand"] = brand
        if images:   attrs["images"] = images
        fields = {
            "sku": (f"SKU-{upc[-8:]}" if upc else _new_id("cat").upper()),
            "name": want_title or "Untitled item",
            "description": (look or {}).get("description") or "",
            "category": category or (look or {}).get("category") or "video_game",
            "currency": "GBP", "qty_on_hand": 0, "upc": upc,
            "store_id": store_id or None, "attributes": attrs}
        p = await _run(core._db_upsert_product, fields)
        await emit_event({"type": "commerce.progress", "stage": "catalog.resolve",
                          "message": f"catalog + '{p.get('name','')[:40]}'"})
        return {"ok": True, "product": p, "matched": False, "lookup": look}

    @capability(
        "business.unit.intake", http_method="POST",
        http_path="/business/unit/intake", http_tags=["commerce"],
        schema=enum_schema(condition=CONDITIONS, completeness=COMPLETENESS,
                           category=CATEGORIES),
        description="Stock ONE physical unit in a single step: resolve its catalog "
                    "product (from a barcode or a title), create the unit with its own "
                    "condition / grade / photos / price, and keep the catalog qty in "
                    "sync. Optionally grade + describe it from its photos with the "
                    "vision model. Input: upc (str) OR title (str) [+category, platform, "
                    "brand], product_id (str — attach to a known catalog row instead), "
                    "condition, grade (0-10), completeness, cost, price, currency "
                    "(GBP), photos (list of URLs or data-URIs), location, notes, "
                    "auto_describe (bool — vision grade+describe), auto_price (bool), "
                    "lookup (bool), store_id. Output: {ok, unit, product, lookup, "
                    "assessment}.")
    async def cap_unit_intake(
        upc: str = "", title: str = "", product_id: str = "", category: str = "",
        platform: str = "", brand: str = "", condition: str = "", grade: float = 0.0,
        completeness: str = "", cost: float = 0.0, price: float = 0.0,
        currency: str = "GBP", photos: List = None, location: str = "", notes: str = "",
        description: str = "", auto_describe: bool = False, auto_price: bool = False,
        lookup: bool = True, store_id: str = "", trace_id=None):
        await _ensure_schema()
        core = _core()
        if not core:
            return {"error": "commerce core module not loaded"}
        # 1 — catalog product
        prod = None; look = None
        if product_id:
            prod = await _run(core._db_get_product, product_id)
        if not prod:
            res = await cap_catalog_resolve(upc=upc, title=title, category=category,
                                            platform=platform, brand=brand,
                                            lookup=lookup, store_id=store_id, trace_id=None)
            if res.get("error"):
                return res
            prod, look = res.get("product"), res.get("lookup")
        if not prod:
            return {"error": "could not resolve a catalog product (give upc or title)"}
        # 2 — persist photos
        stored = await _persist_photos(photos or [])
        # 3 — create the unit
        attrs = {}
        if platform: attrs["platform"] = platform
        if notes:    attrs["notes"] = notes
        unit = await _run(_db_upsert_unit, {
            "product_id": prod["id"], "store_id": store_id or prod.get("store_id") or "",
            "sku": f"{prod.get('sku') or 'U'}-{uuid.uuid4().hex[:4].upper()}",
            "upc": prod.get("upc") or upc, "title": prod.get("name") or title,
            "category": category or prod.get("category") or "video_game",
            "condition": condition or "good", "grade": grade,
            "completeness": completeness or "", "cost": cost, "price": price,
            "currency": currency or "GBP", "photos": stored,
            "description": description or "",
            "location": location or "", "attributes": attrs, "status": "in_stock"})
        await _run(_sync_product_qty_sync, prod["id"])
        # 4 — optional vision grade + description (uses the just-stored photos)
        assessment = None
        if auto_describe and stored:
            assessment = await _assess_and_apply(unit, prod)
            unit = assessment.get("unit", unit) if assessment else unit
        # 5 — optional price suggestion
        if auto_price and not _f(price):
            sug = await _suggest_unit_price(prod, unit)
            if sug:
                unit = await _run(_db_upsert_unit, {"id": unit["id"], "price": sug})
        await emit_event({"type": "commerce.progress", "stage": "unit.intake",
                          "message": f"+unit '{unit.get('title','')[:36]}' "
                                     f"({unit.get('condition')})"})
        return {"ok": True, "unit": unit, "product": prod, "lookup": look,
                "assessment": (assessment or {}).get("draft") if assessment else None}

    async def _suggest_unit_price(prod: dict, unit: dict) -> Optional[float]:
        # prefer the live market reprice cap, fall back to the pricing engine
        raw = _cap_raw("business.market.reprice")
        if raw:
            try:
                r = await raw(product_id=prod["id"], trace_id=None)
                if r and r.get("suggested_price"):
                    return _f(r["suggested_price"])
            except Exception:
                pass
        pricing = _pricing()
        if pricing and hasattr(pricing, "cap_price_suggest"):
            try:
                s = await pricing.cap_price_suggest(product_id=prod["id"], margin=0.30,
                                                    undercut=0.03, trace_id=None)
                if s.get("ok"):
                    return _f(s.get("suggested_price"))
            except Exception:
                pass
        return None

    async def _assess_and_apply(unit: dict, prod: dict) -> Optional[dict]:
        """Grade + describe a unit from its first stored photo and write the result
        back onto the unit. Returns {draft, unit}."""
        photos = unit.get("photos") or []
        if not photos:
            return None
        ctx = {"title": prod.get("name"), "platform": (prod.get("attributes") or {}).get("platform"),
               "category": unit.get("category"), "completeness": unit.get("completeness")}
        first = photos[0]
        if str(first).startswith(("http", "/")):
            image_b64, image_url = "", first
        else:
            image_b64, image_url = first, ""
        draft, res = await _vision_json(image_b64, image_url, _assess_prompt(ctx), "")
        if not draft:
            return {"draft": None, "unit": unit, "error": (res or {}).get("error")}
        patch = {"id": unit["id"]}
        if draft.get("condition") in CONDITIONS: patch["condition"] = draft["condition"]
        if draft.get("grade") is not None:       patch["grade"] = _f(draft.get("grade"))
        if draft.get("completeness") in COMPLETENESS: patch["completeness"] = draft["completeness"]
        if draft.get("description"): patch["description"] = str(draft["description"])
        if draft.get("title"):      patch["title"] = str(draft["title"])[:80]
        patch["quality"] = {"grade": _f(draft.get("grade")),
                            "flaws": draft.get("flaws") or [],
                            "keywords": draft.get("keywords") or [],
                            "assessed_by": (res or {}).get("model", "")}
        saved = await _run(_db_upsert_unit, patch)
        return {"draft": draft, "unit": saved}

    @capability(
        "business.unit.list", http_method="GET", http_path="/business/unit/list",
        http_tags=["commerce"], memory="off", silent=True,
        schema=enum_schema(status=UNIT_STATUS),
        description="List physical units (individual copies). Input: product_id (all "
                    "copies of one title), status (in_stock|listed|sold|reserved|"
                    "scrapped), store_id, q (title/sku/upc search), limit. "
                    "Output: {units:[...], count}.")
    async def cap_unit_list(product_id: str = "", status: str = "", store_id: str = "",
                            q: str = "", limit: int = 300, trace_id=None):
        await _ensure_schema()
        rows = await _run(_db_list_units, product_id, status, store_id, q, int(limit))
        return {"units": rows, "count": len(rows)}

    @capability(
        "business.unit.get", http_method="GET", http_path="/business/unit/get",
        http_tags=["commerce"], memory="off", silent=True,
        description="Fetch one unit by id, with its catalog product attached. "
                    "Input: id (str!). Output: {ok, unit, product}.")
    async def cap_unit_get(id: str = "", trace_id=None):
        await _ensure_schema()
        u = await _run(_db_get_unit, id)
        if not u:
            return {"error": "unit not found"}
        core = _core()
        prod = await _run(core._db_get_product, u["product_id"]) if core else None
        return {"ok": True, "unit": u, "product": prod}

    @capability(
        "business.unit.upsert", http_method="POST", http_path="/business/unit/upsert",
        http_tags=["commerce"],
        schema=enum_schema(condition=CONDITIONS, completeness=COMPLETENESS,
                           status=UNIT_STATUS, category=CATEGORIES),
        description="Create or edit a unit. Input: id (blank = new), product_id, "
                    "title, category, condition, grade (0-10), completeness, cost, "
                    "price, currency, photos (list of URLs/data-URIs — data-URIs are "
                    "persisted), description, location, status, store_id, attributes "
                    "(dict). Output: {ok, unit}.")
    async def cap_unit_upsert(
        id: str = "", product_id: str = "", title: str = "", category: str = "",
        condition: str = "", grade: float = None, completeness: str = "",
        cost: float = None, price: float = None, currency: str = "",
        photos: List = None, description: str = None, location: str = None,
        status: str = "", store_id: str = "", attributes: Dict = None, trace_id=None):
        await _ensure_schema()
        fields = {"id": id or "", "product_id": product_id or None, "title": title or None,
                  "category": category or None, "condition": condition or None,
                  "grade": grade, "completeness": completeness or None, "cost": cost,
                  "price": price, "currency": currency or None,
                  "description": description, "location": location,
                  "status": status or None, "store_id": store_id or None,
                  "attributes": attributes}
        if photos is not None:
            fields["photos"] = await _persist_photos(photos)
        u = await _run(_db_upsert_unit, {k: v for k, v in fields.items() if v is not None or k == "id"})
        if u.get("product_id"):
            await _run(_sync_product_qty_sync, u["product_id"])
        return {"ok": True, "unit": u}

    @capability(
        "business.unit.delete", http_method="POST", http_path="/business/unit/delete",
        http_tags=["commerce"],
        description="Delete a unit and resync its catalog quantity. Input: id (str!). "
                    "Output: {ok}.")
    async def cap_unit_delete(id: str = "", trace_id=None):
        await _ensure_schema()
        pid = await _run(_db_delete_unit, id)
        if pid:
            await _run(_sync_product_qty_sync, pid)
        return {"ok": True}

    @capability(
        "business.vision.identify_item", http_method="POST",
        http_path="/business/vision/identify_item", http_tags=["commerce", "vision"],
        description="Identify an item that has NO scannable barcode (loose game "
                    "cartridges, discs, etc.) from a photo — title / platform / region "
                    "candidates with confidence, and it reads a barcode out of the "
                    "photo if one is visible. Input: image_b64 (str — base64/data-URI) "
                    "OR image_url (str), category (hint), model (str). "
                    "Output: {ok, candidates:[{title,platform,region,category,"
                    "confidence}], barcode, notes, raw} or {error}.")
    async def cap_vision_identify_item(image_b64: str = "", image_url: str = "",
                                       category: str = "", model: str = "", trace_id=None):
        if not (image_b64 or image_url):
            return {"error": "image_b64 or image_url required"}
        draft, res = await _vision_json(image_b64, image_url, _IDENTIFY_PROMPT, model)
        if draft is None:
            return res
        cands = draft.get("candidates") or []
        if not cands and draft.get("title"):
            cands = [{"title": draft.get("title"), "platform": draft.get("platform", ""),
                      "region": draft.get("region", ""),
                      "category": draft.get("category", category or "video_game"),
                      "confidence": _f(draft.get("confidence"), 0.5)}]
        await emit_event({"type": "commerce.progress", "stage": "vision.identify",
                          "message": f"vision id → '{(cands[0].get('title') if cands else '')[:40]}'"})
        return {"ok": True, "candidates": cands, "barcode": draft.get("barcode", ""),
                "notes": draft.get("notes", ""), "raw": (res or {}).get("text", ""),
                "model": (res or {}).get("model")}

    @capability(
        "business.vision.assess_unit", http_method="POST",
        http_path="/business/vision/assess_unit", http_tags=["commerce", "vision"],
        description="Grade a unit's condition and write its per-unit description from "
                    "its photo(s), WITH the catalog details in context so the copy is "
                    "described accurately. Pass unit_id to assess a stored unit and "
                    "write the result back, OR pass image(s) + context ad-hoc. "
                    "Input: unit_id (str), image_b64 (str), image_url (str), "
                    "context (dict: title/platform/category/completeness), apply (bool "
                    "default true when unit_id given), model. Output: {ok, draft:"
                    "{condition,grade,completeness,flaws,title,description,keywords}, "
                    "unit?} or {error}.")
    async def cap_vision_assess_unit(unit_id: str = "", image_b64: str = "",
                                     image_url: str = "", context: Dict = None,
                                     apply: bool = True, model: str = "", trace_id=None):
        await _ensure_schema()
        core = _core()
        if unit_id:
            unit = await _run(_db_get_unit, unit_id)
            if not unit:
                return {"error": "unit not found"}
            prod = await _run(core._db_get_product, unit["product_id"]) if core else {}
            if not (unit.get("photos") or image_b64 or image_url):
                return {"error": "unit has no photos to assess"}
            if not (image_b64 or image_url) and unit.get("photos"):
                first = unit["photos"][0]
                if str(first).startswith(("http", "/")): image_url = first
                else: image_b64 = first
            r = await _assess_and_apply(unit, prod or {}) if apply else None
            if r and r.get("draft"):
                return {"ok": True, "draft": r["draft"], "unit": r["unit"]}
            draft, res = await _vision_json(image_b64, image_url,
                                            _assess_prompt(context or {
                                                "title": unit.get("title"),
                                                "completeness": unit.get("completeness")}), model)
            if draft is None:
                return res
            return {"ok": True, "draft": draft, "unit": unit}
        if not (image_b64 or image_url):
            return {"error": "unit_id or an image is required"}
        draft, res = await _vision_json(image_b64, image_url, _assess_prompt(context or {}), model)
        if draft is None:
            return res
        if draft.get("title"):
            draft["title"] = str(draft["title"])[:80]
        return {"ok": True, "draft": draft, "raw": (res or {}).get("text", "")}

    @capability(
        "business.unit.draft_listing", http_method="POST",
        http_path="/business/unit/draft_listing", http_tags=["commerce"],
        schema=enum_schema(platform=["ebay", "vinted"]),
        description="Draft a marketplace listing FROM a unit — using that unit's own "
                    "price, condition, photos and description — via the existing "
                    "listing engine, then link the draft back to the unit and mark it "
                    "'listed'. Input: unit_id (str!), platform (ebay|vinted), "
                    "price (float — override). Output: {ok, listing, unit}.")
    async def cap_unit_draft_listing(unit_id: str = "", platform: str = "ebay",
                                     price: float = None, trace_id=None):
        await _ensure_schema()
        lst = _listing()
        if not lst or not hasattr(lst, "cap_listing_draft"):
            return {"error": "commerce_listing module not loaded"}
        unit = await _run(_db_get_unit, unit_id)
        if not unit:
            return {"error": "unit not found"}
        dr = await lst.cap_listing_draft(
            product_id=unit["product_id"], platform=platform,
            condition=unit.get("condition") or "good",
            category=unit.get("category") or "",
            title=unit.get("title") or "", description=unit.get("description") or "",
            price=price if price is not None else (_f(unit.get("price")) or None),
            photos=unit.get("photos") or [], auto_price=not _f(unit.get("price")),
            trace_id=None)
        if dr.get("error"):
            return dr
        listing = dr.get("listing") or {}
        saved = await _run(_db_upsert_unit, {"id": unit_id, "status": "listed",
                                             "listing_id": listing.get("id") or ""})
        return {"ok": True, "listing": listing, "unit": saved}

    # ── Scan Station panel ────────────────────────────────────────────────────

    _HERE = _Path(__file__).parent

    @APP.get("/business/intake/panel", include_in_schema=False)
    @APP.get("/commerce/intake/panel", include_in_schema=False)
    async def _commerce_intake_panel_route():
        from fastapi.responses import HTMLResponse
        p = _HERE / "commerce_intake_panel.html"
        if p.exists():
            return HTMLResponse(p.read_text(encoding="utf-8"))
        return HTMLResponse("<p style='color:red'>commerce_intake_panel.html not found</p>")

    log.info("business.intake: ready (per-unit intake + vision id/grade + Scan Station)")
