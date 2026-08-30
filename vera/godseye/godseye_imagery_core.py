"""godseye_imagery_core.py — geospatial image search: sources and parsers.

"Find images taken near here", not "identify where this image was taken". The
first is tractable with open APIs; the second needs closed services and is out
of scope deliberately.

Vera queries several providers by bounding box, normalises them into one record
shape and serves the result, so the globe gets a single imagery layer and new
providers are a server-side change — the same arrangement as the CCTV manifest.

App-free (no HTTP client, no orchestrator): the parsers are the part that goes
quietly wrong when a provider changes its payload, so they are unit-testable
against captured fixtures. Every shape below was captured from a live response
on 2026-08-30, after two CCTV parsers written from *plausible guesses* would
have returned nothing from every provider while looking perfectly healthy.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

#: One record shape for every provider. `capturedAt` is carried from the start
#: because "this place through time" is a date filter over exactly this field —
#: cheap now, painful to retrofit. `license`/`attribution` likewise: Commons and
#: iNaturalist are CC-licensed with attribution requirements, and bolting that
#: on after the UI exists means auditing every call site.
RECORD_KEYS = ("id", "lat", "lng", "title", "thumbUrl", "fullUrl", "pageUrl",
               "capturedAt", "provider", "license", "attribution", "bearing")

#: Providers needing no key at all — probed 2026-08-30, all returned 200.
OPEN_PROVIDERS = ("commons", "inaturalist", "wikipedia")

#: Free but key-gated. Absent a key they are skipped silently, exactly like the
#: Google 3D tiles path: present a key, get the feature.
KEYED_PROVIDERS = {
    "mapillary": "VERA_MAPILLARY_TOKEN",
    "flickr": "VERA_FLICKR_KEY",
}

#: Mapillary matters disproportionately: it is the only source here that ships
#: a camera BEARING, and pose is what photogrammetry/splatting needs later.

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
INAT_API = "https://api.inaturalist.org/v1/observations"
MAPILLARY_API = "https://graph.mapillary.com/images"
FLICKR_API = "https://api.flickr.com/services/rest/"

#: Commons geosearch happily returns audio and video — it is a *file* search,
#: not an image search. Filtering on mime is not optional.
_IMAGE_MIME = re.compile(r"^image/", re.I)


def _num(v: Any, limit: float = 180.0) -> Optional[float]:
    """`limit` is explicit because this is used for two different kinds of
    number: coordinates (±180) and compass bearings (0–360). Defaulting a
    bearing to the coordinate bound silently discarded every Mapillary pose —
    which is the one field that source is carried for."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) <= limit else None      # f == f rejects NaN


def _pages(payload: Any) -> Dict[str, Any]:
    """MediaWiki `query.pages`, or nothing. A provider serving an HTML error
    page must yield no results, not raise and take the whole fan-out down."""
    if not isinstance(payload, dict):
        return {}
    query = payload.get("query")
    pages = query.get("pages") if isinstance(query, dict) else None
    return pages if isinstance(pages, dict) else {}


def _rec(**kw) -> Dict[str, Any]:
    return {k: kw.get(k) for k in RECORD_KEYS}


# ── bbox helpers ─────────────────────────────────────────────────────────────
def bbox_centre(bbox: Tuple[float, float, float, float]) -> Tuple[float, float, float]:
    """(south, west, north, east) -> (lat, lon, radius_m).

    Commons and Wikipedia only speak point+radius, so a bbox has to be reduced.
    The radius is the half-diagonal, so the circle CONTAINS the box rather than
    being inscribed in it — under-covering would silently drop corner results.
    """
    south, west, north, east = (float(v) for v in bbox)
    lat = (south + north) / 2.0
    lon = (west + east) / 2.0
    dlat_m = abs(north - south) * 111_320.0 / 2.0
    # Longitude degrees shrink with latitude; guard the poles where cos -> 0.
    dlon_m = abs(east - west) * 111_320.0 * max(0.01, math.cos(math.radians(lat))) / 2.0
    return lat, lon, math.sqrt(dlat_m ** 2 + dlon_m ** 2)


def clamp_radius(radius_m: float, ceiling: float = 10_000.0) -> int:
    """MediaWiki rejects gsradius above 10 km outright."""
    return int(max(10.0, min(float(radius_m), ceiling)))


# ── request builders (pure: URL + params, no I/O) ────────────────────────────
def commons_geosearch_params(lat: float, lon: float, radius_m: float,
                             limit: int = 50) -> Dict[str, str]:
    return {"action": "query", "list": "geosearch", "format": "json",
            "gscoord": f"{lat}|{lon}", "gsradius": str(clamp_radius(radius_m)),
            "gslimit": str(max(1, min(int(limit), 500))),
            "gsnamespace": "6"}                     # 6 = File:


