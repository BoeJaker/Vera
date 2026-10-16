// DU-K2: the no-loss gate for the canvas ELEMENTS (ESTATE-UI-REVIEW §7, Appendix K.2). Every element body is drawn from a
// realistic fixture, at the card size and at max, and what it draws is snapshotted: its controls (data-act), its data
// hooks (data-* names), its structure (vc-* classes) and the words it must show. An element upgrade may ADD any of these;
// one that DROPS one fails here - name it in the commit, with the owner's say-so, and re-snapshot.
//   node tests/test_element_keeps.cjs            check against tests/fixtures/element_keeps.json
//   node tests/test_element_keeps.cjs --write    (re)write the snapshot (after an intended change only)
// The `app` element (DA-05b, the app window) is the spine's and is still being built: not snapshotted here yet.
const fs = require('node:fs'), path = require('node:path');
const root = path.join(__dirname, '..');
const SNAP = path.join(__dirname, 'fixtures', 'element_keeps.json');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
// a minimal document: several bodies decide their controls only where there is one (a diagram's Copy / Source / Open)
const stub = () => ({ style: {}, dataset: {}, setAttribute() {}, appendChild() {}, classList: { add() {}, remove() {}, contains() { return false; } }, addEventListener() {} });
globalThis.document = { createElement: stub, querySelector: () => null, querySelectorAll: () => [], head: stub(), body: stub(), addEventListener() {} };
const mod = require(path.join(root, 'vera', 'canvas', 'canvas_element.js'));
t('the canvas module exposes its element bodies', !!(mod && mod.BLOCK));
if (!mod || !mod.BLOCK) { console.log('1 FAILED'); process.exit(1); }

const rows = (n, f) => Array.from({ length: n }, (_, i) => f(i));
const LONG_MD = '# Vector databases in 2026\n\nA short survey.\n\n## Options\n\n- **pgvector**: inside Postgres\n- **Qdrant**: a server\n\n## Numbers\n\n| db | p95 ms |\n|---|---|\n| pgvector | 12 |\n| qdrant | 8 |\n\n```python\nprint("hello")\n```\n';
const FX = {
  markdown: { content: { title: 'Survey', md: LONG_MD }, words: ['Vector databases', 'pgvector', 'Qdrant'] },
  calendar: { content: { month: '2026-09', events: [{ id: 'a', title: 'Standup', start: '2026-09-15T09:30:00', end: '2026-09-15T09:45:00' }, { id: 'b', title: 'Dentist', start: '2026-09-30T11:00:00' }] }, words: ['Standup'] },
  timeline: { content: { events: [{ when: '1984-06-01', label: 'The first release' }, { when: '1999-03-12', label: 'Version two shipped' }, { when: '2026-10-12', label: 'Census started' }] }, words: ['The first release', 'Census started'] },
  source: { content: { url: 'https://qdrant.tech/blog/x', domain: 'qdrant.tech', title: 'Qdrant 1.9', chars: 5400, snippet: 'What is new in Qdrant 1.9', text: 'Qdrant 1.9 adds ' + 'words '.repeat(60) }, words: ['Qdrant 1.9'] },
  code: { content: { title: 'probe.py', lang: 'python', filename: 'probe.py', path: 'tools/probe.py', code: 'def f(x):\n    return x * 2\n\nprint(f(21))\n' }, words: ['probe.py'] },
  explode: { content: { path: 'tools/probe.py', code: 'def f(x):\n    return x * 2\n' }, words: [] },
  diagram: { content: { title: 'Flow', mermaid: 'graph TD\n  A[Ask] --> B[Search]\n  B --> C[Answer]\n', caption: 'the lookup' }, words: ['Search', 'the lookup'] },   // in node (no DOM) the body is its source
  image: { content: { url: 'https://x.example/render.png', alt: 'render 04', caption: 'render 04', title: 'render' }, words: ['render 04'] },
  note: { content: { text: 'Remember the gate before promote', author: 'owner' }, words: ['Remember the gate'] },
  records: { content: { title: 'Pages', kind: 'web', items: rows(5, (i) => ({ id: 'u' + i, url: 'https://site' + i + '.example/p', domain: 'site' + i + '.example', title: 'Page ' + i, snippet: 'what page ' + i + ' says', when: '2026-10-1' + i })) }, words: ['Page 0', 'Page 4'] },
  table: { content: { caption: 'nodes', columns: ['node', 'load', 'temp'], rows: rows(6, (i) => ['ct12' + i, String(10 * i), String(40 + i)]) }, words: ['ct120', 'load'] },
  widget: { content: { title: 'CPU', widget: 'trace', record: { form: 'trace', title: 'CPU', data: rows(12, (i) => ({ t: i, v: 20 + (i % 5) * 7 })) } }, words: [] },
  session: { content: { host: 'ct126', command: 'uptime', output: ' 10:00 up 3 days, load average: 0.4', attached: false }, words: ['uptime'] },
  notebook: { content: { notebook_id: 'nb1', cell_id: 'c1', cell_type: 'code', lang: 'python', source: 'x = 1\nprint(x)', output: '1' }, words: [] },
  panel: { content: { panel: 'markets', id: 'markets', title: 'Markets', src: '/ui/panels/markets' }, words: [] },
  loop: { content: { goal: 'fix the flaky test', status: 'running', steps: [{ n: 1, kind: 'plan', text: 'read the test' }, { n: 2, kind: 'edit', text: 'patch' }] }, words: ['fix the flaky test'] },
  activity: { content: { calls: [{ cap: 'web.search', args: { q: 'qdrant' }, status: 'done', ms: 820 }, { cap: 'memory.seek', args: { q: 'qdrant' }, status: 'running' }] }, words: ['web.search'] },
  research: { content: { query: 'vector databases', status: 'done', report: LONG_MD, items: rows(3, (i) => ({ url: 'https://s' + i + '.example', title: 'Source ' + i, snippet: 's' + i })), plan: ['find', 'read', 'write'] }, words: ['Vector databases'] },
  entities: { content: { groups: [{ group: 'Organisations', entities: [{ text: 'Qdrant', type: 'org', count: 3 }, { text: 'Timescale', type: 'org' }] }, { group: 'Technology', entities: [{ text: 'pgvector', type: 'tech' }] }] }, words: ['Qdrant', 'pgvector'] },
  json: { content: { title: 'answer', data: { ok: true, nodes: [{ id: 'ct126', load: 0.4 }], meta: { n: 1 } } }, words: ['ct126'] },
  kv: { content: { fields: { host: 'ct126', cores: 16, status: 'ok', uptime: '3 days' } }, words: ['ct126', 'cores'] },
  chat: { content: { title: 'excerpt', messages: [{ role: 'user', text: 'what is pgvector?' }, { role: 'assistant', text: 'A Postgres extension.' }] }, words: ['pgvector'] },
  _many: { content: { items: [{ kind: 'note', content: { text: 'one' } }, { kind: 'note', content: { text: 'two' } }] }, words: ['one', 'two'] },
  result: { content: { cap: 'text.hash', ok: true, args: { text: 'probe' }, ms: 12, result: { hash: 'abc123', algorithm: 'sha256' } }, words: ['abc123'] },   // its head is the container's now (D1 step 4)
  schedule: { content: { when: 'every day 02:30', what: 'backup' }, words: ['backup'] },
  html: { content: { title: 'page', html: '<h1>Hello</h1><p>world</p>' }, words: [] },
};

