// How an item changes face (DEEP-29 11A.4 Decision 3; WSU-58 S2): VeraItem.transition gives the server's answers (the shared
// cases and grid), and the desk element itself - driven through pill / tile / window / full, locked and parked on a stub DOM,
// for EVERY BLOCK kind - does what the rule says, the same for every kind. Owner O2 (2026-10-17): a window stays until it is
// sent back.   node tests/test_item_faces.cjs
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const I = require(path.join(__dirname, '..', 'vera', 'ui', 'vera_item.js'));
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const V = require(FILE);
const CASES = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'item_face_cases.json'), 'utf8'));
let n = 0; const t = (name, fn) => { fn(); n++; console.log('ok -', name); };
const dc = (o) => JSON.parse(JSON.stringify(o));
const T = (x) => [x.face, x.state, x.locked ? 1 : 0, x.back, x.dock, x.pilled ? 1 : 0];
const U = (a) => ({ face: a[0], state: a[1], locked: !!a[2], back: a[3], dock: a[4], pilled: !!a[5] });

t('the written cases: the JS half gives the rule\'s answers, and never changes the item', () => {
  CASES.cases.forEach((c) => { const item = dc(c.item); const got = I.transition(item, c.event);
    assert.deepStrictEqual(item, c.item, c.name);
    Object.entries(c.want).forEach(([k, v]) => assert.deepStrictEqual(got[k], v, c.name + ': ' + k + ' ' + JSON.stringify(got))); });
});
t('the grid: every face state x every event, exactly as the Python half answers', () => {
  assert(CASES.grid.length > 1000);
  CASES.grid.forEach(([from, ev, want]) => {
    assert.deepStrictEqual(I.faceState(U(from)), U(from), 'a face state reads as itself ' + JSON.stringify(from));
    assert.deepStrictEqual(T(I.transition(U(from), ev)), want, JSON.stringify([from, ev])); });
});
t('the vocabulary matches item_core', () => {
  const py = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'item_core.py'), 'utf8');
  const tup = (name) => (py.match(new RegExp('^' + name + ' = \\(([^)]*)\\)', 'm')) || [])[1].split(',').map((s) => s.trim().replace(/^"|"$/g, '')).filter(Boolean);
  assert.deepStrictEqual(I.FACE_EVENTS, tup('FACE_EVENTS')); assert.deepStrictEqual(I.LAYOUT_EVENTS, tup('LAYOUT_EVENTS'));
});

/* ── the element itself, on a stub DOM ─────────────────────────────────────────────────────────────────────────────────── */
function loadElement() {
  let Cls = null;
  const store = {};
  class Node0 {
    constructor() { this._attrs = {}; this.dataset = {}; }
    attachShadow() { this.shadowRoot = { getElementById: () => null, querySelectorAll: () => [], querySelector: () => null }; return this.shadowRoot; }
    getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; } hasAttribute(k) { return k in this._attrs; }
    setAttribute(k, v) { this._attrs[k] = String(v); } removeAttribute(k) { delete this._attrs[k]; } dispatchEvent() { return true; }
    addEventListener() {} toggleAttribute(k, on) { if (on) this._attrs[k] = ''; else delete this._attrs[k]; }
  }
  const ctx = { console, setTimeout: (f) => 0, clearTimeout: () => {}, requestAnimationFrame: () => 0, HTMLElement: Node0,
    localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } },
    CustomEvent: class { constructor(n, o) { this.type = n; this.detail = o && o.detail; } }, getComputedStyle: () => ({ zIndex: 'auto' }),
    fetch: () => Promise.resolve({ json: () => ({}) }), document: {}, navigator: {},
    customElements: { get: () => null, define: (name, c) => { if (name === 'vera-canvas') Cls = c; } } };
  ctx.window = ctx; ctx.globalThis = ctx;
  vm.createContext(ctx); vm.runInContext(fs.readFileSync(FILE, 'utf8'), ctx, { filename: FILE });
  assert(Cls, 'the element class'); return { Cls, store };
}
function classes(init) { const s = new Set(init || []); return { add: (...a) => a.forEach((x) => s.add(x)), remove: (...a) => a.forEach((x) => s.delete(x)),
  contains: (x) => s.has(x), toggle: (x, on) => { const v = on === undefined ? !s.has(x) : !!on; if (v) s.add(x); else s.delete(x); return v; } }; }

