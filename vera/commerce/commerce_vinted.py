"""
commerce_vinted.py — Vinted marketplace connector + market OSINT search
=======================================================================

Vinted has no official public API, so this speaks the same JSON endpoints the
website itself uses (``/api/v2/…``). Two access levels:

  1. **Anonymous market search** — the catalogue search endpoint works with a
     throwaway session (a plain GET of the locale homepage mints the
     ``access_token_web`` cookie). This powers market OSINT / deal-finding and
     needs **no** login, so it is always available.
  2. **Authenticated actions** — listing your wardrobe, pushing a new listing,
     reading sold orders. These need a real logged-in session. Rather than
     automate the login (fragile + against Vinted's ToS), the operator pastes
     the ``access_token_web`` + ``_vinted_fr_session`` cookies from a browser
     once; we seal them at rest (same pattern as eBay creds) and refresh the
     access token from the refresh cookie when possible.

The connector plugs into ``commerce_platforms.CONNECTORS`` under id ``vinted``,
so all the generic ``commerce.platform.*`` capabilities (accounts, listings.sync,
listing.push, orders.sync) work against Vinted too. Vinted-specific helpers
(``commerce.vinted.connect`` / ``commerce.vinted.search``) live here and are also
consumed by ``commerce_market.py`` for the unified deal scanner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

log = logging.getLogger("vera.commerce.vinted")

try:
    from Vera.vera.capability_orchestration import (
        capability, emit_event, now_iso, enum_schema,
    )
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.commerce.vinted").warning("commerce vinted unavailable: %s", e)
    _CAP_AVAILABLE = False

try:
    import httpx
    HAS_HTTPX = True
except Exception:                              # pragma: no cover
    HAS_HTTPX = False


# ─────────────────────────────────────────────────────────────────────────────
# Locale / endpoints
# ─────────────────────────────────────────────────────────────────────────────

VINTED_DOMAINS = {
    "uk": "www.vinted.co.uk",
    "ie": "www.vinted.ie",
    "de": "www.vinted.de",
    "fr": "www.vinted.fr",
    "es": "www.vinted.es",
    "it": "www.vinted.it",
    "nl": "www.vinted.nl",
    "pl": "www.vinted.pl",
    "us": "www.vinted.com",
}
DEFAULT_DOMAIN = "www.vinted.co.uk"
DEFAULT_CURRENCY = "GBP"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# condition / status → normalised label (Vinted status_id → text varies; keep text)
VINTED_ORDER = ["newest_first", "relevance", "price_low_to_high",
                "price_high_to_low", "favourite_count_desc"]

# anonymous-session cache: domain -> {"cookies":..., "token":..., "exp":epoch}
_SESS: Dict[str, dict] = {}
_SESS_TTL = 600


async def _run(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"

def _platforms():
    return sys.modules.get("commerce_platforms")

def _domain_for(env_or_domain: str = "") -> str:
    v = (env_or_domain or "").strip().lower()
    if not v:
        return DEFAULT_DOMAIN
    if v in VINTED_DOMAINS:
        return VINTED_DOMAINS[v]
    if "." in v:
        return v.replace("https://", "").replace("http://", "").strip("/")
    return DEFAULT_DOMAIN


# ─────────────────────────────────────────────────────────────────────────────
# Session + price helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_price(p: Any) -> tuple:
    """Vinted returns price as {'amount','currency_code'} (new) or a string (old)."""
    if p is None:
        return (None, DEFAULT_CURRENCY)
    if isinstance(p, dict):
        try:
            return (float(p.get("amount")), p.get("currency_code") or DEFAULT_CURRENCY)
        except (TypeError, ValueError):
            return (None, p.get("currency_code") or DEFAULT_CURRENCY)
    try:
        return (float(str(p).replace(",", ".")), DEFAULT_CURRENCY)
    except (TypeError, ValueError):
        return (None, DEFAULT_CURRENCY)


async def _anon_session(domain: str) -> dict:
    """Mint (and cache) an anonymous browsing session for a Vinted locale."""
    if not HAS_HTTPX:
        return {"cookies": {}, "token": "", "exp": 0}
    cached = _SESS.get(domain)
    if cached and cached.get("exp", 0) > time.time():
        return cached
    cookies: Dict[str, str] = {}
    token = ""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                     headers={"User-Agent": _UA}) as c:
            await c.get(f"https://{domain}/")
            cookies = {k: v for k, v in c.cookies.items()}
    except Exception as e:
        log.warning("vinted anon session failed (%s): %s", domain, e)
    token = cookies.get("access_token_web", "")
    sess = {"cookies": cookies, "token": token, "exp": time.time() + _SESS_TTL}
    _SESS[domain] = sess
    return sess


def _acct_session(acct: Optional[dict]) -> dict:
    """Build a request session from a stored (opened) Vinted platform account."""
    creds = (acct or {}).get("creds") or {}
    cookies: Dict[str, str] = {}
    tok = creds.get("access_token_web") or ""
    if tok:
        cookies["access_token_web"] = tok
    if creds.get("session_cookie"):
        cookies["_vinted_fr_session"] = creds["session_cookie"]
    if creds.get("refresh_token_web"):
        cookies["refresh_token_web"] = creds["refresh_token_web"]
    return {"cookies": cookies, "token": tok,
            "domain": _domain_for(creds.get("domain") or (acct or {}).get("env")),
            "user_id": creds.get("user_id") or ""}


async def _api_get(domain: str, path: str, params: dict, sess: dict) -> dict:
    headers = {"User-Agent": _UA, "Accept": "application/json, text/plain, */*",
               "Referer": f"https://{domain}/"}
    if sess.get("token"):
        headers["Authorization"] = f"Bearer {sess['token']}"
    async with httpx.AsyncClient(timeout=40, follow_redirects=True) as c:
        r = await c.get(f"https://{domain}{path}", params=params, headers=headers,
                        cookies=sess.get("cookies") or {})
        r.raise_for_status()
        return r.json()


# ─────────────────────────────────────────────────────────────────────────────
# Public search (market OSINT)
# ─────────────────────────────────────────────────────────────────────────────

async def vinted_search(query: str, domain: str = DEFAULT_DOMAIN, per_page: int = 48,
                        order: str = "newest_first", price_from: float = None,
                        price_to: float = None, catalog_ids: str = "",
                        sess: dict = None) -> dict:
    """Search Vinted's live catalogue. Returns normalised items + a price summary."""
    if not HAS_HTTPX:
        return {"error": "httpx not installed", "items": []}
    domain = _domain_for(domain)
    sess = sess or await _anon_session(domain)
    params: Dict[str, Any] = {"search_text": query, "per_page": min(int(per_page), 96),
                              "order": order if order in VINTED_ORDER else "newest_first",
                              "page": 1}
    if price_from is not None:
        params["price_from"] = price_from
    if price_to is not None:
        params["price_to"] = price_to
    if catalog_ids:
        params["catalog_ids"] = catalog_ids
    try:
        data = await _api_get(domain, "/api/v2/catalog/items", params, sess)
    except Exception as e:
        # Session may have gone stale — mint a fresh anon session once and retry.
        _SESS.pop(domain, None)
        try:
            sess = await _anon_session(domain)
            data = await _api_get(domain, "/api/v2/catalog/items", params, sess)
        except Exception as e2:
            return {"error": f"vinted search failed: {e2 or e}", "items": []}
    items = []
    for it in (data.get("items") or []):
        amount, currency = _parse_price(it.get("price") or it.get("total_item_price"))
        photo = (it.get("photo") or {})
        items.append({
            "external_id": str(it.get("id", "")),
            "title": it.get("title", ""),
            "price": amount,
            "currency": currency,
            "brand": it.get("brand_title", ""),
            "size": it.get("size_title", ""),
            "condition": it.get("status", ""),
            "url": it.get("url", ""),
            "photo": photo.get("url", ""),
            "favourites": it.get("favourite_count", 0),
            "seller": ((it.get("user") or {}).get("login", "")),
        })
    prices = sorted(p for p in (i["price"] for i in items) if p and p > 0)
    summary = None
    if prices:
        import statistics
        summary = {"low": round(prices[0], 2), "median": round(statistics.median(prices), 2),
                   "high": round(prices[-1], 2), "sample_size": len(prices),
                   "currency": items[0]["currency"] if items else DEFAULT_CURRENCY}
    return {"items": items, "count": len(items), "summary": summary,
            "domain": domain, "query": query}


