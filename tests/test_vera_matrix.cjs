// THE matrix (looplab-control §9 K2, 18 Oct 2026): a matrix is a record; the pure half runs in Node.
const assert = require('assert');
const path = require('path');
const M = require(path.join(__dirname, '..', 'vera', 'ui', 'vera_matrix.js'));
let fails = 0;
function t(name, fn) { try { fn(); console.log('ok  ', name); } catch (e) { fails++; console.log('FAIL', name, '-', e.message); } }

const REC = { map: { cells: 'rows', row: 'goal', col: 'run', value: 'status', tip: '{goal} · {run} · {status}', cols_order: 'n' },
              colour: { done: '#3fae6c', partial: '#e0a458', failed: '#f87171', '*': '#5a6270' }, ok: ['done'] };
const ANS = { rows: [
  { goal: 'timer', run: 'r76', n: 76, status: 'done' }, { goal: 'timer', run: 'r75', n: 75, status: 'failed' },
  { goal: 'serves own file', run: 'r75', n: 75, status: 'done' }, { goal: 'serves own file', run: 'r76', n: 76, status: 'failed' },
  { goal: 'web lookup', run: 'r76', n: 76, status: 'done' }, { goal: 'no run' }] };

t('a matrix record is checked', () => {
  assert.deepStrictEqual(M.problems(REC), []);
  assert.strictEqual(M.problems({ map: {} }).length, 2);
});
t('cells come from the answer through the record; a row without its fields is dropped', () => {
  const cells = M.cellsOf(REC, ANS);
  assert.strictEqual(cells.length, 5);
  assert.strictEqual(cells[0].tip, 'timer · r76 · done');
});
t('columns follow cols_order, rows keep first-seen order; per-row pass % and per-column green counts', () => {
  const L = M.layout(REC, M.cellsOf(REC, ANS));
  assert.deepStrictEqual(L.cols, ['r75', 'r76']);
  assert.deepStrictEqual(L.rows, ['timer', 'serves own file', 'web lookup']);
  assert.strictEqual(L.rowStats.timer.pct, 50); assert.strictEqual(L.rowStats['web lookup'].pct, 100);
  assert.strictEqual(L.colStats.r75.ok, 1); assert.strictEqual(L.colStats.r76.ok, 2);
});
t('sort: failures first; window keeps the last N columns', () => {
  const L = M.layout(Object.assign({}, REC, { sort: 'failures', window: 1 }), M.cellsOf(REC, ANS));
  assert.deepStrictEqual(L.cols, ['r76']);
  assert.strictEqual(L.rows[0], 'serves own file');            // 0% in the window
});
t('group splits rows under headings', () => {
  const rec = Object.assign({}, REC, { group: 'area' });
  const L = M.layout(rec, M.cellsOf(rec, { rows: [{ goal: 'a', run: 'r1', status: 'done', area: 'ui' }, { goal: 'b', run: 'r1', status: 'done', area: 'core' }] }));
  assert.deepStrictEqual(L.groups.map((g) => g.name), ['ui', 'core']);
});
t('colours: a value map with a fallback, or a numeric scale', () => {
  assert.strictEqual(M.colourOf(REC, 'failed'), '#f87171'); assert.strictEqual(M.colourOf(REC, 'odd'), '#5a6270');
  const num = { colour: { scale: [0, 100], good: 'high' } };
  assert.strictEqual(M.colourOf(num, 100), 'rgb(47,138,85)'); assert.strictEqual(M.colourOf(num, 0), 'rgb(220,80,80)');
});
t('render draws each cell with its colour, the row % and the totals; no controls on the face', () => {
  const L = M.layout(REC, M.cellsOf(REC, ANS));
  const html = M.render(REC, L, { selected: { row: 'timer', col: 'r76' } });
  assert.ok(html.includes('data-r="timer" data-c="r76"') && html.includes('#f87171') && html.includes('>50%<'));
  assert.ok(html.includes('vm-c on')); assert.ok(!/<button/.test(html));
  assert.ok(M.render(REC, M.layout(REC, [])).includes('nothing to show'));
});
t('a numeric matrix with no ok list shows no pass % or totals', () => {
  const rec = { map: { cells: 'stats', row: 'job', col: 'node', value: 's' }, colour: { scale: [0, 100], good: 'low' } };
  const L = M.layout(rec, M.cellsOf(rec, { stats: [{ job: 'chat', node: 'gpu-250', s: 64 }] }));
  const html = M.render(rec, L);
  assert.ok(!html.includes('%<') && !html.includes('>green<'));
  assert.ok(html.includes('>gpu-250<'));                       // the full column name
});
if (fails) { console.log(fails + ' FAILED'); process.exit(1); } else console.log('all passed');
