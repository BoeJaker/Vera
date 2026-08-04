/* ============================================================================
 * vera-dashboard.js  —  VeraDash: one reusable dashboard widget-grid framework
 * ============================================================================
 * Vera had three near-identical dashboards (main, workers/ollama, dream), each
 * with its own copy of the grid framework. This module is the single shared
 * implementation, combining the best of each:
 *   • workers/ollama: drag + resize ONLY in edit mode (so widget text stays
 *     selectable — "lock movement"), hide/show picker, and the widget loader.
 *   • main dashboard: pop-out (float on the page + open in a new window).
 *
 * Usage (per grid):
 *     const ctl = VeraDash.init(gridEl, {
 *       key:    'main',          // localStorage namespace → vera.dash.<key>
 *       loader: true,            // "+ Add Widget" picker + dynamic widgets
 *       popout: true,            // float + new-window for every widget
 *       editBtn:'dashEditBtn',   // (optional) toolbar Configure button id
 *       hiddenPicker:'...',      // (optional) container shown while editing
 *       hiddenChips:'...'        // (optional) where restore-chips render
 *     });
 *     // ctl.toggleEdit() / ctl.reset() / ctl.openLoader() / ctl.hide(wid) /
 *     // ctl.show(wid) / ctl.addWidget(panelId)  — wire toolbar buttons to these.
 *
 * No shadow DOM: widgets stay in the page so existing getElementById-based
 * content-update code keeps working. The only widget that ever leaves the grid
 * is a floated one (reparented to <body> so it survives top-level tab switches);
 * a same-span placeholder holds its slot until it docks.
 *
 * Relies on each page's existing .dash-grid / .widget / .w-x CSS + the w-wN and
 * w-hN span classes; injects only its own CSS for the loader modal, the float,
 * and the pop buttons.
 * Reuses GET /ui/panel/list, /ui/panel/get and /ui/panel/window.
 * ========================================================================== */
