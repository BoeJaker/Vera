"""
pwa_capabilities.py — Vera as an installable Progressive Web App
================================================================

Turns the orchestrator harness into something a browser will install as a
real application: its own window, its own icon in the launcher/dock, an
offline shell when the box is unreachable, and a status bar that follows the
active Vera theme.

Routes
  GET /manifest.webmanifest   the web app manifest (also /manifest.json)
  GET /sw.js                  the service worker, scoped to /
  GET /pwa/offline            the offline shell (precached by the worker)
  GET /pwa/panel              settings + diagnostics panel
  GET /ui/pwa/pwa.js          the client: registration, install, update
  GET /ui/pwa/icon.svg        the app mark, vector
  GET /ui/pwa/icon-{n}.png    app icons (any purpose)
  GET /ui/pwa/maskable-{n}.png        app icons (maskable purpose)
  GET /ui/pwa/apple-touch-icon.png    iOS home-screen icon
  GET /favicon.ico            so every page stops 404ing for one

Capabilities
  pwa.config.get   read the live PWA configuration
  pwa.config.set   change name/colours/display/caching, persisted to Redis
  pwa.status       diagnostics: what is served, what a client will see

All of the actual logic — validation, the manifest, the cache policy, icon
rasterisation — lives in the app-free `pwa_core`, so it is unit-testable
without booting the orchestrator (tests/test_pwa_core.py).

Safety note on the service worker: it only intercepts navigations and an
explicit allowlist of static UI assets, and it is network-first by default.
Capability calls, event streams and API GETs are never touched, and a UI file
edited on the server is still picked up on the next load with no restart —
exactly as before. `pwa.config.set(enabled=false)` turns the whole thing off
and makes /sw.js serve a worker that uninstalls itself and drops its caches.
"""
# NOTE: no `from __future__ import annotations` here on purpose — the
# @capability decorator derives each cap's JSON schema from the REAL
# annotation objects, and stringised annotations would silently degrade every
# typed parameter below to "string".

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability, emit_event

from Vera.vera.pwa.pwa_core import (
    APPLE_ICON_SIZE, ASSET_STRATEGIES, DEFAULT_CONFIG, DISPLAY_MODES,
    ICON_SIZES, RENDERABLE_SIZES, asset_version, build_manifest, cache_policy,
    head_tags, icon_urls, normalise_config, render_icon, render_icon_svg,
)

log = logging.getLogger("vera.pwa")

_HERE = Path(__file__).parent
_REDIS_KEY = "vera:pwa:config"

# Loop Lab hands out sandbox PORTS from a pool, so :8982 is a different branch
# next week — and a service worker's caches are keyed by origin, which includes
# the port but not the branch. A cached page could therefore resurface under a
# later, unrelated sandbox whenever that container is down. Prod has no such
# ambiguity, so the install layer is simply off by default in dev containers;
# pwa.config.set(enabled=true) turns it on in one when you want to test it.
_DEV_MODE = (os.getenv("VERA_DEV_MODE", "").strip().lower()
             not in ("", "0", "false", "no"))

# Live config, lazily hydrated from Redis on first use. Redis is the store of
# record; this is only a cache so the icon/manifest routes don't round-trip.
_CONFIG: Dict[str, Any] = dict(DEFAULT_CONFIG, enabled=not _DEV_MODE)
_CONFIG_LOADED = False

# Rendered icons, keyed by (size, maskable, background, colour). Rasterising a
# 512px icon in pure Python is not free, so it is done once per variant and
# then served from here. Cleared whenever the config changes.
_ICON_CACHE: Dict[tuple, bytes] = {}
_ICON_LOCK = None                     # asyncio.Lock, created on first use


def _redis():
    return getattr(_orch, "REDIS", None)


async def _load_config() -> Dict[str, Any]:
    """Return the live config, hydrating from Redis exactly once."""
    global _CONFIG, _CONFIG_LOADED
    if _CONFIG_LOADED:
        return _CONFIG
    r = _redis()
    if r is None:
        # Redis isn't up yet (module import can precede it). Serve defaults for
        # now but stay un-hydrated, so a saved config is still picked up once
        # the handle exists rather than being lost until the next restart.
        return _CONFIG
    _CONFIG_LOADED = True                 # one attempt only: a failing read
    try:                                  # must not retry-storm every request
        raw = await r.get(_REDIS_KEY)
        if raw:
            stored = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            # Re-validate on read: a hand-edited key must not be able to put a
            # foreign origin into the manifest of an installed app.
            # A stored config from a previous branch on this pooled
            # port must not switch the worker back on in a sandbox.
            stored.setdefault("enabled", not _DEV_MODE)
            _CONFIG, _ = normalise_config(stored)
    except Exception as exc:
        log.warning("pwa: could not load config from redis (%s) - using defaults", exc)
    return _CONFIG


