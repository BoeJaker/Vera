// DA-02a: VeraWidget.present() - one reading of a capability's answer (DESK-SPINE-DESIGN.md §2): a declared
// presentation decides exactly; else the hint and the shape rules (fromCapResult, unchanged); nothing drawable -> no item.
// Runs the shared fixtures (tests/fixtures/presentation/<kind>/<case>.json) that the server mirror (DA-02b) runs too.
//   node tests/test_presentation_browser.cjs
const fs = require('node:fs'); const path = require('node:path'); const vm = require('node:vm');
const here = __dirname, root = path.join(here, '..');
const defined = {};
const ctx = { setTimeout, clearTimeout, window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {} }) } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document;
vm.runInNewContext(fs.readFileSync(path.join(root, 'vera', 'ui', 'iso.js'), 'utf8'), ctx);
vm.runInNewContext(fs.readFileSync(path.join(root, 'vera', 'widgets', 'widget_element.js'), 'utf8'), ctx);
vm.runInNewContext(fs.readFileSync(path.join(root, 'vera', 'widgets', 'present.js'), 'utf8'), ctx);
const W = ctx.window.VeraWidget;
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

t('present() is on VeraWidget', typeof W.present === 'function' && typeof W.presentations === 'function' && typeof W.presentationOf === 'function');
// the vocabulary is capability_contract_core's: the same kinds, the same views
const py = fs.readFileSync(path.join(root, 'vera', 'capability_contract_core.py'), 'utf8');
const pyKinds = [...py.matchAll(/^    "([a-z]+)": \{"keys": /gm)].map((m) => m[1]).sort();
t('the kinds are the contract core\'s', JSON.stringify(Object.keys(W.presentKinds).sort()) === JSON.stringify(pyKinds), Object.keys(W.presentKinds).join(','));
const viewsBad = Object.keys(W.presentKinds).filter((k) => { const m = py.match(new RegExp('"' + k + '": \\{"keys": [\\s\\S]*?"views": \\(([^)]*)\\)')); const pv = m ? [...m[1].matchAll(/"([a-z]+)"/g)].map((x) => x[1]) : []; return JSON.stringify(pv) !== JSON.stringify(W.presentKinds[k].views); });
t('each kind has the contract core\'s views', !viewsBad.length, viewsBad.join(','));

// the shared fixtures
const dir = path.join(here, 'fixtures', 'presentation'); let n = 0;
const files = []; for (const k of fs.readdirSync(dir)) for (const f of fs.readdirSync(path.join(dir, k))) if (f.endsWith('.json')) files.push(k + '/' + f);
for (const f of files.sort()) {
  const fx = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8')); n++;
  const got = W.present(fx.cap, fx.result, { args: fx.args, presentation: fx.presentation });
  const e = fx.expect; const forms = got.records.map((r) => r.form);
  const bad = [];
  if (got.element !== e.element) bad.push('element ' + got.element + ' != ' + e.element);
  if (got.kind !== e.kind) bad.push('kind ' + got.kind + ' != ' + e.kind);
  if (got.title !== e.title) bad.push('title "' + got.title + '" != "' + e.title + '"');
  if (JSON.stringify(forms) !== JSON.stringify(e.forms)) bad.push('forms ' + JSON.stringify(forms) + ' != ' + JSON.stringify(e.forms));
  if (e.empty != null && got.empty !== e.empty) bad.push('empty "' + got.empty + '" != "' + e.empty + '"');
  if (e.first) { const d = got.records[0] && got.records[0].data; const d0 = Array.isArray(d) ? d[0] : d; Object.keys(e.first).forEach((k) => { if (!d0 || d0[k] !== e.first[k]) bad.push('first.' + k + ' ' + JSON.stringify(d0 && d0[k]) + ' != ' + JSON.stringify(e.first[k])); }); }
  // the first record's data exactly - the server mirror reads the same fixture to the same data (DA-01e keyed series)
  if (e.data !== undefined) { const got0 = JSON.stringify(got.records[0] && got.records[0].data), want = JSON.stringify(e.data); if (got0 !== want) bad.push('data ' + String(got0).slice(0, 160) + ' != ' + want.slice(0, 160)); }
  // every record it gives draws its answer, not its empty face
  got.records.forEach((r) => { const h = String(W.draw(r.form, r.data, 'm', { record: r, title: r.title, sample: false }) || ''); if (/class="wempty"/.test(h)) bad.push(r.form + ' draws empty: ' + h.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').slice(0, 80)); });
  t('fixture ' + f, !bad.length, bad.join('; '));
}
t('fixtures found (' + n + ')', n >= 20);

// a declared record carries its declaration (a re-read goes through present() again); an undeclared one reads as before
const d = W.present('demo.quotes', { quotes: [{ symbol: 'A', last: 1 }] }, { presentation: { kind: 'table', map: { rows: 'quotes' } } });
t('a declared record carries its declaration', d.declared === true && d.records[0].presentation && d.records[0].presentation.kind === 'table');
const rows = [{ a: 1, b: 2 }, { a: 3, b: 4 }];
const u = W.present('demo.rows', rows);
t('an undeclared answer reads as fromCapResult reads it', u.declared === false && JSON.stringify(u.records.map((r) => r.form)) === JSON.stringify(W.fromCapResult('demo.rows', rows).map((r) => r.form)));
// /mcp/tools' list feeds the registry: declared ones only
W.presentations([{ name: 'demo.reg', presentation: { status: 'declared', kind: 'document', map: { body: 'md' } } }, { name: 'demo.bad', presentation: { status: 'invalid', kind: 'table', map: {} } }, { name: 'demo.plain' }]);
t('the registry keeps declared presentations only', !!W.presentationOf('demo.reg') && !W.presentationOf('demo.bad') && !W.presentationOf('demo.plain'));
const viaReg = W.present('demo.reg', { md: '# Hi\n\nthere' });
t('present() reads the registry when not handed one', viaReg.kind === 'document' && viaReg.records[0].form === 'markdown' && viaReg.records[0].data === '# Hi\n\nthere');
// the route serves the two files as one script: they parse together and present() is there after it
const both = fs.readFileSync(path.join(root, 'vera', 'widgets', 'widget_element.js'), 'utf8') + String.fromCharCode(10) + fs.readFileSync(path.join(root, 'vera', 'widgets', 'present.js'), 'utf8');
let parsed = true; try { new vm.Script(both); } catch (e) { parsed = String(e); }
t('widget_element.js + present.js parse as one script', parsed === true, parsed);
// DA-01e (3): no options - the record is what it was; with them on a row form, the form gets its draw without them
// (its own draw.sort would sort again, high first) and group_by's column first - test_presentation_server says the same
{ const rows = { rows: [{ a: 2, b: 'x' }, { a: 1, b: 'y' }] }; const plain = { kind: 'table', form: 'table', map: { rows: 'rows' }, draw: { columns: ['a', 'b'], limit: 4 } };
  const g0 = W.present('demo.t', rows, { presentation: plain }).records[0];
  t('no options: the data and the draw as they were', g0.data === rows && JSON.stringify(g0.draw) === '{"columns":["a","b"],"limit":4}');
  const g1 = W.present('demo.t', rows, { presentation: Object.assign({}, plain, { draw: { columns: ['a', 'b'], sort: 'a', group_by: 'b' } }) }).records[0];
  t('options on a row form: arranged, the form\'s draw without them', JSON.stringify(g1.draw) === '{"columns":["b","a"]}' && JSON.stringify(g1.data.rows.map((r) => r.a)) === '[1,2]' && rows.rows[0].a === 2, JSON.stringify(g1.draw));
  const g2 = W.present('demo.t', { t: { a: 50 } }, { presentation: { kind: 'table', form: 'temps', map: { rows: 't' }, draw: { sort: 'name' } } }).records[0];
  t('another form keeps its draw as its own words', JSON.stringify(g2.draw) === '{"sort":"name"}'); }
const ui = fs.readFileSync(path.join(root, 'vera', 'ui builder', 'ui_capabilities.py'), 'utf8');
// the route appends its companions in one loop since K5 (19 Oct 2026): present.js, then widget_dive.js (VeraDive)
t('the widget route appends present.js', /for name in \([^)]*"present\.js"[^)]*\):[\s\S]{0,160}body \+= chr\(10\) \+ extra\.read_text/.test(ui));
t('widget_element.js + present.js + widget_dive.js parse as one script (what the route serves)', (() => {
  const all = both + String.fromCharCode(10) + fs.readFileSync(path.join(root, 'vera', 'widgets', 'widget_dive.js'), 'utf8');
  try { new vm.Script(all); return true; } catch (e) { return e.message; } })() === true);
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
