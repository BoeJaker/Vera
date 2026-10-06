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

console.log(n + ' passed');
