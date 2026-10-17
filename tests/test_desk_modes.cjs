// DR-01..03 (owner decision D2, 2026-10-16): the desk's modes and the placement score, in the layout kernel.
//   node tests/test_desk_modes.cjs
'use strict';
const assert = require('assert');
const path = require('path');
const D = require(path.join(__dirname, '..', 'vera', 'ui', 'desk_layout_core.js'));
let n = 0; const t = (name, fn) => { fn(); n++; console.log('ok -', name); };

const R = { l: -440, t: 0, r: 0, b: 900 };                       // the chat docked on the canvas's left
const d = { W: 1200, H: 900, m: 8, top: 4 };
const it = (id, kind, rel, extra) => Object.assign({ id, kind, rel: rel || [] }, extra || {});
const overlap = (a, b) => Math.abs(a.x - b.x) * 2 < a.w + b.w && Math.abs(a.y - b.y) * 2 < a.h + b.h;
const noOverlaps = (cells) => { const c = Object.values(cells); for (let i = 0; i < c.length; i++) for (let j = i + 1; j < c.length; j++) assert(!overlap(c[i], c[j]), 'cells overlap'); };
const inRegion = (cells, F) => Object.values(cells).forEach((c) => { assert(c.x - c.w / 2 >= F.l - 0.5 && c.x + c.w / 2 <= F.r + 0.5 && c.y - c.h / 2 >= F.t - 0.5 && c.y + c.h / 2 <= F.b + 0.5, 'cell outside the free region'); });
const F = D.freeRegion(R, d, 170);

