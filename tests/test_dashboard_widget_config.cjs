// The dashboard's widget-config paths in vera/chat/vera-dashboard.js (UI redesign: the Dashboard · WidgetConfig ·
// WidgetSpec · Sizes boards): the size ladder the editor is handed, the form's sample by shape (VeraWidget.sample
// first), a record handed to the element with its sample only when its source cannot be read, and the text the
// behaviour hangs on (the surface's contract, ⚙ on every tile, a page tile retired under its record, drop on the
// grid, the edit persisted inline). The DOM paths are smoked in a browser (the slice's stand-in); this is the pure
// side.
//   node tests/test_dashboard_widget_config.cjs
'use strict';
const fs = require('fs'); const path = require('path'); const vm = require('vm'); const assert = require('assert');
const ROOT = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'vera', 'chat', 'vera-dashboard.js'), 'utf8');

function stubEl() { return { style: {}, dataset: {}, classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} }, setAttribute() {}, getAttribute() { return null; }, appendChild() {}, remove() {}, querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, insertAdjacentHTML() {} }; }
function boot(widget) {
  const window = { location: { hash: '' }, addEventListener() {}, innerWidth: 1440, innerHeight: 1000 };
  window.self = window; window.top = window; if (widget) window.VeraWidget = widget;
  const document = Object.assign(stubEl(), { readyState: 'complete', head: stubEl(), body: stubEl(), documentElement: stubEl(), createElement: () => stubEl(), getElementById: () => null });
  const ctx = { window, document, localStorage: { getItem: () => null, setItem() {}, removeItem() {} }, console, setTimeout, clearTimeout, fetch: () => Promise.reject(new Error('no net')) };
  vm.createContext(ctx); vm.runInContext(src, ctx, { filename: 'vera-dashboard.js' });
  return ctx.window.VeraDash;
}
const J = (x) => JSON.parse(JSON.stringify(x)); const eq = (a, b, m) => assert.deepStrictEqual(J(a), J(b), m);
let n = 0; const T = (name, fn) => { try { fn(); n++; console.log('PASS', name); } catch (e) { console.log('FAIL', name, '\n  ', e && e.stack || e); process.exitCode = 1; } };

