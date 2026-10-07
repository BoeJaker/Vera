// 2026-09-28 (owner): "the composite widgets often have lists in them but do not give them enough room or allow the list to be
// scrolled. composites that contain elements that would typically be large like lists should be considered xl composite and
// then the ordinary composite be reserved for smaller constituent widgets. the stack topology widget is not tall enough by
// default - i tried to drag it and make it taller and it seemed to have a max i could drag it to"
//   node tests/test_widgets_room_to_grow.cjs   (CommonJS: the pipeline's gate parses every js file as a script)
const fs = require('node:fs'); const path = require('node:path'); const vm = require('node:vm');
const here = __dirname;
const src = fs.readFileSync(path.join(here, '..', 'vera', 'widgets', 'widget_element.js'), 'utf8');
const VD = fs.readFileSync(path.join(here, '..', 'vera', 'chat', 'vera-dashboard.js'), 'utf8');
const MAIN = JSON.parse(fs.readFileSync(path.join(here, '..', 'vera', 'widgets', 'layouts', 'main.json'), 'utf8'));
const defined = {};
const ctx = { setTimeout, clearTimeout, window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {} }) } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document;
vm.runInNewContext(fs.readFileSync(path.join(here, '..', 'vera', 'ui', 'iso.js'), 'utf8'), ctx);
vm.runInNewContext(src, ctx);
const W = ctx.window.VeraWidget;
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// ── the XL composite: a list among the parts ──
const figures = { form: 'composite', layout: 'grid', children: [{ slot: 'a', record: { form: 'counter', title: 'a' } }, { slot: 'b', record: { form: 'ring', title: 'b' } }] };
const withList = { form: 'composite', layout: 'grid', children: [{ slot: 'a', record: { form: 'numbers', title: 'the stream' } }, { slot: 'b', record: { form: 'rows', title: 'consumers' } }] };
t('the classifier is exported with its floor', typeof W.compositeXL === 'function' && W.xlCompositeRows >= 6);
t('a composite of figures is an ordinary composite', W.compositeXL(figures) === false);
t('a composite with a list among its parts is an XL composite', W.compositeXL(withList) === true);
t('a report or a rail is neither (its lists are summary blocks)', !W.compositeXL(Object.assign({}, withList, { layout: 'report' })) && !W.compositeXL(Object.assign({}, withList, { layout: 'rail' })));
t('anything that is not a composite is not one', !W.compositeXL({ form: 'rows' }) && !W.compositeXL(null));

// an XL composite's list draws EVERY row and scrolls in its slot; the same list in a short ordinary frame is cut
const rowsData = Array.from({ length: 30 }, (_, i) => ({ name: 'worker-' + i, status: i % 3 ? 'ok' : 'busy', value: i }));
const recXL = { id: 't', form: 'composite', layout: 'grid', children: [{ slot: 'a', record: { form: 'numbers', title: 'n', data: { length: 34, pending: 0 } } }, { slot: 'b', record: { form: 'rows', title: 'consumers', data: rowsData } }] };
const html = W.draw('composite', null, 'xl', { record: recXL, height: 300, width: 900 });
const slotB = (html.split('data-slot="b"')[1] || '');
t('the list slot is marked to scroll', /data-slot="b" data-scroll="1"/.test(html), html.slice(0, 200));
t('it draws all 30 rows, not "+ N more"', (slotB.match(/worker-\d+/g) || []).filter((v, i, a) => a.indexOf(v) === i).length === 30 && !/\+ \d+ more/.test(slotB));
t('the slot CSS scrolls it', /\.vw-slot\[data-scroll\] \.vw-slot-b\{align-items:flex-start;overflow-y:auto/.test(src));
t('the figures stay unscrolled', !/data-slot="a" data-scroll/.test(html));

// ── VeraDash: taller than six rows, and the XL floor ──
const maxr = +((VD.match(/var MAX_ROWS = (\d+);/) || [])[1] || 0);
t('a tile may be up to 16 rows', maxr === 16);
t('setSpan clamps to MAX_ROWS, not 6', /ch = Math\.max\(1, Math\.min\(MAX_ROWS, Math\.round\(\+span\[1\] \|\| 1\)\)\)/.test(VD) && !/Math\.min\(6, Math\.round\(\+span\[1\]/.test(VD));
t('the drag snaps up to MAX_ROWS', /targetH = snap\(Math\.round\(\(pxH \+ gapY\) \/ rowPitch\), 1, MAX_ROWS, null\)/.test(VD));
t('the row span is written inline too (host pages only know w-h1..w-h6)', /w\.classList\.add\('w-h' \+ n\); w\.style\.gridRow = 'span ' \+ n;/.test(VD));
t('no six-class clean-up loop is left', !/\[1, 2, 3, 4, 5, 6\]\.forEach/.test(VD));
t('the element\'s own grid CSS names every row span', /\[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16\]\.map\(function \(n\) \{ return 'vera-dashboard \.w-h' \+ n/.test(VD));
t('an XL composite is floored at its rows', /VW\.compositeXL\(xr\)\) \{ var s0 = spanOf\(w\), floor = \+VW\.xlCompositeRows \|\| 6; if \(s0\[1\] < floor\) setSpan\(w, \[s0\[0\], floor\]\);/.test(VD));

// ── the stack topology's default ──
const topo = MAIN.widgets.find((w) => w.record && w.record.id === 'topology-map');
t('the stack topology opens 10 rows tall at XL', topo && topo.span[1] === 10 && topo.record.frame.size === 'xl', topo && JSON.stringify([topo.span, topo.record.frame]));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