def commons_imageinfo_params(pageids: List[int], thumb_px: int = 320) -> Dict[str, str]:
    """Geosearch returns no urls at all, so a second call is unavoidable — but
    it batches, so it stays one request per page of results, not one per image."""
    return {"action": "query", "format": "json",
            "pageids": "|".join(str(int(p)) for p in pageids),
            "prop": "imageinfo", "iiprop": "url|mime|extmetadata",
            "iiurlwidth": str(int(thumb_px))}


def wikipedia_geosearch_params(lat: float, lon: float, radius_m: float,
                               limit: int = 30, thumb_px: int = 320) -> Dict[str, str]:
    """Used as a generator so coordinates and thumbnails arrive together — one
    request, unlike Commons."""
    return {"action": "query", "format": "json", "generator": "geosearch",
            "ggscoord": f"{lat}|{lon}", "ggsradius": str(clamp_radius(radius_m)),
            "ggslimit": str(max(1, min(int(limit), 500))),
            "prop": "pageimages|coordinates", "piprop": "thumbnail",
            "pithumbsize": str(int(thumb_px))}


def inat_params(bbox: Tuple[float, float, float, float], limit: int = 50) -> Dict[str, str]:
    south, west, north, east = (float(v) for v in bbox)
    return {"swlat": str(south), "swlng": str(west),
            "nelat": str(north), "nelng": str(east),
            "photos": "true", "per_page": str(max(1, min(int(limit), 200))),
            "order_by": "observed_on"}


def mapillary_params(bbox: Tuple[float, float, float, float], token: str,
                     limit: int = 50) -> Dict[str, str]:
    south, west, north, east = (float(v) for v in bbox)
    return {"bbox": f"{west},{south},{east},{north}",
            "fields": "id,computed_geometry,thumb_1024_url,captured_at,compass_angle",
            "limit": str(max(1, min(int(limit), 200))), "access_token": token}


def flickr_params(bbox: Tuple[float, float, float, float], key: str,
                  limit: int = 50) -> Dict[str, str]:
    south, west, north, east = (float(v) for v in bbox)
    return {"method": "flickr.photos.search", "api_key": key, "format": "json",
            "nojsoncallback": "1", "bbox": f"{west},{south},{east},{north}",
            "has_geo": "1", "extras": "geo,url_s,url_m,date_taken,license,owner_name",
            "per_page": str(max(1, min(int(limit), 250)))}


# ── parsers ──────────────────────────────────────────────────────────────────
def parse_commons(geosearch: Any, imageinfo: Any) -> List[Dict[str, Any]]:
    """Join geosearch (coordinates) to imageinfo (urls) on pageid."""
    coords: Dict[int, Dict[str, Any]] = {}
    _q = geosearch.get("query") if isinstance(geosearch, dict) else None
    for row in ((_q or {}).get("geosearch") or [] if isinstance(_q, dict) else []):
        if not isinstance(row, dict):
            continue
        lat, lon = _num(row.get("lat")), _num(row.get("lon"))
        if lat is None or lon is None:
            continue
        coords[int(row.get("pageid", -1))] = {"lat": lat, "lng": lon,
                                              "title": row.get("title") or ""}

    out: List[Dict[str, Any]] = []
    for pid, page in _pages(imageinfo).items():
        try:
            pid_i = int(pid)
        except (TypeError, ValueError):
            continue
        geo = coords.get(pid_i)
        info = (page or {}).get("imageinfo") or []
        if not geo or not info:
            continue
        ii = info[0] or {}
        # Commons geosearch is a FILE search: it returns .ogg audio and video
        # alongside photographs. Without this the layer fills with non-images.
        if not _IMAGE_MIME.match(str(ii.get("mime") or "")):
            continue
        meta = ii.get("extmetadata") or {}

        def _meta(key: str) -> str:
            v = (meta.get(key) or {}).get("value") or ""
            return re.sub(r"<[^>]+>", "", str(v)).strip()

        # "File:Map of Punt region.jpg" is a wiki page name, not a caption —
        # strip both the namespace and the extension for display.
        title = geo["title"] or ""
        if title.startswith("File:"):
            title = title[5:]
        title = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", title)
        out.append(_rec(
            id=f"commons-{pid_i}", lat=geo["lat"], lng=geo["lng"],
            title=title,
            thumbUrl=ii.get("thumburl") or ii.get("url"),
            fullUrl=ii.get("url"),
            pageUrl=f"https://commons.wikimedia.org/?curid={pid_i}",
            capturedAt=_meta("DateTimeOriginal") or _meta("DateTime") or None,
            provider="Wikimedia Commons",
            license=_meta("LicenseShortName") or None,
            attribution=_meta("Artist") or None, bearing=None))
    return out


