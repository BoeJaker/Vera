// The main dashboard across the catalogue (UI redesign, Notes/42 defect 50 — "a broad array of the possible widgets and
// proper composite widgets to replace existing ones"): the layout's breadth against the real envelopes, the element's
// composite that fills its frame, the tables that fit their rows, the ring's own dial, a level's unit, the staged OK
// around widget.validate, and the dashboard's fixed rows / gesture hold / checking chip — in the text and in node.
//   node tests/test_dashboard_records_array.cjs
'use strict';
const fs = require('fs'); const path = require('path'); const vm = require('vm'); const assert = require('assert');
const ROOT = path.join(__dirname, '..');
const DASH = fs.readFileSync(path.join(ROOT, 'vera', 'chat', 'vera-dashboard.js'), 'utf8');
const EL = fs.readFileSync(path.join(ROOT, 'vera', 'widgets', 'widget_element.js'), 'utf8');
const LAY = JSON.parse(fs.readFileSync(path.join(ROOT, 'vera', 'widgets', 'layouts', 'main.json'), 'utf8'));
const PAGE = fs.readFileSync(path.join(ROOT, 'vera', 'capability_orchestration.html'), 'utf8');
let n = 0; const T = (name, fn) => { try { fn(); n++; console.log('PASS', name); } catch (e) { console.log('FAIL', name, '\n  ', e && e.stack || e); process.exitCode = 1; } };

// the element in node: window.VeraWidget without a DOM
function element() {
  const stub = () => ({ style: {}, dataset: {}, setAttribute() {}, getAttribute() { return null; }, appendChild() {}, querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, textContent: '' });
  const window = { addEventListener() {}, location: { hash: '' }, customElements: { get() { return null; }, define() {} }, HTMLElement: class {}, CustomEvent: class {}, ResizeObserver: class { observe() {} disconnect() {} } };
  const document = Object.assign(stub(), { head: stub(), body: stub(), createElement: () => stub(), documentElement: stub() });
  const ctx = { window, document, console, setTimeout, clearTimeout, setInterval, clearInterval, fetch: () => Promise.reject(new Error('no net')), HTMLElement: window.HTMLElement, CustomEvent: window.CustomEvent, customElements: window.customElements };
  vm.createContext(ctx); vm.runInContext(EL, ctx, { filename: 'widget_element.js' });
  return ctx.window.VeraWidget;
}
const VW = element();
const recs = {}; LAY.widgets.forEach((t) => { recs[t.record.id] = t.record; });
const strip = (h) => String(h).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

