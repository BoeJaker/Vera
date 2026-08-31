"""Tests for the PWA layer's pure core (vera/pwa/pwa_core.py).

Imported as lowercase `vera.pwa.pwa_core` with the repo root pushed onto
sys.path — NOT `Vera.vera...`, which resolves to whatever checkout happens to
be first on the path and would silently test the wrong tree (dev-lifecycle §6,
the namespace-package import trap).
"""
import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vera.pwa import pwa_core as core  # noqa: E402


# ── config validation ───────────────────────────────────────────────────────

def test_defaults_are_self_consistent():
    cfg, rejected = core.normalise_config()
    assert rejected == []
    assert cfg["display"] in core.DISPLAY_MODES
    assert cfg["asset_strategy"] in core.ASSET_STRATEGIES
    assert cfg["start_url"].startswith(cfg["scope"])


def test_patch_merges_over_base_without_touching_other_keys():
    cfg, rejected = core.normalise_config({"short_name": "Vera Dev"})
    assert rejected == []
    assert cfg["short_name"] == "Vera Dev"
    assert cfg["name"] == core.DEFAULT_CONFIG["name"]


@pytest.mark.parametrize("bad_url", [
    "https://evil.example/",       # absolute cross-origin
    "//evil.example/",             # protocol-relative
    "../../etc/passwd",            # traversal
    "javascript:alert(1)",         # scheme injection
    "not-a-path",
])
def test_start_url_cannot_leave_this_origin(bad_url):
    """A manifest start_url is honoured by the browser — letting a stored
    config repoint it at another origin would hand the installed app away."""
    cfg, rejected = core.normalise_config({"start_url": bad_url})
    assert cfg["start_url"] == core.DEFAULT_CONFIG["start_url"]
    assert "start_url" in rejected


def test_shortcut_urls_are_filtered_the_same_way():
    cfg, _ = core.normalise_config({"shortcuts": [
        {"name": "Good", "url": "/print/panel"},
        {"name": "Bad", "url": "https://evil.example/x"},
        {"name": "", "url": "/nameless"},
        "not-a-mapping",
    ]})
    assert cfg["shortcuts"] == [{"name": "Good", "url": "/print/panel"}]


@pytest.mark.parametrize("bad", ["red", "#12", "#1234567", "", "rgb(1,2,3)"])
def test_bad_colour_is_rejected_and_falls_back(bad):
    cfg, rejected = core.normalise_config({"theme_color": bad})
    assert cfg["theme_color"] == core.DEFAULT_CONFIG["theme_color"]
    assert "theme_color" in rejected


def test_good_colours_accepted_in_both_lengths():
    cfg, rejected = core.normalise_config({"theme_color": "#abc",
                                           "icon_color": "#A1B2C3"})
    assert rejected == []
    assert cfg["theme_color"] == "#abc"
    assert cfg["icon_color"] == "#A1B2C3"


def test_unknown_and_invalid_enum_keys_are_reported_not_applied():
    cfg, rejected = core.normalise_config({"display": "hologram",
                                           "asset_strategy": "cache-only",
                                           "wat": 1})
    assert cfg["display"] == core.DEFAULT_CONFIG["display"]
    assert cfg["asset_strategy"] == core.DEFAULT_CONFIG["asset_strategy"]
    assert set(rejected) == {"display", "asset_strategy", "wat"}


def test_numeric_fields_are_clamped_not_trusted():
    cfg, _ = core.normalise_config({"network_timeout_ms": 10 ** 9,
                                    "max_page_cache_entries": -5})
    assert cfg["network_timeout_ms"] == 30000
    assert cfg["max_page_cache_entries"] == 0


def test_scope_is_widened_when_it_would_exclude_start_url():
    """A start_url outside scope makes the app uninstallable — the config
    must repair that rather than emit a manifest no browser will accept."""
    cfg, _ = core.normalise_config({"scope": "/nested/", "start_url": "/"})
    assert cfg["scope"] == "/"
    assert cfg["start_url"].startswith(cfg["scope"])


def test_stored_config_is_revalidated_not_trusted():
    """The Redis read path re-runs the stored blob through normalise_config as
    a PATCH, so a hand-edited key gets the same validation as an API call."""
    stored = {"start_url": "//evil.example", "theme_color": "nonsense",
              "display": "standalone", "short_name": "Vera"}
    cfg, rejected = core.normalise_config(stored)
    assert cfg["start_url"] == core.DEFAULT_CONFIG["start_url"]
    assert cfg["theme_color"] == core.DEFAULT_CONFIG["theme_color"]
    assert {"start_url", "theme_color"} <= set(rejected)
    assert cfg["short_name"] == "Vera"          # the valid parts still apply


