// The desk's layout rules (owner approved 2026-10-06, design board 9): sizes by distance to the chat, arrangements,
// the chat's path, stacking, fullscreen, the panel's layers and its widgets making room.
'use strict';
const assert = require('assert');
const path = require('path');
const D = require(path.join(__dirname, '..', 'vera', 'ui', 'desk_layout_core.js'));

let n = 0;
function t(name, fn) { fn(); n++; console.log('ok -', name); }

const FLOAT = D.chatRect('float', null);
const LEFT = D.chatRect('left', null);

t('the chat floats in the middle, docks at an edge, and snaps when let go near one', () => {
  assert.deepStrictEqual(FLOAT, { l: 580, t: 215, r: 1020, b: 685 });
  assert.strictEqual(LEFT.l, 16); assert.strictEqual(D.chatRect('right', null).r, 1584);
  assert.strictEqual(D.snapEdge({ l: 10, t: 200, r: 450, b: 670 }), 'left');
  assert.strictEqual(D.snapEdge({ l: 1150, t: 200, r: 1590, b: 670 }), 'right');
  assert.strictEqual(D.snapEdge({ l: 500, t: 420, r: 940, b: 890 }), 'bottom');
  assert.strictEqual(D.snapEdge(FLOAT), null);
});

t('items are largest beside the chat, cards further out, chips only near the far edge', () => {
  assert.strictEqual(D.sizeAt(FLOAT, 1150, 450).face, 'max');           // just beside it
  assert.strictEqual(D.sizeAt(FLOAT, 1380, 450).face, 'card');          // ~72% of the way to the edge
  assert.strictEqual(D.sizeAt(FLOAT, 1500, 450).face, 'chip');          // near the edge
  assert.strictEqual(D.sizeAt(FLOAT, 800, 866).face, 'chip');           // the bottom edge is far too (less room below)
});

t('a docked chat has a whole side free: sizes scale to it, not to a fixed distance', () => {
  assert.strictEqual(D.sizeAt(LEFT, 700, 450).face, 'max');             // ~240 px out of ~1060
  assert.strictEqual(D.sizeAt(LEFT, 1200, 450).face, 'card');           // ~70% of the free side
  assert.strictEqual(D.sizeAt(LEFT, 1500, 450).face, 'chip');
});

t('the person’s size holds until ~93% out; a pinned size holds everywhere', () => {
  const size = { w: 400, h: 260 };
  assert.strictEqual(D.sizeAt(FLOAT, 1460, 450, { size }).why, 'your size');   // 88% out: still yours (normal: a chip)
  assert.strictEqual(D.sizeAt(FLOAT, 1460, 450).face, 'chip');
  assert.strictEqual(D.sizeAt(FLOAT, 1505, 450, { size }).face, 'chip');        // 97% out
  const pinned = D.sizeAt(FLOAT, 1505, 450, { size, pinned: true });
  assert.strictEqual(pinned.why, 'pinned'); assert.strictEqual(pinned.w, 400);
});

t('arrangements place every item, and a docked orbit fills the free side in bands', () => {
  const items = 'abcdefghijklm'.split('').map((id, i) => ({ id, kind: ['charts', 'sources', 'notes'][i % 3], age: i % 4 }));
  ['orbit', 'kind', 'timeline', 'stack'].forEach((mode) => {
    const pos = D.arrange(mode, FLOAT, items);
    assert.strictEqual(Object.keys(pos).length, items.length, mode + ' places every item');
  });
  // what does not fit in the bands sits at the border, and there it really is a chip
  const narrow = { W: 702, H: 639, m: 8, top: 4, edgeX: 24, edgeTop: 24, edgeBottom: 22 }, CR = { l: -440, t: 0, r: 0, b: 639 };
  const many = 'abcdefghijklmnopqrst'.split('').map((id) => ({ id, kind: 'x' }));
  const np = D.arrange('orbit', CR, many, narrow);
  const faces = many.slice(11).map((it) => D.sizeAt(CR, np[it.id][0], np[it.id][1], {}, narrow).face);
  assert.ok(faces.every((f) => f === 'chip'), 'border items are chips: ' + faces.join(','));
  const bands = D.arrange('orbit', LEFT, items);
  assert.ok(bands.a[0] > LEFT.r && bands.a[0] < 700, 'the first band sits just beside a left-docked chat');
  assert.strictEqual(D.sizeAt(LEFT, bands.a[0], bands.a[1]).face, 'max');
});

