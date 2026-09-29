// 2026-09-27 (owner): "also make sure that any lhm for any panel can be configured to include widgets just like the chat ui"
//   node tests/test_lhm_panel_widgets.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const LHM = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-lhm.js'), 'utf8');
const HAR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'capability_orchestration.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

t('a docked menu has \u270e in its head', /var ed = _el\('button', 'lhm-a-edit'/.test(LHM));
t('editing, it has the chat\'s edit bar and adds from the same widget sheet', /var bar = _ebar\(/.test(LHM) && /S\.open\(\{ mode:'add', into:'side', title:'Add to '/.test(LHM));
t('the widgets are kept per panel and per menu', /'vera\.lhm\.absorbed\.added\.' \+ key \+ '\.' \+ menu/.test(LHM));
t('drawn live as records, removable while editing', /document\.createElement\('vera-widget'\); try\{ vw\.setAttribute\('record', JSON\.stringify\(rec\)\);/.test(LHM) && /'lhm-a-rm', '\\u2715'/.test(LHM));
t('every docked menu takes them (the harness gives the panel as the key)', /editKey: pid\.replace\(\/--\\d\+\$\/, ''\) \}\);/.test(HAR));
t('the bridge finds a panel by its tab, its element or its name', /document\.getElementById\('panel-' \+ id\) \? id : \(document\.getElementById\('panel-auto-' \+ id\)/.test(HAR) && /\.toLowerCase\(\) === low\);/.test(HAR));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