# ── versioning ──────────────────────────────────────────────────────────────

def test_version_changes_with_config_and_with_sources():
    cfg, _ = core.normalise_config()
    other, _ = core.normalise_config({"short_name": "V"})
    assert core.asset_version(cfg) == core.asset_version(cfg)          # stable
    assert core.asset_version(cfg) != core.asset_version(other)        # config
    assert core.asset_version(cfg, "sw-a") != core.asset_version(cfg, "sw-b")


# ── manifest ────────────────────────────────────────────────────────────────

def test_manifest_meets_the_installability_checklist():
    cfg, _ = core.normalise_config()
    m = core.build_manifest(cfg, "abc123")
    for field in ("name", "short_name", "start_url", "scope", "display",
                  "icons", "theme_color", "background_color"):
        assert m.get(field), f"manifest missing {field}"
    assert m["display"] in core.DISPLAY_MODES
    sizes = {i["sizes"] for i in m["icons"] if i["type"] == "image/png"}
    # Chrome requires a 192 and a 512 PNG before it will offer to install.
    assert "192x192" in sizes and "512x512" in sizes
    assert any(i.get("purpose") == "maskable" for i in m["icons"])
    assert m["start_url"].startswith(m["scope"])
    assert json.loads(json.dumps(m)) == m       # serialisable as-is


def test_every_icon_url_is_version_stamped():
    cfg, _ = core.normalise_config()
    m = core.build_manifest(cfg, "deadbeef")
    for icon in m["icons"]:
        assert "v=deadbeef" in icon["src"], icon["src"]


def test_maskable_icon_url_cannot_be_captured_by_the_plain_icon_route():
    """/ui/pwa/icon-{size}.png would match `icon-maskable-192.png` too, and a
    path-parameter type failure is a 422 — not a fall-through to the next
    route. The maskable URLs must therefore not start with `icon-`."""
    urls = core.icon_urls("v1")
    for size in core.ICON_SIZES:
        assert not urls[f"maskable{size}"].startswith("/ui/pwa/icon-")


def test_shortcuts_omitted_when_empty():
    cfg, _ = core.normalise_config()
    assert "shortcuts" not in core.build_manifest(cfg, "v")
    cfg2, _ = core.normalise_config({"shortcuts": [{"name": "P", "url": "/print/panel"}]})
    assert core.build_manifest(cfg2, "v")["shortcuts"][0]["url"] == "/print/panel"


# ── service-worker routing policy ───────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/mcp/call", "/mcp/tools", "/events", "/health", "/workshop/agent_loop/sessions",
    "/evolve/pipeline/list", "/api/thing", "/chat/send", "/dream/state",
    "/openapi.json", "/vscode/abc/", "/metrics",
])
def test_live_endpoints_are_never_intercepted(path):
    """The whole safety argument for shipping a worker on a live dashboard:
    capability calls and streams must behave exactly as with no worker."""
    assert core.classify_request(path) == "bypass"


def test_non_get_is_never_intercepted():
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert core.classify_request("/ui/elements/x.js", method) == "bypass"


@pytest.mark.parametrize("path", [
    "/ui/elements/activity_timeline.js",
    "/ui/vera-ui.js",
    "/ui/vera-panel.css",
    "/ui/themes.css",
    "/ui/pwa/icon-192.png",
    "/manifest.webmanifest",
    "/some/panel/style.css",
])
def test_static_assets_are_cacheable(path):
    assert core.classify_request(path) == "asset"


def test_bypass_beats_the_asset_allowlist():
    """A .js under a bypassed prefix must stay bypassed — deny wins."""
    assert core.classify_request("/mcp/thing.js") == "bypass"
    assert core.classify_request("/chat/widget.js") == "bypass"
    assert core.classify_request("/evolve/x.css") == "bypass"


def test_navigations_are_pages():
    assert core.classify_request("/", mode="navigate") == "page"
    assert core.classify_request("/ui/panels/agents-panel", mode="navigate") == "page"
    # ...but a bypassed path stays bypassed even as a navigation.
    assert core.classify_request("/docs", mode="navigate") == "bypass"


def test_unknown_dynamic_get_is_left_alone():
    """Anything not on the allowlist is not intercepted at all, so a cap that
    grows a new HTTP route tomorrow cannot start being served from cache."""
    assert core.classify_request("/some/new/cap/route") == "bypass"


