# 47 · PWA — Vera as an installable app

`vera/pwa/` makes the [harness UI](./02-harness-ui.md) installable as a
Progressive Web App: its own window and launcher icon, an offline shell when the
box cannot be reached, and a title/status bar that follows the active Vera
theme. It adds no dependency — icons are rasterised in pure Python, so nothing
needs Pillow and no binary icon lives in the repository.

The module is split in two. `vera/pwa/pwa_core.py` is app-free (no FastAPI, no
Redis, no `Vera.vera.*` imports) and holds the configuration schema and
validation, the manifest builder, the service-worker cache policy, the
`<head>` tag set and the icon rasteriser. `vera/pwa/pwa_capabilities.py` serves
the routes, owns persistence and registers the capabilities and panel. The
browser side is `sw.js` (the service worker), `pwa_client.js` (registration,
install and update prompts, theme-colour sync), `offline.html` and
`pwa_panel.html`.

**Maturity:** complete and enabled by default on every instance except Loop Lab
per-branch sandboxes. Installation requires an origin the browser trusts
(see [Why it might not offer to install](#8-why-it-might-not-offer-to-install)).

## Contents

- [1. Source map](#1-source-map)
- [2. Capabilities](#2-capabilities)
- [3. Routes](#3-routes)
- [4. Configuration](#4-configuration)
  - [Validation](#validation)
  - [Versioning](#versioning)
- [5. The service worker is deliberately narrow](#5-the-service-worker-is-deliberately-narrow)
  - [Request classification](#request-classification)
  - [Updates and the kill switch](#updates-and-the-kill-switch)
- [6. The client (`/ui/pwa/pwa.js`)](#6-the-client-uipwapwajs)
- [7. Sandboxes](#7-sandboxes)
- [8. Why it might not offer to install](#8-why-it-might-not-offer-to-install)
- [9. Worked examples](#9-worked-examples)
- [10. Troubleshooting](#10-troubleshooting)
- [Related](#related)

## 1. Source map

| File | Responsibility |
|---|---|
| `vera/pwa/pwa_core.py` | `DEFAULT_CONFIG`, `normalise_config`, `asset_version`, `icon_urls`, `build_manifest`, `cache_policy`, `classify_request`, `head_tags`, `encode_png`, `render_icon`, `render_icon_svg`, `is_pooled_sandbox` |
| `vera/pwa/pwa_capabilities.py` | Routes, Redis persistence (`vera:pwa:config`), icon cache, `pwa.*` capabilities, panel registration |
| `vera/pwa/sw.js` | Service worker; carries one `__VERA_PWA_POLICY__` placeholder filled from `pwa_core.cache_policy()` at serve time |
| `vera/pwa/pwa_client.js` | Client served at `/ui/pwa/pwa.js` |
| `vera/pwa/offline.html` | Offline shell, precached by the worker |
| `vera/pwa/pwa_panel.html` | Settings and diagnostics panel |
| `tests/test_pwa_core.py` | Asserts every policy key is read by `sw.js` and that the shipped harness still carries the installability tags |

## 2. Capabilities

| Capability | Route | Purpose |
|---|---|---|
| `pwa.config.get` | `GET /pwa/config` | The live configuration, its version stamp, and the URLs it produces (manifest, service worker, offline page, icons) |
| `pwa.config.set` | `POST /pwa/config` | Change any subset of the configuration; persisted to Redis; returns `{ok, config, version, rejected, persisted, note}` and emits `pwa.config.changed {version, changed}` |
| `pwa.status` | `GET /pwa/status` | Diagnostics: `enabled`, `version`, which asset files are present, icons rendered, config source (`redis` or in-memory defaults), sandbox state and `disabled_reason`, the exact cache policy, URLs, install requirements, the `<head>` tags, and the allowed display modes, asset strategies and icon sizes |

`pwa.config.set` with no recognised field returns `{"error": "nothing to set",
"accepts": [...]}`.

## 3. Routes

| Route | Serves | Caching headers |
|---|---|---|
| `/manifest.webmanifest` (and `/manifest.json`) | The web app manifest | `no-cache` |
| `/sw.js` | The service worker, scope `/` (`Service-Worker-Allowed: /`) | `no-cache, no-store, must-revalidate` |
| `/pwa/offline` | The offline shell | `no-cache` |
| `/pwa/panel` | Settings and diagnostics panel | — |
| `/ui/pwa/pwa.js` | The client | `no-cache` |
| `/ui/pwa/icon.svg` | The app mark (vector) | 7 days |
| `/ui/pwa/icon-{size}.png`, `/ui/pwa/maskable-{size}.png` | App icons, `any` and `maskable` purpose | 7 days |
| `/ui/pwa/apple-touch-icon.png` | 180 px iOS home-screen icon | 7 days |
| `/favicon.ico` | Favicon | — |

Icons are rendered at the advertised sizes 192 and 512, plus 180 (Apple); a
route only renders sizes in `{96, 144, 180, 192, 256, 512, 1024}`, so a URL
cannot request an arbitrarily large image. Rendered icons are cached per
size, purpose and colours, and the cache is cleared when the configuration
changes. The icon is drawn from signed-distance fields, which antialiases at one
sample per pixel.

The harness carries the installability tags in its `<head>`: the viewport
meta, `<link rel="manifest" href="/manifest.webmanifest">`, `theme-color`,
the SVG icon and Apple touch icon links, the mobile/Apple web-app metas and
`<script src="/ui/pwa/pwa.js" defer>`. `pwa_core.head_tags()` produces the same
set and the test suite asserts they match.

## 4. Configuration

Stored as JSON in Redis at `vera:pwa:config`; served from an in-memory copy
hydrated once from Redis. Defaults (`pwa_core.DEFAULT_CONFIG`):

| Key | Default | Allowed values |
|---|---|---|
| `enabled` | `true` (`false` in a pooled sandbox) | boolean |
| `name` | `Vera — Orchestrator Harness` | text, ≤ 300 chars |
| `short_name` | `Vera` | text, ≤ 300 chars |
| `description` | `Vera's orchestrator harness — capabilities, agents, loops and the estate, in one place.` | text, ≤ 300 chars |
| `start_url` | `/` | same-origin absolute path |
| `scope` | `/` | same-origin absolute path; reset to `/` if it does not contain `start_url` |
| `display` | `standalone` | `fullscreen`, `standalone`, `minimal-ui`, `browser` |
| `orientation` | `any` | `any`, `portrait`, `landscape`, `natural` |
| `theme_color` | `#181614` | `#rgb` / `#rrggbb` |
| `background_color` | `#181614` | `#rgb` / `#rrggbb` |
| `icon_color` | `#5a9e8f` | `#rgb` / `#rrggbb` |
| `asset_strategy` | `network-first` | `network-first`, `stale-while-revalidate` |
| `network_timeout_ms` | `4000` | clamped to 500–30000 |
| `max_page_cache_entries` | `60` | clamped to 0–1000 |
| `shortcuts` | `[]` | up to 8 `{name (≤60), url, description (≤200)}` with same-origin URLs |

The manifest adds `id` (= `start_url`), `categories`
(`productivity`, `utilities`), `lang`, `dir`, the PNG icons (any and maskable at
192 and 512) and the SVG icon; each shortcut gets the 192 px icon.

### Validation

Configuration is validated **on write and on read**. Invalid values fall back
to the previous value rather than raising — a bad colour in Redis must never
take the manifest route down — and the key is named in `rejected`. Paths must be
same-origin absolute paths: a value starting with `//` or containing `..` is
refused, so a hand-edited Redis key cannot repoint an installed app at another
origin.

### Versioning

Every icon and manifest URL carries a 12-character version stamp: a SHA-256
over the configuration plus the source of `sw.js` and `pwa_client.js`. Changing
any of them busts icon caches and changes the bytes of `/sw.js`, which is what
makes a browser notice there is a new worker to install.

## 5. The service worker is deliberately narrow

Vera is a live dashboard: nearly every request is a capability call, an event
stream, or loop output, and a cached answer to any of them is worse than no
answer. So the worker **only** calls `respondWith` for two things:

* **navigations** — the shell and each panel iframe, always network-first,
  falling back to the cached page and then to `/pwa/offline`;
* **an explicit allowlist of static UI files** — paths under `/ui/elements/`,
  `/ui/pwa/`, `/ui/vera-`, plus `/ui/themes.css`, `/manifest.webmanifest`, and
  same-origin files ending in `.js`, `.css`, `.png`, `.svg`, `.webp`, `.ico`,
  `.woff`, `.woff2` or `.ttf`.

Everything else falls through untouched, so it behaves exactly as it does with
no worker installed. A **bypass list beats the allowlist**: anything under
`/mcp/`, `/events`, `/event`, `/stream`, `/sse`, `/ws`, `/api/`, `/workshop/`,
`/evolve/`, `/dream/`, `/agent`, `/chat/`, `/loops/`, `/health`, `/metrics`,
`/docs`, `/openapi.json`, `/vscode/` or `/godseye/` is never intercepted, even a
`.js` file. Non-GET requests, ranged requests, cross-origin requests and
`text/event-stream` are never intercepted. Extension-less paths under `/ui`
such as `/ui/loader` and `/ui/themes` are live capability endpoints and are
deliberately absent from the allowlist.

The default asset strategy is **network-first**: the network always wins when
it answers, so a UI file edited on the server is live on the next load with no
restart. The cache is only consulted when the network is slower than
`network_timeout_ms` or gone. `asset_strategy="stale-while-revalidate"` serves
the cached copy first and refreshes it behind, which is faster but can leave a
UI change one load behind.

On install the worker precaches `/pwa/offline`, the manifest and the SVG,
192 px and 512 px icons into `vera-pwa-assets-<version>`; pages go to
`vera-pwa-pages-<version>`, bounded by `max_page_cache_entries`. On activation
it deletes every older `vera-pwa-*` cache and claims open clients.

### Request classification

`pwa_core.classify_request(path, method, mode)` is a Python twin of the
worker's routing, so the rules are testable without a browser:

| Result | Meaning |
|---|---|
| `bypass` | The worker must not intercept (browser default behaviour) |
| `page` | A navigation: network-first with offline fallback |
| `asset` | A cacheable static asset |

### Updates and the kill switch

The worker never calls `skipWaiting()` on install. Swapping it under a running
dashboard is how you get half-old, half-new assets in one session; instead the
client shows a *Reload* prompt and, on the user's word, posts
`VERA_PWA_SKIP_WAITING`; the page reloads on `controllerchange`. The worker
also answers `VERA_PWA_VERSION` and `VERA_PWA_CLEAR` (drop all `vera-pwa-*`
caches) messages.

`pwa.config.set(enabled=false)` is a real kill switch: `/sw.js` then serves a
worker that drops every `vera-pwa-*` cache and unregisters itself, so browsers
that already installed it clean themselves up rather than running the old one
forever.

## 6. The client (`/ui/pwa/pwa.js`)

The client is best-effort: on a browser without service-worker support, or an
origin that is not a secure context, the page behaves exactly as it did without
it and nothing throws. It:

- registers `/sw.js` (scope `/`) when the page is a secure context, otherwise
  records `insecure context — trust the TLS certificate to install` in its
  status;
- keeps `<meta name="theme-color">` equal to the painted `--bg0` (falling back
  to `--bg1`), so the standalone window's title bar and the mobile status bar
  follow theme changes;
- captures `beforeinstallprompt` and shows an unobtrusive install pill; a
  dismissal is remembered for 30 days (`localStorage` key
  `vera:pwa:installDismissed`);
- shows an update pill when a new worker is waiting.

The install and update pills are drawn only in the top-level window, never
inside panel iframes.

Public API:

```javascript
window.veraPWA.status()       // {supported, secureContext, registered, controlled,
                              //  installable, installed, updateReady, version, error}
window.veraPWA.install()      // show the browser's install prompt when available
window.veraPWA.update()       // activate a waiting worker (the page reloads)
window.veraPWA.unregister()   // unregister the worker
window.veraPWA.clearCaches()  // drop every vera-pwa-* cache
```

## 7. Sandboxes

The install layer defaults **off inside a Loop Lab per-branch container** and
nowhere else. Those containers take their host port from a pool, and a service
worker's caches are keyed by origin — which includes the port but not the
branch — so a page cached under one port today could resurface under a
different branch's sandbox tomorrow. The signal is `VERA_IS_DEV_SANDBOX`
(any value other than empty, `0`, `false` or `no`), which only Loop Lab's
per-branch compose sets; deliberately **not** `VERA_DEV_MODE`, which a
production `.env` may set as well. A stored configuration from a previous
branch cannot switch the worker back on in a sandbox, and `pwa.status` explains
the disabled state in `disabled_reason`. `pwa.config.set(enabled=true)` turns
it on in a sandbox to test the install flow there.

## 8. Why it might not offer to install

`pwa.status` reports this, and the panel renders it. The usual answer is the
first one:

1. **The certificate isn't trusted.** An HTTPS origin whose self-signed
   certificate the user clicked through is *not* a secure context, and browsers
   refuse to register a service worker there. Trust Vera's certificate on the
   device (or use a certificate from a trusted CA via `TLS_CERTFILE` /
   `TLS_KEYFILE`), or reach Vera over `localhost`, which is exempt. See
   [Configuration §2](./10-configuration.md#2-network-hosts-and-tls) and
   [Security](./29-security.md).
2. A reachable manifest with `name`, 192 px and 512 px icons, and a `start_url`
   inside `scope` — all served here.
3. A registered service worker with a fetch handler — `/sw.js`.

The panel (`pwa`, label "Install / PWA") is registered in element mode, so it
does not claim a top-level tab; open it directly at `/pwa/panel`, or promote it
to a tab with the harness's ⊞ control ([Harness UI](./02-harness-ui.md#modeelement)).

## 9. Worked examples

```bash
# What is served, and why it might not install
curl -s http://localhost:8999/pwa/status | jq '{enabled, version, disabled_reason, assets_ok}'

# Rename the app and give it a shortcut
curl -s -X POST http://localhost:8999/pwa/config -H 'content-type: application/json' \
  -d '{"short_name":"Vera Lab","shortcuts":[{"name":"Perf","url":"/perf/panel"}]}'

# Trade instant UI edits for faster loads
curl -s -X POST http://localhost:8999/mcp/call -H 'content-type: application/json' \
  -d '{"name":"pwa.config.set","arguments":{"asset_strategy":"stale-while-revalidate"}}'

# Turn the install layer off everywhere (installed workers uninstall themselves)
curl -s -X POST http://localhost:8999/pwa/config -H 'content-type: application/json' \
  -d '{"enabled":false}'
```

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| No install prompt | See [§8](#8-why-it-might-not-offer-to-install); check `window.veraPWA.status().secureContext` |
| A UI edit is not visible | `asset_strategy` is `stale-while-revalidate` (reload twice), or the network timed out and a cached copy was served |
| An old worker keeps serving | Accept the Reload prompt, call `veraPWA.update()`, or disable and re-enable via `pwa.config.set` |
| `rejected` lists a key | The value failed validation and the previous value was kept |
| `config_source` is `in-memory defaults` | Redis was not connected when the configuration was first read; settings are still applied in memory but not persisted |
| PWA disabled in a sandbox | Expected; `pwa.config.set(enabled=true)` to test there |

## Related

- [Harness UI](./02-harness-ui.md) — the shell this makes installable, and its themes
- [UI Builder](./26-ui-builder.md) — themes, panels, and the loading animation
- [Configuration](./10-configuration.md) — TLS and host settings that decide secure-context
- [Security](./29-security.md) — certificates and trust
- [Loop Lab](./33-evolve.md) — per-branch sandboxes
