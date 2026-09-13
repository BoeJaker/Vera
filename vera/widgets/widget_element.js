/* vera/widgets/widget_element.js — <vera-widget> and window.VeraWidget (UI redesign M2, Notes/40 §3; the Widgets ·
   Sizes · WidgetConfig boards).
   ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
   ONE renderer for every widget record, wherever it is placed: a dashboard tile, a canvas item, an LHM slot, a
   block in a reply, a notebook cell, an ops node box. The record (widget_record.py's schema — the full record,
   the registry's template shape or a fence's short form) says the FORM, the SOURCE and the SIZE; this element
   resolves the source, subscribes at the record's refresh (never tighter than 10 s; a source that is not
   read-shaped waits for a click), draws the form AT THE SIZE and exposes the actions and the events.

   A size is a composition, not a scale (the Sizes board):
     xs  glyph + one figure, inline in a sentence          s   a chip: glyph + figure + label
     m   the form drawn, with its title and caption          l   the form plus its detail list beside it
     xl  a panel: the form, its table, its log and its actions

   API
     window.VeraWidget.draw(form, data, size, opts?)  → HTML string   the form's markup at the size (host styles it
                                                                        with VeraWidget.css(); ensureCss(root) injects once)
     window.VeraWidget.forms()                         → [{id, shape, sizes, drawn}]   what this file can draw today
     window.VeraWidget.normalise(record)               → the record in one shape (form · source · title · read · frame · draw)
     window.VeraWidget.formByShape(data)               → the default form a result's shape picks (the Formats board's rule)
     window.VeraWidget.dataFor(data, form)             → the part of a result the form draws
     window.VeraWidget.applyMap(data, map, shape)      → the source's envelope through the record's read.map: the shape's
                                                          fields (value · series · rows · nodes …) picked by path, row fields
                                                          renamed; the record's read.range is the level's lo – hi
     window.VeraWidget.pick(obj, path)                 → one dotted path (a.b[2].c, history[-1]) out of an envelope
     window.VeraWidget.formFor(record, data)           → the form that can draw THIS data (the chosen one, else the shape's, else kv)
     window.VeraWidget.readable(cap)                   → may a block read this capability on its own?
     window.VeraWidget.key(record)                     → the record's identity: form · source · args
     window.VeraWidget.hydrate(root)                   → mounts <vera-mermaid> into the pipes form's slots when it is defined
     window.VeraWidget.sample(formOrShape)             → realistic sample data for a form (or a shape): the face a widget has
                                                          before it has read anything — draw() and the element use it, marked
     window.VeraWidgetConfig.open(opts)                → the WidgetConfig board as a sheet (catalogue · record · live preview);
                                                          Promise<record | null> — every picker and every ⚙ goes through it
     <vera-widget record='{…json…}' size="m|auto" base="">   .record (property) · .refresh() · .read()
       a COMPOSITE record reads its children's own sources (each child a record; a child whose source is $subject.<path>
       takes that slice of the composite's one read); frame.motion false holds a moving form still; skin sets the
       pack (data-style) on the element so the page's own pack rules dress it
       events (bubbling, composed): widget:open · widget:pin · widget:ask · widget:ops · widget:print · widget:mute ·
                                    widget:refresh · widget:resize · widget:rendered

   Dependency-free, theme variables with fallbacks. The chat's reply block, the canvas item and the dashboard
   tile all draw through here — the chat's own nine renderers became these.                                     */
