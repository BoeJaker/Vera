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

  function init() { initCollapse(); initSections(); }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.veraPanel = {
    setCollapsed: setCollapsed,
    isCollapsed: function () { return document.body.classList.contains('lhm-collapsed'); },
    initSections: initSections,
  };
})();
