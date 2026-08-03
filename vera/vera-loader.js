/**
 * vera-loader.js — configurable loading animation
 * ================================================
 * Replaces the plain CSS spinner with a configurable animation. The default is
 * an evolving graph of nodes and edges that drift, connect, and fade in/out.
 *
 * How it works
 *   • Auto-upgrades any `.vera-loading` overlay: the inner `.vera-spinner` is
 *     swapped for the configured animation (the "loading…" label is kept).
 *   • A MutationObserver picks up overlays added later (lazy iframes, widgets).
 *   • One shared requestAnimationFrame ticker drives every live loader and
 *     pauses when the tab is hidden or the overlay leaves the DOM — cheap.
 *   • Config is read synchronously from localStorage `vera:ui:loader` for an
 *     instant first paint, then reconciled from `/ui/loader` in the background.
 *
 * Public API (window.VeraLoader)
 *   .mount(container, opts) → controller{stop()}   render into any element
 *   .setConfig(cfg)                                 apply + broadcast + persist
 *   .getConfig()                                    current config
 *   .ANIMATIONS                                     { id: {label, draw} }
 *
 * Add an animation: push into ANIMATIONS with a `factory(canvas, ctx, cfg)`
 * returning a `frame(t)` function. That's the whole seam.
 */
(function () {
  'use strict';
  if (window.VeraLoader) return;                       // singleton per document
  var BASE = window.location.origin;
  var CACHE_KEY = 'vera:ui:loader';

  // ── config ────────────────────────────────────────────────────────────────
  var DEFAULT_CFG = { type: 'graph', speed: 1, density: 1, sprite: null };
  var _cfg = readCache();

  function readCache() {
    try {
      var raw = localStorage.getItem(CACHE_KEY);
      if (raw) return Object.assign({}, DEFAULT_CFG, JSON.parse(raw));
    } catch (e) {}
    return Object.assign({}, DEFAULT_CFG);
  }
  function writeCache(cfg) { try { localStorage.setItem(CACHE_KEY, JSON.stringify(cfg)); } catch (e) {} }

  // ── theme colour resolution (reads the panel's live CSS vars) ──────────────
  // The returned object is *live*: it is registered and, on a theme change, its
  // fields are recomputed IN PLACE so running animations (which captured it once
  // in their factory) repaint in the new theme without being torn down.
  var _liveCols = [];
  function _resolveColors(el) {
    var cs = getComputedStyle(el || document.documentElement);
    function pick() {
      for (var i = 0; i < arguments.length; i++) {
        var v = cs.getPropertyValue(arguments[i]).trim();
        if (v) return v;
      }
      return '';
    }
    return {
      node: pick('--acc', '--ac', '--accent') || '#5a9e8f',
      node2: pick('--acc2', '--ac2', '--ok') || pick('--acc', '--ac') || '#8fb87a',
      edge: pick('--acc', '--ac', '--accent') || '#5a9e8f',
      dim: pick('--dim2', '--t2', '--fg2') || '#6b7585'
    };
  }
  function themeColors(el) {
    var target = el || document.documentElement;
    var obj = _resolveColors(target);
    _liveCols.push({ el: target, obj: obj });
    return obj;
  }
  var _repaintT = null;
  function repaintThemes() {
    for (var i = _liveCols.length - 1; i >= 0; i--) {
      var e = _liveCols[i];
      // drop entries whose element left the DOM (loader removed)
      if (e.el !== document.documentElement && e.el && !e.el.isConnected) { _liveCols.splice(i, 1); continue; }
      var fresh = _resolveColors(e.el);
      e.obj.node = fresh.node; e.obj.node2 = fresh.node2; e.obj.edge = fresh.edge; e.obj.dim = fresh.dim;
    }
  }
  function scheduleRepaint() {
    if (_repaintT) return;
    // debounce: applyVars() sets dozens of properties in a burst
    _repaintT = setTimeout(function () { _repaintT = null; repaintThemes(); }, 60);
  }
  // Watch for theme switches (data-theme flip) and inline var updates (applyVars
  // writes CSS custom properties onto <html>'s style attribute).
  try {
    new MutationObserver(scheduleRepaint).observe(document.documentElement, {
      attributes: true, attributeFilter: ['data-theme', 'style']
    });
    // Cross-panel broadcast some shells emit on theme change.
    window.addEventListener('message', function (e) {
      var d = e && e.data; if (d && (d.type === 'vera:theme' || d.type === 'theme')) scheduleRepaint();
    });
  } catch (e) {}

  // ── shared ticker: one RAF for every live loader ───────────────────────────
  var _frames = [];               // {fn, canvas}
  var _running = false;
  function _tick(now) {
    _running = false;
    if (document.hidden) { return schedule(); }   // parked while hidden
    for (var i = _frames.length - 1; i >= 0; i--) {
      var f = _frames[i];
      if (!f.canvas.isConnected) { _frames.splice(i, 1); continue; }  // auto-clean
      try { f.fn(now); } catch (e) { _frames.splice(i, 1); }
    }
    if (_frames.length) schedule();
  }
  function schedule() {
    if (_running || !_frames.length) return;
    _running = true;
    requestAnimationFrame(_tick);
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) schedule(); });

  // ── canvas helper (DPR-capped for perf) ────────────────────────────────────
  function makeCanvas(container, w, h) {
    var dpr = Math.min(window.devicePixelRatio || 1, 1.6);
    var cv = document.createElement('canvas');
    cv.style.cssText = 'display:block;width:' + w + 'px;height:' + h + 'px';
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    var ctx = cv.getContext('2d');
    ctx.scale(dpr, dpr);
    cv._w = w; cv._h = h;
    return { cv: cv, ctx: ctx };
  }
  function hexA(hex, a) {
    // #rgb / #rrggbb → rgba() with alpha
    var h = (hex || '').trim().replace('#', '');
    if (h.length === 3) h = h.replace(/(.)/g, '$1$1');
    var r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
    if (isNaN(r) || isNaN(g) || isNaN(b)) { r = 90; g = 158; b = 143; }
    return 'rgba(' + r + ',' + g + ',' + b + ',' + a + ')';
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  ANIMATIONS — each is factory(canvas, ctx, cfg, container) → frame(now)
  // ═══════════════════════════════════════════════════════════════════════════
  var ANIMATIONS = {

    // Default: an evolving graph — nodes drift, edges form between nearby nodes,
    // and both nodes and edges fade in and out; spent nodes respawn elsewhere.
    graph: {
      label: 'Evolving graph',
      factory: function (cv, ctx, cfg, container) {
        var W = cv._w, H = cv._h;
        var col = themeColors(container);
        var speed = 0.5 * (cfg.speed || 1);
        var n = Math.round((10 + Math.random() * 4) * (cfg.density || 1));
        n = Math.max(6, Math.min(24, n));
        var LINK = Math.min(W, H) * 0.62;               // connect radius
        var nodes = [];
        function spawn(seed) {
          return {
            x: Math.random() * W, y: Math.random() * H,
            vx: (Math.random() - 0.5) * 0.35 * speed,
            vy: (Math.random() - 0.5) * 0.35 * speed,
            r: 1.6 + Math.random() * 2.2,
            a: seed ? Math.random() : 0,                 // current alpha
            ta: 0.35 + Math.random() * 0.65,             // target alpha
            // random lifecycle so fades feel organic, not synced
            hold: 40 + Math.random() * 160
          };
        }
        for (var i = 0; i < n; i++) nodes.push(spawn(true));
        var last = 0;
        return function frame(now) {
          var dt = last ? Math.min(2, (now - last) / 16.67) : 1; last = now;
          ctx.clearRect(0, 0, W, H);

          // update
          for (var i = 0; i < nodes.length; i++) {
            var p = nodes[i];
            p.x += p.vx * dt; p.y += p.vy * dt;
            if (p.x < 0 || p.x > W) p.vx *= -1;
            if (p.y < 0 || p.y > H) p.vy *= -1;
            p.x = Math.max(0, Math.min(W, p.x));
            p.y = Math.max(0, Math.min(H, p.y));
            // ease toward target alpha; when a hold expires, retarget — and if
            // faded out, respawn elsewhere (node "transforms")
            p.a += (p.ta - p.a) * 0.03 * dt;
            p.hold -= dt;
            if (p.hold <= 0) {
              p.hold = 40 + Math.random() * 160;
              if (p.a < 0.25 && Math.random() < 0.5) { nodes[i] = spawn(false); nodes[i].ta = 0.4 + Math.random() * 0.6; }
              else p.ta = Math.random() < 0.25 ? 0.12 + Math.random() * 0.2 : 0.5 + Math.random() * 0.5;
            }
          }

          // edges (behind nodes) — opacity from both endpoints' alpha + distance
          ctx.lineWidth = 1;
          for (var a = 0; a < nodes.length; a++) {
            for (var b = a + 1; b < nodes.length; b++) {
              var p1 = nodes[a], p2 = nodes[b];
              var dx = p1.x - p2.x, dy = p1.y - p2.y;
              var d = Math.sqrt(dx * dx + dy * dy);
              if (d > LINK) continue;
              var ea = (1 - d / LINK) * Math.min(p1.a, p2.a) * 0.55;
              if (ea < 0.02) continue;
              ctx.strokeStyle = hexA(col.edge, ea);
              ctx.beginPath(); ctx.moveTo(p1.x, p1.y); ctx.lineTo(p2.x, p2.y); ctx.stroke();
            }
          }

          // nodes with a soft glow
          ctx.shadowBlur = 6;
          for (var k = 0; k < nodes.length; k++) {
            var q = nodes[k];
            var c = k % 3 === 0 ? col.node2 : col.node;
            ctx.shadowColor = hexA(c, Math.min(0.9, q.a));
            ctx.fillStyle = hexA(c, Math.min(1, q.a));
            ctx.beginPath(); ctx.arc(q.x, q.y, q.r, 0, 6.2832); ctx.fill();
          }
          ctx.shadowBlur = 0;
        };
      }
    },

    // Concentric pulse rings.
    pulse: {
      label: 'Pulse rings',
      factory: function (cv, ctx, cfg, container) {
        var W = cv._w, H = cv._h, cx = W / 2, cy = H / 2;
        var col = themeColors(container);
        var R = Math.min(W, H) / 2 - 2;
        var speed = 0.0016 * (cfg.speed || 1);
        var rings = 3;
        return function frame(now) {
          ctx.clearRect(0, 0, W, H);
          for (var i = 0; i < rings; i++) {
            var t = ((now * speed) + i / rings) % 1;
            var r = t * R, a = (1 - t) * 0.8;
            ctx.strokeStyle = hexA(col.node, a); ctx.lineWidth = 2;
            ctx.beginPath(); ctx.arc(cx, cy, r, 0, 6.2832); ctx.stroke();
          }
          ctx.fillStyle = hexA(col.node2, 0.9);
          ctx.beginPath(); ctx.arc(cx, cy, 2.4, 0, 6.2832); ctx.fill();
        };
      }
    },

    // Dots orbiting a centre.
    orbit: {
      label: 'Orbit',
      factory: function (cv, ctx, cfg, container) {
        var W = cv._w, H = cv._h, cx = W / 2, cy = H / 2;
        var col = themeColors(container);
        var R = Math.min(W, H) / 2 - 6, dots = 3, speed = 0.0022 * (cfg.speed || 1);
        return function frame(now) {
          ctx.clearRect(0, 0, W, H);
          for (var i = 0; i < dots; i++) {
            var ang = now * speed + (i * 6.2832 / dots);
            var x = cx + Math.cos(ang) * R, y = cy + Math.sin(ang) * R;
            ctx.shadowBlur = 8; ctx.shadowColor = hexA(col.node, 0.8);
            ctx.fillStyle = hexA(i ? col.node : col.node2, 0.95);
            ctx.beginPath(); ctx.arc(x, y, 3.2, 0, 6.2832); ctx.fill();
          }
          ctx.shadowBlur = 0;
        };
      }
    }
  };

  // ── mount an animation into a container ────────────────────────────────────
  function mount(container, opts) {
    opts = opts || {};
    var cfg = Object.assign({}, _cfg, opts);
    var type = cfg.type || 'graph';

    // Sprite: a user-supplied sheet (data: URI recommended under CSP).
    if (type === 'sprite' && cfg.sprite && cfg.sprite.url) return mountSprite(container, cfg);
    // Plain spinner: leave the CSS spinner in place (no canvas).
    if (type === 'spinner' || !ANIMATIONS[type]) return { el: null, stop: function () {} };

    var w = opts.w || 148, h = opts.h || 96;
    var made = makeCanvas(container, w, h);
    container.appendChild(made.cv);
    var frame = ANIMATIONS[type].factory(made.cv, made.ctx, cfg, container);
    _frames.push({ fn: frame, canvas: made.cv });
    schedule();
    return {
      el: made.cv,
      stop: function () {
        for (var i = _frames.length - 1; i >= 0; i--) if (_frames[i].canvas === made.cv) _frames.splice(i, 1);
        if (made.cv.parentNode) made.cv.parentNode.removeChild(made.cv);
      }
    };
  }

  function mountSprite(container, cfg) {
    var s = cfg.sprite, fps = s.fps || 12, fw = s.w || 48, fh = s.h || 48, frames = s.frames || 1;
    var el = document.createElement('div');
    el.style.cssText = 'width:' + fw + 'px;height:' + fh + 'px;image-rendering:pixelated;' +
      'background:url(' + s.url + ') 0 0/' + (fw * frames) + 'px ' + fh + 'px no-repeat';
    container.appendChild(el);
    var i = 0, last = 0, step = 1000 / fps;
    var fn = function (now) {
      if (now - last < step) return; last = now;
      i = (i + 1) % frames;
      el.style.backgroundPositionX = '-' + (i * fw) + 'px';
    };
    _frames.push({ fn: fn, canvas: el }); schedule();
    return { el: el, stop: function () { if (el.parentNode) el.parentNode.removeChild(el); } };
  }

  // ── auto-upgrade .vera-loading overlays ────────────────────────────────────
  function upgrade(root) {
    var scope = root && root.querySelectorAll ? root : document;
    var overlays = scope.querySelectorAll ? scope.querySelectorAll('.vera-loading:not([data-vera-loader])') : [];
    for (var i = 0; i < overlays.length; i++) upgradeOne(overlays[i]);
    // the root itself might be a .vera-loading
    if (root && root.classList && root.classList.contains('vera-loading') && !root.hasAttribute('data-vera-loader'))
      upgradeOne(root);
  }
  function upgradeOne(ov) {
    if ((_cfg.type || 'graph') === 'spinner') return;    // user chose the plain spinner
    ov.setAttribute('data-vera-loader', _cfg.type || 'graph');
    var spinner = ov.querySelector('.vera-spinner');
    var host = document.createElement('div');
    host.className = 'vera-loader-host';
    host.style.cssText = 'display:flex;align-items:center;justify-content:center';
    if (spinner) spinner.replaceWith(host); else ov.insertBefore(host, ov.firstChild);
    mount(host, {});
  }

  var _mo = null;
  function startObserver() {
    if (_mo) return;
    _mo = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        var added = muts[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          var nd = added[j];
          if (nd.nodeType !== 1) continue;
          if (nd.classList && nd.classList.contains('vera-loading')) upgradeOne(nd);
          else if (nd.querySelector && nd.querySelector('.vera-loading')) upgrade(nd);
        }
      }
    });
    _mo.observe(document.body || document.documentElement, { childList: true, subtree: true });
  }

  // ── config API ─────────────────────────────────────────────────────────────
  function setConfig(cfg) {
    _cfg = Object.assign({}, DEFAULT_CFG, cfg || {});
    writeCache(_cfg);
    // Persist server-side + broadcast so other panels pick it up (best effort).
    fetch(BASE + '/ui/loader/set', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_cfg)
    }).catch(function () {});
    try { window.parent.postMessage({ type: 'vera:loader', config: _cfg }, '*'); } catch (e) {}
  }
  function getConfig() { return Object.assign({}, _cfg); }

  // Cross-panel sync: adopt a sibling/parent's loader change live.
  window.addEventListener('message', function (e) {
    if (e.data && e.data.type === 'vera:loader' && e.data.config) {
      _cfg = Object.assign({}, DEFAULT_CFG, e.data.config); writeCache(_cfg);
    }
  });

  // Background reconcile with the server (custom sprite, cross-device default).
  fetch(BASE + '/ui/loader').then(function (r) { return r.json(); }).then(function (d) {
    if (d && d.config && d.config.type) { _cfg = Object.assign({}, DEFAULT_CFG, d.config); writeCache(_cfg); }
  }).catch(function () {});

  window.VeraLoader = {
    mount: mount, upgrade: upgrade, setConfig: setConfig, getConfig: getConfig,
    ANIMATIONS: ANIMATIONS
  };

  function _init() { upgrade(document); startObserver(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _init);
  else _init();
})();
