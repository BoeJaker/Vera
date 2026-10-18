// WSU-58 S8 (DEEP-29 11A.4 Decision 5.2 / 5.3): one natural(width) per body. _deskFit, _widgetFit, _previewH and _diagramGrew
// ask the body's natural (registered with VeraItem.registerBody beside fit and minSize) instead of working the size out
// themselves; the numbers must be the ones they worked out before. The old formulas are copied below as they were (bleeding-
// edge-design 37c94196) and compared with the registered naturals on 4000 measured fixtures each. And DESK_FORM_MAXW moved into
// the widget catalogue as maxW per form (VeraWidget.forms; widget_record.DESK_MAXW): the same numbers, read by formMaxW.
//   node tests/test_body_natural.cjs      (CommonJS: the gate parses js as scripts)
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');
const ROOT = path.join(__dirname, '..');
const FILE = path.join(ROOT, 'vera', 'canvas', 'canvas_element.js');
const SRC = fs.readFileSync(FILE, 'utf8');
const V = require(FILE); const VI = require(path.join(ROOT, 'vera', 'ui', 'vera_item.js'));
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

/* a fixed pseudo-random stream, so a failure is the same failure every run */
let seed = 58; const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; };
const num = (lo, hi, frac) => { const v = lo + rnd() * (hi - lo); return frac ? v : Math.round(v); };
const N = 4000;

/* ── the registry ── */
const KINDS = Object.keys(V.BLOCK).filter((k) => k[0] !== '_');
t('every body registers a natural(width, m)', KINDS.every((k) => typeof VI.body(k).natural === 'function'), KINDS.filter((k) => typeof VI.body(k).natural !== 'function').join());
t('the diagram, the widget and the image have their own; the rest share the content one',
  VI.body('diagram').natural === V.NATURAL.diagram && VI.body('widget').natural === V.NATURAL.widget && VI.body('note').natural === V.NATURAL._ && VI.body('code').natural === V.NATURAL._
  && typeof V.NATURAL.image === 'function');