# ─────────────────────────────────────────────────────────────────────────────
# Connector
# ─────────────────────────────────────────────────────────────────────────────

class VintedConnector:
    id = "vinted"
    label = "Vinted"
    real = True
    needs_oauth = False        # uses pasted session cookies, not an OAuth dance

    def capabilities(self) -> dict:
        return {"pricing": True, "listings": True, "orders": True, "market": True}

    async def list_listings(self, acct: dict, limit: int = 100) -> dict:
        sess = _acct_session(acct)
        uid = sess.get("user_id")
        if not (sess.get("token") and uid):
            return {"error": "Vinted not connected — run commerce.vinted.connect with a "
                             "browser access_token_web cookie + your user_id"}
        try:
            data = await _api_get(sess["domain"], f"/api/v2/wardrobe/{uid}/items",
                                  {"per_page": min(int(limit), 200), "page": 1}, sess)
        except Exception as e:
            return {"error": f"vinted wardrobe fetch failed: {e}"}
        out = []
        for it in (data.get("items") or []):
            amount, currency = _parse_price(it.get("price"))
            out.append({"sku": f"vinted-{it.get('id')}", "external_id": str(it.get("id", "")),
                        "name": it.get("title", ""), "description": it.get("description", ""),
                        "price": amount, "currency": currency,
                        "qty_on_hand": 1 if not it.get("is_reserved") else 0,
                        "url": it.get("url", "")})
        return {"listings": out, "count": len(out)}

    async def push_listing(self, acct: dict, product: dict) -> dict:
        """Best-effort create. Vinted item upload needs photos uploaded first; we
        submit the text draft and surface the API's own error if photos/attrs are
        required, so the operator can finish in-app rather than us over-promising."""
        sess = _acct_session(acct)
        if not sess.get("token"):
            return {"error": "Vinted not connected (no access_token_web)"}
        attrs = product.get("attributes") or {}
        photo_ids = attrs.get("vinted_photo_ids") or []
        body = {
            "item": {
                "title": (product.get("name") or "")[:100],
                "description": product.get("description") or product.get("name") or "",
                "price": round(float(product.get("price") or 0), 2),
                "currency": product.get("currency") or DEFAULT_CURRENCY,
                "catalog_id": attrs.get("vinted_catalog_id"),
                "brand_id": attrs.get("vinted_brand_id"),
                "status_id": attrs.get("vinted_status_id"),
                "assigned_photos": [{"id": p} for p in photo_ids],
            }
        }
        if not HAS_HTTPX:
            return {"error": "httpx not installed"}
        headers = {"User-Agent": _UA, "Content-Type": "application/json",
                   "Accept": "application/json", "Authorization": f"Bearer {sess['token']}",
                   "Referer": f"https://{sess['domain']}/"}
        try:
            async with httpx.AsyncClient(timeout=40, follow_redirects=True) as c:
                r = await c.post(f"https://{sess['domain']}/api/v2/item_upload/items",
                                 json=body, headers=headers, cookies=sess.get("cookies") or {})
        except Exception as e:
            return {"error": f"vinted push failed: {e}"}
        if r.status_code in (200, 201):
            j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            item = (j.get("item") or {})
            return {"ok": True, "external_id": str(item.get("id", "")),
                    "url": item.get("url", ""), "note": "listing created on Vinted"}
        return {"error": f"vinted returned {r.status_code}: {r.text[:300]} — "
                         "photos usually must be uploaded in-app first "
                         "(store their ids in attributes.vinted_photo_ids)"}

    async def archive_listing(self, acct: dict, external_id: str) -> dict:
        sess = _acct_session(acct)
        if not sess.get("token"):
            return {"error": "Vinted not connected"}
        if not HAS_HTTPX:
            return {"error": "httpx not installed"}
        headers = {"User-Agent": _UA, "Accept": "application/json",
                   "Authorization": f"Bearer {sess['token']}",
                   "Referer": f"https://{sess['domain']}/"}
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                r = await c.delete(f"https://{sess['domain']}/api/v2/items/{external_id}",
                                   headers=headers, cookies=sess.get("cookies") or {})
        except Exception as e:
            return {"error": f"vinted archive failed: {e}"}
        if r.status_code in (200, 204):
            return {"ok": True, "external_id": external_id}
        return {"error": f"vinted returned {r.status_code}: {r.text[:200]}"}

    async def list_orders(self, acct: dict, limit: int = 100) -> dict:
        sess = _acct_session(acct)
        if not sess.get("token"):
            return {"error": "Vinted not connected"}
        try:
            data = await _api_get(sess["domain"], "/api/v2/my_orders",
                                  {"type": "sold", "per_page": min(int(limit), 100),
                                   "page": 1}, sess)
        except Exception as e:
            return {"error": f"vinted orders fetch failed: {e}"}
        out = []
        for o in (data.get("my_orders") or data.get("orders") or []):
            amount, currency = _parse_price(o.get("price") or o.get("total"))
            title = o.get("title") or (o.get("item") or {}).get("title", "")
            out.append({
                "external_id": str(o.get("id", "") or o.get("transaction_id", "")),
                "status": (o.get("status") or "sold").lower(),
                "items": [{"sku": "", "name": title, "qty": 1, "unit_price": amount or 0}],
                "total": amount or 0, "currency": currency,
                "placed_at": o.get("created_at", "") or o.get("date", ""),
            })
        return {"orders": out, "count": len(out)}


