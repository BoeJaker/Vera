// DA-02c: the chat reads a DECLARED answer through VeraWidget.present - the one reading the canvas and the server share -
// and lands the item its element names; an undeclared capability goes through the chat's adapters exactly as before.
// Runs the page's own adapter block (cut out of chat_panel.html) against the real widget_element.js + present.js.
//   node tests/test_chat_present_declared.cjs
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const root = path.join(__dirname, '..');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const src = fs.readFileSync(path.join(root, 'vera', 'chat', 'chat_panel.html'), 'utf8');

t('loadAllCaps feeds the declarations from /mcp/tools', /ALL_CAPS=Array\.isArray\(res\)\?res:\(res\?\.tools\|\|\[\]\);\s*\/\/[^\n]*\n\s*try\{ if\(window\.VeraWidget&&typeof VeraWidget\.presentations==='function'\) VeraWidget\.presentations\(ALL_CAPS\); \}catch\(_\)\{\}/.test(src));
t('_cvAdapt asks the declaration first', src.includes("{ const dp=_cvPresented(capName, result, args); if(dp!==undefined) return dp; }   /* declared first (DA-02c) */"));

// the widget surface, as the page has it
function widgets(withPresent) {
  const defined = {};
  const c = { setTimeout, clearTimeout, window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, k) => { defined[n] = k; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {} }) } };
  c.window.customElements = c.customElements; c.window.document = c.document;
  vm.runInNewContext(fs.readFileSync(path.join(root, 'vera', 'ui', 'iso.js'), 'utf8'), c);
  vm.runInNewContext(fs.readFileSync(path.join(root, 'vera', 'widgets', 'widget_element.js'), 'utf8'), c);
  if (withPresent) vm.runInNewContext(fs.readFileSync(path.join(root, 'vera', 'widgets', 'present.js'), 'utf8'), c);
  return c.window.VeraWidget;
}
// the page's adapter block, run with that surface
const A = "  const CV_ADAPTERS=[];", B = "  function _cvHarvest(body){";
const i0 = src.indexOf(A), i1 = src.indexOf(B);
t('the adapter block is in the page', i0 >= 0 && i1 > i0);
function adapt(W) {
  const win = { VeraWidget: W };
  const ctx = { window: win, VeraWidget: W, URL, ALL_CAPS: [], console,
    _widgetData: (x) => (Array.isArray(x) ? x : x), _widgetFormByShape: () => null, _widgetRecord: (o) => o };
  vm.createContext(ctx);
  vm.runInContext(src.slice(i0, i1) + '\n;this.__adapt=_cvAdapt;', ctx);
  return ctx.__adapt;
}
const Q = { quotes: [{ symbol: 'BTC', last: 61000, pct: 1.2 }, { symbol: 'ETH', last: 2400, pct: -0.4 }] };

// 1. no declaration: present()'s own fallback reads the generic shapes (D10 step 2b, owner 2026-10-17) - rows are a table
//    and a flat record its fields, built by present(); everything the other adapters make is made as before
const before = adapt(widgets(false)), Wp = widgets(true), after = adapt(Wp);
const SAMPLES = [['exec.bash.run', { command: 'uptime', stdout: 'up 3 days', rc: 0 }], ['demo.prose', { report: '# Report\n\n' + 'word '.repeat(80) }],
  ['code.read', { code: 'print(1)', path: 'a.py' }], ['demo.none', '']];
const same = SAMPLES.filter(([c, r]) => JSON.stringify(before(c, r, {})) !== JSON.stringify(after(c, r, {})));
t('an undeclared terminal / document / code / empty answer lands exactly as before', !same.length, same.map((x) => x[0]).join(','));
{ const r = after('demo.rows', Q, {}); t('undeclared rows: a table, through present()', !!r && r.kind === 'table' && r.content.columns.join(',') === 'symbol,last,pct' && r.content.rows.length === 2, JSON.stringify(r));
  const k = after('demo.kv', { host: 'ct126', cores: 16 }, {}); t('an undeclared flat record: its fields, through present()', !!k && k.kind === 'kv' && k.content.fields.host === 'ct126' && k.content.fields.cores === 16, JSON.stringify(k)); }