(function () {
  'use strict';
  if (window.VeraDash) return;

  var esc = function (s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  };

  // Embedded = this dashboard lives inside a harness tab iframe (dream, workers
  // & ollama, …). A float reparented to OUR body would vanish the moment the
  // user switches top-level tabs (the whole iframe is display:none'd), so the
  // pop-out is escalated to the harness instead — see soloRequest()/_soloBoot().
  var EMBEDDED = (function () {
    try { return window.self !== window.top; } catch (e) { return true; }
  })();

  /* ── injected CSS (once) ─────────────────────────────────────────────── */
  function injectCSS() {
    if (document.getElementById('vera-dash-css')) return;
    var s = document.createElement('style');
    s.id = 'vera-dash-css';
    s.textContent = [
      // Self-contained so a floated widget still looks right when reparented to
      // <body>, outside any page that scopes .widget/.w-* CSS (e.g. dream's
      // "#sec-overview .widget"). Widgets with inline padding:0 (.w-body.flush)
      // keep it — inline style outranks this rule.
      '.widget.floating{position:fixed;z-index:9000;resize:both;overflow:hidden;',
      'min-width:280px;min-height:180px;display:flex;flex-direction:column;',
      'border:1px solid var(--acc);border-radius:var(--radius-lg,8px);',
      'box-shadow:0 18px 60px rgba(0,0,0,.65);background:var(--bg2)}',
      '.widget.floating .w-head{display:flex;align-items:center;gap:6px;',
      'padding:7px 10px 6px;border-bottom:1px solid var(--border);flex-shrink:0;cursor:move}',
      '.widget.floating .w-body{flex:1;min-height:0;overflow:auto;padding:9px 11px}',
      '.widget.floating .w-actions{opacity:1}',
      '.w-iconbtn.vd-pop.on{color:var(--acc)}',
      '.vd-wl-overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;',
      'align-items:center;justify-content:center;z-index:9500;backdrop-filter:blur(2px)}',
      '.vd-wl-box{background:var(--bg1);border:1px solid var(--border2);',
      'border-radius:var(--radius-lg,8px);width:520px;max-width:96%;max-height:80%;',
      'display:flex;flex-direction:column;box-shadow:0 8px 40px rgba(0,0,0,.5)}',
      '.vd-wl-head{display:flex;align-items:center;gap:8px;padding:10px 14px;',
      'border-bottom:1px solid var(--border)}',
      '.vd-wl-head input{flex:1;background:var(--bg0);border:1px solid var(--border2);',
      'color:var(--text);padding:5px 9px;border-radius:3px;font-family:var(--mono);font-size:11px}',
      '.vd-wl-list{flex:1;overflow-y:auto;padding:6px}',
      '.vd-wl-list .wl-item{display:flex;align-items:center;gap:8px;padding:7px 10px;',
      'border-radius:3px;cursor:pointer;transition:.12s;border:1px solid transparent}',
      '.vd-wl-list .wl-item:hover{background:var(--bg2);border-color:var(--border)}',
      '.vd-wl-list .wl-item-icon{font-size:14px;width:22px;text-align:center;flex-shrink:0}',
      '.vd-wl-list .wl-item-label{font-family:var(--mono);font-size:10.5px;font-weight:600;color:var(--text);flex:1}',
      '.vd-wl-list .wl-item-id{font-size:8.5px;color:var(--dim2);font-family:var(--mono)}',
      '.vd-wl-list .wl-item-mode{font-size:7.5px;padding:1px 5px;border-radius:3px;',
      'background:var(--bg0);border:1px solid var(--border);color:var(--dim2);font-family:var(--mono)}',
      '.vd-chip{display:inline-block;font-size:9.5px;padding:3px 8px;border-radius:11px;',
      'border:1px solid var(--border);background:var(--bg2);color:var(--dim2);cursor:pointer;',
      'user-select:none;font-family:var(--mono);margin:2px}',
      '.vd-chip:hover{border-color:var(--acc);color:var(--acc)}',
      // Solo mode: this page was loaded (in a harness float) just to show ONE
      // widget — hide everything else and let the widget fill the viewport.
      // Self-contained styling like .widget.floating, since page CSS may scope
      // .widget rules under a section id.
      'html.vd-solo-mode body>*:not(.vd-solo){display:none!important}',
      'html.vd-solo-mode body>.widget.vd-solo{display:flex!important;position:fixed;inset:0;',
      'width:auto!important;height:auto!important;flex-direction:column;z-index:9999;',
      'margin:0;border:none;border-radius:0;background:var(--bg1,#16181d)}',
      'html.vd-solo-mode .widget.vd-solo .w-head{display:flex;align-items:center;gap:6px;',
      'padding:7px 10px 6px;border-bottom:1px solid var(--border);flex-shrink:0}',
      'html.vd-solo-mode .widget.vd-solo .w-body{flex:1;min-height:0;overflow:auto;padding:9px 11px}',
      'html.vd-solo-mode .widget.vd-solo .w-actions{display:none}',
      'html.vd-solo-mode .widget.vd-solo .w-resize{display:none}',
      // Loading overlay for widget iframes while they boot (self-contained so it
      // works even on pages that don't include vera-panel.css).
      '.vera-loading{position:absolute;inset:0;z-index:40;display:flex;align-items:center;',
      'justify-content:center;gap:10px;background:color-mix(in srgb,var(--bg1,#16181d) 60%,transparent);',
      'backdrop-filter:blur(1px)}',
      '.vera-spinner{width:22px;height:22px;border-radius:50%;flex-shrink:0;',
      'border:2.5px solid var(--border2,#333);border-top-color:var(--acc,#5a9e8f);',
      'animation:veraSpin .7s linear infinite}',
      '@keyframes veraSpin{to{transform:rotate(360deg)}}'
    ].join('');
    document.head.appendChild(s);
  }

  /* ── solo boot: #vd-solo=<wid> shows exactly one widget, fullscreen ────── */
  // The harness opens THIS page in a floating iframe with that hash when an
  // embedded dashboard's widget is popped out. The widget is reparented to
  // <body> (escaping any display:none pane) but stays in the same document, so
  // the page's own polling/update JS keeps driving it.
  function _soloBoot() {
    var m = /(?:^|[#&])vd-solo=([^&]+)/.exec(window.location.hash || '');
    if (!m) return;
    var wid = decodeURIComponent(m[1]);
    var tries = 0;
    (function find() {
      var w = document.querySelector('.widget[data-wid="' + wid + '"]');
      if (!w) {
        if (++tries < 150) return setTimeout(find, 100);   // dynamic widgets restore late
        return;
      }
      injectCSS();
      document.documentElement.classList.add('vd-solo-mode');
      document.body.appendChild(w);
      w.classList.add('vd-solo');
      w.classList.remove('hidden');
    })();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _soloBoot, { once: true });
  } else {
    _soloBoot();
  }

  /* ── shared "+ Add Widget" modal (one per document) ──────────────────── */
  var _modal = null, _modalCtl = null, _modalPanels = [];
  function ensureModal() {
    if (_modal) return;
    injectCSS();
    var o = document.createElement('div');
    o.className = 'vd-wl-overlay';
    o.innerHTML =
      '<div class="vd-wl-box"><div class="vd-wl-head">' +
      '<span style="font-family:var(--mono);font-size:10px;font-weight:700;color:var(--acc);letter-spacing:1px">ADD WIDGET</span>' +
      '<input class="vd-wl-search" placeholder="Search panels & elements…">' +
      '<button class="w-iconbtn vd-wl-close" style="cursor:pointer">✕</button></div>' +
      '<div class="vd-wl-list">Loading…</div></div>';
    document.body.appendChild(o);
    _modal = o;
    o.addEventListener('click', function (e) { if (e.target === o) closeModal(); });
    o.querySelector('.vd-wl-close').onclick = closeModal;
    o.querySelector('.vd-wl-search').addEventListener('input', function (e) { renderLoader(e.target.value); });
  }
  function closeModal() { if (_modal) _modal.style.display = 'none'; }
  function fetchPanels() {
    return fetch('/ui/panel/list', { headers: { Accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (d) { _modalPanels = (d && d.panels) || (Array.isArray(d) ? d : []); })
      .catch(function () { _modalPanels = []; });
  }
  function renderLoader(q) {
    var list = _modal.querySelector('.vd-wl-list');
    q = (q || '').toLowerCase();
    var f = _modalPanels.filter(function (p) {
      if ((p.mode || '') === 'tab') return false;             // only injectable elements
      if (q && (p.id || '').toLowerCase().indexOf(q) < 0 &&
        (p.label || '').toLowerCase().indexOf(q) < 0) return false;
      return true;
    }).sort(function (a, b) { return (a.tab_order || 100) - (b.tab_order || 100); });
    if (!f.length) {
      list.innerHTML = '<div style="font-size:10px;color:var(--dim);padding:10px">' +
        (q ? 'No panels match "' + esc(q) + '"' : 'No panels registered') + '</div>';
      return;
    }
    list.innerHTML = f.map(function (p) {
      return '<div class="wl-item" data-id="' + esc(p.id) + '">' +
        '<span class="wl-item-icon">' + (p.icon || '▣') + '</span>' +
        '<span class="wl-item-label">' + esc(p.label || p.id) + '</span>' +
        '<span class="wl-item-id">' + esc(p.id) + '</span>' +
        '<span class="wl-item-mode">' + esc(p.mode || 'tab') + '</span></div>';
    }).join('');
    list.querySelectorAll('.wl-item').forEach(function (it) {
      it.onclick = function () { var id = it.dataset.id; closeModal(); if (_modalCtl) _modalCtl.addWidget(id); };
    });
  }

  // Wrap a panel's fragment html+js in a self-contained iframe doc (theme via
  // vera-ui.js + an api() shim) — same as the original widget loaders.
  function buildDoc(full) {
    var body = (full && full.html) || '<div style="padding:10px;color:var(--dim,#888)">No content</div>';
    var js = (full && full.js) || '';
    return '<!doctype html><html><head><meta charset="utf-8">' +
      '<script src="/ui/vera-ui.js"><\/script>' +
      '<style>html,body{height:100%;margin:0;background:var(--bg1,#16181d);' +
      'color:var(--text,#d6d9df);font-family:var(--mono,ui-monospace,monospace);' +
      'font-size:11px}*{box-sizing:border-box}</style>' +
      '<script>window.BASE=window.location.origin;' +
      'window.base=function(){return window.location.origin;};' +
      'window.api=async function(path,method,body){method=method||"GET";' +
      'var opts={method:method,headers:{"Content-Type":"application/json"}};' +
      'if(body!=null)opts.body=JSON.stringify(body);' +
      'try{var r=await fetch(window.location.origin+path,opts);var t=await r.text();' +
      'return t&&t.trim()?JSON.parse(t):null;}catch(e){return null;}};<\/script>' +
      '</head><body>' + body +
      (js ? '<script>try{' + js + '\n}catch(e){console.error("[widget]",e);}<\/script>' : '') +
      '</body></html>';
  }

  /* ── one dashboard instance ──────────────────────────────────────────── */
  function init(grid, opts) {
    if (!grid) return null;
    if (grid._veraDash) return grid._veraDash;
    opts = opts || {};
    injectCSS();
    var key = opts.key || grid.id || 'default';
    var SKEY = 'vera.dash.' + key;
    var withLoader = opts.loader !== false;
    var withPopout = opts.popout !== false;
    var state = { order: [], hidden: new Set(), sizes: {}, dynamic: {}, editing: false };
    var dragSrc = null, fdrag = null;

    function widgets() { return Array.prototype.slice.call(grid.querySelectorAll(':scope > .widget')); }
    function byId(wid) { return widgets().filter(function (w) { return w.dataset.wid === wid; })[0]; }
    var $ = function (id) { return id ? document.getElementById(id) : null; };

    function save() {
      try {
        localStorage.setItem(SKEY, JSON.stringify({
          order: state.order, hidden: Array.from(state.hidden),
          sizes: state.sizes, dynamic: state.dynamic
        }));
      } catch (e) { /* private mode / quota */ }
    }
    function load() {
      try {
        var j = JSON.parse(localStorage.getItem(SKEY) || 'null');
        if (!j) return;
        state.order = Array.isArray(j.order) ? j.order : [];
        state.hidden = new Set(Array.isArray(j.hidden) ? j.hidden : []);
        state.sizes = (j.sizes && typeof j.sizes === 'object') ? j.sizes : {};
        state.dynamic = (j.dynamic && typeof j.dynamic === 'object') ? j.dynamic : {};
      } catch (e) { /* ignore */ }
    }

    /* drag-reorder (edit mode only → text stays selectable otherwise) */
    function onDragStart(e) {
      if (!state.editing) return;
      var w = e.target.closest('.widget'); if (!w || w.parentNode !== grid) return;
      dragSrc = w; w.classList.add('dragging');
      _scrollTarget = null;   // recomputed lazily by _autoScrollOnDrag below
      try { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', w.dataset.wid || ''); } catch (_) {}
    }

    // Auto-scroll while dragging near the top/bottom edge of the scrolling
    // container. Native HTML5 drag-and-drop is *supposed* to auto-scroll on
    // its own, but that only reliably works over the grid's own background —
    // every .widget has overflow:hidden, which makes browsers lose track of
    // the actual scrollable ancestor while the pointer is over a widget
    // (rather than the gaps between them), so this reimplements it manually,
    // independent of whatever element the dragover currently targets.
    var _scrollTarget = null;
    function _scrollableAncestor(el) {
      var node = el.parentElement;
      while (node && node !== document.body) {
        var st = getComputedStyle(node);
        if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && node.scrollHeight > node.clientHeight) return node;
        node = node.parentElement;
      }
      return document.scrollingElement || document.documentElement;
    }
    function _autoScrollOnDrag(e) {
      if (!state.editing || !dragSrc) return;
      if (!_scrollTarget) _scrollTarget = _scrollableAncestor(grid);
      var margin = 70, maxSpeed = 22;
      var r = _scrollTarget.getBoundingClientRect ? _scrollTarget.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
      var y = e.clientY, dy = 0;
      if (y < r.top + margin) dy = -maxSpeed * (1 - Math.max(0, y - r.top) / margin);
      else if (y > r.bottom - margin) dy = maxSpeed * (1 - Math.max(0, r.bottom - y) / margin);
      if (dy) _scrollTarget.scrollTop += dy;
    }
    document.addEventListener('dragover', _autoScrollOnDrag);
    function onDragOver(e) {
      if (!state.editing || !dragSrc) return;
      var w = e.target.closest('.widget'); if (!w || w === dragSrc || w.parentNode !== grid) return;
      e.preventDefault();
      grid.querySelectorAll('.widget.drag-over').forEach(function (x) { x.classList.remove('drag-over'); });
      w.classList.add('drag-over');
    }
    function onDragLeave(e) { var w = e.target.closest('.widget'); if (w) w.classList.remove('drag-over'); }
    function onDrop(e) {
      if (!state.editing || !dragSrc) return;
      var t = e.target.closest('.widget'); if (!t || t === dragSrc || t.parentNode !== grid) return;
      e.preventDefault();
      var r = t.getBoundingClientRect();
      grid.insertBefore(dragSrc, (e.clientX < r.left + r.width / 2) ? t : t.nextSibling);
      grid.querySelectorAll('.widget.drag-over').forEach(function (x) { x.classList.remove('drag-over'); });
      state.order = widgets().map(function (w) { return w.dataset.wid; }); save();
    }
    function onDragEnd() {
      if (dragSrc) dragSrc.classList.remove('dragging');
      grid.querySelectorAll('.widget.drag-over').forEach(function (x) { x.classList.remove('drag-over'); });
      dragSrc = null;
    }

    /* resize (edit mode only) — snap width to allowed spans */
    function onResizeDown(e) {
      if (!state.editing) return;
      var handle = e.target.closest('.w-resize'); if (!handle || !grid.contains(handle)) return;
      e.preventDefault(); e.stopPropagation();
      var w = handle.closest('.widget'); if (!w) return;
      var sx = e.clientX, sy = e.clientY;
      var cw = parseInt((w.className.match(/w-w(\d+)/) || [])[1] || '4', 10);
      var ch = parseInt((w.className.match(/w-h(\d+)/) || [])[1] || '1', 10);
      var allowed = [2, 3, 4, 6, 8, 12];
      function mv(ev) {
        var nw = Math.max(2, Math.min(12, cw + Math.round((ev.clientX - sx) / 80)));
        var nh = Math.max(1, Math.min(6, ch + Math.round((ev.clientY - sy) / 70)));
        var near = allowed[0], best = Infinity;
        allowed.forEach(function (a) { var d = Math.abs(a - nw); if (d < best) { best = d; near = a; } });
        nw = near;
        allowed.forEach(function (n) { w.classList.remove('w-w' + n); });
        [1, 2, 3, 4, 5, 6].forEach(function (n) { w.classList.remove('w-h' + n); });
        w.classList.add('w-w' + nw); w.classList.add('w-h' + nh);
        state.sizes[w.dataset.wid] = { w: nw, h: nh };
      }
      function up() { document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up); save(); }
      document.addEventListener('mousemove', mv); document.addEventListener('mouseup', up);
    }

    /* hide / show + restore-chip picker */
    function hide(wid) { state.hidden.add(wid); save(); applyLayout(); }
    function show(wid) { state.hidden.delete(wid); save(); applyLayout(); }
    function renderHidden() {
      var picker = $(opts.hiddenPicker), host = $(opts.hiddenChips);
      if (picker) picker.style.display = (state.hidden.size && state.editing) ? 'flex' : 'none';
      if (!host) return;
      if (!state.hidden.size || !state.editing) { host.innerHTML = ''; return; }
      host.innerHTML = Array.from(state.hidden).map(function (wid) {
        var w = byId(wid);
        var label = w ? ((w.querySelector('.w-title') || {}).textContent || wid) : wid;
        return '<span class="vd-chip" data-wid="' + esc(wid) + '">+ ' + esc(label) + '</span>';
      }).join('');
      host.querySelectorAll('.vd-chip').forEach(function (c) { c.onclick = function () { show(c.dataset.wid); }; });
    }

    /* apply persisted order (new widgets → end) + hidden + sizes */
    function applyLayout() {
      var ws = widgets(), map = {};
      ws.forEach(function (w) { map[w.dataset.wid] = w; });
      var seen = {}, ordered = [];
      state.order.forEach(function (id) { if (map[id] && !seen[id]) { ordered.push(map[id]); seen[id] = 1; } });
      ws.forEach(function (w) { if (!seen[w.dataset.wid]) ordered.push(w); });
      var need = false;
      for (var i = 0; i < ordered.length; i++) { if (grid.children[i] !== ordered[i]) { need = true; break; } }
      if (need) ordered.forEach(function (w) { grid.appendChild(w); });
      ws.forEach(function (w) {
        if (state.hidden.has(w.dataset.wid)) w.classList.add('hidden'); else w.classList.remove('hidden');
      });
      Object.keys(state.sizes).forEach(function (wid) {
        var w = map[wid]; if (!w) return; var sz = state.sizes[wid];
        if (sz.w) { [2, 3, 4, 6, 8, 12].forEach(function (n) { w.classList.remove('w-w' + n); }); w.classList.add('w-w' + sz.w); }
        if (sz.h) { [1, 2, 3, 4, 5, 6].forEach(function (n) { w.classList.remove('w-h' + n); }); w.classList.add('w-h' + sz.h); }
      });
      renderHidden();
    }

    function toggleEdit() {
      state.editing = !state.editing;
      grid.classList.toggle('editing', state.editing);
      widgets().forEach(function (w) { w.setAttribute('draggable', state.editing ? 'true' : 'false'); });
      var b = $(opts.editBtn);
      if (b) {
        b.textContent = state.editing ? '✓ Done' : '⚙ Configure';
        b.classList.toggle('primary', state.editing);   // main/workers active class
        b.classList.toggle('pri', state.editing);        // dream active class
      }
      renderHidden();
    }
    function reset() {
      if (!confirm('Reset dashboard layout to defaults?')) return;
      try { localStorage.removeItem(SKEY); } catch (e) {}
      state.order = []; state.hidden = new Set(); state.sizes = {};
      applyLayout();
    }

    /* ── pop-out: float on page (survives tab switch) + new window ─────── */
    function addPopButtons(w) {
      if (!withPopout) return;
      var act = w.querySelector('.w-actions'); if (!act || act.querySelector('.vd-pop')) return;
      var win = w.dataset.panel
        ? '<button class="w-iconbtn vd-popwin" title="Open in a new window">&#x29C9;</button>' : '';
      var popTitle = EMBEDDED
        ? 'Pop out (floats over the whole app, survives tab switches)'
        : 'Pop out (float on page)';
      act.insertAdjacentHTML('afterbegin',
        '<button class="w-iconbtn vd-pop" title="' + popTitle + '">&#x2922;</button>' + win);
      act.querySelector('.vd-pop').onclick = function () { popToggle(w); };
      var ww = act.querySelector('.vd-popwin'); if (ww) ww.onclick = function () { popWindow(w); };
    }
    function popToggle(w) {
      // Inside a harness tab iframe a local float would be hidden with the tab —
      // ask the harness to float a live solo view of this widget instead.
      if (EMBEDDED) return soloRequest(w);
      if (w.classList.contains('floating')) dock(w); else floatW(w);
    }
    function soloRequest(w) {
      var r = w.getBoundingClientRect();
      var title = ((w.querySelector('.w-title') || {}).textContent || w.dataset.wid || '').trim();
      // Instant-paint snapshot: send the widget's current markup + this page's
      // <style> blocks (the .widget/.w-head/.kpi-val/... rules live there, not
      // on the element itself) so the harness can render real content in the
      // float immediately via srcdoc — no second navigation, no poll. href is
      // kept only as a legacy fallback for a harness still running the old
      // vera-dashboard.js (rollout skew), which ignores html/css and falls
      // back to the old full-tab-reload + #vd-solo poll.
      var css = '';
      try {
        document.querySelectorAll('style').forEach(function (s) { css += s.textContent + '\n'; });
      } catch (e) {}
      try {
        window.parent.postMessage({
          type: 'vera.dash.solo',
          wid: w.dataset.wid,
          title: title,
          href: String(window.location.href),
          w: Math.max(320, Math.round(r.width)),
          h: Math.max(220, Math.round(r.height)),
          html: w.outerHTML,
          css: css
        }, '*');
      } catch (e) { /* sandboxed parent — leave the widget in place */ }
      _startSoloMirror(w);
    }

    // Keep a popped-out widget's floating mirror live without a second page
    // boot: watch the widget's own subtree (the SOURCE page's normal load*()
    // polling already mutates it on its usual cadence) and re-post a fresh
    // snapshot, debounced, whenever it changes. One observer per widget id —
    // cheap to leave running even after the float is closed on the harness
    // side, since an unanswered postMessage is a no-op there.
    var _soloMirrors = {};
    function _startSoloMirror(w) {
      var wid = w.dataset.wid;
      if (_soloMirrors[wid]) return;
      var t = null;
      var mo = new MutationObserver(function () {
        clearTimeout(t);
        t = setTimeout(function () {
          try {
            window.parent.postMessage({ type: 'vera.dash.solo.update', wid: wid, html: w.outerHTML }, '*');
          } catch (e) {}
        }, 150);
      });
      mo.observe(w, { childList: true, subtree: true, characterData: true, attributes: true });
      _soloMirrors[wid] = mo;
    }
    function floatW(w) {
      var r = w.getBoundingClientRect();
      var ph = document.createElement('div');
      ph.className = w.className.replace(/\bwidget\b/, '').trim();   // keep span classes, not '.widget'
      ph.setAttribute('data-vd-ph', w.dataset.wid); ph.style.visibility = 'hidden';
      w.parentNode.insertBefore(ph, w.nextSibling); w._vdPh = ph;
      w.classList.add('floating');
      w.style.left = Math.max(8, Math.min(window.innerWidth - r.width - 8, r.left)) + 'px';
      w.style.top = Math.max(8, r.top) + 'px';
      w.style.width = Math.max(300, r.width) + 'px';
      w.style.height = Math.max(200, r.height) + 'px';
      document.body.appendChild(w);   // escape the tab panel so the float survives tab switches
      var pb = w.querySelector('.vd-pop'); if (pb) { pb.classList.add('on'); pb.title = 'Dock back into the grid'; }
      w.querySelector('.w-head').addEventListener('mousedown', floatStart);
    }
    function dock(w) {
      w.querySelector('.w-head').removeEventListener('mousedown', floatStart);
      w.classList.remove('floating');
      w.style.left = w.style.top = w.style.width = w.style.height = '';
      var ph = w._vdPh || grid.querySelector('[data-vd-ph="' + w.dataset.wid + '"]');
      if (ph && ph.parentNode) { ph.parentNode.insertBefore(w, ph); ph.remove(); }
      else grid.appendChild(w);
      w._vdPh = null;
      var pb = w.querySelector('.vd-pop'); if (pb) { pb.classList.remove('on'); pb.title = 'Pop out (float on page)'; }
    }
    function floatStart(e) {
      if (e.target.closest('button')) return;
      var w = e.currentTarget.closest('.widget'); if (!w || !w.classList.contains('floating')) return;
      var r = w.getBoundingClientRect();
      fdrag = { w: w, dx: e.clientX - r.left, dy: e.clientY - r.top };
      document.addEventListener('mousemove', floatMove); document.addEventListener('mouseup', floatStop);
      e.preventDefault();
    }
    function floatMove(e) {
      if (!fdrag) return;
      fdrag.w.style.left = Math.max(0, Math.min(window.innerWidth - 60, e.clientX - fdrag.dx)) + 'px';
      fdrag.w.style.top = Math.max(0, Math.min(window.innerHeight - 28, e.clientY - fdrag.dy)) + 'px';
    }
    function floatStop() { fdrag = null; document.removeEventListener('mousemove', floatMove); document.removeEventListener('mouseup', floatStop); }
    function popWindow(w) {
      var pid = w.dataset.panel; if (!pid) return;
      window.open('/ui/panel/window?id=' + encodeURIComponent(pid), 'veraPanel_' + pid,
        'width=1040,height=820,menubar=no,toolbar=no,location=no,status=no');
    }

    /* per-widget wiring (drag/resize handlers + pop buttons), once each */
    function wireWidget(w) {
      if (w._vdWired) return; w._vdWired = true;
      w.setAttribute('draggable', state.editing ? 'true' : 'false');
      w.addEventListener('dragstart', onDragStart);
      w.addEventListener('dragover', onDragOver);
      w.addEventListener('dragleave', onDragLeave);
      w.addEventListener('drop', onDrop);
      w.addEventListener('dragend', onDragEnd);
      addPopButtons(w);
    }

    /* widget loader (+ Add Widget) + dynamic widgets */
    function openLoader() {
      if (!withLoader) return;
      ensureModal(); _modalCtl = ctl; _modal.style.display = 'flex';
      var inp = _modal.querySelector('.vd-wl-search'); inp.value = ''; inp.focus();
      fetchPanels().then(function () { renderLoader(''); });
    }
    function addWidget(panelId, o2) {
      var wid = 'dyn-' + String(panelId).replace(/[^a-zA-Z0-9_-]/g, '');
      if (grid.querySelector(':scope > [data-wid="' + wid + '"]')) return Promise.resolve();
      return fetch('/ui/panel/get?id=' + encodeURIComponent(panelId))
        .then(function (r) { return r.json(); })
        .then(function (full) {
          if (!full || full.error) return;
          var widget = document.createElement('div');
          widget.className = 'widget w-w6 w-h3';
          widget.dataset.wid = wid; widget.dataset.panel = panelId;
          widget.innerHTML =
            '<div class="w-head"><span class="w-grip">⠿</span><span class="w-dot ok"></span>' +
            '<span class="w-title">' + (full.icon || '') + ' ' + esc(full.label || panelId) + '</span>' +
            '<span class="w-actions"><button class="w-iconbtn" data-vd-hide title="Hide">×</button>' +
            '<button class="w-iconbtn" data-vd-remove title="Remove widget">🗑</button></span></div>' +
            '<div class="w-body flush" style="padding:0;position:relative"></div>' +
            '<span class="w-resize" data-resize></span>';
          var iframe = document.createElement('iframe');
          iframe.style.cssText = 'width:100%;height:100%;min-height:240px;border:none;background:transparent';
          iframe.srcdoc = buildDoc(full);
          var wbody = widget.querySelector('.w-body');
          wbody.appendChild(iframe);
          var wov = document.createElement('div');
          wov.className = 'vera-loading';
          wov.innerHTML = '<div class="vera-spinner"></div>';
          wbody.appendChild(wov);
          iframe.addEventListener('load', function () { wov.remove(); }, { once: true });
          setTimeout(function () { if (wov.parentNode) wov.remove(); }, 10000);
          widget.querySelector('[data-vd-hide]').onclick = function () { hide(wid); };
          widget.querySelector('[data-vd-remove]').onclick = function () { removeDynamic(wid); };
          grid.appendChild(widget);
          wireWidget(widget);
          state.dynamic[wid] = { panelId: panelId, wid: wid };
          if (!(o2 && o2.silent)) { state.order = widgets().map(function (w) { return w.dataset.wid; }); }
          save();
        }).catch(function () {});
    }
    function removeDynamic(wid) {
      var w = grid.querySelector(':scope > [data-wid="' + wid + '"]'); if (w) w.remove();
      delete state.dynamic[wid]; state.hidden.delete(wid);
      state.order = widgets().map(function (x) { return x.dataset.wid; }); save();
    }
    function restoreDynamic() {
      Object.keys(state.dynamic).forEach(function (wid) {
        var info = state.dynamic[wid];
        if (grid.querySelector(':scope > [data-wid="' + wid + '"]')) return;
        addWidget(info.panelId, { silent: true });
      });
    }

    /* ── boot this instance ──────────────────────────────────────────── */
    load();
    widgets().forEach(wireWidget);
    grid.addEventListener('mousedown', onResizeDown);
    applyLayout();
    if (withLoader) restoreDynamic();

    var ctl = {
      key: key, grid: grid,
      toggleEdit: toggleEdit, reset: reset, openLoader: openLoader,
      hide: hide, show: show, addWidget: addWidget, refresh: applyLayout
    };
    grid._veraDash = ctl;
    return ctl;
  }

  window.VeraDash = { init: init };
})();
