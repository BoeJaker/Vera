// The dashboard's tile move in vera/chat/vera-dashboard.js (UI redesign, the Dashboard board's lifted tile and dashed
// slot; live defect 31 — "Configure then drag a widget: the page locks"): the one slot rule the pointer gesture and a
// native drop share (VeraDash.slotAt), and the text the behaviour hangs on — the move as the page's own pointer
// gesture under the drag guard, the edit-mode grid lines on a layer of their own (a background on the grid made
// every scroll step of the container a 75–150 ms job, and the drag's auto-scroll runs one per dragover), dragenter
// cancelled for the drop model, Escape and blur putting the tile back. The gesture itself is smoked in a browser.
//   node tests/test_dashboard_move.cjs
'use strict';
const fs = require('fs'); const path = require('path'); const vm = require('vm'); const assert = require('assert');
const ROOT = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'vera', 'chat', 'vera-dashboard.js'), 'utf8');

function stubEl() { return { style: {}, dataset: {}, classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} }, setAttribute() {}, getAttribute() { return null; }, appendChild() {}, remove() {}, querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, insertAdjacentHTML() {} }; }
function boot() {
  const window = { location: { hash: '' }, addEventListener() {}, innerWidth: 1440, innerHeight: 1000 };
  window.self = window; window.top = window;
  const document = Object.assign(stubEl(), { readyState: 'complete', head: stubEl(), body: stubEl(), documentElement: stubEl(), createElement: () => stubEl(), getElementById: () => null });
  const ctx = { window, document, localStorage: { getItem: () => null, setItem() {}, removeItem() {} }, console, setTimeout, clearTimeout, fetch: () => Promise.reject(new Error('no net')) };
  vm.createContext(ctx); vm.runInContext(src, ctx, { filename: 'vera-dashboard.js' });
  return ctx.window.VeraDash;
}
const J = (x) => JSON.parse(JSON.stringify(x)); const eq = (a, b, m) => assert.deepStrictEqual(J(a), J(b), m);
let n = 0; const T = (name, fn) => { try { fn(); n++; console.log('PASS', name); } catch (e) { console.log('FAIL', name, '\n  ', e && e.stack || e); process.exitCode = 1; } };

const VD = boot();
// a 12-column row of three tiles and a full-width one beneath, the second tile lifted
const R = (l, t, r, b) => ({ left: l, top: t, right: r, bottom: b });
const tiles = [
  { wid: 'a', rect: R(16, 100, 475, 446) }, { wid: 'b', rect: R(489, 100, 948, 446), src: true }, { wid: 'c', rect: R(961, 100, 1420, 446) },
  { wid: 'hid', rect: R(0, 0, 0, 0), hidden: true }, { wid: 'd', rect: R(16, 460, 1420, 700) }
];
T('over a tile: before it on its left half, after it (before the next) on the right', () => {
  eq(VD.slotAt(tiles, 100, 200), { next: 'a', over: 'a', before: true });
  eq(VD.slotAt(tiles, 400, 200), { next: 'c', over: 'a', before: false }, 'after a = before c (the lifted tile is not a target)');
  eq(VD.slotAt(tiles, 1000, 200), { next: 'c', over: 'c', before: true });
  eq(VD.slotAt(tiles, 1300, 200), { next: 'd', over: 'c', before: false });
  eq(VD.slotAt(tiles, 700, 600), { next: 'd', over: 'd', before: true }, 'the wide tile\'s left half');
  eq(VD.slotAt(tiles, 1000, 600), { next: null, over: 'd', before: false }, 'after the last tile: the end');
});
T('over the grid itself: before the first tile that follows in reading order, else the end', () => {
  eq(VD.slotAt(tiles, 700, 452), { next: 'd', over: null, before: true }, 'in the gap under the row: the next row');
  eq(VD.slotAt(tiles, 1430, 200), { next: 'd', over: null, before: true }, 'right of the row: the next row');
  eq(VD.slotAt(tiles, 8, 200), { next: 'a', over: null, before: true }, 'left of the row: before its first tile');
  eq(VD.slotAt(tiles, 700, 900), { next: null, over: null, before: true }, 'past the last tile: the end');
  eq(VD.slotAt(tiles, 700, 50), { next: 'a', over: null, before: true }, 'above everything: the first tile');
});
T('the lifted tile and hidden tiles are never targets; an empty grid lands at the end', () => {
  eq(VD.slotAt(tiles, 700, 200), { next: 'c', over: null, before: true }, 'over the lifted tile itself: as the grid');
  eq(VD.slotAt([{ wid: 'only', rect: R(0, 0, 100, 100), src: true }], 50, 50), { next: null, over: null, before: true });
  eq(VD.slotAt([], 50, 50), { next: null, over: null, before: true });
  eq(VD.slotAt(null, 50, 50), { next: null, over: null, before: true });
});
T('the behaviour the browser smoke drives is in the text', () => {
  for (const s of [
    'function onMoveDown(e)', 'function moveLift()', 'function moveTick()', 'function moveScrollTick()', 'function moveEnd(commit)',
    "if (e.target.closest('.w-resize, button, input, select, textarea, a[href], [contenteditable], iframe, .vd-ghost')) return;",
    'if (Math.abs(e.clientX - move.sx) + Math.abs(e.clientY - move.sy) < 4) return;', "_dragGuardOn('grabbing');",
    "m.w.style.transform = 'translate(' + dx + 'px,' + dy + 'px) rotate(-1deg) scale(1.02)';",
    "m.ghost.className = 'vd-ghost'", "'drop here · ' + sp[0] + ' × ' + sp[1]", "grid.addEventListener('mousedown', onMoveDown);",
    "if (e.key === 'Escape' && move) { e.preventDefault(); e.stopPropagation(); moveEnd(false); }", "window.addEventListener('blur', onMoveBlur);",
    'grid.insertBefore(m.w, next);', 'landed(m.w);',
    // the native path keeps every handler and takes the same rule
    'function onDragEnter(e) { if (state.editing && dragSrc) e.preventDefault(); }', "w.addEventListener('dragenter', onDragEnter);", "grid.addEventListener('dragenter', onDragEnter);",
    'var slot = slotAt(tileRects(dragSrc), e.clientX, e.clientY);', 'function onDragStart(e)', 'function onGridDragOver(e)', "document.addEventListener('dragover', _autoScrollOnDrag);",
    // the grid lines on their own layer, never on the grid's background
    "'.dash-grid.editing{position:relative;background-image:none}'", "'.dash-grid.editing::before{content:\"\";position:absolute;z-index:0;pointer-events:none;will-change:transform;'",
    "grid.style.setProperty('--vd-pl', (parseFloat(cs.paddingLeft) || 0) + 'px');", "'.vd-ghost{position:fixed;",
    "'.dash-grid.editing > .widget.dragging{transition:none}'", 'withSample: withSample, slotAt: slotAt };'
  ]) assert(src.includes(s), s);
  assert(!/'\.dash-grid\.editing\{background-image:linear-gradient/.test(src), 'no gradient background on the grid itself');
});
console.log(n + ' passed' + (process.exitCode ? ', some failed' : ''));