t('an item in the chat’s path moves to the nearest side and takes the size it has there', () => {
  const r = D.pushOut(FLOAT, 1000, 450, (x, y) => D.sizeAt(FLOAT, x, y));
  assert.ok(r.pushed);
  assert.ok(r.x - r.size.w / 2 >= FLOAT.r, 'clear of the chat');
  assert.strictEqual(D.pushOut(FLOAT, 1300, 450, (x, y) => D.sizeAt(FLOAT, x, y)).pushed, false);
});

t('overlapping items are separated - the first keeps its place, none lands in the chat or off the desk', () => {
  const R = { l: 600, t: 200, r: 1000, b: 700 };
  const items = [{ id: 'a', x: 300, y: 300, w: 260, h: 160 }, { id: 'b', x: 320, y: 320, w: 260, h: 160 }, { id: 'c', x: 340, y: 340, w: 200, h: 100 }];
  D.separate(items, R, null, { W: 1600, H: 900 });
  assert.deepStrictEqual([items[0].x, items[0].y], [300, 300]);
  const ov = (p, q) => p.x - p.w / 2 < q.x + q.w / 2 && q.x - q.w / 2 < p.x + p.w / 2 && p.y - p.h / 2 < q.y + q.h / 2 && q.y - q.h / 2 < p.y + p.h / 2;
  assert.ok(!ov(items[0], items[1]) && !ov(items[0], items[2]) && !ov(items[1], items[2]));
  items.forEach((it) => assert.ok(!(it.x + it.w / 2 > R.l && it.x - it.w / 2 < R.r && it.y + it.h / 2 > R.t && it.y - it.h / 2 < R.b)));
});

t('closer to the chat stacks higher; dragged and fullscreen on top', () => {
  assert.ok(D.stackOrder(0.1) > D.stackOrder(0.8));
  assert.strictEqual(D.stackOrder(0.5, { dragging: true }), 200);
  assert.strictEqual(D.stackOrder(0.5, { full: true }), 150);
});

t('fullscreen fills the largest part of the desk the chat does not cover', () => {
  const g = D.fullRegion(LEFT);
  assert.ok(g.l >= LEFT.r && g.r === 1584);
});

t('layers: graph, panel, items, chat - and the panel can come in front of the items, never the chat', () => {
  assert.deepStrictEqual(D.layerOrder(), ['graph', 'panel', 'items', 'chat']);
  assert.deepStrictEqual(D.layerOrder({ panelFront: true }), ['graph', 'items', 'panel', 'chat']);
  assert.deepStrictEqual(D.layerOrder({ panel: false }), ['graph', 'items', 'chat']);
});

t('panel widgets move to the free space nearest home, compact before covered, stay put when told', () => {
  const grid = { ox: 0, oy: 0, cols: 4, rows: 3, cw: 100, ch: 100, gap: 0 };
  const W = [{ id: 'watch', w: 2, h: 2 }, { id: 'news', w: 1, h: 1 }];
  const clear = D.makeRoom(grid, [], W);
  assert.deepStrictEqual(clear.map((x) => [x.id, x.c, x.r, x.moved]), [['watch', 0, 0, false], ['news', 2, 0, false]]);
  const over = D.makeRoom(grid, [{ l: 0, t: 0, r: 150, b: 150 }], W);   // an item over the watchlist's home
  const watch = over.find((x) => x.id === 'watch');
  assert.ok(watch.moved && !watch.covered);
  const tight = D.makeRoom({ ox: 0, oy: 0, cols: 2, rows: 2, cw: 100, ch: 100, gap: 0 }, [{ l: 0, t: 0, r: 150, b: 90 }], [{ id: 'watch', w: 2, h: 2 }]);
  assert.ok(tight[0].compact && !tight[0].covered);
  const kept = D.makeRoom(grid, [{ l: 0, t: 0, r: 150, b: 150 }], W, { makeRoom: false });
  assert.ok(kept[0].covered && !kept[0].moved);
});

