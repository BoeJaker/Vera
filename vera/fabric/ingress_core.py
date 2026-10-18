"""The ingress gateway's pure core (shared-planning web-ingress, WI-2; PLAN §3.1).

Everything the gateway decides without I/O, in one place, so the map keys one way:

  url_key(url)            one canonical key per page - today the same URL is keyed four ways across
                          discovery._normalize_url, reader_cache.canonical, info_seek_core.canon_url and
                          fabric_records.source_url. This is the superset: lower-case scheme and host, no "www.",
                          no default port, no fragment, no tracking or secret-looking parameters, the rest sorted,
                          no trailing slash. Idempotent.
  content_hash(text)      sha256 over whitespace-normalised text: the dedupe key for bodies.
  simhash64 / hamming     near-duplicate detection; lsh_bands for the index.
  extract_links(html)     absolute, canonical links with their anchor text.
  record_policy(...)      none | meta | full - what the map may keep of a fetch (owner, 11 Oct 2026: external
                          pages in full, logged in or not; internal hosts never; installs never; private APIs
                          meta only). A caller may lower the policy, never raise it.
  reader_for(...)         the reader route from content type, magic bytes and extension; kind_of / max_bytes_for.

No I/O, no Vera imports: testable from a worktree and usable on a node worker.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

# ── URL keys ──────────────────────────────────────────────────────────────────

#: Parameters that only track the visitor (the union of the three lists in the codebase, plus a few more).
TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "mc_cid", "mc_eid", "igshid", "spm",
    "ref", "ref_src", "ref_url", "referrer", "_ga", "_gl", "_hsenc", "_hsmi", "mkt_tok", "si", "s_kwcid",
    "ocid", "cmpid", "trk", "trkid", "share_id", "feature", "source", "vero_id", "oly_enc_id", "oly_anon_id",
})
TRACKING_PREFIXES = ("utm_", "pk_", "piwik_", "matomo_", "hsa_", "ga_", "mtm_")
#: Parameters that look like credentials. They never reach the map, and url_key_detail reports them.
SECRET_PARAMS = frozenset({
    "key", "apikey", "api_key", "api-key", "token", "access_token", "auth_token", "id_token", "refresh_token",
    "sig", "signature", "sign", "auth", "authorization", "password", "passwd", "pwd", "secret", "client_secret",
    "session", "sessionid", "session_id", "sid", "jwt", "bearer", "x-amz-signature", "x-amz-credential",
    "x-amz-security-token", "sv", "se", "st", "sp", "sr", "skoid", "sktid", "code", "state", "nonce",
})
DEFAULT_PORTS = {"http": 80, "https": 443}
#: Host suffixes and names that are inside the estate: never recorded (owner Q3, 11 Oct 2026).
INTERNAL_SUFFIXES = (".int", ".local", ".lan", ".internal", ".home", ".home.arpa", ".localdomain")
INTERNAL_HOSTS = frozenset({"localhost", "llm.int", "host.docker.internal"})

_SAFE_PATH = "/:@!$&'()*+,;=-._~%"


def _norm_path(path: str) -> str:
    """One spelling of a path: percent-encoding normalised (decoded where safe, upper-case hex elsewhere),
    repeated slashes collapsed, no trailing slash except the root."""
    p = quote(unquote(path or "/"), safe=_SAFE_PATH)
    p = re.sub(r"%[0-9a-f]{2}", lambda m: m.group(0).upper(), p)
    p = re.sub(r"/{2,}", "/", p) or "/"
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/") or "/"
    return p


def _is_tracking(k: str) -> bool:
    k = k.lower()
    return k in TRACKING_PARAMS or k.startswith(TRACKING_PREFIXES)


def _is_secret(k: str) -> bool:
    return k.lower() in SECRET_PARAMS


def url_key_detail(url: str) -> Tuple[str, List[str]]:
    """(canonical key, the secret-looking parameter names that were dropped). A URL that cannot be parsed keys
    as its stripped text without a fragment, so nothing is ever lost from the map for want of a key."""
    raw = str(url or "").strip()
    if not raw:
        return "", []
    try:
        s = urlsplit(raw)
    except ValueError:
        return raw.split("#", 1)[0], []
    scheme = s.scheme.lower()
    if scheme not in ("http", "https") or not s.netloc:
        return raw.split("#", 1)[0], []
    host = (s.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    try:
        port = s.port
    except ValueError:
        port = None
    netloc = host if (not port or DEFAULT_PORTS.get(scheme) == port) else f"{host}:{port}"
    kept, dropped = [], []
    for k, v in parse_qsl(s.query, keep_blank_values=True):
        if _is_secret(k):
            dropped.append(k)
        elif not _is_tracking(k):
            kept.append((k, v))
    kept.sort()
    return urlunsplit((scheme, netloc, _norm_path(s.path), urlencode(kept, doseq=False), "")), dropped


def url_key(url: str) -> str:
    """The one key the map uses for a URL. Idempotent: url_key(url_key(u)) == url_key(u)."""
    return url_key_detail(url)[0]


def host_of(url: str) -> str:
    """The host as the map's per-host tables name it (lower case, no "www.")."""
    try:
        h = (urlsplit(str(url or "").strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def is_internal(url: str, extra_hosts: Iterable[str] = ()) -> bool:
    """A host inside the estate: private, loopback or link-local addresses, estate suffixes (.int, .local...),
    localhost, and any host the configuration names. Such fetches are never recorded."""
    h = host_of(url)
    if not h:
        return True                                # no host: nothing to record anyway
    if h in INTERNAL_HOSTS or h in {str(x).lower() for x in extra_hosts}:
        return True
    if h.endswith(INTERNAL_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(h.strip("[]"))
    except ValueError:
        return "." not in h                        # a bare single-label name is a LAN name
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified)


# ── content identity ──────────────────────────────────────────────────────────

_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9]+")


def normalise_text(text: str) -> str:
    return _WS.sub(" ", str(text or "")).strip()


def accept_encoding() -> str:
    """The Accept-Encoding to send: only what this httpx can decode. The crawl headers said 'gzip, deflate, br' while
    prod's httpx had no brotli decoder, so every server that chose br had its compressed body read as text and stored
    (18 Oct: 1,234 of 9,068 fabric_pages, 5,087 records)."""
    try:
        from httpx._decoders import SUPPORTED_DECODERS
        have = set(SUPPORTED_DECODERS)
    except Exception:  # noqa: BLE001
        have = {"gzip", "deflate"}
    return ", ".join(e for e in ("gzip", "deflate", "br", "zstd") if e in have) or "identity"


BINARY_SAMPLE = 2000
BINARY_SHARE = 0.05


def looks_binary(text: str) -> bool:
    """True when decoded text is really bytes: replacement characters or control characters (other than whitespace)
    are more than 5 % of its first 2,000 characters. Readable text in any script stays well under."""
    s = str(text or "")[:BINARY_SAMPLE]
    if not s:
        return False
    bad = sum(1 for ch in s if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\t\n\r\f\v"))
    return bad / len(s) > BINARY_SHARE


def content_hash(text: str) -> str:
    """sha256 of the whitespace-normalised text: the same words in the same order hash the same."""
    return hashlib.sha256(normalise_text(text).encode("utf-8", "replace")).hexdigest()


def bytes_hash(data: bytes) -> str:
    """sha256 of raw bytes: the blob store's key for a body."""
    return hashlib.sha256(data or b"").hexdigest()


def simhash64(text: str, shingle: int = 3) -> int:
    """A 64-bit simhash over word shingles. Two texts that differ by a few words are within a few bits."""
    words = _TOKEN.findall(str(text or "").lower())
    if not words:
        return 0
    if len(words) < shingle:
        grams = [" ".join(words)]
    else:
        grams = [" ".join(words[i:i + shingle]) for i in range(len(words) - shingle + 1)]
    acc = [0] * 64
    for g in grams:
        h = int.from_bytes(hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest(), "big")
        for b in range(64):
            acc[b] += 1 if (h >> b) & 1 else -1
    out = 0
    for b in range(64):
        if acc[b] > 0:
            out |= 1 << b
    return out


def hamming(a: int, b: int) -> int:
    return bin((int(a) ^ int(b)) & ((1 << 64) - 1)).count("1")


NEAR_DUP_BITS = 3


def near_duplicate(a: int, b: int, bits: int = NEAR_DUP_BITS) -> bool:
    return hamming(a, b) <= bits


def shingle_jaccard(a: str, b: str, shingle: int = 3) -> float:
    """Jaccard similarity of word shingles: the near-duplicate measure for SHORT texts, where a 64-bit simhash
    over a few dozen shingles is too coarse (one changed word moved the fixture's near-duplicate 20 bits)."""
    def grams(t: str):
        w = _TOKEN.findall(str(t or "").lower())
        if len(w) < shingle:
            return {" ".join(w)} if w else set()
        return {" ".join(w[i:i + shingle]) for i in range(len(w) - shingle + 1)}
    ga, gb = grams(a), grams(b)
    if not ga and not gb:
        return 1.0
    return len(ga & gb) / max(1, len(ga | gb))


SHORT_TEXT_WORDS = 400
NEAR_DUP_JACCARD = 0.8


def near_duplicate_text(a: str, b: str) -> bool:
    """Two texts that say the same thing with a few words changed: simhash within NEAR_DUP_BITS for long texts,
    shingle Jaccard >= NEAR_DUP_JACCARD when either is short (under SHORT_TEXT_WORDS words)."""
    na, nb = len(str(a or "").split()), len(str(b or "").split())
    if min(na, nb) < SHORT_TEXT_WORDS:
        return shingle_jaccard(a, b) >= NEAR_DUP_JACCARD
    return near_duplicate(simhash64(a), simhash64(b))


def lsh_bands(sim: int, bands: int = 4) -> List[int]:
    """The simhash split into `bands` equal slices (4 × 16 bits): two hashes within 3 bits share at least one
    band, so a candidate lookup is an equality query per band, not a scan."""
    width = 64 // bands
    mask = (1 << width) - 1
    return [(int(sim) >> (i * width)) & mask for i in range(bands)]


# ── links ─────────────────────────────────────────────────────────────────────

_SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "about:", "blob:", "file:")


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.base: str = ""
        self.found: List[Tuple[str, str, str]] = []       # (href, rel, text)
        self._open: Optional[List[str]] = None
        self._cur: Optional[Tuple[str, str]] = None
        self._text: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "base" and a.get("href") and not self.base:
            self.base = a["href"]
        elif tag == "a" and a.get("href"):
            self._cur = (a["href"], a.get("rel", ""))
            self._text = []
        elif tag == "link" and a.get("href") and any(r in a.get("rel", "").lower().split()
                                                     for r in ("alternate", "canonical", "next", "prev")):
            self.found.append((a["href"], a.get("rel", "").lower(), a.get("title", "")))
        elif tag in ("iframe", "frame") and a.get("src"):
            self.found.append((a["src"], tag, ""))

    def handle_data(self, data: str) -> None:
        if self._cur is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._cur is not None:
            href, rel = self._cur
            self.found.append((href, rel.lower(), normalise_text("".join(self._text))[:120]))
            self._cur = None


def extract_links(html: str, base_url: str, limit: int = 2000) -> List[Dict[str, str]]:
    """Every link a page carries, canonical and keyed, first occurrence kept:
    [{"url": the canonical key, "raw": the absolute link as written, "text": anchor text, "rel": rel}]."""
    p = _Links()
    try:
        p.feed(str(html or ""))
        p.close()
    except Exception:  # noqa: BLE001 - a broken page still yields what was parsed
        pass
    base = p.base or base_url
    out: List[Dict[str, str]] = []
    seen = set()
    for href, rel, text in p.found:
        h = href.strip()
        if not h or h.startswith("#") or h.lower().startswith(_SKIP_SCHEMES):
            continue
        try:
            absolute = urljoin(base, h)
        except ValueError:
            continue
        key = url_key(absolute)
        if not key or key in seen or not key.startswith(("http://", "https://")):
            continue
        seen.add(key)
        out.append({"url": key, "key": key, "raw": absolute.split("#", 1)[0], "text": text, "rel": rel})
        if len(out) >= limit:
            break
    return out


# ── record policy ─────────────────────────────────────────────────────────────

RECORD_LEVELS = ("none", "meta", "full")
#: Purposes that are software or model installs: never recorded (not public data).
INSTALL_PURPOSES = frozenset({"install", "pip", "apt", "model_pull", "git_clone", "provision"})
#: Purposes whose data is private to the user or an account: the egress log line only.
PRIVATE_PURPOSES = frozenset({"private", "account", "mail", "calendar", "broker", "oauth", "messaging",
                              "llm_provider", "telemetry"})


def record_policy(url: str, *, purpose: str = "", has_auth: bool = False, requested: str = "",
                  internal_hosts: Iterable[str] = ()) -> str:
    """What the map may keep of this fetch. Internal hosts and installs: none. Private purposes: meta (host,
    status, bytes, caller - never a body or a query string). Everything else, logged in or not: full (owner,
    11 Oct 2026). `requested` may lower the level and never raises it; `has_auth` is kept for the receipt but
    does not lower the level by itself."""
    if is_internal(url, internal_hosts):
        level = "none"
    elif purpose in INSTALL_PURPOSES:
        level = "none"
    elif purpose in PRIVATE_PURPOSES:
        level = "meta"
    else:
        level = "full"
    if requested in RECORD_LEVELS and RECORD_LEVELS.index(requested) < RECORD_LEVELS.index(level):
        level = requested
    return level


# ── reader routes ─────────────────────────────────────────────────────────────

#: content type (without parameters) -> reader
_CT_READERS: Dict[str, str] = {
    "text/html": "html", "application/xhtml+xml": "html", "text/plain": "text", "text/markdown": "text",
    "application/json": "json", "application/ld+json": "json", "application/geo+json": "geojson",
    "text/csv": "csv", "text/tab-separated-values": "csv", "application/csv": "csv",
    "application/rss+xml": "rss", "application/atom+xml": "atom", "application/xml": "xml", "text/xml": "xml",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.oasis.opendocument.spreadsheet": "ods", "application/msword": "binary",
    "application/vnd.ms-excel": "binary", "application/epub+zip": "epub", "application/zip": "zip",
    "application/x-tar": "tar", "application/gzip": "gzip", "application/x-gzip": "gzip",
    "application/vnd.apache.parquet": "parquet", "application/x-parquet": "parquet",
    "application/vnd.google-earth.kml+xml": "kml", "application/yaml": "yaml", "application/x-yaml": "yaml",
    "text/yaml": "yaml", "application/octet-stream": "",             # decide by bytes or extension
}
_EXT_READERS: Dict[str, str] = {
    ".html": "html", ".htm": "html", ".txt": "text", ".md": "text", ".json": "json", ".jsonld": "json",
    ".geojson": "geojson", ".csv": "csv", ".tsv": "csv", ".rss": "rss", ".atom": "atom", ".xml": "xml",
    ".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".ods": "ods", ".epub": "epub", ".zip": "zip",
    ".tar": "tar", ".gz": "gzip", ".tgz": "gzip", ".parquet": "parquet", ".kml": "kml", ".yaml": "yaml",
    ".yml": "yaml", ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
    ".svg": "image", ".wav": "audio", ".mp3": "audio", ".m4a": "audio", ".ogg": "audio", ".mp4": "video",
    ".webm": "video", ".mkv": "video", ".bin": "binary", ".exe": "binary", ".dmg": "binary", ".wasm": "binary",
}
_ZIP_BY_EXT = {".docx": "docx", ".xlsx": "xlsx", ".epub": "epub", ".ods": "ods"}


def _sniff(head: bytes) -> str:
    b = head or b""
    if b.startswith(b"%PDF"):
        return "pdf"
    if b.startswith(b"PK\x03\x04"):
        return "zip"
    if b.startswith(b"\x1f\x8b"):
        return "gzip"
    if b.startswith(b"PAR1"):
        return "parquet"
    if b.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"RIFF")) and b[8:12] == b"WEBP":
        return "image"
    if b.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")):
        return "image"
    if b.startswith(b"RIFF") and b[8:12] == b"WAVE":
        return "audio"
    if b.startswith(b"ID3") or b[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return "audio"
    if b[4:8] == b"ftyp":
        return "video"
    if b.lstrip().startswith(b"<?xml") or b.lstrip().startswith(b"<"):
        return _sniff_xml(b)
    t = b.lstrip()[:1]
    if t in (b"{", b"["):
        return "json"
    return ""


def _sniff_xml(b: bytes) -> str:
    s = b[:2048].lower()
    if b"<rss" in s:
        return "rss"
    if b"<feed" in s:
        return "atom"
    if b"<urlset" in s or b"<sitemapindex" in s:
        return "sitemap"
    if b"<kml" in s:
        return "kml"
    if b"<html" in s or b"<!doctype html" in s or b"<body" in s:
        return "html"
    return "xml" if s.lstrip().startswith(b"<?xml") else "html"


def reader_for(content_type: str, url: str = "", head: bytes = b"") -> str:
    """The reader for a response: the content type decides, the first bytes refine or override a vague type,
    and the extension breaks ties. A zip is a docx / xlsx / epub / ods by extension. 'binary' means nothing to read."""
    ct = str(content_type or "").split(";", 1)[0].strip().lower()
    path = ""
    try:
        path = (urlsplit(str(url or "")).path or "").lower()
    except ValueError:
        pass
    ext = "." + path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
    sniffed = _sniff(head)
    if ct.startswith("image/"):
        r = "image"
    elif ct.startswith("audio/"):
        r = "audio"
    elif ct.startswith("video/"):
        r = "video"
    else:
        r = _CT_READERS.get(ct, "")
    if r in ("", "xml", "zip", "text") and sniffed:
        r = sniffed if r != "text" or sniffed in ("html", "json", "rss", "atom", "sitemap") else r
    if r == "zip" and ext in _ZIP_BY_EXT:
        r = _ZIP_BY_EXT[ext]
    if r == "gzip" and path.endswith((".xml.gz",)):
        r = "sitemap"
    if not r:
        r = _EXT_READERS.get(ext, "")
    if not r and ct.startswith("text/"):
        r = "text"
    return r or "binary"


KIND_OF_READER: Dict[str, str] = {
    "html": "page", "text": "page", "json": "api", "geojson": "file", "csv": "file", "rss": "feed",
    "atom": "feed", "sitemap": "sitemap", "xml": "file", "pdf": "file", "docx": "file", "xlsx": "file",
    "ods": "file", "epub": "file", "zip": "file", "tar": "file", "gzip": "file", "parquet": "file",
    "kml": "file", "yaml": "api", "image": "media", "audio": "media", "video": "media", "binary": "file",
}


def kind_of(reader: str) -> str:
    return KIND_OF_READER.get(reader, "file")


#: Bytes the gateway reads in full per reader before switching to HEAD + a range sample (PLAN §3.6).
MAX_BYTES: Dict[str, int] = {
    "html": 2 * 1024 * 1024, "text": 2 * 1024 * 1024, "json": 10 * 1024 * 1024, "geojson": 10 * 1024 * 1024,
    "csv": 10 * 1024 * 1024, "rss": 2 * 1024 * 1024, "atom": 2 * 1024 * 1024, "sitemap": 10 * 1024 * 1024,
    "xml": 10 * 1024 * 1024, "pdf": 10 * 1024 * 1024, "docx": 10 * 1024 * 1024, "xlsx": 10 * 1024 * 1024,
    "ods": 10 * 1024 * 1024, "epub": 10 * 1024 * 1024, "zip": 10 * 1024 * 1024, "tar": 10 * 1024 * 1024,
    "gzip": 10 * 1024 * 1024, "parquet": 1024 * 1024, "kml": 10 * 1024 * 1024, "yaml": 2 * 1024 * 1024,
    "image": 0, "audio": 0, "video": 0, "binary": 0,
}
SAMPLE_BYTES = 64 * 1024
#: An archive member may inflate to at most this many times its stored size before it is refused (zip bombs).
MAX_INFLATE_RATIO = 100
MAX_ARCHIVE_DEPTH = 2


def max_bytes_for(reader: str) -> int:
    """0 means: metadata and a range sample only, never the full body, unless a caller asks for raw bytes."""
    return MAX_BYTES.get(reader, 0)


def archive_member_ok(stored: int, inflated: int, depth: int = 0) -> bool:
    if depth >= MAX_ARCHIVE_DEPTH:
        return False
    if stored <= 0:
        return inflated == 0
    return inflated / max(1, stored) <= MAX_INFLATE_RATIO and inflated <= MAX_BYTES["zip"]


# ── politeness: one shared slot per host ──────────────────────────────────────

def next_slot(now: float, stored_next: float, interval: float, jitter: float = 0.0) -> Tuple[float, float]:
    """The per-host throttle as a shared record: given the host's stored next-allowed time, (how long this caller
    waits, the new next-allowed time to store). With every process reading and writing the same record, however
    many fetchers run, one host sees at most one request per interval (PLAN §3.1; web_client's throttle was
    per process)."""
    now, nxt = float(now or 0.0), float(stored_next or 0.0)
    wait = max(0.0, nxt - now)
    new_next = max(now, nxt) + max(0.0, float(interval or 0.0)) + max(0.0, float(jitter or 0.0))
    return wait, new_next


ROBOTS_MAX_BYTES = 64 * 1024


def robots_summary(text: str) -> Dict[str, object]:
    """What the map keeps of a robots.txt (stored, not obeyed - owner, 11 Oct 2026): the crawl-delay for '*' if
    any, the sitemap URLs, and how many Disallow lines there are."""
    delay: Optional[float] = None
    sitemaps: List[str] = []
    disallows = 0
    agent_all = False
    for raw in str(text or "")[:ROBOTS_MAX_BYTES].splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if k == "user-agent":
            agent_all = (v == "*")
        elif k == "crawl-delay" and agent_all:
            try:
                delay = float(v)
            except ValueError:
                pass
        elif k == "sitemap" and v:
            sitemaps.append(v)
        elif k == "disallow" and v:
            disallows += 1
    return {"crawl_delay": delay, "sitemaps": sitemaps[:20], "disallows": disallows}
