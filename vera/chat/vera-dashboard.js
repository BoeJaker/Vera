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
 *
 * ON RECORDS (UI redesign M5 — Notes/40 §4, the Dashboard and Sizes boards):
 * a dashboard is a LAYOUT RECORD — {dashboard, layout, key, user, grid {cols
 * 12, row 58, gap 10, widths}, widgets[]} — and a tile is {record, at, span,
 * hidden, refresh, floated}. Each grid's default lives in a layout file
 * (vera/widgets/layouts/<key>.json, served at /ui/widgets/layouts/<key>):
 * every widget of the grid as a record (form · source · title · span · the
 * children of a composite). A tile the page still draws by hand carries
 * draw.body = "page" — its body stays in the markup so the page's own
 * updaters keep addressing it by id; the record owns the frame (title chip,
 * span, size, max_body, hidden, order). A record without a page body is
 * drawn by <vera-widget> at the size its span picks (2–3 wide S, 4 M, 6 L,
 * 8–12 XL; rows add detail, then the table). The loader drops a panel
 * record. vera.dash.<key> persists the layout record; a legacy {order,
 * hidden, sizes, dynamic} is migrated in memory (VeraDash.migrate — the same
 * rule as vera/widgets/migrate_layouts.py) and written back in the new
 * shape. Saved layouts (vera.dash.<key>.layouts), a Layouts menu and Arrange
 * (dense flow) sit beside the Configure button. Every tile gets its grip
 * and its resize handle by construction. Every mechanic above is kept.
 *
 * WIDGET CONFIG (the WidgetConfig · Dashboard · WidgetSpec boards): "+ Add
 * Widget" opens the widget surface — window.VeraWidgetConfig: the catalogue,
 * the record editor, a LIVE preview — and places the record it resolves as a
 * record tile at the span its size asks for (a panel record is the loader's
 * iframe tile, as before). ⚙ on every tile opens the same surface on the
 * tile's record; Save writes the record back into the layout (a page or file
 * tile's edit rides inline, flagged `edited`) and the tile follows it. A tile
 * draws through <vera-widget> whenever its record has a form and its body is
 * not the page's (draw.body != "page"); a page tile that converts retires its
 * hand-drawn markup under the drawing — kept, hidden, ids intact, so the
 * page's own updaters go on writing into it — and comes back when the record
 * returns to the page body. A record whose source cannot be read draws the
 * form's sample (window.VeraWidget.sample, else the shape's own here), marked
 * "sample", so nothing is ever blank. Edit mode carries the Dashboard board's
 * cues — the grid lines, a ring on every tile, the accent corner, the moving
 * label — and a tile can be dropped on the grid itself, not only on another
 * tile. Without the surface loaded the panel picker is the loader, as before
 * (and always reachable as openPanels), and ⚙ opens the record itself.
 *
 * THE MOVE IS THE PAGE'S OWN GESTURE (the Dashboard board's lifted tile and
 * dashed slot): in edit mode a press on a tile and a pull of a few pixels
 * lift the tile itself — it follows the pointer on a transform, nothing
 * reflows — while the dashed ghost sits on the slot it will take; release
 * lands it there (the same insertBefore, save and applyLayout as ever),
 * Escape puts it back. This replaces the native HTML5 drag session for the
 * mouse. That session belongs to the OS, not the page: once it starts the
 * page sees no mouse events, cannot end it, cannot scroll while the pointer
 * rests, and on Windows Chrome it locked the harness dashboard outright
 * ("Configure then drag a widget — the page locks, the cursor stays a hand").
 * The pointer gesture is the mechanism the resize corner and the float head
 * already use — tracked on the document under a full-viewport guard so an
 * iframe under the pointer never swallows it — and the page owns every step.
 * The dragstart / dragover / drop / dragend handlers stay wired for any
 * native session that still reaches the grid (dragenter is cancelled too, as
 * the drop model asks), so nothing that worked is gone.
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

  // Any mousedown-driven drag (resize handle, floating-widget header) that
  // tracks mousemove on `document` breaks the instant the pointer passes
  // over an iframe — a widget's own embedded panel, another widget's,
  // whatever's underneath a float — because an iframe is a SEPARATE
  // browsing context; mousemove over it fires in ITS document, never
  // bubbling up to this page's listener, so the drag looks like it just
  // "stops" until the pointer returns to non-iframe territory. Reported as
  // "resizing doesn't work if resizing over another widget" / "[floating]
  // stops moving if the mouse escapes it". Fix: a transparent full-viewport
  // overlay above everything for the drag's duration — the pointer is
  // always over OUR element, never an iframe, so tracking never drops.
  var _dragGuardEl = null;
  function _dragGuardOn(cursor) {
    if (_dragGuardEl) return;
    _dragGuardEl = document.createElement('div');
    _dragGuardEl.style.cssText = 'position:fixed;inset:0;z-index:2147483647;' +
      'cursor:' + (cursor || 'move') + ';background:transparent';
    document.body.appendChild(_dragGuardEl);
  }
  function _dragGuardOff() {
    if (_dragGuardEl) { _dragGuardEl.remove(); _dragGuardEl = null; }
  }

  // The dashboard's real scrolling ancestor is almost never `window` — every
  // host page (main dashboard, Dream, Workers & Ollama) wraps its tab content
  // in its own overflow-y:auto container (a `.panel` div here, something else
  // elsewhere), with `window`/`document.documentElement` itself never
  // scrolling at all. Walk up from the grid to find whichever ancestor is
  // ACTUALLY the one with scroll room, rather than assuming a specific class
  // name (which would only work on one host page) or `window` (which mostly
  // never scrolls on any of them).
  function _scrollParent(el) {
    var node = el.parentElement;
    while (node && node !== document.body) {
      var cs = getComputedStyle(node);
      if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll') && node.scrollHeight > node.clientHeight + 1) {
        return node;
      }
      node = node.parentElement;
    }
    return document.scrollingElement || document.documentElement;
  }

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
      // Every widget is a record: the chip in the head says form · source (the Harness board). Quiet until the grid
      // is being edited, when it is the thing you are arranging.
      '.vd-rec{font-family:var(--mono);font-size:8px;color:var(--dim2);opacity:.55;white-space:nowrap;overflow:hidden;',
      'text-overflow:ellipsis;max-width:38%;flex-shrink:1;margin-left:6px;letter-spacing:0;text-transform:none;font-weight:400}',
      '.dash-grid.editing .vd-rec{opacity:1;color:var(--acc)}',
      '.vd-rec.sample{font-style:italic}',
      // a record placed before widget.validate answered (the staged Add): the chip says so, then says what it found
      '.vd-rec.checking{font-style:italic;opacity:.8}',
      '.vd-rec.bad{color:var(--err,#c96b6b);opacity:.9}',
      // Edit mode as the Dashboard board draws it: the grid's own columns and rows under the tiles, a ring on every
      // tile, the accent corner handle always showing, the moving tile lifted with its label, a dashed frame while a
      // tile can be dropped on the grid itself — the cues that say "this is arrangeable now". The vars come from
      // the grid's geometry (gridVars) so the lines sit on the real columns whatever the host page's gap.
      // The lines are painted on a layer of their own (a positioned ::before promoted with will-change), never on the
      // grid's background: a background on the grid — two gradients tiled over its 3,500 px — made every scroll step
      // of the dashboard's container a 75–150 ms synchronous job (measured; 0.2 ms without it), and the drag's
      // auto-scroll runs a step per dragover, which Windows fires continuously while the pointer rests at the edge —
      // the main thread saturated, the native drag session starved and the page locked ("Configure then drag a
      // widget — the page locks"). On its own layer the lines cost the scroll nothing (0.1 ms a step).
      '.dash-grid.editing{position:relative;background-image:none}',
      '.dash-grid.editing::before{content:"";position:absolute;z-index:0;pointer-events:none;will-change:transform;',
      'top:0;bottom:0;left:var(--vd-pl,0px);right:var(--vd-pr,0px);',
      'background-image:linear-gradient(var(--border,rgba(255,255,255,.09)) 1px,transparent 1px),',
      'linear-gradient(90deg,var(--border,rgba(255,255,255,.09)) 1px,transparent 1px);',
      'background-size:calc((100% + var(--vd-gap,10px)) / var(--vd-cols,12)) calc(var(--vd-row,58px) + var(--vd-gap,10px));',
      'background-position:-1px -1px}',
      '.dash-grid.editing > .widget{box-shadow:0 0 0 1px color-mix(in srgb,var(--acc,#5a9e8f) 30%,transparent),var(--card,var(--shadow,none));cursor:grab}',
      '.dash-grid.editing > .widget > .w-resize{display:flex;opacity:.85}',
      '.dash-grid.editing > .widget > .w-resize::before{border-right-color:var(--acc,#5a9e8f);border-bottom-color:var(--acc,#5a9e8f);width:10px;height:10px}',
      '.dash-grid.editing > .widget:hover > .w-resize{opacity:1}',
      '.dash-grid.editing > .widget.dragging{opacity:.94;transform:rotate(-1deg) scale(1.02);',
      'box-shadow:0 0 0 1.5px var(--acc,#5a9e8f),0 24px 50px -16px #000;z-index:6;cursor:grabbing}',
      '.dash-grid.editing > .widget.dragging::before{content:"moving · drop on the grid";position:absolute;left:8px;bottom:6px;z-index:2;',
      'font-family:var(--mono);font-size:8px;color:var(--acc,#5a9e8f);pointer-events:none}',
      '.dash-grid.editing.vd-drop-here{outline:2px dashed var(--acc,#5a9e8f);outline-offset:6px}',
      // the moving tile follows the pointer on its transform: the host page's transform transition would trail it
      '.dash-grid.editing > .widget.dragging{transition:none}',
      // the dashed slot the lifted tile will take (the board's ghost: "drop here · 4 × 2") — fixed on the body, as the resize
      // ghost is: a box inside the grid would dirty the grid's layout on every move and the auto-scroll would pay for it
      '.vd-ghost{position:fixed;box-sizing:border-box;border-radius:var(--radius-lg,8px);border:1.5px dashed var(--acc,#5a9e8f);',
      'background:color-mix(in srgb,var(--acc,#5a9e8f) 8%,transparent);pointer-events:none;z-index:9600}',
      '.vd-ghost span{position:absolute;left:8px;top:6px;font-family:var(--mono);font-size:8.5px;color:var(--acc,#5a9e8f);white-space:nowrap}',
      // the tile that was just placed shows where it landed
      '@keyframes vdLanded{0%{box-shadow:0 0 0 2px var(--acc,#5a9e8f),0 0 24px color-mix(in srgb,var(--acc,#5a9e8f) 45%,transparent)}100%{box-shadow:none}}',
      '.widget.vd-landed{animation:vdLanded 1.6s ease-out}',
      // ⚙ — the record's editor on every tile; lit while the grid is being edited
      '.w-iconbtn.vd-cfg{font-size:12px}',
      '.dash-grid.editing .w-iconbtn.vd-cfg{color:var(--acc,#5a9e8f)}',
      // the retired hand-drawn body of a tile the record now draws (kept for the page's updaters)
      '.w-body > .w-page[hidden]{display:none!important}',
      // the drawing keeps its own height inside the tile's scrolling body: a composite taller than the tile scrolls, it is
      // never shrunk to the box and clipped top and bottom (the element centres its body in whatever height it is given)
      // The grid's rows are the Dashboard board's fixed units (58 px, the span IS the height): the host page's
      // minmax(58px, auto) let a row grow with its content, so a refresh tick that rewrote a page body, or a composite
      // that re-drew with more rows, changed a row's height and reflowed the whole grid under the pointer — the
      // "can't move" feel of a move with Auto 5 s on. A drawing taller than its tile scrolls inside the body instead.
      '.dash-grid[data-vd]{grid-auto-rows:var(--vd-row,58px)}',
      '.w-body > vera-widget.vd-draw{display:block;flex:1 1 auto;min-height:0;padding:8px}',
      // the record sheet (⚙ without the widget surface loaded): the record itself, editable
      '.vd-sheet textarea{width:100%;min-height:220px;background:var(--bg0);border:1px solid var(--border2);color:var(--text);',
      'font-family:var(--mono);font-size:10px;padding:8px;border-radius:3px;resize:vertical;box-sizing:border-box}',
      '.vd-sheet .vd-note{font-size:9.5px;color:var(--dim2);font-family:var(--mono);padding:0 0 6px;line-height:1.5}',
      '.vd-sheet .vd-err{font-size:9.5px;color:var(--err,#c96b6b);font-family:var(--mono);min-height:12px;padding-top:4px}',
      '.vd-sheet .vd-lm-save button{width:auto;padding:0 10px;height:24px}',
      '.vd-wl-list .wl-item.wl-records{border-color:var(--border);margin-bottom:4px}',
      '.vd-wl-list .wl-item.wl-records .wl-item-label{color:var(--acc)}',
      // The Layouts menu (saved arrangements, per user, per dashboard) and Arrange, beside Configure.
      '.vd-lm-list .lm-row{display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:3px;border:1px solid transparent}',
      '.vd-lm-list .lm-row:hover{background:var(--bg2);border-color:var(--border)}',
      '.vd-lm-list .lm-name{font-family:var(--mono);font-size:10.5px;font-weight:600;color:var(--text);flex:1}',
      '.vd-lm-list .lm-name.on{color:var(--acc)}',
      '.vd-lm-list .lm-meta{font-size:8.5px;color:var(--dim2);font-family:var(--mono)}',
      '.vd-lm-list button{font-size:9px;padding:2px 8px;border-radius:3px;border:1px solid var(--border);background:var(--bg0);',
      'color:var(--dim2);cursor:pointer;font-family:var(--mono)}',
      '.vd-lm-list button:hover{border-color:var(--acc);color:var(--acc)}',
      '.vd-lm-save{display:flex;gap:6px;padding:8px 10px;border-top:1px solid var(--border)}',
      '.vd-lm-save input{flex:1;background:var(--bg0);border:1px solid var(--border2);color:var(--text);padding:4px 8px;',
      'border-radius:3px;font-family:var(--mono);font-size:10.5px}',
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
      '@keyframes veraSpin{to{transform:rotate(360deg)}}',
      // Resize ghost — see onResizeDown(). A plain fixed-position preview box
      // that tracks the mouse directly; the real widget/grid are untouched
      // until mouseup, so nothing reflows (and therefore nothing jumps) mid-drag.
      '.vd-rghost{position:fixed;z-index:9600;pointer-events:none;box-sizing:border-box;',
      'border:2px dashed var(--acc,#5a9e8f);border-radius:var(--radius-lg,8px);',
      'background:color-mix(in srgb, var(--acc,#5a9e8f) 10%, transparent)}',
      '.vd-rghost-label{position:absolute;right:6px;bottom:5px;font-family:var(--mono);',
      'font-size:10px;font-weight:600;color:var(--acc,#5a9e8f);background:var(--bg1,#16181d);',
      'padding:2px 7px;border-radius:3px;border:1px solid var(--acc,#5a9e8f)}'
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
    if (!f.length && (q || !(window.VeraWidgetConfig && typeof window.VeraWidgetConfig.open === 'function'))) {
      list.innerHTML = '<div style="font-size:10px;color:var(--dim);padding:10px">' +
        (q ? 'No panels match "' + esc(q) + '"' : 'No panels registered') + '</div>';
      return;
    }
    // the picker's way to the widget surface (forms · sources · live preview) when it is on the page
    var toRecords = (!q && window.VeraWidgetConfig && typeof window.VeraWidgetConfig.open === 'function')
      ? '<div class="wl-item wl-records" data-records><span class="wl-item-icon">▣</span><span class="wl-item-label">A widget record…</span>' +
        '<span class="wl-item-id">forms · sources · live preview</span><span class="wl-item-mode">record</span></div>' : '';
    list.innerHTML = toRecords + f.map(function (p) {
      return '<div class="wl-item" data-id="' + esc(p.id) + '">' +
        '<span class="wl-item-icon">' + (p.icon || '▣') + '</span>' +
        '<span class="wl-item-label">' + esc(p.label || p.id) + '</span>' +
        '<span class="wl-item-id">' + esc(p.id) + '</span>' +
        '<span class="wl-item-mode">' + esc(p.mode || 'tab') + '</span></div>';
    }).join('');
    list.querySelectorAll('.wl-item').forEach(function (it) {
      if (it.hasAttribute('data-records')) { it.onclick = function () { closeModal(); if (_modalCtl) _modalCtl.openLoader(); }; return; }
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

  /* ── the layout record's pure parts (shared by every instance; VeraDash.* exports them) ─────────────────── */
  // The grid the Dashboard board draws — the units VeraDash has always used, so a layout migrates one to one.
  var GRID = { cols: 12, row: 58, gap: 10, widths: [2, 3, 4, 6, 8, 12] };
  // The span → size rule (the Sizes board; widget_record.size_for_span is the same rule): 2–3 wide S, 4 M, 6 L,
  // 8–12 XL; extra rows on a 6-wide add the detail, then the table.
  function sizeForSpan(w, h) {
    w = +w || 0; h = +h || 1;
    if (w <= 1) return 'xs';
    if (w <= 3) return 's';
    if (w <= 4) return 'm';
    if (w <= 6) return h < 3 ? 'l' : 'xl';
    return 'xl';
  }
  // The span a record asks for: its own frame.span, else the size's default cell; a panel is the loader's 6 × 3.
  function spanFor(record) {
    var fr = record && record.frame && typeof record.frame === 'object' ? record.frame : {};
    if (Array.isArray(fr.span) && fr.span.length === 2 && +fr.span[0]) return [+fr.span[0], +fr.span[1] || 1];
    if (record && record.form === 'panel') return [6, 3];
    var size = String(fr.size || (record && record.size) || (record && record.draw && record.draw.size) || 'm').toLowerCase();
    return { xs: [2, 1], s: [2, 1], m: [4, 2], l: [6, 2], xl: [8, 4] }[size] || [4, 2];   // l = 6 × 2: three rows on a 6-wide read as XL (sizeForSpan)
  }
  // The Sizes board's ladder on this grid — the sizes a tile can be placed at (2 wide is already S; XS is inline
  // text) with the span each lands at; the editor is handed the size names (VeraWidgetConfig's sizes).
  function sizeLadder() {
    return ['s', 'm', 'l', 'xl'].map(function (s) { return { size: s, span: spanFor({ frame: { size: s } }) }; });
  }
  function ladderSizes() { return sizeLadder().map(function (s) { return s.size; }); }
  /* ── the form's sample ──
     A record with no source, or one the element will not read on its own (a name that is not read-shaped), still
     draws: window.VeraWidget.sample(form) when the element offers it, else enough of the form's SHAPE to draw it here.
     The tile says "sample" in its chip. Nothing on a dashboard is ever blank. */
  var SHAPE_OF = { counter: 'level', hero: 'level', level: 'level', meter: 'level', gauge: 'level', radial: 'level', ring: 'level', rate: 'level', bar: 'level', tank: 'level', turbine: 'level',
    trace: 'series', sparkline: 'series', line: 'series', area: 'series', step: 'series', chart: 'series', horizon: 'series', candles: 'series',
    bars: 'values', thermo: 'values', heat: 'values', pills: 'values', kv: 'values', column: 'values', ranked: 'values', histogram: 'values', 'meter-panel': 'values', form: 'values',
    donut: 'parts', stack: 'parts', parts: 'parts', matrix: 'matrix',
    log: 'events', lane: 'events', feed: 'events', timeline: 'events', pulse: 'events',
    table: 'items', list: 'items', cards: 'items', rows: 'items', files: 'items', checklist: 'items', tree: 'items', gallery: 'items', people: 'items', node: 'items',
    stepper: 'stages', pipeline: 'stages', stages: 'stages', program: 'stages', calendar: 'calendar', agenda: 'calendar',
    string: 'string', terminal: 'string', announcement: 'string', ask: 'string',
    graph: 'graph', pipes: 'graph', topology: 'graph', flow: 'graph', minigraph: 'graph', context_graph: 'graph', scatter: 'points', globe: 'points' };
  var SAMPLE = {
    series: [12, 18, 15, 22, 27, 24, 31, 36, 33, 41, 38, 44, 47, 43, 52, 49, 55, 58, 54, 62, 60, 66, 63, 62],
    level: { value: 62, min: 0, max: 100, unit: '%', delta: 4 },
    values: { ct126: 71, ct121: 54, pxstore: 42, ct130: 33 },
    parts: { running: 26, paused: 3, stopped: 4 },
    matrix: { ct126: { cpu: 'ok', mem: 'ok', disk: 'warn' }, ct121: { cpu: 'ok', mem: 'warn', disk: 'ok' }, ct130: { cpu: 'down', mem: 'down', disk: 'down' } },
    events: [{ t: '2026-09-13T14:38:00', kind: 'memory', text: 'memory.select · 2 injected' }, { t: '2026-09-13T14:39:00', kind: 'err', text: 'ct130 connect timeout' },
      { t: '2026-09-13T14:40:00', kind: 'cap', text: 'code.author wrote 2 files' }, { t: '2026-09-13T14:41:00', kind: 'loop', text: 'loop v7 · step 5 · waiting on you' }],
    items: [{ name: 'ct126', status: 'ok', value: '18 ms' }, { name: 'ct121', status: 'ok', value: '22 ms' }, { name: 'pxstore', status: 'ok', value: '31 ms' }, { name: 'ct130', status: 'down', value: 'timeout' }],
    stages: { stages: [{ name: 'recall', done: true }, { name: 'read', done: true }, { name: 'author', current: true }, { name: 'verify' }, { name: 'land' }] },
    calendar: [{ when: '2026-09-13T09:00', title: 'heartbeat' }, { when: '2026-09-13T12:00', title: 'model sync' }, { when: '2026-09-14T03:00', title: 'dream cycle' }],
    string: 'sample — the record has no readable source yet',
    points: [{ x: 1, y: 3 }, { x: 2, y: 5 }, { x: 3, y: 4 }, { x: 4, y: 8 }, { x: 5, y: 7 }, { x: 6, y: 9 }, { x: 7, y: 8 }],
    // the context galaxy's shape: every family of an assembled context, a record's relevance, the edges it cites
    graph: { nodes: [{ id: 'q1', label: 'the question', family: 'context', score: 0.95 }, { id: 'm1', label: 'memory · boot path', family: 'memory', score: 0.82 },
      { id: 'm2', label: 'memory · re-embed', family: 'memory', score: 0.61 }, { id: 'v1', label: 'vector · fabric', family: 'vector', score: 0.7 }, { id: 'v2', label: 'vector · sandbox', family: 'vector', score: 0.42, included: false },
      { id: 'g1', label: 'graph · ct126', family: 'graph', score: 0.55 }, { id: 'c1', label: 'cap · code.read', family: 'cap', score: 0.74 }, { id: 'r1', label: 'run · loop v7', family: 'run', score: 0.48 },
      { id: 's1', label: 'skill · fixer', family: 'skill', score: 0.66 }],
      rels: [{ from: 'm1', to: 'q1', kind: 'cite' }, { from: 'v1', to: 'q1', kind: 'cite' }, { from: 'c1', to: 'r1', kind: 'ran' }, { from: 'g1', to: 'm1', kind: 'links' }],
      links: [{ source: 'm1', target: 'q1' }, { source: 'v1', target: 'q1' }, { source: 'c1', target: 'r1' }, { source: 'g1', target: 'm1' }] }
  };
  function sampleFor(form) {
    if (window.VeraWidget && typeof window.VeraWidget.sample === 'function') { try { var s = window.VeraWidget.sample(form); if (s != null) return s; } catch (e) { /* the shape's own below */ } }
    var f = String(form || '').toLowerCase(), shape = '';
    if (window.VeraWidget && typeof window.VeraWidget.forms === 'function') { var x = window.VeraWidget.forms().filter(function (y) { return y.id === f; })[0]; shape = (x && x.shape) || ''; }
    if (!shape) shape = SHAPE_OF[f] || '';
    return SAMPLE[shape];
  }
  // the record as the element is handed it: with the form's sample when it cannot read, marked sample
  function withSample(rec) {
    if (!rec || typeof rec !== 'object' || rec.data !== undefined || rec.form === 'panel' || rec.form === 'composite') return rec;
    var src = typeof rec.source === 'string' ? rec.source : '';
    var readable = src ? (window.VeraWidget && typeof window.VeraWidget.readable === 'function' ? window.VeraWidget.readable(src) : true) : false;
    if (readable) return rec;
    var o = {}; Object.keys(rec).forEach(function (k) { o[k] = rec[k]; }); o.sample = true;
    // an element with a sample of its own draws its sample face itself (marked, with the Read button for a source
    // that waits for a click) — it is only handed the shape's sample as data when it has none
    if (!(window.VeraWidget && typeof window.VeraWidget.sample === 'function')) { var s = sampleFor(rec.form); if (s !== undefined) o.data = s; }
    return o;
  }
  function panelRecord(panelId, wid, label) {
    panelId = String(panelId || '');
    return { id: wid || ('dyn-' + panelId.replace(/[^a-zA-Z0-9_-]/g, '')), form: 'panel', panel: panelId, source: 'panel:' + panelId,
      title: label || panelId, frame: { size: 'l', span: [6, 3] } };
  }
  // Dense flow: each visible tile takes the first cell, top row first then left to right, where its span fits —
  // the position a browser gives the same order in a 12-column auto-flow grid. at = [col, row]; a hidden tile
  // has no place (at: null). Positions are grid units, so a layout survives a window resize and the two narrow
  // breakpoints (where the page's CSS re-flows the same order into 6 or 2 columns).
  function flow(tiles, cols) {
    cols = cols || GRID.cols;
    var occ = {};
    function fits(r, c, w, h) { for (var y = r; y < r + h; y++) for (var x = c; x < c + w; x++) if (occ[y + ',' + x]) return false; return true; }
    function mark(r, c, w, h) { for (var y = r; y < r + h; y++) for (var x = c; x < c + w; x++) occ[y + ',' + x] = 1; }
    return (tiles || []).map(function (t) {
      var o = {}; Object.keys(t).forEach(function (k) { o[k] = t[k]; });
      if (t.hidden) { o.at = null; return o; }
      var sp = Array.isArray(t.span) ? t.span : [4, 1];
      var w = Math.min(cols, Math.max(1, +sp[0] || 4)), h = Math.max(1, +sp[1] || 1);
      for (var r = 0; r < 10000; r++) {
        for (var c = 0; c + w <= cols; c++) {
          if (fits(r, c, w, h)) { mark(r, c, w, h); o.at = [c, r]; return o; }
        }
      }
      o.at = [0, r]; return o;
    });
  }
  // Where a lifted tile lands for a pointer at (x, y) — the one rule for the pointer gesture and for a native drop.
  // Over another tile: before it on its left half, after it on the right. Over the grid itself (a gap, the padding,
  // past the last tile): before the first tile that follows the point in reading order — below it, or on its row
  // and to its right — else the end. tiles = [{wid, rect:{left,top,right,bottom}, src, hidden}] in grid order.
  function slotAt(tiles, x, y) {
    var list = (tiles || []).filter(function (t) { return t && t.rect && !t.hidden; });
    var others = list.filter(function (t) { return !t.src; });
    var over = others.filter(function (t) { var r = t.rect; return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom; })[0];
    if (over) {
      var r = over.rect;
      if (x < r.left + (r.right - r.left) / 2) return { next: over.wid, over: over.wid, before: true };
      var i = others.indexOf(over);
      return { next: i + 1 < others.length ? others[i + 1].wid : null, over: over.wid, before: false };
    }
    var next = others.filter(function (t) { var r = t.rect; return r.top > y || (y >= r.top && y <= r.bottom && r.left > x); })[0];
    return { next: next ? next.wid : null, over: null, before: true };
  }
  // Arrange / compact: the tiles in the order dense flow packs them (row, then column), hidden ones last in their
  // own order — so the grid closes the holes a tall tile left without moving anything the user did not ask about.
  function arrange(tiles, cols) {
    var placed = flow(tiles, cols);
    var vis = placed.filter(function (t) { return t.at; }), hid = placed.filter(function (t) { return !t.at; });
    vis.sort(function (a, b) { return (a.at[1] - b.at[1]) || (a.at[0] - b.at[0]); });
    return vis.concat(hid);
  }
  // The legacy shape → the layout record (one to one, the Dashboard board's rule): order → at (dense flow),
  // sizes → span, hidden → hidden, dynamic → a panel record (or the record a dynamic record tile carried). Page
  // tiles the legacy state never named keep their markup span and follow the ordered ones. Mirrored in
  // vera/widgets/migrate_layouts.py; the two are held to the same fixture by the tests.
  function migrate(legacy, o) {
    o = o || {}; legacy = (legacy && typeof legacy === 'object') ? legacy : {};
    var page = Array.isArray(o.page) ? o.page : [], key = o.key || '';
    var order = Array.isArray(legacy.order) ? legacy.order : [];
    var hidden = Array.isArray(legacy.hidden) ? legacy.hidden : [];
    var sizes = (legacy.sizes && typeof legacy.sizes === 'object') ? legacy.sizes : {};
    var dynamic = (legacy.dynamic && typeof legacy.dynamic === 'object') ? legacy.dynamic : {};
    var known = {};
    page.forEach(function (p) { if (p && p.id) known[String(p.id)] = { span: p.span }; });
    Object.keys(dynamic).forEach(function (wid) {
      var dd = dynamic[wid] || {};
      var rec = (dd.record && typeof dd.record === 'object') ? dd.record : null;
      var r = {};
      if (rec) { r.id = wid; Object.keys(rec).forEach(function (k) { r[k] = rec[k]; }); if (!r.id) r.id = wid; }
      known[wid] = { record: rec ? r : panelRecord(dd.panelId, wid) };
    });
    var ids = [], seen = {};
    order.concat(page.map(function (p) { return p && p.id; }), Object.keys(dynamic)).forEach(function (id) {
      id = String(id || ''); if (id && known[id] && !seen[id]) { seen[id] = 1; ids.push(id); }
    });
    var tiles = ids.map(function (id) {
      var k = known[id], sz = sizes[id];
      var span = (sz && +sz.w) ? [+sz.w, +sz.h || 1] : (k.span || (k.record ? spanFor(k.record) : [4, 1]));
      var refresh = (k.record && k.record.read && k.record.read.refresh) || (k.record && k.record.refresh) || '';
      return { record: k.record || id, at: null, span: span, hidden: hidden.indexOf(id) >= 0, refresh: refresh, floated: false };
    });
    return { v: 2, dashboard: key, layout: 'default', key: key, user: '', grid: { cols: GRID.cols, row: GRID.row, gap: GRID.gap, widths: GRID.widths.slice() },
      widgets: flow(tiles, GRID.cols) };
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
    var state = { order: [], hidden: new Set(), sizes: {}, dynamic: {}, editing: false,
      // the record side: the layout file's records by wid, per-tile meta {at, refresh, floated}, which wids the
      // persisted layout named (so a file's default hidden applies to new tiles only), the layout's name and grid
      // and the records the user EDITED through ⚙ on tiles that are not theirs (page tiles, the file's) — persisted
      // inline in the layout, flagged edited, so the file's record is what reset() returns to
      records: {}, edits: {}, meta: {}, seen: {}, name: 'default', user: '', file: null,
      grid: { cols: GRID.cols, row: GRID.row, gap: GRID.gap, widths: GRID.widths.slice() } };
    var dragSrc = null, fdrag = null;
    var LKEY = SKEY + '.layouts';     // the saved arrangements of this dashboard, by name

    function widgets() { return Array.prototype.slice.call(grid.querySelectorAll(':scope > .widget')); }
    function byId(wid) { return widgets().filter(function (w) { return w.dataset.wid === wid; })[0]; }
    // a floated tile lives on <body>; the grid keeps its placeholder
    function byIdAnywhere(wid) { return byId(wid) || document.querySelector('.widget[data-wid="' + wid + '"]'); }
    var $ = function (id) { return id ? document.getElementById(id) : null; };
    function spanOf(w) {
      return [parseInt((w.className.match(/w-w(\d+)/) || [])[1] || '4', 10), parseInt((w.className.match(/w-h(\d+)/) || [])[1] || '1', 10)];
    }
    function setSpan(w, span) {
      if (!span || !+span[0]) return;
      // a width the grid's CSS knows (a 5 persisted by another build would fall to one column) and a row 1–6
      var cw = snapWidth(+span[0]), ch = Math.max(1, Math.min(6, Math.round(+span[1] || 1)));
      [2, 3, 4, 6, 8, 12].forEach(function (n) { w.classList.remove('w-w' + n); });
      [1, 2, 3, 4, 5, 6].forEach(function (n) { w.classList.remove('w-h' + n); });
      w.classList.add('w-w' + cw); w.classList.add('w-h' + ch);
    }
    function snapWidth(n) { var near = GRID.widths[0], best = Infinity; GRID.widths.forEach(function (a) { var dd = Math.abs(a - n); if (dd < best) { best = dd; near = a; } }); return near; }
    // the tile's record: the user's edit first, then the file's, then the one a user tile carries
    function recordOf(wid) { return state.edits[wid] || state.records[wid] || (state.dynamic[wid] && state.dynamic[wid].record) || null; }
    function withId(r, wid) { var o = {}; Object.keys(r || {}).forEach(function (k) { o[k] = r[k]; }); o.id = wid; return o; }
    // the span picks the size a record tile draws at; the tile says its size either way
    function syncSize(w) {
      var sp = spanOf(w), size = sizeForSpan(sp[0], sp[1]);
      w.dataset.size = size;
      var el = w.querySelector(':scope > .w-body > vera-widget');
      if (el && el.getAttribute('size') !== size) el.setAttribute('size', size);
    }

    /* ── persistence: the LAYOUT RECORD (the Dashboard board) ──
       vera.dash.<key> holds {v:2, dashboard, layout, key, user, grid, widgets:[{record, at, span, hidden, refresh,
       floated}]}. The mechanics keep thinking in order / hidden / sizes / dynamic — they are what drag, resize,
       hide and the loader mutate — so the record is BUILT from them and the DOM on save, and UNPACKED into them
       on load. A page tile's record is its id (the layout file has the record); a tile the user added carries
       its record inline. A legacy {order, hidden, sizes, dynamic} is migrated in memory and written back in the
       new shape the next time anything saves. */
    function layoutRecord() {
      var tiles = [];
      Array.prototype.slice.call(grid.children).forEach(function (el) {
        var floated = el.hasAttribute && el.hasAttribute('data-vd-ph');
        var w = floated ? byIdAnywhere(el.getAttribute('data-vd-ph')) : (el.classList && el.classList.contains('widget') ? el : null);
        if (!w || !w.dataset.wid) return;
        var wid = w.dataset.wid, dyn = state.dynamic[wid], m = state.meta[wid] || {};
        var sp = state.sizes[wid] && +state.sizes[wid].w ? [+state.sizes[wid].w, +state.sizes[wid].h || 1] : spanOf(w);
        var ed = state.edits[wid];
        var t = { record: ed ? ed : (dyn ? (dyn.record || panelRecord(dyn.panelId, wid)) : wid), at: null, span: sp,
          hidden: state.hidden.has(wid), refresh: m.refresh || '', floated: !!floated };
        if (ed) t.edited = true;   // a page or file tile whose record the user changed: the edit rides inline
        tiles.push(t);
      });
      return { v: 2, dashboard: (state.file && state.file.dashboard) || key, layout: state.name || 'default', key: key,
        user: state.user || '', grid: state.grid, widgets: flow(tiles, state.grid.cols) };
    }
    function unpack(rec) {
      state.order = []; state.hidden = new Set(); state.sizes = {}; state.dynamic = {}; state.edits = {}; state.meta = {}; state.seen = {};
      state.name = rec.layout || 'default'; state.user = rec.user || '';
      if (rec.grid && +rec.grid.cols) state.grid = rec.grid;
      (rec.widgets || []).forEach(function (t) {
        if (!t) return;
        var r = (t.record && typeof t.record === 'object') ? t.record : null;
        var wid = r ? String(r.id || '') : String(t.record || '');
        if (!wid) return;
        state.order.push(wid); state.seen[wid] = 1;
        if (t.hidden) state.hidden.add(wid);
        if (Array.isArray(t.span) && +t.span[0]) state.sizes[wid] = { w: +t.span[0], h: +t.span[1] || 1 };
        state.meta[wid] = { at: Array.isArray(t.at) ? t.at : null, refresh: t.refresh || '', floated: !!t.floated };
        if (r && t.edited) state.edits[wid] = r;
        else if (r) state.dynamic[wid] = (r.form === 'panel' && r.panel) ? { panelId: r.panel, wid: wid, record: r } : { record: r, wid: wid };
      });
    }
    function save() {
      try { localStorage.setItem(SKEY, JSON.stringify(layoutRecord())); } catch (e) { /* private mode / quota */ }
    }
    function load() {
      try {
        var j = JSON.parse(localStorage.getItem(SKEY) || 'null');
        if (!j) return;
        if (!Array.isArray(j.widgets)) {
          j = migrate(j, { key: key, page: widgets().map(function (w) { return { id: w.dataset.wid, span: spanOf(w) }; }) });
        }
        unpack(j);
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
    // the drop model takes the dragenter's answer until the first dragover: a tile or the grid that is a target says so
    function onDragEnter(e) { if (state.editing && dragSrc) e.preventDefault(); }
    function onDragEnd() {
      if (dragSrc) dragSrc.classList.remove('dragging');
      grid.querySelectorAll('.widget.drag-over').forEach(function (x) { x.classList.remove('drag-over'); });
      grid.classList.remove('vd-drop-here');
      dragSrc = null;
    }
    // Drop on the grid itself (the Dashboard board's "drop on the grid"): a tile let go over a gap, the padding or
    // past the last tile takes the place the pointer names — before the first tile that follows it in reading
    // order, else the end. Dropping on another tile stays that tile's own (onDrop above).
    function onGridDragOver(e) {
      if (!state.editing || !dragSrc) return;
      var over = e.target.closest('.widget');
      if (over && over.parentNode === grid && over !== dragSrc) return;
      e.preventDefault(); grid.classList.add('vd-drop-here');
    }
    function onGridDrop(e) {
      if (!state.editing || !dragSrc) return;
      var over = e.target.closest('.widget');
      if (over && over.parentNode === grid && over !== dragSrc) return;
      e.preventDefault(); grid.classList.remove('vd-drop-here');
      var slot = slotAt(tileRects(dragSrc), e.clientX, e.clientY);
      grid.insertBefore(dragSrc, slot.next ? byId(slot.next) : null);
      state.order = widgets().map(function (w) { return w.dataset.wid; }); save(); applyLayout();
    }
    // every visible tile's box, in grid order, the moving one flagged — what slotAt reads
    function tileRects(src) {
      return widgets().map(function (w) { var r = w.getBoundingClientRect(); return { wid: w.dataset.wid, rect: { left: r.left, top: r.top, right: r.right, bottom: r.bottom }, src: w === src, hidden: w.classList.contains('hidden') }; });
    }

    /* ── the move: a pointer gesture on the tile (the Dashboard board's lifted tile and dashed slot) ──
       A press on a tile in edit mode — not on a control, the resize corner or an embedded panel's own frame — and a
       pull past a few pixels lift the tile: it follows the pointer on its transform (the class's lift plus the
       pointer's offset; nothing reflows), the moving label on it, the dashed ghost on the slot it will take,
       computed by slotAt from the live boxes; the container auto-scrolls while the pointer rests at its edge (a
       rAF loop, as the resize does — a pointer that stays still fires no events); release lands the tile — the
       same insertBefore, save and applyLayout as a drop — and the landed flash says where; Escape puts it back.
       The press itself is cancelled so the browser never opens a native drag session for the mouse. */
    var move = null;
    // a live gesture (a move, a resize) holds every layout pass until it ends: the grid never reflows under the pointer
    var gesture = 0, layoutAfter = false;
    function endGesture() { gesture = 0; if (layoutAfter) { layoutAfter = false; applyLayout(); } }
    function onMoveDown(e) {
      if (!state.editing || e.button !== 0) return;
      if (e.target.closest('.w-resize, button, input, select, textarea, a[href], [contenteditable], iframe, .vd-ghost')) return;
      var w = e.target.closest('.widget'); if (!w || w.parentNode !== grid || w.classList.contains('floating')) return;
      e.preventDefault();
      var scrollEl = _scrollParent(grid);
      move = { w: w, sx: e.clientX, sy: e.clientY, x: e.clientX, y: e.clientY, live: false, scrolled: 0, scrollEl: scrollEl, slot: null, ghost: null, raf: 0 };
      document.addEventListener('mousemove', onMoveMove);
      document.addEventListener('mouseup', onMoveUp);
      document.addEventListener('keydown', onMoveKey, true);
      window.addEventListener('blur', onMoveBlur);
    }
    function onMoveMove(e) {
      if (!move) return;
      move.x = e.clientX; move.y = e.clientY;
      if (!move.live) {
        if (Math.abs(e.clientX - move.sx) + Math.abs(e.clientY - move.sy) < 4) return;
        moveLift();
      }
      if (!move.raf) move.raf = requestAnimationFrame(moveTick);
    }
    function moveLift() {
      var m = move; m.live = true;
      dragSrc = m.w; m.w.classList.add('dragging');
      m.startRect = m.w.getBoundingClientRect();
      m.ghost = document.createElement('div'); m.ghost.className = 'vd-ghost'; m.ghost.innerHTML = '<span></span>';
      document.body.appendChild(m.ghost);
      _dragGuardOn('grabbing'); gesture = 1;
      m.scrollRaf = requestAnimationFrame(moveScrollTick);
    }
    // the lifted tile at the pointer, the ghost on the slot; the scroll so far keeps the tile under a resting pointer
    function moveTick() {
      var m = move; if (!m || !m.live) return;
      m.raf = 0;
      var dx = m.x - m.sx, dy = m.y - m.sy + m.scrolled;
      m.w.style.transform = 'translate(' + dx + 'px,' + dy + 'px) rotate(-1deg) scale(1.02)';
      var rects = tileRects(m.w), slot = slotAt(rects, m.x, m.y);
      var same = m.slot && m.slot.next === slot.next && m.slot.over === slot.over && m.slot.before === slot.before;
      if (same) return;
      m.slot = slot;
      var gr = grid.getBoundingClientRect(), sp = spanOf(m.w);
      var w = m.startRect.width, h = m.startRect.height, left, top;
      var target = slot.next ? rects.filter(function (t) { return t.wid === slot.next; })[0] : null;
      if (target) { left = target.rect.left; top = target.rect.top; }
      else {   // the end: under the last visible tile, at the grid's left edge
        var bottom = 0; rects.forEach(function (t) { if (!t.src && !t.hidden && t.rect.bottom > bottom) bottom = t.rect.bottom; });
        var cs = getComputedStyle(grid);
        left = gr.left + (parseFloat(cs.paddingLeft) || 0); top = bottom ? bottom + (parseFloat(cs.rowGap || cs.gap) || GRID.gap) : gr.top + (parseFloat(cs.paddingTop) || 0);
      }
      m.ghost.style.left = left + 'px'; m.ghost.style.top = top + 'px'; m.ghost.style.width = Math.min(w, gr.right - left) + 'px'; m.ghost.style.height = h + 'px';
      m.ghost.firstChild.textContent = 'drop here · ' + sp[0] + ' × ' + sp[1] + (target ? '' : ' · at the end');
    }
    // the container scrolls while the pointer rests within EDGE of its top or bottom, as the resize corner does
    function moveScrollTick() {
      var m = move; if (!m || !m.live) return;
      var r = m.scrollEl.getBoundingClientRect(), EDGE = 56, MAX = 16;
      var top = Math.max(0, r.top), bottom = Math.min(window.innerHeight, r.bottom), d = 0;
      if (m.y < top + EDGE) d = -MAX * (1 - (m.y - top) / EDGE);
      else if (m.y > bottom - EDGE) d = MAX * (1 - (bottom - m.y) / EDGE);
      if (d) {
        var before = m.scrollEl.scrollTop; m.scrollEl.scrollBy(0, d);
        var actual = m.scrollEl.scrollTop - before;
        if (actual) { m.scrolled += actual; m.slot = null; if (!m.raf) m.raf = requestAnimationFrame(moveTick); }
      }
      m.scrollRaf = requestAnimationFrame(moveScrollTick);
    }
    function onMoveKey(e) { if (e.key === 'Escape' && move) { e.preventDefault(); e.stopPropagation(); moveEnd(false); } }
    function onMoveUp() { moveEnd(true); }
    function onMoveBlur() { moveEnd(false); }   // the button let go over another window: the tile goes back
    function moveEnd(commit) {
      var m = move; if (!m) return; move = null;
      document.removeEventListener('mousemove', onMoveMove);
      document.removeEventListener('mouseup', onMoveUp);
      document.removeEventListener('keydown', onMoveKey, true);
      window.removeEventListener('blur', onMoveBlur);
      if (!m.live) return;   // a click: the tile never lifted
      if (m.raf) cancelAnimationFrame(m.raf); if (m.scrollRaf) cancelAnimationFrame(m.scrollRaf);
      _dragGuardOff(); endGesture();
      if (m.ghost) m.ghost.remove();
      m.w.style.transform = ''; m.w.classList.remove('dragging');
      if (dragSrc === m.w) dragSrc = null;
      var slot = commit ? slotAt(tileRects(m.w), m.x, m.y) : null;
      if (!slot) return;
      var next = slot.next ? byId(slot.next) : null;
      if (next === m.w.nextElementSibling || next === m.w) return;   // let go where it was
      grid.insertBefore(m.w, next);
      state.order = widgets().map(function (w) { return w.dataset.wid; }); save(); applyLayout();
      landed(m.w);
    }
    // the edit-mode grid lines follow the grid's own geometry: its columns, its gap, the layout record's row
    function gridVars() {
      var cs = getComputedStyle(grid);
      grid.style.setProperty('--vd-cols', String(state.grid.cols || GRID.cols));
      grid.style.setProperty('--vd-gap', (parseFloat(cs.columnGap || cs.gap) || GRID.gap) + 'px');
      grid.style.setProperty('--vd-row', (state.grid.row || GRID.row) + 'px');
      // the lines' layer sits on the content box, as the background did (the host page pads the grid)
      grid.style.setProperty('--vd-pl', (parseFloat(cs.paddingLeft) || 0) + 'px');
      grid.style.setProperty('--vd-pr', (parseFloat(cs.paddingRight) || 0) + 'px');
    }

    /* resize (edit mode only) — snap width/height to allowed spans.
     *
     * Previously this applied the new w-wN/w-hN classes to the LIVE widget on
     * every mousemove, which reflows the whole CSS grid immediately — other
     * widgets shift, and the widget being resized can itself move to a new
     * row/column mid-drag. The resize handle the mouse was tracking then isn't
     * where the mouse is anymore, so the drag "jumps"/ends up the wrong size
     * relative to the cursor, and (since a reflow can slide unrelated content
     * — another widget, an iframe — under the pointer) tracking feels like it
     * breaks whenever the mouse crosses onto the widget itself.
     *
     * Fix: a plain fixed-position ghost box previews the target size,
     * following the mouse directly — the real widget/grid are untouched
     * (nothing reflows) until mouseup, which commits the final span ONCE. */
    function onResizeDown(e) {
      if (!state.editing) return;
      var handle = e.target.closest('.w-resize'); if (!handle || !grid.contains(handle)) return;
      e.preventDefault(); e.stopPropagation();
      var w = handle.closest('.widget'); if (!w) return;
      var sx = e.clientX, sy = e.clientY;
      var cw = parseInt((w.className.match(/w-w(\d+)/) || [])[1] || '4', 10);
      var ch = parseInt((w.className.match(/w-h(\d+)/) || [])[1] || '1', 10);
      var allowed = [2, 3, 4, 6, 8, 12];
      // Per-column/per-row pixel pitch derived from the widget's OWN current
      // box (not a guessed fixed constant, which drifted out of sync with the
      // real grid on any viewport width / sidebar state / zoom / row-height-
      // grown-by-content) — self-correct every time from what's actually on
      // screen right now. Column/row gap read from the grid's computed style
      // so this works unmodified on every host dashboard (main/Dream/Workers
      // & Ollama), even if they don't all use the same gap.
      var startRect = w.getBoundingClientRect();
      var gcs = getComputedStyle(grid);
      var gapX = parseFloat(gcs.columnGap || gcs.gap) || 10;
      var gapY = parseFloat(gcs.rowGap || gcs.gap) || 10;
      var colPitch = (startRect.width + gapX) / cw;
      var rowPitch = (startRect.height + gapY) / ch;

      var ghost = document.createElement('div');
      ghost.className = 'vd-rghost';
      ghost.style.left = startRect.left + 'px';
      ghost.style.top = startRect.top + 'px';
      ghost.style.width = startRect.width + 'px';
      ghost.style.height = startRect.height + 'px';
      var label = document.createElement('div');
      label.className = 'vd-rghost-label';
      ghost.appendChild(label);
      document.body.appendChild(ghost);

      function snap(n, lo, hi, list) {
        n = Math.max(lo, Math.min(hi, n));
        if (!list) return n;
        var near = list[0], best = Infinity;
        list.forEach(function (a) { var d = Math.abs(a - n); if (d < best) { best = d; near = a; } });
        return near;
      }
      var targetW = cw, targetH = ch;
      var scrolled = 0;   // cumulative auto-scroll so far — see autoScrollTick()
      function applySize(clientX, clientY) {
        // Effective drag distance = raw viewport mouse delta PLUS however much
        // the page has auto-scrolled so far. Without the scrolled term, once
        // the cursor is pinned at the viewport edge (where it stays, visually,
        // while the page scrolls under it) the target height would plateau
        // right there and never grow further even as the page kept scrolling —
        // the whole point of auto-scrolling during a resize.
        var pxW = Math.max(140, startRect.width + (clientX - sx));
        var pxH = Math.max(70, startRect.height + (clientY - sy) + scrolled);
        ghost.style.width = pxW + 'px';
        ghost.style.height = pxH + 'px';
        targetW = snap(Math.round((pxW + gapX) / colPitch), 2, 12, allowed);
        targetH = snap(Math.round((pxH + gapY) / rowPitch), 1, 6, null);
        label.textContent = targetW + ' × ' + targetH;
      }
      // Resizing taller/shorter than the visible viewport needs the page to
      // scroll to follow, the same way dragging a file to a list's edge
      // auto-scrolls it — otherwise the target size (below/above the fold)
      // is simply unreachable by mouse. A plain mousemove listener alone
      // can't do this: it only fires while the mouse is actually moving, but
      // the user holds the cursor STILL at the viewport edge and expects the
      // page to keep scrolling — so a rAF loop re-checks the last known
      // cursor position every frame, independent of new mousemove events.
      var scrollEl = _scrollParent(grid);
      var EDGE = 56, MAX_SPEED = 16;
      var lastX = sx, lastY = sy, rafId = null;
      function autoScrollTick() {
        // The edge zone is relative to the SCROLL CONTAINER's own box, not
        // the raw browser viewport — on a page where that container doesn't
        // fill the whole window (a toolbar/tab-bar above it, say) the two
        // don't line up, and triggering off the wrong one either fires the
        // auto-scroll too early or never at the container's actual edge.
        var r = scrollEl.getBoundingClientRect();
        var top = Math.max(0, r.top), bottom = Math.min(window.innerHeight, r.bottom);
        var d = 0;
        if (lastY < top + EDGE) d = -MAX_SPEED * (1 - (lastY - top) / EDGE);
        else if (lastY > bottom - EDGE) d = MAX_SPEED * (1 - (bottom - lastY) / EDGE);
        if (d) {
          var before = scrollEl.scrollTop;
          scrollEl.scrollBy(0, d);
          var actual = scrollEl.scrollTop - before;   // 0 at scroll start/end
          if (actual) {
            // The ghost is position:fixed (viewport-relative) and its left/top
            // were computed from a rect taken ONCE at drag start — scrolling
            // the container after that moves the widget's real position
            // without moving the ghost, so compensate by the same amount to
            // keep it visually anchored over the widget as it scrolls beneath.
            scrolled += actual;
            ghost.style.top = (startRect.top - scrolled) + 'px';
            applySize(lastX, lastY);   // re-derive target size at the new ghost height
          }
        }
        rafId = requestAnimationFrame(autoScrollTick);
      }
      function mv(ev) {
        lastX = ev.clientX; lastY = ev.clientY;
        applySize(ev.clientX, ev.clientY);
      }
      function up() {
        document.removeEventListener('mousemove', mv);
        document.removeEventListener('mouseup', up);
        if (rafId) cancelAnimationFrame(rafId);
        _dragGuardOff(); endGesture();
        ghost.remove();
        allowed.forEach(function (n) { w.classList.remove('w-w' + n); });
        [1, 2, 3, 4, 5, 6].forEach(function (n) { w.classList.remove('w-h' + n); });
        w.classList.add('w-w' + targetW); w.classList.add('w-h' + targetH);
        // Resize only ever sets the w-wN/w-hN span classes — nothing else.
        // An earlier version of this also stamped an inline max-height here,
        // computed from THIS drag's measured row pitch — but that pitch is
        // only trustworthy when the widget's CURRENT rendered height truly
        // reflects its span, which isn't true for any widget that already
        // carries its own fixed max-height (Proxmox/Docker/Ollama/Host Temps
        // all do, in their own CSS). Measuring off an already-capped box and
        // then stamping a SECOND, competing inline cap from that measurement
        // is exactly what made repeated resizes drift — "2x1" ending up a
        // different actual size each time depending on whatever the box
        // happened to measure right before the drag. The class is the only
        // thing that needs to be idempotent; each widget's own CSS is
        // responsible for its own height ceiling, if it has one.
        state.sizes[w.dataset.wid] = { w: targetW, h: targetH };
        syncSize(w);       // the span picks the size the record draws at
        save();
        applyLayout();     // and the positions follow
      }
      applySize(sx, sy);   // seed the label before any movement
      _dragGuardOn('nwse-resize'); gesture = 1;
      rafId = requestAnimationFrame(autoScrollTick);
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
      if (gesture) { layoutAfter = true; return; }
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
      ws.forEach(function (w) {
        var sz = state.sizes[w.dataset.wid];
        if (sz && sz.w) { [2, 3, 4, 6, 8, 12].forEach(function (n) { w.classList.remove('w-w' + n); }); w.classList.add('w-w' + sz.w); }
        if (sz && sz.h) { [1, 2, 3, 4, 5, 6].forEach(function (n) { w.classList.remove('w-h' + n); }); w.classList.add('w-h' + sz.h); }
      });
      stampAt();
      ws.forEach(syncSize);
      renderHidden();
    }
    // at = [col, row] from dense flow over the grid's order — what the record persists and the tile shows
    function stampAt() {
      var tiles = widgets().map(function (w) { return { wid: w.dataset.wid, span: spanOf(w), hidden: state.hidden.has(w.dataset.wid) }; });
      flow(tiles, state.grid.cols).forEach(function (t) {
        var w = byId(t.wid); if (!w) return;
        state.meta[t.wid] = state.meta[t.wid] || {};
        state.meta[t.wid].at = t.at;
        if (t.at) w.dataset.at = t.at[0] + ',' + t.at[1]; else delete w.dataset.at;
      });
    }
    // Arrange / compact: the tiles in the order dense flow packs them, so the holes a tall tile left close.
    function doArrange() {
      var tiles = widgets().map(function (w) { return { wid: w.dataset.wid, span: spanOf(w), hidden: state.hidden.has(w.dataset.wid) }; });
      state.order = arrange(tiles, state.grid.cols).map(function (t) { return t.wid; });
      applyLayout(); save();
    }

    function toggleEdit() {
      state.editing = !state.editing;
      grid.classList.toggle('editing', state.editing);
      if (state.editing) gridVars();
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
      state.order = []; state.hidden = new Set(); state.sizes = {}; state.edits = {}; state.meta = {}; state.seen = {}; state.name = 'default';
      widgets().forEach(function (w) { if (w.dataset.record && !w.dataset.fromFile) { w.remove(); delete state.dynamic[w.dataset.wid]; } });
      widgets().forEach(function (w) { var r = recordOf(w.dataset.wid); if (r) { drawTile(w, r); recordChip(w); } });   // an edited tile back to its record
      if (state.file) applyFile(state.file); else applyLayout();
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
      // The header's own × button is every widget's normal "hide from the
      // dashboard" control (inline onclick="dashHideWidget(...)", hardcoded
      // per-widget) — while floating, that reads as "close this window" but
      // actually banishes the widget from the dashboard entirely, needing
      // the "+ Add Widget" picker to bring it back. Reported as "you can
      // quite easily remove it from the UI pressing × thinking you are
      // docking it." Intercepted here (capture phase, so it runs before the
      // inline onclick) ONLY while floating — docked widgets keep the
      // original hide behaviour untouched. Attached once; re-checks
      // .floating live so it's a no-op after dock() without needing removal.
      if (!w._vdCloseHooked) {
        w._vdCloseHooked = true;
        var btns = w.querySelectorAll('.w-actions .w-iconbtn');
        var closeBtn = btns[btns.length - 1];
        if (closeBtn) {
          closeBtn.addEventListener('click', function (e) {
            if (!w.classList.contains('floating')) return;
            e.preventDefault(); e.stopImmediatePropagation();
            dock(w);
          }, true);
        }
      }
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
      _dragGuardOn('move');
      document.addEventListener('mousemove', floatMove); document.addEventListener('mouseup', floatStop);
      e.preventDefault();
    }
    function floatMove(e) {
      if (!fdrag) return;
      fdrag.w.style.left = Math.max(0, Math.min(window.innerWidth - 60, e.clientX - fdrag.dx)) + 'px';
      fdrag.w.style.top = Math.max(0, Math.min(window.innerHeight - 28, e.clientY - fdrag.dy)) + 'px';
    }
    function floatStop() { fdrag = null; document.removeEventListener('mousemove', floatMove); document.removeEventListener('mouseup', floatStop); _dragGuardOff(); }
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
      w.addEventListener('dragenter', onDragEnter);
      w.addEventListener('dragover', onDragOver);
      w.addEventListener('dragleave', onDragLeave);
      w.addEventListener('drop', onDrop);
      w.addEventListener('dragend', onDragEnd);
      ensureChrome(w);
      addPopButtons(w);
    }
    // The frame is the record's: a grip to move it, a hide button, the resize handle (the one ol-bgqueue lacked)
    // and the chip that says form · source. Only what is missing is added; a page's own head is left as written.
    function ensureChrome(w) {
      var head = w.querySelector(':scope > .w-head');
      if (head) {
        if (!head.querySelector('.w-grip')) head.insertAdjacentHTML('afterbegin', '<span class="w-grip">⠿</span>');
        if (!head.querySelector('.w-actions')) {
          head.insertAdjacentHTML('beforeend', '<span class="w-actions"><button class="w-iconbtn" data-vd-hide title="Hide">×</button></span>');
          head.querySelector('[data-vd-hide]').onclick = function () { hide(w.dataset.wid); };
        }
      }
      if (!w.querySelector(':scope > .w-resize')) w.insertAdjacentHTML('beforeend', '<span class="w-resize" data-resize></span>');
      recordChip(w);
    }
    function recordChip(w) {
      var head = w.querySelector(':scope > .w-head'); if (!head) return;
      var r = recordOf(w.dataset.wid);
      var src = r ? (typeof r.source === 'string' ? r.source : ((r.reads && typeof r.reads.cap === 'string') ? r.reads.cap : '')) : '';
      var text = r ? (String((r.draw && r.draw.form) || r.form || '') + (src ? ' · ' + src : (r.panel ? ' · panel:' + r.panel : ''))) : '';
      var chip = head.querySelector('.vd-rec');
      if (!text) { if (chip) chip.remove(); return; }
      if (!chip) {
        chip = document.createElement('span'); chip.className = 'vd-rec';
        var act = head.querySelector('.w-actions');
        if (act) head.insertBefore(chip, act); else head.appendChild(chip);
      }
      var probs = w.dataset.problems ? w.dataset.problems.split('\n').filter(Boolean) : [];
      chip.textContent = text + (w.dataset.sample ? ' · sample' : '') + (w.dataset.checking ? ' · checking…' : '') + (probs.length ? ' · ' + probs.length + ' problem' + (probs.length === 1 ? '' : 's') : '');
      chip.classList.toggle('sample', !!w.dataset.sample); chip.classList.toggle('checking', !!w.dataset.checking); chip.classList.toggle('bad', !!probs.length);
      chip.title = 'record ' + (r.id || w.dataset.wid) + ' · ' + text + (w.dataset.sample ? ' · drawn from the form\'s sample: the source cannot be read' : '')
        + (w.dataset.checking ? ' · widget.validate is checking this record' : '') + (probs.length ? '\nwidget.validate: ' + probs.join(' · ') : '');
      ensureCfg(w);
      if (r.form) w.dataset.form = r.form;
      if (src) w.dataset.source = src; else if (r.panel) w.dataset.source = 'panel:' + r.panel;
      var mb = r.frame && r.frame.max_body, body = w.querySelector(':scope > .w-body');
      if (mb && body) { body.style.maxHeight = mb + 'px'; if (!body.style.overflowY) body.style.overflowY = 'auto'; }
    }
    // ⚙ — the record's editor (the WidgetConfig board) on every tile that has a record; first in the actions so the
    // head's own × stays last (the float's close hook counts on that)
    function ensureCfg(w) {
      var act = w.querySelector(':scope > .w-head .w-actions'); if (!act || act.querySelector('.vd-cfg')) return;
      var b = document.createElement('button'); b.className = 'w-iconbtn vd-cfg'; b.textContent = '⚙';
      b.title = 'Configure this widget — its record: source · form · size · options';
      b.onclick = function (e) { e.stopPropagation(); configure(w.dataset.wid, b); };
      act.insertBefore(b, act.firstChild);
    }

    /* ── the record draws the tile ──
       A record tile's <vera-widget> takes the record (with the form's sample when its source cannot be read). A page
       tile keeps its hand-drawn body while its record says draw.body = "page"; once the record says otherwise the
       page's markup is retired under the drawing — moved into a hidden holder, ids intact, so the page's updaters go
       on writing into it — and the element draws the record at the size the span picks; a record that returns to
       the page body brings the markup back. */
    function drawTile(w, rec) {
      if (!w || !rec || typeof rec !== 'object') return;
      var body = w.querySelector(':scope > .w-body'); if (!body) return;
      var shown = withSample(rec);
      if (shown.sample) w.dataset.sample = '1'; else delete w.dataset.sample;
      var el = body.querySelector(':scope > vera-widget');
      if (w.dataset.record) {                       // a record tile: the element is the body
        if (rec.form === 'context_graph') ensureContextGraph();
        if (el) el.setAttribute('record', JSON.stringify(shown));
        return;
      }
      var holder = body.querySelector(':scope > .w-page');
      var pageBody = !rec.form || (rec.draw && rec.draw.body === 'page');
      if (pageBody) {
        if (holder) { while (holder.firstChild) body.insertBefore(holder.firstChild, holder); holder.remove(); }
        if (el) el.remove();
        delete w.dataset.converted; delete w.dataset.sample;
        return;
      }
      ensureElement(); if (rec.form === 'context_graph') ensureContextGraph();
      if (!holder) {
        holder = document.createElement('div'); holder.className = 'w-page'; holder.hidden = true;
        while (body.firstChild) holder.appendChild(body.firstChild);
        body.appendChild(holder);
      }
      if (!el) { el = document.createElement('vera-widget'); el.className = 'vd-draw'; body.appendChild(el); }
      var sp = spanOf(w); el.setAttribute('size', sizeForSpan(sp[0], sp[1]));
      el.setAttribute('record', JSON.stringify(shown));
      w.dataset.converted = '1';
    }

    /* ── ⚙: the record editor on a tile ──
       The widget surface (VeraWidgetConfig — the WidgetConfig board) opens on the tile's record; while it is open
       the tile itself previews the edit (onChange); Save writes the record back (applyRecord), Cancel leaves the
       tile as it was. Without the surface on this page the record sheet opens instead: the record as the layout
       keeps it, editable, saved through the same path. */
    // The record is placed the moment the sheet closes; widget.validate (7–15 s on a busy backend) finishes behind it:
    // the tile says "checking…" meanwhile, then takes the normalised record, or names the problems on its chip and
    // stays editable through ⚙. The sheet drives this through onValidating / onValidated.
    function checking(w, on) { if (!w) return; if (on) w.dataset.checking = '1'; else delete w.dataset.checking; recordChip(w); }
    function validated(w, r, probs) {
      if (!w) return; delete w.dataset.checking;
      if (probs && probs.length) w.dataset.problems = probs.join('\n'); else delete w.dataset.problems;
      if (r && typeof r === 'object') applyRecord(w.dataset.wid, r); else recordChip(w);
    }
    // the staged validation of a tile that may not exist yet (the Add): what the sheet reports waits for the tile
    function stager() {
      var s = { w: null, on: false, res: null };
      s.onValidating = function () { s.on = true; if (s.w) checking(s.w, true); };
      s.onValidated = function (r, probs) { s.res = [r, probs || []]; s.on = false; if (s.w) validated(s.w, s.res[0], s.res[1]); };
      s.tile = function (w) { s.w = w; if (!w) return w; if (s.res) validated(w, s.res[0], s.res[1]); else if (s.on) checking(w, true); return w; };
      return s;
    }
    function configure(wid, anchor) {
      var rec = recordOf(wid), w = byIdAnywhere(wid); if (!rec || !w) return Promise.resolve(null);
      var was = withId(rec, wid);
      var back = function () { drawTile(w, was); recordChip(w); return null; };
      if (window.VeraWidgetConfig && typeof window.VeraWidgetConfig.open === 'function') {
        var p, st = stager(); st.tile(w);
        try {
          p = window.VeraWidgetConfig.open({ mode: 'edit', record: was, into: 'dashboard', title: was.title || wid, anchor: anchor || w, sizes: ladderSizes(), templates: true,
            onChange: function (r) { if (r && typeof r === 'object') { drawTile(w, withId(r, wid)); recordChip(w); } },
            onValidating: st.onValidating, onValidated: st.onValidated });
        } catch (e) { p = Promise.reject(e); }
        return Promise.resolve(p).then(function (r) { return (r && typeof r === 'object') ? applyRecord(wid, r) : back(); }, back);
      }
      return openSheet(wid, was);
    }
    /* Save: the record goes back into the layout — a user tile's in place, a page or file tile's as its edit — and
       the tile follows it: the title (when the head's title is plain text — a page head with its own spans inside is
       the page's), the span its size asks for when the size or span changed (a hand resize stays), the refresh, the
       chip, the body. */
    function applyRecord(wid, r) {
      var w = byIdAnywhere(wid), old = recordOf(wid); if (!w || !r || typeof r !== 'object') return null;
      var rec = withId(r, wid);
      if (state.dynamic[wid] && !state.records[wid]) {
        state.dynamic[wid].record = rec;
        if (rec.form === 'panel') state.dynamic[wid].panelId = rec.panel || String(rec.source || '').replace(/^panel:/, '');
      } else state.edits[wid] = rec;
      var t = w.querySelector(':scope > .w-head .w-title');
      if (t && !t.children.length && (rec.title || rec.name)) t.textContent = rec.title || rec.name;
      var fo = (old && old.frame) || {}, fn = rec.frame || {};
      if ((fn.span && JSON.stringify(fn.span) !== JSON.stringify(fo.span)) || (fn.size && fn.size !== fo.size)) {
        setSpan(w, spanFor(rec)); var sp = spanOf(w); state.sizes[wid] = { w: sp[0], h: sp[1] };
      }
      state.meta[wid] = state.meta[wid] || { at: null, refresh: '', floated: false };
      state.meta[wid].refresh = (rec.read && rec.read.refresh) || rec.refresh || '';
      drawTile(w, rec); recordChip(w); syncSize(w); save(); applyLayout();
      return rec;
    }
    var _sheet = null;
    function openSheet(wid, rec) {
      injectCSS();
      return new Promise(function (resolve) {
        if (!_sheet) {
          _sheet = document.createElement('div'); _sheet.className = 'vd-wl-overlay vd-sheet';
          _sheet.innerHTML =
            '<div class="vd-wl-box" style="width:520px"><div class="vd-wl-head">' +
            '<span style="font-family:var(--mono);font-size:10px;font-weight:700;color:var(--acc);letter-spacing:1px">WIDGET RECORD</span>' +
            '<span class="vd-sheet-title" style="flex:1;font-size:9px;color:var(--dim2);font-family:var(--mono)"></span>' +
            '<button class="w-iconbtn vd-wl-close" style="cursor:pointer">✕</button></div>' +
            '<div class="vd-wl-list"><div class="vd-note">The widget editor (the WidgetConfig surface) is not loaded on this page — this is the record itself, as the layout keeps it: form · source · title · read · frame · draw · actions.</div>' +
            '<textarea spellcheck="false"></textarea><div class="vd-err"></div></div>' +
            '<div class="vd-lm-save"><span style="flex:1"></span><button class="w-iconbtn" data-sheet-cancel style="cursor:pointer">Cancel</button>' +
            '<button class="w-iconbtn" data-sheet-save style="cursor:pointer;color:var(--acc)">Save</button></div></div>';
          document.body.appendChild(_sheet);
        }
        var ta = _sheet.querySelector('textarea'), err = _sheet.querySelector('.vd-err');
        _sheet.querySelector('.vd-sheet-title').textContent = wid + ' · ' + (rec.form || '') + (rec.source ? ' · ' + rec.source : '');
        ta.value = JSON.stringify(rec, null, 2); err.textContent = '';
        var done = function (v) { _sheet.style.display = 'none'; resolve(v); };
        _sheet.onclick = function (e) { if (e.target === _sheet) done(null); };
        _sheet.querySelector('.vd-wl-close').onclick = function () { done(null); };
        _sheet.querySelector('[data-sheet-cancel]').onclick = function () { done(null); };
        _sheet.querySelector('[data-sheet-save]').onclick = function () {
          var j; try { j = JSON.parse(ta.value); } catch (e) { err.textContent = 'not JSON: ' + e.message; return; }
          if (!j || typeof j !== 'object' || !j.form) { err.textContent = 'a record needs a form'; return; }
          done(applyRecord(wid, j));
        };
        _sheet.style.display = 'flex'; ta.focus();
      });
    }

    /* widget loader (+ Add Widget) + dynamic widgets
       The widget surface when it is loaded — the catalogue, the record editor, the live preview (the WidgetConfig
       board) — and the record it resolves lands on the grid (placeRecord); the panel picker otherwise, and always
       through openPanels(): every registered panel as an iframe tile, as before. */
    function openLoader() {
      if (!withLoader) return;
      if (window.VeraWidgetConfig && typeof window.VeraWidgetConfig.open === 'function') {
        var p, st = stager();
        try { p = window.VeraWidgetConfig.open({ mode: 'add', into: 'dashboard', title: 'Add a widget', sizes: ladderSizes(), templates: true, onValidating: st.onValidating, onValidated: st.onValidated }); }
        catch (e) { p = Promise.reject(e); }
        return Promise.resolve(p).then(function (rec) { return rec ? Promise.resolve(placeRecord(rec)).then(st.tile) : null; }, function () { return null; });
      }
      return openPanels();
    }
    function openPanels() {
      if (!withLoader) return;
      ensureModal(); _modalCtl = ctl; _modal.style.display = 'flex';
      var inp = _modal.querySelector('.vd-wl-search'); inp.value = ''; inp.focus();
      fetchPanels().then(function () { renderLoader(''); });
    }
    // a record from the picker lands as the tile its form asks for: a panel record is the loader's iframe tile, any
    // other a record tile at the span its size picks; the new tile lights up and scrolls into view
    function placeRecord(rec) {
      if (!rec || typeof rec !== 'object') return null;
      var pid = rec.panel || (typeof rec.source === 'string' && /^panel:/.test(rec.source) ? rec.source.slice(6) : '');
      if (rec.form === 'panel' && pid) return addWidget(pid).then(function () { return landed(byId('dyn-' + String(pid).replace(/[^a-zA-Z0-9_-]/g, ''))); });
      return landed(addRecord(rec, {}));
    }
    function landed(w) {
      if (!w) return null;
      w.classList.add('vd-landed'); setTimeout(function () { w.classList.remove('vd-landed'); }, 1800);
      try { w.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch (e) { /* an old host */ }
      return w;
    }
    /* a RECORD tile (UI redesign M2): the same grid mechanics — grip, resize ghost, snap, hide, float, solo —
       around a <vera-widget> drawing the record at the size its span picks (the element watches its own width).
       Persisted beside the panel tiles as dynamic {record, wid}; the loader's panel tiles are unchanged. */
    function ensureElement() {
      if (window.VeraWidget || document.querySelector('script[data-vera-widget-el]')) return;
      var s = document.createElement('script'); s.src = '/ui/widgets/widget_element.js'; s.setAttribute('data-vera-widget-el', '1');
      document.head.appendChild(s);
    }
    // The context graph form draws its galaxy itself up to L; at XL the element hands the data to the chat's own
    // <vera-context-graph> (widget_element hydrate), which only the chat page loads — so a dashboard that draws a
    // context_graph record fetches that script once and re-renders the tiles that were waiting for it.
    function ensureContextGraph() {
      if ((window.customElements && customElements.get('vera-context-graph')) || document.querySelector('script[data-vera-context-graph]')) return;
      var s = document.createElement('script'); s.src = '/ui/context_graph_element.js'; s.setAttribute('data-vera-context-graph', '1');
      s.onload = function () { grid.querySelectorAll('vera-widget').forEach(function (el) { try { if (el.record && el.record.form === 'context_graph' && el.render) el.render(); } catch (e) { /* the element draws again on its own read */ } }); };
      document.head.appendChild(s);
    }
    function spanClass(record) {
      var size = String((record && ((record.frame && record.frame.size) || record.size || (record.draw && record.draw.size))) || 'm').toLowerCase();
      return { xs: 'w-w2 w-h1', s: 'w-w2 w-h1', m: 'w-w4 w-h2', l: 'w-w6 w-h2', xl: 'w-w8 w-h4' }[size] || 'w-w4 w-h2';
    }
    function addRecord(record, o2) {
      if (!record || typeof record !== 'object') return null;
      ensureElement(); if (record.form === 'context_graph') ensureContextGraph();
      var rid = String(record.id || record.title || record.name || record.form || 'widget').replace(/[^a-zA-Z0-9_-]/g, '-').slice(0, 48);
      var wid = (o2 && o2.wid) || ('rec-' + rid + '-' + Math.random().toString(36).slice(2, 6));
      if (grid.querySelector(':scope > [data-wid="' + wid + '"]')) return null;
      var widget = document.createElement('div');
      widget.className = 'widget ' + spanClass(record);
      widget.dataset.wid = wid; widget.dataset.record = '1';
      if (o2 && o2.fromFile) widget.dataset.fromFile = '1';
      if (o2 && o2.span) setSpan(widget, o2.span);
      if (state.sizes[wid] && +state.sizes[wid].w) setSpan(widget, [state.sizes[wid].w, state.sizes[wid].h]);
      var refresh = (state.meta[wid] && state.meta[wid].refresh) || (o2 && o2.refresh) || '';
      if (refresh) { record.read = (record.read && typeof record.read === 'object') ? record.read : {}; record.read.refresh = refresh; }
      widget.innerHTML =
        '<div class="w-head"><span class="w-grip">⠿</span><span class="w-dot ok"></span>' +
        '<span class="w-title">' + esc(record.title || record.name || record.form || 'widget') + '</span>' +
        '<span class="w-actions"><button class="w-iconbtn" data-vd-hide title="Hide">×</button>' +
        ((o2 && o2.fromFile) ? '' : '<button class="w-iconbtn" data-vd-remove title="Remove widget">🗑</button>') + '</span></div>' +
        '<div class="w-body" style="padding:8px;position:relative;min-height:0"></div>' +
        '<span class="w-resize" data-resize></span>';
      var el = document.createElement('vera-widget');
      var sp0 = spanOf(widget);
      el.setAttribute('size', sizeForSpan(sp0[0], sp0[1]));   // the span picks the size (the Sizes board), not the pixels
      var shown = withSample(record); if (shown.sample) widget.dataset.sample = '1';   // no readable source: the form's sample, said so
      el.setAttribute('record', JSON.stringify(shown));
      widget.querySelector('.w-body').appendChild(el);
      widget.querySelector('[data-vd-hide]').onclick = function () { hide(wid); };
      var rm = widget.querySelector('[data-vd-remove]'); if (rm) rm.onclick = function () { removeDynamic(wid); };
      grid.appendChild(widget);
      if (o2 && o2.fromFile) { state.records[wid] = record; } else { state.dynamic[wid] = { record: record, wid: wid }; }
      state.meta[wid] = state.meta[wid] || { at: null, refresh: refresh, floated: false };
      wireWidget(widget);
      if (!(o2 && o2.silent)) { state.order = widgets().map(function (w) { return w.dataset.wid; }); }
      save();
      return widget;
    }
    function addWidget(panelId, o2) {
      var wid = 'dyn-' + String(panelId).replace(/[^a-zA-Z0-9_-]/g, '');
      if (grid.querySelector(':scope > [data-wid="' + wid + '"]')) return Promise.resolve();
      return fetch('/ui/panel/get?id=' + encodeURIComponent(panelId))
        .then(function (r) { return r.json(); })
        .then(function (full) {
          if (!full || full.error) return;
          var record = panelRecord(panelId, wid, ((full.icon || '') + ' ' + (full.label || panelId)).trim());
          var widget = document.createElement('div');
          widget.className = 'widget w-w6 w-h3';
          widget.dataset.wid = wid; widget.dataset.panel = panelId; widget.dataset.form = 'panel';
          if (state.sizes[wid] && +state.sizes[wid].w) setSpan(widget, [state.sizes[wid].w, state.sizes[wid].h]);
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
          state.dynamic[wid] = { panelId: panelId, wid: wid, record: record };
          state.meta[wid] = state.meta[wid] || { at: null, refresh: '', floated: false };
          wireWidget(widget);
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
      var pending = [];
      Object.keys(state.dynamic).forEach(function (wid) {
        var info = state.dynamic[wid];
        if (grid.querySelector(':scope > [data-wid="' + wid + '"]')) return;
        if (!info) return;
        if (info.panelId) { pending.push(addWidget(info.panelId, { silent: true })); return; }
        if (info.record) addRecord(info.record, { silent: true, wid: wid });
      });
      // the loader's tiles arrive after a fetch — once they are all in, the persisted order is applied once more
      // so a restored panel sits where the layout put it, not at the end
      if (pending.length) Promise.all(pending).then(function () { applyLayout(); }, function () {});
    }

    /* ── boot this instance ──────────────────────────────────────────── */
    load();
    widgets().forEach(wireWidget);
    grid.addEventListener('mousedown', onResizeDown);
    grid.addEventListener('mousedown', onMoveDown);
    grid.addEventListener('dragenter', onDragEnter);
    grid.addEventListener('dragover', onGridDragOver); grid.addEventListener('drop', onGridDrop);
    grid.addEventListener('dragleave', function (e) { if (e.target === grid) grid.classList.remove('vd-drop-here'); });
    applyLayout();
    if (withLoader) restoreDynamic();

    /* ── the layout file: this grid's widgets as records (vera/widgets/layouts/<key>.json) ──
       The file is the DEFAULT layout. Under the user's persisted state it: names every page tile's record (the chip,
       data-form / data-source, max_body); sets the span and the hidden flag of tiles the persisted layout never
       named; orders a fresh dashboard the file's way; and draws, through <vera-widget>, any record that has no page
       body. A page whose route is missing runs exactly as it did before — the DOM alone. */
    function applyFile(file) {
      if (!file || !Array.isArray(file.widgets)) return;
      state.file = file;
      var fileOrder = [], fresh = !state.order.length;
      if (file.grid && +file.grid.cols && fresh) state.grid = file.grid;
      file.widgets.forEach(function (t) {
        var r = (t && t.record && typeof t.record === 'object') ? t.record : null;
        var wid = r ? String(r.id || '') : String((t && t.record) || '');
        if (!wid) return;
        fileOrder.push(wid);
        if (r) state.records[wid] = r;
        var eff = state.edits[wid] || r;   // the user's edit of this tile, else the file's record
        var w = byIdAnywhere(wid);
        var span = Array.isArray(t.span) && +t.span[0] ? t.span : (eff ? spanFor(eff) : null);
        if (w) {
          if (!state.seen[wid]) {          // the file's defaults, for a tile the user has not arranged
            if (span && !(state.sizes[wid] && +state.sizes[wid].w)) setSpan(w, span);
            if (t.hidden) state.hidden.add(wid);
          }
          state.meta[wid] = state.meta[wid] || { at: null, refresh: '', floated: false };
          if (!state.meta[wid].refresh && t.refresh) state.meta[wid].refresh = t.refresh;
          if (r && !(r.draw && r.draw.body === 'page') && w.dataset.record) w.dataset.fromFile = '1';
          if (eff) drawTile(w, eff);   // the record draws the body it owns; a page body stays the page's
          recordChip(w);
        } else if (eff && eff.form && !(eff.draw && eff.draw.body === 'page')) {
          // a record the page does not draw by hand: the element draws it
          if (!state.meta[wid]) state.meta[wid] = { at: null, refresh: t.refresh || '', floated: false };
          if (t.hidden && !state.seen[wid]) state.hidden.add(wid);
          addRecord(eff, { silent: true, wid: wid, fromFile: true, span: span, refresh: t.refresh || '' });
        }
      });
      if (fresh) state.order = fileOrder;
      applyLayout();
      if (!EMBEDDED) {   // a tile the layout had floated comes back floating (a solo request needs the harness; skipped there)
        Object.keys(state.meta).forEach(function (wid) { if (state.meta[wid].floated) { var w = byId(wid); if (w && !w.classList.contains('floating')) setTimeout(function () { try { floatW(w); } catch (e) {} }, 300); } });
      }
    }
    function loadFile() {
      if (opts.layout === false) return Promise.resolve(null);
      var url = typeof opts.layout === 'string' ? opts.layout : '/ui/widgets/layouts/' + encodeURIComponent(key);
      return fetch(url, { headers: { Accept: 'application/json' } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) { if (j && Array.isArray(j.widgets)) applyFile(j); return j; })
        .catch(function () { return null; });
    }

    /* ── saved layouts: per user, per dashboard (vera.dash.<key>.layouts = {name: layout record}) ── */
    function layouts() {
      try { var j = JSON.parse(localStorage.getItem(LKEY) || 'null'); return (j && typeof j === 'object') ? j : {}; } catch (e) { return {}; }
    }
    function saveLayout(name) {
      name = String(name || '').trim().slice(0, 48); if (!name) return null;
      state.name = name;
      var all = layouts(); all[name] = layoutRecord();
      try { localStorage.setItem(LKEY, JSON.stringify(all)); } catch (e) {}
      save(); renderLayouts();
      return all[name];
    }
    function loadLayout(name) {
      var rec = name === 'default' ? null : layouts()[name];
      if (name !== 'default' && !rec) return false;
      // the user's tiles go; the record's come back through the same paths a boot uses
      widgets().forEach(function (w) { if (w.dataset.record && !w.dataset.fromFile) w.remove(); });
      widgets().forEach(function (w) { if (w.dataset.panel && state.dynamic[w.dataset.wid]) w.remove(); });
      if (rec) unpack(rec); else { state.order = []; state.hidden = new Set(); state.sizes = {}; state.dynamic = {}; state.edits = {}; state.meta = {}; state.seen = {}; state.name = 'default'; }
      widgets().forEach(function (w) { var r = recordOf(w.dataset.wid); if (r) { drawTile(w, r); recordChip(w); } });   // the tiles follow the layout's records
      widgets().forEach(function (w) { if (!state.sizes[w.dataset.wid]) { var r = recordOf(w.dataset.wid), t = state.file && state.file.widgets.filter(function (x) { return x && (x.record === w.dataset.wid || (x.record && x.record.id === w.dataset.wid)); })[0]; if (t && Array.isArray(t.span)) setSpan(w, t.span); else if (r) setSpan(w, spanFor(r)); } });
      applyLayout();
      if (withLoader) restoreDynamic();
      if (state.file) applyFile(state.file);
      save(); renderLayouts();
      return true;
    }
    function deleteLayout(name) {
      var all = layouts(); if (!(name in all)) return false;
      delete all[name];
      try { localStorage.setItem(LKEY, JSON.stringify(all)); } catch (e) {}
      if (state.name === name) { state.name = 'default'; save(); }
      renderLayouts(); return true;
    }
    var _lm = null;
    function openLayouts() {
      injectCSS();
      if (!_lm) {
        _lm = document.createElement('div');
        _lm.className = 'vd-wl-overlay vd-lm';
        _lm.innerHTML =
          '<div class="vd-wl-box" style="width:440px"><div class="vd-wl-head">' +
          '<span style="font-family:var(--mono);font-size:10px;font-weight:700;color:var(--acc);letter-spacing:1px">LAYOUTS</span>' +
          '<span style="flex:1;font-size:9px;color:var(--dim2);font-family:var(--mono)">saved arrangements · per user, per dashboard</span>' +
          '<button class="w-iconbtn vd-wl-close" style="cursor:pointer">✕</button></div>' +
          '<div class="vd-wl-list vd-lm-list"></div>' +
          '<div class="vd-lm-save"><input placeholder="Save the current arrangement as…"><button class="w-iconbtn" data-lm-save style="cursor:pointer">Save</button></div></div>';
        document.body.appendChild(_lm);
        _lm.addEventListener('click', function (e) { if (e.target === _lm) _lm.style.display = 'none'; });
        _lm.querySelector('.vd-wl-close').onclick = function () { _lm.style.display = 'none'; };
        var inp = _lm.querySelector('input');
        var doSave = function () { if (saveLayout(inp.value)) inp.value = ''; };
        _lm.querySelector('[data-lm-save]').onclick = doSave;
        inp.addEventListener('keydown', function (e) { if (e.key === 'Enter') doSave(); });
      }
      renderLayouts();
      _lm.style.display = 'flex';
    }
    function renderLayouts() {
      if (!_lm) return;
      var all = layouts(), names = Object.keys(all).sort();
      var n = function (rec) { return (rec && rec.widgets ? rec.widgets.length : 0) + ' widgets'; };
      var rows = ['<div class="lm-row"><span class="lm-name' + (state.name === 'default' ? ' on' : '') + '">default</span>' +
        '<span class="lm-meta">' + (state.file ? 'from the layout file · ' + n(state.file) : 'the page as written') + '</span>' +
        '<button data-lm-load="default">Load</button></div>'];
      names.forEach(function (nm) {
        rows.push('<div class="lm-row"><span class="lm-name' + (state.name === nm ? ' on' : '') + '">' + esc(nm) + '</span>' +
          '<span class="lm-meta">' + n(all[nm]) + '</span><button data-lm-load="' + esc(nm) + '">Load</button>' +
          '<button data-lm-del="' + esc(nm) + '" title="Delete">✕</button></div>');
      });
      var list = _lm.querySelector('.vd-lm-list');
      list.innerHTML = rows.join('');
      list.querySelectorAll('[data-lm-load]').forEach(function (b) { b.onclick = function () { loadLayout(b.getAttribute('data-lm-load')); }; });
      list.querySelectorAll('[data-lm-del]').forEach(function (b) { b.onclick = function () { deleteLayout(b.getAttribute('data-lm-del')); }; });
    }
    // Layouts ▾ and Arrange beside the page's own Configure button (the Dashboard board's toolbar) — the same
    // classes as that button so each page's toolbar styles them; once per toolbar.
    function injectToolbar() {
      var b = $(opts.editBtn); if (!b || !b.parentNode || b.parentNode.querySelector('[data-vd-layouts]')) return;
      var cls = b.className.replace(/\b(primary|pri)\b/g, '').trim();
      var lb = document.createElement('button'); lb.className = cls; lb.textContent = 'Layouts ▾'; lb.title = 'Saved layouts of this dashboard';
      lb.setAttribute('data-vd-layouts', key); lb.onclick = openLayouts;
      var ab = document.createElement('button'); ab.className = cls; ab.textContent = 'Arrange'; ab.title = 'Compact the grid: close the gaps, keep the order';
      ab.setAttribute('data-vd-arrange', key); ab.onclick = doArrange;
      b.parentNode.insertBefore(ab, b.nextSibling); b.parentNode.insertBefore(lb, b.nextSibling);
    }

    var ctl = {
      key: key, grid: grid,
      toggleEdit: toggleEdit, reset: reset, openLoader: openLoader,
      hide: hide, show: show, addWidget: addWidget, addRecord: addRecord, refresh: applyLayout,
      // the widget surface's paths: the picker of panels, a record placed, a tile's ⚙, a record saved, a tile drawn
      openPanels: openPanels, placeRecord: placeRecord, configure: configure, applyRecord: applyRecord,
      drawTile: function (wid, rec) { var w = byIdAnywhere(wid); if (w) drawTile(w, rec || recordOf(wid)); recordChip(w); return w; },
      recordOf: recordOf, editing: function () { return state.editing; },
      // the record side
      layout: layoutRecord, records: function () { var o = {}; widgets().forEach(function (w) { var r = recordOf(w.dataset.wid); if (r) o[w.dataset.wid] = r; }); return o; },
      applyFile: applyFile, file: function () { return state.file; }, arrange: doArrange,
      layouts: layouts, saveLayout: saveLayout, loadLayout: loadLayout, deleteLayout: deleteLayout, openLayouts: openLayouts,
      state: function () { return { name: state.name, order: state.order.slice(), hidden: Array.from(state.hidden), sizes: state.sizes, grid: state.grid }; }
    };
    grid._veraDash = ctl;
    grid.dataset.vd = '1'; gridVars(); window.addEventListener('resize', gridVars);
    ctl.ready = loadFile();
    injectToolbar();
    return ctl;
  }

  window.VeraDash = { init: init, migrate: migrate, flow: flow, arrange: arrange, sizeForSpan: sizeForSpan, spanFor: spanFor, panelRecord: panelRecord, GRID: GRID,
    sizeLadder: sizeLadder, ladderSizes: ladderSizes, sample: sampleFor, withSample: withSample, slotAt: slotAt };
})();