t('a narrow desk never puts two border chips on one slot (more chips than border slots)', () => {
  /* the canvas column beside a docked chat at 1366 px: the chat is off the desk's left edge */
  const d = { W: 560, H: 640, top: 40, edgeX: 80, edgeTop: 60, edgeBottom: 34 };
  const R = { l: -460, t: 40, r: -16, b: 624 };
  const items = Array.from({ length: 20 }, (_, i) => ({ id: 'n' + i, kind: 'note', age: i }));
  const pos = D.arrange('orbit', R, items, d);
  const small = Object.keys(pos).map((id) => ({ p: pos[id], s: D.sizeAt(R, pos[id][0], pos[id][1], { icon: pos[id][2] === 'icon' }, d) }))
    .filter((x) => x.s.face === 'chip' || x.s.face === 'icon');
  let clash = 0;
  for (let i = 0; i < small.length; i++) for (let j = i + 1; j < small.length; j++) {
    const a = small[i], b = small[j];
    if (Math.abs(a.p[0] - b.p[0]) < (a.s.w + b.s.w) / 2 && Math.abs(a.p[1] - b.p[1]) < (a.s.h + b.s.h) / 2) clash++;
  }
  assert.ok(small.length >= 10, 'small: ' + small.length);
  assert.strictEqual(clash, 0);
  /* a tiny desk: the border cannot hold them all, the rest are icons - and still none on another */
  const tiny = { W: 423, H: 407, top: 4, edgeX: 24, edgeTop: 24, edgeBottom: 22 };
  const tp = D.arrange('orbit', { l: -440, t: 0, r: 0, b: 407 }, items, tiny);
  const ic = Object.keys(tp).filter((id) => tp[id][2] === 'icon');
  assert.ok(ic.length > 0, 'icons on a tiny desk');
  const tr = { l: -440, t: 0, r: 0, b: 407 };
  const ts = Object.keys(tp).map((id) => ({ p: tp[id], s: D.sizeAt(tr, tp[id][0], tp[id][1], { icon: tp[id][2] === 'icon' }, tiny) })).filter((x) => x.s.face !== 'max' && x.s.face !== 'card');
  let tc = 0;
  for (let i = 0; i < ts.length; i++) for (let j = i + 1; j < ts.length; j++) {
    const a = ts[i], b = ts[j];
    if (Math.abs(a.p[0] - b.p[0]) < (a.s.w + b.s.w) / 2 && Math.abs(a.p[1] - b.p[1]) < (a.s.h + b.s.h) / 2) tc++;
  }
  assert.strictEqual(tc, 0);
});

