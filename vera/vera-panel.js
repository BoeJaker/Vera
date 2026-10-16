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
    var sidebar = document.querySelector('[data-vera-lhm]');
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
    var SEL = '[data-section], [data-sec], [data-s], [data-view], [data-tab], [data-nav], [data-pane], [data-go], [data-k], [data-t]';
    // Canonical markup first: .nav-btn is the menu's only target, so a heading
    // or a rule that happens to carry data-view (the Estate groups its items
    // that way) is never mistaken for one. The loose list stays for a panel
    // that opted in before this shape existed.
    var btns = nav.querySelectorAll('.nav-btn');
    if (!btns.length) btns = nav.querySelectorAll(SEL);
    if (!nav.querySelector('.nav-btn')) btns = Array.prototype.filter.call(btns, function (b) { return !!(b.matches && (b.matches('button, a, [role="button"], [onclick]') || b.matches('[role="tab"], .tab'))); });   /* a tab drawn as a div (its click bound in script) is an item too */
    // a menu in markup of its own (Research's icon rail: buttons with a title and an onclick, no data attribute): its
    // clickable things with a name are the items - never what sits under [data-lhm-skip] (Research's theme palette)
    if (!btns.length) btns = Array.prototype.filter.call(nav.querySelectorAll('button, a, [role="button"], [onclick]'), function (b) {
      return !(b.closest && b.closest('[data-lhm-skip]')) && !!((b.getAttribute('title') || b.textContent || '').trim()); });
    // A button the panel's own rules hide (a view filter — the Estate's
    // ?view=models shows only the model pages) is not part of the menu it is
    // publishing. Ask for the button's OWN computed display, never its
    // offsetParent: a panel is routinely still in a hidden container when this
    // runs (the shell builds a tab before it shows it), which makes every
    // offsetParent null and would let the whole menu through — the Estate
    // published its Models pages into its Estate menu that way. Computed
    // display is 'none' only when a rule aimed at this element says so, and is
    // unaffected by an ancestor being hidden.
    btns = Array.prototype.filter.call(btns, function (b) {
      try { return getComputedStyle(b).display !== 'none'; } catch (e) { return true; }
    });
    if (!btns.length) return;
    host._vpNavBridged = true;
    // a sidebar that CARRIES content of its own (panes: the Calendar's layers, events, assistant) keeps that content
    // when its menu is docked in the harness - only its header and tab strip fold (vera-panel.css, .vp-has-content)
    if (host.querySelector('#lhm-body, .lhm-pane, [data-lhm-content]')) host.classList.add('vp-has-content');

    /* AN ID NAMES ONE ITEM. The first attribute in this order that a button carried used to be its id, and the Estate
       marks every one of its items data-view="estate" (the view that owns it) beside a data-pane that names it: all
       nineteen published as "estate", the host lit them all, and picking any of them - Live ops, Docker - sent an id
       that meant none of them (owner, 2026-09-27: "i cant select sub menu options for things like the estate"). The
       id is now the first attribute, in the same order, that EVERY item carries with a DIFFERENT value; a panel whose
       ids were already unique keeps exactly the ids it had. */
    var ID_ATTRS = ['data-section', 'data-sec', 'data-s', 'data-view', 'data-tab', 'data-nav', 'data-pane', 'data-go', 'data-k', 'data-t'];
    var idAttr = ID_ATTRS.filter(function (a) {
      var seen = {};
      for (var i = 0; i < btns.length; i++) { var v = btns[i].getAttribute(a); if (!v || seen[v]) return false; seen[v] = 1; }
      return true;
    })[0] || '';
    /* none of those names every item apart: any data-* attribute that does is the id (the Estate's Storage says data-p;
       2026-09-27, it nested as seven 'null' items no pick could reach) - and failing even that, the item's place */
    if (!idAttr) {
      // each item's own element id first (Research: nv-r, nv-p ...; Cap Ontology: vt-matrix ...) - set on purpose, where a
      // data attribute may be anyone's (the bridge tags a top bar's buttons data-vpb-hid p1, p2 ... when it offers the bar)
      if (!idAttr) { var seen3 = {}, ok3 = true; for (var bk = 0; bk < btns.length; bk++) { var v3 = btns[bk].getAttribute('id'); if (!v3 || seen3[v3]) { ok3 = false; break; } seen3[v3] = 1; } if (ok3) idAttr = 'id'; }
    }
    if (!idAttr) {
      var at0 = btns[0].attributes || [];
      for (var ai = 0; ai < at0.length && !idAttr; ai++) {
        var nm = at0[ai].name; if (!/^data-/.test(nm) || /^data-(w|tip|title|label|desc|i18n|vera-|rcm-|icon|lhm-|vpb-)/.test(nm)) continue;   /* data-icon is the glyph, never the id */
        var seen2 = {}, ok = true;
        for (var bj = 0; bj < btns.length; bj++) { var v2 = btns[bj].getAttribute(nm); if (!v2 || seen2[v2]) { ok = false; break; } seen2[v2] = 1; }
        if (ok) idAttr = nm;
      }
    }
    function idOf(b) {
      if (idAttr) return b.getAttribute(idAttr);
      return b.getAttribute('data-section') || b.getAttribute('data-sec') || b.getAttribute('data-s') ||
             b.getAttribute('data-view') || b.getAttribute('data-tab') || b.getAttribute('data-nav') ||
             b.getAttribute('data-pane') || b.getAttribute('data-go') || b.getAttribute('data-k') || b.getAttribute('data-t') ||
             ('n' + Array.prototype.indexOf.call(btns, b));
    }
    // title attribute first — it's already clean text with no icon glyph, and
    // the canonical markup carries one on every item. Failing that, a panel
    // that wraps its label in a child element rather than a bare trailing
    // text node (the shape the collapse CSS above relies on to hide just the
    // label in icon-rail mode) gets those checked explicitly, then whatever
    // plain text nodes exist, then the whole button's text as a last resort
    // (icon glyph and all).
    function labelOf(b) {
      var dl = (b.getAttribute('data-label') || '').trim(); if (dl) return dl;   /* a title that is a description does not name the item */
      var t = (b.getAttribute('title') || '').trim();
      if (t) return t;
      var lblEl = b.querySelector('.lbl, .fab-nb-label, .nav-label');
      if (lblEl && lblEl.textContent.trim()) return lblEl.textContent.trim();
      var txt = '';
      Array.prototype.forEach.call(b.childNodes, function (n) { if (n.nodeType === 3) txt += n.textContent; });
      txt = txt.trim();
      // the label in a span beside its .gl glyph: the text without the glyph
      var gl = !txt && b.querySelector('.gl');
      if (gl) { Array.prototype.forEach.call(b.childNodes, function (n) { if (n !== gl) txt += n.textContent || ''; }); txt = txt.replace(/\s+/g, ' ').trim(); }
      return txt || (b.textContent || '').trim() || idOf(b);
    }
    // the group an item sits under (the nearest .nav-grp heading before it) and its glyph: the harness draws the
    // panel's menu in its LHM from these - a rail icon per group, or per item when the menu has no groups
    function groupOf(b) {
      for (var el = b.previousElementSibling; el; el = el.previousElementSibling) {
        if (el.classList && el.classList.contains('nav-grp')) return (el.textContent || '').trim().slice(0, 40);
      }
      var g = b.closest && b.closest('[data-nav-group]');
      return g ? String(g.getAttribute('data-nav-group') || '').slice(0, 40) : '';
    }
    function iconOf(b) { var di = b.getAttribute && b.getAttribute('data-icon'); if (di) return Array.from(di.trim()).slice(0, 2).join(''); var g = b.querySelector('.gl'); return g ? Array.from((g.textContent || '').trim()).slice(0, 2).join('') : ''; }
    /* WHAT AN ITEM IS FOR (owner, 2026-10-03: descriptions "visible on mouseover and could even aide the agents"): data-desc,
       short, for the pointer - or a title the item does not take its name from (it has a data-label); data-desc-long, for
       an agent. A group heading (.nav-grp, or the [data-nav-group] box) may carry data-icon / data-desc / data-desc-long
       for the group's one rail icon. */
    function descOf(b) { var d = (b.getAttribute('data-desc') || '').trim(); if (d) return d; var t = (b.getAttribute('title') || '').trim(); return (t && b.getAttribute('data-label') && t !== labelOf(b)) ? t : ''; }
    function descLongOf(b) { return (b.getAttribute('data-desc-long') || '').trim(); }
    function groupEl(b) {
      for (var el = b.previousElementSibling; el; el = el.previousElementSibling) { if (el.classList && el.classList.contains('nav-grp')) return el; }
      return (b.closest && b.closest('[data-nav-group]')) || null;
    }
    var items = Array.prototype.map.call(btns, function (b) {
      var it = { id: idOf(b), label: labelOf(b), group: groupOf(b), icon: iconOf(b), desc: descOf(b), desc_long: descLongOf(b) };
      /* the widgets the panel names for this item's menu (data-lhm-widgets="template@size ..."), as the chat's menus carry theirs */
      var wl = (b.getAttribute('data-lhm-widgets') || '').trim(); if (wl) it.widgets = wl.split(/[\s,]+/);
      // an item drawn with an SVG icon (Dream, the DAG workshop, Telegram) hands it on; the host draws only what its cleaner keeps
      if (!it.icon) { var sv = b.querySelector && b.querySelector('svg'); if (sv && sv.outerHTML && sv.outerHTML.length <= 3000) it.icon_svg = sv.outerHTML; }
      var ge = it.group ? groupEl(b) : null;
      if (ge) { it.group_icon = (ge.getAttribute('data-icon') || '').trim(); it.group_desc = (ge.getAttribute('data-desc') || '').trim(); it.group_desc_long = (ge.getAttribute('data-desc-long') || '').trim(); }
      // the panel's own menu (standalone) shows it on hover too; the name is pinned first, so the title never renames the item
      if (it.desc) { try { if (!b.getAttribute('data-label')) b.setAttribute('data-label', it.label); b.title = it.label + ' — ' + it.desc; } catch (e) {} }
      return it;
    });
    // ".active" is the canonical mark; ".on" is still accepted for a panel
    // that has not moved to this markup yet — scoped to just these nav
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
      // Hand the bridge the exact button for each id rather than leaving it to
      // re-find one by attribute: two items can carry the same value on
      // different attributes (the Estate's data-pane="estate" map beside every
      // data-view="estate" item), and the loser of that race is a dead menu
      // entry. We already hold the element, so there is nothing to guess.
      window.VeraPanelBridge.registerNav(items, function (id) {
        for (var i = 0; i < btns.length; i++) {
          if (idOf(btns[i]) === String(id)) { btns[i].click(); return; }
        }
        throw new Error('no menu item ' + id);
      });
      window.VeraPanelBridge.setNavActive(currentActive());
      var mo = new MutationObserver(function () {
        window.VeraPanelBridge.setNavActive(currentActive());
      });
      Array.prototype.forEach.call(btns, function (b) {
        mo.observe(b, { attributes: true, attributeFilter: ['class'] });
      });
    }
    ready(25);   // ~5s — covers vera-panel-bridge.js loading after this script

    /* SIDE BY SIDE FROM THE PANEL'S OWN MENU TOO (owner, 2026-09-28: "in the LHM all the sub menus are supposed to let
       you open pages as extra panels side-by-side"). While this menu is shown in the frame (not docked: the harness in
       tabs mode, or keeping inner menus), Ctrl/\u2318- or middle-click on an item - or on a page sub-section under it -
       asks the harness to open this panel again beside, at that item (vera:lhm:tab, as the chat's bridge does). */
    function splitKey(ev) { return !!(ev && (ev.ctrlKey || ev.metaKey || ev.button === 1)); }
    function besideOf(el) {
      if (!el || !el.closest) return '';
      var sb = el.closest('.vp-sub');
      if (sb && nav.contains(sb)) { var a = null; for (var i = 0; i < btns.length; i++) if (btns[i].classList.contains('active') || btns[i].classList.contains('on')) { a = btns[i]; break; } return (a ? idOf(a) + '>' : '') + 's:' + sb.getAttribute('data-vp-sub'); }
      for (var j = 0; j < btns.length; j++) if (btns[j] === el || btns[j].contains(el)) return idOf(btns[j]);
      return '';
    }
    function besideAsk(ev) {
      if (!splitKey(ev)) return;
      var inHarness = false; try { inHarness = window.parent && window.parent !== window && window.parent === window.top; } catch (e) {}
      var pid = ''; try { pid = window.VeraPanelBridge && window.VeraPanelBridge.panelId ? String(window.VeraPanelBridge.panelId() || '') : ''; } catch (e) {}
      if (!inHarness || !pid) return;
      var sec = besideOf(ev.target); if (!sec) return;
      ev.preventDefault(); ev.stopPropagation();
      if (ev.type === 'auxclick' || ev.type === 'click') { try { window.parent.postMessage({ type: 'vera:lhm:tab', id: pid.replace(/--\d+$/, ''), section: sec, by: 'you' }, '*'); } catch (e) {} }
    }
    nav.addEventListener('click', besideAsk, true);
    nav.addEventListener('auxclick', besideAsk, true);
    nav.addEventListener('mousedown', function (ev) { if (ev.button === 1 && besideOf(ev.target)) ev.preventDefault(); }, true);   /* no autoscroll on a middle-press */

    /* A PAGE'S OWN SUB-SECTIONS under the lit item (owner, 2026-09-28: "the estate ui's observe menu is missing the perf
       section"). A section that switches views in the page (Observe: Events / Perf) marks each tab data-vera-sub="<id>";
       the shown strip's tabs are mirrored here, the current one lit, and a pick clicks the tab. A strip in a bar the harness
       absorbed still counts as shown - that bar is folded away only because the harness holds it. */
    if (!document.getElementById('vp-sub-css')) { var sc = document.createElement('style'); sc.id = 'vp-sub-css';
      sc.textContent = '#sidebar[data-vera-lhm] .vp-sub,[data-vera-lhm] .vp-sub{display:flex;align-items:center;gap:7px;width:calc(100% - 22px);margin:1px 0 1px 22px;padding:5px 10px;border:none;border-radius:6px;background:transparent;color:var(--dim2,var(--t2,#8a92a0));font:inherit;font-size:11.5px;text-align:left;cursor:pointer}'
        + '#sidebar[data-vera-lhm] .vp-sub::before,[data-vera-lhm] .vp-sub::before{content:"\\203a";opacity:.5}'
        + '#sidebar[data-vera-lhm] .vp-sub:hover,[data-vera-lhm] .vp-sub:hover{background:var(--bg2,#1a1f26);color:var(--fg,var(--text,#d8dde3))}'
        + '#sidebar[data-vera-lhm] .vp-sub.active,[data-vera-lhm] .vp-sub.active{color:var(--fg,var(--text,#d8dde3));background:color-mix(in srgb,var(--acc,#5a9e8f) 14%,transparent)}'
        + 'body.lhm-collapsed #sidebar[data-vera-lhm] .vp-sub{display:none}';
      (document.head || document.documentElement).appendChild(sc); }
    function subShown(el) { for (var n = el; n && n !== document.body; n = n.parentElement) { if (n.hidden) return false; var cs = getComputedStyle(n);
      if (cs.visibility === 'hidden') return false; if (cs.display === 'none' && !n.hasAttribute('data-vpb-hdr-bar')) return false; } return !!el; }
    function subLabel(el) { var own = ''; Array.prototype.forEach.call(el.childNodes, function (c) { if (c.nodeType === 3) own += c.textContent; });
      return String(el.getAttribute('data-label') || own.trim() || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40); }
    var subSig = '';
    function syncSubs() {
      var act = null; for (var i = 0; i < btns.length; i++) if (btns[i].classList.contains('active') || btns[i].classList.contains('on')) { act = btns[i]; break; }
      var tabs = Array.prototype.filter.call(document.querySelectorAll('[data-vera-sub]'), subShown).slice(0, 24);
      var sig = (act ? idOf(act) : '') + '|' + tabs.map(function (t) { return t.getAttribute('data-vera-sub') + (/\b(active|on|selected)\b/.test(String(t.className || '')) ? '*' : ''); }).join(',');
      if (sig === subSig) return; subSig = sig;
      Array.prototype.forEach.call(nav.querySelectorAll('.vp-sub'), function (x) { x.remove(); });
      if (!act || !tabs.length) return;
      var after = act;
      tabs.forEach(function (t) { var b = document.createElement('button'); b.type = 'button'; b.className = 'vp-sub' + (/\b(active|on|selected)\b/.test(String(t.className || '')) ? ' active' : '');
        b.textContent = subLabel(t); b.title = b.textContent; b.setAttribute('data-vp-sub', t.getAttribute('data-vera-sub'));
        b.addEventListener('click', function () { t.click(); subSig = ''; setTimeout(syncSubs, 60); });
        after.parentNode.insertBefore(b, after.nextSibling); after = b; });
    }
    syncSubs(); setInterval(syncSubs, 700);
    /* a menu measured while its page is hidden (a harness tab not shown yet) has no size to say which way it runs: decide once it has one */
    function placeMenus() { var strip = initStripMenu(host, btns, items); initMenuWidgets(strip || host, btns, idOf, groupOf, items); }
    var hr0 = null; try { hr0 = host.getBoundingClientRect(); } catch (e) {}
    if (hr0 && (hr0.width || hr0.height)) placeMenus();
    else if (typeof ResizeObserver === 'function') { var ro0 = new ResizeObserver(function () { var r = host.getBoundingClientRect(); if (!r.width && !r.height) return; ro0.disconnect(); placeMenus(); }); ro0.observe(host); }
    else placeMenus();
  }

  /* ── 3c. A MENU THAT IS A STRIP ACROSS THE PAGE, DRAWN AS THE MENU (owner, 2026-10-03, the LHM audit point 1: "each panel
     use a common lhm styling that matches the lhm of the harness"). Nine panels mark a tab strip as their menu (ML Lab,
     Home Assistant, Perf, the vector browser, the widget registry and gallery, Cap ontology, the system monitor, Research):
     docked, the harness draws it as its LHM; opened on their own they showed a strip. On their own they now get the
     menu column - the same items, words, descriptions and icons - each item pressing the panel's own tab, the lit one
     following the panel. The strip's tabs step aside; anything else in the strip (a search, a button) stays. Docked, the
     column steps aside and the strip is back, as before. A panel can keep its strip: data-vp-strip-keep on the menu. */
  /* WHICH WAY A MENU RUNS, from where its items sit: the second item below the first is a column, beside it a strip. The
     menu's own box said it wrongly while the page was still laying out (Research's column read as a strip, 2026-10-04). */
  function isColumn(host, btns) {
    if (host.id === 'sidebar' || host.tagName === 'ASIDE' || (host.classList.contains('vp-menu') && !host.classList.contains('inline'))) return true;
    try {
      var a = btns[0] && btns[0].getBoundingClientRect(), b = btns[1] && btns[1].getBoundingClientRect();
      if (a && b && (a.width || a.height) && (b.width || b.height)) return Math.abs(b.top - a.top) > Math.abs(b.left - a.left);
      var fd = getComputedStyle(host).flexDirection; if (/column/.test(fd) && getComputedStyle(host).display.indexOf('flex') >= 0) return true;
      var hr = host.getBoundingClientRect(); return hr.height > hr.width * 1.5 && hr.width < 420;
    } catch (e) { return false; }
  }
  function initStripMenu(host, btns, items) {
    if (!host || host._vpStrip || host.hasAttribute('data-vp-strip-keep')) return null;
    if (isColumn(host, btns) || btns.length < 2) return null;
    host._vpStrip = true;
    if (!document.getElementById('vp-strip-css')) { var st = document.createElement('style'); st.id = 'vp-strip-css';
      st.textContent = 'body.vp-has-strip-menu{padding-left:var(--lhm-width,184px)!important;box-sizing:border-box}'
        + '.vp-menu.vp-strip-menu{position:fixed;left:0;top:0;bottom:0;z-index:40;box-sizing:border-box}'
        + '.vp-strip-menu .vp-sm-hd{font-size:9px;letter-spacing:.09em;text-transform:uppercase;font-weight:600;color:var(--dim2,var(--t3,#6b7280));padding:6px 10px 8px}'
        + 'body.vp-has-strip-menu .vp-strip-tab{display:none!important}'
        + 'html.vpb-nav-hosted body.vp-has-strip-menu{padding-left:0!important}html.vpb-nav-hosted .vp-strip-menu{display:none!important}'
        + 'html.vpb-nav-hosted body.vp-has-strip-menu .vp-strip-tab{display:revert!important}';
      (document.head || document.documentElement).appendChild(st); }
    var col = document.createElement('nav'); col.className = 'vp-menu vp-strip-menu'; col.setAttribute('aria-label', 'Menu'); col.setAttribute('data-w', 'menu · column');
    var hd = document.createElement('div'); hd.className = 'vp-sm-hd'; hd.textContent = String(document.title || '').replace(/^\s*Vera\s*[—–\-·|:]\s*/i, '').trim() || 'Menu'; col.appendChild(hd);
    var mine = [];
    items.forEach(function (it, i) {
      var b = document.createElement('button'); b.type = 'button'; b.className = 'nav-btn';
      var gl = document.createElement('span'); gl.className = 'gl';
      if (it.icon_svg) gl.innerHTML = it.icon_svg; else gl.textContent = it.icon || '';
      b.appendChild(gl); b.appendChild(document.createTextNode(it.label));
      b.title = it.desc ? it.label + ' — ' + it.desc : it.label;
      if (it.desc) b.setAttribute('data-desc', it.desc); if (it.desc_long) b.setAttribute('data-desc-long', it.desc_long);
      b.addEventListener('click', function () { try { btns[i].click(); } catch (e) {} setTimeout(lit, 30); });
      col.appendChild(b); mine.push({ b: b, gl: gl, it: it });
      try { btns[i].classList.add('vp-strip-tab'); } catch (e) {}
    });
    function lit() { mine.forEach(function (m, i) { var on = btns[i].classList.contains('active') || btns[i].classList.contains('on') || btns[i].getAttribute('aria-selected') === 'true';
      m.b.classList.toggle('active', !!on); }); }
    /* an item with no mark of its own takes the glyph its name maps to - the one table the harness's menu uses (no letters) */
    function glyphs() { if (!window.VeraLHM || !VeraLHM.glyphFor) return; mine.forEach(function (m) { if (!m.gl.textContent && !m.gl.firstElementChild) { try { m.gl.textContent = VeraLHM.glyphFor(m.it.label) || ''; } catch (e) {} } }); }
    if (window.VeraLHM) glyphs(); else { var s = document.createElement('script'); s.src = '/ui/vera-lhm.js'; s.onload = glyphs; (document.head || document.documentElement).appendChild(s); }
    document.body.insertBefore(col, document.body.firstChild);
    document.body.classList.add('vp-has-strip-menu');
    lit(); var mo = new MutationObserver(lit);
    Array.prototype.forEach.call(btns, function (b) { mo.observe(b, { attributes: true, attributeFilter: ['class', 'aria-selected'] }); });
    initMenuLook();
    return col;
  }

  /* ── 3b. THE MENU'S WIDGETS ON THE PANEL'S OWN MENU (owner, 2026-10-03, the LHM audit: "sub/internal LHMs editable, with
     widgets like the chat UI"; 2026-10-04: "go ahead on 3/4"). The harness's docked menu took widgets (✎, + Add widget, the
     chat's widget sheet); the panel opened on its own showed none and could not take any. The same lists, from the same
     store (/ui/lhm/widgets, per panel and per menu), under the open item's menu - 'i:<item id>', or 'g:<group>' when the
     menu has groups, the names the docked menu uses - with the same ✎. Docked, the harness draws them and this steps aside. */
  function initMenuWidgets(host, btns, idOf, groupOf, items) {
    if (!host || host._vpMw || !window.fetch) return;
    /* a menu that is a column (the canonical sidebar, a .vp-menu column, an aside) - never a tab strip across the page */
    if (!(host.classList.contains('vp-strip-menu') || isColumn(host, btns))) return;
    host._vpMw = true;
    var grouped = (function () { var g = {}; items.forEach(function (it) { if (it.group) g[it.group] = 1; }); return Object.keys(g).length >= 2; })();
    var path = location.pathname, panel = '', store = null, editing = false, elAsked = false;
    if (!document.getElementById('vp-mw-css')) { var st = document.createElement('style'); st.id = 'vp-mw-css';
      st.textContent = '.vp-mw{display:flex;flex-direction:column;gap:8px;margin:10px 6px 6px;padding-top:8px;border-top:1px solid var(--border,var(--bd,rgba(255,255,255,.09)))}'
        + '.vp-mw-h{display:flex;align-items:center;gap:6px;font-size:9px;letter-spacing:.09em;text-transform:uppercase;font-weight:600;color:var(--dim2,var(--t3,#6b7280))}'
        + '.vp-mw-h span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis}.vp-mw-h button{white-space:nowrap;flex:0 0 auto;background:transparent;border:1px solid transparent;border-radius:var(--r-sm,4px);color:inherit;font:inherit;font-size:11px;letter-spacing:0;text-transform:none;padding:1px 6px;cursor:pointer}'
        + '.vp-mw-h button:hover,.vp-mw-h button.on{color:var(--acc,var(--ac,#5a9e8f));border-color:currentColor}'
        + '.vp-mw.empty:not(.editing) .vp-mw-h span{visibility:hidden}.vp-mw.empty:not(.editing){border-top-color:transparent;opacity:.55}'
        + '.vp-mw-w{position:relative}.vp-mw-w vera-widget{display:block}'
        + '.vp-mw-rm{position:absolute;top:4px;right:4px;z-index:2;background:var(--bg2,var(--s2,#1a1f26));border:1px solid var(--border2,var(--bd2,#444));border-radius:4px;color:var(--text,var(--t1,#ddd));font-size:10px;line-height:1;padding:3px 5px;cursor:pointer}'
        + '.vp-cta{margin:6px 6px 8px;padding:7px 10px;border:1px solid var(--border,var(--bd,rgba(255,255,255,.12)));border-radius:var(--r-sm,6px);background:var(--bg2,var(--s2,#1a1f26));color:var(--text,var(--t1,#ddd));font:inherit;font-size:11px;text-align:left;cursor:pointer}'
        + '.vp-cta:hover{border-color:var(--acc,var(--ac,#5a9e8f));color:var(--acc,var(--ac,#5a9e8f))}.vp-cta[hidden]{display:none}'
        + 'html.vpb-nav-hosted .vp-mw,body.lhm-collapsed .vp-mw,html.vpb-nav-hosted .vp-cta,body.lhm-collapsed .vp-cta{display:none!important}';
      (document.head || document.documentElement).appendChild(st); }
    var box = document.createElement('div'); box.className = 'vp-mw'; box.setAttribute('data-w', 'menu widgets · host');
    host.appendChild(box);
    /* the menu's primary action under its widgets, as the chat's menus end: the panel's [data-lhm-cta] for the open item
       (="<item id>"), else its one for every menu (=""); pressing it presses the panel's own */
    var cta = document.createElement('button'); cta.type = 'button'; cta.className = 'vp-cta'; cta.setAttribute('data-w', 'cta · button'); host.appendChild(cta);
    function ctaFor() { var b = active(), id = b ? String(idOf(b)) : '', els = Array.prototype.slice.call(document.querySelectorAll('[data-lhm-cta]'));
      return els.filter(function (e) { return e.getAttribute('data-lhm-cta') === id && id; })[0] || els.filter(function (e) { return !e.getAttribute('data-lhm-cta'); })[0] || null; }
    function drawCta() { var e = ctaFor(); cta.hidden = !e; if (!e) return; cta.textContent = (e.getAttribute('data-label') || e.textContent || e.title || 'Go').replace(/\s+/g, ' ').trim().slice(0, 40);
      cta.title = e.getAttribute('data-desc') || e.title || ''; cta.onclick = function () { e.click(); }; }
    drawCta(); var ctaMo = new MutationObserver(drawCta); Array.prototype.forEach.call(btns, function (b) { ctaMo.observe(b, { attributes: true, attributeFilter: ['class'] }); });
    function active() { for (var i = 0; i < btns.length; i++) if (btns[i].classList.contains('active') || btns[i].classList.contains('on')) return btns[i]; return btns[0] || null; }
    function menuKey() { var b = active(); if (!b) return ''; return grouped ? 'g:' + (groupOf(b) || 'More').slice(0, 80) : 'i:' + String(idOf(b)).slice(0, 80); }
    function defaults() { var b0 = active(), el = (b0 && b0.hasAttribute('data-lhm-widgets')) ? b0 : document.querySelector('[data-lhm-widgets]:not(.nav-btn)'); if (!el) return [];
      return String(el.getAttribute('data-lhm-widgets') || '').split(/[\s,]+/).filter(function (x) { return /^[\w.:-]+(@(xs|s|m|l|xl))?$/.test(x); }).slice(0, 8)
        .map(function (x) { var p = x.split('@'); return { template_id: p[0], size: p[1] || 's' }; }); }
    function list() { var k = menuKey(); return (store && Object.prototype.hasOwnProperty.call(store, k)) ? store[k].slice() : defaults(); }
    function save(l) { var k = menuKey(); if (!k) return; store = store || {}; store[k] = l;
      fetch('/ui/lhm/widgets/set', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ panel: panel, path: path, menu: k, widgets: l }) }).catch(function () {}); }
    /* the widget element and its sheet (widget_element.js), loaded the first time a widget is drawn or added */
    function withEl(cb) {
      if (window.customElements && customElements.get('vera-widget') && window.VeraWidgetConfig) return cb();
      var sc = document.querySelector('script[src*="widget_element.js"]');
      if (!sc && !elAsked) { elAsked = true; sc = document.createElement('script'); sc.src = '/ui/widgets/widget_element.js'; (document.head || document.documentElement).appendChild(sc); }
      var n = 0; (function wait() { if (window.customElements && customElements.get('vera-widget')) return cb(); if (++n < 50) setTimeout(wait, 100); })();
    }
    function draw() {
      var l = list(); box.innerHTML = ''; box.classList.toggle('empty', !l.length); box.classList.toggle('editing', editing);
      var h = document.createElement('div'); h.className = 'vp-mw-h'; var lab = document.createElement('span'); lab.textContent = 'Widgets'; h.appendChild(lab);
      if (editing) { var add = document.createElement('button'); add.type = 'button'; add.textContent = '+ Add widget'; add.title = 'Any widget form, a template of yours - with a preview';
        add.addEventListener('click', function () { withEl(function () { var S = window.VeraWidgetConfig; if (!S || !S.open) return;
          try { Promise.resolve(S.open({ mode: 'add', into: 'side', title: 'Add to this menu', templates: true, sizes: ['xs', 's', 'm'] })).then(function (rec) { if (!rec) return; var l2 = list(); l2.push(rec); save(l2); draw(); }).catch(function () {}); } catch (e) {} }); });
        h.appendChild(add); }
      var ed = document.createElement('button'); ed.type = 'button'; ed.className = editing ? 'on' : ''; ed.textContent = editing ? '✓ Done' : '✎';
      ed.title = editing ? 'Leave edit mode (what you changed stays)' : 'Edit this menu - add widgets to it, as the chat\'s menus take them';
      ed.addEventListener('click', function () { editing = !editing; draw(); }); h.appendChild(ed); box.appendChild(h);
      if (!l.length) return;
      withEl(function () {
        l.forEach(function (rec, i) {
          var w = document.createElement('div'); w.className = 'vp-mw-w'; w.setAttribute('data-w', (rec.title || rec.template_id || rec.form || 'widget') + ' · widget');
          var vw = document.createElement('vera-widget');
          if (rec.template_id) { vw.setAttribute('template-id', rec.template_id); vw.setAttribute('size', rec.size || 's'); }
          else { try { vw.setAttribute('record', JSON.stringify(rec)); } catch (e) {} vw.setAttribute('size', (rec.frame && rec.frame.size) || 's'); }
          vw.setAttribute('item-drawer', ''); w.appendChild(vw);
          if (editing) { var rm = document.createElement('button'); rm.type = 'button'; rm.className = 'vp-mw-rm'; rm.textContent = '✕'; rm.title = 'Take it out of this menu';
            rm.addEventListener('click', function () { var l3 = list(); l3.splice(i, 1); save(l3); draw(); }); w.appendChild(rm); }
          box.appendChild(w);
        });
      });
    }
    function drop() { if (box.parentNode) box.parentNode.removeChild(box); }
    fetch('/ui/lhm/widgets?path=' + encodeURIComponent(path)).then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.ok) { drop(); return; } panel = d.panel || ''; store = d.menus || {}; draw();
      var key = menuKey(), mo = new MutationObserver(function () { var k = menuKey(); if (k !== key) { key = k; draw(); } });
      Array.prototype.forEach.call(btns, function (b) { mo.observe(b, { attributes: true, attributeFilter: ['class'] }); });
    }).catch(drop);
  }

  /* ── 4. ONE LOOK FOR A MENU THAT IS NOT THE CANONICAL SIDEBAR (owner, 2026-10-03: "each panel use a common lhm styling
     that matches the lhm of the harness"). A page whose menu cannot be the canonical <aside id="sidebar"> (its own
     #sidebar already holds a list, its views are a column beside the page) marks it class="vp-menu" - a column - or
     "vp-menu inline" - a block heading a column - and gets the canonical item look from here, without vera-panel.css's
     page-wide rules (.chip, .opt, .field ...). The values are the canonical sidebar's (vera-panel.css #sidebar[data-vera-lhm]
     .nav-btn; test_lhm_shared_menu_look keeps them equal); colours fall back through the names the page's own theme uses. */
  var VPM_BG1 = 'var(--bg1, var(--s1, #16181d))', VPM_BG2 = 'var(--bg2, var(--s2, #1a1f26))',
      VPM_BD = 'var(--border, var(--line, var(--bd, rgba(255,255,255,.09))))', VPM_BD2 = 'var(--border2, var(--bd2, var(--line, rgba(255,255,255,.16))))',
      VPM_FG2 = 'var(--fg2, var(--dim2, var(--t2, var(--fg-dim, #9aa3ad))))', VPM_FG = 'var(--fg, var(--text, var(--t1, #d8dde3)))',
      VPM_ACC = 'var(--acc, var(--accent, var(--ac, #5a9e8f)))';
  function initMenuLook() {
    if (!document.querySelector('.vp-menu') || document.getElementById('vp-menu-css')) return;
    var st = document.createElement('style'); st.id = 'vp-menu-css';
    st.textContent =
      '.vp-menu{width:var(--lhm-width,184px);flex-shrink:0;display:flex;flex-direction:column;padding:8px 6px;overflow-y:auto;background:' + VPM_BG1 + ';border-right:1px solid ' + VPM_BD + '}' +
      '.vp-menu.inline{width:auto;border-right:none;border-bottom:1px solid ' + VPM_BD + ';overflow:visible}' +
      '.vp-menu .nav-btn{display:flex;align-items:center;gap:9px;width:100%;padding:8px 10px;background:transparent;border:1px solid transparent;color:' + VPM_FG2 + ';border-radius:var(--ui-radius,5px);font:inherit;font-size:11px;cursor:pointer;text-align:left;margin-bottom:2px;white-space:nowrap;overflow:hidden;transition:background .12s,color .12s,border-color .12s}' +
      '.vp-menu .nav-btn:hover{background:' + VPM_BG2 + ';color:' + VPM_FG + '}' +
      '.vp-menu .nav-btn.active,.vp-menu .nav-btn.on{background:' + VPM_BG2 + ';border-color:' + VPM_BD2 + ';color:' + VPM_ACC + '}' +
      '.vp-menu .nav-btn svg{width:14px;height:14px;flex-shrink:0;opacity:.82}.vp-menu .nav-btn.active svg{opacity:1}' +
      '.vp-menu .nav-btn .gl{width:15px;flex-shrink:0;text-align:center;opacity:.85;font-size:13px}' +
      'html.vpb-nav-hosted .vp-menu{display:none!important}';   // docked: the harness draws this menu
    (document.head || document.documentElement).appendChild(st);
  }

  function init() { initMenuLook(); initCollapse(); initSections(); initNavBridge(); }

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

