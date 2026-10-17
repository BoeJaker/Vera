// A dashboard tile's head is the item's (DEEP-29 11A.4 step 5, owner decision D1 2026-10-16): the same row as a canvas item
// - pill, max, more, drop - on every grid VeraDash draws; the tile's own buttons are listed by its more menu (the buttons
// themselves, moved in and put back), never drawn in the head.
//   node tests/test_dashboard_item_head.cjs
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const S = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-dashboard.js'), 'utf8');
let n = 0; const t = (name, fn) => { fn(); n++; console.log('ok -', name); };

/* owner 2026-10-16: the head keeps the controls of what is in it - the tile's own buttons stay visible, no more menu */
t('every wired tile gets the item head', () => { assert(S.includes('      addPopButtons(w);\n      itemHead(w);\n    }')); });
t('the row is the item row, in order; a section divider keeps more and drop', () => {
  const i = S.indexOf('function itemHead(w)'); const B = S.slice(i, i + 2000);
  const o = ["hb('pill'", "hb('max'", "hb('drop'"].map((x) => B.indexOf(x)); assert(o.every((x) => x > 0)); assert.deepStrictEqual(o, o.slice().sort((a, b) => a - b)); assert(!B.includes("hb('more'"));
  assert(B.includes("(sec ? '' :"));
});
t('the tile\'s own buttons stay in its head (its hide button is the drop control\'s twin); the row shows on hover, focus, arranging, floating', () => {
  assert(S.includes("'.widget > .w-head > .w-actions > .vd-dup{display:none!important}',") && S.includes("if (isHideBtn(x)) x.classList.add('vd-dup');") && !S.includes('function tileMenu('));
  assert(S.includes("'.widget:is(:hover,:focus-within,.floating,.vd-full,.vd-pill) > .w-head > .vd-row,.dash-grid.editing .widget > .w-head > .vd-row{display:inline-flex}',"));
});
t('pill folds to the head line and is kept; max fills the view in page pixels and Esc restores; drop docks a floating tile', () => {
  assert(S.includes("'.dash-grid .widget.vd-pill{grid-row:span 1!important;") && S.includes("faceSet(w.dataset.wid, on ? 'pill' : '');") && S.includes("if (faces()[w.dataset.wid] === 'pill' && !sec) pillToggle(w, true);"));
  assert(S.includes('var z = (w.getBoundingClientRect().width / (w.offsetWidth || 1)) || 1') && S.includes("if (e.key === 'Escape' && w.classList.contains('vd-full'))"));
  assert(S.includes("if (a === 'drop') { if (w.classList.contains('floating')) return dock(w);"));
  assert(S.includes('z-index:var(--z-full,150)') && S.includes('z-index:var(--z-pop,230)'), 'the item z bands');
});
t('a popped-out tile is a window: the item band 130-149, the last pressed on top, one stack with the harness solo views', () => {
  const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'capability_orchestration.html'), 'utf8');
  assert(S.includes("var ZW = (window.VeraItem && window.VeraItem.Z && window.VeraItem.Z.window) || [130, 149];") && S.includes("x.style.zIndex = String(ZW[0] + Math.max(0, Math.min(span, span - (n - 1 - i))));") && S.includes('window.VeraWindows = window.VeraWindows || { raise: winRaise, drop: winDrop };'));
  assert(S.includes("'.widget.floating{position:fixed;z-index:var(--z-window,130);") && !/z-index:9000/.test(S));
  assert(H.includes('.widget.floating{position:fixed;z-index:var(--z-window,130);') && H.includes("window.VeraWindows.raise(el); el.addEventListener('mousedown'") && !/z-index:9000/.test(H));
});
console.log(n + ' passed');