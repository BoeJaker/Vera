// DA-04c (d): netscan on the desk - a scan's run item is the table of hosts with their open ports; the network map is an
// app (exec-panel.netmap) reading netscan.graph, drawn by the estate graph. What it needed: the graph reads a
// cytoscape-shaped answer ({data:{...}}), the chat's table keeps a list of plain values as a column and follows a
// declaration's draw.columns, a widget drawn inside an app is fed the app's answer (not its sample face), and the
// estate graph inside the canvas gets its styles.
//   node tests/test_netscan_desk.cjs
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');
const R = path.join(__dirname, '..');
const WE = fs.readFileSync(path.join(R, 'vera', 'widgets', 'widget_element.js'), 'utf8');
const H = fs.readFileSync(path.join(R, 'vera', 'chat', 'chat_panel.html'), 'utf8');
const C = fs.readFileSync(path.join(R, 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const X = fs.readFileSync(path.join(R, 'vera', 'execution', 'exec_capabilities.py'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// the graph reads netscan.graph's cytoscape shape
{ const a = WE.indexOf('  const cyUnwrap = '), b = WE.indexOf('  R.vgraph = ', a); const ctx = { String, Array, Object }; vm.createContext(ctx);
  vm.runInContext(WE.slice(a, b) + ';this.g=toVeraGraph;', ctx);
  const G = ctx.g({ nodes: [{ data: { id: 'h1', label: '10.0.0.1', type: 'NetHost' } }, { data: { id: 'p22', label: '22', type: 'NetPort' } }], edges: [{ data: { id: 'e1', source: 'h1', target: 'p22', label: 'EXPOSES' } }] });
  t('cytoscape nodes and edges are read as their data', G.nodes.length === 2 && G.nodes[0].id === 'h1' && G.nodes[0].label === '10.0.0.1' && G.nodes[0].type === 'NetHost' && G.edges.length === 1 && G.edges[0].from === 'h1' && G.edges[0].to === 'p22' && G.edges[0].rel === 'EXPOSES', JSON.stringify(G));
  const P = ctx.g({ nodes: [{ id: 'a', label: 'A' }, { id: 'b' }], links: [{ source: 'a', target: 'b' }] });
  t('...plain nodes and links as before', P.nodes.length === 2 && P.edges.length === 1 && P.edges[0].from === 'a'); }

// the chat's table: a list of plain values is a column; draw.columns choose and order them
{ const a = H.indexOf('  function _cvPresented(capName, result, args, opts){'), b = H.indexOf('  // the one entry point', a);
  const decl = { 'netscan.lan.scan': { kind: 'table', form: 'table', map: { rows: 'alive' }, draw: { columns: ['ip', 'hostname', 'open_ports', 'alive_via', 'mac'] } }, 'x.list': { kind: 'table', form: 'table' } };
  const W = { presentationOf: (c) => decl[c] || null, present: (cap, res) => ({ element: 'table', title: cap, records: [{ form: 'table', data: cap === 'netscan.lan.scan' ? { rows: res.alive } : res }] }), dataFor: (d) => d, mapped: (r, f, d) => d };
  const ctx = { window: { VeraWidget: W }, _cvCalMonth: () => '', Object, String, Array, JSON }; vm.createContext(ctx);
  vm.runInContext(H.slice(a, b) + ';this.f=_cvPresented;', ctx);
  const it = ctx.f('netscan.lan.scan', { cidr: '10.0.0.0/29', alive: [{ ip: '10.0.0.1', hostname: 'gw', mac: 'aa', open_ports: [22, 80, 443], alive_via: 'tcp' }, { ip: '10.0.0.4', hostname: 'nas', mac: '', open_ports: [], alive_via: 'icmp' }] }, {});
  t('a scan is its hosts, in the declared columns, ports joined', !!it && it.kind === 'table' && JSON.stringify(it.content.columns) === '["ip","hostname","open_ports","alive_via","mac"]' && it.content.rows[0][2] === '22, 80, 443' && it.content.rows[1][2] === '', JSON.stringify(it && it.content));
  const u = ctx.f('x.list', [{ name: 'a', tags: ['x', 'y'], meta: { k: 1 } }], {});
  t('undeclared columns: a list of plain values kept, an object left out', !!u && JSON.stringify(u.content.columns) === '["name","tags"]' && u.content.rows[0][1] === 'x, y'); }

// a widget inside an app is fed the app's answer; the estate graph there gets its styles
t('an app and a composite part record what they drew under their sub-key', C.includes("(el._nested || (el._nested = {}))[key + ':app'] = v.content || {};") && C.includes("(el._nested || (el._nested = {}))[key + ':part:' + p.as] = v.content || {};"));
t('...and the live layer reads it when no item has that key', C.includes("_contentOf(key) { const b = this._blockOf(key); if (!b) { const n = this._nested && this._nested[key]; return n ? Object.assign({}, n) : null; }"));
t('a vgraph widget on the desk adopts the graph styles', C.includes("if (rc && /vgraph/.test(String((rc.record || rc).form || ''))) this._adoptGraphCss();"));

// the declarations and the app
t('netscan.lan.scan declares its hosts as a table with their ports', X.includes('contract={"presentation": {"kind": "table", "form": "table", "map": {"rows": "alive"},') && X.includes('"draw": {"columns": ["ip", "hostname", "open_ports", "alive_via", "mac"]}}},'));
t('netscan.graph declares the graph, minimal chrome', X.includes('contract={"presentation": {"kind": "graph", "form": "vgraph", "draw": {"chrome": "min"}}},'));
const app = JSON.parse(fs.readFileSync(path.join(R, 'vera', 'apps', 'exec-panel.json'), 'utf8')).find((a) => a.id === 'exec-panel.netmap');
t('the Network map app reads netscan.graph and can scan a network', !!app && app.read.cap === 'netscan.graph' && app.view.kind === 'graph' && app.actions[0].cap === 'netscan.lan.scan' && app.actions[0].fields[0].key === 'cidr' && app.actions[0].fields[0].required === true);
t('...its read opens without a press', /\^netscan\\\.graph\$/.test(C));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