def _disabled_reason(cfg: Dict[str, Any]) -> str:
    """Why the install layer is off, when it is — so pwa.status never leaves a
    disabled PWA looking like a bug."""
    if cfg.get("enabled", True):
        return ""
    if _DEV_MODE:
        return ("dev container (VERA_DEV_MODE): sandbox ports are pooled, so a "
                "cached page could resurface under a different branch. Call "
                "pwa.config.set(enabled=true) to test the PWA in this sandbox.")
    return "switched off via pwa.config.set(enabled=false)"


def _read(name: str) -> str:
    """Read a sibling asset. Re-read per request, like Vera's other static
    routes, so editing sw.js/pwa.js takes effect without a restart."""
    path = _HERE / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _version(cfg: Dict[str, Any]) -> str:
    """Version stamp over the config AND the served worker/client sources, so
    any edit to either makes the browser install the new worker."""
    return asset_version(cfg, _read("sw.js"), _read("pwa_client.js"))


async def _icon_bytes(size: int, cfg: Dict[str, Any], maskable: bool) -> bytes:
    """Rendered icon bytes, rasterised at most once per variant.

    Rasterising 512px in pure Python takes on the order of a second, and a
    browser asks for five icons back to back when it installs the app — done
    inline that is seconds of stalled event loop on a live orchestrator. So
    it goes to a worker thread, behind a lock so two concurrent requests for
    a cold icon don't both pay for it.
    """
    key = (size, maskable, cfg["background_color"], cfg["icon_color"])
    hit = _ICON_CACHE.get(key)
    if hit is not None:
        return hit
    global _ICON_LOCK
    if _ICON_LOCK is None:
        _ICON_LOCK = asyncio.Lock()
    async with _ICON_LOCK:
        hit = _ICON_CACHE.get(key)        # another request may have won the race
        if hit is None:
            hit = await asyncio.to_thread(
                render_icon, size, background=cfg["background_color"],
                color=cfg["icon_color"], maskable=maskable)
            if len(_ICON_CACHE) > 24:     # bounded; variants are cheap to redo
                _ICON_CACHE.clear()
            _ICON_CACHE[key] = hit
    return hit


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@APP.get("/manifest.webmanifest", include_in_schema=False)
async def _serve_manifest():
    from fastapi.responses import Response
    cfg = await _load_config()
    body = json.dumps(build_manifest(cfg, _version(cfg)), indent=2)
    return Response(content=body, media_type="application/manifest+json",
                    headers={"Cache-Control": "no-cache"})


@APP.get("/manifest.json", include_in_schema=False)
async def _serve_manifest_json():
    # Alias: plenty of tooling (and older docs) look for /manifest.json.
    return await _serve_manifest()


# A worker that removes itself, served instead of the real one when the PWA is
# switched off. Without this, disabling the feature would leave every browser
# that already installed the worker running it forever.
_UNREGISTER_SW = """/* Vera PWA is disabled — this worker uninstalls itself. */
'use strict';
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k.startsWith('vera-pwa-'))
      .map((k) => caches.delete(k)));
    await self.registration.unregister();
    const clients = await self.clients.matchAll({ type: 'window' });
    clients.forEach((c) => { try { c.navigate(c.url); } catch (e) {} });
  })());
});
"""


@APP.get("/sw.js", include_in_schema=False)
async def _serve_service_worker():
    from fastapi.responses import Response
    cfg = await _load_config()
    headers = {
        # The worker script itself must never be served from cache, or an
        # update would never be discovered.
        "Cache-Control": "no-cache, no-store, must-revalidate",
        # Redundant at the root, but explicit: this worker controls all of /.
        "Service-Worker-Allowed": "/",
    }
    if not cfg.get("enabled", True):
        return Response(content=_UNREGISTER_SW,
                        media_type="application/javascript", headers=headers)
    source = _read("sw.js")
    if not source:
        log.error("pwa: sw.js missing next to %s", __file__)
        return Response(content="/* vera sw.js not found */",
                        media_type="application/javascript", headers=headers)
    policy = json.dumps(cache_policy(cfg, _version(cfg)), separators=(",", ":"))
    return Response(content=source.replace("__VERA_PWA_POLICY__", policy),
                    media_type="application/javascript", headers=headers)


