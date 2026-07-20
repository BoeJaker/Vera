"""
commerce_listing.py — Barcode intake, vision item-ID & one-click listings
==========================================================================

The "get it listed fast" surface, plus the panel route for the whole reselling
operation.

  • **Barcode intake** — a USB barcode scanner behaves like a keyboard, so the
    panel just captures the digits; ``commerce.barcode.lookup`` resolves a
    UPC/EAN/ISBN to a title / brand / category / images (keyless UPCitemdb trial,
    OpenLibrary for books, web-search fallback), and ``commerce.scan.intake``
    turns a scan straight into an inventory product (or bumps its quantity).
  • **Vision item-ID** — ``commerce.vision.describe_item`` sends a photo to the
    ``vision.describe`` model with a selling-oriented prompt and returns a draft
    title, condition, description, category and keywords.
  • **One-click listings** — ``commerce.listing.draft`` builds a full listing
    (title ≤ 80 chars, description, item specifics, a suggested price from the
    live pricing engine) from a product; ``commerce.listing.publish`` pushes it
    live on eBay (inventory item → offer → publish) and/or Vinted;
    ``commerce.listing.archive`` ends it. ``commerce.ebay.defaults`` stores the
    eBay business-policy / location / category ids publishing needs.

Storage: shared Data-Fabric SQLite db. Money is GBP by default.
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

log = logging.getLogger("vera.commerce.listing")

try:
    from Vera.vera.capability_orchestration import (
        APP, capability, emit_event, now_iso, enum_schema, CAPABILITY_REGISTRY,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.commerce.listing").warning("commerce listing unavailable: %s", e)
    _CAP_AVAILABLE = False

try:
    import httpx
    HAS_HTTPX = True
except Exception:                              # pragma: no cover
    HAS_HTTPX = False

LISTING_PLATFORMS = ["ebay", "vinted"]
LISTING_STATUS = ["draft", "published", "archived", "error"]
ITEM_CONDITIONS = ["new", "like_new", "very_good", "good", "acceptable", "for_parts"]
CATEGORIES = ["video_game", "console", "accessory", "controller", "merch",
              "collectible", "amiibo", "trading_card", "other"]

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
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

def _core():        return sys.modules.get("commerce_capabilities")
def _platforms():   return sys.modules.get("commerce_platforms")
def _pricing():     return sys.modules.get("commerce_pricing_capabilities")
def _media():       return sys.modules.get("media_capabilities")

def _cap_raw(name: str):
    reg = CAPABILITY_REGISTRY.get(name) if CAPABILITY_REGISTRY else None
    return reg.get("raw") if reg else None


# ─────────────────────────────────────────────────────────────────────────────
# Schema — listings
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_schema_sync():
    global _SCHEMA_READY
    conn = _sqlite_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS commerce_listings (
                id             TEXT PRIMARY KEY,
                product_id     TEXT,
                store_id       TEXT,
                sku            TEXT,
                platform       TEXT,
                account_id     TEXT,
                title          TEXT,
                description    TEXT,
                condition      TEXT,
                category       TEXT,
                category_id    TEXT,
                price          REAL DEFAULT 0,
                currency       TEXT DEFAULT 'GBP',
                item_specifics TEXT,
                photos         TEXT,
                status         TEXT DEFAULT 'draft',
                external_id    TEXT,
                offer_id       TEXT,
                listing_id     TEXT,
                url            TEXT,
                last_error     TEXT,
                created_at     TEXT,
                updated_at     TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_listings_product ON commerce_listings(product_id);
            CREATE INDEX IF NOT EXISTS ix_listings_status ON commerce_listings(status);
        """)
        try:
            have = [r[1] for r in conn.execute("PRAGMA table_info(commerce_listings)").fetchall()]
            if "store_id" not in have:
                conn.execute("ALTER TABLE commerce_listings ADD COLUMN store_id TEXT")
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()
    _SCHEMA_READY = True

async def _ensure_schema():
    if not _SCHEMA_READY:
        await _run(_ensure_schema_sync)

_L_COLS = ("id", "product_id", "store_id", "sku", "platform", "account_id", "title",
           "description", "condition", "category", "category_id", "price", "currency",
           "item_specifics", "photos", "status", "external_id", "offer_id",
           "listing_id", "url", "last_error", "created_at", "updated_at")
