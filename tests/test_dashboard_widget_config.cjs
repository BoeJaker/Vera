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
T('without the element, the sample is the shape\'s own: every drawn family has one', () => {
  assert(Array.isArray(VD.sample('trace')) && VD.sample('trace').length > 10, 'series');
  assert.strictEqual(typeof VD.sample('counter').value, 'number', 'level');
  assert(Object.keys(VD.sample('thermo')).length >= 3 && Object.values(VD.sample('thermo')).every((v) => typeof v === 'number'), 'values');
  assert(VD.sample('donut').running > 0, 'parts');
  assert(VD.sample('log').every((r) => r.t && r.text), 'events');
  assert(VD.sample('table').every((r) => r.name), 'items');
  assert(VD.sample('stepper').stages.length >= 4, 'stages');
  assert(VD.sample('agenda').every((r) => r.when && r.title), 'calendar');
  assert.strictEqual(typeof VD.sample('terminal'), 'string', 'string');
  assert(VD.sample('scatter').every((p) => 'x' in p && 'y' in p), 'points');
  assert(VD.sample('matrix').ct126.cpu, 'matrix');
  const g = VD.sample('context_graph'); assert(g.nodes.length >= 6 && g.rels.length >= 3 && g.links.length === g.rels.length, 'graph: nodes, rels (the context graph) and links (pipes)');
  assert(new Set(g.nodes.map((x) => x.family)).size >= 5, 'the galaxy sample has families to ring');
  assert(g.nodes.some((x) => x.included === false), 'one related-not-injected record (hollow)');
  assert.strictEqual(VD.sample('panel'), undefined, 'a panel has no sample'); assert.strictEqual(VD.sample('composite'), undefined, 'nor a composite');
});
T('the element\'s own sample and forms win when they are there', () => {
  const W = boot({ sample: (f) => (f === 'trace' ? [1, 2, 3] : null), forms: () => [{ id: 'oddform', shape: 'level' }], readable: () => false });
  eq(W.sample('trace'), [1, 2, 3], 'VeraWidget.sample first');
  assert.strictEqual(typeof W.sample('oddform').value, 'number', 'a form the element names by shape falls to the shape\'s sample');
});
T('a record is handed to the element with its sample only when its source cannot be read', () => {
  const W = boot({ readable: (c) => /^obs\./.test(c), forms: () => [] });
  const live = W.withSample({ form: 'counter', source: 'obs.pending' }); assert.strictEqual(live.data, undefined, 'a readable source: the element reads it'); assert(!live.sample);
  const none = W.withSample({ form: 'counter', title: 'x' }); assert.strictEqual(none.data.value, 62); assert.strictEqual(none.sample, true, 'no source: the sample, said so');
  const write = W.withSample({ form: 'trace', source: 'code.write' }); assert(Array.isArray(write.data) && write.sample, 'a write-shaped source is not read: the sample');
  const inline = W.withSample({ form: 'trace', data: [9, 8] }); eq(inline.data, [9, 8]); assert(!inline.sample, 'inline data stays');
  assert.strictEqual(W.withSample({ form: 'panel', panel: 'x' }).sample, undefined, 'a panel is not sampled');
  const cg = W.withSample({ form: 'context_graph', title: 'Context galaxy' }); assert(cg.data.nodes.length && cg.sample, 'the context graph without a session draws its sample');
  const E = boot({ readable: () => false, forms: () => [], sample: () => [1, 2] });
  const own = E.withSample({ form: 'trace', title: 'x' }); assert.strictEqual(own.data, undefined, 'an element with its own sample face draws it itself'); assert.strictEqual(own.sample, true, 'the tile still says sample');
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
