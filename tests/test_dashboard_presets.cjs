// The main dashboard's PRESETS (the layout files it ships) and the two behaviours that make a dashboard hold in a
// sandbox: a widget keeps its face across a failed or empty refresh, and the framework can swap the grid to a preset.
//   node tests/test_dashboard_presets.cjs
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const R = path.join(__dirname, '..');
const VD = fs.readFileSync(path.join(R, 'vera', 'chat', 'vera-dashboard.js'), 'utf8');
const WE = fs.readFileSync(path.join(R, 'vera', 'widgets', 'widget_element.js'), 'utf8');
const CH = fs.readFileSync(path.join(R, 'vera', 'capability_orchestration.html'), 'utf8');
const CO = fs.readFileSync(path.join(R, 'vera', 'capability_orchestration.py'), 'utf8');
const EV = fs.readFileSync(path.join(R, 'vera', 'evolve', 'evolve_capabilities.py'), 'utf8');

// ── the widget keeps its face ──
t('a read that failed keeps the last reading, else the sample face — never an empty tile', WE.includes("this._read = this._data !== undefined; this.render(); return; }") && WE.includes("if (!got && this._data === undefined) { this._err = ''; this._empty = true; this._read = false; this.render(); return; }"));
t('the caption says why: the read was empty, the sample stays', WE.includes("else if (this._empty && !have) why = esc(rec.source) + ' · read empty · sample';") && WE.includes("this._empty = false;"));
t('the seven-segment counter never wraps onto a second line: the digits shrink with the tile', /\.vb-seg7\{[^}]*flex-wrap:nowrap/.test(WE) && /\.vb-seg7 span\{[^}]*font-size:clamp\(13px, min\(68cqh, 15cqw\), 34px\)/.test(WE) && /\.vb-hero b\{[^}]*font-size:clamp\(16px, min\(60cqh, 12cqw\), 30px\)/.test(WE));

// ── the read-through hook (the policy itself is tested in tests/test_sandbox_read_through.py) ──
t('a read-only estate capability, called in a sandbox, is answered by prod before the local one runs — and only a GET', CO.includes('if _READ_THROUGH_URL and http_method == "GET" and not kw.get("_local") and _sg_read_through_allowed(name, http_method):') && CO.includes('_rt = await _upstream_read(name, kw)') && CO.includes('async def _upstream_read(name: str, kw: dict):'));
t("prod answers in the MCP envelope; the capability's own result is unwrapped from it (a tile drew the envelope's keys before)", CO.includes('if isinstance(j, dict) and j.get("type") == "tool_result" and "content" in j:') && CO.includes('return json.loads(txt)') && CO.includes('            return c'));
t('prod is asked with the arguments as given, no trace of ours, and marked as a sandbox read', CO.includes('args = {k: v for k, v in (kw or {}).items() if k != "trace_id"}') && CO.includes('"caller_kind": "sandbox-read"') && CO.includes('headers={"X-Vera-Read-Through": "sandbox"}'));
t('the sandbox compose says where prod is (the code defaults to the same door)', EV.includes('VERA_UPSTREAM_READ_URL: "https://host.docker.internal:8999/mcp/call"'));

