/* Vera PWA client — served at /ui/pwa/pwa.js
 *
 * Registers the service worker, keeps <meta name="theme-color"> in step with
 * whatever Vera theme is actually painted, and offers an unobtrusive
 * install / update pill. Everything here is best-effort: a browser with no
 * service-worker support, or an origin the browser doesn't consider secure
 * (a self-signed cert the user clicked through), degrades to exactly the
 * behaviour Vera had before — the page still works, nothing throws.
 *
 * Public API: window.veraPWA.{install, update, unregister, clearCaches, status}
 */
(function () {
  'use strict';
  if (window.__VERA_PWA_LOADED__) return;
  window.__VERA_PWA_LOADED__ = true;

  var TOP = window.top === window.self;
  var DISMISS_KEY = 'vera:pwa:installDismissed';
  var DISMISS_DAYS = 30;

  var state = {
    supported: 'serviceWorker' in navigator,
    secureContext: !!window.isSecureContext,
    registered: false,
    controlled: !!(navigator.serviceWorker && navigator.serviceWorker.controller),
    installable: false,
    installed: window.matchMedia
      ? window.matchMedia('(display-mode: standalone)').matches
      : false,
    updateReady: false,
    version: '',
    error: '',
  };
  window.__VERA_PWA__ = state;

  var deferredPrompt = null;
  var registration = null;

  // ── theme-color ──────────────────────────────────────────────────────────
  // Vera repaints its CSS vars on every theme change; mirroring --bg0 into
  // theme-color is what makes the standalone window's title bar and the
  // mobile status bar match the theme instead of a colour frozen at build.
  function syncThemeColor() {
    try {
      var css = getComputedStyle(document.documentElement);
      var color = (css.getPropertyValue('--bg0') || '').trim()
               || (css.getPropertyValue('--bg1') || '').trim();
      if (!color) return;
      var meta = document.querySelector('meta[name="theme-color"]');
      if (!meta) {
        meta = document.createElement('meta');
        meta.setAttribute('name', 'theme-color');
        document.head.appendChild(meta);
      }
      if (meta.getAttribute('content') !== color) {
        meta.setAttribute('content', color);
      }
    } catch (e) { /* never let cosmetics break the page */ }
  }

  function watchTheme() {
    syncThemeColor();
    try {
      new MutationObserver(syncThemeColor).observe(document.documentElement, {
        attributes: true, attributeFilter: ['data-theme', 'style'],
      });
    } catch (e) { /* no observer: the initial sync still applied */ }
    window.addEventListener('message', function (ev) {
      if (ev && ev.data && ev.data.type === 'vera:theme') {
        setTimeout(syncThemeColor, 50);
      }
    });
  }

  // ── the pill ─────────────────────────────────────────────────────────────

  function dismissedRecently() {
    try {
      var at = parseInt(localStorage.getItem(DISMISS_KEY) || '0', 10);
      return at > 0 && (Date.now() - at) < DISMISS_DAYS * 864e5;
    } catch (e) { return false; }
  }

  function removePill() {
    var el = document.getElementById('vera-pwa-pill');
    if (el && el.parentNode) el.parentNode.removeChild(el);
  }

  function showPill(label, hint, onClick, dismissible) {
    if (!TOP || !document.body) return;
    removePill();
    var pill = document.createElement('div');
    pill.id = 'vera-pwa-pill';
    pill.setAttribute('role', 'status');
    pill.style.cssText = [
      'position:fixed', 'right:14px', 'bottom:14px', 'z-index:2147483000',
      'display:flex', 'align-items:center', 'gap:8px',
      'padding:7px 8px 7px 12px', 'max-width:min(360px,calc(100vw - 28px))',
      'font-family:var(--sans,system-ui,sans-serif)', 'font-size:12px',
      'color:var(--text,#ddd5c8)', 'background:var(--bg2,#272421)',
      'border:1px solid var(--border,#3a3530)', 'border-radius:999px',
      'box-shadow:0 4px 18px rgba(0,0,0,.35)',
    ].join(';');

    var text = document.createElement('span');
    text.textContent = label;
    text.style.cssText = 'white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
    if (hint) text.title = hint;
    pill.appendChild(text);

    var act = document.createElement('button');
    act.type = 'button';
    act.textContent = hint || 'Install';
    act.style.cssText = [
      'cursor:pointer', 'border:none', 'border-radius:999px',
      'padding:5px 12px', 'font:inherit', 'font-weight:600',
      'background:var(--acc,#5a9e8f)', 'color:var(--bg0,#181614)',
    ].join(';');
    act.addEventListener('click', function () { onClick(); });
    pill.appendChild(act);

    if (dismissible) {
      var close = document.createElement('button');
      close.type = 'button';
      close.setAttribute('aria-label', 'Dismiss');
      close.textContent = '×';
      close.style.cssText = [
        'cursor:pointer', 'border:none', 'background:transparent',
        'color:var(--dim2,#8a7e70)', 'font-size:16px', 'line-height:1',
        'padding:2px 6px',
      ].join(';');
      close.addEventListener('click', function () {
        try { localStorage.setItem(DISMISS_KEY, String(Date.now())); } catch (e) {}
        removePill();
      });
      pill.appendChild(close);
    }
    document.body.appendChild(pill);
  }

  function offerInstall() {
    if (!TOP || state.installed || dismissedRecently()) return;
    showPill('Install Vera as an app', 'Install', function () {
      api.install();
    }, true);
  }

  function offerUpdate() {
    if (!TOP) return;
    showPill('A new version of Vera is ready', 'Reload', function () {
      api.update();
    }, true);
  }

  // ── service worker ───────────────────────────────────────────────────────

  function watchWorker(reg) {
    registration = reg;
    state.registered = true;
    // A worker already waiting means an update landed in a previous visit.
    if (reg.waiting && navigator.serviceWorker.controller) {
      state.updateReady = true;
      offerUpdate();
    }
    reg.addEventListener('updatefound', function () {
      var incoming = reg.installing;
      if (!incoming) return;
      incoming.addEventListener('statechange', function () {
        // Only an update, not the very first install (no controller yet).
        if (incoming.state === 'installed' && navigator.serviceWorker.controller) {
          state.updateReady = true;
          offerUpdate();
        }
      });
    });
  }

  function registerWorker() {
    if (!state.supported) { state.error = 'service workers unsupported'; return; }
    if (!state.secureContext) {
      // Chrome treats an https origin whose certificate the user bypassed as
      // insecure, so this is the expected outcome on a self-signed LAN cert
      // until that certificate is trusted. Surfaced, not hidden.
      state.error = 'insecure context — trust the TLS certificate to install';
      return;
    }
    navigator.serviceWorker.register('/sw.js', { scope: '/' })
      .then(function (reg) {
        watchWorker(reg);
        return navigator.serviceWorker.ready;
      })
      .then(function () {
        var ctrl = navigator.serviceWorker.controller;
        state.controlled = !!ctrl;
        if (ctrl) {
          var ch = new MessageChannel();
          ch.port1.onmessage = function (ev) {
            if (ev.data && ev.data.version) state.version = ev.data.version;
          };
          ctrl.postMessage({ type: 'VERA_PWA_VERSION' }, [ch.port2]);
        }
      })
      .catch(function (err) {
        state.error = String((err && err.message) || err);
      });

    var reloading = false;
    navigator.serviceWorker.addEventListener('controllerchange', function () {
      // Only reload for an update the user asked for, never on first install.
      if (!state.updateReady || reloading) return;
      reloading = true;
      window.location.reload();
    });
  }

  // ── public API ───────────────────────────────────────────────────────────

  var api = {
    status: function () {
      return JSON.parse(JSON.stringify(state));
    },
    install: function () {
      if (!deferredPrompt) return Promise.resolve({ ok: false, reason: 'no prompt available' });
      var prompt = deferredPrompt;
      deferredPrompt = null;
      state.installable = false;
      prompt.prompt();
      return prompt.userChoice.then(function (choice) {
        if (choice && choice.outcome === 'accepted') removePill();
        return { ok: true, outcome: choice && choice.outcome };
      });
    },
    update: function () {
      if (registration && registration.waiting) {
        registration.waiting.postMessage({ type: 'VERA_PWA_SKIP_WAITING' });
        return; // controllerchange reloads us
      }
      window.location.reload();
    },
    checkForUpdate: function () {
      return registration ? registration.update() : Promise.resolve();
    },
    unregister: function () {
      if (!navigator.serviceWorker) return Promise.resolve(false);
      return navigator.serviceWorker.getRegistrations().then(function (regs) {
        return Promise.all(regs.map(function (r) { return r.unregister(); }));
      }).then(function () { state.registered = false; return true; });
    },
    clearCaches: function () {
      if (!window.caches) return Promise.resolve(0);
      return caches.keys().then(function (keys) {
        var mine = keys.filter(function (k) { return k.indexOf('vera-pwa-') === 0; });
        return Promise.all(mine.map(function (k) { return caches.delete(k); }))
          .then(function () { return mine.length; });
      });
    },
  };
  window.veraPWA = api;

  // ── wire up ──────────────────────────────────────────────────────────────

  window.addEventListener('beforeinstallprompt', function (ev) {
    ev.preventDefault();          // suppress the browser's own mini-infobar
    deferredPrompt = ev;
    state.installable = true;
    try {
      window.dispatchEvent(new CustomEvent('vera:pwa:installable'));
    } catch (e) { /* older browsers */ }
    offerInstall();
  });

  window.addEventListener('appinstalled', function () {
    state.installed = true;
    state.installable = false;
    deferredPrompt = null;
    removePill();
  });

  function boot() {
    watchTheme();
    registerWorker();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
