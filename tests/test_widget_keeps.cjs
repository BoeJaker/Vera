// The no-loss gate for widget upgrades (DU-K; shared-planning/context-and-ui/ESTATE-UI-REVIEW.md Appendix K).
// Owner 2026-10-11: "be careful not to lose features of existing widgets / elements".
//
// What it holds, per live form (the last definition of a name wins, as in the browser):
//   - the draw options it reads (o.draw.<x>), the interactive state it keeps (ui(o,'x') / o.ui.x / ui.x),
//     the message that names its required input (EMPTY('...')), and every alias that reaches it;
//   - that it still draws a face, without throwing, at s, m and l;
// and for the shared frame: the six states, view-as, the writable form, skins, motion, mounted children.
//
// A feature that disappears fails the test. A new one is reported and allowed. When a removal is deliberate, rewrite
// the snapshot and say why in the commit:  UPDATE_KEEPS=1 node tests/test_widget_keeps.cjs
//   node tests/test_widget_keeps.cjs
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const R = path.join(__dirname, '..');
const WE = fs.readFileSync(path.join(R, 'vera', 'widgets', 'widget_element.js'), 'utf8');
const SNAP = path.join(__dirname, 'fixtures', 'widget_keeps.json');

/* ── 1. the features, read from the code ── */
const marks = [...WE.matchAll(/^ {2}R(?:\['([^']+)'\]|\.([A-Za-z_]\w*))\s*=\s*\(/gm)].map((m) => ({ name: m[1] || m[2], at: m.index }));
const live = {};
marks.forEach((m, i) => {
  const body = WE.slice(m.at, i + 1 < marks.length ? marks[i + 1].at : Math.min(WE.length, m.at + 20000));
  const set = (re) => [...new Set([...body.matchAll(re)].map((x) => x[1]))].sort();
  live[m.name] = {   // a later definition of the same name replaces an earlier one, as `R.x = ` does at run time
    options: set(/o\.draw\.([A-Za-z_]\w*)/g),
    ui: [...new Set([...set(/ui\(o,\s*'([^']+)'/g), ...set(/o\.ui\.([A-Za-z_]\w*)/g)])].sort(),
    empty: set(/EMPTY\('([^']+)'/g),
  };
});
const am = WE.match(/const ALIAS = \{([\s\S]*?)\};/);
const aliases = {};
if (am) for (const x of am[1].matchAll(/'?([\w@-]+)'?\s*:\s*'([\w@-]+)'/g)) aliases[x[1]] = x[2];
const now = { forms: live, aliases };

if (process.env.UPDATE_KEEPS === '1') {
  fs.mkdirSync(path.dirname(SNAP), { recursive: true });
  fs.writeFileSync(SNAP, JSON.stringify(now, null, 1) + '\n');
  console.log('wrote', SNAP, Object.keys(live).length, 'forms'); process.exit(0);
}
const was = JSON.parse(fs.readFileSync(SNAP, 'utf8'));

/* ── 2. nothing that was there has gone ── */
const lost = [], added = [];
for (const [name, k] of Object.entries(was.forms)) {
  const n = now.forms[name];
  if (!n) { lost.push(name + ' (the whole form)'); continue; }
  for (const key of ['options', 'ui', 'empty']) for (const v of k[key]) if (!n[key].includes(v)) lost.push(name + ' ' + key + ': ' + v);
  for (const key of ['options', 'ui', 'empty']) for (const v of n[key]) if (!k[key].includes(v)) added.push(name + ' ' + key + ': ' + v);
}
for (const name of Object.keys(now.forms)) if (!was.forms[name]) added.push(name + ' (new form)');
for (const [a, f] of Object.entries(was.aliases)) if (now.aliases[a] !== f) lost.push('alias ' + a + ' → ' + f + (now.aliases[a] ? ' (now → ' + now.aliases[a] + ')' : ''));
t('every form, option, interactive state, empty message and alias in the snapshot is still there (' + Object.keys(was.forms).length + ' forms, ' + Object.keys(was.aliases).length + ' aliases)', lost.length === 0, lost.slice(0, 12).join(' | ') + (lost.length > 12 ? ' …+' + (lost.length - 12) : ''));
if (added.length) console.log('note: new since the snapshot (allowed; refresh with UPDATE_KEEPS=1 when you land): ' + added.slice(0, 10).join(' | ') + (added.length > 10 ? ' …+' + (added.length - 10) : ''));

/* ── 3. every live form still draws a face, at three sizes ── */
const defined = {};
const ctx = { window: {}, console: { log() {}, warn() {}, error() {}, info() {}, debug() {} }, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], querySelector: () => null, createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null, addEventListener() {} }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(fs.readFileSync(path.join(R, 'vera', 'ui', 'iso.js'), 'utf8'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
const broken = [];
for (const name of Object.keys(live)) for (const size of ['s', 'm', 'l']) {
  try { const h = W.draw(name, undefined, size, { width: 420, height: size === 'l' ? 220 : 120 }); if (typeof h !== 'string' || !h.trim()) broken.push(name + '@' + size + ' (empty)'); }
  catch (e) { broken.push(name + '@' + size + ' (' + String(e && e.message || e).slice(0, 60) + ')'); }
}
t('every live form draws its sample face at s, m and l without throwing (' + Object.keys(live).length + ' forms)', broken.length === 0, broken.slice(0, 10).join(' | '));

/* ── 4. the shared frame (ESTATE-UI-REVIEW §2) ── */
const frame = [
  ['six honest states on the host', ["'reading'", "'failed'", "'sample'", "'empty'", "'stale'", "'live'", "setAttribute('data-state', state)"]],
  ['the caption: source, why, viewed as, drawn as', ["'viewed as '", "'drawn as '", "'<div class=\"vw-cap\">'"]],
  ['view-as persists per record', ["localStorage.getItem('vera.widget.as.' + key(rec))", 'viewsFor(rec, form0, this._data)']],
  ['record actions at xl and the read button', ["size === 'xl' ? '<div class=\"vw-acts\">'", "querySelector('[data-read]')"]],
  ['form-local state and links', ['[data-vb-set]', '[data-vb-input]', '[data-vb-link]', "'widget:open'"]],
  ['the writable form keeps focus and saves what changed', ['[data-vf]', '[data-vf-save]', '_formSave()']],
  ['skins and motion off', ["setAttribute('data-style', skin)", "data-motion=\"0\""]],
  ['the tween between renders and the entry motion', ['motionBefore(this._sh)', 'motionAfter(this._sh, was)', "setAttribute('data-entered', '')"]],
  ['mounted children: vgraph and Loop Lab elements', ['mountVeraGraph(this, rec, dataM, size)', 'mountElement(this, rec)']],
  ['no scrollbar for a few pixels of overflow', ["classList.toggle('vw-fit', over > 0 && over <= 6)"]],
  ['the rendered event carries the state', ["new CustomEvent('widget:rendered'"]],
];
for (const [what, needles] of frame) { const miss = needles.filter((n) => !WE.includes(n)); t('frame keeps: ' + what, miss.length === 0, 'missing: ' + miss.join(' , ')); }

console.log(fails ? fails + ' failed' : 'all passed'); process.exit(fails ? 1 : 0);