t('the container asks the registered natural (VeraItem.body), else its own', SRC.includes("_natural(kind, width, m) { const b = VI && typeof VI.body === 'function' ? VI.body(kind) : null; const f = (b && b.natural) || NATURAL[kind] || NATURAL._; return f(width, m) || {}; }"));
t('the four callers ask natural; the old formulas are gone from them', (SRC.match(/this\._natural\(/g) || []).length === 7
  && !SRC.includes('Math.max(96, Math.round(c.offsetHeight - bd.clientHeight + (low - B.top) / Z + 14))') && !SRC.includes('const nat = inner.naturalHeight(w)')
  && !SRC.includes('nw = inner.naturalWidth() || 0') && !SRC.includes('Math.max(96, Math.round(c.offsetHeight - pv.clientHeight + ph + 4))') && !SRC.includes('const want = c.offsetHeight + more + 8'));
const nat = (kind, w, m) => VI.body(kind).natural(w, m) || {};

/* ── the old formulas, as they were ── */
const OLD = {
  /* _deskFit, an image */
  image: (oh, ch, cw, iw, ih, capH) => { const s = Math.min(1.5, (cw - 8) / iw); return Math.max(96, Math.round(oh - ch + ih * s + (capH != null ? capH + 6 : 0) + 12)); },
  /* _deskFit, a widget's least height */
  minH: (oh, more) => (more > 6 ? oh + more + 8 : undefined),
  /* _deskFit, a drawn page (the height _previewH heard) */
  page: (oh, pvh, ph) => Math.max(96, Math.round(oh - pvh + ph + 4)),
  /* _deskFit, a text-like card's width */
  text: (head, used, ow, bw) => Math.max(240, head, used + (ow - bw) + 36),
  /* _deskFit, how far down the content reaches */
  content: (oh, bh, low, top, Z) => Math.max(96, Math.round(oh - bh + (low - top) / Z + 14)),
  /* _diagramGrew */
  diagram: (n) => (!n ? null : { dh: Math.max(60, Math.round(n)) + 'px', natural: String(Math.round(n)) }),
  /* _widgetFit */
  face: (f) => { let nw = 0; try { nw = f() || 0; } catch (e) { nw = 0; } return nw > 0 ? String(Math.round(nw)) : ''; },
};

{ let bad = '';
  for (let i = 0; i < N && !bad; i++) { const oh = num(40, 1400), ch = num(10, oh), cw = num(20, 1600), iw = num(1, 3000), ih = num(1, 3000), capH = rnd() < 0.5 ? num(10, 60) : null;
    const want = OLD.image(oh, ch, cw, iw, ih, capH), got = nat('image', cw, { img: { w: iw, h: ih }, cap: capH != null ? capH + 6 : 0, chrome: oh - ch }).h;
    if (got !== want) bad = JSON.stringify({ oh, ch, cw, iw, ih, capH, want, got }); }
  t('an image: the same height as _deskFit worked out', !bad, bad); }
{ let bad = '';
  for (let i = 0; i < N && !bad; i++) { const oh = num(40, 1400), more = num(-50, 400);
    const want = OLD.minH(oh, more), got = nat('widget', num(100, 900), { more, cardH: oh }).minH;
    if (got !== want) bad = JSON.stringify({ oh, more, want, got }); }
  t('a widget: the same least height (more than 6 px short: card + more + 8; else none)', !bad, bad); }
{ let bad = '';
  for (let i = 0; i < N && !bad; i++) { const oh = num(40, 1400), pvh = num(0, oh), ph = num(1, 4000); const kind = ['code', 'html', 'markdown', 'widget', 'diagram', 'image'][i % 6];
    const want = OLD.page(oh, pvh, ph), got = nat(kind, num(100, 900), { page: ph, chrome: oh - pvh }).h;
    if (got !== want) bad = JSON.stringify({ kind, oh, pvh, ph, want, got }); }
  t('a drawn page (_previewH\'s height): the same card height, whatever the kind', !bad, bad); }
{ let bad = '';
  for (let i = 0; i < N && !bad; i++) { const bw = num(100, 1500), ow = bw + num(0, 40), used = num(0, 1600), head = rnd() < 0.2 ? 0 : num(40, 600); const kind = ['note', 'markdown', 'table', 'kv', 'entities'][i % 5];
    const want = OLD.text(head, used, ow, bw), got = nat(kind, bw, { used, side: ow - bw, head }).w;
    if (got !== want) bad = JSON.stringify({ kind, bw, ow, used, head, want, got }); }
  t('a text-like card: the same width (WSU-69)', !bad, bad); }
{ let bad = '';
  for (let i = 0; i < N && !bad; i++) { const oh = num(40, 1400), bh = num(0, oh), top = num(0, 900, true), low = top + num(0, 1200, true), Z = [1, 1.25, 1.4, 0.8, 1.1][i % 5];
    const kind = ['note', 'markdown', 'table', 'kv', 'diagram', 'notebook', 'code', 'result'][i % 8];
    const want = OLD.content(oh, bh, low, top, Z), got = nat(kind, num(100, 900), { low: (low - top) / Z, chrome: oh - bh }).h;
    if (got !== want) bad = JSON.stringify({ kind, oh, bh, low, top, Z, want, got }); }
  t('content reaching down: the same height, under every UI scale', !bad, bad); }
{ let bad = '';
  for (let i = 0; i < N && !bad; i++) { const w = num(60, 1600, true); const k = rnd(); const n = k < 0.05 ? 0 : k < 0.08 ? NaN : num(0, 2000, true); const seen = [];
    const draw = { naturalHeight: (x) => { seen.push(x); return n; } };
    const old = OLD.diagram(n), got = nat('diagram', w, { draw });
    const same = old ? (got.h + 'px' === old.dh && String(got.drawn) === old.natural) : !got.h;
    if (!same || seen[0] !== w) bad = JSON.stringify({ w, n, old, got, seen }); }
  t('a diagram: the same --dh and data-natural, the drawing asked at the slot\'s width', !bad, bad); }
{ let bad = '';
  const faces = [() => 92, () => 93, () => 0, () => undefined, () => { throw new Error('x'); }, () => 410, () => 77];
  for (let i = 0; i < 500 && !bad; i++) { const f = faces[i % faces.length]; const face = { naturalWidth: f };
    const want = OLD.face(f), w = nat('widget', 0, { face }).w || 0, got = w > 0 ? String(w) : '';
    if (got !== want) bad = JSON.stringify({ i, want, got }); }
  t('a widget\'s face: the same width (a face that throws or says nothing is 0, as wide as you like)', !bad, bad); }
t('nothing measured, nothing said', JSON.stringify(nat('note', 300, {})) === '{}' && JSON.stringify(nat('widget', 300, {})) === '{}' && JSON.stringify(nat('diagram', 300, { draw: null })) === '{}');

/* ── DESK_FORM_MAXW is the catalogue's maxW now ── */
const OLD_MAXW = { checklist: 420, list: 440, links: 440, notices: 460, people: 460, kv: 520, pills: 480, string: 460, announcement: 460,
  counter: 380, hero: 400, ring: 360, radial: 360, dial: 360, gauge: 420, meter: 420, level: 380, glance: 460, numbers: 520, status: 480, stepper: 640, files: 520 };
t('the element has no table of its own: it reads the catalogue (formMaxW)', !SRC.includes('DESK_FORM_MAXW') && SRC.includes("if (wfm && formMaxW(wfm) && wfm !== 'files'") && SRC.includes('|| formMaxW(wf);'));
{
  const defined = {};
  const ctx = { setTimeout, clearTimeout, window: {}, console, HTMLElement: class {}, CustomEvent: class {},
    customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } },
    document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {} }) } };
  ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document;
  vm.runInNewContext(fs.readFileSync(path.join(ROOT, 'vera', 'ui', 'iso.js'), 'utf8'), ctx);
  vm.runInNewContext(fs.readFileSync(path.join(ROOT, 'vera', 'widgets', 'widget_element.js'), 'utf8'), ctx);
  const F = ctx.window.VeraWidget.forms(); const got = {}; F.forEach((f) => { if (f.maxW) got[f.id] = f.maxW; });
  t('the browser catalogue carries maxW on exactly the forms the table had, with the same numbers', JSON.stringify(Object.keys(got).sort().map((k) => [k, got[k]])) === JSON.stringify(Object.keys(OLD_MAXW).sort().map((k) => [k, OLD_MAXW[k]])), JSON.stringify(got));
  /* the element reads it */
  const prevW = global.window; global.window = undefined;
  const fm = SRC.slice(SRC.indexOf('  let FORM_MAXW = null;'), SRC.indexOf('  /* EVERY ELEMENT HAS A PREFERRED ASPECT'));
  const formMaxW = new Function('root', fm + '; return formMaxW;')({ VeraWidget: ctx.window.VeraWidget });
  t('formMaxW answers what the table answered, for every form and for none', Object.keys(OLD_MAXW).every((k) => formMaxW(k) === OLD_MAXW[k]) && !formMaxW('trace') && !formMaxW('table') && !formMaxW('') && !formMaxW('calendar'));
  const none = new Function('root', fm + '; return formMaxW;')({});
  t('without the catalogue on the page it caps nothing (and asks again later)', none('list') === 0);
  global.window = prevW;
}
const PY = fs.readFileSync(path.join(ROOT, 'vera', 'widgets', 'widget_record.py'), 'utf8');
{ const m = PY.match(/DESK_MAXW = \{([^}]*)\}/); const py = {}; (m ? m[1] : '').replace(/"([\w-]+)":\s*(\d+)/g, (_, k, v) => { py[k] = +v; });
  t('the python catalogue (widget_record.DESK_MAXW) holds the same numbers', JSON.stringify(py) === JSON.stringify(OLD_MAXW) || JSON.stringify(Object.keys(py).sort().map((k) => py[k])) === JSON.stringify(Object.keys(OLD_MAXW).sort().map((k) => OLD_MAXW[k])), JSON.stringify(py)); }
console.log(fails ? fails + ' failed' : 'all passed'); process.exit(fails ? 1 : 0);
