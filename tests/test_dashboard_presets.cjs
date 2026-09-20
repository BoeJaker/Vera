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
t("prod answers in the MCP envelope; the capability's own result is unwrapped from it (a tile drew the envelope's keys before)", CO.includes('if isinstance(j, dict) and j.get("type") == "tool_result" and isinstance(j.get("content"), list):') && CO.includes('return json.loads(txt)'));
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
console.log((fails ? 'FAILED ' : 'passed ') + (fails ? fails + ' check(s)' : 'all checks'));
process.exit(fails ? 1 : 0);
