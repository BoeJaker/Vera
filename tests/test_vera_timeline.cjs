// THE timeline (looplab-control §9 K1, 18 Oct 2026): lanes are records; the pure half runs in Node.
const assert = require('assert');
const path = require('path');
const T = require(path.join(__dirname, '..', 'vera', 'ui', 'vera_timeline.js'));
let fails = 0;
function t(name, fn) { try { fn(); console.log('ok  ', name); } catch (e) { fails++; console.log('FAIL', name, '-', e.message); } }

t('the seven lane types are registered', () => {
  assert.deepStrictEqual(T.types(), ['band', 'bar', 'chip', 'graph', 'mark', 'strip', 'tiles']);
});
t('a lane record registers; a bad one says why', () => {
  const s = T.registerLane({ lane: 'errors', label: 'Errors', type: 'strip', source: { cap: 'obs.events', args: { level: 'error' } }, map: { items: 'events', t: 'ts', value: 'n' }, live: 30 });
  assert.strictEqual(s.lane, 'errors'); assert.strictEqual(s.live, 30); assert.strictEqual(T.lane('errors').source.cap, 'obs.events');
  assert.strictEqual(T.registerLane({ lane: 'x y', type: 'nope' }), null);
  assert.ok(T.problems({ lane: 'x y', type: 'nope' }).length === 2);
});
t('a family can add a lane type without touching the element', () => {
  T.registerLaneType('dots', (items, ctx) => items.map(() => '<circle class="dot"/>').join(''));
  assert.ok(T.types().includes('dots'));
  const s = T.registerLane({ lane: 'd', type: 'dots' });
  const html = T.render({ width: 800, range: { from: 0, to: 3600e3, now: 0 }, lanes: [{ spec: s, items: [{ t: 10e3 }], index: 0 }] });
  assert.ok(html.includes('class="dot"'));
});
t('itemsOf maps a source answer through the record, sorted by time', () => {
  const s = T.registerLane({ lane: 'runs', type: 'bar', map: { items: 'events', t: 'start', end: 'end', label: 'title', tip: '{title} - {kind}', color: { field: 'kind', map: { census: '#c9a35a' }, else: '#7aa2f7' } } });
  const items = T.itemsOf(s, { events: [{ start: '2026-10-18T10:00:00Z', end: '2026-10-18T11:00:00Z', title: 'b', kind: 'gate' },
                                        { start: '2026-10-18T09:00:00Z', end: '2026-10-18T09:30:00Z', title: 'a', kind: 'census' },
                                        { title: 'no time' }] });
  assert.deepStrictEqual(items.map((i) => i.label), ['a', 'b']);
  assert.strictEqual(items[0].color, '#c9a35a'); assert.strictEqual(items[1].color, '#7aa2f7');
  assert.strictEqual(items[0].tip, 'a - census');
  assert.strictEqual(items[0].end - items[0].t, 1800e3);
});
t('epoch seconds and ISO strings both read as times', () => {
  assert.strictEqual(T.toMs(1792300000), 1792300000000); assert.strictEqual(T.toMs('2026-10-18T00:00:00Z'), Date.parse('2026-10-18T00:00:00Z'));
});
t('the scale maps the range onto the drawing width after the label column', () => {
  const sc = T.scale({ from: 0, to: 1000 }, 1150);
  assert.strictEqual(sc.x(0), T.LABEL_W); assert.strictEqual(sc.x(1000), 1150); assert.strictEqual(sc.x(500), T.LABEL_W + 500);
  assert.strictEqual(sc.x(-50), T.LABEL_W); assert.strictEqual(Math.round(sc.t(sc.x(250))), 250);
});
t('render draws every lane, the now-line, the upcoming hatch and a lane read error', () => {
  const a = T.registerLane({ lane: 'a', label: 'Code', type: 'mark' }), b = T.registerLane({ lane: 'b', label: 'Errors', type: 'strip' });
  const html = T.render({ width: 1000, range: { from: 0, to: 7200e3, now: 3600e3 },
                          lanes: [{ spec: a, items: [{ t: 600e3, label: '▲', tip: 'release' }], index: 0 }, { spec: b, error: 'timeout', index: 1 }] });
  assert.ok(html.includes('data-lane="a"') && html.includes('>Code<') && html.includes('>Errors<'));
  assert.ok(html.includes('class="vt-now"') && html.includes('url(#vt-hatch)'));
  assert.ok(html.includes('could not read'));
  assert.ok(!/<button/.test(html), 'no controls on the face');
});
t('a running bar (no end) shimmers; a graph lane links a child to its parent', () => {
  const bar = T.registerLane({ lane: 'g1', type: 'bar' }), gr = T.registerLane({ lane: 'g2', type: 'graph' });
  const html = T.render({ width: 1000, range: { from: 0, to: 7200e3, now: 3600e3 }, lanes: [
    { spec: bar, items: [{ t: 1000e3, end: null, label: 'gate', color: '#e0a458' }], index: 0 },
    { spec: gr, items: [{ id: 'c1', t: 100e3, track: 'edge', color: '#7aa2f7' }, { id: 'c2', t: 900e3, track: 'feat', parent: 'c1', color: '#e0a458' }], index: 1 }] });
  assert.ok(html.includes('class="vt-run"')); assert.ok(/<path d="M [\d.]+ [\d.]+ C/.test(html));
});
t('map.where keeps one source to its own lane; a colour can come from the row', () => {
  const s = T.registerLane({ lane: 'rel', type: 'mark', map: { items: 'events', t: 'start', where: { source: ['releases'] }, color: { field: 'color' } } });
  const items = T.itemsOf(s, { events: [{ start: 1, source: 'releases', color: '#4ade80' }, { start: 2, source: 'loop-lab', color: '#fff' }] });
  assert.strictEqual(items.length, 1); assert.strictEqual(items[0].color, '#4ade80');
});
t('only items overlapping the range are drawn; a run that started before it but runs into it is', () => {
  const s = T.registerLane({ lane: 'v', type: 'bar' }), sc = T.scale({ from: 1000, to: 2000 }, 1000);
  const vis = T.visible([{ t: 100, end: 500 }, { t: 800, end: 1200 }, { t: 1500, end: null }, { t: 2500, end: 2600 }], sc);
  assert.deepStrictEqual(vis.map((i) => i.t), [800, 1500]);
});
if (fails) { console.log(fails + ' FAILED'); process.exit(1); } else console.log('all passed');
