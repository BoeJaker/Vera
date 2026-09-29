// 2026-09-27 (owner): "any lhm items that can be made into widgets ... like the calendar controls and even the calendar from
// the comms ui itself - and the different parts of it like the schedule view on the right"
//   node tests/test_lhm_default_widgets.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const BR = R('vera/chat/vera-panel-bridge.js'), L = R('vera/chat/vera-lhm.js'), CAL = R('vera/calendar/calendar_panel.html'), WE = R('vera/widgets/widget_element.js');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('a page names its menu widgets and they ride in the lhm spec', /document\.querySelector\('\[data-lhm-widgets\]'\)/.test(BR) && /open: \[\], widgets: wd \}/.test(BR));
t('a nesting page passes its shown child\'s widgets up', /k\.nav\.widgets = nv\.lhm\.widgets\.slice\(0, 8\)/.test(BR) && /kid\.nav\.widgets\.forEach/.test(BR));
t('the docked menu draws them until the viewer keeps a list of their own', /_absHasSaved\(key, cur\.id\) \? _absAddedOf\(key, cur\.id\) : _absDefaults\(spec\)/.test(L) && /var list = mine\(\);/.test(L) && /var l2 = mine\(\); l2\.push\(rec\);/.test(L) && /var l3 = mine\(\); l3\.splice\(i, 1\);/.test(L));
t('drawn by template, at their size, and every one opens the item drawer', /vw\.setAttribute\('template-id', rec\.template_id\)/.test(L) && /vw\.setAttribute\('item-drawer', ''\);/.test(L));
t('the Calendar names its parts: controls, month, schedule, todos', /data-lhm-widgets="cal:controls cal:month@m cal:schedule@m cal:todos@m"/.test(CAL));
t('a list form reads the answer\'s one list ({todos, count})', /const la = ks\.filter\(\(k\) => Array\.isArray\(x\[k\]\)\); if \(la\.length === 1\) return dataFor/.test(WE));
t('the schedule\'s end sits under its start, the column sized with the text', /esc\(a\) \+ \(b \? '<small>' \+ esc\(b\) \+ '<\/small>' : ''\)/.test(WE) && /grid-template-columns:4\.4em minmax\(0,1fr\)/.test(WE));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
