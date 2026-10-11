"""A topic's sources beyond web search: subscriptions to DATA-FABRIC sources
(pure: no I/O, no app import).

Intel does not fetch feeds. A topic subscribes to fabric sources - the fabric
pulls them on its own schedule into its own datasets, with its dedup, embedding
and entity extraction - and the sweep reads what is new there and sends it
through the same triage as web results. One fabric source can serve several
topics: its id is derived from what it reads, so subscribing twice reuses it.

Kinds a person can add (the panel's form is built from KINDS):

  feed             any site, page or feed URL - its RSS/Atom feed is discovered
  arxiv            an arXiv category (cs.AI, q-fin.ST, econ.GN …)
  youtube          a channel URL, channel id or @handle
  github_releases  owner/repo - its releases feed
  usgs             USGS earthquakes: magnitude (significant | 4.5 | 2.5 | 1.0 | all),
                   optionally inside a bounding box
  api              any JSON API, with dot-path field mapping (see the fabric's
                   source_map_core)
  telegram         a PUBLIC Telegram channel (its t.me/s/ web preview; no account)
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:                       # relative first: the sibling package of whichever tree loaded us (Vera.vera or vera)
    from ..fabric import source_map_core as srcmap
except ImportError:  # loaded outside a package
    try:
        from Vera.vera.fabric import source_map_core as srcmap
    except ImportError:
        from vera.fabric import source_map_core as srcmap

KINDS: Dict[str, Dict[str, Any]] = {
    "feed": {"label": "Website or feed", "value": "Page, site or RSS/Atom URL", "signal_kind": "page",
             "interval_h": 6, "discover": True},
    "arxiv": {"label": "arXiv category", "value": "Category, e.g. cs.AI or q-fin.ST", "signal_kind": "paper",
              "interval_h": 12},
    "youtube": {"label": "YouTube channel", "value": "Channel URL, channel id or @handle", "signal_kind": "video",
                "interval_h": 12, "discover": True},
    "github_releases": {"label": "GitHub releases", "value": "owner/repo", "signal_kind": "repo", "interval_h": 12},
    "usgs": {"label": "Earthquakes (USGS)", "value": "Magnitude: significant, 4.5, 2.5, 1.0 or all",
             "signal_kind": "page", "interval_h": 1, "geo": True},
    "api": {"label": "JSON API", "value": "URL returning JSON", "signal_kind": "page", "interval_h": 6,
            "mapping": True},
    "telegram": {"label": "Telegram channel", "value": "Public channel: @name or t.me/name", "signal_kind": "note",
                 "interval_h": 3},
    # an endpoint of a Platforms API connection: the connection holds the address and the key (data-connections)
    "connection": {"label": "API connection", "value": "Connection and path, e.g. guardian /search?q=house+prices",
                   "signal_kind": "page", "interval_h": 6, "mapping": True},
}
USGS_LEVELS = ("significant", "4.5", "2.5", "1.0", "all")
USGS_MAPPING = {"item_path": "features", "title_field": "properties.title", "url_field": "properties.url",
                "time_field": "properties.time", "text_fields": "properties.place,properties.type",
                "geo_field": "geometry.coordinates"}
MAX_AGE_DAYS = 14          # older items in a feed are history, not news
PER_SOURCE = 30            # records read per source per sweep


def source_id(kind: str, value: str) -> str:
    """Stable fabric source id for what a source reads, shared by every topic that adds it."""
    key = f"{kind}|{(value or '').strip().lower().rstrip('/')}"
    return "intel-src-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def parse_bbox(text: Any) -> Optional[Tuple[float, float, float, float]]:
    """'min_lon,min_lat,max_lon,max_lat' -> tuple, or None (also None when malformed or out of range)."""
    if not text:
        return None
    if isinstance(text, (list, tuple)):
        parts = list(text)
    else:
        parts = re.split(r"[,\s]+", str(text).strip())
    try:
        a, b, c, d = (float(x) for x in parts[:4])
    except (TypeError, ValueError):
        return None
    if len(parts) != 4 or not (-180 <= a < c <= 180 and -90 <= b < d <= 90):
        return None
    return a, b, c, d


def in_bbox(geo: Optional[Dict[str, float]], bbox: Optional[Tuple[float, float, float, float]]) -> bool:
    if not bbox:
        return True
    if not geo:
        return False
    return bbox[0] <= geo["lon"] <= bbox[2] and bbox[1] <= geo["lat"] <= bbox[3]


def plan_source(kind: str, value: str, *, label: str = "", interval_h: float = 0) -> Dict[str, Any]:
    """What to register with fabric.sources.add for a kind + value.
    {"error"} when the value does not fit; {"discover": url} when the feed must be
    found first (feed, YouTube handle); else the fabric.sources.add arguments."""
    kind = (kind or "").strip().lower()
    value = (value or "").strip()
    spec = KINDS.get(kind)
    if not spec:
        return {"error": f"unknown kind {kind!r} - one of {', '.join(KINDS)}"}
    if not value:
        return {"error": f"{spec['value']} is required"}
    interval = int(float(interval_h or spec["interval_h"]) * 3600)
    sid = source_id(kind, value)
    base = {"id": sid, "label": (label or f"{spec['label']}: {value}")[:80], "dataset_id": f"intel_src_{sid[10:]}",
            "interval": max(900, interval), "tags": f"intel,intel-source,{kind}"}
    if kind == "usgs":
        level = value.lower().replace("m", "").strip()
        if level not in USGS_LEVELS:
            return {"error": "magnitude must be one of " + ", ".join(USGS_LEVELS)}
        url = f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{level}_day.geojson"
        return {**base, "url": url, "source_type": "api", "config": dict(USGS_MAPPING)}
    if kind == "telegram":
        name = srcmap.telegram_channel(value)
        if not name:
            return {"error": "a public channel: @name, t.me/name or https://t.me/s/name"}
        return {**base, "url": f"https://t.me/s/{name}", "source_type": "telegram", "config": {"channel": name}}
    if kind == "connection":
        conn, _, path = value.partition(" ")
        conn, path = conn.strip().lower(), path.strip()
        if not re.fullmatch(r"[a-z0-9_]{1,64}", conn):
            return {"error": "start with the connection's id (Platforms > API connections), then the path"}
        if path.lower().startswith(("http://", "https://")):
            return {"error": "give the path under the connection, not a full URL"}
        return {**base, "url": "", "source_type": "api", "connection": conn, "path": path}
    if kind == "api":
        if not value.startswith(("http://", "https://")):
            return {"error": "the API URL must start with http:// or https://"}
        return {**base, "url": value, "source_type": "api"}
    preset = srcmap.preset_feed(kind, value) if kind in ("arxiv", "youtube", "github_releases") else ""
    if preset:
        return {**base, "url": preset, "source_type": "rss"}
    if kind in ("arxiv", "github_releases"):
        return {"error": f"{value!r} is not {spec['value'].lower()}"}
    return {**base, "discover": value}                       # feed, or a YouTube handle / channel page


def record_hit(record: Dict[str, Any], *, kind: str, bbox=None) -> Optional[Dict[str, Any]]:
    """A fabric record -> a search-hit-shaped dict for core.normalise_signal (None: outside the bbox)."""
    data = record.get("data")
    if isinstance(data, str):
        try:
            import json
            data = json.loads(data)
        except ValueError:
            data = {}
    view = srcmap.record_view(data if isinstance(data, dict) else {}, str(record.get("text") or ""))
    if bbox is not None and not in_bbox(view.get("geo"), bbox):
        return None
    hit = {"url": view["link"], "title": view["title"], "snippet": view["summary"],
           "published": view["published"] or str(record.get("created_at") or ""),
           "kind": KINDS.get(kind, {}).get("signal_kind", "page")}
    if view.get("geo"):
        hit["places"] = [{"name": view["title"][:80], "lat": view["geo"]["lat"], "lon": view["geo"]["lon"],
                          "as": "source"}]
    return hit


def fresh(published: str, now: Optional[datetime] = None, max_age_days: int = MAX_AGE_DAYS) -> bool:
    """True unless the item is dated and older than the window (undated items are kept)."""
    iso = srcmap.to_iso(published)
    if not iso:
        return True
    when = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return when >= (now or datetime.now(timezone.utc)) - timedelta(days=max_age_days)


def stale(last_pulled: str, interval_s: int, now: Optional[datetime] = None) -> bool:
    """Should the sweep ask the fabric to pull first? Never pulled, or 1.5 intervals ago."""
    iso = srcmap.to_iso(last_pulled)
    if not iso:
        return True
    when = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - when > timedelta(seconds=1.5 * max(900, int(interval_s or 0)))


def subscription(plan: Dict[str, Any], kind: str, value: str, *, bbox=None, now_iso: str = "") -> Dict[str, Any]:
    """What a topic keeps about one source it reads."""
    sub = {"id": plan["id"], "kind": kind, "value": value, "label": plan["label"], "dataset_id": plan["dataset_id"],
           "url": plan.get("url", ""), "interval": plan.get("interval", 0), "added": now_iso}
    if bbox:
        sub["bbox"] = list(bbox)
    return sub


def users_of(topics: List[Dict[str, Any]], sid: str, *, except_topic: str = "") -> List[str]:
    """Topic ids that still read source `sid`."""
    return [t["id"] for t in topics if t.get("id") != except_topic
            and any(s.get("id") == sid for s in (t.get("sources") or []))]
