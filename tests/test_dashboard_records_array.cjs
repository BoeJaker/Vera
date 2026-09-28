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

T('the layout: every visible tile but the topology a record; the breadth of forms; the page tiles it replaced are hidden', () => {
  const shown = LAY.widgets.filter((t) => !t.hidden && t.record && typeof t.record === 'object');
  const rec = shown.filter((t) => t.record.draw && t.record.draw.body === 'record'), page = shown.filter((t) => t.record.draw && t.record.draw.body === 'page');
  assert(rec.length >= 60, 'record tiles: ' + rec.length); assert.deepStrictEqual(page.map((t) => t.record.id), ['topology-map']); assert(recs['topology-map'].note);
  const forms = new Set(); rec.forEach((t) => { forms.add(t.record.form); (t.record.children || []).forEach((c) => forms.add(c.record.form)); });
  assert(forms.size >= 25, 'forms: ' + Array.from(forms).join(' '));
  for (const f of ['composite', 'counter', 'pills', 'ring', 'level', 'lines', 'rows', 'table', 'ranked', 'donut', 'log', 'terminal', 'numbers', 'vgraph', 'sandboxes', 'gauge', 'thermo', 'treemap', 'section']) assert(forms.has(f), f);
  assert(!PAGE.includes('data-wid="events"'), 'events has no markup'); assert.strictEqual(recs.events.draw.body, 'record');
  assert.strictEqual(recs.events.form, 'log'); assert.strictEqual(recs.events.source, 'obs.events');
  // the page's own KPI tiles and info cards the records replaced: still in the page, named hidden in the file
  for (const id of ['status', 'mode', 'redis', 'workers', 'caps', 'pending', 'mesh-info', 'looplab-info']) { assert(PAGE.includes('data-wid="' + id + '"'), id + ' markup'); const t = LAY.widgets.find((x) => (typeof x.record === 'string' ? x.record : x.record.id) === id); assert(t && t.hidden, id + ' hidden'); }
});
T('the records draw from the envelopes their sources really answer (a fixture of each, as the mirror gave them)', () => {
  const health = { redis: true, postgres: true, chroma: false, neo4j: true, workers: 1, caps: 2408, mode: 'distributed' };
  const memory = { backends: { postgres: { connected: true, total: 344047 }, chroma: { connected: true, count: 342000 }, neo4j: { connected: true, nodes: 338493, relationships: 4121227, sessions: 9287 } } };
  const sysmon = { ollama: { ok: true, online: 3, total: 3, gpu: 1, in_use: 2, nodes: [{ id: 'cpu-246', label: 'CPU Node A', status: 'online', latency_ms: 23 }, { id: 'gpu-250', label: 'GPU Node', status: 'online', latency_ms: 14 }] } };
  const workers = { 'worker-a': { status: 'idle', host: 'h1', cpu_pct: 12, ram_pct: 40, current_task: 'idle', tasks_done: 3, tasks_failed: 0 }, 'worker-b': { status: 'busy', host: 'h1', cpu_pct: 55, ram_pct: 61, current_task: 'cap.call', tasks_done: 1, tasks_failed: 1 } };
  const events = [{ type: 'cap.ok', name: 'docker.ps', ts: '2026-09-13T17:06:50', elapsed_ms: 8 }, { type: 'cap.err', name: 'obs.cluster', ts: '2026-09-13T17:06:52', elapsed_ms: 12 }];
  const draw = (id, data, size) => { const r = recs[id]; const nn = VW.normalise(r); return strip(VW.draw(VW.formFor(r, data), data, size || nn.frame.size, { record: nn, draw: nn.draw, bare: true, sample: false, height: 200, width: 600 })); };
  assert.strictEqual(VW.figure('counter', VW.mapped(VW.normalise({ form: 'counter', read: { map: { value: 'workers' } } }), 'counter', health)), '1');
  assert.strictEqual(VW.figure('counter', VW.mapped(VW.normalise({ form: 'counter', read: { map: { value: 'caps' } } }), 'counter', health)), '2,408');
  const ms = draw('memory-stores', memory); assert(/neo4j links/.test(ms) && /4\.12M/.test(ms) && /postgres records/.test(ms) && /344k/.test(ms), 'the stores, one bar each, the figures short: ' + ms);
  const wl = draw('ops-workers', workers); assert(/worker-a/.test(wl) && /worker-b/.test(wl), 'a dict keyed by worker id lists as rows: ' + wl);
  const ev = draw('events', events); assert(/docker\.ps/.test(ev) && /cap\.ok/.test(ev), ev);
  // the composite: its children as slices of the subject, one form per slot
  const oll = recs['sysmon-ollama']; const kids = {}; oll.children.forEach((c) => { kids[c.slot] = { __read: true, data: /^\$subject/.test(c.record.source) ? VW.pick(sysmon, c.record.source.replace(/^\$subject\.?/, '')) : undefined }; });
  const html = VW.draw('composite', sysmon, 'm', { record: VW.normalise(oll), kids, height: 360, width: 420 });
  assert.strictEqual((html.match(/class="vw-slot[ "]/g) || []).length, oll.children.length, html.slice(0, 200));
  const t = strip(html); assert(/in use/.test(t) && /2 of 3/.test(t), 'the ring: ' + t.slice(0, 160)); assert(/CPU Node A/.test(t) && /online/.test(t), t);
  assert(/class="vb-dial"/.test(html), 'the ring\'s dial is its own element');
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
    'const fitRows = (o, size, rowH, chrome) =>', 'const slotHOf = (ri) => bodyH ? Math.max(44, (fls[ri] || 44) + Math.floor(free * (wts[ri] || 1) / wsum)) : 0;',
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
