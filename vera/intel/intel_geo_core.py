"""Intelligence - place names to coordinates (pure: keys and parsing only).

`geo.lookup` asks OpenStreetMap's Nominatim (public, keyless, at most one
request a second) and caches every answer, so a place is looked up once. Signals
carry the places they name as `places: [{name, lat, lon, country, cc}]`.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "vera-intel/1 (self-hosted research assistant)"
MIN_INTERVAL_S = 1.1                       # Nominatim's usage policy: at most 1 request per second
MISS_TTL_DAYS = 30

# Entity types (the topic label sets) that name a place.
PLACE_LABELS = {"location", "city or region", "country", "development site", "military facility",
                "planning authority"}


def geo_key(name: str, cc: str = "") -> str:
    k = re.sub(r"\s+", " ", re.sub(r"[^\w ,.-]+", " ", str(name or "").lower())).strip(" ,.-")
    return f"{k}|{cc.lower()}" if cc else k


def parse(results: Any, name: str = "") -> Optional[Dict[str, Any]]:
    """The best Nominatim hit as {name, lat, lon, bbox:[south, north, west, east],
    country, cc, admin, type, importance}, or None."""
    if not isinstance(results, list) or not results:
        return None
    r = results[0] if isinstance(results[0], dict) else {}
    try:
        lat, lon = float(r["lat"]), float(r["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    addr = r.get("address") or {}
    try:
        bbox = [float(x) for x in (r.get("boundingbox") or [])][:4]
    except (TypeError, ValueError):
        bbox = []
    return {"name": r.get("name") or name, "lat": round(lat, 5), "lon": round(lon, 5), "bbox": bbox,
            "country": addr.get("country") or "", "cc": (addr.get("country_code") or "").lower(),
            "admin": addr.get("state") or addr.get("region") or "", "type": r.get("addresstype") or r.get("type") or "",
            "importance": round(float(r.get("importance") or 0), 3), "display": str(r.get("display_name") or "")[:160]}


def places_in(entity_types: Dict[str, str], limit: int = 2) -> List[str]:
    """The place names among a signal's typed entities, in their order."""
    out = []
    for name, lab in (entity_types or {}).items():
        if lab in PLACE_LABELS and name not in out and 2 <= len(name) <= 80:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def pins(signals, *, recent_after: float = 0.0, limit: int = 60):
    """A topic's places as globe pins: one per place, sized by how many signals
    name it, the strongest story as its detail, lit when named recently."""
    from datetime import datetime
    agg = {}
    for s in signals:
        for pl in s.get("places") or []:
            try:
                lat, lon = float(pl["lat"]), float(pl["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            k = (str(pl.get("name") or "").lower(), round(lat, 2), round(lon, 2))
            a = agg.setdefault(k, {"name": pl.get("name") or pl.get("as") or "?", "lat": lat, "lon": lon,
                                   "size": 0, "best": -1.0, "detail": "", "url": "", "last": ""})
            a["size"] += 1
            sc = float(s.get("score") or 0)
            if sc > a["best"]:
                a["best"], a["detail"], a["url"] = sc, str(s.get("title") or ""), str(s.get("url") or "")
            seen = str(s.get("seen") or "")
            if seen > a["last"]:
                a["last"] = seen
    out = []
    for a in sorted(agg.values(), key=lambda a: (-a["size"], -a["best"], a["name"])):
        try:
            lit = datetime.fromisoformat(a["last"].replace("Z", "+00:00")).timestamp() >= recent_after if a["last"] else False
        except ValueError:
            lit = False
        out.append({"name": a["name"], "lat": a["lat"], "lon": a["lon"], "size": a["size"],
                    "meta": f"{a['size']} source{'s' if a['size'] != 1 else ''} · {a['last'][:10]}",
                    "detail": a["detail"][:140], "url": a["url"], "lit": lit})
    return out[:limit]