t('the old modes map onto the new ones; unknown is auto', () => {
  assert.strictEqual(D.modeOf('orbit'), 'auto'); assert.strictEqual(D.modeOf('stack'), 'auto');
  assert.strictEqual(D.modeOf('kind'), 'map'); assert.strictEqual(D.modeOf('timeline'), 'flow');
  assert.strictEqual(D.modeOf('compare'), 'compare'); assert.strictEqual(D.modeOf('nope'), 'auto');
  assert.strictEqual(D.layoutMode('auto', R, [it('a', 'x')], d), null);
});
t('the free region is beside the chat, short of the pill strip', () => {
  assert.deepStrictEqual([F.l, F.r, F.farRight], [14, 1022, true]);
});
t('focus: the lead takes 60% of the free width, its relations beside it, the rest pills', () => {
  const L = D.layoutMode('focus', R, [it('lead', 'records', ['m1']), it('rel1', 'table', ['m1']), it('rel2', 'widget', ['m1']), it('other', 'code', ['m9']), it('out', 'note', [], { border: true })], d, { strip: 170 });
  assert(Math.abs(L.cells.lead.w - Math.round((F.r - F.l) * 0.6)) <= 1);
  assert(L.cells.rel1 && L.cells.rel2 && !L.cells.other);
  assert.deepStrictEqual(L.chips.sort(), ['other', 'out']);
  noOverlaps(L.cells); inRegion(L.cells, F);
});
t('compare: up to four of the lead\'s kind, the same size, tops aligned', () => {
  const L = D.layoutMode('compare', R, [it('a', 'widget'), it('b', 'widget'), it('c', 'widget'), it('x', 'code')], d, { strip: 170 });
  const c = ['a', 'b', 'c'].map((k) => L.cells[k]);
  assert(c.every((x) => x && x.w === c[0].w && x.h === c[0].h)); assert(c.every((x) => x.y - x.h / 2 === c[0].y - c[0].h / 2));
  assert.deepStrictEqual(L.chips, ['x']); noOverlaps(L.cells); inRegion(L.cells, F);
});
t('flow: in the order the work happened, reading order', () => {
  const L = D.layoutMode('flow', R, [it('c', 'x', [], { t: 3 }), it('a', 'x', [], { t: 1 }), it('b', 'x', [], { t: 2 })], d, { strip: 170 });
  assert(L.cells.a.x < L.cells.b.x && L.cells.b.x < L.cells.c.x || L.cells.a.y < L.cells.c.y);
  noOverlaps(L.cells); inRegion(L.cells, F);
});
t('board: four lanes by status, items under their lane\'s heading', () => {
  const L = D.layoutMode('board', R, [it('r1', 'activity', [], { status: 'running' }), it('d1', 'research', [], { status: 'done' }), it('n1', 'loop', [], { status: 'needs' }), it('t1', 'note')], d, { strip: 170 });
  assert.deepStrictEqual(L.lanes.map((l) => l.label), ['To do', 'Running', 'Needs you', 'Done']);
  const laneOf = (k) => L.lanes.findIndex((l) => L.cells[k].x - L.cells[k].w / 2 >= l.x - 0.5 && L.cells[k].x + L.cells[k].w / 2 <= l.x + l.w + 0.5);
  assert.deepStrictEqual(['t1', 'r1', 'n1', 'd1'].map(laneOf), [0, 1, 2, 3]);
  noOverlaps(L.cells); inRegion(L.cells, F);
});
t('map: what shares context clusters together, each cluster labelled by what it shares', () => {
  const L = D.layoutMode('map', R, [it('a1', 'x', ['m1']), it('a2', 'y', ['m1']), it('a3', 'z', ['m1']), it('b1', 'x', ['m2']), it('b2', 'y', ['m2'])], d, { strip: 170 });
  assert.strictEqual(L.lanes.length, 2); assert.strictEqual(L.lanes[0].n, 3); assert.strictEqual(L.lanes[0].label, 'm1');
  noOverlaps(L.cells); inRegion(L.cells, F);
});
t('free: nothing is laid out (the person\'s places stand)', () => { const L = D.layoutMode('free', R, [it('a', 'x')], d); assert(L.free && !Object.keys(L.cells).length); });
t('placement score: a new item goes into free room, near what it shares keys with, never over what is placed', () => {
  const placed = [{ x: 200, y: 150, w: 360, h: 260, rel: ['m1'] }, { x: 800, y: 150, w: 360, h: 260, rel: ['m2'] }];
  const s1 = D.bestSlot({ w: 300, h: 200, rel: ['m2'] }, placed, R, d, { strip: 170 });
  assert(s1 && placed.every((p) => !overlap(p, s1)));
  assert(Math.abs(s1.x - 800) < Math.abs(s1.x - 200), 'next to the item sharing its keys');
  const full = D.bestSlot({ w: 2000, h: 2000 }, placed, R, d); assert.strictEqual(full, null);
});
/* the canvas and the chat use them */
const fs = require('fs');
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
t('the canvas lays a mode out by the kernel, cells sizing the cards; rise and grow only in auto', () => {
  assert(C.includes("const mode = D.modeOf ? D.modeOf(this.getAttribute('desk-arrange')) : 'auto';") && C.includes("const ML = mode !== 'auto' && D.layoutMode ? D.layoutMode(mode, R, info, desk"));
  assert(C.includes("const so = { size: user || msz || ksz, pinned: (!!user && c.classList.contains('pinned')) || (!user && !!msz)"));
  assert(C.includes('if (!hold && !modeLaid && typeof D.grow'));
});
t('a newcomer is placed by the score; the hold survives arrivals', () => {
  assert(C.includes("(this._hold.keys || []).forEach((k) => { if (!now.has(k) && this._hold.rects) delete this._hold.rects[k]; });") && C.includes('const p = (!pilledKey && userAt(key)) || (fresh && !(mode === \'slots\' && ML && ML.cells[key]) && slotFor(key)) || pos[key]'));
});
t('lanes and the proposal are drawn on the desk', () => {
  assert(C.includes("el.className = 'desk-lane';") && C.includes('    _propose(st, mode, info, T, W) {') && C.includes("this._propUndo = { from: this.getAttribute('desk-arrange') || 'auto'"));
});
t('the chat offers the new modes, keeps a proposal on the Settings sheet, and a pick pins', () => {
  assert(H.includes('<option value="focus">Focus') && H.includes("['board','board','Columns: to do, running, needs you, done']"));
  assert(H.includes("el.addEventListener('vera:canvas:mode'") && H.includes("localStorage.setItem('vera:canvas:modepin', a.n==='auto'?'':'1')"));
});
/* LV-3 / LV-4: the slots arrangement - the kernel mode, the pre-pass that composites before anything is drawn */
t('slots: a mode of the kernel that fits by aspect; the canvas composites who shares a slot before drawing', () => { const Dk = require(path.join(__dirname, '..', 'vera', 'ui', 'desk_layout_core.js'));
  assert(Dk.MODES.includes('slots') && Dk.modeOf('slots') === 'slots');
  const R0 = { l: -440, t: 0, r: 0, b: 1000 }; const it = (id, aspect) => ({ id, kind: 'widget', aspect, rel: [] });
  const ML = Dk.layoutMode('slots', R0, [it('chart', 'landscape'), it('news', 'portrait'), it('t1', 'square'), it('t2', 'square')], { W: 1240, H: 1000, m: 8, top: 4 }, { strip: 170 });
  assert(ML && ML.cells.chart && ML.cells.news && ML.cells.t1 && ML.cells.t2 && ML.fit && ML.cells.news.h > 900 && ML.cells.chart.w > ML.cells.chart.h);
  assert(C.includes('this._slotUnion = un;') && C.includes('if (slotsU) { const byK') && C.includes("status: statusOf(x.c), aspect: this._aspectOf(x.c, x.c.dataset.key || '') }; });")); });
console.log(n + ' passed');
