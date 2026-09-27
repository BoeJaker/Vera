// 2026-09-27: an empty pane is a glance - the Fabric panel with no dataset chosen shows the fabric at a glance
//   node tests/test_fabric_glance.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const FP = R('vera/fabric/fabric_panel.html'), G = JSON.parse(R('vera/widgets/layouts/fabric-glance.json'));
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('the empty pane holds the glance dashboard', /<vera-dashboard layout="fabric-glance"/.test(FP) && /<script src="\/ui\/vera-dashboard\.js" data-fab-dash="1"><\/script>/.test(FP));
t('the glance reads only the light caps', G.widgets.length >= 8 && G.widgets.every((w) => !/fabric\.(sources|graphs\.snapshot)$/.test(w.record.source)));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