// what a body keeps: the controls it DECLARES for the one item head (D1 step 4: declare(el, key, {sub, acts, menus})),
// the controls in its body, its data hooks, its structure, its words
const declOf = (d) => { const out = []; const walk = (x, pre) => { if (!x) return; if (Array.isArray(x)) return x.forEach((y) => walk(y, pre));
  if (typeof x === 'object') { if (x.act) out.push(pre + x.act); if (x.items) walk(x.items, pre); if (x.acts) walk(x.acts, pre); } };
  walk(d && d.acts, 'act:'); walk(d && d.menus, 'menu:'); return [...new Set(out)].sort(); };
function keepsOf(html, words, decl) {
  const s = String(html || '');
  const acts = [...new Set([...s.matchAll(/data-act="([^"]+)"/g)].map((m) => m[1]))].sort();
  const data = [...new Set([...s.matchAll(/\s(data-[a-z0-9-]+)=/g)].map((m) => m[1]))].filter((d) => d !== 'data-act').sort();
  const cls = [...new Set([...s.matchAll(/class="([^"]*)"/g)].flatMap((m) => m[1].split(/\s+/)).filter((c) => /^vc-/.test(c)))].sort();
  return { declared: declOf(decl), acts, data, cls, words: words.filter((w) => s.indexOf(w) >= 0) };
}
const kinds = Object.keys(mod.BLOCK).filter((k) => k !== 'app');
const now = {};
for (const k of kinds) {
  const fx = FX[k];
  if (!fx) { now[k] = { missing: true }; continue; }
  for (const size of ['m', 'xl']) {
    let h = ''; let err = null; const host = { dataset: {}, _live: {} };   // the item a body draws into: it declares onto it
    try { h = mod.BLOCK[k](JSON.parse(JSON.stringify(fx.content)), size, k + ':probe', host); } catch (e) { err = String(e && e.message || e); }
    now[k + '@' + size] = err ? { error: err } : Object.assign({ chars: String(h).length > 0 }, keepsOf(h, fx.words, host._decl && host._decl[k + ':probe']));
  }
}
t('every element kind has a fixture', kinds.every((k) => FX[k]), kinds.filter((k) => !FX[k]).join(','));
t('every body draws from its fixture without throwing', Object.values(now).every((v) => !v.error), Object.entries(now).filter(([, v]) => v.error).map(([k, v]) => k + ': ' + v.error).join(' | '));
t('every body shows the words its fixture must show', Object.entries(now).every(([k, v]) => !FX[k.split('@')[0]] || v.error || v.words.length === FX[k.split('@')[0]].words.length),
  Object.entries(now).filter(([k, v]) => FX[k.split('@')[0]] && !v.error && v.words.length !== FX[k.split('@')[0]].words.length).map(([k, v]) => k + ' shows ' + v.words.join('/')).join(' | '));

if (process.argv.includes('--write')) {
  fs.mkdirSync(path.dirname(SNAP), { recursive: true });
  fs.writeFileSync(SNAP, JSON.stringify(now, null, 1) + '\n');
  console.log('wrote ' + SNAP + ' (' + Object.keys(now).length + ' bodies)');
} else {
  const was = JSON.parse(fs.readFileSync(SNAP, 'utf8'));
  for (const key of Object.keys(was)) {
    const a = was[key], b = now[key] || {};
    if (!b || b.error) { t('kept: ' + key, false, 'gone or throws'); continue; }
    const lost = [];
    ['declared', 'acts', 'data', 'cls', 'words'].forEach((f) => (a[f] || []).forEach((x) => { if (!(b[f] || []).includes(x)) lost.push(f + ':' + x); }));
    t('kept: ' + key, !lost.length, 'lost ' + lost.join(', '));
  }
}
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
