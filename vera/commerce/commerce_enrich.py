"""
commerce_enrich.py — Item enrichment: prices, full history, metadata, reviews & rarity
======================================================================================

Turns a bare scan/title into a rich dossier, entirely from **free** sources:

  • **PriceCharting (scraped, keyless)** — the games-price standard. One product page
    yields the current Loose / CIB / New / Graded prices, the *full price history*
    (its embedded ``chart_data`` — a per-condition ``[[ts, cents], …]`` series that
    goes back to the game's launch), a liquidity/rarity read ("N sales per day/week/
    month"), and the metadata table (release date → year, genre, publisher,
    developer, player count, ESRB, UPC). Region (PAL/JP/NTSC) and edition come from
    the product URL slug. Prices are USD → converted to GBP with a live keyless rate.
  • **eBay + Vinted (current market)** — the existing ``business.market.search`` gives
    the live GBP low/median/high across both marketplaces.
  • **Reviews & sentiment (keyless)** — a web search for the title's reviews, distilled
    by the LLM into a one-line verdict, a sentiment and a 0-100 score.

  ``business.enrich.item`` aggregates all of that (cached), records a price-history
  snapshot, and writes the valuation back onto the product. ``business.pricehistory.get``
  returns the full merged series for charting. ``business.enrich.refresh_all`` re-values
  every inventory item so the shop's valuation stays current — schedule it nightly.

Storage: shared Data-Fabric SQLite db. Money shown in GBP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional

log = logging.getLogger("vera.commerce.enrich")

try:
    from Vera.vera.capability_orchestration import (
        capability, emit_event, now_iso, enum_schema, CAPABILITY_REGISTRY,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.commerce.enrich").warning("commerce enrich unavailable: %s", e)
    _CAP_AVAILABLE = False

try:
    import httpx
    HAS_HTTPX = True
except Exception:                              # pragma: no cover
    HAS_HTTPX = False

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_PC = "https://www.pricecharting.com"
_CACHE_TTL = 7 * 24 * 3600          # full re-enrich cache lifetime
_VAL_TTL = 24 * 3600                # valuation-only refresh cadence
_SCHEMA_READY = False


def _f(v, d=0.0):
    try: return float(v)
    except (TypeError, ValueError): return d

def _new_id(p): return f"{p}_{uuid.uuid4().hex[:12]}"

async def _run(fn, *a):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *a)

def _core():    return sys.modules.get("commerce_capabilities")
def _market():  return sys.modules.get("commerce_market")

def _cap_raw(name: str):
    reg = CAPABILITY_REGISTRY.get(name) if CAPABILITY_REGISTRY else None
    return reg.get("raw") if reg else None

async def _http(url: str, timeout: int = 25) -> str:
    if not HAS_HTTPX:
        return ""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers={"User-Agent": _UA,
                                              "Accept-Language": "en-GB,en;q=0.9"}) as c:
            r = await c.get(url)
            if r.status_code != 200:
                return ""
            return r.text
    except Exception as e:
        log.debug("http %s: %s", url, e)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# FX — USD → GBP (keyless, cached)
# ─────────────────────────────────────────────────────────────────────────────

_FX = {"rate": 0.79, "at": 0.0}

async def _usd_gbp() -> float:
    if time.time() - _FX["at"] < 6 * 3600 and _FX["at"]:
        return _FX["rate"]
    txt = await _http("https://api.frankfurter.app/latest?from=USD&to=GBP", 12)
    try:
        rate = _f((json.loads(txt).get("rates") or {}).get("GBP"))
        if rate:
            _FX["rate"], _FX["at"] = rate, time.time()
    except Exception:
        pass
    return _FX["rate"]


# ─────────────────────────────────────────────────────────────────────────────
# Schema — enrichment cache + price history
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_schema_sync():
    global _SCHEMA_READY
    conn = _sqlite_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS commerce_enrichment (
                key        TEXT PRIMARY KEY,
                upc        TEXT,
                title      TEXT,
                data       TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS commerce_price_history (
                id         TEXT PRIMARY KEY,
                item_key   TEXT,
                product_id TEXT,
                ts         INTEGER,
                source     TEXT,
                variant    TEXT,
                value      REAL,
                currency   TEXT DEFAULT 'GBP',
                sample     INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_pricehist_key ON commerce_price_history(item_key);
            CREATE INDEX IF NOT EXISTS ix_pricehist_ts  ON commerce_price_history(ts);
        """)
        conn.commit()
    finally:
        conn.close()
    _SCHEMA_READY = True