const VD = boot(null);
T('the size ladder is the Sizes board: each size with the span it lands at', () => {
  eq(VD.sizeLadder(), [{ size: 's', span: [2, 1] }, { size: 'm', span: [4, 2] }, { size: 'l', span: [6, 2] }, { size: 'xl', span: [8, 4] }]);
  VD.sizeLadder().forEach((s) => assert.strictEqual(VD.sizeForSpan(s.span[0], s.span[1]), s.size, s.size + ' round-trips through the span rule'));
  eq(VD.ladderSizes(), ['s', 'm', 'l', 'xl'], 'the editor is handed the names (VeraWidgetConfig sizes)');
});
T('the dashboard keeps no sample of its own: without the element there is none (DO-0.3 - the element\'s is the one)', () => {
  ['trace', 'counter', 'thermo', 'donut', 'log', 'table', 'stepper', 'agenda', 'terminal', 'scatter', 'matrix', 'context_graph', 'panel', 'composite'].forEach((f) => assert.strictEqual(VD.sample(f), undefined, f));
  assert(!/var SHAPE_OF = |var SAMPLE = /.test(src), 'no SHAPE_OF / SAMPLE copies in the dashboard');
});
T('the element\'s own sample is the one the dashboard hands on', () => {
  const W = boot({ sample: (f) => (f === 'trace' ? [1, 2, 3] : null), forms: () => [{ id: 'oddform', shape: 'level' }], readable: () => false });
  eq(W.sample('trace'), [1, 2, 3], 'VeraWidget.sample');
  assert.strictEqual(W.sample('oddform'), undefined, 'nothing of its own for a form the element has no sample for');
});
T('a record is handed to the element marked sample when its source cannot be read - never with sample data (DO-0.3)', () => {
  const W = boot({ readable: (c) => /^obs\./.test(c), forms: () => [] });
  const live = W.withSample({ form: 'counter', source: 'obs.pending' }); assert.strictEqual(live.data, undefined, 'a readable source: the element reads it'); assert(!live.sample);
  const none = W.withSample({ form: 'counter', title: 'x' }); assert.strictEqual(none.data, undefined); assert.strictEqual(none.sample, true, 'no source: marked, the element draws its face');
  const write = W.withSample({ form: 'trace', source: 'code.write' }); assert(write.data === undefined && write.sample, 'a write-shaped source is not read: marked');
  const inline = W.withSample({ form: 'trace', data: [9, 8] }); eq(inline.data, [9, 8]); assert(!inline.sample, 'inline data stays');
  assert.strictEqual(W.withSample({ form: 'panel', panel: 'x' }).sample, undefined, 'a panel is not sampled');
  const E = boot({ readable: () => false, forms: () => [], sample: () => [1, 2] });
  const own = E.withSample({ form: 'trace', title: 'x' }); assert.strictEqual(own.data, undefined, 'an element with its own sample face draws it itself'); assert.strictEqual(own.sample, true, 'the tile still says sample');
});
T('the sample face only while configuring: edit mode or the tile\'s sheet open, else no-sample on the element (DO-0.3)', () => {
  ['function sampleGate(w, el) {', "if (state.editing || (w && w.dataset.configuring)) el.removeAttribute('no-sample');", "w.dataset.configuring = '1'; sampleGate(w);",
   "if (!state.editing) el.setAttribute('no-sample', '');", "sampleGate(w); recordChip(w); });"].forEach((x) => assert(src.includes(x), x));
});
T('the behaviour the browser smoke drives is in the text', () => {
  for (const s of [
    "window.VeraWidgetConfig.open({ mode: 'add', into: 'dashboard', title: 'Add a widget', sizes: ladderSizes(), templates: true, onValidating: st.onValidating, onValidated: st.onValidated })",
    "window.VeraWidgetConfig.open({ mode: 'edit', record: was, into: 'dashboard', title: was.title || wid, anchor: anchor || w, sizes: ladderSizes(), templates: true,",
    'function placeRecord(rec)', "if (rec.form === 'panel' && pid) return addWidget(pid)", 'return landed(addRecord(rec, {}));',
    'function openPanels()', 'function ensureCfg(w)', "b.className = 'w-iconbtn vd-cfg'", 'function configure(wid, anchor)', 'function applyRecord(wid, r)',
    'function drawTile(w, rec)', "holder.className = 'w-page'; holder.hidden = true;", "w.dataset.converted = '1';", 'if (holder) { while (holder.firstChild) body.insertBefore(holder.firstChild, holder); holder.remove(); }',
    'function onGridDragOver(e)', 'function onGridDrop(e)', "grid.classList.add('vd-drop-here')", 'function gridVars()',
    'if (ed) t.edited = true;', 'if (r && t.edited) state.edits[wid] = r;', 'var eff = state.edits[wid] || r;', 'if (eff) drawTile(w, eff);',
    'function openSheet(wid, rec)', "var shown = withSample(record); if (shown.sample) widget.dataset.sample = '1';",
    "openPanels: openPanels,", "placeRecord: placeRecord, configure: configure, applyRecord: applyRecord,",
    'function snapWidth(n)', '"moving · drop on the grid"', 'function ensureContextGraph()', 'function ladderSizes()', "s.src = '/ui/context_graph_element.js'"
  ]) assert(src.includes(s), s);
  assert(src.includes("if (window.VeraWidgetConfig && typeof window.VeraWidgetConfig.open === 'function') {\n        var p, st = stager();\n        try { p = window.VeraWidgetConfig.open({ mode: 'add'"), 'the surface is the loader when it is loaded (staged around widget.validate)');
  assert(src.includes('return openPanels();'), 'and the panel picker otherwise');
});
console.log(n + ' passed' + (process.exitCode ? ', some failed' : ''));