// ── presets ──
t('the framework loads a preset: the records\' tiles go, the page\'s own hide unless the file names them, the file applies', VD.includes('function loadPreset(pkey) {') && VD.includes("widgets().forEach(function (w) { if (!named[w.dataset.wid]) state.hidden.add(w.dataset.wid); });") && VD.includes('loadPreset: loadPreset, presets: PRESETS,'));
t('Layouts ▾ lists the presets with a Load each', VD.includes('data-lm-preset=') && VD.includes("list.querySelectorAll('[data-lm-preset]').forEach(function (b) { b.onclick = function () { loadPreset(b.getAttribute('data-lm-preset')); }; });"));
t('the main dashboard ships four: Overview · Estate · Inference · Distributed compute', /presets:\[\{key:'main',name:'Overview'/.test(CH) && /\{key:'main-estate',name:'Estate'/.test(CH) && /\{key:'main-inference',name:'Inference/.test(CH) && /\{key:'main-compute',name:'Distributed compute'/.test(CH));

// ── the layout files: well formed, every tile a record with a form, a source (or a subject) and a span inside the grid ──
const FORMS = new Set((WE.match(/^\s+R\.([a-z_]+) = /gm) || []).map((m) => m.trim().replace(/^R\./, '').replace(/\s*=\s*$/, '')).concat((WE.match(/^\s+R\['([a-z_@]+)'\] = /gm) || []).map((m) => m.trim().replace(/^R\['/, '').replace(/'\]\s*=\s*$/, ''))));
const SOURCES = new Set(['obs.health', 'obs.events', 'obs.scheduler', 'obs.workers', 'obs.node_temps', 'obs.diagnostics', 'obs.redis', 'obs.pending', 'sysmon.status', 'sysmon.history', 'topology.snapshot', 'perf.scan', 'memory.stats', 'jobs.stats',
  'ollama.gate.status', 'ollama.instances', 'ollama.route_stats', 'ollama.request_log', 'ollama.list_models', 'ollama.routing.get', 'ollama.embed_config', 'catalog.installed', 'catalog.nodes', 'bench.results', 'background.status',
  'estate.health', 'backup.status', 'mesh.nodes', 'evolve.sandbox.list', 'evolve.sandbox.status', 'evolve.pipeline.list', 'sandbox.session.list', 'ide.vscode.instances', 'obs.cluster']);
['main-estate', 'main-inference', 'main-compute'].forEach((k) => {
  const j = JSON.parse(fs.readFileSync(path.join(R, 'vera', 'widgets', 'layouts', k + '.json'), 'utf8'));
  const ids = new Set(); let bad = [];
  (j.widgets || []).forEach((w) => { const r = w.record || {}; if (!r.id || ids.has(r.id)) bad.push('id ' + r.id); ids.add(r.id);
    if (!FORMS.has(r.form)) bad.push(r.id + ' form ' + r.form);
    if (r.form !== 'composite' && !SOURCES.has(r.source)) bad.push(r.id + ' source ' + r.source);
    if (!(Array.isArray(w.span) && w.span[0] >= 1 && w.span[0] <= 12 && w.span[1] >= 1)) bad.push(r.id + ' span');
    if (!(r.draw && r.draw.body === 'record')) bad.push(r.id + ' body');
    (r.children || []).forEach((c) => { const cr = c.record || {}; if (!FORMS.has(cr.form)) bad.push(cr.id + ' child form ' + cr.form); if (!(String(cr.source || '').startsWith('$subject') || SOURCES.has(cr.source))) bad.push(cr.id + ' child source ' + cr.source); }); });
  t(k + ': ' + (j.widgets || []).length + ' record tiles, every form drawable, every source a known read, every span in the grid', j.key === k && j.grid && j.grid.cols === 12 && (j.widgets || []).length >= 12 && !bad.length, JSON.stringify(bad.slice(0, 6)));
});
// ── the element itself, in a bare context (the widget test's loader): it reads the dashboard's sources on its own, and the
//    topology form draws a snapshot-shaped answer ──
{ const vm = require('node:vm'); const defined = {};
  const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
  ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
  vm.runInNewContext(fs.readFileSync(path.join(R, 'vera', 'ui', 'iso.js'), 'utf8'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
  const reads = ['ollama.route_stats', 'bench.results', 'ollama.embed_config', 'ollama.routing.get', 'catalog.installed', 'catalog.nodes', 'estate.health', 'backup.status', 'background.status', 'topology.snapshot', 'ollama.gate.status', 'ollama.instances', 'ollama.list_models', 'evolve.sandbox.list', 'obs.health'];
  t('the element reads every source the presets name on its own (no Read button left waiting)', reads.every((c) => W.readable(c) === true), JSON.stringify(reads.filter((c) => !W.readable(c))));
  t('and never a writing capability', ['ollama.routing.save', 'ollama.pull', 'evolve.sandbox.spawn', 'catalog.install', 'bench.run'].every((c) => W.readable(c) === false));
  const snap = { nodes: [{ id: 'hub', label: 'Vera', kind: 'hub' }, { id: 'cat:nodes', label: 'Nodes', kind: 'category' }, { id: 'node:1', label: 'ct126', kind: 'node', status: 'ok' }], edges: [{ from: 'hub', to: 'cat:nodes' }, { from: 'cat:nodes', to: 'node:1', kind: 'serves' }], ts: 1 };
  const h = W.draw('topology', snap, 'm', { bare: true });
  t('the topology form draws a snapshot-shaped answer (nodes + edges) as the graph', !/wempty|vw-nodata/.test(h) && /vb-topo/.test(h) && (h.match(/class="tn"/g) || []).length === 3, h.slice(0, 160)); }
// ── the read-through waits for a slow reading (prod's topology.snapshot takes ~11 s; the old 12 s limit fell back to the sandbox's empty stores) ──
{ const CO = fs.readFileSync(path.join(R, 'vera', 'capability_orchestration.py'), 'utf8');
  t('the read-through timeout is a named constant, 40 s by default, overridable by VERA_UPSTREAM_READ_TIMEOUT_S', CO.includes('_READ_THROUGH_TIMEOUT_S = float(os.environ.get("VERA_UPSTREAM_READ_TIMEOUT_S") or 40)') && CO.includes('timeout=_READ_THROUGH_TIMEOUT_S') && !CO.includes('verify=False, timeout=12)')); }
// -- the Cluster overview (main.json): bands that fill the twelve columns, no overlap, human titles, every page tile named --
{ const main = JSON.parse(fs.readFileSync(path.join(R, 'vera', 'widgets', 'layouts', 'main.json'), 'utf8'));
  const shown = main.widgets.filter((w) => !w.hidden), cells = {}; let overlap = '', rows = 0;
  shown.forEach((w) => { const [x, y] = w.at, [cw, ch] = w.span; for (let i = x; i < x + cw; i++) for (let j = y; j < y + ch; j++) { const k = i + ',' + j; if (cells[k]) overlap = overlap || (w.record.id + ' over ' + cells[k]); cells[k] = w.record.id; rows = Math.max(rows, j + 1); } });
  const holes = []; for (let j = 0; j < rows; j++) for (let i = 0; i < 12; i++) if (!cells[i + ',' + j]) holes.push(i + ',' + j);
  t('the overview places every tile by hand and no two overlap', !overlap, overlap);
  t('every row of the overview is filled across its twelve columns (no gaps)', !holes.length, holes.slice(0, 6).join(' '));
  const tilesOnly = shown.filter((w) => w.record.form !== 'section'), sections = shown.filter((w) => w.record.form === 'section');
  t('the overview has at least twenty-four tiles over real sources, under at least five sections', tilesOnly.length >= 24 && tilesOnly.every((w) => w.record.source) && sections.length >= 5 && sections.every((w) => w.span[0] === 12 && w.span[1] === 1), tilesOnly.length + ' tiles ' + sections.length + ' sections');
  t('every title is a name, not an id (no dots, no underscores, no "· source")', shown.every((w) => /^[A-Z][^_]*$/.test(w.record.title) && !/\.(status|stats|list|health|scan)/.test(w.record.title)), shown.filter((w) => !/^[A-Z][^_]*$/.test(w.record.title)).map((w) => w.record.title).join(' | '));
  const oldIds = ['host-resources', 'host-temps', 'status', 'mode', 'redis', 'workers', 'caps', 'pending', 'postgres', 'chroma', 'neo4j', 'ollama', 'mesh-info', 'looplab-info', 'sandboxes-info'];
  const named = {}; main.widgets.forEach((w) => { named[typeof w.record === 'string' ? w.record : w.record.id] = w; });
  t('the page tiles the overview replaced are named hidden, so a fresh dashboard does not show them twice', oldIds.every((id) => named[id] && named[id].hidden), oldIds.filter((id) => !(named[id] && named[id].hidden)).join(' '));
  t('the composites carry an odd child count somewhere (the last-row fill is exercised) and the stack topology is eight wide', shown.some((w) => w.record.form === 'composite' && w.record.children.length % 2 === 1) && named['topology-map'].span[0] === 8);
  // the maps' new words draw the right thing through the element itself
  const vm = require('node:vm'); const defined = {};
  const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
  ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
  vm.runInNewContext(fs.readFileSync(path.join(R, 'vera', 'ui', 'iso.js'), 'utf8'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
  const sys = { resources: { cpu: 21, mem: 56 }, proxmox: { running: 21, guests: 55, mem_pct: 58.6, configured: 1, nodes: 1 }, docker: { running: 64, containers: 244 }, ollama: { online: 3 } };
  const fleet = W.draw('rows', sys, 'm', { bare: true, record: { form: 'rows', read: { map: { pick: { 'guests up': 'proxmox.running', 'containers up': 'docker.running', 'ollama online': 'ollama.online' } } }, draw: { columns: ['name', 'value'] } } });
  t('pick builds rows from paths anywhere in the answer (a fleet from proxmox, docker and ollama)', /guests up/.test(fleet) && /containers up/.test(fleet) && /ollama online/.test(fleet) && /\b64\b/.test(fleet) && !/wempty/.test(fleet), fleet.slice(0, 200));
  const kv = W.draw('kv', sys.proxmox, 'm', { bare: true, record: { form: 'kv', read: { map: { keys: ['guests', 'running'] } } } });
  t('keys keeps only the named entries of a dict, in that order', /guests/.test(kv) && /running/.test(kv) && !/mem_pct/.test(kv) && kv.indexOf('guests') < kv.indexOf('running'), kv.slice(0, 200));
  const comp = { form: 'composite', children: [{ slot: 'a', record: { form: 'counter', data: { value: 3 } } }, { slot: 'b', record: { form: 'rows', data: [{ name: 'x', value: 1 }, { name: 'y', value: 2 }] } }, { slot: 'c', record: { form: 'kv', data: { p: 1 } } }] };
  const h3 = W.draw('composite', null, 'm', { bare: true, record: comp, height: 200, width: 300 });
  const hs = [...h3.matchAll(/height:(\d+)px/g)].map((m) => +m[1]);
  t('a composite of three: the last child spans the row (no hole), and the row holding the list is taller than the row of one figure', /grid-column:span 2/.test(h3) && hs.length === 3 && hs[0] === hs[1] && hs[1] > hs[2], JSON.stringify(hs) + ' ' + (h3.match(/grid-column:[^;"]+/g) || []).join(','));
  const h5 = W.draw('composite', null, 'm', { bare: true, record: { form: 'composite', children: comp.children.concat([{ slot: 'd', record: { form: 'kv', data: { q: 1 } } }, { slot: 'e', record: { form: 'kv', data: { r: 1 } } }]) }, height: 300, width: 700 });
  t('a wide composite of five draws three columns and its last row fills the width', /grid-template-columns:repeat\(3,1fr\)/.test(h5) && /grid-column:span 2/.test(h5), (h5.match(/grid-(template-columns|column):[^;"]+/g) || []).join(','));
  t('the shadow styles carry the packs: labels read the pack metrics, pixel hardens lines and stripes bars, terminal hatches', /text-transform:var\(--label-case/.test(WE) && /:host-context\(\[data-style="pixel"\]\) polyline/.test(WE) && /:host-context\(\[data-style="terminal"\]\)/.test(WE) && /:host-context\(\[data-style="newspaper"\]\)/.test(WE));
  const HTML = fs.readFileSync(path.join(R, 'vera', 'capability_orchestration.html'), 'utf8'), UI = fs.readFileSync(path.join(R, 'vera', 'vera-ui.js'), 'utf8');
  t('the packs\' faces are loaded: the shell page links them, and the UI script adds the link on any page that paints a pack', /Pixelify\+Sans/.test(HTML) && /id="veraPackFonts"/.test(HTML) && /_ensurePackFonts\(\)/.test(UI) && /Press\+Start\+2P/.test(UI));
  t('the tile head and body take the pack\'s label case, tracking, weight and padding; pixel marks titles, newspaper rules the head, terminal outlines', /text-transform:var\(--label-case,uppercase\)/.test(HTML) && /\.w-head\{padding:11px var\(--pad,12px\) 0/.test(HTML) && /\[data-style="pixel"\] \.w-head \.w-title::before/.test(HTML) && /\[data-style="newspaper"\] \.w-head\{border-bottom/.test(HTML) && /\[data-style="terminal"\] \.dash-toolbar \.btn\.teal/.test(HTML)); }
// -- a rows tile shows the columns its record names (the map's keys were the columns, and they skipped 'value') --
{ const vm = require('node:vm'); const defined = {};
  const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
  ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
  vm.runInNewContext(fs.readFileSync(path.join(R, 'vera', 'ui', 'iso.js'), 'utf8'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
  const health = { ollama: { 'gpu-250': { label: 'GPU Node', status: 'online', latency_ms: 17 }, 'cpu-246': { label: 'CPU Node A', status: 'online', latency_ms: 19 } } };
  const rec = { form: 'rows', read: { map: { rows: 'ollama', value: 'latency_ms', status: 'status' } }, draw: { columns: ['name', 'status', 'value'] } };
  const h = W.draw('rows', health, 'm', { bare: true, record: rec, draw: rec.draw });
  t("node latency rows show the instance, its status and its latency (the record's columns, not the map's keys)", /gpu-250/.test(h) && /online/.test(h) && /\b17\b/.test(h) && /\b19\b/.test(h), h.slice(0, 240));
  const comp = W.draw('composite', null, 'm', { bare: true, record: { form: 'composite', children: [{ slot: 'a', record: { form: 'counter', title: 'postgres', data: { value: 345923, unit: 'records' } } }, { slot: 'b', record: { form: 'rows', title: 'guests', data: [{ name: 'x', value: 1 }, { name: 'y', value: 2 }] } }] }, height: 200, width: 300 });
  t('a composite slot of a counter carries no second figure in its head (the seven-segment figure stands alone); a slot of rows says how many', /<span class="vw-slot-h">postgres<\/span>/.test(comp) && (comp.match(/vb-seg7/g) || []).length === 1 && /2 rows/.test(comp), comp.replace(/<style[\s\S]*?<\/style>/g, '').slice(0, 200)); }
console.log((fails ? 'FAILED ' : 'passed ') + (fails ? fails + ' check(s)' : 'all checks'));
process.exit(fails ? 1 : 0);