# ─────────────────────────────────────────────────────────────────────────────
# Register the connector into the platforms registry (load order: platforms first)
# ─────────────────────────────────────────────────────────────────────────────

def _register_connector():
    plat = _platforms()
    if plat is None:
        return False
    try:
        plat.CONNECTORS["vinted"] = VintedConnector()
        if "vinted" not in getattr(plat, "CONNECTOR_IDS", []):
            plat.CONNECTOR_IDS.append("vinted")
        for f in ("access_token_web", "refresh_token_web", "session_cookie"):
            plat.SECRET_CRED_FIELDS.add(f)
        return True
    except Exception as e:
        log.warning("vinted connector registration failed: %s", e)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    _register_connector()

    @capability(
        "business.vinted.connect", http_method="POST",
        http_path="/business/vinted/connect", http_tags=["commerce"],
        description="Connect a Vinted account by pasting browser session cookies "
                    "(no OAuth). Open vinted.co.uk logged in, copy the "
                    "access_token_web cookie (and optionally _vinted_fr_session + "
                    "your numeric user_id from your profile URL). Secrets are sealed. "
                    "Input: label (str), domain (uk|ie|de|fr|… or a host, default "
                    "www.vinted.co.uk), access_token_web (str — sealed), "
                    "session_cookie (str — _vinted_fr_session, sealed), "
                    "refresh_token_web (str, sealed), user_id (str), id (str — update). "
                    "Output: {ok, account}.")
    async def cap_vinted_connect(
        label: str = "Vinted", domain: str = "uk", access_token_web: str = "",
        session_cookie: str = "", refresh_token_web: str = "", user_id: str = "",
        store_id: str = "", id: str = "", trace_id=None):
        plat = _platforms()
        if not plat:
            return {"error": "commerce_platforms module not loaded"}
        await plat._ensure_schema()
        creds: Dict[str, Any] = {"domain": _domain_for(domain)}
        if access_token_web:  creds["access_token_web"] = access_token_web
        if session_cookie:    creds["session_cookie"] = session_cookie
        if refresh_token_web: creds["refresh_token_web"] = refresh_token_web
        if user_id:           creds["user_id"] = user_id
        if store_id:          creds["store_id"] = store_id
        acct = await _run(plat._db_upsert_account, {
            "id": id or None, "connector": "vinted", "label": label or "Vinted",
            "env": _domain_for(domain), "creds": creds,
            "status": "connected" if access_token_web else "configured"})
        await emit_event({"type": "commerce.progress", "stage": "vinted.connect",
                          "message": f"Vinted account {acct.get('label')} saved"})
        return {"ok": True, "account": acct}

    @capability(
        "business.vinted.search", http_method="POST",
        http_path="/business/vinted/search", http_tags=["commerce"],
        schema=enum_schema(order=VINTED_ORDER),
        description="Search Vinted's live catalogue (anonymous — no account needed). "
                    "For market research / deal-finding on video games, merch & "
                    "accessories. Input: query (str!), domain (uk|ie|… default uk), "
                    "per_page (int default 48), order (newest_first|price_low_to_high|"
                    "price_high_to_low|favourite_count_desc), price_from (float), "
                    "price_to (float), catalog_ids (str). "
                    "Output: {items:[{title,price,currency,brand,condition,url,photo,"
                    "favourites}], count, summary:{low,median,high,sample_size}}.")
    async def cap_vinted_search(
        query: str = "", domain: str = "uk", per_page: int = 48,
        order: str = "newest_first", price_from: float = None, price_to: float = None,
        catalog_ids: str = "", trace_id=None):
        if not query.strip():
            return {"error": "query required"}
        await emit_event({"type": "commerce.progress", "stage": "vinted.search",
                          "message": f"Vinted: '{query}'"})
        return await vinted_search(query, _domain_for(domain), int(per_page), order,
                                   price_from, price_to, catalog_ids)

    log.info("business.vinted: ready (connector registered=%s, httpx=%s)",
             "vinted" in getattr(_platforms(), "CONNECTORS", {}), HAS_HTTPX)
