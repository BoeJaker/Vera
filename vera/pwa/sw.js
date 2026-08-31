/* Vera service worker.
 *
 * Served from /sw.js by vera/pwa/pwa_capabilities.py, which substitutes the
 * policy blob below from vera/pwa/pwa_core.cache_policy() — that Python
 * function is the single source of truth for the routing rules, so this file
 * never re-declares them and the two can't drift.
 *
 * Design rule, and the reason this is safe to ship on a live dashboard:
 * the worker only calls respondWith for (a) navigations and (b) an explicit
 * allowlist of static UI assets. Everything else — every capability call,
 * event stream, websocket handshake and API GET — is left completely
 * untouched, so it behaves exactly as it does with no worker installed.
 * The bypass list is checked first and beats the allowlist.
 */
'use strict';

const POLICY = __VERA_PWA_POLICY__;
const ORIGIN = self.location.origin;

// ── routing ────────────────────────────────────────────────────────────────

function isBypassed(path) {
  for (const prefix of POLICY.bypassPrefixes) {
    if (path === prefix || path.startsWith(prefix)) return true;
  }
  return false;
}

function isAsset(path) {
  for (const prefix of POLICY.assetPrefixes) {
    if (path.startsWith(prefix)) return true;
  }
  if (path === '/manifest.webmanifest') return true;
  for (const ext of POLICY.assetExtensions) {
    if (path.endsWith(ext)) return true;
  }
  return false;
}

// ── helpers ────────────────────────────────────────────────────────────────

function withTimeout(promise, ms) {
  if (!ms || ms <= 0) return promise;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('vera-pwa-timeout')), ms);
    promise.then(
      (value) => { clearTimeout(timer); resolve(value); },
      (err) => { clearTimeout(timer); reject(err); },
    );
  });
}

/* Only ever store a same-origin, non-partial success. An opaque or errored
 * response cached here would be indistinguishable from a real one later. */
function isStorable(res) {
  return !!res && res.ok && res.status === 200 && res.type === 'basic';
}

async function trimCache(name, max) {
  if (!max || max <= 0) return;
  try {
    const cache = await caches.open(name);
    const keys = await cache.keys();
    for (let i = 0; i < keys.length - max; i++) await cache.delete(keys[i]);
  } catch (e) { /* trimming is best-effort */ }
}

/* Network-first: the network always wins when it answers, so a UI asset
 * edited on the server is picked up on the very next load — Vera's existing
 * "no restart needed for UI changes" behaviour is preserved. The cache is
 * only consulted when the network is slow or gone. */
async function networkFirst(event, request, cacheName) {
  const cache = await caches.open(cacheName);
  const network = fetch(request).then(async (res) => {
    if (isStorable(res)) {
      try { await cache.put(request, res.clone()); } catch (e) { /* quota */ }
    }
    return res;
  });
  try {
    return await withTimeout(network, POLICY.networkTimeoutMs);
  } catch (err) {
    const hit = await cache.match(request);
    if (hit) {
      // Let the slow request finish and refresh the cache behind us.
      event.waitUntil(network.catch(() => {}));
      return hit;
    }
    // Nothing cached: the network is the only answer, so wait it out rather
    // than fail on a timeout that was only meant to shortcut to the cache.
    return await network;
  }
}

async function staleWhileRevalidate(event, request, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request);
  const network = fetch(request).then(async (res) => {
    if (isStorable(res)) {
      try { await cache.put(request, res.clone()); } catch (e) { /* quota */ }
    }
    return res;
  });
  if (hit) {
    event.waitUntil(network.catch(() => {}));
    return hit;
  }
  return await network;
}

async function handleAsset(event, request) {
  try {
    if (POLICY.assetStrategy === 'stale-while-revalidate') {
      return await staleWhileRevalidate(event, request, POLICY.assetCache);
    }
    return await networkFirst(event, request, POLICY.assetCache);
  } catch (err) {
    const hit = await caches.match(request);
    if (hit) return hit;
    return new Response('', { status: 504, statusText: 'Vera is offline' });
  }
}

/* Navigations (the shell, and every panel iframe) are always network-first —
 * a cached page is a last resort, never the default. */
async function handlePage(event, request) {
  const cache = await caches.open(POLICY.pageCache);
  try {
    const res = await fetch(request);
    if (isStorable(res) &&
        (res.headers.get('content-type') || '').includes('text/html')) {
      try {
        await cache.put(request, res.clone());
        event.waitUntil(trimCache(POLICY.pageCache, POLICY.maxPageCacheEntries));
      } catch (e) { /* quota */ }
    }
    return res;
  } catch (err) {
    const hit = await cache.match(request) || await cache.match(request, { ignoreSearch: true });
    if (hit) return hit;
    // Only a real top-level page gets the offline shell; an iframe panel that
    // can't load should stay visibly empty rather than nest a second shell.
    if (request.destination === 'document' || request.destination === '') {
      const offline = await caches.match(POLICY.offlineUrl);
      if (offline) return offline;
    }
    return new Response('', { status: 504, statusText: 'Vera is offline' });
  }
}

// ── lifecycle ──────────────────────────────────────────────────────────────

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(POLICY.assetCache);
    // One bad URL must not fail the whole install, so each is added alone.
    await Promise.all(POLICY.precache.map((url) =>
      cache.add(new Request(url, { cache: 'reload' })).catch(() => {})));
  })());
  // Deliberately no skipWaiting(): swapping the worker under a running
  // dashboard is how you get half-old, half-new assets in one session. The
  // client offers a Reload instead and sends VERA_PWA_SKIP_WAITING.
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys
      .filter((k) => k.startsWith(POLICY.cachePrefix)
                  && k !== POLICY.assetCache && k !== POLICY.pageCache)
      .map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('message', (event) => {
  const data = event.data || {};
  const reply = (payload) => {
    try {
      if (event.ports && event.ports[0]) event.ports[0].postMessage(payload);
      else if (event.source) event.source.postMessage(payload);
    } catch (e) { /* the client went away */ }
  };
  if (data.type === 'VERA_PWA_SKIP_WAITING') {
    self.skipWaiting();
  } else if (data.type === 'VERA_PWA_VERSION') {
    reply({ type: 'VERA_PWA_VERSION', version: POLICY.version });
  } else if (data.type === 'VERA_PWA_CLEAR') {
    event.waitUntil((async () => {
      const keys = await caches.keys();
      await Promise.all(keys.filter((k) => k.startsWith(POLICY.cachePrefix))
        .map((k) => caches.delete(k)));
      reply({ type: 'VERA_PWA_CLEARED' });
    })());
  }
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  let url;
  try { url = new URL(request.url); } catch (e) { return; }
  if (url.origin !== ORIGIN) return;

  // Ranged media and server-sent events must never be intercepted: a cached
  // partial or a buffered stream is worse than no worker at all.
  if (request.headers.has('range')) return;
  const accept = request.headers.get('accept') || '';
  if (accept.includes('text/event-stream')) return;

  const path = url.pathname;
  if (isBypassed(path)) return;

  if (request.mode === 'navigate') {
    event.respondWith(handlePage(event, request));
    return;
  }
  if (isAsset(path)) {
    event.respondWith(handleAsset(event, request));
  }
  // Anything else falls through untouched — browser default behaviour.
});
