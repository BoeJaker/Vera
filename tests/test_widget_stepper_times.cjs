// The stepper keeps every step's time and up to 12 steps (ESTATE-UI-REVIEW 4.2, DU-X2): the early stepper drew each
// step's `when` / `ms` and 12 steps; the track that replaced it drew 8 and no times. Both forms that use the track
// (stepper, pipeline) show them again; past 8 steps the track goes dense and every step keeps its name as a tooltip.
//   node tests/test_widget_stepper_times.cjs
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const R = path.join(__dirname, '..');
const WE = fs.readFileSync(path.join(R, 'vera', 'widgets', 'widget_element.js'), 'utf8');
const defined = {};
const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null, addEventListener() {} }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(fs.readFileSync(path.join(R, 'vera', 'ui', 'iso.js'), 'utf8'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
const spans = (h) => (h.match(/<span class="(done|now|bad|)( quiet)?" title=/g) || []).length;
const smalls = (h) => [...h.matchAll(/<small>([^<]*)<\/small>/g)].map((m) => m[1]);

const st = [{ name: 'plan', done: true, ms: 340 }, { name: 'search', done: true, elapsed_ms: 1500 }, { name: 'read', state: 'running', duration_s: 95 }, { name: 'write', when: '14:02' }];
const h = W.draw('stepper', st, 'm', { sample: false });
t('each step draws its time: ms, elapsed_ms, duration_s, a short when', JSON.stringify(smalls(h)) === JSON.stringify(['340 ms', '1.5 s', '2 m', '14:02']), JSON.stringify(smalls(h)));
t('a step with no time draws no time element', !/<small><\/small>/.test(W.draw('stepper', [{ name: 'a' }, { name: 'b' }], 'm', { sample: false })));
t('the name and time are the step\'s tooltip', /title="read · 2 m"/.test(h));
const twelve = Array.from({ length: 14 }, (_, i) => ({ name: 's' + i, done: i < 5, state: i === 5 ? 'running' : undefined, ms: 100 * i }));
const h12 = W.draw('stepper', twelve, 'm', { sample: false });
t('up to 12 steps are drawn (was 8)', spans(h12) === 12, String(spans(h12)));
t('past 8 the track is dense; the current and the last done step stay named, others quiet', /vb-stepr dense/.test(h12) && /class="now" title="s5/.test(h12) && /class="done" title="s4/.test(h12) && /class="done quiet" title="s0/.test(h12));
t('8 steps or fewer: not dense, nothing quiet', !/dense|quiet/.test(W.draw('stepper', twelve.slice(0, 8), 'm', { sample: false })));
t('the pipeline form, which shares the track, shows stage times too', smalls(W.draw('pipeline', { stages: st }, 'm', { sample: false })).length === 4);
t('the dense and time styles are in the form CSS', WE.includes('.vb-stepr small{') && WE.includes('.vb-stepr.dense span.quiet em'));
console.log(fails ? fails + ' failed' : 'all passed'); process.exit(fails ? 1 : 0);
