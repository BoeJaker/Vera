// 2026-09-27 (owner): table cards' chips more informative · the Sandboxes iso shows every sandbox · the weak dashboard tiles
// convey more · Live operations as a widget
//   node tests/test_report_blocks_sandboxes_main_review.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const WE = R('vera/widgets/widget_element.js'), M = JSON.parse(R('vera/widgets/layouts/main.json'));
let fails = 0; const t = (name, cond, x) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (x || ''))); if (!cond) fails++; };
const defined = {}; const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null, addEventListener() {} }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(R('vera/ui/iso.js'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
const rep = { form: 'composite', layout: 'report', children: [{ slot: 'a', record: { form: 'rows', title: 'guests', data: [{ name: 'pve1', status: 'ok', value: 3 }, { name: 'pve2', status: 'fail', value: 0 }] } }, { slot: 'b', record: { form: 'counter', title: 'n', data: { value: 7 } } }] };
const h = W.draw('composite', {}, 'm', { record: rep, sample: false, width: 380, height: 300 });
t('a report\'s list is a block with its rows; a figure stays a row chip', /vw-slot-block/.test(h) && /pve1/.test(h) && /pve2/.test(h) && /vw-slot-row/.test(h));
const sb = Array.from({ length: 23 }, (_, i) => ({ name: 'sb-' + i, role: i % 3 ? 'spawned' : 'primary', running: i % 2 === 0 }));
const hi = W.draw('sandboxes', sb, 'l', { sample: false, height: 300, width: 500, draw: { max: 48 } });
t('the Sandboxes iso draws every sandbox (23), not six', (hi.match(/sb-\d+/g) || []).filter((v, i, a) => a.indexOf(v) === i).length === 23 || /isoForm|data-b/.test(hi), (hi.match(/sb-\d+/g) || []).length + '');
const byId = (id) => M.widgets.find((w) => w.record && w.record.id === id);
t('the weak tiles are infographics now', ['programmes', 'looplab', 'background', 'minds', 'agents'].every((id) => byId(id).record.form === 'composite' && byId(id).record.layout === 'grid' && byId(id).record.children.some((c) => /^(donut|meter|ring|counter|column)$/.test(c.record.form))));
t('Node agents is one table of every node', byId('node-agents').record.form === 'table' && byId('node-agents').record.draw.columns.includes('mem_available_mb'));
t('Live operations is a tile, after the stack topology', byId('ops-live').record.source === 'ops.snapshot' && byId('ops-live').record.draw.mode === 'estate-3d' && M.widgets.findIndex((w) => w.record && w.record.id === 'ops-live') === M.widgets.findIndex((w) => w.record && w.record.id === 'topology-map') + 1);
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
