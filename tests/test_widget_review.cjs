// The widget review (2026-09-27): the map words that turn a list into an infographic, the faces that fit their tile,
// a read that never answers, the sample face that no longer covers its content, and the overview's new bands.
//   node tests/test_widget_review.cjs
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const R = path.join(__dirname, '..');
const WE = fs.readFileSync(path.join(R, 'vera', 'widgets', 'widget_element.js'), 'utf8');
const VD = fs.readFileSync(path.join(R, 'vera', 'chat', 'vera-dashboard.js'), 'utf8');
const defined = {};
const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(fs.readFileSync(path.join(R, 'vera', 'ui', 'iso.js'), 'utf8'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
const M = (map, shape, data) => JSON.parse(JSON.stringify(W.applyMap(data, map, shape)));
const text = (h) => h.replace(/<style[\s\S]*?<\/style>/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
const draw = (form, data, map, o) => W.draw(form, data, (o && o.size) || 'm', Object.assign({ bare: true, sample: false, height: 90, width: 480, record: { form, read: { map: map || {} }, draw: (o && o.draw) || {} }, draw: (o && o.draw) || {} }, o || {}));

// ── the map words ──
const pipes = { pipelines: [{ id: 'a1', branch: 'fix/x', decision: 'promoted' }, { id: 'a2', branch: 'fix/y', decision: 'promoted' }, { id: 'a3', branch: 'feat/z', decision: 'held' }, { id: 'a4', branch: 'feat/q' }] };
t('count: a list counted by a field, largest first (pipelines by decision); a row without the field counts as (none)', JSON.stringify(M({ parts: 'pipelines', count: 'decision' }, 'parts', pipes)) === JSON.stringify({ promoted: 2, held: 1, '(none)': 1 }));
t('count + sum: a field added up per group (calls per caller)', JSON.stringify(M({ values: 'sessions', count: 'actor', sum: 'count' }, 'values', { sessions: [{ actor: 'a', count: 3 }, { actor: 'b', count: 1 }, { actor: 'a', count: 4 }] })) === JSON.stringify({ a: 7, b: 1 }));
t('count on an items form gives rows of {name, value}', JSON.stringify(M({ rows: 'pipelines', count: 'decision' }, 'items', pipes)[0]) === JSON.stringify({ name: 'promoted', value: 2 }));
t('count + span: rows counted per day, oldest first (dreams per day)', JSON.stringify(M({ values: 'h', count: 'at', span: 'day' }, 'values', { h: [{ at: '2026-09-27T10:00' }, { at: '2026-09-25T09:00' }, { at: '2026-09-27T11:00' }] })) === JSON.stringify({ '2026-09-25': 1, '2026-09-27': 2 }));
const act = { buckets: [{ hour: 'h1', pass: 2, fail: 0 }, { hour: 'h2', pass: 5, fail: 1 }] };
const sp = M({ series: 'buckets', split: ['pass', 'fail'], t: 'hour' }, 'series', act);
t('split: one series per named field, stacked by the area (test runs per hour)', sp.pass.length === 2 && sp.fail[1].v === 1 && sp.pass[1].t === 'h2' && /vb-area/.test(draw('area', act, { series: 'buckets', split: ['pass', 'fail'], t: 'hour' })));
t('reverse: a newest-first list drawn oldest-first', JSON.stringify(M({ series: 'runs', v: 'passed', reverse: true }, 'series', { runs: [{ passed: 3 }, { passed: 2 }, { passed: 1 }] }).map((p) => p.v)) === '[1,2,3]');
const ent = M({ values: '$', entries: 'status', keys: ['proxmox', 'mesh'] }, 'values', { proxmox: 'ok', docker: 'ok', mesh: 'err', ts: 'x' });
t('entries: a dict of plain values as rows, keys keeps and orders them (a health summary as pills)', JSON.stringify(ent) === JSON.stringify([{ name: 'proxmox', status: 'ok' }, { name: 'mesh', status: 'err' }]));
const cl = { workers: { w1: { status: 'idle', tasks_done: 3, cpu_pct: '12.5', ram_pct: '40' }, w2: { status: 'busy', tasks_done: 4, cpu_pct: '80', ram_pct: '60' }, w3: { status: 'running:x', tasks_done: 0 } } };
t('of + total: true counts the rows of a list or a dict of things', M({ of: 'workers', total: true }, 'level', cl).value === 3);
t('of + total: <field> adds the field up', M({ of: 'workers', total: 'tasks_done' }, 'level', cl).value === 7);
const busy = M({ of: 'workers', total: true, where: { status: ['busy', 'idle'] }, max: true }, 'level', cl);
t('where keeps the matching rows; max: true is the whole count (busy OF all)', busy.value === 2 && busy.max === 3);
const fr = M({ cells: 'workers', fields: ['cpu_pct', 'ram_pct'] }, 'matrix', cl);
t('fields: each row as its name and those fields, numbers written as text read as numbers', fr[0].name === 'w1' && fr[0].cpu_pct === 12.5 && fr[0].ram_pct === 40 && !('tasks_done' in fr[0]));
t('a map word is never taken for a field rename', /MAP_WORDS = new Set\(\['pick', 'keys', 'reverse', 'entries', 'count', 'sum', 'split', 'fields', 'of', 'total', 'where', 'span'\]\)/.test(WE));

// ── the faces fit their tile ──
const ev = Array.from({ length: 40 }, (_, i) => ({ ts: '2026-09-27T14:' + String(59 - i).padStart(2, '0') + ':00', type: 'k', name: 'line ' + i }));
const lg = draw('log', ev, { events: '$', t: 'ts', kind: 'type', text: 'name' });
const nLines = (lg.match(/<span class="t">/g) || []).length;
t('a log draws as many lines as its body holds (90 px: six), never sixteen squashed onto each other', nLines === 6 && /\.vb-log > span\{flex:none\}/.test(WE), nLines);
t('and the NEWEST of them whichever end the source keeps them at', /line 0\b/.test(text(lg)) && !/line 39/.test(text(lg)));
const pl = draw('pills', Array.from({ length: 30 }, (_, i) => ({ name: 'workflow-number-' + i, status: 'ok' })), {}, { height: 50, width: 300 });
t('pills fit the body and the rest become one "+ N" pill (a third row was cut in half)', /class="more"/.test(pl) && (pl.match(/<span title=/g) || []).length < 12, (pl.match(/<span title=/g) || []).length);
const rk = draw('ranked', { stats: [{ model: 'jaahas/qwen3.5-uncensored:9b', n: 5 }, { model: 'jaahas/qwen3.5-uncensored:27b', n: 9 }, { model: 'jaahas/qwen3.5-uncensored', n: 2 }] }, { values: 'stats', count: 'model', sum: 'n' }, { width: 260 });
const rkWide = draw('ranked', { stats: [{ model: 'jaahas/qwen3.5-uncensored:9b', n: 5 }, { model: 'jaahas/qwen3.5-uncensored:27b', n: 9 }] }, { values: 'stats', count: 'model', sum: 'n' }, { width: 600 });
t('ranked names are whole where they fit (2026-09-28: "cpu-247" read "...47" for no reason)', />jaahas\/qwen3\.5-uncensored:27b</.test(rkWide), text(rkWide).slice(0, 120));
t('in a narrow tile, ranked labels that share a beginning drop it, so the part that tells them apart shows (the full name stays the title)', /…qwen3\.5-uncensored:27b|…uncensored:27b|…27b/.test(rk) && /title="jaahas\/qwen3\.5-uncensored:27b"/.test(rk), text(rk).slice(0, 120));
const many = draw('ranked', Object.fromEntries(Array.from({ length: 12 }, (_, i) => ['k' + i, 20 - i])), {}, { height: 60 });
t('ranked draws the rows its body holds (60 px: three)', (many.match(/class="vb-rw"/g) || []).length === 3, (many.match(/class="vb-rw"/g) || []).length);
const ag = draw('agenda', Array.from({ length: 8 }, (_, i) => ({ when: '2026-09-27T0' + i + ':00', title: 'b' + i })), {}, { height: 70 });
t('an agenda draws the bookings its body holds (70 px: two)', (ag.match(/class="vb-ag/g) || []).length === 2, (ag.match(/class="vb-ag/g) || []).length);
const tm = draw('terminal', { lines: Array.from({ length: 30 }, (_, i) => 'line ' + i) }, {}, { height: 120 });
t('a terminal draws the lines its body holds, the last of them', /line 29/.test(tm) && !/line 20\b/.test(tm));
const nb = draw('numbers', { a: 1, b: 2, c: 3, d: 4 }, {}, { width: 480 }), nb2 = draw('numbers', { a: 1, b: 2, c: 3, d: 4 }, {}, { width: 240 });
t('four figures stand in one row where the body is wide enough, else two a row, smaller', /repeat\(4,1fr\)/.test(nb) && /tworow/.test(nb2) && /repeat\(2,1fr\)/.test(nb2));
t('XL composes its table only where the body has room for it', /opts\.height && opts\.height < 220\)\) return '<div class="vw-l">/.test(WE));
t('a boolean status has a colour (a test lane that is ok, a workflow that is off)', /\|true\|yes\|active\|enabled\|promoted\|adopted\|success\)\$/.test(WE) && /\|false\|no\|pending\|planned\)\$/.test(WE));
t('byte counts in a composite row read as bytes', /91\.5 GB/.test(W.draw('rows', [{ name: 'bpool', value: 98198093824 }], 's', { draw: { bytes: true }, record: { form: 'rows', draw: { bytes: true } } })));