async def _ensure_schema():
    if not _SCHEMA_READY:
        await _run(_ensure_schema_sync)

def _norm_key(title: str = "", upc: str = "", platform: str = "") -> str:
    if upc:
        return "upc:" + re.sub(r"[^0-9Xx]", "", upc)
    t = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    p = re.sub(r"[^a-z0-9]+", "-", (platform or "").lower()).strip("-")
    return f"t:{t}" + (f"@{p}" if p else "")

def _cache_get(key: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT data, updated_at FROM commerce_enrichment WHERE key=?",
                         (key,)).fetchone()
        if not r:
            return None
        d = dict(r)
        try:
            out = json.loads(d.get("data") or "{}")
            out["_updated_at"] = d.get("updated_at")
            return out
        except Exception:
            return None
    finally:
        conn.close()

def _cache_put(key: str, upc: str, title: str, data: dict):
    conn = _sqlite_conn()
    try:
        conn.execute("INSERT OR REPLACE INTO commerce_enrichment (key,upc,title,data,updated_at) "
                     "VALUES (?,?,?,?,?)",
                     (key, upc or "", title or "", json.dumps(data), now_iso()))
        conn.commit()
    finally:
        conn.close()

def _hist_record(item_key: str, product_id: str, points: List[dict]):
    if not points:
        return
    conn = _sqlite_conn()
    try:
        for p in points:
            conn.execute("INSERT INTO commerce_price_history "
                         "(id,item_key,product_id,ts,source,variant,value,currency,sample) "
                         "VALUES (?,?,?,?,?,?,?,?,?)",
                         (_new_id("ph"), item_key, product_id or "", int(p.get("ts") or time.time() * 1000),
                          p.get("source", ""), p.get("variant", ""), _f(p.get("value")),
                          p.get("currency", "GBP"), int(p.get("sample") or 0)))
        conn.commit()
    finally:
        conn.close()

def record_sale_sync(title: str, upc: str, platform: str, product_id: str, price: float):
    """Append a realised-SALE point to the product's price history so the internal
    DB tracks what things actually sell for over time. Called when a unit sells."""
    if not _f(price):
        return
    _ensure_schema_sync()
    _hist_record(_norm_key(title, upc, platform), product_id,
                 [{"ts": int(time.time() * 1000), "source": "own_sale",
                   "variant": "sold", "value": _f(price), "currency": "GBP"}])

