// 2026-09-28 (owner): counters "should state what they are counting and could have more stats on rotation", styled "in-line with the
// active style like newspaper or pixel"; "requests by worker" "is truncating the labels on the left for no reason"
//   node tests/test_counters_say_and_rotate.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const WE = R('vera/widgets/widget_element.js'), M = JSON.parse(R('vera/widgets/layouts/main.json'));
let fails = 0; const t = (name, cond, x) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (x || ''))); if (!cond) fails++; };
const defined = {}; const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null, addEventListener() {} }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(R('vera/ui/iso.js'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
const snap = { inflight_total: 53, inflight_kinds: { 'cap call': 48, 'git op': 5, background: 0 }, counts: { errors: 19, nodes: 72 } };
const r = M.widgets.find((w) => w.record && w.record.id === 'ops-inflight').record;
const h = W.draw('counter', W.mapped(W.normalise(r), 'counter', snap), 'm', { bare: true, record: W.normalise(r), draw: r.draw, height: 110, width: 280 });
t('the counter says what it counts', /class="vb-what">calls and ops in flight</.test(h), h.slice(0, 300));
t('and turns over the other figures of the answer', /class="vb-rot" style="--n:5"/.test(h) && /<b>48<\/b> cap calls/.test(h) && /<b>19<\/b> open problems/.test(h), h.slice(0, 600));
t('a counter without a named what says the word its value is read from', (() => { const rr = { form: 'counter', read: { map: { value: 'queue_depth' } } }; const x = W.draw('counter', { value: 4 }, 'm', { bare: true, record: W.normalise(rr), height: 90 }); return /class="vb-what">queue depth</.test(x); })());
t('the packs dress the figures (newspaper serif + ruled caption, pixel plate, terminal readout)', WE.includes(':host-context([data-style="newspaper"]) .vb-what{font-variant:small-caps') && WE.includes(':host-context([data-style="pixel"]) .vb-seg7{padding:6px 10px;') && WE.includes(":host-context([data-style=\"terminal\"]) .vb-seg7::after{content:'_';"));
const rk = W.draw('ranked', [{ name: 'cpu-247', value: 100 }, { name: 'cpu-246', value: 90 }], 'm', { bare: true, height: 90, width: 420 });
t('ranked names are whole when they fit ("cpu-247", not "...47")', />cpu-247</.test(rk) && />cpu-246</.test(rk) && !/…47/.test(rk), rk.slice(0, 300));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
