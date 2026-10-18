// Owner 2026-10-11: "elements don't always pack well and can leave big gaps even if i try to drag elements into the gap"
// (WSU-73) and "if i change the size of an element ... it also stops moving automatically - this is fine but there needs to
// be a way to restore it to normal behaviour, moving out of the way of other panels but keep the resize, perhaps it could
// become pinned or locked on resize - configurable in the settings" (WSU-74). Harness: tools/snip_gap_resize.js.
//   node tests/test_desk_gap_resize.cjs
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const R = path.join(__dirname, '..');
const CE = fs.readFileSync(path.join(R, 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const D = require(path.join(R, 'vera', 'ui', 'desk_layout_core.js'));
const CH = fs.readFileSync(path.join(R, 'vera', 'chat', 'chat_panel.html'), 'utf8');
// the kernel: a card with the person's size is never grown
{ const items = [{ id: 'a', x: 200, y: 200, w: 200, h: 150, own: true }, { id: 'b', x: 700, y: 200, w: 200, h: 150 }];
  const api = D.api || D; api.grow(items, { l: 0, r: 0, t: 0, b: 0 }, { W: 1200, H: 900 }, { maxW: 1100, maxH: 800 });
  t('grow leaves a card the person sized (own) at its size', items[0].w === 200 && items[0].h === 150, JSON.stringify(items[0]));
  t('...and still grows the others', items[1].w > 200 || items[1].h > 150, JSON.stringify(items[1])); }
t('a card let go in a gap is fitted into the free area round the drop point', CE.includes('_gapAt(cx, cy, except) {') && CE.includes('const gap = this._gapAt(d.cx, d.cy, d.key);') && CE.includes('if (gap && gap.w >= 200 && gap.h >= 90 && !d.own'));
t('the gap search is bounded (40 cells each way)', CE.includes('top >= Math.max(0, pj - 40)') && CE.includes('bot < Math.min(rows, pj + 41)'));
t('what a resize does: pin (default) / move (size kept, free) / lock', CE.includes("deskResizeMode() {") && CE.includes("if (mode === 'move') this._deskPosSet(key, { w: Math.round(r.w), h: Math.round(r.h), face, free: true });") && CE.includes("if (mode === 'lock' && !this._lockGet()[key]"));
t('back to the arrangement keeps the size (free entry), and placement reads it after a reload', CE.includes("this._deskPosSet(key, u && u.w > 0 && u.h > 0 ? { w: u.w, h: u.h, face: u.face, free: true } : null)") && CE.includes('const fr = up[key] && up[key].free && up[key].w > 0 ? up[key] : null;'));
t('a person\'s size beats the content fit and is drawn exactly while the card moves', CE.includes('const nat0 = user ? null : (this._natH || {})[key];') && CE.includes('own: !!user') && CE.includes(': it.own && !it.grown ? Object.assign({}, D.sizeAt(R, it.x, it.y, it.so, desk), { w: it.w, h: it.h'));
t('the Canvas menu offers the choice (keep moving / pin it / lock it)', CH.includes("When I resize a card") && CH.includes("if(a.a==='resize'){ if(typeof c.setDeskResizeMode==='function') c.setDeskResizeMode(a.n);"));
console.log(fails ? fails + ' failed' : 'all passed'); process.exit(fails ? 1 : 0);
