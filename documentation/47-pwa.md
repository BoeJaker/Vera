# 47 · PWA — Vera as an installable app

> **Doc status:** complete reference for `vera/pwa/`.

`vera/pwa/pwa_capabilities.py` makes the [harness UI](./02-harness-ui.md) installable as a Progressive Web App: its own window and launcher icon, an offline shell when the box can't be reached, and a status bar that follows the active Vera theme. It adds no dependency — icons are rasterised in pure Python, so nothing needs Pillow and no binary lives in the repo.

| Cap | Purpose |
|---|---|
| `pwa.config.get` | Read the live PWA config, the version stamp, and the URLs it produces |
| `pwa.config.set` | Change name/colours/display/caching; persisted to Redis |
| `pwa.status` | Diagnostics — what's served, and what a browser will check before offering to install |

| Route | Serves |
|---|---|
| `/manifest.webmanifest` (and `/manifest.json`) | The web app manifest |
| `/sw.js` | The service worker, scoped to `/` |
| `/pwa/offline` | The offline shell (precached by the worker) |
| `/pwa/panel` | Settings + diagnostics panel |
| `/ui/pwa/pwa.js` | Client: registration, install prompt, update prompt, theme-color sync |
| `/ui/pwa/icon-{n}.png`, `/ui/pwa/maskable-{n}.png`, `/ui/pwa/icon.svg`, `/ui/pwa/apple-touch-icon.png`, `/favicon.ico` | Generated app icons |

## The service worker is deliberately narrow

Vera is a live dashboard: nearly every request is a capability call, an event stream, or loop output, and a cached answer to any of them is worse than no answer. So the worker **only** calls `respondWith` for two things:

* **navigations** — the shell and each panel iframe, always network-first;
* **an explicit allowlist of static UI files** — `/ui/elements/*`, `/ui/vera-*`, `/ui/themes.css`, `/ui/pwa/*`, plus same-origin files with a static extension.

Everything else falls through untouched, so it behaves exactly as it does with no worker installed. A **bypass list beats the allowlist**, so a `.js` served from under `/mcp/`, `/chat/`, `/evolve/` and friends stays uncached. Non-GET requests, ranged requests, cross-origin requests and `text/event-stream` are never intercepted.

The default asset strategy is **network-first**: the network always wins when it answers, so Vera's existing property that *a UI file edited on the server is live on the next load with no restart* is preserved exactly. The cache is only consulted when the network is slow (`network_timeout_ms`) or gone. `pwa.config.set(asset_strategy="stale-while-revalidate")` trades that for speed — the cached copy is served first and refreshed behind it, which means a UI change can be one load behind.

The worker never calls `skipWaiting()` on install. Swapping it under a running dashboard is how you get half-old, half-new assets in one session; instead the client offers a *Reload* and the update happens on the user's word.

`pwa.config.set(enabled=false)` is a real kill switch: `/sw.js` then serves a worker that drops every `vera-pwa-*` cache and unregisters itself, so browsers that already installed it clean themselves up rather than running the old one forever.

The install layer defaults **off inside a Loop Lab per-branch container** and nowhere else. Those containers take their host port from a pool, and a service worker's caches are keyed by origin — which includes the port but not the branch — so a page cached under `:8982` today could resurface under a different branch's sandbox tomorrow. The signal is `VERA_IS_DEV_SANDBOX`, which only evolve's per-branch compose sets; deliberately **not** `VERA_DEV_MODE`, which prod's own `.env` sets as well. `pwa.config.set(enabled=true)` turns it on in a sandbox to test the install flow there.

## Where the logic lives

`pwa_core.py` is app-free — no FastAPI, no Redis — and holds the config schema and validation, the manifest builder, the cache policy, and the icon rasteriser. `sw.js` carries a single `__VERA_PWA_POLICY__` placeholder that the route fills from `pwa_core.cache_policy()`, so the routing rules have one definition rather than a Python copy and a JavaScript copy that drift. `tests/test_pwa_core.py` asserts that every key the policy emits is actually read by `sw.js`, and that the shipped shell still carries the tags that make it installable.

Config is validated on write **and on read**: `start_url`, `scope` and shortcut URLs must be same-origin absolute paths, and colours must be hex. A manifest's `start_url` is honoured by the browser, so a hand-edited Redis key must not be able to repoint an installed app at another origin.

Every icon and manifest URL carries a version stamp hashed over the config plus the worker and client sources, which is both what busts icon caches and what makes a browser notice there is a new worker to install.

## Why it might not offer to install

`pwa.status` reports this, and the panel renders it. The usual answer is the first one:

1. **The certificate isn't trusted.** An HTTPS origin whose self-signed certificate the user clicked through is *not* a secure context, and browsers refuse to register a service worker there. Trust Vera's certificate on the device, or reach it over `localhost`, which is exempt. See [Security](./29-security.md).
2. A reachable manifest with `name`, 192px and 512px icons, and a `start_url` inside `scope` — all served here.
3. A registered service worker with a fetch handler — `/sw.js`.

The panel is registered in element mode, so it doesn't claim a top-level tab; open it directly at `/pwa/panel`, or promote it to a tab with the harness's ⊞ affordance ([Harness UI](./02-harness-ui.md)).

## Related

- [Harness UI](./02-harness-ui.md) — the shell this makes installable, and its themes
- [UI Builder](./26-ui-builder.md) — themes, panels, and the loading animation
- [Configuration](./10-configuration.md) — TLS and host settings that decide secure-context