// ── a read that never answers; the sample face ──
t('every read has a deadline; past it the batch fails and the next refresh asks again (a dead promise held its source forever)', WE.includes('const DEADLINE_MS = { fast: 25000, slow: 60000 };') && WE.includes("catch (e) { items.forEach((it) => it.no(e)); return; }") && /withDeadline\(DEADLINE_MS\[isSlow\(name\) \? 'slow' : 'fast'\]/.test(WE));
t('a read that failed before it ever answered is asked again soon', WE.includes('_retrySoon(fn) {') && WE.includes('if (this._data === undefined) this._retrySoon(() => this.read());') && WE.includes('this._retrySoon(() => this._readKids())'));
t('the sample tag sits in its own line (it covered the first value) and a bare host draws none', /\.vw-sampled\{position:relative;width:100%;min-width:0;display:flex;flex-direction:column\}\.vw-sampled > \.vw-sampletag\{display:block;align-self:flex-end/.test(WE) && !/vw-sampletag\{position:absolute/.test(WE) && !/vw-sampletag/.test(W.draw('trace', null, 'm', { sampleTag: false })) && /vw-sampletag/.test(W.draw('trace', null, 'm')));
t('the face says its state on the host (reading · failed · sample · live) and the dashboard head says it', WE.includes("if (this.getAttribute('data-state') !== state) this.setAttribute('data-state', state);") && VD.includes("grid.addEventListener('widget:rendered', function (e) {") && VD.includes("var sw = sampled ? 'sample' : (ws === 'reading' ? 'reading…' : (ws === 'failed' ? 'read failed' : ''));"));
const pend = W.draw('composite', null, 'm', { bare: true, height: 120, width: 400, kids: { a: { __pending: true } }, record: { form: 'composite', layout: 'report', children: [{ slot: 'a', record: { form: 'kv', source: 'dream.director.status', title: 'director' } }] } });
t('a composite child still reading says "reading", never the sample\'s values ("serving · ct126 · qwen3:30b")', /vw-kread/.test(pend) && !/qwen3:30b|ct126/.test(pend), text(pend));
t('the element reads the review\'s sources on its own', ['evolve.activity', 'evolve.tasks.overview', 'activity.sessions', 'syslog.errors', 'dream.sensor.cap_calls', 'evolve.unittest.history', 'evolve.audit.list', 'cap_ontology.stats', 'dash.health.summary', 'perf.stalls', 'obs.modules', 'obs.cluster', 'jobs.stats', 'dream.trigger.list', 'dream.whitelist.list', 'dream.history', 'dream.last', 'dream.scheduler.status'].every((c) => W.readable(c)), ['evolve.activity', 'activity.sessions'].filter((c) => !W.readable(c)).join(' '));

// ── the overview's new bands ──
const main = JSON.parse(fs.readFileSync(path.join(R, 'vera', 'widgets', 'layouts', 'main.json'), 'utf8'));
const recs = {}; main.widgets.forEach((w) => { if (w.record && typeof w.record === 'object') recs[w.record.id] = w; });
t('the overview has the Loop Lab and Capabilities bands and a third Now row', recs['sec-looplab'] && recs['sec-caps'] && ['subsystems', 'loop-stalls', 'warnings'].every((id) => recs[id] && recs[id].at[1] > recs['sec-now'].at[1] && recs[id].at[1] < recs['sec-inference'].at[1]));
const NEWIDS = ['subsystems', 'loop-stalls', 'warnings', 'lab-runs', 'lab-decisions', 'lab-suite', 'lab-lanes', 'lab-actions', 'cap-modules', 'cap-called', 'cap-callers', 'cap-latency', 'cap-count', 'cap-ontology', 'cap-stream',
  'agent-programmes', 'agent-runs', 'agent-goals'];
t('every new tile is a record over a readable source, drawn by the element, opening its place', NEWIDS.every((id) => { const r = recs[id] && recs[id].record; return r && r.draw.body === 'record' && W.readable(r.source) && r.open; }), NEWIDS.filter((id) => !(recs[id] && W.readable(recs[id].record.source))).join(' '));
// each new record, drawn at its own size against an answer shaped like its source's (taken from prod, trimmed)
const FIX = {
  'dash.health.summary': { proxmox: 'ok', docker: 'ok', ollama: 'ok', mesh: 'err', loop_lab: 'err', sandboxes: 'ok', mimic: 'ok', fabric: 'ok', redis: 'ok', ts: '2026-09-27T14:37:21Z' },
  'perf.stalls': { events: [{ kind: 'stall', ts: '2026-09-27T14:25:36Z', stalled_ms: 605 }, { kind: 'gc', ts: '2026-09-27T14:20:36Z', stalled_ms: 819 }], stalls: 12, hangs: 10, worst_ms: 1422 },
  'syslog.errors': { errors: [], warnings: [{ ts: '2026-09-27T14:37:00Z', cap_group: 'docker', message: 'docker.ps returned error: HTTP 502' }] },
  'evolve.activity': act,
  'evolve.pipeline.list': pipes,
  'evolve.unittest.history': { runs: [{ ts: 't2', passed: 5188 }, { ts: 't1', passed: 5180 }], lanes: [{ branch: 'fix/x', ok: true, passed: 5414 }], trend: { passed: 5188, failed: 1, green_streak: 0 } },
  'evolve.audit.list': { audit: [{ action: 'sandbox.exec' }, { action: 'pipeline.promote' }, { action: 'sandbox.exec' }] },
  'obs.modules': { modules: [{ name: 'dream_capabilities', caps_added: 153 }, { name: 'evolve_capabilities', caps_added: 129 }] },
  'dream.sensor.cap_calls': { sample: [{ name: 'obs.health', ts: '2026-09-27T13:29:40Z', elapsed_ms: 8 }, { name: 'docker.ps', ts: '2026-09-27T13:29:39Z', elapsed_ms: 111 }, { name: 'obs.health', ts: '2026-09-27T13:29:38Z', elapsed_ms: 3 }] },
  'activity.sessions': { sessions: [{ actor: 'unknown', count: 449 }, { actor: 'agent:claude-code', count: 11 }] },
  'obs.health': { caps: 2554, workers: 2 },
  'cap_ontology.stats': { covered_caps: 87, total_caps: 2554 },
  'jobs.stats': { stream: { length: 34, pending_total: 0, consumer_count: 1 }, stats: { total_orphan_reclaimed: 2 } },
  'loops.program.list': { programs: [{ name: 'wifi-pos-fix', status: 'active', loops: [{}, {}, {}] }, { name: 'MarketDataIngestion', status: 'done', loops: [{}] }] },
  'evolve.runs': { runs: [{ ts: '2026-09-27T01:32:24Z' }, { ts: '2026-09-26T07:50:00Z' }, { ts: '2026-09-26T07:47:00Z' }] },
  'goals.list': { goals: [{ name: 'earn income', status: 'active' }] } };
const sizeH = { s: 24, m: 90, l: 90, xl: 90 };
NEWIDS.forEach((id) => { const w = recs[id], r = w.record; const d = FIX[r.source];
  let h;
  if (r.form === 'composite') { const kids = {}; r.children.forEach((c) => { kids[c.slot] = { __read: true, data: W.pick(d, c.record.source.replace(/^\$subject\.?/, '')), err: '' }; }); h = W.draw('composite', d, r.frame.size, { bare: true, height: 90, width: 480, record: r, kids }); }
  else h = W.draw(r.form, d, r.frame.size, { bare: true, sample: false, height: sizeH[r.frame.size], width: w.span[0] * 125, record: r, draw: r.draw });
  t(id + ' draws its source (' + r.form + ' · ' + r.source + ')', !/wempty|vw-nodata|data-sample/.test(h) && text(h).length > 0 || /<svg|vb-(area|trace|column|tmap|donut)/.test(h), text(h).slice(0, 100)); });
t('the looplab report names a pipeline by its branch and a sandbox by its state; backups read their schedule and bytes; routing adds a model\'s rows up',
  JSON.stringify(recs.looplab.record.children.find((c) => c.slot === 'c').record.read.map) === JSON.stringify({ rows: 'pipelines', name: 'branch', status: 'decision' })
  && recs.looplab.record.children.find((c) => c.slot === 'b').record.read.map.status === 'running'
  && recs.backups.record.children.find((c) => c.slot === 'b').record.read.map.value === 'schedule' && recs.backups.record.children.find((c) => c.slot === 'c').record.draw.bytes === true
  && JSON.stringify(recs.routing.record.read.map) === JSON.stringify({ values: 'stats', count: 'model', sum: 'n' }));
['main-compute', 'main-inference'].forEach((k) => { const j = JSON.parse(fs.readFileSync(path.join(R, 'vera', 'widgets', 'layouts', k + '.json'), 'utf8')); const ids = j.widgets.map((w) => w.record.id);
  t(k + ' carries the new band cut from the overview', k === 'main-compute' ? ids.includes('sec-looplab') && ids.includes('lab-runs') : ids.includes('sec-caps') && ids.includes('cap-modules')); });
console.log((fails ? 'FAILED ' : 'passed ') + (fails ? fails + ' check(s)' : 'all checks'));
process.exit(fails ? 1 : 0);