def _hist_snapshots(item_key: str) -> List[dict]:
    conn = _sqlite_conn()
    try:
        rows = conn.execute("SELECT ts,source,variant,value,currency,sample FROM "
                            "commerce_price_history WHERE item_key=? ORDER BY ts", (item_key,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# PriceCharting scraper
# ─────────────────────────────────────────────────────────────────────────────

_REGION = {"pal-": "PAL", "jp-": "JP"}
_ED_KEYS = [("limited", "Limited Edition"), ("premium", "Premium Edition"),
            ("collector", "Collector's Edition"), ("special", "Special Edition"),
            ("steelbook", "Steelbook"), ("game-of-the-year", "Game of the Year"),
            ("goty", "Game of the Year"), ("greatest-hits", "Greatest Hits"),
            ("platinum", "Platinum")]

def _slug_meta(url: str) -> dict:
    m = re.search(r"/game/([a-z0-9-]+)/([a-z0-9-]+)", url or "")
    if not m:
        return {}
    console_slug, slug = m.group(1), m.group(2)
    region = "NTSC"
    for pre, lbl in _REGION.items():
        if console_slug.startswith(pre):
            region = lbl; console_slug = console_slug[len(pre):]; break
    edition = "Standard"
    for kw, lbl in _ED_KEYS:
        if kw in slug:
            edition = lbl; break
    console = console_slug.replace("-", " ").title()
    return {"console": console, "region": region, "edition": edition, "slug": slug}

async def _pc_search(query: str, region_pref: str = "PAL") -> List[dict]:
    if not query:
        return []
    html = await _http(f"{_PC}/search-products?q={urllib.parse.quote(query)}&type=prices")
    if not html:
        return []
    seen, out = set(), []
    for href in re.findall(r'href="(https://www\.pricecharting\.com/game/[^"]+)"', html):
        url = href.replace("&amp;", "&")
        if url in seen:
            continue
        seen.add(url)
        meta = _slug_meta(url)
        if meta:
            meta["url"] = url; out.append(meta)
    # prefer the region we sell into, then a Standard edition, then order found
    out.sort(key=lambda m: (0 if m["region"] == region_pref else 1,
                            0 if m["edition"] == "Standard" else 1))
    return out[:12]

def _attr(html: str, label: str) -> str:
    m = re.search(r'<td[^>]*class="title"[^>]*>\s*' + re.escape(label) +
                  r':\s*</td>\s*<td[^>]*>(.*?)</td>', html, re.S | re.I)
    if not m:
        return ""
    return re.sub(r"<[^>]+>", " ", m.group(1)).strip()

def _parse_chart(html: str) -> Dict[str, list]:
    m = re.search(r"chart_data\s*=\s*(\{[^}]*\})", html, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}

def _last_nonzero(series: list) -> Optional[int]:
    for ts, cents in reversed(series or []):
        if cents:
            return cents
    return None

_VOL_RE = re.compile(r"(\d+)\s+sales?\s+per\s+(day|week|month|year)", re.I)
_VOL_PER_YEAR = {"day": 365, "week": 52, "month": 12, "year": 1}

def _rarity(html: str) -> dict:
    best = 0.0
    for n, per in _VOL_RE.findall(html):
        best = max(best, _f(n) * _VOL_PER_YEAR.get(per.lower(), 1))
    # sales/year → rarity bucket (used-condition liquidity)
    if best >= 300:   lbl, score = "Common", 15
    elif best >= 52:  lbl, score = "Uncommon", 35
    elif best >= 12:  lbl, score = "Scarce", 55
    elif best >= 3:   lbl, score = "Rare", 75
    elif best > 0:    lbl, score = "Very rare", 90
    else:             lbl, score = "Unknown", 0
    return {"label": lbl, "score": score, "sales_per_year": round(best, 1)}

async def _pc_lookup(query: str, upc: str = "", region_pref: str = "PAL") -> Optional[dict]:
    cands = await _pc_search(upc or query, region_pref) or (
        await _pc_search(query, region_pref) if upc else [])
    if not cands:
        return None
    top = cands[0]
    html = await _http(top["url"])
    if not html:
        return {"url": top["url"], **top, "alternatives": cands[1:6]}
    chart = _parse_chart(html)
    rate = await _usd_gbp()
    def gbp(cents):
        return round((cents / 100.0) * rate, 2) if cents else None
    prices = {"loose": gbp(_last_nonzero(chart.get("used"))),
              "cib":   gbp(_last_nonzero(chart.get("cib"))),
              "new":   gbp(_last_nonzero(chart.get("new"))),
              "graded": gbp(_last_nonzero(chart.get("graded"))),
              "currency": "GBP", "fx_usd_gbp": rate}
    # full history (down-sample to keep payloads sane), GBP
    history = []
    for variant, key in (("loose", "used"), ("cib", "cib"), ("new", "new")):
        for ts, cents in (chart.get(key) or []):
            if cents:
                history.append({"ts": ts, "variant": variant,
                                "value": round((cents / 100.0) * rate, 2)})
    rel = _attr(html, "Release Date")
    ym = re.search(r"(19|20)\d{2}", rel)
    im = re.search(r'og:image"\s+content="(https://[^"]+)"', html) or \
         re.search(r'(https://storage\.googleapis\.com/images\.pricecharting\.com/[^"\s]+?/\d+\.jpg)', html)
    cover = im.group(1) if im else ""
    meta = {"console": top.get("console"), "region": top.get("region"),
            "edition": top.get("edition"), "release_date": rel,
            "release_year": ym.group(0) if ym else "",
            "genre": _attr(html, "Genre"), "publisher": _attr(html, "Publisher"),
            "developer": _attr(html, "Developer"),
            "players": _attr(html, "Player Count"), "esrb": _attr(html, "ESRB Rating"),
            "upc": _attr(html, "UPC") or upc, "cover": cover}
    return {"url": top["url"], "prices": prices, "history": history, "cover": cover,
            "rarity": _rarity(html), "meta": meta, "alternatives": cands[1:6]}


# ─────────────────────────────────────────────────────────────────────────────
# Retro box art & screenshots — libretro-thumbnails (free, keyless)
# ─────────────────────────────────────────────────────────────────────────────

_LR = "https://thumbnails.libretro.com"
# console (as PriceCharting labels it, lower-cased) → libretro system folder
_LR_SYS = {
    "super nintendo": "Nintendo - Super Nintendo Entertainment System",
    "snes": "Nintendo - Super Nintendo Entertainment System",
    "nintendo": "Nintendo - Nintendo Entertainment System",
    "nes": "Nintendo - Nintendo Entertainment System",
    "nintendo 64": "Nintendo - Nintendo 64", "n64": "Nintendo - Nintendo 64",
    "gamecube": "Nintendo - GameCube", "wii": "Nintendo - Wii", "wii u": "Nintendo - Wii U",
    "switch": "Nintendo - Nintendo Switch", "nintendo switch": "Nintendo - Nintendo Switch",
    "game boy": "Nintendo - Game Boy", "gameboy": "Nintendo - Game Boy",
    "game boy color": "Nintendo - Game Boy Color", "gameboy color": "Nintendo - Game Boy Color",
    "game boy advance": "Nintendo - Game Boy Advance", "gba": "Nintendo - Game Boy Advance",
    "gameboy advance": "Nintendo - Game Boy Advance",
    "nintendo ds": "Nintendo - Nintendo DS", "ds": "Nintendo - Nintendo DS",
    "nintendo 3ds": "Nintendo - Nintendo 3DS", "3ds": "Nintendo - Nintendo 3DS",
    "playstation": "Sony - PlayStation", "ps1": "Sony - PlayStation", "psx": "Sony - PlayStation",
    "playstation 2": "Sony - PlayStation 2", "ps2": "Sony - PlayStation 2",
    "playstation 3": "Sony - PlayStation 3", "ps3": "Sony - PlayStation 3",
    "playstation portable": "Sony - PlayStation Portable", "psp": "Sony - PlayStation Portable",
    "playstation vita": "Sony - PlayStation Vita", "vita": "Sony - PlayStation Vita",
    "genesis": "Sega - Mega Drive - Genesis", "mega drive": "Sega - Mega Drive - Genesis",
    "master system": "Sega - Master System - Mark III",
    "sega saturn": "Sega - Saturn", "saturn": "Sega - Saturn",
    "dreamcast": "Sega - Dreamcast", "game gear": "Sega - Game Gear",
    "xbox": "Microsoft - Xbox", "xbox 360": "Microsoft - Xbox 360",
    "atari 2600": "Atari - 2600", "atari": "Atari - 2600",
}
_LR_REGION = {"PAL": ["Europe", "World", "USA"], "NTSC": ["USA", "World", "Europe"],
              "JP": ["Japan", "World", "USA"]}

def _lr_sanitize(name: str) -> str:
    # No-Intro/libretro naming: these chars become '_'
    return re.sub(r'[&*/:`<>?\\|]', "_", (name or "").strip())

async def _head_ok(client, url: str) -> bool:
    try:
        r = await client.head(url)
        if r.status_code == 405:          # some CDNs disallow HEAD
            r = await client.get(url)
        return r.status_code == 200
    except Exception:
        return False

async def _libretro_images(title: str, console: str, region: str = "PAL") -> List[dict]:
    if not (HAS_HTTPX and title):
        return []
    sysname = _LR_SYS.get((console or "").strip().lower())
    if not sysname:
        return []
    base_name = _lr_sanitize(re.sub(r"\s*\(.*?\)|\s*\[.*?\]", "", title).strip())
    variants = [f"{base_name} ({r})" for r in _LR_REGION.get(region, ["Europe", "USA", "World"])] + [base_name]
    out = []
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True,
                                     headers={"User-Agent": _UA}) as c:
            for kind, label in (("Named_Boxarts", "boxart"), ("Named_Snaps", "screenshot"),
                                ("Named_Titles", "title_screen")):
                for v in variants:
                    url = f"{_LR}/{sysname}/{kind}/{urllib.parse.quote(v)}.png"
                    if await _head_ok(c, url):
                        out.append({"url": url, "kind": label, "source": "libretro"})
                        break                 # one per kind is enough
    except Exception as e:
        log.debug("libretro: %s", e)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Current market (eBay + Vinted) via the existing search cap
# ─────────────────────────────────────────────────────────────────────────────

async def _current_market(query: str) -> dict:
    raw = _cap_raw("business.market.search")
    if not raw:
        return {}
    try:
        r = await raw(query=query, platform="any", limit=40, trace_id=None)
    except Exception as e:
        log.debug("market search: %s", e)
        return {}
    bp = r.get("by_platform") or {}
    return {"combined": r.get("combined") or {},
            "ebay": bp.get("ebay") or {}, "vinted": bp.get("vinted") or {},
            "sample_items": (r.get("items") or [])[:8]}


# ─────────────────────────────────────────────────────────────────────────────
# Reviews & sentiment (web search + LLM)
# ─────────────────────────────────────────────────────────────────────────────

async def _reviews(title: str, platform: str) -> dict:
    web = _cap_raw("web.search"); gen = _cap_raw("llm.generate")
    if not web or not gen:
        return {}
    q = f"{title} {platform} game review".strip()
    try:
        res = await web(query=q, limit=6, discover="off", trace_id=None)
    except Exception:
        return {}
    results = (res or {}).get("results", []) if isinstance(res, dict) else []
    if not results:
        return {}
    snips = "\n".join(f"- {r.get('title','')}: {r.get('snippet','')}" for r in results[:6])
    src = [r.get("url", "") for r in results[:5] if r.get("url")]
    try:
        g = await gen(job_type="chat",
            system="You summarise game reviews. Reply ONLY as JSON: {\"summary\": one "
                   "sentence verdict, \"sentiment\": \"positive\"|\"mixed\"|\"negative\", "
                   "\"score\": 0-100 critical-consensus estimate, \"highlights\": [2-4 short "
                   "praise/criticism points]}. No prose outside JSON.",
            prompt=f"Reviews for {title} ({platform}):\n{snips}", trace_id=None)
        m = re.search(r"\{[\s\S]*\}", (g or {}).get("text", "") or "")
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        data = {}
    if data:
        data["sources"] = src
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    @capability(
        "business.enrich.item", http_method="POST",
        http_path="/business/enrich/item", http_tags=["commerce"],
        description="Build a full dossier for an item from free sources: PriceCharting "
                    "(loose/CIB/new prices, FULL price history, rarity, metadata — "
                    "console, release year, edition, region, publisher, genre, players, "
                    "UPC), live eBay+Vinted GBP market stats, and an LLM review/sentiment "
                    "summary. Caches the result and records a price-history snapshot. "
                    "Input: title (str), upc (str), platform (str), category (str), "
                    "region (PAL|NTSC|JP default PAL), product_id (str — write the "
                    "valuation back onto this product), reviews (bool default true), "
                    "refresh (bool — bypass cache). Output: {ok, enrichment:{product, "
                    "prices, market, rarity, reviews, price_history, sources}}.")
    async def cap_enrich_item(title: str = "", upc: str = "", platform: str = "",
                              category: str = "", region: str = "PAL",
                              product_id: str = "", reviews: bool = True,
                              refresh: bool = False, trace_id=None):
        await _ensure_schema()
        if not (title or upc):
            return {"error": "title or upc required"}
        key = _norm_key(title, upc, platform)
        if not refresh:
            cached = _cache_get(key)
            if cached and cached.get("_updated_at"):
                # fresh enough? (parse iso loosely by comparing string date span is overkill;
                # rely on updated_at recency via a stored epoch)
                if time.time() - (cached.get("_epoch") or 0) < _CACHE_TTL:
                    return {"ok": True, "enrichment": cached, "cached": True}
        q = " ".join(x for x in [title, platform] if x).strip() or upc
        await emit_event({"type": "commerce.progress", "stage": "enrich.item",
                          "message": f"enrich '{(title or upc)[:40]}'"})
        pc, market, rev = await asyncio.gather(
            _pc_lookup(q, upc, region),
            _current_market(q),
            _reviews(title or q, platform) if reviews else _noop(),
        )
        pc = pc or {}
        pmeta = pc.get("meta") or {}
        product = {"title": title or q, "upc": upc or pmeta.get("upc", ""),
                   "console": pmeta.get("console") or platform,
                   "release_year": pmeta.get("release_year", ""),
                   "edition": pmeta.get("edition", ""), "region": pmeta.get("region") or region,
                   "publisher": pmeta.get("publisher", ""), "developer": pmeta.get("developer", ""),
                   "genre": pmeta.get("genre", ""), "players": pmeta.get("players", ""),
                   "esrb": pmeta.get("esrb", ""), "pricecharting_url": pc.get("url", ""),
                   "cover": pc.get("cover", "")}
        enrichment = {
            "product": product,
            "prices": {"pricecharting": pc.get("prices") or {}},
            "market": market or {},
            "rarity": pc.get("rarity") or {},
            "reviews": rev or {},
            "price_history": pc.get("history") or [],
            "alternatives": pc.get("alternatives") or [],
            "sources": [s for s in [pc.get("url"), "eBay Browse", "Vinted"] if s],
            "_epoch": time.time(),
        }
        _cache_put(key, upc, title or q, enrichment)
        # record snapshot points for our own accruing history
        snaps = []
        pcp = pc.get("prices") or {}
        for v in ("loose", "cib", "new"):
            if pcp.get(v):
                snaps.append({"ts": int(time.time() * 1000), "source": "pricecharting",
                              "variant": v, "value": pcp[v], "currency": "GBP"})
        med = (market.get("combined") or {}).get("median")
        if med:
            snaps.append({"ts": int(time.time() * 1000), "source": "ebay_vinted",
                          "variant": "market", "value": med, "currency": "GBP",
                          "sample": (market.get("combined") or {}).get("sample_size", 0)})
        _hist_record(key, product_id, snaps)
        # write valuation back onto the product for at-a-glance inventory value
        if product_id:
            await _write_valuation(product_id, enrichment)
        return {"ok": True, "enrichment": enrichment, "cached": False}

    async def _noop():
        return {}

    async def _write_valuation(product_id: str, enrichment: dict):
        core = _core()
        if not core:
            return
        prod = await _run(core._db_get_product, product_id)
        if not prod:
            return
        pcp = enrichment.get("prices", {}).get("pricecharting", {}) or {}
        med = (enrichment.get("market", {}).get("combined") or {}).get("median")
        val = pcp.get("cib") or med or pcp.get("loose")
        attrs = dict(prod.get("attributes") or {})
        attrs["valuation"] = {
            "market_gbp": val, "pc_loose": pcp.get("loose"), "pc_cib": pcp.get("cib"),
            "pc_new": pcp.get("new"), "ebay_median": med,
            "rarity": (enrichment.get("rarity") or {}).get("label"),
            "console": enrichment["product"].get("console"),
            "release_year": enrichment["product"].get("release_year"),
            "edition": enrichment["product"].get("edition"),
            "region": enrichment["product"].get("region"),
            "at": now_iso()}
        await _run(core._db_upsert_product, {"id": product_id, "attributes": attrs})

    @capability(
        "business.pricehistory.get", http_method="GET",
        http_path="/business/pricehistory/get", http_tags=["commerce"],
        memory="off", silent=True,
        description="Full price history for an item, ready to chart: PriceCharting's "
                    "complete per-condition series (loose/CIB/new, back to launch) plus "
                    "every valuation snapshot Vera has recorded over time. Input: title, "
                    "upc, platform. Output: {ok, series:{loose:[{ts,value}], cib:[...], "
                    "new:[...], market:[...]}, points, currency}.")
    async def cap_pricehistory_get(title: str = "", upc: str = "", platform: str = "",
                                   trace_id=None):
        await _ensure_schema()
        key = _norm_key(title, upc, platform)
        series = {"loose": [], "cib": [], "new": [], "market": [], "sold": []}
        cached = _cache_get(key)
        if cached:
            for pt in cached.get("price_history") or []:
                v = pt.get("variant")
                if v in series:
                    series[v].append({"ts": pt.get("ts"), "value": pt.get("value")})
        # merge our recorded snapshots (dedupe by ts+variant against PC series)
        for s in _hist_snapshots(key):
            v = "market" if s.get("variant") == "market" else s.get("variant")
            if v in series:
                series[v].append({"ts": s.get("ts"), "value": s.get("value")})
        for v in series:
            series[v].sort(key=lambda p: p.get("ts") or 0)
        n = sum(len(x) for x in series.values())
        return {"ok": True, "series": series, "points": n, "currency": "GBP"}

    @capability(
        "business.enrich.refresh_all", http_method="POST",
        http_path="/business/enrich/refresh_all", http_tags=["commerce"],
        description="Re-value every inventory product from PriceCharting + the live "
                    "market so the shop's stock valuation stays current, recording a "
                    "price-history point for each. Skips reviews for speed. Schedule this "
                    "nightly. Input: store_id (str — scope), limit (int default 200), "
                    "only_stale (bool default true — skip items valued in the last day). "
                    "Output: {ok, updated, skipped, errors}.")
    async def cap_enrich_refresh_all(store_id: str = "", limit: int = 200,
                                     only_stale: bool = True, trace_id=None):
        await _ensure_schema()
        core = _core()
        if not core:
            return {"error": "commerce core not loaded"}
        prods = await _run(core._db_list_products, "", "", int(limit), store_id)
        updated = skipped = errors = 0
        for p in prods:
            attrs = p.get("attributes") or {}
            val = attrs.get("valuation") or {}
            if only_stale and val.get("at"):
                try:
                    # cheap staleness check via the epoch stored in the cache
                    key = _norm_key(p.get("name", ""), p.get("upc", ""), "")
                    c = _cache_get(key)
                    if c and time.time() - (c.get("_epoch") or 0) < _VAL_TTL:
                        skipped += 1; continue
                except Exception:
                    pass
            try:
                r = await cap_enrich_item(title=p.get("name", ""), upc=p.get("upc", ""),
                                          platform=(attrs.get("platform") or ""),
                                          product_id=p["id"], reviews=False,
                                          refresh=True, trace_id=None)
                if r.get("ok"): updated += 1
                else: errors += 1
            except Exception as e:
                log.debug("refresh %s: %s", p.get("id"), e); errors += 1
            await asyncio.sleep(1.2)          # be gentle on PriceCharting
        await emit_event({"type": "commerce.progress", "stage": "enrich.refresh_all",
                          "message": f"revalued {updated} item(s)"})
        return {"ok": True, "updated": updated, "skipped": skipped, "errors": errors,
                "total": len(prods)}

    @capability(
        "business.market.sold", http_method="POST",
        http_path="/business/market/sold", http_tags=["commerce"],
        description="Recently SOLD comparables for a query. eBay blocks direct sold-"
                    "listing scraping, so this returns PriceCharting's realised price "
                    "points (its prices ARE derived from actual eBay sales) as the sold "
                    "signal, newest last. Input: title, upc, platform, region. "
                    "Output: {ok, sold:[{ts,variant,value,currency}], note}.")
    async def cap_market_sold(title: str = "", upc: str = "", platform: str = "",
                              region: str = "PAL", trace_id=None):
        await _ensure_schema()
        q = " ".join(x for x in [title, platform] if x).strip() or upc
        pc = await _pc_lookup(q, upc, region) or {}
        hist = pc.get("history") or []
        recent = sorted(hist, key=lambda h: h.get("ts") or 0)[-24:]
        return {"ok": True, "sold": [{"ts": h["ts"], "variant": h["variant"],
                                      "value": h["value"], "currency": "GBP"} for h in recent],
                "note": "Realised prices via PriceCharting (derived from eBay sales)."}

    @capability(
        "business.catalog.products", http_method="GET",
        http_path="/business/catalog/products", http_tags=["commerce"],
        memory="off", silent=True,
        description="Browse the internal product DB that accretes as you scan: every "
                    "product Vera has enriched, with its latest valuation, when it was "
                    "last refreshed (age/stale), and how many have sold over time. "
                    "Input: q (title search), limit, stale_days (default 7 — mark items "
                    "not refreshed in N days as due). Output: {products:[{key,title,"
                    "console,cib,market,rarity,updated_at,age_days,stale,sold_count,"
                    "last_sold}], count}.")
    async def cap_catalog_products(q: str = "", limit: int = 300, stale_days: int = 7,
                                   trace_id=None):
        await _ensure_schema()
        def _rows():
            conn = _sqlite_conn()
            try:
                sql = "SELECT key,title,upc,data,updated_at FROM commerce_enrichment"; args = []
                if q:
                    sql += " WHERE title LIKE ?"; args.append(f"%{q}%")
                sql += " ORDER BY updated_at DESC LIMIT ?"; args.append(int(limit))
                rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
                sold = {}
                for r in conn.execute("SELECT item_key, COUNT(*) n, MAX(value) mx, MAX(ts) t "
                                      "FROM commerce_price_history WHERE variant='sold' "
                                      "GROUP BY item_key").fetchall():
                    d = dict(r); sold[d["item_key"]] = {"n": d["n"], "last": d["mx"], "t": d["t"]}
                return rows, sold
            finally:
                conn.close()
        rows, sold = await _run(_rows)
        out = []
        for r in rows:
            try:
                data = json.loads(r.get("data") or "{}")
            except Exception:
                data = {}
            prod = data.get("product") or {}
            pc = (data.get("prices") or {}).get("pricecharting") or {}
            epoch = data.get("_epoch") or 0
            age = int((time.time() - epoch) / 86400) if epoch else None
            s = sold.get(r["key"]) or {}
            out.append({"key": r["key"], "title": r.get("title"), "console": prod.get("console"),
                        "cib": pc.get("cib"),
                        "market": ((data.get("market") or {}).get("combined") or {}).get("median"),
                        "rarity": (data.get("rarity") or {}).get("label"),
                        "updated_at": r.get("updated_at"), "age_days": age,
                        "stale": (age is not None and age >= int(stale_days)),
                        "sold_count": s.get("n", 0), "last_sold": s.get("last")})
        return {"products": out, "count": len(out)}

    @capability(
        "business.images.fetch", http_method="POST",
        http_path="/business/images/fetch", http_tags=["commerce"],
        description="Fetch box art & in-game SCREENSHOTS for a game from free retro "
                    "image libraries — libretro-thumbnails (box art, snapshots, title "
                    "screens, region-aware) plus the PriceCharting cover — to drop into "
                    "a listing. Input: title (str!), console (str), region (PAL|NTSC|JP "
                    "default PAL), cover (str — a known cover URL to include), upc. "
                    "Output: {ok, images:[{url, kind(boxart|screenshot|title_screen), "
                    "source}]}.")
    async def cap_images_fetch(title: str = "", console: str = "", region: str = "PAL",
                               cover: str = "", upc: str = "", trace_id=None):
        if not title:
            return {"error": "title required"}
        imgs = []
        if cover:
            imgs.append({"url": cover, "kind": "boxart", "source": "pricecharting"})
        imgs += await _libretro_images(title, console, region)
        seen, out = set(), []
        for i in imgs:
            if i.get("url") and i["url"] not in seen:
                seen.add(i["url"]); out.append(i)
        await emit_event({"type": "commerce.progress", "stage": "images.fetch",
                          "message": f"{len(out)} image(s) for '{title[:32]}'"})
        return {"ok": True, "images": out}

    log.info("business.enrich: ready (PriceCharting scrape + market + reviews + history + art)")
