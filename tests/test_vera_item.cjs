// The item contract's browser half (vera/ui/vera_item.js; DEEP-29 11A.4, owner decision D1 2026-10-16): the record
// gives the server's answers (the shared cases), bodies declare and the container draws, and one z table.
//   node tests/test_vera_item.cjs
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const I = require(path.join(__dirname, '..', 'vera', 'ui', 'vera_item.js'));
const CASES = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'item_contract_cases.json'), 'utf8'));
let n = 0; const t = (name, fn) => { fn(); n++; console.log('ok -', name); };
const dc = (o) => JSON.parse(JSON.stringify(o));

t('normalise gives the server\'s answers on every shared case', () => {
  CASES.normalise.forEach((c) => {
    const got = I.normalise(dc(c.block));
    if (c.want) assert.deepStrictEqual(got, c.want, c.name);
    Object.entries(c.want_fields || {}).forEach(([k, v]) => assert.deepStrictEqual(got[k], v, c.name + ': ' + k));
  });
});
t('applySet gives the server\'s answers on every shared case, and a refused call writes nothing', () => {
  CASES.apply_set.forEach((c) => {
    const b = dc(c.block), before = dc(b);
    const r = I.applySet(b, c.fields);
    assert.deepStrictEqual(r, c.want, c.name);
    if (r.problems.length) assert.deepStrictEqual(b, before, c.name);
  });
});
t('a body declares its views and actions; the container draws the standard row in order', () => {
  I.registerBody('code', { views: ['code', 'structure', 'both'], fit: 'content', minSize: 's', actions: [{ id: 'copy' }, { id: 'edit', face: 'full-if-long' }, { id: 'run' }] });
  assert.deepStrictEqual(I.controls('code'), ['view', 'lock', 'pill', 'max', 'more', 'drop']);
  assert.deepStrictEqual(I.controls('code', { narrow: true }), ['lock', 'pill', 'more', 'drop']);
  assert.deepStrictEqual(I.controls('note'), ['lock', 'pill', 'max', 'more', 'drop'], 'one view: no view toggle');
  const m = I.menu('code', {});
  assert(m.indexOf('refresh') < 0, 'no source: no run again');
  assert.deepStrictEqual(m.slice(-2), ['copy', 'run'], 'the body\'s own actions follow the standard ones');
  assert.strictEqual(m.filter((x) => x === 'edit' || x === 'copy').length, 2, 'without repeats');
  assert(I.menu('result', { source: { cap: 'x' } }).indexOf('refresh') >= 0);
});
t('an unregistered kind still gets the whole container', () => {
  const b = I.body('mystery');
  assert.deepStrictEqual([b.views, b.fit, b.minSize, b.actions], [[], 'content', 's', []]);
});
t('the z table: ranges stay in their band, and vera-item.css says the same numbers', () => {
  assert.strictEqual(I.z('tile', 0), 10); assert.strictEqual(I.z('tile', 500), 110);
  assert.strictEqual(I.z('window', 3), 133); assert.strictEqual(I.z('window', 99), 149);
  assert.strictEqual(I.z('pop'), 230); assert.strictEqual(I.z('sheet'), 300);
  const css = fs.readFileSync(path.join(__dirname, '..', 'vera', 'ui', 'vera-item.css'), 'utf8');
  const v = (name) => Number((css.match(new RegExp('--z-' + name + ':\\s*(\\d+)')) || [])[1]);
  const want = { back: I.Z.back, 'layer-behind': I.Z.layerBehind, tile: I.Z.tile[0], 'tile-top': I.Z.tile[1], pill: I.Z.pill,
    'tile-focus': I.Z.tileFocus, window: I.Z.window[0], 'window-top': I.Z.window[1], full: I.Z.full, 'layer-front': I.Z.layerFront,
    flash: I.Z.flash, drag: I.Z.drag, pop: I.Z.pop, menu: I.Z.menu, chat: I.Z.chat, sheet: I.Z.sheet, toast: I.Z.toast };
  Object.entries(want).forEach(([k, x]) => assert.strictEqual(v(k), x, '--z-' + k));
  const order = ['back', 'layerBehind', 'pill', 'tileFocus', 'full', 'layerFront', 'flash', 'drag', 'pop', 'menu', 'chat', 'sheet', 'toast'].map((k) => I.Z[k]);
  assert.deepStrictEqual(order, order.slice().sort((a, b) => a - b), 'the bands are in order');
});
console.log(n + ' passed');
