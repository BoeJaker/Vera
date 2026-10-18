// An app window on the desk (DA-05b, DESK-SPINE-DESIGN §4.2): an app from the catalogue as an item - opened (manifest,
// skeleton, read), drawn like a capability's result, refreshed only while seen, restored from its content.
//   node tests/test_app_window.cjs
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const V = require(FILE); const S = fs.readFileSync(FILE, 'utf8');
let n = 0; const t = (name, fn) => { fn(); n++; console.log('ok -', name); };

t('opening: a skeleton, and the element is asked to open it once', () => {
  const opened = []; const el = { _apps: {}, _appOpen: (k, c) => opened.push([k, c.app]) };
  const h = V.BLOCK.app({ app: 'markets.overview' }, 'm', 'app:x', el);
  assert(/vc-app-sk/.test(h) && /opening markets\.overview/.test(h)); assert.deepStrictEqual(opened, [['app:x', 'markets.overview']]);
  V.BLOCK.app({ app: 'markets.overview' }, 'm', 'app:x', Object.assign(el, { _apps: { 'app:x': { opening: true } } })); assert.strictEqual(opened.length, 1, 'not twice');
});
t('read: drawn as a capability\'s result; its head line says the panel and when it was read; Refresh and Open the panel', () => {
  const el = { _apps: { 'app:y': { manifest: { panel: 'markets', title: 'Overview', read: { cap: 'markets.overview' } }, cap: 'markets.overview', args: {}, result: { rows: [{ a: 1 }, { a: 2 }] }, at: Date.now() - 4000 } } };
  const h = V.BLOCK.app({ app: 'markets.overview' }, 'm', 'app:y', el);
  assert(/class="vc-app vc-app-rowsapp"/.test(h) && /data-app="markets.overview"/.test(h) && !/vc-app-sk/.test(h) && /data-app-q="1"/.test(h), 'rows are a list to work');
  const d = el._decl['app:y']; assert(/markets · read 4 s ago/.test(d.sub)); assert.deepStrictEqual(d.acts.map((a) => a.act), ['appread', 'appcsv', 'appopen']);
});
t('the manifest\'s own view when the generic mapping has only the raw tree; an error says what failed; a missing argument says what it needs', () => {
  assert(S.includes("if ((!v || v.kind === 'json') && mk && BLOCK[mk] && mk !== 'app' && st.result && typeof st.result === 'object') v = { kind: mk, content: st.result };"));
  const el = { _apps: { 'app:z': { manifest: { panel: 'p' }, error: 'boom' } } }; assert(/vc-res-err">boom/.test(V.BLOCK.app({ app: 'a' }, 'm', 'app:z', el)));
  assert(S.includes("st.waiting = m.title + ' needs ' + miss.join(', ');"));
});
t('only a read-shaped source is read on its own; refresh only while seen, one read in flight', () => {
  assert(S.includes("if (readShaped(st.cap)) return this._appRead(key); st.waiting = st.cap + ' is not a read - press Refresh to run it';"));
  assert(S.includes("if (!forced && !readShaped(st.cap)) return;") && S.includes('if (!st || !st.cap || st.reading) return;'));
  assert(S.includes("if (!c || !c.offsetParent || c.classList.contains('desk-chip') || c.classList.contains('cmp-behind') || c.classList.contains('compact') || c.classList.contains('ufold')) return;"));
  assert(S.includes("if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;"));
});
t('it is a block type the server keeps, named by its manifest, with its own glyph', () => {
  const P = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_capabilities.py'), 'utf8');
  assert(/"app":\s+\{"desc": "An APP from the catalogue/.test(P) && P.includes('"content": "{app:str(app id), args?:obj, title?:str}"'));
  assert(V.KIND_GLYPH.app === '▦' && S.includes('if (b && b.content && !b.content.title && m.title) b.content.title = m.title;'));
});
t('a pill wins over a hand placement (the pill control did nothing on a card placed or sized by hand)', () => {
  assert(S.includes('const pilledKey = !lk && !!this._pillGet()[key];') && S.includes('const us = !lk && !pilledKey && up[key]') && S.includes('const p = (!pilledKey && userAt(key)) ||'));
});
/* DA-05c: the app minimum on a list */
t('the rows of an answer: an array of records, or the one array an answer holds; nothing else', () => {
  assert.strictEqual(V.appRows([{ a: 1 }]).length, 1); assert.strictEqual(V.appRows({ events: [{ a: 1 }, { a: 2 }] }).length, 2);
  assert.deepStrictEqual(V.appRows({ rows: [] }), []); assert.strictEqual(V.appRows({ value: 3 }), null); assert.strictEqual(V.appRows([1, 2]), null);
  assert.strictEqual(V.appRows({ meta: 1, quotes: [{ s: 'A' }] }).length, 1, 'the one array of records under any name');
});
t('filter keeps the rows with every word; a column sorts up, down, then as read; CSV of what is shown', () => {
  const rows = [{ sym: 'SPY', chg: 1.2 }, { sym: 'QQQ', chg: -0.5 }, { sym: 'DIA', chg: 0.3, note: 'dow' }];
  assert.deepStrictEqual(V.appView(rows, { q: 'q' }).list.map((x) => x.x.sym), ['QQQ']);
  assert.deepStrictEqual(V.appView(rows, { sort: ['chg', 'asc'] }).list.map((x) => x.x.sym), ['QQQ', 'DIA', 'SPY']);
  assert.deepStrictEqual(V.appView(rows, { sort: ['chg', 'desc'] }).list.map((x) => x.x.sym), ['SPY', 'DIA', 'QQQ']);
  const v = V.appView(rows, { q: 'dow' }); assert.strictEqual(V.appCsv(v.cols, v.list), 'sym,chg,note\nDIA,0.3,dow');
});
t('table or cards; a row opened in a drawer with the detail app; the count says what the filter left', () => {
  const st = { rows: [{ sym: 'SPY' }, { sym: 'QQQ' }], manifest: { detail: { app: 'markets.symbol', args: { symbol: '{row.sym}' } } }, ui: { q: '', sel: 1, detail: 1 } };
  const t1 = V.appRowsHtml(st, 'table'); assert(/data-app-sort="sym"/.test(t1.html) && /<tr data-app-row="1" class="sel">/.test(t1.html) && /vc-app-dt/.test(t1.html) && /data-app-detail="1"/.test(t1.html) && t1.count === '2 rows');
  const t2 = V.appRowsHtml(Object.assign({}, st, { ui: { q: 'zzz' } }), 'cards'); assert(/No row has "zzz"/.test(t2.html) && t2.count === '0 of 2');
  assert(/vc-app-c sel/.test(V.appRowsHtml(st, 'cards').html));
});
t('the person works it in place: typing filters, a head sorts, a row opens, keys move; Copy CSV; the detail app goes on the desk', () => {
  ["body.addEventListener('input', (ev) => { const t = ev.target; if (!t || !t.matches || !t.matches('[data-app-q]')) return;", "a.ui.sort = !so || so[0] !== k ? [k, 'asc'] : so[1] === 'asc' ? [k, 'desc'] : null;",
   "if (ev.key === '/' && !q)", "if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp')", "if (ev.key === 'Enter' && a.ui.sel != null)", "if (act === 'appcsv')",
   "return this.call('canvas.add', { kind: 'app', key: 'app:' + dm.app + (sig ? ':' + sig : ''), content: { app: dm.app, args } });", "if (rows) rows.innerHTML = rh.html;"].forEach((x) => assert(S.includes(x), x));
});
console.log(n + ' passed');