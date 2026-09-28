// 2026-09-27 (owner): "id like all widgets to be able to do something similar like click an item in a dashboard to see full
// data in a right hand drawer" - the dashboards had it; the canvas, the chat's menus and the docked menus now too
//   node tests/test_drawer_everywhere.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const WE = R('vera/widgets/widget_element.js'), CV = R('vera/canvas/canvas_element.js'), L = R('vera/chat/vera-lhm.js');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('the canvas\'s widget items open the drawer', /inner = document\.createElement\('vera-widget'\); inner\.setAttribute\('size', h\.dataset\.size \|\| 'm'\); inner\.setAttribute\('bare', ''\); inner\.setAttribute\('item-drawer', ''\);/.test(CV));
t('every LHM widget opens the drawer (the chat\'s menus, the side, the docked menus)', (L.match(/setAttribute\('item-drawer', ''\)/g) || []).length >= 3);
t('a narrow frame hands the drawer up to the page around it, as plain data', /\(win\.innerWidth \|\| 0\) < 560 && up\.VeraWidget && typeof up\.VeraWidget\.drawer === 'function'/.test(WE) && /return up\.VeraWidget\.drawer\(\{ record: plain\(detail\.record\) \|\| \{\}, item: plain\(detail\.item\)/.test(WE));
// behaviour: the hand-up, with a fake frame and a fake parent
{
  let got = null; const up = { VeraWidget: { drawer: (d) => { got = d; return 'up'; } } };
  const win = { innerWidth: 260, parent: up };
  const src = WE.slice(WE.indexOf('  function drawer(detail) {'), WE.indexOf('  function paintDrawer() {'));
  const fn = new Function('document', '_drawer', 'return (' + src.trim().replace(/^function drawer/, 'function') + ')')({ defaultView: win }, null);
  const r = fn({ record: { form: 'month', title: 'Month' }, item: { date: '2026-09-28', events: [{ title: 'Work' }] }, path: 'day', ref: '', data: [{ a: 1 }], host: { ownerDocument: { defaultView: win } } });
  t('the parent draws it: the item, the record, the answer, no host', r === 'up' && got && got.item.date === '2026-09-28' && got.record.title === 'Month' && Array.isArray(got.data) && got.host === null);
}
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
