// 2026-09-28: the operations band has room to read; a list's summary earns its room; a donut leaves out the noughts
//   node tests/test_ops_band_room.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const WE = R('vera/widgets/widget_element.js'), M = JSON.parse(R('vera/widgets/layouts/main.json'));
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
const by = (id) => M.widgets.find((w) => w.record && w.record.id === id);
t('in flight and routing 8 rows, workers and the stream 4', by('ops-inflight').span[1] === 8 && by('ops-routing').span[1] === 8 && by('ops-workers').span[1] === 4 && by('ops-workers').at[1] === by('ops-inflight').at[1] + 8);
t('a summary only with two statuses and room for four rows', /\(new Set\(rw0\.map\(word\)\)\)\.size < 2/.test(WE));
t('a donut leaves out the parts at nought', /const kv = keyed\(d\)\.filter\(\(x\) => x\[1\] !== 0\)\.slice\(0, 8\);/.test(WE));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
