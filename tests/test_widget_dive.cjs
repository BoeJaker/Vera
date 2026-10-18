// The deep dive's eight improvements (looplab-control K5, the canvas's Deep dive board): the pure half runs in Node.
//   node tests/test_widget_dive.cjs
const assert = require('assert'), fs = require('fs'), path = require('path');
const D = require(path.join(__dirname, '..', 'vera', 'widgets', 'widget_dive.js'));
const EL = fs.readFileSync(path.join(__dirname, '..', 'vera', 'widgets', 'widget_element.js'), 'utf8');
const UI = fs.readFileSync(path.join(__dirname, '..', 'vera', 'ui builder', 'ui_capabilities.py'), 'utf8');
let fails = 0;
function t(name, fn) { try { fn(); console.log('ok  ', name); } catch (e) { fails++; console.log('FAIL', name, '-', e.message); } }

const ROWS = [{ id: 'a', ts: '2026-10-18T10:00:00Z', v: 1 }, { id: 'b', ts: '2026-10-18T12:00:00Z', v: 2 }, { id: 'c', ts: '2026-10-18T14:00:00Z', v: 3 }];
t('the rows\' own time field is found; numbers that are not dates are not one', () => {
  assert.strictEqual(D.timeField(ROWS), 'ts');
  assert.strictEqual(D.timeField([{ n: 5 }, { n: 7 }, { n: 9 }]), '');
  assert.strictEqual(D.timeField([{ started_at: 1760781600, x: 1 }, { started_at: 1760785200, x: 2 }]), 'started_at', 'epoch seconds under a time name');
  assert.strictEqual(D.timeField([{ ts: 'x' }]), '', 'one row is not a series');
});
t('2 brush: only the rows in the range; bins count them over the span', () => {
  const r = D.brushRows(ROWS, 'ts', Date.parse('2026-10-18T11:00:00Z'), Date.parse('2026-10-18T15:00:00Z'));
  assert.deepStrictEqual(r.map((x) => x.id), ['b', 'c']);
  assert.strictEqual(D.brushRows(ROWS, 'ts', null, null).length, 3);
  const b = D.bins(ROWS, 'ts', 4); assert.strictEqual(b.counts.reduce((a, c) => a + c, 0), 3); assert.strictEqual(b.from, Date.parse(ROWS[0].ts));
});
t('6 diff over time: added, gone, changed and the same, by each row\'s identity', () => {
  const next = [{ id: 'a', ts: ROWS[0].ts, v: 1 }, { id: 'b', ts: ROWS[1].ts, v: 9 }, { id: 'd', ts: '2026-10-18T16:00:00Z', v: 4 }];
  const d = D.diffAnswers(ROWS, next);
  assert.deepStrictEqual([d.added.length, d.removed.length, d.changed.length, d.same], [1, 1, 1, 1]);
  assert.strictEqual(d.added[0].id, 'd'); assert.strictEqual(d.removed[0].id, 'c'); assert.strictEqual(d.changed[0].after.v, 9);
  assert.strictEqual(D.rowKey({ name: 'x', v: 1 }), 'name:x'); assert.ok(D.rowKey({ v: 1 }).startsWith('j:'));
});
t('3 related records: templates reading the same family, the closest first, never itself', () => {
  const list = [{ id: 'self', name: 'Lane', reads: { cap: 'evolve.release.lane' } }, { id: 'a', name: 'Releases', reads: { cap: 'evolve.releases.view' } },
    { id: 'b', name: 'Release status', reads: { cap: 'evolve.release.status' } }, { id: 'c', name: 'Models', reads: { cap: 'ollama.route_stats' } }, { id: 'd', name: 'Panel', form: 'panel' }];
  const r = D.relatedTemplates(list, { id: 'self', source: 'evolve.release.lane' });
  assert.deepStrictEqual(r.map((x) => x.id), ['b', 'a']);
  assert.deepStrictEqual(D.relatedTemplates(list, { form: 'table' }), [], 'a record that reads nothing has no family');
});
t('7 notes are kept per record', () => {
  assert.notStrictEqual(D.noteKey({ id: 'x' }), D.noteKey({ id: 'y' }));
  assert.ok(D.noteKey({ source: 'a.b', title: 'T' }).includes('a.b'));
});
t('the dive hands VeraDive the open sheet; a row opens the drawer; a second read is diffed', () => {
  assert.ok(EL.includes("window.VeraDive.enhance({ sc, bd, rec, data, rows: (d) => diveRows(rec, d), setRows,"));
  assert.ok(EL.includes("tw.setAttribute('item-drawer', '');"), '5 row drill');
  assert.ok(EL.includes('fill(v); if (dx) dx.answered(v);'), '6 the read again is handed over');
  assert.ok(UI.includes('for name in ("present.js", "widget_dive.js"):'), 'served with widget_element.js');
});
t('8 keyboard: every key is listed on the sheet and typing in a field is never taken', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'widgets', 'widget_dive.js'), 'utf8');
  ['r', 'l', 'n', '/', 'b'].forEach((k) => assert.ok(src.includes("e.key === '" + k + "'"), k));
  assert.ok(src.includes('/^(INPUT|TEXTAREA|SELECT)$/.test(tg.tagName)'));
});
if (fails) { console.log(fails + ' FAILED'); process.exit(1); } else console.log('all passed');
