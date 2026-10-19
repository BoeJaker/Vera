// Wave 1 (panel-derived): the Mesh panel's renderTransports() + renderList() moved once into <vera-mesh-nodes>
// (vera/mesh/mesh_nodes_element.js). The panel draws its node list through it with `select` (a row calls its own loadNode());
// the desk mounts it for a mesh.nodes answer, seeded, display only, with `detail` (board / firmware / address / signal, what the
// desk's table showed). tests/fixtures/mesh_nodes_parity.json holds what the panel's own code drew before, at a fixed clock.
'use strict';
const fs = require('fs'); const path = require('path'); const vm = require('vm'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const E = R('vera/mesh/mesh_nodes_element.js'), P = R('vera/mesh/mesh_panel.html'), WE = R('vera/widgets/widget_element.js'), MC = R('vera/mesh/mesh_capabilities.py');
const FX = JSON.parse(R('tests/fixtures/mesh_nodes_parity.json'));
class D extends Date { static now() { return FX.now; } }
const ctx = { window: {}, HTMLElement: class {}, customElements: { get: () => false, define() {} }, Date: D, Object, String, Math, JSON }; vm.createContext(ctx); vm.runInContext(E, ctx);
const M = ctx.window.VeraMeshNodes;
t('with select the list reads as the panel drew it, byte for byte (every case)', FX.cases.every((c, i) => M.listHtml(c.nodes, c.q, c.sel, { select: true }) === FX.html[i].list));
t('the transports bar reads as the panel drew it (every case)', FX.cases.every((c, i) => M.txHtml(c.transports) === FX.html[i].tx));
const desk = M.listHtml(FX.cases[0].nodes, '', null, { detail: true });
t('without select (the desk) no row calls loadNode', !desk.includes('onclick') && !desk.includes('loadNode'));
t('with detail each node shows its role, board, firmware, address and signal; a node without them adds nothing',
  desk.includes('display · esp32-s3 · fw 1.4.2 · 192.168.0.61 · -58 dBm') && desk.includes('relay · 10.0.0.9 · -81 dBm')
  && M.listHtml([{ node_id: 'x', status: 'new' }], '', null, { detail: true }) === M.listHtml([{ node_id: 'x', status: 'new' }], '', null, {}));
t('the desk names its own empty text', M.listHtml([], '', null, { empty: 'No mesh nodes registered' }) === '<span class="muted">No mesh nodes registered</span>');
t('the panel draws its list and transports through the element; its own copies are gone',
  P.includes('<vera-mesh-nodes id="nodeList" refresh="0" select list-only></vera-mesh-nodes>') && P.includes('<script src="/ui/elements/mesh_nodes.js"></script>')
  && P.includes("document.getElementById('nodeList').seed({nodes:_nodes, query:document.getElementById('nodeSearch').value||'', selected:_sel});")
  && P.includes("document.getElementById('txBar').innerHTML = VeraMeshNodes.txHtml(tx);")
  && !P.includes("const mods=Object.keys(n.modules||{})") && !P.includes("const order=[['http','HTTP']") && P.includes('async function loadNode(id, quiet){'));
t('the element is served and the widget element form knows it', MC.includes('"/ui/elements/mesh_nodes.js"') && WE.includes("'vera-mesh-nodes': 'mesh_nodes'"));
const decl = JSON.parse(R('tests/fixtures/presentation_declared.json'))['mesh.nodes'] || {};
t('mesh.nodes opens as the element with detail (its table kept as a view)', decl.form === 'element' && decl.draw && decl.draw.tag === 'vera-mesh-nodes'
  && decl.draw.attrs && decl.draw.attrs.detail === '1' && decl.map && decl.map.rows === 'nodes' && Array.isArray(decl.draw.columns));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);
