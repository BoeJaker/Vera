// WSU-58 S6 (DEEP-29 11A.4 Decision 4): every body kind registers with VeraItem.registerBody - its views, fit, minSize and
// the actions it can declare - and the item's head row and its more menu are read from VeraItem.controls / VeraItem.menu,
// with what is live (the line, which actions show now, on / off) still declared by the body as it is drawn. The dashboard
// tile's row comes from the same VeraItem.controls (owner O4). Nothing a person sees changes: the rows and entries below are
// the ones the head and the menu drew before S6, kind by kind and state by state.
//   node tests/test_item_body_registry.cjs      (CommonJS: the gate parses js as scripts)
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const ROOT = path.join(__dirname, '..');
const FILE = path.join(ROOT, 'vera', 'canvas', 'canvas_element.js');
const V = require(FILE); const SRC = fs.readFileSync(FILE, 'utf8');
const VI = require(path.join(ROOT, 'vera', 'ui', 'vera_item.js'));
const VI_SRC = fs.readFileSync(path.join(ROOT, 'vera', 'ui', 'vera_item.js'), 'utf8');
const DASH = fs.readFileSync(path.join(ROOT, 'vera', 'chat', 'vera-dashboard.js'), 'utf8');
let n = 0; const t = (name, fn) => { fn(); n++; console.log('ok -', name); };

const KINDS = Object.keys(V.BLOCK).filter((k) => k[0] !== '_');

t('every BLOCK kind is registered with VeraItem', () => {
  assert(KINDS.length >= 20, 'every kind is exported');
  const reg = VI.bodies(); KINDS.forEach((k) => assert(reg.includes(k), k + ' is not registered'));
  KINDS.forEach((k) => { const b = VI.body(k); assert(['content', 'fill', 'fixed'].includes(b.fit), k + ' fit'); assert(VI.SIZES.includes(b.minSize), k + ' minSize'); });
});

t('a kind\'s registered views are the views it can offer (viewsOf)', () => {
  const want = { records: ['graph', 'list', 'cards', 'table'], table: ['table', 'cards', 'list', 'chart', 'graph'], entities: ['chips', 'list', 'graph'],
    json: ['tree', 'flat', 'validate'], kv: ['fields', 'graph'], app: ['table', 'cards', 'drawn'], code: ['code', 'structure', 'both'] };
  Object.keys(want).forEach((k) => assert.deepStrictEqual(VI.body(k).views, want[k], k));
  /* what viewsOf returns for a kind is always among what it registered */
  const el = { _apps: {}, explodeOffer: () => 'both' };
  const sample = { json: { data: { a: 1 } }, kv: { fields: { a: 1, b: 2, c: 3 } }, table: { columns: ['a', 'n'], rows: [['x', 1], ['y', 2]] }, entities: { groups: [{ type: 'p', items: [{ name: 'a' }] }] }, records: { items: [{ id: '1', title: 't' }] } };
  Object.keys(sample).forEach((k) => { let vw; try { vw = V.viewsOf({ type: k, key: k + ':v', content: sample[k] }, el); } catch (e) { return; }
    (vw.views || []).forEach((v) => assert(VI.body(k).views.includes(v), k + ' offers ' + v + ' it did not register')); });
});

