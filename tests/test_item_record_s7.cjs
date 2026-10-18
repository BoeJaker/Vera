// WSU-58 S7: the rest of the item state lives in the item record. Maximised (face 'full' + back), the layer (place.layer),
// the group (taken out / joined / the front tab's time) and a kept window (face 'window' + its place + dock) are written
// through canvas.item.set and read back by _itemHydrate; this browser's localStorage families are caches. A second browser
// with EMPTY storage restores all four from the record.
//   node tests/test_item_record_s7.cjs
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const SRC = fs.readFileSync(FILE, 'utf8');
const I = require(path.join(__dirname, '..', 'vera', 'ui', 'vera_item.js'));
let n = 0; const t = async (name, fn) => { await fn(); n++; console.log('ok -', name); };
const dc = (o) => JSON.parse(JSON.stringify(o));
const CID = 'cv-s7';

/* the server: one session canvas, canvas.item.set answered as the record does (VeraItem.applySet = item_core.apply_set, held
   to the same answers by the shared cases) */
function server(blocks) {
  const S = { blocks: dc(blocks), calls: [] };
  S.fetch = async (url, opt) => {
    const body = JSON.parse(opt.body); const a = Object.assign({}, body.arguments); S.calls.push(dc(a));
    const key = a.key; delete a.id; delete a.key;
    const b = S.blocks.find((x) => x.key === key);
    const ans = !b ? { ok: false, error: 'unknown key: ' + key } : (() => { const r = I.applySet(b, a);
      return r.problems.length ? { ok: false, error: r.problems.join('; '), key } : { ok: true, key, changed: r.changed }; })();
    return { json: async () => ({ type: 'tool_result', tool_name: 'canvas.item.set', content: ans }) };
  };
  S.doc = () => ({ id: CID, revision: S.calls.length, blocks: dc(S.blocks) });
  return S;
}

/* one browser: the page's own element class on a stub DOM, its own localStorage, the shared server */
function browser(S) {
  let Cls = null; const store = {}; const timers = new Map(); let tid = 0; const warns = [];
  class Node0 {
    constructor() { this._attrs = {}; this.dataset = {}; }
    attachShadow() { this.shadowRoot = { getElementById: () => null, querySelectorAll: () => [], querySelector: () => null }; return this.shadowRoot; }
    getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; } hasAttribute(k) { return k in this._attrs; }
    setAttribute(k, v) { this._attrs[k] = String(v); } removeAttribute(k) { delete this._attrs[k]; } dispatchEvent() { return true; }
    addEventListener() {} toggleAttribute(k, on) { if (on) this._attrs[k] = ''; else delete this._attrs[k]; }
  }
  const ctx = { console: { log() {}, error() {}, warn: (...a) => warns.push(a.join(' ')) },
    setTimeout: (fn) => { const id = ++tid; timers.set(id, fn); return id; }, clearTimeout: (id) => timers.delete(id),
    requestAnimationFrame: () => 0, HTMLElement: Node0,
    localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; },
      get length() { return Object.keys(store).length; }, key: (i) => Object.keys(store)[i] },
    CustomEvent: class { constructor(n2, o) { this.type = n2; this.detail = o && o.detail; } }, getComputedStyle: () => ({ zIndex: 'auto' }),
    fetch: (...a) => S.fetch(...a), document: {}, navigator: {},
    customElements: { get: () => null, define: (name, c) => { if (name === 'vera-canvas') Cls = c; } } };
  ctx.window = ctx; ctx.globalThis = ctx;
  vm.createContext(ctx); vm.runInContext(SRC, ctx, { filename: FILE });
  assert(Cls, 'the element class');
  const el = new Cls(); el.setAttribute('canvas-id', CID); el.setAttribute('align', 'desk'); el.setAttribute('stage', '');
  el._doc = S.doc(); el._deskGeo = { W: 1200, H: 800 };
  /* the layout, its history and the paint (another session's code): not what this test is about */
  ['render', '_placeNow', '_holdStart', '_histPush', '_histBack', '_pillCard', '_chipPeek', '_revealSoon', 'refresh'].forEach((m) => { el[m] = () => {}; });
  const flush = async () => { for (let i = 0; i < 30 && timers.size; i++) { const fns = [...timers.values()]; timers.clear(); fns.forEach((f) => f()); await el._itemChain; } await el._itemChain;
    el._itemPend = new Set(); /* the 1.5 s "still on its way" window has passed */ };
  /* a page load: what render does with the record and this browser's caches before it draws (_itemHydrate, then the kept maximise) */
  const load = (doc) => { el._doc = doc; el._itemHydrate(doc); if (el._maxFor !== el.canvasId) { el._maxFor = el.canvasId; el._max = store['vera:canvas:max:' + CID] || ''; } };
  return { el, store, flush, load, warns };
}
const press = (el, key, act, closest) => el._act({ dataset: { act, layerKey: key }, closest: closest || (() => null) });
const tabBar = (keys) => ({ querySelectorAll: () => keys.map((k) => ({ dataset: { layerKey: k } })) });
const BLOCKS = [
  { id: 'b1', key: 'note:a', type: 'note', state: 'now', content: { text: 'maximise me' } },
  { id: 'b2', key: 'chart:b', type: 'widget', state: 'now', content: {} },
  { id: 'b3', key: 'records:c', type: 'records', state: 'now', content: {} },
  { id: 'b4', key: 'records:d', type: 'records', state: 'now', content: {} },
  { id: 'b5', key: 'records:e', type: 'records', state: 'now', content: {} },
  { id: 'b6', key: 'table:f', type: 'table', state: 'now', content: {} },
  { id: 'b7', key: 'note:w', type: 'note', state: 'now', content: {} },
  { id: 'b8', key: 'note:p', type: 'note', state: 'now', content: {} },
];

