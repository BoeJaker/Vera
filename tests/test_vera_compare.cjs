// THE compare workspace (looplab-control §9 K4, 18 Oct 2026): pins are records; the pure half runs in Node.
const assert = require('assert');
const path = require('path');
const C = require(path.join(__dirname, '..', 'vera', 'ui', 'vera_compare.js'));
const T = require(path.join(__dirname, '..', 'vera', 'ui', 'vera_timeline.js'));
let fails = 0;
function t(name, fn) { try { fn(); console.log('ok  ', name); } catch (e) { fails++; console.log('FAIL', name, '-', e.message); } }

const RUNS = { lane: 'll.runs', label: 'Runs', type: 'bar', color: '#7aa2f7', source: { cap: 'evolve.schedule.events', args: {} } };
const H = 3600e3, T0 = Date.parse('2026-10-18T12:00:00Z');

t('a pin is a record: a lane, or an item with a time', () => {
  assert.deepStrictEqual(C.problems({ kind: 'lane', lane: RUNS }), []);
  assert.strictEqual(C.problems({ kind: 'item', lane: 'll.runs' }).length, 1, 'an item needs t');
  assert.strictEqual(C.problems({ kind: 'nope' }).length, 2);
  const p = C.normPin({ kind: 'item', lane: RUNS, t: '2026-10-18T12:00:00Z', label: 'release', drill: 'event:r1' });
  assert.strictEqual(p.pin, 'item:event:r1'); assert.strictEqual(p.t, T0); assert.strictEqual(p.color, '#7aa2f7');
  assert.strictEqual(C.normPin({ kind: 'lane', lane: 'll.runs' }).pin, 'lane:ll.runs');
});
t('pins add once per id, remove and clear - without browser storage too', () => {
  C.clearPins();
  C.addPin({ kind: 'lane', lane: RUNS }); C.addPin({ kind: 'lane', lane: RUNS });
  C.addPin({ kind: 'item', lane: RUNS, t: T0, drill: 'event:r1' });
  assert.strictEqual(C.pins().length, 2);
  C.removePin('lane:ll.runs'); assert.deepStrictEqual(C.pins().map((p) => p.pin), ['item:event:r1']);
  assert.strictEqual(C.addPin({ kind: 'item', lane: RUNS }), null);
  C.clearPins(); assert.strictEqual(C.pins().length, 0);
});
const DATA = [
  { spec: { lane: 'll.runs', label: 'Runs', color: '#7aa2f7' }, items: [{ t: T0 - 2 * H, label: 'a' }, { t: T0 - H, label: 'b' }, { t: T0 + 0.5 * H, label: 'c' }] },
  { spec: { lane: 'lat', label: 'Latency', color: '#e0a458' }, items: [{ t: T0 - H, value: 10, label: 'x' }, { t: T0 + H, value: 16, label: 'y' }, { t: T0 + 2 * H, value: 18, label: 'z' }] },
];
t('insights: before vs after the anchor per lane, means where lanes carry values, nearest items as evidence', () => {
  const rows = C.insights(DATA, T0, 3 * H);
  assert.strictEqual(rows[0].before, 2); assert.strictEqual(rows[0].after, 1); assert.strictEqual(rows[0].delta, -1);
  assert.strictEqual(rows[0].mean_delta, null);
  assert.strictEqual(rows[1].mean_before, 10); assert.strictEqual(rows[1].mean_after, 17); assert.strictEqual(rows[1].mean_delta, 7);
  assert.deepStrictEqual(rows[0].evidence.map((e) => e.offset_min), [30, -60, -120]);
  assert.deepStrictEqual(C.insights(DATA, null, H), []);
});
t('the compared window never runs past now, and is the same length both sides', () => {
  assert.strictEqual(C.spanFor(T0, 3 * H, T0 + H), H);
  const rows = C.insights(DATA, T0, 3 * H, T0 + H);
  assert.strictEqual(rows[0].before, 1, 'only the hour before counts, matching the hour after');
  assert.strictEqual(rows[0].after, 1);
  assert.deepStrictEqual(C.insights(DATA, T0, 3 * H, T0 + 1000), [], 'too recent: nothing to compare');
  assert.ok(C.insightRows([], C.spanFor(T0, 3 * H, T0 + 1000), 3 * H).includes('too recent'));
  assert.ok(C.insightRows(rows, H, 3 * H).includes('cut to now'));
});
t('the window bounds what counts', () => {
  const rows = C.insights(DATA, T0, H);
  assert.strictEqual(rows[0].before, 1); assert.strictEqual(rows[1].after, 1);
});
t('layer mode: one row of every lane, sorted, each item keeping its lane colour', () => {
  const items = C.composite(DATA);
  assert.strictEqual(items.length, 6);
  assert.ok(items.every((it, i) => i === 0 || items[i - 1].t <= it.t));
  assert.strictEqual(items.find((i) => i.label === 'x').color, '#e0a458');
  assert.ok(items[0].tip.startsWith('Runs · '));
});
t('pinned items become guide lines; the timeline draws them across every lane', () => {
  const ov = C.overlay([C.normPin({ kind: 'item', lane: RUNS, t: T0, label: 'release 65bab' }), C.normPin({ kind: 'lane', lane: RUNS })]);
  assert.strictEqual(ov.length, 1);
  const s = T.registerLane(RUNS);
  const html = T.render({ width: 900, range: { from: T0 - 3 * H, to: T0 + 3 * H, now: T0 }, lanes: [{ spec: s, items: [], index: 0 }], overlay: [Object.assign(ov[0], { anchor: true })] });
  assert.ok(html.includes('class="vt-guide"') && html.includes('release 65bab') && html.includes('stroke-width="2.2"'));
  const out = T.render({ width: 900, range: { from: T0 + H, to: T0 + 3 * H, now: T0 }, lanes: [{ spec: s, items: [], index: 0 }], overlay: ov });
  assert.ok(!out.includes('vt-guide'), 'a guide outside the range is not drawn');
});
t('insight rows: a change says which way, evidence is listed, no buttons in the rows', () => {
  const html = C.insightRows(C.insights(DATA, T0, 3 * H));
  assert.ok(html.includes('+7 (mean)') && html.includes('vc-d up') && html.includes('-1 item(s)') && html.includes('vc-d down'));
  assert.ok(html.includes('+30m c'));
  assert.ok(!/<button/.test(html));
});
if (fails) { console.log(fails + ' FAILED'); process.exit(1); } else console.log('all passed');
