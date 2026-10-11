// Before the 13 early duplicate form definitions are deleted (DU-X1, ESTATE-UI-REVIEW 4.1/4.3): every input an early
// copy accepted is drawn by the live form that replaced it, so deleting the early copy loses nothing that ever drew.
//   node tests/test_widget_dead_copy_inputs.cjs
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const R = path.join(__dirname, '..');
const WE = fs.readFileSync(path.join(R, 'vera', 'widgets', 'widget_element.js'), 'utf8');
const defined = {};
const ctx = { window: {}, console: { log() {}, warn() {}, error() {} }, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], querySelector: () => null, createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null, addEventListener() {} }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(fs.readFileSync(path.join(R, 'vera', 'ui', 'iso.js'), 'utf8'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
const txt = (h) => String(h).replace(/<style[\s\S]*?<\/style>/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
const draws = (form, d, needles, o) => { const h = W.draw(form, d, 'l', Object.assign({ sample: false, width: 480, height: 220 }, o || {})); const s = txt(h); const empty = /vw-empty|vb-empty|class="empty"/.test(h) && !needles.every((n) => s.includes(n)); return { ok: !empty && needles.every((n) => s.includes(n)), s: s.slice(0, 160) }; };
const check = (what, form, d, needles, o) => { const r = draws(form, d, needles, o); t(what, r.ok, r.s); };

check('counter: a level with a delta draws the value and its delta', 'counter', { value: 42, delta: 3 }, ['42', '3']);
check('heat: a { name: number } map', 'heat', { cpu: 30, ram: 70, disk: 12 }, ['cpu', 'ram']);
check('matrix: { row: { col: value } }', 'matrix', { web1: { cpu: 3, ram: 5 }, web2: { cpu: 1, ram: 9 } }, ['web1', 'web2']);
check('matrix: [{ name, a, b }]', 'matrix', [{ name: 'web1', cpu: 3, ram: 5 }, { name: 'web2', cpu: 1, ram: 9 }], ['web1', 'web2']);
check('stack: a { name: number } map', 'stack', { a: 3, b: 1 }, ['a', 'b']);
check('pills: a { name: number } map', 'pills', { ok: 3, failing: 1 }, ['ok', 'failing']);
check('pills: rows with a status', 'pills', [{ name: 'redis', status: 'ok' }, { name: 'neo4j', status: 'down' }], ['redis', 'neo4j']);
check('log: rows whose text is in text', 'log', [{ text: 'alpha line' }, { text: 'beta line' }], ['alpha line']);
check('log: rows whose text is in msg', 'log', [{ msg: 'alpha msg' }], ['alpha msg']);
check('log: rows whose text is in message', 'log', [{ message: 'alpha message' }], ['alpha message']);
check('log: rows whose text is in line', 'log', [{ line: 'alpha raw line' }], ['alpha raw line']);
check('lane: rows by kind, text in message', 'lane', [{ kind: 'error', message: 'disk full' }, { kind: 'info', message: 'started' }], ['disk full']);
check('lane: rows by kind, text in line', 'lane', [{ level: 'warn', line: 'slow call' }], ['slow call']);
check('table: rows, with draw.columns honoured', 'table', [{ name: 'a', x: 1, y: 2 }], ['name', 'x'], { draw: { columns: ['name', 'x'] }, record: { draw: { columns: ['name', 'x'] } } });
check('files: rows with a path', 'files', [{ path: 'vera/a.py', size: 10 }, { path: 'vera/b.py', size: 3 }], ['a.py']);
check('checklist: done / checked / state', 'checklist', [{ text: 'one', done: true }, { text: 'two', checked: true }, { text: 'three', state: 'done' }], ['one', 'two', 'three']);
check('stepper: a plain list of names', 'stepper', ['plan', 'do', 'check'], ['plan', 'check']);
check('calendar: dated rows draw (as the day heat; the event list is agenda / month / schedule)', 'calendar', [{ when: '2026-10-11T09:00', title: 'Standup' }, { when: '2026-10-12T10:00', title: 'Review' }], []);
check('agenda: the early calendar\'s event list (times and titles by day)', 'agenda', [{ when: '2026-10-11T09:00', title: 'Standup' }, { when: '2026-10-12T10:00', title: 'Review' }], ['Standup', 'Review']);
check('scatter: points', 'scatter', [{ x: 1, y: 2, name: 'p' }, { x: 3, y: 1, name: 'q' }, { x: 2, y: 5, name: 'r' }], []);
console.log(fails ? fails + ' failed' : 'all passed'); process.exit(fails ? 1 : 0);
