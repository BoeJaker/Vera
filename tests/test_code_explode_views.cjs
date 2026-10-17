// WSU-58 S4: the code card's explode views (the code alone · the structure alone · both) are ORDINARY views - offered by
// viewsOf for a code item with its structure open, picked through the same view control and menu as every other kind
// (setview), written to the item record through _itemSet, and back after a reload (_itemHydrate). The special xcode
// head branch, the xv rows in _viewMenu and the xview action are gone.
//   node tests/test_code_explode_views.cjs
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const SRC = fs.readFileSync(FILE, 'utf8');
let n = 0; const t = (name, fn) => { fn(); n++; console.log('ok -', name); };

function load() {
  let Cls = null; const store = {};
  const ctx = {
    console: { log() {}, error() {}, warn() {} },
    setTimeout: () => 0, clearTimeout: () => {},
    localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } },
    customElements: { get: () => undefined, define: (n2, c) => { Cls = c; } },
    fetch: async () => ({ json: async () => ({}) }),
    Promise, JSON, Object, Array, String, Number, Math, Date, Set, Map, RegExp, Error, URL, isFinite, parseFloat, parseInt,
  };
  ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
  vm.createContext(ctx); vm.runInContext(SRC, ctx, { filename: 'canvas_element.js' });
  assert.ok(Cls, 'the page defines <vera-canvas>');
  const api = ctx.VeraCanvas; assert.ok(api && typeof api.viewsOf === 'function', 'viewsOf is on the api');
  const mk = () => { const el = Object.create(Cls.prototype); Object.defineProperty(el, 'canvasId', { value: 'cv1' });
    el.getAttribute = () => null; el._bidOf = () => false; return el; };
  return { Cls, api, mk, store };
}
const CODE = { type: 'code', key: 'code:a', content: { lang: 'python', code: Array.from({ length: 20 }, (_, i) => 'x' + i + ' = f(' + i + ')').join('\n') } };
const SHORT = { type: 'code', key: 'code:s', content: { lang: 'python', code: 'x = 1\ny = 2' } };
const XP = { type: 'explode', key: 'explode:code:a', content: { binds: 'code:a' } };

t('viewsOf: a code item with its structure open offers code · structure · both, both by default', () => {
  const S = load(); const el = S.mk(); el._xploded = new Set(['code:a']);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(S.api.viewsOf(CODE, el))), { views: ['code', 'structure', 'both'], cur: 'both' });
});

t('viewsOf: without its structure open (or too short to explode) a code item has one way to be seen', () => {
  const S = load(); const el = S.mk(); el._xploded = new Set();
  assert.deepStrictEqual(S.api.viewsOf(CODE, el).views.length, 0);
  el._xploded = new Set(['code:s']); assert.deepStrictEqual(S.api.viewsOf(SHORT, el).views.length, 0);
  el.getAttribute = (a) => (a === 'explode-offer' ? 'never' : null); el._xploded = new Set(['code:a']);
  assert.deepStrictEqual(S.api.viewsOf(CODE, el).views.length, 0, 'explode-offer=never: no views');
});

t('viewsOf: before a render has worked out the set, the document says whether the structure is open', () => {
  const S = load(); const el = S.mk(); el._doc = { blocks: [CODE, XP] };
  assert.deepStrictEqual(S.api.viewsOf(CODE, el).views.length, 3);
});

t('a view the code item does not offer falls back to both', () => {
  const S = load(); const el = S.mk(); el._xploded = new Set(['code:a']); el._views = { 'code:a': 'tree' };
  assert.strictEqual(S.api.viewsOf(CODE, el).cur, 'both');
});

t('the pick goes through setview: written through _itemSet as the item\'s view, and shown', () => {
  const S = load(); const el = S.mk(); el._xploded = new Set(['code:a']); const sets = [];
  el._itemSet = (k, f) => sets.push([k, JSON.parse(JSON.stringify(f))]);
  el._blockOf = (k) => (k === 'code:a' ? CODE : null);
  Object.defineProperty(el, 'shadowRoot', { value: { querySelectorAll: () => [] } });
  Object.defineProperty(el, 'dataset', { value: {} });
  const btn = { dataset: { act: 'setview', view: 'structure', layerKey: 'code:a' }, closest: () => null };
  el._act(btn, {});
  assert.deepStrictEqual(sets, [['code:a', { view: 'structure' }]]);
  assert.strictEqual(S.api.viewsOf(CODE, el).cur, 'structure');
  assert.ok(/"code:a":"structure"/.test(S.store['vera:canvas:views:cv1'] || ''), 'kept in this browser too');
});

t('after a reload the record\'s view is restored (_itemHydrate)', () => {
  const S = load(); const el = S.mk(); el._xploded = new Set(['code:a']);
  el._lockGet = () => ({}); el._deskPosGet = () => ({}); el._pillReal = () => ({}); el._folds = () => ({}); el._foldsFor = 'cv1';
  el._itemHydrate({ blocks: [Object.assign({}, CODE, { view: 'code' }), XP] });
  assert.strictEqual(S.api.viewsOf(CODE, el).cur, 'code');
});

t('the head draws the pick from viewsOf (xv-<view>) through the ordinary view control', () => {
  assert.ok(SRC.includes("const vw = viewsOf(b, this);\n        const xv = b.type === 'code' && vw.views[0] === 'code' ? vw.cur : '';"), 'xv comes from viewsOf');
  assert.ok(SRC.includes("(xv ? ' xv-' + xv : '')"), 'the panes still answer to xv-<view>');
  assert.ok(SRC.includes('this._xploded = xplodedKeys;'), 'render hands viewsOf the open structures');
  ['code', 'structure', 'both'].forEach((v) => { assert.ok(new RegExp('\\b' + v + ": '").test(SRC.match(/const VIEW_LABEL = \{([^}]*)\}/)[1]), v + ' has a label'); });
});

t('the xcode branch, the xv rows in _viewMenu and the xview action are gone', () => {
  assert.ok(!/\bxcode\b/.test(SRC), 'no xcode');
  assert.ok(!SRC.includes('_xpView'), 'no _xpView');
  assert.ok(!SRC.includes("'xview'") && !SRC.includes('data-act="xview"'), 'no xview action');
  const i = SRC.indexOf('    _viewMenu(key, btn) {'); const seg = SRC.slice(i, SRC.indexOf('\n    toggleLock(', i));
  assert.ok(i > 0 && !/xv-\(code/.test(seg) && !/\bxv\b/.test(seg), '_viewMenu has one branch');
  assert.ok(/if \(xplodable\) hx\('data-act="explode"', xploded \? 'Source' : 'Explode'/.test(SRC), 'one Explode / Source button');
});

console.log(n + ' passed');