/* a sample of every kind that declares, in the states that change what it declares */
const SAMPLE = {
  code: { code: 'def f():\n    return 1\n', lang: 'html', filename: 'f.html' }, diagram: { mermaid: 'graph TD\n A-->B', title: 'Boot' },
  explode: { binds: 'code:x', title: 'x' }, session: { host_id: 'ct126', ws: '/ws/term/x' },
  notebook: { notebook_id: 'nb1', cell_id: 'c1', cell_type: 'code', lang: 'python', content: 'print(1)' },
  panel: { panel: 'workers', src: '/ui/panels/file/worker_panel.html' }, html: { html: '<p>hi</p>' },
  result: { cap: 'fabric.query', args: {}, result: { ok: true }, ok: true }, app: { app: 'x' },
};
t('every action a body declares as it is drawn is one its kind registered', () => {
  global.document = global.document || {};
  const states = [{ deskAlign: () => true }, { deskAlign: () => false }, { deskAlign: () => true, _layer: {} }];
  KINDS.forEach((k) => states.forEach((st) => {
    const el = Object.assign({ _live: {}, _pq: {}, _pdOpen: {}, _apps: {}, previewOn: () => false }, st); const key = k + ':r';
    try { V.BLOCK[k](SAMPLE[k] || {}, 'm', key, el); } catch (e) { return; }
    const d = (el._decl || {})[key]; if (!d) return;
    (d.acts || []).forEach((a) => { const id = a.href ? 'link' : a.act; assert(VI.action(k, id), k + ' declares ' + id + ' it did not register'); });
  }));
  /* a report (an html item at a served address) declares its links */
  const el = {}; V.BLOCK.html({ url: '/reports/r.html', download_url: '/reports/r.zip' }, 'm', 'html:rep', el);
  assert((el._decl['html:rep'].acts || []).every((a) => a.href) && VI.action('html', 'link'));
});

t('what is static about an action is registered, and the head fills it in (the diagram\'s Edit source leads and stands for the kit\'s Edit)', () => {
  const a = VI.action('diagram', 'dgedit'); assert(a && a.lead === true && a.replaces === 'edit', JSON.stringify(a));
  global.document = global.document || {};
  const el = { _live: {} }; V.BLOCK.diagram({ mermaid: 'graph TD\n A-->B' }, 'l', 'diagram:s', el);
  const d = el._decl['diagram:s'].acts.find((x) => x.act === 'dgedit'); assert(d && d.lead === undefined && d.replaces === undefined, 'declared without the static flags');
  assert(SRC.includes("const actOf = (kind, a) => {") && SRC.includes('const dacts = declActs(b.type, d);') && SRC.includes('const da = declActs(kb.type, d);'));
});

t('the kit\'s actions are declared by the body that drew its card - the head parses no html - ranked as before', () => {
  const h0 = SRC.indexOf("THE ITEM'S ONE HEAD"), h1 = SRC.indexOf('<div class="hda" data-w="canvas.item.controls">', h0); const HEAD = SRC.slice(h0, h1);
  assert(h0 > 0 && h1 > h0); assert(!/createElement\('template'\)|tp\.innerHTML|code-card/.test(HEAD), 'the head parses the body');
  assert(HEAD.includes('const kit = kitNow;'));
  ['kitDeclare(el, VO.card(', 'kitDeclare(el, VO.fileCard(', 'kitDeclare(el, VO.diagram(', 'kitDeclare(el, VO.render(shown'].forEach((s) => assert(SRC.includes(s), s));
  assert.deepStrictEqual(VI.body('code').actions.filter((a) => a.kit).map((a) => a.id),
    ['copy', 'run', 'edit', 'preview', 'dsrc', 'present', 'outline', 'fpop', 'pop', 'dpop', 'save', 'dsvg', 'dpng'].map((v) => 'vo:' + v), 'the head\'s former RANK');
  /* the body hands its first card's actions to the item */
  const tp = { content: { querySelector: () => ({ card: 1 }) } }; let built = 0;
  global.document = { createElement: () => { built++; return tp; } };
  const saved = global.VeraOut; global.VeraOut = { barActions: () => ({ acts: [{ vo: 'copy', label: 'Copy' }], menus: [] }), card: () => '<div class="code-card vc-kit" data-cb="cv-x"></div>' };
  try { const el = { _live: {} }; V.BLOCK.code({ code: 'x = 1', lang: 'python' }, 'm', 'code:k', el);
    assert(el._kitNow && el._kitNow.acts[0].vo === 'copy' && built === 1, 'declared');
    V.BLOCK.code({ code: 'y = 2', lang: 'python' }, 'm', 'code:k', el); assert.strictEqual(built, 1, 'the first card the item drew is the one');
  } finally { global.VeraOut = saved; }
  assert(SRC.includes('let inner; this._kitNow = null;') && SRC.includes('const kitNow = this._kitNow; this._kitNow = null;'));
});