# ── the policy blob the worker consumes ─────────────────────────────────────

def test_cache_policy_is_json_serialisable_and_complete():
    cfg, _ = core.normalise_config()
    policy = core.cache_policy(cfg, "v1")
    assert json.loads(json.dumps(policy)) == policy
    for key in ("version", "assetCache", "pageCache", "cachePrefix",
                "assetStrategy", "networkTimeoutMs", "maxPageCacheEntries",
                "offlineUrl", "precache", "bypassPrefixes", "assetPrefixes",
                "assetExtensions"):
        assert key in policy, f"policy missing {key}"
    assert policy["assetCache"].startswith(policy["cachePrefix"])
    assert policy["pageCache"].startswith(policy["cachePrefix"])
    # A new version must produce new cache names, or activate() would keep
    # serving the previous version's entries.
    assert core.cache_policy(cfg, "v2")["assetCache"] != policy["assetCache"]


def test_policy_blob_carries_no_free_text_from_the_config():
    """The blob is pasted straight into JavaScript source, so only
    enum-validated and numeric fields may reach it. Operator-supplied free
    text must not - that is what would let a stored name break the parse."""
    cfg, _ = core.normalise_config({"name": "Vera </script> test",
                                    "description": "'; evil() //"})
    blob = json.dumps(core.cache_policy(cfg, "v1"), separators=(",", ":"))
    assert "</" not in blob
    assert "evil" not in blob and "Vera <" not in blob


def test_offline_shell_is_precached():
    cfg, _ = core.normalise_config()
    policy = core.cache_policy(cfg, "v1")
    assert policy["offlineUrl"] in policy["precache"]
    assert core.classify_request(policy["offlineUrl"], mode="navigate") == "page"


# ── PNG encoder ─────────────────────────────────────────────────────────────

