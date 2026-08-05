/* ============================================================================
 * vera-panel.js — Vera canonical panel chrome behavior
 * ----------------------------------------------------------------------------
 * Additive script. Include AFTER vera-ui.js:
 *
 *   <link rel="stylesheet" href="/ui/vera-panel.css">
 *   <script src="/ui/vera-ui.js"></script>
 *   <script src="/ui/vera-panel.js"></script>
 *
 * Responsibilities (all opt-in via the canonical markup, no per-panel JS):
 *   1. Inject a collapse chevron into #side-head and toggle the icon-rail
 *      (body.lhm-collapsed), persisted to localStorage per panel.
 *   2. Upgrade section headers marked .sec or [data-collapsible] into
 *      collapsible toggles with a caret (chat-rail behavior).
 *   3. Bridge #nav's own nav-btn sections to VeraPanelBridge.registerNav()
 *      (vera-panel-bridge.js) — the main-shell / chat-side-rail top-level
 *      nav-unification feature — so EVERY canonical-LHM panel gets its
 *      sections injected into whichever outer menu hosts it, automatically,
 *      the moment it also includes vera-panel-bridge.js. No per-panel JS: a
 *      MutationObserver watches for whichever nav-btn the panel's OWN
 *      switch function (nav()/dwSection()/show()/… — every panel names it
 *      differently) marks .active, and mirrors that via setNavActive() —
 *      panels never need to call the bridge themselves.
 *
 * It deliberately does NOT touch existing data-section nav handlers — panels
 * keep their own section-switch wiring, so this is purely additive.
 * ========================================================================== */
