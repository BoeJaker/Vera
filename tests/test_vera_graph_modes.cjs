// 2026-09-27 (owner): "the exploded view in the chat ui's graphing and the estate 3d and 2d mode to be modules or display
// modes of vera graph and defined as part of it"
//   node tests/test_vera_graph_modes.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const VG = R('vera/vera_graph.js'), M = R('vera/vera_graph_modes.js'), PY = R('vera/vera_graph_panels.py');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('the graph has a mode registry beside its panel registry', /function registerMode\(def\)\{/.test(VG) && /registerMode:  registerMode,/.test(VG) && /listModes:     listModes,/.test(VG));
t('every graph gets a Mode select and setMode/getMode', /instance\.setMode = function\(id\)\{/.test(VG) && /instance\.getMode = function\(\)\{ return _modeCur; \};/.test(VG) && /_modeSel\.className = 'vg-mode';/.test(VG));
t('a mode is told when the graph\'s data changes', /_modeHandle\.update\(\)/.test(VG));
t('the modes are part of the graph: it loads their module', /_ms\.src = _base \+ 'vera-graph-modes\.js'/.test(VG) && /@APP\.get\("\/ui\/vera-graph-modes\.js"/.test(PY));
t('exploded, estate 3D, estate 2D are registered', /G\.registerMode\(\{ id: 'exploded'/.test(M) && /id: 'estate-3d'/.test(M) && /id: 'estate-2d'/.test(M));
t('through the renderers the product has: <vera-exploded> and the one iso projection', /customElements\.get\('vera-exploded'\)/.test(M) && /ISO\.scene\(ISO\.proj\(tilt, azim, k\), boxes\)/.test(M));
t('2D is the projection seen straight down (the tops)', /estate\(90\)/.test(M) && /f\.k === 't'/.test(M));
t('hover lights a block and says what it is; click opens the node', /brightness\(1\.45\)/.test(M) && /api\.open\(byN\[id\]\)/.test(M));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
