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

t('every wired tile gets the item head', () => { assert(S.includes('      addPopButtons(w);\n      itemHead(w);\n    }')); });
t('the row is the item row, in order; a section divider keeps more and drop', () => {
  const i = S.indexOf('function itemHead(w)'); const B = S.slice(i, i + 2000);
  const o = ["hb('pill'", "hb('max'", "hb('more'", "hb('drop'"].map((x) => B.indexOf(x)); assert(o.every((x) => x > 0)); assert.deepStrictEqual(o, o.slice().sort((a, b) => a - b));
  assert(B.includes("(sec ? '' :"));
});
t('the tile\'s own buttons are never drawn in the head; the row shows on hover, focus, arranging, floating', () => {
  assert(S.includes("'.widget > .w-head > .w-actions{display:none!important}',"));
  assert(S.includes("'.widget:is(:hover,:focus-within,.floating,.vd-full,.vd-pill) > .w-head > .vd-row,.dash-grid.editing .widget > .w-head > .vd-row{display:inline-flex}',"));
});
t('the more menu shows the tile\'s own buttons and puts them back', () => {
  assert(S.includes('function tileMenu(w, btn)') && S.includes('row.appendChild(b);') && S.includes('pop._close = function () { back.forEach('));
  assert(S.includes("filter(function (b) { return !(b.tagName === 'BUTTON' && isHideBtn(b)); })"), 'hide is the drop control, not a menu entry');
});
t('pill folds to the head line and is kept; max fills the view in page pixels and Esc restores; drop docks a floating tile', () => {
  assert(S.includes("'.dash-grid .widget.vd-pill{grid-row:span 1!important;") && S.includes("faceSet(w.dataset.wid, on ? 'pill' : '');") && S.includes("if (faces()[w.dataset.wid] === 'pill' && !sec) pillToggle(w, true);"));
  assert(S.includes('var z = (w.getBoundingClientRect().width / (w.offsetWidth || 1)) || 1') && S.includes("if (e.key === 'Escape' && w.classList.contains('vd-full'))"));
  assert(S.includes("if (a === 'drop') { if (w.classList.contains('floating')) return dock(w);"));
  assert(S.includes('z-index:var(--z-full,150)') && S.includes('z-index:var(--z-pop,230)'), 'the item z bands');
});
console.log(n + ' passed');
