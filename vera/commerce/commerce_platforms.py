"""
commerce_platforms.py — Marketplace / e-commerce platform connectors
====================================================================

Lets Vera *manage* stores on external platforms — **eBay** is wired for real;
Shopify / WooCommerce / Etsy / Amazon are declared stubs that appear in the
registry (so the UI shows the roadmap) and return a clear "not yet implemented"
error until built.

Design mirrors the codebase's "twin registry" pattern (``vera/delivery.py`` /
``vera/output_formats.py``): a ``CONNECTORS`` dict of pluggable connector
objects, and ``commerce.platform.*`` capabilities that dispatch by connector id.

Credentials are sealed at rest with ``vera/security/secrets.py`` (Fernet) and
never returned to the UI — exactly like ``accounts_capabilities.py``. eBay uses
OAuth: a client-credentials **app token** for read-only pricing (Browse API,
consumed by ``commerce_pricing_capabilities.py``) and an authorization-code
**user token** for managing inventory/orders (Sell APIs).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

log = logging.getLogger("vera.commerce.platforms")

try:
    from Vera.vera.capability_orchestration import (
        APP, capability, emit_event, now_iso, enum_schema,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    from Vera.vera.security import secrets as vsecrets
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.commerce.platforms").warning("commerce platforms unavailable: %s", e)
    _CAP_AVAILABLE = False

try:
    import httpx
    HAS_HTTPX = True
except Exception:                              # pragma: no cover
    HAS_HTTPX = False


# ─────────────────────────────────────────────────────────────────────────────
# eBay endpoints / scopes
# ─────────────────────────────────────────────────────────────────────────────

EBAY_ENV = {
    "production": {
        "auth":  "https://auth.ebay.com/oauth2/authorize",
        "token": "https://api.ebay.com/identity/v1/oauth2/token",
        "api":   "https://api.ebay.com",
    },
    "sandbox": {
        "auth":  "https://auth.sandbox.ebay.com/oauth2/authorize",
        "token": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
        "api":   "https://api.sandbox.ebay.com",
    },
}

EBAY_APP_SCOPE = "https://api.ebay.com/oauth/api_scope"
# Default eBay marketplace for this (UK) operation; override per-account via
# creds['marketplace'] (e.g. EBAY_US, EBAY_DE …).
EBAY_DEFAULT_MARKETPLACE = "EBAY_GB"
EBAY_SELL_SCOPES = [
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
    "https://api.ebay.com/oauth/api_scope/sell.account",
]

# Which cred fields are secret (sealed at rest, redacted on output).
# Vinted (commerce_vinted.py) adds its session-cookie fields to this set too.
SECRET_CRED_FIELDS = {"client_secret", "access_token_web", "refresh_token_web",
                      "session_cookie"}

_SCHEMA_READY = False

# In-process caches (single-process dev runtime).
_OAUTH_STATES: Dict[str, dict] = {}            # state token -> {account_id, ts}
_APP_TOKEN: Dict[str, tuple] = {}              # account_id -> (token, expiry_epoch)
_USER_TOKEN: Dict[str, tuple] = {}             # account_id -> (token, expiry_epoch)
_OAUTH_STATE_TTL = 900


async def _run(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"

def _core():
    return sys.modules.get("commerce_capabilities")


# ─────────────────────────────────────────────────────────────────────────────
# Schema + account store (secrets sealed)
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_schema_sync():
    global _SCHEMA_READY
    conn = _sqlite_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS commerce_platform_accounts (
                id            TEXT PRIMARY KEY,
                connector     TEXT,
                label         TEXT,
                env           TEXT DEFAULT 'production',
                creds         TEXT,
                oauth_token   TEXT,
                oauth_refresh TEXT,
                scopes        TEXT,
                status        TEXT DEFAULT 'configured',
                created_at    TEXT,
                updated_at    TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()
    _SCHEMA_READY = True

async def _ensure_schema():
    if not _SCHEMA_READY:
        await _run(_ensure_schema_sync)

def _open_account(row) -> dict:
    """Return an account dict with secret cred fields + oauth tokens DECRYPTED."""
    d = dict(row)
    creds = {}
    try:
        creds = json.loads(d.get("creds") or "{}")
    except Exception:
        creds = {}
    for f in SECRET_CRED_FIELDS:
        if creds.get(f):
            creds[f] = vsecrets.open_secret(creds[f])
    d["creds"] = creds
    d["oauth_token"] = vsecrets.open_secret(d.get("oauth_token") or "")
    d["oauth_refresh"] = vsecrets.open_secret(d.get("oauth_refresh") or "")
    d["scopes"] = json.loads(d.get("scopes") or "[]") if d.get("scopes") else []
    return d

def _redact_account(row) -> dict:
    """UI-safe view: secrets replaced with a '••••' hint, tokens as booleans."""
    d = dict(row)
    creds = {}
    try:
        creds = json.loads(d.get("creds") or "{}")
    except Exception:
        creds = {}
    _had_vinted_token = bool(creds.get("access_token_web"))
    for f in SECRET_CRED_FIELDS:
        creds[f] = vsecrets.redact(creds.get(f, ""))
    d["creds"] = creds
    # eBay reports connected via a stored refresh token; Vinted via a session cookie.
    d["connected"] = bool(d.get("oauth_refresh")) or _had_vinted_token
    d.pop("oauth_token", None)
    d.pop("oauth_refresh", None)
    d["scopes"] = json.loads(d.get("scopes") or "[]") if d.get("scopes") else []
    return d

def _db_list_accounts(opened: bool = False) -> List[dict]:
    conn = _sqlite_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM commerce_platform_accounts ORDER BY created_at").fetchall()
        return [_open_account(r) if opened else _redact_account(r) for r in rows]
    finally:
        conn.close()

def _db_get_account(aid: str, opened: bool = False) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM commerce_platform_accounts WHERE id=?",
                         (aid,)).fetchone()
        if not r:
            return None
        return _open_account(r) if opened else _redact_account(r)
    finally:
        conn.close()

def _db_first_account(connector: str, opened: bool = False) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute(
            "SELECT * FROM commerce_platform_accounts WHERE connector=? ORDER BY created_at LIMIT 1",
            (connector,)).fetchone()
        if not r:
            return None
        return _open_account(r) if opened else _redact_account(r)
    finally:
        conn.close()

def _db_upsert_account(fields: dict) -> dict:
    """Seal secret cred fields + oauth tokens, then INSERT OR REPLACE."""
    conn = _sqlite_conn()
    try:
        aid = fields.get("id") or ""
        existing = conn.execute("SELECT * FROM commerce_platform_accounts WHERE id=?",
                                (aid,)).fetchone() if aid else None
        base = _open_account(existing) if existing else {}
        aid = base.get("id") or aid or _new_id("plat")
        merged = {**base, **{k: v for k, v in fields.items() if v is not None}}

        creds = dict(merged.get("creds") or {})
        for f in SECRET_CRED_FIELDS:
            if creds.get(f):
                creds[f] = vsecrets.seal(creds[f])   # idempotent for already-sealed

        oauth_refresh = merged.get("oauth_refresh") or ""
        oauth_token = merged.get("oauth_token") or ""
        now = now_iso()
        conn.execute(
            "INSERT OR REPLACE INTO commerce_platform_accounts "
            "(id,connector,label,env,creds,oauth_token,oauth_refresh,scopes,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (aid, merged.get("connector", ""), merged.get("label", ""),
             merged.get("env", "production") or "production", json.dumps(creds),
             vsecrets.seal(oauth_token) if oauth_token else "",
             vsecrets.seal(oauth_refresh) if oauth_refresh else "",
             json.dumps(merged.get("scopes") or []), merged.get("status", "configured"),
             base.get("created_at") or now, now))
        conn.commit()
        return _db_get_account(aid) or {"id": aid}
    finally:
        conn.close()

def _db_delete_account(aid: str) -> bool:
    conn = _sqlite_conn()
    try:
        cur = conn.execute("DELETE FROM commerce_platform_accounts WHERE id=?", (aid,))
        conn.commit()
        _APP_TOKEN.pop(aid, None)
        _USER_TOKEN.pop(aid, None)
        return cur.rowcount > 0
    finally:
        conn.close()

def _db_patch_creds(aid: str, patch: dict) -> Optional[dict]:
    """Merge extra (non-secret) cred keys onto an account — e.g. eBay listing
    defaults (business policy ids, merchant location, category, marketplace)."""
    acct = _db_get_account(aid, opened=True)
    if not acct:
        return None
    creds = dict(acct.get("creds") or {})
    creds.update({k: v for k, v in (patch or {}).items() if v not in (None, "")})
    return _db_upsert_account({"id": aid, "creds": creds})


# ─────────────────────────────────────────────────────────────────────────────
# eBay OAuth helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ebay_endpoints(acct: dict) -> dict:
    return EBAY_ENV.get(acct.get("env") or "production", EBAY_ENV["production"])

def _ebay_marketplace(acct: dict) -> str:
    return (acct.get("creds") or {}).get("marketplace") or EBAY_DEFAULT_MARKETPLACE

def _basic_auth(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")

async def ebay_app_token(account_id: str = "") -> Optional[str]:
    """Client-credentials app token for the Browse API (public pricing). Picks
    the given eBay account, else the first configured one. Cached to expiry."""
    if not HAS_HTTPX:
        return None
    acct = (_db_get_account(account_id, opened=True) if account_id
            else _db_first_account("ebay", opened=True))
    if not acct:
        return None
    cid = (acct.get("creds") or {}).get("client_id", "")
    csec = (acct.get("creds") or {}).get("client_secret", "")
    if not (cid and csec):
        return None
    cached = _APP_TOKEN.get(acct["id"])
    if cached and cached[1] > time.time() + 30:
        return cached[0]
    ep = _ebay_endpoints(acct)
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(ep["token"],
                headers={"Authorization": _basic_auth(cid, csec),
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials", "scope": EBAY_APP_SCOPE})
            resp.raise_for_status()
            tok = resp.json()
    except Exception as e:
        log.warning("ebay app-token failed: %s", e)
        return None
    at = tok.get("access_token", "")
    if at:
        _APP_TOKEN[acct["id"]] = (at, time.time() + int(tok.get("expires_in", 7200) or 7200))
    return at or None

async def ebay_user_token(account_id: str) -> Optional[str]:
    """Authorization-code user token (refreshed) for the Sell APIs."""
    if not HAS_HTTPX:
        return None
    acct = _db_get_account(account_id, opened=True)
    if not acct or not acct.get("oauth_refresh"):
        return None
    cid = (acct.get("creds") or {}).get("client_id", "")
    csec = (acct.get("creds") or {}).get("client_secret", "")
    if not (cid and csec):
        return None
    cached = _USER_TOKEN.get(account_id)
    if cached and cached[1] > time.time() + 30:
        return cached[0]
    ep = _ebay_endpoints(acct)
    scopes = " ".join(acct.get("scopes") or EBAY_SELL_SCOPES)
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(ep["token"],
                headers={"Authorization": _basic_auth(cid, csec),
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "refresh_token",
                      "refresh_token": acct["oauth_refresh"], "scope": scopes})
            resp.raise_for_status()
            tok = resp.json()
    except Exception as e:
        log.warning("ebay user-token refresh failed: %s", e)
        return None
    at = tok.get("access_token", "")
    if at:
        _USER_TOKEN[account_id] = (at, time.time() + int(tok.get("expires_in", 7200) or 7200))
    return at or None

def _ebay_auth_url(acct: dict, state: str) -> str:
    cid = (acct.get("creds") or {}).get("client_id", "")
    ru_name = (acct.get("creds") or {}).get("ru_name", "")
    ep = _ebay_endpoints(acct)
    scopes = acct.get("scopes") or EBAY_SELL_SCOPES
    q = urlencode({
        "client_id": cid, "response_type": "code",
        "redirect_uri": ru_name,            # eBay expects the RuName here
        "scope": " ".join(scopes), "state": state,
    })
    return f"{ep['auth']}?{q}"


# ─────────────────────────────────────────────────────────────────────────────
# Connector registry
# ─────────────────────────────────────────────────────────────────────────────

class StubConnector:
    """A declared-but-unbuilt platform. Shows in the registry; every op errors."""
    real = False
    needs_oauth = True

    def __init__(self, cid: str, label: str):
        self.id = cid
        self.label = label

    def capabilities(self) -> dict:
        return {"pricing": False, "listings": False, "orders": False}

    def _err(self) -> dict:
        return {"error": f"connector '{self.id}' is declared but not yet implemented"}

    async def list_listings(self, acct, limit=100): return self._err()
    async def push_listing(self, acct, product):   return self._err()
    async def publish_listing(self, acct, product, opts=None): return self._err()
    async def archive_listing(self, acct, external_id="", sku=""): return self._err()
    async def list_orders(self, acct, limit=100):   return self._err()


class EbayConnector:
    id = "ebay"
    label = "eBay"
    real = True
    needs_oauth = True

    def capabilities(self) -> dict:
        return {"pricing": True, "listings": True, "orders": True}

    async def list_listings(self, acct: dict, limit: int = 100) -> dict:
        token = await ebay_user_token(acct["id"])
        if not token:
            return {"error": "eBay account not connected (complete OAuth first)"}
        ep = _ebay_endpoints(acct)
        try:
            async with httpx.AsyncClient(timeout=40) as c:
                resp = await c.get(
                    f"{ep['api']}/sell/inventory/v1/inventory_item",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"limit": min(int(limit), 200)})
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return {"error": f"eBay inventory fetch failed: {e}"}
        out = []
        for it in data.get("inventoryItems", []):
            avail = ((it.get("availability") or {})
                     .get("shipToLocationAvailability") or {})
            prod = it.get("product") or {}
            out.append({
                "sku": it.get("sku", ""),
                "name": prod.get("title", ""),
                "description": prod.get("description", ""),
                "qty_on_hand": avail.get("quantity", 0),
                "upc": (prod.get("upc") or [""])[0] if prod.get("upc") else "",
            })
        return {"listings": out, "count": len(out)}

    async def push_listing(self, acct: dict, product: dict) -> dict:
        token = await ebay_user_token(acct["id"])
        if not token:
            return {"error": "eBay account not connected (complete OAuth first)"}
        sku = product.get("sku")
        if not sku:
            return {"error": "product has no SKU (required by eBay Inventory API)"}
        ep = _ebay_endpoints(acct)
        body: Dict[str, Any] = {
            "availability": {"shipToLocationAvailability": {
                "quantity": int(product.get("qty_on_hand", 0) or 0)}},
            "condition": "NEW",
            "product": {
                "title": product.get("name", "")[:80],
                "description": product.get("description", "") or product.get("name", ""),
            },
        }
        if product.get("upc"):
            body["product"]["upc"] = [str(product["upc"])]
        _attrs = product.get("attributes") or {}
        _imgs = _attrs.get("images") or _attrs.get("imageUrls") or []
        if isinstance(_imgs, list) and _imgs:
            body["product"]["imageUrls"] = [str(u) for u in _imgs[:12]]
        if isinstance(_attrs.get("aspects"), dict) and _attrs["aspects"]:
            body["product"]["aspects"] = {k: (v if isinstance(v, list) else [str(v)])
                                          for k, v in _attrs["aspects"].items()}
        try:
            async with httpx.AsyncClient(timeout=40) as c:
                resp = await c.put(
                    f"{ep['api']}/sell/inventory/v1/inventory_item/{sku}",
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json",
                             "Content-Language": "en-US"},
                    json=body)
        except Exception as e:
            return {"error": f"eBay push failed: {e}"}
        if resp.status_code in (200, 201, 204):
            return {"ok": True, "sku": sku,
                    "note": "inventory item upserted; publish an offer to make it a live listing"}
        return {"error": f"eBay returned {resp.status_code}: {resp.text[:300]}"}

    async def publish_listing(self, acct: dict, product: dict, opts: dict = None) -> dict:
        """One-click go-live: ensure the inventory item, create/replace an offer,
        then publish it into a real listing. Needs eBay business policies +
        merchant location + a leaf category (from opts or the account defaults)."""
        token = await ebay_user_token(acct["id"])
        if not token:
            return {"error": "eBay account not connected (complete OAuth first)"}
        sku = product.get("sku")
        if not sku:
            return {"error": "product has no SKU (required to publish)"}
        opts = opts or {}
        creds = acct.get("creds") or {}
        attrs = product.get("attributes") or {}
        ep = _ebay_endpoints(acct)
        mkt = opts.get("marketplace") or _ebay_marketplace(acct)
        # 1. inventory item
        inv = await self.push_listing(acct, product)
        if inv.get("error"):
            return inv
        price = float(opts.get("price") or product.get("price") or 0)
        if price <= 0:
            return {"error": "a positive price is required to publish an offer",
                    "inventory_ok": True}
        currency = opts.get("currency") or product.get("currency") or "GBP"
        fulfillment = opts.get("fulfillment_policy_id") or creds.get("fulfillment_policy_id")
        payment = opts.get("payment_policy_id") or creds.get("payment_policy_id")
        ret = opts.get("return_policy_id") or creds.get("return_policy_id")
        loc = opts.get("merchant_location_key") or creds.get("merchant_location_key")
        category = (opts.get("category_id") or creds.get("category_id")
                    or attrs.get("ebay_category_id"))
        missing = [n for n, v in (("fulfillment_policy_id", fulfillment),
                                  ("payment_policy_id", payment),
                                  ("return_policy_id", ret),
                                  ("merchant_location_key", loc),
                                  ("category_id", category)) if not v]
        if missing:
            return {"error": "eBay publish needs business policies, a merchant "
                             f"location key and a leaf category — missing: {missing}. "
                             "Save them once with commerce.ebay.defaults or pass in opts.",
                    "needs": missing, "inventory_ok": True}
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                   "Content-Language": "en-GB", "X-EBAY-C-MARKETPLACE-ID": mkt}
        offer_body: Dict[str, Any] = {
            "sku": sku, "marketplaceId": mkt, "format": "FIXED_PRICE",
            "availableQuantity": int(product.get("qty_on_hand", 1) or 1),
            "categoryId": str(category),
            "listingDescription": product.get("description") or product.get("name") or "",
            "listingPolicies": {"fulfillmentPolicyId": fulfillment,
                                "paymentPolicyId": payment, "returnPolicyId": ret},
            "pricingSummary": {"price": {"value": f"{price:.2f}", "currency": currency}},
            "merchantLocationKey": loc,
        }
        try:
            async with httpx.AsyncClient(timeout=45) as c:
                g = await c.get(f"{ep['api']}/sell/inventory/v1/offer", headers=headers,
                                params={"sku": sku, "marketplace_id": mkt})
                offers = (g.json().get("offers", []) if g.status_code == 200 else [])
                offer_id = offers[0].get("offerId", "") if offers else ""
                if offer_id:
                    u = await c.put(f"{ep['api']}/sell/inventory/v1/offer/{offer_id}",
                                    headers=headers, json=offer_body)
                    if u.status_code not in (200, 204):
                        return {"error": f"offer update {u.status_code}: {u.text[:300]}"}
                else:
                    cr = await c.post(f"{ep['api']}/sell/inventory/v1/offer",
                                      headers=headers, json=offer_body)
                    if cr.status_code not in (200, 201):
                        return {"error": f"offer create {cr.status_code}: {cr.text[:300]}"}
                    offer_id = (cr.json() or {}).get("offerId", "")
                pub = await c.post(f"{ep['api']}/sell/inventory/v1/offer/{offer_id}/publish",
                                   headers=headers)
        except Exception as e:
            return {"error": f"eBay publish failed: {e}"}
        if pub.status_code in (200, 201):
            listing_id = (pub.json() or {}).get("listingId", "")
            base = "https://www.ebay.co.uk/itm/" if mkt == "EBAY_GB" else "https://www.ebay.com/itm/"
            return {"ok": True, "sku": sku, "offer_id": offer_id, "listing_id": listing_id,
                    "marketplace": mkt, "url": (base + listing_id) if listing_id else ""}
        return {"error": f"publish {pub.status_code}: {pub.text[:400]}", "offer_id": offer_id}

    async def archive_listing(self, acct: dict, external_id: str = "", sku: str = "") -> dict:
        """End a live listing by withdrawing its offer (id or resolved from SKU)."""
        token = await ebay_user_token(acct["id"])
        if not token:
            return {"error": "eBay account not connected"}
        ep = _ebay_endpoints(acct)
        mkt = _ebay_marketplace(acct)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                   "X-EBAY-C-MARKETPLACE-ID": mkt}
        offer_id = external_id
        try:
            async with httpx.AsyncClient(timeout=40) as c:
                if not offer_id and sku:
                    g = await c.get(f"{ep['api']}/sell/inventory/v1/offer", headers=headers,
                                    params={"sku": sku, "marketplace_id": mkt})
                    offers = (g.json().get("offers", []) if g.status_code == 200 else [])
                    offer_id = offers[0].get("offerId", "") if offers else ""
                if not offer_id:
                    return {"error": "no offer id or SKU to withdraw"}
                r = await c.post(f"{ep['api']}/sell/inventory/v1/offer/{offer_id}/withdraw",
                                 headers=headers)
        except Exception as e:
            return {"error": f"eBay withdraw failed: {e}"}
        if r.status_code in (200, 204):
            return {"ok": True, "offer_id": offer_id, "note": "listing withdrawn / ended"}
        return {"error": f"withdraw {r.status_code}: {r.text[:300]}"}

    async def list_orders(self, acct: dict, limit: int = 100) -> dict:
        token = await ebay_user_token(acct["id"])
        if not token:
            return {"error": "eBay account not connected (complete OAuth first)"}
        ep = _ebay_endpoints(acct)
        try:
            async with httpx.AsyncClient(timeout=40) as c:
                resp = await c.get(
                    f"{ep['api']}/sell/fulfillment/v1/order",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"limit": min(int(limit), 200)})
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return {"error": f"eBay order fetch failed: {e}"}
        out = []
        for o in data.get("orders", []):
            items = []
            for li in o.get("lineItems", []):
                items.append({
                    "sku": li.get("sku", ""),
                    "name": li.get("title", ""),
                    "qty": int(li.get("quantity", 1) or 1),
                    "unit_price": float(((li.get("lineItemCost") or {}).get("value")) or 0),
                })
            total = float(((o.get("pricingSummary") or {}).get("total") or {}).get("value") or 0)
            out.append({
                "external_id": o.get("orderId", ""),
                "status": (o.get("orderFulfillmentStatus") or "new").lower(),
                "items": items, "total": total,
                "currency": (((o.get("pricingSummary") or {}).get("total") or {}).get("currency") or "USD"),
                "placed_at": o.get("creationDate", ""),
            })
        return {"orders": out, "count": len(out)}


CONNECTORS: Dict[str, Any] = {
    "ebay":        EbayConnector(),
    "shopify":     StubConnector("shopify", "Shopify"),
    "woocommerce": StubConnector("woocommerce", "WooCommerce"),
    "etsy":        StubConnector("etsy", "Etsy"),
    "amazon":      StubConnector("amazon", "Amazon"),
}
CONNECTOR_IDS = list(CONNECTORS.keys())


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    @capability(
        "business.platform.connectors", http_method="GET",
        http_path="/business/platform/connectors", http_tags=["commerce"],
        memory="off", silent=True,
        description="List available marketplace connectors, whether each is fully built "
                    "('real'), whether it needs OAuth, its capability flags, and how many "
                    "accounts are connected. Output: {connectors:[...]}.")
    async def cap_connectors(trace_id=None):
        await _ensure_schema()
        accts = await _run(_db_list_accounts, False)
        by_conn: Dict[str, int] = {}
        for a in accts:
            by_conn[a["connector"]] = by_conn.get(a["connector"], 0) + 1
        out = []
        for cid, conn in CONNECTORS.items():
            out.append({
                "id": cid, "label": conn.label, "real": conn.real,
                "needs_oauth": conn.needs_oauth, "capabilities": conn.capabilities(),
                "accounts": by_conn.get(cid, 0),
            })
        return {"connectors": out}

    @capability(
        "business.platform.accounts", http_method="GET",
        http_path="/business/platform/accounts", http_tags=["commerce"],
        memory="off", silent=True,
        description="List connected platform accounts (secrets REDACTED). "
                    "Output: {accounts:[...]} each with connector, label, env, status, connected(bool).")
    async def cap_accounts(trace_id=None):
        await _ensure_schema()
        return {"accounts": await _run(_db_list_accounts, False)}

    @capability(
        "business.platform.connect", http_method="POST",
        http_path="/business/platform/connect", http_tags=["commerce"],
        schema=enum_schema(connector=CONNECTOR_IDS, env=["production", "sandbox"]),
        description="Save a platform account's app credentials and, for OAuth connectors "
                    "(eBay), return an auth_url the human opens to grant access. "
                    "Input: connector (str!: ebay|shopify|…), label (str), env (production|sandbox), "
                    "client_id (str), client_secret (str — sealed), ru_name (str — eBay redirect "
                    "URL name), id (str — update an existing account). "
                    "Output: {ok, account, auth_url?} — open auth_url to finish eBay OAuth.")
    async def cap_connect(connector: str = "", label: str = "", env: str = "production",
                          client_id: str = "", client_secret: str = "", ru_name: str = "",
                          id: str = "", trace_id=None):
        await _ensure_schema()
        connector = (connector or "").lower()
        if connector not in CONNECTORS:
            return {"error": f"unknown connector '{connector}' (have {CONNECTOR_IDS})"}
        conn = CONNECTORS[connector]
        if not conn.real:
            # Still let them stage it, but flag clearly.
            pass
        creds = {}
        if client_id:     creds["client_id"] = client_id
        if client_secret: creds["client_secret"] = client_secret
        if ru_name:       creds["ru_name"] = ru_name
        fields = {"id": id or None, "connector": connector,
                  "label": label or conn.label, "env": env, "creds": creds or None,
                  "status": "configured"}
        if connector == "ebay" and not id:
            fields["scopes"] = EBAY_SELL_SCOPES
        acct = await _run(_db_upsert_account, fields)

        result = {"ok": True, "account": acct}
        # Build OAuth start URL for eBay if we have the client id + RuName.
        if connector == "ebay":
            opened = await _run(_db_get_account, acct["id"], True)
            c = (opened.get("creds") or {})
            if c.get("client_id") and c.get("ru_name"):
                state = uuid.uuid4().hex
                _OAUTH_STATES[state] = {"account_id": acct["id"], "ts": time.time()}
                result["auth_url"] = _ebay_auth_url(opened, state)
            else:
                result["note"] = ("provide client_id + ru_name to generate the eBay "
                                  "authorization URL")
        elif not conn.real:
            result["note"] = f"connector '{connector}' is not implemented yet — saved for later"
        return result

    @capability(
        "business.platform.disconnect", http_method="POST",
        http_path="/business/platform/disconnect", http_tags=["commerce"],
        description="Remove a platform account and its stored credentials/tokens. "
                    "Input: id (str!). Output: {ok} or {error}.")
    async def cap_disconnect(id: str = "", trace_id=None):
        await _ensure_schema()
        if not id:
            return {"error": "id required"}
        ok = await _run(_db_delete_account, id)
        return {"ok": ok} if ok else {"error": "not found"}

    @capability(
        "business.platform.listings.sync", http_method="POST",
        http_path="/business/platform/listings/sync", http_tags=["commerce"],
        description="Pull listings from a connected platform into local inventory "
                    "(upserts products, matching on SKU). Input: account_id (str!), "
                    "limit (int default 100). Output: {ok, synced, connector} or {error}.")
    async def cap_listings_sync(account_id: str = "", limit: int = 100, trace_id=None):
        await _ensure_schema()
        acct = await _run(_db_get_account, account_id, True)
        if not acct:
            return {"error": "account not found"}
        conn = CONNECTORS.get(acct["connector"])
        if not conn:
            return {"error": f"no connector for '{acct['connector']}'"}
        res = await conn.list_listings(acct, int(limit))
        if res.get("error"):
            return res
        core = _core()
        synced = 0
        for lst in res.get("listings", []):
            if core and lst.get("sku"):
                await _run(core._db_upsert_product, {
                    "sku": lst["sku"], "name": lst.get("name"),
                    "description": lst.get("description"), "upc": lst.get("upc"),
                    "qty_on_hand": lst.get("qty_on_hand"),
                    "attributes": {"source": acct["connector"]}})
                synced += 1
        await emit_event({"type": "commerce.progress", "stage": "listings.sync",
                          "message": f"{acct['connector']}: synced {synced} listings"})
        return {"ok": True, "synced": synced, "connector": acct["connector"]}

    @capability(
        "business.platform.listing.push", http_method="POST",
        http_path="/business/platform/listing/push", http_tags=["commerce"],
        description="Create or update a listing on a platform from a local product. "
                    "Input: account_id (str!), product_id (str! — local product id or sku). "
                    "Output: connector result {ok, sku, note} or {error}.")
    async def cap_listing_push(account_id: str = "", product_id: str = "", trace_id=None):
        await _ensure_schema()
        acct = await _run(_db_get_account, account_id, True)
        if not acct:
            return {"error": "account not found"}
        core = _core()
        if not core:
            return {"error": "commerce core module not loaded"}
        product = await _run(core._db_get_product, product_id)
        if not product:
            return {"error": "product not found"}
        conn = CONNECTORS.get(acct["connector"])
        if not conn:
            return {"error": f"no connector for '{acct['connector']}'"}
        res = await conn.push_listing(acct, product)
        await emit_event({"type": "commerce.progress", "stage": "listing.push",
                          "message": f"{acct['connector']}: pushed {product.get('sku')}"})
        return res

    @capability(
        "business.platform.orders.sync", http_method="POST",
        http_path="/business/platform/orders/sync", http_tags=["commerce"],
        description="Pull orders from a connected platform into local orders (dedup by "
                    "external id; newly-imported orders draw down matching SKU stock). "
                    "Input: account_id (str!), limit (int default 100). "
                    "Output: {ok, imported, updated, connector} or {error}.")
    async def cap_orders_sync(account_id: str = "", limit: int = 100, trace_id=None):
        await _ensure_schema()
        acct = await _run(_db_get_account, account_id, True)
        if not acct:
            return {"error": "account not found"}
        conn = CONNECTORS.get(acct["connector"])
        if not conn:
            return {"error": f"no connector for '{acct['connector']}'"}
        res = await conn.list_orders(acct, int(limit))
        if res.get("error"):
            return res
        core = _core()
        imported = updated = 0
        for o in res.get("orders", []):
            ext = o.get("external_id")
            if not ext or not core:
                continue
            existed = await _run(core._db_find_order_by_external, acct["connector"], ext)
            saved = await _run(core._db_upsert_order, {
                "source": acct["connector"], "external_id": ext,
                "store_id": (acct.get("creds") or {}).get("store_id", ""),
                "status": o.get("status", "new"), "items": o.get("items", []),
                "total": o.get("total"), "currency": o.get("currency", "USD"),
                "placed_at": o.get("placed_at")})
            if existed:
                updated += 1
            else:
                imported += 1
                for it in o.get("items", []):
                    if it.get("sku") and it.get("qty"):
                        await _run(core._db_adjust_stock, it["sku"],
                                   -abs(int(it["qty"])), "sync", saved["id"],
                                   f"{acct['connector']} order {ext}")
        await emit_event({"type": "commerce.progress", "stage": "orders.sync",
                          "message": f"{acct['connector']}: {imported} new / {updated} updated"})
        return {"ok": True, "imported": imported, "updated": updated,
                "connector": acct["connector"]}

    # ── OAuth callback (eBay redirects here after consent) ─────────────────────

    def _oauth_result_page(ok: bool, msg: str) -> str:
        import html as _h
        accent = "#28c28a" if ok else "#ef5b5b"
        title = "Connected ✓" if ok else "Authorisation failed"
        return (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Vera · Connect platform</title></head>"
            "<body style='background:#0d0f12;color:#e8eaed;margin:0;min-height:100vh;"
            "display:flex;align-items:center;justify-content:center;font-family:"
            "system-ui,-apple-system,Segoe UI,Roboto,sans-serif'>"
            "<div style='text-align:center;max-width:440px;padding:32px'>"
            f"<h2 style='color:{accent};margin:0 0 12px'>{title}</h2>"
            f"<p style='color:#aab;line-height:1.5'>{_h.escape(str(msg))}</p>"
            "<p style='color:#667;font-size:13px;margin-top:24px'>You can close this "
            "tab and return to Vera.</p></div>"
            "<script>setTimeout(function(){window.close()},2500)</script>"
            "</body></html>")

    async def _ebay_exchange_code(acct: dict, code: str) -> dict:
        cid = (acct.get("creds") or {}).get("client_id", "")
        csec = (acct.get("creds") or {}).get("client_secret", "")
        ru_name = (acct.get("creds") or {}).get("ru_name", "")
        if not (cid and csec and ru_name):
            return {"error": "missing client_id/client_secret/ru_name"}
        ep = _ebay_endpoints(acct)
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                resp = await c.post(ep["token"],
                    headers={"Authorization": _basic_auth(cid, csec),
                             "Content-Type": "application/x-www-form-urlencoded"},
                    data={"grant_type": "authorization_code", "code": code,
                          "redirect_uri": ru_name})
                resp.raise_for_status()
                tok = resp.json()
        except Exception as e:
            return {"error": f"token exchange failed: {e}"}
        rt = tok.get("refresh_token")
        if not rt:
            return {"error": "no refresh_token returned"}
        await _run(_db_upsert_account, {"id": acct["id"], "oauth_refresh": rt,
                                        "oauth_token": tok.get("access_token", ""),
                                        "status": "connected"})
        if tok.get("access_token"):
            _USER_TOKEN[acct["id"]] = (tok["access_token"],
                                       time.time() + int(tok.get("expires_in", 7200) or 7200))
        return {"ok": True}

    @APP.get("/commerce/oauth/callback", include_in_schema=False)
    async def _commerce_oauth_callback(code: str = "", state: str = "", error: str = ""):
        from fastapi.responses import HTMLResponse
        if error:
            return HTMLResponse(_oauth_result_page(False, f"eBay reported: {error}"))
        st = _OAUTH_STATES.pop(state, None)
        if not st or (time.time() - st.get("ts", 0)) > _OAUTH_STATE_TTL:
            return HTMLResponse(_oauth_result_page(False, "state expired or invalid — retry connect"))
        if not code:
            return HTMLResponse(_oauth_result_page(False, "no authorization code returned"))
        acct = await _run(_db_get_account, st["account_id"], True)
        if not acct:
            return HTMLResponse(_oauth_result_page(False, "account no longer exists"))
        res = await _ebay_exchange_code(acct, code)
        if res.get("error"):
            return HTMLResponse(_oauth_result_page(False, res["error"]))
        return HTMLResponse(_oauth_result_page(
            True, f"{acct.get('label') or 'eBay'} is now connected. Vera can manage it."))

    log.info("business.platforms: ready (connectors=%s)", CONNECTOR_IDS)