/* one canvas with one item of a kind on the desk */
function desk(kind) {
  const { Cls, store } = loadElement(); const el = new Cls(); const key = kind + ':one';
  el.setAttribute('canvas-id', 'cv-faces'); el.setAttribute('align', 'desk'); el.setAttribute('stage', '');
  const card = { dataset: { key, type: kind, deskFace: 'card' }, classList: classes(['it', 'now']), style: { left: '100px', top: '80px', zIndex: '' },
    offsetWidth: 320, offsetHeight: 220, querySelectorAll: () => [], querySelector: () => null, closest: () => null };
  const stage = { querySelectorAll: () => [card] };
  el.shadowRoot = { getElementById: (id) => (id === 'stage' ? stage : null), querySelectorAll: () => [card], querySelector: () => null };
  el._doc = { revision: 1, blocks: [{ id: 'bk1', key, type: kind, state: 'now', size: 'm', content: {} }] };
  el._deskGeo = { W: 1200, H: 800 };
  /* the layout and its history (another session's code), the network and the paint: not what this test is about */
  ['render', '_placeNow', '_holdStart', '_histPush', '_histBack', '_pillCard', '_chipPeek', '_revealSoon', 'refresh'].forEach((m) => { el[m] = () => {}; });
  el.writes = []; el._itemSet = (k, f) => { el.writes.push(Object.assign({ key: k }, f)); };
  el.call = (name, args) => { const b = el._doc.blocks[0];
    if (name === 'canvas.park') b.state = 'parked'; else if (name === 'canvas.pin') b.state = 'pinned'; else if (name === 'canvas.add') b.state = 'now';
    card.classList.toggle('pinned', b.state === 'pinned'); return Promise.resolve({ ok: true }); };
  return { el, card, key, store };
}
const press = (el, key, act) => el._act({ dataset: { act, layerKey: key }, closest: () => null });

/* WHAT THE DESK SHOWS, read off the element's state in the order its placing reads it (_placeDesk, render): a maximised item
   in the now / pinned bands fills the desk (a front layer before anything else); a window (kept, or a pill opened as one) floats;
   a locked item keeps the face it was locked with; the person's pill, a card let go among the pills, or a pill the layout made
   is a pill; a parked item is a pill on the shelf; the rest are tiles. */
function shown(d) {
  const { el, card, key } = d; const b = el._doc.blocks[0]; const state = b.state || 'now';
  const lk = el._lockGet()[key], pos = el._deskPosGet()[key];
  const under = () => { if ((el._winKeepGet()[key]) || (el._wins && el._wins.has(key))) return 'window';
    if (lk) return /chip|icon/.test(lk.face || '') ? 'pill' : 'tile';
    if (el._pillReal()[key] || (pos && pos.chip) || card.classList.contains('desk-chip')) return 'pill'; return 'tile'; };
  const dockOf = () => (el._wins && el._wins.has(key)) ? 'pill' : el._winKeepGet()[key] ? 'tile' : '';
  if (state === 'parked' || state === 'hidden') return { face: 'pill', state, locked: !!lk, back: '', dock: '', pilled: !!el._pillReal()[key] };
  const u = under(); const full = el._max === key;
  const face = full ? 'full' : u, back = full ? u : '';
  const dock = (face === 'window' || back === 'window') ? dockOf() : '';
  return { face, state, locked: !!lk, back, dock, pilled: !!el._pillReal()[key] };
}

