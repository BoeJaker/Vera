// The widget review (2026-09-27): the dream, workers/ollama and Estate-overview grids converted to records the element
// draws. A tile that stays the page's says why in its record's note; every converted tile reads a source the element
// reads on its own and draws an answer shaped like that source's real one (trimmed from prod).
//   node tests/test_dashboard_conversions.cjs
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const R = path.join(__dirname, '..');
const WE = fs.readFileSync(path.join(R, 'vera', 'widgets', 'widget_element.js'), 'utf8');
const defined = {};
const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(fs.readFileSync(path.join(R, 'vera', 'ui', 'iso.js'), 'utf8'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
const FORMS = new Set(W.forms().map((f) => f.id));
const text = (h) => h.replace(/<style[\s\S]*?<\/style>/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
const lay = (k) => JSON.parse(fs.readFileSync(path.join(R, 'vera', 'widgets', 'layouts', k + '.json'), 'utf8'));
const W2 = { status: 'idle', tasks_done: 3, cpu_pct: '33.6', ram_pct: '84.8', disk_pct: '56.7', capabilities: [] };
const FIX = {
  'dream.scheduler.status': { scheduler_running: false, enabled: false, idle_minutes: 0.05, min_idle_minutes: 60, in_cycle: false, background_loops: 0 },
  'dream.trigger.list': { triggers: [{ name: 'a', enabled: true }, { name: 'b', enabled: false }, { name: 'c', enabled: true }], count: 3 },
  'dream.history': { history: [{ label: 'Activity Summariser', signal: 0.92, started_at: '2026-07-19T20:47:15Z' }, { label: 'Thought · Boejaker', signal: 0.5, started_at: '2026-07-18T10:00:00Z' }, { label: 'Activity Summariser', signal: 0.7, started_at: '2026-07-18T09:00:00Z' }], total: 200 },
  'dream.whitelist.list': { whitelist: ['a', 'b'], count: 61 },
  'dream.last': { title: 'Active Project Status', label: 'Activity Summariser', ended_at: '2026-07-19T20:50:34Z', elapsed_s: 199.45, signal: 0.924, delivered: { notebook: 'error: cap unavailable', memory: true, telegram: true } },
  'dream.hitl.pending': { pending: [], count: 0 },
  'dream.journal.list': { ok: true, journals: [{ journal_id: 'director', entries: 2015 }, { journal_id: 'trigger:source_review', entries: 1316 }] },
  'obs.cluster': { workers: { 'worker-a': Object.assign({ id: 'worker-a' }, W2), 'worker-b': Object.assign({}, W2, { status: 'busy', tasks_done: 4 }) }, ollama: { 'gpu-250': { status: 'online', model_count: 1, in_use: 0 }, 'cpu-246': { status: 'online', model_count: 2, in_use: 1 } }, queues: { pending_tasks: 0 } },
  'obs.health': { caps: 2554, workers: 2, mcp_servers: 0, redis: true, postgres: true, chroma: true, neo4j: false, mode: 'distributed', census: { state: 'done', control: 'run', done: 12 } },
  'ollama.request_log': { entries: [{ ts: '2026-09-27T14:37:00Z', instance: 'cpu-246', model: 'nomic-embed-text' }] },
  'ollama.route_stats': { stats: [{ model: 'jaahas/qwen3.5-uncensored', n: 6029 }, { model: 'jaahas/qwen3.5-uncensored', n: 5827 }, { model: 'qwen2.5:7b', n: 805 }] },
  'jobs.stats': { stream: { length: 34, pending_total: 0 }, stats: { total_orphan_reclaimed: 2 } },
  'sysmon.status': { resources: { cpu: 46.4, mem: 84.9, proc_mb: 3570 } },
  'sysmon.history': { samples: [{ t: 1, cpu: 20, mem: 80, pmx_mem_pct: 60, pmx_running: 22, dkr_running: 65, oll_online: 3, pmx_temp_max: 81 }, { t: 2, cpu: 30, mem: 81, pmx_mem_pct: 61, pmx_running: 22, dkr_running: 66, oll_online: 3, pmx_temp_max: 82 }] },
  'background.status': { running: 'ide.claude_sessions.ingest', busy_reason: 'the GPU gate was held 58s ago', quiet_for_s: 0, min_quiet_s: 600, queue: { depth: 0, waiting: [], note: 'nothing queued' }, jobs: [{ name: 'ingest', runs: 3, last_ok: true }], timeline: [{ title: 'digest', starts_in_s: 300 }], eta_total_s: 0 },
  'backup.status': { guests: [{ name: 'LLM', status: 'running', state: 'ok' }, { name: 'Kali', status: 'stopped', state: 'excluded' }] },
  'obs.node_temps': { hosts: [{ label: 'ollama126.vera.int', max_c: 76 }, { label: 'VFS-02', max_c: 70 }] } };

// ── every grid: records, notes, the conversions ──
const GRIDS = { dream: 9, 'wol-workers': 9, 'wol-ollama': 9, 'wol-jobs': 10, 'wol-wkjobs': 8, 'wol-observe': 2, 'estate-overview': 11 };
let converted = 0, kept = 0;
Object.keys(GRIDS).forEach((k) => {
  const L = lay(k); const bad = [];
  L.widgets.forEach((w) => { const r = w.record;
    if (!r || typeof r !== 'object') { bad.push('no record'); return; }
    if (!FORMS.has(r.form)) bad.push(r.id + ' form ' + r.form);
    if (!(Array.isArray(w.span) && r.frame && JSON.stringify(r.frame.span) === JSON.stringify(w.span))) bad.push(r.id + ' span');
    if (r.draw.body === 'page') { kept++; if (!/^stays the page's: /.test(r.note || '')) bad.push(r.id + ' kept without saying why'); return; }
    converted++;
    if (!W.readable(r.source)) bad.push(r.id + ' source ' + r.source + ' is not read on its own');
    (r.children || []).forEach((c) => { const s = c.record.source; if (!/^\$subject/.test(s) && !W.readable(s)) bad.push(r.id + ':' + c.slot + ' source ' + s); });
    // the record draws its source's answer, not a sample and not an empty state
    const d = FIX[r.source]; if (!d) { bad.push(r.id + ' no fixture for ' + r.source); return; }
    let h;
    if (r.form === 'composite') { const kids = {}; (r.children || []).forEach((c) => { const s = c.record.source; kids[c.slot] = { __read: true, data: /^\$subject/.test(s) ? W.pick(d, s.replace(/^\$subject\.?/, '')) : FIX[s], err: '' }; });
      h = W.draw('composite', d, r.frame.size, { bare: true, height: 120, width: 400, record: r, kids }); if (/read · empty|read failed/.test(h)) bad.push(r.id + ' a child drew nothing: ' + text(h).slice(0, 80)); }
    else h = W.draw(r.form, d, r.frame.size, { bare: true, sample: false, height: 90, width: w.span[0] * 125, record: r, draw: r.draw });
    if (/wempty|vw-nodata|data-sample/.test(h)) bad.push(r.id + ' drew ' + text(h).slice(0, 80)); });
  t(k + ': ' + L.widgets.length + ' tiles, each a record; the page\'s own say why; the rest draw their source', !bad.length, JSON.stringify(bad.slice(0, 5)));
});
t('the review converted every grid tile a capability answers for (' + converted + ' record tiles; ' + kept + ' stay the page\'s: forms, drawer chips, the job tracker)', converted >= 30, converted + ' converted, ' + kept + ' kept');

// ── the figures say the right thing of the real shapes ──
const fig = (k, id) => { const r = lay(k).widgets.find((w) => w.record.id === id).record; return JSON.parse(JSON.stringify(W.mapped(r, r.form, FIX[r.source]))); };
t('workers: counted, busy of all ("running*" is any running status), tasks added up', fig('wol-workers', 'w-total').value === 2 && fig('wol-workers', 'w-busy').value === 1 && fig('wol-workers', 'w-busy').max === 2 && fig('wol-workers', 'w-tasks').value === 7);
t('ollama nodes online of all; models loaded; requests in flight', fig('wol-ollama', 'ol-online').value === 2 && fig('wol-ollama', 'ol-online').max === 2 && fig('wol-ollama', 'ol-models').value === 3 && fig('wol-ollama', 'ol-active').value === 1);
t('the cluster resources are a heat map of each worker\'s cpu, ram and disk, coloured by the percent itself', (() => { const m = fig('wol-workers', 'w-resources'); return m.length === 2 && m[0].cpu_pct === 33.6 && Object.keys(m[0]).join() === 'name,cpu_pct,ram_pct,disk_pct'; })());
t('dream: the triggers on of all, idle toward the threshold, dreams by trigger', fig('dream', 'triggers-kpi').value === 2 && fig('dream', 'triggers-kpi').max === 3 && fig('dream', 'idle').max === 60 && fig('dream', 'trigger-activity')['Activity Summariser'] === 2);
t('dream: dreams per day, oldest first', JSON.stringify(fig('dream', 'dream-per-day')) === JSON.stringify({ '2026-07-18': 2, '2026-07-19': 1 }));
const heat = W.draw('heat', FIX['obs.cluster'], 'l', { bare: true, height: 90, width: 700, record: lay('wol-workers').widgets.find((w) => w.record.id === 'w-resources').record, draw: { palette: 'load', total: false } });
t('the heat map\'s cells share the body\'s height, its columns are named, a wide cell says its value, no row sums', /height:(1[0-9]|2[0-6])px;aspect-ratio:auto/.test(heat) && /class="vb-heatrow hd"/.test(heat) && />cpu_pct</.test(heat) && />33\.6</.test(heat) && !/class="v">/.test(heat), text(heat).slice(0, 120));
const don = W.draw('donut', { running: 22, stopped: 34 }, 'm', { draw: { palette: 'status' } });
t('a donut of states in palette status is green for running and red for stopped', /stroke="var\(--b-ac2\)"[\s\S]*<title>running|<title>running[\s\S]*/.test(don) && /var\(--b-ac2\)/.test(don) && /var\(--b-ac4\)/.test(don));
const yes = W.draw('pills', [{ name: 'redis', status: true }, { name: 'neo4j', status: false }], 's', {});
t('a yes/no chip is its dot and its full name, not "r true"', /<b>redis<\/b>/.test(yes) && !/true<\/b>/.test(yes), text(yes));
const rows2 = W.draw('composite', null, 'm', { bare: true, height: 200, width: 700, record: { form: 'composite', layout: 'rows', children: [{ slot: 'a', record: { form: 'kv', data: { a: 1 } } }, { slot: 'b', record: { form: 'kv', data: { b: 2 } } }] } });
t('a rows composite shares its height between its slots (each took all of it)', ((rows2.match(/height:(\d+)px/g) || []).map((s) => +s.replace(/\D/g, '')).every((h) => h < 200)), (rows2.match(/height:(\d+)px/g) || []).join(','));
t('the Estate overview has a layout file of its own now, the drawer tiles kept, three new ones drawn', (() => { const L = lay('estate-overview'); const ids = L.widgets.map((w) => w.record.id); return L.key === 'estate-overview' && ['guests-state', 'backup-state', 'host-temps'].every((i) => ids.includes(i)) && L.widgets.find((w) => w.record.id === 'health').record.draw.body === 'page'; })());
console.log((fails ? 'FAILED ' : 'passed ') + (fails ? fails + ' check(s)' : 'all checks'));
process.exit(fails ? 1 : 0);
