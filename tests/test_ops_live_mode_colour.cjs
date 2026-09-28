// 2026-09-28 (owner): Live operations "isnt displaying the exploded modes - it flicks back to estate 3d" · "better color coding
// of the live operations widget" · composites "getting their content cut off at the bottom"
//   node tests/test_ops_live_mode_colour.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const WE = R('vera/widgets/widget_element.js'), VG = R('vera/vera_graph.js'), M = JSON.parse(R('vera/widgets/layouts/main.json'));
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('the mode is applied once, then the viewer\'s pick is kept (per widget)', /if \(el\._vgMode && cur && cur !== el\._vgMode\) \{ el\._vgMode = cur; try \{ localStorage\.setItem\(mk, cur\);/.test(WE) && !/try \{ if \(g\.setMode && \(g\.getMode \? g\.getMode\(\) : ''\) !== mode\) g\.setMode\(mode\); \} catch/.test(WE));
t('a node that carries its own colour keeps it (additive in Vera graph)', /if \(typeof node\.color === 'string' && node\.color\) return node\.color;/.test(VG));
t('colour by plane, a problem red or amber, with a key; Live operations asks for it', /function vgColour\(G\)/.test(WE) && /function vgKey\(host, k\)/.test(WE) && M.widgets.find((w) => w.record && w.record.id === 'ops-live').record.draw.colour === 'status');
t('content that outgrows its tile scrolls', /:host\(\[bare\]\) \.vw-root\{overflow-y:auto;/.test(WE));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
