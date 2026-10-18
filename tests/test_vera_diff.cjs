// THE diff viewer (looplab-control §9 K6, 18 Oct 2026): the pure half runs in Node.
const assert = require('assert');
const path = require('path');
const D = require(path.join(__dirname, '..', 'vera', 'ui', 'vera_diff.js'));
let fails = 0;
function t(name, fn) { try { fn(); console.log('ok  ', name); } catch (e) { fails++; console.log('FAIL', name, '-', e.message); } }

const DIFF = [
  'diff --git a/vera/x.py b/vera/x.py',
  'index 1..2 100644',
  '--- a/vera/x.py',
  '+++ b/vera/x.py',
  '@@ -1,4 +1,4 @@ def run():',
  ' import os',
  '-def run(limit=10):',
  '+def run(limit=20):',
  '     return os.getcwd()',
  ' # end',
  'diff --git a/vera/new.js b/vera/new.js',
  'new file mode 100644',
  '--- /dev/null',
  '+++ b/vera/new.js',
  '@@ -0,0 +1,2 @@',
  '+const a = 1;',
  '+// note',
].join('\n');

t('a unified diff parses into files, hunks and both sides\' line numbers', () => {
  const f = D.parse(DIFF);
  assert.strictEqual(f.length, 2);
  assert.strictEqual(f[0].path, 'vera/x.py'); assert.strictEqual(f[0].adds, 1); assert.strictEqual(f[0].dels, 1);
  const L = f[0].hunks[0].lines;
  assert.deepStrictEqual(L.map((l) => l.t).join(''), ' -+  ');
  assert.strictEqual(L[1].old, 2); assert.strictEqual(L[2].new, 2); assert.strictEqual(L[4].new, 4);
  assert.strictEqual(f[0].hunks[0].ctx, 'def run():');
  assert.ok(f[1].new); assert.strictEqual(f[1].hunks[0].lines[1].new, 2);
});
t('word diff marks only what changed', () => {
  const w = D.wordDiff('def run(limit=10):', 'def run(limit=20):');
  assert.deepStrictEqual(w.filter((p) => p.t !== '=').map((p) => p.t + p.s), ['-10', '+20']);
  assert.strictEqual(w.filter((p) => p.t !== '+').map((p) => p.s).join(''), 'def run(limit=10):');
});
t('syntax tokens by file type, escaped', () => {
  const h = D.tokens('def f(x): return "a<b" # hi', 'm.py');
  assert.ok(h.includes('<span class="vd-k">def</span>') && h.includes('<span class="vd-s">&quot;a&lt;b&quot;</span>') && h.includes('<span class="vd-c"># hi</span>'));
  assert.ok(D.tokens('const n = 42; // x', 'a.js').includes('<span class="vd-n">42</span>'));
  assert.strictEqual(D.tokens('<b>', 'notes.txt'), '&lt;b&gt;');
});
t('long unchanged runs fold, keeping context around each change', () => {
  const lines = [];
  for (let i = 1; i <= 20; i++) lines.push({ t: ' ', text: 'x' + i, old: i, new: i });
  lines.splice(10, 0, { t: '+', text: 'new', old: null, new: 11 });
  const f = D.fold(lines, 3);
  const folds = f.filter((l) => l.t === 'fold');
  assert.strictEqual(folds.length, 2, 'one run before the change, one after');
  assert.ok(f.some((l) => l.t === '+'));
  assert.strictEqual(f.filter((l) => l.t === ' ').length, 6, 'three either side of the change');
});
t('render: tree, hunks, word marks, lint inline on its line; split view pairs a line with its replacement', () => {
  const files = D.parse(DIFF);
  const lint = [{ path: 'vera/x.py', line: 2, code: 'E501', message: 'too long', source: 'line-checks', severity: 'info' },
                { path: 'vera/new.js', line: null, code: 'JS-PARSE', message: 'does not parse', source: 'node', severity: 'error' }];
  const html = D.render(files, { lint });
  assert.ok(html.includes('class="vd-tree"') && html.includes('vd-tl err'));
  assert.ok(html.includes('<span class="vd-w-">10</span>') && html.includes('<span class="vd-w+">20</span>'));
  assert.ok(html.includes('<b>E501</b> too long') && html.includes('<b>JS-PARSE</b>'));
  const split = D.render(files, { mode: 'split' });
  assert.ok(/vd-del">[^]*?<\/td><td class="vd-ln" data-line="2">2<\/td><td class="vd-code vd-add">/.test(split), 'removed and added on one row');
  assert.ok(D.render(files, { closed: { 'vera/x.py': true } }).split('vd-hunk').length < html.split('vd-hunk').length, 'a closed file hides its hunks');
  assert.ok(D.render([], {}).includes('no changes'));
});
t('lint summary names every check, and an absent one says so', () => {
  const s = D.lintSummary({ summary: { errors: 1, warnings: 2, info: 0 }, sources: { 'line-checks': 'ok', ruff: 'absent' } });
  assert.ok(s.includes('1 error(s)') && s.includes('ruff (absent)') && s.includes('line-checks'));
});
if (fails) { console.log(fails + ' FAILED'); process.exit(1); } else console.log('all passed');