def parse_wikipedia(payload: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for pid, page in _pages(payload).items():
        if not isinstance(page, dict):
            continue
        coord = (page.get("coordinates") or [{}])[0] or {}
        lat, lon = _num(coord.get("lat")), _num(coord.get("lon"))
        thumb = (page.get("thumbnail") or {}).get("source")
        if lat is None or lon is None or not thumb:
            continue                                    # an article with no image is not imagery
        out.append(_rec(
            id=f"wikipedia-{pid}", lat=lat, lng=lon, title=page.get("title") or "",
            thumbUrl=thumb, fullUrl=thumb,
            pageUrl=f"https://en.wikipedia.org/?curid={pid}",
            capturedAt=None, provider="Wikipedia",
            license="See page", attribution=None, bearing=None))
    return out


def parse_inaturalist(payload: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    rows = payload.get("results") if isinstance(payload, dict) else None
    for obs in (rows if isinstance(rows, list) else []):
        if not isinstance(obs, dict):
            continue
        # `location` is a "lat,lng" STRING, not a pair of numbers.
        parts = str(obs.get("location") or "").split(",")
        if len(parts) != 2:
            continue
        lat, lon = _num(parts[0]), _num(parts[1])
        photos = obs.get("photos") or []
        if lat is None or lon is None or not photos:
            continue
        photo = photos[0] or {}
        square = str(photo.get("url") or photo.get("square_url") or "")
        if not square:
            continue
        out.append(_rec(
            id=f"inat-{obs.get('id')}", lat=lat, lng=lon,
            title=obs.get("species_guess") or "iNaturalist observation",
            thumbUrl=square,
            # iNat photo urls are sized by filename segment; "large" is the
            # same asset at a usable resolution.
            fullUrl=square.replace("/square.", "/large."),
            pageUrl=obs.get("uri") or None,
            capturedAt=obs.get("observed_on") or obs.get("time_observed_at") or None,
            provider="iNaturalist", license=photo.get("license_code") or None,
            attribution=(obs.get("user") or {}).get("login"), bearing=None))
    return out


def parse_mapillary(payload: Any) -> List[Dict[str, Any]]:
    """The only provider carrying a camera bearing — keep it, later work needs pose."""
    out: List[Dict[str, Any]] = []
    rows = payload.get("data") if isinstance(payload, dict) else None
    for img in (rows if isinstance(rows, list) else []):
        if not isinstance(img, dict):
            continue
        coords = ((img.get("computed_geometry") or {}).get("coordinates") or [])
        if len(coords) != 2:
            continue
        lon, lat = _num(coords[0]), _num(coords[1])      # GeoJSON is lon,lat
        thumb = img.get("thumb_1024_url")
        if lat is None or lon is None or not thumb:
            continue
        out.append(_rec(
            id=f"mapillary-{img.get('id')}", lat=lat, lng=lon,
            title="Mapillary street view", thumbUrl=thumb, fullUrl=thumb,
            pageUrl=f"https://www.mapillary.com/app/?pKey={img.get('id')}",
            capturedAt=img.get("captured_at"), provider="Mapillary",
            license="CC BY-SA", attribution=None,
            bearing=_num(img.get("compass_angle"), limit=360.0)))
    return out


def parse_flickr(payload: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    photos = payload.get("photos") if isinstance(payload, dict) else None
    rows = photos.get("photo") if isinstance(photos, dict) else None
    for p in (rows if isinstance(rows, list) else []):
        if not isinstance(p, dict):
            continue
        lat, lon = _num(p.get("latitude")), _num(p.get("longitude"))
        thumb = p.get("url_s") or p.get("url_m")
        if lat is None or lon is None or not thumb:
            continue
        out.append(_rec(
            id=f"flickr-{p.get('id')}", lat=lat, lng=lon, title=p.get("title") or "",
            thumbUrl=thumb, fullUrl=p.get("url_m") or thumb,
            # A Flickr owner id looks like "42@N00"; percent-encoding the @
            # produces a url that does not resolve.
            pageUrl=("https://www.flickr.com/photos/"
                     f"{quote(str(p.get('owner') or ''), safe='@')}/{p.get('id')}"),
            capturedAt=p.get("datetaken") or None, provider="Flickr",
            license=str(p.get("license") or "") or None,
            attribution=p.get("ownername") or None, bearing=None))
    return out


# ── assembly ─────────────────────────────────────────────────────────────────
def dedupe(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Same id, or the same provider at the same 5-decimal point, is one image."""
    out, seen = [], set()
    for r in records:
        lat, lng = r.get("lat"), r.get("lng")
        if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
            continue
        key = r.get("id") or f"{r.get('provider')}:{lat:.5f}:{lng:.5f}"
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def build_result(groups: List[List[Dict[str, Any]]], *, bbox, generated_at: str,
                 errors: Optional[Dict[str, str]] = None,
                 skipped: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    images = dedupe([r for g in groups for r in (g or [])])
    by_provider: Dict[str, int] = {}
    for r in images:
        p = r.get("provider") or "unknown"
        by_provider[p] = by_provider.get(p, 0) + 1
    return {
        "generatedAt": generated_at,
        "bbox": list(bbox) if bbox else None,
        "count": len(images),
        "byProvider": by_provider,
        # A provider erroring and a provider being skipped for want of a key are
        # different facts; collapsing them hides a broken source behind "no key".
        "errors": errors or {},
        "skipped": skipped or {},
        "images": images,
    }