t('the desk exploded: its layers are the lattice planes, every item keeps its key, the graph is at the back', () => {
  const items = [
    { key: 'a', label: 'A', face: 'max', type: 'widget', mid: 'm2' },
    { key: 'b', label: 'B', face: 'card', type: 'records', mid: 'm1' },
    { key: 'c', label: 'C', face: 'chip', type: 'note' },
    { key: 'panel:sys', label: 'Monitor', face: 'layer-behind', layer: 'behind', type: 'panel' },
  ];
  const L = D.latticeOf({ items, turns: [{ mid: 'm1', label: 'hi' }, { mid: 'm2', label: 'more' }], graph: [{ id: 'g1', label: 'rec' }] });
  assert.deepStrictEqual(L.planes.map((p) => p.id), ['chat', 'items', 'panel', 'graph']);   // front first, the graph at the back
  const byId = {}; L.nodes.forEach((x) => { byId[x.id] = x; });
  items.forEach((it) => assert.ok(byId[it.key] && byId[it.key].desk.key === it.key, 'item ' + it.key + ' keeps its key'));
  assert.strictEqual(byId['panel:sys'].plane, 'panel');
  assert.strictEqual(byId.a.area, 'beside the chat'); assert.strictEqual(byId.b.area, 'at a glance'); assert.strictEqual(byId.c.area, 'at the border');
  assert.strictEqual(byId['ctx:g1'].plane, 'graph');
  assert.ok(L.links.some((l) => l.a === 'a' && l.b === 'turn:m2'), 'an item is piped to its turn');
  assert.ok(L.links.some((l) => l.a === 'ctx:g1' && l.b === 'turn:m2'), 'the context is piped to the turn in focus (the newest)');
  // a full cell spills into a neighbour area: every item is drawn, nine at most to a cell
  const many = Array.from({ length: 12 }, (_, i) => ({ key: 'k' + i, face: 'chip' }));
  const M = D.latticeOf({ items: many });
  assert.strictEqual(M.nodes.filter((x) => x.plane === 'items').length, 12);
  assert.ok(M.areas.indexOf('at the border · 2') === M.areas.indexOf('at the border') + 1, 'the spill area sits beside its own');
  assert.strictEqual(M.nodes.filter((x) => x.area === 'at the border').length, 9);
  assert.ok(!/more/.test(M.planes.find((p) => p.id === 'items').name));
  const crowded = D.latticeOf({ items: Array.from({ length: 100 }, (_, i) => ({ key: 'crowded-' + i, face: 'chip' })) });
  assert.strictEqual(crowded.nodes.filter((x) => x.plane === 'items').length, 100, 'large desks never silently drop items');
});

t('every arrangement lays its cards in bands: none overlap, docked or floating, before any separation', () => {
  const d = { W: 900, H: 700, m: 8, top: 4, edgeX: 24, edgeTop: 24, edgeBottom: 22 };
  const items = Array.from({ length: 20 }, (_, i) => ({ id: 'n' + i, kind: ['widget', 'records', 'note', 'table'][i % 4], age: i % 5 }));
  [['left', { l: -440, t: 0, r: 0, b: 700 }], ['float', { l: 260, t: 60, r: 640, b: 560 }]].forEach(([side, R]) => {
    ['orbit', 'kind', 'timeline', 'stack'].forEach((mode) => {
      const pos = D.arrange(mode, R, items, d);
      assert.strictEqual(Object.keys(pos).length, items.length, mode + ' ' + side + ' places every item');
      const big = Object.keys(pos).map((id) => ({ id, p: pos[id], s: D.sizeAt(R, pos[id][0], pos[id][1], { icon: pos[id][2] === 'icon' }, d) }))
        .filter((x) => x.s.face === 'max' || x.s.face === 'card');
      let ov = 0;
      for (let i = 0; i < big.length; i++) for (let j = i + 1; j < big.length; j++) {
        const a = big[i], b = big[j];
        if (Math.abs(a.p[0] - b.p[0]) < (a.s.w + b.s.w) / 2 - 2 && Math.abs(a.p[1] - b.p[1]) < (a.s.h + b.s.h) / 2 - 2) ov++;
      }
      assert.strictEqual(ov, 0, mode + ' ' + side + ': ' + ov + ' overlapping cards');
      big.forEach((x) => assert.ok(x.p[0] + x.s.w / 2 <= R.l + 2 || x.p[0] - x.s.w / 2 >= R.r - 2 || x.p[1] + x.s.h / 2 <= R.t + 2 || x.p[1] - x.s.h / 2 >= R.b - 2, mode + ' ' + side + ': a card under the chat'));
    });
  });
});

