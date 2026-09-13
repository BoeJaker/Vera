// The canvas add bar's "widget" is the WidgetConfig sheet (Notes/42 defect 23), and a widget item with a record is the
// live <vera-widget> in the column's live layer — the same widget everywhere. The pure body and the source strings.
//   node tests/test_canvas_widget_sheet.cjs   (CommonJS: the gate parses js as scripts)
const path = require('node:path'); const fs = require('node:fs');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const V = require(FILE); const SRC = fs.readFileSync(FILE, 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const B = V.BLOCK; global.window = global.window || {};   // the body reads window.VeraWidget (absent here: the record card)
t('the widget body is exported', !!B && typeof B.widget === 'function');
// without the element on the page (node): a record with a form draws through the static drawer or its card
const rec = { form: 'gauge', title: 'GPU load', source: 'obs.health', draw: { palette: 'load' }, frame: { size: 'm' }, record: { form: 'gauge' } };
const html = B.widget(rec, 'm', 'widget:gauge-1');
t('a record without the element still says what it is', /gauge/.test(html) && !/vc-live/.test(html), html.slice(0, 120));
t('a record\'s form is read from the record, not only draw.form', /const form = rec \? String\(rec\.form \|\| rec\.draw\.form \|\| ''\)/.test(SRC));
// with the element registered: the live slot
global.customElements = { get: (n) => n === 'vera-widget' ? function () {} : undefined };
const live = B.widget(rec, 'l', 'widget:gauge-1');
t('with <vera-widget> on the page a record is a live slot keyed by the item, at its size', /class="vc-live" data-live="widget" data-key="widget:gauge-1" data-size="l"/.test(live), live);
t('the caption says form · source', /vc-cap mono">gauge · obs\.health</.test(live), live);
t('no source → the caption says sample', /gauge · sample</.test(B.widget({ form: 'gauge', draw: {} }, 'm', 'k')));
t('no key (a preview) → the static path, not a live slot', !/vc-live/.test(B.widget(rec, 'm', '')));
delete global.customElements;
// the add bar
t('the add bar\'s widget opens the sheet when the page has it', /n: 'widget', ik: 'WG', kind: 'widget'[^\n]*sheet: true/.test(SRC) && /if \(k\.sheet\) return this\._widgetSurface\(\) \? this\._widgetPick\(btn, k\) : this\._sheetless\(btn, k\);/.test(SRC));
t('the surface is this window\'s or the host\'s VeraWidgetConfig', /_widgetSurface\(\) \{[^\n]*window\.VeraWidgetConfig[^\n]*window\.parent/.test(SRC));
t('the sheet opens into the canvas, beside the add button, with templates', /S\.open\(\{ mode: 'add', into: 'canvas', anchor: btn, templates: true/.test(SRC));
t('the record lands yours — beside the turn in view, related to no turn', /anchor: \{ origin: 'you', beside: focusMid \} \};\s*\n\s*this\._open\.add\(nk\);\s*\n[^\n]*vera:canvas:add/.test(SRC));
t('the item\'s content carries the form, the size and the record', /content = Object\.assign\(\{\}, rec, \{ widget: form, form, title: rec\.title \|\| form, draw: Object\.assign\(\{\}, rec\.draw \|\| \{\}, \{ form, size \}\), record: rec \}\)/.test(SRC));
// the live layer
t('the live layer mounts <vera-widget> with the record, and clears the slot', /kind === 'widget'\) \{ inner = document\.createElement\('vera-widget'\); inner\.setAttribute\('size'[^\n]*inner\.record = rc\.record \|\| rc;[^\n]*h\.textContent = '';/.test(SRC));
t('a changed record or size reaches the mounted element', /kind === 'widget'\) \{ const inner = el\.firstChild, rc = this\._contentOf\(key\); const sz = h\.dataset\.size \|\| 'm';[^\n]*inner\._recJson !== j/.test(SRC));
t('the live wrapper is transparent and the element fills it', /#live \.lv\[data-kind="widget"\]\{background:transparent\}#live \.lv vera-widget\{display:block;width:100%;height:100%\}/.test(SRC));
console.log(fails ? fails + ' failed' : 'all pass'); process.exit(fails ? 1 : 0);
