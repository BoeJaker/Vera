"""godseye_cctv_core.py — pure CCTV source registry and feed normalisation.

Vera becomes the source of truth for Godseye's camera list. Godseye ships a
committed ``public/manifests/cctv-verified.json`` and separately live-fetches a
handful of providers in the browser; pointing its ``VERIFIED_CCTV_MANIFEST``
constant at Vera instead means new sources can be added here, server side,
without touching the fork or rebuilding it.

App-free on purpose (no HTTP client, no orchestrator) so the parsers — the part
that silently produces zero cameras when a provider changes its payload — are
unit-testable against fixtures.

Every source below was PROBED before being included (2026-08-30). Several
providers that Godseye still calls have since closed: Alberta, Florida and
Georgia now answer ``<Error><Message>Invalid Key</Message></Error>``, and New
Jersey and Utah return 403. Those are recorded in ``CLOSED_SOURCES`` rather than
deleted, so nobody re-adds them expecting them to work.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional

#: Output shape is Godseye's own: mergeFeeds() drops anything without finite
#: `lat`/`lng` (note lng, NOT lon) and dedupes on provider:id:lat:lng.
FEED_KEYS = ("id", "name", "lat", "lng", "url", "videoUrl", "fallbackUrl",
             "city", "mediaType", "refreshSeconds", "provider",
             # Most agency cameras are STILLS by design. These two carry the
             # difference: `streamCapable` says the operator publishes live
             # video for this camera, `detailsUrl` is the page where the stream
             # url has to be read from (it is not derivable — the page slug
             # "us101eureka5thrstreetlookingnorth" becomes "eureka_5th_r").
             "streamCapable", "detailsUrl")

#: A camera is only "video" if you can actually watch motion. Stills refreshed
#: every few minutes are not, however often they update.
VIDEO_MEDIA_TYPES = ("stream", "video")

#: Caltrans publishes one district file per district, 01-12, and each really is
#: distinct data (verified by hash — d01/d04/d08/d12 all differ). Godseye reads
#: only district 08, so the other eleven are pure additional coverage.
CALTRANS_DISTRICTS = [f"{d:02d}" for d in range(1, 13)]

SOURCES: List[Dict[str, Any]] = [
    *[{"id": f"caltrans-d{d}", "kind": "caltrans", "provider": "Caltrans",
       "region": f"California D{d}", "refresh_seconds": 300,
       "url": f"https://cwwp2.dot.ca.gov/vm/js/cctv{d}.js"}
      for d in CALTRANS_DISTRICTS],
    {"id": "on-511", "kind": "onenetwork", "provider": "Ontario 511",
     "region": "Ontario", "refresh_seconds": 300,
     "url": "https://511on.ca/api/v2/get/cameras", "base": "https://511on.ca"},
    {"id": "ia-511", "kind": "onenetwork", "provider": "Iowa 511",
     "region": "Iowa", "refresh_seconds": 300,
     "url": "https://511ia.org/api/v2/get/cameras", "base": "https://511ia.org"},
    {"id": "ne-511", "kind": "onenetwork", "provider": "Nebraska 511",
     "region": "Nebraska", "refresh_seconds": 300,
     "url": "https://511.nebraska.gov/api/v2/get/cameras",
     "base": "https://511.nebraska.gov"},
    {"id": "tfl-jamcams", "kind": "tfl", "provider": "TfL JamCams",
     "region": "London", "refresh_seconds": 5,
     "url": "https://api.tfl.gov.uk/Place/Type/JamCam"},
]

#: Probed and rejected — kept so the next person does not re-add them.
CLOSED_SOURCES = {
    "https://511.alberta.ca/api/v2/get/cameras": "400 Invalid Key (Godseye still calls this)",
    "https://fl511.com/api/v2/get/cameras": "400 Invalid Key",
    "https://511ga.org/api/v2/get/cameras": "400 Invalid Key",
    "https://511nj.org/api/v2/get/cameras": "403",
    "https://udottraffic.utah.gov/api/v2/get/cameras": "403",
    "https://webcams.nyctmc.org/api/cameras": "no response",
}

_VIDEO_EXT = (".m3u8", ".mp4", ".ts", ".webm")
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif")


def infer_media_type(url: str = "", video_url: str = "") -> str:
    """Match Godseye's own inference: a stream beats a clip beats a still."""
    for candidate in (video_url or "", url or ""):
        low = candidate.lower().split("?")[0]
        if low.endswith(".m3u8"):
            return "stream"
        if low.endswith(_VIDEO_EXT):
            return "video"
    if (url or "").lower().split("?")[0].endswith(_IMAGE_EXT):
        return "image"
    return "image" if url else "unknown"


