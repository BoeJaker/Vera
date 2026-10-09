"""Source-record shaping for the data fabric (pure: no I/O, no app import).

Three jobs the pullers in data_fabric.py hand off here:

* **Field mapping for JSON APIs.** A plain `api` source flattens each item's
  top-level scalars into text, which turns a GeoJSON feature (USGS earthquakes,
  most geo feeds) into an unreadable blob with no title, link or time. A source
  may now say where those live, with dot paths into nested JSON:

      config = {"item_path": "features", "title_field": "properties.title",
                "url_field": "properties.url", "time_field": "properties.time",
                "text_fields": ["properties.place", "properties.type"],
                "geo_field": "geometry.coordinates"}

  `map_item` then gives a record whose `data` carries the same `title`, `link`,
  `published` (and `geo`) keys an RSS record has, plus the original item.

* **Feed discovery.** `feed_links(html, base)` finds the RSS/Atom feeds a page
  advertises (`<link rel="alternate" type="application/rss+xml">`), which is
  also how a YouTube channel page names its feed. `preset_feed` builds feed URLs
  that need no page: arXiv categories, YouTube channel ids, GitHub release feeds.

* **One record shape for readers.** `record_view(data)` reads title / link /
  published / summary / geo out of any source type's record (RSS `link`, GitHub
  `html_url`, API `url`, …), so a consumer such as intel need not know the type.
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

MAPPING_KEYS = ("title_field", "url_field", "time_field", "text_fields", "geo_field", "summary_field")


# ── nested JSON ──────────────────────────────────────────────────────────────

def dot_get(obj: Any, path: str, default: Any = None) -> Any:
    """`dot_get(item, "properties.title")`; list indexes as numbers ("a.0.b"). Missing -> default."""
    if not path:
        return default
    cur = obj
    for part in str(path).split("."):
        if part == "":
            continue
        if isinstance(cur, dict):
            if part not in cur:
                return default
            cur = cur[part]
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            i = int(part)
            if not -len(cur) <= i < len(cur):
                return default
            cur = cur[i]
        else:
            return default
    return cur


def items_at(data: Any, path: str) -> List[Any]:
    """The list of items a source's `item_path` (or legacy `jq_path`) points at.
    Accepts "features", "data.items", and the jq-ish "items[*]" / ".items[]"."""
    p = re.sub(r"\[\*?\]", "", str(path or "")).strip(".").strip()
    got = dot_get(data, p, default=None) if p else data
    if got is None:
        return []
    return got if isinstance(got, list) else [got]


def has_mapping(cfg: Optional[Dict[str, Any]]) -> bool:
    return bool(cfg) and any(cfg.get(k) for k in MAPPING_KEYS)


# ── time ─────────────────────────────────────────────────────────────────────

def to_iso(value: Any) -> str:
    """Epoch seconds or milliseconds, or an ISO-ish string -> ISO 8601 UTC ('' when unreadable)."""
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"-?\d+(\.\d+)?", value.strip())):
        v = float(value)
        if v > 1e11:                                   # milliseconds (USGS, many JS APIs)
            v /= 1000.0
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (OverflowError, OSError, ValueError):
            return ""
    s = str(value).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass
    try:                                               # RFC 822 (RSS pubDate)
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, IndexError):
        return ""


def _geo(value: Any) -> Optional[Dict[str, float]]:
    """GeoJSON [lon, lat(, depth)] or {lat, lon} / {latitude, longitude} -> {lat, lon}."""
    try:
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            lon, lat = float(value[0]), float(value[1])
        elif isinstance(value, dict):
            lat = float(value.get("lat", value.get("latitude")))
            lon = float(value.get("lon", value.get("lng", value.get("longitude"))))
        else:
            return None
    except (TypeError, ValueError):
        return None
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return {"lat": lat, "lon": lon}
    return None


# ── mapping one API item ─────────────────────────────────────────────────────

def map_item(item: Any, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """One JSON item -> {"text", "data"} with readable title/link/published(/geo).
    The original item stays under data["item"], so nothing is lost."""
    if not isinstance(item, dict):
        return {"text": str(item)[:4000], "data": {"value": item}}
    title = str(dot_get(item, cfg.get("title_field") or "", "") or "").strip()
    link = str(dot_get(item, cfg.get("url_field") or "", "") or "").strip()
    published = to_iso(dot_get(item, cfg.get("time_field") or "", ""))
    fields = cfg.get("text_fields") or []
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.split(",") if f.strip()]
    parts = [str(dot_get(item, f, "") or "").strip() for f in fields]
    summary = str(dot_get(item, cfg.get("summary_field") or "", "") or "").strip()
    body = " · ".join(p for p in parts if p)
    text = ". ".join(x for x in (title, summary, body) if x)[:4000]
    if not text:                                      # nothing mapped: the old flattening
        text = " ".join(str(v) for v in item.values() if isinstance(v, (str, int, float)))[:4000]
    data: Dict[str, Any] = {"title": title, "link": link, "published": published,
                            "summary": (summary or body)[:1000], "item": item}
    geo = _geo(dot_get(item, cfg.get("geo_field") or "", None)) if cfg.get("geo_field") else None
    if geo:
        data["geo"] = geo
    return {"text": text, "data": data}


# ── reading any record ───────────────────────────────────────────────────────

_TITLE_KEYS = ("title", "name", "full_name", "headline", "subject")
_LINK_KEYS = ("link", "html_url", "url", "web_url", "href", "permalink")
_TIME_KEYS = ("published", "published_at", "created_at", "updated", "updated_at", "date", "time", "pubDate")
_SUMMARY_KEYS = ("summary", "description", "body", "extract", "message", "content")


def _first(data: Dict[str, Any], keys) -> Any:
    for k in keys:
        v = data.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def record_view(data: Optional[Dict[str, Any]], text: str = "") -> Dict[str, Any]:
    """Title / link / published / summary / geo from any fabric source record's data."""
    d = data if isinstance(data, dict) else {}
    title = str(_first(d, _TITLE_KEYS) or "").strip()
    if not title and text:
        title = text.split("\n")[0].split(". ")[0][:160]
    link = str(_first(d, _LINK_KEYS) or "").strip()
    if not link.startswith(("http://", "https://")):
        link = ""
    summary = _first(d, _SUMMARY_KEYS)
    summary = _html.unescape(re.sub(r"<[^>]+>", " ", str(summary or ""))) if summary else ""
    summary = re.sub(r"\s+", " ", summary).strip() or (text or "")[:600]
    view = {"title": title[:300], "link": link, "published": to_iso(_first(d, _TIME_KEYS)),
            "summary": summary[:1200]}
    geo = d.get("geo") if isinstance(d.get("geo"), dict) else None
    if geo and "lat" in geo and "lon" in geo:
        view["geo"] = {"lat": float(geo["lat"]), "lon": float(geo["lon"])}
    return view


