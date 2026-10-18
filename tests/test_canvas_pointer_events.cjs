// WSU-58 S8 (DEEP-29 11A.4 Decision 4.7, B12): a desk card's head, its corner grip and a composite's tab are pressed, carried and
// let go by POINTER events (pointerdown / pointermove / pointerup + pointercancel), the pointer held (setPointerCapture) while
// it carries, so a finger or a pen works as the mouse does. Every threshold and rule is the mouse's as it was. A real mouse's own
// mouse events (sent after its pointer events) only stop the text selection; a SYNTHETIC mouse event (isTrusted false - the
// probes and tests that dispatch MouseEvent) is forwarded to the same press, move and release.
//
// The element's own handler block (from `const rzPress` to the picker's search box) is run against a stub host, body and
// document, and driven with trusted pointer events, a second finger, the real mouse's trailing mouse events and synthetic ones.
//   node tests/test_canvas_pointer_events.cjs      (CommonJS: the gate parses js as scripts)
'use strict';
const fs = require('fs'), path = require('path');
const SRC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

/* ── the source: pointer handlers bound, no mouse-driven drag left ───────────────────────────────────────────────── */
const a = SRC.indexOf('      const rzPress = (ev) =>'), b = SRC.indexOf("      // a picker's search box");
t('the handler block is found', a > 0 && b > a);
const BLOCK = SRC.slice(a, b);
t('grip, tab and card are pressed by pointerdown', /body\.addEventListener\('pointerdown', \(ev\) => \{ if \(ev\.isPrimary === false\) return; if \(rzPress\(ev\)\)/.test(BLOCK)
  && BLOCK.includes("body.addEventListener('pointerdown', (ev) => { if (ev.isPrimary !== false) tabPress(ev); });")
  && BLOCK.includes("body.addEventListener('pointerdown', (ev) => { if (ev.isPrimary !== false) dgPress(ev); });"));
t('moves and releases are pointermove / pointerup / pointercancel', (BLOCK.match(/addEventListener\('pointermove'/g) || []).length === 3
  && (BLOCK.match(/\['pointerup', 'pointercancel'\]\.forEach/g) || []).length === 3);
t('the pointer is held while it carries (setPointerCapture through _ptrHold)', SRC.includes('_ptrHold(ev) { if (!ev || ev.pointerId == null') && SRC.includes('this.setPointerCapture(ev.pointerId)')
  && (BLOCK.match(/this\._ptrHold\(ev\)/g) || []).length === 3);
/* every mouse listener left in the block either forwards a synthetic event only, or only stops the selection */
const mouse = BLOCK.match(/addEventListener\('mouse(down|move|up)'[^\n]*/g) || [];
t('no mouse-driven drag handler is left: each mouse listener forwards only a synthetic event (isTrusted false)', mouse.length === 9
  && mouse.every((l) => /if \(!ev\.isTrusted\) (rzPress|tabPress|dgPress|tabMove|dgMove|rzMove|tabUp|dgUp|rzUp)\(/.test(l) || /ev\.preventDefault\(\);? (ev\.stopPropagation\(\); )?if \(!ev\.isTrusted\)/.test(l) || /\.rz'\); if \(!g \|\| !g\.closest/.test(l)), mouse.join('\n'));
t('touch and pen are not taken for a scroll on a desk head, grip or tab', SRC.includes(':host([align="desk"]) .stage .it > .it-hd,:host([align="desk"]) .stage .it > .rz,:host([align="desk"]) .stage .cmp-tab{touch-action:none}'));
t('the double click (LV-8 expand) is as it was', BLOCK.includes("body.addEventListener('dblclick', (ev) => { const it = deskHandle(ev); if (!it) return; ev.preventDefault(); this._expandToggle(it.dataset.key); });"));

/* ── the block, run ─────────────────────────────────────────────────────────────────────────────────────────────── */
const listeners = () => { const L = {}; return { L, addEventListener(type, fn) { (L[type] = L[type] || []).push(fn); }, fire(ev) { (L[ev.type] || []).forEach((fn) => fn(ev)); } }; };
const cls = () => { const s = new Set(); return { add: (...x) => x.forEach((y) => s.add(y)), remove: (...x) => x.forEach((y) => s.delete(y)), contains: (x) => s.has(x), toggle: (x, on) => (on ? s.add(x) : s.delete(x)), s }; };
const node = (o) => Object.assign({ dataset: {}, classList: cls(), style: { setProperty(k, v) { this[k] = v; } }, offsetWidth: 300, offsetHeight: 200, clientWidth: 1000, clientHeight: 800,
  getBoundingClientRect() { return { left: 0, top: 0, width: this.offsetWidth, height: this.offsetHeight, right: this.offsetWidth, bottom: this.offsetHeight }; }, getClientRects: () => [1],
  _cl: {}, closest(sel) { return this._cl[sel] || null; }, parentElement: null }, o);
function world() {
  const stage = node({ offsetWidth: 1000, offsetHeight: 800 });
  const card = node({ dataset: { key: 'note:a', deskFace: 'card' }, style: { left: '100px', top: '100px', setProperty(k, v) { this[k] = v; } } });
  const grip = node({}); grip._cl = { '.rz': grip, '.it[data-key]': card };
  const hd = node({}); hd._cl = { '.it-hd': hd, '.stage .it[data-key]': card };
  const strip = node({ getBoundingClientRect: () => ({ left: 100, top: 100, right: 400, bottom: 130, width: 300, height: 30 }) });
  const tab = node({ dataset: { layerKey: 'note:b' }, parentElement: strip }); tab._cl = { '.cmp-tab[data-layer-key]': tab };
  const body = listeners(), doc = listeners(); const calls = []; const held = [];
  const host = { ownerDocument: doc, _pw: {}, _px: {}, _open: new Set(), _deskGeo: { W: 1000, H: 800, R: {}, desk: {} },
    shadowRoot: { getElementById: (id) => (id === 'stage' ? stage : null), querySelectorAll: () => [] },
    deskAlign: () => true, deskResizeMode: () => 'pin', hasAttribute: () => true, setPointerCapture: (id) => held.push(id), dispatchEvent() {}, render() {},
    _lockGet: () => ({}), _pillGet: () => ({}), _winKeepGet: () => ({}), _deskPosGet: () => ({}), _cmpGet: () => ({}), _gapAt: () => null };
  ['_holdStart', '_liveLayout', '_liveFollow', '_placeNow', '_histPush', '_joinSet', '_sepSet', '_deskPosSet', '_focusSet', '_pillSet', '_winKeepSet', '_cmpSet', '_expandToggle', '_toggleMax', '_setSize']
    .forEach((m) => { host[m] = (...args) => calls.push([m].concat(args)); });
  /* the element's own _ptrHold, as written */
  const ph = SRC.match(/\n    _ptrHold\(ev\) \{([^\n]*)\}\n/); host._ptrHold = new Function('ev', ph[1]);
  const root = { VeraDesk: { sizeAt: () => ({ face: 'card', w: 300, h: 200, t: 0.5 }) } };
  const run = new Function('body', 'root', 'zOf', 'sizeOfHeight', 'CustomEvent', 'requestAnimationFrame', BLOCK);
  run.call(host, body, root, () => 150, () => 'm', function CustomEvent() {}, () => 0);
  const ev = (type, target, o) => Object.assign({ type, target, button: 0, clientX: 0, clientY: 0, isTrusted: true, dp: false, sp: false,
    preventDefault() { this.dp = true; }, stopPropagation() { this.sp = true; }, composedPath: () => [] }, o);
  return { host, body, doc, calls, held, card, grip, hd, tab, ev, has: (m) => calls.some((c) => c[0] === m) };
}

{ /* the grip: a trusted pointer sizes the card; its own trailing mouse events change nothing but the selection */
  const w = world(); const { host, body, doc, ev } = w;
  body.fire(ev('pointerdown', w.grip, { pointerId: 7, isPrimary: true, pointerType: 'mouse', clientX: 400, clientY: 300 }));
  const r = host._rz; t('grip: a pointerdown starts the resize, with the card\'s size as it was', !!r && r.h0 === 200 && r.w0 === 300 && r.pid === 7);
  t('grip: the pointer is held', w.held.join() === '7');
  const md = ev('mousedown', w.grip, { clientX: 400, clientY: 300 }); body.fire(md);
  t('grip: the real mouse\'s own mousedown only stops the selection and the press reaching the page', md.dp && md.sp && host._rz === r);
  doc.fire(ev('pointermove', w.grip, { pointerId: 8, isPrimary: false, clientX: 600, clientY: 600 }));
  t('grip: another pointer (a second finger) is not the hand', r.h === 200 && r.w === 300);
  doc.fire(ev('mousemove', w.grip, { clientX: 600, clientY: 600 }));
  t('grip: the real mouse\'s own mousemove is not a second move', r.h === 200);
  doc.fire(ev('pointermove', w.grip, { pointerId: 7, clientX: 450, clientY: 340 }));
  t('grip: the pointer sizes both axes as the mouse did (w0 + dx, h0 + dy)', r.w === 350 && r.h === 240, r.w + 'x' + r.h);
  doc.fire(ev('pointerup', w.grip, { pointerId: 7, clientX: 450, clientY: 340 }));
  t('grip: pointerup lets go and keeps the person\'s rectangle', host._rz === null && w.calls.some((c) => c[0] === '_deskPosSet' && c[1] === 'note:a' && c[2].w === 350 && c[2].h === 240));
}
{ /* the grip by a synthetic mouse (a probe): forwarded, nothing held */
  const w = world(); const { host, body, doc, ev } = w;
  const md = ev('mousedown', w.grip, { isTrusted: false, clientX: 400, clientY: 300 }); body.fire(md);
  t('grip (synthetic): a dispatched mousedown starts the resize and stops the press as before', !!host._rz && md.dp && md.sp && w.held.length === 0);
  doc.fire(ev('mousemove', w.grip, { isTrusted: false, clientX: 380, clientY: 320 }));
  t('grip (synthetic): the dispatched mousemove sizes it', host._rz.w === 280 && host._rz.h === 220);
  doc.fire(ev('mouseup', w.grip, { isTrusted: false }));
  t('grip (synthetic): the dispatched mouseup lets go', host._rz === null && w.has('_deskPosSet'));
}
{ /* a card carried by its head: by touch */
  const w = world(); const { host, body, doc, ev } = w;
  body.fire(ev('pointerdown', w.hd, { pointerId: 3, isPrimary: true, pointerType: 'touch', clientX: 200, clientY: 110 }));
  const d = host._dg; t('card: a touch on the head picks it up (not moved yet)', !!d && d.moved === false && d.pid === 3 && w.held.length === 0);
  body.fire(ev('pointerdown', w.hd, { pointerId: 4, isPrimary: false, pointerType: 'touch', clientX: 500, clientY: 500 }));
  t('card: a second finger does not pick anything else up', host._dg === d);
  doc.fire(ev('pointermove', w.hd, { pointerId: 3, clientX: 202, clientY: 111 }));
  t('card: under 4 px it has not moved (the threshold as it was)', d.moved === false);
  doc.fire(ev('pointermove', w.hd, { pointerId: 3, clientX: 240, clientY: 150 }));
  t('card: past it, it follows the pointer, on top, and the pointer is held', d.moved === true && d.el.classList.contains('desk-dragging') && w.held.join() === '3' && w.has('_holdStart'));
  doc.fire(ev('pointerup', w.hd, { pointerId: 3, clientX: 240, clientY: 150 }));
  t('card: let go, it stays where it was let go', host._dg === null && w.calls.some((c) => c[0] === '_deskPosSet' && c[1] === 'note:a' && c[2] && c[2].fx > 0) && host._dgEnd > 0);
}
{ /* a card by the real mouse: its own mousedown stops the selection only; a press that does not move is a click */
  const w = world(); const { host, body, doc, ev } = w;
  body.fire(ev('pointerdown', w.hd, { pointerId: 1, isPrimary: true, pointerType: 'mouse', clientX: 200, clientY: 110 })); const d = host._dg;
  const md = ev('mousedown', w.hd, { clientX: 200, clientY: 110 }); body.fire(md);
  t('card: the mouse\'s own mousedown stops the text selection and starts nothing new', md.dp && host._dg === d);
  doc.fire(ev('pointerup', w.hd, { pointerId: 1, clientX: 200, clientY: 110 }));
  t('card: a press without a move is not a carry (nothing placed, nothing held)', host._dg === null && !w.has('_deskPosSet') && w.held.length === 0 && !host._dgEnd);
  body.fire(ev('pointerdown', w.hd, { pointerId: 1, isPrimary: true, button: 2, clientX: 200, clientY: 110 }));
  t('card: another button than the first is ignored, as before', host._dg == null);
  body.fire(ev('dblclick', w.hd, {}));
  t('card: a double click on the head still expands it (LV-8)', w.calls.some((c) => c[0] === '_expandToggle' && c[1] === 'note:a'));
}
{ /* a card by a synthetic mouse (the LV-0 gestures, snip_*.js) */
  const w = world(); const { host, body, doc, ev } = w;
  body.fire(ev('mousedown', w.hd, { isTrusted: false, clientX: 200, clientY: 110 }));
  doc.fire(ev('mousemove', w.hd, { isTrusted: false, clientX: 260, clientY: 170 }));
  t('card (synthetic): carried by dispatched mouse events', !!host._dg && host._dg.moved === true && w.held.length === 0);
  doc.fire(ev('mouseup', w.hd, { isTrusted: false }));
  t('card (synthetic): let go', host._dg === null && w.has('_deskPosSet'));
}
{ /* a tab carried out of its strip by a pen */
  const w = world(); const { host, body, doc, ev } = w;
  body.fire(ev('pointerdown', w.tab, { pointerId: 5, isPrimary: true, pointerType: 'pen', clientX: 150, clientY: 115 }));
  const d = host._tabDrag; t('tab: a pen on a tab presses it', !!d && d.key === 'note:b' && d.pid === 5);
  doc.fire(ev('pointermove', w.tab, { pointerId: 5, clientX: 150, clientY: 155 }));
  t('tab: 25 px off the strip is still a click (the 28 px rule as it was), nothing held', d.out === false && w.held.length === 0);
  doc.fire(ev('pointermove', w.tab, { pointerId: 5, clientX: 150, clientY: 170 }));
  t('tab: past 28 px it is taken out, and the pointer is held', d.out === true && d.tab.classList.contains('tab-out') && w.held.join() === '5');
  doc.fire(ev('pointerup', w.tab, { pointerId: 5, clientX: 150, clientY: 170 }));
  t('tab: let go, it is a card of its own where it was let go', host._tabDrag === null && w.calls.some((c) => c[0] === '_sepSet' && c[1] === 'note:b' && c[2] === true) && w.calls.some((c) => c[0] === '_deskPosSet' && c[1] === 'note:b'));
}
{ /* a tab by a synthetic mouse (the LV-0 GESTURES carry) */
  const w = world(); const { host, body, doc, ev } = w;
  body.fire(ev('mousedown', w.tab, { isTrusted: false, clientX: 150, clientY: 115 }));
  doc.fire(ev('mousemove', w.tab, { isTrusted: false, clientX: 150, clientY: 200 }));
  doc.fire(ev('mouseup', w.tab, { isTrusted: false, clientX: 150, clientY: 200 }));
  t('tab (synthetic): carried out by dispatched mouse events', host._tabDrag === null && w.calls.some((c) => c[0] === '_sepSet' && c[1] === 'note:b'));
  const w2 = world();
  w2.body.fire(w2.ev('mousedown', w2.tab, { clientX: 150, clientY: 115 }));
  t('tab: the real mouse\'s own mousedown presses nothing (its pointerdown did)', w2.host._tabDrag == null);
}
console.log(fails ? fails + ' failed' : 'all passed'); process.exit(fails ? 1 : 0);