def _num(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if -90.0 <= abs(f) <= 180.0 or abs(f) <= 180.0 else None


def _feed(**kw) -> Dict[str, Any]:
    out = {k: kw.get(k) for k in FEED_KEYS}
    out["mediaType"] = out["mediaType"] or infer_media_type(out.get("url") or "",
                                                            out.get("videoUrl") or "")
    out["refreshSeconds"] = out["refreshSeconds"] or 300
    return out


# ── parsers ──────────────────────────────────────────────────────────────────
_CALTRANS_LINE = re.compile(r"=\s*'(.*)';\s*$")
_CALTRANS_LOC = re.compile(r"/vm/loc/([^/]+)/([^/.]+)\.htm$", re.I)


def split_catalog_payload(payload: str) -> List[str]:
    """Caltrans separates fields with a non-ASCII byte, so split on runs of
    anything outside printable ASCII — same rule Godseye's own parser uses."""
    return [p.strip() for p in re.split(r"[^\x20-\x7E]+", str(payload or "")) if p.strip()]


def parse_caltrans(text: str, source: Dict[str, Any],
                   limit: int = 5000) -> List[Dict[str, Any]]:
    """Caltrans publishes JS, not JSON: ``cctv[N] = 'page¤lon¤lat¤name¤flag';``

    The still image is derivable from the detail page URL by convention, so a
    usable feed needs no per-camera page fetch — which matters when there are
    twelve districts of them.
    """
    feeds: List[Dict[str, Any]] = []
    seen = set()
    for i, raw in enumerate(str(text or "").splitlines()):
        line = raw.strip()
        if not line.startswith("cctv["):
            continue
        m = _CALTRANS_LINE.search(line)
        if not m:
            continue
        parts = split_catalog_payload(m.group(1))
        if len(parts) < 4:
            continue
        page_url, name = parts[0], parts[3] or f"Caltrans Camera {i + 1}"
        lng, lat = _num(parts[1]), _num(parts[2])
        # Field 5 is Caltrans' own "this camera has live video" flag. Roughly
        # two thirds of them set it, and ignoring it — as the first version of
        # this parser did — throws away every live stream the agency publishes.
        stream_capable = str(parts[4] if len(parts) > 4 else "0").strip() == "1"
        if lat is None or lng is None or not page_url.startswith("https://"):
            continue
        loc = _CALTRANS_LOC.search(page_url)
        district = loc.group(1) if loc else "d0"
        slug = loc.group(2) if loc else f"cam-{i + 1}"
        key = f"{district}-{slug}"
        if key in seen:
            continue
        seen.add(key)
        still = (f"https://cwwp2.dot.ca.gov/data/{district}/cctv/image/"
                 f"{slug}/{slug}.jpg")
        feeds.append(_feed(
            id=f"caltrans-{key}", name=name, lat=lat, lng=lng,
            url=still, videoUrl=None, fallbackUrl=still,
            city=source.get("region", ""), mediaType="image",
            refreshSeconds=source.get("refresh_seconds"),
            provider=source["provider"],
            streamCapable=stream_capable,
            detailsUrl=page_url if stream_capable else None))
        if len(feeds) >= limit:
            break
    return feeds


def parse_onenetwork(payload: Any, source: Dict[str, Any],
                     limit: int = 5000) -> List[Dict[str, Any]]:
    """The shared '511' vendor API (Ontario, Iowa, Nebraska).

    A camera carries a ``Views`` array rather than a single url, and only the
    Enabled ones are worth showing — reading a top-level ``Url`` here silently
    yields zero cameras, which is exactly what the first draft of this did.
    """
    # A provider serving an HTML error page instead of JSON must yield no
    # cameras, not raise and take the whole refresh down with it.
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("features") or payload.get("Cameras") or []
    else:
        rows = []
    base = source.get("base", "").rstrip("/")
    feeds: List[Dict[str, Any]] = []
    for cam in rows:
        if not isinstance(cam, dict):
            continue
        lat, lng = _num(cam.get("Latitude")), _num(cam.get("Longitude"))
        if lat is None or lng is None:
            continue
        view = next((v for v in (cam.get("Views") or [])
                     if isinstance(v, dict) and v.get("Status") == "Enabled" and v.get("Url")),
                    None)
        if not view:
            continue
        url = view["Url"]
        if not url.startswith("http"):
            url = f"{base}{'' if url.startswith('/') else '/'}{url}"
        feeds.append(_feed(
            id=f"{source['id']}-{cam.get('Id') or len(feeds)}",
            name=cam.get("Location") or cam.get("Roadway") or "511 camera",
            lat=lat, lng=lng,
            url=url, videoUrl=url if url.lower().endswith(".m3u8") else None,
            fallbackUrl=url,
            city=cam.get("Location") or source.get("region", ""),
            mediaType=None, refreshSeconds=source.get("refresh_seconds"),
            provider=source["provider"]))
        if len(feeds) >= limit:
            break
    return feeds


def parse_tfl(payload: Any, source: Dict[str, Any],
              limit: int = 5000) -> List[Dict[str, Any]]:
    """TfL JamCams. `videoUrl` is a ~10s clip TfL refreshes periodically — it is
    NOT a live stream, which is why the UI appears to loop; see the clip-to-HLS
    work for making that continuous."""
    rows = payload if isinstance(payload, list) else []
    feeds: List[Dict[str, Any]] = []
    for cam in rows:
        if not isinstance(cam, dict):
            continue
        lat, lng = _num(cam.get("lat")), _num(cam.get("lon"))
        if lat is None or lng is None:
            continue
        meta = {p.get("key"): p.get("value")
                for p in (cam.get("additionalProperties") or [])
                if isinstance(p, dict) and p.get("key")}
        image, video = meta.get("imageUrl", ""), meta.get("videoUrl", "")
        if not (image or video):
            continue
        feeds.append(_feed(
            id=f"{source['id']}-{cam.get('id') or len(feeds)}",
            name=cam.get("commonName") or "TfL JamCam",
            lat=lat, lng=lng,
            url=image or video, videoUrl=video or None, fallbackUrl=image or None,
            city="London", mediaType=None,
            refreshSeconds=source.get("refresh_seconds"),
            provider=source["provider"]))
        if len(feeds) >= limit:
            break
    return feeds


PARSERS = {"caltrans": parse_caltrans, "onenetwork": parse_onenetwork, "tfl": parse_tfl}


def parse_source(source: Dict[str, Any], body: Any,
                 limit: int = 5000) -> List[Dict[str, Any]]:
    fn = PARSERS.get(source.get("kind", ""))
    return fn(body, source, limit) if fn else []


# ── manifest assembly ────────────────────────────────────────────────────────
_M3U8 = re.compile(r"https://[^\s\"'<>]+\.m3u8")


def extract_stream_url(html: str) -> str:
    """Pull the HLS playlist out of a camera's detail page.

    It cannot be derived: the page slug is
    ``us101eureka5thrstreetlookingnorth`` while the stream is
    ``wzmedia.dot.ca.gov/D1/eureka_5th_r_320x240.stream/playlist.m3u8``. So the
    page has to be read once per camera — which is why results are cached
    rather than re-resolved on every manifest build.
    """
    m = _M3U8.search(str(html or ""))
    return m.group(0) if m else ""


def apply_streams(feeds: List[Dict[str, Any]],
                  streams: Dict[str, str]) -> List[Dict[str, Any]]:
    """Fold resolved stream urls back into feeds, by feed id.

    A camera becomes ``mediaType: "stream"`` only once a real playlist url is
    held for it. A `streamCapable` flag on its own is a promise, not a stream;
    promoting on the flag alone fills the map with cameras that show nothing.
    """
    out = []
    for feed in feeds:
        url = (streams or {}).get(feed.get("id", ""))
        if url:
            feed = {**feed, "videoUrl": url, "mediaType": "stream"}
        out.append(feed)
    return out


def video_only(feeds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stills refreshed every few minutes are not video, however often they
    update — that distinction is the whole point of this filter."""
    return [f for f in feeds if f.get("mediaType") in VIDEO_MEDIA_TYPES]


def pending_stream_targets(feeds: List[Dict[str, Any]], streams: Dict[str, str],
                           limit: int = 250) -> List[Dict[str, str]]:
    """Which cameras still need their detail page read.

    Batched so resolution is incremental and resumable: there are thousands,
    and hammering an agency web server to fill a cache in one pass is how an IP
    gets blocked.
    """
    out = []
    for feed in feeds:
        fid = feed.get("id", "")
        if not feed.get("streamCapable") or not feed.get("detailsUrl"):
            continue
        if fid in (streams or {}):
            continue
        out.append({"id": fid, "detailsUrl": feed["detailsUrl"]})
        if len(out) >= limit:
            break
    return out


def merge_feeds(groups: Iterable[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Same dedupe key Godseye's own mergeFeeds uses, so a camera that appears
    in two providers collapses to one marker rather than stacking."""
    merged: List[Dict[str, Any]] = []
    seen = set()
    for group in groups:
        for feed in group or []:
            lat, lng = feed.get("lat"), feed.get("lng")
            if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
                continue
            key = f"{feed.get('provider', 'pub')}:{feed.get('id', '')}:{lat:.4f}:{lng:.4f}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(feed)
    return merged


def build_manifest(groups: Iterable[Iterable[Dict[str, Any]]], *, generated_at: str,
                   errors: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The document Godseye's CameraLayer parses, plus our own provenance."""
    feeds = merge_feeds(groups)
    by_provider: Dict[str, int] = {}
    for f in feeds:
        by_provider[f.get("provider") or "unknown"] = by_provider.get(f.get("provider") or "unknown", 0) + 1
    return {
        "generatedAt": generated_at,
        "feedCount": len(feeds),
        "verifiedCount": len(feeds),
        "catalogCount": 0,
        "continuousLiveCount": sum(1 for f in feeds if f.get("mediaType") == "stream"),
        # How much of the map is watchable motion vs a refreshing still, and
        # how much live video is still waiting on a detail-page read.
        "videoCount": len(video_only(feeds)),
        "streamPending": sum(1 for f in feeds
                             if f.get("streamCapable") and f.get("mediaType") != "stream"),
        "source": "vera",
        "byProvider": by_provider,
        "errors": errors or {},
        "feeds": feeds,
    }