# ── feeds ────────────────────────────────────────────────────────────────────

_LINK_TAG = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"""([a-zA-Z:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json", "application/json+feed")


def feed_links(page_html: str, base_url: str = "") -> List[Dict[str, str]]:
    """Feeds a page advertises: [{url, title, type}] in page order, absolute, deduplicated."""
    out, seen = [], set()
    for tag in _LINK_TAG.findall(page_html or ""):
        attrs = {k.lower(): (a or b or c) for k, a, b, c in _ATTR.findall(tag)}
        rel = attrs.get("rel", "").lower().split()
        typ = attrs.get("type", "").lower().strip()
        href = _html.unescape(attrs.get("href", "")).strip()
        if "alternate" not in rel or typ not in _FEED_TYPES or not href:
            continue
        url = urljoin(base_url, href) if base_url else href
        if url in seen or not url.startswith(("http://", "https://")):
            continue
        seen.add(url)
        out.append({"url": url, "title": _html.unescape(attrs.get("title", "")).strip(), "type": typ})
    return out


def looks_like_feed(body: str, content_type: str = "") -> bool:
    ct = (content_type or "").lower()
    if any(t in ct for t in ("rss", "atom", "feed+json")):
        return True
    head = (body or "")[:600].lstrip().lower()
    return head.startswith("<?xml") and ("<rss" in head or "<feed" in head or "<rdf" in head) \
        or head.startswith("<rss") or head.startswith("<feed")


_YT_CHANNEL = re.compile(r"(?:youtube\.com/channel/|^)(UC[\w-]{22})\b")


def preset_feed(kind: str, value: str) -> str:
    """Feed URLs that need no page fetch ('' when the value does not fit the kind)."""
    v = (value or "").strip()
    k = (kind or "").lower()
    if k == "arxiv":                                   # cs.AI, q-fin.ST, econ.GN …
        cat = v.replace("https://arxiv.org/list/", "").split("/")[0].strip()
        return f"https://rss.arxiv.org/rss/{cat}" if re.fullmatch(r"[a-z-]+(\.[A-Za-z-]+)?", cat) else ""
    if k == "youtube":
        m = _YT_CHANNEL.search(v)
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}" if m else ""
    if k == "github_releases":                         # owner/repo or a github.com URL
        m = re.search(r"(?:github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", v)
        return f"https://github.com/{m.group(1)}/{m.group(2)}/releases.atom" if m else ""
    return ""


def youtube_page(value: str) -> str:
    """A YouTube handle or channel URL to fetch for its advertised feed ('' if not YouTube)."""
    v = (value or "").strip()
    if v.startswith("@"):
        return f"https://www.youtube.com/{v}"
    host = urlparse(v if "://" in v else "https://" + v).hostname or ""
    return v if host.endswith("youtube.com") else ""
