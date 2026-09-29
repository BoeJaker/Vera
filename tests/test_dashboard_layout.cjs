// The layout record's pure parts in vera/chat/vera-dashboard.js (UI redesign M5): the span → size rule, dense flow,
// arrange, and the legacy → record migration — held to the same fixture as vera/widgets/migrate_layouts.py.
//   node tests/test_dashboard_layout.cjs
'use strict';
const fs = require('fs'); const path = require('path'); const vm = require('vm'); const assert = require('assert');
const ROOT = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'vera', 'chat', 'vera-dashboard.js'), 'utf8');

// the script only needs a window and a document to define itself; nothing is rendered here
function stubEl() { return { style: {}, dataset: {}, classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} }, setAttribute() {}, getAttribute() { return null; }, appendChild() {}, remove() {}, querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, insertAdjacentHTML() {} }; }
const window = { location: { hash: '' }, addEventListener() {}, innerWidth: 1440, innerHeight: 1000 };
window.self = window; window.top = window;
const document = Object.assign(stubEl(), { readyState: 'complete', head: stubEl(), body: stubEl(), documentElement: stubEl(), createElement: () => stubEl(), getElementById: () => null });
const ctx = { window, document, localStorage: { getItem: () => null, setItem() {}, removeItem() {} }, console, setTimeout, clearTimeout, fetch: () => Promise.reject(new Error('no net')) };
vm.createContext(ctx); vm.runInContext(src, ctx, { filename: 'vera-dashboard.js' });
const VD = ctx.window.VeraDash;
assert(VD && typeof VD.init === 'function', 'VeraDash defined');

// values made inside the vm context carry its own Array/Object prototypes; compare by value
const J = (x) => JSON.parse(JSON.stringify(x)); const eq = (a, b, m) => assert.deepStrictEqual(J(a), J(b), m);
let n = 0; const T = (name, fn) => { try { fn(); n++; console.log('PASS', name); } catch (e) { console.log('FAIL', name, '\n  ', e && e.stack || e); process.exitCode = 1; } };

T('the span → size rule is the Sizes board (and widget_record.size_for_span)', () => {
  eq([1, 2, 3, 4, 6, 8, 12].map((w) => VD.sizeForSpan(w, 1)), ['xs', 's', 's', 'm', 'l', 'xl', 'xl']);
  assert.strictEqual(VD.sizeForSpan(6, 3), 'xl', 'more rows on a 6-wide add the table');
  assert.strictEqual(VD.sizeForSpan(4, 4), 'm');
});
T('a record asks for a span: its frame.span, else its size, a panel the loader cell', () => {
  eq(VD.spanFor({ frame: { span: [8, 4] } }), [8, 4]);
  eq(VD.spanFor({ form: 'panel' }), [6, 3]);
  eq(VD.spanFor({ size: 's' }), [2, 1]);
  eq(VD.spanFor({}), [4, 2]);
});
T('dense flow gives at = [col, row] top row first, a hidden tile no place', () => {
  const out = VD.flow([{ span: [4, 1] }, { span: [6, 2] }, { span: [6, 3] }, { span: [2, 1], hidden: true }, { span: [12, 2] }, { span: [4, 2] }], 12);
  eq(out.map((t) => t.at), [[0, 0], [4, 0], [0, 2], null, [0, 5], [6, 2]]);
});
T('arrange packs the order row-major and keeps hidden tiles last', () => {
  const out = VD.arrange([{ id: 'a', span: [4, 1] }, { id: 'b', span: [6, 2] }, { id: 'c', span: [6, 3] }, { id: 'h', span: [2, 1], hidden: true }, { id: 'd', span: [12, 2] }, { id: 'e', span: [4, 2] }], 12);
  eq(out.map((t) => t.id), ['a', 'b', 'c', 'e', 'd', 'h']);
});

// the fixture vera/widgets/migrate_layouts.py is held to as well
const LEGACY = { order: ['b', 'a', 'dyn-system-monitor'], hidden: ['c'], sizes: { a: { w: 6, h: 2 } },
  dynamic: { 'dyn-system-monitor': { panelId: 'system-monitor', wid: 'dyn-system-monitor' }, 'rec-x': { record: { form: 'counter', source: 'obs.pending', title: 'Pending', read: { refresh: '5s' } }, wid: 'rec-x' } } };
const PAGE = [{ id: 'a', span: [2, 1] }, { id: 'b', span: [4, 1] }, { id: 'c', span: [2, 1] }, { id: 'd', span: [12, 2] }];
T('a legacy vera.dash.<key> migrates one to one: order → at, sizes → span, hidden → hidden, dynamic → records', () => {
  const L = VD.migrate(LEGACY, { key: 'main', page: PAGE });
  assert.strictEqual(L.v, 2); assert.strictEqual(L.key, 'main'); assert.strictEqual(L.layout, 'default');
  eq(L.grid, { cols: 12, row: 58, gap: 10, widths: [2, 3, 4, 6, 8, 12] });
  const ids = L.widgets.map((t) => typeof t.record === 'string' ? t.record : t.record.id);
  eq(ids, ['b', 'a', 'dyn-system-monitor', 'c', 'd', 'rec-x']);
  eq(L.widgets.map((t) => t.span), [[4, 1], [6, 2], [6, 3], [2, 1], [12, 2], [4, 2]]);
  eq(L.widgets.map((t) => t.at), [[0, 0], [4, 0], [0, 2], null, [0, 5], [6, 2]]);
  eq(L.widgets.map((t) => t.hidden), [false, false, false, true, false, false]);
  const dyn = L.widgets[2].record;
  eq({ id: dyn.id, form: dyn.form, panel: dyn.panel, source: dyn.source }, { id: 'dyn-system-monitor', form: 'panel', panel: 'system-monitor', source: 'panel:system-monitor' });
  const rx = L.widgets[5].record;
  assert.strictEqual(rx.id, 'rec-x'); assert.strictEqual(rx.form, 'counter'); assert.strictEqual(L.widgets[5].refresh, '5s');
  assert.strictEqual(typeof L.widgets[0].record, 'string', 'a page tile is named by id; its record is in the layout file');
});
T('an empty legacy state is the page as written', () => {
  const L = VD.migrate({}, { key: 'dream', page: PAGE });
  eq(L.widgets.map((t) => t.record), ['a', 'b', 'c', 'd']);
  eq(L.widgets.map((t) => t.at), [[0, 0], [2, 0], [6, 0], [0, 1]]);
});
T('a legacy order naming tiles the page no longer has drops them', () => {
  const L = VD.migrate({ order: ['gone', 'b'] }, { key: 'k', page: PAGE });
  eq(L.widgets.map((t) => t.record), ['b', 'a', 'c', 'd']);
});
console.log(n + ' passed' + (process.exitCode ? ', some failed' : ''));