_L_JSON = {"item_specifics", "photos"}

def _listing_out(row) -> Optional[dict]:
    if not row:
        return None
    d = dict(row)
    for c in _L_JSON:
        try:
            d[c] = json.loads(d.get(c) or ("{}" if c == "item_specifics" else "[]"))
        except Exception:
            d[c] = {} if c == "item_specifics" else []
    return d

def _db_upsert_listing(fields: dict) -> dict:
    conn = _sqlite_conn()
    try:
        lid = fields.get("id") or ""
        existing = conn.execute("SELECT * FROM commerce_listings WHERE id=?",
                                (lid,)).fetchone() if lid else None
        base = _listing_out(existing) if existing else {}
        lid = base.get("id") or lid or _new_id("lst")
        merged = {**base, **{k: v for k, v in fields.items() if v is not None}}
        now = now_iso()
        row = {}
        for c in _L_COLS:
            if c == "id": row[c] = lid
            elif c == "created_at": row[c] = base.get("created_at") or now
            elif c == "updated_at": row[c] = now
            elif c in _L_JSON:
                row[c] = json.dumps(merged.get(c) if merged.get(c) is not None
                                    else ({} if c == "item_specifics" else []))
            elif c == "price": row[c] = _f(merged.get(c))
            else: row[c] = merged.get(c, "")
        conn.execute("INSERT OR REPLACE INTO commerce_listings (%s) VALUES (%s)" %
                     (",".join(_L_COLS), ",".join("?" for _ in _L_COLS)),
                     tuple(row[c] for c in _L_COLS))
        conn.commit()
        return _listing_out(conn.execute("SELECT * FROM commerce_listings WHERE id=?",
                                         (lid,)).fetchone())
    finally:
        conn.close()