(function () {
  'use strict';
  if (window.VeraWidget) return;
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const num = (v) => { const n = typeof v === 'number' ? v : parseFloat(v); return isFinite(n) ? n : 0; };
  const fmt = (v) => { const n = num(v); return Math.abs(n) >= 100 ? Math.round(n).toLocaleString() : (Math.round(n * 10) / 10).toString(); };
  const SIZES = ['xs', 's', 'm', 'l', 'xl'];
  const HEIGHT = { xs: 14, s: 24, m: 70, l: 110, xl: 200 };
  const EMPTY = (t) => '<span class="wempty">' + esc(t) + '</span>';
  const ALIAS = { sparkline: 'trace', line: 'trace', area: 'trace', step: 'trace', chart: 'trace', scope: 'trace', slope: 'trace', horizon: 'trace', bump: 'trace',
                  ring: 'radial', gauge: 'radial', level: 'counter', hero: 'counter', meter: 'bar', 'meter-panel': 'thermo', bullet: 'thermo',
                  column: 'bars', ranked: 'bars', lollipop: 'bars', histogram: 'bars', pareto: 'bars', diverging: 'bars', waterfall: 'bars',
                  parts: 'donut', 'stacked-bar': 'stack', stacks: 'stack', funnel: 'stack', treemap: 'heat', waffle: 'heat', dots: 'matrix',
                  numbers: 'pills', rings: 'thermo', 'spark-table': 'table', rows: 'list', cards: 'list', people: 'list', gallery: 'list',
                  links: 'list', board: 'checklist', tree: 'files', feed: 'log', timeline: 'log', pulse: 'log', comet: 'log',
                  pipeline: 'stepper', stages: 'stepper', conveyor: 'stepper', program: 'stepper', gantt: 'stepper', agenda: 'calendar',
                  announcement: 'string', 'split-flap': 'string', ask: 'string', graph: 'pipes', minigraph: 'pipes', flow: 'pipes', topology: 'pipes',
                  node: 'kv', form: 'kv', tank: 'radial', turbine: 'counter', rate: 'counter', candles: 'trace', orbit: 'list', shelf: 'list', stack: 'list', city: 'table', iso: 'table', globe: 'scatter',
                  // the rest of the catalogue (widget_record.py's FORMS): every form id resolves to a renderer, so a pick never
                  // lands as "no drawing yet" — the board's names too (dial, galaxy)
                  threshold: 'bar', 'small-multiples': 'trace', box: 'bars', radar: 'bars', carousel: 'list', terminal: 'log', controls: 'pills', button: 'string', header: 'string', rail: 'list', dial: 'radial', galaxy: 'context_graph',
                  // the boards' forms the catalogue names now and this file draws in its own way next: the nearest face until then
                  sweep: 'log', activity: 'log', notices: 'log', library: 'list', pages: 'list', wiki: 'list', devices: 'list', notebook: 'list', hosts: 'list', containers: 'list', models: 'list', datasets: 'list', sandboxes: 'list',
                  approvals: 'stepper', frame: 'string', diagram: 'pipes', glance: 'pills', compare: 'bars', tabs: 'matrix', slider: 'thermo', ticker: 'counter', battery: 'bar', tablei: 'table', temps: 'thermo', checks: 'checklist', memgraph: 'pipes', logi: 'log', notice: 'string' };
  // what this file draws today (the rest of the catalogue resolves through ALIAS or says so)
  const DRAWN = { trace: 'series', radial: 'level', counter: 'level', bar: 'level', bars: 'values', thermo: 'values', heat: 'values', matrix: 'matrix', donut: 'parts',
                  stack: 'parts', pills: 'values', log: 'events', lane: 'events', table: 'items', files: 'items', list: 'items', checklist: 'items', stepper: 'stages',
                  calendar: 'calendar', string: 'string', kv: 'values', pipes: 'graph', context_graph: 'graph', scatter: 'points', panel: 'panel', composite: 'composite' };
  const canon = (form) => { const f = String(form || '').toLowerCase(); return DRAWN[f] ? f : (ALIAS[f] || f); };

  /* ── the data a form draws ────────────────────────────────────────────── */
  // a capability result's SHAPE → its default form, deterministically (the Formats board's table)
  function formByShape(x, depth) {
    depth = depth || 0; if (depth > 2 || x == null) return null;
    if (Array.isArray(x)) {
      if (!x.length) return null;
      const o = x[0];
      if (o == null || typeof o !== 'object') return x.length > 1 && x.every((v) => typeof v === 'number') ? 'trace' : null;
      if (('t' in o || 'ts' in o || 'time' in o || 'x' in o) && ('v' in o || 'value' in o || 'y' in o)) return ('x' in o && 'y' in o && !('t' in o)) ? 'scatter' : 'trace';
      if (('t' in o || 'ts' in o || 'time' in o || 'when' in o) && ('text' in o || 'msg' in o || 'message' in o || 'line' in o)) return x.length > 20 ? 'lane' : 'log';
      if ('path' in o) return 'files';
      if ('done' in o || 'checked' in o) return 'checklist';
      if ('name' in o || 'id' in o || 'title' in o) return 'table';
      return Object.keys(o).length <= 8 ? 'table' : null;
    }
    if (typeof x === 'object') {
      if (typeof x.value === 'number' && ('min' in x || 'max' in x)) return 'radial';
      if (typeof x.value === 'number') return 'counter';
      if (Array.isArray(x.nodes) && (Array.isArray(x.links) || Array.isArray(x.edges))) return 'pipes';
      if (Array.isArray(x.stages) || Array.isArray(x.steps)) return 'stepper';
      const ks = Object.keys(x);
      if (ks.length >= 2 && ks.every((k) => typeof x[k] === 'number')) return ks.length > 8 ? 'heat' : 'thermo';
      for (const k of ['data', 'result', 'items', 'rows', 'points', 'series', 'history', 'values', 'entries', 'results']) if (x[k] != null) { const f = formByShape(x[k], depth + 1); if (f) return f; }
    }
    return null;
  }
  // the part of a result the form draws, dug out of the envelope the shape mapper saw through
  function dataFor(x, form, depth) {
    depth = depth || 0; if (x == null || depth > 2) return x;
    form = canon(form);
    if (Array.isArray(x)) return x;
    if (typeof x === 'object') {
      if ((form === 'radial' || form === 'counter' || form === 'bar') && typeof x.value === 'number') return x;
      if ((form === 'pipes' || form === 'context_graph') && Array.isArray(x.nodes)) return x;
      if (form === 'stepper' && (Array.isArray(x.stages) || Array.isArray(x.steps))) return x;
      const ks = Object.keys(x); if ((form === 'thermo' || form === 'heat' || form === 'bars' || form === 'donut' || form === 'pills' || form === 'kv' || form === 'stack') && ks.length >= 2 && ks.every((k) => typeof x[k] === 'number')) return x;
      for (const k of ['data', 'result', 'items', 'rows', 'points', 'series', 'history', 'values', 'entries', 'results']) if (x[k] != null) { const f = formByShape(x[k], depth + 1); if (f) return dataFor(x[k], form, depth + 1); }
      for (const k of ['data', 'result', 'items', 'rows', 'points', 'series', 'history', 'values', 'entries', 'results']) if (Array.isArray(x[k]) && !x[k].length) return x[k];   // an empty read is empty, not an envelope to list
    }
    return x;
  }
  const series = (d) => (Array.isArray(d) ? d : []).map((p) => typeof p === 'number' ? p : num(p && (p.v ?? p.value ?? p.y ?? p.close)));
  const keyed = (d) => (d && typeof d === 'object' && !Array.isArray(d)) ? Object.keys(d).filter((k) => typeof d[k] === 'number').map((k) => [k, d[k]]) : (Array.isArray(d) ? d.filter((r) => r && typeof r === 'object' && ['v', 'value', 'n', 'count'].some((k) => typeof r[k] === 'number')).map((r) => [String(r.name ?? r.k ?? r.key ?? r.label ?? ''), num(r.v ?? r.value ?? r.n ?? r.count)]).filter((kv) => kv[0]) : []);
  const rows = (d) => (Array.isArray(d) ? d : []).filter((r) => r && typeof r === 'object');
  const level = (d) => (d && typeof d === 'object' && !Array.isArray(d)) ? { v: num(d.value), lo: num(d.min ?? 0), hi: num(d.max ?? 100), unit: typeof d.unit === 'string' ? d.unit : '', delta: d.delta } : (typeof d === 'number' ? { v: d, lo: 0, hi: 100, unit: '' } : null);
  // nothing to draw: no result, an empty list or object, an empty string
  const isEmpty = (d) => d == null || d === '' || (Array.isArray(d) && !d.length) || (typeof d === 'object' && !Array.isArray(d) && !Object.keys(d).length);

  /* ── read.map: the source's envelope through the record's field mapping (the sheet's "← field" rows) ──────────
     A source answers in its own shape ({ok, history:[…]}, {nodes:[{hostname, load}]}); the record says which of its
     fields feed the form: the SHAPE's container fields pick a path out of the envelope (series ← history, rows ←
     data.nodes, value ← gpu.util, nodes ← topo.nodes), every other key renames a field inside each row (name ←
     hostname, value ← load). A path that resolves to nothing leaves the data as it was, so a map written for one
     source never blanks the sample face of another. */
  const SHAPE_FIELDS = { level: ['value', 'min', 'max', 'unit'], series: ['series'], values: ['values'], events: ['events'], graph: ['nodes', 'links'], items: ['rows'], stages: ['stages'], rate: ['rate', 'unit'],
    parts: ['parts'], ohlcv: ['bars'], matrix: ['cells'], calendar: ['days'], string: ['text'], points: ['points'], panel: ['panel'], composite: ['children'] };
  const CONTAINER = { series: 'series', values: 'values', events: 'events', items: 'rows', stages: 'stages', parts: 'parts', ohlcv: 'bars', matrix: 'cells', calendar: 'days', string: 'text', points: 'points', graph: 'nodes' };
  function pick(x, p) {
    if (x == null) return undefined; const path = String(p == null ? '' : p).trim(); if (!path || path === '$') return x;
    const parts = path.replace(/^\$\.?/, '').replace(/\[(-?\d+)\]/g, '.$1').split('.').filter(Boolean); let cur = x;
    for (const k of parts) { if (cur == null) return undefined; if (Array.isArray(cur) && /^-?\d+$/.test(k)) { const i = +k; cur = cur[i < 0 ? cur.length + i : i]; } else cur = cur[k]; }
    return cur;
  }
  function applyMap(x, map, shape) {
    if (!map || typeof map !== 'object' || x == null) return x; const keys = Object.keys(map).filter((k) => map[k] != null && map[k] !== ''); if (!keys.length) return x;
    const sh = String(shape || ''), fields = SHAPE_FIELDS[sh] || [], cont = CONTAINER[sh];
    if (sh === 'level' || sh === 'rate') { const o = (x && typeof x === 'object' && !Array.isArray(x)) ? Object.assign({}, x) : { value: x }; let hit = false;
      ['value', 'rate', 'min', 'max', 'unit', 'delta', 'trend'].forEach((k) => { if (map[k] == null) return; const v = pick(x, map[k]); if (v !== undefined) { o[k === 'rate' ? 'value' : k] = v; hit = true; } });
      return hit ? o : x; }
    let base = x, hit = false;
    if (sh === 'graph') { const o = Object.assign({}, (x && typeof x === 'object' && !Array.isArray(x)) ? x : {}); ['nodes', 'links'].forEach((k) => { if (map[k] == null) return; const v = pick(x, map[k]); if (v !== undefined) { o[k === 'links' ? 'links' : 'nodes'] = v; hit = true; } }); if (!hit) return x; base = o; }
    else if (cont && map[cont] != null) { const v = pick(x, map[cont]); if (v !== undefined) { base = v; hit = true; } }
    const renames = keys.filter((k) => !fields.includes(k) && k !== cont && !(sh === 'graph' && (k === 'nodes' || k === 'links')));
    if (renames.length && Array.isArray(base) && base.some((r) => r && typeof r === 'object')) { base = base.map((r) => { if (!r || typeof r !== 'object') return r; const o = Object.assign({}, r); renames.forEach((k) => { const v = pick(r, map[k]); if (v !== undefined) { o[k] = v; hit = true; } }); return o; }); }
    else if (renames.length && sh === 'graph' && base && Array.isArray(base.nodes)) { base = Object.assign({}, base, { nodes: base.nodes.map((r) => { if (!r || typeof r !== 'object') return r; const o = Object.assign({}, r); renames.forEach((k) => { const v = pick(r, map[k]); if (v !== undefined) { o[k] = v; hit = true; } }); return o; }) }); }
    return hit ? base : x;
  }
  // the record's read.map + read.range on the data a form is handed (the element and the sheet both go through here)
  function mapped(rec, form, data) {
    if (!rec || data === undefined) return data; const m = rec.read && rec.read.map, sh = DRAWN[canon(form)];
    let d = (m && typeof m === 'object' && Object.keys(m).length) ? applyMap(data, m, sh) : data;
    const rg = rec.read && rec.read.range;
    if (Array.isArray(rg) && rg.length === 2 && (sh === 'level' || sh === 'rate') && d && typeof d === 'object' && !Array.isArray(d)) d = Object.assign({}, d, { min: num(rg[0]), max: num(rg[1]) });
    return d;
  }

  /* ── the SAMPLE face: realistic data per shape, so every form has a face before it has read anything (the pickers'
     previews, a record placed without a source, a source that failed) — deterministic, marked "sample" when drawn ── */
  const wave = (n, base, amp) => Array.from({ length: n }, (_, i) => ({ t: i, v: Math.round((base + Math.sin(i / 2.1) * amp + Math.cos(i / 3.7) * amp * 0.5) * 10) / 10 }));
  const T0 = '2026-09-13T14:';
  const SAMPLE = {
    level: () => ({ value: 62, min: 0, max: 100, unit: '%', delta: 4 }),
    rate: () => ({ value: 41, unit: 'tok/s', delta: 3 }),
    series: () => wave(24, 48, 22),
    values: () => ({ ct126: 62, ct121: 41, ct118: 18, ct130: 74, gate: 55, embed: 33 }),
    parts: () => ({ llm: 46, embed: 22, tools: 18, idle: 14 }),
    events: () => [['41:02', 'INFO', 'value 0.62 · gate lease held'], ['38:40', 'INFO', 'pull skipped · resident'], ['36:11', 'WARN', 'ct130 over 70° · throttle line'], ['31:05', 'INFO', 'source attached · sysmon.status'], ['28:52', 'LOOP', 'step 5 waiting on you'], ['22:19', 'INFO', 'digest 4 of 4 boots']].map((r) => ({ t: T0 + r[0], kind: r[1], text: r[2] })),
    items: () => [['ct126', 'serving', 62, '14:41'], ['ct121', 'ok', 41, '14:38'], ['ct118', 'idle', 18, '14:31'], ['ct130', 'down', 0, '13:52'], ['ct122', 'ok', 55, '14:40']].map((r) => ({ name: r[0], status: r[1], value: r[2], when: r[3] })),
    stages: () => ({ stages: [{ name: 'plan', done: true }, { name: 'gather', done: true }, { name: 'act', current: true }, { name: 'verify' }, { name: 'land' }] }),
    graph: () => ({ nodes: [['ct126', 'ct126', 'run', 0.92], ['fabric', 'fabric.py', 'vector', 0.84], ['recall', 'lease recall', 'memory', 0.61], ['gate', 'gate.load', 'cap', 0.72], ['digest', 'digest note', 'fabric', 0.48], ['plan', 'plan · step 5', 'plan', 0.55], ['web', 'ollama docs', 'web', 0.31]].map((n) => ({ id: n[0], label: n[1], family: n[2], source: n[2], score: n[3], included: n[3] > 0.4 })),
      links: [['ct126', 'gate', 'cite'], ['fabric', 'digest', 'cite'], ['recall', 'fabric', 'mem'], ['plan', 'ct126', 'read'], ['gate', 'plan', 'cite']].map((l) => ({ source: l[0], target: l[1], from: l[0], to: l[1], kind: l[2], label: l[2] })) }),
    ohlcv: () => Array.from({ length: 12 }, (_, i) => { const o = 100 + Math.sin(i / 1.7) * 6, c = o + Math.cos(i / 1.3) * 4; return { t: i, open: Math.round(o * 10) / 10, high: Math.round((Math.max(o, c) + 2) * 10) / 10, low: Math.round((Math.min(o, c) - 2) * 10) / 10, close: Math.round(c * 10) / 10, volume: 40 + i * 7 }; }),
    matrix: () => ({ ct126: { api: 'ok', db: 'ok', gpu: 'warn', disk: 'ok' }, ct121: { api: 'ok', db: 'ok', gpu: 'ok', disk: 'ok' }, ct118: { api: 'ok', db: 'down', gpu: 'ok', disk: 'warn' }, ct130: { api: 'down', db: 'down', gpu: 'down', disk: 'ok' } }),
    calendar: () => [['13T09:00', 'digest'], ['13T11:30', 'sweep · fabric'], ['13T14:00', 'loop v7 · step 5'], ['14T09:00', 'digest'], ['14T16:00', 'benchmark'], ['15T10:00', 'review']].map((r) => ({ when: '2026-09-' + r[0], title: r[1] })),
    string: () => 'ct126 is serving qwen3:30b · 4 in flight · step 5 waiting on you',
    points: () => Array.from({ length: 14 }, (_, i) => ({ x: 10 + i * 6 + (i % 3) * 2, y: 20 + Math.sin(i / 2) * 14 + i * 2, label: 'm' + i })),
    panel: () => ({ panel: 'system-monitor' }),
    composite: () => ({ layout: '2x2', children: [{ slot: 'a', record: { form: 'radial', title: 'Gate', data: SAMPLE.level() } }, { slot: 'b', record: { form: 'trace', title: 'Latency', data: SAMPLE.series() } }, { slot: 'c', record: { form: 'log', title: 'Events', data: SAMPLE.events() } }, { slot: 'd', record: { form: 'thermo', title: 'Temperature', data: SAMPLE.values() } }] }),
  };
  // a form whose face wants more than its shape's sample gives
  const FORM_SAMPLE = {
    files: () => [['/srv/vera/fabric.py', '12 KB', '14:41'], ['/srv/vera/gate.py', '4 KB', '14:38'], ['/notes/42-plan.md', '9 KB', '13:02'], ['/out/report.html', '31 KB', '12:48']].map((r) => ({ path: r[0], size: r[1], changed: r[2] })),
    checklist: () => [{ text: 'gate passed', done: true }, { text: 'sweep the estate', done: true }, { text: 'verify on the mirror' }, { text: 'land', due: 'today' }],
    kv: () => ({ status: 'serving', node: 'ct126', model: 'qwen3:30b', in_flight: 4, waiting: 'step 5' }),
    pills: () => [['redis', 'ok'], ['neo4j', 'ok'], ['ollama', 'running'], ['ct130', 'down'], ['gate', 'ok']].map((r) => ({ name: r[0], status: r[1] })),
    heat: () => ({ ct126: 62, ct121: 41, ct118: 18, ct130: 74, ct122: 55, ct119: 33, ct127: 48, ct131: 27, ct120: 66, ct123: 12 }),
    context_graph: () => { const g = SAMPLE.graph(); return { nodes: g.nodes, rels: g.links.map((l) => ({ from: l.from, to: l.to, kind: l.kind })) }; },
    lane: () => SAMPLE.events(),
  };
  // sample(formOrShape): the sample a form draws — a shape name gives the shape's, a form id its own (or its shape's)
  function sample(x) {
    const k = String(x || '').toLowerCase();
    if (SAMPLE[k] && !DRAWN[k]) return SAMPLE[k]();
    const f = canon(k); if (FORM_SAMPLE[f]) return FORM_SAMPLE[f]();
    const sh = DRAWN[f] || k; return (SAMPLE[sh] || SAMPLE.string)();
  }
  // the sample face, marked — a class on the inline sizes, a tag on the cell sizes
  const sampleFace = (html, size) => (size === 'xs' || size === 's') ? html.replace(/^<span class="(vw-xs|vw-chip)"/, '<span class="$1 vw-sampled" data-sample="1"') : '<div class="vw-sampled" data-sample="1">' + html + '<i class="vw-sampletag" title="sample data — nothing read yet">sample</i></div>';
  // the one figure a size below M shows
  function figure(form, data) {
    form = canon(form); const d = dataFor(data, form);
    if (d == null) return '';
    if (form === 'radial' || form === 'counter' || form === 'bar') { const l = level(d); return l ? fmt(l.v) + esc(l.unit || ((form === 'radial' && l.lo === 0 && l.hi === 100) ? '%' : '')) : ''; }
    if (form === 'trace' || form === 'scatter') { const s = series(d); return s.length ? fmt(s[s.length - 1]) : ''; }
    if (form === 'thermo' || form === 'heat' || form === 'bars' || form === 'donut' || form === 'pills' || form === 'kv' || form === 'stack' || form === 'matrix') { const kv = keyed(d); return kv.length ? kv.length + ' · ' + esc(kv[0][0]) + ' ' + fmt(kv[0][1]) : ''; }
    if (form === 'stepper') { const st = (d.stages || d.steps || d); const arr = Array.isArray(st) ? st : []; const done = arr.filter((s) => s && (s.done || s.state === 'done' || s.status === 'done')).length; return arr.length ? done + ' / ' + arr.length : ''; }
    if (form === 'string') return esc(String(typeof d === 'string' ? d : (d.text ?? d.value ?? '')).slice(0, 24));
    const r = rows(d); return r.length ? r.length + ' rows' : (typeof d === 'string' ? esc(d.slice(0, 24)) : '');
  }

  /* ── the renderers, at M (the gallery's unit); L and XL compose around them ─ */
  const R = {};
  R.trace = (d, H, o) => {
    const pts = series(d).slice(-120);
    if (pts.length < 2) return EMPTY(pts.length + ' point' + (pts.length === 1 ? '' : 's') + ' · a trace needs two');
    const lo = Math.min(...pts), hi = Math.max(...pts), sp = (hi - lo) || 1, W = 300;
    const xy = pts.map((v, i) => [(i / (pts.length - 1)) * W, (H - 4) - ((v - lo) / sp) * (H - 8)]);
    const line = xy.map((p) => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
    const bands = (o && o.draw && Array.isArray(o.draw.bands)) ? o.draw.bands.map((b) => { const y = (H - 4) - ((num(b) - lo) / sp) * (H - 8); return '<line x1="0" x2="' + W + '" y1="' + y.toFixed(1) + '" y2="' + y.toFixed(1) + '" stroke="var(--warn,#c9a35a)" stroke-dasharray="3 3" stroke-width="1" vector-effect="non-scaling-stroke"/>'; }).join('') : '';
    return '<svg class="vw-svg" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" style="height:' + H + 'px"><path d="M0,' + H + ' L' + line.split(' ').join(' L') + ' L' + W + ',' + H + ' Z" fill="var(--acc,#5a9e8f)" fill-opacity=".12"/>' + bands + '<polyline points="' + line + '" fill="none" stroke="var(--acc,#5a9e8f)" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg>';
  };
  R.radial = (d, H) => {
    const l = level(d); if (!l) return EMPTY('a ring needs { value, min, max }');
    const f = Math.max(0, Math.min(1, (l.v - l.lo) / ((l.hi - l.lo) || 1)));
    const r = Math.max(16, (H - 8) / 2), c = 2 * Math.PI * r, s = H / 2 + 2, unit = l.unit || ((l.hi === 100 && l.lo === 0) ? '%' : '');
    return '<svg class="vw-svg" viewBox="0 0 ' + (s * 2) + ' ' + (s * 2) + '" style="height:' + H + 'px;width:auto"><circle cx="' + s + '" cy="' + s + '" r="' + r + '" fill="none" stroke="var(--bg2,#1a1c20)" stroke-width="6"/><circle cx="' + s + '" cy="' + s + '" r="' + r + '" fill="none" stroke="var(--acc,#5a9e8f)" stroke-width="6" stroke-dasharray="' + (f * c).toFixed(1) + ' ' + (c - f * c).toFixed(1) + '" transform="rotate(-90 ' + s + ' ' + s + ')" stroke-linecap="round"/><text x="' + s + '" y="' + s + '" text-anchor="middle" dominant-baseline="central" font-size="' + Math.max(10, r * 0.55) + '" fill="var(--text,#d8dce4)" font-family="var(--mono,ui-monospace,monospace)">' + esc(fmt(l.v)) + esc(unit) + '</text></svg>';
  };
  R.counter = (d, H) => {
    const l = level(d); if (!l) return EMPTY('a counter needs a value');
    const dl = l.delta != null ? '<span class="vw-delta ' + (num(l.delta) >= 0 ? 'up' : 'down') + '">' + (num(l.delta) >= 0 ? '▲' : '▼') + ' ' + esc(fmt(Math.abs(num(l.delta)))) + '</span>' : '';
    return '<div class="vw-hero" style="min-height:' + Math.min(H, 60) + 'px"><b>' + esc(fmt(l.v)) + '</b><span class="vw-unit">' + esc(l.unit) + '</span>' + dl + '</div>';
  };
  R.bar = (d, H) => {
    const l = level(d); if (!l) return EMPTY('a meter needs { value, min, max }');
    const f = Math.max(0, Math.min(1, (l.v - l.lo) / ((l.hi - l.lo) || 1)));
    return '<div class="vw-meter"><span class="vw-track"><i style="width:' + (f * 100).toFixed(1) + '%"></i></span><b>' + esc(fmt(l.v)) + esc(l.unit || ((l.hi === 100 && l.lo === 0) ? '%' : '')) + '</b>' + (l.delta != null ? '<small class="vw-delta ' + (num(l.delta) >= 0 ? 'up' : 'down') + '">' + (num(l.delta) >= 0 ? '▲' : '▼') + esc(fmt(Math.abs(num(l.delta)))) + '</small>' : '') + '</div>';
  };
  R.bars = (d, H, o) => {
    let kv = keyed(d); if (!kv.length) return EMPTY('no numbers to draw');
    if (o && o.draw && o.draw.sort !== false) kv = kv.slice().sort((a, b) => b[1] - a[1]);
    kv = kv.slice(0, (o && o.draw && o.draw.limit) || 12);
    const hi = Math.max(...kv.map((x) => Math.abs(x[1]))) || 1, W = 300, bw = W / kv.length;
    return '<svg class="vw-svg" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" style="height:' + H + 'px">' + kv.map((x, i) => { const h = Math.max(1, (Math.abs(x[1]) / hi) * (H - 14)); return '<rect x="' + (i * bw + 2).toFixed(1) + '" y="' + (H - 12 - h).toFixed(1) + '" width="' + Math.max(1, bw - 4).toFixed(1) + '" height="' + h.toFixed(1) + '" rx="2" fill="var(--acc,#5a9e8f)"><title>' + esc(x[0]) + ' · ' + esc(fmt(x[1])) + '</title></rect><text x="' + (i * bw + bw / 2).toFixed(1) + '" y="' + (H - 2) + '" text-anchor="middle" font-size="8" fill="var(--dim2,#8a92a0)" font-family="var(--mono,monospace)">' + esc(String(x[0]).slice(0, 6)) + '</text>'; }).join('') + '</svg>';
  };
  R.thermo = (d) => {
    const kv = keyed(d); if (!kv.length) return EMPTY('no numbers to draw');
    const hi = Math.max(...kv.map((x) => Math.abs(x[1]))) || 1;
    return '<div class="vw-therm">' + kv.slice(0, 8).map((x) => '<div><label title="' + esc(x[0]) + '">' + esc(x[0]) + '</label><span><i style="width:' + (100 * Math.abs(x[1]) / hi).toFixed(1) + '%"></i></span><b>' + esc(fmt(x[1])) + '</b></div>').join('') + '</div>';
  };
  R.heat = (d) => {
    const kv = keyed(d); if (!kv.length) return EMPTY('no numbers to draw');
    const hi = Math.max(...kv.map((x) => Math.abs(x[1]))) || 1, cols = Math.min(6, Math.max(3, Math.ceil(Math.sqrt(kv.length))));
    return '<div class="vw-heat" style="grid-template-columns:repeat(' + cols + ',1fr)">' + kv.slice(0, 48).map((x) => '<div style="opacity:' + (0.2 + 0.8 * Math.abs(x[1]) / hi).toFixed(2) + '" title="' + esc(x[0]) + ' · ' + esc(fmt(x[1])) + '">' + esc(x[0]) + '</div>').join('') + '</div>';
  };
  R.matrix = (d) => {
    // {row: {col: value}} or [{name, a, b, c}] → a grid of cells coloured by value (a status matrix)
    let rws = [], cols = [];
    if (d && typeof d === 'object' && !Array.isArray(d)) { rws = Object.keys(d).filter((k) => d[k] && typeof d[k] === 'object').map((k) => [k, d[k]]); }
    else rows(d).forEach((r) => rws.push([String(r.name ?? r.id ?? ''), r]));
    if (!rws.length) return EMPTY('a matrix needs rows of values');
    cols = Object.keys(rws[0][1]).filter((c) => typeof rws[0][1][c] !== 'object' && c !== 'name' && c !== 'id').slice(0, 10);
    const val = (v) => typeof v === 'number' ? v : (v === true || /^(ok|up|green|pass)$/i.test(String(v)) ? 1 : (v === false || /^(down|fail|red|error)$/i.test(String(v)) ? 0 : 0.5));
    return '<div class="vw-matrix" style="grid-template-columns:auto repeat(' + cols.length + ',1fr)"><i></i>' + cols.map((c) => '<i>' + esc(c) + '</i>').join('') + rws.slice(0, 12).map((r) => '<b>' + esc(r[0]) + '</b>' + cols.map((c) => { const v = val(r[1][c]); return '<span style="background:color-mix(in srgb,var(--acc,#5a9e8f) ' + Math.round(v * 80 + 10) + '%,var(--bg2,#1a1c20))" title="' + esc(r[0]) + ' · ' + esc(c) + ' · ' + esc(String(r[1][c])) + '"></span>'; }).join('')).join('') + '</div>';
  };
  R.donut = (d, H) => {
    const kv = keyed(d).slice(0, 8); if (!kv.length) return EMPTY('parts need { name: number }');
    const tot = kv.reduce((s, x) => s + Math.abs(x[1]), 0) || 1, r = Math.max(14, (H - 8) / 2), c = 2 * Math.PI * r, s = H / 2 + 2; let acc = 0;
    const cols = ['var(--acc,#5a9e8f)', 'var(--acc2,#8fb87a)', 'var(--acc3,#d4a96a)', '#a78bfa', '#e07a9a', '#5ab0d8', '#c9a35a', '#7ac9b0'];
    const arcs = kv.map((x, i) => { const f = Math.abs(x[1]) / tot; const el = '<circle cx="' + s + '" cy="' + s + '" r="' + r + '" fill="none" stroke="' + cols[i % cols.length] + '" stroke-width="8" stroke-dasharray="' + Math.max(0, f * c - 2).toFixed(1) + ' ' + (c - f * c + 2).toFixed(1) + '" stroke-dashoffset="' + (-acc * c).toFixed(1) + '" transform="rotate(-90 ' + s + ' ' + s + ')"><title>' + esc(x[0]) + ' · ' + esc(fmt(x[1])) + '</title></circle>'; acc += f; return el; }).join('');
    return '<div class="vw-donut"><svg class="vw-svg" viewBox="0 0 ' + (s * 2) + ' ' + (s * 2) + '" style="height:' + H + 'px;width:auto">' + arcs + '</svg><div class="vw-legend">' + kv.map((x, i) => '<span><i style="background:' + cols[i % cols.length] + '"></i>' + esc(x[0]) + '<b>' + esc(fmt(x[1])) + '</b></span>').join('') + '</div></div>';
  };
  R.stack = (d) => {
    const kv = keyed(d).slice(0, 8); if (!kv.length) return EMPTY('parts need { name: number }');
    const tot = kv.reduce((s, x) => s + Math.abs(x[1]), 0) || 1;
    const cols = ['var(--acc,#5a9e8f)', 'var(--acc2,#8fb87a)', 'var(--acc3,#d4a96a)', '#a78bfa', '#e07a9a', '#5ab0d8', '#c9a35a', '#7ac9b0'];
    return '<div class="vw-stack"><div class="vw-stackbar">' + kv.map((x, i) => '<i style="width:' + (100 * Math.abs(x[1]) / tot).toFixed(1) + '%;background:' + cols[i % cols.length] + '" title="' + esc(x[0]) + ' · ' + esc(fmt(x[1])) + '"></i>').join('') + '</div><div class="vw-legend">' + kv.map((x, i) => '<span><i style="background:' + cols[i % cols.length] + '"></i>' + esc(x[0]) + '<b>' + esc(fmt(x[1])) + '</b></span>').join('') + '</div></div>';
  };
  R.pills = (d) => {
    const kv = keyed(d); const st = rows(d);
    if (kv.length) return '<div class="vw-pills">' + kv.slice(0, 12).map((x) => '<span class="vw-pill"><small>' + esc(x[0]) + '</small><b>' + esc(fmt(x[1])) + '</b></span>').join('') + '</div>';
    if (st.length) return '<div class="vw-pills">' + st.slice(0, 12).map((r) => { const s = String(r.status ?? r.state ?? r.value ?? ''); const ok = /^(ok|up|green|pass|running|healthy)$/i.test(s); const bad = /^(down|fail|red|error|stopped)$/i.test(s); return '<span class="vw-pill ' + (ok ? 'ok' : bad ? 'bad' : '') + '"><small>' + esc(String(r.name ?? r.id ?? r.label ?? '')) + '</small><b>' + esc(s) + '</b></span>'; }).join('') + '</div>';
    return EMPTY('pills need { name: number } or rows with a status');
  };
  const logLine = (r) => '<div><span class="t">' + esc(String(r.t ?? r.ts ?? r.time ?? r.when ?? '').slice(11, 19) || String(r.t ?? r.ts ?? '').slice(0, 8)) + '</span><span class="k">' + esc(String(r.kind ?? r.level ?? r.type ?? '')) + '</span><span>' + esc(String(r.text ?? r.msg ?? r.message ?? r.line ?? r.title ?? '')) + '</span></div>';
  R.log = (d) => {
    const rw = rows(d); if (!rw.length || !rw.some((r) => r.text != null || r.msg != null || r.message != null || r.line != null || r.title != null)) return EMPTY('a log needs rows with text');
    return '<div class="vw-log">' + rw.slice(-12).map(logLine).join('') + '</div>';
  };
  R.lane = (d) => {
    const rw = rows(d); if (!rw.length || !rw.some((r) => r.text != null || r.msg != null || r.message != null || r.line != null)) return EMPTY('a log needs rows with text');
    const by = {}; rw.forEach((r) => { const k = String(r.kind ?? r.level ?? r.type ?? 'other'); (by[k] = by[k] || []).push(r); });
    return '<div class="vw-log">' + Object.keys(by).slice(0, 6).map((k) => '<div class="lane">' + esc(k) + ' · ' + by[k].length + '</div>' + by[k].slice(-4).map(logLine).join('')).join('') + '</div>';
  };
  R.table = (d, H, o) => {
    const rw = rows(d); if (!rw.length) return EMPTY('no rows');
    const want = (o && o.draw && Array.isArray(o.draw.columns)) ? o.draw.columns : null;
    const cols = (want || Object.keys(rw[0]).filter((k) => typeof rw[0][k] !== 'object')).slice(0, 6);
    return '<div class="vw-tablewrap" style="max-height:' + Math.max(H, 60) + 'px"><table><thead><tr>' + cols.map((c) => '<th>' + esc(c) + '</th>').join('') + '</tr></thead><tbody>' + rw.slice(0, (o && o.draw && o.draw.limit) || 12).map((r) => '<tr>' + cols.map((c) => '<td title="' + esc(String(r[c] ?? '')) + '">' + esc(String(r[c] ?? '')) + '</td>').join('') + '</tr>').join('') + '</tbody></table></div>';
  };
  R.files = (d, H, o) => { const rw = rows(d); if (!rw.length) return EMPTY('no rows'); if (!rw.some((r) => r.path != null)) return EMPTY('files need a path field'); return R.table(rw, H, { draw: { columns: ['path'].concat(Object.keys(rw[0]).filter((k) => k !== 'path' && typeof rw[0][k] !== 'object').slice(0, 4)) } }); };
  R.list = (d) => {
    const rw = rows(d); const str = (Array.isArray(d) ? d : []).filter((x) => typeof x === 'string');
    if (str.length) return '<div class="vw-list">' + str.slice(0, 12).map((s) => '<div><b>' + esc(s) + '</b></div>').join('') + '</div>';
    if (!rw.length) return EMPTY('no rows');
    return '<div class="vw-list">' + rw.slice(0, 12).map((r) => '<div><b>' + esc(String(r.name ?? r.title ?? r.label ?? r.id ?? '')) + '</b><small>' + esc(String(r.meta ?? r.when ?? r.status ?? r.count ?? r.value ?? '')) + '</small></div>').join('') + '</div>';
  };
  R.checklist = (d) => {
    const rw = rows(d); if (!rw.length) return EMPTY('a checklist needs items');
    return '<div class="vw-check">' + rw.slice(0, 12).map((r) => { const on = !!(r.done || r.checked || r.state === 'done' || r.status === 'done'); return '<div class="' + (on ? 'on' : '') + '"><i>' + (on ? '☑' : '☐') + '</i><span>' + esc(String(r.text ?? r.title ?? r.name ?? '')) + '</span>' + (r.due || r.when ? '<small>' + esc(String(r.due ?? r.when)) + '</small>' : '') + '</div>'; }).join('') + '</div>';
  };
  R.stepper = (d) => {
    const arr = Array.isArray(d) ? d : (d && (d.stages || d.steps)) || []; if (!arr.length) return EMPTY('stages need a list');
    return '<div class="vw-steps">' + arr.slice(0, 12).map((s, i) => { const o = (s && typeof s === 'object') ? s : { name: String(s) }; const st = o.done || o.state === 'done' || o.status === 'done' ? 'done' : (o.current || o.state === 'running' || o.status === 'running' || o.state === 'current' ? 'now' : (o.state === 'failed' || o.status === 'failed' || o.error ? 'bad' : '')); return '<div class="' + st + '"><i>' + (i + 1) + '</i><span>' + esc(String(o.name ?? o.title ?? o.step ?? '')) + '</span>' + (o.when || o.ms != null ? '<small>' + esc(String(o.when ?? (o.ms + ' ms'))) + '</small>' : '') + '</div>'; }).join('') + '</div>';
  };
  R.calendar = (d) => {
    const rw = rows(d); if (!rw.length) return EMPTY('a calendar needs events');
    const day = (r) => String(r.when ?? r.start ?? r.t ?? r.date ?? '').slice(0, 10) || 'undated';
    const by = {}; rw.forEach((r) => (by[day(r)] = by[day(r)] || []).push(r));
    return '<div class="vw-cal">' + Object.keys(by).sort().slice(0, 6).map((k) => '<div class="day"><b>' + esc(k) + '</b>' + by[k].slice(0, 5).map((r) => '<span><small>' + esc(String(r.when ?? r.start ?? r.t ?? '').slice(11, 16)) + '</small>' + esc(String(r.title ?? r.name ?? r.text ?? '')) + '</span>').join('') + '</div>').join('') + '</div>';
  };
  R.string = (d) => { const s = typeof d === 'string' ? d : (d && typeof d === 'object' ? String(d.text ?? d.value ?? d.message ?? JSON.stringify(d)) : String(d)); return s ? '<div class="vw-str">' + esc(s.slice(0, 2000)) + '</div>' : EMPTY('nothing to show'); };
  R.kv = (d) => {
    const o = (d && typeof d === 'object' && !Array.isArray(d)) ? d : null; if (!o) return EMPTY('nothing to list');
    const ks = Object.keys(o).filter((k) => typeof o[k] !== 'object' || o[k] === null).slice(0, 10); if (!ks.length) return EMPTY('no plain values to list');
    return '<div class="vw-log vw-kv">' + ks.map((k) => '<div><span class="k">' + esc(k) + '</span><span>' + esc(String(o[k])) + '</span></div>').join('') + '</div>';
  };
  R.scatter = (d, H) => {
    const pts = rows(d).map((p) => [num(p.x ?? p.t ?? p[0]), num(p.y ?? p.v ?? p.value ?? p[1]), num(p.size ?? p.r ?? 3)]); if (pts.length < 2) return EMPTY('a scatter needs points');
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]); const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys), W = 300;
    return '<svg class="vw-svg" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" style="height:' + H + 'px">' + pts.slice(0, 400).map((p) => '<circle cx="' + (((p[0] - x0) / ((x1 - x0) || 1)) * (W - 12) + 6).toFixed(1) + '" cy="' + ((H - 6) - ((p[1] - y0) / ((y1 - y0) || 1)) * (H - 12)).toFixed(1) + '" r="' + Math.max(2, Math.min(8, p[2] || 3)) + '" fill="var(--acc,#5a9e8f)" fill-opacity=".7"/>').join('') + '</svg>';
  };
  // the CONTEXT GRAPH (the chat's own graph, the Graph board): a lane per family — context · memory · loop · plan ·
  // canvas — its members scattered by relevance (left = most relevant), hollow where related but not injected, the
  // relations drawn between them. Data: {nodes:[{id, label, lane?, kind?, source?, score, col?, included?}],
  // rels:[{from, to, kind}]}. S–L draw this mini graph; XL hands the data to the full <vera-context-graph> (hydrate).
  const CG_LANES = [['context', 'context', 'var(--dv1,#a78bfa)'], ['memory', 'memory', 'var(--acc2,#5ec9a0)'], ['loop', 'loop', 'var(--acc,#5a9e8f)'], ['plan', 'plan', 'var(--dim,#8a92a0)'], ['canvas', 'canvas', 'var(--dv2,#c9955a)']];
  const cgLane = (n) => { if (n.lane) return String(n.lane); const k = String(n.kind || n.source || '').toLowerCase(); if (/memory|recall/.test(k)) return 'memory'; if (/loop|step|run|cap$/.test(k)) return 'loop'; if (/plan|goal/.test(k)) return 'plan'; if (/canvas|land|pin/.test(k)) return 'canvas'; return 'context'; };
  // THE MINI CONTEXT GRAPH — the Canvas board's "context galaxy", as a widget form so the same drawing sits in the
  // Context menu, on an exploded plate, on the canvas or a dashboard: every FAMILY of the assembled context on a ring,
  // a record's relevance as its distance from the hub, the family's colour, its label at the outer record, the edges the
  // records cite. opts.view galaxy · iso · flow · timeline; opts.allEdges draws every record to the hub; opts.off = {family:true}
  // dims a family; opts.color(family) overrides the palette; opts.width the plate (262 by default); opts.layout 'lanes' keeps
  // the lane drawing. XL is the full <vera-context-graph> (hydrate() mounts it).
  const CG_FAM = { agent:['Agent + system','var(--t3,#6b7280)'], skill:['Skills','var(--dv6,#c96b6b)'], ontology:['Ontologies','var(--dv4,#c9955a)'], memory:['Memory recalls','var(--dv2,#5ec9a0)'],
    vector:['Vector matches','var(--dv1,#a78bfa)'], graph:['Graph (Neo4j)','var(--dv7,#fb923c)'], fabric:['Fabric records','var(--dv3,#38bdf8)'], cap:['Capabilities','var(--dv5,#ec4899)'], web:['Web','#f59e0b'],
    news:['News','#e879f9'], run:['Runs','var(--acc,#5a9e8f)'], related_qa:['Related Q&A','#8fb87a'], worldview:['Worldview','#9e8fa0'], entities:['Entities','#c9a35a'], urls:['URLs','#7dd3fc'],
    both:['Vector + graph','#8fb87a'], plan:['Plan','#8a92a0'], canvas:['Canvas','#c9955a'], loop:['Loop','#5a9e8f'], context:['Context','#a78bfa'] };
  const cgFam = (n) => { const k = String(n.family || n.source || n.kind || 'context').toLowerCase(); if (CG_FAM[k]) return k; if (/memory|recall/.test(k)) return 'memory'; if (/loop|step|run/.test(k)) return 'run'; if (/cap/.test(k)) return 'cap'; return k; };
  const cgFamLabel = (f) => (CG_FAM[f] || [f])[0];
  R.context_graph = (d, H, opts) => {
    opts = opts || {}; const nodes = ((d && d.nodes) || []).filter((n) => n && (n.id || n.label)).slice(0, 80); const rels = ((d && (d.rels || d.links || d.edges)) || []).slice(0, 160);
    if (!nodes.length) return EMPTY('no context yet');
    if (opts.size === 'xl' && opts.full !== false) return '<div class="vw-cgfull" data-cg="' + esc(JSON.stringify({ nodes, rels })) + '" style="position:relative;height:100%;min-height:' + Math.max(H, 220) + 'px"><small class="wempty">context graph · ' + nodes.length + ' records</small></div>';
    if (opts.layout === 'lanes') return cgLanes(nodes, rels, H, opts);
    const V = opts.view || 'galaxy', OFF = opts.off || {}, colorOf = typeof opts.color === 'function' ? opts.color : null;
    const W = opts.width || 262, K = Math.max(.5, Math.min(1.6, H / 196)), cx = W / 2, cy = H / 2 + 6 * K, RAD = Math.PI / 180;
    const fams = [], byF = {}; nodes.forEach((n) => { const f = cgFam(n); if (!byF[f]) { byF[f] = []; fams.push(f); } byF[f].push(n); });
    const sc = (n) => Math.max(0, Math.min(1, +(n.score == null ? .5 : n.score)));
    fams.forEach((f) => byF[f].sort((a, b) => sc(b) - sc(a)));
    const iso = (x, y, z) => { const A = 32 * RAD, dx = x - cx, dy = y - cy; return [cx + dx * Math.cos(A) - dy * Math.sin(A), cy + (dx * Math.sin(A) + dy * Math.cos(A)) * .56 - z]; };
    const px = (v) => v.toFixed(1) + 'px';
    const P = {}, dots = [], labels = [], stems = [], edges = [];
    fams.forEach((f, si) => { const col = (colorOf && colorOf(f)) || (CG_FAM[f] || [])[1] || 'var(--dim2,#8a92a0)'; const on = !OFF[f];
      const list = byF[f].slice(0, 5), N = list.length, a0 = -90 + si * (360 / fams.length);
      list.forEach((n, k) => { const rel = sc(n), id = String(n.id || n.label); let x, y, z = 0;
        if (V === 'flow') { x = 18 + (si + .5) * ((W - 36) / Math.max(1, fams.length)); y = (24 + k * 26) * K; }
        else if (V === 'timeline') { x = 30 + (1 - rel) * (W - 80) + (si % 3) * 6; y = (14 + (si + .5) * ((H - 28) / Math.max(1, fams.length)) / K) * K; }
        else { const r = (28 + k * 20 + (1 - rel) * 8) * K, a = (a0 + (k - (N - 1) / 2) * 11) * RAD; x = cx + Math.cos(a) * r; y = cy + Math.sin(a) * r;
          if (V === 'iso') { const tw = (r / (80 * K)) * 40 * RAD, a2 = a + tw; const gx = cx + Math.cos(a2) * r, gy = cy + Math.sin(a2) * r; z = rel * 22 * K; const q = iso(gx, gy, z); x = q[0]; y = q[1]; if (on && z > 4) { const q0 = iso(gx, gy, 0); stems.push({ x:q0[0], y:q0[1] - z, h:z }); } } }
        P[id] = [x, y];
        dots.push({ id, x, y, col, op:on ? (0.45 + rel * .55) : 0.12, lit:rel > .8 && n.included !== false, hollow:n.included === false, t:(n.label || n.id) + ' · ' + cgFamLabel(f) + ' · relevance ' + rel.toFixed(2) + (n.included === false ? ' · related, not injected' : '') });
        if (k === N - 1) labels.push({ n:cgFamLabel(f).replace(' + system', '').replace(' matches', '').replace(' recalls', '').replace(' records', ''), col, x:x + (V === 'flow' ? -6 : 8), y:y + (V === 'flow' ? 12 : -4), on }); }); });
    const hub = V === 'flow' ? [cx, H - 12] : V === 'timeline' ? [14, H - 10] : V === 'iso' ? iso(cx, cy, 16 * K) : [cx, cy];
    if (V === 'iso') { const h0 = iso(cx, cy, 0); stems.push({ x:h0[0], y:h0[1] - 16 * K, h:16 * K }); }
    const rings = (V === 'galaxy' || V === 'iso') ? [28, 50, 72].map((r) => { const c = V === 'iso' ? iso(cx, cy, 0) : [cx, cy]; return { x:c[0], y:c[1], d:r * 2 * K }; }) : [];
    const seg = (a, b, col, cls) => { const dx = b[0] - a[0], dy = b[1] - a[1]; edges.push({ x:a[0], y:a[1], len:Math.hypot(dx, dy), deg:Math.atan2(dy, dx) * 180 / Math.PI, col, cls }); };
    const colOfId = {}; dots.forEach((p) => { colOfId[p.id] = p.col; });
    rels.forEach((e) => { const a = P[String(e.from)], b = P[String(e.to)]; if (a && b) seg(a, b, colOfId[String(e.from)] || 'var(--dim2,#8a92a0)', ''); });
    if (opts.allEdges) dots.forEach((p) => { if (p.op > .2) seg([p.x, p.y], hub, p.col, 'faint'); });
    let h = '<div class="vw-gal vw-gal-' + esc(V) + '" style="position:relative;height:' + H + 'px;overflow:hidden"><div style="position:absolute;left:50%;top:0;width:' + W + 'px;height:' + H + 'px;margin-left:' + (-W / 2) + 'px">';
    rings.forEach((g) => { h += '<span class="vw-gring" style="left:' + px(g.x) + ';top:' + px(g.y) + ';width:' + px(g.d) + ';height:' + px(g.d) + '"></span>'; });
    stems.forEach((s) => { h += '<span class="vw-gstem" style="left:' + px(s.x) + ';top:' + px(s.y) + ';height:' + px(s.h) + '"></span>'; });
    edges.forEach((e) => { h += '<span class="vw-gedge' + (e.cls ? ' ' + e.cls : '') + '" style="left:' + px(e.x) + ';top:' + px(e.y) + ';width:' + px(e.len) + ';transform:rotate(' + e.deg.toFixed(1) + 'deg);--c:' + e.col + '"></span>'; });
    dots.forEach((p) => { h += '<span class="vw-gd' + (p.lit ? ' lit' : '') + (p.hollow ? ' hollow' : '') + '" data-id="' + esc(p.id) + '" style="left:' + px(p.x) + ';top:' + px(p.y) + ';--c:' + p.col + ';opacity:' + p.op.toFixed(2) + '" title="' + esc(p.t) + '"></span>'; });
    labels.forEach((l) => { h += '<span class="vw-glb" style="left:' + px(l.x) + ';top:' + px(l.y) + ';color:' + l.col + (l.on ? '' : ';opacity:.3') + '">' + esc(l.n) + '</span>'; });
    h += '<span class="vw-ghub" style="left:' + px(hub[0]) + ';top:' + px(hub[1]) + '">' + esc(opts.hub || 'aide') + '<b>' + esc(opts.hubMeta || (nodes.length + ' rec')) + '</b></span>';
    return h + '</div></div>';
  };
  // the lane drawing (records in lanes by family, relevance left → right): opts.layout === 'lanes'
  function cgLanes(nodes, rels, H, opts) {
    const idOf = (n) => String(n.id || n.label); const laneOf = {}; nodes.forEach((n) => { laneOf[idOf(n)] = cgLane(n); });
    const lanes = CG_LANES.filter((L) => nodes.some((n) => cgLane(n) === L[0])); const LH = Math.max(22, Math.floor((H - 4) / Math.max(1, lanes.length)));
    const pos = {}; let html = '<div class="vw-cg" style="position:relative;height:' + H + 'px;overflow:hidden;font-family:var(--mono,ui-monospace,monospace)">';
    lanes.forEach((L, li) => { const top = 2 + li * LH; const members = nodes.filter((n) => cgLane(n) === L[0]);
      html += '<div style="position:absolute;left:0;right:0;top:' + top + 'px;height:' + LH + 'px;border-top:1px dashed color-mix(in srgb,' + L[2] + ' 35%,transparent)"><span style="position:absolute;right:4px;top:1px;font-size:7.5px;letter-spacing:.1em;text-transform:uppercase;color:' + L[2] + ';opacity:.8">' + esc(L[1]) + ' · ' + members.length + '</span></div>';
      members.forEach((n, i) => { const sc = Math.max(0, Math.min(1, +(n.score == null ? 0.5 : n.score))); const x = 5 + (1 - sc) * 82; const y = top + 9 + (i % 2) * Math.max(6, LH - 16) * 0.55 + (LH > 30 ? 4 : 0); pos[idOf(n)] = { x, y, col: n.col || L[2] }; });
    });
    html += '<svg viewBox="0 0 100 ' + H + '" preserveAspectRatio="none" style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none">' + rels.map((e) => { const a = pos[String(e.from)], b = pos[String(e.to)]; if (!a || !b) return ''; const k = String(e.kind || e.label || 'cite').toLowerCase(); return '<line x1="' + a.x.toFixed(1) + '" y1="' + a.y.toFixed(1) + '" x2="' + b.x.toFixed(1) + '" y2="' + b.y.toFixed(1) + '" stroke="' + (laneOf[String(e.from)] === 'memory' || laneOf[String(e.to)] === 'memory' ? 'var(--acc2,#5ec9a0)' : 'var(--dim2,#8a92a0)') + '" stroke-opacity=".55" stroke-width="1" vector-effect="non-scaling-stroke"><title>' + esc(k) + '</title></line>'; }).join('') + '</svg>';
    const labels = opts.labels === true || opts.size === 'l';
    nodes.forEach((n) => { const p = pos[idOf(n)]; if (!p) return; const ghost = n.included === false; const sc = +(n.score == null ? 0.5 : n.score);
      html += '<span title="' + esc((n.label || n.id) + ' · ' + cgLane(n) + ' · relevance ' + sc.toFixed(2) + (ghost ? ' · related, not injected' : '')) + '" style="position:absolute;left:' + p.x.toFixed(1) + '%;top:' + p.y.toFixed(1) + 'px;width:9px;height:9px;border-radius:50%;transform:translate(-50%,-50%);box-shadow:inset 0 0 0 1.5px ' + p.col + ';background:' + (ghost ? 'transparent' : 'color-mix(in srgb,' + p.col + ' 32%,transparent)') + '"></span>'
        + (labels ? '<span style="position:absolute;left:' + p.x.toFixed(1) + '%;top:' + (p.y + 6).toFixed(1) + 'px;transform:translateX(-50%);font-size:7.5px;color:var(--dim2,#8a92a0);white-space:nowrap;max-width:90px;overflow:hidden;text-overflow:ellipsis">' + esc(String(n.label || n.id).slice(0, 18)) + '</span>' : ''); });
    return html + '</div>';
  }
  R.pipes = (d) => {
    // {nodes, links} → the ONE diagram renderer (<vera-mermaid>) through a slot hydrate() fills when the element is defined
    const nodes = ((d && d.nodes) || []).slice(0, 40), links = ((d && (d.links || d.edges)) || []).slice(0, 80);
    const idOf = (n) => typeof n === 'object' ? String(n.id ?? n.name ?? '') : String(n);
    const lbl = (n) => typeof n === 'object' ? String(n.label ?? n.name ?? n.id ?? '') : String(n);
    const safe = (s) => String(s).replace(/[^A-Za-z0-9_]/g, '_');
    const L = ['graph LR'];
    nodes.forEach((n) => { const id = idOf(n); if (id) L.push(safe(id) + '["' + lbl(n).replace(/"/g, "'") + '"]'); });
    links.forEach((e) => { const a = idOf(e.source ?? e.from ?? e.a ?? ''), b = idOf(e.target ?? e.to ?? e.b ?? ''); if (a && b) L.push(safe(a) + ' -->' + (e.label ? '|' + String(e.label).replace(/\|/g, '/') + '|' : '') + ' ' + safe(b)); });
    if (L.length < 2) return EMPTY('no nodes');
    return '<div class="vw-mm" data-mm-code="' + esc(L.join('\n')) + '"><small>' + (nodes.length) + ' nodes · ' + links.length + ' links</small></div>';
  };
  R.panel = (d, H, o) => {
    const pid = (o && o.panel) || (d && typeof d === 'object' && d.panel) || (typeof d === 'string' ? d : '');
    if (!pid) return EMPTY('a panel widget names a registered panel (panel:<id>)');
    return '<iframe class="vw-panel" src="' + esc((o && o.base) || '') + '/ui/panel/window?id=' + encodeURIComponent(pid) + '" style="height:' + Math.max(H, 160) + 'px" title="' + esc(pid) + '"></iframe>';
  };
  R.composite = (d, H, o) => {
    const rec = (o && o.record) || {}; const kids = Array.isArray(rec.children) ? rec.children : [];
    if (!kids.length) return EMPTY('a composite needs children');
    const layout = rec.layout || 'grid', kd = (o && o.kids) || {}, chip = layout === 'rail' || layout === 'report';
    return '<div class="vw-comp vw-comp-' + esc(layout) + '">' + kids.slice(0, 12).map((c, i) => {
      const r0 = c && typeof c.record === 'object' ? c.record : null; const slot = String((c && c.slot) || String.fromCharCode(97 + i));
      if (!r0) return '<div class="vw-slot" data-slot="' + esc(slot) + '"><small class="wempty">' + esc(String(c && c.record || '')) + '</small></div>';
      const n = normalise(r0); const data = r0.data !== undefined ? r0.data : kd[slot]; const kopts = { record: n, draw: n.draw, bare: true };
      if (chip) return '<div class="vw-slot vw-slot-row" data-slot="' + esc(slot) + '"><span class="k">' + esc(n.title || n.form) + '</span>' + draw(n.form, data, 's', kopts) + '</div>';
      const fig = figure(n.form, mapped(n, n.form, data === undefined ? sample(n.form) : data));
      return '<div class="vw-slot" data-slot="' + esc(slot) + '"><span class="vw-slot-h">' + esc(n.title || n.form) + (fig ? '<b>' + fig + '</b>' : '') + '</span><div class="vw-slot-b">' + draw(n.form, data, 'm', Object.assign({ height: Math.max(44, Math.round(H * 0.8)), title: n.title }, kopts)) + '</div></div>'; }).join('') + '</div>';
  };

  /* ── draw at a size: the composition around the form ─────────────────── */
  const GLYPH = { context_graph: '◎', trace: '∿', radial: '◔', counter: '123', bar: '▬', bars: '▥', thermo: '≣', heat: '▦', matrix: '▦', donut: '◑', stack: '▤', pills: '◦', log: '≡', lane: '≡', table: '▦', files: '⊞', list: '≡', checklist: '☑', stepper: '⋮', calendar: '▦', string: '¶', kv: '≔', pipes: '⌥', scatter: '⁘', panel: '▭', composite: '⊞' };
  function draw(form, data, size, opts) {
    opts = opts || {}; const f0 = String(form || ''); const f = canon(f0); size = SIZES.includes(size) ? size : 'm';
    const H = opts.height || HEIGHT[size] || 70;
    if (!R[f]) return EMPTY('form ' + f0 + ' · no drawing yet');
    if (opts.map !== false) { const rec0 = opts.record; const m = opts.map || (rec0 && rec0.read && rec0.read.map), rg = opts.range || (rec0 && rec0.read && rec0.read.range);
      if ((m && typeof m === 'object' && Object.keys(m).length) || (Array.isArray(rg) && rg.length === 2)) data = mapped({ read: { map: opts.map || m, range: opts.range || rg } }, f, data); }
    const d = dataFor(data, f);
    // nothing to draw yet (no result, an empty one, or a placeholder string handed to a form that draws numbers): the
    // form's SAMPLE face, marked — never "no data yet" (opts.sample === false keeps the bare answer for a caller that asks)
    if (f !== 'panel' && f !== 'composite' && (isEmpty(d) || (typeof d === 'string' && DRAWN[f] !== 'string'))) {
      if (opts.sample === false) return EMPTY('no data yet');
      return sampleFace(draw(f0, sample(f), size, Object.assign({}, opts, { sample: false, map: false })), size);
    }
    if (f === 'composite' && !(opts.record && Array.isArray(opts.record.children) && opts.record.children.length)) {
      if (opts.sample === false) return EMPTY('a composite needs children');
      return sampleFace(draw(f0, data, size, Object.assign({}, opts, { sample: false, record: Object.assign({}, opts.record || {}, sample('composite')) })), size);
    }
    if (size === 'xs') return '<span class="vw-xs" title="' + esc(opts.title || f0) + '"><i>' + (GLYPH[f] || '▢') + '</i>' + (figure(f, data) || '—') + '</span>';
    if (size === 's') return '<span class="vw-chip" title="' + esc(opts.title || f0) + '"><i>' + (GLYPH[f] || '▢') + '</i><b>' + (figure(f, data) || '—') + '</b>' + (opts.title ? '<small>' + esc(opts.title) + '</small>' : '') + '</span>';
    let body; try { body = R[f](d, H, Object.assign({ size: size }, opts)); } catch (e) { body = EMPTY('could not draw ' + f0 + ': ' + (e && e.message || e)); }
    if (size === 'm' || opts.bare) return body;
    // L: the form plus its detail list beside it; XL: the form, its table, its log
    const kv = keyed(d).slice(0, 8); const rw = rows(d);
    const detail = kv.length ? '<div class="vw-detail">' + kv.map((x) => '<div><span>' + esc(x[0]) + '</span><b>' + esc(fmt(x[1])) + '</b></div>').join('') + '</div>'
      : (rw.length ? '<div class="vw-detail">' + rw.slice(0, 8).map((r) => '<div><span>' + esc(String(r.name ?? r.title ?? r.text ?? r.path ?? r.id ?? '')) + '</span><b>' + esc(String(r.value ?? r.v ?? r.status ?? r.count ?? '')) + '</b></div>').join('') + '</div>'
      : (Array.isArray(d) && d.length ? '<div class="vw-detail"><div><span>points</span><b>' + d.length + '</b></div><div><span>last</span><b>' + esc(fmt(series(d).slice(-1)[0])) + '</b></div><div><span>min · max</span><b>' + esc(fmt(Math.min(...series(d)))) + ' · ' + esc(fmt(Math.max(...series(d)))) + '</b></div></div>' : ''));
    if (size === 'l') return '<div class="vw-l"><div class="vw-main">' + body + '</div>' + detail + '</div>';
    const table = (kv.length || rw.length) && f !== 'table' && f !== 'files' ? R.table(rw.length ? rw : kv.map((x) => ({ name: x[0], value: x[1] })), 140, {}) : '';
    return '<div class="vw-xl"><div class="vw-main">' + body + '</div>' + detail + (table ? '<div class="vw-xltable">' + table + '</div>' : '') + '</div>';
  }
  function forms() { return Object.keys(DRAWN).map((id) => ({ id, shape: DRAWN[id], sizes: SIZES.slice(), drawn: true })).concat(Object.keys(ALIAS).map((id) => ({ id, shape: DRAWN[ALIAS[id]] || '', sizes: SIZES.slice(), drawn: true, as: ALIAS[id] }))); }

  /* ── the record, in one shape (widget_record.py's rules, as far as a renderer needs them) ── */
  function normalise(src) {
    const o = (src && typeof src === 'object') ? src : {};
    const reads = (o.reads && typeof o.reads === 'object') ? o.reads : {};
    const read = (o.read && typeof o.read === 'object') ? o.read : {};
    const drawIn = (o.draw && typeof o.draw === 'object') ? o.draw : {};
    const frame = (o.frame && typeof o.frame === 'object') ? o.frame : {};
    // the template shape says the KIND in `form` and the drawing in `draw.form`; the full record's `form` is the drawing
    const form = String(drawIn.form || o.form || 'table').toLowerCase();
    const size = String(frame.size || o.size || drawIn.size || 'm').toLowerCase();
    const source = String(typeof o.source === 'string' ? o.source : (reads.cap || o.cap || ''));
    const args = Object.assign({}, (reads.args && typeof reads.args === 'object') ? reads.args : {}, (read.args && typeof read.args === 'object') ? read.args : {}, (o.args && typeof o.args === 'object') ? o.args : {}, o.window ? { window: o.window } : (read.window ? { window: read.window } : {}));
    const draw = {}; Object.keys(drawIn).forEach((k) => { if (k !== 'form' && k !== 'size' && k !== 'motion') draw[k] = drawIn[k]; });
    const panel = String(o.panel || (o.source && typeof o.source === 'object' && o.source.panel) || (source.startsWith('panel:') ? source.slice(6) : ''));
    return { id: String(o.id || ''), form, source: source.startsWith('panel:') ? '' : source, title: String(o.title || o.name || source || form),
      read: { refresh: String(o.refresh || read.refresh || reads.every || ''), window: String(o.window || read.window || ''), args,
        map: (read.map && typeof read.map === 'object') ? Object.assign({}, read.map) : {}, range: (Array.isArray(read.range) && read.range.length === 2) ? [num(read.range[0]), num(read.range[1])] : null },
      frame: { size: SIZES.includes(size) ? size : 'm', caption: frame.caption !== false, legend: !!frame.legend, motion: frame.motion == null ? null : !!frame.motion }, draw, panel,
      skin: String(o.skin || 'inherit').toLowerCase(), subject: String(o.subject || ''), actions: Array.isArray(o.actions) ? o.actions : ['dive', 'pin', 'ask'],
      children: Array.isArray(o.children) ? o.children : undefined, layout: o.layout, data: o.data };
  }
  const readable = (cap) => /(\.(get|list|status|load|history|read|stats|metrics|recent|tail|search|find|show|info|summary|query|health|state|series|events|nodes|jobs|runs|snapshot|top)|^obs\.|^sysmon\.|^perf\.|^nodes\.|^docker\.(ps|stats)|^git\.log|^markets\.)/.test(cap) && !/(write|delete|remove|create|run|exec|kill|restart|stop|start|set|save|send|post|push|upsert)\b/.test(cap);
  const key = (rec) => { const n = normalise(rec); return n.form + ' ' + (n.source || (n.panel ? 'panel:' + n.panel : '')) + ' ' + JSON.stringify(n.read.args || {}); };
  // the form that can draw THIS data: the chosen one, else what its shape picks, else the key · value list
  function formFor(rec, data) {
    const n = normalise(rec); data = mapped(n, n.form, data); const empty = (f) => { try { return /class="wempty"/.test(draw(f, data, 'm', { bare: true, map: false })); } catch (_) { return true; } };
    if (data === undefined || !empty(n.form)) return n.form;
    const byShape = formByShape(data); if (byShape && !empty(byShape)) return byShape;
    return 'kv';
  }
  // the pipes form's slots become <vera-mermaid> when that element is defined
  function hydrate(root) {
    const R0 = root || document; let n = 0;
    // the MAX context graph inside a widget (XL): the record's data feeds the chat's own element once it is defined
    const cgs = R0.querySelectorAll ? R0.querySelectorAll('.vw-cgfull[data-cg]:not([data-live])') : [];
    if (cgs.length && window.customElements && customElements.get('vera-context-graph')) cgs.forEach((slot) => { slot.dataset.live = '1'; let d = {}; try { d = JSON.parse(slot.dataset.cg || '{}'); } catch (_) {}
      const el = document.createElement('vera-context-graph'); el.style.cssText = 'position:absolute;inset:0'; slot.innerHTML = ''; slot.appendChild(el);
      try { el.setContext((d.nodes || []).map((x) => Object.assign({ source: x.source || x.family || x.lane || 'context' }, x)), (d.rels || d.edges || []).map((e) => ({ from: e.from, to: e.to, label: e.kind || e.label || '' })), { focus: (d.nodes || []).filter((x) => x.included !== false).map((x) => x.id) }); } catch (_) {} n++; });
    const slots = R0.querySelectorAll ? R0.querySelectorAll('.vw-mm[data-mm-code]:not([data-live])') : [];
    if (!slots.length || !(window.customElements && customElements.get('vera-mermaid'))) return n;
    slots.forEach((slot) => { slot.dataset.live = '1'; const el = document.createElement('vera-mermaid'); el.setAttribute('bare', ''); el.setAttribute('fill', ''); slot.innerHTML = ''; slot.appendChild(el); try { el.render(slot.dataset.mmCode); } catch (_) {} n++; });
    return n;
  }

  /* ── the styles (host-injected once; the element carries them in its shadow) ── */
  const CSS = `
.wempty{color:var(--dim2,#8a92a0);font-size:9.5px;font-family:var(--mono,ui-monospace,monospace)}
.vw-sampled{position:relative;width:100%;min-width:0}.vw-sampled > .vw-sampletag{position:absolute;right:0;top:-2px;font-style:normal;font-family:var(--mono,ui-monospace,monospace);font-size:7.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--acc3,#d4a96a);opacity:.85;pointer-events:none}
span.vw-sampled{opacity:.85}
.vw-svg{display:block;width:100%}
.vw-gal{border-radius:8px;background:radial-gradient(circle at 50% 52%,rgba(110,168,216,.08),transparent 60%)}
.vw-gd{position:absolute;width:9px;height:9px;border-radius:50%;background:var(--surf2,var(--bg2,#1a1c20));box-shadow:0 0 0 1.2px var(--c);transform:translate(-50%,-50%);cursor:pointer}
.vw-gd.lit{background:var(--c)}.vw-gd.hollow{background:transparent;box-shadow:0 0 0 1px var(--c)}
.vw-gedge{position:absolute;height:1px;background:var(--c);transform-origin:0 50%;opacity:.55;pointer-events:none}.vw-gedge.faint{opacity:.18}
.vw-glb{position:absolute;font-family:var(--mono,ui-monospace,monospace);font-size:7.5px;white-space:nowrap;opacity:.8;pointer-events:none}
.vw-ghub{position:absolute;transform:translate(-50%,-50%);width:26px;height:26px;border-radius:50%;background:var(--surf2,var(--bg2,#1a1c20));box-shadow:0 0 0 1.2px var(--acc,#5a9e8f),0 0 14px rgba(110,168,216,.35);display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:var(--mono,ui-monospace,monospace);font-size:7px;color:var(--dim2,#8a92a0);line-height:1}
.vw-ghub b{font-size:6.5px;color:var(--dim,#6b7280);font-weight:400}
.vw-gring{position:absolute;border-radius:50%;box-shadow:0 0 0 1px color-mix(in srgb,var(--border2,#4a4540) 70%,transparent);transform:translate(-50%,-50%);pointer-events:none}
.vw-gal-iso .vw-gring{transform:translate(-50%,-50%) scaleY(.56)}
.vw-gstem{position:absolute;width:1px;background:color-mix(in srgb,var(--dim2,#8a92a0) 45%,transparent);pointer-events:none}
.vw-gstem::after{content:'';position:absolute;left:-2px;bottom:-1px;width:5px;height:3px;border-radius:50%;background:color-mix(in srgb,var(--dim2,#8a92a0) 55%,transparent)}
.vw-xs{display:inline-flex;align-items:center;gap:3px;font-family:var(--mono,ui-monospace,monospace);font-size:.92em;color:var(--text,#d8dce4);vertical-align:-1px}
.vw-xs i,.vw-chip i{font-style:normal;color:var(--acc,#5a9e8f);font-size:.9em}
.vw-chip{display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border-radius:12px;border:1px solid var(--border,rgba(255,255,255,.09));background:var(--bg2,#1a1c20);font-size:10px;color:var(--text,#d8dce4);white-space:nowrap}
.vw-chip b{font-family:var(--mono,ui-monospace,monospace);font-weight:600}.vw-chip small{color:var(--dim2,#8a92a0);font-size:9px}
.vw-hero{display:flex;align-items:baseline;gap:4px}.vw-hero b{font-size:26px;font-weight:600;letter-spacing:-.02em;color:var(--text,#d8dce4);font-family:var(--mono,ui-monospace,monospace)}.vw-hero .vw-unit{color:var(--dim2,#8a92a0);font-size:12px}
.vw-delta{font-size:9.5px;font-family:var(--mono,ui-monospace,monospace);margin-left:6px}.vw-delta.up{color:var(--acc2,#8fb87a)}.vw-delta.down{color:var(--err,#c96b6b)}
.vw-meter{display:flex;align-items:center;gap:8px;width:100%}.vw-track{flex:1;height:8px;border-radius:4px;background:var(--bg2,#1a1c20);overflow:hidden;display:block}.vw-track i{display:block;height:100%;background:var(--acc,#5a9e8f);border-radius:4px}.vw-meter b{font-family:var(--mono,ui-monospace,monospace);font-weight:400;color:var(--text,#d8dce4)}
.vw-therm{display:flex;flex-direction:column;gap:4px;width:100%;font-size:9.5px}.vw-therm div{display:grid;grid-template-columns:90px 1fr 48px;gap:6px;align-items:center}.vw-therm label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text,#d8dce4)}.vw-therm span{display:block;height:8px;border-radius:4px;background:var(--bg2,#1a1c20);overflow:hidden}.vw-therm span i{display:block;height:100%;background:var(--acc,#5a9e8f);border-radius:4px}.vw-therm b{font-family:var(--mono,ui-monospace,monospace);font-weight:400;text-align:right;color:var(--text,#d8dce4)}
.vw-heat{display:grid;gap:2px;width:100%}.vw-heat div{aspect-ratio:1.6;border-radius:3px;background:var(--acc,#5a9e8f);display:flex;align-items:flex-end;padding:2px 4px;font-family:var(--mono,ui-monospace,monospace);font-size:8px;color:var(--text,#d8dce4);overflow:hidden}
.vw-matrix{display:grid;gap:2px;width:100%;font-size:8.5px;font-family:var(--mono,ui-monospace,monospace);align-items:center}.vw-matrix i{color:var(--dim2,#8a92a0);text-align:center;font-style:normal;overflow:hidden;white-space:nowrap}.vw-matrix b{color:var(--text,#d8dce4);font-weight:400;padding-right:6px;white-space:nowrap}.vw-matrix span{display:block;height:14px;border-radius:2px}
.vw-donut{display:flex;align-items:center;gap:10px;width:100%}.vw-legend{display:flex;flex-direction:column;gap:2px;font-size:9.5px;min-width:0}.vw-legend span{display:flex;align-items:center;gap:5px;white-space:nowrap;overflow:hidden;color:var(--text,#d8dce4)}.vw-legend i{width:8px;height:8px;border-radius:50%;flex-shrink:0}.vw-legend b{margin-left:auto;font-family:var(--mono,ui-monospace,monospace);font-weight:400;color:var(--dim2,#8a92a0)}
.vw-stack{display:flex;flex-direction:column;gap:6px;width:100%}.vw-stackbar{display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--bg2,#1a1c20)}.vw-stackbar i{display:block;height:100%}.vw-stack .vw-legend{flex-direction:row;flex-wrap:wrap;gap:4px 10px}
.vw-pills{display:flex;flex-wrap:wrap;gap:4px;width:100%}.vw-pill{display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border-radius:12px;border:1px solid var(--border,rgba(255,255,255,.09));background:var(--bg2,#1a1c20);font-size:9.5px}.vw-pill small{color:var(--dim2,#8a92a0)}.vw-pill b{font-family:var(--mono,ui-monospace,monospace);font-weight:400;color:var(--text,#d8dce4)}.vw-pill.ok b{color:var(--acc2,#8fb87a)}.vw-pill.bad b{color:var(--err,#c96b6b)}
.vw-log{display:flex;flex-direction:column;gap:2px;width:100%;font-family:var(--mono,ui-monospace,monospace);font-size:9px}.vw-log div{display:flex;gap:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text,#d8dce4)}.vw-log .t{color:var(--dim,#6b7280);flex-shrink:0}.vw-log .k{color:var(--acc,#5a9e8f);flex-shrink:0;min-width:48px}.vw-log .lane{color:var(--dim2,#8a92a0);text-transform:uppercase;letter-spacing:.08em;font-size:8px;margin-top:3px}
.vw-tablewrap{width:100%;overflow:auto}.vw-tablewrap table{border-collapse:collapse;font-size:9.5px;width:100%}.vw-tablewrap th{text-align:left;font-family:var(--mono,ui-monospace,monospace);font-size:8px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim,#6b7280);padding:2px 6px;border-bottom:1px solid var(--border2,rgba(255,255,255,.18))}.vw-tablewrap td{padding:2px 6px;border-bottom:1px solid var(--border,rgba(255,255,255,.09));white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:160px;color:var(--text,#d8dce4)}
.vw-list{display:flex;flex-direction:column;gap:3px;width:100%;font-size:10px}.vw-list div{display:flex;justify-content:space-between;gap:8px;overflow:hidden;white-space:nowrap;color:var(--text,#d8dce4)}.vw-list b{font-weight:500;overflow:hidden;text-overflow:ellipsis}.vw-list small{color:var(--dim2,#8a92a0);font-family:var(--mono,ui-monospace,monospace);flex-shrink:0}
.vw-check{display:flex;flex-direction:column;gap:3px;width:100%;font-size:10px}.vw-check div{display:flex;gap:6px;align-items:baseline;color:var(--text,#d8dce4)}.vw-check i{font-style:normal;color:var(--dim2,#8a92a0)}.vw-check .on i{color:var(--acc2,#8fb87a)}.vw-check .on span{color:var(--dim2,#8a92a0);text-decoration:line-through}.vw-check small{margin-left:auto;color:var(--dim,#6b7280);font-family:var(--mono,ui-monospace,monospace)}
.vw-steps{display:flex;flex-direction:column;gap:3px;width:100%;font-size:10px}.vw-steps div{display:flex;gap:7px;align-items:center;color:var(--text,#d8dce4)}.vw-steps i{font-style:normal;width:16px;height:16px;border-radius:50%;border:1px solid var(--border2,rgba(255,255,255,.18));display:inline-flex;align-items:center;justify-content:center;font-size:8px;font-family:var(--mono,ui-monospace,monospace);color:var(--dim2,#8a92a0);flex-shrink:0}.vw-steps .done i{background:var(--acc2,#8fb87a);border-color:var(--acc2,#8fb87a);color:#0e0f12}.vw-steps .now i{border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f);box-shadow:0 0 0 2px color-mix(in srgb,var(--acc,#5a9e8f) 25%,transparent)}.vw-steps .bad i{border-color:var(--err,#c96b6b);color:var(--err,#c96b6b)}.vw-steps small{margin-left:auto;color:var(--dim,#6b7280);font-family:var(--mono,ui-monospace,monospace)}
.vw-cal{display:flex;gap:8px;width:100%;overflow:auto;font-size:9.5px}.vw-cal .day{min-width:110px;display:flex;flex-direction:column;gap:2px}.vw-cal b{font-family:var(--mono,ui-monospace,monospace);font-weight:400;color:var(--dim2,#8a92a0);font-size:8.5px;text-transform:uppercase;letter-spacing:.06em}.vw-cal span{display:flex;gap:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text,#d8dce4)}.vw-cal small{color:var(--dim,#6b7280);font-family:var(--mono,ui-monospace,monospace)}
.vw-str{white-space:pre-wrap;font-size:10.5px;line-height:1.5;color:var(--text,#d8dce4);width:100%}
.vw-mm{width:100%;min-height:70px;height:100%;display:flex;align-items:center;justify-content:center}.vw-mm small{color:var(--dim2,#8a92a0);font-family:var(--mono,ui-monospace,monospace);font-size:9px}.vw-mm vera-mermaid{display:block;width:100%;height:100%}
.vw-panel{width:100%;border:none;background:transparent;display:block}
.vw-l{display:grid;grid-template-columns:1fr 160px;gap:10px;width:100%;align-items:start}.vw-xl{display:grid;grid-template-columns:1fr 180px;gap:10px;width:100%;align-items:start}.vw-xl .vw-xltable{grid-column:1/-1}
.vw-main{min-width:0}.vw-detail{display:flex;flex-direction:column;gap:3px;font-size:9.5px;border-left:1px solid var(--border,rgba(255,255,255,.09));padding-left:10px}.vw-detail div{display:flex;justify-content:space-between;gap:8px;color:var(--text,#d8dce4)}.vw-detail span{color:var(--dim2,#8a92a0);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.vw-detail b{font-family:var(--mono,ui-monospace,monospace);font-weight:400}
.vw-comp{display:grid;gap:8px;width:100%;grid-template-columns:1fr 1fr}.vw-comp-rows,.vw-comp-report{grid-template-columns:1fr}.vw-comp-rail{grid-template-columns:1fr}.vw-comp-2x2{grid-template-columns:1fr 1fr}
.vw-slot{min-width:0;min-height:0;background:var(--surf2,var(--bg2,#1a1c20));border-radius:var(--r-sm,6px);padding:7px 9px 8px;display:flex;flex-direction:column;gap:4px;box-shadow:var(--elev-lo,0 1px 2px rgba(0,0,0,.14))}
.vw-slot-h{display:flex;align-items:baseline;gap:6px;font-size:8.5px;text-transform:uppercase;letter-spacing:.08em;font-weight:600;color:var(--t3,var(--dim,#6b7280));flex-shrink:0;white-space:nowrap;overflow:hidden}.vw-slot-h b{margin-left:auto;font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:11px;color:var(--t1,var(--text,#d8dce4));font-weight:400;text-transform:none;letter-spacing:0}
.vw-slot-b{flex:1;min-height:0;display:flex;align-items:center}.vw-slot-b > *{width:100%}
.vw-slot-row{flex-direction:row;align-items:center;gap:8px;background:transparent;box-shadow:none;border-radius:0;padding:3px 0;border-bottom:1px solid var(--bd,var(--border,rgba(255,255,255,.09)))}.vw-slot-row .k{width:72px;flex-shrink:0;font-size:9.5px;color:var(--t3,var(--dim,#6b7280));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
@media (max-width:520px){.vw-l,.vw-xl{grid-template-columns:1fr}.vw-detail{border-left:none;padding-left:0}}`;
  function ensureCss(root) {
    const host = root && root.head ? root.head : root;
    if (!host || !host.querySelector) return;
    if (host.querySelector('style[data-vera-widget-css]')) return;
    const st = document.createElement('style'); st.setAttribute('data-vera-widget-css', '1'); st.textContent = CSS; host.appendChild(st);
  }

  /* ── the element ──────────────────────────────────────────────────────── */
  const ELEMENT_CSS = `:host{display:block;color:var(--text,#d8dce4);font-family:var(--sans,system-ui,sans-serif);font-size:11px;min-width:0}
.vw-root{display:flex;flex-direction:column;gap:5px;height:100%;min-width:0}
.vw-hd{display:flex;align-items:center;gap:6px;font-family:var(--mono,ui-monospace,monospace);font-size:8.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim,#6b7280);font-weight:600}
.vw-hd i{width:6px;height:6px;border-radius:50%;background:var(--acc,#5a9e8f);flex-shrink:0}.vw-hd b{margin-left:auto;font-size:11px;color:var(--text,#d8dce4);font-weight:400;text-transform:none;letter-spacing:0}
.vw-body{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;overflow:hidden;font-size:10.5px}
.vw-cap{font-size:9px;color:var(--dim2,#8a92a0);font-family:var(--mono,ui-monospace,monospace);display:flex;gap:6px;align-items:center}.vw-cap .sp{flex:1}
.vw-acts{display:flex;gap:4px;flex-wrap:wrap}.vw-acts button{font-size:8.5px;color:var(--text,#d8dce4);background:var(--bg2,#1a1c20);border:1px solid var(--border,rgba(255,255,255,.09));border-radius:99px;padding:2px 7px;cursor:pointer;font-family:inherit}.vw-acts button:hover{color:var(--acc,#5a9e8f);border-color:var(--acc,#5a9e8f)}
.vw-read{font-size:9px;padding:2px 8px;border:1px solid var(--border,rgba(255,255,255,.09));border-radius:4px;background:var(--bg2,#1a1c20);color:var(--dim2,#8a92a0);cursor:pointer;font-family:inherit}
:host([size="xs"]) .vw-root,:host([size="s"]) .vw-root{display:inline-flex}:host([size="xs"]),:host([size="s"]){display:inline-block}
.vw-root[data-motion="0"] *,.vw-root[data-motion="0"] *::before,.vw-root[data-motion="0"] *::after{animation:none!important;transition:none!important}`;
  const ACTIONS = { dive: 'Deep dive', pin: 'Pin to canvas', ask: 'Ask Vera', ops: 'Open in Ops', print: 'Print card', mute: 'Mute' };
  const REFRESH_FLOOR = 10;
  // one capability call for the element and the surface: prod's envelope is {type:'tool_result', tool_name, content};
  // a stand-in may answer {result} or the bare object — every one is opened
  async function call(base, name, args) {
    const r = await fetch((base || '') + '/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, arguments: args || {} }) });
    const j = await r.json(); return (j && j.type === 'tool_result') ? j.content : (j && j.result !== undefined ? j.result : (j && j.content !== undefined ? j.content : j));
  }
  const parseRefresh = (s) => { const m = String(s || '').match(/^(\d+(?:\.\d+)?)\s*(ms|s|m|h)?$/); if (!m) return 0; const n = parseFloat(m[1]); return m[2] === 'ms' ? n / 1000 : m[2] === 'm' ? n * 60 : m[2] === 'h' ? n * 3600 : n; };
  const sizeForWidth = (w) => w <= 120 ? 'xs' : w <= 220 ? 's' : w <= 380 ? 'm' : w <= 620 ? 'l' : 'xl';

  class VeraWidgetEl extends HTMLElement {
    constructor() { super(); this._sh = this.attachShadow({ mode: 'open' }); this._rec = null; this._data = undefined; this._drawn = ''; this._timer = null; this._ro = null; this._auto = 'm'; this._kids = {}; }
    static get observedAttributes() { return ['record', 'size', 'base', 'template-id']; }
    get record() { return this._rec; }
    set record(v) { this._rec = normalise(v); this._data = (v && v.data !== undefined) ? v.data : undefined; this._drawn = ''; this._kids = {}; if (this.isConnected) this._boot(); }
    get base() { return this.getAttribute('base') || window._veraBase || ''; }
    get size() { const s = this.getAttribute('size'); return s && s !== 'auto' && SIZES.includes(s) ? s : (s === 'auto' ? this._auto : (this._rec ? this._rec.frame.size : 'm')); }
    connectedCallback() {
      const a = this.getAttribute('record'); if (a && !this._rec) { try { this.record = JSON.parse(a); } catch (_) { this._rec = normalise({}); } }
      if (this.getAttribute('size') === 'auto' && window.ResizeObserver && !this._ro) { this._ro = new ResizeObserver(() => { const s = sizeForWidth(this.clientWidth || 300); if (s !== this._auto) { this._auto = s; this.render(); this.dispatchEvent(new CustomEvent('widget:resize', { bubbles: true, composed: true, detail: { size: s } })); } }); this._ro.observe(this); }
      this._boot();
    }
    disconnectedCallback() { if (this._timer) { clearInterval(this._timer); this._timer = null; } if (this._ro) { this._ro.disconnect(); this._ro = null; } }
    attributeChangedCallback(n, _o, v) {
      if (n === 'record' && v != null) { try { this.record = JSON.parse(v); } catch (_) {} }
      else if (n === 'template-id' && v) { this._fromTemplate(v); }
      else if (this.isConnected) this.render();
    }
    async _fromTemplate(id) {
      try { const r = await this._call('widget.template.get', { id }); if (r && r.template) { this.record = r.template; } } catch (_) {}
    }
    _call(name, args) { return call(this.base, name, args); }
    _boot() {
      if (!this._rec) { this._rec = normalise({}); }
      this.render();
      const cap = this._rec.source, comp = this._rec.form === 'composite';
      if (comp) this._readKids();
      else if (this._data === undefined && cap && readable(cap)) this.read();
      const every = Math.max(REFRESH_FLOOR, parseRefresh(this._rec.read.refresh));
      if (this._timer) clearInterval(this._timer);
      if (comp && this._rec.read.refresh) this._timer = setInterval(() => this._readKids(), every * 1000);
      else if (cap && this._rec.read.refresh && readable(cap)) this._timer = setInterval(() => this.read(), every * 1000);
    }
    // a composite's children read on their own: each child with a readable source is called with its own args; a child
    // whose source is $subject.<path> takes that slice of the composite's one read (the composite's source, its args);
    // a child that carries data keeps it. What came back sits per slot and the composite draws it.
    async _readKids() {
      const rec = this._rec; if (!rec || rec.form !== 'composite' || !Array.isArray(rec.children)) return;
      const kids = rec.children.map((c, i) => ({ slot: String((c && c.slot) || String.fromCharCode(97 + i)), r: (c && typeof c.record === 'object') ? normalise(c.record) : null, own: c && typeof c.record === 'object' && c.record.data !== undefined }));
      let subj; const wants = kids.some((k) => k.r && /^\$subject/.test(k.r.source));
      if (wants && rec.source && readable(rec.source)) { try { subj = await this._call(rec.source, rec.read.args || {}); } catch (e) { subj = undefined; this._err = String(e && e.message || e).slice(0, 120); } }
      const out = Object.assign({}, this._kids || {});
      await Promise.all(kids.map(async (k) => { if (!k.r || k.own) return;
        if (/^\$subject/.test(k.r.source)) { if (subj !== undefined) { const v = pick(subj, k.r.source.replace(/^\$subject\.?/, '')); if (v !== undefined) out[k.slot] = v; } return; }
        if (k.r.source && readable(k.r.source)) { try { const v = await this._call(k.r.source, k.r.read.args || {}); if (!(v && typeof v === 'object' && v.error && Object.keys(v).length <= 2)) out[k.slot] = v; } catch (_) {} } }));
      if (this._rec !== rec) return; this._kids = out; if (subj !== undefined) this._data = subj; this.render();
      this.dispatchEvent(new CustomEvent('widget:refresh', { bubbles: true, composed: true, detail: { record: rec, data: this._data, kids: out } }));
    }
    async read(forced) {
      const cap = this._rec && this._rec.source; if (!cap) return;
      if (!forced && !readable(cap)) return;
      let res; try { res = await this._call(cap, this._rec.read.args || {}); } catch (e) { res = { error: String(e && e.message || e) }; }
      if (res && typeof res === 'object' && res.error && Object.keys(res).length <= 2) { this._err = String(res.error).slice(0, 120); this.render(); return; }
      this._err = ''; this._data = res; this._drawn = formFor(this._rec, res); this.render();
      this.dispatchEvent(new CustomEvent('widget:refresh', { bubbles: true, composed: true, detail: { record: this._rec, data: res } }));
    }
    refresh() { return this.read(true); }
    _act(id) { this.dispatchEvent(new CustomEvent('widget:' + id, { bubbles: true, composed: true, detail: { record: this._rec, data: this._data, key: key(this._rec) } })); }
    render() {
      const rec = this._rec || normalise({}); const size = this.size; const form = this._drawn || rec.form;
      const opts = { record: rec, draw: rec.draw, title: rec.title, panel: rec.panel, base: this.base, kids: this._kids || {} };   // L and XL compose around the form
      // nothing read yet — no source, a source that waits for a click, a read in flight, a read that failed — draws the
      // form's SAMPLE face, marked, and says why in the caption; the widget always has a face (never "no data yet")
      const noData = this._data === undefined || isEmpty(this._data);
      const sampled = noData && form !== 'panel' && form !== 'composite';
      let why = '';
      if (this._err) why = esc(rec.source + ': ' + this._err);
      else if (this._data === undefined && rec.source && !readable(rec.source)) why = '<button class="vw-read" data-read>Read ' + esc(rec.source) + '</button>';
      else if (this._data === undefined && rec.source) why = 'reading ' + esc(rec.source) + '…';
      const body = draw(form, noData ? undefined : this._data, size, opts);
      const small = size === 'xs' || size === 's';
      const figureTxt = figure(form, sampled ? sample(form) : mapped(rec, form, this._data));
      // the record's skin dresses the element with the page's own pack rules (data-style is what themes.css keys on);
      // motion off holds every moving form still
      const skin = rec.skin && rec.skin !== 'inherit' ? rec.skin : '';
      if (skin) this.setAttribute('data-style', skin); else if (this._skinned) this.removeAttribute('data-style');
      this._skinned = !!skin;
      const cap = (why ? why + ' · ' : '') + (rec.read.window ? 'window ' + esc(rec.read.window) : (rec.source ? esc(rec.source) : (rec.panel ? 'panel ' + esc(rec.panel) : (sampled ? 'sample · no source' : ''))));
      const acts = size === 'xl' ? '<div class="vw-acts">' + rec.actions.filter((a) => ACTIONS[a]).map((a) => '<button data-act="' + a + '">' + ACTIONS[a] + '</button>').join('') + '</div>' : '';
      this._sh.innerHTML = '<style>' + ELEMENT_CSS + CSS + '</style><div class="vw-root" data-form="' + esc(form) + '" data-size="' + size + '"' + (sampled ? ' data-sample="1"' : '') + (rec.frame.motion === false ? ' data-motion="0"' : '') + (rec.frame.legend ? ' data-legend="1"' : '') + '>'
        + (small ? body : '<div class="vw-hd"><i></i>' + esc(rec.title) + (figureTxt && (form === 'radial' || form === 'counter' || form === 'bar' || form === 'trace') ? '<b>' + figureTxt + '</b>' : '') + '</div><div class="vw-body">' + body + '</div>'
          + '<div class="vw-cap">' + cap + '<span class="sp"></span>' + (this._drawn && this._drawn !== rec.form ? 'drawn as ' + esc(this._drawn) + ' · ' : '') + esc(rec.form) + ' · ' + size + '</div>' + acts)
        + '</div>';
      const rb = this._sh.querySelector('[data-read]'); if (rb) rb.addEventListener('click', () => this.read(true));
      this._sh.querySelectorAll('[data-act]').forEach((b) => b.addEventListener('click', () => this._act(b.dataset.act)));
      hydrate(this._sh);
      this.dispatchEvent(new CustomEvent('widget:rendered', { bubbles: true, composed: true, detail: { form, size, sample: sampled } }));
    }
  }
  if (window.customElements && !customElements.get('vera-widget')) customElements.define('vera-widget', VeraWidgetEl);
  window.VeraWidget = { draw, forms, normalise, formByShape, dataFor, applyMap, pick, mapped, formFor, readable, key, hydrate, sample, call, css: () => CSS, ensureCss, figure, sizes: SIZES.slice(), heights: Object.assign({}, HEIGHT), sizeForWidth, shapeFields: SHAPE_FIELDS, version: 2 };

  /* ── THE WIDGET SURFACE — window.VeraWidgetConfig (the WidgetConfig board; the pickers of the Canvas, Harness and
     Dashboard boards) ─────────────────────────────────────────────────────────────────────────────────────────────
     One sheet behind every "+ Add a widget" and every ⚙, wherever a widget can go: the CATALOGUE on the left (the
     host's other menus, the two context-graph entries, every form by shape with its projection · motion · live
     badges, the panels, your templates; one search across all of them), the RECORD in the middle (identity ·
     source · frame · drawing · children · actions · placement — everything the form can be told), the PREVIEW on the
     right (a live <vera-widget> of the record at the chosen size — its own source read, the sample face until
     then — with the record's JSON under it). Resolves with the record widget.validate accepted, or null.
       VeraWidgetConfig.open({mode, record, into, title, anchor, sizes, shape, menuItems, templates, onChange, ok, base})
       → Promise<record | null>;  VeraWidgetConfig.close();  VeraWidgetConfig.version                                  */
  const CFG_CSS = `
.vwc-scrim{position:fixed;inset:0;z-index:9000;background:rgba(0,0,0,.42)}
.vwc{position:fixed;z-index:9001;display:flex;flex-direction:column;width:min(1380px,calc(100vw - 24px));height:min(900px,calc(100vh - 24px));left:50%;top:50%;transform:translate(-50%,-50%);
  background:var(--s1,var(--bg1,#15171c));color:var(--t1,var(--text,#d8dce4));font-family:var(--f-ui,var(--sans,system-ui,sans-serif));font-size:11px;border-radius:var(--ui-radius,10px);
  box-shadow:var(--elev,0 1px 2px rgba(0,0,0,.2),0 24px 60px -20px rgba(0,0,0,.7),0 0 0 1px var(--bd,var(--border,rgba(255,255,255,.09))));overflow:hidden;font-variant-numeric:tabular-nums}
.vwc.anchored{transform:none}
.vwc *,.vwc *::before,.vwc *::after{box-sizing:border-box}
.vwc button{font:inherit;color:inherit;background:none;border:none;cursor:pointer}
.vwc input{font:inherit;color:var(--t1,var(--text,#d8dce4));background:var(--s2,var(--bg2,#1a1c20));border:1px solid var(--bd,var(--border,rgba(255,255,255,.09)));border-radius:var(--r-sm,6px);padding:2px 7px;font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:10px;min-width:0}
.vwc input:focus{outline:none;border-color:var(--ac,var(--acc,#5a9e8f))}
.vwc .mono{font-family:var(--f-mono,var(--mono,ui-monospace,monospace))}
.vwc .sp{flex:1}
.vwc-hd{display:flex;align-items:baseline;gap:12px;padding:12px 18px 10px;border-bottom:1px solid var(--bd,var(--border,rgba(255,255,255,.09)));flex-shrink:0}
.vwc-hd h3{margin:0;font-size:15px;font-weight:600;letter-spacing:-.01em}
.vwc-hd .sub{font-size:10.5px;color:var(--t2,var(--dim2,#8a92a0));max-width:760px;line-height:1.45}
.vwc-hd .x{color:var(--t3,var(--dim,#6b7280));font-size:14px;align-self:center;padding:0 4px}
.vwc-hd .vwc-seg{align-self:center;flex-wrap:nowrap}.vwc-hd .vwc-seg button{height:22px;padding:0 10px;font-size:10px}
.vwc-cs .cn{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:8.5px;color:var(--t3,var(--dim,#6b7280));white-space:nowrap}
.vwc-hd .x:hover{color:var(--t1,var(--text,#d8dce4))}
.vwc-3{flex:1;min-height:0;display:grid;grid-template-columns:292px 1fr 470px;gap:12px;padding:12px 18px 0}
@media (max-width:1100px){.vwc-3{grid-template-columns:220px 1fr 320px}}
.vwc-cat,.vwc-rec,.vwc-pvw{background:var(--s2,var(--bg2,#1a1c20));border-radius:var(--ui-radius,10px);box-shadow:0 0 0 1px var(--bd,var(--border,rgba(255,255,255,.09)));display:flex;flex-direction:column;min-height:0;overflow:hidden}
.vwc-cs{display:flex;align-items:center;gap:8px;padding:9px 12px;font-size:10.5px;color:var(--t3,var(--dim,#6b7280));border-bottom:1px solid var(--bd,var(--border,rgba(255,255,255,.09)));flex-shrink:0}
.vwc-cs input{flex:1;background:var(--bg,var(--bg0,#0e0f12));border-radius:var(--r-sm,6px);padding:4px 8px}
.vwc-cl{flex:1;min-height:0;overflow:auto;padding:4px 6px 10px;scrollbar-width:thin}
.vwc-g{padding:6px 0 2px}
.vwc-gh{display:flex;align-items:baseline;gap:8px;padding:4px 6px 3px;font-size:8.5px;text-transform:uppercase;letter-spacing:.08em;font-weight:600;color:var(--t3,var(--dim,#6b7280))}
.vwc-gh em{font-style:normal;text-transform:none;letter-spacing:0;font-weight:400;opacity:.8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vwc-r{display:grid;grid-template-columns:1fr auto auto auto;gap:4px;align-items:center;min-height:22px;padding:0 6px;border-radius:var(--r-sm,6px);font-size:10.5px;color:var(--t2,var(--dim2,#8a92a0));cursor:pointer}
.vwc-r:hover{background:var(--s3,var(--bg3,#232732));color:var(--t1,var(--text,#d8dce4))}
.vwc-r.on{background:var(--pri-bg,var(--ac,var(--acc,#5a9e8f)));color:var(--pri-fg,var(--on-ac,#0b1119))}
.vwc-r .n{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vwc-r .n small{color:inherit;opacity:.65;margin-left:5px;font-size:9px;font-family:var(--f-mono,var(--mono,ui-monospace,monospace))}
.vwc-r .bd{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:8px;color:var(--t3,var(--dim,#6b7280));background:var(--s3,var(--bg3,#232732));border-radius:var(--r-pill,999px);padding:1px 5px;white-space:nowrap}
.vwc-r.on .bd{background:rgba(255,255,255,.18);color:inherit}
.vwc-r .bd.live{color:var(--ac2,var(--acc2,#8fb87a))}.vwc-r .bd.mot{color:var(--ac3,var(--acc3,#d4a96a))}.vwc-r .bd.none{opacity:.5}
.vwc-r .padd{font-size:9px;padding:1px 7px;border-radius:var(--r-pill,999px);background:var(--pri-bg,var(--ac,var(--acc,#5a9e8f)));color:var(--pri-fg,var(--on-ac,#0b1119));font-weight:600}
.vwc-rh{display:flex;align-items:baseline;gap:9px;padding:11px 16px 8px;border-bottom:1px solid var(--bd,var(--border,rgba(255,255,255,.09)));flex-shrink:0;min-width:0}
.vwc-rh .fn{font-size:15px;font-weight:600;letter-spacing:-.01em;white-space:nowrap}
.vwc-rh .ty{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:9px;color:var(--t3,var(--dim,#6b7280));background:var(--s3,var(--bg3,#232732));border-radius:var(--r-pill,999px);padding:2px 7px;white-space:nowrap}
.vwc-rh .note{font-size:10px;color:var(--t3,var(--dim,#6b7280));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vwc-rb{flex:1;min-height:0;overflow:auto;padding:4px 16px 14px;display:grid;grid-template-columns:1fr 1fr;gap:2px 22px;align-content:start;scrollbar-width:thin}
.vwc-sec{padding:8px 0 6px;min-width:0}.vwc-sec.wide{grid-column:1/-1}
.vwc-sh{font-size:8.5px;text-transform:uppercase;letter-spacing:.09em;font-weight:600;color:var(--t3,var(--dim,#6b7280));padding-bottom:5px;display:flex;gap:8px;align-items:baseline}
.vwc-sh em{font-style:normal;text-transform:none;letter-spacing:0;font-weight:400;font-size:9.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vwc-sh em b{color:var(--t2,var(--dim2,#8a92a0));font-weight:500}
.vwc-row{display:grid;grid-template-columns:76px 1fr;align-items:center;gap:8px;min-height:24px;font-size:10px}
.vwc-row > .k{color:var(--t3,var(--dim,#6b7280));text-transform:uppercase;letter-spacing:.07em;font-size:8.5px;font-weight:600}
.vwc-row .val{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:10px;color:var(--t1,var(--text,#d8dce4));background:var(--s3,var(--bg3,#232732));border-radius:var(--r-sm,6px);padding:3px 8px;justify-self:start;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.vwc-row .val.dim{color:var(--t3,var(--dim,#6b7280));font-family:var(--f-ui,var(--sans,system-ui,sans-serif))}
.vwc-row input.val{background:var(--s3,var(--bg3,#232732));border:none;box-shadow:inset 0 -1px 0 var(--ac,var(--acc,#5a9e8f));justify-self:stretch;width:100%}
.vwc-seg{display:flex;gap:2px;background:var(--s3,var(--bg3,#232732));border-radius:var(--r-sm,6px);padding:2px;justify-self:start;flex-wrap:wrap}
.vwc-seg button{height:17px;padding:0 8px;font-size:9px;color:var(--t2,var(--dim2,#8a92a0));border-radius:calc(var(--r-sm,6px) - 2px);white-space:nowrap}
.vwc-seg button.on{background:var(--pri-bg,var(--ac,var(--acc,#5a9e8f)));color:var(--pri-fg,var(--on-ac,#0b1119));font-weight:600}
.vwc-seg button.off{opacity:.35;cursor:default}
.vwc-sw{width:26px;height:15px;border-radius:9px;background:var(--s3,var(--bg3,#232732));position:relative;justify-self:start;cursor:pointer;flex-shrink:0}
.vwc-sw.on{background:var(--ac2,var(--acc2,#8fb87a))}
.vwc-sw i{position:absolute;top:2px;left:2px;width:11px;height:11px;border-radius:50%;background:var(--s1,var(--bg1,#15171c));transition:left .16s}
.vwc-sw.on i{left:13px}
.vwc-row .swl{display:flex;align-items:center;gap:8px;min-width:0}.vwc-row .swl .dim{font-size:9px;color:var(--t3,var(--dim,#6b7280));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vwc-srcl{display:flex;flex-wrap:wrap;gap:4px;padding:2px 0 6px;max-height:96px;overflow:auto;scrollbar-width:thin}
.vwc-src{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:9.5px;color:var(--t2,var(--dim2,#8a92a0));background:var(--s3,var(--bg3,#232732));border-radius:var(--r-pill,999px);padding:2px 8px;cursor:pointer;white-space:nowrap}
.vwc-src:hover{color:var(--t1,var(--text,#d8dce4))}
.vwc-src.on{background:var(--pri-bg,var(--ac,var(--acc,#5a9e8f)));color:var(--pri-fg,var(--on-ac,#0b1119))}
.vwc-chips{display:flex;flex-wrap:wrap;gap:4px;padding:2px 0 4px}
.vwc-chip{font-size:9.5px;color:var(--t2,var(--dim2,#8a92a0));background:var(--s3,var(--bg3,#232732));border-radius:var(--r-pill,999px);padding:3px 9px;cursor:pointer}
.vwc-chip.on{background:color-mix(in srgb,var(--ac,var(--acc,#5a9e8f)) 26%,var(--s3,var(--bg3,#232732)));color:var(--t1,var(--text,#d8dce4));box-shadow:0 0 0 1px color-mix(in srgb,var(--ac,var(--acc,#5a9e8f)) 50%,transparent)}
.vwc-chip.warn{color:var(--warn,#c9a35a);box-shadow:0 0 0 1px color-mix(in srgb,var(--warn,#c9a35a) 50%,transparent)}
.vwc-chip.bad{color:var(--err,#c96b6b);box-shadow:0 0 0 1px color-mix(in srgb,var(--err,#c96b6b) 55%,transparent)}
.vwc-chip.ok{color:var(--ac2,var(--acc2,#8fb87a))}
.vwc-dim{font-size:9.5px;color:var(--t3,var(--dim,#6b7280));line-height:1.45;padding-top:2px}
.vwc-kids{display:flex;flex-direction:column;gap:3px;padding:2px 0 4px}
.vwc-kid{display:grid;grid-template-columns:8px 90px 1fr 26px 48px 18px;gap:8px;align-items:center;height:24px;padding:0 8px;border-radius:var(--r-sm,6px);background:var(--s3,var(--bg3,#232732));font-size:9.5px;color:var(--t2,var(--dim2,#8a92a0))}
.vwc-kid i{width:8px;height:8px;border-radius:2px;display:block}.vwc-kid .mono{color:var(--t1,var(--text,#d8dce4))}.vwc-kid .src{font-size:9px;color:var(--t3,var(--dim,#6b7280));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vwc-kid .sz{font-size:8.5px;color:var(--ac2,var(--acc2,#8fb87a))}.vwc-kid .slot{font-size:8.5px;color:var(--t3,var(--dim,#6b7280))}.vwc-kid button{font-size:9px;color:var(--t3,var(--dim,#6b7280))}
.vwc-kadd{display:flex;align-items:center;gap:4px;flex-wrap:wrap;padding-top:3px}
.vwc-kadd .k{font-size:8.5px;color:var(--t3,var(--dim,#6b7280));text-transform:uppercase;letter-spacing:.07em;font-weight:600;margin-right:4px}
.vwc-chipb{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:9px;color:var(--t2,var(--dim2,#8a92a0));background:var(--s3,var(--bg3,#232732));border-radius:var(--r-pill,999px);padding:2px 8px}
.vwc-chipb:hover{color:var(--t1,var(--text,#d8dce4))}
.vwc-ph{display:flex;align-items:baseline;gap:9px;padding:11px 16px 8px;font-size:8.5px;text-transform:uppercase;letter-spacing:.09em;font-weight:600;color:var(--t3,var(--dim,#6b7280));border-bottom:1px solid var(--bd,var(--border,rgba(255,255,255,.09)));flex-shrink:0}
.vwc-ph .dim{text-transform:none;letter-spacing:0;font-weight:400;font-size:9.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vwc-ph .dim.sample{color:var(--ac3,var(--acc3,#d4a96a))}.vwc-ph .dim.live{color:var(--ac2,var(--acc2,#8fb87a))}
.vwc-stage{flex:0 0 auto;display:flex;align-items:center;justify-content:center;padding:16px;min-height:290px;max-height:52%;overflow:auto;scrollbar-width:thin;
  background:radial-gradient(70% 90% at 50% 40%,color-mix(in srgb,var(--ac,var(--acc,#5a9e8f)) 7%,transparent),transparent 70%)}
.vwc-pvf{flex:none;background:var(--s1,var(--bg1,#15171c));border-radius:var(--ui-radius,10px);box-shadow:0 0 0 1px var(--bd,var(--border,rgba(255,255,255,.09))),0 10px 30px -16px rgba(0,0,0,.6);padding:10px 12px;min-width:0}
.vwc-pvf.xs,.vwc-pvf.s{background:transparent;box-shadow:none;padding:0;font-size:12.5px;line-height:1.75;color:var(--t2,var(--dim2,#8a92a0));max-width:380px}
.vwc-pvf.xs b,.vwc-pvf.s b{color:var(--t1,var(--text,#d8dce4))}
.vwc-pvf.m{width:302px;min-height:200px}.vwc-pvf.l{width:100%;min-height:200px}.vwc-pvf.xl{width:100%;min-height:300px}
.vwc-pvf vera-widget{display:block;width:100%}
.vwc-pvn{color:var(--t3,var(--dim,#6b7280));font-size:10px;text-align:center;padding:14px;line-height:1.5}.vwc-pvn .big{display:block;font-size:15px;font-weight:600;color:var(--t2,var(--dim2,#8a92a0));padding-bottom:4px}
.vwc-pj{flex:1;min-height:0;display:flex;flex-direction:column;padding:6px 16px 0}
.vwc-pjh{font-size:8.5px;text-transform:uppercase;letter-spacing:.09em;font-weight:600;color:var(--t3,var(--dim,#6b7280));padding-bottom:5px}
.vwc-pj pre{flex:1;min-height:0;overflow:auto;margin:0;font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:9.5px;line-height:1.55;color:var(--t2,var(--dim2,#8a92a0));background:var(--bg,var(--bg0,#0e0f12));border-radius:var(--r-sm,6px);padding:9px 11px;white-space:pre-wrap;word-break:break-all;scrollbar-width:thin}
.vwc-ft{display:flex;align-items:center;gap:6px;padding:10px 18px 12px;flex-shrink:0;flex-wrap:wrap}
.vwc-ft .msgs{flex:1;display:flex;gap:4px;flex-wrap:wrap;min-width:0}
.vwc-ft button{height:26px;padding:0 11px;font-size:10px;color:var(--t2,var(--dim2,#8a92a0));background:var(--s2,var(--bg2,#1a1c20));border-radius:var(--r-sm,6px);box-shadow:0 0 0 1px var(--bd,var(--border,rgba(255,255,255,.09)))}
.vwc-ft button:hover{color:var(--t1,var(--text,#d8dce4))}
.vwc-ft button.pri{background:var(--pri-bg,var(--ac,var(--acc,#5a9e8f)));color:var(--pri-fg,var(--on-ac,#0b1119));font-weight:600;box-shadow:none}
.vwc-ft .st{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:9px;color:var(--t3,var(--dim,#6b7280))}
.vwc-empty{color:var(--t3,var(--dim,#6b7280));font-size:10px;padding:10px 8px}`;
  // what each SHAPE is (the board's SHAPES table) and the fields a form of it draws (the mapping rows)
  const SHAPE_DESC = { level: 'one number in a range', series: 'numbers over time', values: 'a set of labelled numbers', events: 'things that happened, with a time', graph: 'nodes and the links between them',
    items: 'a list of things with fields', stages: 'an ordered sequence with a position', rate: 'a number per second', parts: 'shares of a whole', ohlcv: 'open, high, low, close, volume', matrix: 'rows by columns',
    calendar: 'a value per day', string: 'text', points: 'pairs of numbers', panel: 'a registered panel id', composite: 'a frame of other widgets' };
  const SHAPE_ORDER = ['level', 'series', 'values', 'events', 'graph', 'items', 'stages', 'rate', 'parts', 'ohlcv', 'matrix', 'calendar', 'string', 'points', 'panel', 'composite'];
  const SHAPE_NAME = { level: 'Level', series: 'Series', values: 'Named values', events: 'Events', graph: 'Graph', items: 'Items', stages: 'Stages', rate: 'Rate', parts: 'Parts', ohlcv: 'OHLCV', matrix: 'Matrix', calendar: 'Calendar', string: 'String', points: 'XY points', panel: 'Panel', composite: 'Composite' };
  const RANGE_OF = { level: '0 – max · clamp', values: '0 – max · clamp', rate: '0 – auto' };
  // where a record can go (the board's Placement chips) and what each host asks for
  const PLACES = [['canvas', 'Canvas item'], ['dashboard', 'Dashboard'], ['rail', 'LHM rail'], ['notebook', 'Notebook cell'], ['chat', 'Chat message'], ['ops', 'Ops node box']];
  const INTO = { lhm: { place: 'rail', sizes: ['xs', 's', 'm', 'l'] }, side: { place: 'rail', sizes: ['xs', 's', 'm', 'l'] }, dashboard: { place: 'dashboard', sizes: ['s', 'm', 'l', 'xl'] },
    canvas: { place: 'canvas', sizes: ['s', 'm', 'l', 'xl'] }, reply: { place: 'chat', sizes: ['xs', 's', 'm', 'l'] }, notebook: { place: 'notebook', sizes: ['m', 'l', 'xl'] } };
  const ACTS = [['dive', 'Deep dive'], ['pin', 'Pin to canvas'], ['ask', 'Ask Vera'], ['ops', 'Open in Ops'], ['print', 'Print card'], ['mute', 'Mute alerts']];
  const PALS = ['load', 'kind', 'status', 'accent', 'mono'];
  const BANDS_OF = { load: '60% · 85% → ac2 · ac3 · ac4', status: 'ok · warn · fail', kind: 'dv1 … dv7 by kind' };
  const SIZE_NAME = { xs: 'XS · inline · in a sentence', s: 'S · chip · a row on a node', m: 'M · cell · the form drawn', l: 'L · wide · the cell and its detail list', xl: 'XL · panel · table, log, actions' };
  const LAYOUTS = [['2x2', '2 × 2'], ['rows', 'rows'], ['report', 'report'], ['rail', 'rail'], ['grid', 'grid']];
  const KCOL = { radial: 'var(--ac,var(--acc,#5a9e8f))', trace: 'var(--ac2,var(--acc2,#8fb87a))', log: 'var(--ac3,var(--acc3,#d4a96a))', thermo: 'var(--err,#c96b6b)', bars: 'var(--dv2,#5ec9a0)', table: 'var(--dv3,#38bdf8)', counter: 'var(--dv1,#a78bfa)' };
  // the two context-graph entries of the catalogue: the same form at two sizes (the Context menu's mini galaxy; the full graph)
  const CG_ENTRIES = [{ id: 'context_graph', size: 'm', n: 'Context galaxy · mini', c: 'the Context menu\'s galaxy · every family on a ring' },
    { id: 'context_graph', size: 'xl', n: 'Context graph · full', c: 'the chat\'s own <vera-context-graph>, in a widget' }];
  const dbg = (fn, ms) => { let t = null; return function () { const a = arguments; clearTimeout(t); t = setTimeout(() => fn.apply(null, a), ms); }; };
  const clone = (o) => JSON.parse(JSON.stringify(o == null ? null : o));

  // the working record: any accepted shape → the full shape the sheet edits (widget_record.py's, with the board's
  // frame.dive and placement[] carried alongside deep_dive and place)
  function recFrom(src, into) {
    const o = (src && typeof src === 'object') ? src : {};
    const n = normalise(o);
    const frame = (o.frame && typeof o.frame === 'object') ? o.frame : {};
    const drawIn = (o.draw && typeof o.draw === 'object') ? o.draw : {};
    const placement = Array.isArray(o.placement) ? o.placement.slice() : (Array.isArray(o.placed) ? o.placed.map((p) => String(p).toLowerCase().replace(/^lhm.*$/, 'rail').replace(/^reply.*$/, 'chat')) : (o.place ? [o.place] : []));
    const draw = Object.assign({}, n.draw); delete draw.proj;
    const rec = { id: n.id || '', form: n.form, shape: String(o.shape || ''), projection: String(o.projection || drawIn.proj || ''), source: n.source || (n.panel ? 'panel:' + n.panel : ''),
      title: String(o.title || o.name || ''), read: { refresh: n.read.refresh, window: n.read.window, args: Object.assign({}, n.read.args), map: (o.read && o.read.map && typeof o.read.map === 'object') ? Object.assign({}, o.read.map) : {}, range: n.read.range || null },
      frame: { size: n.frame.size, caption: frame.caption !== false, legend: !!frame.legend, motion: frame.motion == null ? null : !!frame.motion, dive: frame.dive != null ? !!frame.dive : (frame.deep_dive != null ? !!frame.deep_dive : true) },
      draw, skin: String(o.skin || 'inherit'), actions: Array.isArray(o.actions) ? o.actions.slice() : (Array.isArray(o.can) && o.can.length ? o.can.filter((a) => ACTS.some((x) => x[0] === a)) : ['dive', 'pin', 'ask']),
      placement: placement.filter((p) => PLACES.some((x) => x[0] === p)), panel: n.panel || '' };
    if (into && !rec.placement.length && INTO[into]) rec.placement = [INTO[into].place];
    if (n.form === 'composite' || Array.isArray(o.children)) { rec.form = rec.form || 'composite'; rec.layout = String(o.layout || 'grid'); rec.subject = String(o.subject || '');
      rec.children = (Array.isArray(o.children) ? o.children : []).map((c, i) => ({ slot: String((c && c.slot) || String.fromCharCode(97 + i)), record: (c && typeof c.record === 'object') ? recFrom(c.record) : { form: String((c && (c.form || c.record)) || 'radial'), source: String((c && c.source) || ''), title: '', frame: { size: String((c && c.size) || 's') } } })); }
    if (o.template) rec.template = String(o.template);
    if (o.data !== undefined) rec.data = o.data;
    return rec;
  }
  // the record as it leaves the sheet: deep_dive and place beside dive and placement (both shapes read it)
  function recOut(rec) {
    const r = clone(rec); r.frame.deep_dive = r.frame.dive; r.place = r.placement[0] || ''; if (r.frame.motion == null) delete r.frame.motion; if (!r.read.range) delete r.read.range;
    if (r.projection) r.draw.proj = r.projection;
    if (Array.isArray(r.children)) r.children = r.children.map((c) => ({ slot: c.slot, record: recOut(c.record) }));
    return r;
  }
  const isLive = (form) => !!R[canon(form)];

  let S = null;   // the one open sheet's state
  function cfgCss() { if (document.head && !document.head.querySelector('style[data-vera-widget-config-css]')) { const st = document.createElement('style'); st.setAttribute('data-vera-widget-config-css', '1'); st.textContent = CFG_CSS; document.head.appendChild(st); } }
  const h = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

  function cfgOpen(opts) {
    opts = opts || {};
    if (S) cfgClose(null);
    cfgCss();
    const base = opts.base || window._veraBase || '';
    const into = INTO[opts.into] ? opts.into : '';
    const sizes = (Array.isArray(opts.sizes) && opts.sizes.length ? opts.sizes : (into ? INTO[into].sizes : SIZES)).filter((s) => SIZES.includes(s));
    const rec = recFrom(opts.record || { form: 'radial' }, into);
    if (!rec.form) rec.form = 'radial';
    if (!sizes.includes(rec.frame.size)) rec.frame.size = sizes.includes('m') ? 'm' : sizes[sizes.length - 1];
    return new Promise((resolve) => {
      S = { opts, base, into, sizes, rec, mode: opts.mode === 'edit' ? 'edit' : 'add', q: '', cat: null, sources: null, tpls: null, menu: Array.isArray(opts.menuItems) ? opts.menuItems : [],
        picked: null, problems: [], warnings: [], titleTouched: !!(opts.record && (opts.record.title || opts.record.name)), armed: false, status: '', resolve, el: null, pv: null, lastPv: '' };
      build(); place(opts.anchor); loadCatalogue();
      document.addEventListener('keydown', onKey, true);
    });
  }
  function cfgClose(result) {
    if (!S) return; const st = S; S = null;
    document.removeEventListener('keydown', onKey, true);
    if (st.el && st.el.parentNode) st.el.parentNode.removeChild(st.el);
    if (st.scrim && st.scrim.parentNode) st.scrim.parentNode.removeChild(st.scrim);
    try { st.resolve(result === undefined ? null : result); } catch (_) {}
  }
  function onKey(ev) {
    if (!S) return;
    if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); cfgClose(null); }
    else if (ev.key === 'Enter' && !(ev.target && ev.target.classList && ev.target.classList.contains('vwc-q'))) { ev.preventDefault(); ev.stopPropagation(); ok(); }
  }
  function place(anchor) {
    const el = S.el; if (!el) return;
    let r = null;
    try { if (anchor && anchor.getBoundingClientRect) r = anchor.getBoundingClientRect(); else if (anchor && typeof anchor.x === 'number') r = { left: anchor.x, right: anchor.x, top: anchor.y, bottom: anchor.y }; } catch (_) { r = null; }
    if (!r) return;
    const W = el.offsetWidth || 1380, H = el.offsetHeight || 900, vw = window.innerWidth, vh = window.innerHeight;
    let left = r.right + 12, top = Math.max(8, Math.min(r.top, vh - H - 8));
    if (left + W > vw - 8) left = r.left - W - 12;
    if (left < 8) return;   // no room beside it: centred (the default)
    el.classList.add('anchored'); el.style.left = left + 'px'; el.style.top = top + 'px';
  }

  /* ── the sheet's DOM, built once; the three columns render on their own ── */
  function build() {
    const scrim = h('div', 'vwc-scrim'); scrim.addEventListener('click', () => cfgClose(null)); document.body.appendChild(scrim); S.scrim = scrim;
    const el = h('div', 'vwc'); el.setAttribute('role', 'dialog'); el.setAttribute('data-w', 'widget config · sheet'); el.setAttribute('data-mode', S.mode);
    const hd = h('div', 'vwc-hd'); hd.appendChild(h('h3', '', S.opts.title || (S.mode === 'edit' ? 'Widget config' : 'Add a widget')));
    hd.appendChild(h('span', 'sub', 'One record makes any widget. Pick a form on the left; the record in the middle is everything that form can be told — what it reads, how it is framed, how it is drawn, what it can do, where it can go; the preview on the right is the widget those values produce.'));
    hd.appendChild(h('span', 'sp')); hd.appendChild(h('span', 'vwc-seg vwc-packs')); const x = h('button', 'x', '✕'); x.type = 'button'; x.title = 'Cancel (Esc)'; x.addEventListener('click', () => cfgClose(null)); hd.appendChild(x); el.appendChild(hd);
    const cols = h('div', 'vwc-3');
    const cat = h('div', 'vwc-cat'); const cs = h('div', 'vwc-cs'); cs.appendChild(h('span', '', '⌕')); const q = h('input', 'vwc-q'); q.type = 'search'; q.placeholder = 'find a form, a source, a template…'; q.addEventListener('input', () => { S.q = q.value; renderCat(); renderRec(); }); cs.appendChild(q); cs.appendChild(h('span', 'cn', '')); cat.appendChild(cs); cat.appendChild(h('div', 'vwc-cl')); cols.appendChild(cat);
    const rc = h('div', 'vwc-rec'); rc.appendChild(h('div', 'vwc-rh')); rc.appendChild(h('div', 'vwc-rb')); cols.appendChild(rc);
    const pv = h('div', 'vwc-pvw'); pv.appendChild(h('div', 'vwc-ph')); pv.appendChild(h('div', 'vwc-stage')); const pj = h('div', 'vwc-pj'); pj.appendChild(h('div', 'vwc-pjh', 'The record')); pj.appendChild(h('pre')); pv.appendChild(pj); cols.appendChild(pv);
    el.appendChild(cols); el.appendChild(h('div', 'vwc-ft'));
    document.body.appendChild(el); S.el = el;
    renderCat(); renderRec(); renderPvw(); renderFoot(); renderPacks();
    setTimeout(() => { try { q.focus(); } catch (_) {} }, 0);
  }
  // the style packs (the same four the chat's Aa sheet sets): the segment in the header previews one on the sheet and
  // writes it as the record's skin; "page" is inherit — the widget wears whatever pack the page wears
  const PACKS = [['inherit', 'Page'], ['standard', 'Standard'], ['newspaper', 'News'], ['terminal', 'Term'], ['pixel', 'Pixel']];
  function setSkin(p) { if (!S) return; S.rec.skin = p || 'inherit'; if (S.rec.skin === 'inherit') S.el.removeAttribute('data-style'); else S.el.setAttribute('data-style', S.rec.skin); changed(true); }
  function renderPacks() {
    if (!S) return; const seg = S.el.querySelector('.vwc-packs'); if (!seg) return; seg.innerHTML = ''; const cur = S.rec.skin || 'inherit';
    PACKS.forEach((p) => { const b = h('button', p[0] === cur ? 'on' : '', p[1]); b.type = 'button'; b.title = p[0] === 'inherit' ? 'the page\'s own pack' : 'preview the ' + p[1] + ' pack · the record\'s skin'; b.addEventListener('click', () => setSkin(p[0])); seg.appendChild(b); });
  }
  async function loadCatalogue() {
    const st = S;
    try { const r = await call(st.base, 'widget.forms', {}); if (st !== S) return; st.cat = (r && Array.isArray(r.forms) && r.forms.length) ? r : null; } catch (_) { if (st !== S) return; st.cat = null; }
    if (!st.cat) st.cat = { forms: forms().filter((f) => !f.as).map((f) => ({ id: f.id, shape: f.shape, proj: ['flat'], sizes: SIZES.slice(), options: [], glyph: f.id, motion: false })), local: true };
    // the record's derived keys need the catalogue: its shape, its projection; the first validation runs now
    st.rec.shape = shapeOf(st.rec); const fe = formEntry(st.rec.form); if (!st.rec.projection) st.rec.projection = (fe && fe.proj && fe.proj[0]) || 'flat';
    renderCat(); renderRec(); renderPvw(); validateLater();
    try { const r = await call(st.base, 'widget.sources', { limit: 400 }); if (st !== S) return; st.sources = (r && Array.isArray(r.sources)) ? r.sources : []; } catch (_) { if (st !== S) return; st.sources = []; }
    renderCat(); renderRec();
    if (st.opts.templates) { try { const r = await call(st.base, 'widget.template.list', { limit: 300 }); if (st !== S) return; st.tpls = (r && Array.isArray(r.templates)) ? r.templates : []; } catch (_) { if (st !== S) return; st.tpls = []; } renderCat(); }
  }
  const formEntry = (id) => { const f = String(id || '').toLowerCase(); const cat = (S && S.cat && S.cat.forms) || []; return cat.find((x) => x.id === f) || cat.find((x) => x.id === canon(f)) || null; };
  const shapeOf = (rec) => { const f = formEntry(rec.form); return (f && f.shape) || DRAWN[canon(rec.form)] || rec.shape || ''; };
  const matches = (s) => !S.q || String(s || '').toLowerCase().indexOf(S.q.toLowerCase()) >= 0;

  /* ── the catalogue column ── */
  function renderCat() {
    if (!S) return; const list = S.el.querySelector('.vwc-cl'); list.innerHTML = '';
    const rec = S.rec, cat = S.cat, q = S.q.toLowerCase();
    const group = (n, d) => { const g = h('div', 'vwc-g'); const gh = h('div', 'vwc-gh'); gh.appendChild(h('span', '', n)); if (d) gh.appendChild(h('em', '', d)); g.appendChild(gh); list.appendChild(g); return g; };
    const row = (g, o) => { const r = h('div', 'vwc-r' + (o.on ? ' on' : '')); r.setAttribute('data-w', o.n + ' · catalogue row'); const n = h('span', 'n', o.n); if (o.sub) n.appendChild(h('small', '', o.sub)); r.appendChild(n);
      (o.bd || []).forEach((b) => r.appendChild(h('span', 'bd' + (b[1] ? ' ' + b[1] : ''), b[0])));
      if (o.add) { const b = h('button', 'padd', '+ Add'); b.type = 'button'; b.addEventListener('click', (ev) => { ev.stopPropagation(); o.add(); }); r.appendChild(b); }
      r.addEventListener('click', o.pick); r.addEventListener('dblclick', () => ok()); g.appendChild(r); return r; };
    let any = false;
    // the host's other menus' elements (the LHM's "From the other menus"): picked as they are
    const menu = S.menu.filter((it) => matches(it.n + ' ' + (it.c || '') + ' ' + (it.g || '')));
    if (menu.length) { any = true; const g = group('From the other menus', 'an element of another menu, placed as it is');
      menu.forEach((it) => row(g, { n: it.n, sub: it.c, bd: [[it.g || '≡', 'none']], on: S.picked && S.picked.kind === 'menu' && S.picked.item === it, pick: () => { S.picked = { kind: 'menu', item: it }; renderCat(); renderRec(); renderPvw(); renderFoot(); }, add: () => cfgClose(it.record) })); }
    // the two context-graph entries: the same form at two sizes
    const cg = CG_ENTRIES.filter((e) => matches(e.n + ' ' + e.id + ' context graph galaxy'));
    if (cg.length && (!S.opts.shape || S.opts.shape === 'graph')) { any = true; const g = group('Context graph', 'the chat\'s context, as a widget · mini at M, the full graph at XL');
      cg.forEach((e) => row(g, { n: e.n, sub: e.c, bd: [[e.size.toUpperCase(), 'none'], ['live', 'live']], on: !S.picked && rec.form === 'context_graph' && rec.frame.size === e.size,
        pick: () => { S.picked = null; pickForm('context_graph', { size: S.sizes.includes(e.size) ? e.size : S.sizes[S.sizes.length - 1] }); } })); }
    // every form, by shape, with its badges
    if (!cat) { list.appendChild(h('div', 'vwc-empty', 'Loading the catalogue…')); }
    else { const shapes = SHAPE_ORDER.filter((s) => !S.opts.shape || S.opts.shape === s);
      shapes.forEach((sh) => { const fs = cat.forms.filter((f) => f.shape === sh && f.id !== 'context_graph' && matches(f.id + ' ' + (f.name || '') + ' ' + (f.glyph || '') + ' ' + sh)); if (!fs.length) return; any = true;
        const g = group(SHAPE_NAME[sh] || sh, SHAPE_DESC[sh] || '');
        fs.forEach((f) => { const proj = (f.proj || ['flat']); const live = isLive(f.id); const nm = f.name && f.name !== f.id ? f.name : '';
          row(g, { n: nm || f.id, sub: nm ? f.id : '', bd: [[proj.length > 1 ? 'both' : proj[0], 'none'], [f.motion ? 'moves' : 'still', f.motion ? 'mot' : 'none'], [live ? 'live' : 'record', live ? 'live' : 'none']],
            on: !S.picked && rec.form === f.id && !(f.id === 'context_graph'), pick: () => { S.picked = null; pickForm(f.id, {}); } }); }); }); }
    // the registry: the panels (the old panel-as-widget entries, kept as a group) and your templates
    if (S.opts.templates) { const tp = S.tpls;
      if (!tp) list.appendChild(h('div', 'vwc-empty', 'Loading your templates…'));
      else { const panels = tp.filter((t) => /^panel:/.test(t.id) && matches(t.name + ' ' + t.id)), mine = tp.filter((t) => !/^panel:/.test(t.id) && matches(t.name + ' ' + t.id + ' ' + t.form + ' ' + ((t.reads || {}).cap || '')));
        if (mine.length) { any = true; const g = group('Your templates', 'saved records — placeable anywhere');
          mine.forEach((t) => row(g, { n: t.name || t.id, sub: t.form + ((t.reads || {}).cap ? ' · ' + t.reads.cap : ''), bd: [['⧉', 'none'], [((t.source || {}).origin === 'built-in') ? 'built-in' : 'yours', 'none']], on: S.picked && S.picked.kind === 'tpl' && S.picked.id === t.id, pick: () => pickTemplate(t) })); }
        if (panels.length) { any = true; const g = group('Panels', 'every registered panel, as a widget');
          panels.forEach((t) => row(g, { n: t.name || t.id, sub: t.id.slice(6), bd: [['panel', 'none']], on: S.picked && S.picked.kind === 'tpl' && S.picked.id === t.id, pick: () => pickTemplate(t) })); } } }
    if (!any) list.appendChild(h('div', 'vwc-empty', 'Nothing matches "' + S.q + '".'));
    // the counts line: "N forms · M with a live build" (the board's search-row text), in the search field and beside it
    const ph = S.el.querySelector('.vwc-cs'); const live = cat ? cat.forms.filter((f) => isLive(f.id)).length : 0; const line = cat ? (cat.forms.length + ' forms · ' + live + ' with a live build') : '';
    ph.title = line; const qi = ph.querySelector('.vwc-q'); if (qi) qi.placeholder = cat ? 'find a form · ' + line : 'find a form, a source, a template…'; const cn = ph.querySelector('.cn'); if (cn) cn.textContent = cat ? (cat.forms.length + ' · ' + live + ' live') : '';
  }
  function pickForm(id, o) {
    const rec = S.rec; const was = rec.form; rec.form = id; rec.shape = shapeOf(rec);
    const f = formEntry(id); if (f) { if (!(f.proj || []).includes(rec.projection)) rec.projection = (f.proj || ['flat'])[0]; if (!(f.sizes || SIZES).includes(rec.frame.size)) rec.frame.size = f.sizes[f.sizes.length - 1]; }
    if (o && o.size) rec.frame.size = o.size;
    if (was !== id) { rec.draw = {}; rec.read.map = {}; if (rec.source && S.sources) { const s = S.sources.find((x) => x.id === rec.source); if (s && s.shape !== rec.shape) rec.source = ''; } }
    if (id === 'composite' && !Array.isArray(rec.children)) { rec.layout = '2x2'; rec.children = []; }
    if (!S.titleTouched) rec.title = '';
    changed(true);
  }
  function pickTemplate(t) { S.picked = { kind: 'tpl', id: t.id }; const keep = S.rec.placement; S.rec = recFrom(Object.assign({}, t, { template: t.id }), S.into); if (!S.rec.placement.length) S.rec.placement = keep; S.rec.shape = shapeOf(S.rec); S.titleTouched = true; if (!S.sizes.includes(S.rec.frame.size)) S.rec.frame.size = S.sizes.includes('m') ? 'm' : S.sizes[0]; changed(true); }
  function pickSource(src) { const rec = S.rec; rec.source = src ? src.id : ''; rec.read.map = {}; rec.read.args = {}; if (src) { (src.args || []).forEach((a) => { rec.read.args[a] = ''; }); if (!rec.read.refresh && src.refresh_min && src.refresh_min !== 'live' && src.refresh_min !== 'event') rec.read.refresh = src.refresh_min; if (!rec.draw.unit && src.unit) rec.draw.unit = src.unit; } if (!S.titleTouched) rec.title = ''; changed(true); }

  /* ── every edit lands here: the record column re-renders (unless typing), the preview, the JSON, the host, the validator ── */
  const validateLater = dbg(() => validate(), 350);
  function changed(rerender) {
    if (!S) return; S.armed = false; S.rec.shape = shapeOf(S.rec);
    if (rerender) { renderRec(); renderCat(); renderPacks(); }
    renderPvw(); renderFoot(); validateLater();
    try { if (typeof S.opts.onChange === 'function') S.opts.onChange(recOut(S.rec)); } catch (_) {}
  }
  async function validate() {
    const st = S; if (!st) return; const seq = (st.vseq = (st.vseq || 0) + 1);   // an older answer never overwrites a newer record's
    try { const r = await call(st.base, 'widget.validate', { record: recOut(st.rec) }); if (st !== S || seq !== st.vseq) return; const probs = (r && Array.isArray(r.problems)) ? r.problems : [];
      // a record with no source draws its sample face and stays editable (widget_record.validate's own rule): a warning here, not a bar
      st.problems = probs.filter((p) => p !== 'no source'); st.warnings = ((r && Array.isArray(r.warnings)) ? r.warnings : []).concat(probs.filter((p) => p === 'no source').map(() => 'no source · the sample face until one is picked')); st.validated = (r && r.record && typeof r.record === 'object') ? r.record : null; }
    catch (_) { if (st !== S || seq !== st.vseq) return; st.problems = []; st.warnings = []; st.validated = null; }
    renderFoot();
  }
  const titleOf = (rec) => rec.title || ((rec.form || 'widget') + (rec.source ? ' · ' + rec.source : ''));

  /* ── the record column ── */
  function renderRec() {
    if (!S) return; const rec = S.rec, hd = S.el.querySelector('.vwc-rh'), bd = S.el.querySelector('.vwc-rb'); hd.innerHTML = ''; bd.innerHTML = '';
    if (S.picked && S.picked.kind === 'menu') { const it = S.picked.item; hd.appendChild(h('span', 'fn', it.n)); hd.appendChild(h('span', 'ty', it.c || 'menu element')); bd.appendChild(h('div', 'vwc-dim', 'An element of another menu, placed as it is — its record is the menu\'s own. Add puts it in this menu.')); return; }
    const f = formEntry(rec.form), shape = shapeOf(rec); const projs = (f && f.proj && f.proj.length) ? f.proj : ['flat'];
    hd.appendChild(h('span', 'fn', rec.form || 'widget')); hd.appendChild(h('span', 'ty', shape + ' · ' + (rec.projection || projs[0]))); hd.appendChild(h('span', 'sp')); hd.appendChild(h('span', 'note', SHAPE_DESC[shape] || (f ? '' : 'not in the catalogue')));
    const sec = (title, em, wide) => { const s = h('div', 'vwc-sec' + (wide ? ' wide' : '')); const sh = h('div', 'vwc-sh'); sh.appendChild(h('span', '', title)); if (em) { const e = h('em'); e.innerHTML = em; sh.appendChild(e); } s.appendChild(sh); bd.appendChild(s); return s; };
    const row = (s, k, node) => { const r = h('div', 'vwc-row'); r.appendChild(h('span', 'k', k)); r.appendChild(node); s.appendChild(r); return r; };
    const val = (t, dim) => h('span', 'val' + (dim ? ' dim' : ''), t);
    const seg = (list, cur, on) => { const g = h('span', 'vwc-seg'); list.forEach((x) => { const b = h('button', (x[0] === cur ? 'on' : '') + (x[2] === false ? ' off' : ''), x[1] || x[0]); b.type = 'button'; if (x[3]) b.title = x[3]; b.addEventListener('click', () => { if (x[2] === false) return; on(x[0]); }); g.appendChild(b); }); return g; };
    const sw = (cur, on, note) => { const w = h('span', 'swl'); const b = h('span', 'vwc-sw' + (cur ? ' on' : '')); b.setAttribute('role', 'switch'); b.setAttribute('aria-checked', cur ? 'true' : 'false'); b.appendChild(h('i')); b.addEventListener('click', () => on(!cur)); w.appendChild(b); if (note) w.appendChild(h('span', 'dim', note)); return w; };
    const inp = (v, on, ph) => { const i = h('input', 'val'); i.type = 'text'; i.value = v == null ? '' : String(v); if (ph) i.placeholder = ph; i.addEventListener('input', () => on(i.value)); return i; };
    // Identity
    const id = sec('Identity');
    row(id, 'Title', inp(rec.title, (v) => { rec.title = v; S.titleTouched = !!v; changed(false); }, titleOf(rec)));
    row(id, 'Form', val(rec.form));
    row(id, 'Shape', val(shape + ' · derived from the form', true));
    row(id, 'Projection', seg(projs.map((p) => [p, p]), rec.projection || projs[0], (p) => { rec.projection = p; changed(true); }));
    row(id, 'Skin', seg(PACKS.map((p) => [p[0], p[0] === 'inherit' ? 'inherit' : p[1].toLowerCase()]), rec.skin || 'inherit', setSkin));
    // Source
    const needsNone = rec.form === 'panel' || rec.form === 'composite';
    const so = sec('Source', 'anything of shape <b>' + esc(shape) + '</b>' + (S.sources ? '' : ' · loading…'));
    if (rec.form === 'panel') { row(so, 'Panel', inp(rec.panel || rec.source.replace(/^panel:/, ''), (v) => { rec.panel = v; rec.source = v ? 'panel:' + v : ''; changed(false); }, 'a registered panel id')); }
    else if (!needsNone) {
      const srcs = (S.sources || []).filter((s) => s.shape === shape && matches(s.id + ' ' + (s.note || '')));
      const chips = h('div', 'vwc-srcl'); const none = h('span', 'vwc-src' + (rec.source ? '' : ' on'), 'none · sample'); none.title = 'no source: the widget draws its sample face'; none.addEventListener('click', () => pickSource(null)); chips.appendChild(none);
      srcs.slice(0, 80).forEach((s) => { const c = h('span', 'vwc-src' + (rec.source === s.id ? ' on' : ''), s.id); c.title = (s.note || '') + (s.refresh_min ? ' · refresh ≥ ' + s.refresh_min : '') + (s.unit ? ' · ' + s.unit : ''); c.addEventListener('click', () => pickSource(s)); chips.appendChild(c); });
      if (rec.source && !srcs.some((s) => s.id === rec.source)) { const c = h('span', 'vwc-src on', rec.source); c.title = 'the record\'s source (not in the catalogue for this shape)'; chips.appendChild(c); }
      if (S.sources && !srcs.length) chips.appendChild(h('span', 'vwc-dim', 'no ' + shape + ' source registered · type one:'));
      so.appendChild(chips);
      if (S.sources && !srcs.length) row(so, 'Source', inp(rec.source, (v) => { rec.source = v; changed(false); }, 'capability id'));
      const src = (S.sources || []).find((s) => s.id === rec.source);
      if (src && (src.args || []).length) { src.args.forEach((a) => row(so, a, inp(rec.read.args[a], (v) => { rec.read.args[a] = v; changed(false); }, 'arg'))); }
      if (rec.source) (SHAPE_FIELDS[shape] || []).forEach((k) => { const r = row(so, k, inp(rec.read.map[k], (v) => { if (v) rec.read.map[k] = v; else delete rec.read.map[k]; changed(false); }, '← ' + k)); r.title = 'the source\'s field that feeds ' + k; });
      row(so, 'Refresh', seg([['live', 'live'], ['5s', '5s'], ['10s', '10s'], ['1m', '1m'], ['event', 'on event'], ['', 'once']], rec.read.refresh || '', (v) => { rec.read.refresh = v; changed(true); }));
      row(so, 'Window', seg([['1h', '1h'], ['24h', '24h'], ['7d', '7d'], ['', 'all']], rec.read.window || '', (v) => { rec.read.window = v; changed(true); }));
      { const rr = row(so, 'Range', inp(rec.read.range ? rec.read.range.join(' – ') : '', (v) => { const m = String(v).match(/(-?\d+(?:\.\d+)?)\s*(?:–|-|,|to|\.\.)\s*(-?\d+(?:\.\d+)?)/); rec.read.range = m ? [Number(m[1]), Number(m[2])] : null; changed(false); }, RANGE_OF[shape] || 'auto')); rr.title = 'lo – hi · the range the form draws against when the source gives none (the record\'s wins)'; }
    } else so.appendChild(h('div', 'vwc-dim', 'a ' + rec.form + ' reads nothing of its own'));
    // Frame
    const fr = sec('Frame');
    const fsizes = (f && f.sizes) || SIZES;
    row(fr, 'Size', seg(SIZES.map((s) => [s, s.toUpperCase(), S.sizes.includes(s) && fsizes.includes(s), S.sizes.includes(s) && fsizes.includes(s) ? SIZE_NAME[s] : 'not at this size here']), rec.frame.size, (s) => { rec.frame.size = s; changed(true); }));
    row(fr, 'Caption', sw(rec.frame.caption, (v) => { rec.frame.caption = v; changed(true); }));
    row(fr, 'Legend', sw(rec.frame.legend, (v) => { rec.frame.legend = v; changed(true); }));
    const canMove = !!(f && f.motion); const motion = rec.frame.motion == null ? canMove : rec.frame.motion;
    row(fr, 'Motion', sw(motion, (v) => { rec.frame.motion = v; changed(true); }, canMove ? (motion ? 'this form moves' : 'held still') : 'this form has nothing to move'));
    row(fr, 'Deep dive', sw(rec.frame.dive, (v) => { rec.frame.dive = v; changed(true); }, '⤢ in the header opens the sheet'));
    // Drawing
    const dr = sec('Drawing', 'options this form has');
    const opts = (f && f.options) || [];
    if (!opts.length) dr.appendChild(h('div', 'vwc-dim', 'no options of its own'));
    opts.forEach((o) => row(dr, o, inp(rec.draw[o], (v) => { if (v === '') delete rec.draw[o]; else rec.draw[o] = /^-?\d+(\.\d+)?$/.test(v) ? Number(v) : v; changed(false); }, o)));
    row(dr, 'Palette', seg(PALS.map((p) => [p, p]), rec.draw.palette || 'load', (p) => { rec.draw.palette = p; changed(true); }));
    row(dr, 'Bands', inp(Array.isArray(rec.draw.bands) ? rec.draw.bands.join(', ') : '', (v) => { const b = v.split(/[,\s·]+/).map(Number).filter((n) => isFinite(n)); if (b.length) rec.draw.bands = b; else delete rec.draw.bands; changed(false); }, BANDS_OF[rec.draw.palette || 'load'] || '—'));
    // Children (composite)
    if (rec.form === 'composite') { const ch = sec('Children', 'a composite is a frame, a layout and these records', true);
      row(ch, 'Layout', seg(LAYOUTS, rec.layout || 'grid', (l) => { rec.layout = l; changed(true); }));
      const kids = h('div', 'vwc-kids'); (rec.children || []).forEach((c, i) => { const k = h('div', 'vwc-kid'); const dot = h('i'); dot.style.background = KCOL[c.record.form] || 'var(--t3,#6b7280)'; k.appendChild(dot); k.appendChild(h('span', 'mono', c.record.form)); k.appendChild(h('span', 'src', c.record.source || '—')); k.appendChild(h('span', 'sz', String(c.record.frame.size || 's').toUpperCase())); k.appendChild(h('span', 'slot', 'slot ' + c.slot)); const x = h('button', '', '✕'); x.type = 'button'; x.title = 'Remove'; x.addEventListener('click', () => { rec.children.splice(i, 1); changed(true); }); k.appendChild(x); kids.appendChild(k); });
      const kadd = h('div', 'vwc-kadd'); kadd.appendChild(h('span', 'k', '+ add')); ['radial', 'trace', 'bars', 'table', 'log', 'counter'].forEach((fm) => { const b = h('button', 'vwc-chipb', '+ ' + fm); b.type = 'button'; b.addEventListener('click', () => { rec.children = rec.children || []; rec.children.push({ slot: String.fromCharCode(97 + rec.children.length), record: recFrom({ form: fm, size: 's' }) }); changed(true); }); kadd.appendChild(b); }); kids.appendChild(kadd); ch.appendChild(kids);
      ch.appendChild(h('div', 'vwc-dim', 'each child is a full record of its own — the composite only says where it sits and at what size')); }
    // Actions
    const ac = sec('Actions', 'on the widget\'s menu'); const chips = h('div', 'vwc-chips'); ACTS.forEach((a) => { const c = h('span', 'vwc-chip' + (rec.actions.includes(a[0]) ? ' on' : ''), a[1]); c.addEventListener('click', () => { rec.actions = rec.actions.includes(a[0]) ? rec.actions.filter((x) => x !== a[0]) : rec.actions.concat([a[0]]); changed(true); }); chips.appendChild(c); }); ac.appendChild(chips);
    // Placement
    const pl = sec('Placement', 'where this record can be dropped'); const pchips = h('div', 'vwc-chips'); PLACES.forEach((p) => { const c = h('span', 'vwc-chip' + (rec.placement.includes(p[0]) ? ' on' : ''), p[1]); c.addEventListener('click', () => { rec.placement = rec.placement.includes(p[0]) ? rec.placement.filter((x) => x !== p[0]) : rec.placement.concat([p[0]]); changed(true); }); pchips.appendChild(c); }); pl.appendChild(pchips);
    pl.appendChild(h('div', 'vwc-dim', 'the same record everywhere · placement only sets the frame the widget is dropped into'));
  }

  /* ── the preview column: a live <vera-widget> of the record, at the chosen size ── */
  function pvRecord(rec) {
    const r = recOut(rec); r.title = titleOf(rec);
    if (r.form === 'composite' && Array.isArray(r.children)) r.children = r.children.map((c) => ({ slot: c.slot, record: Object.assign({}, c.record, { data: c.record.data !== undefined ? c.record.data : (c.record.source ? undefined : sample(c.record.form)) }) }));
    return r;
  }
  function renderPvw() {
    if (!S) return; const rec = S.rec, ph = S.el.querySelector('.vwc-ph'), stage = S.el.querySelector('.vwc-stage'), pre = S.el.querySelector('.vwc-pj pre');
    ph.innerHTML = '';
    if (S.picked && S.picked.kind === 'menu') { ph.appendChild(h('span', '', 'Preview')); stage.innerHTML = ''; const n = h('div', 'vwc-pvn'); n.appendChild(h('span', 'big', S.picked.item.n)); n.appendChild(document.createTextNode('an element of the ' + (S.picked.item.c || 'other') + ' menu · drawn by that menu when placed')); stage.appendChild(n); pre.textContent = JSON.stringify(S.picked.item.record || {}, null, 1); S.lastPv = ''; return; }
    const size = rec.frame.size; ph.appendChild(h('span', '', 'Preview')); ph.appendChild(h('span', 'dim', SIZE_NAME[size] || size)); ph.appendChild(h('span', 'sp')); const note = h('span', 'dim', ''); ph.appendChild(note);
    const out = pvRecord(rec); const j = JSON.stringify(out, null, 1).replace(/\n\s+(?=[\]}])/g, ' ').replace(/\[\n\s+/g, '[').replace(/,\n\s+(?=[^"{ ])/g, ', '); pre.textContent = j;
    const keyNow = JSON.stringify([out.form, out.source, out.read, out.frame.size, out.frame.motion, out.frame.legend, out.skin, out.draw, out.projection, out.children, out.panel, out.title]);
    let vw = S.pv;
    if (!vw || !vw.isConnected || keyNow !== S.lastPv) {
      stage.innerHTML = ''; const fr = h('div', 'vwc-pvf ' + size); fr.setAttribute('data-w', 'preview · ' + size);
      if (window.customElements && customElements.get('vera-widget')) { vw = document.createElement('vera-widget'); vw.setAttribute('size', size); if (S.base) vw.setAttribute('base', S.base); vw.record = out;
        vw.addEventListener('widget:rendered', (ev) => { const d = (ev && ev.detail) || {}; note.textContent = d.sample ? 'sample · no source read' : (rec.source ? 'live · ' + rec.source : 'record only'); note.className = 'dim ' + (d.sample ? 'sample' : 'live'); });
        if (size === 'xs') { fr.appendChild(document.createTextNode('ct126 is serving qwen3:30b at ')); fr.appendChild(vw); fr.appendChild(document.createTextNode(' with 4 in flight and step 5 waiting on you.')); }
        else fr.appendChild(vw); }
      else { const n = h('div', 'vwc-pvn'); n.appendChild(h('span', 'big', rec.form)); n.appendChild(document.createTextNode('the widget renderer is not on this page')); fr.appendChild(n); vw = null; }
      stage.appendChild(fr); S.pv = vw; S.lastPv = keyNow;
    }
  }
  /* ── the foot: the validator's problems and warnings, ⧉ Save as template, Cancel, Add / Save ── */
  function renderFoot() {
    if (!S) return; const ft = S.el.querySelector('.vwc-ft'); ft.innerHTML = '';
    const msgs = h('div', 'msgs'); S.problems.forEach((p) => { const c = h('span', 'vwc-chip bad', p); c.title = 'widget.validate: a problem'; msgs.appendChild(c); }); S.warnings.forEach((w) => { const c = h('span', 'vwc-chip warn', w); c.title = 'widget.validate: a warning'; msgs.appendChild(c); });
    if (S.status) msgs.appendChild(h('span', 'st', S.status)); ft.appendChild(msgs);
    const menuPick = S.picked && S.picked.kind === 'menu';
    if (S.opts.templates && !menuPick) { const sv = h('button', '', '⧉ Save as template'); sv.type = 'button'; sv.title = 'Save this record to the widget registry — placeable anywhere, from any picker'; sv.addEventListener('click', saveTemplate); ft.appendChild(sv); }
    const cp = h('button', '', 'Copy record'); cp.type = 'button'; cp.addEventListener('click', () => { try { navigator.clipboard.writeText(JSON.stringify(recOut(S.rec), null, 1)); S.status = 'copied'; renderFoot(); } catch (_) {} }); ft.appendChild(cp);
    const cx = h('button', '', 'Cancel'); cx.type = 'button'; cx.addEventListener('click', () => cfgClose(null)); ft.appendChild(cx);
    const okB = h('button', 'pri', S.armed && S.problems.length ? (S.opts.ok || (S.mode === 'edit' ? 'Save' : 'Add')) + ' anyway' : (S.opts.ok || (S.mode === 'edit' ? 'Save' : 'Add'))); okB.type = 'button'; okB.setAttribute('data-ok', '1'); okB.addEventListener('click', ok); ft.appendChild(okB);
  }
  async function saveTemplate() {
    const st = S; if (!st) return; const rec = recOut(st.rec); if (!rec.title) rec.title = titleOf(st.rec);
    st.status = 'saving…'; renderFoot();
    try { const r = await call(st.base, 'widget.template.save', { template: rec, force: true }); if (st !== S) return; st.status = (r && r.ok) ? 'saved ⧉ ' + ((r.template && r.template.id) || '') : ('save failed: ' + ((r && (r.error || (r.problems || []).join('; '))) || '')); if (r && r.ok && Array.isArray(st.tpls)) { st.tpls = st.tpls.filter((t) => t.id !== r.template.id).concat([r.template]); renderCat(); } }
    catch (e) { if (st !== S) return; st.status = 'save failed: ' + String(e && e.message || e); }
    renderFoot();
  }
  async function ok() {
    const st = S; if (!st) return;
    if (st.picked && st.picked.kind === 'menu') { cfgClose(st.picked.item.record); return; }
    await validate(); if (st !== S) return;
    if (st.problems.length && !st.armed) { st.armed = true; renderFoot(); return; }
    const out = recOut(st.rec); if (!out.title) out.title = titleOf(st.rec);
    // what widget.validate normalised, with the sheet's own keys kept beside it
    const v = st.validated; const fin = v ? Object.assign({}, v, { title: out.title || v.title, read: Object.assign({}, v.read, { map: out.read.map, args: out.read.args, range: out.read.range || null }), skin: out.skin || v.skin, frame: Object.assign({}, v.frame, { dive: out.frame.dive, caption: out.frame.caption, legend: out.frame.legend, motion: out.frame.motion != null ? out.frame.motion : v.frame.motion }),
      draw: Object.assign({}, out.draw, v.draw), actions: out.actions, placement: out.placement, place: out.place, template: out.template, panel: v.panel || out.panel, projection: v.projection || out.projection, children: out.children || v.children }) : out;
    if (fin.data === undefined && out.data !== undefined) fin.data = out.data;
    cfgClose(fin);
  }
  window.VeraWidgetConfig = { open: cfgOpen, close: () => cfgClose(null), recordFrom: recFrom, recordOut: recOut, entries: CG_ENTRIES.slice(), packs: PACKS.slice(), version: 2 };
})();
