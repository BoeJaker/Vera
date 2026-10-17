// LV-2 (DESK-ARRANGEMENT 7): slots by aspect - portrait columns, landscape bands, a row of squares, a grid of tiles; out of room,
// an item joins a slot of its aspect (a composite) instead of every slot shrinking; the fewest cards moved from where they are.
//   node tests/test_desk_fit_layout.cjs
'use strict';
const path = require('path');
const D = require(path.join(__dirname, '..', 'vera', 'ui', 'desk_layout_core.js'));
let bad = 0; const t = (name, ok, extra) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name + (ok || !extra ? '' : '  ' + JSON.stringify(extra))); };
const REG = { l: 8, t: 8, r: 1229, b: 1196 };            // the owner's desk on the design mirror (1237 x 1204)
const it = (id, aspect, rank) => ({ id, aspect, rank });
const inside = (L, reg) => L.slots.every((s) => s.l >= reg.l - 0.5 && s.t >= reg.t - 0.5 && s.l + s.w <= reg.r + 0.5 && s.t + s.h <= reg.b + 0.5);
const noOverlap = (L) => L.slots.every((a, i) => L.slots.every((b, j) => i >= j || a.l + a.w <= b.l || b.l + b.w <= a.l || a.t + a.h <= b.t || b.t + b.h <= a.t));
const slotOf = (L, id) => L.slots.find((s) => s.ids.includes(id));

{ const L = D.fitLayout([it('chart', 'landscape', 0), it('news', 'portrait', 1), it('table', 'square', 2), it('map', 'square', 3)], REG, { near: 'left' });
  const c = slotOf(L, 'chart'), n = slotOf(L, 'news'), a = slotOf(L, 'table'), b = slotOf(L, 'map');
  t('the owner\'s markets desk: a list is a full-height column, the chart a wide band, the tables side by side', n.h > 1100 && n.w <= 560 && c.w > c.h * 1.4 && c.w >= 600 && a.t === b.t && a !== b, L);
  t('...every item in a slot of its own, inside the desk, nothing overlapping', L.pills.length === 0 && L.slots.every((s) => s.ids.length === 1) && inside(L, REG) && noOverlap(L)); }

{ const narrow = { l: 8, t: 8, r: 900, b: 900 };
  const L = D.fitLayout([it('a', 'portrait', 0), it('b', 'portrait', 1), it('c', 'portrait', 2), it('d', 'portrait', 3)], narrow, {});
  const cols = L.slots.filter((s) => s.aspect === 'portrait');
  t('out of room, lists composite as lists (no truncated list tiles): 4 lists on a 900 px desk are 2 columns, 2 tabs each', cols.length === 2 && cols.every((s) => s.ids.length === 2) && L.pills.length === 0, L); }

{ const small = { l: 0, t: 0, r: 900, b: 700 };
  const L = D.fitLayout([it('ohlcv', 'landscape', 0), it('trend', 'landscape', 1), it('trend2', 'landscape', 2), it('names', 'square', 3)], small, {});
  const bands = L.slots.filter((s) => s.aspect === 'landscape');
  t('time series with no room composite in a wide band (the most relevant in front), never a squeezed band or a pill', bands.length >= 1 && bands.length <= 2 && bands.every((s) => s.w >= s.h * 1.6) && bands.some((s) => s.ids.length >= 2) && slotOf(L, 'ohlcv').ids[0] === 'ohlcv' && L.pills.length === 0, L); }

{ const L = D.fitLayout(['a', 'b', 'c', 'd', 'e', 'f'].map((x, i) => it(x, 'tile', i)), REG, {});
  t('tiles tile as small elements, each its own', L.slots.length === 6 && L.slots.every((s) => s.aspect === 'tile' && s.ids.length === 1 && s.w <= 300 && s.h <= 300), L); }

{ const items = [it('chart', 'landscape', 0), it('news', 'portrait', 1), it('table', 'square', 2)];
  const L1 = D.fitLayout(items, REG, { near: 'left' }); const cur = {};
  L1.slots.forEach((s) => s.ids.forEach((id) => { cur[id] = { x: s.l + s.w / 2, y: s.t + s.h / 2, w: s.w, h: s.h }; }));
  const L2 = D.fitLayout(items.concat([it('gauge', 'tile', 3)]), REG, { near: 'left', current: cur });
  const n1 = slotOf(L1, 'news'), n2 = slotOf(L2, 'news');
  t('a tile arriving does not move the list column', n1.l === n2.l && n1.w === n2.w && n1.h === n2.h, { n1, n2 }); }

{ let worst = 0, fails = 0;
  for (let k = 0; k < 300; k++) { const A = ['portrait', 'landscape', 'square', 'tile']; const n = 1 + (k % 11);
    const reg = { l: 0, t: 0, r: 600 + (k * 37) % 1900, b: 500 + (k * 53) % 900 };
    const L = D.fitLayout(Array.from({ length: n }, (_, i) => it('i' + i, A[(k + i * 3) % 4], i)), reg, {});
    const placed = new Set(); L.slots.forEach((s) => s.ids.forEach((id) => placed.add(id))); L.pills.forEach((id) => placed.add(id));
    if (!inside(L, reg) || !noOverlap(L) || placed.size !== n) fails++; worst = Math.max(worst, L.pills.length); }
  t('300 random desks: every item placed once (a slot, a tab or a pill), inside the desk, no overlaps', fails === 0, { fails }); }

{ const items = ['l1', 'l2', 'l3', 'l4', 'l5', 'l6'].map((x, i) => it(x, 'portrait', i)).concat([it('c', 'landscape', 6), it('s1', 'square', 7), it('s2', 'square', 8)]);
  const L = D.fitLayout(items, REG, {}); const mixed = L.slots.filter((s) => s.ids.length > 1 && new Set(s.ids.map((id) => items.find((x) => x.id === id).aspect)).size > 1);
  t('a composite is always of one aspect (many lists: list columns with tabs, never lists as tabs of a table)', mixed.length === 0 && L.slots.some((s) => s.aspect === 'portrait'), L); }
{ const L = D.fitLayout(Array.from({ length: 11 }, (_, i) => it('l' + i, 'portrait', i)).concat([it('c', 'landscape', 11), it('s', 'square', 12)]), { l: 0, t: 0, r: 1100, b: 1000 }, {});
  t('a card holds at most 8 tabs; past that a pill of its own', L.slots.every((s) => s.ids.length <= 8) && L.pills.length >= 1, L); }
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);
