// A WIDGET IS AS WIDE AS ITS FACE (owner, 2026-09-24: "canvas items that are smaller than a column have large blank
// areas i.e. widgets"). A sticker-sized widget was given a share of a column — a third, a half — and drew a 90px chip
// in it, leaving the rest of that share blank and pushing whatever came next onto the next row.
//
// The element answers how wide its face really is (a chip face is inline-flex and nowrap; every other face is
// width:100% by design and answers 0, meaning "as wide as you like"), the slot carries it, and the placer cuts the
// card down to it and flows the rest of the row against THAT.
//   node tests/test_canvas_widget_natural_width.cjs      (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const here = __dirname;
const WSRC = fs.readFileSync(path.join(here, '..', 'vera', 'widgets', 'widget_element.js'), 'utf8');
const SRC = fs.readFileSync(path.join(here, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

/* ── the element answers for its own face ────────────────────────────────────────────────────────────────────── */
const defined = {};
const ctx = { setTimeout, clearTimeout, window: {}, console, HTMLElement: class {}, CustomEvent: class {},
  customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } },
  document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {} }) } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document;
vm.runInNewContext(fs.readFileSync(path.join(here, '..', 'vera', 'ui', 'iso.js'), 'utf8'), ctx);
vm.runInNewContext(WSRC, ctx);
const C = defined['vera-widget'];
t('the element is defined and answers for its width', !!C && typeof C.prototype.naturalWidth === 'function');

const rootWith = (node, expectSel) => ({ querySelector(sel) { if (expectSel) expectSel(sel); return node; } });
const face = (w) => ({ getBoundingClientRect: () => ({ width: w }), scrollWidth: Math.round(w) });
const nw = (node, expectSel) => C.prototype.naturalWidth.call({ _sh: rootWith(node, expectSel) });

t('a chip face answers its own measured width', nw(face(92)) === 92, String(nw(face(92))));
t('...rounded up, so a card is never a fraction of a pixel too narrow', nw(face(92.2)) === 93, String(nw(face(92.2))));
t('a face that is meant to FILL answers 0 — as wide as you like', nw(null) === 0);
t('a zero-width face answers 0 as well: nothing measured is not a width', nw(face(0)) === 0);
t('a face with no rect falls back to its scroll width', C.prototype.naturalWidth.call({ _sh: rootWith({ scrollWidth: 77 }) }) === 77);
t('no shadow root, no answer', C.prototype.naturalWidth.call({ _sh: null }) === 0);
{
  let sel = '';
  nw(face(50), (s) => { sel = s; });
  t('only a DIRECT chip face of the root counts — a chip inside a bigger face is not the face',
    sel === '.vw-root > .vw-xs, .vw-root > .vw-chip', sel);
}
t('it is read-only: nothing is drawn, set or dispatched to answer it',
  /naturalWidth\(\) \{[\s\S]{0,420}?\n    \}/.test(WSRC)
  && !/naturalWidth\(\) \{[\s\S]{0,420}?(innerHTML|setAttribute|dispatchEvent|render\()/.test(WSRC));

/* ── the canvas asks, and the placer uses the answer ─────────────────────────────────────────────────────────── */
t('the canvas asks when the face has been drawn, and again when it is redrawn',
  /inner\.addEventListener\('widget:rendered', \(\) => this\._widgetFit\(key, inner\)\); \}/.test(SRC)
  && /this\._widgetFit\(key, inner\);   \/\/ a re-rendered slot is a fresh face/.test(SRC));
t('the slot carries the answer, and only a change to it costs a placement',
  /if \(now\) h\.dataset\.natw = now; else delete h\.dataset\.natw;/.test(SRC) && /if \(was === now\) return;/.test(SRC));
t('an element that cannot answer is left alone', /typeof inner\.naturalWidth !== 'function'\) return;/.test(SRC));
t('the card is cut down to the face plus the card\'s OWN padding, measured rather than assumed',
  /const chrome = Math\.max\(0, c\.clientWidth - s\.clientWidth\);/.test(SRC)
  && /const px = nat \+ chrome; const share = c\.clientWidth;/.test(SRC));
t('...only when it really is narrower than its share', /return px > 40 && px < share - 8 \? px : 0;/.test(SRC));
t('and the placer flows the rest of the row against the width it actually got',
  /wants\[i\] = Math\.max\(0\.08, \(natural\[i\] \+ gap\) \/ \(w \+ gap\)\);/.test(SRC)
  && /want: wants\[ci\],/.test(SRC));
t('read every card, then write every card — a read and a write per card in one loop is a flush per card',
  SRC.indexOf('const natural = cards.map((c, i) => {') < SRC.indexOf('cards.forEach((c, i) => { if (!natural[i]) return;'));
t('an item that asks for a column or more is not narrowed, and neither is one you opened or dragged',
  /if \(wants\[i\] >= 1 \|\| c\.classList\.contains\('openin'\) \|\| c\.classList\.contains\('sized'\) \|\| c\.classList\.contains\('compact'\)\) return 0;/.test(SRC));
t('nothing is written to the canvas: no size record changes, the card just fits its content',
  !/_widgetFit[\s\S]{0,700}?canvas\.update/.test(SRC));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