// 2. declared: present() decides, and the item is its element's
const declare = (cap, p) => Wp.presentations([{ name: cap, presentation: Object.assign({ status: 'declared' }, p) }]);
declare('demo.quotes', { kind: 'table', map: { rows: 'quotes', name: 'symbol', value: 'last' }, title: 'Quotes' });
let it = after('demo.quotes', Q, {});
t('a declared table is a table item: its columns and rows', it && it.kind === 'table' && JSON.stringify(it.content.columns) === '["symbol","last","pct"]' && it.content.rows.length === 2 && it.content.rows[0][0] === 'BTC' && it.content.caption === 'Quotes', JSON.stringify(it));
declare('demo.report', { kind: 'document', map: { body: 'out.md' } });
it = after('demo.report', { out: { md: '# Hello\n\nworld' } }, {});
t('a declared document is a markdown item', it && it.kind === 'markdown' && it.content.md === '# Hello\n\nworld' && it.n === 'Hello');
declare('demo.search', { kind: 'records', map: { rows: 'hits', title: 'name', url: 'link' }, title: 'Hits' });
it = after('demo.search', { hits: [{ name: 'A', link: 'https://a.example/x', snippet: 's' }] }, {});
t('declared records are a records item', it && it.kind === 'records' && it.content.items[0].title === 'A' && it.content.items[0].domain === 'a.example');
declare('demo.read', { kind: 'code', map: { code: 'text', lang: 'language', path: 'file' } });
it = after('demo.read', { text: 'x = 1\ny = 2', language: 'python', file: 'src/a.py' }, {});
t('declared code is a code item', it && it.kind === 'code' && it.content.lang === 'python' && it.content.filename === 'a.py' && it.n === 'a.py · 2 lines');
declare('demo.patch', { kind: 'diff', map: { diff: 'p' } });
it = after('demo.patch', { p: '--- a\n+++ b\n-x\n+y' }, {});
t('a declared diff is a code item in diff', it && it.kind === 'code' && it.content.lang === 'diff');
declare('demo.exec', { kind: 'terminal', map: { command: 'cmd', stdout: 'out', rc: 'rc' } });
it = after('demo.exec', { cmd: 'uptime', out: 'up 3 days', rc: 0 }, {});
t('a declared terminal is a session item', it && it.kind === 'session' && it.content.command === 'uptime' && /up 3 days/.test(it.content.output));
declare('demo.info', { kind: 'kv', map: { fields: 'info' } });
it = after('demo.info', { info: { host: 'ct126', cores: 16 } }, {});
t('declared fields are a kv item', it && it.kind === 'kv' && it.content.fields.cores === 16);
declare('demo.cal', { kind: 'calendar', map: { events: 'items', start: 'from', title: 'summary' } });
it = after('demo.cal', { items: [{ from: '2026-10-12T09:00', summary: 'standup' }] }, {});
t('a declared calendar is the month', it && it.kind === 'calendar' && it.content.month === '2026-10' && it.content.events[0].title === 'standup');
declare('demo.load', { kind: 'metric', map: { value: 'cpu', max: 'cap' } });
it = after('demo.load', { cpu: 62, cap: 100 }, {});
t('a declared metric is a widget item', it && it.kind === 'widget' && it.content.form === 'ring' && it.content.record.presentation.kind === 'metric');
declare('demo.host', { kind: 'composite', parts: [{ kind: 'metric', map: { value: 'load' }, title: 'Load' }, { kind: 'log', map: { events: 'lines', t: 'ts', text: 'msg' }, title: 'Log' }] });
it = after('demo.host', { load: 0.6, lines: [{ ts: '2026-10-12T10:00:00', msg: 'ok' }] }, {});
t('a declared composite is one widget item per part', Array.isArray(it) && it.length === 2 && it[0].content.form === 'hero' && it[1].content.form === 'log');
declare('demo.toggle', { kind: 'receipt', map: { ok: 'ok' } });
t('a declared receipt makes no item', after('demo.toggle', { ok: true }, {}) === null);
declare('demo.long', { kind: 'job', map: { job_id: 'id' } });
t('a declared job makes no item (its live item follows)', after('demo.long', { id: 'j1' }, {}) === null);
declare('demo.empty', { kind: 'table', map: { rows: 'quotes' } });
t('a declared answer with nothing in it makes no item', after('demo.empty', { quotes: [] }, {}) === null);
// a canvas.* receipt stays never-drawn even if one were declared (the override comes first)
declare('canvas.add', { kind: 'kv', map: { fields: '$' } });
t('the never-draw overrides still come first', after('canvas.add', { ok: true, key: 'x' }, {}) === null);
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