/* THE HEAD ROW, AS IT WAS DRAWN BEFORE S6 (canvas_element.js render, d15e612f): the reference the registry must give back */
const ROW_BEFORE = (s) => [].concat(s.views.length > 1 ? ['view'] : [], s.desk && s.win ? ['dock'] : [], s.desk ? ['lock'] : [],
  !s.maxed && !s.bid ? [s.desk ? 'pill' : 'fold'] : [], ['max', 'more', 'drop']);
t('the head row: VeraItem.controls gives the row the head drew before, on every kind, desk and column, maximised or not', () => {
  const VIEWS = [[], ['list'], ['list', 'cards'], ['code', 'structure', 'both']]; let cases = 0;
  KINDS.forEach((k) => VIEWS.forEach((views) => [true, false].forEach((desk) => [true, false].forEach((maxed) => [true, false].forEach((bid) => [true, false].forEach((win) => {
    const s = { views, desk, maxed, bid, win };
    const got = VI.controls(k, { views, window: desk && win, lock: desk, pill: !maxed && !bid, fold: !desk });
    assert.deepStrictEqual(got, ROW_BEFORE(s), k + ' ' + JSON.stringify(s)); cases++;
  }))))));
  assert(cases > 1000);
  /* the head draws exactly that row, each control as before */
  assert(SRC.includes("const row = VI.controls(b.type, { views: vw.views, window: desk && !!this._winKeepGet()[key], lock: desk, pill: !maxed && !bid, fold: !desk });"));
  assert(SRC.includes('const ctl = row.map((id) => HB[id]());'));
  const i = SRC.indexOf('const HB = {'), j = SRC.indexOf('const row = VI.controls', i);
  const B = SRC.slice(i, j); [["view:", "hb('hdview'"], ["dock:", "hb('dock'"], ["lock:", "hb('lock'"], ["pill:", "hb('pill'"], ["fold:", "hb('fold'"], ["max:", "hb('max'"], ["more:", "hb('hdmore'"], ["drop:", "hb('remove'"]]
    .forEach(([id, btn]) => assert(B.includes(id + ' () => ' + btn), id));
  /* the old API's answers are kept */
  assert.deepStrictEqual(VI.controls('code', {}), ['view', 'lock', 'pill', 'max', 'more', 'drop']);
  assert.deepStrictEqual(VI.controls('note', { narrow: true }), ['lock', 'pill', 'more', 'drop']);
});

/* THE MENU'S STANDARD ENTRIES, AS _hdMenu DREW THEM BEFORE S6 (d15e612f), in its order */
const MENU_BEFORE = (s) => [].concat((s.narrow || s.peek) && s.hView ? ['view'] : [], s.peek && s.hLock ? ['lock'] : [], (s.narrow || s.peek) && s.hMax ? ['max'] : [],
  !s.bid ? ['park', 'pin'] : [], ['size'], s.bid ? ['up', 'down'] : [], s.desk && s.placed ? ['arrange'] : [], !s.desk && !s.bid ? ['inplace'] : [],
  s.desk && !s.bid ? ['fold'] : [], s.grouped ? ['group'] : [], s.explode ? ['explode'] : [], s.edit ? ['edit'] : [], s.ctx ? ['context'] : [],
  s.cap ? ['refresh'] : [], s.peek ? ['drop'] : [], s.desk ? ['layer'] : [], ['dive']);
