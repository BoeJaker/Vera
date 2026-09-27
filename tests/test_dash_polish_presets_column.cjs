// 2026-09-27: tile titles keep their head; Data fabric + Stack monitor are harness presets; a column chart scales to what it draws
//   node tests/test_dash_polish_presets_column.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const VD = R('vera/chat/vera-dashboard.js'), H = R('vera/capability_orchestration.html'), WE = R('vera/widgets/widget_element.js');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('the record chip shows on hover, configuring or news', /vera-dashboard \.w-head \.vd-rec\{max-width:0;opacity:0/.test(VD) && /\.vd-rec:is\(\.sample,\.reading,\.failed,\.bad,\.checking\)/.test(VD));
t('Data fabric and Stack monitor are harness presets', /\{key:'fabric',name:'Data fabric'/.test(H) && /\{key:'sysmon',name:'Stack monitor'/.test(H));
t('a column chart scales to the window it draws', /hi = Math\.max\(\.\.\.vals\.slice\(from\)\) \|\| 1;/.test(WE));
{ const W = (global.window = global.window || {}); }
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
