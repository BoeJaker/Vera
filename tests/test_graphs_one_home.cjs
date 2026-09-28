// 2026-09-27 (owner): the memory graph "in-keeping with the new design - blocks off = background transparent" · mermaid and
// the chat's graphs "in vera graph ... so they all have one home"
//   node tests/test_graphs_one_home.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const VG = R('vera/vera_graph.js'), M = R('vera/vera_graph_modes.js'), CHAT = R('vera/chat/chat_panel.html');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('every Vera graph is see-through with blocks off', /html\[data-blocks="off"\] \.vg-canvas-area,html\[data-blocks="off"\] \.vg-canvas,html\[data-blocks="off"\] \.vg-mode-host\{background:transparent!important/.test(VG));
t('the mode select keeps a sensible width', /max-width:150px;flex:0 0 auto';/.test(VG));
t('Mermaid is a mode of the graph, through <vera-mermaid>', /G\.registerMode\(\{ id: 'mermaid',/.test(M) && /customElements\.get\('vera-mermaid'\)/.test(M) && /lines = \['graph LR'\]/.test(M));
t('the chat\'s memory graph is a Vera graph', /_memVG=veraUI\.Graph\.create\(host, \{ apiBase:BASE, height:'fill'/.test(CHAT) && /_memVG\.load\(\{ nodes:nodes\.map/.test(CHAT));
t('its old drawing is only the fallback', /if\(window\.veraUI&&veraUI\.Graph\)\{ if\(_memVGDraw\(nodes, fe\)\) return; \}/.test(CHAT) && /#paneMemGraph\.vg #memGraphSvg,#paneMemGraph\.vg #memLegend\{display:none\}/.test(CHAT));
t('Fit fits the graph', /if\(_memVG&&document\.getElementById\('paneMemGraph'\)\?\.classList\.contains\('vg'\)\)\{ try\{ _memVG\.fit\(\);/.test(CHAT));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
