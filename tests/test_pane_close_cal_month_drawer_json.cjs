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
t('a string holding JSON shows as what it holds (the JSON view: as JSON; the fields view: as the drawer\'s HTML)', /const held = inJson\(v\);/.test(WE) && /if \(o && typeof o === 'object'\) return drTree\(o\);/.test(WE));
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
// 2026-09-28 (owner): "the json content of the right details panel could be formatted much nicer as html instead of as raw json"
{
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const isPlain = (v) => v == null || typeof v !== 'object', fieldVal = (k, v) => esc(String(v));
  const src = WE.slice(WE.indexOf('  function drTree(root) {'), WE.indexOf('  const isPlain'));
  const drTree = new Function('esc', 'isPlain', 'fieldVal', src + '; return drTree;')(esc, isPlain, fieldVal);
  const h = drTree({ host: { name: 'corp', up: true, disk: { used: 3 } }, tags: ['a', 'b'], guests: [{ vmid: 126, name: 'Ollama', cfg: { x: 1 } }, { vmid: 129, name: 'Ollama-B' }], empty: [] });
  t('the fields view draws no JSON punctuation: no quoted keys, no braces', !/&quot;host&quot;|[{}]/.test(h.replace(/<[^>]+>/g, '').replace(/\{ \} JSON shows everything/g, '')));
  t('an object is a section that counts its keys, the first level open, its plain values as rows', /<details class="vw-dr-sec" open><summary><b>host<\/b><i class="c">3 keys<\/i>/.test(h) && /<span class="k">name<\/span><span class="v">corp<\/span>/.test(h) && /<details class="vw-dr-sec"><summary><b>disk<\/b>/.test(h));
  t('a list of plain values is chips', /<div class="vw-dr-chips"><span>a<\/span><span>b<\/span><\/div>/.test(h));
  t('a list of records is a table of their plain columns, a nested value counted', /<table class="vw-dr-tb"><thead><tr><th>vmid<\/th><th>name<\/th><\/tr>/.test(h) && /<td>126<\/td><td>Ollama<\/td>/.test(h) && /nested values counted - \{ \} JSON shows everything/.test(h));
  t('an empty list says so', /<b>empty<\/b><i class="c">0 items<\/i><\/summary><div class="in"><i class="c">empty<\/i>/.test(h));
  t('the fields view uses it for the nested part, a JSON string and the args', /'Nested' : 'Everything'\) \+ '<\/h4>' \+ drTree\(/.test(WE) && /drTree\(JSON\.parse\(args\)\)/.test(WE));
}
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