/* ── "on the desk" (DA-06c): a panel element with data-app="<app id>" (and data-app-args='{"k":"v"}') opens that app on
   the desk of the chat page hosting this panel. Hidden until the host answers that it can (a panel open on its own,
   or in a page with no desk, shows no dead button). ── */
(function () {
  'use strict';
  if (window.__veraPanelApps) return; window.__veraPanelApps = true;
  var host = window.parent && window.parent !== window ? window.parent : null;
  var css = document.createElement('style');
  css.textContent = 'html:not(.vp-desk) [data-app]{display:none!important}';
  (document.head || document.documentElement).appendChild(css);
  if (!host) return;
  window.addEventListener('message', function (ev) {
    if (ev.source !== host || !ev.data || ev.data.type !== 'vera:app:ready') return;
    document.documentElement.classList.add('vp-desk');
  });
  try { host.postMessage({ type: 'vera:app:hello' }, location.origin); } catch (e) { /* no host to ask */ }
  document.addEventListener('click', function (ev) {
    var b = ev.target && ev.target.closest ? ev.target.closest('[data-app]') : null; if (!b) return;
    ev.preventDefault(); ev.stopPropagation();
    var args = {}; try { args = b.getAttribute('data-app-args') ? JSON.parse(b.getAttribute('data-app-args')) : {}; } catch (e) { args = {}; }
    try { host.postMessage({ type: 'vera:app:open', app: b.getAttribute('data-app'), args: args }, location.origin); } catch (e) { /* host gone */ }
  }, true);
})();
