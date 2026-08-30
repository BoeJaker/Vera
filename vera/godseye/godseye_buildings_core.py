"""godseye_buildings_core.py — OSM building footprints for 3D extrusion.

The other half of "3D maps" without a vendor: Terrarium gives the ground its
relief, and OSM gives it buildings. Overpass is the source; it is free and
needs no key, but it is also slow, aggressively rate-limited and shared
infrastructure, so caching and hard bounds are part of the design rather than
an optimisation.

App-free (no HTTP, no orchestrator) so the query builder, the height heuristics
and the geometry parsing are testable against a captured payload. The shape
below was captured live on 2026-08-30: `out tags geom` returns coordinates
inline as ``geometry: [{lat, lon}, ...]``, which is what makes one request per
area enough — the alternative resolves node ids separately.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

#: Storey height when a building declares levels but not metres. OSM's own wiki
#: uses ~3 m as the conventional assumption.
LEVEL_HEIGHT_M = 3.0

#: What to extrude when a building declares neither. Better a plausible low box
#: than a flat polygon that reads as missing data.
DEFAULT_HEIGHT_M = 8.0

#: Overpass is shared infrastructure. A whole-city bbox is minutes of somebody
#: else's CPU, so refuse anything larger outright rather than waiting for the
#: server to time out. ~0.04 deg2 is roughly a large neighbourhood.
MAX_BBOX_DEG2 = 0.04

#: Even inside that area a dense centre yields tens of thousands of ways, which
#: no browser will extrude smoothly.
DEFAULT_LIMIT = 1500

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def bbox_area_deg2(bbox: Sequence[float]) -> float:
    south, west, north, east = (float(v) for v in bbox)
    return abs(north - south) * abs(east - west)


def bbox_is_sane(bbox: Sequence[float]) -> Tuple[bool, str]:
    """Reject a query before it is sent, not after Overpass times out."""
    try:
        south, west, north, east = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return False, "bbox must be four numbers [south, west, north, east]"
    if south >= north or west >= east:
        return False, "bbox must be [south, west, north, east] with south < north"
    if not (-90 <= south < north <= 90) or not (-180 <= west < east <= 180):
        return False, "bbox out of range"
    area = bbox_area_deg2(bbox)
    if area > MAX_BBOX_DEG2:
        return False, (f"bbox too large ({area:.4f} deg2 > {MAX_BBOX_DEG2}); "
                       "Overpass is shared infrastructure - zoom in")
    return True, ""


def overpass_query(bbox: Sequence[float], timeout: int = 25) -> str:
    """`out tags geom` so coordinates arrive inline — one request, not two.

    Relations are included because large or courtyard buildings are multipolygons
    and omitting them silently drops exactly the landmarks people look for.
    """
    south, west, north, east = (float(v) for v in bbox)
    box = f"{south},{west},{north},{east}"
    return (f"[out:json][timeout:{int(timeout)}];"
            f'(way["building"]({box});relation["building"]({box}););'
            f"out tags geom;")


def parse_height(tags: Dict[str, Any]) -> float:
    """OSM heights are free text: "12", "12.5 m", "40'" and so on.

    Pull the first number out rather than trusting the field to be numeric, and
    fall back through levels to a default so every footprint extrudes to
    something. A zero-height building is indistinguishable from missing data.
    """
    tags = tags or {}
    raw = str(tags.get("height") or tags.get("building:height") or "").strip()
    m = _NUM.search(raw)
    if m:
        try:
            h = float(m.group())
            if h > 0:
                return h
        except ValueError:
            pass
    levels = str(tags.get("building:levels") or tags.get("levels") or "").strip()
    m = _NUM.search(levels)
    if m:
        try:
            n = float(m.group())
            if n > 0:
                return n * LEVEL_HEIGHT_M
        except ValueError:
            pass
    return DEFAULT_HEIGHT_M


def parse_min_height(tags: Dict[str, Any]) -> float:
    """`min_height` lifts a structure off the ground (bridges, overhangs)."""
    m = _NUM.search(str((tags or {}).get("min_height") or ""))
    if not m:
        return 0.0
    try:
        v = float(m.group())
        return v if v > 0 else 0.0
    except ValueError:
        return 0.0


def parse_overpass(payload: Any, limit: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
    """Overpass elements -> flat footprints ready to extrude.

    Coordinates come out as a flat [lon, lat, lon, lat, ...] array because that
    is exactly what Cesium's `Cartesian3.fromDegreesArray` wants; building it
    here avoids a per-vertex transform in the browser for thousands of shapes.
    """
    if not isinstance(payload, dict):
        return []
    elements = payload.get("elements")
    if not isinstance(elements, list):
        return []

    out: List[Dict[str, Any]] = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        geom = el.get("geometry")
        if not isinstance(geom, list) or len(geom) < 3:
            continue                      # not a closed shape; nothing to extrude
        flat: List[float] = []
        for pt in geom:
            if not isinstance(pt, dict):
                continue
            lat, lon = pt.get("lat"), pt.get("lon")
            if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                continue
            flat.extend((float(lon), float(lat)))
        if len(flat) < 6:                 # fewer than three usable vertices
            continue
        tags = el.get("tags") if isinstance(el.get("tags"), dict) else {}
        out.append({
            "id": f"{el.get('type', 'way')}-{el.get('id')}",
            "height": parse_height(tags),
            "minHeight": parse_min_height(tags),
            "name": tags.get("name") or "",
            "coords": flat,
        })
        if len(out) >= limit:
            break
    return out


def bbox_cache_key(bbox: Sequence[float], precision: int = 3) -> str:
    """Snap to a grid so panning slightly re-uses the cached answer instead of
    hammering Overpass with near-identical queries."""
    south, west, north, east = (round(float(v), precision) for v in bbox)
    return f"{south}_{west}_{north}_{east}"


def build_result(buildings: List[Dict[str, Any]], *, bbox: Sequence[float],
                 generated_at: str, cached: bool = False,
                 truncated: bool = False) -> Dict[str, Any]:
    return {
        "generatedAt": generated_at,
        "bbox": [float(v) for v in bbox],
        "count": len(buildings),
        "cached": cached,
        # The UI needs to know it is showing a subset, otherwise a dense centre
        # silently looks like a sparse one.
        "truncated": truncated,
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "buildings": buildings,
    }
