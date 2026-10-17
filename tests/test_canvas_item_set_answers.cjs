// WSU-58 S1 (defect 2.4): every view a body offers reaches the item record, and a refused field never takes a queued
// place or lock down with it. canvas.item.set refuses a WHOLE call when one field is bad; the page used to merge a view
// with the place / lock queued in the same 400 ms and never read the answer, so a view the record did not know (the
// JSON lens's tree / flat / validate, an app's drawn) silently lost the move too. Now the view goes on its own call, a
// refused multi-field call is sent again a field at a time, and a refusal is told on the console.
//   node tests/test_canvas_item_set_answers.cjs
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const SRC = fs.readFileSync(FILE, 'utf8');
const I = require(path.join(__dirname, '..', 'vera', 'ui', 'vera_item.js'));
let n = 0; const t = async (name, fn) => { await fn(); n++; console.log('ok -', name); };

/* the element's class, taken from the page's own customElements.define, with a stand-in server that answers as
   canvas.item.set does (VeraItem.applySet is the server's twin, held to its answers by the shared cases) */
function load() {
  let Cls = null; const timers = new Map(); let tid = 0; const warns = []; const calls = [];
  const blocks = {};
  const ctx = {
    console: { log() {}, error() {}, warn: (...a) => warns.push(a.join(' ')) },
    setTimeout: (fn) => { const id = ++tid; timers.set(id, fn); return id; }, clearTimeout: (id) => timers.delete(id),
    localStorage: { getItem: () => null, setItem() {} },
    customElements: { get: () => undefined, define: (n2, c) => { Cls = c; } },
    fetch: async (url, opt) => {
      const body = JSON.parse(opt.body); calls.push(body.arguments);
      const a = Object.assign({}, body.arguments); const key = a.key; delete a.id; delete a.key;
      const b = blocks[key] || (blocks[key] = { type: 'note', key });
      const r = I.applySet(b, a);
      const ans = r.problems.length ? { ok: false, error: r.problems.join('; '), key } : { ok: true, key, changed: r.changed };
      return { json: async () => ({ type: 'tool_result', tool_name: 'canvas.item.set', content: ans }) };
    },
    Promise, JSON, Object, Array, String, Number, Math, Date, Set, Map, RegExp, Error, URL, isFinite, parseFloat, parseInt,
  };
  ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
  vm.createContext(ctx); vm.runInContext(SRC, ctx, { filename: 'canvas_element.js' });
  assert.ok(Cls, 'the page defines <vera-canvas>');
  const el = Object.create(Cls.prototype); Object.defineProperty(el, 'canvasId', { value: 'cv1' }); el._bidOf = () => false;
  const flush = async () => { for (let i = 0; i < 20 && timers.size; i++) { const fns = [...timers.values()]; timers.clear(); fns.forEach((f) => f()); await el._itemChain; } await el._itemChain; };
  return { el, calls, warns, blocks, flush };
}

(async () => {
  await t('the record knows every view the desk\'s bodies offer (viewsOf, VIEW_LABEL, the code explode)', () => {
    const lab = SRC.match(/const VIEW_LABEL = \{([^}]*)\}/)[1].match(/(\w+):/g).map((s) => s.slice(0, -1));
    ['code', 'structure', 'both'].concat(lab).forEach((v) => assert.ok(I.VIEWS.includes(v), v + ' is not in VeraItem.VIEWS'));
    ['tree', 'flat', 'validate', 'drawn'].forEach((v) => assert.deepStrictEqual(I.applySet({ type: 'json', key: 'j' }, { view: v }).problems, [], v));
  });

  await t('a refused view never drops the place queued beside it, and the refusal is told', async () => {
    const S = load();
    S.el._itemSet('note:a', { place: { fx: 0.3, fy: 0.2, by: 'person' } });
    S.el._itemSet('note:a', { view: 'radar' });
    await S.flush();
    assert.deepStrictEqual(S.blocks['note:a'].place, { fx: 0.3, fy: 0.2, by: 'person' }, 'the place reached the record');
    assert.ok(S.calls.some((c) => c.view === 'radar' && Object.keys(c).length === 3), 'the view went on its own call');
    assert.ok(S.warns.some((w) => /canvas\.item\.set refused/.test(w) && /view/.test(w)), 'the refusal is on the console');
  });

  await t('a lock queued with a json tree view: both are written', async () => {
    const S = load();
    S.el._itemSet('json:b', { place: { fx: 0.5, fy: 0.5, w: 300, h: 200, by: 'person', locked: true } });
    S.el._itemSet('json:b', { view: 'tree' });
    await S.flush();
    assert.strictEqual(S.blocks['json:b'].place.locked, true); assert.strictEqual(S.blocks['json:b'].view, 'tree');
    assert.deepStrictEqual(S.warns, []);
  });

  await t('a refused call of several fields is sent again a field at a time: only the bad one is lost', async () => {
    const S = load();
    S.el._itemSet('note:c', { face: 'pill', size: 'huge' });
    await S.flush();
    assert.strictEqual(S.blocks['note:c'].face, 'pill', 'the good field reached the record');
    assert.ok(!('size' in S.blocks['note:c']) || S.blocks['note:c'].size !== 'huge');
    assert.ok(S.warns.some((w) => /size/.test(w)), 'the bad field is told');
  });

  await t('an app\'s drawn view is written', async () => {
    const S = load(); S.el._itemSet('app:d', { view: 'drawn' }); await S.flush();
    assert.strictEqual(S.blocks['app:d'].view, 'drawn'); assert.deepStrictEqual(S.warns, []);
  });

  await t('the records card\'s view is written like every other kind\'s, and read back on reload', () => {
    const i = SRC.indexOf("if (act === 'setview')"); const seg = SRC.slice(i, i + 500);
    assert.ok(/if \(b\.type === 'records'\) \{ const st = recState\(this, key\); st\.view = v; st\.page = 0; \}\s*viewSet\(this, key, v\);/.test(seg), 'setview writes a records view through viewSet');
    assert.ok(SRC.includes("const kept = el ? viewOf(el, key, '') : ''; if (kept || c.view) st.view = String(kept || c.view);"), 'recCtx starts from the kept view');
    assert.ok(SRC.includes('if (this._recUi && this._recUi[key]) this._recUi[key].view = b.view;'), 'hydrate hands the record\'s view to a drawn records card');
  });

  console.log(n + ' passed');
})().catch((e) => { console.error('FAIL', e && e.stack || e); process.exit(1); });