@APP.get("/pwa/offline", include_in_schema=False)
async def _serve_offline():
    from fastapi.responses import HTMLResponse
    body = _read("offline.html") or "<p>Vera is offline.</p>"
    return HTMLResponse(content=body, headers={"Cache-Control": "no-cache"})


@APP.get("/ui/pwa/pwa.js", include_in_schema=False)
async def _serve_pwa_client():
    from fastapi.responses import Response
    return Response(content=_read("pwa_client.js")
                    or "console.warn('vera pwa_client.js not found');",
                    media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


def _png(body: bytes):
    from fastapi.responses import Response
    # Immutable + long max-age is safe because every icon URL in the manifest
    # carries the version stamp as a query string.
    return Response(content=body, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=604800"})


@APP.get("/ui/pwa/icon.svg", include_in_schema=False)
async def _serve_icon_svg():
    from fastapi.responses import Response
    cfg = await _load_config()
    return Response(content=render_icon_svg(background=cfg["background_color"],
                                            color=cfg["icon_color"]),
                    media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=604800"})


@APP.get("/ui/pwa/icon-{size}.png", include_in_schema=False)
async def _serve_icon_png(size: int):
    from fastapi.responses import Response
    if size not in RENDERABLE_SIZES:
        return Response(status_code=404, content=b"")
    cfg = await _load_config()
    return _png(await _icon_bytes(size, cfg, False))


@APP.get("/ui/pwa/maskable-{size}.png", include_in_schema=False)
async def _serve_icon_maskable_png(size: int):
    from fastapi.responses import Response
    if size not in RENDERABLE_SIZES:
        return Response(status_code=404, content=b"")
    cfg = await _load_config()
    return _png(await _icon_bytes(size, cfg, True))


@APP.get("/ui/pwa/apple-touch-icon.png", include_in_schema=False)
async def _serve_apple_icon():
    # iOS ignores the manifest's icons for the home screen and masks the
    # result itself, so this one is drawn full-bleed like a maskable icon.
    cfg = await _load_config()
    return _png(await _icon_bytes(APPLE_ICON_SIZE, cfg, True))


@APP.get("/favicon.ico", include_in_schema=False)
async def _serve_favicon():
    # Browsers request this for any page that doesn't declare an icon; serving
    # a PNG under the .ico name is universally accepted and beats a 404 per
    # panel iframe. Pages that do declare one use the crisper SVG.
    cfg = await _load_config()
    return _png(await _icon_bytes(96, cfg, False))


@APP.get("/pwa/panel", include_in_schema=False)
async def _serve_pwa_panel():
    from fastapi.responses import HTMLResponse
    body = _read("pwa_panel.html")
    return HTMLResponse(content=body or "<p style='color:red'>pwa_panel.html not found</p>")


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability("pwa.config.get", memory="off", silent=True,
            http_method="GET", http_path="/pwa/config", http_tags=["pwa"],
            description="Read the live PWA configuration (app name, colours, "
                        "display mode, service-worker caching strategy) plus the "
                        "current version stamp and the URLs it produces. No inputs.")
async def pwa_config_get(trace_id=None):
    cfg = await _load_config()
    version = _version(cfg)
    return {"config": cfg, "version": version, "icons": icon_urls(version),
            "manifest_url": "/manifest.webmanifest",
            "service_worker_url": "/sw.js",
            "offline_url": "/pwa/offline"}


@capability("pwa.config.set", memory="on",
            http_method="POST", http_path="/pwa/config", http_tags=["pwa"],
            description="Change the PWA configuration and persist it. Any subset of: "
                        "enabled (bool — false makes /sw.js serve a worker that "
                        "uninstalls itself and clears its caches), name, short_name, "
                        "description, start_url, scope (same-origin absolute paths "
                        "only), display (fullscreen|standalone|minimal-ui|browser), "
                        "orientation, theme_color, background_color, icon_color "
                        "(#rrggbb), asset_strategy (network-first|stale-while-revalidate "
                        "— network-first keeps UI edits instant, the other trades that "
                        "for speed), network_timeout_ms, max_page_cache_entries, "
                        "shortcuts (list of {name,url}). Changing anything bumps the "
                        "version stamp, which is what makes installed clients pick the "
                        "new worker up. Returns the merged config and any rejected keys.")
async def pwa_config_set(enabled: Optional[bool] = None, name: str = "",
                         short_name: str = "", description: str = "",
                         start_url: str = "", scope: str = "", display: str = "",
                         orientation: str = "", theme_color: str = "",
                         background_color: str = "", icon_color: str = "",
                         asset_strategy: str = "",
                         network_timeout_ms: Optional[int] = None,
                         max_page_cache_entries: Optional[int] = None,
                         shortcuts: Optional[List[dict]] = None,
                         trace_id=None):
    base = await _load_config()
    patch: Dict[str, Any] = {}
    for key, value in (("enabled", enabled), ("name", name),
                       ("short_name", short_name), ("description", description),
                       ("start_url", start_url), ("scope", scope),
                       ("display", display), ("orientation", orientation),
                       ("theme_color", theme_color),
                       ("background_color", background_color),
                       ("icon_color", icon_color),
                       ("asset_strategy", asset_strategy),
                       ("network_timeout_ms", network_timeout_ms),
                       ("max_page_cache_entries", max_page_cache_entries),
                       ("shortcuts", shortcuts)):
        if value is not None and value != "":
            patch[key] = value
    if not patch:
        return {"error": "nothing to set", "config": base,
                "accepts": sorted(DEFAULT_CONFIG)}

    cfg, rejected = normalise_config(patch, base)

    global _CONFIG
    _CONFIG = cfg
    _ICON_CACHE.clear()                   # colours may have moved

    persisted = False
    r = _redis()
    if r:
        try:
            await r.set(_REDIS_KEY, json.dumps(cfg))
            persisted = True
        except Exception as exc:
            log.warning("pwa: could not persist config (%s)", exc)

    version = _version(cfg)
    await emit_event({"type": "pwa.config.changed", "version": version,
                      "changed": sorted(k for k in patch if k not in rejected)})
    return {"ok": True, "config": cfg, "version": version,
            "rejected": rejected, "persisted": persisted,
            "note": "installed clients pick this up on their next visit "
                    "(the worker is re-fetched and offers a reload)"}


@capability("pwa.status", memory="off", silent=True,
            http_method="GET", http_path="/pwa/status", http_tags=["pwa"],
            description="PWA diagnostics: whether the install layer is enabled, the "
                        "version stamp, which asset files are actually present on "
                        "disk, the exact service-worker cache policy in force, and "
                        "the installability requirements a browser will check. Use "
                        "this first when Vera will not offer to install. No inputs.")
async def pwa_status(trace_id=None):
    cfg = await _load_config()
    version = _version(cfg)
    assets = {name: (_HERE / name).exists()
              for name in ("sw.js", "pwa_client.js", "offline.html", "pwa_panel.html")}
    policy = cache_policy(cfg, version)
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "version": version,
        "assets_present": assets,
        "assets_ok": all(assets.values()),
        "icons_rendered": len(_ICON_CACHE),
        "config_source": "redis" if _redis() else "in-memory defaults",
        "dev_mode": _DEV_MODE,
        "disabled_reason": _disabled_reason(cfg),
        "policy": policy,
        "urls": {"manifest": "/manifest.webmanifest", "service_worker": "/sw.js",
                 "offline": "/pwa/offline", "panel": "/pwa/panel",
                 "client": "/ui/pwa/pwa.js", **icon_urls(version)},
        "install_requirements": [
            "served over a certificate the browser TRUSTS — a self-signed cert "
            "the user clicked through is NOT a secure context, and service "
            "worker registration is refused there (localhost is exempt)",
            "a reachable manifest with name, icons (192 and 512) and a "
            "start_url inside scope — all served here",
            "a registered service worker with a fetch handler — /sw.js",
        ],
        "head_tags": head_tags(cfg),
        "displays": list(DISPLAY_MODES),
        "asset_strategies": list(ASSET_STRATEGIES),
        "icon_sizes": list(ICON_SIZES),
    }


# ─────────────────────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────────────────────

# mode="element": registered and discoverable (and promotable to its own tab
# with the harness's ⊞ affordance) without claiming a permanent top-level tab
# for what is a settings page. Reachable directly at /pwa/panel.
_orch.register_ui(
    panel_id="pwa",
    label="Install / PWA",
    icon="⬓",
    mode="element",
    tab_order=260,
    html=('<div style="height:100%;display:flex;flex-direction:column">'
          '<iframe src="/pwa/panel" style="flex:1;border:none;width:100%;'
          'height:100%;background:var(--bg0,#181614)"></iframe></div>'),
    ui_caps=["pwa.status", "pwa.config.get", "pwa.config.set"],
)

log.info("pwa_capabilities loaded — installable app (/manifest.webmanifest, "
         "/sw.js, /pwa/offline, /pwa/panel)")
