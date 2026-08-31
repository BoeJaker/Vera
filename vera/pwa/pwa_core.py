"""
pwa_core.py — pure logic behind Vera's PWA layer
================================================

App-free on purpose: no FastAPI, no Redis, no `Vera.vera.*` imports, so the
whole thing is importable as plain `vera.pwa.pwa_core` and unit-testable
without booting the orchestrator (dev-lifecycle §6 — the *_core pattern).

What lives here:
  * the config schema + validation (`normalise_config`)
  * the web app manifest builder (`build_manifest`)
  * the service-worker cache policy — the SINGLE source of truth, serialised
    into sw.js at serve time so the JS never re-declares it (`cache_policy`)
  * the `<head>` snippet every installable page needs (`head_tags`)
  * a dependency-free PNG encoder + icon rasteriser (`encode_png`,
    `render_icon`) so app icons need neither Pillow nor a binary in the repo

The icon is drawn from signed distance fields, which gives clean antialiasing
at one sample per pixel — no supersampling pass, so a 512px icon renders in
well under a second and is then cached by the caller.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import zlib
from typing import Any, Dict, List, Mapping, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

# `display` values the spec allows, most immersive first.
DISPLAY_MODES = ("fullscreen", "standalone", "minimal-ui", "browser")

# How the service worker treats a cacheable static asset.
#   network-first          — always ask the network; cache is the offline
#                            fallback only. Keeps Vera's "UI edits are served
#                            fresh, no restart" property exactly as it is today.
#   stale-while-revalidate — serve the cached copy instantly and refresh in the
#                            background. Faster, but a UI change can be one
#                            load behind. Opt in per instance.
ASSET_STRATEGIES = ("network-first", "stale-while-revalidate")

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "name": "Vera — Orchestrator Harness",
    "short_name": "Vera",
    "description": "Vera's orchestrator harness — capabilities, agents, loops "
                   "and the estate, in one place.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "any",
    "theme_color": "#181614",
    "background_color": "#181614",
    "icon_color": "#5a9e8f",
    "asset_strategy": "network-first",
    # How long the worker waits for the network before falling back to cache.
    # Only meaningful for network-first; the LAN is fast, so this is short.
    "network_timeout_ms": 4000,
    # Bound on the navigation/page cache so an instance with hundreds of panels
    # can't grow the offline shell without limit.
    "max_page_cache_entries": 60,
    # Manifest shortcuts. Empty by default — the shell has no URL-addressable
    # tabs, so a default entry would be a broken link. Set real ones (e.g.
    # {"name": "Printer", "url": "/print/panel"}) via pwa.config.set.
    "shortcuts": [],
}

# Icon sizes we render and advertise. 180 is Apple's touch icon.
ICON_SIZES: Tuple[int, ...] = (192, 512)
APPLE_ICON_SIZE = 180
# Every size a route is allowed to render, so a URL can't ask for 20000px.
RENDERABLE_SIZES = frozenset(ICON_SIZES) | {APPLE_ICON_SIZE, 96, 144, 256, 1024}

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
# Same-origin, absolute, no scheme/host, no traversal, no protocol-relative.
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]*$")


def _clean_path(value: Any, fallback: str) -> str:
    """Accept only a same-origin absolute path; anything else is refused.

    Guards `start_url`/`scope`/shortcut URLs, which end up in a manifest the
    browser will honour — a `//evil.example` there would repoint the app.
    """
    text = str(value or "").strip()
    if not text or text.startswith("//") or ".." in text:
        return fallback
    return text if _PATH_RE.match(text) else fallback


def _clean_color(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text if _HEX_RE.match(text) else fallback


def _clean_shortcuts(value: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(value, (list, tuple)):
        return out
    for item in value[:8]:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()[:60]
        url = _clean_path(item.get("url"), "")
        if not name or not url:
            continue
        entry = {"name": name, "url": url}
        desc = str(item.get("description") or "").strip()[:200]
        if desc:
            entry["description"] = desc
        out.append(entry)
    return out


def normalise_config(patch: Mapping[str, Any] | None = None,
                     base: Mapping[str, Any] | None = None,
                     ) -> Tuple[Dict[str, Any], List[str]]:
    """Merge `patch` over `base` (default: shipped defaults), validating every
    field. Returns `(config, rejected_keys)`.

    Invalid values fall back to the base value rather than raising — a bad
    colour in Redis must never take the manifest route down — but the key is
    named in `rejected` so `pwa.config.set` can tell the caller what it
    ignored instead of silently accepting it.
    """
    cfg = dict(DEFAULT_CONFIG)
    if base:
        for key in DEFAULT_CONFIG:
            if key in base:
                cfg[key] = base[key]
    rejected: List[str] = []
    patch = dict(patch or {})

    for key, value in patch.items():
        if key not in DEFAULT_CONFIG:
            rejected.append(key)
            continue
        previous = cfg[key]
        if key == "enabled":
            cfg[key] = bool(value)
        elif key in ("name", "short_name", "description"):
            text = str(value or "").strip()[:300]
            if text:
                cfg[key] = text
            else:
                rejected.append(key)
        elif key in ("start_url", "scope"):
            cleaned = _clean_path(value, previous)
            cfg[key] = cleaned
            if cleaned != str(value or "").strip():
                rejected.append(key)
        elif key == "display":
            if value in DISPLAY_MODES:
                cfg[key] = value
            else:
                rejected.append(key)
        elif key == "orientation":
            if value in ("any", "portrait", "landscape", "natural"):
                cfg[key] = value
            else:
                rejected.append(key)
        elif key in ("theme_color", "background_color", "icon_color"):
            cleaned = _clean_color(value, previous)
            cfg[key] = cleaned
            if cleaned != str(value or "").strip():
                rejected.append(key)
        elif key == "asset_strategy":
            if value in ASSET_STRATEGIES:
                cfg[key] = value
            else:
                rejected.append(key)
        elif key == "network_timeout_ms":
            try:
                cfg[key] = max(500, min(30000, int(value)))
            except (TypeError, ValueError):
                rejected.append(key)
        elif key == "max_page_cache_entries":
            try:
                cfg[key] = max(0, min(1000, int(value)))
            except (TypeError, ValueError):
                rejected.append(key)
        elif key == "shortcuts":
            cfg[key] = _clean_shortcuts(value)

    # A scope that doesn't contain start_url makes the app uninstallable.
    if not str(cfg["start_url"]).startswith(str(cfg["scope"])):
        cfg["scope"] = "/"
    return cfg, rejected


# ─────────────────────────────────────────────────────────────────────────────
# VERSIONING — one short hash over everything a client caches
# ─────────────────────────────────────────────────────────────────────────────

def asset_version(cfg: Mapping[str, Any], *extra: str) -> str:
    """Short, deterministic version stamp.

    Anything that changes the served config or the worker source changes this,
    which (a) busts the icon/manifest query strings and (b) changes the sw.js
    bytes, which is what makes a browser install the new worker at all.
    """
    payload = json.dumps({k: cfg.get(k) for k in sorted(DEFAULT_CONFIG)},
                         sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8"))
    for item in extra:
        digest.update(b"\x00")
        digest.update((item or "").encode("utf-8", "replace"))
    return digest.hexdigest()[:12]


# ─────────────────────────────────────────────────────────────────────────────
# MANIFEST
# ─────────────────────────────────────────────────────────────────────────────

def icon_urls(version: str) -> Dict[str, str]:
    """Every icon URL, version-stamped so an updated icon actually reloads."""
    q = f"?v={version}"
    urls = {"svg": f"/ui/pwa/icon.svg{q}",
            "apple": f"/ui/pwa/apple-touch-icon.png{q}"}
    for size in ICON_SIZES:
        urls[f"png{size}"] = f"/ui/pwa/icon-{size}.png{q}"
        # Deliberately NOT /ui/pwa/icon-maskable-{n}.png: that would also match
        # the icon-{size} route's path template, and a path-parameter type
        # failure is a 422, not a fall-through to the next route.
        urls[f"maskable{size}"] = f"/ui/pwa/maskable-{size}.png{q}"
    return urls


def build_manifest(cfg: Mapping[str, Any], version: str) -> Dict[str, Any]:
    """The web app manifest served at /manifest.webmanifest."""
    urls = icon_urls(version)
    icons: List[Dict[str, Any]] = []
    for size in ICON_SIZES:
        icons.append({"src": urls[f"png{size}"], "sizes": f"{size}x{size}",
                      "type": "image/png", "purpose": "any"})
    for size in ICON_SIZES:
        icons.append({"src": urls[f"maskable{size}"], "sizes": f"{size}x{size}",
                      "type": "image/png", "purpose": "maskable"})
    # Vector last: browsers that understand it get a crisp icon at any size,
    # the rest have already matched a PNG above.
    icons.append({"src": urls["svg"], "sizes": "any",
                  "type": "image/svg+xml", "purpose": "any"})

    manifest: Dict[str, Any] = {
        "id": cfg.get("start_url") or "/",
        "name": cfg["name"],
        "short_name": cfg["short_name"],
        "description": cfg["description"],
        "start_url": cfg["start_url"],
        "scope": cfg["scope"],
        "display": cfg["display"],
        "orientation": cfg["orientation"],
        "theme_color": cfg["theme_color"],
        "background_color": cfg["background_color"],
        "categories": ["productivity", "utilities"],
        "lang": "en",
        "dir": "ltr",
        "icons": icons,
    }
    shortcuts = _clean_shortcuts(cfg.get("shortcuts"))
    if shortcuts:
        for entry in shortcuts:
            entry["icons"] = [{"src": urls["png192"], "sizes": "192x192",
                               "type": "image/png"}]
        manifest["shortcuts"] = shortcuts
    return manifest


# ─────────────────────────────────────────────────────────────────────────────
# SERVICE-WORKER CACHE POLICY
# ─────────────────────────────────────────────────────────────────────────────

# Requests the worker must never touch, whatever else matches. Vera is mostly
# live data — capability calls, event streams, loop output — and a cached
# answer to any of them is worse than no answer. Deny beats allow.
BYPASS_PREFIXES: Tuple[str, ...] = (
    "/mcp/", "/events", "/event", "/stream", "/sse", "/ws", "/api/",
    "/workshop/", "/evolve/", "/dream/", "/agent", "/chat/", "/loops/",
    "/health", "/metrics", "/docs", "/openapi.json", "/vscode/", "/godseye/",
)

# Static UI assets that are safe to keep a copy of. Everything not matched
# here is left entirely to the browser — the worker does not call
# respondWith at all, so it cannot change the behaviour of a live call.
# NB: these must all be FILES. /ui/loader and /ui/themes (no extension) are
# live capability endpoints that happen to sit under /ui — they are
# deliberately absent, and so fall through to "bypass" like any other API.
ASSET_PREFIXES: Tuple[str, ...] = ("/ui/elements/", "/ui/pwa/", "/ui/vera-",
                                   "/ui/themes.css")
ASSET_EXTENSIONS: Tuple[str, ...] = (".js", ".css", ".png", ".svg", ".webp",
                                     ".ico", ".woff", ".woff2", ".ttf")

OFFLINE_URL = "/pwa/offline"


def precache_urls(version: str) -> List[str]:
    """The minimum that must be present for the offline shell to render."""
    urls = icon_urls(version)
    return [OFFLINE_URL, "/manifest.webmanifest",
            urls["svg"], urls["png192"], urls["png512"]]


def cache_policy(cfg: Mapping[str, Any], version: str) -> Dict[str, Any]:
    """The policy blob substituted into sw.js — one source of truth for both
    sides, so the worker and the tests can never disagree about it."""
    # NB: no "enabled" key — whether the PWA is on is decided by the route,
    # which serves a self-uninstalling worker instead of this one. Every key
    # here must be read by sw.js (tests/test_pwa_core.py enforces that).
    return {
        "version": version,
        "assetCache": f"vera-pwa-assets-{version}",
        "pageCache": f"vera-pwa-pages-{version}",
        "cachePrefix": "vera-pwa-",
        "assetStrategy": cfg.get("asset_strategy", "network-first"),
        "networkTimeoutMs": int(cfg.get("network_timeout_ms", 4000)),
        "maxPageCacheEntries": int(cfg.get("max_page_cache_entries", 60)),
        "offlineUrl": OFFLINE_URL,
        "precache": precache_urls(version),
        "bypassPrefixes": list(BYPASS_PREFIXES),
        "assetPrefixes": list(ASSET_PREFIXES),
        "assetExtensions": list(ASSET_EXTENSIONS),
    }


def classify_request(path: str, method: str = "GET", mode: str = "") -> str:
    """Mirror of the worker's routing decision, as a pure function.

    Returns one of:
      "bypass"  — worker must not intercept (browser default behaviour)
      "asset"   — cacheable static asset
      "page"    — navigation: network-first with an offline fallback

    The worker implements exactly this using the same policy lists; keeping a
    Python twin means the routing rules are testable without a browser.
    """
    if (method or "GET").upper() != "GET":
        return "bypass"
    path = path or "/"
    for prefix in BYPASS_PREFIXES:
        if path == prefix or path.startswith(prefix):
            return "bypass"
    if mode == "navigate":
        return "page"
    base = path.split("?", 1)[0]
    if base.startswith(ASSET_PREFIXES):
        return "asset"
    if base == "/manifest.webmanifest":
        return "asset"
    if base.endswith(ASSET_EXTENSIONS):
        return "asset"
    return "bypass"


# ─────────────────────────────────────────────────────────────────────────────
# HEAD TAGS
# ─────────────────────────────────────────────────────────────────────────────

def head_tags(cfg: Mapping[str, Any] | None = None) -> str:
    """The tags a top-level page needs to be installable.

    These ship statically in capability_orchestration.html (see
    tests/test_pwa_core.py, which asserts the shell still carries them);
    pwa.js re-syncs theme-color to whatever theme is actually painted.
    """
    cfg = cfg or DEFAULT_CONFIG
    return (
        '<meta name="viewport" content="width=device-width, initial-scale=1, '
        'viewport-fit=cover">\n'
        '<link rel="manifest" href="/manifest.webmanifest">\n'
        f'<meta name="theme-color" content="{cfg["theme_color"]}">\n'
        '<link rel="icon" href="/ui/pwa/icon.svg" type="image/svg+xml">\n'
        '<link rel="apple-touch-icon" href="/ui/pwa/apple-touch-icon.png">\n'
        '<meta name="mobile-web-app-capable" content="yes">\n'
        '<meta name="apple-mobile-web-app-capable" content="yes">\n'
        # "black", not "black-translucent": translucent draws page content
        # underneath the iOS status bar, and this shell has no safe-area
        # padding to compensate, so the top row would be hidden.
        '<meta name="apple-mobile-web-app-status-bar-style" '
        'content="black">\n'
        f'<meta name="apple-mobile-web-app-title" content="{cfg["short_name"]}">\n'
        '<script src="/ui/pwa/pwa.js" defer></script>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# ICONS — dependency-free PNG
# ─────────────────────────────────────────────────────────────────────────────

def encode_png(width: int, height: int, rows: List[bytes]) -> bytes:
    """Minimal 8-bit RGBA PNG encoder (filter type 0 on every scanline).

    Pillow would do this too, but it isn't a hard dependency of the
    orchestrator and an app icon must never be the reason a page 500s.
    """
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    if len(rows) != height:
        raise ValueError(f"expected {height} rows, got {len(rows)}")
    stride = width * 4

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    raw = bytearray()
    for row in rows:
        if len(row) != stride:
            raise ValueError(f"row length {len(row)} != {stride}")
        raw.append(0)          # filter: None
        raw += row
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def hex_to_rgb(value: str) -> Tuple[int, int, int]:
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(f"not a hex colour: {value!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


# The mark, in a unit box centred on the origin: a "V" of two round-capped
# strokes with a node at each terminal and at the vertex — the letter and the
# capability graph at the same time.
_GLYPH_A = (-0.42, -0.34)
_GLYPH_B = (0.00, 0.40)
_GLYPH_C = (0.42, -0.34)
_STROKE_R = 0.115
_NODE_R = 0.163
# Fraction of the tile the mark occupies. Maskable icons must sit inside the
# platform's 80%-diameter safe circle, so their mark is drawn smaller.
_GLYPH_SCALE = 0.62
_GLYPH_SCALE_MASKABLE = 0.50
# Rounded-square radius as a fraction of the tile; maskable is full-bleed
# because the platform applies its own mask.
_CORNER_RATIO = 0.2222


def _sd_segment(px: float, py: float, ax: float, ay: float,
                bx: float, by: float, r: float) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    vv = vx * vx + vy * vy
    t = 0.0 if vv <= 0.0 else max(0.0, min(1.0, (wx * vx + wy * vy) / vv))
    dx, dy = wx - t * vx, wy - t * vy
    return math.sqrt(dx * dx + dy * dy) - r


def render_icon(size: int, *, background: str = None, color: str = None,
                maskable: bool = False) -> bytes:
    """Rasterise the app icon at `size`x`size` and return PNG bytes.

    Antialiased from signed distance fields: coverage is `0.5 - distance`
    clamped to [0,1], which is a good approximation of exact pixel coverage
    for these shapes and costs one sample per pixel instead of a
    supersampling pass.
    """
    size = int(size)
    if size <= 0 or size > 2048:
        raise ValueError("size out of range")
    bg_r, bg_g, bg_b = hex_to_rgb(background or DEFAULT_CONFIG["background_color"])
    fg_r, fg_g, fg_b = hex_to_rgb(color or DEFAULT_CONFIG["icon_color"])

    scale = _GLYPH_SCALE_MASKABLE if maskable else _GLYPH_SCALE
    corner = 0.0 if maskable else size * _CORNER_RATIO
    half = size / 2.0
    g = size * scale

    ax, ay = half + _GLYPH_A[0] * g, half + _GLYPH_A[1] * g
    bx, by = half + _GLYPH_B[0] * g, half + _GLYPH_B[1] * g
    cx, cy = half + _GLYPH_C[0] * g, half + _GLYPH_C[1] * g
    stroke = _STROKE_R * g
    node = _NODE_R * g

    # Bounding box of the mark (plus a pixel of AA slack) so the vast majority
    # of background pixels skip the distance evaluations entirely.
    pad = node + 1.5
    gx0, gx1 = min(ax, bx, cx) - pad, max(ax, bx, cx) + pad
    gy0, gy1 = min(ay, by, cy) - pad, max(ay, by, cy) + pad

    inner = size - corner          # corner-square boundary on the far side
    rows: List[bytes] = []
    for y in range(size):
        py = y + 0.5
        row = bytearray(size * 4)
        in_glyph_band = gy0 <= py <= gy1
        # A row only needs the rounded-rect test where it passes through a
        # corner square; elsewhere the tile is solid across its full width.
        corner_row = corner > 0.0 and (py < corner or py > inner)
        for x in range(size):
            px = x + 0.5

            alpha = 1.0
            if corner_row and (px < corner or px > inner):
                qx = abs(px - half) - (half - corner)
                qy = abs(py - half) - (half - corner)
                mx, my = max(qx, 0.0), max(qy, 0.0)
                d = (math.sqrt(mx * mx + my * my)
                     + min(max(qx, qy), 0.0) - corner)
                alpha = min(max(0.5 - d, 0.0), 1.0)
                if alpha <= 0.0:
                    continue          # fully transparent: leave the row zeroed

            cov = 0.0
            if in_glyph_band and gx0 <= px <= gx1:
                d = _sd_segment(px, py, ax, ay, bx, by, stroke)
                d2 = _sd_segment(px, py, bx, by, cx, cy, stroke)
                if d2 < d:
                    d = d2
                for nx, ny in ((ax, ay), (bx, by), (cx, cy)):
                    ddx, ddy = px - nx, py - ny
                    dn = math.sqrt(ddx * ddx + ddy * ddy) - node
                    if dn < d:
                        d = dn
                cov = min(max(0.5 - d, 0.0), 1.0)
                if cov > alpha:
                    cov = alpha       # never let the mark spill past the tile

            i = x * 4
            if cov <= 0.0:
                row[i] = bg_r
                row[i + 1] = bg_g
                row[i + 2] = bg_b
            else:
                inv = 1.0 - cov
                row[i] = int(bg_r * inv + fg_r * cov + 0.5)
                row[i + 1] = int(bg_g * inv + fg_g * cov + 0.5)
                row[i + 2] = int(bg_b * inv + fg_b * cov + 0.5)
            row[i + 3] = int(alpha * 255.0 + 0.5)
        rows.append(bytes(row))
    return encode_png(size, size, rows)


def render_icon_svg(*, background: str = None, color: str = None,
                    maskable: bool = False) -> str:
    """The same mark as SVG — crisp at any size, and what the favicon uses."""
    bg = _clean_color(background, DEFAULT_CONFIG["background_color"])
    fg = _clean_color(color, DEFAULT_CONFIG["icon_color"])
    size = 512.0
    scale = _GLYPH_SCALE_MASKABLE if maskable else _GLYPH_SCALE
    corner = 0.0 if maskable else size * _CORNER_RATIO
    half = size / 2.0
    g = size * scale

    def pt(p):
        return half + p[0] * g, half + p[1] * g

    ax, ay = pt(_GLYPH_A)
    bx, by = pt(_GLYPH_B)
    cx, cy = pt(_GLYPH_C)
    stroke = 2.0 * _STROKE_R * g          # SVG stroke-width is the full width
    node = _NODE_R * g
    circles = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{node:.1f}" fill="{fg}"/>'
        for x, y in ((ax, ay), (bx, by), (cx, cy)))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" '
        f'width="512" height="512" role="img" aria-label="Vera">'
        f'<rect width="512" height="512" rx="{corner:.1f}" ry="{corner:.1f}" '
        f'fill="{bg}"/>'
        f'<path d="M {ax:.1f} {ay:.1f} L {bx:.1f} {by:.1f} L {cx:.1f} {cy:.1f}" '
        f'fill="none" stroke="{fg}" stroke-width="{stroke:.1f}" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        f'{circles}</svg>'
    )