t('a widget that says where it sits keeps that home: only the covered one moves', () => {
  const grid = { ox: 0, oy: 0, cols: 6, rows: 6, cw: 100, ch: 100, gap: 0 };
  /* homes a dense flow would not give: a at (3,0), b at (0,2) */
  const W = [{ id: 'a', w: 2, h: 1, c: 3, r: 0 }, { id: 'b', w: 1, h: 1, c: 0, r: 2 }, { id: 'c', w: 1, h: 1, c: 5, r: 5 }];
  const res = D.makeRoom(grid, [{ l: 0, t: 200, r: 100, b: 300 }], W);
  const by = {}; res.forEach((x) => { by[x.id] = x; });
  assert.deepStrictEqual([by.a.c, by.a.r, by.a.moved], [3, 0, false]);
  assert.deepStrictEqual([by.c.c, by.c.r, by.c.moved], [5, 5, false]);
  assert.ok(by.b.moved && !by.b.covered, 'the covered one moves out');
  /* a column of tiles, the top one covered: only it moves - the rest never shift down to make way */
  const col = [0, 1, 2, 3].map((i) => ({ id: 't' + i, w: 2, h: 1, c: 0, r: i }));
  const cr = D.makeRoom({ ox: 0, oy: 0, cols: 6, rows: 8, cw: 100, ch: 100, gap: 0 }, [{ l: 0, t: 0, r: 200, b: 100 }], col);
  assert.deepStrictEqual(cr.filter((x) => x.moved).map((x) => x.id), ['t0']);
  assert.ok(!cr.find((x) => x.id === 't0').covered);
});

t('a chip that only fits at its own size is moved clear at that size, not left inside a card the person grew', () => {
  /* a card grown out to the right border (fixed: the person's rectangle) and two border chips it now covers; the size a
     spot would give a chip near the chat is a full card, for which no room is left anywhere */
  const R = { l: -440, t: 0, r: 0, b: 700 }, d = { W: 900, H: 700 };
  const big = { id: 'grown', x: 380, y: 300, w: 760, h: 520, fixed: true };
  const chips = [{ id: 'c1', x: 820, y: 120, w: 150, h: 34 }, { id: 'c2', x: 820, y: 170, w: 150, h: 34 }];
  const res = D.separate([big].concat(chips), R, () => ({ w: 700, h: 500 }), d);
  const hit = (a, b) => Math.abs(a.x - b.x) < (a.w + b.w) / 2 && Math.abs(a.y - b.y) < (a.h + b.h) / 2;
  chips.forEach((c) => { const o = res.find((x) => x.id === c.id); assert.ok(!hit(o, big), c.id + ' still inside the grown card');
    assert.deepStrictEqual([o.w, o.h, !!o.keepSize], [150, 34, true], c.id + ' kept its size'); });
  assert.ok(!hit(res.find((x) => x.id === 'c1'), res.find((x) => x.id === 'c2')), 'the chips clear of each other');
  assert.deepStrictEqual([big.x, big.y, big.w, big.h], [380, 300, 760, 520], 'the grown card did not move');
});

t('an item out of the topic is a border chip even when the bands beside the chat have room', () => {
  const R = D.chatRect('left', null), d = { W: 1600, H: 900 };
  const items = [{ id: 'in1' }, { id: 'out1', border: true }, { id: 'in2' }, { id: 'out2', border: true }];
  ['orbit', 'kind', 'timeline', 'stack'].forEach((mode) => {
    const pos = D.arrange(mode, R, items.map((x) => Object.assign({ kind: 'records', age: 0 }, x)), d);
    ['out1', 'out2'].forEach((k) => { assert.ok(pos[k], mode + ': ' + k + ' placed'); assert.strictEqual(D.sizeAt(R, pos[k][0], pos[k][1], {}, d).face !== 'max' && D.sizeAt(R, pos[k][0], pos[k][1], {}, d).face !== 'card', true, mode + ': ' + k + ' is a border face'); });
    ['in1', 'in2'].forEach((k) => assert.ok(pos[k] && ['max', 'card'].indexOf(D.sizeAt(R, pos[k][0], pos[k][1], {}, d).face) >= 0, mode + ': ' + k + ' beside the chat'));
  });
});

