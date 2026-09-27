// 2026-09-28 (owner): the default dashboard "cant see active jobs readily and routing to workers" - it opens on operations
//   node tests/test_ops_band.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const WE = R('vera/widgets/widget_element.js'), M = JSON.parse(R('vera/widgets/layouts/main.json'));
let fails = 0; const t = (name, cond, x) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (x || ''))); if (!cond) fails++; };
t('the overview opens on operations: in flight, routing, workers, the job stream', ['sec-ops', 'ops-inflight', 'ops-routing', 'ops-workers', 'ops-stream'].every((id, i) => M.widgets[i].record.id === id));
const defined = {}; const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null, addEventListener() {} }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(R('vera/ui/iso.js'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
const snap = { nodes: [{ label: 'loop-a', plane: 'work', status: 'run', inflight: 3 }, { label: 'Ollama', plane: 'runtime', status: 'ok', load: 8.6 }] };
const m = W.applyMap(snap, { rows: 'nodes', where: { plane: 'work' }, name: 'label' }, 'items');
t('where keeps the rows of one plane', Array.isArray(m) && m.length === 1 && m[0].name === 'loop-a', JSON.stringify(m));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
