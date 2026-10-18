// THE drill panel, live strip and states (looplab-control §9 K3, 18 Oct 2026): the pure half runs in Node.
const assert = require('assert');
const path = require('path');
const { VeraDrill: D, VeraLive: L, VeraStates: S } = require(path.join(__dirname, '..', 'vera', 'ui', 'vera_kit.js'));
let fails = 0;
function t(name, fn) { try { fn(); console.log('ok  ', name); } catch (e) { fails++; console.log('FAIL', name, '-', e.message); } }

t('five states, one look; an unknown kind falls back to empty', () => {
  assert.deepStrictEqual(S.kinds(), ['loading', 'empty', 'error', 'stale', 'broken']);
  assert.ok(S.html('error', 'boom <b>').includes('vk-error') && S.html('error', 'boom <b>').includes('boom &lt;b&gt;'));
  assert.ok(S.html('loading').includes('vk-spin'));
  assert.ok(S.html('nope').includes('vk-empty'));
});
t('stale is more than two intervals old', () => {
  assert.strictEqual(S.stale(121, 60), true); assert.strictEqual(S.stale(100, 60), false);
});
t('a drill path round-trips through its encoding, ids with odd characters too', () => {
  const p = [{ kind: 'schedule', id: 'bc6f6c1f8c', tab: 'runs' }, { kind: 'branch', id: 'feat/k3 drill', tab: '' }];
  const e = D.encode(p);
  assert.strictEqual(e.split('/')[0], 'schedule:bc6f6c1f8c@runs');
  assert.deepStrictEqual(D.decode(e), p);
  assert.deepStrictEqual(D.decode(''), []); assert.deepStrictEqual(D.decode('bad'), []);
});
t('the drill rides in the panel hash beside the page and its other keys', () => {
  const h = D.hashSet('#overview?run=abc', 'drill', 'schedule:x@runs');
  assert.strictEqual(h, '#overview?run=abc&drill=schedule:x@runs');
  assert.strictEqual(D.hashGet(h, 'drill'), 'schedule:x@runs');
  assert.strictEqual(D.hashGet(h, 'run'), 'abc');
  assert.strictEqual(D.hashSet(h, 'drill', ''), '#overview?run=abc');
  assert.strictEqual(D.hashSet('#ship', 'drill', 'a:b'), '#ship?drill=a:b');
  // an id holding ':' or '/' survives the hash once - not double-encoded, not split
  const p = [{ kind: 'event', id: 'run:cc6c', tab: '' }, { kind: 'branch', id: 'feat/x', tab: 'runs' }];
  const h2 = D.hashSet('#overview', 'drill', D.encode(p));
  assert.ok(!h2.includes('%25'), h2);
  assert.deepStrictEqual(D.decode(D.hashGet(h2, 'drill')), p);
});
t('a kind is a record: render is required, tabs may be a function of the record', () => {
  assert.strictEqual(D.registerKind('x y', { render: () => '' }), null);
  assert.strictEqual(D.registerKind('nobody', {}), null);
  D.registerKind('schedule', { label: 'Schedule', render: () => 'ok', tabs: (rec) => rec && rec.runs ? [{ id: 'detail' }, { id: 'runs' }] : [{ id: 'detail' }] });
  assert.strictEqual(D.kind('schedule').label, 'Schedule');
  assert.strictEqual(D.tabsOf('schedule', { runs: 1 }).length, 2); assert.strictEqual(D.tabsOf('schedule', {}).length, 1);
  assert.strictEqual(D.kind('schedule').title({ title: 'Census' }, 'id1'), 'Census');
  assert.strictEqual(D.kind('schedule').title(null, 'id1'), 'id1');
});
const REC = { live: 'runs', label: 'Runs', source: { cap: 'evolve.schedule.events', args: {} },
              map: { items: 'events', where: { running: true }, id: 'id', label: 'title', sub: '{kind}', started: 'start', progress: { done: 'done', total: 'tasks' }, drill: 'event' } };
t('a live record is checked', () => {
  assert.deepStrictEqual(L.problems(REC), []);
  assert.strictEqual(L.problems({}).length, 2);
});
t('live items: only what matches, elapsed from the start, progress from done/total', () => {
  const now = Date.parse('2026-10-18T12:10:00Z');
  const items = L.itemsOf(REC, { events: [{ id: 'a', title: 'Census', kind: 'census', running: true, start: '2026-10-18T12:00:00Z', done: 3, tasks: 12 },
                                          { id: 'b', title: 'old', running: false }] }, now);
  assert.strictEqual(items.length, 1);
  assert.strictEqual(items[0].elapsed, 600); assert.strictEqual(items[0].progress, 0.25); assert.strictEqual(items[0].sub, 'census');
  assert.strictEqual(items[0].drill, 'event');
  const pct = L.itemsOf({ live: 'g', source: { cap: 'x' }, map: { progress: 'pct' } }, [{ pct: 40 }])[0];
  assert.strictEqual(pct.progress, 0.4); assert.strictEqual(pct.label, 'g');
});
t('the strip draws a chip per item, a clock and a bar; nothing running says so', () => {
  const html = L.render([{ label: 'Census', sub: 'census', elapsed: 600, progress: 0.25, color: '#c9a35a' }]);
  assert.ok(html.includes('vk-chip') && html.includes('10m') && html.includes('width:25%') && html.includes('--c:#c9a35a'));
  assert.ok(L.render([], { none: 'quiet' }).includes('quiet'));
  assert.ok(!/<button/.test(html), 'chips are the selection; no buttons on the strip');
});
if (fails) { console.log(fails + ' FAILED'); process.exit(1); } else console.log('all passed');
