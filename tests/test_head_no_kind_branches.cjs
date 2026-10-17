// WSU-58 S3 (one container for every item): the panel and the result declare their head actions like every other kind -
// the item's head builder and its more menu (_hdMenu) have no branch for a kind. A panel's Behind / Front and its bridge
// actions (Refresh, Query, Dispatch, Standalone) come from declare() in BLOCK.panel; a result's Run again is the head's
// generic Refresh, shown for any item with a source (sourceOf - VeraItem.normalise's rule).
//   node tests/test_head_no_kind_branches.cjs      (CommonJS: the gate parses js as scripts)
'use strict';
const path = require('node:path'), fs = require('node:fs');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const V = require(FILE); const SRC = fs.readFileSync(FILE, 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const h0 = SRC.indexOf("THE ITEM'S ONE HEAD"), h1 = SRC.indexOf('<div class="hda" data-w="canvas.item.controls">', h0);
const HEAD = h0 > 0 && h1 > h0 ? SRC.slice(h0, h1) : '';
const m0 = SRC.indexOf('    _hdMenu(key, btn) {'), m1 = SRC.indexOf('    _viewMenu(key, btn) {', m0);
const MENU = m0 > 0 && m1 > m0 ? SRC.slice(m0, m1) : '';
const KIND = /b\.type\s*[!=]==\s*'(panel|result)'/;
t('the head builder and the menu are found', HEAD.length > 1000 && MENU.length > 1000, HEAD.length + ' / ' + MENU.length);
t('no panel / result branch in the head builder', !KIND.test(HEAD), (HEAD.match(KIND) || [])[0]);
t('no panel / result branch in the more menu', !KIND.test(MENU), (MENU.match(KIND) || [])[0]);

const acts = (el, k) => ((el._decl || {})[k] || {}).acts || [];
/* the panel on the desk: Behind / Front first (lit by its layer), then the bridge's four */
const desk = { _pq: {}, _pdOpen: {}, _layer: { 'panel:w': 'behind' }, deskAlign: () => true };
V.BLOCK.panel({ panel: 'workers', title: 'Workers', src: '/ui/panels/file/worker_panel.html' }, 'l', 'panel:w', desk);
const pd = acts(desk, 'panel:w');
t('a panel on the desk declares Behind, Front, Refresh, Query, Dispatch, Standalone', pd.map((a) => a.act).join() === 'layer-behind,layer-front,prefresh,pquery,pdispatch,pstand', JSON.stringify(pd));
t('with the labels its head showed', pd.map((a) => a.label).join('|') === 'Behind|Front|Refresh|Query|Dispatch|Standalone ↗');
t('its layer lit', pd[0].on === true && !pd[1].on);
const col = { _pq: {}, _pdOpen: {}, deskAlign: () => false };
V.BLOCK.panel({ panel: 'workers', src: '/x' }, 'l', 'panel:c', col);
t('in the column only the bridge four (no desk, no layers)', acts(col, 'panel:c').map((a) => a.act).join() === 'prefresh,pquery,pdispatch,pstand');
const front = { _pq: {}, _pdOpen: {}, _layer: { 'panel:f': 'front' }, deskAlign: () => true };
V.BLOCK.panel({ panel: 'workers', src: '/x' }, 'l', 'panel:f', front);
t('a panel in front: Front lit', acts(front, 'panel:f')[1].on === true && !acts(front, 'panel:f')[0].on);

/* the result: its line declared, no acts of its own - Refresh is the container's, from its source */
const er = {}; V.BLOCK.result({ cap: 'fabric.query', args: { q: 'x' }, result: { ok: true }, ok: true, ms: 40 }, 'm', 'result:r', er);
const rd = (er._decl || {})['result:r'];
t('a result declares (its line; no acts of its own)', !!rd && /fabric\.query/.test(rd.sub || '') && Array.isArray(rd.acts));
t('a running result declares too', (() => { const e = {}; V.BLOCK.result({ cap: 'a.b', running: true }, 'm', 'result:x', e); return !!(e._decl || {})['result:x'] && Array.isArray(e._decl['result:x'].acts); })());
t('Refresh in the head and the menu comes from the item\'s source, and runs canvas.run on a trace',
  HEAD.includes("const src = sourceOf(b); if (src.cap) hx('data-act=\"rerun\"', 'Refresh', 'Run ' + src.cap + ' again with the same arguments');")
  && MENU.includes("const src = sourceOf(b); if (src.cap && !inHead('[data-act=\"rerun\"]')) h += row('rerun', 'Refresh',")
  && SRC.includes("if (act === 'rerun') { const c = sourceOf(this._blockOf(key)); if (!c.cap) return;")
  && SRC.includes(".then((tr) => this.call('canvas.run', { cap: c.cap, args: c.args || {}, key }, { trace: tr.tid })"));
t('sourceOf is VeraItem.normalise\'s rule: its own source, else a result\'s call', SRC.includes("const sourceOf = (b) => { b = b || {}; if (b.source && typeof b.source === 'object') return b.source;"));

/* a declared Behind marks the layer buttons as the item's own: the generic pair and the pill's ly follow the declaration */
t('the generic Behind / Front give way to declared ones; the pill\'s ⇣ ⇡ follow the declaration', HEAD.includes("const dLay = (d.acts || []).some((a) => a.act === 'layer-behind');")
  && HEAD.includes("if (!dLay && desk && ((this._layer || {})[key] || this._max === key))") && HEAD.includes("+ (dLay && desk ? '<span class=\"ly\" data-act=\"layer-behind\""));
t('the menu never repeats a declared action it already lists (Behind / Front)', MENU.includes("!(x.act && !x.aid && h.includes('data-act=\"' + x.act + '\"'))"));

/* the dead: the panel's own layer buttons and its old head (.vc-th) */
t('layBtns is gone', !/layBtns/.test(SRC));
t('no .vc-th style, and the desk handle is the item head alone', !/\.vc-th\s*\{|\.vc-th \.|\.vc-th b\{|> \.vc-th\)/.test(SRC) && !/vc-th/.test(SRC.slice(SRC.indexOf('const deskHandle = (ev) =>'), SRC.indexOf('const deskHandle = (ev) =>') + 400))
  && SRC.includes(".stage .it:not([data-desk-face^=\"layer-\"]) > .it-hd{cursor:grab}") && SRC.includes(".stage .it.desk-locked > .it-hd{cursor:default!important}"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