/* the element's own entry points for each event (the handler's body, where the handler is a closure on the page) */
const DO = {
  pill: (d) => press(d.el, d.key, 'pill'),
  unpill: (d) => d.el._pillSet(d.key, false),                                   /* a click on the person's pill */
  open: (d) => { const { el, card, key } = d;                                    /* a click on a pill (body click handler) */
    if (el._pillReal()[key]) { el._pillSet(key, false); return; }
    if (card.classList.contains('desk-chip')) el._winOpen(card); },
  max: (d) => press(d.el, d.key, 'max'),
  restore: (d) => { const { el, key } = d;                                       /* Esc: on a full item (its keydown), else a window (the page's) */
    if (el._max === key) { press(el, key, 'max'); return; }
    el._focusKey = key; if (el._winKeepGet()[key]) { el._winDock(key); return; } if (el._wins && el._wins.has(key)) el._winClose(key); },
  send_back: (d) => press(d.el, d.key, 'dock'),
  lock: (d) => { if (!d.el._lockGet()[d.key]) press(d.el, d.key, 'lock'); },
  unlock: (d) => { if (d.el._lockGet()[d.key]) press(d.el, d.key, 'lock'); },
  park: (d) => press(d.el, d.key, 'park'),
  unpark: (d) => d.el.call('canvas.add', { key: d.key }),                        /* the shelf's chip */
  pin: (d) => press(d.el, d.key, 'pin'),
  unpin: (d) => press(d.el, d.key, 'pin'),
  focus: (d) => d.el._focusSet(d.card),
  blur: (d) => d.el._focusSet(null),
  /* the layout's own decisions (the placing pass): a resize let go with no room keeps a window; far from the chat a pill */
  no_room: (d) => { if (!d.el._lockGet()[d.key]) d.el._winKeepSet(d.key, { w: 640, h: 420, fx: 0.4, fy: 0.4 }); },
  to_border: (d) => { if (!d.el._lockGet()[d.key]) { d.card.classList.add('desk-chip'); d.card.dataset.deskFace = 'chip'; } },
  from_border: (d) => { if (!d.el._lockGet()[d.key]) { d.card.classList.remove('desk-chip'); d.card.dataset.deskFace = 'card'; } },
};
/* through all four faces, a window kept and blurred, a pill opened as a window, locked and parked */
const WALK = ['pill', 'open', 'no_room', 'blur', 'focus', 'max', 'restore', 'send_back', 'max', 'pill', 'restore', 'max', 'max', 'unpill',
  'to_border', 'open', 'blur', 'restore', 'open', 'pill', 'from_border', 'lock', 'max', 'restore', 'no_room', 'to_border', 'unlock',
  'no_room', 'blur', 'send_back', 'pin', 'max', 'unpin', 'restore', 'park', 'unpark'];

const KINDS = Object.keys(V.BLOCK);
const runs = {};
t('every BLOCK kind, driven through the faces on the element: the desk shows what the rule says', () => {
  assert(KINDS.length >= 20, 'every kind is exported');
  KINDS.forEach((kind) => {
    const d = desk(kind); let rule = I.faceState({ face: 'tile' }); const seen = [];
    assert.deepStrictEqual(shown(d), rule, kind + ': starts a tile');
    WALK.forEach((ev, i) => {
      d.el.writes = [];
      DO[ev](d); rule = I.transition(rule, ev); const got = shown(d);
      assert.deepStrictEqual(got, rule, kind + ' step ' + i + ' ' + ev + ': ' + JSON.stringify(got) + ' rule ' + JSON.stringify(rule));
      /* what goes up to the item record (canvas.item.set) is the face shown - or, under a full item (max is not in the record yet,
         S7), the face it returns to */
      d.el.writes.filter((w) => w.face).forEach((w) => assert.strictEqual(w.face, rule.face === 'full' ? rule.back : rule.face, kind + ' step ' + i + ' ' + ev + ' writes face ' + w.face));
      seen.push(T(got)); });
    runs[kind] = seen;
  });
});
t('the rule is the same for every kind: code and records (and every other kind) walk exactly alike', () => {
  assert.deepStrictEqual(runs.code, runs.records, 'code vs records');
  KINDS.forEach((k) => assert.deepStrictEqual(runs[k], runs.records, k + ' vs records'));
  const faces = new Set(runs.code.map((x) => x[0])); ['pill', 'tile', 'window', 'full'].forEach((f) => assert(faces.has(f), 'the walk reaches ' + f));
});
t('a locked item: the pill control and the layout leave its face as it is, on every kind', () => {
  KINDS.forEach((kind) => { const d = desk(kind); DO.lock(d); d.el.writes = []; DO.pill(d); DO.to_border(d); DO.no_room(d);
    const rule = I.transition(I.faceState({ face: 'tile', locked: true }), 'pill');
    assert.strictEqual(shown(d).face, rule.face, kind);
    /* KNOWN (WSU-58 S2 report, fixed when the container is rewired): the element remembers that pill press (_pillSet runs with
       no lock check), writes face 'pill' to the item record while the desk shows a locked tile, and the item becomes a pill
       the moment it is unlocked - the rule refuses the press, so unlocking gives the tile back */
    assert(d.el.writes.some((w) => w.face === 'pill'), kind + ': known - the record is told pill');
    DO.unlock(d); assert.strictEqual(shown(d).face, 'pill', kind + ': known - unlocked, it is the pill pressed while locked');
    assert.strictEqual(I.transition(I.transition(rule, 'pill'), 'unlock').face, 'tile'); });
});
console.log(n + ' passed');
