// P-SUMMON (looplab-control W1, owner 18 Oct): type a widget in, it comes in as a temporary tile; clear it and it goes.
//   node tests/test_dashboard_summon.cjs
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), assert = require('assert');
const DASH = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-dashboard.js'), 'utf8');
const C = require(path.join(__dirname, '..', 'vera', 'ui', 'vera_compare.js'));
let fails = 0;
function t(name, fn) { try { fn(); console.log('ok  ', name); } catch (e) { fails++; console.log('FAIL', name, '-', e.message); } }

const i0 = DASH.indexOf('/* summon:match begin */'), i1 = DASH.indexOf('/* summon:match end */');
const ctx = {}; vm.createContext(ctx); vm.runInContext(DASH.slice(i0, i1) + '\nthis.M = summonMatches;', ctx);
const LIST = [
  { id: 'panel:board-panel', name: 'Board', form: 'panel' },
  { id: 'looplab:agents-board', name: 'Board', form: 'ci-board', reads: { cap: 'ci.board' } },
  { id: 'looplab:live-board', name: 'Open work', form: 'ci-board', reads: { cap: 'ci.board', note: 'the board, open lanes' } },
  { id: 'git', name: 'Commit graph', form: 'element' },
  { id: 'cen', name: 'Census trends', form: 'trend-layers', tags: ['census'] },
];
t('the best match for "board" is the drawn board before the board panel of the same name', () => {
  const m = ctx.M(LIST, 'board');
  assert.strictEqual(m[0].id, 'looplab:agents-board'); assert.strictEqual(m[1].id, 'panel:board-panel');
  assert.ok(m.some((x) => x.id === 'looplab:live-board'), 'a board by what it reads still matches');
});
t('every word must match somewhere; a word that matches nothing drops a template', () => {
  assert.strictEqual(ctx.M(LIST, 'commit graph')[0].id, 'git');
  assert.strictEqual(ctx.M(LIST, 'commit zebra').length, 0);
  assert.strictEqual(ctx.M(LIST, 'census')[0].id, 'cen');
});
t('an empty search brings nothing', () => { assert.strictEqual(ctx.M(LIST, '  ').length, 0); /* lengths: arrays from the vm context are another realm's */ });
t('summoned tiles are never saved with the layout, and clearing the search removes what it brought', () => {
  assert.ok(DASH.includes("if (!w || !w.dataset.wid || w.dataset.temp) return;"), 'layoutRecord skips temporary tiles');
  const sm = DASH.slice(DASH.indexOf('function summon(q, o2)'), DASH.indexOf('function removeDynamic('));
  assert.ok(sm.indexOf('clearSummoned();') < sm.indexOf('templates().then'), 'what the search brought before goes first');
  assert.ok(sm.includes("mcp('widget.template.instantiate', { id: t.id, where: 'dashboard'") && sm.includes('temporary: true'));
  assert.ok(sm.includes('setTimeout(function () { placeInstance(wid, t); }, SUMMON_PLACE_MS)'), 'placed on the server only once shown a moment');
  assert.ok(sm.includes("mcp('widget.instance.remove', { id: tmp.instance })"), 'its placed instance is removed with it');
  assert.ok(sm.includes('state.dynamic[wid] = { record: tmp.record, wid: wid }; delete state.temp[wid];') && sm.includes('save();'), 'keep makes it an ordinary tile');
});
t('a tile head drags as an item record; Compare turns a lanes widget into lane pins and says why for anything else', () => {
  assert.ok(DASH.includes("e.dataTransfer.setData('application/x-vera-item', JSON.stringify(item))"));
  const lanes = { kind: 'widget', content: { record: { form: 'lanes', draw: { lane_specs: [{ lane: 'x.runs', label: 'Runs', type: 'bar', source: { cap: 'a.b' } }, { lane: 'x y' }] } } } };
  const r = C.pinsFromItem(lanes);
  assert.strictEqual(r.pins.length, 2); assert.strictEqual(C.normPin(r.pins[0]).pin, 'lane:x.runs');
  assert.ok(C.pinsFromItem({ kind: 'widget', content: { record: { form: 'table' } } }).why.includes('table'));
  assert.ok(C.pinsFromItem({}).why);
});
t('the summon box belongs to the dashboard element, so every dashboard can have it', () => {
  assert.ok(DASH.includes("self.hasAttribute('summon') ? '<input type=\"search\" class=\"vdb-summon\""));
  assert.ok(DASH.includes("if (e.key === 'Escape') { sIn.value = ''; go(); }"), 'Esc clears it, and what it brought goes');
});
t("no item drag while editing: the title is the tile's reorder handle then (vera-d0's review)", () => {
  const dr = DASH.slice(DASH.indexOf('function itemDrag('), DASH.indexOf('function removeDynamic('));
  assert.ok(dr.indexOf('if (state.editing) return;') < dr.indexOf('e.stopPropagation()'), 'editing returns before the item drag stops the event');
  assert.ok(dr.includes("head.setAttribute('draggable', state.editing ? 'false' : 'true')"));
  assert.ok(DASH.includes("if (hd) hd.setAttribute('draggable', state.editing ? 'false' : 'true');"), 'toggling edit toggles the head');
});
if (fails) { console.log(fails + ' FAILED'); process.exit(1); } else console.log('all passed');
