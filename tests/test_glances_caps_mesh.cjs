// 2026-09-28: Cap Hub's and Mesh's empty panes are glances
//   node tests/test_glances_caps_mesh.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
const CH = R('vera/capabilities/cap_hub.html'), MP = R('vera/mesh/mesh_panel.html'), C = JSON.parse(R('vera/widgets/layouts/caps-glance.json')), MG = JSON.parse(R('vera/widgets/layouts/mesh-glance.json'));
t('Cap Hub: the glance in the empty pane', /<vera-dashboard layout="caps-glance"/.test(CH) && /vera-dashboard\.js/.test(CH) && C.widgets.length === 6);
t('Mesh: the glance in the empty inspector', /<vera-dashboard layout="mesh-glance"/.test(MP) && /vera-dashboard\.js/.test(MP) && MG.widgets.length === 2);
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
