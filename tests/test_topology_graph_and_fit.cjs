// 2026-09-28 (owner): the stack topology "more like the live operations view ... with the option to switch to the simple node/edge
// graph" · lists "not tall enough" (connections) · "some are too wide i.e. the scheduler" · "the entire composite widget has its own scrollbar"
//   node tests/test_topology_graph_and_fit.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const WE = R('vera/widgets/widget_element.js'), M = JSON.parse(R('vera/widgets/layouts/main.json'));
let fails = 0; const t = (name, cond, x) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (x || ''))); if (!cond) fails++; };
const tile = (id) => M.widgets.find((w) => w.record && w.record.id === id);
const tp = tile('topology-map').record;
t('the stack topology is the Vera graph - estate 3D, coloured by status - over topology.snapshot (edges as its links, kind as its type)', tp.form === 'vgraph' && tp.draw.mode === 'estate-3d' && tp.draw.colour === 'status' && tp.read.map.links === 'edges' && tp.read.map.type === 'kind' && tp.draw.body === 'record');
t('the lists have room: connections, mesh and recon six rows; the sandboxes six; the scheduler no longer the page\'s width', ['connections', 'mesh', 'recon'].every((id) => tile(id).span[1] === 6) && tile('sandboxes').span[1] === 6 && tile('scheduler').span[0] === 8);
const defined = {}; const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null, addEventListener() {} }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(R('vera/ui/iso.js'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
const rows = (n) => Array.from({ length: n }, (_, i) => ({ name: 'item-' + i, status: 'ok', value: i }));
const rec = { form: 'composite', layout: 'report', children: [{ slot: 'a', record: { form: 'rows', title: 'one', data: rows(30), draw: { limit: 12 } } }, { slot: 'b', record: { form: 'rows', title: 'two', data: rows(30), draw: { limit: 12 } } }] };
const html = W.draw('composite', null, 'm', { bare: true, record: rec, height: 300, width: 400 });
const hs = [...html.matchAll(/class="vw-slot vw-slot-block[^"]*"[\s\S]*?height:(\d+)px/g)].map((m) => +m[1]);
t('a node\'s own colour reaches the graph (addNode carries it; the nodeColor hook reads it)', R('vera/vera_graph.js').includes("if (typeof nodeSpec.color === 'string' && nodeSpec.color) n.color = nodeSpec.color;") && R('vera/vera_graph.js').includes("if (typeof node.color === 'string' && node.color) return node.color;"));
t('a record in an old page tile keeps none of its body cap', R('vera/chat/vera-dashboard.js').includes("'.dash-grid .w-body:has(> vera-widget){max-height:none!important}'"));
t('a report\'s blocks share the body when their natural heights exceed it (two twelve-row lists in 300 px)', WE.includes('let bhScale = 1; if (layout === \'report\' && o && o.height)') && /\+ \d+ more/.test(html), html.replace(/<style[\s\S]*?<\/style>/g, '').slice(0, 300));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