t('cards grow into the free desk round them, never into each other, the chat or past the edge', () => {
  const R = { l: -440, t: 0, r: 0, b: 900 }, d = { W: 1200, H: 900 };
  const box = (it) => ({ l: it.x - it.w / 2, t: it.y - it.h / 2, r: it.x + it.w / 2, b: it.y + it.h / 2 });
  const a = { id: 'a', x: 200, y: 150, w: 300, h: 196 }, b = { id: 'b', x: 560, y: 150, w: 300, h: 196 };
  const chip = { id: 'c', x: 1100, y: 40, w: 150, h: 34, chip: true }, fixed = { id: 'f', x: 300, y: 700, w: 400, h: 200, fixed: true };
  const items = [a, b, chip, fixed].map((x) => Object.assign({}, x));
  D.grow(items, R, d, { gap: 14 });
  const [A, B, C, F] = items;
  assert.ok(A.grown && B.grown, 'both cards grew');
  assert.ok(A.w * A.h > 300 * 196 * 1.5 && B.w * B.h > 300 * 196 * 1.5, 'by a good deal: ' + [A.w, A.h, B.w, B.h]);
  assert.deepStrictEqual([C.x, C.y, C.w, C.h], [1100, 40, 150, 34], 'a chip stays as it is');
  assert.deepStrictEqual([F.x, F.y, F.w, F.h], [300, 700, 400, 200], 'a fixed card stays as it is');
  const hit = (p, q, g) => p.l < q.r + g && q.l < p.r + g && p.t < q.b + g && q.t < p.b + g;
  for (let i = 0; i < items.length; i++) for (let j = i + 1; j < items.length; j++) assert.ok(!hit(box(items[i]), box(items[j]), 13), items[i].id + ' and ' + items[j].id + ' clear');
  items.forEach((it) => { const q = box(it); assert.ok(q.l >= 0 && q.t >= 0 && q.r <= d.W && q.b <= d.H, it.id + ' within the desk'); });
  [A, B].forEach((it) => assert.ok(it.w / it.h <= 2.2 + 0.05 && it.h / it.w <= 2.2 + 0.05, it.id + ' a page, not a ribbon: ' + it.w + 'x' + it.h));
});

t('cards rise up the desk into the room above them; fixed ones stay; none meet', () => {
  const R = { l: -440, t: 0, r: 0, b: 900 }, d = { W: 900, H: 900 };
  const items = [{ id: 'a', x: 170, y: 400, w: 300, h: 200 }, { id: 'b', x: 170, y: 640, w: 300, h: 200 },
                 { id: 'c', x: 482, y: 500, w: 300, h: 200 }, { id: 'f', x: 482, y: 160, w: 300, h: 100, fixed: true }];
  D.settle(items, R, d, { gap: 12 });
  const [A, B, C, F] = items;
  assert.strictEqual(A.y - A.h / 2, 4, 'the first card is at the top: ' + A.y);
  assert.strictEqual(B.y - B.h / 2 >= A.y + A.h / 2 + 12 && B.y - B.h / 2 <= A.y + A.h / 2 + 16, true, 'the next sits under it, a gap apart: ' + B.y);
  assert.deepStrictEqual([F.x, F.y], [482, 160], 'a fixed card stays');
  assert.ok(C.y - C.h / 2 >= F.y + F.h / 2 + 12 && C.y - C.h / 2 <= F.y + F.h / 2 + 16, 'the card under a fixed one rises to it: ' + C.y);
  assert.ok(A.settled && B.settled && C.settled && !F.settled);
});

t('a card with the desk to itself grows to a page of it, not past the aspect', () => {
  const it = [{ id: 'x', x: 600, y: 450, w: 300, h: 196 }];
  D.grow(it, { l: -440, t: 0, r: 0, b: 900 }, { W: 1200, H: 900 }, {});
  assert.ok(it[0].w >= 1100 || it[0].h >= 800, 'it reaches an edge: ' + it[0].w + 'x' + it[0].h);
  assert.ok(it[0].w / it[0].h <= 2.25, 'within 1:2.2');
});

console.log(n + ' passed');