(function () {
  'use strict';

  var STORE_KEY = 'vera:lhm:collapsed:' + (location.pathname || 'panel');

  // ── 1. Sidebar collapse → icon rail ──────────────────────────────────────
  function chevronSvg() {
    return '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" ' +
      'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M10 3.5L5.5 8L10 12.5"/></svg>';
  }

  function setCollapsed(on) {
    document.body.classList.toggle('lhm-collapsed', !!on);
    try { localStorage.setItem(STORE_KEY, on ? '1' : '0'); } catch (e) {}
  }

  function initCollapse() {
    var sidebar = document.querySelector('#sidebar[data-vera-lhm]');
    var head = sidebar ? sidebar.querySelector('#side-head') : null;
    if (!head || !sidebar || document.getElementById('lhm-toggle')) return;

    var btn = document.createElement('button');
    btn.id = 'lhm-toggle';
    btn.type = 'button';
    btn.title = 'Collapse / expand sidebar';
    btn.setAttribute('aria-label', 'Toggle sidebar');
    btn.innerHTML = chevronSvg();
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      setCollapsed(!document.body.classList.contains('lhm-collapsed'));
    });
    head.appendChild(btn);

    var saved = '0';
    try { saved = localStorage.getItem(STORE_KEY) || '0'; } catch (e) {}
    if (saved === '1') setCollapsed(true);
  }

  // ── 2. Collapsible sections ──────────────────────────────────────────────
  // A header marked .sec or [data-collapsible] becomes a clickable toggle; the
  // siblings that follow it (until the next such header) are wrapped in a
  // .sec-body that the caret expands/collapses.
  function initSections() {
    var headers = document.querySelectorAll('[data-collapsible]');
    Array.prototype.forEach.call(headers, function (h) {
      if (h._vpWired) return;
      h._vpWired = true;
      h.classList.add('sec-toggle');

      var caret = document.createElement('span');
      caret.className = 'sec-caret';
      caret.textContent = '▶';
      h.insertBefore(caret, h.firstChild);

      // Collect following siblings into a body wrapper.
      var body = document.createElement('div');
      body.className = 'sec-body';
      var sib = h.nextElementSibling;
      while (sib && !sib.matches('[data-collapsible]')) {
        var next = sib.nextElementSibling;
        body.appendChild(sib);
        sib = next;
      }
      h.parentNode.insertBefore(body, h.nextSibling);

      var startCollapsed = h.getAttribute('data-collapsed') === '1' ||
                           h.classList.contains('collapsed');
      if (startCollapsed) { h.classList.add('collapsed'); body.classList.add('collapsed'); }

      h.addEventListener('click', function () {
        var c = h.classList.toggle('collapsed');
        body.classList.toggle('collapsed', c);
      });
    });
  }

  // ── 3. Nav → VeraPanelBridge, generic across every LHM-marked panel ──────
  // The canonical shape is <aside id="sidebar" data-vera-lhm><nav id="nav">
  // <button class="nav-btn" data-section="x">…, but several panels built
  // their own nav rail before this file existed (their own classes/CSS,
  // just a data-vera-lhm + data-section marker added to opt in) — rather
  // than special-case each one's own class names here, or in the bridge's
  // nav_select fallback, ANY element carrying data-vera-lhm qualifies, and
  // ANY descendant carrying data-section/-sec/-s (whatever class it has)
  // counts as a nav item; whichever one gets .active (every switcher found
  // so far already uses that convention for its own styling, canonical or
  // not) is treated as current. One shared implementation for the whole
  // panel population, not per-panel exceptions.
  function initNavBridge() {
    var host = document.querySelector('[data-vera-lhm]');
    if (!host || host._vpNavBridged) return;
    var nav = (host.id === 'nav') ? host : (host.querySelector('#nav') || host);
    var SEL = '[data-section], [data-sec], [data-s], [data-view], [data-tab], [data-nav], [data-pane], [data-go], [data-k]';
    var btns = nav.querySelectorAll(SEL);
    if (!btns.length) return;
    host._vpNavBridged = true;

    function idOf(b) {
      return b.getAttribute('data-section') || b.getAttribute('data-sec') || b.getAttribute('data-s') ||
             b.getAttribute('data-view') || b.getAttribute('data-tab') || b.getAttribute('data-nav') ||
             b.getAttribute('data-pane') || b.getAttribute('data-go') || b.getAttribute('data-k');
    }
    // title attribute first — it's already clean text with no icon glyph.
    // Failing that, a couple of panels wrap the label in its own child
    // element (.lbl, .fab-nb-label) rather than a bare trailing text node
    // (the shape the collapse CSS above relies on to hide just the label
    // in icon-rail mode) — check those explicitly before falling back to
    // whatever plain text nodes exist, then the whole button's text as a
    // last resort (icon glyph and all).
    function labelOf(b) {
      var t = (b.getAttribute('title') || '').trim();
      if (t) return t;
      var lblEl = b.querySelector('.lbl, .fab-nb-label, .nav-label');
      if (lblEl && lblEl.textContent.trim()) return lblEl.textContent.trim();
      var txt = '';
      Array.prototype.forEach.call(b.childNodes, function (n) { if (n.nodeType === 3) txt += n.textContent; });
      txt = txt.trim();
      return txt || (b.textContent || '').trim() || idOf(b);
    }
    var items = Array.prototype.map.call(btns, function (b) { return { id: idOf(b), label: labelOf(b) }; });
    // ".on" (markets_studio_panel.html's own railBtn convention, among
    // others) alongside the canonical ".active" — scoped to just these nav
    // buttons, so it's never ambiguous with an unrelated "on" state
    // elsewhere in the panel.
    function currentActive() {
      for (var i = 0; i < btns.length; i++) {
        if (btns[i].classList.contains('active') || btns[i].classList.contains('on')) return idOf(btns[i]);
      }
      return '';
    }
    function ready(tries) {
      if (!window.VeraPanelBridge) {
        if (tries > 0) setTimeout(function () { ready(tries - 1); }, 200);
        return;
      }
      window.VeraPanelBridge.registerNav(items);
      window.VeraPanelBridge.setNavActive(currentActive());
      var mo = new MutationObserver(function () {
        window.VeraPanelBridge.setNavActive(currentActive());
      });
      Array.prototype.forEach.call(btns, function (b) {
        mo.observe(b, { attributes: true, attributeFilter: ['class'] });
      });
    }
    ready(25);   // ~5s — covers vera-panel-bridge.js loading after this script
  }

  function init() { initCollapse(); initSections(); initNavBridge(); }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.veraPanel = {
    setCollapsed: setCollapsed,
    isCollapsed: function () { return document.body.classList.contains('lhm-collapsed'); },
    initSections: initSections,
    initNavBridge: initNavBridge,
  };
})();
