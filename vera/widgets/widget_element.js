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
   tile all draw through here — the chat's own nine renderers became these, and the Widgets board's seventy-five
   still forms are drawn here in their own right (the vb- section), each with a sample face.                     */
(function () {
  'use strict';
  if (window.VeraWidget) return;
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const num = (v) => { const n = typeof v === 'number' ? v : parseFloat(v); return isFinite(n) ? n : 0; };
  const fmt = (v) => { const n = num(v); return Math.abs(n) >= 100 ? Math.round(n).toLocaleString() : (Math.round(n * 10) / 10).toString(); };
  const SIZES = ['xs', 's', 'm', 'l', 'xl'];
  const HEIGHT = { xs: 14, s: 24, m: 96, l: 130, xl: 220 };
  const EMPTY = (t) => '<span class="wempty">' + esc(t) + '</span>';
  // a form id the catalogue names but this file does not draw in its own right yet resolves to the nearest face; the
  // board's captions too (the motion and iso forms land in the next slices)
  const ALIAS = { sparkline: 'trace', line: 'trace', chart: 'trace', scope: 'trace', ring: 'ring', gauge: 'gauge', 'meter-panel': 'thermo', parts: 'donut', stacks: 'stack', rows: 'list', cards: 'list', tree: 'files',
                  pulse: 'log', comet: 'log', stages: 'stepper', conveyor: 'stepper', program: 'stepper', 'split-flap': 'string', ask: 'string', topology: 'pipes', form: 'kv', tank: 'radial', turbine: 'counter', rate: 'counter',
                  orbit: 'list', shelf: 'list', city: 'table', iso: 'table', globe: 'scatter', controls: 'pills', button: 'string', header: 'string', dial: 'radial', galaxy: 'context_graph',
                  sweep: 'log', activity: 'log', notices: 'log', library: 'list', pages: 'list', wiki: 'list', devices: 'list', notebook: 'list', hosts: 'list', containers: 'list', models: 'list', datasets: 'list', sandboxes: 'list',
                  approvals: 'stepper', frame: 'string', diagram: 'pipes', ticker: 'counter', battery: 'level', tablei: 'table', temps: 'thermo', checks: 'checklist', memgraph: 'minigraph', logi: 'log', notice: 'announcement', trend: 'hero', sparks: 'small-multiples' };
  // what this file draws today (the rest of the catalogue resolves through ALIAS or says so)
  const DRAWN = { trace: 'series', radial: 'level', counter: 'level', bar: 'level', bars: 'values', thermo: 'values', heat: 'matrix', matrix: 'matrix', donut: 'parts',
                  stack: 'parts', pills: 'values', log: 'events', lane: 'events', table: 'items', files: 'items', list: 'items', checklist: 'items', stepper: 'stages',
                  calendar: 'calendar', string: 'string', kv: 'values', pipes: 'graph', context_graph: 'graph', scatter: 'points', panel: 'panel', composite: 'composite',
                  // the Widgets board's still forms, drawn in their own right
                  feed: 'events', gallery: 'items', terminal: 'string', agenda: 'calendar', people: 'items', links: 'items', announcement: 'string', board: 'items', hero: 'level', gauge: 'level', carousel: 'items',
                  'small-multiples': 'series', ring: 'level', area: 'series', histogram: 'values', waterfall: 'values', treemap: 'parts', radar: 'values', gantt: 'stages', flow: 'graph', bullet: 'values',
                  'stacked-bar': 'parts', threshold: 'values', column: 'values', graph: 'graph', numbers: 'values', minigraph: 'graph', ranked: 'values', meter: 'level', funnel: 'stages', waffle: 'parts',
                  lollipop: 'values', box: 'values', slope: 'series', horizon: 'series', diverging: 'values', step: 'series', level: 'level', candles: 'ohlcv', 'spark-table': 'items', rings: 'values',
                  timeline: 'events', bump: 'series', dots: 'matrix', pareto: 'values', tabs: 'matrix', slider: 'values', node: 'values', glance: 'values', pipeline: 'stages', compare: 'values', rail: 'items' };
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
    context_graph: () => { const g = SAMPLE.graph(); return { nodes: g.nodes, rels: g.links.map((l) => ({ from: l.from, to: l.to, kind: l.kind })) }; },
    // ── the boards' forms: a face each, the board's own demo made data (so the gallery and the pickers show the form as drawn) ──
    feed: () => [['system', 'Digest gate landed — 312caef', 'The second boot skipped the pull entirely. Four re-embeds became none; boot is 18 s again.', 'aide', '14:44'], ['dream', 'Nightly review: three writers still touch the tree', 'state_paths, the notebook exporter and the media mirror write inside the repo.', 'dream director', '06:02'], ['team', 'ct130 is back — for now', 'Brought up after the connect timeout; the prober has it on backoff.', 'boejaker', 'yesterday'], ['markets', 'BTC · the March gap filled', 'QChart flagged the fill at 14:41. The backtest waiting on it is unblocked.', 'markets.watch', '14:41']].map((r) => ({ kind: r[0], title: r[1], body: r[2], who: r[3], when: r[4] })),
    table: () => [['ct126', 62, 71, 4], ['ct121', 41, 54, 1], ['ct118', 18, 48, 0], ['pxstore', 12, 42, 0], ['workstation', 33, 51, 2], ['ct130', 0, 0, 0]].map((r) => ({ node: r[0], load: r[1], temp: r[2], in_flight: r[3] })),
    gallery: () => [['render 04', 'png'], ['ops map', 'png'], ['boot chart', 'svg'], ['sprite 04', 'png'], ['companion', 'png'], ['thumb 12', 'jpg'], ['report fig 2', 'svg'], ['screenshot', 'png']].map((g) => ({ name: g[0], kind: g[1] })),
    terminal: () => ({ session: 'loop-lab-dev', state: 'attached', when: '14:41', lines: ['vera@ct126:~$ vera node status', 'ct126 · runtimes · ollama-gpu · load 62% · 4 in flight', 'vera@ct126:~$ tail -n2 vera_start.log', 'fabric: digest unchanged — 0 re-embeds', 'boot 5 · 18.1s', 'vera@ct126:~$ '] }),
    agenda: () => [['09:00', 'stand-up', 'ops · 15 min', ''], ['11:00', 'ct130 NIC swap', 'ops · pve-02', ''], ['14:30', 'loop v7 · fixer', 'running · step 5', 'now'], ['16:00', 'sweep the boot writers', 'booked by Vera · turn 5', '1h'], ['18:00', 'markets close', 'NY', ''], ['23:00', 'pxstore ZFS move', 'maintenance · 40 min', '40m']].map((a) => ({ when: '2026-09-13T' + a[0], title: a[1], detail: a[2], duration: a[3], now: a[3] === 'now' })),
    people: () => [['aide', 'agent · qwen3:30b on ct126', 'online', 'step 5 · waiting'], ['dream', 'agent · nightly review', 'idle', 'idle · 06:04'], ['narrator', 'agent · system narrator', 'online', 'gathering'], ['boejaker', 'owner', 'online', 'here'], ['ops', 'operator', 'away', 'away 2h'], ['review bot', 'agent · pipeline review', 'busy', '2 queued']].map((p) => ({ name: p[0], role: p[1], presence: p[2], doing: p[3] })),
    links: () => [['OP', 'Ops'], ['LL', 'Loop Lab'], ['NB', 'Notebook'], ['MK', 'Markets'], ['GR', 'Graph'], ['ST', 'Settings'], ['ct', 'ct126'], ['⌘', 'Caps'], ['?', 'Docs']].map((l) => ({ k: l[0], name: l[1] })),
    announcement: () => ({ priority: 'maintenance', when: 'tonight 23:00', title: 'pxstore moves to the new ZFS pool', body: 'Model store read-only from 23:00 to 23:40. Loops that need a model already resident carry on; new pulls queue until the move is done.', actions: ['Acknowledge', 'Remind me at 22:30'], who: 'ops' }),
    board: () => [['sweep the boot writers', 'to do', 'turn 5 · booked 16:00'], ['ct130 NIC swap', 'to do', 'ops · 11:00'], ['coastlines for the globes', 'to do', 'design'], ['loop v7 · fixer', 'doing', 'step 5 · waiting on you'], ['nightly review follow-ups', 'doing', 'dream · 3 writers'], ['digest gate · 312caef', 'done', 'landed 14:44'], ['ollama restart on ct126', 'done', '13:58'], ['re-embeds to zero', 'done', 'goal · 3 of 7']].map((r) => ({ name: r[0], column: r[1], detail: r[2] })),
    hero: () => ({ value: 62, unit: '%', delta: 14, trend: wave(26, 52, 30).map((p) => p.v) }),
    gauge: () => [['GPU mem', 7.4, 12, 'G'], ['Disk', 71, 100, '%'], ['Gate', 1, 1, '']].map((g) => ({ name: g[0], value: g[1], max: g[2], unit: g[3] })),
    carousel: () => [['loop-lab-dev', 'ct126', 'running', '41d'], ['session-a41c', 'ct121', 'running', '3h'], ['session-7f0e', 'ct118', 'paused', '2d'], ['pipeline-0d02', 'ct126', 'running', '12m'], ['dream-nightly', 'ct121', 'running', '6h'], ['bench-ctx', 'pxstore', 'stopped', '—'], ['operator-a', 'ct126', 'running', '1d'], ['mirror-be', 'ct130', 'stopped', '—']].map((r) => ({ name: r[0], node: r[1], status: r[2], up: r[3] })),
    'small-multiples': () => [['ct126', 18, '18ms'], ['ct121', 22, '22ms'], ['ct118', 9, '9ms'], ['ct104', 4, '4ms'], ['pxstore', 31, '31ms'], ['ct130', 0, 'timeout']].map((s, i) => ({ name: s[0], last: s[2], status: s[2] === 'timeout' ? 'timeout' : '', series: wave(18, 12 + i * 4, 8).map((p) => p.v) })),
    'spark-table': () => [['ct126', '61%', '1.2G'], ['ct121', '44%', '0.8G'], ['ct118', '27%', '0.3G'], ['pxstore', '71%', '4.1G'], ['ct130', '—', '—']].map((r, i) => ({ node: r[0], series: wave(20, 14 + i * 5, 9).map((p) => p.v), mem: r[1], net: r[2], status: r[0] === 'ct130' ? 'down' : '' })),
    ring: () => ({ value: 18.4, min: 0, max: 32, unit: 'k tokens', note: '18.4k of 32k tokens' }),
    area: () => ({ llm: wave(22, 20, 6).map((p) => p.v), embed: wave(22, 14, 5).map((p) => p.v + 3), fabric: wave(22, 11, 4).map((p) => p.v), git: wave(22, 8, 3).map((p) => p.v) }),
    histogram: () => Object.fromEntries([3, 9, 22, 41, 58, 72, 61, 44, 30, 19, 12, 7, 4, 2].map((v, i) => [(i * 140) + 'ms', v])),
    waterfall: () => ({ system: 2140, skills: 3280, recall: 2610, vector: 4420, ontology: 1950, reply: 4000 }),
    treemap: () => ({ llm: 34, memory: 20, code: 14, fabric: 10, git: 8, web: 6, nlp: 5, other: 3 }),
    heat: () => [['llm', 412], ['embed', 288], ['fabric', 150], ['git', 96], ['web', 54]].map((r, ri) => ({ name: r[0], total: r[1], cells: Array.from({ length: 24 }, (_, i) => Math.round(Math.abs(Math.sin(ri * 1.7 + i * 0.55)) * Math.abs(Math.cos(i * 0.21 + ri)) * 100)) })),
    calendar: () => Array.from({ length: 84 }, (_, i) => { const d = new Date(2026, 5, 22 + i); return { when: d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'), value: Math.round(Math.abs(Math.sin(i * 0.9) * Math.cos(i * 0.31)) * 12) }; }),
    radar: () => ({ gpu: 82, queue: 34, mem: 58, disk: 71, net: 90, errors: 12 }),
    gantt: () => [['1 recon', 0, 12, 'done'], ['2 read', 10, 22, 'done'], ['3 probe', 30, 14, 'done'], ['4 author', 42, 26, 'running'], ['5 test', 66, 16, ''], ['6 verify', 80, 14, '']].map((g) => ({ name: g[0], start: g[1], duration: g[2], status: g[3] })),
    flow: () => ({ nodes: [['chat', 'dv1'], ['router', 'ac'], ['gate', 'dv4'], ['ollama', 'dv1'], ['cap bus', 'dv6'], ['fabric', 'dv2']].map((n) => ({ id: n[0] })), links: [['chat', 'router', 7], ['router', 'gate', 4], ['router', 'cap bus', 4], ['gate', 'ollama', 4], ['cap bus', 'fabric', 3]].map((e) => ({ from: e[0], to: e[1], value: e[2] })) }),
    bullet: () => [['merged', 41, 50, 'ok'], ['review', 12, 50, 'ok'], ['queued', 8, 50, ''], ['failed', 3, 50, 'fail'], ['rolled back', 1, 50, 'warn']].map((s) => ({ name: s[0], value: s[1], max: s[2], status: s[3] })),
    'stacked-bar': () => ({ Vector: 4420, Skills: 3280, Memory: 2610, System: 2140, Ontology: 1950, Graph: 1880, Fabric: 2120 }),
    threshold: () => ({ 'gpu V100': 71, 'cpu pkg': 54, nvme0: 42, nvme1: 39, chipset: 48, ambient: 33 }),
    slider: () => ({ 'gpu V100': 71, 'cpu pkg': 54, nvme0: 42, nvme1: 39, chipset: 48, ambient: 33 }),
    column: () => wave(16, 5, 4).map((p) => Math.max(0, Math.round(p.v))),
    graph: () => ({ nodes: [['vera', 'ac'], ['fabric', 'dv2'], ['redis', 'dv2'], ['neo4j', 'dv4'], ['ollama', 'dv1'], ['gate', 'dv3'], ['pxstore', 'dv6'], ['router', 'ac3'], ['chat', 'dv7']].map((n) => ({ id: n[0], kind: n[1] })), links: [['vera', 'fabric'], ['vera', 'redis'], ['vera', 'router'], ['fabric', 'neo4j'], ['fabric', 'pxstore'], ['router', 'ollama'], ['router', 'gate'], ['redis', 'chat']].map((e) => ({ from: e[0], to: e[1] })) }),
    minigraph: () => ({ nodes: [['m1', 'memory', .9], ['m2', 'memory', .6], ['v1', 'vector', .8], ['v2', 'vector', .5], ['g1', 'graph', .7], ['m3', 'memory', .4], ['v3', 'vector', .3, false], ['g2', 'graph', .2, false], ['e1', 'entity', .5]].map((n) => ({ id: n[0], family: n[1], score: n[2], included: n[3] !== false })), links: [['m1', 'm2'], ['m1', 'v1'], ['m2', 'm3'], ['m2', 'g1'], ['v1', 'v2'], ['v1', 'e1'], ['g1', 'v3'], ['v2', 'g2']].map((e) => ({ from: e[0], to: e[1] })) }),
    ranked: () => [['qwen3:30b', 62, '5.9G'], ['nomic-embed', 18, '1.1G'], ['qwen3:8b', 11, '0.4G'], ['whisper-sm', 6, '0.2G'], ['kokoro-tts', 3, '0.1G']].map((m) => ({ name: m[0], value: m[1], text: m[2] })),
    meter: () => ({ value: 99.4, min: 0, max: 100, unit: '%', delta: -0.2, col: 'var(--b-ac2)', note: '41 errors in 7d · budget 1%', trend: [8, 14, 6, 22, 11, 4, 9, 31, 17, 6, 12, 5, 19, 7] }),
    funnel: () => [['planned', 9], ['started', 8], ['tools ran', 6], ['verified', 5], ['landed', 3]].map((f) => ({ name: f[0], value: f[1] })),
    waffle: () => ({ exercised: 62, 'not yet': 38, note: '62 of 100 caps exercised this week' }),
    lollipop: () => ({ fabric: 4200, evolve: 3400, agents: 2600, markets: 1900, mesh: 1100, media: 600 }),
    box: () => [['recon', 4, 12, 20, 34, 46], ['read', 10, 24, 33, 48, 62], ['probe', 6, 16, 24, 38, 55], ['author', 22, 44, 58, 76, 92], ['verify', 8, 20, 29, 41, 58]].map((b) => ({ name: b[0], lo: b[1], q1: b[2], md: b[3], q3: b[4], hi: b[5] })),
    slope: () => [['boot', 22, 74], ['embed', 34, 66], ['recall', 48, 71], ['reply', 61, 82], ['total', 76, 88]].map((s) => ({ name: s[0], before: s[1], after: s[2] })),
    horizon: () => ({ ct126: wave(20, 22, 10).map((p) => p.v), ct121: wave(20, 30, 9).map((p) => p.v), ct118: wave(20, 12, 6).map((p) => p.v), ct104: wave(20, 8, 4).map((p) => p.v), pxstore: wave(20, 32, 8).map((p) => p.v) }),
    diverging: () => ({ 'llm.generate': 34, 'memory.select': -18, 'code.read': -26, 'fabric.status': 12, 'nlp.rerank': -8, 'web.research': 41 }),
    step: () => wave(14, 5, 4).map((p) => Math.max(0, Math.round(p.v))),
    counter: () => ({ value: 18442, delta: 12, note: 'since 00:00', trend: wave(24, 55, 40).map((p) => p.v) }),
    level: () => ({ value: 7, min: 0, max: 10, what: 'held', expiring: 1, note: '3 free · next expiry in 4m 12s · amber = expiring' }),
    matrix: () => ({ ct126: { ollama: 'ok', fabric: 'ok', redis: 'ok', neo4j: 'ok', gate: 'ok' }, ct121: { ollama: 'ok', fabric: 'ok', redis: 'none', neo4j: 'none', gate: 'warn' }, ct118: { ollama: 'down', fabric: 'ok', redis: 'ok', neo4j: 'none', gate: 'ok' }, pxstore: { ollama: 'none', fabric: 'ok', redis: 'ok', neo4j: 'ok', gate: 'none' }, ct130: { ollama: 'down', fabric: 'down', redis: 'none', neo4j: 'none', gate: 'down' } }),
    rings: () => ({ merged: 82, reviewed: 61, tested: 44 }),
    timeline: () => [['09:10', 'boot'], ['10:02', 'dream'], ['11:30', 'loop v7'], ['12:15', 'promote'], ['13:05', 'ERR ct130'], ['14:38', '312caef'], ['16:00', 'review'], ['18:30', 'backtest']].map((e) => ({ when: '2026-09-13T' + e[0], title: e[1], kind: /ERR/.test(e[1]) ? 'error' : 'info' })),
    bump: () => ({ 'llm.generate': [1, 1, 2, 2, 1], 'memory.select': [2, 3, 3, 1, 2], 'code.read': [3, 2, 1, 3, 3], 'fabric.status': [4, 5, 4, 5, 4], 'web.research': [5, 4, 5, 4, 5] }),
    dots: () => ({ value: 112, busy: 112, total: 160, note: '160 slots · 112 busy · drains in ~40s' }),
    pareto: () => ({ timeout: 38, 'gate wait': 33, oom: 11, parse: 8, auth: 5, disk: 3, dns: 1, other: 1 }),
    tabs: () => ({ ct126: { temp: 71, latency: 18, disk: 71, load: 2.4 }, ct121: { temp: 54, latency: 22, disk: 44, load: 1.1 }, ct118: { temp: 48, latency: 9, disk: 27, load: .6 }, pxstore: { temp: 42, latency: 31, disk: 88, load: 3.9 }, ct130: { temp: 0, latency: 0, disk: 0, load: 0 } }),
    node: () => ({ status: 'healthy', detail: 'V100 12 GB', uptime: '41d', 'gpu temp': { value: 71, unit: '°', max: 95 }, 'latency p50': { value: 18, unit: 'ms', max: 60 }, 'disk · dockerdata': { value: 71, unit: '%', max: 100 }, 'load · 8 cores': { value: 2.4, unit: '', max: 8 } }),
    glance: () => [['tokens in context', '18.4k'], ['messages', 24], ['loops running', 2], ['cost today', '$0.41']].map((g, i) => ({ name: g[0], value: g[1], series: wave(16, 20 + i * 4, 9).map((p) => p.v) })),
    pipeline: () => ({ stages: [{ name: 'adopt', done: true }, { name: 'branch', done: true }, { name: 'test', done: true }, { name: 'review', current: true }, { name: 'promote' }, { name: 'merged' }], stats: { tests_passed: 212, failed: 3, lines: '+6 −2', reviewers: '2 · 1 approved' }, note: 'waiting on you at review' }),
    compare: () => [['gpu temp', 71, 54, 95, '°'], ['latency p50', 18, 22, 40, 'ms'], ['disk used', 71, 44, 100, '%'], ['load / 8', 2.4, 1.1, 8, '']].map((r) => ({ name: r[0], a: r[1], b: r[2], max: r[3], unit: r[4] })),
    numbers: () => [['capabilities', 340], ['services up', 29], ['pending', 3], ['sessions', 412]].map((n) => ({ name: n[0], value: n[1] })),
    log: () => [['14:41', 'LOOP', 'loop v7 step 4/9 code.author'], ['14:41', 'INFO', 'cap fabric.status ok 142ms'], ['14:40', 'INFO', 'cap obs.provenance 312caef'], ['14:39', 'WARN', 'ct130 connect timeout'], ['14:38', 'INFO', 'cap code.read fabric_cap.py'], ['14:37', 'INFO', 'pipeline 0d022af0 review'], ['14:36', 'INFO', 'memory.select 6 recalls'], ['14:35', 'INFO', 'gate lease acquired v7'], ['14:34', 'INFO', 'dream cycle in 22m'], ['14:33', 'ERR', 'embed queue drain'], ['14:32', 'INFO', 'fabric digest unchanged'], ['14:31', 'INFO', 'sandbox 7f0e paused']].map((l) => ({ t: '2026-09-13T' + l[0], kind: l[1], text: l[2] })),
    lane: () => [['goal', 'Cut boot re-work to zero', '3/7 · day 4'], ['dream', 'Nightly review cycle', '41m'], ['loop', 'v7 · step 4 of 9', 'running 6m'], ['cap', 'obs.provenance', '142ms'], ['run', 'pipeline 0d022af0', 'review']].map((l) => ({ kind: l[0], text: l[1], meta: l[2], t: '2026-09-13T14:41' })),
    stepper: () => ({ stages: [{ name: 'adopt', done: true }, { name: 'branch', done: true }, { name: 'test', done: true }, { name: 'review', current: true }, { name: 'promote' }, { name: 'merged' }] }),
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
    if (form === 'string') return esc(String(typeof d === 'string' ? d : (d.text ?? d.value ?? d.title ?? '')).slice(0, 24));
    if (DRAWN[form] === 'ohlcv') { const r = rows(d); return r.length ? fmt(num(r[r.length - 1].close ?? r[r.length - 1].c)) : ''; }
    if (DRAWN[form] === 'matrix' || DRAWN[form] === 'calendar') { const r = rows(d); const n = r.length || ((d && typeof d === 'object') ? Object.keys(d).length : 0); return n ? n + ' rows' : ''; }
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

  /* ══ THE STILL FORMS — the Widgets board's seventy-five ways of reading data, each the board's own drawing ═════════
     The board's markup and CSS, ported under the vb- prefix and made data-driven: the demo constants are the shape's
     data now. Colour comes through the bridge on .vw-b (the board's --ac · --s3 · --t2 · --dv* over the chat's older
     --acc · --bg2 names, with fallbacks), so the same face draws in the shadow root, in a reply, on the dashboard.
     A renderer takes (data, H, opts): H is the size's height, opts.draw the record's options, opts.ui the element's
     UI state (a pager, a sort, a tab, a slider — set through data-vb-set on the markup, kept per element). */
  const B = { ac:'var(--b-ac)', ac2:'var(--b-ac2)', ac3:'var(--b-ac3)', ac4:'var(--b-ac4)', ac5:'var(--b-ac5)', s2:'var(--b-s2)', s3:'var(--b-s3)', surf2:'var(--b-surf2)', surf3:'var(--b-surf3)', t1:'var(--b-t1)', t2:'var(--b-t2)', t3:'var(--b-t3)', bd:'var(--b-bd)', bd2:'var(--b-bd2)', on:'var(--b-on)' };
  const DV = (i) => 'var(--b-dv' + (((i % 7) + 7) % 7 + 1) + ')';
  const mix = (c, pct, base) => 'color-mix(in srgb,' + c + ' ' + pct + '%,' + (base || 'transparent') + ')';
  const stCol = (s) => { const k = String(s == null ? '' : s).toLowerCase(); return /^(ok|up|green|pass|running|healthy|done|serving|online|live|merged|here|attached|good)$/.test(k) ? B.ac2 : /^(warn|paused|degraded|amber|waiting|review|queued|stale|expiring|idle|away|busy)$/.test(k) ? B.ac3 : /^(down|fail|failed|red|error|stopped|dead|err|off|offline|timeout)$/.test(k) ? B.ac4 : B.t3; };
  // the palettes the sheet offers: load (bands → ac2 · ac3 · ac4), kind (dv1…7), status (ok · warn · fail), accent, mono
  const palOf = (o, dflt) => { const p = (o && o.draw && o.draw.palette) || dflt || 'kind'; const bands = (o && o.draw && Array.isArray(o.draw.bands) && o.draw.bands.length) ? o.draw.bands.map(num) : [60, 85];
    if (p === 'load') return (i, v, hi) => { const f = hi ? v / hi * 100 : v; return f >= bands[1] ? B.ac4 : f >= bands[0] ? B.ac3 : B.ac2; };
    if (p === 'accent') return () => B.ac; if (p === 'mono') return (i) => mix(B.t1, 85 - (i % 6) * 12, B.s3); if (p === 'status') return (i, v, hi, s) => stCol(s);
    return (i) => DV(i); };
  const wrap = (form, inner, cls, style) => '<div class="vw-b vb-' + form + (cls ? ' ' + cls : '') + '"' + (style ? ' style="' + style + '"' : '') + '>' + inner + '</div>';
  const cap = (t) => t ? '<span class="vb-lbl">' + t + '</span>' : '';
  const hhmm = (t) => { const s = String(t == null ? '' : t); const m = s.match(/(\d{1,2}):(\d{2})/); return m ? m[1] + ':' + m[2] : s.slice(0, 8); };
  const dayOf = (t) => { const s = String(t == null ? '' : t); const m = s.match(/(\d{4}-\d{2}-\d{2})/); return m ? m[1] : s.slice(0, 10); };
  const txt = (r) => String(r.text ?? r.msg ?? r.message ?? r.line ?? r.title ?? r.name ?? '');
  const nameOf = (r) => String(r.name ?? r.title ?? r.label ?? r.id ?? r.key ?? r.k ?? r.path ?? r.node ?? '');
  const valOf = (r) => num(r.value ?? r.v ?? r.n ?? r.count ?? r.load ?? r.y ?? r.share ?? 0);
  const pct = (v, hi) => Math.max(0, Math.min(100, hi ? v / hi * 100 : v));
  const set = (k, v) => ' data-vb-set="' + esc(k) + ':' + esc(String(v)) + '"';
  const ui = (o, k, d) => (o && o.ui && o.ui[k] != null) ? o.ui[k] : d;
  // many series: {series:{a:[…]}} · {a:[…], b:[…]} · [{name, series|points|values|history:[…]}] · [[…],[…]] · one series
  const multi = (d) => { if (d == null) return []; if (Array.isArray(d)) { if (d.length && Array.isArray(d[0])) return d.map((s, i) => ({ n: 's' + (i + 1), v: series(s) })); if (d.length && d[0] && typeof d[0] === 'object' && !Array.isArray(d[0]) && (Array.isArray(d[0].series) || Array.isArray(d[0].points) || Array.isArray(d[0].values) || Array.isArray(d[0].history) || Array.isArray(d[0].spark))) return d.map((s, i) => ({ n: nameOf(s) || 's' + (i + 1), v: series(s.series || s.points || s.values || s.history || s.spark), col: s.col || s.color, r: s })); const v = series(d); return v.length ? [{ n: '', v }] : []; }
    if (typeof d === 'object') { const src = (d.series && typeof d.series === 'object' && !Array.isArray(d.series)) ? d.series : d; const ks = Object.keys(src).filter((k) => Array.isArray(src[k]) && src[k].length && k !== 'links' && k !== 'nodes'); if (ks.length) return ks.map((k) => ({ n: k, v: series(src[k]) })); if (Array.isArray(d.series)) return multi(d.series); if (Array.isArray(d.rows)) return multi(d.rows); }
    return []; };
  const poly = (vals, W, H, pad, lo, hi) => { lo = lo == null ? Math.min(...vals) : lo; hi = hi == null ? Math.max(...vals) : hi; const r = (hi - lo) || 1; return vals.map((v, i) => ((i / Math.max(1, vals.length - 1)) * W).toFixed(1) + ',' + (pad + (1 - (v - lo) / r) * (H - pad * 2)).toFixed(1)); };
  const spark = (vals, col, W, H, sw) => '<svg viewBox="0 0 ' + (W || 100) + ' ' + (H || 18) + '" preserveAspectRatio="none" class="vb-spk"><polyline points="' + poly(vals, W || 100, H || 18, 2).join(' ') + '" fill="none" stroke="' + col + '" stroke-width="' + (sw || 1.5) + '" stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg>';
  const stagesOf = (d) => { const arr = Array.isArray(d) ? d : (d && (d.stages || d.steps || d.items)) || []; return arr.map((s, i) => (s && typeof s === 'object') ? s : { name: String(s) }).map((o) => { const st = o.done || o.state === 'done' || o.status === 'done' || o.status === 'merged' ? 'done' : (o.current || o.now || o.state === 'running' || o.status === 'running' || o.state === 'current' || o.status === 'review' ? 'now' : (o.state === 'failed' || o.status === 'failed' || o.error ? 'bad' : '')); return Object.assign({ st }, o); }); };
  // the chart's height inside the size's body: what the form's own chrome (a hero, a legend, a caption) leaves
  const chH = (H, used) => Math.max(40, Math.round(H - used));
  const evsOf = (d) => rows(d).filter((r) => r.t != null || r.ts != null || r.time != null || r.when != null || r.text != null || r.title != null || r.msg != null);

  /* ── the standard set: a feed, files, a table, a gallery, a terminal, an agenda, people, links, a notice, a board ── */
  R.feed = (d, H, o) => {
    const ev = evsOf(d); if (!ev.length) return EMPTY('a feed needs stories');
    const n = ev.length, i = Math.max(0, Math.min(n - 1, +ui(o, 'page', 0) || 0)), r = ev[i], k = String(r.kind ?? r.level ?? r.type ?? r.source ?? '');
    return wrap('feed', '<div class="vb-fc"><span class="k"><i style="background:' + (r.col || r.color || (k ? DV(Math.abs(k.length * 7 + k.charCodeAt(0))) : B.ac)) + '"></i>' + esc(k) + '</span><span class="h">' + esc(String(r.title ?? txt(r))) + '</span>' + (r.body || r.text && r.title ? '<span class="b">' + esc(String(r.body ?? r.text)) + '</span>' : '') + '<span class="m"><span>' + esc(String(r.who ?? r.author ?? r.by ?? '')) + '</span><span>' + esc(hhmm(r.when ?? r.t ?? r.ts ?? r.time)) + '</span></span></div>'
      + '<div class="vb-fn"><button' + set('page', (i + n - 1) % n) + '>‹</button><button' + set('page', (i + 1) % n) + '>›</button>' + ev.slice(0, 8).map((_, j) => '<i class="' + (j === i ? 'on' : '') + '"' + set('page', j) + '></i>').join('') + '<span>' + (i + 1) + ' / ' + n + '</span></div>');
  };
  R.files = (d, H, o) => {
    const rw = rows(d); if (!rw.length) return EMPTY('no rows'); if (!rw.some((r) => r.path != null || r.name != null || r.file != null)) return EMPTY('files need a path or a name');
    const ext = (p) => { const m = String(p).match(/\.([a-z0-9]{1,4})$/i); return m ? m[1].toLowerCase() : ''; }; const EC = { py: DV(0), js: DV(3), html: DV(4), md: DV(2), csv: DV(1), json: DV(6), pdf: B.ac4, png: DV(5), jpg: DV(5), svg: DV(3) };
    return wrap('files', '<div class="vb-fr h"><span></span><span>name</span><span class="m">size</span><span class="m">changed</span></div>' + rw.slice(0, (o && o.draw && o.draw.limit) || 8).map((r) => { const p = String(r.path ?? r.name ?? r.file ?? ''), e = String(r.ext ?? r.kind ?? ext(p) ?? ''); return '<div class="vb-fr" title="' + esc(p) + '"><span class="ic" style="background:' + (EC[e] || B.t3) + '">' + esc(e.slice(0, 4)) + '</span><span class="n">' + esc(p.split('/').pop() || p) + (r.who || r.owner || r.by ? '<small>' + esc(String(r.who ?? r.owner ?? r.by)) + '</small>' : '') + '</span><span class="m">' + esc(String(r.size ?? r.bytes ?? '')) + '</span><span class="m">' + esc(String(r.changed ?? r.when ?? r.mtime ?? r.age ?? '')) + '</span></div>'; }).join(''));
  };
  R.table = (d, H, o) => {
    let rw = rows(d); if (!rw.length) return EMPTY('no rows');
    const want = (o && o.draw && Array.isArray(o.draw.columns)) ? o.draw.columns : null, cols = (want || Object.keys(rw[0]).filter((k) => typeof rw[0][k] !== 'object')).slice(0, 6), lim = (o && o.draw && o.draw.limit) || 8;
    const sortBy = String(ui(o, 'sort', (o && o.draw && o.draw.sort) || '')), dir = +ui(o, 'dir', -1) || -1; const lit = o && o.draw && o.draw.lit != null ? o.draw.lit : null;
    if (sortBy && cols.includes(sortBy)) rw = rw.slice().sort((a, b) => { const x = a[sortBy], y = b[sortBy]; return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x ?? '').localeCompare(String(y ?? ''))) * dir; });
    const page = Math.max(0, +ui(o, 'page', 0) || 0), pages = Math.max(1, Math.ceil(rw.length / lim)), pg = Math.min(page, pages - 1), shown = rw.slice(pg * lim, pg * lim + lim);
    const cell = (r, c) => { const v = r[c]; const n = typeof v === 'number'; const hot = lit != null && n && v >= num(lit); return '<span class="c' + (n ? ' num' : '') + (hot ? ' dn' : '') + '" title="' + esc(String(v ?? '')) + '">' + esc(v == null ? '' : (n ? fmt(v) : String(v))) + '</span>'; };
    return wrap('table', '<div class="vb-dgr h" style="grid-template-columns:' + cols.map((c, i) => i ? 'minmax(40px,auto)' : '1fr').join(' ') + '">' + cols.map((c) => '<button class="' + (sortBy === c ? 'on' : '') + '"' + set('sort', c) + (sortBy === c ? ' data-vb-set2="dir:' + (-dir) + '"' : ' data-vb-set2="dir:-1"') + '>' + esc(c) + '<span>' + (sortBy === c ? (dir < 0 ? '↓' : '↑') : '') + '</span></button>').join('') + '</div>'
      + shown.map((r) => '<div class="vb-dgr" style="grid-template-columns:' + cols.map((c, i) => i ? 'minmax(40px,auto)' : '1fr').join(' ') + '">' + cols.map((c) => cell(r, c)).join('') + '</div>').join('')
      + '<div class="vb-dgf"><span>' + (sortBy ? 'sorted by ' + esc(sortBy) + (dir < 0 ? ' · high first' : ' · low first') : rw.length + ' rows') + '</span><span style="margin-left:auto">' + (pg * lim + 1) + '–' + Math.min(rw.length, pg * lim + lim) + ' of ' + rw.length + '</span><button' + set('page', Math.max(0, pg - 1)) + '>‹</button><button' + set('page', Math.min(pages - 1, pg + 1)) + '>›</button></div>');
  };
  R.gallery = (d, H, o) => {
    const rw = rows(d); const str = (Array.isArray(d) ? d : []).filter((x) => typeof x === 'string'); const items = rw.length ? rw : str.map((s) => ({ src: s, name: s.split('/').pop() })); if (!items.length) return EMPTY('a gallery needs items');
    return wrap('gallery', '<div class="vb-gal" style="height:' + chH(H, 4) + 'px">' + items.slice(0, (o && o.draw && o.draw.thumbs) || 8).map((g, i) => { const src = g.src ?? g.url ?? g.thumb ?? g.image ?? ''; const bg = src ? 'url(' + esc(String(src)) + ') center/cover' : (g.bg || 'linear-gradient(135deg,' + mix(DV(i), 55, B.s3) + ',' + mix(DV(i + 3), 35, B.s3) + ')'); return '<div class="g" style="background:' + bg + '" title="' + esc(nameOf(g)) + '"><b>' + esc(String(g.kind ?? g.ext ?? g.k ?? '')) + '</b><span>' + esc(nameOf(g)) + '</span></div>'; }).join('') + '</div>');
  };
  R.terminal = (d, H, o) => {
    const lines = Array.isArray(d) ? d.map((l) => typeof l === 'string' ? l : txt(l)) : (typeof d === 'string' ? d.split('\n') : (d && typeof d === 'object' ? (Array.isArray(d.lines) ? d.lines.map((l) => typeof l === 'string' ? l : txt(l)) : String(d.text ?? d.output ?? d.tail ?? '').split('\n')) : []));
    if (!lines.length || !lines.some((l) => l)) return EMPTY('a terminal needs lines');
    const head = (d && typeof d === 'object' && !Array.isArray(d)) ? [d.session ?? d.host ?? d.name, d.state ?? (d.attached ? 'attached' : ''), d.when ?? d.t].filter(Boolean).join(' · ') : ((o && o.title) || '');
    const tail = lines.slice(-((o && o.draw && o.draw.lines) || (H > 120 ? 12 : 6)));
    const ln = (l) => { const s = esc(l); const m = s.match(/^([^\s:$#]+@[^\s:$#]+:[^$#]*[$#])(.*)$/); if (m) return '<b>' + m[1] + '</b>' + m[2]; return s.replace(/(\b\d+ (?:re-embeds|passed|failed)\b)/g, '<em>$1</em>'); };
    return wrap('terminal', (head ? '<div class="vb-trmh"><i></i><span>' + esc(head) + '</span></div>' : '') + '<div class="vb-trm">' + tail.map(ln).join('\n') + '<span class="car"></span></div>');
  };
  R.agenda = (d, H, o) => {
    const ev = evsOf(d); if (!ev.length) return EMPTY('an agenda needs bookings');
    const nowH = new Date().getHours() * 60 + new Date().getMinutes(); const mins = (r) => { const m = hhmm(r.when ?? r.t ?? r.start ?? r.time).match(/(\d{1,2}):(\d{2})/); return m ? +m[1] * 60 + +m[2] : -1; };
    let cur = ev.findIndex((r) => r.now || r.current || r.cls === 'now'); if (cur < 0) { let best = -1; ev.forEach((r, i) => { const m = mins(r); if (m >= 0 && m <= nowH) best = i; }); cur = best; }
    return wrap('agenda', ev.slice(0, 8).map((r, i) => '<div class="vb-ag' + (i === cur ? ' now' : '') + '"><span class="t">' + esc(hhmm(r.when ?? r.t ?? r.start ?? r.time)) + '</span><i style="background:' + (r.col || r.color || (stCol(r.status) === B.t3 ? DV(i) : stCol(r.status))) + '"></i><span class="n">' + esc(String(r.title ?? r.name ?? txt(r))) + (r.detail || r.d || r.who ? '<small>' + esc(String(r.detail ?? r.d ?? r.who)) + '</small>' : '') + '</span><span class="w">' + esc(String(r.duration ?? r.w ?? (i === cur ? 'now' : ''))) + '</span></div>').join(''));
  };
  R.people = (d, H, o) => {
    const rw = rows(d); if (!rw.length) return EMPTY('people need rows');
    const ini = (n) => n.split(/[\s._-]+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join('').toUpperCase() || '?';
    return wrap('people', rw.slice(0, 8).map((p, i) => { const n = nameOf(p); const pres = p.presence ?? p.online ?? p.status; return '<div class="vb-pr"><span class="av" style="background:' + (p.col || p.color || DV(i)) + '">' + esc(String(p.ini ?? ini(n))) + '<i style="background:' + (pres === true ? B.ac2 : pres === false ? B.t3 : stCol(pres)) + '"></i></span><span class="n">' + esc(n) + (p.role || p.detail || p.d || p.kind ? '<small>' + esc(String(p.role ?? p.detail ?? p.d ?? p.kind)) + '</small>' : '') + '</span><span class="s">' + esc(String(p.doing ?? p.s ?? p.state ?? (typeof pres === 'string' ? pres : '') ?? '')) + '</span></div>'; }).join(''));
  };
  R.links = (d, H, o) => {
    const rw = rows(d); const str = (Array.isArray(d) ? d : []).filter((x) => typeof x === 'string'); const items = rw.length ? rw : str.map((s) => ({ name: s })); if (!items.length) return EMPTY('links need items');
    return wrap('links', '<div class="vb-ql">' + items.slice(0, (o && o.draw && o.draw.tiles) || 9).map((l) => { const n = nameOf(l), k = String(l.k ?? l.short ?? l.glyph ?? n.slice(0, 2)); const href = l.href ?? l.url ?? l.panel ?? ''; return (href ? '<a href="' + esc(String(href)) + '" data-vb-link="' + esc(String(href)) + '">' : '<button type="button">') + '<b>' + esc(k) + '</b>' + esc(n) + (href ? '</a>' : '</button>'); }).join('') + '</div>');
  };
  R.announcement = (d, H, o) => {
    const a = (d && typeof d === 'object' && !Array.isArray(d)) ? d : (Array.isArray(d) && d.length && typeof d[0] === 'object' ? d[0] : { body: String(d == null ? '' : d) }); if (!(a.body || a.text || a.title || a.message)) return EMPTY('a notice needs text');
    const pri = String(a.priority ?? a.kind ?? a.level ?? 'info').toLowerCase(), col = /^(crit|error|urgent|high|maintenance)/.test(pri) ? B.ac4 : /^(warn|notice|maint|medium)/.test(pri) ? B.ac3 : B.ac;
    const acts = Array.isArray(a.actions) ? a.actions : ['Acknowledge'];
    return wrap('announcement', '<div class="vb-ann" style="--pc:' + col + '"><span class="k">' + esc([pri, a.when ?? a.t ?? a.until].filter(Boolean).join(' · ')) + '</span>' + (a.title ? '<span class="h">' + esc(String(a.title)) + '</span>' : '') + '<span class="b">' + esc(String(a.body ?? a.text ?? a.message ?? '')) + '</span><span class="a">' + acts.slice(0, 3).map((x, i) => '<button type="button" class="' + (i === 0 ? 'pri' : '') + '"' + set('ack', typeof x === 'object' ? (x.id || x.label) : x) + '>' + esc(typeof x === 'object' ? (x.label || x.name || x.id) : x) + '</button>').join('') + (a.who || a.by ? '<small>' + esc(String(a.who ?? a.by)) + '</small>' : '') + '</span></div>');
  };
  R.board = (d, H, o) => {
    let cols = []; if (d && typeof d === 'object' && !Array.isArray(d) && Array.isArray(d.columns)) cols = d.columns.map((c) => ({ n: nameOf(c), items: rows(c.items || c.cards || []) }));
    else if (d && typeof d === 'object' && !Array.isArray(d) && Object.keys(d).every((k) => Array.isArray(d[k]))) cols = Object.keys(d).map((k) => ({ n: k, items: rows(d[k]) }));
    else { const rw = rows(d); const by = {}; const order = (o && o.draw && Array.isArray(o.draw.columns)) ? o.draw.columns.slice() : []; rw.forEach((r) => { const k = String(r.column ?? r.lane ?? r.status ?? r.state ?? 'to do'); if (!by[k]) { by[k] = []; if (!order.includes(k)) order.push(k); } by[k].push(r); }); cols = order.map((k) => ({ n: k, items: by[k] || [] })); }
    if (!cols.length) return EMPTY('a board needs columns of cards');
    const kc = (n, i) => /done|merged|closed/.test(n) ? B.ac2 : /doing|progress|running|now/.test(n) ? B.ac : /waiting|blocked|review/.test(n) ? B.ac3 : DV(i + 2);
    return wrap('board', '<div class="vb-kb">' + cols.slice(0, 4).map((c, i) => '<div class="kc"><span class="kh">' + esc(c.n) + '<b>' + c.items.length + '</b></span>' + c.items.slice(0, H > 120 ? 6 : 3).map((it) => '<span class="kt" style="--kc:' + (it.col || it.color || kc(c.n.toLowerCase(), i)) + '">' + esc(nameOf(it) || txt(it)) + (it.detail || it.d || it.when || it.who ? '<small>' + esc(String(it.detail ?? it.d ?? [it.who, it.when].filter(Boolean).join(' · '))) + '</small>' : '') + '</span>').join('') + '</div>').join('') + '</div>');
  };

  /* ── levels: hero + trend, arc gauges, the progress ring, meter + delta, the counter, the level (battery) ── */
  const trendOf = (d, o) => { const t = (d && typeof d === 'object' && !Array.isArray(d)) ? (d.trend ?? d.history ?? d.series ?? d.samples) : null; return Array.isArray(t) ? series(t) : ((o && o.draw && Array.isArray(o.draw.trend)) ? series(o.draw.trend) : []); };
  R.hero = (d, H, o) => {
    const l = level(d); if (!l) return EMPTY('a hero needs a value');
    const tr = trendOf(d, o); const unit = l.unit || ((l.hi === 100 && l.lo === 0) ? '%' : '');
    const dl = l.delta != null ? '<span class="vb-lbl ' + (num(l.delta) >= 0 ? 'up' : 'dn') + '">' + (num(l.delta) >= 0 ? '▲' : '▼') + ' ' + esc(fmt(Math.abs(num(l.delta)))) + '</span>' : '';
    const chart = tr.length > 1 ? '<div class="vb-chart" style="height:' + chH(H, 62) + 'px"><svg viewBox="0 0 150 80" preserveAspectRatio="none"><path d="M0,80 L' + poly(tr, 150, 80, 5).join(' L') + ' L150,80 Z" fill="' + B.ac + '" fill-opacity=".16"/><polyline points="' + poly(tr, 150, 80, 5).join(' ') + '" fill="none" stroke="' + B.ac + '" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg></div>' : '';
    const sorted = tr.slice().sort((a, b) => a - b); const note = tr.length > 1 ? 'peak ' + fmt(sorted[sorted.length - 1]) + ' · median ' + fmt(sorted[Math.floor(sorted.length / 2)]) : '';
    return wrap('hero', '<div class="vb-hero"><b>' + esc(fmt(l.v)) + '</b><span class="u">' + esc(unit) + '</span>' + dl + '</div>' + chart + cap(note));
  };
  R.gauge = (d, H, o) => {
    const l = level(d); const kv = l && !keyed(d).length ? [[(o && o.title) || 'value', l.v, l.hi, l.unit]] : keyed(d).map((x) => [x[0], x[1], (o && o.draw && o.draw.max) || 100, '']); if (!kv.length) return EMPTY('gauges need values');
    const arc = Math.PI * 24, pal = palOf(o, 'load'); const rw = rows(d); const meta = (k) => rw.find((r) => nameOf(r) === k) || {};
    return wrap('gauge', '<div class="vb-row">' + kv.slice(0, 4).map((g, i) => { const m = meta(g[0]); const hi = num(m.max ?? g[2]) || 100, f = Math.max(0, Math.min(1, g[1] / hi)); const col = pal(i, g[1], hi); return '<span class="vb-gg"><svg width="62" height="40" viewBox="0 0 62 40"><path d="M7 36 A24 24 0 0 1 55 36" fill="none" stroke="' + B.s3 + '" stroke-width="6" stroke-linecap="round"/><path d="M7 36 A24 24 0 0 1 55 36" fill="none" stroke="' + col + '" stroke-width="6" stroke-linecap="round" stroke-dasharray="' + (arc * f).toFixed(1) + ' ' + arc.toFixed(1) + '"/></svg><b style="color:' + col + '">' + esc(String(m.text ?? (fmt(g[1]) + (m.unit ?? g[3] ?? '')))) + '</b><span class="vb-lbl">' + esc(g[0]) + '</span></span>'; }).join('') + '</div>');
  };
  R.ring = (d, H, o) => {
    const l = level(d); if (!l) return EMPTY('a ring needs { value, min, max }');
    const f = Math.max(0, Math.min(1, (l.v - l.lo) / ((l.hi - l.lo) || 1))), C = 2 * Math.PI * 26, unit = l.unit || ((l.hi === 100 && l.lo === 0) ? '%' : ''), big = Math.min(96, Math.max(56, H + 14));
    const note = (d && d.note) || (unit === '%' ? '' : fmt(l.v) + ' of ' + fmt(l.hi) + (unit ? ' ' + unit : ''));
    return wrap('ring', '<span class="vb-ring" style="width:' + big + 'px;height:' + big + 'px"><svg viewBox="0 0 64 64"><circle cx="32" cy="32" r="26" fill="none" stroke="' + B.s3 + '" stroke-width="7"/><circle cx="32" cy="32" r="26" fill="none" stroke="' + palOf(o, 'accent')(0, f * 100, 100) + '" stroke-width="7" stroke-linecap="round" stroke-dasharray="' + (C * f).toFixed(1) + ' ' + C.toFixed(1) + '"/></svg><span>' + Math.round(f * 100) + '%</span></span>' + cap(esc(note)), 'vb-center');
  };
  R.meter = (d, H, o) => {
    const l = level(d); if (!l) return EMPTY('a meter needs { value, min, max }');
    const f = Math.max(0, Math.min(1, (l.v - l.lo) / ((l.hi - l.lo) || 1))), unit = l.unit || ((l.hi === 100 && l.lo === 0) ? '%' : ''); const col = (d && d.col) || ((o && o.draw && o.draw.palette) ? palOf(o)(0, f * 100, 100) : B.ac); const tr = trendOf(d, o);
    const dl = l.delta != null ? '<span class="vb-lbl ' + (num(l.delta) >= 0 ? 'up' : 'dn') + '">' + (num(l.delta) >= 0 ? '▲' : '▼') + ' ' + esc(fmt(Math.abs(num(l.delta)))) + '</span>' : '';
    return wrap('meter', '<div class="vb-hero"><b style="color:' + col + '">' + esc(fmt(l.v)) + '</b><span class="u">' + esc(unit) + '</span>' + dl + '</div><span class="vb-bar2"><i style="width:' + (f * 100).toFixed(1) + '%;background:' + col + '"></i><i style="width:' + (100 - f * 100).toFixed(1) + '%;background:' + mix(B.ac4, 70, B.s3) + '"></i></span>' + cap(esc(String((d && d.note) || (unit === '%' ? '' : fmt(l.v) + ' of ' + fmt(l.hi) + ' ' + unit))))
      + (tr.length > 1 ? '<div class="vb-cols" style="height:' + Math.max(24, H - 84) + 'px"><div class="vb-colbars">' + tr.slice(-16).map((v) => '<i style="height:' + pct(v, Math.max(...tr)).toFixed(0) + '%;background:' + B.ac4 + ';opacity:.7"></i>').join('') + '</div></div>' : ''));
  };
  R.counter = (d, H, o) => {
    const l = level(d); if (!l) return EMPTY('a counter needs a value');
    const s = Math.round(l.v).toLocaleString(); const digits = (o && o.draw && o.draw.digits) ? Math.max(0, num(o.draw.digits) - s.replace(/,/g, '').length) : 0; const tr = trendOf(d, o);
    const dl = l.delta != null ? '<b class="' + (num(l.delta) >= 0 ? 'up' : 'dn') + '">' + (num(l.delta) >= 0 ? '+' : '') + esc(fmt(l.delta)) + (l.unit ? '' : '%') + '</b> on the period before' : '';
    return wrap('counter', '<div class="vb-seg7">' + '0'.repeat(digits).split('').filter(Boolean).map(() => '<span class="p" data-g="8">0</span>').join('') + s.split('').map((ch) => '<span class="' + (ch === ',' || ch === '.' ? 'p' : '') + '" data-g="' + (ch === ',' ? ',' : '8') + '">' + ch + '</span>').join('') + (l.unit ? '<span class="p u">' + esc(l.unit) + '</span>' : '') + '</div>'
      + (tr.length > 1 ? '<div class="vb-tick">' + tr.slice(-24).map((v, i, a) => '<i style="height:' + pct(v, Math.max(...a)).toFixed(0) + '%;background:' + (i === a.length - 1 ? B.ac : mix(B.ac, 55)) + '"></i>').join('') + '</div>' : '') + cap([d && d.note, dl].filter(Boolean).join(' · ')));
  };
  R.level = (d, H, o) => {
    const l = level(d); if (!l) return EMPTY('a level needs { value, max }');
    const cells = (o && o.draw && o.draw.cells) || 10, held = Math.round((l.v - l.lo) / ((l.hi - l.lo) || 1) * cells), warn = num(d && d.expiring) || 0;
    return wrap('level', '<div class="vb-batt"><div class="cells">' + Array.from({ length: cells }, (_, i) => '<i class="' + (i < held ? (i >= held - warn ? 'on warn' : 'on') : '') + '"></i>').join('') + '</div><b></b></div><div class="vb-hero"><b>' + esc(fmt(l.v)) + '</b><span class="u">/ ' + esc(fmt(l.hi)) + ' ' + esc(l.unit || (d && d.what) || 'held') + '</span></div>' + cap(esc(String((d && d.note) || (fmt(l.hi - l.v) + ' free')))));
  };

  /* ── series: the stacked area, the histogram, the step chart, slope, horizon, bump, small multiples, the spark table ── */
  R.area = (d, H, o) => {
    const ms = multi(d); if (!ms.length || !ms.some((s) => s.v.length > 1)) return EMPTY('an area needs series');
    const W = 300, N = Math.max(...ms.map((s) => s.v.length)); const base = new Array(N).fill(H); const tot = new Array(N).fill(0); ms.forEach((s) => s.v.forEach((v, i) => { tot[i] += v; })); const hi = Math.max(...tot) || 1; const pal = palOf(o);
    const paths = ms.map((s, si) => { const top = base.map((b, i) => b - (s.v[i] || 0) / hi * (H - 6)); const dd = 'M0,' + base[0].toFixed(1) + ' ' + top.map((y, i) => 'L' + ((i / (N - 1)) * W).toFixed(1) + ',' + y.toFixed(1)).join(' ') + ' L' + W + ',' + base[N - 1].toFixed(1) + ' Z'; for (let i = 0; i < N; i++) base[i] = top[i]; return '<path d="' + dd + '" fill="' + (s.col || pal(si)) + '" fill-opacity=".55"/>'; });
    return wrap('area', '<div class="vb-chart" style="height:' + chH(H, 26) + 'px"><svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' + paths.join('') + '</svg></div><div class="vb-lg row">' + ms.map((s, i) => '<span><i style="background:' + (s.col || pal(i)) + '"></i>' + esc(s.n) + '</span>').join('') + '</div>');
  };
  R.histogram = (d, H, o) => {
    let kv = keyed(d); let vals = kv.length ? kv.map((x) => x[1]) : series(d); if (!vals.length) { return EMPTY('a histogram needs counts'); }
    if (!kv.length && vals.length > 40) { const bins = (o && o.draw && o.draw.bins) || 14, lo = Math.min(...vals), hi = Math.max(...vals), w = ((hi - lo) || 1) / bins; const c = new Array(bins).fill(0); vals.forEach((v) => { c[Math.min(bins - 1, Math.floor((v - lo) / w))]++; }); kv = c.map((n, i) => [fmt(lo + i * w), n]); vals = c; }
    const hi = Math.max(...vals) || 1, n = vals.length;
    return wrap('histogram', '<div class="vb-chart" style="height:' + chH(H, 22) + 'px"><div class="vb-colbars">' + vals.map((v, i) => '<i style="height:' + pct(v, hi).toFixed(0) + '%;background:' + (i < n * .43 ? B.ac : i < n * .72 ? DV(3) : B.ac3) + '" title="' + esc(kv[i] ? String(kv[i][0]) : '') + ' · ' + fmt(v) + '"></i>').join('') + '</div></div>' + cap(esc(String((d && d.note) || (kv.length ? kv[0][0] + ' ─ ' + kv[Math.floor(n / 2)][0] + ' ─ ' + kv[n - 1][0] : '')))));
  };
  R.step = (d, H, o) => {
    const v = series(d); if (v.length < 2) return EMPTY('a step chart needs two points');
    const W = 150, lo = Math.min(...v), hi = Math.max(...v), sp = (hi - lo) || 1; const pts = []; v.forEach((x, i) => { const x0 = (i / v.length) * W, x1 = ((i + 1) / v.length) * W, y = (H - 6) - (x - lo) / sp * (H - 12); pts.push(x0.toFixed(1) + ',' + y.toFixed(1), x1.toFixed(1) + ',' + y.toFixed(1)); });
    return wrap('step', '<div class="vb-chart" style="height:' + chH(H, 22) + 'px"><svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none"><polyline points="' + pts.join(' ') + '" fill="none" stroke="' + B.ac3 + '" stroke-width="2" stroke-linejoin="miter" vector-effect="non-scaling-stroke"/></svg></div>' + cap('discrete · last ' + fmt(v[v.length - 1]) + ' · peak ' + fmt(hi)));
  };
  R.slope = (d, H, o) => {
    let ms = multi(d); const rw = rows(d); if (!ms.length && rw.length && rw.some((r) => r.before != null || r.a != null)) ms = rw.map((r, i) => ({ n: nameOf(r), v: [num(r.before ?? r.a ?? r.from), num(r.after ?? r.b ?? r.to)] })); if (!ms.length) return EMPTY('a slope needs before/after pairs');
    const all = ms.flatMap((s) => [s.v[0], s.v[s.v.length - 1]]); const lo = Math.min(...all), hi = Math.max(...all), sp = (hi - lo) || 1; const y = (v) => (92 - (v - lo) / sp * 84).toFixed(1); const pal = palOf(o);
    return wrap('slope', '<div class="vb-chart" style="height:' + chH(H, 22) + 'px"><svg viewBox="0 0 120 100" preserveAspectRatio="none">' + ms.slice(0, 8).map((s, i) => { const a = s.v[0], b = s.v[s.v.length - 1]; return '<line x1="18" y1="' + y(a) + '" x2="102" y2="' + y(b) + '" stroke="' + (s.col || (b > a ? B.ac2 : pal(i))) + '" stroke-width="2" vector-effect="non-scaling-stroke"><title>' + esc(s.n) + ' · ' + fmt(a) + ' → ' + fmt(b) + '</title></line>'; }).join('') + '</svg></div>' + cap(esc(String((d && d.note) || 'before → after · ' + ms.length + ' lines'))));
  };
  R.horizon = (d, H, o) => {
    const ms = multi(d); if (!ms.length) return EMPTY('a horizon needs series');
    const hi = Math.max(...ms.flatMap((s) => s.v)) || 1, pal = palOf(o, 'load');
    return wrap('horizon', ms.slice(0, 8).map((s, si) => '<div class="vb-hz"><span class="n">' + esc(s.n) + '</span><span class="bars">' + s.v.slice(-24).map((v) => '<i style="height:' + pct(v, hi).toFixed(0) + '%;background:' + (v / hi > .75 ? B.ac3 : v / hi > .45 ? B.ac : mix(B.ac, 45)) + '"></i>').join('') + '</span></div>').join(''));
  };
  R.bump = (d, H, o) => {
    const ms = multi(d); if (!ms.length || !ms.some((s) => s.v.length > 1)) return EMPTY('a bump needs ranks over time'); const N = Math.max(...ms.map((s) => s.v.length)), K = ms.length, pal = palOf(o);
    return wrap('bump', '<div class="vb-chart" style="height:' + chH(H, 20) + 'px"><svg viewBox="0 0 150 90" preserveAspectRatio="none">' + ms.map((s, i) => '<polyline points="' + s.v.map((rk, j) => ((j / Math.max(1, N - 1)) * 150).toFixed(1) + ',' + (8 + (Math.max(1, rk) - 1) * (74 / Math.max(1, K - 1))).toFixed(1)).join(' ') + '" fill="none" stroke="' + (s.col || pal(i)) + '" stroke-width="2" vector-effect="non-scaling-stroke"><title>' + esc(s.n) + '</title></polyline>').join('') + '</svg></div><div class="vb-bumpl">' + ms.slice(0, 6).map((s, i) => '<span style="color:' + (s.col || pal(i)) + '">' + esc(s.n) + '</span>').join('') + '</div>');
  };
  R['small-multiples'] = (d, H, o) => {
    const ms = multi(d); if (!ms.length) return EMPTY('small multiples need series');
    return wrap('small-multiples', ms.slice(0, (o && o.draw && o.draw.rows) || 6).map((s, i) => { const last = s.r && (s.r.last ?? s.r.value ?? s.r.v); const bad = s.r && (s.r.status === 'timeout' || s.r.state === 'down' || s.r.timeout); const col = bad ? B.ac3 : (s.col || B.ac); return '<span class="vb-sm"><span class="vb-lbl">' + esc(s.n) + '</span>' + spark(s.v, col) + '<span class="v" style="color:' + col + '">' + esc(bad ? String(s.r.status || 'timeout') : (last != null ? String(typeof last === 'number' ? fmt(last) : last) : fmt(s.v[s.v.length - 1]))) + '</span></span>'; }).join(''));
  };
  R['spark-table'] = (d, H, o) => {
    const rw = rows(d); const ms = multi(d); if (!ms.length || !ms.some((s) => s.r)) return EMPTY('a spark table needs rows with a series');
    const extra = Object.keys(ms[0].r || {}).filter((k) => !Array.isArray(ms[0].r[k]) && typeof ms[0].r[k] !== 'object' && !['name', 'title', 'label', 'id', 'col', 'color', 'status', 'state'].includes(k)).slice(0, 3);
    return wrap('spark-table', '<div class="vb-sth"><span>' + esc((o && o.draw && o.draw.name) || 'row') + '</span><span>' + esc((o && o.draw && o.draw.figure) || 'trend') + '</span>' + extra.map((k) => '<span>' + esc(k) + '</span>').join('') + '</div>' + ms.slice(0, 8).map((s, i) => { const bad = s.r && /timeout|down/.test(String(s.r.status ?? s.r.state ?? '')); return '<div class="vb-str" style="grid-template-columns:50px 1fr' + ' 40px'.repeat(extra.length) + '"><span class="n">' + esc(s.n) + '</span>' + spark(s.v, bad ? B.ac3 : (s.col || B.ac)) + extra.map((k) => '<span class="v">' + esc(String(s.r[k] ?? '—')) + '</span>').join('') + '</div>'; }).join(''));
  };

  /* ── values: columns, ranked, lollipop, waterfall, pareto, box, diverging, bullet, threshold, radar, numbers, pills ── */
  R.column = (d, H, o) => {
    const kv = keyed(d); const vals = kv.length ? kv.map((x) => x[1]) : series(d); if (!vals.length) return EMPTY('columns need values');
    const hi = Math.max(...vals) || 1, pal = palOf(o, 'load'), lim = (o && o.draw && o.draw.limit) || 24;
    return wrap('column', '<div class="vb-chart" style="height:' + chH(H, 22) + 'px"><div class="vb-colbars gap">' + vals.slice(-lim).map((v, i) => '<i style="height:' + pct(v, hi).toFixed(0) + '%;background:' + pal(i, v, hi) + '" title="' + esc(kv[i] ? String(kv[i][0]) + ' · ' : '') + fmt(v) + '"></i>').join('') + '</div></div>' + cap(esc(String((d && d.note) || ('last ' + Math.min(vals.length, lim) + ' · peak ' + fmt(hi))))));
  };
  R.ranked = (d, H, o) => {
    const kv = keyed(d).slice().sort((a, b) => b[1] - a[1]).slice(0, (o && o.draw && o.draw.limit) || 6); if (!kv.length) return EMPTY('ranked bars need values'); const hi = kv[0][1] || 1, pal = palOf(o); const rw = rows(d);
    return wrap('ranked', kv.map((x, i) => { const r = rw.find((q) => nameOf(q) === x[0]) || {}; return '<span class="vb-rw"><span class="n" title="' + esc(x[0]) + '">' + esc(x[0]) + '</span><span class="tr"><i style="width:' + pct(x[1], hi).toFixed(1) + '%;background:' + (r.col || pal(i)) + '"></i></span><span class="v">' + esc(String(r.text ?? r.size ?? fmt(x[1]))) + '</span></span>'; }).join(''));
  };
  R.lollipop = (d, H, o) => {
    const kv = keyed(d).slice(0, 8); if (!kv.length) return EMPTY('a lollipop needs values'); const hi = Math.max(...kv.map((x) => Math.abs(x[1]))) || 1, pal = palOf(o);
    return wrap('lollipop', kv.map((x, i) => '<span class="vb-ll"><span class="n">' + esc(x[0]) + '</span><span class="st"><b style="width:' + pct(Math.abs(x[1]), hi).toFixed(1) + '%"></b><i style="left:' + pct(Math.abs(x[1]), hi).toFixed(1) + '%;background:' + pal(i) + '"></i></span><span class="v">' + esc(fmt(x[1])) + '</span></span>').join(''));
  };
  R.waterfall = (d, H, o) => {
    const kv = keyed(d).slice(0, 8); if (!kv.length) return EMPTY('a waterfall needs parts'); const tot = kv.reduce((s, x) => s + Math.abs(x[1]), 0) || 1, pal = palOf(o); let cum = 0; const n = kv.length, w = 96 / n;
    return wrap('waterfall', '<div class="vb-chart rel" style="height:' + chH(H, 22) + 'px">' + kv.map((x, i) => { const h = Math.abs(x[1]) / tot * 82, y = 88 - cum - h; const el = '<span style="position:absolute;border-radius:2px;left:' + (2 + i * w).toFixed(1) + '%;top:' + y.toFixed(1) + '%;width:' + (w * .8).toFixed(1) + '%;height:' + h.toFixed(1) + '%;background:' + (i === n - 1 ? B.ac2 : pal(i)) + '" title="' + esc(x[0]) + ' · ' + fmt(x[1]) + '"></span>'; if (i < n - 1) cum += h; return el; }).join('') + '</div>' + cap(kv.map((x) => esc(x[0])).join(' → ')));
  };
  R.pareto = (d, H, o) => {
    const kv = keyed(d).slice().sort((a, b) => b[1] - a[1]).slice(0, 10); if (!kv.length) return EMPTY('a pareto needs counts'); const hi = kv[0][1] || 1, tot = kv.reduce((s, x) => s + x[1], 0) || 1; let c = 0;
    const line = kv.map((x, i) => { c += x[1]; return ((i + .5) / kv.length * 100).toFixed(1) + ',' + (100 - c / tot * 100).toFixed(1); }).join(' '); let c2 = 0; const top2 = kv.slice(0, 2).reduce((s, x) => s + x[1], 0);
    return wrap('pareto', '<div class="vb-par" style="height:' + chH(H, 22) + 'px"><div class="bars">' + kv.map((x, i) => '<i style="height:' + pct(x[1], hi).toFixed(0) + '%;background:' + (i < 2 ? B.ac : DV(2)) + '" title="' + esc(x[0]) + ' · ' + fmt(x[1]) + '"></i>').join('') + '</div><svg viewBox="0 0 100 100" preserveAspectRatio="none"><polyline points="' + line + '" fill="none" stroke="' + B.ac3 + '" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg></div>' + cap(esc(kv[0][0]) + (kv[1] ? ' + ' + esc(kv[1][0]) : '') + ' are ' + Math.round(top2 / tot * 100) + '% of all'));
  };
  R.box = (d, H, o) => {
    const rw = rows(d).filter((r) => r.q1 != null || r.median != null || r.md != null || Array.isArray(r.values) || Array.isArray(r.samples)); if (!rw.length) return EMPTY('a box plot needs rows with quartiles or samples');
    const q = (r) => { if (r.q1 != null) return { lo: num(r.lo ?? r.min ?? r.q1), q1: num(r.q1), md: num(r.md ?? r.median ?? r.q2), q3: num(r.q3), hi: num(r.hi ?? r.max ?? r.q3) }; const v = series(r.values || r.samples).slice().sort((a, b) => a - b), at = (f) => v[Math.min(v.length - 1, Math.floor(f * (v.length - 1)))]; return { lo: v[0], q1: at(.25), md: at(.5), q3: at(.75), hi: v[v.length - 1] }; };
    const qs = rw.map(q); const lo = Math.min(...qs.map((x) => x.lo)), hi = Math.max(...qs.map((x) => x.hi)), sp = (hi - lo) || 1, P = (v) => ((v - lo) / sp * 100).toFixed(1) + '%', pal = palOf(o);
    return wrap('box', rw.slice(0, 6).map((r, i) => { const x = qs[i]; return '<span class="vb-bx"><span class="n">' + esc(nameOf(r)) + '</span><span class="r"><b class="wk" style="left:' + P(x.lo) + ';width:' + ((x.hi - x.lo) / sp * 100).toFixed(1) + '%"></b><i class="q" style="left:' + P(x.q1) + ';width:' + ((x.q3 - x.q1) / sp * 100).toFixed(1) + '%;background:' + pal(i) + '"></i><i class="md" style="left:' + P(x.md) + '"></i></span></span>'; }).join(''));
  };
  R.diverging = (d, H, o) => {
    const kv = keyed(d).slice(0, 8); if (!kv.length) return EMPTY('a diverging needs signed values'); const centre = num(o && o.draw && o.draw.centre) || 0; const hi = Math.max(...kv.map((x) => Math.abs(x[1] - centre))) || 1;
    return wrap('diverging', kv.map((x) => { const v = x[1] - centre, w = Math.abs(v) / hi * 50; return '<span class="vb-dv"><span class="n">' + esc(x[0]) + '</span><span class="ax"><b></b><i style="left:' + (v < 0 ? 50 - w : 50).toFixed(1) + '%;width:' + w.toFixed(1) + '%;background:' + (v < 0 ? B.ac2 : B.ac3) + '" title="' + fmt(x[1]) + '"></i></span></span>'; }).join('') + cap('◀ under · over ▶'));
  };
  R.bullet = (d, H, o) => {
    const kv = keyed(d).slice(0, 6); if (!kv.length) return EMPTY('bullet bars need values'); const rw = rows(d); const hi = Math.max(...kv.map((x) => x[1]), ...rw.map((r) => num(r.max ?? r.target ?? 0))) || 1, pal = palOf(o, 'status');
    return wrap('bullet', kv.map((x, i) => { const r = rw.find((q) => nameOf(q) === x[0]) || {}; const tg = r.target ?? (o && o.draw && o.draw.target); return '<span class="vb-rw"><span class="n">' + esc(x[0]) + '</span><span class="tr"><i style="width:' + pct(x[1], num(r.max) || hi).toFixed(1) + '%;background:' + (r.col || (r.status ? stCol(r.status) : DV(i))) + '"></i>' + (tg != null ? '<em style="left:' + pct(num(tg), num(r.max) || hi).toFixed(1) + '%"></em>' : '') + '</span><span class="v">' + esc(String(r.text ?? fmt(x[1]))) + '</span></span>'; }).join(''));
  };
  R.threshold = (d, H, o) => {
    const kv = keyed(d).slice(0, 8); if (!kv.length) return EMPTY('a threshold scale needs values'); const max = num(o && o.draw && o.draw.max) || Math.max(90, ...kv.map((x) => x[1])); const bands = (o && o.draw && Array.isArray(o.draw.bands)) ? o.draw.bands.map(num) : [70, 85]; const unit = (o && o.draw && o.draw.unit) || '°';
    return wrap('threshold', kv.map((x) => '<span class="vb-sc"><span class="n">' + esc(x[0]) + '</span><span class="g" style="background:linear-gradient(90deg,' + B.s3 + ' ' + (bands[0] / max * 100 - 15).toFixed(0) + '%,' + mix(B.ac3, 45, B.s3) + ' ' + (bands[0] / max * 100).toFixed(0) + '%,' + mix(B.ac4, 55, B.s3) + ' 100%)"><b style="left:' + pct(x[1], max).toFixed(1) + '%"></b></span><span class="v" style="color:' + (x[1] >= bands[1] ? B.ac4 : x[1] >= bands[0] ? B.ac3 : B.t1) + '">' + esc(fmt(x[1]) + unit) + '</span></span>').join(''));
  };
  R.radar = (d, H, o) => {
    const ev = evsOf(d); if (ev.length && !keyed(d).length && R.sweep) return R.sweep(d, H, o);
    const kv = keyed(d).slice(0, 8); if (kv.length < 3) return EMPTY('a radar needs three or more values'); const n = kv.length, hi = Math.max(...kv.map((x) => x[1])) || 1, cx = 60, cy = 54;
    const ring = (f) => kv.map((_, i) => { const A = (-90 + i * 360 / n) * Math.PI / 180; return (cx + Math.cos(A) * 44 * f).toFixed(1) + ',' + (cy + Math.sin(A) * 44 * f).toFixed(1); }).join(' ');
    const data = kv.map((x, i) => { const A = (-90 + i * 360 / n) * Math.PI / 180, r = 44 * x[1] / hi; return (cx + Math.cos(A) * r).toFixed(1) + ',' + (cy + Math.sin(A) * r).toFixed(1); }).join(' ');
    return wrap('radar', '<div class="vb-chart" style="height:' + chH(H, 20) + 'px"><svg viewBox="0 0 120 110" style="width:auto;margin:0 auto"><polygon points="' + ring(1) + '" fill="none" stroke="' + B.bd + '"/><polygon points="' + ring(.6) + '" fill="none" stroke="' + B.bd + '"/><polygon points="' + data + '" fill="' + B.ac + '" fill-opacity=".28" stroke="' + B.ac + '" stroke-width="1.5"/></svg></div>' + cap(kv.map((x) => esc(x[0])).join(' · ')));
  };
  R.numbers = (d, H, o) => {
    const kv = keyed(d).slice(0, 6); const rw = rows(d); if (!kv.length) return EMPTY('a number grid needs values'); const pal = palOf(o, 'accent');
    return wrap('numbers', '<div class="vb-bigs">' + kv.map((x, i) => { const r = rw.find((q) => nameOf(q) === x[0]) || {}; return '<div><b style="color:' + (r.col || (i === 0 ? B.ac : i === 1 ? B.ac2 : i === 2 ? B.ac3 : B.t1)) + '">' + esc(String(r.text ?? fmt(x[1]))) + '</b><span>' + esc(x[0]) + '</span></div>'; }).join('') + '</div>');
  };
  R.pills = (d, H, o) => {
    const kv = keyed(d); const st = rows(d);
    if (st.length && st.some((r) => r.status != null || r.state != null)) return wrap('pills', '<div class="vb-pillw">' + st.slice(0, 14).map((r) => { const s = String(r.status ?? r.state ?? ''); return '<span title="' + esc(s) + '"><i style="background:' + stCol(s) + '"></i>' + esc(nameOf(r)) + '</span>'; }).join('') + '</div>');
    if (kv.length) return wrap('pills', '<div class="vb-pillw">' + kv.slice(0, 14).map((x, i) => '<span><i style="background:' + DV(i) + '"></i>' + esc(x[0]) + '<b>' + esc(fmt(x[1])) + '</b></span>').join('') + '</div>');
    return EMPTY('pills need rows with a status or { name: number }');
  };

  /* ── parts: the funnel, the waffle, the stacked bar, the treemap, the donut ── */
  R.funnel = (d, H, o) => {
    const st = stagesOf(d); const kv = st.length && st.some((s) => s.value != null || s.count != null) ? st.map((s) => [nameOf(s), valOf(s)]) : keyed(d); if (!kv.length) return EMPTY('a funnel needs stages with counts'); const hi = kv[0][1] || 1, pal = palOf(o);
    return wrap('funnel', '<div class="vb-funnel">' + kv.slice(0, 7).map((x, i) => '<span style="width:' + Math.max(28, pct(x[1], hi)).toFixed(0) + '%;background:' + (i === kv.length - 1 ? B.ac2 : i === 0 ? B.ac : pal(i)) + '">' + esc(x[0]) + '<b>' + esc(fmt(x[1])) + '</b></span>').join('') + '</div>');
  };
  R.waffle = (d, H, o) => {
    const kv = keyed(d).slice(0, 6); if (!kv.length) return EMPTY('a waffle needs parts'); const cells = (o && o.draw && o.draw.cells) || 100; const l = level(d); const tot = (l && kv.length === 1) ? l.hi : kv.reduce((s, x) => s + Math.abs(x[1]), 0) || 1; const pal = palOf(o);
    const cols = cells === 60 ? 10 : 10; let acc = 0; const fills = []; kv.forEach((x, i) => { const n = Math.round(Math.abs(x[1]) / tot * cells); for (let j = 0; j < n && fills.length < cells; j++) fills.push(i === 0 ? B.ac : pal(i)); }); while (fills.length < cells) fills.push(B.s3);
    return wrap('waffle', '<div class="vb-waffle" style="grid-template-columns:repeat(' + cols + ',1fr)">' + fills.map((c) => '<i style="background:' + c + '"></i>').join('') + '</div>' + cap(esc(String((d && d.note) || (fmt(kv[0][1]) + ' of ' + fmt(tot) + ' ' + kv[0][0])))));
  };
  R['stacked-bar'] = (d, H, o) => {
    const kv = keyed(d).slice(0, 8); if (!kv.length) return EMPTY('a stacked bar needs parts'); const tot = kv.reduce((s, x) => s + Math.abs(x[1]), 0) || 1, pal = palOf(o); const rw = rows(d);
    return wrap('stacked-bar', '<span class="vb-stackbar">' + kv.map((x, i) => '<i style="width:' + (Math.abs(x[1]) / tot * 100).toFixed(1) + '%;background:' + pal(i) + '" title="' + esc(x[0]) + ' · ' + fmt(x[1]) + '"></i>').join('') + '</span><div class="vb-lg">' + kv.map((x, i) => { const r = rw.find((q) => nameOf(q) === x[0]) || {}; return '<span><i style="background:' + pal(i) + '"></i>' + esc(x[0]) + '<b>' + esc(String(r.text ?? fmt(x[1]))) + '</b></span>'; }).join('') + '</div>');
  };
  R.treemap = (d, H, o) => {
    const kv = keyed(d).slice().sort((a, b) => b[1] - a[1]).slice(0, 10); if (!kv.length) return EMPTY('a treemap needs parts'); const pal = palOf(o);
    // the board's rule for its eight: two big, two middling, the rest small — widths within a row by share
    const rowsOf = [kv.slice(0, 2), kv.slice(2, 4), kv.slice(4)].filter((r) => r.length), hs = [46, 28, 24];
    return wrap('treemap', '<div class="vb-tmap" style="height:' + chH(H, 4) + 'px">' + rowsOf.map((r, ri) => { const t = r.reduce((s, x) => s + x[1], 0) || 1; return r.map((x) => '<span style="width:calc(' + (x[1] / t * 100).toFixed(1) + '% - 2px);height:' + hs[ri] + '%;background:' + pal(kv.indexOf(x)) + '" title="' + esc(x[0]) + ' · ' + fmt(x[1]) + '">' + esc(x[0]) + '</span>').join(''); }).join('') + '</div>');
  };

  /* ── matrices and calendars: the heat map, the status matrix, the dot matrix, the calendar, node health tabs ── */
  const cellsOf = (d) => { // rows × columns of numbers: {row:{col:v}} · [{name, cells:[…]}] · [{name, a, b, c}] · many series
    if (d && typeof d === 'object' && !Array.isArray(d) && Object.keys(d).some((k) => d[k] && typeof d[k] === 'object' && !Array.isArray(d[k]))) { const rws = Object.keys(d).filter((k) => d[k] && typeof d[k] === 'object' && !Array.isArray(d[k])); const cols = Object.keys(d[rws[0]]).filter((c) => typeof d[rws[0]][c] !== 'object'); return { rows: rws.map((k) => ({ n: k, v: cols.map((c) => d[k][c]) })), cols }; }
    const rw = rows(d); if (rw.length && rw.some((r) => Array.isArray(r.cells || r.values || r.series))) return { rows: rw.map((r) => ({ n: nameOf(r), v: (r.cells || r.values || r.series).map((x) => typeof x === 'number' ? x : valOf(x)), t: r.total ?? r.value })), cols: [] };
    if (rw.length) { const cols = Object.keys(rw[0]).filter((c) => typeof rw[0][c] !== 'object' && !['name', 'id', 'title', 'label'].includes(c)); return { rows: rw.map((r) => ({ n: nameOf(r), v: cols.map((c) => r[c]) })), cols }; }
    const ms = multi(d); if (ms.length) return { rows: ms.map((s) => ({ n: s.n, v: s.v })), cols: [] }; return { rows: [], cols: [] }; };
  R.heat = (d, H, o) => {
    const kv = keyed(d); const m = kv.length ? { rows: [], cols: [] } : cellsOf(d);
    if (m.rows.length && m.rows.some((r) => r.v.some((x) => typeof x === 'number'))) { const hi = Math.max(...m.rows.flatMap((r) => r.v.map((x) => num(x)))) || 1; const pal = palOf(o);
      return wrap('heat', m.rows.slice(0, 8).map((r, ri) => '<span class="vb-heatrow"><span class="vb-lbl">' + esc(r.n) + '</span><span class="vb-heat" style="grid-template-columns:repeat(' + r.v.length + ',1fr)">' + r.v.map((x, ci) => '<i style="background:' + mix(pal(ri), Math.round(8 + num(x) / hi * 88), 'transparent') + '" title="' + esc(r.n) + (m.cols[ci] ? ' · ' + esc(String(m.cols[ci])) : '') + ' · ' + fmt(x) + '"></i>').join('') + '</span><span class="v">' + esc(fmt(r.t != null ? r.t : r.v.reduce((s, x) => s + num(x), 0))) + '</span></span>').join('')); }
    if (!kv.length) return EMPTY('a heat map needs rows of numbers');
    const hi = Math.max(...kv.map((x) => Math.abs(x[1]))) || 1, cols = Math.min(12, Math.max(4, kv.length)); const pal = palOf(o, 'load');
    return wrap('heat', '<div class="vb-heatstrip" style="grid-template-columns:repeat(' + cols + ',1fr)">' + kv.slice(0, 48).map((x, i) => '<i style="background:' + pal(i, x[1], hi) + ';opacity:' + (0.25 + 0.75 * Math.abs(x[1]) / hi).toFixed(2) + '" title="' + esc(x[0]) + ' · ' + fmt(x[1]) + '"></i>').join('') + '</div>');
  };
  R.matrix = (d, H, o) => {
    const m = cellsOf(d); if (!m.rows.length) return EMPTY('a matrix needs rows of values'); const cols = m.cols.length ? m.cols : m.rows[0].v.map((_, i) => String(i + 1));
    const col = (v) => typeof v === 'number' ? (v >= 1 ? B.ac2 : v <= 0 ? B.ac4 : v >= .5 ? B.ac3 : B.s3) : (v === true ? B.ac2 : v === false ? B.ac4 : stCol(v));
    return wrap('matrix', '<div class="vb-mxh" style="grid-template-columns:54px repeat(' + cols.length + ',1fr)"><span></span>' + cols.map((c) => '<span>' + esc(String(c)) + '</span>').join('') + '</div>' + m.rows.slice(0, 8).map((r) => '<div class="vb-mxr" style="grid-template-columns:54px repeat(' + cols.length + ',1fr)"><span class="n">' + esc(r.n) + '</span>' + r.v.map((v, i) => '<i style="background:' + col(v) + '" title="' + esc(r.n) + ' · ' + esc(String(cols[i])) + ' · ' + esc(String(v)) + '"></i>').join('') + '</div>').join('') + cap('green ok · amber degraded · red down · grey not deployed'));
  };
  R.dots = (d, H, o) => {
    const l = level(d); let fills; const cells = 160;
    if (l && (d.busy != null || d.total != null || l.hi)) { const tot = num(d.total ?? l.hi) || cells, busy = num(d.busy ?? l.v); fills = Array.from({ length: cells }, (_, i) => i < busy / tot * cells ? B.ac : B.s3); }
    else { const v = keyed(d).length ? keyed(d).map((x) => x[1]) : series(d); if (!v.length) return EMPTY('a dot matrix needs a level or values'); const hi = Math.max(...v) || 1; fills = v.slice(0, cells).map((x) => x / hi > .66 ? B.ac : x / hi > .33 ? mix(B.ac, 45) : B.s3); }
    return wrap('dots', '<div class="vb-dots">' + fills.map((c) => '<i style="background:' + c + '"></i>').join('') + '</div>' + cap(esc(String((d && d.note) || (l ? fmt(d.total ?? l.hi) + ' slots · ' + fmt(d.busy ?? l.v) + ' busy' : fills.length + ' cells')))));
  };
  R.calendar = (d, H, o) => {
    let by = {}; const rw = rows(d);
    if (d && typeof d === 'object' && !Array.isArray(d) && d.days && typeof d.days === 'object') Object.keys(d.days).forEach((k) => { by[dayOf(k)] = num(d.days[k]); });
    else if (d && typeof d === 'object' && !Array.isArray(d) && Object.keys(d).length && Object.keys(d).every((k) => /^\d{4}-\d{2}-\d{2}/.test(k))) Object.keys(d).forEach((k) => { by[dayOf(k)] = num(d[k]); });
    else rw.forEach((r) => { const k = dayOf(r.when ?? r.date ?? r.day ?? r.t ?? r.start); if (k) by[k] = (by[k] || 0) + (r.value != null ? num(r.value) : (r.count != null ? num(r.count) : 1)); });
    const days = Object.keys(by).sort(); if (!days.length) return EMPTY('a calendar needs dated rows');
    const weeks = (o && o.draw && o.draw.weeks) || 12, end = new Date(days[days.length - 1] + 'T00:00:00'), start = new Date(end); start.setDate(end.getDate() - weeks * 7 + 1); const hi = Math.max(...days.map((k) => by[k])) || 1; const cells = [];
    const dk = (t) => t.getFullYear() + '-' + String(t.getMonth() + 1).padStart(2, '0') + '-' + String(t.getDate()).padStart(2, '0');
    for (let t = new Date(start); t <= end; t.setDate(t.getDate() + 1)) { const k = dk(t), v = by[k] || 0; cells.push('<i style="background:' + mix(B.ac2, Math.round(6 + v / hi * 84), 'transparent') + '" title="' + k + ' · ' + fmt(v) + '"></i>'); }
    const tot = days.reduce((s, k) => s + by[k], 0);
    return wrap('calendar', '<div class="vb-cal" style="grid-template-rows:repeat(7,1fr);height:' + chH(H, 22) + 'px">' + cells.join('') + '</div>' + cap(weeks + ' weeks · ' + fmt(tot) + ' ' + esc((o && o.draw && o.draw.what) || 'items')));
  };
  R.tabs = (d, H, o) => {
    const m = cellsOf(d); if (!m.rows.length || !m.cols.length) return EMPTY('tabs need rows × measures'); const cur = String(ui(o, 'tab', m.cols[0])), mi = Math.max(0, m.cols.indexOf(cur)); const maxOf = m.cols.map((c, ci) => Math.max(...m.rows.map((r) => num(r.v[ci]))) || 1); const pal = palOf(o, 'load');
    return wrap('tabs', '<div class="vb-tabs">' + m.cols.slice(0, 5).map((c) => '<button class="' + (c === m.cols[mi] ? 'on' : '') + '"' + set('tab', c) + '>' + esc(String(c)) + '</button>').join('') + '</div>' + m.rows.slice(0, 6).map((r) => { const v = num(r.v[mi]), down = r.v.every((x) => !num(x)); return '<div class="vb-nhr"><span class="n">' + esc(r.n) + '</span><span class="tr"><i style="width:' + (down ? 0 : pct(v, maxOf[mi])).toFixed(0) + '%;background:' + (down ? B.t3 : pal(0, v, maxOf[mi])) + '"></i></span><span class="v">' + (down ? 'down' : esc(fmt(v))) + '</span><span class="pips">' + m.cols.slice(0, 5).map((c, ci) => '<i style="height:' + (down ? 10 : 22 + num(r.v[ci]) / maxOf[ci] * 78).toFixed(0) + '%;background:' + (ci === mi ? B.t1 : down ? B.s3 : pal(0, num(r.v[ci]), maxOf[ci])) + '" title="' + esc(String(c)) + ' ' + fmt(r.v[ci]) + '"></i>').join('') + '</span></div>'; }).join('') + cap(esc(String(m.cols[mi])) + ' per row · the pips are the other measures'));
  };
  R.slider = (d, H, o) => {
    const kv = keyed(d).slice(0, 8); if (!kv.length) return EMPTY('a threshold slider needs values'); const max = num(o && o.draw && o.draw.max) || Math.max(95, ...kv.map((x) => x[1])), min = num(o && o.draw && o.draw.min) || 0; const thr = num(ui(o, 'thr', (o && o.draw && o.draw.threshold) || 70)); const over = kv.filter((x) => x[1] >= thr).length; const unit = (o && o.draw && o.draw.unit) || '°';
    return wrap('slider', '<div class="vb-sld"><span>' + esc((o && o.draw && o.draw.what) || 'threshold') + ' ≥</span><input type="range" min="' + min + '" max="' + max + '" value="' + thr + '" data-vb-input="thr"><b>' + thr + esc(unit) + '</b></div>' + kv.map((x) => '<span class="vb-sc"><span class="n">' + esc(x[0]) + '</span><span class="g"><em style="left:' + pct(thr, max).toFixed(1) + '%"></em><b style="left:' + pct(x[1], max).toFixed(1) + '%"></b></span><span class="v" style="color:' + (x[1] >= thr ? B.ac4 : B.t1) + '">' + esc(fmt(x[1]) + unit) + '</span></span>').join('') + cap(over + ' of ' + kv.length + ' at or over ' + thr + esc(unit) + ' · drag to move the line'));
  };

  /* ── stages and time: the stepper, the gantt, the timeline, the pipeline card, the checklist ── */
  const stepr = (st) => '<div class="vb-stepr">' + st.slice(0, 8).map((s, i) => '<span class="' + s.st + '"><i>' + (s.st === 'done' ? '✓' : s.st === 'bad' ? '✕' : (i + 1)) + '</i><em>' + esc(nameOf(s) || String(s.step ?? '')) + '</em></span>').join('') + '</div>';
  R.stepper = (d, H, o) => {
    const st = stagesOf(d); if (!st.length) return EMPTY('stages need a list'); const now = st.findIndex((s) => s.st === 'now');
    return wrap('stepper', stepr(st) + cap(st.map((s) => esc(nameOf(s))).join(' → ') + (now >= 0 ? ' · currently at ' + esc(nameOf(st[now])) : '')));
  };
  R.gantt = (d, H, o) => {
    const st = stagesOf(d); if (!st.length) return EMPTY('a gantt needs rows with a start and a length'); const s0 = st.map((s) => num(s.start ?? s.x ?? s.from ?? 0)), s1 = st.map((s, i) => num(s.end ?? s.to ?? (s0[i] + num(s.duration ?? s.w ?? s.ms ?? s.len ?? 1)))); const lo = Math.min(...s0), hi = Math.max(...s1), sp = (hi - lo) || 1;
    return wrap('gantt', st.slice(0, (o && o.draw && o.draw.rows) || 8).map((s, i) => '<span class="vb-gr"><span class="n">' + esc(nameOf(s)) + '</span><span class="track"><i style="left:' + ((s0[i] - lo) / sp * 100).toFixed(1) + '%;width:' + Math.max(1, (s1[i] - s0[i]) / sp * 100).toFixed(1) + '%;background:' + (s.col || (s.st === 'done' ? B.ac2 : s.st === 'now' ? B.ac : s.st === 'bad' ? B.ac4 : B.t3)) + '" title="' + esc(nameOf(s)) + ' · ' + fmt(s0[i]) + ' → ' + fmt(s1[i]) + '"></i></span></span>').join(''));
  };
  R.timeline = (d, H, o) => {
    const ev = evsOf(d).filter((r) => hhmm(r.when ?? r.t ?? r.ts ?? r.time).includes(':')); if (!ev.length) return EMPTY('a timeline needs timed events'); const mins = (t) => { const m = hhmm(t).match(/(\d{1,2}):(\d{2})/); return +m[1] * 60 + +m[2]; }; const ms = ev.map((r) => mins(r.when ?? r.t ?? r.ts ?? r.time)); const lo = Math.floor(Math.min(...ms) / 60) * 60, hi = Math.ceil((Math.max(...ms) + 1) / 60) * 60, sp = (hi - lo) || 1; const now = new Date().getHours() * 60 + new Date().getMinutes();
    return wrap('timeline', '<div class="vb-tl" style="height:' + chH(H, 22) + 'px"><b class="ax"></b>' + (now >= lo && now <= hi ? '<i class="now" style="left:' + ((now - lo) / sp * 100).toFixed(1) + '%"></i>' : '') + ev.slice(0, 10).map((r, i) => '<span class="' + (i % 2 ? 'up' : '') + '" style="left:' + ((ms[i] - lo) / sp * 100).toFixed(1) + '%;--ec:' + (r.col || r.color || (stCol(r.kind ?? r.level) === B.t3 ? DV(i) : stCol(r.kind ?? r.level))) + '"><i></i><em>' + esc(String(r.title ?? txt(r)).slice(0, 14)) + '</em><small>' + esc(hhmm(r.when ?? r.t ?? r.ts ?? r.time)) + '</small></span>').join('') + '</div>' + cap(String(lo / 60).padStart(2, '0') + ':00 → ' + String(hi / 60).padStart(2, '0') + ':00 · the marker is now'));
  };
  R.pipeline = (d, H, o) => {
    const st = stagesOf(d); if (!st.length) return EMPTY('a pipeline needs stages'); const src = (d && typeof d === 'object' && !Array.isArray(d)) ? (d.stats && typeof d.stats === 'object' ? d.stats : d) : {}; const cards = Object.keys(src).filter((k) => !['stages', 'steps', 'stats', 'name', 'id', 'title'].includes(k) && (typeof src[k] === 'number' || typeof src[k] === 'string')).slice(0, 4);
    return wrap('pipeline', stepr(st) + (cards.length ? '<div class="vb-pcard">' + cards.map((k, i) => '<div><b style="color:' + (/fail|err/.test(k) ? B.ac4 : /pass|ok|merged/.test(k) ? B.ac2 : B.t1) + '">' + esc(String(src[k])) + '</b><span>' + esc(k.replace(/_/g, ' ')) + '</span></div>').join('') + '</div>' : '') + cap(esc(String((d && d.note) || ''))));
  };
  R.checklist = (d, H, o) => {
    const rw = rows(d); if (!rw.length) return EMPTY('a checklist needs items'); const done = rw.filter((r) => r.done || r.checked || r.state === 'done' || r.status === 'done').length;
    return wrap('checklist', '<div class="vb-chk">' + rw.slice(0, 10).map((r) => { const on = !!(r.done || r.checked || r.state === 'done' || r.status === 'done'); return '<span class="vb-ck' + (on ? ' done' : '') + '"><i>' + (on ? '✓' : '') + '</i>' + esc(String(r.text ?? r.title ?? r.name ?? '')) + (r.due || r.when ? '<small>' + esc(String(r.due ?? r.when)) + '</small>' : '') + '</span>'; }).join('') + '</div>' + cap(done + ' of ' + rw.length + (d && d.note ? ' · ' + esc(String(d.note)) : '')));
  };

  /* ── events: the log stream, the lane, the comet and pulse are motion forms (next section) ── */
  R.log = (d, H, o) => {
    const rw = evsOf(d); if (!rw.length || !rw.some((r) => r.text != null || r.msg != null || r.message != null || r.line != null || r.title != null)) return EMPTY('a log needs rows with text');
    const kc = (k) => /err|fail|crit/i.test(k) ? B.ac4 : /warn/i.test(k) ? B.ac3 : /loop|step|run/i.test(k) ? B.t1 : B.t2;
    return wrap('log', '<div class="vb-log">' + rw.slice(-((o && o.draw && (o.draw.limit || o.draw.tail)) || (H > 120 ? 16 : 10))).map((r) => { const k = String(r.kind ?? r.level ?? r.type ?? ''); return '<span style="color:' + (r.col || kc(k)) + '"><span class="t">' + esc(hhmm(r.t ?? r.ts ?? r.time ?? r.when)) + '</span> ' + (k ? '<span class="k">' + esc(k) + '</span> ' : '') + esc(txt(r)) + '</span>'; }).join('') + '</div>');
  };
  R.lane = (d, H, o) => {
    const rw = evsOf(d); if (!rw.length) return EMPTY('a lane needs events'); const by = {}, order = []; rw.forEach((r) => { const k = String(r.kind ?? r.level ?? r.type ?? r.lane ?? 'other'); if (!by[k]) { by[k] = []; order.push(k); } by[k].push(r); });
    return wrap('lane', '<div class="vb-lane">' + order.slice(0, (o && o.draw && o.draw.lanes) || 5).map((k, i) => { const last = by[k][by[k].length - 1]; return '<div style="--lc:' + (last.col || DV(i)) + '"><b>' + esc(k) + '</b><em>' + esc(txt(last)) + '</em><i>' + esc(String(last.meta ?? last.detail ?? hhmm(last.t ?? last.ts ?? last.when ?? '')) + (by[k].length > 1 ? ' · ' + by[k].length : '')) + '</i></div>'; }).join('') + '</div>');
  };

  /* ── graphs: the node graph, the flow, the mini graph (the pipes and the topology are in the motion / iso sections) ── */
  const graphOf = (d) => { const nodes = ((d && (d.nodes || d.vertices)) || []).map((n, i) => typeof n === 'object' ? Object.assign({ id: String(n.id ?? n.name ?? i) }, n) : { id: String(n), label: String(n) }); const links = ((d && (d.links || d.edges || d.rels)) || []).map((e) => ({ a: String(e.source ?? e.from ?? e.a ?? ''), b: String(e.target ?? e.to ?? e.b ?? ''), v: num(e.value ?? e.weight ?? e.rate ?? 1), label: e.label ?? e.kind })).filter((e) => e.a && e.b); return { nodes, links }; };
  // a layered layout: roots (no incoming link) on the first layer, then by depth; each layer spread across the width
  const layers = (g) => { const inn = {}; g.links.forEach((e) => { inn[e.b] = (inn[e.b] || 0) + 1; }); const depth = {}; const q = g.nodes.filter((n) => !inn[n.id]).map((n) => n.id); if (!q.length && g.nodes.length) q.push(g.nodes[0].id); q.forEach((id) => { depth[id] = 0; }); let head = 0; while (head < q.length) { const id = q[head++]; g.links.filter((e) => e.a === id).forEach((e) => { if (depth[e.b] == null) { depth[e.b] = depth[id] + 1; q.push(e.b); } }); } g.nodes.forEach((n) => { if (depth[n.id] == null) depth[n.id] = 0; }); const by = {}; g.nodes.forEach((n) => { (by[depth[n.id]] = by[depth[n.id]] || []).push(n); }); const L = Object.keys(by).length; const pos = {}; Object.keys(by).forEach((k) => { const row = by[k]; row.forEach((n, i) => { pos[n.id] = [(i + .5) / row.length * 100, L > 1 ? 12 + (+k) / (L - 1) * 76 : 50]; }); }); return pos; };
  const edgeSpan = (a, b, W, H) => { const dx = (b[0] - a[0]) * W / 100, dy = (b[1] - a[1]) * H / 100; return { len: Math.hypot(dx, dy).toFixed(1) + 'px', deg: (Math.atan2(dy, dx) * 180 / Math.PI).toFixed(1) + 'deg' }; };
  R.graph = (d, H, o) => {
    const g = graphOf(d); if (!g.nodes.length) return EMPTY('a graph needs nodes'); const pos = (o && o.draw && o.draw.layout === 'flow') ? layers(g) : layers(g); const W = 300, hi = Math.max(...g.links.map((e) => e.v), 1); const pal = palOf(o);
    return wrap('graph', '<div class="vb-topo" style="height:' + Math.max(80, H) + 'px">' + g.links.map((e) => { const a = pos[e.a], b = pos[e.b]; if (!a || !b) return ''; const s = edgeSpan(a, b, W, Math.max(80, H)); return '<span class="te" style="left:' + a[0].toFixed(1) + '%;top:' + a[1].toFixed(1) + '%;width:' + s.len + ';transform:rotate(' + s.deg + ')"></span>'; }).join('') + g.nodes.map((n, i) => { const p = pos[n.id]; const dd = 8 + Math.min(10, num(n.size ?? n.weight ?? n.score * 12 ?? 4)); return '<span class="tn" style="left:' + p[0].toFixed(1) + '%;top:' + p[1].toFixed(1) + '%;width:' + dd + 'px;height:' + dd + 'px;background:' + (n.col || n.color || (n.kind || n.family ? DV(String(n.kind || n.family).length) : (i ? pal(i) : B.ac))) + '" title="' + esc(String(n.label ?? n.name ?? n.id)) + '"></span>'; }).join('') + '</div>', '', 'width:' + W + 'px;max-width:100%');
  };
  R.flow = (d, H, o) => {
    const g = graphOf(d); if (!g.nodes.length) return EMPTY('a flow needs nodes and flows'); const hi = Math.max(...g.links.map((e) => e.v), 1); const W = 300, HH = Math.max(80, H); const pos = {}; const lay = layers(g); Object.keys(lay).forEach((k) => { pos[k] = [lay[k][1], lay[k][0]]; });   // the flow runs left → right: depth is x
    return wrap('flow', '<div class="vb-topo" style="height:' + HH + 'px">' + g.links.map((e) => { const a = pos[e.a], b = pos[e.b]; if (!a || !b) return ''; const s = edgeSpan(a, b, W, HH); return '<span class="fe" style="left:' + a[0].toFixed(1) + '%;top:' + a[1].toFixed(1) + '%;width:' + s.len + ';height:' + (2 + e.v / hi * 6).toFixed(1) + 'px;transform:rotate(' + s.deg + ')" title="' + esc(e.a + ' → ' + e.b + (e.label ? ' · ' + e.label : '')) + '"></span>'; }).join('') + g.nodes.map((n, i) => { const p = pos[n.id]; return '<span class="fn" style="left:' + p[0].toFixed(1) + '%;top:' + p[1].toFixed(1) + '%;color:' + (n.col || n.color || (i ? DV(i) : B.ac)) + '">' + esc(String(n.label ?? n.name ?? n.id)) + '</span>'; }).join('') + '</div>', '', 'width:' + W + 'px;max-width:100%');
  };
  R.minigraph = (d, H, o) => {
    const g = graphOf(d); if (!g.nodes.length) return EMPTY('a mini graph needs nodes'); const pos = layers(g); const W = 220, HH = Math.max(70, H);
    return wrap('minigraph', '<div class="vb-topo" style="height:' + HH + 'px">' + g.links.map((e) => { const a = pos[e.a], b = pos[e.b]; if (!a || !b) return ''; const s = edgeSpan(a, b, W, HH); return '<span class="te" style="left:' + a[0].toFixed(1) + '%;top:' + a[1].toFixed(1) + '%;width:' + s.len + ';transform:rotate(' + s.deg + ')"></span>'; }).join('') + g.nodes.map((n, i) => { const p = pos[n.id], hollow = n.included === false || n.hollow; const dd = 6 + Math.min(8, num(n.score != null ? n.score * 8 : (n.size ?? 3))); return '<span class="tn' + (hollow ? ' hollow' : '') + '" style="left:' + p[0].toFixed(1) + '%;top:' + p[1].toFixed(1) + '%;width:' + dd + 'px;height:' + dd + 'px;--c:' + (n.col || n.color || DV(String(n.family ?? n.kind ?? i).length + i)) + '" title="' + esc(String(n.label ?? n.name ?? n.id)) + '"></span>'; }).join('') + '</div>', '', 'width:' + W + 'px;max-width:100%');
  };

  /* ── the composites the board draws as forms of their own: one node, four measures, a compare; and the carousel ── */
  R.node = (d, H, o) => {
    const kv = keyed(d); const rw = rows(d); const src = (d && typeof d === 'object' && !Array.isArray(d)) ? d : {}; const cards = kv.length ? kv.map((x) => [x[0], (src[x[0]] && typeof src[x[0]] === 'object') ? src[x[0]] : { value: x[1] }]) : Object.keys(src).filter((k) => src[k] && typeof src[k] === 'object' && typeof src[k].value === 'number').map((k) => [k, src[k]]).concat(rw.map((r) => [nameOf(r), r]));
    if (!cards.length) return EMPTY('a node card needs measures'); const pal = palOf(o, 'load'); const st = src.status ?? src.state ?? (rw[0] && rw[0].status);
    return wrap('node', (st ? '<span class="vb-nstat"><i style="background:' + stCol(st) + '"></i>' + esc([st, src.detail ?? src.hardware ?? src.gpu, src.uptime != null ? 'up ' + src.uptime : ''].filter(Boolean).join(' · ')) + '</span>' : '') + '<div class="vb-ncard">' + cards.slice(0, 4).map((c, i) => { const m = c[1], v = num(m.value ?? m.v), hi = num(m.max) || (m.unit === '%' || /%|°/.test(String(m.unit)) ? 100 : Math.max(v, 1) * 1.6); const col = m.col || pal(i, v, hi); return '<div><b style="color:' + (hi === 100 ? col : B.t1) + '">' + esc(fmt(v)) + '<small>' + esc(String(m.unit ?? '')) + '</small></b><span>' + esc(String(m.label ?? c[0])) + '</span><span class="bar"><i style="width:' + pct(v, hi).toFixed(0) + '%;background:' + col + '"></i></span></div>'; }).join('') + '</div>');
  };
  R.glance = (d, H, o) => {
    const rw = rows(d); const src = (d && typeof d === 'object' && !Array.isArray(d)) ? d : {}; const items = rw.length ? rw.map((r) => ({ n: nameOf(r), v: r.value ?? r.v ?? r.text, s: series(r.series || r.history || r.trend || r.spark || []), col: r.col })) : Object.keys(src).filter((k) => src[k] != null && typeof src[k] !== 'object' || (src[k] && typeof src[k] === 'object' && !Array.isArray(src[k]))).map((k) => { const m = src[k]; return typeof m === 'object' ? { n: m.label ?? k, v: m.value ?? m.text ?? m.v, s: series(m.series || m.history || m.trend || []), col: m.col } : { n: k, v: m, s: [] }; });
    if (!items.length) return EMPTY('a glance needs figures'); const pal = palOf(o);
    return wrap('glance', '<div class="vb-glance">' + items.slice(0, (o && o.draw && o.draw.figures) || 4).map((it, i) => '<div><b>' + esc(typeof it.v === 'number' ? fmt(it.v) : String(it.v ?? '')) + '</b><span>' + esc(it.n) + '</span>' + (it.s.length > 1 ? spark(it.s, it.col || pal(i), 100, 16) : '') + '</div>').join('') + '</div>');
  };
  R.compare = (d, H, o) => {
    const rw = rows(d); const src = (d && typeof d === 'object' && !Array.isArray(d)) ? d : {}; let pairs = [], A = 'a', Bn = 'b';
    if (rw.length && rw.some((r) => (r.a != null && r.b != null) || (r.left != null && r.right != null))) pairs = rw.map((r) => ({ n: nameOf(r), a: num(r.a ?? r.left), b: num(r.b ?? r.right), hi: num(r.max) || Math.max(num(r.a ?? r.left), num(r.b ?? r.right)) * 1.25, u: r.unit || '' }));
    else { const ks = Object.keys(src).filter((k) => src[k] && typeof src[k] === 'object' && !Array.isArray(src[k])); if (ks.length >= 2) { A = ks[0]; Bn = ks[1]; const ms = Object.keys(src[A]).filter((k) => typeof src[A][k] === 'number'); pairs = ms.map((k) => ({ n: k, a: num(src[A][k]), b: num(src[Bn][k]), hi: Math.max(num(src[A][k]), num(src[Bn][k])) * 1.25 || 1, u: '' })); } }
    if (!pairs.length) return EMPTY('a compare needs two sides per measure');
    return wrap('compare', pairs.slice(0, 6).map((p) => '<div class="vb-cmpr"><span class="n">' + esc(p.n) + '</span><span class="side l"><b>' + esc(fmt(p.a) + p.u) + '</b><i style="width:' + pct(p.a, p.hi).toFixed(0) + '%;background:' + B.ac + '"></i></span><span class="side r"><i style="width:' + pct(p.b, p.hi).toFixed(0) + '%;background:' + B.ac5 + '"></i><b>' + esc(fmt(p.b) + p.u) + '</b></span></div>').join('') + '<div class="vb-lgr"><span><i style="background:' + B.ac + '"></i>' + esc(String(src.a_label ?? A)) + '</span><span><i style="background:' + B.ac5 + '"></i>' + esc(String(src.b_label ?? Bn)) + '</span><span class="vb-lbl" style="margin-left:auto">bars grow outward from the middle</span></div>');
  };
  R.carousel = (d, H, o) => {
    const rw = rows(d); if (!rw.length) return EMPTY('a carousel needs rows'); const pages = ['donut + the busiest', 'every row', 'the racks — one slot per row']; const pg = Math.max(0, Math.min(2, +ui(o, 'page', 0) || 0));
    const by = {}; rw.forEach((r) => { const s = String(r.status ?? r.state ?? 'other'); by[s] = (by[s] || 0) + 1; }); const parts = Object.keys(by).map((k) => [k, by[k]]); const tot = rw.length; const C = 2 * Math.PI * 26; let acc = 0;
    const list = (rs) => '<div class="vb-flist"><span class="h"><span>' + esc((o && o.draw && o.draw.what) || 'row') + '</span><span class="m">node</span><span class="m">state</span><span class="m">up</span></span>' + rs.map((r) => '<span><span><i style="background:' + stCol(r.status ?? r.state) + '"></i>' + esc(nameOf(r)) + '</span><span class="m">' + esc(String(r.node ?? r.host ?? '')) + '</span><span class="m">' + esc(String(r.status ?? r.state ?? '')) + '</span><span class="m">' + esc(String(r.up ?? r.uptime ?? r.age ?? '')) + '</span></span>').join('') + '</div>';
    let body;
    if (pg === 0) body = '<div class="vb-carp"><span class="vb-ring"><svg viewBox="0 0 64 64">' + '<circle cx="32" cy="32" r="26" fill="none" stroke="' + B.s3 + '" stroke-width="9"/>' + parts.map((p) => { const fr = p[1] / tot; const el = '<circle cx="32" cy="32" r="26" fill="none" stroke="' + stCol(p[0]) + '" stroke-width="9" stroke-dasharray="' + Math.max(0, C * fr - 2).toFixed(1) + ' ' + (C - C * fr + 2).toFixed(1) + '" stroke-dashoffset="' + (-C * acc).toFixed(1) + '"/>'; acc += fr; return el; }).join('') + '</svg><span>' + tot + '</span></span><div class="vb-lg" style="width:96px">' + parts.map((p) => '<span><i style="background:' + stCol(p[0]) + '"></i>' + esc(p[0]) + '<b>' + p[1] + '</b></span>').join('') + '</div>' + list(rw.slice(0, 4)) + '</div>';
    else if (pg === 1) body = '<div class="vb-carp">' + list(rw.slice(0, 8)) + '</div>';
    else body = R.racks ? R.racks(rw, H, o) : '<div class="vb-carp">' + list(rw.slice(0, 8)) + '</div>';
    return wrap('carousel', body + '<div class="vb-carn"><button' + set('page', (pg + 2) % 3) + '>‹</button>' + [0, 1, 2].map((j) => '<i class="' + (j === pg ? 'on' : '') + '"' + set('page', j) + '></i>').join('') + '<button' + set('page', (pg + 1) % 3) + '>›</button><span class="vb-lbl">' + pages[pg] + '</span></div>');
  };
  // the rail: any records at menu width (the Motion board's "any widget at menu width") — a composite of chips
  R.rail = (d, H, o) => { const rec = (o && o.record) || {}; if (Array.isArray(rec.children) && rec.children.length) return R.composite(d, H, Object.assign({}, o, { record: Object.assign({}, rec, { layout: 'rail' }) })); return R.list(d, H, o); };
  /* ── the candlestick and the activity rings ── */
  R.candles = (d, H, o) => {
    const bars = rows(d).filter((r) => r.open != null || r.o != null); if (bars.length < 2) return EMPTY('candles need OHLC bars');
    const B4 = bars.slice(-((o && o.draw && o.draw.bars) || 16)); const O = (b) => num(b.open ?? b.o), Cc = (b) => num(b.close ?? b.c), Hh = (b) => num(b.high ?? b.h ?? Math.max(O(b), Cc(b))), L = (b) => num(b.low ?? b.l ?? Math.min(O(b), Cc(b)));
    const hi = Math.max(...B4.map(Hh)), lo = Math.min(...B4.map(L)), sp = (hi - lo) || 1; let up = 0; const P = (v) => (100 - (v - lo) / sp * 100);
    const html = B4.map((b, i) => { const isUp = Cc(b) >= O(b); if (isUp) up++; const col = isUp ? B.ac2 : B.ac4; return '<span style="left:' + ((i + .5) / B4.length * 100).toFixed(1) + '%;width:' + (80 / B4.length).toFixed(1) + '%"><i class="wk" style="top:' + P(Hh(b)).toFixed(1) + '%;height:' + ((Hh(b) - L(b)) / sp * 100).toFixed(1) + '%;background:' + col + '"></i><i class="bd" style="top:' + P(Math.max(O(b), Cc(b))).toFixed(1) + '%;height:' + Math.max(1.5, Math.abs(Cc(b) - O(b)) / sp * 100).toFixed(1) + '%;background:' + col + '"></i></span>'; }).join('');
    const chg = O(B4[0]) ? (Cc(B4[B4.length - 1]) - O(B4[0])) / O(B4[0]) * 100 : 0;
    return wrap('candles', '<div class="vb-cnd" style="height:' + chH(H, 26) + 'px">' + html + '</div><div class="vb-lgr"><span><i style="background:' + B.ac2 + '"></i>up ' + up + '</span><span><i style="background:' + B.ac4 + '"></i>down ' + (B4.length - up) + '</span><span class="vb-lbl" style="margin-left:auto">' + (chg >= 0 ? '+' : '') + chg.toFixed(1) + '% · ' + B4.length + ' bars</span></div>');
  };
  R.rings = (d, H, o) => {
    const kv = keyed(d).slice(0, 3); if (!kv.length) return EMPTY('activity rings need up to three values'); const rw = rows(d); const cols = [B.ac2, B.ac, B.ac5], R0 = [34, 24, 14];
    const ringD = (r, f) => { const C = 2 * Math.PI * r; return (C * f).toFixed(1) + ' ' + C.toFixed(1); };
    return wrap('rings', '<div class="vb-row"><div class="vb-rings"><svg viewBox="0 0 80 80">' + kv.map((x, i) => { const m = rw.find((q) => nameOf(q) === x[0]) || {}; const f = Math.max(0, Math.min(1, x[1] / (num(m.max) || 100))); return '<circle cx="40" cy="40" r="' + R0[i] + '" fill="none" stroke="' + B.s3 + '" stroke-width="7"/><circle cx="40" cy="40" r="' + R0[i] + '" fill="none" stroke="' + cols[i] + '" stroke-width="7" stroke-dasharray="' + ringD(R0[i], f) + '" stroke-linecap="round"/>'; }).join('') + '</svg></div><div class="vb-lg">' + kv.map((x, i) => '<span><i style="background:' + cols[i] + '"></i>' + esc(x[0]) + '<b>' + esc(fmt(x[1])) + '%</b></span>').join('') + '</div></div>');
  };
  R.scatter = (d, H, o) => {
    const pts = rows(d).map((p) => ({ x: num(p.x ?? p.t ?? p[0]), y: num(p.y ?? p.v ?? p.value ?? p[1]), s: num(p.size ?? p.r ?? p.calls ?? 3), k: String(p.kind ?? p.class ?? p.label ?? ''), col: p.col || p.color })); if (pts.length < 2) return EMPTY('a scatter needs points');
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y), ss = pts.map((p) => p.s); const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys), s1 = Math.max(...ss) || 1; const kinds = []; const pal = palOf(o);
    return wrap('scatter', '<div class="vb-chart rel" style="height:' + chH(H, 22) + 'px">' + pts.slice(0, 400).map((p) => { let ki = kinds.indexOf(p.k); if (ki < 0) { kinds.push(p.k); ki = kinds.length - 1; } const dd = 5 + p.s / s1 * 12; return '<span style="position:absolute;transform:translate(-50%,-50%);border-radius:50%;left:' + (6 + (p.x - x0) / ((x1 - x0) || 1) * 88).toFixed(1) + '%;top:' + (94 - (p.y - y0) / ((y1 - y0) || 1) * 84).toFixed(1) + '%;width:' + dd.toFixed(1) + 'px;height:' + dd.toFixed(1) + 'px;background:' + (p.col || pal(ki)) + ';opacity:.8" title="' + esc(p.k) + ' ' + fmt(p.x) + ' · ' + fmt(p.y) + '"></span>'; }).join('') + '</div>' + cap(esc(String((d && d.note) || ('x ' + ((o && o.draw && o.draw.x) || 'x') + ' · y ' + ((o && o.draw && o.draw.y) || 'y') + ' · area ' + ((o && o.draw && o.draw.size) || 'size'))))));
  };

  /* ── draw at a size: the composition around the form ─────────────────── */
  const GLYPH = { context_graph: '◎', trace: '∿', radial: '◔', counter: '123', bar: '▬', bars: '▥', thermo: '≣', heat: '▦', matrix: '▦', donut: '◑', stack: '▤', pills: '◦', log: '≡', lane: '≡', table: '▦', files: '⊞', list: '≡', checklist: '☑', stepper: '⋮', calendar: '▦', string: '¶', kv: '≔', pipes: '⌥', scatter: '⁘', panel: '▭', composite: '⊞' };
  // the glyph a size below M carries (the Sizes board): a ring for a level or a share, a spark for a series, a tube for
  // named values, a dot for events and graphs, the count glyph for the rest — drawn from the data, never a character
  function glyphOf(form, data) {
    const f = canon(form), sh = DRAWN[f] || '', d = dataFor(data, f); const ringG = (fr, col) => '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="6" fill="none" stroke="var(--b-s3,#222630)" stroke-width="3"/><circle cx="8" cy="8" r="6" fill="none" stroke="' + col + '" stroke-width="3" stroke-dasharray="' + (2 * Math.PI * 6 * Math.max(0, Math.min(1, fr))).toFixed(1) + ' 37.7" transform="rotate(-90 8 8)"/></svg>';
    try {
      if (sh === 'level' || sh === 'rate') { const l = level(d); if (l) return ringG((l.v - l.lo) / ((l.hi - l.lo) || 1), 'var(--b-ac,#6ea8d8)'); }
      if (sh === 'parts') { const kv = keyed(d); if (kv.length) { const t = kv.reduce((s, x) => s + Math.abs(x[1]), 0) || 1; return ringG(Math.abs(kv[0][1]) / t, 'var(--b-ac,#6ea8d8)'); } }
      if (sh === 'series' || sh === 'ohlcv') { const v = sh === 'ohlcv' ? rows(d).map((r) => num(r.close ?? r.c)) : series(d); if (v.length > 1) return '<svg class="sp" viewBox="0 0 34 12" preserveAspectRatio="none"><polyline points="' + v.slice(-16).map((x, i, a) => (i * 34 / Math.max(1, a.length - 1)).toFixed(1) + ',' + (11 - (x - Math.min(...a)) / ((Math.max(...a) - Math.min(...a)) || 1) * 10).toFixed(1)).join(' ') + '" fill="none" stroke="var(--b-ac2,#5ec9a0)" stroke-width="1.5"/></svg>'; }
      if (sh === 'values') { const kv = keyed(d); if (kv.length) { const hi = Math.max(...kv.map((x) => Math.abs(x[1]))) || 1; return '<span class="th"><i style="height:' + Math.round(Math.abs(kv[0][1]) / hi * 100) + '%"></i></span>'; } }
      if (sh === 'events' || sh === 'graph' || sh === 'points') { const r = rows(d)[0]; return '<span class="dot" style="background:' + ((r && (r.col || r.color)) || (r && /err|fail|down/i.test(String(r.kind ?? r.level ?? r.status ?? '')) ? 'var(--b-ac4,#e06060)' : 'var(--b-ac3,#e09a55)')) + '"></span>'; }
    } catch (_) {}
    return GLYPH[f] || '▢';
  }
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
    if (size === 'xs') return '<span class="vw-xs" title="' + esc(opts.title || f0) + '"><i class="vw-g">' + glyphOf(f, data) + '</i>' + (figure(f, data) || '—') + '</span>';
    if (size === 's') return '<span class="vw-chip" title="' + esc(opts.title || f0) + '"><i class="vw-g">' + glyphOf(f, data) + '</i><b>' + (figure(f, data) || '—') + '</b>' + (opts.title ? '<small>' + esc(opts.title) + '</small>' : '') + '</span>';
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
.vw-g{display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;vertical-align:-2px}.vw-g svg{width:14px;height:14px;display:block}.vw-g .sp{width:34px;height:12px}.vw-chip .vw-g{width:16px;height:16px}.vw-chip .vw-g svg{width:16px;height:16px}.vw-chip .vw-g .sp{width:38px;height:14px}
.vw-g .th{width:7px;height:13px;border-radius:4px;background:var(--b-s3,var(--bg2,#222630));position:relative;overflow:hidden;display:block}.vw-g .th i{position:absolute;left:0;right:0;bottom:0;background:var(--b-ac4,#e06060)}.vw-g .dot{width:8px;height:8px;border-radius:50%;display:block}
.vw-xs .vw-g:has(.sp),.vw-chip .vw-g:has(.sp){width:auto}
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
@media (max-width:520px){.vw-l,.vw-xl{grid-template-columns:1fr}.vw-detail{border-left:none;padding-left:0}}
/* ── the boards' forms (vb-): the token bridge, then the Widgets board's CSS under its own prefix ── */
.vw-b{--b-ac:var(--ac,var(--acc,#6ea8d8));--b-ac2:var(--ac2,var(--acc2,#5ec9a0));--b-ac3:var(--ac3,var(--acc3,#e09a55));--b-ac4:var(--ac4,var(--err,#e06060));--b-ac5:var(--ac5,#a78bfa);--b-s2:var(--s2,var(--bg2,#1a1d24));--b-s3:var(--s3,var(--bg3,#222630));--b-surf:var(--surf,var(--bg1,#15181e));--b-surf2:var(--surf2,var(--bg2,#1b1f27));--b-surf3:var(--surf3,var(--bg3,#242934));--b-t1:var(--t1,var(--text,#d4dae4));--b-t2:var(--t2,var(--dim2,#8a92a0));--b-t3:var(--t3,var(--dim,#6b7280));--b-bd:var(--bd,var(--border,rgba(255,255,255,.07)));--b-bd2:var(--bd2,var(--border2,rgba(255,255,255,.14)));--b-on:var(--on-ac,#0b1119);--b-dv1:var(--dv1,#866ec5);--b-dv2:var(--dv2,#54a863);--b-dv3:var(--dv3,#3585c9);--b-dv4:var(--dv4,#bb881a);--b-dv5:var(--dv5,#b95c88);--b-dv6:var(--dv6,#00aba4);--b-dv7:var(--dv7,#bd6533);--b-mono:var(--f-mono,var(--mono,ui-monospace,Menlo,monospace));--b-ui:var(--f-ui,var(--sans,system-ui,sans-serif));--b-r:var(--r-sm,6px);--b-pill:var(--r-pill,999px);
  width:100%;min-width:0;min-height:0;font-family:var(--b-ui);color:var(--b-t1);font-size:10px;display:flex;flex-direction:column;gap:6px;font-variant-numeric:tabular-nums;box-sizing:border-box}
.vw-b *,.vw-b *::before,.vw-b *::after{box-sizing:border-box}
.vw-b button{font:inherit;color:inherit;background:none;border:none;cursor:pointer;padding:0}
.vw-b.vb-center{align-items:center;justify-content:center}
.vb-lbl{font-size:9.5px;color:var(--b-t2);line-height:1.35}.vb-lbl.up,.vb-lbl .up,.vw-b .up{color:var(--b-ac2)}.vb-lbl.dn,.vb-lbl .dn,.vw-b .dn{color:var(--b-ac4)}
.vb-row{display:flex;align-items:center;gap:8px;width:100%}
.vb-chart{flex:none;min-height:56px;position:relative;width:100%}.vb-chart svg{display:block;width:100%;height:100%;min-height:56px;overflow:visible}.vb-chart.rel{min-height:64px}
.vb-spk{width:100%;height:18px;display:block;overflow:visible}
.vb-hero{display:flex;align-items:baseline;gap:7px}.vb-hero b{font-family:var(--b-mono);font-size:30px;font-weight:700;letter-spacing:-.035em;line-height:1}.vb-hero .u{font-family:var(--b-mono);font-size:13px;color:var(--b-t2)}
.vb-lg{display:flex;flex-direction:column;gap:4px;min-width:0}.vb-lg span{display:flex;align-items:center;gap:7px;font-size:10px;color:var(--b-t2);white-space:nowrap;overflow:hidden}.vb-lg i{width:8px;height:8px;border-radius:2px;flex-shrink:0}.vb-lg b{margin-left:auto;font-family:var(--b-mono);font-size:9.5px;color:var(--b-t1);font-weight:400}.vb-lg.row{flex-direction:row;flex-wrap:wrap;gap:4px 12px}
.vb-lgr{display:flex;align-items:center;gap:12px;font-size:9.5px;color:var(--b-t2)}.vb-lgr span{display:inline-flex;align-items:center;gap:6px}.vb-lgr i{width:8px;height:8px;border-radius:2px}
.vb-rw{display:flex;align-items:center;gap:8px;font-size:10px}.vb-rw .n{width:64px;flex-shrink:0;color:var(--b-t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-rw .tr{flex:1;height:7px;border-radius:4px;background:var(--b-s3);overflow:hidden;position:relative}.vb-rw .tr i{display:block;height:100%;border-radius:4px}.vb-rw .tr em{position:absolute;top:-2px;width:2px;height:11px;background:var(--b-t1);transform:translateX(-50%)}.vb-rw .v{width:38px;text-align:right;font-family:var(--b-mono);font-size:9.5px;color:var(--b-t1);flex-shrink:0}
/* the standard set */
.vb-fc{flex:1;min-height:0;border-radius:var(--b-r);background:var(--b-surf2);padding:9px 11px;display:flex;flex-direction:column;gap:4px;box-shadow:var(--elev-lo,0 1px 2px rgba(0,0,0,.14));animation:vb-fcin .35s ease}@keyframes vb-fcin{from{opacity:0;transform:translateX(10px)}}
.vb-fc .k{font-size:8.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--b-t3);display:flex;gap:6px;align-items:center}.vb-fc .k i{width:6px;height:6px;border-radius:50%}.vb-fc .h{font-size:12px;font-weight:600;color:var(--b-t1);line-height:1.35}.vb-fc .b{font-size:10px;color:var(--b-t2);line-height:1.45;overflow:hidden}.vb-fc .m{font-family:var(--b-mono);font-size:8.5px;color:var(--b-t3);display:flex;gap:8px}
.vb-fn,.vb-carn{display:flex;align-items:center;gap:5px}.vb-fn button,.vb-carn button{width:22px;height:18px;border-radius:var(--b-r);background:var(--b-surf2);color:var(--b-t2);font-size:11px}.vb-fn i,.vb-carn i{width:6px;height:6px;border-radius:50%;background:var(--b-s3);cursor:pointer}.vb-fn i.on,.vb-carn i.on{background:var(--b-ac)}.vb-fn span{margin-left:auto;font-family:var(--b-mono);font-size:8.5px;color:var(--b-t3)}.vb-carn .vb-lbl{margin-left:8px}
.vb-fr{display:grid;grid-template-columns:18px 1fr 44px 52px;gap:7px;align-items:center;height:22px;font-size:10px;border-bottom:1px solid var(--b-bd)}.vb-fr.h{color:var(--b-t3);font-size:8.5px;text-transform:uppercase;letter-spacing:.08em;height:18px}.vb-fr .ic{width:14px;height:16px;border-radius:2px;font-family:var(--b-mono);font-size:6.5px;font-weight:700;color:#0e0f12;display:flex;align-items:flex-end;justify-content:center;padding-bottom:1px}.vb-fr .n{color:var(--b-t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-fr .n small{color:var(--b-t3);margin-left:5px;font-size:9px}.vb-fr .m{font-family:var(--b-mono);font-size:9px;color:var(--b-t3);text-align:right}
.vb-dgr{display:grid;gap:6px;align-items:center;height:21px;font-size:9.5px;border-bottom:1px solid var(--b-bd)}.vb-dgr.h{height:20px}.vb-dgr.h button{font-size:8.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--b-t3);text-align:left;display:flex;gap:3px;align-items:center;white-space:nowrap}.vb-dgr.h button.on{color:var(--b-t1)}.vb-dgr:nth-child(odd):not(.h){background:color-mix(in srgb,var(--b-surf2) 60%,transparent)}.vb-dgr .c{color:var(--b-t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding-left:4px}.vb-dgr .c.num{font-family:var(--b-mono);color:var(--b-t2);text-align:right;padding-left:0}.vb-dgr .c.dn{color:var(--b-ac4)}
.vb-dgf{display:flex;align-items:center;gap:6px;font-size:8.5px;color:var(--b-t3);padding-top:4px;margin-top:auto}.vb-dgf button{width:18px;height:16px;border-radius:4px;background:var(--b-surf2);color:var(--b-t2)}
.vb-gal{flex:none;min-height:110px;display:grid;grid-template-columns:repeat(4,1fr);grid-auto-rows:1fr;gap:5px}.vb-gal .g{border-radius:var(--b-r);position:relative;overflow:hidden;min-height:44px;cursor:pointer;transition:transform .15s ease}.vb-gal .g:hover{transform:scale(1.04)}.vb-gal .g span{position:absolute;left:0;right:0;bottom:0;padding:3px 6px;font-size:8px;color:#fff;background:linear-gradient(transparent,rgba(0,0,0,.6));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-gal .g b{position:absolute;right:4px;top:3px;font-family:var(--b-mono);font-size:7.5px;color:#fff;opacity:.8}
.vb-trm{flex:1;min-height:0;border-radius:var(--b-r);background:var(--b-s2);padding:7px 9px;font-family:var(--b-mono);font-size:9.5px;line-height:1.6;color:var(--b-t2);white-space:pre-wrap;overflow:hidden;box-shadow:inset 0 0 0 1px var(--b-bd)}.vb-trm b{color:var(--b-ac2);font-weight:500}.vb-trm em{color:var(--b-ac3);font-style:normal}.vb-trm .car{display:inline-block;width:6px;height:11px;background:var(--b-t1);vertical-align:-2px;animation:vb-tcur 1s steps(1) infinite}@keyframes vb-tcur{0%,50%{opacity:1}51%,100%{opacity:0}}
.vb-trmh{display:flex;gap:6px;align-items:center;font-size:8.5px;color:var(--b-t3);font-family:var(--b-mono)}.vb-trmh i{width:7px;height:7px;border-radius:50%;background:var(--b-ac2)}
.vb-ag{display:grid;grid-template-columns:38px 3px 1fr auto;gap:8px;align-items:center;padding:4px 0;border-bottom:1px solid var(--b-bd);font-size:10px}.vb-ag .t{font-family:var(--b-mono);font-size:9px;color:var(--b-t3)}.vb-ag i{width:3px;height:22px;border-radius:2px}.vb-ag .n{color:var(--b-t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-ag .n small{display:block;color:var(--b-t3);font-size:8.5px}.vb-ag .w{font-family:var(--b-mono);font-size:8.5px;color:var(--b-t3)}.vb-ag.now{background:color-mix(in srgb,var(--b-ac) 10%,transparent);border-radius:var(--b-r);padding:4px 6px;margin:0 -6px}.vb-ag.now .t{color:var(--b-ac)}
.vb-pr{display:grid;grid-template-columns:24px 1fr auto;gap:8px;align-items:center;height:28px;font-size:10px}.vb-pr .av{width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:8.5px;font-weight:700;color:#0e0f12;position:relative}.vb-pr .av i{position:absolute;right:-1px;bottom:-1px;width:7px;height:7px;border-radius:50%;box-shadow:0 0 0 2px var(--b-surf)}.vb-pr .n{color:var(--b-t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-pr .n small{display:block;color:var(--b-t3);font-size:8.5px}.vb-pr .s{font-family:var(--b-mono);font-size:8.5px;color:var(--b-t3);white-space:nowrap}
.vb-ql{flex:1;min-height:0;display:grid;grid-template-columns:repeat(3,1fr);grid-auto-rows:1fr;gap:6px}.vb-ql button,.vb-ql a{border-radius:var(--b-r);background:var(--b-surf2);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;font-size:9px;color:var(--b-t2);box-shadow:var(--elev-lo,0 1px 2px rgba(0,0,0,.14));min-height:36px;text-decoration:none;padding:4px}.vb-ql button:hover,.vb-ql a:hover{color:var(--b-t1);background:color-mix(in srgb,var(--b-ac) 12%,var(--b-surf2))}.vb-ql b{font-family:var(--b-mono);font-size:13px;color:var(--b-t1)}
.vb-ann{flex:1;min-height:0;border-radius:var(--b-r);padding:10px 12px;display:flex;flex-direction:column;gap:5px;background:color-mix(in srgb,var(--pc) 10%,var(--b-surf2));box-shadow:inset 3px 0 0 0 var(--pc)}.vb-ann .k{font-size:8.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--pc);font-weight:600}.vb-ann .h{font-size:13px;font-weight:600;color:var(--b-t1);line-height:1.3}.vb-ann .b{font-size:10px;color:var(--b-t2);line-height:1.45;overflow:hidden}.vb-ann .a{display:flex;gap:6px;align-items:center;flex-wrap:wrap}.vb-ann .a button{height:21px;padding:0 9px;border-radius:var(--b-pill);background:var(--b-surf);font-size:9.5px;color:var(--b-t1);box-shadow:var(--elev-lo,0 1px 2px rgba(0,0,0,.14))}.vb-ann .a button.pri{background:var(--pc);color:#1a1408;font-weight:600}.vb-ann .a small{margin-left:auto;color:var(--b-t3);font-size:8.5px}
.vb-kb{flex:1;min-height:0;display:grid;grid-template-columns:repeat(3,1fr);gap:6px}.vb-kb .kc{background:var(--b-surf2);border-radius:var(--b-r);padding:6px;display:flex;flex-direction:column;gap:4px;min-height:0;min-width:0;overflow:hidden}.vb-kb .kh{font-size:8.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--b-t3);display:flex;gap:5px}.vb-kb .kh b{margin-left:auto;font-family:var(--b-mono);font-weight:400}.vb-kb .kt{background:var(--b-surf);border-radius:4px;padding:4px 6px;font-size:9px;color:var(--b-t1);line-height:1.3;box-shadow:inset 2px 0 0 0 var(--kc);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-kb .kt small{display:block;color:var(--b-t3);font-size:8px}
/* levels */
.vb-gg{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px}.vb-gg b{font-family:var(--b-mono);font-size:12px}.vb-gg .vb-lbl{font-size:9px}
.vb-ring{position:relative;flex-shrink:0;display:block}.vb-ring svg{width:100%;height:100%;transform:rotate(-90deg);display:block}.vb-ring > span{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-family:var(--b-mono);font-size:15px;font-weight:700}
.vb-bar2{display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--b-s3);width:100%}.vb-bar2 i{display:block;height:100%}
.vb-colbars{display:flex;align-items:flex-end;gap:2px;height:100%;min-height:56px;width:100%}.vb-colbars.gap{gap:3px}.vb-colbars i{flex:1;border-radius:2px 2px 0 0;display:block;min-height:1px}.vb-cols{flex:none;min-height:24px;display:flex}
.vb-seg7{display:flex;gap:3px;justify-content:center;align-items:baseline;padding:8px 0 4px;flex-wrap:wrap}.vb-seg7 span{position:relative;font-family:var(--b-mono);font-size:34px;font-weight:700;line-height:1;letter-spacing:-.02em}.vb-seg7 span.p{font-size:22px;color:var(--b-t3)}.vb-seg7 span.u{font-size:13px}.vb-seg7 span::before{content:attr(data-g);position:absolute;left:0;top:0;opacity:0;pointer-events:none}
.vb-tick{display:flex;align-items:flex-end;gap:2px;height:30px;flex-shrink:0}.vb-tick i{flex:1;border-radius:1px 1px 0 0;min-height:1px}
.vb-batt{display:flex;align-items:center;gap:3px;height:38px;flex-shrink:0}.vb-batt .cells{flex:1;height:100%;display:flex;gap:3px;padding:4px;border-radius:var(--b-r);box-shadow:inset 0 0 0 2px var(--b-bd2)}.vb-batt .cells i{flex:1;border-radius:2px;background:var(--b-s3)}.vb-batt .cells i.on{background:var(--b-ac2)}.vb-batt .cells i.warn{background:var(--b-ac3)}.vb-batt b{width:5px;height:14px;background:var(--b-bd2);border-radius:0 2px 2px 0}
/* series */
.vb-sm{display:grid;grid-template-columns:44px 1fr 40px;align-items:center;gap:7px}.vb-sm .v{font-family:var(--b-mono);font-size:9px;text-align:right}
.vb-sth,.vb-str{display:grid;gap:8px;align-items:center;height:22px;padding:0 4px}.vb-sth{grid-template-columns:50px 1fr 40px 40px}.vb-sth span{font-size:8px;color:var(--b-t3);text-transform:uppercase;letter-spacing:.08em}.vb-str .n{font-size:9.5px;color:var(--b-t2)}.vb-str .v{font-family:var(--b-mono);font-size:9px;text-align:right;color:var(--b-t1)}
.vb-hz{display:flex;align-items:center;gap:7px}.vb-hz .n{width:50px;font-size:9.5px;color:var(--b-t2);flex-shrink:0;white-space:nowrap;overflow:hidden}.vb-hz .bars{flex:1;display:flex;align-items:flex-end;gap:1px;height:18px}.vb-hz .bars i{flex:1;min-height:1px}
.vb-bumpl{display:flex;justify-content:space-between;font-size:8px;font-family:var(--b-mono);flex-wrap:wrap;gap:2px 6px}
.vb-cnd{flex:none;min-height:70px;position:relative;box-shadow:inset 0 -1px 0 var(--b-bd)}.vb-cnd span{position:absolute;top:0;bottom:0;transform:translateX(-50%)}.vb-cnd .wk{position:absolute;left:50%;width:1px;transform:translateX(-50%)}.vb-cnd .bd{position:absolute;left:0;right:0;border-radius:1px}
/* values */
.vb-ll{display:flex;align-items:center;gap:8px;font-size:10px}.vb-ll .n{width:58px;flex-shrink:0;color:var(--b-t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-ll .st{flex:1;position:relative;height:11px}.vb-ll .st b{position:absolute;top:5px;left:0;height:1.5px;background:var(--b-bd2);border-radius:1px}.vb-ll .st i{position:absolute;top:1.5px;width:8px;height:8px;border-radius:50%;transform:translateX(-4px)}.vb-ll .v{width:36px;text-align:right;font-family:var(--b-mono);font-size:9px;color:var(--b-t1)}
.vb-par{flex:none;min-height:64px;position:relative}.vb-par .bars{position:absolute;inset:0;display:flex;align-items:flex-end;gap:3px}.vb-par .bars i{flex:1;border-radius:2px 2px 0 0}.vb-par svg{position:absolute;inset:0;width:100%;height:100%}
.vb-bx{display:flex;align-items:center;gap:8px;font-size:10px}.vb-bx .n{width:52px;flex-shrink:0;color:var(--b-t2)}.vb-bx .r{flex:1;position:relative;height:14px}.vb-bx .wk{position:absolute;top:6.5px;height:1.5px;background:var(--b-bd2)}.vb-bx .q{position:absolute;top:2px;height:11px;border-radius:2px;opacity:.55}.vb-bx .md{position:absolute;top:0;width:2px;height:15px;background:var(--b-t1)}
.vb-dv{display:flex;align-items:center;gap:7px;font-size:9.5px}.vb-dv .n{width:64px;flex-shrink:0;color:var(--b-t2);text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-dv .ax{flex:1;position:relative;height:12px}.vb-dv .ax b{position:absolute;left:50%;top:0;width:1px;height:12px;background:var(--b-bd2)}.vb-dv .ax i{position:absolute;top:2px;height:8px;border-radius:2px}
.vb-sc{display:flex;align-items:center;gap:8px;font-size:10px}.vb-sc .n{width:56px;color:var(--b-t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex-shrink:0}.vb-sc .g{flex:1;height:6px;border-radius:3px;position:relative;background:linear-gradient(90deg,var(--b-s3) 55%,color-mix(in srgb,var(--b-ac3) 45%,var(--b-s3)) 78%,color-mix(in srgb,var(--b-ac4) 55%,var(--b-s3)) 100%)}.vb-sc .g b{position:absolute;top:-3px;width:3px;height:12px;border-radius:2px;background:var(--b-t1);transform:translateX(-50%)}.vb-sc .g em{position:absolute;top:-4px;width:2px;height:14px;background:var(--b-ac4);opacity:.75;transform:translateX(-50%)}.vb-sc .v{width:34px;text-align:right;font-family:var(--b-mono);font-size:9.5px}
.vb-sld{display:flex;align-items:center;gap:8px;font-size:9.5px;color:var(--b-t2)}.vb-sld input{flex:1;accent-color:var(--b-ac);height:14px;min-width:0}.vb-sld b{font-family:var(--b-mono);color:var(--b-t1);width:34px;text-align:right}
.vb-bigs{display:grid;grid-template-columns:1fr 1fr;gap:8px;flex:1;min-height:0;align-content:center}.vb-bigs div{display:flex;flex-direction:column;gap:1px;min-width:0}.vb-bigs b{font-family:var(--b-mono);font-size:19px;font-weight:700;letter-spacing:-.03em;line-height:1}.vb-bigs span{font-size:9px;color:var(--b-t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vb-pillw{display:flex;flex-wrap:wrap;gap:5px}.vb-pillw span{height:22px;padding:0 9px;display:inline-flex;align-items:center;gap:6px;border-radius:var(--b-pill);background:var(--b-surf2);font-size:9.5px;color:var(--b-t2)}.vb-pillw i{width:6px;height:6px;border-radius:50%}.vb-pillw b{font-family:var(--b-mono);font-weight:400;color:var(--b-t1)}
/* parts */
.vb-funnel{display:flex;flex-direction:column;gap:4px;flex:1;min-height:0;justify-content:center}.vb-funnel span{height:24px;border-radius:3px;display:flex;align-items:center;padding:0 9px;font-size:9.5px;color:var(--b-on);align-self:center;justify-content:space-between;gap:8px}.vb-funnel b{font-family:var(--b-mono);font-weight:400}
.vb-waffle{display:grid;gap:3px;flex:1;min-height:0;align-content:center}.vb-waffle i{aspect-ratio:1;border-radius:2px}
.vb-stackbar{display:flex;height:26px;border-radius:5px;overflow:hidden;gap:1px;background:var(--b-s3);width:100%}.vb-stackbar i{display:block;height:100%}
.vb-tmap{flex:none;min-height:100px;display:flex;flex-wrap:wrap;gap:2px;align-content:stretch}.vb-tmap span{border-radius:3px;display:flex;align-items:flex-end;padding:4px 5px;font-family:var(--b-mono);font-size:8px;color:var(--b-on);overflow:hidden;white-space:nowrap}
/* matrices and calendars */
.vb-heatrow{display:grid;grid-template-columns:46px 1fr 32px;align-items:center;gap:8px}.vb-heatrow .v{font-family:var(--b-mono);font-size:9px;text-align:right}.vb-heat{display:grid;gap:2px;align-content:center}.vb-heat i{aspect-ratio:1;border-radius:2px;display:block}
.vb-heatstrip{display:grid;gap:2px;flex:1;min-height:0;align-content:center}.vb-heatstrip i{aspect-ratio:1;border-radius:2px;max-width:44px;justify-self:center;width:100%}
.vb-mxh,.vb-mxr{display:grid;gap:4px;align-items:center}.vb-mxh span{font-family:var(--b-mono);font-size:8px;color:var(--b-t3);text-align:center;white-space:nowrap;overflow:hidden}.vb-mxr .n{font-size:9.5px;color:var(--b-t2);white-space:nowrap;overflow:hidden}.vb-mxr i{height:17px;border-radius:3px;display:block}
.vb-dots{display:grid;grid-template-columns:repeat(20,1fr);gap:3px;flex:1;min-height:0;align-content:center}.vb-dots i{aspect-ratio:1;border-radius:50%;display:block}
.vb-cal{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:2px;flex:none;min-height:70px}.vb-cal i{border-radius:2px;display:block}
.vb-tabs{display:flex;gap:2px;background:var(--b-surf2);border-radius:var(--b-r);padding:2px;align-self:flex-start;box-shadow:var(--elev-lo,0 1px 2px rgba(0,0,0,.14))}.vb-tabs button{height:20px;padding:0 9px;font-size:9.5px;color:var(--b-t2);border-radius:calc(var(--b-r) - 2px)}.vb-tabs button.on{background:var(--pri-bg,var(--b-ac));color:var(--pri-fg,var(--b-on));font-weight:600}
.vb-nhr{display:grid;grid-template-columns:52px 1fr 44px 62px;gap:8px;align-items:center;font-size:9.5px}.vb-nhr .n{color:var(--b-t2);white-space:nowrap;overflow:hidden}.vb-nhr .tr{height:8px;border-radius:4px;background:var(--b-s3);overflow:hidden}.vb-nhr .tr i{display:block;height:100%;border-radius:4px}.vb-nhr .v{font-family:var(--b-mono);text-align:right;color:var(--b-t1)}.vb-nhr .pips{display:flex;gap:3px;align-items:flex-end;height:12px}.vb-nhr .pips i{width:10px;border-radius:1px;display:block}
/* stages and time */
.vb-stepr{display:flex;align-items:center;gap:0;flex:1;min-height:0;padding-top:6px}.vb-stepr span{flex:1;display:flex;flex-direction:column;align-items:center;gap:5px;position:relative;min-width:0}.vb-stepr span::before{content:'';position:absolute;top:8px;left:-50%;width:100%;height:2px;background:var(--b-s3)}.vb-stepr span:first-child::before{display:none}.vb-stepr span.done::before{background:var(--b-ac2)}.vb-stepr i{width:17px;height:17px;border-radius:50%;background:var(--b-s3);z-index:1;display:flex;align-items:center;justify-content:center;font-size:8px;color:var(--b-t3);font-style:normal}.vb-stepr span.done i{background:var(--b-ac2);color:var(--b-on)}.vb-stepr span.now i{background:var(--b-ac);color:var(--b-on);box-shadow:0 0 0 4px color-mix(in srgb,var(--b-ac) 22%,transparent)}.vb-stepr span.bad i{background:var(--b-ac4);color:var(--b-on)}.vb-stepr em{font-style:normal;font-size:8.5px;color:var(--b-t3);text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}.vb-stepr span.done em,.vb-stepr span.now em{color:var(--b-t1)}
.vb-gr{display:flex;align-items:center;gap:7px;font-size:9.5px;height:16px}.vb-gr .n{width:52px;flex-shrink:0;color:var(--b-t2);white-space:nowrap;overflow:hidden}.vb-gr .track{flex:1;position:relative;height:10px;background:var(--b-s3);border-radius:3px}.vb-gr .track i{position:absolute;top:0;height:10px;border-radius:3px}
.vb-tl{flex:none;min-height:80px;position:relative;margin:0 30px}.vb-tl .ax{position:absolute;left:0;right:0;top:50%;height:2px;background:var(--b-s3)}.vb-tl .now{position:absolute;top:22%;bottom:22%;width:2px;background:var(--b-ac);box-shadow:0 0 0 3px color-mix(in srgb,var(--b-ac) 22%,transparent)}.vb-tl span{position:absolute;top:50%;transform:translate(-50%,-5px);display:flex;flex-direction:column;align-items:center;gap:4px;width:58px}.vb-tl span.up{transform:translate(-50%,calc(-100% + 5px));flex-direction:column-reverse}.vb-tl span i{width:10px;height:10px;border-radius:50%;background:var(--ec);box-shadow:0 0 0 2px var(--b-surf)}.vb-tl span em{font-style:normal;font-size:8.5px;color:var(--b-t1);white-space:nowrap}.vb-tl span small{font-family:var(--b-mono);font-size:8px;color:var(--b-t3)}
.vb-pcard{display:grid;grid-template-columns:1fr 1fr;gap:6px;flex:1;min-height:0;align-content:center}.vb-pcard div{background:var(--b-surf2);border-radius:var(--b-r);padding:6px 8px;display:flex;flex-direction:column;gap:2px;min-width:0}.vb-pcard b{font-family:var(--b-mono);font-size:15px;font-weight:700;line-height:1}.vb-pcard span{font-size:8.5px;color:var(--b-t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vb-chk{display:flex;flex-direction:column;gap:6px}.vb-ck{display:flex;align-items:center;gap:8px;font-size:10px;color:var(--b-t2)}.vb-ck i{width:13px;height:13px;border-radius:4px;flex-shrink:0;display:flex;align-items:center;justify-content:center;box-shadow:inset 0 0 0 1.5px var(--b-bd2);font-style:normal;font-size:9px;color:var(--b-on)}.vb-ck.done i{background:var(--b-ac2);box-shadow:none}.vb-ck.done{color:var(--b-t1)}.vb-ck small{margin-left:auto;color:var(--b-t3);font-family:var(--b-mono);font-size:8.5px}
/* events */
.vb-log{flex:1;min-height:0;overflow:hidden;display:flex;flex-direction:column;gap:1px;font-family:var(--b-mono);font-size:9px;line-height:1.55}.vb-log span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-log .t{color:var(--b-t3)}.vb-log .k{opacity:.8}
.vb-lane{display:flex;gap:6px;flex:1;min-height:0;align-items:stretch;overflow:hidden}.vb-lane div{flex:1;min-width:0;background:var(--b-surf2);border-radius:var(--b-r);padding:6px 7px;display:flex;flex-direction:column;gap:2px;box-shadow:inset 2px 0 0 var(--lc)}.vb-lane b{font-size:9px;color:var(--lc);text-transform:uppercase;letter-spacing:.08em}.vb-lane em{font-style:normal;font-size:9.5px;color:var(--b-t1);line-height:1.3;overflow:hidden}.vb-lane i{font-style:normal;font-family:var(--b-mono);font-size:8px;color:var(--b-t3);margin-top:auto}
/* graphs */
.vb-topo{flex:none;min-height:0;position:relative;width:100%}.vb-topo .tn{position:absolute;transform:translate(-50%,-50%);border-radius:50%}.vb-topo .tn.hollow{background:transparent!important;box-shadow:inset 0 0 0 1.5px var(--c)}.vb-topo .tn:not(.hollow){background:var(--c)}.vb-topo .te{position:absolute;height:1px;transform-origin:0 50%;background:var(--b-bd2)}.vb-topo .fe{position:absolute;transform-origin:0 50%;border-radius:2px;background:var(--b-ac);opacity:.5}.vb-topo .fn{position:absolute;transform:translate(-50%,-50%);border-radius:3px;padding:2px 6px;font-size:8.5px;background:var(--b-surf2);white-space:nowrap}
/* the composites */
.vb-nstat{display:flex;align-items:center;gap:7px;font-size:10px;color:var(--b-t2)}.vb-nstat i{width:8px;height:8px;border-radius:50%}
.vb-ncard,.vb-glance{display:grid;grid-template-columns:1fr 1fr;gap:6px;flex:1;min-height:0;align-content:center}.vb-ncard div,.vb-glance div{background:var(--b-surf2);border-radius:var(--b-r);padding:6px 8px;display:flex;flex-direction:column;gap:2px;min-width:0}.vb-ncard b,.vb-glance b{font-family:var(--b-mono);font-size:15px;font-weight:700;line-height:1}.vb-ncard b small{font-size:9px;font-weight:400;color:var(--b-t3);margin-left:1px}.vb-ncard span,.vb-glance span{font-size:8.5px;color:var(--b-t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-ncard .bar{height:4px;border-radius:2px;background:var(--b-s3);margin-top:3px;overflow:hidden}.vb-ncard .bar i{display:block;height:100%}.vb-glance .vb-spk{height:16px;margin-top:3px}
.vb-cmpr{display:grid;grid-template-columns:1fr 70px 1fr;gap:8px;align-items:center;font-size:10px}.vb-cmpr .n{grid-column:2;text-align:center;color:var(--b-t2);order:2;white-space:nowrap;overflow:hidden}.vb-cmpr .side{display:flex;align-items:center;gap:6px;height:12px}.vb-cmpr .side.l{order:1;justify-content:flex-end}.vb-cmpr .side.r{order:3}.vb-cmpr .side i{display:block;height:8px;border-radius:4px}.vb-cmpr .side b{font-family:var(--b-mono);font-size:9.5px;color:var(--b-t1);width:34px;text-align:right}.vb-cmpr .side.r b{text-align:left}
.vb-carp{flex:1;min-height:0;display:flex;align-items:center;gap:12px}.vb-carp .vb-ring{width:64px;height:64px}.vb-carp .vb-ring > span{font-size:13px}
.vb-flist{flex:1;display:flex;flex-direction:column;gap:1px;font-size:9.5px;min-width:0}.vb-flist > span{display:grid;grid-template-columns:1fr 46px 50px 36px;gap:6px;align-items:center;height:19px}.vb-flist span i{width:6px;height:6px;border-radius:50%;display:inline-block;margin-right:6px;vertical-align:middle}.vb-flist .h{color:var(--b-t3);font-size:8px;text-transform:uppercase;letter-spacing:.08em}.vb-flist .m{font-family:var(--b-mono);color:var(--b-t2);text-align:right;white-space:nowrap;overflow:hidden}
.vb-rings{width:96px;height:96px;flex-shrink:0}.vb-rings svg{width:96px;height:96px;transform:rotate(-90deg)}`;
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
    constructor() { super(); this._sh = this.attachShadow({ mode: 'open' }); this._rec = null; this._data = undefined; this._drawn = ''; this._timer = null; this._ro = null; this._auto = 'm'; this._kids = {}; this._ui = {}; }
    static get observedAttributes() { return ['record', 'size', 'base', 'template-id']; }
    get record() { return this._rec; }
    set record(v) { this._rec = normalise(v); this._data = (v && v.data !== undefined) ? v.data : undefined; this._drawn = ''; this._kids = {}; if (this.isConnected) this._boot(); }
    get base() { return this.getAttribute('base') || window._veraBase || ''; }
    get size() { const s = this.getAttribute('size'); return s && s !== 'auto' && SIZES.includes(s) ? s : (s === 'auto' ? this._auto : (this._rec ? this._rec.frame.size : 'm')); }
    connectedCallback() {
      const a = this.getAttribute('record'); if (a && !this._rec) { try { this.record = JSON.parse(a); } catch (_) { this._rec = normalise({}); } }
      if (window.ResizeObserver && !this._rz) { let last = 0; this._rz = new ResizeObserver(() => { const h = this.clientHeight || 0; if (Math.abs(h - last) > 12) { last = h; this._measured = ''; if (this._rec) this.render(); } }); this._rz.observe(this); }
      if (this.getAttribute('size') === 'auto' && window.ResizeObserver && !this._ro) { this._ro = new ResizeObserver(() => { const s = sizeForWidth(this.clientWidth || 300); if (s !== this._auto) { this._auto = s; this.render(); this.dispatchEvent(new CustomEvent('widget:resize', { bubbles: true, composed: true, detail: { size: s } })); } }); this._ro.observe(this); }
      this._boot();
    }
    disconnectedCallback() { if (this._timer) { clearInterval(this._timer); this._timer = null; } if (this._ro) { this._ro.disconnect(); this._ro = null; } if (this._rz) { this._rz.disconnect(); this._rz = null; } }
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
      const opts = { record: rec, draw: rec.draw, title: rec.title, panel: rec.panel, base: this.base, kids: this._kids || {}, ui: this._ui, height: this._bodyH || undefined };   // L and XL compose around the form
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
      const setUi = (kv) => { const i = kv.indexOf(':'); if (i < 0) return; const k = kv.slice(0, i), v = kv.slice(i + 1); this._ui[k] = /^-?\d+(\.\d+)?$/.test(v) ? Number(v) : v; };
      this._sh.querySelectorAll('[data-vb-set]').forEach((b) => b.addEventListener('click', (ev) => { ev.stopPropagation(); setUi(b.dataset.vbSet); if (b.dataset.vbSet2) setUi(b.dataset.vbSet2); this.render(); }));
      this._sh.querySelectorAll('[data-vb-input]').forEach((i) => i.addEventListener('input', () => { this._ui[i.dataset.vbInput] = /^-?\d+(\.\d+)?$/.test(i.value) ? Number(i.value) : i.value; this.render(); }));
      this._sh.querySelectorAll('[data-vb-link]').forEach((a) => a.addEventListener('click', (ev) => { ev.preventDefault(); this.dispatchEvent(new CustomEvent('widget:open', { bubbles: true, composed: true, detail: { record: this._rec, href: a.dataset.vbLink, key: key(this._rec) } })); }));
      hydrate(this._sh);
      if (!small && this._measured !== size) { const b = this._sh.querySelector('.vw-body'); const hb = b ? b.clientHeight : 0; this._measured = size; if (hb > 48 && Math.abs(hb - (this._bodyH || 0)) > 12) { this._bodyH = hb; this.render(); return; } }
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