def _png_chunks(blob):
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    pos, out = 8, []
    while pos < len(blob):
        (length,) = struct.unpack(">I", blob[pos:pos + 4])
        tag = blob[pos + 4:pos + 8]
        data = blob[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", blob[pos + 8 + length:pos + 12 + length])
        assert crc == zlib.crc32(tag + data) & 0xFFFFFFFF, f"bad CRC on {tag}"
        out.append((tag, data))
        pos += 12 + length
    return out


def test_encode_png_is_a_valid_png():
    rows = [bytes([255, 0, 0, 255] * 4) for _ in range(3)]
    blob = core.encode_png(4, 3, rows)
    chunks = _png_chunks(blob)
    assert [t for t, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    width, height, depth, colour = struct.unpack(">IIBB", chunks[0][1][:10])
    assert (width, height, depth, colour) == (4, 3, 8, 6)   # 8-bit RGBA
    raw = zlib.decompress(chunks[1][1])
    assert len(raw) == 3 * (1 + 4 * 4)                      # filter byte per row
    assert raw[0] == 0 and raw[1:5] == bytes([255, 0, 0, 255])


def test_encode_png_refuses_malformed_input():
    with pytest.raises(ValueError):
        core.encode_png(2, 2, [b"\x00" * 8])                # too few rows
    with pytest.raises(ValueError):
        core.encode_png(2, 2, [b"\x00" * 4, b"\x00" * 8])   # short row
    with pytest.raises(ValueError):
        core.encode_png(0, 1, [])


# ── icons ───────────────────────────────────────────────────────────────────

def test_hex_to_rgb_handles_both_forms():
    assert core.hex_to_rgb("#fff") == (255, 255, 255)
    assert core.hex_to_rgb("5a9e8f") == (0x5A, 0x9E, 0x8F)
    with pytest.raises(ValueError):
        core.hex_to_rgb("#ff")


def test_render_icon_produces_the_requested_size_and_is_deterministic():
    a = core.render_icon(64)
    b = core.render_icon(64)
    assert a == b                                    # cacheable, reproducible
    (width, height) = struct.unpack(">II", _png_chunks(a)[0][1][:8])
    assert (width, height) == (64, 64)


def test_icon_actually_draws_the_mark():
    """A solid tile would still be a valid PNG — check the glyph colour is
    really present, and that it moved when asked to."""
    def pixels(blob, size):
        raw = zlib.decompress(_png_chunks(blob)[1][1])
        stride = size * 4
        return {tuple(raw[1 + r * (stride + 1) + c * 4:1 + r * (stride + 1) + c * 4 + 3])
                for r in range(size) for c in range(size)}

    default = pixels(core.render_icon(64), 64)
    assert core.hex_to_rgb(core.DEFAULT_CONFIG["background_color"]) in default
    assert core.hex_to_rgb(core.DEFAULT_CONFIG["icon_color"]) in default

    recoloured = pixels(core.render_icon(64, color="#ff0000"), 64)
    assert (255, 0, 0) in recoloured


def test_rounded_corners_are_transparent_but_maskable_is_full_bleed():
    def corner_alpha(blob, size):
        raw = zlib.decompress(_png_chunks(blob)[1][1])
        return raw[1 + 3]        # alpha of pixel (0,0)

    assert corner_alpha(core.render_icon(64), 64) == 0
    assert corner_alpha(core.render_icon(64, maskable=True), 64) == 255


def test_render_icon_rejects_absurd_sizes():
    for bad in (0, -1, 4096):
        with pytest.raises(ValueError):
            core.render_icon(bad)


def test_every_advertised_size_is_renderable_by_a_route():
    assert set(core.ICON_SIZES) <= core.RENDERABLE_SIZES
    assert core.APPLE_ICON_SIZE in core.RENDERABLE_SIZES


def test_icon_svg_is_well_formed_and_uses_the_configured_colours():
    svg = core.render_icon_svg(background="#101010", color="#00ff00")
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert svg.count("<circle") == 3
    assert "#101010" in svg and "#00ff00" in svg
    assert "viewBox=\"0 0 512 512\"" in svg


def test_icon_svg_refuses_an_injected_colour():
    """The colour lands inside an SVG attribute — it must be validated, not
    interpolated raw."""
    svg = core.render_icon_svg(color='#fff" onload="alert(1)')
    assert "onload" not in svg
    assert core.DEFAULT_CONFIG["icon_color"] in svg


# ── the shipped shell must actually carry the tags ──────────────────────────

SHELL = REPO_ROOT / "vera" / "capability_orchestration.html"


@pytest.mark.skipif(not SHELL.exists(), reason="shell HTML not in this checkout")
def test_the_shell_carries_every_tag_that_makes_it_installable():
    """head_tags() documents what a page needs; this asserts the real shell
    still has it, so removing a tag during an unrelated UI edit fails here
    rather than silently making Vera uninstallable."""
    html = SHELL.read_text(encoding="utf-8", errors="replace")
    for needle in ('rel="manifest"',
                   'href="/manifest.webmanifest"',
                   'name="viewport"',
                   'name="theme-color"',
                   'rel="apple-touch-icon"',
                   'src="/ui/pwa/pwa.js"'):
        assert needle in html, f"app shell lost {needle}"


@pytest.mark.skipif(not SHELL.exists(), reason="shell HTML not in this checkout")
def test_head_tags_and_the_shell_reference_the_same_urls():
    html = SHELL.read_text(encoding="utf-8", errors="replace")
    for url in ("/manifest.webmanifest", "/ui/pwa/pwa.js", "/ui/pwa/icon.svg",
                "/ui/pwa/apple-touch-icon.png"):
        assert url in core.head_tags(), f"head_tags() lost {url}"
        assert url in html, f"app shell lost {url}"


# ── the worker source and the policy must fit together ──────────────────────

SW = REPO_ROOT / "vera" / "pwa" / "sw.js"


@pytest.mark.skipif(not SW.exists(), reason="sw.js not in this checkout")
def test_worker_source_has_exactly_one_policy_placeholder():
    source = SW.read_text(encoding="utf-8")
    assert source.count("__VERA_PWA_POLICY__") == 1


@pytest.mark.skipif(not SW.exists(), reason="sw.js not in this checkout")
def test_worker_reads_every_key_the_policy_provides():
    """Catches the drift this split invites: a policy key nothing consumes, or
    a worker reading a key Python stopped emitting."""
    source = SW.read_text(encoding="utf-8")
    cfg, _ = core.normalise_config()
    for key in core.cache_policy(cfg, "v1"):
        assert f"POLICY.{key}" in source, f"sw.js never reads POLICY.{key}"


@pytest.mark.skipif(not SW.exists(), reason="sw.js not in this checkout")
def test_worker_never_calls_skip_waiting_on_install():
    """Swapping the worker under a running dashboard mixes old and new assets
    in one session; the update is offered to the user instead."""
    source = SW.read_text(encoding="utf-8")
    install = source.split("addEventListener('install'", 1)[1].split(
        "addEventListener('activate'", 1)[0]
    # Comments in that block explain *why* it isn't called; judge the code.
    code = "\n".join(line.split("//")[0] for line in install.split("\n"))
    assert "skipWaiting" not in code