(async () => {
  await t('the record knows back, dock and the group fields - and the two halves agree on them', () => {
    const py = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'item_core.py'), 'utf8');
    const tup = (name) => (py.match(new RegExp('^' + name + ' = \\(([^)]*)\\)', 'm')) || [])[1].split(',').map((s) => s.trim().replace(/^"|"$/g, '')).filter(Boolean);
    assert.deepStrictEqual(I.SETTABLE, tup('SETTABLE')); assert.deepStrictEqual(I.BACKS, tup('BACKS')); assert.deepStrictEqual(I.DOCKS, tup('DOCKS'));
    ['back', 'dock'].forEach((f) => assert(I.SETTABLE.includes(f), f));
  });

  await t('each of the four states round-trips: set in one browser -> the record -> a second browser with empty storage', async () => {
    const S = server(BLOCKS); const A = browser(S); A.load(S.doc());
    A.el._toggleMax('note:a');                                                         /* max */
    A.el.setLayer('chart:b', 'behind');                                                /* the layer */
    press(A.el, 'records:c', 'cmpsep');                                                /* a group: one taken out */
    const ts0 = Date.now(); A.el._cmpSet(Object.assign(A.el._cmpGet(), { 'records:d': ts0 - 5000, 'records:e': ts0 - 4000 }));
    press(A.el, 'records:d', 'cmptab', (sel) => (sel === '.cmp-tabs' ? tabBar(['records:d', 'records:e']) : null));   /* ...its front tab */
    A.el._joinSet('table:f', 'records:d');                                             /* ...one dropped on another (LV-11) */
    A.el._winKeepSet('note:w', { w: 640, h: 420, fx: 0.4, fy: 0.4 });                  /* a window the desk kept */
    await A.flush();
    assert.deepStrictEqual(A.warns, [], 'nothing refused');
    const rec = (k) => I.normalise(S.blocks.find((b) => b.key === k));
    assert.strictEqual(rec('note:a').face, 'full'); assert.strictEqual(rec('note:a').back, 'tile');
    assert.strictEqual(rec('chart:b').place.layer, 'behind');
    assert.strictEqual(rec('records:c').group.sep, true);
    assert.strictEqual(rec('records:d').group.front, true); assert.strictEqual(rec('records:e').group.front, false);
    assert(rec('records:d').group.at > rec('records:e').group.at, 'the picked tab is the latest');
    assert.strictEqual(rec('table:f').group.id, 'records:d');
    assert.strictEqual(rec('note:w').face, 'window'); assert.strictEqual(rec('note:w').dock, 'tile');
    assert.deepStrictEqual(rec('note:w').place, { fx: 0.4, fy: 0.4, w: 640, h: 420, by: 'person' });

    const B = browser(S); assert.strictEqual(B.store['vera:canvas:max:' + CID], undefined, 'B starts empty');
    B.load(S.doc());
    assert.strictEqual(B.el._max, 'note:a', 'max restored');
    assert.strictEqual(B.store['vera:canvas:max:' + CID], 'note:a', '...and cached');
    assert.strictEqual(B.el._layer['chart:b'], 'behind', 'layer restored');
    assert.strictEqual(JSON.parse(B.store['vera:canvas:layers'])['chart:b'], 'behind', '...and cached');
    assert.strictEqual(B.el._sepGet()['records:c'], 1, 'taken out of its group');
    assert.strictEqual(JSON.parse(B.store['vera:canvas:desksep:' + CID])['records:c'], 1);
    const fr = B.el._cmpGet(); assert(fr['records:d'] > fr['records:e'], 'the same tab is in front');
    assert.strictEqual(B.el._joinGet()['table:f'], 'records:d', 'joined');
    assert.deepStrictEqual(dc(B.el._winKeepGet()['note:w']), { w: 640, h: 420, fx: 0.4, fy: 0.4 }, 'the window kept');
    assert.deepStrictEqual(JSON.parse(B.store['vera:canvas:deskwin:' + CID])['note:w'], { w: 640, h: 420, fx: 0.4, fy: 0.4 });
    /* B's caches agree with A's on every family */
    ['deskwin', 'desksep', 'deskjoin'].forEach((f) => assert.deepStrictEqual(JSON.parse(B.store['vera:canvas:' + f + ':' + CID]), JSON.parse(A.store['vera:canvas:' + f + ':' + CID]), f));

    /* ...and undone in B, A follows on its next read */
    B.el._toggleMax('note:a'); B.el.setLayer('chart:b', ''); press(B.el, 'records:c', 'cmpjoin'); B.el._joinSet('table:f', null); B.el._winDock('note:w');
    await B.flush();
    A.el._itemPend = new Set(); A.el._itemHydrate(S.doc());
    assert.strictEqual(A.el._max, '', 'restored in A'); assert.strictEqual(A.store['vera:canvas:max:' + CID], undefined);
    assert.strictEqual(A.el._layer['chart:b'], undefined, 'among again in A');
    assert.strictEqual(A.el._sepGet()['records:c'], undefined, 'back in its group in A');
    assert.strictEqual(A.el._joinGet()['table:f'], undefined, 'no longer joined in A');
    assert.strictEqual(A.el._winKeepGet()['note:w'], undefined, 'the window sent back in A');
    assert.strictEqual(I.normalise(S.blocks.find((b) => b.key === 'note:a')).face, 'tile');
  });

  await t('a full item keeps what it returns to: a kept window maximised comes back a window; a pill pressed while full goes up as back', async () => {
    const S = server(BLOCKS); const A = browser(S);
    A.el._winKeepSet('note:w', { w: 500, h: 300, fx: 0.5, fy: 0.5 }); A.el._toggleMax('note:w');
    A.el._toggleMax('note:p'); press(A.el, 'note:p', 'pill');      /* a second item maximised restores the first; the second pilled while full */
    await A.flush();
    const rec = (k) => I.normalise(S.blocks.find((b) => b.key === k));
    assert.deepStrictEqual([rec('note:w').face, rec('note:w').dock], ['window', 'tile'], 'the first one is a window again');
    assert.deepStrictEqual([rec('note:p').face, rec('note:p').back], ['full', 'pill']);
    /* as the face rule has it */
    const want = I.transition(I.transition(I.faceState({ face: 'tile' }), 'max'), 'pill'); assert.deepStrictEqual([want.face, want.back], ['full', 'pill']);
    const B = browser(S); B.load(S.doc());
    assert.strictEqual(B.el._max, 'note:p'); assert.strictEqual(B.el._pillReal()['note:p'], 1, 'restored, it will be a pill');
    assert.deepStrictEqual(dc(B.el._winKeepGet()['note:w']), { w: 500, h: 300, fx: 0.5, fy: 0.5 });
  });

  await t('the caches stand where the record says nothing; a maximise kept from before the record goes up instead of being lost', async () => {
    const S = server(BLOCKS.map((b) => (b.key === 'note:a' ? Object.assign({}, b, { face: 'tile' }) : b)));   /* a pre-S7 record: face, no back */
    const B = browser(S);
    B.store['vera:canvas:max:' + CID] = 'note:a'; B.store['vera:canvas:desksep:' + CID] = JSON.stringify({ 'records:c': 1 });
    B.store['vera:canvas:layers'] = JSON.stringify({ 'chart:b': 'front' }); B.store['vera:canvas:deskwin:' + CID] = JSON.stringify({ 'note:p': { w: 400, h: 300, fx: 0.3, fy: 0.3 } });
    B.load(S.doc());
    assert.strictEqual(B.el._max, 'note:a', 'the maximise stands');
    assert.strictEqual(B.el._sepGet()['records:c'], 1); assert.strictEqual(JSON.parse(B.store['vera:canvas:layers'])['chart:b'], 'front');
    assert(B.el._winKeepGet()['note:p'], 'a window the record says nothing of stands');
    await B.flush();
    assert.strictEqual(I.normalise(S.blocks.find((b) => b.key === 'note:a')).face, 'full', 'the kept maximise went up');
  });

  await t('a change of ours still on its way is not overwritten by an older record', async () => {
    const S = server(BLOCKS); const A = browser(S);
    A.el._toggleMax('note:a');                     /* queued, not sent */
    A.el._itemHydrate(S.doc());                    /* the record does not have it yet */
    assert.strictEqual(A.el._max, 'note:a'); assert.strictEqual(A.store['vera:canvas:max:' + CID], 'note:a');
  });

  await t('the probe and layout paths are untouched: hydrate of records with none of these fields changes nothing', () => {
    const S = server(BLOCKS.map((b) => Object.assign({}, b, { face: 'tile', place: { fx: 0.2, fy: 0.3, by: 'person' } })));
    const B = browser(S); B.el._html = 'drawn'; B.load(S.doc());
    ['deskwin', 'desksep', 'deskjoin', 'deskfront', 'max'].forEach((f) => assert.strictEqual(B.store['vera:canvas:' + f + ':' + CID], undefined, f));
    assert.strictEqual(B.store['vera:canvas:layers'], undefined); assert.deepStrictEqual(dc(B.el._layer), {});
  });

  console.log(n + ' passed');
})().catch((e) => { console.error('FAIL', e && e.stack || e); process.exit(1); });
