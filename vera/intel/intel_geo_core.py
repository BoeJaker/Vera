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
