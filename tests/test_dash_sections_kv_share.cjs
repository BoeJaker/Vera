// 2026-09-28: a section in <vera-dashboard> is a heading; four key/values get a list's share of a composite
//   node tests/test_dash_sections_kv_share.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const VD = R('vera/chat/vera-dashboard.js'), WE = R('vera/widgets/widget_element.js');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('a section tile is a heading in the element', /vera-dashboard \.widget\.w-section\{background:transparent!important/.test(VD) && /vera-dashboard \.widget\.w-section \.w-body\{display:none!important\}|\.w-section \.w-body\{display:none!important\}/.test(VD));
t('four key/values get a list\'s share', /r0\.read\.map\.keys\.length >= 4\) return 1\.15;/.test(WE));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