t('the more menu: VeraItem.menu gives the entries _hdMenu drew before, in its order, in every state', () => {
  const F = ['narrow', 'peek', 'hView', 'hLock', 'hMax', 'bid', 'desk', 'placed', 'grouped', 'explode', 'edit', 'ctx', 'cap'];
  let cases = 0;
  for (let m = 0; m < (1 << F.length); m += 7) {
    const s = {}; F.forEach((f, i) => { s[f] = !!(m & (1 << i)); });
    const o = { view: (s.narrow || s.peek) && s.hView, lock: s.peek && s.hLock, max: (s.narrow || s.peek) && s.hMax, park: !s.bid, pin: !s.bid, size: true,
      up: s.bid, down: s.bid, arrange: s.desk && s.placed, inplace: !s.desk && !s.bid, fold: s.desk && !s.bid, group: s.grouped, explode: s.explode,
      edit: s.edit, context: s.ctx, refresh: s.cap, drop: s.peek, layer: s.desk, dive: true };
    ['note', 'code', 'panel', 'result', 'diagram'].forEach((k) => {
      const got = VI.menu(k, { source: s.cap ? { cap: 'a.b' } : {} }, o).filter((id) => VI.MENU_ALL.includes(id));
      assert.deepStrictEqual(got, MENU_BEFORE(s), k + ' ' + JSON.stringify(s)); cases++; });
  }
  assert(cases > 500);
  /* no source, no Refresh, whatever the host says */
  assert(!VI.menu('note', {}, { refresh: true, size: true }).includes('refresh'));
  /* the body's registered actions follow the standard entries; the element draws the live ones (own) after them */
  const m = VI.menu('session', {}, { size: true }); assert.deepStrictEqual(m, ['size', 'tattach', 'tdetach', 'tshare']);
  const a = SRC.indexOf('    _hdMenu(key, btn) {'), b = SRC.indexOf('    _viewMenu(key, btn) {'); const M = SRC.slice(a, b);
  assert(M.includes("let h = VI.menu(b.type, { source: src }, o).map((id) => (R[id] ? R[id]() : '')).join('');"));
  const order = VI.MENU_ALL.filter((id) => M.includes('        ' + id + ': () => ')).map((id) => M.indexOf('        ' + id + ': () => '));
  assert.strictEqual(order.length, 19, 'every entry the canvas draws has its renderer'); assert.deepStrictEqual(order, order.slice().sort((x, y) => x - y));
  assert(M.indexOf("let h = VI.menu(") < M.indexOf('const own = (kk, host, lead)'), 'the body\'s own after the standard entries');
});

t('the dashboard tile\'s row is VeraItem.controls (owner O4): pill, max, drop - a section drop only', () => {
  assert.deepStrictEqual(VI.controls('widget', { views: [], lock: false, more: false, pill: true, max: true }), ['pill', 'max', 'drop']);
  assert.deepStrictEqual(VI.controls('widget', { views: [], lock: false, more: false, pill: false, max: false }), ['drop']);
  const i = DASH.indexOf('function itemHead(w)'); const B = DASH.slice(i, i + 2600);
  assert(B.includes("VI.controls('widget', { views: [], lock: false, more: false, pill: !sec, max: !sec })"));
  assert(B.includes("row.map(function (id) { return HB[id] ? HB[id]() : ''; }).join('')"));
});

t('the routes serve vera_item.js in front of the element and the dashboard; a second copy keeps the first\'s registry', () => {
  const CC = fs.readFileSync(path.join(ROOT, 'vera', 'canvas', 'canvas_capabilities.py'), 'utf8');
  const CP = fs.readFileSync(path.join(ROOT, 'vera', 'chat', 'chat_panels_capabilities.py'), 'utf8');
  assert(CC.includes('js = _VERA_ITEM_JS.read_text(encoding="utf-8") + "\\n" + js'));
  assert(CP.includes('return Response(content=pre + p.read_text(encoding="utf-8"),'));
  const ctx = { console }; ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(VI_SRC, ctx); ctx.VeraItem.registerBody('code', { views: ['code', 'both'] }); const first = ctx.VeraItem;
  vm.runInContext(VI_SRC, ctx); assert.strictEqual(ctx.VeraItem, first, 'the first copy stays'); assert.deepStrictEqual(Array.from(ctx.VeraItem.body('code').views), ['code', 'both']);
});

console.log(n + ' passed');
