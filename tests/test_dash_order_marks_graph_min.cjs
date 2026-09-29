// 2026-09-28: the overview in reading order (no holes); yes/no as marks; a key/value set of four a whole row; a tile's graph alone
//   node tests/test_dash_order_marks_graph_min.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const WE = R('vera/widgets/widget_element.js'), M = JSON.parse(R('vera/widgets/layouts/main.json'));
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
const at = M.widgets.filter((w) => Array.isArray(w.at)).map((w) => w.at[1] * 100 + w.at[0]);
t('the overview lists its tiles row by row', at.every((v, i) => !i || v >= at[i - 1]));
t('a yes/no is a mark, not the word', /yn = typeof v === 'boolean'/.test(WE));
t('four or more key/values take a whole row', /f === 'kv' && !!\(r\.read && r\.read\.map && Array\.isArray\(r\.read\.map\.keys\) && r\.read\.map\.keys\.length >= 4\)/.test(WE));
t('a tile graph can be its figure alone; Live operations is', /\.vw-vgraph-host\.min \.vg-bottom-area\{display:none!important\}/.test(WE) && M.widgets.find((w) => w.record && w.record.id === 'ops-live').record.draw.chrome === 'min');
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