def _db_get_listing(lid: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM commerce_listings WHERE id=?", (lid,)).fetchone()
        return _listing_out(r)
    finally:
        conn.close()

def _db_list_listings(status: str = "", platform: str = "", product_id: str = "",
                      limit: int = 300, store_id: str = "") -> List[dict]:
    conn = _sqlite_conn()
    try:
        sql = "SELECT * FROM commerce_listings"; where, args = [], []
        if status:     where.append("status=?"); args.append(status)
        if platform:   where.append("platform=?"); args.append(platform)
        if product_id: where.append("product_id=?"); args.append(product_id)
        if store_id:   where.append("store_id=?"); args.append(store_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"; args.append(int(limit))
        return [_listing_out(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Barcode lookup
# ─────────────────────────────────────────────────────────────────────────────

def _clean_barcode(code: str) -> str:
    return re.sub(r"[^0-9Xx]", "", code or "").strip()

async def _upcitemdb(code: str) -> Optional[dict]:
    if not HAS_HTTPX:
        return None
    try:
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": _UA}) as c:
            r = await c.get("https://api.upcitemdb.com/prod/trial/lookup",
                            params={"upc": code})
            if r.status_code != 200:
                return None
            data = r.json()
    except Exception as e:
        log.debug("upcitemdb failed: %s", e)
        return None
    items = data.get("items") or []
    if not items:
        return None
    it = items[0]
    return {"title": it.get("title", ""), "brand": it.get("brand", ""),
            "category": it.get("category", ""),
            "description": it.get("description", ""),
            "images": it.get("images", [])[:8],
            "model": it.get("model", ""), "source": "upcitemdb"}

async def _openlibrary(code: str) -> Optional[dict]:
    if not HAS_HTTPX or len(code) not in (10, 13):
        return None
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _UA}) as c:
            r = await c.get("https://openlibrary.org/api/books",
                            params={"bibkeys": f"ISBN:{code}", "format": "json",
                                    "jscmd": "data"})
            data = r.json()
    except Exception:
        return None
    rec = data.get(f"ISBN:{code}")
    if not rec:
        return None
    return {"title": rec.get("title", ""),
            "brand": ", ".join(a.get("name", "") for a in rec.get("authors", [])),
            "category": "book", "description": "",
            "images": [rec.get("cover", {}).get("large", "")] if rec.get("cover") else [],
            "source": "openlibrary"}

async def _barcode_web(code: str) -> Optional[dict]:
    raw = _cap_raw("web.search")
    if not raw:
        return None
    try:
        res = await raw(query=f"{code} product barcode", limit=5, discover="off", trace_id=None)
    except Exception:
        return None
    results = (res or {}).get("results", []) if isinstance(res, dict) else []
    if not results:
        return None
    top = results[0]
    return {"title": top.get("title", ""), "brand": "", "category": "",
            "description": top.get("snippet", ""), "images": [], "source": "web"}

async def _barcode_lookup(code: str) -> Optional[dict]:
    code = _clean_barcode(code)
    if not code:
        return None
    return (await _upcitemdb(code) or await _openlibrary(code)
            or await _barcode_web(code))


# ─────────────────────────────────────────────────────────────────────────────
# Vision helpers
# ─────────────────────────────────────────────────────────────────────────────

_VISION_PROMPT = (
    "You are a UK reseller listing this item on eBay and Vinted (video games, "
    "consoles, merch, accessories). Look at the photo and reply with ONLY a JSON "
    "object with keys: title (a concise eBay-style title, max 80 chars, include "
    "platform/brand/edition if visible), brand, platform, category (one of "
    "video_game, console, accessory, controller, merch, collectible, trading_card, "
    "other), condition (new, like_new, very_good, good, acceptable, for_parts), "
    "description (2-3 sentences a buyer would want, note any wear/damage seen), "
    "keywords (array of 5-8 search terms). No prose outside the JSON.")

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
        # tolerate trailing commas / single quotes
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", blob.replace("'", '"')))
        except Exception:
            return None


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    @capability(
        "business.barcode.lookup", http_method="POST",
        http_path="/business/barcode/lookup", http_tags=["commerce"],
        description="Resolve a UPC / EAN / ISBN barcode to product metadata "
                    "(title, brand, category, images) via keyless sources — UPCitemdb "
                    "trial, OpenLibrary for books, then a web-search fallback. "
                    "Input: code (str! — the scanned digits). "
                    "Output: {ok, product:{title,brand,category,description,images,source}} "
                    "or {error}.")
    async def cap_barcode_lookup(code: str = "", trace_id=None):
        c = _clean_barcode(code)
        if not c:
            return {"error": "code required"}
        await emit_event({"type": "commerce.progress", "stage": "barcode.lookup",
                          "message": f"barcode {c}"})
        info = await _barcode_lookup(c)
        if not info:
            return {"error": f"no product found for barcode {c}"}
        info["upc"] = c
        return {"ok": True, "product": info}

    @capability(
        "business.scan.intake", http_method="POST",
        http_path="/business/scan/intake", http_tags=["commerce"],
        description="Scan an item into inventory in one step: look up the barcode, then "
                    "create a product (or bump quantity if the UPC already exists). "
                    "Input: code (str! — scanned barcode), qty (int default 1), "
                    "cost (float — what you paid), price (float — intended sale price), "
                    "condition (str), category (str), location (str — bin/shelf), "
                    "currency (default GBP). Output: {ok, product, matched(bool), lookup}.")
    async def cap_scan_intake(
        code: str = "", qty: int = 1, cost: float = 0.0, price: float = 0.0,
        condition: str = "", category: str = "", location: str = "",
        currency: str = "GBP", trace_id=None):
        await _ensure_schema()
        core = _core()
        if not core:
            return {"error": "commerce core module not loaded"}
        c = _clean_barcode(code)
        if not c:
            return {"error": "code required"}
        # already have this UPC?
        existing = None
        try:
            found = await _run(core._db_list_products, c, "", 5)
            existing = next((p for p in found if str(p.get("upc")) == c), None)
        except Exception:
            existing = None
        lookup = await _barcode_lookup(c)
        if existing:
            p = await _run(core._db_adjust_stock, existing["id"], abs(_i(qty, 1)) or 1,
                           "restock", "", f"scan intake {c}")
            return {"ok": True, "product": p, "matched": True, "lookup": lookup}
        attrs = {"upc": c}
        if lookup:
            attrs["images"] = lookup.get("images") or []
            attrs["brand"] = lookup.get("brand", "")
            attrs["source"] = lookup.get("source", "")
        fields = {
            "sku": f"SKU-{c[-8:]}" if c else None,
            "name": (lookup or {}).get("title") or f"Item {c}",
            "description": (lookup or {}).get("description") or "",
            "category": category or (lookup or {}).get("category") or "",
            "cost": cost, "price": price, "currency": currency,
            "qty_on_hand": abs(_i(qty, 1)) or 1, "upc": c,
            "location": location or None, "attributes": attrs}
        p = await _run(core._db_upsert_product, fields)
        await emit_event({"type": "commerce.progress", "stage": "scan.intake",
                          "message": f"intake {p.get('name')} ×{qty}"})
        return {"ok": True, "product": p, "matched": False, "lookup": lookup}

    @capability(
        "business.vision.describe_item", http_method="POST",
        http_path="/business/vision/describe_item", http_tags=["commerce", "vision"],
        description="Look at a photo of an item and return a ready-to-list draft "
                    "(title, brand, platform, category, condition, description, "
                    "keywords) using the vision model. Input: image_b64 (str — base64 or "
                    "data URI) OR image_url (str — http(s) or a fabric path like "
                    "/images/file/x.png), model (str — optional). "
                    "Output: {ok, draft:{title,brand,category,condition,description,"
                    "keywords}, raw} or {error}.")
    async def cap_vision_describe_item(image_b64: str = "", image_url: str = "",
                                       model: str = "", trace_id=None):
        media = _media()
        if not media or not hasattr(media, "cap_vision_describe"):
            return {"error": "vision module (media_capabilities) not loaded — pull a "
                            "vision model (e.g. ollama pull qwen2.5vl)"}
        if not (image_b64 or image_url):
            return {"error": "image_b64 or image_url required"}
        res = await media.cap_vision_describe(image_b64=image_b64, image_url=image_url,
                                              prompt=_VISION_PROMPT, model=model, trace_id=None)
        if res.get("error"):
            return res
        draft = _extract_json(res.get("text", "")) or {}
        if draft.get("title"):
            draft["title"] = str(draft["title"])[:80]
        await emit_event({"type": "commerce.progress", "stage": "vision.describe_item",
                          "message": f"vision → '{draft.get('title', '')[:48]}'"})
        return {"ok": True, "draft": draft, "raw": res.get("text", ""),
                "model": res.get("model")}

    @capability(
        "business.ebay.defaults", http_method="POST",
        http_path="/business/ebay/defaults", http_tags=["commerce"],
        description="Store the eBay business-policy / location / category ids that "
                    "publishing a live offer needs, onto an eBay platform account. Get "
                    "these from eBay: Account → Business policies + Inventory locations. "
                    "Input: account_id (str! — the eBay account), fulfillment_policy_id, "
                    "payment_policy_id, return_policy_id, merchant_location_key, "
                    "category_id (default leaf category), marketplace (default EBAY_GB). "
                    "Output: {ok, account} (secrets redacted).")
    async def cap_ebay_defaults(
        account_id: str = "", fulfillment_policy_id: str = "", payment_policy_id: str = "",
        return_policy_id: str = "", merchant_location_key: str = "", category_id: str = "",
        marketplace: str = "", store_id: str = "", trace_id=None):
        plat = _platforms()
        if not plat:
            return {"error": "commerce_platforms module not loaded"}
        await plat._ensure_schema()
        if not account_id:
            return {"error": "account_id required"}
        patch = {"fulfillment_policy_id": fulfillment_policy_id,
                 "payment_policy_id": payment_policy_id,
                 "return_policy_id": return_policy_id,
                 "merchant_location_key": merchant_location_key,
                 "category_id": category_id, "marketplace": marketplace,
                 "store_id": store_id}
        acct = await _run(plat._db_patch_creds, account_id, patch)
        if not acct:
            return {"error": "account not found"}
        return {"ok": True, "account": acct}

    @capability(
        "business.listing.draft", http_method="POST",
        http_path="/business/listing/draft", http_tags=["commerce"],
        schema=enum_schema(platform=LISTING_PLATFORMS, condition=ITEM_CONDITIONS,
                           category=CATEGORIES),
        description="Build a listing draft from a product: title (≤80), description, "
                    "item specifics and a suggested price from the live pricing engine "
                    "(never below cost+margin). Input: product_id (str!), platform "
                    "(ebay|vinted), condition, category, title (str — override), "
                    "description (str — override), price (float — override; else "
                    "auto-suggested), item_specifics (dict), photos (list of URLs), "
                    "auto_price (bool default true). Output: {ok, listing}.")
    async def cap_listing_draft(
        product_id: str = "", platform: str = "ebay", condition: str = "good",
        category: str = "", title: str = "", description: str = "", price: float = None,
        item_specifics: Dict = None, photos: List = None, auto_price: bool = True,
        trace_id=None):
        await _ensure_schema()
        core = _core()
        if not core:
            return {"error": "commerce core module not loaded"}
        prod = await _run(core._db_get_product, product_id)
        if not prod:
            return {"error": "product not found"}
        attrs = prod.get("attributes") or {}
        gen_title = title or prod.get("name") or ""
        gen_title = gen_title[:80]
        gen_desc = description or prod.get("description") or prod.get("name") or ""
        photos = photos or attrs.get("images") or []
        # price
        final_price = price
        basis = "override"
        if final_price is None and auto_price:
            pricing = _pricing()
            if pricing and hasattr(pricing, "cap_price_suggest"):
                try:
                    sug = await pricing.cap_price_suggest(product_id=prod["id"],
                                                          margin=0.30, undercut=0.03, trace_id=None)
                    if sug.get("ok"):
                        final_price = sug["suggested_price"]; basis = sug.get("basis", "market")
                except Exception as e:
                    log.debug("price suggest failed: %s", e)
        if final_price is None:
            final_price = _f(prod.get("price"))
            basis = "product"
        specifics = dict(item_specifics or {})
        for k in ("brand", "platform", "model"):
            if attrs.get(k) and k not in specifics:
                specifics[k.title()] = attrs[k]
        specifics.setdefault("Condition", condition)
        listing = await _run(_db_upsert_listing, {
            "product_id": prod["id"], "store_id": prod.get("store_id") or "",
            "sku": prod.get("sku"), "platform": platform,
            "title": gen_title, "description": gen_desc, "condition": condition,
            "category": category or attrs.get("category") or "",
            "category_id": attrs.get("ebay_category_id") or "",
            "price": final_price, "currency": prod.get("currency") or "GBP",
            "item_specifics": specifics, "photos": photos, "status": "draft"})
        listing["price_basis"] = basis
        await emit_event({"type": "commerce.progress", "stage": "listing.draft",
                          "message": f"draft '{gen_title[:40]}' £{_f(final_price):.2f}"})
        return {"ok": True, "listing": listing}

    @capability(
        "business.listing.list", http_method="GET",
        http_path="/business/listing/list", http_tags=["commerce"],
        memory="off", silent=True,
        schema=enum_schema(status=LISTING_STATUS, platform=LISTING_PLATFORMS),
        description="List saved listings/drafts. Input: status (draft|published|"
                    "archived|error), platform, product_id, store_id (scope to a store), "
                    "limit. Output: {listings:[...], count}.")
    async def cap_listing_list(status: str = "", platform: str = "", product_id: str = "",
                               limit: int = 300, store_id: str = "", trace_id=None):
        await _ensure_schema()
        rows = await _run(_db_list_listings, status, platform, product_id, int(limit), store_id)
        return {"listings": rows, "count": len(rows)}

    @capability(
        "business.listing.publish", http_method="POST",
        http_path="/business/listing/publish", http_tags=["commerce"],
        description="Publish a draft LIVE on its platform in one click. eBay: syncs the "
                    "product (price/qty/photos) then creates & publishes the offer (needs "
                    "business.ebay.defaults set once). Vinted: creates the item (photos "
                    "usually uploaded in-app first). Input: listing_id (str! — a draft), "
                    "account_id (str — platform account; else the first for that "
                    "platform), price (float — override), category_id (str — eBay leaf). "
                    "Output: {ok, listing, result} or {error}.")
    async def cap_listing_publish(listing_id: str = "", account_id: str = "",
                                  price: float = None, category_id: str = "", trace_id=None):
        await _ensure_schema()
        core = _core(); plat = _platforms()
        if not (core and plat):
            return {"error": "commerce core/platforms not loaded"}
        listing = await _run(_db_get_listing, listing_id)
        if not listing:
            return {"error": "listing not found"}
        prod = await _run(core._db_get_product, listing["product_id"])
        if not prod:
            return {"error": "product for this listing no longer exists"}
        platform = listing["platform"]
        acct = (await _run(plat._db_get_account, account_id, True) if account_id
                else await _run(plat._db_first_account, platform, True))
        if not acct:
            return {"error": f"no connected {platform} account — connect one first"}
        conn = plat.CONNECTORS.get(platform)
        if not conn:
            return {"error": f"no connector for '{platform}'"}
        # sync product with the listing's price / photos before publishing
        final_price = price if price is not None else _f(listing.get("price"))
        attrs = dict(prod.get("attributes") or {})
        if listing.get("photos"):
            attrs["images"] = listing["photos"]
        if category_id or listing.get("category_id"):
            attrs["ebay_category_id"] = category_id or listing.get("category_id")
        await _run(core._db_upsert_product, {
            "id": prod["id"], "price": final_price,
            "description": listing.get("description") or prod.get("description"),
            "name": listing.get("title") or prod.get("name"), "attributes": attrs})
        prod = await _run(core._db_get_product, prod["id"])
        # publish
        if platform == "ebay" and hasattr(conn, "publish_listing"):
            opts = {"price": final_price, "currency": listing.get("currency") or "GBP",
                    "category_id": category_id or listing.get("category_id")}
            res = await conn.publish_listing(acct, prod, opts)
        else:
            res = await conn.push_listing(acct, prod)
        patch = {"id": listing_id, "account_id": acct["id"], "price": final_price}
        if res.get("ok") or res.get("listing_id") or res.get("external_id"):
            patch.update({"status": "published",
                          "external_id": res.get("external_id") or res.get("listing_id") or "",
                          "offer_id": res.get("offer_id") or "",
                          "listing_id": res.get("listing_id") or "",
                          "url": res.get("url") or "", "last_error": ""})
        else:
            patch.update({"status": "error", "last_error": res.get("error", "unknown")})
        saved = await _run(_db_upsert_listing, patch)
        await emit_event({"type": "commerce.progress", "stage": "listing.publish",
                          "message": f"{platform}: {saved['status']} '{saved.get('title','')[:36]}'"})
        return {"ok": bool(res.get("ok")), "listing": saved, "result": res}

    @capability(
        "business.listing.archive", http_method="POST",
        http_path="/business/listing/archive", http_tags=["commerce"],
        description="End / archive a listing. If it is live it is withdrawn on the "
                    "platform (eBay offer withdraw / Vinted delete), then marked "
                    "archived locally. Input: listing_id (str!). "
                    "Output: {ok, listing, result}.")
    async def cap_listing_archive(listing_id: str = "", trace_id=None):
        await _ensure_schema()
        plat = _platforms()
        listing = await _run(_db_get_listing, listing_id)
        if not listing:
            return {"error": "listing not found"}
        result = {"ok": True, "note": "was a draft — nothing live to withdraw"}
        if listing.get("status") == "published" and plat:
            acct = await _run(plat._db_get_account, listing.get("account_id"), True) \
                if listing.get("account_id") else await _run(plat._db_first_account,
                                                             listing["platform"], True)
            conn = plat.CONNECTORS.get(listing["platform"]) if acct else None
            if conn and hasattr(conn, "archive_listing") and acct:
                ext = listing.get("offer_id") or listing.get("external_id") or listing.get("listing_id")
                result = await conn.archive_listing(acct, external_id=ext,
                                                    sku=listing.get("sku") or "")
        saved = await _run(_db_upsert_listing, {"id": listing_id, "status": "archived"})
        await emit_event({"type": "commerce.progress", "stage": "listing.archive",
                          "message": f"archived '{saved.get('title','')[:36]}'"})
        return {"ok": True, "listing": saved, "result": result}

    # ── Panel route (the whole reselling operation UI) ─────────────────────────

    _HERE = _Path(__file__).parent

    @APP.get("/business/store/panel", include_in_schema=False)
    @APP.get("/commerce/uk/panel", include_in_schema=False)
    async def _commerce_uk_panel_route():
        from fastapi.responses import HTMLResponse
        p = _HERE / "commerce_uk_panel.html"
        if p.exists():
            return HTMLResponse(p.read_text(encoding="utf-8"))
        return HTMLResponse("<p style='color:red'>commerce_uk_panel.html not found</p>")

    log.info("business.listing: ready (barcode + vision + one-click listings)")