T('the layout: every tile but the topology a record; the breadth of forms; the record-only tiles the page has no markup for', () => {
  const rec = LAY.widgets.filter((t) => t.record.draw.body === 'record'), page = LAY.widgets.filter((t) => t.record.draw.body === 'page');
  assert.strictEqual(rec.length, 25); assert.deepStrictEqual(page.map((t) => t.record.id), ['topology-map']); assert(recs['topology-map'].note);
  const forms = new Set(); rec.forEach((t) => { forms.add(t.record.form); (t.record.children || []).forEach((c) => forms.add(c.record.form)); });
  assert(forms.size >= 15, 'forms: ' + Array.from(forms).join(' '));
  for (const f of ['composite', 'counter', 'hero', 'pills', 'ring', 'meter', 'trace', 'rows', 'table', 'cards', 'feed', 'kv', 'temps', 'string', 'terminal']) assert(forms.has(f), f);
  for (const id of ['events', 'health']) { assert(!PAGE.includes('data-wid="' + id + '"'), id + ' has no markup'); assert.strictEqual(recs[id].draw.body, 'record'); }
  assert.strictEqual(recs.events.form, 'feed'); assert.strictEqual(recs.events.source, 'obs.events');
  // the KPI tiles are the board's smallest tile, 2 × 2, in the page and the file alike; a three-child composite has four rows
  for (const id of ['status', 'mode', 'redis', 'workers', 'caps', 'pending']) { assert(PAGE.includes('<div class="widget w-w2 w-h2" data-wid="' + id + '">'), id); assert.deepStrictEqual(recs[id].frame.span, [2, 2]); }
  for (const id of ['mesh-info', 'looplab-info', 'connections']) { assert(PAGE.includes('<div class="widget w-w4 w-h4" data-wid="' + id + '">'), id); assert.deepStrictEqual(recs[id].frame.span, [4, 4]); }
  assert(PAGE.includes('<div class="widget w-w6 w-h4" data-wid="scheduler">'));
});
T('the records draw from the envelopes their sources really answer (a fixture of each, as the mirror gave them)', () => {
  const health = { redis: true, postgres: true, chroma: false, neo4j: true, workers: 1, caps: 2408, mode: 'distributed' };
  const memory = { backends: { postgres: { connected: true, total: 344047 }, chroma: { connected: false }, neo4j: { connected: true, nodes: 338493, relationships: 4121227, sessions: 9287 } } };
  const sysmon = { proxmox: { ok: false, clusters: [], configured: 0, nodes: 0, guests: 0, running: 0, mem_used_gb: 0, mem_total_gb: 0 }, docker: { ok: false, hosts: [{ id: 'local', label: 'local', reachable: false, containers: 0, running: 0 }], total_hosts: 1, reachable: 0, running: 0, containers: 0 },
    ollama: { ok: true, online: 3, total: 3, gpu: 1, in_use: 2, nodes: [{ id: 'cpu-246', label: 'CPU Node A', status: 'online', latency_ms: 23 }, { id: 'gpu-250', label: 'GPU Node', status: 'online', latency_ms: 14 }] }, top_processes: [] };
  const workers = { 'worker-a': { status: 'idle', host: 'h1', tasks_done: 3, tasks_failed: 0, cap_count: 2408, capabilities: ['x'] }, 'worker-b': { status: 'busy', host: 'h1', tasks_done: 1, tasks_failed: 1, cap_count: 2408, capabilities: ['x'] } };
  const events = [{ type: 'cap.ok', name: 'docker.ps', ts: '2026-09-13T17:06:50', elapsed_ms: 8 }, { type: 'cap.err', name: 'obs.cluster', ts: '2026-09-13T17:06:52', elapsed_ms: 12 }];
  const draw = (id, data, size) => { const r = recs[id]; const nn = VW.normalise(r); return strip(VW.draw(VW.formFor(r, data), data, size || nn.frame.size, { record: nn, draw: nn.draw, bare: true, sample: false, height: 200 })); };
  assert.strictEqual(VW.figure('counter', VW.mapped(VW.normalise(recs.workers), 'counter', health)), '1');
  assert.strictEqual(VW.figure('counter', VW.mapped(VW.normalise(recs.caps), 'counter', health)), '2,408');
  assert.strictEqual(VW.figure('string', VW.mapped(VW.normalise(recs.mode), 'string', health)), 'distributed');
  assert(/344,047/.test(draw('postgres', memory)) && /records/.test(draw('postgres', memory)) && !/%/.test(draw('postgres', memory)), 'the hero carries the record\'s unit, never a % it was not given');
  assert(/nodes 338,493/.test(draw('neo4j', memory)) && /relationships 4,121,227/.test(draw('neo4j', memory)), draw('neo4j', memory));
  const wl = draw('workerlist', workers); assert(/worker-a/.test(wl) && /worker-b/.test(wl) && /idle/.test(wl) && /2,408/.test(wl), 'a dict keyed by worker id lists as rows: ' + wl);
  const olRec = VW.normalise(recs.ollama); const olHtml = VW.draw('cards', sysmon, 'xl', { record: olRec, draw: olRec.draw, bare: true, sample: false, height: 200 }); const ol = strip(olHtml);
  assert(/CPU Node A/.test(ol) && /GPU Node/.test(ol) && /title="online"/.test(olHtml), 'the cards: a status dot per node — ' + ol);
  const ev = draw('events', events); assert(/docker\.ps/.test(ev) && /cap\.ok/.test(ev), ev);
  // the composite: its children as slices of the subject, one form per slot, the slots sharing the frame's height
  const oll = recs['sysmon-ollama']; const kids = {}; oll.children.forEach((c) => { kids[c.slot] = { __read: true, data: VW.pick(sysmon, c.record.source.replace(/^\$subject\.?/, '')) }; });
  const html = VW.draw('composite', sysmon, 'm', { record: VW.normalise(oll), kids, height: 200 });
  assert((html.match(/class="vw-slot"/g) || []).length === 4 && /vw-comp-2x2/.test(html), html.slice(0, 200));
  assert((html.match(/style="height:96px"/g) || []).length === 4, 'four slots of (200 - 8) / 2 px');
  const t = strip(html); assert(/in use\s*2/.test(t) && /67%/.test(t) && /2 of 3/.test(t), 'the ring: ' + t.slice(0, 160)); assert(/CPU Node A/.test(t) && /online/.test(t), t);
  assert(/class="vb-dial"/.test(html) && !/class="vb-ring"[^>]*style/.test(html), 'the ring\'s dial is its own element');
});
T('the element: the frame decides the rows a table shows; a composite or a feed is its own composition; a level\'s unit', () => {
  const rows = Array.from({ length: 40 }, (_, i) => ({ name: 'job-' + i, runs: i, last: '2026-09-13' }));
  const rec = VW.normalise({ form: 'table', draw: { columns: ['name', 'runs', 'last'] } });
  const tall = strip(VW.draw('table', rows, 'xl', { record: rec, draw: rec.draw, height: 400 })), short = strip(VW.draw('table', rows, 'xl', { record: rec, draw: rec.draw, height: 120 }));
  const count = (s) => (s.match(/job-\d+/g) || []).length;
  assert(count(tall) > count(short) && count(short) >= 2 && count(tall) <= 20, count(tall) + ' vs ' + count(short));
  assert(/of 40/.test(short), 'the rest pages');
  const comp = VW.draw('composite', {}, 'xl', { record: VW.normalise({ form: 'composite', children: [{ slot: 'a', record: { form: 'counter', data: { value: 3 } } }, { slot: 'b', record: { form: 'string', data: 'hi' } }] }), kids: {}, height: 200 });
  assert(!/vw-xl/.test(comp) && !/vw-detail/.test(comp), 'a composite is its own composition at XL');
  const feed = VW.draw('feed', [{ t: '2026-09-13T10:00:00', kind: 'x', text: 'one' }, { t: '2026-09-13T10:01:00', kind: 'y', text: 'two' }], 'xl', { record: VW.normalise({ form: 'feed' }), height: 200 });
  assert(!/vw-xl/.test(feed) && !/vw-xltable/.test(feed), 'a feed is its own composition at XL');
  assert.strictEqual(VW.figure('hero', 5), '5'); assert.strictEqual(VW.figure('ring', { value: 1, max: 4 }), '1'); assert.strictEqual(VW.figure('radial', { value: 40, min: 0, max: 100 }), '40%');
  assert.strictEqual(VW.figure('counter', VW.mapped(VW.normalise({ form: 'counter', draw: { unit: 'clients' } }), 'counter', { connected_clients: 11, value: 11 })), '11 clients');
  const bare = strip(VW.draw('hero', { value: 12 }, 'm', { bare: true })); assert(!/%/.test(bare), 'a bare count is not a percentage: ' + bare);
  const bounded = strip(VW.draw('hero', { value: 12, min: 0, max: 100 }, 'm', { bare: true })); assert(/%/.test(bounded), 'a bounded level is: ' + bounded);
});
T('the staged OK, the fixed rows, the gesture hold and the checking chip are in the text', () => {
  for (const s of [
    "const fresh = !st.dirty && st.vAnswered && st.vAnswered === st.vseq;", 'async function validateDetached(st, out) {', "if (typeof st.opts.onValidating === 'function') st.opts.onValidating();",
    "st.opts.onValidated(v.problems.length ? null : finalise(out, v.validated), v.problems, v.warnings);", 'S.dirty = true;', 'st.vAnswered = seq;',
    '.vw-body{flex:1;min-height:0;display:flex;align-items:safe center;justify-content:center;overflow:auto;font-size:10.5px;container-type:size}',
    'const fitRows = (o, size, rowH, chrome) =>', "const slotH = (o && o.height && !chip) ? Math.max(44, Math.floor((o.height - (nrows - 1) * 8) / nrows)) : 0",
    '.vw-slot-b{flex:1;min-height:0;display:flex;align-items:safe center;overflow:auto}', "bounded: d.max != null || d.min != null"
  ]) assert(EL.includes(s), s);
  for (const s of [
    "'.dash-grid[data-vd]{grid-auto-rows:var(--vd-row,58px)}'", "'.w-body > vera-widget.vd-draw{display:block;flex:1 1 auto;min-height:0;padding:8px}'",
    "grid.dataset.vd = '1'; gridVars(); window.addEventListener('resize', gridVars);",
    'var gesture = 0, layoutAfter = false;', 'if (gesture) { layoutAfter = true; return; }', "_dragGuardOn('grabbing'); gesture = 1;", "_dragGuardOn('nwse-resize'); gesture = 1;", '_dragGuardOff(); endGesture();',
    'function stager() {', 'function checking(w, on)', 'function validated(w, r, probs)', "onValidating: st.onValidating, onValidated: st.onValidated",
    "(w.dataset.checking ? ' · checking…' : '')", "'.vd-rec.checking{font-style:italic;opacity:.8}'"
  ]) assert(DASH.includes(s), s);
  assert(!DASH.includes("flex:1 0 auto"), 'the drawing never grows the tile');
});
// the board's tile is ONE head: the tile draws the title and the record chip, the element inside is bare
T('every tile the dashboard builds is bare', () => {
  assert.strictEqual((DASH.match(/setAttribute\('bare', ''\)/g) || []).length, 2);
  assert.ok(/el\.className = 'vd-draw'; el\.setAttribute\('bare', ''\);/.test(DASH));
});
console.log(n + ' passed' + (process.exitCode ? ', some failed' : ''));
