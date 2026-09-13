// <vera-widget> / window.VeraWidget (UI redesign M2): every drawn form renders fixture data at M; XS and S are the
// compositions the Sizes board names; L adds the detail list and XL the table; an unknown form says so; both record
// shapes normalise to one; the shape rule and the fallback rule are the chat's, now here.
//   node tests/test_widget_element.cjs   (CommonJS: the pipeline's gate parses every js file as a script)
const fs = require('node:fs'); const path = require('node:path'); const vm = require('node:vm');
const here = __dirname;
const src = fs.readFileSync(path.join(here, '..', 'vera', 'widgets', 'widget_element.js'), 'utf8');
const defined = {};
const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {} }) } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document;
vm.runInNewContext(src, ctx);
const W = ctx.window.VeraWidget;
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

t('api', ['draw', 'forms', 'normalise', 'formByShape', 'dataFor', 'applyMap', 'pick', 'mapped', 'formFor', 'readable', 'key', 'hydrate', 'css', 'ensureCss', 'sample', 'call'].every((k) => typeof W[k] === 'function') && W.version === 2);
t('the element is defined', !!defined['vera-widget']);
const series = [{ t: 1, v: 40 }, { t: 2, v: 48 }, { t: 3, v: 62 }, { t: 4, v: 55 }];
const fix = {
  trace: [series, /<polyline/], radial: [{ value: 62, min: 0, max: 100 }, /62%<\/text>/], counter: [{ value: 1234, unit: 'ms', delta: -12 }, /vw-hero[\s\S]*1,234[\s\S]*ms[\s\S]*down/],
  bar: [{ value: 7, min: 0, max: 10, unit: 'G' }, /vw-meter[\s\S]*width:70\.0%/], bars: [{ a: 3, b: 9, c: 1 }, /<rect[\s\S]*<rect[\s\S]*<rect/], thermo: [{ cpu: 62, gpu: 31 }, /vw-therm[\s\S]*cpu/],
  heat: [Object.fromEntries('abcdefghij'.split('').map((k, i) => [k, i + 1])), /vw-heat[\s\S]*grid-template-columns:repeat\(4,1fr\)/], matrix: [{ ct126: { api: 'ok', db: 'down' }, ct121: { api: 1, db: 0.5 } }, /vw-matrix[\s\S]*ct126/],
  donut: [{ running: 26, paused: 3, stopped: 4 }, /vw-donut[\s\S]*running/], stack: [{ llm: 5, embed: 3 }, /vw-stackbar/], pills: [[{ name: 'redis', status: 'ok' }, { name: 'neo4j', status: 'down' }], /vw-pill ok[\s\S]*vw-pill bad/],
  log: [[{ t: '2026-09-11T09:21:00', kind: 'INFO', text: 'pull skipped' }], /vw-log[\s\S]*09:21:00[\s\S]*pull skipped/], lane: [[{ t: 1, kind: 'a', text: 'x' }, { t: 2, kind: 'b', text: 'y' }], /lane">a · 1/],
  table: [[{ name: 'ct126', load: 0.6 }, { name: 'ct121', load: 0.2 }], /<th>name<\/th><th>load<\/th>/], files: [[{ path: '/a.py', size: 12 }], /<th>path<\/th>/], list: [['one', 'two'], /vw-list[\s\S]*one/],
  checklist: [[{ text: 'gate', done: true }, { text: 'sweep' }], /class="on"[\s\S]*☑[\s\S]*☐/], stepper: [{ stages: [{ name: 'plan', done: true }, { name: 'act', current: true }, { name: 'verify' }] }, /done[\s\S]*now[\s\S]*verify/],
  calendar: [[{ when: '2026-09-11T16:00', title: 'sweep' }], /vw-cal[\s\S]*2026-09-11[\s\S]*16:00/], string: ['hello', /vw-str">hello/], kv: [{ ok: true, node: 'ct126' }, /vw-kv[\s\S]*node/],
  scatter: [[{ x: 1, y: 2 }, { x: 3, y: 4 }], /<circle[\s\S]*<circle/], pipes: [{ nodes: [{ id: 'a' }, { id: 'b' }], links: [{ source: 'a', target: 'b' }] }, /vw-mm[\s\S]*data-mm-code="graph LR/],
};
let ok = true;
for (const [form, [data, re]] of Object.entries(fix)) { const h = W.draw(form, data, 'm'); if (!re.test(h) || /class="wempty"/.test(h)) { ok = false; console.log('  ' + form + ' → ' + h.slice(0, 160)); } }
t('every drawn form renders its fixture at M (' + Object.keys(fix).length + ' forms)', ok);
t('aliases resolve to a renderer', /<polyline/.test(W.draw('sparkline', series, 'm')) && /<polyline/.test(W.draw('line', series, 'm')) && /62%/.test(W.draw('ring', { value: 62, min: 0, max: 100 }, 'm')) && /<rect/.test(W.draw('ranked', { a: 1, b: 2 }, 'm')));
t('unknown form says so', /class="wempty">form zzz · no drawing yet/.test(W.draw('zzz', {}, 'm')));
t('no data draws the SAMPLE face, marked (sample:false keeps the bare answer)', /data-sample="1"/.test(W.draw('trace', null, 'm')) && /<polyline/.test(W.draw('trace', null, 'm')) && /vw-sampletag/.test(W.draw('trace', null, 'm')) && /a trace needs two/.test(W.draw('trace', [1], 'm')) && /no data yet/.test(W.draw('trace', null, 'm', { sample: false })) && !/data-sample/.test(W.draw('trace', series, 'm')));
t('XS is glyph + one figure', /^<span class="vw-xs"[\s\S]*<i>∿<\/i>55<\/span>$/.test(W.draw('trace', series, 'xs')));
t('S is a chip with the label', /^<span class="vw-chip"[\s\S]*<b>62%<\/b><small>gate<\/small><\/span>$/.test(W.draw('radial', { value: 62, min: 0, max: 100 }, 's', { title: 'gate' })));
t('L adds the detail list', /vw-l[\s\S]*vw-therm[\s\S]*vw-detail[\s\S]*cpu/.test(W.draw('thermo', { cpu: 62, gpu: 31 }, 'l')));
t('XL adds the table', /vw-xl[\s\S]*vw-detail[\s\S]*vw-xltable[\s\S]*<th>name<\/th><th>value<\/th>/.test(W.draw('thermo', { cpu: 62, gpu: 31 }, 'xl')));
t('bare returns just the form at any size', !/vw-l|vw-xl/.test(W.draw('thermo', { cpu: 62 }, 'xl', { bare: true })));
t('the envelope is seen through', /<polyline/.test(W.draw('trace', { ok: true, history: series }, 'm')));
// the shape rule (the Formats board's table) and the fallback
t('formByShape', W.formByShape({ value: 3, max: 10 }) === 'radial' && W.formByShape({ value: 3 }) === 'counter' && W.formByShape(series) === 'trace' && W.formByShape([{ x: 1, y: 2 }]) === 'scatter' && W.formByShape({ a: 1, b: 2 }) === 'thermo' && W.formByShape(Object.fromEntries('abcdefghij'.split('').map((k) => [k, 1]))) === 'heat' && W.formByShape([{ t: 1, kind: 'x', text: 'y' }]) === 'log' && W.formByShape({ nodes: [], links: [] }) === 'pipes' && W.formByShape([{ name: 'a' }]) === 'table' && W.formByShape([{ path: 'p' }]) === 'files' && W.formByShape([{ text: 'x', done: true }]) === 'checklist' && W.formByShape({ stages: [] }) === 'stepper' && W.formByShape({ ok: true, items: series }) === 'trace' && W.formByShape({ ok: true, text: 'hi' }) === null);
t('formFor: the chosen form, else the shape, else kv', W.formFor({ form: 'trace' }, series) === 'trace' && W.formFor({ form: 'table' }, { cpu: 62, gpu: 31 }) === 'thermo' && W.formFor({ form: 'table' }, { ok: true, node: 'ct126' }) === 'kv' && W.formFor({ form: 'radial' }, undefined) === 'radial');
// the record in one shape
const a = W.normalise({ name: 'GPU + queue', form: 'meter', reads: { cap: 'sysmon.status', args: { node: 'ct126' }, every: '10s' }, draw: { form: 'meter', size: 'S', glow: 1 } });
const b = W.normalise({ title: 'GPU', form: 'trace', source: 'sysmon.history', window: '1h', size: 'l', refresh: '5s' });
const c = W.normalise({ name: 'Gate', form: 'meter', reads: { cap: 'obs.pending' }, draw: { form: 'radial', size: 'S' } });
t('normalise: the template shape draws draw.form, not the kind', c.form === 'radial' && c.frame.size === 's');
t('normalise: the template shape', a.form === 'meter' && a.source === 'sysmon.status' && a.read.args.node === 'ct126' && a.read.refresh === '10s' && a.frame.size === 's' && a.draw.glow === 1 && a.title === 'GPU + queue');
t('normalise: the short form', b.form === 'trace' && b.source === 'sysmon.history' && b.read.window === '1h' && b.read.args.window === '1h' && b.frame.size === 'l' && b.read.refresh === '5s');
t('normalise: a panel record', W.normalise({ form: 'panel', source: 'panel:system-monitor' }).panel === 'system-monitor');
t('key is form · source · args', W.key({ form: 'trace', source: 'obs.events', args: { window: '1h' } }) === 'trace obs.events {"window":"1h"}');
t('readable: quiet reads only', W.readable('obs.health') && W.readable('sysmon.history') && W.readable('jobs.list') && !W.readable('code.write') && !W.readable('exec.code.run') && !W.readable('canvas.create'));
t('hydrate is a no-op without the mermaid element', W.hydrate({ querySelectorAll: () => [{}] }) === 0);
t('css names the classes the markup uses', /\.wempty/.test(W.css()) && /\.vw-therm/.test(W.css()) && /\.vw-xl/.test(W.css()));
t('forms() lists the drawn forms and the aliases', W.forms().some((f) => f.id === 'trace' && f.shape === 'series') && W.forms().some((f) => f.id === 'sparkline' && f.as === 'trace') && W.forms().length > 60);
// the context graph as a form: mini lanes at M (a lane per family, hollow where not injected, the relations), the full element's slot at XL
const cgd = { nodes: [{ id: 'v1', label: 'fabric.py', kind: 'chunk', score: 0.9 }, { id: 'm1', label: 'recall', kind: 'memory', score: 0.6, included: false }, { id: 's1', label: 'recall step', kind: 'step', score: 0.8 }], rels: [{ from: 'm1', to: 'v1', kind: 'mem' }] };
const cgm = W.draw('context_graph', cgd, 'm', { bare: true, height: 90, labels: true, layout: 'lanes' });
t('context_graph at M: three lanes, three members, one relation, the ghost hollow, labels', /vw-cg/.test(cgm) && (cgm.match(/border-top:1px dashed/g) || []).length === 3 && (cgm.match(/border-radius:50%/g) || []).length === 3 && /<line /.test(cgm) && /background:transparent/.test(cgm) && /fabric\.py/.test(cgm), cgm.slice(0, 200));
t('context_graph at XL: the slot the full element fills', /vw-cgfull/.test(W.draw('context_graph', cgd, 'xl', {})) && /data-cg=/.test(W.draw('context_graph', cgd, 'xl', {})));
t('context_graph is a drawn form of the graph shape', W.forms().some((f) => f.id === 'context_graph' && f.shape === 'graph' && f.drawn));
// ── the sample face (defects 18 · 19): every form has a face before it has read anything ──
const SHAPES = ['level', 'series', 'values', 'events', 'graph', 'items', 'stages', 'rate', 'parts', 'ohlcv', 'matrix', 'calendar', 'string', 'points', 'panel', 'composite'];
t('sample() gives every shape realistic data', SHAPES.every((s) => W.sample(s) != null) && W.sample('level').value === 62 && W.sample('series').length === 24 && Object.keys(W.sample('values')).length === 6 && W.sample('events').length === 6 && W.sample('graph').nodes.length === 7 && W.sample('stages').stages.length === 5 && W.sample('ohlcv').length === 12 && typeof W.sample('string') === 'string' && W.sample('composite').children.length === 4);
t('sample(form) follows the form: files have paths, checklists are ticked, the context graph has rels', W.sample('files')[0].path === '/srv/vera/fabric.py' && W.sample('checklist')[0].done === true && W.sample('context_graph').rels.length === 5 && W.sample('tank').value === 62 && W.sample('meter').max === 100);
// the catalogue's every form id (widget_record.py's FORMS) draws a face with no data — none says "no drawing yet" or "no data yet"
const py = fs.readFileSync(path.join(here, '..', 'vera', 'widgets', 'widget_record.py'), 'utf8');
const ids = [...py.matchAll(/_F\("([a-z0-9_-]+)",/g)].map((m) => m[1]);
const faceless = ids.filter((id) => { const h = W.draw(id, undefined, 'm'); return /no drawing yet|no data yet|class="wempty"/.test(h) && id !== 'panel'; });
t('every catalogue form draws a face with no data (' + ids.length + ' forms)', ids.length > 90 && !faceless.length, faceless.join(' '));
const faces = ['meter', 'gauge', 'column', 'graph', 'pulse', 'heat', 'trace', 'thermo', 'donut'].map((id) => W.draw(id, undefined, 'm').replace(/data-sample="1"/, ''));
t('different forms draw DIFFERENT faces (defect 18: not one placeholder for everything)', new Set(faces).size === faces.length && /vw-meter/.test(faces[0]) && /<circle/.test(faces[1]) && /<rect/.test(faces[2]) && /vw-mm/.test(faces[3]) && /vw-log/.test(faces[4]) && /vw-heat/.test(faces[5]));
t('a placeholder string handed to a numeric form draws the sample, not the complaint', /data-sample="1"/.test(W.draw('meter', 'no reading yet', 'm')) && /vw-meter/.test(W.draw('meter', 'no reading yet', 'm')) && /vw-str">hello/.test(W.draw('string', 'hello', 'm')));
t('the sample face at XS and S is marked with a class', /vw-xs vw-sampled" data-sample="1"/.test(W.draw('radial', null, 'xs')) && /vw-chip vw-sampled"/.test(W.draw('radial', null, 's')) && /62%/.test(W.draw('radial', null, 's')));
t('an empty result is no data: the sample face', /data-sample="1"/.test(W.draw('thermo', {}, 'm')) && /data-sample="1"/.test(W.draw('table', [], 'm')) && /data-sample="1"/.test(W.draw('trace', { ok: true, history: [] }, 'm')));
t('a composite without children draws the sample composite (four children)', (W.draw('composite', null, 'm', { record: { form: 'composite' } }).match(/class="vw-slot" data-slot/g) || []).length === 4 && /data-sample="1"/.test(W.draw('composite', null, 'm')));
// ── read.map: the source's envelope through the record's field mapping; read.range: the level's lo – hi ──
t('pick walks a dotted path with indices', W.pick({ a: { b: [1, { c: 7 }] } }, 'a.b[1].c') === 7 && W.pick({ h: [1, 2, 3] }, 'h[-1]') === 3 && W.pick({ x: 1 }, '$') && W.pick({ x: 1 }, 'nope.deeper') === undefined);
const env = { ok: true, gpu: { util: 71, cap: 100 }, data: { nodes: [{ hostname: 'ct126', load: 0.6, temp: 71 }, { hostname: 'ct121', load: 0.2, temp: 54 }] }, history: series };
t('applyMap: a level picks value/max by path', W.applyMap(env, { value: 'gpu.util', max: 'gpu.cap' }, 'level').value === 71 && W.applyMap(env, { value: 'gpu.util', max: 'gpu.cap' }, 'level').max === 100);
const rowsM = W.applyMap(env, { rows: 'data.nodes', name: 'hostname', value: 'load' }, 'items');
t('applyMap: items pick the rows and rename their fields', Array.isArray(rowsM) && rowsM.length === 2 && rowsM[0].name === 'ct126' && rowsM[0].value === 0.6 && rowsM[0].hostname === 'ct126');
t('applyMap: a series picks its list; values from rows become keyed', W.applyMap(env, { series: 'history' }, 'series') === series && /<rect[\s\S]*ct126/.test(W.draw('bars', env, 'm', { map: { values: 'data.nodes', name: 'hostname', value: 'temp' } })));
t('applyMap: a path that resolves to nothing leaves the data alone (the sample face survives a stale map)', W.applyMap(env, { value: 'nope.x' }, 'level') === env && /data-sample="1"/.test(W.draw('radial', undefined, 'm', { record: { form: 'radial', read: { map: { value: 'gpu.util' } } } })));
t('draw() honours the record\'s read.map', /71%<\/text>/.test(W.draw('radial', env, 'm', { record: { form: 'radial', read: { map: { value: 'gpu.util', max: 'gpu.cap' } } } })) && /<polyline/.test(W.draw('trace', { ok: true, samples: series }, 'm', { record: { form: 'trace', read: { map: { series: 'samples' } } } })));
t('draw() honours the record\'s read.range (the record\'s wins over the source\'s)', /width:50\.0%/.test(W.draw('bar', { value: 5, min: 0, max: 100 }, 'm', { record: { form: 'bar', read: { range: [0, 10] } } })));
const nm = W.normalise({ form: 'radial', source: 'obs.pending', read: { map: { value: 'v' }, range: [0, 20] }, frame: { size: 'm', motion: false, legend: true }, skin: 'pixel', subject: 'nodes.ct126' });
t('normalise carries read.map · read.range · frame.motion/legend · skin · subject', nm.read.map.value === 'v' && nm.read.range[1] === 20 && nm.frame.motion === false && nm.frame.legend === true && nm.skin === 'pixel' && nm.subject === 'nodes.ct126' && W.normalise({ form: 'radial' }).frame.motion === null && W.normalise({ form: 'radial' }).skin === 'inherit');
// ── the composite: every child an ordinary widget with its own record and its own read (opts.kids per slot) ──
const crec = { form: 'composite', layout: '2x2', children: [{ slot: 'a', record: { form: 'radial', title: 'Gate', source: 'obs.pending' } }, { slot: 'b', record: { form: 'trace', title: 'Latency', source: 'sysmon.history', read: { map: { series: 'history' } } } }] };
const ch = W.draw('composite', undefined, 'm', { record: crec, kids: { a: { value: 3, min: 0, max: 20 }, b: { ok: true, history: series } } });
t('a composite draws its children from what the element read per slot, through each child\'s own map', (ch.match(/class="vw-slot" data-slot/g) || []).length === 2 && /vw-slot-h">Gate<b>3<\/b>/.test(ch) && /<polyline/.test(ch) && !/data-sample/.test(ch) && /vw-comp-2x2/.test(ch));
t('a child with nothing read yet draws its sample face, marked', /data-sample="1"/.test(W.draw('composite', undefined, 'm', { record: crec, kids: {} })));
const rl = W.draw('composite', undefined, 'm', { record: Object.assign({}, crec, { layout: 'report' }), kids: { a: { value: 3, min: 0, max: 20 } } });
t('a report / rail composite draws its children as chips in rows', /vw-slot-row[\s\S]*class="k">Gate<\/span><span class="vw-chip"/.test(rl) && /vw-comp-report/.test(rl));
t('css names the sample tag', /\.vw-sampletag/.test(W.css()) && /\.vw-sampled/.test(W.css()));
// ── the one /mcp/call helper opens prod's envelope and a stand-in's ──
(async () => {
  const answers = [{ type: 'tool_result', tool_name: 'x', content: { ok: true, a: 1 } }, { result: { ok: true, b: 2 } }, { ok: true, c: 3 }];
  let i = 0; ctx.fetch = async () => ({ json: async () => answers[i++] });
  const r = [await W.call('', 'x'), await W.call('', 'x'), await W.call('', 'x')];
  t('call() opens {type:tool_result, content}, {result} and the bare object', r[0].a === 1 && r[1].b === 2 && r[2].c === 3);
  // ── the surface (window.VeraWidgetConfig): the API, its record shapes, the two context-graph entries ──
  const C = ctx.window.VeraWidgetConfig;
  t('VeraWidgetConfig: open/close, version 2, the packs, the two context-graph entries', C && typeof C.open === 'function' && typeof C.close === 'function' && C.version === 2 && C.packs.length === 5 && C.packs[0][0] === 'inherit' && C.packs[4][0] === 'pixel' && C.entries.length === 2 && C.entries[0].id === 'context_graph' && C.entries[0].size === 'm' && C.entries[1].size === 'xl' && /mini/.test(C.entries[0].n) && /full/.test(C.entries[1].n));
  const rf = C.recordFrom({ name: 'GPU + queue', form: 'meter', reads: { cap: 'sysmon.status', args: { node: 'ct126' }, every: '10s' }, draw: { form: 'meter', size: 'S' }, placed: ['LHM · Ops glance', 'dashboard'] }, 'lhm');
  t('recordFrom: a template becomes the sheet\'s record (source · read · frame · placement)', rf.form === 'meter' && rf.source === 'sysmon.status' && rf.read.args.node === 'ct126' && rf.read.refresh === '10s' && rf.frame.size === 's' && rf.frame.dive === true && rf.placement.join(',') === 'rail,dashboard' && rf.title === 'GPU + queue');
  const rf2 = C.recordFrom({ form: 'radial' }, 'canvas');
  t('recordFrom: into sets the placement default', rf2.placement.join(',') === 'canvas' && C.recordFrom({ form: 'radial' }, 'reply').placement[0] === 'chat' && C.recordFrom({ form: 'radial' }, 'lhm').placement[0] === 'rail');
  const ro = C.recordOut(Object.assign(rf2, { projection: 'iso', frame: Object.assign(rf2.frame, { dive: false }), read: Object.assign(rf2.read, { range: [0, 8] }) }));
  t('recordOut: the full shape both readers take (deep_dive + dive, place + placement, draw.proj, read.range)', ro.frame.deep_dive === false && ro.frame.dive === false && ro.place === 'canvas' && ro.placement[0] === 'canvas' && ro.draw.proj === 'iso' && Array.isArray(ro.actions) && ro.read && ro.read.args && ro.read.range[1] === 8 && !('range' in C.recordOut(C.recordFrom({ form: 'radial' })).read));
  const comp = C.recordFrom({ form: 'composite', layout: '2x2', children: [{ slot: 'a', form: 'radial', source: 'obs.pending', size: 's' }, { slot: 'b', record: { form: 'trace', source: 'sysmon.history' } }] });
  t('recordFrom: a composite\'s children are records', comp.children.length === 2 && comp.children[0].record.form === 'radial' && comp.children[0].record.source === 'obs.pending' && comp.children[1].record.form === 'trace' && comp.layout === '2x2');
  console.log(fails ? fails + ' FAILED' : 'all passed');
  process.exit(fails ? 1 : 0);
})();

