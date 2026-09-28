// 2026-09-27 (owner): a pane opened beside "no way to close" it · the month "cuts off on thursday" · "prettyfy all the
// json properly in the right hand details panel"
//   node tests/test_pane_close_cal_month_drawer_json.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const H = R('vera/capability_orchestration.html'), CAL = R('vera/calendar/calendar_panel.html'), WE = R('vera/widgets/widget_element.js');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('every open pane, while more than one is, names itself and closes', /if\(!\(visibleCount > 1 && _openTabs\.has\(pid\)\)\)\{ if\(chip\) chip\.remove\(\); return; \}/.test(H) && /chip\.querySelector\('\.px'\)\.addEventListener\('click', ev => \{ ev\.stopPropagation\(\); splitTab\(pid\);/.test(H) && /\.pane-x\{position:absolute;/.test(H));
t('its name docks its menu (it becomes the first pane)', /_openTabs\.clear\(\); _openTabs\.add\(pid\); rest\.forEach\(x => _openTabs\.add\(x\)\); _tabRender\(\);/.test(H) && /function _paneLabel\(pid\)\{/.test(H));
t('the month: seven columns that share the width, a chip cut to its day', /\.cal-grid\{ display:grid;grid-template-columns:repeat\(7,minmax\(0,1fr\)\)/.test(CAL) && /\.cal-grid \.cal-cell \.chip\{ display:block;max-width:100%;/.test(CAL) && !/repeat\(7,1fr\)/.test(CAL));
t('the drawer draws JSON as JSON: keys quoted, braces, commas, two levels open', /function drJson\(root\)/.test(WE) && /'<details' \+ \(depth < 2 \? ' open' : ''\)/.test(WE) && /\.vw-dr-json\{/.test(WE));
t('a string holding JSON shows as that JSON (fields and tree)', /const held = inJson\(v\);/.test(WE) && /if \(o && typeof o === 'object'\) return drJson\(o\);/.test(WE));
t('Fields / JSON, remembered', /data-dr="view"/.test(WE) && /localStorage\.setItem\('vera\.drawer\.view', D\.view\)/.test(WE));
// behaviour: drJson itself
{
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const src = WE.slice(WE.indexOf('  function drJson(root) {'), WE.indexOf('  const isPlain'));
  const drJson = new Function('esc', src + '; return drJson;')(esc);
  const h = drJson({ a: 1, s: 'x', b: true, n: null, o: { k: [1, 2] }, j: '{"inner":3}' });
  const text = h.replace(/<[^>]+>/g, '');
  t('the text reads as JSON', /"a": 1,/.test(text.replace(/&quot;/g, '"')) && /"s": "x",/.test(text.replace(/&quot;/g, '"')) && /"k": \[/.test(text.replace(/&quot;/g, '"')) && /"inner": 3/.test(text.replace(/&quot;/g, '"')));
}
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
